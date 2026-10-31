from __future__ import annotations

import pytest

from claude_session.errors import (
    AmbiguousError,
    CliError,
    FailedError,
    InternalError,
    InvalidPermissionProfile,
    LaunchFailedError,
    LaunchValidationError,
    NotFoundError,
    RefusedError,
    SchemaNewerError,
    UsageError,
)

from .schemas import validate

CASES: list[tuple[CliError, str, int]] = [
    (InternalError("boom"), "internal", 1),
    (UsageError("bad flag"), "usage", 2),
    (UsageError("invalid config", detail="default_kind: not a string"), "usage", 2),
    (NotFoundError("unknown session"), "not_found", 10),
    (FailedError("no crontab command", detail="crontab: not found"), "failed", 12),
    (FailedError("transcript read failed"), "failed", 12),
    (SchemaNewerError("database schema 9 is newer than supported 1"), "schema_newer", 12),
    (
        LaunchFailedError(
            "bubblewrap is not installed",
            csession_id="cs-abcd2345",
            run=1,
            end_reason="sandbox_unavailable",
            hint="install bubblewrap and socat",
        ),
        "launch_failed",
        12,
    ),
    (RefusedError("session is live", reason="live"), "refused", 20),
    (AmbiguousError("ambiguous", matches=["cs-abcd2345", "cs-abcd7777"]), "ambiguous", 21),
]


@pytest.mark.parametrize(("error", "code", "exit_code"), CASES)
def test_envelope_matches_schema_and_exit_code(error: CliError, code: str, exit_code: int) -> None:
    envelope = error.envelope()
    validate(envelope, "claude-session/cli/error.json")
    assert envelope["error"]["code"] == code
    assert error.exit_code == exit_code


def test_launch_validation_errors_carry_their_end_reason() -> None:
    error = InvalidPermissionProfile("bad profile", detail="sandbox.enabled: refused")
    assert isinstance(error, LaunchValidationError)
    assert isinstance(error, FailedError)  # uncaught, it still exits 12
    assert error.exit_code == 12
    assert error.end_reason == "invalid_permission_profile"
    assert (error.message, error.detail) == ("bad profile", "sandbox.enabled: refused")
    assert str(error) == "bad profile"
    validate(error.envelope(), "claude-session/cli/error.json")


def test_launch_validation_error_end_reason_keyword() -> None:
    error = LaunchValidationError("cwd contains *", end_reason="unsafe_cwd")
    assert (error.end_reason, error.exit_code, error.detail) == ("unsafe_cwd", 12, None)


def test_launch_validation_error_keyword_overrides_the_class_default() -> None:
    error = InvalidPermissionProfile("bad", end_reason="unsafe_read_dir")
    assert error.end_reason == "unsafe_read_dir"
    assert InvalidPermissionProfile("bad").end_reason == "invalid_permission_profile"


def test_launch_validation_error_needs_an_end_reason() -> None:
    with pytest.raises(TypeError, match="end_reason"):
        LaunchValidationError("no reason")
