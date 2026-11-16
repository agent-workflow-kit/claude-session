"""The consumer contract (docs/interface.md, Stability for consumers): the output fields and
behaviors other tools rely on, pinned against REAL command output, so a change that would break
a consumer fails a test here and is never found first by the consumer.

How the pins work. A pin is a field path and a type (``PINS``): ``taints: bool``,
``profile.reads_third_party: bool``, ``hooks[].selected: bool`` (``[]`` checks every item of an
array, and there must be one). ``violations`` reports a pinned field that is missing (removed or
renamed) or of another type (retyped); it ignores every field it does not name, so an additive
optional field never fails a pin. The documents come from the real ``claude-session`` entry
point run against the fake ``claude`` (``tests/fakebin``), not from fixtures written here, and
they are not whole-document goldens (those are ``tests/golden``).

Behavior pins are plain tests against the same real commands. Each group's docstring quotes the
comment on issue #45 (the 1.0 readiness checklist) that names the surface.

The mutation tests at the end prove the pins work: removing, renaming or retyping any pinned
field of the real output fails ``violations``, and adding fields (also nested ones) does not.
"""

from __future__ import annotations

import copy
import json
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from .fakeclaude import read_jsonl
from .test_exec_headless import SECRET_VALUE
from .test_exec_hooks import HOOK
from .test_start_adopt import DONE, START, STOP, Env, result_step

# --- the pin machinery -------------------------------------------------------------------

