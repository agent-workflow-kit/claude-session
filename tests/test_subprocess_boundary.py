"""Static check: ``gateway`` is the only module that can start a process (design.md, Modules)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "claude_session"
GATEWAY = PACKAGE / "gateway.py"

PACKAGE_NAME = "claude_session"
GATEWAY_MODULE = f"{PACKAGE_NAME}.gateway"

# Modules whose import alone means starting processes (or, for ctypes, calling libc directly;
# pydoc, imaplib and http.server start processes internally).
FORBIDDEN_MODULES = {
    "subprocess",
    "multiprocessing",
    "pty",
    "asyncio.subprocess",
    "_posixsubprocess",
    "concurrent.futures.process",
    "ctypes",
    "_ctypes",
    "webbrowser",
    "pydoc",
    "imaplib",
    "http.server",
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
    # These run ``uname -p`` or ``file``; use sys.platform or os.uname instead.
    *(f"platform.{func}" for func in ("platform", "processor", "architecture", "uname")),
}
# Event-loop methods that start processes, whatever the loop object is called.
LOOP_STARTERS = {"subprocess_exec", "subprocess_shell"}
IMPORT_FUNCS = {
    "__import__",
    "builtins.__import__",
    "importlib.__import__",
    "importlib.import_module",
    "import_module",
}


def _gateway_internals() -> set[str]:
    """The modules gateway imports: reaching them through gateway is a re-export."""
    names: set[str] = set()
    for node in ast.parse(GATEWAY.read_text()).body:
        if isinstance(node, ast.Import):
            names |= {a.asname or a.name.split(".")[0] for a in node.names}
    return names


GATEWAY_INTERNALS = _gateway_internals()


def _canonical(name: str) -> str:
    """``claude_session.<mod>.gateway.x`` (gateway bound in another package module) is
    ``claude_session.gateway.x``."""
    parts = name.split(".")
    if parts[0] == PACKAGE_NAME and "gateway" in parts[1:]:
        return ".".join([GATEWAY_MODULE, *parts[parts.index("gateway", 1) + 1 :]])
    return name


def _bad(name: str) -> bool:
    """Whether the fully qualified ``name`` reaches a process starter."""
    name = _canonical(name)
    if any(name == m or name.startswith(m + ".") for m in FORBIDDEN_MODULES):
        return True
    if any(name == f or name.startswith(f + ".") for f in FORBIDDEN_NAMES):
        return True
    if name.startswith(GATEWAY_MODULE + "."):
        attr = name[len(GATEWAY_MODULE) + 1 :].split(".")[0]
        return attr.startswith("_") or attr in GATEWAY_INTERNALS
    return False


def _bad_star(module: str) -> bool:
    """Whether ``from module import *`` binds a process starter."""
    module = _canonical(module)
    return (
        _bad(module)
        or module == GATEWAY_MODULE
        or any(f.startswith(module + ".") for f in FORBIDDEN_NAMES)
    )


def _from_module(node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    # The package is flat: every relative import is relative to claude_session.
    return PACKAGE_NAME + (f".{node.module}" if node.module else "")


def _import_call(node: ast.AST, aliases: dict[str, str]) -> tuple[str, str] | None:
    """For ``import_module('m')`` / ``__import__('m')`` (also ``name='m'``): the function's
    full name and the module named."""
    if not isinstance(node, ast.Call):
        return None
    func = _dotted(node.func, aliases)
    if func not in IMPORT_FUNCS:
        return None
    arg = node.args[0] if node.args else None
    for kw in node.keywords:
        if kw.arg == "name":
            arg = kw.value
    if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
        return None
    return func, arg.value


def _imported(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """The module an import call returns: ``__import__('a.b')`` gives ``a`` (unless it has a
    fromlist), ``import_module('a.b')`` gives ``a.b``."""
    found = _import_call(node, aliases)
    if found is None:
        return None
    func, module = found
    assert isinstance(node, ast.Call)
    has_fromlist = len(node.args) >= 4 or any(kw.arg == "fromlist" for kw in node.keywords)
    if func.endswith("__import__") and not has_fromlist:
        return module.split(".")[0]
    return module


def _dotted(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """The full name of ``a.b.c`` with ``a`` resolved through the module's imports and simple
    assignments, or through an import call (``import_module('os').system``)."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(aliases.get(node.id, node.id))
    elif (base := _imported(node, aliases)) is not None:
        parts.append(base)
    else:
        return None
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
                if a.name == "*":
                    if _bad_star(module):
                        found.append(f"from {module} import *")
                    continue
                full = f"{module}.{a.name}"
                aliases[a.asname or a.name] = full
                if _bad(full):
                    found.append(f"from {module} import {a.name}")
        elif isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            # Simple aliases: ``x = os``, ``x = os.path``, ``x = import_module('os')``.
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) == 1 and isinstance(targets[0], ast.Name):
                target = _dotted(node.value, aliases)
                if target is not None:
                    aliases[targets[0].id] = target
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr in LOOP_STARTERS:
                found.append(f"<loop>.{node.attr}")
            name = _dotted(node, aliases)
            if name is not None and _bad(name):
                found.append(name)
        elif (call := _import_call(node, aliases)) is not None and _bad(call[1]):
            found.append(f"{call[0]}({call[1]!r})")
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
        # results of import calls
        "import importlib\nimportlib.import_module('os').system('x')",
        "__import__('os').posix_spawn('/bin/x', [], {})",
        "__import__('concurrent.futures').futures.ProcessPoolExecutor()",
        "from importlib import import_module\nimport_module('claude_session.gateway')._prctl()",
        "import importlib\ng = importlib.import_module('claude_session.gateway')\ng.subprocess",
        # keyword forms
        "from importlib import import_module\nimport_module(name='subprocess')",
        "__import__(name='subprocess')",
        "import importlib\nimportlib.__import__('subprocess')",
        # star imports from modules that hold process starters
        "from os import *",
        "from posix import *",
        "from asyncio import *",
        "from concurrent.futures import *",
        "from subprocess import *",
        "from platform import *",
        "from .gateway import *",
        # event loop process starters, whatever the loop is called
        "loop.subprocess_exec(proto, '/bin/x')",
        "self._loop.subprocess_shell(proto, 'x')",
        "import _ctypes",
        # the gateway module bound through another module
        "from .hookevent import gateway\ngateway._prctl()",
        "from claude_session.hookevent import gateway as g\ng.subprocess.Popen(['/bin/x'])",
        "from . import hookevent\nhookevent.gateway.os.system('x')",
        # simple assignment aliases
        "import os\nx = os\nx.system('x')",
        "import os\nrun = os.posix_spawn",
        "import os\nx = os\ny = x\ny.fork()",
        # stdlib modules that start processes internally
        "import platform\nplatform.platform()",
        "import platform\nplatform.processor()",
        "import platform\nplatform.architecture()",
        "from platform import uname",
        "import pydoc",
        "import imaplib",
        "import http.server",
        "from http import server",
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
        "import importlib\nimportlib.import_module('os').getpid()",
        "import os\nx = os\nx.getpid()",
        "from json import *",
        "import platform\nplatform.system()",
        "import sys\nsys.platform",
        "import http.client",
        "from . import hookevent\nhookevent.gateway_ok",
    ],
)
def test_checker_allows(source: str) -> None:
    assert violations(source) == []
