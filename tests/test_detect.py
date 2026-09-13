"""Detector tests on hand-built books. Arithmetic for every expected number is in the comments.

Books are written the way Kalshi sends them: bids only, ascending, decimal strings.
A YES ask comes from the NO bids (ask = 1 - no_bid) and a NO ask from the YES bids.
"""
from decimal import Decimal as D

from mispricing.detect import basket_pnl, detect_event, detect_ladder, quotes_from_book, quotes_from_candle
from mispricing.fees import FeeParams

FEES = FeeParams("quadratic_with_maker_fees", D("1"))


def book(yes=(), no=()):
    """yes / no: (price, size) bids in any order -> Kalshi-shaped ascending book."""
    fmt = lambda lv: [[str(p), str(s)] for p, s in sorted(lv, key=lambda x: D(str(x[0])))]
    return {"yes": fmt(yes), "no": fmt(no)}


def ladder_event(strikes, strike_type="greater"):
    key = "floor_strike" if strike_type in ("greater", "greater_or_equal") else "cap_strike"
    return {"event_ticker": "EV", "structure": "ladder",
            "markets": [{"ticker": f"T{k}", "strike_type": strike_type, key: k} for k in strikes]}


# --- quotes --------------------------------------------------------------------

def test_book_bids_become_asks_of_the_other_side():
    # NO bids 0.15 and 0.19 (best) -> YES asks 0.81 (best) then 0.85
    q = quotes_from_book(book(yes=[("0.80", 10)], no=[("0.15", 5), ("0.19", 7)]))
    assert q.yes_asks == ((D("0.81"), D("7")), (D("0.85"), D("5")))
    assert q.no_asks == ((D("0.20"), D("10")),)


def test_empty_side_is_no_level_not_a_zero_price():
    q = quotes_from_candle("0.0000", "1.0000")
    assert q.yes_asks == () and q.no_asks == ()


# --- basket P&L -------------------------------------------------------------------

def test_small_ladder_edge_is_eaten_by_fees():
    # Buy YES wide @0.81 and NO narrow @0.17 (narrow YES bid 0.83). Gross 0.02 per pair.
    # Depth 100 each. At q=100: fees ceil(1.0773)=1.08 + ceil(0.9877)=0.99 = 2.07
    #   net = 100 - 81 - 17 - 2.07 = -0.07.   At q=1: fees 0.02 + 0.01, net = 0.02 - 0.03 = -0.01.
    legs = [((D("0.81"), D(100)),), ((D("0.17"), D(100)),)]
    pnl = basket_pnl(legs, D(1), FEES)
    assert pnl["gross_top"] == D("0.02")
    assert pnl["q_max"] == D(100)
    assert pnl["net_1"] == D("-0.01")
    assert pnl["best_q"] == D(1) and pnl["best_net"] == D("-0.01")  # least bad; still not an arbitrage
    evaluate_100 = D(100) - D(81) - D(17) - D("2.07")
    assert evaluate_100 == D("-0.07")


def test_large_ladder_edge_survives_fees():
    # YES wide @0.60 (30 of 50 used), NO narrow @0.30 x30. Gross 0.10 per pair, q_max 30.
    # q=30: fees ceil(0.07*30*0.24=0.504)=0.51 + ceil(0.07*30*0.21=0.441)=0.45 = 0.96
    #   net = 30 - 18 - 9 - 0.96 = 2.04
    # q=1:  fees 0.02 + 0.02, net = 1 - 0.90 - 0.04 = 0.06
    legs = [((D("0.60"), D(50)),), ((D("0.30"), D(30)),)]
    pnl = basket_pnl(legs, D(1), FEES)
    assert pnl["q_max"] == D(30) and pnl["top_size"] == D(30)
    assert pnl["best_q"] == D(30) and pnl["best_fees"] == D("0.96") and pnl["best_net"] == D("2.04")
    assert pnl["net_1"] == D("0.06")


