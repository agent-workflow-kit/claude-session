"""End to end with real Claude Code through `claude-session` itself: headless implement-like
run with an artifact, a 10 MiB artifact (#26), symlink and hard-link artifacts, interactive
run with auto-exit, a respond-like fork into another directory, and (where a sandbox exists) a
review run under the generated isolated settings (#59 item 8, E1, private-dir writes).

Every run uses the private environment of `LiveEnv`: the state directory is under its private
HOME, the temporary-directory root is the per-test one from `conftest.private_tmp_root`, and
`claude_auth_env` names the credential variable in use."""

from __future__ import annotations

import json
import shutil
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
    config: Path

    def argv(self, *args: str) -> list[str]:
        entry = ENTRY.format(tmp_root=pathrules.TMP_ROOT, exe=str(CLAUDE_SESSION))
        return [sys.executable, "-c", entry, *args]

    def env(self) -> dict[str, str]:
        return self.live.env({"LIVE_SECRET_8": SECRET_8})

    def run(
        self, *args: str, cwd: Path, timeout: float = 900
    ) -> tuple[gateway.Result, dict[str, Any]]:
        argv = self.argv(*args, "--json", "--config", str(self.config))
        proc = self.live.gw.run(
            argv, env=self.env(), cwd=str(cwd), stdin="", timeout=timeout,
            new_process_group=True, kill_grace=10,
        )  # fmt: skip
        assert proc.stdout, proc.stderr
        outcome = json.loads(proc.stdout)
        self.live.ledger.charge(float(outcome.get("cost_usd") or 0.0))
        return proc, outcome

    def exec(self, prompt: str, *flags: str, cwd: Path, kind: str = "implement") -> Any:
        prompt_file = self.live.root / f"prompt-{abs(hash(prompt)) % 10**6}.md"
        prompt_file.write_text(prompt + "\n", encoding="utf-8")
        return self.run(
            "exec", "--kind", kind, "--mode", "headless", "--model", self.live.model,
            "--max-turns", "15", "--prompt-file", str(prompt_file), "--cwd", str(cwd), *flags,
            cwd=cwd,
        )  # fmt: skip


def git_repo(live: LiveEnv, name: str) -> Path:
    path = live.dir(name)
    proc = live.gw.run(
        [shutil.which("git") or "/usr/bin/git", "-c", "init.defaultBranch=main", "init", "-q"],
        env=live.env(), cwd=str(path), timeout=30,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    return path


@pytest.fixture
def product(live: LiveEnv) -> Product:
    names = list(live.auth.env) if live.auth else []
    config = live.root / "config.yaml"
    config.write_text(
        f"claude_bin: {live.claude}\nclaude_auth_env: {json.dumps(names)}\n"
        'secret_env: ["LIVE_SECRET_8"]\n',
        encoding="utf-8",
    )
    return Product(live, config)


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
    an 8-character secret configured) inside its 60 s timeout; the report is ingested, not
    deferred or rejected `too_large`."""
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
    """A6, B1, Q29: `resume --fork --cwd <other>` works; the fork has its own id and a transcript
    in the other directory's project."""
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
    assert fork_file.parent != original.parent


@pytest.fixture
def pane(live: LiveEnv) -> Iterator[Tmux]:
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    tmux = Tmux(live.env(), live.root / "t.sock")
    try:
        yield tmux
    finally:
        tmux.kill()


@pytest.mark.live
def test_an_interactive_implement_run_exits_by_itself_after_a_done_report(
    live: LiveEnv, product: Product, pane: Tmux
) -> None:
    """Auto-exit in interactive mode (README): after `done` and an idle Stop, `exec` ends claude
    `auto_exit_delay` later and exits 0."""
    work = git_repo(live, "work")
    live.trust(work)
    prompt = live.root / "prompt.md"
    prompt.write_text(f"Reply OK. {REPORT_HINT}\n", encoding="utf-8")
    argv = product.argv(
        "exec", "--kind", "implement", "--mode", "interactive", "--model", live.model,
        "--prompt-file", str(prompt), "--cwd", str(work), "--config", str(product.config),
    )  # fmt: skip
    pane.start(argv, cwd=work)
    state = pane.wait_dead(600)
    assert state is not None, pane.capture()
    live.observe("interactive exec exit status", state.exit_status)
    assert state.exit_status == 0, pane.capture()


needs_sandbox = pytest.mark.skipif(not sandbox_available(), reason="no Claude Code sandbox here")


@pytest.mark.live
@needs_sandbox
def test_a_review_run_in_the_sandbox_hides_the_host_and_cannot_write_private_files(
    live: LiveEnv, product: Product
) -> None:
    """The generated isolated settings, live (#59 item 8; E1): startup works, a marker outside
    the allowlist is hidden, the run's private files cannot be written, and no placeholder
    file lands in the host directory that was denied."""
    outside = live.dir("outside")
    (outside / "marker.txt").write_text("HOST-MARKER-9921", encoding="utf-8")
    private = live.home / ".local" / "state" / "claude-session" / "private"
    work = git_repo(live, "work")
    proc, outcome = product.exec(
        f"Try, with the Bash tool and one command per call: `cat {outside / 'marker.txt'}`; "
        f"`ls {outside}`; `find {private} -name settings.json`, then append the text "
        "AGENT-WROTE-THIS to the first file that find prints with `echo AGENT-WROTE-THIS >> "
        "<file>`. Write what each command printed (or its error) to an artifact probe.txt and "
        "attach it. Do not retry failures. " + REPORT_HINT,
        "--sandbox", "required", cwd=work, kind="review",
    )  # fmt: skip
    assert proc.returncode == 0, (proc.stderr[-800:], outcome)
    probe = Path(outcome["report"]["artifact"]["path"]).read_text(encoding="utf-8")
    assert "HOST-MARKER-9921" not in probe
    hits = [p for p in private.rglob("*") if p.is_file() and b"AGENT-WROTE-THIS" in p.read_bytes()]
    assert hits == []
    assert sorted(p.name for p in outside.iterdir()) == ["marker.txt"]
