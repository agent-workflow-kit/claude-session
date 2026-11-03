"""Unit tests of the live suite's support code (``tests/live/harness.py``, ``tmux_driver.py``) and
of its gating. They cost nothing: the fake ``claude`` stands in for the real one, tmux runs
``sh``, and the gating checks run pytest on ``tests/live`` without the opt-in. None of these is
marked ``live``."""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
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


def test_the_login_cache_copies_once_installs_0600_and_collects_a_refresh(
    tmp_path: Path,
) -> None:
    source = tmp_path / "creds.json"
    source.write_text('{"a": 1}', encoding="utf-8")
    source.chmod(0o644)
    cache = harness.new_login_cache(source)
    try:
        source.write_text('{"changed": "after the copy"}', encoding="utf-8")
        home = tmp_path / "home"
        cache.install(home)
        target = home / ".claude" / ".credentials.json"
        assert target.read_text(encoding="utf-8") == '{"a": 1}'  # the session copy, not a reread
        assert target.stat().st_mode & 0o777 == 0o600
        target.write_text('{"a": 2}', encoding="utf-8")  # claude refreshed the token
        cache.collect(home)
        other = tmp_path / "other-home"
        cache.install(other)
        assert (other / ".claude" / ".credentials.json").read_text(encoding="utf-8") == '{"a": 2}'
        assert source.read_text(encoding="utf-8") == '{"changed": "after the copy"}'
        assert source.stat().st_mode & 0o777 == 0o644
        target.unlink()
        target.symlink_to(tmp_path / "elsewhere")
        cache.collect(home)  # a symlink is never followed
        assert cache.path.read_text(encoding="utf-8") == '{"a": 2}'
    finally:
        cache.remove()
    assert not cache.directory.exists()


