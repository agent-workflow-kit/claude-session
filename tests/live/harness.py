"""Support code for the ``live`` suite: tests that run the REAL ``claude`` binary (``make live``).

Live tests cost API usage, so the suite is opt-in twice over: ``addopts`` deselects the
``live`` marker, and ``conftest.py`` skips every test here unless ``CSESSION_LIVE=1``
(``make live`` sets it). Nothing in this module starts ``claude`` at import; the pure helpers
(environment building, stream parsing, the cost ledger, classification) are unit-tested with
the fake claude in ``tests/test_live_harness.py``.

Isolation. Every test gets a ``LiveEnv``: a private root directory (short, directly under
``/tmp``, so Claude's socket paths fit) holding a private ``HOME``, ``XDG_*`` directories,
``TMPDIR`` and the working directories. The environment handed to ``claude`` is built from an
allowlist, never from the whole parent environment, so nothing of the user's real
configuration, tmux, ssh agent or git-host login leaks in. The user's real ``~/.claude`` is
never written; it is read only when ``CSESSION_LIVE_COPY_LOGIN=1`` asks for the login file to
be copied (once per pytest session, see ``LoginCache``) into the private homes of the tests that
need credentials. A run without credentials (``auth=False``) gets a home of its own without it.

Cost. Every path that starts ``claude`` first passes ``LiveEnv.spend_gate`` (the test skips
once the session's ``Ledger`` has reached ``CSESSION_LIVE_MAX_USD``) and is then charged: the
cost the run reported, or the per-run estimate ``CSESSION_LIVE_RUN_USD`` when the cost is
unknown (a timeout, unparsable output, a killed run, an interactive session). Every ``-p`` argv
carries ``--max-budget-usd`` (``LiveEnv.budget``); interactive sessions cannot take it.

Processes. A ``claude`` run goes through the gateway, so a timeout ends exactly the process
group it started. A spawned headless run gets its own process group, killed at teardown while
its leader is still our unreaped child. A hook child that outlives ``claude`` is signalled by
exact PID only while its start-time token still matches the one the hook recorded itself
(``LiveEnv.pid_record_command``).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from claude_session import gateway, procinfo
from claude_session.gateway import Child, GatewayTimeout, Result

OPT_IN_ENV = "CSESSION_LIVE"
COPY_LOGIN_ENV = "CSESSION_LIVE_COPY_LOGIN"
MODEL_ENV = "CSESSION_LIVE_MODEL"
MAX_USD_ENV = "CSESSION_LIVE_MAX_USD"
RUN_USD_ENV = "CSESSION_LIVE_RUN_USD"
OBSERVATIONS_ENV = "CSESSION_LIVE_OBSERVATIONS"
CLAUDE_ENV = "CSESSION_LIVE_CLAUDE"

DEFAULT_MODEL = "haiku"
DEFAULT_MAX_USD = 3.0
DEFAULT_RUN_USD = 0.25

# Parent variables copied into the private environment: locale, terminal type, proxy and
# certificate settings (a corporate network may need them), and the credentials Claude Code
# reads from the environment. Everything else (HOME, XDG_*, TMUX, SSH_AUTH_SOCK, GH_*, AWS_*,
# CLAUDE_*, ANTHROPIC_* other than the auth ones...) is dropped.
PASS_THROUGH = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
)
AUTH_VARS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
# Variables a test's `extra_env` may not set: every name `build_env` sets except `PATH`, plus
# every `XDG_*` name, `GIT_CONFIG_GLOBAL` and `CLAUDE_CONFIG_DIR` (see `forbidden_extra`), so a
# test cannot point claude back at the real configuration or undo the private environment.
FORBIDDEN_EXTRA = (
    "HOME",
    "TMPDIR",
    "GH_CONFIG_DIR",
    "GLAB_CONFIG_DIR",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "CLAUDE_CONFIG_DIR",
    "DISABLE_AUTOUPDATER",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
)


def forbidden_extra(name: str) -> bool:
    """Whether a test's ``extra_env`` may not set ``name``."""
    return name in FORBIDDEN_EXTRA or name.startswith("XDG_")


