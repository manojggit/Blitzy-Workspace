"""Unit tests for app.utils.record_layouts, the copybook record and timestamp codec.

Expected values are derived from the legacy sources and sample images, never from the unit:
- LRECLs, field offsets and FILLER spans are summed from the PIC clauses of cpy/CVACT01Y.cpy,
    cpy/CVCUS01Y.cpy, cpy/CVACT02Y.cpy, cpy/CVACT03Y.cpy, cpy/CVTRA01Y.cpy through
    cpy/CVTRA06Y.cpy, cpy/CSUSR01Y.cpy and cpy/COSTM01.CPY. For example, CVACT01Y holds
    11 + 1 + 12 * 3 + 10 * 3 + 12 * 2 + 10 + 10 = 122 bytes of fields, then FILLER X(178).
- DALYREJS is FD-REJS-RECORD in cbl/CBTRN02C.cbl: the 350-byte daily record, then the 80-byte
    WS-VALIDATION-TRAILER of WS-VALIDATION-FAIL-REASON 9(04) and its description X(76).
- Record 1 of data/ASCII/acctdata.txt and of data/ASCII/dailytran.txt, read through REPO_ROOT,
    with their zoned fields decoded by hand: 00000001940{ is +194.00 and 0000005047G is +504.77.
- The four 26-character timestamp forms of the Technical Specification, Section 0.6.6:
    "S" YYYY-MM-DD HH:MM:SS.ffffff, "D" YYYY-MM-DD-HH.MM.SS.cc0000, "T" YYYY-MM-DD followed by
    16 spaces (midnight), and 26 spaces (NULL, format " ").
- Hostile records are record 1 of data/ASCII/acctdata.txt, custdata.txt, carddata.txt and
    dailytran.txt with one whole field overwritten at the offset its copybook PIC clauses give,
    and a CSUSR01Y record, which no ASCII image holds, built from literals around a password
    generated at run time. Every refusal names the field and its offset; X, 9 and date refusals
    must not echo the refused text, while S9V and TS26 ones quote it and are checked by cause.
"""

import secrets
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

import pytest

from app.core.paths import REPO_ROOT
from app.utils import record_layouts
from app.utils.decimal_utils import ZonedDecimalError
from app.utils.record_layouts import (
    ACCOUNT_LAYOUT,
    CARD_LAYOUT,
    CUSTOMER_LAYOUT,
    DAILY_TRANSACTION_LAYOUT,
    USER_SECURITY_LAYOUT,
    DecodeRecord,
    DecodeTimestamp,
    EncodeRecord,
    EncodeTimestamp,
    RecordFormatError,
    TimestampFormatError,
)

ACCOUNT_IMAGE = "data/ASCII/acctdata.txt"
DAILY_TRANSACTION_IMAGE = "data/ASCII/dailytran.txt"
TIMESTAMP_LENGTH = 26

# (layout constant, LRECL from the copybook PIC clauses).
LAYOUT_LRECLS = (
    ("ACCOUNT_LAYOUT", 300),
    ("CUSTOMER_LAYOUT", 500),
    ("CARD_LAYOUT", 150),
    ("CARD_XREF_LAYOUT", 50),
    ("TRAN_CAT_BALANCE_LAYOUT", 50),
    ("DISCLOSURE_GROUP_LAYOUT", 50),
    ("TRANSACTION_TYPE_LAYOUT", 60),
    ("TRANSACTION_CATEGORY_LAYOUT", 60),
    ("TRANSACTION_LAYOUT", 350),
    ("DAILY_TRANSACTION_LAYOUT", 350),
    ("USER_SECURITY_LAYOUT", 80),
    ("STATEMENT_WORK_LAYOUT", 350),
    ("DALYREJS_LAYOUT", 430),
)

# (layout constant, FILLER offset, FILLER length). Each copybook has exactly one FILLER; the
# DALYREJS one is the daily record's, inside the record, because the trailer has none.
FILLER_SPANS = (
    ("ACCOUNT_LAYOUT", 122, 178),
    ("CUSTOMER_LAYOUT", 332, 168),
    ("CARD_LAYOUT", 91, 59),
    ("CARD_XREF_LAYOUT", 36, 14),
    ("TRAN_CAT_BALANCE_LAYOUT", 28, 22),
    ("DISCLOSURE_GROUP_LAYOUT", 22, 28),
    ("TRANSACTION_TYPE_LAYOUT", 52, 8),
    ("TRANSACTION_CATEGORY_LAYOUT", 56, 4),
    ("TRANSACTION_LAYOUT", 330, 20),
    ("DAILY_TRANSACTION_LAYOUT", 330, 20),
    ("USER_SECURITY_LAYOUT", 57, 23),
    ("STATEMENT_WORK_LAYOUT", 330, 20),
    ("DALYREJS_LAYOUT", 330, 20),
)