def test_a_run_without_credentials_gets_a_home_without_the_login_file(
    live_env: harness.LiveEnv,
) -> None:
    (live_env.home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    with_auth, without = live_env.env(), live_env.env(auth=False)
    assert with_auth["HOME"] == str(live_env.home) and with_auth["ANTHROPIC_API_KEY"] == FAKE_LOGIN
    assert without["HOME"] != with_auth["HOME"] and "ANTHROPIC_API_KEY" not in without
    assert not (Path(without["HOME"]) / ".claude" / ".credentials.json").exists()
    assert without["XDG_CONFIG_HOME"].startswith(without["HOME"])


def test_the_cheap_fixture_never_resolves_credentials() -> None:
    source = (REPO / "tests" / "live" / "conftest.py").read_text(encoding="utf-8")
    assert "resolve_auth(os.environ, _real_home()) if needs_auth else None" in source


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


FORBIDDEN_NAMES = [*harness.FORBIDDEN_EXTRA, "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "XDG_CACHE_HOME"]


@pytest.mark.parametrize("name", FORBIDDEN_NAMES)
def test_a_test_cannot_point_claude_back_at_the_real_configuration(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(ValueError, match=name):
        harness.build_env({}, root=tmp_path, auth=None, extra={name: "/real/home"})


def test_every_name_the_private_environment_sets_is_forbidden_except_path(tmp_path: Path) -> None:
    env = harness.build_env({}, root=tmp_path, auth=None)
    assert sorted(n for n in env if n != "PATH" and not harness.forbidden_extra(n)) == []
    for name in ("GIT_CONFIG_GLOBAL", "CLAUDE_CONFIG_DIR", "HOME", "TMPDIR"):
        assert harness.forbidden_extra(name)
    assert not harness.forbidden_extra("PATH") and not harness.forbidden_extra("CSESSION_ID")


def test_extra_environment_is_added_after_the_allowlist(tmp_path: Path) -> None:
    env = harness.build_env({}, root=tmp_path, auth=None, extra={"CSESSION_ID": "cs-1"})
    assert env["CSESSION_ID"] == "cs-1" and "ANTHROPIC_API_KEY" not in env


def test_claude_json_trusts_only_the_given_directories(tmp_path: Path) -> None:
    data = harness.claude_json([tmp_path / "a"])
    assert data["hasCompletedOnboarding"] is True
    assert list(data["projects"]) == [str(tmp_path / "a")]
    assert data["projects"][str(tmp_path / "a")]["hasTrustDialogAccepted"] is True
    assert "customApiKeyResponses" not in data


def test_claude_json_approves_the_api_key_by_its_last_20_characters() -> None:
    key = "sk-ant-api03-" + "x" * 30 + "ABCDEFGHIJ0123456789"
    data = harness.claude_json([], api_key=key + "\n")
    assert data["customApiKeyResponses"] == {"approved": ["ABCDEFGHIJ0123456789"], "rejected": []}


def test_trust_approves_the_api_key_in_use(live_env: harness.LiveEnv) -> None:
    live_env.trust(live_env.dir("work"))
    data = json.loads((live_env.home / ".claude.json").read_text(encoding="utf-8"))
    assert data["customApiKeyResponses"]["approved"] == [FAKE_LOGIN[-20:]]
    assert list(data["projects"]) == [str(live_env.root / "work")]


def test_reprs_never_show_a_credential_or_the_parent_environment(tmp_path: Path) -> None:
    secret = "sk-planted-secret-value-1234"
    auth = harness.Auth({"ANTHROPIC_API_KEY": secret}, None, "test")
    env = harness.LiveEnv(
        root=tmp_path,
        claude="/bin/true",
        model="haiku",
        run_usd=0.25,
        ledger=harness.Ledger(1.0),
        base_environ={"ANTHROPIC_API_KEY": secret, "OTHER_TOKEN": secret},
        auth=auth,
    )
    for text in (repr(auth), repr(env), str(env)):
        assert secret not in text


def test_relative_claude_and_path_entries_resolve_to_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "tmux"):
        (bin_dir / name).write_text("#!/bin/sh\n", encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    monkeypatch.chdir(tmp_path)
    explicit = harness.claude_path({harness.CLAUDE_ENV: "bin/claude"})
    assert explicit == str(bin_dir / "claude")
    assert harness.claude_path({"PATH": "bin"}) == str(bin_dir / "claude")
    assert harness.tool_path("tmux", {"PATH": "bin"}) == str(bin_dir / "tmux")
    assert harness.tool_path("no-such-tool", {"PATH": "bin"}) is None


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


def test_a_spent_ledger_skips_every_billed_path(live_env: harness.LiveEnv, tmp_path: Path) -> None:
    use_fake(live_env, tmp_path, [{"do": "result", "result": "OK"}])
    live_env.ledger.charge(5.0)
    with pytest.raises(pytest.skip.Exception):
        live_env.headless("hi")
    with pytest.raises(pytest.skip.Exception):
        live_env.claude_run(["-p", *live_env.budget(), "--", "hi"])
    with pytest.raises(pytest.skip.Exception):
        live_env.spawn_headless("hi", out=tmp_path / "o", err=tmp_path / "e")
    with pytest.raises(pytest.skip.Exception):
        live_env.spend_gate()
    assert live_env.spawned == []
    # A run without credentials costs nothing and is not gated.
    proc = live_env.claude_run(["--version"], auth=False)
    assert proc.returncode == 0


def test_a_print_run_without_a_budget_is_refused(live_env: harness.LiveEnv) -> None:
    for argv in (["-p", "--", "hi"], ["--print", "hi"]):
        with pytest.raises(ValueError, match="max-budget-usd"):
            live_env.claude_run(argv)
    assert live_env.ledger.runs == 0


def test_a_timeout_fails_the_test_ends_the_run_and_charges_the_estimate(
    live_env: harness.LiveEnv, tmp_path: Path
) -> None:
    use_fake(live_env, tmp_path, [{"do": "hang"}])
    with pytest.raises(AssertionError, match="timed out"):
        live_env.headless("hi", timeout=1)
    assert live_env.ledger.spent_usd == pytest.approx(live_env.run_usd)


def test_output_without_a_cost_charges_the_estimate(
    live_env: harness.LiveEnv, tmp_path: Path
) -> None:
    use_fake(live_env, tmp_path, [{"do": "emit", "line": "not json"}])
    with pytest.raises(ValueError):
        live_env.headless("hi")
    assert live_env.ledger.spent_usd == pytest.approx(live_env.run_usd)
    assert harness.cost_of_output("not json\n") is None
    assert harness.cost_of_output('{"type": "result"}\n') is None
    assert harness.cost_of_output('{"type": "result", "total_cost_usd": 0.1}\n') == 0.1


def test_a_spawned_run_is_charged_its_reported_cost_at_close(
    live_env: harness.LiveEnv, tmp_path: Path
) -> None:
    use_fake(live_env, tmp_path, [{"do": "result", "result": "OK", "total_cost_usd": 0.125}])
    run = live_env.spawn_headless("hi", out=tmp_path / "out.jsonl", err=tmp_path / "err.txt")
    assert harness.wait_for(lambda: run.returncode() is not None, 30)
    assert run.returncode() == 0 and run.returncode() == 0  # peeking never reaps
    live_env.close()
    assert live_env.ledger.spent_usd == pytest.approx(0.125)


def test_a_spawned_run_without_a_result_is_killed_with_its_group_and_charged_the_estimate(
    live_env: harness.LiveEnv, tmp_path: Path
) -> None:
    pidfile = tmp_path / "bg.pid"
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\nsleep 300 &\necho $! > {pidfile}\nwait\n", encoding="utf-8")
    script.chmod(0o755)
    live_env.claude = str(script)
    run = live_env.spawn_headless("hi", out=tmp_path / "out.jsonl", err=tmp_path / "err.txt")
    assert harness.wait_for(lambda: pidfile.exists() and pidfile.read_text().strip() != "", 10)
    background = int(pidfile.read_text(encoding="utf-8"))
    assert os.getpgid(background) == run.pid  # the run leads its own group
    assert run.returncode() is None
    live_env.close()
    assert harness.wait_for(lambda: harness.gone(background), 10)
    assert live_env.ledger.spent_usd == pytest.approx(live_env.run_usd)


def test_teardown_collects_the_login_only_after_the_spawned_runs_end(
    live_env: harness.LiveEnv, tmp_path: Path
) -> None:
    source = tmp_path / "creds.json"
    source.write_text('{"token": "old"}', encoding="utf-8")
    run_state: list[int | None] = []

    class Spy(harness.LoginCache):
        def collect(self, home: Path) -> None:
            run_state.append(run.returncode())  # still running here = a rotation could be lost
            super().collect(home)

    directory = tmp_path / "cache"
    directory.mkdir()
    cache = Spy(source, directory)
    cache.install(live_env.home)
    started = tmp_path / "started"
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\n"
        """printf '{"token": "rotated"}' > "$HOME/.claude/.credentials.json"\n"""
        f"touch {started}\nexec sleep 300\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    live_env.claude = str(script)
    run = live_env.spawn_headless("hi", out=tmp_path / "out.jsonl", err=tmp_path / "err.txt")
    assert harness.wait_for(started.exists, 10)
    harness.teardown(live_env, cache)
    assert run_state == [-signal.SIGKILL]  # the run had ended before the snapshot
    assert cache.path.read_text(encoding="utf-8") == '{"token": "rotated"}'
    assert live_env.ledger.spent_usd == pytest.approx(live_env.run_usd)  # charged once
    assert not live_env.root.exists()
    live_env.close()  # the fixture's close after teardown neither recharges nor fails
    assert live_env.ledger.spent_usd == pytest.approx(live_env.run_usd)


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
    assert harness.wait_for(lambda: harness.gone(pid), 10)


@needs_tmux
def test_a_failed_start_leaves_no_tmux_server(live_env: harness.LiveEnv, tmux: Tmux) -> None:
    with pytest.raises(RuntimeError, match="could not start"):
        tmux.start(["sleep", "300"], cwd=live_env.dir("work"), width=0)
    assert not tmux.started
    tmux.kill()
    probe = subprocess.run(
        ["tmux", "-f", "/dev/null", "-S", str(live_env.root / "t.sock"), "list-sessions"],
        env=live_env.env(),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert probe.returncode != 0


# --- recorded-PID signals -----------------------------------------------------------------------


def _token(pid: int) -> str:
    from claude_session import gateway, procinfo

    token = procinfo.ProcInfo(gateway.SystemGateway()).start_time(pid)
    assert token is not None
    return token


def test_kill_recorded_requires_the_matching_start_token() -> None:
    proc = subprocess.Popen(["sleep", "300"])
    try:
        token = _token(proc.pid)
        assert not harness.kill_recorded(proc.pid, None)
        assert not harness.kill_recorded(proc.pid, token + "0")
        assert proc.poll() is None
        assert harness.kill_recorded(proc.pid, token)
        proc.wait(10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_signal_recorded_sends_any_signal_and_a_gone_process_only_stops_the_sending() -> None:
    proc = subprocess.Popen(["sleep", "300"])
    try:
        token = _token(proc.pid)
        assert harness.signal_recorded(proc.pid, token, signal.SIGTERM)
        assert proc.wait(10) == -signal.SIGTERM
        assert not harness.signal_recorded(proc.pid, token, signal.SIGTERM)  # reaped: no error
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_runs_executable_checks_the_program_of_the_exact_process(tmp_path: Path) -> None:
    sleep = shutil.which("sleep")
    assert sleep is not None
    proc = subprocess.Popen([sleep, "300"])
    script = tmp_path / "prog.py"
    script.write_text("import time\ntime.sleep(300)\n", encoding="utf-8")
    interpreted = subprocess.Popen([sys.executable, str(script)])
    try:
        assert harness.runs_executable(proc.pid, sleep)
        assert not harness.runs_executable(proc.pid, "/bin/sh")
        assert harness.runs_executable(interpreted.pid, str(script))  # a script's interpreter
    finally:
        for p in (proc, interpreted):
            p.kill()
            p.wait()
    assert not harness.runs_executable(proc.pid, sleep)  # gone


def test_a_hook_records_its_own_pid_and_token_and_teardown_kills_it(
    live_env: harness.LiveEnv, tmp_path: Path
) -> None:
    pids = tmp_path / "pids"
    command = live_env.pid_record_command("$$", pids)
    proc = subprocess.Popen(["/bin/sh", "-c", f"{command}; exec sleep 300"], env=live_env.env())
    try:
        assert harness.wait_for(lambda: bool(harness.read_pids(pids)), 30)
        ((pid, token),) = harness.read_pids(pids)
        assert pid == proc.pid and token == _token(proc.pid)
        live_env.close()
        assert proc.wait(10) == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_read_pids_skips_bad_lines(tmp_path: Path) -> None:
    path = tmp_path / "pids"
    path.write_text("12 boot:34\nnot a pid\n56 \n", encoding="utf-8")
    assert harness.read_pids(path) == [(12, "boot:34"), (56, None)]
    assert harness.read_pids(tmp_path / "missing") == []


# --- transcripts -------------------------------------------------------------------------------


def test_tool_result_text_holds_only_what_tools_returned() -> None:
    records = [
        {"type": "user", "message": {"content": "run sh -c 'echo shell-ok'"}},
        {
            "type": "assistant",
            "message": {"id": "m1", "content": [{"type": "tool_use", "id": "t1", "name": "Bash"}]},
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "out-one"},
                    {
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "content": [{"type": "text", "text": "out-two"}, {"type": "image"}],
                    },
                ]
            },
        },
    ]
    text = harness.tool_result_text(records)
    assert "out-one" in text and "out-two" in text and "shell-ok" not in text


def test_tool_uses_are_grouped_by_assistant_message() -> None:
    def use(message_id: str, tool_id: str) -> dict[str, object]:
        block = {"type": "tool_use", "id": tool_id, "name": "Read"}
        return {"type": "assistant", "message": {"id": message_id, "content": [block]}}

    records = [
        use("m1", "a"),
        use("m1", "b"),
        {"type": "user", "message": {"content": "x"}},
        use("m1", "c"),
        use("m2", "d"),
        {"type": "assistant", "message": {"id": "m3", "content": [{"type": "text"}]}},
    ]
    grouped = harness.tool_uses_by_message(records)
    assert [[b["id"] for b in uses] for uses in grouped] == [["a", "b", "c"], ["d"]]


# --- tool calls in a stream, and the elicitation test server --------------------------------


def _stream_with_tools() -> list[dict[str, object]]:
    def use(ident: str, name: str, **inp: object) -> dict[str, object]:
        block = {"type": "tool_use", "id": ident, "name": name, "input": inp}
        return {"type": "assistant", "message": {"content": [block]}}

    def text(value: str) -> dict[str, object]:
        return {"type": "text", "text": value}

    def result(ident: str, content: object, error: bool = False) -> dict[str, object]:
        block = {"type": "tool_result", "tool_use_id": ident, "content": content}
        if error:
            block["is_error"] = True
        return {"type": "user", "message": {"content": [block]}}

    return [
        {"type": "system", "subtype": "init"},
        use("a", "Read", file_path="/x"),
        result("a", "file text"),
        use("b", "Grep", pattern="p"),
        result("b", [{"type": "text", "text": "one"}, {"type": "image"}, text("two")], True),
        use("c", "Bash", command="ls"),  # no result came back
        {"type": "assistant", "message": {"content": "plain string content"}},
        {"type": "result", "result": "done"},
    ]  # fmt: skip


def test_tool_calls_pair_each_use_with_its_result_in_order() -> None:
    calls = harness.tool_calls(_stream_with_tools())
    assert [(c.name, c.output, c.is_error) for c in calls] == [
        ("Read", "file text", False),
        ("Grep", "one\ntwo", True),
        ("Bash", "", False),
    ]
    assert calls[0].input == {"file_path": "/x"}


def test_tool_calls_ignore_results_without_a_matching_use() -> None:
    orphan = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "zz", "content": "x"}]},
    }
    assert harness.tool_calls([orphan]) == []


