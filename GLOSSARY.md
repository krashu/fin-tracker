# fin-tracker

A household's spending (Indian cards, bank accounts, cash) and investments (Indian MFs, US stocks,
manual holdings), rolled up in INR. Decisions live in `docs/adr/`; this file holds definitions only.

## Money

**Paise**:
An INR amount as an integer count of paise (₹1 = 100 paise); the only way an INR amount is stored or
computed.
_Avoid_: rupees (for a stored value), amount, float amount

**Native paise**:
An investment amount as an integer count of the minor unit of the instrument's own currency (paise for
INR, cents for USD), converted to INR only through the FX rate stamped at the transaction date.
_Avoid_: native amount, cents (as a column name)

## Transactions & import

**Account**:
One ledger money moves through — a credit card, a bank account, or cash — owned by one user.
_Avoid_: user account, login

**Identity fields**:
The four values that say what a transaction is: date, amount, normalized merchant and account.
_Avoid_: key fields, natural key

**Normalized merchant**:
The merchant text reduced to its matching form (lower-cased, Unicode NFC, whitespace collapsed); the shared key for
dedup, auto-tagging and labels.
_Avoid_: clean merchant, merchant key, canonical merchant (that is an alias's output)

**Merchant alias**:
A user's rule folding many merchant descriptors onto one canonical merchant (every `swiggy …` variant onto
`swiggy`).
_Avoid_: merchant rule, mapping, synonym

**Canonical merchant**:
The single merchant name a normalized merchant resolves to through the user's aliases; `unknown via <psp>`
for a gateway handle (ADR-0011 rule 12); otherwise the normalized merchant itself. What auto-tagging, labels
and merchant dashboards group by.
_Avoid_: normalized merchant (that is the input), brand, merchant key

**Seed dictionary**:
The built-in set of common Indian merchant aliases and default categories every new user starts with.
_Avoid_: default rules, merchant list

**Fingerprint**:
The frozen hash of a transaction's current identity fields; two rows with the same fingerprint say the
same thing.
_Avoid_: dedup key, hash, transaction id

**Occurrence**:
The ordinal that tells apart genuinely separate transactions sharing one fingerprint (the second identical
₹50 coffee on the same day is occurrence 1).
_Avoid_: duplicate index, copy number

**Origin fingerprint**:
The fingerprint of the statement line a transaction was imported from, fixed at import even if the
transaction is later edited; recomputable from the origin's inputs, which the first edit stores.
_Avoid_: original hash, source fingerprint

**Statement**:
One issuer document (a card or bank PDF) covering one account for one period.
_Avoid_: file, upload, bill

**Statement summary**:
The balances and period a statement prints about itself (opening balance, closing balance, period start
and end); any of them may be absent. A bank statement that prints no closing figure closes at its last
printed running balance (ADR-0010 rule 9).
_Avoid_: header, metadata, statement totals

**Statement period**:
The date range a statement prints for itself; when the statement prints none, there is no statement
period, even if one could be inferred from its rows.
_Avoid_: billing cycle, window (that is what reconciliation uses)

**Import batch**:
Everything brought in by one statement upload, reviewed and confirmed together.
_Avoid_: upload, import job

**Running balance**:
The balance a bank statement prints after each row; each row is checked against it.
_Avoid_: closing balance (that is the statement's last), available balance

**Balance reconciliation**:
Checking that a statement's opening balance plus its rows equals its closing balance; a non-zero
difference is the reconciliation delta.
_Avoid_: balance check, statement reconciliation (F4a also reconciles pairs)

**Staged**:
Imported from a statement but not yet confirmed by the user in review.
_Avoid_: pending, draft, unconfirmed

**Re-stage**:
A statement re-upload bringing back a line whose transaction is no longer in the user's data (deleted or
discarded); an edited transaction never re-stages.
_Avoid_: re-import duplicate, resurrect

## Categories

**Category**:
A user's bucket for transactions, of one kind (spend or income); either a parent or a subcategory.
_Avoid_: tag (that is a label or tag rule), bucket (in UI copy), budget head

