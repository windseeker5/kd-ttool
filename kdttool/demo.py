"""Demo mode: plays a made-up run through the dashboard so the UI/UX can be looked at
without a browser session against a real site or a real catalog.

Nothing here touches a project's catalog or scripts. The fake screenshots are drawn
from small inline HTML mock-ups (rendered locally by Playwright, no network), and the
run lands in projects/example/reports/demo/ like any other run, so the Export report
button and `--report demo` work on it too.
"""

import json
import os
import shutil
import tempfile
import time
import traceback

from . import config, scriptgen
from .events import EventBus

RUN_ID = "demo"

ROWS = [
    dict(order="01", area="Signup", script="01_signup.py", title="Visitor signs up", viewport="desktop + mobile",
         summary="A new visitor creates an account.",
         description="Fills the signup form, submits it, and lands on the welcome page.",
         verifies=["Form validates an empty email", "Welcome page shows the new name"], money=False),
    dict(order="02", area="Login", script="02_login.py", title="Existing user logs in", viewport="desktop",
         summary="Login with a good and a bad password.",
         description="Logs in with a wrong password (expects an error), then the right one.",
         verifies=["Wrong password is refused", "Right password reaches the dashboard"], money=False),
    dict(order="03", area="Catalog", script="03_products.py", title="Products list and search", viewport="desktop + mobile",
         summary="Browse and search the product list.",
         description="Opens the list, searches for a product, opens its page.",
         verifies=["Search narrows the list", "Product page loads"], money=False),
    dict(order="04", area="Checkout", script="04_checkout.py", title="Checkout with a test card", viewport="desktop",
         summary="Buy something with a test card.",
         description="Adds an item to the cart and pays. This one is meant to fail in the demo.",
         verifies=["Order confirmation appears"], money=False),
    dict(order="05", area="Settings", script="05_settings.py", title="Change profile settings", viewport="desktop",
         summary="Edit and save the profile.",
         description="Changes the display name and reloads to check it stuck.",
         verifies=["Saved message shows", "New name survives a reload"], money=False),
    dict(order="90", area="Payments", script="90_real_payment.py", title="Real payment (real money)", viewport="desktop",
         summary="Moves real money, so it only runs on purpose.",
         description="Skipped in the demo, like any real-money row without --confirm-money.",
         verifies=["Payment goes through"], money=True),
]

# (kind of mock page, caption) per screenshot, and the steps/notes narrated before them.
SCRIPT = {
    "01": dict(steps=["[desktop] goto /signup", "[desktop] fill input[name=email]", "[desktop] click button 'Create account'",
                      "[mobile] goto /signup", "[mobile] click button 'Create account'"],
               notes=["Empty email is refused with 'Email is required'.", "[mobile] layout fits a 390px screen."],
               shots=[("form", "signup_empty", "desktop"), ("welcome", "welcome_desktop", "desktop"), ("form", "signup_mobile", "mobile")]),
    "02": dict(steps=["[desktop] goto /login", "[desktop] fill input[name=password]", "[desktop] click button 'Log in'",
                      "[desktop] goto /dashboard"],
               notes=["Wrong password shows 'Invalid credentials'."],
               shots=[("error", "wrong_password", "desktop"), ("dashboard", "dashboard", "desktop")]),
    "03": dict(steps=["[desktop] goto /products", "[desktop] fill input[type=search]", "[desktop] click a.product-card",
                      "[mobile] goto /products"],
               notes=["Search for 'lamp' returned 3 of 24 products."],
               shots=[("list", "product_list", "desktop"), ("list", "search_lamp", "desktop"), ("list", "list_mobile", "mobile")]),
    "04": dict(steps=["[desktop] goto /cart", "[desktop] click button 'Pay now'", "[desktop] ⚠ uncaught exception: card_error"],
               notes=["Cart holds 1 item."],
               shots=[("cart", "cart", "desktop")],
               fail="AssertionError: expected 'Order confirmed' but the page showed 'Payment failed'",
               fail_shot=("error", "failure", "desktop")),
    "05": dict(steps=["[desktop] goto /settings", "[desktop] fill input[name=display_name]", "[desktop] click button 'Save'",
                      "[desktop] goto /settings"],
               notes=["Saved message appeared.", "New name still there after reload."],
               shots=[("settings", "settings_saved", "desktop")]),
}