def opted_in(environ: Mapping[str, str]) -> bool:
    """Whether the user asked for live tests (``CSESSION_LIVE=1``)."""
    return environ.get(OPT_IN_ENV) == "1"


def claude_path(environ: Mapping[str, str]) -> str | None:
    """The ``claude`` to test: ``CSESSION_LIVE_CLAUDE``, else the first one on ``PATH``."""
    explicit = environ.get(CLAUDE_ENV)
    if explicit:
        return os.path.abspath(explicit) if os.access(explicit, os.X_OK) else None
    return tool_path("claude", environ)


def tool_path(name: str, environ: Mapping[str, str]) -> str | None:
    """``name`` found on ``environ``'s ``PATH``, as an absolute path (the gateway refuses a
    relative one, which a relative ``PATH`` entry would give)."""
    found = shutil.which(name, path=environ.get("PATH"))
    return os.path.abspath(found) if found else None


@dataclass(frozen=True)
class Auth:
    """How a private home logs in: environment variables, or a login file copied from
    ``source`` to ``<home>/.claude/.credentials.json``."""

    env: Mapping[str, str] = field(repr=False)
    source: Path | None
    description: str

    @property
    def api_key(self) -> str | None:
        return self.env.get("ANTHROPIC_API_KEY")


def resolve_auth(environ: Mapping[str, str], real_home: Path) -> Auth | None:
    """Pick the credentials a live run uses, or ``None`` when there are none (tests skip).

    In order: ``ANTHROPIC_API_KEY``; ``CLAUDE_CODE_OAUTH_TOKEN`` (from ``claude setup-token``);
    the user's own login file, only with ``CSESSION_LIVE_COPY_LOGIN=1``. The copy is a risk the
    user must choose: an OAuth refresh inside the private home may rotate the refresh token and
    sign the real installation out. The real file is only read, never written.
    """
    for name in AUTH_VARS:
        value = environ.get(name)
        if value:
            return Auth({name: value}, None, name)
    if environ.get(COPY_LOGIN_ENV) == "1":
        source = real_home / ".claude" / ".credentials.json"
        if source.is_file() and not source.is_symlink():
            return Auth({}, source, "copied login file")
    return None


def build_env(
    environ: Mapping[str, str],
    *,
    root: Path,
    auth: Auth | None,
    extra: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> dict[str, str]:
    """The complete environment for a ``claude`` child, from the allowlist only; ``home``
    defaults to ``<root>/home``."""
    home = home or root / "home"
    env: dict[str, str] = {k: environ[k] for k in PASS_THROUGH if k in environ}
    env["PATH"] = environ.get("PATH", "/usr/bin:/bin")
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "TMPDIR": str(root / "tmp"),
            "GH_CONFIG_DIR": str(home / ".config" / "gh"),
            "GLAB_CONFIG_DIR": str(home / ".config" / "glab"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
    )
    if auth is not None:
        env.update(auth.env)
    for name, value in (extra or {}).items():
        if forbidden_extra(name):
            raise ValueError(f"a live test may not set {name}")
        env[name] = value
    return env


def claude_json(trusted: Iterable[Path], *, api_key: str | None = None) -> dict[str, Any]:
    """The private ``~/.claude.json``: onboarding done, the given directories trusted (an
    interactive session shows the trust dialog otherwise) and, with ``api_key``, that key
    approved (interactive Claude Code asks about a custom ``ANTHROPIC_API_KEY`` otherwise; it
    stores the key's last 20 characters). Test B2 passes no directory."""
    data: dict[str, Any] = {
        "hasCompletedOnboarding": True,
        "projects": {
            str(path): {"hasTrustDialogAccepted": True, "allowedTools": []} for path in trusted
        },
    }
    if api_key:
        data["customApiKeyResponses"] = {"approved": [api_key.strip()[-20:]], "rejected": []}
    return data


class Ledger:
    """Adds up the cost the runs report; ``exhausted`` once the cap is passed."""

    def __init__(self, cap_usd: float) -> None:
        self.cap_usd = cap_usd
        self.spent_usd = 0.0
        self.runs = 0

    def charge(self, usd: float) -> None:
        self.spent_usd += max(usd, 0.0)
        self.runs += 1

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.cap_usd


def env_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if not value > 0:
        raise ValueError(f"{name} must be positive, got {raw!r}")
    return value


# --- stream-json and hook logs -------------------------------------------------------------


def parse_stream(text: str) -> list[dict[str, Any]]:
    """The JSON objects of a ``stream-json`` output, one per non-blank line; a line that is
    not a JSON object is an error (the live tests assert on well-formed output)."""
    events: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"line {number} is not a JSON object")
        events.append(value)
    return events


