"""Replaces: CSUTLDTC (CEEDAYS wrapper), copybooks CSUTLDPY (EDIT-DATE-CCYYMMDD, EDIT-YEAR-CCYY,
EDIT-MONTH, EDIT-DAY, EDIT-DAY-MONTH-YEAR, EDIT-DATE-LE, EDIT-DATE-OF-BIRTH) and CSUTLDWY (working
fields and flags).

CheckDate is the CSUTLDTC call: COTRN02C and CORPT00C pass a 10-character date with the mask
YYYY-MM-DD and test only the severity and the message number (2513, unsupported range, is
accepted). It is this port's fixed reading of Language Environment CEEDAYS for the two masks the
callers use. FormatDateCheckResult renders the 80-byte WS-MESSAGE area CSUTLDTC returns.

EditDateCcyymmdd is COACTUPC's PERFORM EDIT-DATE-CCYYMMDD THRU EDIT-DATE-CCYYMMDD-EXIT. The
paragraphs run as one PERFORM THRU range, so a GO TO <paragraph>-EXIT inside the year, month or
day edit falls through into the next paragraph: all three part edits and the combination edit
always run, and only the first suffix is kept (the WS-RETURN-MSG-OFF rule). A suffix is returned
without the field name; the service prepends TRIM(WS-EDIT-VARIABLE-NAME), keeps the request's
first message and fits it to the 75-byte WS-RETURN-MSG. An empty suffix is WS-RETURN-MSG-OFF.

Two source quirks are ported unchanged. EDIT-DAY sets the day flag valid before editing it. After
an EDIT-DATE-LE failure, GO TO EDIT-DATE-LE-EXIT lands in the paragraph that ends with SET
WS-EDIT-DATE-IS-VALID TO TRUE, so the message and inputError stand while all three flags read
VALID. Dates that pass the CSUTLDPY edits (1900-2099, calendar-valid) always pass CheckDate, so
that path is defined but unreachable without a substituted CheckDate.

No function reads the clock or the settings: EditDateOfBirth takes today's date as a parameter.
"""

import calendar
import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from app.utils.cobol_intrinsics import Numval, TestNumval
from app.utils.decimal_utils import StoreInto

# CEEDAYS feedback tokens tested by CSUTLDTC (cbl/CSUTLDTC.cbl lines 62-70). Every error token
# carries severity 3; the message number is the token's second halfword.
CEEDAYS_VALID_SEVERITY = 0
CEEDAYS_ERROR_SEVERITY = 3
CEEDAYS_MSG_DATE_VALID = 0
CEEDAYS_MSG_INSUFFICIENT = 2507
CEEDAYS_MSG_BAD_DATE_VALUE = 2508
CEEDAYS_MSG_INVALID_ERA = 2509
CEEDAYS_MSG_UNSUPPORTED_RANGE = 2513
CEEDAYS_MSG_INVALID_MONTH = 2517
CEEDAYS_MSG_BAD_PIC_STRING = 2518
CEEDAYS_MSG_NON_NUMERIC = 2520
CEEDAYS_MSG_YEAR_IN_ERA_ZERO = 2521

# WS-RESULT PIC X(15), set by the EVALUATE at cbl/CSUTLDTC.cbl lines 128-148. Messages 2509 and
# 2521 belong to era pictures and the WHEN OTHER text to unknown tokens; no caller's mask
# produces them, so they are defined for completeness and never returned.
RESULT_TEXT_LENGTH = 15
CEEDAYS_RESULT_TEXTS: dict[int, str] = {
    messageNumber: resultText.ljust(RESULT_TEXT_LENGTH)
    for messageNumber, resultText in (
        (CEEDAYS_MSG_DATE_VALID, "Date is valid"),
        (CEEDAYS_MSG_INSUFFICIENT, "Insufficient"),
        (CEEDAYS_MSG_BAD_DATE_VALUE, "Datevalue error"),
        (CEEDAYS_MSG_INVALID_ERA, "Invalid Era"),
        (CEEDAYS_MSG_UNSUPPORTED_RANGE, "Unsupp. Range"),
        (CEEDAYS_MSG_INVALID_MONTH, "Invalid month"),
        (CEEDAYS_MSG_BAD_PIC_STRING, "Bad Pic String"),
        (CEEDAYS_MSG_NON_NUMERIC, "Nonnumeric data"),
        (CEEDAYS_MSG_YEAR_IN_ERA_ZERO, "YearInEra is 0"),
    )
}
CEEDAYS_RESULT_OTHER_TEXT = "Date is invalid"

