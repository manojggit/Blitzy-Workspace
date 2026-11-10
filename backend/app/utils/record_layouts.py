"""Replaces: copybooks CVACT01Y, CVCUS01Y/CUSTREC, CVACT02Y, CVACT03Y, CVTRA01Y, CVTRA02Y,
CVTRA03Y, CVTRA04Y, CVTRA05Y, CVTRA06Y, CVTRA07Y, CSUSR01Y, COSTM01, the CBTRN02C DALYREJS
record, and the 26-byte timestamp texts of CSDAT01Y, CBTRN02C Z-GET-DB2-FORMAT-TIMESTAMP,
CBACT04C and COTRN02C.

This is the only module that knows copybook offsets. DecodeRecord turns one fixed-width record
(EBCDIC images decoded with code page 037 first) into a dict keyed by PostgreSQL column name,
and EncodeRecord rebuilds the record from such a dict for the GDG archives. FILLER content is
never read and is always written as spaces. DecodeTimestamp and EncodeTimestamp convert the four
26-character timestamp forms to a datetime plus a one-character format code and back, byte for
byte. RenderLine builds the CVTRA07Y report lines. The module performs no I/O and reads no
settings.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from app.utils.cobol_format import (
    REPORT_DETAIL_AMOUNT_PICTURE,
    REPORT_TOTAL_AMOUNT_PICTURE,
    FormatEdited,
    PictureError,
)
from app.utils.decimal_utils import DecodeZoned, EncodeZoned, StoreInto, ZonedDecimalError

# Field kinds. Unsigned 9(n) maps two ways: identifiers stay text with their leading zeros
# (KIND_DIGITS) while codes and counters become integers (KIND_UNSIGNED_NUMBER).
KIND_ALPHANUMERIC = "X"
KIND_DIGITS = "9"
KIND_UNSIGNED_NUMBER = "9N"
KIND_SIGNED_ZONED = "S9V"
KIND_DATE = "DATE10"
KIND_TIMESTAMP = "TS26"
VALID_KINDS = frozenset(
    {
        KIND_ALPHANUMERIC,
        KIND_DIGITS,
        KIND_UNSIGNED_NUMBER,
        KIND_SIGNED_ZONED,
        KIND_DATE,
        KIND_TIMESTAMP,
    }
)

# Timestamp format codes, stored in the orig_ts_format and proc_ts_format columns.
# "S": YYYY-MM-DD HH:MM:SS.ffffff (CSDAT01Y WS-TIMESTAMP, DALYTRAN ORIG-TS, CB00).
TIMESTAMP_FORMAT_SPACE = "S"
# "D": YYYY-MM-DD-HH.MM.SS.cc0000 (CBTRN02C and CBACT04C Z-GET-DB2-FORMAT-TIMESTAMP).
TIMESTAMP_FORMAT_DB2 = "D"
# "T": YYYY-MM-DD followed by 16 spaces (COTRN02C moves the entered date into both fields).
TIMESTAMP_FORMAT_DATE_ONLY = "T"
# " ": 26 spaces (DALYTRAN PROC-TS), stored as NULL.
TIMESTAMP_FORMAT_BLANK = " "
TIMESTAMP_LENGTH = 26
DATE_LENGTH = 10
BLANK_TIMESTAMP = " " * TIMESTAMP_LENGTH
BLANK_DATE = " " * DATE_LENGTH
# A TS26 field decodes into two columns: its value under the field name, its form code here.
TIMESTAMP_FORMAT_COLUMN_SUFFIX = "_format"
FIXED_KIND_LENGTHS = {KIND_DATE: DATE_LENGTH, KIND_TIMESTAMP: TIMESTAMP_LENGTH}
# The DB2 form carries hundredths of a second (DB2-MIL) followed by the literal '0000'.
MICROSECONDS_PER_HUNDREDTH = 10_000
MIDNIGHT = time(0, 0)

# NUL cannot be stored in PostgreSQL text, and CR or LF would split a line-oriented GDG file.
FORBIDDEN_TEXT_CHARACTERS = frozenset("\x00\r\n")
# [0-9] rather than \d, which also matches non-ASCII digits.
DIGITS_PATTERN = re.compile(r"[0-9]+")
DATE_PATTERN = re.compile(r"(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})")
TIMESTAMP_PATTERNS = (
    (
        TIMESTAMP_FORMAT_SPACE,
        re.compile(
            DATE_PATTERN.pattern
            + r" (?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
            + r"\.(?P<microsecond>[0-9]{6})"
        ),
    ),
    (
        TIMESTAMP_FORMAT_DB2,
        re.compile(
            DATE_PATTERN.pattern
            + r"-(?P<hour>[0-9]{2})\.(?P<minute>[0-9]{2})\.(?P<second>[0-9]{2})"
            + r"\.(?P<hundredths>[0-9]{2})0000"
        ),
    ),
    (TIMESTAMP_FORMAT_DATE_ONLY, re.compile(DATE_PATTERN.pattern + r" {16}")),
)

# FD-REPTFILE-REC PIC X(133) in CBTRN03C.
REPORT_LINE_WIDTH = 133


class RecordFormatError(ValueError):
    """A record of the wrong length, bad field content, a missing value or a bad layout table.

    fieldName and fieldOffset locate the field a decoded record was refused for: its column name
    and zero-based offset, taken from the layout table and never from the record. Both are None
    for every other refusal.
    """

    def __init__(
        self, message: str, fieldName: str | None = None, fieldOffset: int | None = None
    ) -> None:
        super().__init__(message)
        self.fieldName = fieldName
        self.fieldOffset = fieldOffset


class TimestampFormatError(ValueError):
    """Text that is none of the four 26-character forms, or a value the form cannot hold."""


class _FieldValueError(ValueError):
    """One field's value is unusable; the record or line codec adds the layout context."""


@dataclass(frozen=True)
class FieldSpec:
    """One copybook field: its column name, zero-based offset, byte length and kind.

    intDigits and decDigits describe an S9(intDigits)V9(decDigits) picture and are used only by
    KIND_SIGNED_ZONED. copybookName is the COBOL data name, kept for traceability.
    """

    name: str
    offset: int
    length: int
    kind: str
    intDigits: int = 0
    decDigits: int = 0
    copybookName: str = ""