def results(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [e for e in events if e.get("type") == "result"]


def init_event(events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    for event in events:
        if event.get("type") == "system" and event.get("subtype") == "init":
            return event
    raise AssertionError("no system/init event in the stream")


@dataclass(frozen=True)
class ToolCall:
    """One tool use of a ``stream-json`` run and the text of its result ("" if none came)."""

    name: str
    input: Mapping[str, Any]
    output: str
    is_error: bool


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def tool_calls(events: Sequence[Mapping[str, Any]]) -> list[ToolCall]:
    """The tool uses in a ``stream-json`` run (``--verbose``), in order, each with the result the
    tool returned: what the agent was actually given, not what it said about it."""
    uses: dict[str, tuple[str, Mapping[str, Any]]] = {}
    order: list[str] = []
    outputs: dict[str, tuple[str, bool]] = {}
    for event in events:
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            ident = str(block.get("id") or block.get("tool_use_id") or "")
            if not ident:  # cannot be paired: skipped rather than merged under one key
                continue
            if block.get("type") == "tool_use":
                raw = block.get("input")
                uses[ident] = (str(block.get("name")), raw if isinstance(raw, dict) else {})
                order.append(ident)
            elif block.get("type") == "tool_result":
                outputs[ident] = (_result_text(block.get("content")), bool(block.get("is_error")))
    return [ToolCall(uses[i][0], uses[i][1], *outputs.get(i, ("", False))) for i in order]


# The input fields that name what a tool call looked at.
PROBE_FIELDS = {"Read": ("file_path",), "Grep": ("path", "pattern"), "Glob": ("path", "pattern"),
                "Bash": ("command",)}  # fmt: skip


def probe_calls(calls: Sequence[ToolCall], target: str, *tools: str) -> list[ToolCall]:
    """The calls of ``tools`` whose input names ``target`` (Read: ``file_path``; Grep and Glob:
    ``path`` or ``pattern``; Bash: ``command``). A leak check runs on these, after asserting
    there is one: a check over calls that never probed the target passes vacuously."""
    return [
        c for c in calls
        if c.name in tools
        and any(target in str(c.input.get(key, "")) for key in PROBE_FIELDS.get(c.name, ()))
    ]  # fmt: skip


def _names_file(text: str, name: str) -> bool:
    """Whether ``text`` names the file ``name`` as a path component (``/w/link.txt``,
    ``link.txt``), not as part of a longer name (``unlink.txt.bak``)."""
    return re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w.-])", text) is not None


def leaked(calls: Sequence[ToolCall], marker: str, names: Sequence[str] = ()) -> list[ToolCall]:
    """The calls whose result holds ``marker``, or, for a result that is not an error, names
    one of the files ``names``: Grep's default ``files_with_matches`` mode lists only paths, and
    a path listed for a pattern found only in the secret means the content was read."""
    return [
        c for c in calls
        if marker in c.output or (not c.is_error and any(_names_file(c.output, n) for n in names))
    ]  # fmt: skip


def found(calls: Sequence[ToolCall], text: str, names: Sequence[str] = ()) -> bool:
    """A control: some call returned ``text``, or (a paths-only Grep) listed one of ``names``
    in a result that is not an error."""
    return bool(leaked(calls, text, names))


