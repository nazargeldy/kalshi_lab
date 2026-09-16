# kalshi_lab

Order-flow monitoring, paper trading, and honest backtesting for **Kalshi**
prediction markets. Live on an Oracle VM as the `kalshi-lab` systemd service,
alerts via Telegram, paper account published as a GitHub Pages dashboard.

## Status (2026-09-13): the signal was measured and it carries no information

This project set out to find an edge in "unusual trade activity" on Kalshi —
size shocks, frequency bursts, one-sided taker flow, price momentum. It built a
live monitor, a $1,000 paper account, and a validation suite, then ran the
signal for three weeks and measured it against 609,646 real trades.

**Result: the signal is a coin flip at every horizon.**

| after alert | price went up | price went down | median move |
|---|---|---|---|
| +30 sec | 44% | 44% | 0¢ |
| +5 min | 48% | 47% | 0¢ |
| +30 min | 49% | 50% | 0¢ |
| +4 hours | 42% | 52% | −2¢ |
| settlement | 45% win | 55% lose | −2¢ |

Every way of trading it was tested with time-split validation (tune on the
first half of alerts, evaluate blind on the second). All fail:

| approach | blind out-of-sample ROI | verdict |
|---|---|---|
| Taker at alert price (what ran live) | −13.0% | ✗ |
| Category / score / side filters | in-sample gains vanish out-of-sample | ✗ |
| Maker: rest a bid, earn the spread (`maker_backtest.py`) | −17.4% [−36%, +3%] | ✗ |
| Momentum confirmation: wait 5 min, enter if held (`event_study.py`) | −17.4% [−37%, +3%] | ✗ |

The maker result is the instructive one. Bids were hit 80% of the time; when
hit, the trade won 40%; when not hit, 69%. The market fills you precisely when
you are wrong. Spread capture cannot rescue a quote placed on a coin flip.

**Live paper account:** 352 settled alerts, 45.7% win rate, −7.4% per-trade ROI
(CI [−19.1%, +4.8%]), account −48.8% after the flat-sizing fix (−63% before).
The account was not blown up by one bad bet; it was ground down by a small
negative edge multiplied by high turnover.

### Things that were real (and fixed)

- **Score was anti-predictive** (Spearman ρ = −0.10): the confidence score put
  the *most* money on the *worst* bets. Sizing is now flat. That alone recovered
  14 points of return on the same trades.
- **One-sided taker flow** was the only statistically significant signal — and
  it was negative (−49.7% ROI, CI upper −14%). Disabled 2026-08-28.
- **Range-ladder markets** (`KXBTC` "price range") went 23% / −52.6% ROI. Blocked.
- **Entertainment and Mentions** categories were significant losers. Blocked.
- **US politics is deliberately NOT blocked.** The old Polymarket rule banned it;
  Kalshi's trader base is US persons on a CFTC venue and the (tiny) sample was
  positive. There is no evidence for a ban here.

### Policy v2 (2026-09-17): account frozen, filtered signal under validation

A second look at the full 368-trade record (reconstructed from the dashboard
history) put numbers on where the money went, and the paper trader now
enforces the answers. All rules live in `src/rules.py` and are shared by the
monitor and the paper trader.

| finding | rule |
|---|---|
| Crypto price markets were 52% of trades, 41.7% win, −$535 | blocked (`KXBTC*`, `KXETH*`, ... and title match) |
| Entries under 20¢ went 1 for 23 (4.3% win vs 16% implied), −$369 | entry must be 25–88¢ (was 12–88) |
| 198 trades in 38 markets; 29 markets traded on both sides | one position per event, never the opposite side, no re-entry within 24h, checked against the DB so a restart cannot forget a position |
| $169 of a $385 account locked in bets settling Dec 2026 / Jan 2027 | market must settle within 14 days; `close_ts` is now stored per alert |
| Actual win rate 45.4% vs 47.7% market-implied: no edge to size | **no new positions** until 200 settled eligible alerts beat the implied rate by 4 points (the fee hurdle) |

Alerts before the cutoff replay under the old rules so the historical curve is
not rewritten. Everything after it is still logged and settled; the dashboard's
"Signal Validation" panel shows the running actual-vs-implied test and the gate
opens automatically when it passes.

### What would be different next

Everything above asked *who is trading*. The untested direction is *what is the
true probability*, from a source retail traders do not price precisely — e.g.
weather markets vs. NWS forecast probabilities. Same validation bar applies.

