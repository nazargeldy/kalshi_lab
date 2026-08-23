"""
Resolve Kalshi alerts against real settled outcomes.

Unlike the Polymarket resolver (which read markets[0] of a multi-market event and
got 44% of outcomes wrong), Kalshi tickers are unique: one ticker == one contract.
We fetch that exact ticker and read its `result` field. No matching heuristics,
no guessing. If a market is not settled yet we leave it PENDING.
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kalshi_client import KalshiClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "kalshi_alerts.db")


def db():
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def resolve(verbose=True):
    con = db()
    cli = KalshiClient()
    rows = con.execute(
        "SELECT id, ticker, side FROM alerts WHERE result IS NULL OR result=''"
    ).fetchall()
    if verbose:
        print("{} unresolved alerts".format(len(rows)))

    wins = losses = pending = errors = 0
    for aid, ticker, side in rows:
        try:
            m = cli.get("/trade-api/v2/markets/" + ticker).get("market", {})
        except Exception as e:
            errors += 1
            if verbose:
                print("  err {}: {}".format(ticker, str(e)[:60]))
            time.sleep(0.2)
            continue

        result = (m.get("result") or "").lower()
        status = (m.get("status") or "").lower()

        # Only settled markets have a usable result.
        if result not in ("yes", "no") or status not in ("finalized", "settled", "closed"):
            pending += 1
            time.sleep(0.15)
            continue

        # We alerted a SIDE. Did that side win?
        won = (side == "YES" and result == "yes") or (side == "NO" and result == "no")
        outcome = "WIN" if won else "LOSS"
        con.execute("UPDATE alerts SET result=?, settled_ts=? WHERE id=?",
                    (outcome, datetime.now(timezone.utc).isoformat(), aid))
        con.commit()
        if won:
            wins += 1
        else:
            losses += 1
        if verbose:
            print("  {} {} side={} result={}".format(outcome, ticker[:40], side, result))
        time.sleep(0.15)

    if verbose:
        n = wins + losses
        print("resolved: {}W {}L  ({:.1%})  pending={} errors={}".format(
            wins, losses, (wins / n if n else 0), pending, errors))
    con.close()
    return wins, losses, pending


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    resolve(verbose=not a.quiet)