MISSING_FILE = re.compile(r"does not exist|no such file|enoent", re.IGNORECASE)


def missing_file(call: ToolCall) -> bool:
    """Whether the call failed because its target was not there (nothing was tested)."""
    return MISSING_FILE.search(call.output) is not None


PROC_LISTING = re.compile(r"ls(\s+--?[\w=-]+)*\s+/proc/?(\s+2>&1)?")


def is_proc_listing(command: str) -> bool:
    """Whether a Bash command is a plain listing of ``/proc`` (options and ``2>&1`` allowed)."""
    return PROC_LISTING.fullmatch(command.strip()) is not None


LONG_LISTING = re.compile(r"[-bcdlps][-rwxsStT]{9}")


def proc_entries(call: ToolCall) -> set[str] | None:
    """The PIDs a ``/proc`` listing showed, or ``None`` when the listing failed: an error, no
    output, or no ``self`` entry (every ``/proc`` has one). Reads every word of a short listing
    (one or several columns) and the name of each ``ls -l`` line."""
    words: list[str] = []
    for line in call.output.splitlines():
        if re.fullmatch(r"total \d+", line.strip()):  # the header of a long listing
            continue
        if LONG_LISTING.match(line):
            words += line.split(" -> ")[0].split()[-1:]
        else:
            words += line.split()
    if call.is_error or "self" not in words:
        return None
    return {w for w in words if w.isdigit()}


def total_cost(events: Sequence[Mapping[str, Any]]) -> float:
    """The cost of all ``result`` objects (one per turn with background tasks)."""
    return reported_cost(events) or 0.0


def reported_cost(events: Sequence[Mapping[str, Any]]) -> float | None:
    """The summed cost of the ``result`` objects, or ``None`` when none reports one (the cost
    is then unknown, and the ledger charges the estimate)."""
    costs = [
        float(value)
        for event in results(events)
        if isinstance(value := event.get("total_cost_usd"), int | float)
        and not isinstance(value, bool)
    ]
    return sum(costs) if costs else None


def cost_of_output(text: str) -> float | None:
    """The reported cost of a run's ``stream-json`` output; ``None`` when the output is not
    ``stream-json`` or reports no cost."""
    try:
        return reported_cost(parse_stream(text))
    except ValueError:  # json.JSONDecodeError included
        return None


def check_budget(args: Sequence[str]) -> None:
    """Refuse a ``-p`` argv without ``--max-budget-usd`` (only ``--print`` runs can take it)."""
    if ("-p" in args or "--print" in args) and "--max-budget-usd" not in args:
        raise ValueError("a -p run needs --max-budget-usd (LiveEnv.budget())")


def read_log(path: Path) -> list[dict[str, Any]]:
    """The records of a ``record_hook.py`` log, each with ``payload``: its stdin parsed as JSON
    (``None`` when it is not)."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        try:
            record["payload"] = json.loads(record.get("stdin", ""))
        except ValueError:
            record["payload"] = None
        records.append(record)
    return records


def events_named(records: Sequence[Mapping[str, Any]], event: str) -> list[Mapping[str, Any]]:
    """The payloads of the log records whose ``hook_event_name`` is ``event``."""
    out = []
    for record in records:
        payload = record.get("payload")
        if isinstance(payload, dict) and payload.get("hook_event_name") == event:
            out.append(payload)
    return out


def hook_group(command: str, *, matcher: str | None = None, timeout: int | None = None) -> dict:
    """One ``hooks`` group of a settings file, in Claude Code's layout."""
    entry: dict[str, Any] = {"type": "command", "command": command}
    if timeout is not None:
        entry["timeout"] = timeout
    group: dict[str, Any] = {"hooks": [entry]}
    if matcher is not None:
        group["matcher"] = matcher
    return group