# The masks the callers pass: COTRN02C and CORPT00C WS-DATE-FORMAT, and CSUTLDPY line 291.
ISO_DATE_MASK = "YYYY-MM-DD"
CCYYMMDD_DATE_MASK = "YYYYMMDD"
SUPPORTED_DATE_MASKS = frozenset({ISO_DATE_MASK, CCYYMMDD_DATE_MASK})
MASK_DIGIT_CHARACTERS = frozenset("YMD")
# Lilian day 1, the first day CEEDAYS converts; earlier dates are an unsupported range.
LILIAN_DAY_ONE: tuple[int, int, int] = (1582, 10, 15)

# WS-MESSAGE fixed texts and widths, cbl/CSUTLDTC.cbl lines 42-57 (80 bytes in total).
MESSAGE_CODE_LABEL = "Mesg Code:".ljust(11)
TESTED_DATE_LABEL = "TstDate:".ljust(9)
MASK_USED_LABEL = "Mask used:"
DATE_FIELD_LENGTH = 10
MASK_FIELD_LENGTH = 10
# The two trailing FILLERs, X(01) and X(03), both spaces.
MESSAGE_TRAILER = " " * 4
CODE_DIGITS = 4

# Message suffixes, byte for byte from cpy/CSUTLDPY.cpy (line numbers of each STRING literal).
YEAR_REQUIRED_SUFFIX = " : Year must be supplied."  # line 37
YEAR_NOT_NUMERIC_SUFFIX = " must be 4 digit number."  # line 54
CENTURY_INVALID_SUFFIX = " : Century is not valid."  # line 79
MONTH_REQUIRED_SUFFIX = " : Month must be supplied."  # line 101
MONTH_INVALID_SUFFIX = ": Month must be a number between 1 and 12."  # lines 119, 136
DAY_REQUIRED_SUFFIX = " : Day must be supplied."  # line 161
DAY_INVALID_SUFFIX = ":day must be a number between 1 and 31."  # lines 180, 195
NO_31_DAYS_SUFFIX = ":Cannot have 31 days in this month."  # line 221
NO_30_DAYS_SUFFIX = ":Cannot have 30 days in this month."  # line 236
NOT_LEAP_YEAR_SUFFIX = ":Not a leap year.Cannot have 29 days in this month."  # line 266
FUTURE_DATE_SUFFIX = ":cannot be in the future "  # line 363, trailing space kept
LE_ERROR_SEVERITY_LABEL = " validation error Sev code: "  # line 308
LE_ERROR_MESSAGE_LABEL = " Message code: "  # line 310

# WS-EDIT-DATE-CCYYMMDD layout and the CSUTLDWY 88-level values the edits test.
EDIT_DATE_LENGTH = 8
VALID_CENTURIES = frozenset({"19", "20"})  # 88 LAST-CENTURY, THIS-CENTURY
VALID_MONTHS = range(1, 13)  # 88 WS-VALID-MONTH
VALID_DAYS = range(1, 32)  # 88 WS-VALID-DAY
THIRTY_ONE_DAY_MONTHS = frozenset({1, 3, 5, 7, 8, 10, 12})  # 88 WS-31-DAY-MONTH
FEBRUARY = 2  # 88 WS-FEBRUARY
THIRTY_FIRST_DAY = 31  # 88 WS-DAY-31
THIRTIETH_DAY = 30  # 88 WS-DAY-30
LEAP_DAY = 29  # 88 WS-DAY-29
LEAP_DIVISOR_CENTURY_YEAR = 400
LEAP_DIVISOR = 4

