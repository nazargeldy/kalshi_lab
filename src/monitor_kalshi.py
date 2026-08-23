"""
Kalshi-only unusual-activity monitor -> Telegram.

100% Kalshi. No Polymarket anywhere. Alerts link to kalshi.com.

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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "kalshi_alerts.db")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

ALERT_THRESHOLD = 75
DAILY_CAP = 20
MARKET_COOLDOWN = 3600
EVENT_COOLDOWN = 4 * 3600
SIDE_FLIP_WINDOW = 24 * 3600
POLL_SEC = 30
BASELINE_MIN_TRADES = 8

JUNK_PREFIXES = ("KXMVE",)
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
  link TEXT, contracts REAL, result TEXT, settled_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ticker ON alerts(ticker);
"""


def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


def series_of(t):
    return (t or "").split("-")[0]


def is_junk(t):
    return any(series_of(t).startswith(j) for j in JUNK_PREFIXES)


def is_sports(t):
    s = series_of(t).upper()
    return any(h in s for h in SPORTS_HINTS)


def is_direction(ticker, title=""):
    s = series_of(ticker).upper()
    if any(h in s for h in DIRECTION_HINTS):
        return True
    tl = (title or "").lower()
    return any(h in tl for h in DIRECTION_TITLE)


def kalshi_url(series, event):
    if series and event:
        return "https://kalshi.com/markets/" + series.lower() + "/" + event.lower()
    return "https://kalshi.com/markets"


def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[telegram not configured]")
        return False
    try:
        r = requests.post(
            "https://api.telegram.org/bot" + TG_TOKEN + "/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15)
        if not r.ok:
            print("telegram HTTP " + str(r.status_code) + ": " + r.text[:160])
        return r.ok
    except Exception as e:
        print("telegram error: " + str(e))
        return False


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

        fl = list(self.flow[tk])
        if len(fl) >= 12:
            imb = abs(sum(fl)) / len(fl)
            side = "YES" if sum(fl) > 0 else "NO"
            if imb >= 0.80:
                s += 22; hits += 1
                reasons.append("Extreme one-sided flow ({:.0%} {} takers)".format(imb, side))
            elif imb >= 0.60:
                s += 13; hits += 1
                reasons.append("Heavy one-sided flow ({:.0%} {} takers)".format(imb, side))

        pr = list(self.prices[tk])
        if len(pr) >= 10:
            delta = (pr[-1] - pr[0]) * 100
            if abs(delta) >= 15:
                s += 20; hits += 1; reasons.append("Price moved {:+.0f}c".format(delta))
            elif abs(delta) >= 8:
                s += 12; hits += 1; reasons.append("Price moved {:+.0f}c".format(delta))

        if hours_to_close is not None:
            if hours_to_close <= 2:
                s += 18; reasons.append("Closes within 2 hours")
            elif hours_to_close <= 24:
                s += 15; reasons.append("Closes within 24 hours")
            elif hours_to_close <= 72:
                s += 10; reasons.append("Closes within 3 days")

        # Lesson from the last project: cheap longshots lose badly after fees.
        cents = price * 100
        if cents <= 10 or cents >= 90:
            s -= 12
            reasons.append("Longshot penalty ({:.0f}c)".format(cents))
        elif cents <= 20 or cents >= 80:
            s -= 4

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
    if not dry_run:
        tg_send("\U0001F7E2 <b>Kalshi monitor online</b>\nWatching Kalshi markets only.")

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
        for t in reversed(new):
            tk = t.get("ticker")
            if not tk or is_junk(tk) or is_sports(tk) or is_direction(tk):
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
                    market_meta[tk] = {
                        "title": m.get("title") or tk,
                        "event": m.get("event_ticker"),
                        "close": m.get("close_time"),
                        "yes_sub": m.get("yes_sub_title"),
                        "no_sub": m.get("no_sub_title"),
                    }
                except Exception:
                    market_meta[tk] = {"title": tk, "event": None, "close": None,
                                       "yes_sub": None, "no_sub": None}
            meta = market_meta[tk]

            htc = None
            if meta.get("close"):
                try:
                    ct = datetime.fromisoformat(meta["close"].replace("Z", "+00:00"))
                    htc = (ct - datetime.now(timezone.utc)).total_seconds() / 3600.0
                    if htc < 0:
                        continue
                except Exception:
                    pass

            if is_direction(tk, meta.get("title")):
                continue

            score, reasons, hits = base.score(tk, size, price, htc)
            if score < threshold:
                continue
            if now - market_cd.get(tk, 0) < MARKET_COOLDOWN:
                continue

            ev = meta.get("event") or tk
            if now - event_cd.get(ev, 0) < EVENT_COOLDOWN:
                print("  EVENT-dedup skip: " + meta["title"][:50])
                market_cd[tk] = now
                continue

            fl = list(base.flow[tk])
            side_dir = "YES" if sum(fl) > 0 else "NO"
            prev = event_side.get(ev)
            if prev and prev != side_dir and now - event_cd.get(ev, 0) < SIDE_FLIP_WINDOW:
                print("  SIDE-FLIP skip: " + meta["title"][:45])
                market_cd[tk] = now
                continue

            if sent_today >= DAILY_CAP:
                print("  daily cap reached")
                continue

            entry_c = round(price * 100) if side_dir == "YES" else round((1 - price) * 100)
            label = (meta.get("yes_sub") or "Yes") if side_dir == "YES" else (meta.get("no_sub") or "No")
            link = kalshi_url(series_of(tk), ev)
            rl = "".join("  • " + r + "\n" for r in reasons)
            msg = ("\U0001F6A8 <b>Unusual Activity — Kalshi</b>\n\n"
                   "❓ <b>" + meta["title"][:150] + "</b>\n\n"
                   "\U0001F4CC <b>Side:</b> " + side_dir + " (" + str(label) + ")  @ "
                   + str(entry_c) + "¢\n"
                   "\U0001F4E6 Trade size: {:,.0f} contracts\n".format(size) +
                   "⚡ Score: {:.0f}/100  ({} signals)\n\n".format(score, hits) +
                   "\U0001F4C8 <b>Why flagged:</b>\n" + rl + "\n"
                   "\U0001F517 <a href='" + link + "'>Trade on Kalshi</a>")

            print("ALERT {:.0f} {} {}@{}c  {}".format(score, tk, side_dir, entry_c,
                                                      meta["title"][:45]))
            ok = True if dry_run else tg_send(msg)
            if ok:
                sent_today += 1
                con.execute(
                    "INSERT INTO alerts (ts,ticker,event_ticker,title,side,entry_cents,"
                    "score,reasons,link,contracts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(), tk, ev, meta["title"],
                     side_dir, entry_c, score, " | ".join(reasons), link, size))
                con.commit()
                event_cd[ev] = now
                event_side[ev] = side_dir
            market_cd[tk] = now

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
