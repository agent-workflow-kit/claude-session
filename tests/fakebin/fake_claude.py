"""A fake Claude Code CLI driven by a scenario file (design.md, Testing).

``tests/fakebin/claude`` runs ``main``. The fake parses the ``claude`` argv claude-session
builds (design.md, Generated settings: argv), reads the hooks from the generated ``--settings``
and plays a scenario: it runs the injected hook commands with Claude Code's payloads, runs
``claude-session report`` the way an agent's Bash tool would, prints ``stream-json`` lines and
exits, hangs, ignores signals or dies on cue. Standard library only.

Behaviour marked *unverified* below is taken from Claude Code's documentation or chosen as the
likely behaviour; the spike (design.md, Spike results) did not check it.

**Finding the scenario.** ``$FAKE_CLAUDE_SCENARIO`` names a JSON scenario file;
``$FAKE_CLAUDE_LOG`` (or the scenario's ``log``) names a JSON-lines file the fake appends
what it did to. ``--version`` needs no scenario (``$FAKE_CLAUDE_VERSION`` or the scenario's
``version`` overrides the printed version). Without a scenario any other call fails.

**Scenario** (unknown keys and step types are refused, so a typo fails loudly; so are NaN and
infinite numbers)::

    {
      "session_id": "<uuid>",            # default: --session-id, else --resume's id, else new
      "transcript_path": "<abs path>",   # default: Claude Code's layout under $HOME (not created)
      "permission_mode": "default",      # sent in every hook payload
      "report_command": "claude-session report",  # the agent's command prefix (shell text)
      "log": "<abs path>",
      "version": "2.1.312 (Claude Code)",
      "extra_flags": {"--allowedTools": "variadic", "--foo": "bool"},  # more accepted options
      "signals": {"TERM": {"exit": 143, "session_end": "other"}, "HUP": "ignore"},
      "raise_after_steps": "TERM",       # test knob: the fake signals itself after the steps
      "signal_on_error": "TERM",         # test knob: ... as it starts handling a step's error
      "steps": [ {"do": "<step>", ...}, ... ]
    }

**argv** follows the real claude's grammar (commander.js; ``CLAUDE_OPTIONS``): ``--opt value``
and ``--opt=value``, ``--`` ends the options, a value is the next argument whatever it is, an
optional value (``-d``/``--debug [filter]``, ``-r``/``--resume [value]``, ...) is the next
argument unless it looks like an option, and a variadic option (``--add-dir``,
``--allowedTools``/``--allowed-tools``, ``--disallowedTools``, ``--mcp-config``, ``--betas``,
``--tools``, ``--file``) takes one value and then every argument up to the next option or
``--``. So, as with the real claude, ``--debug`` or ``--allowedTools X`` right before a prompt
eats it unless ``--`` comes first (verified live on Claude Code 2.1.314 for ``--debug`` and
``--allowedTools``; the other kinds are read from ``claude --help`` 2.1.314). Only the flags
claude-session passes (and ``extra_flags``, each ``bool``, ``value``, ``optional`` or
``variadic``, which must match Claude Code's grammar for an option it has) are accepted;
``--resume`` without an id (Claude Code's picker) is not modelled and fails loudly. How
commander takes values (a dash-led value after a required option, a lone ``-`` as a value) is
commander's documented behaviour, *unverified* on claude itself. With ``-p`` and no prompt
argument the prompt is read from stdin (unless stdin is a terminal); like the real claude it
waits 3 s for stdin to have data or end, then reads it to the end; nothing at all exits 1.
TERM, HUP and INT kill the fake during that wait (no handlers are in place yet).

**Hook steps** fire every matching hook of one event and take, besides their own keys,
``payload`` (fields merged over the built payload, for hostile-input tests), ``omit`` (field
names dropped, for minimal payloads), ``raw_payload`` (text sent on stdin instead of JSON) and
``on_block`` (steps run when a hook blocked). Every payload has Claude Code's documented common
fields ``session_id``, ``transcript_path``, ``cwd``, ``permission_mode`` and
``hook_event_name``, plus:

- ``session_start``: ``source`` (``startup``, ``resume``, ``fork``, ``clear``, ``compact``;
  default from argv), ``session_id`` (``clear`` defaults to a new id; a new id also gives a new
  default transcript path, both used by later steps), ``transcript_path`` → ``source``.
- ``user_prompt_submit``: ``prompt`` (default: the argv prompt, else empty) → ``prompt``.
- ``permission_request``: ``tool_name``, ``tool_input`` → the same.
- ``notification``: ``notification_type`` (``permission_prompt``, ``idle_prompt``,
  ``elicitation_dialog``), ``message`` (default per type) → the same.
- ``post_tool_use``: ``tool_name``, ``tool_input``, ``tool_response``, ``tool_use_id``
  (default a new ``toolu_`` id), optional ``agent_id`` and ``agent_type`` (a subagent's tool;
  ``session_id`` stays the parent's, live A2).
- ``stop``: ``background_tasks`` (list, default empty; whether the real claude omits an empty
  list is unverified, ``omit`` it to test that) → ``stop_hook_active`` (true on the Stop
  after a blocked Stop, false again after a UserPromptSubmit), ``background_tasks``.
- ``session_end``: ``reason`` → ``reason``.
- ``parallel``: ``steps``, ``post_tool_use``/``permission_request``/``notification`` steps
  (without ``on_block``) whose hooks all run at once, like the hooks of parallel tool calls
  (live A7); ``on_block`` runs when any of them blocked.

**Agent steps** (tool calls; hook steps too): ``bash`` (``command``; runs it with ``/bin/sh -c``
in the cwd with this process's environment in a process group of its own, then fires
``PostToolUse`` for ``Bash`` with ``tool_input`` ``{"command"}`` and ``tool_response``
``{"stdout", "stderr", "interrupted": false, "isImage": false}``; ``post_tool_use``:
``on_success`` (default: a failed command instead fires ``PostToolUseFailure`` with
``tool_name``, ``tool_input``, ``tool_use_id``, ``error`` (``Exit code <n>`` and stderr) and
``is_interrupt``; unverified), ``always`` (``PostToolUse`` either way) or ``never``; optional
``tool_response``, ``tool_use_id``, ``error``, ``agent_id``/``agent_type``), ``report``
(``args``, the arguments after the report command, and optional ``heredoc`` text fed through
``<<'EOF'``; otherwise as ``bash``) and ``write_file`` (``path``, relative to the cwd,
``content``; then ``PostToolUse`` for ``Write`` with ``tool_input`` ``{"file_path",
"content"}`` and ``tool_response`` ``{"filePath", "success": true}`` (Claude Code's documented
example); ``post_tool_use``: ``always`` (default) or ``never``).

**Output and process steps**: ``result`` (one ``stream-json`` ``result`` object; keys
``subtype``, ``is_error``, ``result``, ``total_cost_usd``, ``num_turns``, ``duration_ms``,
``terminal_reason``, ``permission_denials``, ``session_id``), ``emit`` (``line``: text or a JSON
object; ``newline``, default true), ``truncated_result`` (the first ``chars`` characters, or
``bytes`` bytes of the UTF-8 text (a cut may split a character), of a serialized ``result``
object, no newline), ``stderr`` (``text``), ``sleep`` (``seconds``), ``touch`` (``path``: an
empty marker file, for tests to synchronize on), ``ignore_signals`` (``signals``: names such as
``TERM``), ``hang`` and ``exit`` (``code``). The fake exits 0 after the last step. Output goes
out as UTF-8 bytes; only a broken pipe is ignored.

**Hooks** run as Claude Code runs them (design.md, Spike results): ``/bin/sh -c <command>`` in
the cwd, with this process's environment and the payload on stdin. All matching hooks of an
event (and of a ``parallel`` step) run at once and finish in any order (spike: "order not
stable"; live A7: completion order); identical command strings of one event run once. Each
runs in a process group of its own (unverified) and has its own ``timeout`` (default 60 s): at
the timeout its process gets SIGTERM (Node's ``child.kill``), its pipes are closed, its output
is dropped (live A10), and after a short grace its whole group gets SIGKILL as cleanup (the
group and the kill sequence are unverified). A hook is done when its process exited and its
output is closed (a grandchild holding the output keeps it running up to the timeout).

A hook blocks with exit 2 (stderr is the reason) or exit 0 and a JSON object with
``"decision": "block"``, on ``Stop``, ``SubagentStop``, ``PostToolUse``, ``UserPromptSubmit``,
``PermissionRequest`` and ``PreToolUse`` only; elsewhere exit 2 is a non-blocking error
(``hook_error``) and the decision is ignored. ``hookSpecificOutput`` counts only with a
``hookEventName`` equal to the event (else ``hook_output_invalid``); its
``additionalContext``, and plain ``SessionStart`` stdout, are recorded as context.

**Matchers.** ``Stop``, ``SubagentStop`` and ``UserPromptSubmit`` ignore the matcher. Other
events match it against a payload field: ``SessionStart`` (source), ``PermissionRequest``,
``PreToolUse``, ``PostToolUse`` and ``PostToolUseFailure`` (tool name), ``Notification``
(type) and ``SessionEnd`` (reason). An absent, empty or ``*`` matcher matches everything; a
matcher of only letters, digits, ``_`` and ``|`` is an exact match against one of its
``|``-separated names; anything else is an unanchored regular-expression search.

**Signals.** By default SIGTERM fires ``SessionEnd`` with reason ``other`` and exits 143 and
SIGHUP exits 129 (spike: auto-exit by signal; that SIGHUP fires no ``SessionEnd`` is
unverified). SIGINT is only logged in interactive mode (spike: one Ctrl-C at an idle prompt
does not exit) and exits 130 with ``-p`` (unverified). An action is ``"ignore"`` (log only),
``{"exit": n, "session_end": reason?}`` or ``{"die": true, "session_end": reason?}`` (die of
the signal itself after the ``SessionEnd``). Running hooks and Bash commands are SIGKILLed with
their process groups first. A signal arriving while the fake already unwinds for one is only
logged; one arriving after the last step still takes its action. ``"ignore"`` installs a
handler that only logs, never ``SIG_IGN``, so children do not inherit the ignored signal.
Hooks and Bash commands get ``PR_SET_PDEATHSIG(SIGKILL)`` on Linux, so a killed fake leaves
none behind. The fake signals only the processes and groups it spawned and has not reaped.

**Exit codes.** 1 for an argv the real claude rejects; 99 for a broken scenario or settings
file or any fake-internal error (never 1, which a real claude also uses).

**Log records** (``"event"`` key): ``start``, ``step``, ``hook``, ``hook_skipped``,
``hook_error``, ``hook_output_invalid``, ``context``, ``block``, ``bash``, ``signal``,
``signal_ignored``, ``error``, ``exit``.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import selectors
import shlex
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

SCENARIO_ENV = "FAKE_CLAUDE_SCENARIO"
LOG_ENV = "FAKE_CLAUDE_LOG"
VERSION_ENV = "FAKE_CLAUDE_VERSION"
DEFAULT_VERSION = "2.1.312 (Claude Code)"
DEFAULT_REPORT_COMMAND = "claude-session report"
DEFAULT_PERMISSION_MODE = "default"
DEFAULT_HOOK_TIMEOUT = 60.0
HOOK_KILL_GRACE = 1.0
STDIN_WAIT = 3.0  # how long ``-p`` without a prompt waits for stdin (design.md, Spike results)
POLL_INTERVAL = 0.02
READ_SIZE = 65536
# Exit code for a broken scenario or a fake-internal error, distinct from anything claude uses.
FAKE_ERROR = 99
# Exit code of a real claude for an argv it rejects.
ARGV_ERROR = 1
NO_INPUT = "Input must be provided either through stdin or as a prompt argument when using --print"

BOOL, VALUE, OPTIONAL, VARIADIC = "bool", "value", "optional", "variadic"
# Claude Code's options and how its argv parser (commander.js) reads each one, from
# ``claude --help`` of 2.1.314 (``<v>`` value, ``[v]`` optional, ``<v...>`` variadic, none bool).
# commander (its documented parsing, not checked on claude): a value (and a variadic's first
# value) is the next argument whatever it is; an optional value is the next argument unless it
# looks like an option (starts with "-" and is longer than "-"); a variadic then takes every
# following argument up to one that looks like an option, ``--`` included; ``--opt=v`` gives
# exactly one value. The two hidden options claude-session passes
# (``--append-system-prompt-file``, ``--max-turns``) are from Claude Code's documentation, not
# ``--help``.
CLAUDE_OPTIONS: dict[str, str] = {
    "--add-dir": VARIADIC,
    "--agent": VALUE,
    "--agents": VALUE,
    "--allow-dangerously-skip-permissions": BOOL,
    "--allowedTools": VARIADIC,
    "--append-system-prompt": VALUE,
    "--append-system-prompt-file": VALUE,
    "--autocompact": VALUE,
    "--ax-screen-reader": BOOL,
    "--bg": BOOL,
    "--bare": BOOL,
    "--betas": VARIADIC,
    "--brief": BOOL,
    "--chrome": BOOL,
    "--cloud": OPTIONAL,
    "--continue": BOOL,
    "--dangerously-skip-permissions": BOOL,
    "--debug": OPTIONAL,
    "--debug-file": VALUE,
    "--desktop": BOOL,
    "--disable-slash-commands": BOOL,
    "--disallowedTools": VARIADIC,
    "--effort": VALUE,
    "--environment": VALUE,
    "--exclude-dynamic-system-prompt-sections": BOOL,
    "--fallback-model": VALUE,
    "--file": VARIADIC,
    "--fork-session": BOOL,
    "--forward-subagent-text": BOOL,
    "--from-pr": OPTIONAL,
    "--help": BOOL,
    "--ide": BOOL,
    "--include-hook-events": BOOL,
    "--include-partial-messages": BOOL,
    "--input-format": VALUE,
    "--json-schema": VALUE,
    "--max-budget-usd": VALUE,
    "--max-turns": VALUE,
    "--mcp-config": VARIADIC,
    "--model": VALUE,
    "--name": VALUE,
    "--no-chrome": BOOL,
    "--no-session-persistence": BOOL,
    "--output-format": VALUE,
    "--permission-mode": VALUE,
    "--permission-prompts": VALUE,
    "--plugin-dir": VALUE,
    "--plugin-url": VALUE,
    "--print": BOOL,
    "-p": BOOL,
    "--prompt-suggestions": OPTIONAL,
    "--remote-control": OPTIONAL,
    "--remote-control-session-name-prefix": VALUE,
    "--replay-user-messages": BOOL,
    "--restricted": BOOL,
    "--resume": OPTIONAL,
    "--safe-mode": BOOL,
    "--session-id": VALUE,
    "--setting-sources": VALUE,
    "--settings": VALUE,
    "--strict-mcp-config": BOOL,
    "--system-prompt": VALUE,
    "--system-prompt-snapshot": VALUE,
    "--teleport": OPTIONAL,
    "--tmux": BOOL,
    "--tools": VARIADIC,
    "--verbose": BOOL,
    "--worktree": OPTIONAL,
}
# Other spellings of one option (``-p`` stays its own key; ``-v`` is ``--version``).
ALIASES = {
    "-c": "--continue",
    "-d": "--debug",
    "-h": "--help",
    "-n": "--name",
    "-r": "--resume",
    "-w": "--worktree",
    "--allowed-tools": "--allowedTools",
    "--background": "--bg",
    "--disallowed-tools": "--disallowedTools",
}
# The options claude-session passes (design.md, Generated settings: argv): the only ones
# accepted besides a scenario's ``extra_flags``; anything else is an argv bug in the code under
# test and is rejected, as the real claude rejects unknown options.
STRICT_FLAGS = {
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
    "--debug",
    "-p",
    "--print",
    "--fork-session",
    "--strict-mcp-config",
    "--verbose",
}
REPEATABLE_FLAGS = {"--plugin-dir"}
OUTPUT_FORMATS = {"text", "json", "stream-json"}
EXTRA_FLAG_KINDS = {BOOL, VALUE, OPTIONAL, VARIADIC}

SOURCES = {"startup", "resume", "fork", "clear", "compact"}
NOTIFICATION_MESSAGES = {
    "permission_prompt": "Claude needs your permission to use a tool",
    "idle_prompt": "Claude is waiting for your input",
    "elicitation_dialog": "Claude Code needs your input",
}
SIGNALS = {"TERM": signal.SIGTERM, "HUP": signal.SIGHUP, "INT": signal.SIGINT}
SIGNUMS = set(SIGNALS.values())
HEREDOC_DELIMITER = "EOF"

SCENARIO_KEYS = {
    "session_id",
    "transcript_path",
    "permission_mode",
    "report_command",
    "log",
    "version",
    "extra_flags",
    "signals",
    "raise_after_steps",
    "signal_on_error",
    "steps",
}
HOOK_STEP_KEYS = {"payload", "omit", "raw_payload", "on_block"}
TOOL_KEYS = {"post_tool_use", "agent_id", "agent_type", "tool_response", "tool_use_id", "error"}
STEP_KEYS: dict[str, set[str]] = {
    "session_start": {"source", "session_id", "transcript_path"},
    "user_prompt_submit": {"prompt"},
    "permission_request": {"tool_name", "tool_input"},
    "notification": {"notification_type", "message"},
    "post_tool_use": {
        "tool_name",
        "tool_input",
        "tool_response",
        "tool_use_id",
        "agent_id",
        "agent_type",
    },
    "stop": {"background_tasks"},
    "session_end": {"reason"},
    "parallel": {"steps"},
    "bash": {"command", *TOOL_KEYS},
    "report": {"args", "heredoc", *TOOL_KEYS},
    "write_file": {"path", "content", "post_tool_use", "agent_id", "agent_type", "tool_use_id"},
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
    "truncated_result": {"chars", "bytes", "result", "session_id"},
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
    "parallel",
    "bash",
    "report",
    "write_file",
}
PARALLEL_STEPS = {"post_tool_use", "permission_request", "notification"}
POST_TOOL_USE_MODES = {"on_success", "always", "never"}
WRITE_POST_TOOL_USE_MODES = {"always", "never"}

# Events whose hooks can block (exit 2 or "decision": "block").
BLOCKING_EVENTS = {
    "Stop",
    "SubagentStop",
    "PostToolUse",
    "UserPromptSubmit",
    "PermissionRequest",
    "PreToolUse",
}
# Events that ignore the matcher.
NO_MATCHER_EVENTS = {"Stop", "SubagentStop", "UserPromptSubmit"}
# The payload field a hook event's matcher is compared with.
MATCH_FIELD = {
    "SessionStart": "source",
    "PermissionRequest": "tool_name",
    "PreToolUse": "tool_name",
    "Notification": "notification_type",
    "PostToolUse": "tool_name",
    "PostToolUseFailure": "tool_name",
    "SessionEnd": "reason",
}
EXACT_MATCHER = re.compile(r"[A-Za-z0-9_|]+")


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


@contextlib.contextmanager
def signals_blocked() -> Iterator[None]:
    """TERM, HUP and INT blocked (restored after), so no handler runs inside."""
    old = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNUMS)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)


# --- argv ----------------------------------------------------------------------------------


@dataclass
class Argv:
    flags: dict[str, Any] = field(default_factory=dict)
    prompt: str | None = None
    version: bool = False

    @property
    def headless(self) -> bool:
        return bool(self.flags.get("-p") or self.flags.get("--print"))


def _looks_like_option(arg: str) -> bool:
    """commander's test: ``-`` alone is a value, ``--`` and ``-x...`` are options."""
    return len(arg) > 1 and arg.startswith("-")


