"""A minimal MCP server for the elicitation live test (stdio, standard library only).

It offers one tool, `ask`. Calling it sends the client an `elicitation/create` request and
waits for the answer; the tool result reports the action the user took (`accept`, `decline`,
`cancel`). Nothing else is implemented: no resources, no prompts, no logging. It writes one line
per event to the file named by `ELICIT_SERVER_LOG` (if set) so a test can see what happened.

Run as `python mcp_elicit_server.py`; `tests/test_live_harness.py` drives it with a fake client.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
QUESTION = "Which color should the report use?"
TOOL = {
    "name": "ask",
    "description": "Ask the user which color to use. Takes no arguments.",
    "inputSchema": {"type": "object", "properties": {}},
}


def log(line: str) -> None:
    path = os.environ.get("ELICIT_SERVER_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def read() -> dict[str, Any] | None:
    """The next JSON object from stdin; None at end of input."""
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                return value


def elicit(request_id: int) -> str:
    """Send the elicitation request and return the action of the matching response."""
    send(
        {
            "jsonrpc": "2.0",
            "id": f"elicit-{request_id}",
            "method": "elicitation/create",
            "params": {
                "message": QUESTION,
                "requestedSchema": {
                    "type": "object",
                    "properties": {"color": {"type": "string", "title": "Color"}},
                    "required": ["color"],
                },
            },
        }
    )
    log("elicitation sent")
    while (message := read()) is not None:
        if message.get("id") == f"elicit-{request_id}":
            result = message.get("result")
            action = result.get("action") if isinstance(result, dict) else None
            return str(action or "error")
    return "closed"


def handle(message: dict[str, Any]) -> None:
    method, ident = message.get("method"), message.get("id")
    if ident is None:  # a notification (initialized, cancelled): no answer
        return
    if method == "initialize":
        params = message.get("params") or {}
        version = params.get("protocolVersion") or PROTOCOL_VERSION
        log(f"initialize capabilities={json.dumps(params.get('capabilities'), sort_keys=True)}")
        send(
            {
                "jsonrpc": "2.0",
                "id": ident,
                "result": {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "elicit", "version": "1"},
                },
            }
        )
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": ident, "result": {"tools": [TOOL]}})
    elif method == "tools/call":
        action = elicit(ident if isinstance(ident, int) else 0)
        log(f"answered {action}")
        text = f"The user's action was: {action}"
        send(
            {"jsonrpc": "2.0", "id": ident, "result": {"content": [{"type": "text", "text": text}]}}
        )
    else:
        send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "not found"}})


def main() -> int:
    while (message := read()) is not None:
        handle(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
