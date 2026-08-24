"""
Kalshi paper-trading account + dashboard.

Funded with $1,000. For every Kalshi alert it opens a simulated position sized
by confidence, and settles when the real market settles.

Design decisions carried over from the previous project's failures:
  - CASH ACCOUNTING: money leaves cash when a bet opens, so exposure can never
    exceed the account (the old one showed $7,666 deployed on a $1,534 account).
  - PER-EVENT CAP: no single event can take more than EVENT_CAP_PCT of the
    account (one FOMC event across 25 correlated sub-markets cost -52%).
  - FORWARD-ONLY: starts at inception, never replays history as if we had traded it.
  - SIGNAL WIN RATE reported alongside account return, so a cash-constrained
    account can't flatter the underlying strategy (or vice versa).
  - Fees modelled with Kalshi's real formula.

Run:
  python src/paper_trader.py --report
  python src/paper_trader.py --html docs/index.html
"""
import argparse
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "kalshi_alerts.db")


def load_accounts():
    """Accounts are independent paper simulations over the SAME shared alert
    stream. Each has its own bankroll, cash, positions and caps, so one account
    can never influence another."""
    import json
    cfg = os.path.join(ROOT, "accounts.json")
    if not os.path.exists(cfg):
        return [{"id": "main", "label": "Main", "paper_bankroll_cents": 100000}]
    with open(cfg, encoding="utf-8") as f:
        return [a for a in json.load(f).get("accounts", []) if a.get("enabled", True)]


STARTING_CENTS = 1000_00              # $1,000 default
EVENT_CAP_PCT = 0.15                  # max 15% of account on any one event
MAX_STAKE_CENTS = 50_00
MIN_STAKE_CENTS = 20_00
APPLY_FEES = True

# Confidence -> % of account. Deliberately modest so ~20-30 concurrent
# positions fit and the account can take most signals.
SIZING = [(95, 0.050), (90, 0.040), (85, 0.030), (0, 0.020)]


def fee_cents(contracts, price_cents):
    if not APPLY_FEES:
        return 0
    p = price_cents / 100.0
    return math.ceil(0.07 * contracts * p * (1 - p) * 100)


def stake_pct(score):
    for thr, pct in SIZING:
        if score >= thr:
            return pct
    return SIZING[-1][1]


def fmt(c):
    return "${:,.2f}".format(c / 100.0)


def build(starting_cents=None, label=None):
    con = sqlite3.connect(DB, timeout=30)
    rows = con.execute(
        "SELECT id, ts, ticker, event_ticker, title, side, entry_cents, score, "
        "reasons, link, result FROM alerts ORDER BY ts ASC").fetchall()

    start = starting_cents or STARTING_CENTS
    cash = start
    open_pos, closed = [], []
    wins = losses = 0
    peak = start
    max_dd = 0.0
    curve = []
    event_exposure = defaultdict(int)
    skipped_cap = 0

    def open_cost():
        return sum(p["stake"] for p in open_pos)

    for (aid, ts, ticker, ev, title, side, entry_c, score, reasons, link, result) in rows:
        if entry_c is None or not (1 <= entry_c <= 99):
            continue
        equity = cash + open_cost()
        stake = round(stake_pct(score or 0) * equity)
        stake = max(MIN_STAKE_CENTS, min(stake, MAX_STAKE_CENTS))

        # per-event concentration cap
        ev_key = ev or ticker
        if event_exposure[ev_key] + stake > EVENT_CAP_PCT * equity:
            skipped_cap += 1
            continue
        stake = min(stake, max(0, cash - 100))
        if stake < MIN_STAKE_CENTS // 2:
            continue

        contracts = stake / entry_c
        fee = fee_cents(contracts, entry_c)
        rec = {"id": aid, "ts": ts, "ticker": ticker, "event": ev_key, "title": title,
               "side": side, "entry": entry_c, "score": score or 0,
               "stake": stake, "contracts": contracts, "fee": fee, "link": link}

        if result not in ("WIN", "LOSS"):
            cash -= stake
            event_exposure[ev_key] += stake
            open_pos.append(rec)
            continue

        cash -= stake
        if result == "WIN":
            payout = round(contracts * 100)
            cash += payout - fee
            pnl = payout - stake - fee
            wins += 1
        else:
            cash -= fee
            pnl = -stake - fee
            losses += 1
        if cash < 0:
            cash = 0
        equity = cash + open_cost()
        peak = max(peak, equity)
        if peak:
            max_dd = max(max_dd, (peak - equity) / peak)
        rec.update({"result": result, "pnl": pnl, "equity": equity})
        closed.append(rec)
        curve.append((ts, equity))

    equity = cash + open_cost()
    n = wins + losses
    # true strategy win rate over ALL settled alerts, regardless of funding
    sig = [r for r in rows if r[10] in ("WIN", "LOSS")]
    sig_w = sum(1 for r in sig if r[10] == "WIN")

    return {
        "stats": {
            "equity": equity, "cash": cash, "start": start,
            "label": label or "Main",
            "return_pct": (equity - start) / start,
            "trades": n, "wins": wins, "losses": losses,
            "win_rate": (wins / n) if n else 0,
            "signal_n": len(sig), "signal_wins": sig_w,
            "signal_rate": (sig_w / len(sig)) if sig else 0,
            "open": len(open_pos), "open_exposure": open_cost(),
            "peak": peak, "max_dd": max_dd, "skipped_cap": skipped_cap,
            "total_alerts": len(rows),
        },
        "closed": closed, "open": open_pos, "curve": curve,
    }


