"""A fake Claude Code CLI driven by a scenario file (design.md, Testing).

``tests/fakebin/claude`` runs ``main``. The fake parses the ``claude`` argv claude-session
builds (design.md, Generated settings: argv), reads the hooks from the generated ``--settings``
and plays a scenario: it runs the injected hook commands with Claude Code's payloads, runs
``claude-session report`` the way an agent's Bash tool would, prints ``stream-json`` lines and
exits, hangs or ignores signals on cue. Standard library only.

**Finding the scenario.** ``$FAKE_CLAUDE_SCENARIO`` names a JSON scenario file;
``$FAKE_CLAUDE_LOG`` (or the scenario's ``log``) names a JSON-lines file the fake appends
what it did to. ``--version`` needs no scenario (``$FAKE_CLAUDE_VERSION`` or the scenario's
``version`` overrides the printed version). Without a scenario any other call fails.

**Scenario** (unknown keys and step types are refused, so a typo fails loudly)::

    {
      "session_id": "<uuid>",            # default: --session-id, else --resume's id, else new
      "transcript_path": "<abs path>",   # default: Claude Code's layout under $HOME (not created)
      "report_command": "claude-session report",  # the agent's command prefix (shell text)
      "log": "<abs path>",
      "version": "2.1.312 (Claude Code)",
      "signals": {"TERM": {"exit": 143, "session_end": "other"}, "HUP": "ignore"},
      "steps": [ {"do": "<step>", ...}, ... ]
    }

**Hook steps** fire every matching hook of one event and take, besides their own keys,
``payload`` (fields merged over the built payload, for hostile-input tests), ``omit`` (field
names dropped), ``raw_payload`` (text sent on stdin instead of JSON) and ``on_block`` (steps
run when a hook blocked). Payload fields are only those design.md and interface.md document:

- ``session_start``: ``source`` (``startup``, ``resume``, ``fork``, ``clear``, ``compact``;
  default from argv), ``session_id`` (``clear`` defaults to a new id, which later steps use) →
  ``session_id``, ``transcript_path``, ``source``.
- ``user_prompt_submit`` → ``session_id``.
- ``permission_request``: ``tool_name``, ``tool_input`` → the same plus ``session_id``.
- ``notification``: ``notification_type`` (``permission_prompt``, ``idle_prompt``,
  ``elicitation_dialog``) → the same plus ``session_id``.
- ``post_tool_use``: ``tool_name``, ``tool_input``, ``tool_response``, optional ``agent_id``
  and ``agent_type`` (a subagent's tool; ``session_id`` stays the parent's, live A2).
- ``stop``: ``background_tasks`` (list, default empty) → ``session_id``, ``stop_hook_active``
  (true on the Stop after a blocked Stop), ``background_tasks``.
- ``session_end``: ``reason`` → ``session_id``, ``reason``.

**Agent steps**: ``bash`` (``command``; runs it with ``/bin/sh -c`` in the cwd with this
process's environment, then fires ``PostToolUse`` for ``Bash`` with ``tool_input``
``{"command"}`` and ``tool_response`` ``{"stdout", "stderr"}``; ``post_tool_use``:
``on_success`` (default: a failed command fires no ``PostToolUse``), ``always`` or ``never``;
optional ``agent_id``/``agent_type``), ``report`` (``args``, the arguments after the report
command, and optional ``heredoc`` text fed through ``<<'EOF'``; otherwise as ``bash``) and
``write_file`` (``path``, ``content``; the Write tool's effect only, no hook).

**Output and process steps**: ``result`` (one ``stream-json`` ``result`` object; keys
``subtype``, ``is_error``, ``result``, ``total_cost_usd``, ``num_turns``, ``duration_ms``,
``terminal_reason``, ``permission_denials``, ``session_id``), ``emit`` (``line``: text or a JSON
object; ``newline``, default true), ``truncated_result`` (the first ``chars`` characters of a
serialized ``result`` object, no newline), ``stderr`` (``text``), ``sleep`` (``seconds``),
``touch`` (``path``: an empty marker file, for tests to synchronize on), ``ignore_signals``
(``signals``: names such as ``TERM``), ``hang`` and ``exit`` (``code``). The fake exits 0
after the last step.

**Hooks** run as Claude Code runs them (design.md, Spike results): ``/bin/sh -c <command>`` in
the cwd, with this process's environment and the payload on stdin, sequentially in settings
order, each in its own process group. At its ``timeout`` (default 60 s) the group gets SIGTERM,
then SIGKILL after a short grace, and the output is dropped (live A10). A hook blocks with exit
2 (stderr is the reason) or exit 0 and a JSON object with ``"decision": "block"``.
``hookSpecificOutput.additionalContext`` and plain ``SessionStart`` stdout are recorded as
context. Matchers apply to ``SessionStart`` (source), ``PermissionRequest`` and ``PostToolUse``
(tool name), ``Notification`` (type) and ``SessionEnd`` (reason); absent, empty or ``*`` match
everything, anything else must match the whole value as a regular expression. A non-catch-all
matcher on an event without a matched field never fires, so a mis-generated matcher shows.

**Signals.** By default SIGTERM fires ``SessionEnd`` with reason ``other`` and exits 143, SIGHUP
exits 129 (spike: auto-exit by signal) and SIGINT exits 130, each ending a running hook first.
A signal arriving while the fake already unwinds for one is only logged. ``"ignore"`` (in
``signals`` or through ``ignore_signals``) installs a handler that only logs, never ``SIG_IGN``,
so hook children do not inherit the ignored signal.

**Log records** (``"event"`` key): ``start``, ``step``, ``hook``, ``hook_skipped``,
``block``, ``bash``, ``signal``, ``signal_ignored``, ``exit``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

SCENARIO_ENV = "FAKE_CLAUDE_SCENARIO"
LOG_ENV = "FAKE_CLAUDE_LOG"
VERSION_ENV = "FAKE_CLAUDE_VERSION"
DEFAULT_VERSION = "2.1.312 (Claude Code)"
DEFAULT_REPORT_COMMAND = "claude-session report"
DEFAULT_HOOK_TIMEOUT = 60.0
HOOK_KILL_GRACE = 1.0
# Exit code for a broken scenario or a fake-internal error, distinct from anything claude uses.
FAKE_ERROR = 99
# Exit code of a real claude for an argv it rejects.
ARGV_ERROR = 1

# The flags claude-session passes (design.md, Generated settings: argv); anything else is an
# argv bug in the code under test and is rejected, as the real claude rejects unknown options.
VALUE_FLAGS = {
    "--settings",
    "--setting-sources",
    "--model",
    "--add-dir",
    "--plugin-dir",
    "--append-system-prompt-file",
    "--system-prompt-snapshot",
    "--resume",
    "--session-id",
    "--output-format",
    "--max-turns",
}
REPEATABLE_FLAGS = {"--add-dir", "--plugin-dir"}
BOOL_FLAGS = {"-p", "--print", "--fork-session", "--strict-mcp-config", "--verbose", "--debug"}
OUTPUT_FORMATS = {"text", "json", "stream-json"}

SOURCES = {"startup", "resume", "fork", "clear", "compact"}
NOTIFICATION_TYPES = {"permission_prompt", "idle_prompt", "elicitation_dialog"}
SIGNALS = {"TERM": signal.SIGTERM, "HUP": signal.SIGHUP, "INT": signal.SIGINT}
DEFAULT_SIGNALS: dict[str, Any] = {
    "TERM": {"exit": 128 + signal.SIGTERM, "session_end": "other"},
    "HUP": {"exit": 128 + signal.SIGHUP},
    "INT": {"exit": 128 + signal.SIGINT},
}
HEREDOC_DELIMITER = "EOF"

SCENARIO_KEYS = {
    "session_id",
    "transcript_path",
    "report_command",
    "log",
    "version",
    "signals",
    "steps",
}
HOOK_STEP_KEYS = {"payload", "omit", "raw_payload", "on_block"}
STEP_KEYS: dict[str, set[str]] = {
    "session_start": {"source", "session_id", "transcript_path"},
    "user_prompt_submit": set(),
    "permission_request": {"tool_name", "tool_input"},
    "notification": {"notification_type"},
    "post_tool_use": {"tool_name", "tool_input", "tool_response", "agent_id", "agent_type"},
    "stop": {"background_tasks"},
    "session_end": {"reason"},
    "bash": {"command", "post_tool_use", "agent_id", "agent_type", "tool_response"},
    "report": {"args", "heredoc", "post_tool_use", "agent_id", "agent_type", "tool_response"},
    "write_file": {"path", "content"},
    "result": {
        "subtype",
        "is_error",
        "result",
        "total_cost_usd",
        "num_turns",
        "duration_ms",
        "terminal_reason",
        "permission_denials",
        "session_id",
    },
    "emit": {"line", "newline"},
    "truncated_result": {"chars", "result", "session_id"},
    "stderr": {"text"},
    "sleep": {"seconds"},
    "touch": {"path"},
    "ignore_signals": {"signals"},
    "hang": set(),
    "exit": {"code"},
}
HOOK_STEPS = {
    "session_start",
    "user_prompt_submit",
    "permission_request",
    "notification",
    "post_tool_use",
    "stop",
    "session_end",
    "bash",
    "report",
}
POST_TOOL_USE_MODES = {"on_success", "always", "never"}


class FakeError(Exception):
    """A broken scenario, settings file or fake-internal failure: exit ``FAKE_ERROR``."""


class ArgvError(Exception):
    """An argv the real claude would reject: exit ``ARGV_ERROR``."""


class Signalled(BaseException):
    """Raised from a signal handler to unwind the scenario (a ``BaseException`` so no
    ``except Exception`` in the steps can swallow it)."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