# WS-VALIDATION-FAIL-REASON 9(04) at 350, WS-VALIDATION-FAIL-REASON-DESC X(76) at 354.
DALYREJS_TRAILER_SPANS = [(350, 4), (354, 76)]

ACCOUNT_RECORD_ONE = {
    "acct_id": "00000000001",
    "active_status": "Y",
    "curr_balance": Decimal("194.00"),
    "credit_limit": Decimal("2020.00"),
    "cash_credit_limit": Decimal("1020.00"),
    "open_date": date(2014, 11, 20),
    "expiration_date": date(2025, 5, 20),
    "reissue_date": date(2025, 5, 20),
    "curr_cycle_credit": Decimal("0.00"),
    "curr_cycle_debit": Decimal("0.00"),
    "addr_zip": "A000000000",
    "group_id": "",
}

DAILY_TRANSACTION_RECORD_ONE = {
    "tran_id": "0000000000683580",
    "tran_type_cd": "01",
    "tran_cat_cd": 1,
    "tran_source": "POS TERM",
    "tran_desc": "Purchase at Abshire-Lowe",
    "tran_amt": Decimal("504.77"),
    "merchant_id": "800000000",
    "merchant_name": "Abshire-Lowe",
    "merchant_city": "North Enoshaven",
    "merchant_zip": "72112",
    "card_num": "4859452612877065",
    "orig_ts": datetime(2022, 6, 10, 19, 27, 53),
    "orig_ts_format": "S",
    "proc_ts": None,
    "proc_ts_format": " ",
}

# (layout, sample image, every column of its first record).
SAMPLE_RECORDS = (
    (ACCOUNT_LAYOUT, ACCOUNT_IMAGE, ACCOUNT_RECORD_ONE),
    (DAILY_TRANSACTION_LAYOUT, DAILY_TRANSACTION_IMAGE, DAILY_TRANSACTION_RECORD_ONE),
)
SAMPLE_RECORD_IDS = ("acctdata-record-1", "dailytran-record-1")

# (text, value, format code) for every timestamp form.
TIMESTAMP_FORMS = (
    ("2022-06-10 19:27:53.000000", datetime(2022, 6, 10, 19, 27, 53), "S"),
    ("2022-07-18-00.00.00.000000", datetime(2022, 7, 18), "D"),
    ("2022-07-18-13.45.12.340000", datetime(2022, 7, 18, 13, 45, 12, 340000), "D"),
    ("2022-07-17" + " " * 16, datetime(2022, 7, 17), "T"),
    (" " * TIMESTAMP_LENGTH, None, " "),
)
TIMESTAMP_FORM_IDS = ("S-dalytran-orig-ts", "D-midnight", "D-hundredths", "T-midnight", "blank")

INVALID_TIMESTAMP_TEXTS = (
    "2022/06/10 19:27:53.000000",
    "2022-06-10 19:27:53.00000",
    "2022-13-01 00:00:00.000000",
)
INVALID_TIMESTAMP_IDS = ("slash-separated", "25-characters", "month-13")

# (value, format code) pairs no 26-character form can hold.
UNENCODABLE_TIMESTAMPS = (
    (datetime(2022, 7, 18, 13, 45, 12, 345678), "D"),
    (datetime(2022, 7, 17, 12, 0), "T"),
    (datetime(2022, 7, 17), " "),
    (None, "S"),
    (datetime(2022, 7, 18, tzinfo=UTC), "S"),
    (datetime(2022, 7, 18), "X"),
)
UNENCODABLE_TIMESTAMP_IDS = (
    "D-below-hundredths",
    "T-not-midnight",
    "blank-with-value",
    "S-without-value",
    "zone-aware",
    "unknown-format-code",
)


def ReadFirstRecord(relativePath: str) -> str:
    """Return record 1 of a CRLF-terminated ASCII image under the repository root."""
    return (REPO_ROOT / relativePath).read_bytes().split(b"\r\n")[0].decode("ascii")


def CollectSpans(layout: record_layouts.RecordLayout) -> list[tuple[int, int]]:
    """Return the (offset, length) of every field and every FILLER span, in offset order."""
    fieldSpans = [(field.offset, field.length) for field in layout.fields]
    return sorted(fieldSpans + list(layout.FillerSpans()))


