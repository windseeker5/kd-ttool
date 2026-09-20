"""Row 02 — every link on the homepage has a real destination."""

from kdttool.browser import new_page


def run(ctx):
    with new_page(viewport="desktop") as page:
        page.goto(ctx.base_url)
        hrefs = page.eval_on_selector_all("a", "els => els.map(e => e.getAttribute('href'))")
        ctx.note(f"Found {len(hrefs)} link(s) on the homepage.")
        ctx.step(f"checking {len(hrefs)} link(s)")

        empty = [h for h in hrefs if not h or h.strip() in ("", "#")]
        if empty:
            raise AssertionError(f"{len(empty)} link(s) have no real destination: {empty}")
