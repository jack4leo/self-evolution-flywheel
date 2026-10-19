#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

init_git_target() {
  mkdir -p "$1"
  git -C "$1" init -q
}

run_installer_in_pty() {
  python3 - "$1" "$2" "$3" "$4" "${5:-no-hooks}" <<'PY'
import os
import pty
import select
import signal
import sys
import time
from pathlib import Path

installer, target, answer, output_path = sys.argv[1:5]
mode = sys.argv[5] if len(sys.argv) > 5 else "no-hooks"
pid, master = pty.fork()
if pid == 0:
    args = ["/bin/bash", installer, "--target", target]
    if mode != "hooks":
        args.append("--no-hooks")
    os.execv("/bin/bash", args)

output = bytearray()
sent_answer = False
deadline = time.monotonic() + 60
status = None
while time.monotonic() < deadline:
    ready, _, _ = select.select([master], [], [], 0.1)
    if ready:
        try:
            chunk = os.read(master, 4096)
        except OSError:
            chunk = b""
        output.extend(chunk)
        if not sent_answer and b"[y/N] " in output:
            os.write(master, answer.encode("utf-8") + b"\n")
            sent_answer = True
    waited, status = os.waitpid(pid, os.WNOHANG)
    if waited:
        break
else:
    os.kill(pid, signal.SIGTERM)
    _, status = os.waitpid(pid, 0)
    Path(output_path).write_bytes(output)
    raise SystemExit("installer did not finish within 60 seconds")

# The child may exit while its final diagnostics are still queued on the PTY.
last_output = time.monotonic()
drain_deadline = last_output + 1
while time.monotonic() < drain_deadline:
    ready, _, _ = select.select([master], [], [], 0.1)
    if not ready:
        if time.monotonic() - last_output >= 0.2:
            break
        continue
    try:
        chunk = os.read(master, 4096)
    except OSError:
        break
    if not chunk:
        break
    output.extend(chunk)
    last_output = time.monotonic()

Path(output_path).write_bytes(output)
raise SystemExit(os.waitstatus_to_exitcode(status))
PY
}

# A pre-existing dedicated framework entry is framework-owned: unknown or customized
# content is refused before any target write, regardless of hook mode.
conflict_target="$TMP_DIR/conflict-target"
init_git_target "$conflict_target"
mkdir -p "$conflict_target/.agent-maintenance"
printf '#!/usr/bin/env bash\necho user-owned\n' > "$conflict_target/.agent-maintenance/flywheel-install.sh"
cp "$conflict_target/.agent-maintenance/flywheel-install.sh" "$TMP_DIR/original-flywheel-install.sh"
for conflict_mode in hooks no-hooks; do
  conflict_args=()
  [[ "$conflict_mode" == no-hooks ]] && conflict_args+=(--no-hooks)
  conflict_log="$TMP_DIR/conflict-$conflict_mode.log"
  if bash "$REPO_ROOT/install.sh" --target "$conflict_target" "${conflict_args[@]}" > "$conflict_log" 2>&1; then
    fail "installer accepted a customized framework entry ($conflict_mode)"
  fi
  grep -Fq 'unknown or customized framework file refused: .agent-maintenance/flywheel-install.sh' "$conflict_log" \
    || fail "customized framework refusal was not clear ($conflict_mode)"
  cmp -s "$TMP_DIR/original-flywheel-install.sh" "$conflict_target/.agent-maintenance/flywheel-install.sh" \
    || fail "installer changed the user-owned framework entry ($conflict_mode)"
  [[ ! -e "$conflict_target/AGENTS.md" ]] || fail "installer wrote files before framework refusal ($conflict_mode)"
done

# A valid pre-existing provider config that needs a hook merge is preserved;
# only that provider is skipped while the other provider is still installed.
existing_config_target="$TMP_DIR/existing-config-target"
init_git_target "$existing_config_target"
mkdir -p "$existing_config_target/.codex"
printf '{"hooks":{}}\n' > "$existing_config_target/.codex/hooks.json"
cp "$existing_config_target/.codex/hooks.json" "$TMP_DIR/existing-config-before.json"
bash "$REPO_ROOT/install.sh" --target "$existing_config_target" > "$TMP_DIR/existing-config.log" 2>&1
cmp -s "$TMP_DIR/existing-config-before.json" "$existing_config_target/.codex/hooks.json" \
  || fail "manual-merge config conflict changed the existing settings"
[[ -f "$existing_config_target/AGENTS.md" && -f "$existing_config_target/prompts/bootstrap-project.md" ]] \
  || fail "a manual hook merge conflict blocked independent template files"
[[ -f "$existing_config_target/.claude/settings.json" ]] \
  || fail "Codex hook conflict incorrectly skipped Claude hook registration"
python3 - "$existing_config_target/.claude/settings.json" <<'PY'
import json
import sys
from pathlib import Path
config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
handlers = [handler for group in config["hooks"]["SessionStart"] for handler in group["hooks"]]
assert any(handler.get("type") == "command" and handler.get("command") == 'bash "${CLAUDE_PROJECT_DIR}/.agent-maintenance/session-start.sh" claude' for handler in handlers)
PY
grep -Eq 'startup-hook registration is skipped|hook registration|UNCHANGED|Codex' "$TMP_DIR/existing-config.log" \
  || fail "existing Codex settings handling was not reported"

# Some macOS Python builds do not expose os.listxattr. A valid existing provider
# conflict must still skip only that hook instead of aborting the full install.
no_listxattr_bin="$TMP_DIR/no-listxattr-bin"
mkdir -p "$no_listxattr_bin"
real_python3="$(command -v python3)"
cat > "$no_listxattr_bin/python3" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-" ]]; then
  shift
  exec "$SELF_EVOLUTION_REAL_PYTHON3" -c 'import os,sys; hasattr(os,"listxattr") and delattr(os,"listxattr"); sys.argv[0]="-"; exec(compile(sys.stdin.buffer.read(), "<stdin>", "exec"))' "$@"
fi
exec "$SELF_EVOLUTION_REAL_PYTHON3" -c 'import os,runpy,sys; hasattr(os,"listxattr") and delattr(os,"listxattr"); script=sys.argv.pop(1); sys.argv[0]=script; runpy.run_path(script, run_name="__main__")' "$@"
SH
chmod +x "$no_listxattr_bin/python3"
no_listxattr_target="$TMP_DIR/no-listxattr-target"
init_git_target "$no_listxattr_target"
mkdir -p "$no_listxattr_target/.codex"
printf '{"hooks":{}}\n' > "$no_listxattr_target/.codex/hooks.json"
cp "$no_listxattr_target/.codex/hooks.json" "$TMP_DIR/no-listxattr-before.json"
PATH="$no_listxattr_bin:$PATH" SELF_EVOLUTION_REAL_PYTHON3="$real_python3" \
  bash "$REPO_ROOT/install.sh" --target "$no_listxattr_target" \
  > "$TMP_DIR/no-listxattr.log" 2>&1 \
  || fail "installer aborted when Python lacked os.listxattr"
cmp -s "$TMP_DIR/no-listxattr-before.json" "$no_listxattr_target/.codex/hooks.json" \
  || fail "missing os.listxattr fallback changed existing Codex settings"
[[ -f "$no_listxattr_target/.claude/settings.json" ]] \
  || fail "missing os.listxattr blocked independent Claude hook registration"
[[ -f "$no_listxattr_target/.agent-maintenance/installer-transaction.py" ]] \
  || fail "missing os.listxattr blocked transactional template publication"
grep -Eq 'hook registration|UNCHANGED|Codex' "$TMP_DIR/no-listxattr.log" \
  || fail "missing os.listxattr fallback did not report Codex handling"

