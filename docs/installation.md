# Installation and Initial Bootstrap

This template repository is named **self-evolution-flywheel**. The installer copies maintenance-mechanism files into the target repository and registers project-level `SessionStart` hooks for Codex CLI and Claude Code when the existing project settings allow safe no-overwrite creation.

## Usage

A copy created with GitHub **Use this template** already contains the documentation, prompts, and project-level hook configuration. To install into an existing repository, run a fixed-version template checkout's `./install.sh`:

```bash
./install.sh
```

### Run a one-line command directly in the target project

If the target project does not contain a template copy and you do not want to clone the template repository separately, run this in the **root of the target Git repository**:

```bash
curl -fsSL https://raw.githubusercontent.com/jack4leo/self-evolution-flywheel/main/install.sh | bash
```

Pipeline mode shallow-clones `main` into a temporary directory; the piped script only performs the clone and delegates, so the installer and template files come from the same checkout instead of a separately downloaded source archive that could be out of sync. Git verifies object hashes, but not commit signatures. The template files are installed into the current directory. Codex and Claude Code startup hooks are registered by default only when the target settings are absent or already contain the exact hook; if a valid pre-existing settings file needs a hook added, only that provider's registration is skipped, the other provider is still validated independently, and template files are installed. Bash, Git, Python 3, and curl are required. Use this command only after pipeline-install support has been merged into `main`. `curl | bash` immediately executes a remote script; inspect [install.sh](../install.sh) before first use. Codex/Claude Code may still require the user to trust the project settings. Pipeline mode obtains the latest `main` commit available at clone time; to pin a version or verify a commit signature, check out and review the desired commit and run that checkout's local `./install.sh`, rather than pinning only the raw script URL.

To start Codex Bootstrap immediately after installation (rather than only installing files and hooks), run:

```bash
curl -fsSL https://raw.githubusercontent.com/jack4leo/self-evolution-flywheel/main/install.sh | bash -s -- --bootstrap codex
```

Without `--bootstrap`, no agent is started and no project source is modified automatically. A subsequent startup hook only gives a reminder; it does not tell the agent to interrupt the user's task or fill in project documentation. Start initialization explicitly with the local installer as described below. The target directory must already exist, and when hooks are enabled it must be the root of a Git repository. The installer also publishes the template entry as `./.agent-maintenance/flywheel-install.sh`; use that path only for later bootstrap runs in the same target. The target's existing `./install.sh`, if any, is preserved as a business/project file and is never used as the template entry.

If the template repository is cloned separately into another directory:

```bash
/path/to/self-evolution-flywheel/install.sh --target /path/to/your-project
```

To explicitly start initialization now:

```bash
./.agent-maintenance/flywheel-install.sh --bootstrap codex
# or
./.agent-maintenance/flywheel-install.sh --bootstrap claude
```

`--bootstrap codex` uses `codex exec --sandbox workspace-write -` to read and execute `prompts/bootstrap-project.md`; `--bootstrap claude` uses that file as the first prompt in a Claude Code interactive session. The CLI must be installed, logged in, and callable from `PATH`. Codex's write scope is restricted to the workspace by the `workspace-write` sandbox; other permissions continue to follow the CLI's own configuration.

The installation script requires Bash, Git, and Python 3 (for safely merging JSON settings); a GitHub Template project already includes the files and hook configuration. Windows users should run the script in WSL or a Unix-like environment that provides Bash/Git/Python.

Use `--target DIR` to specify the target directory, or `--no-hooks` to copy files only without changing agent settings. The target directory must already exist; when hooks are enabled by default, the target must be the root of a Git repository. Non-Git directories are rejected; to install files only, explicitly use `--no-hooks`.

### Post-install Git ignore notice

After file and hook writes complete, the installer checks the target's `AGENTS.md`, including with `--no-hooks` and on repeat runs. For an untracked file, it asks Git for the effective ignore result rather than reading only `.gitignore`: repository and parent-directory `.gitignore` rules, `.git/info/exclude`, and the effective `core.excludesFile` (including Git's default global ignore file) participate with Git's normal precedence and negation behavior. An ignored file produces a warning with the winning rule's source file, line number, and pattern; a negated or non-matching rule does not produce an ignored-file warning.

The warning means that a newly installed or pre-existing **untracked** `AGENTS.md` remains on disk but is normally omitted from `git status` and ordinary `git add`, so it will not be shared through a commit unless the maintainer takes action. It does not mean that the local file was not installed or that an agent necessarily cannot read it. An **already tracked** file gets a separate notice: ignore rules do not apply to it and do not stop Git recording later changes. The installer does not use `--no-index` to misreport hypothetical rules as an actual tracked-file problem.

