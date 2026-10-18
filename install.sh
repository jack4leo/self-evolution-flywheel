#!/usr/bin/env bash
set -eo pipefail

# A raw-GitHub pipe has no script directory, so clone the full source tree before
# delegating to the regular installer. Local checkout/template installs stay offline.
if [[ -z "${BASH_SOURCE[0]:-}" ]]; then
  command -v git >/dev/null 2>&1 || { echo "ERROR: git is required for pipe installation." >&2; exit 127; }

  source_dir="$(mktemp -d)"
  trap 'rm -rf "$source_dir"' EXIT
  echo "Cloning Self-Evolution Flywheel source..."
  git clone --quiet --depth 1 --single-branch --branch main \
    "https://github.com/jack4leo/self-evolution-flywheel.git" "$source_dir"
  [[ -f "$source_dir/install.sh" && -f "$source_dir/prompts/bootstrap-project.md" ]] || {
    echo "ERROR: cloned source is missing installer files." >&2
    exit 2
  }

  bash "$source_dir/install.sh" "$@"
  exit $?
fi

usage() {
  cat <<'USAGE'
Usage: ./install.sh [--target DIR] [--no-hooks] [--bootstrap codex|claude]

Install Self-Evolution Flywheel project guidance into an existing repository.
Existing files are never overwritten. A different AGENTS.md is shown as a
proposed diff and must be merged manually; the installer does not ask for
confirmation when it cannot safely apply the change. Existing provider JSON is
never rewritten: exact hooks are accepted, missing settings files are created,
and if an existing settings file needs a hook added, only that provider's hook
registration is skipped; the other provider is still validated independently.
Other template files are still installed when both settings files are valid.

Options:
  --target DIR              Destination project root (default: current directory)
  --no-hooks                Copy guidance without registering startup hooks
  --bootstrap codex|claude  After installation, start that CLI with the bootstrap prompt
  -h, --help                Show this help
USAGE
}

