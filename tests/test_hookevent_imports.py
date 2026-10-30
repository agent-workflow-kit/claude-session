"""The injected-hook path must not pay for config loading (design.md, Hook-event fast path)."""

from __future__ import annotations

import json
import subprocess
import sys

# Runs the module exactly as the injected hooks do (`python -I -m claude_session hook-event ...`)
# and reports which heavy modules ended up imported.
PROBE = """
import json, runpy, sys
sys.argv = ["claude_session", "hook-event", "--state-dir", "/nonexistent", "SessionStart"]
try:
    runpy.run_module("claude_session", run_name="__main__", alter_sys=True)
except SystemExit as exc:
    code = exc.code
else:
    code = None
heavy = ("claude_session.config", "pydantic", "yaml")
print(json.dumps({"code": code, "loaded": [m for m in heavy if m in sys.modules]}))
"""


def test_hook_event_does_not_import_config_or_pydantic() -> None:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", PROBE],
        input="{}",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result == {"code": 0, "loaded": []}
