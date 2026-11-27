"""
1-year backtest, step 3+4: replay the live alert logic over the real tape.

  candidates  For every market, feed its trades in time order through the SAME
              Baselines class the live monitor uses (imported, not copied), and
              record every trade that scores >= CAND_MIN with the side, entry
              price and hours-to-close it would have had at that moment.
  simulate    Walk all candidates across all markets in time order and apply the
              live gates: market cooldown, event cooldown, side-flip, daily cap,
              and rules.reject_reason (crypto, 25-88c band, 30-day horizon, one
              position per event). An earlier alert counts as settled only if its
              market had settled by then, so "already in this event" behaves as
              it did live.
  account     Event-driven $1,000 account: open at the alert, pay the Kalshi fee,
              lock the stake until the market's real settlement time, 5% of
              equity per bet, 15% per-event cap.

Known differences from live, all stated in the report:
  - live polls the latest 200 trades every 30s, so on busy days it misses
    trades and its baselines are thinner than a full-tape replay;
  - entry is the trade's own price (no spread, no slippage) - same as live;
  - close date uses max(close_time, expected_expiration) so a market that
    settled early cannot leak its outcome through the 30-day rule.

    python bt1y_replay.py candidates
    python bt1y_replay.py simulate [--threshold 65] [--from 2025-11-26] [--to 2026-11-26]
"""
import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rules  # noqa: E402
import monitor_kalshi as M  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.getenv("BT1Y_DB") or os.path.join(ROOT, "data", "bt1y.db")
CAND_MIN = 50

CAND_SCHEMA = """
CREATE TABLE IF NOT EXISTS cands (ticker TEXT, ts REAL, score REAL, hits INTEGER,
  side TEXT, entry_c INTEGER, price REAL, size REAL, htc REAL, eff_close TEXT, reasons TEXT);
CREATE INDEX IF NOT EXISTS idx_cands_ts ON cands(ts);
CREATE TABLE IF NOT EXISTS cand_done (ticker TEXT PRIMARY KEY);
"""


