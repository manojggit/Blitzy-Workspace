"""Unit tests for app.utils.date_validation, the port of program CSUTLDTC and copybooks
CSUTLDPY and CSUTLDWY (the CSUTLDTC and CSUTLDPY edits and their date-message builders).

Expected values come from the cited COBOL lines, never from the unit's own output:
- cbl/CSUTLDTC.cbl: the CEEDAYS feedback tokens (lines 62-70: X'0003 09CB' is severity 3 and
  message 2507, X'0003 09CC' is 2508, X'0003 09D1' is 2513, X'0003 09D5' is 2517, X'0003 09D6'
  is 2518, X'0003 09D8' is 2520), the WS-RESULT texts (lines 128-149) and the 80-byte
  WS-MESSAGE area (lines 42-57).
- cpy/CSUTLDPY.cpy lines 18-372: EDIT-DATE-CCYYMMDD through EDIT-DATE-OF-BIRTH, each suffix
  at the line its table row cites. COACTUPC performs the paragraphs as one PERFORM THRU
  range, so a GO TO <paragraph>-EXIT falls into the next edit and IF WS-RETURN-MSG-OFF keeps
  only the first suffix. The unit returns the suffix without the field-name prefix. EDIT-DAY
  stores NUMVAL of the day (lines 170-173) into the unsigned 9(2) WS-EDIT-DATE-DD-N, so " 5",
  "5 " and "-5" become day 05 and "00" or ".5" fails WS-VALID-DAY (lines 187-199).
- cpy/CSUTLDWY.cpy: the 19 and 20 century 88-levels (lines 9-10), the day REDEFINES and
  WS-VALID-DAY (lines 25-29) and the part flags (43-57).
- cbl/COACTUPC.cbl lines 1536-1543 perform EDIT-DATE-OF-BIRTH only when the CCYYMMDD flags
  are valid; the unit refuses any other date with ValueError.

EDIT-DATE-OF-BIRTH (lines 341-372) moves only the calendar date of FUNCTION CURRENT-DATE, and
the date of birth is valid only when today is later. Otherwise INPUT-ERROR is set, all three
part flags are NOT-OK and the suffix is ':cannot be in the future ' (line 363). today may be a
datetime such as datetime.now(), the online programs' clock, whose time of day never counts.
"""

import re
from datetime import date, datetime

import pytest

from app.utils import date_validation
from app.utils.date_validation import (
    CEEDAYS_MSG_UNSUPPORTED_RANGE,
    CheckDate,
    DateCheckResult,
    DatePartStatus,
    EditDateCcyymmdd,
    EditDateOfBirth,
    FormatDateCheckResult,
)

# (dateText, mask, (severity, messageNumber, WS-RESULT text)), cbl/CSUTLDTC.cbl lines 62-70
# and 128-149. YYYYMMDD is the mask CSUTLDPY passes (line 291).
CHECK_DATE_CASES = [
    ("2022-07-18", "YYYY-MM-DD", (0, 0, "Date is valid")),
    ("20240229", "YYYYMMDD", (0, 0, "Date is valid")),
    ("2022-02-30", "YYYY-MM-DD", (3, 2508, "Datevalue error")),
    ("1500-01-01", "YYYY-MM-DD", (3, 2513, "Unsupp. Range")),
    ("2022-AB-01", "YYYY-MM-DD", (3, 2520, "Nonnumeric data")),
    # FC-INSUFFICIENT-DATA, lines 63 and 131-132: the right-trimmed date is shorter than the
    # mask. LS-DATE X(10) (line 84) space-pads a short date, and the padding is trimmed.
    ("2022-07", "YYYY-MM-DD", (3, 2507, "Insufficient")),
    ("2022-07   ", "YYYY-MM-DD", (3, 2507, "Insufficient")),
    ("2022071", "YYYYMMDD", (3, 2507, "Insufficient")),
    # FC-INVALID-MONTH, lines 67 and 139-140: a month outside 1-12.
    ("2022-13-01", "YYYY-MM-DD", (3, 2517, "Invalid month")),
    ("20220018", "YYYYMMDD", (3, 2517, "Invalid month")),
    # FC-BAD-PIC-STRING, lines 68 and 141-142: a valid date with a mask of no date tokens.
    ("2022-07-18", "XXXXXXXXXX", (3, 2518, "Bad Pic String")),
]
CHECK_DATE_IDS = [
    "iso-valid",
    "ccyymmdd-leap-day-valid",
    "fc-bad-date-value",
    "fc-unsupp-range-before-lilian-day-one",
    "fc-non-numeric-data",
    "fc-insufficient-data-iso-short",
    "fc-insufficient-data-iso-space-padded",
    "fc-insufficient-data-ccyymmdd-short",
    "fc-invalid-month-iso-13",
    "fc-invalid-month-ccyymmdd-00",
    "fc-bad-pic-string",
]

