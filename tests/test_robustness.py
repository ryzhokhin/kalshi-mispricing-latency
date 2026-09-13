"""Filter and bootstrap tests."""
from mispricing.robustness import BASELINE, apply_filter, bootstrap, passes


def ep(event="E", vol=500.0, size=10.0, spread=0.03, lo=1.0, hi=3.0, net=-0.01):
    return {"event_ticker": event, "min_leg_volume_24h": vol, "first_top_size": size,
            "first_max_leg_spread": spread, "lo": lo, "hi": hi, "max_best_net": net, "max_net_1": None}


def test_filter_thresholds():
    assert passes(ep(), BASELINE)
    assert not passes(ep(vol=50), BASELINE)          # traded too little
    assert not passes(ep(size=2), BASELINE)          # fewer than 5 baskets at the best price
    assert not passes(ep(spread=0.25), BASELINE)     # a leg's spread is wider than 10c
    assert not passes(ep(spread=None), BASELINE)     # a leg has an empty side


def test_unknown_depth_is_not_filtered_on_size():
    assert passes(ep(size=None), BASELINE)           # candle history has no sizes


def test_loosest_settings_keep_everything():
    loose = {"min_volume_24h": 0.0, "min_top_size": 0.0, "max_spread": 1.0}
    eps = [ep(vol=0, size=0, spread=None), ep(vol=1e6)]
    assert apply_filter(eps, loose) == eps


def test_single_event_cluster_bootstrap_is_degenerate():
    # 40 episodes all on ONE event: resampling events can only redraw that event,
    # so the share never changes -- while resampling episodes pretends there is variation.
    eps = [ep(net=0.5 if i % 2 else -0.1) for i in range(40)]
    share = lambda s: sum(e["max_best_net"] > 0 for e in s) / len(s)
    lo, hi = bootstrap(eps, share, clusters=True, B=300)
    assert lo == hi == 0.5
    nlo, nhi = bootstrap(eps, share, clusters=False, B=300)
    assert nlo < 0.5 < nhi


def test_clustered_intervals_are_wider_when_events_differ():
    # 5 events; within an event every episode agrees, across events they differ.
    eps = [ep(event=f"E{k}", net=0.5 if k < 2 else -0.1) for k in range(5) for _ in range(20)]
    share = lambda s: sum(e["max_best_net"] > 0 for e in s) / len(s)
    clo, chi = bootstrap(eps, share, clusters=True, B=500)
    nlo, nhi = bootstrap(eps, share, clusters=False, B=500)
    assert (chi - clo) > 2 * (nhi - nlo)
