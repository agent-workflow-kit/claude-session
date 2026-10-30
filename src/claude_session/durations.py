"""Duration syntax (interface.md, Identifiers: Durations), shared by flags, config and environment.

A duration is a non-negative number of seconds (fractions allowed), or ``<number><unit>``
matching ``^\\d+(\\.\\d+)?(ms|s|m|h|d)$``. Where a bound may be absent, ``none`` (or ``null`` in
config) means no bound. Values are returned as seconds (``float``).
Standard library only.
"""

from __future__ import annotations

import math
import re

UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
WITH_UNIT = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h|d)$")
BARE = re.compile(r"^\d+(?:\.\d+)?$")
NONE = "none"


def parse(value: str | int | float, *, positive: bool = False, bare: bool = True) -> float:
    """Seconds for ``value``: a number, a bare-number string (when ``bare``) or a string with
    a unit. ``positive`` refuses zero (bounding durations). Raises ``ValueError``."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"expected a duration, got {type(value).__name__}")
    if isinstance(value, str):
        match = WITH_UNIT.match(value)
        if match:
            seconds = float(match.group(1)) * UNITS[match.group(2)]
        elif bare and BARE.match(value):
            seconds = float(value)
        else:
            form = "seconds or <number><unit>" if bare else "<number><unit>"
            raise ValueError(
                f"invalid duration {value!r}: expected {form} with unit ms, s, m, h or d"
            )
    else:
        seconds = float(value)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f"invalid duration {value!r}: must be a non-negative number")
    if positive and seconds <= 0:
        raise ValueError(f"invalid duration {value!r}: must be greater than zero")
    return seconds


def parse_bound(
    value: str | int | float | None, *, positive: bool = True, bare: bool = True
) -> float | None:
    """Like ``parse``, but ``none`` (or ``None``) means no bound and gives ``None``."""
    if value is None or value == NONE:
        return None
    return parse(value, positive=positive, bare=bare)
