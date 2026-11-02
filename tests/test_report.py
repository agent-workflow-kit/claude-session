"""The ``report`` command, write side (interface.md, report; design.md, Outbox and
ingestion): validation, size caps, both artifact forms, run.json, the outbox file."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from claude_session import cli, ids, outbox, sessiondir, store
from claude_session.errors import UsageError
from claude_session.store import Store

from .golden import assert_golden
from .outbox_env import ARTIFACT_MAX, SUMMARY_MAX, Run, listing
from .test_hookevent import SID


@pytest.fixture
def run(tmp_path: Path) -> Iterator[Run]:
    root = tmp_path / "state"
    Store.open(str(root / "state.db")).close()
    yield Run(root).setup()


def queued_request(run: Run, rid: str) -> dict[str, Any]:
    path = run.outbox / f"{rid}.json"
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
    return json.loads(path.read_text(encoding="utf-8"))


def usage(run: Run, match: str, **kwargs: Any) -> None:
    with pytest.raises(UsageError, match=match):
        run.report(**kwargs)
    assert listing(run.outbox) == []


# Grammars and names shared with modules the hook path may not import


def test_grammars_match_ids_and_sessiondir() -> None:
    assert outbox.ARTIFACT_NAME.pattern == ids.ARTIFACT_NAME.pattern
    assert outbox.SESSION_ID.pattern == ids.SESSION_ID.pattern
    assert (outbox.RUN_JSON, outbox.SECRET, outbox.OUTBOX, outbox.ARTIFACTS, outbox.SESSIONS) == (
        sessiondir.RUN_JSON,
        sessiondir.SECRET,
        sessiondir.OUTBOX,
        sessiondir.ARTIFACTS,
        sessiondir.SESSIONS,
    )
    secret = sessiondir.new_secret()
    assert outbox.secret_sha256(secret.value) == secret.sha256
    assert outbox.STATUSES == cli.REPORT_STATUSES


def test_request_ids_are_uuidv7_in_creation_order() -> None:
    made = [
        outbox.new_request_id(now_ns=lambda n=n: 1_700_000_000_000_000_000 + n * 300)
        for n in range(50)
    ]
    for rid in made:
        parsed = uuid.UUID(rid)
        assert parsed.version == 7 and parsed.variant == uuid.RFC_4122
        assert str(parsed) == rid
        assert outbox.is_request_name(f"{rid}.json")
    assert made == sorted(made)
    assert len(set(made)) == len(made)


@pytest.mark.parametrize(
    "name",
    [
        "x.json",
        "0190b6b2-1c2d-4e5f-8a8b-7c6d5e4f3a21.json",  # version 4
        "0190B6B2-1C2D-7E5F-8A8B-7C6D5E4F3A21.json",  # upper case
        "0190b6b2-1c2d-7e5f-8a8b-7c6d5e4f3a21",
        "0190b6b2-1c2d-7e5f-8a8b-7c6d5e4f3a21.json.tmp",
        "0190b6b2-1c2d-7e5f-ca8b-7c6d5e4f3a21.json",  # variant
        ".json",
        "",
    ],
)
def test_other_names_are_not_request_names(name: str) -> None:
    assert not outbox.is_request_name(name)


# Success


def test_done_with_a_summary_is_queued(run: Run) -> None:
    queued = run.report(summary="Implemented the parser. 🎉")
    assert queued.status == "done" and queued.artifact is None
    assert listing(run.outbox) == [f"{queued.request_id}.json"]
    request = queued_request(run, queued.request_id)
    assert request == {
        "v": 1,
        "run": 1,
        "secret": run.secret,
        "status": "done",
        "summary": "Implemented the parser. 🎉",
        "reason": None,
        "artifact": None,
        "artifact_sha256": None,
        "created": request["created"],
    }
    assert outbox.CREATED.fullmatch(request["created"])
    # ensure_ascii=False: the text is stored as UTF-8, not escaped
    assert "🎉".encode() in (run.outbox / f"{queued.request_id}.json").read_bytes()
    assert not [n for n in listing(run.artifacts) if n.startswith(".report-")]


def test_the_request_carries_the_run_from_run_json(tmp_path: Path) -> None:
    root = tmp_path / "state"
    Store.open(str(root / "state.db")).close()
    run = Run(root).setup(run_no=3)
    queued = run.report()
    assert queued_request(run, queued.request_id)["run"] == 3


@pytest.mark.parametrize("status", ["blocked", "failed", "needs-input"])
def test_other_statuses_need_a_reason_and_take_an_optional_summary(run: Run, status: str) -> None:
    reason = run.write_artifact("reason.md", b"Need a decision on the API.\n")
    queued = run.report(status, reason_file=reason, summary="partial")
    request = queued_request(run, queued.request_id)
    assert (request["status"], request["reason"], request["summary"]) == (
        status,
        "Need a decision on the API.\n",
        "partial",
    )


def test_summary_file_and_reason_file(run: Run) -> None:
    summary = run.write_artifact("summary.md", "Résumé\n".encode())
    reason = run.write_artifact("reason.md", b"why")
    queued = run.report("done", summary_file=summary, reason_file=reason)
    request = queued_request(run, queued.request_id)
    assert (request["summary"], request["reason"]) == ("Résumé\n", "why")


def test_summary_file_dash_reads_stdin(run: Run) -> None:
    queued = run.report("done", summary_file="-", stdin=b"from a heredoc\n")
    assert queued_request(run, queued.request_id)["summary"] == "from a heredoc\n"


def test_reason_file_dash_reads_stdin(run: Run) -> None:
    queued = run.report("blocked", reason_file="-", stdin=b"blocked on review")
    assert queued_request(run, queued.request_id)["reason"] == "blocked on review"


def test_summary_and_reason_files_may_live_anywhere(run: Run, tmp_path: Path) -> None:
    elsewhere = tmp_path / "notes.md"
    elsewhere.write_text("outside the session directory")
    queued = run.report("done", summary_file=str(elsewhere))
    assert queued_request(run, queued.request_id)["summary"] == "outside the session directory"


@pytest.mark.parametrize("form", ["name", "path", "real-path"])
def test_artifact_forms_record_the_name_and_hash(run: Run, form: str) -> None:
    data = b'{"replies": []}\n'
    path = run.write_artifact("result.json", data)
    value = {
        "name": "result.json",
        "path": path,
        "real-path": os.path.join(os.path.realpath(run.csession_dir), "artifacts", "result.json"),
    }[form]
    queued = run.report(artifact=value)
    assert queued.artifact == "result.json"
    request = queued_request(run, queued.request_id)
    assert request["artifact"] == "result.json"
    assert request["artifact_sha256"] == hashlib.sha256(data).hexdigest()


def test_an_empty_artifact_is_accepted(run: Run) -> None:
    run.write_artifact("empty.txt", b"")
    queued = run.report(artifact="empty.txt")
    assert queued_request(run, queued.request_id)["artifact_sha256"] == hashlib.sha256().hexdigest()


def test_the_caps_come_from_run_json(tmp_path: Path) -> None:
    root = tmp_path / "state"
    Store.open(str(root / "state.db")).close()
    run = Run(root).setup(summary_max=10, artifact_max=5)
    run.report(summary="x" * 10)
    with pytest.raises(UsageError, match="summary_max_bytes"):
        run.report(summary="x" * 11)
    run.write_artifact("a", b"12345")
    run.report(artifact="a")
    run.write_artifact("b", b"123456")
    with pytest.raises(UsageError, match="artifact_max_bytes"):
        run.report(artifact="b")


def test_a_summary_at_the_cap_in_multibyte_characters(run: Run) -> None:
    text = "é" * (SUMMARY_MAX // 2)  # 2 bytes each: exactly the cap
    run.report(summary=text)
    with pytest.raises(UsageError, match="summary_max_bytes"):
        run.report(summary=text + "x")


def test_an_artifact_at_the_cap(run: Run) -> None:
    run.write_artifact("big.bin", b"\0" * ARTIFACT_MAX)
    run.report(artifact="big.bin")
    run.write_artifact("bigger.bin", b"\0" * (ARTIFACT_MAX + 1))
    usage_match = "larger than artifact_max_bytes"
    with pytest.raises(UsageError, match=usage_match):
        run.report(artifact="bigger.bin")


def test_the_whole_request_is_capped(tmp_path: Path) -> None:
    """JSON escaping can make the file larger than its fields: control characters become
    six-byte escapes."""
    root = tmp_path / "state"
    Store.open(str(root / "state.db")).close()
    run = Run(root).setup(summary_max=8192)
    reason = run.write_artifact("r.md", b"\x01" * 8192)
    with pytest.raises(UsageError, match="request size limit"):
        run.report("done", summary="\x01" * 8192, reason_file=reason)


# Text validation


@pytest.mark.parametrize("text", ["", "   ", "\n\t \n"])
def test_a_blank_summary_file_is_refused_for_done(run: Run, text: str) -> None:
    path = run.write_artifact("summary.md", text.encode())
    usage(run, "summary is empty", summary_file=path)


def test_a_blank_summary_on_stdin_is_refused(run: Run) -> None:
    usage(run, "summary is empty", summary_file="-", stdin=b"  \n")


def test_a_blank_reason_is_refused(run: Run) -> None:
    path = run.write_artifact("reason.md", b"\n")
    usage(run, "reason is empty", status="blocked", reason_file=path)


def test_a_blank_optional_summary_is_refused(run: Run) -> None:
    reason = run.write_artifact("reason.md", b"r")
    usage(run, "summary is empty", status="failed", summary=" ", reason_file=reason)


def test_done_without_a_summary_is_refused(run: Run) -> None:
    with pytest.raises(UsageError, match="requires a non-empty"):
        outbox.file_report(
            status="done",
            summary=None,
            summary_file=None,
            reason_file=None,
            artifact=None,
            environ=run.environ(),
            stdin=None,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("status", ["blocked", "failed", "needs-input"])
def test_a_reason_is_required(run: Run, status: str) -> None:
    usage(run, "requires --reason-file", status=status, summary="s")


def test_both_files_on_stdin_are_refused(run: Run) -> None:
    usage(run, "both read stdin", status="blocked", summary_file="-", reason_file="-")


def test_invalid_utf8_is_refused(run: Run) -> None:
    path = run.write_artifact("summary.md", b"ok \xff\xfe")
    usage(run, "not valid UTF-8", summary_file=path)


def test_a_lone_surrogate_in_the_summary_argument_is_refused(run: Run) -> None:
    usage(run, "not valid UTF-8", summary="bad \udcff")


def test_a_nul_character_is_refused(run: Run) -> None:
    usage(run, "NUL", summary="a\0b")


def test_an_oversized_stdin_summary_is_refused(run: Run) -> None:
    usage(run, "larger than summary_max_bytes", summary_file="-", stdin=b"x" * (SUMMARY_MAX + 1))


def test_an_oversized_summary_file_is_refused(run: Run) -> None:
    path = run.write_artifact("summary.md", b"x" * (SUMMARY_MAX + 1))
    usage(run, "larger than summary_max_bytes", summary_file=path)


def test_a_symlinked_summary_file_is_refused(run: Run, tmp_path: Path) -> None:
    target = tmp_path / "secret-notes"
    target.write_text("private")
    link = run.artifacts / "summary.md"
    link.symlink_to(target)
    usage(run, "symbolic link", summary_file=str(link))


def test_a_fifo_summary_file_is_refused_without_waiting(run: Run) -> None:
    fifo = run.artifacts / "summary.md"
    os.mkfifo(fifo)
    usage(run, "not a regular file", summary_file=str(fifo))


def test_a_directory_summary_file_is_refused(run: Run, tmp_path: Path) -> None:
    usage(run, "not a regular file", summary_file=str(tmp_path))


def test_a_missing_summary_file_is_refused(run: Run, tmp_path: Path) -> None:
    usage(run, "No such file", summary_file=str(tmp_path / "missing"))


# The secret


def test_the_run_secret_in_the_summary_is_refused(run: Run) -> None:
    usage(run, "summary contains a secret", summary=f"token {run.secret} here")


def test_the_run_secret_in_the_reason_is_refused(run: Run) -> None:
    reason = run.write_artifact("reason.md", run.secret.encode())
    usage(run, "reason contains a secret", status="blocked", reason_file=reason)


def test_the_run_secret_in_the_artifact_is_refused(run: Run) -> None:
    run.write_artifact("out.txt", b"prefix " + run.secret.encode() + b" suffix")
    usage(run, "artifact contains a secret", artifact="out.txt")


@pytest.mark.parametrize("field", ["summary", "reason", "artifact"])
def test_the_run_secret_in_upper_case_is_refused(run: Run, field: str) -> None:
    upper = run.secret.upper()
    assert upper != run.secret
    if field == "summary":
        usage(run, "summary contains a secret", summary=f"token {upper} here")
    elif field == "reason":
        reason = run.write_artifact("reason.md", upper.encode())
        usage(run, "reason contains a secret", status="blocked", reason_file=reason)
    else:
        run.write_artifact("out.txt", b"prefix " + upper.encode() + b" suffix")
        usage(run, "artifact contains a secret", artifact="out.txt")


# The outbox write


def test_the_request_is_written_and_renamed_within_the_outbox(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In the sandbox outbox/ and artifacts/ are separate bind mounts: a rename between them
    fails with EXDEV, so the temporary file must live in outbox/ itself."""
    real_rename = os.rename

    def same_dir_only(
        src: Any, dst: Any, *, src_dir_fd: Any = None, dst_dir_fd: Any = None
    ) -> None:
        if src_dir_fd is not None and dst_dir_fd is not None:
            a, b = os.fstat(src_dir_fd), os.fstat(dst_dir_fd)
            if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
        real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "rename", same_dir_only)
    run.write_artifact("a.md", b"a")
    queued = run.report(artifact="a.md")
    assert listing(run.outbox) == [f"{queued.request_id}.json"]
    assert listing(run.artifacts) == ["a.md"]


