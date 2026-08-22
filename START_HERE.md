# Kalshi Lab — Project Kickoff Prompt

> Paste everything below the line into a fresh Claude Code session started in
> `C:\Users\geldy\kalshi_lab`.

---

## MISSION

Build **`kalshi_lab`** — a standalone, backtest-driven trading research system for
**Kalshi** prediction markets. It must be completely independent from my existing
Polymarket bot. I want to iteratively find and validate a *real, measured* edge on
Kalshi using historical data, and improve it every cycle via backtesting.

## CONTEXT: WHAT ALREADY EXISTS (DO NOT MODIFY)

I run a working Polymarket alert bot at `C:\Users\geldy\kalshi_alerts\`
(despite the name, **it is 100% Polymarket**). It is live on an Oracle VM
(`ubuntu@<oracle-vm-ip>`, systemd service `kalshi-monitor`) and validated at a
~55-59% signal win rate over ~40 settled trades.

**That project is off-limits.** Do not edit, deploy to, or restart anything in
`kalshi_alerts/` or on the Oracle VM. You may **read** it for reference —
`scoring.py`, `alert_manager.py`, `auto_resolve.py`, and `paper_trader.py`
(in `C:\Users\geldy\paper_trader\`) contain patterns worth reusing.

The two tracks stay separate: **Polymarket keeps running as-is; Kalshi is a new,
clean build with its own data and its own edge.**

## THE SINGLE MOST IMPORTANT CONSTRAINT

**Do NOT port the Polymarket strategy to Kalshi.** The Polymarket bot's edge comes
from on-chain transparency — whale wallet tracking, per-trader win rates ("elite
predictor"), and VPIN order-flow imbalance. Polymarket is on-chain so every wallet
and its history is public.

**Kalshi is anonymous and regulated.** There are no wallets, no per-trader
identities, no trader history. Those signals *cannot exist* on Kalshi. Any plan
that assumes them is wrong.

Kalshi edges are structurally different and must be found empirically:
- **Calibration / mispricing** (e.g. favorite-longshot bias)
- **Maker-side edge** (earning spread, NO-side entries)
- **Settlement-lag and stale-quote plays**
- **Cross-venue price gaps**

## KALSHI HAS REAL HISTORICAL DATA — THIS IS THE CORE ADVANTAGE

Unlike the Polymarket project (where we waited days per data point), Kalshi exposes
settled history, so **backtesting is possible from day one**:

- `GET /historical/trades` — trade-level history
- `GET /historical/markets` and `/historical/markets/{ticker}` — settled markets + outcomes
- `GET /historical/markets/{ticker}/candlesticks` — OHLC candles
- `GET /historical/cutoff` — live/historical boundary timestamps
- Live window is ~3 months, then data auto-archives to the historical endpoints
- Cursor-based pagination, same as live endpoints

Docs: https://docs.kalshi.com/getting_started/historical_data

**Build the backtest loop first.** Every strategy claim must be measured against
real settled outcomes with fees and spread modeled — never on vibes.

## PRIOR ART — BUILD ON TOP OF THESE, DON'T REINVENT

Research these first and reuse what's good (check licenses; `homerun` is AGPL-3.0):

**Backtesting / platforms**
- https://github.com/braedonsaunders/homerun — Kalshi+Polymarket platform, L2 book
  replay, realistic fill modeling, shadow-vs-live triangulation, 25+ strategies. Active.
- https://github.com/Oddpool/PredictionMarketBench — benchmark on real Kalshi replay data
- https://github.com/Quentin-Piot/prediction-market-backtester — quant-style engine
- https://github.com/nikhilnd/kalshi-market-making — USC QuantSC market-making sim

**Edge research (START HERE — most valuable)**
- https://github.com/DanMcInerney/kalshi-analysis — calibration study of Kalshi after
  spreads and fees. Reported edges: Financials 0.8%, Sports 2.4%, Crypto 3.3%, and
  **"Mentions" markets 15.0%** — with *persistent positive edge for NO-side maker
  entries* across price buckets and time periods.

**API clients**
- https://github.com/pmxt-dev/pmxt — "CCXT for prediction markets", unified API
  (`pmxt.Kalshi()` / `pmxt.Polymarket()`), supports real execution
- https://github.com/arshka/pykalshi — orderbook state from WS deltas, candlesticks
- https://github.com/TexasCoding/kalshi-python-sdk — full REST (104 ops) + WS coverage
- https://github.com/the-odds-company/aiokalshi — asyncio-native client

## FIRST HYPOTHESIS TO TEST

Reproduce the `kalshi-analysis` calibration study **on current data** and determine
whether the **"Mentions" NO-side maker edge is still live**. Treat their numbers as a
hypothesis to validate, not fact — historical miscalibration often disappears once
known. If it's gone, run the calibration sweep across all categories and find what
*is* mispriced now.

## HARD-WON LESSONS FROM THE POLYMARKET BUILD (avoid these bugs)

These cost me weeks. Design against them from the start:

1. **Resolution correctness is everything.** My resolver naively read `markets[0]` of
   a multi-market event, so price-ladder and date-ladder sub-markets resolved against
   the *wrong* contract — **44% of outcomes were wrong**, and the dashboard showed
   fake profits for weeks. Always match the exact contract; if you can't confidently
   match, leave it unresolved rather than guessing.
2. **Correlated bets destroy accounts.** One FOMC meeting resolved against ~25
   correlated Fed sub-markets simultaneously → **-52% drawdown**. Enforce per-event
   and per-theme exposure caps, not just per-market.
3. **Never bet both sides of one event.** Following flow made the bot buy NO then YES
   on the same market hours apart, losing to whipsaw + fees.
4. **Small samples lie.** 34 trades cannot distinguish a real 55% edge from luck.
   Report confidence intervals; do not celebrate early returns.
5. **Model cash properly.** Track cash vs. open exposure so the sim can't deploy money
   it doesn't have; cap per-bet size; account for fees and slippage.
6. **Beware capital lock-up.** Long-dated markets (2028 elections, year-end Fed) froze
   my whole bankroll for months. Prefer short-horizon markets or cap long-dated exposure.
7. **Paper results are optimistic.** Assume slippage, partial fills, and thin books —
   especially on the exact spikes a signal chases.

## CREDENTIALS

All secrets already exist. **Copy the env file from the existing project rather than
retyping secrets** (it's gitignored and stays out of version control):

```bash
copy C:\Users\geldy\kalshi_alerts\.env C:\Users\geldy\kalshi_lab\.env
```

That file contains the working values for:

| Variable | Purpose |
|---|---|
| `KALSHI_ENV` | `prod` (REST `https://api.elections.kalshi.com`, WS `wss://api.elections.kalshi.com/trade-api/ws/v2`) |
| `KALSHI_KEY_ID` | Kalshi API key id |
| `KALSHI_PRIVATE_KEY_PATH` | points to `C:\Users\geldy\kalshi_alerts\kalshi_priv.pem` — **copy this .pem into the new folder and update the path** |
| `TELEGRAM_BOT_TOKEN` | same bot that sends my alerts |
| `TELEGRAM_CHAT_ID` | my chat id |
| `NOTION_TOKEN` / `NOTION_DB_ID` | existing alert ledger (Polymarket) — reference only; **create a separate Notion DB or local SQLite for Kalshi** |