@dataclass(frozen=True)
class RecordLayout:
    """A copybook record: its name, LRECL and fields in ascending offset order.

    Bytes no field covers are FILLER. trailingFillerLength declares the FILLER after the last
    field, which the image reader may accept as missing from a short ASCII line. A table that
    contradicts itself raises RecordFormatError when the layout is built, so at import time.
    """

    name: str
    lrecl: int
    fields: tuple[FieldSpec, ...]
    trailingFillerLength: int = 0

    def __post_init__(self) -> None:
        _ValidateRecordLayout(self)

    def FillerSpans(self) -> tuple[tuple[int, int], ...]:
        """Return the (offset, length) of every byte span no field covers, in offset order."""
        spans = []
        position = 0
        for field in self.fields:
            if field.offset > position:
                spans.append((position, field.offset - position))
            position = field.offset + field.length
        if self.lrecl > position:
            spans.append((position, self.lrecl - position))
        return tuple(spans)


def _ValidateRecordLayout(layout: RecordLayout) -> None:
    """Refuse an empty or out-of-order field table, and a wrong trailing FILLER length."""
    if layout.lrecl < 1 or not layout.fields:
        raise RecordFormatError(f"{layout.name}: a layout needs a positive LRECL and fields")
    previousEnd = 0
    for field in layout.fields:
        _ValidateFieldSpec(layout, field, previousEnd)
        previousEnd = field.offset + field.length
    if layout.trailingFillerLength != layout.lrecl - previousEnd:
        raise RecordFormatError(
            f"{layout.name}: trailingFillerLength {layout.trailingFillerLength} differs from"
            f" the {layout.lrecl - previousEnd} bytes after the last field"
        )


def _ValidateFieldSpec(layout: RecordLayout, field: FieldSpec, previousEnd: int) -> None:
    """Refuse a field that overlaps, overruns LRECL or contradicts its kind's length rule."""
    fixedLength = FIXED_KIND_LENGTHS.get(field.kind, field.length)
    zonedDigits = field.intDigits + field.decDigits
    problems = (
        (field.kind not in VALID_KINDS, f"unknown kind {field.kind!r}"),
        (field.length < 1, "the length must be at least 1"),
        (field.offset < previousEnd, f"it starts before the previous field ends at {previousEnd}"),
        (field.offset + field.length > layout.lrecl, f"it ends beyond LRECL {layout.lrecl}"),
        (field.length != fixedLength, f"kind {field.kind} must be {fixedLength} bytes long"),
        (
            field.kind == KIND_SIGNED_ZONED
            and (zonedDigits != field.length or min(field.intDigits, field.decDigits) < 0),
            f"S9({field.intDigits})V9({field.decDigits}) does not fill {field.length} bytes",
        ),
    )
    for failed, reason in problems:
        if failed:
            raise RecordFormatError(_DescribeField(layout.name, field, reason))


def _DescribeField(layoutName: str, field: FieldSpec, reason: str) -> str:
    """Name the layout, the field and its offset in front of reason."""
    return (
        f"{layoutName} field {field.name} ({field.copybookName}) at offset {field.offset}: {reason}"
    )


# Layout tables. Each FieldSpec row reads: column name, zero-based offset, length, kind,
# intDigits, decDigits (both used only by S9V fields) and the copybook data name.


def _TransactionRecordFields(prefix: str) -> tuple[FieldSpec, ...]:
    """Return the CVTRA05Y/CVTRA06Y fields, which share offsets and column names.

    The two copybooks differ only in their data-name prefix, TRAN- or DALYTRAN-.
    """
    return (
        FieldSpec("tran_id", 0, 16, KIND_ALPHANUMERIC, 0, 0, f"{prefix}ID"),
        FieldSpec("tran_type_cd", 16, 2, KIND_ALPHANUMERIC, 0, 0, f"{prefix}TYPE-CD"),
        FieldSpec("tran_cat_cd", 18, 4, KIND_UNSIGNED_NUMBER, 0, 0, f"{prefix}CAT-CD"),
        FieldSpec("tran_source", 22, 10, KIND_ALPHANUMERIC, 0, 0, f"{prefix}SOURCE"),
        FieldSpec("tran_desc", 32, 100, KIND_ALPHANUMERIC, 0, 0, f"{prefix}DESC"),
        FieldSpec("tran_amt", 132, 11, KIND_SIGNED_ZONED, 9, 2, f"{prefix}AMT"),
        FieldSpec("merchant_id", 143, 9, KIND_DIGITS, 0, 0, f"{prefix}MERCHANT-ID"),
        FieldSpec("merchant_name", 152, 50, KIND_ALPHANUMERIC, 0, 0, f"{prefix}MERCHANT-NAME"),
        FieldSpec("merchant_city", 202, 50, KIND_ALPHANUMERIC, 0, 0, f"{prefix}MERCHANT-CITY"),
        FieldSpec("merchant_zip", 252, 10, KIND_ALPHANUMERIC, 0, 0, f"{prefix}MERCHANT-ZIP"),
        FieldSpec("card_num", 262, 16, KIND_ALPHANUMERIC, 0, 0, f"{prefix}CARD-NUM"),
        FieldSpec("orig_ts", 278, 26, KIND_TIMESTAMP, 0, 0, f"{prefix}ORIG-TS"),
        FieldSpec("proc_ts", 304, 26, KIND_TIMESTAMP, 0, 0, f"{prefix}PROC-TS"),
    )


