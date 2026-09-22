"""Writes reports/<run_id>/report.md from a run's results."""

import base64
import html
import json
import os
import shutil
import urllib.parse
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


def format_duration(seconds):
    """1789.6 -> '29m 50s'; 42 -> '42s'; 4000 -> '1h 06m 40s'."""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    return f"{m}m {sec:02d}s" if m else f"{sec}s"


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
        f"Total time: {format_duration(sum(r['duration_s'] for r in results))}",
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


# ---------------------------------------------------------------------------
# HTML report: one self-contained file (images embedded), built from events.jsonl
# so it works for finished, interrupted, and old runs alike.
# ---------------------------------------------------------------------------

_HTML_STYLE = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2026;--mute:#6b7380;--line:#e2e5ea;--pass:#1a7f4b;--fail:#c62828;--skip:#8a6d00}
@media(prefers-color-scheme:dark){:root{--bg:#14171b;--card:#1c2026;--ink:#e8eaed;--mute:#9aa3ae;--line:#2d333b;--pass:#4cc38a;--fail:#ff6b6b;--skip:#e0b84a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}.mute{color:var(--mute)}
.summary{display:flex;gap:16px;flex-wrap:wrap;margin:16px 0 24px}
.pill{padding:6px 12px;border-radius:999px;border:1px solid var(--line);background:var(--card);font-weight:600}
.pill.pass,b.pass{color:var(--pass)}.pill.fail,b.fail{color:var(--fail)}.pill.skipped,.pill.running,b.skipped,b.running{color:var(--skip)}
.row{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:12px 0;padding:14px 16px}
.row.fail{border-left:4px solid var(--fail)}.row.pass{border-left:4px solid var(--pass)}
.row h2{font-size:16px;margin:0 0 4px}.row .meta{font-size:13px;color:var(--mute);margin-bottom:8px}
.notes{margin:6px 0;padding-left:20px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:10px;overflow:auto;font-size:12px;white-space:pre-wrap}
details{margin-top:10px}summary{cursor:pointer;color:var(--mute);font-size:13px}
.shots{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin-top:10px}
figure{margin:0}figure img{width:100%;height:170px;object-fit:cover;object-position:top;border:1px solid var(--line);border-radius:6px;cursor:zoom-in;display:block}
figcaption{font-size:12px;color:var(--mute);margin-top:4px;word-break:break-word}
#lb{position:fixed;inset:0;background:rgba(0,0,0,.88);display:none;overflow:auto;z-index:10;cursor:zoom-out;padding:16px}
#lb img{display:block;margin:0 auto;max-width:100%}#lb p{color:#fff;text-align:center;margin:0 0 8px}
"""

_HTML_SCRIPT = """
const lb=document.getElementById('lb'),lbi=lb.querySelector('img'),lbp=lb.querySelector('p');
document.querySelectorAll('figure img').forEach(i=>i.addEventListener('click',()=>{lbi.src=i.src;lbp.textContent=i.alt;lb.style.display='block';lb.scrollTop=0}));
lb.addEventListener('click',()=>{lb.style.display='none'});
document.addEventListener('keydown',e=>{if(e.key==='Escape')lb.style.display='none'});
"""


def _load_events(events_path):
    events = []
    with open(events_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # torn last line from a killed run
    return events


def _embed(run_dir, rel_path):
    try:
        with open(os.path.join(run_dir, rel_path), "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return None


def _render_html(run_id, image_src):
    """The report page as a string, or None when the run has no event log.
    image_src(rel_path) -> the <img src> for a screenshot, or None if it is missing."""
    run_dir = config.run_dir(run_id)
    events_path = os.path.join(run_dir, "events.jsonl")
    if not os.path.exists(events_path):
        return None
    events = _load_events(events_path)

    rows, order_seen = {}, []
    base_url, finished = "", False
    started_ts = ended_ts = None
    for e in events:
        kind, order = e.get("kind"), e.get("order")
        if kind == "run_start":
            base_url = e.get("base_url", "")
            started_ts = e.get("ts")
            for r in e.get("rows", []):
                rows[r["order"]] = dict(r, status="not run", duration_s=None, error=None,
                                        notes=[], shots=[], steps=[], reason=None)
                order_seen.append(r["order"])
        elif kind == "run_end":
            finished = True
            ended_ts = e.get("ts")
        elif order in rows:
            row = rows[order]
            if kind == "row_start":
                row["status"] = "running"
            elif kind == "note":
                row["notes"].append(e.get("text", ""))
            elif kind == "step":
                row["steps"].append(e.get("text", ""))
            elif kind == "screenshot":
                row["shots"].append((e.get("label", ""), e.get("path", "")))
            elif kind == "row_end":
                row.update(status=e.get("status", "?"), duration_s=e.get("duration_s"),
                           error=e.get("error"), reason=e.get("reason"))

    counts = {k: sum(1 for r in rows.values() if r["status"] == k) for k in ("pass", "fail", "skipped")}
    unfinished = sum(1 for r in rows.values() if r["status"] in ("running", "not run"))
    esc = html.escape
    icon = {"pass": "✅", "fail": "❌", "skipped": "⏭️", "running": "⏳", "not run": "…"}

    out = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{esc(config.PROJECT_NAME)} UAT report {esc(run_id)}</title>",
        f"<style>{_HTML_STYLE}</style></head><body><main>",
        f"<h1>{esc(config.PROJECT_NAME)} — UAT run report</h1>",
        f"<div class='mute'>Run {esc(run_id)} · target {esc(base_url)}"
        + (f" · total time {format_duration(ended_ts - started_ts)}" if started_ts and ended_ts else "") + "</div>",
        "<div class='summary'>",
        f"<span class='pill pass'>{counts['pass']} passed</span>",
        f"<span class='pill fail'>{counts['fail']} failed</span>",
        f"<span class='pill skipped'>{counts['skipped']} skipped</span>",
    ]
    if unfinished or not finished:
        out.append(f"<span class='pill running'>run did not finish — {unfinished} row(s) incomplete</span>")
    out.append("</div>")

    for order in order_seen:
        r = rows[order]
        dur = f" · {r['duration_s']:.1f}s" if isinstance(r["duration_s"], (int, float)) else ""
        css = r["status"] if r["status"] in ("pass", "fail") else ""
        out.append(f"<section class='row {css}'>")
        out.append(f"<h2>{icon.get(r['status'], '?')} [{esc(str(order))}] {esc(r.get('title') or r.get('area', ''))}</h2>")
        out.append(f"<div class='meta'>{esc(r.get('area', ''))} · <code>{esc(r.get('script') or '')}</code> · "
                   f"<b class='{esc(r['status'].replace(' ', ''))}'>{esc(r['status'])}</b>{dur}</div>")
        if r.get("summary"):
            out.append(f"<div>{esc(r['summary'])}</div>")
        if r.get("reason"):
            out.append(f"<div class='mute'>{esc(r['reason'])}</div>")
        if r["notes"]:
            out.append("<ul class='notes'>" + "".join(f"<li>{esc(n)}</li>" for n in r["notes"]) + "</ul>")
        if r["error"]:
            out.append(f"<pre>{esc(str(r['error']))}</pre>")
        if r["shots"]:
            out.append("<div class='shots'>")
            for label, rel in r["shots"]:
                src = image_src(rel)
                if src is None:
                    out.append(f"<figure><figcaption>{esc(label)} — image missing ({esc(rel)})</figcaption></figure>")
                else:
                    out.append(f"<figure><img src='{src}' alt='{esc(label)}' loading='lazy'>"
                               f"<figcaption>{esc(label)}</figcaption></figure>")
            out.append("</div>")
        if r["steps"]:
            out.append(f"<details><summary>Step log ({len(r['steps'])})</summary><pre>"
                       + esc("\n".join(r["steps"])) + "</pre></details>")
        out.append("</section>")

    out.append("</main><div id='lb'><p></p><img alt=''></div>")
    out.append(f"<script>{_HTML_SCRIPT}</script></body></html>")

    return "\n".join(out)


def write_html_report(run_id):
    """Build reports/<run_id>/report.html (images embedded). None if no event log."""
    page = _render_html(run_id, lambda rel: _embed(config.run_dir(run_id), rel))
    if page is None:
        return None
    path = os.path.join(config.run_dir(run_id), "report.html")
    with open(path, "w") as f:
        f.write(page)
    return path


def export_folder(run_id):
    """Build a standalone folder reports/<run_id>/export/ (index.html, images/, traces/,
    events.jsonl) plus reports/<run_id>/export.zip of it. Returns (folder, zip_path),
    or None when the run has no event log. Rebuilt from scratch each time."""
    run_dir = config.run_dir(run_id)
    out_dir = os.path.join(run_dir, "export")
    zip_base = os.path.join(run_dir, "export")

    def copy_in(rel):
        src = os.path.join(run_dir, rel)
        if not os.path.isfile(src):
            return None
        dest = os.path.join(out_dir, "images", rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        return "images/" + urllib.parse.quote(rel.replace(os.sep, "/"))

    if not os.path.exists(os.path.join(run_dir, "events.jsonl")):
        return None
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir)
    page = _render_html(run_id, copy_in)
    with open(os.path.join(out_dir, "index.html"), "w") as f:
        f.write(page)
    shutil.copy2(os.path.join(run_dir, "events.jsonl"), out_dir)
    # Playwright traces of FAILED rows ride along (open with `npx playwright show-trace`). Passing
    # rows' traces are multi-MB and only exist in a run that was cut short, so they stay out.
    failed_dirs = {e.get("trace_dir") for e in _load_events(os.path.join(run_dir, "events.jsonl"))
                   if e.get("kind") == "row_end" and e.get("status") == "fail"}
    for name in sorted(failed_dirs - {None}):
        row_dir = os.path.join(run_dir, name)
        if not os.path.isdir(row_dir):
            continue
        for f_name in os.listdir(row_dir):
            if f_name.startswith("trace_") and f_name.endswith(".zip"):
                os.makedirs(os.path.join(out_dir, "traces", name), exist_ok=True)
                shutil.copy2(os.path.join(row_dir, f_name), os.path.join(out_dir, "traces", name))
    zip_path = shutil.make_archive(zip_base, "zip", out_dir)
    return out_dir, zip_path