def test_walking_the_book_stops_when_marginal_edge_is_gone():
    # Leg A asks: 0.60 x15, 0.66 x50.   Leg B asks: 0.30 x10, 0.35 x20.
    # seg 1: 0.60+0.30=0.90 -> +0.10, step 10 -> q=10
    # seg 2: 0.60+0.35=0.95 -> +0.05, step 5  -> q=15
    # seg 3: 0.66+0.35=1.01 -> -0.01, stop.   q_max = 15
    # q=15: A fee ceil(0.07*15*0.24=0.252)=0.26
    #       B fee ceil(0.147 + 0.07*5*0.35*0.65=0.079625 -> 0.226625)=0.23   (one order, two fills)
    #       cost 9 + 3 + 1.75 = 13.75, net = 15 - 13.75 - 0.49 = 0.76
    # q=10: fees 0.17 + 0.15, cost 9, net = 10 - 9 - 0.32 = 0.68
    legs = [((D("0.60"), D(15)), (D("0.66"), D(50))), ((D("0.30"), D(10)), (D("0.35"), D(20)))]
    pnl = basket_pnl(legs, D(1), FEES)
    assert pnl["q_max"] == D(15)
    assert pnl["best_q"] == D(15) and pnl["best_net"] == D("0.76")


def test_no_gross_edge_returns_none():
    assert basket_pnl([((D("0.50"), D(10)),), ((D("0.50"), D(10)),)], D(1), FEES) is None


def test_empty_leg_returns_none():
    assert basket_pnl([((D("0.10"), D(10)),), ()], D(1), FEES) is None


def test_unknown_depth_uses_assumed_sizes():
    # candles: no sizes. Gross 0.10 at 0.60 + 0.30; q=100: fees 1.68 + 1.47 = 3.15, net = 10 - 3.15 = 6.85
    pnl = basket_pnl([((D("0.60"), None),), ((D("0.30"), None),)], D(1), FEES)
    assert pnl["q_max"] is None and pnl["best_q"] == D(100) and pnl["best_net"] == D("6.85")


# --- ladder ------------------------------------------------------------------------

def test_greater_ladder_violation_direction():
    # "X > 3.75" (wide) YES ask 0.60; "X > 4.00" (narrow) YES bid 0.70 -> impossible, flagged.
    ev = ladder_event([3.75, 4.00])
    quotes = {"T3.75": quotes_from_book(book(yes=[("0.55", 50)], no=[("0.40", 50)])),
              "T4.0": quotes_from_book(book(yes=[("0.70", 30)], no=[("0.20", 30)]))}
    found = detect_ladder(ev, quotes, FEES)
    assert [(k, legs) for k, legs, _ in found] == [("ladder", (("YES", "T3.75"), ("NO", "T4.0")))]
    assert found[0][2]["gross_top"] == D("0.10")


def test_monotone_ladder_has_no_violation():
    ev = ladder_event([3.75, 4.00])
    quotes = {"T3.75": quotes_from_book(book(yes=[("0.80", 50)], no=[("0.19", 50)])),   # bid .80 ask .81
              "T4.0": quotes_from_book(book(yes=[("0.10", 30)], no=[("0.88", 30)]))}    # bid .10 ask .12
    assert detect_ladder(ev, quotes, FEES) == []


def test_non_adjacent_violation_is_found():
    # strike 1: bid .40 ask .50 | strike 2: bid .30 ask .70 | strike 3: bid .60 ask .90
    # 1-2: bid2 .30 < ask1 .50 ok.  2-3: bid3 .60 < ask2 .70 ok.  1-3: bid3 .60 > ask1 .50 VIOLATION.
    ev = ladder_event([1, 2, 3])
    quotes = {"T1": quotes_from_book(book(yes=[("0.40", 10)], no=[("0.50", 10)])),
              "T2": quotes_from_book(book(yes=[("0.30", 10)], no=[("0.30", 10)])),
              "T3": quotes_from_book(book(yes=[("0.60", 10)], no=[("0.10", 10)]))}
    found = detect_ladder(ev, quotes, FEES)
    assert [legs for _, legs, _ in found] == [(("YES", "T1"), ("NO", "T3"))]


