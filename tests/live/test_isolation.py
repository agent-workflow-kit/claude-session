"""Permission and project-config behaviour isolated runs rely on that needs no sandbox:
planted skills (E2), skills in `--add-dir` directories (#59 item 10), report command forms
under `dontAsk` (A8), read-only plugin dirs (C6).
The sandbox rows are in test_sandbox.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ..fakeclaude import report_stub
from .harness import LiveEnv, read_log, transcript_text


def plant_skill(directory: Path, name: str, token: str) -> None:
    skill = directory / ".claude" / "skills" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Replies with a fixed token.\n---\n\n"
        f"Reply with exactly the token {token}.\n",
        encoding="utf-8",
    )


@pytest.mark.live
def test_planted_project_and_nested_skills_do_not_load_without_setting_sources(
    live: LiveEnv,
) -> None:
    """E2. The control run (`--setting-sources project`, trusted) proves the planted skill is
    loadable. For the nested skill, whether the control loads it from the subdirectory is
    observed; its absence under `--setting-sources ""` is asserted only when it does."""
    work = live.dir("work")
    plant_skill(work, "planted", "PLANTED-TOP")
    nested = work / "sub"
    nested.mkdir()
    plant_skill(nested, "planted-nested", "PLANTED-NESTED")
    live.trust(work)
    control = live.headless("/planted", "--setting-sources", "project", cwd=work)
    assert "PLANTED-TOP" in control.text, "control: the planted skill should load"
    run = live.headless("/planted", "--setting-sources", "", cwd=work)
    assert "PLANTED-TOP" not in run.text
    nested_control = live.headless("/planted-nested", "--setting-sources", "project", cwd=nested)
    loadable = "PLANTED-NESTED" in nested_control.text
    live.observe("E2: a nested skill loads with --setting-sources project", loadable)
    nested_run = live.headless("/planted-nested", "--setting-sources", "", cwd=nested)
    if loadable:
        assert "PLANTED-NESTED" not in nested_run.text
    else:
        live.observe(
            'E2: nested skill under --setting-sources "" (control did not load it)',
            "PLANTED-NESTED" in nested_run.text,
        )


@pytest.mark.live
def test_skills_in_an_add_dir_directory_under_empty_setting_sources(live: LiveEnv) -> None:
    """#59 item 10: does `.claude/skills` in an `--add-dir` directory load under
    `--setting-sources ""`? An open question, so the answer is recorded; the control (the same
    directory as the trusted cwd with `--setting-sources project`) proves the skill loadable."""
    extra = live.dir("extra")
    plant_skill(extra, "addskill", "ADD-DIR-SKILL")
    live.trust(extra)
    control = live.headless("/addskill", "--setting-sources", "project", cwd=extra)
    assert "ADD-DIR-SKILL" in control.text, "control: the skill should load from its project"
    run = live.headless(
        "/addskill", "--setting-sources", "", "--add-dir", str(extra), cwd=live.dir("work")
    )
    live.observe('#59 10: a skill in --add-dir loads under --setting-sources ""',
                 "ADD-DIR-SKILL" in run.text)  # fmt: skip


@pytest.mark.live
def test_report_command_forms_under_dont_ask(live: LiveEnv) -> None:
    """A8: only the bare `claude-session report ...` matches `Bash(claude-session report:*)`;
    `--summary="..."`, a heredoc `--summary-file -` and the absolute-path rule work; `cd ... &&`
    and `$(...)` are denied. A stand-in `claude-session` records what actually ran."""
    log = live.root / "report.jsonl"
    stub = report_stub(live.dir("stubbin"), log)
    rules = ["Bash(claude-session report:*)", f"Bash({stub} report:*)"]
    settings = live.write_json(
        "s.json", {"permissions": {"defaultMode": "dontAsk", "allow": rules}}
    )
    path = f"{stub.parent}:{live.base_environ.get('PATH', '/usr/bin:/bin')}"
    prompt = (
        "Run these five shell commands with the Bash tool, one per call, exactly as written. "
        "If one is refused, do not retry it or change it; go on to the next.\n"
        '1. claude-session report done --summary="form-bare"\n'
        "2. claude-session report done --summary-file - <<'EOF'\nform-heredoc\nEOF\n"
        f'3. {stub} report done --summary="form-absolute"\n'
        '4. cd / && claude-session report done --summary="form-cd"\n'
        '5. claude-session report done --summary="$(echo form-subst)"\n'
        "Then reply DONE."
    )
    live.headless(
        prompt, "--setting-sources", "", "--settings", str(settings), extra_env={"PATH": path}
    )
    ran = json.dumps([[r["args"], r["stdin"]] for r in read_log(log)])
    for allowed in ("form-bare", "form-heredoc", "form-absolute"):
        assert allowed in ran, (allowed, ran)
    for denied in ("form-cd", "form-subst"):
        assert denied not in ran, (denied, ran)


@pytest.mark.live
def test_a_plugin_dir_is_readable_with_add_dir_and_not_writable_with_deny_rules(
    live: LiveEnv,
) -> None:
    """C6: the Read tool reaches plugin files through `--add-dir`; `--add-dir` makes the
    directory writable, which the Edit/Write denies undo."""
    plugin = live.dir("plugin")
    (plugin / "reference.md").write_text("The reference word is QUINCE.\n", encoding="utf-8")
    pattern = f"/{plugin}/**"
    settings = live.write_json(
        "s.json",
        {
            "permissions": {
                "defaultMode": "dontAsk",
                "allow": ["Read", "Write", "Edit"],
                "deny": [f"Edit({pattern})", f"Write({pattern})"],
            }
        },
    )
    run = live.headless(
        f"Use the Read tool on {plugin / 'reference.md'} and tell me the reference word. Then "
        f"use the Write tool to create {plugin / 'new.txt'} with the content x. Then reply DONE.",
        "--setting-sources", "", "--settings", str(settings), "--add-dir", str(plugin),
    )  # fmt: skip
    assert "QUINCE" in run.text.upper()
    assert not (plugin / "new.txt").exists()
    assert "QUINCE" in transcript_text(live.projects, run.session_id).upper()