INSTALLER_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/$(basename "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname "$INSTALLER_PATH")"
# Installed copies use a dedicated entry so a target project's own install.sh
# is never mistaken for this template installer.
if [[ "$(basename "${BASH_SOURCE[0]}")" == "flywheel-install.sh" ]]; then
  SOURCE_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
  LOCAL_ENTRY=1
else
  SOURCE_DIR="$SCRIPT_DIR"
  LOCAL_ENTRY=0
fi
ORIGINAL_ARGS=("$@")
TARGET_DIR="$PWD"
TRANSACTION_HELPER="$SOURCE_DIR/.agent-maintenance/installer-transaction.py"
[[ -f "$TRANSACTION_HELPER" ]] || {
  echo "ERROR: missing installer transaction helper: .agent-maintenance/installer-transaction.py" >&2
  exit 2
}
INSTALL_HOOKS=1
BOOTSTRAP_AGENT=""

while (($#)); do
  case "$1" in
    --target)
      [[ $# -ge 2 ]] || { echo "ERROR: --target requires a directory" >&2; exit 2; }
      TARGET_DIR="$2"
      shift 2
      ;;
    --no-hooks)
      INSTALL_HOOKS=0
      shift
      ;;
    --bootstrap)
      [[ $# -ge 2 ]] || { echo "ERROR: --bootstrap requires codex or claude" >&2; exit 2; }
      BOOTSTRAP_AGENT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$BOOTSTRAP_AGENT" in
  ""|codex|claude) ;;
  *) echo "ERROR: --bootstrap must be codex or claude" >&2; exit 2 ;;
esac

[[ -d "$TARGET_DIR" ]] || { echo "ERROR: target directory does not exist: $TARGET_DIR" >&2; exit 2; }
TARGET_DIR="$(cd "$TARGET_DIR" && pwd -P)"
if [[ "$LOCAL_ENTRY" == 1 && "$TARGET_DIR" != "$SOURCE_DIR" ]]; then
  echo "ERROR: the project-local .agent-maintenance/flywheel-install.sh only supports its own project root." >&2
  echo "       Use a fixed-version independent checkout: /abs/source/install.sh --target $TARGET_DIR" >&2
  exit 2
fi
[[ -f "$SOURCE_DIR/prompts/bootstrap-project.md" ]] || {
  echo "ERROR: run this installer from a Self-Evolution Flywheel checkout/template copy." >&2
  exit 2
}

if [[ "$INSTALL_HOOKS" == 1 ]] && ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required to safely merge Codex/Claude JSON settings." >&2
  exit 2
fi

# Project-local Codex hooks require the target itself to be the Git root.
if [[ "$INSTALL_HOOKS" == 1 ]]; then
  git_root="$(git -C "$TARGET_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
  if [[ -z "$git_root" ]]; then
    echo "ERROR: project hooks require a Git repository root. Use --no-hooks for file-only installation." >&2
    exit 2
  fi
  if [[ "$(cd "$git_root" && pwd -P)" != "$TARGET_DIR" ]]; then
    echo "ERROR: --target must be the Git repository root: $git_root" >&2
    echo "       Use --no-hooks to install files into a subdirectory." >&2
    exit 2
  fi
fi

# Keep one exclusive lock on the target directory for the full installer run.
# The helper execs this script with the locked directory descriptor inherited;
# SIGKILL closes it automatically and does not leave a lock file behind.
if [[ -n "${SELF_EVOLUTION_FLYWHEEL_LOCK_FD:-}" ]]; then
  python3 "$TRANSACTION_HELPER" verify-lock "$TARGET_DIR" "$SELF_EVOLUTION_FLYWHEEL_LOCK_FD" || exit $?
else
  exec python3 "$TRANSACTION_HELPER" lock-exec "$TARGET_DIR" "$(command -v bash)" "$INSTALLER_PATH" "${ORIGINAL_ARGS[@]}"
fi

# Recover only a provider staging hard link that can be tied to this installer's
# exact generated settings inode, owner, contents and two-link publish boundary.
if [[ "$INSTALL_HOOKS" == 1 ]]; then
  python3 "$TRANSACTION_HELPER" recover-hook-staging "$TARGET_DIR" codex
  python3 "$TRANSACTION_HELPER" recover-hook-staging "$TARGET_DIR" claude
fi

files=(
  AGENTS.md
  CLAUDE.md
  LESSONS.md
)
if [[ "$LOCAL_ENTRY" == 0 ]]; then
  # A project may own install.sh as business code. The dedicated local entry
  # must not require, enumerate, or copy that file as its own source.
  files+=(install.sh)
fi
files+=(
  .agent-maintenance/session-start.sh
  .agent-maintenance/installer-transaction.py
  .github/PULL_REQUEST_TEMPLATE.md
  .github/workflows/lesson-promotion-gate.yml
)
template_entry="$TARGET_DIR/.agent-maintenance/flywheel-install.sh"
while IFS= read -r -d '' source_path; do
  files+=("${source_path#"$SOURCE_DIR"/}")
done < <(find "$SOURCE_DIR/scripts" -type f ! -path '*/__pycache__/*' ! -name '*.pyc' -print0)
while IFS= read -r -d '' source_path; do
  files+=("${source_path#"$SOURCE_DIR"/}")
done < <(find "$SOURCE_DIR/docs" "$SOURCE_DIR/prompts" -type f \
  ! -path "$SOURCE_DIR/docs/source-and-extraction.md" -print0)
for relative_path in "${files[@]}"; do
  [[ -f "$SOURCE_DIR/$relative_path" ]] || {
    echo "ERROR: missing installer source file: $relative_path" >&2
    exit 2
  }
done

safe_compare_target() {
  python3 "$TRANSACTION_HELPER" compare "$TARGET_DIR" "$1" "$2"
}

validate_destination_parents() {
  local relative_path="$1"
  local parent_path="${relative_path%/*}"
  [[ "$parent_path" == "$relative_path" ]] && return 0

  local current="$TARGET_DIR"
  local component
  local -a components
  IFS='/' read -r -a components <<< "$parent_path"
  for component in "${components[@]}"; do
    current="$current/$component"
    if [[ -L "$current" ]]; then
      echo "ERROR: target path traverses a symbolic-link directory: ${current#"$TARGET_DIR"/}" >&2
      return 1
    fi
    if [[ -e "$current" && ! -d "$current" ]]; then
      echo "ERROR: target path parent is not a directory: ${current#"$TARGET_DIR"/}" >&2
      return 1
    fi
  done
}

for relative_path in "${files[@]}"; do
  validate_destination_parents "$relative_path" || exit 3
done
# Never run cmp/open against an arbitrary pre-existing target path. lstat all
# ordinary destinations before the first publication; special files and links
# are rejected without following them, preserving the target and avoiding FIFO
# or device blocking. Provider settings have their stricter preflight below.
python3 - "$TARGET_DIR" "${files[@]}" <<'PY'
import os
import stat
import sys
root, *relative_files = sys.argv[1:]
for relative in relative_files:
    path = os.path.join(root, relative)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        continue
    if not stat.S_ISREG(info.st_mode):
        print(f"ERROR: existing target path is not a regular file (preserved): {relative}", file=sys.stderr)
        sys.exit(3)
PY
validate_destination_parents ".agent-maintenance/flywheel-install.sh" || exit 3
template_entry_conflict=0
if [[ -L "$template_entry" || ( -e "$template_entry" && ! -f "$template_entry" ) ]]; then
  echo "ERROR: existing template installer entry is not a regular file: .agent-maintenance/flywheel-install.sh" >&2
  exit 3
elif [[ -e "$template_entry" ]]; then
  if safe_compare_target ".agent-maintenance/flywheel-install.sh" "$INSTALLER_PATH"; then
    :
  else
    compare_status=$?
    if ((compare_status > 1)); then
      echo "ERROR: cannot safely compare existing template installer entry; it was preserved." >&2
      exit 3
    fi
    template_entry_conflict=1
  fi
fi

if [[ "$INSTALL_HOOKS" == 1 ]]; then
  validate_destination_parents ".codex/hooks.json" || exit 3
  validate_destination_parents ".claude/settings.json" || exit 3
fi

preflight_hook_config() {
  local provider="$1"
  local settings_path="$2"
  python3 - "$provider" "$settings_path" "$TARGET_DIR" "$TRANSACTION_HELPER" <<'PY'
import json
import errno
import os
import stat
import subprocess
import sys

provider, filename, target_dir, helper = sys.argv[1:]

def open_checked_settings(path, expected):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("platform cannot safely open settings without following symbolic links")

    def opener(name, flags):
        safe_flags = flags | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        noatime = getattr(os, "O_NOATIME", 0)
        try:
            return os.open(name, safe_flags | noatime)
        except OSError as exc:
            if not noatime or exc.errno not in {errno.EPERM, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP}:
                raise
            return os.open(name, safe_flags)

    handle = open(path, encoding="utf-8", opener=opener)
    try:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("settings path is not a regular file")
        if (opened.st_dev, opened.st_ino, opened.st_nlink) != (
            expected.st_dev,
            expected.st_ino,
            expected.st_nlink,
        ):
            raise OSError("settings file changed between inspection and open")
        return handle, opened
    except Exception:
        handle.close()
        raise

try:
    if os.path.lexists(filename):
        info = os.lstat(filename)
        if stat.S_ISLNK(info.st_mode):
            raise OSError("settings file is a symbolic link")
        if not stat.S_ISREG(info.st_mode):
            raise OSError("settings path is not a regular file")
        if info.st_nlink > 1:
            if info.st_nlink != 2:
                raise OSError("settings file has extra hard links")
            validation = subprocess.run(
                [sys.executable, helper, "validate-hook-staging", target_dir, provider],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=os.environ.copy(),
                pass_fds=(int(os.environ["SELF_EVOLUTION_FLYWHEEL_LOCK_FD"]),),
            )
            if validation.returncode != 0:
                detail = validation.stdout.strip()
                raise OSError(
                    "multiply-linked settings lack one verified provider staging candidate"
                    + (f": {detail}" if detail else "")
                )
        handle, opened = open_checked_settings(filename, info)
        try:
            listxattr = getattr(os, "listxattr", None)
            getxattr = getattr(os, "getxattr", None)
            if listxattr is not None and getxattr is not None:
                for name in listxattr(handle.fileno()):
                    getxattr(handle.fileno(), name)
            try:
                config = json.load(handle)
            finally:
                if os.fstat(handle.fileno()).st_atime_ns != opened.st_atime_ns:
                    os.utime(
                        handle.fileno(),
                        ns=(opened.st_atime_ns, opened.st_mtime_ns),
                    )
        finally:
            handle.close()
    else:
        config = {}
except Exception as exc:
    print(f"ERROR: cannot safely prepare {filename}: {exc}; no files were installed.", file=sys.stderr)
    sys.exit(3)

if not isinstance(config, dict):
    print(f"ERROR: {filename} must contain a JSON object; no files were installed.", file=sys.stderr)
    sys.exit(3)
hooks = config.get("hooks", {})
if not isinstance(hooks, dict):
    print(f"ERROR: {filename}: 'hooks' must be a JSON object; no files were installed.", file=sys.stderr)
    sys.exit(3)
sessions = hooks.get("SessionStart", [])
if not isinstance(sessions, list):
    print(f"ERROR: {filename}: 'hooks.SessionStart' must be a JSON array; no files were installed.", file=sys.stderr)
    sys.exit(3)
for session in sessions:
    if not isinstance(session, dict):
        print(f"ERROR: {filename}: each SessionStart entry must be a JSON object; no files were installed.", file=sys.stderr)
        sys.exit(3)
    if "matcher" in session and not isinstance(session["matcher"], str):
        print(f"ERROR: {filename}: each SessionStart matcher must be a string; no files were installed.", file=sys.stderr)
        sys.exit(3)
    handlers = session.get("hooks")
    if not isinstance(handlers, list):
        print(f"ERROR: {filename}: each SessionStart 'hooks' value must be a JSON array; no files were installed.", file=sys.stderr)
        sys.exit(3)
    for handler in handlers:
        if not isinstance(handler, dict) or not isinstance(handler.get("type"), str):
            print(f"ERROR: {filename}: each SessionStart hook must be an object with a string 'type'; no files were installed.", file=sys.stderr)
            sys.exit(3)
        if handler["type"] == "command" and not isinstance(handler.get("command"), str):
            print(f"ERROR: {filename}: each command SessionStart hook must have a string 'command'; no files were installed.", file=sys.stderr)
            sys.exit(3)

hook_commands = {
    "codex": 'bash "$(git rev-parse --show-toplevel)/.agent-maintenance/session-start.sh" codex',
    "claude": 'bash "${CLAUDE_PROJECT_DIR}/.agent-maintenance/session-start.sh" claude',
}
all_session_sources_matcher = "startup|resume|clear|compact|fork"

def matcher_covers_all_sources(session):
    if "matcher" not in session:
        return True
    return session["matcher"] in ("", "*", ".*", all_session_sources_matcher)

registered = any(
    isinstance(session, dict)
    and matcher_covers_all_sources(session)
    and any(
        isinstance(handler, dict)
        and handler.get("type") == "command"
        and handler.get("command") == hook_commands[provider]
        for handler in session.get("hooks", [])
    )
    for session in sessions
)
if os.path.lexists(filename) and not registered:
    print(
        f"NOTICE: existing {filename} needs a manual hook merge; it was preserved and startup-hook registration is skipped. Template files will still be installed.",
        file=sys.stderr,
    )
    sys.exit(11)
PY
}

# Validate both provider settings independently before installing any template
# files. A valid conflict skips only that provider's hook registration.
CODEX_HOOKS_ENABLED=0
CLAUDE_HOOKS_ENABLED=0
hook_config_conflicts=()
codex_preflight_error=0
claude_preflight_error=0
if [[ "$INSTALL_HOOKS" == 1 ]]; then
  if preflight_hook_config codex "$TARGET_DIR/.codex/hooks.json"; then
    CODEX_HOOKS_ENABLED=1
  else
    preflight_status=$?
    if ((preflight_status == 11)); then
      hook_config_conflicts+=(".codex/hooks.json")
    else
      codex_preflight_error=$preflight_status
    fi
  fi
  if preflight_hook_config claude "$TARGET_DIR/.claude/settings.json"; then
    CLAUDE_HOOKS_ENABLED=1
  else
    preflight_status=$?
    if ((preflight_status == 11)); then
      hook_config_conflicts+=(".claude/settings.json")
    else
      claude_preflight_error=$preflight_status
    fi
  fi
  if ((codex_preflight_error)); then
    exit "$codex_preflight_error"
  fi
  if ((claude_preflight_error)); then
    exit "$claude_preflight_error"
  fi
fi

# Never register hooks against a pre-existing project script with different
# contents. Do this before copying files or changing either provider's settings.
if [[ "$INSTALL_HOOKS" == 1 ]]; then
  hook_script="$TARGET_DIR/.agent-maintenance/session-start.sh"
  if [[ -e "$hook_script" || -L "$hook_script" ]]; then
    if [[ ! -f "$hook_script" ]] || ! safe_compare_target ".agent-maintenance/session-start.sh" "$SOURCE_DIR/.agent-maintenance/session-start.sh"; then
      echo "ERROR: existing .agent-maintenance/session-start.sh differs from the installer; it was preserved and no startup hooks were registered." >&2
      echo "       Resolve the file conflict, or use --no-hooks for file-only installation." >&2
      exit 3
    fi
  fi
fi

# Detect predictable destination permission failures before writing any template
# files or provider settings. This checks only paths that would actually change.
preflight_write_access() {
  python3 - "$SOURCE_DIR" "$TARGET_DIR" "$CODEX_HOOKS_ENABLED" "$CLAUDE_HOOKS_ENABLED" "${files[@]}" <<'PY'
import json
import errno
import os
import stat
import sys

source_dir, target_dir, codex_hooks_enabled, claude_hooks_enabled, *relative_files = sys.argv[1:]
needed_directories = set()

def open_checked_settings(path, expected):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("platform cannot safely open settings without following symbolic links")

    def opener(name, flags):
        safe_flags = flags | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        noatime = getattr(os, "O_NOATIME", 0)
        try:
            return os.open(name, safe_flags | noatime)
        except OSError as exc:
            if not noatime or exc.errno not in {errno.EPERM, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP}:
                raise
            return os.open(name, safe_flags)

    handle = open(path, encoding="utf-8", opener=opener)
    try:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("settings path is not a regular file")
        if (opened.st_dev, opened.st_ino, opened.st_nlink) != (
            expected.st_dev,
            expected.st_ino,
            expected.st_nlink,
        ):
            raise OSError("settings file changed between inspection and open")
        return handle, opened
    except Exception:
        handle.close()
        raise

for relative in relative_files:
    destination = os.path.join(target_dir, relative)
    if os.path.lexists(destination):
        if relative == "AGENTS.md":
            needed_directories.add(os.path.dirname(destination))
        continue
    needed_directories.add(os.path.dirname(destination))

if codex_hooks_enabled == "1" or claude_hooks_enabled == "1":
    hook_settings = {
        ".codex/hooks.json": ("codex", codex_hooks_enabled, 'bash "$(git rev-parse --show-toplevel)/.agent-maintenance/session-start.sh" codex'),
        ".claude/settings.json": ("claude", claude_hooks_enabled, 'bash "${CLAUDE_PROJECT_DIR}/.agent-maintenance/session-start.sh" claude'),
    }
    all_session_sources_matcher = "startup|resume|clear|compact|fork"
    for relative, (provider, enabled, command) in hook_settings.items():
        if enabled != "1":
            continue
        filename = os.path.join(target_dir, relative)
        config = {}
        if os.path.lexists(filename):
            try:
                expected = os.lstat(filename)
                if stat.S_ISLNK(expected.st_mode):
                    raise OSError("settings file is a symbolic link")
                if not stat.S_ISREG(expected.st_mode):
                    raise OSError("settings path is not a regular file")
                handle, opened = open_checked_settings(filename, expected)
                try:
                    try:
                        config = json.load(handle)
                    finally:
                        if os.fstat(handle.fileno()).st_atime_ns != opened.st_atime_ns:
                            os.utime(
                                handle.fileno(),
                                ns=(opened.st_atime_ns, opened.st_mtime_ns),
                            )
                finally:
                    handle.close()
            except Exception as exc:
                print(
                    f"ERROR: cannot safely inspect {filename}: {exc}; no files were installed.",
                    file=sys.stderr,
                )
                sys.exit(3)
        sessions = config.get("hooks", {}).get("SessionStart", [])
        already_registered = any(
            isinstance(session, dict)
            and (
                "matcher" not in session
                or session.get("matcher") in ("", "*", ".*", all_session_sources_matcher)
            )
            and any(
                isinstance(handler, dict)
                and handler.get("type") == "command"
                and handler.get("command") == command
                for handler in session.get("hooks", [])
            )
            for session in sessions
        )
        if not already_registered:
            needed_directories.add(os.path.dirname(filename))

for directory in sorted(needed_directories):
    candidate = directory
    while not os.path.lexists(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    try:
        info = os.lstat(candidate)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError("nearest existing parent is not a real directory")
        if not os.access(candidate, os.W_OK | os.X_OK):
            raise PermissionError("directory is not writable and searchable")
    except OSError as exc:
        relative = os.path.relpath(directory, target_dir)
        print(
            f"ERROR: destination directory is not writable ({relative}): {exc}; no files were installed.",
            file=sys.stderr,
        )
        sys.exit(3)
PY
}

preflight_write_access

work_dir="$(mktemp -d)"
trap 'rm -rf -- "$work_dir"' EXIT
transaction_state="$work_dir/transaction.json"
python3 "$TRANSACTION_HELPER" init "$TARGET_DIR" "$transaction_state"
transaction_active=1
retain_work_dir=0

rollback_transaction() {
  python3 "$TRANSACTION_HELPER" rollback "$TARGET_DIR" "$transaction_state"
}

installer_cleanup() {
  local status=$?
  trap - EXIT
  if ((status != 0 && transaction_active)); then
    echo "ERROR: installation write failed; attempting to roll back this install." >&2
    if ! rollback_transaction; then
      echo "ERROR: rollback was incomplete; uncertain files and recovery entries were preserved rather than deleted." >&2
      retain_work_dir=1
    fi
  fi
  if ((retain_work_dir)); then
    echo "ERROR: rollback snapshots retained for recovery at: $work_dir" >&2
  else
    rm -rf -- "$work_dir"
  fi
  exit "$status"
}

trap installer_cleanup EXIT
managed_route_file="$work_dir/agents-routing.md"
cat > "$managed_route_file" <<'ROUTE'
<!-- BEGIN SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->
## Self-Evolution Flywheel

- Before work, read `docs/project-profile.md`, `LESSONS.md`, and `docs/code-standards.md`; read the affected subsystem page when relevant.
- Follow repository evidence, documented validation, and permission boundaries; keep changes focused.
- Before finishing, update affected docs/tests, run applicable checks, and report verified results and limitations.
<!-- END SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->
ROUTE

display_managed_diff() {
  python3 - "$1" "$2" <<'PY'
import subprocess
import sys
import unicodedata

try:
    process = subprocess.Popen(
        ["diff", "-u", "-L", "AGENTS.md (current)", "-L", "AGENTS.md (proposed)", *sys.argv[1:]],
        stdout=subprocess.PIPE,
    )
except OSError as exc:
    print(f"ERROR: could not start diff: {exc}", file=sys.stderr)
    sys.exit(2)

def escape_controls(raw):
    text = raw.decode("utf-8", "surrogateescape")
    escaped = []
    for char in text:
        codepoint = ord(char)
        category = unicodedata.category(char)
        if char == "\n":
            escaped.append(char)
        elif 0xDC80 <= codepoint <= 0xDCFF:
            escaped.append(f"\\x{codepoint - 0xDC00:02x}")
        elif category.startswith("C") or category in {"Zl", "Zp"}:
            if codepoint <= 0xFF:
                escaped.append(f"\\x{codepoint:02x}")
            elif codepoint <= 0xFFFF:
                escaped.append(f"\\u{codepoint:04x}")
            else:
                escaped.append(f"\\U{codepoint:08x}")
        else:
            escaped.append(char)
    return "".join(escaped)

assert process.stdout is not None
for line in iter(process.stdout.readline, b""):
    sys.stdout.buffer.write(escape_controls(line).encode("utf-8"))
    sys.stdout.buffer.flush()
process.stdout.close()
sys.exit(process.wait())
PY
}

prepare_managed_agents() {
  local destination_path="$1"
  local baseline="$work_dir/agents.original"
  local stage_file diff_status=0

  if ! command -v python3 >/dev/null 2>&1; then
    echo "PRESERVED existing file (Python 3 is required to safely manage AGENTS.md): AGENTS.md" >&2
    return 1
  fi
  if ! python3 "$TRANSACTION_HELPER" snapshot "$TARGET_DIR" "AGENTS.md" "$baseline"; then
    echo "PRESERVED existing file (could not snapshot it safely): AGENTS.md" >&2
    return 1
  fi
  stage_file="$(mktemp "$work_dir/agents-stage.XXXXXX")"
  if ! python3 - "$baseline" "$managed_route_file" "$stage_file" <<'PY'
import sys
from pathlib import Path

source, route_path, stage_path = map(Path, sys.argv[1:])
begin = "<!-- BEGIN SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->"
end = "<!-- END SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->"

try:
    original = source.read_bytes()
    text = original.decode("utf-8")
    route = route_path.read_text(encoding="utf-8")
except (OSError, UnicodeError) as exc:
    print(f"ERROR: cannot safely prepare managed routing for AGENTS.md: {exc}; left unchanged.", file=sys.stderr)
    sys.exit(3)

newline = "\r\n" if "\r\n" in text else "\n"
route = route.replace("\r\n", "\n").replace("\n", newline)
lines = text.splitlines(keepends=True)
markers = [line.rstrip("\r\n") for line in lines]
begin_positions = [i for i, line in enumerate(markers) if line == begin]
end_positions = [i for i, line in enumerate(markers) if line == end]

if not begin_positions and not end_positions:
    updated = route + newline + text
elif len(begin_positions) == 1 and len(end_positions) == 1 and begin_positions[0] < end_positions[0]:
    first, last = begin_positions[0], end_positions[0]
    prefix = "".join(lines[:first])
    suffix = "".join(lines[last + 1 :])
    updated = prefix + route + suffix
else:
    print("ERROR: AGENTS.md has missing, duplicated, or out-of-order managed routing markers; left unchanged.", file=sys.stderr)
    sys.exit(3)

try:
    with stage_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
except OSError as exc:
    print(f"ERROR: cannot stage managed routing for AGENTS.md: {exc}; left unchanged.", file=sys.stderr)
    sys.exit(3)
PY
  then
    rm -f "$stage_file"
    return 1
  fi

  if cmp -s "$baseline" "$stage_file"; then
    rm -f "$stage_file"
    echo "UNCHANGED managed routing block in AGENTS.md"
    return 0
  fi

  echo "Proposed diff for AGENTS.md (no changes applied yet):"
  display_managed_diff "$baseline" "$stage_file" || diff_status=$?
  if ((diff_status > 1)); then
    echo "PRESERVED existing file (could not display the proposed diff): AGENTS.md" >&2
    rm -f "$stage_file"
    return 1
  fi

  echo "PRESERVED AGENTS.md: automatic replacement is unavailable; merge the reviewed block manually." >&2
  rm -f "$stage_file"
  return 3
}

prompt_ready=0
conflicts=()
manual_merge_required=0
for relative_path in "${files[@]}"; do
  source_path="$SOURCE_DIR/$relative_path"
  destination_path="$TARGET_DIR/$relative_path"
  [[ -f "$source_path" ]] || { echo "ERROR: missing installer source file: $relative_path" >&2; exit 2; }

  if [[ -e "$destination_path" || -L "$destination_path" ]]; then
    if [[ "$relative_path" == "AGENTS.md" ]]; then
      if [[ -L "$destination_path" || ! -f "$destination_path" ]]; then
        echo "PRESERVED existing non-regular or symlink file: AGENTS.md"
        conflicts+=("$relative_path")
      elif [[ "$source_path" -ef "$destination_path" ]] || safe_compare_target "$relative_path" "$source_path"; then
        echo "UNCHANGED $relative_path"
      elif prepare_managed_agents "$destination_path"; then
        :
      else
        managed_status=$?
        echo "PRESERVED existing file (managed routing not applied): AGENTS.md"
        conflicts+=("$relative_path")
        if ((managed_status == 3)); then
          manual_merge_required=1
        fi
      fi
      continue
    fi
    if [[ "$source_path" -ef "$destination_path" ]] || safe_compare_target "$relative_path" "$source_path"; then
      echo "UNCHANGED $relative_path"
      [[ "$relative_path" == "prompts/bootstrap-project.md" ]] && prompt_ready=1
    else
      echo "PRESERVED existing file (not overwritten): $relative_path"
      conflicts+=("$relative_path")
    fi
    continue
  fi

  if copy_result="$(python3 "$TRANSACTION_HELPER" copy "$TARGET_DIR" "$relative_path" "$source_path" "$transaction_state")"; then
    :
  else
    copy_status=$?
    if ((copy_status == 10)); then
      echo "PRESERVED existing file (appeared during installation): $relative_path"
      conflicts+=("$relative_path")
      continue
    fi
    exit "$copy_status"
  fi
  echo "INSTALLED $relative_path"
  [[ "$relative_path" == "prompts/bootstrap-project.md" ]] && prompt_ready=1
done

# Keep the template installer available even when the target already owns
# install.sh. This copy is intentionally a separate entry point.
template_entry="$TARGET_DIR/.agent-maintenance/flywheel-install.sh"
if [[ -e "$template_entry" || -L "$template_entry" ]]; then
  if [[ -L "$template_entry" || ! -f "$template_entry" ]]; then
    echo "ERROR: existing template installer entry is not a regular file: .agent-maintenance/flywheel-install.sh" >&2
    exit 3
  fi
  if ((template_entry_conflict == 0)); then
    echo "UNCHANGED .agent-maintenance/flywheel-install.sh"
  else
    echo "PRESERVED existing file (not overwritten): .agent-maintenance/flywheel-install.sh"
    conflicts+=(".agent-maintenance/flywheel-install.sh")
  fi
else
  python3 "$TRANSACTION_HELPER" copy "$TARGET_DIR" ".agent-maintenance/flywheel-install.sh" "$INSTALLER_PATH" "$transaction_state" >/dev/null
  echo "INSTALLED .agent-maintenance/flywheel-install.sh"
fi

merge_hook_config() {
  local provider="$1"
  local settings_path="$2"
  local relative_path="${settings_path#"$TARGET_DIR"/}"
  local result
  result="$(python3 "$TRANSACTION_HELPER" merge-hook "$TARGET_DIR" "$provider" "$relative_path" "$transaction_state")" || return $?
  printf '%s %s\n' "$result" "$settings_path"
}

if [[ "$CODEX_HOOKS_ENABLED" == 1 ]]; then
  merge_hook_config codex "$TARGET_DIR/.codex/hooks.json"
fi
if [[ "$CLAUDE_HOOKS_ENABLED" == 1 ]]; then
  merge_hook_config claude "$TARGET_DIR/.claude/settings.json"
fi

# All file and provider writes succeeded. Later notices, optional bootstrap launch,
# or a manual-merge exit must not undo an otherwise completed install.
transaction_active=0

# Ask Git, not a partial .gitignore parser: this includes info/exclude and the
# effective global excludesFile, with normal precedence and negation semantics.
# Diagnostics are advisory even if Git is unavailable or a query fails.
if ! python3 - "$TARGET_DIR" <<'PY'
import os
import subprocess
import sys

target = sys.argv[1]
env = dict(os.environ, LC_ALL="C", GIT_OPTIONAL_LOCKS="0")
# -C changes the working directory, but inherited hook/repository variables can
# still select a foreign Git directory, index, worktree, or discovery boundary.
# Isolate only repository-local state for these read-only diagnostics. Keep HOME,
# XDG_CONFIG_HOME and GIT_CONFIG* so normal global/configured excludes still apply.
for name in (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_IMPLICIT_WORK_TREE", "GIT_PREFIX", "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE",
    "GIT_REPLACE_REF_BASE", "GIT_NO_REPLACE_OBJECTS", "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
):
    env.pop(name, None)


def git(*args, input=None):
    return subprocess.run(
        ["git", "-C", target, *args], input=input,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=10,
    )


def check():
    repository = git("rev-parse", "--is-inside-work-tree")
    if repository.returncode:
        if b"not a git repository" in repository.stderr.lower():
            return  # File-only installation outside Git has no ignore semantics.
        raise RuntimeError("Git could not identify the target worktree")
    if repository.stdout.strip() != b"true":
        return

    tracked = git("--literal-pathspecs", "ls-files", "--error-unmatch", "--", "AGENTS.md")
    if tracked.returncode == 0:
        print("NOTICE: AGENTS.md is already tracked; Git ignore rules do not apply to it.")
        return
    if tracked.returncode != 1:
        raise RuntimeError("Git could not inspect the index")

    ignored = git("check-ignore", "--verbose", "-z", "--stdin", input=b"AGENTS.md\0")
    if ignored.returncode == 1 and not ignored.stdout:
        return
    if ignored.returncode != 0:
        raise RuntimeError("git check-ignore failed")
    fields = ignored.stdout.split(b"\0")
    if len(fields) != 5 or fields[-1] or fields[3] != b"AGENTS.md":
        raise RuntimeError("unexpected git check-ignore output")
    source, line, pattern = fields[:3]
    if pattern.startswith(b"!"):
        return  # Verbose Git output includes winning negations, not just ignores.

    # Escape control characters in user-owned rule paths/patterns before display.
    rule = ascii(os.fsdecode(source + b":" + line + b":" + pattern))
    print(
        "WARNING: AGENTS.md is untracked and ignored by Git; "
        f"matching rule {rule}. It remains on disk but is normally omitted from "
        "git status and ordinary git add, so it will not be shared in a commit "
        "without maintainer action. Ignore rules were not changed.",
        file=sys.stderr,
    )


try:
    check()
except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
    print(f"WARNING: cannot check Git ignore status for AGENTS.md: {ascii(str(exc))}; "
          "completed installation was preserved; ignore rules were not changed.", file=sys.stderr)
PY
then
  echo "WARNING: cannot check Git ignore status for AGENTS.md; completed installation was preserved; ignore rules were not changed." >&2
fi

if ((${#conflicts[@]})); then
  echo
echo "Some existing project files were preserved. Review/merge these if you want the template version:"
  printf '  - %s\n' "${conflicts[@]}"
fi

if ((CODEX_HOOKS_ENABLED || CLAUDE_HOOKS_ENABLED)); then
  cat <<'NOTICE'

Startup hooks are registered where provider settings allowed safe registration.
They only check for
.agent-maintenance/bootstrap-complete and required project documents, then print
a reminder; they do not run bootstrap, start a model, or fetch network content.
Codex/Claude Code may require you to review/trust the project hook before it
runs. See docs/installation.md.
NOTICE
fi

if ((${#hook_config_conflicts[@]})); then
  printf 'NOTICE: startup-hook registration was skipped for existing provider settings that need a manual merge: %s.\n' \
    "${hook_config_conflicts[*]}"
fi

if ((template_entry_conflict)) && [[ -n "$BOOTSTRAP_AGENT" ]]; then
  echo "ERROR: existing .agent-maintenance/flywheel-install.sh differs from this installer; refusing to bootstrap through an untrusted entry." >&2
  exit 3
fi

if ((manual_merge_required)); then
  echo "ERROR: installation incomplete: the reviewed AGENTS.md block was not applied because conditional replacement is unavailable; merge it manually." >&2
  exit 4
fi

release_target_lock() {
  local lock_fd="${SELF_EVOLUTION_FLYWHEEL_LOCK_FD:-}"
  [[ -z "$lock_fd" ]] && return 0
  case "$lock_fd" in
    *[!0-9]*)
      echo "ERROR: installer lock descriptor is invalid; refusing to launch bootstrap CLI." >&2
      return 1
      ;;
  esac
  ((lock_fd > 2)) || {
    echo "ERROR: installer lock descriptor cannot be safely closed; refusing to launch bootstrap CLI." >&2
    return 1
  }
  # The descriptor is supplied by our launcher and validated as digits above;
  # numeric eval keeps this close operation compatible with macOS Bash 3.2.
  eval "exec ${lock_fd}>&-"
  unset SELF_EVOLUTION_FLYWHEEL_LOCK_FD
}

if [[ -n "$BOOTSTRAP_AGENT" ]]; then
  [[ "$prompt_ready" == 1 ]] || {
    echo "ERROR: an existing prompts/bootstrap-project.md differs from the template; preserved it and will not launch it automatically." >&2
    exit 3
  }
  case "$BOOTSTRAP_AGENT" in
    codex)
      command -v codex >/dev/null 2>&1 || { echo "ERROR: codex CLI not found in PATH." >&2; exit 127; }
      echo "Starting Codex with prompts/bootstrap-project.md (workspace-write sandbox)."
      cd "$TARGET_DIR"
      release_target_lock || exit 3
      exec codex exec --sandbox workspace-write - < prompts/bootstrap-project.md
      ;;
    claude)
      command -v claude >/dev/null 2>&1 || { echo "ERROR: claude CLI not found in PATH." >&2; exit 127; }
      echo "Starting Claude Code with prompts/bootstrap-project.md."
      prompt="$(cat "$TARGET_DIR/prompts/bootstrap-project.md")"
      cd "$TARGET_DIR"
      release_target_lock || exit 3
      exec claude "$prompt"
      ;;
  esac
fi

echo
echo "Installation complete for: $TARGET_DIR"
if ((template_entry_conflict)); then
  echo "The existing .agent-maintenance/flywheel-install.sh differs from this installer; inspect and replace it before bootstrapping."
else
  echo "To initialize now: ./.agent-maintenance/flywheel-install.sh --bootstrap codex  OR  ./.agent-maintenance/flywheel-install.sh --bootstrap claude"
fi
