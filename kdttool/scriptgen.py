"""Rebuilds a test's script from its catalog entry by asking Claude Code, and lets the
dashboard show the change as a diff before anything on disk is replaced.

`claude -p` runs on this machine with read-only tools (Read/Grep/Glob), so it can look at
the project's helpers and other scripts but cannot write anything itself. Its answer comes
back as text; only an explicit Approve puts it in scripts/.
"""

import difflib
import os
import py_compile
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from . import catalog_store, config

TIMEOUT_S = 240
FAKE = False  # demo mode: return a canned proposal instead of calling Claude

_jobs = {}
_jobs_lock = threading.Lock()


def read_script(row):
    path = catalog_store.script_path(row)
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return ""


def build_prompt(row, current_code):
    fields = "\n".join(
        f"- {k}: {row.get(k)}" for k in
        ("order", "title", "area", "summary", "description", "verifies", "viewport", "credentials", "email", "money", "sensitive")
    )
    if current_code:
        base = f"Here is the CURRENT script for this test (scripts/{row['script']}):\n\n```python\n{current_code}\n```\n\n" \
               "Update it so it does exactly what the catalog entry now says. Keep everything that still applies " \
               "(helper calls, selectors, screenshot labels); change only what the new wording requires."
    else:
        base = f"There is no script yet (it will be scripts/{row['script']}). Look at a few similar scripts in scripts/ and " \
               "the modules in helpers/ first, then write a new one in the same style, reusing the helpers."
    return (
        "You maintain a Playwright UAT script for a KD-TTOOL project. The catalog entry below is the source of "
        "truth, written in plain English by the test owner.\n\n"
        f"CATALOG ENTRY\n{fields}\n\n{base}\n\n"
        "Conventions: the script defines `run(ctx)`; use `from kdttool.browser import new_page`; take pictures with "
        "`ctx.screenshot(page, label)`; add `ctx.note(...)` lines for what was verified; raise AssertionError when a "
        "check fails. Never write secrets into the script: read them from the environment / config as the other "
        "scripts do.\n\n"
        "Reply with ONLY the complete new file content in a single ```python fenced block, no commentary."
    )


def _extract_code(text):
    match = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    code = match.group(1) if match else text
    return code.strip("\n") + "\n"


_MONEY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?")
_COUNT_RE = re.compile(r"\b\d+\s*(?:sessions?|credits?)\b", re.I)


def _literal_values(row):
    """Dollar amounts and session/credit counts mentioned in the catalog entry's own words —
    the concrete numbers a rebuilt script is expected to actually contain."""
    text = f"{row.get('description', '')} {row.get('verifies', '')}"
    return sorted(set(_MONEY_RE.findall(text)) | set(m.strip() for m in _COUNT_RE.findall(text)))


def _numeric_core(value):
    """'$50.00' -> '50.00', '4 sessions' -> '4' — the bare number a script would actually
    contain (as `50.00` or `"50.00"`, not necessarily with a `$` or the word 'sessions')."""
    match = re.search(r"\d[\d,]*(?:\.\d+)?", value)
    return match.group(0).replace(",", "") if match else value


def _missing_values(row, code):
    """Which of the catalog entry's own literal values (see _literal_values) are nowhere in
    `code`. A script that doesn't contain a number the catalog entry states can't actually be
    doing what the entry says — that value most likely lives in a shared file this rebuild
    isn't allowed to touch (it only ever edits the one script file)."""
    wanted = _literal_values(row)
    missing = []
    for value in wanted:
        core = _numeric_core(value)
        if core and core not in code:
            missing.append(value)
    return missing


def _validate(code):
    if "def run(" not in code:
        return "The proposed script has no run(ctx) function."
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(code)
        tmp_path = tmp.name
    try:
        py_compile.compile(tmp_path, doraise=True)
    except py_compile.PyCompileError as exc:
        return f"The proposed script does not compile: {exc.msg}"
    finally:
        os.unlink(tmp_path)
    return None


def _fake_proposal(row, current_code):
    time.sleep(2.0)
    if current_code:
        return current_code.rstrip("\n") + f"\n    ctx.note({('Rebuilt to match: ' + row['title'])!r})\n"
    return (
        f'"""{row["title"]} — built from the catalog entry."""\n\n'
        "from kdttool.browser import new_page\n\n\n"
        "def run(ctx):\n"
        "    with new_page() as page:\n"
        "        page.goto(ctx.base_url)\n"
        f"        ctx.screenshot(page, {row['order']!r})\n"
        f"        ctx.note({row['description'][:80]!r})\n"
    )


