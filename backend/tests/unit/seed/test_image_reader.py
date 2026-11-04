"""Unit tests for app.seed.image_reader against the shipped CardDemo seed images.

The reader replaces the IDCAMS REPRO INFILE side of the ACCTFILE, CARDFILE, CUSTFILE, XREFFILE,
TRANFILE, TRANTYPE, TRANCATG, DISCGRP, TCATBALF and DUSRSECJ jobs, decoding records by the
copybooks CVACT01Y, CVACT02Y, CVCUS01Y, CVACT03Y, CVTRA01Y-CVTRA06Y and CSUSR01Y. These tests pin
the Technical Specification, Section 0.8.2:
- Every shipped EBCDIC image in data/EBCDIC, and every ASCII copy in data/ASCII, reads with its
    expected record count; the TRANFILE priming record of DALYTRAN.PS.INIT is skipped and counted.
- ASCII framing: CRLF records, exactly one final terminator removed (CRLF or the lone LF ending
    tcatbal.txt and trantype.txt), and cardxref.txt lines short by exactly their trailing FILLER.
- Fields decode strictly by copybook offset, money as two-place Decimal and never float.
- Truncated, bare-terminator, wrong-length and undecodable inputs raise ImageFormatError naming
    the file and the record.
- An ImageFormatError's message is one line: control characters in its path or reason are
    shown escaped, while its path, record number and reason keep the values given.
The suite is database-free, and every path derives from REPO_ROOT, never the working directory.
USRSEC passwords are compared only against an independent decode of the image, never a literal.
"""

import datetime
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

import pytest

from app.core.paths import REPO_ROOT
from app.seed.image_reader import (
    BARE_TERMINATOR_REASON,
    ENCODING_ASCII,
    ENCODING_EBCDIC,
    SAMPLE_IMAGE_NAMES,
    ImageContents,
    ImageFormatError,
    IsTranfilePrimingRecord,
    ReadImage,
    SampleImageNames,
    SampleImagePath,
)
from app.utils.record_layouts import (
    ACCOUNT_LAYOUT,
    CARD_LAYOUT,
    CARD_XREF_LAYOUT,
    CUSTOMER_LAYOUT,
    DAILY_TRANSACTION_LAYOUT,
    DISCLOSURE_GROUP_LAYOUT,
    TIMESTAMP_FORMAT_BLANK,
    TIMESTAMP_FORMAT_SPACE,
    TRAN_CAT_BALANCE_LAYOUT,
    TRANSACTION_CATEGORY_LAYOUT,
    TRANSACTION_LAYOUT,
    TRANSACTION_TYPE_LAYOUT,
    USER_SECURITY_LAYOUT,
    RecordFormatError,
    RecordLayout,
    TimestampFormatError,
)

EBCDIC_DIR = REPO_ROOT / "data" / "EBCDIC"
ASCII_DIR = REPO_ROOT / "data" / "ASCII"
RELATIVE_EBCDIC_DIR = Path("data", "EBCDIC")
CP037 = "cp037"
CRLF = "\r\n"
LF = "\n"

TRANFILE_IMAGE_NAME = "AWS.M2.CARDDEMO.DALYTRAN.PS.INIT"
USRSEC_IMAGE_NAME = "AWS.M2.CARDDEMO.USRSEC.PS"
# Shipped images no dataset may name: ACCDATA.PS duplicates ACCTDATA.PS byte for byte, and
# EXPORT.DATA.PS belongs to the out-of-scope CBEXPORT.
UNREAD_IMAGE_NAMES = frozenset({"AWS.M2.CARDDEMO.ACCDATA.PS", "AWS.M2.CARDDEMO.EXPORT.DATA.PS"})