def _page_html(kind, title):
    body = {
        "form": "<h2>Create your account</h2><label>Email</label><div class=f></div><label>Password</label><div class=f></div><button>Create account</button>",
        "welcome": "<h2>Welcome, Alex!</h2><div class=cards><div class=c><b>0</b>orders</div><div class=c><b>0</b>points</div><div class=c><b>1</b>profile</div></div>",
        "error": "<div class=err>Something went wrong — please try again.</div><h2>Log in</h2><label>Email</label><div class=f></div><label>Password</label><div class=f></div><button>Log in</button>",
        "dashboard": "<h2>Dashboard</h2><div class=cards><div class=c><b>12</b>orders</div><div class=c><b>340</b>points</div><div class=c><b>3</b>alerts</div></div><div class=t><i></i><i></i><i></i><i></i></div>",
        "list": "<h2>Products</h2><div class=cards><div class=c>Lamp<br><b>$24</b></div><div class=c>Chair<br><b>$89</b></div><div class=c>Desk<br><b>$210</b></div><div class=c>Shelf<br><b>$55</b></div></div>",
        "cart": "<h2>Your cart</h2><div class=t><i></i><i></i></div><button>Pay now</button>",
        "settings": "<h2>Profile settings</h2><div class=ok>Saved.</div><label>Display name</label><div class=f></div><button>Save</button>",
    }[kind]
    return f"""<html><body style="margin:0;font:14px system-ui;background:#f4f6fa;color:#1c2026">
<style>.bar{{background:#2b3a67;color:#fff;padding:14px 20px;font-weight:600}}main{{padding:20px;max-width:760px}}
.f{{height:34px;border:1px solid #c9ced8;border-radius:6px;background:#fff;margin:4px 0 12px}}label{{font-size:12px;color:#5b6472}}
button{{background:#2b3a67;color:#fff;border:0;border-radius:6px;padding:10px 18px}}.cards{{display:flex;gap:12px;flex-wrap:wrap}}
.c{{background:#fff;border:1px solid #dde1e8;border-radius:8px;padding:16px;min-width:110px}}.c b{{display:block;font-size:22px}}
.err{{background:#fde8e8;color:#b42318;padding:10px;border-radius:6px;margin-bottom:12px}}.ok{{background:#e6f6ec;color:#1a7f4b;padding:10px;border-radius:6px;margin-bottom:12px}}
.t i{{display:block;height:38px;background:#fff;border:1px solid #dde1e8;border-radius:6px;margin-bottom:8px}}</style>
<div class=bar>Demo Shop · {title}</div><main>{body}</main></body></html>"""


def _render_shots(run_dir):
    """Draw every mock screenshot into the run dir. Returns {(order, label): rel_path}."""
    from playwright.sync_api import sync_playwright
    paths = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for order, spec in SCRIPT.items():
            row_dir_name = f"{order}_{next(r['script'] for r in ROWS if r['order'] == order)[:-3]}"
            os.makedirs(os.path.join(run_dir, row_dir_name), exist_ok=True)
            shots = list(spec["shots"]) + ([spec["fail_shot"]] if spec.get("fail_shot") else [])
            for i, (kind, label, viewport) in enumerate(shots, 1):
                size = {"width": 390, "height": 700} if viewport == "mobile" else {"width": 1100, "height": 620}
                page = browser.new_page(viewport=size)
                page.set_content(_page_html(kind, label.replace("_", " ")))
                name = f"FAILED_{viewport}_1.png" if label == "failure" else f"{i:02d}_{label}.png"
                rel = os.path.join(row_dir_name, name)
                page.screenshot(path=os.path.join(run_dir, rel), full_page=True)
                page.close()
                paths[(order, label)] = rel
        browser.close()
    return paths


