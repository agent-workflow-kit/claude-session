"""``label add`` / ``label remove`` (interface.md, label add / label remove and Labels): pairs
of a multiset, no-op adds and removes, ``--dry-run``, id resolution, the ``kind=`` label set at
creation, and JSON per schema (golden files)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from claude_session import store
from claude_session.cli import main
from claude_session.store import NewRun, Store

from .golden import assert_golden
from .schemas import validate

SID = "cs-abcd2345"
OTHER = "cs-abcd7777"
T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
ADD_SCHEMA = "claude-session/cli/label-add.json"
REMOVE_SCHEMA = "claude-session/cli/label-remove.json"
ERROR_SCHEMA = "claude-session/cli/error.json"


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """The clock of every store the commands open (``now[0]``)."""
    moment = [T1]
    monkeypatch.setattr(store, "utc_now", lambda: moment[0])
    return moment


@pytest.fixture
def st(now: list[datetime]) -> Iterator[Store]:
    with store.open_state(clock=lambda: T0) as s:
        yield s


def new_run(**fields: Any) -> NewRun:
    base: dict[str, Any] = {
        "state_root": "/state",
        "kind": "implement",
        "reads_third_party": False,
        "mode": "headless",
        "repo": None,
        "model": None,
        "skill": None,
        "has_prompt": True,
        "spec": {},
        "resolved": {},
        "config_hash": None,
        "invocation": {},
        "hook_interpreter": "/usr/bin/env",
        "config_path": None,
        "wrapper_pid": 4242,
        "wrapper_start": "boot:1",
        "labels": ("team=core",),
    }
    return NewRun(**{**base, **fields})


def allocate(s: Store, sid: str = SID, **fields: Any) -> str:
    return s.allocate(new_run(**fields), new_id=lambda used: sid)


def labels_in_db(s: Store, sid: str = SID) -> list[str]:
    return [
        f"{key}={value}"
        for key, value in s.conn.execute(
            "SELECT key, value FROM labels WHERE csession_id = ? ORDER BY key, value", (sid,)
        )
    ]


def updated_at(s: Store, sid: str = SID) -> str:
    (value,) = s.conn.execute(
        "SELECT updated_at FROM sessions WHERE csession_id = ?", (sid,)
    ).fetchone()
    return str(value)


def run_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, Any, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


# kind= at creation


def test_kind_label_is_set_at_creation(st: Store) -> None:
    allocate(st, kind="review", labels=("team=core", "kind=other"))
    assert labels_in_db(st) == ["kind=other", "kind=review", "team=core"]


# label add


def test_add_new_pairs(st: Store, capsys: pytest.CaptureFixture[str]) -> None:
    allocate(st)
    code, obj, _ = run_json(
        ["label", "add", SID, "prio=high", "a.b_c-d=x:y/z#1@2+3,4", "--json"], capsys
    )
    assert code == 0
    assert_golden(obj, "label-add-new")
    assert labels_in_db(st) == ["a.b_c-d=x:y/z#1@2+3,4", "kind=implement", "prio=high", "team=core"]


def test_adding_an_existing_pair_is_a_no_op(st: Store, capsys: pytest.CaptureFixture[str]) -> None:
    allocate(st)
    before = updated_at(st)
    code, obj, _ = run_json(["label", "add", SID, "team=core", "--json"], capsys)
    assert code == 0
    validate(obj, ADD_SCHEMA)
    assert obj["added"] == []
    assert obj["labels"] == ["kind=implement", "team=core"]
    assert updated_at(st) == before


def test_a_pair_given_twice_is_added_once(st: Store, capsys: pytest.CaptureFixture[str]) -> None:
    allocate(st)
    _, obj, _ = run_json(["label", "add", SID, "x=1", "x=1", "--json"], capsys)
    assert obj["added"] == ["x=1"]
    assert labels_in_db(st).count("x=1") == 1


def test_one_key_may_hold_several_values(st: Store, capsys: pytest.CaptureFixture[str]) -> None:
    allocate(st)
    _, obj, _ = run_json(["label", "add", SID, "team=prod", "team=", "--json"], capsys)
    assert obj["added"] == ["team=prod", "team="]
    assert obj["labels"] == ["kind=implement", "team=", "team=core", "team=prod"]


def test_a_value_may_contain_equals(st: Store, capsys: pytest.CaptureFixture[str]) -> None:
    allocate(st)
    _, obj, _ = run_json(["label", "add", SID, "q=a=b", "--json"], capsys)
    assert obj["added"] == ["q=a=b"]
    row = st.conn.execute("SELECT key, value FROM labels WHERE key = 'q'").fetchone()
    assert row == ("q", "a=b")


def test_a_change_updates_updated_at(st: Store, capsys: pytest.CaptureFixture[str]) -> None:
    allocate(st)
    assert updated_at(st) == store.format_ts(T0)
    run_json(["label", "add", SID, "x=1", "--json"], capsys)
    assert updated_at(st) == store.format_ts(T1)


def test_labels_change_on_another_session_only(
    st: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    allocate(st)
    allocate(st, OTHER)
    run_json(["label", "add", SID, "x=1", "--json"], capsys)
    assert labels_in_db(st, OTHER) == ["kind=implement", "team=core"]


def test_labels_show_in_status(st: Store, capsys: pytest.CaptureFixture[str]) -> None:
    allocate(st)
    run_json(["label", "add", SID, "x=1", "--json"], capsys)
    run_json(["label", "remove", SID, "team=core", "--json"], capsys)
    _, obj, _ = run_json(["status", SID, "--json"], capsys)
    assert obj["labels"] == ["kind=implement", "x=1"]


def test_pairs_after_an_option_are_taken(st: Store, capsys: pytest.CaptureFixture[str]) -> None:
    allocate(st)
    _, obj, _ = run_json(["label", "add", SID, "a=1", "--json", "b=2"], capsys)
    assert obj["added"] == ["a=1", "b=2"]


@pytest.mark.parametrize(
    "argv",
    [
        ["label", "add", "--json", SID, "a=1", "--dry-run", "--", "b=2"],
        ["label", "add", SID, "a=1", "--json", "--dry-run", "--", "b=2"],
        ["label", "add", "--dry-run", "--json", SID, "--", "a=1", "b=2"],
    ],
)
def test_pairs_after_double_dash_are_taken(
    st: Store, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    allocate(st)
    code, obj, _ = run_json(argv, capsys)
    assert code == 0
    assert obj["added"] == ["a=1", "b=2"]
    assert obj["dry_run"] is True


@pytest.mark.parametrize(
    "argv",
    [
        ["label", "add", "--json", SID, "a=1", "--dry-run", "--", "-x=y"],
        ["label", "add", "--json", SID, "a=1", "--", "--dry-run"],
    ],
)
def test_option_like_pairs_after_double_dash_are_usage_errors(
    st: Store, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    allocate(st)
    code, obj, _ = run_json(argv, capsys)
    assert code == 2
    assert obj["error"]["code"] == "usage"
    assert labels_in_db(st) == ["kind=implement", "team=core"]


# label remove


def test_remove_present_and_absent_pairs(st: Store, capsys: pytest.CaptureFixture[str]) -> None:
    allocate(st)
    code, obj, _ = run_json(
        ["label", "remove", SID, "team=core", "team=other", "absent=1", "team=core", "--json"],
        capsys,
    )
    assert code == 0
    assert_golden(obj, "label-remove-present")
    assert labels_in_db(st) == ["kind=implement"]


def test_removing_an_absent_pair_is_a_no_op(st: Store, capsys: pytest.CaptureFixture[str]) -> None:
    allocate(st)
    before = updated_at(st)
    _, obj, _ = run_json(["label", "remove", SID, "team=Core", "--json"], capsys)
    validate(obj, REMOVE_SCHEMA)
    assert obj["removed"] == []
    assert labels_in_db(st) == ["kind=implement", "team=core"]
    assert updated_at(st) == before


def test_the_kind_label_has_no_built_in_meaning(
    st: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    allocate(st)
    _, obj, _ = run_json(["label", "remove", SID, "kind=implement", "--json"], capsys)
    assert obj["removed"] == ["kind=implement"]
    assert obj["labels"] == ["team=core"]
    (kind,) = st.conn.execute("SELECT launch_kind FROM sessions").fetchone()
    assert kind == "implement"


def test_all_labels_can_be_removed(st: Store, capsys: pytest.CaptureFixture[str]) -> None:
    allocate(st)
    _, obj, _ = run_json(["label", "remove", SID, "kind=implement", "team=core", "--json"], capsys)
    validate(obj, REMOVE_SCHEMA)
    assert obj["labels"] == []


# --dry-run


@pytest.mark.parametrize(
    ("action", "pairs", "labels", "added", "removed"),
    [
        ("add", ["x=1", "team=core"], ["kind=implement", "team=core", "x=1"], ["x=1"], []),
        ("remove", ["team=core", "y=2"], ["kind=implement"], [], ["team=core"]),
    ],
)
def test_dry_run_reports_and_changes_nothing(
    st: Store,
    capsys: pytest.CaptureFixture[str],
    action: str,
    pairs: list[str],
    labels: list[str],
    added: list[str],
    removed: list[str],
) -> None:
    allocate(st)
    before = (labels_in_db(st), updated_at(st))
    code, obj, _ = run_json(["label", action, SID, *pairs, "--dry-run", "--json"], capsys)
    assert code == 0
    assert_golden(obj, f"label-{action}-dry-run")
    assert obj == {
        "csession_id": SID,
        "labels": labels,
        "added": added,
        "removed": removed,
        "dry_run": True,
    }
    assert (labels_in_db(st), updated_at(st)) == before


# Ids


def test_a_unique_prefix_in_any_case_resolves(
    st: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    allocate(st)
    _, obj, _ = run_json(["label", "add", "ABCD2", "x=1", "--json"], capsys)
    assert obj["csession_id"] == SID


@pytest.mark.parametrize("text", ["cs-zzzz2345", "cs-abcd%", "cs-abcd23456"])
def test_an_unknown_session_exits_10(
    st: Store, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    allocate(st)
    code, obj, _ = run_json(["label", "add", text, "x=1", "--json"], capsys)
    assert code == 10
    validate(obj, ERROR_SCHEMA)
    assert labels_in_db(st) == ["kind=implement", "team=core"]


def test_a_forgotten_session_exits_10(st: Store, capsys: pytest.CaptureFixture[str]) -> None:
    allocate(st)
    with st.transaction() as conn:
        conn.execute("UPDATE sessions SET forgotten_at = updated_at")
    for action in ("add", "remove"):
        code, obj, _ = run_json(["label", action, SID, "x=1", "--json"], capsys)
        assert code == 10
        assert obj["error"]["code"] == "not_found"


def test_an_ambiguous_prefix_exits_21(st: Store, capsys: pytest.CaptureFixture[str]) -> None:
    allocate(st)
    allocate(st, OTHER)
    code, obj, _ = run_json(["label", "add", "abcd", "x=1", "--json"], capsys)
    assert code == 21
    assert obj["error"]["matches"] == [SID, OTHER]


@pytest.mark.parametrize(
    "pair", ["k", "=v", "K=v", "k=v w", "k=\x1b[2J", "k=é", "k=" + "v" * 257, "k" * 65 + "=v"]
)
def test_an_invalid_pair_exits_2_and_changes_nothing(
    st: Store, capsys: pytest.CaptureFixture[str], pair: str
) -> None:
    allocate(st)
    code, obj, _ = run_json(["label", "add", SID, "x=1", pair, "--json"], capsys)
    assert code == 2
    assert obj["error"]["code"] == "usage"
    assert labels_in_db(st) == ["kind=implement", "team=core"]


# Human output


def test_human_output(st: Store, capsys: pytest.CaptureFixture[str]) -> None:
    allocate(st)
    assert main(["label", "add", SID, "x=1", "team=core"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"{SID}: added x=1",
        "labels: kind=implement, team=core, x=1",
    ]
    assert main(["label", "add", SID, "x=1"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == f"{SID}: nothing to add"
    assert main(["label", "remove", SID, "nope=1", "--dry-run"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"{SID}: dry run: nothing to remove",
        "labels: kind=implement, team=core, x=1",
    ]
    assert main(["label", "remove", SID, "kind=implement", "team=core", "x=1"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"{SID}: removed kind=implement, team=core, x=1",
        "labels: (none)",
    ]


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_stored_label_that_is_not_utf8_does_not_break_a_change(
    st: Store, capsys: pytest.CaptureFixture[str], dry_run: bool
) -> None:
    """Stored text is decoded leniently (as by ``status`` and ``list``); the bad row is kept."""
    allocate(st)
    with st.transaction() as conn:
        conn.execute(
            "INSERT INTO labels (csession_id, key, value) VALUES (?, 'bad', CAST(x'fffe' AS TEXT))",
            (SID,),
        )
    flags = ["--dry-run"] if dry_run else []
    code, obj, _ = run_json(["label", "add", SID, "x=1", "--json", *flags], capsys)
    assert code == 0
    assert obj["added"] == ["x=1"]
    code, obj, _ = run_json(["label", "remove", SID, "team=core", "--json", *flags], capsys)
    assert code == 0
    assert obj["removed"] == ["team=core"]
    (count,) = st.conn.execute("SELECT count(*) FROM labels WHERE key = 'bad'").fetchone()
    assert count == 1


@pytest.mark.parametrize("dry_run", [False, True])
def test_change_labels_on_a_forgotten_or_unknown_session_is_none(st: Store, dry_run: bool) -> None:
    """A session forgotten between id resolution and the change is not changed."""
    allocate(st)
    with st.transaction() as conn:
        conn.execute("UPDATE sessions SET forgotten_at = updated_at")
    for sid in (SID, OTHER):
        assert st.change_labels(sid, [("x", "1")], add=True, dry_run=dry_run) is None
        assert st.change_labels(sid, [("team", "core")], add=False, dry_run=dry_run) is None
    assert labels_in_db(st) == ["kind=implement", "team=core"]