def test_temporary_names_are_reserved_and_never_request_names() -> None:
    name = outbox.temp_name()
    assert outbox.is_temp_name(name) and not outbox.is_request_name(name)
    assert not outbox.is_temp_name(f"{outbox.new_request_id()}.json")
    assert not outbox.is_temp_name(".tmp-xyz")


# The session: environment, run.json, directories


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"CSESSION_ID": SID},
        {"CSESSION_DIR": "/x"},
        {"CSESSION_ID": "cs-ABCDEFGH", "CSESSION_DIR": "/x"},
        {"CSESSION_ID": "abcdefgh", "CSESSION_DIR": "/x"},
        {"CSESSION_ID": SID, "CSESSION_DIR": "relative/dir"},
        {"CSESSION_ID": SID, "CSESSION_DIR": ""},
    ],
)
def test_report_outside_a_session_is_a_usage_error(run: Run, environ: dict[str, str]) -> None:
    usage(run, "inside a claude-session session", environ=environ)


def test_csession_dir_must_match_csession_id(run: Run, tmp_path: Path) -> None:
    """Another session's directory (even a well-formed one) is refused."""
    other = "cs-zzzzzzzz"
    usage(run, "does not match", environ={"CSESSION_ID": other, "CSESSION_DIR": run.csession_dir})