# --- argv ----------------------------------------------------------------------------------


@dataclass
class Argv:
    flags: dict[str, Any] = field(default_factory=dict)
    prompt: str | None = None
    version: bool = False

    @property
    def headless(self) -> bool:
        return bool(self.flags.get("-p") or self.flags.get("--print"))


def parse_argv(args: list[str]) -> Argv:
    """Parse claude's argv strictly: unknown options, a missing value, a repeated single-use
    flag or more than one prompt are rejected, as are the combinations the spike found
    invalid."""
    parsed = Argv()
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--version" or arg == "-v":
            parsed.version = True
        elif arg in VALUE_FLAGS:
            if i + 1 >= len(args):
                raise ArgvError(f"option '{arg}' argument missing")
            value = args[i + 1]
            i += 1
            if arg in REPEATABLE_FLAGS:
                parsed.flags.setdefault(arg, []).append(value)
            elif arg in parsed.flags:
                raise ArgvError(f"option '{arg}' given more than once")
            else:
                parsed.flags[arg] = value
        elif arg in BOOL_FLAGS:
            parsed.flags[arg] = True
        elif arg.startswith("-") and arg != "-":
            raise ArgvError(f"unknown option '{arg}'")
        elif parsed.prompt is not None:
            raise ArgvError(f"too many arguments: '{arg}'")
        else:
            parsed.prompt = arg
        i += 1
    if parsed.version:
        return parsed
    _check_combinations(parsed)
    return parsed


