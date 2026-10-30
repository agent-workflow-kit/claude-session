"""Validate JSON output against the schemas under ``schemas/``.

Every schema is registered under its ``$id`` so relative ``$ref``s between them resolve.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMA_ROOT = Path(__file__).resolve().parent.parent / "schemas"


@cache
def registry() -> Registry[Any]:
    resources: list[tuple[str, Resource[Any]]] = []
    for path in sorted(SCHEMA_ROOT.rglob("*.json")):
        contents = json.loads(path.read_text(encoding="utf-8"))
        resources.append((contents["$id"], Resource.from_contents(contents)))
    return Registry().with_resources(resources)


def validate(instance: object, schema_id: str) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``instance`` matches ``schema_id``."""
    schema = registry().contents(schema_id)
    Draft202012Validator(schema, registry=registry()).validate(instance)
