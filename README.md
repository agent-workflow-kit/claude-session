# claude-session

Runs and supervises Claude Code sessions (terminal-agnostic).

## Status

Pre-design: interface not yet specified; see [docs/design.md](docs/design.md).

## Layer

L1 (primitive).

## What it does

- `exec` is the session wrapper around `claude`: it starts `claude` as a child, injects Claude Code hooks to track state, delivers the prompt as `$CSESSION_DIR/prompt.md`, records the outcome and exits with an outcome code (design guide §4.2: 0 done, 12-18 other outcomes).
- `start` allocates a session and prints the `exec` command line for a caller to place; the caller decides where it runs.
- Sessions report their status with `report done|blocked|failed|needs-input`, authenticated with a per-session secret.
- Supports `resume` (including `--fork`), kind profiles (`--kind`), labels, taint tracking for third-party-reading kinds, `forget`, `reconcile` and transcript access.
- Standalone, it records state and exits with an outcome code; post-exit policies belong to callers.

## Indicative commands

Indicative, finalized in [docs/interface.md](docs/interface.md).

```text
  claude-session exec
  claude-session start
  claude-session resume
  claude-session report
  claude-session status
  claude-session list
  claude-session kinds
  claude-session label add/remove
  claude-session forget
  claude-session reconcile
  claude-session transcript
  claude-session hook-event <event>  (internal)
  claude-session verify-secret <csession_id>  (internal)
```

## Dependencies

- `claude`: binding: TBD (A/B/C), decided in this tool's design session
- memoctl, optional, for session-start context (binding B per architecture §5.1: config `notes_context_cmd`, enabled per kind with `notes_context: true`)

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/claude-session
```

## Documentation

- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit tool design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
