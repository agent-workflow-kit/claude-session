"""Path rules of a launch (interface.md, Isolation: Session-writable paths and Path rules;
design.md, cwd resolution and Security notes).

Every rule compares resolved paths: the realpath of the path (``os.path.realpath`` resolves
the existing part and keeps the rest lexically, which is the realpath of the nearest existing
parent plus the remainder). Two more defenses back the lexical comparison:

- **Identity.** A path is also inside a directory when the directory's ``(st_dev, st_ino)``
  is the identity of the path or of one of its existing ancestors. A bind mount (or any
  second name of a directory that realpath cannot see through) therefore cannot hide that
  a path is inside a refused or session-writable directory.
- **Case.** On macOS (case-insensitive by default) paths are also compared case-folded, so
  ``~/.SSH`` is ``~/.ssh``; for existing paths the identity check covers it as well. The
  exceptions (the session's own directories) compare exactly, so a case variant of them
  gets no exception.

The checks raise ``errors.LaunchValidationError`` with the end reason interface.md names
(``unsafe_cwd``, ``unsafe_path``, ``unsafe_plugin_dir``, ``unsafe_read_dir``,
``unsafe_claude_args``, ``plugin_dir_missing``; ``InvalidPermissionProfile`` for profile,
``read_dirs`` and ``write_dirs`` entries); an isolated run's ``--outcome-file`` under a
session-writable path is a ``UsageError`` (exit 2). Messages name paths, never profile
values other than the offending path.

TODO(#15): exec builds ``Host`` and ``PathRules`` at launch step 5 and calls, for isolated
runs, ``check_host`` first (the write checks assume a valid ``$HOME``), ``check_cwd`` (before
``permission_profiles.expand``), ``resolve_dirs`` and ``resolve_write_dirs``, builds
``session_writable``, then ``check_read_dirs``, ``check_write_dirs`` and ``check_profile``
(with it), ``check_plugin_dirs``, ``check_generated`` (every generated allowRead entry, the
git directory included), ``check_claude_args``, and passes ``session_writable`` to
``permission_profiles.select``, ``envbuild.build``, ``resolve_tool`` (``claude_bin``,
``bwrap``, ``socat``, ``git`` on the sanitized PATH) and
``settings_builder.check_interpreter``; trusted runs call ``check_profile`` (refused paths
only). #28 repeats them at adoption.
"""

from __future__ import annotations

import os
import pwd
import re
import stat
import sys
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from . import sessiondir
from .errors import InvalidPermissionProfile, LaunchValidationError, UsageError

# Credential locations under $HOME a read entry must neither contain nor be inside
# (interface.md, Path rules: read_dirs and plugin directories).
CREDENTIAL_PATHS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".config",
    ".claude",
    ".netrc",
    ".git-credentials",
    ".local/state",
    ".local/share/keyrings",
    ".cargo/credentials",
    ".cargo/credentials.toml",
    ".gem/credentials",
    ".gradle/gradle.properties",
    ".m2/settings.xml",
    ".m2/settings-security.xml",
)
# The fixed system directories every isolated run may read (settings_builder lists them in
# allowRead); a generated entry inside one is exempt from the caller's-cwd rule.
SYSTEM_READ_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc")
# Symlinks followed while resolving one path before it counts as a loop (as the kernel).
MAX_SYMLINKS = 40
# The only claude_args isolated runs accept (exact tokens; interface.md, Path rules).
ISOLATED_CLAUDE_ARGS = frozenset({"--verbose", "--debug"})
# Subdirectories of the run's own $CSESSION_DIR exempt from the unsafe-write-target rules.
OWN_WRITABLE = (sessiondir.WORK, sessiondir.OUTBOX, sessiondir.ARTIFACTS)
# Claude Code tools whose allow rules make a path writable, and every file tool whose allow
# or ask rules grant access to a path.
WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
FILE_TOOLS = WRITE_TOOLS | {"Read", "Glob", "Grep", "NotebookRead"}
GRANT_LISTS = ("allow", "ask")
# The parent of every session's private temporary directory: /tmp/csession-<uid>.
TMP_ROOT = "/tmp"
CASE_INSENSITIVE = sys.platform == "darwin"

# A permission rule: ``Tool`` or ``Tool(content)``.
_RULE = re.compile(r"([A-Za-z][A-Za-z0-9_]*)(?:\((.*)\))?", re.DOTALL)
# Glob and escape characters in a rule's path pattern: the literal part ends before them.
_GLOB = re.compile(r"[*?\[\]{}\\]")

_Identity = tuple[int, int]


def tmp_parent(uid: int | None = None) -> str:
    """``/tmp/csession-<uid>`` (default: this process's uid), as given (not resolved)."""
    return os.path.join(TMP_ROOT, f"csession-{os.getuid() if uid is None else uid}")


# Resolved paths


@dataclass(frozen=True)
class _Ref:
    """A resolved path: its realpath, the comparison key (case-folded on case-insensitive
    platforms), its own identity (None when it does not exist), the identities of it and
    every existing ancestor, and for each existing ancestor its identity paired with the
    name of the next component of the path (``children``: so a second name of ``$HOME``
    still shows that the path continues with a ``.<name>`` entry, existing or not)."""

    real: str
    key: str
    ident: _Identity | None
    chain: frozenset[_Identity]
    children: frozenset[tuple[_Identity, str]] = frozenset()


def _ref(path: str) -> _Ref:
    """Resolve ``path`` (absolute; a relative one resolves against the process cwd). Raises
    ``ValueError`` for a path ``realpath`` cannot take (a NUL)."""
    real = os.path.realpath(path)
    chain: set[_Identity] = set()
    children: set[tuple[_Identity, str]] = set()
    ident: _Identity | None = None
    current = real
    child: str | None = None
    while True:
        try:
            info = os.stat(current)
        except OSError:
            info = None
        if info is not None:
            identity = (info.st_dev, info.st_ino)
            chain.add(identity)
            if current == real:
                ident = identity
            if child is not None:
                children.add((identity, child))
        parent = os.path.dirname(current)
        if parent == current:
            break
        child = os.path.basename(current)
        current = parent
    return _Ref(
        real=real,
        key=_key(real),
        ident=ident,
        chain=frozenset(chain),
        children=frozenset(children),
    )


