"""Engine settings plus the currently active project's settings.

The engine never hard-codes a target. `activate("<project>")` (called once by
run.py) imports `projects/<project>/project.py` and copies every UPPER_CASE name
it defines onto this module, so engine code and project scripts both read
`config.BASE_URL`, `config.ADMIN_EMAIL`, `config.CATALOG` ... at call time.

A project may override any default below (VIEWPORTS, HEADLESS, DASHBOARD_PORT,
CATALOG_INTRO, ...). Names a project must provide: BASE_URL, CATALOG.
"""

import importlib.util
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECTS_DIR = os.path.join(ROOT_DIR, "projects")

# --- defaults a project may override -------------------------------------------------------------

# Headless by default: a full run is faster, and the live dashboard is what you
# actually watch. `run.py --headed` flips this to a real visible browser.
HEADLESS = os.environ.get("UAT_HEADLESS", "1").lower() not in ("0", "false", "no")

# Local-only live dashboard. Never bound to anything but localhost.
DASHBOARD_PORT = int(os.environ.get("UAT_DASHBOARD_PORT", "8899"))

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "mobile": {"width": 390, "height": 844},
}

# Real-money scripts refuse to run unless this flag was passed to run.py.
CONFIRM_MONEY_FLAG = "--confirm-money"

# When True, every time a modal / dialog opens on a page, the engine takes a screenshot
# of it automatically (numbered with the row's other screenshots). Off by default; a
# project switches it on in project.py. Rows marked `sensitive` are never captured.
SCREENSHOT_MODALS = False

# Safety cap so a loop that opens the same modal hundreds of times can't fill the disk.
MAX_MODAL_SHOTS_PER_ROW = 40

# Text rendered at the top of the generated CATALOG.md, one string per line.
CATALOG_INTRO = []

# --- filled in by activate() ---------------------------------------------------------------------

PROJECT_NAME = None
PROJECT_DIR = None
SCRIPTS_DIR = None
REPORTS_DIR = None
CATALOG_MD_PATH = None
BASE_URL = ""
CATALOG = []


def available_projects():
    if not os.path.isdir(PROJECTS_DIR):
        return []
    return sorted(
        name for name in os.listdir(PROJECTS_DIR)
        if os.path.isfile(os.path.join(PROJECTS_DIR, name, "project.py"))
    )


def activate(name):
    """Load projects/<name>/project.py and make it the active project."""
    project_dir = os.path.join(PROJECTS_DIR, name)
    project_file = os.path.join(project_dir, "project.py")
    if not os.path.isfile(project_file):
        raise SystemExit(
            f"Unknown project {name!r}. Available: {', '.join(available_projects()) or '(none)'}. "
            f"A project is a folder under projects/ containing project.py."
        )

    # Scripts import their project's helper package (`from helpers.fixtures import ...`)
    # and the project's own modules (catalog_data), so put the project folder on the path.
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)

    module = _this_module()
    module.PROJECT_NAME = name
    module.PROJECT_DIR = project_dir
    module.SCRIPTS_DIR = os.path.join(project_dir, "scripts")
    module.REPORTS_DIR = os.path.join(project_dir, "reports")
    module.CATALOG_MD_PATH = os.path.join(project_dir, "CATALOG.md")

    spec = importlib.util.spec_from_file_location(f"kdttool_project_{name}", project_file)
    project = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(project)
    for attr in dir(project):
        if attr.isupper():
            setattr(module, attr, getattr(project, attr))

    # A catalog saved from the dashboard (catalog.json) wins over catalog_data.py.
    saved = _load_saved_catalog(project_dir)
    if saved is not None:
        module.CATALOG = saved

    if not module.BASE_URL or not module.CATALOG:
        raise SystemExit(f"projects/{name}/project.py must define BASE_URL and CATALOG.")


def _load_saved_catalog(project_dir):
    import json
    path = os.path.join(project_dir, "catalog.json")
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return json.load(f)


def _this_module():
    return sys.modules[__name__]


def run_dir(run_id):
    """Self-contained folder for one run: events.jsonl, report.md, per-row dirs."""
    return os.path.join(REPORTS_DIR, run_id)