def _work(job, row):
    try:
        current = read_script(row)
        job["base"] = current
        prompt = build_prompt(row, current)
        job["prompt"] = prompt
        if FAKE:
            reply = _fake_proposal(row, current)
        else:
            exe = shutil.which("claude")
            if not exe:
                raise RuntimeError("The `claude` command is not on this machine's PATH.")
            proc = subprocess.run(
                [exe, "-p", prompt, "--allowedTools", "Read,Grep,Glob", "--output-format", "text"],
                cwd=config.PROJECT_DIR, capture_output=True, text=True, timeout=TIMEOUT_S,
            )
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or "claude failed").strip()[-600:])
            reply = proc.stdout
        code = _extract_code(reply)
        problem = _validate(code)
        if problem:
            raise RuntimeError(problem)
        job["proposed"] = code
        job["diff"] = "".join(difflib.unified_diff(
            current.splitlines(True), code.splitlines(True),
            fromfile=f"scripts/{row['script']} (now)", tofile=f"scripts/{row['script']} (proposed)", n=3,
        ))
        missing = _missing_values(row, code)
        if missing:
            job["state"] = "mismatch"
            job["missing"] = missing
            job["error"] = (
                "This test's own words mention " + ", ".join(missing) + ", but that doesn't appear "
                "anywhere in the script — Claude only edits this one file, so if that value actually "
                "lives in a shared helper, it was left unchanged. The script does NOT yet do what the "
                "catalog entry says; find and update that value by hand, or restate the entry."
            )
        else:
            job["state"] = "done"
    except subprocess.TimeoutExpired:
        job.update(state="error", error=f"Claude took longer than {TIMEOUT_S}s. Try again, or use the copied prompt in Claude Code.")
    except Exception as exc:  # noqa: BLE001 - the dashboard shows whatever went wrong
        job.update(state="error", error=str(exc))


def start_rebuild(order):
    row = catalog_store.find_row(order)
    if row is None:
        raise catalog_store.CatalogError(f"No test {order!r} in the catalog.")
    if row.get("manual") or not row.get("script"):
        raise catalog_store.CatalogError("A manual test has no script to build.")
    job = {"id": uuid.uuid4().hex[:10], "order": order, "state": "running", "started": time.time(),
           "error": None, "diff": "", "proposed": None, "base": None, "prompt": None}
    with _jobs_lock:
        _jobs[job["id"]] = job
    threading.Thread(target=_work, args=(job, dict(row)), daemon=True).start()
    return job["id"]


def get_job(job_id):
    """Job state for the dashboard (the proposed code itself stays server-side until approved)."""
    job = _jobs.get(job_id)
    if job is None:
        return None
    return {k: job.get(k) for k in ("id", "order", "state", "error", "diff", "prompt", "missing")}


def approve(job_id):
    job = _jobs.get(job_id)
    if job is None or job["state"] not in ("done", "mismatch"):
        raise catalog_store.CatalogError("Nothing to approve.")
    row = catalog_store.find_row(job["order"])
    if row is None:
        raise catalog_store.CatalogError("That test no longer exists.")
    path = catalog_store.script_path(row)
    if read_script(row) != job["base"]:
        raise catalog_store.CatalogError("The script changed on disk since the rebuild started. Rebuild again.")
    if job["proposed"] != job["base"]:  # identical = Claude says it already matches: just mark it up to date
        os.makedirs(config.SCRIPTS_DIR, exist_ok=True)
        if os.path.isfile(path):
            backup = os.path.join(catalog_store.history_dir(), f"{os.path.basename(path)}.{time.strftime('%Y%m%d_%H%M%S')}.bak")
            shutil.copy2(path, backup)
        with open(path, "w", encoding="utf-8") as f:
            f.write(job["proposed"])
    if job["state"] == "done":
        # Only a real, confirmed match clears the "out of date" flag — a mismatch approval
        # (see _missing_values) saves whatever code changed but stays flagged, because the
        # catalog's own words still aren't provably true of the script.
        catalog_store.mark_script_built(job["order"])
    job["state"] = "approved"
    return catalog_store.find_row(job["order"])


def reject(job_id):
    job = _jobs.get(job_id)
    if job is not None and job["state"] in ("done", "error", "mismatch"):
        job["state"] = "rejected"