# Invalid settings in the later provider are rejected before any template or
# earlier-provider file is written.
invalid_claude_target="$TMP_DIR/invalid-claude-target"
init_git_target "$invalid_claude_target"
mkdir -p "$invalid_claude_target/.codex" "$invalid_claude_target/.claude"
printf '%s\n' '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"bash \"$(git rev-parse --show-toplevel)/.agent-maintenance/session-start.sh\" codex"}]}]}}' \
  > "$invalid_claude_target/.codex/hooks.json"
printf '{invalid json\n' > "$invalid_claude_target/.claude/settings.json"
cp "$invalid_claude_target/.codex/hooks.json" "$TMP_DIR/codex-before-invalid-claude.json"
cp "$invalid_claude_target/.claude/settings.json" "$TMP_DIR/claude-before-invalid.json"
if bash "$REPO_ROOT/install.sh" --target "$invalid_claude_target" > "$TMP_DIR/invalid-claude.log" 2>&1; then
  fail "installer accepted invalid Claude settings"
fi
[[ ! -e "$invalid_claude_target/AGENTS.md" ]] || fail "invalid later-provider settings still installed template files"
cmp -s "$TMP_DIR/codex-before-invalid-claude.json" "$invalid_claude_target/.codex/hooks.json" \
  || fail "invalid Claude settings caused an earlier Codex settings write"
cmp -s "$TMP_DIR/claude-before-invalid.json" "$invalid_claude_target/.claude/settings.json" \
  || fail "installer changed invalid Claude settings"
grep -Fq 'no files were installed' "$TMP_DIR/invalid-claude.log" \
  || fail "cross-provider preflight failure was not reported"

# Fail the transaction journal's atomic publication before any project write.
journal_failure_modules="$TMP_DIR/journal-failure-modules"
mkdir -p "$journal_failure_modules"
cat > "$journal_failure_modules/sitecustomize.py" <<'PY'
import os

_replace = os.replace
def _fail_journal(src, dst, *args, **kwargs):
    if os.path.basename(os.fspath(dst)) == "transaction.json":
        raise OSError("injected transaction journal failure")
    return _replace(src, dst, *args, **kwargs)
os.replace = _fail_journal
PY
journal_failure_target="$TMP_DIR/journal-failure-target"
init_git_target "$journal_failure_target"
if PYTHONPATH="$journal_failure_modules" bash "$REPO_ROOT/install.sh" --target "$journal_failure_target" > "$TMP_DIR/journal-failure.log" 2>&1; then
  fail "installer succeeded without a transaction journal"
fi
grep -Fq 'injected transaction journal failure' "$TMP_DIR/journal-failure.log" \
  || fail "transaction journal failure was not visible"
[[ ! -e "$journal_failure_target/AGENTS.md" && ! -e "$journal_failure_target/.codex/hooks.json" \
    && ! -e "$journal_failure_target/.claude/settings.json" ]] \
  || fail "transaction journal failure occurred after project files were written"

journal_update_modules="$TMP_DIR/journal-update-modules"
mkdir -p "$journal_update_modules"
cat > "$journal_update_modules/sitecustomize.py" <<'PY'
import json
import os

_replace = os.replace
def _fail_directory_journal_update(src, dst, *args, **kwargs):
    if os.path.basename(os.fspath(dst)) == "transaction.json":
        # Target the original scenario, not the incidental number of writes:
        # publishing now durably records its exact post-link/unlink ctime.
        with open(src, encoding="utf-8") as handle:
            state = json.load(handle)
        if os.environ["JOURNAL_FAIL_DIRECTORY"] in state["created_directories"]:
            raise OSError("injected transaction journal update failure")
    return _replace(src, dst, *args, **kwargs)
os.replace = _fail_directory_journal_update
PY
journal_update_target="$TMP_DIR/journal-update-target"
init_git_target "$journal_update_target"
if PYTHONPATH="$journal_update_modules" JOURNAL_FAIL_DIRECTORY=.agent-maintenance \
    bash "$REPO_ROOT/install.sh" --target "$journal_update_target" > "$TMP_DIR/journal-update.log" 2>&1; then
  fail "installer succeeded after a transaction journal update failure"
fi
grep -Fq 'injected transaction journal update failure' "$TMP_DIR/journal-update.log" \
  || fail "mid-transaction journal failure was not visible"
[[ -d "$journal_update_target/.agent-maintenance" ]] \
  || fail "journal update rollback did not conservatively preserve its created directory"
grep -Fq 'PRESERVED directory created before transaction journal failure' "$TMP_DIR/journal-update.log" \
  || fail "directory preserved after journal failure was not reported"
[[ ! -e "$journal_update_target/AGENTS.md" && ! -e "$journal_update_target/CLAUDE.md" \
    && ! -e "$journal_update_target/LESSONS.md" ]] \
  || fail "journal update failure left earlier template files at their active paths"
grep -Fq 'rollback was incomplete' "$TMP_DIR/journal-update.log" \
  || fail "post-journal-failure recovery status was not made visible"
recovery_dir="$(sed -n 's/^ERROR: rollback snapshots retained for recovery at: //p' "$TMP_DIR/journal-update.log" | tail -n 1)"
[[ -n "$recovery_dir" && -f "$recovery_dir/transaction.json" ]] \
  || fail "post-journal-failure transaction log was not retained"

# Fail the observable AGENTS baseline snapshot output; preserve AGENTS.md while
# allowing unrelated template files to install and report the conflict.
snapshot_failure_modules="$TMP_DIR/snapshot-failure-modules"
mkdir -p "$snapshot_failure_modules"
cat > "$snapshot_failure_modules/sitecustomize.py" <<'PY'
import os

_open = os.open
def _fail_agents_snapshot(file, flags, *args, **kwargs):
    try:
        path = os.fspath(file)
    except TypeError:
        path = ""
    if os.path.basename(path).startswith("agents.original.tmp-") and flags & os.O_CREAT:
        raise OSError("injected AGENTS baseline snapshot failure")
    return _open(file, flags, *args, **kwargs)
os.open = _fail_agents_snapshot
PY
snapshot_failure_target="$TMP_DIR/snapshot-failure-target"
mkdir -p "$snapshot_failure_target"
printf '<!-- BEGIN SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->\n# Preserve during snapshot failure\n<!-- END SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->\n' > "$snapshot_failure_target/AGENTS.md"
cp "$snapshot_failure_target/AGENTS.md" "$TMP_DIR/snapshot-failure-before.md"
if PYTHONPATH="$snapshot_failure_modules" bash "$REPO_ROOT/install.sh" --target "$snapshot_failure_target" --no-hooks > "$TMP_DIR/snapshot-failure.log" 2>&1; then
  fail "AGENTS snapshot failure unexpectedly reported complete installation"
else
  snapshot_status=$?
  [[ "$snapshot_status" -ne 0 ]] || fail "AGENTS snapshot failure unexpectedly returned success"
fi
cmp -s "$TMP_DIR/snapshot-failure-before.md" "$snapshot_failure_target/AGENTS.md" \
  || fail "AGENTS snapshot failure changed the existing file"
grep -Fq 'injected AGENTS baseline snapshot failure' "$TMP_DIR/snapshot-failure.log" || fail "AGENTS snapshot fault was not reached"
[[ ! -e "$snapshot_failure_target/CLAUDE.md" ]] \
  || fail "AGENTS snapshot failure wrote unrelated files before refusal"

# A read-only later-provider directory must be rejected before any project file
# or earlier-provider setting is written. Run as an unprivileged user even when
# the suite itself is invoked as root so the permission state matches real use.
readonly_claude_target="$TMP_DIR/readonly-claude-target"
init_git_target "$readonly_claude_target"
mkdir -p "$readonly_claude_target/.codex" "$readonly_claude_target/.claude"
chmod 555 "$readonly_claude_target/.claude"
python3 - "$REPO_ROOT/install.sh" "$readonly_claude_target" "$TMP_DIR/readonly-claude.log" "$TMP_DIR" <<'PY'
import os
import subprocess
import sys
from pathlib import Path

