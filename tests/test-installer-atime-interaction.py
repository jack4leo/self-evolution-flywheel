#!/usr/bin/env python3
"""Installer confirmation tests for real PTYs, EOF/no-TTY, and atime semantics."""
from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import stat
import sys
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / ".agent-maintenance/installer-transaction.py"

def load_helper():
    spec = importlib.util.spec_from_file_location("installer_transaction", HELPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module

M = load_helper()
ATIME_EXPECTATION = os.environ.get("FLYWHEEL_EXPECT_ATIME_CHANGE")


def _open_read(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    noatime = getattr(os, "O_NOATIME", 0)
    try:
        return os.open(path, flags | noatime)
    except OSError as exc:
        if not noatime or exc.errno not in {errno.EPERM, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP}:
            raise
        return os.open(path, flags)


def metadata(path: Path) -> dict:
    """Snapshot fd metadata with the canonical native xattr/ACL helpers."""
    fd = _open_read(path)
    try:
        info = os.fstat(fd)
        attrs = M.xattrs(fd)
        if attrs is None:
            raise AssertionError("canonical xattr inspection is unavailable")
        return {
            "dev": info.st_dev,
            "ino": info.st_ino,
            "nlink": info.st_nlink,
            "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "gid": info.st_gid,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
            "atime_ns": info.st_atime_ns,
            "flags": getattr(info, "st_flags", None),
            "xattrs": attrs,
            "protected": M.protected_metadata(fd),
        }
    finally:
        os.close(fd)


def content(path: Path) -> bytes:
    fd = _open_read(path)
    try:
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def fixture_baseline(path: Path) -> tuple[dict, bytes]:
    """Read seed bytes first, then restore atime and take the baseline stat."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        original_mtime = os.fstat(fd).st_mtime_ns
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        original = b"".join(chunks)
        os.utime(fd, ns=(1_000_000_000, original_mtime))
    finally:
        os.close(fd)
    return metadata(path), original


def complete_snapshot(path: Path) -> tuple[dict, bytes]:
    before = metadata(path)
    data = content(path)
    return before, data


def run_pty(command: str, answer: str | None = None, eof: bool = False, before_answer=None):
    pid, master = pty.fork()
    if pid == 0:
        os.execv("/bin/bash", ["bash", "-c", command])
    output = bytearray()
    sent = False
    status = None
    deadline = time.monotonic() + 40
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        chunk = b""
                    else:
                        raise
                output.extend(chunk)
                if not sent and b"[y/N] " in output:
                    if before_answer:
                        before_answer(bytes(output))
                    if eof:
                        # Send terminal EOF; closing the PTY master would SIGHUP the child.
                        os.write(master, b"\x04")
                    else:
                        os.write(master, (answer or "").encode() + b"\n")
                    sent = True
            waited, child_status = os.waitpid(pid, os.WNOHANG)
            if waited:
                status = child_status
                break
        if status is None:
            waited, child_status = os.waitpid(pid, 0)
            status = child_status
        while master is not None and select.select([master], [], [], 0.1)[0]:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        return os.waitstatus_to_exitcode(status), bytes(output), sent
    finally:
        if status is None:
            os.killpg(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        if master is not None:
            os.close(master)


class InstallerAtimeInteractionTests(unittest.TestCase):
    def setUp(self):
        root = os.environ.get("TMPDIR")
        self.tmp = tempfile.TemporaryDirectory(prefix="flywheel-atime-interaction-", dir=root)
        self.area = Path(self.tmp.name).resolve()
        self.project = self.area / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.original = b"{\n  \"env\": {\"KEEP\": \"yes\"},\n  \"hooks\": {\"Stop\": [{\"hooks\": [{\"type\": \"command\", \"command\": \"bash custom-session-start.sh claude\"}]}]}\n}\n"

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, no_hooks=True):
        args = ["bash", str(ROOT / "install.sh"), "--target", str(self.project)]
        if no_hooks:
            args.append("--no-hooks")
        return shlex.join(args)

    def seed_agents(self):
        path = self.project / "AGENTS.md"
        path.write_bytes(self.original)
        return path

    def seed_claude(self):
        path = self.project / ".claude/settings.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"env": {"KEEP": "yes"}, "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "bash custom-session-start.sh claude"}]}]}}, indent=2) + "\n")
        return path

    def assert_unchanged_except_atime(self, path, before, before_bytes):
        after, after_bytes = complete_snapshot(path)
        for key, value in before.items():
            if key == "atime_ns":
                continue
            self.assertEqual(after[key], value, f"{path} metadata field changed: {key}")
        self.assertEqual(after_bytes, before_bytes)

    def assert_expected_read_atime(self, before, after):
        # Mount read semantics describe one inode, never a pathname replacement.
        self.assertEqual((after["dev"], after["ino"]), (before["dev"], before["ino"]),
                         "read-atime expectations require the same inode")
        if ATIME_EXPECTATION is None and sys.platform.startswith("linux"):
            return
        if ATIME_EXPECTATION == "1":
            self.assertNotEqual(after["atime_ns"], before["atime_ns"])
        elif ATIME_EXPECTATION == "0":
            self.assertEqual(after["atime_ns"], before["atime_ns"])
        else:
            self.fail(f"invalid FLYWHEEL_EXPECT_ATIME_CHANGE={ATIME_EXPECTATION!r}")

    def test_agents_yes_installs_backup_and_retry_is_idempotent(self):
        path = self.seed_claude()
        before, original = fixture_baseline(path)
        code, output, sent = run_pty(self.command(False), "y")
        self.assertEqual((code, sent), (0, True), output)
        self.assertNotEqual(path.read_bytes(), original)
        backups = list(path.parent.glob(".settings.json.confirm-backup-*"))
        self.assertTrue(backups, output)
        self.assertTrue(any(backup.read_bytes() == original for backup in backups))
        first = path.read_bytes()
        code, output, sent = run_pty(self.command(), None)
        self.assertEqual((code, sent), (0, False), output)
        self.assertEqual(path.read_bytes(), first)
        self.assertEqual(metadata(path)["xattrs"], before["xattrs"])

    def test_agents_no_eof_and_notty_preserve_all_non_atime_metadata(self):
        for mode in ("no", "eof", "notty"):
            with self.subTest(mode=mode):
                path = self.seed_claude()
                before, original = fixture_baseline(path)
                if mode == "notty":
                    result = subprocess.run(self.command(False), shell=True, input=b"y\n", stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, timeout=30)
                    code, output, sent = result.returncode, result.stdout, False
                else:
                    code, output, sent = run_pty(self.command(False), "n" if mode == "no" else None,
                                                 eof=mode == "eof")
                if mode == "eof":
                    self.assertEqual(code, 0, output)
                    self.assertTrue(sent)
                elif mode == "notty":
                    self.assertEqual(code, 0, output)
                    self.assertFalse(sent)
                else:
                    self.assertEqual(code, 0, output)
                    self.assertTrue(sent)
                after = metadata(path)
                self.assert_expected_read_atime(before, after)
                self.assert_unchanged_except_atime(path, before, original)
                if mode == "notty":
                    self.assertNotIn(b"[y/N]", output)
                    self.assertIn(b"no interactive terminal", output)

    def test_claude_yes_and_declines_use_same_metadata_contract(self):
        for answer in ("y", "n"):
            with self.subTest(answer=answer):
                # Each provider confirmation starts from an otherwise empty root;
                # the successful case creates AGENTS.md as part of installation.
                if (self.project / "AGENTS.md").exists():
                    (self.project / "AGENTS.md").unlink()
                path = self.seed_claude()
                before, original = fixture_baseline(path)
                code, output, sent = run_pty(self.command(False), answer)
                self.assertEqual(sent, True, output)
                if answer == "y":
                    self.assertEqual(code, 0, output)
                    self.assertNotEqual(path.read_bytes(), original)
                    backups = list(path.parent.glob(".settings.json.confirm-backup-*"))
                    self.assertTrue(backups, output)
                    self.assertTrue(any(backup.read_bytes() == original for backup in backups))
                else:
                    self.assertEqual(code, 0, output)
                    self.assert_unchanged_except_atime(path, before, original)

    def test_claude_eof_and_notty_are_safe(self):
        for mode in ("eof", "notty"):
            with self.subTest(mode=mode):
                path = self.seed_claude()
                before, original = fixture_baseline(path)
                if mode == "notty":
                    result = subprocess.run(self.command(False), shell=True, input=b"y\n",
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
                    code, output, sent = result.returncode, result.stdout, False
                else:
                    code, output, sent = run_pty(self.command(False), eof=True)
                self.assertEqual(code, 0, output)
                self.assertFalse(sent) if mode == "notty" else self.assertTrue(sent)
                after = metadata(path)
                self.assert_expected_read_atime(before, after)
                self.assert_unchanged_except_atime(path, before, original)

    def test_read_atime_expectations_reject_replaced_inodes(self):
        before = {"dev": 1, "ino": 10, "atime_ns": 1_000_000_000}
        after = {**before, "ino": 11}
        with self.assertRaisesRegex(AssertionError, "same inode"):
            self.assert_expected_read_atime(before, after)

    def test_marked_agents_auto_update_without_prompt(self):
        path = self.project / "AGENTS.md"
        begin = b"<!-- BEGIN SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->"
        end = b"<!-- END SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->"
        prefix, suffix = b"user prefix\r\n", b"\nuser suffix\r\n"
        path.write_bytes(prefix + begin + b"\nold managed route\n" + end + suffix)
        os.chmod(path, 0o640)
        os.utime(path, ns=(1_000_000_000, 2_000_000_000))
        seed_fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            M.xattr_set(seed_fd, "user.flywheel.agents-atime", b"\x00preserved\xff")
        finally:
            os.close(seed_fd)
        # No writable fixture descriptor remains open during the installation.
        original_fd = _open_read(path)
        try:
            before, original = fixture_baseline(path)
            pinned = os.fstat(original_fd)
            self.assertEqual((pinned.st_dev, pinned.st_ino), (before["dev"], before["ino"]))
            code, output, sent = run_pty(self.command(), "y")
            self.assertEqual((code, sent), (0, False), output)

            # Successful exchange changes the active inode. noatime suppresses
            # read side effects, not creation timestamps on a fresh staging inode.
            active = metadata(path)
            self.assertNotEqual((active["dev"], active["ino"]), (before["dev"], before["ino"]))
            self.assertNotEqual(active["atime_ns"], before["atime_ns"],
                                "replacement must not inherit the original's stale atime")
            source = (ROOT / "AGENTS.md").read_bytes()
            managed = source[source.index(begin):source.index(end) + len(end)]
            self.assertEqual(content(path), prefix + managed + suffix)
            for key in ("nlink", "mode", "uid", "gid", "mtime_ns", "flags", "xattrs", "protected"):
                self.assertEqual(active[key], before[key], key)

            backups = list(path.parent.glob(".AGENTS.md.confirm-backup-*"))
            self.assertEqual(len(backups), 1, output)
            backup = backups[0]
            displaced = metadata(backup)
            pinned = os.fstat(original_fd)
            self.assertEqual((displaced["dev"], displaced["ino"]), (pinned.st_dev, pinned.st_ino))
            # This is the original inode: the strict normal/noatime read contract
            # still applies, as do exact original bytes and protected metadata.
            self.assert_expected_read_atime(before, displaced)
            self.assertEqual(content(backup), original)
            for key in ("dev", "ino", "nlink", "mode", "uid", "gid", "size", "mtime_ns", "flags", "xattrs", "protected"):
                self.assertEqual(displaced[key], before[key], key)

            # Same-version retry is read-only for the now-active inode. Reset the
            # private fixture's atime to make read side effects observable again.
            retry_before, retry_bytes = fixture_baseline(path)
            code, output, sent = run_pty(self.command(), None)
            self.assertEqual((code, sent), (0, False), output)
            self.assert_expected_read_atime(retry_before, metadata(path))
            self.assert_unchanged_except_atime(path, retry_before, retry_bytes)
            self.assertEqual(list(path.parent.glob(".AGENTS.md.confirm-backup-*")), backups)
            self.assertEqual(content(backup), original)
        finally:
            os.close(original_fd)

    def test_atime_read_after_proposal_is_allowed_but_ctime_mutation_is_rejected(self):
        path = self.seed_claude()
        before, original = fixture_baseline(path)

        def read_after_prompt(_output):
            # Helper reads validate stage identity/ctime and deliberately do not restore atime.
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                staged = M.identity(fd)
                M.read_open_file(fd)
                observed = M.identity(fd)
                self.assertEqual(observed["ctime_ns"], staged["ctime_ns"])
            finally:
                os.close(fd)

        code, output, sent = run_pty(self.command(False), "y", before_answer=read_after_prompt)
        self.assertEqual((code, sent), (0, True), output)
        after = metadata(path)
        # This probe intentionally performs a normal helper read after the
        # proposal; its atime change is allowed and is not an N/EOF/no-TTY
        # confirmation-path expectation.

        path.unlink()
        path.write_bytes(original)
        before, original = fixture_baseline(path)

        def mutate_and_restore(_output):
            fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            try:
                original_bytes = os.pread(fd, before["size"], 0)
                os.pwrite(fd, b"X" * len(original_bytes), 0)
                os.pwrite(fd, original_bytes, 0)
                os.fchmod(fd, before["mode"] ^ 0o100)
                os.fchmod(fd, before["mode"])
                adversary = "user.flywheel-adversary"
                if M.xattrs(fd) is None:
                    raise AssertionError("canonical xattr inspection is unavailable")
                M.xattr_set(fd, adversary, b"changed")
                M.xattr_remove(fd, adversary)
                # Restore timestamps last; only ctime should remain different.
                os.utime(fd, ns=(before["atime_ns"], before["mtime_ns"]))
            finally:
                os.close(fd)

        code, output, sent = run_pty(self.command(False), "y", before_answer=mutate_and_restore)
        self.assertNotEqual(code, 0, output)
        self.assertTrue(sent)
        after, after_bytes = complete_snapshot(path)
        self.assertNotEqual(after["ctime_ns"], before["ctime_ns"])
        for key in ("dev", "ino", "nlink", "mode", "uid", "gid", "size", "mtime_ns", "xattrs", "protected", "flags"):
            self.assertEqual(after[key], before[key], f"adversary changed protected field: {key}")
        self.assertEqual(after_bytes, original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
