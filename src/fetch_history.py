"""Fetch Kalshi settled-market history into SQLite. Resumable, rate-limit aware."""
import argparse, os, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kalshi_client import KalshiClient

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "kalshi.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
  ticker TEXT PRIMARY KEY, event_ticker TEXT, series_ticker TEXT, title TEXT,
  result TEXT, status TEXT, market_type TEXT, strike_type TEXT,
  last_price REAL, yes_bid REAL, yes_ask REAL, settlement_value REAL,
  volume REAL, open_interest REAL, liquidity REAL,
  open_time TEXT, close_time TEXT, settlement_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_mkt_series ON markets(series_ticker);
CREATE INDEX IF NOT EXISTS idx_mkt_result ON markets(result);
CREATE TABLE IF NOT EXISTS events (
  event_ticker TEXT PRIMARY KEY, series_ticker TEXT, title TEXT, category TEXT
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

def _f(d, key):
    v = d.get(key)
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None

def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    return c

def series_of(ticker: str) -> str:
    # KXFOO-S2026ABC-XYZ  ->  KXFOO
    return (ticker or "").split("-")[0]

def fetch_markets(pages: int = 200, page_size: int = 200, resume: bool = True):
    c, cli = db(), KalshiClient()
    cur = None
    if resume:
        row = c.execute("SELECT v FROM meta WHERE k='markets_cursor'").fetchone()
        cur = row[0] if row else None
    total_new = 0
    for i in range(pages):
        params = {"limit": page_size}
        if cur:
            params["cursor"] = cur
        try:
            d = cli.get("/trade-api/v2/historical/markets", params)
        except Exception as e:
            print(f"  page {i}: error {e}; backing off"); time.sleep(3); continue
        mkts = d.get("markets", [])
        if not mkts:
            print("  no more markets."); break
        rows = []
        for m in mkts:
            tk = m.get("ticker")
            rows.append((
                tk, m.get("event_ticker"), series_of(tk), m.get("title"),
                m.get("result"), m.get("status"), m.get("market_type"), m.get("strike_type"),
                _f(m, "last_price_dollars"), _f(m, "yes_bid_dollars"), _f(m, "yes_ask_dollars"),
                _f(m, "settlement_value_dollars"),
                _f(m, "volume_fp"), _f(m, "open_interest_fp"), _f(m, "liquidity_dollars"),
                m.get("open_time"), m.get("close_time"), m.get("settlement_ts"),
            ))
        c.executemany("INSERT OR REPLACE INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        total_new += len(rows)
        cur = d.get("cursor")
        c.execute("INSERT OR REPLACE INTO meta VALUES ('markets_cursor',?)", (cur,))
        c.commit()
        if (i + 1) % 10 == 0 or i == 0:
            n = c.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
            print(f"  page {i+1}: +{len(rows)}  (db total {n})")
        if not cur:
            print("  reached end of history."); break
        time.sleep(0.12)
    n = c.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    print(f"Done. fetched_this_run={total_new}  db_total={n}")
    c.close()

def fetch_event_categories(limit_pages: int = 100):
    """Categories live on events, not markets — join them in."""
    c, cli = db(), KalshiClient()
    cur, seen = None, 0
    for i in range(limit_pages):
        params = {"limit": 200}
        if cur:
            params["cursor"] = cur
        try:
            d = cli.get("/trade-api/v2/events", params)
        except Exception as e:
            print(f"  events page {i}: {e}"); time.sleep(2); continue
        evs = d.get("events", [])
        if not evs:
            break
        c.executemany("INSERT OR REPLACE INTO events VALUES (?,?,?,?)",
                      [(e.get("event_ticker"), e.get("series_ticker"),
                        e.get("title"), e.get("category")) for e in evs])
        c.commit(); seen += len(evs)
        cur = d.get("cursor")
        if not cur:
            break
        time.sleep(0.12)
    print(f"events cached: {seen} (db {c.execute('SELECT COUNT(*) FROM events').fetchone()[0]})")
    c.close()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=200)
    ap.add_argument("--events", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="ignore saved cursor")
    a = ap.parse_args()
    if a.events:
        fetch_event_categories()
    else:
        fetch_markets(pages=a.pages, resume=not a.fresh)
