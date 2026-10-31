"""``exec --mode headless`` end to end: the ``claude-session`` entry point launches the fake
``claude`` (``tests/fakebin``), which plays a scenario through the generated settings: the
injected hooks (``hook-event``), ``claude-session report`` and ``stream-json`` output.

Every process is started through ``subprocess.Popen`` in this test process (the
``claude-session`` executable) or by it (the fake, with ``PR_SET_PDEATHSIG``), so the
``no_leftover_processes`` fixture ends any that a failing test leaves behind.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from claude_session import pathrules

from .conftest import REAL_TMP_ROOT
from .fakeclaude import claude_bin, read_jsonl, write_json
from .test_schemas import registry

CLAUDE_SESSION = Path(sys.executable).parent / "claude-session"
EXEC_SCHEMA = "claude-session/cli/exec.json"
SECRET_VALUE = "vault-token-value-0123456789"
# The ``claude-session`` entry point with this test's temporary-directory root
# (``conftest.private_tmp_root``): a subprocess does not inherit the in-process patch.
# ``sys.argv[0]`` stays the real executable, whose directory exec puts on the agent's PATH.
ENTRY = """\
import sys
from claude_session import pathrules
pathrules.TMP_ROOT = {tmp_root!r}
sys.argv[0] = {exe!r}
from claude_session.__main__ import main
sys.exit(main())
"""


def claude_session_cmd() -> list[str]:
    """The command that runs ``claude-session`` as the executable does, under the current
    (test-private) ``pathrules.TMP_ROOT``."""
    entry = ENTRY.format(tmp_root=pathrules.TMP_ROOT, exe=str(CLAUDE_SESSION))
    return [sys.executable, "-c", entry]


@dataclass
class Run:
    proc: subprocess.CompletedProcess[str]
    outcome: dict[str, Any]
    state: Path
    log: Path

    @property
    def code(self) -> int:
        return self.proc.returncode

    def rows(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        db = sqlite3.connect(f"file:{self.state / 'state.db'}?mode=ro", uri=True)
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def run_row(self) -> dict[str, Any]:
        db = sqlite3.connect(f"file:{self.state / 'state.db'}?mode=ro", uri=True)
        try:
            cursor = db.execute("SELECT * FROM runs WHERE csession_id = ?", (self.sid,))
            names = [c[0] for c in cursor.description]
            return dict(zip(names, cursor.fetchone(), strict=True))
        finally:
            db.close()

    @property
    def sid(self) -> str:
        return self.outcome["csession_id"]

    @property
    def private_run(self) -> Path:
        return self.state / "private" / self.sid / "runs" / "1"

    def fake_log(self) -> list[dict[str, Any]]:
        return read_jsonl(self.log)


def validate_exec(outcome: Mapping[str, Any]) -> None:
    schema = registry().contents(EXEC_SCHEMA)
    validator = Draft202012Validator(schema, registry=registry(), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(outcome), key=str)
    assert not errors, [e.message for e in errors]


@pytest.fixture
def state(_private_state_dir: Path) -> Path:
    return _private_state_dir / "claude-session"


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def private_home(tmp_path: Path) -> dict[str, str]:
    """A private ``HOME`` and ``XDG_CONFIG_HOME``: no git or other program the run starts
    reads the user's configuration."""
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True, exist_ok=True)
    return {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}


