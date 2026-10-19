#!/usr/bin/env python3
"""Regression tests for descriptor xattrs and confirmed transactions."""
from __future__ import annotations

import base64
import ctypes
import errno
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / ".agent-maintenance/installer-transaction.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("installer_transaction_xattrs", HELPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


M = load_helper()


def list_xattrs(fd):
    return M.xattr_list(fd)


def get_xattr(fd, name):
    return M.xattr_get(fd, name)


def set_xattr(fd, name, value):
    return M.xattr_set(fd, name, value)


def remove_xattr(fd, name):
    return M.xattr_remove(fd, name)


def names_as_text(names):
    return {n.decode() if isinstance(n, bytes) else n for n in names}


def native_attrs():
    if sys.platform == "darwin":
        return {
            "com.apple.quarantine": b"0081;65f2;Safari;https://example.test/\x00",
            "com.flywheel.binary": b"\x00\x01\xff\x80",
            "com.flywheel.empty": b"",
        }
    return {
        "user.quarantine": b"0081;65f2;Safari;https://example.test/\x00",
        "user.binary": b"\x00\x01\xff\x80",
        "user.empty": b"",
    }


class SeededXattrsMixin:
    def _seed_fd(self, path, attrs):
        fd = os.open(path, os.O_RDWR)
        try:
            for name, value in attrs.items():
                set_xattr(fd, name, value)
        except OSError as exc:
            os.close(fd)
            if sys.platform == "darwin":
                self.fail("Darwin must support the required test xattrs: %s" % exc)
            self.skipTest("filesystem does not support test xattrs: %s" % exc)
        except (AttributeError, NotImplementedError) as exc:
            os.close(fd)
            if sys.platform == "darwin":
                self.fail("Darwin xattr fallback is unavailable: %s" % exc)
            self.skipTest("filesystem does not support test xattrs: %s" % exc)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        return attrs

    def _hide_python_xattr_apis(self):
        if sys.platform == "darwin":
            for name in ("listxattr", "getxattr", "setxattr", "removexattr"):
                patcher = mock.patch.object(M.os, name, None, create=True)
                patcher.start()
                self.addCleanup(patcher.stop)

    def _attrs(self, path):
        fd = os.open(path, os.O_RDONLY)
        try:
            return {name: get_xattr(fd, name) for name in list_xattrs(fd)}
        finally:
            os.close(fd)


