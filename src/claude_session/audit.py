"""``audit.jsonl`` writer (interface.md, Audit and log; schemas/audit.json).

Text fields are redacted against the secret values; identifiers, enums and timestamps are
validated instead (by hand: the writers never import ``jsonschema``). A record that cannot be
written raises: the caller must not take the audited action (interface.md, Audit and log).
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Iterable, Mapping
from datetime import datetime

from .logs import (
    JSONValue,
    append_record,
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
    if grace is not None and (
        isinstance(grace, bool) or not isinstance(grace, int | float) or not math.isfinite(grace)
    ):
        raise ValueError("audit input grace must be a finite number or null")
    return checked


def _redact_label(label: str, secrets: Collection[str]) -> str | None:
    """A label as written: unchanged, ``<key>=redacted`` when it contains a secret (so it still
    matches the label grammar), or None (dropped) when its key contains one."""
    if not leaks_secret(label, secrets):
        return label
    key = label.partition("=")[0]
    if leaks_secret(key, secrets):
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
    A label containing a secret is written as ``<key>=redacted``, and dropped if its key
    contains one.

    Raises ``ValueError`` for a value the schema does not allow and ``OSError`` when the file
    cannot be written. Never swallow either: the audited action must not be taken without its
    record.
    """
    # TODO(#3): callers build secrets with logs.secret_values from config's secret_env and
    # claude_auth_env names (and the run secret where they have one).
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
