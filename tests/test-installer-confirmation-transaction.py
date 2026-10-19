#!/usr/bin/env python3
from __future__ import annotations

import json
import importlib.util
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


def run(*args, cwd=None):
    return subprocess.run(
        ["python3", str(HELPER), *map(str, args)], cwd=cwd or ROOT,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


class ConfirmationTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="confirm-transaction-")
        self.area = pathlib.Path(self.tmp.name)
        self.project = self.area / "project"
        (self.project / ".claude").mkdir(parents=True)
        self.target = self.project / ".claude/settings.json"
        self.target.write_bytes(b'{"user":true}\n')
        os.chmod(self.target, 0o640)
        try:
            os.setxattr(self.target, "user.confirmation", b"keep-me")
        except (AttributeError, OSError):
            pass
        self.prefix = self.area / "review" / "settings"
        self.prefix.parent.mkdir()
        self.stage = self.area / "staged.json"
        self.stage.write_bytes(b'{"confirmed":true}\n')
        self.state = self.area / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def propose(self):
        result = run("proposal", self.project, ".claude/settings.json", self.prefix)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.prefix.parent / "settings.original").is_file())
        return json.loads((self.prefix.parent / "settings.identity.json").read_text())

    def test_proposal_apply_preserves_original_metadata_and_leaves_recovery(self):
        before = self.target.stat()
        identity = self.propose()
        self.assertIn("inode", identity)
        self.assertIn("content", identity)
        self.assertIn("metadata", identity)
        self.assertIn("parent", identity)
        result = run("apply-proposal", self.project, ".claude/settings.json", self.prefix, self.stage, self.state)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.read_bytes(), self.stage.read_bytes())
        backups = list(self.target.parent.glob(".settings.json.confirm-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b'{"user":true}\n')
        self.assertEqual((backups[0].stat().st_mode & 0o777), before.st_mode & 0o777)
        try:
            self.assertEqual(os.getxattr(backups[0], "user.confirmation"), b"keep-me")
        except (AttributeError, OSError):
            pass

    def test_in_place_change_after_confirmation_is_refused(self):
        self.propose()
        self.target.write_bytes(b'external in-place change\n')
        result = run("apply-proposal", self.project, ".claude/settings.json", self.prefix, self.stage, self.state)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_bytes(), b'external in-place change\n')

    def test_inode_replacement_after_confirmation_is_refused(self):
        self.propose()
        moved = self.target.with_name("replacement")
        self.target.rename(moved)
        self.target.write_bytes(b'external replacement\n')
        result = run("apply-proposal", self.project, ".claude/settings.json", self.prefix, self.stage, self.state)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_bytes(), b'external replacement\n')

    def test_parent_symlink_replacement_is_refused(self):
        self.propose()
        outside = self.area / "outside"
        outside.mkdir()
        original_parent = self.project / ".claude"
        original_parent.rename(self.project / ".claude-real")
        (self.project / ".claude").symlink_to(outside, target_is_directory=True)
        result = run("apply-proposal", self.project, ".claude/settings.json", self.prefix, self.stage, self.state)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((outside / "settings.json").exists())

    def test_missing_or_unsafe_stage_fails_closed(self):
        self.propose()
        result = run("apply-proposal", self.project, ".claude/settings.json", self.prefix, self.area / "missing", self.state)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.target.read_bytes(), b'{"user":true}\n')

    def test_confirmation_path_is_allowlisted(self):
        other = self.project / "other.json"
        other.write_bytes(b"original\n")
        result = run("proposal", self.project, "other.json", self.prefix)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not permitted", result.stderr)

    def test_rollback_preserves_active_and_displaced_confirmation_inodes(self):
        self.propose()
        result = run("apply-proposal", self.project, ".claude/settings.json", self.prefix, self.stage, self.state)
        self.assertEqual(result.returncode, 0, result.stderr)
        backup = next(self.target.parent.glob(".settings.json.confirm-backup-*"))
        rollback = run("rollback", self.project, self.state)
        self.assertNotEqual(rollback.returncode, 0)
        self.assertEqual(self.target.read_bytes(), b'{"confirmed":true}\n')
        self.assertEqual(backup.read_bytes(), b'{"user":true}\n')

    def module(self):
        spec = importlib.util.spec_from_file_location('confirmation_transaction', HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_exchange_boundary_replacement_and_inplace_write_preserved(self):
        module = self.module()
        for replace in [False, True]:
            with self.subTest(replace=replace):
                self.target.write_bytes(b'{"user":true}\n')
                self.propose()
                external = b'{"external":"at exchange boundary"}\n'
                real_exchange = module.rename_exchange
                def race(parent_fd, left, right):
                    if replace:
                        replacement = self.target.with_name('external.tmp')
                        replacement.write_bytes(external)
                        replacement.replace(self.target)
                    else:
                        self.target.write_bytes(external)
                    real_exchange(parent_fd, left, right)
                with mock.patch.object(module, 'rename_exchange', race):
                    with self.assertRaises(module.InstallError):
                        module.apply_proposal_command(str(self.project), '.claude/settings.json',
                                                      str(self.prefix), str(self.stage), str(self.state))
                recovery = list(self.target.parent.glob('.settings.json.confirm-backup-*'))
                self.assertTrue(any(path.read_bytes() == external for path in recovery))
                self.assertEqual(json.loads(self.state.read_bytes())['operations'][-1]['status'], 'pending')

    def test_preexchange_journal_failure_does_not_delete_competing_backup_path(self):
        module = self.module()
        self.propose()
        external = b'external replaced staging name\n'
        replaced = []
        def fail_state(_filename, state):
            backup = self.target.parent / state['operations'][-1]['confirmation_backup']
            replacement = self.target.with_name('external.tmp')
            replacement.write_bytes(external)
            replacement.replace(backup)
            replaced.append(backup)
            raise OSError('injected journal failure')
        with mock.patch.object(module, 'save_state', fail_state):
            with self.assertRaises(OSError):
                module.apply_proposal_command(str(self.project), '.claude/settings.json',
                                              str(self.prefix), str(self.stage), str(self.state))
        self.assertEqual(self.target.read_bytes(), b'{"user":true}\n')
        self.assertEqual(replaced[0].read_bytes(), external)

    def test_open_writer_fd_retained_after_success(self):
        self.propose()
        fd = os.open(self.target, os.O_WRONLY | os.O_APPEND)
        try:
            result = run('apply-proposal', self.project, '.claude/settings.json', self.prefix, self.stage, self.state)
            self.assertEqual(result.returncode, 0, result.stderr)
            os.write(fd, b'late external descriptor write\n')
        finally:
            os.close(fd)
        backup = next(self.target.parent.glob('.settings.json.confirm-backup-*'))
        self.assertIn(b'late external descriptor write', backup.read_bytes())
        self.assertEqual(self.target.read_bytes(), b'{"confirmed":true}\n')

    def test_deleted_active_still_reports_retained_original_backup(self):
        self.propose()
        result = run('apply-proposal', self.project, '.claude/settings.json', self.prefix, self.stage, self.state)
        self.assertEqual(result.returncode, 0, result.stderr)
        backup = next(self.target.parent.glob('.settings.json.confirm-backup-*'))
        self.target.unlink()
        result = run('rollback', self.project, self.state)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('recovery backup', result.stderr)
        self.assertEqual(backup.read_bytes(), b'{"user":true}\n')


if __name__ == "__main__":
    unittest.main()
