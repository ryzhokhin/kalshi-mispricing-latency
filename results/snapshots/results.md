# Results: snapshots

_Generated 2026-09-13 06:28 UTC by `report.py`._

## Headline

Over 1.0 h of 1.0s order-book polling, I found 50 no-arbitrage violation episodes before fees across 4 events; 4 were profitable after fees at the best available size (0 after the stale-quote filter). Median lifetime was between 919ms and 2.9s. Measured reaction time is ~688ms (1.4s worst case); an estimated 89% of violations outlived it. In replay, 0 of 2 fee-positive signals filled both legs, for simulated P&L of $-2.06 ($0.00 counting only fills that were certain).

## Data

|  |  |
|---|---|
| source | recorded order books (`data/snapshots`) |
| window | 2026-09-13 05:28 UTC → 2026-09-13 06:28 UTC |
| poll interval | 1.0s |
| markets / events watched | 298 / 21 |
| recording sessions | 1 |
| polls (batches) / failed | 14,384 / 0 (0%) |

## Violations before and after fees

An episode is one continuous stretch during which a violation was visible. "After fees" = positive net P&L at the best size the book allowed.

| kind | episodes | positive after fees | episodes (filtered) | positive after fees (filtered) |
|---|---|---|---|---|
| yes_no_cross | 0 | 0 | 0 | 0 |
| ladder | 1 | 0 | 0 | 0 |
| set_short | 25 | 0 | 13 | 0 |
| set_long | 24 | 4 | 5 | 0 |
| **total** | 50 | 4 | 18 | 0 |

Stale-quote filter: 24h volume ≥ 100 contracts on every leg, ≥ 5 baskets at the best prices, every leg's spread ≤ 10¢.

## Lifetimes

Lifetimes are intervals, not points: a violation seen at polls t_s..t_e lived between t_e − t_s and t_next − t_prev. Nothing shorter than the 1.0s poll can be resolved.

| group | episodes | events | median lifetime | definitely outlived typical budget | Kaplan–Meier outlived | catch probability (KM) |
|---|---|---|---|---|---|---|
| all | 50 | 4 | 919ms – 2.9s | 56% | 100% | 89% |
| stale-quote filtered | 18 | 3 | 0ms – 2.1s | 44% | 100% | 87% |
| positive after fees | 4 | 1 | 1.5s – 3.6s | 100% | 100% | 100% |
| filtered and positive after fees | 0 | 0 | – | – | – | – |

![lifetimes](lifetimes_all.png)
![lifetimes](lifetimes_filtered.png)

## Latency budget

Budget: typical **688ms**, worst **1.4s** (interval/2 + p50(fetch + compute + order); worst = interval + p95 of each (latency.py)).

| component | p50 | p95 |
|---|---|---|
| detection lag (Uniform over the poll interval) | 500ms | 1.0s |
| fetch order books (warm GET, ~100 tickers) | 102ms | 201ms |
| compute (quotes + detectors, one batch) | 2ms | 4ms |
| order round trip (unauthenticated POST, rejected 401 — a lower bound) | 88ms | 197ms |
| TCP connect (RTT to the CDN edge, not the exchange) | 15ms | 95ms |

Measured 2026-09-13T03:24:54Z via CDN edge LAX54-P12. No order was ever placed.

## Execution replay

Each new fee-positive violation becomes IOC limit buys on every leg, landing after a reaction time resampled from the latency measurements, and fills against the next recorded book. Unchanged books make a fill certain; changed books are evaluated pessimistically. Size is capped at 500 baskets per signal. Queue priority and competing traders are not modeled, which makes this optimistic.

|  | all signals | stale-quote filtered |
|---|---|---|
| signals | 2 | 0 |
| full fill rate (both legs, full size) | 0% | – |
| leg-risk incidents (one leg short) | 2 | 0 |
| P&L the detector expected | $0.12 | $0.00 |
| simulated P&L | $-2.06 | $0.00 |
| simulated P&L, certain fills only | $0.00 | $0.00 |
| contracts written off (no bid to unwind) | 0.00 | 0 |

| outcome | count |
|---|---|
| legged (book changed) | 1 |
| partial (book changed) | 1 |

## Robustness

### Error bars: episodes are not independent

95% bootstrap intervals on the stale-quote filtered episodes. *Naive* resamples episodes; *cluster* resamples whole events. When the cluster interval is much wider, the naive one was overconfident. **Only 3 events: with fewer than ~10 clusters the bootstrap itself is unreliable (it can even come out narrower than the naive interval); treat every interval here as indicative.**

| statistic | point | naive 95% CI | cluster 95% CI (by event) |
|---|---|---|---|
| share positive after fees (best size) | 0% | 0% – 0% | 0% – 0% |
| median lifetime, lower bound (s) | 0ms | 0ms – 1.0s | 0ms – 849ms |
| median lifetime, upper bound (s) | 2.1s | 2.0s – 3.1s | 2.1s – 2.9s |
| share definitely outliving typical budget | 44% | 22% – 67% | 33% – 60% |

### Sensitivity to the filter thresholds

One threshold moves, the others stay at baseline (bold).

| threshold | value | episodes | events | positive after fees | median lifetime | catch probability (KM) |
|---|---|---|---|---|---|---|
| min_volume_24h | 0 | 18 | 3 | 0 | 0ms – 2.1s | 87% |
| min_volume_24h | 10 | 18 | 3 | 0 | 0ms – 2.1s | 87% |
| min_volume_24h | **100** | 18 | 3 | 0 | 0ms – 2.1s | 87% |
| min_volume_24h | 1000 | 18 | 3 | 0 | 0ms – 2.1s | 87% |
| min_volume_24h | 10000 | 18 | 3 | 0 | 0ms – 2.1s | 87% |
| min_top_size | 0 | 48 | 4 | 3 | 927ms – 2.9s | 89% |
| min_top_size | 1 | 27 | 4 | 0 | 0ms – 2.2s | 86% |
| min_top_size | **5** | 18 | 3 | 0 | 0ms – 2.1s | 87% |
| min_top_size | 25 | 12 | 3 | 0 | 425ms – 2.5s | 89% |
| min_top_size | 100 | 5 | 3 | 0 | 0ms – 2.2s | 87% |
| max_spread | 1 | 19 | 3 | 0 | 0ms – 2.2s | 87% |
| max_spread | 0.2 | 19 | 3 | 0 | 0ms – 2.2s | 87% |
| max_spread | **0.1** | 18 | 3 | 0 | 0ms – 2.1s | 87% |
| max_spread | 0.05 | 18 | 3 | 0 | 0ms – 2.1s | 87% |
| max_spread | 0.02 | 17 | 3 | 0 | 0ms – 2.2s | 87% |

### Sensitivity to the episode gap rule (filtered)

| max gap (poll intervals) | episodes | median lifetime | catch probability (KM) |
|---|---|---|---|
| 2 | 18 | 0ms – 2.1s | 87% |
| **3** | 18 | 0ms – 2.1s | 87% |
| 5 | 18 | 0ms – 2.1s | 87% |

## Reproduce

```
python -m mispricing.report --source snapshots
```
