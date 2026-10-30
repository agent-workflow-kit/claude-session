"""The schema harness: every schema is valid and registered, and the fixtures match them."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError
from referencing.exceptions import Unresolvable

from .golden import GOLDEN_ROOT, assert_golden, schema_id_for
from .schemas import SCHEMA_ROOT, registry, validate

ROOT = SCHEMA_ROOT.parent
SCHEMA_FILES = sorted(SCHEMA_ROOT.rglob("*.json"))
GOLDEN_FILES = sorted(GOLDEN_ROOT.glob("*.json"))


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def rel(path: Path) -> str:
    return path.relative_to(SCHEMA_ROOT).as_posix()


def test_schemas_exist() -> None:
    names = {rel(p) for p in SCHEMA_FILES}
    assert {"config.json", "log.json", "audit.json", "cli/error.json"} <= names


@pytest.mark.parametrize("path", SCHEMA_FILES, ids=rel)
def test_schema_is_valid_draft_2020_12(path: Path) -> None:
    schema = load(path)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("path", SCHEMA_FILES, ids=rel)
def test_schema_id_names_its_path(path: Path) -> None:
    assert load(path)["$id"] == f"claude-session/{rel(path)}"


@pytest.mark.parametrize("path", SCHEMA_FILES, ids=rel)
def test_registry_is_keyed_by_id(path: Path) -> None:
    schema = load(path)
    assert registry().contents(schema["$id"]) == schema


def refs(node: object) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value
            else:
                yield from refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from refs(item)


@pytest.mark.parametrize("path", SCHEMA_FILES, ids=rel)
def test_every_ref_resolves(path: Path) -> None:
    schema = load(path)
    resolver = registry().resolver(base_uri=schema["$id"])
    for ref in refs(schema):
        try:
            resolver.lookup(ref)
        except Unresolvable as exc:
            pytest.fail(f"{rel(path)}: unresolvable $ref {ref!r}: {exc}")


def test_validate_rejects_a_bad_instance() -> None:
    with pytest.raises(ValidationError):
        validate({"ts": "x", "level": "loud", "event": "launch"}, "claude-session/log.json")


def test_config_example_validates_against_the_config_schema() -> None:
    config = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    validate(config, "claude-session/config.json")


def test_config_schema_rejects_an_unknown_key() -> None:
    config = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    config["no_such_key"] = 1
    with pytest.raises(ValidationError):
        validate(config, "claude-session/config.json")


def test_golden_files_exist() -> None:
    assert GOLDEN_FILES


@pytest.mark.parametrize("path", GOLDEN_FILES, ids=lambda p: p.name)
def test_golden_file_validates_against_its_schema(path: Path) -> None:
    validate(load(path), schema_id_for(path.stem))


@pytest.mark.parametrize(
    ("name", "schema_id"),
    [
        ("error-usage", "claude-session/cli/error.json"),
        ("label-add-two-pairs", "claude-session/cli/label-add.json"),
        ("exec-dry-run-review", "claude-session/cli/exec-dry-run.json"),
        ("exec-done", "claude-session/cli/exec.json"),
        ("list", "claude-session/cli/list.json"),
    ],
)
def test_golden_name_picks_the_longest_schema(name: str, schema_id: str) -> None:
    assert schema_id_for(name) == schema_id


def test_golden_name_without_a_schema_is_an_error() -> None:
    with pytest.raises(LookupError):
        schema_id_for("nonsense-case")


def test_assert_golden_validates_before_comparing() -> None:
    golden = load(GOLDEN_ROOT / "error-usage.json")
    assert_golden(golden, "error-usage")
    with pytest.raises(ValidationError):
        assert_golden({"error": {"code": "usage"}}, "error-usage")
    with pytest.raises(AssertionError):
        assert_golden({"error": {"code": "usage", "message": "other"}}, "error-usage")
