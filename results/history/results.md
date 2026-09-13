# Results: history

_Generated 2026-09-13 06:28 UTC by `report.py`._

## Headline

Over 168.0 h of 60.0s candles, I found 77 no-arbitrage violation episodes before fees across 16 events; 4 were profitable after fees at the best available size (0 after the stale-quote filter). Median lifetime was between 4.0m and 6.0m. Candle data cannot resolve anything faster than a minute, so it says nothing about a sub-second budget.

## Data

|  |  |
|---|---|
| source | 1-minute candles |
| window | 2026-09-06 00:10 UTC → 2026-09-13 00:09 UTC |
| poll interval | 60.0s |
| markets / events watched | 300 / 30 |
| recording sessions | 1 |
| polls (batches) / failed | 9,983 / 0 (0%) |

## Violations before and after fees

An episode is one continuous stretch during which a violation was visible. "After fees" = positive net P&L at the best size the book allowed (history has no depth: 1 or 100 contracts assumed).

| kind | episodes | positive after fees | episodes (filtered) | positive after fees (filtered) |
|---|---|---|---|---|
| yes_no_cross | 0 | 0 | 0 | 0 |
| ladder | 41 | 4 | 10 | 0 |
| set_short | 24 | 0 | 24 | 0 |
| set_long | 12 | 0 | 11 | 0 |
| **total** | 77 | 4 | 45 | 0 |

Stale-quote filter: 24h volume ≥ 100 contracts on every leg, ≥ 5 baskets at the best prices (not applied to history), every leg's spread ≤ 10¢.

## Lifetimes

Lifetimes are intervals, not points: a violation seen at polls t_s..t_e lived between t_e − t_s and t_next − t_prev. Nothing shorter than the 60.0s poll can be resolved.

| group | episodes | events | median lifetime | definitely outlived typical budget | Kaplan–Meier outlived | catch probability (KM) |
|---|---|---|---|---|---|---|
| all | 77 | 16 | 4.0m – 6.0m | 66% | 100% | 100% |
| stale-quote filtered | 45 | 16 | 4.0m – 6.0m | 64% | 100% | 100% |
| positive after fees | 4 | 1 | 10.5m – 12.5m | 100% | 100% | 100% |
| filtered and positive after fees | 0 | 0 | – | – | – | – |

![lifetimes](lifetimes_all.png)
![lifetimes](lifetimes_filtered.png)

## Latency budget

Budget: typical **30.2s**, worst **60.4s** (interval/2 + p50(fetch + compute + order); worst = interval + p95 of each (latency.py)).

## Robustness

### Error bars: episodes are not independent

95% bootstrap intervals on the stale-quote filtered episodes. *Naive* resamples episodes; *cluster* resamples whole events. When the cluster interval is much wider, the naive one was overconfident.

| statistic | point | naive 95% CI | cluster 95% CI (by event) |
|---|---|---|---|
| share positive after fees (best size) | 0% | 0% – 0% | 0% – 0% |
| median lifetime, lower bound (s) | 4.0m | 60.0s – 21.0m | 0ms – 21.0m |
| median lifetime, upper bound (s) | 6.0m | 3.0m – 23.0m | 2.0m – 23.0m |
| share definitely outliving typical budget | 64% | 51% – 78% | 46% – 82% |

### Sensitivity to the filter thresholds

One threshold moves, the others stay at baseline (bold).

| threshold | value | episodes | events | positive after fees | median lifetime | catch probability (KM) |
|---|---|---|---|---|---|---|
| min_volume_24h | 0 | 74 | 16 | 4 | 4.0m – 6.0m | 100% |
| min_volume_24h | 10 | 62 | 16 | 0 | 4.0m – 6.0m | 100% |
| min_volume_24h | **100** | 45 | 16 | 0 | 4.0m – 6.0m | 100% |
| min_volume_24h | 1000 | 40 | 15 | 0 | 3.0m – 5.0m | 100% |
| min_volume_24h | 10000 | 36 | 12 | 0 | 3.0m – 5.0m | 100% |
| min_top_size | 0 | 45 | 16 | 0 | 4.0m – 6.0m | 100% |
| min_top_size | 1 | 45 | 16 | 0 | 4.0m – 6.0m | 100% |
| min_top_size | **5** | 45 | 16 | 0 | 4.0m – 6.0m | 100% |
| min_top_size | 25 | 45 | 16 | 0 | 4.0m – 6.0m | 100% |
| min_top_size | 100 | 45 | 16 | 0 | 4.0m – 6.0m | 100% |
| max_spread | 1 | 47 | 16 | 0 | 3.0m – 5.0m | 100% |
| max_spread | 0.2 | 45 | 16 | 0 | 4.0m – 6.0m | 100% |
| max_spread | **0.1** | 45 | 16 | 0 | 4.0m – 6.0m | 100% |
| max_spread | 0.05 | 43 | 15 | 0 | 4.0m – 6.0m | 100% |
| max_spread | 0.02 | 40 | 14 | 0 | 4.5m – 6.5m | 100% |

## Reproduce

```
python -m mispricing.report --source history --universe data/universe_history.json --series-fees data/series_fees_history.json
```
