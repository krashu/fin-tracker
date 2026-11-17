# ADR-0015: Sells consume lots first-in-first-out, after same-day equity trades net out

**Status**: Accepted
**Date**: 2026-11-15 (decided) · **Revised**: 2026-11-15
**Scope**: PRD §F7 (transaction-type rules, realised-gain classification), §F11 ·
`app/services/holdings_service.py` (`compute_holdings`, `_replay`, `_apply`, `_consume_fifo`, `_Lot`),
`app/services/investment_import_service.py` (persist loop, per-instrument coverage check),
`app/api/v1/investment_transactions.py` (reinvestment endpoint), `app/models/investment_transaction.py`
(`ix_investment_transactions_user_instrument_date`), `app/models/realised_lot.py`
**Supersedes / Related**: ADR-0014 (consumes the lot-match), ADR-0016 (each lot keeps its own rate),
ADR-0002 (IDCW and switch pairs)

## Decision

A `sell` or `switch_out` consumes an instrument's open lots oldest first, for both cost basis and
holding period. Lots acquired on the same date are ordered by their position in the source statement
(the CAS or broker file), then by insertion id. One replay order serves holdings, the oversell check on every write and the F11 capital-gains
statement. For Indian listed shares, same-day buys and sells
of one instrument net against each other first, as intraday trades, and only the remainder reaches
FIFO.

## Rules

1. **Every acquisition opens its own lot**, carrying its date, units, native cost (amount + fees) and
   its stamped FX rate: `buy`, `sip`, `switch_in`, and the `buy` leg of an IDCW reinvestment. A `bonus`
   opens a zero-cost lot dated the bonus date. A `split` rescales earlier lots in place and keeps their
   dates. — *Enforced by*: service (`_apply`) ·
   *Locked by*: `tests/services/test_holdings_service.py::test_reinvest_lot_is_separate_from_the_funding_lot`,
   `::test_bonus_adds_units_at_zero_cost`, `::test_switch_in_adds_and_switch_out_consumes_fifo`
2. **A `sell` or `switch_out` consumes from the oldest open lot.** The boundary lot releases a
   proportional share of its remaining cost, rounded half-even to whole paise. — *Enforced by*: service
   (`_consume_fifo`) · *Locked by*: `tests/services/test_holdings_service.py::test_partial_sell_releases_fifo_cost_basis`
3. **Replay order is acquisition date, then source position, then id.** Source position is carried
   by insertion order, so three writers keep it:
   - the CSV importer persists rows in file order;
   - the reinvestment endpoint inserts the `dividend` leg before the `buy` leg;
   - any future investment export writes rows in `(date, id)` order, so a round trip keeps it.

   The stored sort key is therefore `(date, id)`, backed by `ix_investment_transactions_user_instrument_date`.
   — *Enforced by*: service · *Locked by*:
   `tests/services/test_holdings_service.py::test_same_date_tiebreak_consumes_lower_id_first`,
   `tests/services/test_investment_import_service.py::test_rows_persist_in_file_order`
4. **Every consumer uses the same replay.** Holdings, the oversell check (rule 9), the `realised_lots`
   rebuild and F11 never order or match lots on their own; F11 reads the persisted match (rule 5)
   instead of replaying. — *Enforced by*: service (`holdings_service._replay`, `_apply`,
   `_REPLAY_ORDER`) ·
   *Locked by*: `tests/services/test_realised_lots.py::test_f11_and_holdings_agree_on_remaining_lots`
5. **The match is persisted.** Each `sell` or `switch_out` writes one `realised_lots` row per lot it
   consumed and one per same-day buy it netted against (rule 7), each marked `kind` `fifo` or
   `intraday`: the buy row, units, the cost released from the lot (or the netted buy's share of its
   cost), the buy and sell dates, and the row's share of the sell's gross consideration
   (`amount_native_paise`) and of its fees (`fees_native_paise`, the transfer expenses), all in native
   paise. A sell's shares are allocated in replay order, intraday matches first, each the half-even
   proportional share of what remains by units, so the last row takes the remainder and the shares sum
   exactly to the sell's amount and fees. The rows for an instrument are rebuilt on every write to it,
   so F11 recovers gain, transfer expenses and holding period per sold lot without replaying; its
   capital-gains statement reads only `fifo` rows, and rule 8's intraday listing reads the `intraday`
   rows. — *Enforced by*: schema and service (`holdings_service.rebuild_realised_lots`) ·
   *Locked by*: `tests/services/test_realised_lots.py::test_sell_records_each_consumed_lot`,
   `::test_sell_shares_sum_to_the_sell_totals`, `::test_intraday_is_not_a_capital_gain`
