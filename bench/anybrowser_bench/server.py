"""Serves task pages and records what is submitted to them.

State tasks are graded here rather than in the page. A form POST that reached
the server is a fact no agent can talk its way into; a page-side flag, or the
agent's own report of what it typed, is not.

Each mounted instance gets its own path prefix, so one server can host a whole
suite and submissions never cross between attempts.
"""

from __future__ import annotations

import http.server
import itertools
import threading
from collections import defaultdict
from typing import Any
from urllib.parse import parse_qsl

from .task import Instance, Submission

__all__ = ["BenchServer"]

_RECEIVED = (
    "<!doctype html><meta charset='utf-8'><title>Received</title>"
    "<h1>Thanks</h1><p>Your submission was received.</p>"
)


class BenchServer:
    def __init__(self) -> None:
        self._pages: dict[str, dict[str, str]] = {}
        self._submissions: dict[str, list[Submission]] = defaultdict(list)
        self._lock = threading.Lock()
        self._ids = itertools.count()
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def __enter__(self) -> BenchServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def mount(self, instance: Instance) -> str:
        """Host an instance's pages. Returns its root URL, ending in '/'."""
        key = f"{instance.task_id}-{instance.seed}-{next(self._ids)}"
        with self._lock:
            self._pages[key] = dict(instance.pages)
        return f"{self.url}/t/{key}/"

    def submissions(self, root: str) -> list[Submission]:
        key = root.rstrip("/").rsplit("/", 1)[-1]
        with self._lock:
            return list(self._submissions[key])

    def _handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        bench = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _route(self) -> tuple[str, str] | None:
                parts = self.path.split("?", 1)[0].split("/", 3)
                if len(parts) < 4 or parts[1] != "t":
                    return None
                return parts[2], parts[3] or "index.html"

            def do_GET(self) -> None:
                route = self._route()
                with bench._lock:
                    page = bench._pages.get(route[0], {}).get(route[1]) if route else None
                if page is None:
                    self._send(404, "<h1>Not found</h1>")
                else:
                    self._send(200, page)

            def do_POST(self) -> None:
                route = self._route()
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8", "replace")
                if route is None or route[0] not in bench._pages:
                    self._send(404, "<h1>Not found</h1>")
                    return
                with bench._lock:
                    bench._submissions[route[0]].append(
                        Submission(path=route[1], fields=dict(parse_qsl(body)))
                    )
                self._send(200, _RECEIVED)

            def _send(self, status: int, html: str) -> None:
                data = html.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        return Handler