def test_tool_calls_skip_blocks_without_an_id() -> None:
    """Blocks without an id cannot be paired: they are skipped, not merged under one key."""
    stream = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/a"}},
            {"type": "tool_use", "id": "", "name": "Grep", "input": {}},
            {"type": "tool_use", "id": "k", "name": "Glob", "input": {"pattern": "*"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "orphan"},
            {"type": "tool_result", "tool_use_id": "k", "content": "x.txt"},
        ]}},
    ]  # fmt: skip
    calls = harness.tool_calls(stream)
    assert [(c.name, c.output) for c in calls] == [("Glob", "x.txt")]


def _call(name: str, output: str = "", error: bool = False, **inp: object) -> harness.ToolCall:
    return harness.ToolCall(name, inp, output, error)


def test_probe_calls_select_the_calls_that_name_the_target() -> None:
    calls = [
        _call("Read", file_path="/w/plain.txt"),
        _call("Read", file_path="/w/link.txt"),
        _call("Grep", pattern="S", path="/w/link.txt"),
        _call("Grep", pattern="S", path="/w"),
        _call("Glob", pattern="dirlink/*", path="/w"),
        _call("Bash", command="cat /proc/42/environ"),
        _call("Bash", command="ls /proc"),
    ]
    assert harness.probe_calls(calls, "/w/link.txt", "Read") == [calls[1]]
    assert harness.probe_calls(calls, "/w/link.txt", "Read", "Grep") == calls[1:3]
    assert harness.probe_calls(calls, "dirlink", "Glob") == [calls[4]]
    assert harness.probe_calls(calls, "/proc/42/environ", "Bash") == [calls[5]]
    assert harness.probe_calls(calls, "/w/other.txt", "Read", "Grep") == []


