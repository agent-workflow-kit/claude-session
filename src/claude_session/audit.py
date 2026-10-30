"""``audit.jsonl`` writer (interface.md, Audit and log; schemas/audit.json)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import cast

from .logs import JSONValue, append_record, redact, timestamp

ACTIONS = frozenset(
    {"resume_tainted", "forget", "stop", "launch_escalation_override", "sandbox_lowered"}
)
RESULTS = frozenset({"ok", "refused", "error"})
INPUT_KEYS = frozenset(
    {
        "run_kind",
        "kind",
        "run",
        "forked_from",
        "labels",
        "tty",
        "overridden_hooks",
        "sandbox_from",
        "sandbox_to",
        "grace",
    }
)


def append(
    path: str,
    *,
    actor: str,
    target: str,
    action: str,
    result: str,
    decision_ids: Iterable[str] = (),
    inputs: Mapping[str, JSONValue] | None = None,
    secrets: Iterable[str] = (),
    now: datetime | None = None,
) -> dict[str, JSONValue]:
    """Append one audit record (one per session, also for bulk ``forget``) and return it as
    written, after redacting ``secrets`` from every string.

    Raises ``ValueError`` for an unknown action, result or input key, and ``OSError`` when the
    file cannot be written; callers decide whether that fails the command.
    """
    # TODO(#3): callers take the secret_env names from config.
    if action not in ACTIONS:
        raise ValueError(f"unknown audit action: {action!r}")
    if result not in RESULTS:
        raise ValueError(f"unknown audit result: {result!r}")
    if inputs is not None and (unknown := set(inputs) - INPUT_KEYS):
        raise ValueError(f"unknown audit inputs: {sorted(unknown)}")
    secret_set = frozenset(s for s in secrets if s)
    record: dict[str, JSONValue] = {
        "ts": timestamp(now),
        "actor": actor,
        "decision_id": list(decision_ids),
        "target": target,
        "action": action,
        "inputs": None if inputs is None else dict(inputs),
        "result": result,
    }
    redacted = cast(dict[str, JSONValue], redact(record, secret_set))
    append_record(path, redacted, ())
    return redacted