def report(starting_cents=None, label=None):
    d = build(starting_cents, label)
    s = d["stats"]
    print("Kalshi Paper Trader [{}]  ({:%Y-%m-%d %H:%M UTC})".format(
        s.get("label", "Main"), datetime.now(timezone.utc)))
    print("  Start           : " + fmt(s["start"]))
    print("  Account value   : {}  (cash {} + open {})".format(
        fmt(s["equity"]), fmt(s["cash"]), fmt(s["open_exposure"])))
    print("  Return          : {:+.1f}%".format(s["return_pct"] * 100))
    print("  Signal win rate : {:.1%}  ({}/{} settled alerts)  <- true edge".format(
        s["signal_rate"], s["signal_wins"], s["signal_n"]))
    print("  Account trades  : {}  ({}W/{}L, {:.1%})".format(
        s["trades"], s["wins"], s["losses"], s["win_rate"]))
    print("  Open positions  : {}  ({})".format(s["open"], fmt(s["open_exposure"])))
    print("  Max drawdown    : -{:.1f}%".format(s["max_dd"] * 100))
    print("  Alerts total    : {} (skipped by event cap: {})".format(
        s["total_alerts"], s["skipped_cap"]))


def svg_curve(curve, w=920, h=220, start=None):
    if len(curve) < 2:
        return '<div class="muted">Not enough settled trades yet to draw a curve.</div>'
    vals = [c[1] for c in curve]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi = lo + 1
    pad, n = 8, len(vals)
    xs = lambda i: pad + i * (w - 2 * pad) / (n - 1)
    ys = lambda v: h - pad - (v - lo) * (h - 2 * pad) / (hi - lo)
    pts = " ".join("{:.1f},{:.1f}".format(xs(i), ys(v)) for i, v in enumerate(vals))
    base_v = start or STARTING_CENTS
    up = vals[-1] >= base_v
    col = "#2ecc71" if up else "#e74c3c"
    base = ""
    if lo <= base_v <= hi:
        by = ys(base_v)
        base = ('<line x1="{}" y1="{:.1f}" x2="{}" y2="{:.1f}" stroke="#555" '
                'stroke-dasharray="4 4" stroke-width="1"/>').format(pad, by, w - pad, by)
    return ('<svg viewBox="0 0 {} {}" width="100%" preserveAspectRatio="none">'
            '{}<polyline points="{}" fill="none" stroke="{}" stroke-width="2"/>'
            '<text x="{}" y="14" fill="#888" font-size="11">{}</text>'
            '<text x="{}" y="{}" fill="#888" font-size="11">{}</text></svg>').format(
        w, h, base, pts, col, pad, fmt(hi), pad, h - 2, fmt(lo))