class XattrFallbackTests(SeededXattrsMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="installer-xattrs-")
        self.target = pathlib.Path(self.tmp.name) / "target"
        self.target.write_bytes(b"original\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_fd_wrappers_round_trip_binary_and_empty_values(self):
        attrs = self._seed_fd(self.target, native_attrs())
        self._hide_python_xattr_apis()
        fd = os.open(self.target, os.O_RDWR)
        try:
            self.assertEqual(self._attrs(self.target), attrs)
            replacement = b"replacement\x00\xfe"
            set_xattr(fd, next(iter(attrs)), replacement)
            self.assertEqual(get_xattr(fd, next(iter(attrs))), replacement)
            remove_xattr(fd, "com.flywheel.empty" if "com.flywheel.empty" in attrs else "user.empty")
        finally:
            os.close(fd)


class ConfirmedXattrTransactionTests(SeededXattrsMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="confirmed-xattrs-")
        self.area = pathlib.Path(self.tmp.name)
        self.project = self.area / "project"
        (self.project / ".claude").mkdir(parents=True)
        self.target = self.project / ".claude/settings.json"
        self.target.write_bytes(b'{"old":true}\n')
        self.stage = self.area / "stage"
        self.stage.write_bytes(b'{"new":true}\n')
        self.prefix = self.area / "review" / "settings"
        self.prefix.parent.mkdir()
        self.state = self.area / "state.json"
        self.attrs = self._seed_fd(self.target, native_attrs())
        # Seed while Python's Darwin os API is present, then force every
        # transaction path through the native descriptor implementation.
        self._hide_python_xattr_apis()

    def tearDown(self):
        self.tmp.cleanup()

    def propose(self):
        M.proposal_command(str(self.project), ".claude/settings.json", str(self.prefix))
        return json.loads((self.prefix.parent / "settings.identity.json").read_text())

    def _assert_attrs(self, path):
        self.assertEqual(self._attrs(path), self.attrs)

    def test_snapshot_and_successful_write_preserve_all_xattrs_on_active_and_backup(self):
        expected = self.propose()
        self.assertTrue(expected["metadata"]["xattrs_supported"])
        self.assertEqual(
            {k: base64.b64decode(v) for k, v in expected["metadata"]["xattrs"].items()},
            self.attrs,
        )
        M.apply_proposal_command(
            str(self.project), ".claude/settings.json", str(self.prefix), str(self.stage), str(self.state)
        )
        backup = next(self.target.parent.glob(".settings.json.confirm-backup-*"))
        self.assertEqual(backup.read_bytes(), b'{"old":true}\n')
        self._assert_attrs(backup)
        self.assertEqual(self.target.read_bytes(), self.stage.read_bytes())
        self._assert_attrs(self.target)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["operations"][-1]["status"], "applied")
        self.assertTrue(state["operations"][-1]["confirmation_backup"])

    def test_identity_change_fails_closed_without_retrying_write(self):
        self.propose()
        fd = os.open(self.target, os.O_RDWR)
        try:
            set_xattr(fd, next(iter(self.attrs)), b"changed")
        finally:
            os.close(fd)
        with self.assertRaises(M.InstallError):
            M.apply_proposal_command(str(self.project), ".claude/settings.json", str(self.prefix), str(self.stage), str(self.state))
        self.assertEqual(self.target.read_bytes(), b'{"old":true}\n')
        self.assertEqual(self._attrs(self.target)[next(iter(self.attrs))], b"changed")

    def test_read_failure_is_not_published(self):
        with mock.patch.object(M, "xattr_get", side_effect=OSError(errno.EIO, "read")):
            with self.assertRaises(OSError):
                self.propose()
        self.assertEqual(self.target.read_bytes(), b'{"old":true}\n')

    def test_list_failure_during_apply_is_not_published(self):
        self.propose()
        with mock.patch.object(M, "xattr_list", side_effect=OSError(errno.EIO, "list")):
            with self.assertRaises((M.InstallError, OSError, NotImplementedError)):
                M.apply_proposal_command(str(self.project), ".claude/settings.json", str(self.prefix), str(self.stage), str(self.state))
        self.assertEqual(self.target.read_bytes(), b'{"old":true}\n')

    def test_set_failure_during_apply_is_not_published(self):
        self.propose()
        with mock.patch.object(M, "xattr_set", side_effect=OSError(errno.EIO, "set")):
            with self.assertRaises(M.InstallError):
                M.apply_proposal_command(str(self.project), ".claude/settings.json", str(self.prefix), str(self.stage), str(self.state))
        self.assertEqual(self.target.read_bytes(), b'{"old":true}\n')

    def test_remove_failure_is_not_silent(self):
        fd = os.open(self.target, os.O_RDWR)
        try:
            info = os.fstat(fd)
            values = {"uid": info.st_uid, "gid": info.st_gid, "mode": info.st_mode & 0o7777,
                      "atime_ns": info.st_atime_ns, "mtime_ns": info.st_mtime_ns, "xattrs": {}}
            with mock.patch.object(M, "xattr_remove", side_effect=OSError(errno.EIO, "remove")):
                with self.assertRaises(M.InstallError):
                    M.apply_metadata(fd, values)
        finally:
            os.close(fd)
        self._assert_attrs(self.target)


    def test_exchange_failure_retry_and_rollback_preserve_metadata(self):
        self.propose()
        with mock.patch.object(M, "rename_exchange", side_effect=OSError(errno.EIO, "exchange")):
            with self.assertRaises(OSError):
                M.apply_proposal_command(str(self.project), ".claude/settings.json", str(self.prefix), str(self.stage), str(self.state))
        self.assertEqual(json.loads(self.state.read_text())["operations"][-1]["status"], "pending")
        self.assertEqual(self.target.read_bytes(), b'{"old":true}\n')
        self._assert_attrs(self.target)
        M.apply_proposal_command(str(self.project), ".claude/settings.json", str(self.prefix), str(self.stage), str(self.state))
        self.assertEqual(json.loads(self.state.read_text())["operations"][-1]["status"], "applied")
        self.assertEqual(M.rollback(str(self.project), str(self.state)), 1)
        self.assertEqual(self.target.read_bytes(), self.stage.read_bytes())
        self._assert_attrs(self.target)
        for recovery in self.target.parent.glob(".settings.json.confirm-backup-*"):
            self._assert_attrs(recovery)

    def test_same_length_value_change_during_snapshot_is_refused(self):
        real_get = M.xattr_get
        count = 0
        first = next(iter(self.attrs))
        def changing(fd, name):
            nonlocal count
            if name == first:
                count += 1
                if count == 2:
                    return b"x" * len(self.attrs[first])
            return real_get(fd, name)
        with mock.patch.object(M, "xattr_get", changing):
            with self.assertRaises(M.InstallError):
                self.propose()
        self.assertEqual(self.target.read_bytes(), b'{"old":true}\n')
        self._assert_attrs(self.target)

    def test_incomplete_second_enumeration_is_refused(self):
        real_list = M.xattr_list
        count = 0
        def incomplete(fd):
            nonlocal count
            count += 1
            names = real_list(fd)
            return names[:-1] if count == 2 else names
        with mock.patch.object(M, "xattr_list", incomplete):
            with self.assertRaises(M.InstallError):
                self.propose()
        self.assertEqual(self.target.read_bytes(), b'{"old":true}\n')
        self._assert_attrs(self.target)


