"""The catalog: single source of truth for what this project tests.

CATALOG.md is *generated* from this list — edit a row here (and its script),
never the generated table.

Fields:
  order        str, matches the script filename prefix and run order
  script       filename in scripts/, or None for a [MANUAL] catalog-only row
  title        short name shown in the dashboard's test list
  summary      one plain line shown collapsed in the dashboard
  area         short section name
  description  plain-English description of what the script does
  credentials  which login the script uses ("none" if public)
  email        address used for anything the script creates ("n/a" if none)
  viewport     "desktop+mobile" | "desktop" | "mobile" | "n/a"
  verifies     what it checks
  money        True if the step moves real money (only runs with --confirm-money)
  manual       True if the row is never automated (catalog-only reminder)
"""

CATALOG = [
    dict(
        order="01", script="01_homepage_loads.py", area="Smoke", title="Homepage loads",
        summary="Does the homepage load with a title and no JavaScript errors, on desktop and phone?",
        description="Open the homepage at a desktop and a mobile viewport, take a screenshot of each, and "
                    "check the page has a title and logged no JavaScript errors.",
        credentials="none", email="n/a", viewport="desktop+mobile",
        verifies="Page title is not empty; no console errors or uncaught exceptions; a screenshot per viewport.",
        money=False, manual=False,
    ),
    dict(
        order="02", script="02_links_work.py", area="Smoke", title="Links work",
        summary="Does every link on the homepage point somewhere real?",
        description="Collect every link on the homepage and check none is empty or a bare '#'.",
        credentials="none", email="n/a", viewport="desktop",
        verifies="Every <a> on the homepage has a real href.",
        money=False, manual=False,
    ),
    dict(
        order="90", script=None, area="[MANUAL] Example", title="Manual check (by hand)",
        summary="A reminder for something a person must check; never automated.",
        description="Manual rows appear in the catalog and in the end-of-run reminder list, but no script runs.",
        credentials="n/a", email="n/a", viewport="n/a",
        verifies="Manual — no automated check.",
        money=False, manual=True,
    ),
]