# CSUSR01Y: SEC-USR-PWD X(08) at offset 48 of the 80-byte SEC-USER-DATA record.
USRSEC_LRECL = 80
USRSEC_PASSWORD_SLICE = slice(48, 56)
# DALYTRAN.PS.INIT: bytes 0-341 are X'00' and bytes 342-349 read "00000100".
PRIMING_ZERO_BYTES = 342
PRIMING_TAIL_TEXT = "00000100"
# CVACT03Y: cardxref.txt lines hold the 36 field bytes and omit the 14-byte FILLER at offset 36.
XREF_LINE_LENGTH = 36
XREF_FILLER_TEXT = " " * 14
# CVTRA06Y: DALYTRAN-ORIG-TS X(26) at offset 278.
ORIG_TS_OFFSET = 278
TIMESTAMP_LENGTH = 26
UNKNOWN_TIMESTAMP_TEXT = "2022/06/10 19:27:53"
# ImageFormatError's documented record number for a problem with the whole file: an EBCDIC
# size that is not a multiple of the LRECL is refused before any record is read, so the error
# names record 0, not the incomplete record.
WHOLE_FILE_RECORD_NUMBER = 0
MONEY_EXPONENT = -2


class ImageCase(NamedTuple):
    """One dataset's shipped seed image: its table, file names, layout and record count."""

    table: str
    ebcdicName: str
    asciiName: str | None
    layout: RecordLayout
    expectedCount: int


IMAGE_CASES = (
    ImageCase("accounts", "AWS.M2.CARDDEMO.ACCTDATA.PS", "acctdata.txt", ACCOUNT_LAYOUT, 50),
    ImageCase("cards", "AWS.M2.CARDDEMO.CARDDATA.PS", "carddata.txt", CARD_LAYOUT, 50),
    ImageCase("customers", "AWS.M2.CARDDEMO.CUSTDATA.PS", "custdata.txt", CUSTOMER_LAYOUT, 50),
    ImageCase("card_xref", "AWS.M2.CARDDEMO.CARDXREF.PS", "cardxref.txt", CARD_XREF_LAYOUT, 50),
    ImageCase(
        "transaction_types",
        "AWS.M2.CARDDEMO.TRANTYPE.PS",
        "trantype.txt",
        TRANSACTION_TYPE_LAYOUT,
        7,
    ),
    ImageCase(
        "transaction_categories",
        "AWS.M2.CARDDEMO.TRANCATG.PS",
        "trancatg.txt",
        TRANSACTION_CATEGORY_LAYOUT,
        18,
    ),
    ImageCase(
        "disclosure_groups",
        "AWS.M2.CARDDEMO.DISCGRP.PS",
        "discgrp.txt",
        DISCLOSURE_GROUP_LAYOUT,
        51,
    ),
    ImageCase(
        "transaction_category_balances",
        "AWS.M2.CARDDEMO.TCATBALF.PS",
        "tcatbal.txt",
        TRAN_CAT_BALANCE_LAYOUT,
        50,
    ),
    ImageCase(
        "daily_transactions",
        "AWS.M2.CARDDEMO.DALYTRAN.PS",
        "dailytran.txt",
        DAILY_TRANSACTION_LAYOUT,
        300,
    ),
    ImageCase("users", USRSEC_IMAGE_NAME, None, USER_SECURITY_LAYOUT, 10),
)
IMAGE_CASE_IDS = tuple(imageCase.table for imageCase in IMAGE_CASES)
ASCII_CASES = tuple(imageCase for imageCase in IMAGE_CASES if imageCase.asciiName is not None)
ASCII_CASE_IDS = tuple(imageCase.table for imageCase in ASCII_CASES)
CASES_BY_TABLE = {imageCase.table: imageCase for imageCase in IMAGE_CASES}
ENCODINGS = (ENCODING_EBCDIC, ENCODING_ASCII)


class LoneLfCase(NamedTuple):
    """An ASCII image whose final record ends with a lone LF, and its expected shape."""

    table: str
    expectedSize: int


LONE_LF_CASES = (
    LoneLfCase("transaction_category_balances", 2599),
    LoneLfCase("transaction_types", 433),
)
LONE_LF_IDS = ("tcatbal.txt", "trantype.txt")

