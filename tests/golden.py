"""Golden-file checks for ``--json`` outputs.

A golden file ``tests/golden/<schema>-<case>.json`` holds the expected output for one case; its
``<schema>`` prefix names the CLI schema it must satisfy (``schemas/cli/<schema>.json``; the
longest matching name wins, so ``label-add-x`` belongs to ``label-add``). Set
``UPDATE_GOLDEN=1`` to rewrite the files from the actual output (review the diff before
committing).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .schemas import SCHEMA_ROOT, validate

GOLDEN_ROOT = Path(__file__).resolve().parent / "golden"
CLI_SCHEMAS = SCHEMA_ROOT / "cli"


def schema_id_for(name: str) -> str:
    """The ``$id`` of the CLI schema golden file ``name`` (without ``.json``) belongs to."""
    stems = sorted((p.stem for p in CLI_SCHEMAS.glob("*.json")), key=len, reverse=True)
    for stem in stems:
        if name == stem or name.startswith(f"{stem}-"):
            return f"claude-session/cli/{stem}.json"
    raise LookupError(f"golden file {name!r} matches no schema under schemas/cli/")


def assert_golden(actual: object, name: str) -> None:
    """Validate ``actual`` against its schema, then compare it with ``golden/<name>.json``."""
    validate(actual, schema_id_for(name))
    path = GOLDEN_ROOT / f"{name}.json"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.write_text(json.dumps(actual) + "\n", encoding="utf-8")
    assert actual == json.loads(path.read_text(encoding="utf-8"))
