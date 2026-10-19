#!/usr/bin/env python3
"""Exact journal versions across installer-owned publication and recovery."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "publication_version", ROOT / ".agent-maintenance/installer-transaction.py"
)
M = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(M)


class PublicationVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="publication-version-")
        self.addCleanup(self.tmp.cleanup)
        self.area = Path(self.tmp.name).resolve()
        self.project = self.area / "project"
        self.project.mkdir()
        self.source = self.area / "source"
        self.source.write_bytes(b"installer-created payload\n")
        self.state = self.area / "state.json"
        M.save_state(str(self.state), {"operations": [], "created_directories": []})

    def publish(self, provider=None):
        with contextlib.redirect_stdout(io.StringIO()):
            if provider is None:
                M.op_copy(str(self.project), "victim.txt", str(self.source), str(self.state))
                return self.project / "victim.txt"
            relative = M.HOOK_SETTINGS[provider]
            M.op_merge_hook(str(self.project), provider, relative, str(self.state))
            return self.project / relative

    def signature(self, target):
        parent = os.open(target.parent, M.DIR_FLAGS)
        try:
            return M.signature_at(parent, target.name)
        finally:
            os.close(parent)

    def rollback(self):
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            result = M.rollback(str(self.project), str(self.state))
        return result, errors.getvalue()

    def test_copy_and_both_providers_record_exact_published_version(self):
        for provider in (None, "codex", "claude"):
            with self.subTest(provider=provider):
                target = self.publish(provider)
                operation = M.load_state(str(self.state))["operations"][-1]
                self.assertEqual(operation["status"], "published")
                self.assertEqual(operation["expected"], self.signature(target))
                self.assertEqual(operation["expected"]["nlink"], 1)
                self.assertEqual(operation["expected"]["ctime_ns"], target.stat().st_ctime_ns)
                self.assertTrue(M.same_identity(self.signature(target), operation["expected"]))
        result, errors = self.rollback()
        self.assertEqual(result, 1)  # Recovery entries are never deleted.
        for relative in ("victim.txt", *M.HOOK_SETTINGS.values()):
            target = self.project / relative
            self.assertFalse(target.exists())
            self.assertEqual(len(list(target.parent.glob(f".{target.name}.rollback-*"))), 1)
        self.assertIn("PRESERVED rollback recovery file", errors)

    def test_ctime_only_change_after_publication_remains_strict(self):
        target = self.publish()
        expected = M.load_state(str(self.state))["operations"][-1]["expected"]
        time.sleep(0.01)
        os.chmod(target, expected["mode"])
        current = self.signature(target)
        self.assertEqual({key for key in current if current[key] != expected[key]}, {"ctime_ns"})
        self.assertFalse(M.same_identity(current, expected))
        result, errors = self.rollback()
        self.assertEqual(result, 1)
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), self.source.read_bytes())
        self.assertFalse(list(self.project.glob(".victim.txt.rollback-*")))
        self.assertIn("PRESERVED concurrent change", errors)
        self.assertEqual(M.load_state(str(self.state))["operations"][-1]["expected"], expected)

    def test_write_restore_before_rollback_is_not_reauthorized(self):
        target = self.publish()
        original = target.read_bytes()
        expected = M.load_state(str(self.state))["operations"][-1]["expected"]
        time.sleep(0.01)
        with target.open("r+b") as handle:
            handle.write(b"X" * len(original))
            handle.flush()
            os.fsync(handle.fileno())
            handle.seek(0)
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        os.utime(target, ns=(target.stat().st_atime_ns, expected["mtime_ns"]))
        current = self.signature(target)
        self.assertEqual({key for key in current if current[key] != expected[key]}, {"ctime_ns"})
        result, errors = self.rollback()
        self.assertEqual(result, 1)
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), original)
        self.assertIn("PRESERVED concurrent change", errors)

    def test_failed_post_publication_journal_keeps_stale_version_and_file(self):
        save = M.save_state

        def fail_final_save(path, state):
            if state["operations"] and state["operations"][-1].get("status") == "published":
                raise OSError("injected exact-version journal failure")
            return save(path, state)

        with mock.patch.object(M, "save_state", fail_final_save):
            with self.assertRaisesRegex(OSError, "exact-version journal failure"):
                self.publish()
        target = self.project / "victim.txt"
        operation = M.load_state(str(self.state))["operations"][-1]
        self.assertNotEqual(operation.get("status"), "published")
        self.assertNotEqual(self.signature(target)["ctime_ns"], operation["expected"]["ctime_ns"])
        result, errors = self.rollback()
        self.assertEqual(result, 1)
        self.assertTrue(target.exists())
        self.assertFalse(list(self.project.glob(".victim.txt.rollback-*")))
        self.assertIn("PRESERVED concurrent change", errors)

    def test_namespace_comparison_only_allows_ctime(self):
        target = self.publish()
        expected = self.signature(target)
        self.assertTrue(M.namespace_identity_matches({**expected, "ctime_ns": expected["ctime_ns"] + 1}, expected))
        for key in expected:
            if key == "ctime_ns":
                continue
            with self.subTest(field=key):
                changed = {**expected, key: "different"}
                self.assertFalse(M.namespace_identity_matches(changed, expected))


if __name__ == "__main__":
    unittest.main(verbosity=2)
