"""Fetch Kalshi's own category for every alerted market and analyse by category.

The Polymarket project banned 'geopolitical' outright. That was derived from a
non-US, crypto-native trader base. Kalshi's traders are US persons trading a
CFTC-regulated venue, so US politics/econ markets may behave completely
differently. This tests it instead of assuming.
"""
import os, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kalshi_client import KalshiClient
from analyze_alerts import boot, wilson, roi
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "kalshi_alerts.db")


def ensure_cat_table(con):
    con.execute("CREATE TABLE IF NOT EXISTS mkt_cat "
                "(ticker TEXT PRIMARY KEY, event_ticker TEXT, category TEXT)")
    con.commit()


def fetch_categories():
    con = sqlite3.connect(DB, timeout=30)
    ensure_cat_table(con)
    cli = KalshiClient()
    todo = con.execute(
        "SELECT DISTINCT a.ticker, a.event_ticker FROM alerts a "
        "LEFT JOIN mkt_cat m ON m.ticker=a.ticker "
        "WHERE m.ticker IS NULL AND a.result IN ('WIN','LOSS')").fetchall()
    print("fetching categories for {} markets...".format(len(todo)))
    ev_cache = {}
    for i, (tk, ev) in enumerate(todo, 1):
        cat = None
        try:
            if ev and ev in ev_cache:
                cat = ev_cache[ev]
            elif ev:
                cat = cli.get("/trade-api/v2/events/" + ev).get("event", {}).get("category")
                ev_cache[ev] = cat
        except Exception:
            cat = None
        con.execute("INSERT OR REPLACE INTO mkt_cat VALUES (?,?,?)", (tk, ev, cat))
        if i % 40 == 0:
            con.commit(); print("  {}/{}".format(i, len(todo)))
        time.sleep(0.08)
    con.commit()
    print("done.")
    return con


def report(con):
    rows = con.execute(
        "SELECT a.entry_cents, a.score, a.side, a.result, a.title, "
        "COALESCE(m.category,'?') FROM alerts a "
        "LEFT JOIN mkt_cat m ON m.ticker=a.ticker "
        "WHERE a.result IN ('WIN','LOSS') AND a.entry_cents BETWEEN 1 AND 99").fetchall()
    data = [{"entry": e, "score": s, "side": sd, "win": r == "WIN",
             "title": t, "cat": c, "roi": roi(e, r == "WIN")}
            for e, s, sd, r, t, c in rows]

    def show(g, name):
        print("\n{:<26} {:>4} {:>7} {:>18} {:>10} {:>17}".format(
            name, "n", "win%", "win 95% CI", "ROI", "ROI 95% CI"))
        print("-" * 88)
        for k, rs in sorted(g.items(), key=lambda x: -len(x[1])):
            n = len(rs)
            if n < 5:
                continue
            w = sum(1 for r in rs if r["win"])
            _, cl, ch = wilson(w, n)
            m, lo, hi = boot([r["roi"] for r in rs])
            flag = "  <== PROFITABLE" if lo > 0 else ("  <== LOSING" if hi < 0 else "")
            print("{:<26} {:>4} {:>6.1%} [{:>5.2f},{:>5.2f}] {:>+9.1%} [{:>+6.1%},{:>+6.1%}]{}".format(
                str(k)[:26], n, w / n, cl, ch, m, lo, hi, flag))

    g = defaultdict(list)
    for r in data:
        g[r["cat"]].append(r)
    show(g, "BY KALSHI CATEGORY")

    # category x side - does US politics behave differently by direction?
    g2 = defaultdict(list)
    for r in data:
        if r["cat"] in ("Politics", "Economics", "Elections", "World"):
            g2[r["cat"] + " / " + r["side"]].append(r)
    if g2:
        show(g2, "US POLICY MARKETS x SIDE")

    print("\nTotal settled with category: {}".format(len(data)))


if __name__ == "__main__":
    con = fetch_categories() if "--fetch" in sys.argv else sqlite3.connect(DB)
    ensure_cat_table(con)
    report(con)