def test_a_leak_is_the_marker_or_a_named_file_in_a_successful_result() -> None:
    content = _call("Grep", "/w/link.txt:F1-SECRET-7782")
    paths_only = _call("Grep", "Found 1 file\n/w/link.txt")
    relative = _call("Grep", "link.txt")
    other = _call("Grep", "Found 1 file\n/w/plain.txt\n/w/unlink.txt.bak")
    denied = _call("Grep", "Permission denied: /w/link.txt", error=True)
    read = _call("Read", "1\tF1-SECRET-7782")
    assert harness.leaked([content], "F1-SECRET-7782", ("link.txt",)) == [content]
    assert harness.leaked([paths_only, relative], "F1-SECRET-7782", ("link.txt",)) == [
        paths_only,
        relative,
    ]
    assert harness.leaked([other, denied], "F1-SECRET-7782", ("link.txt",)) == []
    assert harness.leaked([read], "F1-SECRET-7782") == [read]


def test_a_control_accepts_the_text_or_a_listed_file() -> None:
    assert harness.found([_call("Grep", "plain.txt:CONTROL")], "CONTROL")
    assert harness.found([_call("Grep", "Found 1 file\n/w/plain.txt")], "CONTROL", ("plain.txt",))
    assert not harness.found([_call("Grep", "No files found")], "CONTROL", ("plain.txt",))
    assert not harness.found([_call("Grep", "/w/plain.txt", error=True)], "X", ("plain.txt",))


