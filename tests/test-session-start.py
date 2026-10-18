#!/usr/bin/env python3
"""Startup reminders validate document shape without performing bootstrap."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / ".agent-maintenance/session-start.sh"
REQUIRED_DOCS = (
    "AGENTS.md", "LESSONS.md", "docs/project-profile.md",
    "docs/code-standards.md", "docs/contributors.md", "docs/subsystems/README.md",
)


class SessionStartTests(unittest.TestCase):
    def make_project(self, root: Path) -> None:
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        for relative in REQUIRED_DOCS:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Verified fixture\n", encoding="utf-8")
        (root / "prompts").mkdir()
        (root / "prompts/bootstrap-project.md").write_text("fixture\n", encoding="utf-8")
        (root / ".agent-maintenance").mkdir()
        (root / ".agent-maintenance/bootstrap-complete").write_text(
            "Reviewed HEAD: " + "a" * 40 + "\n", encoding="utf-8"
        )

    def run_hook(self, root: Path, provider: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("CLAUDE_PROJECT_DIR", None)
        cwd = root
        if provider == "claude":
            env["CLAUDE_PROJECT_DIR"] = str(root)
            cwd = root.parent  # Must use the provider root, not the current directory.
        return subprocess.run(
            ["bash", str(HOOK), provider], cwd=cwd, env=env,
            capture_output=True, text=True, timeout=5,
        )

    def test_required_documents(self) -> None:
        variants = (
            "regular", "no-newline", "unknown", "missing", "empty", "whitespace",
            "directory", "symlink", "dangling-symlink", "fifo", "socket",
        )
        for provider in ("codex", "claude"):
            for relative in REQUIRED_DOCS:
                for variant in variants:
                    with self.subTest(provider=provider, path=relative, variant=variant):
                        with tempfile.TemporaryDirectory(prefix="flywheel-session-") as tmp:
                            root = Path(tmp) / "project"
                            self.make_project(root)
                            path = root / relative
                            path.unlink()
                            sock = None
                            if variant == "regular":
                                path.write_text("# Valid\n", encoding="utf-8")
                            elif variant == "no-newline":
                                path.write_text("verified", encoding="utf-8")
                            elif variant == "unknown":
                                path.write_text("UNKNOWN\n", encoding="utf-8")
                            elif variant == "empty":
                                path.touch()
                            elif variant == "whitespace":
                                path.write_text(" \t\r\n\v\f", encoding="utf-8")
                            elif variant == "directory":
                                path.mkdir()
                            elif variant in ("symlink", "dangling-symlink"):
                                external = Path(tmp) / "outside.md"
                                if variant == "symlink":
                                    external.write_text("# Outside\n", encoding="utf-8")
                                path.symlink_to(external)
                            elif variant == "fifo":
                                os.mkfifo(path)
                            elif variant == "socket":
                                sock = socket.socket(socket.AF_UNIX)
                                sock.bind(str(path))
                            before = self.snapshot(root)
                            try:
                                result = self.run_hook(root, provider)
                            finally:
                                if sock is not None:
                                    sock.close()
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertEqual(result.stderr, "")
                            if variant in ("regular", "no-newline", "unknown"):
                                self.assertEqual(result.stdout, "")
                            else:
                                self.assertIn("completion marker is stale", result.stdout)
                                self.assertIn(relative, result.stdout)
                                self.assertIn("must not preempt or interrupt", result.stdout)
                            self.assertEqual(before, self.snapshot(root), "hook wrote project files")

    @staticmethod
    def snapshot(root: Path) -> dict:
        # Do not open symlinks, FIFOs or sockets; ignore atime from legitimate reads.
        return {
            str(path.relative_to(root)): (
                path.lstat().st_mode, path.lstat().st_size, path.lstat().st_mtime_ns,
                path.read_bytes() if path.is_file() and not path.is_symlink() else None,
            )
            for path in root.rglob("*")
        }

    def test_task_prompt_bootstrap_authority(self) -> None:
        # Text-contract regression, not a claim of deterministic model behavior.
        prompt = (REPO_ROOT / "prompts/task-work.md").read_text(encoding="utf-8")
        self.assertNotIn("If the project is not bootstrapped, first run", prompt)
        self.assertIn("explicitly requests template integration or project initialization", prompt)
        self.assertIn("do not start bootstrap merely because", prompt)
        self.assertIn("limit work to what can be verified", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
