# CardDemo data model: COBOL types, rounding and schema design

This document is the reviewed mapping of CardDemo's COBOL data types, arithmetic truncation and
VSAM datasets onto Python and PostgreSQL. It is the design authority for the models, migrations,
utilities, services and jobs that implement it: they follow it, and code that differs from it is a
defect. A later change to the mapping is made in this document first, and is shown by the reviewed
fixture diff under `batch/tests/fixtures/expected/`.

Related documents:

- [batch.md](batch.md): the batch chain, loader semantics, cycles and GDG archives.
- [architecture.md](architecture.md): layers and cross-cutting decisions, including data masking.
- [traceability.md](traceability.md): each legacy artefact, its disposition and its target path.

Offsets in this document are 0-based byte offsets into the copybook record.

## Conversion rules applied

The general conversion rules from the modernization request, as applied column by column in
[Schema design](#schema-design):

- `PIC X(n)` becomes `VARCHAR(n)`, or `CHAR(n)` for fixed-width codes.
- `PIC 9(n)` DISPLAY becomes `VARCHAR(n)` when the field is an identifier or key, which keeps its
    leading zeros, and a numeric type when the field is used arithmetically.
- Signed money, `PIC S9(n)V99`, becomes `NUMERIC(n+2,2)`.
- 10-byte `YYYY-MM-DD` date text becomes `DATE`.
- A VSAM KSDS key becomes the `PRIMARY KEY`. A VSAM alternate index (AIX) becomes an index on the
    same columns, unique or non-unique by cardinality.
- `FILLER` bytes are dropped. Each is recorded in a table comment with its offset and length, so
    record-length parity checks against the legacy flat files still add up.

Money is never floating point. Every monetary value is `NUMERIC` in PostgreSQL and `Decimal` in
Python.

Every signed field of the in-scope dataset copybooks and seed images is zoned DISPLAY with a
trailing sign overpunch, not `COMP-3`. The request's notes describe these fields as `COMP-3`; the
copybooks declare them DISPLAY. No in-scope seed image contains a `COMP` or `COMP-3` field, so no
packed decoder is built (see [Loader decoding](#loader-decoding)). Packed and binary usages remain
in program working storage only, with no column: the CBSTM03A amounts `S9(9)V99 COMP-3` (row 6 of
[PIC to type mapping](#pic-to-type-mapping)) and the `9(n) COMP-3` and `S9(4) COMP` counters
(row 7).

## Numeric storage rule

No in-scope arithmetic statement uses `ROUNDED` or `ON SIZE ERROR`. This was established by a
full-text search of `cbl/CBACT04C.cbl`, `cbl/CBTRN02C.cbl`, `cbl/CBTRN03C.cbl`,
`cbl/CBSTM03A.CBL`, `cbl/COBIL00C.cbl`, `cbl/COTRN02C.cbl` and `cbl/COACTUPC.cbl`.

Every arithmetic result and every numeric `MOVE` is therefore stored by
`StoreInto(value, intDigits, decDigits)` in `backend/app/utils/decimal_utils.py`, in two steps:

1. `quantize(Decimal(10) ** -decDigits, rounding=ROUND_DOWN)`, which is truncation toward zero.
2. High-order truncation of the integer part modulo `10 ** intDigits`, keeping the sign.

This is IBM Enterprise COBOL behaviour without `ROUNDED` and without `ON SIZE ERROR`.

All arithmetic runs in `COBOL_CONTEXT = Context(prec=31)`, which covers the 30-digit
`ARITH(COMPAT)` intermediate limit.

Worked examples:

| Input | Receiver | Stored result | Why |
|---|---|---|---|
| `1234.567` | `S9(10)V99` | `1234.56` | The surplus fractional digit is truncated |
| `-0.019` | `S9(09)V99` | `-0.01` | Truncation is toward zero and keeps the sign |
| `1000000000.50` | `S9(09)V99` | `0.50` | The high-order digit is lost: the COBIL00C boundary |
| `999999` + 1 | `9(06)` counter | `000000` | The counter width rule |

## PIC to type mapping

The mapping has ten rows, numbered alike in both tables and in the notes below. Screen inputs
arrive as raw text, not in these types (see [Screen input](#screen-input)).

The PostgreSQL type, the Python type and the response-model type of each pattern:

| # | PIC / USAGE pattern | PostgreSQL | Python | Response model |
|---|---|---|---|---|
| 1 | `9(n)` DISPLAY, identifier | `VARCHAR(n)`, digit check | `str` | `str` |
| 2 | `9(n)` DISPLAY, code | `SMALLINT` | `int` | `int` |
| 3 | `S9(10)V99` DISPLAY zoned | `NUMERIC(12,2)` | `Decimal` | `Decimal` |
| 4 | `S9(09)V99` DISPLAY | `NUMERIC(11,2)` | `Decimal` | `Decimal` |
| 5 | `S9(04)V99` DISPLAY | `NUMERIC(6,2)` | `Decimal` | `Decimal` |
| 6 | `S9(9)V99 COMP-3` | No column | `Decimal` | |
| 7 | Counters | No column | `int` | |
| 8 | `X(n)` | `VARCHAR(n)` right-trimmed | `str` | `str` |
| 9 | `X(10)` date text `YYYY-MM-DD` | `DATE` (nullable for blank) | `date` | `str` (ISO) |
| 10 | `X(26)` timestamp | `TIMESTAMP(6)` plus format | `datetime` | `str` |

The sign handling and truncation of each pattern:

| # | PIC / USAGE pattern | Sign handling | Truncation |
|---|---|---|---|
| 1 | `9(n)` DISPLAY, identifier | Unsigned | Zero-padded to n |
| 2 | `9(n)` DISPLAY, code | Unsigned | Modulo `10**n` |
| 3 | `S9(10)V99` DISPLAY zoned | Trailing overpunch | `StoreInto(v, 10, 2)` |
| 4 | `S9(09)V99` DISPLAY | Trailing overpunch | `StoreInto(v, 9, 2)` |
| 5 | `S9(04)V99` DISPLAY | Trailing overpunch | `StoreInto(v, 4, 2)` |
| 6 | `S9(9)V99 COMP-3` | Signed | `StoreInto(v, 9, 2)` |
| 7 | Counters | Unsigned or signed binary | Width rule |
| 8 | `X(n)` | n/a | Right-padded with spaces |
| 9 | `X(10)` date text `YYYY-MM-DD` | n/a | n/a |
| 10 | `X(26)` timestamp | n/a | [Timestamps](#timestamps) |

Notes by row:

1. In-scope fields: ACCT-ID `9(11)`, CUST-ID `9(09)`, XREF-CUST-ID, XREF-ACCT-ID, CARD-ACCT-ID,
    CUST-SSN `9(09)`, CARD-CVV-CD `9(03)` and TRAN-MERCHANT-ID `9(09)`. The digit check is
    `^[0-9]{n}$`. CARD-CVV-CD is `CHAR(3)` rather than `VARCHAR(3)`. The value is zero-padded to n
    on every numeric-to-ID move, for example `NUMVAL` into ACCT-ID.
2. In-scope fields: TRAN-CAT-CD, TRANCAT-CD and DIS-TRAN-CAT-CD `9(04)`, and
    CUST-FICO-CREDIT-SCORE `9(03)`. The response renders the value zero-padded to n. A move from a
    signed field stores the absolute value.
3. In-scope fields: ACCT-CURR-BAL, ACCT-CREDIT-LIMIT, ACCT-CASH-CREDIT-LIMIT, ACCT-CURR-CYC-CREDIT
    and ACCT-CURR-CYC-DEBIT.
4. In-scope fields: TRAN-AMT, DALYTRAN-AMT and TRAN-CAT-BAL, plus the working fields WS-TEMP-BAL,
    WS-MONTHLY-INT, WS-TOTAL-INT, WS-PAGE-TOTAL, WS-ACCOUNT-TOTAL and WS-GRAND-TOTAL.
5. In-scope field: DIS-INT-RATE.
6. In-scope fields: CBSTM03A WS-TOTAL-AMT and WS-TRN-AMT, in working storage only.
7. Counter patterns: `9(n)`, `9(n) COMP-3` and `S9(4) COMP`. In-scope fields: record counts,
    CBTRN03C WS-LINE-COUNTER, and CBSTM03A CR-CNT, TR-CNT and WS-TRCT. Width rule: every `ADD 1`
    stores `(n + 1) % 10**digits` for the receiver's PIC digits, with no size check, as the source
    has none.
8. Every `X(n)` field stored as `VARCHAR(n)`, key columns included, is `NOT NULL DEFAULT ''`.
    `CHAR(n)` is used instead for fixed codes and carries no empty default; the timestamp format
    columns default to a single space, as their tables say. `password_hash` replaces SEC-USR-PWD
    rather than mapping it, so it is a required `VARCHAR(255)` with no default. Fixed-width output
    is right-padded with spaces.
9. A blank date is stored as `NULL`.
10. The column is `TIMESTAMP(6) WITHOUT TIME ZONE`, nullable, plus a `CHAR(1)` format column. The
    response model carries the 26-character text. Truncation follows the
    [Timestamps](#timestamps) rules.

## Arithmetic statements and numeric moves

The specified arithmetic statements and numeric moves of the ported programs are below, as 13
numbered rows. Any other arithmetic result or numeric `MOVE` in those programs, for example the
total reset and counter increment at `cbl/CBTRN03C.cbl` lines 298–299, is stored by the
[numeric storage rule](#numeric-storage-rule), and every counter `ADD 1` follows the width rule of
PIC row 7.

The source of each row, and its receiver and intermediate precision:

| # | Source | Receiver and intermediate precision |
|---|---|---|
| 1 | `cbl/CBACT04C.cbl` lines 464–465 | Note 1 |
| 2 | `cbl/CBACT04C.cbl` line 467 | Exact |
| 3 | `cbl/CBACT04C.cbl` lines 352–354 | Exact |
| 4 | `cbl/CBACT04C.cbl` lines 474–479 | Note 4 |
| 5 | `cbl/CBTRN02C.cbl` lines 403–405 | Note 5 |
| 6 | `cbl/CBTRN02C.cbl` lines 508, 527 | Exact |
| 7 | `cbl/CBTRN02C.cbl` lines 547–552 | Exact; note 7 |
| 8 | `cbl/COBIL00C.cbl` line 224 | Note 8 |
| 9 | `cbl/COBIL00C.cbl` line 234 | Note 9 |
| 10 | `cbl/COTRN02C.cbl` lines 444–449; `cbl/COBIL00C.cbl` lines 212–217 | Note 10 |
| 11 | `cbl/CBTRN03C.cbl` lines 200, 287, 297 | Exact |
| 12 | `cbl/CBSTM03A.CBL` line 429 | Exact |
| 13 | `cbl/COTRN02C.cbl` lines 383, 456; `cbl/COACTUPC.cbl` lines 1079–1135 | Note 13 |

The COBOL statement of each row and its Python expression:

- **Row 1.**
    - COBOL: `COMPUTE WS-MONTHLY-INT = (TRAN-CAT-BAL * DIS-INT-RATE) / 1200`
    - Python: `StoreInto(tranCatBal * disIntRate / Decimal(1200), 9, 2)`
- **Row 2.**
    - COBOL: `ADD WS-MONTHLY-INT TO WS-TOTAL-INT`
    - Python: `StoreInto(total + monthly, 9, 2)`
- **Row 3.**
    - COBOL: `ADD WS-TOTAL-INT TO ACCT-CURR-BAL`; cycle credit and debit `MOVE 0`
    - Python: `StoreInto(bal + total, 10, 2)`; both cycle fields `Decimal("0.00")`
- **Row 4.**
    - COBOL: `ADD 1 TO WS-TRANID-SUFFIX`, then `STRING PARM-DATE WS-TRANID-SUFFIX`
    - Python: `suffix = (suffix + 1) % 10**6`; `tranId = parmDate + f"{suffix:06d}"`
- **Row 5.**
    - COBOL: `COMPUTE WS-TEMP-BAL = ACCT-CURR-CYC-CREDIT - ACCT-CURR-CYC-DEBIT + DALYTRAN-AMT`
    - Python: `StoreInto(cycCredit - cycDebit + amt, 9, 2)`
- **Row 6.**
    - COBOL: `ADD DALYTRAN-AMT TO TRAN-CAT-BAL` (create or update)
    - Python: `StoreInto(catBal + amt, 9, 2)`
- **Row 7.**
    - COBOL: `ADD DALYTRAN-AMT TO ACCT-CURR-BAL`; then to CYC-CREDIT or CYC-DEBIT
    - Python: `StoreInto(field + amt, 10, 2)`
- **Row 8.**
    - COBOL: `MOVE ACCT-CURR-BAL TO TRAN-AMT`
    - Python: `tranAmt = StoreInto(currBal, 9, 2)`
- **Row 9.**
    - COBOL: `COMPUTE ACCT-CURR-BAL = ACCT-CURR-BAL - TRAN-AMT`
    - Python: `StoreInto(currBal - tranAmt, 10, 2)`
- **Row 10.**
    - COBOL: `MOVE TRAN-ID TO WS-TRAN-ID-N` / `WS-TRAN-ID-NUM`, then `ADD 1`
    - Python: `nextId = (int(highestId or "0") + 1) % 10**16`, zero-padded to 16
- **Row 11.**
    - COBOL: `ADD TRAN-AMT TO WS-PAGE-TOTAL WS-ACCOUNT-TOTAL`;
        `ADD WS-PAGE-TOTAL TO WS-GRAND-TOTAL`
    - Python: `StoreInto(total + amt, 9, 2)`
- **Row 12.**
    - COBOL: `ADD TRNX-AMT TO WS-TOTAL-AMT`
    - Python: `StoreInto(total + amt, 9, 2)`
- **Row 13.**
    - COBOL: `FUNCTION NUMVAL-C(...)` on screen amounts
    - Python: `cobol_intrinsics.NumvalC(rawText)` after the program's edit passes

Notes by statement:

- **Note 1.** The product is exact, with 4 decimal places. `ARITH(COMPAT)` keeps 4 quotient
    decimals and the store truncates to 2, which equals one truncation of the exact quotient to 2
    places.
- **Note 4.** WS-TRANID-SUFFIX is `9(06)` (`cbl/CBACT04C.cbl` line 173). The suffix wraps from
    999999 to 000000.
- **Note 5.** The receiver is `S9(09)V99` (`cbl/CBTRN02C.cbl` line 187). The result is compared
    with `creditLimit >= tempBal`.
- **Note 7.** The amount is added to CYC-CREDIT when it is ≥ 0, and to CYC-DEBIT otherwise.
- **Note 8.** This moves `S9(10)V99` into `S9(09)V99`. The high-order digit is lost when the
    balance reaches 1,000,000,000.00.
- **Note 9.** The result is zero only while the balance is below 1,000,000,000.00. Test: a balance
    of `1000000000.50` pays `0.50` and leaves `1000000000.00`.
- **Note 10.** The receiver is `9(16)`. An empty table gives TRAN-ID zeros through the `ENDFILE`
    branch, so the first ID is `0000000000000001`. `9999999999999999` wraps to
    `0000000000000000`.
- **Note 13.** The value is then stored with `StoreInto(v, 9, 2)` for a transaction amount, or
    `StoreInto(v, 10, 2)` for the account amounts.

### Screen input

Request models declare every screen field as `str` and enforce only two structural limits: the BMS
field length as `max_length`, and the code-page-037 printable repertoire. A request outside them is
rejected with 422. Type, sign, range and format edits run in the service's port of the program's
edits, on the raw text.

`TestNumvalC`, `NumvalC`, `TestNumval` and `Numval` in `backend/app/utils/cobol_intrinsics.py`
port the `TEST-NUMVAL-C`, `NUMVAL-C`, `TEST-NUMVAL` and `NUMVAL` intrinsic functions. Their
grammars:

- All four accept leading and trailing spaces, at most one sign (a leading `+` or `-`, or a
    trailing `+`, `-`, `CR` or `DB`), and `.` as the decimal point.
- `TEST-NUMVAL-C` and `NUMVAL-C` also accept a `$` currency sign, after the leading sign when there
    is one, and `,` grouping in the integer part.
- `TEST-NUMVAL` and `NUMVAL` accept neither `$` nor `,`.

Numeric conversion happens only after the program's edit passes. For example:

- CAUP accepts `$1,234.567` for a credit limit and stores `1234.56`.
- CT02 rejects the same text with `Amount should be in format -99999999.99`.
- CT02 accepts `+00000050.00`.

## Loader decoding

`DecodeZoned(text, scale)` in `backend/app/utils/decimal_utils.py` and the image reader in
`backend/app/seed/image_reader.py` decode the seed images:

- EBCDIC images are decoded with code page 037 first, so the zone nibble becomes a character:
    - `X'C0'`–`X'C9'` become `{` and `A`–`I` (positive);
    - `X'D0'`–`X'D9'` become `}` and `J`–`R` (negative);
    - `X'F0'`–`X'F9'` become plain digits.
- The same overpunch decoder then serves both encodings.
- No packed (`COMP-3`) decoder is built. `AWS.M2.CARDDEMO.EXPORT.DATA.PS` is the only image with
    packed or binary fields, and it belongs to the out-of-scope CBEXPORT.
- `EncodeZoned` produces the overpunch form for fixed-width GDG records.

Image framing:

- ASCII images in `data/ASCII/` are CRLF-terminated text. The exceptions are the final records of
    `data/ASCII/tcatbal.txt` and `data/ASCII/trantype.txt`, which end with a lone LF.
- `data/ASCII/cardxref.txt` lines are 36 bytes: the 14-byte trailing FILLER is trimmed.
- EBCDIC images in `data/EBCDIC/` are fixed-length code page 037 records with no terminators.
- `AWS.M2.CARDDEMO.USRSEC.PS` and `AWS.M2.CARDDEMO.DALYTRAN.PS.INIT` exist only in EBCDIC.

## Timestamps

The 26-byte timestamp fields have four producers, each with its own text form:

| Producer | Text form | Format code |
|---|---|---|
| DALYTRAN `ORIG-TS` | `YYYY-MM-DD HH:MM:SS.ffffff` | `S` |
| POSTTRAN `PROC-TS`; INTCALC `ORIG-TS` and `PROC-TS` | `YYYY-MM-DD-HH.MM.SS.cc0000` | `D` |
| CB00 `ORIG-TS` and `PROC-TS` | `YYYY-MM-DD HH:MM:SS.000000` | `S` |
| CT02 `ORIG-TS` and `PROC-TS` (the entered date) | `YYYY-MM-DD` followed by 16 spaces | `T` |
| DALYTRAN `PROC-TS` (blank in every sample record) | 26 spaces | blank; the value is `NULL` |

Sources and details by producer:

- **DALYTRAN `ORIG-TS`.** Copied unchanged into the posted transaction (`cbl/CBTRN02C.cbl`
    line 436). The sample value is `2022-06-10 19:27:53.000000`.
- **POSTTRAN and INTCALC.** `Z-GET-DB2-FORMAT-TIMESTAMP` builds the text from
    `FUNCTION CURRENT-DATE` (`cbl/CBTRN02C.cbl` lines 692–703; `cbl/CBACT04C.cbl` lines 497–498,
    613–624). It carries hundredths of a second (`cc`) and a fixed `0000` tail.
- **CB00.** `ASKTIME` and `FORMATTIME` fill the CSDAT01Y layout (`cbl/COBIL00C.cbl`
    lines 249–266; `cpy/CSDAT01Y.cpy`, `WS-TIMESTAMP`).
- **CT02.** The entered date is moved into both fields (`cbl/COTRN02C.cbl` lines 464–465).

Rules:

- **Storage.** The value goes in the `TIMESTAMP(6)` column, and the form in `orig_ts_format` or
    `proc_ts_format` (`CHAR(1)`), on both `transactions` and `daily_transactions`. A `T` value is
    stored as midnight.
- **Decoding.** `record_layouts.DecodeTimestamp(text)` recognises exactly these four forms and
    returns `(value, format)`. Any other text fails the load with exit 12.
- **Clock precision.** Batch `Clock.Now()` is truncated to hundredths of a second. CB00's clock is
    truncated to whole seconds.
- **Serialization.** `record_layouts.EncodeTimestamp(value, timestampFormat)` rebuilds the
    original 26 bytes. GDG records therefore reproduce the source bytes exactly, and API responses
    show the same text the map showed.

## Schema design

### Conventions

- **Schemas.** One per dataset group: `security`, `accounts`, `customers`, `cards`, `xref`,
    `transactions`, `reference` and `disclosure`, plus `batch` for orchestration.
- **Nullability.** Every column is `NOT NULL` unless it is marked `NULL`.
- **FILLER.** Every `FILLER` is dropped and recorded as a `COMMENT ON TABLE` giving its offset and
    length.
- **Key collation.** Key columns use `COLLATE "C"`.
- **EBCDIC ordering.** EBCDIC key order comes from generated sort columns:
    - Revision `0003` creates the immutable SQL function `ebcdic_sort_key(text)`, a `translate()`
        that maps each code page 037 printable character to the Unicode code point equal to its
        cp037 byte value. For example, space becomes U+0040, `a` becomes U+0081, `A` becomes
        U+00C1 and `0` becomes U+00F0.
    - It also adds the stored generated columns `user_id_sort`, `card_num_sort` and
        `tran_id_sort`, typed `TEXT`, declared `COLLATE "C"` and indexed.
    - Under C collation, UTF-8 byte order equals code-point order. Ordering by these columns
        therefore equals EBCDIC byte order for every character a 3270 field can carry.
    - Every key-ordered read uses them: the browses, the highest-ID lookup, batch key-order reads,
        and the report and statement orderings.
    - The digit-checked keys `acct_id` and `cust_id` sort identically in both orders and need no
        sort column.
- **Foreign keys.** Foreign keys are `DEFERRABLE INITIALLY IMMEDIATE`, so loaders can replace a
    table inside one transaction.

To read users in EBCDIC key order, order by the generated column:

```sql
SELECT user_id
FROM security.users
ORDER BY user_id_sort;
```

For the user IDs `a1`, `AA`, `ADMIN001`, `A1` and `1A`, the output is lower case before upper case
before digits:

```text
a1
AA
ADMIN001
A1
1A
```

### `security.users`

Copybook `cpy/CSUSR01Y.cpy` lines 17–23: 80 bytes, `FILLER X(23)` at offset 57. Replaces the
USRSEC KSDS, `KEYS(8,0)`.

| Column | Source PIC (offset) | Type | Key and notes |
|---|---|---|---|
| `user_id` | SEC-USR-ID `X(08)` (0) | `VARCHAR(8)` | PK (`KEYS(8,0)`) |
| `user_id_sort` | generated | `TEXT` | Generated and stored; indexed; ordering for CU00 |
| `first_name` | SEC-USR-FNAME `X(20)` (8) | `VARCHAR(20)` | |
| `last_name` | SEC-USR-LNAME `X(20)` (28) | `VARCHAR(20)` | |
| `password_hash` | replaces SEC-USR-PWD `X(08)` (48) | `VARCHAR(255)` | bcrypt; no plaintext |
| `user_type` | SEC-USR-TYPE `X(01)` (56) | `CHAR(1)` | No check constraint |
| `created_at` | new audit column | `TIMESTAMPTZ DEFAULT now()` | |
| `updated_at` | new audit column | `TIMESTAMPTZ DEFAULT now()` | Trigger, revision `0006` |

Notes:

- `user_id_sort` is `TEXT GENERATED ALWAYS AS (ebcdic_sort_key(user_id)) STORED`.
- `updated_at` is maintained by a trigger created in revision `0006`.
- `user_type` has no check constraint because COUSR01C accepts any value. The admin role is exactly
    `A`; every other value is a regular user (see [architecture.md](architecture.md)).

### `accounts.accounts`

Copybook `cpy/CVACT01Y.cpy` lines 4–17: 300 bytes, `FILLER X(178)` at offset 122. Replaces the
ACCTDATA KSDS, `KEYS(11 0)`, with no AIX.

| Column | Source PIC (offset) | Type | Key and notes |
|---|---|---|---|
| `acct_id` | ACCT-ID `9(11)` (0) | `VARCHAR(11)` | PK (`KEYS(11 0)`); digit check |
| `active_status` | ACCT-ACTIVE-STATUS `X(01)` (11) | `CHAR(1)` | |
| `curr_balance` | ACCT-CURR-BAL `S9(10)V99` (12) | `NUMERIC(12,2)` | Zoned DISPLAY, not `COMP-3` |
| `credit_limit` | ACCT-CREDIT-LIMIT `S9(10)V99` (24) | `NUMERIC(12,2)` | As `curr_balance` |
| `cash_credit_limit` | ACCT-CASH-CREDIT-LIMIT `S9(10)V99` (36) | `NUMERIC(12,2)` | See notes |
| `open_date` | ACCT-OPEN-DATE `X(10)` (48) | `DATE NULL` | |
| `expiration_date` | ACCT-EXPIRAION-DATE `X(10)` (58) | `DATE NULL` | Source name misspelt (sic) |
| `reissue_date` | ACCT-REISSUE-DATE `X(10)` (68) | `DATE NULL` | |
| `curr_cycle_credit` | ACCT-CURR-CYC-CREDIT `S9(10)V99` (78) | `NUMERIC(12,2)` | |
| `curr_cycle_debit` | ACCT-CURR-CYC-DEBIT `S9(10)V99` (90) | `NUMERIC(12,2)` | |
| `addr_zip` | ACCT-ADDR-ZIP `X(10)` (102) | `VARCHAR(10)` | |
| `group_id` | ACCT-GROUP-ID `X(10)` (112) | `VARCHAR(10) DEFAULT ''` | No FK |

Notes:

- `curr_balance`, `credit_limit` and `cash_credit_limit` are zoned DISPLAY, not `COMP-3` as the
    request's notes say.
- The request asks for a foreign key from `group_id` to `disclosure_groups.group_id`. There is
    none, because that column is not unique there, and CBACT04C falls back to `DEFAULT` for
    unmatched groups (`cbl/CBACT04C.cbl` lines 415–460).

### `customers.customers`

Copybook `cpy/CVCUS01Y.cpy` lines 4–23: 500 bytes, `FILLER X(168)` at offset 332. Replaces the
CUSTDATA KSDS, `KEYS(9 0)`. `cpy/CUSTREC.cpy` is identical apart from the DOB field name,
`CUST-DOB-YYYYMMDD`.

| Column | Source PIC (offset) | Type | Key and notes |
|---|---|---|---|
| `cust_id` | CUST-ID `9(09)` (0) | `VARCHAR(9)` | PK (`KEYS(9 0)`); digit check |
| `first_name` | CUST-FIRST-NAME `X(25)` (9) | `VARCHAR(25)` | |
| `middle_name` | CUST-MIDDLE-NAME `X(25)` (34) | `VARCHAR(25)` | |
| `last_name` | CUST-LAST-NAME `X(25)` (59) | `VARCHAR(25)` | |
| `addr_line_1` | CUST-ADDR-LINE-1 `X(50)` (84) | `VARCHAR(50)` | |
| `addr_line_2` | CUST-ADDR-LINE-2 `X(50)` (134) | `VARCHAR(50)` | |
| `addr_line_3` | CUST-ADDR-LINE-3 `X(50)` (184) | `VARCHAR(50)` | |
| `addr_state_cd` | CUST-ADDR-STATE-CD `X(02)` (234) | `CHAR(2)` | |
| `addr_country_cd` | CUST-ADDR-COUNTRY-CD `X(03)` (236) | `CHAR(3)` | |
| `addr_zip` | CUST-ADDR-ZIP `X(10)` (239) | `VARCHAR(10)` | |
| `phone_num_1` | CUST-PHONE-NUM-1 `X(15)` (249) | `VARCHAR(15)` | |
| `phone_num_2` | CUST-PHONE-NUM-2 `X(15)` (264) | `VARCHAR(15)` | |
| `ssn` | CUST-SSN `9(09)` (279) | `VARCHAR(9)` | Digit check; masked in API responses, see notes |
| `govt_issued_id` | CUST-GOVT-ISSUED-ID `X(20)` (288) | `VARCHAR(20)` | |
| `date_of_birth` | CUST-DOB-YYYY-MM-DD `X(10)` (308) | `DATE NULL` | |
| `eft_account_id` | CUST-EFT-ACCOUNT-ID `X(10)` (318) | `VARCHAR(10)` | |
| `primary_holder_ind` | CUST-PRI-CARD-HOLDER-IND `X(01)` (328) | `CHAR(1)` | |
| `fico_credit_score` | CUST-FICO-CREDIT-SCORE `9(03)` (329) | `SMALLINT` | |

Notes:

- `ssn` is stored in full, because COACTUPC edits it. API responses mask it as `***-**-` plus the
    last four digits (`MaskSsn` in `backend/app/core/security.py`), except
    `GET /accounts/{acctId}/edit` and `PUT /accounts/{acctId}`, which carry it unmasked for that
    edit (see [architecture.md](architecture.md)).

### `cards.cards`

Copybook `cpy/CVACT02Y.cpy` lines 4–11: 150 bytes, `FILLER X(59)` at offset 91. Replaces the
CARDDATA KSDS, `KEYS(16 0)`, and its CARDAIX alternate index, `KEYS(11 16)`.

| Column | Source PIC (offset) | Type | Key and notes |
|---|---|---|---|
| `card_num` | CARD-NUM `X(16)` (0) | `VARCHAR(16)` | PK (`KEYS(16 0)`); masked in API responses |
| `card_num_sort` | generated | `TEXT` | `ebcdic_sort_key(card_num)`, stored; indexed |
| `acct_id` | CARD-ACCT-ID `9(11)` (16) | `VARCHAR(11)` | Digit check; FK; indexed |
| `cvv_cd` | CARD-CVV-CD `9(03)` (27) | `CHAR(3)` | Digit check; stored for CARDFILE record parity |
| `embossed_name` | CARD-EMBOSSED-NAME `X(50)` (30) | `VARCHAR(50)` | |
| `expiration_date` | CARD-EXPIRAION-DATE `X(10)` (80) | `DATE NULL` | Source name misspelt (sic) |
| `active_status` | CARD-ACTIVE-STATUS `X(01)` (90) | `CHAR(1)` | |

Notes:

- `card_num_sort` gives the ordering for CCLI and for the statement and report sorts.
- `acct_id` has a foreign key to `accounts.accounts`.
- The index `ix_cards_acct_id` on `acct_id` replaces the CARDAIX `KEYS(11 16)` that the request's
    indexing notes name.
- `cvv_cd` appears in no API schema.

### `xref.card_xref`

Copybook `cpy/CVACT03Y.cpy` lines 4–8: 50 bytes, `FILLER X(14)` at offset 36. Replaces the
CARDXREF KSDS, `KEYS(16 0)`, and its CXACAIX alternate index, `KEYS(11,25)`.

The cross-reference is a table, not the view the request recommends, because `CARD-RECORD` has no
customer ID (`cpy/CVACT02Y.cpy` lines 4–11). The cross-reference is the only record linking a card
to its customer.

| Column | Source PIC (offset) | Type | Key and notes |
|---|---|---|---|
| `card_num` | XREF-CARD-NUM `X(16)` (0) | `VARCHAR(16)` | PK; FK → `cards.cards` |
| `cust_id` | XREF-CUST-ID `9(09)` (16) | `VARCHAR(9)` | FK → `customers.customers`; indexed |
| `acct_id` | XREF-ACCT-ID `9(11)` (25) | `VARCHAR(11)` | FK → `accounts.accounts`; indexed |

Notes:

- The index on `cust_id` is `ix_card_xref_cust_id`.
- The index on `acct_id` is `ix_card_xref_acct_id`, which replaces CXACAIX, `KEYS(11,25)`.

### `transactions.transactions`

Copybook `cpy/CVTRA05Y.cpy` lines 4–18: 350 bytes, `FILLER X(20)` at offset 330. Replaces the
TRANSACT KSDS, `KEYS(16 0)`, and its alternate index, `KEYS(26 304)`.

| Column | Source PIC (offset) | Type | Key and notes |
|---|---|---|---|
| `tran_id` | TRAN-ID `X(16)` (0) | `VARCHAR(16)` | PK (`KEYS(16 0)`) |
| `tran_id_sort` | generated | `TEXT` | `ebcdic_sort_key(tran_id)`, stored; indexed |
| `tran_type_cd` | TRAN-TYPE-CD `X(02)` (16) | `CHAR(2)` | No FK |
| `tran_cat_cd` | TRAN-CAT-CD `9(04)` (18) | `SMALLINT` | No FK |
| `tran_source` | TRAN-SOURCE `X(10)` (22) | `VARCHAR(10)` | |
| `tran_desc` | TRAN-DESC `X(100)` (32) | `VARCHAR(100)` | |
| `tran_amt` | TRAN-AMT `S9(09)V99` (132) | `NUMERIC(11,2)` | Zoned |
| `merchant_id` | TRAN-MERCHANT-ID `9(09)` (143) | `VARCHAR(9)` | Digit check |
| `merchant_name` | TRAN-MERCHANT-NAME `X(50)` (152) | `VARCHAR(50)` | |
| `merchant_city` | TRAN-MERCHANT-CITY `X(50)` (202) | `VARCHAR(50)` | |
| `merchant_zip` | TRAN-MERCHANT-ZIP `X(10)` (252) | `VARCHAR(10)` | |
| `card_num` | TRAN-CARD-NUM `X(16)` (262) | `VARCHAR(16)` | FK → `cards.cards`; indexed |
| `orig_ts` | TRAN-ORIG-TS `X(26)` (278) | `TIMESTAMP(6) WITHOUT TIME ZONE NULL` | |
| `proc_ts` | TRAN-PROC-TS `X(26)` (304) | `TIMESTAMP(6) WITHOUT TIME ZONE NULL` | Indexed |
| `orig_ts_format` | the text form of `orig_ts` | `CHAR(1) DEFAULT ' '` | `S`, `D`, `T` or blank |
| `proc_ts_format` | the text form of `proc_ts` | `CHAR(1) DEFAULT ' '` | `S`, `D`, `T` or blank |

Notes:

- `tran_id_sort` gives the ordering for CT00, the highest-ID lookup, TRANBKP, COMBTRAN and
    CREASTMT.
- The type and category codes have no foreign key, because COTRN02C accepts any numeric codes and
    CBTRN02C checks none.
- `orig_ts` and `proc_ts` are not `TIMESTAMPTZ`, as the request says: the copybook carries no zone,
    and a zone would shift values.
- The index on `card_num` is `ix_transactions_card_num`.
- The index on `proc_ts` is `ix_transactions_proc_ts`, which replaces the alternate index
    `KEYS(26 304)`.
- The format codes are the text forms defined in [Timestamps](#timestamps).

### `transactions.daily_transactions`

Copybook `cpy/CVTRA06Y.cpy` lines 4–18: 350 bytes, `FILLER X(20)` at offset 330. This is the
staging table for the sequential DALYTRAN daily file.

`record_seq` is the primary key. The other columns mirror `transactions.transactions` with the
DALYTRAN field names, and with the same types, widths and format columns:

| Column | Source PIC (offset) | Type | Key and notes |
|---|---|---|---|
| `record_seq` | the record's position in the daily file | `INTEGER` | PK; starts at 1 |
| `tran_id` | DALYTRAN-ID `X(16)` (0) | `VARCHAR(16)` | Indexed, non-unique |
| `tran_type_cd` | DALYTRAN-TYPE-CD `X(02)` (16) | `CHAR(2)` | |
| `tran_cat_cd` | DALYTRAN-CAT-CD `9(04)` (18) | `SMALLINT` | |
| `tran_source` | DALYTRAN-SOURCE `X(10)` (22) | `VARCHAR(10)` | |
| `tran_desc` | DALYTRAN-DESC `X(100)` (32) | `VARCHAR(100)` | |
| `tran_amt` | DALYTRAN-AMT `S9(09)V99` (132) | `NUMERIC(11,2)` | Zoned |
| `merchant_id` | DALYTRAN-MERCHANT-ID `9(09)` (143) | `VARCHAR(9)` | |
| `merchant_name` | DALYTRAN-MERCHANT-NAME `X(50)` (152) | `VARCHAR(50)` | |
| `merchant_city` | DALYTRAN-MERCHANT-CITY `X(50)` (202) | `VARCHAR(50)` | |
| `merchant_zip` | DALYTRAN-MERCHANT-ZIP `X(10)` (252) | `VARCHAR(10)` | |
| `card_num` | DALYTRAN-CARD-NUM `X(16)` (262) | `VARCHAR(16)` | No FK |
| `orig_ts` | DALYTRAN-ORIG-TS `X(26)` (278) | `TIMESTAMP(6) WITHOUT TIME ZONE NULL` | |
| `proc_ts` | DALYTRAN-PROC-TS `X(26)` (304) | `TIMESTAMP(6) WITHOUT TIME ZONE NULL` | |
| `orig_ts_format` | the text form of `orig_ts` | `CHAR(1) DEFAULT ' '` | `S`, `D`, `T` or blank |
| `proc_ts_format` | the text form of `proc_ts` | `CHAR(1) DEFAULT ' '` | `S`, `D`, `T` or blank |

Notes:

- The index on `tran_id` is `ix_daily_transactions_tran_id`. It is not unique, because a
    sequential file may repeat an ID.
- `card_num` has no foreign key, so that POSTTRAN reject 100 (`INVALID CARD NUMBER FOUND`) stays
    reachable.
- The table has no generated sort column: POSTTRAN reads it in `record_seq` order.
- The ORM model is `backend/app/models/daily_transaction.py`, the eleventh dataset model.

Choice: of the request's two staging options, this design takes option (b), a separate
`daily_transactions` table, because COTRN00C and COTRN01C show only posted transactions. Option
(a), a status column on `transactions`, would put unposted daily rows into the CT00 browse, the
CT02 and CB00 highest-ID allocation, and TRANBKP, unless every one of those reads added a filter
the source never had.

Flow:

- `batch/loaders/dalytran.py`, chain step 12, replaces the table from the DALYTRAN image.
- POSTTRAN reads it in `record_seq` order, exactly as CBTRN02C reads the sequential file. POSTTRAN
    never updates or deletes staged rows, as the legacy daily file is input only.
- Revision `0002` seeds it with 300 rows.

### `transactions.transaction_category_balances`

Copybook `cpy/CVTRA01Y.cpy` lines 4–10: 50 bytes, `FILLER X(22)` at offset 28. Replaces the
TCATBALF KSDS, `KEYS(17 0)`.

| Column | Source PIC (offset) | Type | Key and notes |
|---|---|---|---|
| `acct_id` | TRANCAT-ACCT-ID `9(11)` (0) | `VARCHAR(11)` | PK part; FK → `accounts.accounts` |
| `tran_type_cd` | TRANCAT-TYPE-CD `X(02)` (11) | `CHAR(2)` | PK part |
| `tran_cat_cd` | TRANCAT-CD `9(04)` (13) | `SMALLINT` | PK part |
| `balance` | TRAN-CAT-BAL `S9(09)V99` (17) | `NUMERIC(11,2)` | |

Notes:

- The primary key is all three key columns (`KEYS(17 0)`).
- `acct_id` carries the `^[0-9]{11}$` digit check of [PIC row 1](#pic-to-type-mapping), because
    TRANCAT-ACCT-ID is a `9(11)` identifier. Revision `0005_pic9_digit_checks` creates it.
- There is no foreign key to the categories, because CBTRN02C creates a balance for any code
    (`cbl/CBTRN02C.cbl` lines 500–512).
- The request says INTCALC updates this table. It does not: INTCALC opens TCATBALF for input only
    (`cbl/CBACT04C.cbl` paragraph `0000-TCATBALF-OPEN`, line 236).

### `disclosure.disclosure_groups`

Copybook `cpy/CVTRA02Y.cpy` lines 4–10: 50 bytes, `FILLER X(28)` at offset 22. Replaces the
DISCGRP KSDS, `KEYS(16 0)`.

| Column | Source PIC (offset) | Type | Key and notes |
|---|---|---|---|
| `group_id` | DIS-ACCT-GROUP-ID `X(10)` (0) | `VARCHAR(10)` | PK part |
| `tran_type_cd` | DIS-TRAN-TYPE-CD `X(02)` (10) | `CHAR(2)` | PK part; FK part |
| `tran_cat_cd` | DIS-TRAN-CAT-CD `9(04)` (12) | `SMALLINT` | PK part; FK part |
| `interest_rate` | DIS-INT-RATE `S9(04)V99` (16) | `NUMERIC(6,2)` | Not `NUMERIC(5,2)` |

Notes:

- The primary key is all three key columns (`KEYS(16 0)`).
- The foreign key on (`tran_type_cd`, `tran_cat_cd`) references
    `reference.transaction_categories`.
- `interest_rate` is `NUMERIC(6,2)`, not the request's `NUMERIC(5,2)`, because `S9(04)V99` holds
    six digits.

### `reference.transaction_types`

Copybook `cpy/CVTRA03Y.cpy` lines 4–7: 60 bytes, `FILLER X(08)` at offset 52. Replaces the
TRANTYPE KSDS, `KEYS(2 0)`.

| Column | Source PIC (offset) | Type | Key and notes |
|---|---|---|---|
| `tran_type` | TRAN-TYPE `X(02)` (0) | `CHAR(2)` | PK (`KEYS(2 0)`) |
| `description` | TRAN-TYPE-DESC `X(50)` (2) | `VARCHAR(50)` | |

### `reference.transaction_categories`

Copybook `cpy/CVTRA04Y.cpy` lines 4–9: 60 bytes, `FILLER X(04)` at offset 56. Replaces the
TRANCATG KSDS, `KEYS(6 0)`.

| Column | Source PIC (offset) | Type | Key and notes |
|---|---|---|---|
| `tran_type_cd` | TRAN-TYPE-CD `X(02)` (0) | `CHAR(2)` | PK part; FK → `transaction_types` |
| `tran_cat_cd` | TRAN-CAT-CD `9(04)` (2) | `SMALLINT` | PK part |
| `description` | TRAN-CAT-TYPE-DESC `X(50)` (6) | `VARCHAR(50)` | |

Notes:

- The primary key is both key columns (`KEYS(6 0)`).
- `tran_type_cd` references `reference.transaction_types`.

### Supporting tables

These tables have no legacy equivalent. They hold application state rather than copybook records,
so they are SQLAlchemy Core tables in `backend/app/models/support_tables.py` (`LOGIN_ATTEMPTS`,
`CYCLES`, `CYCLE_STEPS`, `JOB_RUNS` and `GDG_GENERATIONS`) on the same metadata, not ORM models.
Their semantics are in [batch.md](batch.md) and, for the login throttle,
[architecture.md](architecture.md).

`security.login_attempts`:

- `user_id` `VARCHAR(8)`, PK.
- `failure_count` `SMALLINT`.
- `locked_until` `TIMESTAMPTZ NULL`.
- `last_failure_at` `TIMESTAMPTZ NULL`.

`batch.cycles`:

- `cycle_id` `BIGSERIAL`, PK.
- `status`: `OPEN`, `COMPLETED` or `ABANDONED`.
- `opened_by`: `SEED`, `seed-all` or `run-all`.
- `opened_at`.
- `closed_at` `NULL`.
- Partial index `ix_cycles_cycle_id_open` on `cycle_id` `WHERE status = 'OPEN'`, not unique: finding
    and abandoning the `OPEN` cycle reads no closed cycles.

`batch.cycle_steps`:

- PK (`cycle_id`, `step_no`), with `cycle_id` an FK → `batch.cycles` and `step_no` `SMALLINT`.
- `state`: `PENDING`, `COMPLETED`, `FAILED` or `INTERRUPTED`.
- `last_run_id`, FK → `batch.job_runs`, `NULL`.
- One row per chain step, created when the cycle opens.

`batch.job_runs`:

- `run_id` `BIGSERIAL`, PK.
- `cycle_id`, FK → `batch.cycles`, `NULL`.
- `step_no` `SMALLINT NULL`.
- `job_name`.
- `params` `JSONB`.
- `status`: `RUNNING`, `COMPLETED`, `FAILED`, `SKIPPED` or `INTERRUPTED`.
- `exit_code` `SMALLINT NULL`.
- `counts` `JSONB`.
- `started_at`.
- `finished_at` `NULL`.
- Run history only: any number of rows per step, with an index on (`cycle_id`, `step_no`).
- Partial index `ix_job_runs_run_id_running` on `run_id` `WHERE status = 'RUNNING'`, not unique:
    classifying interrupted runs at the start of every command reads no finished runs.

`batch.gdg_generations`:

- PK (`base_name`, `generation`).
- `file_path`.
- `record_count`.
- `sha256`.
- `state`: `COMMITTED`, `PUBLISHED`, `ROLLED_OFF`, `LOST` or `ABANDONED`.
- `run_id`, FK → `batch.job_runs`.
- `created_at`.
- Partial indexes, none unique, so catalog lookups read no rolled-off or abandoned history:
    `ix_gdg_generations_run_id_committed` on `run_id` `WHERE state = 'COMMITTED'` (catalog
    recovery and a run's publication); `ix_gdg_generations_run_id_lost` on `run_id`
    `WHERE state = 'LOST'` (the lost generations of a cycle's runs); and
    `ix_gdg_generations_base_name_generation_published` on (`base_name`, `generation`)
    `WHERE state = 'PUBLISHED'` (resolving `(0)` and `(-n)`, and retention beyond `LIMIT 5`).

### Alembic revisions

The revisions live in `backend/alembic/versions/`. The head is `0009`.

| Revision | Content |
|---|---|
| `0001_create_dataset_schemas_and_tables` | The schemas and dataset tables (notes below) |
| `0002_seed_sample_data` | Loads the eleven dataset tables from the EBCDIC images (notes below) |
| `0003_alternate_index_replacements` | The indexes and sort columns (notes below) |
| `0004_relationship_foreign_keys` | The foreign keys above |
| `0005_pic9_digit_checks` | The `^[0-9]{n}$` checks on the identifier columns |
| `0006_users_audit_timestamps` | The `updated_at` trigger on `security.users` |
| `0007_security_login_attempts` | `security.login_attempts` |
| `0008_batch_job_run_ledger` | The batch ledger tables; records the seed (notes below) |
| `0009_batch_gdg_catalog` | `batch.gdg_generations` |

Notes:

- **`0001`.** Nine schemas; the eleven dataset tables with primary keys, column types and FILLER
    comments.
- **`0002`.** `app.seed.sample_seed.LoadSampleData` loads the tables with the loader semantics of
    [batch.md](batch.md). It runs only when `APP_ENV` is `local` or `test`. Under `production` it
    logs a skip and loads nothing, so the sample users never reach a production database. A
    completed load sets the comment of schema `batch` to the seed marker, in the seed's own
    transaction: `CardDemo sample seed loaded by Alembic revision 0002`. Its downgrade truncates
    the tables and clears the marker, under the same `APP_ENV` gate.
- **`0003`.** The indexes named above; `ebcdic_sort_key(text)`; the three generated sort columns
    and their indexes.
- **`0008`.** `batch.cycles`, `batch.cycle_steps` and `batch.job_runs`. When `0002` loaded data,
    `0008` also records that seed as an `OPEN` cycle (`opened_by = 'SEED'`): one `job_runs` row
    per seeded loader, chain steps 1–5 and 7–12 `COMPLETED`, and the rest `PENDING`. `0008`
    judges that `0002` loaded data only when schema `batch` carries the seed marker and
    `accounts.accounts` still holds rows. Rows without the marker, such as data loaded while
    `0002` was skipped under `production`, record no cycle. In offline mode (`--sql`) `0008`
    records none either.

### Expected rows after migration

Expected rows after `alembic upgrade head`:

| Table | Rows |
|---|---|
| `security.users` | 10 |
| `accounts.accounts` | 50 |
| `customers.customers` | 50 |
| `cards.cards` | 50 |
| `xref.card_xref` | 50 |
| `reference.transaction_types` | 7 |
| `reference.transaction_categories` | 18 |
| `disclosure.disclosure_groups` | 51 |
| `transactions.transaction_category_balances` | 50 |
| `transactions.daily_transactions` | 300 |
| `transactions.transactions` | 0 |

Seed-count rationale for the category-balance and transaction counts:

- **Category balances are 50, not 49.** Both images hold 50 records:
    `data/EBCDIC/AWS.M2.CARDDEMO.TCATBALF.PS` is 2,500 bytes, or 50 records of 50 bytes, and
    `data/ASCII/tcatbal.txt` has 50 records.
- **Transactions are 0, not 301.** TRANFILE loads only the priming record
    (`jcl/TRANFILE.jcl` STEP15). In `data/EBCDIC/AWS.M2.CARDDEMO.DALYTRAN.PS.INIT`, bytes 0–341 of
    that record are `X'00'` and bytes 342–349 read `00000100`. The record is skipped, because
    PostgreSQL text cannot hold `NUL` and a PostgreSQL table needs no priming. A count of 301 would
    be the priming record plus all 300 daily records, which only a design loading the daily file
    into `transactions` holds. This design stages the daily file in `daily_transactions` instead,
    so `transactions` holds 0 rows until POSTTRAN runs.

### Data facts

Facts about the seed images that drive the design:

- All 50 accounts have a blank `ACCT-GROUP-ID`, so INTCALC always falls back to the `DEFAULT`
    group.
- Every category balance is (account, `01`, `0001`, `0.00`).
- The daily records use only the pairs (`01`, `0001`) × 250 and (`03`, `0001`) × 50, and carry a
    blank `PROC-TS`.
- Decoding is strictly by copybook offset: `ACCT-ADDR-ZIP` at offset 102 holds group-like codes
    such as `A000000000`, while `ACCT-GROUP-ID` at offset 112 is blank.
- The ASCII copies differ from the EBCDIC images in two records:
    - account `00000000049` has `addr_zip` `A000000000` in ASCII and `ZEROAPR` in EBCDIC
        (`data/ASCII/acctdata.txt` record 49);
    - disclosure-group key `DEFAULT`/`07`/`0001` has rate `0.00` in ASCII and `15.00` in EBCDIC
        (`data/ASCII/discgrp.txt` record 34).
- The JCL loads the EBCDIC images, so they are the default loader input.

### Settled choices

- **Cross-reference:** a table, `xref.card_xref`, not a view.
- **Daily-transaction staging:** option (b), the separate `transactions.daily_transactions` table.
- **GDG replacement:** fixed-width file archives with a database catalog,
    `batch.gdg_generations` (see [batch.md](batch.md)).
