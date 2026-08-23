"""
Three robustness checks on a candidate edge:
  1. Bootstrap CI on ROI itself (not just win rate) — is it distinguishable from 0?
  2. Time-split — does it hold in BOTH halves of the sample, or was it one lucky period?
  3. Liquidity — is the quoted bid actually deep enough to fill?
"""
import argparse, math, os, random, sqlite3, statistics, sys
from collections import defaultdict

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "kalshi.db")
random.seed(42)


def fee(p, c=1.0):
    return math.ceil(0.07 * c * p * (1 - p) * 100) / 100.0


def load(con, horizon, min_vol, category=None):
    q = """SELECT m.ticker, m.series_ticker, m.result, m.volume, m.close_time,
                  s.price, s.yes_bid, s.yes_ask, sm.category, m.open_interest
           FROM snapshots s
           JOIN markets m ON m.ticker = s.ticker
           LEFT JOIN series_meta sm ON sm.ticker = m.series_ticker
           WHERE s.horizon_h=? AND m.result IN ('yes','no')
             AND s.yes_bid IS NOT NULL AND s.yes_ask IS NOT NULL
             AND m.volume >= ?"""
    a = [horizon, min_vol]
    if category:
        q += " AND sm.category = ?"; a.append(category)
    rows = con.execute(q, a).fetchall()
    return [r for r in rows if not (r[1] or "").startswith("KXMVE")]


def trade_no(r):
    """Buy NO at (1 - yes_bid). Returns per-$1-staked profit, or None."""
    bid = r[6]
    if bid is None or not (0 < bid < 1):
        return None
    price = 1.0 - bid
    cost = price + fee(price)
    payout = 1.0 if r[2] == "no" else 0.0
    return (payout - cost) / cost          # ROI per dollar staked


def bootstrap_roi(returns, n_boot=5000):
    """Percentile bootstrap CI for mean ROI."""
    if not returns:
        return (0, 0, 0)
    n = len(returns)
    means = []
    for _ in range(n_boot):
        s = [returns[random.randrange(n)] for _ in range(n)]
        means.append(sum(s) / n)
    means.sort()
    return (sum(returns) / n, means[int(0.025 * n_boot)], means[int(0.975 * n_boot)])


def check(con, horizon, min_vol, category):
    rows = load(con, horizon, min_vol, category)
    rets = [(r, trade_no(r)) for r in rows]
    rets = [(r, x) for r, x in rets if x is not None]
    print(f"\n{'='*82}\nSTRATEGY: buy NO  |  category={category}  horizon={horizon}h  "
          f"min_vol={min_vol}\n{'='*82}")
    if len(rets) < 10:
        print(f"n={len(rets)} — too few to evaluate."); return
    vals = [x for _, x in rets]

    # ---- CHECK 1: bootstrap CI on ROI ----
    mean, lo, hi = bootstrap_roi(vals)
    verdict = "REAL (CI excludes 0)" if lo > 0 else ("NEGATIVE" if hi < 0 else "NOT DISTINGUISHABLE FROM 0")
    print(f"\n[1] BOOTSTRAP ROI   n={len(vals)}")
    print(f"    mean ROI      : {mean:+.2%}")
    print(f"    95% CI        : [{lo:+.2%}, {hi:+.2%}]")
    print(f"    median trade  : {statistics.median(vals):+.2%}")
    print(f"    verdict       : {verdict}")

    # ---- CHECK 2: time split ----
    dated = [(r[4] or "", v) for r, v in rets]
    dated.sort(key=lambda x: x[0])
    mid = len(dated) // 2
    h1 = [v for _, v in dated[:mid]]
    h2 = [v for _, v in dated[mid:]]
    m1, l1, u1 = bootstrap_roi(h1, 2000)
    m2, l2, u2 = bootstrap_roi(h2, 2000)
    print(f"\n[2] TIME SPLIT")
    print(f"    first half  ({dated[0][0][:10]} .. {dated[mid-1][0][:10]})  "
          f"n={len(h1):4}  ROI={m1:+.2%}  CI[{l1:+.2%},{u1:+.2%}]")
    print(f"    second half ({dated[mid][0][:10]} .. {dated[-1][0][:10]})  "
          f"n={len(h2):4}  ROI={m2:+.2%}  CI[{l2:+.2%},{u2:+.2%}]")
    consistent = (m1 > 0) == (m2 > 0)
    print(f"    verdict       : {'CONSISTENT sign' if consistent else 'INCONSISTENT — likely noise'}")

    # ---- CHECK 3: liquidity ----
    vols = sorted(r[3] or 0 for r, _ in rets)
    ois = sorted((r[9] or 0) for r, _ in rets)
    spreads = [(r[7] - r[6]) for r, _ in rets if r[7] is not None and r[6] is not None]
    print(f"\n[3] LIQUIDITY")
    print(f"    volume   median={vols[len(vols)//2]:,.0f}  p25={vols[len(vols)//4]:,.0f}  "
          f"p10={vols[len(vols)//10]:,.0f}")
    print(f"    open int median={ois[len(ois)//2]:,.0f}")
    if spreads:
        spreads.sort()
        print(f"    bid-ask spread median={spreads[len(spreads)//2]:.3f}  "
              f"p75={spreads[int(len(spreads)*0.75)]:.3f}  "
              f"p90={spreads[int(len(spreads)*0.90)]:.3f}")
        wide = sum(1 for s in spreads if s >= 0.10) / len(spreads)
        print(f"    fraction with spread >= 10c: {wide:.1%}")
    # ROI restricted to genuinely liquid names
    liq = [v for r, v in rets if (r[3] or 0) >= 5000]
    if len(liq) >= 20:
        ml, ll, ul = bootstrap_roi(liq, 3000)
        print(f"    ROI on volume>=5000 only: n={len(liq)} {ml:+.2%} CI[{ll:+.2%},{ul:+.2%}]")
    else:
        print(f"    ROI on volume>=5000 only: n={len(liq)} (too few)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default="Mentions")
    ap.add_argument("--horizon", type=int, default=24)
    ap.add_argument("--min-volume", type=float, default=200)
    a = ap.parse_args()
    con = sqlite3.connect(DB)
    check(con, a.horizon, a.min_volume, a.category)
