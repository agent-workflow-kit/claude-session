# claude-session

Runs and supervises Claude Code sessions (terminal-agnostic).

## Status

Designed (interface 0.1.0-design); not yet implemented. [docs/interface.md](docs/interface.md) is the normative interface (launch order, state transitions, outcome mapping, isolation); [docs/design.md](docs/design.md) covers the design and spike results.

## Layer

L1 (primitive).

## What it does

- `exec` is the session wrapper around `claude`: it starts `claude` as a child, injects Claude Code hooks to track state, delivers the prompt as `$CSESSION_DIR/prompt.md`, records the outcome and exits with an outcome code (design guide §4.2: 0 done, 11–18 other outcomes).
- `start` allocates a session with a pending run and prints the `exec` command line for a caller to place; the caller decides where it runs (the adopting process needs the variables listed in `required_env_names`). `start --resume <id>` does the same for a resume or fork. A pending run not adopted within `lost_grace` ends `not-started`.
- Sessions report their status with `report done|blocked|failed|needs-input`, authenticated with a per-run secret (rotated on every run).
- The `respond` kind edits files in its git cwd but never commits: its git directory is read-only, and the caller commits the reported `changed_files` after the user confirms (a caller may fork a session into another directory with `--fork --cwd`, e.g. a disposable checkout) (see [Artifact contract](docs/interface.md#artifact-contract-for-callers)).
- Supports `resume` (including `--fork`; a session that never captured a Claude session id resumes as a fresh conversation), `stop`, kind profiles (`--kind`), labels, taint tracking for third-party-reading kinds, `forget` (one session or in bulk), `reconcile`, `export` of audit records and transcript access.
- **Auto-exit**: by default (`auto_exit: true` in every built-in kind), `exec` ends `claude` `auto_exit_delay` after the session files a `done` report and stops (with no background tasks running), in interactive mode too. Use `--no-auto-exit` (or `auto_exit: false` in a kind) to keep the conversation open after the report.
- Standalone, it records state and exits with an outcome code; post-exit policies belong to callers.

## Commands

Full reference: [docs/interface.md](docs/interface.md).

```text
  claude-session exec [launch flags]          run a session in the foreground; exit with its outcome code
  claude-session start [launch flags] --json  allocate a session; print the argv to place
  claude-session start --resume <id> [--fork] --json   prepare a resume (or fork) to place
  claude-session resume <id> [--fork]         run the session again (or a fork of it)
  claude-session stop <id> [--grace <d>]      end a live run (SIGTERM, then SIGKILL) or cancel a pending one
  claude-session wait <id> [--run <n>]        block until the session (or run) ends
  claude-session report done|blocked|failed|needs-input   (run by the agent)
  claude-session status <id> | list | kinds
  claude-session label add|remove <id> k=v...
  claude-session transcript <id>
  claude-session forget <id> | forget --older-than <d>   hide one session, or in bulk (never a live or pending one)
  claude-session reconcile | cron install|uninstall
  claude-session export [--since <t>] [--action <a>]   print audit records
  claude-session hook-event --state-dir <dir> <event>   (internal)
  claude-session verify-secret <id>           (internal)
```

Exit codes: 0 done; 1 internal error; 2 usage (including interactive `exec`/`resume` without a TTY and an invalid config); 10 unknown session; 11 `pre-launch` veto or hook timeout; 12 never started (launch failed, canceled, never adopted, interrupted, or config invalid at adoption), or an error with code `schema_newer` (database newer than this claude-session) or `failed`; 13 blocked or needs input (or a `pre-launch` escalation); 14 exited unreported; 15 killed (stopped, a forwarded signal such as `forwarded_term`, or an internal error); 16 timeout; 17 lost; 18 failed; 20 refused (`live`, `tainted`, `already_adopted`, `expired`); 21 ambiguous id; 22 `wait` timed out. A run that never started ends in state `not-started` with an `end_reason` naming the cause (e.g. `unsafe_path`, `no_git_cwd`, `config_invalid`). With `--json`, waiting commands print an outcome object (also written to `--outcome-file` by whichever process finalizes the run; a write failure is only a warning); other failures print an error envelope. The full mapping is in [Outcome mapping](docs/interface.md#outcome-mapping).

Global flags: `--json`, `--config`, `--verbose`, `--quiet`, `--version`, and `--yes` (accepted everywhere, a no-op: claude-session never asks for confirmation).

Agents report with a bare `claude-session report …` command (or its absolute path); forms with `cd … &&` or `$(…)` are denied by the generated permissions. See [`report`](docs/interface.md#report).

Environment: sessions get `CSESSION_ID`, `CSESSION_DIR` and `CSESSION_KIND`. Config overrides use the `CSESSION_` prefix (top-level scalar keys only). Hooks (`pre-launch`, `post-state-change`) run as `sh -c` with the state directory as cwd and get `CSESSION_HOOK`, `CSESSION_ID` and `CSESSION_OBJECT_TYPE`. `post-state-change` fires on every turn (`working` ↔ `idle`) and is delivered at least once (deduplicate on `event_id`), so notification hooks should filter with the `states` selector (e.g. `states: [needs-input, exited, exited-unreported, killed, timeout, lost, not-started]`); hook entries also take `kinds`, `exclude_kinds` and `labels` selectors. A hook that launches sessions must exclude their kinds (`exclude_kinds`) to avoid recursion. claude-session needs no credentials of its own.

Upgrade claude-session while no sessions are live.

## Dependencies

- `claude` (Claude Code CLI): binding A (hard); version 2.1.312 or later (config `min_claude_version`).
- memoctl, optional, for session-start context: binding B (config `notes_context_cmd`, enabled per kind with `notes_context: true`). Install: `uv tool install git+https://github.com/agent-workflow-kit/memoctl`.
- For sandboxed kinds (`review`, `gate-check`, `respond` by default) on Linux and WSL2: `bubblewrap` and `socat` (`sudo apt install bubblewrap socat`). macOS needs nothing extra.

## Install

```sh
uv tool install --link-mode=copy git+https://github.com/agent-workflow-kit/claude-session
```

`--link-mode=copy` (or `UV_LINK_MODE=copy`) is required for sandboxed and third-party-reading
kinds: by default uv hard-links the installed files to its cache, and a sandboxed session that
can write the cache could then change the code of claude-session's hooks, which run outside the
sandbox. Isolated runs refuse to launch (`unsafe_path`) when claude-session's files have more
than one link; reinstall with `--link-mode=copy` (add `--reinstall`) to fix that.

## Documentation

- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit tool design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
