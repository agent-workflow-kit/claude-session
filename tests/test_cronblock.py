"""``cron install`` / ``cron uninstall``: tests run against a fake ``crontab`` program on a
private PATH (state in a file under ``tmp_path``), never the real crontab."""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from claude_session import cronblock
from claude_session.cli import main
from claude_session.errors import FailedError, UsageError

from .fake_gateway import FakeGateway
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


@pytest.fixture(autouse=True)
def _never_the_real_crontab(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every real gateway run in this module must be the fake program under ``tmp_path``."""
    from claude_session.gateway import SystemGateway

    real_run = SystemGateway.run

    def guarded(self: SystemGateway, argv: Any, **kwargs: Any) -> Any:
        assert str(argv[0]).startswith(str(tmp_path)), f"refusing to run {argv[0]}"
        return real_run(self, argv, **kwargs)

    monkeypatch.setattr(SystemGateway, "run", guarded)


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


def private_env_file(root: Path, name: str = "env.sh", mode: int = 0o600) -> Path:
    path = root / name
    path.write_text("export FOO=bar\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_env_file_is_sourced_before_the_command(
    env: Env, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = private_env_file(tmp_path)
    code, out, _ = install(capsys, "--env-file", str(path))
    assert code == 0 and out["env_file"] == os.path.realpath(path)
    assert f". {path} && PATH=" in block_lines(out)[1]


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
    directory = tmp_path / "it's $(touch pwned); `id` 50% & #x"
    directory.mkdir()
    path = private_env_file(directory)
    code, out, _ = install(capsys, "--env-file", str(path))
    assert code == 0
    cron_line = block_lines(out)[1]
    # cron turns an unescaped % into a newline: every % is escaped
    assert "%" in cron_line
    assert cron_line.replace("\\%", "").count("%") == 0
    # undo cron's escaping, then the shell must see the path as one word
    import shlex

    command = cron_line.split(" ", 5)[5].replace("\\%", "%")
    sh, flag, script = shlex.split(command)
    assert (sh, flag) == ("/bin/sh", "-c")
    words = shlex.split(script)
    assert words[:3] == [".", str(path), "&&"]
    assert not any("\n" in line for line in cron_line.split("\n"))
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
    import shlex

    command = block_lines(out)[1].split(" ", 5)[5].replace("\\%", "%")
    script = shlex.split(command)[2]
    assert (
        shlex.split(script)[0]
        == "PATH=/tmp/my home/o'brien/.local/bin:/usr/local/bin:/usr/bin:/bin"
    )


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
        "# claude-session managed  \n*/1 * * * * old\n# claude-session managed end\r\n",
        encoding="utf-8",
    )
    code, out, _ = install(capsys)
    assert out["action"] == "updated" and env.crontab() == out["block"] + "\n"


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
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(env.bin)  # a cwd-relative search would find ./crontab here
    crontab = cronblock.Crontab(FakeGateway(), {"PATH": f":.:bin:{env.bin.name}:crontab"})
    assert not crontab.program.startswith(str(env.bin))


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
        cronblock.uninstall(fake, environ, dry_run=False)
    fake2, environ, program = scripted(tmp_path)
    fake2.on([program, "-l"], error=PermissionError(13, "Permission denied"))
    with pytest.raises(FailedError, match="cannot run crontab"):
        cronblock.uninstall(fake2, environ, dry_run=False)


def test_crontab_is_called_with_absolute_path_and_the_text_on_stdin(tmp_path: Path) -> None:
    fake, environ, program = scripted(tmp_path)
    fake.on([program, "-l"], stdout=USER_LINES)
    fake.on([program, "-"])
    result = cronblock.install(
        fake, environ, interval=7, env_file=None, dry_run=True, python="/p/py"
    )
    assert result["action"] == "installed"
    assert [c.argv for c in fake.calls] == [(program, "-l")]


def test_python_path_with_a_newline_is_refused(tmp_path: Path) -> None:
    fake, environ, program = scripted(tmp_path)
    fake.on([program, "-l"], stdout="")
    with pytest.raises(FailedError):
        cronblock.install(fake, environ, interval=None, env_file=None, dry_run=True, python="/p\ny")


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
