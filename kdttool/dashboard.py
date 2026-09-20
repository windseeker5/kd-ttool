"""Live dashboard for a UAT run — a local page you watch while it runs.

Deliberately stdlib-only (http.server on a daemon thread): this tool should
never pull in Flask or fight the app under test for a port. The page polls
/events every 500ms rather than using SSE — the event volume is tiny and a poll
survives the run process being busy inside a blocking Playwright call.

Bound to 127.0.0.1 only. It serves screenshots off local disk and there is no
reason for anything off this machine to reach it.
"""

import json
import os
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(__file__)
PAGE_PATH = os.path.join(HERE, "dashboard.html")


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_handler(bus, run_dir):
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass  # the run's own output is the thing worth reading in the terminal

        def _send(self, code, body, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            route = parsed.path

            if route in ("/", "/index.html"):
                with open(PAGE_PATH, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
                return

            if route == "/events":
                since = int((parse_qs(parsed.query).get("since") or ["0"])[0])
                events, seq = bus.snapshot(since=since)
                body = json.dumps({"events": events, "seq": seq}).encode()
                self._send(200, body, "application/json")
                return

            if route.startswith("/shot/"):
                rel = unquote(route[len("/shot/"):])
                full = os.path.realpath(os.path.join(run_dir, rel))
                # Never serve outside the run directory, whatever the path says.
                if not full.startswith(os.path.realpath(run_dir) + os.sep) or not os.path.isfile(full):
                    self._send(404, b"not found", "text/plain")
                    return
                with open(full, "rb") as f:
                    self._send(200, f.read(), "image/png")
                return

            self._send(404, b"not found", "text/plain")

    return Handler


def start(bus, run_dir, port):
    """Serve the dashboard on a daemon thread. Returns (server, url)."""
    server = _ThreadingHTTPServer(("127.0.0.1", port), _make_handler(bus, run_dir))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"