def parse_argv(args: list[str], extra: dict[str, str] | None = None) -> Argv:
    """Parse claude's argv strictly, with commander's value grammar (``CLAUDE_OPTIONS``):
    unknown options, a missing value, a repeated single-use flag or more than one prompt are
    rejected, as are the combinations the spike found invalid. ``extra`` maps more accepted
    options to a kind (``bool``, ``value``, ``optional``, ``variadic``)."""
    kinds = {name: CLAUDE_OPTIONS[name] for name in STRICT_FLAGS}
    kinds |= {ALIASES.get(k, k): v for k, v in (extra or {}).items()}
    parsed = Argv()
    i = 0
    options_done = False

    def positional(arg: str) -> None:
        if parsed.prompt is not None:
            raise ArgvError(f"too many arguments: '{arg}'")
        parsed.prompt = arg

    while i < len(args):
        arg = args[i]
        i += 1
        if options_done or not _looks_like_option(arg):
            positional(arg)
            continue
        if arg == "--":
            options_done = True
            continue
        name, eq, inline = arg.partition("=") if arg.startswith("--") else (arg, "", "")
        has_inline = eq == "="
        name = ALIASES.get(name, name)
        kind = kinds.get(name)
        if name in ("--version", "-v"):
            if has_inline:
                raise ArgvError(f"option '{name}' does not take an argument")
            parsed.version = True
        elif kind in (VALUE, VARIADIC):
            if has_inline:
                value = inline
            elif i < len(args):
                value = args[i]
                i += 1
            else:
                raise ArgvError(f"option '{name}' argument missing")
            if kind == VARIADIC:
                values = [value]
                while not has_inline and i < len(args) and not _looks_like_option(args[i]):
                    values.append(args[i])
                    i += 1
                parsed.flags.setdefault(name, []).extend(values)
            elif name in REPEATABLE_FLAGS:
                parsed.flags.setdefault(name, []).append(value)
            elif name in parsed.flags:
                raise ArgvError(f"option '{name}' given more than once")
            else:
                parsed.flags[name] = value
        elif kind == OPTIONAL:
            if has_inline:
                parsed.flags[name] = inline
            elif i < len(args) and not _looks_like_option(args[i]):
                parsed.flags[name] = args[i]
                i += 1
            else:
                parsed.flags[name] = True
        elif kind == BOOL:
            if has_inline:
                raise ArgvError(f"option '{name}' does not take an argument")
            parsed.flags[name] = True
        else:
            raise ArgvError(f"unknown option '{arg}'")
    if parsed.version:
        return parsed
    if parsed.flags.get("--resume") is True:
        raise FakeError("--resume without a session id opens Claude Code's picker; not modelled")
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


