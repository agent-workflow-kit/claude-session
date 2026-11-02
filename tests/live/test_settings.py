"""How `--settings` interacts with the other settings sources and Claude Code's validation.

Covers #56 items 5 to 7, the `disableAllHooks` question (a), the permission-mode rows of
design.md (B: `--setting-sources ""`, bypass downgrade) and rule paths with spaces or `:`.
The managed-settings questions (b, and #59 item 13) need root and are human-run: see
docs/live-tests.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ..fakeclaude import recorder
from .harness import LiveEnv, events_named, hook_group, init_event, read_log

OK = "Reply OK."


def mode_of(run: Any) -> str:
    value = init_event(run.events)["permissionMode"]
    assert isinstance(value, str)
    return value


def project_settings(live: LiveEnv, shared: dict[str, Any], local: dict[str, Any]) -> Path:
    work = live.dir("work")
    live.write_json("work/.claude/settings.json", shared)
    live.write_json("work/.claude/settings.local.json", local)
    return work


@pytest.mark.live
def test_settings_default_mode_beats_project_and_local_without_user_sources(live: LiveEnv) -> None:
    """#56 item 5. The control first: with no `--settings` the project/local value applies, so
    the override below is not vacuous."""
    work = project_settings(
        live,
        {"permissions": {"defaultMode": "acceptEdits"}},
        {"permissions": {"defaultMode": "plan"}},
    )
    sources = ("--setting-sources", "project,local")
    control = live.headless(OK, *sources, cwd=work)
    live.observe("project+local defaultMode with no --settings", mode_of(control))
    assert mode_of(control) in ("plan", "acceptEdits"), "project settings were not honoured"
    settings = live.write_json("s.json", {"permissions": {"defaultMode": "dontAsk"}})
    run = live.headless(OK, *sources, "--settings", str(settings), cwd=work)
    assert mode_of(run) == "dontAsk"


@pytest.mark.live
def test_empty_setting_sources_with_an_explicit_default_mode_does_not_start_in_auto(
    live: LiveEnv,
) -> None:
    """B: `--setting-sources ""` alone starts in `auto` (recorded); the generated
    `defaultMode: default` must win."""
    bare = live.headless(OK, "--setting-sources", "")
    live.observe('permissionMode with --setting-sources "" and no mode', mode_of(bare))
    settings = live.write_json("s.json", {"permissions": {"defaultMode": "default"}})
    run = live.headless(OK, "--setting-sources", "", "--settings", str(settings))
    assert mode_of(run) == "default"


@pytest.mark.live
def test_bypass_permissions_is_downgraded_in_project_settings_but_not_in_settings(
    live: LiveEnv,
) -> None:
    work = project_settings(live, {"permissions": {"defaultMode": "bypassPermissions"}}, {})
    project = live.headless(OK, "--setting-sources", "project", cwd=work)
    assert mode_of(project) != "bypassPermissions"
    settings = live.write_json("s.json", {"permissions": {"defaultMode": "bypassPermissions"}})
    explicit = live.headless(OK, "--setting-sources", "", "--settings", str(settings), cwd=work)
    assert mode_of(explicit) == "bypassPermissions"


INVALID = {
    "bad-default-mode": {"defaultMode": "bogus"},
    "additional-dirs-not-a-list": {"additionalDirectories": "/tmp"},
    "additional-dirs-not-strings": {"additionalDirectories": [1]},
}


@pytest.mark.live
@pytest.mark.parametrize("permissions", INVALID.values(), ids=INVALID.keys())
def test_an_invalid_value_makes_claude_discard_the_whole_settings_file_hooks_included(
    live: LiveEnv, permissions: dict[str, Any]
) -> None:
    """#56 item 7 (probed on 2.1.314): the builder rejects such values because of this."""
    log = live.root / "start.jsonl"
    hooks = {"SessionStart": [hook_group(recorder(log, args=["SessionStart"]))]}
    good = live.write_json("good.json", {"hooks": hooks, "permissions": {"defaultMode": "default"}})
    live.headless(OK, "--settings", str(good))
    assert len(events_named(read_log(log), "SessionStart")) == 1, "control: the hook must fire"
    bad = live.write_json("bad.json", {"hooks": hooks, "permissions": permissions})
    live.headless(OK, "--settings", str(bad))
    assert len(events_named(read_log(log), "SessionStart")) == 1, "the hook fired from a bad file"


@pytest.mark.live
@pytest.mark.parametrize("where", ["user", "project", "local"])
def test_disable_all_hooks_in_a_settings_source_and_the_settings_file_override(
    live: LiveEnv, where: str
) -> None:
    """Open question (a): does `disableAllHooks: false` in `--settings` beat `true` from a
    setting source? The control must hold (the source suppresses the hooks); the override is
    recorded either way, since it decides whether generated settings can close the gap."""
    log = live.root / "start.jsonl"
    hooks = {"SessionStart": [hook_group(recorder(log, args=["SessionStart"]))]}
    paths = {
        "user": "home/.claude/settings.json",
        "project": "work/.claude/settings.json",
        "local": "work/.claude/settings.local.json",
    }
    live.write_json(paths[where], {"disableAllHooks": True})
    work = live.dir("work")
    sources = ("--setting-sources", "user,project,local")
    plain = live.write_json("plain.json", {"hooks": hooks})
    live.headless(OK, *sources, "--settings", str(plain), cwd=work)
    assert not events_named(read_log(log), "SessionStart"), f"{where} disableAllHooks is ignored"
    override = live.write_json("override.json", {"hooks": hooks, "disableAllHooks": False})
    live.headless(OK, *sources, "--settings", str(override), cwd=work)
    live.observe(
        f"disableAllHooks:false in --settings overrides true from {where}",
        bool(events_named(read_log(log), "SessionStart")),
    )


