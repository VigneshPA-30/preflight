"""Live view of what Preflight is doing while an agent uses it. Start it on the side:

    uv run python -m preflight.viewer            # then open http://localhost:8765

Standard library only. It tails the event log the MCP server writes (PREFLIGHT_EVENTS) and, when
PREFLIGHT_PORT is set, lists the graphs that exist in FalkorDB right now (as a plain TCP client:
it never opens or owns the database, so closing it cannot hurt the MCP server).
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from preflight.events import events_path

PAGE = Path(__file__).with_name("viewer.html")
_client = None
_client_lock = threading.Lock()


def graph_listing() -> dict:
    """Graphs in FalkorDB right now, through the TCP port (so clones show up while they exist)."""
    global _client
    port = os.environ.get("PREFLIGHT_PORT")
    if not port:
        return {"connected": False, "reason": "set PREFLIGHT_PORT (same value as the MCP server) to list graphs"}
    try:
        with _client_lock:
            if _client is None:
                import falkordb
                _client = falkordb.FalkorDB(host="127.0.0.1", port=int(port))
            names = _client.list_graphs()
            out = []
            for name in names:
                g = _client.select_graph(name)
                n = g.ro_query("MATCH (n) RETURN count(n)").result_set[0][0]
                e = g.ro_query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
                out.append({"name": name, "nodes": n, "edges": e,
                            "kind": "clone" if name.startswith("uni_") else ("memory" if name.startswith("mem_") else "code graph")})
        out.sort(key=lambda g: ({"code graph": 0, "memory": 1, "clone": 2}[g["kind"]], g["name"]))
        return {"connected": True, "graphs": out}
    except Exception as exc:  # noqa: BLE001
        _client = None
        return {"connected": False, "reason": f"{type(exc).__name__}: {str(exc)[:160]}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "PreflightViewer"

    def log_message(self, *args) -> None:   # keep the terminal quiet
        pass

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        if path in {"/", "/index.html"}:
            self._send(200, "text/html; charset=utf-8", PAGE.read_bytes())
        elif path == "/api/graphs":
            self._send(200, "application/json", json.dumps(graph_listing()).encode())
        elif path == "/api/info":
            info = {"events_file": str(events_path()), "db_port": os.environ.get("PREFLIGHT_PORT"),
                    "browser_url": os.environ.get("PREFLIGHT_BROWSER_URL", "http://localhost:3000")}
            self._send(200, "application/json", json.dumps(info).encode())
        elif path == "/stream":
            replay = 300
            for part in query.split("&"):
                if part.startswith("replay="):
                    replay = int(part[7:] or 0)
            self._stream(replay)
        else:
            self._send(404, "text/plain", b"not found")

    def _stream(self, replay: int) -> None:
        """Server-Sent Events: the last `replay` events, then every new line of the event log."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        path = events_path()
        offset, last_beat = 0, time.time()
        try:
            if path.exists():
                data = path.read_bytes()
                offset = len(data)
                for line in data.splitlines()[-replay:] if replay else []:
                    self.wfile.write(b"data: " + line + b"\n\n")
                self.wfile.flush()
            while True:
                if path.exists():
                    size = path.stat().st_size
                    if size < offset:          # the log was rotated or cleared
                        offset = 0
                    if size > offset:
                        with path.open("rb") as f:
                            f.seek(offset)
                            chunk = f.read()
                        complete = chunk[: chunk.rfind(b"\n") + 1]      # never send half a line
                        offset += len(complete)
                        for line in complete.splitlines():
                            self.wfile.write(b"data: " + line + b"\n\n")
                        self.wfile.flush()
                if time.time() - last_beat > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_beat = time.time()
                time.sleep(0.15)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def make_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


def main() -> None:
    ap = argparse.ArgumentParser(description="Live view of Preflight's MCP calls and FalkorDB clones.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--clear", action="store_true", help="empty the event log first")
    a = ap.parse_args()
    if a.clear and events_path().exists():
        events_path().write_text("")
    srv = make_server(a.host, a.port)
    print(f"Preflight viewer:  http://localhost:{a.port}")
    print(f"  events file:     {events_path()}")
    print(f"  FalkorDB port:   {os.environ.get('PREFLIGHT_PORT') or 'not set (graph list disabled)'}")
    print("  Ctrl+C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
