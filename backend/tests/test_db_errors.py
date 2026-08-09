"""Unit tests for :mod:`app.core.db_errors.is_unique_violation`.

Focus: the whole-token SQLite matching (guards the substring-collision class from
the auto-tagging review — ``labels.user_id`` ⊂ ``transaction_labels.user_id``)
and the Postgres index-name branch.
"""

from __future__ import annotations

from app.core.db_errors import is_unique_violation


def _sqlite(cols: list[str]) -> Exception:
    """A stand-in for a SQLite IntegrityError.orig with the given failing columns."""
    return Exception("UNIQUE constraint failed: " + ", ".join(cols))


def _pg(index_name: str) -> Exception:
    """A stand-in for a Postgres IntegrityError.orig naming the violated index."""
    return Exception(
        f'duplicate key value violates unique constraint "{index_name}"\n'
        "DETAIL:  Key (user_id, name)=(...) already exists."
    )


def test_sqlite_exact_column_set_matches() -> None:
    orig = _sqlite(["labels.user_id", "labels.name"])
    assert is_unique_violation(
        orig, index_name="uq_labels_user_name", columns=["labels.user_id", "labels.name"]
    )


def test_sqlite_substring_column_does_not_collide() -> None:
    """``labels.user_id`` is a substring of ``transaction_labels.user_id`` — the
    old substring match would have mis-fired; token matching must not."""
    orig = _sqlite(["transaction_labels.user_id", "transaction_labels.label_id"])
    # The labels-name constraint check must NOT match a transaction_labels conflict.
    assert not is_unique_violation(
        orig, index_name="uq_labels_user_name", columns=["labels.user_id", "labels.name"]
    )


def test_sqlite_partial_column_overlap_does_not_match() -> None:
    """All requested columns must be present — a strict superset in the message
    with a missing requested column is not a match."""
    orig = _sqlite(["merchant_tag_map.user_id", "merchant_tag_map.merchant_normalized"])
    assert not is_unique_violation(
        orig,
        index_name="uq_merchant_tag_map_user_merchant_category",
        columns=[
            "merchant_tag_map.user_id",
            "merchant_tag_map.merchant_normalized",
            "merchant_tag_map.category_id",
        ],
    )


def test_postgres_index_name_matches() -> None:
    orig = _pg("uq_labels_user_name")
    assert is_unique_violation(
        orig, index_name="uq_labels_user_name", columns=["labels.user_id", "labels.name"]
    )


def test_non_unique_error_is_not_matched() -> None:
    orig = Exception("FOREIGN KEY constraint failed")
    assert not is_unique_violation(
        orig, index_name="uq_labels_user_name", columns=["labels.user_id", "labels.name"]
    )


def test_current_constraints_match_own_message_not_siblings() -> None:
    """Each live constraint matches its own SQLite conflict and none of the others
    (all four are token-disjoint today; this locks that in)."""
    constraints = {
        "uq_merchant_tag_map_user_merchant_category": [
            "merchant_tag_map.user_id",
            "merchant_tag_map.merchant_normalized",
            "merchant_tag_map.category_id",
        ],
        "uq_merchant_label_map_user_merchant_label": [
            "merchant_label_map.user_id",
            "merchant_label_map.merchant_normalized",
            "merchant_label_map.label_id",
        ],
        "uq_labels_user_name": ["labels.user_id", "labels.name"],
        "uq_categories_active_user_name": ["categories.user_id", "categories.name"],
    }
    for own_index, own_cols in constraints.items():
        orig = _sqlite(own_cols)
        for index_name, cols in constraints.items():
            expected = index_name == own_index
            assert is_unique_violation(orig, index_name=index_name, columns=cols) is expected, (
                f"{index_name} against {own_index}'s message"
            )