# CVACT01Y ACCOUNT-RECORD, LRECL 300 (ACCTDATA); FILLER X(178) at offset 122.
ACCOUNT_LAYOUT = RecordLayout(
    name="CVACT01Y",
    lrecl=300,
    fields=(
        FieldSpec("acct_id", 0, 11, KIND_DIGITS, 0, 0, "ACCT-ID"),
        FieldSpec("active_status", 11, 1, KIND_ALPHANUMERIC, 0, 0, "ACCT-ACTIVE-STATUS"),
        FieldSpec("curr_balance", 12, 12, KIND_SIGNED_ZONED, 10, 2, "ACCT-CURR-BAL"),
        FieldSpec("credit_limit", 24, 12, KIND_SIGNED_ZONED, 10, 2, "ACCT-CREDIT-LIMIT"),
        FieldSpec("cash_credit_limit", 36, 12, KIND_SIGNED_ZONED, 10, 2, "ACCT-CASH-CREDIT-LIMIT"),
        FieldSpec("open_date", 48, 10, KIND_DATE, 0, 0, "ACCT-OPEN-DATE"),
        # The copybook spells this data name EXPIRAION.
        FieldSpec("expiration_date", 58, 10, KIND_DATE, 0, 0, "ACCT-EXPIRAION-DATE"),
        FieldSpec("reissue_date", 68, 10, KIND_DATE, 0, 0, "ACCT-REISSUE-DATE"),
        FieldSpec("curr_cycle_credit", 78, 12, KIND_SIGNED_ZONED, 10, 2, "ACCT-CURR-CYC-CREDIT"),
        FieldSpec("curr_cycle_debit", 90, 12, KIND_SIGNED_ZONED, 10, 2, "ACCT-CURR-CYC-DEBIT"),
        FieldSpec("addr_zip", 102, 10, KIND_ALPHANUMERIC, 0, 0, "ACCT-ADDR-ZIP"),
        FieldSpec("group_id", 112, 10, KIND_ALPHANUMERIC, 0, 0, "ACCT-GROUP-ID"),
    ),
    trailingFillerLength=178,
)

# CVCUS01Y CUSTOMER-RECORD, LRECL 500 (CUSTDATA); CUSTREC is the same record, naming the birth
# date CUST-DOB-YYYYMMDD. FILLER X(168) at offset 332.
CUSTOMER_LAYOUT = RecordLayout(
    name="CVCUS01Y",
    lrecl=500,
    fields=(
        FieldSpec("cust_id", 0, 9, KIND_DIGITS, 0, 0, "CUST-ID"),
        FieldSpec("first_name", 9, 25, KIND_ALPHANUMERIC, 0, 0, "CUST-FIRST-NAME"),
        FieldSpec("middle_name", 34, 25, KIND_ALPHANUMERIC, 0, 0, "CUST-MIDDLE-NAME"),
        FieldSpec("last_name", 59, 25, KIND_ALPHANUMERIC, 0, 0, "CUST-LAST-NAME"),
        FieldSpec("addr_line_1", 84, 50, KIND_ALPHANUMERIC, 0, 0, "CUST-ADDR-LINE-1"),
        FieldSpec("addr_line_2", 134, 50, KIND_ALPHANUMERIC, 0, 0, "CUST-ADDR-LINE-2"),
        FieldSpec("addr_line_3", 184, 50, KIND_ALPHANUMERIC, 0, 0, "CUST-ADDR-LINE-3"),
        FieldSpec("addr_state_cd", 234, 2, KIND_ALPHANUMERIC, 0, 0, "CUST-ADDR-STATE-CD"),
        FieldSpec("addr_country_cd", 236, 3, KIND_ALPHANUMERIC, 0, 0, "CUST-ADDR-COUNTRY-CD"),
        FieldSpec("addr_zip", 239, 10, KIND_ALPHANUMERIC, 0, 0, "CUST-ADDR-ZIP"),
        FieldSpec("phone_num_1", 249, 15, KIND_ALPHANUMERIC, 0, 0, "CUST-PHONE-NUM-1"),
        FieldSpec("phone_num_2", 264, 15, KIND_ALPHANUMERIC, 0, 0, "CUST-PHONE-NUM-2"),
        FieldSpec("ssn", 279, 9, KIND_DIGITS, 0, 0, "CUST-SSN"),
        FieldSpec("govt_issued_id", 288, 20, KIND_ALPHANUMERIC, 0, 0, "CUST-GOVT-ISSUED-ID"),
        FieldSpec("date_of_birth", 308, 10, KIND_DATE, 0, 0, "CUST-DOB-YYYY-MM-DD"),
        FieldSpec("eft_account_id", 318, 10, KIND_ALPHANUMERIC, 0, 0, "CUST-EFT-ACCOUNT-ID"),
        FieldSpec(
            "primary_holder_ind", 328, 1, KIND_ALPHANUMERIC, 0, 0, "CUST-PRI-CARD-HOLDER-IND"
        ),
        FieldSpec(
            "fico_credit_score", 329, 3, KIND_UNSIGNED_NUMBER, 0, 0, "CUST-FICO-CREDIT-SCORE"
        ),
    ),
    trailingFillerLength=168,
)

# CVACT02Y CARD-RECORD, LRECL 150 (CARDDATA); FILLER X(59) at offset 91.
CARD_LAYOUT = RecordLayout(
    name="CVACT02Y",
    lrecl=150,
    fields=(
        FieldSpec("card_num", 0, 16, KIND_ALPHANUMERIC, 0, 0, "CARD-NUM"),
        FieldSpec("acct_id", 16, 11, KIND_DIGITS, 0, 0, "CARD-ACCT-ID"),
        FieldSpec("cvv_cd", 27, 3, KIND_DIGITS, 0, 0, "CARD-CVV-CD"),
        FieldSpec("embossed_name", 30, 50, KIND_ALPHANUMERIC, 0, 0, "CARD-EMBOSSED-NAME"),
        # The copybook spells this data name EXPIRAION.
        FieldSpec("expiration_date", 80, 10, KIND_DATE, 0, 0, "CARD-EXPIRAION-DATE"),
        FieldSpec("active_status", 90, 1, KIND_ALPHANUMERIC, 0, 0, "CARD-ACTIVE-STATUS"),
    ),
    trailingFillerLength=59,
)

# CVACT03Y CARD-XREF-RECORD, LRECL 50 (CARDXREF); FILLER X(14) at offset 36, which
# data/ASCII/cardxref.txt omits from its 36-character lines.
CARD_XREF_LAYOUT = RecordLayout(
    name="CVACT03Y",
    lrecl=50,
    fields=(
        FieldSpec("card_num", 0, 16, KIND_ALPHANUMERIC, 0, 0, "XREF-CARD-NUM"),
        FieldSpec("cust_id", 16, 9, KIND_DIGITS, 0, 0, "XREF-CUST-ID"),
        FieldSpec("acct_id", 25, 11, KIND_DIGITS, 0, 0, "XREF-ACCT-ID"),
    ),
    trailingFillerLength=14,
)