def DecimalScales(record: dict[str, object]) -> dict[str, int]:
    """Return the exponent of every Decimal column, so 194.00 and 194 differ."""
    return {
        columnName: value.as_tuple().exponent
        for columnName, value in record.items()
        if isinstance(value, Decimal)
    }


@pytest.mark.parametrize("layoutName, expectedLrecl", LAYOUT_LRECLS)
def test_layout_lrecl_matches_copybook(layoutName, expectedLrecl):
    assert getattr(record_layouts, layoutName).lrecl == expectedLrecl


@pytest.mark.parametrize("layoutName, expectedLrecl", LAYOUT_LRECLS)
def test_fields_and_fillers_tile_the_record(layoutName, expectedLrecl):
    spans = CollectSpans(getattr(record_layouts, layoutName))
    position = 0
    for offset, length in spans:
        assert offset == position, f"{layoutName}: gap or overlap at offset {position}"
        position = offset + length
    assert position == expectedLrecl
    assert sum(length for _, length in spans) == expectedLrecl


@pytest.mark.parametrize("layoutName, fillerOffset, fillerLength", FILLER_SPANS)
def test_filler_span_positions(layoutName, fillerOffset, fillerLength):
    layout = getattr(record_layouts, layoutName)
    assert layout.FillerSpans() == ((fillerOffset, fillerLength),)


def test_dalyrejs_appends_the_validation_trailer_to_the_daily_record():
    layout = record_layouts.DALYREJS_LAYOUT
    dailyFieldCount = len(DAILY_TRANSACTION_LAYOUT.fields)
    trailerSpans = [(field.offset, field.length) for field in layout.fields[dailyFieldCount:]]
    assert layout.fields[:dailyFieldCount] == DAILY_TRANSACTION_LAYOUT.fields
    assert trailerSpans == DALYREJS_TRAILER_SPANS


@pytest.mark.parametrize(
    "layout, relativePath, expectedRecord", SAMPLE_RECORDS, ids=SAMPLE_RECORD_IDS
)
def test_decode_first_sample_record(layout, relativePath, expectedRecord):
    decoded = DecodeRecord(ReadFirstRecord(relativePath), layout)
    assert decoded == expectedRecord
    for columnName, expectedValue in expectedRecord.items():
        assert type(decoded[columnName]) is type(expectedValue), columnName
    assert DecimalScales(decoded) == DecimalScales(expectedRecord)


@pytest.mark.parametrize(
    "layout, relativePath, expectedRecord", SAMPLE_RECORDS, ids=SAMPLE_RECORD_IDS
)
def test_encode_record_round_trips_byte_for_byte(layout, relativePath, expectedRecord):
    recordText = ReadFirstRecord(relativePath)
    encodedFromColumns = EncodeRecord(expectedRecord, layout)
    roundTripped = EncodeRecord(DecodeRecord(recordText, layout), layout)
    assert encodedFromColumns.encode("ascii") == recordText.encode("ascii")
    assert roundTripped.encode("ascii") == recordText.encode("ascii")
    assert len(roundTripped) == layout.lrecl


@pytest.mark.parametrize("textLength", (299, 301), ids=("one-short", "one-over-long"))
def test_decode_record_rejects_text_of_another_length(textLength):
    recordText = ReadFirstRecord(ACCOUNT_IMAGE) + " "
    with pytest.raises(RecordFormatError):
        DecodeRecord(recordText[:textLength], ACCOUNT_LAYOUT)


@pytest.mark.parametrize(
    "text, expectedValue, expectedFormat", TIMESTAMP_FORMS, ids=TIMESTAMP_FORM_IDS
)
def test_decode_timestamp_forms(text, expectedValue, expectedFormat):
    assert DecodeTimestamp(text) == (expectedValue, expectedFormat)


@pytest.mark.parametrize(
    "text, expectedValue, expectedFormat", TIMESTAMP_FORMS, ids=TIMESTAMP_FORM_IDS
)
def test_encode_timestamp_rebuilds_26_bytes(text, expectedValue, expectedFormat):
    encoded = EncodeTimestamp(expectedValue, expectedFormat)
    assert encoded == text
    assert len(encoded) == TIMESTAMP_LENGTH


@pytest.mark.parametrize("text", INVALID_TIMESTAMP_TEXTS, ids=INVALID_TIMESTAMP_IDS)
def test_decode_timestamp_rejects_other_text(text):
    with pytest.raises(TimestampFormatError):
        DecodeTimestamp(text)


