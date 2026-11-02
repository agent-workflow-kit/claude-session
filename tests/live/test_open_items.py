"""The design's open items (issue #42): the Read, Grep and Glob tools on a symlink in the cwd
that points outside (F1), `/proc` isolation inside the sandbox (F2), and the search tools under the
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

import os
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from .harness import (
    LiveEnv,
    ToolCall,
    found,
    init_event,
    is_proc_listing,
    leaked,
    missing_file,
    probe_calls,
    proc_entries,
    tool_calls,
)
from .test_sandbox import needs_sandbox, sandbox_settings

F1_SECRET = "F1-SECRET-7782"
SSH_MARKER = "SSH-MARKER-5530"
CONTROL = "CONTROL-WORD-3141"
SEARCH_TOOLS = "Read,Grep,Glob"


def shown(calls: Sequence[ToolCall], *names: str) -> list[str]:
    """A short rendering of what the named tools returned, for observations."""
    return [f"{c.name}{'!' if c.is_error else ''}: {c.output[:160]!r}" for c in calls
            if c.name in names]  # fmt: skip


GREP_CONTENT = 'with output_mode "content"'


def grep_modes(calls: Sequence[ToolCall]) -> list[str]:
    """The `output_mode` each Grep call used (Claude Code's default is `files_with_matches`)."""
    return [str(c.input.get("output_mode", "default")) for c in calls if c.name == "Grep"]


@pytest.mark.live
@needs_sandbox
@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_f1_a_symlink_in_the_cwd_does_not_open_a_file_outside(live: LiveEnv, tool: str) -> None:
    """F1: `link.txt` (and `dirlink`) in the cwd point at a file (and a directory) outside the
    allowed directories, as sandboxed Bash could plant them. The test plants them itself before
    the run, so no probe can race the planting. Read and Grep must not return the file's content
    or, for Grep's paths-only mode, list the link (`blockReadsOutsideWorkingDirectories` must
    check the resolved path). A direct read of the outside file is the control that the block
    is in force; a plain file in the cwd is the control that the tool works. Glob returns names
    only: what it lists through `dirlink` is recorded."""
    work, outside = live.dir("work"), live.dir("outside")
    secret, link = outside / "secret.txt", work / "link.txt"
    secret.write_text(F1_SECRET + "\n", encoding="utf-8")
    (work / "plain.txt").write_text(CONTROL + "\n", encoding="utf-8")
    os.symlink(secret, link)
    os.symlink(outside, work / "dirlink")
    settings = sandbox_settings(live, work, auto_allow=True)
    settings["permissions"]["allow"] += ["Read", "Grep", "Glob"]
    settings["permissions"]["blockReadsOutsideWorkingDirectories"] = True
    path = live.write_json("s.json", settings)
    steps = {
        "Read": f"Use the Read tool on {work / 'plain.txt'}, then on {secret}, then on {link}.",
        "Grep": (
            f"Use the Grep tool {GREP_CONTENT} each time: pattern CONTROL-WORD and path {work}; "
            f"then pattern F1-SECRET and path {secret}; then pattern F1-SECRET and path {link}; "
            f"then pattern F1-SECRET and path {work}."
        ),
        "Glob": f"Use the Glob tool with pattern `*.txt` and path {work}; then with pattern "
        f"`dirlink/*` and path {work}.",
    }
    run = live.headless(
        f"{steps[tool]} One call each, in this order. Do not retry a failure and use no other "
        "tool. Then reply DONE.",
        "--setting-sources", "", "--settings", str(path), "--tools", SEARCH_TOOLS, cwd=work,
    )  # fmt: skip
    calls = [c for c in tool_calls(run.events) if c.name == tool]
    live.observe(f"F1 {tool} results", shown(calls, tool))
    if tool == "Grep":
        live.observe("F1 Grep output_mode used", grep_modes(calls))
    assert calls, f"the agent never used {tool}"
    names = ("plain.txt",) if tool != "Read" else ()
    assert found(calls, CONTROL, names), f"control failed: {tool} did not return the plain file"
    if tool == "Glob":
        probes = probe_calls(calls, "dirlink", "Glob")
        assert probes, "the agent never probed dirlink with Glob"
        live.observe(
            "F1 Glob lists the names behind a symlinked directory",
            any("secret.txt" in c.output for c in probes),
        )
        return
    direct = probe_calls(calls, str(secret), tool)
    assert direct, f"the agent never probed {secret} directly with {tool}"
    assert leaked(direct, F1_SECRET, ("secret.txt",)) == [], (
        f"control failed: {tool} read the outside file directly, so the read block is not in "
        "force and F1 is inconclusive (this is not a symlink finding)"
    )
    probes = probe_calls(calls, str(link), tool)
    assert probes, f"the agent never probed {link} with {tool}"
    assert not [c for c in probes if missing_file(c)], (
        f"{tool} reported the planted link missing, so nothing was tested",
        shown(probes, tool),
    )
    # Every call counts: Grep on the cwd also reaches the link.
    names = ("link.txt",) if tool == "Grep" else ()
    assert leaked(calls, F1_SECRET, names) == [], (
        f"{tool} returned the content behind the planted symlink (or listed the link)"
    )


@pytest.mark.live
@needs_sandbox
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc is Linux only")
def test_f2_the_sandbox_hides_host_processes(live: LiveEnv) -> None:
    """F2: inside the sandbox `/proc` shows only the sandbox's own processes (private PID
    namespace). A host process the test started (exact PID, killed at teardown; a minimal
    environment of `PATH` and a marker) must not be listed in `/proc`, and the sandbox must not
    read the marker through `/proc/<pid>/environ`. A `/proc` listing that fails or lacks `self`
    fails the test: the namespace is then unverified, not verified. Whether `/proc/<pid>/root`
    is reachable and the host's process count are recorded; environ text never is."""
    marker = f"F2-ENV-{uuid.uuid4().hex}"
    child_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "F2_ENV_MARKER": marker}
    sleeper = live.gw.spawn(["sleep", "600"], env=child_env, cwd=str(live.root))
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
    reads = probe_calls(calls, f"/proc/{pid}/environ", "Bash")
    others = [c for c in calls if c not in reads]
    live.observe("F2 Bash results (environ reads omitted)", shown(others, "Bash"))
    live.observe(
        "F2 environ reads (error, marker leaked)",
        [(c.is_error, marker in c.output) for c in reads],
    )
    listing = [c for c in calls if is_proc_listing(str(c.input.get("command", "")))]
    assert listing, "the agent never listed /proc"
    inside = proc_entries(listing[0])
    assert inside is not None, "/proc unreadable under deny-root; PID namespace unverified"
    live.observe("F2 processes visible in /proc (sandbox, host)", [len(inside), len(host_pids)])
    assert str(pid) not in inside, "/proc in the sandbox lists the host child"
    assert reads, f"the agent never read /proc/{pid}/environ"
    assert marker not in "\n".join(c.output for c in calls), "the host process's environ was read"


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
    secret = ssh / "id_test"
    run = live.headless(
        f"Use these tools, one per call, in this order, with no other tool and no retry of a "
        f"failure: Read {work / 'plain.txt'}; Grep pattern CONTROL-WORD path {work} "
        f"{GREP_CONTENT}; Glob pattern `*.txt` path {work}; Read {secret}; Grep pattern "
        f"SSH-MARKER path {ssh} {GREP_CONTENT}; Glob pattern `*` path {ssh}. Then reply DONE.",
        "--setting-sources", "", "--settings", str(path), "--tools", SEARCH_TOOLS, cwd=work,
    )  # fmt: skip
    live.observe(f"tools in init ({variant})", init_event(run.events).get("tools"))
    calls = tool_calls(run.events)
    live.observe(f"{variant} results", shown(calls, "Read", "Grep", "Glob"))
    live.observe(f"Grep output_mode used ({variant})", grep_modes(calls))
    by_tool = {tool: [c for c in calls if c.name == tool] for tool in ("Read", "Grep", "Glob")}
    for tool, used in by_tool.items():
        assert used, f"the agent never used {tool}"
    assert found(by_tool["Read"], CONTROL), "control failed: Read"
    assert found(by_tool["Grep"], CONTROL, ("plain.txt",)), "control failed: Grep"
    assert found(by_tool["Glob"], "plain.txt"), "control failed: Glob"
    probes = {
        "Read": probe_calls(calls, str(secret), "Read"),
        "Grep": probe_calls(calls, str(ssh), "Grep"),
        "Glob": probe_calls(calls, str(ssh), "Glob"),
    }
    for tool, used in probes.items():
        assert used, f"the agent never probed {ssh} with {tool}"
    leaks = {
        "Read": bool(leaked(probes["Read"], SSH_MARKER)),
        "Grep": bool(leaked(probes["Grep"], SSH_MARKER, ("id_test",))),
        "Glob (name listed)": bool(leaked(probes["Glob"], "id_test")),
    }
    live.observe(f"search tools that returned the planted key or its name ({variant})", leaks)
    if variant == "both":
        assert not leaks["Read"] and not leaks["Grep"], leaks
