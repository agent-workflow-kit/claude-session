"""Argv values that are not valid UTF-8 are refused cleanly (issue #72).

Python decodes argv with ``surrogateescape``: an undecodable byte arrives as a lone surrogate
(``U+DC80..U+DCFF``), which cannot be encoded to UTF-8 for the store, the logs, the audit
records, JSON, sqlite or the terminal. Every command refuses such a token before anything is
read or allocated: exit 2 ``usage``, a schema-valid error envelope with ``--json``, no
traceback, nothing written to the state directory, no process started.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_session import cli

from . import fakeclaude
from .schemas import validate

BAD_VALUES = [
    pytest.param(b"\xff", id="invalid-start-byte"),
    pytest.param(b"a\xc3", id="truncated-sequence"),
    pytest.param(b"\xed\xa0\x80", id="encoded-surrogate"),
    pytest.param(b"\xf4\x90\x80\x80", id="above-max-code-point"),
    pytest.param(b"\xc0\xaf", id="overlong-encoding"),
    pytest.param(b"ok\x01\xff\x1fok", id="control-characters-around"),
    pytest.param(b"x" * 100_000 + b"\xff", id="long-then-invalid"),
    pytest.param("café ☃ ".encode() + b"\xe2\x98", id="valid-unicode-then-truncated"),
]
ONE_BAD = os.fsdecode(b"bad\xff")
STORE_FILES = ("state.db", "log.jsonl", "audit.jsonl")


def _takes_value(action: object) -> bool:
    nargs = getattr(action, "nargs", None)
    return nargs != 0 and not isinstance(action, cli._VersionAction)


def _cases() -> list[pytest.param]:  # type: ignore[valid-type]
    """Every value-taking flag and positional of every command, from the parser itself."""
    cases = []
    for name, leaf in cli.build_parser().leaves.items():
        command = name.split()
        for action in leaf._actions:
            if not _takes_value(action) or action.dest == "help":
                continue
            if action.option_strings:
                flag = action.option_strings[0]
                cases.append(pytest.param(command, [flag, "{}"], id=f"{name} {flag}"))
                cases.append(pytest.param(command, [f"{flag}={{}}"], id=f"{name} {flag}="))
            else:
                cases.append(pytest.param(command, ["{}"], id=f"{name} <{action.dest}>"))
    return cases


CASES = _cases()


class Sandbox:
    """A private state directory and a ``claude`` that records every start."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.started = root / "started"
        self.started.mkdir()
        scenario = root / "scenario.json"
        scenario.write_text("{}", encoding="utf-8")
        self.claude = fakeclaude.claude_bin(root / "bin", scenario)
        self.env = {
            **os.environ,
            "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(root / "home" / ".config"),
            "XDG_STATE_HOME": str(root / "state"),
            "FAKE_CLAUDE_STARTED_DIR": str(self.started),
            "CSESSION_CLAUDE_BIN": str(self.claude),
        }
        (root / "home").mkdir()

    @property
    def state(self) -> Path:
        return self.root / "state"

    def run(
        self, argv: list[str], *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        raw = [os.fsencode(a) for a in argv]
        return subprocess.run(
            [os.fsencode(sys.executable), b"-m", b"claude_session", *raw],
            env=self.env,
            cwd=cwd or self.root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )

    def assert_untouched(self) -> None:
        assert not self.state.exists(), list(self.state.rglob("*"))
        assert list(self.started.iterdir()) == []


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def assert_clean_refusal(proc: subprocess.CompletedProcess[bytes], *, json_mode: bool) -> None:
    assert proc.returncode == 2, proc.stderr
    assert b"Traceback" not in proc.stderr
    assert b"UnicodeEncodeError" not in proc.stderr
    stderr = proc.stderr.decode("utf-8")  # the refusal itself is valid UTF-8
    assert "not valid UTF-8" in stderr
    assert "\udcff" not in stderr
    if json_mode:
        envelope = json.loads(proc.stdout.decode("utf-8"))
        validate(envelope, "claude-session/cli/error.json")
        assert envelope["error"]["code"] == "usage"
        assert "not valid UTF-8" in envelope["error"]["message"]
    else:
        assert proc.stdout == b""


@pytest.mark.parametrize(("command", "shape"), CASES)
def test_every_flag_and_positional_refuses_undecodable_bytes(
    sandbox: Sandbox, command: list[str], shape: list[str]
) -> None:
    tail = [part.format(ONE_BAD) for part in shape]
    proc = sandbox.run([*command, *tail, "--json"])
    assert_clean_refusal(proc, json_mode=True)
    sandbox.assert_untouched()


def test_text_mode_prints_no_envelope(sandbox: Sandbox) -> None:
    proc = sandbox.run(["start", "--model", ONE_BAD])
    assert_clean_refusal(proc, json_mode=False)
    sandbox.assert_untouched()


@pytest.mark.parametrize("raw", BAD_VALUES)
@pytest.mark.parametrize(
    "prefix",
    [
        ["start", "--json", "--model"],
        ["exec", "--json", "--actor"],
        ["exec", "--json", "--hook"],
        ["exec", "--json", "--decision-id"],
        ["exec", "--json", "--label"],
        ["exec", "--json", "--cwd"],
        ["exec", "--json", "--prompt-file"],
        ["resume", "--json"],
        ["wait", "--json"],
        ["list", "--json", "--kind"],
        ["forget", "--json"],
    ],
    ids=lambda p: " ".join(p),
)
def test_hostile_values(sandbox: Sandbox, prefix: list[str], raw: bytes) -> None:
    proc = sandbox.run([*prefix, os.fsdecode(raw)])
    assert_clean_refusal(proc, json_mode=True)
    sandbox.assert_untouched()


def test_a_bad_token_among_good_ones_names_its_position_not_its_value(sandbox: Sandbox) -> None:
    proc = sandbox.run(["exec", "--json", "--model", "m", "--actor", ONE_BAD])
    assert_clean_refusal(proc, json_mode=True)
    message = json.loads(proc.stdout)["error"]["message"]
    assert "argument 6" in message
    assert "--actor" in message
    assert "bad" not in message


def test_the_command_word_and_an_unknown_command_are_refused_too(sandbox: Sandbox) -> None:
    for argv in ([ONE_BAD], [ONE_BAD, "--json"], ["--json", ONE_BAD]):
        assert_clean_refusal(sandbox.run(argv), json_mode="--json" in argv)
    sandbox.assert_untouched()


def test_a_token_after_double_dash_is_refused(sandbox: Sandbox) -> None:
    proc = sandbox.run(["label", "add", "--json", "cs-aaaaaaaa", "--", ONE_BAD])
    assert_clean_refusal(proc, json_mode=True)
    sandbox.assert_untouched()


def test_help_and_version_do_not_excuse_a_bad_token(sandbox: Sandbox) -> None:
    for extra in ("--help", "--version"):
        proc = sandbox.run(["exec", extra, "--model", ONE_BAD])
        assert_clean_refusal(proc, json_mode=False)
    sandbox.assert_untouched()


def test_the_check_precedes_the_schema_check(sandbox: Sandbox) -> None:
    """A newer state.db would be `schema_newer` (12); the argv refusal comes first and the
    database is neither opened nor migrated."""
    sandbox.state.mkdir(parents=True)
    db = sandbox.state / "state.db"
    db.write_bytes(b"not a database")
    before = db.read_bytes()
    proc = sandbox.run(["list", "--json", "--kind", ONE_BAD])
    assert_clean_refusal(proc, json_mode=True)
    assert db.read_bytes() == before
    assert sorted(p.name for p in sandbox.state.iterdir()) == ["state.db"]


def test_valid_unicode_is_not_refused_as_utf8(sandbox: Sandbox) -> None:
    proc = sandbox.run(["list", "--json", "--kind", "café-☃"])
    assert b"not valid UTF-8" not in proc.stderr + proc.stdout
    assert b"Traceback" not in proc.stderr


def test_a_usage_error_writes_its_envelope_to_a_valid_outcome_file(sandbox: Sandbox) -> None:
    out = sandbox.root / "out" / "outcome.json"
    out.parent.mkdir()
    elsewhere = sandbox.root / "elsewhere"  # the cwd itself is never written to
    elsewhere.mkdir()
    argv = ["exec", "--json", "--outcome-file", str(out), "--model", ONE_BAD]
    proc = sandbox.run(argv, cwd=elsewhere)
    assert_clean_refusal(proc, json_mode=True)
    written = json.loads(out.read_text(encoding="utf-8"))
    validate(written, "claude-session/cli/error.json")
    assert written["error"]["code"] == "usage"
    assert list(sandbox.started.iterdir()) == []
    assert not sandbox.state.exists()


@pytest.mark.parametrize("flag", ["--outcome-file", "--cwd"])
def test_no_outcome_file_when_its_own_path_or_cwd_is_undecodable(
    sandbox: Sandbox, flag: str
) -> None:
    elsewhere = sandbox.root / "elsewhere"
    elsewhere.mkdir()
    out = sandbox.root / "out"
    out.mkdir()
    good = out / "good.json"
    if flag == "--outcome-file":
        argv = ["exec", "--json", flag, str(out / ONE_BAD)]
    else:
        argv = ["exec", "--json", "--outcome-file", str(good), flag, ONE_BAD]
    proc = sandbox.run(argv, cwd=elsewhere)
    assert_clean_refusal(proc, json_mode=True)
    assert list(out.iterdir()) == []
    sandbox.assert_untouched()


def test_a_cwd_that_is_not_utf8_is_not_an_argv_refusal(sandbox: Sandbox) -> None:
    """The process's own working directory is not argv: this check never looks at it."""
    cwd = sandbox.root / os.fsdecode(b"dir\xff")
    cwd.mkdir()
    proc = sandbox.run(["kinds", "--json"], cwd=cwd)
    assert b"not valid UTF-8" not in proc.stderr
    assert b"Traceback" not in proc.stderr


def test_hook_event_with_undecodable_argv_exits_0_silently(sandbox: Sandbox) -> None:
    proc = sandbox.run(["hook-event", "--state-dir", str(sandbox.state), "Stop", ONE_BAD])
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")
    proc = sandbox.run(["hook-event", "--state-dir", str(sandbox.root / ONE_BAD), "Stop"])
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")


def test_in_process_main_refuses_a_lone_surrogate(capsys: pytest.CaptureFixture[str]) -> None:
    """A surrogate that did not come from bytes (no round trip to the OS) is refused alike."""
    assert cli.main(["exec", "--json", "--model", "\ud800"]) == 2
    out, err = capsys.readouterr()
    validate(json.loads(out), "claude-session/cli/error.json")
    assert "Traceback" not in err
    assert "not valid UTF-8" in err
