"""CI guard: refuse fixtures containing obvious PII patterns.

PAN + card regexes import from :mod:`app.core.pii_patterns` so they
stay in sync with the structured-log scrubber. Email + phone patterns
are fixture-only (no parity requirement with log scrubbing) and stay
inline.

The standalone script ``scripts/redact_fixture.py`` keeps its own copies
of all four patterns to preserve its stdlib-only invariant. The card + PAN
halves are pinned equal to the canonical ones by
:func:`test_redact_script_pattern_parity` below — a gate, not a comment.

What this does NOT catch: names, addresses, bank account numbers, Aadhaar
(too generic to detect without false positives). Those still rely on the
eyeball-before-commit rule in CLAUDE.md.

Failure messages report pattern name + offset only — never the matched
value. Echoing the secret into CI logs (which is where AssertionError
messages land) would turn the guard into a leak path; see the unit test
below for the regression guarantee.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

from app.core.pii_patterns import CARD_RE, PAN_RE

FIXTURE_ROOT = Path(__file__).parent / "fixtures"

PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("card-number", CARD_RE),
    ("pan", PAN_RE),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("phone-in", re.compile(r"\+91[-\s]?\d{10}\b")),
]

# Sentinel values written by scripts/redact_fixture.py — allowed to appear.
SENTINELS = {"0000-0000-0000-0000", "AAAAA0000A", "user@example.com", "+91-0000000000"}

TEXT_SUFFIXES = {".csv", ".json", ".txt", ".md", ".yaml", ".yml", ".tsv", ""}


def _scan_for_pii(root: Path) -> list[str]:
    """Return a list of failure descriptors (pattern + offset, no values).

    Pure function: takes a root, returns strings, never raises. Pulled out
    of the test body so the unit test below can drive it against a synthetic
    plant without triggering AssertionError.
    """
    failures: list[str] = []
    if not root.exists():
        return failures
    for fixture in root.rglob("*"):
        if not fixture.is_file() or fixture.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = fixture.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary file; redact extracted text instead
        for name, pattern in PII_PATTERNS:
            for match in pattern.finditer(text):
                if match.group(0) in SENTINELS:
                    continue
                rel = fixture.relative_to(root)
                failures.append(f"{rel} :: {name} at offset {match.start()}")
    return failures


def test_fixtures_have_no_pii() -> None:
    failures = _scan_for_pii(FIXTURE_ROOT)
    assert not failures, (
        "PII pattern matched in fixtures:\n  "
        + "\n  ".join(failures)
        + "\nRun: python scripts/redact_fixture.py --inplace <file>"
    )


def _load_redact_script() -> ModuleType | None:
    """Import ``scripts/redact_fixture.py`` by path, or None if not found.

    The script lives at the REPO root, not under ``backend/``, and pytest runs
    with ``rootdir=backend`` and no ``pythonpath`` entry for the parent — so a
    plain ``import scripts.redact_fixture`` cannot resolve. Anchor the walk on
    ``.env.example`` the way ``tests/conftest._find_dotenv`` does.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / ".env.example").is_file():
            script = parent / "scripts" / "redact_fixture.py"
            if not script.is_file():
                return None
            spec = importlib.util.spec_from_file_location("_redact_fixture", script)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    return None


def test_redact_script_pattern_parity() -> None:
    """``scripts/redact_fixture.py`` holds hand-copied patterns; pin them equal.

    The script keeps its own copies to preserve a stdlib-only invariant (it must
    run without the backend venv), so the duplication is deliberate and cannot be
    removed. This test is what makes it safe: widening a regex in
    :mod:`app.core.pii_patterns` without widening the script fails CI instead of
    silently letting a fixture through un-redacted.

    Parity covers card + PAN only: those are the two patterns
    ``app.core.pii_patterns`` owns. Email / phone are fixture-only (declared as
    such in this module's docstring) and have no canonical counterpart, so only
    their sentinel replacements are pinned, via ``SENTINELS``.
    """
    script = _load_redact_script()
    if script is None:  # pragma: no cover - only when run outside a full checkout
        pytest.skip("scripts/redact_fixture.py not found next to .env.example")

    by_name = {name: (pattern, replacement) for name, pattern, replacement in script.PII_PATTERNS}

    assert by_name["card-number"][0].pattern == CARD_RE.pattern
    assert by_name["pan"][0].pattern == PAN_RE.pattern
    # Every replacement the script writes must be an allowed sentinel here, or
    # the guard would flag files the script just redacted.
    assert {replacement for _, replacement in by_name.values()} == SENTINELS


def test_failure_message_excludes_planted_secret(tmp_path: Path) -> None:
    """The guard must NOT echo the matched value into failure descriptors.

    Plants a synthetic PAN-shaped string in a temp "fixture" and confirms
    the scan reports the leak by pattern name + offset only. Defends the
    previous shape where ``match.group(0)`` landed in AssertionError
    messages and from there into CI logs.
    """
    synthetic_pan = "FAKEX1234Z"
    (tmp_path / "planted.txt").write_text(synthetic_pan, encoding="utf-8")

    failures = _scan_for_pii(tmp_path)

    assert len(failures) == 1
    assert synthetic_pan not in failures[0]
    assert "pan" in failures[0]