@pytest.mark.live
@pytest.mark.parametrize("dirname", ["a b", "a:b"])
def test_file_rules_match_paths_with_spaces_and_colons(live: LiveEnv, dirname: str) -> None:
    """#56 item 6: `Write(//a b/**)`, `Edit(...)` and `Read(...)` for such paths (`//` + the
    absolute path). Under `dontAsk`, only a matching allow rule lets the tool run."""
    outside = live.dir(dirname)
    (outside / "in.txt").write_text("cherry\n", encoding="utf-8")
    pattern = f"/{outside}/**"  # "//" + absolute path
    rules = [f"{tool}({pattern})" for tool in ("Read", "Edit", "Write")]
    settings = live.write_json(
        "s.json", {"permissions": {"defaultMode": "dontAsk", "allow": rules}}
    )
    common = ("--setting-sources", "", "--settings", str(settings))
    read = live.headless(
        f"Use the Read tool on {outside / 'in.txt'} and reply with its content.", *common
    )
    assert "cherry" in read.text.lower(), read.final.get("permission_denials")
    target = outside / "out.txt"
    live.headless(f"Use the Write tool to create {target} with the content written.", *common)
    assert target.exists() and "written" in target.read_text(encoding="utf-8")


@pytest.mark.live
def test_settings_hooks_merge_with_user_and_project_hooks_and_a_deny_beats_an_allow(
    live: LiveEnv,
) -> None:
    """Spike: hooks from `--settings`, user and project settings all fire; permissions merge and
    a deny from any source beats an allow."""
    user, project, injected = (live.root / n for n in ("user.jsonl", "project.jsonl", "inj.jsonl"))
    for path, log in (
        ("home/.claude/settings.json", user),
        ("work/.claude/settings.json", project),
    ):
        hooks = {"SessionStart": [hook_group(recorder(log, args=["SessionStart"]))]}
        live.write_json(path, {"hooks": hooks})
    work = live.dir("work")
    (work / "note.txt").write_text("walnut\n", encoding="utf-8")
    project_file = work / ".claude" / "settings.json"
    project_file.write_text(
        json.dumps(
            {
                "hooks": {"SessionStart": [hook_group(recorder(project, args=["SessionStart"]))]},
                "permissions": {"deny": ["Read(./note.txt)"]},
            }
        ),
        encoding="utf-8",
    )
    hooks = {"SessionStart": [hook_group(recorder(injected, args=["SessionStart"]))]}
    settings = live.write_json(
        "s.json", {"hooks": hooks, "permissions": {"allow": ["Read"], "defaultMode": "dontAsk"}}
    )
    run = live.headless(
        "Use the Read tool on ./note.txt and tell me the first word, or say DENIED if you cannot.",
        "--setting-sources", "user,project", "--settings", str(settings), cwd=work,
    )  # fmt: skip
    for log in (user, project, injected):
        assert len(events_named(read_log(log), "SessionStart")) == 1, log.name
    assert "walnut" not in run.text.lower()


@pytest.mark.live
def test_claude_md_loads_only_from_declared_setting_sources(live: LiveEnv) -> None:
    """`--setting-sources ""` loads no CLAUDE.md (the control loads it)."""
    work = live.dir("work")
    (work / "CLAUDE.md").write_text("The magic word is GRAPEFRUIT.\n", encoding="utf-8")
    ask = "What is the magic word from your instructions? If there is none, reply UNKNOWN."
    assert "GRAPEFRUIT" in live.headless(ask, "--setting-sources", "project", cwd=work).text
    assert "GRAPEFRUIT" not in live.headless(ask, "--setting-sources", "", cwd=work).text


@pytest.mark.live
def test_block_reads_outside_working_directories_is_a_permissions_key_a_bare_read_does_not_lift(
    live: LiveEnv,
) -> None:
    """#59 item 1 and the sandbox follow-ups: the key sits under `permissions`; a Read outside
    the working directories is refused even under a bare `Read` allow rule; `--add-dir` opens a
    directory."""
    outside, extra = live.dir("outside"), live.dir("extra")
    (outside / "x.txt").write_text("outside-word PLUM\n", encoding="utf-8")
    (extra / "y.txt").write_text("extra-word CHERRY\n", encoding="utf-8")
    work = live.dir("work")
    settings = live.write_json(
        "s.json",
        {
            "permissions": {
                "defaultMode": "dontAsk",
                "allow": ["Read"],
                "blockReadsOutsideWorkingDirectories": True,
            }
        },
    )
    common = ("--setting-sources", "", "--settings", str(settings))
    ask = "Use the Read tool on {} and reply with its content, or DENIED."
    blocked = live.headless(ask.format(outside / "x.txt"), *common, cwd=work)
    assert "PLUM" not in blocked.text.upper()
    allowed = live.headless(ask.format(extra / "y.txt"), *common, "--add-dir", str(extra), cwd=work)
    assert "CHERRY" in allowed.text.upper()