EXPECTED_ACCOUNT_1 = {
    "acct_id": "00000000001",
    "active_status": "Y",
    "curr_balance": Decimal("194.00"),
    "credit_limit": Decimal("2020.00"),
    "cash_credit_limit": Decimal("1020.00"),
    "open_date": datetime.date(2014, 11, 20),
    "expiration_date": datetime.date(2025, 5, 20),
    "reissue_date": datetime.date(2025, 5, 20),
    "curr_cycle_credit": Decimal("0.00"),
    "curr_cycle_debit": Decimal("0.00"),
    "addr_zip": "A000000000",
    "group_id": "",
}
ACCOUNT_MONEY_COLUMNS = (
    "curr_balance",
    "credit_limit",
    "cash_credit_limit",
    "curr_cycle_credit",
    "curr_cycle_debit",
)

EXPECTED_DAILY_TIMESTAMPS = (
    datetime.datetime(2022, 6, 10, 19, 27, 53),
    TIMESTAMP_FORMAT_SPACE,
    None,
    TIMESTAMP_FORMAT_BLANK,
)
EXPECTED_DAILY_1 = {
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
    "orig_ts": EXPECTED_DAILY_TIMESTAMPS[0],
    "orig_ts_format": EXPECTED_DAILY_TIMESTAMPS[1],
    "proc_ts": EXPECTED_DAILY_TIMESTAMPS[2],
    "proc_ts_format": EXPECTED_DAILY_TIMESTAMPS[3],
}

EXPECTED_XREF_1 = {"card_num": "0500024453765740", "cust_id": "000000050", "acct_id": "00000000050"}

EXPECTED_USER_IDS = tuple(f"ADMIN{number:03d}" for number in range(1, 6)) + tuple(
    f"USER{number:04d}" for number in range(1, 6)
)
EXPECTED_USER_TYPES = ("A",) * 5 + ("U",) * 5


def ReadShipped(fileName: str, layout: RecordLayout, encoding: str) -> ImageContents:
    """Read a shipped seed image from data/EBCDIC or data/ASCII, chosen by encoding."""
    directory = EBCDIC_DIR if encoding == ENCODING_EBCDIC else ASCII_DIR
    return ReadImage(directory / fileName, layout, encoding)


def ReadCase(imageCase: ImageCase, encoding: str) -> ImageContents:
    """Read a dataset's shipped image in the given encoding with the dataset's layout."""
    fileName = imageCase.ebcdicName if encoding == ENCODING_EBCDIC else imageCase.asciiName
    return ReadShipped(fileName, imageCase.layout, encoding)


def WriteAsciiImage(tmpPath: Path, records: tuple[str, ...], tail: str) -> Path:
    """Write records joined by CRLF and followed by tail as an ASCII image; return its path."""
    imagePath = tmpPath / "image.txt"
    imagePath.write_bytes((CRLF.join(records) + tail).encode("ascii"))
    return imagePath


def WriteEbcdicImage(tmpPath: Path, payload: str) -> Path:
    """Write payload encoded with code page 037 as an EBCDIC image; return its path."""
    imagePath = tmpPath / "image.dat"
    imagePath.write_bytes(payload.encode(CP037))
    return imagePath


def SyntheticTypeRecord(code: str, description: str) -> str:
    """Return a 60-character CVTRA03Y record: type code, description and 8 FILLER spaces."""
    return code + description.ljust(50) + " " * 8


def ReplaceCharacter(record: str, position: int, character: str) -> str:
    """Return record with the character at position replaced, keeping its length."""
    return record[:position] + character + record[position + 1 :]


def SplitFixedRecords(imageBytes: bytes, lrecl: int) -> list[bytes]:
    """Split terminator-free fixed-length image bytes into lrecl-byte records."""
    return [imageBytes[start : start + lrecl] for start in range(0, len(imageBytes), lrecl)]


def ReadUsrsecPasswords() -> list[str]:
    """Decode SEC-USR-PWD from every USRSEC record independently of the reader under test."""
    imageBytes = (EBCDIC_DIR / USRSEC_IMAGE_NAME).read_bytes()
    records = [record.decode(CP037) for record in SplitFixedRecords(imageBytes, USRSEC_LRECL)]
    return [record[USRSEC_PASSWORD_SLICE].rstrip(" ") for record in records]


