"""The carry-over table as pure functions (interface.md, Resume and fork carry-over):
``resume.carry`` over a previous run's stored explicit flags, and ``resume.conversation``."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

import pytest

from claude_session import kinds, launch, resume
from claude_session.errors import UsageError

SNAPSHOT = "/state/private/cs-abcd2345/runs/1/permission-profile.json"
STORED: dict[str, Any] = {
    "timeout": "1h",
    "max_turns": 5,
    "model": "m1",
    "permission_profile": "readonly",
    "sandbox": "preferred",
    "setting_sources": "user",
    "reads_third_party": True,
    "auto_exit": False,
    "env_allow": ["A_VAR"],
    "env_allowlist_only": ["B_VAR"],
    "plugin_dir": ["/plugins/p"],
}


def source(**fields: Any) -> resume.Source:
    base = resume.Source(
        csession_id="cs-abcd2345",
        run=1,
        launch_kind="implement",
        run_kind="implement",
        mode="headless",
        cwd="/work",
        repo="example.com/org/repo",
        spec=dict(STORED),
        permission_profile="readonly",
        permission_profile_snapshot=None,
        final=True,
        not_started=False,
        has_prompt=True,
        input_files=(),
        claude_session_id=None,
        tainted=False,
        git_dir="none",
        labels=("kind=implement",),
        session_dir="/state/sessions/cs-abcd2345",
        outcome="done",
    )
    return replace(base, **fields)


def test_same_kind_and_mode_carry_every_stored_flag() -> None:
    carried = resume.carry(source(), kind="implement", mode="headless", resets=())
    expected = {k: v for k, v in STORED.items() if k != "plugin_dir"}
    assert carried.flags == expected
    assert carried.plugin_dirs == ("/plugins/p",)
    assert carried.permission_profile_label is None


def test_a_kind_change_carries_nothing() -> None:
    carried = resume.carry(source(), kind="review", mode="headless", resets=())
    assert (carried.flags, carried.plugin_dirs) == ({}, ())


def test_a_mode_change_drops_timeout_and_max_turns_only() -> None:
    carried = resume.carry(source(), kind="implement", mode="interactive", resets=())
    assert "timeout" not in carried.flags and "max_turns" not in carried.flags
    assert carried.flags["model"] == "m1"


@pytest.mark.parametrize("name", sorted(resume.RESETS))
def test_each_reset_drops_exactly_its_setting(name: str) -> None:
    carried = resume.carry(source(), kind="implement", mode="headless", resets=(name,))
    key = resume.RESETS[name]
    everything = {**{k: v for k, v in STORED.items() if k != "plugin_dir"}}
    if key == "plugin_dir":
        assert carried.plugin_dirs == ()
        assert carried.flags == everything
    else:
        assert key not in carried.flags
        assert carried.flags == {k: v for k, v in everything.items() if k != key}


def test_reset_names_match_the_cli() -> None:
    from claude_session import cli

    assert resume.RESETS == cli.RESETTABLE


def test_reads_third_party_cannot_be_reset() -> None:
    assert "reads-third-party" not in resume.RESETS


def test_a_profile_file_is_carried_as_the_previous_runs_snapshot() -> None:
    spec = {**STORED, "permission_profile": "/home/u/profile.json"}
    carried = resume.carry(
        source(spec=spec, permission_profile="file:abc", permission_profile_snapshot=SNAPSHOT),
        kind="implement",
        mode="headless",
        resets=(),
    )
    assert carried.flags["permission_profile"] == SNAPSHOT
    assert carried.permission_profile_label == "file:abc"


def test_a_snapshot_carried_twice_keeps_its_recorded_label() -> None:
    # The previous run carried a snapshot and ended before writing its own.
    spec = {**STORED, "permission_profile": SNAPSHOT, launch.CARRIED_LABEL: "file:abc"}
    carried = resume.carry(
        source(spec=spec, permission_profile=None), kind="implement", mode="headless", resets=()
    )
    assert carried.flags["permission_profile"] == SNAPSHOT
    assert carried.permission_profile_label == "file:abc"


def test_a_profile_file_without_a_snapshot_is_never_re_read() -> None:
    spec = {**STORED, "permission_profile": "/home/u/profile.json"}
    carried = resume.carry(
        source(spec=spec, permission_profile=None), kind="implement", mode="headless", resets=()
    )
    assert carried.permission_profile_label is None
    # kinds.resolve refuses to carry a file it would have to re-read without a label.
    previous = kinds.Previous(repo=None, flags=carried.flags)
    from claude_session import config

    with pytest.raises(UsageError, match="no recorded snapshot"):
        kinds.resolve(config.load(None), "implement", mode="headless", previous=previous)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("plugin_dir", "not-a-list"),
        ("plugin_dir", ["relative/dir"]),
        ("plugin_dir", ["/nul\x00byte"]),
        ("model", "-p"),
        ("max_turns", 0),
        ("max_turns", 2**60),
        ("timeout", "0"),
        ("timeout", "1e3"),
        ("setting_sources", "user,bogus"),
        ("env_allow", ["NOT-A-NAME"]),
        ("env_allowlist_only", ["$(evil)"]),
    ],
)
def test_a_tampered_stored_flag_exits_2(key: str, value: Any) -> None:
    with pytest.raises(UsageError):
        resume.carry(
            source(spec={**STORED, key: value}), kind="implement", mode="headless", resets=()
        )


def test_conversation_resumes_the_latest_claude_id() -> None:
    claude = str(uuid.uuid4())
    talk, warnings = resume.conversation(source(claude_session_id=claude), fork=False)
    assert talk == launch.Conversation(resume=True, resume_from=claude)
    assert warnings == []


def test_conversation_of_a_fork_gets_a_new_id() -> None:
    claude = str(uuid.uuid4())
    talk, warnings = resume.conversation(source(claude_session_id=claude), fork=True)
    assert (talk.resume_from, talk.fork) == (claude, True)
    assert talk.session_id not in (None, claude)
    assert warnings == []


@pytest.mark.parametrize("fork", [False, True])
def test_no_captured_claude_id_starts_a_fresh_conversation(fork: bool) -> None:
    talk, warnings = resume.conversation(source(claude_session_id=None), fork=fork)
    assert (talk.resume, talk.resume_from, talk.fork) == (True, None, False)
    assert talk.session_id is not None and str(uuid.UUID(talk.session_id)) == talk.session_id
    assert len(warnings) == 1 and "fresh conversation" in warnings[0]