def test_a_copied_session_directory_elsewhere_is_refused(run: Run, tmp_path: Path) -> None:
    """A planted directory with a run.json naming the real state root does not match its
    own realpath."""
    fake = tmp_path / "fake" / "sessions" / SID
    fake.mkdir(parents=True)
    (fake / "run.json").write_bytes((Path(run.csession_dir) / "run.json").read_bytes())
    (fake / "secret").write_text(run.secret)
    usage(run, "does not match", environ={"CSESSION_ID": SID, "CSESSION_DIR": str(fake)})


def test_csession_dir_through_a_symlink_resolves_to_the_session(run: Run, tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(run.csession_dir)
    queued = run.report(environ={"CSESSION_ID": SID, "CSESSION_DIR": str(link)})
    assert listing(run.outbox) == [f"{queued.request_id}.json"]


def test_artifact_path_through_the_symlinked_csession_dir(run: Run, tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(run.csession_dir)
    run.write_artifact("a.md", b"a")
    environ = {"CSESSION_ID": SID, "CSESSION_DIR": str(link)}
    queued = run.report(artifact=str(link / "artifacts" / "a.md"), environ=environ)
    assert queued.artifact == "a.md"


def test_the_real_path_form_of_a_symlinked_csession_dir_is_refused(
    run: Run, tmp_path: Path
) -> None:
    """The lexical parent must be exactly $CSESSION_DIR/artifacts: no symlink resolution."""
    link = tmp_path / "link"
    link.symlink_to(run.csession_dir)
    run.write_artifact("a.md", b"a")
    real = os.path.join(os.path.realpath(run.csession_dir), "artifacts", "a.md")
    environ = {"CSESSION_ID": SID, "CSESSION_DIR": str(link)}
    usage(run, r"\$CSESSION_DIR/artifacts/<name>", artifact=real, environ=environ)


def test_repeated_slashes_in_the_artifact_path_are_collapsed(run: Run) -> None:
    run.write_artifact("a.md", b"a")
    queued = run.report(artifact=run.csession_dir + "//artifacts///a.md")
    assert queued.artifact == "a.md"


def write_run_json(run: Run, doc: Any) -> None:
    path = Path(run.csession_dir) / "run.json"
    path.unlink()
    path.write_bytes(doc if isinstance(doc, bytes) else json.dumps(doc).encode())


def run_json(run: Run) -> dict[str, Any]:
    return json.loads((Path(run.csession_dir) / "run.json").read_text())


@pytest.mark.parametrize(
    "change",
    [
        {"csession_id": "cs-zzzzzzzz"},
        {"run": 0},
        {"run": True},
        {"run": "1"},
        {"state_root": "relative"},
        {"summary_max_bytes": 0},
        {"artifact_max_bytes": -1},
        {"outbox": {"dev": 1}},
        {"artifacts": {"dev": 1, "ino": "2"}},
        {"outbox": None},
    ],
)
def test_a_bad_run_json_is_refused(run: Run, change: dict[str, Any]) -> None:
    write_run_json(run, {**run_json(run), **change})
    with pytest.raises(UsageError):
        run.report()
    assert listing(run.outbox) == []


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b"\xff"])
def test_a_garbled_run_json_is_refused(run: Run, raw: bytes) -> None:
    write_run_json(run, raw)
    usage(run, "run.json")


