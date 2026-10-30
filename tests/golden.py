"""Golden-file checks for ``--json`` outputs and other schema-bound documents.

A golden file ``tests/golden/[<dir>/]<schema>-<case>.json`` holds the expected output for one
case. Its directory and ``<schema>`` prefix name the schema it must satisfy: a top-level file
belongs to ``schemas/cli/<schema>.json``, a file under ``golden/<dir>/`` (for example
``golden/hooks/``) to ``schemas/<dir>/<schema>.json``. The longest matching schema name wins,
so ``label-add-x`` belongs to ``label-add``. Set ``UPDATE_GOLDEN=1`` to rewrite the files from
the actual output (indented, keys sorted; review the diff before committing).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .schemas import SCHEMA_ROOT, validate

GOLDEN_ROOT = Path(__file__).resolve().parent / "golden"
DEFAULT_SCHEMA_DIR = "cli"


def schema_id_for(name: str) -> str:
    """The ``$id`` of the schema golden file ``name`` (relative to the golden root, without
    ``.json``) belongs to."""
    directory, _, stem_and_case = name.rpartition("/")
    directory = directory or DEFAULT_SCHEMA_DIR
    schema_dir = SCHEMA_ROOT / directory
    stems = sorted((p.stem for p in schema_dir.glob("*.json")), key=len, reverse=True)
    for stem in stems:
        if stem_and_case == stem or stem_and_case.startswith(f"{stem}-"):
            return f"claude-session/{directory}/{stem}.json"
    raise LookupError(f"golden file {name!r} matches no schema under schemas/{directory}/")


def golden_name(path: Path, root: Path = GOLDEN_ROOT) -> str:
    """The golden name of the file ``path`` under ``root``: its relative path without
    ``.json``."""
    return path.relative_to(root).with_suffix("").as_posix()


def assert_golden(
    actual: object,
    name: str,
    *,
    normalize: Callable[[Any], Any] | None = None,
    root: Path = GOLDEN_ROOT,
) -> None:
    """Validate ``actual`` against its schema, then compare it with ``<root>/<name>.json``.

    ``normalize`` (applied to ``actual`` first, before validation, comparison and
    ``UPDATE_GOLDEN`` writes) replaces volatile values such as timestamps or paths.
    """
    if normalize is not None:
        actual = normalize(actual)
    validate(actual, schema_id_for(name))
    path = root / f"{name}.json"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert actual == json.loads(path.read_text(encoding="utf-8"))
