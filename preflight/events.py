"""Live event log. The MCP server appends one JSON line per event; the viewer streams the file.

A file (not a socket) is used on purpose: the MCP server is started by Claude Desktop, and the viewer is a
separate process you start yourself. They only need to agree on a path (env PREFLIGHT_EVENTS).
Writing an event must never break a tool call, so every error is swallowed.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

CURRENT: dict[str, str | None] = {"call": None}   # id of the tool call in progress (one client at a time)
_lock = threading.Lock()


def events_path() -> Path:
    return Path(os.environ.get("PREFLIGHT_EVENTS", "~/.preflight/events.jsonl")).expanduser()


def emit(kind: str, **data) -> None:
    try:
        rec = {"ts": round(time.time() * 1000), "kind": kind, "pid": os.getpid()}
        if CURRENT["call"] and "call" not in data:
            rec["call"] = CURRENT["call"]
        rec.update(data)
        line = json.dumps(rec, default=str)
        p = events_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with _lock, p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def rotate(max_bytes: int = 2_000_000, keep_lines: int = 1000) -> None:
    """Keep the log small: when it is big, keep only the newest lines."""
    try:
        p = events_path()
        if p.exists() and p.stat().st_size > max_bytes:
            lines = p.read_text(encoding="utf-8").splitlines()[-keep_lines:]
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