def test_less_ladder_flips_which_rung_is_wide():
    # "X < 2" is inside "X < 3", so strike 3 is wide. Violation if bid(X<2) > ask(X<3).
    ev = ladder_event([2, 3], strike_type="less")
    quotes = {"T2": quotes_from_book(book(yes=[("0.70", 10)], no=[("0.25", 10)])),   # bid .70
              "T3": quotes_from_book(book(yes=[("0.55", 10)], no=[("0.40", 10)]))}   # ask .60
    found = detect_ladder(ev, quotes, FEES)
    assert [legs for _, legs, _ in found] == [(("YES", "T3"), ("NO", "T2"))]


# --- outcome sets and the YES/NO check ----------------------------------------------------

T_NOW = 1_800_000_000  # 2027-01-15, after every open/close time below
OPEN = "2026-01-01T00:00:00Z"


def set_event(n, other_markets=()):
    return {"event_ticker": "SET", "structure": "outcome_set",
            "markets": [{"ticker": f"O{i}", "open_time": OPEN} for i in range(n)],
            "other_markets": list(other_markets)}


def test_short_basket():
    # YES bids .40 .35 .30 sum 1.05 -> NO asks .60 .65 .70 cost 1.95, payout n-1 = 2, gross .05
    quotes = {f"O{i}": quotes_from_book(book(yes=[(b, 100)], no=[("0.01", 100)]))
              for i, b in enumerate(["0.40", "0.35", "0.30"])}
    kinds = {k: pnl for k, _, pnl in detect_event(set_event(3), quotes, FEES, T_NOW)}
    assert set(kinds) == {"set_short"} and kinds["set_short"]["gross_top"] == D("0.05")


def test_long_basket_eaten_by_fees():
    # YES asks .30 .33 .34 -> gross .03 per set.
    # q=100 fees: ceil(1.47)=1.47 + ceil(1.5477)=1.55 + ceil(1.5708)=1.58 = 4.60 > 3.00
    quotes = {f"O{i}": quotes_from_book(book(yes=[("0.01", 100)], no=[(no_bid, 100)]))
              for i, no_bid in enumerate(["0.70", "0.67", "0.66"])}
    kinds = {k: pnl for k, _, pnl in detect_event(set_event(3), quotes, FEES, T_NOW)}
    long = kinds["set_long"]
    assert long["gross_top"] == D("0.03") and long["best_net"] < 0
    assert basket_pnl([quotes[f"O{i}"].yes_asks for i in range(3)], D(1), FEES)["q_max"] == D(100)


def test_partial_set_is_never_evaluated():
    quotes = {"O0": quotes_from_book(book(no=[("0.90", 10)])), "O1": quotes_from_book(book(no=[("0.90", 10)]))}
    assert detect_event(set_event(3), quotes, FEES, T_NOW) == []   # O2 missing: a 2-leg "basket" would be fake


def test_yes_no_cross():
    # YES bid .55 + NO bid .50 = 1.05 -> buy NO @.45 and YES @.50 = .95 for a sure $1
    q = {"T1": quotes_from_book(book(yes=[("0.55", 10)], no=[("0.50", 10)]))}
    found = [f for f in detect_event(ladder_event([1]), q, FEES, T_NOW) if f[0] == "yes_no_cross"]
    assert len(found) == 1 and found[0][2]["gross_top"] == D("0.05")


def test_long_basket_waits_until_left_out_outcomes_have_closed():
    # Two "finalists" with YES asks .30 + .30 = .60 look like 40c free -- but a third
    # player's market was still open until 2026-09-02, so before then the set was incomplete.
    quotes = {f"O{i}": quotes_from_book(book(yes=[("0.01", 100)], no=[("0.70", 100)])) for i in range(2)}
    knocked_out = [{"ticker": "O_OUT", "status": "finalized", "result": "no",
                    "open_time": OPEN, "close_time": "2026-09-02T17:14:50Z"}]
    ev = set_event(2, knocked_out)
    before = 1788300000  # 2026-09-01
    after = 1788400000   # 2026-09-03
    assert [k for k, _, _ in detect_event(ev, quotes, FEES, before)] == []
    assert [k for k, _, _ in detect_event(ev, quotes, FEES, after)] == ["set_long"]
