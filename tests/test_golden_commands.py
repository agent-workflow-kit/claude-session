"""Golden ``--json`` outputs of the commands that run against a state directory alone
(interface.md, Commands): ``reconcile``, ``cron``, ``forget`` and ``stop``. Each output is
validated against its schema first (``assert_golden``), then compared with
``tests/golden/<schema>-<case>.json`` after the volatile values (ids, times, paths) are
replaced."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from claude_session import store
from claude_session.cli import main
from claude_session.store import Final, Store

from .golden import assert_golden
from .test_cronblock import Env
from .test_forget import make
from .test_liveness import Procs, session

TS = "2026-01-01T00:00:00.000000Z"  # valid placeholders: goldens are schema-validated too
CLAUDE_ID = "c0ffee00-0000-4000-8000-000000000001"
SID = re.compile(r"cs-[a-z2-7]{8}")
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z")


@pytest.fixture
def st() -> Iterator[Store]:
    with store.open_state() as s:
        yield s


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    """A fake ``crontab`` first on PATH (tests/test_cronblock.py)."""
    return Env(tmp_path, monkeypatch)


@pytest.fixture
def root(st: Store) -> str:
    return os.path.dirname(st.path)


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text("kill_grace: 0.5s\npoll_interval: 0.05s\n", encoding="utf-8")
    return path


@pytest.fixture
def procs() -> Iterator[Procs]:
    p = Procs()
    try:
        yield p
    finally:
        p.close()


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any]:
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def scrub(value: Any, extra: tuple[tuple[str, str], ...] = ()) -> Any:
    """``value`` with session ids, timestamps and the given ``(text, replacement)`` pairs
    replaced in every string."""

    def text(s: str) -> str:
        for old, new in extra:
            s = s.replace(old, new)
        return TIMESTAMP.sub(TS, SID.sub("cs-aaaaaaaa", s))

    if isinstance(value, str):
        return text(value)
    if isinstance(value, list):
        return [scrub(item, extra) for item in value]
    if isinstance(value, dict):
        return {key: scrub(item, extra) for key, item in value.items()}
    return value


def test_reconcile_of_an_empty_state(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(capsys, "reconcile", "--dry-run")
    assert code == 0
    assert_golden(out, "reconcile-empty")


def test_cron_install_dry_run(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(capsys, "cron", "install", "--dry-run", "--interval", "5")
    assert code == 0
    assert_golden(out, "cron-install-dry-run", normalize=_cron_block)


def _cron_block(out: dict[str, Any]) -> dict[str, Any]:
    """The block holds the interpreter and PATH of this machine: only its shape is kept."""
    out = json.loads(json.dumps(out))
    block = out["block"]
    match = re.fullmatch(
        r"(# claude-session managed\n\*/5 \* \* \* \* /bin/sh -c ').*(reconcile >/dev/null'\n"
        r"# claude-session managed end)",
        block,
    )
    assert match, block
    out["block"] = f"{match.group(1)}<command> {match.group(2)}"
    return out


def test_cron_uninstall_dry_run(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(capsys, "cron", "uninstall", "--dry-run")
    assert code == 0
    assert_golden(out, "cron-uninstall-dry-run")


def test_forget_older_than_dry_run_lists_the_sessions_it_would_hide(
    st: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    root = Path(st.path).parent
    make(st, str(root), labels=("team=x",))
    code, out = run(capsys, "forget", "--older-than", "7d", "--dry-run", "--actor", "auto:golden")
    assert code == 0
    assert_golden(out, "forget-bulk-dry-run", normalize=scrub)


def test_stop_dry_run_of_a_finished_session(
    capsys: pytest.CaptureFixture[str], config: Path, st: Store, root: str, procs: Procs
) -> None:
    sid = session(st, root, wrapper=procs.live())
    st.finalize_run(sid, 1, Final("session", "lost", "lost", 17, "lost", "exit"))
    code, out = run(capsys, "stop", sid, "--dry-run", "--config", str(config))
    assert code == 0, out
    assert_golden(out, "stop-already-final-dry-run", normalize=scrub)
