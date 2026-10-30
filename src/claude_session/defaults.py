"""Built-in config defaults: every key of config.example.yaml with its value, in the same raw
form (duration strings, YAML-shaped dicts and lists).

``config`` merges the config files over this; tests check that it equals config.example.yaml
(interface.md, Config). Standard library only. Treat it as read-only: callers deep-copy it.
"""

from __future__ import annotations

from typing import Any


def _env(*, inherit: bool, set_: dict[str, str] | None = None) -> dict[str, Any]:
    return {"inherit": inherit, "allow": [], "set": set_ or {}, "path_prepend": []}


def _kind(**fields: Any) -> dict[str, Any]:
    """A kind profile: the trusted, unsandboxed fields, with ``fields`` replacing some."""
    kind: dict[str, Any] = {
        "permission_profile": "default",
        "sandbox": "off",
        "reads_third_party": False,
        "implicit_done": True,
        "setting_sources": ["user", "project", "local"],
        "mcp": "inherit",
        "cwd": "caller",
        "env": _env(inherit=True),
        "read_dirs": [],
        "write_dirs": [],
        "timeout": "60m",
        "interactive_timeout": None,
        "max_turns": None,
        "auto_exit": True,
        "backstop": True,
        "notes_context": True,
        "model": None,
        "claude_args": [],
    }
    unknown = set(fields) - set(kind)
    assert not unknown, unknown
    kind.update(fields)
    return kind


def _isolated(**fields: Any) -> dict[str, Any]:
    """A kind that reads third-party content: sandboxed, no user settings, no MCP."""
    base: dict[str, Any] = {
        "permission_profile": "review",
        "sandbox": "required",
        "reads_third_party": True,
        "setting_sources": [],
        "mcp": "none",
        "cwd": "session-dir",
        "env": _env(inherit=False),
        "notes_context": False,
    }
    return _kind(**{**base, **fields})


# Deny rules shared by the built-in readonly, review and respond profiles.
_NO_EXTERNAL_TOOLS = [
    "Bash(gh:*)",
    "Bash(glab:*)",
    "Bash(agent-spawn:*)",
    "Bash(memoctl add:*)",
]
_NO_NETWORK_TOOLS = ["WebFetch", "WebSearch", "mcp__*"]
_SUBAGENTS = ["Agent", "Task", "Skill"]
_NO_NETWORK_SANDBOX = {"network": {"allowedDomains": []}}

# TODO(#9): kinds.py should reuse BUILTIN_KINDS rather than keep a second built-in kinds table.
BUILTIN_KINDS: dict[str, dict[str, Any]] = {
    "implement": _kind(),
    "draft-ticket": _kind(),
    "audit": _kind(notes_context=False),
    "improve": _kind(notes_context=False),
    "rank": _isolated(
        permission_profile="readonly",
        sandbox="preferred",
        reads_third_party=False,
        implicit_done=False,
        timeout="15m",
    ),
    "review": _isolated(timeout="120m"),
    "gate-check": _isolated(implicit_done=False, timeout="15m"),
    "respond": _isolated(
        permission_profile="respond",
        cwd="caller",
        env=_env(
            inherit=False,
            set_={
                "GIT_CONFIG_GLOBAL": "${CSESSION_DIR}/empty-gitconfig",
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        ),
    ),
}

BUILTIN_PERMISSION_PROFILES: dict[str, dict[str, Any]] = {
    "default": {},
    "readonly": {
        "permissions": {"allow": _SUBAGENTS, "deny": _NO_NETWORK_TOOLS + _NO_EXTERNAL_TOOLS},
    },
    "review": {
        "permissions": {"allow": _SUBAGENTS, "deny": _NO_NETWORK_TOOLS + _NO_EXTERNAL_TOOLS},
        "sandbox": _NO_NETWORK_SANDBOX,
    },
    "respond": {
        "permissions": {
            "allow": ["Bash", "Read", "Edit(/${CWD}/**)", "Write(/${CWD}/**)", *_SUBAGENTS],
            "deny": [
                "Bash(git push:*)",
                "Bash(git remote:*)",
                "Bash(git config:*)",
                "Bash(git credential:*)",
                *_NO_EXTERNAL_TOOLS,
                *_NO_NETWORK_TOOLS,
            ],
        },
        "sandbox": _NO_NETWORK_SANDBOX,
    },
}

BUILTIN: dict[str, Any] = {
    "claude_bin": "claude",
    "min_claude_version": "2.1.312",
    "default_kind": "implement",
    "claude_auth_env": [],
    "secret_env": [],
    "notes_context_cmd": ["memoctl", "context"],
    "notes_context_timeout": "5s",
    "notes_context_max_bytes": 16384,
    "notes_context_sources": ["startup"],
    "summary_max_bytes": 65536,
    "artifact_max_bytes": 10485760,
    "input_max_bytes": 10485760,
    "retention_days": 30,
    "lost_grace": "10m",
    "auto_exit_delay": "2s",
    "kill_grace": "10s",
    "poll_interval": "0.5s",
    "session_start_timeout": "120s",
    "hooks": {"pre-launch": [], "post-state-change": []},
    "me": {},
    "kinds": BUILTIN_KINDS,
    "permission_profiles": BUILTIN_PERMISSION_PROFILES,
    "repos": {},
}
