"""
Kalshi-only unusual-activity monitor.

100% Kalshi. No Polymarket. Alerts persist to SQLite, appear on the
dashboard, and are pushed to any configured channel (Telegram / Discord).

Signals (all from Kalshi's public trade feed):
  A) Size shock      - trade size vs rolling market baseline (robust z-score)
  B) Burst           - trade frequency spike
  C) Flow imbalance  - taker_side skew (Kalshi's answer to VPIN)
  D) Price momentum  - move over the recent window
  E) Short-dated     - hours to close (context, not an anomaly signal)

Guardrails carried over from the previous project's expensive mistakes:
  - event-level dedup (one FOMC event across 25 sub-markets cost -52%)
  - no side-flipping on the same event
  - junk-market + sports filters
  - longshot penalty (cheap tails lose after fees)
  - daily cap + per-market cooldown
  - >=2 independent anomaly signals required before any alert
  - rules.py (2026-09-16): no crypto price markets, entry 25-88c, settles
    within 14 days, one position per event with no side flips or 24h re-entry.
    Checked against the DB so a restart cannot forget an open position.
"""
import argparse
import os
import sqlite3
import statistics
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
from kalshi_client import KalshiClient
from notifier import notify as send_notification
import rules

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "kalshi_alerts.db")

# The account did not blow up on one bad bet - it was ground down. -7.6% per
# trade over 292 trades in three weeks compounds to -63%. Cutting turnover is
# the largest lever available, because the edge is not reliably positive and
# every extra trade pays the fee again.
#
# Turnover is cut with DAILY_CAP, NOT by raising ALERT_THRESHOLD. Raising the
# threshold would be actively harmful: the score is anti-predictive, so the
# 75-79 band is the BEST performing one (n=132, -0.9% ROI) and 95-99 is the
# worst (n=26, -24.8%). Lifting the bar to 80 would throw away the best band
# and keep the worst. Threshold stays at 75 until the score is fixed or
# replaced.
# 2026-09-16: time-to-close no longer contributes points (see score()). Daily
# markets used to get +15 for free, so 75 was really "60 from anomaly signals"
# for crypto/weather and "75 from anomaly signals" for everything else - which
# made a YES alert on any market closing >3 days out mathematically impossible
# (ceiling 35+22+20-8 = 69). Threshold is now 60 for every category alike.
ALERT_THRESHOLD = 60
DAILY_CAP = 10   # was 8; raised 2026-10-11 now that weather no longer eats the cap
MARKET_COOLDOWN = 3600
EVENT_COOLDOWN = 4 * 3600
SIDE_FLIP_WINDOW = 24 * 3600
POLL_SEC = 30
BASELINE_MIN_TRADES = 8

# Categories we never trade. Uses Kalshi's own event category, which is far more
# reliable than matching ticker substrings.
#   Sports        - match markets are sharp, fast, dominated by specialists.
#   Entertainment - n=11 live, 36% win / -47% ROI, CI upper -5.4%. Awards, chart
#                   positions and reality-TV eliminations resolve on private
#                   information (voting panels, studio decisions) that no amount
#                   of order-flow watching can reach.
#   Mentions      - n=5 live, -52% ROI, CI upper -3.3%. "Will X say word Y" is a
#                   coin flip on speech we cannot forecast; spreads are wide.
# NOTE: Politics/Elections are deliberately NOT blocked. The old Polymarket rule
# banned them, but that was a non-US, crypto-native trader base. On Kalshi the
# traders are US persons on a CFTC venue and the live sample is tiny but
# positive (Politics n=2, Elections n=3). There is no evidence to ban them here.
#
# 2026-09-23: Mentions carries earnings-call word-mention markets
# (KXEARNINGSMENTION*, "What will X say on their earnings call?") alongside
# the celebrity/award/political "what will X say" markets that drove the
# -52% ROI finding above. Those two are different bets - one is about a
# public company's disclosed talking points, the other is unscriptable
# speech - so earnings-mention markets are exempted below (rules.is_earnings_mention)
# rather than lumped in with the rest of the category.
#
# 2026-10-11: Climate and Weather blocked. 148 settled weather alerts across two
# periods: -13.4% (n=77, since 9/27) and -14.0% (n=71, 8/23-9/26) net ROI per
# trade, and weather was 75% of all alerts since the reset - it crowded out
# everything else under the daily cap.
BLOCKED_CATEGORIES = {"Sports", "Entertainment", "Mentions", "Climate and Weather"}