class FastScorer:
    """Same arithmetic as monitor_kalshi.Baselines.score for ONE market, with
    sorted/indexed windows instead of re-sorting 200 sizes on every trade.
    `verify` checks it against the live class trade by trade."""

    def __init__(self):
        from bisect import insort, bisect_left
        self._insort, self._bl = insort, bisect_left
        self.sizes, self.sorted_sizes = [], []
        self.times, self.prices, self.flow = [], [], []
        self.flow_sum = 0

    def add_and_score(self, size, ts, price, taker_yes):
        n0 = len(self.sizes)
        self.sizes.append(size); self._insort(self.sorted_sizes, size)
        if n0 + 1 > 200:
            ev = self.sizes[n0 - 200]
            del self.sorted_sizes[self._bl(self.sorted_sizes, ev)]
        self.times.append(ts); self.prices.append(price)
        f = 1 if taker_yes else -1
        self.flow.append(f); self.flow_sum += f
        if len(self.flow) > 100:
            self.flow_sum -= self.flow[-101]
        n = n0 + 1
        s = 0; hits = 0
        side = "YES" if self.flow_sum > 0 else "NO"

        ss = self.sorted_sizes
        k = len(ss)
        if k >= M.BASELINE_MIN_TRADES:
            med = ss[k // 2] if k % 2 else (ss[k // 2 - 1] + ss[k // 2]) / 2
            if size > med:
                import statistics
                mad = statistics.median([abs(x - med) for x in ss]) or 1.0
                z = (size - med) / (1.4826 * mad + 1e-9)
                if z >= 8: s += 35; hits += 1
                elif z >= 5: s += 24; hits += 1
                elif z >= 3: s += 12; hits += 1

        tw = min(n, 400)
        if tw >= M.BASELINE_MIN_TRADES:
            lo = n - tw
            now = ts
            l1 = n - self._bl(self.times, now - 60, lo, n)
            l60 = max(1, n - self._bl(self.times, now - 3600, lo, n))
            rate = l1 / max(l60 / 60.0, 0.5)
            if rate >= 10: s += 22; hits += 1
            elif rate >= 5: s += 14; hits += 1

        pw = min(n, 200)
        if pw >= 10:
            delta = (price - self.prices[n - pw]) * 100
            if abs(delta) >= 15: s += 20; hits += 1
            elif abs(delta) >= 8: s += 12; hits += 1

        cents = price * 100
        if cents <= 10 or cents >= 90: s -= 12
        elif cents <= 20 or cents >= 80: s -= 4
        if side == "YES":
            s -= 8
        raw = min(s, 100)
        return (min(raw, 45) if hits < 2 else raw), hits, side


def verify(limit=40):
    con = sqlite3.connect(DB, timeout=300)
    tks = [r[0] for r in con.execute("SELECT ticker FROM markets WHERE tape_done=1 AND n_trades>0 "
                                     "ORDER BY RANDOM() LIMIT ?", (limit,))]
    bad = tot = 0
    for tk in tks:
        b, f = M.Baselines(), FastScorer()
        for ts, price, size, ty in con.execute(
                "SELECT ts, price, size, taker_yes FROM trades WHERE ticker=? ORDER BY ts", (tk,)):
            if size <= 0 or not (0 < price < 1):
                continue
            b.add(tk, size, ts, price, bool(ty))
            s1, _, h1 = b.score(tk, size, price, None)
            sd1 = "YES" if sum(b.flow[tk]) > 0 else "NO"
            s2, h2, sd2 = f.add_and_score(size, ts, price, bool(ty))
            tot += 1
            if (s1, h1, sd1) != (s2, h2, sd2):
                bad += 1
                if bad <= 5:
                    print("MISMATCH", tk, ts, (s1, h1, sd1), (s2, h2, sd2))
    print("verified {} trades over {} markets, mismatches {}".format(tot, len(tks), bad))


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


_FRAC = __import__("re").compile(r"\.(\d+)")


def pts(s):
    """rules.parse_ts, but tolerant of Kalshi's variable-length fractional
    seconds (older Pythons only accept exactly 3 or 6 digits)."""
    if not s:
        return None
    s = _FRAC.sub(lambda m: "." + (m.group(1) + "000000")[:6], str(s).replace("Z", "+00:00"), 1)
    return rules.parse_ts(s)


def effective_close(mk):
    em = mk["category"] == "Mentions" and rules.is_earnings_mention(mk["ticker"], mk["title"])
    if em:
        e = M.earliest_date(mk["occurrence"], M.ticker_date(mk["event_ticker"]))
        if e:
            return e, True
    cands = [x for x in (mk["close_time"], mk["expected_exp"]) if pts(x)]
    if not cands:
        return None, em
    return max(cands, key=lambda x: pts(x)).replace("Z", "+00:00"), em


def candidates():
    con = sqlite3.connect(DB, timeout=300)
    con.row_factory = sqlite3.Row
    con.executescript(CAND_SCHEMA)
    todo = con.execute("SELECT * FROM markets WHERE tape_done=1 AND n_trades>=? AND ticker NOT IN "
                       "(SELECT ticker FROM cand_done) ORDER BY ticker", (M.BASELINE_MIN_TRADES,)).fetchall()
    print("markets to replay:", len(todo), flush=True)
    for i, mk in enumerate(todo):
        tk, title = mk["ticker"], mk["title"] or ""
        out = []
        skip = (M.is_direction(tk, title) or M.is_range_ladder(tk, title) or rules.is_crypto(tk, title)
                or M.is_junk(tk) or M.is_sports(tk) or M.is_weather(tk) or M.is_trump(tk))
        if not skip:
            eff, em = effective_close(mk)
            ct = pts(eff).timestamp() if eff else None
            fs = FastScorer()
            for ts, price, size, ty in con.execute(
                    "SELECT ts, price, size, taker_yes FROM trades WHERE ticker=? ORDER BY ts", (tk,)):
                if size <= 0 or not (0 < price < 1):
                    continue
                score, hits, side = fs.add_and_score(size, ts, price, bool(ty))
                htc = (ct - ts) / 3600.0 if ct else None
                if htc is not None and htc < 0:
                    continue
                if score < CAND_MIN:
                    continue
                entry = round(price * 100) if side == "YES" else round((1 - price) * 100)
                out.append((tk, ts, score, hits, side, entry, price, size, htc, eff, ""))
        con.executemany("INSERT INTO cands VALUES (?,?,?,?,?,?,?,?,?,?,?)", out)
        con.execute("INSERT OR IGNORE INTO cand_done VALUES (?)", (tk,))
        if i % 200 == 0:
            con.commit()
            print("{} {}/{}".format(datetime.now().strftime("%H:%M:%S"), i, len(todo)), flush=True)
    con.commit()
    print("candidates done:", con.execute("SELECT COUNT(*) FROM cands").fetchone()[0], flush=True)


def fee_cents(contracts, price_c):
    p = price_c / 100.0
    return math.ceil(0.07 * contracts * p * (1 - p) * 100)


def simulate(threshold, d_from, d_to, yes_ok=True, daily_cap=None, label=None, quiet=False):
    daily_cap = M.DAILY_CAP if daily_cap is None else daily_cap
    con = sqlite3.connect(DB, timeout=300)
    con.row_factory = sqlite3.Row
    mk = {r["ticker"]: dict(r) for r in con.execute(
        "SELECT * FROM markets WHERE ticker IN (SELECT DISTINCT ticker FROM cands WHERE score>=?)",
        (threshold,))}
    lo = pts(d_from + "T00:00:00+00:00").timestamp()
    hi = pts(d_to + "T00:00:00+00:00").timestamp()
    rows = con.execute("SELECT * FROM cands WHERE score>=? AND ts>=? AND ts<? ORDER BY ts",
                       (threshold, lo, hi)).fetchall()

    market_cd, event_cd, event_side = {}, {}, {}
    ev_alerts = defaultdict(list)   # dk -> [(ts, side, settle_ts, outcome)]
    sent = defaultdict(int)
    alerts = []
    skips = defaultdict(int)
    for c in rows:
        tk, now = c["ticker"], c["ts"]
        m = mk[tk]
        if now - market_cd.get(tk, 0) < M.MARKET_COOLDOWN:
            continue
        em = m["category"] == "Mentions" and rules.is_earnings_mention(tk, m["title"])
        dk = tk if em else (m["event_ticker"] or tk)
        if now - event_cd.get(dk, 0) < M.EVENT_COOLDOWN:
            market_cd[tk] = now; skips["event cooldown"] += 1
            continue
        prev = event_side.get(dk)
        if prev and prev != c["side"] and now - event_cd.get(dk, 0) < M.SIDE_FLIP_WINDOW:
            market_cd[tk] = now; skips["side flip"] += 1
            continue
        day = datetime.fromtimestamp(now, timezone.utc).date()
        if sent[day] >= daily_cap:
            skips["daily cap"] += 1
            continue
        prior = [(iso(t), s, (o if st <= now else None)) for t, s, st, o in ev_alerts[dk]]
        ec = pts(c["eff_close"])
        why = rules.reject_reason(tk, m["title"], c["side"], c["entry_c"],
                                  ec.isoformat() if ec else None, iso(now), prior)
        if not why and not yes_ok and c["side"] == "YES":
            why = "YES blocked (variant)"
        if why:
            market_cd[tk] = now
            skips[why.split(" ")[0] if why.startswith("entry") else why] += 1
            continue
        won = (c["side"] == "YES") == (m["result"] == "yes")
        outcome = "WIN" if won else "LOSS"
        settle = pts(m["settlement_ts"] or m["close_time"]).timestamp()
        sent[day] += 1
        event_cd[dk] = now
        event_side[dk] = c["side"]
        market_cd[tk] = now
        ev_alerts[dk].append((now, c["side"], settle, outcome))
        alerts.append({"ts": now, "settle": max(settle, now), "ticker": tk, "dk": dk,
                       "series": m["series"], "category": m["category"], "em": em,
                       "title": m["title"], "side": c["side"], "entry": c["entry_c"],
                       "score": c["score"], "outcome": outcome, "htc": c["htc"]})

    acct = account(alerts)
    res = summarize(alerts, acct, threshold, d_from, d_to, skips, label)
    if not quiet:
        print(json.dumps(res["headline"], indent=1))
    return res, alerts


def account(alerts, start=100000, pct=0.05, ev_cap=0.15):
    cash = start
    open_pos = []   # (settle, stake, payout, fee, dk, idx)
    ev_exp = defaultdict(int)
    peak, max_dd, curve, taken = start, 0.0, [], []

    def settle_until(t):
        nonlocal cash, peak, max_dd
        open_pos.sort(key=lambda p: p[0])
        while open_pos and open_pos[0][0] <= t:
            st, stake, payout, fee, dk, idx = open_pos.pop(0)
            cash += payout
            ev_exp[dk] -= stake
            eq = cash + sum(p[1] for p in open_pos)
            peak = max(peak, eq)
            max_dd = max(max_dd, (peak - eq) / peak)
            curve.append((st, eq))

    for i, a in enumerate(alerts):
        settle_until(a["ts"])
        equity = cash + sum(p[1] for p in open_pos)
        stake = max(2000, round(pct * equity))
        if ev_exp[a["dk"]] + stake > ev_cap * equity:
            continue
        stake = min(stake, max(0, cash - 100))
        if stake < 1000:
            continue
        contracts = stake / a["entry"]
        fee = fee_cents(contracts, a["entry"])
        cash -= stake + fee
        payout = round(contracts * 100) if a["outcome"] == "WIN" else 0
        ev_exp[a["dk"]] += stake
        open_pos.append((a["settle"], stake, payout, fee, a["dk"], i))
        taken.append((i, stake, payout - stake - fee))
    settle_until(float("inf"))
    return {"end": cash, "start": start, "max_dd": max_dd, "curve": curve, "taken": taken}


def per_trade(alerts):
    """Fee-adjusted ROI of a flat $1 stake on each alert - independent of sizing."""
    out = []
    for a in alerts:
        p = a["entry"] / 100.0
        fee = 0.07 * (1 - p)          # per $1 staked
        r = (1 - p) / p - fee if a["outcome"] == "WIN" else -1 - fee
        out.append(r)
    return out


def boot_ci(xs, n=2000):
    import random
    if len(xs) < 5:
        return (float("nan"), float("nan"))
    rnd = random.Random(7)
    ms = sorted(sum(rnd.choice(xs) for _ in xs) / len(xs) for _ in range(n))
    return ms[int(0.025 * n)], ms[int(0.975 * n)]


def group(alerts, key):
    g = defaultdict(list)
    for a, r in zip(alerts, per_trade(alerts)):
        g[key(a)].append((a, r))
    out = []
    for k, v in g.items():
        rs = [r for _, r in v]
        w = sum(1 for a, _ in v if a["outcome"] == "WIN")
        out.append({"key": k, "n": len(v), "win": w / len(v),
                    "implied": sum(a["entry"] for a, _ in v) / len(v) / 100,
                    "roi": sum(rs) / len(rs)})
    return sorted(out, key=lambda x: -x["n"])


def summarize(alerts, acct, threshold, d_from, d_to, skips, label):
    rs = per_trade(alerts)
    n = len(alerts)
    w = sum(1 for a in alerts if a["outcome"] == "WIN")
    lo, hi = boot_ci(rs)
    head = {"label": label or "threshold {}".format(threshold), "window": [d_from, d_to],
            "alerts": n, "wins": w, "win_rate": round(w / n, 3) if n else None,
            "implied_win": round(sum(a["entry"] for a in alerts) / n / 100, 3) if n else None,
            "roi_per_trade": round(sum(rs) / n, 4) if n else None,
            "roi_ci95": [round(lo, 4), round(hi, 4)],
            "account_end": round(acct["end"] / 100, 2),
            "account_return": round(acct["end"] / acct["start"] - 1, 4),
            "max_drawdown": round(acct["max_dd"], 3),
            "bets_funded": len(acct["taken"])}
    return {"headline": head, "skips": dict(skips),
            "by_month": group(alerts, lambda a: datetime.fromtimestamp(a["ts"], timezone.utc).strftime("%Y-%m")),
            "by_category": group(alerts, lambda a: ("Earnings mentions" if a["em"] else a["category"])),
            "by_side": group(alerts, lambda a: a["side"]),
            "by_score": group(alerts, lambda a: "{}-{}".format(int(a["score"]) // 5 * 5, int(a["score"]) // 5 * 5 + 4)),
            "by_entry": group(alerts, lambda a: "{}-{}c".format(a["entry"] // 10 * 10, a["entry"] // 10 * 10 + 9)),
            "by_series": group(alerts, lambda a: a["series"])[:25],
            "curve": acct["curve"]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["candidates", "simulate", "verify"])
    ap.add_argument("--threshold", type=float, default=M.ALERT_THRESHOLD)
    ap.add_argument("--from", dest="d_from", default="2025-11-26")
    ap.add_argument("--to", dest="d_to", default="2026-11-26")
    a = ap.parse_args()
    if a.step == "verify":
        verify()
    elif a.step == "candidates":
        candidates()
    else:
        simulate(a.threshold, a.d_from, a.d_to)