# [0-9] rather than str.isdigit(), which also accepts non-ASCII digits a 3270 field cannot carry.
ASCII_DIGITS_PATTERN = re.compile(r"[0-9]+")
LOW_VALUE = "\x00"


@dataclass(frozen=True)
class DateCheckResult:
    """The CSUTLDTC result: CEEDAYS severity and message number, and the WS-RESULT text.

    testedDate and maskUsed are the WS-DATE and WS-DATE-FMT parts of the 80-byte area.
    """

    severity: int
    messageNumber: int
    resultText: str
    testedDate: str = ""
    maskUsed: str = ""


def CheckDate(dateText: str, mask: str) -> DateCheckResult:
    """Validate dateText against mask as CSUTLDTC's CALL "CEEDAYS" does (lines 103-151).

    Supported masks are YYYY-MM-DD and YYYYMMDD after right-trimming spaces. The first failure
    is returned with severity 3: 2518 for any other mask; 2507 when the right-trimmed date is
    shorter than the mask; 2520 when it is longer, a separator differs or a Y/M/D position is
    not 0-9; 2517 for a month outside 1-12; 2508 for day 0 or a day beyond the month's length
    (proleptic Gregorian); 2513 for a date before 1582-10-15, year 0000 included. Otherwise the
    severity and message number are 0 and the text is "Date is valid".
    """
    messageNumber = _CeedaysMessageNumber(dateText.rstrip(" "), mask.rstrip(" "))
    severity = CEEDAYS_ERROR_SEVERITY
    if messageNumber == CEEDAYS_MSG_DATE_VALID:
        severity = CEEDAYS_VALID_SEVERITY
    return DateCheckResult(
        severity=severity,
        messageNumber=messageNumber,
        resultText=CEEDAYS_RESULT_TEXTS[messageNumber],
        testedDate=dateText,
        maskUsed=mask,
    )


def FormatDateCheckResult(result: DateCheckResult) -> str:
    """Return the 80-character WS-MESSAGE area CSUTLDTC moves to LS-RESULT (lines 42-57, 97).

    Characters 0-3 hold the severity and 15-18 the message number, the two fields COTRN02C and
    CORPT00C compare (CSUTLDTC-RESULT-SEV-CD and CSUTLDTC-RESULT-MSG-NUM).
    """
    areaParts = (
        _FourDigitText(result.severity),
        MESSAGE_CODE_LABEL,
        _FourDigitText(result.messageNumber),
        " ",
        _FitText(result.resultText, RESULT_TEXT_LENGTH),
        " ",
        TESTED_DATE_LABEL,
        _FitText(result.testedDate, DATE_FIELD_LENGTH),
        " ",
        MASK_USED_LABEL,
        _FitText(result.maskUsed, MASK_FIELD_LENGTH),
        MESSAGE_TRAILER,
    )
    return "".join(areaParts)


def BuildLeValidationSuffix(severity: int, messageNumber: int) -> str:
    """Return the EDIT-DATE-LE message suffix built at CSUTLDPY lines 306-313.

    WS-SEVERITY and WS-MSG-NO are the 4-digit display forms of the CEEDAYS codes.
    """
    return (
        LE_ERROR_SEVERITY_LABEL
        + _FourDigitText(severity)
        + LE_ERROR_MESSAGE_LABEL
        + _FourDigitText(messageNumber)
    )