@pytest.mark.parametrize(
    "value, timestampFormat", UNENCODABLE_TIMESTAMPS, ids=UNENCODABLE_TIMESTAMP_IDS
)
def test_encode_timestamp_refuses_values_its_form_cannot_hold(value, timestampFormat):
    with pytest.raises(TimestampFormatError):
        EncodeTimestamp(value, timestampFormat)


class FieldContext(NamedTuple):
    """A field as its copybook gives it: record name, column, data name, offset and length."""

    layoutName: str
    columnName: str
    copybookName: str
    fieldOffset: int
    fieldLength: int


CUSTOMER_IMAGE = "data/ASCII/custdata.txt"
CARD_IMAGE = "data/ASCII/carddata.txt"

# The layout under test and the sample image whose record 1 is corrupted, by copybook record.
SAMPLE_IMAGES = {
    "CVACT01Y": (ACCOUNT_LAYOUT, ACCOUNT_IMAGE),
    "CVCUS01Y": (CUSTOMER_LAYOUT, CUSTOMER_IMAGE),
    "CVACT02Y": (CARD_LAYOUT, CARD_IMAGE),
    "CVTRA06Y": (DAILY_TRANSACTION_LAYOUT, DAILY_TRANSACTION_IMAGE),
}

# Offsets and lengths summed from the PIC clauses of cpy/CVACT01Y.cpy, cpy/CVCUS01Y.cpy,
# cpy/CVACT02Y.cpy, cpy/CVTRA06Y.cpy and cpy/CSUSR01Y.cpy.
ACCT_ID_FIELD = FieldContext("CVACT01Y", "acct_id", "ACCT-ID", 0, 11)
ACCT_CURR_BAL_FIELD = FieldContext("CVACT01Y", "curr_balance", "ACCT-CURR-BAL", 12, 12)
ACCT_OPEN_DATE_FIELD = FieldContext("CVACT01Y", "open_date", "ACCT-OPEN-DATE", 48, 10)
ACCT_ADDR_ZIP_FIELD = FieldContext("CVACT01Y", "addr_zip", "ACCT-ADDR-ZIP", 102, 10)
CUST_SSN_FIELD = FieldContext("CVCUS01Y", "ssn", "CUST-SSN", 279, 9)
CUST_DOB_FIELD = FieldContext("CVCUS01Y", "date_of_birth", "CUST-DOB-YYYY-MM-DD", 308, 10)
CARD_CVV_FIELD = FieldContext("CVACT02Y", "cvv_cd", "CARD-CVV-CD", 27, 3)
DALYTRAN_CAT_CD_FIELD = FieldContext("CVTRA06Y", "tran_cat_cd", "DALYTRAN-CAT-CD", 18, 4)
DALYTRAN_DESC_FIELD = FieldContext("CVTRA06Y", "tran_desc", "DALYTRAN-DESC", 32, 100)
DALYTRAN_AMT_FIELD = FieldContext("CVTRA06Y", "tran_amt", "DALYTRAN-AMT", 132, 11)
DALYTRAN_MERCHANT_NAME_FIELD = FieldContext(
    "CVTRA06Y", "merchant_name", "DALYTRAN-MERCHANT-NAME", 152, 50
)
DALYTRAN_ORIG_TS_FIELD = FieldContext("CVTRA06Y", "orig_ts", "DALYTRAN-ORIG-TS", 278, 26)
SEC_USR_PWD_FIELD = FieldContext("CSUSR01Y", "password", "SEC-USR-PWD", 48, 8)

# CSUSR01Y SEC-USR-ID X(08) at 0, FNAME and LNAME X(20) at 8 and 28, PWD X(08) at 48, TYPE X(01)
# at 56, FILLER X(23) at 57. The user is none of the USRSEC sample users.
USER_SECURITY_LRECL = 80
USER_SECURITY_PASSWORD_LENGTH = 8
NON_SAMPLE_USER_ID = "QAUSER01"
NON_SAMPLE_FIRST_NAME = "Hostile"
NON_SAMPLE_LAST_NAME = "Record"
NON_SAMPLE_USER_TYPE = "U"

