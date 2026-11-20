"""``realised_lots`` — the persisted FIFO match (ADR-0015 rules 4, 5 and 9).

Drives :func:`app.services.holdings_service.rebuild_realised_lots` directly against an
in-memory session. The route, importer, demo-seed and teardown wiring is covered where those
live; this file locks the replay's output.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Instrument,
    InvestmentAccount,
    InvestmentTransaction,
    InvestmentTxnTypeStr,
    RealisedLot,
    User,
)
from app.models.instrument import AssetClassStr
from app.services.fx_math import to_inr_paise
from app.services.holdings_service import (
    OversellError,
    _replay,
    _ReplayOut,
    compute_holdings,
    rebuild_realised_lots,
)

D1, D2, D3, D4 = date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1), date(2026, 4, 1)


def _instrument(
    session: Session,
    user_id: UUID,
    *,
    symbol: str = "INF001",
    currency: str = "INR",
    asset_class: AssetClassStr | None = None,
) -> Instrument:
    inst = Instrument(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} Fund",
        asset_class=asset_class or ("indian_mf" if currency == "INR" else "us_equity"),
        currency=currency,
        exchange="MFCentral" if currency == "INR" else "NASDAQ",
        current_nav=Decimal("100"),
    )
    session.add(inst)
    session.flush()
    return inst


def _default_account_id(session: Session, user_id: UUID) -> int:
    acc = session.scalar(select(InvestmentAccount).where(InvestmentAccount.user_id == user_id))
    if acc is None:
        acc = InvestmentAccount(
            user_id=user_id,
            name="Default Portfolio",
            account_type="manual",
            provider="manual",
            external_account_ref="default",
            currency="INR",
            is_active=True,
        )
        session.add(acc)
        session.flush()
    return acc.id


def _txn(
    session: Session,
    user_id: UUID,
    instrument_id: int,
    txn_type: InvestmentTxnTypeStr,
    on: date,
    units: str = "0",
    amount: int = 0,
    fees: int = 0,
    fx: str = "1",
    account_id: int | None = None,
) -> InvestmentTransaction:
    aid = account_id if account_id is not None else _default_account_id(session, user_id)
    txn = InvestmentTransaction(
        user_id=user_id,
        account_id=aid,
        instrument_id=instrument_id,
        date=on,
        transaction_type=txn_type,
        units=Decimal(units),
        price_per_unit_native=None,
        amount_native_paise=amount,
        fees_native_paise=fees,
        fx_rate_to_inr=Decimal(fx),
    )
    session.add(txn)
    session.flush()
    return txn


def _rows(session: Session, user_id: UUID | None = None) -> list[RealisedLot]:
    stmt = select(RealisedLot).order_by(RealisedLot.id)
    if user_id is not None:
        stmt = stmt.where(RealisedLot.user_id == user_id)
    return list(session.scalars(stmt))


def _shape(r: RealisedLot) -> tuple[int, int, Decimal, int, int, int, date, date]:
    return (
        r.sell_txn_id,
        r.buy_txn_id,
        r.units,
        r.cost_native_paise,
        r.proceeds_native_paise,
        r.sell_fees_native_paise,
        r.buy_date,
        r.sell_date,
    )


def _three_lots(session: Session, user: User, inst: Instrument):  # type: ignore[no-untyped-def]
    a = _txn(session, user.id, inst.id, "buy", D1, "10", 100_000, 1_000)
    b = _txn(session, user.id, inst.id, "buy", D2, "10", 120_000)
    c = _txn(session, user.id, inst.id, "buy", D2, "5", 70_000)  # same date as B, higher id
    return a, b, c


def test_sell_records_each_consumed_lot(session: Session, user: User) -> None:
    inst = _instrument(session, user.id)
    a, b, c = _three_lots(session, user, inst)
    s1 = _txn(session, user.id, inst.id, "sell", D3, "22", 264_001, 1_001)
    rebuild_realised_lots(session, user_id=user.id, instrument_id=inst.id)

    rows = _rows(session)
    assert [_shape(r) for r in rows] == [
        (s1.id, a.id, Decimal("10"), 101_000, 120_000, 455, D1, D3),
        (s1.id, b.id, Decimal("10"), 120_000, 120_001, 455, D2, D3),
        # Boundary lot: half-even share of C's cost, 70_000 * 2/5.
        (s1.id, c.id, Decimal("2"), 28_000, 24_000, 91, D2, D3),
    ]
    assert {r.user_id for r in rows} == {user.id}

    s2 = _txn(session, user.id, inst.id, "sell", D4, "3", 50_000)
    rebuild_realised_lots(session, user_id=user.id, instrument_id=inst.id)
    rows = _rows(session)
    assert len(rows) == 4
    assert _shape(rows[-1] if rows[-1].sell_txn_id == s2.id else rows[0]) == (
        s2.id,
        c.id,
        Decimal("3"),
        42_000,
        50_000,
        0,
        D2,
        D4,
    )
    # C's two releases sum to its whole cost.
    assert sum(r.cost_native_paise for r in rows if r.buy_txn_id == c.id) == 70_000


def test_bonus_lot_matches_at_zero_cost_and_switch_out_records_like_a_sell(
    session: Session, user: User
) -> None:
    inst = _instrument(session, user.id)
    bonus = _txn(session, user.id, inst.id, "bonus", D1, "5")
    out = _txn(session, user.id, inst.id, "switch_out", D2, "5", 9_000, 100)
    rebuild_realised_lots(session, user_id=user.id, instrument_id=inst.id)

    assert [_shape(r) for r in _rows(session)] == [
        (out.id, bonus.id, Decimal("5"), 0, 9_000, 100, D1, D2)
    ]


def test_sell_shares_sum_to_the_sell_totals(session: Session, user: User) -> None:
    inst = _instrument(session, user.id)
    _three_lots(session, user, inst)
    s1 = _txn(session, user.id, inst.id, "sell", D3, "22", 264_001, 1_001)
    rebuild_realised_lots(session, user_id=user.id, instrument_id=inst.id)

    rows = [r for r in _rows(session) if r.sell_txn_id == s1.id]
    assert [r.proceeds_native_paise for r in rows] == [120_000, 120_001, 24_000]
    assert [r.sell_fees_native_paise for r in rows] == [455, 455, 91]
    assert sum(r.proceeds_native_paise for r in rows) == 264_001
    assert sum(r.sell_fees_native_paise for r in rows) == 1_001

    # Three uneven lots with a remainder to hand out; a switch_out allocates the same way.
    inst2 = _instrument(session, user.id, symbol="INF002")
    for on, units in ((D1, "3"), (D1, "3"), (D2, "4")):
        _txn(session, user.id, inst2.id, "buy", on, units, 1_000)
    out = _txn(session, user.id, inst2.id, "switch_out", D3, "10", 1_000, 7)
    rebuild_realised_lots(session, user_id=user.id, instrument_id=inst2.id)

    rows = [r for r in _rows(session) if r.sell_txn_id == out.id]
    assert len(rows) == 3
    assert sum(r.proceeds_native_paise for r in rows) == 1_000
    assert sum(r.sell_fees_native_paise for r in rows) == 7
    for r in rows:
        assert abs(r.proceeds_native_paise - Decimal(1_000) * r.units / 10) <= 1
        assert abs(r.sell_fees_native_paise - Decimal(7) * r.units / 10) <= 1


def test_rebuild_refuses_an_uncovered_sell(session: Session, user: User) -> None:
    """ADR-0015 rule 9: a stored oversell raises and leaves existing rows untouched."""
    inst = _instrument(session, user.id)
    _txn(session, user.id, inst.id, "buy", D1, "10", 100_000)
    _txn(session, user.id, inst.id, "sell", D2, "4", 40_000)
    rebuild_realised_lots(session, user_id=user.id, instrument_id=inst.id)
    before = [_shape(r) for r in _rows(session)]
    assert len(before) == 1

    bad = _txn(session, user.id, inst.id, "sell", D3, "9", 90_000)  # only 6 left
    with pytest.raises(OversellError) as exc:
        rebuild_realised_lots(session, user_id=user.id, instrument_id=inst.id)
    assert exc.value.uncovered == ((bad.id, Decimal("3")),)
    assert [_shape(r) for r in _rows(session)] == before


def test_rebuild_is_scoped_to_one_user_and_instrument(session: Session, user: User) -> None:
    other_user = User(id=uuid4())
    session.add(other_user)
    session.flush()
    mine = _instrument(session, user.id)
    sibling = _instrument(session, user.id, symbol="INF002")
    theirs = _instrument(session, other_user.id)
    for u, i in ((user.id, mine), (user.id, sibling), (other_user.id, theirs)):
        _txn(session, u, i.id, "buy", D1, "10", 100_000)
        _txn(session, u, i.id, "sell", D2, "5", 60_000)
        rebuild_realised_lots(session, user_id=u, instrument_id=i.id)
    assert len(_rows(session)) == 3
    before = {r.id: _shape(r) for r in _rows(session)}
    mine_sell_ids = {
        t.id
        for t in session.scalars(
            select(InvestmentTransaction).where(InvestmentTransaction.instrument_id == mine.id)
        )
    }

    # Rebuilding one instrument again neither duplicates its rows nor disturbs the others.
    rebuild_realised_lots(session, user_id=user.id, instrument_id=mine.id)
    rows = _rows(session)
    assert len(rows) == 3
    after = {r.id: _shape(r) for r in rows}
    for rid, shape in before.items():
        if shape[0] not in mine_sell_ids:
            assert after[rid] == shape  # same id, same content: never deleted and re-inserted
    assert len(_rows(session, other_user.id)) == 1


def test_intraday_is_not_a_capital_gain(session: Session, user: User) -> None:
    """ADR-0015 rules 5, 7, 8: the day's buy is matched to the sell as an ``intraday`` row,
    only the residual 30 units consume the old lot as a ``fifo`` row, and the sell's proceeds
    and fees are shared across both so they still sum to the sell's totals."""
    inst = _instrument(session, user.id, asset_class="indian_equity")
    old = _txn(session, user.id, inst.id, "buy", D1, "100", 1_000_000)
    day_buy = _txn(session, user.id, inst.id, "buy", D2, "50", 600_000)
    sell = _txn(session, user.id, inst.id, "sell", D2, "80", 1_040_000, 800)
    rebuild_realised_lots(session, user_id=user.id, instrument_id=inst.id)

    rows = _rows(session)
    assert [(_shape(r), r.kind) for r in rows] == [
        ((sell.id, day_buy.id, Decimal("50"), 600_000, 650_000, 500, D2, D2), "intraday"),
        ((sell.id, old.id, Decimal("30"), 300_000, 390_000, 300, D1, D2), "fifo"),
    ]
    fifo = [r for r in rows if r.kind == "fifo"]
    assert day_buy.id not in {r.buy_txn_id for r in fifo}
    assert sum(r.units for r in fifo) == Decimal("30")
    assert all(r.buy_date == r.sell_date for r in rows if r.kind == "intraday")
    assert sum(r.proceeds_native_paise for r in rows) == 1_040_000
    assert sum(r.sell_fees_native_paise for r in rows) == 800