# WS-MESSAGE, cbl/CSUTLDTC.cbl lines 42-57, for CheckDate("2022-07-18", "YYYY-MM-DD"):
# (start, end, text) per field.
VALID_AREA_SLICES = (
    (0, 4, "0000"),  # WS-SEVERITY X(04)
    (4, 15, "Mesg Code: "),  # FILLER X(11) VALUE 'Mesg Code:'
    (15, 19, "0000"),  # WS-MSG-NO X(04)
    (19, 20, " "),  # FILLER X(01)
    (20, 35, "Date is valid  "),  # WS-RESULT X(15)
    (35, 36, " "),  # FILLER X(01)
    (36, 45, "TstDate: "),  # FILLER X(09) VALUE 'TstDate:'
    # WS-DATE X(10) holds the tested date text. Line 122 moves the whole WS-DATE-TO-TEST group,
    # binary Vstring-length first, but no caller reads past WS-MSG-NO (COTRN02C, CORPT00C and
    # CSUTLDPY test only the severity and message number), so the port shows the date itself.
    (45, 55, "2022-07-18"),
    (55, 56, " "),  # FILLER X(01)
    (56, 66, "Mask used:"),  # FILLER X(10) VALUE 'Mask used:'
    (66, 76, "YYYY-MM-DD"),  # WS-DATE-FMT X(10)
    (76, 80, "    "),  # FILLER X(01) and FILLER X(03)
)
# The fields CheckDate("2022-02-30", "YYYY-MM-DD") sets: FC-BAD-DATE-VALUE, line 133.
FAILURE_AREA_SLICES = (
    (0, 4, "0003"),
    (15, 19, "2508"),
    (20, 35, "Datevalue error"),
)

CENTURY_INVALID_SUFFIX = " : Century is not valid."  # line 79
NOT_LEAP_YEAR_SUFFIX = ":Not a leap year.Cannot have 29 days in this month."  # line 266

# (WS-EDIT-DATE-CCYYMMDD, expected suffix), cpy/CSUTLDPY.cpy at the cited STRING literal.
MESSAGE_SUFFIX_CASES = [
    ("    0718", " : Year must be supplied."),  # line 37
    ("20AB0718", " must be 4 digit number."),  # line 54
    ("21990718", CENTURY_INVALID_SUFFIX),
    ("18990718", CENTURY_INVALID_SUFFIX),
    ("2022  18", " : Month must be supplied."),  # line 101
    ("20221318", ": Month must be a number between 1 and 12."),  # line 119
    ("202207  ", " : Day must be supplied."),  # line 161
    ("202207AB", ":day must be a number between 1 and 31."),  # line 180
    ("20220732", ":day must be a number between 1 and 31."),  # line 195
    ("20220431", ":Cannot have 31 days in this month."),  # line 221
    ("20220230", ":Cannot have 30 days in this month."),  # line 236
    ("20230229", NOT_LEAP_YEAR_SUFFIX),
    ("19000229", NOT_LEAP_YEAR_SUFFIX),
]
MESSAGE_SUFFIX_IDS = [
    "year-blank",
    "year-not-numeric",
    "century-21",
    "century-18",
    "month-blank",
    "month-13",
    "day-blank",
    "day-not-numeric",
    "day-32",
    "april-31",
    "february-30",
    "2023-not-divisible-by-4",
    "1900-yy-00-not-divisible-by-400",
]

# WS-EDIT-DATE-CCYYMMDD values whose day passes TEST-NUMVAL (CSUTLDPY line 170) and stores 5:
# COMPUTE WS-EDIT-DATE-DD-N = NUMVAL (lines 171-173) into the unsigned 9(2) REDEFINES
# (CSUTLDWY lines 25-29) drops the sign, so WS-EDIT-DATE-CCYYMMDD then reads 20220705.
NORMALIZED_DAY_TEXTS = ["202207 5", "2022075 ", "202207-5"]
NORMALIZED_DAY_IDS = ["day-leading-space", "day-trailing-space", "day-leading-minus"]

