"""Argv values that are not valid UTF-8 are refused cleanly (issue #72).

Python decodes argv with the filesystem encoding and ``surrogateescape``: under UTF-8 an
undecodable byte arrives as a lone surrogate (``U+DC80..U+DCFF``), which cannot be encoded to
UTF-8 for the store, the logs, the audit records, JSON, sqlite or the terminal. Every command
refuses such a token before anything is allocated: exit 2 ``usage``, a schema-valid error
envelope with ``--json``, no traceback, nothing written to the state directory, no process
started.

The scan is one code path, so the per-flag and hostile-value cases run in-process through
``cli.main`` with ``os.fsdecode``d tokens; subprocesses are kept for real OS argv decoding,
``--help``/``--version``, the outcome file, the process cwd and ``hook-event``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_session import cli, fsutil, hookevent
from claude_session.errors import UsageError
from claude_session.hookevent import HookEventError

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


def state_root() -> Path:
    """This test's private state directory (``XDG_STATE_HOME``, from conftest)."""
    return Path(os.environ["XDG_STATE_HOME"]) / "claude-session"


def refuse_in_process(argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    """Run ``cli.main(argv)`` and assert a clean ``--json`` usage refusal; returns the
    message. Nothing may be created in the state directory."""
    assert cli.main(argv) == 2
    out, err = capsys.readouterr()
    assert "Traceback" not in err
    assert "not valid UTF-8" in err
    envelope = json.loads(out)
    validate(envelope, "claude-session/cli/error.json")
    assert envelope["error"]["code"] == "usage"
    message: str = envelope["error"]["message"]
    assert "not valid UTF-8" in message
    assert fsutil.is_utf8(message)
    assert not state_root().parent.exists()
    return message


@pytest.mark.parametrize(("command", "shape"), CASES)
def test_every_flag_and_positional_refuses_undecodable_bytes(
    command: list[str], shape: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    tail = [part.format(ONE_BAD) for part in shape]
    refuse_in_process([*command, *tail, "--json"], capsys)


HOSTILE_PREFIXES = [
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
]


@pytest.mark.parametrize("raw", BAD_VALUES)
@pytest.mark.parametrize("prefix", HOSTILE_PREFIXES, ids=lambda p: " ".join(p))
def test_hostile_values(prefix: list[str], raw: bytes, capsys: pytest.CaptureFixture[str]) -> None:
    refuse_in_process([*prefix, os.fsdecode(raw)], capsys)


@pytest.mark.parametrize(
    ("raw", "prefix"),
    [
        pytest.param(value.values[0], HOSTILE_PREFIXES[i % len(HOSTILE_PREFIXES)], id=value.id)
        for i, value in enumerate(BAD_VALUES)
    ],
)
def test_hostile_bytes_from_the_os(sandbox: Sandbox, raw: bytes, prefix: list[str]) -> None:
    """Real argv bytes, decoded by the interpreter (one prefix per value)."""
    proc = sandbox.run([*prefix, os.fsdecode(raw)])
    assert_clean_refusal(proc, json_mode=True)
    sandbox.assert_untouched()


def test_text_mode_prints_no_envelope(sandbox: Sandbox) -> None:
    proc = sandbox.run(["start", "--model", ONE_BAD])
    assert_clean_refusal(proc, json_mode=False)
    sandbox.assert_untouched()


def test_a_bad_token_among_good_ones_names_its_position_not_its_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    message = refuse_in_process(["exec", "--json", "--model", "m", "--actor", ONE_BAD], capsys)
    assert message == "argument 6 (--actor) is not valid UTF-8"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(
            ["exec", "--json", "--model", ONE_BAD], "argument 4 (--model)", id="value-flag"
        ),
        pytest.param(
            ["exec", "--json", f"--model={ONE_BAD}"], "argument 3 (--model)", id="value-flag="
        ),
        pytest.param(["list", "--json", ONE_BAD], "argument 3", id="after-boolean-flag"),
        pytest.param(
            ["label", "add", "--json", "cs-aaaaaaaa", "--", ONE_BAD],
            "argument 6",
            id="after-double-dash",
        ),
        pytest.param(
            ["exec", "--json", "--", "--model", ONE_BAD],
            "argument 5",
            id="value-flag-after-double-dash",
        ),
        pytest.param(
            ["exec", "--json", "--model=abc", ONE_BAD], "argument 4", id="after-flag=value"
        ),
        pytest.param(["exec", "--json", "--help", ONE_BAD], "argument 4", id="after-help"),
        pytest.param(["exec", "--json", "--version", ONE_BAD], "argument 4", id="after-version"),
        pytest.param(
            ["report", "--json", "--summary", "--not a flag", ONE_BAD],
            "argument 5",
            id="after-a-value-spelled-like-a-flag",
        ),
        pytest.param(["exec", "--json", f"--json={ONE_BAD}"], "argument 3", id="boolean-flag="),
        pytest.param(["exec", "--json", f"--unknown={ONE_BAD}"], "argument 3", id="unknown-flag="),
    ],
)
def test_the_flag_label_names_only_a_known_value_taking_option(
    argv: list[str], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """``(<flag>)`` is a known value-taking option string, never another token's text."""
    assert refuse_in_process(argv, capsys) == f"{expected} is not valid UTF-8"


def test_the_command_word_and_an_unknown_command_are_refused_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for argv in ([ONE_BAD, "--json"], ["--json", ONE_BAD]):
        refuse_in_process(argv, capsys)
    assert cli.main([ONE_BAD]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "argument 1 is not valid UTF-8" in err


def test_a_token_after_double_dash_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    refuse_in_process(["label", "add", "--json", "cs-aaaaaaaa", "--", ONE_BAD], capsys)


def test_help_and_version_do_not_excuse_a_bad_token(sandbox: Sandbox) -> None:
    for extra in ("--help", "--version"):
        proc = sandbox.run(["exec", extra, "--model", ONE_BAD])
        assert_clean_refusal(proc, json_mode=False)
    sandbox.assert_untouched()


def test_the_check_precedes_the_schema_check(capsys: pytest.CaptureFixture[str]) -> None:
    """A newer state.db would be `schema_newer` (12); the argv refusal comes first and the
    database is neither opened nor migrated."""
    state = state_root()
    state.mkdir(parents=True)
    db = state / "state.db"
    db.write_bytes(b"not a database")
    before = db.read_bytes()
    assert cli.main(["list", "--json", "--kind", ONE_BAD]) == 2
    out, _ = capsys.readouterr()
    assert json.loads(out)["error"]["code"] == "usage"
    assert db.read_bytes() == before
    assert sorted(p.name for p in state.iterdir()) == ["state.db"]


def test_valid_unicode_is_not_refused_as_utf8(sandbox: Sandbox) -> None:
    proc = sandbox.run(["list", "--json", "--kind", "café-☃"])
    assert b"not valid UTF-8" not in proc.stderr + proc.stdout
    assert b"Traceback" not in proc.stderr


def test_the_check_encodes_with_the_filesystem_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under a non-UTF-8 locale (UTF-8 mode off) a byte such as 0xe9 arrives decoded, as
    ``é``: the check re-encodes each token with the filesystem encoding and decodes it as
    strict UTF-8, so such a token is refused too."""
    assert fsutil.is_utf8("café-☃")
    assert not fsutil.is_utf8(ONE_BAD)
    assert not fsutil.is_utf8("\ud800")
    with monkeypatch.context() as m:
        m.setattr(os, "fsencode", lambda text: text.encode("latin-1"))
        latin1_e = fsutil.is_utf8("é")  # b"\xe9": not UTF-8
        latin1_snowman = fsutil.is_utf8("☃")  # not encodable at all
        latin1_ascii = fsutil.is_utf8("plain")
        with pytest.raises(UsageError, match=r"^argument 3 \(--kind\) is not valid UTF-8$"):
            cli.check_argv_utf8(["list", "--kind", "é"])
        with pytest.raises(HookEventError):
            hookevent.parse_argv(["--state-dir", "/x/sd-é", "--run", "1", "Stop"])
    assert (latin1_e, latin1_snowman, latin1_ascii) == (False, False, True)


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


def test_no_outcome_file_when_its_own_path_is_undecodable(sandbox: Sandbox) -> None:
    elsewhere = sandbox.root / "elsewhere"
    elsewhere.mkdir()
    out = sandbox.root / "out"
    out.mkdir()
    proc = sandbox.run(["exec", "--json", "--outcome-file", str(out / ONE_BAD)], cwd=elsewhere)
    assert_clean_refusal(proc, json_mode=True)
    assert list(out.iterdir()) == []
    sandbox.assert_untouched()


def test_an_undecodable_cwd_still_gets_its_envelope_in_the_outcome_file(
    sandbox: Sandbox,
) -> None:
    elsewhere = sandbox.root / "elsewhere"
    elsewhere.mkdir()
    out = sandbox.root / "out"
    out.mkdir()
    good = out / "good.json"
    proc = sandbox.run(
        ["exec", "--json", "--outcome-file", str(good), "--cwd", ONE_BAD], cwd=elsewhere
    )
    assert_clean_refusal(proc, json_mode=True)
    written = json.loads(good.read_text(encoding="utf-8"))
    validate(written, "claude-session/cli/error.json")
    assert written["error"]["code"] == "usage"
    assert list(elsewhere.iterdir()) == []
    sandbox.assert_untouched()


def test_a_relative_outcome_file_with_an_undecodable_cwd(sandbox: Sandbox) -> None:
    """A relative path resolves against the process's cwd (never ``--cwd``), which an
    envelope write before parsing refuses (fail closed, as with a valid ``--cwd``): a
    warning, nothing written anywhere, no crash."""
    elsewhere = sandbox.root / "elsewhere"
    elsewhere.mkdir()
    out = sandbox.root / "out"
    out.mkdir()
    proc = sandbox.run(
        ["exec", "--json", "--outcome-file", "../out/rel.json", "--cwd", ONE_BAD], cwd=elsewhere
    )
    assert_clean_refusal(proc, json_mode=True)
    assert b"warning: --outcome-file not written" in proc.stderr
    assert list(elsewhere.iterdir()) == []
    assert list(out.iterdir()) == []
    sandbox.assert_untouched()


def test_a_cwd_that_is_not_utf8_is_not_an_argv_refusal(sandbox: Sandbox) -> None:
    """The process's own working directory is not argv: the argv check never looks at it,
    and the launch fails `unsafe_cwd` (exit 12) instead."""
    cwd = sandbox.root / os.fsdecode(b"dir\xff")
    cwd.mkdir()
    proc = sandbox.run(["start", "--json", "--model", "m", "--skill", "x"], cwd=cwd)
    assert b"Traceback" not in proc.stderr
    assert proc.returncode == 12, proc.stderr
    error = json.loads(proc.stdout)["error"]
    assert (error["code"], error["end_reason"]) == ("launch_failed", "unsafe_cwd")
    assert not error["message"].startswith("argument")
    assert list(sandbox.started.iterdir()) == []


SID = "cs-aaaaaaaa"


def test_hook_event_with_undecodable_argv_exits_0_silently(sandbox: Sandbox) -> None:
    sandbox.env["CSESSION_ID"] = SID
    proc = sandbox.run(["hook-event", "--state-dir", str(sandbox.state), "Stop", ONE_BAD])
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")
    bad_state = sandbox.root / ONE_BAD
    bad_state.mkdir()
    proc = sandbox.run(["hook-event", "--state-dir", str(bad_state), "--run", "1", "Stop"])
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")
    assert list(bad_state.iterdir()) == []


def test_hook_event_refuses_an_undecodable_state_dir() -> None:
    with pytest.raises(HookEventError, match="--state-dir is not valid UTF-8"):
        hookevent.parse_argv(["--state-dir", f"/x/{ONE_BAD}", "--run", "1", "Stop"])


def test_in_process_main_refuses_a_lone_surrogate(capsys: pytest.CaptureFixture[str]) -> None:
    """A surrogate that did not come from bytes (no round trip to the OS) is refused alike."""
    refuse_in_process(["exec", "--json", "--model", "\ud800"], capsys)
