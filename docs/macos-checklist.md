# macOS manual checklist

Run this once on a Mac before 1.0 (issue #43; [design.md, Testing](design.md#testing) and [Platform notes](design.md#platform-notes)). Everything else claude-session relies on was verified on Linux. The macOS-specific code paths (`/bin/ps` start times, Seatbelt, `/private/tmp`, case folding, no `/proc`, no pidfd) are covered by unit tests with fakes, or not at all. This checklist is how they meet a real macOS.

**Status: not run on macOS.** Nothing below was observed on a Mac. Every expected result is derived from the code (the file or function is named in each step) and from [interface.md, Platform notes](interface.md#platform-notes). Items marked **to be observed** state the invariant that must hold and leave the actual value to the person running the step: record it, do not pre-fill it. The acceptance criterion "run once on macOS with results recorded" stays open until the [results record](#results-record) is filled in and committed.

M1 to M6 are free and need no real `claude` (the dry runs use the fake one from `tests/fakebin`), except the optional live steps M4.10 to M4.15, which are billed `make live` runs or `claude -p` runs by hand. The live suite ([live-tests.md](live-tests.md)) is the optional M7: it needs credentials and costs API usage, and it is the only way to see Claude Code's own Seatbelt profile enforce the generated settings. Every billed step says so.

## How to run it

- Use a Mac (Apple silicon or Intel) with the stock `/bin/zsh`, Xcode command line tools, [`uv`](https://docs.astral.sh/uv/) and `git`. Interactive live tests also need `tmux` (for example from Homebrew).
- Run from a checkout of this repository (a worktree is fine). Each step names its command and its expected result.
- Result values for the record: `PASS` (expected result seen), `FAIL` (differs: copy the output), `OBS` (a **to be observed** item: copy the value and say whether the invariant holds), `SKIP` (with the reason).
- A `FAIL` or a broken invariant is a finding. Open an issue; do not edit the expected result to match.

### Private environment (every step)

No step may touch your real configuration. Start one shell for the whole checklist and keep the root short: Unix socket names have to fit under it (M3).

```sh
export CS_MAC=$(mktemp -d /tmp/csm.XXXXXX)
mkdir -p "$CS_MAC/home" "$CS_MAC/xdg/config" "$CS_MAC/xdg/state" "$CS_MAC/xdg/data" "$CS_MAC/uv-cache"
export HOME="$CS_MAC/home" XDG_CONFIG_HOME="$CS_MAC/xdg/config" XDG_STATE_HOME="$CS_MAC/xdg/state" XDG_DATA_HOME="$CS_MAC/xdg/data" UV_CACHE_DIR="$CS_MAC/uv-cache"
cd /path/to/claude-session-checkout
export CS_PATH0="$PATH"
export PATH="$PWD/tests/fakebin:$PATH"
```

Expected: `echo "$CS_MAC"` prints `/tmp/csm.` and six characters. `/tmp` is a symlink on macOS (M3 checks that). With the private `HOME`, `uv` downloads its dependencies (and a Python, if none is on `PATH`) into the private cache the first time.

The dry runs (M3.7, M4.4 to M4.9, M6.2, M6.4) need a `claude` on `PATH`: without one they report `would_fail` `claude_not_found` and empty sandbox lists. `exec --dry-run` only resolves `claude` on `PATH` and checks the file; it never runs it (no `claude --version`, no API call). The fake `claude` in `tests/fakebin` stands in for it, so `which claude` prints `<checkout>/tests/fakebin/claude`. The live steps (M4.10 to M4.15, M7) need the real `claude`: run `export PATH="$CS_PATH0"` first.

A small git repository and a prompt file are used by M3 to M6:

```sh
git init -q "$CS_MAC/repo" && git -C "$CS_MAC/repo" -c user.name=t -c user.email=t@example.invalid commit -q --allow-empty -m init
echo "checklist" > "$CS_MAC/prompt.md"
```

When you are done, remove the scratch directory: `rm -rf "$CS_MAC"` (check `echo "$CS_MAC"` first).

## M1. Baseline

| ID | Step | Expected |
|---|---|---|
| M1.1 | `uv run python -c "from claude_session import selfcheck; print(selfcheck.detect_platform())"` | `macos` (`selfcheck.detect_platform`: `sys.platform == "darwin"`). |
| M1.2 | `make verify` | Everything passes: ruff, format, pyright, pytest, markdown lint. Every test marked Linux-only skips (it needs `/proc`, pidfd, `PR_SET_PDEATHSIG` or Linux pty names): the `linux_only` (`tests/conftest.py`) and `skipif(not LINUX)` ones in `tests/test_procinfo.py`, `test_gateway.py`, `test_fake_claude.py`, `test_leftover_guard.py`, `test_store.py`, `test_exec_hooks.py`, `test_toolhooks.py`, `test_sessiondir.py`, `test_exec_headless.py`, `test_exec_interactive.py`, and `test_dryrun.py::test_a_hanging_version_probe_leaves_no_process` (no `/proc`). `test_exec_headless.py::test_a_required_sandbox_without_prerequisites_is_not_started` skips too, because the Seatbelt sandbox is available. Record the pass and skip counts. macOS has `/usr/bin/true` but no `/bin/true`: the tests spawn `conftest.TRUE_BIN` (found in `/bin`, then `/usr/bin`), so do not create `/bin/true`. pyright checks for the platform it runs on; on Linux, `uv run pyright --pythonplatform Darwin` checks the macOS view and is clean as well. |
| M1.3 | `uv run python -c "import os, sys; print(sys.platform, os.path.realpath('/tmp'))"` | `darwin /private/tmp`. |
| M1.4 | `uv run python -c "import os; print(os.chmod in os.supports_follow_symlinks)"`, then `uv run pytest -v tests/test_tmpdir.py::test_remove_repairs_unreadable_subdirectories tests/test_fsutil.py::test_open_dir_at_repairs_an_owned_mode_000_directory tests/test_fsutil.py::test_ensure_dir_mode_repair_never_chmods_a_swapped_in_symlink` | The three tests `PASSED` (not `SKIPPED`). **To be observed**: the first command's value. Off Linux, `fsutil._repair_dir_mode_at` ([`fsutil.py`](../src/claude_session/fsutil.py)) repairs a mode-000 directory with a no-follow `chmod` relative to the parent and expects `True` here (`lchmod`); with `False` it refuses the repair (`UnsafePathError`), and removing a private tmpdir with an unreadable subdirectory fails: a finding. (Linux prints `False` and uses `O_PATH` and `/proc/self/fd` instead.) |

The real-process `procinfo` tests (`test_real_*` in `tests/test_procinfo.py`) are Linux-only, so on macOS only the fake-`ps` tests run. M2 exists for that reason.

## M2. Process start time and token

What the code does ([`procinfo.py`](../src/claude_session/procinfo.py)): on Linux the token is `<boot_id>:<start ticks>` from `/proc`. On macOS there is no `/proc`: it runs `/bin/ps -o stat= -o lstart= -p <pid>` through the gateway with only `LC_ALL=C`, `TZ=UTC` and the sanitized `PATH` in the environment, splits the output on whitespace, and returns the five `lstart` words joined by single spaces, for example `Mon Nov 2 14:40:01 2026`. The token has one-second resolution and no boot id (macOS allocates PIDs sequentially). The store compares tokens for equality only: a PID with another token is another process.

`store.start_time` turns every `procinfo` error into an empty string (an unanswerable check counts as alive), so the steps call `ProcInfo` directly. A silent fallback would otherwise look like a pass.

| ID | Step | Expected |
|---|---|---|
| M2.1 | `/bin/ps -o stat= -o lstart= -p $$` | One line: a state such as `Ss`, then `lstart` as five words, for example `Mon Nov  2 14:40:01 2026` (BSD `ps` pads a single-digit day with two spaces). Record the line. |
| M2.2 | `uv run python -c "import os; from claude_session import gateway, procinfo; p = procinfo.ProcInfo(gateway.SystemGateway()); t = p.start_time(os.getpid()); print(repr(t), p.is_alive(os.getpid(), t))"` | A token of five words separated by single spaces (`'Mon Nov 2 14:40:01 2026'`: no double space, no state letters) and `True`. An exception or `None` is a finding. |
| M2.3 | Run M2.2 with `TZ=Asia/Tokyo LC_ALL=de_DE.UTF-8` in front of `uv run`. | The same shape, with the time in UTC (the code forces `TZ=UTC`). **To be observed**: that BSD `ps` honours `TZ` for `lstart`; if it prints local time anyway the token is still stable per process, but note it. |
| M2.4 | `uv run python -c "import os; from claude_session import gateway, procinfo; p = procinfo.ProcInfo(gateway.SystemGateway()); a = p.start_time(os.getpid()); b = p.start_time(os.getpid()); print(repr(a), a == b)"` | The token and `True`. |
| M2.5 | `uv run python -c "from claude_session import gateway, procinfo; print(repr(procinfo.ProcInfo(gateway.SystemGateway()).start_time(1)))"` | A token for `launchd` (boot time, UTC). **To be observed**: that `ps` may read another user's process. A `ProcInfoError` here means `is_alive` raises for processes of other users. |
| M2.6 | The script below. | The output shown under it. |

```sh
uv run python - <<'EOF'
import os, signal, time
from claude_session import gateway, procinfo
gw = gateway.SystemGateway()
p = procinfo.ProcInfo(gw)
time.sleep(1.1)  # lstart has one-second resolution: start the child in a later second
child = gw.spawn(["/bin/sleep", "60"])
token = p.start_time(child.pid)
print("running", repr(token), p.is_alive(child.pid, token))
child.send_signal(signal.SIGKILL)
for _ in range(100):  # not reaped yet: a zombie
    if p.start_time(child.pid) is None:
        break
    time.sleep(0.05)
print("zombie ", p.start_time(child.pid), repr(p.start_time(child.pid, zombie=True)), p.is_alive(child.pid, token))
child.wait(timeout=30)
print("reaped ", p.start_time(child.pid), p.is_alive(child.pid, token))
print("other  ", p.is_alive(os.getpid(), token))
EOF
```

Expected (the token is whatever M2.2 style gives; it is the same on the `running` and the `zombie=True` lines):

```text
running 'Mon Nov 2 14:40:01 2026' True
zombie  None 'Mon Nov 2 14:40:01 2026' False
reaped  None False
other   False
```

- `zombie`: `start_time(pid)` is `None` and `is_alive` is `False` (a zombie counts as "wrapper dead"); with `zombie=True` the token is still given. **To be observed**: the raw `/bin/ps -o stat= -o lstart= -p <zombie pid>` line. The code accepts a state starting with `Z` with a full line, and a short line (which it treats as gone); record which one macOS prints.
- `other`: the token of this Python process, which started more than a second before the child (the `sleep(1.1)`), differs from the child's. Without the pause both can start in the same second and give equal tokens, which is the documented one-second resolution, not a finding.
- `reaped`: `ps` exits 1 with no output for a missing PID, which the code reads as gone. **To be observed**: `/bin/ps -p 99999999; echo $?` prints `1`. Any other non-zero status raises `ProcInfoError`.
- A `ps` that takes longer than 10 s (`PS_TIMEOUT`) raises `GatewayTimeout`, which `store.start_time` turns into `""`.

## M3. Temporary directories and socket paths

What the code does: the private temporary directory of a sandboxed run is `<realpath(/tmp)>/csession-<euid>/<session id>` ([`tmpdir.py`](../src/claude_session/tmpdir.py), `pathrules.TMP_ROOT = "/tmp"`). A session id is `cs-` and 8 characters (11 bytes). On macOS `/tmp` is a symlink to `/private/tmp`, so the directory is `/private/tmp/csession-501/cs-xxxxxxxx` for uid 501 (37 bytes). The default macOS `$TMPDIR` (`/var/folders/xx/.../T/`, behind the `/var` symlink to `/private/var`) is never used as the base.

`envbuild` sets `TMPDIR` and `CLAUDE_CODE_TMPDIR` to the private directory of a sandboxed run. Claude Code puts the proxy sockets of the sandbox network bridge there; the names `claude-http-<16 hex>.sock` and `claude-socks-<16 hex>.sock` were read from the Claude Code binary and not verified live (`selfcheck.py`, #41, #60). `selfcheck.check_socket_path` requires `len(dir) + 1 + 40 <= 103` on macOS (104 bytes including the NUL; 40 bytes reserved for a 34-byte name plus margin), so the private directory path may be at most 62 bytes. Failure is `socket_path_too_long`; it fails every sandboxed run, under `preferred` too.

| ID | Step | Expected |
|---|---|---|
| M3.1 | `ls -ld /tmp /var; stat -f '%Su %Sp' /private/tmp` | `/tmp -> private/tmp`, `/var -> private/var`, and `root drwxrwxrwt` (root-owned and sticky, which `tmpdir._open_base` accepts). |
| M3.2 | `uv run python -c "from claude_session import tmpdir; p = tmpdir.run_path('cs-aaaaaaaa'); print(p, len(p))"` | `/private/tmp/csession-<your uid>/cs-aaaaaaaa` and `37` for a three-digit uid (`id -u`). The `/tmp` symlink is resolved before use. |
| M3.3 | `printf '%s\n' "$TMPDIR"` | Record it. **To be observed**: typically `/var/folders/<2 characters>/<about 30 characters>/T/` (the example in M3.5 has 30; real lengths vary). It must not appear in M3.2's output. |
| M3.4 | `uv run python -c "import os; print(os.path.realpath(os.environ['TMPDIR']))"` | `/private/var/folders/.../T`. With a 30-character component that is 56 bytes; appended with `/csession-<uid>/cs-aaaaaaaa` (25 bytes for a three-digit uid) it is 81 bytes, over the 62 allowed: this is why the base is `/tmp`. Record the real length. |
| M3.5 | The first script below. | `ok 37`, then `socket_path_too_long` with the message "would need 122 bytes; the limit is 103 bytes". |
| M3.6 | The second script below (binds Unix sockets in a `/tmp` directory with names of growing length). | **To be observed**: the longest path that binds is **103 bytes**, and 104 fails with `OSError` (`AF_UNIX path too long`). This measures `sizeof(sun_path)` as CPython was built with it: CPython refuses a path that does not fit with its terminating NUL before calling the kernel, so a 104 failure comes from that check, not from `bind(2)`. Claude Code's runtime fills the same `struct sockaddr_un`, so it is the limit that matters. The code assumes a 104-byte `sun_path`. A higher limit makes the check merely conservative; a lower one makes it wrong: a finding. |
| M3.7 | `ls /tmp/csession-$(id -u) > "$CS_MAC/before.txt" 2>&1`, then `uv run claude-session exec --dry-run --json --kind review --sandbox preferred --mode headless --prompt-file "$CS_MAC/prompt.md" > "$CS_MAC/dry.json"`, then `ls /tmp/csession-$(id -u) 2>&1 \| diff - "$CS_MAC/before.txt" && echo unchanged` | `unchanged`: a dry run creates nothing in the private root. |

```sh
uv run python - <<'EOF'
from claude_session import errors, selfcheck
good = "/private/tmp/csession-501/cs-aaaaaaaa"
long = "/private/var/folders/zz/zyxvpxvq6csfxvn_n0000000000000/T/csession-501/cs-aaaaaaaa"
for path in (good, long):
    try:
        selfcheck.check_socket_path(path, "macos")
        print("ok", len(path))
    except errors.LaunchValidationError as exc:
        print(exc.end_reason, exc)
EOF
```

```sh
uv run python - <<'EOF'
import os, socket, tempfile
base = tempfile.mkdtemp(prefix="sock.", dir="/tmp")
real = os.path.realpath(base)
best = None
for total in range(90, 110):
    path = real + "/" + "s" * (total - len(real) - 1)
    s = socket.socket(socket.AF_UNIX)
    try:
        s.bind(path)
        best = total
        os.unlink(path)
    except OSError as exc:
        print("fails at", total, exc)
        break
    finally:
        s.close()
print("longest ok:", best)
os.rmdir(base)
EOF
```

## M4. Seatbelt sandbox

What the code does ([`selfcheck.py`](../src/claude_session/selfcheck.py)): the sandbox counts as available on macOS when `/usr/bin/sandbox-exec` is an executable regular file and the probe `/usr/bin/sandbox-exec -p "(version 1)(allow default)" /usr/bin/true` exits 0 (10 s timeout; only `PATH=/usr/bin:/bin` and `LC_ALL=C` in the environment). No `bwrap` or `socat` is looked up on macOS. With the probe failing: `required` ends the launch `sandbox_unavailable` (hint: the macOS Seatbelt sandbox needs `/usr/bin/sandbox-exec`); `preferred` warns and records `sandbox_active: false`, and Claude Code then runs unsandboxed with a warning; `off` checks nothing. The probe shows that `sandbox-exec` runs a trivial profile. It does not show that Claude Code's own generated profile works (M4.11).

| ID | Step | Expected |
|---|---|---|
| M4.1 | `ls -l /usr/bin/sandbox-exec; /usr/bin/sandbox-exec -p "(version 1)(allow default)" /usr/bin/true; echo "exit $?"` | The file exists and is executable; no output; `exit 0`. **To be observed**: the tool is documented as deprecated; record whether the run prints a notice and whether the fixed path holds on this macOS version. A non-zero exit or another location is a finding for `selfcheck.SANDBOX_EXEC`. |
| M4.2 | `uv run python -c "from claude_session import selfcheck; print(selfcheck.check_prerequisites('macos', bwrap=None, socat=None))"` | `Prerequisites(available=True, problem=None, hint=None)`. |
| M4.3 | `uv run python -c "from claude_session import selfcheck; print(selfcheck.check_prerequisites('macos', bwrap=None, socat=None, sandbox_exec='/nonexistent/sandbox-exec'))"` | `available=False`, a problem naming that path, and the Seatbelt hint. |
| M4.3b | The script below the table. | `preferred False` with one warning naming `/nonexistent/sandbox-exec` and the Seatbelt hint; `required sandbox_unavailable`; `long socket_path_too_long` with "would need N bytes; the limit is 103 bytes", N over 103. This is the `preferred` fallback as claude-session sees it (covered by `tests/test_selfcheck.py::test_preferred_without_prerequisites_warns_and_still_gets_a_tmpdir` and `test_macos_failing_probe` with fakes). |
| M4.4 | `uv run claude-session exec --dry-run --json --kind review --mode headless --prompt-file "$CS_MAC/prompt.md" > "$CS_MAC/dry.json"`, then `uv run python -c "import json,os; d = json.load(open(os.environ['CS_MAC'] + '/dry.json')); print(d['would_fail'], d['sandbox']['available'], d['sandbox']['problems'], d['warnings'])"` | `None True [] []`, or warnings unrelated to the sandbox. (On Linux without `bwrap` the same command prints `sandbox_unavailable`.) |

```sh
uv run python - <<'EOF'
from claude_session import errors, selfcheck
for name, mode, base in (("preferred", "preferred", None), ("required", "required", None), ("long", "preferred", "/tmp/" + "d" * 60)):
    try:
        r = selfcheck.self_check(mode, None, bwrap=None, socat=None, create=False, tmp_base=base, sandbox_exec="/nonexistent/sandbox-exec")
        print(name, r.sandbox_active, r.warnings)
    except errors.LaunchValidationError as exc:
        print(name, exc.end_reason, exc)
EOF
```

M4.3b passes a missing `sandbox_exec` to `selfcheck.self_check` (dry-run form, `create=False`: nothing is created); the platform is detected. Claude Code's own behavior under the fallback (running unsandboxed with a warning) needs a failing `/usr/bin/sandbox-exec`, which cannot be arranged on a Mac without disabling SIP; that is out of scope. Do not move or edit `/usr/bin/sandbox-exec`.

### The generated settings on macOS

M4.5 to M4.9 read dry runs: `--sandbox preferred` fills the lists whenever the sandbox is available. A `respond` run reads third-party content with the caller's cwd, which gives the git-directory rules.

```sh
uv run claude-session exec --dry-run --json --kind respond --cwd "$CS_MAC/repo" --sandbox preferred --mode headless --prompt-file "$CS_MAC/prompt.md" > "$CS_MAC/dry-respond.json"
uv run python -c "import json,os; d = json.load(open(os.environ['CS_MAC'] + '/dry-respond.json')); [print(k, *d['sandbox'][k], sep='\n  ') for k in ('allow_read', 'allow_write', 'deny_read', 'deny_write')]"
```

| ID | Item | Expected |
|---|---|---|
| M4.5 | Deny-root reads with the allowlist (`pathrules.SYSTEM_READ_DIRS`, `settings_builder.system_read_dirs`) | `deny_read` is `['/']`. `allow_read` holds the cwd as its realpath (`/private/tmp/csm.../repo`, never `/tmp/...`), the session directory, `/usr`, `/bin`, `/sbin`, `/etc` and `/private/etc` (the system directories as given and resolved), the directories of `claude` (here the checkout's `tests/fakebin`) and `git`, claude-session's venv and Python prefixes, and `<repo>/.git`. `/lib`, `/lib32`, `/lib64` are absent (missing entries are skipped). `/opt` is absent by design: a Homebrew toolchain goes in `read_dirs`. |
| M4.6 | TMPDIR in `allowWrite` only | `allow_write` contains `/private/tmp/csession-<uid>/cs-aaaaaaaa` (the placeholder id of a dry run); no `allow_read` entry is under `/private/tmp/csession-<uid>` (design.md E1: in `allowRead` it makes every Bash call fail). |
| M4.7 | Git directory and config denies | `<repo>/.git` (realpath) is in `allow_read` and in `deny_write`; `<repo>/.claude`, `.mcp.json`, `CLAUDE.md` and `CLAUDE.local.md` are in `deny_write` although none exists; the private directory under the state root is in `deny_write`. **To be observed**: no list keeps the unresolved `/tmp/...` spelling (the state root is under `/tmp/csm.XXXXXX`); a rule on the symlink spelling would not match the real path Seatbelt sees. |
| M4.8 | Missing paths: the first commands below | `exit 12`, then `plugin_dir_missing` and a warning "a launch would fail (plugin_dir_missing): plugin directory does not exist: '...'" (other warnings may follow). |
| M4.9 | Plugin `--add-dir`: the second commands below | `exit 0`, `None`, then the values after each `--plugin-dir` and `--add-dir` in `claude_argv`: the session directory, then the plugin twice (once for `--plugin-dir`, once for `--add-dir`), as the realpath `/private/tmp/csm.../plugin`, never `/tmp/...`. Then run the list-printing command above with `dry-plugin.json` in place of `dry-respond.json`: the plugin is in `allow_read` and `deny_write`, and not in `allow_write`. |

```sh
uv run claude-session exec --dry-run --json --kind respond --cwd "$CS_MAC/repo" --sandbox preferred --mode headless --plugin-dir "$CS_MAC/does-not-exist" --prompt-file "$CS_MAC/prompt.md" > "$CS_MAC/dry-missing.json"; echo "exit $?"
uv run python -c "import json,os; d = json.load(open(os.environ['CS_MAC'] + '/dry-missing.json')); print(d['would_fail']); print(*d['warnings'], sep='\n')"
```

```sh
mkdir -p "$CS_MAC/plugin/.claude-plugin"
uv run claude-session exec --dry-run --json --kind respond --cwd "$CS_MAC/repo" --sandbox preferred --mode headless --plugin-dir "$CS_MAC/plugin" --prompt-file "$CS_MAC/prompt.md" > "$CS_MAC/dry-plugin.json"; echo "exit $?"
uv run python -c "import json,os; d = json.load(open(os.environ['CS_MAC'] + '/dry-plugin.json')); a = d['claude_argv']; print(d['would_fail']); print([a[i + 1] for i, x in enumerate(a) if x in ('--plugin-dir', '--add-dir')])"
```

The lists are text until Claude Code's Seatbelt profile enforces them. Enforcement needs a real `claude`, credentials and API budget (billed), so M4.10 to M4.15 are optional. Before them: `export PATH="$CS_PATH0"` (the real `claude`, not the fake), and `export ANTHROPIC_API_KEY=...` or `export CLAUDE_CODE_OAUTH_TOKEN=...` (from `claude setup-token`), the variables [live-tests.md](live-tests.md) names. `CSESSION_LIVE_COPY_LOGIN=1` does not work from this shell: it copies the login file from `HOME`, which is the private one here; on macOS Claude Code may also keep its login in the Keychain rather than in a file (**to be observed**). `CSESSION_LIVE_MAX_USD` caps the cost of a `make live` run (default 3). These are the live tests of M7 (`make live PYTEST_ARGS='-k "sandbox or e2e or isolation"'`), which skip where no sandbox is present (`harness.sandbox_available` assumes Seatbelt is always there on macOS). Their hand-built settings use `/usr`, `/bin`, `/sbin`, `/etc` (and realpaths), the cwd and the TMPDIR in `allowWrite`. What to read from them on a Mac:

| ID | Item (live test) | Expected / to be observed |
|---|---|---|
| M4.10 | Deny-root reads, allowlist, TMPDIR in `allowWrite` (`test_sandbox.py::test_deny_root_hides_files_outside_the_allowlist_and_tools_still_work`, `test_e2e.py::test_a_review_run_...`) | A marker outside the allowlist is not readable from Bash; `ls`, `cat`, `sh -c` and a write in the cwd work. **To be observed**: that a Unix socket can be bound in a TMPDIR listed only in `allowWrite` (the network bridge needs it) and that `/usr`, `/bin`, `/sbin`, `/etc` are enough for `ls`, `cat`, `sh`, `git` and `python3`. If a tool fails with "operation not permitted" or "no such file", note the path: the allowlist has no `/System`, `/Library` or `/private/var/db`, and a missing system directory would be a real finding. That is a hypothesis from reading the code, not an observation. |
| M4.11 | Probe fidelity | If M4.1 passes but M4.10 shows Claude Code failing to start its sandbox, the probe passes while the sandbox does not work: record it; `sandbox_active` would then be true when it should not be. |
| M4.12 | Git directory read-only, main checkout and linked worktree (`test_sandbox.py::test_a_denied_git_dir_is_read_only_for_sandboxed_git`) | `git status` works; `git branch` and `git commit` fail; the repository keeps one commit. **To be observed**: that `/usr/bin/git`, an Xcode shim that execs the real binary under `/Library/Developer/CommandLineTools`, runs under deny-root reads. |
| M4.13 | Missing-path denies (by hand, billed: the commands below the table) | No existing test checks this on macOS. The settings have the deny-root shape of `sandbox_settings` in `tests/live/test_sandbox.py`, with a `denyWrite` entry for `<work>/CLAUDE.md`, which does not exist yet (as M4.7 generates for `.claude` and `CLAUDE.md`); the prompt asks Bash to create it. Expected: `denied` (design.md: Claude Code's sandbox blocks these on Linux). **To be observed** on Seatbelt; also record what `claude` printed. |
| M4.14 | Plugin dir via `--add-dir` (`test_isolation.py::test_a_plugin_dir_is_readable_...`) | Readable by the Read tool, not writable. The suite's root is under `/tmp`, a symlinked ancestor: note whether the realpath form is what Seatbelt matched. |
| M4.15 | `autoAllowBashIfSandboxed` (`test_sandbox.py::test_auto_allow_bash_...`) | With `false`, `echo hi > out.txt` is denied; with the default, it is written. As on Linux. |

M4.13, by hand (one `claude -p` run with the small model, capped at 0.25 USD; needs the real `claude` and a credential, see above):

```sh
W="$CS_MAC/m413"; mkdir -p "$W/work" "$W/tmp"
uv run python - "$W" <<'EOF'
import json, os, sys
w = sys.argv[1]
work, tmp = os.path.realpath(w + "/work"), os.path.realpath(w + "/tmp")
read = []
for d in ("/usr", "/bin", "/sbin", "/etc"):
    for c in (d, os.path.realpath(d)):
        if c not in read:
            read.append(c)
fs = {"denyRead": ["/"], "allowRead": read + [work], "allowWrite": [tmp], "denyWrite": [work + "/CLAUDE.md"]}
settings = {
    "permissions": {"defaultMode": "dontAsk", "allow": ["Bash(echo:*)"]},
    "sandbox": {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
                "autoAllowBashIfSandboxed": True, "network": {"allowedDomains": []}, "filesystem": fs},
}
json.dump(settings, open(w + "/settings.json", "w"), indent=1)
EOF
(cd "$W/work" && TMPDIR="$W/tmp" claude -p --model haiku --max-budget-usd 0.25 --settings "$W/settings.json" -- 'Use the Bash tool to run exactly this command: echo x > CLAUDE.md')
test ! -e "$W/work/CLAUDE.md" && echo denied
```

## M5. BSD and GNU tool differences

The code and tests run fixed paths (`/bin/ps`, `/usr/bin/git`, `/usr/bin/true`, `/bin/sh`, `/bin/sleep`, `/bin/echo`) and Python; no GNU-only flag is in the sources. These are the differences that matter on a Mac.

| ID | Item | Expected |
|---|---|---|
| M5.1 | `ps` is BSD `ps`, not procps | M2.1 shows the `lstart` column; `/bin/ps -p 99999999; echo $?` prints `1` and no process line. |
| M5.2 | `/bin/sh` (hooks run as `sh -c`) | `/bin/sh -c 'echo ok'` prints `ok`. **To be observed**: `ls -l /bin/sh` (bash 3.2 in POSIX mode or `dash`, depending on the release). |
| M5.3 | Fixed tool paths | `ls /bin/sleep /bin/echo /usr/bin/true /usr/bin/git /bin/ps` finds all of them. There is no `/bin/true` on macOS; the tests use `conftest.TRUE_BIN` (M1.2). |
| M5.4 | `git` | `git --version` works. The code runs git with `-c core.fsmonitor= -c core.hooksPath=/dev/null ...` and `GIT_CONFIG_NOSYSTEM=1`; a Homebrew `git` earlier on `PATH` is also fine. |
| M5.5 | Case-insensitive file system (`pathrules.CASE_INSENSITIVE`) | `uv run python -c "from claude_session import pathrules; print(pathrules.CASE_INSENSITIVE)"` prints `True`. Paths are compared case-folded and NFD-decomposed. **To be observed**: on a case-sensitive volume the rule is only stricter. |
| M5.6 | GNU-only commands | The code, the tests and this checklist use none of `timeout`, `readlink -f`, `stat -c`, `sha256sum`, `date -d`, `sed -i` without a suffix. This checklist uses `stat -f` and a BSD-compatible `mktemp -d` template. Do not install `coreutils` to make a step pass: it would hide a finding. |

## M6. Hard links and `uv`

`settings_builder.check_hard_links` ([`settings_builder.py`](../src/claude_session/settings_builder.py)) refuses the launch of an isolated run (`unsafe_path`) when a regular file in the claude-session package directory or its virtual environment's site-packages has more than one link: the unsandboxed hook could be changed through another link. `uv` hard-links from its cache by default on Linux; its documented default on macOS is `clone` (copy-on-write on APFS), which does not raise the link count, but that has not been checked here. `pyproject.toml` sets `link-mode = "copy"` for the dev environment, and the error message names `uv tool install --link-mode=copy` (or `UV_LINK_MODE=copy`).

| ID | Step | Expected |
|---|---|---|
| M6.1 | `uv run python -c "import os; from claude_session import settings_builder as s; s.check_hard_links([os.path.dirname(s.__file__)]); print('no hard links')"` | `no hard links`. |
| M6.2 | `uv tool install --link-mode=copy --force .` (installs under the private `XDG_DATA_HOME`; `uv tool dir` and `uv tool dir --bin` print where), then `"$(uv tool dir --bin)/claude-session" exec --dry-run --json --kind review --mode headless --prompt-file "$CS_MAC/prompt.md"; echo "exit $?"` | `"would_fail": null` in the JSON and `exit 0`. |
| M6.3 | `D="$(uv tool dir)/claude-session"; test -d "$D" && find "$D" -type f -links +1 \| head \|\| echo MISSING` | No output. `MISSING` means the tool directory is not where `uv tool dir` says (a `FAIL`, not a pass). |
| M6.4 | `UV_LINK_MODE=hardlink uv tool install --force --reinstall .`, then the dry run of M6.2 | **To be observed**: with the cache and target on one volume this prints `"would_fail": "unsafe_path"` with a warning naming the file with 2 hard links and the `uv tool install --link-mode=copy` (or `UV_LINK_MODE=copy`) fix, and `exit 12`; if it passes, record the volume layout. Either way the error text must match the fix that works. |

## M7. Optional: the live suite on macOS

Requires credentials and API budget (billed; `CSESSION_LIVE_MAX_USD` caps a run, default 3); read [live-tests.md](live-tests.md) first. As for M4.10 to M4.15: `export PATH="$CS_PATH0"` and export `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`; `CSESSION_LIVE_COPY_LOGIN=1` finds no login under the private `HOME`, and without a credential every test that needs a login skips. The suite's private root is directly under `/tmp` (`tempfile.mkdtemp(prefix="cs-live-", dir="/tmp")` in `tests/live/harness.py`, so `/private/tmp/cs-live-.../` after resolution), its `TMPDIR` is `<root>/tmp`, and a socket name inside it has to fit M3's limit.

| ID | Step | Expected |
|---|---|---|
| M7.1 | `make live PYTEST_ARGS='-k sandbox'` | Passes, or fails for a reason you record under M4.10 to M4.15. |
| M7.2 | `make live PYTEST_ARGS='-k interactive'` (needs `tmux`) | Passes; the test that reads `/proc/<pid>/cmdline` skips (it is marked Linux-only). |
| M7.3 | `make live` | Everything else as on Linux. Compare the recorded observations with [live-tests.md](live-tests.md#observations). |
| M7.4 | While M7.1 runs, in a second shell: `lsof -U -a -c claude 2>/dev/null \| grep -o '/[^ ]*\.sock' \| sort -u; ls -la /private/tmp/cs-live-*/tmp; find /private/var/folders -name 'claude-*.sock' 2>/dev/null` | **To be observed**: where Claude Code creates its `claude-*.sock` files. Expected from the code: under the run's `TMPDIR` (`/private/tmp/cs-live-.../tmp` here; `/private/tmp/csession-<uid>/cs-xxxxxxxx` for a `claude-session` run), none under `/var/folders`. Record the socket names and their length in bytes against the 40 bytes `selfcheck` reserves (`SOCKET_NAME_RESERVE`). |

## M8. Absent on macOS (do not "fix")

| Item | Where | On macOS |
|---|---|---|
| `bubblewrap`, `socat` | `selfcheck._linux` | Never looked up; the `apt install` hints appear only on Linux and WSL2. |
| `/proc`, boot id | `procinfo._linux_start` | Replaced by `ps` (M2). |
| `pidfd`, `PR_SET_PDEATHSIG`, `os.waitid` | `gateway`, `tests/conftest.py` | `die_with_parent` is Linux only and ignored elsewhere; process-group handling is best effort without `os.waitid` (gateway docstrings). **To be observed** only if a leaked process is seen after `exec` is interrupted: report its PID, and do not kill by name pattern. |
| WSL1 handling | `selfcheck._WSL1_HINT` | Not applicable. |

## Results record

Fill this in after the run and commit it with the macOS version. Leave `not run` until a person has run the step.

| Field | Value |
|---|---|
| macOS version and hardware | not run |
| claude-session commit | not run |
| `claude --version` (M7 only) | not run |
| Date and person | not run |

| ID | Result | Observed value or note |
|---|---|---|
| M1.1 to M1.4 | not run | |
| M2.1 to M2.6 | not run | |
| M3.1 to M3.7 | not run | |
| M4.1 to M4.9 | not run | |
| M4.10 to M4.15 | not run | |
| M5.1 to M5.6 | not run | |
| M6.1 to M6.4 | not run | |
| M7.1 to M7.4 | not run | |

Findings go to GitHub issues and are linked here.