def IsTwoPlaceDecimal(value: object) -> bool:
    """Return True for a Decimal with exactly two fraction digits, as NUMERIC(n,2) stores it."""
    return isinstance(value, Decimal) and value.as_tuple().exponent == MONEY_EXPONENT


def AssertImageError(error: ImageFormatError, imagePath: Path, recordNumber: int) -> None:
    """Assert the error names the synthetic image and the record, with a non-empty reason."""
    assert error.path == imagePath
    assert error.recordNumber == recordNumber
    assert isinstance(error.reason, str)
    assert error.reason != ""


class FramingCase(NamedTuple):
    """A synthetic ASCII image that fails framing, and the record it must fail on."""

    layout: RecordLayout
    records: tuple[str, ...]
    tail: str
    recordNumber: int
    bareTerminator: bool


TYPE_RECORD_1 = SyntheticTypeRecord("01", "Purchase")
TYPE_RECORD_2 = SyntheticTypeRecord("02", "Payment")
STRAY_TERMINATOR_POSITION = 10
# The 36 field bytes of cardxref.txt record 1, in CVACT03Y offset order.
XREF_LINE_1 = "".join(EXPECTED_XREF_1.values())
# Short of the 36-byte FILLER-less length, cutting into XREF-ACCT-ID at offset 25.
SHORT_XREF_LENGTH = 30
# These cases use the real copybook layouts. test_image_reader_streaming.py covers the full
# framing matrix on a synthetic three-byte layout.
FRAMING_ERROR_CASES = (
    FramingCase(
        TRANSACTION_TYPE_LAYOUT,
        (TYPE_RECORD_1, ReplaceCharacter(TYPE_RECORD_2, STRAY_TERMINATOR_POSITION, "\r")),
        CRLF,
        2,
        True,
    ),
    FramingCase(
        TRANSACTION_TYPE_LAYOUT,
        (TYPE_RECORD_1, ReplaceCharacter(TYPE_RECORD_2, STRAY_TERMINATOR_POSITION, LF)),
        CRLF,
        2,
        True,
    ),
    FramingCase(TRANSACTION_TYPE_LAYOUT, (TYPE_RECORD_1, TYPE_RECORD_2 + "X"), CRLF, 2, False),
    FramingCase(CARD_XREF_LAYOUT, (XREF_LINE_1, XREF_LINE_1[:SHORT_XREF_LENGTH]), CRLF, 2, False),
    FramingCase(TRANSACTION_TYPE_LAYOUT, (TYPE_RECORD_1, TYPE_RECORD_2), CRLF + LF, 3, False),
)
FRAMING_ERROR_IDS = (
    "bare-cr-inside-record",
    "bare-lf-inside-record",
    "over-long-record",
    "short-record-not-filler-short",
    "second-final-terminator",
)
# Exactly one final terminator, CRLF or a lone LF, is removed at end of file.
FINAL_TERMINATORS = (CRLF, LF)


class ErrorTextCase(NamedTuple):
    """An ImageFormatError's path and reason, and the one-line message it must show for them."""

    path: str | Path
    reason: str
    shownText: str