# CVTRA01Y TRAN-CAT-BAL-RECORD, LRECL 50 (TCATBALF); FILLER X(22) at offset 28.
TRAN_CAT_BALANCE_LAYOUT = RecordLayout(
    name="CVTRA01Y",
    lrecl=50,
    fields=(
        FieldSpec("acct_id", 0, 11, KIND_DIGITS, 0, 0, "TRANCAT-ACCT-ID"),
        FieldSpec("tran_type_cd", 11, 2, KIND_ALPHANUMERIC, 0, 0, "TRANCAT-TYPE-CD"),
        FieldSpec("tran_cat_cd", 13, 4, KIND_UNSIGNED_NUMBER, 0, 0, "TRANCAT-CD"),
        FieldSpec("balance", 17, 11, KIND_SIGNED_ZONED, 9, 2, "TRAN-CAT-BAL"),
    ),
    trailingFillerLength=22,
)

# CVTRA02Y DIS-GROUP-RECORD, LRECL 50 (DISCGRP); FILLER X(28) at offset 22.
DISCLOSURE_GROUP_LAYOUT = RecordLayout(
    name="CVTRA02Y",
    lrecl=50,
    fields=(
        FieldSpec("group_id", 0, 10, KIND_ALPHANUMERIC, 0, 0, "DIS-ACCT-GROUP-ID"),
        FieldSpec("tran_type_cd", 10, 2, KIND_ALPHANUMERIC, 0, 0, "DIS-TRAN-TYPE-CD"),
        FieldSpec("tran_cat_cd", 12, 4, KIND_UNSIGNED_NUMBER, 0, 0, "DIS-TRAN-CAT-CD"),
        FieldSpec("interest_rate", 16, 6, KIND_SIGNED_ZONED, 4, 2, "DIS-INT-RATE"),
    ),
    trailingFillerLength=28,
)

# CVTRA03Y TRAN-TYPE-RECORD, LRECL 60 (TRANTYPE); FILLER X(08) at offset 52.
TRANSACTION_TYPE_LAYOUT = RecordLayout(
    name="CVTRA03Y",
    lrecl=60,
    fields=(
        FieldSpec("tran_type", 0, 2, KIND_ALPHANUMERIC, 0, 0, "TRAN-TYPE"),
        FieldSpec("description", 2, 50, KIND_ALPHANUMERIC, 0, 0, "TRAN-TYPE-DESC"),
    ),
    trailingFillerLength=8,
)

# CVTRA04Y TRAN-CAT-RECORD, LRECL 60 (TRANCATG); FILLER X(04) at offset 56.
TRANSACTION_CATEGORY_LAYOUT = RecordLayout(
    name="CVTRA04Y",
    lrecl=60,
    fields=(
        FieldSpec("tran_type_cd", 0, 2, KIND_ALPHANUMERIC, 0, 0, "TRAN-TYPE-CD"),
        FieldSpec("tran_cat_cd", 2, 4, KIND_UNSIGNED_NUMBER, 0, 0, "TRAN-CAT-CD"),
        FieldSpec("description", 6, 50, KIND_ALPHANUMERIC, 0, 0, "TRAN-CAT-TYPE-DESC"),
    ),
    trailingFillerLength=4,
)

# CVTRA05Y TRAN-RECORD, LRECL 350 (TRANSACT, TRANSACT.BKUP, SYSTRAN, TRANSACT.COMBINED);
# FILLER X(20) at offset 330.
TRANSACTION_LAYOUT = RecordLayout(
    name="CVTRA05Y",
    lrecl=350,
    fields=_TransactionRecordFields("TRAN-"),
    trailingFillerLength=20,
)

# CVTRA06Y DALYTRAN-RECORD, LRECL 350 (DALYTRAN); FILLER X(20) at offset 330. The staging table
# mirrors transactions, so the column names are TRANSACTION_LAYOUT's.
DAILY_TRANSACTION_LAYOUT = RecordLayout(
    name="CVTRA06Y",
    lrecl=350,
    fields=_TransactionRecordFields("DALYTRAN-"),
    trailingFillerLength=20,
)

# CSUSR01Y SEC-USER-DATA, LRECL 80 (USRSEC); SEC-USR-FILLER X(23) at offset 57. The password is
# the image's plaintext: loaders hash it into password_hash and never store it.
USER_SECURITY_LAYOUT = RecordLayout(
    name="CSUSR01Y",
    lrecl=80,
    fields=(
        FieldSpec("user_id", 0, 8, KIND_ALPHANUMERIC, 0, 0, "SEC-USR-ID"),
        FieldSpec("first_name", 8, 20, KIND_ALPHANUMERIC, 0, 0, "SEC-USR-FNAME"),
        FieldSpec("last_name", 28, 20, KIND_ALPHANUMERIC, 0, 0, "SEC-USR-LNAME"),
        FieldSpec("password", 48, 8, KIND_ALPHANUMERIC, 0, 0, "SEC-USR-PWD"),
        FieldSpec("user_type", 56, 1, KIND_ALPHANUMERIC, 0, 0, "SEC-USR-TYPE"),
    ),
    trailingFillerLength=23,
)

# COSTM01 TRNX-RECORD, LRECL 350 (the CREASTMT TRXFL work file, re-keyed card number first);
# FILLER X(20) at offset 330.
STATEMENT_WORK_LAYOUT = RecordLayout(
    name="COSTM01",
    lrecl=350,
    fields=(
        FieldSpec("card_num", 0, 16, KIND_ALPHANUMERIC, 0, 0, "TRNX-CARD-NUM"),
        FieldSpec("tran_id", 16, 16, KIND_ALPHANUMERIC, 0, 0, "TRNX-ID"),
        FieldSpec("tran_type_cd", 32, 2, KIND_ALPHANUMERIC, 0, 0, "TRNX-TYPE-CD"),
        FieldSpec("tran_cat_cd", 34, 4, KIND_UNSIGNED_NUMBER, 0, 0, "TRNX-CAT-CD"),
        FieldSpec("tran_source", 38, 10, KIND_ALPHANUMERIC, 0, 0, "TRNX-SOURCE"),
        FieldSpec("tran_desc", 48, 100, KIND_ALPHANUMERIC, 0, 0, "TRNX-DESC"),
        FieldSpec("tran_amt", 148, 11, KIND_SIGNED_ZONED, 9, 2, "TRNX-AMT"),
        FieldSpec("merchant_id", 159, 9, KIND_DIGITS, 0, 0, "TRNX-MERCHANT-ID"),
        FieldSpec("merchant_name", 168, 50, KIND_ALPHANUMERIC, 0, 0, "TRNX-MERCHANT-NAME"),
        FieldSpec("merchant_city", 218, 50, KIND_ALPHANUMERIC, 0, 0, "TRNX-MERCHANT-CITY"),
        FieldSpec("merchant_zip", 268, 10, KIND_ALPHANUMERIC, 0, 0, "TRNX-MERCHANT-ZIP"),
        FieldSpec("orig_ts", 278, 26, KIND_TIMESTAMP, 0, 0, "TRNX-ORIG-TS"),
        FieldSpec("proc_ts", 304, 26, KIND_TIMESTAMP, 0, 0, "TRNX-PROC-TS"),
    ),
    trailingFillerLength=20,
)

