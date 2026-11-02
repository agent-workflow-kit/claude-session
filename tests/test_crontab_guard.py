"""The test suite's own guard against the user's real crontab (``conftest.crontab_guard`` and
the ``crontab`` tripwire first on every test's PATH). No test here runs anything named
``crontab`` except fakes and the tripwire: a broken guard is caught by a fake outside the base
temporary directory that records whether it ran."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from claude_session.gateway import SystemGateway
from tests.conftest import crontab_guard


def recording_crontab(directory: Path) -> tuple[Path, Path]:
    """A ``crontab`` in ``directory`` that only records that it ran."""
    marker = directory / "ran"
    program = directory / "crontab"
    program.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 0\n", encoding="utf-8")
    program.chmod(0o755)
    return program, marker


def outside(private_tmp_root: Path) -> Path:
    """A directory outside the pytest base temporary directory (``private_tmp_root`` is
    under the system temporary directory, not under the base)."""
    directory = private_tmp_root / "elsewhere"
    directory.mkdir()
    return directory


def test_guard_allows_a_fake_under_the_base_temporary_directory(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    program, _ = recording_crontab(tmp_path)
    crontab_guard([str(program), "-l"], {}, str(tmp_path_factory.getbasetemp()))


def test_guard_ignores_other_programs(tmp_path_factory: pytest.TempPathFactory) -> None:
    crontab_guard(["/bin/true"], {}, str(tmp_path_factory.getbasetemp()))


def test_guard_refuses_a_crontab_elsewhere(
    private_tmp_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    program, _ = recording_crontab(outside(private_tmp_root))
    base = str(tmp_path_factory.getbasetemp())
    with pytest.raises(pytest.fail.Exception, match="outside the test temporary directory"):
        crontab_guard([str(program), "-l"], {}, base)
    with pytest.raises(pytest.fail.Exception):
        crontab_guard(["crontab", "-l"], {"executable": str(program)}, base)


def test_guard_resolves_a_bare_name_on_the_childs_path(
    private_tmp_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    directory = outside(private_tmp_root)
    recording_crontab(directory)
    with pytest.raises(pytest.fail.Exception):
        crontab_guard(
            ["crontab", "-l"],
            {"env": {"PATH": str(directory)}},
            str(tmp_path_factory.getbasetemp()),
        )


def test_guard_refuses_a_shell_command_that_mentions_crontab(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    with pytest.raises(pytest.fail.Exception, match="through a shell"):
        crontab_guard("echo crontab", {"shell": True}, str(tmp_path_factory.getbasetemp()))


def test_the_real_gateway_never_starts_a_crontab_elsewhere(private_tmp_root: Path) -> None:
    program, marker = recording_crontab(outside(private_tmp_root))
    # Failed is a BaseException: an ``except Exception`` in the code under test cannot hide it.
    with pytest.raises(pytest.fail.Exception):
        SystemGateway().run([str(program), "-l"], keep_raw_stdout=True)
    assert not marker.exists()


def test_popen_with_a_bare_name_never_starts_a_crontab_elsewhere(private_tmp_root: Path) -> None:
    directory = outside(private_tmp_root)
    _, marker = recording_crontab(directory)
    with pytest.raises(pytest.fail.Exception):
        subprocess.Popen(["crontab", "-l"], env={"PATH": str(directory)})
    assert not marker.exists()


def test_tripwire_crontab_comes_first_on_path(tmp_path_factory: pytest.TempPathFactory) -> None:
    found = shutil.which("crontab")
    assert found is not None
    base = os.path.realpath(tmp_path_factory.getbasetemp())
    assert os.path.commonpath([os.path.realpath(found), base]) == base  # never the real one
    done = subprocess.run([found, "-l"], capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 97 and "tripwire" in done.stderr