def _key(path: str) -> str:
    """The comparison key: on case-insensitive platforms (macOS) Unicode-decomposed (NFD,
    as the file system stores names) and case-folded, so ``~/.Café`` in either normal form
    or case is one name."""
    if not CASE_INSENSITIVE:
        return path
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", path).casefold())


def home_join(home: str, rest: str) -> str:
    """``$HOME`` joined with the rest of a ``~/rest`` path, normalized. Leading slashes of
    ``rest`` are dropped, so ``~//x`` stays under ``$HOME`` (``os.path.join`` would make it
    ``/x``)."""
    return os.path.normpath(home.rstrip("/") + "/" + rest.lstrip("/"))


def _under(path: str, root: str) -> bool:
    """Whether normalized absolute ``path`` is ``root`` or below it (lexically)."""
    return path == root or path.startswith(root.rstrip("/") + "/")


def _contains(outer: _Ref, inner: _Ref) -> bool:
    """Whether ``inner`` is ``outer`` or inside it: lexically (realpaths, case-folded where
    the platform is case-insensitive) or by identity (``outer`` is ``inner`` or one of its
    existing ancestors, also through a bind mount)."""
    if _under(inner.key, outer.key):
        return True
    return outer.ident is not None and outer.ident in inner.chain


def _exactly_under(inner: _Ref, outer: _Ref) -> bool:
    """Lexical, case-sensitive containment: for the exceptions, which must never widen."""
    return _under(inner.real, outer.real)


def _overlap(a: _Ref, b: _Ref) -> bool:
    return _contains(a, b) or _contains(b, a)


def _same(a: _Ref, b: _Ref) -> bool:
    return a.key == b.key or (a.ident is not None and a.ident == b.ident)


def _shown(path: str) -> str:
    return path.encode("utf-8", "backslashreplace").decode("utf-8")


def contains_any(candidate: str, paths: Iterable[str]) -> bool:
    """Whether one of ``paths`` is ``candidate`` or inside it (resolved; the other direction
    of a session-writable check). ``candidate`` is compared both as given (normalized) and
    resolved, so a symlinked candidate cannot hide what lies below its literal path."""
    try:
        resolved = _ref(candidate)
        refs = [_ref(path) for path in paths]
    except ValueError:
        return True  # fail closed: an unresolvable path is treated as overlapping
    literal = os.path.normpath(candidate)
    # The literal form only as the outer side: its key, and the identity it resolves to.
    unresolved = _Ref(real=literal, key=_key(literal), ident=resolved.ident, chain=frozenset())
    return any(_contains(form, ref) for ref in refs for form in (unresolved, resolved))


# Lexical backstop (the settings builder: pure, no file system access)


def lexical_root_problem(path: str, *, home: str) -> str | None:
    """Why normalized, resolved ``path`` may not be in a sandbox ``allowRead`` or
    ``allowWrite`` list, compared lexically only: ``/``, ``$HOME`` or an ancestor of it."""
    if _under(_key(home), _key(path)):
        return "is /, $HOME or an ancestor of $HOME"
    return None


def lexical_read_problem(path: str, *, home: str, session_dir: str) -> str | None:
    """``lexical_root_problem`` plus, outside ``session_dir``: containing or being inside a
    credential path (``CREDENTIAL_PATHS``), compared lexically only."""
    problem = lexical_root_problem(path, home=home)
    if problem is not None or _under(path, session_dir):
        return problem
    key = _key(path)
    for name in CREDENTIAL_PATHS:
        credential = _key(os.path.join(home, name))
        if _under(key, credential) or _under(credential, key):
            return f"contains or is inside the credential path ~/{name}"
    return None


# Session-writable paths


class SessionWritable:
    """The session-writable paths of a run (interface.md, Session-writable paths), resolved.

    Calling it answers whether a path (resolved: its realpath, or that of its nearest
    existing parent) is one of them or inside one; ``overlaps`` answers the other direction
    (one of them is the path or inside it). A path that cannot be resolved counts as
    session-writable (fail closed)."""

    def __init__(self, paths: Iterable[str]) -> None:
        self._refs = tuple(_ref(path) for path in dict.fromkeys(paths))

    def encloses(self, path: str) -> bool:
        """Whether a session-writable path is a proper ancestor of ``path`` (resolved): the
        agent could rename the ancestor's contents and swap in a symlink. A path equal to a
        session-writable path is not enclosed by it. Unresolvable: True (fail closed)."""
        try:
            ref = _ref(path)
        except ValueError:
            return True
        return any(_contains(w, ref) and not _same(w, ref) for w in self._refs)

    @property
    def paths(self) -> tuple[str, ...]:
        """The resolved session-writable paths (for ``check_interpreter``'s
        ``writable_paths``)."""
        return tuple(ref.real for ref in self._refs)

    def __call__(self, path: str) -> bool:
        try:
            ref = _ref(path)
        except ValueError:
            return True
        return any(_contains(writable, ref) for writable in self._refs)

    def overlaps(self, path: str) -> bool:
        """Whether a session-writable path is ``path`` or inside it."""
        return contains_any(path, self.paths)


def session_writable(
    *,
    session_dir: str,
    cwd: str,
    home: str,
    tmpdir: str | None = None,
    write_dirs: Sequence[str] = (),
    profile: Mapping[str, Any] | None = None,
    writable_cwd: bool = True,
) -> SessionWritable:
    """The session-writable paths of an isolated run: its cwd when the run may write it
    (``writable_cwd``; every isolated kind's cwd is: ``work/`` or the caller's cwd of a kind
    such as ``respond``), ``$CSESSION_DIR`` itself (an ``--add-dir`` that is not
    write-denied, so sandboxed Bash may write in it) with ``outbox/``, ``artifacts/`` and
    ``work/``, the ``write_dirs``, the expanded ``profile``'s sandbox ``allowWrite`` entries
    and ``Edit``/``Write`` (and ``MultiEdit``/``NotebookEdit``) allow-rule targets, and the
    private temporary directory. Plugin directories are ``--add-dir`` directories too, but
    write-denied. Raises ``InvalidPermissionProfile`` for an allow-rule target it cannot
    place (``rule_target``)."""
    paths = [
        *([cwd] if writable_cwd else []),
        session_dir,
        *(os.path.join(session_dir, name) for name in OWN_WRITABLE),
        *write_dirs,
        *([tmpdir] if tmpdir is not None else []),
    ]
    if profile is not None:
        paths += _profile_writes(profile, cwd=cwd, home=home)
    return SessionWritable(paths)