class DarwinAbiSimulationTests(unittest.TestCase):
    """Exercise the strict fd ABI on Linux without pretending os has Darwin APIs."""

    def setUp(self):
        self.api = {}
        platform = mock.patch.object(M.sys, "platform", "darwin")
        platform.start()
        self.addCleanup(platform.stop)
        self.patchers = [
            mock.patch.object(M.os, name, None, create=True)
            for name in ("listxattr", "getxattr", "setxattr", "removexattr")
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.native = mock.patch.object(M, "_darwin_xattr_functions", return_value=self.api)
        self.native.start()
        self.addCleanup(self.native.stop)

    def test_list_uses_four_argument_enum_abi_and_rejects_malformed_lists(self):
        calls = []
        def enum(fd, buffer, size, options):
            calls.append((fd, buffer, size, options))
            if buffer is None:
                return 4
            ctypes.memmove(buffer, b"a\x00\x00\x00", 4)
            return 4
        self.api["list"] = enum
        with self.assertRaises(OSError):
            list_xattrs(7)
        self.assertEqual(calls[0][2:], (0, M._DARWIN_XATTR_SHOWCOMPRESSION))

    def test_list_rejects_duplicate_empty_names_and_truncation(self):
        def duplicate(fd, buffer, size, options):
            if buffer is None: return 4
            ctypes.memmove(buffer, b"a\x00a\x00", 4); return 4
        self.api["list"] = duplicate
        with self.assertRaises(OSError):
            list_xattrs(7)
        def empty(fd, buffer, size, options):
            if buffer is None: return 3
            ctypes.memmove(buffer, b"a\x00\x00", 3); return 3
        self.api["list"] = empty
        with self.assertRaises(OSError):
            list_xattrs(7)
        def truncated(fd, buffer, size, options):
            if buffer is None: return 3
            ctypes.memmove(buffer, b"abx", 3); return 3
        self.api["list"] = truncated
        with self.assertRaises(OSError):
            list_xattrs(7)

    def test_erange_is_an_immediate_error(self):
        def erange(*args):
            ctypes.set_errno(errno.ERANGE)
            return -1
        self.api["list"] = erange
        with self.assertRaises(OSError) as caught:
            list_xattrs(7)
        self.assertEqual(caught.exception.errno, errno.ERANGE)

    def test_get_uses_six_argument_abi_and_rejects_incomplete_growth(self):
        calls = []
        def get(fd, name, buffer, size, position, options):
            calls.append((fd, name, buffer, size, position, options))
            if buffer is None:
                return 0 if len(calls) == 1 else 2
            ctypes.memmove(buffer, b"x", 1)
            return 1
        self.api["get"] = get
        with self.assertRaises(OSError):
            get_xattr(7, "a")
        self.assertEqual(calls[0][3:], (0, 0, M._DARWIN_XATTR_SHOWCOMPRESSION))


    def test_compression_metadata_is_explicitly_refused(self):
        with tempfile.TemporaryFile() as handle:
            with mock.patch.object(M, "xattr_list", return_value=["com.apple.decmpfs"]):
                with self.assertRaisesRegex(M.InstallError, "compression"):
                    M.xattrs(handle.fileno())

    def test_unavailable_native_backend_is_not_an_empty_snapshot(self):
        with mock.patch.object(M, "_darwin_xattr_functions", return_value=None):
            with self.assertRaises(OSError):
                M.xattrs(7)

    def test_set_and_remove_errors_keep_native_errno(self):
        def set_error(fd, name, buffer, size, position, options):
            self.assertEqual((fd, name, size, position, options), (7, b"a", 3, 0, 0))
            ctypes.set_errno(errno.EACCES)
            return -1
        def remove_error(fd, name, options):
            self.assertEqual((fd, name, options), (7, b"a", 0))
            ctypes.set_errno(errno.EIO)
            return -1
        self.api.update(set=set_error, remove=remove_error)
        with self.assertRaises(OSError) as caught:
            set_xattr(7, "a", b"abc")
        self.assertEqual(caught.exception.errno, errno.EACCES)
        with self.assertRaises(OSError) as caught:
            remove_xattr(7, "a")
        self.assertEqual(caught.exception.errno, errno.EIO)


if __name__ == "__main__":
    unittest.main(verbosity=2)
