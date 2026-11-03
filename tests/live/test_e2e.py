"""End to end with real Claude Code through `claude-session` itself: headless implement-like
run with an artifact, a 10 MiB artifact (#26), symlink and hard-link artifacts, interactive
run with auto-exit, a respond-like fork into another directory, and (where a sandbox exists) a
review run under the generated isolated settings (#59 item 8, E1, private-dir writes).

Every run uses the private environment of `LiveEnv`: the state directory is under its private
HOME, the temporary-directory root is the per-test one from `conftest.private_tmp_root`, and
`claude_auth_env` names the credential variable in use.

Cost: every run passes the spend gate first and is charged the cost `exec` reports (the
estimate when it reports none). Headless runs of the trusted `implement` kind get
`--max-budget-usd` through the kind's `claude_args`; isolated kinds (review) accept only
`--verbose`/`--debug` there, and interactive runs cannot take the flag, so those runs have no
per-run cap (the session cap still applies). The store and `log.jsonl` are read directly
(read-only), since `status` is not implemented yet."""

from __future__ import annotations

import json
import sqlite3
import stat
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from claude_session import gateway, pathrules

from .harness import (
    LiveEnv,
    find_transcripts,
    sandbox_available,
    tool_path,
    transcript_text,
)
from .tmux_driver import Tmux

CLAUDE_SESSION = Path(sys.executable).parent / "claude-session"
# The `claude-session` entry point under this test's temporary-directory root (a subprocess does
# not inherit the in-process patch); `sys.argv[0]` stays the real executable, whose directory
# `exec` puts on the agent's PATH.
ENTRY = """\
import sys
from claude_session import pathrules
pathrules.TMP_ROOT = {tmp_root!r}
sys.argv[0] = {exe!r}
from claude_session.__main__ import main
sys.exit(main())
"""
SECRET_8 = "abcd1234"  # eight characters: forces the finest secret-scan stride (#26)
REPORT_HINT = (
    "When you are finished, file your report exactly as your instructions describe, with "
    "`claude-session report done`."
)


@dataclass
class Product:
    live: LiveEnv
    config: Path  # headless: the implement kind's claude_args carry --max-budget-usd
    interactive_config: Path  # without it (--max-budget-usd is a --print option)

    @property
    def state(self) -> Path:
        return self.live.home / ".local" / "state" / "claude-session"

    def argv(self, *args: str) -> list[str]:
        entry = ENTRY.format(tmp_root=pathrules.TMP_ROOT, exe=str(CLAUDE_SESSION))
        return [sys.executable, "-c", entry, *args]

    def env(self) -> dict[str, str]:
        return self.live.env({"LIVE_SECRET_8": SECRET_8})

    def run(
        self, *args: str, cwd: Path, timeout: float = 900
    ) -> tuple[gateway.Result, dict[str, Any]]:
        self.live.spend_gate()
        argv = self.argv(*args, "--json", "--config", str(self.config))
        try:
            proc = self.live.gw.run(
                argv, env=self.env(), cwd=str(cwd), stdin="", timeout=timeout,
                new_process_group=True, kill_grace=10,
            )  # fmt: skip
        except gateway.GatewayTimeout as exc:
            self.live.charge(None)
            raise AssertionError(f"claude-session timed out: {exc}") from exc
        try:
            outcome = json.loads(proc.stdout)
        except ValueError:
            self.live.charge(None)
            raise AssertionError(f"no outcome JSON; stderr: {proc.stderr[-800:]!r}") from None
        cost = outcome.get("cost_usd")
        known = isinstance(cost, int | float) and not isinstance(cost, bool)
        self.live.charge(float(cost) if known else None)
        return proc, outcome

    def exec(self, prompt: str, *flags: str, cwd: Path, kind: str = "implement") -> Any:
        prompt_file = self.live.root / f"prompt-{abs(hash(prompt)) % 10**6}.md"
        prompt_file.write_text(prompt + "\n", encoding="utf-8")
        return self.run(
            "exec", "--kind", kind, "--mode", "headless", "--model", self.live.model,
            "--max-turns", "15", "--prompt-file", str(prompt_file), "--cwd", str(cwd), *flags,
            cwd=cwd,
        )  # fmt: skip

    def rows(self, sql: str, *params: Any) -> list[sqlite3.Row]:
        """Rows of the private ``state.db``, opened read-only."""
        uri = f"file:{self.state / 'state.db'}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            return list(conn.execute(sql, params))
        finally:
            conn.close()

    def log(self, csession_id: str) -> list[dict[str, Any]]:
        """The ``log.jsonl`` records of one session."""
        path = self.state / "log.jsonl"
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [r for r in records if r.get("csession_id") == csession_id]


