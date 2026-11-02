# Live tests

The `live` suite (`tests/live/`) runs the real `claude` binary and re-checks every Claude Code behavior that [design.md](design.md) and [interface.md](interface.md) rely on. Run it after every Claude Code upgrade and before a release. It is never part of `make verify` or CI.

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
| `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` | Credentials passed to the private environment (first one found). |
| `CSESSION_LIVE_COPY_LOGIN=1` | Copy `~/.claude/.credentials.json` into the private home (read only on the real side). An OAuth refresh in the copy can rotate the refresh token and sign your real installation out, so prefer a token. |
| `CSESSION_LIVE_MODEL` | Model for runs (default `haiku`). |
| `CSESSION_LIVE_RUN_USD` | `--max-budget-usd` per headless run (default 0.25). |
| `CSESSION_LIVE_MAX_USD` | Session cap: the rest of the suite skips once the reported cost reaches it (default 3). |
| `CSESSION_LIVE_CLAUDE` | A `claude` other than the one on `PATH`. |
| `CSESSION_LIVE_OBSERVATIONS` | Append the recorded observations (below) to this JSON-lines file. |

Tests that need a login skip without one. The zero-cost tests (`--version`, `--help`, the cloud-provider switch values with an invalid key) need only the binary.

## Isolation and safety