def run_exec(
    tmp_path: Path,
    state: Path,
    cwd: Path,
    steps: list[dict[str, Any]],
    *,
    args: Sequence[str] = (),
    config: str = "",
    prompt: str | None = "Do the task.\n",
    env: Mapping[str, str] | None = None,
    scenario_extra: Mapping[str, Any] | None = None,
    timeout: float = 90,
    claude: Path | None = None,
) -> Run:
    log = tmp_path / "fake.jsonl"
    scenario = write_json(tmp_path / "scenario.json", {"steps": steps, **(scenario_extra or {})})
    fake = claude or claude_bin(tmp_path / "fakebin", scenario, log)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"claude_bin: {fake}\n{config}", encoding="utf-8")
    argv = [*claude_session_cmd(), "exec", "--mode", "headless", "--json", "--config", str(cfg)]
    if prompt is not None:
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        argv += ["--prompt-file", str(prompt_file)]
    proc = subprocess.run(
        [*argv, *args],
        cwd=cwd,
        env={**os.environ, **private_home(tmp_path), **(env or {})},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert proc.stdout, proc.stderr
    outcome = json.loads(proc.stdout)
    validate_exec(outcome)
    assert outcome["exit_code"] == proc.returncode
    return Run(proc, outcome, state, log)


START = {"do": "session_start"}
STOP = {"do": "stop"}


def result_step(text: str = "the result text", **fields: Any) -> dict[str, Any]:
    return {"do": "result", "result": text, **fields}


def test_a_headless_run_that_reports_done_exits_0_with_a_frozen_artifact(
    tmp_path: Path, state: Path, work: Path
) -> None:
    run = run_exec(
        tmp_path,
        state,
        work,
        [
            START,
            {"do": "bash", "command": 'printf "artifact body" > "$CSESSION_DIR/artifacts/out.txt"'},
            {"do": "report", "args": ["done", "--summary", "all good", "--artifact", "out.txt"]},
            STOP,
            result_step(total_cost_usd=0.5, num_turns=3),
        ],
    )
    assert run.code == 0, run.proc.stderr
    outcome = run.outcome
    assert (outcome["outcome"], outcome["end_reason"], outcome["state"]) == (
        "done",
        "normal",
        "exited",
    )
    report = outcome["report"]
    assert report["status"] == "done" and report["implicit"] is False
    assert report["summary"] == "all good"
    frozen = Path(report["artifact"]["path"])
    assert frozen.read_bytes() == b"artifact body"
    assert frozen.is_relative_to(run.private_run / "artifact")
    assert outcome["cost_usd"] == 0.5
    assert outcome["claude_session_id"] is not None
    assert outcome["session_started_at"] is not None
    assert outcome["phase"] == "session"
    # The injected hooks ran: the run went starting -> working, and its report was ingested.
    causes = [r[0] for r in run.rows("SELECT cause FROM events ORDER BY id")]
    assert causes[0] == "launch" and "SessionStart" in causes and "report" in causes
    assert causes[-1] == "exit"
    assert run.rows("SELECT final FROM events ORDER BY id DESC LIMIT 1") == [(1,)]
    row = run.run_row()
    assert row["child_pid"] is not None and row["child_start"]
    assert row["wrapper_pid"] is not None and row["wrapper_start"]
    assert row["num_turns"] == 3
    assert not (run.private_run / "secret-index").exists()  # secret-equivalent: deleted
    for name in ("result.json", "stderr.log", "settings.json", "hookconf.json"):
        assert stat.S_IMODE((run.private_run / name).stat().st_mode) == 0o600


FAST = "poll_interval: 0.05s\n"


def test_an_unreported_clean_run_is_an_implicit_done(
    tmp_path: Path, state: Path, work: Path
) -> None:
    run = run_exec(
        tmp_path, state, work, [START, STOP, result_step("# Result\n\nfine\n")], config=FAST
    )
    assert run.code == 0, run.proc.stderr
    report = run.outcome["report"]
    assert (run.outcome["outcome"], run.outcome["end_reason"]) == ("done", "normal")
    assert report["implicit"] is True and report["summary"] is None
    assert report["artifact"]["name"] == "result.md"
    assert Path(report["artifact"]["path"]).read_text() == "# Result\n\nfine\n"


NO_IMPLICIT = FAST + "kinds:\n  implement:\n    implicit_done: false\n"


def test_an_unreported_clean_run_without_implicit_done_is_exited_unreported(
    tmp_path: Path, state: Path, work: Path
) -> None:
    run = run_exec(tmp_path, state, work, [START, STOP, result_step()], config=NO_IMPLICIT)
    assert run.code == 14
    assert (run.outcome["outcome"], run.outcome["end_reason"]) == ("exited-unreported", "no_report")
    assert run.outcome["report"] is None and run.outcome["state"] == "exited-unreported"


def test_the_backstop_blocks_the_first_unreported_stop_once(
    tmp_path: Path, state: Path, work: Path
) -> None:
    report = {"do": "report", "args": ["blocked", "--reason-file", "-"], "heredoc": "need help"}
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, {"do": "stop", "on_block": [report, STOP]}, result_step()],
        config=FAST,
    )
    assert run.code == 13
    assert (run.outcome["outcome"], run.outcome["report"]["reason"]) == ("blocked", "need help\n")
    blocks = [r for r in run.fake_log() if r["event"] == "block"]
    assert len(blocks) == 1


def test_a_kind_without_the_backstop_is_never_blocked(
    tmp_path: Path, state: Path, work: Path
) -> None:
    config = FAST + "kinds:\n  implement:\n    backstop: false\n"
    run = run_exec(tmp_path, state, work, [START, STOP, result_step()], config=config)
    assert [r for r in run.fake_log() if r["event"] == "block"] == []
    assert run.outcome["report"]["implicit"] is True


def test_a_run_past_max_turns_exits_16(tmp_path: Path, state: Path, work: Path) -> None:
    run = run_exec(
        tmp_path,
        state,
        work,
        [
            START,
            STOP,
            result_step(subtype="error_max_turns", is_error=True),
            {"do": "exit", "code": 1},
        ],
        args=["--max-turns", "2"],
        config=FAST,
    )
    assert run.code == 16
    assert (run.outcome["outcome"], run.outcome["end_reason"]) == ("timeout", "max_turns")
    start = next(r for r in run.fake_log() if r["event"] == "start")
    argv = start["argv"]
    assert argv[argv.index("--max-turns") + 1] == "3"  # one more than asked (design.md)


@pytest.mark.parametrize(
    ("steps", "code", "outcome", "end_reason"),
    [
        ([START, result_step(is_error=True)], 14, "exited-unreported", "api_error"),
        ([START, result_step(), {"do": "exit", "code": 3}], 14, "exited-unreported", "exit_3"),
        ([START, {"do": "truncated_result", "chars": 20}], 14, "exited-unreported", "no_result"),
        ([START], 14, "exited-unreported", "no_result"),
        ([], 14, "exited-unreported", "no_session_start"),
        ([START, {"do": "hang"}], 15, "killed", "signal_term"),
    ],
)
def test_headless_rows_end_to_end(
    tmp_path: Path,
    state: Path,
    work: Path,
    steps: list[dict[str, Any]],
    code: int,
    outcome: str,
    end_reason: str,
) -> None:
    extra = {}
    if steps and steps[-1] == {"do": "hang"}:
        # The fake signals itself (a signal exec did not send): TERM exits 143.
        steps = steps[:-1]
        extra = {"raise_after_steps": "TERM"}
    run = run_exec(tmp_path, state, work, steps, config=FAST, scenario_extra=extra)
    assert run.code == code, run.proc.stderr
    assert (run.outcome["outcome"], run.outcome["end_reason"]) == (outcome, end_reason)


