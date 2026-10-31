"""Text safe to print: the one sanitizer for every line of human output (stdout views, stderr
errors and warnings, tracebacks) and the surrogate replacement for JSON output.

Values shown to a human may be untrusted (agent reports, stored paths, labels, ids typed by a
caller). ``display`` makes each one inert on a terminal: nothing in it can move the cursor,
change the title or clipboard (OSC), start a new line, reorder or hide text, or pass for an
escape ``display`` itself wrote.
"""

from __future__ import annotations

import re
import unicodedata

# Escaped besides C0, DEL, C1, backslash, surrogates and every category Cf character: line and
# paragraph separators (they break lines like a newline), the bidi controls (Cf already;
# listed so the set does not depend on the Unicode database version) and invisible fillers
# that are not Cf.
_ALWAYS_ESCAPED = frozenset(
    "\u2028\u2029"  # line and paragraph separators
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"  # bidi
    "\u034f\u115f\u1160\u3164\uffa0\u180e"  # grapheme joiner, Hangul fillers, MVS
)
_SURROGATES = re.compile("[\ud800-\udfff]")


def display(value: object, extra: str = "") -> str:
    """``value`` (``-`` for None) as inert text for one line of human output.

    Escaped as ``\\xNN``, ``\\uNNNN`` or ``\\UNNNNNNNN``: C0 controls (TAB, CR and LF
    included), DEL, C1 controls (NEL included), line and paragraph separators, bidi controls,
    lone surrogates, every format (Cf) character (zero-width characters, BOM, soft hyphen,
    word joiner, invisible operators) and a few invisible fillers. A backslash becomes
    ``\\\\``, so an escape in the output was always written here. ``extra`` lists more
    (ASCII) characters to escape, such as a column separator."""
    text = "-" if value is None else str(value)
    return "".join(_escape(char, extra) for char in text)


def _escape(char: str, extra: str) -> str:
    code = ord(char)
    if char == "\\":
        return "\\\\"
    if code < 0x20 or 0x7F <= code <= 0x9F or char in extra:
        return f"\\x{code:02x}"
    if code < 0xA0:
        return char
    if char in _ALWAYS_ESCAPED or 0xD800 <= code <= 0xDFFF or unicodedata.category(char) == "Cf":
        return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"
    return char


def display_width(text: str) -> int:
    """Terminal columns of ``text`` (already through ``display``): East Asian wide and
    fullwidth characters take two, combining marks none, everything else one."""
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def no_surrogates(text: str) -> str:
    """``text`` with every lone surrogate (from a ``\\udXXX`` JSON escape or an argv byte that
    is not UTF-8) replaced by U+FFFD, so it encodes as strict UTF-8."""
    return _SURROGATES.sub("\ufffd", text)