6. **Holding period runs from the lot's own acquisition date to the sell date.** A bonus lot starts at
   the bonus date; a split keeps the original date; an IDCW reinvestment starts at the reinvestment
   date. — *Enforced by*: service · *Locked by*: PRD §Verification 10
7. **Same-day trades of an `indian_equity` instrument net first.** On each date, the lesser of the
   day's bought and sold units is [intraday](../../GLOSSARY.md#investments): the day's buys are matched
   to its sells in source order, open no lot, and consume none. Only the residual buy opens a lot
   (rule 1) or the residual sell consumes FIFO (rule 2). Trade times are not needed; the netting is by
   date, which is how Indian brokers report it. — *Enforced by*: service
   (`holdings_service._replay`) · *Locked by*:
   `tests/services/test_holdings_service.py::test_same_day_buy_and_sell_net_before_fifo`,
   `tests/api/test_investment_transactions.py::test_reinvestment_buy_leg_nets_intraday_on_indian_equity`
8. **Intraday results are reported apart from capital gains**: F11 lists each intraday match (units,
   buy and sell value, profit or loss) as speculative business income and never adds it to a
   capital-gains bucket. Rule 9's replay nets the day first, so a same-day sell listed before its buy
   in an imported file is not an oversell. Manual entry writes one row at a time, so a sell posted
   before its same-day buy is still refused (422): the buy does not exist yet. — *Enforced by*:
   service (`holdings_service._replay`; `tax_statement_service.intraday_statement` for the listing) ·
   *Locked by*:
   `tests/services/test_realised_lots.py::test_intraday_is_not_a_capital_gain`,
   `tests/services/test_tax_statement_service.py::test_intraday_trades_lists_each_same_day_match`,
   `frontend/app/tax/intraday-trades.test.tsx` ("lists each intraday match with the server's profit,
   apart from capital gains"),
   `tests/services/test_investment_import_service.py::test_same_day_sell_before_buy_is_not_an_oversell`,
   `tests/api/test_investment_transactions.py::test_same_day_sell_posted_before_its_buy_422`
9. **No write leaves a sell uncovered.** After any write to an instrument's investment transactions —
   manual entry, reinvestment, delete, CSV import — the rule 3 replay (after rule 7 netting) must match
   every `sell` and `switch_out` in full against lots open at its own date. An API write that leaves any
   units unmatched is refused with 422 and nothing it wrote persists. An import is checked once per
   instrument it adds rows to, over the whole file: if the replay leaves any sell uncovered, none of
   that instrument's rows from the file persist (nor an instrument the file created), the import lists
   every uncovered sell (by file line, or by date for a sell already recorded), and the file's other
   instruments import normally. Units held today are not the test; a back-dated sell, or a sell or
   delete that strips a later sell of its lots, is refused the same way. — *Enforced by*: service
   (`holdings_service.rebuild_realised_lots`, `investment_import_service.persist_investment_rows`) ·
   *Locked by*:
   `tests/api/test_investment_transactions.py::test_back_dated_sell_uncovered_at_its_date_422`,
   `::test_sell_that_uncovers_a_later_sell_422`,
   `::test_delete_consumed_buy_422_when_the_sell_would_be_uncovered`,
   `::test_delete_consumed_buy_204_when_other_lots_cover_the_sell`,
   `tests/services/test_investment_import_service.py::test_import_rejects_a_sell_that_uncovers_a_later_sell`,
   `::test_import_is_atomic_per_instrument`, `::test_import_lists_every_uncovered_sell`,
   `::test_import_replays_each_instrument_once`,
   `tests/services/test_realised_lots.py::test_rebuild_refuses_an_uncovered_sell`

## Out of bounds

- **Computing tax on intraday income.** Rule 8 reports it; speculative business income is taxed at
  slab rates with its own loss rules, which F11 does not compute.
- **Same-day netting outside `indian_equity`**: MF units have no intraday market, and US shares and
  manual instruments are out of scope. F&O is not modelled at all.
- **Choosing lots by hand** (specific identification for tax-loss harvesting), and the LIFO option that
  the 2025 Act may allow for unlisted demat shares. Neither is in v1.
- **Tax buckets, rates and FX for tax.** PRD §F7 owns the classification constants, and ADR-0014 owns
  tax FX.

## Why

FIFO is what Indian tax law prescribes for securities, and it is what registrars and brokers apply.

- **The 2025 Act:** *"the cost of acquisition and the period of holding of any securities shall be
  determined on the basis of the first-in-first-out method"* ([Income-tax Act 2025, as reproduced by
  Indian Kanoon](https://indiankanoon.org/doc/23042319/)). **The section number is unconfirmed.** The
  Gazette copy of the Act was not reachable. Do not cite a section number until it is confirmed.
- **Before 1 April 2026:** s.45(2A) of the Income-tax Act 1961 applied FIFO to demat holdings.
- **Practice:** registrars redeem MF units FIFO across SIP and lump-sum lots. Zerodha's Console says
  *"Your buy average price follows the First In, First Out (FIFO) method"*.

FIFO is also the simplest rule to explain with no per-lot UI.

No source sets a same-date order. Within one date the tax result changes only when lots differ in cost
per unit, which is rare. The order still has to be **deterministic**, so that re-running a report gives
the same figures (PRD §Verification 10). Source order is the order the investor's own statement shows.
The insertion id is the last resort, and nothing can tie on it.

A same-day buy and sell of a listed share is speculative business income, not a capital gain, and
Indian brokers net it by date before applying FIFO (Zerodha's Tax P&L reports intraday separately).
Without the netting, the day's sell would consume the oldest lot, so both the gain and the cost of the
shares the user kept would disagree with the broker's report.

## Alternatives rejected

- **Average cost** — matches no Indian tax computation; holding periods are lost.
- **Id only, ignoring source order** — order would depend on upload order rather than the statement.
- **A stored `source_line` column** — the same order as file-order insertion, plus a column; revisit
  only if a writer cannot insert in source order.
- **Plain FIFO with intraday trades flagged and excluded** — a same-day sell would still consume the
  oldest lot, wrongly moving the cost basis and holding period of shares the user kept.

## Consequences

- An importer that batches, sorts or parallelises inserts would silently change same-date matching.
  `test_rows_persist_in_file_order` guards that; the importer inserts the whole file in one flush, in file order.
- F11 rests on rule 5. `realised_lots` is rebuilt per instrument inside every write's transaction;
  because rule 9 refuses uncovered sells, the holdings read-model's oversell clamp guards reads only.
- Reopen if the 2025 Act's FIFO clause turns out to exclude MF units in statement-of-account form, or
  if a v2 feature adds lot selection.

## History

- 2026-11-15 — Accepted. FIFO was already the PRD §F7 rule and the code. The new parts are the
  same-date order (date → source position → id), the citation to the 2025 Act, and intraday trades
  ruled out of bounds.
- 2026-11-15 — Revised the same day: same-day `indian_equity` trades net first as intraday (rules 7–8),
  matching broker tax P&L; previously out of bounds.
- 2026-11-16 — Rule 9: an import is checked once per instrument and refused per instrument (was: per
  row); every uncovered sell is listed.
- 2026-11-16 — Rule 5: intraday matches are persisted beside FIFO matches in realised_lots, marked
  kind; F11 capital gains read only fifo rows. Rule 8: the same-day sell-before-buy allowance applies
  to a file; manual entry still refuses a sell posted before its buy.
- 2026-11-16 — Rule 8: the F11 intraday listing landed (`GET /tax/intraday-trades`, the /tax page);
  `indian_*` instruments are INR at every write boundary.
