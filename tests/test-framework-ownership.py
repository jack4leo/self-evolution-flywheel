#!/usr/bin/env python3
"""End-to-end contract tests for framework-owned reinstall/update behavior.

These tests intentionally exercise the public installer rather than importing its
implementation.  They are English test code; the user-facing reports remain the
repository's responsibility.
"""
from __future__ import annotations

import json
import os
import pty
import select
import re
import signal
import time
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
FRAMEWORK = (
    ".agent-maintenance/flywheel-install.sh",
    ".agent-maintenance/installer-transaction.py",
    ".agent-maintenance/framework-ownership.py",
    ".agent-maintenance/session-start.sh",
    "scripts/freeze-review-range.py",
    "scripts/validate_lesson_promotions.py",
    "scripts/watermark.py",
    "scripts/write-frozen-review.py",
    ".github/workflows/lesson-promotion-gate.yml",
)
PROFILE_BEGIN = "<!-- BEGIN SELF-EVOLUTION-FLYWHEEL PROFILE FRAMEWORK -->"
PROFILE_END = "<!-- END SELF-EVOLUTION-FLYWHEEL PROFILE FRAMEWORK -->"
FACTS_BEGIN = "<!-- BEGIN SELF-EVOLUTION-FLYWHEEL PROFILE FACTS -->"
FACTS_END = "<!-- END SELF-EVOLUTION-FLYWHEEL PROFILE FACTS -->"
ROUTING_BEGIN = b"<!-- BEGIN SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->"
ROUTING_END = b"<!-- END SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->"


class FrameworkOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="flywheel-ownership-")
        self.addCleanup(self.tmp.cleanup)
        self.area = Path(self.tmp.name)

    def project(self, name: str = "project") -> Path:
        root = self.area / name
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        return root

    def run_install(self, source: Path, target: Path, *extra: str, hooks: bool = False) -> subprocess.CompletedProcess[str]:
        args = ["bash", str(source / "install.sh"), "--target", str(target)]
        if not hooks:
            args.append("--no-hooks")
        return subprocess.run(
            [*args, *extra],
            cwd=target, text=True, capture_output=True, timeout=90,
        )

    def install(self, source: Path, target: Path) -> None:
        result = self.run_install(source, target)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @staticmethod
    def snapshot(root: Path) -> dict[str, bytes]:
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file() and not p.is_symlink()}

    def test_first_install_seeds_framework_and_only_project_local_entry(self) -> None:
        target = self.project()
        business_install = b"#!/bin/sh\necho business application\n"
        (target / "install.sh").write_bytes(business_install)
        self.install(ROOT, target)
        self.assertEqual((target / "install.sh").read_bytes(), business_install)
        for relative in FRAMEWORK:
            with self.subTest(path=relative):
                self.assertTrue((target / relative).is_file(), relative)
        entry = target / ".agent-maintenance/flywheel-install.sh"
        self.assertTrue(entry.is_file())
        self.assertNotEqual(entry.read_bytes(), business_install)
        self.assertTrue((target / ".agent-maintenance/framework-version.json").is_file())
        for relative in ("LESSONS.md", "docs/code-standards.md", "docs/subsystems/README.md", "prompts/task-work.md"):
            self.assertTrue((target / relative).is_file(), relative)

    def test_identical_repeat_is_byte_stable_and_local_entry_works(self) -> None:
        target = self.project()
        self.install(ROOT, target)
        before = self.snapshot(target)
        result = subprocess.run(
            ["bash", str(target / ".agent-maintenance/flywheel-install.sh"), "--target", str(target), "--no-hooks"],
            cwd=target, text=True, capture_output=True, timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(before, self.snapshot(target))
        self.assertIn("UNCHANGED", result.stdout)

    def test_source_snapshot_upgrade_updates_framework_but_preserves_project_content(self) -> None:
        target = self.project()
        self.install(ROOT, target)
        user_files = {
            "AGENTS.md": ROOT.joinpath("AGENTS.md").read_bytes() + b"user-owned bytes\r\n",
            "docs/project-profile.md": ROOT.joinpath("docs/project-profile.md").read_bytes().replace(b"- Project name / one-line purpose: `UNKNOWN`", b"- Project name / one-line purpose: USER FACTS"),
            "docs/code-standards.md": b"project customized standards\n",
            "docs/subsystems/README.md": b"project subsystem facts\n",
            "LESSONS.md": b"# Lessons\nuser lesson\n",
            "docs/custom.txt": b"custom project file\n",
        }
        for relative, data in user_files.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        source = self.area / "source-v2"
        shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        changed = (".agent-maintenance/session-start.sh", ".agent-maintenance/installer-transaction.py",
                   ".agent-maintenance/framework-ownership.py", "install.sh")
        for relative in changed:
            path = source / relative
            if path.exists():
                path.write_bytes(path.read_bytes() + b"\n# framework version two\n")
        result = self.run_install(source, target)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for relative, data in user_files.items():
            self.assertEqual((target / relative).read_bytes(), data, relative)
        for relative in FRAMEWORK:
            if relative in changed and (source / relative).is_file():
                self.assertEqual((target / relative).read_bytes(), (source / relative).read_bytes(), relative)
        version = json.loads((target / ".agent-maintenance/framework-version.json").read_text())
        self.assertTrue(version.get("framework_hashes"), version)
        self.assertEqual((target / FRAMEWORK[0]).read_bytes(), (source / "install.sh").read_bytes())
        self.assertIsNone(version["source_revision"])
        self.assertEqual(len(version["content_digest"]), 64)

    def test_local_entry_retains_template_provenance_not_target_git_head(self) -> None:
        target = self.project()
        self.install(ROOT, target)
        version_path = target / ".agent-maintenance/framework-version.json"
        version = version_path.read_bytes()
        (target / "business.txt").write_text("project commit unrelated to the framework\n")
        subprocess.run(["git", "-C", str(target), "add", "business.txt"], check=True)
        subprocess.run(["git", "-C", str(target), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Project facts"], check=True)
        root_entry = target / "install.sh"
        os.mkfifo(root_entry)  # Must not be opened, copied or inspected as a source.
        result = subprocess.run(["bash", str(target / FRAMEWORK[0]), "--target", str(target), "--no-hooks"], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(version_path.read_bytes(), version)
        self.assertTrue(__import__("stat").S_ISFIFO(root_entry.lstat().st_mode))

    def test_local_entry_cannot_self_bless_customized_framework(self) -> None:
        target = self.project()
        self.install(ROOT, target)
        path = target / ".agent-maintenance/session-start.sh"
        path.write_bytes(path.read_bytes() + b"\n# custom local code\n")
        before = self.snapshot(target)
        result = subprocess.run(["bash", str(target / FRAMEWORK[0]), "--target", str(target), "--no-hooks"], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("diff", result.stdout + result.stderr)
        self.assertEqual(self.snapshot(target), before)

    def test_manifest_cannot_expand_ownership_or_accept_duplicate_keys(self) -> None:
        for case in ("extra-path", "duplicate-schema"):
            with self.subTest(case=case):
                target = self.project(case)
                self.install(ROOT, target)
                path = target / ".agent-maintenance/framework-version.json"
                if case == "extra-path":
                    config = json.loads(path.read_bytes())
                    config["framework_hashes"]["install.sh"] = "0" * 64
                    path.write_text(json.dumps(config))
                else:
                    path.write_bytes(path.read_bytes().replace(b'"schema": 1', b'"schema": 1, "schema": 1', 1))
                before = self.snapshot(target)
                result = self.run_install(ROOT, target)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.snapshot(target), before)

    def test_agents_requires_exactly_one_ordered_managed_pair_and_rejects_unchanged(self) -> None:
        cases = {
            "missing": b"# User\n",
            "duplicate": (ROUTING_BEGIN + b"\nmanaged\n" + ROUTING_END + b"\n" + ROUTING_BEGIN + b"\nagain\n" + ROUTING_END + b"\n"),
            "wrong-order": (ROUTING_END + b"\nmanaged\n" + ROUTING_BEGIN + b"\n"),
        }
        for name, content in cases.items():
            with self.subTest(case=name):
                target = self.project(name)
                path = target / "AGENTS.md"
                path.write_bytes(content)
                before = self.snapshot(target)
                result = self.run_install(ROOT, target)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(before, self.snapshot(target))

    def test_profile_framework_and_facts_blocks_replace_only_their_own_bytes(self) -> None:
        target = self.project()
        original = (PROFILE_BEGIN + "\nold framework\n" + PROFILE_END + "\n" +
                    "user bytes\r\n" + FACTS_BEGIN + "\nold facts\n" + FACTS_END + "\n").encode()
        profile = target / "docs/project-profile.md"
        profile.parent.mkdir(parents=True)
        profile.write_bytes(original)
        self.install(ROOT, target)
        updated = profile.read_bytes()
        self.assertIn(PROFILE_BEGIN.encode(), updated)
        self.assertIn(FACTS_BEGIN.encode(), updated)
        self.assertIn(b"user bytes\r\n", updated)
        self.assertNotIn(b"old framework", updated)
        self.assertEqual(updated[updated.index(FACTS_BEGIN.encode()):], original[original.index(FACTS_BEGIN.encode()):])
        self.assertIn(b"old facts", updated)
        self.assertEqual(updated.count(PROFILE_BEGIN.encode()), 1)
        self.assertEqual(updated.count(FACTS_BEGIN.encode()), 1)

    def test_unknown_framework_script_is_rejected_with_diff_and_no_changes(self) -> None:
        target = self.project()
        self.install(ROOT, target)
        path = target / ".agent-maintenance/session-start.sh"
        path.write_bytes(path.read_bytes() + b"\n# unauthorized local edit\n")
        before = self.snapshot(target)
        result = self.run_install(ROOT, target)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("diff", (result.stdout + result.stderr).lower())
        self.assertEqual(before, self.snapshot(target))

    def test_json_environment_plugins_marketplaces_and_other_hooks_are_preserved(self) -> None:
        target = self.project()
        settings = target / ".claude/settings.json"
        settings.parent.mkdir()
        original = {
            "env": {"KEEP": "yes", "HUGE": 123456789012345678901234567890123456789}, "plugins": {"local": {"enabled": True}},
            "marketplaces": ["private"], "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "user-stop"}]}],
                "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "user-start"}]}],
            }, "userField": [1, None, "x"],
        }
        settings.write_bytes(json.dumps(original, indent=2).encode() + b"\n")
        result = self.run_install(ROOT, target, hooks=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        actual = json.loads(settings.read_text())
        for key in ("env", "plugins", "marketplaces", "userField"):
            self.assertEqual(actual[key], original[key])
        self.assertEqual(actual["hooks"]["Stop"], original["hooks"]["Stop"])
        self.assertEqual(actual["hooks"]["SessionStart"][0], original["hooks"]["SessionStart"][0])
        canonical = 'bash "${CLAUDE_PROJECT_DIR}/.agent-maintenance/session-start.sh" claude'
        self.assertEqual(sum(h.get("command") == canonical for s in actual["hooks"]["SessionStart"] for h in s["hooks"]), 1)
        before = settings.read_bytes()
        repeat = self.run_install(ROOT, target, hooks=True)
        self.assertEqual(repeat.returncode, 0, repeat.stdout + repeat.stderr)
        self.assertEqual(settings.read_bytes(), before)

    def test_faulted_write_keeps_original_bytes_and_recovery_journal(self) -> None:
        target = self.project()
        self.install(ROOT, target)
        relative = ".agent-maintenance/installer-transaction.py"
        original = (target / relative).read_bytes()
        source = self.area / "fault-source"
        shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        helper = source / relative
        token = "    if function(parent_fd, os.fsencode(left), parent_fd, os.fsencode(right), 0x2) != 0:\n"
        self.assertIn(token, helper.read_text())
        helper.write_text(helper.read_text().replace(token, "    raise OSError('injected exchange failure')\n" + token, 1))
        result = self.run_install(source, target)
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertEqual((target / relative).read_bytes(), original)
        backups = list((target / ".agent-maintenance").glob(".installer-transaction.py.confirm-backup-*"))
        self.assertTrue(any(path.read_bytes() == helper.read_bytes() for path in backups), output)
        match = re.search(r"rollback snapshots retained for recovery at: (.+)", output)
        self.assertIsNotNone(match, output)
        workdir = Path(match.group(1).strip())
        self.addCleanup(shutil.rmtree, workdir, True)
        journal = json.loads((workdir / "transaction.json").read_bytes())
        self.assertTrue(any(op["path"] == relative and op.get("confirmation_backup") for op in journal["operations"]))

    def _pty_install(self, target: Path, answer: bytes) -> tuple[int, bytes]:
        pid, master = pty.fork()
        if pid == 0:
            os.chdir(target)
            os.execv("/bin/bash", ["/bin/bash", str(ROOT / "install.sh"), "--target", str(target)])
        output = bytearray()
        sent = False
        deadline = time.monotonic() + 60
        status = None
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], .1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                output.extend(chunk)
                if not sent and b"[y/N] " in output:
                    os.write(master, answer)
                    sent = True
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                break
        else:
            os.kill(pid, signal.SIGKILL)
            _, status = os.waitpid(pid, 0)
        while select.select([master], [], [], .2)[0]:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        os.close(master)
        self.assertTrue(sent, bytes(output).decode(errors="replace"))
        return os.waitstatus_to_exitcode(status), bytes(output)

    def test_ambiguous_flywheel_hook_yes_adds_canonical_without_removal(self) -> None:
        target = self.project(); settings = target / ".claude/settings.json"; settings.parent.mkdir()
        settings.write_text(json.dumps({"hooks": {"SessionStart": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo flywheel-custom"}]}]}}) + "\n")
        code, output = self._pty_install(target, b"y\n")
        self.assertEqual(code, 0, output.decode(errors="replace"))
        actual = json.loads(settings.read_text())
        commands = [h.get("command") for s in actual["hooks"]["SessionStart"] for h in s["hooks"]]
        self.assertIn("echo flywheel-custom", commands)
        self.assertEqual(commands.count('bash "${CLAUDE_PROJECT_DIR}/.agent-maintenance/session-start.sh" claude'), 1)

    def test_ambiguous_flywheel_hook_without_terminal_preserves_exact_bytes(self) -> None:
        target = self.project(); settings = target / ".claude/settings.json"; settings.parent.mkdir()
        original = b'{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"session-start.sh custom"}]}]}}\r\n'
        settings.write_bytes(original); result = self.run_install(ROOT, target, hooks=True)
        self.assertEqual(settings.read_bytes(), original)

    def test_ordinary_hook_conflict_is_additive_without_prompt(self) -> None:
        target = self.project(); settings = target / ".claude/settings.json"; settings.parent.mkdir()
        settings.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo ordinary"}]}]}}) + "\n")
        result = self.run_install(ROOT, target, hooks=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr); self.assertIn("echo ordinary", settings.read_text())

    def test_owned_symlink_and_fifo_are_rejected_without_reading(self) -> None:
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind):
                target = self.project(kind); path = target / ".agent-maintenance/session-start.sh"; path.parent.mkdir()
                if kind == "symlink": path.symlink_to(self.area / "outside")
                else: os.mkfifo(path)
                before = self.snapshot(target); result = self.run_install(ROOT, target)
                self.assertNotEqual(result.returncode, 0); self.assertEqual(before, self.snapshot(target))

    def test_malformed_profile_markers_fail_closed(self) -> None:
        target = self.project(); profile = target / "docs/project-profile.md"; profile.parent.mkdir()
        profile.write_bytes((PROFILE_BEGIN + "\nframework\n" + FACTS_END + "\n" + FACTS_BEGIN + "\nfacts\n" + PROFILE_END + "\n").encode())
        before = self.snapshot(target); result = self.run_install(ROOT, target)
        self.assertNotEqual(result.returncode, 0); self.assertEqual(before, self.snapshot(target))

    def test_agents_invalid_utf8_and_mixed_newlines_are_preserved(self) -> None:
        target = self.project(); path = target / "AGENTS.md"
        data = b"# User\r\ninvalid: \xff\xfe\n" + ROOT.joinpath("AGENTS.md").read_bytes() + b"outside\r\n"
        path.write_bytes(data); before = path.read_bytes(); result = self.run_install(ROOT, target)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
