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
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.core.paths import REPO_ROOT
from app.utils import record_layouts
from app.utils.record_layouts import (
    ACCOUNT_LAYOUT,
    DAILY_TRANSACTION_LAYOUT,
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
