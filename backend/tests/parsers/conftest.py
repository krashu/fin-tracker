"""Parser-only fixtures.

The shared real-PDF + dotenv plumbing now lives in :mod:`tests.conftest`
(rootdir conftest, visible everywhere). This module keeps only the
parser-specific committed JSON sentinel fixtures plus the synthetic
encrypted-PDF byte string used by the ``InvalidPasswordError`` test.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES_ROOT = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def encrypted_pdf_bytes() -> bytes:
    """A minimal AES-encrypted PDF with user-password ``correct``.

    Built inline with pikepdf so the InvalidPasswordError path is testable
    in CI without committing a binary fixture and independently of how the
    user's real bank PDFs happen to be encrypted (some statements use
    permissions-only encryption with no user password, which pikepdf opens
    with any password — those PDFs can't exercise this branch).
    """
    import pikepdf  # local import keeps the heavy lib off the module-load path

    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    buf = io.BytesIO()
    pdf.save(buf, encryption=pikepdf.Encryption(user="correct", owner="correct"))
    return buf.getvalue()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_lines(path: Path) -> list[str]:
    """Read a summary-block text fixture as the line list ``_extract_text`` yields."""
    return path.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def axis_tables() -> list[list[list[str]]]:
    return _load_json(FIXTURES_ROOT / "axis_cc" / "tables_sample.json")


@pytest.fixture
def axis_tables_expected() -> list[dict[str, Any]]:
    return _load_json(FIXTURES_ROOT / "axis_cc" / "tables_sample.expected.json")


@pytest.fixture
def axis_flipkart_tables() -> list[list[list[str]]]:
    return _load_json(FIXTURES_ROOT / "axis_cc" / "tables_sample_flipkart.json")


@pytest.fixture
def axis_flipkart_tables_expected() -> list[dict[str, Any]]:
    return _load_json(FIXTURES_ROOT / "axis_cc" / "tables_sample_flipkart.expected.json")


@pytest.fixture
def icici_tables() -> list[list[list[str]]]:
    return _load_json(FIXTURES_ROOT / "icici_cc" / "tables_sample.json")


@pytest.fixture
def icici_tables_expected() -> list[dict[str, Any]]:
    return _load_json(FIXTURES_ROOT / "icici_cc" / "tables_sample.expected.json")


@pytest.fixture
def axis_summary_lines() -> list[str]:
    return _load_lines(FIXTURES_ROOT / "axis_cc" / "summary_sample.txt")


@pytest.fixture
def axis_summary_expected() -> dict[str, Any]:
    return _load_json(FIXTURES_ROOT / "axis_cc" / "summary_sample.expected.json")


@pytest.fixture
def icici_summary_lines() -> list[str]:
    return _load_lines(FIXTURES_ROOT / "icici_cc" / "summary_sample.txt")


@pytest.fixture
def icici_summary_expected() -> dict[str, Any]:
    return _load_json(FIXTURES_ROOT / "icici_cc" / "summary_sample.expected.json")
