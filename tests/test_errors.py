from __future__ import annotations

import pytest

from claude_session.errors import (
    AmbiguousError,
    CliError,
    FailedError,
    InternalError,
    LaunchFailedError,
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