def _check_combinations(parsed: Argv) -> None:
    flags = parsed.flags
    fmt = flags.get("--output-format")
    if fmt is not None and fmt not in OUTPUT_FORMATS:
        raise ArgvError(f"invalid --output-format '{fmt}'")
    if fmt is not None and not parsed.headless:
        raise ArgvError("--output-format only works with --print")
    if fmt == "stream-json" and not flags.get("--verbose"):
        raise ArgvError("--output-format=stream-json with --print requires --verbose")
    if flags.get("--fork-session") and "--resume" not in flags:
        raise ArgvError("--fork-session requires --resume")
    # Spike: --session-id with --resume is rejected unless --fork-session is given.
    if "--session-id" in flags and "--resume" in flags and not flags.get("--fork-session"):
        raise ArgvError("--session-id can only be used with --resume if --fork-session is set")
    for name in ("--session-id", "--resume"):
        if name in flags and not _is_uuid(flags[name]):
            raise ArgvError(f"{name} must be a valid UUID")
    turns = flags.get("--max-turns")
    if turns is not None and not (turns.isdigit() and int(turns) > 0):
        raise ArgvError(f"invalid --max-turns '{turns}'")


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


# --- settings ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Hook:
    matcher: str | None
    command: str
    timeout: float


def load_settings(value: str | None) -> dict[str, Any]:
    """The ``--settings`` value: inline JSON (starting with ``{``) or a file path."""
    if value is None:
        return {}
    try:
        text = value
        if not value.lstrip().startswith("{"):
            with open(value, encoding="utf-8") as fh:
                text = fh.read()
        settings = json.loads(text)
    except (OSError, ValueError) as exc:
        raise FakeError(f"cannot read --settings: {exc}") from exc
    if not isinstance(settings, dict):
        raise FakeError("--settings is not a JSON object")
    return settings