DIGITS_REASON = "the text must consist of the digits 0-9 only"
FORBIDDEN_CHARACTER_REASON = "the text contains a NUL, CR or LF character"
CALENDAR_DATE_REASON = "the YYYY-MM-DD text is not a calendar date"
DATE_PATTERN_REASON = "the text is neither blank nor a YYYY-MM-DD date"
# NUL cannot be stored in PostgreSQL text; CR and LF would split a line-oriented record file.
FORBIDDEN_CHARACTERS = (
    pytest.param("\x00", id="nul"),
    pytest.param("\r", id="cr"),
    pytest.param("\n", id="lf"),
)

# (field, text overwriting the whole field, reason) for X, 9, 9N and date fields.
CORRUPT_FIELD_REFUSALS = (
    pytest.param(ACCT_ID_FIELD, "0000000000A", DIGITS_REASON, id="acct-id-letter"),
    pytest.param(ACCT_ID_FIELD, "0000000001 ", DIGITS_REASON, id="acct-id-trailing-space"),
    pytest.param(ACCT_ID_FIELD, "\u0660" * 11, DIGITS_REASON, id="acct-id-arabic-indic-digits"),
    pytest.param(CUST_SSN_FIELD, "0209738X8", DIGITS_REASON, id="cust-ssn-letter"),
    pytest.param(CARD_CVV_FIELD, "7O7", DIGITS_REASON, id="card-cvv-letter"),
    pytest.param(DALYTRAN_CAT_CD_FIELD, "00-1", DIGITS_REASON, id="dalytran-cat-cd-hyphen"),
    pytest.param(DALYTRAN_CAT_CD_FIELD, " 001", DIGITS_REASON, id="dalytran-cat-cd-leading-space"),
    pytest.param(ACCT_OPEN_DATE_FIELD, "2014-02-30", CALENDAR_DATE_REASON, id="open-date-feb-30"),
    pytest.param(ACCT_OPEN_DATE_FIELD, "2014-13-01", CALENDAR_DATE_REASON, id="open-date-month-13"),
    pytest.param(ACCT_OPEN_DATE_FIELD, "2014/11/20", DATE_PATTERN_REASON, id="open-date-slashes"),
    pytest.param(CUST_DOB_FIELD, "1961-02-29", CALENDAR_DATE_REASON, id="dob-feb-29-common-year"),
    pytest.param(CUST_DOB_FIELD, "19610608  ", DATE_PATTERN_REASON, id="dob-without-hyphens"),
    pytest.param(ACCT_ADDR_ZIP_FIELD, "A0000\x000000", FORBIDDEN_CHARACTER_REASON, id="zip-nul"),
    pytest.param(ACCT_ADDR_ZIP_FIELD, "A0000\r0000", FORBIDDEN_CHARACTER_REASON, id="zip-cr"),
    pytest.param(ACCT_ADDR_ZIP_FIELD, "A0000\n0000", FORBIDDEN_CHARACTER_REASON, id="zip-lf"),
    pytest.param(
        DALYTRAN_DESC_FIELD,
        "Purchase at Abshire-Lowe\r\n".ljust(100),
        FORBIDDEN_CHARACTER_REASON,
        id="dalytran-desc-crlf",
    ),
)

# (field, text overwriting the whole field, error class the RecordFormatError is raised from)
# for S9V and TS26 fields, whose own messages quote the text they refuse.
CORRUPT_FIELD_CAUSES = (
    pytest.param(ACCT_CURR_BAL_FIELD, "00000001940X", ZonedDecimalError, id="curr-bal-overpunch"),
    pytest.param(ACCT_CURR_BAL_FIELD, "0000000A940{", ZonedDecimalError, id="curr-bal-letter"),
    pytest.param(ACCT_CURR_BAL_FIELD, " " * 12, ZonedDecimalError, id="curr-bal-blank"),
    pytest.param(DALYTRAN_AMT_FIELD, "0000005047Z", ZonedDecimalError, id="tran-amt-overpunch"),
    pytest.param(DALYTRAN_AMT_FIELD, "+0000504.77", ZonedDecimalError, id="tran-amt-edited"),
    pytest.param(
        DALYTRAN_ORIG_TS_FIELD,
        "2022-06-10T19:27:53.000000",
        TimestampFormatError,
        id="orig-ts-iso-separator",
    ),
    pytest.param(
        DALYTRAN_ORIG_TS_FIELD,
        "2022-02-30 19:27:53.000000",
        TimestampFormatError,
        id="orig-ts-feb-30",
    ),
)


def SpliceField(recordText: str, fieldContext: FieldContext, fieldText: str) -> str:
    """Return recordText with the whole field overwritten by fieldText, at the same length."""
    fieldEnd = fieldContext.fieldOffset + fieldContext.fieldLength
    splicedText = recordText[: fieldContext.fieldOffset] + fieldText + recordText[fieldEnd:]
    assert len(splicedText) == len(recordText), fieldContext
    return splicedText


