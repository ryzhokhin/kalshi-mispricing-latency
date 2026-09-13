# Results: snapshots

_Generated 2026-09-13 21:38 UTC by `report.py`._

## Headline

Over 16.0 h of 1.0s order-book polling, I found 192 no-arbitrage violation episodes before fees across 8 events; 8 were profitable after fees at the best available size (2 after the stale-quote filter). Median lifetime was between 0ms and 2.3s. Measured reaction time is ~688ms (1.4s worst case); an estimated 88% of violations outlived it. In replay, 0 of 6 fee-positive signals filled both legs, for simulated P&L of $-29.78 ($0.00 counting only fills that were certain).

## Data

|  |  |
|---|---|
| source | recorded order books (`data/snapshots`) |
| window | 2026-09-13 05:28 UTC → 2026-09-13 21:28 UTC |
| poll interval | 1.0s |
| markets / events watched | 298 / 21 |
| recording sessions | 1 |
| polls (batches) / failed | 229,132 / 0 (0%) |

## Violations before and after fees

An episode is one continuous stretch during which a violation was visible. "After fees" = positive net P&L at the best size the book allowed.

| kind | episodes | positive after fees | episodes (filtered) | positive after fees (filtered) |
|---|---|---|---|---|
| yes_no_cross | 0 | 0 | 0 | 0 |
| ladder | 10 | 1 | 3 | 0 |
| set_short | 66 | 1 | 42 | 0 |
| set_long | 116 | 6 | 67 | 2 |
| **total** | 192 | 8 | 112 | 2 |

Stale-quote filter: 24h volume ≥ 100 contracts on every leg, ≥ 5 baskets at the best prices, every leg's spread ≤ 10¢.

## Lifetimes

Lifetimes are intervals, not points: a violation seen at polls t_s..t_e lived between t_e − t_s and t_next − t_prev. Nothing shorter than the 1.0s poll can be resolved.

| group | episodes | events | median lifetime | definitely outlived typical budget | Kaplan–Meier outlived | catch probability (KM) |
|---|---|---|---|---|---|---|
| all | 192 | 8 | 0ms – 2.3s | 49% | 100% | 88% |
| stale-quote filtered | 112 | 6 | 0ms – 2.1s | 44% | 100% | 87% |
| positive after fees | 8 | 4 | 937ms – 3.0s | 62% | 100% | 91% |
| filtered and positive after fees | 2 | 2 | 0ms – 2.1s | 0% | 100% | 82% |

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
| signals | 6 | 2 |
| full fill rate (both legs, full size) | 0% | 0% |
| leg-risk incidents (one leg short) | 6 | 2 |
| P&L the detector expected | $4.59 | $0.47 |
| simulated P&L | $-29.78 | $-21.64 |
| simulated P&L, certain fills only | $0.00 | $0.00 |
| contracts written off (no bid to unwind) | 500.00 | 0.00 |

| outcome | count |
|---|---|
| legged (book changed) | 4 |
| partial (book changed) | 2 |

## Robustness

### Error bars: episodes are not independent

95% bootstrap intervals on the stale-quote filtered episodes. *Naive* resamples episodes; *cluster* resamples whole events. When the cluster interval is much wider, the naive one was overconfident. **Only 6 events: with fewer than ~10 clusters the bootstrap itself is unreliable (it can even come out narrower than the naive interval); treat every interval here as indicative.**

| statistic | point | naive 95% CI | cluster 95% CI (by event) |
|---|---|---|---|
| share positive after fees (best size) | 2% | 0% – 4% | 0% – 3% |
| median lifetime, lower bound (s) | 0ms | 0ms – 887ms | 0ms – 2.1s |
| median lifetime, upper bound (s) | 2.1s | 2.1s – 2.9s | 2.1s – 4.1s |
| share definitely outliving typical budget | 44% | 35% – 53% | 33% – 70% |

### Sensitivity to the filter thresholds

One threshold moves, the others stay at baseline (bold).

| threshold | value | episodes | events | positive after fees | median lifetime | catch probability (KM) |
|---|---|---|---|---|---|---|
| min_volume_24h | 0 | 114 | 7 | 2 | 0ms – 2.1s | 87% |
| min_volume_24h | 10 | 112 | 6 | 2 | 0ms – 2.1s | 87% |
| min_volume_24h | **100** | 112 | 6 | 2 | 0ms – 2.1s | 87% |
| min_volume_24h | 1000 | 110 | 6 | 2 | 0ms – 2.1s | 87% |
| min_volume_24h | 10000 | 109 | 5 | 2 | 0ms – 2.1s | 87% |
| min_top_size | 0 | 185 | 7 | 5 | 0ms – 2.2s | 88% |
| min_top_size | 1 | 135 | 7 | 2 | 0ms – 2.1s | 87% |
| min_top_size | **5** | 112 | 6 | 2 | 0ms – 2.1s | 87% |
| min_top_size | 25 | 81 | 6 | 2 | 0ms – 2.2s | 88% |
| min_top_size | 100 | 52 | 5 | 1 | 0ms – 2.2s | 88% |
| max_spread | 1 | 115 | 7 | 4 | 0ms – 2.1s | 87% |
| max_spread | 0.2 | 113 | 6 | 2 | 0ms – 2.1s | 87% |
| max_spread | **0.1** | 112 | 6 | 2 | 0ms – 2.1s | 87% |
| max_spread | 0.05 | 112 | 6 | 2 | 0ms – 2.1s | 87% |
| max_spread | 0.02 | 106 | 6 | 2 | 0ms – 2.1s | 87% |

### Sensitivity to the episode gap rule (filtered)

| max gap (poll intervals) | episodes | median lifetime | catch probability (KM) |
|---|---|---|---|
| 2 | 112 | 0ms – 2.1s | 87% |
| **3** | 112 | 0ms – 2.1s | 87% |
| 5 | 112 | 0ms – 2.1s | 87% |

## Reproduce

```
python -m mispricing.report --source snapshots
```