# Days that pass TEST-NUMVAL but store a value outside 88 WS-VALID-DAY 1 THROUGH 31: NUMVAL 0,
# and NUMVAL 0.5 truncated to 0 by the 9(2) receiver (CSUTLDPY lines 187-199).
OUT_OF_RANGE_DAY_TEXTS = ["20220700", "202207.5"]
OUT_OF_RANGE_DAY_IDS = ["day-zero", "day-fraction"]

VALID_DATE_TEXTS = ["20240229", "20000229", "20220718", *NORMALIZED_DAY_TEXTS]
VALID_DATE_IDS = [
    "2024-divisible-by-4",
    "2000-yy-00-divisible-by-400",
    "ordinary-date",
    *NORMALIZED_DAY_IDS,
]

# Dates EDIT-DATE-CCYYMMDD leaves with a part flag not valid, so COACTUPC never performs
# EDIT-DATE-OF-BIRTH on them (cbl/COACTUPC.cbl lines 1536-1543).
UNEDITED_BIRTH_DATE_TEXTS = ["20221318", "    0718", "20230229", "20220700"]
UNEDITED_BIRTH_DATE_IDS = ["month-13", "year-blank", "not-leap-year", "day-zero"]
BIRTH_DATE_REFUSAL_MESSAGE = (
    "EDIT-DATE-OF-BIRTH needs a date whose EDIT-DATE-CCYYMMDD flags are all valid"
)

# (WS-EDIT-DATE-CCYYMMDD, DateEditResult attribute, expected flag): a part edit that fails on
# blank sets FLG-*-BLANK and any other failure FLG-*-NOT-OK; the 31-day check sets only the
# day and month flags (lines 216-217); the leap check sets all three (lines 260-262).
PART_STATUS_CASES = [
    ("    0718", "yearStatus", DatePartStatus.BLANK),
    ("20AB0718", "yearStatus", DatePartStatus.NOT_OK),
    ("2022  18", "monthStatus", DatePartStatus.BLANK),
    ("20221318", "monthStatus", DatePartStatus.NOT_OK),
    ("202207  ", "dayStatus", DatePartStatus.BLANK),
    ("202207AB", "dayStatus", DatePartStatus.NOT_OK),
    ("20220732", "dayStatus", DatePartStatus.NOT_OK),
    ("20220431", "dayStatus", DatePartStatus.NOT_OK),
    ("20220431", "monthStatus", DatePartStatus.NOT_OK),
    ("20220431", "yearStatus", DatePartStatus.VALID),
    ("20230229", "yearStatus", DatePartStatus.NOT_OK),
]
PART_STATUS_IDS = [
    "year-blank",
    "year-not-numeric",
    "month-blank",
    "month-13",
    "day-blank",
    "day-not-numeric",
    "day-32",
    "april-31-day",
    "april-31-month",
    "april-31-year-stays-valid",
    "not-leap-year-year",
]

ALL_PARTS_VALID = (DatePartStatus.VALID, DatePartStatus.VALID, DatePartStatus.VALID)

TODAY = date(2024, 3, 15)
FUTURE_DATE_MESSAGE = ":cannot be in the future "


@pytest.mark.parametrize("dateText, mask, expected", CHECK_DATE_CASES, ids=CHECK_DATE_IDS)
def test_check_date_feedback(dateText, mask, expected):
    result = CheckDate(dateText, mask)
    assert (result.severity, result.messageNumber, result.resultText.rstrip()) == expected


def test_unsupported_range_constant():
    """COTRN02C and CORPT00C accept CEEDAYS message 2513 (COTRN02C lines 400 and 420)."""
    assert CEEDAYS_MSG_UNSUPPORTED_RANGE == 2513


def test_format_date_check_result_area_layout():
    area = FormatDateCheckResult(CheckDate("2022-07-18", "YYYY-MM-DD"))
    assert len(area) == 80
    actualSlices = tuple((start, end, area[start:end]) for start, end, _ in VALID_AREA_SLICES)
    assert actualSlices == VALID_AREA_SLICES


def test_format_date_check_result_failure_codes():
    area = FormatDateCheckResult(CheckDate("2022-02-30", "YYYY-MM-DD"))
    assert len(area) == 80
    actualSlices = tuple((start, end, area[start:end]) for start, end, _ in FAILURE_AREA_SLICES)
    assert actualSlices == FAILURE_AREA_SLICES