installer, target, output_path, temp_root = sys.argv[1:]
if os.geteuid() == 0:
    os.chmod(temp_root, 0o755)
    for path in Path(target).rglob("*"):
        os.chown(path, 65534, 65534, follow_symlinks=False)
    os.chown(target, 65534, 65534)

def drop_privileges():
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)

with Path(output_path).open("wb") as output:
    result = subprocess.run(
        ["bash", installer, "--target", target],
        stdout=output,
        stderr=subprocess.STDOUT,
        preexec_fn=drop_privileges if os.geteuid() == 0 else None,
    )
if result.returncode == 0:
    raise SystemExit("installer accepted a read-only Claude settings directory")
PY
chmod 755 "$readonly_claude_target/.claude"
[[ ! -e "$readonly_claude_target/AGENTS.md" ]] \
  || fail "read-only Claude target left template files installed"
[[ ! -e "$readonly_claude_target/.codex/hooks.json" && ! -e "$readonly_claude_target/.claude/settings.json" ]] \
  || fail "read-only Claude preflight created provider settings"
grep -Fq 'no files were installed' "$TMP_DIR/readonly-claude.log" \
  || fail "read-only Claude preflight failure was not reported before installation"

# Simulate a permission change after preflight but before Claude settings can be
# staged. Earlier template and Codex writes must be rolled back.
rollback_target="$TMP_DIR/rollback-target"
init_git_target "$rollback_target"
mkdir -p "$rollback_target/.codex" "$rollback_target/.claude"
rollback_python_bin="$TMP_DIR/rollback-python-bin"
mkdir -p "$rollback_python_bin"
real_python3="$(command -v python3)"
cat > "$rollback_python_bin/python3" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ -n "${TEST_INJECT_MODE:-}" && -e "$TEST_CODEX" && ! -e "$TEST_INJECT_MARKER" ]]; then
  touch "$TEST_INJECT_MARKER"
  if [[ "$TEST_INJECT_MODE" == "concurrent" ]]; then
    cat "$TEST_CODEX_EXPECTED" > "$TEST_CODEX"
    cat "$TEST_AGENTS_EXPECTED" > "$TEST_AGENTS"
  fi
  chmod 555 "$TEST_CLAUDE_DIR"
fi
exec "$TEST_REAL_PYTHON3" "$@"
SH
chmod +x "$rollback_python_bin/python3"
python3 - "$REPO_ROOT/install.sh" "$rollback_target" "$TMP_DIR/rollback.log" "$TMP_DIR" "$rollback_python_bin" "$real_python3" <<'PY'
import os
import subprocess
import sys
from pathlib import Path

installer, target, output_path, temp_root, mock_bin, real_python = sys.argv[1:]
if os.geteuid() == 0:
    os.chmod(temp_root, 0o755)
    for root in (Path(target), Path(mock_bin)):
        for path in root.rglob("*"):
            os.chown(path, 65534, 65534, follow_symlinks=False)
        os.chown(root, 65534, 65534)

def drop_privileges():
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)

environment = os.environ.copy()
environment["PATH"] = mock_bin + os.pathsep + environment["PATH"]
environment["TEST_CLAUDE_DIR"] = str(Path(target) / ".claude")
environment["TEST_CODEX"] = str(Path(target) / ".codex/hooks.json")
environment["TEST_INJECT_MARKER"] = str(Path(target) / ".test-injected")
environment["TEST_REAL_PYTHON3"] = real_python
environment["TEST_INJECT_MODE"] = "rollback"
with Path(output_path).open("wb") as output:
    result = subprocess.run(
        ["bash", installer, "--target", target],
        stdout=output,
        stderr=subprocess.STDOUT,
        env=environment,
        preexec_fn=drop_privileges if os.geteuid() == 0 else None,
    )
if result.returncode == 0:
    raise SystemExit("injected Claude settings write failure unexpectedly succeeded")
PY
chmod 755 "$rollback_target/.claude"
[[ ! -e "$rollback_target/AGENTS.md" && ! -e "$rollback_target/prompts/bootstrap-project.md" ]] \
  || fail "mid-install Claude write failure left template files behind"
[[ ! -e "$rollback_target/.codex/hooks.json" && ! -e "$rollback_target/.claude/settings.json" ]] \
  || fail "mid-install Claude failure left provider settings at their active paths"
grep -Fq 'attempting to roll back this install' "$TMP_DIR/rollback.log" \
  || fail "mid-install write failure did not report rollback"
grep -Fq 'rollback was incomplete' "$TMP_DIR/rollback.log" \
  || fail "rollback did not disclose preserved recovery entries"
recovery_dir="$(sed -n 's/^ERROR: rollback snapshots retained for recovery at: //p' "$TMP_DIR/rollback.log" | tail -n 1)"
[[ -n "$recovery_dir" && -f "$recovery_dir/transaction.json" ]] \
  || fail "conservative rollback did not retain its recovery journal"
compgen -G "$rollback_target/.codex/.hooks.json.rollback-*" >/dev/null \
  || fail "rollback deleted the uncertain Codex inode instead of preserving recovery data"

# If another writer changes a file after this install wrote it, rollback must
# preserve that writer's version, report incomplete recovery, and retain backups.
concurrent_target="$TMP_DIR/concurrent-target"
init_git_target "$concurrent_target"
mkdir -p "$concurrent_target/.codex" "$concurrent_target/.claude"
concurrent_expected="$TMP_DIR/concurrent-codex-expected.json"
printf '{"hooks":{},"external":"concurrent writer"}\n' > "$concurrent_expected"
concurrent_agents_expected="$TMP_DIR/concurrent-agents-expected.md"
printf '# External concurrent AGENTS update\n' > "$concurrent_agents_expected"
python3 - "$REPO_ROOT/install.sh" "$concurrent_target" "$TMP_DIR/concurrent.log" "$TMP_DIR" "$rollback_python_bin" "$real_python3" "$concurrent_expected" "$concurrent_agents_expected" <<'PY'
import os
import subprocess
import sys
from pathlib import Path

installer, target, output_path, temp_root, mock_bin, real_python, expected, expected_agents = sys.argv[1:]
if os.geteuid() == 0:
    os.chmod(temp_root, 0o755)
    for root in (Path(target), Path(mock_bin)):
        for path in root.rglob("*"):
            os.chown(path, 65534, 65534, follow_symlinks=False)
        os.chown(root, 65534, 65534)

def drop_privileges():
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)

environment = os.environ.copy()
environment["PATH"] = mock_bin + os.pathsep + environment["PATH"]
environment["TEST_CLAUDE_DIR"] = str(Path(target) / ".claude")
environment["TEST_CODEX"] = str(Path(target) / ".codex/hooks.json")
environment["TEST_INJECT_MARKER"] = str(Path(target) / ".test-injected")
environment["TEST_CODEX_EXPECTED"] = expected
environment["TEST_AGENTS"] = str(Path(target) / "AGENTS.md")
environment["TEST_AGENTS_EXPECTED"] = expected_agents
environment["TEST_REAL_PYTHON3"] = real_python
environment["TEST_INJECT_MODE"] = "concurrent"
with Path(output_path).open("wb") as output:
    result = subprocess.run(
        ["bash", installer, "--target", target],
        stdout=output,
        stderr=subprocess.STDOUT,
        env=environment,
        preexec_fn=drop_privileges if os.geteuid() == 0 else None,
    )
if result.returncode == 0:
    raise SystemExit("concurrent Claude write failure unexpectedly succeeded")
PY
chmod 755 "$concurrent_target/.claude"
cmp -s "$concurrent_expected" "$concurrent_target/.codex/hooks.json" \
  || fail "rollback overwrote a concurrent Codex settings update"
cmp -s "$concurrent_agents_expected" "$concurrent_target/AGENTS.md" \
  || fail "rollback deleted a newly installed file after a concurrent edit"
