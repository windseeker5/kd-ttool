"""Discovers and executes the active project's scripts/*.py in catalog order, collects results."""

import importlib.util
import os
import re
import time
import traceback

from . import browser, config


def row_title(row):
    """Short plain name for the dashboard. Add `title="..."` to a catalog row to
    choose it yourself; otherwise it is derived from the script filename
    ('02_signup_payment_first.py' -> 'Signup payment first')."""
    if row.get("title"):
        return row["title"]
    stem = os.path.splitext(row["script"] or "")[0]
    words = stem.split("_", 1)[1] if "_" in stem else stem
    words = words.replace("_", " ").strip()
    return (words[:1].upper() + words[1:]) if words else row["area"]


def row_dir_name(order, script_name):
    return f"{order}_{os.path.splitext(script_name)[0]}"


class Context:
    """Passed to every script's run(ctx). Carries config + report-writing helpers."""

    def __init__(self, order, script_name, money_confirmed, run_id, bus=None):
        self.order = order
        self.script_name = script_name
        self.base_url = config.BASE_URL
        self.money_confirmed = money_confirmed
        self.run_id = run_id
        self._bus = bus
        self._notes = []
        self._screenshots = []
        # One self-contained folder per run (events.jsonl, report.md, a dir per
        # row) so a report and the screenshots it points at can never drift
        # apart, and an old run's evidence is never overwritten by a later one.
        self._shot_dir = os.path.join(config.run_dir(run_id), row_dir_name(order, script_name))
        self._shot_count = 0

    def note(self, message):
        self._notes.append(message)
        if self._bus:
            self._bus.emit("note", order=self.order, text=message)

    def step(self, message):
        """Narrate something the auto-instrumentation can't see (an assertion
        passing, a wait for an email). Live-log only — not kept in the report."""
        if self._bus:
            self._bus.emit("step", order=self.order, text=message)

    def screenshot(self, page, label, full_page=True):
        os.makedirs(self._shot_dir, exist_ok=True)
        # Numbered so a label reused within a row can never overwrite an earlier shot.
        self._shot_count += 1
        safe = re.sub(r"[^\w.-]+", "_", str(label)).strip("_") or "shot"
        filename = f"{self._shot_count:02d}_{safe}.png"
        full_path = os.path.join(self._shot_dir, filename)
        page.screenshot(path=full_path, full_page=full_page)
        # Relative to the run dir, which is where report.md now lives too.
        rel_path = os.path.relpath(full_path, config.run_dir(self.run_id))
        self._screenshots.append(rel_path)
        if self._bus:
            self._bus.emit("screenshot", order=self.order, label=label, path=rel_path)
        return full_path

    def collect_failure_shots(self):
        """Register the FAILED_*.png files browser.py saved when a page raised."""
        if not os.path.isdir(self._shot_dir):
            return
        run_dir = config.run_dir(self.run_id)
        for name in sorted(os.listdir(self._shot_dir)):
            if name.startswith("FAILED_") and name.endswith(".png"):
                rel_path = os.path.relpath(os.path.join(self._shot_dir, name), run_dir)
                self._screenshots.append(rel_path)
                if self._bus:
                    self._bus.emit("screenshot", order=self.order, label="failure", path=rel_path)


