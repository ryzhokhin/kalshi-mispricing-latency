# Kalshi mispricing detector with a latency budget

How often do Kalshi prediction-market prices break hard no-arbitrage rules, how many of those breaks survive fees and book depth, and do they last long enough for a retail-speed trader to catch them?

The project records live order books for ~300 Kalshi markets, detects every executable violation of logical price constraints, charges Kalshi's fee schedule at the size the book actually offers, measures how long each violation survives, measures the detect-to-order reaction time without placing orders, and replays simulated orders against the recorded books. A local dashboard runs the same pipeline live with alerts.

**Finding.** Violations exist, almost entirely on in-play sports events, but over a 16-hour recording none could have been captured: fees remove nearly all of them, the rest are either too thin to trade or last only while books reprice, and every simulated order left at least one leg unfilled.

**Write-up:** [paper/paper.md](paper/paper.md) — constraints and proofs, fee model, lifetime estimation with censoring, reaction-time decomposition, execution replay and robustness.

## Results

<!-- RESULTS:START -->
Over 16.0 h of 1.0s order-book polling, I found 192 no-arbitrage violation episodes before fees across 8 events; 8 were profitable after fees at the best available size (2 after the stale-quote filter). Median lifetime was between 0ms and 2.3s. Measured reaction time is ~688ms (1.4s worst case); an estimated 88% of violations outlived it. In replay, 0 of 6 fee-positive signals filled both legs, for simulated P&L of $-29.78 ($0.00 counting only fills that were certain).

|  | all | stale-quote filtered |
|---|---|---|
| violation episodes (before fees) | 192 | 112 |
| positive after fees | 8 | 2 |
| median lifetime | 0ms – 2.3s | 0ms – 2.1s |
| reaction time (typical / worst) | 688ms / 1.4s |  |
| catch probability (Kaplan–Meier) | 88% | 87% |
| replay: signals / full fills / simulated P&L | 6 / 0% / $-29.78 |  |

![How long violations survive](results/snapshots/lifetimes_all.png)

Full tables, error bars and sensitivity: [results/snapshots/results.md](results/snapshots/results.md)
<!-- RESULTS:END -->

A second dataset, seven days of one-minute candles with weekday coverage, is analysed in [results/history/results.md](results/history/results.md). It measures how common and how large violations are; it cannot resolve anything faster than a minute.

## What is detected

Every detector checks one condition: a basket of contracts that pays at least *W* in every outcome must not be purchasable for less than *W* after fees. Every leg is a taker buy at the ask; selling YES at the bid is the same trade as buying NO at 1 − bid.

| kind | buy | guaranteed payout |
|---|---|---|
| threshold ladder | YES on "X > K₁", NO on "X > K₂", K₁ < K₂ | 1 |
| short basket | NO on each of n mutually exclusive outcomes | n − 1 |
| long basket | YES on each outcome | 1, only if the set is complete |
| YES/NO cross | YES and NO of one market | 1 (a data-integrity check: the matching engine prevents it) |

Details that make the counts trustworthy:

- **Fees** are `roundup(0.07 × m × C × P(1 − P))`, rounded once per order, with the per-series multiplier read from the API and exact decimal arithmetic.
- **Depth**: legs are walked level by level and the size with the best net P&L is kept.
- **No lookahead**: a long basket is evaluated only once every other market in its event has closed. Applying today's market list to past data would treat a tournament's two finalists as a complete set a week before the final.
- **Ladders** must have one market per strike; sports spreads that list both teams under one strike are excluded.

## Method in brief

