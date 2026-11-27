import sys, json; sys.path.insert(0, "src")
import bt1y_replay as R
al = json.load(open("data/bt1y_uncapped_alerts.json"))["65"]
MID = R.pts("2026-05-26T00:00:00+00:00").timestamp()
def st(x):
    if not x: return "n=0"
    rs = R.per_trade(x); lo, hi = R.boot_ci(rs)
    return "n={:5d} win={:.3f} impl={:.3f} roi={:+.4f} ci=[{:+.4f},{:+.4f}]".format(len(x),
        sum(a["outcome"]=="WIN" for a in x)/len(x), sum(a["entry"] for a in x)/len(x)/100, sum(rs)/len(x), lo, hi)
cat = lambda a: "Earnings mentions" if a["em"] else a["category"]
sets = {
 "NO, Econ+Politics+SciTech": lambda a: a["side"]=="NO" and cat(a) in ("Economics","Politics","Science and Technology"),
 "NO, all except Financials/Earnings": lambda a: a["side"]=="NO" and cat(a) not in ("Financials","Earnings mentions"),
 "NO, all": lambda a: a["side"]=="NO",
 "YES, Commodities": lambda a: a["side"]=="YES" and cat(a)=="Commodities",
 "YES, all except Commodities": lambda a: a["side"]=="YES" and cat(a)!="Commodities",
}
for k, f in sets.items():
    x = [a for a in al if f(a)]
    print(k); print("   year", st(x)); print("   H1  ", st([a for a in x if a["ts"]<MID])); print("   H2  ", st([a for a in x if a["ts"]>=MID]))
