"""Session ids, id-prefix resolution and the name grammars of interface.md, Identifiers.

Pure functions: the store supplies the "id is used" check for allocation and the candidates for
prefix resolution. Standard library only (plus ``errors``), so any module may import it.
"""

from __future__ import annotations

import base64
import re
import secrets
import string
from collections.abc import Callable, Iterable
from typing import NamedTuple
from urllib.parse import urlsplit

from .errors import AmbiguousError, InternalError, NotFoundError, UsageError

ID_PREFIX = "cs-"
ID_BODY_LENGTH = 8  # base32 characters: 40 random bits
MIN_PREFIX_LENGTH = 4  # characters after cs-
MAX_ALLOCATION_ATTEMPTS = 16

# `fullmatch` everywhere: `$` would also match before a trailing newline.
SESSION_ID = re.compile(r"cs-[a-z2-7]{8}")
KIND_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
PERMISSION_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
SKILL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}")
ARTIFACT_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")
LABEL_KEY = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
LABEL_VALUE = re.compile(r"[A-Za-z0-9._:/#@+=,-]{0,256}")
# Prefix input after `cs-`: what any id prefix of at least MIN_PREFIX_LENGTH can look like.
ID_PREFIX_BODY = re.compile(r"[a-z2-7]{4,8}")
# Same grammar as schemas/config.json `repos` propertyNames (tests check the two are equal),
# applied with fullmatch, so a trailing newline never passes.
REPO_ID = re.compile(
    r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?(/(?!\.\.?(/|$))[A-Za-z0-9._-]+)+$"
)
# Default ports dropped when deriving a repo id from an origin URL.
DEFAULT_PORTS = {"http": 80, "https": 443, "ssh": 22, "git+ssh": 22, "ssh+git": 22, "git": 9418}
# scp-like `[user@]host:path`: no slash before the colon.
# git splits these at the first ':', so the user part holds no ':' (else: no repo id).
_SCP_ORIGIN = re.compile(r"(?:[^@/:]+@)?(?P<host>[^@/:]+):(?P<path>.*)")
# Space, C0 controls and DEL: never part of an origin (the caller trims git's newline).
_ORIGIN_UNSAFE = re.compile(r"[\x00-\x20\x7f]")
# Id input folds ASCII letters only (str.lower would map e.g. KELVIN SIGN to k).
_ASCII_FOLD = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_GIT_SUFFIX = re.compile(r"\.git$", re.IGNORECASE)


# Allocation


def new_session_id(
    is_used: Callable[[str], bool],
    *,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    max_attempts: int = MAX_ALLOCATION_ATTEMPTS,
) -> str:
    """Allocate a ``cs-`` id that ``is_used`` rejects for none of: live, forgotten or deleted
    sessions, retrying on collision. Pass ``Store.session_id_used`` (it checks ``sessions``
    and ``deleted_ids``) inside the transaction that inserts the session."""
    for _ in range(max_attempts):
        body = base64.b32encode(random_bytes(5)).decode("ascii").lower()
        candidate = ID_PREFIX + body
        if not is_session_id(candidate):
            raise InternalError(f"random source gave a malformed session id: {candidate!r}")
        if not is_used(candidate):
            return candidate
    raise InternalError(f"could not allocate a session id after {max_attempts} attempts")


def is_session_id(text: str) -> bool:
    """A full session id (``verify-secret`` requires one)."""
    return SESSION_ID.fullmatch(text) is not None


# Prefix resolution


class IdCandidate(NamedTuple):
    """A session the store offers for prefix resolution."""

    csession_id: str
    forgotten: bool = False


def session_id_prefix(text: str, flag: str | None = None) -> str:
    """Normalize session id input (ASCII case-insensitive) to a lowercase ``cs-`` prefix; fewer
    than 4 characters after ``cs-`` is a usage error (exit 2). Malformed input is not rejected
    here: ``resolve_session_id`` reports it as unknown."""
    folded = text.translate(_ASCII_FOLD)  # one character for one: lengths are the input's
    body = folded.removeprefix(ID_PREFIX)
    if len(body) < MIN_PREFIX_LENGTH:
        where = f"{flag}: " if flag else ""
        raise UsageError(
            f"{where}session id {text!r} is too short: give at least {MIN_PREFIX_LENGTH} "
            f"characters after {ID_PREFIX}"
        )
    return ID_PREFIX + body


