"""``verify-secret`` (interface.md, Internal commands): whether a presented secret is the
current-run secret of a session whose current run is adopted (wrapper set) and live (not ended,
and its wrapper or its recorded child confirmed alive).

Every way of not matching (unknown or forgotten session, an id that is not a full id, a pending
or ended run, dead processes, no recorded secret, a wrong or malformed secret) gives the same
answer and does the same work: one lookup, one hash, one compare and two liveness probes, so
neither the answer nor its timing is an oracle for session ids or run states. The presented
secret is hashed (``sha256`` of its ASCII text, as ``sessiondir`` records it in
``runs.secret_sha256``) and the digests are compared with ``hmac.compare_digest``. The secret
is never logged, printed or put in an error.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import BinaryIO

from . import ids
from .store import MAX_PID, Store

# A run secret is 32 random bytes as 64 lowercase hex characters (design.md, Identifiers).
SECRET = re.compile(rb"[0-9a-f]{64}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
# Input beyond this (before stripping) is never a secret; reading stops there.
MAX_INPUT_BYTES = 4096
# Compared against when there is no stored digest, so the compare always runs.
_NO_DIGEST = "0" * 64
# Looked up for an id that is not a full id, so the lookup always runs; it can never be a
# session id ("0" is not in the base32 alphabet), and the result is discarded anyway.
_NO_SESSION = "cs-00000000"
# Probed when there is no process to probe. The PID passes every range check but names no
# process: Linux PIDs are below pid_max, which is at most PID_MAX_LIMIT (2**22), and macOS
# PIDs stay below 100000. So the probe takes the same "no such process" path as a dead
# wrapper or child, and the token never matches a real one.
_STAND_IN_PID = 2**22
_STAND_IN_START = "verify-secret:stand-in"


def read_secret(stream: BinaryIO) -> bytes | None:
    """The presented secret: at most ``MAX_INPUT_BYTES`` of ``stream`` with trailing ASCII
    whitespace stripped; None when the input is longer or cannot be read."""
    try:
        data = stream.read(MAX_INPUT_BYTES + 1)
    except OSError:
        return None
    if not isinstance(data, bytes) or len(data) > MAX_INPUT_BYTES:
        return None
    return data.rstrip()


def _probe(st: Store, pid: int | None, start: str | None) -> bool:
    """Whether the process ``(pid, start)`` is known to run (``Store.known_liveness``: "cannot
    tell" is not alive). Without a usable PID or start token the process is not alive, and a
    stand-in is probed instead, so every call does the same work."""
    if pid is not None and 0 < pid <= MAX_PID and start is not None and start != "":
        usable, target = True, (pid, start)
    else:
        usable, target = False, (_STAND_IN_PID, _STAND_IN_START)
    alive = bool(st.known_liveness(*target))
    return usable & alive


def matches(st: Store, csession_id: str, presented: bytes | None) -> bool:
    """Whether ``presented`` is the current-run secret of ``csession_id`` (a full id: no
    prefix matching; ASCII case-insensitive) and that run is adopted and live.

    Live: the run has not ended and its wrapper or its recorded child is known to run (PID and
    start token); when liveness cannot be told, the check fails closed. Every path does the
    same work and the conditions are combined only at the end, without short-circuiting.
    """
    candidate = presented if presented is not None else b""
    digest = hashlib.sha256(candidate).hexdigest()
    # Non-ASCII text is not folded: a look-alike such as KELVIN SIGN must not become "k".
    sid = csession_id.lower() if csession_id.isascii() else csession_id
    id_ok = ids.is_session_id(sid)
    run = st.secret_run(sid if id_ok else _NO_SESSION)
    stored = run.secret_sha256 if run is not None else None
    digest_ok = stored is not None and _DIGEST.fullmatch(stored) is not None
    # TODO(#33): switch to the shared liveness predicate once it lands, keeping fail-closed
    # semantics and the constant-work shape (R1-2).
    wrapper_alive = _probe(
        st,
        run.wrapper_pid if run is not None else None,
        run.wrapper_start if run is not None else None,
    )
    child_alive = _probe(
        st,
        run.child_pid if run is not None else None,
        run.child_start if run is not None else None,
    )
    same = hmac.compare_digest(digest, stored if digest_ok and stored is not None else _NO_DIGEST)
    well_formed = presented is not None and SECRET.fullmatch(presented) is not None
    return bool(
        same & well_formed & id_ok & (run is not None) & digest_ok & (wrapper_alive | child_alive)
    )