def test_cost_and_turns_are_summed_over_every_result_object(
    tmp_path: Path, state: Path, work: Path
) -> None:
    run = run_exec(
        tmp_path,
        state,
        work,
        [
            START,
            result_step("one", total_cost_usd=0.25, num_turns=2),
            {"do": "emit", "line": {"type": "assistant"}},
            result_step("two", total_cost_usd=0.5, num_turns=3),
        ],
        config=FAST,
    )
    assert run.outcome["cost_usd"] == pytest.approx(0.75)
    assert run.run_row()["num_turns"] == 5
    assert Path(run.outcome["report"]["artifact"]["path"]).read_text() == "two"


SECRET_CONFIG = FAST + "secret_env: [VAULT_TOKEN]\n"


def test_an_implicit_result_holding_a_secret_is_rejected(
    tmp_path: Path, state: Path, work: Path
) -> None:
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step(f"the token is {SECRET_VALUE}!")],
        config=SECRET_CONFIG,
        env={"VAULT_TOKEN": SECRET_VALUE},
    )
    assert run.code == 14
    assert (run.outcome["outcome"], run.outcome["end_reason"]) == (
        "exited-unreported",
        "result_rejected",
    )
    assert not list((run.private_run).glob("artifact/*/result.md"))


def test_an_implicit_result_over_the_artifact_cap_is_rejected(
    tmp_path: Path, state: Path, work: Path
) -> None:
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step("x" * 101)],
        config=FAST + "artifact_max_bytes: 100\n",
    )
    assert run.outcome["end_reason"] == "result_rejected"


def test_a_report_holding_a_secret_is_rejected_and_the_agent_refiles(
    tmp_path: Path, state: Path, work: Path
) -> None:
    bad = {"do": "report", "args": ["done", "--summary-file", "-"], "heredoc": SECRET_VALUE}
    good = {"do": "report", "args": ["done", "--summary", "clean"]}
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, bad, good, STOP, result_step()],
        config=SECRET_CONFIG,
        env={"VAULT_TOKEN": SECRET_VALUE},
    )
    assert run.code == 0
    assert run.outcome["report"]["summary"] == "clean"
    results = sorted(r[0] for r in run.rows("SELECT result FROM requests"))
    assert results == ["contains_secret", "ingested"]
    log = (state / "log.jsonl").read_text()
    assert SECRET_VALUE not in log
    assert "contains_secret" in log


def test_claude_not_found_ends_the_run_not_started(tmp_path: Path, state: Path, work: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"claude_bin: {tmp_path / 'missing' / 'claude'}\n", encoding="utf-8")
    (tmp_path / "p.md").write_text("x")
    proc = subprocess.run(
        [
            *claude_session_cmd(),
            "exec",
            "--mode",
            "headless",
            "--json",
            "--config",
            str(cfg),
            "--prompt-file",
            str(tmp_path / "p.md"),
        ],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 12, proc.stderr
    outcome = json.loads(proc.stdout)
    validate_exec(outcome)
    assert (outcome["phase"], outcome["outcome"], outcome["end_reason"]) == (
        "launch",
        "not-started",
        "claude_not_found",
    )
    assert outcome["state"] == "not-started" and outcome["cost_usd"] is None
    assert outcome["message"] and "claude_bin" in outcome["message"]


def test_an_unsafe_session_directory_ends_the_run_session_dir_unsafe(
    tmp_path: Path, state: Path, work: Path
) -> None:
    state.mkdir(parents=True, mode=0o700)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (state / "sessions").symlink_to(elsewhere)
    run = run_exec(tmp_path, state, work, [START, result_step()], config=FAST)
    assert run.code == 12
    assert (run.outcome["outcome"], run.outcome["end_reason"]) == (
        "not-started",
        "session_dir_unsafe",
    )
    assert list(elsewhere.iterdir()) == []
    assert not run.log.exists()  # claude never started


def test_an_isolated_run_with_a_lowered_sandbox_is_audited(
    tmp_path: Path, state: Path, work: Path
) -> None:
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        args=["--reads-third-party", "--sandbox", "off"],
        config=FAST,
    )
    assert run.code == 0, run.proc.stderr
    assert any("sandbox_lowered" in w for w in run.outcome["warnings"])
    records = read_jsonl(state / "audit.jsonl")
    assert [r["action"] for r in records] == ["sandbox_lowered"]
    assert records[0]["inputs"]["sandbox_to"] == "off"
    assert records[0]["target"] == run.sid
    settings = json.loads((run.private_run / "settings.json").read_text())
    assert any(rule.startswith("Edit(") for rule in settings["permissions"]["deny"])
    assert run.rows("SELECT tainted FROM sessions") == [(1,)]