@pytest.mark.parametrize("dateText, expectedSuffix", MESSAGE_SUFFIX_CASES, ids=MESSAGE_SUFFIX_IDS)
def test_edit_date_message_suffix(dateText, expectedSuffix):
    """1900 has YY = 00, so CCYY is divided by 400, not 4 (CSUTLDPY lines 245-256)."""
    result = EditDateCcyymmdd(dateText)
    assert result.messageSuffix == expectedSuffix
    assert result.inputError is True


@pytest.mark.parametrize("dateText", VALID_DATE_TEXTS, ids=VALID_DATE_IDS)
def test_edit_date_accepts_valid_dates(dateText):
    result = EditDateCcyymmdd(dateText)
    assert result.messageSuffix == ""
    assert result.inputError is False
    assert (result.yearStatus, result.monthStatus, result.dayStatus) == ALL_PARTS_VALID


@pytest.mark.parametrize(
    "dateText, partName, expectedStatus", PART_STATUS_CASES, ids=PART_STATUS_IDS
)
def test_edit_date_part_status(dateText, partName, expectedStatus):
    result = EditDateCcyymmdd(dateText)
    assert getattr(result, partName) is expectedStatus


def test_edit_date_keeps_first_message_and_still_edits_later_parts():
    """GO TO EDIT-YEAR-CCYY-EXIT (CSUTLDPY line 42) falls through into EDIT-MONTH and EDIT-DAY.

    Both still set their flags, while IF WS-RETURN-MSG-OFF (lines 116 and 158) keeps the year's
    message as the only one.
    """
    result = EditDateCcyymmdd("    13  ")
    assert result.messageSuffix == " : Year must be supplied."
    assert result.inputError is True
    assert result.yearStatus is DatePartStatus.BLANK
    assert result.monthStatus is DatePartStatus.NOT_OK
    assert result.dayStatus is DatePartStatus.BLANK


def test_edit_date_reports_le_validation_error(monkeypatch):
    """EDIT-DATE-LE (CSUTLDPY lines 284-327) with a failing CSUTLDTC substituted.

    The call passes WS-EDIT-DATE-CCYYMMDD with the mask 'YYYYMMDD' (lines 291-296). A non-zero
    severity sets INPUT-ERROR and builds the suffix from the 4-digit severity and message
    number (lines 305-313). Its GO TO EDIT-DATE-LE-EXIT then falls into SET
    WS-EDIT-DATE-IS-VALID TO TRUE (line 327), so the part flags that lines 302-304 set NOT-OK
    end VALID, while INPUT-ERROR and the suffix stand.
    """
    observedCalls = []

    def FailingCheckDate(dateText, mask):
        observedCalls.append((dateText, mask))
        return DateCheckResult(
            severity=3,
            messageNumber=2508,
            resultText="Datevalue error",
            testedDate=dateText,
            maskUsed=mask,
        )

    monkeypatch.setattr(date_validation, "CheckDate", FailingCheckDate)
    result = date_validation.EditDateCcyymmdd("20220718")
    assert observedCalls == [("20220718", "YYYYMMDD")]
    assert result.messageSuffix == " validation error Sev code: 0003 Message code: 2508"
    assert result.inputError is True
    assert (result.yearStatus, result.monthStatus, result.dayStatus) == ALL_PARTS_VALID


def InstallRecordingCheckDate(monkeypatch):
    """Substitute CSUTLDTC with a fake that records each (dateText, mask) it is called with.

    The fake returns FC-INVALID-DATE, severity 0 and 'Date is valid' (cbl/CSUTLDTC.cbl lines
    129-130). Returns the list the calls are recorded in.
    """
    observedCalls = []

    def ValidCheckDate(dateText, mask):
        observedCalls.append((dateText, mask))
        return DateCheckResult(
            severity=0,
            messageNumber=0,
            resultText="Date is valid",
            testedDate=dateText,
            maskUsed=mask,
        )

    monkeypatch.setattr(date_validation, "CheckDate", ValidCheckDate)
    return observedCalls