# Backstop for when the category lookup fails. Series prefixes seen in live alerts.
WEATHER_PREFIXES = ("KXHIGH", "KXLOW", "KXTEMP", "KXRAIN", "KXSNOW", "KXWIND")

# 2026-10-11: any series with TRUMP in its name is skipped (matched on the
# series ticker, not the title, so politics markets that merely mention him
# still alert). Approval-rating series went 1-5 and Trump markets are one
# correlated bet on a single person.

# Refuse terrible risk/reward. Buying at 98c risks 98c to make 2c -> needs a
# 98%+ hit rate just to break even. Buying at 3c is a lottery ticket.
# Was 12-88. Live result for entries under 20c: 1 win in 23 (4.3% vs 16%
# implied), -$369. Raised to 25 - see rules.py.
MIN_ENTRY_CENTS = rules.MIN_ENTRY_CENTS
MAX_ENTRY_CENTS = rules.MAX_ENTRY_CENTS

JUNK_PREFIXES = ("KXMVE",)

# Range/bucket ladders: "Bitcoin price RANGE on Aug 23" splits one event into a
# strip of narrow buckets, so you need the settle to land inside a band rather
# than merely above or below a strike. Lower base rate, wider spreads, and the
# fee is charged on each leg. KXBTC (the range ladder) went 23.1% win / -52.6%
# ROI over n=26 with a CI upper bound of -14.3% - the strongest negative in the
# sample - while its threshold-style sibling KXBTCD was roughly break-even.
RANGE_SERIES = ("KXBTC",)
RANGE_TITLE = ("price range", "range on", "between")
# Ultra-short crypto/index direction markets ("price up in next 15 mins?").
# These are the coin-flip category that produced 39% win rate / -16% ROI in the
# previous project. Structurally unpredictable; unusual flow carries no edge.
DIRECTION_HINTS = ("15M", "1H", "HOURLY", "UPDOWN")
DIRECTION_TITLE = ("up in next", "up or down")
SPORTS_HINTS = ("GAME", "NFL", "NBA", "MLB", "NHL", "SOCCER", "LIGUE", "LALIGA",
                "SERIEA", "EPL", "UFC", "TENNIS", "GOLF", "NCAA", "CFB", "MLS",
                "BUNDES", "CRICKET", "ESPORT", "VALORANT", "LOL", "CS2", "WC")

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticker TEXT, event_ticker TEXT,
  title TEXT, side TEXT, entry_cents INTEGER, score REAL, reasons TEXT,
  link TEXT, contracts REAL, result TEXT, settled_ts TEXT, close_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ticker ON alerts(ticker);