**Parent category**:
A top-level category; it may hold subcategories and may also be used directly.
_Avoid_: root (outside code), group, super-category

**Subcategory**:
A category directly under a parent, of the same kind; it cannot have subcategories of its own.
_Avoid_: child (outside code), sub-tag, nested category

**Fallback category**:
Each kind's "Other" category: always present, never archived, renamed or put under a parent; where a transaction
lands when no category was chosen.
_Avoid_: default category, uncategorised, misc

**Archive**:
Retiring a category so it can no longer be chosen while its history stays intact; archiving a parent
archives its subcategories. There is no un-archive.
_Avoid_: delete, hide, disable

**Rollup**:
A parent's total: its own direct spend plus its subcategories' spend, archived subcategories included.
_Avoid_: aggregate, group total

**Direct spend**:
Spend tagged on a parent category itself rather than on one of its subcategories.
_Avoid_: unassigned spend, parent-only spend

**Label**:
A free-form user tag on a transaction, independent of its category; a transaction can carry many.
_Avoid_: tag (ambiguous with tag rule), category

## Auto-tagging

**Tag rule**:
A user's learned link from a canonical merchant to a category, with a count of how often it was taught.
_Avoid_: mapping, auto-rule, category rule

**Teach**:
One user decision strengthening a tag rule; a bulk gesture teaches once per distinct merchant.
_Avoid_: learn event, hit, train

**Suggestion**:
The category a tag rule pre-fills on an imported transaction, kept unchanged afterwards so its accuracy
can be measured.
_Avoid_: prediction, auto-category (for the user-facing idea)

**Passive accept**:
Confirming an imported transaction without changing its suggested category; counts as a decision.
_Avoid_: auto-accept, implicit approval

**Confident rule**:
A tag rule taught often enough that its suggestion is shown as confident.
_Avoid_: strong rule, trusted rule

**Pinned rule**:
A tag rule the user fixed by hand; it always wins and counts as user-authored even if it began as a seed.
_Avoid_: locked rule, manual rule

**Board**:
The user's confirmed transactions, where categories are reviewed and edited after import.
_Avoid_: ledger, expenses list, review queue (that holds staged transactions)

**Acceptance rate**:
The share of suggestions the user kept, counting only suggestions whose category is still live.
_Avoid_: accuracy, hit rate

## Paired rows

**Transfer pair**:
Two transactions of one user that record a single movement of money between their accounts (a card bill
paid from a bank account, a bank-to-bank move); excluded from spend and income.
_Avoid_: linked transactions, matched pair (for the stored link)

**Leg**:
One transaction of a transfer pair or investment pair.
_Avoid_: side, half

**Unlink**:
Dissolving a pair so each leg stands alone again; a leg that existed before it was linked returns to its
pre-link type and category.
_Avoid_: unpair, unmatch

**Unpaired transfer**:
A transfer with no partner: a leg whose partner was deleted or not restored, a card-bill debit
committed while its card's statement for that date was in, a card's bill-payment credit committed with
no category, or a row the user typed as a transfer, until a link pairs it. Excluded from spend and
income like any transfer.
_Avoid_: orphan leg, lone leg, pending transfer

**Investment pair**:
Two investment transactions recording one event, such as a mutual fund's IDCW payout and its
reinvestment, or the sell and buy of a fund switch.
_Avoid_: transfer pair (for investments)

## Transaction types & signs

**Transaction type**:
What kind of money movement a transaction is: spend, income or transfer. Refunds and income reversals are
signs within a type, not types of their own.
_Avoid_: refund type, category type (that is category kind)

**Refund**:
A spend transaction with a positive amount, in the same category as the spend it reverses, so it nets that
category's total down.
_Avoid_: return, credit, refund transaction type

**Income reversal**:
An income transaction with a negative amount, in the same category as the income it reverses (a salary
clawback), so it nets that category's total down.
_Avoid_: negative spend, clawback type