def _load_script_module(script_path):
    spec = importlib.util.spec_from_file_location(
        os.path.splitext(os.path.basename(script_path))[0], script_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _drop_traces(run_dir, row_dir):
    """Delete a row's Playwright traces. A trace records every typed value, so rows that type
    secrets (catalog `sensitive=True`) must never leave one on disk, even after a failure."""
    path = os.path.join(run_dir, row_dir)
    if os.path.isdir(path):
        for name in os.listdir(path):
            if name.startswith("trace_") and name.endswith(".zip"):
                os.remove(os.path.join(path, name))


def _prune_traces(results, run_id):
    """Traces are multi-MB apiece and a full run makes ~40. Keep only the ones
    attached to a failure — that's the only time you go time-travelling."""
    removed = 0
    for r in results:
        if r["status"] == "fail":
            continue
        row_dir = os.path.join(config.run_dir(run_id), row_dir_name(r["order"], r["script"]))
        if not os.path.isdir(row_dir):
            continue
        for name in os.listdir(row_dir):
            if name.startswith("trace_") and name.endswith(".zip"):
                os.remove(os.path.join(row_dir, name))
                removed += 1
    return removed


def run_all(only=None, money_confirmed=False, bus=None, run_id=None, keep_traces=False):
    """Run every non-manual catalog row in order (optionally filtered to `only`
    order values). Returns (results, manual_reminders, run_id).
    """
    results = []
    manual_reminders = []
    run_id = run_id or time.strftime("%Y-%m-%d_%H%M%S")

    planned = [
        row for row in config.CATALOG
        if not row["manual"] and not (only and row["order"] not in only)
    ]
    if bus:
        bus.emit(
            "run_start",
            run_id=run_id,
            base_url=config.BASE_URL,
            headless=config.HEADLESS,
            rows=[
                {
                    "order": r["order"], "area": r["area"], "script": r["script"], "money": r["money"],
                    "title": row_title(r), "summary": r.get("summary", ""), "description": r["description"],
                    "verifies": r["verifies"], "viewport": r["viewport"],
                }
                for r in planned
            ],
        )

    for row in config.CATALOG:
        if row["manual"]:
            manual_reminders.append(f"[{row['order']}] {row['area']}: {row['description']}")
            continue

        if only and row["order"] not in only:
            continue

        script_path = os.path.join(config.SCRIPTS_DIR, row["script"])
        result = dict(
            order=row["order"], script=row["script"], area=row["area"],
            status="skipped", duration_s=0.0, error=None, screenshots=[], notes=[],
        )

        if row["money"] and not money_confirmed:
            result["status"] = "skipped"
            result["notes"].append("Skipped: money-moving step requires --confirm-money.")
            print(f"[{row['order']}] {row['area']} ... SKIPPED (requires --confirm-money)")
            if bus:
                bus.emit("row_end", order=row["order"], status="skipped",
                         duration_s=0.0, reason="requires --confirm-money")
            results.append(result)
            continue

        if not os.path.exists(script_path):
            result["status"] = "skipped"
            result["notes"].append(f"Skipped: {row['script']} not implemented yet.")
            print(f"[{row['order']}] {row['area']} ... SKIPPED (not implemented yet)")
            if bus:
                bus.emit("row_end", order=row["order"], status="skipped",
                         duration_s=0.0, reason="not implemented yet")
            results.append(result)
            continue

        print(f"[{row['order']}] {row['area']} ... running", flush=True)
        ctx = Context(row["order"], row["script"], money_confirmed, run_id, bus=bus)
        if bus:
            bus.emit("row_start", order=row["order"], area=row["area"], script=row["script"])
        start = time.monotonic()
        try:
            # One browser for the whole row; new_page() makes contexts on it.
            browser.start_row_session(row["order"], ctx._shot_dir, bus, failure_shots=not row.get("sensitive"), ctx=ctx)
            module = _load_script_module(script_path)
            module.run(ctx)
            result["status"] = "pass"
        except Exception as exc:  # noqa: BLE001 - report every failure, don't stop the whole run
            result["status"] = "fail"
            result["error"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        finally:
            browser.stop_row_session()
            if row.get("sensitive"):
                _drop_traces(config.run_dir(run_id), row_dir_name(row["order"], row["script"]))
            result["duration_s"] = time.monotonic() - start
            ctx.collect_failure_shots()
            result["screenshots"] = ctx._screenshots
            result["notes"].extend(ctx._notes)

        duration = result["duration_s"]
        if result["status"] == "pass":
            print(f"[{row['order']}] {row['area']} ... PASS ({duration:.1f}s)")
        else:
            first_error_line = (result["error"] or "").strip().splitlines()[-1] if result["error"] else "unknown error"
            print(f"[{row['order']}] {row['area']} ... FAIL ({duration:.1f}s): {first_error_line}")

        if bus:
            bus.emit("row_end", order=row["order"], status=result["status"],
                     duration_s=result["duration_s"], error=result["error"],
                     trace_dir=row_dir_name(row["order"], row["script"]))

        results.append(result)

    if not keep_traces:
        _prune_traces(results, run_id)

    return results, manual_reminders, run_id
