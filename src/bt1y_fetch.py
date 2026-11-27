"""
1-year backtest, step 1+2: build the universe and download the tape.

  universe  every series in a category the live monitor does not block,
            minus the same ticker filters it applies (crypto, sports, weather,
            Trump, combos, direction markets); then every market in those
            series that settled YES/NO inside the window.
  tape      every trade in those markets, from both the live endpoint and the
            /historical/ archive (Kalshi moves data older than a rolling cutoff
            there).

Public endpoints only, no keys. Resumable: re-running skips finished work.

    python bt1y_fetch.py universe
    python bt1y_fetch.py tape [--min-volume 500]
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rules  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.getenv("BT1Y_DB") or os.path.join(ROOT, "data", "bt1y.db")
API = "https://api.elections.kalshi.com/trade-api/v2"

WINDOW_FROM = "2025-11-26T00:00:00+00:00"
WINDOW_TO = "2026-11-26T00:00:00+00:00"

# Mirrors monitor_kalshi.py. Mentions is allowed only for earnings-call words.
BLOCKED_CATEGORIES = {"Sports", "Entertainment", "Climate and Weather", "Crypto"}
JUNK_PREFIXES = ("KXMVE",)
WEATHER_PREFIXES = ("KXHIGH", "KXLOW", "KXTEMP", "KXRAIN", "KXSNOW", "KXWIND")
DIRECTION_HINTS = ("15M", "1H", "HOURLY", "UPDOWN")
SPORTS_HINTS = ("GAME", "NFL", "NBA", "MLB", "NHL", "SOCCER", "LIGUE", "LALIGA",
                "SERIEA", "EPL", "UFC", "TENNIS", "GOLF", "NCAA", "CFB", "MLS",
                "BUNDES", "CRICKET", "ESPORT", "VALORANT", "LOL", "CS2", "WC")

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (ticker TEXT PRIMARY KEY, category TEXT, title TEXT,
  markets_done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS markets (ticker TEXT PRIMARY KEY, series TEXT, event_ticker TEXT,
  title TEXT, category TEXT, result TEXT, open_time TEXT, close_time TEXT,
  expected_exp TEXT, occurrence TEXT, settlement_ts TEXT, volume REAL, yes_sub TEXT,
  no_sub TEXT, tape_done INTEGER DEFAULT 0, n_trades INTEGER);
CREATE TABLE IF NOT EXISTS trades (ticker TEXT, ts REAL, price REAL, size REAL,
  taker_yes INTEGER);
CREATE INDEX IF NOT EXISTS idx_trades_tk ON trades(ticker, ts);
"""

import threading
from concurrent.futures import ThreadPoolExecutor

_tls = threading.local()
_lock = threading.Lock()
_last = [0.0]
MIN_GAP = 0.075  # ~13 req/s shared across threads, under the public 20/s limit


def _session():
    if not hasattr(_tls, "s"):
        _tls.s = requests.Session()
    return _tls.s


def get(path, **params):
    for attempt in range(8):
        with _lock:
            gap = time.time() - _last[0]
            if gap < MIN_GAP:
                time.sleep(MIN_GAP - gap)
            _last[0] = time.time()
        try:
            r = _session().get(API + path, params=params, timeout=40)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(min(60, 2 ** attempt))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("gave up on " + path)


