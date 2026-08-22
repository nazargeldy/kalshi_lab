"""
Honest calibration + strategy backtest using PRE-RESOLUTION snapshots.

Unlike calibration.py (which uses last_price and is contaminated by lookahead
bias), this evaluates: "if I had bought at the ASK, H hours before close, what
would have actually happened?" Execution is modelled realistically:
  buy YES -> pay yes_ask;  buy NO -> pay (1 - yes_bid);  plus Kalshi fees.
"""
import argparse, math, os, sqlite3, sys
from collections import defaultdict

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "kalshi.db")


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (p, max(0.0, c - h), min(1.0, c + h))


def fee(price, contracts=1.0):
    return math.ceil(0.07 * contracts * price * (1 - price) * 100) / 100.0


def load(con, horizon, min_vol, exclude_junk=True):
    q = """SELECT m.ticker, m.series_ticker, m.result, m.volume,
                  s.price, s.yes_bid, s.yes_ask, sm.category
           FROM snapshots s
           JOIN markets m ON m.ticker = s.ticker
           LEFT JOIN series_meta sm ON sm.ticker = m.series_ticker
           WHERE s.horizon_h = ? AND m.result IN ('yes','no')
             AND s.yes_ask IS NOT NULL AND s.yes_bid IS NOT NULL
             AND m.volume >= ?"""
    rows = con.execute(q, (horizon, min_vol)).fetchall()
    if exclude_junk:
        rows = [r for r in rows if not (r[1] or "").startswith("KXMVE")]
    return rows


def pnl_buy_yes(r):
    """Buy YES at ask. Returns (cost, payout, net)."""
    ask = r[6]
    if not ask or ask <= 0 or ask >= 1:
        return None
    cost = ask + fee(ask)
    payout = 1.0 if r[2] == "yes" else 0.0
    return (cost, payout, payout - cost)


def pnl_buy_no(r):
    """Buy NO — i.e. pay (1 - yes_bid)."""
    bid = r[5]
    if bid is None or bid <= 0 or bid >= 1:
        return None
    price = 1.0 - bid
    cost = price + fee(price)
    payout = 1.0 if r[2] == "no" else 0.0
    return (cost, payout, payout - cost)


def report(rows, horizon, width=0.10):
    print(f"\n{'='*86}\nHORIZON: {horizon}h before close   |   n={len(rows)} markets")
    print("=" * 86)
    if not rows:
        print("no data"); return
    k = sum(1 for r in rows if r[2] == "yes")
    print(f"Base rate: {k}/{len(rows)} = {k/len(rows):.1%} resolve YES\n")

    print(f"{'ask bucket':>13} {'n':>6} {'implied':>8} {'actual':>8} {'95% CI':>16} "
          f"{'buyYES ROI':>11} {'buyNO ROI':>10}")
    print("-" * 86)
    bk = defaultdict(list)
    for r in rows:
        a = r[6]
        if a and 0 < a < 1:
            bk[min(int(a / width), int(1/width) - 1)].append(r)
    for b in sorted(bk):
        rs = bk[b]
        n = len(rs)
        kk = sum(1 for r in rs if r[2] == "yes")
        lo, hi = b * width, (b + 1) * width
        act, cl, ch = wilson(kk, n)
        ys = [pnl_buy_yes(r) for r in rs]; ys = [x for x in ys if x]
        ns = [pnl_buy_no(r) for r in rs];  ns = [x for x in ns if x]
        yroi = (sum(x[2] for x in ys) / sum(x[0] for x in ys)) if ys else 0
        nroi = (sum(x[2] for x in ns) / sum(x[0] for x in ns)) if ns else 0
        flag = ""
        if cl > hi: flag = " <-YES cheap"
        elif ch < lo: flag = " <-NO cheap"
        print(f"{lo:>5.2f}-{hi:<5.2f} {n:>6} {(lo+hi)/2:>8.3f} {act:>8.3f} "
              f"[{cl:>5.3f},{ch:>5.3f}] {yroi:>+10.1%} {nroi:>+9.1%}{flag}")

    # Category-level
    cat = defaultdict(list)
    for r in rows:
        cat[r[7] or "?"].append(r)
    print(f"\n{'category':<26} {'n':>6} {'yes%':>7} {'buyYES ROI':>11} {'buyNO ROI':>10} {'95% CI (yes%)':>18}")
    print("-" * 86)
    for cname, rs in sorted(cat.items(), key=lambda x: -len(x[1])):
        n = len(rs)
        if n < 20:
            continue
        kk = sum(1 for r in rs if r[2] == "yes")
        act, cl, ch = wilson(kk, n)
        ys = [x for x in (pnl_buy_yes(r) for r in rs) if x]
        ns = [x for x in (pnl_buy_no(r) for r in rs) if x]
        yroi = (sum(x[2] for x in ys) / sum(x[0] for x in ys)) if ys else 0
        nroi = (sum(x[2] for x in ns) / sum(x[0] for x in ns)) if ns else 0
        print(f"{cname[:26]:<26} {n:>6} {act:>7.3f} {yroi:>+10.1%} {nroi:>+9.1%}   [{cl:.3f},{ch:.3f}]")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=24)
    ap.add_argument("--min-volume", type=float, default=200)
    ap.add_argument("--all-horizons", action="store_true")
    a = ap.parse_args()
    con = sqlite3.connect(DB)
    hs = [24, 72, 168] if a.all_horizons else [a.horizon]
    for h in hs:
        report(load(con, h, a.min_volume), h)
