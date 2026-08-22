# kalshi_lab

Backtest-driven trading research for **Kalshi** prediction markets.
Independent of the legacy Polymarket bot (`../kalshi_alerts`, which is Polymarket-only).

## Why this exists

The Polymarket bot's edge came from on-chain transparency — whale wallets, per-trader
win rates, VPIN. **Kalshi is anonymous**, so those signals cannot be ported. Kalshi's
edges are structural instead: calibration/mispricing, maker-side spread capture, and
order-flow (Kalshi *does* expose `taker_side` on trades).

The big advantage: **Kalshi publishes settled history**, so strategies are backtested
against real outcomes instead of waiting weeks for paper trades.

## Layout

```
src/kalshi_client.py    RSA-PSS signed API client (read-only, no order placement)
src/fetch_history.py    raw paginated history fetch + sqlite schema
src/fetch_by_series.py  series-driven fetch (skips auto-generated junk markets)
src/calibration.py      calibration study w/ Wilson CIs + Kalshi fee model
data/kalshi.db          sqlite store
```

## Data notes

- Historical cutoff ~3 months; older data lives on `/trade-api/v2/historical/*`.
- **99.7% of raw paginated markets are auto-generated `KXMVE*` combinatorial
  esports/cross-category legs.** Always exclude them (`series_ticker NOT LIKE 'KXMVE%'`)
  or fetch per-series instead.
- Categories live on *series*/*events*, not markets — join via `series_meta`.
- API uses `_dollars` / `_fp` suffixed fields (e.g. `last_price_dollars`, `volume_fp`).

## Guardrails

- **No order placement.** Client is read-only by design. Live trading is a
  deliberate, separate, human-enabled step.
- Every result reported with sample size + confidence interval + net-of-fee edge.
- Fee model: `ceil(0.07 * C * P * (1-P))` per Kalshi's published formula.

## Usage

```bash
python src/fetch_by_series.py --categories "Mentions,Financials,Crypto" --max-series 500
python src/calibration.py --min-volume 100
```