def test_another_state_root_in_run_json_is_refused(run: Run, tmp_path: Path) -> None:
    write_run_json(run, {**run_json(run), "state_root": str(tmp_path / "elsewhere")})
    usage(run, "does not match")


def test_a_missing_run_json_is_refused(run: Run) -> None:
    (Path(run.csession_dir) / "run.json").unlink()
    usage(run, "run.json is missing")


def test_a_symlinked_run_json_is_refused(run: Run, tmp_path: Path) -> None:
    path = Path(run.csession_dir) / "run.json"
    copy = tmp_path / "run.json"
    copy.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(copy)
    usage(run, "run.json is not a regular")


def test_a_hard_linked_run_json_is_refused(run: Run, tmp_path: Path) -> None:
    os.link(Path(run.csession_dir) / "run.json", tmp_path / "extra-link")
    usage(run, "run.json is not a regular")


@pytest.mark.parametrize("content", [b"", b"short", b"A" * 64, b"a" * 65])
def test_a_bad_secret_file_is_refused(run: Run, content: bytes) -> None:
    path = Path(run.csession_dir) / "secret"
    path.unlink()
    path.write_bytes(content)
    with pytest.raises(UsageError, match="secret"):
        run.report()


@pytest.mark.parametrize("name", ["outbox", "artifacts"])
def test_a_replaced_directory_is_refused(run: Run, name: str) -> None:
    """The agent cannot swap outbox/ or artifacts/ for another directory (dev/ino in
    run.json)."""
    target = Path(run.csession_dir) / name
    target.rename(Path(run.csession_dir) / f"{name}.old")
    target.mkdir()
    with pytest.raises(UsageError, match=f"{name} is not the directory"):
        run.report()
    assert listing(target) == []


