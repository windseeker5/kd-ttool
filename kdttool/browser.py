"""Playwright session helpers: launch, viewport presets, console-error watching.

Two things happen here that a project's scenario scripts never have to know about:

  * **One browser per row, not per block.** Each `with new_page(...)` used to
    call chromium.launch() — a full browser start/stop, ~40 of them in a full
    run. kdttool/runner.py now opens one browser per catalog row and new_page()
    just makes a fresh *context* on it. Contexts are still fully isolated, so a
    logged-out guest page in row 02 stays logged out.
  * **Instrumentation.** Every context gets a Playwright trace and a DOM-level
    listener that reports clicks and field changes to the run's event bus, so
    the live dashboard shows a step-by-step log without a single script edit.

new_page()'s signature is unchanged on purpose — project scripts are untouched.
"""

import os
from contextlib import contextmanager

from playwright.sync_api import sync_playwright

from . import config

# Set by kdttool/runner.py for the duration of one catalog row. None when a script
# is imported and run on its own, in which case new_page() falls back to
# launching (and tearing down) its own browser per call, exactly as before.
_session = None

# Reports clicks and field changes from inside the page. Deliberately reads no
# input values: login() fills the real admin password on every single row, and
# this text ends up in events.jsonl and on screen.
_STEP_PROBE = """
(() => {
  if (window.__uatStepProbe) return;
  window.__uatStepProbe = true;
  const isSecret = (el) =>
    el && el.tagName === 'INPUT' &&
    ['password', 'hidden'].includes((el.getAttribute('type') || '').toLowerCase());
  const label = (el) => {
    if (!el || !el.tagName) return '?';
    const tag = el.tagName.toLowerCase();
    if (el.id) return tag + '#' + el.id;
    if (el.getAttribute && el.getAttribute('name')) return tag + '[name=' + el.getAttribute('name') + ']';
    let out = tag;
    const cls = typeof el.className === 'string' ? el.className.trim().split(/\\s+/)[0] : '';
    if (cls) out += '.' + cls;
    if (tag !== 'input' && tag !== 'select' && tag !== 'textarea') {
      const txt = (el.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 40);
      if (txt) out += ' "' + txt + '"';
    }
    return out;
  };
  document.addEventListener('click', (e) => {
    const target = (e.target.closest && e.target.closest('button, a, label, input, select, [role=button], .btn')) || e.target;
    if (isSecret(target)) return;
    console.debug('[uat-step] click ' + label(target));
  }, true);
  document.addEventListener('change', (e) => {
    if (isSecret(e.target)) return;
    console.debug('[uat-step] fill ' + label(e.target));
  }, true);
})();
"""

