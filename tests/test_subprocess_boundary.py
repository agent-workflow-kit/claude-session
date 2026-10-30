"""Static check: ``gateway`` is the only module that can start a process (design.md, Modules)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "claude_session"
GATEWAY = PACKAGE / "gateway.py"

PACKAGE_NAME = "claude_session"
GATEWAY_MODULE = f"{PACKAGE_NAME}.gateway"

# Modules whose import alone means starting processes (or, for ctypes, calling libc directly).
FORBIDDEN_MODULES = {
    "subprocess",
    "multiprocessing",
    "pty",
    "asyncio.subprocess",
    "_posixsubprocess",
    "concurrent.futures.process",
    "ctypes",
    "webbrowser",
}
# Process-starting functions reachable without those imports, by full name.
FORBIDDEN_NAMES = {
    *(
        f"{mod}.{func}"
        for mod in ("os", "posix")
        for func in (
            "system",
            "popen",
            "fork",
            "forkpty",
            "posix_spawn",
            "posix_spawnp",
            *(f"exec{s}" for s in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")),
            *(f"spawn{s}" for s in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")),
        )
    ),
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "concurrent.futures.ProcessPoolExecutor",
}
IMPORT_FUNCS = {"__import__", "builtins.__import__", "importlib.import_module", "import_module"}


def _gateway_internals() -> set[str]:
    """The modules gateway imports: reaching them through gateway is a re-export."""
    names: set[str] = set()
    for node in ast.parse(GATEWAY.read_text()).body:
        if isinstance(node, ast.Import):
            names |= {a.asname or a.name.split(".")[0] for a in node.names}
    return names


GATEWAY_INTERNALS = _gateway_internals()


def _bad(name: str) -> bool:
    """Whether the fully qualified ``name`` reaches a process starter."""
    if any(name == m or name.startswith(m + ".") for m in FORBIDDEN_MODULES):
        return True
    if any(name == f or name.startswith(f + ".") for f in FORBIDDEN_NAMES):
        return True
    if name.startswith(GATEWAY_MODULE + "."):
        attr = name[len(GATEWAY_MODULE) + 1 :].split(".")[0]
        return attr.startswith("_") or attr in GATEWAY_INTERNALS
    return False


def _from_module(node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    # The package is flat: every relative import is relative to claude_session.
    return PACKAGE_NAME + (f".{node.module}" if node.module else "")


def _dotted(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """The full name of ``a.b.c`` with ``a`` resolved through the module's imports."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(aliases.get(node.id, node.id))
    return ".".join(reversed(parts))


def violations(source: str) -> list[str]:
    tree = ast.parse(source)
    aliases: dict[str, str] = {}
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[a.asname or a.name.split(".")[0]] = (
                    a.name if a.asname else a.name.split(".")[0]
                )
                if _bad(a.name):
                    found.append(f"import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            module = _from_module(node)
            if _bad(module):
                found.append(f"from {module} import")
            for a in node.names:
                full = f"{module}.{a.name}"
                aliases[a.asname or a.name] = full
                if _bad(full):
                    found.append(f"from {module} import {a.name}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            name = _dotted(node, aliases)
            if name is not None and _bad(name):
                found.append(name)
        elif (
            isinstance(node, ast.Call)
            and _dotted(node.func, aliases) in IMPORT_FUNCS
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and _bad(node.args[0].value)
        ):
            found.append(f"{_dotted(node.func, aliases)}({node.args[0].value!r})")
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
        # module aliases
        "import os as o\no.system('x')",
        "import posix as p\np.fork()",
        "import asyncio as aio\naio.create_subprocess_exec('/bin/x')",
        "from os import system as s",
        # import_module reached as an attribute
        "import importlib\nimportlib.import_module('subprocess')",
        "import importlib as il\nil.import_module('multiprocessing')",
        "import builtins\nbuiltins.__import__('subprocess')",
        # the gateway's own imports, or its private names, re-exported
        "from .gateway import subprocess",
        "from .gateway import os",
        "from claude_session.gateway import subprocess as sp",
        "from .gateway import _pdeathsig_preexec",
        "from . import gateway\ngateway.subprocess.run(['/bin/x'])",
        "from . import gateway as g\ng.os.system('x')",
        "import claude_session.gateway\nclaude_session.gateway.subprocess.Popen(['/bin/x'])",
        "from claude_session import gateway\ngateway._prctl()",
        # other process starters
        "from concurrent.futures import ProcessPoolExecutor",
        "import concurrent.futures\nconcurrent.futures.ProcessPoolExecutor()",
        "import concurrent.futures as cf\ncf.ProcessPoolExecutor()",
        "import concurrent.futures.process",
        "import ctypes",
        "from ctypes import CDLL",
        "import webbrowser",
    ],
)
def test_checker_catches(source: str) -> None:
    assert violations(source)


@pytest.mark.parametrize(
    "source",
    [
        "import os\nos.kill(1, 0)",
        "import os.path",
        "from os import getpid",
        "import signal",
        "from .gateway import DEVNULL, SystemGateway",
        "from . import gateway\ngateway.SystemGateway()",
        "import concurrent.futures\nconcurrent.futures.ThreadPoolExecutor()",
        "import importlib\nimportlib.import_module('json')",
    ],
)
def test_checker_allows(source: str) -> None:
    assert violations(source) == []