def _CeedaysMessageNumber(trimmedDate: str, trimmedMask: str) -> int:
    """Return the CEEDAYS message number for a right-trimmed date and mask, 0 when valid."""
    if trimmedMask not in SUPPORTED_DATE_MASKS:
        return CEEDAYS_MSG_BAD_PIC_STRING
    if len(trimmedDate) < len(trimmedMask):
        return CEEDAYS_MSG_INSUFFICIENT
    if not _MatchesMask(trimmedDate, trimmedMask):
        return CEEDAYS_MSG_NON_NUMERIC
    year, month, day = _DateParts(trimmedDate, trimmedMask)
    if month not in VALID_MONTHS:
        return CEEDAYS_MSG_INVALID_MONTH
    if day < 1 or day > calendar.monthrange(year, month)[1]:
        return CEEDAYS_MSG_BAD_DATE_VALUE
    if (year, month, day) < LILIAN_DAY_ONE:
        return CEEDAYS_MSG_UNSUPPORTED_RANGE
    return CEEDAYS_MSG_DATE_VALID


def _MatchesMask(dateText: str, mask: str) -> bool:
    """True when dateText has the mask's length, ASCII digits at Y/M/D and its separators."""
    if len(dateText) != len(mask):
        return False
    return all(
        ASCII_DIGITS_PATTERN.fullmatch(dateCharacter) is not None
        if maskCharacter in MASK_DIGIT_CHARACTERS
        else dateCharacter == maskCharacter
        for dateCharacter, maskCharacter in zip(dateText, mask, strict=True)
    )


def _DateParts(dateText: str, mask: str) -> tuple[int, int, int]:
    """Return (year, month, day) from a date already matched against its mask."""
    yearStart = mask.index("YYYY")
    monthStart = mask.index("MM")
    dayStart = mask.index("DD")
    return (
        int(dateText[yearStart : yearStart + 4]),
        int(dateText[monthStart : monthStart + 2]),
        int(dateText[dayStart : dayStart + 2]),
    )


def _FourDigitText(code: int) -> str:
    """Return code as MOVE stores it into an unsigned PIC 9(4) display field."""
    return f"{int(StoreInto(code, CODE_DIGITS, 0, signed=False)):0{CODE_DIGITS}d}"


def _FitText(text: str, length: int) -> str:
    """Return text as MOVE stores it into a PIC X(length) field: space padded or cut."""
    return text.ljust(length)[:length]


class DatePartStatus(Enum):
    """A CSUTLDWY part flag: WS-EDIT-YEAR-FLG, WS-EDIT-MONTH or WS-EDIT-DAY (lines 43-57)."""

    VALID = LOW_VALUE  # 88 FLG-YEAR-ISVALID, FLG-MONTH-ISVALID, FLG-DAY-ISVALID
    NOT_OK = "0"  # 88 FLG-YEAR-NOT-OK, FLG-MONTH-NOT-OK, FLG-DAY-NOT-OK
    BLANK = "B"  # 88 FLG-YEAR-BLANK, FLG-MONTH-BLANK, FLG-DAY-BLANK


@dataclass(frozen=True)
class DateEditResult:
    """Outcome of one CSUTLDPY edit: the WS-EDIT-DATE-FLGS parts, INPUT-ERROR and the suffix.

    inputError is True when this edit set INPUT-ERROR. messageSuffix is the first suffix this
    edit produced, without the field name, and "" when it produced none (WS-RETURN-MSG-OFF).
    """

    yearStatus: DatePartStatus
    monthStatus: DatePartStatus
    dayStatus: DatePartStatus
    inputError: bool
    messageSuffix: str

    @property
    def isValid(self) -> bool:
        """88 WS-EDIT-DATE-IS-VALID: the year, month and day flags are all VALID."""
        partStatuses = (self.yearStatus, self.monthStatus, self.dayStatus)
        return all(status is DatePartStatus.VALID for status in partStatuses)


class _DatePart(Enum):
    """The three WS-EDIT-DATE-FLGS parts an edit marks."""

    YEAR = "year"
    MONTH = "month"
    DAY = "day"


def _NotOkStatuses() -> dict[_DatePart, DatePartStatus]:
    """SET WS-EDIT-DATE-IS-INVALID (VALUE '000'): every part flag NOT-OK, CSUTLDPY line 19."""
    return dict.fromkeys(_DatePart, DatePartStatus.NOT_OK)


