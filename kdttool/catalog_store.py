"""Reads and writes the active project's catalog from the dashboard.

A project's catalog starts life as `catalog_data.py` (hand-formatted Python). The first time the
dashboard saves a change, the catalog moves to `projects/<name>/catalog.json`, which
`config.activate()` prefers from then on. `catalog_data.py` is left untouched.

Every save first copies the previous catalog into `projects/<name>/.history/`, because a
private project has no git safety net.
"""

import hashlib
import json
import os
import re
import threading
import time

from . import catalog, config

VIEWPORTS = ("desktop+mobile", "desktop", "mobile", "n/a")
TEXT_FIELDS = ("title", "area", "summary", "description", "credentials", "email", "verifies")
FLAG_FIELDS = ("money", "manual", "sensitive")
# What the script is built from: when any of these change, the script is out of date.
HASH_FIELDS = ("description", "verifies", "viewport", "credentials", "email", "money")

_lock = threading.Lock()


class CatalogError(ValueError):
    """A change the dashboard asked for that we refuse (message is shown to the user)."""


def json_path():
    return os.path.join(config.PROJECT_DIR, "catalog.json")


def history_dir():
    path = os.path.join(config.PROJECT_DIR, ".history")
    os.makedirs(path, exist_ok=True)
    return path


def content_hash(row):
    parts = [str(row.get(k, "")) for k in HASH_FIELDS]
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:12]


def script_path(row):
    if not row.get("script"):
        return None
    name = os.path.basename(row["script"])  # never trust a path, only a file name
    return os.path.join(config.SCRIPTS_DIR, name)


def script_state(row):
    """draft (no script yet) | manual | stale (words changed since the script was built) | ok"""
    if row.get("manual"):
        return "manual"
    path = script_path(row)
    if not path or not os.path.isfile(path):
        return "draft"
    built = row.get("built_hash")
    return "stale" if built and built != content_hash(row) else "ok"


def find_row(order):
    return next((r for r in config.CATALOG if r["order"] == order), None)


def public_rows():
    """Catalog rows as the dashboard sees them (plus a computed script_state)."""
    return [dict(r, script_state=script_state(r)) for r in config.CATALOG]


def _write(rows):
    """Back up the current catalog, then write the new one and refresh CATALOG.md."""
    path = json_path()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    snapshot = os.path.join(history_dir(), f"catalog_{stamp}.json")
    with open(snapshot, "w") as f:
        json.dump(config.CATALOG, f, indent=2, ensure_ascii=False)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
    config.CATALOG[:] = rows  # same list object the runner and reports already hold
    last = catalog.load_last_status()
    catalog.write_catalog_md(status_by_order=last.get("status"), run_timestamp=last.get("run_timestamp"))


def _clean_fields(fields):
    out = {}
    for key in TEXT_FIELDS:
        if key in fields:
            out[key] = str(fields[key] or "").strip()
    for key in FLAG_FIELDS:
        if key in fields:
            out[key] = bool(fields[key])
    if "viewport" in fields:
        if fields["viewport"] not in VIEWPORTS:
            raise CatalogError(f"Viewport must be one of: {', '.join(VIEWPORTS)}.")
        out["viewport"] = fields["viewport"]
    if "title" in out and not out["title"]:
        raise CatalogError("A test needs a title.")
    if "description" in out and not out["description"]:
        raise CatalogError("Describe what the test should do, in your own words.")
    return out


def save_row(order, fields):
    """Update one row's editable fields. Returns the updated public row."""
    with _lock:
        rows = [dict(r) for r in config.CATALOG]
        row = next((r for r in rows if r["order"] == order), None)
        if row is None:
            raise CatalogError(f"No test {order!r} in the catalog.")
        # Remember what the script was built from *before* this edit, so the edit shows as "out of date".
        if "built_hash" not in row and script_state(row) == "ok":
            row["built_hash"] = content_hash(row)
        row.update(_clean_fields(fields))
        _write(rows)
        return dict(row, script_state=script_state(row))


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "new_test"


def _new_order(rows, after_order):
    """A free order id that reads as 'just after' `after_order`: 01 -> 01a, 01b -> 01c."""
    used = {r["order"] for r in rows}
    base_src = after_order if after_order is not None else (rows[0]["order"] if rows else "00")
    match = re.match(r"^(\d+)([a-z]?)$", base_src)
    prefix, suffix = (match.group(1), match.group(2)) if match else (base_src, "")
    start = ord(suffix) + 1 if suffix else ord("a")
    for code in range(start, ord("z") + 1):
        candidate = f"{prefix}{chr(code)}"
        if candidate not in used:
            return candidate
    n = 1
    while f"{prefix}x{n}" in used:
        n += 1
    return f"{prefix}x{n}"


def insert_row(after_order, fields):
    """Add a draft test right after `after_order` (None = at the top). Returns the new public row."""
    with _lock:
        rows = [dict(r) for r in config.CATALOG]
        if after_order is None:
            index = 0
        else:
            idx = next((i for i, r in enumerate(rows) if r["order"] == after_order), None)
            if idx is None:
                raise CatalogError(f"No test {after_order!r} to insert after.")
            index = idx + 1
        clean = _clean_fields(fields)
        if not clean.get("title") or not clean.get("description"):
            raise CatalogError("A new test needs a title and a description of what it should do.")
        order = _new_order(rows, after_order)
        neighbour = rows[index - 1] if index else (rows[0] if rows else {})
        row = dict(
            order=order, script=f"{order}_{_slug(clean['title'])}.py",
            area=clean.get("area") or neighbour.get("area", "New"),
            title=clean["title"], summary=clean.get("summary") or clean["title"],
            description=clean["description"],
            credentials=clean.get("credentials", neighbour.get("credentials", "")),
            email=clean.get("email", "n/a"),
            viewport=clean.get("viewport", "desktop"),
            verifies=clean.get("verifies", ""),
            money=clean.get("money", False), manual=False, sensitive=clean.get("sensitive", False),
        )
        rows.insert(index, row)
        _write(rows)
        return dict(row, script_state=script_state(row))


def mark_script_built(order):
    """Record that the script now matches the catalog words (after an approved rebuild)."""
    with _lock:
        rows = [dict(r) for r in config.CATALOG]
        row = next((r for r in rows if r["order"] == order), None)
        if row is None:
            return
        row["built_hash"] = content_hash(row)
        _write(rows)