def resolve_session_id(text: str, candidates: Iterable[IdCandidate]) -> str:
    """The one non-forgotten session whose id starts with ``text`` (``cs-`` optional).

    Raises ``UsageError`` (exit 2) for a short prefix, ``NotFoundError`` (exit 10) when nothing
    matches (forgotten sessions never do) and ``AmbiguousError`` (exit 21) listing the sorted
    candidates when several match.
    """
    prefix = session_id_prefix(text)
    if ID_PREFIX_BODY.fullmatch(prefix.removeprefix(ID_PREFIX)) is None:
        raise NotFoundError(f"unknown session: {text}")  # can match no id; no lookup
    matches = sorted(
        {c.csession_id for c in candidates if not c.forgotten and c.csession_id.startswith(prefix)}
    )
    if not matches:
        raise NotFoundError(f"unknown session: {text}")
    if len(matches) > 1:
        raise AmbiguousError(f"ambiguous session id: {text}", matches=matches)
    return matches[0]


def lookup_session_id(text: str, find: Callable[[str], Iterable[tuple[str, bool]]]) -> str:
    """``resolve_session_id`` against a store: ``find`` (``Store.session_id_candidates``)
    is called with the normalized ``cs-`` prefix only once the input can match an id, so a
    short or malformed input never reaches a query."""
    prefix = session_id_prefix(text)
    if ID_PREFIX_BODY.fullmatch(prefix.removeprefix(ID_PREFIX)) is None:
        raise NotFoundError(f"unknown session: {text}")
    return resolve_session_id(
        text, (IdCandidate(sid, forgotten) for sid, forgotten in find(prefix))
    )


# Name grammars


def is_kind_name(name: str) -> bool:
    return KIND_NAME.fullmatch(name) is not None


def is_permission_profile_name(name: str) -> bool:
    # TODO(#22): enforce on --permission-profile names.
    return PERMISSION_PROFILE_NAME.fullmatch(name) is not None


def is_skill_name(name: str) -> bool:
    return SKILL_NAME.fullmatch(name) is not None


def is_artifact_name(name: str) -> bool:
    """One path segment (artifact names and ``--input-file`` basenames), not ``.`` or ``..``."""
    return name not in (".", "..") and ARTIFACT_NAME.fullmatch(name) is not None


def parse_label(text: str) -> tuple[str, str]:
    """Split ``key=value`` at the first ``=``; raise ``ValueError`` naming the broken part."""
    key, sep, value = text.partition("=")
    if not sep:
        raise ValueError("expected key=value")
    if LABEL_KEY.fullmatch(key) is None:
        raise ValueError(f"key must match ^{LABEL_KEY.pattern}$")
    if LABEL_VALUE.fullmatch(value) is None:
        raise ValueError(f"value must match ^{LABEL_VALUE.pattern}$")
    return key, value


def is_repo_id(text: str) -> bool:
    """``host[:port]/path`` exactly as schemas/config.json defines a repo id."""
    return REPO_ID.fullmatch(text) is not None


def repo_id_from_origin(origin: str) -> str | None:
    """Normalize an ``origin`` URL to a repo id, or None when it does not yield a valid one
    (local paths, ``file://``, unknown schemes, no path)."""
    # TODO(#9): call from the --repo derivation (git calls go through the gateway).
    if not origin.isascii() or _ORIGIN_UNSAFE.search(origin):
        return None  # a repo id is ASCII; str.lower would fold e.g. KELVIN SIGN into k
    if "://" in origin:
        try:
            parts = urlsplit(origin)
            port = parts.port
        except ValueError:
            return None
        scheme = parts.scheme.translate(_ASCII_FOLD)
        if scheme not in DEFAULT_PORTS or not parts.hostname:
            return None
        host = parts.hostname
        if port is not None and port != DEFAULT_PORTS[scheme]:
            host = f"{host}:{port}"
        path = parts.path
    else:
        match = _SCP_ORIGIN.fullmatch(origin)
        if match is None:
            return None
        host = match.group("host")
        path = match.group("path")
    path = _GIT_SUFFIX.sub("", path.strip("/"))
    repo = f"{host.translate(_ASCII_FOLD)}/{path}"
    return repo if is_repo_id(repo) else None
