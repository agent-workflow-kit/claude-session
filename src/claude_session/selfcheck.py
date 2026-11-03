"""The launch-time sandbox self-check (interface.md, Platform notes and Launch order step 5;
design.md, Generated settings: ``sandbox_active``).

``sandbox_active`` is true when the run's sandbox mode is not ``off``, the prerequisites are
present (Linux and WSL2: ``bwrap`` and ``socat`` resolved to absolute paths, ``socat -V``
runs and user namespaces are usable; macOS: ``sandbox-exec``) and the socket path fits.

Launch step 5 (interface.md, Launch order) checks, in this order: permission profile, cwd
and the other path rules, plugin directories, git directory, temporary directory,
``claude_bin``, ``claude --version``, ``claude_args`` (isolated runs), sandbox
prerequisites, socket path length. The caller runs ``tmpdir.check`` at the
temporary-directory slot; ``self_check`` runs after ``claude_args``: sandbox prerequisites,
socket path length, then (unless a dry run) ``tmpdir.create_reporting``, which validates the
base and per-uid root again, so nothing is created before every check has passed. A leftover
it renamed aside and could not remove becomes a warning naming it with the ``rm -rf``
command that removes it.

- ``required``: a missing prerequisite fails the launch (``sandbox_unavailable``).
- ``preferred``: it gives a warning and ``sandbox_active: false``; the run still gets its
  private tmpdir (the generated settings keep ``sandbox.enabled: true`` without
  ``failIfUnavailable``, and Claude Code then runs unsandboxed with a warning, spike
  "Respond isolation").
- A socket path that does not fit fails every sandboxed run (``socket_path_too_long``):
  with the sandbox enabled, its network bridge would break (spike: unix socket paths are
  limited to about 108 bytes).
- ``off``: nothing is checked or created.

Prerequisite probes run through the #51 ``Gateway`` (a fake one in tests), by absolute path
and with a fixed minimal environment.

``exec`` (``launch.check_tmpdir``) calls ``tmpdir.check`` at the temporary-directory slot of
launch step 5 and (``launch.check_sandbox``) ``self_check`` after the ``claude_args`` check,
as the last action of step 5, with ``bwrap`` and ``socat``
resolved by ``pathrules.resolve_tool`` (outside session-writable paths; this module only
checks that they are absolute, executable regular files, and probes them by their realpath),
records ``sandbox_active`` on the run, adds the warnings to the run's warnings, passes
``tmpdir`` to ``settings_builder.Isolation`` and ``envbuild``, and maps a
``LaunchValidationError`` (with its ``hint`` as the outcome message) to a not-started run.
TODO(#28): adoption calls both again.
``--dry-run`` (``dryrun``, through ``launch.checks``) makes the same two calls with
``create=False``, for ``would_fail``, ``sandbox_active`` and ``warnings``.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass

from . import tmpdir
from .errors import LaunchValidationError
from .gateway import Gateway, GatewayTimeout, SystemGateway

END_REASONS = ("tmpdir_unsafe", "sandbox_unavailable", "socket_path_too_long")
SANDBOX_MODES = ("required", "preferred", "off")
PLATFORMS = ("linux", "wsl2", "wsl1", "macos", "unsupported")

# sizeof(sockaddr_un.sun_path), including the terminating NUL (design.md, Platform notes).
SOCKET_PATH_MAX = {"linux": 108, "wsl2": 108, "wsl1": 108, "macos": 104}
# Bytes reserved for the name of the longest socket Claude Code creates in TMPDIR. Claude
# Code 2.1.314 names its proxy bridges claude-http-<16 hex>.sock and
# claude-socks-<16 hex>.sock (34 bytes, read from the binary, not verified live: #41); the
# rest is margin for a longer name in a later version.
SOCKET_NAME_RESERVE = 40

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
PROBE_TIMEOUT = 10.0
# Probes run with this environment only: nothing inherited reaches them.
PROBE_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
# Linux and WSL2: the namespaces Claude Code's sandbox relies on (user, PID, network) and the
# mounts it makes. A probe that fails means bubblewrap cannot sandbox anything here.
BWRAP_PROBE_ARGS = (
    "--unshare-user",
    "--unshare-pid",
    "--unshare-net",
    "--die-with-parent",
    "--ro-bind",
    "/",
    "/",
    "--proc",
    "/proc",
    "--dev",
    "/dev",
    "--",
    "/bin/sh",
    "-c",
    ":",
)
SANDBOX_EXEC_PROBE_ARGS = ("-p", "(version 1)(allow default)", "/usr/bin/true")

_INSTALL_HINT = "install bubblewrap and socat (for example: sudo apt install bubblewrap socat)"
_SOCAT_HINT = "reinstall socat (for example: sudo apt install --reinstall socat)"
SOCAT_PROBE_ARGS = ("-V",)
_USERNS_HINT = (
    "bubblewrap could not create its namespaces: enable unprivileged user namespaces"
    " (sysctl kernel.unprivileged_userns_clone=1 and user.max_user_namespaces > 0; on Ubuntu,"
    " an AppArmor profile for bwrap or kernel.apparmor_restrict_unprivileged_userns=0)"
)
_WSL1_HINT = "WSL1 has no sandbox: use WSL2, or set the kind's sandbox to preferred"
_MACOS_HINT = f"the macOS Seatbelt sandbox needs {SANDBOX_EXEC}"
_UNSUPPORTED_HINT = "Claude Code has no sandbox on this platform; set sandbox to preferred or off"
_SOCKET_HINT = "use a shorter temporary directory path (the private TMPDIR lives under /tmp)"

_SYSTEM_GATEWAY = SystemGateway()


@dataclass(frozen=True)
class Prerequisites:
    available: bool
    problem: str | None = None
    hint: str | None = None


@dataclass(frozen=True)
class SelfCheck:
    """``tmpdir``: the private temporary directory (created unless a dry run; None for
    ``off`` and for a dry run without a session id). ``problems``: why a ``preferred``
    sandbox is unavailable (``<problem>; <hint>``; a dry run reports it)."""

    sandbox_active: bool
    tmpdir: str | None
    warnings: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


def detect_platform(sys_platform: str | None = None, release: str | None = None) -> str:
    """``linux``, ``wsl2``, ``wsl1``, ``macos`` or ``unsupported``. WSL is told by the kernel
    release: WSL2 kernels are ``*-microsoft-standard-WSL2``, WSL1 reports ``*-Microsoft``."""
    if sys_platform is None:
        sys_platform = sys.platform
    if sys_platform == "darwin":
        return "macos"
    if not sys_platform.startswith("linux"):
        return "unsupported"
    if release is None:
        release = os.uname().release
    lowered = release.lower()
    if "microsoft" not in lowered:
        return "linux"
    if "wsl2" in lowered or "microsoft-standard" in lowered:
        return "wsl2"
    return "wsl1"


def check_socket_path(path: str, platform: str) -> None:
    """The longest socket Claude Code creates in ``path`` (the private TMPDIR) must fit
    ``sun_path``. Raises ``LaunchValidationError`` (``socket_path_too_long``)."""
    limit = SOCKET_PATH_MAX.get(platform, min(SOCKET_PATH_MAX.values()))
    length = len(os.fsencode(path)) + 1 + SOCKET_NAME_RESERVE  # "/" + name
    if length > limit - 1:  # the terminating NUL
        raise LaunchValidationError(
            f"socket paths in the private temporary directory {path} would need {length}"
            f" bytes; the limit is {limit - 1} bytes",
            end_reason="socket_path_too_long",
            hint=_SOCKET_HINT,
        )


def check_prerequisites(
    platform: str,
    *,
    bwrap: str | None,
    socat: str | None,
    gw: Gateway = _SYSTEM_GATEWAY,
    sandbox_exec: str = SANDBOX_EXEC,
) -> Prerequisites:
    """Whether the sandbox can run here: the tools present and a probe that succeeds."""
    if platform in ("linux", "wsl2"):
        return _linux(bwrap, socat, gw)
    if platform == "macos":
        return _macos(sandbox_exec, gw)
    if platform == "wsl1":
        return Prerequisites(False, "WSL1 has no sandbox", _WSL1_HINT)
    return Prerequisites(False, f"no sandbox on platform {platform!r}", _UNSUPPORTED_HINT)


def self_check(
    mode: str,
    csession_id: str | None,
    *,
    bwrap: str | None,
    socat: str | None,
    platform: str | None = None,
    gw: Gateway = _SYSTEM_GATEWAY,
    tmp_base: str | None = None,  # None: pathrules.TMP_ROOT
    create: bool = True,
    sandbox_exec: str = SANDBOX_EXEC,
) -> SelfCheck:
    """Run the sandbox self-check for a run with sandbox ``mode``: sandbox prerequisites,
    socket path length, then the private tmpdir is created (a leftover moved aside and left
    there is reported in ``warnings``). The caller has run
    ``tmpdir.check`` earlier in launch step 5. ``create=False`` (dry run) checks the same and
    does not touch the tmpdir at all; ``csession_id`` may then be None (a new session), and
    the socket path is checked for an id of the fixed length. Raises
    ``LaunchValidationError`` (``sandbox_unavailable``, ``socket_path_too_long``,
    ``tmpdir_unsafe``) in that order of precedence."""
    if mode not in SANDBOX_MODES:
        raise ValueError(f"unknown sandbox mode: {mode!r}")
    if mode == "off":
        return SelfCheck(sandbox_active=False, tmpdir=None)
    if create and csession_id is None:
        raise ValueError("creating the private tmpdir needs the session id")
    if platform is None:
        platform = detect_platform()
    prerequisites = check_prerequisites(
        platform, bwrap=bwrap, socat=socat, gw=gw, sandbox_exec=sandbox_exec
    )
    warnings: list[str] = []
    problems: tuple[str, ...] = ()
    if not prerequisites.available:
        problems = (f"{prerequisites.problem}; {prerequisites.hint}",)
        if mode == "required":
            raise LaunchValidationError(
                f"sandbox unavailable: {prerequisites.problem}",
                end_reason="sandbox_unavailable",
                hint=prerequisites.hint,
            )
        warnings.append(
            f"sandbox 'preferred' but unavailable ({prerequisites.problem}): the run is not"
            f" sandboxed; {prerequisites.hint}"
        )
    check_socket_path(tmpdir.run_path(csession_id or _PLACEHOLDER_ID, tmp_base), platform)
    path = None if csession_id is None else tmpdir.run_path(csession_id, tmp_base)
    if create:
        assert csession_id is not None
        path, stale = tmpdir.create_reporting(csession_id, base=tmp_base)
        if stale is not None:
            warnings.append(
                f"leftover private temporary directory moved aside: {stale};"
                f" remove it: rm -rf {stale}"
            )
    return SelfCheck(
        sandbox_active=prerequisites.available,
        tmpdir=path,
        warnings=tuple(warnings),
        problems=problems,
    )


# Session ids have a fixed length (ids.SESSION_ID); a dry run of a new session checks this one.
_PLACEHOLDER_ID = "cs-aaaaaaaa"


def _linux(bwrap: str | None, socat: str | None, gw: Gateway) -> Prerequisites:
    for name, path in (("bwrap", bwrap), ("socat", socat)):
        problem = _tool_problem(name, path)
        if problem is not None:
            return Prerequisites(False, problem, _INSTALL_HINT)
    assert bwrap is not None and socat is not None
    problem = _probe([socat, *SOCAT_PROBE_ARGS], gw)
    if problem is not None:
        return Prerequisites(False, f"socat does not run: {problem}", _SOCAT_HINT)
    problem = _probe([bwrap, *BWRAP_PROBE_ARGS], gw)
    if problem is not None:
        return Prerequisites(False, f"bubblewrap cannot create namespaces: {problem}", _USERNS_HINT)
    return Prerequisites(True)


def _macos(sandbox_exec: str, gw: Gateway) -> Prerequisites:
    problem = _tool_problem("sandbox-exec", sandbox_exec)
    if problem is None:
        problem = _probe([sandbox_exec, *SANDBOX_EXEC_PROBE_ARGS], gw)
    if problem is not None:
        return Prerequisites(False, problem, _MACOS_HINT)
    return Prerequisites(True)


def _tool_problem(name: str, path: str | None) -> str | None:
    if path is None:
        return f"{name} not found"
    if not os.path.isabs(path):
        return f"{name} is not an absolute path: {path!r}"
    try:
        info = os.stat(path)
    except OSError as exc:
        return f"{name} at {path}: {exc.strerror or exc}"
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        return f"{name} at {path} is not an executable file"
    return None


def _probe(argv: list[str], gw: Gateway) -> str | None:
    """Run a probe; None when it exits 0, else what went wrong (no output: it may hold
    paths of the host, and the exit status is enough)."""
    try:
        result = gw.run(argv, timeout=PROBE_TIMEOUT, env=dict(PROBE_ENV))
    except GatewayTimeout:
        return f"probe timed out after {PROBE_TIMEOUT:g} s"
    except OSError as exc:
        return f"probe could not run: {exc.strerror or exc}"
    except ValueError as exc:
        return f"probe could not run: {exc}"
    if result.returncode != 0:
        return f"probe exited with status {result.returncode}"
    return None
