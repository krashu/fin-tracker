"""Parser-only fixtures.

The shared real-PDF + dotenv plumbing now lives in :mod:`tests.conftest`
(rootdir conftest, visible everywhere). This module keeps only the
parser-specific committed JSON sentinel fixtures plus the synthetic
encrypted-PDF byte string used by the ``InvalidPasswordError`` test and a
synthetic text-only PDF factory for the zero-row statement tests.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
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


@pytest.fixture
def text_pdf_bytes() -> Callable[[list[str]], bytes]:
    """Factory: a one-page PDF printing ``lines`` as plain text, no tables.

    pdfplumber's ``extract_text`` gives the lines back and ``extract_tables``
    finds nothing, so ``parse()`` sees a statement with a summary block but no
    transaction table. ``[]`` builds a blank page. Built in memory with pikepdf
    (Helvetica, one ``Tj`` per line) so no binary fixture is committed.
    """
    import pikepdf  # local import keeps the heavy lib off the module-load path

    def build(lines: list[str]) -> bytes:
        pdf = pikepdf.new()
        pdf.add_blank_page(page_size=(612, 792))
        page = pdf.pages[0]
        page.Resources = pikepdf.Dictionary(
            Font=pikepdf.Dictionary(
                F1=pikepdf.Dictionary(
                    Type=pikepdf.Name.Font,
                    Subtype=pikepdf.Name.Type1,
                    BaseFont=pikepdf.Name.Helvetica,
                )
            )
        )
        shown = " 0 -14 Td ".join(
            "(" + line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj"
            for line in lines
        )
        page.Contents = pdf.make_stream(f"BT /F1 10 Tf 72 720 Td {shown} ET".encode("latin-1"))
        buf = io.BytesIO()
        pdf.save(buf)
        return buf.getvalue()

    return build


@pytest.fixture
def positioned_pdf_bytes() -> Callable[[list[list[tuple[float, float, str]]]], bytes]:
    """Factory: a PDF whose pages print each ``(x, y, text)`` at that position.

    ``y`` is measured from the page bottom, as PDF does. One ``Td``/``Tj`` per string, in
    Helvetica 8 pt, so ``extract_words`` sees real positioned words and a word-table reader
    can be tested on column geometry without a binary fixture. One list per page.
    """
    import pikepdf  # local import keeps the heavy lib off the module-load path

    def build(pages: list[list[tuple[float, float, str]]]) -> bytes:
        pdf = pikepdf.new()
        font = pikepdf.Dictionary(
            Font=pikepdf.Dictionary(
                F1=pikepdf.Dictionary(
                    Type=pikepdf.Name.Font,
                    Subtype=pikepdf.Name.Type1,
                    BaseFont=pikepdf.Name.Helvetica,
                )
            )
        )
        for strings in pages:
            pdf.add_blank_page(page_size=(612, 792))
            page = pdf.pages[-1]
            page.Resources = font
            shown = " ".join(
                f"BT /F1 8 Tf {x} {y} Td ("
                + text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                + ") Tj ET"
                for x, y, text in strings
            )
            page.Contents = pdf.make_stream(shown.encode("latin-1"))
        buf = io.BytesIO()
        pdf.save(buf)
        return buf.getvalue()

    return build


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_lines(path: Path) -> list[str]:
    """Read a summary-block text fixture as the line list ``_extract_text`` yields."""
    return path.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def hdfc_bank_fixture() -> Callable[[str], Any]:
    """Loader for ``tests/fixtures/hdfc_bank/<name>``: parsed JSON for ``*.json``, the line
    list for ``*.txt`` (the shape ``_extract_text`` yields)."""

    def load(name: str) -> Any:
        path = FIXTURES_ROOT / "hdfc_bank" / name
        return _load_json(path) if name.endswith(".json") else _load_lines(path)

    return load


@pytest.fixture
def axis_bank_fixture() -> Callable[[str], Any]:
    """Loader for ``tests/fixtures/axis_bank/<name>``: parsed JSON for ``*.json``, the line
    list for ``*.txt`` (the shape ``_extract_text`` yields)."""

    def load(name: str) -> Any:
        path = FIXTURES_ROOT / "axis_bank" / name
        return _load_json(path) if name.endswith(".json") else _load_lines(path)

    return load


@pytest.fixture
def icici_bank_fixture() -> Callable[[str], Any]:
    """Loader for ``tests/fixtures/icici_bank/<name>``: parsed JSON for ``*.json``, the line
    list for ``*.txt`` (the shape ``_extract_text`` yields)."""

    def load(name: str) -> Any:
        path = FIXTURES_ROOT / "icici_bank" / name
        return _load_json(path) if name.endswith(".json") else _load_lines(path)

    return load


@pytest.fixture
def sbi_bank_fixture() -> Callable[[str], Any]:
    """Loader for ``tests/fixtures/sbi_bank/<name>``, the same shape as ``hdfc_bank_fixture``."""

    def load(name: str) -> Any:
        path = FIXTURES_ROOT / "sbi_bank" / name
        return _load_json(path) if name.endswith(".json") else _load_lines(path)

    return load


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
