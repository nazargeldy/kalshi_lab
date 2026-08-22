"""Fetch settled Kalshi markets series-by-series (avoids the auto-generated junk
that swamps raw pagination). Resumable: skips series already done."""
import argparse, json, os, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kalshi_client import KalshiClient
from fetch_history import db, series_of, _f

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def ensure_cols(c):
    c.executescript("""
      CREATE TABLE IF NOT EXISTS series_meta (ticker TEXT PRIMARY KEY, category TEXT, title TEXT);
      CREATE TABLE IF NOT EXISTS series_done (ticker TEXT PRIMARY KEY, n INTEGER, ts TEXT);
    """)

def run(categories, max_series, pages_per_series, min_delay=0.08):
    c, cli = db(), KalshiClient()
    ensure_cols(c)
    series = json.load(open(os.path.join(ROOT, "data", "series.json")))
    c.executemany("INSERT OR REPLACE INTO series_meta VALUES (?,?,?)",
                  [(s["ticker"], s.get("category"), s.get("title")) for s in series])
    c.commit()

    done = {r[0] for r in c.execute("SELECT ticker FROM series_done").fetchall()}
    todo = [s for s in series
            if (not categories or s.get("category") in categories)
            and s["ticker"] not in done]
    todo = todo[:max_series]
    print(f"{len(todo)} series to fetch (categories={categories or 'ALL'})")

    tot = 0
    for i, s in enumerate(todo, 1):
        tk = s["ticker"]
        cur, got = None, 0
        for _ in range(pages_per_series):
            params = {"limit": 200, "series_ticker": tk}
            if cur:
                params["cursor"] = cur
            try:
                d = cli.get("/trade-api/v2/historical/markets", params)
            except Exception:
                time.sleep(1.0); break
            mk = d.get("markets", [])
            if not mk:
                break
            rows = [(m.get("ticker"), m.get("event_ticker"), series_of(m.get("ticker")),
                     m.get("title"), m.get("result"), m.get("status"),
                     m.get("market_type"), m.get("strike_type"),
                     _f(m, "last_price_dollars"), _f(m, "yes_bid_dollars"),
                     _f(m, "yes_ask_dollars"), _f(m, "settlement_value_dollars"),
                     _f(m, "volume_fp"), _f(m, "open_interest_fp"), _f(m, "liquidity_dollars"),
                     m.get("open_time"), m.get("close_time"), m.get("settlement_ts"))
                    for m in mk]
            c.executemany("INSERT OR REPLACE INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            got += len(rows)
            cur = d.get("cursor")
            if not cur:
                break
            time.sleep(min_delay)
        c.execute("INSERT OR REPLACE INTO series_done VALUES (?,?,datetime('now'))", (tk, got))
        c.commit()
        tot += got
        if i % 25 == 0 or i == len(todo):
            n = c.execute("SELECT COUNT(*) FROM markets WHERE series_ticker NOT LIKE 'KXMVE%'").fetchone()[0]
            print(f"  [{i}/{len(todo)}] {tk:28} +{got:4}  running_total={tot}  db_nonjunk={n}")
        time.sleep(min_delay)
    print(f"DONE. added={tot}")
    c.close()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--categories", default="Mentions,Financials,Crypto,Economics,Companies,Commodities")
    ap.add_argument("--max-series", type=int, default=400)
    ap.add_argument("--pages", type=int, default=3)
    a = ap.parse_args()
    cats = [x.strip() for x in a.categories.split(",")] if a.categories.lower() != "all" else None
    run(cats, a.max_series, a.pages)
