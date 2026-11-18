"""Tests for install_string_length_guard (ADR-0001, backlog D9)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Account, Base
from tests.string_length_guard import install_string_length_guard


def test_string_length_guard_passes_valid_strings() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    install_string_length_guard(factory)

    with factory() as session:
        acc = Account(
            user_id=uuid.uuid4(),
            name="Valid Name",
            type="bank",
            currency="INR",
        )
        session.add(acc)
        session.flush()
        assert acc.id is not None


def test_string_length_guard_refuses_overflow() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    install_string_length_guard(factory)

    with factory() as session:
        acc = Account(
            user_id=uuid.uuid4(),
            name="x" * 129,  # Account.name is String(128)
            type="bank",
            currency="INR",
        )
        session.add(acc)
        with pytest.raises(AssertionError, match="string length overflow .*Account.name"):
            session.flush()


def test_string_length_guard_refuses_overflow_on_update() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    install_string_length_guard(factory)

    with factory() as session:
        acc = Account(
            user_id=uuid.uuid4(),
            name="Valid Name",
            type="bank",
            currency="INR",
        )
        session.add(acc)
        session.flush()

        acc.name = "x" * 129
        with pytest.raises(AssertionError, match="string length overflow .*Account.name"):
            session.flush()