[[ ! -e "$concurrent_target/.claude/settings.json" ]] \
  || fail "concurrent write failure created Claude settings in an unwritable directory"
grep -Fq 'PRESERVED concurrent change; rollback skipped: .codex/hooks.json' "$TMP_DIR/concurrent.log" \
  || fail "rollback did not report preserving the concurrent Codex update"
grep -Fq 'PRESERVED concurrent change; rollback skipped: AGENTS.md' "$TMP_DIR/concurrent.log" \
  || fail "rollback did not report preserving the concurrently edited new file"
grep -Fq 'rollback was incomplete' "$TMP_DIR/concurrent.log" \
  || fail "concurrent update did not make incomplete rollback explicit"
recovery_dir="$(sed -n 's/^ERROR: rollback snapshots retained for recovery at: //p' "$TMP_DIR/concurrent.log" | tail -n 1)"
[[ -n "$recovery_dir" && -f "$recovery_dir/transaction.json" ]] \
  || fail "incomplete rollback did not retain its recovery journal"

# Replace a validated parent with a symlink exactly as the first nested write is
# about to run. The descriptor-relative writer must refuse it and roll back.
racy_symlink_target="$TMP_DIR/racy-symlink-target"
racy_symlink_outside="$TMP_DIR/racy-symlink-outside"
racy_python_bin="$TMP_DIR/racy-python-bin"
mkdir -p "$racy_symlink_target/docs" "$racy_symlink_outside/automation" "$racy_python_bin"
cat > "$racy_python_bin/python3" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ -e "$TEST_TARGET/AGENTS.md" && -d "$TEST_TARGET/docs" \
    && ! -e "$TEST_MARKER" && ! -L "$TEST_TARGET/docs" ]]; then
  touch "$TEST_MARKER"
  mv "$TEST_TARGET/docs" "$TEST_TARGET/docs.before-race"
  ln -s "$TEST_OUTSIDE" "$TEST_TARGET/docs"
fi
exec "$TEST_REAL_PYTHON3" "$@"
SH
chmod +x "$racy_python_bin/python3"
if PATH="$racy_python_bin:$PATH" TEST_TARGET="$racy_symlink_target" \
    TEST_OUTSIDE="$racy_symlink_outside" TEST_REAL_PYTHON3="$real_python3" \
    TEST_MARKER="$racy_symlink_target/.test-injected" \
    bash "$REPO_ROOT/install.sh" --target "$racy_symlink_target" --no-hooks \
    > "$TMP_DIR/racy-symlink.log" 2>&1; then
  fail "installer accepted a parent replaced by a symlink after preflight"
fi
[[ ! -e "$racy_symlink_outside/automation/runner-contract.md" ]] \
  || fail "post-preflight symlink replacement wrote outside the target"
[[ ! -e "$racy_symlink_target/AGENTS.md" ]] \
  || fail "post-preflight symlink refusal did not roll back earlier template writes"
grep -Fq 'installation write failed; attempting to roll back' "$TMP_DIR/racy-symlink.log" \
  || fail "post-preflight symlink refusal did not run transactional rollback"

# Existing destination parents must be real directories, not symlinks outside the project.
symlink_parent_target="$TMP_DIR/symlink-parent-target"
symlink_parent_outside="$TMP_DIR/symlink-parent-outside"
init_git_target "$symlink_parent_target"
mkdir -p "$symlink_parent_outside"
ln -s "$symlink_parent_outside" "$symlink_parent_target/docs"
if bash "$REPO_ROOT/install.sh" --target "$symlink_parent_target" --no-hooks > "$TMP_DIR/symlink-parent.log" 2>&1; then
  fail "installer accepted a symlinked destination parent"
fi
[[ ! -e "$symlink_parent_outside/project-profile.md" ]] || fail "installer wrote documentation through a symlink outside the target"
[[ ! -e "$symlink_parent_target/AGENTS.md" ]] || fail "symlink-parent rejection occurred after earlier template writes"
grep -Fq 'symbolic-link directory' "$TMP_DIR/symlink-parent.log" \
  || fail "symlink-parent rejection was not clear"

# Hook settings parents are checked too; a symlinked .claude directory must not
# redirect settings writes outside the target repository.
symlink_claude_target="$TMP_DIR/symlink-claude-target"
symlink_claude_outside="$TMP_DIR/symlink-claude-outside"
init_git_target "$symlink_claude_target"
mkdir -p "$symlink_claude_outside"
ln -s "$symlink_claude_outside" "$symlink_claude_target/.claude"
if bash "$REPO_ROOT/install.sh" --target "$symlink_claude_target" > "$TMP_DIR/symlink-claude.log" 2>&1; then
  fail "installer accepted a symlinked .claude directory"
fi
[[ ! -e "$symlink_claude_outside/settings.json" ]] || fail "installer wrote Claude settings through a symlink"
[[ ! -e "$symlink_claude_target/AGENTS.md" ]] || fail "symlinked .claude rejection occurred after template writes"

# A provider settings symlink is refused rather than replacing the link or touching its target.
settings_link_target="$TMP_DIR/settings-link-target"
settings_link_outside="$TMP_DIR/settings-link-outside.json"
init_git_target "$settings_link_target"
mkdir -p "$settings_link_target/.claude"
printf '{"hooks":{}}\n' > "$settings_link_outside"
cp "$settings_link_outside" "$TMP_DIR/settings-link-outside-before.json"
ln -s "$settings_link_outside" "$settings_link_target/.claude/settings.json"
if bash "$REPO_ROOT/install.sh" --target "$settings_link_target" > "$TMP_DIR/settings-link.log" 2>&1; then
  fail "installer accepted a symlinked Claude settings file"
fi
[[ -L "$settings_link_target/.claude/settings.json" ]] || fail "installer replaced the Claude settings symlink"
cmp -s "$TMP_DIR/settings-link-outside-before.json" "$settings_link_outside" \
  || fail "installer changed the symlink target"
[[ ! -e "$settings_link_target/AGENTS.md" ]] || fail "settings symlink rejection occurred after template writes"
grep -Fq 'settings file is a symbolic link' "$TMP_DIR/settings-link.log" \
  || fail "settings symlink rejection was not clear"

# Existing hook JSON that would need modification is preserved without blocking
# template files; content, ownership, mode, mtime, ctime and xattrs stay intact.
# Read-only preflight/proposals may naturally update atime, even without a TTY.
hook_metadata_target="$TMP_DIR/hook-metadata-target"
init_git_target "$hook_metadata_target"
mkdir -p "$hook_metadata_target/.claude"
printf '%s\n' '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"bash .agent-maintenance/session-start.sh custom"}]}]}}' > "$hook_metadata_target/.claude/settings.json"
cp "$hook_metadata_target/.claude/settings.json" "$TMP_DIR/hook-settings-original.json"
python3 - "$hook_metadata_target/.claude/settings.json" "$TMP_DIR/hook-metadata-expected.json" <<'PY'
import errno
import json
import os
import stat
import struct
import sys
from pathlib import Path

filename, expected_path = map(Path, sys.argv[1:])
os.chmod(filename, 0o640)
if os.geteuid() == 0:
    os.chown(filename, 65534, 65534)
xattr_supported = False
try:
    os.setxattr(filename, "user.self_evolution_hook_test", b"preserve-me")
    xattr_supported = True
except OSError as exc:
    if exc.errno not in {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}:
        raise
named_user = 12345 if os.stat(filename).st_uid != 12345 else 12346
acl = (
    struct.pack("<I", 2)
    + struct.pack("<HHI", 0x01, 0x06, 0xFFFFFFFF)
    + struct.pack("<HHI", 0x02, 0x04, named_user)
    + struct.pack("<HHI", 0x04, 0x04, 0xFFFFFFFF)
    + struct.pack("<HHI", 0x10, 0x04, 0xFFFFFFFF)
    + struct.pack("<HHI", 0x20, 0x00, 0xFFFFFFFF)
)
try:
    os.setxattr(filename, "system.posix_acl_access", acl)
