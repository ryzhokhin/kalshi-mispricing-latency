"""Fee model tests. Every expected value below was worked by hand; the arithmetic is in the comment."""
from decimal import Decimal as D

import pytest

from mispricing.fees import FeeParams, fee, fill_fee, order_fee

STANDARD = FeeParams("quadratic_with_maker_fees", D("1"))
NO_MAKER = FeeParams("quadratic", D("1"))
HALF = FeeParams("quadratic_with_maker_fees", D("0.5"))  # e.g. KXMLBGAME


# --- taker, single price -------------------------------------------------------

def test_one_contract_at_50c_rounds_up_to_2c():
    # 0.07 * 1 * 0.50 * 0.50 = 0.0175 -> $0.02
    assert fee("0.50", 1, STANDARD) == D("0.02")


def test_hundred_contracts_at_50c_is_exact():
    # 0.07 * 100 * 0.25 = 1.75 -> $1.75 (already on the cent grid)
    assert fee("0.50", 100, STANDARD) == D("1.75")


def test_ten_contracts_at_40c():
    # 0.07 * 10 * 0.40 * 0.60 = 0.168 -> $0.17
    assert fee("0.40", 10, STANDARD) == D("0.17")


def test_seven_contracts_at_62c():
    # 0.07 * 7 = 0.49; 0.62 * 0.38 = 0.2356; 0.49 * 0.2356 = 0.115444 -> $0.12
    assert fee("0.62", 7, STANDARD) == D("0.12")


def test_fractional_contracts():
    # 0.07 * 2.5 * 0.25 = 0.04375 -> $0.05
    assert fee("0.50", "2.5", STANDARD) == D("0.05")


def test_fee_is_symmetric_in_p_and_one_minus_p():
    # P(1-P) is unchanged by P -> 1-P, so YES at p and NO at 1-p pay the same.
    for p in ["0.03", "0.17", "0.38", "0.49"]:
        q = D(1) - D(p)
        assert fee(p, 37, STANDARD) == fee(q, 37, STANDARD)


def test_fee_peaks_at_50c():
    fees = {p: fee(p, 1000, STANDARD) for p in ["0.10", "0.30", "0.50", "0.70", "0.90"]}
    assert max(fees, key=fees.get) == "0.50"


# --- rounding is per order ----------------------------------------------------

def test_many_small_orders_cost_more_than_one_big_order():
    # 100 orders of 1 contract: 100 * $0.02 = $2.00. One order of 100: $1.75.
    assert 100 * fee("0.50", 1, STANDARD) == D("2.00")
    assert fee("0.50", 100, STANDARD) == D("1.75")


def test_multi_level_order_rounds_once():
    # Fill 1: 0.07 * 3 * 0.40 * 0.60 = 0.0504
    # Fill 2: 0.07 * 5 * 0.41 * 0.59 = 0.35 * 0.2419 = 0.084665
    # One order: 0.135065 -> $0.14.  As two orders: $0.06 + $0.09 = $0.15.
    assert order_fee([("0.40", 3), ("0.41", 5)], STANDARD) == D("0.14")
    assert fee("0.40", 3, STANDARD) + fee("0.41", 5, STANDARD) == D("0.15")


def test_fill_fee_rounds_up_to_micro_dollars():
    # 0.07 * 0.333 * 0.667 = 0.07 * 0.222111 = 0.01554777 -> 0.015548
    rate = STANDARD.rate("taker")
    assert fill_fee("0.333", 1, rate) == D("0.015548")


def test_direct_member_precision():
    # 0.0175 rounded up to $0.0001 stays 0.0175
    assert fee("0.50", 1, STANDARD, precision=D("0.0001")) == D("0.0175")


# --- maker and multipliers ------------------------------------------------------

def test_maker_fee_is_quarter_of_taker():
    # 0.0175 * 100 * 0.25 = 0.4375 -> $0.44
    assert fee("0.50", 100, STANDARD, role="maker") == D("0.44")


def test_series_without_maker_fees_charges_makers_nothing():
    assert fee("0.50", 100, NO_MAKER, role="maker") == D("0.00")


def test_fee_multiplier_halves_the_rate():
    # 0.035 * 100 * 0.25 = 0.875 -> $0.88
    assert fee("0.50", 100, HALF) == D("0.88")


# --- inputs that must fail loudly -------------------------------------------------

@pytest.mark.parametrize("price", ["0", "1", "0.0000", "1.2", "-0.1"])
def test_untradeable_prices_raise(price):
    with pytest.raises(ValueError):
        fee(price, 1, STANDARD)


def test_negative_contracts_raise():
    with pytest.raises(ValueError):
        fee("0.50", -1, STANDARD)


def test_flat_fee_type_is_not_silently_modeled():
    with pytest.raises(NotImplementedError):
        fee("0.50", 1, FeeParams("flat"))


def test_zero_contracts_cost_nothing():
    assert fee("0.50", 0, STANDARD) == D("0.00")