@pytest.mark.parametrize(
    ("output", "missing"),
    [
        ("File does not exist.", True),
        ("Path does not exist: /w/link.txt", True),
        ("ENOENT: no such file or directory", True),
        ("Permission denied", False),
        ("No files found", False),
    ],
)
def test_missing_file_errors_are_recognized(output: str, missing: bool) -> None:
    assert harness.missing_file(_call("Read", output, error=True)) is missing


@pytest.mark.parametrize(
    ("command", "listing"),
    [
        ("ls /proc", True),
        ("  ls /proc/ ", True),
        ("ls -1 /proc", True),
        ("ls -la /proc 2>&1", True),
        ("ls --color=never /proc", True),
        ("ls -1 --color=never /proc/ 2>&1", True),
        ("ls /proc/self", False),
        ("ls /procfs", False),
        ("cat /proc/1/cmdline", False),
        ("ls /proc; cat /etc/passwd", False),
    ],
)
def test_proc_listing_commands(command: str, listing: bool) -> None:
    assert harness.is_proc_listing(command) is listing


def test_proc_entries_parses_a_listing_and_rejects_a_failed_one() -> None:
    ok = _call("Bash", "1\n2\n7\nacpi\nself\nthread-self\nuptime")
    assert harness.proc_entries(ok) == {"1", "2", "7"}
    columns = _call("Bash", "1    7    acpi\n12   self uptime\n")
    assert harness.proc_entries(columns) == {"1", "7", "12"}
    long = _call(
        "Bash",
        "total 0\ndr-xr-xr-x 9 root root 0 Jan 1 00:00 1\n"
        "lrwxrwxrwx 1 root root 0 Jan 1 00:00 self -> 9\n",
    )
    assert harness.proc_entries(long) == {"1"}
    assert harness.proc_entries(_call("Bash", "", error=False)) is None
    assert harness.proc_entries(_call("Bash", "ls: cannot open directory '/proc'", True)) is None
    assert harness.proc_entries(_call("Bash", "1\n2\n")) is None  # no `self`: not a /proc


