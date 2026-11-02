# Live tests

The `live` suite (`tests/live/`) runs the real `claude` binary against the Claude Code behaviors that [design.md](design.md) and [interface.md](interface.md) rely on, as far as a test on one Linux host can check them. The [coverage](#coverage) section maps each behavior to its test, and lists what is not covered, with the issue that owns it (#42 for the remaining Linux items, #43 for the macOS checklist). Run it after every Claude Code upgrade and before a release. It is never part of `make verify` or CI.

## Running it

```sh
export ANTHROPIC_API_KEY=...     # or CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`)
make live                        # all live tests
make live PYTEST_ARGS='-k hooks' # a subset
```

`make live` sets `CSESSION_LIVE=1` and selects the `live` marker. Without that variable every live test skips, even under `pytest -m live`; `addopts` in `pyproject.toml` deselects the marker in a normal run, and `tests/live/conftest.py` marks everything in the directory, so a test cannot leak into CI by forgetting the marker.

| Variable | Meaning |
|---|---|
| `CSESSION_LIVE=1` | Opt-in. Required. |
| `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` | Credentials passed to the private environment (first one found). An API key is also marked approved in the private `~/.claude.json`, so interactive sessions do not stop at Claude Code's custom-key dialog. |
| `CSESSION_LIVE_COPY_LOGIN=1` | Use a copy of `~/.claude/.credentials.json`. It is copied once per pytest session into a private directory; tests that need credentials get it installed into their private home, and a token Claude refreshes there is carried to the next test (never written back to the real home, which is only read). An OAuth refresh can still rotate the refresh token and sign your real installation out, so prefer a token. |
| `CSESSION_LIVE_MODEL` | Model for runs (default `haiku`). |
| `CSESSION_LIVE_RUN_USD` | `--max-budget-usd` of each `-p` run, and the estimate charged for a run whose cost is unknown (default 0.25). |
| `CSESSION_LIVE_MAX_USD` | Cost cap of one pytest process (default 3): once the charged total reaches it, the rest of the suite skips. Do not split the suite across processes (no `pytest-xdist`): each would have its own cap. |
| `CSESSION_LIVE_CLAUDE` | A `claude` other than the one on `PATH`. |
| `CSESSION_LIVE_OBSERVATIONS` | Append the recorded observations (below) to this JSON-lines file. |

Tests that need a login skip without one. The zero-cost tests (`--version`, `--help`, the cloud-provider switch values with an invalid key) need only the binary, and get no credentials at all.

## Cost

Every path that starts `claude` with credentials passes one spend gate first (`LiveEnv.spend_gate`; the test skips once the cap is reached) and is then charged:

- headless (`-p`) runs, run to completion or spawned: the cost their `result` objects report; the estimate (`CSESSION_LIVE_RUN_USD`) when the run timed out, was killed, or its output has no cost. Every `-p` argv carries `--max-budget-usd` (`LiveEnv.budget()`; `claude_run` refuses a `-p` argv without it).
- interactive sessions (tmux): the estimate, charged when the session starts. `--max-budget-usd` works only with `--print`, so an interactive session has no per-run cap.
- end-to-end runs through `claude-session`: the `cost_usd` of the outcome, else the estimate. Headless `implement` runs get `--max-budget-usd` through the kind's `claude_args` in the test config. Isolated kinds (review) accept only `--verbose` and `--debug` there, and interactive runs cannot take it, so those runs have no per-run cap.

## Isolation and safety

- Each test gets a private root directly under `/tmp` (short, mode 0700: Claude's unix socket names must fit) with its own `HOME`, `XDG_*`, `TMPDIR` and working directories. The child environment is built from an allowlist (locale, proxy and certificate settings, one credential), never from the parent. A test's extra variables may not set any name the private environment sets except `PATH` (so no `HOME`, `TMPDIR`, `XDG_*`, `GH_CONFIG_DIR`, `GLAB_CONFIG_DIR`, `GIT_CONFIG_*`, `CLAUDE_CONFIG_DIR` or the Claude Code switches it sets). A run without credentials (`auth=False`) uses a second private home that never holds a login file. The real `~/.claude` is not written.
- `tests/conftest.py` gives live tests a private `HOME` too; the real one is read at import only to find the login file `CSESSION_LIVE_COPY_LOGIN` asks for. Its process watchdog allows a live test 3600 s (`LIVE_WATCHDOG_SECONDS`) instead of 60.
- `claude` runs through the gateway: a timeout ends exactly the process group it started. A spawned headless run has its own process group, killed at teardown while its leader is still unreaped. Signal tests send to the exact child they spawned; interactive ones to the tmux pane PID, after checking that it runs `claude` (`/proc/<pid>/exe`) and pinning its start-time token, and every signal goes through that token. A process a hook leaves behind records its own PID and start-time token as it starts (`LiveEnv.pid_record_command`), and is killed only while that token still matches.
- Interactive tests use a private tmux server (`-f /dev/null`, a socket in the private root, the private environment) and stop it by that socket, also after a failed start.
- Representations of the environment objects never show the parent environment or a credential (pytest prints them on a failure).
- Isolated-run tests need `link-mode = "copy"` in the dev venv, as `pyproject.toml` sets.
- Sandbox tests (`needs_sandbox`) skip where bubblewrap or socat is missing; two tests need the opposite (no sandbox) and skip where one is installed.

## Observations

Some checks are open questions (an exit code, a payload shape, a message text). Those tests assert what the design tolerates and record the value seen with `live.observe(key, value)`: printed in the terminal summary and, with `CSESSION_LIVE_OBSERVATIONS`, written to a file. After a run, compare the observations with the fake claude's assumptions (`tests/fakebin/fake_claude.py`) and with the table rows named in the tests, and update them. The open follow-up in code is the `TODO(#42)` at the `post_tool_use` row of `src/claude_session/hookevent.py` (#58 item 1).

## Extending (issues #42 and #43)

- New API-using tests take the `live` fixture, zero-cost ones `cheap`; both come from `tests/live/conftest.py`. Put helpers in `tests/live/harness.py` and unit-test them in `tests/test_live_harness.py`, with the fake claude or `sh`.
- `LiveEnv.headless(prompt, *flags)` runs a small-model `-p` with `stream-json`, the prompt after `--`. `LiveEnv.claude_run` runs with exactly the given argv (add `*live.budget()` to a `-p` argv). `spawn_headless` and `tmux_driver.Tmux` are for signals and interactive sessions; a new path that starts `claude` must call `spend_gate()` and `charge()`.
- Probe hooks are `tests/fakeclaude.recorder(log, ...)` commands in a `--settings` file; read them with `harness.read_log` and `harness.events_named`. Read what tools returned with `harness.session_tool_results`, never the whole transcript (it holds the prompt).
- Sandbox tests use `needs_sandbox` (`test_sandbox.py`); macOS-only checks belong in the [manual macOS checklist](macos-checklist.md) (#43), which also says what to read from this suite on a Mac.
- New rows go in the tables below, with an owner when not covered.

## Coverage

"Never run" means no human has run the test yet against a real Claude Code; every test below is in that state until the first `make live` run (the harness itself is unit-tested). Zero-cost probes of the version and `--help` flags were run once against 2.1.316.

### Spike and live-verification tables of design.md

| Row | Test |
|---|---|
| `--plugin-dir` additive; manifest name; duplicate: first wins; skill with arguments (A5) | `test_headless.py`: plugin tests |
| `--settings` hooks merge; deny beats allow (with an allow-only control) | `test_settings.py::test_settings_hooks_merge_with_user_and_project_hooks_and_a_deny_beats_an_allow` |
| `--resume` forgets plugin dirs (judged from the init event) | `test_headless.py::test_resume_does_not_remember_plugin_dirs` |
| Session id on resume; `--session-id`; fork ids and transcripts; resume from another cwd (A6); interactive fork (B1, headless half) | `test_headless.py::test_resume_keeps_the_id_...`, `test_a_preassigned_session_id_...` (the fork's project directory is observed) |
| Stop-hook block (JSON and exit 2) | `test_hooks.py::test_a_stop_hook_can_block_once_...` |
| `-p` result fields; error turn limit | `test_headless.py::test_result_object_...`, `test_the_turn_limit_...` |
| Background subagents: several results (A1) | `test_headless_signals.py::test_background_subagents_...` |
| SessionStart sources and context delivery | `test_headless.py` (resume/fork), `test_interactive.py::test_clear_changes_the_id_...`, `test_hooks.py::test_session_start_plain_stdout_...` |
| Prompt delivery: argv plus stdin | `test_headless.py::test_argv_prompt_and_stdin_are_concatenated` |
| `--append-system-prompt-file`, `--max-turns`; snapshot (A3) | `test_headless.py::test_append_system_prompt_file_...`, `test_system_prompt_snapshot_off_...` |
| Permission prompts (a writing command, in `default` mode), notification timing, denial fires nothing, `idle_prompt` | `test_interactive.py::test_permission_prompt_notifications_...` |
| Auto-exit by signal (SIGTERM 143 with SessionEnd `other` and a resume, SIGHUP, Ctrl-C) | `test_interactive.py::test_signals_...`, `test_double_ctrl_c_exit_code` |
| `--setting-sources`: `""` mode, CLAUDE.md, bypass downgrade (trusted directories; one untrusted control observed) | `test_settings.py` |
| `--strict-mcp-config` (incl. plugin MCP, D4) | `test_headless.py::test_strict_mcp_config_...` |
| Hooks and the environment | `test_hooks.py::test_hook_payload_environment_cwd_and_shell` |
| Stop with background tasks | `test_headless_signals.py::test_background_subagents_...` |
| Read tool vs sandbox; `blockReadsOutsideWorkingDirectories` (with a control without the key) | `test_settings.py::test_block_reads_...` |
| Subagent tool use (A2); parallel hook order (A7: every PostToolUse hook ends before Stop starts; whether the Reads were parallel is observed) | `test_hooks.py` |
| `PostToolUse` block (A4) | `test_hooks.py::test_a_post_tool_use_block_reason_...` |
| Report command forms (A8) | `test_isolation.py::test_report_command_forms_under_dont_ask` |
| A `Write(...)` allow does not permit the Write tool, `Edit(...)` does (A8) | `test_settings.py::test_a_write_allow_rule_...` |
| Default mode denies in `-p` (A9) | `test_headless.py::test_in_default_mode_...` |
| Hook timeout (A10): seconds, SIGTERM at the timeout (SIGKILL escalation observed) | `test_hooks.py::test_hook_timeout_is_in_seconds`, `test_a_timed_out_hook_...` |
| Trust dialog (B2): appears, no hooks, Ctrl-C ignored, Esc exits 0 | `test_interactive.py::test_the_trust_dialog_...` |
| AskUserQuestion (B4) | `test_interactive.py::test_ask_user_question_...` |
| Idle prompt (B6) | `test_interactive.py::test_permission_prompt_notifications_...` |
| Missing-path `denyWrite` for Bash and the Write tool (C1) | `test_sandbox.py::test_a_deny_write_of_a_missing_path_...` (needs sandbox) |
| Nested deny inside an allowed parent (C3) | `test_sandbox.py::test_a_nested_deny_inside_an_allowed_parent_...` (needs sandbox) |
| Plugin dir via `--add-dir` (C6) | `test_isolation.py::test_a_plugin_dir_is_readable_...` |
| Git dir read-only, main and linked (D1, D2): `git status` runs, writes fail | `test_sandbox.py::test_a_denied_git_dir_...` (needs sandbox) |
| `autoAllowBashIfSandboxed` (D3) | `test_sandbox.py::test_auto_allow_bash_...` (needs sandbox) |
| Deny-root reads, TMPDIR (E1) | `test_sandbox.py::test_deny_root_...`, `test_e2e.py::test_a_review_run_in_the_sandbox_...` (need sandbox) |
| Planted skills (E2): top-level asserted; nested asserted when its control loads it, else observed | `test_isolation.py::test_planted_project_...` |
| Respond fork into another directory: recorded transcript path is the fork's real transcript | `test_e2e.py::test_a_respond_like_fork_...` |
| `contains_secret` on 10 MiB, ingested by the hook before the Stop | `test_e2e.py::test_a_10_mib_artifact_...` |
| Outbox/artifact symlink and hard link | `test_e2e.py::test_symlink_and_hard_link_artifacts_...` |
| Headless implement run files its report; interactive run auto-exits | `test_e2e.py` |
| Review-like run: report filed, private files (settings, `prompt.md`, `run.json`) unwritable | `test_e2e.py::test_a_review_run_without_the_sandbox_...` (any host), `test_a_review_run_in_the_sandbox_...` (needs sandbox; also placeholders and a clean checkout) |

### Testing list of design.md

| Item | Test or owner |
|---|---|
| Glob, Grep and LS under the review profile | #42 |
| `--strict-mcp-config` drops a `--plugin-dir` plugin's MCP servers | `test_headless.py::test_strict_mcp_config_...` |
| MCP elicitation (`elicitation_dialog`) | #42 |
| Background subagents: last `result` taken, cost and turns summed | Raw stream: `test_headless_signals.py::test_background_subagents_...`; the product's summing: #42 |
| Report forms under `dontAsk` | `test_isolation.py::test_report_command_forms_under_dont_ask` |
| A trusted run with `--setting-sources ""` does not start in `auto` | `test_settings.py::test_empty_setting_sources_...` |
| An injected hook finishes within its timeout under a full outbox and a full-size artifact | Full-size artifact: `test_e2e.py::test_a_10_mib_artifact_...`; full outbox: #42 |
| AskUserQuestion sets `needs-input(question)` | Claude Code half: `test_interactive.py::test_ask_user_question_...`; the product state: #42 |
| A bundled plugin skill reads its own reference file in a review session and cannot write the plugin dir | Claude Code half: `test_isolation.py::test_a_plugin_dir_is_readable_...`; through the product: #42 |
| `report` from a uv tool install inside the sandbox (C5) | #42 |
| The agent cannot modify any private-dir file | `test_e2e.py` review runs (settings, prompt, `run.json`, any private file) |
| Editing the `--settings` file mid-run has no effect | #42 |
| Outbox or artifacts symlink or hard link rejected | `test_e2e.py::test_symlink_and_hard_link_artifacts_...` |
| `contains_secret` on 10 MiB within the time budget | `test_e2e.py::test_a_10_mib_artifact_...` |
| A headless `implement` run files its report | `test_e2e.py::test_a_headless_implement_run_...` |
| Deny-root reads (E1): markers hidden; Bash, interpreters work | `test_sandbox.py::test_deny_root_...`; git: `test_a_denied_git_dir_...`; the network bridge: #42 |
| Planted project and nested skills (E2) | `test_isolation.py::test_planted_project_...` |
| A fork with `--cwd` records the transcript path from its SessionStart payload | `test_e2e.py::test_a_respond_like_fork_...`, `test_headless.py::test_resume_keeps_the_id_...` |
| Manual macOS checklist | [macos-checklist.md](macos-checklist.md) (#43) |

### Not covered, with the owner

| Row | Owner | Why |
|---|---|---|
| Credential isolation (`GH_CONFIG_DIR`, shell-profile `SSH_AUTH_SOCK`, `CLAUDE_CONFIG_DIR` loses the login) | #42 | Depends on the user's shell profile and git-host CLIs. The harness environment allowlist is unit-tested. |
| Prompt delivery: 128 KiB argv limit; `-p` waits 3 s for an open stdin | #42 | An OS limit and a timing race. |
| Plain `--resume` of a live session interleaves writes | #42 | Needs two concurrent real sessions; the wrapper refuses it, which the unit tests cover. |
| B2: trust inherited from a trusted parent directory; "No, exit" exits 1 | #42 | Not in the current dialog test. |
| B3: SessionStart context delivered after `/clear` and `/compact` | #42 | The test checks ids and sources only. |
| B5: planted `.git` with `core.fsmonitor` | #42 | Needs a planted repository and a count of Claude's own git calls. |
| C (built-in write protections beyond the placeholder check), C2 (single-`*` glob ignored), C5 (uv tool install), C7 (`GIT_CONFIG_COUNT`), C8 (outbox/artifacts directory swap) | #42 | Sandbox internals on a Linux sandbox host. |
| Respond isolation: network, tmux socket and SSH agent blocked | #42 | Needs an outside listener. |
| Sandbox: TMPDIR socket length; `excludedCommands` matching; `.bash_profile` placeholder | #42 | Unit-tested in `selfcheck`; the live half needs a sandbox host. |
| Search tools under the review profile (Glob, Grep, LS); symlink planted by sandboxed Bash and the Read tool (F1); `/proc` isolation (F2); MCP elicitation (`elicitation_dialog`) | #42 | Open items. |
| Managed settings (#56 (b), #59 item 13) | #42 | Needs a root-owned managed settings file (procedure below). |
| macOS Seatbelt checks, process start time, socket paths | #43 | Manual, macOS only: [macos-checklist.md](macos-checklist.md). |

### Comments on issue #41

| Item | Test or owner |
|---|---|
| #55 Bedrock/Vertex truthy values | `test_provider_env.py` (zero cost; message patterns are guesses, see the docstring) |
| #56 1 `--` before the prompt | `test_headless.py::test_double_dash_...` and the control (no prompt: `-p` fails for want of input) |
| #56 2 Notification matchers | `test_interactive.py::test_permission_prompt_notifications_...` (`permission_prompt` and `idle_prompt`; `elicitation_dialog`: #42) |
| #56 3 Omitted `matcher` | `test_hooks.py::test_a_group_without_a_matcher_...` (PostToolUse and SessionStart; PermissionRequest is only ever seen unmatched in the interactive test) |
| #56 4 Hook `timeout` unit | `test_hooks.py::test_hook_timeout_is_in_seconds` |
| #56 5 `defaultMode` precedence | `test_settings.py::test_settings_default_mode_...` |
| #56 6 Rule paths with spaces or `:` | `test_settings.py::test_file_rules_match_paths_...` (with a no-rule control) |
| #56 7 Invalid values discard the file | `test_settings.py::test_an_invalid_value_...` |
| #56 (a) `disableAllHooks: false` override | `test_settings.py::test_disable_all_hooks_...` (records the answer) |
| #56 (b), #59 13 managed `allowManagedPermissionRulesOnly`, `allowManagedHooksOnly`, `allowManagedReadPathsOnly` | #42, human: needs a root-owned managed settings file (`/etc/claude-code/managed-settings.json`). Procedure: write the file, run a `dontAsk` isolated run, expect no report, no Write, no Bash; with the hooks flag expect the `session_start_timeout` warning. |
| #57 SIGHUP, hook timeout kill sequence, `background_tasks` omitted, PostToolUseFailure, Notification texts, parser details, hidden flags | Observed: `test_interactive.py::test_signals_...`, `test_hooks.py` (SIGTERM asserted, SIGKILL escalation and `background_tasks` observed, Stop payload, failure event), `test_headless.py::test_append_system_prompt_file_...` (hidden flags). commander.js parsing details (`-` values, `--` ending a list): #42. |
| #58 1 subagent PostToolUse vs a waiting prompt | `test_hooks.py::test_a_subagents_tool_use_...` and `test_interactive.py` record `agent_id`, `tool_use_id` (observations); the table change is the `TODO(#42)` in `src/claude_session/hookevent.py`. |
| #58 2 AskUserQuestion then `permission_prompt` | `test_interactive.py::test_ask_user_question_...` (records) |
| #58 3 PostToolUseFailure | `test_hooks.py::test_post_tool_use_payload_and_the_event_after_a_failed_tool` (records which fires) |
| #59 1, 2 key names and path syntax | `test_settings.py::test_block_reads_...`; path syntax in the sandbox tests |
| #59 3 `failIfUnavailable` | `test_sandbox.py::test_a_missing_sandbox_fails_...` |
| #59 4 network bridge with `allowedDomains: []` under deny-root | #42 (sandbox host) |
| #59 5 `disableBypassPermissionsMode` | `test_settings.py::test_disable_bypass_permissions_mode_...`; `strictAllowlist`, `disableAutoMode` and leaving `dontAsk` with Shift+Tab: #42 |
| #59 6 explicit `false` sandbox keys override user settings | #42 (sandbox host) |
| #59 7 system read dirs on a usr-merged distro | `test_sandbox.py::test_deny_root_...` (literal paths and realpaths) |
| #59 8 full golden settings live: startup, no placeholder files | `test_e2e.py::test_a_review_run_in_the_sandbox_...` (cwd, session directory, denied host dir) |
| #59 9 `~/.gitconfig` re-opened under deny-root | #42 (sandbox host) |
| #59 10 skills in `--add-dir` under `--setting-sources ""` | `test_isolation.py::test_skills_in_an_add_dir_directory_...` (records the answer) |
| #59 11 `sandbox: off` vs user `enabled: true` | #42 (sandbox host) |
| #59 12 Grep and Glob read denies | #42 |
| #60 `preferred` fallback: Claude Code runs unsandboxed when the sandbox is missing | `test_sandbox.py::test_without_a_sandbox_enabled_without_fail_if_unavailable_...` (Linux without a sandbox) |
| #60 bwrap probe fidelity, socat, socket names, `TMPDIR` vs `CLAUDE_CODE_TMPDIR`, `/tmp` ownership | #42 (a sandbox host and a namespace); unit tests cover the probe |
| #63 10 MiB artifact | `test_e2e.py::test_a_10_mib_artifact_...` |
| #64 trailing newline; exit form on a signal | `test_headless.py::test_headless_stream_json_ends_...`, `test_headless_signals.py` |
| #67 1 to 4 | `test_interactive.py::test_double_ctrl_c_exit_code`, `test_signals_...`; `test_headless_signals.py` |
| #37 1 to 7 and review additions | `test_transcript.py` (item 7 also in `test_headless.py`); counts of `subagents/` and `tool-results/` entries are recorded, not compared with the bounds |
| #76 SessionStart `source` values | `test_headless.py`, `test_interactive.py::test_clear_changes_the_id_...` |

### Tests of the suite itself

`tests/test_live_harness.py` covers the helpers (environment allowlist and forbidden names, credentials and the login cache, the API-key approval, reprs, the ledger and the spend gate on every path, charging the estimate on a timeout or unparsable output, spawned runs and their process groups, stream and transcript parsing, the tmux driver, token-checked signals and hook PID records, `headless` against the fake claude) and the gating: a default run deselects every live test, `-m live` without the opt-in skips them all, `make live` sets the opt-in and `verify` does not depend on it. `tests/test_leftover_guard.py` covers the live watchdog limit.
