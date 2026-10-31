# claude-session: implementation plan

Status: planned, 2026-10-29. Implements [interface.md](interface.md) (normative) using the internal design in [design.md](design.md). Work is tracked as GitHub issues in this repository; each issue names its stage below. When an issue is picked up, its implementer expands it into bite-sized test-first steps (write the failing test, see it fail, implement, see it pass, commit) before writing code.

## Conventions for every stage

- Test-first. Every behavior in a task starts as a failing test.
- All subprocesses go through `src/claude_session/gateway.py`; tests replace it. Time comes from an injectable clock.
- Every `--json` output gets a golden-file test validated against its schema in `schemas/` (with a `referencing` registry keyed by `$id`).
- Real-Claude tests are marked `live`, are deselected by default, and never run in CI (`make live` runs them locally).
- `make verify` passes before every commit. PR titles are Conventional Commit headers; no `!` before 1.0.
- New runtime dependencies: only `pyyaml` and `pydantic` (design guide section 3). Test-only: `jsonschema`, `referencing`, `rfc3339-validator`, `pyyaml` (for `config.example.yaml`).

## Module map

The plan splits six modules out of design.md's module list to keep files focused: `ids` (from `cli`/`store`), `endmap` (from `supervisor`), `isolation` (from `settings_builder`), `lifecycle` (from `supervisor` and `reconcile`), `notes` (from `hookevent`) and `secretindex` (from `outbox`). Responsibilities are unchanged.

| Module | Stage | Responsibility |
|---|---|---|
| `cli.py` | 0 | argparse surface, dispatch, `--json` envelopes, exit codes; dispatches `hook-event` before heavy imports |
| `ids.py` | 0 | `cs-` ids, prefix matching, label/kind/repo/skill grammars |
| `config.py` | 0 | YAML + `config.d` merge, Pydantic models, env overrides, precedence |
| `store.py` | 0 | SQLite WAL, numbered migrations, leases, all queries |
| `gateway.py` | 0 | the only subprocess boundary (`claude`, `git`, notes command, `crontab`, sandbox prerequisite checks) |
| `procinfo.py` | 0 | process start time and liveness (Linux `/proc`, macOS `ps`) |
| `logs.py`, `audit.py` | 0 | `log.jsonl`, `audit.jsonl` writers |
| `kinds.py` | 1 | effective profile resolution with origins |
| `envbuild.py` | 1 | session environment |
| `sessiondir.py` | 1 | session/private dirs, secret, prompts, inputs, `run.json`, instructions, no-follow writes |
| `settings_builder.py` | 1, 3 | generated settings, `claude` argv (pure) |
| `hookevent.py` | 1 | injected-hook fast path |
| `outbox.py` | 1 | `report` write side and ingestion |
| `endmap.py` | 1, 2 | headless and interactive outcome mapping |
| `supervisor.py` | 1, 2 | `exec`/`resume` process supervision |
| `toolhooks.py` | 2, 4 | `pre-launch` and `post-state-change` execution and delivery |
| `isolation.py` | 3 | path rules, session-writable paths, PATH sanitizing |
| `secretindex.py` | 3 | `contains_secret` keyed q-gram index: build, read, streamed scan (stdlib only: on the hook path) |
| `lifecycle.py` | 4 | start/adoption, resume/fork carry-over, taint, stop, forget, liveness |
| `reconcile.py` | 4 | reconcile, retention, cron block |
| `transcripts.py` | 5 | transcript discovery and rendering |
| `notes.py` | 5 | notes-context command |
| `tests/fakebin/claude` | 1 | scenario-driven fake Claude Code |

## Stage 0: Foundations

**Goal**: A package skeleton that every later stage builds on: CLI shell, config, state store, ids, gateway, logs.
**Success Criteria**: `claude-session --version` works; `list --json` on an empty store prints `{"sessions": []}`; invalid config fails with a message naming the key (exit 2); schema-newer databases are refused with `schema_newer` (exit 12); `make verify` green.
**Tests**:

- config: defaults equal `config.example.yaml`; drop-in merge (maps deep-merged, lists replaced, hook lists appended); env overrides only for the documented scalar keys; precedence for top-level scalars: flags > env > config > built-in; for kind settings: flags > previous run's flags > repo > config kind > built-in.
- ids: allocation format and collision retry; prefix matching (≥4 chars, ambiguous → 21, forgotten excluded); label, kind, repo-id, skill grammars (valid and invalid cases).
- store: migrations apply in order under the `migrate` lease; leases (take, renew, expiry, dead-holder takeover by PID + start time); `BEGIN IMMEDIATE` with busy timeout.
- procinfo: start time on Linux; PID-reuse detection.
- cli: `--json` error envelope shape for usage (2), not found (10), ambiguous (21); `--yes` accepted as a no-op.
- schema harness: every file under `schemas/` passes `check_schema`; `config.example.yaml` validates.

**Status**: Not Started

## Stage 1: Headless core (0.1)

**Goal**: `exec --mode headless` runs a session end to end with the fake Claude, records state from injected hooks, ingests reports through the outbox, applies the backstop and the headless outcome mapping.
**Success Criteria**: with the fake Claude, a headless run that reports `done` exits 0 with a frozen artifact; an unreported clean run becomes implicit done (or `exited-unreported` with `implicit_done: false`); a run past `--max-turns` exits 16; `status --json` and `list --json` match their schemas.
**Tests**:

- fake Claude (`tests/fakebin/claude`): reads a scenario file; parses the generated `--settings`; runs the injected hook commands with realistic payloads (SessionStart sources, UserPromptSubmit, PostToolUse incl. inside a subagent, Stop with and without `background_tasks`, SessionEnd); runs `claude-session report` like an agent; prints stream-json result objects per turn; exits, hangs or ignores signals on cue.
- kinds: built-in kinds equal the documented table; unknown kind → review profile + warning; per-repo overrides; per-call flags; origins.
- envbuild: inherit vs allowlist; `claude_auth_env` (Bedrock/Vertex conditional); `secret_env` stripping; third-party extras; `CSESSION_*` set; `path_prepend`.
- sessiondir: layout and modes (0700/0600); secret rotation and SHA-256; prompt and input copies (no-follow, caps); `run.json` contents; `instructions.md` has no per-run values.
- settings_builder (trusted kinds): injected hooks with `-I` interpreter command and timeouts (PostToolUse/Stop 60 s, others 30 s); report allow rules (bare and absolute path); Edit+Write rules for artifacts/outbox; explicit `defaultMode`; `--system-prompt-snapshot off`; bootstrap prompt forms (skill first).
- hookevent: every row of the transition table; Stop with background tasks keeps `working`; backstop blocks once per run; SessionStart records ids/sources/transcript paths and emits the context line; never fails a session on internal error.
- outbox: `report` validation (summary/reason rules, artifact name or path form, size caps, state root from `run.json`, never opens `state.db`); UUIDv7 names; claim by rename; secret check; artifact copy-then-scan and freeze with SHA-256; rejection feedback through PostToolUse; drain caps; latest valid wins.
- endmap (headless): every row of the headless precedence, `is_error`, `error_max_turns`, complete-result detection, cost and turns summed over result objects.
- supervisor (headless): spawn with stdout to `result.json`, own process group, `/dev/null` stdin, `PR_SET_PDEATHSIG` on Linux; final drain before mapping; exit codes.

**Status**: Not Started

## Stage 2: Interactive mode and supervision (0.2)

**Goal**: Interactive sessions in a terminal with permission/escalation states, auto-exit, signals, bounds, `wait`, lifecycle-hook delivery and outcome files.
**Success Criteria**: an interactive fake session goes `working` → `needs-input(permission)` → `working` → `reported` → auto-exit, closing with exit 0; Ctrl-C does not count as a kill; SIGTERM to `exec` ends `killed` (15) unless a report exists; `wait` returns the run's outcome; `post-state-change` fires once per real change with `event_id`; the outcome file is written exactly once.
**Tests**:

- signals table (interface "Signals"): per mode and phase; `pending_cause` written before forwarding; SIGKILL after `kill_grace` to the same verified PID.
- auto-exit: only after a `done` report and a Stop with no background tasks; delay; re-check before SIGTERM.
- `PermissionRequest` → `needs-input(permission)`; AskUserQuestion → `question`; denied prompt stays until UserPromptSubmit; sticky escalation; display priority.
- interactive end mapping table (user exit, normal, `exit_<code>`, `signal_<name>`, `no_session_start`).
- timeouts and `interactive_timeout`; `--timeout none`; `session_start_timeout` warning (headless).
- `wait`: binds to the current run; `--run`; `--timeout` (22, `timed_out`, `pending`); forgotten mid-wait → 10.
- toolhooks: `post-state-change` selectors (kinds, exclude_kinds, labels at event time, states); background worker order; delivery lease; at-least-once with `event_id`/`event_ts`; hook process group SIGTERM → SIGKILL on timeout; hook cwd is the state root.
- outcome file: written by the finalizer right after the finalization CAS; backfill; write failure is a warning; never written to a rejected path; mode 0600; no-follow.

**Status**: Not Started

## Stage 3: Isolation (0.3)

**Goal**: Kind profiles, permission profiles, sandbox generation and every path rule, so third-party-reading kinds run confined.
**Success Criteria**: `exec --dry-run --json` for `review`, `gate-check`, `respond` and `rank` produces the documented settings; launches with unsafe inputs fail with the documented launch end reasons; `kinds --json` reports sandbox availability and `claude_version_ok`.
**Tests**:

- settings_builder (isolated): `denyRead ["/"]` + the generated `allowRead` list; TMPDIR in `allowWrite` only; protected-file denies; `.git` deny; Claude-config denies under cwd, `$CSESSION_DIR` and add-dirs; git dir read-only with `GIT_OPTIONAL_LOCKS=0`; `autoAllowBashIfSandboxed: false`; `dontAsk` forced; conservative Read denies for credential locations; `--add-dir` for `$CSESSION_DIR` and plugin dirs with write denies; `--setting-sources`, `--strict-mcp-config`; `GIT_CONFIG_COUNT` hardening env.
- profile validation: refused keys; variable expansion; empty expansion fails; name vs file detection; file snapshot carried on resume.
- isolation path rules: unsafe write targets (with own `work/` exception); `read_dirs`/plugin dirs/generated allowRead refusals (`unsafe_read_dir`); session-writable paths definition; PATH sanitizing and absolute tool resolution (`unsafe_path`); outcome-file path rejection; `unsafe_cwd`; `unsafe_claude_args` allowlist; `no_git_cwd`; `git_dir_changed`; credential-in-git-config warning.
- sandbox self-check: prerequisites per platform; socket path length; `tmpdir_unsafe` checks (lstat, owner, mode, no symlink).
- contains_secret: q-gram index built without plaintext; hits in summary, reason, artifact, implicit `result.md`; 10 MiB performance budget.
- `--reads-third-party` implications; `sandbox_lowered` audit and warning.
- `kinds --json` and `exec --dry-run --json` golden files.

**Status**: Not Started

## Stage 4: Session lifecycle (0.4)

**Goal**: The full lifecycle: `start` and adoption with pending runs, resume and fork with carry-over, taint, escalation override, `pre-launch` hooks, `stop`, `forget`, liveness, `reconcile`, retention and `cron`.
**Success Criteria**: `start` → place → `exec --session-id` works and is a single-winner CAS; resume rebuilds settings per the carry-over table; a fork may take a new `--cwd`; tainted sessions refuse non-third-party resumes without a human `--allow-tainted`; `stop` ends live and pending runs correctly; `reconcile` finalizes dead runs exactly once and delivers their events.
**Tests**:

- launch order steps 1–7, each failure's allocation behavior and exit code; `start` stores and freezes the spec, config path and per-invocation flags; adoption re-validation; `expired`, `already_adopted`; `never_adopted` after `lost_grace`.
- carry-over table rows incl. `--reset`, kind change, mode change, `env_allow` vs `env_allowlist_only`; fork defaults, fork cwd rules, fork input copy (no-follow, regular, nlink 1); resume of a session without a captured Claude id starts fresh.
- taint: committed at allocation; forks copy it; refusal and `--allow-tainted` (TTY or explicit human actor); audit `resume_tainted`.
- `pre-launch`: selectors; veto/escalate/timeout semantics; `decision_id` recording; `--override-escalation` targets only recorded escalating entries; audit `launch_escalation_override`.
- stop: wrapper then child SIGTERM, SIGKILL child first after grace, `stop_requested_at` → `stopped`; pending run → `canceled`; CAS race with adoption; output fields.
- forget: refuses live and pending new-session records; bulk `--older-than` with filters; audit per session; hides from prefix matching.
- liveness (status, list, wait, resume, start --resume, stop, forget): finalize only when the wrapper is dead; `wrapper_lost`, `lost`, `launch_interrupted`, `finalized_from_result`, `finalized_from_cause`; status/list never run hooks.
- reconcile: lease; outbox salvage for the finalized run; retention deletion (fd-based) incl. tmpdirs; `orphans` array; delivers undelivered events.
- cron install/uninstall: managed block, env-file checks, idempotence.

**Status**: Not Started

## Stage 5: Transcripts, notes, audit, labels, upgrades (0.5)

**Goal**: The remaining surface: `transcript`, notes context, `export`, `label add/remove`, `verify-secret`, minimum-version and upgrade behavior.
**Success Criteria**: every command in interface.md exists with golden `--json` output; the remaining interface sections are implemented.
**Tests**:

- transcripts: discovery across `/clear`, resume and fork; subagent files and meta; offloaded tool results; `--raw`, `--paths`, `--subagents`, `--run`; control-character stripping in text mode; missing files.
- notes: argv form (`--repo=`, `--label=`), no shell; fencing with a nonce; timeout and size cap; exit 0 and 10 are success; failures never block; sources filter.
- export: filters; schema.
- labels: multiset semantics, grammar, `kind=` label at creation.
- verify-secret: full id only; adopted and live run only; whitespace stripping; constant-time compare; exit 0/11.
- min Claude version check (`claude_too_old`); hook interpreter path missing → warning; `schema_newer`.

**Status**: Not Started

## Stage 6: Live verification and 1.0 readiness

**Goal**: The `live` suite covers every Claude Code behavior the design relies on, and the tool is ready for its first consumers.
**Success Criteria**: `make live` passes on Linux and WSL2 against the then-current Claude Code; the macOS checklist has been run once; agent-spawn and review-run have integrated against the interface; the 1.0 criteria below hold.
**Tests** (`live`):

- every row of design.md's spike and live-verification tables (plugins, settings merge, resume/fork ids, Stop block, stream-json results and costs, snapshot, prompt forms, permission and question prompts, signals, credential isolation, setting sources and auto mode, strict MCP incl. plugin MCP, sandbox reads/writes/deny-root/TMPDIR, git read-only in linked worktrees and main checkouts, `autoAllowBashIfSandboxed`, planted skills, hook timeouts, parallel hook ordering);
- still-open items: Read/Grep/Glob on a planted symlink (F1), `/proc` isolation (F2), MCP elicitation detection, macOS Seatbelt equivalents.
- end-to-end with real Claude: headless review-like run with an artifact; interactive implement-like run with auto-exit; respond-like fork into another directory.

**Status**: Not Started

## Version roadmap

Issues are tied only to major milestones (`0.x`, `1.x`, `2.x`); minor and patch releases follow from Conventional Commit titles.

- **0.x**: stages 0–6. Minor versions arrive as features merge; the stage numbers above are an ordering, not version promises.
- **1.0** when: every command, flag, state, outcome and schema in interface.md is implemented and golden-tested; `make live` passes on Linux and WSL2 against the current Claude Code; the macOS checklist has been run once; agent-spawn and review-run have integrated against the interface without interface changes for one release cycle. Reaching 1.0 is a deliberate breaking-change commit.
- **2.0**: reserved for a breaking change to session ids, the state or outcome model, the report protocol, or any public schema. None is planned.