@pytest.mark.parametrize("name", ["outbox", "artifacts"])
def test_a_symlinked_directory_is_refused(run: Run, name: str) -> None:
    target = Path(run.csession_dir) / name
    moved = Path(run.csession_dir) / f"{name}.real"
    target.rename(moved)
    target.symlink_to(moved)  # the same directory, through a symlink
    with pytest.raises(UsageError, match=f"{name} is not the directory"):
        run.report()
    assert listing(moved) == [] or name == "artifacts"


# Artifacts


@pytest.mark.parametrize(
    "value",
    [
        "../run.json",
        "..",
        ".",
        "a b",
        "x" * 129,
        "subdir/a.md",
        "/etc/passwd",
        "artifacts/a.md",  # relative: not $CSESSION_DIR/artifacts
    ],
)
def test_an_artifact_outside_artifacts_or_misnamed_is_refused(run: Run, value: str) -> None:
    run.write_artifact("a.md", b"a")
    with pytest.raises(UsageError, match="--artifact"):
        run.report(artifact=value)


def test_an_artifact_path_with_dot_dot_is_refused(run: Run) -> None:
    run.write_artifact("a.md", b"a")
    value = f"{run.csession_dir}/outbox/../artifacts/a.md"
    with pytest.raises(UsageError, match="artifacts/<name>"):
        run.report(artifact=value)


