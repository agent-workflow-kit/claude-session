"""A real session layout for the report and ingestion tests: the session and private
directories as ``exec`` prepares them (``SessionDirs``), ``run.json``, the secret, the run row
with its secret digest and directory identities, ``hookconf.json`` and ``secret-index``."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

from claude_session import hookevent, outbox, secretindex, sessiondir
from claude_session.fsutil import write_private_file
from claude_session.store import Store

from .test_hookevent import SID, Env

SUMMARY_MAX = 1024
ARTIFACT_MAX = 4096
CLAUDE_SESSION = "/opt/claude-session/bin/claude-session"


class Run(Env):
    """``Env`` plus the session directory layout of run ``run_no``."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.secret = ""
        self.run_no = 1

    def setup(
        self,
        *,
        run_no: int = 1,
        state: str = "working",
        activity: str | None = "working",
        summary_max: int = SUMMARY_MAX,
        artifact_max: int = ARTIFACT_MAX,
        record: bool = True,
        secrets: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> Run:
        """``secrets``: the configured secret values indexed with the run secret."""
        self.run_no = run_no
        self.add(run_no=run_no, state=state, activity=activity, started=True, **kwargs)
        self.prepare(
            run_no,
            summary_max=summary_max,
            artifact_max=artifact_max,
            record=record,
            secrets=secrets,
        )
        return self

    def prepare(
        self,
        run_no: int,
        *,
        summary_max: int,
        artifact_max: int,
        record: bool = True,
        secrets: tuple[str, ...] = (),
    ) -> None:
        with sessiondir.SessionDirs.open(str(self.root), SID) as dirs:
            layout = dirs.prepare_run(run_no)
            secret = dirs.write_run_files(
                run_no,
                claude_session_path=CLAUDE_SESSION,
                summary_max_bytes=summary_max,
                artifact_max_bytes=artifact_max,
                input_files=[],
            )
        self.secret = secret.value
        self.write_conf(run_no, summary_max=summary_max, artifact_max=artifact_max)
        self.write_index(run_no, (*secrets, self.secret))
        if record:
            with Store.open(self.db) as store:
                store.record_run_files(
                    SID,
                    run_no,
                    secret_sha256=secret.sha256,
                    outbox_dev_ino=layout.outbox.text,
                    artifacts_dev_ino=layout.artifacts.text,
                )

    def write_conf(self, run_no: int, *, summary_max: int, artifact_max: int) -> None:
        run_dir = Path(self.private_dir) / "runs" / str(run_no)
        data = hookevent.hookconf_bytes(
            [], summary_max_bytes=summary_max, artifact_max_bytes=artifact_max
        )
        write_private_file(str(run_dir / "hookconf.json"), data)

    def write_index(self, run_no: int, values: tuple[str, ...]) -> None:
        """What exec writes at launch (#15): the run's secret-index."""
        run_dir = Path(self.private_dir) / "runs" / str(run_no)
        write_private_file(str(run_dir / secretindex.INDEX_NAME), secretindex.build(values))

    # Paths

    @property
    def outbox(self) -> Path:
        return Path(self.csession_dir) / "outbox"

    @property
    def artifacts(self) -> Path:
        return Path(self.csession_dir) / "artifacts"

    @property
    def private_run(self) -> Path:
        return Path(self.private_dir) / "runs" / str(self.run_no)

    @property
    def claimed(self) -> Path:
        return self.private_run / "claimed"

    def environ(self) -> dict[str, str]:
        return {"CSESSION_ID": SID, "CSESSION_DIR": self.csession_dir}

    # The agent's side

    def report(
        self,
        status: str = "done",
        *,
        summary: str | None = None,
        summary_file: str | None = None,
        reason_file: str | None = None,
        artifact: str | None = None,
        stdin: bytes = b"",
        environ: dict[str, str] | None = None,
    ) -> outbox.Queued:
        if status == "done" and summary is None and summary_file is None:
            summary = "all done"
        return outbox.file_report(
            status=status,
            summary=summary,
            summary_file=summary_file,
            reason_file=reason_file,
            artifact=artifact,
            environ=self.environ() if environ is None else environ,
            stdin=io.BytesIO(stdin),
        )

    def write_artifact(self, name: str, data: bytes) -> str:
        path = self.artifacts / name
        path.write_bytes(data)
        return str(path)

    def request(self, **fields: Any) -> dict[str, Any]:
        """A valid request document for the current run, with ``fields`` replaced."""
        doc: dict[str, Any] = {
            "v": 1,
            "run": self.run_no,
            "secret": self.secret,
            "status": "done",
            "summary": "done it",
            "reason": None,
            "artifact": None,
            "artifact_sha256": None,
            "created": "2026-10-30T00:00:00.000000Z",
        }
        doc.update(fields)
        return doc

    def plant(self, doc: Any = None, *, raw: bytes | None = None, rid: str | None = None) -> str:
        """Write a request file into the outbox directly (as a hostile agent could)."""
        rid = rid or outbox.new_request_id()
        data = raw if raw is not None else json.dumps(doc or self.request()).encode()
        (self.outbox / f"{rid}.json").write_bytes(data)
        return rid

    # The hooks

    def post_tool_use(self) -> str:
        return self.hook("PostToolUse", {"hook_event_name": "PostToolUse"})

    def stop_hook(self) -> str:
        return self.hook("Stop", {"hook_event_name": "Stop", "stop_hook_active": False})

    # state.db

    def requests(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM requests ORDER BY request_id", ())

    def report_events(self) -> list[dict[str, Any]]:
        return [e for e in self.events() if e["cause"] == "report"]

    def log(self) -> list[dict[str, Any]]:
        path = self.root / "log.jsonl"
        if not path.exists():
            return []
        records = [json.loads(line) for line in path.read_text().splitlines()]
        # The store logs its own migration when it creates the database (not a drain record).
        return [r for r in records if r["event"] != "migration"]


def rejection(output: str) -> str:
    """The rejection code of a PostToolUse output."""
    parsed = json.loads(output)
    assert parsed["decision"] == "block"
    reason = parsed["reason"]
    assert reason.startswith("claude-session report rejected (")
    assert reason.endswith(" Fix the problem and file the report again.")
    return reason.split("(", 1)[1].split(")", 1)[0]


def listing(path: Path) -> list[str]:
    return sorted(os.listdir(path)) if path.exists() else []