except OSError as exc:
    if exc.errno not in {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}:
        raise
initial = os.stat(filename)
os.utime(filename, ns=(max(0, initial.st_mtime_ns - 172800 * 1_000_000_000), initial.st_mtime_ns))
info = os.stat(filename)
attrs = {name: os.getxattr(filename, name) for name in os.listxattr(filename)}
expected_path.write_text(json.dumps({
    "uid": info.st_uid,
    "gid": info.st_gid,
    "mode": stat.S_IMODE(info.st_mode),
    "atime_ns": info.st_atime_ns,
    "mtime_ns": info.st_mtime_ns,
    "ctime_ns": info.st_ctime_ns,
    "attrs": {name: value.hex() for name, value in attrs.items()},
    "xattr_supported": xattr_supported,
}), encoding="utf-8")
PY
bash "$REPO_ROOT/install.sh" --target "$hook_metadata_target" > "$TMP_DIR/hook-metadata.log" 2>&1 \
  || fail "existing settings conflict prevented safe file-only installation"
[[ -f "$hook_metadata_target/AGENTS.md" ]] \
  || fail "existing settings conflict blocked independent template files"
[[ -f "$hook_metadata_target/.codex/hooks.json" ]] \
  || fail "Claude settings conflict incorrectly skipped Codex hook registration"
python3 - "$hook_metadata_target/.codex/hooks.json" <<'PY'
import json
import sys
from pathlib import Path
config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
handlers = [handler for group in config["hooks"]["SessionStart"] for handler in group["hooks"]]
assert any(handler.get("type") == "command" and handler.get("command") == 'bash "$(git rev-parse --show-toplevel)/.agent-maintenance/session-start.sh" codex' for handler in handlers)
PY
grep -Eq 'hook registration|UNCHANGED|Claude' "$TMP_DIR/hook-metadata.log" \
  || fail "existing settings handling did not explain hook registration"
python3 - "$hook_metadata_target/.claude/settings.json" "$TMP_DIR/hook-metadata-expected.json" "$TMP_DIR/hook-settings-original.json" <<'PY'
import json
import os
import stat
import sys
from pathlib import Path

filename, expected_path, original_path = map(Path, sys.argv[1:])
expected = json.loads(expected_path.read_text(encoding="utf-8"))
info = os.stat(filename)
fd = os.open(filename, os.O_RDONLY | getattr(os, "O_NOATIME", 0))
try:
    actual_bytes = os.read(fd, 1024 * 1024)
finally:
    os.close(fd)
assert actual_bytes == original_path.read_bytes(), "safe refusal changed existing settings bytes"
assert (info.st_uid, info.st_gid) == (expected["uid"], expected["gid"]), "hook settings owner or group changed"
assert stat.S_IMODE(info.st_mode) == expected["mode"], "hook settings permissions changed"
# atime is the sole permitted read side effect; it is not restored or compared.
assert info.st_mtime_ns == expected["mtime_ns"], "hook settings mtime changed"
assert info.st_ctime_ns == expected["ctime_ns"], "hook settings ctime changed"
actual = {name: os.getxattr(filename, name).hex() for name in os.listxattr(filename)}
assert actual == expected["attrs"], "hook settings extended attributes or ACL changed"
PY

# The session-start hook only reminds, validates the completion marker and core
# documents, and honors CLAUDE_PROJECT_DIR for Claude Code.
session_target="$TMP_DIR/session-start-target"
session_claude_target="$TMP_DIR/session-start-claude-target"
init_git_target "$session_target"
init_git_target "$session_claude_target"
mkdir -p "$session_target/prompts" "$session_target/.agent-maintenance" "$session_claude_target/.agent-maintenance"
cp "$REPO_ROOT/prompts/bootstrap-project.md" "$session_target/prompts/bootstrap-project.md"
for root in "$session_target" "$session_claude_target"; do
  mkdir -p "$root/docs/subsystems"
  for relative in AGENTS.md LESSONS.md docs/project-profile.md docs/code-standards.md docs/contributors.md docs/subsystems/README.md; do
    printf '# fixture\n' > "$root/$relative"
  done
done
if ! (cd "$session_target" && bash "$REPO_ROOT/.agent-maintenance/session-start.sh") > "$TMP_DIR/session-unmarked.log" 2> "$TMP_DIR/session-unmarked.stderr"; then
  fail "session-start failed for an unmarked project"
fi
[[ ! -s "$TMP_DIR/session-unmarked.stderr" ]] || { cat "$TMP_DIR/session-unmarked.stderr" >&2; fail "normal bootstrap reminder was written to stderr"; }
grep -Fq 'bootstrap reminder' "$TMP_DIR/session-unmarked.log" \
  || fail "session-start did not emit a bootstrap reminder for an unmarked project"
grep -Fq 'must not preempt or interrupt' "$TMP_DIR/session-unmarked.log" \
  || fail "session-start reminder could preempt the user's task"
grep -Fq './.agent-maintenance/flywheel-install.sh --bootstrap codex' "$TMP_DIR/session-unmarked.log" \
  || fail "session-start did not point to explicit bootstrap"
[[ ! -e "$session_target/.agent-maintenance/bootstrap-complete" ]] \
  || fail "session-start created a completion marker"
printf 'complete\n' > "$session_target/.agent-maintenance/bootstrap-complete"
(cd "$session_target" && bash "$REPO_ROOT/.agent-maintenance/session-start.sh") > "$TMP_DIR/session-legacy-marker.log" 2>&1
grep -Fq 'completion marker is missing a valid Reviewed HEAD SHA' "$TMP_DIR/session-legacy-marker.log" \
  || fail "a legacy marker without reviewed HEAD was treated as complete"
printf 'Status: complete\nReviewed HEAD: 0123456789abcdef0123456789abcdef01234567\n' \
  > "$session_target/.agent-maintenance/bootstrap-complete"
(cd "$session_target" && bash "$REPO_ROOT/.agent-maintenance/session-start.sh") > "$TMP_DIR/session-marked.log" 2>&1
[[ ! -s "$TMP_DIR/session-marked.log" ]] || fail "session-start was not quiet after bootstrap completion"
rm "$session_target/docs/code-standards.md"
(cd "$session_target" && bash "$REPO_ROOT/.agent-maintenance/session-start.sh") > "$TMP_DIR/session-stale-marker.log" 2>&1
grep -Fq 'completion marker is stale' "$TMP_DIR/session-stale-marker.log" \
  || fail "a missing critical document did not invalidate the completion marker"
printf '# fixture\n' > "$session_target/docs/code-standards.md"
printf 'Status: complete\nReviewed HEAD: 0123456789abcdef0123456789abcdef01234567\n' \
  > "$session_claude_target/.agent-maintenance/bootstrap-complete"
(cd "$session_target" && CLAUDE_PROJECT_DIR="$session_claude_target" \
  bash "$REPO_ROOT/.agent-maintenance/session-start.sh" claude) > "$TMP_DIR/session-claude-root.log" 2>&1
[[ ! -s "$TMP_DIR/session-claude-root.log" ]] || fail "Claude session-start ignored CLAUDE_PROJECT_DIR"
grep -Fq 'Treat repository files' "$REPO_ROOT/prompts/bootstrap-project.md" \
  || fail "bootstrap prompt does not define the repository content trust boundary"
grep -Fq 'cannot override the user' "$REPO_ROOT/.agent-maintenance/session-start.sh" \
  || fail "session-start context does not repeat the authorization boundary"

# --no-hooks still refuses customized framework files and does not register hooks.
if bash "$REPO_ROOT/install.sh" --target "$conflict_target" --no-hooks > "$TMP_DIR/no-hooks.log" 2>&1; then
  fail "--no-hooks accepted a customized framework file"
