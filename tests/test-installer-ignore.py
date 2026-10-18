#!/usr/bin/env python3
"""Black-box regression tests for post-install AGENTS.md Git-ignore diagnostics."""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "install.sh"
WARNING = "WARNING: AGENTS.md is untracked and ignored by Git;"
NOTICE = "NOTICE: AGENTS.md is already tracked; Git ignore rules do not apply"


class InstallerIgnoreDiagnosticsTests(unittest.TestCase):
    def _env(self, home: pathlib.Path, global_config: pathlib.Path) -> dict[str, str]:
        env = os.environ.copy()
        for key in (
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_IMPLICIT_WORK_TREE", "GIT_PREFIX", "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE",
            "GIT_REPLACE_REF_BASE", "GIT_NO_REPLACE_OBJECTS", "GIT_NAMESPACE",
            "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
            "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
        ):
            env.pop(key, None)
        for key in list(env):
            if key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
                env.pop(key, None)
        env.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / "xdg"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": str(global_config),
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        return env

    def _git(self, cwd: pathlib.Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=cwd, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )

    def _init(self, path: pathlib.Path, env: dict[str, str]) -> None:
        path.mkdir(parents=True)
        self._git(path, "init", "--quiet", env=env)

    def _run(self, target: pathlib.Path, env: dict[str, str], *extra: str, no_hooks: bool = True) -> subprocess.CompletedProcess[str]:
        args = ["bash", str(INSTALLER), "--target", str(target)]
        if no_hooks:
            args.append("--no-hooks")
        args.extend(extra)
        return subprocess.run(
            args,
            cwd=target, env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=60,
        )

    def _oracle(self, target: pathlib.Path, env: dict[str, str], ignored: bool) -> None:
        result = subprocess.run(
            ["git", "check-ignore", "--verbose", "-z", "--stdin"],
            cwd=target, env=env, input=b"AGENTS.md\0", stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        fields = result.stdout.split(b"\0")
        actual = False
        if result.stdout:
            self.assertEqual(len(fields), 5)
            source, line, pattern, path = fields[:4]
            self.assertTrue(source and line and path == b"AGENTS.md")
            actual = not pattern.startswith(b"!")
        self.assertEqual(actual, ignored, (result.returncode, result.stdout))

    @contextmanager
    def _fixture(self, name: str = "project"):
        temp = tempfile.TemporaryDirectory(prefix="installer-ignore-")
        root = pathlib.Path(temp.name)
        home = root / "home"
        home.mkdir()
        global_config = home / ".gitconfig"
        global_config.write_bytes(b"[core]\n")
        env = self._env(home, global_config)
        target = root / name
        self._init(target, env)
        try:
            yield temp, root, target, home, global_config, env
        finally:
            temp.cleanup()

    def _snapshot(self, paths: list[pathlib.Path]) -> dict[pathlib.Path, tuple[bytes, int, int, int]]:
        result = {}
        for path in paths:
            stat = path.stat()
            result[path] = (path.read_bytes(), stat.st_ino, stat.st_mode, stat.st_mtime_ns)
        return result

    def _assert_preserved(self, before, paths):
        self.assertEqual(before, self._snapshot(paths))

    def _assert_warning(self, result: subprocess.CompletedProcess[str], source: str, line: int, pattern: str):
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(WARNING, result.stdout)
        self.assertIn(source, result.stdout)
        self.assertIn(f":{line}:", result.stdout)
        self.assertIn(pattern, result.stdout)

    def test_gitignore_covers_new_and_existing_untracked_and_duplicate_install(self):
        with self._fixture() as fixture:
            _, _, target, _, global_config, env = fixture
            ignore = target / ".gitignore"
            ignore.write_text("# fixture\nAGENTS.md\n", encoding="utf-8")
            global_exclude = target.parent / "global-ignore"
            global_exclude.write_text("AGENTS.md\n", encoding="utf-8")
            self._git(target, "config", "--global", "core.excludesFile", str(global_exclude), env=env)
            self._oracle(target, env, True)
            agents = target / "AGENTS.md"
            before = self._snapshot([ignore, target / ".git/info/exclude", target / ".git/config", global_config])
            result = self._run(target, env)
            self._assert_warning(result, ".gitignore", 2, "AGENTS.md")
            self.assertTrue(agents.is_file())
            content = agents.read_bytes()
            result2 = self._run(target, env)
            self._assert_warning(result2, ".gitignore", 2, "AGENTS.md")
            self.assertEqual(agents.read_bytes(), content)
            self._assert_preserved(before, list(before))

    def test_ignore_diagnostic_also_runs_with_hooks_enabled(self):
        with self._fixture() as fixture:
            _, _, target, _, _, env = fixture
            (target / ".gitignore").write_text("AGENTS.md\n", encoding="utf-8")
            self._oracle(target, env, True)
            result = self._run(target, env, no_hooks=False)
            self._assert_warning(result, ".gitignore", 1, "AGENTS.md")
            self.assertTrue((target / "AGENTS.md").is_file())

    def test_git_info_exclude_is_reported_without_rewriting_ignore_files(self):
        with self._fixture() as fixture:
            _, _, target, _, global_config, env = fixture
            exclude = target / ".git/info/exclude"
            exclude.write_text("# local\nAGENTS.md\n", encoding="utf-8")
            self._oracle(target, env, True)
            paths = [exclude, target / ".git/config", global_config]
            before = self._snapshot(paths)
            result = self._run(target, env)
            self._assert_warning(result, ".git/info/exclude", 2, "AGENTS.md")
            self.assertTrue((target / "AGENTS.md").is_file())
            self._assert_preserved(before, paths)

    def test_global_core_excludes_file_is_reported_and_preserved(self):
        with self._fixture() as fixture:
            _, _, target, _, global_config, env = fixture
            global_exclude = target.parent / "global-ignore"
            global_exclude.write_text("# global\nAGENTS.md\n", encoding="utf-8")
            self._git(target, "config", "--global", "core.excludesFile", str(global_exclude), env=env)
            self._oracle(target, env, True)
            paths = [global_config, global_exclude, target / ".git/config"]
            before = self._snapshot(paths)
            result = self._run(target, env)
            self._assert_warning(result, str(global_exclude), 2, "AGENTS.md")
            self.assertTrue((target / "AGENTS.md").is_file())
            self._assert_preserved(before, paths)

    def test_non_ignored_agents_has_no_warning(self):
        with self._fixture() as fixture:
            _, _, target, _, global_config, env = fixture
            ignore = target / ".gitignore"
            ignore.write_text("other-file\n", encoding="utf-8")
            self._oracle(target, env, False)
            before = self._snapshot([ignore, target / ".git/info/exclude", target / ".git/config", global_config])
            result = self._run(target, env)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertNotIn(WARNING, result.stdout)
            self.assertNotIn("AGENTS.md is already tracked", result.stdout)
            self.assertTrue((target / "AGENTS.md").is_file())
            self._assert_preserved(before, list(before))

    def test_negation_rule_wins_and_does_not_warn(self):
        with self._fixture() as fixture:
            _, _, target, _, _, env = fixture
            ignore = target / ".gitignore"
            ignore.write_text("AGENTS.md\n!AGENTS.md\n", encoding="utf-8")
            paths = [ignore, target / ".git/info/exclude", target / ".git/config"]
            before = self._snapshot(paths)
            self._oracle(target, env, False)
            result = self._run(target, env)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertNotIn(WARNING, result.stdout)
            self.assertTrue((target / "AGENTS.md").is_file())
            self._assert_preserved(before, paths)

    def test_check_ignore_rc128_is_warning_but_install_succeeds(self):
        with self._fixture() as fixture:
            _, root, target, _, _, env = fixture
            wrapper = root / "bin" / "git"
            wrapper.parent.mkdir()
            wrapper.write_text(
                "#!/bin/sh\n"
                "for arg in \"$@\"; do\n"
                "  if [ \"$arg\" = check-ignore ]; then\n"
                "    echo injected-check-ignore-failure >&2; exit 128\n"
                "  fi\n"
                "done\n"
                "exec \"$REAL_GIT\" \"$@\"\n", encoding="utf-8",
            )
            wrapper.chmod(0o755)
            env = dict(env)
            env["REAL_GIT"] = shutil.which("git") or "git"
            env["PATH"] = f"{wrapper.parent}:{env['PATH']}"
            result = self._run(target, env)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("cannot check Git ignore status for AGENTS.md", result.stdout)
            self.assertIn("completed installation was preserved", result.stdout)
            self.assertTrue((target / "AGENTS.md").is_file())

    def test_default_xdg_global_ignore_is_reported(self):
        with self._fixture() as fixture:
            _, root, target, home, _, env = fixture
            global_exclude = home / "xdg" / "git" / "ignore"
            global_exclude.parent.mkdir(parents=True)
            global_exclude.write_text("# xdg global\nAGENTS.md\n", encoding="utf-8")
            self._oracle(target, env, True)
            before = self._snapshot([global_exclude, target / ".git/config"])
            result = self._run(target, env)
            self._assert_warning(result, str(global_exclude), 2, "AGENTS.md")
            self._assert_preserved(before, list(before))

    def test_repository_negation_overrides_global_ignore(self):
        with self._fixture() as fixture:
            _, root, target, home, _, env = fixture
            global_exclude = home / "xdg" / "git" / "ignore"
            global_exclude.parent.mkdir(parents=True)
            global_exclude.write_text("AGENTS.md\n", encoding="utf-8")
            ignore = target / ".gitignore"
            ignore.write_text("!AGENTS.md\n", encoding="utf-8")
            paths = [global_exclude, ignore, target / ".git/config"]
            before = self._snapshot(paths)
            self._oracle(target, env, False)
            result = self._run(target, env)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertNotIn(WARNING, result.stdout)
            self._assert_preserved(before, paths)

    def test_warning_explains_disk_status_omission_and_sharing(self):
        with self._fixture() as fixture:
            _, _, target, _, _, env = fixture
            (target / ".gitignore").write_text("AGENTS.md\n", encoding="utf-8")
            result = self._run(target, env)
            self._assert_warning(result, ".gitignore", 1, "AGENTS.md")
            for phrase in ("remains on disk", "normally omitted", "shared in a commit"):
                self.assertIn(phrase, result.stdout)

    def test_rule_source_control_characters_are_escaped(self):
        with self._fixture() as fixture:
            _, root, target, _, _, env = fixture
            global_exclude = root / "rule\twith\ncontrols"
            global_exclude.write_text("AGENTS.md\n", encoding="utf-8")
            self._git(target, "config", "--global", "core.excludesFile", str(global_exclude), env=env)
            self._oracle(target, env, True)
            result = self._run(target, env)
            self._assert_warning(result, "rule\\twith\\ncontrols", 1, "AGENTS.md")
            self.assertNotIn("rule\twith\ncontrols", result.stdout)

    def test_tracked_agents_uses_template_bytes_and_emits_notice(self):
        with self._fixture() as fixture:
            _, _, target, _, _, env = fixture
            agents = target / "AGENTS.md"
            agents.write_bytes((REPO_ROOT / "AGENTS.md").read_bytes())
            self._git(target, "add", "-f", "AGENTS.md", env=env)
            (target / ".gitignore").write_text("AGENTS.md\n", encoding="utf-8")
            preserved = self._snapshot([agents, target / ".git/index"])
            result = self._run(target, env)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn(NOTICE, result.stdout)
            self.assertNotIn(WARNING, result.stdout)
            self._assert_preserved(preserved, list(preserved))

    def test_foreign_repository_environment_cannot_hide_target_ignore(self):
        variants = (
            "git-dir", "index", "worktree", "dir-and-worktree", "common-dir",
            "implicit-worktree", "ceiling", "combined",
        )
        for variant in variants:
            with self.subTest(variant=variant), self._fixture() as fixture:
                _, root, repo, _, global_config, env = fixture
                target = repo / "nested" / "child"
                target.mkdir(parents=True)
                ignore = repo / ".gitignore"
                ignore.write_text("AGENTS.md\n", encoding="utf-8")
                foreign = root / "foreign"
                self._init(foreign, env)
                foreign_agents = foreign / "nested" / "child" / "AGENTS.md"
                foreign_agents.parent.mkdir(parents=True)
                foreign_agents.write_text("foreign tracked file\n", encoding="utf-8")
                self._git(foreign, "add", ".", env=env)
                foreign_git = foreign / ".git"
                overrides = {
                    "git-dir": {"GIT_DIR": str(foreign_git)},
                    "index": {"GIT_INDEX_FILE": str(foreign_git / "index")},
                    "worktree": {"GIT_WORK_TREE": str(foreign)},
                    "dir-and-worktree": {
                        "GIT_DIR": str(foreign_git), "GIT_WORK_TREE": str(foreign),
                    },
                    "common-dir": {"GIT_COMMON_DIR": str(foreign_git)},
                    "implicit-worktree": {"GIT_IMPLICIT_WORK_TREE": "0"},
                    "ceiling": {"GIT_CEILING_DIRECTORIES": str(target.parent)},
                    "combined": {
                        "GIT_DIR": str(foreign_git), "GIT_WORK_TREE": str(foreign),
                        "GIT_COMMON_DIR": str(foreign_git),
                        "GIT_INDEX_FILE": str(foreign_git / "index"),
                        "GIT_OBJECT_DIRECTORY": str(foreign_git / "objects"),
                        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(foreign_git / "objects"),
                        "GIT_PREFIX": "foreign/", "GIT_IMPLICIT_WORK_TREE": "0",
                        "GIT_CEILING_DIRECTORIES": str(target.parent),
                        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "0",
                    },
                }[variant]
                paths = [ignore, repo / ".git/config", global_config,
                         foreign_git / "config", foreign_git / "index", foreign_agents]
                before = self._snapshot(paths)
                self._oracle(target, env, True)
                result = self._run(target, dict(env, **overrides))
                self._assert_warning(result, ".gitignore", 1, "AGENTS.md")
                self.assertNotIn(NOTICE, result.stdout)
                self.assertNotIn("cannot check Git ignore status", result.stdout)
                self._assert_preserved(before, paths)

    def test_foreign_index_cannot_turn_tracked_target_into_ignore_warning(self):
        with self._fixture() as fixture:
            _, root, target, _, _, env = fixture
            agents = target / "AGENTS.md"
            agents.write_bytes((REPO_ROOT / "AGENTS.md").read_bytes())
            self._git(target, "add", "AGENTS.md", env=env)
            (target / ".gitignore").write_text("AGENTS.md\n", encoding="utf-8")
            foreign = root / "foreign"
            self._init(foreign, env)
            (foreign / "other").write_text("foreign\n", encoding="utf-8")
            self._git(foreign, "add", "other", env=env)
            paths = [target / ".git/index", foreign / ".git/index"]
            before = self._snapshot(paths)
            result = self._run(target, dict(env, GIT_INDEX_FILE=str(foreign / ".git/index")))
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn(NOTICE, result.stdout)
            self.assertNotIn(WARNING, result.stdout)
            self._assert_preserved(before, paths)

    def test_foreign_git_dir_does_not_give_non_git_target_ignore_semantics(self):
        with self._fixture() as fixture:
            _, root, foreign, _, _, env = fixture
            target = root / "non-git"
            target.mkdir()
            (foreign / ".gitignore").write_text("AGENTS.md\n", encoding="utf-8")
            (foreign / "AGENTS.md").write_text("foreign tracked file\n", encoding="utf-8")
            self._git(foreign, "add", "-f", "AGENTS.md", env=env)
            result = self._run(target, dict(env, GIT_DIR=str(foreign / ".git")))
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertNotIn(WARNING, result.stdout)
            self.assertNotIn(NOTICE, result.stdout)
            self.assertNotIn("cannot check Git ignore status", result.stdout)

    def test_repository_environment_isolated_without_losing_config_overrides(self):
        for config_kind in ("count", "parameters"):
            with self.subTest(config_kind=config_kind), self._fixture() as fixture:
                _, root, target, _, global_config, env = fixture
                foreign = root / "foreign"
                self._init(foreign, env)
                exclude = root / "override-ignore"
                exclude.write_text("# configured\nAGENTS.md\n", encoding="utf-8")
                overrides = {"GIT_DIR": str(foreign / ".git"),
                             "GIT_WORK_TREE": str(foreign)}
                if config_kind == "count":
                    overrides.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.excludesFile",
                                     GIT_CONFIG_VALUE_0=str(exclude))
                else:
                    overrides["GIT_CONFIG_PARAMETERS"] = f"'core.excludesFile={exclude}'"
                paths = [exclude, global_config, target / ".git/config", foreign / ".git/config"]
                before = self._snapshot(paths)
                result = self._run(target, dict(env, **overrides))
                self._assert_warning(result, str(exclude), 2, "AGENTS.md")
                self.assertNotIn("cannot check Git ignore status", result.stdout)
                self._assert_preserved(before, paths)

    def test_no_hooks_subdirectory_uses_parent_gitignore_rule(self):
        with self._fixture() as fixture:
            _, _, root, _, _, env = fixture
            (root / ".gitignore").write_text("AGENTS.md\n", encoding="utf-8")
            target = root / "nested" / "child"
            target.mkdir(parents=True)
            self._oracle(target, env, True)
            result = self._run(target, env)
            self._assert_warning(result, ".gitignore", 1, "AGENTS.md")
            self.assertTrue((target / "AGENTS.md").is_file())

    def test_non_git_directory_still_installs_and_does_not_warn(self):
        with tempfile.TemporaryDirectory(prefix="installer-ignore-nongit-") as name:
            root = pathlib.Path(name)
            home = root / "home"
            home.mkdir()
            global_config = home / ".gitconfig"
            env = self._env(home, global_config)
            target = root / "not-a-repository"
            target.mkdir()
            (target / ".gitignore").write_text("AGENTS.md\n", encoding="utf-8")
            result = self._run(target, env)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertNotIn(WARNING, result.stdout)
            self.assertTrue((target / "AGENTS.md").is_file())


if __name__ == "__main__":
    unittest.main()