**Cashback**:
A card credit the issuer gives back on purchases; a rebate on spending, recorded like a refund in a
spend-kind Cashback category. Referral and sign-up bonuses are not cashback; they are income.
_Avoid_: cashback income, reward income

**Gross spend**:
The total of a period's outflows before refunds net them down (negative spend amounts only).
_Avoid_: total spend (ambiguous: net spend includes refunds)

**Category kind**:
Whether a category holds spend or income; a refund uses a spend-kind category, an income reversal an
income-kind one.
_Avoid_: category type

## Investments

**Lot**:
Units of one instrument acquired in a single transaction, with their own acquisition date and cost;
sells consume lots oldest first.
_Avoid_: tranche, batch, position (that is the sum of open lots)

**Realised lot**:
The part of one lot a sell consumed, or a same-day intraday match, marked by its kind, stored with
its units, cost, share of the sell's proceeds and fees, and both dates; rebuilt on every
investment write.
_Avoid_: disposal

**Holding period**:
The time from a lot's acquisition date to the date it is sold; it decides whether a gain is short- or
long-term.
_Avoid_: age, tenure

**Intraday trade**:
Units of an Indian listed share bought and sold on the same date, netted against each other before
FIFO; speculative business income, never a capital gain.
_Avoid_: day trade, same-day sell (for a sale of shares held from an earlier date)

**Stamped FX rate**:
The INR conversion rate stored on an investment transaction for its own date, fixed when the row is
written.
_Avoid_: historical rate, current rate

**TTBR**:
State Bank of India's telegraphic-transfer buying rate, the rate Indian tax law uses to convert
foreign-currency income.
_Avoid_: SBI rate, tax FX rate

## Users & tenancy

**User**:
One person's login and the unit of data isolation: every owned row belongs to exactly one user.
_Avoid_: account (that is a card / bank / cash ledger), tenant (say user), member (for the data owner)

**Household**:
The 2–5 people who share one self-hosted instance; each is a separate user and sees only their own data.
_Avoid_: tenant, family, group (no household grouping exists in v1)

**Operator**:
Whoever runs and configures the instance (env settings, deployment); not a role inside the app.
_Avoid_: admin, owner

**Owned table**:
A table whose every row belongs to one user via `user_id`.
_Avoid_: user table, private table

**Global table**:
A table with no `user_id` whose data is the same for every user (benchmarks, benchmark NAVs, FX rates).
_Avoid_: shared table, reference table

**Refresh family**:
The chain of refresh tokens descending from one login, revoked together.
_Avoid_: session chain, token family

**Demo account**:
The single seeded user whose credentials are published in the source; never reachable over HTTPS.
_Avoid_: demo user, guest

**Guest sandbox**:
A short-lived, password-less user created on demand, seeded with demo data and pruned at expiry.
_Avoid_: demo account, guest account, trial user

## Dates & periods

**Business date**:
The calendar date in India (IST) that "today" means everywhere in the app; stored instants stay in UTC.
_Avoid_: UTC date, server date, local date

**Calendar year**:
1 January to 31 December, written "2026"; the year every spend view, spend year picker and spend
"year-to-date" figure (including a credit card's YTD) uses.
_Avoid_: financial year (for spending)

**Financial year**:
The Indian financial year, 1 April to 31 March, written "FY 2026-27"; the year investment figures and every
tax report use. Under the Income-tax Act, 2025 it is also the **tax year** ("Tax Year 2026-27").
_Avoid_: fiscal year, assessment year (discontinued from tax year 2026-27; only for FY 2025-26 and earlier)

**IDCW**:
Income Distribution cum Capital Withdrawal: a mutual fund's payout, formerly called a dividend; "dividend"
is kept for stocks.
_Avoid_: MF dividend

## Deployment

**Postgres cutover**:
The v2 switch of a deployment's database from SQLite to Postgres, running the same migration chain and
app code unchanged.
_Avoid_: v2 migration, database migration (an Alembic migration is a different thing)