def provider_of_error(text: str) -> str:
    """Classify the failure of a run that had no usable credentials: ``bedrock``, ``vertex``
    or ``anthropic`` (the first-party API's own authentication error), else ``unknown``.
    The patterns are guesses at Claude Code's messages; the test that uses this prints the
    text it saw (observations) so a wrong guess shows up as ``unknown`` with the text."""
    lowered = text.lower()
    if re.search(r"bedrock|\baws\b|amazon", lowered):
        return "bedrock"
    if re.search(r"vertex|gcp|google cloud|application default credentials", lowered):
        return "vertex"
    if re.search(r"invalid api key|authentication|not logged in|/login|x-api-key", lowered):
        return "anthropic"
    return "unknown"


# --- the live environment ------------------------------------------------------------------


class LiveSkip(pytest.skip.Exception):
    """The environment cannot run this live test (no claude, no login, cost cap reached):
    raised by a helper, it skips the test."""


@dataclass
class Headless:
    """A finished headless run."""

    proc: Result
    events: list[dict[str, Any]]

    @property
    def final(self) -> Mapping[str, Any]:
        found = results(self.events)
        assert found, f"no result object; stderr: {self.proc.stderr[:500]!r}"
        return found[-1]

    @property
    def session_id(self) -> str:
        value = self.final["session_id"]
        assert isinstance(value, str)
        return value

    @property
    def text(self) -> str:
        value = self.final.get("result")
        return value if isinstance(value, str) else ""


def _unreaped(child: Child) -> bool:
    """Whether ``child`` is our child not yet reaped (running or a zombie), so neither its PID
    nor the process group it leads can have been reused. Without ``os.waitid`` (macOS before
    Python 3.13) only a running child counts. Plain sys.platform and version checks (not
    hasattr) let pyright narrow ``os.waitid`` for each platform."""
    if sys.platform == "win32" or (sys.platform == "darwin" and sys.version_info < (3, 13)):
        return child.poll() is None
    try:
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return False
    return True


@dataclass
class Spawned:
    """A headless run started without waiting (``LiveEnv.spawn_headless``), in its own process
    group. ``returncode`` never reaps the leader, so teardown can still end its whole group."""

    child: Child = field(repr=False)
    out: Path
    err: Path

    @property
    def pid(self) -> int:
        return self.child.pid

    def returncode(self) -> int | None:
        """The exit code (negative signal number if killed), or ``None`` while running."""
        if sys.platform == "win32" or (sys.platform == "darwin" and sys.version_info < (3, 13)):
            return self.child.poll()  # no os.waitid (see _unreaped)
        try:
            info = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return self.child.poll()  # reaped already: the handle knows the code
        if info is None:
            return None
        return info.si_status if info.si_code == os.CLD_EXITED else -info.si_status

    def send_signal(self, sig: int) -> None:
        self.child.send_signal(sig)

    def end(self) -> None:
        """SIGKILL the run's process group while the leader is still our unreaped child (its
        group ID then names no other group), then reap the leader."""
        if _unreaped(self.child):
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.child.wait(10)


RECORD_PID = """\
import sys
from claude_session import gateway, procinfo
pid = int(sys.argv[1])
token = procinfo.ProcInfo(gateway.SystemGateway()).start_time(pid)
with open(sys.argv[2], "a", encoding="utf-8") as fh:
    fh.write(f"{pid} {token or ''}\\n")
"""


