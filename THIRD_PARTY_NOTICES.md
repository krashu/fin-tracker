# Third-party notices

Logic adapted from other projects, with each one's licence. Nothing here is copied wholesale; the
note says which functions were adapted and where.

## bank-statement-parser (HDFC narration splitting)

- **Source**: https://github.com/akhilnarang/bank-statement-parser
- **Commit**: `40fd618cebf6688d8316a5a7a5d116d781c97b1e`
- **File**: `bank_statement_parser/parsers/hdfc.py`
- **Adapted into**: `backend/app/parsers/hdfc_bank.py` (`_split_narrations`, from `_group_narrations`
  and `_clean_narration`): join a packed narration cell's lines, split on each
  `Value Dt DD/MM/YYYY [Ref X]` marker, one narration per transaction.
- **Licence**: MIT

```text
MIT License

Copyright (c) 2026 Akhil Narang

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## bank-statement-parser (ICICI narration grouping)

- **Source**: https://github.com/akhilnarang/bank-statement-parser
- **Commit**: `40fd618cebf6688d8316a5a7a5d116d781c97b1e`
- **File**: `bank_statement_parser/parsers/icici.py`
- **Adapted into**: `backend/app/parsers/icici_bank.py` (`_read_centred`, from
  `IciciBankStatementParser._parse_icici_page`: a row takes as many particulars lines below its
  date line as above it, and the narration order; ours counts lines instead of distances.
  `_STOP_RE`, from `_TABLE_END_MARKERS`)
- **Licence**: MIT (full text in the HDFC section above)

## bank-statement-parser (SBI layouts)

- **Source**: https://github.com/akhilnarang/bank-statement-parser
- **Commit**: `40fd618cebf6688d8316a5a7a5d116d781c97b1e`
- **File**: `bank_statement_parser/parsers/sbi.py`
- **Adapted into**: `backend/app/parsers/sbi_bank.py`:
  - `_join_wrapped` and `_clean_yono_narration`, from `_yono_narration`: join a wrapped narration with no
    space at a `/`, `-` or `*` break, and drop the YONO type prefix (`DEP TFR`, `WDL TFR`, `POS ATM PURCH`)
    and branch stamp (`<digits> AT <branch code> ...`);
  - the YONO branch of `_header_columns`, from `_parse_yono_table`: only `Balance` is labelled, and Debit
    and Credit are the two columns to its left;
  - the e-account opening balance in `_read_estatement_summary`, from `_ON_DATE_RE`: match the
    `on DD-MM-YY:` fragment, because the label can come out garbled.
- **Licence**: MIT (same copyright holder and text as the HDFC entry above)
