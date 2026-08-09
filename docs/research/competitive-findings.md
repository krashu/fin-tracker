# Competitive findings — features & mechanisms worth adopting

**Date:** 2026-07-30 · **Method:** four parallel research passes (OSS/GitHub, Indian
products & tax rails, global commercial SaaS, community demand signal) · **Status:**
findings adjudicated, build order proposed, not yet accepted into [PRD.md](../../PRD.md)

This document exists so the ranking survives the conversation that produced it, and so
the items in §7 (Killed) don't get re-litigated. It is *not* a PRD amendment — where a
finding contradicts the PRD, that's called out and the PRD still wins until edited.

Findings are marked **[verified]** (read the code / a primary source myself),
**[cited]** (traced to a vendor doc or primary source by a research pass), or
**[inferred]** (reasoning beyond what any source states).

---

## 1. Defects found in our own code

These came out of comparing our mechanisms against the corpus. All three are
**[verified]** — read directly, not taken from a research summary.

> **A full implementation brief for these three lives in
> [defect-handoff.md](defect-handoff.md)** — exact code, repro steps, the six affected call
> sites, open design decisions, and verification criteria. Work from that, not from this
> summary.

### 1.1 Same-day duplicate data loss in statement import

[`import_service.py:220`](../../backend/app/services/import_service.py#L220) skips a row
when `fp in existing_fps`; [`:251`](../../backend/app/services/import_service.py#L251)
adds each newly-inserted fingerprint to that set **inside the row loop**. Two genuinely
distinct transactions sharing date + amount + merchant + account therefore collide
*within a single statement*, and the second is dropped.

Realistic in Indian spending: two auto rides at the same fare on one day, two identical
tolls, two same-price coffees. This is the primary import path, not an edge case.

**Fix:** an occurrence ordinal in the fingerprint payload (Nth identical row that day),
or an explicit within-batch counter. Requires a recompute migration.

### 1.2 Fingerprint payload has no separator

[`fingerprint.py:35`](../../backend/app/services/fingerprint.py#L35):

```python
payload = f"{txn_date.isoformat()}{amount_paise}{normalized_merchant}{account_id}"
```

Merchant `AMAZON1` + account `2` hashes identically to `AMAZON` + account `12`; the
amount/merchant boundary has the same class of collision. Low probability, real defect.

**Fix:** a separator that cannot occur in a normalized merchant. Same recompute migration
as 1.1. Worth pairing with a `fingerprint_version` discriminator (Firefly III's approach —
it keeps `import_hash` and `import_hash_v2` side by side and dedups against the union
during transition) so the Indian RRN-stripping change that `fingerprint.py`'s own
`TODO(ADR)` anticipates isn't another big-bang recompute.

The ADR that file asks for should cover 1.1, 1.2, and the versioning scheme together.

### 1.3 IDCW-reinvest is unrepresentable

[`schemas/investments.py`](../../backend/app/schemas/investments.py) enforces
`units == 0` on `dividend`. An Indian MF dividend-**reinvestment** plan therefore can't
be recorded: the units never arrive, and both holdings and XIRR drift silently.

**Fix:** Sharesight's shape **[cited]** — a `dividend` row *plus* a linked `buy` that
opens a new lot with its own cost basis and acquisition date. Reuses the existing
`switch_pair_id` linked-pair idiom rather than introducing a concept.

Per [feedback: bug discipline], all three want reproduce → minimal repro → fix →
regression test, in that order.

---

## 2. Build order

| # | Work | Rationale | Effort |
|---|---|---|---|
| 1 | §1.1 + §1.2 — one recompute migration + the ADR | Verified data loss; everything else is a feature | ~1 day |
| 2 | §1.3 — IDCW-reinvest representable | Silent holdings/XIRR drift | hours |
| 3 | §3.1 — currency-gain decomposition | Best value-per-hour finding; both FX clocks already exist | 2–3 days |
| 4 | §3.2 — suppress XIRR annualisation under ~1 year | Two vendors converged independently | hours |
| 5 | §4 — the F11 slice | Cut ~two-thirds by the §5 answers | ~1 week |
| 6 | §3.3 — split the PDF→text parser seam | Do it *before* the next parser, not after | ~1 day |
| 7 | §3.4 — restore-then-verify round-trip test | Actual Budget has this exact hole open as a bug | ~half day |
| 8 | §3.5 — import-batch coverage-window gap detection | A missing statement is currently invisible | ~1 day |

Deferred as a single deliverable, not three: **§6 instrument NAV history**.

---

## 3. Adopt

### 3.1 Decompose return into price vs currency

**[cited]** — double-sourced, arrived at independently:

- Sharesight's *components return* splits total return into capital gain / dividends /
  **currency gain**, holding the denominator constant and swapping only the numerator.
  Their documented honesty rule: the components **deliberately do not sum** to the total,
  because the percentage↔amount relationship is exponential. Their currency-gain figure
  covers only the currency movement on *invested capital*, explicitly excluding FX
  movement on unrealised appreciation.
- Portfolio Performance models it as a closed-form waterfall that must balance:
  `INITIAL_VALUE + CAPITAL_GAINS + REALIZED_CAPITAL_GAINS + EARNINGS + CURRENCY_GAINS
  + TRANSFERS − FEES − TAXES = FINAL_VALUE`, with `CURRENCY_GAINS` as its own line.

**Why it's cheap here:** [`holdings_service.py:48-54`](../../backend/app/services/holdings_service.py#L48-L54)
already computes `invested_inr_paise` on the historical FX clock and
`current_value_inr_paise` on today's. The delta between those clocks *is* the currency
component, and `_Lot` already carries `fx_rate_to_inr` through FIFO consumption.

**Why it matters:** for an Indian investor holding US equity, the INR return currently
conflates "the asset went up" with "the rupee fell" — the split is the number most worth
seeing, and INDmoney doesn't show it cleanly. Adopt PP's identity as a **test invariant**
(same idiom as the existing per-tag reconciliation identity): if the categories don't sum
to `FINAL_VALUE`, the accounting has a bug.

### 3.2 Don't annualise a return over a short holding period

**[cited]** Sharesight suppresses annualisation below one year of average-time-invested
and shows a holding-period return instead; Kubera shows plain ROI below a year and IRR
above, on the stated grounds that IRR "only becomes meaningful when the investment
duration exceeds one year." Two independent vendors converging means real users hit the
300%-XIRR-on-a-3-month-SIP problem.

Slots into the existing typed degenerate reasons in `_safe_xirr` as a
`duration_too_short` reason falling back to absolute return.

### 3.3 Split the PDF→text seam in the parser protocol

**[cited]** Portfolio Performance runs **135 per-issuer extractors against 133 committed
fixture directories**, where fixtures are `.txt` files of *extracted PDF text* carrying a
provenance header (`PDFBox Version: …` / `Portfolio Performance Version: …`). Layout drift
is absorbed by adding an alternative section (`oneOf(...)`) rather than rewriting the
parser, so old fixtures keep passing. Issuer routing is by registered marker strings, so
the user never selects an issuer.

**Why it matters here:** [`parsers/base.py:68`](../../backend/app/parsers/base.py#L68) is
`parse(cls, pdf_bytes, password) -> list[RawTransaction]`. With no text seam, every
fixture must be a redacted **PDF** — which is exactly why CLAUDE.md §Fixture redaction
says `redact_fixture.py` covers text-based fixtures while PDFs still need an eyeball.
Splitting `pdf_bytes → list[str]` from `list[str] → list[RawTransaction]`:

- makes the redaction script authoritative for parser fixtures,
- makes a format-drift variant cost one text file,
- and makes a `pdfplumber` upgrade that silently changes extraction **legible** (stamp the
  producing version into the fixture) instead of mysterious.

Sequence this **before** writing more parsers, since it changes what a fixture is.

Licence: PP is EPL-1.0 — read for design, don't copy files.

### 3.4 Restore-then-verify round-trip test

**[cited]** Upgrade/migration is the single largest topic in Firefly III's tracker (788
issues mention "upgrade"). More pointedly, Actual Budget has an open bug where **sync
fails after restoring from backup** — the restore path itself is the break. Wealthfolio's
3.5 release silently broke money math in two separately-reported ways.

We ship CSV backup export + additive re-import. A round-trip assertion (export → wipe →
import → assert equality on money paths) is the cheapest high-value test gap identified in
the whole sweep. Golden-file tests over money math would cover the second failure mode.

### 3.5 Import-batch coverage windows → gap detection

**[inferred]** from hledger's file-keyed dedup state. hledger's own mechanism (a sidecar
`.latest.<FILE>` holding the last-seen date, then drop-N on the boundary) is **worse** than
our fingerprint for overlapping statement PDFs — it silently drops back-dated corrections
forever. But the transferable idea is dedup/coverage state keyed to the **source file**
rather than the transaction.

Import batches already exist with ids. Recording each batch's covered date range yields
"Axis has statements for Apr, May, Jul — June is missing," which is currently invisible.

### 3.6 Smaller adoptions

- **`external_ref` on transactions** **[cited]** — Firefly's data-importer lets the user
  nominate identifier column(s) as the dedup key; Actual tries `imported_id` before any
  fuzzy pass. If Axis/ICICI statement rows carry an issuer reference / auth number, that's
  *provider-supplied* identity — strictly better than a derived hash, and it removes the
  date-drift half of the fingerprint problem for those issuers. **Gated on checking a
  fixture** (see §8).
- **Per-field locks** **[cited]** — Maybe Finance's `Enrichable` keeps a
  `locked_attributes` map and auto-locks every attribute a user's save changed, so human
  precedence is automatic; every automated writer logs what it set and why. Our `pinned`
  flag locks a *rule*; this locks a *field on a row*. Today, correcting one transaction
  feeds the merchant→category learner, so a one-off fix leaks into policy. A portable
  shape here is a thin `transaction_field_locks` table — Maybe's JSONB `?|` filter isn't
  portable across the SQLite→Postgres path.
- **Symbol identity plumbing** **[cited]** — Ghostfolio keeps a symbol-redirect table, an
  `isActive` flag so delisted instruments are deactivated rather than deleted (preserving
  transaction FKs), a `CLOSE` vs `INTRADAY` state on each price row, and a **parallel
  overrides table** so user corrections layer at read time instead of overwriting provider
  data. Indian equity produces all of these cases (post-merger ISIN changes, ticker
  renames, MF scheme mergers). The overrides pattern also answers the manual per-instrument
  NAV-edit UI already on the backlog: write edits to an overrides table, so a later refresh
  doesn't discard them. The `CLOSE`/`INTRADAY` flag guards a concrete bug — a manual NAV
  refresh at 2pm IST overwriting a genuine close for the same date.
  *Do not* copy Ghostfolio's money handling: it stores `marketPrice`, `fee`, `unitPrice`,
  `quantity` as `Float`, which CLAUDE.md §Working agreements forbids.
- **Percentage-of-amount tolerance** **[cited]** — Actual uses one named constant
  (`±7.5%` of the value, rounded to integer cents; `±2 days` for dates) shared by its rules
  engine *and* its recurrence detector. If amount tolerance is ever added (recurring
  detection, transfer pairing), a percentage beats a flat rupee threshold and one shared
  constant keeps features consistent.

---

## 4. The F11 slice

### 4.1 Schema decisions to lock *before* the `realised_lots` migration

Each changes that table's shape, so deciding after the migration means a second migration.
All three **[cited]** from Wealthfolio's `lots` / `lot_disposals` migrations.

1. **Splits as a cumulative-ratio column, not in-place mutation of prior buys.**
   [PRD.md:387-390](../../PRD.md#L387-L390) plans to adjust prior `buy`/`sip` rows in place.
   That is destructive: the original contract-note quantities are gone, so the statement
   can no longer be reconciled against the broker's own, and a mis-entered ratio can't be
   undone. Wealthfolio keeps `original_quantity` / `cost_per_unit` immutable and carries a
   cumulative post-acquisition split ratio applied at read time. Holding period is trivially
   preserved. Portfolio Performance reaches the same place differently — it models splits as
   `SecurityEvent` reference data on the *instrument*, entirely outside the transaction
   table. **This contradicts the PRD and needs a PRD amendment, not a silent divergence.**
2. **Denormalise `proceeds` and `realized_pnl` onto the disposal row.** Makes the F11
   statement a single-table scan, and freezes the number as computed. That second property
   is the cheap version of Sharesight's *Lock for period* **[cited]** — Sharesight freezes
   both the method and the parcel matching per financial year, warning that unlocking
   "will alter the CGT result for that period and all subsequent periods." Denormalising
   gets most of that protection without a lock table.
3. **`cost_basis_method` stored per lot.** Makes the per-lot FIFO override that
   [PRD.md:411-413](../../PRD.md#L411-L413) defers a data change later rather than a
   migration. Keep FIFO as the only *offered* policy — Sharesight ships five (FIFO, LIFO,
   minimise gain, maximise gain, minimise CGT, plus an auto-optimise), which is precisely
   the CLAUDE.md §2 abstraction-ahead-of-second-use we don't want.

Migration discipline **[cited]**: Wealthfolio shipped this transition additively —
dual-write both representations, no read-path switchover in the same migration. Our move
from `_consume_fifo`'s in-memory FIFO to persisted lots is the same shape.

### 4.2 FX: Rule 115 needs its own table

**[cited]** Rule 115, Income-tax Rules 1962: "the rate of exchange … shall be the
**telegraphic transfer buying rate** … as on the specified date," and for income under the
head *Capital gains* the specified date is **the last day of the month immediately
preceding the month in which the asset is transferred**. INDmoney's own Tax Centre states
it uses exactly that — SBI TT Buy Rate, previous month-end, citing Rule 115.

Consequences:

- This is a **report-time lookup, not a stamped column** — the specified date depends on
  the *sell* date, so the rate is unknowable at buy time. The existing mid-market stamp
  stays untouched for valuation and XIRR.
- **Use a separate `sbi_ttbr_rates` table; do not widen `fx_rates`.** **[verified]**
  [`fx_rate_quote.py:37-45`](../../backend/app/models/fx_rate_quote.py#L37-L45) makes the
  unique index `(from_currency, to_currency, date)` — `source` is a column but not part of
  the key, so a TTBR row for a date that already has a frankfurter row violates the
  constraint. Widening it is worse: the comment directly above documents the carry-forward
  read as `WHERE from=? AND to=? AND date<=? ORDER BY date DESC`, which would return a TTBR
  row into portfolio valuation.
- SBI publishes no API and no historical archive. ~12 month-end rows per year for USD, so a
  seeded, user-editable table with a provenance field beats scraping.

**Unresolved [cited]:** whether Rule 115 converts cost at the *acquisition*-month rate or
both legs at the sell-month rate. Rule 115A's acquisition-date averaging applies to
non-residents holding shares of an Indian company — not this case — and no CBDT circular
settles it. **Make it a config choice, default to INDmoney's convention** so the numbers
reconcile against the broker statement, and print the assumption on the report.

### 4.3 Classification constants must be date-keyed

**[cited]** Finance (No. 2) Act 2024 changed holding-period and indexation rules
**mid-financial-year** — transfers on or after 23 Jul 2024 fall under the new regime. A
single FY can contain both. [PRD.md:404](../../PRD.md#L404)'s "named constants in a config
module" is therefore insufficient: the classifier must resolve constants by **transfer
date**.

Live demonstration of the hazard: Paisa (the India-specific OSS tracker) still carries
inline `0.10` / `0.15` equity rate literals in `internal/taxation/tax.go`, stale since the
2024 change, on a repo dormant ~8 months.

### 4.4 Output artifact: Schedule 112A CSV

**[cited]** The Income Tax Department's e-filing portal publishes *Instructions for filling
Schedule 112A/115AD(1)(b)(iii)(P)* specifying a 14-column CSV the offline ITR utility
ingests. Column `1a` is `BE` (acquired on or before 31 Jan 2018) or `AE` (after).

The load-bearing detail: for **`AE`** rows the utility wants an **aggregate** row — ISIN
literal `INNOTREQUIRD`, name literal `CONSOLIDATED`, per-unit columns blank. So scrip-wise
per-lot detail is required **only for pre-Feb-2018 lots**.

Given §5.1 (no pre-Feb-2018 holdings), the entire CSV is currently **one consolidated
line** whose figures are sums over `realised_lots`. Read the spec's exact required-vs-blank
set for the deduction columns before coding rather than trusting this summary.

The per-lot on-screen statement remains where the reconciliation value lives.

### 4.5 Remaining slice

`realised_lots` + disposals (§4.1) · date-keyed classification constants (§4.3) ·
`sbi_ttbr_rates` + report-time lookup (§4.2) · dividend withholding column (§4.6) ·
thin 112A serializer (§4.4) · non-equity `indian_mf` guard (§5.2) · STCG/LTCG buckets and
the ₹1.25 L exemption line already specified in [PRD.md §F11](../../PRD.md#L523).

### 4.6 Dividend withholding column

**[cited]** Both the global-commercial and India passes converged independently.
[`investment_transaction.py:131-138`](../../backend/app/models/investment_transaction.py#L131-L138)
carries `amount_native_paise` and `fees_native_paise` only. A US dividend paid to an Indian
resident is withheld at source under the India-US DTAA, so gross and withheld must be
stored separately — otherwise the row either overstates cash received or loses the foreign
tax credit, and [PRD.md:536-538](../../PRD.md#L536-L538)'s dividend summary is wrong for
USD rows.

One nullable int64 column; do it in the same migration as the F11 dividend summary.

If a Form 67 deadline is ever surfaced in the UI, use **Rule 128(9) as amended by CBDT
Notification 100/2022** (end of the relevant assessment year). INDmoney's own help text
still shows the pre-2022 position (ITR due date).

---

## 5. Scope deleted by portfolio answers (2026-07-30)

### 5.1 No unsold lots acquired on or before 31 Jan 2018

Deletes: the `fmv_31jan2018` column, the one-off 31-Jan-2018 bhavcopy seed for equities,
the historical-NAV lookup via `parsers/mfapi.py` (which the Zscaler block would have made
painful anyway), and the three-way `max(cost, min(FMV, sale_price))` branch in the matcher.
Also collapses the 112A CSV to a single `AE` line (§4.4).

Becomes a PRD note. Revisit only if pre-2018 lots appear.

Reference for if it ever binds: Zerodha's **tradewise P&L** already emits FMV as at
31 Jan 2018 and the grandfathered cost of acquisition per matched lot **[cited]**.

### 5.2 MF side is pure equity and index funds

Deletes: capturing the AMFI scheme-category string, the versioned category→tax-class
mapping, and the user-override column — roughly two days to zero.

**Keep a guard.** [PRD.md:401](../../PRD.md#L401) keys slab-taxation off
`asset_class ∈ {bond, fd}`, which misses debt mutual funds entirely (they are
`indian_mf`) — §50AA cannot fire. **[verified]** That defect doesn't vanish just because
no debt MFs are held today; `asset_class` still permits adding one, which would silently
classify as equity. The fix shrinks from a mapping table to a refusal: if an `indian_mf`
instrument isn't known-equity, don't classify it — flag it on the statement.

Preserved for if this changes:

- The equity-oriented test is **≥65% equity, "computed with reference to the annual
  average of the monthly averages of the opening and closing figures"** **[cited]** — a
  realised-portfolio test over a year, not a label. So **tax class must be an axis
  independent of `asset_class`**, which is doing allocation-donut duty. Paisa reaches the
  same conclusion with an explicit `TaxCategory ∈ {Equity, Equity65, Equity35, Debt,
  UnlistedEquity}` enum.
- §50AA's "specified mutual fund" was **redefined w.e.f. 1 Apr 2026** to >65% in debt and
  money-market instruments, replacing the old ≤35%-equity test **[cited]**.
- The free machine-readable input is AMFI's `DownloadSchemeData_Po.aspx?mf=0`, returning
  `AMC, Code, Name, Type, Category, NAVName` — a *different* endpoint from the `NAVAll.txt`
  we already consume, on the same host (which matters: AMFI is reachable on the corp box
  where `api.mfapi.in` is Zscaler-blocked). **[inferred]** that `Category` carries the SEBI
  classification string — read from Paisa's positional parser, endpoint not fetched.
- Store the raw category string, not an enum: SEBI recategorised schemes on 26 Feb 2026
  (discontinuing Solution Oriented, adding Life Cycle funds), so the vocabulary churns.

---

## 6. Deferred as one deliverable: instrument NAV history

Three separately-requested features share **exactly one** prerequisite — a dense per-
instrument historical NAV/price series. [`instrument.py:113-114`](../../backend/app/models/instrument.py#L113-L114)
has only scalar `current_nav` + `nav_updated_at`. The pattern already exists for
benchmarks in [`benchmark.py:70`](../../backend/app/models/benchmark.py#L70).

Unlocked by it:

1. **Unrealised-CGT-as-of-date** **[cited]** — Sharesight's report takes a report date,
   cost base, and market price *on that date*, output per-parcel. The Indian framing is the
   genuinely actionable one: which lots cross 12 months next month, and how much ₹1.25 L
   LTCG exemption headroom remains.
2. **TWRR + the drawdown suite** **[cited]** — Portfolio Performance's TTWROR is ~10 lines
   *given* a value series (`delta = (V_i + outbound)/(V_{i-1} + inbound) − 1`, chained
   geometrically, with a divide-by-zero guard for empty periods). The same series then
   yields max drawdown, its interval, drawdown *duration*, longest recovery time,
   volatility and semi-deviation. XIRR was buildable first precisely because it needs only
   cashflows plus a terminal value.
3. **The moving portfolio-vs-benchmark line chart**, already parked at
   [PRD.md:849-850](../../PRD.md#L849-L850) with "dense per-holding historical NAV" named
   as its blocker.

Cost is weeks and sits almost entirely in the price history, not the metrics (~2 days on
top). Scope it as one deliverable.

**Warning attached to this work** **[cited]**: Maybe Finance (54.4k stars, archived
2025-07-24) background-synced facts into `balances` / `holdings` cache tables and concluded
it "improved performance, but at the *cost* of data consistency" — *"Every view of the app
touches nearly all the user's data. If any piece of data is wrong, every view in the app is
wrong. There is nowhere to hide in a personal finance app."* CLAUDE.md §2's
no-portfolio-snapshot-cache rule now has a named casualty behind it. Expect the NAV-history
work to generate cache pressure; don't yield.

---

## 7. Killed — do not re-litigate without new evidence

| Item | Why |
|---|---|
| **Receipt OCR** | 2–5 issue mentions across Firefly + Actual vs 69–525 for recurring (~50×), and **zero** prior art in a 17-repo corpus. Weakest-supported item on the roadmap. |
| **Google Drive sync** (deprioritised, not killed) | 3–5 mentions; no repo in the corpus syncs to consumer cloud storage. Self-hosters instead asked Wealthfolio for a **configurable database location** so they can point it at their own sync substrate — likely the cheaper answer to the same need. Drops below recurring detection. |
| **Rules-engine expansion** (ordered rules, boolean logic, regex, amount conditions) | Firefly III maintains a written refusal: *"Most rule engines, even the filters in GMail, are very complex because of user demands and as a result, nobody uses them anymore."* Actual's 67- and 96-comment templating/formula threads are what the other road looks like. Actual's specificity scoring (`is`=10 … `contains`/`matches`=0) would be **inert** against exact-merchant-only rules. CLAUDE.md §2. |
| **Statistical / ML auto-categorisation** | **No benchmark showing it beats exact-match memory exists in any repo searched** — smart_importer's own accuracy notes are qualitative. Measure our exact-match miss rate first; if small, it's a §2 over-build. If ever justified, prefer Paisa's ~60-line TF-IDF (no scikit-learn, derivable from one SQL query, amount-as-token is a good trick for fixed recurring payments) over smart_importer's linear SVC. |
| **Dividend forecasting** | Two passes independently said skip: no Indian ex-date/pay-date feed exists, so every row would be `Estimated` — a chart of our own past. Portfolio Performance's `DividendEvent` (ex-date + pay-date + per-unit amount as instrument reference data) is the right shape *if* a feed ever appears. |
| **Schedule FA generation** | Needs a **daily** units × price series per US instrument across the calendar year plus **per-day** TTBR (the month-end table in §4.2 doesn't cover it), on a calendar-year axis that matches no other report. Weeks. INDmoney auto-fills Schedule FA for holdings on its platform, and US equity is held through exactly one broker. The ₹10 lakh penalty argues the disclosure must *happen*, not that we must compute it. **Contingent — see §8.2.** |
| **Target-allocation drift** | Already parked in PRD §Considered-unscheduled. If it lands, two Wealthfolio decisions matter more than the schema: `rebalance_goal = nearest_band` (rebalance to the band edge, not the exact target) and `allow_sells` defaulting to **0**. Rebalancing to exact target with sells permitted maximises taxable events — wrong for someone managing a ₹1.25 L exemption. Hybrid bands (band scales with target weight) beat a flat 5%. |
| **Household / multi-user roll-up** | Actual Budget closed this `not_planned` at 26 👍 — the highest-reaction rejected item found anywhere — and the community's in-thread workaround is **one container per user behind a reverse proxy**, which is already our architecture. Stays parked. |
| **Inbound-email parse rail** | Sharesight's per-portfolio inbound email address needs mail infrastructure; wrong for a local-first single-user app. |
| **Whole-row content hash** | Firefly's `sha256(entire row)` re-imports everything on any upstream format tweak. Our 4-field hash is better. Only the **versioning** idea transfers (§1.2). |
| **Account Aggregator / MF Central / eCAS for cost basis** | See §9. |

---

## 8. Open questions

### 8.1 Do Axis or ICICI statement rows carry an issuer reference / auth number?

Gates §3.6's `external_ref`. If yes, provider-supplied identity beats a derived hash and
removes the date-drift half of the fingerprint problem for both issuers. Checkable against
an existing fixture.

### 8.2 Does the INDmoney tax statement give per-security peak value for the calendar year?

Settles the Schedule FA disagreement between two research passes. The "nobody serves this
well" claim was explicitly **[inferred]** from INDmoney marketing a Tax Centre without
visible peak-value output; the counter-claim is INDmoney's own vendor page. Five minutes
with the actual statement resolves a weeks-vs-zero build decision. Until then §7 holds.

### 8.3 PRD amendments needed

- §4.1 item 1 contradicts [PRD.md:387-390](../../PRD.md#L387-L390) (split representation).
- §4.3 supersedes [PRD.md:404](../../PRD.md#L404) (constants must be date-keyed).
- §5.2 fixes [PRD.md:401](../../PRD.md#L401) (slab test misses debt MFs).
- §4.6 extends [PRD.md:536-538](../../PRD.md#L536-L538) (dividend summary needs withholding).

---

## 9. Confident negatives — closed rails

- **Account Aggregator**: an FIU must be an entity registered with and regulated by a
  financial-sector regulator; becoming the AA itself requires an Indian company with
  ₹2 crore net owned funds. A solo self-hosted tool qualifies as neither. Permanently
  closed. **[cited]**
- **MF Central third-party API**: AMFI directed MF Central to stop sharing investor
  portfolio data with third-party platforms in Sept 2025, on distributor-poaching and
  consent-quality grounds. The OTP-consent paid-access framework is gone. **[cited]**
- **Depository eCAS (CDSL/NSDL)**: *does* include transactions, contrary to a prior
  internal note — but the transaction section is **period-scoped** (monthly on activity,
  else half-yearly holdings in Apr/Oct). It cannot reconstruct a 2015 cost basis, so it is
  functionally holdings-only for our purposes. Password = PAN of the first holder in caps.
  **[cited]**
- **Broker APIs**: no public API on Zerodha Console, Groww, Upstox, Dhan or Angel One for
  tax/P&L reports — all are web-login downloads. **[cited]**
- **No aggregator rail covers India**: Firefly's data-importer supports GoCardless-Nordigen
  (EU PSD2), SimpleFIN (US) and Spectre; Wealthfolio has a broker-connect crate. None
  covers Indian banks or brokers. **Our PDF + CSV rails are the correct architecture, not a
  compromise.** **[cited]**
- **Nobody in the corpus does** receipt OCR, email/SMS ingestion, consumer-cloud sync, or
  per-holding return contribution.

**The rail that is open and underused:** a **read-only reconciliation importer** for a
broker tax-P&L. Zerodha's tradewise P&L is one row per FIFO-matched lot and carries FMV as
at 31 Jan 2018 plus grandfathered cost — so it validates our FIFO *and* our grandfathering
against the broker's own arithmetic. `casparser` (MIT) also ships a beta capital-gains
report with 112A output; treat it as a reconciliation oracle, never a source of truth.
Rank this above building any new report.

---

## 10. Corpus

Verified via the GitHub REST API on 2026-07-29/30.

| Repo | ★ | Last commit | Open | Licence |
|---|---|---|---|---|
| actualbudget/actual | 27,815 | 2026-07-27 | 210 | **MIT** |
| firefly-iii/firefly-iii | 24,164 | 2026-07-27 | 151 | AGPL-3.0 |
| ghostfolio/ghostfolio | 9,023 | 2026-07-29 | 278 | AGPL-3.0 |
| afadil/wealthfolio | 8,410 | 2026-07-14 | 353 | AGPL-3.0 |
| ellite/Wallos | 8,284 | 2026-07-19 | 57 | GPL-3.0 |
| ledger/ledger | 6,000 | 2026-07-03 | 13 | NOASSERTION |
| beancount/beancount | 5,849 | 2026-05-18 | 232 | GPL-2.0 |
| simonmichael/hledger | 4,599 | 2026-07-25 | 350 | GPL-3.0 |
| Gnucash/gnucash | 4,301 | 2026-07-26 | 52 | NOASSERTION |
| buchen/portfolio | 3,989 | 2026-07-29 | 460 | **EPL-1.0** |
| ananthakumaran/paisa | 3,187 | **2025-12-02** | 89 | AGPL-3.0 |
| Ivy-Apps/ivy-wallet | 3,162 | 2026-04-02 | 69 | GPL-3.0 · **ARCHIVED** |
| beancount/fava | 2,539 | 2026-07-28 | 108 | MIT |
| moneymanagerex | 2,239 | 2026-07-29 | 478 | GPL-2.0 |
| firefly-iii/data-importer | 812 | 2026-07-21 | 1 | AGPL-3.0 |
| beancount/smart_importer | 304 | 2026-07-26 | 4 | **MIT** |
| maybe-finance/maybe | 54,358 | 2025-07-24 | 0 | AGPL-3.0 · **ARCHIVED** |

Commercial products studied: Sharesight (primary), Kubera, Lunch Money, PocketSmith,
Monarch, Tiller, Copilot, Snowball, YNAB, Empower · INDmoney, Kuvera, Zerodha Console,
MProfit, Quicko, Groww, Vested.

**Licence constraint:** only **Actual Budget** and **smart_importer** (both MIT) are
code-borrowable, with attribution. Portfolio Performance is EPL-1.0 (file-level copyleft —
read for design, don't copy files). Ghostfolio, Wealthfolio, Paisa, Firefly and Maybe are
AGPL-3.0: reading to understand a mechanism is fine, copying code into a self-hosted app
that may one day be network-exposed triggers source-provision obligations. `ledger` and
`gnucash` returned `NOASSERTION` — **unverified**, check their `COPYING` files before
borrowing.

---

## 11. Method notes and gaps

- **Reddit was inaccessible** to the research tooling (all six target subreddits returned
  errors), so demand evidence leans on GitHub issue reactions and Hacker News, which
  over-weights people willing to file issues.
- **Calibration matters:** the highest reaction count on any open issue across nine
  projects is **32 👍**. Anything at 5–10 is three or four people. Firefly's bot
  auto-replies that "+1" comments don't influence the roadmap. Read all demand figures as
  direction, not mandate. Raw mention counts also conflate demand with codebase surface
  (Actual's 1,637 "budget" hits partly reflect it *being* a budgeting app) — the OCR and
  Drive results survive that confound because neither project has the surface to inflate it.
- **No quantified app-store or review-site data** was obtainable for any Indian or global
  commercial product; searches returned SEO filler.
- **Tax facts:** every rate, threshold, and holding period cited here carries an assessment
  year in its source and should be re-verified against primary sources before implementation.
  The Income-tax Act 2025 took effect 1 Apr 2026 (governing tax year 2026-27 onward, while
  FY 2025-26 remains under ITA 1961), and it was **not verified** whether the Income-tax
  Rules 2026 preserve Rule 115's specified-date table, nor whether Finance Act 2026 touched
  the capital-gains provisions. Treat §4 as a design input, not as tax authority.
- **Not reported rather than reported shallowly:** budgets (Actual's envelope
  implementation is a product architecture, not a liftable mechanism), and Money Manager EX
  / GnuCash / Ledger / Beancount core / Fava, none of which yielded a mechanism beating
  those above.

## 12. Structural note

The OSS ecosystem is **cleanly split by explicit maintainer policy**, and fin-tracker
straddles the seam. Firefly III — the largest OSS budgeting app — refuses portfolio
tracking *in writing* and redirects users to Portfolio Performance. Ghostfolio and
Wealthfolio track portfolios with essentially no budgeting surface (1 issue mentions
"budget" in Ghostfolio; 0 mention "capital gains" in Actual). Maybe Finance was the one
funded attempt at both halves plus multi-currency, and its post-mortem names multi-currency
investments as the challenge it *didn't* anticipate.

So "spending + Indian MF/equity + US equity + FX + XIRR, local-first, one app" is not an
incidental scope choice — it is the unfilled gap, and Maybe's retrospective is a map of the
rocks in it. The nearest Indian OSS analogue, Paisa, has been dormant ~8 months; its
top user complaint was manual import plus (the maintainer's own words) "very crude tf-idf
based categorization," with users asking for merchant-history-based auto-categorisation —
which we already ship.
