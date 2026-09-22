# KD-TTOOL

An automated browser test runner for **user acceptance testing (UAT)**. You describe your tests in a
plain-English **catalog**, write one small Playwright script per row, and KD-TTOOL runs them, shows
progress on a **live dashboard**, and saves a **self-contained report** (screenshots, step log, and a
Playwright trace for anything that failed).

It is built around *projects*: the engine is generic, and each site or app you test is a folder under
`projects/`.

## Why

- **A catalog anyone can read.** Every test row has a title, a plain-English description and what it
  verifies. `CATALOG.md` is generated from it, with the last result per row.
- **Watch it run.** A local dashboard shows the row grid, a live step log and each screenshot the moment
  it is taken. Old runs can be replayed.
- **Evidence, not just pass/fail.** Each run gets its own folder with a report and screenshots.
- **Safe by default.** Rows that move real money only run with `--confirm-money`; manual rows are
  catalog-only reminders that are never automated.

## Quick start

```bash
python -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/playwright install chromium

./venv/bin/python run.py --list-projects
./venv/bin/python run.py --project example        # runs the bundled example against example.com
```

Common options:

```bash
./venv/bin/python run.py --project example --only 01,02        # specific catalog rows
./venv/bin/python run.py --project example --headed            # watch a real browser window
./venv/bin/python run.py --project example --wait              # start the tool, then click Play in the dashboard
./venv/bin/python run.py --project example --no-dashboard      # no live dashboard
./venv/bin/python run.py --project example --replay <run_id>   # reopen a past run's dashboard
./venv/bin/python run.py --project example --report <run_id>   # rebuild a past run's report.html
./venv/bin/python run.py --project example --export <run_id>   # standalone folder + zip of a run
./venv/bin/python run.py --project example --confirm-money     # also run rows that move real money
```

Each run writes `projects/<project>/reports/<run_id>/` (`report.html`, `report.md`, `events.jsonl`, one
folder of screenshots per row) and refreshes `projects/<project>/CATALOG.md`.

**`report.html` is the one to open.** It is a single file with every screenshot embedded (click one to
enlarge), so you can double-click it or email it. It is also written if a run is interrupted, and
`--report <run_id>` rebuilds it for any past run. Screenshots are numbered per row (`01_home.png`,
`02_home.png`, ...) so a repeated label never overwrites one, and a failed row gets an automatic
`FAILED_*.png` of the page as it was (skipped for `sensitive` rows).

**Export report** (button at the top of the dashboard, or `--export <run_id>`) builds a standalone folder,
`reports/<run_id>/export/`, plus `export.zip`: `index.html` (with the step log), an `images/` folder of the
real PNG files, `events.jsonl`, and `traces/` for failed rows. Zip it and send it; it needs nothing else.

## Starting from the dashboard (`--wait`)

`run.py --wait` starts the tool and does **not** start the tests, and does not open a browser tab for you:
use the dashboard tab you already have open (it reconnects by itself if the tool is restarted) or open the
address it prints. The page shows the target and the catalog, and lets you edit it; the run begins when you
click **Play**, and a browser window pops up to do the tests (untick **Show browser** to run it invisibly). `--headed`, `--only` and `--confirm-money` still apply. The run's folder and timestamp
are created at the moment you press Start, so quitting without starting leaves nothing behind.

## Screenshots of modals

A project can set `SCREENSHOT_MODALS = True` in `project.py`. Every time a modal (Bootstrap `.modal.show`,
`<dialog>`, SweetAlert) finishes opening, the engine takes a screenshot of it (`modal_<title>`, numbered with
the row's other screenshots) and logs a "modal opened" step. The modal is held for about a third of a second
while the picture is taken, so even a script that clicks inside it immediately can't close it first.
Rows marked `sensitive` are never captured. Native browser pop-ups (`alert()` / `confirm()`) are not part of
the page, so they can't be photographed; their message is logged as a step instead.

## Editing tests from the dashboard

The left side of the dashboard is the list of tests, each showing its catalog summary and two icons:

- **Catalog icon (book):** opens an editor that slides in from the left. Change the title, the plain-English
  description or what it must check, then **Save**. The catalog is stored in `projects/<name>/catalog.json`
  (created on the first save; `catalog_data.py` is left alone) and `CATALOG.md` is regenerated. Every save
  first copies the old catalog into `projects/<name>/.history/`.
- **Script icon (`</>`):** shows the script's code. **Rebuild from catalog** asks `claude -p` (Claude Code on
  this machine, read-only tools) to rewrite that one script from your words, then shows a diff. Nothing is
  replaced until you press **Approve**; the old script is backed up in `.history/`.
- **`+` between two tests** (hover a test) or **+ Add test:** type what the new test should do; it is saved to the
  catalog as a draft, then **Build script** writes its code the same way.

Edits are refused while a run is in progress. Drag the divider to resize the list. `python run.py --demo` lets you
try all of this on a made-up catalog without touching a real project.

## Watching a run: two screens

Start a run with a visible browser and the live dashboard:

```bash
./venv/bin/python run.py --project <name> --headed
```

- **Screen 1, the dashboard:** opens by itself in your browser (address printed at the start, port 8899
  or the next free one). Rows fill in live with the step log and each screenshot.
- **Screen 2, the test browser:** `--headed` opens a real Chrome window that clicks through the site.
  Put the two side by side.

Leave out `--headed` to run hidden (dashboard only). Add `--only 01,01d` for specific rows, and
`--confirm-money` only when you are present to pay. Reopen a finished run with `--replay <run_id>`.

## Layout

```
kdttool/                 the engine: runner, catalog, dashboard, events, report, browser helpers, config
run.py                   command-line entry point
projects/
  example/               a working, credential-free template project (tracked in git)
    project.py           settings: BASE_URL, secrets read from .env, optional CATALOG_INTRO ...
    catalog_data.py      the catalog: one dict per row
    scripts/             one file per row, each exposing run(ctx)
    .env.example         placeholder for secrets (the real .env is never committed)
    reports/             generated output (git-ignored)
```

A project can also contain a `helpers/` package (shared login/fixture code, imported as
`from helpers.x import ...`) and a `fixtures/` folder of files your scripts upload.

## Creating your own project

1. Copy the template: `cp -r projects/example projects/mysite`
2. Edit `projects/mysite/project.py`: set `BASE_URL` and anything else your scripts need. Every UPPER_CASE
   name in `project.py` is available to scripts as `kdttool.config.<NAME>`.
3. Describe your tests in `catalog_data.py`, and write one script per row in `scripts/`:

   ```python
   from kdttool.browser import new_page

   def run(ctx):
       with new_page(viewport="desktop") as page:   # isolated, traced, dashboard-instrumented
           page.goto(ctx.base_url)
           ctx.screenshot(page, "home")
           ctx.note("Homepage loaded")              # kept in the report
           ctx.step("checking something")           # live log only
   ```

   A script passes if `run(ctx)` returns and fails if it raises.
4. Run it: `./venv/bin/python run.py --project mysite`

## Keeping projects private

Only `projects/example/` is tracked by git. Everything else under `projects/` is ignored (see
`.gitignore`), so your own projects, their catalogs, target URLs and credentials stay on your machine and
cannot be pushed by accident. Put secrets in `projects/<name>/.env` (also ignored); commit only
`.env.example` with placeholders. If you want your project versioned, give it its own **private**
repository.

A project folder may also hold its own `README.md` listing what to improve next.

## Requirements

Python 3.10+, `playwright` and `python-dotenv` (see `requirements.txt`).