# CBTRN02C FD-REJS-RECORD, LRECL 430 (DALYREJS): the 350-byte daily record, whose FILLER at
# offset 330 for 20 bytes stays uncovered, then the 80-byte WS-VALIDATION-TRAILER.
DALYREJS_LAYOUT = RecordLayout(
    name="DALYREJS",
    lrecl=430,
    fields=DAILY_TRANSACTION_LAYOUT.fields
    + (
        FieldSpec("reject_reason", 350, 4, KIND_UNSIGNED_NUMBER, 0, 0, "WS-VALIDATION-FAIL-REASON"),
        FieldSpec(
            "reject_reason_desc", 354, 76, KIND_ALPHANUMERIC, 0, 0, "WS-VALIDATION-FAIL-REASON-DESC"
        ),
    ),
    trailingFillerLength=0,
)

ALL_RECORD_LAYOUTS = (
    ACCOUNT_LAYOUT,
    CUSTOMER_LAYOUT,
    CARD_LAYOUT,
    CARD_XREF_LAYOUT,
    TRAN_CAT_BALANCE_LAYOUT,
    DISCLOSURE_GROUP_LAYOUT,
    TRANSACTION_TYPE_LAYOUT,
    TRANSACTION_CATEGORY_LAYOUT,
    TRANSACTION_LAYOUT,
    DAILY_TRANSACTION_LAYOUT,
    USER_SECURITY_LAYOUT,
    STATEMENT_WORK_LAYOUT,
    DALYREJS_LAYOUT,
)


def DecodeRecord(text: str, layout: RecordLayout) -> dict[str, object]:
    """Decode one fixed-width record into a dict keyed by column name; FILLER is never read.

    X fields are right-trimmed of spaces, 9 fields stay digit text, 9N fields become int, S9V
    fields become Decimal, dates become date or None, and each TS26 field yields its value and
    its "<name>_format" code. text must be exactly LRECL characters: padding a short ASCII line
    is the image reader's job. Raises RecordFormatError naming the layout, field and offset.
    """
    if not isinstance(text, str):
        raise TypeError(f"a record must be str, not {type(text).__name__}")
    if len(text) != layout.lrecl:
        raise RecordFormatError(
            f"{layout.name}: the record is {len(text)} characters long; its LRECL is {layout.lrecl}"
        )
    record: dict[str, object] = {}
    for field in layout.fields:
        record.update(_DecodeField(text[field.offset : field.offset + field.length], field, layout))
    return record


def EncodeRecord(values: dict[str, object], layout: RecordLayout) -> str:
    """Encode the column values of one record as LRECL characters, FILLER written as spaces.

    Every field's column must be present (a TS26 field also needs "<name>_format"). Numbers are
    stored as a COBOL MOVE does, truncating digits the picture cannot hold. The record carries
    no terminator. Raises RecordFormatError naming the layout, field and offset, or the layout and
    LRECL when the encoded record has another length.
    """
    characters = [" "] * layout.lrecl
    for field in layout.fields:
        fieldEnd = field.offset + field.length
        characters[field.offset : fieldEnd] = _EncodeField(values, field, layout)
    record = "".join(characters)
    if len(record) != layout.lrecl:
        raise RecordFormatError(
            f"{layout.name}: the encoded record is {len(record)} characters long; its LRECL is"
            f" {layout.lrecl}"
        )
    return record


def _DecodeField(fieldText: str, field: FieldSpec, layout: RecordLayout) -> dict[str, object]:
    """Decode one field's text with its kind's decoder, adding the layout context to errors.

    The RecordFormatError also carries the field's column name and offset as fieldName and
    fieldOffset.
    """
    try:
        return _DECODERS[field.kind](fieldText, field)
    except (_FieldValueError, ZonedDecimalError, TimestampFormatError) as error:
        message = _DescribeField(layout.name, field, str(error))
        raise RecordFormatError(message, field.name, field.offset) from error


def _EncodeField(values: dict[str, object], field: FieldSpec, layout: RecordLayout) -> str:
    """Encode one field with its kind's encoder, adding the layout context to errors."""
    try:
        return _ENCODERS[field.kind](values, field)
    except (_FieldValueError, ZonedDecimalError, TimestampFormatError) as error:
        raise RecordFormatError(_DescribeField(layout.name, field, str(error))) from error
    except InvalidOperation as error:
        reason = "the value needs more than the 31 digits of a COBOL intermediate"
        raise RecordFormatError(_DescribeField(layout.name, field, reason)) from error


def _DecodeAlphanumeric(fieldText: str, field: FieldSpec) -> dict[str, object]:
    """PIC X(n): the text right-trimmed of spaces only."""
    _RequireStorableText(fieldText)
    return {field.name: fieldText.rstrip(" ")}


def _DecodeDigits(fieldText: str, field: FieldSpec) -> dict[str, object]:
    """PIC 9(n) identifier: the digit text, leading zeros kept."""
    _RequireDigits(fieldText)
    return {field.name: fieldText}


def _DecodeUnsignedNumber(fieldText: str, field: FieldSpec) -> dict[str, object]:
    """PIC 9(n) code or counter: the digits as an int."""
    _RequireDigits(fieldText)
    return {field.name: int(fieldText)}


