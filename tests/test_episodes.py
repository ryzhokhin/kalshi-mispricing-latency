"""Episode and lifetime tests on hand-built timelines. Expected values worked by hand in the comments."""
import pytest

from mispricing.episodes import budgets, build_episodes, capture_share, kaplan_meier, km_at, outlived, quantile, wilson

ROW = {"gross_top": "0.02", "net_1": "-0.01", "best_net": "-0.01", "min_leg_volume_24h": "10"}


def timeline(times, session="A"):
    return [(session, i, float(t)) for i, t in enumerate(times)]


def present_at(obs, times):
    return {o[:2]: ROW for o in obs if o[2] in times}


def test_bounds_of_an_interior_episode():
    # polls at 0..9 s, violation seen at 3, 4, 5: started in (2, 3], ended in [5, 6)
    # lo = 5 - 3 = 2,  hi = 6 - 2 = 4
    obs = timeline(range(10))
    [e] = build_episodes(obs, present_at(obs, {3, 4, 5}))
    assert (e["lo"], e["hi"], e["n_obs"]) == (2, 4, 3)
    assert not e["left_censored"] and not e["right_censored"]


def test_single_snapshot_lifetime_is_between_zero_and_two_intervals():
    obs = timeline(range(10))
    [e] = build_episodes(obs, present_at(obs, {7}))
    assert (e["lo"], e["hi"]) == (0, 2)


def test_present_at_first_poll_is_left_censored():
    obs = timeline(range(10))
    [e] = build_episodes(obs, present_at(obs, {0, 1}))
    assert e["left_censored"] and e["hi"] is None and e["lo"] == 1


def test_present_at_last_poll_is_right_censored():
    obs = timeline(range(10))
    [e] = build_episodes(obs, present_at(obs, {8, 9}))
    assert e["right_censored"] and e["hi"] is None


def test_same_violation_twice_is_two_episodes():
    obs = timeline(range(10))
    eps = build_episodes(obs, present_at(obs, {2, 5}))
    assert [(e["start"], e["lo"], e["hi"]) for e in eps] == [(2, 0, 2), (5, 0, 2)]


def test_session_change_censors_both_sides():
    # Recorder restarted: session A polls 0..4, session B polls 10..14.
    # Seen at 3, 4 (end of A) and 10, 11 (start of B): two episodes, never merged into one 8 s episode.
    obs = timeline(range(5), "A") + [("B", i, float(t)) for i, t in enumerate(range(10, 15))]
    present = {o[:2]: ROW for o in obs if o[2] in {3, 4, 10, 11}}
    a, b = build_episodes(obs, present)
    assert (a["start"], a["end"], a["right_censored"]) == (3, 4, True)
    assert (b["start"], b["end"], b["left_censored"]) == (10, 11, True)


def test_gap_in_polls_breaks_and_censors():
    # polls 0,1,2 then failures until 10,11. max_gap 3 s. Seen at 1, 2, 10.
    obs = timeline([0, 1, 2, 10, 11])
    eps = build_episodes(obs, present_at(obs, {1, 2, 10}), max_gap=3)
    assert [(e["start"], e["end"], e["left_censored"], e["right_censored"]) for e in eps] == [
        (1, 2, False, True), (10, 10, True, False)]


def test_episode_keeps_best_economics_seen():
    obs = timeline(range(5))
    present = {obs[1][:2]: ROW, obs[2][:2]: {**ROW, "gross_top": "0.05", "best_net": "0.30", "net_1": ""}}
    [e] = build_episodes(obs, present)
    assert e["max_gross"] == 0.05 and e["max_best_net"] == 0.30 and e["max_net_1"] == -0.01


# --- statistics ----------------------------------------------------------------------

def test_quantile_interpolates():
    assert quantile([4, 1, 3, 2], 0.5) == 2.5
    assert quantile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9) == pytest.approx(9.1)


def test_kaplan_meier_without_censoring_is_the_empirical_survival():
    # durations 1, 2, 3: S = 2/3 after 1, 1/3 after 2, 0 after 3
    curve = kaplan_meier([1, 2, 3], [True, True, True])
    assert [round(s, 6) for _, s in curve] == [round(2 / 3, 6), round(1 / 3, 6), 0.0]
    assert km_at(curve, 0.5) == 1.0 and km_at(curve, 2.5) == pytest.approx(1 / 3)


def test_kaplan_meier_with_censoring():
    # 1 (ended), 2 (censored: still alive at 2), 3 (ended)
    # t=1: at risk 3, 1 ends -> S = 2/3.   t=2: censored, no drop, leaves the risk set.
    # t=3: at risk 1, 1 ends -> S = 2/3 * 0 = 0.   The empirical share would wrongly say 1/3 at t=2.5.
    curve = kaplan_meier([1, 2, 3], [True, False, True])
    assert km_at(curve, 2.5) == pytest.approx(2 / 3)
    assert km_at(curve, 3) == 0.0


def test_outlived_reports_a_range_not_a_point():
    # budget 1.5 s. Episodes [lo, hi]: [0, 2] maybe, [2, 4] definitely, [0, 1] no, [3, None] definitely
    eps = [{"lo": 0, "hi": 2}, {"lo": 2, "hi": 4}, {"lo": 0, "hi": 1}, {"lo": 3, "hi": None}]
    o = outlived(eps, 1.5)
    assert o["definitely"] == 0.5 and o["possibly"] == 0.75


def test_wilson_interval_stays_in_bounds():
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and 0.2 < hi < 0.35
    lo, hi = wilson(10, 10)
    assert hi == 1.0 and 0.65 < lo < 0.8


# --- latency budget -------------------------------------------------------------------

LAT = {"get_total": [0.10, 0.10, 0.30], "compute_batch": [0.01, 0.01, 0.01], "post_total": [0.10, 0.10, 0.20]}


def test_budget_adds_half_interval_and_medians():
    # typical = 1.0/2 + 0.10 + 0.01 + 0.10 = 0.71.  worst = 1.0 + p95s.
    # p95 by interpolation (position 0.95 * 2 = 1.9): get 0.10 + 0.9 * 0.20 = 0.28, compute 0.01,
    # post 0.10 + 0.9 * 0.10 = 0.19 -> worst = 1.0 + 0.28 + 0.01 + 0.19 = 1.48
    b, _ = budgets({"interval": 1.0, "rtts": [], "latency": LAT})
    assert b["typical"] == pytest.approx(0.71)
    assert b["worst"] == pytest.approx(1.48)


def test_budget_falls_back_to_recording_rtt():
    b, how = budgets({"interval": 1.0, "rtts": [0.2, 0.2, 0.2]})
    assert b["typical"] == pytest.approx(0.7) and "proxy" in how


def test_capture_share_extremes():
    # Every reaction time is between 0.21 s and 1.51 s.
    long_lived = [{"lo": 10, "hi": 12}] * 5    # always caught
    instant = [{"lo": 0, "hi": 0.1}] * 5       # never caught
    meta = {"interval": 1.0, "rtts": [], "latency": LAT}
    assert capture_share(long_lived, meta) == (1.0, 1.0, 1.0)
    assert capture_share(instant, meta) == (0.0, 0.0, 0.0)


def test_capture_share_needs_measurements():
    assert capture_share([{"lo": 1, "hi": 2}], {"interval": 1.0, "rtts": []}) is None