def test_a_preferred_sandbox_without_prerequisites_warns_and_cleans_its_tmpdir(
    tmp_path: Path, state: Path, work: Path
) -> None:
    run = run_exec(
        tmp_path,
        state,
        work,
        [
            START,
            {"do": "bash", "command": 'printf "%s" "$TMPDIR" > tmpdir.txt'},
            STOP,
            result_step(),
        ],
        args=["--sandbox", "preferred"],
        config=FAST,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert run.code == 0, run.proc.stderr
    if any("unavailable" in w for w in run.outcome["warnings"]):
        assert run.run_row()["sandbox_active"] == 0
    tmp = Path((work / "tmpdir.txt").read_text())
    assert tmp.name == run.sid and tmp.parent.name == f"csession-{os.geteuid()}"
    assert not tmp.exists()  # removed at run end


def real_tmp_root_entries() -> set[str] | None:
    """The entries of the real ``/tmp/csession-<uid>`` (None: it does not exist)."""
    root = os.path.join(REAL_TMP_ROOT, pathrules.tmp_root_name(os.geteuid()))
    try:
        return set(os.listdir(root))
    except FileNotFoundError:
        return None


def test_the_suite_keeps_runs_out_of_the_real_tmp_root(
    tmp_path: Path, state: Path, work: Path, private_tmp_root: Path
) -> None:
    """A run's private tmpdir is made under the test's own root, never the user's real
    ``/tmp/csession-<uid>`` (whose location is fixed: ``/tmp``)."""
    assert REAL_TMP_ROOT == "/tmp"
    before = real_tmp_root_entries()
    run = run_exec(
        tmp_path,
        state,
        work,
        [
            START,
            {"do": "bash", "command": 'printf "%s" "$TMPDIR" > tmpdir.txt'},
            {"do": "report", "args": ["done", "--summary", "ok"]},
            STOP,
            result_step(),
        ],
        args=["--sandbox", "preferred"],
        config=FAST,
    )
    assert run.code == 0, run.proc.stderr
    tmp = Path((work / "tmpdir.txt").read_text())
    root = private_tmp_root.resolve() / pathrules.tmp_root_name(os.geteuid())
    assert tmp == root / run.sid
    assert root.is_dir() and not tmp.exists()
    assert real_tmp_root_entries() == before


def test_a_required_sandbox_without_prerequisites_is_not_started(
    tmp_path: Path, state: Path, work: Path
) -> None:
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, result_step()],
        args=["--reads-third-party"],
        config=FAST,
        env={"PATH": "/usr/bin:/bin"},
    )
    if run.code == 0:
        pytest.skip("bubblewrap and socat are installed here")
    assert (run.outcome["outcome"], run.outcome["end_reason"]) == (
        "not-started",
        "sandbox_unavailable",
    )
    assert run.outcome["message"]  # the install hint


def test_sigint_during_supervision_is_an_internal_error_that_ends_the_child(
    tmp_path: Path, state: Path, work: Path
) -> None:
    marker = tmp_path / "hanging"
    log = tmp_path / "fake.jsonl"
    scenario = write_json(
        tmp_path / "scenario.json",
        {"steps": [START, {"do": "touch", "path": str(marker)}, {"do": "hang"}]},
    )
    fake = claude_bin(tmp_path / "fakebin", scenario, log)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"claude_bin: {fake}\n{FAST}kill_grace: 2s\n", encoding="utf-8")
    (tmp_path / "p.md").write_text("x")
    proc = subprocess.Popen(
        [
            *claude_session_cmd(),
            "exec",
            "--mode",
            "headless",
            "--json",
            "--config",
            str(cfg),
            "--prompt-file",
            str(tmp_path / "p.md"),
        ],
        cwd=work,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = 30.0
        import time

        start = time.monotonic()
        while not marker.exists():
            assert proc.poll() is None, proc.communicate()
            assert time.monotonic() - start < deadline
            time.sleep(0.02)
        proc.send_signal(2)  # SIGINT to the exact process started here
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    # exec dies by SIGINT after the run is finalized, without a traceback (TODO(#17): SIGINT
    # is then forwarded as forwarded_int instead of being an internal error).
    assert proc.returncode == -2, err
    assert "Traceback" not in err
    db = sqlite3.connect(f"file:{state / 'state.db'}?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT pending_cause, outcome, end_reason, exit_code, child_pid FROM runs"
        ).fetchone()
    finally:
        db.close()
    assert row[:4] == ("internal_error", "killed", "internal_error", 15)
    child = row[4]
    assert not os.path.exists(f"/proc/{child}") or "Z" in Path(f"/proc/{child}/stat").read_text()


def test_a_rejected_report_is_refiled_from_the_hooks_block(
    tmp_path: Path, state: Path, work: Path
) -> None:
    good = {"do": "report", "args": ["done", "--summary", "refiled"]}
    bad = {
        "do": "report",
        "args": ["done", "--summary-file", "-"],
        "heredoc": f"leaked {SECRET_VALUE}",
        "on_block": [good],
    }
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, bad, STOP, result_step()],
        config=SECRET_CONFIG,
        env={"VAULT_TOKEN": SECRET_VALUE},
    )
    assert run.code == 0, run.proc.stderr
    assert run.outcome["report"]["summary"] == "refiled"
    blocks = [r for r in run.fake_log() if r["event"] == "block"]
    assert len(blocks) == 1 and "contains_secret" in json.dumps(blocks[0])