- Each test gets a private root directly under `/tmp` (short, mode 0700: Claude's unix socket names must fit) with its own `HOME`, `XDG_*`, `TMPDIR` and working directories. The child environment is built from an allowlist (locale, proxy and certificate settings, one credential), never from the parent. Tests cannot set `HOME`, `XDG_*` or `CLAUDE_CONFIG_DIR`. The real `~/.claude` is not written.
- `tests/conftest.py` now gives live tests a private `HOME` too; the real one is read at import only to find the login file `CSESSION_LIVE_COPY_LOGIN` asks for.
- `claude` runs through the gateway: a timeout ends exactly the process group it started. Signal tests send to the exact child they spawned (interactive ones to the tmux pane PID, checked against `/proc/<pid>/cmdline`). A leaked hook child is killed only while its recorded start-time token still matches.
- Interactive tests use a private tmux server (`-f /dev/null`, a socket in the private root, the private environment) and stop it by that socket.
- Isolated-run tests need `link-mode = "copy"` in the dev venv, as `pyproject.toml` sets.

## Observations

Some checks are open questions (an exit code, a payload shape, a message text). Those tests assert what the design tolerates and record the value seen with `live.observe(key, value)`: printed in the terminal summary and, with `CSESSION_LIVE_OBSERVATIONS`, written to a file. After a run, compare the observations with the fake claude's assumptions (`tests/fakebin/fake_claude.py`) and with the table rows named in the tests, and update them.

## Extending

- New API-using tests take the `live` fixture, zero-cost ones `cheap`; both come from `tests/live/conftest.py`. Put helpers in `tests/live/harness.py` and unit-test them in `tests/test_live_harness.py`, with the fake claude or `sh`.
- `LiveEnv.headless(prompt, *flags)` runs a small-model `-p` with `stream-json`, the prompt after `--`. `LiveEnv.claude_run` runs with exactly the given argv. `spawn_headless` and `tmux_driver.Tmux` are for signals and interactive sessions.
- Probe hooks are `tests/fakeclaude.recorder(log, ...)` commands in a `--settings` file; read them with `harness.read_log` and `harness.events_named`.
- Sandbox tests use `needs_sandbox` (`test_sandbox.py`); macOS-only checks belong in the manual checklist of #43.

## Coverage

"Never run" means no human has run the test yet against a real Claude Code; every test below is in that state until the first `make live` run (the harness itself is unit-tested). Zero-cost probes of the version and `--help` flags were run once against 2.1.316.

### Spike and live-verification tables of design.md

| Row | Test |
|---|---|
| `--plugin-dir` additive; manifest name; duplicate: first wins; skill with arguments (A5) | `test_headless.py`: plugin tests |
| `--settings` hooks merge; deny beats allow | `test_settings.py::test_settings_hooks_merge_with_user_and_project_hooks_and_a_deny_beats_an_allow` |
| `--resume` forgets plugin dirs | `test_headless.py::test_resume_does_not_remember_plugin_dirs` |
| Session id on resume; `--session-id`; fork ids and transcripts; resume from another cwd (A6); interactive fork (B1, headless half) | `test_headless.py::test_resume_keeps_the_id_...`, `test_a_preassigned_session_id_...` |
| Stop-hook block (JSON and exit 2) | `test_hooks.py::test_a_stop_hook_can_block_once_...` |
| `-p` result fields; error turn limit | `test_headless.py::test_result_object_...`, `test_the_turn_limit_...` |
| Background subagents: several results (A1) | `test_headless_signals.py::test_background_subagents_...` |
| SessionStart sources and context delivery | `test_headless.py` (resume/fork), `test_interactive.py::test_clear_changes_the_id_...`, `test_hooks.py::test_session_start_plain_stdout_...` |
| Prompt delivery: argv plus stdin | `test_headless.py::test_argv_prompt_and_stdin_are_concatenated` |
| `--append-system-prompt-file`, `--max-turns`; snapshot (A3) | `test_headless.py::test_append_system_prompt_file_...`, `test_system_prompt_snapshot_off_...` |
| Permission prompts, notification timing, denial fires nothing, `idle_prompt` | `test_interactive.py::test_permission_prompt_notifications_...` |
| Auto-exit by signal (SIGTERM 143, SIGHUP, Ctrl-C) | `test_interactive.py::test_signals_...`, `test_double_ctrl_c_exit_code` |
| `--setting-sources`: `""` mode, CLAUDE.md, bypass downgrade | `test_settings.py` |
| `--strict-mcp-config` (incl. plugin MCP, D4) | `test_headless.py::test_strict_mcp_config_...` |
| Hooks and the environment | `test_hooks.py::test_hook_payload_environment_cwd_and_shell` |
| Stop with background tasks | `test_headless_signals.py::test_background_subagents_...` |
| Read tool vs sandbox; `blockReadsOutsideWorkingDirectories` | `test_settings.py::test_block_reads_...` |
| Subagent tool use (A2); parallel hook order (A7) | `test_hooks.py` |
| `PostToolUse` block (A4) | `test_hooks.py::test_a_post_tool_use_block_reason_...` |
| Report command forms (A8) | `test_isolation.py::test_report_command_forms_under_dont_ask` |
| Default mode denies in `-p` (A9) | `test_headless.py::test_in_default_mode_...` |
| Hook timeout (A10) | `test_hooks.py::test_hook_timeout_is_in_seconds`, `test_a_timed_out_hook_...` |
| Trust dialog (B2) | `test_interactive.py::test_the_trust_dialog_...` |
| AskUserQuestion (B4) | `test_interactive.py::test_ask_user_question_...` |
| Idle prompt (B6) | `test_interactive.py::test_permission_prompt_notifications_...` |
| Plugin dir via `--add-dir` (C6) | `test_isolation.py::test_a_plugin_dir_is_readable_...` |
| Git dir read-only, main and linked (D1, D2) | `test_sandbox.py::test_a_denied_git_dir_...` (needs sandbox) |
| `autoAllowBashIfSandboxed` (D3) | `test_sandbox.py::test_auto_allow_bash_...` (needs sandbox) |
| Deny-root reads, TMPDIR (E1) | `test_sandbox.py::test_deny_root_...`, `test_e2e.py::test_a_review_run_...` (need sandbox) |
| Planted skills (E2) | `test_isolation.py::test_planted_project_...` |
| Respond fork into another directory | `test_e2e.py::test_a_respond_like_fork_...` |
| `contains_secret` on 10 MiB | `test_e2e.py::test_a_10_mib_artifact_...` |
| Outbox/artifact symlink and hard link | `test_e2e.py::test_symlink_and_hard_link_artifacts_...` |
| Headless implement run files its report; interactive run auto-exits | `test_e2e.py` |
| Private-dir files unwritable by the agent | `test_e2e.py::test_a_review_run_in_the_sandbox_...` (needs sandbox) |

Not covered, with the reason:

| Row | Why |
|---|---|
| Credential isolation (`GH_CONFIG_DIR`, shell-profile `SSH_AUTH_SOCK`, `CLAUDE_CONFIG_DIR` loses the login) | Depends on the user's shell profile and git-host CLIs: manual. The harness environment allowlist is unit-tested. |
| Prompt delivery: 128 KiB argv limit; `-p` waits 3 s for an open stdin | An OS limit and a timing race. |
| Plain `--resume` of a live session interleaves writes | Needs two concurrent real sessions; the wrapper refuses it, which the unit tests cover. |
| B5 planted `.git` with `core.fsmonitor`; C (built-in write protections, placeholder files), C1 to C3, C5 (uv tool install in the sandbox), C7, C8 | Sandbox internals and a tool install; list for a sandbox host session (#43 covers macOS). `test_e2e.py::test_a_review_run_...` checks that no stray files land in the denied host directory. |
| Respond isolation: network, tmux socket and SSH agent blocked | Needs an outside listener; manual. |
| Sandbox: TMPDIR socket length; `excludedCommands` matching; `.bash_profile` placeholder | Unit-tested in `selfcheck`; the rest is informational. |
| LS under the review profile | Not a tool in the Claude Code the design was verified against (design.md, D3); `test_open_items.py::test_search_tools_...` records the `init` tool list, so a later version that adds it shows up in the observations. Nothing asserts on it. |
| Glob and Grep through the generated review settings end to end | Isolated runs refuse `--tools` in `claude_args` (`unsafe_claude_args`), and without it Glob and Grep are not offered, so the product cannot enable them. `test_open_items.py` builds the same permission keys by hand. |
| macOS Seatbelt checks | Manual checklist: issue #43. |

### Comments on issue #41

| Item | Test or status |
|---|---|
| #55 Bedrock/Vertex truthy values | `test_provider_env.py` (zero cost; message patterns are guesses, see the docstring) |
| #56 1 `--` before the prompt | `test_headless.py::test_double_dash_...` and the control |
| #56 2 Notification matchers | `test_interactive.py::test_permission_prompt_notifications_...` (`permission_prompt` and `idle_prompt`; `elicitation_dialog` is `test_interactive.py::test_an_mcp_elicitation_...`) |
| #56 3 Omitted `matcher` | `test_hooks.py::test_a_group_without_a_matcher_...` (PostToolUse and SessionStart; PermissionRequest is only ever seen unmatched in the interactive test) |
| #56 4 Hook `timeout` unit | `test_hooks.py::test_hook_timeout_is_in_seconds` |
| #56 5 `defaultMode` precedence | `test_settings.py::test_settings_default_mode_...` |
| #56 6 Rule paths with spaces or `:` | `test_settings.py::test_file_rules_match_paths_...` |
| #56 7 Invalid values discard the file | `test_settings.py::test_an_invalid_value_...` |
| #56 (a) `disableAllHooks: false` override | `test_settings.py::test_disable_all_hooks_...` (records the answer) |
| #56 (b), #59 13 managed `allowManagedPermissionRulesOnly`, `allowManagedHooksOnly`, `allowManagedReadPathsOnly` | Human: needs a root-owned managed settings file (`/etc/claude-code/managed-settings.json`). Procedure: write the file, run a `dontAsk` isolated run, expect no report, no Write, no Bash; with the hooks flag expect the `session_start_timeout` warning. |
| #57 SIGHUP, hook timeout kill sequence, `background_tasks` omitted, PostToolUseFailure, Notification texts, parser details, hidden flags | Observed: `test_interactive.py::test_signals_...`, `test_hooks.py` (kill sequence, Stop payload, failure event), `test_headless.py::test_append_system_prompt_file_...` (hidden flags). commander.js parsing details (`-` values, `--` ending a list) are not covered. |
| #58 1 subagent PostToolUse vs a waiting prompt | `test_hooks.py::test_a_subagents_tool_use_...` and `test_interactive.py` record `agent_id`, `tool_use_id` (observations); the table change waits for a human reading them. |
| #58 2 AskUserQuestion then `permission_prompt` | `test_interactive.py::test_ask_user_question_...` (records) |
| #58 3 PostToolUseFailure | `test_hooks.py::test_post_tool_use_payload_and_the_event_after_a_failed_tool` (records which fires) |
| #59 1, 2 key names and path syntax | `test_settings.py::test_block_reads_...`; path syntax in the sandbox tests |
| #59 3 `failIfUnavailable` | `test_sandbox.py::test_a_missing_sandbox_fails_...` |
| #59 4, 7, 8, 9 network bridge, system dirs, full golden settings, `~/.gitconfig` | `test_e2e.py::test_a_review_run_...` (generated settings, deny-root); the network bridge and gitconfig re-open are not separately asserted |
| #59 5, 6, 10, 11 `strictAllowlist`, disabled modes, explicit `false` keys, skills in `--add-dir`, `sandbox: off` vs user `enabled: true` | Not covered yet (need a sandbox host); listed for the first human run |
| #59 12 Grep and Glob read denies | `test_open_items.py::test_search_tools_and_the_home_read_denies` (three variants; only `both`, which is what isolated runs generate, asserts, and only for Read and Grep content; the rest is recorded) |
| #60 bwrap probe fidelity, socat, socket names, `TMPDIR` vs `CLAUDE_CODE_TMPDIR`, `preferred` fallback, `/tmp` ownership | Not covered (need a sandbox host and a namespace); unit tests cover the probe |
| #63 10 MiB artifact | `test_e2e.py::test_a_10_mib_artifact_...` |
| #64 trailing newline; exit form on a signal | `test_headless.py::test_headless_stream_json_ends_...`, `test_headless_signals.py` |
| #67 1 to 4 | `test_interactive.py::test_double_ctrl_c_exit_code`, `test_signals_...`; `test_headless_signals.py` |
| #37 1 to 7 and review additions | `test_transcript.py` (item 7 also in `test_headless.py`); counts of `subagents/` and `tool-results/` entries are recorded, not compared with the bounds |
| #76 SessionStart `source` values | `test_headless.py`, `test_interactive.py::test_clear_changes_the_id_...` |

### Open items (issue #42)

None of these tests has been run against a real Claude Code. They are queued for the first human `make live`; until then the findings columns of design.md (F1, F2) and interface.md (residual risks, home-directory Read denies) are unchanged. After the run, read the observations (`CSESSION_LIVE_OBSERVATIONS`) and update those rows, and open a follow-up issue for any required change.

| Item | Test | What it asserts and records |
|---|---|---|
| F1 Read, Grep, Glob on a symlink planted by sandboxed Bash | `test_open_items.py::test_f1_...[Read\|Grep\|Glob]` (needs a sandbox) | Bash plants a file symlink and a directory symlink to a private `outside/` dir; Read and Grep must not return the secret (assert, with a plain file in the cwd as the control that the tool works); what Glob lists behind the directory link is recorded. If this fails, the home Read denies in the isolated settings must stay. |
| F2 `/proc` isolation | `test_open_items.py::test_f2_...` (needs a sandbox, Linux) | A host child (exact PID, killed at teardown) holds a marker in its environment; sandboxed `cat /proc/<pid>/environ` must not show it, and `ls /proc` must list fewer processes than the host. `/proc/<pid>/root` and `/proc/1/cmdline` are recorded only. |
| MCP elicitation | `test_interactive.py::test_an_mcp_elicitation_...` | A stdlib stdio MCP server (`tests/live/mcp_elicit_server.py`) sends `elicitation/create` from its tool; the `elicitation_dialog` Notification hook must fire. Records the payload, any `PermissionRequest`, the screen, and what Esc does to the pending request. |
| Search tools and the home Read denies (#59 12) | `test_open_items.py::test_search_tools_...[block-reads\|read-deny\|both]` | A planted `~/.ssh/id_test` in the private home; Read, Grep and Glob under `blockReadsOutsideWorkingDirectories` only, a `Read(//<home>/.ssh/**)` deny only, and both. Records which tools return the key or its name; asserts only for `both` and only for Read and Grep content. |
| `--strict-mcp-config` drops plugin MCP servers | already covered: `test_headless.py::test_strict_mcp_config_...` | The issue's scope lists it; no new test. |

Planted files and symlinks live only under the test's private root (`live.root`); the private `HOME` is the only home used.

### Tests of the suite itself

`tests/test_live_harness.py` covers the helpers (environment allowlist, credentials, ledger, stream parsing, tool-call pairing, the elicitation MCP server driven by a fake client, tmux driver, recorded-PID kill, `headless` against the fake claude) and the gating: a default run deselects every live test, `-m live` without the opt-in skips them all, `make live` sets the opt-in and `verify` does not depend on it.
