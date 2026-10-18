"""FalkorDB connection helpers. One embedded database, many graphs.

Graph naming:
  canon_<workspace>_<hash>   the real code graph (read-mostly)
  uni_<runid>_<n>            temporary "universe" clones (always deleted)
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import socket
import threading
import time
from pathlib import Path

from redislite.falkordb_client import FalkorDB

log = logging.getLogger("preflight.db")

UNIVERSE_PREFIX = "uni_"
_lock = threading.Lock()
_db: FalkorDB | None = None


def db_path() -> str:
    """Where the embedded database file lives. Override with PREFLIGHT_DB."""
    default = Path(__file__).resolve().parent.parent / ".data" / "falkor.db"
    path = Path(os.environ.get("PREFLIGHT_DB", str(default))).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _listening(port: int) -> bool:
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def open_database(path: str, port: str | None) -> FalkorDB:
    """Open the embedded database; with `port`, also listen on 127.0.0.1:port (for the FalkorDB Browser).

    Claude Desktop KILLS the MCP server, which can leave the embedded database server running. The next start
    then attaches to that old server and silently ignores the requested port. So: if the port is not open,
    shut the leftover server down (it saves first) and start a new one with the port."""
    if not port:
        return FalkorDB(path)
    cfg = {"port": str(port), "bind": "127.0.0.1"}
    db = FalkorDB(path, serverconfig=cfg)
    if not _listening(int(port)):
        log.warning("database server was started without port %s (left over from a killed session): restarting it", port)
        try:
            db.execute_command("SHUTDOWN", "SAVE")
        except Exception:  # noqa: BLE001   the connection drops as the server stops: expected
            pass
        time.sleep(0.5)
        db = FalkorDB(path, serverconfig=cfg)
        if not _listening(int(port)):
            log.error("port %s is still not open (is it used by another program?)", port)
    return db


def get_db() -> FalkorDB:
    """Open the database once and reuse it (lazy, thread-safe)."""
    global _db
    with _lock:
        if _db is None:
            port = os.environ.get("PREFLIGHT_PORT")     # lets the FalkorDB Browser connect (localhost only)
            log.info("opening FalkorDB at %s%s", db_path(), f" (TCP port {port})" if port else "")
            try:
                _db = open_database(db_path(), port)
            except Exception:  # noqa: BLE001   e.g. the port is taken: keep working without it
                log.exception("could not open TCP port %s; continuing without it", port)
                _db = FalkorDB(db_path())
        return _db


def canon_name(workspace: Path) -> str:
    """Stable graph key for a workspace folder."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", workspace.name).strip("_") or "ws"
    digest = hashlib.sha1(str(workspace).encode()).hexdigest()[:8]
    return f"canon_{slug}_{digest}"


def list_graph_names() -> list[str]:
    return [n.decode() if isinstance(n, bytes) else n for n in get_db().list_graphs()]


def graph_exists(name: str) -> bool:
    return name in list_graph_names()


def sweep_universes() -> int:
    """Delete leftover universe graphs (for example after a crash)."""
    removed = 0
    for name in list_graph_names():
        if name.startswith(UNIVERSE_PREFIX):
            try:
                get_db().select_graph(name).delete()
                removed += 1
            except Exception:  # noqa: BLE001
                log.exception("could not delete leftover graph %s", name)
    return removed