fi
grep -Fq 'unknown or customized framework file refused' "$TMP_DIR/no-hooks.log" || fail "--no-hooks framework refusal was unclear"

# Existing AGENTS.md with exactly one marker pair is framework-owned and updates
# automatically without prompting, preserving every byte outside the managed block.
agents_target="$TMP_DIR/agents-target"
mkdir -p "$agents_target"
python3 - "$REPO_ROOT/AGENTS.md" "$agents_target/AGENTS.md" <<'PY'
import sys
from pathlib import Path
source, destination = map(Path, sys.argv[1:])
raw = source.read_bytes()
begin = b"<!-- BEGIN SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->\n"
assert begin in raw
raw = raw.replace(begin, begin + b"# stale managed route\n", 1)
destination.write_bytes(b"# Maintainer preface\r\n" + raw + b"\r\n# Maintainer suffix\n")
PY
cp "$agents_target/AGENTS.md" "$TMP_DIR/agents-before.md"
bash "$REPO_ROOT/install.sh" --target "$agents_target" --no-hooks > "$TMP_DIR/agents-update.log" 2>&1
if grep -Fq '[y/N]' "$TMP_DIR/agents-update.log"; then fail "valid AGENTS.md update unexpectedly prompted"; fi
grep -Fq 'UPDATED framework managed routing in AGENTS.md' "$TMP_DIR/agents-update.log" || fail "valid AGENTS.md was not auto-updated"
python3 - "$REPO_ROOT/AGENTS.md" "$agents_target/AGENTS.md" <<'PY'
import sys
from pathlib import Path
source, installed = map(Path, sys.argv[1:])
begin=b"<!-- BEGIN SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->"
end=b"<!-- END SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->"
a=source.read_bytes(); b=installed.read_bytes()
assert b.startswith(b"# Maintainer preface\r\n") and b.endswith(b"\r\n# Maintainer suffix\n")
assert b.count(begin)==1 and b.count(end)==1
assert b[b.index(begin):b.index(end)+len(end)] == a[a.index(begin):a.index(end)+len(end)]
PY

# Malformed AGENTS markers are refused before writes and never prompt.
malformed_agents_target="$TMP_DIR/malformed-agents-target"
mkdir -p "$malformed_agents_target"
printf '<!-- BEGIN SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->\n# malformed\n' > "$malformed_agents_target/AGENTS.md"
cp "$malformed_agents_target/AGENTS.md" "$TMP_DIR/malformed-agents-before.md"
if bash "$REPO_ROOT/install.sh" --target "$malformed_agents_target" --no-hooks > "$TMP_DIR/malformed-agents.log" 2>&1; then fail "malformed AGENTS.md was accepted"; fi
! grep -Fq '[y/N]' "$TMP_DIR/malformed-agents.log" || fail "malformed AGENTS.md prompted"
cmp -s "$TMP_DIR/malformed-agents-before.md" "$malformed_agents_target/AGENTS.md" || fail "malformed AGENTS.md changed"
[[ ! -e "$malformed_agents_target/CLAUDE.md" ]] || fail "malformed AGENTS.md wrote unrelated files"

# Claude unknown ownership is the only interactive confirmation path; decline
# preserves all existing hooks and reports an exact diff.
claude_ambiguous_target="$TMP_DIR/claude-ambiguous-target"
init_git_target "$claude_ambiguous_target"
mkdir -p "$claude_ambiguous_target/.claude"
printf '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"bash .agent-maintenance/flywheel-custom.sh"}]}]}}\n' > "$claude_ambiguous_target/.claude/settings.json"
cp "$claude_ambiguous_target/.claude/settings.json" "$TMP_DIR/claude-ambiguous-before.json"
run_installer_in_pty "$REPO_ROOT/install.sh" "$claude_ambiguous_target" n "$TMP_DIR/claude-ambiguous.log" hooks
grep -Fq '[y/N]' "$TMP_DIR/claude-ambiguous.log" || fail "ambiguous Claude hook did not prompt"
grep -Fq -- '--- .claude/settings.json (current)' "$TMP_DIR/claude-ambiguous.log" || fail "ambiguous Claude diff was not displayed"
cmp -s "$TMP_DIR/claude-ambiguous-before.json" "$claude_ambiguous_target/.claude/settings.json" || fail "declined Claude ambiguity removed a hook"

# Hard-linked AGENTS files are never detached or replaced, even with valid markers.
agents_hardlink_target="$TMP_DIR/agents-hardlink-target"
mkdir -p "$agents_hardlink_target"
cp "$REPO_ROOT/AGENTS.md" "$agents_hardlink_target/AGENTS.md"
ln "$agents_hardlink_target/AGENTS.md" "$agents_hardlink_target/AGENTS.md.shared"
cp "$agents_hardlink_target/AGENTS.md" "$TMP_DIR/agents-hardlink-before.md"
if bash "$REPO_ROOT/install.sh" --target "$agents_hardlink_target" --no-hooks > "$TMP_DIR/agents-hardlink.log" 2>&1; then
  fail "hard-linked AGENTS.md was accepted"
fi
cmp -s "$TMP_DIR/agents-hardlink-before.md" "$agents_hardlink_target/AGENTS.md" || fail "hard-linked AGENTS.md changed"
python3 - "$agents_hardlink_target/AGENTS.md" "$agents_hardlink_target/AGENTS.md.shared" <<'PYHL'
import os, sys
assert os.path.samefile(*sys.argv[1:]), "AGENTS.md hard-link relationship was broken"
assert os.stat(sys.argv[1]).st_nlink == 2
PYHL
! grep -Fq '[y/N]' "$TMP_DIR/agents-hardlink.log" || fail "unsafe hardlink prompted"

# Automatic managed-block diffs must escape all untrusted terminal controls.
agents_control_target="$TMP_DIR/agents-control-target"
mkdir -p "$agents_control_target"
python3 - "$agents_control_target/AGENTS.md" <<'PYCTRL'
import sys
from pathlib import Path
Path(sys.argv[1]).write_bytes(
    b"<!-- BEGIN SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->\n"
    b"unsafe: \x1b[2J CSI \x1b]0;title-test\x07 C1: \xc2\x9b\n"
    b"<!-- END SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->\n"
    b"# Keep outside byte-for-byte\r\n"
)
PYCTRL
bash "$REPO_ROOT/install.sh" --target "$agents_control_target" --no-hooks > "$TMP_DIR/agents-control.log" 2>&1
python3 - "$TMP_DIR/agents-control.log" "$agents_control_target/AGENTS.md" <<'PYESC'
import sys
from pathlib import Path
output = Path(sys.argv[1]).read_bytes()
for control in (b"\x1b", b"\x07", b"\xc2\x9b"):
    assert control not in output, f"raw terminal control emitted: {control!r}"
for visible in (b"\\x1b[2J", b"\\x1b]0;title-test", b"\\x07", b"\\x9b"):
    assert visible in output, f"escaped control missing: {visible!r}"
assert Path(sys.argv[2]).read_bytes().endswith(b"# Keep outside byte-for-byte\r\n")
assert b"[y/N]" not in output
PYESC

# A new template's AGENTS block, not a duplicated shell literal, is authoritative.
updated_source="$TMP_DIR/updated-source"
mkdir -p "$updated_source"
tar -cf - --exclude='./.git' --exclude='__pycache__' -C "$REPO_ROOT" . | tar -xf - -C "$updated_source"
python3 - "$updated_source/AGENTS.md" <<'PYROUTE'
import sys
from pathlib import Path
path = Path(sys.argv[1])
path.write_bytes(path.read_bytes().replace(b"# Agent Work Entry Point", b"# Versioned Agent Work Entry Point", 1))
PYROUTE
bash "$updated_source/install.sh" --target "$agents_target" --no-hooks > "$TMP_DIR/agents-version-update.log" 2>&1
grep -Fq '# Versioned Agent Work Entry Point' "$agents_target/AGENTS.md" || fail "template routing version was not installed"
grep -Fq '# Maintainer preface' "$agents_target/AGENTS.md" || fail "routing upgrade lost user prefix"
grep -Fq '# Maintainer suffix' "$agents_target/AGENTS.md" || fail "routing upgrade lost user suffix"
! grep -Fq '[y/N]' "$TMP_DIR/agents-version-update.log" || fail "known routing upgrade prompted"

