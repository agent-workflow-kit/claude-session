# claude-session: interface

Status: 0.1.0-design, 2026-10-29. Written before implementation (architecture section 18.1).

**This document is the normative source** for claude-session's behavior: the launch model and order, the flags accepted at `start` and adoption, resume and fork carry-over, the state transitions, the outcome and end-reason mapping, the outcome exit codes, the output by exit code and the isolation rules. Architecture section 8 states the cross-tool contract and links here; [design.md](design.md) explains how it is implemented. Where another document differs, this one wins.

Conventions: design guide sections 4 to 9. JSON Schemas (draft 2020-12) are in [schemas/](../schemas/); `$ref`s between them are relative (e.g. `outcome.json#/$defs/outcome`), so validate with a registry holding every schema. Every command that prints data supports `--json`; with `--json`, errors are printed on stdout as an error envelope ([schemas/cli/error.json](../schemas/cli/error.json)); see [Output by exit code](#output-by-exit-code).

Normative sections: [Launch order](#launch-order), [Flags at start and adoption](#flags-at-start-and-adoption), [Resume and fork carry-over](#resume-and-fork-carry-over), [State transitions](#state-transitions), [Outcome mapping](#outcome-mapping), [Output by exit code](#output-by-exit-code), [Isolation](#isolation).

## Concepts

- **Session**: one Claude Code conversation that claude-session started, identified by a `csession_id`: `cs-` followed by 8 lowercase base32 characters (`a-z`, `2-7`), e.g. `cs-k3v7q2xa`. See [Identifiers](#identifiers) for accepted input forms.
- **Run**: one execution of a session (run 1, then one per resume), numbered from 1. Reports, outcomes, bounds, the resolved cwd and the permission-profile snapshot are per run. A run that ended in the launch phase keeps its number.
- **Phase**: a run is in the `launch` phase until `claude` is spawned (including while it is pending) and in the `session` phase afterwards.
- **Pending run**: a run created by `start` (run 1 of a new session or fork) or `start --resume` (run n) whose wrapper is not yet set. `exec --session-id` adopts it (see [Launch order](#launch-order)).
- **Kind**: the explicit `--kind` of a run, selecting a kind profile ([Config](#config)). The launch kind is the kind of run 1 and never changes; the run kind is the kind of the current run.
- **Third-party-reading run**: a run whose kind has `reads_third_party: true` or that was launched with `--reads-third-party`. Such a run taints the session (see [Taint](#taint)).
- **Isolated run**: a run that is third-party-reading or whose sandbox mode is not `off`; it gets the generated isolation rules ([Isolation](#isolation)). Other runs are trusted.
- **Session directory** (`$CSESSION_DIR`): the agent-visible directory `<state>/sessions/<csession_id>/`, where `<state>` is `$XDG_STATE_HOME/claude-session` (default `~/.local/state/claude-session`):

  ```text
  prompt.md             caller's prompt for run 1, verbatim (absent if none); protected
  prompts/<n>.md        caller's prompt for run n > 1; protected
  instructions.md       standard instructions (--append-system-prompt-file), regenerated
                        every run with identical content; protected
  run.json              {csession_id, run, state_root, summary_max_bytes,
                        artifact_max_bytes, outbox: {dev, ino}, artifacts: {dev, ino},
                        input_files: [...]} (state_root absolute; input_files are this
                        run's basenames), written by exec every run, read by report; protected
  secret                per-run secret, mode 0600, rotated every run; protected
  inputs/<n>/<name>     files given with --input-file for run n (readable; kept for
                        later runs); protected
  outbox/               report requests waiting for ingestion, and report's temporary
                        files (.tmp-<hex>, never ingested) (agent-writable)
  artifacts/            working area for summaries and result files (agent-writable)
  work/                 cwd for kinds with cwd: session-dir (agent-writable when it is the cwd)
  gh-empty/             empty GH_CONFIG_DIR / GLAB_CONFIG_DIR (third-party-reading runs); protected;
                        recreated empty by exec on every isolated run
  empty-gitconfig       empty GIT_CONFIG_GLOBAL (respond kind); protected; recreated empty
                        by exec on every isolated run
  ```

  "Protected" files are denied to the agent for writing (sandbox `denyWrite` and `Edit`/`Write` deny rules). Every file `exec` writes here is written without following symlinks (see [Isolation](#isolation)). The directory's root itself stays writable for new names inside the sandbox (a sandbox deny on it would override the nested allows; spike); no trusted process reads arbitrary names in the root, and the protected files are denied by name. The instructions contain no session-specific literals: "Your session directory is given at session start and in `$CSESSION_DIR`." The state root, every session and private directory are created with mode 0700 and every file with mode 0600 (explicit modes, not the umask).

- **Private directory**: `<state>/private/<csession_id>/`, never added to the session (`--add-dir`) and denied for `Edit`/`Write` in every run; in sandboxed runs the sandbox also denies reading it (isolated runs deny reads from `/` and allow only a generated list; see [Generated settings](#generated-settings)). The guarantee that the agent can neither read nor write it holds for isolated runs; a trusted, unsandboxed run is not confined:

  ```text
  runs/<n>/staged/                        prompt and input copies made by start, moved
                                          into the session directory at adoption
  runs/<n>/settings.json                  generated Claude Code settings (--settings)
  runs/<n>/hookconf.json                  configuration read by hook-event
  runs/<n>/result.json                    headless `--output-format stream-json --verbose`
                                          output, one JSON object per line (stdout
                                          redirected straight to this file, not a pipe)
  runs/<n>/stderr.log                     headless stderr (may contain secrets; never
                                          copied to the caller's stdout or stderr)
  runs/<n>/secret-index                   keyed hashes for contains_secret (secret-
                                          equivalent: never agent-readable; deleted at
                                          run end)
  runs/<n>/permission-profile.json        snapshot of the permission profile used by run n
  runs/<n>/artifact/<ingest_seq>/<name>   frozen artifact; <ingest_seq> is the ingestion
                                          sequence number within the run, so later reports
                                          never overwrite earlier frozen files
  ```

- **Session-writable paths**: see [Session-writable paths](#session-writable-paths).
- **Private temporary directory**: sandboxed runs get `/tmp/csession-<uid>/<csession_id>/` (mode 0700), exported as `TMPDIR` and `CLAUDE_CODE_TMPDIR`; it is in the sandbox's `allowWrite` only, never in `allowRead` (live E1). The resolved `/tmp` must be owned by root or the user and, when group or others can write it, have the sticky bit. `/tmp/csession-<uid>/` is created with `lstat` checks: it must be owned by the user, mode 0700 and not a symlink. Otherwise the launch fails (`tmpdir_unsafe`); the message names the failing entry by its full path and the hint fits that entry. Another local user can create `/tmp/csession-<uid>/` first: it is refused, never repaired or replaced, so every sandboxed launch of that user fails until an administrator removes it (the message names its owner's uid; the remedy until then is a kind's `sandbox` of `off`). The tmpdir is removed at the end of every run (fd-based removal that never follows symlinks) and recreated on resume; a leftover that cannot be removed (for example a tree deeper than the removal accepts, which the agent can build in its TMPDIR) is renamed aside to `/tmp/csession-<uid>/.stale-<csession_id>-<hex>` (never a session id), so it cannot block later runs. Removing it is retried once under its new name; when that fails too, it is reported with the `rm -rf` command that removes it: at launch and resume as a run warning (`leftover private temporary directory moved aside: <path>; remove it: rm -rf <path>`), and at run end in the removal error. `reconcile` removes the tmpdirs of final runs and the `.stale-*` entries.

### Identifiers

- **Session id input**: id input is case-insensitive, and the `cs-` prefix is optional. A unique prefix of at least 4 characters after `cs-` is accepted; fewer exits 2; an ambiguous prefix exits 21 (`matches` lists the candidates); an unknown id exits 10. Forgotten sessions are excluded from prefix matching and report as unknown. `verify-secret` requires the full id (`cs-` plus all 8 characters). Ids of deleted sessions are kept, so a new session never reuses one.
- **`--run <n>`**: a run number that does not exist exits 10.
- **Kind names**: `^[a-z0-9][a-z0-9_-]{0,63}$`, enforced on `--kind`, config `kinds` keys and per-repo kind keys; invalid exits 2 (config: validation error).
- **Permission-profile names**: `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` (so never ending in `.json`, `.yaml` or `.yml`).
- **Labels**: `key=value`, split at the first `=`. Key: `^[a-z0-9][a-z0-9_.-]{0,63}$`. Value: 0 to 256 characters from `A-Z a-z 0-9 . _ : / # @ + = , -` (no spaces or control characters; may contain `=`; may be empty). Enforced at every entry point (`--label`, `label add`, `label remove`, `list --label`, hook `labels` selectors); invalid labels exit 2.
- **Skill names** (`--skill`): `^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$`; invalid exits 2.
- **Artifact names** (also `--input-file` basenames): one path segment matching `^[A-Za-z0-9._-]{1,128}$`, not `.` or `..`.
- **Secret**: 32 random bytes as 64 lowercase hex characters, new for every run.
- **Durations** (flags, config and environment overrides): a non-negative number of seconds (fractions allowed; as text, matching `^[0-9]+(\.[0-9]+)?$`, so `.5`, `5.`, `1e3`, `+5` and `1_000` are invalid), or `<number><unit>` matching `^[0-9]+(\.[0-9]+)?(ms|s|m|h|d)$` (e.g. `500ms`, `90s`, `10m`, `2h`). Digits are ASCII and the whole value must match. A duration is at most `36500d`. Where a bound may be absent, `none` (or `null` in config) means no bound. In config files a YAML number is seconds, but a string duration needs a unit (`90s`; `'90'` is invalid): the bare-number text form is for flags and `CSESSION_*` variables only.
- **Repo id**: lowercase host, an optional `:port` only for a non-default port, then `/` and path segments matching `[A-Za-z0-9._-]+`, never `.` or `..` (`host/<full project path>`). Derived from an `origin` URL by stripping the scheme, `user@`, a default port and a trailing `.git`, and lowercasing the host; the scp form `git@host:path` becomes `host/path`. Accepted origin forms: `https`, `http`, `ssh`, `git+ssh`, `ssh+git` and `git` URLs, and scp-style `[user@]host:path`; anything else (local paths, `file://`, other schemes, an empty path) has no repo id. A repo id given on the command line or in config is compared verbatim with the derived id. Write it without a default port. Invalid repo ids exit 2. A repo id is passed to the notes command as one token, `--repo=<id>`, omitted when the session has no repo. When it is derived: see [`--repo`](#launch-flags).

## Launch order

`start` creates the session and run 1 as a pending run (no wrapper); `start --resume` creates run n as a pending run. `exec --session-id <id>` **adopts** the pending run. `exec` without `--session-id` and `resume` perform every step themselves.

1. **Schema check** (every command, before anything else, except `report`, which never opens `state.db`, and `hook-event`, which never migrates): a database schema newer than this claude-session → error envelope `code: schema_newer`, exit 12; nothing is allocated. An older schema is migrated (see [Upgrades](#upgrades)). A missing `state.db` (and state directory) is created (directory 0700, file 0600) and migrated to the current schema. The check is also skipped when the first argument is not a known command (a usage error follows), and when an argument before any `--` is `-h`, `--help` or `--version` (they only print usage or text; an option value spelled `--help` therefore also skips it).
2. **Parse and usage checks** → exit 2; nothing is allocated. Includes config loading and validation (an invalid config exits 2, `code: usage`, with `detail`); the [flag rules](#flag-rules); the TTY check of plain `exec` and `resume` in interactive mode (stdin and stdout must be TTYs; exit 2 with a hint; `start` never checks the TTY for the run, its caller is a program); human-only flags without a TTY or an explicit `--actor human:*`; prompt and input files larger than `input_max_bytes`; an isolated run's `--outcome-file` under a [session-writable path](#session-writable-paths).
3. **Refusal checks** (`live`, `tainted`, `already_adopted`, `expired`) → exit 20 (10 unknown or forgotten, 21 ambiguous); nothing is allocated. `resume`, `start --resume` and `stop` run the inline [liveness check](#liveness) first.
4. **Allocate and record** the session and run (the taint is committed here: [Taint](#taint)).
5. **Launch validations**, in this order (the first failure gives the end reason): the rule paths (state root and `claude-session` path: `unsafe_path`), the session directories (`session_dir_unsafe`), `$HOME` (`unsafe_path`), the cwd (`unsafe_cwd`), the permission profile read and validated (`invalid_permission_profile`; its write entries and the `write_dirs` that do not use `${GIT_DIR}` are expanded here, so that git is never run from a path they make writable), git and the git directory (`unsafe_path`, `unsafe_cwd`; `respond` without one: `no_git_cwd`), the profile expanded with `${GIT_DIR}`, `read_dirs` and `write_dirs`, the profile file against every session-writable path and the profile's paths and the other [path rules](#path-rules) (`invalid_permission_profile`), plugin directories, the hook interpreter (`unsafe_path`), the temporary directory (`tmpdir_unsafe`), `claude_bin` (`claude_not_found`) and the other tools (`unsafe_path`), `claude --version` against `min_claude_version`, `PATH` (`unsafe_path`), generated read entries (`unsafe_read_dir`), `claude_args` (isolated runs), setting sources, then sandbox prerequisites and socket path length (the sandbox self-check, which creates the private temporary directory as the last action of this step). For example, a `respond` run whose profile file cannot be read and whose cwd has no git directory ends `invalid_permission_profile`; one whose profile names a refused path and whose cwd has no git directory ends `no_git_cwd`. On failure the run ends `not-started` with a launch-phase `end_reason`, exactly one final `post-state-change` fires, and the command exits 12 (`start`: error envelope `code: launch_failed` with `end_reason`, `hint`, `csession_id` and `run`).
6. **`pre-launch` hooks** (in the adopting `exec`, or in `exec`/`resume` without `start`): veto → exit 11, escalate → exit 13; the run ends `not-started` (outcome `vetoed` or `escalated`, end reason `hook_veto`, `hook_timeout` or `hook_escalate`).
7. **Prepare and spawn**: drain the outbox of the previous run (ingest if that run is still being finalized, i.e. `ended_at` is unset and its wrapper is dead; otherwise discard and log), rotate the secret, write the session-directory files (move the staged prompt and inputs into place, write `instructions.md`, `run.json` and `secret`), and spawn `claude` (on Linux, headless children get `PR_SET_PDEATHSIG(SIGTERM)`, set by the gateway's spawn; interactive children get SIGHUP from the terminal). The run enters the session phase; `--timeout` starts counting here (pre-launch hooks do not count). An unsafe or unusable entry of the session or private directory met in step 5 or 7 (a symlink, a file of another owner or type, a failed write) ends the run `not-started` with end reason `session_dir_unsafe` (exit 12), like a launch validation.

`exec` and `resume` install their signal handlers before they allocate a run, and the adopting `exec` before the adoption compare-and-set; see [Signals](#signals).

`start` performs steps 1–5, stages copies of the prompt and input files in `private/<id>/runs/<n>/staged/`, stores everything, and prints `argv`. The **adopting `exec`** performs:

- the adoption compare-and-set: the current run is pending (no wrapper), not final, and `now < expires_at`; it sets the wrapper PID and start time. Refusals: a current run that is final (`canceled`, `never_adopted`, `launch_interrupted`, or any other end) → exit 20, `expired`; a non-final run that already has a wrapper → exit 20, `already_adopted`; a pending run past `expires_at` is first finalized `not-started` (`never_adopted`) and then refused (exit 20, `expired`). A forgotten session exits 10;
- the config: the adopting `exec` loads the absolute config path `start` stored (see [Flags at start and adoption](#flags-at-start-and-adoption)); an invalid config ends the run `not-started` with `config_invalid` (exit 12);
- `--outcome-file` given now overrides the stored path and is checked again (realpath); a rejected path exits 2 and the run stays pending;
- all launch validations of step 5 again (cwd, plugin directories, the stored profile snapshot, git directory, `write_dirs`, `read_dirs`, `PATH`, temporary directory, socket path, `claude_bin`, `bwrap`, `socat` and `git` resolution and the `claude` version, sandbox prerequisites), comparing the stored realpaths; plus the TTY check for interactive mode, which here ends the run `not-started` with `no_tty` (exit 12);
- steps 6 and 7.

Every run keeps one `phase`, `outcome` and `end_reason`; launch-phase ends give exit 11, 12 or 13 with `phase: launch`, session state `not-started` and exactly one `post-state-change` with `final: true`. The taint stays recorded.

**Pending runs.** A pending run not adopted before `expires_at` (`start` + `lost_grace`) ends `not-started` with end reason `never_adopted`; waiters exit 12. `stop` cancels a pending run (`canceled`). `forget` refuses a session whose current run is pending (exit 20, `live`); cancel it with `stop` first.

**Promotion.** To run a resume somewhere other than the foreground (e.g. a parked session promoted into a terminal), a caller uses `start --resume <id>`, places the printed `argv`, and collects the result with `wait <id> --run <run>`.

**Interrupted launch.** If the launching or adopting `exec` receives SIGTERM, SIGHUP or SIGINT during steps 5–7 (before `claude` is spawned), it terminates the hook process group (SIGTERM, `kill_grace`, SIGKILL) and ends the run `not-started` with end reason `launch_interrupted` (exit 12); when the signal came from `stop`, the end reason is `canceled`. If the wrapper dies in this window, the [liveness check](#liveness) finds no wrapper and no recorded `child_pid` and ends the run `not-started` (`launch_interrupted`). An internal error before `claude` is spawned (for example the `sandbox_lowered` audit record cannot be written, or `claude` cannot be started) also ends the run `not-started` with `launch_interrupted`: the command prints the outcome object (its `message` names the failure) and exits 12, and `log.jsonl` gets a `launch_interrupted` record naming the exception type. The spawn is the last step of the launch phase.

**Environment at adoption.** The adopting `exec` builds the session environment (values) from its own process, using the environment rules `start` stored. The adopting process must therefore have the variables named in `required_env_names` (`start --json`; names only: the `claude_auth_env` names in use, the kind's `env.allow` names and the `env.set` names) and a `PATH` containing `claude` (`claude-session` itself is run by the absolute `argv[0]`). If the current config hash differs from the one `start` froze, a warning is logged and added to `warnings`; the parts of the config read at adoption are listed in [Flags at start and adoption](#flags-at-start-and-adoption). `exec` prepends the directory of the `claude-session` executable it runs from to the session `PATH` (after [sanitizing](#path-rules) it); the standard instructions name the absolute path as fallback because user shell profiles may reorder `PATH`.

**Prompts and inputs.** `start` stages copies, so the caller may delete its files once `start` returns. Each prompt and input file is at most `input_max_bytes` (default 10 MiB; larger exits 2). `--prompt-file -` while stdin is a TTY exits 2 for `start` too. Inputs live in `$CSESSION_DIR/inputs/<run>/` and are kept for later runs (a resumed or promoted session can still read earlier inputs). The prompt and inputs of a run that ended `not-started` were never delivered; they are carried to the next run automatically unless that run is given its own `--prompt-file` (replaces the prompt) or `--input-file` (replaces the input set).

**Escalation override.** After a run ends `escalated` (exit 13, `phase: launch`), a caller records its own pending confirmation and, after a human accepts, relaunches with `resume <id> --override-escalation` or `start --resume <id> --override-escalation` (then adopted). The flag is accepted only there, only when the previous run ended `escalated`, and only with a TTY or an explicit `--actor human:<user>` (else exit 2). It overrides exactly the hook entries recorded as escalating on that run (stored as entry hashes); their exit 13 counts as continue, other hooks still escalate, vetoes still veto. Audited `launch_escalation_override` with `overridden_hooks`. A new session that escalated at launch is retried the same way: `resume` (or `start --resume`) of the `not-started` session, which starts a fresh conversation (see [Resume and fork carry-over](#resume-and-fork-carry-over)).

### Bootstrap prompt

When a bootstrap prompt is sent as the first message:

- skill and prompt: `/<skill> Your claude-session directory is <abs $CSESSION_DIR>. Read and follow <prompt path>` (the text after the skill name is the skill's argument text);
- prompt only: `Your claude-session directory is <abs>. Read and follow <prompt path>`;
- skill only: `/<skill> Your claude-session directory is <abs>.`;
- headless resume without a prompt: `Your claude-session directory is <abs>. Continue the task. When you are finished, file your status report.` (an interactive resume without a prompt gets no initial message).

Independently, the injected `SessionStart` hook always adds the trusted context line `claude-session directory: <abs>; run <n>; inputs: inputs/<n>/<a>, inputs/<n>/<b>` (the run's input files, if any; every source: `startup`, `resume`, `fork`, `clear`, `compact`), so the path, the run and the inputs are known even when no first message is sent.

The bootstrap prompt is the last argument of the `claude` argv, preceded by `--`, so no option given before it (for example a `claude_args` entry such as `--debug` or `--allowedTools`) can take it as a value; without a prompt there is no `--`.

Every run passes `--system-prompt-snapshot off` to `claude`, including the first: a resume with `off` uses the newly generated `instructions.md` (live test; a snapshot taken at creation would otherwise win).

## Launch flags

Shared by `exec`, `start` and `resume` (where each is accepted: [Flags at start and adoption](#flags-at-start-and-adoption)). Flags outrank config; what later runs inherit is in [Resume and fork carry-over](#resume-and-fork-carry-over).

| Flag | Default | Meaning |
|---|---|---|
| `--kind <name>` | config `default_kind` (`implement`); resume: the previous run's kind; fork: the source's last run kind | Kind profile ([kind-name grammar](#identifiers)). An unknown name resolves exactly as `--kind review` would (including the repo's per-repo `review` override; flags still apply), is recorded as the kind (`CSESSION_KIND`, `kind=` label) and produces a warning on stderr and in `--json` `warnings`. |
| `--mode interactive\|headless` | `interactive`; resume: the previous run's mode | Interactive needs a TTY on stdin and stdout: plain `exec`/`resume` exit 2 without one; at adoption the run ends `not-started` (`no_tty`, exit 12). |
| `--skill <name>` | none | Skill invoked by the [bootstrap prompt](#bootstrap-prompt) (`/<name>`). |
| `--prompt-file <path>\|-` | none | Caller's prompt, copied (by `start`: staged); at most `input_max_bytes`. `-` with `--mode interactive` while stdin is a TTY exits 2 (also for `start`). |
| `--input-file <path>` | none | Repeatable. Copied (by `start`: staged) to `$CSESSION_DIR/inputs/<run>/<basename>` (readable, write-denied); each at most `input_max_bytes`. Basenames must be unique and match the [artifact-name grammar](#identifiers); `-` is rejected (exit 2). |
| `--plugin-dir <dir>` | none | Repeatable; passed to `claude --plugin-dir`. For isolated runs it is also passed as `--add-dir` (the Read tool reaches plugin files only that way) and made read-only (sandbox `allowRead` and `denyWrite`, `Read` allow and `Edit`/`Write` deny rules). Plugin contents are trusted code: their hooks run outside the sandbox. Subject to the [path rules](#path-rules) (`unsafe_plugin_dir`); a carried directory that no longer exists fails the launch (`plugin_dir_missing`). |
| `--repo <repo-id>` | kinds whose cwd is `session-dir`: none (never derived); other kinds: derived from the `origin` remote of the caller's cwd (or of `--cwd` when given), else none; a `git` that `PATH` resolves under that cwd, `<state>/sessions` or `/tmp/csession-<uid>` (checked hop by hop, as for the [`PATH` rule](#path-rules)) is never run at this step: no repo is derived and the run gets a warning | Selects per-repo overrides. Resolved before the kind's cwd; stored and carried. A derived value has origin `derived`. Callers that know the repo (skill wrappers) pass it explicitly. |
| `--label key=value` | none | Repeatable. On resume, added to the existing labels. |
| `--hook <name>=<cmd>` | none | Repeatable; per-invocation hook (`pre-launch`, `post-state-change`), stored with the run for late delivery. |
| `--hook-timeout <name>=<duration>` | 30s | Repeatable; the timeout of this invocation's `--hook` entries for hook point `<name>`. Configured entries keep their own `timeout`. |
| `--cwd <path>\|session-dir` | kind's `cwd`; resume: the previous run's cwd; fork: the source's cwd | `session-dir` means `$CSESSION_DIR/work/`. A resume always runs in the previous run's cwd (a different `--cwd` exits 2); a fork (`--fork`) accepts any `--cwd` that passes the [path rules](#path-rules). |
| `--timeout <duration>\|none` | kind's `timeout` (headless) or `interactive_timeout` (interactive) | Wall-clock bound of the run in its mode, counted from the spawn of `claude` (`pre-launch` hooks do not count). `0` exits 2 (use `none`). |
| `--max-turns <n>` | kind's `max_turns` | At least 1 and at most 9007199254740991 (else exit 2). Headless only; with interactive mode it is ignored with a warning. |
| `--model <name>` | kind's `model` | Passed to `claude --model`. Must not start with `-` (else exit 2). |
| `--permission-profile <name\|file>` | kind's `permission_profile` | A value containing `/` or ending in `.json`, `.yaml` or `.yml` is a file; anything else is a config profile name. See [Isolation](#isolation). |
| `--sandbox required\|preferred\|off` | kind's `sandbox` | Sandbox mode for this run. A third-party-reading run below `required` (from any origin) is audited `sandbox_lowered` at adoption and reported in `warnings`. |
| `--setting-sources <list>` | kind's `setting_sources` | Comma list of `user`, `project`, `local`; `""` for none. Third-party-reading runs must load none: a non-empty list fails the launch (`unsafe_setting_sources`, see [Generated settings](#generated-settings)). |
| `--reads-third-party` | kind's `reads_third_party` | Forces the run to be third-party-reading (there is no per-call way to force `false`; change the kind instead). Implies `sandbox: required`, `setting_sources: []` and `env.inherit: false` unless each is given explicitly in the same call, and `mcp: none` (which has no per-call flag); origin `flag-implied`. Carried flags (from the previous run) never override these implications, also when `--reads-third-party` itself is carried; an explicit flag that an implication overrides produces a warning. |
| `--auto-exit` / `--no-auto-exit` | kind's `auto_exit` | Whether `claude` is ended `auto_exit_delay` after a `done` report and a qualifying `Stop` (no background tasks). Immediately before SIGTERM, `exec` re-checks that the latest event is still that `Stop`; otherwise it waits for the next one. |
| `--env-allow VAR` | none | Repeatable; adds a name to the effective allowlist. Each name must match `^[A-Za-z_][A-Za-z0-9_]*$` (ASCII; no patterns), else exit 2. It cannot unlock `secret_env` variables (only the kind's config `env.allow` can). |
| `--env-allowlist-only VAR` | none | Repeatable. The first use replaces the kind's `env.allow` with the given names and turns off env inheritance; `--env-allowlist-only ''` gives an empty list (`''` is ignored when names are also given); any other name must match `^[A-Za-z_][A-Za-z0-9_]*$`, else exit 2. `--env-allow` adds to the replaced list. Because the kind's `env.allow` is replaced, its unlock of `secret_env` variables is off too: while allowlist-only mode is in effect no `secret_env` variable is passed, even one the kind's config `env.allow` or this flag names. |
| `--reset <setting>` | none | Resume (`resume`, `start --resume`) only. Repeatable; drops the stored value of `model`, `plugin-dir`, `env-allow`, `env-allowlist-only`, `setting-sources`, `sandbox`, `timeout`, `max-turns`, `permission-profile` or `auto-exit`. |
| `--outcome-file <path>` | none | Also accepted by `wait`. See [Outcome file](#outcome-file). |
| `--allow-tainted` | off | Resume (and fork) only. Human-only; audited (`resume_tainted`). |
| `--override-escalation` | off | Resume only (not with `--fork`), after a run that ended `escalated`. Human-only; audited. See [Escalation override](#launch-order). |
| `--actor <actor>` | `human:<me.name>` when stdin is a TTY, else `auto:unknown` | Recorded in audit records and hook payloads (`actor` is always the effective actor). An audited honor system, not authentication. |
| `--decision-id <id>` | none | Repeatable; recorded with the run (`decision_ids`) and in audit records. |
| `--dry-run` | off | Resolve everything and print the plan without allocating or launching. See [Dry run](#dry-run). |

### Flag rules

Each of these exits 2 before anything is allocated:

- `--hook-timeout <name>=…` without a `--hook <name>=…` in the same call, or an unknown hook name;
- `--timeout 0` (use `none`); `wait --timeout 0`;
- `--reset`, `--fork`, `--allow-tainted` or `--override-escalation` on plain `start` (without `--resume`) or on `exec`;
- `--reset X` together with the flag it resets (e.g. `--reset model --model m`), or an unknown `--reset` name (`reads-third-party` cannot be reset: change the kind instead);
- `--input-file -`, duplicate input basenames, or names outside the artifact-name grammar; a prompt or input file larger than `input_max_bytes`;
- `--override-escalation` with `--fork`, or when the previous run did not end `escalated`; `--allow-tainted` or `--override-escalation` without a TTY or an explicit `--actor human:*` (human-only flags; exit 2 rather than 13 because they are flags on an otherwise automatable command);
- a headless new session with neither skill nor prompt;
- outside launches: `transcript --raw --json` and `transcript --raw --paths`; `label add`/`label remove` without any `key=value` pair; `forget <id>` with a bulk-only flag (`--older-than`, `--state`, `--kind`, `--label`); `forget` without an id or `--older-than`.

Non-empty setting sources on a third-party-reading run (for example `--reads-third-party --setting-sources user`) is not a flag rule: it is a launch validation (`not-started`, `unsafe_setting_sources`, exit 12).

Untrusted or agent-generated text is never accepted *only* as a flag value: prompts come through `--prompt-file`, inputs through `--input-file`, report text through `--summary-file`/`--reason-file`. `report --summary <text>` exists for one-line summaries.

### Outcome file

`--outcome-file <path>` (made absolute and stored with the run) receives exactly what `--json` would print for the run (the outcome object), in every mode, by **whichever process finalizes the run** (`exec`, `resume`, `start`, `stop`, `forget`, an inline liveness check, `reconcile`; see [Liveness](#liveness)):

- The finalizer writes it immediately after winning the finalization compare-and-set, before draining hooks, and records `outcome_file_written_at` on the run. The liveness check and `reconcile` write it for final runs where it is unset (a finalizer that died before writing).
- It is written as a temporary file plus rename relative to a parent-directory file descriptor opened with `O_NOFOLLOW`, mode 0600.
- An allocated run's outcome file always receives the outcome object, also when `start` fails a launch validation (`start`'s stdout still prints the error envelope).
- For isolated runs the path must not be under a [session-writable path](#session-writable-paths) (compared by realpath of the path or its nearest existing parent): exit 2 at `start`, `exec` and `resume`; at adoption a rejected `--outcome-file` exits 2 and the run stays pending. A rejected path is never written.
- A refusal or usage error before a run is allocated writes its error envelope only to an `--outcome-file` given on that command line and not rejected.
- A write failure never changes the run's outcome or the command's exit code: the command prints its normal output, adds a warning, and logs `outcome_file_failed`.
- `schema_newer` and a `failed` at the schema check (step 1 of the [launch order](#launch-order)) write no outcome file: they precede parsing.

`wait --outcome-file` writes `wait`'s own output (same rules).

### Dry run

`exec`, `start` and `resume` with `--dry-run --json` print the resolved plan ([schemas/cli/exec-dry-run.json](../schemas/cli/exec-dry-run.json)); they allocate nothing, run no hooks and never finalize a run (the liveness check only reports): `dry_run: true`, `csession_id` (null when a real launch would allocate a new session or fork; otherwise the existing id, including an inspected `start` record), the `run`, `fork`, `forked_from`, `launch_kind`, `run_kind`, the effective profile, `origins` (`builtin`, `config`, `repo`, `previous_run`, `flag`, `flag-implied` or `derived` for every resolved setting; a map or list setting such as `env.set` or `env.allow` has one origin, the last layer that changed it), `labels`, `taints`, `skill`, `has_prompt`, `input_files`, `implicit_done`, `permission_profile`, `timeout`, `max_turns`, `actor`, `override_escalation`, `allow_tainted`, `sandbox_lowered`, `config_hash_changed` (adoption dry run), generated settings, the `claude` argv, environment variable names (never values), sandbox lists, `hooks` (each with `timeout`, `source: config|flag` and `selected`), `would_refuse`, `would_fail` (a launch-validation end reason; never a hook one) and `warnings`. Refusals are checked first: with `would_refuse` set it exits 20 (and still reports `would_fail`); otherwise 12 when `would_fail` is set, else 0. `schema_newer` and `failed` still print the error envelope. `exec --session-id <id> --dry-run` inspects the record without adopting it. Without `--json`, a dry run prints a short human-readable summary of the plan (kind, mode, cwd, sandbox, profile, refusal or failure).

## Flags at start and adoption

`start` stores every per-invocation flag in the launch specification; the adopting `exec` uses it.

| Flag | `start` | Stored in the launch spec | `exec --session-id` |
|---|---|---|---|
| launch-setting flags (`--kind` … `--env-allowlist-only`, `--repo`, `--label`, `--reset`) | accepted (`--reset` with `--resume`) | resolved values with their origins, plus `config_hash` | rejected (exit 2) |
| `--prompt-file`, `--input-file` | accepted | copies staged in `private/<id>/runs/<n>/staged/` | rejected |
| `--hook`, `--hook-timeout` | accepted | yes (also used for late delivery) | rejected |
| `--auto-exit`, `--no-auto-exit` | accepted | yes | rejected |
| `--allow-tainted`, `--override-escalation` | with `--resume` only; TTY or explicit `--actor human:*` checked here | yes; audited at adoption | rejected |
| `--actor`, `--decision-id` | accepted | yes | rejected |
| `--outcome-file` | accepted | yes (absolute path) | accepted; overrides the stored path (checked again; rejected: exit 2, run stays pending) |
| `--dry-run` | accepted (nothing is created) | no | accepted (inspects the record) |
| `--json` | required (exit 2 without) | no | accepted |
| `--config` | accepted | the absolute path of the config used | rejected; the stored path is loaded |
| `--verbose`, `--quiet`, `--yes` | accepted | no | accepted |

`start` freezes the fully resolved specification (all resolved values with their origins, including the resolved permission profile and the caps `summary_max_bytes`, `artifact_max_bytes`, `input_max_bytes`) plus a `config_hash` and the absolute config path. The adopting `exec` loads that config path again and reads from it only: the hooks, the timing scalars (`lost_grace`, `auto_exit_delay`, `kill_grace`, `poll_interval`, `session_start_timeout`, notes timeouts), `claude_bin` and `min_claude_version`. An invalid config at adoption ends the run `not-started` (`config_invalid`, exit 12). The adopting `exec` re-runs the validations and builds the environment as described in [Launch order](#launch-order). Late delivery of `post-state-change` events for a run with no live wrapper uses the configured hooks from the *current* config plus the stored per-invocation `--hook` entries.

## Resume and fork carry-over

Each run stores the resolved values of its launch settings. Resolution order for a new run: built-in → config kind → per-repo override → previous run's stored explicit flags → flags given now. `--reset <setting>` drops a stored value so the profile default applies again. "Kind changed" compares the new run's kind with the previous run's kind (for a fork: the source's last run).

| Setting | Same kind as previous run | Kind changed | Notes |
|---|---|---|---|
| kind (`--kind`) | the previous run's kind when omitted | given now | the launch kind never changes |
| `--repo` | carried | carried | |
| cwd (`--cwd`) | carried | carried | resume: always the previous run's resolved cwd (a different `--cwd` exits 2); fork: the source's resolved cwd unless `--cwd` is given now |
| `--mode` | carried | carried | the default of `--mode` on resume is the previous run's mode |
| `--timeout`, `--max-turns` | carried | not carried | dropped when `--mode` changes; `--reset timeout`, `--reset max-turns` |
| `--model` | carried | not carried | `--reset model` |
| `--permission-profile` | carried (snapshot) | not carried | a profile file is never re-read; `--reset permission-profile` |
| `--plugin-dir` | carried | not carried | given now: replaces the stored list; a carried directory that no longer exists fails the launch (`plugin_dir_missing`); `--reset plugin-dir` |
| `--env-allow` | carried | not carried | stored separately; replaced only by `--env-allow` given now; `--reset env-allow` |
| `--env-allowlist-only` | carried | not carried | stored separately; replaced only by `--env-allowlist-only` given now; allowlist-only mode persists until `--reset env-allowlist-only` |
| `--sandbox` | carried | not carried | `--reset sandbox` |
| `--setting-sources` | carried | not carried | `--reset setting-sources` |
| `--reads-third-party` | carried | not carried | cannot be reset |
| `--auto-exit` / `--no-auto-exit` | carried | not carried | `--reset auto-exit` |
| `--label` | labels belong to the session | same | given now: added |
| prompt, `--input-file` | only from a previous run that ended `not-started` | same | given now: replaces the carried prompt or input set; earlier inputs stay readable in `inputs/<n>/`; fork: see below |
| `--skill` | never | never | the skill is re-invoked only if `--skill` is given |
| `--hook`, `--hook-timeout`, `--outcome-file`, `--dry-run`, `--allow-tainted`, `--override-escalation`, `--actor`, `--decision-id`, `--reset` | never | never | per invocation |

A flag given now always replaces the stored value.

**Resume.** `resume <id>` runs `claude --resume <latest claude_session_id>`. A session with no captured Claude session id (it never reached `SessionStart`: launch-phase end, quit at the trust dialog, early crash) is resumed as a **fresh conversation**: a new Claude session id via `--session-id`, `SessionStart` source `startup`, with a warning. A session in state `not-started` is resumable. Refusals, in order: live session or live `claude` child (exit 20, `live`; not checked with `--fork`); tainted session resumed in a kind that is not third-party-reading without `--allow-tainted` (exit 20, `tainted`).

**Fork.** `--fork` creates a new session (`claude --resume <claude id> --fork-session --session-id <new uuid>`) with `forked_from` set. Its run 1 inherits from the source's last run per the table, including the source's resolved cwd unless `--cwd` is given (any directory that passes the [path rules](#path-rules); a caller may fork into another directory, for example a disposable copy of the source's repository). Without `--cwd`, a source whose cwd is a session `work/` directory is refused (`fork_cwd_unsupported`). Labels are not copied: the fork gets `kind=<its run-1 kind>` plus the given `--label`s. Taint is copied; a tainted source forked into a kind that is not third-party-reading requires `--allow-tainted`. `--override-escalation` with `--fork` exits 2. The fork copies the source's whole `inputs/` directory into its own session directory (earlier inputs stay readable; the copy is fd-based and rejects symlinks, non-regular files and files with more than one link); when the source's current run ended `not-started`, its undelivered prompt and inputs become the fork's run-1 prompt and inputs (unless given now). The source is unchanged and may be live. A source with no captured Claude session id has no conversation to fork: the fork starts a fresh conversation (as a resume would), with a warning.

A resume always runs in the previous run's resolved cwd. A fork runs in the source's cwd or in the `--cwd` given (live A6: Claude Code resumes and forks a conversation from a different cwd; the fork's transcript path is taken from its `SessionStart` payload, as for every run). A plain resume from a different cwd would append to the original project's transcript; claude-session does not allow it.

### Taint

A session is tainted when a third-party-reading run is **allocated** (step 4 of the [launch order](#launch-order)), not when it launches: a run that then ends `not-started` (failed validation, veto, cancel, expiry) still taints the session, and the taint is never removed. A tainted session resumed or forked into a kind that is not third-party-reading is refused (`tainted`) unless `--allow-tainted` is given (human-only, audited `resume_tainted`).

## Commands

### `exec`

```text
claude-session exec [launch flags] [--json] [--outcome-file <path>]
claude-session exec --session-id <id> [--dry-run] [--json] [--outcome-file <path>]
```

Runs a session in the foreground and exits with its outcome code. Without `--session-id`, performs every step of the [launch order](#launch-order) for a new session. With `--session-id`, adopts the pending run created by `start` (a new session, a fork or a pending resume); it accepts only `--json`, `--dry-run`, `--outcome-file` and the global `--verbose`, `--quiet` and `--yes` (anything else exits 2).

Headless runs: if no `SessionStart` arrives within `session_start_timeout` (default 120 s), `exec` logs a warning and adds it to `warnings` (injected hooks may be suppressed; see [Residual risks](#residual-risks)). Interactive runs are exempt (they may wait at the trust dialog).

**Headless output.** Headless runs use `claude -p --output-format stream-json --verbose`; stdout goes to the private `result.json` (one JSON object per line) and stderr to the private `stderr.log`, never to the caller. `-p` waits for background subagents and emits one `result` object per model turn: the **final result** is the last object with `type: result`; `cost_usd` is the sum of `total_cost_usd` over all `result` objects and `num_turns` their sum (a value that is not a finite non-negative number, or not a non-negative integer, is left out; a cost sum that is not finite is null). A **complete result** means the file holds at least one `result` object, its last object is one, and the file does not end in a line without its newline: such a tail (a writer killed mid-line) makes the result incomplete even when it parses (the `live` tests check that Claude Code ends its last `stream-json` line with a newline). The headless table of [Outcome mapping](#outcome-mapping) reads the final result; claude's own exit status is never used when salvaging a run whose wrapper was lost.

**Internal error after spawn.** If `exec` hits an internal error while supervising, it keeps supervising when it can; if it must exit, it records `pending_cause` `internal_error`, sends SIGTERM to the child and finalizes the run (end reason `internal_error`; outcome `killed` unless a report exists) before exiting 1.

#### Signals

Normative. "Forwarded" means sent once to the verified child, after `pending_cause` is recorded.

| Signal | Interactive, session phase | Headless, session phase | Launch phase (adopted or launching, before spawn) |
|---|---|---|---|
| SIGINT | ignored by `exec` (`claude` receives it from the terminal; `exec` never re-forwards it) | forwarded (`forwarded_int`) | run ends `not-started`, `launch_interrupted` |
| SIGTERM | forwarded (`forwarded_term`) | forwarded (`forwarded_term`) | `launch_interrupted` (`canceled` when `stop_requested_at` is set) |
| SIGHUP | forwarded (`forwarded_hup`) | forwarded (`forwarded_hup`) | `launch_interrupted` (`canceled` when `stop_requested_at` is set) |
| SIGTSTP, SIGCONT | default action (job control applies to the foreground process group) | default action | default action |

A SIGTERM from `stop` is recorded as `pending_cause` `stopped` (end reason `stopped`), not `forwarded_term`. Signal handlers are installed before the adoption compare-and-set (and before allocation in `exec`/`resume`). If `stop` has to SIGKILL a wrapper, `stop` finalizes the run itself through the liveness path. Headless children also get `PR_SET_PDEATHSIG(SIGTERM)` on Linux, so they do not outlive a killed wrapper; `stop` remains the remedy for any other orphan.

Output: with `--json`, prints the [outcome object](#outcome-object) on exit ([schemas/cli/exec.json](../schemas/cli/exec.json)). In interactive mode the terminal belongs to `claude`, so the JSON is printed only after `claude` exits; use `--outcome-file` to collect it without parsing the terminal. Without `--json`, prints a one-line summary on stderr at exit.

An interactive session started in a directory Claude Code has never trusted shows Claude's trust dialog before `SessionStart` (trust is inherited from a trusted parent directory). The dialog fires no hooks; the session stays `starting` until the user accepts. Quitting there (Esc exits 0, "No, exit" exits 1) ends the run `exited-unreported` with `no_session_start` and leaves no Claude session id; a later resume starts a fresh conversation.

### `start`

```text
claude-session start [launch flags] [--dry-run] --json
claude-session start --resume <id> [--fork] [launch flags] [--reset <setting>]... [--allow-tainted] [--override-escalation] [--dry-run] --json
```

Performs steps 1–5 of the [launch order](#launch-order), stages the prompt and input copies, stores the launch specification and every per-invocation flag ([table](#flags-at-start-and-adoption)), and prints `{"csession_id", "csession_dir", "argv", "run", "forked_from", "expires_at", "required_env_names", "env_inherit", "warnings"}` ([schemas/cli/start.json](../schemas/cli/start.json)), where `argv` is `[<absolute claude-session path>, "exec", "--session-id", "<id>"]`, `run` is the pending run, `required_env_names` the names the adopting process should have (`claude_auth_env` names in use, the kind's `env.allow` names and the `env.set` names) and `env_inherit` whether the session inherits the rest of that environment. The caller places `argv` in a terminal or process of its choice (with the environment described in [Launch order](#launch-order)). A launch-validation failure exits 12 with the error envelope (`code: launch_failed`, with `run`; the run's outcome file still receives the outcome object). `pre-launch` hooks run in the adopting `exec`.

With `--resume <id>`, `start` runs the inline [liveness check](#liveness) and the resume refusal checks synchronously and creates run n as a pending run. Without `--fork`, the printed id is the existing session's id; with `--fork`, a new fork id is allocated and printed with `forked_from`.

### `resume`

```text
claude-session resume <id> [launch flags] [--fork] [--reset <setting>]... [--allow-tainted] [--override-escalation] [--json] [--outcome-file <path>]
```

Runs a new run of an existing session in the foreground and exits with its outcome code. Performs every step of the [launch order](#launch-order); the refusal checks and the recording of the new run happen in one transaction. What the run inherits, fresh-conversation resumes and forks: [Resume and fork carry-over](#resume-and-fork-carry-over). A resume whose launch fails ends `not-started` (final). Output: the [outcome object](#outcome-object) ([schemas/cli/resume.json](../schemas/cli/resume.json)).

### `stop`

```text
claude-session stop <id> [--grace <duration>] [--actor <actor>] [--decision-id <id>]... [--dry-run] [--json]
```

Ends a session's current run deliberately ([schemas/cli/stop.json](../schemas/cli/stop.json)). Audited (`stop`).

1. Runs the inline [liveness check](#liveness), then records `stop_requested_at` on the run (in a transaction).
2. **Pending run** (no wrapper, not final): cancel is a compare-and-set on "no wrapper and not final". If it wins: no signals; the run ends `not-started` with end reason `canceled`; `stop` writes the final event, delivers hooks and writes the outcome file; exit 0 (`canceled: true`). If adoption won, `stop` continues with step 3.
3. **Run with a wrapper**: SIGTERM to the verified wrapper PID (it forwards to `claude`, or, in its launch window, ends the run `canceled`). After `--grace` (default `kill_grace`), SIGKILL to the verified child first, then to the wrapper if it is still alive; a SIGKILLed wrapper cannot finalize, so `stop` then finalizes the run through the liveness path (`recorded_by_stop: true`).
4. **Run whose wrapper is gone** with a live child: the same signals to the verified child; `stop` records the final state itself (`recorded_by_stop: true`). This is the remedy for an orphaned child; `reconcile` never signals.
5. `stop` waits until the run is final (up to grace + `kill_grace` + 5 s). Output: `ended`, `run_exit_code` (the run's outcome code), `signals` (each `{pid, role: wrapper|child, signal}`), `state`, `outcome`, `end_reason`. With `ended: false`, `run_exit_code`, `outcome` and `end_reason` are null and `state` is live; with `canceled: true` or `already_final: true`, `signals` is empty.

The outcome follows [Outcome mapping](#outcome-mapping) (`stopped`: `killed` without a report; a run that reported keeps its report's outcome). Exit 0 when the run ended; 12 with `ended: false` if it is still live after the wait; 10 for an unknown session. A run that is already final is left alone: exit 0 with `already_final: true`. Teardown and `forget` never end live runs; `stop` is the one command that does, only when a human or an explicitly configured caller runs it.

### `wait`

```text
claude-session wait <id> [--run <n>] [--timeout <duration>] [--outcome-file <path>] [--json]
```

Binds to the session's current run at invocation (or to run `n` with `--run`), blocks until that run is final, then exits with its outcome code and prints the [outcome object](#outcome-object) with `--json` ([schemas/cli/wait.json](../schemas/cli/wait.json)), plus `timed_out`, `run_exit_code` and `pending`. Each poll (every `poll_interval`) runs the inline [liveness check](#liveness), so `wait` on a pending run never adopted ends with `not-started` (`never_adopted`, exit 12) once it expires.

- If the run is already final, returns at once.
- If `--timeout` expires first, exits 22 with `timed_out: true`, `exit_code: 22`, `run_exit_code: null`, null `outcome` and `end_reason`, the run's current `report` (or null) and `state`, `pending`, and `phase` (`launch` while the run is pending, `session` after adoption).
- If the session is forgotten while waiting, exits 10 with the error envelope.
- `--timeout 0` exits 2.

### `report`

```text
claude-session report done|blocked|failed|needs-input
    (--summary-file <path>|- | --summary <text>)
    [--reason-file <path>|-] [--artifact <name|path>] [--json]
```

Run by the agent inside its session. Requires `CSESSION_ID` and `CSESSION_DIR` (else exit 2). It never opens `state.db` (so it is exempt from the schema check). It reads the protected `run.json` for the state root, the run number, the limits and the device and inode of `outbox/` and `artifacts/`, and requires `realpath(CSESSION_DIR)` to equal `<state_root>/sessions/<CSESSION_ID>` (else exit 2).

- `done` requires a non-empty summary; `blocked`, `failed` and `needs-input` require a reason (a summary is optional for them). A `--summary` text that begins with `-` must be given as `--summary=<text>`.
- **Command forms.** Only a bare `claude-session report …` command matches the generated `Bash(claude-session report:*)` rule; the generated rules also allow `Bash(<absolute claude-session path> report:*)` (the install path is per machine, so the instructions may name it). A command prefixed with `cd … &&` or containing `$(…)` is denied (live test). The standard instructions show exactly these forms:
  - write the text with the Write tool to `$CSESSION_DIR/artifacts/summary.md` (or `reason.md`), then `claude-session report done --summary-file <path>`;
  - `claude-session report done --summary="<one line>"`;
  - a heredoc: `claude-session report done --summary-file - <<'EOF'` … `EOF`;
  - the absolute path of `claude-session` in place of the bare name, as a fallback when `PATH` does not resolve it.
  The permissions for this are generated for every run ([Generated settings](#generated-settings)), so it works under `dontAsk`. `--summary` text is equally untrusted.
- `--summary-file`/`--reason-file` is `-` (stdin) or any readable regular file (opened with `O_NOFOLLOW`); its content is checked like any summary (`contains_secret` rejects secrets).
- Size: the summary and the reason are each at most `summary_max_bytes` bytes of UTF-8 after decoding; the whole request file is at most 4 × `summary_max_bytes` + 16 KiB (written with `ensure_ascii=False`).
- `--artifact` takes a bare [artifact name](#identifiers) or a path whose lexical parent (no symlink resolution; repeated slashes are collapsed) is exactly `$CSESSION_DIR/artifacts` as given in the environment (the realpath of a symlinked `$CSESSION_DIR` is refused); the instructions use the path form. The file is opened with `O_NOFOLLOW` and must be a regular file with one link, at most `artifact_max_bytes`; `outbox/` and `artifacts/` must match the device and inode in `run.json`. The instructions say to attach the artifact again on every report.
- Validation failures exit 2 with a message. On success, the request (fields include `run`) is written atomically to `$CSESSION_DIR/outbox/<uuidv7>.json` (a temporary file in `outbox/` under a reserved `.tmp-<hex>` name, renamed within `outbox/`), the command prints `report queued` (or [schemas/cli/report.json](../schemas/cli/report.json); `request_id` is the uuid) and exits 0.
- **Ingestion** (architecture section 8.3) happens in the injected hooks and repeats every check:
  - `outbox/` and `artifacts/` are opened without following symlinks and must be the directories `exec` created (same owner, device and inode).
  - Each drain reads at most 4096 directory entries and claims at most 64 requests; other names (not matching the UUIDv7 pattern) and directories are rejected `malformed`, removed and logged as one aggregate record per drain, never fed back to the agent; `report`'s temporary files are left alone. Of the claimed requests the newest 16 are processed, newest first (the UUIDv7 names order them): the first that passes every check is ingested, so at most one report is ingested per drain; older requests are discarded unread, and those over the cap discarded unchecked; both are logged. A request older than a report already ingested for the run is discarded. A drain stops claiming and checking once half its hook budget is spent; what it did not reach waits for the next drain.
  - Each request is **claimed** by an atomic rename into the run's private directory before it is processed; `request_id` is unique in `state.db`, so a request is ingested at most once. Files must be regular, with one link, and are size-checked before reading.
  - The artifact is copied through one `O_NOFOLLOW` file descriptor into a private temporary file; only that copy is hashed, scanned (`contains_secret`) and compared with the request's hash, then frozen. The copy and the scan happen before the state transaction; only the state change is committed inside it.
  - Ingestion runs inside a hook with an explicit timeout ([Generated settings](#generated-settings)); a hook that Claude Code cancels at its timeout commits nothing, and the request (still in the outbox, or claimed but not committed) is taken up by the next drain.
  - If ingestion rejects a request, the agent is told the reason through the `PostToolUse` hook and should report again; only when the newest processed request was rejected (an older request's rejection no longer matters). An untrusted `outbox/` (`outside_dir`) and malformed names are logged, never fed back. Rejection reasons: `bad_secret`, `not_live`, `stale_run` (request for another run), `artifact_changed`, `artifact_missing`, `outside_dir`, `not_regular_file`, `too_large` (over a size limit, or the `contains_secret` scan of the first request a drain checks did not finish within the drain's budget, when that check began with at least 90% of the budget left; any other request whose scan runs out of time is left for the next drain), `contains_secret`, `malformed`.
- `contains_secret`: the summary, reason, artifact name or artifact contains the value (8 or more characters) of a passed `claude_auth_env` variable, a `secret_env` variable present in `exec`'s environment, or the run secret (in any letter case; `report` refuses it too, exit 2). `exec` stores only keyed hashes of those values, in the private run directory; that index is secret-equivalent (the values can be recovered from it), is never readable by the agent and is deleted at run end. The values may total at most 256 KiB (as environment bytes); `exec` refuses to launch with a larger set. The agent is told to remove the value.
- Within a run, the latest ingested report wins; the run's `report.artifact` is the latest report's artifact or null (earlier frozen files stay on disk).

### `status`

```text
claude-session status <id> [--run <n>] [--json]
```

Runs the inline [liveness check](#liveness) (which may finalize a run; `status` never runs hooks), then shows a session and its current run (or run `n`) ([schemas/cli/status.json](../schemas/cli/status.json)): top-level `pending`, `expires_at`, `updated_at` and `warnings` (e.g. a live run's recorded hook interpreter path no longer exists); the run's own `state`, `phase`, `message`, `stop_requested_at`, `warnings`, report, frozen artifact path and SHA-256, outcome and end reason, cost, skill and whether a prompt was given, input files, `implicit_done`, permission profile (name or `file:<sha256 prefix>`), whether the sandbox actually ran ([`sandbox_active`](#platform-notes)), decision ids, liveness of the wrapper and child, taint, labels, the Claude session ids seen, `session_started_at`, and the last 20 state events of the session (oldest first) with their `event_id`, `event_ts`, activities, `needs_input_reason`, `cause`, `final` and whether hooks were `delivered`. `run.started_at` is null while the run is pending. A pending run has `phase: launch`, state `starting`, a non-null `expires_at`, null `started_at` and null `cwd` (`run.cwd` is null until the working directory is resolved: pending runs, and runs that ended during launch); `outcome`, `ended_at` and `exit_code` are null exactly while the run is live. Runs that `status` (or `list`) finalizes get their final events delivered by the next delivering command or by `reconcile` (cron, or agent-spawn's reconcile).

### `list`

```text
claude-session list [--label key=value]... [--state <state>]... [--kind <kind>] [--launch-kind <kind>]
    [--run-kind <kind>] [--mode interactive|headless] [--needs-input] [--pending] [--repo <repo-id>]
    [--since <time>] [--limit <n>] [--forked-from <id>] [--report-status <status>|none]...
    [--live] [--tainted] [--json]
```

Lists sessions that are not forgotten, most recently updated first, with no default limit ([schemas/cli/list.json](../schemas/cli/list.json)); items include `pending`, `phase` and `expires_at` (of the current run), `last_outcome`, `last_end_reason` and `exit_code` (of the current run; null while it is live), `report_status` and `updated_at`. `--label` is repeatable and ANDed; `--state` and `--report-status` are repeatable and ORed (`--report-status none` matches a current run without a report); `--kind` matches the launch kind or any run kind, `--launch-kind` only the launch kind, `--run-kind` only the current run's kind; `--needs-input` is `state == needs-input`; `--pending` selects sessions whose current run is pending; `--since` takes a duration (that long ago) or an ISO 8601 time (UTC when it has no offset); the duration grammar is tried first, so bare digits such as `20240101` are seconds; it matches `updated_at` (the time of the last change to the session record: state, activity, report, run, labels or taint); `--limit` caps the number of results (at least 1, else exit 2). Like `status`, `list` may finalize runs through the liveness check and never runs hooks.

### `kinds`

```text
claude-session kinds [--kind <name>] [--repo <repo-id>] [--json]
```

Prints the effective kind profiles (built-in merged with config, and per-repo overrides when `--repo` is given), each with its effective `PATH`, read and write directories, sandbox availability, `implicit_done` and `warnings` (e.g. a third-party-reading kind that would run unsandboxed); and per-profile `required_env_names`; plus the resolved `claude` path, `claude --version`, `claude_version_ok`, `min_claude_version` and state-directory warnings ([schemas/cli/kinds.json](../schemas/cli/kinds.json)). `--kind <unknown>` shows the `review` profile with `fallback: true`.

### `label add` / `label remove`

```text
claude-session label add <id> key=value... [--dry-run] [--json]
claude-session label remove <id> key=value... [--dry-run] [--json]
```

Labels are a multiset of `key=value` pairs (design guide section 9; grammar in [Identifiers](#identifiers)). At least one pair is required (else exit 2). Adding an existing pair is a no-op; removing a pair that is absent is a no-op. Prints the resulting labels and the pairs actually `added` or `removed` ([schemas/cli/label-add.json](../schemas/cli/label-add.json), [schemas/cli/label-remove.json](../schemas/cli/label-remove.json)). No label key has built-in meaning; `kind=<launch kind>` is added at creation for display.

### `transcript`

```text
claude-session transcript <id> [--raw | --paths] [--subagents] [--run <n>] [--json]
```

- Default: the session's Claude Code transcripts rendered as readable text, in order (one section per Claude session id, headed by its source and run): user and assistant text, tool calls as one-line summaries, tool results truncated to 20 lines, offloaded tool results referenced by path.
- `--raw`: the transcript JSONL files concatenated, unchanged (no control-character stripping); a newline is added after a file that does not end in one when another file follows, so records of different files never share a line. Meant for programs: do not print it to a terminal. `--raw --json` and `--raw --paths` exit 2.
- `--paths` / `--json`: the list of files ([schemas/cli/transcript.json](../schemas/cli/transcript.json)), each with its subagent transcripts.
- `--subagents`: include subagent transcripts (inline after the tool call that started them; with `--raw`, appended after their parent file).
- `--run <n>`: only the files of run `n`.

All transcript content is untrusted. claude-session never copies transcripts; files Claude Code has deleted are reported with `exists: false`. A read error exits 12 (`failed`).

- A recorded transcript path (from a `SessionStart` payload) is used only when it is `<root>/<project>/<claude session id>.jsonl` for a root `$HOME/.claude/projects` or `$CLAUDE_CONFIG_DIR/projects` (as written or resolved), in normal form and printable unchanged. Any other is listed with `path: null` and a warning on stderr, and never read. A root that resolves to `/`, `$HOME`, an ancestor of `$HOME`, or a directory that is or contains a credential path ([Path rules](#path-rules)) is not used (a warning on stderr).
- Files are opened below the root one component at a time without following symlinks. A transcript, subagent or meta file that is not a regular file with one link owned by the user, a directory on the way (`subagents` and `tool-results` included) that is a symlink or not a directory owned by the user, or a root writable by others (group write is allowed), exits 12 (`failed`) before any output. An offloaded tool result is referenced by path only if it is a regular file with one link owned by the user; otherwise the text shows `[offloaded result not shown: unsafe file]` and stderr a warning. A file is read up to its size when opened (a live session keeps appending); reading stops with exit 12 after 300 seconds spent reading (time waiting for the output to be consumed does not count).
- Read caps: a JSONL record over 8 MiB is skipped (text shows `[record over 8388608 bytes skipped]`), a record that is not a JSON object is skipped (text shows `[unparseable record skipped]`), a meta file over 64 KiB gives null fields, a directory listing keeps at most 10,000 matching names after scanning at most 100,000 entries (a warning on stderr when either bound is hit), and a rendered text line is cut at 4000 characters (`... (<n> more characters)`).
- `--paths` prints the existing files, each main file followed by its subagent files (as `--json` lists them, with or without `--subagents`); missing files are warned about on stderr. `--raw` skips missing files the same way (missing subagent files are warned about only with `--subagents`).
- `--run <n>` selects the Claude session ids first seen in run `n`: a plain resume keeps its Claude id, so what it appends stays in the file of the run that started that id.

### `forget`

```text
claude-session forget <id> [--actor <actor>] [--decision-id <id>]... [--dry-run] [--json]
claude-session forget --older-than <duration> [--state <state>]... [--kind <kind>] [--label key=value]...
    [--actor <actor>] [--decision-id <id>]... [--dry-run] [--json]
```

Hides sessions: later commands report them as not found (exit 10; adopting a forgotten session also exits 10). Audited (one record per session, also in bulk mode). The record, session directory and private directory are deleted by `reconcile` `retention_days` after `forget`. Prints `{"dry_run", "forgotten": [...], "skipped": [...]}` ([schemas/cli/forget.json](../schemas/cli/forget.json)).

- `forget` runs the inline [liveness check](#liveness) first (changes have `cause: forget`).
- `forget <id>` refuses a live session (exit 20, `live`), including any session whose current run is pending (a pending resume or a pending new-session record): cancel it with [`stop`](#stop) first. Bulk-only flags with an id exit 2; `forget` requires an id or `--older-than` (exit 2 otherwise, also when only filters are given).
- Bulk mode (`--older-than`, matching `updated_at`, with optional `--state`, `--kind` (the launch kind) and `--label` filters) forgets every match and skips live sessions, pending ones included (`reason: live`); exit 0.

Callers such as gatectl and ticket-queue should forget their headless sessions after collecting the results.

### `reconcile`

```text
claude-session reconcile [--dry-run] [--json]
```

Safe under concurrent invocation (lease `reconcile`; a second caller exits 0 reporting it is already running). Repairs state against reality and reports every change with its `run`, `outcome` and `end_reason` ([schemas/cli/reconcile.json](../schemas/cli/reconcile.json)). It applies the [liveness check](#liveness) to every live session, delivers undelivered events, and also:

- a run whose wrapper is gone but whose child is alive → listed in `orphans` (not a change; it stays live; `stop` ends it; reconcile never signals);
- a wrapper-lost run with a recorded `pending_cause` → finalized per the cause table (`finalized_from_cause`);
- final runs without `outcome_file_written_at` → the outcome file is written;
- final events of runs finalized by `status` or `list` (which never deliver) are delivered;
- pending outbox requests of runs that are already final are discarded and logged;
- private temporary directories of final runs are removed;
- forgotten sessions older than `retention_days`: record, session directory and private directory deleted (fd-based, never following symlinks; ids are kept so they are never reused);
- warns (in `warnings`) when a live run's recorded hook interpreter path no longer exists.

Changes made by reconcile have `cause: reconcile`. Leases (`reconcile`, `delivery:<id>`) expire after 60 s; the holder renews its lease every 20 s while it works (hook delivery renews it during long hooks); a dead holder's lease may be taken over at once.

#### Liveness

`status`, `list`, `wait` (each poll), `resume`, `start --resume`, `stop`, `forget` and `reconcile` check a session's liveness inline (wrapper and child PID plus start time). They finalize a run only when its **wrapper is dead** (process gone or zombie) or it has none; while the wrapper is alive, `exec` owns the run. Finalization is a compare-and-set on the run not having ended (`ended_at` unset), so there is exactly one final event, one final `post-state-change` and one outcome-file write per run. Changes made by the inline checks have `cause: liveness`.

| Situation | Result |
|---|---|
| pending run (no wrapper), `now ≥ expires_at` | `not-started`, `never_adopted` |
| adopted, wrapper dead, no `child_pid` recorded | `not-started`, `launch_interrupted` |
| wrapper dead, child alive | stays live (listed in reconcile's `orphans`); its events are delivered late |
| wrapper and child dead | ingest that run's pending outbox requests (secret checked against that run's stored hash), then finalize per [Wrapper-lost mapping](#outcome-mapping) and write the outcome file |

Requests for other runs are discarded and logged. A run that is already final is never ingested into.

### `export`

```text
claude-session export [--since <time>] [--action <action>]... [--json]
```

Prints audit records (design guide section 6), oldest first: all of `audit.jsonl`, or those since `--since` (duration or ISO 8601 time, read as for `list`), or with one of the given `--action`s (each one of the `action` values of [audit.json](../schemas/audit.json), else exit 2) ([schemas/cli/export.json](../schemas/cli/export.json), an array whose items `$ref` [audit.json](../schemas/audit.json)). Without `--json`, one line per record.

### `cron install` / `cron uninstall`

```text
claude-session cron install [--interval <minutes>] [--env-file <path>] [--dry-run] [--json]
claude-session cron uninstall [--dry-run] [--json]
```

Writes or removes the managed crontab block `# claude-session managed`, which runs `claude-session reconcile` every `--interval` minutes (1–59, default 10; else exit 2) with an explicit `PATH` including `~/.local/bin`, optionally prefixed by sourcing `--env-file` (it must be owned by the user and not group- or world-writable, else exit 2). Install: `action` is `installed`, `updated` or `unchanged`, and `block` is the managed block as it is in the crontab afterwards (or would be, with `--dry-run`) ([schemas/cli/cron-install.json](../schemas/cli/cron-install.json)). Uninstall: `action` is `removed` (`block` is the removed block) or `unchanged` (`block` null; no managed block was present) ([schemas/cli/cron-uninstall.json](../schemas/cli/cron-uninstall.json)). No `crontab` command exits 12 (`failed`). Not needed when agent-spawn's reconcile (which runs this one) is scheduled; the inline liveness check keeps single sessions correct without cron.

### Internal commands

- `hook-event --state-dir <abs> <event>`: run by the hooks claude-session injects into sessions, as `<absolute interpreter> -I -m claude_session hook-event --state-dir <abs state dir> <Event>` (Python isolated mode). It reads the Claude Code hook payload on stdin, identifies its session from `CSESSION_ID`, and reads its configuration from the private directory (never from an agent-writable file, never deriving the state directory from the session environment). Events: `SessionStart`, `UserPromptSubmit`, `PermissionRequest` (including `tool_name: AskUserQuestion`), `Notification` (including matcher `elicitation_dialog`), `PostToolUse` (all tools), `Stop`, `SessionEnd`. Each injected hook has an explicit `timeout` (`PostToolUse` and `Stop` 60 s, the others 30 s) and finishes well within it: Claude Code SIGTERMs a hook at its timeout, drops its output and continues. It always exits 0 (it acts on Claude Code only through its JSON output); on an internal error it logs at `error` and prints nothing; `exec` re-derives the final state when `claude` exits. It never migrates the database (see [Upgrades](#upgrades)). Not for direct use.
- `verify-secret <full id>` (`cs-` plus all 8 characters; no prefix matching): runs outside sessions (agent-spawn) and opens the database; reads a secret on stdin (trailing whitespace stripped); exits 0 if it matches the session's current-run secret and the current run is adopted (wrapper set) and live, 11 otherwise (including unknown sessions, to avoid an oracle). Used by agent-spawn's self-labeling. `--json` prints `{"match": true|false}` ([schemas/cli/verify-secret.json](../schemas/cli/verify-secret.json)).

## Global flags

`--json`, `--config <path>`, `--verbose`, `--quiet`, `--version`, `--yes` (accepted by every command and a no-op: claude-session never prompts for confirmation). Internal commands (`hook-event`, see [Internal commands](#internal-commands)) take only their documented argv; global flags do not apply to them. Global flags follow the command name (`claude-session list --json`); only `--version` is also accepted before it. `--verbose` and `--quiet` are mutually exclusive (exit 2). `--help` is exempt from `--json`, and `--version` prints plain text even with `--json`. `--dry-run` is accepted by `exec`, `start`, `resume`, `stop`, `forget`, `label add/remove`, `reconcile` and `cron install/uninstall` (for the non-launch commands it prints what would change).

Human-readable output of untrusted fields (transcript text, summaries, reasons, subagent descriptions) strips or escapes C0 and C1 control characters. `--json` output carries them JSON-escaped; `transcript --raw` prints files unchanged.

## States

`state` is one of:

- live: `starting`, `working`, `idle`, `needs-input`, `reported`;
- final: `exited`, `exited-unreported`, `killed`, `timeout`, `lost`, `not-started`.

`needs-input` carries `needs_input_reason`: `permission` (a permission prompt is waiting), `question` (an `AskUserQuestion` prompt is waiting: Claude Code fires `PermissionRequest` with `tool_name: AskUserQuestion`), `elicitation` (an MCP elicitation dialog is waiting; the `elicitation_dialog` matcher is not live-tested without an MCP server) or `escalation` (the run reported `needs-input`). Pressing Esc at a question, like denying a permission, fires no hook. `activity` (`working` or `idle`) is tracked in every live state after the first activity event, including `reported` and `needs-input`, so a headless agent working after it escalated is visible; it is null in `starting` and in final states.

`not-started` means the run's `claude` process never started (a launch-phase end). `claude` exiting after it was spawned but before `SessionStart` is `exited-unreported` with end reason `no_session_start`. A final state lasts until a resume starts a new run.

## State transitions

The state is **derived** from four per-run values, which the events below change:

- `activity`: null, `working` or `idle`;
- `prompt`: none, `permission`, `question` or `elicitation` (a prompt waiting for the user);
- `escalation_open`: true from an ingested `needs-input` report until the next `UserPromptSubmit`;
- `report`: the run's latest report (or none).

**Derivation** (live runs), first match: `prompt` set → `needs-input(prompt)`; `escalation_open` → `needs-input(escalation)`; a report exists → `reported`; `activity` null → `starting`; otherwise `activity` (`working` or `idle`). So `permission`, `question` and `elicitation` take precedence over `escalation`, and `needs-input` over `reported`. `post-state-change` fires when `state`, `activity` or `needs_input_reason` changes, and when the run's report status or `reported_at` changes (cause `report`); the event's `cause` is the hook event or internal cause named below.

| Event (`cause`) | Applies in | Changes | Resulting state (activity) |
|---|---|---|---|
| `start` / `start --resume` / `exec` / `resume` creates the run (`launch`) | — | new run, all values reset | `starting` (null) |
| adoption (`adopt`) | `starting` (pending) | wrapper set; no state change | `starting` (null) |
| launch-phase end: validation failure, veto, escalation, `no_tty`, `canceled`, `never_adopted`, `launch_interrupted` (`launch`, `adopt`, `stop`, `liveness`, `reconcile`) | `starting` | final | `not-started` (null) |
| `SessionStart` `startup`/`resume`/`fork` (`SessionStart`) | `starting` | headless: activity `working`; interactive: activity `idle`; Claude session id recorded | `working` (working) or `idle` (idle) |
| `SessionStart` `startup`/`resume`/`fork` | any other live state | Claude session id recorded | unchanged |
| `SessionStart` `clear`/`compact` | any live state | Claude session id recorded (`clear` gives a new one); context line re-added | unchanged |
| `UserPromptSubmit` | any live state | activity `working`; prompt none; `escalation_open` false | `reported` (working) if a report exists, else `working` (working) |
| `PermissionRequest` | any live state after `SessionStart` | prompt `permission`; activity `working` | `needs-input(permission)` (working) |
| `Notification` `permission_prompt` (follows a `PermissionRequest` by about 6 s) | any live state after `SessionStart` | prompt `permission` unless a `question` or `elicitation` prompt is waiting (then kept); activity `working` | `needs-input(question\|elicitation)` (working) if one was waiting, else `needs-input(permission)` (working) |
| `PermissionRequest` with `tool_name: AskUserQuestion` | any live state after `SessionStart` | prompt `question`; activity `working` | `needs-input(question)` (working) |
| `Notification` `elicitation_dialog` | any live state after `SessionStart` | prompt `elicitation`; activity `working` | `needs-input(elicitation)` (working) |
| `PostToolUse` (a tool ran, so any prompt was approved) | any live state after `SessionStart` | prompt none; activity `working` | `needs-input(escalation)` (working) if `escalation_open`, else `reported` (working) if a report exists, else `working` (working) |
| `Stop` with non-empty `background_tasks` | any live state after `SessionStart` | activity `working` | derived: prompt kept (`needs-input(permission\|question\|elicitation)`), else `needs-input(escalation)`, `reported` or `working` (working) |
| `Stop` blocked by the backstop (headless, once per run: the injected `Stop` hook blocks with a fixed message because no report was filed; a run whose kind has `backstop: false` has `backstop_used` true from launch and is never blocked) | any live state after `SessionStart` | nothing: activity unchanged, no event | unchanged |
| `Stop` without background tasks, `Notification` `idle_prompt` (about 60 s after the turn) | any live state after `SessionStart` | activity `idle`; prompt kept (a denied prompt stays until `UserPromptSubmit`) | derived: `needs-input(permission\|question\|elicitation)`, else `needs-input(escalation)`, `reported` or `idle` (idle) |
| report ingested, `done`/`blocked`/`failed` (`report`) | any live state | report set; `escalation_open` false | `needs-input(permission\|question\|elicitation)` if a prompt is waiting, else `reported` (activity unchanged; an event fires even when only the report changed) |
| report ingested, `needs-input` (`report`) | any live state | report set; `escalation_open` true | `needs-input(permission\|question\|elicitation)` if a prompt is waiting, else `needs-input(escalation)` (activity unchanged) |
| `SessionEnd` (`SessionEnd`) | any live state | `session_end_reason` recorded (reasons that do not end the process, such as `clear`, are ignored for the mapping) | unchanged |
| `claude` exits; or finalization by `stop`, `forget`, liveness or reconcile (`exit`, `timeout`, `auto_exit`, `stop`, `forget`, `liveness`, `reconcile`) | any live state | final | per [Outcome mapping](#outcome-mapping): `exited` if a report exists, else the outcome's state (null) |

Notes:

- A report is ingested by the hook invocation that drains the outbox (normally the `PostToolUse` of the `claude-session report` call); its row applies after that hook's own event.
- An escalation stays `needs-input(escalation)` across `Stop` and idle events; after the next `UserPromptSubmit` the state is `reported` with `report.status: needs-input` until another report replaces it (exit 13 if the run ends without one).
- **Ordering assumption** (verified by a live test, including parallel tool calls): Claude Code completes all `PostToolUse` hooks of a turn before the turn's `Stop` hook runs.
- Spurious `SubagentStop` events exist; claude-session does not inject a `SubagentStop` hook.
- Notifications should match `state == needs-input` together with `needs_input_reason`.

## Outcome mapping

When `claude` exits, `exec` first ingests the outbox (the last report may not have been drained by a hook yet), then maps. The outcome of a run, in this order:

1. **Report**: a report filed in the run (explicit, including a `needs-input` escalation) gives the report's outcome (`done`, `blocked`, `needs-input`, `failed`) and the final state `exited`. The `end_reason` is still recorded from the tables below.
2. Otherwise the outcome of the first matching row of the table for the run's situation (launch phase, interactive, headless, wrapper lost).

**Cause → outcome** (`pending_cause` is written by `exec` before it sends any signal; a `stop` request sets it to `stopped`):

| Cause | Outcome without a report | `end_reason` |
|---|---|---|
| `pending_cause` `timeout` | `timeout` (16) | `timeout` |
| `pending_cause` `stopped` | `killed` (15) | `stopped` |
| `pending_cause` `forwarded_term`, `forwarded_hup`, `forwarded_int` (`exec` forwarding a signal it received, without a stop request; see [Signals](#signals)) | `killed` (15) | the cause |
| `pending_cause` `internal_error` (`exec` must exit after an internal error) | `killed` (15) | `internal_error` |
| `pending_cause` `auto_exit` (set only after a `done` report) | the report's outcome | `auto_exit` |
| `claude` killed by a signal `exec` did not send (exit 128+n or signaled, no `pending_cause`) | `killed` (15) | `signal_<name>`, e.g. `signal_kill` |

**Interactive** (no report), first match:

| Condition | Outcome | `end_reason` |
|---|---|---|
| `pending_cause` set, or killed by a signal | per the cause table | per the cause table |
| `claude` gone before `SessionStart` | `exited-unreported` | `no_session_start` |
| last process-ending `SessionEnd` reason `prompt_input_exit` | `exited-unreported` | `user_exit` |
| exit 0 otherwise | `exited-unreported` | `normal` |
| non-zero exit | `exited-unreported` | `exit_<code>` |

**Headless**, first match, on the final result object of a complete result (see [Headless output](#exec)); with a report the outcome is the report's and only `end_reason` comes from here:

| Condition | Outcome without a report | `end_reason` |
|---|---|---|
| `pending_cause` set, or killed by a signal | per the cause table | per the cause table |
| `subtype: error_max_turns` or `terminal_reason: max_turns` | `timeout` | `max_turns` |
| `is_error: true` | `exited-unreported` | `api_error` |
| clean exit with a result, kind `implicit_done: true` | `done`, implicit (state `exited`, `report.implicit: true`, `summary` null, the result text frozen as artifact `result.md` after the same ingestion checks) | `normal` |
| ... while report requests of the run are still in the outbox or `claimed/` after the final drain (a report may wait unprocessed; the run gets a warning) | `exited-unreported` | `result_rejected` |
| ... and `result.md` fails an ingestion check (size, `contains_secret`) | `exited-unreported` | `result_rejected` |
| clean exit with a result, kind `implicit_done: false` | `exited-unreported` | `no_report` (with a report: `normal`) |
| non-zero exit | `exited-unreported` | `exit_<code>` |
| no complete result | `exited-unreported` | `no_session_start` if no `SessionStart` was seen, else `no_result` |

**Wrapper lost** (finalized by `stop`, an inline liveness check or `reconcile`; wrapper and child dead), first match: `pending_cause` set → the cause table; a headless run whose `result.json` holds a complete result → the headless table (`finalized_from_result`); claude's exit status is never used here; a report exists → the report's outcome, state `exited`, `end_reason` `wrapper_lost`; otherwise `lost` (17), `end_reason` `lost`.

**Launch phase**:

| Situation | Outcome | `end_reason` | Exit |
|---|---|---|---|
| `pre-launch` veto | `vetoed` | `hook_veto` | 11 |
| `pre-launch` timeout | `vetoed` | `hook_timeout` | 11 |
| `pre-launch` escalation | `escalated` | `hook_escalate` | 13 |
| launch validation failed | `not-started` | `claude_not_found`, `claude_too_old`, `sandbox_unavailable`, `socket_path_too_long`, `invalid_permission_profile`, `unsafe_cwd`, `unsafe_path`, `unsafe_plugin_dir`, `unsafe_read_dir`, `unsafe_claude_args`, `unsafe_setting_sources`, `plugin_dir_missing`, `git_dir_changed`, `no_git_cwd`, `fork_cwd_unsupported`, `no_tty` (adoption only), `tmpdir_unsafe`, `session_dir_unsafe` | 12 |
| config invalid at adoption; the `contains_secret` values (the configured `claude_auth_env` and `secret_env` values) too large to index | `not-started` | `config_invalid` | 12 |
| pending run canceled by `stop` or `forget`, or `stop` during the launch window | `not-started` | `canceled` | 12 |
| pending run expired | `not-started` | `never_adopted` | 12 |
| launch interrupted by a signal, the wrapper died before spawn, or an internal error before spawn (e.g. the audit record could not be written, `claude` could not be started) | `not-started` | `launch_interrupted` | 12 |

**End reasons** (outcome object, `status.run`, hook payloads):

| Phase | Values |
|---|---|
| session | `normal`, `auto_exit`, `user_exit`, `forwarded_term`, `forwarded_hup`, `forwarded_int`, `signal_<name>`, `stopped`, `internal_error`, `timeout`, `max_turns`, `api_error`, `no_result`, `no_report`, `result_rejected`, `no_session_start`, `wrapper_lost`, `lost`, `exit_<code>` |
| launch | `hook_veto`, `hook_escalate`, `hook_timeout`, `canceled`, `never_adopted`, `launch_interrupted`, `config_invalid`, and the validation reasons above |

Per outcome (encoded in [schemas/cli/outcome.json](../schemas/cli/outcome.json) `$defs/outcome_rules`): `timeout` has `timeout` or `max_turns`; `lost` has `lost`; `killed` has `stopped`, `forwarded_*`, `signal_<name>` or `internal_error`; `auto_exit` and `wrapper_lost` occur only with a report outcome; `exited-unreported` has none of those.

`schema_newer` is not an end reason: it is an error code (no run is allocated). `status.run.session_end_reason` and the `post-state-change` payload separately carry the `reason` of Claude Code's last `SessionEnd` hook, verbatim.

**Outcome → exit code** (also the final state when there is no report):

| Outcome | Exit | Final state |
|---|---|---|
| `done` (including implicit done) | 0 | `exited` |
| `blocked` | 13 | `exited` |
| `needs-input` | 13 | `exited` |
| `failed` | 18 | `exited` |
| `exited-unreported` | 14 | `exited-unreported` |
| `killed` | 15 | `killed` |
| `timeout` | 16 | `timeout` |
| `lost` | 17 | `lost` |
| `not-started` | 12 | `not-started` |
| `vetoed` | 11 | `not-started` |
| `escalated` | 13 | `not-started` |

## Exit codes

| Code | Meaning in claude-session |
|---|---|
| 0 | success; for waiting commands: reported done (including implicit done) |
| 1 | internal error |
| 2 | usage error: bad flags or values (labels, kind or skill names, repo ids, durations, short id prefix, input-file names, `--input-file -`, `--timeout 0`, `wait --timeout 0`, `--interval` outside 1–59, prompt or input files over `input_max_bytes`), an invalid config (`detail` names the error), the [flag rules](#flag-rules), `report` outside a session or with invalid input or a `CSESSION_DIR` that does not match `CSESSION_ID`, `start` without `--json`, flags other than `--json`/`--dry-run`/`--outcome-file`/`--verbose`/`--quiet`/`--yes` with `--session-id`, `--cwd` on a resume (not a fork) that differs from the stored cwd, `--prompt-file -` in interactive mode with a TTY stdin (also `start`), plain `exec`/`resume` in interactive mode without a TTY, an isolated run's `--outcome-file` under a [session-writable path](#session-writable-paths) (at adoption: the run stays pending), `transcript --raw --json` or `--raw --paths`, `label add`/`remove` without pairs, `forget <id>` with bulk-only flags, `forget` with neither an id nor `--older-than`, `--verbose` with `--quiet`, `--max-turns` < 1 or > 9007199254740991, `list --limit` < 1, an unknown `export --action`, an unsafe `cron --env-file` |
| 10 | unknown or forgotten session (also when forgotten during `wait`, and on adoption); `--run` out of range |
| 11 | `pre-launch` hook veto or hook timeout (`phase: launch`); `verify-secret` mismatch |
| 12 | `claude` never started (`phase: launch`: launch failed, `no_tty` or `config_invalid` at adoption, canceled, never adopted, launch interrupted); `stop` when the run is still live after its wait; error codes `schema_newer` and `failed` (any command) |
| 13 | the run ended blocked or with a `needs-input` report; a `pre-launch` hook escalated (`phase: launch`) |
| 14 | exited-unreported |
| 15 | killed (stopped, a forwarded signal, a signal `exec` did not send, or `internal_error`) |
| 16 | timeout |
| 17 | lost |
| 18 | reported failed |
| 20 | refused; `reason` is one of `live`, `tainted`, `already_adopted`, `expired` (also `--dry-run` with `would_refuse`) |
| 21 | ambiguous session id prefix |
| 22 | `wait --timeout` expired while the run was still live |

A closed stdout (output piped to `head`, for example) ends any command with exit 1 and no error message when a write fails while the command runs; output small enough to stay in the stream buffer fails only at exit, and the command keeps its exit code.

## Output by exit code

With `--json` (and in `--outcome-file`):

| Exit | Command | Stdout |
|---|---|---|
| 0, 11, 12, 13–18 | `exec`, `resume`, `wait` | [outcome object](#outcome-object); `phase: launch` for 11, 12 and a launch-phase 13 |
| 22 | `wait` | outcome object with `timed_out: true`, `exit_code: 22`, `run_exit_code: null`, null `outcome` and `end_reason` |
| 0, 12, 20 | `exec`, `start`, `resume` with `--dry-run` | [dry-run plan](#dry-run) (the error envelope only for `schema_newer` and `failed`) |
| 12 | `start` | error envelope, `code: launch_failed`, with `end_reason`, `hint`, `csession_id`, `run` (the run's outcome file gets the outcome object) |
| 12 | any command | error envelope, `code: schema_newer` or `failed` |
| 0, 12 | `stop` | the stop object (`ended: false` for 12) |
| 0 | other commands | the command's schema |
| 11 | `verify-secret` | `{"match": false}` |
| 1, 2, 10, 20, 21 | any | error envelope |

### Outcome object

One shared definition, [schemas/cli/outcome.json](../schemas/cli/outcome.json) `$defs/outcome`, `$ref`'d by [exec.json](../schemas/cli/exec.json), [resume.json](../schemas/cli/resume.json) and [wait.json](../schemas/cli/wait.json): `{"csession_id", "run", "forked_from", "claude_session_id", "phase", "outcome", "end_reason", "exit_code", "state", "report", "session_started_at", "cost_usd", "message", "warnings"}`, all required (`forked_from`, `claude_session_id`, `session_started_at`, `message`, `cost_usd`, `report` nullable; `outcome` and `end_reason` nullable only for a `wait` timeout). `wait` adds `timed_out`, `run_exit_code` and `pending`. `exit_code` is the run's outcome code, which is also the exit code of `exec`, `resume` and `wait` (22 for a `wait` timeout). `session_started_at` is when the run's first `SessionStart` was recorded. `cost_usd` is the headless sum described in [Headless output](#exec); null for interactive runs, launch-phase ends and a headless run with no `result` object or no usable cost. `message` carries a launch failure's message (the error, its detail such as the refused path or git's first error line, then the hint when there is one) or a `pre-launch` hook's stderr (control characters stripped, capped at 2 KiB); never agent text.

The schemas encode the consistency rules once, in `outcome.json` `$defs/outcome_rules` and `$defs/exit_code_rules`, `$ref`'d by the outcome object, `status.run`, `stop` and `post-state-change`:

- final state per outcome: `done`, `blocked`, `needs-input`, `failed` → `exited`; `exited-unreported`, `killed`, `timeout`, `lost` → the state of the same name; `not-started`, `vetoed`, `escalated` → `not-started`;
- a report outcome requires `report.status` equal to it; every other outcome has `report: null`; `report.implicit: true` requires status `done`, a null summary and artifact name `result.md`;
- `end_reason` per outcome ([End reasons](#outcome-mapping)); `phase: launch` with an outcome ⇒ state `not-started`, `cost_usd: null`, a launch outcome and a launch end reason; `vetoed` ⇒ `hook_veto` or `hook_timeout`; `escalated` ⇒ `hook_escalate`;
- the outcome → exit-code table;
- per schema: `wait` with `timed_out` has a live state, and `pending` ⇒ `phase: launch`; `stop` with `ended: false` has null outcome fields and a live state, `canceled` ⇒ `not-started`/`canceled` and no signals, `already_final` ⇒ no signals; `status.run` with `pending` has `phase: launch`, state `starting`, non-null `expires_at` and null `started_at`, and `outcome`, `ended_at` and `exit_code` are null together; `post-state-change` has `final: true` exactly when `outcome`, `end_reason` and `exit_code` are non-null and the state is final.

### Error codes

`code` in the error envelope ([schemas/cli/error.json](../schemas/cli/error.json)): `usage` (2), `not_found` (10), `ambiguous` (21, with `matches`), `refused` (20, with `reason`), `launch_failed` (12, `start` only, with a launch-validation `end_reason` (never a hook, `canceled`, `never_adopted`, `launch_interrupted`, `no_tty` or `config_invalid` reason), `hint`, a non-null `csession_id` and `run`), `failed` (12, an operational failure outside a launch: no `crontab`, transcript read errors; also when the state directory or `state.db` cannot be created, opened, is unsafe or is not a claude-session database, or the migration lease cannot be obtained or a migration cannot complete (lease lost, database locked) (`detail`: path and cause); optional `detail`), `schema_newer` (12, any command; nothing allocated), `internal` (1). An invalid config is `usage` (2) with `detail`. An outcome-file write failure is a warning, never an error code ([Outcome file](#outcome-file)).

## Hooks

Configured under `hooks:` in config (design guide section 8) or per invocation with `--hook`. Each hook gets JSON on stdin.

| Hook | When | Veto/escalate | Payload |
|---|---|---|---|
| `pre-launch` | step 6 of the [launch order](#launch-order), synchronous, in order | yes: veto exits 11, escalate exits 13 (both `phase: launch`) | [schemas/hooks/pre-launch.json](../schemas/hooks/pre-launch.json) |
| `post-state-change` | on every real state or activity change of a run, including the final one (exactly one with `final: true` per run, also for launch-phase ends) | no | [schemas/hooks/post-state-change.json](../schemas/hooks/post-state-change.json) |

**Execution.** Each entry runs as `sh -c <cmd>` in its own process group, with the state root as its cwd and the [sanitized](#path-rules) `PATH` (`sh` resolved to an absolute path). claude-session clears every inherited `CSESSION_*` variable and then sets `CSESSION_HOOK`, `CSESSION_ID` (the session id) and `CSESSION_OBJECT_TYPE=session`; hooks otherwise inherit claude-session's environment minus the `secret_env` variables (design guide section 8).

**`pre-launch` outcome rules:**

- exit 0: continue; exit 13: escalate (the remaining hooks still run; the launch exits 13); any other non-zero exit: veto (stop immediately; exit 11); timeout: veto with end reason `hook_timeout`.
- stderr (control characters stripped, capped at 2 KiB) becomes the outcome `message`.
- stdout: if it is a JSON object, its `decision_id` (a string or a list of strings) is recorded in the run's `decision_ids` (shown in `status.run` and `post-state-change` payloads), the `launch` log event and, when there is one, the `launch_escalation_override` audit record; other keys are ignored. Non-JSON stdout is ignored and logged.
- The escalating entries of a run are recorded (as entry hashes) for `--override-escalation`.

**Timeouts:** default 30 s per entry (`timeout` in config; `--hook-timeout` for `--hook` entries). On timeout the hook's process group gets SIGTERM, then SIGKILL after `kill_grace`. Gate hooks need an explicit longer timeout. A hook that launches sessions must give them a shorter `--timeout` than its own; a nested `exec` forwards SIGTERM to its `claude`.

**Recursion** is prevented by selectors: a hook that itself launches sessions excludes those kinds (e.g. a gate hook uses `exclude_kinds: [gate-check]`). There is no automatic skip and no depth tracking.

**Delivery of `post-state-change`:** at least once, in order per session. The payload carries `event_id` (deduplicate on it) and `event_ts` (when the change happened; `ts` is the delivery time).

- While a run's wrapper is alive, `exec` delivers on a background worker (entries may set `async: true`), so a slow hook delays later hooks, never supervision.
- For a run with no live wrapper, the command that changes the run delivers synchronously (`start`, `stop`, `forget`, `wait`, `resume`, `reconcile`), including earlier undelivered events (keeping their cause). `status` and `list` never run hooks: they may finalize a run and leave delivery to the next delivering command or `reconcile`.
- A per-session delivery lease (`delivery:<id>`) prevents concurrent double delivery; leases expire after 60 s and are renewed every 20 s by the holder, also during a long hook ([`reconcile`](#reconcile)). Each delivered entry is logged `hook_delivered`.
- Late delivery runs the configured entries of the *current* config plus the per-invocation `--hook` entries stored with the run.

**Selectors** (optional on every config entry, combined with AND): `kinds` (run kind is one of), `exclude_kinds` (run kind is none of), `labels` (the session had all of these `key=value` pairs when the event happened; the labels are stored in the event row, so late delivery matches the same way), and for `post-state-change` only, `states` (the new state is one of).

- Both payloads include `pending`: in `pre-launch`, whether the run being launched was created by `start` (it is being adopted); in `post-state-change`, whether the run is still pending.
- `pre-launch` payloads include `run_kind`, `previous_run_kind` (null for a new session), `allow_tainted`, `override_escalation`, `actor` (the effective actor, never null), `decision_ids` (from `--decision-id`), `permission_profile` (name or `file:<sha256 prefix>`), `setting_sources`, `mcp`, `env_inherit`, `env_names` (names only), `plugin_dirs`, `input_files` (basenames), `auto_exit`, `implicit_done` and the intended `sandbox` mode.
- `post-state-change` payloads include `event_id`, `event_ts`, `phase`, `message`, `forked_from`, `session_started_at`, `session_end_reason` and `sandbox_active` (whether the sandbox actually ran). It fires on every turn (`working` ↔ `idle`); notification wiring should filter with `states`. Its `report.summary` and `report.reason` are untrusted text; notification templates exclude them when `reads_third_party` is true (architecture section 15).
- `cause` (in `post-state-change` and status events) is one of `SessionStart`, `UserPromptSubmit`, `PermissionRequest`, `Notification`, `PostToolUse`, `Stop`, `SessionEnd`, `report`, `launch`, `adopt`, `exit`, `timeout`, `auto_exit`, `stop`, `forget`, `reconcile`, `liveness`.

## Config

`~/.config/claude-session/config.yaml` plus `config.d/*.yaml` ([schemas/config.json](../schemas/config.json); every key with its default in [config.example.yaml](../config.example.yaml)). Maps are deep-merged; lists are replaced, except `hooks.*` lists, which append (design guide section 5). Drop-ins are applied after `config.yaml` in lexical (byte) order of file name; hidden names are skipped, and any other `config.d` entry ending in `.yaml` that is not (after symlinks) a regular file is an invalid config, as is a default `config.yaml` symlink that does not resolve. A missing `config.d` means no drop-ins; a `config.d` that exists but is not a directory (after symlinks), or is a symlink that does not resolve, is an invalid config. `--config <path>` replaces the default file and its drop-ins: only that file is read; `config.d/` is read only next to the default `config.yaml`.

**Environment overrides** use the prefix `CSESSION_` and apply only to these top-level scalar keys: `CSESSION_CLAUDE_BIN`, `CSESSION_DEFAULT_KIND`, `CSESSION_MIN_CLAUDE_VERSION`, `CSESSION_SUMMARY_MAX_BYTES`, `CSESSION_ARTIFACT_MAX_BYTES`, `CSESSION_INPUT_MAX_BYTES`, `CSESSION_NOTES_CONTEXT_TIMEOUT`, `CSESSION_NOTES_CONTEXT_MAX_BYTES`, `CSESSION_RETENTION_DAYS`, `CSESSION_LOST_GRACE`, `CSESSION_AUTO_EXIT_DELAY`, `CSESSION_KILL_GRACE`, `CSESSION_POLL_INTERVAL`, `CSESSION_SESSION_START_TIMEOUT`. Durations accept bare numbers of seconds. Nothing else (`kinds`, `permission_profiles`, `repos`, `hooks`, `secret_env`, `claude_auth_env`, `claude_args`, `notes_context_cmd`, `notes_context_sources`, `me`) can be set from the environment. The reserved names `CSESSION_ID`, `CSESSION_DIR`, `CSESSION_KIND`, `CSESSION_HOOK` and `CSESSION_OBJECT_TYPE` are never config keys and are ignored by the loader.

Config validation: an invalid config (schema or semantic error) exits 2 with `code: usage` and `detail`; at adoption it ends the run `not-started` with `config_invalid`. Durations that bound something (kind `timeout`, `interactive_timeout`, hook `timeout`, `notes_context_timeout`, `lost_grace`, `poll_interval`, `session_start_timeout`) must be greater than zero (`exclusiveMinimum: 0`). `default_kind` must match the [kind-name grammar](#identifiers) and name a built-in or configured kind, and so must each `repos.<repo-id>.kinds.<name>` key; `claude_bin`, and `model` when set, must be non-empty and contain no control characters (C0, DEL or C1); `model` (config, per-repo override or `--model`) must not start with `-`; `null` is accepted only where the schema allows it. Integers are at most 9007199254740991 (2^53 − 1: `max_turns`, `summary_max_bytes`, `artifact_max_bytes`, `input_max_bytes`, `notes_context_max_bytes`); `retention_days` is at most 36500. Values are plain YAML mappings, sequences, strings, numbers, booleans and null; non-JSON YAML types such as timestamps (including unquoted dates like `2001-01-01`), binary, sets, pairs and omap are invalid (quote a date to use it as a string), and YAML merge keys (`<<`) are not supported. Limits per config file: a regular file (symlinks are followed) of at most 1 MiB, at most 100,000 YAML nodes (aliases expanded) and 64 levels of nesting; duplicate mapping keys and non-string keys (quote keys such as `yes`) are invalid, and YAML 1.1 base-60 numbers are not numbers: plain `1:30` is read as a string, and a tagged one (`!!int 1:30`, `!!float 1:30.5`) is invalid. Environment keys: `secret_env` and `claude_auth_env` entries are variable names (`^[A-Za-z_][A-Za-z0-9_]*$`; exact names, no globs); in a kind's `env` (config and per-repo override), `env.allow` entries are names or `PREFIX*` globs (`^[A-Za-z_][A-Za-z0-9_]*\*?$`), `env.set` names match `^[A-Za-z_][A-Za-z0-9_]*$` and must not start with `CSESSION_`, `env.set` values contain no NUL and no `${` other than the exact references `${CSESSION_DIR}`, `${CWD}`, `${HOME}` and `${GIT_DIR}` (a `$` or `}` on its own is literal; `${USER}`, `${${HOME}}` and an unclosed `${HOME` are invalid), and `env.path_prepend` entries are absolute paths without `:` or NUL (used as written, not expanded). `detail` names the key path (a `repos` key that is not a valid repo id is shown as `<invalid repo id>`, an `env.set` key that is not a valid variable name as `<invalid variable name>`) and where it was set, never a value.

Precedence: top-level scalars: built-in → config → environment → flags. Kind settings: built-in kind → config kind → per-repo override → previous run's stored explicit flags → flags given now.

| Key | Default | Meaning |
|---|---|---|
| `claude_bin` | `claude` | `claude` executable, resolved to an absolute path on the sanitized session `PATH` (outside every session-writable path) at launch and again at adoption |
| `min_claude_version` | `2.1.312` | minimum `claude --version` a launch accepts; older fails with `claude_too_old` (built-in: the version the spike verified) |
| `default_kind` | `implement` | kind used when `--kind` is omitted |
| `kinds.<name>` | built-in kinds | kind profiles (fields below); entries merge over built-ins; a new name starts from the **built-in** `review` kind's fields (not a configured override of it) |
| `permission_profiles.<name>` | built-in `default`, `readonly`, `review`, `respond` | profiles: `{permissions: {...}, sandbox: {...}}` Claude Code settings fragments |
| `claude_auth_env` | `[]` | extra variable names (exact names, no globs) always passed for Claude's own authentication, added to the built-in list: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`; `AWS_*` only when `CLAUDE_CODE_USE_BEDROCK` is true; `GOOGLE_*`, `CLOUD_ML_REGION` and `ANTHROPIC_VERTEX_PROJECT_ID` only when `CLAUDE_CODE_USE_VERTEX` is true. A provider flag is true when its value as passed to the session (an `env.set` value wins), trimmed and lowercased, is `1`, `true`, `yes` or `on`; a flag stripped by `secret_env` and not set by `env.set` selects nothing |
| `secret_env` | `[]` | variable names (exact names, no globs) stripped from every kind unless the kind's config `env.allow` names them exactly (per-call `--env-allow` cannot, and while `--env-allowlist-only` is in effect nothing unlocks them) |
| `notes_context_cmd` | `["memoctl", "context"]` | argv run at session start for kinds with `notes_context`, with cwd = the state root and the sanitized `PATH`; `--repo=<id>` (omitted when the session has no repo) and one `--label=<key>=<value>` token per session label are appended (labels are restricted identifiers, never free text). Exit 0 and 10 are success; other exits are logged at `warn` |
| `notes_context_timeout` | `5s` | |
| `notes_context_max_bytes` | `16384` | |
| `notes_context_sources` | `[startup]` | `SessionStart` sources that get notes: `startup`, `resume`, `fork`, `clear`, `compact`, `other` |
| `summary_max_bytes` | `65536` | cap for summary and reason (UTF-8 bytes each) |
| `artifact_max_bytes` | `10485760` | cap for a reported artifact |
| `input_max_bytes` | `10485760` | cap for each `--prompt-file` and `--input-file` (larger exits 2) |
| `retention_days` | `30` | after `forget`, before deletion |
| `lost_grace` | `10m` | a pending run not adopted within it ends `not-started` (`never_adopted`) |
| `auto_exit_delay` | `2s` | |
| `kill_grace` | `10s` | SIGTERM → SIGKILL; default `stop --grace`; hook timeout kill |
| `poll_interval` | `0.5s` | `exec` and `wait` polling |
| `session_start_timeout` | `120s` | headless runs: warn when no `SessionStart` arrives within it |
| `hooks.pre-launch` | `[]` | `{cmd, timeout (default 30s), kinds, exclude_kinds, labels}` entries |
| `hooks.post-state-change` | `[]` | `{cmd, timeout (default 30s), async, kinds, exclude_kinds, labels, states}` entries |
| `repos.<repo-id>.kinds.<name>` | none | `<name>` must be a built-in or configured kind (else invalid config); per-repo overrides of `permission_profile`, `sandbox`, `env`, `read_dirs`, `write_dirs`, `timeout`, `max_turns`, `model` (`reads_third_party` cannot be overridden per repo) |
| `me.name` | OS username | default actor `human:<me.name>` (when stdin is a TTY) |

Kind profile fields (architecture section 8.1): `permission_profile`, `sandbox` (`required`/`preferred`/`off`; quote `"off"` in YAML), `reads_third_party`, `implicit_done`, `setting_sources` (list of `user`, `project`, `local`), `mcp` (`inherit`/`none`), `cwd` (`session-dir`/`caller`), `env` (`inherit`, `allow`, `set`, `path_prepend`; entry rules under Config validation above; `env.set` values are literal configuration, not treated as secrets), `read_dirs`, `write_dirs`, `timeout`, `interactive_timeout`, `max_turns`, `auto_exit`, `backstop`, `notes_context`, `model`, `claude_args` (config files and drop-ins only; isolated runs accept only `--verbose` and `--debug`).

Built-in kinds: `implement`, `draft-ticket`, `audit`, `improve` (trusted, unsandboxed, `cwd: caller`); `rank` (`readonly` profile, sandbox `preferred`, `env.inherit: false`, `cwd: session-dir`, `implicit_done: false`); `review` (third-party-reading, sandbox `required`, `cwd: session-dir`); `gate-check` (as `review`, `implicit_done: false`); `respond` (third-party-reading, sandbox `required`, `cwd: caller`, requires a git cwd, else `no_git_cwd`; its agent edits its cwd but never commits, see [Artifact contract](#artifact-contract-for-callers)). The built-in `review`, `readonly` and `respond` profiles set no `defaultMode` (isolation forces `dontAsk`). `implicit_done` is `true` for every other built-in kind. Every field of every built-in kind is in [config.example.yaml](../config.example.yaml).

## Isolation

### Session-writable paths

The **session-writable paths** of a run are: its cwd when the run may write it (a **writable cwd**: `work/` for kinds with `cwd: session-dir`; the caller's cwd for isolated kinds with `cwd: caller` such as `respond`), `$CSESSION_DIR/outbox/`, `$CSESSION_DIR/artifacts/`, `$CSESSION_DIR/work/`, `write_dirs`, the profile's sandbox `allowWrite` entries and `Edit`/`Write` allow targets, the private temporary directory, and every `--add-dir` directory that is not write-denied (so all of `$CSESSION_DIR`). A profile's `Edit`/`Write` allow target is the literal part of its path pattern before the first glob or escape character, cut back to a whole path segment: `//abs` is absolute, `~/x` is under `$HOME` (so is `~//x`: extra slashes after `~/` never leave `$HOME`), `x` and `./x` are under the cwd, a bare tool name reaches `/` (`Tool(*)` and `Tool()` count as the bare tool name, as in Claude Code), and `/x` (relative to the settings file, which is under the state root) is refused for every run (`invalid_permission_profile`). Every rule below that refers to them (outcome file, plugin directories, profile files, `PATH`, hooks) compares realpaths: of the path, or of its nearest existing parent when it does not exist yet. Every path rule also compares file identities (device and inode of a directory and of each existing ancestor of a path), so a bind mount or another name realpath cannot see through does not hide containment, and on macOS compares case-insensitively; the exceptions (the run's own directories) compare exactly.

### Permission profiles

- Profiles and `env.set`, `read_dirs` and `write_dirs` may use `${CSESSION_DIR}`, `${CWD}`, `${HOME}` and `${GIT_DIR}` (the cwd's git common directory, empty if none). A profile rule whose variable expands empty fails the launch (`invalid_permission_profile`); this applies to profile content only (generated `${GIT_DIR}` rules are emitted only when a git directory was resolved). A variable used in a profile whose value is not a normalized absolute path (no `.` or `..` segment, no `//`, no trailing `/`) or contains `*`, `?`, `[`, `]`, `{`, `}`, `\` or a control character fails the launch (`invalid_permission_profile`). `read_dirs` and `write_dirs` entries also expand a leading `~` and are symlink-resolved. Paths in Claude Code rules are absolute (`//abs/path/**`). For the path rules, a profile's sandbox `filesystem` entries are mapped as Claude Code reads them: `~` and `~/x` are under `$HOME`, `//x` is `/x`, an absolute path is itself, and any other entry (relative, `~user`) is relative to the generated settings file, which is under the state root, so it is refused for every run (`invalid_permission_profile`).
- A profile file passed with `--permission-profile <file>` has the same shape. It is read once, at the launch that names it, and snapshotted to the private directory; later runs use the snapshot. `status` shows it as `file:<sha256 prefix>`. A resume re-reads the snapshot and checks it against the recorded `file:<prefix>` hash; a mismatch fails the launch (`invalid_permission_profile`). Numbers in any profile (named or file) must be finite (`.inf`, `.nan` and `1e400` are refused). For isolated runs, a profile file under a session-writable path is refused (`invalid_permission_profile`).
- Profiles carry only kind-specific rules. No built-in profile makes the git directory writable.
- Isolated runs' profiles may set only these `sandbox` keys (an allowlist): `sandbox.filesystem.allowRead`, `denyRead`, `allowWrite`, `denyWrite`, `sandbox.network.allowedDomains`, `deniedDomains`, and `sandbox.autoAllowBashIfSandboxed: false`. Any other `sandbox` key fails the launch (`invalid_permission_profile`): the generated keys (`enabled`, `failIfUnavailable`, `allowUnsandboxedCommands`, `network.strictAllowlist`, ...) and every escape, for example `excludedCommands`, `enableWeakerNestedSandbox`, `enableWeakerNetworkIsolation`, `allowNestedUserNamespaces`, `allowAppleEvents`, `ignoreViolations`, `filesystem.disabled`, `network.allowUnixSockets`, `network.allowAllUnixSockets`, `network.allowLocalBinding`, `network.allowMachLookup`, `network.httpProxyPort`, `network.socksProxyPort`, and keys Claude Code adds later. They may not set `permissions.additionalDirectories` or any refused path either (`invalid_permission_profile`). For trusted runs only the refused paths apply.
- Profile values must be valid for Claude Code, which otherwise discards the whole settings file, injected hooks included: `permissions.allow`, `deny`, `ask` and `additionalDirectories` are lists of strings, and `permissions.defaultMode` is one of `acceptEdits`, `auto`, `bypassPermissions`, `default`, `dontAsk`, `plan`. Anything else fails the launch (`invalid_permission_profile`).
- **Permission mode.** Every run's generated settings set `permissions.defaultMode` explicitly. Isolated runs: always `dontAsk`; a profile's mode is ignored. Trusted runs: the profile's mode (any mode, including `bypassPermissions`); without one, `default` when `setting_sources` excludes `user` (otherwise the user's settings decide). Reason (live test): with `--setting-sources ""` and no mode, Claude Code starts in a mode that silently allowed unknown commands.

### Path rules

All failures here end the launch `not-started` (exit 12) with the named end reason.

- **Unsafe write targets** (isolated runs; `unsafe_cwd` for the cwd, `invalid_permission_profile` for `write_dirs` and profile write additions): a writable cwd, a `write_dirs` entry or a path a profile makes writable is refused if it is `/`, `$HOME` or an ancestor of `$HOME`, a directory on `PATH` (the caller's absolute `PATH` entries and `env.path_prepend`) or inside one, `$XDG_CONFIG_HOME`, `$XDG_DATA_HOME` or `$XDG_STATE_HOME` (when set to absolute paths), inside one or containing one, the state root or inside it, `~/.claude` or inside it, or any directory that is or is inside a `$HOME/.<name>` entry (or overlaps the realpath of one that is a symlink: writing there changes the configuration it points to; also through another name of `$HOME`, such as a bind mount, for an entry that does not exist yet). A `$HOME/.<name>` entry that resolves to `$HOME`, `/` or an ancestor of `$HOME` (an alias of the home directory, not a configuration directory) is ignored by this rule; `$HOME` and its ancestors have their own. When the account's home directory (`pw_dir` of the user) differs from `$HOME`, its `.<name>` entries and credential paths are protected the same way. Exception: the run's own `$CSESSION_DIR/work/`, `outbox/` and `artifacts/` (and their subpaths), each only while it is a real directory (not a symlink) at its own path (its last component is not a symlink and its realpath is its parent's realpath joined with its name; a symlinked ancestor such as macOS `/tmp -> /private/tmp` keeps the exception, which is then compared by realpath); the private temporary directory's exception from the `/tmp/csession-<uid>` rules has the same condition. Agent worktrees live where the caller puts them (normally not under a hidden home directory); a third-party-reading run whose cwd is under a `$HOME/.<name>` directory is refused. Each built-in kind's default cwd passes these rules (tested).
- **`read_dirs`, a profile's sandbox `allowRead` entries and plugin directories** (isolated runs; `invalid_permission_profile`, `unsafe_plugin_dir`): refused if `/`, `$HOME`, an ancestor of `$HOME`, or if they contain or are inside a credential path: `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.docker`, `~/.config`, `~/.claude`, `~/.netrc`, `~/.git-credentials`, `~/.local/state`, `~/.local/share/keyrings`, `~/.cargo/credentials`, `~/.cargo/credentials.toml`, `~/.gem/credentials`, `~/.gradle/gradle.properties`, `~/.m2/settings.xml`, `~/.m2/settings-security.xml`, and `$XDG_CONFIG_HOME`, `$XDG_STATE_HOME` and `$XDG_DATA_HOME/keyrings` when those variables are set to absolute paths (compared with the credential path's realpath too, so one reached through a symlink counts). Paths inside the run's own `$CSESSION_DIR`, readable anyway, are exempt from the credential paths. Toolchain directories (`~/.nvm`, `~/.local/share/uv`) remain allowed; a toolchain directory that holds a credential file must be narrowed (for example `~/.cargo/bin` and `~/.cargo/registry` instead of `~/.cargo`). A plugin directory under a session-writable path, or containing one, is also refused; one that does not exist fails the launch (`plugin_dir_missing`).
- **Entries inside a session-writable path** (isolated runs; `invalid_permission_profile`): a `read_dirs` or `write_dirs` entry, or a profile's sandbox `allowRead` or `allowWrite` entry (or `additionalDirectories` entry), that lies inside a session-writable path (one of them is a proper ancestor of the entry) is refused: the agent could rename a parent directory and swap in a symlink. These entries reach the generated settings as written, so the check walks the entry as the `PATH` check does: no literal prefix of it, path visited while following its symlinks, or its realpath may lie inside a session-writable path, and no symlink followed may sit in a session-writable directory (the agent could re-point it; for example `<cwd>/lnk -> /opt/data` is refused). An entry equal to a session-writable path is not affected, nor is one equal to the run's own `$CSESSION_DIR`, `outbox/`, `artifacts/`, `work/` or private temporary directory (by realpath); an entry strictly below one of these is refused (list the directory itself: the agent could replace the subdirectory with a symlink). An entry with a `..` component is refused (list the directory itself): the kernel applies `..` after following the symlink before it, so `<cwd>/lnk/..` names the parent of the symlink's target, not the cwd. An entry outside every session-writable path is not refused for not existing.
- **Generated read entries** (isolated runs; `unsafe_read_dir`): every entry `exec` adds to the sandbox `allowRead` list on its own (claude-session's install directories, the Python and `sys.path` set, the user site, `env.path_prepend` directories, the directories of the resolved tools, the git directory) passes the same refusal checks as `read_dirs`, after symlink resolution: not `/`, `$HOME` or an ancestor of `$HOME`; not containing or inside a credential path listed above; not the state root or inside it; not `/tmp/csession-<uid>` or inside it; not the caller's cwd, or a directory containing it, unless the caller's cwd is the run's cwd or the entry is one of the system read directories (`/usr`, `/bin`, `/sbin`, `/lib`, `/lib32`, `/lib64`, `/etc`) or inside one (readable anyway; see [Residual risks](#residual-risks)) (so `python -m claude_session` run from inside a directory, which puts that directory on `sys.path`, fails `unsafe_read_dir`: run the installed `claude-session` executable). Empty (`''`) and relative `sys.path` entries are dropped first; absolute ones are kept, also inside the caller's cwd (one inside a session-writable path fails the interpreter check below). A refused entry fails the launch.
- **Profiles, `read_dirs`, `write_dirs`**: a path under the state root or `/tmp/csession-<uid>/` is refused (`invalid_permission_profile`), except paths inside the session's own `$CSESSION_DIR` (generated denies still win). The private temporary directory is created only at the end of step 5, after these checks, so an entry at or inside it is refused too: nothing a profile needs lives there. A read entry (`read_dirs`, a profile's sandbox `allowRead`, a generated read entry) inside the private temporary directory is refused as well (`invalid_permission_profile`; generated entries `unsafe_read_dir`): listed in `allowRead`, the directory would be mounted read-only and every Bash call would fail (live E1). `write_dirs` under `$CSESSION_DIR` must be inside `outbox/`, `artifacts/` or `work/` and are created before launch if missing (isolated runs then refuse any entry strictly below one of them, see Entries inside a session-writable path above); other `write_dirs` must exist (a missing one produces a warning and is skipped).
- **State root containment** (isolated runs): every path that goes into the sandbox `allowRead` or `allowWrite` lists (the cwd, `read_dirs`, `write_dirs`, the profile's `allowRead` and `allowWrite`, plugin directories, the generated read entries, the git directory) is refused if it is, or contains, the state root, the private directory (`<state>/private`) or `/tmp/csession-<uid>` (the parent of every session's private temporary directory, which holds the other sessions' network proxy sockets). End reasons: `unsafe_cwd` for the cwd, `unsafe_plugin_dir` for plugin directories, `invalid_permission_profile` for `read_dirs`, `write_dirs` and profile entries, `unsafe_read_dir` for the generated read entries and the git directory. The session's own `$CSESSION_DIR` and private temporary directory contain none of them and stay allowed.
- **`PATH`** (isolated runs; `unsafe_path`): relative and empty `PATH` entries are dropped (if no entry remains, `PATH` is `/usr/bin:/bin`); the launch fails if any resolved `PATH` entry, or the `claude-session` executable directory prepended after sanitizing, is inside a session-writable path. Resolution is checked hop by hop: every literal normalized prefix of the path, every symlink followed while resolving it and the directory holding that symlink (which the agent could re-point) must be outside the session-writable paths too; the path is walked as the kernel walks it (a `..` applies after the symlink before it is followed). The kept `PATH` entries are their realpaths. `claude_bin`, `bwrap`, `socat` and `git` are resolved to absolute paths outside session-writable paths (the found file, checked hop by hop, and its realpath, which is the resolved tool path) at launch and checked again at adoption; a resolved tool is executed from its realpath with `argv[0]` set to the bare tool name, so launchers that dispatch on `argv[0]` (volta, mise or snap symlink shims) keep working; the hook interpreter path is checked hop by hop as well; a `claude_bin` with a `/` must be absolute. Hooks and the notes command run with the same sanitized `PATH`.
- **`$HOME`** (`unsafe_path`): must be set to an absolute path other than `/` (every run: the refused-path rules need it); for isolated runs it must not be the state root, inside it or inside `/tmp/csession-<uid>`.
- **Paths in generated rules**: the paths that appear in permission rules must be absolute, normalized, valid UTF-8 and free of control characters and of `*`, `?`, `[`, `]`, `{`, `}`, `(`, `)` and `\`. End reasons: the cwd `unsafe_cwd` (isolated runs; checked before the profile's variables are expanded, so a bad cwd fails `unsafe_cwd` whether or not the profile uses `${CWD}`), plugin directories `unsafe_plugin_dir`, `$HOME` `unsafe_path`, the git directory `unsafe_read_dir` (isolated runs), and the state root and the absolute `claude-session` executable path `unsafe_path` (every run). Paths only in sandbox lists must be free of `*`, `?`, `[`, `]`, `{`, `}` and `\` (end reasons as in the rules above). The `claude-session` path must also need no shell quoting (no whitespace, shell metacharacters such as `` '"$`;&|<>!#~ `` or other characters outside `A-Z a-z 0-9 _ @ % + = : , . / -`): a quoted invocation never matches its `report` allow rule.
- **Hook interpreter** (isolated runs, at launch and adoption; `unsafe_path`): the injected hooks run Python outside the sandbox, so nothing it loads code from may be changeable by the agent. Refused when any of these is inside a session-writable path: the hook interpreter path and its realpath, `sys.prefix`, `sys.exec_prefix`, `sys.base_prefix`, `sys.base_exec_prefix`, the realpath of each symlinked `*.pth` file, and the code-bearing directories: the standard library directories, the `claude_session` package directory, every absolute `sys.path` entry of `exec`'s own interpreter, the site-packages directories (and the user site when enabled) and every path line of their `*.pth` files. Also refused when a session-writable path is inside (or equal to) one of the code-bearing directories; a prefix may contain one (for example a cwd under `/usr/src`). A `*.pth` file that cannot be opened is skipped, as `site` skips it. Also refused when a regular file under the package directory, or (in a virtual environment) under its own site-packages, has more than one hard link: reinstall with `uv tool install --link-mode=copy` (or `UV_LINK_MODE=copy`).
- **cwd reuse**: the resolved cwd is stored per run; a resume always uses the previous run's resolved cwd (`--cwd` on a resume must resolve to the same path, else exit 2). A fork uses the source's resolved cwd, or the `--cwd` given, which passes these path rules like any new cwd. Forking without `--cwd` from a source whose cwd is a session `work/` directory is refused (`fork_cwd_unsupported`). The `respond` kind requires a git cwd (`no_git_cwd`).
- **Git directory**: resolved at the session's first launch and stored, including "no git dir" as a resolved value; re-checked at every resume and adoption. A different git directory, or one that appeared where none was, fails the launch (`git_dir_changed`). "No git dir" is recorded only when git reports `fatal: not a git repository` and no `.git` entry exists at the cwd or an ancestor on the same file system; any other git failure (for example a broken or unreadable `.git`, or a repository git refuses as of dubious ownership) fails the launch (`unsafe_cwd`): the cwd's git directories cannot be determined, so the run cannot protect them. The message carries the first line of git's error output, control characters replaced, at most 200 characters.
- **`claude_args`**: isolated runs accept only `--verbose` and `--debug`, as exact tokens (no `--debug=<filter>` or separate value); anything else fails the launch (`unsafe_claude_args`). Trusted runs: anything.
- **Claude Code version**: `claude --version` below `min_claude_version` fails the launch (`claude_too_old`).

### Generated settings

**Every run** (all kinds):

- `--add-dir $CSESSION_DIR`; `--system-prompt-snapshot off`; `permissions.defaultMode` as described in [Permission profiles](#permission-profiles);
- allow `Bash(claude-session report:*)` and `Bash(<absolute claude-session path> report:*)` (see [`report`](#report) for the forms that match);
- allow `Read` of `//$CSESSION_DIR/**`; allow both `Edit` and `Write` of `//$CSESSION_DIR/artifacts/**`, `//$CSESSION_DIR/outbox/**` and, when it is the cwd, `//$CSESSION_DIR/work/**` (live test: a `Write(…)` allow rule alone does not permit the Write tool; `Edit(…)` does, so both are generated);
- `Edit` and `Write` denies for the protected session files and the private directory;
- the injected hooks, each with an explicit `timeout` (`PostToolUse` and `Stop` 60 s, the others 30 s), see [Internal commands](#internal-commands).

**Isolated runs** also get these rules, which always override profile content:

- `blockReadsOutsideWorkingDirectories`; permission mode `dontAsk`; `permissions.disableAutoMode: "disable"` and `permissions.disableBypassPermissionsMode: "disable"` (no switch to a mode that skips the rules); `sandbox.autoAllowBashIfSandboxed: false` (Claude Code's default `true` runs sandboxed Bash without consulting the allow rules, so a narrow `Bash(...)` allowlist does not limit it; live D3);
- plugin directories: `--add-dir` each one, read-only (sandbox `allowRead` and `denyWrite`, `Read` allow and `Edit`/`Write` deny rules);
- **deny-root reads**: sandbox `denyRead: ["/"]` and a generated `allowRead` list: the cwd, `$CSESSION_DIR`, `/usr`, `/bin`, `/sbin`, `/lib`, `/lib32`, `/lib64`, `/etc`, the directories of the resolved `bwrap`, `socat`, `git` and `claude`, claude-session's install and Python (for sandboxed `claude-session report`: the tool's virtual environment directory, the resolved `sys.base_prefix`, `sys.prefix`, `sys.exec_prefix`, `sys.base_exec_prefix`, the package directory, resolved `sys.path` entries outside the standard locations and the user site when used; a `uv tool` install needs the venv and the uv-managed base interpreter), `read_dirs`, plugin directories, `env.path_prepend` directories and, when the cwd has a git directory, the resolved git directory; every generated entry passes the [generated read entry](#path-rules) checks (`/opt` is not included: add it, or a narrower directory, to `read_dirs`). Everything else (the state root, other sessions, `$HOME`, other temporary directories) is unreadable from sandboxed commands (live E1). The private temporary directory is in `allowWrite` only: listed in `allowRead` it is mounted read-only and every Bash call fails; left out entirely, the network proxy sockets in it break (live E1);
- toolchains outside this list (under `$HOME`, such as `~/.nvm`, or elsewhere, such as a toolchain on another disk or `/home/linuxbrew`) must be named in `read_dirs`;
- **Home-directory Read denies** (conservative until F1 is verified): `Read` deny rules (which Claude Code applies to Grep and Glob on a best-effort basis per its documentation; Bash reads are enforced by the sandbox's deny-root reads) for `//$HOME/.ssh/**`, `//$HOME/.aws/**`, `//$HOME/.gnupg/**`, `//$HOME/.kube/**`, `//$HOME/.docker/**`, `//$HOME/.config/**`, `//$HOME/.claude/**`, `//$HOME/.netrc`, `//$HOME/.git-credentials`, `//$HOME/.local/state/**`, the state root and the private directory. Paths inside the run's own cwd and `$CSESSION_DIR` are not denied: a deny for a prefix that contains either is not generated (so the state root, which always contains `$CSESSION_DIR`, and with the default state root also `~/.local/state`, get none; the private-directory deny remains);
- write access only to the session-writable paths (sandbox `allowWrite`: the cwd, `outbox/`, `artifacts/`, `write_dirs`, the profile's `allowWrite` and the private temporary directory; `work/` only when it is the cwd). The `work/` narrowing applies to `allowWrite` and the `Edit`/`Write` allow rules only: Claude Code lets sandboxed Bash write `--add-dir` directories by default, so Bash may write anywhere in `$CSESSION_DIR` (an `--add-dir`) except the denied paths;
- write denies (sandbox and `Edit`/`Write`) for the protected session files and the private directory;
- when the cwd has no git directory: write denies (sandbox `denyWrite` and `Edit`/`Write`) for `${CWD}/.git` and `//${CWD}/.git/**` (no planted git directory; a `.git` that appears fails the next launch with `git_dir_changed`). With a git directory, an isolated run that is not third-party-reading (for example `implement --sandbox preferred`) can still commit: the git directory is read-only only on third-party-reading runs (below);
- the sandbox block: `enabled: true` (`false` when the mode is `off`, so user or managed settings cannot enable a sandbox that lacks the private temporary directory; `sandbox_active` stays false), `failIfUnavailable` (only when the mode is not `off`; `true` for `required`), `allowUnsandboxedCommands: false`, `autoAllowBashIfSandboxed: false`, explicit `false` for `allowNestedUserNamespaces`, `allowAppleEvents`, `enableWeakerNestedSandbox`, `enableWeakerNetworkIsolation` and `filesystem.disabled` (`--settings` outranks user settings, so a user setting cannot turn them on), the network allowlist with `network.strictAllowlist: true` (a domain outside it is refused, never prompted for, also in an interactive run), and `filesystem`; all of them are emitted also when the mode is `off`. `strictAllowlist`, `disableAutoMode` and `disableBypassPermissionsMode` still need live verification (#41).
- Claude config under the session directory and every `--add-dir` directory: `Edit`/`Write` denies for `.claude/**`, `.mcp.json`, `CLAUDE.md` and `CLAUDE.local.md`, top-level and in the `**/` forms, under `$CSESSION_DIR` and each `--add-dir` directory (defense in depth; see the planted-skills note below).

**Third-party-reading runs** also get:

- **no setting sources**: a non-empty `setting_sources` (`user`, `project` or `local`) fails the launch (`unsafe_setting_sources`): project and local settings come from the third-party checkout and their hooks run outside the sandbox, and the list keys of any source (`excludedCommands`, `allowRead`, `additionalDirectories`) merge with the generated ones. Other isolated runs with setting sources get a warning naming that merge;
- **`.git` write deny, always**: sandbox `denyWrite` and `Edit`/`Write` denies for the concrete `${CWD}/.git` (a file or a directory) and `//${CWD}/.git/**`, whether or not a git directory was resolved;
- **read-only git directory**, when one was resolved: sandbox `allowRead` of `${GIT_DIR}` (the common directory) and, for a linked worktree, of the worktree's gitdir; sandbox `denyWrite` of the whole `${GIT_DIR}` as a concrete path (the sandbox otherwise opens parts of a linked worktree's main `.git` for writing; live D1: this explicit deny wins over the built-in allow, and every write into the git directory fails) and of the worktree's gitdir; `Edit`/`Write` denies for both; `GIT_OPTIONAL_LOCKS=0` in `claude`'s environment (set on every third-party-reading run) so `git status` does not write the index. The agent can therefore not commit, stage or change refs; see [Artifact contract](#artifact-contract-for-callers) for how `respond` work is committed;
- Claude project config: `Edit`/`Write` denies `//${CWD}/.claude/**`, `//${CWD}/.mcp.json`, `//${CWD}/CLAUDE.md`, `//${CWD}/CLAUDE.local.md` and the nested forms `//${CWD}/**/.claude/**`, `//${CWD}/**/.mcp.json`, `//${CWD}/**/CLAUDE.md`, `//${CWD}/**/CLAUDE.local.md`; sandbox `denyWrite` for the concrete top-level paths only (the `**/` rules are `Edit`/`Write`-only; single-`*` globs in `denyWrite` are silently ignored, so sandbox denies are always concrete paths). Claude Code's sandbox itself also blocks writes to `.claude/`, `.mcp.json`, `CLAUDE.md` and common dotfiles in the cwd and `--add-dir` directories, including paths that do not exist yet (live test); the explicit denies remain as defense in depth and for the unsandboxed `preferred` fallback;
- in `claude`'s environment, `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_<n>`/`GIT_CONFIG_VALUE_<n>` setting `core.fsmonitor=`, `core.hooksPath=/dev/null`, `core.sshCommand=`, `credential.helper=`, `diff.external=`, `core.pager=cat`. The entries follow any existing ones (renumbered, so they win); an existing set git would reject (a `GIT_CONFIG_COUNT` that is not 0-1000 in plain digits, or a missing key or value below it) is dropped and only the hardening is passed. Before the entries are added, `GIT_CONFIG_PARAMETERS` (applied after them), `GIT_SSH`, `GIT_SSH_COMMAND`, `GIT_EXTERNAL_DIFF`, `GIT_PAGER`, `GIT_ASKPASS`, `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY` and `GIT_ALTERNATE_OBJECT_DIRECTORIES` are removed, also when `env.set` sets them. This hardens the agent's own Bash `git` calls (honored inside the sandbox; live test) and is partial: a command can override it. Claude Code's own git calls already disable fsmonitor and hooks (live test);
- `GH_CONFIG_DIR` and `GLAB_CONFIG_DIR` set to `$CSESSION_DIR/gh-empty`;
- **git credential warning**, when a git directory was resolved: `exec` reads the repository config (read-only, hardened git; the resolved local and worktree config with includes) and warns "the repository git config may hold a credential in <key>" (in `warnings` and as a `config_warning` log event, naming the key with a subsection that holds userinfo masked as `<redacted>`, never its value) when it finds: any `http.*.extraheader` (or `http.extraHeader`) key; `sendemail.smtppass`, `sendemail.<id>.smtppass` or `imap.pass`; a `credential.helper` or `credential.<url>.helper` value containing `password=`; or a URL with userinfo in a value (`remote.*.url`, `remote.*.pushurl` and the like) or at the start of a key's subsection (`url.<base>.insteadOf`), where the userinfo holds a password (`scheme://user:secret@host`, any scheme) or, for `http` and `https`, a user name (a token is often given as the user name). When git cannot read the config, the warning is "could not read the repository git config to check it for credentials". The launch proceeds; the git directory is readable by the session (see [Residual risks](#residual-risks)).

**Planted skills and hooks.** With `setting_sources: []` (every built-in third-party-reading kind), Claude Code loads no project or nested `.claude/skills` or project hooks, so skills or hooks planted in the cwd never load (live E2). `Skill(...)` allow rules do not restrict skills without hooks (live E2), so claude-session does not rely on them and the built-in profiles allow `Skill`. Plugin skills' frontmatter hooks fire: plugin directories are trusted code.

**Sandbox baseline.** The sandbox mode of any third-party-reading run should be `required`. A run whose resolved mode is below it (from any origin: flag, config, repo override, carry-over) is allowed, audited `sandbox_lowered` at adoption and reported in `warnings`. A third-party-reading run that is not sandboxed (`preferred` without a working sandbox, or `off`) still gets the permission-layer isolation and a warning in `kinds --json` and `--dry-run --json`.

### File handling

- The state root and every session, private and temporary directory are created with mode 0700, every file with mode 0600 (explicit modes).
- Every file `exec` writes into `$CSESSION_DIR` (secret, `run.json`, `instructions.md`, prompts, inputs, `empty-gitconfig`; `gh-empty/` is recreated empty the same way on every isolated run) is written as a temporary file created with `O_NOFOLLOW|O_EXCL` in the private directory and renamed into place relative to directory file descriptors, so no temporary name is ever visible to the agent (the session and private directories must be on one file system, else exit 12); subdirectories are verified to be real directories owned by the user. Retention deletion is fd-based and never follows symlinks. A fork's copy of the source's `inputs/` is fd-based and rejects symlinks, non-regular files and files with more than one link.
- `report` and ingestion apply the file checks in [`report`](#report) (outbox claim by rename, artifact copy through one `O_NOFOLLOW` descriptor before hashing and scanning).
- Headless stderr is written to the private `stderr.log`, never to the caller.
- Every git command claude-session itself runs (all read-only) uses `-c core.fsmonitor= -c core.sshCommand= -c credential.helper= -c core.hooksPath=/dev/null -c diff.external= -c core.pager=cat` and `GIT_CONFIG_NOSYSTEM=1`, and `--no-ext-diff --no-textconv` where they apply.

## Environment variables

Set by `exec` for the session (after clearing any inherited `CSESSION_*`):

| Variable | Value |
|---|---|
| `CSESSION_ID` | the session id |
| `CSESSION_DIR` | the session directory |
| `CSESSION_KIND` | the run kind (claimed, not verified) |
| `PATH` | the directory of the running `claude-session` executable, then `env.path_prepend`, then the kind's `PATH`; for isolated runs sanitized per [Path rules](#path-rules) |
| `TMPDIR`, `CLAUDE_CODE_TMPDIR` | private temporary directory (sandboxed runs) |
| `GH_CONFIG_DIR`, `GLAB_CONFIG_DIR` | `$CSESSION_DIR/gh-empty` (third-party-reading runs) |
| `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_<n>`, `GIT_CONFIG_VALUE_<n>` | git hardening (third-party-reading runs; see [Generated settings](#generated-settings)) |
| `GIT_OPTIONAL_LOCKS` | `0` (every third-party-reading run) |

Reserved names, never config keys: `CSESSION_ID`, `CSESSION_DIR`, `CSESSION_KIND`, `CSESSION_HOOK`, `CSESSION_OBJECT_TYPE`. No `CSESSION_*` name may be set in `env.set` (an invalid config); inherited `CSESSION_*` variables are cleared.

Third-party-reading runs (by kind or `--reads-third-party`) also lose `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`, `GITLAB_TOKEN`, `GL_TOKEN`, `TMUX`, `TMUX_PANE` and `SSH_AUTH_SOCK`. These removals and the `GH_CONFIG_DIR`/`GLAB_CONFIG_DIR` values win over `env.set`; `env.set` may still set a `secret_env` name on any run. `env.set` values are not treated as secrets (they are not in the `contains_secret` index unless the name is a `claude_auth_env` name). Inherited entries no process environment can hold (an empty name, a name containing `=` or NUL, a value containing NUL) are dropped. Isolated runs also lose `LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT` and every `DYLD_*` variable, inherited or set by `env.set` (they would reach the hook interpreter, which runs outside the sandbox); third-party-reading runs lose the git override variables listed under [Generated settings](#generated-settings).

Read by claude-session: the `CSESSION_<KEY>` config overrides listed in [Config](#config); `CSESSION_ID`/`CSESSION_DIR` (by `report` and `hook-event`); `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_DATA_HOME` (unsafe write targets). An empty `CSESSION_<KEY>` override is an invalid config (exit 2; `detail` names the variable). Hook environment: `CSESSION_HOOK`, `CSESSION_ID`, `CSESSION_OBJECT_TYPE`.

No credentials are required by claude-session itself. `claude` uses its own login (or the variables in `claude_auth_env`).

## Labels

Sessions carry labels (`--label` on `exec`/`start`/`resume`, `label add/remove`, `list --label`); grammar in [Identifiers](#identifiers). `kind=<launch kind>` is added at creation (for a fork: its run-1 kind). agent-spawn is the authoritative label store for agents and mirrors agent labels onto their sessions; claude-session gives no key a built-in meaning.

## Audit and log

- `audit.jsonl` ([schemas/audit.json](../schemas/audit.json)) records `resume_tainted` (a tainted session resumed or forked into a kind that is not third-party-reading), `launch_escalation_override` (with `overridden_hooks`), `sandbox_lowered` (written at adoption), `stop` and `forget` (one record per session, also in bulk mode), each with `actor`, `decision_id` (an array, possibly empty), `target` (the csession_id), `inputs` (null when absent; may include `run_kind`, `kind`, `run`, `forked_from`, `labels`, `tty`, `overridden_hooks`, `sandbox_from`, `sandbox_to`, `grace`) and `result` (`ok`, `refused` or `error`). `--actor` is an audited honor system, not authentication. Launch is not an external action, and claude-session takes no external actions (no posting, pushing or merging). [`export`](#export) prints the records.
- `log.jsonl` ([schemas/log.json](../schemas/log.json)) records events of these types: `launch` (with decision ids; a launch-phase validation failure is a `launch` record at level `error` with its `reason`), `adopt`, `state_change`, `report_ingested`, `report_rejected`, `outbox_discarded`, `hook_delivered`, `hook_failed`, `hook_timeout`, `hook_veto`, `hook_escalate`, `hook_output_ignored` (non-JSON `pre-launch` stdout), `hook_event_failed`, `notes_failed`, `signal_sent` (with the exact PID), `reconcile_change`, `config_warning` (e.g. config hash changed since `start`, or a git credential warning), `session_start_missing`, `migration`, `sandbox_lowered`, `launch_interrupted`, `outcome_file_failed`. `reason` is a report rejection reason or a launch-phase end reason; the malformed outbox entries one drain removes are logged as one `report_rejected` record with reason `malformed` and `count`. The log never contains secrets (the values `contains_secret` uses, 8 or more characters; see Redaction below), summaries, reasons or prompt text. There is no log-level setting: every record is written, `debug` included.
- Both files hold one JSON object per line; `ts` is UTC with millisecond precision and a `Z` suffix. They are mode 0600, appended with `O_APPEND` (one write per record), and never written through a symlink or a hard link (a file with more than one link, or owned by another user, is refused, and so is a file whose name no longer refers to the opened file when it is checked); a torn last line (an earlier short write) is terminated before the next record. Appends are serialized by a file lock, waited for at most 1 s; a write that cannot get it fails.
- Redaction: the text fields are redacted against the values `contains_secret` uses (8 or more characters), each occurrence replaced by `[redacted]` (if a secret would still remain, the whole field is replaced). The text fields are `message`, `signal` and `decision_ids` in `log.jsonl`, and `actor`, `decision_id`, `inputs.overridden_hooks` and `inputs.labels` in `audit.jsonl`. Every other field (identifiers such as csession ids and kind names, enums, numbers, timestamps) is never redacted; it is validated against the schema and the [identifier grammars](#identifiers) instead. A label containing a secret is written as `<key>=redacted` when every occurrence lies wholly inside its value; it is dropped when an occurrence starts in its key or at the `=`, or if `<key>=redacted` would still contain one. `message` is cut to 4096 characters after redaction, ending with `[truncated]` (unless the whole field is replaced by `[redacted]`); the cut never splits a `[redacted]` marker.
- An audit record is written with `result: ok` before the audited action takes effect; if the action then fails, a second record with `result: error` follows; that follow-up record is best effort (if it cannot be written, the command's exit status is that of the failed action). A refused request for an audited action is recorded with `result: refused`, and the action is not taken. If a record cannot be written, the action is not taken and the command exits 1 (`internal`). `hook-event` logging is best effort (it still exits 0 with no output).

## Upgrades

Database migrations are additive within a schema major version. `hook-event` never migrates; with older code and a newer additive schema it keeps working. Every other command checks the schema first: it migrates an older database, and refuses a newer one (error code `schema_newer`, exit 12, nothing allocated). The schema check migrates an older database under `--dry-run` too; a migration is not an allocation.

Upgrade claude-session while no sessions are live: the injected hooks run a recorded interpreter path, and `reconcile` and `status` warn when a live run's recorded hook interpreter path no longer exists.

## Artifact contract for callers

A caller that launches a session with a skill expecting an artifact reads `status --json` (or the outcome JSON of a waiting command): `run.report.artifact.path` is the frozen copy in the private directory, `sha256` and `size` describe it; the artifact is the latest report's (or null), so instructions tell the agent to attach it on every report. The session cannot read or change the frozen copy. For implicit done (kinds with `implicit_done: true`), the artifact is `result.md` (the text of the final headless `result` object), `report.summary` is null and `report.implicit` is true; a caller that needs an explicit report checks `report.implicit` (or uses a kind with `implicit_done: false`). Inputs for the session are passed with `--input-file`.

**The `respond` slot.** claude-session does not interpret artifacts; this is the expectation of the caller (for example, review-respond) that owns the respond flow, recorded here because it shapes the `respond` kind. A `respond` agent edits files in its cwd but never commits (its git directory is read-only; its standard instructions say: do not stage or commit, leave the changes in the working copy, list the changed files). A caller forks into another directory (`--fork --cwd`), for example a disposable copy of the repository, so the agent never touches the user's own worktree; the forked conversation still mentions the source's paths, so the caller's prompt names the working copy. Because a plain resume always keeps the previous run's cwd ([Resume and fork carry-over](#resume-and-fork-carry-over)), a caller that runs several respond rounds forks every round from the previous round's respond fork into a new disposable directory (`--fork --cwd <new copy>`) and never resumes a respond session into a different directory. Its artifact is JSON:

```json
{"replies": [{"comment_id": "<id>", "body": "<text>"}], "changed_files": ["<path relative to the cwd>"], "deleted_files": ["<path relative to the cwd>"], "commit_message": "<text>"}
```

On the user's confirmation, the caller reads only the listed `changed_files` from the run's cwd (normalized relative paths with no `.git` component, every component opened without following symlinks, regular files with one link, size-capped; a missing file is an error and deletions come only from `deleted_files`), applies them in a checkout of its own, commits with the user's identity using hardened git outside the sandbox, pushes fast-forward, and posts the replies; nothing trusted runs inside the agent-touched directory. All of it is untrusted agent text.

Retention: session and private directories of sessions that are not forgotten are kept (they back parked agents); `retention_days` applies only after `forget`. Callers that keep artifacts copy them before `forget` plus the retention period.

## Residual risks

- Log and audit redaction is per field (and per list item): a secret split across fields, list items or records is not caught.
- A summary, reason or artifact may still leak a secret that is not in `claude_auth_env` or `secret_env`, or one transformed beyond a literal match; `contains_secret` catches only literal values of 8 or more characters.
- Repository-managed hook frameworks (husky, pre-commit configs) can be edited by a `respond` session; it cannot commit, and a caller that commits reads only the listed files and uses hardened git (`core.hooksPath=/dev/null`) in its own checkout.
- **Context carry**: forking or resuming a trusted session into a third-party-reading kind carries its whole conversation (possibly including secrets the trusted agent read) into a context driven by third-party text. `contains_secret` catches only configured literal values. This is the designed `respond` flow; a fork into a separate directory limits file access, and the sandbox has no network.
- **Concurrent sandboxed starts** (live E1): several sandboxed sessions starting at the same moment in the same cwd can hit a bubblewrap mount race. The failure surfaces through the normal session-phase mapping (`exit_<code>` or `no_session_start`); callers that start parallel sandboxed runs should give each its own cwd.
- The sandbox leaves empty `config.worktree` placeholder files and a `.claude/.cc-writes/` directory on the host (live test); inside the sandbox `git add -A` and `git add .` fail because of placeholder files, so instructions stage explicitly (and `respond` does not stage at all).
- The `GIT_CONFIG_COUNT` hardening is defense in depth only: a command can override it.
- `--plugin-dir` contents are trusted code: their hooks run outside the sandbox and their MCP servers may start (whether `--strict-mcp-config` drops plugin MCP servers is a live test). No refusal logic covers plugin hooks or MCP servers.
- An unsandboxed third-party-reading run (sandbox lowered, audited) relies on the permission layer alone.
- Managed policy settings can drop parts of the generated settings: `allowManagedPermissionRulesOnly` drops the `--settings` allow and deny rules (an isolated run then fails closed under `dontAsk`: the agent cannot file its report), and `allowManagedReadPathsOnly` drops the generated sandbox `allowRead` list (sandboxed commands fail under deny-root reads).
- If `disableAllHooks` or managed-only hooks suppress the injected hooks, no `SessionStart` arrives and state tracking is blind; `exec` warns after `session_start_timeout` (headless only).
- An injected hook that exceeds its timeout is SIGTERMed by Claude Code and its output dropped; ingestion is idempotent, so the next drain repeats it, but a state transition of that event is lost until the next event. The drain itself stops at half the hook timeout, scans included: a request whose `contains_secret` scan cannot finish within (about) that whole budget is rejected `too_large` rather than repeated by every drain; one that started late in a drain is left for the next.
- Orphaned interactive children (wrapper killed while the terminal stays open) keep running until `stop`; `reconcile` reports them and never signals.
- **`/etc` is readable** by every sandboxed command (deny-root reads keep it in `allowRead`). User-readable files there may hold credentials, for example proxy settings with a password in `/etc/environment`, `/etc/apt/apt.conf.d/` or `/etc/profile.d/`. Keep such credentials out of world-readable files. Likewise, a caller's cwd under a system read directory (`/usr`, `/etc`, ...) is readable by isolated sessions.
- **The git directory is readable** by a session whose cwd has one: its config, refs and objects. A credential stored in the repository config (`http.*.extraheader`, a remote URL with userinfo) is therefore readable by the agent; `exec` warns about these on third-party-reading runs but does not block them. Use a credential helper instead of storing credentials in the repository config.
- An agent can create nested git repositories (for example `sub/.git`) inside its writable cwd (live D2); the git-directory denies cover only the cwd's own git dir. A caller's own git operations should run only in its own checkout with explicit paths, never inside agent-created subdirectories.
- Symlink-following by the Read tool is unverified (F1, live test in the plan). It is mitigated by the home-directory Read denies under [Generated settings](#generated-settings) and by `blockReadsOutsideWorkingDirectories`.

## Platform notes

- Linux, WSL2 and macOS. The sandbox needs `bubblewrap` and `socat` on Linux and WSL2 (`sudo apt install bubblewrap socat`); macOS uses its built-in Seatbelt. WSL1 has no sandbox: kinds with sandbox `required` fail to launch there (`sandbox_unavailable`) unless the user sets `sandbox: preferred`.
- `sandbox_active` is true when the sandbox mode is not `off` AND its prerequisites were present at launch (Linux/WSL2: `bwrap` and `socat` on `PATH` and user namespaces usable; macOS: `sandbox-exec` present) AND the socket path fits. The sandbox self-check is that test.
- Keep the state directory on the Linux file system under WSL (not under `/mnt/`); `kinds` reports it in `state_dir_warnings`.
- Claude Code `min_claude_version` (default 2.1.312) or later.
- An interactive session in a directory Claude Code has never trusted waits at Claude's trust dialog in state `starting`; trust is inherited from a trusted parent directory. The dialog fires no hooks; Esc exits 0 and "No, exit" exits 1, both before `SessionStart` (`exited-unreported`, `no_session_start`).
- `Notification` `idle_prompt` arrives about 60 s after an interactive turn ends (measured three times).
