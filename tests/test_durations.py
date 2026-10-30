"""Duration syntax (interface.md, Identifiers: Durations)."""

from __future__ import annotations

import pytest

from claude_session import durations


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("0", 0.0),
        ("15", 15.0),
        ("0.5", 0.5),
        ("500ms", 0.5),
        ("90s", 90.0),
        ("0.5s", 0.5),
        ("10m", 600.0),
        ("2h", 7200.0),
        ("1d", 86400.0),
        ("1.5m", 90.0),
        (0, 0.0),
        (30, 30.0),
        (0.25, 0.25),
    ],
)
def test_valid(text: str | float, seconds: float) -> None:
    assert durations.parse(text) == pytest.approx(seconds)


@pytest.mark.parametrize(
    "text",
    ["", "-5", "-5s", "5 s", "5x", "s", ".5s", "5.s", "1e3", "5S", " 5s", "none", "inf", "nan"],
)
def test_invalid(text: str) -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        durations.parse(text)


@pytest.mark.parametrize("value", [-1, -0.5, float("inf"), float("nan")])
def test_invalid_numbers(value: float) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        durations.parse(value)


@pytest.mark.parametrize("value", [True, False, None, [], {}])
def test_rejects_non_durations(value: object) -> None:
    with pytest.raises(ValueError, match="expected a duration"):
        durations.parse(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0, "0", "0s", "0.0m", "0ms", 0.0])
def test_positive_refuses_zero(value: str | float) -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        durations.parse(value, positive=True)


def test_bare_strings_can_be_refused() -> None:
    with pytest.raises(ValueError, match="<number><unit>"):
        durations.parse("15", bare=False)
    assert durations.parse("15s", bare=False) == 15.0
    assert durations.parse(15, bare=False) == 15.0


@pytest.mark.parametrize("value", [None, "none"])
def test_bound_none_means_no_bound(value: str | None) -> None:
    assert durations.parse_bound(value) is None


def test_bound_parses_and_is_positive_by_default() -> None:
    assert durations.parse_bound("10m") == 600.0
    with pytest.raises(ValueError, match="greater than zero"):
        durations.parse_bound("0")
    assert durations.parse_bound("0", positive=False) == 0.0
