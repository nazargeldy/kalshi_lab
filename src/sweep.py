"""Sweep every category x side with per-trade bootstrap ROI + time-split consistency."""
import math, os, random, sqlite3, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validate import fee, load, bootstrap_roi
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "kalshi.db")
random.seed(7)

def roi_yes(r):
    a = r[7]
    if a is None or not (0 < a < 1): return None
    cost = a + fee(a)
    return ((1.0 if r[2] == "yes" else 0.0) - cost) / cost

def roi_no(r):
    b = r[6]
    if b is None or not (0 < b < 1): return None
    p = 1.0 - b; cost = p + fee(p)
    return ((1.0 if r[2] == "no" else 0.0) - cost) / cost

con = sqlite3.connect(DB)
rows = load(con, 24, 200)
by = defaultdict(list)
for r in rows:
    by[r[8] or "?"].append(r)
by["ALL"] = rows

print(f"{'category':<24} {'side':<4} {'n':>5} {'meanROI':>9} {'95% CI':>20} {'H1':>8} {'H2':>8} {'verdict':<12}")
print("-"*98)
out=[]
for cat, rs in sorted(by.items(), key=lambda x:-len(x[1])):
    if len(rs) < 40: continue
    for side, fn in (("YES", roi_yes), ("NO", roi_no)):
        pairs = [(r, fn(r)) for r in rs]
        pairs = [(r,v) for r,v in pairs if v is not None]
        if len(pairs) < 40: continue
        vals = [v for _,v in pairs]
        m, lo, hi = bootstrap_roi(vals, 3000)
        d = sorted(((r[4] or ""), v) for r,v in pairs)
        mid = len(d)//2
        h1 = sum(v for _,v in d[:mid])/max(1,mid)
        h2 = sum(v for _,v in d[mid:])/max(1,len(d)-mid)
        if lo > 0 and (h1>0) == (h2>0): verdict = "SURVIVES"
        elif hi < 0: verdict = "negative"
        else: verdict = "noise"
        print(f"{cat[:24]:<24} {side:<4} {len(vals):>5} {m:>+8.1%} [{lo:>+7.1%},{hi:>+7.1%}] {h1:>+7.1%} {h2:>+7.1%} {verdict:<12}")
        out.append((cat, side, verdict))
print()
surv = [o for o in out if o[2]=="SURVIVES"]
print(f"SURVIVING EDGES: {surv if surv else 'NONE'}")