def parse_hooks(settings: dict[str, Any]) -> dict[str, list[Hook]]:
    """Claude Code's hook layout: ``hooks.<Event>`` is a list of ``{matcher?, hooks: [{type:
    command, command, timeout?}]}``. Anything else is refused."""
    raw = settings.get("hooks", {})
    if not isinstance(raw, dict):
        raise FakeError("settings 'hooks' is not an object")
    hooks: dict[str, list[Hook]] = {}
    for event, groups in raw.items():
        if not isinstance(groups, list):
            raise FakeError(f"hooks.{event} is not a list")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise FakeError(f"hooks.{event}: each entry needs a 'hooks' list")
            matcher = group.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                raise FakeError(f"hooks.{event}: matcher must be a string")
            for entry in group["hooks"]:
                hooks.setdefault(event, []).append(_parse_hook(event, matcher, entry))
    return hooks


def _parse_hook(event: str, matcher: str | None, entry: object) -> Hook:
    if not isinstance(entry, dict) or entry.get("type") != "command":
        raise FakeError(f"hooks.{event}: only type 'command' hooks are supported")
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        raise FakeError(f"hooks.{event}: command must be a non-empty string")
    timeout = entry.get("timeout", DEFAULT_HOOK_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise FakeError(f"hooks.{event}: timeout must be a positive number")
    return Hook(matcher, command, float(timeout))


def matches(matcher: str | None, value: str | None) -> bool:
    """Claude Code's matcher rule: absent, empty or ``*`` matches everything; otherwise the
    whole value must match the matcher as a regular expression (a plain name matches only
    itself). Events without a matched field match only the catch-all forms."""
    if matcher is None or matcher in ("", "*"):
        return True
    if value is None:
        return False
    try:
        return re.fullmatch(matcher, value) is not None
    except re.error as exc:
        raise FakeError(f"invalid hook matcher {matcher!r}: {exc}") from exc


# The payload field a hook event's matcher is compared with.
MATCH_FIELD = {
    "SessionStart": "source",
    "PermissionRequest": "tool_name",
    "Notification": "notification_type",
    "PostToolUse": "tool_name",
    "SessionEnd": "reason",
}


# --- scenario ------------------------------------------------------------------------------


def load_scenario(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            scenario = json.load(fh)
    except (OSError, ValueError) as exc:
        raise FakeError(f"cannot read scenario {path}: {exc}") from exc
    if not isinstance(scenario, dict):
        raise FakeError("scenario is not a JSON object")
    unknown = set(scenario) - SCENARIO_KEYS
    if unknown:
        raise FakeError(f"unknown scenario keys: {sorted(unknown)}")
    steps = scenario.get("steps", [])
    validate_steps(steps, "steps")
    _validate_signals(scenario.get("signals", {}))
    for key in ("session_id", "transcript_path", "report_command", "log", "version"):
        if key in scenario and not isinstance(scenario[key], str):
            raise FakeError(f"scenario {key} must be a string")
    return scenario


def validate_steps(steps: object, where: str) -> None:
    if not isinstance(steps, list):
        raise FakeError(f"{where} must be a list")
    for n, step in enumerate(steps):
        here = f"{where}[{n}]"
        if not isinstance(step, dict) or not isinstance(step.get("do"), str):
            raise FakeError(f"{here} needs a 'do' string")
        kind = step["do"]
        if kind not in STEP_KEYS:
            raise FakeError(f"{here}: unknown step {kind!r}")
        allowed = STEP_KEYS[kind] | {"do"} | (HOOK_STEP_KEYS if kind in HOOK_STEPS else set())
        unknown = set(step) - allowed
        if unknown:
            raise FakeError(f"{here} ({kind}): unknown keys {sorted(unknown)}")
        _validate_step(kind, step, here)
        if "on_block" in step:
            validate_steps(step["on_block"], f"{here}.on_block")


def _validate_step(kind: str, step: dict[str, Any], here: str) -> None:
    def need(key: str, typ: type | tuple[type, ...], required: bool = True) -> None:
        if key not in step:
            if required:
                raise FakeError(f"{here} ({kind}): '{key}' is required")
            return
        value = step[key]
        if isinstance(value, bool) and bool not in (typ if isinstance(typ, tuple) else (typ,)):
            raise FakeError(f"{here} ({kind}): '{key}' has the wrong type")
        if not isinstance(value, typ):
            raise FakeError(f"{here} ({kind}): '{key}' has the wrong type")

    if "payload" in step:
        need("payload", dict)
    if "omit" in step:
        need("omit", list)
    if "raw_payload" in step:
        need("raw_payload", str)
    if kind == "session_start":
        need("source", str, required=False)
        if "source" in step and step["source"] not in SOURCES:
            raise FakeError(f"{here}: source must be one of {sorted(SOURCES)}")
        need("session_id", str, required=False)
        need("transcript_path", str, required=False)
    elif kind == "permission_request":
        need("tool_name", str)
        need("tool_input", dict, required=False)
    elif kind == "notification":
        need("notification_type", str)
        if step["notification_type"] not in NOTIFICATION_TYPES:
            raise FakeError(f"{here}: notification_type must be one of {NOTIFICATION_TYPES}")
    elif kind == "post_tool_use":
        need("tool_name", str)
        need("tool_input", dict, required=False)
        need("tool_response", (dict, str), required=False)
        need("agent_id", str, required=False)
        need("agent_type", str, required=False)
    elif kind == "stop":
        need("background_tasks", list, required=False)
    elif kind == "session_end":
        need("reason", str)
    elif kind in ("bash", "report"):
        if kind == "bash":
            need("command", str)
        else:
            need("args", list)
            if not all(isinstance(a, str) for a in step["args"]):
                raise FakeError(f"{here}: args must be strings")
            need("heredoc", str, required=False)
            if HEREDOC_DELIMITER in step.get("heredoc", "").splitlines():
                raise FakeError(f"{here}: heredoc text contains the line {HEREDOC_DELIMITER}")
        if step.get("post_tool_use", "on_success") not in POST_TOOL_USE_MODES:
            raise FakeError(f"{here}: post_tool_use must be one of {POST_TOOL_USE_MODES}")
        need("agent_id", str, required=False)
        need("agent_type", str, required=False)
        need("tool_response", (dict, str), required=False)
    elif kind == "write_file":
        need("path", str)
        need("content", str)
    elif kind == "result":
        need("subtype", str, required=False)
        need("is_error", bool, required=False)
        need("result", str, required=False)
        need("total_cost_usd", (int, float), required=False)
        need("num_turns", int, required=False)
        need("duration_ms", int, required=False)
        need("terminal_reason", str, required=False)
        need("permission_denials", list, required=False)
        need("session_id", str, required=False)
    elif kind == "emit":
        need("line", (str, dict))
        need("newline", bool, required=False)
    elif kind == "truncated_result":
        need("chars", int)
        if step["chars"] < 0:
            raise FakeError(f"{here}: chars must not be negative")
        need("result", str, required=False)
        need("session_id", str, required=False)
    elif kind == "stderr":
        need("text", str)
    elif kind == "sleep":
        need("seconds", (int, float))
        if step["seconds"] < 0:
            raise FakeError(f"{here}: seconds must not be negative")
    elif kind == "touch":
        need("path", str)
    elif kind == "ignore_signals":
        need("signals", list)
        for name in step["signals"]:
            if name not in SIGNALS:
                raise FakeError(f"{here}: unknown signal {name!r}")
    elif kind == "exit":
        need("code", int)
        if not 0 <= step["code"] <= 255:
            raise FakeError(f"{here}: code must be 0..255")


def _validate_signals(signals: object) -> None:
    if not isinstance(signals, dict):
        raise FakeError("scenario signals must be an object")
    for name, action in signals.items():
        if name not in SIGNALS:
            raise FakeError(f"signals: unknown signal {name!r}")
        if action == "ignore":
            continue
        if not isinstance(action, dict) or set(action) - {"exit", "session_end"}:
            raise FakeError(f"signals.{name}: use 'ignore' or {{exit, session_end}}")
        code = action.get("exit")
        if isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 255:
            raise FakeError(f"signals.{name}: exit must be 0..255")
        end = action.get("session_end")
        if end is not None and not isinstance(end, str):
            raise FakeError(f"signals.{name}: session_end must be a string")


# --- the run -------------------------------------------------------------------------------


@dataclass
class HookOutcome:
    """What the hooks of one event did, as Claude Code reads them."""

    blocked: bool = False
    reasons: list[str] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)


class Fake:
    def __init__(
        self, argv: Argv, scenario: dict[str, Any], hooks: dict[str, list[Hook]], log: str | None
    ) -> None:
        self.argv = argv
        self.scenario = scenario
        self.hooks = hooks
        self.log_path = log
        self.cwd = os.getcwd()
        self.session_id = scenario.get("session_id") or self._default_session_id()
        self.transcript_path = scenario.get("transcript_path") or self._transcript_path()
        self.report_command = scenario.get("report_command", DEFAULT_REPORT_COMMAND)
        self.stop_hook_active = False
        self.signals: dict[str, Any] = {**DEFAULT_SIGNALS, **scenario.get("signals", {})}
        self.handling_signal = False

    def _default_session_id(self) -> str:
        flags = self.argv.flags
        if "--session-id" in flags:
            return flags["--session-id"]
        if "--resume" in flags and not flags.get("--fork-session"):
            return flags["--resume"]
        return str(uuid.uuid4())

    def _transcript_path(self) -> str:
        # Claude Code's layout (design.md, Transcripts); the fake never creates the file.
        home = os.environ.get("HOME", "/nonexistent")
        project = re.sub(r"[^A-Za-z0-9]", "-", self.cwd)
        return os.path.join(home, ".claude", "projects", project, f"{self.session_id}.jsonl")

    def default_source(self) -> str:
        flags = self.argv.flags
        if flags.get("--fork-session"):
            return "fork"
        return "resume" if "--resume" in flags else "startup"

    # -- log and output --

    def log(self, event: str, **fields: Any) -> None:
        if self.log_path is None:
            return
        record = {"event": event, "ts": time.time(), "pid": os.getpid(), **fields}
        line = json.dumps(record, ensure_ascii=False) + "\n"
        fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)

    def stdout(self, text: str) -> None:
        with contextlib.suppress(BrokenPipeError, ValueError):
            sys.stdout.write(text)
            sys.stdout.flush()

    # -- signals --

    def install_signals(self) -> None:
        for name in SIGNALS:
            self._install(name, self.signals.get(name))

    def _install(self, name: str, action: Any) -> None:
        signum = SIGNALS[name]
        if action == "ignore":
            signal.signal(signum, lambda _s, _f: self.log("signal_ignored", signal=name))
        else:
            signal.signal(signum, lambda _s, _f: self._on_signal(name))

    def ignore(self, names: list[str]) -> None:
        for name in names:
            self.signals[name] = "ignore"
            self._install(name, "ignore")

    def _on_signal(self, name: str) -> None:
        self.log("signal", signal=name)
        if self.handling_signal:
            return  # already unwinding for an earlier signal
        self.handling_signal = True
        raise Signalled(name)

    def finish_signal(self, name: str) -> int:
        action = self.signals[name]
        end = action.get("session_end")
        if end is not None:
            self.fire("SessionEnd", {"session_id": self.session_id, "reason": end}, {})
        return action["exit"]

    # -- hooks --

    def fire(self, event: str, payload: dict[str, Any], step: dict[str, Any]) -> HookOutcome:
        """Run every hook of ``event`` whose matcher matches, as Claude Code does."""
        for name in step.get("omit", []):
            payload.pop(name, None)
        payload.update(step.get("payload", {}))
        stdin = step.get("raw_payload")
        if stdin is None:
            stdin = json.dumps(payload, ensure_ascii=False)
        field_name = MATCH_FIELD.get(event)
        value = payload.get(field_name) if field_name else None
        outcome = HookOutcome()
        for hook in self.hooks.get(event, []):
            if field_name is None and hook.matcher not in (None, "", "*"):
                self.log("hook_skipped", hook_event=event, command=hook.command)
                continue
            if not matches(hook.matcher, value if isinstance(value, str) else None):
                self.log("hook_skipped", hook_event=event, command=hook.command)
                continue
            self._run_hook(event, hook, stdin, outcome)
        if outcome.blocked:
            self.log("block", hook_event=event, reasons=outcome.reasons)
        return outcome

    def _run_hook(self, event: str, hook: Hook, stdin: str, outcome: HookOutcome) -> None:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", hook.command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            process_group=0,
        )
        timed_out = False
        try:
            try:
                out, err = proc.communicate(stdin.encode("utf-8"), timeout=hook.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                # The leader stays unreaped until the SIGKILL, so the group ID cannot be
                # reused: ``sh`` dies of SIGTERM while a child of it may ignore it.
                _signal_group(proc, signal.SIGTERM)
                time.sleep(HOOK_KILL_GRACE)
                _signal_group(proc, signal.SIGKILL)
                out, err = proc.communicate()
        except BaseException:
            _signal_group(proc, signal.SIGKILL)
            proc.wait()
            raise
        record: dict[str, Any] = {
            "hook_event": event,
            "command": hook.command,
            "payload": stdin,
            "exit": proc.returncode,
            "timed_out": timed_out,
        }
        if timed_out:
            self.log("hook", **record)  # Claude Code drops a canceled hook's output (live A10)
            return
        stdout = out.decode("utf-8", "replace")
        stderr = err.decode("utf-8", "replace")
        record.update(stdout=stdout, stderr=stderr)
        self.log("hook", **record)
        self._read_output(event, proc.returncode, stdout, stderr, outcome)

    def _read_output(
        self, event: str, code: int, stdout: str, stderr: str, outcome: HookOutcome
    ) -> None:
        if code == 2:
            outcome.blocked = True
            outcome.reasons.append(stderr.strip())
            return
        if code != 0:
            return  # a non-blocking error: Claude Code shows it and continues
        try:
            parsed = json.loads(stdout) if stdout.strip() else None
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            if parsed.get("decision") == "block":
                outcome.blocked = True
                reason = parsed.get("reason")
                outcome.reasons.append(reason if isinstance(reason, str) else "")
            specific = parsed.get("hookSpecificOutput")
            if isinstance(specific, dict) and isinstance(specific.get("additionalContext"), str):
                outcome.contexts.append(specific["additionalContext"])
        elif event == "SessionStart" and stdout.strip():
            outcome.contexts.append(stdout)  # plain stdout reaches the model too (spike)

    # -- steps --

    def run_steps(self, steps: list[dict[str, Any]]) -> int | None:
        """Run ``steps``; the exit code once an ``exit`` step ran, else ``None``."""
        for step in steps:
            self.log("step", step=step)
            code = self.run_step(step)
            if code is not None:
                return code
        return None

    def run_step(self, step: dict[str, Any]) -> int | None:
        kind = step["do"]
        handler: Callable[[dict[str, Any]], HookOutcome | int | None] = getattr(self, f"do_{kind}")
        result = handler(step)
        if isinstance(result, HookOutcome):
            if result.blocked and "on_block" in step:
                return self.run_steps(step["on_block"])
            return None
        return result

    def do_session_start(self, step: dict[str, Any]) -> HookOutcome:
        source = step.get("source", self.default_source())
        if "session_id" in step:
            self.session_id = step["session_id"]
        elif source == "clear":
            self.session_id = str(uuid.uuid4())  # /clear changes the id (spike)
        if "transcript_path" in step:
            self.transcript_path = step["transcript_path"]
        elif source == "clear" or "session_id" in step:
            self.transcript_path = self._transcript_path()
        payload = {
            "session_id": self.session_id,
            "transcript_path": self.transcript_path,
            "source": source,
        }
        outcome = self.fire("SessionStart", payload, step)
        if outcome.contexts:
            self.log("context", contexts=outcome.contexts)
        return outcome

    def do_user_prompt_submit(self, step: dict[str, Any]) -> HookOutcome:
        self.stop_hook_active = False
        return self.fire("UserPromptSubmit", {"session_id": self.session_id}, step)

    def do_permission_request(self, step: dict[str, Any]) -> HookOutcome:
        payload = {
            "session_id": self.session_id,
            "tool_name": step["tool_name"],
            "tool_input": step.get("tool_input", {}),
        }
        return self.fire("PermissionRequest", payload, step)

    def do_notification(self, step: dict[str, Any]) -> HookOutcome:
        payload = {"session_id": self.session_id, "notification_type": step["notification_type"]}
        return self.fire("Notification", payload, step)

    def do_post_tool_use(self, step: dict[str, Any]) -> HookOutcome:
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "tool_name": step["tool_name"],
            "tool_input": step.get("tool_input", {}),
            "tool_response": step.get("tool_response", {}),
        }
        _add_agent(payload, step)
        return self.fire("PostToolUse", payload, step)

    def do_stop(self, step: dict[str, Any]) -> HookOutcome:
        payload = {
            "session_id": self.session_id,
            "stop_hook_active": self.stop_hook_active,
            "background_tasks": step.get("background_tasks", []),
        }
        outcome = self.fire("Stop", payload, step)
        # The Stop after a blocked one reports stop_hook_active (spike: Stop-hook blocking).
        self.stop_hook_active = outcome.blocked
        return outcome

    def do_session_end(self, step: dict[str, Any]) -> HookOutcome:
        payload = {"session_id": self.session_id, "reason": step["reason"]}
        return self.fire("SessionEnd", payload, step)

    def do_bash(self, step: dict[str, Any]) -> HookOutcome:
        return self._bash(step["command"], step)

    def do_report(self, step: dict[str, Any]) -> HookOutcome:
        command = " ".join([self.report_command, *(shlex.quote(a) for a in step["args"])])
        if "heredoc" in step:
            text = step["heredoc"]
            if not text.endswith("\n"):
                text += "\n"
            command += f" <<'{HEREDOC_DELIMITER}'\n{text}{HEREDOC_DELIMITER}"
        return self._bash(command, step)

    def _bash(self, command: str, step: dict[str, Any]) -> HookOutcome:
        """The Bash tool: run ``command`` in the cwd with this environment, then PostToolUse."""
        proc = subprocess.run(
            ["/bin/sh", "-c", command],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            cwd=self.cwd,
            check=False,
        )
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        self.log("bash", command=command, exit=proc.returncode, stdout=stdout, stderr=stderr)
        mode = step.get("post_tool_use", "on_success")
        if mode == "never" or (mode == "on_success" and proc.returncode != 0):
            return HookOutcome()
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_response": step.get("tool_response", {"stdout": stdout, "stderr": stderr}),
        }
        _add_agent(payload, step)
        return self.fire("PostToolUse", payload, step)

    def do_write_file(self, step: dict[str, Any]) -> None:
        path = os.path.join(self.cwd, step["path"])
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(step["content"])

    def _result_object(self, step: dict[str, Any]) -> dict[str, Any]:
        obj: dict[str, Any] = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "",
            "session_id": self.session_id,
            "total_cost_usd": 0.0,
            "num_turns": 1,
        }
        obj.update({k: v for k, v in step.items() if k not in ("do", "chars")})
        return obj

    def do_result(self, step: dict[str, Any]) -> None:
        self.stdout(json.dumps(self._result_object(step), ensure_ascii=False) + "\n")

    def do_truncated_result(self, step: dict[str, Any]) -> None:
        text = json.dumps(self._result_object(step), ensure_ascii=False)
        if step["chars"] >= len(text):
            raise FakeError("truncated_result: chars must be shorter than the object")
        self.stdout(text[: step["chars"]])

    def do_emit(self, step: dict[str, Any]) -> None:
        line = step["line"]
        text = line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)
        self.stdout(text + ("\n" if step.get("newline", True) else ""))

    def do_stderr(self, step: dict[str, Any]) -> None:
        with contextlib.suppress(BrokenPipeError, ValueError):
            sys.stderr.write(step["text"])
            sys.stderr.flush()

    def do_sleep(self, step: dict[str, Any]) -> None:
        time.sleep(step["seconds"])

    def do_touch(self, step: dict[str, Any]) -> None:
        with open(step["path"], "a", encoding="utf-8"):
            pass

    def do_ignore_signals(self, step: dict[str, Any]) -> None:
        self.ignore(step["signals"])

    def do_hang(self, step: dict[str, Any]) -> None:
        while True:
            time.sleep(3600)

    def do_exit(self, step: dict[str, Any]) -> int:
        return step["code"]