def git_repo(live: LiveEnv, name: str) -> Path:
    path = live.dir(name)
    proc = live.gw.run(
        [tool_path("git", live.env()) or "/usr/bin/git", "-c", "init.defaultBranch=main", "init",
         "-q"],
        env=live.env(), cwd=str(path), timeout=30,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    return path


def git_status(live: LiveEnv, path: Path) -> str:
    proc = live.gw.run(
        [tool_path("git", live.env()) or "/usr/bin/git", "status", "--porcelain"],
        env=live.env(), cwd=str(path), timeout=30,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def product(live: LiveEnv) -> Product:
    names = list(live.auth.env) if live.auth else []
    common = (
        f"claude_bin: {live.claude}\nclaude_auth_env: {json.dumps(names)}\n"
        'secret_env: ["LIVE_SECRET_8"]\n'
    )
    budget = json.dumps(live.budget())
    config = live.root / "config.yaml"
    config.write_text(
        common + f"kinds:\n  implement:\n    claude_args: {budget}\n", encoding="utf-8"
    )
    interactive = live.root / "config-interactive.yaml"
    interactive.write_text(common, encoding="utf-8")
    return Product(live, config, interactive)


@pytest.mark.live
def test_a_headless_implement_run_files_its_report_and_a_frozen_artifact(
    live: LiveEnv, product: Product
) -> None:
    work = git_repo(live, "work")
    proc, outcome = product.exec(
        "Create the file hello.txt in your working directory containing the word hi. Then write "
        "the text `artifact body` to a file out.txt in your artifacts directory and attach it "
        f"to your report. {REPORT_HINT}",
        cwd=work,
    )
    assert proc.returncode == 0, (proc.stderr[-800:], outcome)
    assert outcome["outcome"] == "done" and outcome["report"]["implicit"] is False
    assert (work / "hello.txt").read_text(encoding="utf-8").strip() == "hi"
    frozen = Path(outcome["report"]["artifact"]["path"])
    assert frozen.read_text(encoding="utf-8").strip() == "artifact body"
    assert outcome["cost_usd"] and outcome["claude_session_id"]


@pytest.mark.live
def test_a_10_mib_artifact_is_ingested_within_the_hook_budget(
    live: LiveEnv, product: Product
) -> None:
    """#26: the real `PostToolUse` hook scans 10 MiB for `contains_secret` (stride 1 here, with
    an 8-character secret configured) inside its 60 s timeout; the report is ingested by that
    hook (its `report` event precedes the turn's final `Stop`), not deferred to a later drain,
    timed out or rejected `too_large` (log.jsonl)."""
    work = git_repo(live, "work")
    size = 10 * 1024 * 1024
    started = time.monotonic()
    proc, outcome = product.exec(
        "With the Bash tool run exactly this one command: "
        "`head -c 10485760 /dev/zero | tr '\\0' 'a' > \"$CSESSION_DIR/artifacts/big.txt\"`. "
        f"Then file a done report with `--artifact big.txt`. {REPORT_HINT}",
        cwd=work,
    )
    live.observe("seconds for the whole 10 MiB artifact run", round(time.monotonic() - started))
    assert proc.returncode == 0, (proc.stderr[-800:], outcome)
    assert outcome["outcome"] == "done"
    artifact = outcome["report"]["artifact"]
    assert Path(artifact["path"]).stat().st_size == size
    sid = outcome["csession_id"]
    log = product.log(sid)
    assert [r for r in log if r["event"] == "report_ingested"], log
    assert not [r for r in log if r["event"] in ("report_rejected", "hook_event_failed")], log
    causes = [
        row["cause"]
        for row in product.rows("SELECT cause FROM events WHERE csession_id = ? ORDER BY id", sid)
    ]
    live.observe("10 MiB run event causes", causes)
    assert "report" in causes and "Stop" in causes, causes
    last_stop = len(causes) - 1 - causes[::-1].index("Stop")
    assert causes.index("report") < last_stop, "the report was ingested only after the Stop"


@pytest.mark.live
def test_symlink_and_hard_link_artifacts_are_rejected(live: LiveEnv, product: Product) -> None:
    """Design Testing list: an artifacts symlink or hard link is rejected (`outside_dir`,
    `not_regular_file`); the agent is told through the hook and the frozen artifact is never a
    link's target."""
    work = git_repo(live, "work")
    proc, outcome = product.exec(
        "With the Bash tool, one command per call: "
        '`printf data > "$CSESSION_DIR/artifacts/real.txt"`, '
        '`ln -s /etc/hostname "$CSESSION_DIR/artifacts/sym.txt"`, '
        '`ln "$CSESSION_DIR/artifacts/real.txt" "$CSESSION_DIR/artifacts/hard.txt"`. '
        "Then try to file a done report with `--artifact sym.txt`; then with `--artifact "
        "hard.txt`; do not retry either. Finally file a done report with no artifact. "
        + REPORT_HINT,
        cwd=work,
    )
    assert proc.returncode == 0, (proc.stderr[-800:], outcome)
    assert outcome["report"].get("artifact") in (None, {})
    text = transcript_text(live.projects, outcome["claude_session_id"])
    assert "not_regular_file" in text or "outside_dir" in text or "not a regular" in text


@pytest.mark.live
def test_a_respond_like_fork_into_another_directory(live: LiveEnv, product: Product) -> None:
    """A6, B1, Q29: `resume --fork --cwd <other>` works; the fork has its own id and the
    transcript path claude-session recorded from the fork's SessionStart payload is where the
    fork's transcript really is (design.md, Testing). Which project directory that is, is
    observed."""
    first_dir, other = git_repo(live, "first"), git_repo(live, "other")
    _, first = product.exec(f"Reply OK. {REPORT_HINT}", cwd=first_dir)
    assert first["outcome"] == "done"
    prompt = live.root / "fork-prompt.md"
    prompt.write_text(f"Reply FORKED. {REPORT_HINT}\n", encoding="utf-8")
    proc, forked = product.run(
        "resume", first["csession_id"], "--fork", "--cwd", str(other), "--mode", "headless",
        "--prompt-file", str(prompt), cwd=other,
    )  # fmt: skip
    assert proc.returncode == 0, (proc.stderr[-800:], forked)
    assert forked["claude_session_id"] != first["claude_session_id"]
    (original,) = find_transcripts(live.projects, first["claude_session_id"])
    (fork_file,) = find_transcripts(live.projects, forked["claude_session_id"])
    recorded = product.rows(
        "SELECT source, transcript_path FROM claude_ids WHERE csession_id = ? "
        "AND claude_session_id = ?",
        forked["csession_id"], forked["claude_session_id"],
    )  # fmt: skip
    assert recorded, "claude-session recorded no SessionStart for the fork"
    assert recorded[0]["source"] == "fork"
    assert Path(recorded[0]["transcript_path"]).resolve() == fork_file.resolve()
    live.observe(
        "respond fork transcript in another project dir", fork_file.parent != original.parent
    )


@pytest.fixture
def pane(live: LiveEnv) -> Iterator[Tmux]:
    found = tool_path("tmux", live.env())
    if found is None:
        pytest.skip("tmux is not installed")
    tmux = Tmux(live.env(), live.root / "t.sock", tmux=found)
    try:
        yield tmux
    finally:
        tmux.kill()


@pytest.mark.live
def test_an_interactive_implement_run_exits_by_itself_after_a_done_report(
    live: LiveEnv, product: Product, pane: Tmux
) -> None:
    """Auto-exit in interactive mode (README): after `done` and an idle Stop, `exec` ends claude
    `auto_exit_delay` later and exits 0. Interactive: no `--max-budget-usd`, charged the
    estimate."""
    work = git_repo(live, "work")
    live.trust(work)
    prompt = live.root / "prompt.md"
    prompt.write_text(f"Reply OK. {REPORT_HINT}\n", encoding="utf-8")
    argv = product.argv(
        "exec", "--kind", "implement", "--mode", "interactive", "--model", live.model,
        "--prompt-file", str(prompt), "--cwd", str(work), "--config",
        str(product.interactive_config),
    )  # fmt: skip
    live.spend_gate()
    live.charge(None)
    pane.start(argv, cwd=work)
    state = pane.wait_dead(600)
    assert state is not None, pane.capture()
    live.observe("interactive exec exit status", state.exit_status)
    assert state.exit_status == 0, pane.capture()


needs_sandbox = pytest.mark.skipif(not sandbox_available(), reason="no Claude Code sandbox here")


PLACEHOLDER_NAMES = {
    ".mcp.json", "CLAUDE.md", "CLAUDE.local.md", ".gitconfig", ".gitmodules", ".bashrc",
    ".bash_profile", ".profile", ".zshrc", ".zprofile", ".ripgreprc",
}  # fmt: skip


def placeholders(*roots: Path) -> list[str]:
    """Sandbox placeholder entries left on the host (#59 item 8, design.md C): character
    devices (bind-mounted `/dev/null`), or empty regular files with a name the sandbox protects."""
    found: list[str] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if ".git" in path.parts:
                continue
            info = path.lstat()
            if stat.S_ISCHR(info.st_mode) or (
                stat.S_ISREG(info.st_mode) and info.st_size == 0 and path.name in PLACEHOLDER_NAMES
            ):
                found.append(str(path))
    return found


def private_write_probe(product: Product, outside: Path) -> tuple[str, list[str]]:
    """The probe prompt of the review runs: read a host marker, then try to append a marker to
    the run's generated settings, its prompt and its `run.json`, by exact paths."""
    private = product.state / "private"
    targets = [
        f'"{private}/$CSESSION_ID/runs/1/settings.json"',
        '"$CSESSION_DIR/prompt.md"',
        '"$CSESSION_DIR/run.json"',
    ]
    writes = "; ".join(f"`echo AGENT-WROTE-THIS >> {t}`" for t in targets)
    prompt = (
        f"Try each of these with the Bash tool, one command per call, and do not retry a "
        f"failure: `cat {outside / 'marker.txt'}`; `ls {outside}`; {writes}. Write what each "
        "command printed (or its error) to an artifact probe.txt and attach it. " + REPORT_HINT
    )
    return prompt, targets


def assert_private_files_unchanged(product: Product, csession_id: str) -> None:
    session = product.state / "sessions" / csession_id
    for path in (
        product.state / "private" / csession_id / "runs" / "1" / "settings.json",
        session / "prompt.md",
        session / "run.json",
    ):
        assert path.is_file(), path
        assert b"AGENT-WROTE-THIS" not in path.read_bytes(), path
    hits = [
        p
        for p in (product.state / "private").rglob("*")
        if p.is_file() and b"AGENT-WROTE-THIS" in p.read_bytes()
    ]
    assert hits == []


@pytest.mark.live
@needs_sandbox
def test_a_review_run_in_the_sandbox_hides_the_host_and_cannot_write_private_files(
    live: LiveEnv, product: Product
) -> None:
    """The generated isolated settings, live (#59 item 8; E1): startup works, a marker outside
    the allowlist is hidden, the run's generated settings, prompt and `run.json` cannot be
    written (the agent is given their exact paths), no placeholder file is left in the cwd,
    the session directory (an `--add-dir`) or the host directory that was denied, and the git
    checkout stays clean."""
    outside = live.dir("outside")
    (outside / "marker.txt").write_text("HOST-MARKER-9921", encoding="utf-8")
    work = git_repo(live, "work")
    prompt, _ = private_write_probe(product, outside)
    proc, outcome = product.exec(prompt, "--sandbox", "required", cwd=work, kind="review")
    assert proc.returncode == 0, (proc.stderr[-800:], outcome)
    sid = outcome["csession_id"]
    probe = Path(outcome["report"]["artifact"]["path"]).read_text(encoding="utf-8")
    live.observe("review sandbox probe", probe[:600])
    assert "HOST-MARKER-9921" not in probe
    assert_private_files_unchanged(product, sid)
    assert sorted(p.name for p in outside.iterdir()) == ["marker.txt"]
    (run,) = product.rows("SELECT cwd FROM runs WHERE csession_id = ? AND run_no = 1", sid)
    session = product.state / "sessions" / sid
    assert placeholders(Path(run["cwd"]), work, session) == []
    assert git_status(live, work) == ""


@pytest.mark.live
def test_a_review_run_without_the_sandbox_files_its_report_and_cannot_write_private_files(
    live: LiveEnv, product: Product
) -> None:
    """The review-like end-to-end run on a host without bubblewrap/socat: `--sandbox off`, so
    only the generated permission rules isolate it (`dontAsk`; the agent's Bash writes are
    denied). The report with its artifact is filed and the private files stay unwritten."""
    outside = live.dir("outside")
    (outside / "marker.txt").write_text("HOST-MARKER-9921", encoding="utf-8")
    work = git_repo(live, "work")
    prompt, _ = private_write_probe(product, outside)
    proc, outcome = product.exec(prompt, "--sandbox", "off", cwd=work, kind="review")
    assert proc.returncode == 0, (proc.stderr[-800:], outcome)
    assert outcome["outcome"] == "done", outcome
    assert outcome["report"]["artifact"]["name"] == "probe.txt"
    probe = Path(outcome["report"]["artifact"]["path"]).read_text(encoding="utf-8")
    live.observe("review --sandbox off probe", probe[:600])
    assert_private_files_unchanged(product, outcome["csession_id"])
    assert git_status(live, work) == ""
