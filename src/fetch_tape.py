"""
Pull the real trade-by-trade tape for every settled alert, from shortly before
the alert until the market closed. This is the ground truth the maker backtest
fills against: an order only counts as filled when a REAL trade printed through
its price. No synthetic fills, no assumed liquidity.
"""
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kalshi_client import KalshiClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "kalshi_alerts.db")
PRE_ALERT_SEC = 300          # a little context before the alert

SCHEMA = """
CREATE TABLE IF NOT EXISTS tape (
  ticker TEXT, trade_id TEXT PRIMARY KEY, ts TEXT, yes_cents INTEGER,
  count REAL, taker_side TEXT
);
CREATE INDEX IF NOT EXISTS idx_tape_tk ON tape(ticker, ts);
CREATE TABLE IF NOT EXISTS mkt_meta (
  ticker TEXT PRIMARY KEY, open_time TEXT, close_time TEXT, result TEXT,
  tape_fetched_ts TEXT
);
"""


def iso_to_unix(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def fetch_market(cli, tk):
    m = cli.get("/trade-api/v2/markets/" + tk).get("market", {})
    return m.get("open_time"), m.get("close_time"), m.get("result")


def fetch_trades(cli, tk, min_ts):
    out, cursor = [], None
    while True:
        params = {"ticker": tk, "limit": 1000, "min_ts": min_ts}
        if cursor:
            params["cursor"] = cursor
        r = cli.get("/trade-api/v2/markets/trades", params=params)
        for t in r.get("trades", []):
            out.append((tk, t["trade_id"], t["created_time"],
                        int(round(float(t["yes_price_dollars"]) * 100)),
                        float(t.get("count_fp") or t.get("count") or 0),
                        t.get("taker_side")))
        cursor = r.get("cursor")
        if not cursor:
            break
        time.sleep(0.08)
    return out


def main():
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    cli = KalshiClient()

    todo = con.execute(
        "SELECT ticker, MIN(ts) FROM alerts WHERE result IN ('WIN','LOSS') "
        "AND ticker NOT IN (SELECT ticker FROM mkt_meta WHERE tape_fetched_ts IS NOT NULL) "
        "GROUP BY ticker").fetchall()
    print("fetching tape for {} markets...".format(len(todo)))
    n_tr = 0
    for i, (tk, first_ts) in enumerate(todo, 1):
        try:
            ot, ct, res = fetch_market(cli, tk)
            min_ts = iso_to_unix(first_ts) - PRE_ALERT_SEC
            trades = fetch_trades(cli, tk, min_ts)
            con.executemany("INSERT OR IGNORE INTO tape VALUES (?,?,?,?,?,?)", trades)
            con.execute("INSERT OR REPLACE INTO mkt_meta VALUES (?,?,?,?,?)",
                        (tk, ot, ct, res, datetime.now(timezone.utc).isoformat()))
            n_tr += len(trades)
        except Exception as e:
            print("  {} ERR {}".format(tk, e))
        if i % 25 == 0:
            con.commit()
            print("  {}/{}  ({} trades so far)".format(i, len(todo), n_tr))
        time.sleep(0.08)
    con.commit()
    tot = con.execute("SELECT COUNT(*) FROM tape").fetchone()[0]
    print("done. {} trades in tape table.".format(tot))


if __name__ == "__main__":
    main()
