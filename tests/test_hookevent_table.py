"""The state transition table, exhaustively, against interface.md (State transitions).

``expected`` below restates each interface row by its "Resulting state (activity)" column,
independently of ``hookevent``'s four-value encoding, and every live situation is checked
against every table event.
"""

from __future__ import annotations

import itertools

import pytest

from claude_session import hookevent
from claude_session.hookevent import Values

ACTIVITIES = (None, "working", "idle")
PROMPTS = (None, "permission", "question", "elicitation")
MODES = ("headless", "interactive")

ALL_VALUES = [
    Values(activity=a, prompt=p, escalation_open=e, has_report=r)
    for a, p, e, r in itertools.product(ACTIVITIES, PROMPTS, (False, True), (False, True))
]


def derived(v: Values) -> tuple[str, str | None, str | None]:
    """interface.md, Derivation (first match)."""
    if v.prompt is not None:
        return "needs-input", v.prompt, v.activity
    if v.escalation_open:
        return "needs-input", "escalation", v.activity
    if v.has_report:
        return "reported", None, v.activity
    if v.activity is None:
        return "starting", None, None
    return v.activity, None, v.activity


def kept_prompt_else(v: Values, activity: str) -> tuple[str, str | None, str | None]:
    """ "derived: prompt kept (needs-input(permission|question|elicitation)), else
    needs-input(escalation), reported or <activity> (<activity>)"."""
    if v.prompt is not None:
        return "needs-input", v.prompt, activity
    if v.escalation_open:
        return "needs-input", "escalation", activity
    if v.has_report:
        return "reported", None, activity
    return activity, None, activity


def expected(v: Values, event: str, mode: str, started: bool) -> tuple[str, str | None, str | None]:
    before = derived(v)
    state = before[0]
    if event == "session_start_new":
        if state != "starting":
            return before
        activity = "working" if mode == "headless" else "idle"
        return activity, None, activity
    if event in ("session_start_other", "session_end"):
        return before
    if event == "user_prompt_submit":
        return ("reported" if v.has_report else "working"), None, "working"
    if event == "report_final":
        if v.prompt is not None:
            return "needs-input", v.prompt, v.activity
        return "reported", None, v.activity
    if event == "report_needs_input":
        if v.prompt is not None:
            return "needs-input", v.prompt, v.activity
        return "needs-input", "escalation", v.activity
    # The remaining rows apply only "after SessionStart".
    if not started:
        return before
    if event in ("permission", "question", "elicitation"):
        return "needs-input", event, "working"
    if event == "permission_notification":
        # A waiting question or elicitation is kept; otherwise as PermissionRequest.
        if v.prompt in ("question", "elicitation"):
            return "needs-input", v.prompt, "working"
        return "needs-input", "permission", "working"
    if event == "post_tool_use":
        if v.escalation_open:
            return "needs-input", "escalation", "working"
        return ("reported" if v.has_report else "working"), None, "working"
    if event == "stop_background":
        return kept_prompt_else(v, "working")
    if event == "stop_idle":
        return kept_prompt_else(v, "idle")
    raise AssertionError(event)


def test_the_table_covers_exactly_the_interface_events() -> None:
    assert set(hookevent.TABLE) == {
        "session_start_new",
        "session_start_other",
        "user_prompt_submit",
        "permission",
        "permission_notification",
        "question",
        "elicitation",
        "post_tool_use",
        "stop_background",
        "stop_idle",
        "report_final",
        "report_needs_input",
        "session_end",
    }


@pytest.mark.parametrize("event", sorted(hookevent.TABLE))
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("started", (False, True))
def test_every_live_situation_and_event_matches_the_interface(
    event: str, mode: str, started: bool
) -> None:
    for v in ALL_VALUES:
        after = hookevent.transition(v, event, mode=mode, session_started=started)
        assert hookevent.derive(after) == expected(v, event, mode, started), (v, event)


@pytest.mark.parametrize("v", ALL_VALUES)
def test_derive_matches_the_interface_derivation(v: Values) -> None:
    assert hookevent.derive(v) == derived(v)


def test_permission_and_elicitation_take_precedence_over_escalation() -> None:
    v = Values(activity="working", prompt=None, escalation_open=True, has_report=True)
    for event in ("permission", "question", "elicitation"):
        after = hookevent.transition(v, event, mode="interactive", session_started=True)
        assert hookevent.derive(after) == ("needs-input", event, "working")
        assert after.escalation_open  # still open underneath the prompt


@pytest.mark.parametrize("pending", ["question", "elicitation"])
def test_a_permission_prompt_notification_keeps_a_waiting_question(pending: str) -> None:
    v = Values(activity="working", prompt=pending, escalation_open=False, has_report=False)
    after = hookevent.transition(
        v, "permission_notification", mode="interactive", session_started=True
    )
    assert hookevent.derive(after) == ("needs-input", pending, "working")


def test_a_denied_prompt_stays_until_user_prompt_submit() -> None:
    v = Values(activity="working", prompt="permission", escalation_open=False, has_report=False)
    v = hookevent.transition(v, "stop_idle", mode="interactive", session_started=True)
    assert hookevent.derive(v) == ("needs-input", "permission", "idle")
    v = hookevent.transition(v, "user_prompt_submit", mode="interactive", session_started=True)
    assert hookevent.derive(v) == ("working", None, "working")


def test_an_escalation_stays_across_stop_and_closes_on_user_prompt_submit() -> None:
    v = Values(activity="working", prompt=None, escalation_open=False, has_report=False)
    v = hookevent.transition(v, "report_needs_input", mode="headless", session_started=True)
    v = hookevent.transition(v, "stop_idle", mode="headless", session_started=True)
    assert hookevent.derive(v) == ("needs-input", "escalation", "idle")
    v = hookevent.transition(v, "user_prompt_submit", mode="headless", session_started=True)
    assert hookevent.derive(v) == ("reported", None, "working")


def test_stop_with_background_tasks_keeps_working() -> None:
    v = Values(activity="working", prompt=None, escalation_open=False, has_report=False)
    after = hookevent.transition(v, "stop_background", mode="headless", session_started=True)
    assert hookevent.derive(after) == ("working", None, "working")


def test_unknown_table_event_is_an_error() -> None:
    v = ALL_VALUES[0]
    with pytest.raises(KeyError):
        hookevent.transition(v, "SubagentStop", mode="headless", session_started=True)


@pytest.mark.parametrize("state", ["starting", "working", "idle", "needs-input", "reported"])
def test_values_from_a_stored_state_round_trip(state: str) -> None:
    reasons = {"needs-input": ("permission", "question", "elicitation", "escalation")}
    for reason in reasons.get(state, (None,)):
        for activity in ("working", "idle") if state != "starting" else (None,):
            if state in ("working", "idle") and activity != state:
                continue
            for has_report in (False, True):
                if state == "reported" and not has_report:
                    continue
                if state in ("starting", "working", "idle") and has_report:
                    continue
                escalation = reason == "escalation"
                v = hookevent.values_from(
                    state=state,
                    activity=activity,
                    needs_input_reason=reason,
                    escalation_open=escalation,
                    has_report=has_report,
                )
                assert hookevent.derive(v) == (state, reason, activity)