def ts_of(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def series_blocked(tk):
    s = tk.upper()
    return (s.startswith(JUNK_PREFIXES) or s.startswith(WEATHER_PREFIXES)
            or s.startswith(rules.CRYPTO_SERIES_PREFIXES) or "TRUMP" in s
            or any(h in s for h in DIRECTION_HINTS) or any(h in s for h in SPORTS_HINTS))


def con():
    c = sqlite3.connect(DB, timeout=300)
    c.executescript(SCHEMA)
    return c


def universe():
    c = con()
    if not c.execute("SELECT 1 FROM series LIMIT 1").fetchone():
        allser = get("/series").get("series", [])
        keep = []
        for s in allser:
            cat, tk = s.get("category"), s.get("ticker") or ""
            if cat in BLOCKED_CATEGORIES or series_blocked(tk):
                continue
            if cat == "Mentions" and not rules.is_earnings_mention(tk, s.get("title")):
                continue
            keep.append((tk, cat, s.get("title")))
        c.executemany("INSERT OR IGNORE INTO series(ticker,category,title) VALUES (?,?,?)", keep)
        c.commit()
        print("series kept {} of {}".format(len(keep), len(allser)), flush=True)

    lo, hi = int(ts_of(WINDOW_FROM)), int(ts_of(WINDOW_TO))
    todo = c.execute("SELECT ticker, category FROM series WHERE markets_done=0").fetchall()
    for i, (ser, cat) in enumerate(todo):
        rows = []
        for path, extra in (("/historical/markets", {}), ("/markets", {"status": "settled"})):
            cursor = None
            while True:
                q = dict(series_ticker=ser, limit=1000, min_close_ts=lo, max_close_ts=hi, **extra)
                if cursor:
                    q["cursor"] = cursor
                d = get(path, **q)
                for m in d.get("markets", []):
                    if m.get("result") not in ("yes", "no"):
                        continue
                    ct = m.get("close_time") or ""
                    if not (WINDOW_FROM[:10] <= ct[:10] <= WINDOW_TO[:10]):
                        continue
                    rows.append((m["ticker"], ser, m.get("event_ticker"), m.get("title"), cat,
                                 m["result"], m.get("open_time"), ct,
                                 m.get("expected_expiration_time"), m.get("occurrence_datetime"),
                                 m.get("settlement_ts"), float(m.get("volume_fp") or 0),
                                 m.get("yes_sub_title"), m.get("no_sub_title")))
                cursor = d.get("cursor")
                if not cursor:
                    break
        c.executemany("INSERT OR IGNORE INTO markets(ticker,series,event_ticker,title,category,"
                      "result,open_time,close_time,expected_exp,occurrence,settlement_ts,volume,"
                      "yes_sub,no_sub) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        c.execute("UPDATE series SET markets_done=1 WHERE ticker=?", (ser,))
        c.commit()
        if i % 200 == 0:
            n = c.execute("SELECT COUNT(*), SUM(volume) FROM markets").fetchone()
            print("{} {}/{} series, markets so far {} vol {:.0f}".format(
                datetime.now().strftime("%H:%M:%S"), i, len(todo), n[0], n[1] or 0), flush=True)
    print("universe done", c.execute("SELECT COUNT(*), SUM(volume) FROM markets").fetchone(), flush=True)


def tape(min_volume, limit=0):
    c = con()
    cutoff = get("/historical/cutoff").get("trades_created_ts")
    rows = c.execute("SELECT ticker, open_time, close_time, title FROM markets WHERE tape_done=0 "
                     "AND volume>=? ORDER BY volume", (min_volume,)).fetchall()
    # Same title filters the replay applies; no point downloading tape the
    # monitor would never have scored.
    tl = lambda t: (t or "").lower()
    todo = [(tk, ot, ct) for tk, ot, ct, title in rows
            if not (any(h in tl(title) for h in ("price range", "range on", "between",
                                                 "up in next", "up or down"))
                    or rules.is_crypto(tk, title))]
    if limit:
        todo = todo[-limit:]
    print("markets to fetch: {} (trade cutoff {})".format(len(todo), cutoff), flush=True)
    t0 = time.time()

    def fetch(job):
        tk, ot, ct = job
        out, seen, paths = [], set(), []
        if not ot or ot < cutoff:
            paths.append("/historical/trades")
        if not ct or ct >= cutoff:
            paths.append("/markets/trades")
        for path in paths:
            cursor = None
            while True:
                q = {"ticker": tk, "limit": 1000}
                if cursor:
                    q["cursor"] = cursor
                d = get(path, **q)
                for t in d.get("trades", []):
                    tid = t.get("trade_id")
                    if tid in seen:
                        continue
                    seen.add(tid)
                    try:
                        out.append((tk, ts_of(t["created_time"]), float(t["yes_price_dollars"]),
                                    float(t.get("count_fp") or 0), 1 if t.get("taker_side") == "yes" else 0))
                    except Exception:
                        pass
                cursor = d.get("cursor")
                if not cursor:
                    break
        return tk, out

    with ThreadPoolExecutor(max_workers=3) as ex:
        for i, (tk, out) in enumerate(ex.map(fetch, todo)):
            # Short transactions so the universe job writing the same file
            # never waits long on the lock.
            c.execute("DELETE FROM trades WHERE ticker=?", (tk,))
            c.commit()
            for j in range(0, len(out), 20000):
                c.executemany("INSERT INTO trades VALUES (?,?,?,?,?)", out[j:j + 20000])
                c.commit()
            c.execute("UPDATE markets SET tape_done=1, n_trades=? WHERE ticker=?", (len(out), tk))
            c.commit()
            if i % 250 == 0:
                print("{} {}/{} markets  last {} trades  {:.0f} min elapsed".format(
                    datetime.now().strftime("%H:%M:%S"), i, len(todo), len(out),
                    (time.time() - t0) / 60), flush=True)
    print("tape done", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["universe", "tape"])
    ap.add_argument("--min-volume", type=float, default=500)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    universe() if a.step == "universe" else tape(a.min_volume, a.limit)
