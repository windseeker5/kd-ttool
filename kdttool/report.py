"""Writes reports/<run_id>/report.md from a run's results."""

import os
from datetime import datetime

from . import config
from .runner import row_dir_name


def _traces_for(run_timestamp, result):
    row_dir = os.path.join(config.run_dir(run_timestamp), row_dir_name(result["order"], result["script"]))
    if not os.path.isdir(row_dir):
        return []
    return sorted(
        os.path.join(row_dir, name)
        for name in os.listdir(row_dir)
        if name.startswith("trace_") and name.endswith(".zip")
    )


def write_report(results, run_timestamp, manual_reminders):
    """results: list of dicts with keys order, script, area, status, duration_s,
    error, screenshots (list of paths relative to the run dir), notes (list of str).
    """
    run_dir = config.run_dir(run_timestamp)
    os.makedirs(run_dir, exist_ok=True)
    path = os.path.join(run_dir, "report.md")

    total = len(results)
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    skipped = sum(1 for r in results if r["status"] == "skipped")

    lines = [
        f"# {config.PROJECT_NAME} — UAT Run Report — {run_timestamp}",
        "",
        f"Target: `{config.BASE_URL}`",
        "",
        f"**{passed}/{total} passed**, {failed} failed, {skipped} skipped.",
        "",
        f"Replay this run in the live dashboard: `python run.py --project {config.PROJECT_NAME} --replay {run_timestamp}`",
        "",
    ]

    for r in results:
        icon = {"pass": "✅", "fail": "❌", "skipped": "⏭️"}.get(r["status"], "?")
        lines.append(f"## {icon} [{r['order']}] {r['area']} — `{r['script']}`")
        lines.append(f"- Status: **{r['status']}**  ·  Duration: {r['duration_s']:.1f}s")
        if r.get("notes"):
            for note in r["notes"]:
                lines.append(f"- {note}")
        if r["status"] == "fail" and r.get("error"):
            lines.append("")
            lines.append("```")
            lines.append(str(r["error"]))
            lines.append("```")
            for trace in _traces_for(run_timestamp, r):
                rel = os.path.relpath(trace, run_dir)
                lines.append(f"- Step through it: `npx playwright show-trace {trace}` (`{rel}`)")
        if r.get("screenshots"):
            lines.append("")
            for shot in r["screenshots"]:
                label = os.path.splitext(os.path.basename(shot))[0]
                # Embedded, not just named: these images are the point of the run.
                lines.append(f"**{label}**")
                lines.append("")
                lines.append(f"![{label}]({shot})")
                lines.append("")
        lines.append("")

    lines.append("## Still owed — manual only")
    lines.append("")
    for reminder in manual_reminders:
        lines.append(f"- [ ] {reminder}")
    lines.append("")

    with open(path, "w") as f:
        f.write("\n".join(lines))

    return path


def now_timestamp():
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")
