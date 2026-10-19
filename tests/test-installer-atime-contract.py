#!/usr/bin/env python3
"""Regression tests for the confirmation snapshot atime contract."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / ".agent-maintenance/installer-transaction.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("installer_atime_contract", HELPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


M = load_helper()


class AtimeContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="installer-atime-contract-")
        self.area = pathlib.Path(self.tmp.name).resolve()
        self.project = self.area / "project"
        self.project.mkdir()
        self.target = self.project / "AGENTS.md"
        self.target.write_bytes(b"stable contents\n")
        self.parent = os.open(self.project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.fd = os.open(self.target, os.O_RDONLY | os.O_NOFOLLOW)
        self.addCleanup(os.close, self.fd)
        self.addCleanup(os.close, self.parent)
        self.addCleanup(self.tmp.cleanup)

    def test_snapshot_contains_strict_ctime_and_content_identity(self):
        snapshot, data = M.confirmation_snapshot(self.fd, self.parent)
        self.assertEqual(snapshot["content"]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertIn("ctime_ns", snapshot["metadata"])
        self.assertEqual(snapshot["inode"]["nlink"], 1)

    def test_identity_match_ignores_atime_but_rejects_ctime(self):
        snapshot, _ = M.confirmation_snapshot(self.fd, self.parent)
        changed_atime = {section: dict(values) for section, values in snapshot.items()}
        changed_atime["metadata"]["atime_ns"] = snapshot["metadata"].get("atime_ns", 0) + 1
        self.assertTrue(M.confirmation_identity_matches(changed_atime, snapshot))

        changed_ctime = {section: dict(values) for section, values in snapshot.items()}
        changed_ctime["metadata"]["ctime_ns"] = snapshot["metadata"]["ctime_ns"] + 1
        self.assertFalse(M.confirmation_identity_matches(changed_ctime, snapshot))

    def test_plain_read_does_not_change_ctime(self):
        before = os.fstat(self.fd)
        os.lseek(self.fd, 0, os.SEEK_SET)
        self.assertEqual(os.read(self.fd, 4096), b"stable contents\n")
        after = os.fstat(self.fd)
        self.assertEqual(before.st_ctime_ns, after.st_ctime_ns)


if __name__ == "__main__":
    unittest.main(verbosity=2)
