"""The design's open items (issue #42): the Read, Grep and Glob tools on a symlink planted by
sandboxed Bash (F1), `/proc` isolation inside the sandbox (F2), and the search tools under the
home-directory Read denies the isolated settings generate (#59 item 12). MCP elicitation is in
`test_interactive.py` (it needs a terminal).

None of these has been run against a real Claude Code yet. Where the design does not say what
Claude Code does (Grep and Glob under a `Read` deny), the test records the answer with
`live.observe` and asserts only the control, so the first run reports a finding instead of
failing; where the design relies on a behavior (no file content returned through a symlink to an
unreadable place), it asserts.

Everything is planted inside the test's private root (`live.root`, with its private `HOME`);
nothing touches the real home. `--tools` is passed because Glob and Grep are not in the default
tool set (design.md, D3); isolated runs refuse that flag in `claude_args`, so these tests build
settings by hand, like `test_sandbox.py`."""

from __future__ import annotations

import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from .harness import LiveEnv, ToolCall, init_event, tool_calls
from .test_sandbox import needs_sandbox, sandbox_settings

F1_SECRET = "F1-SECRET-7782"
SSH_MARKER = "SSH-MARKER-5530"
CONTROL = "CONTROL-WORD-3141"
SEARCH_TOOLS = "Read,Grep,Glob"


def outputs(calls: Sequence[ToolCall], *names: str) -> list[str]:
    return [c.output for c in calls if c.name in names]


def shown(calls: Sequence[ToolCall], *names: str) -> list[str]:
    """A short rendering of what the named tools returned, for observations."""
    return [f"{c.name}{'!' if c.is_error else ''}: {c.output[:160]!r}" for c in calls
            if c.name in names]  # fmt: skip


@pytest.mark.live
@needs_sandbox
@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_f1_a_symlink_planted_by_sandboxed_bash_does_not_open_a_file_outside(
    live: LiveEnv, tool: str
) -> None:
    """F1: sandboxed Bash plants `link.txt` (or `dirlink`) in the cwd pointing at a file (or
    directory) outside the allowed directories. Read and Grep must not return the file's content
    (`blockReadsOutsideWorkingDirectories` must check the resolved path). Glob returns names
    only: what it lists through `dirlink` is recorded. A plain file in the cwd is the control
    that the tool works at all."""
    work, outside = live.dir("work"), live.dir("outside")
    (outside / "secret.txt").write_text(F1_SECRET + "\n", encoding="utf-8")
    (work / "plain.txt").write_text(CONTROL + "\n", encoding="utf-8")
    settings = sandbox_settings(live, work, allow=["Bash(ln:*)"], auto_allow=True)
    settings["permissions"]["allow"] += ["Read", "Grep", "Glob"]
    settings["permissions"]["blockReadsOutsideWorkingDirectories"] = True
    path = live.write_json("s.json", settings)
    plant = (
        f"With the Bash tool run `ln -s {outside / 'secret.txt'} link.txt` and then "
        f"`ln -s {outside} dirlink`."
    )
    steps = {
        "Read": f"Use the Read tool on {work / 'link.txt'}, then on {work / 'plain.txt'}.",
        "Grep": (
            f"Use the Grep tool with pattern F1-SECRET and path {work / 'link.txt'}; then with "
            f"pattern F1-SECRET and path {work}; then with pattern CONTROL-WORD and path {work}."
        ),
        "Glob": f"Use the Glob tool with pattern `dirlink/*` and path {work}; then with pattern "
        f"`*.txt` and path {work}.",
    }
    run = live.headless(
        f"{plant} {steps[tool]} Do not retry a failure and use no other tool. Then reply DONE.",
        "--setting-sources", "", "--settings", str(path), "--tools", f"Bash,{SEARCH_TOOLS}",
        cwd=work,
    )  # fmt: skip
    assert (work / "link.txt").is_symlink() and (work / "dirlink").is_symlink(), (
        "control failed: sandboxed Bash did not plant the symlinks, so nothing was tested"
    )
    calls = tool_calls(run.events)
    live.observe(f"F1 {tool} results", shown(calls, tool))
    assert any(c.name == tool for c in calls), f"the agent never used {tool}"
    control = CONTROL if tool != "Glob" else "plain.txt"
    assert any(control in out for out in outputs(calls, tool)), (
        f"control failed: {tool} did not return the plain file in the cwd"
    )
    if tool == "Glob":
        live.observe(
            "F1 Glob lists the names behind a symlinked directory",
            any("secret.txt" in out for out in outputs(calls, "Glob")),
        )
    else:
        leaked = [out for out in outputs(calls, tool) if F1_SECRET in out]
        assert leaked == [], f"{tool} returned the content behind the planted symlink"


