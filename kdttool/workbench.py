"""Workbench mode: the dashboard stays open, you pick a project and play one test at a time.

`python run.py` with no --project starts it. Each Play runs that single catalog row as its own
`run.py --project P --only NN --headed` process, in a visible browser window. Why a separate
process per test rather than running the script in here:

  * every project has its own `helpers` package under the same name, and a test's imports,
    Playwright browser and module state must start clean each time;
  * a test that hangs or crashes can be stopped (or dies) without taking the dashboard down.

The child writes its events to reports/<run_id>/events.jsonl as usual (so every step is also a
normal run with its own report). A thread here tails that file and re-emits each event onto the
dashboard's long-lived bus, so the page sees one continuous stream across all the steps played.
Screenshot and trace paths are prefixed with the run id, because each step lives in its own run
folder while the page serves them all from the project's reports/ folder.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time

from . import catalog_store, config, project_io

RUN_PY = os.path.join(config.ROOT_DIR, "run.py")


class WorkbenchError(ValueError):
    """A request the page made that we refuse (message is shown to the user)."""


class Workbench:
    def __init__(self, bus):
        self.bus = bus
        self._lock = threading.Lock()
        self.proc = None
        self.order = None
        self.run_id = None
        self.paused = None          # the pause message while a test waits for a person
        self.last_run_dir = None    # the most recent step's run folder (for Export)
        # The environment before any project's .env was loaded. Each project.py loads its .env
        # into os.environ, and load_dotenv never overwrites a variable already set: without this,
        # a project opened after another would run with the first one's passwords.
        self.base_env = dict(os.environ)

    # ----------------------------------------------------------------- state
    @property
    def busy(self):
        return self.proc is not None and self.proc.poll() is None

    def state(self):
        return {
            "hub": True, "projects": config.available_projects(), "busy": self.busy,
            "running": self.order if self.busy else None, "paused": self.paused if self.busy else None,
            "project_dir": config.PROJECT_DIR,
            "needs_env": bool(config.PROJECT_NAME) and project_io.needs_env(config.PROJECT_DIR),
            "env_fields": project_io.env_fields(config.PROJECT_DIR) if config.PROJECT_NAME else [],
        }

    # ------------------------------------------------------------ projects
    def open_project(self, name):
        with self._lock:
            if self.busy:
                raise WorkbenchError("A test is running. Wait for it to finish (or stop it) before switching project.")
            if name not in config.available_projects():
                raise WorkbenchError(f"Unknown project {name!r}.")
            self._activate(name)
            project_io.remember_last(name)
            self.last_run_dir = None
            self.bus.emit("project_open", project=name, base_url=config.BASE_URL)

    def _activate(self, name):
        os.environ.clear()
        os.environ.update(self.base_env)
        # Reopening the same project (after an import replaced it): forget its cached modules too.
        for mod_name in list(sys.modules):
            if mod_name in ("catalog_data", "helpers") or mod_name.startswith("helpers."):
                del sys.modules[mod_name]
        try:
            config.activate(name)
        except SystemExit as exc:  # activate() speaks to the command line; here, tell the page
            raise WorkbenchError(str(exc))
        except Exception as exc:  # noqa: BLE001 - a broken project.py
            raise WorkbenchError(f"Could not open {name}: {exc}")

    def import_project(self, data, filename=""):
        """Open a project .zip (File > Open project). Returns the project's name."""
        with self._lock:
            if self.busy:
                raise WorkbenchError("A test is running. Wait for it to finish before opening another project.")
        name = project_io.import_zip(data, filename)
        self.open_project(name)
        return name

    def new_project(self, name, base_url):
        """Create an empty project and open it. Returns its name."""
        with self._lock:
            if self.busy:
                raise WorkbenchError("A test is running. Wait for it to finish before opening another project.")
        name = project_io.new_project(name, base_url)
        self.open_project(name)
        return name

    def save_env(self, values):
        """Write the open project's .env from the password form, then reload the project with it."""
        self._edit_open_project(lambda: project_io.write_env(config.PROJECT_DIR, values))

    def save_settings(self, base_url):
        """Change the open project's site address (project.json), then reload the project."""
        self._edit_open_project(lambda: project_io.save_settings(config.PROJECT_DIR, base_url))

    def _edit_open_project(self, change):
        with self._lock:
            if not config.PROJECT_NAME:
                raise WorkbenchError("Open a project first.")
            if self.busy:
                raise WorkbenchError("A test is running. Try again once it has finished.")
            change()
            self._activate(config.PROJECT_NAME)
            self.bus.emit("project_open", project=config.PROJECT_NAME, base_url=config.BASE_URL)

    def reset_project(self):
        """Start the open project fresh: forget every test's last result (reports/last_status.json)
        and the ids tests saved for each other (.uat_state/*.json). Past run folders and reports
        are kept. Nothing on the website under test is touched."""
        with self._lock:
            if not config.PROJECT_NAME:
                raise WorkbenchError("Open a project first.")
            if self.busy:
                raise WorkbenchError("A test is running. Stop it before starting fresh.")
            status = os.path.join(config.REPORTS_DIR, "last_status.json")
            if os.path.isfile(status):
                os.remove(status)
            state_dir = os.path.join(config.PROJECT_DIR, ".uat_state")
            if os.path.isdir(state_dir):
                for name in os.listdir(state_dir):
                    if name.endswith(".json"):
                        os.remove(os.path.join(state_dir, name))
            from . import catalog
            catalog.write_catalog_md()
            self.last_run_dir = None
            self.bus.emit("project_open", project=config.PROJECT_NAME, base_url=config.BASE_URL, fresh=True)

    # ---------------------------------------------------------------- play
    def play(self, order, money=False):
        with self._lock:
            self._check_can_play()
            row = catalog_store.find_row(order)
            if row is None:
                raise WorkbenchError(f"No test {order!r} in {config.PROJECT_NAME}.")
            if row.get("manual"):
                raise WorkbenchError("This test is done by hand; there is nothing to play.")
            if row.get("money") and not money:
                raise WorkbenchError("This test moves real money: confirm it before it runs.")

            self._launch(order, ["--only", order], money)

    def play_all(self, money=False, only=None):
        """Every test of the open project (or just the `only` ones), in catalog order, as ONE run
        (one process, one report). Real-money tests run only with `money` (else they are skipped),
        and the first failure skips the rest: the tests build on each other."""
        with self._lock:
            self._check_can_play()
            args = ["--stop-on-fail"]
            if only:
                known = {r["order"] for r in config.CATALOG if not r.get("manual")}
                bad = [o for o in only if o not in known]
                if bad:
                    raise WorkbenchError(f"Not a test that can be played: {', '.join(bad)}.")
                args += ["--only", ",".join(only)]
            self._launch(None, args, money)

    def _check_can_play(self):
        if not config.PROJECT_NAME:
            raise WorkbenchError("Open a project first.")
        if self.busy:
            raise WorkbenchError("A test is still running. One run at a time.")

    def _launch(self, order, args, money):
        run_id = f"{time.strftime('%Y-%m-%d_%H%M%S')}_{order or 'all'}"
        reports_dir = config.REPORTS_DIR
        cmd = [sys.executable, RUN_PY, "--project", config.PROJECT_NAME, *args,
               "--no-dashboard", "--headed", "--run-id", run_id]
        if money:
            cmd.append("--confirm-money")
        # stdin is a pipe so Continue can answer a pause; stdout stays on this terminal.
        # A new session, so Stop can end the test's whole process group (Playwright's browser too).
        self.proc = subprocess.Popen(cmd, cwd=config.ROOT_DIR, stdin=subprocess.PIPE, text=True,
                                     start_new_session=True)
        self.order, self.run_id, self.paused = order, run_id, None
        self.last_run_dir = os.path.join(reports_dir, run_id)
        threading.Thread(target=self._tail, args=(self.proc, order, run_id, reports_dir), daemon=True).start()

    def resume(self):
        if not self.busy or self.paused is None:
            raise WorkbenchError("Nothing is waiting for you right now.")
        try:
            self.proc.stdin.write("\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise WorkbenchError("The test is no longer running.")

    def stop(self):
        if not self.busy:
            raise WorkbenchError("No test is running.")
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    # ---------------------------------------------------------------- tail
    def _tail(self, proc, order, run_id, reports_dir):
        """`order` is None for Run all: the test in progress is then the last row_start seen."""
        path = os.path.join(reports_dir, run_id, "events.jsonl")
        offset, buffer, row_ended = 0, "", False
        while True:
            exited = proc.poll() is not None
            if os.path.exists(path):
                with open(path) as f:
                    f.seek(offset)
                    chunk = f.read()
                    offset = f.tell()
                buffer += chunk
                *lines, buffer = buffer.split("\n")
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("kind") == "row_start":
                        order, row_ended = event.get("order"), False
                        self.order = order
                    row_ended |= event.get("kind") == "row_end" and event.get("order") == order
                    self._forward(event, run_id)
            if exited:
                break
            time.sleep(0.3)

        self.paused = None
        if order and not row_ended:
            # Stopped from the page, or the process died before reporting a result.
            stopped = proc.returncode in (-signal.SIGTERM, 143)
            self.bus.emit("row_end", order=order, run_id=run_id, status="fail", duration_s=0.0,
                          error="Stopped from the dashboard." if stopped
                          else f"The test process ended unexpectedly (exit code {proc.returncode}).")
        self.bus.emit("step_end", order=order, run_id=run_id)

    def _forward(self, event, run_id):
        kind = event.pop("kind")
        event.pop("seq", None)
        event["run_id"] = run_id
        if kind == "screenshot" and event.get("path"):
            event["path"] = f"{run_id}/{event['path']}"
        if kind == "pause":
            self.paused = event.get("text") or ""
        elif kind == "resume":
            self.paused = None
        self.bus.emit(kind, **event)
