#!/usr/bin/env python3
"""Darwin contract tests for confirmed metadata-preserving transactions.

These tests intentionally fail (rather than skip) when a Darwin primitive needed
by the contract is unavailable.  Non-Darwin runners skip the whole module.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.util
import errno
import hashlib
import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / ".agent-maintenance/installer-transaction.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("installer_macos_metadata_contract", HELPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


M = load_helper()
LIBC = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
ACL_EXTENDED = 0x100
UF_HIDDEN = 0x00008000
UF_IMMUTABLE = 0x00000002


def native(name, restype=ctypes.c_int, argtypes=()):
    fn = getattr(LIBC, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


def checked(fn, *args):
    ctypes.set_errno(0)
    result = fn(*args)
    if result == -1:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, os.strerror(error), getattr(fn, "__name__", "native"))
    return result


def native_acl_bytes(fd):
    get = native("acl_get_fd_np", ctypes.c_void_p, (ctypes.c_int, ctypes.c_int))
    size_fn = native("acl_size", ctypes.c_ssize_t, (ctypes.c_void_p,))
    copy = native("acl_copy_ext_native", ctypes.c_ssize_t,
                  (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t))
    free = native("acl_free", ctypes.c_int, (ctypes.c_void_p,))
    ctypes.set_errno(0)
    acl = get(fd, ACL_EXTENDED)
    if not acl:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, os.strerror(error), "acl_get_fd_np")
    try:
        size = size_fn(acl)
        if size <= 0:
            raise OSError(errno.EIO, "invalid native ACL size")
        buf = ctypes.create_string_buffer(size)
        if copy(buf, acl, size) != size:
            raise OSError(errno.EIO, "native ACL serialization mismatch")
        return bytes(buf.raw[:size])
    finally:
        free(acl)


def set_native_acl(fd, raw):
    copy = native("acl_copy_int_native", ctypes.c_void_p,
                  (ctypes.c_void_p,))
    set_fn = native("acl_set_fd_np", ctypes.c_int,
                    (ctypes.c_int, ctypes.c_void_p, ctypes.c_int))
    free = native("acl_free", ctypes.c_int, (ctypes.c_void_p,))
    buf = ctypes.create_string_buffer(raw)
    acl = copy(buf, len(raw))
    if not acl:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, os.strerror(error), "acl_copy_int_native")
    try:
        checked(set_fn, fd, acl, ACL_EXTENDED)
    finally:
        free(acl)


def fchflags(fd, flags):
    fn = native("fchflags", ctypes.c_int, (ctypes.c_int, ctypes.c_uint))
    checked(fn, fd, flags)


def metadata(fd):
    info = os.fstat(fd)
    return {
        "dev": info.st_dev, "ino": info.st_ino, "nlink": info.st_nlink,
        "size": info.st_size, "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid, "gid": info.st_gid,
        "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
        "flags": int(info.st_flags),
    }


@unittest.skipUnless(sys.platform == "darwin", "Darwin-only native metadata contract")
class DarwinMetadataContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="flywheel-macos-metadata-")
        self.area = pathlib.Path(self.tmp.name).resolve()
        self.project = self.area / "project"
        (self.project / ".claude").mkdir(parents=True)
        self.target = self.project / ".claude/settings.json"
        self.target.write_bytes(b"original settings\n")
        self.stage = self.area / "stage"
        self.stage.write_bytes(b"replacement settings\n")
        self.prefix = self.area / "proposal" / "settings"
        self.prefix.parent.mkdir()
        self.state = self.area / "state.json"
        self._seed_native_metadata()
        self.attrs = self._attrs()
        self.acl = self._acl()
        self.flags = self._flags()

    def tearDown(self):
        self.tmp.cleanup()

    def _open(self, path=None, flags=os.O_RDWR):
        return os.open(path or self.target, flags | os.O_NOFOLLOW)

    def _attrs(self):
        fd = self._open()
        try:
            names = M.xattr_list(fd)
            return {name: M.xattr_get(fd, name) for name in names}
        finally:
            os.close(fd)

    def _acl(self):
        fd = self._open()
        try:
            return native_acl_bytes(fd)
        finally:
            os.close(fd)

    def _flags(self):
        fd = self._open()
        try:
            return int(os.fstat(fd).st_flags)
        finally:
            os.close(fd)

    def _seed_native_metadata(self):
        fd = self._open()
        try:
            attrs = {
                "com.flywheel.binary": b"\x00\x01\xff\x80",
                "com.flywheel.empty": b"",
                "com.apple.quarantine": b"0081;65f2;Safari;https://example.test/\x00",
                "com.apple.ResourceFork": b"resource-fork-contract",
            }
            for name, value in attrs.items():
                try:
                    M.xattr_set(fd, name, value)
                except Exception as exc:  # Darwin support is required, not optional.
                    self.fail("native xattr fixture failed for %s: %s" % (name, exc))
            try:
                fchflags(fd, UF_HIDDEN)
            except OSError as exc:
                self.fail("fchflags(UF_HIDDEN) fixture failed: %s" % exc)
        finally:
            os.close(fd)
        # chmod +a creates a real extended ACL; textual ACL is not the assertion.
        subprocess.run(["chmod", "+a", "user:root allow read", str(self.target)], check=True)

    def propose(self):
        relative = self.target.relative_to(self.project).as_posix()
        M.proposal_command(str(self.project), relative, str(self.prefix))
        return json.loads(pathlib.Path(str(self.prefix) + ".identity.json").read_text())

    def assert_attrs(self, path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            self.assertEqual({name: M.xattr_get(fd, name) for name in M.xattr_list(fd)}, self.attrs)
            self.assertEqual(native_acl_bytes(fd), self.acl)
            self.assertEqual(int(os.fstat(fd).st_flags), self.flags)
        finally:
            os.close(fd)

    def test_real_native_acl_binary_flags_and_all_xattrs_survive_apply_backup_retry(self):
        expected = self.propose()
        protected = expected["metadata"]["protected"]
        self.assertTrue(protected["acl"]["supported"] and protected["acl"]["present"])
        self.assertEqual(base64.b64decode(protected["acl"]["value"]), self.acl)
        self.assertEqual(protected["flags"]["value"], self.flags)
        self.assertEqual(set(expected["metadata"]["xattrs"]), set(self.attrs))
        self.assertEqual({k: base64.b64decode(v) for k, v in expected["metadata"]["xattrs"].items()}, self.attrs)

        M.apply_proposal_command(str(self.project), ".claude/settings.json", str(self.prefix), str(self.stage), str(self.state))
        backup = next(self.target.parent.glob(".settings.json.confirm-backup-*"))
        self.assertEqual(self.target.read_bytes(), self.stage.read_bytes())
        self.assert_attrs(self.target)
        self.assertEqual(backup.read_bytes(), b"original settings\n")
        self.assert_attrs(backup)
        self.assertEqual(json.loads(self.state.read_text())["operations"][-1]["status"], "applied")

    def test_exchange_failure_retry_keeps_native_metadata_on_backup_and_active(self):
        self.propose()
        with mock.patch.object(M, "rename_exchange", side_effect=OSError(errno.EIO, "exchange")):
            with self.assertRaises(OSError):
                M.apply_proposal_command(str(self.project), ".claude/settings.json", str(self.prefix), str(self.stage), str(self.state))
        self.assertEqual(self.target.read_bytes(), b"original settings\n")
        self.assert_attrs(self.target)
        M.apply_proposal_command(str(self.project), ".claude/settings.json", str(self.prefix), str(self.stage), str(self.state))
        self.assert_attrs(self.target)
        backups = list(self.target.parent.glob(".settings.json.confirm-backup-*"))
        self.assertTrue(backups)
        for backup in backups:
            self.assert_attrs(backup)

    def test_positive_apply_covers_agents_and_claude_paths(self):
        for relative in ("AGENTS.md", ".claude/settings.json"):
            with self.subTest(relative=relative):
                self.target = self.project / relative
                self.target.write_bytes(b"original settings\n")
                self.stage = self.area / ("stage-" + relative.replace("/", "-"))
                self.stage.write_bytes(b"replacement settings\n")
                self.prefix = self.area / ("proposal-" + relative.replace("/", "-"))
                self.state = self.area / ("state-" + relative.replace("/", "-") + ".json")
                self._seed_native_metadata()
                self.attrs = self._attrs()
                self.acl = self._acl()
                self.flags = self._flags()
                self.propose()
                M.apply_proposal_command(str(self.project), relative, str(self.prefix), str(self.stage), str(self.state))
                self.assertEqual(self.target.read_bytes(), self.stage.read_bytes())
                self.assert_attrs(self.target)

    def _run_adversary_subtest(self, kind):
        self.propose()  # every adversary gets a fresh proposal
        fd = self._open()
        try:
            before = metadata(fd)
            original = self.target.read_bytes()
            original_attrs = dict(self.attrs)
            original_acl = self.acl
            original_mtime = os.fstat(fd).st_mtime_ns
        finally:
            os.close(fd)
        peer = r"""