def test_intraday_matches_buys_to_sells_in_source_order(session: Session, user: User) -> None:
    """Rule 7: the day's sells take the day's buys in source order; what no buy covers falls
    to FIFO."""
    inst = _instrument(session, user.id, asset_class="indian_equity")
    old = _txn(session, user.id, inst.id, "buy", D1, "10", 100_000)
    s1 = _txn(session, user.id, inst.id, "sell", D2, "4", 44_000, 40)
    b1 = _txn(session, user.id, inst.id, "buy", D2, "3", 30_000)
    s2 = _txn(session, user.id, inst.id, "sell", D2, "6", 70_000, 61)
    b2 = _txn(session, user.id, inst.id, "buy", D2, "5", 52_000)
    rebuild_realised_lots(session, user_id=user.id, instrument_id=inst.id)

    rows = _rows(session)
    assert [(r.kind, r.sell_txn_id, r.buy_txn_id, r.units) for r in rows] == [
        ("intraday", s1.id, b1.id, Decimal("3")),
        ("intraday", s1.id, b2.id, Decimal("1")),
        ("intraday", s2.id, b2.id, Decimal("4")),
        ("fifo", s2.id, old.id, Decimal("2")),
    ]
    for sell in (s1, s2):
        mine = [r for r in rows if r.sell_txn_id == sell.id]
        assert sum(r.proceeds_native_paise for r in mine) == sell.amount_native_paise
        assert sum(r.sell_fees_native_paise for r in mine) == sell.fees_native_paise


