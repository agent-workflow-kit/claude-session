"""``audit.jsonl`` writer and record parser (interface.md, Audit and log; schemas/audit.json).

Text fields are redacted against the secret values; identifiers, enums and timestamps are
validated instead (by hand: the writers never import ``jsonschema``). A record that cannot be
written raises: the caller must not take the audited action (interface.md, Audit and log).
``parse_record`` reads one line back for ``export``, accepting only what the schema allows.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Collection, Iterable, Mapping
from datetime import datetime
from typing import Any

from .logs import (
    JSONValue,
    append_record,
    first_secret_index,
    is_csession_id,
    is_int,
    leaks_secret,
    redact_text,
    timestamp,
    usable_secrets,
)

ACTIONS = frozenset(
    {"resume_tainted", "forget", "stop", "launch_escalation_override", "sandbox_lowered"}
)
RESULTS = frozenset({"ok", "refused", "error"})
SANDBOX_LEVELS = frozenset({"required", "preferred", "off"})
# schemas/audit.json inputs.grace: seconds, at most 2**53 (exact as a JSON number).
GRACE_MIN = 0
GRACE_MAX = 2**53
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
# interface.md, Identifiers (and schemas/audit.json inputs.labels).
LABEL = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}=[A-Za-z0-9._:/#@+=,-]{0,256}")
LABEL_REDACTED_VALUE = "redacted"
# interface.md, Identifiers: kind names.
KIND_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def _string_list(name: str, value: object) -> list[str]:
    if isinstance(value, str | bytes | Mapping) or not isinstance(value, Iterable):
        raise ValueError(f"{name} must be a list of strings")
    items = list(value)
    if not all(isinstance(item, str) for item in items):
        raise ValueError(f"{name} must be a list of strings")
    return items


def _optional_kind(key: str, value: object) -> None:
    if value is not None and not (isinstance(value, str) and KIND_NAME.fullmatch(value)):
        raise ValueError(f"audit input {key} must be a kind name or null")


def _check_inputs(inputs: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    if not isinstance(inputs, Mapping):
        raise ValueError("audit inputs must be a mapping or None")
    if unknown := set(inputs) - INPUT_KEYS:
        raise ValueError(f"unknown audit inputs ({len(unknown)})")
    checked = dict(inputs)
    for key in ("run_kind", "kind"):
        _optional_kind(key, checked.get(key))
    run = checked.get("run")
    if run is not None and not is_int(run):
        raise ValueError("audit input run must be an integer or null")
    forked_from = checked.get("forked_from")
    if forked_from is not None and not is_csession_id(forked_from):
        raise ValueError("audit input forked_from is not a csession_id")
    if "labels" in checked:
        labels = _string_list("audit input labels", checked["labels"])
        if not all(LABEL.fullmatch(label) for label in labels):
            raise ValueError("audit input labels: malformed label")
        checked["labels"] = list(labels)
    if "tty" in checked and not isinstance(checked["tty"], bool):
        raise ValueError("audit input tty must be a boolean")
    if "overridden_hooks" in checked:
        hooks = _string_list("audit input overridden_hooks", checked["overridden_hooks"])
        checked["overridden_hooks"] = list(hooks)
    for key in ("sandbox_from", "sandbox_to"):
        level = checked.get(key)
        if level is not None and level not in SANDBOX_LEVELS:
            raise ValueError(f"audit input {key} must be one of {sorted(SANDBOX_LEVELS)}")
    grace = checked.get("grace")
    # Only a float can be NaN or infinite; the bounds check compares without converting a
    # huge int to a float or a string, and the message never echoes the value.
    if grace is not None:
        finite = is_int(grace) or (isinstance(grace, float) and math.isfinite(grace))
        if not (isinstance(grace, int | float) and finite):
            raise ValueError("audit input grace must be a finite number or null")
        if not GRACE_MIN <= grace <= GRACE_MAX:
            raise ValueError("audit input grace is out of range")
    return checked


def _redact_label(label: str, secrets: Collection[str]) -> str | None:
    """A label as written: unchanged without a secret; ``<key>=redacted`` (so it still matches
    the label grammar) when every secret occurrence lies wholly inside the value; None
    (dropped) when an occurrence starts in the key or at the ``=``, or when
    ``<key>=redacted`` would still contain a secret."""
    if not leaks_secret(label, secrets):
        return label
    key = label.partition("=")[0]
    first = first_secret_index(label, secrets)
    if first == -1 or first <= len(key):
        return None
    redacted = f"{key}={LABEL_REDACTED_VALUE}"
    return None if leaks_secret(redacted, secrets) else redacted


def _redact_inputs(inputs: dict[str, JSONValue], secrets: Collection[str]) -> dict[str, JSONValue]:
    out = dict(inputs)
    if "overridden_hooks" in out:
        hooks = _string_list("overridden_hooks", out["overridden_hooks"])
        out["overridden_hooks"] = [redact_text(h, secrets) for h in hooks]
    if "labels" in out:
        labels = (_redact_label(label, secrets) for label in _string_list("labels", out["labels"]))
        out["labels"] = [label for label in labels if label is not None]
    return out


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
    written.

    ``secrets`` (build them with ``logs.secret_values``; values shorter than
    ``logs.MIN_SECRET_CHARS`` are ignored) are redacted from the text fields: ``actor``,
    ``decision_id`` and the ``overridden_hooks`` and ``labels`` inputs. ``run_kind`` and
    ``kind`` are kind names: validated, never redacted.
    A label containing a secret is written as ``<key>=redacted`` when every occurrence lies
    wholly inside its value; it is dropped when an occurrence starts in its key or at the
    ``=`` (it would reveal part of the secret), or when ``<key>=redacted`` would still contain
    one.

    Raises ``ValueError`` for a value the schema does not allow and ``OSError`` when the file
    cannot be written. Never swallow either: the audited action must not be taken without its
    record.
    """
    # Callers pass the values of the configured secret_env and claude_auth_env variables
    # (envbuild.secret_values; exec's launch does) and the run secret where they have one.
    if action not in ACTIONS:
        raise ValueError("unknown audit action")
    if result not in RESULTS:
        raise ValueError("unknown audit result")
    if not is_csession_id(target):
        raise ValueError("audit target is not a csession_id")
    if not isinstance(actor, str):
        raise ValueError("audit actor must be a string")
    ids = _string_list("decision_ids", decision_ids)
    checked = None if inputs is None else _check_inputs(inputs)
    secret_set = usable_secrets(secrets)
    record: dict[str, JSONValue] = {
        "ts": timestamp(now),
        "actor": redact_text(actor, secret_set),
        "decision_id": [redact_text(i, secret_set) for i in ids],
        "target": target,
        "action": action,
        "inputs": None if checked is None else _redact_inputs(checked, secret_set),
        "result": result,
    }
    append_record(path, record)
    return record