ERROR_TEXT_RECORD_NUMBER = 2
# A line feed before forged log text and an ANSI escape in an operator-chosen image directory,
# and a carriage return and an escape in a reason quoting record text, are each shown as their
# escape, so neither splits the message or reaches a terminal.
FORGED_LOG_TEXT = "ERROR forged-seed-success"
HOSTILE_DIR_NAME = f"EBCDIC\n{FORGED_LOG_TEXT}\x1b[31m"
SHOWN_HOSTILE_DIR_NAME = f"EBCDIC\\n{FORGED_LOG_TEXT}\\x1b[31m"
HOSTILE_REASON = f"field text '12\r{FORGED_LOG_TEXT}\x1b[2J'"
SHOWN_HOSTILE_REASON = f"field text '12\\r{FORGED_LOG_TEXT}\\x1b[2J'"
USRSEC_IMAGE_PATH = EBCDIC_DIR / USRSEC_IMAGE_NAME
ERROR_TEXT_CASES = (
    pytest.param(
        ErrorTextCase(
            USRSEC_IMAGE_PATH,
            BARE_TERMINATOR_REASON,
            f"{USRSEC_IMAGE_PATH}: record {ERROR_TEXT_RECORD_NUMBER}: {BARE_TERMINATOR_REASON}",
        ),
        id="printable",
    ),
    pytest.param(
        ErrorTextCase(
            REPO_ROOT / "data" / HOSTILE_DIR_NAME / USRSEC_IMAGE_NAME,
            BARE_TERMINATOR_REASON,
            f"{REPO_ROOT / 'data'}/{SHOWN_HOSTILE_DIR_NAME}/{USRSEC_IMAGE_NAME}: "
            f"record {ERROR_TEXT_RECORD_NUMBER}: {BARE_TERMINATOR_REASON}",
        ),
        id="control-bearing-path",
    ),
    pytest.param(
        ErrorTextCase(
            str(USRSEC_IMAGE_PATH),
            HOSTILE_REASON,
            f"{USRSEC_IMAGE_PATH}: record {ERROR_TEXT_RECORD_NUMBER}: {SHOWN_HOSTILE_REASON}",
        ),
        id="control-bearing-reason",
    ),
)


@pytest.mark.parametrize("imageCase", IMAGE_CASES, ids=IMAGE_CASE_IDS)
def test_ebcdic_record_counts(imageCase):
    contents = ReadCase(imageCase, ENCODING_EBCDIC)
    assert isinstance(contents, ImageContents)
    assert (len(contents.records), contents.skippedCount) == (imageCase.expectedCount, 0)


@pytest.mark.parametrize("imageCase", ASCII_CASES, ids=ASCII_CASE_IDS)
def test_ascii_record_counts(imageCase):
    contents = ReadCase(imageCase, ENCODING_ASCII)
    assert (len(contents.records), contents.skippedCount) == (imageCase.expectedCount, 0)


@pytest.mark.parametrize("imageCase", IMAGE_CASES, ids=IMAGE_CASE_IDS)
def test_sample_image_paths(imageCase):
    names = SampleImageNames(imageCase.ebcdicName, imageCase.asciiName)
    assert SAMPLE_IMAGE_NAMES[imageCase.table] == names
    ebcdicChoice = SampleImagePath(RELATIVE_EBCDIC_DIR, imageCase.table, ENCODING_EBCDIC)
    assert ebcdicChoice == (EBCDIC_DIR / imageCase.ebcdicName, ENCODING_EBCDIC)
    asciiChoice = SampleImagePath(ASCII_DIR, imageCase.table, ENCODING_ASCII)
    if imageCase.asciiName is None:
        assert asciiChoice == (ASCII_DIR / imageCase.ebcdicName, ENCODING_EBCDIC)
    else:
        assert asciiChoice == (ASCII_DIR / imageCase.asciiName, ENCODING_ASCII)


def test_sample_image_names_cover_every_dataset():
    assert SAMPLE_IMAGE_NAMES["transactions"] == SampleImageNames(TRANFILE_IMAGE_NAME, None)
    assert set(SAMPLE_IMAGE_NAMES) == set(CASES_BY_TABLE) | {"transactions"}
    namedImages = {names.ebcdicName for names in SAMPLE_IMAGE_NAMES.values()}
    assert namedImages.isdisjoint(UNREAD_IMAGE_NAMES)


def test_tranfile_priming_record_is_skipped():
    imagePath = EBCDIC_DIR / TRANFILE_IMAGE_NAME
    imageBytes = imagePath.read_bytes()
    assert len(imageBytes) == TRANSACTION_LAYOUT.lrecl
    assert imageBytes[:PRIMING_ZERO_BYTES] == b"\x00" * PRIMING_ZERO_BYTES
    assert imageBytes[PRIMING_ZERO_BYTES:].decode(CP037) == PRIMING_TAIL_TEXT
    contents = ReadImage(imagePath, TRANSACTION_LAYOUT, ENCODING_EBCDIC, IsTranfilePrimingRecord)
    assert (contents.records, contents.skippedCount) == ((), 1)