def _DecodeSignedZoned(fieldText: str, field: FieldSpec) -> dict[str, object]:
    """PIC S9(i)V9(d) zoned DISPLAY with a trailing sign overpunch: a Decimal of scale d."""
    return {field.name: DecodeZoned(fieldText, field.decDigits)}


def _DecodeDate(fieldText: str, field: FieldSpec) -> dict[str, object]:
    """PIC X(10) date text: None when blank, otherwise a YYYY-MM-DD calendar date."""
    if fieldText == BLANK_DATE:
        return {field.name: None}
    match = DATE_PATTERN.fullmatch(fieldText)
    if match is None:
        raise _FieldValueError("the text is neither blank nor a YYYY-MM-DD date")
    try:
        parsedDate = date(int(match["year"]), int(match["month"]), int(match["day"]))
    except ValueError as error:
        raise _FieldValueError("the YYYY-MM-DD text is not a calendar date") from error
    return {field.name: parsedDate}


def _DecodeTimestampField(fieldText: str, field: FieldSpec) -> dict[str, object]:
    """PIC X(26) timestamp: its value under the field name and its form code beside it."""
    value, timestampFormat = DecodeTimestamp(fieldText)
    return {field.name: value, field.name + TIMESTAMP_FORMAT_COLUMN_SUFFIX: timestampFormat}


def _EncodeAlphanumeric(values: dict[str, object], field: FieldSpec) -> str:
    """PIC X(n): the text right-padded with spaces; longer text is refused, not cut."""
    value = _RequireValue(values, field.name)
    if not isinstance(value, str):
        raise _FieldValueError(f"the value must be str, not {type(value).__name__}")
    _RequireStorableText(value)
    if len(value) > field.length:
        raise _FieldValueError(f"{len(value)} characters exceed the length {field.length}")
    return value.ljust(field.length)


def _EncodeDigits(values: dict[str, object], field: FieldSpec) -> str:
    """PIC 9(n) identifier: digit text zero-filled on the left, or an int stored unsigned."""
    value = _RequireValue(values, field.name)
    if not isinstance(value, str):
        return _RenderUnsigned(value, field.length)
    _RequireDigits(value)
    if len(value) > field.length:
        raise _FieldValueError(f"{len(value)} digits exceed the length {field.length}")
    return value.zfill(field.length)


def _EncodeUnsignedNumber(values: dict[str, object], field: FieldSpec) -> str:
    """PIC 9(n) code or counter: an int stored unsigned, modulo 10**n and zero-padded."""
    return _RenderUnsigned(_RequireValue(values, field.name), field.length)


def _EncodeSignedZoned(values: dict[str, object], field: FieldSpec) -> str:
    """PIC S9(i)V9(d): zoned DISPLAY digits with a trailing sign overpunch."""
    value = _RequireNumber(_RequireValue(values, field.name))
    return EncodeZoned(value, field.intDigits, field.decDigits)


def _EncodeDate(values: dict[str, object], field: FieldSpec) -> str:
    """PIC X(10) date text: ten spaces for None, otherwise YYYY-MM-DD."""
    value = _RequireValue(values, field.name)
    if value is None:
        return BLANK_DATE
    if isinstance(value, datetime) or not isinstance(value, date):
        raise _FieldValueError(f"the value must be a date or None, not {type(value).__name__}")
    return value.isoformat()


def _EncodeTimestampField(values: dict[str, object], field: FieldSpec) -> str:
    """PIC X(26) timestamp: the text its "<name>_format" code names."""
    value = _RequireValue(values, field.name)
    timestampFormat = _RequireValue(values, field.name + TIMESTAMP_FORMAT_COLUMN_SUFFIX)
    return EncodeTimestamp(value, timestampFormat)


def _RenderUnsigned(value: object, length: int) -> str:
    """Store an int into PIC 9(length), keeping the absolute value modulo 10**length."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise _FieldValueError(f"the value must be int, not {type(value).__name__}")
    stored = StoreInto(value, length, 0, signed=False)
    return str(int(stored)).zfill(length)


def _RequireValue(values: dict[str, object], key: str) -> object:
    """Return values[key], refusing a missing key."""
    if key not in values:
        raise _FieldValueError(f"no value was supplied for {key!r}")
    return values[key]


def _RequireNumber(value: object) -> Decimal | int:
    """Return value when it is an exact Decimal or int; a float or a bool is refused."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise _FieldValueError(f"the value must be Decimal or int, not {type(value).__name__}")
    return value


def _RequireStorableText(text: str) -> None:
    """Refuse text holding NUL, CR or LF; the content itself is never echoed."""
    if any(character in FORBIDDEN_TEXT_CHARACTERS for character in text):
        raise _FieldValueError("the text contains a NUL, CR or LF character")


def _RequireDigits(text: str) -> None:
    """Refuse text that is not one or more ASCII digits; the content itself is never echoed."""
    if DIGITS_PATTERN.fullmatch(text) is None:
        raise _FieldValueError("the text must consist of the digits 0-9 only")


_DECODERS = {
    KIND_ALPHANUMERIC: _DecodeAlphanumeric,
    KIND_DIGITS: _DecodeDigits,
    KIND_UNSIGNED_NUMBER: _DecodeUnsignedNumber,
    KIND_SIGNED_ZONED: _DecodeSignedZoned,
    KIND_DATE: _DecodeDate,
    KIND_TIMESTAMP: _DecodeTimestampField,
}

_ENCODERS = {
    KIND_ALPHANUMERIC: _EncodeAlphanumeric,
    KIND_DIGITS: _EncodeDigits,
    KIND_UNSIGNED_NUMBER: _EncodeUnsignedNumber,
    KIND_SIGNED_ZONED: _EncodeSignedZoned,
    KIND_DATE: _EncodeDate,
    KIND_TIMESTAMP: _EncodeTimestampField,
}


