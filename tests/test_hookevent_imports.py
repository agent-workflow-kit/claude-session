"""The injected-hook path must stay light (design.md, Hook-event fast path)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from claude_session import store

# Modules hook-event must never import, whether or not they are installed.
FORBIDDEN = ("pydantic", "yaml", "argparse", "claude_session.cli", "claude_session.config")
# claude_session modules hook-event may load (design.md: stdlib, store, outbox's ingestion
# side and its secretindex, gateway; plus the stdlib-only errors, logs and fsutil helpers).
ALLOWED_PACKAGE_MODULES = {
    "claude_session",
    "claude_session.__main__",
    "claude_session.hookevent",
    "claude_session.store",
    "claude_session.outbox",
    "claude_session.secretindex",
    "claude_session.gateway",
    "claude_session.errors",
    "claude_session.logs",
    "claude_session.fsutil",
}

# Runs the module the way the injected hooks do (`python -I -m claude_session hook-event ...`).
# A meta path finder records every import attempt of a forbidden module (it is consulted before
# the module is looked up, so an attempt is seen even when the module is not installed).
PROBE = """
import json, os, runpy, sys

# A failing hook-event leaves fds 1 and 2 at /dev/null: report through a saved copy.
result_fd = os.dup(1)

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
try:
{runner}
except SystemExit as exc:
    code = exc.code
package = sorted(m for m in sys.modules if m == "claude_session" or m.startswith("claude_session."))
report = {{"code": code, "already": already, "attempted": attempted, "package": package}}
os.write(result_fd, json.dumps(report).encode() + b"\\n")
"""


HOOK_ARGV = ["hook-event", "--state-dir", "/nonexistent", "SessionStart"]
# `python -I -m claude_session hook-event ...`, as the injected hooks run it.
RUN_MODULE_FOR = """
    sys.argv = ["claude_session", *{argv!r}]
    runpy.run_module("claude_session", run_name="__main__", alter_sys=True)
    code = None
"""
RUN_MODULE = RUN_MODULE_FOR.format(argv=HOOK_ARGV)
# The `claude-session` console script: it imports the entry function and calls it.
CONSOLE_SCRIPT = f"""
    from claude_session.__main__ import main
    code = main({HOOK_ARGV!r})
"""


def run_probe(
    extra: str = "",
    runner: str = RUN_MODULE,
    *,
    stdin: str = "{}",
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    script = PROBE.format(forbidden=FORBIDDEN, extra=extra, runner=runner)
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script],
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    # The last line is the probe's report; a processed hook's own output comes before it.
    return json.loads(proc.stdout.splitlines()[-1])


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


def test_a_processed_hook_event_imports_nothing_heavy(tmp_path: Path) -> None:
    """The success path, against a real state.db: SessionStart opens the store, applies the
    table and reads hookconf.json."""
    from .test_hookevent import CLAUDE_ID, SID, Env, common

    root = tmp_path / "state"
    store.Store.open(str(root / "state.db")).close()
    Env(root).add()
    argv = ["hook-event", "--state-dir", str(root), "SessionStart"]
    result = run_probe(
        runner=RUN_MODULE_FOR.format(argv=argv),
        stdin=json.dumps(common("SessionStart", source="startup")),
        env={**os.environ, "CSESSION_ID": SID},
    )
    assert result["code"] == 0
    assert violations(result) == []
    assert "claude_session.logs" not in result["package"]  # nothing failed
    with sqlite3.connect(root / "state.db") as conn:
        assert conn.execute("SELECT claude_session_id FROM claude_ids").fetchall() == [(CLAUDE_ID,)]


def test_a_draining_hook_event_imports_nothing_heavy(tmp_path: Path) -> None:
    """PostToolUse with a request in the outbox: claim, pre-check, record and log."""
    from .outbox_env import Run

    root = tmp_path / "state"
    store.Store.open(str(root / "state.db")).close()
    run = Run(root).setup()
    run.write_artifact("a.md", b"a")
    run.report(artifact="a.md")
    run.plant(run.request(secret="0" * 64))  # a rejection: the log is written too
    argv = ["hook-event", "--state-dir", str(root), "PostToolUse"]
    result = run_probe(
        runner=RUN_MODULE_FOR.format(argv=argv),
        stdin="{}",
        env={**os.environ, "CSESSION_ID": run.environ()["CSESSION_ID"]},
    )
    assert result["code"] == 0
    assert violations(result) == []
    assert "claude_session.outbox" in result["package"]
    assert sorted(r["result"] for r in run.requests()) == ["bad_secret", "ingested"]


def test_console_script_hook_event_imports_nothing_heavy() -> None:
    result = run_probe(runner=CONSOLE_SCRIPT)
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


# `cli` validates flags and dispatches; config loading (pydantic, yaml) happens later, on demand.
CLI_IMPORT_PROBE = """
import json, sys
{extra}
import claude_session.cli
heavy = ("pydantic", "yaml", "claude_session.config")
print(json.dumps(sorted(m for m in sys.modules if any(m == h or m.startswith(h) for h in heavy))))
"""


def test_cli_import_loads_no_config_stack() -> None:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", CLI_IMPORT_PROBE.format(extra="")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == []


def test_failing_hook_event_logs_without_heavy_imports() -> None:
    failing = (
        "import claude_session.hookevent as h\n"
        "def broken(argv):\n"
        "    raise RuntimeError('x')\n"
        "h.main = broken\n"
        "import claude_session.__main__ as entry\n"
        "entry._silence_output = entry._restore_output  # keep the probe's own output"
    )
    result = run_probe(failing, runner=CONSOLE_SCRIPT)
    assert result["code"] == 0
    assert "claude_session.logs" in result["package"]
    assert violations(result) == []


def test_entry_module_does_not_import_re() -> None:
    """``__main__`` runs for every hook; ``re`` is loaded only when a failure is logged."""
    script = (
        "import sys\n"  # not json: it imports re
        "preloaded = 're' in sys.modules\n"
        "import claude_session.__main__\n"
        "print(int(preloaded), int('re' in sys.modules))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    preloaded, loaded = proc.stdout.split()
    if preloaded == "1":
        pytest.skip("the interpreter loads re at startup")
    assert loaded == "0"
