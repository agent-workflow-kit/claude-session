# claude-session

Runs and supervises Claude Code sessions (terminal-agnostic).

claude-session starts `claude` as a child, injects Claude Code hooks to track what it is doing, and
records how the run ended as an outcome code. It is a standalone primitive: it needs no service, no
credentials of its own and no other awkit tool. Callers (scripts, schedulers, agent managers) place
and watch sessions through the commands below.

## Status

Pre-1.0. Every command documented in [docs/interface.md](docs/interface.md) is available.
[docs/interface.md](docs/interface.md) is the normative
interface (launch order, state transitions, outcome mapping, isolation);
[docs/design.md](docs/design.md) explains the design. Where this README differs from them, they win.

## Layer

L1 (primitive).

## Install

```sh
uv tool install --link-mode=copy git+https://github.com/agent-workflow-kit/claude-session
claude-session --version
```

**Use `--link-mode=copy` (or `UV_LINK_MODE=copy`).** By default uv hard-links installed files to its
cache, and a sandboxed session that can write the cache could then change the code of
claude-session's hooks, which run outside the sandbox. Isolated runs (sandboxed or
third-party-reading kinds) refuse to launch (`unsafe_path`) when claude-session's files have more
than one link. If you installed without the flag, reinstall:

```sh
uv tool install --reinstall --link-mode=copy git+https://github.com/agent-workflow-kit/claude-session
```

Prerequisites:

- `claude` (Claude Code CLI), version 2.1.312 or later (config `min_claude_version`), logged in or
  with its usual authentication variables set.
- For sandboxed kinds (`review`, `gate-check` and `respond` by default; `rank` prefers it) on Linux
  and WSL2: `bubblewrap` and `socat` (`sudo apt install bubblewrap socat`). macOS needs nothing
  extra (see the [macOS manual checklist](docs/macos-checklist.md)). WSL1 has no sandbox. Keep the state directory on the Linux file system under WSL.
- memoctl, optional, for session-start context (config `notes_context_cmd`, enabled per kind with
  `notes_context: true`):
  `uv tool install git+https://github.com/agent-workflow-kit/memoctl`.

Upgrade claude-session while no sessions are live.

## Configuration

Everything works without a config file. To change defaults, create
`$XDG_CONFIG_HOME/claude-session/config.yaml` (default `~/.config/claude-session/config.yaml`;
plus optional `config.d/*.yaml` drop-ins next to it);
[config.example.yaml](config.example.yaml) lists every key with its default. A few top-level
scalars can also be set from the environment with the `CSESSION_` prefix, for example
`CSESSION_CLAUDE_BIN`. `--config <path>` reads only that file. State (the database, session
directories and logs) lives under `$XDG_STATE_HOME/claude-session` (default
`~/.local/state/claude-session`). Check what a launch would use:

```sh
claude-session kinds
```

`kinds` prints the resolved `claude` path and version, every kind profile and whether its sandbox
is available on this machine.

## Quick start

A headless session needs a prompt (`--prompt-file`) or a skill (`--skill`). From a project
directory:

```sh
echo "Summarize what this repository does in three sentences." > prompt.md
claude-session exec --mode headless --prompt-file prompt.md --label demo=quickstart
```

`exec` runs in the foreground, then prints one line on stderr and exits with the run's outcome code
(0 means the session reported `done`):

```text
claude-session: cs-o72rzt5u run 1: done (normal), exit 0
```

The id (`cs-` plus 8 characters) names the session in every other command; the examples below use
`o72rzt5u`, so substitute the id your run printed. Look at it:

```sh
claude-session list --label demo=quickstart
claude-session status o72rzt5u
```

```text
ID           STATE   KIND       RUN  MODE      UPDATED                      LABELS
cs-o72rzt5u  exited  implement  1    headless  2026-11-02T15:01:51.568268Z  demo=quickstart,kind=implement
```

Ids are case-insensitive, the `cs-` prefix is optional and a unique prefix of at least 4
characters is accepted. `status` shows the run's state, outcome, cost, report summary and the
working directory; add `--json` to any command that prints data for machine-readable output
validated by the schemas in [schemas/](schemas/).

Add `--json` to `exec` to get the outcome object on stdout instead of the stderr line. Runs without
a kind use the `implement` kind (trusted, unsandboxed, working in your current directory); pick
another with `--kind`.

## Concepts

- **Session**: one Claude Code conversation that claude-session started, identified by a
  `csession_id` such as `cs-k3v7q2xa`.
- **Run**: one execution of a session. Run 1 starts it; every `resume` adds a run. Reports,
  outcomes, time limits and the permission profile are per run.
