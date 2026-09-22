"""Live dashboard for a UAT run — a local page you watch while it runs.

Deliberately stdlib-only (http.server on a daemon thread): this tool should
never pull in Flask or fight the app under test for a port. The page polls
/events every 500ms rather than using SSE — the event volume is tiny and a poll
survives the run process being busy inside a blocking Playwright call.

Bound to 127.0.0.1 only. It serves screenshots off local disk and there is no
reason for anything off this machine to reach it.
"""

import json
import mimetypes
import os
import secrets
import threading
from types import SimpleNamespace
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from . import catalog_store, report, scriptgen
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(__file__)
PAGE_PATH = os.path.join(HERE, "dashboard.html")


# Changes every time the tool starts. A dashboard tab left open from an earlier start sees the
# new value, reloads itself, and so picks up the new server's secret token.
BOOT_ID = secrets.token_urlsafe(8)


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class RunControl:
    """Holds a run back until you press Start in the dashboard (`run.py --wait`).

    run.py blocks on `start_event`; the dashboard's Start button sets it. `headed`
    starts as the command-line choice and the page's "Show browser" box can change it.
    """

    def __init__(self, headed=False, money=False, only=None):
        self.start_event = threading.Event()
        self.headed = headed
        self.money = money
        self.only = sorted(only) if only else []

    @property
    def waiting(self):
        return not self.start_event.is_set()


