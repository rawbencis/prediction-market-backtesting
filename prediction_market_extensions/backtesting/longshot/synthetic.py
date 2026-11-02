"""Deterministic, network-free paired books for real Nautilus execution."""

from decimal import Decimal

from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas
from nautilus_trader.model.enums import AssetClass, BookAction, OrderSide, RecordFlag
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Price, Quantity

SECOND = 1_000_000_000
START_NS = 1_782_864_000 * SECOND


def snapshot(instrument, ts_ns, bid, ask, *, bid_size=20, ask_size=20, ts_event=None):
    """A complete L2 snapshot, not a fabricated quote/complement."""
    event_ns = ts_ns if ts_event is None else ts_event
    deltas = [
        OrderBookDelta(
            instrument.id, BookAction.CLEAR, None, int(RecordFlag.F_SNAPSHOT), 0, event_ns, ts_ns
        ),
        OrderBookDelta(
            instrument.id,
            BookAction.ADD,
            BookOrder(OrderSide.BUY, instrument.make_price(bid), Quantity.from_int(bid_size), 1),
            0,
            0,
            event_ns,
            ts_ns,
        ),
        OrderBookDelta(
            instrument.id,
            BookAction.ADD,
            BookOrder(OrderSide.SELL, instrument.make_price(ask), Quantity.from_int(ask_size), 2),
            int(RecordFlag.F_LAST),
            0,
            event_ns,
            ts_ns,
        ),
    ]
    return OrderBookDeltas(instrument.id, deltas)


def synthetic_fixture():
    """Return (markets, records_by_market, instruments_by_market).

    Cases: no touch, one touch/loss, one reversal, two reversals, partial entry
    and exit, arrival-price rejection, transient target, and unknown settlement.
    Three dates illustrate chronological splits, not statistical validation.
    All cases begin below threshold and include post-close clock-driving data.
    Underlying spot/perpetual features are explicitly unavailable.
    """
    cases = (
        "no_touch",
        "one_touch_loss",
        "one_flip",
        "two_flips",
        "partial",
        "arrival_reprice",
        "transient_target",
        "missing_settlement",
    )
    markets, records, instruments = [], {}, {}
    offsets = (
        0,
        SECOND,
        SECOND + 50_000_000,
        SECOND + 150_000_000,
        2 * SECOND,
        2 * SECOND + 50_000_000,
        2 * SECOND + 150_000_000,
        2 * SECOND + 200_000_000,
        3 * SECOND,
        3 * SECOND + 150_000_000,
        3 * SECOND + 250_000_000,
        3 * SECOND + 500_000_000,
        3 * SECOND + 750_000_000,
        4 * SECOND,
        4 * SECOND + 500_000_000,
        5 * SECOND,
    )
    for index, key in enumerate(cases):
        # Markets overlap within each date, exercising shared portfolio limits.
        start = START_NS + (index // 3) * 86_400 * SECOND + index * 1_000_000
        close = start + 4 * SECOND
        market = {
            "market_id": key,
            "slug": f"synthetic-{key}",
            "asset": "BTC",
            "duration_seconds": 4,
            "source": "synthetic",
            "underlying_available": False,
            "underlying_source": None,
            "start_ns": start,
            "close_ns": close,
            "fee_rate": 0.02,
            "fee_schedule": "polymarket_p*(1-p)",
            "fixture_fee_known": True,
            "settlement_winner": None if key == "missing_settlement" else "UP",
            "settlement_ns": None if key == "missing_settlement" else close + 500_000_000,
        }
        markets.append(market)
        pair = {}
        for side in ("UP", "DOWN"):
            symbol = Symbol(f"LONGSHOT-{index}-{side}")
            pair[side] = BinaryOption(
                instrument_id=InstrumentId(symbol, Venue("POLYMARKET")),
                raw_symbol=symbol,
                asset_class=AssetClass.ALTERNATIVE,
                currency=USDC,
                price_precision=2,
                size_precision=0,
                price_increment=Price.from_str("0.01"),
                size_increment=Quantity.from_int(1),
                activation_ns=start,
                expiration_ns=close + 100 * SECOND,
                ts_event=start,
                ts_init=start,
                taker_fee=Decimal("0.02"),
                outcome=side,
                info={},
            )
        instruments[key] = pair
        market["outcomes"] = {side: str(instrument.id) for side, instrument in pair.items()}
        records[key] = {"UP": [], "DOWN": []}
        for offset in offsets:
            up, down = (0.54, 0.56), (0.44, 0.46)
            if key != "no_touch" and offset >= SECOND:
                up, down = (0.72, 0.74), (0.26, 0.28)
            if (
                key in ("one_flip", "two_flips", "partial", "transient_target")
                and offset >= 2 * SECOND
            ):
                up, down = (0.26, 0.28), (0.72, 0.74)
            if key == "two_flips" and offset >= 3 * SECOND:
                up, down = (0.72, 0.74), (0.26, 0.28)
            if key == "arrival_reprice" and offset >= SECOND + 50_000_000:
                down = (0.43, 0.45)
            if (key == "transient_target" and offset >= 2 * SECOND + 50_000_000) or (
                key == "partial" and offset >= 2 * SECOND + 200_000_000
            ):
                down = (0.60, 0.62)
            for side, (bid, ask) in (("UP", up), ("DOWN", down)):
                ask_size = 2 if key == "partial" and side == "DOWN" and offset < 2 * SECOND else 20
                bid_size = (
                    1
                    if key == "partial"
                    and side == "DOWN"
                    and 2 * SECOND + 50_000_000 <= offset < 3 * SECOND
                    else 20
                )
                if key == "partial" and side == "DOWN" and offset == 2 * SECOND:
                    bid_size = 2
                records[key][side].append(
                    snapshot(
                        pair[side], start + offset, bid, ask, bid_size=bid_size, ask_size=ask_size
                    )
                )
    return markets, records, instruments
