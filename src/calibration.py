"""
Calibration analysis: are Kalshi markets priced correctly?

For markets whose final price was P, how often did they actually resolve YES?
If markets at 0.10 resolve YES 15% of the time, buying YES at 0.10 has edge.

Everything is reported with Wilson 95% confidence intervals and net-of-fee edge,
because raw win rates on small samples are meaningless.
"""
import argparse, math, os, sqlite3, sys

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "kalshi.db")

# Auto-generated combinatorial markets — thousands of near-duplicate legs.
JUNK_SERIES = ("KXMVESPORTSMULTIGAMEEXTENDED", "KXMVECROSSCATEGORY", "KXMVE")


def wilson(k: int, n: int, z: float = 1.96):
    """Wilson score interval — correct for small n, unlike normal approximation."""
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (p, max(0.0, centre - half), min(1.0, centre + half))


def kalshi_fee(price: float, contracts: float = 1.0) -> float:
    """Kalshi trading fee per Kalshi's published formula: 0.07 * C * P * (1-P),
    rounded up to the next cent. Returns dollars."""
    raw = 0.07 * contracts * price * (1.0 - price)
    return math.ceil(raw * 100) / 100.0


def load(con, min_volume: float, exclude_junk: bool, series_like: str = None):
    q = ("SELECT ticker, series_ticker, title, result, last_price, volume, "
         "yes_bid, yes_ask, close_time FROM markets "
         "WHERE last_price IS NOT NULL AND result IN ('yes','no')")
    args = []
    if min_volume:
        q += " AND volume >= ?"; args.append(min_volume)
    if series_like:
        q += " AND series_ticker LIKE ?"; args.append(series_like)
    rows = con.execute(q, args).fetchall()
    if exclude_junk:
        rows = [r for r in rows if not any(r[1].startswith(j) for j in JUNK_SERIES)]
    return rows


def buckets(rows, width=0.10):
    out = {}
    for r in rows:
        p = r[4]
        if p is None or not (0.0 < p < 1.0):
            continue
        b = min(int(p / width), int(1 / width) - 1)
        out.setdefault(b, []).append(r)
    return out


def report_calibration(rows, width=0.10):
    print(f"\n{'price bucket':>14} {'n':>7} {'implied':>8} {'actual':>8} "
          f"{'95% CI':>16} {'edge':>8} {'net-fee':>8}")
    print("-" * 78)
    bk = buckets(rows, width)
    total_signal = []
    for b in sorted(bk):
        rs = bk[b]
        n = len(rs)
        k = sum(1 for r in rs if r[3] == "yes")
        lo_p, hi_p = b * width, (b + 1) * width
        mid = (lo_p + hi_p) / 2
        actual, cl, ch = wilson(k, n)
        edge = actual - mid                       # YES-side edge vs implied prob
        fee = kalshi_fee(mid)                     # per $1 contract
        net = edge - fee
        # Only flag when the CI excludes the implied price (statistically real)
        flag = ""
        if cl > hi_p:
            flag = "  <-- YES underpriced"
        elif ch < lo_p:
            flag = "  <-- NO underpriced"
        print(f"{lo_p:>5.2f}-{hi_p:<5.2f}  {n:>7} {mid:>8.3f} {actual:>8.3f} "
              f"[{cl:>5.3f},{ch:>5.3f}] {edge:>+8.3f} {net:>+8.3f}{flag}")
        total_signal.append((lo_p, hi_p, n, actual, cl, ch, net))
    return total_signal


def report_by_series(rows, min_n=30, top=25):
    from collections import defaultdict
    d = defaultdict(list)
    for r in rows:
        d[r[1]].append(r)
    print(f"\n{'series':<34} {'n':>6} {'yes%':>7} {'avg px':>7} {'brier':>7} {'calib err':>10}")
    print("-" * 78)
    out = []
    for s, rs in d.items():
        n = len(rs)
        if n < min_n:
            continue
        k = sum(1 for r in rs if r[3] == "yes")
        avg_px = sum(r[4] for r in rs) / n
        # Brier score: mean squared error of the price as a probability forecast
        brier = sum((r[4] - (1.0 if r[3] == "yes" else 0.0)) ** 2 for r in rs) / n
        calib_err = (k / n) - avg_px     # + means market underprices YES
        out.append((s, n, k / n, avg_px, brier, calib_err))
    for s, n, yr, ap, br, ce in sorted(out, key=lambda x: -abs(x[5]))[:top]:
        print(f"{s[:34]:<34} {n:>6} {yr:>7.3f} {ap:>7.3f} {br:>7.4f} {ce:>+10.3f}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-volume", type=float, default=0)
    ap.add_argument("--width", type=float, default=0.10)
    ap.add_argument("--include-junk", action="store_true")
    ap.add_argument("--series")
    a = ap.parse_args()

    con = sqlite3.connect(DB)
    rows = load(con, a.min_volume, not a.include_junk, a.series)
    print(f"Loaded {len(rows)} settled markets "
          f"(min_volume={a.min_volume}, junk_excluded={not a.include_junk})")
    if not rows:
        sys.exit("No rows match. Fetch more history first.")
    yes = sum(1 for r in rows if r[3] == "yes")
    print(f"Base rate: {yes}/{len(rows)} = {yes/len(rows):.1%} resolve YES")
    report_calibration(rows, a.width)
    report_by_series(rows)