@pytest.mark.live
@needs_sandbox
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc is Linux only")
def test_f2_the_sandbox_hides_host_processes(live: LiveEnv) -> None:
    """F2: inside the sandbox `/proc` shows only the sandbox's own processes (private PID
    namespace). A host process the test started (exact PID, killed at teardown) holds a marker
    in its environment: the sandbox must not read it through `/proc/<pid>/environ`, and
    `/proc` must list fewer processes than the host's. Whether `/proc/<pid>/root` is reachable
    is recorded."""
    marker = f"F2-ENV-{uuid.uuid4().hex}"
    sleeper = live.gw.spawn(
        ["sleep", "600"], env=live.env({"F2_ENV_MARKER": marker}, auth=False), cwd=str(live.root)
    )
    live.children.append(sleeper)
    pid = sleeper.pid
    host_pids = {p.name for p in Path("/proc").iterdir() if p.name.isdigit()}
    environ = Path(f"/proc/{pid}/environ").read_bytes()
    assert marker.encode() in environ, "control failed: the host cannot read its own child"

    work = live.dir("work")
    settings = live.write_json(
        "s.json", sandbox_settings(live, work, allow=["Bash(ls:*)", "Bash(cat:*)"], auto_allow=True)
    )
    run = live.headless(
        "With the Bash tool run these, one per call, and report what each printed or its error: "
        f"`ls /proc`, `cat /proc/{pid}/environ`, `ls /proc/{pid}/root/`, `cat /proc/1/cmdline`. "
        "Do not retry. Then reply DONE.",
        "--setting-sources", "", "--settings", str(settings), cwd=work,
    )  # fmt: skip
    calls = [c for c in tool_calls(run.events) if c.name == "Bash"]
    live.observe("F2 Bash results", shown(calls, "Bash"))
    listing = [c for c in calls if str(c.input.get("command", "")).strip() == "ls /proc"]
    assert listing, "the agent never listed /proc"
    inside = {w for w in listing[0].output.split() if w.isdigit()}
    live.observe("F2 processes visible in /proc (sandbox, host)", [len(inside), len(host_pids)])
    assert marker not in "\n".join(c.output for c in calls), "the host process's environ was read"
    assert len(inside) < len(host_pids), "/proc lists as many processes as the host has"


def search_settings(live: LiveEnv, variant: str) -> dict[str, Any]:
    ssh = live.home / ".ssh"
    permissions: dict[str, Any] = {"defaultMode": "dontAsk", "allow": ["Read", "Grep", "Glob"]}
    if variant in ("block-reads", "both"):
        permissions["blockReadsOutsideWorkingDirectories"] = True
    if variant in ("read-deny", "both"):
        permissions["deny"] = [f"Read(/{ssh}/**)"]  # `//` + the absolute path, like the builder
    return {"permissions": permissions}


@pytest.mark.live
@pytest.mark.parametrize("variant", ["block-reads", "read-deny", "both"])
def test_search_tools_and_the_home_read_denies(live: LiveEnv, variant: str) -> None:
    """#59 item 12 and the review-profile search tools: Read, Grep and Glob on a planted
    `~/.ssh` of the private home, under `blockReadsOutsideWorkingDirectories` only, a
    `Read(//<home>/.ssh/**)` deny only, and both (what isolated runs generate). With both, Read
    and Grep must not return the key. The single-rule variants only record what leaks (the docs
    call the deny best-effort for Grep and Glob). LS is not covered: it is not in the tool set
    of the Claude Code the design was verified against; `init` tools are recorded."""
    ssh = live.home / ".ssh"
    ssh.mkdir(parents=True)
    (ssh / "id_test").write_text(SSH_MARKER + "\n", encoding="utf-8")
    work = live.dir("work")
    (work / "plain.txt").write_text(CONTROL + "\n", encoding="utf-8")
    path = live.write_json("s.json", search_settings(live, variant))
    run = live.headless(
        f"Use these tools, one per call, with no other tool and no retry of a failure: Read "
        f"{work / 'plain.txt'}; Read {ssh / 'id_test'}; Grep pattern SSH-MARKER path {ssh}; "
        f"Glob pattern `*` path {ssh}; Grep pattern CONTROL-WORD path {work}; Glob pattern "
        f"`*.txt` path {work}. Then reply DONE.",
        "--setting-sources", "", "--settings", str(path), "--tools", SEARCH_TOOLS, cwd=work,
    )  # fmt: skip
    live.observe(f"tools in init ({variant})", init_event(run.events).get("tools"))
    calls = tool_calls(run.events)
    live.observe(f"{variant} results", shown(calls, "Read", "Grep", "Glob"))
    for tool in ("Read", "Grep", "Glob"):
        used = [c for c in calls if c.name == tool]
        assert used, f"the agent never used {tool}"
    assert any(CONTROL in out for out in outputs(calls, "Read")), "control failed: Read"
    assert any(CONTROL in out for out in outputs(calls, "Grep")), "control failed: Grep"
    assert any("plain.txt" in out for out in outputs(calls, "Glob")), "control failed: Glob"
    leaks = {
        tool: any(SSH_MARKER in out for out in outputs(calls, tool)) for tool in ("Read", "Grep")
    }
    leaks["Glob (name listed)"] = any("id_test" in out for out in outputs(calls, "Glob"))
    live.observe(f"search tools that returned the planted key or its name ({variant})", leaks)
    if variant == "both":
        assert not leaks["Read"] and not leaks["Grep"], leaks
