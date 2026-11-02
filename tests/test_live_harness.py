"""Unit tests of the live suite's support code (``tests/live/harness.py``, ``tmux_driver.py``) and
of its gating. They cost nothing: the fake ``claude`` stands in for the real one, tmux runs
``sh``, and the gating checks run pytest on ``tests/live`` without the opt-in. None of these is
marked ``live``."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from .fakeclaude import claude_bin, write_json
from .live import harness
from .live.tmux_driver import Tmux, parse_pane_state

REPO = Path(__file__).resolve().parent.parent
FAKE_LOGIN = "fake-api-key-not-a-secret"


@pytest.fixture
def live_env() -> Iterator[harness.LiveEnv]:
    """A LiveEnv whose ``claude`` is the fake (one scenario per test through ``scenario``)."""
    root = harness.new_root()
    env = harness.LiveEnv(
        root=root,
        claude="",
        model="haiku",
        run_usd=0.25,
        ledger=harness.Ledger(1.0),
        base_environ={"PATH": os.environ["PATH"], "HOME": "/real/home", "TMUX": "x"},
        auth=harness.Auth({"ANTHROPIC_API_KEY": FAKE_LOGIN}, None, "test"),
    )
    try:
        yield env
    finally:
        env.close()


def use_fake(env: harness.LiveEnv, tmp: Path, steps: list[dict[str, object]]) -> Path:
    extra = {"--max-budget-usd": "value"}
    scenario = write_json(tmp / "scenario.json", {"steps": steps, "extra_flags": extra})
    log = tmp / "fake.jsonl"
    env.claude = str(claude_bin(tmp / "bin", scenario, log))
    return log


# --- gating ----------------------------------------------------------------------------------


def test_the_live_suite_is_not_opted_in_by_default() -> None:
    assert not harness.opted_in({})
    assert not harness.opted_in({harness.OPT_IN_ENV: "true"})
    assert not harness.opted_in({harness.OPT_IN_ENV: ""})
    assert harness.opted_in({harness.OPT_IN_ENV: "1"})


def run_pytest(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CSESSION_LIVE")}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_a_default_run_deselects_every_live_test() -> None:
    proc = run_pytest("tests/live", "--collect-only", "-q")
    assert "deselected" in proc.stdout, proc.stdout[-500:]
    assert "tests/live/test_" not in proc.stdout  # nothing collected into the run


def test_live_tests_are_collected_with_the_live_marker() -> None:
    proc = run_pytest("tests/live", "-m", "live", "--collect-only", "-q")
    assert proc.returncode == 0, proc.stdout[-500:] + proc.stderr[-500:]
    collected = [ln for ln in proc.stdout.splitlines() if ln.startswith("tests/live/test_")]
    assert len(collected) >= 20
    other = run_pytest("tests/live", "-m", "not live", "--collect-only", "-q")
    assert "tests/live/test_" not in other.stdout


def test_asking_for_live_tests_without_the_opt_in_skips_them_all() -> None:
    proc = run_pytest("tests/live", "-m", "live", "-q", "-rs", "-p", "no:randomly")
    assert proc.returncode == 0, proc.stdout[-800:]
    assert "passed" not in proc.stdout and "failed" not in proc.stdout, proc.stdout[-800:]
    assert "skipped" in proc.stdout
    assert f"{harness.OPT_IN_ENV}=1" in proc.stdout


def test_make_live_sets_the_opt_in_and_selects_the_marker() -> None:
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    live = makefile.split("\nlive:", 1)[1].split("\n\n", 1)[0]
    assert f"{harness.OPT_IN_ENV}=1" in live and "-m live" in live and "tests/live" in live
    verify = next(ln for ln in makefile.splitlines() if ln.startswith("verify:"))
    assert "live" not in verify.split(":", 1)[1].split()


# --- credentials and environment ---------------------------------------------------------------


def test_auth_prefers_the_api_key_then_the_token(tmp_path: Path) -> None:
    both = {"ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_OAUTH_TOKEN": "t"}
    auth = harness.resolve_auth(both, tmp_path)
    assert auth is not None and dict(auth.env) == {"ANTHROPIC_API_KEY": "k"}
    auth = harness.resolve_auth({"CLAUDE_CODE_OAUTH_TOKEN": "t"}, tmp_path)
    assert auth is not None and dict(auth.env) == {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
    assert harness.resolve_auth({"ANTHROPIC_API_KEY": ""}, tmp_path) is None


def test_the_login_file_is_copied_only_when_asked_and_never_through_a_symlink(
    tmp_path: Path,
) -> None:
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir()
    creds.write_text("{}", encoding="utf-8")
    assert harness.resolve_auth({}, tmp_path) is None
    auth = harness.resolve_auth({harness.COPY_LOGIN_ENV: "1"}, tmp_path)
    assert auth is not None and auth.source == creds and not auth.env
    creds.unlink()
    creds.symlink_to(tmp_path / "elsewhere")
    assert harness.resolve_auth({harness.COPY_LOGIN_ENV: "1"}, tmp_path) is None


def test_install_login_copies_with_mode_0600_and_leaves_the_source_alone(tmp_path: Path) -> None:
    source = tmp_path / "creds.json"
    source.write_text('{"a": 1}', encoding="utf-8")
    source.chmod(0o644)
    root = harness.new_root()
    try:
        harness.install_login(root, harness.Auth({}, source, "copy"))
        target = root / "home" / ".claude" / ".credentials.json"
        assert target.read_text(encoding="utf-8") == '{"a": 1}'
        assert target.stat().st_mode & 0o777 == 0o600
        assert source.stat().st_mode & 0o777 == 0o644
    finally:
        shutil.rmtree(root)


def test_the_child_environment_is_an_allowlist_with_a_private_home(tmp_path: Path) -> None:
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/real/home",
        "XDG_CONFIG_HOME": "/real/home/.config",
        "CLAUDE_CONFIG_DIR": "/real/home/.claude",
        "TMUX": "/tmp/tmux-1000/default,1,0",
        "SSH_AUTH_SOCK": "/run/ssh",
        "AWS_PROFILE": "work",
        "ANTHROPIC_BASE_URL": "https://example.invalid",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "LANG": "C.UTF-8",
        "HTTPS_PROXY": "http://proxy.invalid:3128",
    }
    env = harness.build_env(
        parent, root=tmp_path, auth=harness.Auth({"ANTHROPIC_API_KEY": "k"}, None, "k")
    )
    assert env["HOME"] == str(tmp_path / "home")
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "home" / ".config")
    assert env["TMPDIR"] == str(tmp_path / "tmp")
    for dropped in ("TMUX", "SSH_AUTH_SOCK", "AWS_PROFILE", "CLAUDE_CONFIG_DIR",
                    "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK"):  # fmt: skip
        assert dropped not in env
    assert env["LANG"] == "C.UTF-8" and env["HTTPS_PROXY"] == "http://proxy.invalid:3128"
    assert env["ANTHROPIC_API_KEY"] == "k"
    assert "/real/home" not in "".join(env.values())


@pytest.mark.parametrize("name", list(harness.FORBIDDEN_EXTRA))
def test_a_test_cannot_point_claude_back_at_the_real_configuration(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(ValueError, match=name):
        harness.build_env({}, root=tmp_path, auth=None, extra={name: "/real/home"})


def test_extra_environment_is_added_after_the_allowlist(tmp_path: Path) -> None:
    env = harness.build_env({}, root=tmp_path, auth=None, extra={"CSESSION_ID": "cs-1"})
    assert env["CSESSION_ID"] == "cs-1" and "ANTHROPIC_API_KEY" not in env


def test_claude_json_trusts_only_the_given_directories(tmp_path: Path) -> None:
    data = harness.claude_json([tmp_path / "a"])
    assert data["hasCompletedOnboarding"] is True
    assert list(data["projects"]) == [str(tmp_path / "a")]
    assert data["projects"][str(tmp_path / "a")]["hasTrustDialogAccepted"] is True


def test_the_private_root_is_short_and_private() -> None:
    root = harness.new_root()
    try:
        assert len(str(root)) <= 24  # room for Claude's 34-byte socket names under TMPDIR
        assert root.stat().st_mode & 0o777 == 0o700
        assert (root / "tmp").is_dir() and (root / "home" / ".claude").is_dir()
    finally:
        shutil.rmtree(root)


# --- ledger and numbers ----------------------------------------------------------------------


def test_the_ledger_exhausts_at_the_cap() -> None:
    ledger = harness.Ledger(1.0)
    ledger.charge(0.6)
    assert not ledger.exhausted
    ledger.charge(0.4)
    assert ledger.exhausted and ledger.runs == 2
    ledger.charge(-5)  # a bad number never refunds
    assert ledger.spent_usd == pytest.approx(1.0)


@pytest.mark.parametrize("raw", ["abc", "0", "-1", "nan"])
def test_env_float_rejects_bad_numbers(raw: str) -> None:
    with pytest.raises(ValueError):
        harness.env_float({"X": raw}, "X", 1.0)


def test_env_float_defaults_and_parses() -> None:
    assert harness.env_float({}, "X", 2.5) == 2.5
    assert harness.env_float({"X": ""}, "X", 2.5) == 2.5
    assert harness.env_float({"X": "0.75"}, "X", 2.5) == 0.75


# --- stream parsing and classification ---------------------------------------------------------


def test_parse_stream_reads_objects_and_rejects_other_lines() -> None:
    events = harness.parse_stream('{"type": "system", "subtype": "init", "permissionMode": "x"}\n\n'
                                  '{"type": "result", "total_cost_usd": 0.5}\n'
                                  '{"type": "result", "total_cost_usd": 0.25}\n')  # fmt: skip
    assert harness.init_event(events)["permissionMode"] == "x"
    assert len(harness.results(events)) == 2
    assert harness.total_cost(events) == pytest.approx(0.75)
    with pytest.raises(ValueError):
        harness.parse_stream("[1]\n")
    with pytest.raises(ValueError):
        harness.parse_stream("not json\n")
    with pytest.raises(AssertionError):
        harness.init_event([])


def test_total_cost_ignores_missing_and_non_numeric_costs() -> None:
    events = [
        {"type": "result"},
        {"type": "result", "total_cost_usd": "1"},
        {"type": "result", "total_cost_usd": True},
        {"type": "assistant", "total_cost_usd": 9},
    ]
    assert harness.total_cost(events) == 0.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Could not load credentials from any providers (AWS)", "bedrock"),
        ("Amazon Bedrock authentication failed", "bedrock"),
        ("Could not load the default credentials: Application Default Credentials", "vertex"),
        ("Invalid API key · Please run /login", "anthropic"),
        ("something else entirely", "unknown"),
        ("", "unknown"),
    ],
)
def test_provider_of_error_classifies_known_messages(text: str, expected: str) -> None:
    assert harness.provider_of_error(text) == expected


def test_hook_group_layout() -> None:
    group = harness.hook_group("echo hi", matcher="Bash", timeout=3)
    assert group == {
        "hooks": [{"type": "command", "command": "echo hi", "timeout": 3}],
        "matcher": "Bash",
    }
    assert harness.hook_group("x") == {"hooks": [{"type": "command", "command": "x"}]}


def test_read_log_parses_payloads_and_tolerates_bad_stdin(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    rows = [
        {"args": ["a"], "stdin": json.dumps({"hook_event_name": "Stop", "x": 1})},
        {"args": ["b"], "stdin": "not json"},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n\n", encoding="utf-8")
    records = harness.read_log(log)
    assert [r["payload"] for r in records] == [{"hook_event_name": "Stop", "x": 1}, None]
    assert harness.events_named(records, "Stop") == [{"hook_event_name": "Stop", "x": 1}]
    assert harness.read_log(tmp_path / "missing.jsonl") == []


# --- running the (fake) claude -----------------------------------------------------------------


def test_headless_runs_in_the_private_environment_and_charges_the_ledger(
    live_env: harness.LiveEnv, tmp_path: Path
) -> None:
    log = use_fake(
        live_env,
        tmp_path,
        [{"do": "session_start"}, {"do": "result", "result": "OK", "total_cost_usd": 0.5}],
    )
    run = live_env.headless("Reply with OK.", "--setting-sources", "")
    assert run.text == "OK" and run.proc.returncode == 0
    assert live_env.ledger.spent_usd == pytest.approx(0.5)
    argv = next(
        r["argv"]
        for r in json.loads("[" + ",".join(log.read_text().splitlines()) + "]")
        if "argv" in r
    )
    assert argv[:1] and "--model" in argv and "haiku" in argv
    assert argv[argv.index("--max-budget-usd") + 1] == "0.25"
    assert argv[-2:] == ["--", "Reply with OK."]  # the prompt follows `--`
    assert argv.index("--setting-sources") < argv.index("--")  # test flags precede it


def test_a_spent_ledger_skips_the_next_run(live_env: harness.LiveEnv, tmp_path: Path) -> None:
    use_fake(live_env, tmp_path, [{"do": "result", "result": "OK"}])
    live_env.ledger.charge(5.0)
    with pytest.raises(pytest.skip.Exception):
        live_env.headless("hi")


def test_a_timeout_fails_the_test_and_ends_the_run(
    live_env: harness.LiveEnv, tmp_path: Path
) -> None:
    use_fake(live_env, tmp_path, [{"do": "hang"}])
    with pytest.raises(AssertionError, match="timed out"):
        live_env.headless("hi", timeout=1)


# --- tmux --------------------------------------------------------------------------------------


needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")


def test_parse_pane_state() -> None:
    assert parse_pane_state("123 0  \n").dead is False
    state = parse_pane_state("123 1 3 ")
    assert (state.pid, state.dead, state.exit_status, state.exit_signal) == (123, True, 3, None)
    state = parse_pane_state("9 1  15")
    assert (state.exit_status, state.exit_signal) == (None, 15)


@pytest.fixture
def tmux(live_env: harness.LiveEnv) -> Iterator[Tmux]:
    driver = Tmux(live_env.env(), live_env.root / "t.sock")
    try:
        yield driver
    finally:
        driver.kill()


@needs_tmux
def test_tmux_reports_a_panes_exit_status(live_env: harness.LiveEnv, tmux: Tmux) -> None:
    tmux.start(["sh", "-c", "exit 3"], cwd=live_env.dir("work"))
    state = tmux.wait_dead(10)
    assert state is not None and state.exit_status == 3


@needs_tmux
def test_tmux_sends_keys_and_text_to_a_running_pane(live_env: harness.LiveEnv, tmux: Tmux) -> None:
    tmux.start(["sh", "-c", "read line; echo got:$line; exit 0"], cwd=live_env.dir("work"))
    assert not tmux.state().dead
    tmux.send_text("a b;c")
    tmux.send_keys("Enter")
    assert tmux.wait_for("got:a b;c", 10)
    state = tmux.wait_dead(10)
    assert state is not None and state.exit_status == 0


@needs_tmux
def test_tmux_runs_in_the_private_environment_and_its_own_socket(
    live_env: harness.LiveEnv, tmux: Tmux
) -> None:
    tmux.start(
        ["sh", "-c", 'echo "home=$HOME tmux=${TMUX:-none}"; sleep 30'], cwd=live_env.dir("work")
    )
    assert tmux.wait_for(f"home={live_env.home}", 10)
    assert "tmux=/" in tmux.capture() and str(live_env.root / "t.sock") in tmux.capture()
    assert (live_env.root / "t.sock").exists()


@needs_tmux
def test_tmux_kill_stops_the_server_and_its_pane(live_env: harness.LiveEnv, tmux: Tmux) -> None:
    tmux.start(["sleep", "300"], cwd=live_env.dir("work"))
    pid = tmux.state().pid
    tmux.kill()
    assert not tmux.started
    assert harness.wait_for(lambda: not Path(f"/proc/{pid}").exists(), 10)


# --- recorded-PID kill -------------------------------------------------------------------------


def test_kill_recorded_requires_the_matching_start_token() -> None:
    proc = subprocess.Popen(["sleep", "300"])
    try:
        from claude_session import gateway, procinfo

        token = procinfo.ProcInfo(gateway.SystemGateway()).start_time(proc.pid)
        assert token is not None
        assert not harness.kill_recorded(proc.pid, None)
        assert not harness.kill_recorded(proc.pid, token + "0")
        assert proc.poll() is None
        assert harness.kill_recorded(proc.pid, token)
        proc.wait(10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
