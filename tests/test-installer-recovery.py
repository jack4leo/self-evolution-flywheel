#!/usr/bin/env python3
"""SIGKILL recovery tests for provider-settings staging hard links."""
from __future__ import annotations

import json
import os
import pathlib
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "install.sh"
HELPER = REPO_ROOT / ".agent-maintenance/installer-transaction.py"
CODEX_COMMAND = 'bash "$(git rev-parse --show-toplevel)/.agent-maintenance/session-start.sh" codex'
CLAUDE_COMMAND = 'bash "${CLAUDE_PROJECT_DIR}/.agent-maintenance/session-start.sh" claude'
TEMP_RE = re.compile(r"^\.(?:hooks|settings)\.json\.flywheel-[0-9]+-[0-9a-f]{12}$")


def settings_path(target: pathlib.Path, provider: str) -> pathlib.Path:
    return target / (".codex/hooks.json" if provider == "codex" else ".claude/settings.json")


def hook_command(provider: str) -> str:
    return CODEX_COMMAND if provider == "codex" else CLAUDE_COMMAND


def read_noatime(path: pathlib.Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOATIME", 0)
    fd = os.open(path, flags)
    try:
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def expected_user_settings(provider: str) -> dict:
    return {
        "userOwned": {"label": "keep byte-for-byte", "nested": [1, "two"]},
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact|fork",
                    "hooks": [
                        {"type": "command", "command": hook_command(provider)},
                        {"type": "command", "command": "user-owned-extra-hook"},
                    ],
                }
            ]
        },
    }


def make_fixture(
    base: pathlib.Path,
    label: str,
    crashing_provider: str = "codex",
    with_user_settings: bool = True,
) -> tuple[pathlib.Path, pathlib.Path | None, dict | None]:
    target = base / label
    target.mkdir()
    subprocess.run(["git", "init", "--quiet", str(target)], check=True)
    if not with_user_settings:
        return target, None, None
    user_provider = "claude" if crashing_provider == "codex" else "codex"
    user_path = settings_path(target, user_provider)
    user_path.parent.mkdir()
    expected = expected_user_settings(user_provider)
    user_path.write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(user_path, 0o640)
    xattr_name = "user.self_evolution_recovery_test"
    xattr_supported = False
    try:
        os.setxattr(user_path, xattr_name, b"preserve-user-metadata")
        xattr_supported = True
    except OSError as exc:
        if exc.errno not in {1, 13, 22, 95, 45, 61}:
            raise
    original_mtime_ns = 1_700_000_000_123_456_789
    original_bytes = read_noatime(user_path)
    original_xattr_value = os.getxattr(user_path, xattr_name) if xattr_supported else None
    # Establish the test baseline only after fixture reads (including xattr
    # inspection), since some CI filesystems can update atime during reads.
    os.utime(user_path, ns=(original_mtime_ns, original_mtime_ns))
    info = os.stat(user_path)
    metadata = {
        "bytes": original_bytes,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mode": stat.S_IMODE(info.st_mode),
        "atime_ns": info.st_atime_ns,
        "mtime_ns": info.st_mtime_ns,
        "xattr_supported": xattr_supported,
        "xattr_name": xattr_name,
    }
    if xattr_supported:
        metadata["xattr_value"] = original_xattr_value
    return target, user_path, metadata


def installer_env(tmp_root: pathlib.Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    temp_dir = tmp_root / "installer-tmp"
    temp_dir.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["TMPDIR"] = str(temp_dir)
    env.pop("PYTHONPATH", None)
    if extra:
        env.update(extra)
    return env


def install(target: pathlib.Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALLER), "--target", str(target)],
        cwd=target,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=90,
    )


