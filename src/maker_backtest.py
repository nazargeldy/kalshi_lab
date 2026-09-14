"""
Maker-side backtest on the live alert stream.

Same alerts, same side, same moment. The only change: instead of crossing the
spread (taker), we post a resting bid k cents below the alert price and wait up
to T hours. Then hold to settlement.

FILL RULE (this is where adverse selection lives, so it is deliberately harsh):
  strict  - filled only when a real trade later printed STRICTLY BELOW our bid.
            By price priority a bid at P cannot survive a print at P-1, so this
            is a certain fill. It ignores same-price prints entirely (we might
            have been behind in the queue).
  lenient - also counts prints AT our price. Upper bound; assumes front of queue.
Headline numbers use strict. The filled subset is by construction the subset
where price came DOWN to us - i.e. the adversely-selected one. That is the test.

FEES: maker = ceil(0.0175*C*P*(1-P)), the WORST case (multiplier M=1; the
default M on Kalshi is 0, meaning free). Taker = ceil(0.07*C*P*(1-P)).

VALIDATION: (k, T) is tuned on the FIRST half of alerts by time only, then
applied blind to the SECOND half. The second-half number is the answer.
"""
import bisect
import math
import os
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "kalshi_alerts.db")
random.seed(17)

LATENCY_SEC = 5                       # time to get an order onto the book
K_GRID = [0, 1, 2, 3, 4, 6, 8]        # cents below alert price
T_GRID = [0.5, 1, 2, 4, 12, 999]      # hours to leave the order (999 = to close)
MIN_FILLS_TO_TUNE = 30                # never pick a config on <30 fills


def fee_taker(p):
    return math.ceil(0.07 * (p / 100) * (1 - p / 100) * 100) / 100.0


def fee_maker(p):
    return math.ceil(0.0175 * (p / 100) * (1 - p / 100) * 100) / 100.0


def roi(p, won, maker):
    cost = p / 100.0 + (fee_maker(p) if maker else fee_taker(p))
    return ((1.0 if won else 0.0) - cost) / cost


def boot(vals, n=3000):
    if not vals:
        return (0, 0, 0)
    N = len(vals)
    ms = sorted(sum(vals[random.randrange(N)] for _ in range(N)) / N for _ in range(n))
    return (sum(vals) / N, ms[int(0.025 * n)], ms[int(0.975 * n)])


def pdt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load():
    con = sqlite3.connect(DB)
    alerts = con.execute(
        "SELECT a.id, a.ts, a.ticker, a.side, a.entry_cents, a.result, m.close_time "
        "FROM alerts a LEFT JOIN mkt_meta m ON m.ticker = a.ticker "
        "WHERE a.result IN ('WIN','LOSS') AND a.entry_cents BETWEEN 1 AND 99 "
        "ORDER BY a.ts").fetchall()
    tape = defaultdict(list)
    for tk, ts, yc in con.execute("SELECT ticker, ts, yes_cents FROM tape ORDER BY ts"):
        tape[tk].append((pdt(ts), yc))
    out = []
    for aid, ts, tk, side, ec, res, ct in alerts:
        t0 = pdt(ts)
        close = pdt(ct) if ct else None
        post = [(t, (yc if side == "YES" else 100 - yc))
                for t, yc in tape.get(tk, []) if t > t0 + timedelta(seconds=LATENCY_SEC)]
        # running minimum of our-side price: filled by deadline <=> runmin[idx] < limit
        times, runmin, m = [], [], 10 ** 9
        for t, myp in post:
            m = min(m, myp)
            times.append(t)
            runmin.append(m)
        out.append({"id": aid, "t0": t0, "tk": tk, "side": side, "entry": ec,
                    "win": res == "WIN", "close": close, "post": post,
                    "times": times, "runmin": runmin})
    return out


def simulate(a, k, T):
    """Return (strict_roi, lenient_roi); None where not filled."""
    limit = a["entry"] - k
    if limit < 1:
        return None, None
    deadline = a["t0"] + timedelta(hours=T)
    if a["close"] and a["close"] < deadline:
        deadline = a["close"]
    idx = bisect.bisect_right(a["times"], deadline) - 1
    if idx < 0:
        return None, None
    lowest = a["runmin"][idx]
    s = roi(limit, a["win"], True) if lowest < limit else None
    l = roi(limit, a["win"], True) if lowest <= limit else None
    return s, l


def evaluate(alerts, k, T):
    st, le = [], []
    for a in alerts:
        s, l = simulate(a, k, T)
        if s is not None:
            st.append((s, a["win"]))
        if l is not None:
            le.append((l, a["win"]))
    return st, le


