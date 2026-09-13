"""Replay fill/settlement tests. Every number worked by hand in the comments."""
from decimal import Decimal as D

from mispricing.detect import Quotes
from mispricing.fees import FeeParams
from mispricing.replay import bids_for, ioc_buy, parse_legs, plan_orders, settle

FEES = FeeParams("quadratic_with_maker_fees", D("1"))


def test_parse_legs():
    assert parse_legs("ladder|YES:A|NO:B") == [("YES", "A"), ("NO", "B")]


def test_ioc_buy_respects_limit_and_size():
    asks = ((D("0.60"), D(10)), (D("0.61"), D(10)), (D("0.65"), D(50)))
    assert ioc_buy(asks, D("0.61"), D(15)) == [(D("0.60"), D(10)), (D("0.61"), D(5))]
    assert ioc_buy(asks, D("0.59"), D(15)) == []


def test_bids_come_from_opposite_asks():
    # a NO ask at 0.42 is a YES bid at 0.58
    q = Quotes(yes_asks=((D("0.60"), D(5)),), no_asks=((D("0.42"), D(100)),))
    assert bids_for(q, "YES") == ((D("0.58"), D(100)),)
    assert bids_for(q, "NO") == ((D("0.40"), D(5)),)


def test_plan_uses_worst_level_needed():
    quotes = {"A": Quotes(yes_asks=((D("0.60"), D(10)), (D("0.62"), D(50))), no_asks=())}
    assert plan_orders([("YES", "A")], quotes, D(30)) == [("YES", "A", D("0.62"))]


# Detection book: leg A = YES asks 0.60 x50, leg B = NO asks 0.30 x30. W = 1, q* = 30.
PLAN = [("YES", "A", D("0.60")), ("NO", "B", D("0.30"))]
A_BIDS = ((D("0.42"), D(100)),)  # A's NO asks -> YES bids at 0.58


def test_unchanged_book_reproduces_the_detected_pnl():
    # Same as test_detect: 30 - 18 - 9 - fees (0.51 + 0.45) = 2.04
    later = {"A": Quotes(((D("0.60"), D(50)),), A_BIDS), "B": Quotes((), ((D("0.30"), D(30)),))}
    s = settle(PLAN, D(30), later, D(1), FEES)
    assert s["outcome"] == "full" and s["pnl"] == D("2.04")


def test_second_leg_gone_is_leg_risk_and_loses():
    # B's ask moved to 0.40 > limit: A fills 30 @0.60, B fills 0. Hedged 0.
    # Unwind 30 A into the 0.58 bid.
    #   cost 18.00, buy fee ceil(0.07*30*0.24 = 0.504) = 0.51
    #   proceeds 17.40, sell fee ceil(0.07*30*0.58*0.42 = 0.51156) = 0.52
    #   pnl = -18.00 - 0.51 + 17.40 - 0.52 = -1.63
    later = {"A": Quotes(((D("0.60"), D(50)),), A_BIDS), "B": Quotes((), ((D("0.40"), D(30)),))}
    s = settle(PLAN, D(30), later, D(1), FEES)
    assert s["outcome"] == "legged" and s["hedged"] == 0 and s["unwound"] == 30
    assert s["pnl"] == D("-1.63")


def test_partial_fill_hedges_the_minimum():
    # B only has 10 left at 0.30. Hedged 10, unwind 20 A at 0.58.
    #   payout 10; cost 18 + 3 = 21; buy fees 0.51 + ceil(0.147) = 0.15
    #   proceeds 11.60, sell fee ceil(0.07*20*0.2436 = 0.34104) = 0.35
    #   pnl = 10 - 21 - 0.66 + 11.60 - 0.35 = -0.41
    later = {"A": Quotes(((D("0.60"), D(50)),), A_BIDS), "B": Quotes((), ((D("0.30"), D(10)), (D("0.35"), D(40))))}
    s = settle(PLAN, D(30), later, D(1), FEES)
    assert s["outcome"] == "partial" and s["hedged"] == 10
    assert s["pnl"] == D("-0.41")


def test_no_bid_to_unwind_writes_contracts_off():
    # A fills, B does not, and nobody bids for A: lose cost + buy fee = -18.51
    later = {"A": Quotes(((D("0.60"), D(50)),), ()), "B": Quotes((), ())}
    s = settle(PLAN, D(30), later, D(1), FEES)
    assert s["written_off"] == 30 and s["pnl"] == D("-18.51")


def test_both_legs_gone_is_a_miss_with_zero_pnl():
    later = {"A": Quotes(((D("0.70"), D(50)),), A_BIDS), "B": Quotes((), ((D("0.40"), D(30)),))}
    s = settle(PLAN, D(30), later, D(1), FEES)
    assert s["outcome"] == "miss" and s["pnl"] == 0
