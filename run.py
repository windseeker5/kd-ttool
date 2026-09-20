#!/usr/bin/env python3
"""KD-TTOOL master runner.

Usage:
  python run.py --project example                    # everything except money-tier + manual rows
  python run.py --project example --only 01,02        # just these catalog rows
  python run.py --project example --headed           # watch it in a real Chrome window
  python run.py --project example --confirm-money    # also run rows that move real money
  python run.py --project example --replay 2026-09-13_142211   # reopen a past run's dashboard
  python run.py --list-projects

A project is a folder under projects/ (project.py + catalog_data.py + scripts/). Each
project's CATALOG.md describes, row by row, what its scripts do.

Runs headless by default and opens a live dashboard in your browser: the whole
row grid, a step-by-step log, and each screenshot appearing inline the moment
it's taken. Pass --headed when you'd rather watch the real clicks happen; rows
90/91 are always headed regardless, since you have to type a real card number.

Everything a run produces lands in one folder, projects/<project>/reports/<run_id>/: events.jsonl,
report.md, and a directory per row holding its screenshots (plus a Playwright
trace, kept only for rows that failed unless you pass --keep-traces).
"""

import argparse
import os
import sys
import time
import webbrowser

from kdttool import catalog, config, dashboard, report
from kdttool.events import EventBus, replay_bus
from kdttool.runner import run_all

DEFAULT_PROJECT = "example"


def _serve(bus, run_dir, port, open_browser):
    """Start the dashboard, falling forward to the next free port if needed.

    A dashboard left running from an earlier run (or a --replay you forgot to
    Ctrl-C) keeps holding the default port. Binding then fails, and silently
    continuing without a dashboard is worse than useless: the browser keeps
    polling the STALE server and happily shows the previous run's rows, which
    reads as "the new run produced nothing". Take the next free port instead,
    and say loudly which one won.
    """
    for attempt in range(10):
        candidate = port + attempt
        try:
            server, url = dashboard.start(bus, run_dir, candidate)
        except OSError:
            continue
        if attempt:
            print(
                f"Port {port} was already in use (something else is still serving a dashboard there "
                f"— an older run or a --replay still open). Using port {candidate} instead.",
                flush=True,
            )
        print(f"Dashboard: {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        return server, url

    print(
        f"Dashboard could not start: ports {port}-{port + 9} are all in use. Continuing without it. "
        f"Free one up (e.g. quit an older `run.py --replay`) or pass --port.",
        flush=True,
    )
    return None, None


def _replay(run_id, port):
    run_dir = config.run_dir(run_id)
    events_path = os.path.join(run_dir, "events.jsonl")
    if not os.path.exists(events_path):
        print(f"No event log at {events_path} — that run predates the dashboard, or the id is wrong.")
        return 1
    server, url = _serve(replay_bus(events_path), run_dir, port, open_browser=True)
    if not server:
        return 1
    print("Replaying a finished run. Ctrl-C to quit.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print()
    return 0


def main():
    # --project must be known before the rest of the CLI is built, because the
    # project supplies defaults (dashboard port) and the catalog the flags refer to.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--project", default=DEFAULT_PROJECT)
    pre.add_argument("--list-projects", action="store_true")
    known, _ = pre.parse_known_args()
    if known.list_projects:
        print("\n".join(config.available_projects()) or "(no projects)")
        return
    config.activate(known.project)

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="Project folder under projects/ (default %(default)s).")
    parser.add_argument("--list-projects", action="store_true", help="List available projects and exit.")
    parser.add_argument("--only", help="Comma-separated catalog row numbers to run, e.g. 01,01b,02")
    parser.add_argument(
        "--confirm-money", action="store_true",
        help="Also run rows 90/91, which move real money. Requires you to be present to enter a real "
             "card number / confirm a real e-transfer when prompted.",
    )
    parser.add_argument("--headed", action="store_true", help="Run in a visible Chrome window instead of headless.")
    parser.add_argument("--no-dashboard", action="store_true", help="Don't start or open the live dashboard.")
    parser.add_argument("--port", type=int, default=config.DASHBOARD_PORT, help="Dashboard port (default %(default)s).")
    parser.add_argument("--keep-traces", action="store_true", help="Keep Playwright traces for passing rows too.")
    parser.add_argument("--replay", metavar="RUN_ID", help="Serve a past run's dashboard and exit; runs nothing.")
    args = parser.parse_args()

    if args.replay:
        sys.exit(_replay(args.replay, args.port))

    if args.headed:
        config.HEADLESS = False

    only = set(args.only.split(",")) if args.only else None

    run_id = time.strftime("%Y-%m-%d_%H%M%S")
    run_dir = config.run_dir(run_id)
    os.makedirs(run_dir, exist_ok=True)
    bus = EventBus(jsonl_path=os.path.join(run_dir, "events.jsonl"))

    server = None
    dashboard_url = None
    if not args.no_dashboard:
        server, dashboard_url = _serve(bus, run_dir, args.port, open_browser=True)

    results, manual_reminders, run_id = run_all(
        only=only, money_confirmed=args.confirm_money, bus=bus,
        run_id=run_id, keep_traces=args.keep_traces,
    )

    report_path = report.write_report(results, run_id, manual_reminders)

    status_by_order = {r["order"]: r["status"] for r in results}
    catalog.save_last_status(status_by_order, run_id, report_path)
    catalog_path = catalog.write_catalog_md(status_by_order=status_by_order, run_timestamp=run_id)

    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    skipped = sum(1 for r in results if r["status"] == "skipped")

    bus.emit("run_end", passed=passed, failed=failed, skipped=skipped, report_path=report_path)

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    print(f"Report:  {report_path}")
    print(f"Catalog: {catalog_path}")

    if manual_reminders:
        print("\nStill owed (manual only):")
        for reminder in manual_reminders:
            print(f"  - {reminder}")

    if server:
        print(f"\nDashboard still up at {dashboard_url} — Ctrl-C to quit.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print()

    bus.close()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
