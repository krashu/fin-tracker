"""Custom column types for the investment side (PRD §F7).

Money everywhere in this app is integer ``paise`` / ``cents`` — never float
(see CLAUDE.md §Working agreements). Investment rows add three *non-money*
decimals that paise can't represent: ``units`` (MF holdings carry 3-4 dp, US
fractional shares more), ``price_per_unit_native`` / ``current_nav`` (NAV ~4 dp),
and ``fx_rate_to_inr`` (~6 dp). SQLite has no exact decimal type — SQLAlchemy's
``Numeric`` round-trips Decimals through float on pysqlite (it emits a warning
to that effect), which would reintroduce exactly the precision loss the paise
discipline exists to avoid. SQLAlchemy's own guidance is to store such values as
scaled integers.

``ScaledDecimal`` does that: it stores a Python ``Decimal`` as a scaled ``int64``
(``value * scale``, round-half-even) and reads it back as an exact ``Decimal``
(``stored / scale``). Subclasses fix the scale so column declarations read
self-documentingly and the scale lives in one place.

Migration parity caveat: ``test_migration_parity.py`` compares ``str(column.type)``
between the ORM metadata and the Alembic-built schema. A ``TypeDecorator`` over
``BigInteger`` introspects as ``BIGINT`` — so the migration must declare these
columns as plain ``sa.BigInteger()`` (storage shape), NOT this decorator (Python
semantics). The scaled ``server_default`` / default also live below the decorator,
so any raw SQL default must be the *scaled int* (e.g. ``fx_rate 1.0`` → ``1000000``).
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy import BigInteger, Dialect
from sqlalchemy.types import TypeDecorator


class ScaledDecimal(TypeDecorator[Decimal]):
    """Store a ``Decimal`` as a scaled ``int64``. Subclass and set ``scale``.

    ``scale`` is the integer multiplier (e.g. ``10**8`` keeps 8 decimal places).
    ``int64`` headroom: at scale ``1e8`` the representable magnitude is
    ``~9.22e18 / 1e8 ≈ 9.2e10`` — ~92 billion units, or a NAV of ₹92 billion per
    unit; ~7 orders past anything at personal scale. At ``1e6`` (fx rate) the
    headroom is ``~9.2e12`` against rates of order ``10``.
    """

    impl = BigInteger
    cache_ok = True
    scale: int

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> int | None:
        if value is None:
            return None
        return int((value * self.scale).to_integral_value(rounding=ROUND_HALF_EVEN))

    def process_result_value(self, value: int | None, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return Decimal(value) / Decimal(self.scale)


# ``cache_ok`` is not inherited by TypeDecorator subclasses — each concrete
# type sets it. Safe here: scale is a fixed class constant, no instance state.
class Units(ScaledDecimal):
    """Holding quantity — 8 dp (covers MF 3-4 dp and fractional US shares)."""

    cache_ok = True
    scale = 10**8


class PriceNative(ScaledDecimal):
    """Per-unit price / NAV in native currency — 8 dp."""

    cache_ok = True
    scale = 10**8


class FxRate(ScaledDecimal):
    """Native→INR FX rate — 6 dp (``1.0`` for INR rows in v1)."""

    cache_ok = True
    scale = 10**6
