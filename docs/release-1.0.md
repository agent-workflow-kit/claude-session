# 1.0 release checklist and draft notes

Prepared for issue #45. This file is working material for the maintainer who cuts 1.0; it
changes no behavior. `CHANGELOG.md` and the version in `pyproject.toml` belong to the release
job and are not edited here. The 1.0 commit is deliberately not part of this branch: it waits
for the human steps below.

## Criteria

The 1.0 criteria are in [plan.md, Version roadmap](plan.md#version-roadmap) and issue #45.

| Criterion | State | Evidence or what is left |
|---|---|---|
| Every command, flag, state, outcome, error code, exit code and schema in [interface.md](interface.md) is implemented and golden-tested | Automated for what is merged | `tests/test_interface_audit.py` parses interface.md and checks each against the parser, handlers, schemas and goldens; `tests/test_golden_commands.py` and `tests/test_golden_launch.py` add the missing golden files. Commands still on open pull requests are listed in `AWAITING` in the audit test and fail it as soon as they merge until they have goldens (see "Left for the 1.0 commit") |
| `make verify` passes; golden `--json` outputs validate against `schemas/` | Passes | `assert_golden` validates every golden output against its schema before comparing |
| `make live` passes on Linux and WSL2 against the current Claude Code | Human step | Billed; see [live-tests.md](live-tests.md). Never run by an agent or in CI |
| The macOS checklist has been run once | Human step | [macos-checklist.md](macos-checklist.md); its status line says "not run on macOS" until then |
| agent-spawn and review-run integrated against the interface without interface changes for one release cycle | Human step | agent-spawn depends on the surfaces listed below; none changed in this release |
| 1.0 released through a deliberate `!` commit | Last step | See "Cutting 1.0" |

## Surfaces consumers rely on

These are stable for the whole 1.x line (a change to one is a breaking change: a `!` commit
and 2.0). agent-spawn uses:

- `start --dry-run --json`: `taints`, `profile.reads_third_party`, `would_refuse`, `run_kind`, `repo`;
- `status --json`: `run.phase`, `run.reads_third_party`, `tainted`, `csession_dir`, `recent_events`;
- the exit 12 `launch_failed` error envelope with `csession_id` and `run`;
- per-invocation `--hook post-state-change=...`, stored for late delivery;
- `list --label`.

## Draft release notes

### claude-session 1.0

claude-session runs and supervises Claude Code sessions and records how each run ended as an
outcome code. 1.0 freezes the interface in [interface.md](interface.md) and the JSON schemas
under `schemas/`.

What 1.0 contains:

- `exec`, `start` and `resume` (including `--fork`, `--dry-run` and adoption of a pending run),
  `wait`, `status`, `list`, `kinds`, `report`, `stop`, `forget`, `reconcile`, `cron
  install`/`uninstall`, `label add`/`remove`, `transcript`, `export`, with `--json` output
  validated against `schemas/cli/`.
- Run states, outcomes and exit codes as specified in [States](interface.md#states) and
  [Outcome mapping](interface.md#outcome-mapping).
- Isolation: sandboxed kinds, permission profiles, path rules and taint tracking for
  third-party-reading kinds, with the human overrides `--allow-tainted` and
  `--override-escalation` (audited).
- Hooks: `pre-launch` gates (veto and escalate) and at-least-once `post-state-change`
  delivery.
- A live test suite (`make live`) for the Claude Code behavior the design relies on, and a
  manual macOS checklist.

Stability promise:

- Within 1.x the interface, the `--json` shapes, the exit codes and the state and outcome
  model only grow compatibly. A breaking change to session ids, the state or outcome model,
  the report protocol or any public schema waits for 2.0.
- Database migrations are additive within a schema major version
  ([Upgrades](interface.md#upgrades)). Upgrade while no sessions are live.

Known limits at 1.0 are listed in [Residual risks](interface.md#residual-risks).

## Left for the 1.0 commit

In this order:

1. Merge every open pull request of the stack (the maintainer does this, one by one, checking
   CI after each). After the last one, `tests/test_interface_audit.py` is the guide:
   - it fails while `AWAITING` names a command whose handler is real: empty `AWAITING`,
     add a golden file for each new schema (`UPDATE_GOLDEN=1`, review the diff);
   - it fails while the README table still marks a merged command as coming: update the
     table and the "Also coming in the same release" paragraph, and the "Status" section's
     "marked coming" wording; in the README also drop the `memoctl` and `claude` version
     notes once notes context (#38) and the version check (#40) are in.
2. Resolve the open questions in the handoff and re-run `make verify`.
3. Run `make live` on Linux and on WSL2 (billed), and the macOS checklist once; record the
   results in [live-tests.md](live-tests.md) and the checklist's status line.
4. Confirm agent-spawn and review-run have integrated without interface changes for a
   release cycle (issue #45).
5. Update `awkit-docs/interface-registry.md` for 1.0.

## Cutting 1.0

Only the maintainer, after the steps above. The release job moves a `0.x` version to `1.0.0`
only for a commit whose title carries `!` (`major_on_zero = false`); do not edit the version
or `CHANGELOG.md` by hand.

PR title (it becomes the squash commit, the version bump and the changelog line):

```text
feat!: release 1.0, freezing the claude-session interface
```

Commit body:

```text
Declares the interface in docs/interface.md and the schemas under schemas/ stable.
From here a change to the interface, a --json shape, an exit code or the state and outcome
model is a breaking change.

BREAKING CHANGE: claude-session 1.0 is the first stable release; the 0.x interface carries
no compatibility promise.
```

After the release job has tagged `v1.0.0`: close #45, check `claude-session --version` from an
installed copy, and tell the consumers (agent-spawn, review-run) which version to pin.
