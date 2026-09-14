"""
What happens to the price AFTER we alert?  Event study on the real tape.

For every settled alert, track our-side price at fixed horizons after the alert
and at settlement. If alerts fire at local extremes (a big taker order just
pushed the price), the path will retrace. If they carry information, it will
continue. This is the cleanest test of whether the signal is momentum or noise.
"""
import bisect
import os
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from maker_backtest import load, boot, pdt, roi

HORIZONS = [("+30s", 30), ("+2m", 120), ("+5m", 300), ("+15m", 900),
            ("+30m", 1800), ("+1h", 3600), ("+4h", 14400)]
random.seed(5)


def price_at(a, secs):
    """Last our-side trade price at t0+secs; None if no trade yet or market closed."""
    t = a["t0"] + timedelta(seconds=secs)
    if a["close"] and t > a["close"]:
        return None
    idx = bisect.bisect_right(a["times"], t) - 1
    if idx < 0:
        return None
    return a["post"][idx][1]


def main():
    A = [a for a in load() if a["post"]]
    print("=" * 90)
    print("PRICE PATH AFTER ALERT  (our side, cents; n={} alerts with tape)".format(len(A)))
    print("=" * 90)
    print("{:<8} {:>5} {:>12} {:>12} {:>12} {:>14}".format(
        "horizon", "n", "avg move", "median", "% higher", "% lower"))
    print("-" * 90)
    for name, secs in HORIZONS:
        mv = []
        for a in A:
            p = price_at(a, secs)
            if p is not None:
                mv.append(p - a["entry"])
        if not mv:
            continue
        mv.sort()
        up = sum(1 for x in mv if x > 0) / len(mv)
        dn = sum(1 for x in mv if x < 0) / len(mv)
        print("{:<8} {:>5} {:>+11.1f}c {:>+11.1f}c {:>11.0%} {:>13.0%}".format(
            name, len(mv), sum(mv) / len(mv), mv[len(mv) // 2], up, dn))
    # settlement = 100 if we won else 0
    mv = [(100 if a["win"] else 0) - a["entry"] for a in A]
    print("{:<8} {:>5} {:>+11.1f}c {:>11s} {:>11.0%} {:>13.0%}   (win / lose)".format(
        "settle", len(mv), sum(mv) / len(mv), "-",
        sum(1 for a in A if a["win"]) / len(A), sum(1 for a in A if not a["win"]) / len(A)))

    print()
    print("=" * 90)
    print("THE FIRST PRINT AFTER THE ALERT vs THE PRICE WE RECORDED")
    print("=" * 90)
    gap = [a["post"][0][1] - a["entry"] for a in A]
    gap.sort()
    print("first post-alert trade minus recorded entry (our side):")
    print("  mean {:+.1f}c   median {:+.1f}c   p10 {:+.0f}c   p90 {:+.0f}c".format(
        sum(gap) / len(gap), gap[len(gap) // 2], gap[len(gap) // 10], gap[9 * len(gap) // 10]))
    print("  share where the next print was ABOVE our recorded entry: {:.0%}".format(
        sum(1 for g in gap if g > 0) / len(gap)))
    print("  => the price a taker would ACTUALLY have paid vs what the alert said")

    # realistic taker: enter at the first post-alert print, not the alert price
    print()
    print("=" * 90)
    print("TAKER ROI IF ENTERED AT THE FIRST REAL PRINT AFTER THE ALERT (not the alert price)")
    print("=" * 90)
    v_rec = [roi(a["entry"], a["win"], False) for a in A]
    v_real = []
    for a in A:
        p = a["post"][0][1]
        if 1 <= p <= 99:
            v_real.append(roi(p, a["win"], False))
    for name, v in (("at recorded alert price", v_rec), ("at first real print    ", v_real)):
        m, lo, hi = boot(v)
        print("  {}  n={:>3}  ROI {:>+7.1%} [{:>+6.1%},{:>+6.1%}]".format(name, len(v), m, lo, hi))

    print()
    print("=" * 90)
    print("MOMENTUM CONFIRMATION: wait W, enter as taker only if price has NOT retraced")
    print("(tuned on first half, blind on second)")
    print("=" * 90)
    mid = len(A) // 2
    H1, H2 = A[:mid], A[mid:]

    def run(H, W, tol):
        out = []
        for a in H:
            p = price_at(a, W)
            if p is None or p < a["entry"] - tol or not (1 <= p <= 99):
                continue
            out.append((roi(p + 1, a["win"], False), a["win"]))   # +1c: cross the spread
        return out

    best = None
    print("  first half grid (W seconds x retrace tolerance), ROI / n:")
    for W in (120, 300, 900, 1800):
        row = "    W={:>5}s ".format(W)
        for tol in (0, 1, 2):
            o = run(H1, W, tol)
            m = sum(x[0] for x in o) / len(o) if o else 0
            row += "  tol{}: {:>+6.1%} (n{:>3})".format(tol, m, len(o))
            if len(o) >= 30 and (best is None or m > best[0]):
                best = (m, W, tol)
        print(row)
    if best:
        _, W, tol = best
        o1 = run(H1, W, tol)
        o2 = run(H2, W, tol)
        for name, o, H in (("first half (tuned)", o1, H1), ("SECOND HALF (blind)", o2, H2)):
            v = [x[0] for x in o]
            m, lo, hi = boot(v)
            w = sum(1 for x in o if x[1]) / len(o) if o else 0
            flag = "  <== PROFITABLE" if lo > 0 else ("  <== LOSING" if hi < 0 else "")
            print("  {:<20} W={}s tol={}  n={:>3} ({:.0%} of alerts)  win={:.1%}  ROI {:+.1%} [{:+.1%},{:+.1%}]{}".format(
                name, W, tol, len(o), len(o) / len(H), w, m, lo, hi, flag))


if __name__ == "__main__":
    main()
