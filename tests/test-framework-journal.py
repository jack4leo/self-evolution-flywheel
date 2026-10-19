#!/usr/bin/env python3
"""Real journal-failure recovery tests, without production test-only switches."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def helper():
    spec = importlib.util.spec_from_file_location("journal_test_helper", ROOT / ".agent-maintenance/installer-transaction.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class JournalRecoveryTests(unittest.TestCase):
    def test_failed_replace_retains_old_journal_and_pending_in_memory_state(self):
        tx = helper()
        with tempfile.TemporaryDirectory() as name:
            journal = Path(name) / "transaction.json"
            old = {"operations": [], "created_directories": []}
            tx.save_state(str(journal), old)
            pending = {"operations": [], "created_directories": [".agent-maintenance"]}
            with mock.patch.object(tx.os, "replace", side_effect=OSError("injected journal publication failure")):
                with self.assertRaises(OSError):
                    tx.save_state(str(journal), pending)
            self.assertEqual(json.loads(journal.read_bytes()), old)
            snapshots = list(Path(name).glob("transaction.json.pending-*"))
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(json.loads(snapshots[0].read_bytes()), pending)

    def test_post_exchange_journal_failure_retains_exact_original_and_pending_operation(self):
        with tempfile.TemporaryDirectory() as name:
            area = Path(name)
            source = area / "source"
            shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
            target = area / "project"
            target.mkdir()
            command = ["bash", str(source / "install.sh"), "--target", str(target), "--no-hooks"]
            first = subprocess.run(command, capture_output=True, text=True, timeout=90)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            relative = ".agent-maintenance/installer-transaction.py"
            original = (target / relative).read_bytes()
            with (source / relative).open("ab") as stream:
                stream.write(b"\n# New reviewed framework snapshot for recovery regression.\n")
            injection = area / "injection"
            injection.mkdir()
            (injection / "sitecustomize.py").write_text('''import json, os
_original_replace = os.replace
def fail_applied_journal(source, destination, *args, **kwargs):
    if os.fspath(destination).endswith("/transaction.json"):
        with open(source, encoding="utf-8") as stream:
            state = json.load(stream)
        if any(op.get("status") == "applied" for op in state.get("operations", [])):
            raise OSError("injected post-exchange journal publication failure")
    return _original_replace(source, destination, *args, **kwargs)
os.replace = fail_applied_journal
''')
            result = subprocess.run(command, env={**os.environ, "PYTHONPATH": str(injection), "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=90)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            match = re.search(r"rollback snapshots retained for recovery at: (.+)", output)
            self.assertIsNotNone(match, output)
            workdir = Path(match.group(1).strip())
            self.addCleanup(shutil.rmtree, workdir, True)
            self.assertTrue((workdir / "transaction.json").is_file(), output)
            snapshots = list(workdir.glob("transaction.json.pending-*"))
            self.assertTrue(snapshots, output)
            records = [json.loads(snapshot.read_bytes()) for snapshot in snapshots]
            self.assertTrue(any(op.get("path") == relative and op.get("status") == "applied" for record in records for op in record["operations"]), output)
            backups = list((target / ".agent-maintenance").glob(".installer-transaction.py.confirm-backup-*"))
            self.assertTrue(any(backup.read_bytes() == original for backup in backups), output)
            self.assertEqual((target / relative).read_bytes(), (source / relative).read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