def _profile_writes(profile: Mapping[str, Any], *, cwd: str, home: str) -> list[str]:
    targets = [
        sandbox_path(entry, home=home)
        for entry in _strings(profile, "sandbox", "filesystem", "allowWrite")
    ]
    for rule in _strings(profile, "permissions", "allow"):
        parsed = rule_target(rule, cwd=cwd, home=home)
        if parsed is not None and parsed[0] in WRITE_TOOLS:
            targets.append(parsed[1])
    if _SETTINGS_RELATIVE in targets:
        raise InvalidPermissionProfile(
            "invalid permission profile",
            detail="a profile write path is relative to the generated settings file, inside"
            " the state root",
        )
    return targets


def sandbox_path(entry: str, *, home: str) -> str:
    """The path a profile's sandbox ``filesystem`` entry names: ``~`` and ``~/x`` are under
    ``$HOME`` (``home_join``), ``//x`` is ``/x``, an absolute path stays as it is, and
    anything else (relative, ``~user``) is relative to the settings file, reported as the
    settings-relative marker (inside the state root, so it is refused)."""
    if entry == "~" or entry.startswith("~/"):
        return home_join(home, entry[2:])
    if entry.startswith("//"):
        return "/" + entry.lstrip("/")
    if os.path.isabs(entry):
        return entry
    return _SETTINGS_RELATIVE


def rule_target(rule: str, *, cwd: str, home: str) -> tuple[str, str] | None:
    """The tool and the directory a file-tool permission rule reaches, or None for a rule
    of another tool (``Bash(...)``, ``WebFetch``, ...).

    The path pattern follows Claude Code's forms: ``//abs`` is absolute, ``~/x`` is under
    ``$HOME`` (``~//x`` too), ``x`` and ``./x`` are relative to the cwd; a bare tool name, an
    empty pattern or ``*`` (Claude Code treats ``Tool()`` and ``Tool(*)`` as the bare tool
    name) reaches everything (``/``). The reached directory is the literal part before
    the first glob or escape character, cut back to its last whole path segment, then
    normalized (``..`` applied). ``/x`` is relative to the settings file, which lives in the
    private directory under the state root: it is reported as that, so the refused-path
    rules reject it. Raises ``InvalidPermissionProfile`` for a malformed rule."""
    match = _RULE.fullmatch(rule)
    if match is None:
        return None
    tool, pattern = match.group(1), match.group(2)
    if tool not in FILE_TOOLS:
        return None
    if pattern is None or not pattern.strip() or pattern.strip() == "*":
        return tool, "/"
    if "\x00" in pattern:
        raise InvalidPermissionProfile(
            "invalid permission profile", detail=f"{tool}(...) rule contains a NUL"
        )
    glob = _GLOB.search(pattern)
    literal = pattern if glob is None else pattern[: glob.start()]
    if glob is not None and not literal.endswith("/"):
        literal = literal.rsplit("/", 1)[0] + "/" if "/" in literal else ""
    if pattern.startswith("//"):
        base = "/" + literal[2:]
    elif pattern == "~" or pattern.startswith("~/"):
        base = home_join(home, literal[2:]) if len(literal) >= 2 else home
    elif pattern.startswith("/"):
        return tool, _SETTINGS_RELATIVE
    else:
        base = os.path.join(cwd, literal)
    return tool, os.path.normpath(base) if base else "/"


# A marker target for ``/x`` rule patterns (relative to the settings file in the private
# directory); PathRules treats it as inside the state root.
_SETTINGS_RELATIVE = "\x00settings-relative"


def _strings(content: Mapping[str, Any], *loc: str) -> list[str]:
    node: Any = content
    for part in loc:
        if not isinstance(node, Mapping) or part not in node:
            return []
        node = node[part]
    if not isinstance(node, list):
        return []
    return [item for item in node if isinstance(item, str)]


# Host facts and the rules


@dataclass(frozen=True)
class Host:
    """The host facts the rules compare with (absolute paths; resolved by ``PathRules``).

    - ``home``: ``$HOME``.
    - ``state_root``: the state directory (``private/`` is under it).
    - ``tmp_parent``: ``/tmp/csession-<uid>``, the parent of every private tmpdir.
    - ``path_dirs``: the absolute entries of the caller's ``PATH`` plus
      ``env.path_prepend`` (unsafe write targets: a directory on ``PATH``).
    - ``xdg_dirs``: ``$XDG_CONFIG_HOME``, ``$XDG_DATA_HOME``, ``$XDG_STATE_HOME`` when set
      to absolute paths (their defaults are under ``$HOME/.<name>``, refused anyway).
    - ``xdg_credentials``: credential paths named by XDG variables set to absolute paths:
      ``$XDG_CONFIG_HOME``, ``$XDG_STATE_HOME`` and ``$XDG_DATA_HOME/keyrings`` (as
      ``(label, path)``; the defaults are ``CREDENTIAL_PATHS`` entries).
    - ``account_home``: the home directory of the user's account (``pw_dir``) when it is
      not ``$HOME``: its credential paths and ``.<name>`` entries are protected too."""

    home: str
    state_root: str
    tmp_parent: str
    path_dirs: tuple[str, ...] = ()
    xdg_dirs: tuple[str, ...] = ()
    xdg_credentials: tuple[tuple[str, str], ...] = ()
    account_home: str | None = None

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str],
        *,
        state_root: str,
        uid: int | None = None,
        path_prepend: Sequence[str] = (),
    ) -> Host:
        """The facts from an environment mapping (the caller's) and the user's account entry
        (``pwd``; a failed lookup is ignored). Raises ``LaunchValidationError``
        (``unsafe_path``) for a missing or relative ``$HOME``, or one that is ``/``."""
        home = environ.get("HOME", "")
        if not home or not os.path.isabs(home) or "\x00" in home:
            raise LaunchValidationError(
                "HOME must be set to an absolute path", end_reason="unsafe_path"
            )
        if os.path.realpath(home) == "/":
            raise LaunchValidationError("HOME must not be /", end_reason="unsafe_path")
        try:
            account: str | None = pwd.getpwuid(os.getuid() if uid is None else uid).pw_dir
        except (KeyError, OSError):
            account = None
        if account is not None and (
            not os.path.isabs(account) or os.path.realpath(account) in ("/", os.path.realpath(home))
        ):
            account = None
        path_dirs = [entry for entry in environ.get("PATH", "").split(":") if os.path.isabs(entry)]
        xdg = [
            value
            for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")
            if os.path.isabs(value := environ.get(name, ""))
        ]
        xdg_credentials = [
            (label, path)
            for name, label, sub in (
                ("XDG_CONFIG_HOME", "$XDG_CONFIG_HOME", ""),
                ("XDG_STATE_HOME", "$XDG_STATE_HOME", ""),
                ("XDG_DATA_HOME", "$XDG_DATA_HOME/keyrings", "keyrings"),
            )
            if os.path.isabs(value := environ.get(name, ""))
            for path in [os.path.join(value, sub) if sub else value]
        ]
        return cls(
            home=home,
            state_root=state_root,
            tmp_parent=tmp_parent(uid),
            path_dirs=tuple(dict.fromkeys([*path_prepend, *path_dirs])),
            xdg_dirs=tuple(xdg),
            xdg_credentials=tuple(xdg_credentials),
            account_home=account,
        )


