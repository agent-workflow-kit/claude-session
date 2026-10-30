"""The injected-hook path must stay light (design.md, Hook-event fast path)."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

# Modules hook-event must never import, whether or not they are installed.
FORBIDDEN = ("pydantic", "yaml", "argparse", "claude_session.cli", "claude_session.config")
# claude_session modules hook-event may load (design.md: stdlib, store, outbox's ingestion
# side, gateway; plus the stdlib-only errors, logs and fsutil helpers).
ALLOWED_PACKAGE_MODULES = {
    "claude_session",
    "claude_session.__main__",
    "claude_session.hookevent",
    "claude_session.store",
    "claude_session.outbox",
    "claude_session.gateway",
    "claude_session.errors",
    "claude_session.logs",
    "claude_session.fsutil",
}

# Runs the module the way the injected hooks do (`python -I -m claude_session hook-event ...`).
# A meta path finder records every import attempt of a forbidden module (it is consulted before
# the module is looked up, so an attempt is seen even when the module is not installed).
PROBE = """
import json, runpy, sys

FORBIDDEN = {forbidden!r}
attempted = []

class Recorder:
    def find_spec(self, name, path=None, target=None):
        if any(name == f or name.startswith(f + ".") for f in FORBIDDEN):
            attempted.append(name)
        return None

already = [m for m in FORBIDDEN if m in sys.modules]
sys.meta_path.insert(0, Recorder())
{extra}
sys.argv = ["claude_session", "hook-event", "--state-dir", "/nonexistent", "SessionStart"]
try:
    runpy.run_module("claude_session", run_name="__main__", alter_sys=True)
    code = None
except SystemExit as exc:
    code = exc.code
package = sorted(m for m in sys.modules if m == "claude_session" or m.startswith("claude_session."))
print(json.dumps({{"code": code, "already": already, "attempted": attempted, "package": package}}))
"""


def run_probe(extra: str = "") -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", PROBE.format(forbidden=FORBIDDEN, extra=extra)],
        input="{}",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def violations(result: dict[str, Any]) -> list[str]:
    found = [f"imported before probe: {m}" for m in result["already"]]
    found += [f"import attempted: {m}" for m in result["attempted"]]
    found += [
        f"unexpected module: {m}" for m in result["package"] if m not in ALLOWED_PACKAGE_MODULES
    ]
    return found


def test_hook_event_imports_nothing_heavy() -> None:
    result = run_probe()
    assert result["code"] == 0
    assert violations(result) == []


def test_checker_flags_a_forbidden_import_that_is_not_installed() -> None:
    result = run_probe("try:\n    import pydantic\nexcept ImportError:\n    pass")
    assert "import attempted: pydantic" in violations(result)


def test_checker_flags_cli() -> None:
    result = run_probe("import claude_session.cli")
    found = violations(result)
    assert "import attempted: claude_session.cli" in found
    assert "import attempted: argparse" in found
    assert "unexpected module: claude_session.cli" in found