def test_the_backstop_blocks_after_a_rejected_report(
    tmp_path: Path, state: Path, work: Path
) -> None:
    bad = {"do": "report", "args": ["done", "--summary-file", "-"], "heredoc": SECRET_VALUE}
    good = {"do": "report", "args": ["failed", "--reason-file", "-"], "heredoc": "gave up"}
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, bad, {"do": "stop", "on_block": [good, STOP]}, result_step()],
        config=SECRET_CONFIG,
        env={"VAULT_TOKEN": SECRET_VALUE},
    )
    assert run.code == 18, run.proc.stderr
    assert run.outcome["outcome"] == "failed"
    assert run.run_row()["backstop_used"] == 1


def test_the_injected_hooks_drive_the_state_through_the_transition_table(
    tmp_path: Path, state: Path, work: Path
) -> None:
    run = run_exec(
        tmp_path,
        state,
        work,
        [
            START,
            {"do": "user_prompt_submit"},
            {"do": "permission_request", "tool_name": "Bash", "tool_input": {"command": "ls"}},
            {"do": "post_tool_use", "tool_name": "Bash", "tool_input": {"command": "ls"}},
            {
                "do": "report",
                "args": ["needs-input", "--reason-file", "-"],
                "heredoc": "which branch?",
            },
            STOP,
            {"do": "session_end", "reason": "other"},
            result_step(),
        ],
        config=FAST,
    )
    assert run.code == 13
    changes = run.rows("SELECT cause, to_state, needs_input_reason FROM events ORDER BY id")
    assert changes[0] == ("launch", "starting", None)
    assert ("SessionStart", "working", None) in changes
    assert ("PermissionRequest", "needs-input", "permission") in changes
    assert ("report", "needs-input", "escalation") in changes
    assert changes[-1][:2] == ("exit", "exited")
    assert run.run_row()["session_end_reason"] == "other"


ISOLATED = ["--reads-third-party", "--sandbox", "off"]


def test_an_isolated_run_in_a_hidden_home_directory_is_unsafe_cwd(
    tmp_path: Path, state: Path
) -> None:
    home = tmp_path / "home"
    cwd = home / ".config" / "proj"
    cwd.mkdir(parents=True)
    run = run_exec(
        tmp_path,
        state,
        cwd,
        [START, result_step()],
        args=ISOLATED,
        config=FAST,
        env={"HOME": str(home)},
    )
    assert run.code == 12
    assert (run.outcome["outcome"], run.outcome["end_reason"]) == ("not-started", "unsafe_cwd")
    assert not run.log.exists()


def test_an_isolated_run_with_a_path_entry_in_its_cwd_is_unsafe_path(
    tmp_path: Path, state: Path, work: Path
) -> None:
    (work / "bin").mkdir()
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, result_step()],
        args=ISOLATED,
        config=FAST,
        env={"PATH": f"{work / 'bin'}:/usr/bin:/bin"},
    )
    assert run.code == 12
    assert (run.outcome["outcome"], run.outcome["end_reason"]) == ("not-started", "unsafe_path")


def test_a_missing_secret_index_fails_the_drains_visibly(
    tmp_path: Path, state: Path, work: Path
) -> None:
    index = '"$CSESSION_DIR/../../private/$CSESSION_ID/runs/1/secret-index"'
    run = run_exec(
        tmp_path,
        state,
        work,
        [
            START,
            {"do": "bash", "command": f"rm {index}"},
            {"do": "report", "args": ["done", "--summary", "never ingested"]},
            STOP,
            result_step(),
        ],
        config=FAST + "kinds:\n  implement:\n    implicit_done: false\n",
    )
    # Nothing is ingested without the scan: no report, and the caller is told.
    assert run.outcome["report"] is None
    assert run.outcome["end_reason"] == "no_report"
    assert any("not ingested" in w for w in run.outcome["warnings"])
    assert list((run.private_run / "claimed").iterdir())  # the request stays claimed


def test_no_implicit_done_while_a_report_waits_unprocessed(
    tmp_path: Path, state: Path, work: Path
) -> None:
    index = '"$CSESSION_DIR/../../private/$CSESSION_ID/runs/1/secret-index"'
    run = run_exec(
        tmp_path,
        state,
        work,
        [
            START,
            {"do": "bash", "command": f"rm {index}"},
            {"do": "report", "args": ["failed", "--reason-file", "-"], "heredoc": "broke"},
            STOP,
            result_step("looks fine"),
        ],
        config=FAST,
    )
    assert (run.outcome["outcome"], run.outcome["end_reason"]) == (
        "exited-unreported",
        "result_rejected",
    )
    assert run.code == 14


# Launch-phase ends (review round 1)


def csession_tmpdir(sid: str) -> Path:
    return Path(f"/tmp/csession-{os.geteuid()}") / sid


