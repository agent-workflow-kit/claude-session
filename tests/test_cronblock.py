"""``cron install`` / ``cron uninstall``: tests run against a fake ``crontab`` program on a
private PATH (state in a file under ``tmp_path``) or a scripted gateway, never the real
crontab (``conftest.crontab_guard`` fails any test that tries)."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from claude_session import cronblock
from claude_session.cli import main
from claude_session.errors import FailedError, UsageError
from claude_session.gateway import Result

from .fake_gateway import FakeGateway, Response
from .schemas import validate

FAKE_CRONTAB = textwrap.dedent(
    f"""\
    #!{sys.executable}
    import os, sys
    state = os.environ["FAKE_CRONTAB_FILE"]
    with open(os.environ["FAKE_CRONTAB_LOG"], "a") as log:
        log.write(" ".join(sys.argv[1:]) + "\\n")
    fail = os.environ.get("FAKE_CRONTAB_FAIL", "")
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if fail == "all" or (fail == "list" and arg == "-l") or (fail == "write" and arg != "-l"):
        sys.stderr.write("crontab: simulated failure\\n")
        sys.exit(4)
    if arg == "-l":
        if not os.path.exists(state):
            sys.stderr.write("no crontab for tester\\n")
            sys.exit(1)
        data = open(state, "rb").read()
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
        race = os.environ.get("FAKE_CRONTAB_RACE", "")
        counter = state + ".lists"
        n = int(open(counter).read()) + 1 if os.path.exists(counter) else 1
        open(counter, "w").write(str(n))
        if race and n <= int(race):
            with open(state, "ab") as f:
                f.write(b"# raced %d\\n" % n)
    elif arg == "-":
        open(state, "wb").write(sys.stdin.buffer.read())
    elif arg == "-r":
        os.remove(state)
    else:
        sys.exit(64)
    """
)

USER_LINES = "MAILTO=me@example.com\n0 3 * * * /usr/bin/backup --all # nightly\n@reboot echo hi\n"


class Env:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.bin = root / "bin"
        self.bin.mkdir()
        self.state = root / "crontab.txt"
        self.log = root / "calls.log"
        program = self.bin / "crontab"
        program.write_text(FAKE_CRONTAB, encoding="utf-8")
        program.chmod(0o755)
        monkeypatch.setenv("PATH", str(self.bin))
        monkeypatch.setenv("HOME", str(root / "home"))
        monkeypatch.setenv("FAKE_CRONTAB_FILE", str(self.state))
        monkeypatch.setenv("FAKE_CRONTAB_LOG", str(self.log))
        self.monkeypatch = monkeypatch

    def crontab(self) -> str | None:
        return self.state.read_text(encoding="utf-8") if self.state.exists() else None

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def writes(self) -> list[str]:
        return [c for c in self.calls() if c in ("-", "-r")]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


def run(
    capsys: pytest.CaptureFixture[str], *argv: str, schema: str | None = None
) -> tuple[int, Any, str]:
    code = main([*argv, "--json"])
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    if schema is not None and code == 0:
        validate(out, schema)
    return code, out, captured.err


INSTALL = ("cron", "install")
UNINSTALL = ("cron", "uninstall")
INSTALL_SCHEMA = "claude-session/cli/cron-install.json"
UNINSTALL_SCHEMA = "claude-session/cli/cron-uninstall.json"


def install(capsys: pytest.CaptureFixture[str], *flags: str) -> tuple[int, Any, str]:
    return run(capsys, *INSTALL, *flags, schema=INSTALL_SCHEMA)


def uninstall(capsys: pytest.CaptureFixture[str], *flags: str) -> tuple[int, Any, str]:
    return run(capsys, *UNINSTALL, *flags, schema=UNINSTALL_SCHEMA)


def block_lines(out: dict[str, Any]) -> list[str]:
    return out["block"].split("\n")


def cron_command(line: str) -> str:
    """The command cron runs for a crontab entry ``line`` (five time fields), after cron's
    ``%`` processing as cronie's do_command.c does it: a backslash before ``%`` escapes it
    (the backslash is dropped), ``\\\\`` is not special, and the first unescaped ``%`` ends
    the command (the rest would be its stdin; the block never has one)."""
    raw = line.split(" ", 5)[5]
    out: list[str] = []
    escaped = False
    for i, ch in enumerate(raw):
        if escaped:
            if ch == "%":
                out[-1] = "%"
            else:
                out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
        if ch == "%":
            raise AssertionError(f"unescaped % ends the command early: {raw[: i + 1]!r}")
        out.append(ch)
    return "".join(out)


def sh_words(script: str) -> list[str]:
    """``script`` as the shell's words, with each ``;`` a word of its own."""
    lexer = shlex.shlex(script, posix=True, punctuation_chars=";")
    lexer.whitespace_split = True
    return list(lexer)


def cron_script(line: str) -> str:
    """The script the entry's ``/bin/sh -c`` runs."""
    sh, flag, script = shlex.split(cron_command(line))
    assert (sh, flag) == ("/bin/sh", "-c")
    return script