1. **Lifetimes as intervals.** Seen from poll *tₛ* to *tₑ*, a violation lived between *tₑ − tₛ* and *t_next − t_prev*. Episodes cut off by the start or end of a recording or by polling gaps are censored, and survival is estimated with Kaplan–Meier.
2. **Reaction time.** Detection lag (uniform over the poll interval) + book fetch + compute + order round trip, each measured. The order probe is an unauthenticated POST that the exchange rejects with 401, a lower bound on a real order. Catch probability integrates the survival curve over the measured distribution.
3. **Execution replay.** Each new fee-positive violation becomes immediate-or-cancel limit buys on every leg, capped at 500 baskets, filled against the next recorded book after a sampled reaction time. Unchanged books make a fill certain; changed books are evaluated pessimistically; unhedged legs are unwound into the bids at a second fee.
4. **Robustness.** A liquidity filter (24h volume, size at the best prices, spread), one-at-a-time threshold sensitivity, and a bootstrap that resamples whole events rather than episodes.

## Live dashboard

```
python -m dashboard.server        # http://127.0.0.1:8050
```

- **Live:** recorder health, open violations with net P&L after fees and liquidity status, and an alert feed with browser notifications and sound. Each alert shows its replay verdict once the simulated orders settle. Click any row for the legs' order books.
- **Lifetimes & replay:** survival curve with the reaction-time budget, reaction-time breakdown, replay outcomes and P&L, and counts by kind.
- **Episodes:** every closed episode with filters and CSV export.
- **7-day history:** the candle study.

On start the dashboard replays the recording so far (a few seconds per hour of data), then follows the recorder. It uses the same detection, episode and replay code as the offline report, and the tests check that both paths produce identical episodes.

## Data

- Kalshi public market-data REST API (`api.elections.kalshi.com/trade-api/v2`), no authentication.
- Order books from the batch endpoint `/markets/orderbooks` (≤ 100 tickers per call), about once per second, top 10 levels per side. Each event's markets share a call so legs are observed at the same instant. Only changed books are stored; restarts are separate sessions.
- One-minute candlesticks (bid/ask OHLC, no sizes) for the history study.
- Raw recordings stay local (`data/`); only changed books are stored and each hour is gzipped, so the 16-hour recording is about 17 MB. `scripts/backup_data.sh` archives them.

## Limitations

- Polling cannot resolve sub-second lifetimes.
- The order round trip is a lower bound; no real order was sent.
- The replay ignores queue priority, competing traders and capital locked until settlement, which makes it optimistic.
- 24-hour volume comes from universe selection and ages over a long recording.
- The live recording covers a weekend, when economic markets are quieter.
- Results rest on the number of distinct events reported next to each statistic, not the number of episodes.

## What I would do with better data or lower latency

- **Authenticated WebSocket feed:** removes polling lag, the largest part of the reaction time.
- **Real order acknowledgements:** measure them on Kalshi's demo exchange.
- **Queue position:** estimate it from trade prints.
- **Cross-venue comparison:** add Polymarket, with contracts matched by resolution criteria.

## Run it

```
pip install -e ".[dev]"
python -m pytest

python -m mispricing.universe --category Economics --category Financials --category Commodities \
       --category Sports --max-markets 300 --max-markets-per-category 100
python -m mispricing.fees --fetch            # per-series fee parameters
python -m mispricing.recorder --hours 16     # live order books
python -m mispricing.latency                 # reaction-time budget, no order placed
python -m mispricing.report --source snapshots

python -m mispricing.history --days 7
python -m mispricing.report --source history

python -m dashboard.server
```

## Layout

| path | role |
|---|---|
| `mispricing/api.py`, `storage.py` | HTTP client; recording readers and live tailer |
| `mispricing/universe.py`, `recorder.py`, `history.py` | data collection |
| `mispricing/fees.py` | fee model |
| `mispricing/detect.py` | violation detectors with depth |
| `mispricing/episodes.py` | lifetimes, censoring, Kaplan–Meier, reaction-time budget, charts |
| `mispricing/latency.py` | reaction-time measurement |
| `mispricing/replay.py` | execution replay |
| `mispricing/robustness.py` | liquidity filter, sensitivity, cluster bootstrap |
| `mispricing/report.py` | end-to-end report; writes `results/`, this README's results and the paper's results |
| `dashboard/` | live engine, HTTP server and web UI |
| `tests/` | fee arithmetic, detectors, episodes, replay, robustness, streaming equivalence |
| `paper/paper.md` | write-up |
