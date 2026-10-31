"""``--allow-tainted``: the human-only check and the ``resume_tainted`` audit record
(interface.md, Taint, Launch order step 2 and Audit and log; design.md, Overrides and actors).

A session is tainted when a third-party-reading run is allocated (``Store.allocate``,
``Store.allocate_resume``); the taint is never removed and a fork copies it. A tainted
session resumed or forked into a run that is not third-party-reading is refused ``tainted``
at every point that decides it (``store._refuse_tainted``): the allocation, the adoption
compare-and-set and the check right before step 7. ``--allow-tainted`` lifts that refusal:

- **Human-only** (launch step 2, exit 2; ``check_human_only``): an explicit ``--actor`` must
  be ``human:<user>``; without one, stdin must be a terminal (the default actor is then
  ``human:<me.name>``). An explicit automated actor is refused even with a terminal: the
  caller said it is not a human. An audited honor system, not authentication.
- **Recorded on the run** (``Request.allow_tainted``, stored in the run row's
  ``invocation_json``, never only in the launch spec file): the store reads it from there at
  every refusal point, so the flag of ``start --resume`` reaches its adoption, and nothing
  outside ``state.db`` can turn it on.
- **Audited** (``audit_resume_tainted``): one ``resume_tainted`` record (``result: ok``) per
  run that goes ahead only because of the flag, written right before step 7 (the adopting
  ``exec`` for ``start --resume``: "audited at adoption"), after the last taint check. A
  record that cannot be written ends the run ``not-started`` (``launch_interrupted``,
  exit 12) before ``claude`` is spawned: the override never takes effect unaudited. A run
  that then ends before ``claude`` is spawned (step 7 or the spawn fails, a signal) gets a
  best-effort follow-up record of the same action with ``result: error``.
- **Not audited**: a refusal. A human-only check that fails is a usage error (exit 2,
  before the lookup), and a tainted refusal without the flag (exit 20) is not a use of the
  override.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from . import audit
from .errors import UsageError

HUMAN_PREFIX = "human:"
ACTION = "resume_tainted"
FLAG = "--allow-tainted"


@dataclass(frozen=True)
class AllowTainted:
    """``--allow-tainted`` given, and whether stdin was a terminal when the human-only check
    passed (the audit record's ``inputs.tty``)."""

    tty: bool

    def invocation(self) -> dict[str, Any]:
        """The value stored under ``store.ALLOW_TAINTED`` in the run's ``invocation_json``."""
        return {"tty": self.tty}

    @classmethod
    def from_invocation(cls, invocation: Any) -> AllowTainted | None:
        """The flag as the run row recorded it (``Store`` reads it the same way); anything
        malformed counts as not given."""
        if not isinstance(invocation, dict):
            return None
        value = invocation.get("allow_tainted")
        if not isinstance(value, dict) or not isinstance(value.get("tty"), bool):
            return None
        return cls(tty=value["tty"])


def stdin_is_tty() -> bool:
    try:
        return sys.stdin is not None and bool(sys.stdin.isatty())
    except (OSError, ValueError):
        return False


def is_human_actor(actor: str) -> bool:
    """``human:<user>`` with a non-blank user."""
    return actor.startswith(HUMAN_PREFIX) and actor[len(HUMAN_PREFIX) :].strip() != ""


def check_human_only(flag: str, actor: str | None, *, tty: bool) -> None:
    """A human-only flag (launch step 2): an explicit ``--actor`` must be ``human:<user>``;
    without one, stdin must be a terminal. ``UsageError`` (exit 2) otherwise."""
    if actor is not None:
        if not is_human_actor(actor):
            raise UsageError(
                f"{flag} is human-only: --actor must be human:<user>",
                detail="run it from a terminal, or pass --actor human:<user>",
            )
        return
    if not tty:
        raise UsageError(
            f"{flag} is human-only: it needs a terminal on stdin or an explicit"
            " --actor human:<user>",
            detail="run it from a terminal, or pass --actor human:<user>",
        )


def from_args(args: Any) -> AllowTainted | None:
    """``--allow-tainted`` of ``resume`` or ``start --resume`` after the human-only check
    (``UsageError``, exit 2); None when it is not given."""
    if not getattr(args, "allow_tainted", False):
        return None
    tty = stdin_is_tty()
    check_human_only(FLAG, getattr(args, "actor", None), tty=tty)
    return AllowTainted(tty=tty)


def audit_resume_tainted(
    state_root: str,
    audit_name: str,
    *,
    actor: str,
    decision_ids: Sequence[str],
    target: str,
    run: int,
    run_kind: str,
    forked_from: str | None,
    allow: AllowTainted,
    secrets: Sequence[str],
) -> dict[str, Any]:
    """The ``resume_tainted`` record of a run that goes ahead only because of
    ``--allow-tainted``; returns its inputs (an ``error`` follow-up repeats them when the run
    then ends before ``claude`` is spawned: ``launch.audit_launch_errors``). Raises
    (``OSError``, ``ValueError``) when it cannot be written: the run must not launch without
    it."""
    inputs: dict[str, Any] = {
        "run_kind": run_kind,
        "run": run,
        "forked_from": forked_from,
        "tty": allow.tty,
    }
    audit.append(
        os.path.join(state_root, audit_name),
        actor=actor,
        target=target,
        action=ACTION,
        result="ok",
        decision_ids=decision_ids,
        inputs=inputs,
        secrets=secrets,
    )
    return inputs