class PathRules:
    """The path rules of one run: ``Host`` facts plus the run's ``session_dir``
    (``$CSESSION_DIR``), resolved ``cwd``, the ``caller_cwd`` (the cwd of the invoking
    process, resolved) and the private ``tmpdir`` (None: not sandboxed)."""

    def __init__(
        self,
        host: Host,
        *,
        session_dir: str,
        cwd: str,
        caller_cwd: str,
        tmpdir: str | None = None,
    ) -> None:
        self.host = host
        self.cwd = cwd
        self._home = _ref(host.home)
        self._homes = [(self._home, "~")]
        if host.account_home is not None:
            account = _ref(host.account_home)
            self._homes.append((account, _shown(account.real)))
        self._state = _ref(host.state_root)
        self._private = _ref(os.path.join(host.state_root, sessiondir.PRIVATE))
        self._tmp_parent = _ref(host.tmp_parent)
        self._session = _ref(session_dir)
        self._own = tuple(
            ref
            for name in OWN_WRITABLE
            if (ref := _exception_dir(os.path.join(session_dir, name))) is not None
        )
        self._tmpdir = None if tmpdir is None else _exception_dir(tmpdir)
        self._cwd = _ref(cwd)
        self._caller = _ref(caller_cwd)
        self._path_dirs = tuple(_ref(path) for path in host.path_dirs)
        self._xdg = tuple(_ref(path) for path in host.xdg_dirs)
        self._claude = tuple(_ref(os.path.join(home.real, ".claude")) for home, _ in self._homes)
        self._credentials = tuple(
            (f"{label}/{name}", _ref(os.path.join(home.real, name)))
            for home, label in self._homes
            for name in CREDENTIAL_PATHS
        ) + tuple((label, _ref(path)) for label, path in host.xdg_credentials)
        self._system = tuple(
            dict.fromkeys(form for d in SYSTEM_READ_DIRS for form in (d, os.path.realpath(d)))
        )
        self._hidden = tuple(
            hidden for home, label in self._homes for hidden in _hidden_dirs(home, label)
        )

    # Host

    def check_host(self) -> None:
        """``$HOME`` must not be the state root, inside it or inside ``/tmp/csession-<uid>``:
        the session's own directories would then be under ``$HOME``'s credential rules and
        the home rules would cover the state. Raises ``LaunchValidationError``
        (``unsafe_path``). The launch path must call it before the write checks
        (``check_cwd``, ``check_write_dirs``, ``check_profile``): they assume a valid
        ``$HOME``."""
        if _contains(self._state, self._home) or _contains(self._tmp_parent, self._home):
            raise LaunchValidationError(
                f"HOME is inside the state root or {self.host.tmp_parent}:"
                f" {_shown(self.host.home)!r}",
                end_reason="unsafe_path",
            )

    # Unsafe write targets

    def write_target_problem(self, path: str) -> str | None:
        """Why ``path`` may not be made writable by an isolated run, or None
        (interface.md, Path rules: unsafe write targets, state root containment and the
        refused paths). The run's own ``work/``, ``outbox/`` and ``artifacts/`` (and their
        subpaths) are always allowed while each is a real directory (not a symlink) at its
        own path; a path in the private tmpdir (same condition) passes the tmp rules."""
        if path == _SETTINGS_RELATIVE:
            return "is relative to the generated settings file, inside the state root"
        try:
            ref = _ref(path)
        except ValueError:
            return "cannot be resolved"
        if any(_exactly_under(ref, own) for own in self._own):
            return None
        if _contains(ref, self._home):
            return "is /, $HOME or an ancestor of $HOME"
        for directory in self._path_dirs:
            if _contains(directory, ref):
                return f"is or is inside a directory on PATH ({_shown(directory.real)})"
        for directory in self._xdg:
            if _overlap(directory, ref):
                return f"is, contains or is inside an XDG directory ({_shown(directory.real)})"
        problem = self._state_problem(ref, own_session=False)
        if problem is not None:
            return problem
        if any(_contains(claude, ref) for claude in self._claude):
            return "is or is inside ~/.claude"
        return self._hidden_home_problem(ref)

    def _state_problem(self, ref: _Ref, *, own_session: bool) -> str | None:
        """The state root and ``/tmp/csession-<uid>``: an entry may not be or contain either
        (or the private directory); one inside them is refused unless it is inside the
        run's own ``$CSESSION_DIR`` (state root; only with ``own_session``: write targets
        get their narrower exception first) or private tmpdir (tmp parent)."""
        for root, label in (
            (self._state, "the state root"),
            (self._private, "the private directory"),
            (self._tmp_parent, self.host.tmp_parent),
        ):
            if _contains(ref, root):
                return f"is or contains {label}"
        if _contains(self._private, ref):
            return "is inside the private directory"
        own = own_session and _exactly_under(ref, self._session)
        if _contains(self._state, ref) and not own:
            return "is inside the state root (outside the session's own directory)"
        if _contains(self._tmp_parent, ref) and not self._in_tmpdir(ref):
            return f"is inside {self.host.tmp_parent} (outside the run's private tmpdir)"
        return None

    def _in_tmpdir(self, ref: _Ref) -> bool:
        return self._tmpdir is not None and _exactly_under(ref, self._tmpdir)

    def _hidden_home_problem(self, ref: _Ref) -> str | None:
        """Inside a ``$HOME/.<name>`` entry (of ``$HOME`` or the account's home):
        lexically, by identity (a bind mount or another name of the home directory, also
        for a ``.<name>`` entry that does not exist yet), or overlapping one's realpath
        (``~/.config`` as a symlink to ``/data/config`` makes ``/data/config`` and ``/data``
        refused: writing there changes the configuration)."""
        message = "is or is inside a $HOME/.<name> directory"
        for home, _label in self._homes:
            key = home.key
            if ref.key != key and _under(ref.key, key):
                first = ref.key[len(key.rstrip("/")) + 1 :].split("/", 1)[0]
                if first.startswith("."):
                    return message
            if home.ident is not None and any(
                identity == home.ident and name.startswith(".") for identity, name in ref.children
            ):
                return message
        for hidden in self._hidden:
            if _overlap(hidden.ref, ref):
                if hidden.link:
                    return (
                        f"is, contains or is inside {_shown(hidden.ref.real)}, the target of"
                        f" the symlink {hidden.name}"
                    )
                return message
        return None

    def check_cwd(self) -> None:
        """Isolated runs: the cwd is writable (``work/`` or the caller's cwd), so it must
        pass the unsafe-write-target rules. Raises ``LaunchValidationError``
        (``unsafe_cwd``)."""
        problem = self.write_target_problem(self.cwd)
        if problem is not None:
            raise LaunchValidationError(
                f"cwd {problem}: {_shown(self.cwd)!r}", end_reason="unsafe_cwd"
            )

    def check_write_dirs(self, dirs: Sequence[str], writable: SessionWritable) -> None:
        """Isolated runs: every ``write_dirs`` entry (resolved) passes the
        unsafe-write-target rules and the user-entry rule (``user_entry_problem``). Raises
        ``InvalidPermissionProfile``."""
        for entry in dirs:
            problem = self.write_target_problem(entry) or self.user_entry_problem(entry, writable)
            if problem is not None:
                raise InvalidPermissionProfile(
                    "unsafe write_dirs entry", detail=f"{_shown(entry)}: {problem}"
                )

    def user_entry_problem(self, path: str, writable: SessionWritable) -> str | None:
        """Isolated runs: a user-supplied sandbox entry (``read_dirs``, ``write_dirs``, a
        profile's ``allowRead``, ``allowWrite`` or ``additionalDirectories``) reaches the
        settings as written, so no step of resolving it may be changeable by the agent:
        walked as ``writable_hop`` walks (``_resolution_steps``), no literal prefix, path
        visited or realpath may lie strictly inside a session-writable path (the agent
        could rename a parent and swap in a symlink), and no symlink followed may sit in a
        session-writable directory (the agent could re-point it). An entry equal to the
        run's own ``$CSESSION_DIR``, ``outbox/``, ``artifacts/``, ``work/`` or private
        tmpdir (by realpath, each a real directory) is exempt from the first part; an entry
        strictly below one of them is refused (list the directory itself)."""
        if path == _SETTINGS_RELATIVE:
            return "is relative to the generated settings file, inside the state root"
        if not os.path.isabs(path):
            return "is not an absolute path"
        steps = _resolution_steps(path)
        if steps is None:
            return "cannot be resolved"
        for step, holds_link in steps:
            if holds_link and writable(step):
                return (
                    "is reached through a symlink in a session-writable directory"
                    f" ({_shown(step)}; the session could re-point it)"
                )
        real = os.path.realpath(path)  # _resolution_steps succeeded, so this does too
        own = [*self._own, *([self._tmpdir] if self._tmpdir else []), self._session]
        if any(real == ref.real for ref in own):
            return None
        for ref in own:
            if _under(real, ref.real):
                return (
                    f"is below the run's own directory {_shown(ref.real)} (list that"
                    " directory itself: the session could replace a subdirectory with a"
                    " symlink)"
                )
        for step, _holds_link in steps:
            if writable.encloses(step):
                return (
                    "is inside a session-writable path (the session could replace a parent"
                    " directory with a symlink)"
                )
        return None

    # Read entries

    def read_entry_problem(self, path: str, *, generated: bool = False) -> str | None:
        """Why ``path`` may not be in an isolated run's sandbox ``allowRead`` list, or None:
        ``/``, ``$HOME`` or an ancestor of it; containing or inside a credential path
        (``CREDENTIAL_PATHS`` under ``$HOME`` and the account's home, and the XDG
        credential paths; paths inside the run's own ``$CSESSION_DIR``, readable anyway,
        are exempt); the state-root and tmp rules (inside the private tmpdir is refused
        too: listed in allowRead it would be mounted read-only, live E1). With
        ``generated`` (entries ``exec`` adds on its own): also the caller's cwd, or a
        directory containing it, unless the caller's cwd is the run's cwd or the entry is
        a system read directory or inside one (``SYSTEM_READ_DIRS``, readable anyway)."""
        if path == _SETTINGS_RELATIVE:
            return "is relative to the generated settings file, inside the state root"
        try:
            ref = _ref(path)
        except ValueError:
            return "cannot be resolved"
        if _contains(ref, self._home):
            return "is /, $HOME or an ancestor of $HOME"
        if not _exactly_under(ref, self._session):
            for label, credential in self._credentials:
                if _contains(credential, ref):
                    return f"contains or is inside the credential path {label}"
                if _contains(ref, credential):
                    return (
                        f"contains or is inside the credential path {label} (list narrower"
                        " directories that leave it out, for example ~/.cargo/bin and"
                        " ~/.cargo/registry instead of ~/.cargo)"
                    )
        problem = self._state_problem(ref, own_session=True)
        if problem is not None:
            return problem
        if self._tmpdir is not None and _contains(self._tmpdir, ref):
            return "is inside the private tmpdir, which must stay out of allowRead"
        if (
            generated
            and self._caller.real != self._cwd.real
            and not any(_under(ref.real, system) for system in self._system)
            and _contains(ref, self._caller)
        ):
            return (
                "is or contains the caller's cwd, which is not the run's cwd (a sys.path"
                " entry: running `python -m claude_session` from inside a directory puts"
                " it on sys.path; run the installed claude-session executable instead)"
            )
        return None

    def check_read_dirs(self, dirs: Sequence[str], writable: SessionWritable) -> None:
        """Isolated runs: ``read_dirs`` entries (resolved): the read-entry rules and the
        user-entry rule (``user_entry_problem``). Raises ``InvalidPermissionProfile``."""
        for entry in dirs:
            problem = self.read_entry_problem(entry) or self.user_entry_problem(entry, writable)
            if problem is not None:
                raise InvalidPermissionProfile(
                    "unsafe read_dirs entry", detail=f"{_shown(entry)}: {problem}"
                )

    def check_generated(self, entries: Mapping[str, Sequence[str]]) -> None:
        """Isolated runs: the generated read entries, by label (claude-session's install
        and Python directories, the user site, ``env.path_prepend`` directories, the
        resolved tools' directories, the git directory). Raises ``LaunchValidationError``
        (``unsafe_read_dir``)."""
        for label, values in entries.items():
            for value in values:
                problem = self.read_entry_problem(value, generated=True)
                if problem is not None:
                    raise LaunchValidationError(
                        f"generated read entry ({label}) {problem}: {_shown(value)!r}",
                        end_reason="unsafe_read_dir",
                    )

    def check_plugin_dirs(self, dirs: Sequence[str], writable: SessionWritable) -> None:
        """Isolated runs: each plugin directory must exist (``plugin_dir_missing``), pass the
        read-entry rules and neither be inside a session-writable path nor contain one
        (``unsafe_plugin_dir``): it is made read-only, which a writable directory on either
        side would undo or be undone by."""
        for entry in dirs:
            if not os.path.isdir(entry):
                raise LaunchValidationError(
                    f"plugin directory does not exist: {_shown(entry)!r}",
                    end_reason="plugin_dir_missing",
                )
            problem = self.read_entry_problem(entry)
            if problem is None and writable(entry):
                problem = "is inside a session-writable path"
            if problem is None and writable.overlaps(entry):
                problem = "contains a session-writable path"
            if problem is not None:
                raise LaunchValidationError(
                    f"plugin directory {problem}: {_shown(entry)!r}",
                    end_reason="unsafe_plugin_dir",
                )

    # Profiles

    def check_profile(
        self,
        content: Mapping[str, Any],
        *,
        isolated: bool,
        writable: SessionWritable | None = None,
    ) -> None:
        """The expanded profile's paths. Sandbox ``filesystem`` entries are mapped first
        (``sandbox_path``: ``~/x`` under ``$HOME``, ``//x`` absolute, a relative entry
        relative to the settings file, so refused). Every run: a path a rule or list grants
        (file-tool ``allow``/``ask`` rule targets, ``additionalDirectories``, sandbox
        ``allowRead`` and ``allowWrite``) may not be under the state root or
        ``/tmp/csession-<uid>``, except inside the run's own ``$CSESSION_DIR`` and private
        tmpdir. Isolated runs (``writable`` required): sandbox ``allowRead`` entries pass
        the read-entry rules (credential paths included), sandbox ``allowWrite`` entries
        and ``Edit``/``Write`` allow targets the unsafe-write-target rules, and the sandbox
        entries and ``additionalDirectories`` the user-entry rule. Raises
        ``InvalidPermissionProfile``."""
        if isolated and writable is None:
            raise ValueError("isolated runs need the session-writable paths (writable)")
        home = self.host.home
        granted: list[tuple[str, str]] = []
        writes: list[tuple[str, str]] = []
        for key in GRANT_LISTS:
            for index, rule in enumerate(_strings(content, "permissions", key)):
                parsed = rule_target(rule, cwd=self.cwd, home=home)
                if parsed is None:
                    continue
                where = f"permissions.{key}.{index}"
                granted.append((where, parsed[1]))
                if key == "allow" and parsed[0] in WRITE_TOOLS:
                    writes.append((where, parsed[1]))
        directories: list[tuple[str, str]] = []
        for index, entry in enumerate(_strings(content, "permissions", "additionalDirectories")):
            if entry == "~" or entry.startswith("~/"):
                target = home_join(home, entry[2:])
            else:
                target = os.path.join(self.cwd, entry)  # an absolute entry stays as it is
            directories.append((f"permissions.additionalDirectories.{index}", target))
        reads = [
            (f"sandbox.filesystem.allowRead.{i}", sandbox_path(v, home=home))
            for i, v in enumerate(_strings(content, "sandbox", "filesystem", "allowRead"))
        ]
        allow_writes = [
            (f"sandbox.filesystem.allowWrite.{i}", sandbox_path(v, home=home))
            for i, v in enumerate(_strings(content, "sandbox", "filesystem", "allowWrite"))
        ]
        for where, path in [*granted, *directories, *reads, *allow_writes]:
            problem = self._refused_problem(path)
            if problem is not None:
                raise InvalidPermissionProfile(
                    "permission profile names a refused path", detail=f"{where}: {problem}"
                )
        if writable is None:
            return
        for where, path in reads:
            problem = self.read_entry_problem(path)
            if problem is not None:
                raise InvalidPermissionProfile(
                    "permission profile names an unsafe read path", detail=f"{where}: {problem}"
                )
        for where, path in [*allow_writes, *writes]:
            problem = self.write_target_problem(path)
            if problem is not None:
                raise InvalidPermissionProfile(
                    "permission profile makes an unsafe path writable",
                    detail=f"{where}: {problem}",
                )
        for where, path in [*reads, *allow_writes, *directories]:
            problem = self.user_entry_problem(path, writable)
            if problem is not None:
                raise InvalidPermissionProfile(
                    "permission profile names a path inside a session-writable path",
                    detail=f"{where}: {problem}",
                )

    def _refused_problem(self, path: str) -> str | None:
        """The refused paths of every run: under the state root or the tmp parent, except
        inside the run's own ``$CSESSION_DIR`` or private tmpdir."""
        if path == _SETTINGS_RELATIVE:
            return "is relative to the generated settings file, inside the state root"
        if not os.path.isabs(path):
            return "is not an absolute path"
        try:
            ref = _ref(path)
        except ValueError:
            return "cannot be resolved"
        if _contains(self._state, ref) and not _exactly_under(ref, self._session):
            return "is under the state root (outside the session's own directory)"
        if _contains(self._tmp_parent, ref) and not self._in_tmpdir(ref):
            return f"is under {self.host.tmp_parent} (outside the run's private tmpdir)"
        return None