def AssertNamesField(message: str, fieldContext: FieldContext) -> None:
    """Assert message names the copybook record, column, data name and offset of the field."""
    assert fieldContext.layoutName in message
    assert fieldContext.columnName in message
    assert fieldContext.copybookName in message
    assert f"at offset {fieldContext.fieldOffset}:" in message


def AssertLocatesField(refusal: RecordFormatError, fieldContext: FieldContext) -> None:
    """Assert a decode refusal names the field in its message and carries its column and offset."""
    AssertNamesField(str(refusal), fieldContext)
    assert (refusal.fieldName, refusal.fieldOffset) == (
        fieldContext.columnName,
        fieldContext.fieldOffset,
    )


def AssertWithheld(message: str, text: str) -> None:
    """Assert message holds text in none of its raw, repr and right-trimmed forms."""
    trimmedText = text.rstrip(" ")
    assert trimmedText, "a blank text cannot be checked for an echo"
    for form in (text, repr(text), trimmedText, repr(trimmedText)):
        assert form not in message


def GeneratePassword(length: int) -> str:
    """Return a random URL-safe password of length characters, generated at run time."""
    return secrets.token_urlsafe(length)[:length]


def BuildUserSecurityRecord(passwordText: str) -> str:
    """Return the 80-character CSUSR01Y record of a non-sample user holding passwordText."""
    recordText = (
        NON_SAMPLE_USER_ID.ljust(8)
        + NON_SAMPLE_FIRST_NAME.ljust(20)
        + NON_SAMPLE_LAST_NAME.ljust(20)
        + passwordText.ljust(USER_SECURITY_PASSWORD_LENGTH)
        + NON_SAMPLE_USER_TYPE
    ).ljust(USER_SECURITY_LRECL)
    assert len(recordText) == USER_SECURITY_LRECL
    return recordText


@pytest.mark.parametrize("fieldContext, fieldText, expectedReason", CORRUPT_FIELD_REFUSALS)
def test_decode_record_refuses_corrupt_field_text_without_echoing_it(
    fieldContext, fieldText, expectedReason
):
    layout, imagePath = SAMPLE_IMAGES[fieldContext.layoutName]
    recordText = SpliceField(ReadFirstRecord(imagePath), fieldContext, fieldText)
    with pytest.raises(RecordFormatError) as refusal:
        DecodeRecord(recordText, layout)
    message = str(refusal.value)
    AssertLocatesField(refusal.value, fieldContext)
    assert expectedReason in message
    AssertWithheld(message, fieldText)


@pytest.mark.parametrize("fieldContext, fieldText, expectedCause", CORRUPT_FIELD_CAUSES)
def test_decode_record_raises_from_the_field_decoder_error(fieldContext, fieldText, expectedCause):
    layout, imagePath = SAMPLE_IMAGES[fieldContext.layoutName]
    recordText = SpliceField(ReadFirstRecord(imagePath), fieldContext, fieldText)
    with pytest.raises(RecordFormatError) as refusal:
        DecodeRecord(recordText, layout)
    AssertLocatesField(refusal.value, fieldContext)
    assert isinstance(refusal.value.__cause__, expectedCause)


@pytest.mark.parametrize("forbiddenCharacter", FORBIDDEN_CHARACTERS)
def test_decode_record_withholds_a_password_holding_a_forbidden_character(forbiddenCharacter):
    secretText = GeneratePassword(USER_SECURITY_PASSWORD_LENGTH - 1)
    fieldText = secretText + forbiddenCharacter
    with pytest.raises(RecordFormatError) as refusal:
        DecodeRecord(BuildUserSecurityRecord(fieldText), USER_SECURITY_LAYOUT)
    message = str(refusal.value)
    AssertNamesField(message, SEC_USR_PWD_FIELD)
    assert FORBIDDEN_CHARACTER_REASON in message
    AssertWithheld(message, fieldText)
    AssertWithheld(message, secretText)


# The layout under test and the columns of its sample record 1, by copybook record.
SAMPLE_COLUMNS = {
    "CVACT01Y": (ACCOUNT_LAYOUT, ACCOUNT_RECORD_ONE),
    "CVTRA06Y": (DAILY_TRANSACTION_LAYOUT, DAILY_TRANSACTION_RECORD_ONE),
}
# A change to this value deletes the column instead of replacing it.
REMOVED_COLUMN = object()