def test_priming_record_is_unloadable_without_the_check():
    imagePath = EBCDIC_DIR / TRANFILE_IMAGE_NAME
    with pytest.raises(ImageFormatError) as raised:
        ReadImage(imagePath, TRANSACTION_LAYOUT, ENCODING_EBCDIC)
    AssertImageError(raised.value, imagePath, 1)


def test_priming_check_rejects_daily_record():
    dailyBytes = (EBCDIC_DIR / CASES_BY_TABLE["daily_transactions"].ebcdicName).read_bytes()
    assert not IsTranfilePrimingRecord(dailyBytes[: DAILY_TRANSACTION_LAYOUT.lrecl])
    assert IsTranfilePrimingRecord((EBCDIC_DIR / TRANFILE_IMAGE_NAME).read_bytes())


@pytest.mark.parametrize("loneLfCase", LONE_LF_CASES, ids=LONE_LF_IDS)
def test_lone_lf_final_record(loneLfCase):
    imageCase = CASES_BY_TABLE[loneLfCase.table]
    imageBytes = (ASCII_DIR / imageCase.asciiName).read_bytes()
    assert len(imageBytes) == loneLfCase.expectedSize
    assert imageBytes.endswith(LF.encode()) and not imageBytes.endswith(CRLF.encode())
    assert imageBytes.count(CRLF.encode()) == imageCase.expectedCount - 1
    asciiRecords = ReadCase(imageCase, ENCODING_ASCII).records
    assert len(asciiRecords) == imageCase.expectedCount
    assert asciiRecords == ReadCase(imageCase, ENCODING_EBCDIC).records


def test_cardxref_filler_padding():
    imageCase = CASES_BY_TABLE["card_xref"]
    asciiLines = (ASCII_DIR / imageCase.asciiName).read_bytes().decode("ascii").split(CRLF)
    assert asciiLines[-1] == ""
    assert {len(line) for line in asciiLines[:-1]} == {XREF_LINE_LENGTH}
    ebcdicBytes = (EBCDIC_DIR / imageCase.ebcdicName).read_bytes()
    ebcdicRecords = SplitFixedRecords(ebcdicBytes, CARD_XREF_LAYOUT.lrecl)
    fillers = {record[XREF_LINE_LENGTH:].decode(CP037) for record in ebcdicRecords}
    assert fillers == {XREF_FILLER_TEXT}
    asciiRecords = ReadCase(imageCase, ENCODING_ASCII).records
    assert asciiRecords[0] == EXPECTED_XREF_1
    assert asciiRecords == ReadCase(imageCase, ENCODING_EBCDIC).records


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_account_fields(encoding):
    record = ReadCase(CASES_BY_TABLE["accounts"], encoding).records[0]
    assert record == EXPECTED_ACCOUNT_1
    for column in ACCOUNT_MONEY_COLUMNS:
        assert IsTwoPlaceDecimal(record[column]), column


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_daily_transaction_fields(encoding):
    records = ReadCase(CASES_BY_TABLE["daily_transactions"], encoding).records
    assert records[0] == EXPECTED_DAILY_1
    assert IsTwoPlaceDecimal(records[0]["tran_amt"])
    timestampColumns = ("orig_ts", "orig_ts_format", "proc_ts", "proc_ts_format")
    timestamps = {tuple(record[column] for column in timestampColumns) for record in records}
    assert timestamps == {EXPECTED_DAILY_TIMESTAMPS}