def read_pids(path: Path) -> list[tuple[int, str | None]]:
    """The ``(pid, start-time token)`` records a ``pid_record_command`` wrote."""
    if not path.exists():
        return []
    out: list[tuple[int, str | None]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        pid, _, token = line.strip().partition(" ")
        if pid.isdigit():
            out.append((int(pid), token or None))
    return out


@dataclass
class LiveEnv:
    root: Path
    claude: str
    model: str
    run_usd: float
    ledger: Ledger
    # Environment-holding fields stay out of the repr: pytest prints it on a failure.
    base_environ: Mapping[str, str] = field(repr=False)
    auth: Auth | None = field(repr=False)
    gw: gateway.SystemGateway = field(default_factory=gateway.SystemGateway, repr=False)
    spawned: list[Spawned] = field(default_factory=list, repr=False)
    pid_files: list[Path] = field(default_factory=list, repr=False)
    observations: list[tuple[str, Any]] = field(default_factory=list, repr=False)

    # layout
    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def noauth_home(self) -> Path:
        """The home of runs without credentials: never holds a copied login file."""
        return self.root / "home-noauth"

    @property
    def projects(self) -> Path:
        return self.home / ".claude" / "projects"

    def dir(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_json(self, relative: str, value: Any) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2), encoding="utf-8")
        return path

    def trust(self, *paths: Path) -> None:
        """Mark directories trusted in the private ``~/.claude.json`` (and approve the API key
        in use, so an interactive session asks nothing about it)."""
        api_key = self.auth.api_key if self.auth else None
        self.write_json("home/.claude.json", claude_json(paths, api_key=api_key))

    def env(self, extra: Mapping[str, str] | None = None, *, auth: bool = True) -> dict[str, str]:
        if auth:
            return build_env(self.base_environ, root=self.root, auth=self.auth, extra=extra)
        for name in (".config", ".claude", ".cache", ".local/share", ".local/state"):
            (self.noauth_home / name).mkdir(parents=True, exist_ok=True)
        return build_env(
            self.base_environ, root=self.root, auth=None, extra=extra, home=self.noauth_home
        )

    def observe(self, key: str, value: Any) -> None:
        """Record a measured value (an exit code, a payload shape) for the human reading the
        run: written to ``CSESSION_LIVE_OBSERVATIONS`` and printed in the terminal summary."""
        self.observations.append((key, value))

    # cost
    def budget(self) -> list[str]:
        """The ``--max-budget-usd`` flag every ``-p`` run carries."""
        return ["--max-budget-usd", f"{self.run_usd:g}"]

    def spend_gate(self) -> None:
        """Skip the test once the session's cost cap is reached; every path that starts a
        billed ``claude`` calls this first."""
        if self.ledger.exhausted:
            raise LiveSkip(f"live cost cap of {self.ledger.cap_usd} USD reached")

    def charge(self, cost: float | None) -> None:
        """Charge a run: its reported cost, or the per-run estimate when it is unknown."""
        self.ledger.charge(self.run_usd if cost is None else cost)

    # running claude
    def claude_run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        stdin: str = "",
        timeout: float = 300,
        extra_env: Mapping[str, str] | None = None,
        auth: bool = True,
    ) -> Result:
        """Run ``claude <args>`` to completion with the private environment (no flags added).
        A run with credentials is billed: gated, then charged its reported cost or the
        estimate. A ``-p`` argv must carry ``--max-budget-usd``."""
        check_budget(args)
        if auth:
            self.spend_gate()
        try:
            proc = self.gw.run(
                [self.claude, *args],
                env=self.env(extra_env, auth=auth),
                cwd=str(cwd or self.dir("work")),
                stdin=stdin,
                timeout=timeout,
                new_process_group=True,
                kill_grace=5,
            )
        except GatewayTimeout as exc:
            if auth:
                self.charge(None)
            raise AssertionError(f"claude timed out: {exc}") from exc
        if auth:
            self.charge(cost_of_output(proc.stdout))
        return proc

    def headless(
        self,
        prompt: str,
        *flags: str,
        cwd: Path | None = None,
        stdin: str = "",
        timeout: float = 300,
        extra_env: Mapping[str, str] | None = None,
    ) -> Headless:
        """A small-model ``-p`` run with ``stream-json`` output; the prompt follows ``--``."""
        argv = [
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            self.model,
            *self.budget(),
            *flags,
            "--",
            prompt,
        ]
        proc = self.claude_run(argv, cwd=cwd, stdin=stdin, timeout=timeout, extra_env=extra_env)
        return Headless(proc, parse_stream(proc.stdout))

    def spawn_headless(
        self, prompt: str, *flags: str, cwd: Path | None = None, out: Path, err: Path
    ) -> Spawned:
        """Start a headless run without waiting (signal tests), in its own process group;
        output goes to files. At teardown its group is killed and it is charged."""
        self.spend_gate()
        argv = [
            self.claude,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            self.model,
            *self.budget(),
            *flags,
            "--",
            prompt,
        ]
        with open(out, "wb") as fout, open(err, "wb") as ferr:
            child = self.gw.spawn(
                argv,
                env=self.env(),
                cwd=str(cwd or self.dir("work")),
                stdout=fout.fileno(),
                stderr=ferr.fileno(),
                new_process_group=True,
            )
        run = Spawned(child, out, err)
        self.spawned.append(run)
        return run

    def pid_record_command(self, pid_expr: str, path: Path) -> str:
        """A shell command a hook runs to record ``pid_expr`` (``$$``, ``$!``) with its
        start-time token into ``path``; teardown SIGKILLs each recorded process that still
        carries its token."""
        script = self.root / "record_pid.py"
        if not script.exists():
            script.write_text(RECORD_PID, encoding="utf-8")
        self.pid_files.append(path)
        return f"{shlex.quote(sys.executable)} -I {shlex.quote(str(script))} {pid_expr} " + (
            shlex.quote(str(path))
        )

    # teardown
    def end_runs(self) -> None:
        """End every spawned run (charging it) and kill the recorded hook processes. Each is
        handled once, so a later ``close`` neither ends nor charges it again."""
        while self.spawned:
            run = self.spawned.pop(0)
            run.end()
            try:
                text = run.out.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            self.charge(cost_of_output(text))
        while self.pid_files:
            for pid, token in read_pids(self.pid_files.pop(0)):
                signal_recorded(pid, token, signal.SIGKILL)

    def remove(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def close(self) -> None:
        try:
            self.end_runs()
        finally:
            self.remove()


def teardown(live: LiveEnv, login: LoginCache | None) -> None:
    """End a test's runs, then collect its login (a run still going could rotate the token
    after the snapshot), then remove its root; each step runs even if an earlier one failed."""
    try:
        live.end_runs()
    finally:
        try:
            if login is not None:
                login.collect(live.home)
        finally:
            live.remove()


def wait_for(predicate: Any, timeout: float, interval: float = 0.2) -> bool:
    """Poll ``predicate()`` until truthy or ``timeout`` seconds pass."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def signal_recorded(pid: int, start: str | None, sig: int) -> bool:
    """Send ``sig`` to ``pid`` only if it still carries the start-time token ``start`` recorded
    when its owner reported it. A process that is gone (also between the check and the send)
    is not an error. Returns whether the signal was delivered."""
    if not start:
        return False
    info = procinfo.ProcInfo(gateway.SystemGateway())
    if not info.is_alive(pid, start):
        return False
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return False
    return True


def kill_recorded(pid: int, start: str | None) -> bool:
    """SIGKILL ``pid`` only while it carries the recorded start-time token."""
    return signal_recorded(pid, start, signal.SIGKILL)


def runs_executable(pid: int, path: str) -> bool:
    """Whether the process ``pid`` runs the program ``path`` (Linux: by the realpath of
    ``/proc/<pid>/exe``, or of the script an interpreter runs, its first argument)."""
    want = os.path.realpath(path)
    try:
        if os.path.realpath(f"/proc/{pid}/exe") == want:
            return True
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return len(argv) > 1 and os.path.realpath(argv[1].decode(errors="replace")) == want


def gone(pid: int) -> bool:
    """Whether no process ``pid`` exists (portable: ``kill(pid, 0)``)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def new_root() -> Path:
    """A fresh private root: short and directly in ``/tmp`` (unix socket paths are limited),
    mode 0700, with the directories a ``claude`` run expects."""
    root = Path(tempfile.mkdtemp(prefix="cs-live-", dir="/tmp"))
    root.chmod(0o700)
    for name in (
        "home/.config",
        "home/.claude",
        "home/.cache",
        "home/.local/share",
        "home/.local/state",
        "tmp",
    ):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


CREDENTIALS = Path(".claude") / ".credentials.json"


class LoginCache:
    """The one copy of the user's login file a pytest session uses (``CSESSION_LIVE_COPY_LOGIN``).

    It is copied from the real home once, into a private directory of its own; each test that
    needs credentials gets it installed into its private home, and what that test's ``claude``
    left there (a refreshed token) is collected back into this copy, so the next test starts
    from the newest one instead of each test refreshing the original independently. Nothing is
    ever written to the real home."""

    def __init__(self, source: Path, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "credentials.json"
        self._write(source.read_bytes())

    def _write(self, data: bytes) -> None:
        tmp = self.directory / ".credentials.tmp"
        tmp.write_bytes(data)
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def install(self, home: Path) -> None:
        target = home / CREDENTIALS
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.path.read_bytes())
        target.chmod(0o600)

    def collect(self, home: Path) -> None:
        target = home / CREDENTIALS
        if target.is_symlink() or not target.is_file():
            return
        data = target.read_bytes()
        if data and data != self.path.read_bytes():
            self._write(data)

    def remove(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def new_login_cache(source: Path) -> LoginCache:
    directory = Path(tempfile.mkdtemp(prefix="cs-login-", dir="/tmp"))
    directory.chmod(0o700)
    return LoginCache(source, directory)


# --- transcripts -----------------------------------------------------------------------------


def find_transcripts(projects: Path, session_id: str) -> list[Path]:
    """The main transcript files named ``<session_id>.jsonl`` directly in a project directory."""
    return sorted(projects.glob(f"*/{session_id}.jsonl"))


def transcript_records(path: Path) -> list[dict[str, Any]]:
    """The records of a transcript; every line must be a JSON object."""
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        assert isinstance(value, dict), f"not an object: {line[:80]!r}"
        records.append(value)
    return records


def content_blocks(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The content blocks of a ``user``/``assistant`` record (a string is one text block)."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def tool_names(payloads: Iterable[Mapping[str, Any]]) -> set[str]:
    return {str(p.get("tool_name")) for p in payloads}


def transcript_text(projects: Path, session_id: str) -> str:
    """Everything in the session's main transcript, as one string (for "never appears" checks)."""
    files = find_transcripts(projects, session_id)
    assert len(files) == 1, files
    return files[0].read_text(encoding="utf-8")


def tool_result_text(records: Iterable[Mapping[str, Any]]) -> str:
    """The text of every ``tool_result`` block of a transcript, one block per line: what the
    tools returned, never the prompt or the model's own words."""
    parts: list[str] = []
    for record in records:
        for block in content_blocks(record):
            if block.get("type") != "tool_result":
                continue
            content = block.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(
                    str(item.get("text", ""))
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                )
    return "\n".join(parts)


def session_tool_results(projects: Path, session_id: str) -> str:
    """``tool_result_text`` of the session's main transcript."""
    files = find_transcripts(projects, session_id)
    assert len(files) == 1, files
    return tool_result_text(transcript_records(files[0]))


def tool_uses_by_message(records: Iterable[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """The ``tool_use`` blocks of each assistant message, in order. Claude Code may write one
    message as several records (one per block) sharing ``message.id``; they are joined."""
    order: list[str] = []
    by_id: dict[str, list[Mapping[str, Any]]] = {}
    for index, record in enumerate(records):
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        key = message.get("id") if isinstance(message, dict) else None
        key = str(key) if key else f"#{index}"
        uses = [b for b in content_blocks(record) if b.get("type") == "tool_use"]
        if key not in by_id:
            order.append(key)
            by_id[key] = []
        by_id[key].extend(uses)
    return [by_id[key] for key in order if by_id[key]]


def sandbox_available() -> bool:
    """Whether the sandbox can run here: bubblewrap and socat on Linux, always on macOS."""
    if sys.platform == "darwin":
        return True
    return shutil.which("bwrap") is not None and shutil.which("socat") is not None