def script(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def launch_records(state: Path) -> list[dict[str, Any]]:
    return [
        r for r in read_jsonl(state / "log.jsonl") if r["event"] in ("launch", "launch_interrupted")
    ]


@pytest.mark.parametrize("isolated", [False, True])
def test_a_claude_that_cannot_be_started_ends_the_run_not_started(
    tmp_path: Path, state: Path, work: Path, isolated: bool
) -> None:
    claude = script(tmp_path / "badbin" / "claude", "#!/nonexistent/interp\n")
    run = run_exec(
        tmp_path,
        state,
        work,
        [],
        args=["--reads-third-party", "--sandbox", "preferred"] if isolated else [],
        config=FAST,
        env={"PATH": "/usr/bin:/bin"},
        claude=claude,
    )
    assert run.code == 12, run.proc.stderr
    assert (run.outcome["phase"], run.outcome["outcome"], run.outcome["end_reason"]) == (
        "launch",
        "not-started",
        "launch_interrupted",
    )
    assert "claude could not be started" in run.outcome["message"]
    row = run.run_row()
    assert row["ended_at"] is not None and row["child_pid"] is None
    assert run.outcome["state"] == "not-started"
    assert not (run.private_run / "secret-index").exists()
    assert not csession_tmpdir(run.sid).exists()
    assert [r["reason"] for r in launch_records(state)] == ["launch_interrupted"]


def test_a_git_inside_a_write_dir_is_never_run(tmp_path: Path, state: Path, work: Path) -> None:
    opt = tmp_path / "opt"
    marker = tmp_path / "git-ran"
    script(opt / "bin" / "git", f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, result_step()],
        args=[*ISOLATED, "--repo", "example.com/group/project"],
        config=FAST + f"kinds:\n  implement:\n    write_dirs: [{opt}]\n",
        env={"PATH": f"{opt / 'bin'}:/usr/bin:/bin"},
    )
    assert not marker.exists()
    assert (run.outcome["outcome"], run.outcome["end_reason"]) == ("not-started", "unsafe_path")


@pytest.mark.parametrize("where", ["write_dirs", "allowWrite"])
def test_a_profile_file_the_run_can_write_is_refused(
    tmp_path: Path, state: Path, work: Path, where: str
) -> None:
    opt = tmp_path / "opt"
    opt.mkdir()
    profile = opt / "profile.json"
    config = FAST
    if where == "write_dirs":
        profile.write_text("{}", encoding="utf-8")
        config += f"kinds:\n  implement:\n    write_dirs: [{opt}]\n"
    else:
        content = {"sandbox": {"filesystem": {"allowWrite": [str(opt)]}}}
        profile.write_text(json.dumps(content), encoding="utf-8")
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        args=[*ISOLATED, "--permission-profile", str(profile)],
        config=config,
    )
    assert (run.outcome["outcome"], run.outcome["end_reason"]) == (
        "not-started",
        "invalid_permission_profile",
    )


def test_a_launch_failure_after_the_tmpdir_check_leaves_no_tmpdir(
    tmp_path: Path, state: Path, work: Path
) -> None:
    (work / "lib").mkdir()
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, result_step()],
        args=["--reads-third-party", "--sandbox", "preferred"],
        config=FAST,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(work / "lib")},
    )
    assert (run.outcome["outcome"], run.outcome["end_reason"]) == ("not-started", "unsafe_path")
    assert not csession_tmpdir(run.sid).exists()


def test_an_unsafe_path_failure_names_the_path_and_is_logged(
    tmp_path: Path, state: Path, work: Path
) -> None:
    (work / "bin").mkdir()
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, result_step()],
        args=ISOLATED,
        config=FAST,
        env={"PATH": f"{work / 'bin'}:/usr/bin:/bin"},
    )
    assert run.outcome["end_reason"] == "unsafe_path"
    assert str(work / "bin") in run.outcome["message"]
    (record,) = launch_records(state)
    assert (record["event"], record["level"], record["reason"]) == (
        "launch",
        "error",
        "unsafe_path",
    )


def test_an_unsafe_cwd_failure_carries_gits_error(tmp_path: Path, state: Path, work: Path) -> None:
    (work / ".git").write_text("not a gitfile\n", encoding="utf-8")
    run = run_exec(tmp_path, state, work, [START, result_step()], config=FAST)
    assert run.outcome["end_reason"] == "unsafe_cwd"
    assert "fatal" in run.outcome["message"]


def test_an_internal_error_before_spawn_prints_the_outcome_and_exits_12(
    tmp_path: Path, state: Path, work: Path
) -> None:
    state.mkdir(parents=True, mode=0o700)
    (state / "audit.jsonl").mkdir()  # the sandbox_lowered record cannot be written
    run = run_exec(tmp_path, state, work, [START, result_step()], args=ISOLATED, config=FAST)
    assert run.code == 12, run.proc.stderr
    assert (run.outcome["outcome"], run.outcome["end_reason"]) == (
        "not-started",
        "launch_interrupted",
    )
    assert not run.log.exists()  # claude never started
    (record,) = launch_records(state)
    assert record["event"] == "launch_interrupted" and "Error" in record["message"]
    assert not (run.private_run / "secret-index").exists()


def test_an_unusable_cost_is_null_and_the_run_still_ends(
    tmp_path: Path, state: Path, work: Path
) -> None:
    run = run_exec(
        tmp_path, state, work, [START, STOP, result_step(total_cost_usd=10**400)], config=FAST
    )
    assert run.code == 0, run.proc.stderr
    assert run.outcome["outcome"] == "done" and run.outcome["cost_usd"] is None
    assert not (run.private_run / "secret-index").exists()