- **Kind**: a named profile (`--kind`) that decides the permission profile, sandbox, working
  directory, environment and limits of a run. Built in: `implement`, `draft-ticket`, `audit`,
  `improve` (trusted, run in your directory), `rank`, `review`, `gate-check` and `respond`
  (restricted; see [Safety model](#safety-model)). Add or override kinds in the config.
- **Mode**: `interactive` (the default; needs a terminal, `claude` owns it) or `headless`
  (`claude -p`; output goes to private files, never to your terminal).
- **State**: `starting`, `working`, `idle`, `needs-input`, `reported`, then a final `exited`,
  `exited-unreported`, `killed`, `timeout`, `lost` or `not-started`.
- **Outcome**: how a run ended (`done`, `blocked`, `needs-input`, `failed`, `exited-unreported`,
  `killed`, `timeout`, `lost`, `not-started`), mapped to the exit code of the waiting commands.
- **Report**: the agent's own statement of how it finished (`report done|blocked|failed|needs-input`),
  authenticated with a per-run secret. Without a report, a clean headless exit of a kind with
  `implicit_done` counts as `done`, with the final result as the artifact.

## Usage

### `exec`: run a session in the foreground

Headless, with an input file the agent can read at `$CSESSION_DIR/inputs/<run>/` and a time limit
(input file names may use only letters, digits, `.`, `_` and `-`, at most 128 characters; other
names exit 2):

```sh
claude-session exec --mode headless --prompt-file prompt.md --input-file notes.txt --timeout 30m
claude-session exec --mode headless --skill my-skill --max-turns 20 --json
```

Interactive (the default mode) needs a terminal on stdin and stdout; without one it exits 2. You
talk to `claude` directly and claude-session tracks the state around you:

```sh
claude-session exec --kind implement --prompt-file prompt.md
```

By default the session ends itself (`auto_exit`) shortly after the agent files a `done` report
and has nothing running, in interactive mode too; pass `--no-auto-exit` to keep the conversation
open. To see what a launch would do without starting anything, add `--dry-run`. To collect the
outcome without parsing the terminal, pass `--outcome-file <path>`.

An interactive session in a directory Claude Code has never trusted first shows Claude's trust
dialog; the session stays `starting` until you answer it.

### `start`: allocate a session for another process to place

`start` does everything up to the launch, then prints the command line for a caller to run in a
terminal or process of its choice (as JSON; `--json` is required). The run is pending until that command adopts it; a pending run
not adopted within `lost_grace` (10 minutes) ends `not-started`.

```sh
claude-session start --mode headless --prompt-file prompt.md --label demo=start --json
```

```json
{"csession_id": "cs-fsean3kj", "csession_dir": "<state>/sessions/cs-fsean3kj", "argv": ["<path>/claude-session", "exec", "--session-id", "cs-fsean3kj", "--run", "1"], "run": 1, "forked_from": null, "expires_at": "2026-11-02T15:12:00.313375Z", "required_env_names": [], "env_inherit": true, "warnings": []}
```

Run the printed `argv` (with the environment named in `required_env_names`, if any):

```sh
claude-session exec --session-id cs-fsean3kj --run 1
```

Meanwhile `claude-session list --pending` shows the session in state `starting`. Adoption accepts
only `--run`, `--json`, `--dry-run`, `--outcome-file` and the global `--verbose`, `--quiet` and
`--yes`: every other setting was frozen by `start`.

### `resume` and `--fork`

```sh
claude-session resume o72rzt5u --mode headless --prompt-file followup.md
claude-session resume o72rzt5u --fork --mode headless --prompt-file other-idea.md
```

`resume` adds run 2, 3, ... to the same session (the previous kind and mode are the defaults).
`--fork` creates a new session whose conversation starts from the source's; the source is left as
it is. `start --resume <id> [--fork]` prepares either for placement. A session that never captured
a Claude session id resumes as a fresh conversation, with a warning. What a resume or fork inherits
is in [Resume and fork carry-over](docs/interface.md#resume-and-fork-carry-over).

### `status` and `list`

```sh
claude-session status o72rzt5u            # the current run
claude-session status o72rzt5u --run 1    # a specific run
claude-session status o72rzt5u --json
claude-session list                       # most recently updated first
claude-session list --state needs-input --label demo=quickstart
claude-session list --live --kind review
claude-session list --since 2h --report-status none
claude-session list --pending --json
```

`list` filters: `--label` (repeatable, ANDed), `--state` and `--report-status` (repeatable, ORed;
`--report-status none` is a current run without a report), `--kind`, `--launch-kind`, `--run-kind`,
`--mode`, `--needs-input`, `--pending`, `--live`, `--tainted`, `--repo`, `--since` (a duration such
as `2h` or an ISO 8601 time), `--forked-from` and `--limit`. Both commands check whether
a recorded run is still alive and may finalize it (for example as `lost`); neither runs hooks.

### `wait`

`wait` blocks until a run is final, then exits with the run's outcome code, so it composes in
scripts:

```sh
claude-session wait o72rzt5u --timeout 10m --json
```

```text
{"csession_id": "cs-o72rzt5u", "run": 1, ..., "outcome": "done", "end_reason": "normal", "exit_code": 0, "state": "exited", ..., "timed_out": false, "run_exit_code": 0, "pending": false}
```

`wait` binds to the session's current run when it starts (or run `n` with `--run`). If the run is
already final it returns at once. If `--timeout` expires first it exits 22 with `timed_out: true`;
the session keeps running. `--timeout 0` is a usage error. Waiting on a pending run that nobody
adopts ends `not-started` (`never_adopted`, exit 12) once it expires.

### `report`: from inside a session

Agents run `report` themselves; claude-session adds the instructions and the permission rules
that pre-approve `claude-session report …` commands. You rarely run it by hand. For reference, the
forms the agent is told to use (it writes a summary or reason file to `artifacts/` first):

```sh
claude-session report done --summary-file "$CSESSION_DIR/artifacts/summary.md"
claude-session report done --summary="Fixed the flaky test; see the artifact." \
  --artifact "$CSESSION_DIR/artifacts/report.md"
claude-session report done --summary-file - <<'EOF'
Fixed the flaky test.
EOF
claude-session report blocked --reason-file "$CSESSION_DIR/artifacts/reason.md"
claude-session report needs-input --reason-file "$CSESSION_DIR/artifacts/reason.md"
claude-session report failed --reason-file "$CSESSION_DIR/artifacts/reason.md"
```

`done` needs a non-empty summary; the other three need a reason. A report that contains a secret
(an authentication variable's value or the run secret) is rejected. The latest ingested report of a
run wins. `report` needs `CSESSION_ID` and `CSESSION_DIR`, which `exec` sets for the session. Only a
bare `claude-session report ...` (or the absolute path of the executable) is pre-approved; commands
prefixed with `cd ... &&` or containing `$(...)` are denied. Details:
[`report`](docs/interface.md#report).

### `kinds`

`claude-session kinds [--kind <name>] [--repo <repo-id>] [--json]` prints the effective kind
profiles and whether each one's sandbox can run on this machine. Run it first when a `review`,
`gate-check` or `respond` launch fails.

## Safety model

claude-session treats the agent as untrusted wherever a kind says so. At a user level:

- **Trusted kinds** (`implement`, `draft-ticket`, `audit`, `improve`) run `claude` in your
  directory with your settings and no sandbox. Use them for your own work.
- **Isolated runs** are runs of third-party-reading kinds (`review`, `gate-check`, `respond`, or
  any run with `--reads-third-party`) and runs whose sandbox mode is not `off` (such as `rank`).
  They run in `dontAsk` permission mode, so anything the permission rules do not allow is denied.
  Writes are limited to the session-writable paths: the cwd (for `cwd: caller` kinds such as
  `respond`, your checkout), the session directory (`outbox/` and `artifacts/` for the Edit and
  Write tools; never its protected files), the kind's `write_dirs`, the profile's `allowWrite` and
  a private temporary directory. The sandbox (`bubblewrap` on Linux and WSL2, Seatbelt on macOS)
  also enforces the write limits on Bash commands, denies them reads outside an allow list (the
  cwd, the session directory, system and tool directories, `read_dirs`) and gives them no network
  unless the profile allows domains (the built-in profiles allow none). A kind with sandbox `required` fails
  to launch (`sandbox_unavailable`) rather than run unsandboxed; `preferred` degrades to
  unsandboxed. `status` shows whether the sandbox actually ran (`sandbox_active`). A
  third-party-reading run whose sandbox mode is below `required` is allowed, with a warning, and
  audited `sandbox_lowered`; when it runs without a sandbox (`off`, or `preferred` with none
  available) it relies on the permission rules alone, which a command can evade.
- **Permission profiles** (`--permission-profile <name|file>`, or the kind's setting) are the
  per-kind allow and deny rules: the built-ins are `default`, `readonly`, `review` and `respond`.
  Isolated runs may only add to the narrow set of `sandbox` keys the interface allows; settings
  that would weaken the sandbox are refused (`invalid_permission_profile`). A profile file is read
  once and snapshotted, so later runs use the snapshot.
- **Taint**: a session that has run a third-party-reading kind (one that reads content written by
  other people, such as a pull request) is *tainted*, permanently. `status --json` shows it and
  `list --tainted` filters on it. Resuming or forking a tainted session into a kind that is not
  third-party-reading is refused (exit 20, `tainted`). The human override is `--allow-tainted`
  (see [docs/interface.md](docs/interface.md)).
- **Secrets**: reports, summaries and artifacts are scanned for the values of your authentication
  variables, your `secret_env` variables and the run secret before they are accepted. Kinds with
  `env.inherit: false` (the restricted built-ins) pass only a fixed base set (`PATH`, `HOME`,
  `USER`, `LOGNAME`, `SHELL`, `LANG`, `LC_*`, `TERM`, `COLORTERM`, `TZ`), Claude's
  authentication variables (`claude_auth_env`, so the agent can read your API key), the
  `CSESSION_*` variables and the kind's `env.allow` and `env.set`; see
  [Environment](docs/design.md#environment).
- **Integrity**: hooks run outside the sandbox from claude-session's own files, so isolated runs
  refuse to launch when those files are hard-linked (see [Install](#install)).

The guarantees, their limits and the residual risks are in
[Isolation](docs/interface.md#isolation) and [Residual risks](docs/interface.md#residual-risks).

## Commands

| Command | Purpose | Availability |
|---|---|---|
| `exec` | run a session in the foreground; exit with its outcome code | available |
| `start` | allocate a session (or `--resume <id>` run) and print the argv to place | available |
| `resume <id> [--fork]` | run the session again, or a fork of it | available |
| `wait <id> [--run <n>] [--timeout <d>]` | block until the run is final | available |
| `status <id> [--run <n>]` | show a session and a run | available |
| `list [filters]` | list sessions | available |
| `report done\|blocked\|failed\|needs-input` | the agent's report | available |
| `kinds` | effective kind profiles | available |
| `reconcile` | repair recorded state against reality | available |
| `stop <id> [--grace <d>]` | end a live run (SIGTERM, then SIGKILL) or cancel a pending one | available |
| `label add\|remove <id> k=v...` | edit session labels | available |
| `transcript <id>` | show a session's transcripts | available |
| `forget <id>` / `forget --older-than <d>` | hide sessions | available |
| `cron install\|uninstall` | run `reconcile` from cron | available |
| `export` | print audit records | available |
| `verify-secret` | internal | available |

`pre-launch` hooks (config `hooks.pre-launch`, or `--hook pre-launch=...` per run, with
`--override-escalation`) and `--allow-tainted` are available; see
[docs/interface.md](docs/interface.md) for their rules.

Full reference: [docs/interface.md](docs/interface.md).

## Reference

Exit codes, global flags and the environment sessions see.

Exit codes: 0 done; 1 internal error; 2 usage (including interactive `exec`/`resume` without a TTY and an invalid config); 10 unknown session; 11 `pre-launch` veto or hook timeout; 12 never started (launch failed, canceled, never adopted, interrupted, or config invalid at adoption), or an error with code `schema_newer` (database newer than this claude-session) or `failed`; 13 blocked or needs input (or a `pre-launch` escalation); 14 exited unreported; 15 killed (stopped, a forwarded signal such as `forwarded_term`, or an internal error); 16 timeout; 17 lost; 18 failed; 20 refused (`live`, `tainted`, `already_adopted`, `expired`); 21 ambiguous id; 22 `wait` timed out. A run that never started ends in state `not-started` with an `end_reason` naming the cause (e.g. `unsafe_path`, `no_git_cwd`, `config_invalid`). With `--json`, waiting commands print an outcome object (also written to `--outcome-file` by whichever process finalizes the run; a write failure is only a warning); other failures print an error envelope. The full mapping is in [Outcome mapping](docs/interface.md#outcome-mapping).

Global flags: `--json`, `--config`, `--verbose`, `--quiet`, `--version`, and `--yes` (accepted everywhere, a no-op: claude-session never asks for confirmation). Global flags follow the command name (`claude-session list --json`); only `--version` may also precede it.

Environment: sessions get `CSESSION_ID`, `CSESSION_DIR` and `CSESSION_KIND`. Config overrides use the `CSESSION_` prefix (top-level scalar keys only). `post-state-change` hooks (and `pre-launch` hooks) run as `sh -c` with the state directory as cwd and get `CSESSION_HOOK`, `CSESSION_ID` and `CSESSION_OBJECT_TYPE`. `post-state-change` fires on every turn (`working` ↔ `idle`) and is delivered at least once (deduplicate on `event_id`), so notification hooks should filter with the `states` selector (e.g. `states: [needs-input, exited, exited-unreported, killed, timeout, lost, not-started]`); hook entries also take `kinds`, `exclude_kinds` and `labels` selectors. A hook that launches sessions must exclude their kinds (`exclude_kinds`) to avoid recursion. claude-session needs no credentials of its own.

## Documentation

- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit tool design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