def crash_after_provider_publish(
    base: pathlib.Path,
    tmp_root: pathlib.Path,
    label: str,
    provider: str,
    with_user_settings: bool = True,
):
    target, user_settings_path, metadata = make_fixture(base, label, provider, with_user_settings)
    hook_dir = tmp_root / f"{label}-python-hook"
    hook_dir.mkdir()
    marker = tmp_root / f"{label}-kill-marker"
    sitecustomize = r'''import os
import re
import signal

_real_link = os.link
_root = os.environ.get("SELF_EVOLUTION_RECOVERY_TEST_ROOT")
_marker = os.environ.get("SELF_EVOLUTION_RECOVERY_TEST_MARKER")
_provider = os.environ.get("SELF_EVOLUTION_RECOVERY_TEST_PROVIDER")
_relative = ".codex/hooks.json" if _provider == "codex" else ".claude/settings.json"
_active_name = os.path.basename(_relative)
_expected_parent = os.path.join(_root, os.path.dirname(_relative))
_pattern = re.compile(r"^\." + re.escape(_active_name) + r"\.flywheel-[0-9]+-[0-9a-f]{12}$")

def _kill_after_publish_link(src, dst, *args, **kwargs):
    result = _real_link(src, dst, *args, **kwargs)
    try:
        src_fd = kwargs.get("src_dir_fd")
        dst_fd = kwargs.get("dst_dir_fd")
        if (
            _root
            and _marker
            and _provider in {"codex", "claude"}
            and src_fd is not None
            and dst_fd is not None
            and isinstance(src, str)
            and isinstance(dst, str)
            and _pattern.fullmatch(src)
            and dst == _active_name
            and os.path.samefile(f"/proc/self/fd/{src_fd}", _expected_parent)
            and os.path.samefile(f"/proc/self/fd/{dst_fd}", _expected_parent)
        ):
            staged = os.stat(src, dir_fd=src_fd, follow_symlinks=False)
            current = os.stat(dst, dir_fd=dst_fd, follow_symlinks=False)
            if (
                (staged.st_dev, staged.st_ino) == (current.st_dev, current.st_ino)
                and staged.st_nlink == 2
            ):
                with open(_marker, "w", encoding="utf-8") as handle:
                    handle.write(src + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.killpg(os.getpgrp(), signal.SIGKILL)
    except FileNotFoundError:
        pass
    return result

os.link = _kill_after_publish_link
'''
    (hook_dir / "sitecustomize.py").write_text(sitecustomize, encoding="utf-8")
    env = installer_env(
        tmp_root,
        {
            "PYTHONPATH": str(hook_dir),
            "SELF_EVOLUTION_RECOVERY_TEST_ROOT": str(target),
            "SELF_EVOLUTION_RECOVERY_TEST_MARKER": str(marker),
            "SELF_EVOLUTION_RECOVERY_TEST_PROVIDER": provider,
        },
    )
    log = tmp_root / f"{label}-interrupted.log"
    with log.open("wb") as output:
        process = subprocess.Popen(
            ["bash", str(INSTALLER), "--target", str(target)],
            cwd=target,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise AssertionError("SIGKILL injection did not reach the provider publish-link boundary")
    assert returncode == -signal.SIGKILL, f"expected SIGKILL interruption, got {returncode}; log: {log.read_text(errors='replace')}"
    assert marker.is_file(), f"injection marker was not written at the {provider} post-publish/pre-cleanup boundary"
    marker_name = marker.read_text(encoding="utf-8").strip()
    assert TEMP_RE.fullmatch(marker_name), f"unexpected staged alias name: {marker_name!r}"
    alias = settings_path(target, provider).parent / marker_name
    active = settings_path(target, provider)
    assert alias.is_file() and active.is_file(), "SIGKILL did not leave the expected active settings and staged alias"
    assert os.path.samefile(alias, active), "injected alias is not a hard link to the active provider config"
    assert os.stat(active).st_nlink == 2, "injected link count was not exactly the expected two links"
    return target, user_settings_path, metadata, alias, active


def assert_user_metadata_unchanged(path: pathlib.Path, expected: dict) -> None:
    info = os.stat(path)
    assert read_noatime(path) == expected["bytes"], "user-owned settings bytes were changed"
    assert (info.st_uid, info.st_gid) == (expected["uid"], expected["gid"]), "existing settings owner/group changed"
    assert stat.S_IMODE(info.st_mode) == expected["mode"], "existing settings mode changed"
    assert info.st_atime_ns == expected["atime_ns"], (
        f"user-owned settings atime changed: expected {expected['atime_ns']}, got {info.st_atime_ns}"
    )
    assert info.st_mtime_ns == expected["mtime_ns"], "existing settings mtime changed"
    if expected["xattr_supported"]:
        assert os.getxattr(path, expected["xattr_name"]) == expected["xattr_value"], "existing settings xattr changed"


def assert_install_complete(
    target: pathlib.Path,
    user_settings_path: pathlib.Path | None,
    metadata: dict | None,
    alias: pathlib.Path,
    provider: str,
) -> None:
    active = settings_path(target, provider)
    assert alias.exists(), "possible provider staging link was removed instead of preserved"
    assert os.path.samefile(alias, active), "preserved candidate no longer points to active settings"
    assert os.stat(active).st_nlink == 2, "only the active settings and one candidate link may remain"
    codex_path = target / ".codex" / "hooks.json"
    claude_path = target / ".claude" / "settings.json"
    assert codex_path.is_file() and claude_path.is_file(), "both provider settings files must exist"
    if user_settings_path is not None and metadata is not None:
        assert_user_metadata_unchanged(user_settings_path, metadata)
    codex = json.loads(read_noatime(codex_path).decode("utf-8"))
    codex_handlers = [handler for session in codex["hooks"]["SessionStart"] for handler in session.get("hooks", [])]
    assert [handler.get("command") for handler in codex_handlers].count(CODEX_COMMAND) == 1, "Codex hook missing or duplicated"
    claude = json.loads(read_noatime(claude_path).decode("utf-8"))
    claude_handlers = [handler for session in claude["hooks"]["SessionStart"] for handler in session.get("hooks", [])]
    assert [handler.get("command") for handler in claude_handlers].count(CLAUDE_COMMAND) == 1, "Claude hook missing or duplicated"
    user_provider = "claude" if provider == "codex" else "codex"
    user_handlers = claude_handlers if user_provider == "claude" else codex_handlers
    expected_user_hook_count = 1 if metadata is not None else 0
    assert [handler.get("command") for handler in user_handlers].count("user-owned-extra-hook") == expected_user_hook_count, "user-owned hook changed or duplicated"
    expected_files = [
        "AGENTS.md",
        "CLAUDE.md",
        "LESSONS.md",
        ".agent-maintenance/session-start.sh",
        ".agent-maintenance/installer-transaction.py",
        ".github/PULL_REQUEST_TEMPLATE.md",
    ]
    for directory in (REPO_ROOT / "docs", REPO_ROOT / "prompts"):
        expected_files.extend(
            str(path.relative_to(REPO_ROOT))
            for path in directory.rglob("*")
            if path.is_file() and path != REPO_ROOT / "docs/source-and-extraction.md"
        )
    for relative in expected_files:
        assert (target / relative).read_bytes() == (REPO_ROOT / relative).read_bytes(), f"template file missing or differs: {relative}"
    entry = target / ".agent-maintenance/flywheel-install.sh"
    assert entry.is_file() and not entry.is_symlink(), "dedicated project installer entry is missing"


def assert_fail_closed(target: pathlib.Path, env: dict[str, str], alias: pathlib.Path, phrase: str) -> None:
    before = alias.lstat()
    result = install(target, env)
    assert result.returncode != 0, f"unsafe provider staging state was accepted:\n{result.stdout}"
    assert phrase.lower() in result.stdout.lower(), f"failure did not explain the unsafe state:\n{result.stdout}"
    assert alias.exists(), "unsafe or unrecognized candidate was deleted"
    after = alias.lstat()
    assert (before.st_dev, before.st_ino, before.st_nlink, before.st_uid, before.st_gid) == (
        after.st_dev, after.st_ino, after.st_nlink, after.st_uid, after.st_gid
    ), "fail-closed check changed the candidate entry or inode metadata"


def test_sigkill_retry_repairs_install(tmp_root: pathlib.Path, provider: str) -> None:
    target, user_settings_path, metadata, alias, _ = crash_after_provider_publish(
        tmp_root, tmp_root, f"sigkill-{provider}", provider
    )
    if user_settings_path is not None and metadata is not None:
        interrupted_atime = os.stat(user_settings_path).st_atime_ns
        assert interrupted_atime == metadata["atime_ns"], (
            f"{provider} interrupted publish changed user settings atime before recovery: "
            f"expected {metadata['atime_ns']}, got {interrupted_atime}"
        )
    for attempt in range(2):
        result = install(target, installer_env(tmp_root))
        assert result.returncode == 0, (
            f"rerunning after {provider} SIGKILL did not succeed on attempt {attempt + 1}; "
            f"exit={result.returncode}\n{result.stdout}"
        )
        if user_settings_path is not None and metadata is not None:
            retry_atime = os.stat(user_settings_path).st_atime_ns
            assert retry_atime == metadata["atime_ns"], (
                f"{provider} recovery attempt {attempt + 1} changed user settings atime: "
                f"expected {metadata['atime_ns']}, got {retry_atime}"
            )
        assert_install_complete(target, user_settings_path, metadata, alias, provider)


def test_sigkill_retry_installs_missing_second_provider(tmp_root: pathlib.Path) -> None:
    target, user_settings_path, metadata, alias, _ = crash_after_provider_publish(
        tmp_root, tmp_root, "missing-second-provider", "codex", with_user_settings=False
    )
    result = install(target, installer_env(tmp_root))
    assert result.returncode == 0, (
        "re-running after a Provider A SIGKILL did not install the missing Provider B hook; "
        f"exit={result.returncode}\n{result.stdout}"
    )
    assert user_settings_path is None and metadata is None
    assert_install_complete(target, user_settings_path, metadata, alias, "codex")


def test_extra_hardlink_fails_closed(tmp_root: pathlib.Path) -> None:
    target, _, _, alias, active = crash_after_provider_publish(tmp_root, tmp_root, "extra-hardlink", "codex")
    extra = target / ".codex" / "user-owned-extra-link"
    os.link(active, extra)
    assert os.stat(active).st_nlink == 3
    assert_fail_closed(target, installer_env(tmp_root), alias, "link")
    assert os.path.samefile(alias, active) and os.path.samefile(extra, active)
    assert os.stat(active).st_nlink == 3


def test_wrong_inode_fails_closed(tmp_root: pathlib.Path) -> None:
    target, _, _, alias, active = crash_after_provider_publish(tmp_root, tmp_root, "wrong-inode", "codex")
    alias.unlink()
    alias.write_text("not the active settings inode\n", encoding="utf-8")
    os.chmod(alias, 0o644)
    assert not os.path.samefile(alias, active)
    assert_fail_closed(target, installer_env(tmp_root), alias, "staging")
    assert active.exists() and os.stat(active).st_nlink == 1


def test_owner_or_group_mismatch_fails_closed(tmp_root: pathlib.Path, field: str) -> None:
    target, _, _, alias, active = crash_after_provider_publish(
        tmp_root, tmp_root, f"wrong-{field}", "codex"
    )
    injection_dir = tmp_root / f"stat-mismatch-{field}"
    injection_dir.mkdir()
    (injection_dir / "sitecustomize.py").write_text(r'''import os
_real_stat = os.stat
_root = os.environ["STAT_MISMATCH_ROOT"]
_alias = os.environ["STAT_MISMATCH_ALIAS"]
_field = os.environ["STAT_MISMATCH_FIELD"]
class _StatView:
    def __init__(self, value): self._value = value
    def __getattr__(self, name):
        value = getattr(self._value, name)
        return value + 1 if name == _field else value
def _stat(path, *args, **kwargs):
    result = _real_stat(path, *args, **kwargs)
    parent_fd = kwargs.get("dir_fd")
    if path == _alias and parent_fd is not None and os.path.samefile(f"/proc/self/fd/{parent_fd}", os.path.join(_root, ".codex")):
        return _StatView(result)
    return result
os.stat = _stat
''', encoding="utf-8")
    env = installer_env(
        tmp_root,
        {
            "PYTHONPATH": str(injection_dir),
            "STAT_MISMATCH_ROOT": str(target),
            "STAT_MISMATCH_ALIAS": alias.name,
            "STAT_MISMATCH_FIELD": "st_uid" if field == "owner" else "st_gid",
        },
    )
    # This isolated stat view exercises the mismatch branch on every runner,
    # including non-root CI accounts that cannot chown test inodes.
    assert_fail_closed(target, env, alias, "owner/group")
    assert os.path.samefile(alias, active) and os.stat(active).st_nlink == 2


def test_unknown_name_fails_closed(tmp_root: pathlib.Path) -> None:
    target, _, _, alias, active = crash_after_provider_publish(tmp_root, tmp_root, "unknown-name", "codex")
    unknown = alias.with_name(alias.name + "-unexpected")
    alias.rename(unknown)
    result = install(target, installer_env(tmp_root))
    assert result.returncode != 0, f"unknown alias name was accepted:\n{result.stdout}"
    assert "unrecognized" in result.stdout.lower() or "unknown" in result.stdout.lower(), result.stdout
    assert unknown.exists() and os.path.samefile(unknown, active)
    assert os.stat(active).st_nlink == 2


def test_multiple_candidate_names_fail_closed(tmp_root: pathlib.Path) -> None:
    target, _, _, alias, active = crash_after_provider_publish(tmp_root, tmp_root, "multiple-names", "codex")
    other = alias.with_name(".hooks.json.flywheel-424243-012345abcdef")
    os.link(active, other)
    assert_fail_closed(target, installer_env(tmp_root), alias, "multiple")
    assert os.path.samefile(alias, active) and os.path.samefile(other, active)
    assert os.stat(active).st_nlink == 3


def test_symlink_candidate_fails_closed(tmp_root: pathlib.Path) -> None:
    target, _, _, alias, active = crash_after_provider_publish(tmp_root, tmp_root, "symlink-candidate", "codex")
    alias.unlink()
    alias.symlink_to(active.name)
    assert_fail_closed(target, installer_env(tmp_root), alias, "regular file")
    assert alias.is_symlink() and active.exists()


def test_wrong_content_fails_closed(tmp_root: pathlib.Path) -> None:
    target, _, _, alias, active = crash_after_provider_publish(tmp_root, tmp_root, "wrong-content", "codex")
    active.write_text('{"userOwned":"not the expected hook payload"}\n', encoding="utf-8")
    assert_fail_closed(target, installer_env(tmp_root), alias, "expected codex settings")
    assert os.path.samefile(alias, active) and os.stat(active).st_nlink == 2


def test_candidate_replacement_race_is_non_destructive(tmp_root: pathlib.Path) -> None:
    target, user_settings_path, metadata, alias, active = crash_after_provider_publish(
        tmp_root, tmp_root, "candidate-replacement-race", "codex"
    )
    injection_dir = tmp_root / "candidate-replacement-race-hook"
    injection_dir.mkdir()
    marker = tmp_root / "candidate-replacement-race-marker"
    (injection_dir / "sitecustomize.py").write_text(r'''import os, re
_real_unlink = os.unlink
_root = os.environ["RACE_ROOT"]
_marker = os.environ["RACE_MARKER"]
_pattern = re.compile(r"^\.hooks\.json\.flywheel-[0-9]+-[0-9a-f]{12}$")
def _racing_unlink(path, *args, **kwargs):
    parent_fd = kwargs.get("dir_fd")
    if isinstance(path, str) and _pattern.fullmatch(path) and parent_fd is not None and not os.path.exists(_marker):
        if os.path.samefile(f"/proc/self/fd/{parent_fd}", os.path.join(_root, ".codex")):
            _real_unlink(path, *args, **kwargs)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
            os.write(fd, b"user replacement that must survive")
            os.close(fd)
            with open(_marker, "w", encoding="utf-8") as handle: handle.write(path)
            return _real_unlink(path, *args, **kwargs)
    return _real_unlink(path, *args, **kwargs)
os.unlink = _racing_unlink
''', encoding="utf-8")
    env = installer_env(tmp_root, {"PYTHONPATH": str(injection_dir), "RACE_ROOT": str(target), "RACE_MARKER": str(marker)})
    result = install(target, env)
    assert result.returncode == 0, f"safe retry failed with a candidate-replacement adversary:\n{result.stdout}"
    assert not marker.exists(), "installer attempted pathname unlink on the candidate"
    assert alias.exists() and os.path.samefile(alias, active), "candidate link was not preserved"
    assert_install_complete(target, user_settings_path, metadata, alias, "codex")


def test_forged_matching_hardlink_is_preserved_as_ambiguous(tmp_root: pathlib.Path) -> None:
    target, user_settings_path, metadata = make_fixture(tmp_root, "forged-candidate", "codex")
    first = install(target, installer_env(tmp_root))
    assert first.returncode == 0, first.stdout
    active = settings_path(target, "codex")
    forged = active.with_name(".hooks.json.flywheel-424242-012345abcdef")
    os.link(active, forged)
    result = install(target, installer_env(tmp_root))
    assert result.returncode == 0, f"a matching but unprovable alias blocked a read-only retry:\n{result.stdout}"
    assert "origin cannot be proven" in result.stdout.lower(), result.stdout
    assert forged.exists() and os.path.samefile(forged, active), "forged candidate was deleted"
    assert os.stat(active).st_nlink == 2
    assert_install_complete(target, user_settings_path, metadata, forged, "codex")


def test_competing_installer_does_not_recover(tmp_root: pathlib.Path) -> None:
    target, _, _, alias, active = crash_after_provider_publish(tmp_root, tmp_root, "lock-contention", "codex")
    ready = tmp_root / "lock-holder-ready"
    holder = subprocess.Popen(
        [
            sys.executable,
            str(HELPER),
            "lock-exec",
            str(target),
            "/bin/sh",
            "-c",
            'printf ready > "$1"; sleep 10',
            "lock-holder",
            str(ready),
        ],
        cwd=target,
        env=installer_env(tmp_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), f"lock-holder failed to start: {holder.stdout.read() if holder.poll() is not None else ''}"
        result = install(target, installer_env(tmp_root))
        assert result.returncode != 0, f"a competing installer ran while the target lock was held:\n{result.stdout}"
        assert "another installer" in result.stdout.lower() or "lock" in result.stdout.lower(), result.stdout
        assert alias.exists() and os.path.samefile(alias, active), "competing installer changed the stale alias"
        assert os.stat(active).st_nlink == 2
    finally:
        if holder.poll() is None:
            os.killpg(holder.pid, signal.SIGKILL)
        holder.wait(timeout=10)


def test_bootstrap_releases_lock_for_long_running_cli(tmp_root: pathlib.Path, provider: str) -> None:
    target = tmp_root / f"bootstrap-lock-lifecycle-{provider}"
    target.mkdir()
    subprocess.run(["git", "init", "--quiet", str(target)], check=True)
    initial = install(target, installer_env(tmp_root))
    assert initial.returncode == 0, initial.stdout
    bin_dir = tmp_root / f"bootstrap-cli-bin-{provider}"
    bin_dir.mkdir()
    ready = tmp_root / f"bootstrap-cli-ready-{provider}"
    cli = bin_dir / provider
    cli.write_text(
        '#!/usr/bin/env bash\n'
        'if [[ -n "${SELF_EVOLUTION_FLYWHEEL_LOCK_FD:-}" ]]; then exit 91; fi\n'
        'printf started > "$BOOTSTRAP_READY"\n'
        'sleep 10\n',
        encoding="utf-8",
    )
    cli.chmod(0o755)
    env = installer_env(tmp_root, {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "BOOTSTRAP_READY": str(ready)})
    process = subprocess.Popen(
        ["bash", str(INSTALLER), "--target", str(target), "--bootstrap", provider],
        cwd=target,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), f"long-running CLI stub did not start: {process.stdout.read() if process.poll() is not None else ''}"
        second = install(target, installer_env(tmp_root))
        assert second.returncode == 0, f"lock remained held by bootstrap CLI: exit={second.returncode}\n{second.stdout}"
        assert process.poll() is None, "bootstrap CLI stub exited before the lock-lifecycle check"
        codex_config = json.loads((target / ".codex/hooks.json").read_text(encoding="utf-8"))
        handlers = [handler for session in codex_config["hooks"]["SessionStart"] for handler in session.get("hooks", [])]
        assert [handler.get("command") for handler in handlers].count(CODEX_COMMAND) == 1, "second install duplicated Codex hook"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def main() -> None:
    if sys.platform != "linux":
        raise SystemExit("SIGKILL syscall-boundary recovery tests currently require Linux /proc and process-group semantics")
    with tempfile.TemporaryDirectory(prefix="self-evolution-installer-recovery-") as tmp_name:
        root = pathlib.Path(tmp_name)
        test_sigkill_retry_repairs_install(root, "codex")
        test_sigkill_retry_repairs_install(root, "claude")
        test_sigkill_retry_installs_missing_second_provider(root)
        test_extra_hardlink_fails_closed(root)
        test_wrong_inode_fails_closed(root)
        test_owner_or_group_mismatch_fails_closed(root, "owner")
        test_owner_or_group_mismatch_fails_closed(root, "group")
        test_unknown_name_fails_closed(root)
        test_multiple_candidate_names_fail_closed(root)
        test_symlink_candidate_fails_closed(root)
        test_wrong_content_fails_closed(root)
        test_candidate_replacement_race_is_non_destructive(root)
        test_forged_matching_hardlink_is_preserved_as_ambiguous(root)
        test_competing_installer_does_not_recover(root)
        test_bootstrap_releases_lock_for_long_running_cli(root, "codex")
        test_bootstrap_releases_lock_for_long_running_cli(root, "claude")
    print("PASS: Codex/Claude SIGKILL recovery, non-destructive staging checks, metadata rejection, and lock lifecycle")


if __name__ == "__main__":
    main()
