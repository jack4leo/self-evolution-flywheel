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
        self.assertNotEqual(before["atime_ns"], after["atime_ns"],
                            "no-atime counterexample not reproduced on this filesystem")

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
            self.assertNotEqual(before["atime_ns"], fields(self.fd)["atime_ns"])
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
            subprocess.run(
                [sys.executable, "-c", "import os,sys; os.pwrite(int(sys.argv[1]), b'X'*8, 0)", str(self.fd)],
                pass_fds=(self.fd,), check=True,
            )
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
            forged = b"X" * source_before["size"]
            # An actual same-UID peer runs in the clone-return/open interval.
            # It opens the 0700 directory itself: no source/clone fd is gifted.
            peer = """import json,os,sys
directory = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
info = json.loads(sys.argv[2])
os.unlink('race', dir_fd=directory)
fd = os.open('race', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
os.write(fd, b'X' * info['size'])
os.fchmod(fd, info['mode'])
os.utime(fd, ns=(info['atime_ns'], info['mtime_ns']))
os.close(fd)
os.close(directory)
print(json.dumps({'uid': os.getuid(), 'pid': os.getpid()}))
"""
            actor = json.loads(subprocess.check_output(
                [sys.executable, "-c", peer, str(self.area), json.dumps(source_before)], text=True,
            ))
            self.assertEqual(actor["uid"], os.getuid())
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
                     actor=actor, fake=fake_info, opened_data="same-length forged payload",
                     warning="fstat-after-open cannot authenticate clone inode")
            self.assertEqual(source_before, source_after)
        finally:
            os.close(clone_dir)

    def test_peer_metadata_restore_keeps_ctime_witness(self):
        peer = """import ctypes,json,os,runpy,subprocess,sys
fd,kind,path,helper = int(sys.argv[1]),sys.argv[2],sys.argv[3],sys.argv[4]
m = runpy.run_path(helper)
lib = ctypes.CDLL(None, use_errno=True)
def check(result):
    if result == -1: raise OSError(ctypes.get_errno(), 'native fixture mutation failed')
if kind == 'flags':
    flags = os.fstat(fd).st_flags
    lib.fchflags.argtypes = [ctypes.c_int,ctypes.c_uint32]
    check(lib.fchflags(fd, flags | 0x8000))
    check(lib.fchflags(fd, flags))
elif kind in ('xattr','resourcefork'):
    name = 'com.flywheel.history' if kind == 'xattr' else 'com.apple.ResourceFork'
    m['xattr_set'](fd,name,b'temporary value')
    m['xattr_remove'](fd,name)
elif kind == 'data':
    os.pwrite(fd,b'Z'*8,0)
    os.pwrite(fd,b'original',0)
elif kind == 'acl':
    lib.acl_get_fd_np.argtypes = [ctypes.c_int,ctypes.c_int]
    lib.acl_get_fd_np.restype = ctypes.c_void_p
    lib.acl_set_fd_np.argtypes = [ctypes.c_int,ctypes.c_void_p,ctypes.c_int]
    lib.acl_free.argtypes = [ctypes.c_void_p]
    original = lib.acl_get_fd_np(fd,0x100)
    if not original: raise OSError(ctypes.get_errno(), 'fixture ACL unavailable')
    try:
        subprocess.run(['chmod','+a','user:root allow read',path],check=True)
        check(lib.acl_set_fd_np(fd,original,0x100))
    finally: lib.acl_free(original)
else: raise AssertionError(kind)
print(json.dumps({'uid':os.getuid(),'pid':os.getpid()}))
"""
        for kind in ("flags", "xattr", "resourcefork", "data", "acl"):
            with self.subTest(kind=kind):
                self.fresh()
                original = fields(self.fd)
                attrs = M.xattrs(self.fd)
                actor = json.loads(subprocess.check_output(
                    [sys.executable, "-c", peer, str(self.fd), kind, str(self.source), str(HELPER)],
                    pass_fds=(self.fd,), text=True,
                ))
                self.assertEqual(actor["uid"], os.getuid())
                after_peer = fields(self.fd)
                self.assertNotEqual(original["ctime_ns"], after_peer["ctime_ns"])
                os.utime(self.fd, ns=(original["atime_ns"], original["mtime_ns"]))
                after_restore = fields(self.fd)
                evidence("peer-" + kind + "-restore", original, after_restore,
                         actor=actor, after_peer=after_peer,
                         warning="own utime does not provide provenance for the final ctime")
                self.assertNotEqual(original["ctime_ns"], after_restore["ctime_ns"])
                self.assertEqual({k: v for k, v in original.items() if k != "ctime_ns"},
                                 {k: v for k, v in after_restore.items() if k != "ctime_ns"})
                self.assertEqual(M.xattrs(self.fd), attrs)
                self.assertEqual(os.pread(self.fd, 4096, 0), b"original payload\n" * 64)

    def test_acl_write_changes_ctime_without_mode_change(self):
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
        after_write = fields(self.fd)
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
        evidence("acl-write-then-native-fd-read", before, after_write, acl_text_bytes=n.value,
                 mode_changed=(before["mode"] != after["mode"]))
        evidence("native-acl-read-only", after_write, after)
        self.assertNotEqual(before["ctime_ns"], after_write["ctime_ns"])
        self.assertEqual(before["mode"], after_write["mode"])
        self.assertEqual(after_write, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
