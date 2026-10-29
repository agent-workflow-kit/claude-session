# claude-session: design

Status: Seeded from awkit architecture v1.4; not yet designed.

## Role and layer

Runs and supervises Claude Code sessions (terminal-agnostic). Layer 1 (primitive). Must work fully standalone (architecture §2, C2).

## Responsibilities

From architecture §5.1:

Runs and supervises Claude Code sessions. Terminal-agnostic: it builds and runs the command; the caller decides where. Explicitly Claude-specific: it is the one implementation of the agent session role (section 3.5); higher tools treat it through that role, so another vendor's session tool can plug in later through an adapter (binding C). Its env prefix is `CSESSION_` (design guide section 5).

- Commands: `exec` (the session wrapper around `claude`), `start` (allocates a session and prints the `exec` command line for a caller to place), `resume`, `report`, `status`, `list`, `kinds`, `label add/remove`, `forget`, `reconcile`, `transcript`; internal: `hook-event <event>` (called by the hooks it injects into sessions), `verify-secret <csession_id>` (checks a session secret given on stdin, section 8.3).
- Uses: `claude`; optionally memoctl for session-start context (binding B, `notes_context_cmd`; enabled per kind with `notes_context: true`).
- Notes: `--kind <name>` selects a kind profile from this tool's config. Standalone, it records state and exits with an outcome code; post-exit policies (auto-resume, promotion, parking) belong to agent-spawn. Auto-exit after `report done` is performed by `exec` itself. See section 8 for the full session model. Supports labels on sessions for standalone use; for agents, agent-spawn is the authoritative label store and mirrors agent labels onto the session. Records taint (section 8.5).
- Hooks: `post-state-change` (fired by `exec`, section 8.2).

## Uses

- `claude`: binding: TBD (A/B/C), decided in this tool's design session
- memoctl, optional, for session-start context (binding B per architecture §5.1: config `notes_context_cmd`, enabled per kind with `notes_context: true`)

The memoctl edge is already bound (type B) by the architecture; the `claude` edge binding is decided in this tool's design session.

## Architecture sections to read

Section numbers refer to `../awkit-docs/architecture.md` (v1.4).

- §8 (all of it): the full Claude session model: §8.1 identifiers and kinds, §8.2 the session wrapper (`exec`), §8.3 reporting and outcomes, §8.4 states, §8.5 resume, fork, taint and forget, §8.6 the spike. This is this tool's primary specification.
- §3.4: never destroy work: signal only exact PIDs verified by PID plus start time.
- §6: conventions summary (exit codes, including session outcomes 14-19).
- §12.2: escalation principle (`report needs-input`).
- §13.1: permissions and environment: permission profiles per kind, setting sources, review and respond profiles, isolation of third-party-reading kinds, env allowlists, `secret_env`.
- §13.2: prompt injection: fencing, transport (prompt delivery, `--summary-file`/`--reason-file`), notes context (rule 8), third-party code (rule 9).
- §13.3: credentials.
- §15: notifications: session-state hook payloads carry the run kind and `reads_third_party`.
- §16: shared notes: SessionStart notes context.
- §20: this tool's constraints and open topics.

## First task: spike (architecture §8.6)

Architecture §8 is this tool's specification and must be read in full (§8.1 to §8.6). Before
any interface design, run the spike from §8.6 and record the results here. Empirically confirm:

- `--plugin-dir` is additive with user-level and project-level standalone skills;
- hooks from `--settings` merge with existing user hooks;
- behavior of `--resume` with respect to plugin dirs and settings;
- whether `--resume` keeps or changes the Claude session id, and whether `--session-id <uuid>` can pre-assign it;
- Stop-hook blocking works as described (headless);
- headless `-p` JSON output includes the session id, `result` and `total_cost_usd`, and `claude_session_id` can be captured in both modes from the SessionStart payload, including its start source (`startup`, `resume`, `clear`);
- prompt delivery: the short bootstrap prompt in argv plus `$CSESSION_DIR/prompt.md` works in both modes, and headless runs accept the prompt on stdin;
- `PostToolUse` fires after an answered permission prompt, so it can clear `needs-input(permission)`;
- clean shutdown by signaling the exact `claude` child PID after Stop (auto-exit);
- credential isolation for third-party-reading kinds (section 13.1): pointing `GH_CONFIG_DIR`/`GLAB_CONFIG_DIR` at an empty temp dir hides git-host logins without affecting Claude's own auth;
- `--setting-sources` can exclude user and project settings (and CLAUDE.md) for third-party-reading kinds, so user allow rules cannot widen their profiles; if exclusion is impossible, conservative profiles are expressed as deny rules;
- `--strict-mcp-config` with no servers leaves the session without MCP servers;
- `--fork-session` with `--resume` creates a new Claude session id, leaves the original conversation unchanged and resumable, and the fork is itself resumable by its new id;
- for the respond profile (section 13.1): `git push` and other network access are unreachable, the user's tmux server socket is unreachable, and Claude Code's sandbox (with network denied) works where available.

Spike results: not yet run.

## Constraints (decided)

From architecture §20: the spike (section 8.6) runs first. `hook-event` only records state under the session lock (exceptions in section 8.2); lifecycle hooks are fired by `exec`. The `lost` grace period for orphaned `starting` records and the retention period after `forget` are configurable. The session secret file is mode 0600 in `$CSESSION_DIR`; the stored copy is compared in constant time. The size cap for `--summary-file`/`--reason-file` is configurable. Taint is recorded per session and never cleared.

## Open topics

From architecture §20:

- state store
- transcript access
- artifact collection
- Stop-hook backstop details
- built-in kind profile contents
- PATH/env per kind
- secret file name and rotation on resume

## Decisions

| Decision | Rationale | Date |
|---|---|---|

## Questions for the design session

1. What do the spike results (architecture §8.6) show, and which parts of §8 must change as a result? (Answer this first.)
2. How is the state store laid out (tables, per-session lock, migrations)?
3. How does `transcript` locate and present Claude Code transcripts?
4. How are artifacts collected from `$CSESSION_DIR/artifacts/` and exposed to callers?
5. What are the details of the headless Stop-hook backstop (message, once-per-run tracking)?
6. What exactly do the built-in kind profiles contain (permission rules, env allowlists, cwd, bounds, auto-exit, notes_context)?
7. How are PATH and the environment configured per kind (e.g. version-manager shims)?
8. What is the session secret file called, and is the secret rotated on resume?
