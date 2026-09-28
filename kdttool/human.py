"""Pause a running test until a person has done something by hand (pay with a real card,
send an e-transfer, check an inbox).

    from kdttool.human import pause
    pause("Type your card in the Stripe window and pay, then continue.")

The runner points this at the current row's event bus, so the dashboard shows the message and
a Continue button (it answers by writing a line to this process's stdin). Run from a terminal,
it is a plain "press Enter" prompt — the same line on stdin either way.
"""

import sys

_bus = None
_order = None


def attach(bus, order):
    """Called by the runner at the start of each row."""
    global _bus, _order
    _bus, _order = bus, order


def pause(message):
    print("\n" + "=" * 78, flush=True)
    print(message, flush=True)
    print("Paused. Press Continue in the dashboard, or Enter here, once that's done.", flush=True)
    if _bus:
        _bus.emit("pause", order=_order, text=message)
    line = sys.stdin.readline()
    if _bus:
        _bus.emit("resume", order=_order)
    print("=" * 78 + "\n", flush=True)
    if line == "":
        raise AssertionError("Stopped while waiting for a person (no input — the run was cancelled).")


def wait_for(message, done, tick=None, interval_s=2, not_yet="Not done yet — still waiting."):
    """Wait — with NO time limit — until a person has finished something `done()` can see, e.g.
    paying on Stripe (done = "the browser is back on the app's success page").

    It carries on by itself as soon as `done()` is true, or when the person says they're done
    (the dashboard's Resume button, or Enter in the terminal: a line on stdin). If they say so
    before `done()` agrees, it notes `not_yet` and keeps waiting instead of failing.

    tick(seconds) is how to wait between checks. With a Playwright page, pass
    `tick=lambda s: page.wait_for_timeout(s * 1000)`: a plain sleep stops Playwright from
    processing the browser's events, so page.url would never see a redirect.
    """
    import select
    import time

    print("\n" + "=" * 78, flush=True)
    print(message, flush=True)
    print("Waiting for you (no time limit). It continues by itself when it's done, "
          "or press Resume in the dashboard / Enter here.", flush=True)
    if _bus:
        _bus.emit("pause", order=_order, text=message, auto=True)
    try:
        while not done():
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if ready:
                if sys.stdin.readline() == "":
                    raise AssertionError("Stopped while waiting for a person (the run was cancelled).")
                if done():
                    break
                print(not_yet, flush=True)
                if _bus:
                    _bus.emit("note", order=_order, text=not_yet)
            (tick or time.sleep)(interval_s)
    finally:
        if _bus:
            _bus.emit("resume", order=_order)
        print("=" * 78 + "\n", flush=True)
