"""Errors that end a command, with their exit codes and ``--json`` envelope.

Each error class maps to one ``code`` of the error envelope (schemas/cli/error.json) and to the
exit code interface.md assigns it (Error codes). Commands raise these; ``cli`` prints them.
Standard library only: the hook path may import this module.
"""

from __future__ import annotations

from typing import Any, ClassVar


class CliError(Exception):
    """An error that ends a command with a documented exit code and envelope."""

    code: ClassVar[str] = "internal"
    exit_code: ClassVar[int] = 1

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def fields(self) -> dict[str, Any]:
        """Code-specific envelope fields besides ``code``, ``message`` and ``detail``."""
        return {}

    def envelope(self) -> dict[str, Any]:
        """The ``--json`` error envelope for this error."""
        error: dict[str, Any] = {"code": self.code, "message": self.message, **self.fields()}
        if self.detail is not None:
            error["detail"] = self.detail
        return {"error": error}


class InternalError(CliError):
    """Exit 1: an unexpected internal error."""

    code = "internal"
    exit_code = 1


class UsageError(CliError):
    """Exit 2: bad flags or values, a flag rule, or an invalid config (with ``detail``)."""

    code = "usage"
    exit_code = 2


class NotFoundError(CliError):
    """Exit 10: unknown or forgotten session, or ``--run`` out of range."""

    code = "not_found"
    exit_code = 10


class RefusedError(CliError):
    """Exit 20: refused, with a machine-readable ``reason``."""

    code = "refused"
    exit_code = 20

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason

    def fields(self) -> dict[str, Any]:
        return {"reason": self.reason}


class AmbiguousError(CliError):
    """Exit 21: a session id prefix matching several sessions."""

    code = "ambiguous"
    exit_code = 21

    def __init__(self, message: str, *, matches: list[str]) -> None:
        super().__init__(message)
        self.matches = matches

    def fields(self) -> dict[str, Any]:
        return {"matches": list(self.matches)}


class FailedError(CliError):
    """Exit 12: an operational failure outside a launch (optional ``detail``)."""

    code = "failed"
    exit_code = 12


class SchemaNewerError(CliError):
    """Exit 12: the database schema is newer than this claude-session."""

    code = "schema_newer"
    exit_code = 12


class LaunchFailedError(CliError):
    """Exit 12, ``start`` only: a launch-validation failure of an allocated run."""

    code = "launch_failed"
    exit_code = 12

    def __init__(
        self,
        message: str,
        *,
        csession_id: str,
        run: int,
        end_reason: str,
        hint: str | None,
    ) -> None:
        super().__init__(message)
        self.csession_id = csession_id
        self.run = run
        self.end_reason = end_reason
        self.hint = hint

    def fields(self) -> dict[str, Any]:
        return {
            "csession_id": self.csession_id,
            "run": self.run,
            "end_reason": self.end_reason,
            "hint": self.hint,
        }


class LaunchValidationError(FailedError):
    """A launch validation failed (interface.md, Launch order, step 5): the run ends
    ``not-started`` with ``end_reason`` and the command exits 12.

    A ``FailedError`` subclass (code ``failed``, exit 12), so an error that reaches the CLI
    error mapping uncaught still exits 12 like a not-started run; the launch path maps it to
    the run's outcome first, since only it knows the session id and run (``start``: as
    ``LaunchFailedError``). ``end_reason`` is a class-level default that subclasses set
    (``InvalidPermissionProfile``) and the ``end_reason=`` keyword overrides; the base class
    has none, so raising it needs the keyword. ``message`` and ``detail`` never hold values
    from profile content; they may name file paths, profile names and key paths. ``hint`` is
    the launch failure's hint (interface.md, Launch order: for example the install command
    for bubblewrap and socat); the launch path reports it with the run, never in the
    ``failed`` envelope."""

    # exec maps it to a not-started run (launch.fail_launch: end_reason, its message, detail
    # and hint as the outcome message, exit 12). TODO(#28): start maps it to LaunchFailedError.

    end_reason: str

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        end_reason: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.hint = hint
        if end_reason is not None:
            self.end_reason = end_reason
        elif not isinstance(getattr(type(self), "end_reason", None), str):
            raise TypeError(f"{type(self).__name__} needs an end_reason")


class InvalidPermissionProfile(LaunchValidationError):
    """A permission profile that cannot be used for the run (interface.md, Permission
    profiles)."""

    end_reason = "invalid_permission_profile"
