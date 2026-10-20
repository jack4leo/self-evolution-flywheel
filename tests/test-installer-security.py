#!/usr/bin/env python3
"""Regression tests for installer symlink races and conservative rollback."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.dont_write_bytecode = True
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "install.sh"
HELPER = REPO_ROOT / ".agent-maintenance/installer-transaction.py"
SESSION_START = REPO_ROOT / ".agent-maintenance/session-start.sh"
CODEX_COMMAND = 'bash "$(git rev-parse --show-toplevel)/.agent-maintenance/session-start.sh" codex'
CLAUDE_COMMAND = 'bash "${CLAUDE_PROJECT_DIR}/.agent-maintenance/session-start.sh" claude'
PROVIDER_SETTINGS = {
    "codex": (".codex/hooks.json", CODEX_COMMAND),
    "claude": (".claude/settings.json", CLAUDE_COMMAND),
}


def load_helper():
    spec = importlib.util.spec_from_file_location("installer_transaction_security_test", HELPER)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load transaction helper: {HELPER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def init_git_target(path: pathlib.Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)


class InstallerSecurityRegressionTests(unittest.TestCase):
    def test_settings_symlink_race_cannot_touch_external_file_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="installer-settings-race-") as temp_name:
            temp = pathlib.Path(temp_name)
            target = temp / "project"
            init_git_target(target)
            (target / ".codex").mkdir()
            settings = target / ".codex/hooks.json"
            payload = json.dumps(
                {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": CODEX_COMMAND}]}]}}
            ) + "\n"
            settings.write_text(payload, encoding="utf-8")

            external = temp / "outside-settings.json"
            external.write_text(payload, encoding="utf-8")
            old_mtime_ns = 1_600_000_000_123_456_789
            os.utime(external, ns=(old_mtime_ns, old_mtime_ns))
            marker = temp / "race-injected"
            shim = temp / "python-shim"
            shim.mkdir()
            (shim / "sitecustomize.py").write_text(
                textwrap.dedent(
                    """\
                    import builtins
                    import os
                    import pathlib

                    _real_open = builtins.open
                    _settings = os.environ["RACE_SETTINGS"]
                    _external = os.environ["RACE_EXTERNAL"]
                    _marker = os.environ["RACE_MARKER"]

                    def _race_at_open(file, *args, **kwargs):
                        try:
                            path = os.path.abspath(os.fspath(file))
                        except TypeError:
                            path = ""
                        if path == _settings and not os.path.exists(_marker):
                            os.unlink(_settings)
                            os.symlink(_external, _settings)
                            pathlib.Path(_marker).write_text("injected", encoding="utf-8")
                        return _real_open(file, *args, **kwargs)

                    builtins.open = _race_at_open
                    """
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(shim)
            environment["RACE_SETTINGS"] = str(settings)
            environment["RACE_EXTERNAL"] = str(external)
            environment["RACE_MARKER"] = str(marker)
            result = subprocess.run(
                ["bash", str(INSTALLER), "--target", str(target)],
                cwd=target,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                timeout=60,
            )

            self.assertTrue(marker.is_file(), "test did not replace settings at the open boundary")
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertTrue(settings.is_symlink(), "the injected settings symlink was not preserved")
            self.assertEqual(
                external.stat().st_mtime_ns,
                old_mtime_ns,
                "preflight changed the external target's mtime through a symlink race",
            )
            self.assertFalse((target / "AGENTS.md").exists(), "unsafe settings reached template writes")

    def test_rollback_never_rmdirs_a_directory_moved_outside_target(self) -> None:
        helper = load_helper()
        with tempfile.TemporaryDirectory(prefix="installer-directory-race-") as temp_name:
            temp = pathlib.Path(temp_name)
            target = temp / "project"
            outside = temp / "outside"
            (target / "docs" / "empty").mkdir(parents=True)
            outside.mkdir()
            state_path = temp / "transaction.json"
            helper.save_state(
                str(state_path),
                {"operations": [], "created_directories": ["docs/empty", "docs"]},
            )
            real_rmdir = os.rmdir
            injected = False

            def move_parent_at_rmdir(name, *args, **kwargs):
                nonlocal injected
                if name == "empty" and not injected:
                    os.rename(target / "docs", outside / "moved-docs")
                    os.symlink(outside / "moved-docs", target / "docs")
                    injected = True
                return real_rmdir(name, *args, **kwargs)

            errors = io.StringIO()
            with mock_os_rmdir(move_parent_at_rmdir), contextlib.redirect_stderr(errors):
                status = helper.rollback(str(target), str(state_path))

            self.assertEqual(status, 1, "preserved directories must make rollback explicitly incomplete")
            self.assertFalse(injected, "rollback attempted pathname deletion through a held directory fd")
            self.assertTrue((target / "docs" / "empty").is_dir(), "rollback removed its empty created directory")
            self.assertIn("PRESERVED created directory during rollback", errors.getvalue())

    def test_session_start_rejects_prompt_symlink_outside_project(self) -> None:
        with tempfile.TemporaryDirectory(prefix="session-prompt-symlink-") as temp_name:
            temp = pathlib.Path(temp_name)
            target = temp / "project"
            init_git_target(target)
            (target / "prompts").mkdir()
            external = temp / "external-bootstrap.md"
            external.write_text("untrusted external instructions\n", encoding="utf-8")
            (target / "prompts/bootstrap-project.md").symlink_to(external)

            result = subprocess.run(
                ["bash", str(SESSION_START), "codex"],
                cwd=target,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("symbolic link", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("read and execute prompts/bootstrap-project.md", result.stdout)
            self.assertNotIn("untrusted external instructions", result.stdout)

    def test_help_and_docs_match_confirmed_merge_and_error_stream_behavior(self) -> None:
        help_result = subprocess.run(
            ["bash", str(INSTALLER), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        docs = (REPO_ROOT / "docs/installation.md").read_text(encoding="utf-8")
        help_text = " ".join(help_result.stdout.split())
        self.assertIn("ambiguous ownership requires a reviewed terminal confirmation", help_text)
        self.assertIn("default N", help_text)
        self.assertIn("declining a settings merge skips only that provider", help_text)
        self.assertIn("Existing Codex JSON still requires a manual merge", help_text)
        self.assertIn("requires exact `y`/`Y` confirmation", readme)
        self.assertIn("Existing custom hooks are never deleted", readme)
        self.assertIn("one provider’s skip does not block the other", readme)
        self.assertIn("interactive exact `y`/`Y` response", docs)
        self.assertIn("Unknown or seed-only conflicts are not overwritten", docs)
        self.assertIn("explicit nonzero stderr diagnostic", docs)
        self.assertNotIn("diagnostic to stdout", docs)

    def test_reviewed_through_has_one_authoritative_location(self) -> None:
        lessons = (REPO_ROOT / "LESSONS.md").read_text(encoding="utf-8")
        profile = (REPO_ROOT / "docs/project-profile.md").read_text(encoding="utf-8")
        runner = (REPO_ROOT / "docs/automation/runner-contract.md").read_text(encoding="utf-8")
        self.assertIn("reviewed-through:", lessons.splitlines()[0])
        self.assertIn("single marker at the top of [`LESSONS.md`]", profile)
        self.assertNotIn("SHA reviewed through in the most recent completed review", profile)
        self.assertIn("not a second watermark store", runner)


class InstallerProviderPreflightRegressionTests(unittest.TestCase):
    @staticmethod
    def _snapshot(path: pathlib.Path):
        flags = os.O_RDONLY | getattr(os, "O_NOATIME", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            chunks = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            attrs = {name: os.getxattr(fd, name) for name in os.listxattr(fd)}
            after = os.fstat(fd)
        finally:
            os.close(fd)
        fields = (
            "st_dev", "st_ino", "st_size", "st_mode", "st_uid", "st_gid",
            "st_nlink", "st_atime_ns", "st_mtime_ns", "st_ctime_ns",
        )
        return b"".join(chunks), tuple(getattr(before, name) for name in fields), attrs, tuple(
            getattr(after, name) for name in fields
        )

    @staticmethod
    def _write_state(target: pathlib.Path, provider: str, state: str):
        relative, command = PROVIDER_SETTINGS[provider]
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if state == "normal":
            return path
        if state == "conflict":
            payload = {
                "ownerNote": "preserve this setting",
                "hooks": {
                    "SessionStart": [{
                        "matcher": "startup",
                        "hooks": [{"type": "command", "command": "echo user-owned"}],
                    }]
                },
            }
            path.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
        elif state == "invalid":
            path.write_text("{ invalid JSON\n", encoding="utf-8")
        else:
            raise AssertionError(state)
        return path

    def _run(self, target: pathlib.Path):
        return subprocess.run(
            ["bash", str(INSTALLER), "--target", str(target)],
            cwd=target,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
        )

    def _assert_registered(self, target: pathlib.Path, provider: str):
        relative, command = PROVIDER_SETTINGS[provider]
        config = json.loads((target / relative).read_text(encoding="utf-8"))
        matching = [
            (session, handler)
            for session in config["hooks"]["SessionStart"]
            for handler in session.get("hooks", [])
            if isinstance(handler, dict) and handler.get("command") == command
        ]
        self.assertEqual(len(matching), 1, f"{provider} hook missing or duplicated")
        session, handler = matching[0]
        self.assertEqual(handler.get("type"), "command", f"{provider} hook has wrong type")
        self.assertEqual(
            session.get("matcher"),
            "startup|resume|clear|compact|fork",
            f"{provider} hook does not cover all session-start sources",
        )

    def test_provider_preflight_full_three_by_three_matrix(self) -> None:
        states = ("normal", "conflict", "invalid")
        for codex_state in states:
            for claude_state in states:
                with self.subTest(codex=codex_state, claude=claude_state):
                    with tempfile.TemporaryDirectory(prefix="installer-provider-matrix-") as temp_name:
                        target = pathlib.Path(temp_name) / "project"
                        init_git_target(target)
                        before = {}
                        for provider, state in (("codex", codex_state), ("claude", claude_state)):
                            settings = self._write_state(target, provider, state)
                            if state != "normal":
                                before[provider] = self._snapshot(settings)

                        result = self._run(target)
                        has_invalid = "invalid" in (codex_state, claude_state)
                        if has_invalid:
                            self.assertNotEqual(result.returncode, 0, result.stdout)
                            self.assertIn("no files were installed", result.stdout)
                            self.assertFalse((target / "AGENTS.md").exists(), result.stdout)
                            self.assertFalse((target / ".agent-maintenance/session-start.sh").exists())
                            for provider, state in (("codex", codex_state), ("claude", claude_state)):
                                relative, _ = PROVIDER_SETTINGS[provider]
                                settings = target / relative
                                if state == "normal":
                                    self.assertFalse(settings.exists(), f"invalid preflight created {relative}")
                                else:
                                    self.assertEqual(self._snapshot(settings), before[provider])
                            continue

                        self.assertEqual(result.returncode, 0, result.stdout)
                        self.assertTrue((target / "AGENTS.md").is_file())
                        self.assertTrue((target / ".agent-maintenance/session-start.sh").is_file())
                        for provider, state in (("codex", codex_state), ("claude", claude_state)):
                            relative, _ = PROVIDER_SETTINGS[provider]
                            settings = target / relative
                            if state == "normal":
                                self._assert_registered(target, provider)
                            else:
                                self.assertIn("user-owned", self._snapshot(settings)[0].decode("utf-8"))
                                if provider == "claude":
                                    self._assert_registered(target, provider)
                                else:
                                    self.assertEqual(self._snapshot(settings), before[provider])

    def test_wrong_handler_type_and_narrow_matcher_are_not_registered(self) -> None:
        for provider in ("codex", "claude"):
            relative, command = PROVIDER_SETTINGS[provider]
            for variant in ("wrong-type", "narrow-matcher"):
                with self.subTest(provider=provider, variant=variant):
                    with tempfile.TemporaryDirectory(prefix="installer-hook-schema-") as temp_name:
                        target = pathlib.Path(temp_name) / "project"
                        init_git_target(target)
                        settings = target / relative
                        settings.parent.mkdir(parents=True)
                        handler = {"type": "prompt" if variant == "wrong-type" else "command", "command": command}
                        session = {
                            "matcher": "startup" if variant == "narrow-matcher" else "*",
                            "hooks": [handler],
                        }
                        settings.write_text(
                            json.dumps({"hooks": {"SessionStart": [session]}}) + "\n",
                            encoding="utf-8",
                        )
                        before = self._snapshot(settings)

                        result = self._run(target)

                        self.assertEqual(result.returncode, 0, result.stdout)
                        if provider == "claude":
                            config = json.loads(settings.read_text(encoding="utf-8"))
                            commands = [h.get("command") for group in config["hooks"]["SessionStart"] for h in group.get("hooks", [])]
                            if variant == "narrow-matcher":
                                self.assertEqual(commands.count(command), 1)
                            elif variant == "wrong-type":
                                self.assertEqual(commands.count(command), 2)
                            else:
                                self._assert_registered(target, provider)
                        else:
                            self.assertEqual(self._snapshot(settings), before)
                        self.assertTrue((target / "AGENTS.md").is_file())
                        other_provider = "claude" if provider == "codex" else "codex"
                        self._assert_registered(target, other_provider)
                        self.assertIn(relative, result.stdout)


class mock_os_rmdir:
    """Temporarily patch the shared os module's rmdir and restore it reliably."""

    def __init__(self, replacement):
        self.replacement = replacement
        self.original = None

    def __enter__(self):
        self.original = os.rmdir
        os.rmdir = self.replacement

    def __exit__(self, *_exc):
        os.rmdir = self.original


if __name__ == "__main__":
    unittest.main(verbosity=2)
