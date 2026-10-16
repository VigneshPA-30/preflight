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
import threading
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


def get_db() -> FalkorDB:
    """Open the database once and reuse it (lazy, thread-safe)."""
    global _db
    with _lock:
        if _db is None:
            log.info("opening FalkorDB at %s", db_path())
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