def _surviving_lots(session: Session, inst: Instrument) -> dict[int, tuple[Decimal, int]]:
    """A fresh replay's surviving lots, keyed by the acquisition txn id."""
    out = _ReplayOut(matches=[], uncovered=[])
    lots = _replay(
        session.scalars(
            select(InvestmentTransaction)
            .where(InvestmentTransaction.instrument_id == inst.id)
            .order_by(InvestmentTransaction.date, InvestmentTransaction.id)
        ),
        nets_intraday=inst.asset_class == "indian_equity",
        out=out,
    )
    return {lot.txn_id: (lot.units, lot.cost_paise) for lot in lots}


def test_f11_and_holdings_agree_on_remaining_lots(session: Session, user: User) -> None:
    """ADR-0015 rule 4. F11 reads ``realised_lots``; holdings fold the replay.

    The remaining units and cost per lot derived from the persisted rows alone must equal
    the replay's surviving lots, after back-dating, deletion, reinvestment and bonus.
    """
    inr = _instrument(session, user.id, symbol="INR1")
    usd = _instrument(session, user.id, symbol="USD1", currency="USD")
    gone = _instrument(session, user.id, symbol="GONE")
    eq = _instrument(session, user.id, symbol="EQ", asset_class="indian_equity")

    _txn(session, user.id, inr.id, "buy", D2, "10", 100_000, 500)
    _txn(session, user.id, inr.id, "sip", D2, "6", 66_000)
    div = _txn(session, user.id, inr.id, "dividend", D3, "0", 5_000)
    reinv = _txn(session, user.id, inr.id, "buy", D3, "0.8", 5_000)
    div.pair_id, reinv.pair_id = reinv.id, div.id
    _txn(session, user.id, inr.id, "bonus", D3, "4")
    _txn(session, user.id, inr.id, "sell", D4, "13", 200_000, 700)
    rebuild_realised_lots(session, user_id=user.id, instrument_id=inr.id)
    # Back-dated buy lands before every other lot: the sell re-matches onto it.
    _txn(session, user.id, inr.id, "buy", D1, "3", 27_000)
    rebuild_realised_lots(session, user_id=user.id, instrument_id=inr.id)
    # A deleted buy (the SIP) is re-matched around.
    sip = session.scalars(
        select(InvestmentTransaction).where(InvestmentTransaction.transaction_type == "sip")
    ).one()
    session.delete(sip)
    rebuild_realised_lots(session, user_id=user.id, instrument_id=inr.id)

    for on, units, amount, fx in ((D1, "10", 100_000, "80.5"), (D2, "5", 60_000, "82.25")):
        _txn(session, user.id, usd.id, "buy", on, units, amount, 100, fx)
    _txn(session, user.id, usd.id, "sell", D3, "12", 150_000, 250, "83")
    rebuild_realised_lots(session, user_id=user.id, instrument_id=usd.id)

    _txn(session, user.id, gone.id, "buy", D1, "5", 50_000)
    _txn(session, user.id, gone.id, "sell", D2, "5", 60_000)
    rebuild_realised_lots(session, user_id=user.id, instrument_id=gone.id)

    # An equity with same-day trades: the D2 buy nets with the D2 sell (remainder on the buy
    # side), the D3 sell takes the lots, and the D4 pair leaves a remainder on the sell side.
    _txn(session, user.id, eq.id, "buy", D1, "10", 100_000)
    _txn(session, user.id, eq.id, "buy", D2, "10", 120_000)
    _txn(session, user.id, eq.id, "sell", D2, "4", 52_000, 40)
    _txn(session, user.id, eq.id, "sell", D3, "12", 180_000)
    _txn(session, user.id, eq.id, "buy", D4, "3", 39_000)
    _txn(session, user.id, eq.id, "sell", D4, "5", 70_000)
    rebuild_realised_lots(session, user_id=user.id, instrument_id=eq.id)

    holdings = {h.instrument_id: h for h in compute_holdings(session, user_id=user.id)}
    assert holdings[eq.id].net_units == Decimal("2")
    assert gone.id not in holdings

    for inst in (inr, usd, gone, eq):
        txns = list(
            session.scalars(
                select(InvestmentTransaction).where(InvestmentTransaction.instrument_id == inst.id)
            )
        )
        by_id = {t.id: t for t in txns}
        matches = [r for r in _rows(session) if r.sell_txn_id in by_id]

        # F11 side: remaining per acquisition row from the persisted matches only.
        derived: dict[int, tuple[Decimal, int]] = {}
        for t in txns:
            if t.transaction_type not in ("buy", "sip", "switch_in", "bonus"):
                continue
            cost = (
                0 if t.transaction_type == "bonus" else t.amount_native_paise + t.fees_native_paise
            )
            taken = [r for r in matches if r.buy_txn_id == t.id]
            left_units = t.units - sum((r.units for r in taken), Decimal(0))
            left_cost = cost - sum(r.cost_native_paise for r in taken)
            if left_units > 0:
                derived[t.id] = (left_units, left_cost)
        assert derived == _surviving_lots(session, inst)

        # Every sell's shares sum back to its gross amount and fees.
        for t in txns:
            if t.transaction_type in ("sell", "switch_out"):
                mine = [r for r in matches if r.sell_txn_id == t.id]
                assert sum(r.proceeds_native_paise for r in mine) == t.amount_native_paise
                assert sum(r.sell_fees_native_paise for r in mine) == t.fees_native_paise

        if inst.id in holdings:
            h = holdings[inst.id]
            assert sum(u for u, _ in derived.values()) == h.net_units
            assert sum(c for _, c in derived.values()) == h.invested_native_paise
            assert (
                sum(to_inr_paise(c, by_id[i].fx_rate_to_inr) for i, (_, c) in derived.items())
                == h.invested_inr_paise
            )
        else:
            assert derived == {}

        # Freshness: stored rows equal a fresh replay's matches.
        out = _ReplayOut(matches=[], uncovered=[])
        _replay(
            sorted(txns, key=lambda x: (x.date, x.id)),
            nets_intraday=inst.asset_class == "indian_equity",
            out=out,
        )
        assert [
            (m.sell_txn_id, m.buy_txn_id, m.units, m.cost_native_paise, m.kind) for m in out.matches
        ] == [(r.sell_txn_id, r.buy_txn_id, r.units, r.cost_native_paise, r.kind) for r in matches]
