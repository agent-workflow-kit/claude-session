"""Static check: ``gateway`` is the only module that can start a process (design.md, Modules)."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "claude_session"
GATEWAY = PACKAGE / "gateway.py"

PACKAGE_NAME = "claude_session"
GATEWAY_MODULE = f"{PACKAGE_NAME}.gateway"

# Modules whose import alone means starting processes (or, for ctypes, calling libc directly;
# pydoc, imaplib, http.server, socketserver, ensurepip, venv, _osx_support, pipes and mailcap
# start processes internally, and asyncio.base_subprocess holds the event loop's process
# transports).
FORBIDDEN_MODULES = {
    "subprocess",
    "multiprocessing",
    "pty",
    "asyncio.subprocess",
    "asyncio.base_subprocess",
    "_posixsubprocess",
    "concurrent.futures.process",
    "ctypes",
    "_ctypes",
    "webbrowser",
    "pydoc",
    "imaplib",
    "http.server",
    "socketserver",
    "ensurepip",
    "venv",
    "_osx_support",
    "pipes",
    "mailcap",
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
    # These run ``uname -p``, ``file`` or ``ifconfig``; use sys.platform, os.uname or uuid4.
    *(f"platform.{func}" for func in ("platform", "processor", "architecture", "uname")),
    "uuid.getnode",
    "uuid.uuid1",
}
# Modules whose private names (``uuid._unix_getnode``, ``platform._syscmd_file``) start processes.
PRIVATE_STARTERS = {"uuid", "platform"}
# Attributes that start processes whatever object they are read from (event loops).
FORBIDDEN_ATTRS = {"subprocess_exec", "subprocess_shell", "_make_subprocess_transport"}
IMPORT_FUNCS = {
    "__import__",
    "builtins.__import__",
    "importlib.__import__",
    "importlib.import_module",
    "import_module",
}
# Top-level modules a reference to which reaches a process starter. Read as an attribute of an
# imported module (``shutil.os``, ``posixpath.os``) or imported from another module
# (``from shutil import os``), one of these is a re-export; on any other object
# (``args.pty``, ``self._os``) it is just an attribute name. ``platform``, ``uuid`` and ``http``
# are left out (common attribute names); their starters are caught by full name.
REEXPORT_ROOTS = {
    "os",
    "posix",
    "subprocess",
    "multiprocessing",
    "pty",
    "asyncio",
    "_posixsubprocess",
    "concurrent",
    "ctypes",
    "_ctypes",
    "webbrowser",
    "pydoc",
    "imaplib",
    "importlib",
    "builtins",
    "socketserver",
    "ensurepip",
    "venv",
    "_osx_support",
    "pipes",
    "mailcap",
}


def _reexport_root(attr: str) -> bool:
    """Whether ``attr`` names a re-export root, also as a private alias (``tempfile._os``)."""
    return attr in REEXPORT_ROOTS or attr.lstrip("_") in REEXPORT_ROOTS


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


def _bad_function(name: str) -> bool:
    """Whether ``name`` (``a.b``, possibly read off any object) is a process-starting function
    by its full name, or a private name of a module in ``PRIVATE_STARTERS``."""
    if any(name == f or name.startswith(f + ".") for f in FORBIDDEN_NAMES):
        return True
    module, _, attr = name.partition(".")
    attr = attr.split(".")[0]
    dunder = attr.startswith("__") and attr.endswith("__")
    return module in PRIVATE_STARTERS and attr.startswith("_") and not dunder


def _bad(name: str) -> bool:
    """Whether the fully qualified ``name`` reaches a process starter."""
    name = _canonical(name)
    if any(name == m or name.startswith(m + ".") for m in FORBIDDEN_MODULES):
        return True
    if _bad_function(name):
        return True
    if name.startswith(GATEWAY_MODULE + "."):
        attr = name[len(GATEWAY_MODULE) + 1 :].split(".")[0]
        return attr.startswith("_") or attr in GATEWAY_INTERNALS
    return False


def _package_module(name: str) -> bool:
    """Whether ``name`` is this package or one of its modules (any may bind ``gateway``)."""
    if name == PACKAGE_NAME:
        return True
    head, _, rest = name.partition(".")
    return head == PACKAGE_NAME and (PACKAGE / f"{rest.replace('.', '/')}.py").exists()


def _dangerous(name: str) -> bool:
    """Whether a bare reference to ``name`` (not read through an attribute) can reach a
    process starter: a forbidden module or name, a module holding one, or a package module."""
    name = _canonical(name)
    holders = FORBIDDEN_MODULES | FORBIDDEN_NAMES | (IMPORT_FUNCS - {"__import__", "import_module"})
    return _bad(name) or _package_module(name) or any(h.startswith(name + ".") for h in holders)


def _bad_star(module: str) -> bool:
    """Whether ``from module import *`` binds a process starter (or a forbidden submodule)."""
    module = _canonical(module)
    return (
        _bad(module)
        or module == GATEWAY_MODULE
        or any(f.startswith(module + ".") for f in FORBIDDEN_NAMES | FORBIDDEN_MODULES)
    )


def _from_module(node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    # The package is flat: every relative import is relative to claude_session.
    return PACKAGE_NAME + (f".{node.module}" if node.module else "")


class Dynamic(Exception):
    """An import call whose module cannot be read from the source."""


def _arg(node: ast.Call, index: int, keyword: str) -> ast.expr | None:
    for kw in node.keywords:
        if kw.arg == keyword:
            return kw.value
    return node.args[index] if len(node.args) > index else None


def _constant(node: ast.expr | None, kind: type) -> object:
    if node is None:
        return None
    if isinstance(node, ast.Name) and node.id == "__package__" and kind is str:
        return PACKAGE_NAME  # every module of the flat package is in claude_session
    if not isinstance(node, ast.Constant) or not isinstance(node.value, kind):
        raise Dynamic
    return node.value


def _import_call(node: ast.AST, aliases: dict[str, str]) -> tuple[str, str] | None:
    """For an import call (``import_module('m')``, ``__import__('m')``, keywords too): the
    function's full name and the absolute module named, relative names resolved through
    ``package`` (``import_module``) or ``level`` (``__import__``). Raises ``Dynamic`` when the
    module is not a literal."""
    if not isinstance(node, ast.Call):
        return None
    func = _dotted(node.func, aliases)
    if func not in IMPORT_FUNCS:
        return None
    name = _constant(_arg(node, 0, "name"), str)
    if not isinstance(name, str):
        raise Dynamic
    if func.endswith("import_module"):
        if name.startswith("."):
            package = _constant(_arg(node, 1, "package"), str)
            if not isinstance(package, str):
                raise Dynamic
            name = importlib.util.resolve_name(name, package)
        return func, name
    level = _constant(_arg(node, 4, "level"), int) or 0
    if level == 1:  # the package is flat: relative to claude_session
        name = PACKAGE_NAME + (f".{name}" if name else "")
    elif level != 0:
        raise Dynamic
    return func, name


def _imported(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """The module an import call returns: ``__import__('a.b')`` gives ``a`` (unless it has a
    fromlist), ``import_module('a.b')`` gives ``a.b``."""
    found = _import_call(node, aliases)
    if found is None:
        return None
    func, module = found
    assert isinstance(node, ast.Call)
    if func.endswith("__import__") and _arg(node, 3, "fromlist") is None:
        return module.split(".")[0]
    return module


def _dotted(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """The full name of ``a.b.c`` with ``a`` resolved through the module's imports, or
    through an import call (``import_module('os').system``)."""
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


def _module_base(node: ast.Attribute, aliases: dict[str, str]) -> bool:
    """Whether the base of the chain ``node`` is a name bound by an import or an import call
    (so its attributes are a module's), rather than some other object."""
    base: ast.expr = node
    while isinstance(base, ast.Attribute):
        base = base.value
    if isinstance(base, ast.Name):
        return base.id in aliases
    return _imported(base, aliases) is not None


def _attrs(node: ast.Attribute) -> list[str]:
    """The attribute names of a chain, base excluded: ``f().a.b`` gives ``['a', 'b']``."""
    attrs: list[str] = []
    expr: ast.expr = node
    while isinstance(expr, ast.Attribute):
        attrs.append(expr.attr)
        expr = expr.value
    return attrs[::-1]


def _imports(tree: ast.AST) -> tuple[dict[str, str], list[str]]:
    """Names bound by import statements (to their full names), and import violations."""
    aliases: dict[str, str] = {}
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[a.asname or a.name.split(".")[0]] = (
                    a.name if a.asname else a.name.split(".")[0]
                )
                if _bad(a.name) or any(map(_reexport_root, a.name.split(".")[1:])):
                    found.append(f"import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            module = _from_module(node)
            if _bad(module) or any(map(_reexport_root, module.split(".")[1:])):
                found.append(f"from {module} import")
            for a in node.names:
                if a.name == "*":
                    if _bad_star(module):
                        found.append(f"from {module} import *")
                    continue
                full = f"{module}.{a.name}"
                aliases[a.asname or a.name] = full
                if _bad(full) or _reexport_root(a.name):
                    found.append(f"from {module} import {a.name}")
    return aliases, found


def violations(source: str) -> list[str]:
    tree = ast.parse(source)
    aliases, found = _imports(tree)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def read_as_attribute(node: ast.expr) -> bool:
        parent = parents.get(node)
        return isinstance(parent, ast.Attribute) and parent.value is node

    def called(node: ast.expr) -> bool:
        parent = parents.get(node)
        return isinstance(parent, ast.Call) and parent.func is node

    for node in ast.walk(tree):
        if not isinstance(node, ast.Name | ast.Attribute | ast.Call):
            continue
        try:
            call = _import_call(node, aliases)
            name = None if isinstance(node, ast.Call) else _dotted(node, aliases)
            module = _imported(node, aliases) if call is not None else None
            of_module = isinstance(node, ast.Attribute) and _module_base(node, aliases)
        except Dynamic:
            found.append(f"dynamic import: {ast.unparse(node)}")
            continue
        if isinstance(node, ast.Attribute):
            attrs = _attrs(node) if not read_as_attribute(node) else []
            # Off a module, any name reaching a starter; off another object, only a starter
            # function's full name (``sys.modules['x'].platform.uname``).
            reaches = _bad if of_module else _bad_function
            if node.attr in FORBIDDEN_ATTRS:
                found.append(f"<object>.{node.attr}")
            elif of_module and _reexport_root(node.attr):
                found.append(f"re-export: {ast.unparse(node)}")
            elif any(reaches(".".join(attrs[i:])) for i in range(len(attrs))):
                found.append(f"re-export: {ast.unparse(node)}")
        if call is not None:
            assert module is not None
            if _bad(call[1]):
                found.append(f"{call[0]}({call[1]!r})")
            elif _dangerous(module) and not read_as_attribute(node):
                found.append(f"bare use: {ast.unparse(node)}")
            continue
        if name is None or (isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load)):
            continue
        if isinstance(node, ast.Name) and node.id not in aliases and node.id != "__import__":
            continue  # not bound by an import
        if _bad(name):
            found.append(name)
        elif name in IMPORT_FUNCS:
            if not called(node):
                found.append(f"bare use: {ast.unparse(node)}")
        elif _dangerous(name) and not read_as_attribute(node):
            found.append(f"bare use: {ast.unparse(node)}")
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
        # a dangerous module (or import function) used other than through an attribute
        "import os\nx = os\nx.system('x')",
        "import os\nrun = os.posix_spawn",
        "import os\nx = os\ny = x\ny.fork()",
        "import os\nx = os\nx.getpid()",
        "import os\na, b = os, 1",
        "import os\nif (m := os):\n    pass",
        "import os\nfor m in (os,):\n    m.system('x')",
        "import os\ndef f(m=os):\n    m.system('x')",
        "import os\ndef f():\n    return os",
        "import os\ngetattr(os, 'system')('x')",
        "import os\nvars(os)['system']('x')",
        "import posix as p\nx = [p]",
        "import asyncio\nx = asyncio",
        "import concurrent.futures\nx = concurrent.futures",
        "from concurrent import futures\nx = futures",
        "import importlib\nx = importlib",
        "import builtins\nx = builtins",
        "import platform\nx = platform",
        "from . import gateway\nx = gateway",
        "from . import hookevent\nm = hookevent\nm.gateway",
        "import claude_session\nx = claude_session",
        "import importlib\nf = importlib.import_module",
        "from importlib import import_module\nf = import_module",
        "f = __import__",
        "import importlib\nm = importlib.import_module('os')",
        "x = __import__('asyncio')",
        # import calls naming no literal module
        "import importlib\nimportlib.import_module(name)",
        "__import__(name)",
        "import importlib\nimportlib.import_module('.x', pkg)",
        "__import__('gateway', None, None, ['x'], level)",
        # relative import calls, resolved
        "import importlib\nimportlib.import_module('.subprocess', 'asyncio')",
        "from importlib import import_module as im\nim('.process', package='concurrent.futures')",
        "import importlib\nimportlib.import_module('.gateway', 'claude_session')._prctl()",
        "__import__('gateway', None, None, ['x'], 1)._prctl()",
        # re-exports through other modules
        "import shutil\nshutil.os.system('x')",
        "import posixpath\nposixpath.os",
        "import os.path as p\np.os",
        "from shutil import os",
        "from asyncio.unix_events import subprocess",
        "import asyncio\nasyncio.base_subprocess",
        "import asyncio.base_subprocess",
        "import sys\nsys.modules['x'].platform.uname()",
        "import json\njson.decoder.uuid.uuid1()",
        "import tempfile\ntempfile._os.system('x')",
        "from tempfile import _os",
        # stdlib modules that start processes internally
        "import platform\nplatform.platform()",
        "import platform\nplatform.processor()",
        "import platform\nplatform.architecture()",
        "from platform import uname",
        "import pydoc",
        "import imaplib",
        "import http.server",
        "from http import server",
        "from http import *",
        "from asyncio import base_subprocess",
        "import socketserver",
        "import ensurepip",
        "import venv",
        "import _osx_support",
        "import uuid\nuuid.getnode()",
        "import uuid\nuuid.uuid1()",
        "from uuid import uuid1",
        "loop._make_subprocess_transport(proto, ['/bin/x'], False, None, None, None, 0)",
        # pipes and mailcap run shell commands; uuid and platform hold private starters
        "import pipes",
        "from pipes import Template",
        "import mailcap",
        "from mailcap import findmatch",
        "import shlex\nshlex.pipes",
        "import uuid\nuuid._unix_getnode()",
        "import uuid\nuuid._ifconfig_getnode()",
        "from uuid import _get_command_stdout",
        "import platform\nplatform._syscmd_file('/bin/x', '')",
        "from platform import _syscmd_ver",
        "import json\njson.decoder.uuid._unix_getnode()",
        # relative import calls through __package__ (claude_session)
        "import importlib\nimportlib.import_module('.gateway', __package__)._prctl()",
        "from importlib import import_module\nimport_module('.gateway', package=__package__).os",
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
        "from json import *",
        "import platform\nplatform.system()",
        "import sys\nsys.platform",
        "import http.client",
        "from . import hookevent\nhookevent.gateway_ok",
        "import os\nos.environ.get('PATH')",
        "import os\ndef f(path=os.path):\n    return path",
        "from os import path\nx = path",
        "import uuid\nuuid.uuid4()",
        "from uuid import uuid4",
        "from importlib.metadata import version",
        "import importlib\nimportlib.import_module('.clock', 'claude_session').monotonic",
        "self.gateway.run(['/bin/x'])",
        "from . import clock\nclock.SystemClock()",
        # attribute names that only match a module name, on objects that are not modules
        "args.pty",
        "session.pty.fd",
        "self._os",
        "cfg.venv",
        "opts.concurrent",
        "def f(args):\n    return args.subprocess",
        "import importlib\nimportlib.import_module('.clock', __package__).SystemClock()",
        "from importlib import import_module\nimport_module('.clock', package=__package__).x",
        "import uuid\nuuid.__name__",
    ],
)
def test_checker_allows(source: str) -> None:
    assert violations(source) == []
