"""HTTP API + dashboard server (stdlib only).

Routes:
  GET  /                 dashboard page
  GET  /api/status       file offsets, open events, counters
  GET  /api/events       event timeline (?source=&level=&limit=)
  GET  /api/errors       dead-letter parse errors
  GET  /api/replay       last replay diff (if any)
  POST /api/poll         force one poll cycle
  POST /api/replay       run replay {"apply": bool, "overrides": {...}}
  POST /api/flush        force-close open events
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .collector import LogCollector
from .replay import public_diff, replay_errors

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


class _State:
    def __init__(self, collector: LogCollector):
        self.collector = collector
        self.lock = threading.Lock()
        self.last_replay: Optional[dict] = None
        self.last_replay_path = os.path.join(collector.state_dir, "last_replay.json")
        if os.path.exists(self.last_replay_path):
            try:
                with open(self.last_replay_path, encoding="utf-8") as fh:
                    self.last_replay = json.load(fh)
            except (json.JSONDecodeError, OSError):
                self.last_replay = None

    def run_replay(self, apply: bool, overrides: dict) -> dict:
        with self.lock:
            col = self.collector
            diff = replay_errors(col.errors.unresolved(),
                                 col.cfg.assembler_config(), overrides)
            if apply:
                recovered_ids = []
                for res in diff["results"]:
                    if res["status"] != "recovered":
                        continue
                    for ev in res["_events"]:
                        col.events.add(ev)  # idempotent by event_id
                    recovered_ids.append(res["error_id"])
                col.errors.mark_resolved(recovered_ids, diff["replay_id"])
                col.save_checkpoint()
            self.last_replay = public_diff(diff)
            with open(self.last_replay_path, "w", encoding="utf-8") as fh:
                json.dump(self.last_replay, fh, ensure_ascii=False)
            return self.last_replay


def make_handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        server_version = "logcollector/0.1"

        # -- helpers ------------------------------------------------------
        def _json(self, obj, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return {}
            if n <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}

        def log_message(self, fmt, *args):  # quieter logs
            pass

        # -- routes -------------------------------------------------------
        def do_GET(self) -> None:
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            col = state.collector
            if u.path == "/" or u.path == "/index.html":
                self._serve_index()
            elif u.path == "/api/status":
                with state.lock:
                    self._json(col.status())
            elif u.path == "/api/events":
                with state.lock:
                    self._json({
                        "events": col.events.timeline(
                            source=qs.get("source", [None])[0],
                            level=qs.get("level", [None])[0],
                            limit=int(qs.get("limit", ["1000"])[0])),
                    })
            elif u.path == "/api/errors":
                with state.lock:
                    errs = sorted(col.errors.all(),
                                  key=lambda e: e["detected_ms"])
                    self._json({"errors": errs})
            elif u.path == "/api/replay":
                self._json({"replay": state.last_replay})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            u = urlparse(self.path)
            col = state.collector
            if u.path == "/api/poll":
                with state.lock:
                    self._json(col.poll())
            elif u.path == "/api/flush":
                with state.lock:
                    self._json(col.flush_all(reason="manual"))
            elif u.path == "/api/replay":
                body = self._body()
                diff = state.run_replay(bool(body.get("apply")),
                                        body.get("overrides") or {})
                self._json({"replay": diff})
            else:
                self._json({"error": "not found"}, 404)

        def _serve_index(self) -> None:
            path = os.path.join(WEB_DIR, "index.html")
            try:
                with open(path, "rb") as fh:
                    body = fh.read()
            except OSError:
                self._json({"error": "dashboard missing"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(collector: LogCollector, host: str, port: int,
          poll_interval: float = 1.0) -> ThreadingHTTPServer:
    state = _State(collector)
    server = ThreadingHTTPServer((host, port), make_handler(state))

    if poll_interval > 0:
        def _loop() -> None:
            while True:
                time.sleep(poll_interval)
                try:
                    with state.lock:
                        collector.poll()
                except Exception:
                    pass  # a failed poll must not kill the server

        threading.Thread(target=_loop, daemon=True).start()
    return server
