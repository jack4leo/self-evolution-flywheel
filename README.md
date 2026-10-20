# Self-Evolution Flywheel

This repository is a reusable agent-workflow skeleton: routing, project facts, coding standards, contribution review, subsystem notes, Lessons, and review tooling. Project-specific facts and decisions must be verified in the target repository; placeholders are not evidence.

## Using It in a New Project

1. Clone this repository or create a GitHub **Use this template** copy. To install into an existing project, run `./install.sh` from this checkout, or `/path/to/self-evolution-flywheel/install.sh --target /path/to/project`.
2. Installation adds missing framework files and updates known framework files only when the target content matches the prior manifest hash (or the source content). It does **not** compare Git revisions as an ordered version number. The manifest records the source Git revision and per-file content hashes, so a modified checkout is identifiable even when its revision is not newer.
3. The target project's root `install.sh` is never installed, copied, or inspected as a framework source. Installation publishes the dedicated project entry `.agent-maintenance/flywheel-install.sh`; use an independent, reviewed source checkout when reinstalling or upgrading.
4. A valid managed block in `AGENTS.md` is updated automatically, without a confirmation prompt. A missing file is created. If an existing file has neither routing marker, installation fails closed and does not prepend a block; surrounding project text is never discarded. Malformed or ambiguous markers are preserved and reported.
5. `docs/project-profile.md` is special: only its framework section is replaceable. Its project-facts section and all other project content remain unchanged; missing or malformed profile markers fail closed.
6. Claude’s required `SessionStart` command is additive. A missing canonical hook is added automatically unless an existing command contains `session-start.sh` or `flywheel`; that ambiguity is shown as a diff and requires exact `y`/`Y` confirmation. Existing custom hooks are never deleted. Codex settings that need a hook require manual merging. Missing settings files are created, exact hooks are recognized, and one provider’s skip does not block the other.
7. Run bootstrap only when explicitly requested: `./.agent-maintenance/flywheel-install.sh --bootstrap codex` or `--bootstrap claude`. Startup hooks only validate the completion marker and required documents, then emit a reminder; they do not start an agent or fill documentation automatically.

See [`docs/installation.md`](docs/installation.md) for the safety precondition, ownership boundary, hook behavior, and recovery limits.

## File Map

- [`install.sh`](install.sh): source installer and bootstrap entry.
- [`docs/installation.md`](docs/installation.md): installation and recovery contract.
- [`AGENTS.md`](AGENTS.md), [`CLAUDE.md`](CLAUDE.md): project routing entry points.
- [`LESSONS.md`](LESSONS.md): evidence-backed project error log.
- [`docs/project-profile.md`](docs/project-profile.md): project facts and framework guidance.
- [`docs/code-standards.md`](docs/code-standards.md): general and project-specific standards.
- [`docs/contributors.md`](docs/contributors.md): contribution-review guidance.
- [`docs/subsystems/README.md`](docs/subsystems/README.md): subsystem index.
- [`docs/maintenance-loop.md`](docs/maintenance-loop.md): review and promotion loop.
- [`scripts/freeze-review-range.py`](scripts/freeze-review-range.py), [`scripts/write-frozen-review.py`](scripts/write-frozen-review.py), [`scripts/validate_lesson_promotions.py`](scripts/validate_lesson_promotions.py), [`scripts/watermark.py`](scripts/watermark.py): review-range and Lesson tooling.

## Ownership and Reinstall Boundary

The framework-owned set is fixed by the installer module: `.agent-maintenance/flywheel-install.sh`, `.agent-maintenance/installer-transaction.py`, `.agent-maintenance/framework-ownership.py`, `.agent-maintenance/session-start.sh`, `.github/workflows/lesson-promotion-gate.yml`, the four named `scripts/` files above, and `.agent-maintenance/framework-version.json`. This is not an arbitrary `scripts/*.py` wildcard. The manifest stores the source Git revision plus content hashes.

`README.md`, `docs/`, `prompts/`, `CLAUDE.md`, and `.github/PULL_REQUEST_TEMPLATE.md` are seed-only project content; the current installer does not seed or overwrite this README as part of its target file set. `AGENTS.md` has one separately managed routing block, and `LESSONS.md` remains project-maintained state. The target project's `install.sh` is project code and is never a framework target.

Updates preserve unknown conflicts rather than performing legacy migration. Recovery backups, rollback entries, and the transaction journal may be retained for manual inspection and restoration. The installer does not promise automatic atomic rollback or protection from non-cooperating writers. Keep the target tree single-writer throughout reading, proposals, publication, and recovery; unsupported filesystem or metadata capabilities remain explicit diagnostics and safe refusals, not reasons to fall back to unchecked writes.