@dataclass(frozen=True)
class _Hidden:
    """An existing ``.<name>`` entry of a home directory: its label (``~/.name``), its
    resolved reference and whether it is a symlink."""

    name: str
    ref: _Ref
    link: bool


def _hidden_dirs(home: _Ref, label: str) -> tuple[_Hidden, ...]:
    """The existing ``.<name>`` entries of ``home``, resolved, for the identity and
    realpath checks (a bind mount, or a symlinked dot entry whose target holds the
    configuration). An entry that resolves to the home directory or an ancestor of it (an
    alias of the home directory, not a configuration directory) is left out: the home
    directory has its own rule."""
    try:
        names = os.listdir(home.real)
    except OSError:
        return ()
    found = []
    for name in names:
        if not name.startswith("."):
            continue
        path = os.path.join(home.real, name)
        ref = _ref(path)
        if _contains(ref, home):
            continue
        found.append(_Hidden(name=f"{label}/{name}", ref=ref, link=os.path.islink(path)))
    return tuple(found)


def _exception_dir(path: str) -> _Ref | None:
    """The run's own directory ``path`` as an exception, or None: granted only while its
    final component is a real directory (``lstat``: not a symlink) and its realpath is the
    realpath of its parent joined with its name, so a ``work`` swapped for a symlink widens
    nothing, while a symlinked ancestor (macOS ``/tmp -> /private/tmp``) keeps it. The
    exception is compared by its realpath."""
    literal = os.path.normpath(path)
    try:
        info = os.lstat(literal)
        if not stat.S_ISDIR(info.st_mode):
            return None
        real = os.path.realpath(literal)
        parent = os.path.realpath(os.path.dirname(literal))
    except (OSError, ValueError):
        return None
    if real != os.path.join(parent, os.path.basename(literal)):
        return None
    return _ref(real)


