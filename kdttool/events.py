"""Structured event stream for a UAT run.

Everything interesting that happens during a run — a row starting, a click, a
screenshot landing on disk, a row failing — goes through one EventBus. It has
two consumers:

  * reports/<run_id>/events.jsonl, appended as the run goes, so a finished run
    can be replayed into the dashboard later (`run.py --replay <run_id>`).
  * the live dashboard (kdttool/dashboard.py), which polls the in-memory list from
    its own HTTP thread while Playwright drives the browser on the main one.

Hence the lock: emit() is called from the Playwright thread, snapshot() from
the server thread.
"""

import json
import os
import threading
import time


class EventBus:
    """Ordered, append-only event log. Every event gets a monotonic `seq`."""

    def __init__(self, jsonl_path=None):
        self._lock = threading.Lock()
        self._events = []
        self._seq = 0
        self._jsonl_path = jsonl_path
        self._fh = None
        if jsonl_path:
            os.makedirs(os.path.dirname(jsonl_path), exist_ok=True)
            self._fh = open(jsonl_path, "a", buffering=1)  # line-buffered: survives a hard kill

    def emit(self, kind, **fields):
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "ts": time.time(), "kind": kind}
            event.update(fields)
            self._events.append(event)
            if self._fh:
                self._fh.write(json.dumps(event) + "\n")
        return event

    def snapshot(self, since=0):
        """Every event with seq > since, plus the current high-water mark."""
        with self._lock:
            return [e for e in self._events if e["seq"] > since], self._seq

    def close(self):
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None


def replay_bus(jsonl_path):
    """Rebuild a read-only bus from a finished run's events.jsonl."""
    bus = EventBus(jsonl_path=None)
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # a run killed mid-write can leave one torn last line
            if event.get("kind") == "run_start":
                _fill_from_catalog(event)
            bus._events.append(event)
            bus._seq = max(bus._seq, event.get("seq", 0))
    return bus


def _fill_from_catalog(run_start):
    """Runs recorded before the dashboard showed catalog text have no title /
    description / verifies on their rows. Fill them from the current catalog so
    old runs open in the same layout as new ones."""
    from . import config
    from .runner import row_title
    by_order = {c["order"]: c for c in config.CATALOG}
    for row in run_start.get("rows", []):
        c = by_order.get(row.get("order"))
        if c and not row.get("summary"):
            row.update(title=row_title(c), summary=c.get("summary", ""), description=c["description"],
                       verifies=c["verifies"], viewport=c["viewport"])