def _add_agent(payload: dict[str, Any], step: dict[str, Any]) -> None:
    for key in ("agent_id", "agent_type"):
        if key in step:
            payload[key] = step[key]


def _signal_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    """Signal the hook's own process group while its leader is unreaped (so the group ID
    cannot have been reused)."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)


def _error(message: str, code: int) -> int:
    with contextlib.suppress(BrokenPipeError, ValueError):
        sys.stderr.write(f"fake claude: {message}\n")
        sys.stderr.flush()
    return code


def main(args: list[str]) -> int:
    try:
        argv = parse_argv(args)
    except ArgvError as exc:
        return _error(f"error: {exc}", ARGV_ERROR)
    scenario_path = os.environ.get(SCENARIO_ENV)
    try:
        scenario = load_scenario(scenario_path) if scenario_path else None
    except FakeError as exc:
        return _error(str(exc), FAKE_ERROR)
    if argv.version:
        version = (scenario or {}).get("version") or os.environ.get(VERSION_ENV, DEFAULT_VERSION)
        print(version)
        return 0
    if scenario is None:
        return _error(f"no scenario: set {SCENARIO_ENV}", FAKE_ERROR)
    try:
        hooks = parse_hooks(load_settings(argv.flags.get("--settings")))
    except FakeError as exc:
        return _error(str(exc), FAKE_ERROR)
    fake = Fake(argv, scenario, hooks, scenario.get("log") or os.environ.get(LOG_ENV))
    return run(fake)


def run(fake: Fake) -> int:
    fake.install_signals()
    fake.log(
        "start",
        argv=sys.argv[1:],
        flags=fake.argv.flags,
        prompt=fake.argv.prompt,
        headless=fake.argv.headless,
        cwd=fake.cwd,
        session_id=fake.session_id,
    )
    try:
        try:
            code = fake.run_steps(fake.scenario.get("steps", []))
        except Signalled as sig:
            code = fake.finish_signal(sig.name)
        code = 0 if code is None else code
    except FakeError as exc:
        code = _error(str(exc), FAKE_ERROR)
    fake.log("exit", code=code)
    return code