# read_dirs and write_dirs


def resolve_dirs(
    entries: Sequence[str], variables: Mapping[str, str | None], *, home: str, key: str
) -> list[str]:
    """``read_dirs`` or ``write_dirs`` entries (``key``) expanded and resolved: the profile
    variables (``${CSESSION_DIR}``, ``${CWD}``, ``${HOME}``, ``${GIT_DIR}``; same rules as in
    profiles), then a leading ``~`` or ``~/`` (``~user`` is refused), then the realpath. A
    relative or empty result is refused. Raises ``InvalidPermissionProfile``."""
    from . import permission_profiles  # not at import time: it needs config

    expanded = permission_profiles.expand({key: list(entries)}, variables)[key]
    resolved = []
    for index, entry in enumerate(expanded):
        where = f"{key}.{index}"
        if entry == "~" or entry.startswith("~/"):
            entry = home_join(home, entry[2:])
        elif entry.startswith("~"):
            raise InvalidPermissionProfile(
                f"invalid {key} entry", detail=f"{where}: ~user is not supported"
            )
        if not os.path.isabs(entry) or "\x00" in entry:
            raise InvalidPermissionProfile(
                f"invalid {key} entry", detail=f"{where}: must be an absolute path"
            )
        resolved.append(os.path.realpath(entry))
    return list(dict.fromkeys(resolved))