def render(d):
    s = d["stats"]
    col = "#2ecc71" if s["return_pct"] >= 0 else "#e74c3c"
    sign = "+" if s["return_pct"] >= 0 else ""

    def esc(t):
        return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    rows_html = ""
    for t in reversed(d["closed"][-60:]):
        mark = "✅" if t["result"] == "WIN" else "❌"
        pc = "#2ecc71" if t["pnl"] >= 0 else "#e74c3c"
        rows_html += (
            '<tr><td class="hide-sm">{}</td><td>{}</td>'
            '<td class="mkt" title="{}">{}</td>'
            '<td class="hide-sm">{}</td><td class="r hide-sm">{:.0f}</td>'
            '<td class="r hide-sm">{}¢</td><td class="r">{}</td>'
            '<td class="r" style="color:{}">{}{}</td>'
            '<td class="r hide-sm">{}</td></tr>').format(
            t["ts"][5:16], mark, esc(t["title"]), esc(t["title"])[:80],
            t["side"], t["score"], t["entry"], fmt(t["stake"]),
            pc, "+" if t["pnl"] >= 0 else "", fmt(t["pnl"]), fmt(t["equity"]))

    open_html = ""
    for t in d["open"][:25]:
        open_html += (
            '<tr><td class="hide-sm">{}</td><td class="mkt" title="{}">{}</td>'
            '<td>{}</td><td class="r hide-sm">{:.0f}</td><td class="r hide-sm">{}¢</td>'
            '<td class="r">{}</td></tr>').format(
            t["ts"][5:16], esc(t["title"]), esc(t["title"])[:80], t["side"],
            t["score"], t["entry"], fmt(t["stake"]))
    if not open_html:
        open_html = '<tr><td colspan="6" class="muted">No open positions.</td></tr>'

    tiers = " · ".join("{}+→{:.0f}%".format(th, p * 100) for th, p in SIZING if th)

    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalshi Paper Trader — {label}</title>