def test_usrsec_users():
    records = ReadCase(CASES_BY_TABLE["users"], ENCODING_EBCDIC).records
    assert tuple(record["user_id"] for record in records) == EXPECTED_USER_IDS
    assert tuple(record["user_type"] for record in records) == EXPECTED_USER_TYPES
    passwords = [record["password"] for record in records]
    # Compared through booleans, so a failure never prints a password.
    everyPasswordPresent = all(password.strip() for password in passwords)
    passwordsMatchImage = passwords == ReadUsrsecPasswords()
    assert everyPasswordPresent
    assert passwordsMatchImage


def test_truncated_ebcdic_image(tmp_path):
    assert issubclass(ImageFormatError, ValueError)
    payload = TYPE_RECORD_1 + TYPE_RECORD_2 + TYPE_RECORD_1[:10]
    imagePath = WriteEbcdicImage(tmp_path, payload)
    assert imagePath.stat().st_size == 130
    with pytest.raises(ImageFormatError) as raised:
        ReadImage(imagePath, TRANSACTION_TYPE_LAYOUT, ENCODING_EBCDIC)
    AssertImageError(raised.value, imagePath, WHOLE_FILE_RECORD_NUMBER)


@pytest.mark.parametrize("framingCase", FRAMING_ERROR_CASES, ids=FRAMING_ERROR_IDS)
def test_ascii_framing_errors(tmp_path, framingCase):
    imagePath = WriteAsciiImage(tmp_path, framingCase.records, framingCase.tail)
    with pytest.raises(ImageFormatError) as raised:
        ReadImage(imagePath, framingCase.layout, ENCODING_ASCII)
    AssertImageError(raised.value, imagePath, framingCase.recordNumber)
    assert (raised.value.reason == BARE_TERMINATOR_REASON) is framingCase.bareTerminator


@pytest.mark.parametrize("tail", FINAL_TERMINATORS, ids=("crlf", "lone-lf"))
def test_one_final_terminator_is_removed(tmp_path, tail):
    imagePath = WriteAsciiImage(tmp_path, (TYPE_RECORD_1, TYPE_RECORD_2), tail)
    records = ReadImage(imagePath, TRANSACTION_TYPE_LAYOUT, ENCODING_ASCII).records
    expectedTypes = [{"tran_type": "01", "description": "Purchase"}]
    expectedTypes.append({"tran_type": "02", "description": "Payment"})
    assert list(records) == expectedTypes


def test_unknown_timestamp_text(tmp_path):
    dailyName = CASES_BY_TABLE["daily_transactions"].asciiName
    dailyLine = (ASCII_DIR / dailyName).read_bytes().decode("ascii").split(CRLF, 1)[0]
    assert len(dailyLine) == DAILY_TRANSACTION_LAYOUT.lrecl
    unknownText = UNKNOWN_TIMESTAMP_TEXT.ljust(TIMESTAMP_LENGTH)
    badLine = dailyLine[:ORIG_TS_OFFSET] + unknownText
    badLine += dailyLine[ORIG_TS_OFFSET + TIMESTAMP_LENGTH :]
    imagePath = WriteAsciiImage(tmp_path, (dailyLine, badLine), CRLF)
    with pytest.raises(ImageFormatError) as raised:
        ReadImage(imagePath, DAILY_TRANSACTION_LAYOUT, ENCODING_ASCII)
    AssertImageError(raised.value, imagePath, 2)
    fieldError = raised.value.__cause__
    assert isinstance(fieldError, RecordFormatError)
    assert fieldError.fieldName == "orig_ts"
    assert isinstance(fieldError.__cause__, TimestampFormatError)


@pytest.mark.parametrize("errorTextCase", ERROR_TEXT_CASES)
def test_image_error_message_is_one_escaped_line(errorTextCase):
    error = ImageFormatError(errorTextCase.path, ERROR_TEXT_RECORD_NUMBER, errorTextCase.reason)
    shownText = str(error)
    assert shownText == errorTextCase.shownText
    assert len(shownText.splitlines()) == 1
    assert "\x1b" not in shownText
    assert error.path is errorTextCase.path
    assert (error.recordNumber, error.reason) == (ERROR_TEXT_RECORD_NUMBER, errorTextCase.reason)