# (field, column changes, reason) for missing columns and values of the wrong type.
UNENCODABLE_COLUMN_VALUES = (
    pytest.param(
        ACCT_ID_FIELD,
        {"acct_id": REMOVED_COLUMN},
        "no value was supplied for 'acct_id'",
        id="missing-acct-id",
    ),
    pytest.param(
        ACCT_CURR_BAL_FIELD,
        {"curr_balance": REMOVED_COLUMN},
        "no value was supplied for 'curr_balance'",
        id="missing-curr-balance",
    ),
    pytest.param(
        ACCT_OPEN_DATE_FIELD,
        {"open_date": REMOVED_COLUMN},
        "no value was supplied for 'open_date'",
        id="missing-open-date",
    ),
    pytest.param(
        DALYTRAN_ORIG_TS_FIELD,
        {"orig_ts_format": REMOVED_COLUMN},
        "no value was supplied for 'orig_ts_format'",
        id="missing-orig-ts-format",
    ),
    pytest.param(
        ACCT_CURR_BAL_FIELD,
        {"curr_balance": 194.0},
        "the value must be Decimal or int, not float",
        id="curr-balance-float",
    ),
    pytest.param(
        ACCT_CURR_BAL_FIELD,
        {"curr_balance": True},
        "the value must be Decimal or int, not bool",
        id="curr-balance-bool",
    ),
    pytest.param(
        DALYTRAN_CAT_CD_FIELD,
        {"tran_cat_cd": 1.0},
        "the value must be int, not float",
        id="tran-cat-cd-float",
    ),
    pytest.param(
        DALYTRAN_CAT_CD_FIELD,
        {"tran_cat_cd": True},
        "the value must be int, not bool",
        id="tran-cat-cd-bool",
    ),
    pytest.param(
        ACCT_ID_FIELD, {"acct_id": 1.0}, "the value must be int, not float", id="acct-id-float"
    ),
    pytest.param(
        ACCT_ID_FIELD, {"acct_id": False}, "the value must be int, not bool", id="acct-id-bool"
    ),
    pytest.param(
        ACCT_ADDR_ZIP_FIELD,
        {"addr_zip": 12345},
        "the value must be str, not int",
        id="addr-zip-int",
    ),
    pytest.param(
        ACCT_OPEN_DATE_FIELD,
        {"open_date": "2014-11-20"},
        "the value must be a date or None, not str",
        id="open-date-str",
    ),
    pytest.param(
        ACCT_OPEN_DATE_FIELD,
        {"open_date": datetime(2014, 11, 20)},
        "the value must be a date or None, not datetime",
        id="open-date-datetime",
    ),
)

# (field, text replacing the field's column, reason) for X and 9 columns.
UNENCODABLE_TEXT_VALUES = (
    pytest.param(
        ACCT_ADDR_ZIP_FIELD,
        "A0000000001",
        "11 characters exceed the length 10",
        id="addr-zip-11-characters",
    ),
    pytest.param(
        DALYTRAN_DESC_FIELD,
        "Purchase at Abshire-Lowe".ljust(101, "."),
        "101 characters exceed the length 100",
        id="tran-desc-101-characters",
    ),
    pytest.param(
        ACCT_ID_FIELD, "000000000001", "12 digits exceed the length 11", id="acct-id-12-digits"
    ),
    pytest.param(ACCT_ADDR_ZIP_FIELD, "A0000\x00", FORBIDDEN_CHARACTER_REASON, id="addr-zip-nul"),
    pytest.param(ACCT_ADDR_ZIP_FIELD, "A0000\r", FORBIDDEN_CHARACTER_REASON, id="addr-zip-cr"),
    pytest.param(ACCT_ADDR_ZIP_FIELD, "A0000\n", FORBIDDEN_CHARACTER_REASON, id="addr-zip-lf"),
    pytest.param(
        DALYTRAN_MERCHANT_NAME_FIELD,
        "Abshire\rLowe",
        FORBIDDEN_CHARACTER_REASON,
        id="merchant-name-cr",
    ),
    pytest.param(
        DALYTRAN_DESC_FIELD,
        "Purchase at\nAbshire-Lowe",
        FORBIDDEN_CHARACTER_REASON,
        id="tran-desc-lf",
    ),
    pytest.param(ACCT_ID_FIELD, "0000000000A", DIGITS_REASON, id="acct-id-letter"),
    pytest.param(ACCT_ID_FIELD, "\u0660" * 11, DIGITS_REASON, id="acct-id-arabic-indic-digits"),
)

