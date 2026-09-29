"""Export a project as a zip, and open (import) one on another machine.

An export is the project's tests and what they need to run, nothing it produced: the catalog,
scripts, project.py, helpers/, fixtures/ and .env.example. Left out:

  * results and evidence: reports/ (screenshots, traces, last_status.json), .uat_state/ (ids the
    tests saved for each other), CATALOG.md (it carries the last results), .history/;
  * passwords: .env. The tool asks for them when the project is opened without one;
  * Python caches.

So an imported project always opens as a fresh session.
"""

import io
import os
import re
import shutil
import stat
import time
import zipfile

from . import config

SKIP_DIRS = {"reports", ".uat_state", ".history", "__pycache__", ".git"}
SKIP_FILES = {"CATALOG.md", ".env"}
MAX_IMPORT_BYTES = 100 * 1024 * 1024
MAX_IMPORT_FILES = 5000
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ProjectIOError(ValueError):
    """A request we refuse (message is shown to the user)."""


class ProjectExists(ProjectIOError):
    def __init__(self, name):
        super().__init__(f"A project called {name!r} already exists.")
        self.name = name


def _kept(rel_path):
    """True if a path inside a project belongs in an export (and is accepted by an import)."""
    parts = rel_path.replace("\\", "/").split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return False
    name = parts[-1]
    if name in SKIP_FILES or name.endswith((".pyc", ".pyo", ".bak", "~")):
        return False
    # .env.local, .env.production...: secrets too. Only the empty template travels.
    return not (name.startswith(".env") and name != ".env.example")


def export_zip(name):
    """The zip's bytes: every kept file under a top folder named after the project."""
    if name not in config.available_projects():
        raise ProjectIOError(f"Unknown project {name!r}.")
    project_dir = os.path.join(config.PROJECTS_DIR, name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(project_dir):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for fname in sorted(files):
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, project_dir)
                if os.path.islink(full) or not _kept(rel):
                    continue
                zf.write(full, f"{name}/{rel.replace(os.sep, '/')}")
    return buf.getvalue()


def _entries(zf):
    """(relative path inside the project, ZipInfo) for each file, plus the project's name
    from the zip's top folder (None when project.py sits at the zip's root)."""
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_IMPORT_FILES:
        raise ProjectIOError("This zip has too many files to be a project.")
    if sum(i.file_size for i in infos) > MAX_IMPORT_BYTES:
        raise ProjectIOError("This zip is too big to be a project export (over 100 MB unpacked).")
    names = [i.filename.replace("\\", "/") for i in infos]
    for n, info in zip(names, infos):
        if n.startswith("/") or ".." in n.split("/") or re.match(r"^[A-Za-z]:", n):
            raise ProjectIOError(f"Refused: the zip contains an unsafe path ({n}).")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise ProjectIOError(f"Refused: the zip contains a link ({n}).")
    if "project.py" in names:
        top = None
    else:
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) != 1 or f"{next(iter(tops))}/project.py" not in names:
            raise ProjectIOError("This is not a KD-TTOOL project: no project.py in it.")
        top = next(iter(tops))
    out = []
    for n, info in zip(names, infos):
        rel = n.split("/", 1)[1] if top else n
        if rel and _kept(rel):
            out.append((rel, info))
    return top, out


# What belongs to this machine, not to the project's tests: kept when a zip updates a project.
LOCAL_ITEMS = ("reports", ".uat_state", ".history", ".env", "CATALOG.md")


def import_zip(data, filename=""):
    """Open a project zip: unpack it into projects/<name>/. Returns the project's name.

    The name is the zip's top folder, else the file's name. If that project is already here, its
    tests are updated from the zip and what belongs to this machine (passwords, results, saved ids)
    is kept; the previous test files are moved to projects/.replaced/<name>_<time>/, never deleted.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ProjectIOError("That file is not a zip.")
    with zf:
        top, entries = _entries(zf)
        name = (top or os.path.splitext(os.path.basename(filename))[0]).strip()
        if not NAME_RE.match(name):
            raise ProjectIOError("Project names use letters, digits, '-', '_' or '.', up to 64 characters.")
        target = os.path.join(config.PROJECTS_DIR, name)

        # Unpack next to the target, then swap in: a failed import never leaves half a project.
        staging = os.path.join(config.PROJECTS_DIR, f".importing_{name}_{os.getpid()}")
        shutil.rmtree(staging, ignore_errors=True)
        try:
            for rel, info in entries:
                dest = os.path.join(staging, *rel.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
            if os.path.exists(target):
                for item in LOCAL_ITEMS:
                    if os.path.exists(os.path.join(target, item)):
                        shutil.move(os.path.join(target, item), os.path.join(staging, item))
                keep = os.path.join(config.PROJECTS_DIR, ".replaced")
                os.makedirs(keep, exist_ok=True)
                shutil.move(target, os.path.join(keep, f"{name}_{time.strftime('%Y-%m-%d_%H%M%S')}"))
            os.makedirs(os.path.join(staging, "reports"), exist_ok=True)
            os.rename(staging, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return name


# ------------------------------------------------------------------ passwords (.env)
def _parse_env_example(path):
    """[{name, help}] for each KEY= line of .env.example; `help` is the comment right above it."""
    fields, comment = [], []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("#"):
                comment.append(line.lstrip("#").strip())
            elif "=" in line:
                key = line.split("=", 1)[0].strip()
                if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                    fields.append({"name": key, "help": " ".join(c for c in comment if c)})
                comment = []
            else:
                comment = []
    return fields


def env_fields(project_dir):
    """The password form: each KEY of .env.example (plus any extra key already in .env), with its
    help text and whether it has a value yet. Values themselves never leave this machine's disk."""
    example = os.path.join(project_dir, ".env.example")
    fields = _parse_env_example(example) if os.path.isfile(example) else []
    current = _read_env(project_dir)
    known = {f["name"] for f in fields}
    fields += [{"name": k, "help": ""} for k in current if k not in known]
    for f in fields:
        f["set"] = bool(current.get(f["name"]))
    return fields


