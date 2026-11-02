"""Fixtures and gating for ``tests/live``.

Every test collected from this directory is marked ``live`` here (a test cannot forget the
marker and leak into CI), and is skipped unless ``CSESSION_LIVE=1`` (``make live`` sets it) and
a ``claude`` with usable credentials exists. Zero-cost tests (no API call) use the ``cheap``
fixture, which needs only the binary and never gets credentials (no variable, no login file).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from . import harness

LIVE_DIR = Path(__file__).resolve().parent
_ledger = harness.Ledger(
    harness.env_float(os.environ, harness.MAX_USD_ENV, harness.DEFAULT_MAX_USD)
    if harness.opted_in(os.environ)
    else harness.DEFAULT_MAX_USD
)
_observations: list[tuple[str, object]] = []


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if Path(str(item.path)).is_relative_to(LIVE_DIR):
            item.add_marker(pytest.mark.live)
            if not harness.opted_in(os.environ):
                item.add_marker(
                    pytest.mark.skip(reason=f"live tests need {harness.OPT_IN_ENV}=1 (make live)")
                )


# The user's real HOME, read at import (before the autouse fixtures replace it), only to find
# the login file CSESSION_LIVE_COPY_LOGIN asks for. Nothing is ever written there.
_REAL_HOME = Path(os.environ.get("HOME", "/nonexistent"))


def _real_home() -> Path:
    return _REAL_HOME


_login: harness.LoginCache | None = None


def _login_cache(auth: harness.Auth) -> harness.LoginCache | None:
    """The session's one copy of the login file, made the first time a test needs it."""
    global _login
    if auth.source is None:
        return None
    if _login is None:
        _login = harness.new_login_cache(auth.source)
    return _login


def _make(*, needs_auth: bool) -> Iterator[harness.LiveEnv]:
    claude = harness.claude_path(os.environ)
    if claude is None:
        pytest.skip("claude is not on PATH")
    auth = harness.resolve_auth(os.environ, _real_home()) if needs_auth else None
    if needs_auth and auth is None:
        pytest.skip(
            "no credentials: set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN, or "
            f"{harness.COPY_LOGIN_ENV}=1 to copy the login file into the private home"
        )
    root = harness.new_root()
    login = _login_cache(auth) if auth is not None else None
    if login is not None:
        login.install(root / "home")
    live = harness.LiveEnv(
        root=root,
        claude=claude,
        model=os.environ.get(harness.MODEL_ENV, harness.DEFAULT_MODEL),
        run_usd=harness.env_float(os.environ, harness.RUN_USD_ENV, harness.DEFAULT_RUN_USD),
        ledger=_ledger,
        base_environ=dict(os.environ),
        auth=auth,
    )
    try:
        yield live
    finally:
        _observations.extend(live.observations)
        harness.teardown(live, login)


@pytest.fixture
def live() -> Iterator[harness.LiveEnv]:
    """A private environment with credentials, for tests that call the API."""
    yield from _make(needs_auth=True)


@pytest.fixture
def cheap() -> Iterator[harness.LiveEnv]:
    """A private environment for tests that make no API call (``--help``, bad credentials)."""
    yield from _make(needs_auth=False)


def pytest_sessionfinish(session: pytest.Session) -> None:
    if _login is not None:
        _login.remove()
    path = os.environ.get(harness.OBSERVATIONS_ENV)
    if path and _observations:
        import json

        with open(path, "a", encoding="utf-8") as fh:
            for key, value in _observations:
                fh.write(json.dumps({"key": key, "value": value}, default=str) + "\n")


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if _ledger.runs:
        terminalreporter.write_line(
            f"live: {_ledger.runs} runs, {_ledger.spent_usd:.2f} USD spent "
            f"(cap {_ledger.cap_usd:.2f})"
        )
    for key, value in _observations:
        terminalreporter.write_line(f"live observation: {key} = {value!r}")