@dataclass(frozen=True)
class WriteDirs:
    """Resolved ``write_dirs``: ``dirs`` go to the sandbox ``allowWrite`` list, ``create``
    (inside ``$CSESSION_DIR``) are created before launch, ``warnings`` name the missing
    directories outside it that were skipped."""

    dirs: tuple[str, ...]
    create: tuple[str, ...]
    warnings: tuple[str, ...]


def resolve_write_dirs(resolved: Sequence[str], *, session_dir: str) -> WriteDirs:
    """Sort resolved ``write_dirs``: one under ``$CSESSION_DIR`` must be inside ``outbox/``,
    ``artifacts/`` or ``work/`` (else ``InvalidPermissionProfile``) and is created when
    missing; any other must exist, else it is skipped with a warning. For isolated runs
    ``check_write_dirs`` then refuses an entry strictly below ``outbox/``, ``artifacts/`` or
    ``work/`` (``user_entry_problem``).

    TODO(#15): exec creates ``create`` (private-directory helpers) before launch."""
    session = os.path.realpath(session_dir)
    own = [os.path.join(session, name) for name in OWN_WRITABLE]
    dirs: list[str] = []
    create: list[str] = []
    warnings: list[str] = []
    for entry in resolved:
        if _under(entry, session):
            if not any(_under(entry, root) for root in own):
                raise InvalidPermissionProfile(
                    "unsafe write_dirs entry",
                    detail=f"{_shown(entry)}: inside $CSESSION_DIR but not inside outbox/,"
                    " artifacts/ or work/",
                )
            dirs.append(entry)
            if not os.path.isdir(entry):
                create.append(entry)
        elif os.path.isdir(entry):
            dirs.append(entry)
        else:
            warnings.append(f"write_dirs entry does not exist, skipped: {_shown(entry)}")
    return WriteDirs(dirs=tuple(dirs), create=tuple(create), warnings=tuple(warnings))


# PATH and tools


