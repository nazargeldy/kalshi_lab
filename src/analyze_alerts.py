"""
What is working and what is not, on the live Kalshi alert stream.

Breaks settled alerts down by score, side, entry price, signal type, category and
time-to-close, and reports per-trade ROI with a bootstrap CI so we don't chase
noise. ROI is equal-weighted (what you actually experience betting fixed stakes),
not dollar-weighted.
"""
import math
import os
import random
import re
import sqlite3
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "kalshi_alerts.db")
random.seed(11)


def fee(price_c, contracts=1.0):
    p = price_c / 100.0
    return math.ceil(0.07 * contracts * p * (1 - p) * 100) / 100.0


def roi(entry_c, won):
    """Per-dollar-staked return for one contract bought at entry_c."""
    cost = entry_c / 100.0 + fee(entry_c)
    payout = 1.0 if won else 0.0
    return (payout - cost) / cost


def wilson(k, n, z=1.96):
    if not n:
        return (0, 0, 0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (p, max(0, c - h), min(1, c + h))


def boot(vals, n=3000):
    if not vals:
        return (0, 0, 0)
    N = len(vals)
    ms = sorted(sum(vals[random.randrange(N)] for _ in range(N)) / N for _ in range(n))
    return (sum(vals) / N, ms[int(0.025 * n)], ms[int(0.975 * n)])


def load():
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT ts,ticker,event_ticker,title,side,entry_cents,score,reasons,result "
        "FROM alerts WHERE result IN ('WIN','LOSS') ORDER BY ts").fetchall()
    out = []
    for ts, tk, ev, title, side, ec, sc, rs, res in rows:
        if ec is None or not (1 <= ec <= 99):
            continue
        out.append({"ts": ts, "ticker": tk, "event": ev, "title": title or "",
                    "side": side, "entry": ec, "score": sc or 0,
                    "reasons": rs or "", "win": res == "WIN",
                    "roi": roi(ec, res == "WIN"),
                    "series": (tk or "").split("-")[0]})
    return out


def table(rows, keyfn, name, min_n=5):
    g = defaultdict(list)
    for r in rows:
        g[keyfn(r)].append(r)
    print("\n{:<26} {:>4} {:>7} {:>18} {:>10} {:>16}".format(
        name, "n", "win%", "win 95% CI", "ROI", "ROI 95% CI"))
    print("-" * 86)
    for k, rs in sorted(g.items(), key=lambda x: -len(x[1])):
        n = len(rs)
        if n < min_n:
            continue
        w = sum(1 for r in rs if r["win"])
        _, cl, ch = wilson(w, n)
        m, lo, hi = boot([r["roi"] for r in rs])
        flag = ""
        if lo > 0:
            flag = "  <== PROFITABLE"
        elif hi < 0:
            flag = "  <== LOSING"
        print("{:<26} {:>4} {:>6.1%} [{:>5.2f},{:>5.2f}] {:>+9.1%} [{:>+6.1%},{:>+6.1%}]{}".format(
            str(k)[:26], n, w / n, cl, ch, m, lo, hi, flag))


def main():
    rows = load()
    if len(rows) < 5:
        sys.exit("Not enough settled alerts yet ({}).".format(len(rows)))

    w = sum(1 for r in rows if r["win"])
    m, lo, hi = boot([r["roi"] for r in rows])
    print("=" * 86)
    print("LIVE KALSHI ALERTS - {} settled  ({} .. {})".format(
        len(rows), rows[0]["ts"][:10], rows[-1]["ts"][:10]))
    print("=" * 86)
    print("Overall: {}W/{}L = {:.1%}   ROI {:+.1%}  CI[{:+.1%},{:+.1%}]".format(
        w, len(rows) - w, w / len(rows), m, lo, hi))
    print("Break-even needs win% > avg entry price. avg entry = {:.0f}c".format(
        sum(r["entry"] for r in rows) / len(rows)))

    table(rows, lambda r: r["side"], "BY SIDE")

    def band(r):
        s = int(r["score"])
        return "{}-{}".format(s // 5 * 5, s // 5 * 5 + 4)
    table(rows, band, "BY SCORE")

    def pb(r):
        e = r["entry"]
        if e <= 25:
            return "cheap 12-25c"
        if e <= 40:
            return "26-40c"
        if e <= 60:
            return "41-60c"
        if e <= 75:
            return "61-75c"
        return "expensive 76-88c"
    table(rows, pb, "BY ENTRY PRICE")

    SIGS = [("size shock", "above normal"), ("burst", "frequency spike"),
            ("one-sided flow", "one-sided flow"), ("momentum", "Price moved"),
            ("closes <2h", "within 2 hours"), ("closes <24h", "within 24 hours"),
            ("closes <3d", "within 3 days"), ("longshot pen.", "Longshot penalty")]
    print("\n{:<26} {:>4} {:>7} {:>18} {:>10} {:>16}".format(
        "BY SIGNAL PRESENT", "n", "win%", "win 95% CI", "ROI", "ROI 95% CI"))
    print("-" * 86)
    for label, pat in SIGS:
        rs = [r for r in rows if re.search(pat, r["reasons"], re.I)]
        if len(rs) < 5:
            continue
        n = len(rs)
        ww = sum(1 for r in rs if r["win"])
        _, cl, ch = wilson(ww, n)
        mm, l2, h2 = boot([r["roi"] for r in rs])
        flag = "  <== PROFITABLE" if l2 > 0 else ("  <== LOSING" if h2 < 0 else "")
        print("{:<26} {:>4} {:>6.1%} [{:>5.2f},{:>5.2f}] {:>+9.1%} [{:>+6.1%},{:>+6.1%}]{}".format(
            label, n, ww / n, cl, ch, mm, l2, h2, flag))

    table(rows, lambda r: r["series"], "BY SERIES", min_n=5)

    # crypto vs everything else - the dominant theme in the stream
    def theme(r):
        s = r["series"].upper()
        if any(x in s for x in ("BTC", "ETH", "SOL", "XRP", "CRYPTO")):
            return "crypto price"
        if "FED" in s or "CPI" in s or "GDP" in s:
            return "macro"
        return "other"
    table(rows, theme, "BY THEME", min_n=5)

    # day-by-day equity trend
    print("\nDAILY")
    print("-" * 60)
    byday = defaultdict(list)
    for r in rows:
        byday[r["ts"][:10]].append(r)
    cum = 0.0
    for d in sorted(byday):
        rs = byday[d]
        ww = sum(1 for r in rs if r["win"])
        avg = sum(r["roi"] for r in rs) / len(rs)
        cum += sum(r["roi"] for r in rs)
        print("  {}  n={:3}  {}W/{}L  avgROI={:+7.1%}  cumROI={:+7.1%}".format(
            d, len(rs), ww, len(rs) - ww, avg, cum))


if __name__ == "__main__":
    main()