def test_credential_warnings_are_logged_as_config_warnings(
    tmp_path: Path, state: Path, work: Path
) -> None:
    git_env = {**os.environ, **private_home(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"}
    for argv in (
        ["git", "init", "-q", str(work)],
        ["git", "-C", str(work), "config", "http.extraHeader", "Authorization: Bearer x"],
    ):
        subprocess.run(argv, env=git_env, check=True, timeout=30)
    run = run_exec(tmp_path, state, work, [START, STOP, result_step()], args=ISOLATED, config=FAST)
    assert any("http.extraheader" in w.lower() for w in run.outcome["warnings"])
    warnings = [r for r in read_jsonl(state / "log.jsonl") if r["event"] == "config_warning"]
    assert len(warnings) == 1 and "http.extraheader" in warnings[0]["message"].lower()


@pytest.mark.parametrize(
    ("step", "code"),
    [
        ({"args": ["done", "--summary", "drained late"]}, 0),
        ({"args": ["failed", "--reason-file", "-"], "heredoc": "drained late"}, 18),
    ],
)
def test_a_report_no_hook_drained_is_ingested_by_the_final_drain(
    tmp_path: Path, state: Path, work: Path, step: dict[str, Any], code: int
) -> None:
    report = {"do": "report", **step, "post_tool_use": "never"}
    run = run_exec(tmp_path, state, work, [START, report, result_step()], config=FAST)
    assert run.code == code, run.proc.stderr
    filed = run.outcome["report"]
    assert filed["implicit"] is False
    assert "drained late" in (filed["summary"] or filed["reason"])
    assert run.rows("SELECT result FROM requests") == [("ingested",)]


def test_the_child_start_time_is_recorded_for_a_child_that_exits_at_once(
    tmp_path: Path, state: Path, work: Path
) -> None:
    run = run_exec(tmp_path, state, work, [{"do": "exit", "code": 0}], config=FAST)
    assert run.run_row()["child_start"]


def start_hanging(
    tmp_path: Path, work: Path, steps: list[dict[str, Any]], marker: Path, config: str = ""
) -> subprocess.Popen[str]:
    scenario = write_json(tmp_path / "scenario.json", {"steps": steps})
    fake = claude_bin(tmp_path / "fakebin", scenario, tmp_path / "fake.jsonl")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"claude_bin: {fake}\n{FAST}{config}", encoding="utf-8")
    (tmp_path / "p.md").write_text("x")
    proc = subprocess.Popen(
        [
            *claude_session_cmd(),
            "exec",
            "--mode",
            "headless",
            "--json",
            "--config",
            str(cfg),
            "--prompt-file",
            str(tmp_path / "p.md"),
        ],
        cwd=work,
        env={**os.environ, **private_home(tmp_path)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    import time

    start = time.monotonic()
    while not marker.exists():
        assert proc.poll() is None, proc.communicate()
        assert time.monotonic() - start < 30.0
        time.sleep(0.02)
    return proc


def run_row_of(state: Path) -> dict[str, Any]:
    db = sqlite3.connect(f"file:{state / 'state.db'}?mode=ro", uri=True)
    try:
        cursor = db.execute("SELECT * FROM runs")
        names = [c[0] for c in cursor.description]
        return dict(zip(names, cursor.fetchone(), strict=True))
    finally:
        db.close()


def gone(pid: int, timeout: float = 10.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if "Z" not in Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]:
                time.sleep(0.05)
                continue
        except OSError:
            pass
        return True
    return False


def test_a_second_sigint_during_cleanup_still_ends_the_child_and_the_run(
    tmp_path: Path, state: Path, work: Path
) -> None:
    import signal
    import time

    marker = tmp_path / "hanging"
    steps = [
        START,
        {"do": "ignore_signals", "signals": ["TERM"]},
        {"do": "touch", "path": str(marker)},
        {"do": "hang"},
    ]
    proc = start_hanging(tmp_path, work, steps, marker, config="kill_grace: 3s\n")
    try:
        assert run_row_of(state)["phase"] == "session"  # claude is running
        proc.send_signal(signal.SIGINT)
        time.sleep(1.0)  # cleanup waits kill_grace for the child that ignores TERM
        proc.send_signal(signal.SIGINT)
        _, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == -signal.SIGINT, err
    row = run_row_of(state)
    assert (row["outcome"], row["end_reason"]) == ("killed", "internal_error")
    assert gone(row["child_pid"])


# In-process runs: a fault injected into exec itself


def exec_in_process(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    cwd: Path,
    steps: list[dict[str, Any]],
    *,
    args: Sequence[str] = (),
    config: str = FAST,
    env: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """``cli.main`` of ``exec`` in this process (so a test can patch exec's modules); the
    fake ``claude`` still runs as a child. Returns the exit code and the printed JSON."""
    from claude_session import cli

    scenario = write_json(tmp_path / "scenario.json", {"steps": steps})
    fake = claude_bin(tmp_path / "fakebin", scenario, tmp_path / "fake.jsonl")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"claude_bin: {fake}\n{config}", encoding="utf-8")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Do the task.\n", encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(sys, "argv", [str(CLAUDE_SESSION)])
    for key, value in {**private_home(tmp_path), **(env or {})}.items():
        monkeypatch.setenv(key, value)
    argv = ["exec", "--mode", "headless", "--json", "--config", str(cfg)]
    code = cli.main([*argv, "--prompt-file", str(prompt), *args])
    return code, json.loads(capsys.readouterr().out)


def test_a_failing_finish_still_finalizes_the_run_killed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    state: Path,
    work: Path,
) -> None:
    from claude_session import endmap

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("cannot parse")

    monkeypatch.setattr(endmap, "read_result_file", broken)
    code, printed = exec_in_process(
        monkeypatch,
        capsys,
        tmp_path,
        work,
        [START, STOP, result_step()],
        args=["--sandbox", "preferred"],
        env={"PATH": "/usr/bin:/bin"},
    )
    assert code == 1 and printed["error"]["code"] == "internal"
    row = run_row_of(state)
    assert (row["outcome"], row["end_reason"], row["exit_code"]) == ("killed", "internal_error", 15)
    assert row["ended_at"] is not None and row["cost_usd"] is None
    sid = row["csession_id"]
    assert not (state / "private" / sid / "runs" / "1" / "secret-index").exists()
    assert not csession_tmpdir(sid).exists()


def test_a_leftover_tmpdir_after_a_launch_failure_is_a_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    state: Path,
    work: Path,
) -> None:
    from claude_session import tmpdir

    real_remove = tmpdir.remove

    def leftover(csession_id: str, **_kwargs: Any) -> bool:
        raise tmpdir.TmpdirLeftover(
            "private temporary directory could not be removed",
            path=str(csession_tmpdir(csession_id)),
            stale_path=None,
            hint="remove it: rm -rf <path>",
        )

    state.mkdir(parents=True, mode=0o700)
    (state / "audit.jsonl").mkdir()  # fails the launch after the tmpdir exists
    monkeypatch.setattr(tmpdir, "remove", leftover)
    code, outcome = exec_in_process(
        monkeypatch,
        capsys,
        tmp_path,
        work,
        [START, result_step()],
        args=["--reads-third-party", "--sandbox", "preferred"],
        env={"PATH": "/usr/bin:/bin"},
    )
    real_remove(outcome["csession_id"])
    assert code == 12 and outcome["end_reason"] == "launch_interrupted"
    assert any("could not be removed; remove it: rm -rf" in w for w in outcome["warnings"])


def test_a_failure_right_after_the_tmpdir_is_created_removes_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    state: Path,
    work: Path,
) -> None:
    # Recording the tmpdir on the session fails right after the self-check created it: the
    # tmpdir is removed and its pin closed, and the run ends not-started as before.
    from claude_session import store, tmpdir

    created: list[tuple[tmpdir.Created, tuple[int, int]]] = []
    real_create = tmpdir.create_pinned
    real_update = store.Store.update_session

    def recording(*args: Any, **kwargs: Any) -> tmpdir.Created:
        made = real_create(*args, **kwargs)
        info = os.fstat(made.pin)
        created.append((made, (info.st_dev, info.st_ino)))
        return made

    def failing(self: store.Store, csession_id: str, **fields: Any) -> None:
        if "tmp_dir" in fields:
            raise RuntimeError("database is locked")
        real_update(self, csession_id, **fields)

    monkeypatch.setattr(tmpdir, "create_pinned", recording)
    monkeypatch.setattr(store.Store, "update_session", failing)
    code, outcome = exec_in_process(
        monkeypatch,
        capsys,
        tmp_path,
        work,
        [START, result_step()],
        args=["--sandbox", "preferred"],
        env={"PATH": "/usr/bin:/bin"},
    )
    assert code == 12 and outcome["end_reason"] == "launch_interrupted", outcome
    ((made, identity),) = created
    assert not os.path.lexists(made.path)
    try:  # the pin is closed (its number may have been reused since)
        info = os.fstat(made.pin)
    except OSError:
        pass
    else:
        assert (info.st_dev, info.st_ino) != identity