def sanitize_path(value: str) -> list[str]:
    """The ``PATH`` entries an isolated run keeps: absolute ones, in order (empty and
    relative entries are dropped; they would resolve against whatever cwd a process has)."""
    return [entry for entry in value.split(":") if entry and os.path.isabs(entry)]


def _resolution_steps(path: str) -> list[tuple[str, bool]] | None:
    """The steps of resolving ``path``, in order, each paired with whether it is the
    directory holding a symlink followed: every literal normalized prefix of ``path``, every
    path visited while following its symlinks one component at a time (``lstat``), the
    directory holding each symlink followed, and the final realpath. None for more than
    ``MAX_SYMLINKS`` symlinks or a path that cannot be resolved (callers fail closed)."""
    try:
        literal = os.path.normpath(path)
        steps = [(literal, False)]
        parts = [part for part in literal.split("/") if part]
        steps += [("/" + "/".join(parts[:end]), False) for end in range(1, len(parts))]
        current = "/"
        links = 0
        while parts:
            name = parts.pop(0)
            if name in ("", "."):
                continue
            if name == "..":
                current = os.path.dirname(current)
                continue
            candidate = os.path.join(current, name)
            steps.append((candidate, False))
            try:
                info = os.lstat(candidate)
            except OSError:
                current = candidate
                continue
            if not stat.S_ISLNK(info.st_mode):
                current = candidate
                continue
            links += 1
            if links > MAX_SYMLINKS:
                return None
            steps.append((current, True))  # the directory holding the link
            target = os.readlink(candidate)
            if target.startswith("/"):
                current = "/"
            parts = [part for part in target.split("/") if part] + parts
        steps.append((os.path.realpath(literal), False))
    except (OSError, ValueError):
        return None
    return list(dict.fromkeys(steps))


def writable_hop(path: str, writable: Callable[[str], bool]) -> str | None:
    """The first session-writable step of resolving ``path``, or None: every literal
    normalized prefix of ``path``, every path visited while following its symlinks one
    component at a time (``lstat``), the directory holding each symlink followed, and the
    final realpath are checked with ``writable`` (``_resolution_steps``). A symlink whose
    directory is session-writable can be re-pointed by the agent, even when the final target
    is safe. More than ``MAX_SYMLINKS`` symlinks, or a path that cannot be resolved, is
    returned as it is (fail closed)."""
    steps = _resolution_steps(path)
    if steps is None:
        return path
    for step, _holds_link in steps:
        if writable(step):
            return step
    return None


def check_path_entries(entries: Sequence[str], writable: Callable[[str], bool]) -> None:
    """No ``PATH`` entry may be inside a session-writable path, resolved hop by hop
    (``writable_hop``): an isolated run never runs a binary from one. Raises
    ``LaunchValidationError`` (``unsafe_path``)."""
    for entry in entries:
        hop = writable_hop(entry, writable)
        if hop is not None:
            raise LaunchValidationError(
                f"PATH entry is inside a session-writable path: {_shown(entry)!r}"
                + ("" if hop == entry else f" (through {_shown(hop)!r})"),
                end_reason="unsafe_path",
            )


class ResolvedTool(NamedTuple):
    """A resolved executable: ``path`` is its realpath, the file to execute, and ``name``
    the bare tool name (the basename of the name given) to pass as ``argv[0]``, so a launcher
    that dispatches on ``argv[0]`` (volta, mise or snap symlink shims) still sees the tool it
    was invoked as."""

    path: str
    name: str


def resolve_tool(
    name: str, path_entries: Sequence[str], writable: Callable[[str], bool]
) -> ResolvedTool | None:
    """Executable ``name`` on the (sanitized) ``path_entries``, or None when it is not
    found. An absolute ``name`` is used as it is; a relative name with a ``/`` is refused.
    No step of resolving the found file (``writable_hop``: its literal prefixes, every
    symlink hop and its realpath) may be session-writable. The tool is executed from its
    realpath (``ResolvedTool.path``: no symlink the agent could re-point is followed at
    exec time) with ``argv[0]`` set to the bare tool name (``ResolvedTool.name``). Raises
    ``LaunchValidationError`` (``unsafe_path``).

    TODO(#15): the launch path execs ``path`` (through ``gateway``) with ``argv[0]`` =
    ``name``."""
    if not name or "\x00" in name:
        raise LaunchValidationError("empty or invalid executable name", end_reason="unsafe_path")
    if "/" in name:
        if not os.path.isabs(name):
            raise LaunchValidationError(
                f"executable must be a bare name or an absolute path: {_shown(name)!r}",
                end_reason="unsafe_path",
            )
        candidates = [os.path.normpath(name)]
    else:
        candidates = [os.path.join(entry, name) for entry in path_entries]
    for candidate in candidates:
        if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            continue
        if writable_hop(candidate, writable) is not None:
            raise LaunchValidationError(
                f"executable is inside a session-writable path: {_shown(candidate)!r}",
                end_reason="unsafe_path",
            )
        return ResolvedTool(path=os.path.realpath(candidate), name=os.path.basename(candidate))
    return None


# Other launch checks


def check_claude_args(args: Sequence[str], *, isolated: bool) -> None:
    """Isolated runs accept only ``--verbose`` and ``--debug`` (exact tokens; no values) in
    the kind's ``claude_args``. Raises ``LaunchValidationError`` (``unsafe_claude_args``)."""
    if not isolated:
        return
    refused = [arg for arg in args if arg not in ISOLATED_CLAUDE_ARGS]
    if refused:
        raise LaunchValidationError(
            "isolated runs accept only --verbose and --debug in claude_args"
            f" ({len(refused)} other argument(s) given)",
            end_reason="unsafe_claude_args",
        )


def check_outcome_file(path: str, writable: Callable[[str], bool]) -> None:
    """Isolated runs: an ``--outcome-file`` (resolved: the path or its nearest existing
    parent) under a session-writable path is a usage error (exit 2); it is never written.

    TODO(#21): the outcome-file writers skip a path this rejects; before a run is allocated
    (launch step 2) the caller passes a predicate whose session directory is the whole
    ``<state>/sessions`` directory."""
    if writable(path):
        raise UsageError(
            f"--outcome-file is under a session-writable path of an isolated run: {_shown(path)!r}"
        )
