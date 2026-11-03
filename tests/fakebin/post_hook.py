"""A stub ``post-state-change`` hook for the toolhooks tests.

    python -I post_hook.py --log <file> [--exit N] [--sleep S] [--ignore-term]
        [--child] [--wait-for <file>] [--stderr-bytes N] [--name NAME]

Writes its start record (as the fake ``claude`` does: an empty ``<pid>.<start ticks>`` file in
``$FAKE_CLAUDE_STARTED_DIR``, so the suite ends and reports a hook that outlives its test),
reads the payload from stdin and appends one JSON line to ``--log``: ``name``, ``pid``,
``pgid``, ``cwd``, ``time`` (``time.time()`` once the payload was read), the parsed
``payload``, ``env`` (every variable) and, with ``--child``, the PID of a child it started in
its own process group that sleeps forever (``child``; also with a start record). Then:
``--ignore-term`` ignores SIGTERM; waits until ``--wait-for`` exists (at most 20 s); sleeps
``--sleep``; writes ``--stderr-bytes`` bytes to stderr and stdout; exits ``--exit``.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time

STARTED_ENV = "FAKE_CLAUDE_STARTED_DIR"
WAIT_LIMIT = 20.0


def start_record(pid: int) -> None:
    directory = os.environ.get(STARTED_ENV)
    if not directory or not sys.platform.startswith("linux"):
        return
    with open(f"/proc/{pid}/stat", "rb") as fh:
        ticks = int(fh.read().rpartition(b")")[2].split()[19])
    open(os.path.join(directory, f"{pid}.{ticks}"), "w").close()


def append(path: str, record: dict[str, object]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True)
    parser.add_argument("--name", default="hook")
    parser.add_argument("--exit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--ignore-term", action="store_true")
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--wait-for")
    parser.add_argument("--stderr-bytes", type=int, default=0)
    opts = parser.parse_args(argv)
    start_record(os.getpid())
    if opts.ignore_term:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = None
    if opts.child:
        # Stays in this process group (no setsid): the group kill must reach it.
        child = subprocess.Popen(
            [sys.executable, "-I", "-c", "import time\nwhile True: time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        start_record(child.pid)
    raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    try:
        payload: object = json.loads(raw)
    except ValueError:
        payload = raw
    append(
        opts.log,
        {
            "name": opts.name,
            "pid": os.getpid(),
            "pgid": os.getpgid(0),
            "cwd": os.getcwd(),
            "time": time.time(),
            "payload": payload,
            "env": dict(os.environ),
            "child": None if child is None else child.pid,
        },
    )
    if opts.wait_for:
        deadline = time.monotonic() + WAIT_LIMIT
        while not os.path.exists(opts.wait_for) and time.monotonic() < deadline:
            time.sleep(0.02)
    deadline = time.monotonic() + opts.sleep
    while (left := deadline - time.monotonic()) > 0:
        time.sleep(left)
    if opts.stderr_bytes:
        sys.stderr.write("e" * opts.stderr_bytes)
        sys.stdout.write("o" * opts.stderr_bytes)
    return opts.exit


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
