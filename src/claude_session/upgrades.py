"""Upgrade warnings (interface.md, Upgrades; design.md, Data model and Reconcile).

The injected hooks of a run name the interpreter path recorded at launch
(``runs.hook_interpreter``). Upgrading claude-session while a session is live can remove
it, and the run's hooks then fail. ``status`` and ``reconcile`` warn for each live run whose
recorded interpreter no longer exists, with ``hook_interpreter_warning``.

TODO(#16): ``status`` adds the warning of the shown session's live run to its top-level
``warnings``.
TODO(#33): ``reconcile`` adds the warning of every live run to its ``warnings``.
"""

from __future__ import annotations

import os

MISSING = (
    "hook interpreter no longer exists: {path!r}; the run's hooks fail until it ends "
    "(upgrade claude-session only while no sessions are live)"
)


def hook_interpreter_missing(path: str | None) -> bool:
    """Whether the recorded hook interpreter ``path`` can no longer run the hooks: it does
    not exist (a dangling symlink included: a venv's ``bin/python`` whose base interpreter
    was removed), is not a regular file or is not executable. An empty, relative or
    NUL-holding path, which a launch never records, counts as missing; None (nothing
    recorded) does not."""
    if path is None:
        return False
    if not path or "\x00" in path or not os.path.isabs(path):
        return True
    try:
        return not (os.path.isfile(path) and os.access(path, os.X_OK))
    except (OSError, ValueError):
        return True


def hook_interpreter_warning(path: str | None) -> str | None:
    """The warning for a live run whose recorded hook interpreter is missing, else None."""
    if not hook_interpreter_missing(path):
        return None
    return MISSING.format(path=path)
