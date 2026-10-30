"""Shared fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _private_state_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Every test (and every subprocess it starts) gets its own ``XDG_STATE_HOME``, so no
    test ever touches the real state directory. Kept outside ``tmp_path``, which tests may
    expect to stay empty; not created up front."""
    state_home = tmp_path_factory.mktemp("xdg") / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    return state_home
