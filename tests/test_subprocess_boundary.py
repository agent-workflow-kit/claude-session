"""Static check: ``gateway`` is the only module that can start a process (design.md, Modules)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "claude_session"
GATEWAY = PACKAGE / "gateway.py"

# Modules whose import alone means starting processes.
FORBIDDEN_MODULES = {
    "subprocess",
    "multiprocessing",
    "pty",
    "asyncio.subprocess",
    "_posixsubprocess",
}
# Process-starting functions reachable without those imports.
FORBIDDEN_OS_FUNCS = {
    "system",
    "popen",
    "fork",
    "forkpty",
    "posix_spawn",
    "posix_spawnp",
    *(f"exec{s}" for s in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")),
    *(f"spawn{s}" for s in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")),
}
FORBIDDEN_ASYNCIO_FUNCS = {"create_subprocess_exec", "create_subprocess_shell"}


def _forbidden(name: str) -> bool:
    return any(name == m or name.startswith(m + ".") for m in FORBIDDEN_MODULES)


def violations(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names if _forbidden(a.name)]
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            if _forbidden(node.module):
                found.append(f"from {node.module} import")
            elif node.module in ("os", "posix"):
                found += [
                    f"from os import {a.name}" for a in node.names if a.name in FORBIDDEN_OS_FUNCS
                ]
            elif node.module == "asyncio":
                found += [
                    f"from asyncio import {a.name}"
                    for a in node.names
                    if a.name in FORBIDDEN_ASYNCIO_FUNCS or a.name == "subprocess"
                ]
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in ("os", "posix") and node.attr in FORBIDDEN_OS_FUNCS:
                found.append(f"{node.value.id}.{node.attr}")
            if node.value.id == "asyncio" and node.attr in FORBIDDEN_ASYNCIO_FUNCS:
                found.append(f"asyncio.{node.attr}")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("__import__", "import_module")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and _forbidden(node.args[0].value)
        ):
            found.append(f"{node.func.id}({node.args[0].value!r})")
    return found


def test_no_module_other_than_gateway_starts_processes() -> None:
    modules = sorted(p for p in PACKAGE.rglob("*.py") if p != GATEWAY)
    assert modules, "package not found"
    found = {str(p.relative_to(PACKAGE)): v for p in modules if (v := violations(p.read_text()))}
    assert found == {}


def test_gateway_is_the_module_that_does() -> None:
    assert "import subprocess" in violations(GATEWAY.read_text())


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess",
        "import subprocess as sp",
        "from subprocess import run",
        "import multiprocessing.pool",
        "import pty",
        "from asyncio import subprocess",
        "import asyncio.subprocess",
        "import os\nos.system('x')",
        "import os\nos.execv('/bin/x', [])",
        "import os\nos.posix_spawn('/bin/x', [], {})",
        "from os import fork",
        "import asyncio\nasyncio.create_subprocess_exec('/bin/x')",
        "__import__('subprocess')",
        "from importlib import import_module\nimport_module('subprocess')",
        "def f():\n    import subprocess",
    ],
)
def test_checker_catches(source: str) -> None:
    assert violations(source)


@pytest.mark.parametrize(
    "source",
    ["import os\nos.kill(1, 0)", "import os.path", "from os import getpid", "import signal"],
)
def test_checker_allows(source: str) -> None:
    assert violations(source) == []