Kalshi auth uses **RSA request signing** (key id + private key signing
`timestamp + method + path`), not a bearer token — see `kalshi_alerts/monitor.py`
for a working implementation to copy.

Add a `.gitignore` covering `.env`, `*.pem`, `*.key`, `*.db` before the first commit.

## GUARDRAILS — NON-NEGOTIABLE

- **No real-money trading. No order placement.** Execution code must be dry-run /
  stubbed. I will wire and enable live trading myself, later, deliberately.
- **Do not touch** `kalshi_alerts/`, `paper_trader/`, or the Oracle VM.
- Any human-in-the-loop approval flow must use Telegram inline buttons
  (Approve/Decline), only accept my chat id, and auto-skip on timeout.
  A working reference implementation is at
  `C:\Users\geldy\kalshi_alerts\trade_approval_test.py`.
- Verify claims against real data before reporting them. If a number looks too good,
  independently re-check it before I see it.

## DELIVERABLES (in order)

1. **Scaffold** `kalshi_lab/` — venv, `.gitignore`, `.env` wired, config module.
2. **Kalshi API client** — auth working; smoke test hitting a live endpoint.
   Prefer an existing library (`pykalshi` / `kalshi-python-sdk` / `pmxt`) over
   hand-rolling; justify the choice.
3. **Historical data fetcher** — pull settled markets, trades, and candlesticks into
   local SQLite/Parquet, resumable and rate-limit aware.
4. **Backtest engine** — replay settled markets with fees + spread + realistic fills.
   Evaluate `homerun` first; only build custom if it doesn't fit.
5. **Calibration analysis** — measure predicted-probability vs. realized-frequency by
   category and price bucket. Find where Kalshi is mispriced *today*.
6. **Strategy candidates** — implement, backtest, report with sample size and
   confidence intervals. Kill anything that doesn't beat fees.
7. **Paper trading loop** — forward-test the best strategy on live Kalshi data,
   dry-run only, with a dashboard.

## HOW I WANT YOU TO WORK

- Start by **reading** `kalshi_alerts/scoring.py`, `alert_manager.py`,
  `auto_resolve.py`, and `paper_trader/paper_trader.py` to understand what I built
  and what to reuse conceptually.
- Then research the prior-art repos above before writing code.
- Be skeptical and empirical. Tell me when a result is statistically meaningless,
  when an edge is likely arbitraged away, and when you're uncertain.
- Correct me when I'm wrong — I'd rather hear it early than lose money.

**Begin with deliverables 1 and 2, then report back before proceeding.**