# Hook-enabled installation rejects non-Git targets; --no-hooks can install there.
non_git_target="$TMP_DIR/non-git-target"
mkdir -p "$non_git_target"
if bash "$REPO_ROOT/install.sh" --target "$non_git_target" > "$TMP_DIR/non-git.log" 2>&1; then
  fail "installer accepted a non-Git target with hooks enabled"
fi
grep -Fq 'project hooks require a Git repository root' "$TMP_DIR/non-git.log" \
  || fail "non-Git rejection was not clear"
[[ ! -e "$non_git_target/AGENTS.md" ]] || fail "installer copied files before rejecting the non-Git target"
bash "$REPO_ROOT/install.sh" --target "$non_git_target" --no-hooks > "$TMP_DIR/non-git-no-hooks.log" 2>&1
[[ -f "$non_git_target/AGENTS.md" ]] || fail "--no-hooks did not install into a non-Git target"
[[ ! -e "$non_git_target/.codex/hooks.json" && ! -e "$non_git_target/.claude/settings.json" ]] \
  || fail "non-Git --no-hooks installation registered a hook"

# A clean Git root still receives both hooks through additive JSON merges.
clean_target="$TMP_DIR/clean-target"
init_git_target "$clean_target"
bash "$REPO_ROOT/install.sh" --target "$clean_target" > "$TMP_DIR/clean.log" 2>&1
for relative in \
  scripts/freeze-review-range.py \
  scripts/write-frozen-review.py \
  scripts/validate_lesson_promotions.py \
  .github/workflows/lesson-promotion-gate.yml; do
  [[ -f "$clean_target/$relative" ]] || fail "installer omitted $relative"
  cmp -s "$REPO_ROOT/$relative" "$clean_target/$relative" \
    || fail "installer copied the wrong contents for $relative"
done
[[ ! -e "$clean_target/.github/workflows/installer-tests.yml" ]] \
  || fail "installer copied template-only installer CI without its test suite"
grep -Fq 'scripts/freeze-review-range.py' "$clean_target/prompts/daily-review.md" \
  || fail "installed daily-review prompt does not invoke the frozen-range script"
grep -Fq 'excluded_paths' "$clean_target/prompts/daily-review.md" \
  || fail "installed daily-review prompt does not require honoring frozen exclusions"
grep -Fq 'scripts/write-frozen-review.py' "$clean_target/prompts/daily-review.md" \
  || fail "installed daily-review prompt does not use the frozen-review writer"
python3 - "$clean_target" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
expected = {
    ".codex/hooks.json": 'bash "$(git rev-parse --show-toplevel)/.agent-maintenance/session-start.sh" codex',
    ".claude/settings.json": 'bash "${CLAUDE_PROJECT_DIR}/.agent-maintenance/session-start.sh" claude',
}
for relative_path, command in expected.items():
    config = json.loads((root / relative_path).read_text(encoding="utf-8"))
    handlers = [
        handler
        for session in config["hooks"]["SessionStart"]
        for handler in session.get("hooks", [])
    ]
    assert any(handler.get("type") == "command" and handler.get("command") == command for handler in handlers), relative_path
PY

# A missing bootstrap prompt is diagnosed in stderr and reported as a hook error,
# without emitting normal bootstrap context or starting bootstrap itself.
missing_prompt_target="$TMP_DIR/missing-prompt-target"
init_git_target "$missing_prompt_target"
mkdir -p "$missing_prompt_target/.agent-maintenance"
cp "$REPO_ROOT/.agent-maintenance/session-start.sh" \
  "$missing_prompt_target/.agent-maintenance/session-start.sh"
[[ ! -e "$missing_prompt_target/.agent-maintenance/bootstrap-complete" ]] \
  || fail "missing-prompt fixture unexpectedly has a completion marker"
[[ ! -e "$missing_prompt_target/prompts/bootstrap-project.md" ]] \
  || fail "missing-prompt fixture unexpectedly has the bootstrap prompt"
missing_prompt_status=0
(
  cd "$missing_prompt_target"
  bash .agent-maintenance/session-start.sh codex \
    > "$TMP_DIR/missing-prompt.stdout" 2> "$TMP_DIR/missing-prompt.stderr"
) || missing_prompt_status=$?
[[ "$missing_prompt_status" -eq 1 ]] \
  || fail "missing-prompt SessionStart hook did not report a nonzero status: $missing_prompt_status"
python3 - "$TMP_DIR/missing-prompt.stdout" "$TMP_DIR/missing-prompt.stderr" <<'PY'
import sys
from pathlib import Path
stdout, stderr = (Path(path).read_bytes() for path in sys.argv[1:])
expected = b"Self-Evolution Flywheel: prompts/bootstrap-project.md is missing; bootstrap cannot run. This hook did not perform bootstrap; restore the prompt before using ./.agent-maintenance/flywheel-install.sh --bootstrap codex|claude.\n"
assert stderr == expected, f"unexpected missing-prompt stderr: {stderr!r}"
assert stdout == b"", f"missing-prompt diagnostic was written to stdout: {stdout!r}"
PY

# F1: an existing project installer is preserved, while the template gets a
# distinct executable entry and every bootstrap instruction names that entry.
entry_conflict_target="$TMP_DIR/entry-conflict-target"
mkdir -p "$entry_conflict_target"
printf '#!/usr/bin/env bash\necho PROJECT_INSTALLER_NOT_FLYWHEEL\n' > "$entry_conflict_target/install.sh"
chmod +x "$entry_conflict_target/install.sh"
cp "$entry_conflict_target/install.sh" "$TMP_DIR/business-install-before.sh"
bash "$REPO_ROOT/install.sh" --target "$entry_conflict_target" --no-hooks \
  > "$TMP_DIR/entry-conflict.log" 2>&1
cmp -s "$TMP_DIR/business-install-before.sh" "$entry_conflict_target/install.sh" \
  || fail "business install.sh was not preserved"
grep -Fq './.agent-maintenance/flywheel-install.sh --bootstrap codex' "$TMP_DIR/entry-conflict.log" \
  || fail "completion output named the ambiguous business installer"
[[ -x "$entry_conflict_target/.agent-maintenance/flywheel-install.sh" ]] \
  || fail "dedicated template installer entry was not executable"
grep -Fq 'flywheel-install.sh --bootstrap codex' "$entry_conflict_target/.agent-maintenance/session-start.sh" \
  || fail "startup hook named the ambiguous business installer"
if "$entry_conflict_target/install.sh" --bootstrap codex 2>/dev/null | grep -Fq PROJECT_INSTALLER_NOT_FLYWHEEL; then
  : # proves the business file remains independently owned; the hook never points here.
else
  fail "business installer fixture was unexpectedly changed"
fi

# The dedicated local entry is not a self-repair source: deleting the installed
# business install.sh must not make it enumerate or copy that file, while using
# it for another target must fail with guidance to an independent checkout.
local_source_target="$TMP_DIR/local-source-target"
mkdir -p "$local_source_target"
bash "$REPO_ROOT/install.sh" --target "$local_source_target" --no-hooks \
  > "$TMP_DIR/local-source-first.log" 2>&1
rm -f "$local_source_target/install.sh"
(cd "$local_source_target" && bash .agent-maintenance/flywheel-install.sh --no-hooks \
  > "$TMP_DIR/local-source-second.log" 2>&1) \
  || fail "dedicated local entry treated missing business install.sh as its source"
