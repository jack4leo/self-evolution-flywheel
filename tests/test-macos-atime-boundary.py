#!/usr/bin/env python3
"""Darwin-only probes for the atime restoration / ctime boundary (PR30 draft).

This is deliberately an observation probe, not an end-to-end claim: APFS and
other filesystems may differ.  Every source read is through a pinned fd.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import importlib.util
import json
import mmap
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / ".agent-maintenance/installer-transaction.py"

def load_helper():
    spec = importlib.util.spec_from_file_location("atime_probe_helper", HELPER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod

M = load_helper()
LIBC = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
F_NOCACHE, COPYFILE_DATA, CLONE_ACL = 48, 8, 4
UF_HIDDEN = 0x00008000


def _native(name, restype=ctypes.c_int, argtypes=()):
    fn = getattr(LIBC, name)
    fn.restype, fn.argtypes = restype, list(argtypes)
    return fn


def _call(fn, *args):
    ctypes.set_errno(0)
    result = fn(*args)
    if result == -1:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return result


def fields(fd):
    s = os.fstat(fd)
    out = {"dev": s.st_dev, "ino": s.st_ino, "nlink": s.st_nlink,
           "size": s.st_size,
           "mode": stat.S_IMODE(s.st_mode), "uid": s.st_uid, "gid": s.st_gid,
           "atime_ns": s.st_atime_ns, "mtime_ns": s.st_mtime_ns,
           "ctime_ns": s.st_ctime_ns}
    for key in ("st_flags", "st_birthtime", "st_birthtime_ns"):
        if hasattr(s, key):
            out[key.removeprefix("st_")] = getattr(s, key)
    return out


def evidence(label, before, after, **extra):
    item = {"label": label, "before": before, "after": after,
            "differences": {k: [before.get(k), after.get(k)] for k in before
                             if before.get(k) != after.get(k)}}
    item.update(extra)
    print(json.dumps(item, sort_keys=True), flush=True)
    return item


@unittest.skipUnless(sys.platform == "darwin", "Darwin-only: all probes skipped on non-Darwin")
class AtimeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atime-boundary-")
        self.area = pathlib.Path(self.tmp.name).resolve()
        self.source = (self.area / "source").resolve()
        self.source.write_bytes(b"original payload\n" * 64)
        self.fd = os.open(self.source, os.O_RDWR | os.O_NOFOLLOW)
        self.addCleanup(self._close)

    def _close(self):
        if getattr(self, "fd", None) is not None:
            os.close(self.fd)
            self.fd = None
        self.tmp.cleanup()

    def fresh(self):
        os.close(self.fd)
        self.source.unlink()
        self.source.write_bytes(b"original payload\n" * 64)
        self.fd = os.open(self.source, os.O_RDWR | os.O_NOFOLLOW)
        old = 946684800_000_000_000
        os.utime(self.fd, ns=(old, old + 1234))
        return old

    def test_real_read_restore_atime_changes_ctime_and_helper_fails_closed(self):
        self.fresh()
        before = fields(self.fd)
        os.read(self.fd, 32)
        os.utime(self.fd, ns=(before["atime_ns"], before["mtime_ns"]))
        after = fields(self.fd)
        evidence("read+utime-restore", before, after)
        self.assertNotEqual(before["ctime_ns"], after["ctime_ns"])
        parent = os.open(self.area, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with self.assertRaises(M.InstallError):
                M.confirmation_snapshot(self.fd, parent)
        finally:
            os.close(parent)

    def _observe_read(self, label, reader):
        self.fresh()
        before = fields(self.fd)
        got = reader(self.fd)
        after = fields(self.fd)
        evidence(label, before, after, content_sha256=hashlib.sha256(got).hexdigest())
        self.assertEqual(got, b"original payload\n" * 64)

    def test_fd_read(self):
        self._observe_read("fd-read", lambda fd: os.read(fd, 4096))

    def test_fd_pread(self):
        self._observe_read("fd-pread", lambda fd: os.pread(fd, 4096, 0))

    def test_mmap_private_read(self):
        def read(fd):
            mm = mmap.mmap(fd, 0, access=mmap.ACCESS_COPY)
            try:
                return mm[:]
            finally:
                mm.close()
        self._observe_read("mmap-map-private-read", read)

    def test_fnocache_read(self):
        def read(fd):
            _call(_native("fcntl", ctypes.c_int, (ctypes.c_int, ctypes.c_int, ctypes.c_int)), fd, F_NOCACHE, 1)
            return os.pread(fd, 4096, 0)
        self._observe_read("f-nocache-read", read)

    def test_fcopyfile_data_fd_to_fd(self):
        self.fresh()
        dst = self.area / "copy"
        dfd = os.open(dst, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            before = fields(self.fd)
            copy = _native("fcopyfile", ctypes.c_int, (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32))
            _call(copy, self.fd, dfd, None, COPYFILE_DATA)
            os.lseek(dfd, 0, os.SEEK_SET)
            self.assertEqual(os.read(dfd, 4096), b"original payload\n" * 64)
            evidence("fcopyfile-data", before, fields(self.fd), content_correct=True)
        finally:
            os.close(dfd)

    def test_native_xattrs_including_resource_fork(self):
        self.fresh()
        attrs = {"com.flywheel.binary": b"\x00\x01\xff", "com.flywheel.empty": b"",
                 "com.apple.quarantine": b"0081;65f2;Safari;https://example.test/\x00",
                 "com.apple.ResourceFork": b"resource-fork-fixture"}
        for name, value in attrs.items():
            try:
                M.xattr_set(self.fd, name, value)
            except (OSError, NotImplementedError) as exc:
                self.fail("Darwin native xattr/resource-fork setup failed: %s" % exc)
        before = fields(self.fd)
        got = {name: M.xattr_get(self.fd, name) for name in M.xattr_list(self.fd)}
        after = fields(self.fd)
        evidence("native-fd-xattrs", before, after,
                 xattrs={k: got[k].hex() for k in got}, source_time_changed=before != after)
        for name, value in attrs.items():
            self.assertEqual(got.get(name), value)

    def test_fclonefileat_fd_pinned_cow_and_attributes(self):
        self.fresh()
        clone = self.area / "clone"
        wanted = {"com.flywheel.binary": b"\x00\xff", "com.flywheel.empty": b"",
                  "com.apple.quarantine": b"0081;65f2;Safari;https://example.test/",
                  "com.apple.ResourceFork": b"clone-resource-fork"}
        for name, value in wanted.items():
            M.xattr_set(self.fd, name, value)
        src_before = fields(self.fd)
        clone_dir = os.open(self.area, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fn = _native("fclonefileat", ctypes.c_int,
                         (ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32))
            _call(fn, self.fd, clone_dir, b"clone", CLONE_ACL)
            cfd = os.open(clone, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                self.assertEqual(os.read(cfd, 4096), b"original payload\n" * 64)
                clone_attrs = fields(cfd)
                source_xattrs = {name: M.xattr_get(self.fd, name) for name in M.xattr_list(self.fd)}
                clone_xattrs = {name: M.xattr_get(cfd, name) for name in M.xattr_list(cfd)}
                self.assertEqual(source_xattrs, wanted)
                self.assertEqual(clone_xattrs, wanted)
                evidence("fclonefileat", src_before, fields(self.fd), clone=clone_attrs,
                         native_attrs_equal=(source_xattrs == clone_xattrs))
            finally:
                os.close(cfd)
            os.pwrite(self.fd, b"X" * 8, 0)
            cfd = os.open(clone, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                self.assertEqual(os.read(cfd, 8), b"original")
            finally:
                os.close(cfd)
        except OSError as exc:
            if exc.errno in (getattr(os, "EXDEV", 18), 45, 95):
                self.fail("fclonefileat unsupported on this runner/filesystem: %s" % exc)
            raise
        finally:
            os.close(clone_dir)

    def test_clone_return_to_open_name_substitution_is_not_authenticated(self):
        self.fresh()
        source_before = fields(self.fd)
        clone_dir = os.open(self.area, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fn = _native("fclonefileat", ctypes.c_int,
                         (ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32))
            _call(fn, self.fd, clone_dir, b"race", CLONE_ACL)
            os.unlink(self.area / "race")
            forged = b"X" * source_before["size"]
            (self.area / "race").write_bytes(forged)
            os.chmod(self.area / "race", source_before["mode"])
            os.utime(self.area / "race", ns=(source_before["atime_ns"], source_before["mtime_ns"]))
            fake = os.open(self.area / "race", os.O_RDONLY | os.O_NOFOLLOW)
            try:
                fake_info = fields(fake)
                for key in ("dev", "nlink", "size", "mode", "uid", "gid", "atime_ns", "mtime_ns"):
                    self.assertEqual(fake_info[key], source_before[key])
                self.assertEqual(os.read(fake, 4096), forged)
            finally:
                os.close(fake)
            source_after = fields(self.fd)
            evidence("clone-return-to-open-substitution", source_before, source_after,
                     fake=fake_info, opened_data="same-length forged payload",
                     warning="fstat-after-open cannot authenticate clone inode")
            self.assertEqual(source_before, source_after)
        finally:
            os.close(clone_dir)

    def test_flags_xattrs_resourcefork_and_writer_restore_leave_ctime_witness(self):
        self.fresh()
        original = fields(self.fd)
        original_data = os.pread(self.fd, 4096, 0)
        flag = _native("fchflags", ctypes.c_int, (ctypes.c_int, ctypes.c_uint32))
        _call(flag, self.fd, UF_HIDDEN)
        _call(flag, self.fd, 0)
        M.xattr_set(self.fd, "com.apple.ResourceFork", b"temporary-resource")
        M.xattr_remove(self.fd, "com.apple.ResourceFork")
        os.pwrite(self.fd, b"Z" * 8, 0)
        os.pwrite(self.fd, original_data[:8], 0)
        os.utime(self.fd, ns=(original["atime_ns"], original["mtime_ns"]))
        after = fields(self.fd)
        evidence("flags-xattr-resourcefork-writer-restore", original, after,
                 content_restored=(os.pread(self.fd, 4096, 0) == original_data),
                 interpretation="ctime is the remaining mutation witness; utime cannot erase it")
        self.assertEqual(os.pread(self.fd, 4096, 0), original_data)
        self.assertNotEqual(original["ctime_ns"], after["ctime_ns"])

    def test_acl_native_fd_probe_changes_ctime(self):
        self.fresh()
        acl_get = getattr(LIBC, "acl_get_fd_np", None)
        acl_text = getattr(LIBC, "acl_to_text", None)
        acl_free = getattr(LIBC, "acl_free", None)
        if not all((acl_get, acl_text, acl_free)):
            self.fail("Darwin native ACL APIs unavailable")
        acl_get.restype, acl_get.argtypes = ctypes.c_void_p, [ctypes.c_int, ctypes.c_int]
        acl_text.restype, acl_text.argtypes = ctypes.c_void_p, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
        acl_free.restype, acl_free.argtypes = ctypes.c_int, [ctypes.c_void_p]
        before = fields(self.fd)
        subprocess.run(["chmod", "+a", "user:root allow read", str(self.source)], check=True)
        acl = acl_get(self.fd, 0x100)
        if not acl:
            self.fail("native acl_get_fd_np did not read fixture ACL")
        try:
            n = ctypes.c_ssize_t()
            text = acl_text(acl, ctypes.byref(n))
            self.assertTrue(text and n.value)
            acl_free(text)
        finally:
            acl_free(acl)
        after = fields(self.fd)
        evidence("native-acl-fd", before, after, acl_text_bytes=n.value,
                 mode_changed=(before["mode"] != after["mode"]))
        self.assertNotEqual(before["ctime_ns"], after["ctime_ns"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
