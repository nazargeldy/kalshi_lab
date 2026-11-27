"""Run the 1-year backtest variants and write data/bt1y_results.json."""
import json, os, sqlite3, sys
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bt1y_replay as R

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
F, T, MID = "2025-11-26", "2026-11-26", "2026-05-26"
out = {"generated": datetime.now(timezone.utc).isoformat(), "variants": {}}

def run(key, **kw):
    res, alerts = R.simulate(quiet=True, **kw)
    res["curve"] = res["curve"][::max(1, len(res["curve"]) // 400)]
    out["variants"][key] = res
    h = res["headline"]
    print("{:28s} n={:5d} win={} implied={} roi/trade={} ci={} acct={} dd={}".format(
        key, h["alerts"], h["win_rate"], h["implied_win"], h["roi_per_trade"], h["roi_ci95"],
        h["account_return"], h["max_drawdown"]), flush=True)
    return alerts

base = run("live_rules_thr65", threshold=65, d_from=F, d_to=T, label="Current live rules (score 65+)")
for thr in (60, 70, 75, 80):
    run("thr{}".format(thr), threshold=thr, d_from=F, d_to=T, label="Score {}+".format(thr))
run("thr65_first_half", threshold=65, d_from=F, d_to=MID, label="Score 65+, Nov-May")
run("thr65_second_half", threshold=65, d_from=MID, d_to=T, label="Score 65+, May-Nov")
run("thr65_no_only", threshold=65, d_from=F, d_to=T, yes_ok=False, label="Score 65+, NO bets only")
run("thr60_no_only", threshold=60, d_from=F, d_to=T, yes_ok=False, label="Score 60+, NO bets only")

# How close is the replay to what the live bot actually did? Compare on the
# window where both exist.
live = sqlite3.connect(os.path.join(ROOT, "data", "kalshi_alerts.db"))
la = live.execute("SELECT ts, ticker, side, score FROM alerts WHERE ts >= '2026-10-11'").fetchall()
_, bt = R.simulate(quiet=True, threshold=60, d_from="2026-10-11", d_to=T)
btk = {}
for a in bt:
    btk.setdefault(a["ticker"], []).append(a)
match = 0
for ts, tk, side, sc in la:
    t = R.pts(ts).timestamp()
    if any(abs(a["ts"] - t) < 3 * 3600 and a["side"] == side for a in btk.get(tk, [])):
        match += 1
out["live_overlap"] = {"live_alerts": len(la), "backtest_alerts_same_window": len(bt),
                       "live_reproduced": match}
print("overlap", out["live_overlap"])
json.dump(out, open(os.path.join(ROOT, "data", "bt1y_results.json"), "w"), default=str)
print("written")
