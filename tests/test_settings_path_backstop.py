"""The settings builder's lexical backstop of the path rules (pathrules applies the full rules
before build(); interface.md, Path rules)."""

from __future__ import annotations

from typing import Any

import pytest

from claude_session import config
from claude_session.errors import LaunchValidationError
from tests.test_isolated_settings import CSDIR, HOME, cfg, sandbox_fs, settings

__all__ = ["cfg"]  # the module-scoped config fixture


@pytest.mark.parametrize(
    ("iso", "reason"),
    [
        ({"read_dirs": ("/",)}, "invalid_permission_profile"),
        ({"read_dirs": ("/home",)}, "invalid_permission_profile"),
        ({"read_dirs": (f"{HOME}/.ssh",)}, "invalid_permission_profile"),
        ({"read_dirs": (f"{HOME}/.config/gh",)}, "invalid_permission_profile"),
        ({"write_dirs": (HOME,)}, "invalid_permission_profile"),
        ({"write_dirs": ("/",)}, "invalid_permission_profile"),
        ({"python_dirs": ("/home",)}, "unsafe_read_dir"),
        ({"tool_dirs": (f"{HOME}/.claude/local",)}, "unsafe_read_dir"),
        ({"path_prepend": (f"{HOME}/.aws",)}, "unsafe_read_dir"),
        ({"git_dir": f"{HOME}/.ssh"}, "unsafe_read_dir"),
    ],
)
def test_lexical_backstop_refusals(cfg: config.Config, iso: dict[str, Any], reason: str) -> None:
    with pytest.raises(LaunchValidationError) as info:
        settings(cfg, **iso)
    assert info.value.end_reason == reason


def test_profile_allow_read_of_a_credential_path(cfg: config.Config) -> None:
    perm = {"sandbox": {"filesystem": {"allowRead": [f"{HOME}/.ssh"]}}}
    with pytest.raises(LaunchValidationError) as info:
        settings(cfg, perm=perm)
    assert info.value.end_reason == "invalid_permission_profile"


def test_plugin_dir_inside_a_credential_path(cfg: config.Config) -> None:
    with pytest.raises(LaunchValidationError) as info:
        settings(cfg, plugin_dirs=(f"{HOME}/.claude/plugins/x",))
    assert info.value.end_reason == "unsafe_plugin_dir"


@pytest.mark.parametrize("cwd", ["/", HOME, "/home"])
def test_cwd_home_or_ancestor(cfg: config.Config, cwd: str) -> None:
    with pytest.raises(LaunchValidationError) as info:
        settings(cfg, "respond", cwd=cwd)
    assert info.value.end_reason == "unsafe_cwd"


def test_toolchains_and_own_session_paths_stay_allowed(cfg: config.Config) -> None:
    data = settings(
        cfg,
        read_dirs=(f"{HOME}/.nvm", f"{HOME}/.local/share/uv", f"{CSDIR}/inputs"),
        write_dirs=(f"{CSDIR}/artifacts/x",),
    )
    assert f"{HOME}/.nvm" in sandbox_fs(data)["allowRead"]