def summary(pairs, n_alerts, ci=True):
    if not pairs:
        return dict(n=0, fill=0, win=0, roi=0, lo=0, hi=0, per_alert=0)
    v = [p[0] for p in pairs]
    m, lo, hi = boot(v) if ci else (sum(v) / len(v), 0, 0)
    return dict(n=len(pairs), fill=len(pairs) / n_alerts,
                win=sum(1 for p in pairs if p[1]) / len(pairs),
                roi=m, lo=lo, hi=hi, per_alert=sum(v) / n_alerts)


def fmt(s):
    flag = "  <== PROFITABLE" if s["lo"] > 0 else ("  <== LOSING" if s["hi"] < 0 else "")
    return "n={:>3} fill={:>5.1%} win={:>5.1%}  ROI {:>+7.1%} [{:>+6.1%},{:>+6.1%}]  per-alert {:>+6.1%}{}".format(
        s["n"], s["fill"], s["win"], s["roi"], s["lo"], s["hi"], s["per_alert"], flag)


def tlabel(T):
    return "to-close" if T == 999 else "{}h".format(T)


def main():
    A = load()
    with_tape = [a for a in A if a["post"]]
    print("=" * 96)
    print("MAKER vs TAKER on the live alert stream")
    print("=" * 96)
    print("settled alerts: {}   with post-alert tape: {}   ({} .. {})".format(
        len(A), len(with_tape), A[0]["t0"].date(), A[-1]["t0"].date()))
    mid = len(A) // 2
    H1, H2 = A[:mid], A[mid:]
    print("first half (tune) : {} alerts  {} .. {}".format(len(H1), H1[0]["t0"].date(), H1[-1]["t0"].date()))
    print("second half (blind): {} alerts  {} .. {}".format(len(H2), H2[0]["t0"].date(), H2[-1]["t0"].date()))

    print("\nTAKER baseline (what actually ran, at recorded alert price):")
    for name, H in (("first half ", H1), ("second half", H2), ("all        ", A)):
        v = [roi(a["entry"], a["win"], False) for a in H]
        m, lo, hi = boot(v)
        w = sum(1 for a in H if a["win"]) / len(H)
        print("  {}  n={:>3}  win={:>5.1%}  ROI {:>+7.1%} [{:>+6.1%},{:>+6.1%}]".format(name, len(H), w, m, lo, hi))

    print("\nFIRST HALF grid (strict fills)   rows = k cents below alert price, cols = T")
    print("  k\\T   " + "".join("{:>15}".format(tlabel(T)) for T in T_GRID))
    best, best_key = None, None
    for k in K_GRID:
        row = "  {:>3}   ".format(k)
        for T in T_GRID:
            st, _ = evaluate(H1, k, T)
            s = summary(st, len(H1), ci=False)
            row += "{:>+7.1%} (n{:>3}) ".format(s["roi"], s["n"])
            if s["n"] >= MIN_FILLS_TO_TUNE and (best is None or s["roi"] > best["roi"]):
                best, best_key = s, (k, T)
        print(row)

    if best is None:
        print("\nNo config reached {} fills in the first half. Cannot tune honestly.".format(MIN_FILLS_TO_TUNE))
        return
    k, T = best_key
    best = summary(evaluate(H1, k, T)[0], len(H1))
    print("\nchosen on first half only: k={} T={}".format(k, tlabel(T)))
    print("  first-half: " + fmt(best))

    print("\n" + "=" * 96)
    print("BLIND SECOND HALF  (k={}, T={}, chosen without looking at this data)".format(k, tlabel(T)))
    print("=" * 96)
    st, le = evaluate(H2, k, T)
    print("  strict fills : " + fmt(summary(st, len(H2))))
    print("  lenient fills: " + fmt(summary(le, len(H2))))
    v = [roi(a["entry"], a["win"], False) for a in H2]
    m, lo, hi = boot(v)
    print("  taker same   : n={:>3} fill=100.0% win={:>5.1%}  ROI {:>+7.1%} [{:>+6.1%},{:>+6.1%}]".format(
        len(H2), sum(1 for a in H2 if a["win"]) / len(H2), m, lo, hi))

    print("\nSame config, whole sample (half of it is in-sample, so treat with care):")
    st, le = evaluate(A, k, T)
    print("  strict : " + fmt(summary(st, len(A))))
    print("  lenient: " + fmt(summary(le, len(A))))

    print("\nADVERSE SELECTION CHECK (whole sample, strict, k={} T={}):".format(k, tlabel(T)))
    filled_w, unfilled_w = [], []
    for a in A:
        s, _ = simulate(a, k, T)
        (filled_w if s is not None else unfilled_w).append(a["win"])
    if filled_w and unfilled_w:
        print("  win rate when our bid WAS hit  : {:5.1%}  (n={})".format(sum(filled_w) / len(filled_w), len(filled_w)))
        print("  win rate when it was NOT hit   : {:5.1%}  (n={})".format(sum(unfilled_w) / len(unfilled_w), len(unfilled_w)))
        print("  (a big gap = the market fills us precisely when we are wrong)")


if __name__ == "__main__":
    main()