# Install


def test_install_without_a_crontab_creates_the_block(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = install(capsys)
    assert code == 0
    assert out["action"] == "installed"
    assert (out["interval_minutes"], out["env_file"], out["dry_run"]) == (10, None, False)
    lines = block_lines(out)
    assert lines[0] == "# claude-session managed"
    assert lines[-1] == "# claude-session managed end"
    assert lines[1].startswith("*/10 * * * * /bin/sh -c ")
    assert "reconcile" in lines[1]
    assert "/.local/bin" in lines[1]
    assert env.crontab() == out["block"] + "\n"


def test_install_keeps_every_other_entry_byte_for_byte(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.state.write_text(USER_LINES, encoding="utf-8")
    code, out, _ = install(capsys, "--interval", "5")
    assert code == 0 and out["action"] == "installed" and out["interval_minutes"] == 5
    assert env.crontab() == USER_LINES + out["block"] + "\n"


def test_install_adds_the_newline_a_crontab_lacks(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.state.write_text("0 3 * * * x", encoding="utf-8")  # no final newline
    code, out, _ = install(capsys)
    assert code == 0
    assert env.crontab() == "0 3 * * * x\n" + out["block"] + "\n"


def test_install_twice_is_unchanged_and_does_not_write(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.state.write_text(USER_LINES, encoding="utf-8")
    install(capsys)
    before = env.crontab()
    writes = len(env.writes())
    code, out, _ = install(capsys)
    assert (code, out["action"]) == (0, "unchanged")
    assert env.crontab() == before
    assert len(env.writes()) == writes


def test_install_with_another_interval_updates_in_place(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.state.write_text("# top\n", encoding="utf-8")
    install(capsys)
    with env.state.open("a", encoding="utf-8") as f:
        f.write("5 5 * * * after-block\n")
    code, out, _ = install(capsys, "--interval", "59")
    assert (code, out["action"]) == (0, "updated")
    assert out["interval_minutes"] == 59
    assert env.crontab() == "# top\n" + out["block"] + "\n5 5 * * * after-block\n"
    assert (env.crontab() or "").count("# claude-session managed\n") == 1


@pytest.mark.parametrize("interval", ["0", "60", "-1", "1000"])
def test_install_interval_out_of_range_is_a_usage_error_and_touches_nothing(
    env: Env, capsys: pytest.CaptureFixture[str], interval: str
) -> None:
    code, out, _ = run(capsys, *INSTALL, "--interval", interval)
    assert code == 2 and out["error"]["code"] == "usage"
    assert env.calls() == []


@pytest.mark.parametrize("interval", ["1", "59"])
def test_install_interval_boundaries(
    env: Env, capsys: pytest.CaptureFixture[str], interval: str
) -> None:
    code, out, _ = install(capsys, "--interval", interval)
    assert code == 0 and f"*/{interval} * * * * " in out["block"]


def test_install_dry_run_reports_the_block_and_writes_nothing(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.state.write_text(USER_LINES, encoding="utf-8")
    code, out, _ = install(capsys, "--dry-run")
    assert (code, out["action"], out["dry_run"]) == (0, "installed", True)
    assert env.crontab() == USER_LINES
    assert env.writes() == []
    install(capsys)
    code, out, _ = install(capsys, "--dry-run", "--interval", "3")
    assert (out["action"], out["dry_run"]) == ("updated", True)
    assert "*/3 " in out["block"] and "*/10 " in (env.crontab() or "")
    code, out, _ = install(capsys, "--dry-run")
    assert out["action"] == "unchanged"


def test_install_human_output(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cron", "install"]) == 0
    text = capsys.readouterr().out
    assert text.startswith("crontab block installed\n# claude-session managed\n")


# Command line contents


def test_block_command_runs_this_interpreter_with_an_explicit_path(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = install(capsys)
    line = block_lines(out)[1]
    assert code == 0
    assert sys.executable in line
    assert "PATH=" in line  # set explicitly, not inherited from cron
    assert os.path.join(os.environ["HOME"], ".local/bin") in line


def test_block_runs_the_interpreter_with_e_and_p_but_not_i(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    # -I would drop the user site, where a pip install --user puts claude-session.
    code, out, _ = install(capsys)
    assert code == 0
    words = sh_words(cron_script(block_lines(out)[1]))
    at = words.index(sys.executable)
    assert words[at - 1] == "exec"
    assert words[at + 1 :] == ["-E", "-P", "-m", "claude_session", "reconcile", ">/dev/null"]
    assert "-I" not in words


def private_env_file(root: Path, name: str = "env.sh", mode: int = 0o600) -> Path:
    path = root / name
    path.write_text("export FOO=bar\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_env_file_is_sourced_after_path_and_before_the_command(
    env: Env, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = private_env_file(tmp_path)
    code, out, _ = install(capsys, "--env-file", str(path))
    assert code == 0 and out["env_file"] == os.path.realpath(path)
    script = cron_script(block_lines(out)[1])
    assert script.startswith("PATH=")
    assert f"; export PATH; . {path}; exec " in script
    assert "&&" not in script


def test_env_file_symlink_is_resolved_to_its_target(
    env: Env, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    target = private_env_file(tmp_path, "real.sh")
    link = tmp_path / "link.sh"
    link.symlink_to(target)
    code, out, _ = install(capsys, "--env-file", str(link))
    assert code == 0 and out["env_file"] == os.path.realpath(target)


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666, 0o660])
def test_env_file_writable_by_others_is_refused(
    env: Env, capsys: pytest.CaptureFixture[str], tmp_path: Path, mode: int
) -> None:
    path = private_env_file(tmp_path, mode=mode)
    code, out, _ = run(capsys, *INSTALL, "--env-file", str(path))
    assert code == 2 and out["error"]["code"] == "usage"
    assert "writable" in out["error"]["message"]
    assert env.calls() == []


def test_env_file_readable_by_others_is_accepted(
    env: Env, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = private_env_file(tmp_path, mode=0o644)
    assert install(capsys, "--env-file", str(path))[0] == 0


def test_env_file_missing_directory_or_not_a_regular_file_is_refused(
    env: Env, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    for bad in (tmp_path / "missing", tmp_path, Path("/dev/null")):
        code, out, _ = run(capsys, *INSTALL, "--env-file", str(bad))
        assert code == 2 and out["error"]["code"] == "usage", bad
    assert env.calls() == []


def test_env_file_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = private_env_file(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(UsageError, match="not owned"):
        cronblock.check_env_file(str(path))


def test_env_file_checked_also_with_dry_run(
    env: Env, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = private_env_file(tmp_path, mode=0o666)
    code, _, _ = run(capsys, *INSTALL, "--env-file", str(path), "--dry-run")
    assert code == 2


def test_relative_env_file_is_made_absolute(
    env: Env,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_env_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    code, out, _ = install(capsys, "--env-file", "env.sh")
    assert code == 0 and out["env_file"] == os.path.realpath(tmp_path / "env.sh")


def test_hostile_env_file_names_are_quoted_for_the_shell_and_for_cron(
    env: Env, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    directory = tmp_path / "it's $(touch pwned); `id` 50% & #x %%"
    directory.mkdir(mode=0o700)
    path = private_env_file(directory)
    code, out, _ = install(capsys, "--env-file", str(path))
    assert code == 0
    cron_line = block_lines(out)[1]
    # cron's own % processing (no unescaped %), then the shell must see the path as one word
    assert "%" in cron_line
    words = sh_words(cron_script(cron_line))
    assert words[5:8] == [".", str(path), ";"]
    assert len(block_lines(out)) == 3


@pytest.mark.parametrize("name", ["a\nb.sh", "a\rb.sh"])
def test_env_file_with_a_newline_is_refused(
    env: Env, capsys: pytest.CaptureFixture[str], tmp_path: Path, name: str
) -> None:
    path = private_env_file(tmp_path, name)
    code, out, _ = run(capsys, *INSTALL, "--env-file", str(path))
    assert code == 2 and out["error"]["code"] == "usage"
    assert env.calls() == []


def test_env_file_unicode_name(
    env: Env, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = private_env_file(tmp_path, "env-ünï-日本.sh")
    code, out, _ = install(capsys, "--env-file", str(path))
    assert code == 0 and "env-ünï-日本.sh" in out["block"]


def test_hostile_home_is_refused(
    env: Env, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/tmp/a\nb")
    code, out, _ = run(capsys, *INSTALL)
    assert code == 12 and out["error"]["code"] == "failed"
    assert env.writes() == []


def test_home_with_spaces_and_quotes_is_quoted(
    env: Env, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/tmp/my home/o'brien")
    code, out, _ = install(capsys)
    assert code == 0
    words = sh_words(cron_script(block_lines(out)[1]))
    assert words[:5] == [
        "PATH=/tmp/my home/o'brien/.local/bin:/usr/local/bin:/usr/bin:/bin",
        ";",
        "export",
        "PATH",
        ";",
    ]


# Uninstall


def test_uninstall_removes_only_the_block(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.state.write_text(USER_LINES, encoding="utf-8")
    _, installed, _ = install(capsys)
    with env.state.open("a", encoding="utf-8") as f:
        f.write("1 1 * * * tail\n")
    code, out, _ = uninstall(capsys)
    assert (code, out["action"], out["dry_run"]) == (0, "removed", False)
    assert out["block"] == installed["block"]
    assert env.crontab() == USER_LINES + "1 1 * * * tail\n"


def test_uninstall_leaves_an_empty_crontab_removed(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    install(capsys)
    code, out, _ = uninstall(capsys)
    assert (code, out["action"]) == (0, "removed")
    assert env.crontab() is None
    assert env.writes()[-1] == "-r"


def test_uninstall_twice_is_unchanged(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.state.write_text(USER_LINES, encoding="utf-8")
    install(capsys)
    uninstall(capsys)
    writes = len(env.writes())
    code, out, _ = uninstall(capsys)
    assert (code, out["action"], out["block"]) == (0, "unchanged", None)
    assert env.crontab() == USER_LINES
    assert len(env.writes()) == writes


def test_uninstall_without_a_crontab_is_unchanged(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = uninstall(capsys)
    assert (code, out["action"], out["block"]) == (0, "unchanged", None)
    assert env.writes() == []


def test_uninstall_dry_run_changes_nothing(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    install(capsys)
    before = env.crontab()
    code, out, _ = uninstall(capsys, "--dry-run")
    assert (code, out["action"], out["dry_run"]) == (0, "removed", True)
    assert out["block"] is not None
    assert env.crontab() == before


def test_uninstall_leaves_look_alike_user_lines(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    mine = "# claude-session managed by hand\n# my claude-session managed\n"
    env.state.write_text(mine, encoding="utf-8")
    code, out, _ = uninstall(capsys)
    assert out["action"] == "unchanged" and env.crontab() == mine


def test_uninstall_human_output(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cron", "uninstall"]) == 0
    assert capsys.readouterr().out == "crontab block unchanged\n"


# Malformed or hostile crontabs


@pytest.mark.parametrize(
    "text",
    [
        "# claude-session managed\n*/10 * * * * x\n",  # no end
        "*/10 * * * * x\n# claude-session managed end\n",  # no start
        "# claude-session managed end\n# claude-session managed\n",  # reversed
        "# claude-session managed\n# claude-session managed\n# claude-session managed end\n",
        "# claude-session managed\n# claude-session managed end\n"
        "# claude-session managed\n# claude-session managed end\n",  # two blocks
    ],
)
@pytest.mark.parametrize("command", [INSTALL, UNINSTALL])
def test_malformed_markers_are_refused_and_nothing_is_written(
    env: Env, capsys: pytest.CaptureFixture[str], text: str, command: tuple[str, ...]
) -> None:
    env.state.write_text(text, encoding="utf-8")
    code, out, _ = run(capsys, *command)
    assert code == 12 and out["error"]["code"] == "failed"
    assert env.crontab() == text
    assert env.writes() == []


def test_crontab_that_is_not_utf8_is_refused(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.state.write_bytes(b"0 0 * * * echo \xff\xfe\n")
    code, out, _ = run(capsys, *INSTALL)
    assert code == 12 and "UTF-8" in out["error"]["message"]
    assert env.state.read_bytes() == b"0 0 * * * echo \xff\xfe\n"


def test_odd_unicode_lines_survive(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    # splitlines() would split on these; the crontab is split on newlines only
    text = "0 0 * * * echo \u2028 \x0b \x85 \x0c é 日本\n"
    env.state.write_bytes(text.encode())
    install(capsys)
    assert (env.crontab() or "").startswith(text)
    uninstall(capsys)
    assert env.crontab() == text


def test_markers_with_trailing_whitespace_still_match(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.state.write_text(
        "# claude-session managed  \n*/1 * * * * old\n# claude-session managed end\t \n",
        encoding="utf-8",
    )
    code, out, _ = install(capsys)
    assert out["action"] == "updated" and env.crontab() == out["block"] + "\n"


def test_all_crlf_crontab_with_a_block_is_updated_and_removed_in_place(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    # A crontab saved with CRLF line ends after install: the markers still count (a
    # trailing carriage return is allowed), so there is never a second block.
    _, first, _ = install(capsys)
    crlf_block = first["block"].replace("\n", "\r\n") + "\r\n"
    before = "SHELL=/bin/sh\r\n0 0 * * * mine\r\n"
    after = "1 1 * * * later\r\n"
    text = before + crlf_block + after
    env.state.write_bytes(text.encode())
    code, out, _ = install(capsys)
    assert (code, out["action"]) == (0, "updated")
    written = env.state.read_bytes().decode()
    assert written.split("\n").count(cronblock.HEADER) == 1
    assert written == before + out["block"] + "\n" + after
    env.state.write_bytes(text.encode())
    code, out, _ = uninstall(capsys)
    assert (code, out["action"]) == (0, "removed")
    assert env.state.read_bytes() == (before + after).encode()


@pytest.mark.parametrize("trail", ["\u00a0", "\u3000", "\x0b"])
def test_markers_followed_by_other_whitespace_are_not_markers(
    env: Env, capsys: pytest.CaptureFixture[str], trail: str
) -> None:
    # Only spaces, tabs and a carriage return may follow a marker. An end marker that does
    # not count leaves the block unterminated: refused, nothing deleted.
    text = f"# claude-session managed\n*/1 * * * * old\n# claude-session managed end{trail}\n"
    env.state.write_bytes(text.encode())
    code, out, _ = run(capsys, *UNINSTALL)
    assert code == 12 and "malformed" in out["error"]["message"]
    assert env.state.read_bytes() == text.encode()
    # A start marker that does not count is a foreign line: kept byte for byte.
    foreign = f"# claude-session managed{trail}\n0 0 * * * mine\n"
    env.state.write_bytes(foreign.encode())
    code, out, _ = install(capsys)
    assert (code, out["action"]) == (0, "installed")
    assert env.state.read_bytes() == (foreign + out["block"] + "\n").encode()
    code, out, _ = uninstall(capsys)
    assert (code, out["action"]) == (0, "removed")
    assert env.state.read_bytes() == foreign.encode()


def test_empty_crontab_file_installs(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.state.write_text("", encoding="utf-8")
    code, out, _ = install(capsys)
    assert (code, out["action"]) == (0, "installed")


# Failing or missing crontab


@pytest.mark.parametrize("command", [INSTALL, UNINSTALL])
def test_no_crontab_command_exits_12(
    env: Env,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: tuple[str, ...],
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    code, out, _ = run(capsys, *command)
    assert code == 12 and out["error"]["code"] == "failed"


def test_relative_and_empty_path_entries_are_not_searched(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    fake = other / "crontab"
    fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.chdir(env.bin)  # a cwd-relative search would find ./crontab here
    crontab = cronblock.Crontab(FakeGateway(), {"PATH": f":.:bin:{env.bin.name}:crontab:{other}"})
    assert crontab.program == str(fake)


def test_an_explicit_program_is_used_without_a_path_search(tmp_path: Path) -> None:
    crontab = cronblock.Crontab(FakeGateway(), {"PATH": ""}, "/opt/fake/crontab")
    assert crontab.program == "/opt/fake/crontab"


def test_unreadable_crontab_is_not_treated_as_empty(
    env: Env, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    env.state.write_text(USER_LINES, encoding="utf-8")
    monkeypatch.setenv("FAKE_CRONTAB_FAIL", "list")
    code, out, _ = run(capsys, *INSTALL)
    assert code == 12 and out["error"]["code"] == "failed"
    assert "simulated failure" in out["error"]["detail"]
    assert env.crontab() == USER_LINES
    assert env.writes() == []


def test_failing_write_exits_12_and_keeps_the_crontab(
    env: Env, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    env.state.write_text(USER_LINES, encoding="utf-8")
    monkeypatch.setenv("FAKE_CRONTAB_FAIL", "write")
    code, out, _ = run(capsys, *INSTALL)
    assert code == 12 and out["error"]["code"] == "failed"
    assert env.crontab() == USER_LINES


def test_failing_crontab_binary_on_uninstall(
    env: Env, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    install(capsys)
    monkeypatch.setenv("FAKE_CRONTAB_FAIL", "all")
    code, out, _ = run(capsys, *UNINSTALL)
    assert code == 12


def test_concurrent_edit_before_the_write_is_merged_not_lost(
    env: Env, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    env.state.write_text(USER_LINES, encoding="utf-8")
    monkeypatch.setenv("FAKE_CRONTAB_RACE", "1")  # the first read is followed by an edit
    code, out, _ = install(capsys)
    assert code == 0
    assert env.crontab() == USER_LINES + "# raced 1\n" + out["block"] + "\n"


def test_endless_concurrent_edits_give_up_without_writing(
    env: Env, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    env.state.write_text(USER_LINES, encoding="utf-8")
    monkeypatch.setenv("FAKE_CRONTAB_RACE", "100")
    code, out, _ = run(capsys, *INSTALL)
    assert code == 12 and "keeps changing" in out["error"]["message"]
    assert env.writes() == []


# Gateway-level behavior (scripted gateway)


def fake_crontab_env(tmp_path: Path) -> dict[str, str]:
    program = tmp_path / "crontab"
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    return {"PATH": str(tmp_path), "HOME": "/home/u"}


def scripted(tmp_path: Path) -> tuple[FakeGateway, dict[str, str], str]:
    environ = fake_crontab_env(tmp_path)
    return FakeGateway(), environ, str(tmp_path / "crontab")


def test_timeout_and_oserror_become_failed(tmp_path: Path) -> None:
    fake, environ, program = scripted(tmp_path)
    fake.on([program, "-l"], timeout=True)
    with pytest.raises(FailedError, match="timed out"):
        cronblock.uninstall(fake, environ, dry_run=False, program=program)
    fake2, environ, program = scripted(tmp_path)
    fake2.on([program, "-l"], error=PermissionError(13, "Permission denied"))
    with pytest.raises(FailedError, match="cannot run crontab"):
        cronblock.uninstall(fake2, environ, dry_run=False, program=program)


def test_crontab_is_called_with_absolute_path_and_the_text_on_stdin(tmp_path: Path) -> None:
    fake, environ, program = scripted(tmp_path)
    block = cronblock.build_block(interval=7, env_file=None, python="/p/py", home="/home/u")
    after = USER_LINES + block + "\n"
    fake.on([program, "-l"], responses=[Response(stdout=USER_LINES)] * 2 + [Response(stdout=after)])
    fake.on([program, "-"])
    result = cronblock.install(
        fake, environ, interval=7, env_file=None, dry_run=False, python="/p/py", program=program
    )
    assert result["action"] == "installed"
    assert [c.argv for c in fake.calls] == [
        (program, "-l"),
        (program, "-l"),
        (program, "-"),
        (program, "-l"),
    ]
    assert os.path.isabs(program)
    assert fake.calls[2].stdin == after


def test_read_back_without_the_expected_block_exits_12(tmp_path: Path) -> None:
    fake, environ, program = scripted(tmp_path)
    fake.on([program, "-l"], stdout=USER_LINES)  # the write seems to have no effect
    fake.on([program, "-"])
    with pytest.raises(FailedError, match="expected block after the write"):
        cronblock.install(
            fake, environ, interval=7, env_file=None, dry_run=False, python="/p/py", program=program
        )
    assert [c.argv[1] for c in fake.calls] == ["-l", "-l", "-", "-l"]


@pytest.mark.parametrize("python", ["/p\ny", "/p\\y", "relative/py"])
def test_unusable_python_path_is_refused(tmp_path: Path, python: str) -> None:
    fake, environ, program = scripted(tmp_path)
    fake.on([program, "-l"], stdout="")
    with pytest.raises(FailedError):
        cronblock.install(
            fake,
            environ,
            interval=None,
            env_file=None,
            dry_run=True,
            python=python,
            program=program,
        )


def test_usage_error_type_for_bad_interval(tmp_path: Path) -> None:
    fake, environ, _ = scripted(tmp_path)
    with pytest.raises(UsageError):
        cronblock.install(fake, environ, interval=0, env_file=None, dry_run=True)
    assert fake.calls == []


def test_split_block_exactness() -> None:
    parts = cronblock.split_block(
        "a\n# claude-session managed\nb\n# claude-session managed end\nc\n"
    )
    assert (parts.before, parts.block, parts.after) == (
        ["a"],
        ["# claude-session managed", "b", "# claude-session managed end"],
        ["c"],
    )
    assert cronblock.split_block("").block is None


# The job as cron runs it (/bin/sh on the generated line, a stub interpreter)


STUB = """#!/bin/sh
{{ printf '%s\\n' "$@"; printf 'PATH=%s\\n' "$PATH"; printf 'FOO=%s\\n' "${{FOO-unset}}"; }} > {out}
"""


def stub_python(root: Path) -> tuple[str, Path]:
    """A stand-in interpreter that records its argv, PATH and FOO in a file."""
    directory = root / "stub py"
    directory.mkdir(mode=0o700)
    out = directory / "out.txt"
    program = directory / "python3"
    program.write_text(STUB.format(out=shlex.quote(str(out))), encoding="utf-8")
    program.chmod(0o755)
    return str(program), out


def run_job(line: str, home: Path) -> int:
    """Run the entry's command the way cron does: ``/bin/sh -c`` after ``%`` processing, in
    cron's small environment, from HOME."""
    home.mkdir(exist_ok=True)
    environ = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "LOGNAME": "tester",
        "SHELL": "/bin/sh",
        "PATH": "/usr/bin:/bin",
    }
    done = subprocess.run(
        ["/bin/sh", "-c", cron_command(line)],
        env=environ,
        cwd=home,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return done.returncode


def job_line(tmp_path: Path, env_file: Path | None, home: str = "/home/u") -> tuple[str, Path]:
    python, out = stub_python(tmp_path)
    real = None if env_file is None else cronblock.check_env_file(str(env_file))
    block = cronblock.build_block(interval=5, env_file=real, python=python, home=home)
    return block.split("\n")[1], out


def recorded(out: Path) -> tuple[list[str], str, str]:
    lines = out.read_text(encoding="utf-8").splitlines()
    return lines[:-2], lines[-2].removeprefix("PATH="), lines[-1].removeprefix("FOO=")


def test_job_runs_the_interpreter_with_its_flags_and_path(tmp_path: Path) -> None:
    line, out = job_line(tmp_path, None)
    assert run_job(line, tmp_path / "home") == 0
    argv, path, foo = recorded(out)
    assert argv == ["-E", "-P", "-m", "claude_session", "reconcile"]
    assert path == "/home/u/.local/bin:/usr/local/bin:/usr/bin:/bin"
    assert foo == "unset"


def test_job_sources_the_env_file_and_sees_its_exported_variables(tmp_path: Path) -> None:
    line, out = job_line(tmp_path, private_env_file(tmp_path))
    assert run_job(line, tmp_path / "home") == 0
    assert recorded(out)[2] == "bar"


def test_job_runs_when_the_env_file_ends_in_a_failing_command(tmp_path: Path) -> None:
    path = private_env_file(tmp_path)
    path.write_text("export FOO=bar\nfalse\n", encoding="utf-8")
    line, out = job_line(tmp_path, path)
    assert run_job(line, tmp_path / "home") == 0
    assert recorded(out)[0][-1] == "reconcile"


def test_job_keeps_a_path_the_env_file_extends(tmp_path: Path) -> None:
    path = private_env_file(tmp_path)
    path.write_text('export PATH="/x:$PATH"\n', encoding="utf-8")
    line, out = job_line(tmp_path, path)
    assert run_job(line, tmp_path / "home") == 0
    _, seen, _ = recorded(out)
    assert seen == "/x:/home/u/.local/bin:/usr/local/bin:/usr/bin:/bin"


def test_job_does_not_run_when_the_env_file_is_gone(tmp_path: Path) -> None:
    # ``.`` is a special builtin: a missing file ends the non-interactive shell.
    path = private_env_file(tmp_path)
    line, out = job_line(tmp_path, path)
    path.unlink()
    assert run_job(line, tmp_path / "home") != 0
    assert not out.exists()


def test_job_with_a_hostile_env_file_name_and_home_sources_that_file(tmp_path: Path) -> None:
    directory = tmp_path / "it's $(touch pwned); `id` 50% & #x %% ;"
    directory.mkdir(mode=0o700)
    path = private_env_file(directory, "e%nv 'x'.sh")
    line, out = job_line(tmp_path, path, home="/tmp/my home/o'brien 100%")
    assert run_job(line, tmp_path / "home") == 0
    _, seen, foo = recorded(out)
    assert foo == "bar"
    assert seen.startswith("/tmp/my home/o'brien 100%/.local/bin:")
    assert not (tmp_path / "home" / "pwned").exists()


# Env file location (who else could replace it)


def nested_env_file(tmp_path: Path, *modes: int) -> Path:
    """An env file below directories ``tmp_path/d0/d1/...`` with ``modes`` (outermost first)."""
    directory = tmp_path
    for i, mode in enumerate(modes):
        directory = directory / f"d{i}"
        directory.mkdir()
        directory.chmod(mode)
    return private_env_file(directory)


def test_env_file_in_a_private_directory_tree_is_accepted(tmp_path: Path) -> None:
    path = nested_env_file(tmp_path, 0o755, 0o700)
    assert cronblock.check_env_file(str(path)) == os.path.realpath(path)


def test_env_file_below_a_sticky_world_writable_directory_is_accepted(tmp_path: Path) -> None:
    path = nested_env_file(tmp_path, 0o1777, 0o700)
    assert cronblock.check_env_file(str(path)) == os.path.realpath(path)


@pytest.mark.parametrize("mode", [0o1777, 0o777, 0o770, 0o1770, 0o702])
def test_env_file_in_a_group_or_world_writable_directory_is_refused(
    tmp_path: Path, mode: int
) -> None:
    # Even sticky: a file deleted from its directory can be recreated by anyone.
    path = nested_env_file(tmp_path, mode)
    with pytest.raises(UsageError, match=r"directory .*d0.* group- or world-writable"):
        cronblock.check_env_file(str(path))


@pytest.mark.parametrize("mode", [0o777, 0o770])
def test_env_file_below_a_writable_directory_without_sticky_bit_is_refused(
    tmp_path: Path, mode: int
) -> None:
    path = nested_env_file(tmp_path, mode, 0o700)
    with pytest.raises(UsageError, match=r"directory .*d0.* without the sticky bit"):
        cronblock.check_env_file(str(path))


def test_env_file_below_a_directory_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = nested_env_file(tmp_path, 0o755, 0o700)
    foreign = os.path.realpath(tmp_path / "d0")
    real_stat = os.stat

    def fake_stat(target: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        info = real_stat(target, *args, **kwargs)
        if os.fspath(target) != foreign:
            return info
        fields = list(info)
        fields[stat.ST_UID] = os.geteuid() + 1 if os.geteuid() + 1 != 0 else 2
        return os.stat_result(fields)

    monkeypatch.setattr(cronblock.os, "stat", fake_stat)
    with pytest.raises(UsageError, match=r"directory .*d0.* neither you nor root"):
        cronblock.check_env_file(str(path))


def test_env_file_not_readable_by_you_is_refused(tmp_path: Path) -> None:
    path = private_env_file(tmp_path, mode=0o200)
    with pytest.raises(UsageError, match="not readable"):
        cronblock.check_env_file(str(path))


def test_env_file_in_an_unsafe_directory_exits_2_and_touches_nothing(
    env: Env, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = nested_env_file(tmp_path, 0o777)
    code, out, _ = run(capsys, *INSTALL, "--env-file", str(path))
    assert code == 2 and out["error"]["code"] == "usage"
    assert "d0" in out["error"]["message"]
    assert env.calls() == []


# Backslashes (cron cannot write a backslash before %)


def test_env_file_with_a_backslash_is_refused(
    env: Env, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = private_env_file(tmp_path, "a\\%b.sh")
    code, out, _ = run(capsys, *INSTALL, "--env-file", str(path))
    assert code == 2 and out["error"]["code"] == "usage"
    assert env.calls() == []


def test_env_file_linked_to_a_name_with_a_backslash_is_refused(tmp_path: Path) -> None:
    target = private_env_file(tmp_path, "real\\.sh")
    link = tmp_path / "link.sh"
    link.symlink_to(target)
    with pytest.raises(UsageError, match="backslash"):
        cronblock.check_env_file(str(link))


@pytest.mark.parametrize("home", ["/tmp/a\\b", "/tmp/a\\%b"])
def test_home_with_a_backslash_is_refused(
    env: Env, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, home: str
) -> None:
    monkeypatch.setenv("HOME", home)
    code, out, _ = run(capsys, *INSTALL)
    assert code == 12 and out["error"]["code"] == "failed"
    assert env.writes() == []


def test_cron_command_follows_cron_backslash_rules() -> None:
    assert cron_command("* * * * * a\\%b") == "a%b"
    assert cron_command("* * * * * a\\b") == "a\\b"
    with pytest.raises(AssertionError, match="unescaped"):
        cron_command("* * * * * a\\\\%b")  # \\ is not special: the % ends the command
    with pytest.raises(AssertionError, match="unescaped"):
        cron_command("* * * * * a%b")


# Carriage returns and literal U+FFFD in the crontab


@pytest.mark.parametrize(
    "text",
    [
        b"# a comment\r0 0 * * * looks-live\n1 1 * * * mine\n",  # lone CR in a comment
        b"0 1 * * * a\r\n# c\r\nMAILTO=x\r\n",  # CRLF line ends
        b"0 0 * * * echo \xef\xbf\xbd\n",  # literal U+FFFD (valid UTF-8)
    ],
)
def test_foreign_lines_are_kept_byte_for_byte(
    env: Env, capsys: pytest.CaptureFixture[str], text: bytes
) -> None:
    env.state.write_bytes(text)
    code, out, _ = install(capsys)
    assert (code, out["action"]) == (0, "installed")
    assert env.state.read_bytes() == text + out["block"].encode() + b"\n"
    code, out, _ = uninstall(capsys)
    assert (code, out["action"]) == (0, "removed")
    assert env.state.read_bytes() == text


def test_a_lone_cr_does_not_split_a_line(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    # Were the CR a line break, the second half would be a stray end marker (exit 12).
    text = b"# note\r# claude-session managed end\n"
    env.state.write_bytes(text)
    code, out, _ = install(capsys)
    assert code == 0 and env.state.read_bytes() == text + out["block"].encode() + b"\n"


def test_crontab_with_invalid_utf8_is_refused_by_the_scripted_gateway(tmp_path: Path) -> None:
    fake, environ, program = scripted(tmp_path)
    fake.on([program, "-l"], raw_stdout=b"0 0 * * * \xff\n")
    with pytest.raises(FailedError, match="not valid UTF-8"):
        cronblock.uninstall(fake, environ, dry_run=False, program=program)
    assert [c.argv[1] for c in fake.calls] == ["-l"]


# HOME fallback


class _Passwd:
    def __init__(self, home: str) -> None:
        self.pw_dir = home


@pytest.mark.parametrize("home", ["", "relative/home", None])
def test_unusable_home_falls_back_to_the_passwd_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: str | None
) -> None:
    fake, environ, program = scripted(tmp_path)
    if home is None:
        del environ["HOME"]
    else:
        environ["HOME"] = home
    monkeypatch.setattr(cronblock.pwd, "getpwuid", lambda uid: _Passwd("/home/from-passwd"))
    fake.on([program, "-l"], stdout="")
    result = cronblock.install(
        fake, environ, interval=None, env_file=None, dry_run=True, python="/p/py", program=program
    )
    assert "PATH=/home/from-passwd/.local/bin:" in result["block"]


@pytest.mark.parametrize("passwd_home", ["", "rel", "/a:b", None])
def test_unusable_passwd_home_exits_12(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passwd_home: str | None
) -> None:
    fake, environ, program = scripted(tmp_path)
    environ["HOME"] = ""

    def getpwuid(uid: int) -> _Passwd:
        if passwd_home is None:
            raise KeyError(uid)
        return _Passwd(passwd_home)

    monkeypatch.setattr(cronblock.pwd, "getpwuid", getpwuid)
    with pytest.raises(FailedError, match="home directory"):
        cronblock.install(
            fake,
            environ,
            interval=None,
            env_file=None,
            dry_run=True,
            python="/p/py",
            program=program,
        )
    assert fake.calls == []


class BytelessGateway:
    """A gateway double that builds its ``Result`` without the raw bytes, as a double written
    before ``stdout_bytes`` existed would: the crontab it reports must never read as empty."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Any, **_: Any) -> Result:
        self.calls.append(tuple(argv))
        return Result(tuple(argv), 0, USER_LINES, "")  # type: ignore[call-arg]


def test_a_result_without_its_raw_bytes_cannot_be_built() -> None:
    with pytest.raises(TypeError):
        Result(("crontab", "-l"), 0, USER_LINES, "")  # type: ignore[call-arg]


def test_install_with_a_result_lacking_bytes_refuses_rather_than_overwriting(
    tmp_path: Path,
) -> None:
    gateway = BytelessGateway()
    _, environ, program = scripted(tmp_path)
    with pytest.raises(TypeError):
        cronblock.install(
            gateway,  # type: ignore[arg-type]
            environ,
            interval=7,
            env_file=None,
            dry_run=False,
            python="/p/py",
            program=program,
        )
    assert (program, "-") not in gateway.calls and (program, "-r") not in gateway.calls