TYPES: dict[str, Callable[[Any], bool]] = {
    "str": lambda v: isinstance(v, str),
    "bool": lambda v: isinstance(v, bool),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "num": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "list": lambda v: isinstance(v, list),
    "dict": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def has_type(value: Any, spec: str) -> bool:
    """Whether ``value`` has one of the ``|``-separated types of ``spec``."""
    return any(TYPES[name](value) for name in spec.split("|"))


def values_at(doc: Any, path: str) -> list[Any] | None:
    """The values ``path`` names in ``doc``, or ``None`` when a step is missing (an empty
    array under ``[]`` counts as missing: there is nothing to check)."""
    found = [doc]
    for step in path.split("."):
        each = step.endswith("[]")
        key = step.removesuffix("[]")
        nxt: list[Any] = []
        for item in found:
            if not isinstance(item, dict) or key not in item:
                return None
            value = item[key]
            if each:
                if not isinstance(value, list) or not value:
                    return None
                nxt.extend(value)
            else:
                nxt.append(value)
        found = nxt
    return found


def violations(doc: Any, fields: dict[str, str]) -> list[str]:
    """The pinned fields of ``fields`` (path -> type spec) that ``doc`` lacks or retypes."""
    found: list[str] = []
    for path, spec in fields.items():
        values = values_at(doc, path)
        if values is None:
            found.append(f"{path}: missing")
        elif not all(has_type(v, spec) for v in values):
            found.append(f"{path}: not {spec}")
    return found


# Document name -> pinned fields. The docstrings of the tests that produce each document
# name the consumer and quote the issue comment.
PINS: dict[str, dict[str, str]] = {
    # agent-spawn: `start --dry-run --json` fields `taints`, `profile.reads_third_party`,
    # `would_refuse`, `run_kind`, `repo` (+ the `hooks` entries of per-invocation `--hook`).
    "start_dry_run": {
        "taints": "bool",
        "profile.reads_third_party": "bool",
        "would_refuse": "str|null",
        "run_kind": "str",
        "repo": "str|null",
        "hooks[].hook": "str",
        "hooks[].cmd": "str",
        "hooks[].source": "str",
        "hooks[].selected": "bool",
    },
    # agent-spawn: `status --json` fields `run.phase`, `run.reads_third_party`, `tainted`,
    # `csession_dir`, `recent_events`.
    "status": {
        "csession_id": "str",
        "run.phase": "str",
        "run.reads_third_party": "bool",
        "tainted": "bool",
        "csession_dir": "str",
        "recent_events": "list",
        "state": "str",
        "labels": "list",
    },
    # agent-spawn: the exit-12 `launch_failed` envelope carrying `csession_id` and `run`.
    "launch_failed": {
        "error.code": "str",
        "error.csession_id": "str",
        "error.run": "int",
        "error.end_reason": "str",
        "error.message": "str",
    },
    # agent-spawn / gatectl / ticket-queue: `list --label ... --json`.
    "list": {
        "sessions": "list",
        "sessions[].csession_id": "str",
        "sessions[].labels": "list",
        "sessions[].state": "str",
    },
    # gatectl: `start --json`, then `exec --session-id <id> --run <n> ...`; ticket-queue:
    # the names to pass at adoption (`required_env_names`).
    "start": {
        "csession_id": "str",
        "csession_dir": "str",
        "argv": "list",
        "run": "int",
        "required_env_names": "list",
        "env_inherit": "bool",
    },
    # gatectl: the adopting `exec --json` outcome and the file `--outcome-file` receives.
    "outcome": {
        "csession_id": "str",
        "run": "int",
        "outcome": "str",
        "end_reason": "str|null",
        "exit_code": "int",
    },
    # gatectl: `stop <id> --grace 2s --json` of a pending run.
    "stop_pending": {
        "csession_id": "str",
        "canceled": "bool",
        "ended": "bool",
        "outcome": "str",
        "end_reason": "str",
        "state": "str",
    },
}

# --- fixtures and drivers ------------------------------------------------------------------


@pytest.fixture
def state(_private_state_dir: Path) -> Path:
    return _private_state_dir / "claude-session"


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def env(tmp_path: Path, state: Path, work: Path) -> Env:
    e = Env(tmp_path, state, work, tmp_path / "config.yaml", tmp_path / "fake.jsonl")
    e.scenario([START, DONE, STOP, result_step()])
    e.write_config()
    return e


def run_json(env: Env, *argv: str, code: int | None = 0) -> dict[str, Any]:
    """One real ``claude-session`` command with ``--json``; its exit code is checked."""
    config = [] if "--session-id" in argv else ["--config", str(env.config)]  # adoption: none
    proc = env.cs([*argv, "--json", *config])
    if code is not None:
        assert proc.returncode == code, proc.stdout + proc.stderr
    assert proc.stdout, proc.stderr
    out: dict[str, Any] = json.loads(proc.stdout)
    return out


def hook_command(log: Path, name: str) -> str:
    return shlex.join([sys.executable, "-I", str(HOOK), "--log", str(log), "--name", name])


def hook_payloads(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(line)["payload"] for line in log.read_text().splitlines()]


def fresh_start(env: Env, *args: str, code: int = 0) -> dict[str, Any]:
    """``start --json`` (a prompt is staged), exit code checked."""
    proc, out = env.start(args) if args else env.start()
    assert proc.returncode == code, proc.stdout + proc.stderr
    return out


@pytest.fixture
def docs(env: Env, tmp_path: Path) -> dict[str, Any]:
    """The pinned documents, each produced by a real command."""
    found: dict[str, Any] = {}
    hook = hook_command(tmp_path / "docs-hook.jsonl", "docs")
    prompt = tmp_path / "docs-prompt.md"
    prompt.write_text("Do the task.\n", encoding="utf-8")
    found["start_dry_run"] = run_json(
        env,
        "start",
        "--dry-run",
        "--mode",
        "headless",
        "--prompt-file",
        str(prompt),
        "--hook",
        f"post-state-change={hook}",
    )
    out = fresh_start(env, "--mode", "headless", "--label", "owner=docs")
    found["start"] = out
    sid = out["csession_id"]
    found["stop_pending"] = run_json(env, "stop", sid, "--grace", "2s")
    sid = fresh_start(env, "--mode", "headless", "--label", "owner=docs")["csession_id"]
    found["outcome"] = run_json(env, "exec", "--session-id", sid, "--run", "1")
    found["status"] = run_json(env, "status", sid)
    found["list"] = run_json(env, "list", "--label", "owner=docs")
    env.config.write_text(f"claude_bin: {env.tmp_path / 'missing'}\n", encoding="utf-8")
    found["launch_failed"] = run_json(
        env, "start", "--mode", "headless", "--prompt-file", str(prompt), code=12
    )
    return found


def test_every_pinned_document_holds_its_pins(docs: dict[str, Any]) -> None:
    assert set(docs) == set(PINS)
    for name, fields in PINS.items():
        assert violations(docs[name], fields) == [], name


def test_the_pinned_documents_are_the_real_shapes(docs: dict[str, Any]) -> None:
    """The values, not only the types, of the fields the consumers branch on."""
    dry = docs["start_dry_run"]
    assert (dry["taints"], dry["would_refuse"], dry["run_kind"]) == (False, None, "implement")
    assert dry["profile"]["reads_third_party"] is False
    assert [(h["hook"], h["source"], h["selected"]) for h in dry["hooks"]] == [
        ("post-state-change", "flag", True)
    ]
    status = docs["status"]
    assert status["run"]["phase"] == "session" and status["state"] == "exited"
    assert status["tainted"] is False and status["labels"] == ["kind=implement", "owner=docs"]
    assert status["recent_events"], "an adopted run has events"
    failed = docs["launch_failed"]["error"]
    assert (failed["code"], failed["end_reason"], failed["run"]) == (
        "launch_failed",
        "claude_not_found",
        1,
    )
    assert docs["outcome"]["outcome"] == "done" and docs["outcome"]["exit_code"] == 0
    stopped = docs["stop_pending"]
    assert (stopped["canceled"], stopped["end_reason"], stopped["state"]) == (
        True,
        "canceled",
        "not-started",
    )
    assert docs["outcome"]["csession_id"] in [s["csession_id"] for s in docs["list"]["sessions"]]


# --- agent-spawn -----------------------------------------------------------------------------


def test_agent_spawn_launch_failed_csession_id_is_a_session_status_stop_and_forget_accept(
    env: Env,
) -> None:
    """agent-spawn, issue #45 (the agent-spawn design session note): "the exit-12
    `launch_failed` envelope carrying `csession_id` and `run`" (#119: the id is a real session)."""
    env.config.write_text(f"claude_bin: {env.tmp_path / 'missing'}\n", encoding="utf-8")
    out = run_json(env, "start", "--mode", "headless", code=2)  # no prompt: a usage error
    assert out["error"]["code"] == "usage" and "csession_id" not in out["error"]
    prompt = env.tmp_path / "p.md"
    prompt.write_text("x\n", encoding="utf-8")
    out = run_json(env, "start", "--mode", "headless", "--prompt-file", str(prompt), code=12)
    sid = out["error"]["csession_id"]
    assert run_json(env, "status", sid)["csession_id"] == sid
    assert run_json(env, "stop", sid)["already_final"] is True
    assert sid in json.dumps(run_json(env, "forget", sid))
    run_json(env, "status", sid, code=10)


def test_agent_spawn_per_invocation_post_state_change_hook_is_stored_and_delivered(
    env: Env, tmp_path: Path
) -> None:
    """agent-spawn, issue #45: "per-invocation `--hook post-state-change=...` stored for late
    delivery". The hook is not in the config; it is given at `start` only. The adopting `exec`
    delivers its events, and `stop` of a never-adopted run (no live wrapper: late delivery)
    delivers the final event to it too."""
    adopted_log, canceled_log = tmp_path / "adopted.jsonl", tmp_path / "canceled.jsonl"
    flag = "--hook-timeout"
    sid = fresh_start(
        env,
        "--mode",
        "headless",
        "--hook",
        f"post-state-change={hook_command(adopted_log, 'a')}",
        flag,
        "post-state-change=10s",
    )["csession_id"]
    assert run_json(env, "exec", "--session-id", sid)["outcome"] == "done"
    got = hook_payloads(adopted_log)
    assert got and got[-1]["final"] is True and got[-1]["object_id"] == sid
    assert got[-1]["state"] == "exited"

    pending = fresh_start(
        env,
        "--mode",
        "headless",
        "--hook",
        f"post-state-change={hook_command(canceled_log, 'c')}",
        flag,
        "post-state-change=10s",
    )["csession_id"]
    assert run_json(env, "stop", pending)["canceled"] is True
    late = hook_payloads(canceled_log)
    assert late and late[-1]["final"] is True and late[-1]["object_id"] == pending
    assert late[-1]["end_reason"] == "canceled"


def test_agent_spawn_list_label_selects_by_label(env: Env) -> None:
    """agent-spawn, issue #45: "`list --label`"; ticket-queue: "listing orphaned sessions with
    `list --kind <kind> --label <k>=<v>`"."""
    mine = fresh_start(env, "--mode", "headless", "--label", "owner=a")["csession_id"]
    other = fresh_start(env, "--mode", "headless", "--label", "owner=b")["csession_id"]
    ids = lambda out: sorted(s["csession_id"] for s in out["sessions"])  # noqa: E731
    assert ids(run_json(env, "list", "--label", "owner=a")) == [mine]
    assert ids(run_json(env, "list", "--label", "owner=b")) == [other]
    assert ids(run_json(env, "list", "--kind", "implement", "--label", "owner=a")) == [mine]
    assert ids(run_json(env, "list", "--kind", "review", "--label", "owner=a")) == []
    assert ids(run_json(env, "list", "--label", "owner=none")) == []
    assert ids(run_json(env, "list")) == sorted([mine, other])


# --- gatectl -----------------------------------------------------------------------------------


def test_gatectl_outcome_file_is_accepted_at_adoption(env: Env, tmp_path: Path) -> None:
    """gatectl, issue #45 (the gatectl design note): "`--outcome-file` accepted at adoption"
    (`exec --session-id <id> --run <n> --outcome-file <path> --json`)."""
    sid = fresh_start(env, "--mode", "headless")["csession_id"]
    target = tmp_path / "gate-outcome.json"
    out = run_json(env, "exec", "--session-id", sid, "--run", "1", "--outcome-file", str(target))
    written = json.loads(target.read_text(encoding="utf-8"))
    assert violations(written, PINS["outcome"]) == []
    assert (written["csession_id"], written["outcome"]) == (sid, out["outcome"])


def test_gatectl_stop_cancels_a_pending_run(env: Env, tmp_path: Path) -> None:
    """gatectl, issue #45: "`stop` cancelling a pending run" (`stop <id> --grace 2s`); a
    canceled run is never adopted afterwards."""
    sid = fresh_start(env, "--mode", "headless")["csession_id"]
    out = run_json(env, "stop", sid, "--grace", "2s")
    assert violations(out, PINS["stop_pending"]) == []
    assert (out["canceled"], out["signals"], out["end_reason"]) == (True, [], "canceled")
    assert run_json(env, "status", sid)["run"]["outcome"] == "not-started"
    proc, _ = env.adopt(sid)
    assert proc.returncode == 20  # refused: the run is over (expired)
    assert not env.log.exists()  # claude never started


def test_gatectl_list_label_filters_repeat_and_are_anded(env: Env) -> None:
    """gatectl, issue #45: "`list --label` accepting repeated filters" (orphans:
    `list --label owner=gatectl --json`); interface.md: "`--label` is repeatable and ANDed"."""
    both = fresh_start(env, "--mode", "headless", "--label", "owner=gatectl", "--label", "gate=1")
    one = fresh_start(env, "--mode", "headless", "--label", "owner=gatectl")
    ids = lambda out: sorted(s["csession_id"] for s in out["sessions"])  # noqa: E731
    assert ids(run_json(env, "list", "--label", "owner=gatectl")) == sorted(
        [both["csession_id"], one["csession_id"]]
    )
    twice = run_json(env, "list", "--label", "owner=gatectl", "--label", "gate=1")
    assert ids(twice) == [both["csession_id"]]
    assert ids(run_json(env, "list", "--label", "gate=1", "--label", "owner=nobody")) == []


def test_gatectl_timeout_is_whole_seconds_and_zero_is_a_usage_error(env: Env) -> None:
    """gatectl, issue #45: "`--timeout` as whole seconds (0 is a usage error)"."""
    prompt = env.tmp_path / "p.md"
    prompt.write_text("x\n", encoding="utf-8")
    base = ("--mode", "headless", "--prompt-file", str(prompt))
    plan = run_json(env, "start", "--dry-run", *base, "--timeout", "30")
    assert plan["timeout"] == 30 and has_type(plan["timeout"], "num")
    for zero in ("0", "0s"):
        out = run_json(env, "start", *base, "--timeout", zero, code=2)
        assert out["error"]["code"] == "usage"
    assert env.rows("SELECT COUNT(*) FROM sessions") == [(0,)]
    out = run_json(env, "exec", "--dry-run", *base, "--timeout", "0", code=2)
    assert out["error"]["code"] == "usage"


# --- ticket-queue, ticketctl, the human-only rule (issue #45 notes) ------------------------


def test_ticket_queue_start_reports_the_names_the_adopter_must_pass(env: Env) -> None:
    """ticket-queue, issue #45 ("a minimal environment for `start` and the adopting `exec`,
    passing the names listed in `required_env_names`"): the names are strings, include the
    configured allow names, and `env_inherit` says whether the caller's variables pass."""
    env.write_config("kinds:\n  implement:\n    env:\n      allow: [QUEUE_TOKEN]\n")
    out = fresh_start(env, "--mode", "headless")
    assert all(isinstance(n, str) for n in out["required_env_names"])
    assert "QUEUE_TOKEN" in out["required_env_names"]
    assert out["env_inherit"] is True
    restricted = fresh_start(env, "--mode", "headless", "--env-allowlist-only", "ONLY")
    assert restricted["env_inherit"] is False and "ONLY" in restricted["required_env_names"]


def test_ticketctl_a_secret_env_variable_reaches_only_a_kind_that_names_it(
    env: Env,
) -> None:
    """ticketctl, issue #45 (the ticketctl design note: credentials come from environment
    variables named in its config, listed in `secret_env`): a `secret_env` name is stripped
    from the session unless the kind's `env.allow` names it exactly."""
    probe = {
        "do": "bash",
        "command": 'printf "%s" "${TICKET_TOKEN-unset}" > "$CSESSION_DIR/artifacts/v.txt"',
    }
    env.scenario([START, probe, DONE, STOP, result_step()])

    def seen(config: str) -> str:
        env.write_config(config)
        sid = fresh_start(env, "--mode", "headless")["csession_id"]
        proc, _ = env.adopt(sid, env={"TICKET_TOKEN": "tok-0123456789"})
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return (env.state / "sessions" / sid / "artifacts" / "v.txt").read_text()

    assert seen("secret_env: [TICKET_TOKEN]\n") == "unset"
    allowed = (
        "secret_env: [TICKET_TOKEN]\nkinds:\n  implement:\n    env:\n      allow: [TICKET_TOKEN]\n"
    )
    assert seen(allowed) == "tok-0123456789"


@pytest.mark.parametrize(
    ("actor", "flag"),
    [
        ("auto:bot", "--allow-tainted"),
        ("agent:x", "--override-escalation"),
        ("human:", "--allow-tainted"),
    ],
)
def test_the_human_only_rule_is_checked_before_the_session_lookup(
    env: Env, actor: str, flag: str
) -> None:
    """agent-spawn and review-run, issue #45 (the PR #93 note): an explicit `--actor` must be
    `human:<non-blank>` for `--allow-tainted` and `--override-escalation`; the check runs
    before the session lookup (usage, exit 2, even for an id that does not exist)."""
    out = run_json(env, "resume", "cs-abcdefgh", flag, "--actor", actor, code=2)
    assert out["error"]["code"] == "usage"
    ok = run_json(env, "resume", "cs-abcdefgh", flag, "--actor", "human:alice", code=10)
    assert ok["error"]["code"] == "not_found"


def test_the_pinned_run_state_is_recorded_in_the_fake_run(env: Env) -> None:
    """The fake claude ran once for the adopted session (the pins above use real runs)."""
    sid = fresh_start(env, "--mode", "headless")["csession_id"]
    run_json(env, "exec", "--session-id", sid)
    assert len([r for r in read_jsonl(env.log) if r["event"] == "start"]) == 1
    assert SECRET_VALUE  # imported fixture constant stays referenced


# --- mutation tests: the pins fail on removal, rename, retype and pass on additions ------


def all_pins() -> list[tuple[str, str, str]]:
    return [(name, path, spec) for name, fields in PINS.items() for path, spec in fields.items()]


def mutate(doc: Any, path: str, change: Callable[[dict[str, Any], str], None]) -> Any:
    """A copy of ``doc`` with ``change(parent, key)`` applied at every parent of ``path``'s
    last step."""
    doc = copy.deepcopy(doc)
    *parents, last = path.split(".")
    nodes = [doc]
    for step in parents:
        key = step.removesuffix("[]")
        nodes = [v for n in nodes for v in (n[key] if step.endswith("[]") else [n[key]])]
    for node in nodes:
        change(node, last.removesuffix("[]"))
    return doc


def wrong_value(spec: str) -> Any:
    """A value of a type outside ``spec``."""
    for candidate in ("text", 12345, True, [1], {"x": 1}, None):
        if not has_type(candidate, spec):
            return candidate
    raise AssertionError(spec)


@pytest.mark.parametrize(("name", "path", "spec"), all_pins(), ids=lambda v: str(v))
def test_removing_renaming_or_retyping_a_pinned_field_fails(
    docs: dict[str, Any], name: str, path: str, spec: str
) -> None:
    fields = PINS[name]
    removed = mutate(docs[name], path, lambda n, k: n.pop(k))
    assert f"{path}: missing" in violations(removed, fields)

    def rename(node: dict[str, Any], key: str) -> None:
        node[key + "_renamed"] = node.pop(key)

    assert f"{path}: missing" in violations(mutate(docs[name], path, rename), fields)

    def retype(node: dict[str, Any], key: str) -> None:
        node[key] = wrong_value(spec)

    assert f"{path}: not {spec}" in violations(mutate(docs[name], path, retype), fields)


@pytest.mark.parametrize("name", sorted(PINS))
def test_adding_optional_fields_does_not_fail_a_pin(docs: dict[str, Any], name: str) -> None:
    doc = copy.deepcopy(docs[name])

    def grow(node: Any) -> None:
        if isinstance(node, dict):
            for value in list(node.values()):
                grow(value)
            node["new_optional_field"] = {"nested": [1, 2]}
        elif isinstance(node, list):
            for item in node:
                grow(item)

    grow(doc)
    assert doc != docs[name]
    assert violations(doc, PINS[name]) == []


def test_the_pins_notice_an_emptied_array(docs: dict[str, Any]) -> None:
    doc = copy.deepcopy(docs["list"])
    doc["sessions"] = []
    assert violations(doc, PINS["list"]) == [
        "sessions[].csession_id: missing",
        "sessions[].labels: missing",
        "sessions[].state: missing",
    ]