def read_headless_prompt(parsed: Argv) -> None:
    """``-p`` without a prompt argument reads the prompt from stdin; none at all is an argv
    error."""
    if not parsed.headless or parsed.prompt is not None:
        return
    text = ""
    # ``main`` blocks TERM/HUP/INT; an unbounded read with them blocked could never be ended.
    # They take their default action (death) here, as no handler is in place yet.
    for sig in SIGNUMS:
        signal.signal(sig, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNUMS)
    try:
        with contextlib.suppress(OSError, ValueError):
            if not sys.stdin.isatty():
                with selectors.DefaultSelector() as sel:
                    sel.register(sys.stdin.fileno(), selectors.EVENT_READ)
                    if sel.select(STDIN_WAIT):
                        text = sys.stdin.buffer.read().decode("utf-8", "replace")
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, SIGNUMS)
    if not text.strip():
        raise ArgvError(NO_INPUT)
    parsed.prompt = text


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
    """Claude Code's matcher rule: absent, empty or ``*`` matches everything; letters, digits,
    ``_`` and ``|`` only: an exact match against one of the ``|``-separated names; anything
    else: an unanchored regular-expression search."""
    if matcher is None or matcher in ("", "*"):
        return True
    value = value or ""
    if EXACT_MATCHER.fullmatch(matcher):
        return value in matcher.split("|")
    try:
        return re.search(matcher, value) is not None
    except re.error as exc:
        raise FakeError(f"invalid hook matcher {matcher!r}: {exc}") from exc