# Watches the DOM for modals / dialogs (Bootstrap's `.modal.show`, <dialog open>, SweetAlert ...)
# and gets a picture of each one, without any help from the app or the scripts.
#
# A script is fast: it waits for `.modal.show` and clicks inside the modal a few
# milliseconds later, and a picture taken "after the event" would show a closed modal.
# So the modal is held for a moment instead: as soon as it appears it is made
# unclickable (pointer-events: none), the picture is taken once its fade-in is over,
# then it is released. Playwright simply retries the click until the modal responds,
# so a script only ever loses ~0.3s per modal. A hard 6s cap guarantees the modal can
# never stay stuck. A modal that is opened again later counts as a new sighting.
#
# Native browser pop-ups (window.alert / confirm) are not part of the page, so no
# screenshot can show them; their message is reported as a step instead.
_MODAL_PROBE = """
(() => {
  if (window.__uatModalProbe) return;
  window.__uatModalProbe = true;
  const SEL = '.modal.show, dialog[open], .swal2-popup, [role="dialog"][aria-modal="true"]';
  const open = new Set();
  const shown = (el) => {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
  };
  const title = (el) => {
    const t = el.querySelector('.modal-title, .swal2-title, h1, h2, h3, h4, h5, [id$=Label]');
    const raw = (t && t.innerText) || el.getAttribute('aria-label') || el.id || 'dialog';
    return raw.trim().replace(/\\s+/g, ' ').slice(0, 60);
  };
  const frames = (n) => new Promise((res) => { const f = () => (n-- > 0 ? requestAnimationFrame(f) : res()); f(); });
  // fade-in finished and the dialog stopped sliding
  const settled = async (el) => {
    const box = el.querySelector('.modal-dialog, .swal2-popup') || el;
    const t0 = performance.now();
    let last = '', still = 0;
    while (performance.now() - t0 < 1500 && still < 6) {
      const r = box.getBoundingClientRect();
      const now = [r.top.toFixed(1), r.left.toFixed(1), getComputedStyle(el).opacity].join('|');
      still = (now === last && parseFloat(getComputedStyle(el).opacity) >= 0.99) ? still + 1 : 0;
      last = now;
      await frames(1);
    }
  };
  // Python calls window.__uatModalDone() once the picture is taken.
  const waiting = new Set();
  window.__uatModalDone = () => { for (const f of [...waiting]) f(); waiting.clear(); };
  const announce = async (el) => {
    el.style.setProperty('pointer-events', 'none', 'important');   // hold: the script can't click it yet
    try {
      await settled(el);
      if (open.has(el)) {
        console.debug('[uat-modal] ' + title(el));
        await Promise.race([new Promise((r) => waiting.add(r)), new Promise((r) => setTimeout(r, 6000))]);
      }
    } catch (e) { /* never let a picture problem break the page */ }
    finally { el.style.removeProperty('pointer-events'); }
  };
  let queued = false;
  const check = () => {
    queued = false;
    for (const el of [...open]) if (!el.isConnected || !el.matches(SEL) || !shown(el)) open.delete(el);
    document.querySelectorAll(SEL).forEach((el) => {
      if (open.has(el) || !shown(el)) return;
      open.add(el);
      announce(el);
    });
  };
  const schedule = () => { if (!queued) { queued = true; requestAnimationFrame(check); } };
  const start = () => {
    new MutationObserver(schedule).observe(document.documentElement, {
      subtree: true, childList: true, attributes: true, attributeFilter: ['class', 'open', 'style', 'aria-modal'],
    });
    schedule();
  };
  if (document.documentElement) start(); else document.addEventListener('DOMContentLoaded', start);

  for (const kind of ['alert', 'confirm', 'prompt']) {
    const orig = window[kind];
    window[kind] = function (msg) {
      console.debug('[uat-dialog] ' + kind + ': ' + String(msg == null ? '' : msg).replace(/\\s+/g, ' ').slice(0, 200));
      return orig.apply(this, arguments);
    };
  }
})();
"""


def start_row_session(order, row_dir, bus, headless=None, failure_shots=True, ctx=None):
    """Open one browser for a catalog row. Called by kdttool/runner.py."""
    global _session
    stop_row_session()
    headless = config.HEADLESS if headless is None else headless
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=headless, slow_mo=0 if headless else 150)
    _session = {
        "pw": pw, "browser": browser, "headless": headless,
        "order": order, "row_dir": row_dir, "bus": bus, "traces": 0,
        "failure_shots": failure_shots, "failures": 0,
        # `ctx` lets the browser take numbered screenshots on its own (modals).
        # Same rule as failure shots: rows that type secrets are never photographed.
        "ctx": ctx, "modal_shots": 0,
    }
    return _session


def stop_row_session():
    global _session
    if not _session:
        return
    try:
        _session["browser"].close()
    finally:
        try:
            _session["pw"].stop()
        finally:
            _session = None


def _emit(kind, **fields):
    if _session and _session.get("bus"):
        _session["bus"].emit(kind, order=_session["order"], **fields)


def _next_trace_path():
    """reports/<run_id>/<row>/trace_<n>.zip, or None when running standalone."""
    if not _session or not _session.get("row_dir"):
        return None
    _session["traces"] += 1
    os.makedirs(_session["row_dir"], exist_ok=True)
    return os.path.join(_session["row_dir"], f"trace_{_session['traces']}.zip")


def _on_modal(page, viewport, title):
    """A modal just finished opening (and is being held): narrate it, photograph it when
    the project asks, then let the page carry on."""
    _emit("step", text=f"[{viewport}] modal opened '{title}'")
    try:
        ctx = _session and _session.get("ctx")
        if (ctx and config.SCREENSHOT_MODALS and _session.get("failure_shots")
                and _session["modal_shots"] < config.MAX_MODAL_SHOTS_PER_ROW):
            _session["modal_shots"] += 1
            # Just the visible screen, not the whole page: a full-page capture would put
            # the (fixed-position) modal in the middle of a very tall image.
            ctx.screenshot(page, f"modal_{title}", full_page=False)
    except Exception:  # noqa: BLE001 - a missed picture must never fail the row
        pass
    finally:
        try:
            page.evaluate("() => window.__uatModalDone && window.__uatModalDone()")
        except Exception:  # noqa: BLE001 - the page may already be navigating or closed
            pass


