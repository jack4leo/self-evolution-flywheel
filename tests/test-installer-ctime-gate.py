#!/usr/bin/env python3
"""Strict ctime gate for the confirmation exchange boundary.

The adversary runs synchronously as a separate same-UID process after every
pre-exchange check has completed.  It restores the original bytes and mtime,
but cannot restore ctime.  A helper that authorizes by bytes/mtime alone will
therefore incorrectly report APPLIED; that is intentionally a failing gate
until the helper rejects this case.
"""
from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True

ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / ".agent-maintenance/installer-transaction.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("installer_ctime_gate", HELPER)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load helper module {HELPER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = load_helper()


_ATTACKER = r'''
import base64
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
original = base64.b64decode(sys.argv[2])
mtime_ns = int(sys.argv[3])
malicious = bytes((byte ^ 0xA5) for byte in original)
assert len(malicious) == len(original) and malicious != original
assert os.getuid() == int(sys.argv[4])
fd = os.open(path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
try:
    os.write(fd, malicious)
    os.fsync(fd)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, original)
    os.ftruncate(fd, len(original))
    os.fsync(fd)
finally:
    os.close(fd)
# Restore mtime only.  The writes and this metadata operation leave ctime changed.
os.utime(path, ns=(mtime_ns, mtime_ns), follow_symlinks=False)
'''


class InstallerCtimeGateTests(unittest.TestCase):
    def test_same_uid_restore_bytes_and_mtime_cannot_bypass_ctime_gate(self):
        with tempfile.TemporaryDirectory(prefix="installer-ctime-gate-") as tmp_name:
            area = pathlib.Path(tmp_name).resolve()
            project = (area / "project").resolve()
            project.mkdir()
            target = (project / "AGENTS.md").resolve()
            original_data = b"original confirmation payload\n"
            target.write_bytes(original_data)
            staged = (area / "staged").resolve()
            staged.write_bytes(b"authorized replacement payload\n")
            prefix = (area / "proposal").resolve()
            state_path = (area / "state.json").resolve()
            M.save_state(str(state_path), {"operations": [], "created_directories": []})

            with contextlib.redirect_stdout(io.StringIO()):
                M.proposal_command(str(project), "AGENTS.md", str(prefix))
            with open(f"{prefix}.identity.json", encoding="utf-8") as handle:
                baseline = json.load(handle)
            self.assertEqual(base64.b64decode(base64.b64encode(original_data)), original_data)
            self.assertEqual(baseline["content"]["sha256"], M.hashlib.sha256(original_data).hexdigest())

            real_rename_exchange = M.rename_exchange
            callback_seen = False
            same_uid = False
            after_adversary = None
            protected_diff = None
            applied = False

            def attack_then_real_exchange(parent_fd, left, right):
                nonlocal callback_seen, same_uid, after_adversary, protected_diff
                callback_seen = True
                child = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        _ATTACKER,
                        str(target),
                        base64.b64encode(original_data).decode("ascii"),
                        str(baseline["metadata"]["mtime_ns"]),
                        str(os.getuid()),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(child.stdout, "")
                same_uid = True

                fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    after_adversary, _ = M.confirmation_snapshot(fd, parent_fd)
                finally:
                    os.close(fd)
                # atime is explicitly allowed; every other original metadata
                # field must still match except the ctime changed by the attack.
                metadata = baseline["metadata"]
                actual_metadata = after_adversary["metadata"]
                compared_fields = set(metadata) - {"atime_ns"}
                protected_diff = {
                    field for field in compared_fields
                    if actual_metadata.get(field) != metadata.get(field)
                }
                self.assertEqual(protected_diff, {"ctime_ns"})
                self.assertEqual(after_adversary["content"], baseline["content"])
                self.assertEqual(after_adversary["metadata"]["mtime_ns"], metadata["mtime_ns"])
                self.assertNotEqual(after_adversary["metadata"]["ctime_ns"], metadata["ctime_ns"])
                real_rename_exchange(parent_fd, left, right)

            try:
                with mock.patch.object(M, "rename_exchange", attack_then_real_exchange), contextlib.redirect_stdout(io.StringIO()):
                    try:
                        M.apply_proposal_command(
                            str(project), "AGENTS.md", str(prefix), str(staged), str(state_path)
                        )
                    except M.InstallError:
                        pass
                    else:
                        applied = True
            finally:
                # Keep the diagnostic JSON even when an assertion below makes
                # this security gate red on the currently vulnerable helper.
                print(json.dumps({
                    "baseline": baseline["metadata"],
                    "afteradversary": None if after_adversary is None else after_adversary["metadata"],
                    "protected_fields_diff": sorted(protected_diff),
                    "sameuid": same_uid,
                    "applied": applied,
                }, sort_keys=True))

            self.assertTrue(callback_seen, "adversary did not run at the real rename boundary")
            self.assertTrue(same_uid, "adversary was not confirmed same-UID")
            self.assertEqual(protected_diff, {"ctime_ns"})
            self.assertIsNotNone(after_adversary)
            self.assertNotEqual(
                after_adversary["metadata"]["ctime_ns"], baseline["metadata"]["ctime_ns"]
            )
            self.assertFalse(
                applied,
                "strict ctime gate accepted a same-UID write/restore that changed ctime",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
