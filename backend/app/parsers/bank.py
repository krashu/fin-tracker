"""Shared bank-statement reading: the row type, balance and amount cells, and the checks.

A bank statement prints one row per transaction with a withdrawal column, a deposit column
and a running balance. Every bank parser reads its layout into :class:`BankRow` values and
hands them to :func:`to_transactions`, which owns the rules that must not differ per bank
(ADR-0010 rules 7, 11 and 12):

* the **column** a row's amount sits in decides its sign; a sign is never inferred from the
  balance change;
* the printed running balance is then checked against that sign, row by row, and a mismatch
  raises :class:`~app.parsers.base.RunningBalanceMismatchError` rather than import a guess;
* a deposit that reads as a reversal or refund is typed ``refund``, so it imports as a
  positive ``spend`` that nets the debit it reverses (ADR-0009).

:class:`OpenRow` builds one printed row from its date line and the lines that continue it;
each parser's walker decides which lines belong to which row.

Balances are the user's position: a bank prints money held bare, so it is positive unless
``-`` or ``DR`` marks an overdraft.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

from app.parsers.base import (
    _AMOUNT_RE,
    ParserError,
    RawTransaction,
    RunningBalanceMismatchError,
    TxnType,
    _try_parse_amount,
)

# A deposit whose narration reads as a reversal: HDFC prints a returned UPI debit as
# ``REV-UPI-...`` and a merchant refund as ``REFUND``. Anything else in the deposit column
# stays a plain ``deposit`` and the user corrects it in review (ADR-0010 rule 12).
_REVERSAL_RE = re.compile(r"\bREV(?:ERSAL|ERSED)?\b|\bREFUND\b|\bCHARGEBACK\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class BankRow:
    """One printed transaction row, before typing.

    ``withdrawal_paise`` and ``deposit_paise`` are magnitudes (``>= 0``); exactly one is
    normally non-zero. ``balance_paise`` is the printed running balance in the user's
    position. ``page`` and ``row`` are 1-based: the PDF page and the row's ordinal on it.
    """

    date: date
    narration: str
    withdrawal_paise: int
    deposit_paise: int
    balance_paise: int
    page: int
    row: int


def parse_bank_balance(text: str) -> int | None:
    """A printed bank balance in the user's position, or ``None`` if it is not an amount.

    Negative iff there is a leading ``-`` or a ``DR`` marker (an overdraft); a bare figure
    or a ``CR`` marker is money held.
    """
    match = _AMOUNT_RE.match(text.strip())
    if match is None:
        return None
    parsed = _try_parse_amount(match.group("num"))
    if parsed is None:
        return None
    negative = match.group("sign") == "-" or (match.group("cr") or "").upper() == "DR"
    return -parsed[0] if negative else parsed[0]


def parse_bank_amount(cell: str) -> int:
    """A withdrawal or deposit cell as a magnitude in paise; ``""`` means none (0).

    Raises:
        ParserError: the cell is neither empty nor an unsigned amount. The message never
            quotes the cell.
    """
    if not cell.strip():
        return 0
    match = _AMOUNT_RE.match(cell.strip())
    parsed = _try_parse_amount(match.group("num")) if match else None
    if match is None or parsed is None or match.group("sign") or match.group("cr"):
        raise ParserError("cannot read an amount cell")
    return parsed[0]


def _join_spaced(parts: Sequence[str]) -> str:
    return " ".join(" ".join(parts).split())


@dataclass(slots=True)
class OpenRow:
    """A transaction being built from its date line and the lines that continue it.

    A parser's walker decides which printed lines belong to the row and feeds each one to
    :meth:`absorb`; :meth:`close` then checks it and returns the :class:`BankRow`. ``date`` may
    be set late (SBI reads the year off the next line); closing without one is an error.
    """

    date: date | None
    page: int
    row: int
    narration: list[str] = field(default_factory=list)
    withdrawal: str = ""
    deposit: str = ""
    balance: str = ""

    def _unreadable(self) -> ParserError:
        return ParserError(f"cannot read row on page {self.page}, row {self.row}")

    def absorb(
        self,
        text: Iterable[str],
        *,
        withdrawal: str = "",
        deposit: str = "",
        balance: str = "",
    ) -> None:
        """Add narration parts and any amount cells. A blank cell is skipped; a second value
        for an amount already read is refused."""
        self.narration.extend(text)
        if cell := withdrawal.strip():
            if self.withdrawal:
                raise self._unreadable()
            self.withdrawal = cell
        if cell := deposit.strip():
            if self.deposit:
                raise self._unreadable()
            self.deposit = cell
        if cell := balance.strip():
            if self.balance:
                raise self._unreadable()
            self.balance = cell

    def close(self, join: Callable[[Sequence[str]], str] = _join_spaced) -> BankRow:
        """The finished row. Needs a date, a readable balance and exactly one non-zero side."""
        when = self.date
        balance = parse_bank_balance(self.balance)
        withdrawal = parse_bank_amount(self.withdrawal)
        deposit = parse_bank_amount(self.deposit)
        if when is None or balance is None or (withdrawal > 0) == (deposit > 0):
            raise self._unreadable()
        return BankRow(
            date=when,
            narration=join(self.narration),
            withdrawal_paise=withdrawal,
            deposit_paise=deposit,
            balance_paise=balance,
            page=self.page,
            row=self.row,
        )


def classify_bank_row(narration: str, *, is_deposit: bool) -> TxnType:
    """The parser type for a bank row: ``withdrawal``, ``deposit``, or ``refund`` for a
    deposit whose narration reads as a reversal."""
    if not is_deposit:
        return "withdrawal"
    return "refund" if _REVERSAL_RE.search(narration) else "deposit"


def to_transactions(rows: list[BankRow], *, opening_paise: int | None) -> list[RawTransaction]:
    """Type, check and order printed rows (ADR-0010 rules 6, 11, 12).

    ``rows`` are in printed order. Each row's balance must equal the previous balance plus
    its signed amount; the first row is checked against ``opening_paise``. With no opening
    balance the check starts at the second row (a missing summary is never an error).

    Raises:
        RunningBalanceMismatchError: a printed balance does not follow from the row.
        ParserError: a row has both a withdrawal and a deposit.
    """
    previous = opening_paise
    typed: list[tuple[date, RawTransaction]] = []
    for r in rows:
        if r.withdrawal_paise > 0 and r.deposit_paise > 0:
            raise ParserError(
                f"row has both a withdrawal and a deposit on page {r.page}, row {r.row}"
            )
        signed = r.deposit_paise - r.withdrawal_paise
        if previous is not None and previous + signed != r.balance_paise:
            raise RunningBalanceMismatchError(page=r.page, row=r.row)
        previous = r.balance_paise
        txn_type = classify_bank_row(r.narration, is_deposit=r.deposit_paise > 0)
        txn = RawTransaction(
            date=r.date, amount_paise=signed, merchant_raw=r.narration, txn_type=txn_type
        )
        typed.append((r.date, txn))
    # Printed order is already page, then row, so a stable sort on the date alone yields
    # the (date, page, row) order of ADR-0010 rule 6.
    typed.sort(key=lambda t: t[0])
    return [t[1] for t in typed]
