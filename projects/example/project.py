"""Example KD-TTOOL project: a public-site smoke test that needs no credentials.

Copy this folder to projects/<yourname>/ to start a project of your own. Every
UPPER_CASE name defined here is exposed to scripts as `kdttool.config.<NAME>`.
Your copy is git-ignored by default (see .gitignore), so its URLs, catalog and
credentials never reach a public repo.
"""

import os

from dotenv import load_dotenv

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Optional: put secrets in projects/<name>/.env (gitignored) and read them with os.environ.
load_dotenv(os.path.join(PROJECT_DIR, ".env"))

# Required: the site under test.
BASE_URL = os.environ.get("UAT_BASE_URL", "https://example.com").rstrip("/")

# Optional: text placed at the top of the generated CATALOG.md, one string per line.
CATALOG_INTRO = [
    f"Everything runs against `{BASE_URL}`. No login required.",
]

# Required: the catalog (one dict per row, see catalog_data.py).
from catalog_data import CATALOG  # noqa: E402,F401