def play(bus, run_dir, pace=1.0):
    """Emit the fake run's events with small delays, as a real run would."""
    paths = _render_shots(run_dir)

    def pause(s):
        time.sleep(s * pace)

    bus.emit("run_start", run_id=RUN_ID, base_url="https://demo.example (fake)", headless=True, rows=ROWS)
    passed = failed = skipped = 0
    pause(1.0)
    for row in ROWS:
        order = row["order"]
        if row["money"]:
            bus.emit("row_end", order=order, status="skipped", duration_s=0.0, reason="requires --confirm-money")
            skipped += 1
            continue
        spec = SCRIPT[order]
        bus.emit("row_start", order=order, area=row["area"], script=row["script"])
        started = time.monotonic()
        for i, step in enumerate(spec["steps"]):
            pause(0.5)
            bus.emit("step", order=order, text=step, **({"level": "error"} if "⚠" in step else {}))
            if i < len(spec["shots"]):
                label = spec["shots"][i][1]
                pause(0.4)
                bus.emit("screenshot", order=order, label=label, path=paths[(order, label)])
        for note in spec["notes"]:
            bus.emit("note", order=order, text=note)
        # any screenshots not already shown alongside a step
        for _, label, _v in spec["shots"][len(spec["steps"]):]:
            bus.emit("screenshot", order=order, label=label, path=paths[(order, label)])
        error = None
        if spec.get("fail"):
            label = spec["fail_shot"][1]
            bus.emit("screenshot", order=order, label=label, path=paths[(order, label)])
            try:
                raise AssertionError(spec["fail"].split(": ", 1)[1])
            except AssertionError:
                error = traceback.format_exc()
            status = "fail"
            failed += 1
        else:
            status = "pass"
            passed += 1
        pause(0.6)
        bus.emit("row_end", order=order, status=status, duration_s=round(time.monotonic() - started, 1),
                 error=error, trace_dir=f"{order}_{row['script'][:-3]}")
    bus.emit("run_end", passed=passed, failed=failed, skipped=skipped, report_path=None)


def _sandbox():
    """Point the tool at a throwaway project folder so editing the demo catalog never touches a real project."""
    root = tempfile.mkdtemp(prefix="kdttool-demo-")
    config.PROJECT_DIR = root
    config.SCRIPTS_DIR = os.path.join(root, "scripts")
    config.REPORTS_DIR = os.path.join(root, "reports")
    config.CATALOG_MD_PATH = os.path.join(root, "CATALOG.md")
    os.makedirs(config.SCRIPTS_DIR)
    os.makedirs(config.REPORTS_DIR)
    rows = []
    for r in ROWS:
        row = dict(
            order=r["order"], script=r["script"], area=r["area"], title=r["title"], summary=r["summary"],
            description=r["description"], credentials="demo@example.com", email="n/a",
            viewport=r["viewport"].replace(" + ", "+"), verifies="; ".join(r["verifies"]),
            money=r["money"], manual=False, sensitive=False,
        )
        rows.append(row)
        with open(os.path.join(config.SCRIPTS_DIR, r["script"]), "w") as f:
            f.write(f'"""{r["title"]} (demo script)."""\n\nfrom kdttool.browser import new_page\n\n\n'
                    "def run(ctx):\n    with new_page() as page:\n        page.goto(ctx.base_url)\n"
                    f"        ctx.screenshot(page, {r['order']!r})\n")
    config.CATALOG[:] = rows
    scriptgen.FAKE = True
    with open(os.path.join(root, "catalog.json"), "w") as f:
        json.dump(rows, f, indent=2)


def start_demo(pace=1.0):
    """Prepare a clean demo run dir and return (bus, run_dir, player_thread)."""
    import threading
    _sandbox()
    run_dir = config.run_dir(RUN_ID)
    shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(run_dir)
    bus = EventBus(jsonl_path=os.path.join(run_dir, "events.jsonl"))
    thread = threading.Thread(target=play, args=(bus, run_dir, pace), daemon=True)
    return bus, run_dir, thread