@pytest.mark.parametrize("dateText", NORMALIZED_DAY_TEXTS, ids=NORMALIZED_DAY_IDS)
def test_edit_date_le_receives_normalized_day(dateText, monkeypatch):
    """EDIT-DATE-LE passes WS-EDIT-DATE-CCYYMMDD after the day COMPUTE (CSUTLDPY lines 291-296).

    The COMPUTE (lines 171-173) rewrote WS-EDIT-DATE-DD as 05, so CSUTLDTC receives 20220705.
    """
    observedCalls = InstallRecordingCheckDate(monkeypatch)
    result = date_validation.EditDateCcyymmdd(dateText)
    assert observedCalls == [("20220705", "YYYYMMDD")]
    assert result.messageSuffix == ""
    assert result.inputError is False
    assert (result.yearStatus, result.monthStatus, result.dayStatus) == ALL_PARTS_VALID


@pytest.mark.parametrize("dateText", OUT_OF_RANGE_DAY_TEXTS, ids=OUT_OF_RANGE_DAY_IDS)
def test_edit_date_rejects_numeric_day_outside_valid_days(dateText, monkeypatch):
    """NOT WS-VALID-DAY sets INPUT-ERROR and the day NOT-OK (CSUTLDPY lines 187-199).

    The year and month stay valid, so EDIT-DAY-MONTH-YEAR finds no combination error and its
    IF WS-EDIT-DATE-IS-VALID ... ELSE GO TO EDIT-DATE-CCYYMMDD-EXIT (lines 274-277) skips
    EDIT-DATE-LE: CSUTLDTC is never called.
    """
    observedCalls = InstallRecordingCheckDate(monkeypatch)
    result = date_validation.EditDateCcyymmdd(dateText)
    assert result.messageSuffix == ":day must be a number between 1 and 31."  # line 195
    assert result.inputError is True
    assert result.yearStatus is DatePartStatus.VALID
    assert result.monthStatus is DatePartStatus.VALID
    assert result.dayStatus is DatePartStatus.NOT_OK
    assert observedCalls == []


@pytest.mark.parametrize(
    "dateText, today",
    [
        ("20240315", TODAY),
        ("20240316", TODAY),
        ("20240315", datetime(2024, 3, 15, 10, 30)),
        ("20240316", datetime(2024, 3, 15, 23, 59, 59)),
        # INTEGER-OF-DATE reads WS-EDIT-DATE-CCYYMMDD-N (CSUTLDPY line 346) after EDIT-DAY's
        # COMPUTE stored the day as 05, so both texts are the date 2024-03-05.
        ("202403 5", date(2024, 3, 5)),
        ("202403-5", date(2024, 3, 5)),
    ],
    ids=[
        "today",
        "tomorrow",
        "today-datetime-after-midnight",
        "tomorrow-datetime-end-of-day",
        "today-day-leading-space",
        "today-day-leading-minus",
    ],
)
def test_birth_date_today_or_later_is_in_the_future(dateText, today):
    result = EditDateOfBirth(dateText, today)
    assert result.yearStatus is DatePartStatus.NOT_OK
    assert result.monthStatus is DatePartStatus.NOT_OK
    assert result.dayStatus is DatePartStatus.NOT_OK
    assert result.inputError is True
    assert result.messageSuffix == FUTURE_DATE_MESSAGE
    assert result.isValid is False


@pytest.mark.parametrize(
    "dateText, today",
    [
        ("20240314", TODAY),
        ("20000101", datetime(2024, 1, 1)),
        ("20240314", datetime(2024, 3, 15, 10, 30)),
    ],
    ids=["yesterday", "year-2000-datetime-midnight", "yesterday-datetime-after-midnight"],
)
def test_birth_date_before_today_is_valid(dateText, today):
    result = EditDateOfBirth(dateText, today)
    assert result.yearStatus is DatePartStatus.VALID
    assert result.monthStatus is DatePartStatus.VALID
    assert result.dayStatus is DatePartStatus.VALID
    assert result.inputError is False
    assert result.messageSuffix == ""
    assert result.isValid is True


@pytest.mark.parametrize("dateText", UNEDITED_BIRTH_DATE_TEXTS, ids=UNEDITED_BIRTH_DATE_IDS)
def test_birth_date_edit_refuses_date_with_invalid_flags(dateText):
    """COACTUPC performs EDIT-DATE-OF-BIRTH only IF WS-EDIT-DT-OF-BIRTH-ISVALID (lines 1539-1543).

    The unit refuses such a date instead of comparing it, and its message never echoes the date.
    """
    with pytest.raises(ValueError, match=re.escape(BIRTH_DATE_REFUSAL_MESSAGE)) as raisedError:
        EditDateOfBirth(dateText, TODAY)
    assert dateText.strip() not in str(raisedError.value)