def needs_env(project_dir):
    """True when the project expects passwords (.env.example) but has no .env yet."""
    return (os.path.isfile(os.path.join(project_dir, ".env.example"))
            and not os.path.exists(os.path.join(project_dir, ".env")))


def _read_env(project_dir):
    path = os.path.join(project_dir, ".env")
    if not os.path.isfile(path):
        return {}
    from dotenv import dotenv_values
    return {k: v or "" for k, v in dotenv_values(path).items()}


def _quote(value):
    if value and not re.match(r"^[\w@%+=:,./-]+$", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def write_env(project_dir, values):
    """Save the password form into the project's .env (readable by you only). An empty box keeps
    the value already saved, so the form never has to show a password back."""
    merged = _read_env(project_dir)
    for field in env_fields(project_dir):
        value = " ".join(str(values.get(field["name"], "")).split())
        if value or field["name"] not in merged:
            merged[field["name"]] = value or merged.get(field["name"], "")
    path = os.path.join(project_dir, ".env")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.writelines(f"{k}={_quote(v)}\n" for k, v in merged.items())
    os.replace(tmp, path)


# ------------------------------------------------------------------ settings and new projects
SETTINGS_FILE = "project.json"
URL_RE = re.compile(r"^https?://[^\s/]+(/\S*)?$")


def read_settings(project_dir):
    import json
    path = os.path.join(project_dir, SETTINGS_FILE)
    if not os.path.isfile(path):
        return {}
    with open(path) as f:
        return json.load(f)


def save_settings(project_dir, base_url):
    """The project's own settings, edited from the tool (the site address for now)."""
    import json
    base_url = (base_url or "").strip().rstrip("/")
    if not URL_RE.match(base_url):
        raise ProjectIOError("The site address must start with http:// or https://, e.g. https://example.com")
    settings = dict(read_settings(project_dir), base_url=base_url)
    with open(os.path.join(project_dir, SETTINGS_FILE), "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")


NEW_PROJECT_PY = '''"""{name}: tests for {base_url}.

The site address lives in project.json (File > Project settings in the tool). Every UPPER_CASE name
defined here is available to scripts as `kdttool.config.<NAME>`. Passwords go in .env (list their
names, with empty values, in .env.example so the tool can ask for them).
"""

import os

from dotenv import load_dotenv

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(PROJECT_DIR, ".env"))

from catalog_data import CATALOG  # noqa: E402,F401
'''


def new_project(name, base_url):
    """Create an empty project folder. Returns its name."""
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise ProjectIOError("Project names use letters, digits, '-', '_' or '.', up to 64 characters.")
    target = os.path.join(config.PROJECTS_DIR, name)
    if os.path.exists(target):
        raise ProjectExists(name)
    staging = os.path.join(config.PROJECTS_DIR, f".creating_{name}_{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        os.makedirs(os.path.join(staging, "scripts"))
        os.makedirs(os.path.join(staging, "reports"))
        save_settings(staging, base_url)
        with open(os.path.join(staging, "project.py"), "w") as f:
            f.write(NEW_PROJECT_PY.format(name=name, base_url=base_url.strip()))
        with open(os.path.join(staging, "catalog_data.py"), "w") as f:
            f.write('"""The catalog. Add tests from the tool (+ Add test)."""\n\nCATALOG = []\n')
        os.rename(staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return name


# ------------------------------------------------------------------ last project opened
def _last_path():
    return os.path.join(config.PROJECTS_DIR, ".last_project")


def remember_last(name):
    try:
        with open(_last_path(), "w") as f:
            f.write(name + "\n")
    except OSError:
        pass


def last_project():
    """The project opened last time, if it is still here (the tool reopens it on start)."""
    try:
        with open(_last_path()) as f:
            name = f.read().strip()
    except OSError:
        return None
    return name if name in config.available_projects() else None