<style>
*{{box-sizing:border-box}}
body{{background:#0d1117;color:#e6edf3;margin:0;padding:14px;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;-webkit-text-size-adjust:100%}}
h1{{font-size:18px;margin:0 0 4px}}
.sub{{color:#8b949e;font-size:12px;margin-bottom:16px;line-height:1.5}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:18px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 14px}}
.card .label{{color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:.4px}}
.card .value{{font-size:22px;font-weight:600;margin-top:5px;line-height:1.15}}
.card .value span{{font-size:13px;color:#8b949e}}
.panel{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px;margin-bottom:16px}}
.panel h2{{font-size:13px;margin:0 0 10px;color:#c9d1d9;text-transform:uppercase;letter-spacing:.4px}}
.tablewrap{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}
th,td{{text-align:left;padding:7px 8px;border-bottom:1px solid #21262d;white-space:nowrap}}
th{{color:#8b949e;font-weight:500;font-size:10.5px;text-transform:uppercase}}
td.r,th.r{{text-align:right;font-variant-numeric:tabular-nums}}
td.mkt{{color:#c9d1d9;max-width:46vw;overflow:hidden;text-overflow:ellipsis}}
.muted{{color:#6e7681;text-align:center;padding:16px}}
.legend{{color:#8b949e;font-size:11.5px;margin-top:10px;line-height:1.6}}
.warn{{background:#2d2212;border:1px solid #6b4d1a;color:#e0b968;border-radius:8px;
 padding:10px 12px;font-size:12px;margin-bottom:16px;line-height:1.5}}
@media(min-width:700px){{body{{padding:24px}}h1{{font-size:22px}}.card .value{{font-size:26px}}td.mkt{{max-width:440px}}}}
@media(max-width:560px){{.hide-sm{{display:none}}td.mkt{{max-width:54vw}}}}
</style></head><body>
<h1>\U0001F4C8 Kalshi Paper Trader</h1>
<div class="sub">Simulated account · funded {start} · <b>Kalshi only</b> ·
 sized by confidence · settles on real Kalshi outcomes ·
 <span class="muted">updated {now:%Y-%m-%d %H:%M UTC}</span></div>

<div class="warn"><b>Paper money.</b> No real funds are at risk. This is a live
 forward test to find out whether the alert strategy has an edge on Kalshi &mdash;
 no edge has been validated yet, so treat these numbers as an experiment in progress,
 not a track record.</div>

<div class="cards">
 <div class="card"><div class="label">Account Value</div><div class="value">{equity}</div></div>
 <div class="card"><div class="label">Return</div><div class="value" style="color:{col}">{sign}{ret:.1f}%</div></div>
 <div class="card"><div class="label">Cash Free</div><div class="value">{cash}</div></div>
 <div class="card"><div class="label">Signal Win Rate</div>
   <div class="value">{sigrate:.1f}%<span> ({sigw}/{sign_n})</span></div></div>
 <div class="card"><div class="label">Account Trades</div>
   <div class="value">{trades}<span> ({w}W/{l}L)</span></div></div>
 <div class="card"><div class="label">Max Drawdown</div>
   <div class="value" style="color:#e0a458">-{dd:.1f}%</div></div>
 <div class="card"><div class="label">In Open Bets</div>
   <div class="value">{openexp}<span> ({openn})</span></div></div>
</div>

<div class="panel"><h2>Equity Curve &mdash; {start} → {equity}</h2>
{curve}
<div class="legend">Sizing: {tiers} &nbsp;|&nbsp; per-bet cap {maxstake} &nbsp;|&nbsp;
 per-event cap {evcap:.0f}% of account &nbsp;|&nbsp; Kalshi fees on &nbsp;|&nbsp;
 filters: no sports, no auto-generated, no 15-min direction markets</div></div>

<div class="panel"><h2>Open Positions ({openn})</h2><div class="tablewrap"><table>
<tr><th class="hide-sm">Time</th><th>Market</th><th>Side</th>
<th class="r hide-sm">Score</th><th class="r hide-sm">Entry</th><th class="r">Stake</th></tr>
{open_html}</table></div></div>

<div class="panel"><h2>Settled Trades (last 60)</h2><div class="tablewrap"><table>
<tr><th class="hide-sm">Time</th><th></th><th>Market</th><th class="hide-sm">Side</th>
<th class="r hide-sm">Score</th><th class="r hide-sm">Entry</th><th class="r">Stake</th>
<th class="r">P&amp;L</th><th class="r hide-sm">Equity</th></tr>
{rows_html}</table></div></div>
</body></html>""".format(
        label=s.get("label","Main"), start=fmt(s["start"]), now=datetime.now(timezone.utc), equity=fmt(s["equity"]),
        col=col, sign=sign, ret=s["return_pct"] * 100, cash=fmt(s["cash"]),
        sigrate=s["signal_rate"] * 100, sigw=s["signal_wins"], sign_n=s["signal_n"],
        trades=s["trades"], w=s["wins"], l=s["losses"], dd=s["max_dd"] * 100,
        openexp=fmt(s["open_exposure"]), openn=s["open"],
        curve=svg_curve(d["curve"], start=s["start"]), tiers=tiers, maxstake=fmt(MAX_STAKE_CENTS),
        evcap=EVENT_CAP_PCT * 100, open_html=open_html, rows_html=rows_html)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--html")
    ap.add_argument("--account", help="account id from accounts.json; omit for all")
    a = ap.parse_args()

    accounts = load_accounts()
    if a.account:
        accounts = [x for x in accounts if x["id"] == a.account] or accounts[:1]

    for acct in accounts:
        bank = acct.get("paper_bankroll_cents", STARTING_CENTS)
        label = acct.get("label", acct.get("id", "Main"))
        if a.html:
            # one dashboard per account: docs/index.html, docs/<id>.html
            base = a.html if os.path.isabs(a.html) else os.path.join(ROOT, a.html)
            if len(accounts) > 1:
                d_, f_ = os.path.split(base)
                base = os.path.join(d_, acct["id"] + ".html")
            os.makedirs(os.path.dirname(base), exist_ok=True)
            with open(base, "w", encoding="utf-8") as f:
                f.write(render(build(bank, label)))
            print("wrote " + base)
        else:
            report(bank, label)
            print()