def DecodeTimestamp(text: str) -> tuple[datetime | None, str]:
    """Return (value, formatCode) for a 26-character timestamp text in one of its four forms.

    "S" keeps the six fraction digits as microseconds, "D" turns its hundredths into
    microseconds, "T" is midnight of its date, and 26 spaces give (None, " "). Any other text,
    another length, or an impossible date or clock time raises TimestampFormatError.
    """
    if not isinstance(text, str) or len(text) != TIMESTAMP_LENGTH:
        raise TimestampFormatError(f"a timestamp must be text of {TIMESTAMP_LENGTH} characters")
    if text == BLANK_TIMESTAMP:
        return None, TIMESTAMP_FORMAT_BLANK
    for timestampFormat, pattern in TIMESTAMP_PATTERNS:
        match = pattern.fullmatch(text)
        if match is not None:
            return _BuildTimestamp(match, text), timestampFormat
    raise TimestampFormatError(f"{text!r} is none of the four 26-character timestamp forms")


def EncodeTimestamp(value: datetime | None, timestampFormat: str) -> str:
    """Rebuild the 26-character text of value in the form timestampFormat names.

    " " needs None. "S", "D" and "T" need a datetime without a time zone; "D" also needs whole
    hundredths of a second, and "T" needs midnight. A violation raises TimestampFormatError.
    """
    if not isinstance(timestampFormat, str) or timestampFormat not in _TIMESTAMP_ENCODERS:
        raise TimestampFormatError(f"unknown timestamp format code {timestampFormat!r}")
    if timestampFormat == TIMESTAMP_FORMAT_BLANK:
        return _EncodeBlankTimestamp(value)
    stamp = _RequireNaiveDatetime(value, timestampFormat)
    return _TIMESTAMP_ENCODERS[timestampFormat](stamp)


def _BuildTimestamp(match: re.Match[str], text: str) -> datetime:
    """Build the datetime a matched timestamp pattern describes, refusing impossible values."""
    parts = {name: int(digits) for name, digits in match.groupdict().items()}
    microsecond = parts.get("microsecond", 0)
    microsecond += parts.get("hundredths", 0) * MICROSECONDS_PER_HUNDREDTH
    try:
        return datetime(
            parts["year"],
            parts["month"],
            parts["day"],
            parts.get("hour", 0),
            parts.get("minute", 0),
            parts.get("second", 0),
            microsecond,
        )
    except ValueError as error:
        reason = f"{text!r} is not a valid calendar date and clock time"
        raise TimestampFormatError(reason) from error


def _RequireNaiveDatetime(value: object, timestampFormat: str) -> datetime:
    """Return value when it is a datetime without a time zone, as TIMESTAMP(6) stores it."""
    if not isinstance(value, datetime):
        typeName = type(value).__name__
        raise TimestampFormatError(f"form {timestampFormat!r} needs a datetime, not {typeName}")
    if value.tzinfo is not None:
        raise TimestampFormatError("timestamps carry no time zone; the datetime is zone-aware")
    return value


def _EncodeBlankTimestamp(value: object) -> str:
    """The blank form: 26 spaces, which only a NULL value has."""
    if value is not None:
        raise TimestampFormatError("the blank form ' ' holds no value; the value must be None")
    return BLANK_TIMESTAMP


def _EncodeSpaceTimestamp(stamp: datetime) -> str:
    """The "S" form: YYYY-MM-DD HH:MM:SS.ffffff."""
    clockText = f"{stamp.hour:02d}:{stamp.minute:02d}:{stamp.second:02d}"
    return f"{_DateText(stamp)} {clockText}.{stamp.microsecond:06d}"


def _EncodeDb2Timestamp(stamp: datetime) -> str:
    """The "D" form: YYYY-MM-DD-HH.MM.SS.cc0000, which holds only whole hundredths."""
    hundredths, remainder = divmod(stamp.microsecond, MICROSECONDS_PER_HUNDREDTH)
    if remainder != 0:
        raise TimestampFormatError("the form 'D' holds whole hundredths of a second only")
    clockText = f"{stamp.hour:02d}.{stamp.minute:02d}.{stamp.second:02d}"
    return f"{_DateText(stamp)}-{clockText}.{hundredths:02d}0000"


def _EncodeDateOnlyTimestamp(stamp: datetime) -> str:
    """The "T" form: YYYY-MM-DD followed by 16 spaces, which holds midnight only."""
    if stamp.time() != MIDNIGHT:
        raise TimestampFormatError("the form 'T' holds a date only; the time must be midnight")
    return _DateText(stamp).ljust(TIMESTAMP_LENGTH)


def _DateText(stamp: datetime) -> str:
    """YYYY-MM-DD, built field by field so a year below 1000 keeps its leading zeros."""
    return f"{stamp.year:04d}-{stamp.month:02d}-{stamp.day:02d}"


_TIMESTAMP_ENCODERS = {
    TIMESTAMP_FORMAT_SPACE: _EncodeSpaceTimestamp,
    TIMESTAMP_FORMAT_DB2: _EncodeDb2Timestamp,
    TIMESTAMP_FORMAT_DATE_ONLY: _EncodeDateOnlyTimestamp,
    TIMESTAMP_FORMAT_BLANK: _EncodeBlankTimestamp,
}


@dataclass(frozen=True)
class LineSegment:
    """One elementary item of a print line: a literal, or a named field with an optional picture.

    Exactly one of literal and fieldName is set. A picture, which only a field may have, is a
    numeric-edited picture whose rendered width must equal length.
    """

    length: int
    literal: str | None = None
    fieldName: str | None = None
    picture: str | None = None

    def __post_init__(self) -> None:
        _ValidateLineSegment(self)


@dataclass(frozen=True)
class LineLayout:
    """A print line: its record name, its segments in order, and the width it is padded to."""

    name: str
    segments: tuple[LineSegment, ...]
    width: int = REPORT_LINE_WIDTH

    def __post_init__(self) -> None:
        usedWidth = sum(segment.length for segment in self.segments)
        if usedWidth > self.width:
            raise RecordFormatError(
                f"{self.name}: the segments take {usedWidth} columns; the line has {self.width}"
            )


def RenderLine(values: dict[str, object], layout: LineLayout) -> str:
    """Render a print line as MOVEs into its record do, right-padded with spaces to its width.

    A literal is padded with spaces to its length; LineSegment refuses a longer one when it is
    built. A field with a picture is the numeric-edited text of a Decimal or int value. A field
    without one is a str, left-justified and padded or cut as an alphanumeric MOVE leaves it.
    Raises RecordFormatError for a missing or unusable value, including a non-finite Decimal and
    one needing more than the 31 digits of a COBOL intermediate.
    """
    renderedSegments = [_RenderSegment(values, segment, layout) for segment in layout.segments]
    return "".join(renderedSegments).ljust(layout.width)