def _make_handler(st, port, token, replay, control):
    """`st` is a small holder with .bus and .run_dir: in wait mode they are swapped in
    once Start is pressed (a run's folder is created then, not when the tool launched)."""

    def run_is_live():
        """True while a run is executing: edits are refused then, so the runner never sees a half-changed catalog."""
        if replay:
            return False
        events, _ = st.bus.snapshot()
        kinds = {e["kind"] for e in events}
        if "run_end" in kinds:
            return False
        # Start pressed but the first event has not landed yet: already counts as running.
        return "run_start" in kinds or bool(control and control.start_event.is_set())

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

        def _host_ok(self):
            # Only our own address: stops another website in the browser from talking to localhost.
            return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            if not self._host_ok():
                self._send(403, b"forbidden", "text/plain")
                return
            parsed = urlparse(self.path)
            route = parsed.path

            if route in ("/", "/index.html"):
                with open(PAGE_PATH, "rb") as f:
                    page = f.read().replace(b"__KD_TOKEN__", token.encode())
                self._send(200, page, "text/html; charset=utf-8")
                return

            if route == "/api/catalog":
                self._json(200, {"rows": catalog_store.public_rows(), "live": run_is_live(), "replay": replay})
                return

            if route == "/api/state":
                from . import config
                self._json(200, {
                    "waiting": bool(control and control.waiting), "headed": bool(control and control.headed),
                    "money": bool(control and control.money), "only": control.only if control else [],
                    "project": config.PROJECT_NAME, "base_url": config.BASE_URL, "boot": BOOT_ID,
                })
                return

            if route == "/api/script":
                order = (parse_qs(parsed.query).get("order") or [""])[0]
                row = catalog_store.find_row(order)
                if row is None:
                    self._json(404, {"error": "No such test."})
                    return
                path = catalog_store.script_path(row)
                self._json(200, {
                    "order": order, "script": row.get("script"), "code": scriptgen.read_script(row),
                    "exists": bool(path and os.path.isfile(path)),
                    "modified": os.path.getmtime(path) if path and os.path.isfile(path) else None,
                })
                return

            if route == "/api/job":
                job = scriptgen.get_job((parse_qs(parsed.query).get("id") or [""])[0])
                self._json(200 if job else 404, job or {"error": "Unknown job."})
                return

            if route == "/events":
                since = int((parse_qs(parsed.query).get("since") or ["0"])[0])
                events, seq = st.bus.snapshot(since=since)
                body = json.dumps({"events": events, "seq": seq, "boot": BOOT_ID}).encode()
                self._send(200, body, "application/json")
                return

            if route.startswith("/shot/"):
                rel = unquote(route[len("/shot/"):])
                if not st.run_dir:
                    self._send(404, b"not found", "text/plain")
                    return
                full = os.path.realpath(os.path.join(st.run_dir, rel))
                # Never serve outside the run directory, whatever the path says.
                if not full.startswith(os.path.realpath(st.run_dir) + os.sep) or not os.path.isfile(full):
                    self._send(404, b"not found", "text/plain")
                    return
                with open(full, "rb") as f:
                    self._send(200, f.read(), "image/png")
                return

            if route == "/export.zip":
                zip_path = os.path.join(st.run_dir or "", "export.zip")
                if not os.path.isfile(zip_path):
                    self._send(404, b"not found", "text/plain")
                    return
                with open(zip_path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="report_{os.path.basename(st.run_dir or "run")}.zip"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if route.startswith("/export/"):
                root = os.path.realpath(os.path.join(st.run_dir or "", "export"))
                full = os.path.realpath(os.path.join(root, unquote(route[len("/export/"):]) or "index.html"))
                if not full.startswith(root + os.sep) or not os.path.isfile(full):
                    self._send(404, b"not found", "text/plain")
                    return
                ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
                with open(full, "rb") as f:
                    self._send(200, f.read(), ctype)
                return

            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            route = urlparse(self.path).path
            if not self._host_ok() or self.headers.get("X-KD-Token") != token:
                self._json(403, {"error": "Forbidden."})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except (ValueError, json.JSONDecodeError):
                self._json(400, {"error": "Bad request."})
                return

            if route == "/export":
                self._export()
                return

            if route == "/api/run/start":
                if not control or not control.waiting:
                    self._json(409, {"error": "The run has already started."})
                    return
                control.headed = bool(body.get("headed"))
                control.start_event.set()
                self._json(200, {"ok": True})
                return

            editing = {
                "/api/catalog/save", "/api/catalog/insert", "/api/script/rebuild",
                "/api/script/approve",
            }
            if route in editing and run_is_live():
                self._json(409, {"error": "A run is in progress. Edit the catalog once it has finished."})
                return
            try:
                if route == "/api/catalog/save":
                    row = catalog_store.save_row(body.get("order", ""), body.get("fields") or {})
                    self._json(200, {"row": row})
                elif route == "/api/catalog/insert":
                    row = catalog_store.insert_row(body.get("after"), body.get("fields") or {})
                    self._json(200, {"row": row})
                elif route == "/api/script/rebuild":
                    self._json(200, {"job": scriptgen.start_rebuild(body.get("order", ""))})
                elif route == "/api/script/approve":
                    row = scriptgen.approve(body.get("job", ""))
                    self._json(200, {"row": dict(row, script_state=catalog_store.script_state(row))})
                elif route == "/api/script/reject":
                    scriptgen.reject(body.get("job", ""))
                    self._json(200, {"ok": True})
                else:
                    self._json(404, {"error": "Not found."})
            except catalog_store.CatalogError as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - tell the page, don't kill the server thread
                self._json(500, {"error": str(exc)})

        def _export(self):
            if not st.run_dir:
                self._json(409, {"error": "Nothing to export yet: start the run first."})
                return
            run_id = os.path.basename(st.run_dir)
            try:
                built = report.export_folder(run_id)
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
                return
            if not built:
                self._json(404, {"error": "no event log for this run"})
                return
            folder, zip_path = built
            self._json(200, {"folder": folder, "zip": zip_path})

    return Handler


def start(bus, run_dir, port, replay=False, control=None):
    """Serve the dashboard on a daemon thread. Returns (server, url).
    `replay` marks a finished run being reopened: catalog edits are always allowed there.
    `control` (a RunControl) makes the page show a Start button; the caller then sets
    `server.state.bus` / `server.state.run_dir` once the run really begins."""
    token = secrets.token_urlsafe(24)
    st = SimpleNamespace(bus=bus, run_dir=run_dir)
    server = _ThreadingHTTPServer(("127.0.0.1", port), _make_handler(st, port, token, replay, control))
    server.state = st
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"