def _instrument(page, viewport):
    """Turn page/DOM activity into `step` events on the run's event bus."""
    if not (_session and _session.get("bus")):
        return

    def on_console(msg):
        if msg.type != "debug":
            return
        text = msg.text or ""
        if text.startswith("[uat-step] "):
            _emit("step", text=f"[{viewport}] {text[len('[uat-step] '):]}")
        elif text.startswith("[uat-modal] "):
            _on_modal(page, viewport, text[len("[uat-modal] "):])
        elif text.startswith("[uat-dialog] "):    # native alert()/confirm(): no picture possible, log the message
            _emit("step", text=f"[{viewport}] browser {text[len('[uat-dialog] '):].replace(': ', ' says: ', 1)}")

    def on_navigated(frame):
        if frame != page.main_frame:
            return
        url = frame.url or ""
        if not url or url == "about:blank":
            return
        # Strip the origin — every row targets the same base_url, so the path is
        # the only part that tells you anything.
        shown = url[len(config.BASE_URL):] if url.startswith(config.BASE_URL) else url
        _emit("step", text=f"[{viewport}] goto {shown or '/'}")

    page.on("console", on_console)
    page.on("framenavigated", on_navigated)
    page.on("download", lambda d: _emit("step", text=f"[{viewport}] download {d.suggested_filename}"))
    page.on("pageerror", lambda exc: _emit("step", text=f"[{viewport}] ⚠ uncaught exception: {exc}", level="error"))


@contextmanager
def new_page(viewport="desktop", headless=None):
    """Yield a fresh logged-out page at the given viewport ("desktop" or "mobile").

    Reuses the current row's browser when kdttool/runner.py has opened one (a new
    isolated context per call), and otherwise launches its own. `headless`
    defaults to config.HEADLESS (`--headed` on run.py flips it); pass it
    explicitly to force one call's mode regardless, as _simulate_payment.py does.
    """
    if viewport not in config.VIEWPORTS:
        raise ValueError(f"Unknown viewport {viewport!r}, expected one of {list(config.VIEWPORTS)}")

    want_headless = config.HEADLESS if headless is None else headless
    reuse = _session is not None and _session["headless"] == want_headless

    if reuse:
        with _page_on(_session["browser"], viewport) as page:
            yield page
        return

    # No row session, or this call wants the opposite headless mode from the
    # row's browser (_simulate_payment.py forces headless=True even under
    # --headed). Launch a second browser for it — reusing the row's *Playwright
    # instance* when there is one, since the sync API doesn't like two live
    # instances on the same thread.
    if _session:
        browser = _session["pw"].chromium.launch(
            headless=want_headless, slow_mo=0 if want_headless else 150
        )
        try:
            with _page_on(browser, viewport) as page:
                yield page
        finally:
            browser.close()
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=want_headless, slow_mo=0 if want_headless else 150)
        try:
            with _page_on(browser, viewport) as page:
                yield page
        finally:
            browser.close()


def _failure_screenshot(page, viewport):
    """Best-effort picture of the page at the moment a row raised. Skipped for
    `sensitive` rows, whose pages may be showing secrets."""
    if not (_session and _session.get("row_dir") and _session.get("failure_shots")):
        return
    try:
        _session["failures"] += 1
        os.makedirs(_session["row_dir"], exist_ok=True)
        name = f"FAILED_{viewport}_{_session['failures']}.png"
        page.screenshot(path=os.path.join(_session["row_dir"], name), full_page=True)
    except Exception:  # noqa: BLE001 - never mask the real failure
        pass


@contextmanager
def _page_on(browser, viewport):
    context = browser.new_context(
        viewport=config.VIEWPORTS[viewport],
        accept_downloads=True,
    )
    trace_path = _next_trace_path()
    if trace_path:
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
    context.add_init_script(_STEP_PROBE)
    if config.SCREENSHOT_MODALS and _session and _session.get("bus"):
        context.add_init_script(_MODAL_PROBE)
    page = context.new_page()
    _instrument(page, viewport)
    try:
        yield page
    except Exception:
        _failure_screenshot(page, viewport)
        raise
    finally:
        if trace_path:
            try:
                context.tracing.stop(path=trace_path)
            except Exception:  # noqa: BLE001 - a broken trace must never fail a row
                pass
        context.close()


def watch_console_errors(page):
    """Attach listeners that collect JS console errors and uncaught page errors.

    Returns a list that fills in as the page runs — check it (e.g. `if errors:
    raise AssertionError(errors)`) after whatever navigation/actions you want
    covered. Attach this before navigating so nothing is missed.
    """
    errors = []
    page.on("console", lambda msg: errors.append(f"console.{msg.type}: {msg.text}") if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(f"uncaught exception: {exc}"))
    return errors