# Reading records back (export).

RECORD_KEYS = frozenset({"ts", "actor", "decision_id", "target", "action", "inputs", "result"})
# RFC 3339 date-time (schemas/audit.json ``format: date-time``) in the form ``fromisoformat``
# also reads: ASCII digits, upper-case ``T`` and ``Z``, an optional fraction, a zone always.
TS = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)


class _Malformed(ValueError):
    pass


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _Malformed("duplicate key")
    return dict(pairs)


def _no_constant(name: str) -> Any:
    raise _Malformed(f"{name} is not JSON")


def parse_ts(text: object) -> datetime:
    """The timezone-aware time of a record's ``ts``; ``ValueError`` unless it is an RFC 3339
    date-time with a zone (a leap second is refused: ``datetime`` cannot hold it)."""
    if not isinstance(text, str) or not TS.fullmatch(text):
        raise ValueError("ts is not an RFC 3339 date-time")
    return datetime.fromisoformat(text)  # Python 3.12 or later: fraction digits beyond 6 cut


def check_record(value: object) -> dict[str, JSONValue]:
    """``value`` (parsed JSON) as an audit record, or ``ValueError`` when schemas/audit.json
    does not allow it: exactly the record keys, every value as the writer validates it."""
    if not isinstance(value, dict) or set(value) != RECORD_KEYS:
        raise ValueError("not an audit record")
    parse_ts(value["ts"])
    if not isinstance(value["actor"], str):
        raise ValueError("audit actor must be a string")
    ids = value["decision_id"]
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ValueError("decision_id must be a list of strings")
    if not is_csession_id(value["target"]):
        raise ValueError("audit target is not a csession_id")
    if value["action"] not in ACTIONS:
        raise ValueError("unknown audit action")
    if value["result"] not in RESULTS:
        raise ValueError("unknown audit result")
    inputs = value["inputs"]
    if inputs is not None:
        if not isinstance(inputs, dict):
            raise ValueError("audit inputs must be an object or null")
        _check_inputs(inputs)
    return value


def parse_record(line: bytes) -> dict[str, JSONValue] | None:
    """One ``audit.jsonl`` line (without its newline) as a record, or None when it is not one
    the schema allows: not strict UTF-8, not JSON, NaN or an infinity, a duplicate key, nested
    too deep, or a value schemas/audit.json refuses. Never raises for any input."""
    try:
        value = json.loads(
            line.decode("utf-8"),
            object_pairs_hook=_unique_keys,
            parse_constant=_no_constant,
        )
        return check_record(value)
    except (ValueError, RecursionError):  # UnicodeDecodeError and JSONDecodeError included
        return None
