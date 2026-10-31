"""A stub hook or command for tests of the fake ``claude``: records how it was called and answers
as told.

    python -I record_hook.py --log <file> [--stdout T] [--stderr T] [--exit N] [--sleep S]
        [--ignore-term] -- [recorded args...]

Appends one JSON line to ``--log``: ``args`` (after ``--``), ``stdin`` (text), ``cwd``, ``pid``,
``pgid`` and the ``CSESSION_*`` and ``FAKE_*`` environment variables. With ``--sleep`` it then
sleeps (``--ignore-term`` ignores SIGTERM meanwhile), and finally prints ``--stdout`` and
``--stderr`` and exits ``--exit``. Standard library only; used as an injected hook command (in
place of ``claude-session hook-event``) and as a stand-in ``claude-session`` for ``report``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True)
    parser.add_argument("--stdout", default="")
    parser.add_argument("--stderr", default="")
    parser.add_argument("--exit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--ignore-term", action="store_true")
    parser.add_argument("args", nargs="*")
    opts = parser.parse_args(argv)
    stdin = sys.stdin.buffer.read().decode("utf-8", "replace")
    record = {
        "args": opts.args,
        "stdin": stdin,
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "pgid": os.getpgid(0),
        "env": {k: v for k, v in os.environ.items() if k.startswith(("CSESSION_", "FAKE_"))},
    }
    fd = os.open(opts.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    if opts.ignore_term:
        signal.signal(signal.SIGTERM, lambda _s, _f: None)
    deadline = time.monotonic() + opts.sleep
    while (left := deadline - time.monotonic()) > 0:
        time.sleep(left)
    sys.stdout.write(opts.stdout)
    sys.stderr.write(opts.stderr)
    return opts.exit


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