@dataclass
class _DateEditState:
    """The CSUTLDWY working fields of one EDIT-DATE-CCYYMMDD pass.

    dateText is WS-EDIT-DATE-CCYYMMDD; monthValue and dayValue are what the COMPUTEs into
    WS-EDIT-DATE-MM-N and WS-EDIT-DATE-DD-N stored, None while no COMPUTE has run.
    """

    dateText: str
    statuses: dict[_DatePart, DatePartStatus] = field(default_factory=_NotOkStatuses)
    inputError: bool = False
    messageSuffix: str = ""
    monthValue: int | None = None
    dayValue: int | None = None

    @property
    def centuryYearText(self) -> str:
        """WS-EDIT-DATE-CCYY, positions 1-4."""
        return self.dateText[0:4]

    @property
    def monthText(self) -> str:
        """WS-EDIT-DATE-MM, positions 5-6."""
        return self.dateText[4:6]

    @property
    def dayText(self) -> str:
        """WS-EDIT-DATE-DD, positions 7-8."""
        return self.dateText[6:8]

    def IsValid(self) -> bool:
        """88 WS-EDIT-DATE-IS-VALID over the working flags."""
        return all(status is DatePartStatus.VALID for status in self.statuses.values())

    def ToResult(self) -> DateEditResult:
        """Freeze the working state into the public result."""
        return DateEditResult(
            yearStatus=self.statuses[_DatePart.YEAR],
            monthStatus=self.statuses[_DatePart.MONTH],
            dayStatus=self.statuses[_DatePart.DAY],
            inputError=self.inputError,
            messageSuffix=self.messageSuffix,
        )


def EditDateCcyymmdd(dateText: str) -> DateEditResult:
    """Run PERFORM EDIT-DATE-CCYYMMDD THRU EDIT-DATE-CCYYMMDD-EXIT (CSUTLDPY lines 18-331).

    dateText is WS-EDIT-DATE-CCYYMMDD: shorter text is space padded and longer text cut at 8,
    as MOVE does. The year, month, day and combination edits always run, keeping only the first
    suffix. A combination failure skips EDIT-DATE-LE. After an EDIT-DATE-LE failure the LE
    suffix and inputError stand while all three flags read VALID (lines 315 and 323-327).
    """
    return _RunDateEdits(dateText).ToResult()


def EditDateOfBirth(dateText: str, today: date) -> DateEditResult:
    """Port of EDIT-DATE-OF-BIRTH, CSUTLDPY lines 341-372.

    COACTUPC performs it only when EditDateCcyymmdd(dateText).isValid; a date whose flags are
    not valid raises ValueError. today is the caller's local date (FUNCTION CURRENT-DATE). The
    date of birth is valid only when today is later; today or a later date sets every part
    NOT-OK, inputError and ":cannot be in the future ".
    """
    birthDate = _EditedDate(dateText)
    if today > birthDate:
        return DateEditResult(
            yearStatus=DatePartStatus.VALID,
            monthStatus=DatePartStatus.VALID,
            dayStatus=DatePartStatus.VALID,
            inputError=False,
            messageSuffix="",
        )
    return DateEditResult(
        yearStatus=DatePartStatus.NOT_OK,
        monthStatus=DatePartStatus.NOT_OK,
        dayStatus=DatePartStatus.NOT_OK,
        inputError=True,
        messageSuffix=FUTURE_DATE_SUFFIX,
    )


def _RunDateEdits(dateText: str) -> _DateEditState:
    """The paragraph sequence of EDIT-DATE-CCYYMMDD THRU EDIT-DATE-CCYYMMDD-EXIT."""
    state = _DateEditState(_FitText(dateText, EDIT_DATE_LENGTH))
    _EditYear(state)
    _EditMonth(state)
    _EditDay(state)
    if not _EditDayMonthYear(state):
        _EditDateLe(state)
    return state


