# ADR-0010: A statement parser returns one `ParsedStatement` — rows plus an always-present summary

**Status**: Accepted
**Date**: 2026-08-12 (decided) · **Revised**: 2026-11-17
**Scope**: PRD §F1 (parser contract), §F4a case 5 · `app/parsers/base.py` (`StatementParser`,
`ParsedStatement`, `StatementSummary`, `_decrypt`, `_extract_tables`, `_extract_word_table`, `_extract_text`),
`app/parsers/axis_cc.py`, `app/parsers/icici_cc.py`, `app/parsers/bank.py` (the shared bank-row
contract), `app/parsers/hdfc_bank.py`, `app/parsers/icici_bank.py`, `app/parsers/sbi_bank.py`,
`app/parsers/axis_bank.py`, `app/api/v1/imports.py`, `app/api/uploads.py` (`read_multipart`), `app/services/import_service.py` (`PARSERS`,
`import_statement`), `app/services/reconciliation_service.py` (`reconcile_batch`, `is_card_bill_debit`)
**Supersedes / Related**: ADR-0005 (`ty` enforces the Protocol) · ADR-0006 (rows feed the fingerprint)

## Decision

Every PDF spending-statement parser, card or bank, has a `parse(pdf_bytes, password)` that returns one frozen
`ParsedStatement(rows, summary)`. `summary` is a [statement summary](../../GLOSSARY.md#transactions--import)
that is always present, and each of its four fields is independently optional. A missing summary block
is a legitimate all-`None` result, never an error.

## Rules

1. **The Protocol is `parse`, `interpret_tables` and `interpret_summary`.**
   `parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement`;
   `interpret_tables(tables) -> list[RawTransaction]`;
   `interpret_summary(lines: Sequence[str]) -> StatementSummary`. All three are declared on
   `StatementParser`, so a parser missing one fails `ty`. — *Enforced by*: tooling (`ty` over
   `PARSERS: dict[..., type[StatementParser]]`) · *Locked by*: `uv run python -m ty check app`
2. **`ParsedStatement` and `StatementSummary` are `@dataclass(frozen=True, slots=True)`.** The summary
   fields are `opening_balance_paise`, `closing_balance_paise: int | None` and `period_start`,
   `period_end: date | None`, all defaulting to `None`. — *Enforced by*: schema · *Locked by*: none
   (structural)
3. **`summary` is never `None`.** A layout with no readable summary block yields `StatementSummary()`,
   not a `ParserError`. — *Enforced by*: service · *Locked by*:
   `tests/parsers/test_axis_cc.py::test_interpret_summary_no_labels_returns_all_none`,
   `tests/parsers/test_icici_cc.py::test_interpret_summary_no_labels_returns_all_none`,
   `tests/services/test_import_service.py::test_no_summary_block_leaves_metadata_and_delta_null`
4. **`parse()` composes both paths over one decrypted buffer**: `_decrypt`, then a table source →
   `interpret_tables` for rows and `_extract_text` → `interpret_summary` for the summary. The table
   source is `_extract_tables`, or `_extract_word_table` (a positioned-word reader) for a layout with
   no ruled table; both yield `pages → rows → cells`, rows are still typed by column (rule 12), and a
   sign never comes from the balance delta. — *Enforced by*: service · *Locked by*: the `real_pdf`
   parser tests, `tests/parsers/test_base.py::test_extract_word_table_assigns_words_to_header_columns`,
   `::test_extract_word_table_reads_a_page_whose_stop_line_sits_above_its_header`
5. **A zero-row statement with a summary is valid.** `rows=[]` with any summary field set imports as a
   0-row batch whose balance delta is still checked. `ParserError` is raised only when there are no
   rows **and** an all-`None` summary. A bank parser that never finds its transaction-table header
   raises `ParserError("cannot find the transaction table")` whatever the summary holds: an
   unrecognised layout is never a 0-row batch. A header with no rows under it is a valid 0-row
   statement. Zero-amount rows are dropped before dedup (ADR-0009 rule 3) and counted in
   `ImportSummary.zero_amount_count`, never in `skipped`, so the upload screen reads a zero-only
   statement as one with no transactions. A zero-only statement is never refused, whatever its
   summary holds: it has rows, so its layout was recognised. — *Enforced by*: service · *Locked by*:
   `tests/parsers/test_axis_cc.py::test_zero_rows_with_summary_is_a_valid_statement`,
   `tests/parsers/test_hdfc_bank.py::test_parse_without_a_table_header_raises`,
   `::test_header_with_no_rows_is_an_empty_statement`,
   `tests/parsers/test_icici_bank.py::test_parse_without_a_table_header_raises`,
   `tests/parsers/test_sbi_bank.py::test_interpret_tables_without_a_header_raises`,
   `tests/parsers/test_axis_bank.py::test_statement_without_a_table_header_is_refused`,
   `::test_zero_rows_with_summary_is_a_valid_statement`,
   `tests/services/test_import_service.py::test_zero_amount_rows_are_not_counted_as_already_imported`,
   `frontend/app/imports/statements/upload-form.test.tsx` ("says a statement of only zero-amount
   lines has no transactions, not that they are already in expenses")
6. **Rows are ordered `(date, page_index, row_index)`.** — *Enforced by*: service · *Locked by*: the
   parser snapshot tests
7. **Summary balances are the user's position**: positive is money held, negative is owed. A card
   balance is negative unless its `CR` marker makes it positive (PRD §F4a-5); a bank balance is
   positive unless overdrawn. So `opening + Σ rows = closing` holds for both. — *Enforced by*: service · *Locked by*:
   `tests/parsers/test_axis_cc.py::test_interpret_summary_cc_sign_credit_marker_is_positive`,
   `tests/parsers/test_icici_cc.py::test_interpret_summary_cc_sign_credit_marker_is_positive`,
   `tests/services/test_reconciliation_service.py::test_bank_statement_reconciles_with_positive_balances`
8. **`ImportBatch.period_*` holds only the [statement period](../../GLOSSARY.md#transactions--import)
   as printed.** When none is printed, reconciliation computes its window (min/max row dates) at
   reconcile time and never writes it back; the UI shows "period not printed". — *Enforced by*:
   service · *Locked by*:
   `tests/services/test_reconciliation_service.py::test_window_fallback_is_not_stored`,
   `frontend/components/review/statement-period.test.tsx` "says the period was not printed when the
   statement printed none"
9. **Consumers treat a missing opening or closing balance as "not checked"**
   (`reconciliation_delta_paise = None`), never as an error. The importer stamps all four fields onto
   `ImportBatch` on every upload, re-uploads included. A bank layout that prints no closing figure
   (ICICI, SBI netbanking) is not "missing": its closing balance is the last running balance the
   statement prints, read from its text independently of the table, so reconciliation still catches
   a row the table read dropped. — *Enforced by*: service · *Locked by*:
   `tests/services/test_import_service.py::test_reconciliation_metadata_stamped_and_clean_import_reconciles`,
   `::test_reupload_restamps_metadata_and_recomputes_the_delta`,
   `tests/parsers/test_icici_bank.py::test_interpret_summary_snapshot`,
   `tests/parsers/test_sbi_bank.py::test_interpret_summary_netbanking_closing_is_the_last_printed_balance`
10. **Dispatch is solely `PARSERS[(account.issuer, account.type)]`.** Parser classes carry no
    `.issuer` or `.type` attribute. — *Enforced by*: service · *Locked by*: none

**Bank statements**

11. **A bank parser checks every row against the printed running balance**: previous balance plus the
    row's signed amount must equal the balance printed on that row. A mismatch is a `ParserError`
    naming the page and row, never an import with a guessed sign. With no readable opening balance
    the check starts at the second row (rule 3: a missing summary is never an error);
    where the layout prints the opening balance as a row of the transaction table (Axis
    `OPENING BALANCE`, ICICI `B/F`), `interpret_tables` reads it and checks the first row;
    otherwise `interpret_tables` never sees the opening balance and the first row's check runs in
    `parse`. — *Enforced by*: service ·
    *Locked by*: `tests/parsers/test_bank.py::test_without_opening_the_check_starts_at_row_two`,
    `tests/api/test_imports.py::test_running_balance_mismatch_is_a_422_naming_page_and_row`,
    `tests/parsers/test_hdfc_bank.py::test_running_balance_mismatch_raises`, and the same test for
    each bank parser; the in-table opening:
    `tests/parsers/test_icici_bank.py::test_first_row_is_checked_against_the_bf_row`,
    `tests/parsers/test_axis_bank.py::test_running_balance_mismatch_raises[current-opening]`; the
    check in `parse`: `::test_parse_checks_the_first_row_against_the_opening[above_header]`,
    `tests/parsers/test_sbi_bank.py::test_parse_checks_the_first_row_against_the_opening`
12. **Bank rows are typed by column, then corrected in review**: a debit is a negative `spend`, a
    credit a positive `income`. A credit the parser recognises as a reversal or refund (a failed UPI
    debit returned, a merchant refund) is a positive `spend`, so it nets the debit (ADR-0009). A debit
    recognised as a card-bill payment is flagged `cc_payment_candidate` for F4a; how it commits
    when left unlinked is ADR-0002 rule 7b. — *Enforced by*:
    service · *Locked by*: `tests/parsers/test_hdfc_bank.py::test_failed_upi_reversal_is_a_positive_spend`,
    `::test_card_bill_debit_is_a_payment_candidate`,
    `tests/api/test_imports_review.py::test_candidates_flag_a_bank_card_bill_debit`

**Every upload**

13. **An uploaded file, its extracted text and its password live only in request memory.** The statement
    route reads its multipart body itself: the file stays in memory, and a body over the upload cap is
    refused with 413 as soon as the declared length or the running byte count passes it. The database
    keeps `source_file_hash`, the parsed rows and the summary; a failed upload stores no batch. Nothing
    writes the bytes, the text or the password to disk, the database or a log. — *Enforced by*:
    service · *Locked by*: `tests/api/test_imports.py::test_upload_persists_no_file_text_or_password`,
    `tests/api/test_uploads.py::test_over_cap_body_is_413_before_it_is_read_in_full`

## Out of bounds

- Investment CSV, NAV and FX parsers (`investment_csv.py`, `amfi_navall.py`, `mfapi.py`,
  `frankfurter.py`) and backup restore (`backup_csv.py`): free functions with their own return types,
  not `StatementParser`.
- CSV and spreadsheet spending statements (including netbanking XLS exports): no such parser exists.
  The first one reopens rule 1 (see Consequences).
- Current accounts, overdraft limits and fixed-deposit statements: savings accounts only.
- Iterating a `ParsedStatement` as rows, or any union return: there is no compatibility shim.

## Why

Balance reconciliation (F4a-5) catches a missed page, a duplicated import, or a false-positive dedup that
silently dropped a row. It needs the opening and closing balance and the period, which a statement prints
once beside its rows. A bare `list[RawTransaction]` had nowhere to put them, and widening `parse()`'s
return type is a one-way door across every parser, so the shape had to be right before reconciliation
built on it.

The summary nests inside `ParsedStatement` rather than flattening onto it so that
`interpret_summary(lines)` has its own return type and can be fixtured and snapshot-tested without a PDF.
An always-present summary makes `opening is None or closing is None` the entire reconciliation gate, with
no second `None` check.

A card with no spend in a month still gets a statement with balances; refusing it hid exactly the case
reconciliation should confirm. And a period inferred from row dates is not what the issuer printed, so it
must not be stored as if it were.

A bank statement prints a running balance on every row, which is a free per-row check. A debit read as
a credit doubles the error in the closing balance but cannot say which row is wrong; the running
balance pins it to the row. Indian bank narrations are terse (`UPI/…/REV`), so the parser's guess at a
type is a default the review screen corrects, not a classification.

Statements carry names, addresses, account numbers and sometimes PAN or Aadhaar. Keeping only the
parsed rows is data minimisation under the DPDP Act, and it means no stored file can leak later.

## Alternatives rejected

- **A second optional `parse_summary` classmethod** — a second decrypt pass, a `hasattr` check `ty`
  cannot enforce, and two calls that can disagree about which file they read.
- **A separate `BalanceExtractor` table keyed on `(issuer, type)`** — a second dispatch table with the
  silent-miss failure of an unwired router, and abstraction ahead of a second use.
- **Metadata on a mutable class attribute set by `parse()`** — hidden shared state on a stateless class.
- **Only `parse` on the Protocol, the split by convention** — a new parser could skip
  `interpret_summary` and `ty` would pass it.
- **Storing the fallback window in `period_*`** — the UI would show a period the statement never
  printed.
- **Keeping the uploaded PDF for re-parsing** — a store of the most sensitive documents the app ever
  sees, for a convenience a re-upload already gives.
- **A second sign convention for bank summaries** — two formulas for one reconciliation check.

## Consequences

- Each parser implements three methods; one with no summary block still defines `interpret_summary`
  returning the default.
- The table-shaped Protocol assumes PDF tables. **Reopen** when the first non-table parser (a CSV
  spending statement) lands: split a format-agnostic `parse` contract from a table-parser layer.

## History

- 2026-08-12 — Accepted: `parse()` returns `ParsedStatement`, changed atomically across both parsers and
  the one consumer, with no shim.
- 2026-11-15 — Rewritten to the rules-first template. Scope narrowed to PDF spending parsers. All three
  methods now sit on the Protocol. A zero-row statement with a summary imports. `period_*` holds only
  what was printed.
- 2026-11-15 — Extended to bank statements (HDFC, ICICI, SBI, Axis savings): one sign convention for
  summaries, a per-row running-balance check, column typing. Added rule 13: uploads are never persisted.
- 2026-11-15 — Rule 13: the statement route streams its body under the cap and never spools to disk; a
  failed upload stores no batch; ImportBatch.error_message dropped (migration `0042_drop_import_batch_error_message`).
- 2026-11-16 — Rule 11: with no readable opening balance the running-balance check starts at the second row.
- 2026-11-16 — Rule 4: rows may come from `_extract_word_table` where a layout has no ruled table.
- 2026-11-16 — Rule 12: `cc_payment_candidate` also flags a bank debit naming a card-bill payment (`is_card_bill_debit`), with its review-queue renderer.
- 2026-11-16 — ICICI (current and pre-2020), SBI (netbanking, e-account, YONO) and Axis (current and legacy)
  savings parsers shipped; an SBI e-account file holding more than one account section is refused.
- 2026-11-16 — Rule 5: a bank parser that never finds its table header refuses the file; a header with no
  rows is a valid 0-row statement (HDFC changed to match).
- 2026-11-16 — Rule 9: a bank layout with no printed closing figure closes at its last printed running balance.
- 2026-11-16 — Rule 11: an opening balance printed as a table row seeds the first-row check in `interpret_tables`.
- 2026-11-16 — Rule 5: zero-amount rows are counted in `zero_amount_count`, apart from duplicates, and a
  zero-only statement is never refused (A63).
- 2026-11-16 — Rule 4: a stop line above a later page's own column header no longer hides that page; the
  word reader skips everything above a page's own header (A64).
- 2026-11-17 — Rule 12: an unlinked card-bill debit commits as ADR-0002 rule 7b says (A61).
