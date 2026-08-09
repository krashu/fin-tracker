"""Credit-card ``_classify`` behaviour, shared and divergent.

Both CC parsers own a private ``_classify(description, is_credit) -> TxnType``, and
the snapshot fixtures only reach whichever tokens happen to appear in them. That is
how commit 5515c5c's CHARGEBACK regression row landed on the Axis fixture ONLY,
leaving the ICICI twin's credit-that-is-neither-payment-nor-refund fallthrough
(``icici_cc.py:41``) unexecuted for a token both parsers claim to handle. A table
test over the classifiers directly closes that class of gap for good: adding a
token here exercises both, and no fixture edit can silently cover one twin only.

The two bodies are deliberately NOT unified — 5515c5c's own commit message
sanctions per-issuer regex divergence as the first tuning knob, and all three
review judges independently agreed. So this module has two halves:

* :func:`test_shared_classification` — tokens both parsers must agree on. A change
  that breaks parity on any of these is a bug in whichever one moved.
* the per-parser divergence tables — the four inputs where they legitimately
  DISAGREE, pinned as intended behaviour. Before this, that divergence lived only
  in a commit message; now a "harmless" attempt to align the two regex sets fails
  here and has to be argued for rather than merged by accident.
"""

from __future__ import annotations

import pytest

from app.parsers.axis_cc import _classify as axis_classify
from app.parsers.base import TxnType
from app.parsers.icici_cc import _classify as icici_classify

# (description, is_credit, expected) — must hold for BOTH parsers.
_SHARED: list[tuple[str, bool, TxnType]] = [
    # Payments: the one credit spelling both _PAYMENT_RE patterns carry.
    ("PAYMENT RECEIVED", True, "payment"),
    ("PAYMENT RECEIVED, THANK YOU", True, "payment"),
    # Refund tokens: identical _REFUND_RE on both sides.
    ("REFUND SENTINEL MERCHANT", True, "refund"),
    ("REVERSAL SENTINEL ECOM", True, "refund"),
    ("CHARGEBACK SENTINEL MERCHANT", True, "refund"),
    # Case-insensitivity is explicit on every pattern.
    ("refund sentinel merchant", True, "refund"),
    # An unrecognised CREDIT is `other`, never income. This is the branch the ICICI
    # fixture never reached.
    ("CASHBACK SENTINEL", True, "other"),
    ("SENTINEL UNKNOWN CREDIT", True, "other"),
    # Non-purchase debits both _NON_PURCHASE_DEBIT_RE patterns share.
    ("FINANCE CHARGE", False, "other"),
    ("INTEREST CHARGES", False, "other"),
    ("LATE PAYMENT FEE", False, "other"),
    ("GST", False, "other"),
    ("ANNUAL FEE", False, "other"),
    # Ordinary spend.
    ("SENTINEL GROCERY MUM", False, "purchase"),
    ("SENTINEL CAFE BLR", False, "purchase"),
    # A refund token on the DEBIT side is not a refund — is_credit gates the branch.
    ("REFUND SENTINEL MERCHANT", False, "purchase"),
]


@pytest.mark.parametrize(("description", "is_credit", "expected"), _SHARED)
@pytest.mark.parametrize(
    ("issuer", "classify"),
    [("axis", axis_classify), ("icici", icici_classify)],
)
def test_shared_classification(
    issuer: str,
    classify: object,
    description: str,
    is_credit: bool,
    expected: TxnType,
) -> None:
    assert callable(classify)
    assert classify(description, is_credit) == expected, (
        f"{issuer} disagreed on {description!r} (is_credit={is_credit})"
    )


# The legitimate divergences, as (description, is_credit, axis, icici). Each is a
# token present in exactly one issuer's regex set.
_DIVERGENT: list[tuple[str, bool, TxnType, TxnType]] = [
    # Only ICICI's _NON_PURCHASE_DEBIT_RE has SERVICE TAX; Axis has SERVICE CHARGE.
    ("SERVICE TAX", False, "purchase", "other"),
    # Only ICICI has a bare `CHARGE\b`, so a lone "CHARGE" is a purchase on Axis.
    ("SENTINEL CHARGE", False, "purchase", "other"),
    # Only ICICI's _PAYMENT_RE has AUTOPAY PAYMENT.
    ("AUTOPAY PAYMENT", True, "other", "payment"),
    # Only Axis's _PAYMENT_RE has THANK YOU FOR PAYMENT (ICICI wants PAYMENT THANK YOU).
    ("THANK YOU FOR PAYMENT", True, "payment", "other"),
]


@pytest.mark.parametrize(("description", "is_credit", "axis", "icici"), _DIVERGENT)
def test_per_issuer_divergence_is_intentional(
    description: str, is_credit: bool, axis: TxnType, icici: TxnType
) -> None:
    """These four inputs classify DIFFERENTLY per issuer, on purpose.

    Kept as an assertion rather than a comment so aligning the two regex sets — which
    looks like tidying — fails loudly. If a real statement justifies a change, update
    this table in the same commit and say which issuer's statement drove it.
    """
    assert axis_classify(description, is_credit) == axis
    assert icici_classify(description, is_credit) == icici
    assert axis != icici, "listed as divergent but both parsers agree — move to _SHARED"


def test_service_charge_is_shared_even_though_service_tax_is_not() -> None:
    """The near-miss worth spelling out: SERVICE CHARGE matches on both.

    Axis matches it literally; ICICI matches via its bare ``CHARGE\\b``. So the two
    agree here and diverge on SERVICE TAX one line away in the same regex — which is
    exactly the kind of asymmetry a fixture-only test would never surface.
    """
    assert axis_classify("SERVICE CHARGE", False) == "other"
    assert icici_classify("SERVICE CHARGE", False) == "other"
