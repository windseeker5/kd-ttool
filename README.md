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
./venv/bin/python run.py --project example --no-dashboard      # no live dashboard
./venv/bin/python run.py --project example --replay <run_id>   # reopen a past run's dashboard
./venv/bin/python run.py --project example --confirm-money     # also run rows that move real money
```

Each run writes `projects/<project>/reports/<run_id>/` (`report.md`, `events.jsonl`, one folder of
screenshots per row) and refreshes `projects/<project>/CATALOG.md`.

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

## Requirements

Python 3.10+, `playwright` and `python-dotenv` (see `requirements.txt`).
