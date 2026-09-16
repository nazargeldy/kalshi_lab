"""
Entry rules shared by the live monitor and the paper trader.

Both sides import from here so an alert can never be admitted by one and
rejected by the other. Every rule below is a direct response to a measured
loss in the live paper account (368 settled trades, Aug 23 - Sep 16 2026):

  crypto price markets   192 trades (52% of volume), 41.7% win, -$535.
                         Daily BTC/ETH strikes are a random walk over a few
                         hours; the market price already IS the forecast.
  entry under 25c        <20c bucket went 1 for 23 (4.3% win vs 16% implied),
                         -$369. Classic favourite-longshot bias: cheap YES
                         tickets are overpriced because retail likes lottery
                         tickets, and our flow signal follows retail.
  same event re-entry    198 trades landed in only 38 markets; 29 markets were
                         traded on BOTH sides. Whipsaw plus a fee on every leg.
  long-dated markets     $169 of a $385 account sat in bets settling Dec 2026
                         and Jan 2027 with no exit. Capital lock-up.

Rules apply to alerts at or after POLICY_FROM. Earlier alerts are replayed
under the old rules so the historical curve stays honest - retroactively
filtering history is exactly the in-sample trap the README warns about.
"""
from datetime import datetime, timezone, timedelta

# When the new policy took effect. Alerts before this are replayed unchanged.
POLICY_FROM = "2026-09-17T00:00:00+00:00"

# Crypto price markets: blocked by series prefix (reliable) and by title as a
# backstop for any series we have not seen yet.
CRYPTO_SERIES_PREFIXES = ("KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE", "KXLTC",
                          "KXBNB", "KXADA", "KXAVAX", "KXLINK", "KXCRYPTO")
# Title backstop needs BOTH a coin name and a price word, so "Will a crypto
# bill reach the Senate floor" or "Will the US create a Bitcoin reserve" are
# NOT blocked - only price-of-coin markets are.
CRYPTO_TITLE_HINTS = ("bitcoin", "ethereum", "solana", "dogecoin", "xrp", "litecoin",
                      "btc", "eth price", "sol price")
CRYPTO_PRICE_HINTS = ("price", "above", "below", "reach $", "hit $", "$")

MIN_ENTRY_CENTS = 25
MAX_ENTRY_CENTS = 88

# Never enter a market that settles more than this far out.
MAX_DAYS_TO_CLOSE = 14

# One position per event: no second entry on an event we already hold, never
# the opposite side of an event we have ever taken, and no re-entry within
# this many hours of the last alert on that event.
EVENT_REENTRY_HOURS = 24

# Signal validation gate. The paper account opens NO new positions until the
# filtered signal, measured forward from POLICY_FROM, has this many settled
# alerts AND beats the market-implied win rate by at least the fee hurdle.
# 4 points is roughly what taker fees cost at these prices (0.07 * (1 - p)
# of stake, so 4-5% at 30-45c).
VALIDATION_MIN_N = 200
VALIDATION_FEE_HURDLE = 0.04


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def series_of(ticker):
    return (ticker or "").split("-")[0].upper()


def is_crypto(ticker, title=""):
    if series_of(ticker).startswith(CRYPTO_SERIES_PREFIXES):
        return True
    tl = (title or "").lower()
    return any(c in tl for c in CRYPTO_TITLE_HINTS) and any(p in tl for p in CRYPTO_PRICE_HINTS)


def hours_to_close(close_ts, at_ts):
    ct, at = parse_ts(close_ts), parse_ts(at_ts)
    if ct is None or at is None:
        return None
    return (ct - at).total_seconds() / 3600.0


def is_policy_v2(ts):
    t = parse_ts(ts)
    return t is not None and t >= parse_ts(POLICY_FROM)


def reject_reason(ticker, title, side, entry_c, close_ts, at_ts, prior_event_alerts):
    """Return None if the alert may be entered, else a short reason string.

    prior_event_alerts: iterable of (ts, side, result) for every earlier alert
    on the same event, whatever their outcome.
    """
    if is_crypto(ticker, title):
        return "crypto price market"
    if entry_c is None or not (MIN_ENTRY_CENTS <= entry_c <= MAX_ENTRY_CENTS):
        return "entry outside {}-{}c".format(MIN_ENTRY_CENTS, MAX_ENTRY_CENTS)
    htc = hours_to_close(close_ts, at_ts)
    if htc is None:
        return "close time unknown"
    if htc > MAX_DAYS_TO_CLOSE * 24:
        return "settles >{}d out".format(MAX_DAYS_TO_CLOSE)
    now = parse_ts(at_ts)
    for pts, pside, presult in prior_event_alerts:
        if presult not in ("WIN", "LOSS"):
            return "already in this event"
        if pside != side:
            return "opposite side already taken on this event"
        pt = parse_ts(pts)
        if pt and now and now - pt < timedelta(hours=EVENT_REENTRY_HOURS):
            return "re-entry within {}h".format(EVENT_REENTRY_HOURS)
    return None


def wilson(k, n, z=1.96):
    if not n:
        return (0.0, 0.0, 0.0)
    import math
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (p, max(0.0, c - h), min(1.0, c + h))


def validation(settled_eligible):
    """settled_eligible: iterable of (entry_cents, won: bool) for eligible
    alerts that have settled. Returns a dict with the gate decision."""
    rows = list(settled_eligible)
    n = len(rows)
    wins = sum(1 for _, w in rows if w)
    implied = (sum(e for e, _ in rows) / n / 100.0) if n else 0.0
    actual, lo, hi = wilson(wins, n)
    gap = actual - implied
    passed = n >= VALIDATION_MIN_N and gap >= VALIDATION_FEE_HURDLE
    return {"n": n, "wins": wins, "actual": actual, "lo": lo, "hi": hi,
            "implied": implied, "gap": gap, "hurdle": VALIDATION_FEE_HURDLE,
            "min_n": VALIDATION_MIN_N, "open": passed}