## Layout

```
src/monitor_kalshi.py     live monitor: polls trades, scores, filters, alerts, writes SQLite
src/paper_trader.py       $1,000 paper account replayed from the alert table; renders docs/index.html
src/rules.py              policy v2 entry rules + validation gate, shared by monitor and paper trader
src/notifier.py           Telegram delivery (bot token only, never a user account)
src/setup_telegram.py     one-shot: detects chat id after the user presses Start
src/resolve_alerts.py     settles alerts by exact ticker (Kalshi tickers are unique)
src/kalshi_client.py      RSA-PSS signed client. READ-ONLY - no order placement by design.

src/analyze_alerts.py     live results by side / score / entry / signal / series, bootstrap CIs
src/category_analysis.py  joins Kalshi's own event category onto alerts
src/fetch_tape.py         pulls the real post-alert trade tape for every settled alert
src/maker_backtest.py     maker-side simulation with a strict fill rule + blind validation
src/event_study.py        price path after alert; momentum-confirmation test

src/fetch_history.py      historical market fetch (pre-live research)
src/fetch_by_series.py    series-driven fetch, skips KXMVE auto-generated junk
src/fetch_snapshots.py    pre-resolution price snapshots (lookahead-free)
src/calibration.py        SUPERSEDED - contaminated by lookahead on last_price
src/backtest.py           SUPERSEDED - dollar-weighted ROI flattered results
src/validate.py           equal-weighted ROI, Wilson + bootstrap CIs
src/sweep.py              parameter sweep harness

docs/index.html           paper-trade dashboard (GitHub Pages)
accounts.json             paper account config (key IDs only, never key material)
data/                     gitignored: kalshi_alerts.db (alerts + 609k-trade tape), kalshi.db
```

## Methodology rules (learned the hard way)

- **Equal-weighted ROI**, never dollar-weighted. Dollar-weighting flatters
  strategies with many tiny wins and occasional total losses.
- **Bootstrap CI on every number.** A result is only a result if the interval
  excludes zero.
- **Time-split validation.** Choose parameters on the first half by time, report
  the second half. An in-sample gain that disappears out-of-sample is noise —
  this happened to every filter tried.
- **No lookahead.** `last_price` on a settled market is a tautology. Use
  pre-resolution snapshots or the live tape.
- **Strict fill rule for maker sims.** A resting bid at P only counts as filled
  when a real trade printed *strictly below* P; same-price prints are ignored
  (queue position unknown).
- **Watch for parameter spikes.** A real effect is smooth across neighboring
  settings. One good cell surrounded by bad ones is noise.
- Fee model: taker `ceil(0.07·C·P·(1−P))`, maker `ceil(0.0175·C·P·(1−P))`
  (worst case; Kalshi's default maker multiplier is 0).

## Guardrails

- **No order placement.** The client cannot submit orders. Live trading would be
  a separate, deliberate, human-enabled step — and given the findings, is not
  recommended.
- **Secrets live outside the repo.** Private keys in `~/.kalshi_keys/`,
  Telegram token in `~/.kalshi_keys/telegram.txt`, `.env` gitignored. Key IDs
  (UUIDs) are safe to commit; key material never is.
- **Telegram bot tokens only.** Never a user account. Discord webhooks only,
  never a user token.
- **Sports** blocked (sharp, specialist-dominated). **15-minute / hourly
  direction markets** blocked (structurally unpredictable). **Crypto price
  markets** blocked entirely as of policy v2 (see above).

## Running

```bash
# live monitor (runs as systemd on the VM)
python src/monitor_kalshi.py              # add --dry-run --once to test

# rebuild the paper dashboard from the alert table
python src/paper_trader.py

# analysis
python src/analyze_alerts.py
python src/category_analysis.py --fetch   # first run fetches categories
python src/fetch_tape.py                  # pulls post-alert tape (once)
python src/maker_backtest.py
python src/event_study.py
```

## Operations

- VM service: `kalshi-lab` (the older `kalshi-monitor` unit is the retired
  Polymarket-era codebase — stopped and disabled, do not start it).
- SQLite is in WAL mode; run `PRAGMA wal_checkpoint(TRUNCATE)` before copying
  the DB off the box.
- Daily alert cap is 8; threshold 75. The threshold is deliberately *not*
  raised: the score is anti-predictive, so 75–79 is the best band.