import os, sys, runpy
m = runpy.run_path(sys.argv[2]); fd = os.open(sys.argv[1], os.O_RDWR | os.O_NOFOLLOW)
try:
    before = os.fstat(fd)
    old = int(before.st_flags)
    if sys.argv[3] == 'acl':
        acl = m['native_acl_bytes'](fd)
        m['set_native_acl'](fd, acl)
    elif sys.argv[3] == 'flags':
        m['fchflags'](fd, old ^ 0x00008000)
        m['fchflags'](fd, old)
    elif sys.argv[3] == 'xattrs':
        m['M'].xattr_set(fd, 'com.flywheel.adversary', b'temporary')
        m['M'].xattr_remove(fd, 'com.flywheel.adversary')
    elif sys.argv[3] == 'resourcefork':
        m['M'].xattr_set(fd, 'com.apple.ResourceFork', b'adversary-resource')
        m['M'].xattr_set(fd, 'com.apple.ResourceFork', b'resource-fork-contract')
    elif sys.argv[3] == 'data':
        os.pwrite(fd, b'FORGED', 0)
        os.pwrite(fd, b'original settings\n', 0)
    os.utime(fd, ns=(before.st_atime_ns, before.st_mtime_ns))
finally:
    os.close(fd)
"""
        subprocess.run([sys.executable, "-c", peer, str(self.target), str(__file__), kind], check=True)
        fd = self._open()
        try:
            after_peer = metadata(fd)
            self.assertNotEqual(after_peer["ctime_ns"], before["ctime_ns"])
            for field in ("dev", "ino", "nlink", "size", "mode", "uid", "gid", "mtime_ns", "flags"):
                self.assertEqual(after_peer[field], before[field], field)
            self.assertEqual({name: M.xattr_get(fd, name) for name in M.xattr_list(fd)}, original_attrs)
            self.assertEqual(native_acl_bytes(fd), original_acl)
        finally:
            os.close(fd)
        self.assertEqual(self.target.read_bytes(), original)
        with self.assertRaises(M.InstallError):
            M.apply_proposal_command(str(self.project), ".claude/settings.json", str(self.prefix), str(self.stage), str(self.state))
        self.assertEqual(self.target.read_bytes(), original)

    def test_adversary_acl_is_rejected(self):
        self._run_adversary_subtest("acl")

    def test_adversary_flags_is_rejected(self):
        self._run_adversary_subtest("flags")

    def test_adversary_xattrs_is_rejected(self):
        self._run_adversary_subtest("xattrs")

    def test_adversary_resourcefork_is_rejected(self):
        self._run_adversary_subtest("resourcefork")

    def test_adversary_data_is_rejected(self):
        self._run_adversary_subtest("data")

    def test_native_metadata_read_write_and_enumeration_fail_closed(self):
        for patch, error in (("xattr_list", errno.EIO), ("xattr_get", errno.EIO), ("xattr_set", errno.EIO)):
            with self.subTest(patch=patch):
                self.propose()
                with mock.patch.object(M, patch, side_effect=OSError(error, patch)):
                    with self.assertRaises((M.InstallError, OSError, NotImplementedError)):
                        M.apply_proposal_command(str(self.project), ".claude/settings.json", str(self.prefix), str(self.stage), str(self.state))
                self.assertEqual(self.target.read_bytes(), b"original settings\n")
                self.assert_attrs(self.target)
                self.assertTrue(not self.state.exists() or all(op.get("status") != "applied" for op in json.loads(self.state.read_text()).get("operations", [])))
                preserved_stage = list(self.target.parent.glob(".settings.json.confirm-backup-*"))
                _ = preserved_stage  # preserved staging is an intentional helper safety policy

        self.propose()
        with mock.patch.object(M, "_darwin_acl", side_effect=OSError(errno.EIO, "acl read")):
            with self.assertRaises((M.InstallError, OSError)):
                M.apply_proposal_command(str(self.project), ".claude/settings.json", str(self.prefix), str(self.stage), str(self.state))
        self.assertEqual(self.target.read_bytes(), b"original settings\n")
        self.assert_attrs(self.target)

        self.propose()
        with mock.patch.object(M, "_darwin_set_acl", side_effect=OSError(errno.EIO, "acl write")):
            with self.assertRaises((M.InstallError, OSError)):
                M.apply_proposal_command(str(self.project), ".claude/settings.json", str(self.prefix), str(self.stage), str(self.state))
        self.assertEqual(self.target.read_bytes(), b"original settings\n")
        self.assert_attrs(self.target)

    def test_special_or_unknown_flags_fail_closed(self):
        fd = self._open()
        try:
            fixture_flags = int(os.fstat(fd).st_flags)
            held_fd = os.dup(fd)
            try:
                fchflags(held_fd, fixture_flags | UF_IMMUTABLE)
                self.propose()
                with self.assertRaises(M.InstallError):
                    M.apply_proposal_command(str(self.project), ".claude/settings.json", str(self.prefix), str(self.stage), str(self.state))
            finally:
                fchflags(held_fd, fixture_flags)
                os.close(held_fd)
        finally:
            os.close(fd)
        self.assert_attrs(self.target)

        self.propose()
        target_ino = os.stat(self.target, follow_symlinks=False).st_ino
        real_file_flags = M._file_flags
        def snapshot_flags(candidate_fd):
            if os.fstat(candidate_fd).st_ino != target_ino:
                return {"supported": True, "value": 0x40000000}
            return real_file_flags(candidate_fd)
        with mock.patch.object(M, "_file_flags", side_effect=snapshot_flags):
            with self.assertRaises(M.InstallError):
                M.apply_proposal_command(str(self.project), ".claude/settings.json", str(self.prefix), str(self.stage), str(self.state))
        self.assertEqual(self.target.read_bytes(), b"original settings\n")
        self.assert_attrs(self.target)


if __name__ == "__main__":
    unittest.main(verbosity=2)