! grep -Fq 'missing installer source file: install.sh' "$TMP_DIR/local-source-second.log" \
  || fail "local entry still required the target business install.sh"
[[ ! -e "$local_source_target/install.sh" ]] \
  || fail "local entry copied or recreated the target business install.sh"
local_elsewhere_target="$TMP_DIR/local-source-elsewhere"
mkdir -p "$local_elsewhere_target"
if bash "$local_source_target/.agent-maintenance/flywheel-install.sh" \
    --target "$local_elsewhere_target" --no-hooks > "$TMP_DIR/local-source-elsewhere.log" 2>&1; then
  fail "dedicated local entry accepted a different target root"
fi
grep -Fq 'only supports its own project root' "$TMP_DIR/local-source-elsewhere.log" \
  || fail "cross-target local entry error was not explicit"
grep -Fq '/abs/source/install.sh --target' "$TMP_DIR/local-source-elsewhere.log" \
  || fail "cross-target local entry did not guide an independent source checkout"
[[ ! -e "$local_elsewhere_target/AGENTS.md" ]] \
  || fail "cross-target local entry published files before rejection"

# F2: lstat rejects a FIFO before any template publication; installation must
# finish promptly rather than entering cmp and blocking on the pipe.
fifo_target="$TMP_DIR/fifo-target"
mkdir -p "$fifo_target"
mkfifo "$fifo_target/CLAUDE.md"
fifo_status=0
timeout 10 bash "$REPO_ROOT/install.sh" --target "$fifo_target" --no-hooks \
  > "$TMP_DIR/fifo.log" 2>&1 || fifo_status=$?
[[ "$fifo_status" -eq 3 ]] || fail "FIFO target was not rejected cleanly: $fifo_status"
grep -Fq 'not a regular file' "$TMP_DIR/fifo.log" || fail "FIFO rejection was not reported"
[[ ! -e "$fifo_target/AGENTS.md" ]] || fail "FIFO preflight published files before rejection"

# Mismatched dedicated entry is detected before publication and cannot bootstrap.
unsafe_entry_target="$TMP_DIR/unsafe-entry-target"
mkdir -p "$unsafe_entry_target/.agent-maintenance"
printf '#!/usr/bin/env bash\necho untrusted-entry\n' > "$unsafe_entry_target/.agent-maintenance/flywheel-install.sh"
if bash "$REPO_ROOT/install.sh" --target "$unsafe_entry_target" --no-hooks --bootstrap codex > "$TMP_DIR/unsafe-entry.log" 2>&1; then
  fail "mismatched dedicated entry was allowed to bootstrap"
fi
grep -Fq 'unknown or customized framework file refused' "$TMP_DIR/unsafe-entry.log" || fail "untrusted entry refusal was not clear"
grep -Fq 'unknown or customized framework file refused' "$TMP_DIR/unsafe-entry.log" || fail "untrusted entry did not suppress bootstrap"
repeat_target="$TMP_DIR/repeat-business-target"
mkdir -p "$repeat_target"
printf '#!/usr/bin/env bash\necho business\n' > "$repeat_target/install.sh"
bash "$REPO_ROOT/install.sh" --target "$repeat_target" --no-hooks > "$TMP_DIR/repeat-first.log" 2>&1
bash "$repeat_target/.agent-maintenance/flywheel-install.sh" --target "$repeat_target" --no-hooks > "$TMP_DIR/repeat-second.log" 2>&1
grep -Fq 'UNCHANGED framework .agent-maintenance/flywheel-install.sh' "$TMP_DIR/repeat-second.log" || fail "repeated dedicated installer did not recognize its entry"
grep -Fq 'echo business' "$repeat_target/install.sh" || fail "dedicated rerun changed business install.sh"
# FIFO/socket/device/symlink destinations are rejected before publication.
for special_kind in socket device symlink; do
  special_target="$TMP_DIR/special-$special_kind"
  mkdir -p "$special_target"
  case "$special_kind" in
    socket) python3 - "$special_target/CLAUDE.md" <<'PY2'
import socket, sys
s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.close()
PY2
      ;;
    device) mknod "$special_target/CLAUDE.md" c 1 7 2>/dev/null || continue ;;
    symlink) ln -s "$TMP_DIR/outside-special" "$special_target/CLAUDE.md" ;;
  esac
  special_status=0
  timeout 10 bash "$REPO_ROOT/install.sh" --target "$special_target" --no-hooks > "$TMP_DIR/special-$special_kind.log" 2>&1 || special_status=$?
  [[ "$special_status" -eq 3 ]] || fail "$special_kind target was not rejected cleanly: $special_status"
  [[ ! -e "$special_target/AGENTS.md" ]] || fail "$special_kind rejection published files"
done
# A script streamed through stdin has no BASH_SOURCE path. Simulate git clone
# returning one complete checkout and verify the pipe shim delegates to its installer.
mock_bin="$TMP_DIR/mock-bin"
mkdir -p "$mock_bin"
cat > "$mock_bin/git" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "clone" ]]; then
  destination="${@: -1}"
  mkdir -p "$destination"
  cp -R "$SELF_EVOLUTION_TEST_SOURCE_DIR"/. "$destination"/
  exit 0
fi
exec /usr/bin/git "$@"
SH
chmod +x "$mock_bin/git"
remote_target="$TMP_DIR/remote-target"
mkdir -p "$remote_target"
(
  cd "$remote_target"
  PATH="$mock_bin:$PATH" SELF_EVOLUTION_TEST_SOURCE_DIR="$REPO_ROOT" \
    bash -c 'cat "$1" | bash -s -- --no-hooks' _ "$REPO_ROOT/install.sh"
) > "$TMP_DIR/remote.log" 2>&1
grep -Fq "Installation complete for: $remote_target" "$TMP_DIR/remote.log" \
  || fail "pipe installer did not complete in the current target directory"
grep -Fq 'Cloning Self-Evolution Flywheel source' "$TMP_DIR/remote.log" \
  || fail "pipe installer did not clone the source checkout"
[[ -f "$remote_target/prompts/bootstrap-project.md" ]] \
  || fail "pipe installer did not install the bootstrap prompt"
[[ -f "$remote_target/scripts/watermark.py" ]] \
  || fail "installer did not copy the shared watermark module"
( cd /tmp && python3 "$remote_target/scripts/freeze-review-range.py" --help > "$TMP_DIR/installed-freezer-help.log" ) \
  || fail "installed freezer CLI failed outside its source checkout"
( cd /tmp && python3 "$remote_target/scripts/write-frozen-review.py" --help > "$TMP_DIR/installed-writer-help.log" ) \
  || fail "installed writer CLI failed outside its source checkout"

python3 "$REPO_ROOT/tests/test-installer-races.py"
python3 "$REPO_ROOT/tests/test-installer-recovery.py"
python3 "$REPO_ROOT/tests/test-installer-security.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$REPO_ROOT/tests/test-installer-ignore.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$REPO_ROOT/tests/test-session-start.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$REPO_ROOT/tests/test-range-freezer.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$REPO_ROOT/tests/test-range-writer.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$REPO_ROOT/tests/test-daily-review-temp.py"
python3 "$REPO_ROOT/tests/test-installer-confirmation.py"
python3 "$REPO_ROOT/tests/test-installer-confirmation-transaction.py"
python3 "$REPO_ROOT/tests/test-installer-xattrs.py"
python3 "$REPO_ROOT/tests/test-installer-xattr-install.py"
python3 "$REPO_ROOT/tests/test-installer-atime-contract.py"
python3 "$REPO_ROOT/tests/test-installer-atime-interaction.py"
python3 "$REPO_ROOT/tests/test-macos-metadata-contract.py"
echo "PASS: installer safety, hook, session-start, read-only range-freezer, and frozen-range writer regression tests"
