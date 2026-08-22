"""
Fetch pre-resolution price snapshots — the ONLY honest basis for backtesting.

For each settled market we record the bid/ask/price at fixed horizons BEFORE
close (24h, 72h, 168h). Using last_price instead would be lookahead bias:
a market trading at 2c right before settlement resolves NO ~always, which is
a tautology, not an edge.
"""
import argparse, datetime as dt, os, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kalshi_client import KalshiClient
from fetch_history import db

HORIZONS = [24, 72, 168]   # hours before close

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  ticker TEXT, horizon_h INTEGER, ts INTEGER,
  price REAL, yes_bid REAL, yes_ask REAL, volume REAL, open_interest REAL,
  PRIMARY KEY (ticker, horizon_h)
);
CREATE TABLE IF NOT EXISTS snap_done (ticker TEXT PRIMARY KEY, ok INTEGER);
"""

def _f(d, *path):
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    try:
        return float(cur) if cur not in (None, "") else None
    except (TypeError, ValueError):
        return None

def run(limit, min_volume, delay=0.10):
    c, cli = db(), KalshiClient()
    c.executescript(SCHEMA)
    rows = c.execute("""
        SELECT m.ticker, m.close_time FROM markets m
        LEFT JOIN snap_done d ON d.ticker = m.ticker
        WHERE d.ticker IS NULL
          AND m.series_ticker NOT LIKE 'KXMVE%'
          AND m.result IN ('yes','no')
          AND m.volume >= ?
          AND m.close_time IS NOT NULL
        ORDER BY m.volume DESC LIMIT ?""", (min_volume, limit)).fetchall()
    print(f"{len(rows)} markets need snapshots (min_volume={min_volume})")

    ok = fail = 0
    for i, (tk, ct) in enumerate(rows, 1):
        try:
            close = dt.datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except Exception:
            c.execute("INSERT OR REPLACE INTO snap_done VALUES (?,0)", (tk,)); continue
        end = int(close.timestamp())
        start = end - int(max(HORIZONS) * 3600) - 7200
        try:
            d = cli.get(f"/trade-api/v2/historical/markets/{tk}/candlesticks",
                        {"start_ts": start, "end_ts": end, "period_interval": 60})
            cs = d.get("candlesticks", [])
        except Exception:
            cs = []
        if not cs:
            c.execute("INSERT OR REPLACE INTO snap_done VALUES (?,0)", (tk,))
            fail += 1
        else:
            recs = []
            for h in HORIZONS:
                target = end - h * 3600
                # nearest candle at or before target
                cand = [x for x in cs if x.get("end_period_ts", 0) <= target]
                if not cand:
                    continue
                x = max(cand, key=lambda y: y["end_period_ts"])
                recs.append((tk, h, x["end_period_ts"],
                             _f(x, "price", "close"),
                             _f(x, "yes_bid", "close"), _f(x, "yes_ask", "close"),
                             _f(x, "volume"), _f(x, "open_interest")))
            if recs:
                c.executemany("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?)", recs)
                ok += 1
            c.execute("INSERT OR REPLACE INTO snap_done VALUES (?,1)", (tk,))
        if i % 50 == 0:
            c.commit()
            n = c.execute("SELECT COUNT(DISTINCT ticker) FROM snapshots").fetchone()[0]
            print(f"  [{i}/{len(rows)}] ok={ok} nodata={fail} distinct_snapped={n}")
        time.sleep(delay)
    c.commit()
    n = c.execute("SELECT COUNT(DISTINCT ticker) FROM snapshots").fetchone()[0]
    print(f"DONE ok={ok} nodata={fail} total_snapped={n}")
    c.close()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--min-volume", type=float, default=200)
    a = ap.parse_args()
    run(a.limit, a.min_volume)
