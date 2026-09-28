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

from . import catalog_store, config

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

    # ----------------------------------------------------------------- state
    @property
    def busy(self):
        return self.proc is not None and self.proc.poll() is None

    def state(self):
        return {
            "hub": True, "projects": config.available_projects(), "busy": self.busy,
            "running": self.order if self.busy else None, "paused": self.paused if self.busy else None,
            "tools": self.tools() if config.PROJECT_NAME else [],
        }

    # ------------------------------------------------------------ projects
    def open_project(self, name):
        with self._lock:
            if self.busy:
                raise WorkbenchError("A test is running. Wait for it to finish (or stop it) before switching project.")
            if name not in config.available_projects():
                raise WorkbenchError(f"Unknown project {name!r}.")
            config.activate(name)
            self.last_run_dir = None
            self.bus.emit("project_open", project=name, base_url=config.BASE_URL)

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

    # --------------------------------------------------------------- tools
    def tools(self):
        """The open project's TOOLS, as the page needs them (no command/env details)."""
        return [
            {"id": t["id"], "title": t["title"], "intro": t.get("intro", ""), "next": t.get("next", ""),
             "fields": [dict(f) for f in t.get("fields", [])]}
            for t in (getattr(config, "TOOLS", None) or [])
        ]

    def run_tool(self, tool_id, values):
        """Run a project tool: an existing command-line script, answered from a form.

        The script is started exactly as a person would in a terminal: no shell, and the form's
        answers typed on stdin one per line in `answers` order. `env` values may use {field}
        placeholders. Returns {"ok": bool, "output": str}.
        """
        tool = next((t for t in (getattr(config, "TOOLS", None) or []) if t["id"] == tool_id), None)
        if tool is None:
            raise WorkbenchError(f"No tool {tool_id!r} in {config.PROJECT_NAME}.")
        clean = {}
        for field in tool.get("fields", []):
            # One line per answer: a pasted newline must never become an extra answer.
            value = " ".join(str(values.get(field["name"], "")).split())
            if field.get("required") and not value:
                raise WorkbenchError(f"{field['label']} is required.")
            clean[field["name"]] = value

        command = list(tool["command"])
        if not os.path.isfile(command[0]) and command[0].endswith(".py"):
            raise WorkbenchError(f"Script not found: {command[0]}")
        if command[0].endswith(".py"):
            command.insert(0, sys.executable)
        env = dict(os.environ)
        for key, template in (tool.get("env") or {}).items():
            env[key] = template.format(**clean)
        answers = "".join(clean.get(name, "") + "\n" for name in tool.get("answers", []))
        try:
            done = subprocess.run(command, input=answers, capture_output=True, text=True,
                                  env=env, timeout=tool.get("timeout_s", 60), cwd=os.path.dirname(command[-1]))
        except subprocess.TimeoutExpired:
            return {"ok": False, "output": f"The script did not finish within {tool.get('timeout_s', 60)} seconds."}
        output = (done.stdout or "") + (("\n" + done.stderr) if done.stderr else "")
        ok = done.returncode == 0 and (not tool.get("success_text") or tool["success_text"] in output)
        return {"ok": ok, "output": output.strip()}

    # ---------------------------------------------------------------- play
    def play(self, order, money=False):
        with self._lock:
            if not config.PROJECT_NAME:
                raise WorkbenchError("Open a project first.")
            if self.busy:
                raise WorkbenchError(f"Test {self.order} is still running. One test at a time.")
            row = catalog_store.find_row(order)
            if row is None:
                raise WorkbenchError(f"No test {order!r} in {config.PROJECT_NAME}.")
            if row.get("manual"):
                raise WorkbenchError("This test is done by hand; there is nothing to play.")
            if row.get("money") and not money:
                raise WorkbenchError("This test moves real money: confirm it before it runs.")

            run_id = f"{time.strftime('%Y-%m-%d_%H%M%S')}_{order}"
            reports_dir = config.REPORTS_DIR
            cmd = [sys.executable, RUN_PY, "--project", config.PROJECT_NAME, "--only", order,
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
                    row_ended |= event.get("kind") == "row_end"
                    self._forward(event, run_id)
            if exited:
                break
            time.sleep(0.3)

        self.paused = None
        if not row_ended:
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