def _EditYear(state: _DateEditState) -> None:
    """Port of EDIT-YEAR-CCYY, CSUTLDPY lines 25-90: supplied, numeric, century 19 or 20."""
    centuryYear = state.centuryYearText
    if _IsBlankPart(centuryYear):
        _Fail(state, YEAR_REQUIRED_SUFFIX, DatePartStatus.BLANK, _DatePart.YEAR)
    elif not _IsAsciiDigits(centuryYear):
        _Fail(state, YEAR_NOT_NUMERIC_SUFFIX, DatePartStatus.NOT_OK, _DatePart.YEAR)
    elif centuryYear[0:2] not in VALID_CENTURIES:
        _Fail(state, CENTURY_INVALID_SUFFIX, DatePartStatus.NOT_OK, _DatePart.YEAR)
    else:
        _SetStatus(state, DatePartStatus.VALID, _DatePart.YEAR)


def _EditMonth(state: _DateEditState) -> None:
    """Port of EDIT-MONTH, CSUTLDPY lines 91-147.

    The 88 WS-VALID-MONTH test comes first and holds only for the two digits 01-12; non-digit
    text never satisfies the numeric 88-level. The TEST-NUMVAL/NUMVAL step after it therefore
    always passes and leaves the value unchanged.
    """
    monthText = state.monthText
    if _IsBlankPart(monthText):
        _Fail(state, MONTH_REQUIRED_SUFFIX, DatePartStatus.BLANK, _DatePart.MONTH)
    elif _TwoDigitValue(monthText) not in VALID_MONTHS:
        _Fail(state, MONTH_INVALID_SUFFIX, DatePartStatus.NOT_OK, _DatePart.MONTH)
    else:
        state.monthValue = _TwoDigitValue(monthText)
        _SetStatus(state, DatePartStatus.VALID, _DatePart.MONTH)


def _EditDay(state: _DateEditState) -> None:
    """Port of EDIT-DAY, CSUTLDPY lines 150-207.

    The paragraph first sets the day flag valid (line 152). COMPUTE WS-EDIT-DATE-DD-N =
    NUMVAL(day) stores into unsigned 9(2), so " 5" and "5 " give 5, "-5" gives 5 and ".5" gives 0.
    """
    _SetStatus(state, DatePartStatus.VALID, _DatePart.DAY)
    dayText = state.dayText
    if _IsBlankPart(dayText):
        _Fail(state, DAY_REQUIRED_SUFFIX, DatePartStatus.BLANK, _DatePart.DAY)
    elif not TestNumval(dayText):
        _Fail(state, DAY_INVALID_SUFFIX, DatePartStatus.NOT_OK, _DatePart.DAY)
    else:
        state.dayValue = int(StoreInto(Numval(dayText), 2, 0, signed=False))
        if state.dayValue not in VALID_DAYS:
            _Fail(state, DAY_INVALID_SUFFIX, DatePartStatus.NOT_OK, _DatePart.DAY)


def _EditDayMonthYear(state: _DateEditState) -> bool:
    """Port of EDIT-DAY-MONTH-YEAR, CSUTLDPY lines 209-282.

    Returns True for GO TO EDIT-DATE-CCYYMMDD-EXIT, which skips EDIT-DATE-LE. A part with no
    stored value and no two digits is None, which equals no 88-level value.
    """
    month = _PartValue(state.monthText, state.monthValue)
    day = _PartValue(state.dayText, state.dayValue)
    if day == THIRTY_FIRST_DAY and month not in THIRTY_ONE_DAY_MONTHS:
        _Fail(state, NO_31_DAYS_SUFFIX, DatePartStatus.NOT_OK, _DatePart.DAY, _DatePart.MONTH)
        return True
    if month == FEBRUARY and day == THIRTIETH_DAY:
        _Fail(state, NO_30_DAYS_SUFFIX, DatePartStatus.NOT_OK, _DatePart.DAY, _DatePart.MONTH)
        return True
    if month == FEBRUARY and day == LEAP_DAY and not _IsLeapYear(state.centuryYearText):
        _Fail(state, NOT_LEAP_YEAR_SUFFIX, DatePartStatus.NOT_OK, *_DatePart)
        return True
    return not state.IsValid()