This check is advisory and read-only: it never edits ignore files, Git configuration, or the index, and ignore alone does not change the installation exit status or block optional bootstrap. Query failures produce a warning rather than undoing completed installation; a non-Git file-only target has no ignore check. The result is a snapshot of `AGENTS.md` at check time, not a check of every installed file or a guarantee against later rule/index changes. Existing installation conflicts still retain their usual exit status.

### The project-local entry is not an independent source

After installation, `.agent-maintenance/flywheel-install.sh` is a convenience entry for **this project root only**. It uses the files already installed in that project as its source; it cannot restore a deleted prompt, installer component, or other missing file from itself, and it cannot upgrade the project. It also does not require or copy the target project's business `install.sh`. If an installed file is missing or a newer version is needed, check out the desired fixed version into an independent source directory and run:

```bash
/abs/source/install.sh --target /abs/target
```

The project-local entry rejects `--target` pointing elsewhere so that an incomplete target cannot be mistaken for an independent template checkout. Do not describe this entry as a local “repair” or upgrade mechanism.

## Installation Contents and Overwrite Rules

The installer copies the root-level `AGENTS.md`, `CLAUDE.md`, `LESSONS.md`, the installer itself, `.agent-maintenance/session-start.sh`, `.agent-maintenance/installer-transaction.py`, `.github/PULL_REQUEST_TEMPLATE.md`, `.github/workflows/lesson-promotion-gate.yml`, all regular files under `scripts/`, and the files under `docs/` (excluding `docs/source-and-extraction.md`, which only describes the template's sources) and `prompts/`. It does not copy the template repository's `installer-tests.yml` or its test suite into a target project, and it does not replace the target project's own `README.md` with the template version. The promotion gate is separate so it can run against the installed validator without requiring the template's installer tests.

Existing target files whose content differs are preserved by default and listed as conflicts in the output; the installer does not silently overwrite them. `AGENTS.md` is handled specially: the installer displays a unified diff with the compact routing block inserted into a proposed copy, then reports that the maintainer must merge the reviewed block manually. It does not ask for a confirmation that it cannot honor. POSIX/Linux/macOS do not provide a portable atomic compare-and-replace that is safe against non-cooperative writers. The original `AGENTS.md` remains byte-for-byte unchanged and the installer exits with code `4` to make the unapplied routing change explicit; other conflict-free templates may already have been installed. A missing `AGENTS.md` can be safely created without overwriting an existing path; a file whose bytes already match the template is left unchanged.

The inserted block is delimited by `<!-- BEGIN SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->` and `<!-- END SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->`. Because of the concurrency limitation above, inserting or updating an existing block always requires a manual merge; notes outside the block, ownership, permissions, timestamps, extended attributes, ACLs, and hard-link relationships therefore remain unchanged. Terminal control characters in the diff are displayed using visible escape forms. Symlinks, non-regular files, or files with anomalous markers are not modified automatically.

If `.agent-maintenance/session-start.sh` already exists and differs while hooks are enabled, the installer exits before copying files or modifying settings; resolve the conflict and retry, or use `--no-hooks` to install other files only. Preflight independently checks both providers' settings structure and predictable write-permission failures before copying any template. A valid pre-existing provider settings file that lacks the exact command hook with a supported type and a matcher covering all session-start sources is preserved; registration is skipped only for that provider, while the other provider is still validated and may be registered. Structurally invalid JSON or SessionStart structure, a settings symlink, an unexplained hard link, or a non-regular settings path is rejected before template files are copied because the installer cannot safely establish what configuration it would be leaving untouched. Under the exclusive target lock, a rerun may recognize one provider-specific candidate link only when its name is unambiguous, it points to the active settings inode, its owner/group and mode match, its content is the exact generated payload, and the inode has exactly two links. These checks cannot prove who created the alias: a user-created hard link with matching name and metadata is indistinguishable from an interrupted publish. The installer therefore **never unlinks a candidate alias**; it reports the ambiguity, preserves the link, and permits the settings file only for a read-only/no-op retry when the corresponding hook is already present. Unknown or multiple candidate names, extra hard links, symlinks, and inode/content/owner/group/mode mismatches remain errors and are preserved for inspection. The installer creates a hook settings file with no-overwrite semantics only when that file did not previously exist. Repeated runs recognize existing hooks and do not add duplicates.

Installation writes are recorded in a transaction journal. Before publishing template files and newly created settings, the helper uses target-directory file descriptors; before and after writes, it re-resolves parent paths from the project root and rejects symlinks. A directory descriptor pins the directory inode but does not guarantee that the directory remains inside the project tree. A non-cooperative process can rename the directory between the final path check and the publish syscall. If the post-publication check detects that move, the installer reports an error and attempts rollback. If the target parent has moved outside the project tree or cannot be accessed safely, it preserves the files in that directory, reports incomplete rollback, and retains the journal rather than claiming that a directory descriptor prevents the race. Rollback also deliberately leaves transaction-created directories in place (even when empty): POSIX/Linux/macOS have no portable operation that atomically verifies a directory is still inside the target and removes it. This narrow race cannot be atomically blocked across those platforms; files moved externally may require manual inspection.

Rollback first checks the inode/content identity of files created by this transaction, then isolates them with a no-overwrite rename. Another process may still hold a writable file descriptor to the isolated inode, and POSIX provides no way to determine whether that descriptor will write later. Therefore, the installer does not unlink the isolated file: it preserves a `.name.rollback-*` recovery entry, marks rollback incomplete, and retains and reports the transaction-journal path. Transaction-created directories are likewise preserved and cause rollback to be reported as incomplete; they may remain empty. Concurrent replacement or writes, moved target directories, unavailable quarantine rename, and rollback failures are reported explicitly; uncertain data is preserved rather than automatically cleaned up. If a concurrent process has already deleted a newly created file, rollback does not recreate it. If the installer reports an incomplete rollback or retained recovery material, inspect the reported journal path and entries before retrying; do not remove or retry around uncertain rollback artifacts blindly.

The installer takes an exclusive advisory lock on the target directory while it performs installation and examines provider staging residue; a competing installer fails without modifying it. For the common case where one installer process is forcibly killed after publishing provider settings, rerunning can finish the installation if the remaining state passes the strict checks above. A single possible staging alias may remain indefinitely: it is deliberately not removed because pathname-based unlink cannot safely exclude a replacement user file, and its origin cannot be proved from metadata. Template files remain idempotent, hooks are not duplicated, and unrelated user settings are not rewritten. Bootstrap closes and clears the installer lock descriptor before executing the long-running Codex or Claude CLI, so a later installation is not blocked by the CLI process. Unrecognized or ambiguous residue is preserved and reported for manual inspection.

This is not a crash-consistent multi-file transaction. Abrupt process termination may still leave a partial installation; the documented retry guarantee is for ordinary single-installer SIGKILL recovery when the remaining state passes the strict checks above. Power loss, missing directory `fsync` guarantees, filesystem damage, concurrent external edits or non-cooperating writers, directory renames, and every possible rollback artifact are not automatically repaired. Failure to initialize the journal stops the installer before target files are written. A mid-transaction journal update failure triggers rollback; if recovery is incomplete, the installer preserves recovery entries and the journal and reports that status. If only the baseline snapshot of an existing `AGENTS.md` fails, that file remains unchanged and is reported as a conflict, while other independent templates may still be installed; inspect the conflict output to determine whether a manual merge is needed. Directory descriptors and identity checks do not provide absolute protection against non-cooperative writers or concurrent directory renames.

## What the Startup Hooks Do

- The Codex project hook is written to `.codex/hooks.json`; the Claude Code project hook is written to `.claude/settings.json`.
- On every session start/resume/clear/compact/fork, the hook accepts `.agent-maintenance/bootstrap-complete` only if it is a regular file containing a standalone `Reviewed HEAD: <40- or 64-character lowercase SHA>` line and the required project documents remain present and non-empty. Otherwise, it requires `prompts/bootstrap-project.md` to be a regular file and rejects a symlinked prompt or `prompts/` directory. If the prompt file is missing or symlinked, the script writes its diagnostic to stderr and exits with status `1`; it does not block by running bootstrap. Claude Code documents other nonzero hook exits as non-blocking errors, with the first stderr line used in the hook error notice for plain-text or empty stdout. Codex documents SessionStart plain-text stdout as additional developer context; its hooks reference does not specify exactly how stderr from a failed SessionStart appears in every CLI surface. The diagnostic therefore goes to stderr rather than normal SessionStart context. Tests exercise the script's streams and exit status, not either CLI's UI, so exact presentation in a real CLI is not claimed. If the prompt exists, the hook writes its reminder to stdout only, not an instruction to interrupt the user's task or populate documentation.
- The hook script does not start a sub-agent, execute the bootstrap prompt, inspect source code, write project documentation, or access the network. A startup hook can inject context but does not trigger a model turn by itself; interactive sessions usually require the user to send the first message. Therefore, to run the complete initialization, explicitly use `--bootstrap codex|claude`.
- Codex may require reviewing and trusting project hooks in `/hooks`; Claude Code may require the user to trust the project settings. Trust and confirmation are managed by each CLI, and the installer does not attempt to bypass them.

Treat hooks as part of the project code: inspect `.agent-maintenance/session-start.sh` and the generated settings before enabling them for the first time. The hook checks the completion marker, required documentation files, and whether the bootstrap prompt exists; it does not inspect the prompt's contents. When bootstrap is pending and the prompt exists, it prints a fixed reminder. Documentation is populated only after the owner explicitly starts `--bootstrap` (or otherwise explicitly requests bootstrap work).

An explicit request to integrate this template into an existing project or initialize the project authorizes bootstrap within that scope; the owner does not have to say the literal word "bootstrap". This does not authorize an unrelated business task to start initialization just because documentation or the completion marker is missing, invalid, or stale. In that case, continue the requested task using verified evidence, report relevant unknowns, and leave bootstrap documentation untouched.

Required documents are `AGENTS.md`, `LESSONS.md`, `docs/project-profile.md`, `docs/code-standards.md`, `docs/contributors.md`, and `docs/subsystems/README.md`. For the completion marker to suppress reminders, each must be a non-symlink regular file with readable content containing at least one non-whitespace character (the hook uses the C locale's whitespace classes). Directories, FIFOs, sockets, symlinks (including dangling links), missing files, empty files, and whitespace-only files invalidate completion. File type is checked before content so a static FIFO is never opened. Symlinked `docs/` or `docs/subsystems/` parents also invalidate completion. This is a structural check, not semantic validation of Markdown, project facts, or completion evidence; a documented `UNKNOWN` remains allowed. It is not a security boundary against concurrent non-cooperating filesystem replacements.

## Completion Marker

The bootstrap prompt requires the agent to write `.agent-maintenance/bootstrap-complete` only after completing the documentation review, verification, and report of remaining unknowns. The marker records the exact reviewed HEAD and completion evidence. Legacy non-empty markers without the `Reviewed HEAD:` line are treated as incomplete and should be recreated through an explicitly started bootstrap. The hook becomes active again if the marker is invalid or any required project document fails the file-type/content checks above. A valid marker means that initialization has run; it does not mean that all facts are known or that the maintainer has accepted pending decisions. If initialization is blocked or incomplete, the marker should remain absent so the hook continues providing a reminder at later startups.

## Installer Regression Tests

Run `python3 tests/test-installer-ignore.py -v` for the isolated post-install ignore checks (repository, local exclude, global rules, precedence/negation, tracked versus untracked files, and preservation of ignore/configuration files). These tests are also included in the full installer suite below.

The repository's `.github/workflows/installer-tests.yml` runs `bash tests/test-install.sh` on pushes, pull requests, and manual triggers. The suite uses isolated temporary directories and covers the complete Codex/Claude preflight state matrix (normal, provider-local manual-merge conflict, invalid settings), wrong hook type and narrow matcher recognition, metadata preservation, failures during template copying and provider writes; preservation of concurrent external changes; refusal to replace `AGENTS.md` or settings files without asking for a fake confirmation; journal initialization and mid-transaction update failures; `AGENTS.md` baseline-snapshot failure; secondary rollback failures; rejection of symlink parent directories and settings symlink races; conservative preservation of transaction-created directories; marker HEAD/document validation and a nonzero stderr diagnostic for a missing startup prompt; rejection of an external startup-prompt symlink; help/documentation consistency; deterministic syscall-boundary comparisons between the original PR head and the current implementation; real SIGKILL after both Codex and Claude publish their settings, followed by successful idempotent retries; preservation of candidate aliases and user metadata; extra/ambiguous links, symlink, content, owner and group rejection; candidate-path replacement attempts; lock contention; and lock release before a long-running bootstrap CLI. Run `bash tests/test-install.sh` locally.

## Official Behavior Documentation

- [Codex project-level `AGENTS.md` instructions](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Codex hooks](https://developers.openai.com/codex/hooks)
- [Codex `exec` non-interactive mode and sandbox](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Claude Code hooks](https://code.claude.com/docs/en/hooks)
- [Claude Code hooks guide](https://code.claude.com/docs/en/hooks-guide)
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)