# (field, column changes, error class the RecordFormatError is raised from) for numbers and
# timestamps the field's picture or form cannot hold.
UNENCODABLE_VALUE_CAUSES = (
    pytest.param(
        ACCT_CURR_BAL_FIELD,
        {"curr_balance": Decimal("NaN")},
        ZonedDecimalError,
        id="curr-balance-nan",
    ),
    pytest.param(
        DALYTRAN_AMT_FIELD,
        {"tran_amt": Decimal("Infinity")},
        ZonedDecimalError,
        id="tran-amt-infinity",
    ),
    pytest.param(
        ACCT_CURR_BAL_FIELD,
        {"curr_balance": Decimal("1E+40")},
        InvalidOperation,
        id="curr-balance-beyond-31-digits",
    ),
    pytest.param(
        DALYTRAN_ORIG_TS_FIELD,
        {"orig_ts": datetime(2022, 6, 10, 19, 27, 53, 345678), "orig_ts_format": "D"},
        TimestampFormatError,
        id="orig-ts-d-below-hundredths",
    ),
    pytest.param(
        DALYTRAN_ORIG_TS_FIELD,
        {"orig_ts": "2022-06-10 19:27:53.000000"},
        TimestampFormatError,
        id="orig-ts-text",
    ),
)


def ChangeColumns(columns: dict[str, object], changes: dict[str, object]) -> dict[str, object]:
    """Return a copy of columns with changes applied; a REMOVED_COLUMN value deletes the column."""
    changedColumns = dict(columns)
    for columnName, value in changes.items():
        if value is REMOVED_COLUMN:
            del changedColumns[columnName]
        else:
            changedColumns[columnName] = value
    return changedColumns


def BuildUserSecurityColumns(passwordText: str) -> dict[str, object]:
    """Return the CSUSR01Y columns of the non-sample user, holding passwordText."""
    return {
        "user_id": NON_SAMPLE_USER_ID,
        "first_name": NON_SAMPLE_FIRST_NAME,
        "last_name": NON_SAMPLE_LAST_NAME,
        "password": passwordText,
        "user_type": NON_SAMPLE_USER_TYPE,
    }


@pytest.mark.parametrize("fieldContext, changes, expectedReason", UNENCODABLE_COLUMN_VALUES)
def test_encode_record_refuses_missing_and_mistyped_values(fieldContext, changes, expectedReason):
    layout, columns = SAMPLE_COLUMNS[fieldContext.layoutName]
    with pytest.raises(RecordFormatError) as refusal:
        EncodeRecord(ChangeColumns(columns, changes), layout)
    message = str(refusal.value)
    AssertNamesField(message, fieldContext)
    assert expectedReason in message


@pytest.mark.parametrize("fieldContext, valueText, expectedReason", UNENCODABLE_TEXT_VALUES)
def test_encode_record_refuses_text_without_echoing_it(fieldContext, valueText, expectedReason):
    layout, columns = SAMPLE_COLUMNS[fieldContext.layoutName]
    changes = {fieldContext.columnName: valueText}
    with pytest.raises(RecordFormatError) as refusal:
        EncodeRecord(ChangeColumns(columns, changes), layout)
    message = str(refusal.value)
    AssertNamesField(message, fieldContext)
    assert expectedReason in message
    AssertWithheld(message, valueText)


@pytest.mark.parametrize("fieldContext, changes, expectedCause", UNENCODABLE_VALUE_CAUSES)
def test_encode_record_raises_from_the_field_encoder_error(fieldContext, changes, expectedCause):
    layout, columns = SAMPLE_COLUMNS[fieldContext.layoutName]
    with pytest.raises(RecordFormatError) as refusal:
        EncodeRecord(ChangeColumns(columns, changes), layout)
    AssertNamesField(str(refusal.value), fieldContext)
    assert isinstance(refusal.value.__cause__, expectedCause)


def test_encode_record_withholds_an_overlong_password():
    secretText = GeneratePassword(USER_SECURITY_PASSWORD_LENGTH + 1)
    with pytest.raises(RecordFormatError) as refusal:
        EncodeRecord(BuildUserSecurityColumns(secretText), USER_SECURITY_LAYOUT)
    message = str(refusal.value)
    AssertNamesField(message, SEC_USR_PWD_FIELD)
    assert "9 characters exceed the length 8" in message
    AssertWithheld(message, secretText)
