"""Kalshi trading-fee model. Every edge reported by this repo is charged through here.

Sources, read 2026-09-12:
  - Kalshi Fee Schedule, "Fee Schedule for July 2026 - 7.7.26 Update"
    https://kalshi.com/docs/kalshi-fee-schedule.pdf
  - Fee rounding: https://docs.kalshi.com/getting_started/fee_rounding
  - Per-series fee_type / fee_multiplier: GET /series/{series_ticker}

Model:
  taker fee = roundup( 0.07 * m * C * P * (1 - P) )
  maker fee = roundup( 0.07 * s * m * C * P * (1 - P) )
      C = contracts, P = price of the contract traded, in dollars
      m = series fee_multiplier (1 for most series, 0.5 for e.g. KXMLBGAME)
      s = maker share of the taker rate, by series fee_type:
            quadratic                         0     (no maker fee)
            quadratic_with_maker_fees         0.25  (-> 0.0175)
            quadratic_with_combo_maker_fees   0.5
            flat                              not modeled (Specific Trading Fees table)

Rounding: Kalshi rounds each fill's fee UP to $0.000001, then an accumulator
kept per ORDER rounds the balance change to the member's precision ($0.01 for
non-direct members like us) and rebates any overpayment on later fills. Net
effect modeled here: one round-up to the cent per order, not per contract and
not per fill.

Not modeled: the extra rounding of the trade value itself when a fill's
notional is off the cent grid (subpenny prices, fractional contracts).
"""
import argparse
import json
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

CENT = Decimal("0.01")
MICRO = Decimal("0.000001")
TAKER_RATE = Decimal("0.07")
MAKER_SHARE = {
    "quadratic": Decimal("0"),
    "quadratic_with_maker_fees": Decimal("0.25"),
    "quadratic_with_combo_maker_fees": Decimal("0.5"),
}


def to_decimal(x):
    """API strings ("0.4100") and ints go in exactly; floats go through str() so 0.1 stays 0.1."""
    return x if isinstance(x, Decimal) else Decimal(str(x))


def ceil_to(x, step):
    return (x / step).to_integral_value(rounding=ROUND_CEILING) * step


@dataclass(frozen=True)
class FeeParams:
    fee_type: str = "quadratic"
    multiplier: Decimal = Decimal("1")

    def rate(self, role):
        if self.fee_type not in MAKER_SHARE:
            raise NotImplementedError(f"fee_type {self.fee_type!r} uses Kalshi's Specific Trading Fees table; not modeled")
        taker = TAKER_RATE * to_decimal(self.multiplier)
        if role == "taker":
            return taker
        if role == "maker":
            return taker * MAKER_SHARE[self.fee_type]
        raise ValueError(f"role must be 'taker' or 'maker', got {role!r}")


def fill_fee(price, contracts, rate):
    """Fee for one fill, rounded up to $0.000001 as Kalshi does before order-level rounding."""
    p, c = to_decimal(price), to_decimal(contracts)
    # A price of 0 or 1 is not a tradeable quote -- usually an empty book side.
    # Fail loudly instead of returning a zero fee that makes a fake edge look free.
    if not Decimal(0) < p < Decimal(1):
        raise ValueError(f"price must be strictly between 0 and 1 dollars, got {p}")
    if c < 0:
        raise ValueError(f"contracts must be >= 0, got {c}")
    return ceil_to(rate * c * p * (1 - p), MICRO)


def order_fee(fills, params=FeeParams(), role="taker", precision=CENT):
    """Fee for ONE order that may fill at several price levels.

    fills: iterable of (price, contracts), e.g. the book levels a taker order walks.
    """
    rate = params.rate(role)
    total = sum((fill_fee(p, c, rate) for p, c in fills), Decimal(0))
    return ceil_to(total, precision)


def fee(price, contracts, params=FeeParams(), role="taker", precision=CENT):
    """Fee for one order filled entirely at one price."""
    return order_fee([(price, contracts)], params, role, precision)


# ---- per-series fee parameters (plumbing) -----------------------------------

def fetch_series_fees(universe_path="data/universe.json", out_path="data/series_fees.json"):
    from .api import KalshiClient

    universe = json.loads(Path(universe_path).read_text())
    client = KalshiClient()
    fees = {}
    for series in sorted({ev["series_ticker"] for ev in universe["events"]}):
        data, _ = client.get(f"/series/{series}")
        s = data.get("series", data)
        fees[series] = {"fee_type": s.get("fee_type"), "fee_multiplier": s.get("fee_multiplier")}
    out = {"fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "series": fees}
    Path(out_path).write_text(json.dumps(out, indent=1))
    return out


def load_series_fees(path="data/series_fees.json"):
    """series_ticker -> FeeParams"""
    raw = json.loads(Path(path).read_text())["series"]
    return {k: FeeParams(v["fee_type"], to_decimal(v["fee_multiplier"])) for k, v in raw.items()}


def main():
    ap = argparse.ArgumentParser(description="Fetch per-series fee params and print fee tables.")
    ap.add_argument("--fetch", action="store_true", help="refresh data/series_fees.json from the API")
    args = ap.parse_args()

    if args.fetch:
        out = fetch_series_fees()
        print(f"fetched fee params for {len(out['series'])} series:")
        for k, v in out["series"].items():
            print(f"  {k:18s} {v['fee_type']:32s} m={v['fee_multiplier']}")
        print()

    params = FeeParams("quadratic_with_maker_fees")
    print("taker fee PER CONTRACT, in cents (m=1). Rounding hurts small orders most:")
    print("  price   C=1    C=10   C=100  C=1000 | maker C=100")
    for p in ["0.05", "0.10", "0.25", "0.40", "0.50", "0.60", "0.75", "0.90", "0.95"]:
        row = [fee(p, c, params) / c * 100 for c in (1, 10, 100, 1000)]
        maker = fee(p, 100, params, role="maker")
        print(f"  {p}  " + "  ".join(f"{x:5.2f}" for x in row) + f" | {maker:5.2f}")


if __name__ == "__main__":
    main()
