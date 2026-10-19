#!/usr/bin/env python3
"""Blocking checks for the documented exclusive-writer installation model.

The unsupported final-check/exchange adversary remains in
 test-installer-ctime-gate.py with its original rejection assertion.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "supported_ctime", ROOT / ".agent-maintenance/installer-transaction.py"
)
M = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(M)


class SupportedCtimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="supported-ctime-")
        self.addCleanup(self.tmp.cleanup)
        self.area = Path(self.tmp.name).resolve()
        self.project = self.area / "project"
        self.project.mkdir()
        self.target = self.project / "AGENTS.md"
        self.original = b"original authorized configuration\n"
        self.replacement = b"replacement authorized configuration\n"
        self.target.write_bytes(self.original)
        self.staged = self.area / "staged"
        self.staged.write_bytes(self.replacement)
        self.prefix = self.area / "proposal"
        self.state = self.area / "state.json"
        M.save_state(str(self.state), {"operations": [], "created_directories": []})
        with contextlib.redirect_stdout(io.StringIO()):
            M.proposal_command(str(self.project), "AGENTS.md", str(self.prefix))
        self.baseline = json.loads(Path(f"{self.prefix}.identity.json").read_text())

    def apply(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            M.apply_proposal_command(
                str(self.project), "AGENTS.md", str(self.prefix),
                str(self.staged), str(self.state),
            )
        return output.getvalue()

    def test_single_writer_exchange_applies_and_preserves_original(self):
        with mock.patch.object(M, "rename_exchange", wraps=M.rename_exchange) as exchange:
            output = self.apply()
        self.assertEqual(exchange.call_count, 1)
        self.assertIn("APPLIED", output)
        self.assertEqual(self.target.read_bytes(), self.replacement)
        operation = M.load_state(str(self.state))["operations"][-1]
        self.assertEqual(operation["status"], "applied")
        backup = self.project / operation["confirmation_backup"]
        self.assertEqual(backup.read_bytes(), self.original)
        parent = os.open(self.project, M.DIR_FLAGS)
        try:
            for path, expected in ((self.target, operation["expected"]),
                                   (backup, self.baseline)):
                fd = M.open_existing(parent, path.name)
                try:
                    actual, _ = M.confirmation_snapshot(fd, parent)
                finally:
                    os.close(fd)
                self.assertTrue(M.confirmation_identity_matches(
                    actual, expected, allow_exchange_ctime=(path == backup)
                ))
                self.assertEqual(actual["parent"], expected["parent"])
        finally:
            os.close(parent)

    def test_ctime_only_change_before_final_check_is_rejected(self):
        real_save = M.save_state
        injected = False

        def save_then_change(path, state):
            nonlocal injected
            real_save(path, state)
            if injected or not state["operations"]:
                return
            injected = True
            # An observable version change before the final check must refuse,
            # even though the operator's sole-writer precondition was violated.
            # chmod to the same mode changes ctime without changing file data.
            time.sleep(0.01)
            os.chmod(self.target, self.baseline["metadata"]["mode"])
            parent = os.open(self.project, M.DIR_FLAGS)
            fd = M.open_existing(parent, self.target.name)
            try:
                actual, _ = M.confirmation_snapshot(fd, parent)
            finally:
                os.close(fd)
                os.close(parent)
            self.assertEqual(actual["content"], self.baseline["content"])
            self.assertEqual(actual["inode"], self.baseline["inode"])
            self.assertEqual(actual["parent"], self.baseline["parent"])
            diff = {key for key in actual["metadata"]
                    if actual["metadata"][key] != self.baseline["metadata"][key]}
            self.assertEqual(diff, {"ctime_ns"})

        with mock.patch.object(M, "save_state", save_then_change), \
                mock.patch.object(M, "rename_exchange", wraps=M.rename_exchange) as exchange:
            with self.assertRaisesRegex(M.InstallError, "version changed before exchange"):
                self.apply()
        self.assertTrue(injected)
        exchange.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(self.target.stat().st_ino, self.baseline["inode"]["number"])
        self.assertEqual(M.load_state(str(self.state))["operations"][-1]["status"], "pending")


if __name__ == "__main__":
    unittest.main(verbosity=2)