ELICIT_SERVER = REPO / "tests" / "live" / "mcp_elicit_server.py"


class _Server:
    """The elicitation server driven like a client, with a deadline on every read."""

    def __init__(self, tmp_path: Path) -> None:
        self.log = tmp_path / "server.log"
        self.proc = subprocess.Popen(
            [sys.executable, "-I", str(ELICIT_SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env={"ELICIT_SERVER_LOG": str(self.log), "PATH": os.environ["PATH"]},
        )
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.stdin, stdout = self.proc.stdin, self.proc.stdout
        self.lines: queue.Queue[str] = queue.Queue()
        self.seen: list[dict] = []
        self.reader = threading.Thread(target=lambda: [self.lines.put(x) for x in stdout])
        self.reader.start()

    def send(self, message: dict[str, object]) -> None:
        self.stdin.write(json.dumps(message) + "\n")
        self.stdin.flush()

    def receive(self, timeout: float = 10) -> dict:
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError("the server did not answer in time") from None
        message = json.loads(line)
        self.seen.append(message)
        return message

    def start_question(self) -> dict:
        params = {"protocolVersion": "2025-06-18", "capabilities": {"elicitation": {}}}
        self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
        self.receive()
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.receive()
        self.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ask"}})
        return self.receive()

    def finish(self) -> str:
        self.stdin.close()
        self.proc.wait(10)
        return self.log.read_text(encoding="utf-8")

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)
        self.reader.join(10)  # the server's stdout is at its end once the process is gone
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream is not None and not stream.closed:
                stream.close()


@pytest.fixture
def server(tmp_path: Path) -> Iterator[_Server]:
    srv = _Server(tmp_path)
    try:
        yield srv
    finally:
        srv.close()


def test_the_elicit_server_asks_and_reports_the_users_action(server: _Server) -> None:
    asked = server.start_question()
    server.send({"jsonrpc": "2.0", "id": asked["id"], "result": {"action": "cancel"}})
    server.receive()
    log = server.finish()
    seen = server.seen
    assert seen[0]["result"]["capabilities"] == {"tools": {}}
    assert [t["name"] for t in seen[1]["result"]["tools"]] == ["ask"]
    assert seen[2]["method"] == "elicitation/create"
    assert seen[2]["params"]["requestedSchema"]["type"] == "object"
    assert seen[3]["id"] == 3 and "cancel" in seen[3]["result"]["content"][0]["text"]
    assert "elicitation sent" in log and "answered cancel" in log
    assert '"elicitation": {}' in log  # the client's capabilities are recorded


def test_the_elicit_server_exits_when_the_client_goes_away_mid_question(server: _Server) -> None:
    server.start_question()
    log = server.finish()
    assert server.seen[-1]["method"] == "elicitation/create"
    assert "answered closed" in log


def test_the_elicit_server_answers_ping_outside_a_question(server: _Server) -> None:
    server.send({"jsonrpc": "2.0", "id": 7, "method": "ping"})
    assert server.receive() == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_the_elicit_server_handles_requests_while_a_question_is_pending(server: _Server) -> None:
    asked = server.start_question()
    server.send({"jsonrpc": "2.0", "id": 8, "method": "ping"})
    assert server.receive() == {"jsonrpc": "2.0", "id": 8, "result": {}}
    server.send({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
    assert [t["name"] for t in server.receive()["result"]["tools"]] == ["ask"]
    cancel = {"requestId": 3, "reason": "user pressed Esc"}
    server.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": cancel})
    server.send({"jsonrpc": "2.0", "id": asked["id"], "result": {"action": "decline"}})
    assert "decline" in server.receive()["result"]["content"][0]["text"]
    log = server.finish()
    assert "cancelled" in log and "user pressed Esc" in log
    assert "answered decline" in log


def test_the_elicit_server_refuses_a_second_tool_call_while_a_question_is_pending(
    server: _Server,
) -> None:
    asked = server.start_question()
    server.send({"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "ask"}})
    busy = server.receive()
    assert busy["id"] == 10 and "busy" in busy["error"]["message"]
    server.send({"jsonrpc": "2.0", "id": asked["id"], "result": {"action": "accept"}})
    done = server.receive()
    assert done["id"] == 3 and "accept" in done["result"]["content"][0]["text"]
    assert "answered accept" in server.finish()
