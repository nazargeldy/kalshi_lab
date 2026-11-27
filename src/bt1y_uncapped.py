import sys, json; sys.path.insert(0, "src")
import bt1y_replay as R
from collections import defaultdict
F, MID, T = "2025-11-26", "2026-05-26", "2026-11-26"
def stats(al):
    rs = R.per_trade(al); n = len(al)
    if not n: return "n=0"
    w = sum(a["outcome"] == "WIN" for a in al); lo, hi = R.boot_ci(rs)
    return "n={:6d} win={:.3f} impl={:.3f} roi={:+.4f} ci=[{:+.4f},{:+.4f}]".format(
        n, w / n, sum(a["entry"] for a in al) / n / 100, sum(rs) / n, lo, hi)
out = {}
for thr in (60, 65, 70, 75):
    _, al = R.simulate(thr, F, T, daily_cap=10**9, quiet=True)
    out[thr] = al
    print("thr", thr, "ALL ", stats(al))
    for side in ("YES", "NO"):
        s = [a for a in al if a["side"] == side]
        print("        ", side, " ", stats(s))
        for half, (a0, a1) in (("H1", (F, MID)), ("H2", (MID, T))):
            lo = R.pts(a0 + "T00:00:00+00:00").timestamp(); hi = R.pts(a1 + "T00:00:00+00:00").timestamp()
            print("           ", half, stats([a for a in s if lo <= a["ts"] < hi]))
al = out[65]
print("\n== thr65 uncapped, by category x side")
g = defaultdict(list)
for a in al: g[(("Earnings mentions" if a["em"] else a["category"]), a["side"])].append(a)
for k in sorted(g, key=lambda k: -len(g[k])):
    if len(g[k]) >= 40: print("  {:24s} {:3s} {}".format(k[0], k[1], stats(g[k])))
print("\n== thr65 uncapped, by score band x side")
g = defaultdict(list)
for a in al: g[(int(a["score"]) // 5 * 5, a["side"])].append(a)
for k in sorted(g): print("  {}-{} {:3s} {}".format(k[0], k[0] + 4, k[1], stats(g[k])))
print("\n== thr65 uncapped NO, by entry")
g = defaultdict(list)
for a in al:
    if a["side"] == "NO": g[a["entry"] // 10 * 10].append(a)
for k in sorted(g): print("  {}c {}".format(k, stats(g[k])))
print("\n== thr65 uncapped, alerts per day")
days = defaultdict(int)
for a in al: days[int(a["ts"] // 86400)] += 1
print("  mean", sum(days.values()) / 365, "max", max(days.values()))
json.dump({str(k): v for k, v in out.items()}, open("data/bt1y_uncapped_alerts.json", "w"))