def _RenderSegment(values: dict[str, object], segment: LineSegment, layout: LineLayout) -> str:
    """Render one segment, adding the line and field names to a value error."""
    if segment.literal is not None:
        return _FitText(segment.literal, segment.length)
    try:
        return _RenderFieldSegment(values, segment)
    except (_FieldValueError, ZonedDecimalError) as error:
        raise RecordFormatError(f"{layout.name} field {segment.fieldName}: {error}") from error
    except InvalidOperation as error:
        reason = "the value needs more than the 31 digits of a COBOL intermediate"
        raise RecordFormatError(f"{layout.name} field {segment.fieldName}: {reason}") from error


def _RenderFieldSegment(values: dict[str, object], segment: LineSegment) -> str:
    """Render a named field: numeric-edited through its picture, otherwise as moved text."""
    value = _RequireValue(values, segment.fieldName)
    if segment.picture is not None:
        return FormatEdited(_RequireNumber(value), segment.picture)
    if not isinstance(value, str):
        raise _FieldValueError(f"the value must be str, not {type(value).__name__}")
    return _FitText(value, segment.length)


def _FitText(text: str, length: int) -> str:
    """An alphanumeric MOVE: left-justified, padded with spaces or cut on the right."""
    return text[:length].ljust(length)


def _ValidateLineSegment(segment: LineSegment) -> None:
    """Refuse a segment that is not exactly one of literal or field, or whose picture misfits."""
    if segment.length < 1 or (segment.literal is None) == (segment.fieldName is None):
        raise RecordFormatError(f"{segment}: set exactly one of literal and fieldName")
    if segment.literal is not None and len(segment.literal) > segment.length:
        raise RecordFormatError(f"{segment}: the literal is longer than its length")
    if segment.picture is None:
        return
    if segment.fieldName is None:
        raise RecordFormatError(f"{segment}: a literal cannot have a picture")
    try:
        pictureWidth = len(FormatEdited(0, segment.picture))
    except PictureError as error:
        raise RecordFormatError(f"{segment}: {error}") from error
    if pictureWidth != segment.length:
        raise RecordFormatError(f"{segment}: the picture is {pictureWidth} columns wide")


# CVTRA07Y REPORT-NAME-HEADER: 115 columns.
REPORT_NAME_HEADER_LINE = LineLayout(
    name="REPORT-NAME-HEADER",
    segments=(
        LineSegment(38, literal="DALYREPT"),
        LineSegment(41, literal="Daily Transaction Report"),
        LineSegment(12, literal="Date Range: "),
        LineSegment(10, fieldName="start_date"),
        LineSegment(4, literal=" to "),
        LineSegment(10, fieldName="end_date"),
    ),
)

# CVTRA07Y TRANSACTION-DETAIL-REPORT: 114 columns, TRAN-REPORT-AMT at offset 97. A FILLER VALUE
# SPACES is a single-space literal padded to its length.
TRANSACTION_DETAIL_LINE = LineLayout(
    name="TRANSACTION-DETAIL-REPORT",
    segments=(
        LineSegment(16, fieldName="tran_id"),
        LineSegment(1, literal=" "),
        LineSegment(11, fieldName="acct_id"),
        LineSegment(1, literal=" "),
        LineSegment(2, fieldName="type_cd"),
        LineSegment(1, literal="-"),
        LineSegment(15, fieldName="type_desc"),
        LineSegment(1, literal=" "),
        LineSegment(4, fieldName="cat_cd", picture="9(4)"),
        LineSegment(1, literal="-"),
        LineSegment(29, fieldName="cat_desc"),
        LineSegment(1, literal=" "),
        LineSegment(10, fieldName="source"),
        LineSegment(4, literal=" "),
        LineSegment(15, fieldName="amount", picture=REPORT_DETAIL_AMOUNT_PICTURE),
        LineSegment(2, literal=" "),
    ),
)

# CVTRA07Y TRANSACTION-HEADER-1: 114 columns.
TRANSACTION_HEADER_1_LINE = LineLayout(
    name="TRANSACTION-HEADER-1",
    segments=(
        LineSegment(17, literal="Transaction ID"),
        LineSegment(12, literal="Account ID"),
        LineSegment(19, literal="Transaction Type"),
        LineSegment(35, literal="Tran Category"),
        LineSegment(14, literal="Tran Source"),
        LineSegment(1, literal=" "),
        LineSegment(16, literal="        Amount"),
    ),
)

# CVTRA07Y TRANSACTION-HEADER-2: PIC X(133) VALUE ALL '-'.
TRANSACTION_HEADER_2_LINE = LineLayout(
    name="TRANSACTION-HEADER-2",
    segments=(LineSegment(REPORT_LINE_WIDTH, literal="-" * REPORT_LINE_WIDTH),),
)

# CVTRA07Y REPORT-PAGE-TOTALS: 112 columns.
REPORT_PAGE_TOTALS_LINE = LineLayout(
    name="REPORT-PAGE-TOTALS",
    segments=(
        LineSegment(11, literal="Page Total"),
        LineSegment(86, literal="." * 86),
        LineSegment(15, fieldName="amount", picture=REPORT_TOTAL_AMOUNT_PICTURE),
    ),
)

# CVTRA07Y REPORT-ACCOUNT-TOTALS: 112 columns.
REPORT_ACCOUNT_TOTALS_LINE = LineLayout(
    name="REPORT-ACCOUNT-TOTALS",
    segments=(
        LineSegment(13, literal="Account Total"),
        LineSegment(84, literal="." * 84),
        LineSegment(15, fieldName="amount", picture=REPORT_TOTAL_AMOUNT_PICTURE),
    ),
)

# CVTRA07Y REPORT-GRAND-TOTALS: 112 columns.
REPORT_GRAND_TOTALS_LINE = LineLayout(
    name="REPORT-GRAND-TOTALS",
    segments=(
        LineSegment(11, literal="Grand Total"),
        LineSegment(86, literal="." * 86),
        LineSegment(15, fieldName="amount", picture=REPORT_TOTAL_AMOUNT_PICTURE),
    ),
)