CREATE INDEX IF NOT EXISTS idx_alerts_event ON alerts(event_ticker);
"""


def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    # close_ts was added 2026-09-16 so the paper trader can enforce the
    # max-horizon rule; older rows simply have NULL.
    cols = {r[1] for r in c.execute("PRAGMA table_info(alerts)")}
    if "close_ts" not in cols:
        c.execute("ALTER TABLE alerts ADD COLUMN close_ts TEXT")
        c.commit()
    return c


def prior_event_alerts(con, event):
    """Every earlier alert on this event, from the DB rather than in-memory
    state, so a service restart cannot forget a position we already hold."""
    return con.execute(
        "SELECT ts, side, result FROM alerts WHERE event_ticker=? ORDER BY ts",
        (event,)).fetchall()


def series_of(t):
    return (t or "").split("-")[0]


def is_junk(t):
    return any(series_of(t).startswith(j) for j in JUNK_PREFIXES)


def is_sports(t):
    s = series_of(t).upper()
    return any(h in s for h in SPORTS_HINTS)


def is_weather(t):
    return series_of(t).upper().startswith(WEATHER_PREFIXES)


def is_range_ladder(ticker, title=""):
    if series_of(ticker).upper() in RANGE_SERIES:
        return True
    tl = (title or "").lower()
    return any(h in tl for h in RANGE_TITLE)


def is_direction(ticker, title=""):
    s = series_of(ticker).upper()
    if any(h in s for h in DIRECTION_HINTS):
        return True
    tl = (title or "").lower()
    return any(h in tl for h in DIRECTION_TITLE)


def is_trump(t):
    return "TRUMP" in series_of(t).upper()


# Owner's watchlist: ping on EVERY trade in these markets, regardless of score,
# rules or DAILY_CAP. Matched on ticker or title, so new series are picked up
# automatically. Watch pings never touch the alerts table, so they don't open
# paper positions.
WATCH_TERMS = ("PANW", "PALO ALTO")
WATCH_BIG_USD = 100          # trades at least this size get a whale marker


def is_watched(ticker, title=""):
    hay = ((ticker or "") + " " + (title or "")).upper()
    return any(w in hay for w in WATCH_TERMS)


def format_watch(batch):
    """One Telegram message for all watched trades seen in a poll.
    batch: [(trade, meta), ...] oldest first. Returns (title, body, link)."""
    by_market = {}
    for t, meta in batch:
        by_market.setdefault(t.get("ticker"), (meta, []))[1].append(t)

    first_tk, (first_meta, _) = next(iter(by_market.items()))
    total_usd, lines = 0.0, []
    for tk, (meta, trades) in by_market.items():
        lines.append("\U0001F5E3️ **“{}”**".format(meta.get("yes_sub") or tk))
        for t in trades:
            n = float(t.get("count_fp") or 0)
            yes_p = float(t.get("yes_price_dollars") or 0)
            if t.get("taker_side") == "yes":
                icon, side, p = "\U0001F7E2", "YES", yes_p
            else:
                icon, side = "\U0001F534", "NO"
                p = float(t.get("no_price_dollars") or (1 - yes_p))
            usd = n * p
            total_usd += usd
            whale = "  \U0001F40B" if usd >= WATCH_BIG_USD else ""
            lines.append("{} Bought **{}** · {:,.0f} @ {:.0f}¢ = **${:,.2f}**{}".format(
                icon, side, n, p * 100, usd, whale))
        lines.append("   Now ≈ YES {:.0f}¢".format(
            float(trades[-1].get("yes_price_dollars") or 0) * 100))
        lines.append("")

    head = ["**" + (first_meta.get("title") or first_tk) + "**"]
    occ = first_meta.get("occurrence")
    if occ:
        try:
            h = (datetime.fromisoformat(occ.replace("Z", "+00:00"))
                 - datetime.now(timezone.utc)).total_seconds() / 3600.0
            head.append("⏱️ Event " + _fmt_close(h) if h > 0 else "⏱️ Event underway / done")
        except Exception:
            pass
    head.append("")

    n_tr = len(batch)
    title = "\U0001F440 PANW WATCH · {} new trade{} · ${:,.0f}".format(
        n_tr, "" if n_tr == 1 else "s", total_usd)
    body = "\n".join(head + lines) + "\n\U0001F552 " + datetime.now(timezone.utc).strftime("%H:%M UTC")
    return title, body, kalshi_url(series_of(first_tk), first_meta.get("event"))


DASHBOARD_URL = os.getenv(
    "PAPER_DASHBOARD_URL", "https://nazargeldy.github.io/kalshi-paper-trader/")


def kalshi_url(series, event):
    if series and event:
        return "https://kalshi.com/markets/" + series.lower() + "/" + event.lower()
    return "https://kalshi.com/markets"


def _fmt_close(hours):
    """Human-readable time-to-close for the alert body."""
    if hours is None:
        return "unknown"
    if hours < 1:
        return "in {:.0f} min".format(max(1, hours * 60))
    if hours < 24:
        return "in {:.0f}h".format(hours)
    return "in {:.0f}d".format(hours / 24.0)


def notify(title, body, link=None):
    """Alert sink: stdout + every configured channel (Telegram / Discord).
    Uses a Telegram BOT token and a Discord WEBHOOK - never user accounts."""
    print(title + " | " + body.replace(chr(10), " ")[:160])
    try:
        send_notification(title, body, url=link)
    except Exception as e:
        print("notify error: " + str(e)[:100])
    return True


class Baselines:
    """Rolling per-market state used for anomaly scoring."""

    def __init__(self):
        self.sizes = defaultdict(lambda: deque(maxlen=200))
        self.times = defaultdict(lambda: deque(maxlen=400))
        self.prices = defaultdict(lambda: deque(maxlen=200))
        self.flow = defaultdict(lambda: deque(maxlen=100))

    def add(self, tk, size, ts, price, taker_yes):
        self.sizes[tk].append(size)
        self.times[tk].append(ts)
        self.prices[tk].append(price)
        self.flow[tk].append(1 if taker_yes else -1)

    def score(self, tk, size, price, hours_to_close):
        s = 0
        reasons = []
        hits = 0
        _fl0 = list(self.flow[tk])
        side_dir = "YES" if sum(_fl0) > 0 else "NO"

        sizes = list(self.sizes[tk])
        if len(sizes) >= BASELINE_MIN_TRADES:
            med = statistics.median(sizes)
            mad = statistics.median([abs(x - med) for x in sizes]) or 1.0
            z = (size - med) / (1.4826 * mad + 1e-9)
            if z >= 8:
                s += 35; hits += 1; reasons.append("Trade size {:.0f}x above normal".format(z))
            elif z >= 5:
                s += 24; hits += 1; reasons.append("Trade size {:.0f}x above normal".format(z))
            elif z >= 3:
                s += 12; hits += 1; reasons.append("Trade size {:.0f}x above normal".format(z))

        ts_list = list(self.times[tk])
        if len(ts_list) >= BASELINE_MIN_TRADES:
            now = ts_list[-1]
            last_1m = sum(1 for t in ts_list if now - t <= 60)
            last_60m = max(1, sum(1 for t in ts_list if now - t <= 3600))
            rate = last_1m / max(last_60m / 60.0, 0.5)
            if rate >= 10:
                s += 22; hits += 1; reasons.append("{:.0f}x trade-frequency spike".format(rate))
            elif rate >= 5:
                s += 14; hits += 1; reasons.append("{:.0f}x trade-frequency spike".format(rate))

        # Order-flow imbalance: DISABLED as a scoring signal.
        # Live result over 62 settled alerts: alerts containing it went 35.3%
        # win / -49.7% ROI, CI[-81.5%,-14.4%] - the only statistically
        # significant finding in the sample, and it is negative. Excluding it
        # lifted the rest to 57.8% / +26.7%.
        # Mechanism: heavy one-sided TAKER flow means someone is crossing the
        # spread for immediacy. That is liquidity-demanding, not informed - you
        # enter right after the price was pushed against you. Unlike Polymarket
        # (where wallets are visible and identity carries information), Kalshi
        # flow is anonymous, so aggression is all we see.
        # Kept as an informational tag only; contributes no score and no hit.
        fl = list(self.flow[tk])
        if len(fl) >= 12:
            imb = abs(sum(fl)) / len(fl)
            side = "YES" if sum(fl) > 0 else "NO"
            if imb >= 0.80:
                reasons.append("(fyi) one-sided flow {:.0%} {} takers".format(imb, side))

        pr = list(self.prices[tk])
        if len(pr) >= 10:
            delta = (pr[-1] - pr[0]) * 100
            if abs(delta) >= 15:
                s += 20; hits += 1; reasons.append("Price moved {:+.0f}c".format(delta))
            elif abs(delta) >= 8:
                s += 12; hits += 1; reasons.append("Price moved {:+.0f}c".format(delta))

        # Time-to-close is CONTEXT ONLY - it says nothing about whether the
        # flow is informed. It used to add up to 18 points, which handed every
        # daily crypto/weather market a head start and starved Economics,
        # Politics, Companies, Science etc. of alerts (65% of live trades were
        # BTC/ETH). Kept as a tag so the alert still shows the horizon.
        if hours_to_close is not None:
            if hours_to_close <= 2:
                reasons.append("(fyi) closes within 2 hours")
            elif hours_to_close <= 24:
                reasons.append("(fyi) closes within 24 hours")
            elif hours_to_close <= 72:
                reasons.append("(fyi) closes within 3 days")

        # Lesson from the last project: cheap longshots lose badly after fees.
        cents = price * 100
        if cents <= 10 or cents >= 90:
            s -= 12
            reasons.append("Longshot penalty ({:.0f}c)".format(cents))
        elif cents <= 20 or cents >= 80:
            s -= 4

        # YES-side penalty. Every signal we have fires on unusual SIZE or SPEED,
        # and on a retail venue that flow skews toward buying YES - people stake
        # on things happening, not on them failing. So "big trade just went
        # through" is disproportionately a crowd-following buy signal, and we
        # were taking the same side as the crowd. Live split over 292 settled:
        #   YES n=189  41.8% win  -15.5% ROI
        #   NO  n=103  52.4% win   +7.0% ROI
        # A penalty rather than an outright ban: the NO CI still straddles zero,
        # so this leans against YES without betting the system on n=103.
        if side_dir == "YES":
            s -= 8
            reasons.append("YES-side penalty (crowd-following bias)")

        raw = min(s, 100)
        return (min(raw, 45) if hits < 2 else raw), reasons, hits


def run(dry_run=False, once=False, threshold=ALERT_THRESHOLD):
    cli = KalshiClient()
    con = db()
    base = Baselines()
    market_meta = {}
    seen = set()
    market_cd = {}
    event_cd = {}
    event_side = {}
    sent_today = 0
    cap_day = datetime.now(timezone.utc).date()

    print("Kalshi monitor starting (threshold={}, dry_run={})".format(threshold, dry_run))
    print("Kalshi monitor online. Alerts -> SQLite + dashboard + " + str(__import__("notifier").status()))

    poll = 0
    while True:
        poll += 1
        now = time.time()
        if datetime.now(timezone.utc).date() != cap_day:
            cap_day = datetime.now(timezone.utc).date()
            sent_today = 0
        try:
            d = cli.get("/trade-api/v2/markets/trades", {"limit": 200})
            trades = d.get("trades", [])
        except Exception as e:
            print("poll {}: fetch error {}".format(poll, e))
            time.sleep(POLL_SEC)
            continue

        new = [t for t in trades if t.get("trade_id") not in seen]
        for t in new:
            seen.add(t.get("trade_id"))
        if len(seen) > 20000:
            seen = set(list(seen)[-10000:])

        fresh = 0
        watch_batch = []
        for t in reversed(new):
            tk = t.get("ticker")
            if (not tk or is_junk(tk) or is_sports(tk) or is_direction(tk)
                    or is_weather(tk) or is_trump(tk)):
                continue
            try:
                size = float(t.get("count_fp") or 0)
                price = float(t.get("yes_price_dollars") or 0)
            except (TypeError, ValueError):
                continue
            if size <= 0 or not (0 < price < 1):
                continue
            try:
                ts = datetime.fromisoformat(
                    t["created_time"].replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = now
            base.add(tk, size, ts, price, t.get("taker_side") == "yes")
            fresh += 1

            if tk not in market_meta:
                try:
                    m = cli.get("/trade-api/v2/markets/" + tk).get("market", {})
                    ev_tk = m.get("event_ticker")
                    category = None
                    if ev_tk:
                        try:
                            category = cli.get("/trade-api/v2/events/" + ev_tk)                                           .get("event", {}).get("category")
                        except Exception:
                            category = None
                    market_meta[tk] = {
                        "title": m.get("title") or tk,
                        "event": ev_tk,
                        "close": m.get("close_time"),
                        "occurrence": m.get("occurrence_datetime"),
                        "yes_sub": m.get("yes_sub_title"),
                        "no_sub": m.get("no_sub_title"),
                        "category": category,
                    }
                except Exception:
                    market_meta[tk] = {"title": tk, "event": None, "close": None,
                                       "yes_sub": None, "no_sub": None}
            meta = market_meta[tk]
            # Poll 1 replays the last 200 trades, which were already pinged
            # before a restart.
            if poll > 1 and is_watched(tk, meta.get("title")):
                watch_batch.append((t, meta))

            # Earnings-mention markets: Kalshi leaves close_time months out as a
            # placeholder until the market settles (Alphabet's Oct 29 call showed
            # a Mar 2027 close), which tripped the 14-day rule on every one. The
            # real date is occurrence_datetime.
            em = (meta.get("category") == "Mentions"
                  and rules.is_earnings_mention(tk, meta.get("title")))
            eff_close = (meta.get("occurrence") if em and meta.get("occurrence")
                         else meta.get("close"))

            htc = None
            if eff_close:
                try:
                    ct = datetime.fromisoformat(eff_close.replace("Z", "+00:00"))
                    htc = (ct - datetime.now(timezone.utc)).total_seconds() / 3600.0
                    if htc < 0:
                        continue
                except Exception:
                    pass

            if is_direction(tk, meta.get("title")):
                continue
            if is_range_ladder(tk, meta.get("title")):
                continue
            if rules.is_crypto(tk, meta.get("title")):
                continue
            if meta.get("category") in BLOCKED_CATEGORIES and not em:
                continue

            score, reasons, hits = base.score(tk, size, price, htc)
            if score < threshold:
                continue
            if now - market_cd.get(tk, 0) < MARKET_COOLDOWN:
                continue

            ev = meta.get("event") or tk
            # Each word in an earnings call is its own independent bet, but
            # Kalshi groups ~20 of them under one event. Deduping on the event
            # would allow one word per call, so key them per market instead.
            dk = tk if em else ev
            if now - event_cd.get(dk, 0) < EVENT_COOLDOWN:
                print("  EVENT-dedup skip: " + meta["title"][:50])
                market_cd[tk] = now
                continue

            fl = list(base.flow[tk])
            side_dir = "YES" if sum(fl) > 0 else "NO"
            prev = event_side.get(dk)
            if prev and prev != side_dir and now - event_cd.get(dk, 0) < SIDE_FLIP_WINDOW:
                print("  SIDE-FLIP skip: " + meta["title"][:45])
                market_cd[tk] = now
                continue

            if sent_today >= DAILY_CAP:
                print("  daily cap reached")
                continue

            entry_c = round(price * 100) if side_dir == "YES" else round((1 - price) * 100)
            now_iso = datetime.now(timezone.utc).isoformat()
            why = rules.reject_reason(tk, meta.get("title"), side_dir, entry_c,
                                      eff_close, now_iso,
                                      prior_event_alerts(con, dk))
            if why:
                print("  RULE skip ({}): {} {}@{}c".format(why, meta["title"][:40],
                                                          side_dir, entry_c))
                market_cd[tk] = now
                continue
            label = (meta.get("yes_sub") or "Yes") if side_dir == "YES" else (meta.get("no_sub") or "No")
            link = kalshi_url(series_of(tk), ev)
            rl = "".join("   • " + r + "\n" for r in reasons)

            side_emoji = "\U0001F7E2" if side_dir == "YES" else "\U0001F534"
            if score >= 95:
                heat = "\U0001F525\U0001F525\U0001F525"
            elif score >= 85:
                heat = "\U0001F525\U0001F525"
            else:
                heat = "\U0001F525"

            title_txt = "\U0001F6A8 " + meta["title"][:200]
            body_txt = (
                "{} **Side:** {} ({})  @  **{}¢**\n"
                "\U0001F4E6 **Size:** {:,.0f} contracts\n"
                "{} **Score:** {:.0f}/100  ({} signals)\n"
                "⏱️ **Closes:** {}\n\n"
                "\U0001F4CA **Why flagged:**\n{}\n"
                "\U0001F4C8 [Paper-trade dashboard]({})"
            ).format(side_emoji, side_dir, label, entry_c, size, heat, score, hits,
                     _fmt_close(htc), rl, DASHBOARD_URL)

            print("ALERT {:.0f} {} {}@{}c  {}".format(score, tk, side_dir, entry_c,
                                                      meta["title"][:45]))
            ok = True if dry_run else notify(title_txt, body_txt, link)
            if ok:
                sent_today += 1
                con.execute(
                    "INSERT INTO alerts (ts,ticker,event_ticker,title,side,entry_cents,"
                    "score,reasons,link,contracts,close_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (now_iso, tk, dk, meta["title"], side_dir, entry_c, score,
                     " | ".join(reasons), link, size, eff_close))
                con.commit()
                event_cd[dk] = now
                event_side[dk] = side_dir
            market_cd[tk] = now

        if watch_batch:
            w_title, w_body, w_link = format_watch(watch_batch)
            print("WATCH " + w_title)
            if not dry_run:
                notify(w_title, w_body, w_link)

        if poll % 10 == 1:
            print("poll {}: {} new trades | tracking {} markets | alerts today {}/{}".format(
                poll, fresh, len(base.sizes), sent_today, DAILY_CAP))
        if once:
            break
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--threshold", type=int, default=ALERT_THRESHOLD)
    a = ap.parse_args()
    run(dry_run=a.dry_run, once=a.once, threshold=a.threshold)
