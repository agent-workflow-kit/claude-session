"""The injected-hook fast path (``hook-event``); see design.md, Hook-event fast path.

``cli`` dispatches here before building the argument parser, so this module and everything it
imports must stay standard library only (plus ``store``, ``outbox`` and ``gateway`` once they
exist): never ``config``, Pydantic or YAML.
"""

from __future__ import annotations


def main(argv: list[str]) -> int:
    """Handle one injected hook event.

    Not implemented yet (Stage 1). ``hook-event`` never fails a session: like an internal error,
    an unimplemented handler exits 0 with no output.
    """
    del argv
    return 0