def test_an_artifact_path_in_a_sibling_session_is_refused(run: Run, tmp_path: Path) -> None:
    other = Path(run.root) / "sessions" / "cs-zzzzzzzz" / "artifacts"
    other.mkdir(parents=True)
    (other / "a.md").write_text("a")
    with pytest.raises(UsageError, match="artifacts/<name>"):
        run.report(artifact=str(other / "a.md"))


def test_a_missing_artifact_is_refused(run: Run) -> None:
    usage(run, "no such file", artifact="missing.md")


def test_a_symlinked_artifact_is_refused(run: Run, tmp_path: Path) -> None:
    secret = tmp_path / "id_rsa"
    secret.write_text("private key")
    (run.artifacts / "out.md").symlink_to(secret)
    usage(run, "not a regular file", artifact="out.md")


def test_a_hard_linked_artifact_is_refused(run: Run, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("x")
    os.link(target, run.artifacts / "out.md")
    usage(run, "single link", artifact="out.md")


def test_a_fifo_artifact_is_refused_without_waiting(run: Run) -> None:
    os.mkfifo(run.artifacts / "out.md")
    usage(run, "not a regular file", artifact="out.md")


def test_a_directory_artifact_is_refused(run: Run) -> None:
    (run.artifacts / "out.md").mkdir()
    usage(run, "not a regular file", artifact="out.md")


# The command line


def test_cli_report_prints_report_queued(
    run: Run, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in run.environ().items():
        monkeypatch.setenv(key, value)
    assert cli.main(["report", "done", "--summary", "done"]) == 0
    assert capsys.readouterr().out == "report queued\n"
    assert len(listing(run.outbox)) == 1


def test_cli_report_json_matches_the_schema(
    run: Run, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in run.environ().items():
        monkeypatch.setenv(key, value)
    run.write_artifact("result.md", b"# Result\n")
    argv = ["report", "done", "--summary=-starts with a dash", "--artifact", "result.md", "--json"]
    assert cli.main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    (name,) = listing(run.outbox)
    assert out["request_id"] == name.removesuffix(".json")
    assert queued_request(run, out["request_id"])["summary"] == "-starts with a dash"
    assert_golden(out, "report-artifact", normalize=lambda o: {**o, "request_id": "<uuid>"})


def test_cli_report_usage_error_envelope(
    run: Run, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in run.environ().items():
        monkeypatch.setenv(key, value)
    assert cli.main(["report", "done", "--summary", "x", "--artifact", "nope", "--json"]) == 2
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["error"]["code"] == "usage"


def test_report_never_opens_state_db(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    """report reads run.json only: an unusable or newer state.db does not matter."""
    with sqlite3.connect(run.db) as conn:
        conn.execute("UPDATE meta SET value = '99.0' WHERE key = 'schema_version'")

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("report opened state.db")

    monkeypatch.setattr(sqlite3, "connect", refuse)
    monkeypatch.setattr(store.Store, "open", refuse)
    for key, value in run.environ().items():
        monkeypatch.setenv(key, value)
    assert cli.main(["report", "done", "--summary", "x"]) == 0
    assert len(listing(run.outbox)) == 1


def test_report_works_without_a_state_db(run: Run) -> None:
    os.unlink(run.db)
    run.report()
    assert not os.path.exists(run.db)


def test_report_as_a_process(run: Run) -> None:
    """The real entry point, with the heredoc form."""
    env = {**os.environ, **run.environ()}
    proc = subprocess.run(
        [sys.executable, "-m", "claude_session", "report", "done", "--summary-file", "-"],
        input=b"Done via heredoc\n",
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b"report queued\n"
    (name,) = listing(run.outbox)
    assert queued_request(run, name.removesuffix(".json"))["summary"] == "Done via heredoc\n"