def test_a_report_committed_while_the_end_is_mapped_is_not_lost(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    state: Path,
    work: Path,
) -> None:
    from claude_session import endmap

    real_map = endmap.map_headless
    calls: list[str | None] = []

    def racing(**kwargs: Any) -> endmap.Mapped:
        mapped = real_map(**kwargs)
        calls.append(kwargs["report_status"])
        if len(calls) == 1:  # a report lands between the mapping and the finalization
            db = sqlite3.connect(state / "state.db")
            try:
                db.execute(
                    "UPDATE runs SET report_status = 'failed', report_implicit = 0, "
                    "report_reason = 'late', reported_at = '2026-01-01T00:00:00.000Z'"
                )
                db.commit()
            finally:
                db.close()
        return mapped

    monkeypatch.setattr(endmap, "map_headless", racing)
    code, outcome = exec_in_process(
        monkeypatch, capsys, tmp_path, work, [START, STOP, result_step()], config=NO_IMPLICIT
    )
    assert calls == [None, "failed"]
    assert (code, outcome["outcome"], outcome["end_reason"]) == (18, "failed", "normal")
    assert outcome["state"] == "exited"


def test_a_run_record_failure_after_spawn_is_an_internal_error_and_the_run_ends(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    state: Path,
    work: Path,
) -> None:
    """The record is read after the spawn (R2-1): a failure there goes through the
    internal-error path, so no output descriptor is left open before a spawn."""
    from claude_session import store

    real_run_record = store.Store.run_record
    failed: list[bool] = []

    def flaky(self: store.Store, *args: Any, **kwargs: Any) -> Any:
        if not failed and sys._getframe(1).f_code.co_name == "run_headless":
            failed.append(True)
            raise sqlite3.OperationalError("database is locked")
        return real_run_record(self, *args, **kwargs)

    monkeypatch.setattr(store.Store, "run_record", flaky)
    code, printed = exec_in_process(
        monkeypatch, capsys, tmp_path, work, [START, STOP, result_step()]
    )
    assert failed
    assert code == 1 and printed["error"]["code"] == "internal"
    row = run_row_of(state)
    assert (row["outcome"], row["end_reason"], row["exit_code"]) == ("killed", "internal_error", 15)
    assert row["ended_at"] is not None and row["child_pid"] is not None
