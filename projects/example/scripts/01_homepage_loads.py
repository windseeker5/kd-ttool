"""Row 01 — the homepage loads cleanly at desktop and mobile sizes."""

from kdttool.browser import new_page, watch_console_errors


def run(ctx):
    for viewport in ("desktop", "mobile"):
        with new_page(viewport=viewport) as page:
            errors = watch_console_errors(page)
            page.goto(ctx.base_url)
            page.wait_for_load_state("networkidle")

            title = page.title()
            if not title:
                raise AssertionError(f"[{viewport}] homepage has no <title>.")
            ctx.note(f"[{viewport}] loaded {ctx.base_url} — title: {title!r}")
            ctx.screenshot(page, f"homepage_{viewport}")

            if errors:
                raise AssertionError(f"[{viewport}] JavaScript errors: {errors}")