# --- scenario ------------------------------------------------------------------------------


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise FakeError(f"scenario number {text} is not finite")
    return value


def _no_constant(name: str) -> float:
    raise FakeError(f"scenario number {name} is not finite")


def load_scenario(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            scenario = json.load(fh, parse_float=_finite_float, parse_constant=_no_constant)
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
    for key in (
        "session_id",
        "transcript_path",
        "permission_mode",
        "report_command",
        "log",
        "version",
    ):
        if key in scenario and not isinstance(scenario[key], str):
            raise FakeError(f"scenario {key} must be a string")
    extra = scenario.get("extra_flags", {})
    if not isinstance(extra, dict) or not all(
        isinstance(k, str) and k.startswith("-") and v in EXTRA_FLAG_KINDS for k, v in extra.items()
    ):
        raise FakeError(
            f"scenario extra_flags must map options to one of {sorted(EXTRA_FLAG_KINDS)}"
        )
    declared: dict[str, str] = extra
    for name, kind in declared.items():
        real = CLAUDE_OPTIONS.get(ALIASES.get(name, name))
        if real is not None and real != kind:
            raise FakeError(
                f"scenario extra_flags declares {name} {kind}; Claude Code's grammar is {real}"
            )
    for knob in ("raise_after_steps", "signal_on_error"):
        if scenario.get(knob, "TERM") not in SIGNALS:
            raise FakeError(f"scenario {knob} must be one of {sorted(SIGNALS)}")
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
    elif kind == "user_prompt_submit":
        need("prompt", str, required=False)
    elif kind == "permission_request":
        need("tool_name", str)
        need("tool_input", dict, required=False)
    elif kind == "notification":
        need("notification_type", str)
        if step["notification_type"] not in NOTIFICATION_MESSAGES:
            raise FakeError(
                f"{here}: notification_type must be one of {sorted(NOTIFICATION_MESSAGES)}"
            )
        need("message", str, required=False)
    elif kind == "post_tool_use":
        need("tool_name", str)
        need("tool_input", dict, required=False)
        need("tool_response", (dict, str), required=False)
        need("tool_use_id", str, required=False)
        need("agent_id", str, required=False)
        need("agent_type", str, required=False)
    elif kind == "stop":
        need("background_tasks", list, required=False)
    elif kind == "session_end":
        need("reason", str)
    elif kind == "parallel":
        validate_steps(step.get("steps"), f"{here}.steps")
        for sub in step["steps"]:
            if sub["do"] not in PARALLEL_STEPS or "on_block" in sub:
                raise FakeError(
                    f"{here}: parallel steps must be {sorted(PARALLEL_STEPS)} without on_block"
                )
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
            raise FakeError(f"{here}: post_tool_use must be one of {sorted(POST_TOOL_USE_MODES)}")
        need("agent_id", str, required=False)
        need("agent_type", str, required=False)
        need("tool_response", (dict, str), required=False)
        need("tool_use_id", str, required=False)
        need("error", str, required=False)
    elif kind == "write_file":
        need("path", str)
        need("content", str)
        if step.get("post_tool_use", "always") not in WRITE_POST_TOOL_USE_MODES:
            raise FakeError(f"{here}: post_tool_use must be 'always' or 'never'")
        need("agent_id", str, required=False)
        need("agent_type", str, required=False)
        need("tool_use_id", str, required=False)
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
        if ("chars" in step) == ("bytes" in step):
            raise FakeError(f"{here}: give exactly one of chars and bytes")
        for unit in ("chars", "bytes"):
            need(unit, int, required=False)
            if unit in step and step[unit] < 1:
                raise FakeError(f"{here}: {unit} must be at least 1")
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
        usage = f"signals.{name}: use 'ignore', {{exit, session_end?}} or {{die, session_end?}}"
        if not isinstance(action, dict) or set(action) - {"exit", "session_end", "die"}:
            raise FakeError(usage)
        if "die" in action:
            if action["die"] is not True or "exit" in action:
                raise FakeError(usage)
        else:
            code = action.get("exit")
            if isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 255:
                raise FakeError(f"signals.{name}: exit must be 0..255")
        end = action.get("session_end")
        if end is not None and not isinstance(end, str):
            raise FakeError(f"signals.{name}: session_end must be a string")


# --- child processes -----------------------------------------------------------------------


def _load_prctl() -> Callable[..., int] | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.signal.restype = ctypes.c_void_p
        libc.signal.argtypes = (ctypes.c_int, ctypes.c_void_p)
        global _LIBC_SIGNAL
        _LIBC_SIGNAL = libc.signal
        return libc.prctl
    except (OSError, AttributeError):
        return None


_LIBC_SIGNAL: Callable[..., object] | None = None
_PRCTL = _load_prctl()
_PR_SET_PDEATHSIG = 1


def _child_setup(parent: int) -> None:
    """``preexec_fn`` of every hook and Bash child: TERM/HUP/INT back to their defaults and
    unblocked (the fork happens with them blocked), and on Linux ``PR_SET_PDEATHSIG(SIGKILL)``
    so the child dies with the fake."""
    for sig in SIGNUMS:
        if _LIBC_SIGNAL is not None:
            _LIBC_SIGNAL(sig, 0)  # SIG_DFL in C: no Python handler can run in the child
        else:
            signal.signal(sig, signal.SIG_DFL)
    if _PRCTL is not None:
        _PRCTL(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
        if os.getppid() != parent:  # the fake died before the flag was set
            os.kill(os.getpid(), signal.SIGKILL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNUMS)


def _exited(proc: subprocess.Popen[bytes]) -> bool:
    """Whether ``proc`` exited, without reaping it where possible (an unreaped leader pins its
    PID and process group ID)."""
    if proc.returncode is not None:
        return True
    waitid = getattr(os, "waitid", None)
    if waitid is None:  # macOS: poll reaps; a reaped group is never signalled after
        return proc.poll() is not None
    try:
        return waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def _signal_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    """Signal the child's own process group while its leader is unreaped (so the group ID
    cannot have been reused)."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)


@dataclass
class Running:
    """One hook or Bash command being run."""

    proc: subprocess.Popen[bytes]
    stdin: memoryview
    deadline: float | None
    meta: Any = None
    out: bytearray = field(default_factory=bytearray)
    err: bytearray = field(default_factory=bytearray)
    open_fds: set[int] = field(default_factory=set)
    stdin_fd: int = -1
    timed_out: bool = False
    kill_at: float | None = None


# --- the run -------------------------------------------------------------------------------


@dataclass
class HookOutcome:
    """What the hooks of one event did, as Claude Code reads them."""

    blocked: bool = False
    reasons: list[str] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HookJob:
    event: str
    hook: Hook
    stdin: str
    outcome: HookOutcome


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
        self.permission_mode = scenario.get("permission_mode", DEFAULT_PERMISSION_MODE)
        self.report_command = scenario.get("report_command", DEFAULT_REPORT_COMMAND)
        self.stop_hook_active = False
        defaults: dict[str, Any] = {
            "TERM": {"exit": 128 + signal.SIGTERM, "session_end": "other"},
            "HUP": {"exit": 128 + signal.SIGHUP},
            "INT": {"exit": 128 + signal.SIGINT} if argv.headless else "ignore",
        }
        self.signals: dict[str, Any] = {**defaults, **scenario.get("signals", {})}
        self.handling_signal = False
        # Spawned and not yet reaped: the only processes (and groups) the fake ever signals.
        self.children: list[subprocess.Popen[bytes]] = []

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

    def stdout(self, data: str | bytes) -> None:
        write_out(sys.stdout, data)

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

    def pending_signal(self) -> str | None:
        """A blocked, pending signal whose action is not ``ignore``."""
        pending = signal.sigpending()
        for name, signum in SIGNALS.items():
            if signum in pending and self.signals[name] != "ignore":
                return name
        return None

    def finish_signal(self, name: str) -> int:
        action = self.signals[name]
        end = action.get("session_end")
        if end is not None:
            self.fire("SessionEnd", {"reason": end}, {})
        return 128 + SIGNALS[name] if action.get("die") else action["exit"]

    # -- children --

    def spawn(self, command: str, with_stdin: bool) -> subprocess.Popen[bytes]:
        """``/bin/sh -c command`` in a process group of its own, registered before any signal
        handler can run."""
        parent = os.getpid()
        with signals_blocked():
            proc = subprocess.Popen(
                ["/bin/sh", "-c", command],
                stdin=subprocess.PIPE if with_stdin else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd,
                process_group=0,
                preexec_fn=lambda: _child_setup(parent),
            )
            self.children.append(proc)
        return proc

    def reap(self, proc: subprocess.Popen[bytes]) -> int:
        """Reap ``proc`` and forget it, with no signal handler running in between."""
        with signals_blocked():
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
            code = proc.wait()
            with contextlib.suppress(ValueError):
                self.children.remove(proc)
        return code

    def kill_children(self) -> None:
        """SIGKILL every spawned, unreaped child with its process group, and reap it."""
        for proc in list(self.children):
            _signal_group(proc, signal.SIGKILL)
            self.reap(proc)

    def run_all(self, runs: list[Running], on_done: Callable[[Running, int], None]) -> None:
        """Run ``runs`` concurrently: feed their stdin, collect their output and end each at
        its deadline; ``on_done`` gets each in completion order with its exit status."""
        sel = selectors.DefaultSelector()
        owner: dict[int, tuple[Running, str]] = {}

        def close(run: Running, fd: int) -> None:
            with contextlib.suppress(KeyError, ValueError):
                sel.unregister(fd)
            owner.pop(fd, None)
            run.open_fds.discard(fd)
            if fd == run.stdin_fd and run.proc.stdin is not None:
                with contextlib.suppress(OSError):
                    run.proc.stdin.close()

        try:
            for run in runs:
                proc = run.proc
                if proc.stdin is not None:
                    fd = run.stdin_fd = proc.stdin.fileno()
                    if run.stdin:
                        os.set_blocking(fd, False)
                        sel.register(fd, selectors.EVENT_WRITE)
                        owner[fd] = (run, "in")
                        run.open_fds.add(fd)
                    else:
                        proc.stdin.close()
                for name, stream in (("out", proc.stdout), ("err", proc.stderr)):
                    assert stream is not None
                    sel.register(stream.fileno(), selectors.EVENT_READ)
                    owner[stream.fileno()] = (run, name)
                    run.open_fds.add(stream.fileno())
            active = list(runs)
            while active:
                now = time.monotonic()
                for run in list(active):
                    if run.timed_out:
                        if run.kill_at is not None and now >= run.kill_at:
                            _signal_group(run.proc, signal.SIGKILL)
                            active.remove(run)
                            on_done(run, self.reap(run.proc))
                    elif run.deadline is not None and now >= run.deadline:
                        run.timed_out = True
                        if run.proc.returncode is None:  # unreaped: the PID is still ours
                            with contextlib.suppress(ProcessLookupError, PermissionError):
                                os.kill(run.proc.pid, signal.SIGTERM)
                        for fd in list(run.open_fds):
                            close(run, fd)
                        run.kill_at = now + HOOK_KILL_GRACE
                    elif not run.open_fds and _exited(run.proc):
                        active.remove(run)
                        on_done(run, self.reap(run.proc))
                if not active:
                    break
                if not owner:
                    time.sleep(POLL_INTERVAL)
                    continue
                for key, _mask in sel.select(POLL_INTERVAL):
                    fd = key.fd
                    run, name = owner[fd]
                    if name == "in":
                        try:
                            sent = os.write(fd, run.stdin[:READ_SIZE])
                        except BlockingIOError:
                            continue
                        except OSError:  # the child closed its stdin
                            close(run, fd)
                            continue
                        run.stdin = run.stdin[sent:]
                        if not run.stdin:
                            close(run, fd)
                        continue
                    data = os.read(fd, READ_SIZE)
                    if not data:
                        close(run, fd)
                    elif name == "out":
                        run.out += data
                    else:
                        run.err += data
        finally:
            sel.close()

    # -- hooks --

    def payload(self, event: str, **fields: Any) -> dict[str, Any]:
        """Claude Code's common hook fields, then the event's own."""
        return {
            "session_id": self.session_id,
            "transcript_path": self.transcript_path,
            "cwd": self.cwd,
            "permission_mode": self.permission_mode,
            "hook_event_name": event,
            **fields,
        }

    def _stdin(self, event: str, fields: dict[str, Any], step: dict[str, Any]) -> str:
        payload = self.payload(event, **fields)
        for name in step.get("omit", []):
            payload.pop(name, None)
        payload.update(step.get("payload", {}))
        raw = step.get("raw_payload")
        if raw is not None:
            return raw
        return json.dumps(payload, ensure_ascii=False)

    def _matching(self, event: str, fields: dict[str, Any], step: dict[str, Any]) -> list[Hook]:
        field_name = MATCH_FIELD.get(event)
        value = {**fields, **step.get("payload", {})}.get(field_name) if field_name else None
        seen: set[str] = set()
        out: list[Hook] = []
        for hook in self.hooks.get(event, []):
            if hook.command in seen:
                self.log("hook_skipped", hook_event=event, command=hook.command, duplicate=True)
                continue
            if event not in NO_MATCHER_EVENTS and not matches(
                hook.matcher, value if isinstance(value, str) else None
            ):
                self.log("hook_skipped", hook_event=event, command=hook.command)
                continue
            seen.add(hook.command)
            out.append(hook)
        return out

    def fire(self, event: str, fields: dict[str, Any], step: dict[str, Any]) -> HookOutcome:
        """Run every matching hook of ``event`` at once, as Claude Code does."""
        return self.fire_many([(event, fields, step)])

    def fire_many(self, calls: list[tuple[str, dict[str, Any], dict[str, Any]]]) -> HookOutcome:
        """Fire several events' hooks together (parallel tool calls); the merged outcome."""
        jobs: list[HookJob] = []
        outcomes: list[tuple[str, HookOutcome]] = []
        for event, fields, step in calls:
            outcome = HookOutcome()
            outcomes.append((event, outcome))
            stdin = self._stdin(event, fields, step)
            jobs += [HookJob(event, h, stdin, outcome) for h in self._matching(event, fields, step)]
        runs = []
        for job in jobs:
            proc = self.spawn(job.hook.command, with_stdin=True)
            deadline = time.monotonic() + job.hook.timeout
            runs.append(Running(proc, memoryview(job.stdin.encode("utf-8")), deadline, job))
        self.run_all(runs, self._hook_done)
        merged = HookOutcome()
        for event, outcome in outcomes:
            if outcome.blocked:
                self.log("block", hook_event=event, reasons=outcome.reasons)
            if outcome.contexts:
                self.log("context", hook_event=event, contexts=outcome.contexts)
            merged.blocked |= outcome.blocked
            merged.reasons += outcome.reasons
            merged.contexts += outcome.contexts
        return merged

    def _hook_done(self, run: Running, code: int) -> None:
        job: HookJob = run.meta
        record: dict[str, Any] = {
            "hook_event": job.event,
            "command": job.hook.command,
            "payload": job.stdin,
            "exit": code,
            "timed_out": run.timed_out,
        }
        if run.timed_out:
            self.log("hook", **record)  # Claude Code drops a canceled hook's output (live A10)
            return
        stdout = run.out.decode("utf-8", "replace")
        stderr = run.err.decode("utf-8", "replace")
        record.update(stdout=stdout, stderr=stderr)
        self.log("hook", **record)
        self._read_output(job.event, code, stdout, stderr, job.outcome)

    def _read_output(
        self, event: str, code: int, stdout: str, stderr: str, outcome: HookOutcome
    ) -> None:
        blocking = event in BLOCKING_EVENTS
        if code == 2:
            if blocking:
                outcome.blocked = True
                outcome.reasons.append(stderr.strip())
            else:
                self.log("hook_error", hook_event=event, exit=code, stderr=stderr)
            return
        if code != 0:
            return  # a non-blocking error: Claude Code shows it and continues
        try:
            parsed = json.loads(stdout) if stdout.strip() else None
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            if parsed.get("decision") == "block" and blocking:
                outcome.blocked = True
                reason = parsed.get("reason")
                outcome.reasons.append(reason if isinstance(reason, str) else "")
            if "hookSpecificOutput" in parsed:
                specific = parsed["hookSpecificOutput"]
                if not isinstance(specific, dict) or specific.get("hookEventName") != event:
                    self.log("hook_output_invalid", hook_event=event, output=specific)
                elif isinstance(specific.get("additionalContext"), str):
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
        return self.fire("SessionStart", {"source": source}, step)

    def do_user_prompt_submit(self, step: dict[str, Any]) -> HookOutcome:
        self.stop_hook_active = False
        prompt = step.get("prompt", self.argv.prompt or "")
        return self.fire("UserPromptSubmit", {"prompt": prompt}, step)

    def _permission_request(self, step: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        fields = {"tool_name": step["tool_name"], "tool_input": step.get("tool_input", {})}
        return "PermissionRequest", fields

    def _notification(self, step: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        kind = step["notification_type"]
        message = step.get("message", NOTIFICATION_MESSAGES[kind])
        return "Notification", {"message": message, "notification_type": kind}

    def _post_tool_use(self, step: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        fields: dict[str, Any] = {
            "tool_name": step["tool_name"],
            "tool_input": step.get("tool_input", {}),
            "tool_response": step.get("tool_response", {}),
            "tool_use_id": step.get("tool_use_id") or _tool_use_id(),
        }
        _add_agent(fields, step)
        return "PostToolUse", fields

    def do_permission_request(self, step: dict[str, Any]) -> HookOutcome:
        return self.fire(*self._permission_request(step), step)

    def do_notification(self, step: dict[str, Any]) -> HookOutcome:
        return self.fire(*self._notification(step), step)

    def do_post_tool_use(self, step: dict[str, Any]) -> HookOutcome:
        return self.fire(*self._post_tool_use(step), step)

    def do_parallel(self, step: dict[str, Any]) -> HookOutcome:
        calls = []
        for sub in step["steps"]:
            self.log("step", step=sub, parallel=True)
            event, fields = getattr(self, f"_{sub['do']}")(sub)
            calls.append((event, fields, sub))
        return self.fire_many(calls)

    def do_stop(self, step: dict[str, Any]) -> HookOutcome:
        fields = {
            "stop_hook_active": self.stop_hook_active,
            "background_tasks": step.get("background_tasks", []),
        }
        outcome = self.fire("Stop", fields, step)
        # The Stop after a blocked one reports stop_hook_active (spike: Stop-hook blocking).
        self.stop_hook_active = outcome.blocked
        return outcome

    def do_session_end(self, step: dict[str, Any]) -> HookOutcome:
        return self.fire("SessionEnd", {"reason": step["reason"]}, step)

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
        """The Bash tool: run ``command`` in the cwd with this environment in a process group
        of its own, then PostToolUse (or PostToolUseFailure)."""
        result: list[tuple[Running, int]] = []
        proc = self.spawn(command, with_stdin=False)
        self.run_all([Running(proc, memoryview(b""), None)], lambda r, c: result.append((r, c)))
        ((run, code),) = result
        stdout = run.out.decode("utf-8", "replace")
        stderr = run.err.decode("utf-8", "replace")
        self.log("bash", command=command, exit=code, stdout=stdout, stderr=stderr)
        mode = step.get("post_tool_use", "on_success")
        if mode == "never":
            return HookOutcome()
        fields: dict[str, Any] = {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_use_id": step.get("tool_use_id") or _tool_use_id(),
        }
        _add_agent(fields, step)
        if mode == "on_success" and code != 0:
            error = step.get("error", f"Exit code {code}\n{stderr}".rstrip("\n"))
            fields.update(error=error, is_interrupt=False)
            return self.fire("PostToolUseFailure", fields, step)
        fields["tool_response"] = step.get(
            "tool_response",
            {"stdout": stdout, "stderr": stderr, "interrupted": False, "isImage": False},
        )
        return self.fire("PostToolUse", fields, step)

    def do_write_file(self, step: dict[str, Any]) -> HookOutcome:
        path = os.path.join(self.cwd, step["path"])
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(step["content"])
        if step.get("post_tool_use", "always") == "never":
            return HookOutcome()
        fields: dict[str, Any] = {
            "tool_name": "Write",
            "tool_input": {"file_path": path, "content": step["content"]},
            "tool_response": {"filePath": path, "success": True},
            "tool_use_id": step.get("tool_use_id") or _tool_use_id(),
        }
        _add_agent(fields, step)
        return self.fire("PostToolUse", fields, step)

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
        obj.update({k: v for k, v in step.items() if k not in ("do", "chars", "bytes")})
        return obj

    def do_result(self, step: dict[str, Any]) -> None:
        self.stdout(json.dumps(self._result_object(step), ensure_ascii=False) + "\n")

    def do_truncated_result(self, step: dict[str, Any]) -> None:
        text = json.dumps(self._result_object(step), ensure_ascii=False)
        data: str | bytes = text if "chars" in step else text.encode("utf-8")
        cut = step["chars"] if "chars" in step else step["bytes"]
        if cut >= len(data):
            raise FakeError("truncated_result: the cut must be shorter than the object")
        self.stdout(data[:cut])

    def do_emit(self, step: dict[str, Any]) -> None:
        line = step["line"]
        text = line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)
        self.stdout(text + ("\n" if step.get("newline", True) else ""))

    def do_stderr(self, step: dict[str, Any]) -> None:
        write_out(sys.stderr, step["text"])

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


def _tool_use_id() -> str:
    return f"toolu_{uuid.uuid4().hex[:24]}"


def _add_agent(payload: dict[str, Any], step: dict[str, Any]) -> None:
    for key in ("agent_id", "agent_type"):
        if key in step:
            payload[key] = step[key]


def write_out(stream: Any, data: str | bytes) -> None:
    """Write UTF-8 bytes to ``stream``'s buffer; a broken pipe is the only error ignored (the
    descriptor then points at /dev/null so the exit flush cannot fail)."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    try:
        stream.buffer.write(raw)
        stream.buffer.flush()
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, stream.fileno())
        os.close(devnull)


def _error(message: str, code: int) -> int:
    write_out(sys.stderr, f"fake claude: {message}\n")
    return code


def main(args: list[str]) -> int:
    # TERM/HUP/INT stay blocked until ``run`` has its handlers in place; a signal before then
    # is handled there (or dropped at exit for calls that never get that far).
    signal.pthread_sigmask(signal.SIG_BLOCK, SIGNUMS)
    made: list[Fake] = []
    try:
        return _main(args, made)
    except (Exception, Signalled) as exc:  # a fake bug must never look like claude's exit 1
        signal.pthread_sigmask(signal.SIG_BLOCK, SIGNUMS)
        for fake in made:  # leave no child behind, whatever escaped ``run``
            fake.handling_signal = True
            with contextlib.suppress(Exception):
                fake.kill_children()
        return _error(f"internal error: {exc!r}", FAKE_ERROR)


def _main(args: list[str], made: list[Fake]) -> int:
    scenario_path = os.environ.get(SCENARIO_ENV)
    try:
        scenario = load_scenario(scenario_path) if scenario_path else None
    except FakeError as exc:
        return _error(str(exc), FAKE_ERROR)
    try:
        argv = parse_argv(args, (scenario or {}).get("extra_flags"))
        if not argv.version:
            read_headless_prompt(argv)
    except ArgvError as exc:
        return _error(f"Error: {exc}", ARGV_ERROR)
    except FakeError as exc:
        return _error(str(exc), FAKE_ERROR)
    if argv.version:
        version = (scenario or {}).get("version") or os.environ.get(VERSION_ENV, DEFAULT_VERSION)
        write_out(sys.stdout, version + "\n")
        return 0
    if scenario is None:
        return _error(f"no scenario: set {SCENARIO_ENV}", FAKE_ERROR)
    try:
        hooks = parse_hooks(load_settings(argv.flags.get("--settings")))
    except FakeError as exc:
        return _error(str(exc), FAKE_ERROR)
    fake = Fake(argv, scenario, hooks, scenario.get("log") or os.environ.get(LOG_ENV))
    made.append(fake)
    return run(fake)


def run(fake: Fake) -> int:
    """Play the scenario. Signals are blocked (``main``) except while steps and the signal's
    own ``SessionEnd`` run, so a signal can only arrive where ``Signalled`` is caught, and one
    pending after the last step still takes its action."""
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
    name: str | None = None
    code: int | None = None
    try:
        try:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNUMS)
            code = fake.run_steps(fake.scenario.get("steps", []))
            signal.pthread_sigmask(signal.SIG_BLOCK, SIGNUMS)
            knob = fake.scenario.get("raise_after_steps")
            if knob is not None:
                os.kill(os.getpid(), SIGNALS[knob])  # pending until unblocked
            name = fake.pending_signal()
        except Signalled as sig:
            name = sig.name
        fake.kill_children()
        if name is not None:
            fake.handling_signal = True  # any further signal is only logged
            signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNUMS)
            code = fake.finish_signal(name)
            signal.pthread_sigmask(signal.SIG_BLOCK, SIGNUMS)
        code = 0 if code is None else code
    except Exception as exc:
        try:
            knob = fake.scenario.get("signal_on_error")
            if knob is not None:  # a signal landing before the next line, made deterministic
                os.kill(os.getpid(), SIGNALS[knob])
                signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNUMS)
            fake.handling_signal = True  # before blocking: a pending signal is only logged
            signal.pthread_sigmask(signal.SIG_BLOCK, SIGNUMS)
        except Signalled:  # its handler already set ``handling_signal``; the error still wins
            signal.pthread_sigmask(signal.SIG_BLOCK, SIGNUMS)
        fake.kill_children()
        fake.log("error", message=repr(exc))
        code = _error(str(exc) if isinstance(exc, FakeError) else repr(exc), FAKE_ERROR)
    action = fake.signals[name] if name is not None else None
    die = isinstance(action, dict) and bool(action.get("die"))
    fake.log("exit", code=code, **({"die": name} if die else {}))
    if die and name is not None:
        with contextlib.suppress(OSError, ValueError):
            sys.stdout.flush()
        signal.signal(SIGNALS[name], signal.SIG_DFL)
        os.kill(os.getpid(), SIGNALS[name])
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {SIGNALS[name]})  # dies here
    return code