def _EditDateLe(state: _DateEditState) -> None:
    """Port of EDIT-DATE-LE through the sentence after EDIT-DATE-LE-EXIT, lines 284-328.

    A non-zero severity sets INPUT-ERROR, the LE suffix and every flag NOT-OK, then GO TO
    EDIT-DATE-LE-EXIT, whose paragraph goes on to SET WS-EDIT-DATE-IS-VALID TO TRUE. Both paths
    therefore end with every flag VALID; the quirk is ported as the source runs it.
    """
    checkResult = CheckDate(_NormalizedDateText(state), CCYYMMDD_DATE_MASK)
    if checkResult.severity != CEEDAYS_VALID_SEVERITY:
        leSuffix = BuildLeValidationSuffix(checkResult.severity, checkResult.messageNumber)
        _Fail(state, leSuffix, DatePartStatus.NOT_OK, *_DatePart)
    _SetStatus(state, DatePartStatus.VALID, *_DatePart)


def _EditedDate(dateText: str) -> date:
    """Return WS-EDIT-DATE-CCYYMMDD-N as a date, refusing text the CCYYMMDD edit rejects."""
    state = _RunDateEdits(dateText)
    if not state.IsValid():
        raise ValueError(
            "EDIT-DATE-OF-BIRTH needs a date whose EDIT-DATE-CCYYMMDD flags are all valid"
        )
    editedText = _NormalizedDateText(state)
    return date(int(editedText[0:4]), int(editedText[4:6]), int(editedText[6:8]))


def _NormalizedDateText(state: _DateEditState) -> str:
    """WS-EDIT-DATE-CCYYMMDD after the COMPUTEs that overwrite its month and day parts."""
    monthText = state.monthText if state.monthValue is None else f"{state.monthValue:02d}"
    dayText = state.dayText if state.dayValue is None else f"{state.dayValue:02d}"
    return state.centuryYearText + monthText + dayText


def _IsLeapYear(centuryYear: str) -> bool:
    """CSUTLDPY lines 243-256: CCYY divided by 400 when YY is 00, otherwise by 4.

    Text that is not four digits is never a leap year, as dividing invalid data would not give
    a zero remainder in the port.
    """
    if not _IsAsciiDigits(centuryYear):
        return False
    divisor = LEAP_DIVISOR
    if centuryYear[2:4] == "00":
        divisor = LEAP_DIVISOR_CENTURY_YEAR
    return int(centuryYear) % divisor == 0


def _Fail(state: _DateEditState, suffix: str, status: DatePartStatus, *parts: _DatePart) -> None:
    """SET INPUT-ERROR, keep suffix only while none is set (IF WS-RETURN-MSG-OFF), mark parts."""
    state.inputError = True
    if not state.messageSuffix:
        state.messageSuffix = suffix
    _SetStatus(state, status, *parts)


def _SetStatus(state: _DateEditState, status: DatePartStatus, *parts: _DatePart) -> None:
    """SET the given part flags to status."""
    for part in parts:
        state.statuses[part] = status


def _PartValue(partText: str, storedValue: int | None) -> int | None:
    """The numeric REDEFINES value an 88-level test reads: the stored value or the two digits."""
    if storedValue is not None:
        return storedValue
    return _TwoDigitValue(partText)


def _TwoDigitValue(partText: str) -> int | None:
    """Return the integer of a two-digit part, or None when it holds anything else."""
    if len(partText) == 2 and _IsAsciiDigits(partText):
        return int(partText)
    return None


def _IsBlankPart(partText: str) -> bool:
    """A part EQUAL SPACES or EQUAL LOW-VALUES: entirely spaces or entirely X'00'."""
    return partText == " " * len(partText) or partText == LOW_VALUE * len(partText)


def _IsAsciiDigits(text: str) -> bool:
    """The COBOL NUMERIC class test on unsigned display text: only ASCII 0-9."""
    return ASCII_DIGITS_PATTERN.fullmatch(text) is not None
