"""``verify-secret`` (interface.md, Internal commands): whether a presented secret is the
current-run secret of a session whose current run is adopted (wrapper set) and live.

Every way of not matching (unknown or forgotten session, an id that is not a full id, a pending
or ended run, a dead wrapper, no recorded secret, a wrong or malformed secret) gives the same
answer, so the command is no oracle for session ids or run states. The presented secret is
hashed (``sha256`` of its ASCII text, as ``sessiondir`` records it in ``runs.secret_sha256``)
and the digests are compared with ``hmac.compare_digest``, once per request whatever the
outcome. The secret is never logged, printed or put in an error.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import BinaryIO

from . import ids
from .store import Store

# A run secret is 32 random bytes as 64 lowercase hex characters (design.md, Identifiers).
SECRET = re.compile(rb"[0-9a-f]{64}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
# Input beyond this (before stripping) is never a secret; reading stops there.
MAX_INPUT_BYTES = 4096
# Compared against when there is no stored digest, so the compare always runs.
_NO_DIGEST = "0" * 64


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


def matches(st: Store, csession_id: str, presented: bytes | None) -> bool:
    """Whether ``presented`` is the current-run secret of ``csession_id`` (a full id, exactly
    as stored: no prefix matching and no case folding) and that run is adopted and live.

    The wrapper must be known to run (``Store.known_liveness``): when its liveness cannot be
    told, the check fails closed.
    """
    candidate = presented if presented is not None else b""
    digest = hashlib.sha256(candidate).hexdigest()
    expected: str | None = None
    if ids.is_session_id(csession_id):
        run = st.secret_run(csession_id)
        if (
            run is not None
            and _DIGEST.fullmatch(run.secret_sha256) is not None
            and st.known_liveness(run.wrapper_pid, run.wrapper_start)
        ):
            expected = run.secret_sha256
    same = hmac.compare_digest(digest, expected if expected is not None else _NO_DIGEST)
    well_formed = presented is not None and SECRET.fullmatch(presented) is not None
    return same and well_formed and expected is not None
