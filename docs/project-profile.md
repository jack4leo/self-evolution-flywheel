<!-- BEGIN SELF-EVOLUTION-FLYWHEEL PROFILE FRAMEWORK -->
# Project Profile (Maintained After Bootstrap)

> This is an index of the target project's facts, not template defaults. The initializing agent must verify and fill it from this repository's source code, configuration, CI, and historical documentation. Keep fields without evidence as `UNKNOWN`; record fields requiring product or permission decisions as awaiting maintainer confirmation.

<!-- END SELF-EVOLUTION-FLYWHEEL PROFILE FRAMEWORK -->

<!-- BEGIN SELF-EVOLUTION-FLYWHEEL PROFILE FACTS -->
## Identity and Conventions

- Project name / one-line purpose: `UNKNOWN`
- Default branch: `UNKNOWN`
- Documentation language: `UNKNOWN` (during bootstrap, verify the target repository's established language convention and confirm it with its maintainers)
- Time zone (reviews and scheduling): `UNKNOWN`
- Technology stack and supported platforms: `UNKNOWN`
- Main runtime entry points and deployment form: `UNKNOWN`

## Architecture and Verification

- Subsystem index: see [`subsystems/README.md`](subsystems/README.md)
- Formatting / static-check command: `UNKNOWN`
- Fast targeted tests: `UNKNOWN`
- Full test suite: `UNKNOWN`
- Build / packaging: `UNKNOWN`
- CI workflows and required checks: `UNKNOWN`
- Integration/real-environment verification: `UNKNOWN` (describe credentials, isolation, and a runnable safe method)
- Verification known not to run in CI: `UNKNOWN`
- Test isolation strategy (such as temporary HOME/data directories, credential handling, and temporary-resource cleanup): `UNKNOWN`
- Platform/client verification matrix and uncovered combinations: `UNKNOWN` (record only dimensions actually supported by or affected in the project)
- Investigation owner, record channel, and handling convention for known baseline failures: `UNKNOWN`

## Risk and Permission Boundaries

- Sensitive paths, secrets, and user data: `UNKNOWN`
- High-risk files/operations (such as release, migration, permissions, and CI): `UNKNOWN`
- Explicit product/compatibility decisions and their sources: `UNKNOWN`
- Agent write boundary: `UNKNOWN`
- Whether direct merge, release, or deployment is allowed: `UNKNOWN`; do not assume authorization when unconfirmed.
- Maintainer rules for rejecting/escalating external contributions: see [`contributors.md`](contributors.md); fill in according to the actual project policy.

## Review Scheduling and Watermark

- Daily review execution method / agent: `UNKNOWN`
- Run time / time zone: `UNKNOWN`
- Concurrent/overlapping runs, failure retries, timeout/interruption, and partial-write recovery strategy: `UNKNOWN` (verify against the project's actual scheduler and storage mechanisms)
- Review record archive location: `docs/reviews` is the fixed archive root; only the Markdown filename/date layout may be selected beneath this root. Other repository roots require separate code design and are not implemented here
- Canonical reviewed-through SHA: the single marker at the top of [`LESSONS.md`](../LESSONS.md); do not maintain a duplicate watermark here.
- Last review date: `UNKNOWN`
- Lesson promotion threshold: 3 different review dates by default; see [`LESSONS.md`](../LESSONS.md). Only maintainers may explicitly adjust it.

<!-- END SELF-EVOLUTION-FLYWHEEL PROFILE FACTS -->

## Update Requirements

- Verify relevant fields and update this file when bootstrap, CI/scripts, or the release method changes.
- Write only verified facts; retain source links or command paths (such as `package.json`, workflow files, and configuration schemas).
- Do not turn an unknown item into a fact by repeatedly copying it; record the source of maintainer-confirmed decisions and synchronize the routing entry point.
