"""Replaces: CORPT00C (CR00), BMS CORPT00, copybooks CVTRA05Y, CSUTLDTC.

The CR00 transaction report screen. CORPT00C edited the request and wrote inline JCL to TDQ
``JOBS``, which ran ``proc/TRANREPT.prc`` (backup, sort and filter, then CBTRN03C) to print the
TRANREPT file; here the report is built synchronously by ``cbtrn03c.BuildReport`` and returned
as JSON lines, or rendered as CSV or PDF by ``report_export``.

The TRANREPT STEP01R backup of TRANSACT is superseded: this module never writes, deletes or
archives anything and only reads. CR00 never opened a CLOSEFIL file, so the report is not gated
by the batch window: it calls only ``TransactionRepository.GetReportTransactions`` and the two
``ReferenceRepository`` reads, which never enter the window.

The edits run in first-failure mode: every ``PERFORM SEND-TRNRPT-SCREEN`` ends the task with
``GO TO RETURN-TO-CICS`` (line 580), so the first failing edit raises its literal. Cursor names
are the ``ReportQuery`` wire names, which are the CORPT00 page's field names.

Legacy paths with no code here: ``EIBCALEN = 0`` (back to sign-on) is the session guard and its
401; PF3 (``XCTL COMEN01C``) and the invalid-key branch are frontend navigation;
POPULATE-HEADER-INFO is the frontend ScreenShell.
"""

import calendar
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import MINYEAR, date, datetime
from typing import Any, NamedTuple

from app.core.dependencies import OnlineContext
from app.core.errors import HTTP_EDIT_FAILURE, HTTP_FILE_ERROR, LegacyMessageError, RepositoryError
from app.repositories.reference_repository import ReferenceRepository
from app.repositories.transaction_repository import TransactionRepository
from app.schemas.common import ExportFormat, ReportType
from app.schemas.reports import ReportLine, ReportQuery, TransactionReportResponse
from app.services.reports.cbtrn03c import BuildReport, ReportRequestData
from app.services.reports.report_export import CSV_MEDIA_TYPE, PDF_MEDIA_TYPE, RenderCsv, RenderPdf
from app.utils.cobol_intrinsics import NumvalC
from app.utils.date_validation import (
    CEEDAYS_MSG_UNSUPPORTED_RANGE,
    CEEDAYS_VALID_SEVERITY,
    CheckDate,
    DateCheckResult,
)
from app.utils.decimal_utils import StoreInto

# PROCESS-ENTER-KEY WHEN OTHER (line 438): no report type was selected.
SELECT_REPORT_TYPE_MESSAGE = "Select a report type to print report..."

# The presence EVALUATE of the custom range (lines 258-303).
START_MONTH_EMPTY_MESSAGE = "Start Date - Month can NOT be empty..."
START_DAY_EMPTY_MESSAGE = "Start Date - Day can NOT be empty..."
START_YEAR_EMPTY_MESSAGE = "Start Date - Year can NOT be empty..."
END_MONTH_EMPTY_MESSAGE = "End Date - Month can NOT be empty..."
END_DAY_EMPTY_MESSAGE = "End Date - Day can NOT be empty..."
END_YEAR_EMPTY_MESSAGE = "End Date - Year can NOT be empty..."

# The part checks after NUMVAL-C normalisation (lines 329-379).
START_MONTH_INVALID_MESSAGE = "Start Date - Not a valid Month..."
START_DAY_INVALID_MESSAGE = "Start Date - Not a valid Day..."
START_YEAR_INVALID_MESSAGE = "Start Date - Not a valid Year..."
END_MONTH_INVALID_MESSAGE = "End Date - Not a valid Month..."
END_DAY_INVALID_MESSAGE = "End Date - Not a valid Day..."
END_YEAR_INVALID_MESSAGE = "End Date - Not a valid Year..."

# The CSUTLDTC checks of the composed dates (lines 400 and 420).
START_DATE_INVALID_MESSAGE = "Start Date - Not a valid date..."
END_DATE_INVALID_MESSAGE = "End Date - Not a valid date..."

# SUBMIT-JOB-TO-INTRDR (lines 465-470 and 485-490) and the success STRING (lines 449-452).
# WS-REPORT-NAME and CONFIRMI are DELIMITED BY SPACE.
CONFIRM_PROMPT_TEMPLATE = "Please confirm to print the {reportName} report..."
INVALID_CONFIRM_TEMPLATE = '"{value}" is not a valid value to confirm...'
REPORT_SUBMITTED_TEMPLATE = "{reportName} report submitted for printing ..."

# WIRTE-JOBSUB-TDQ WHEN OTHER (line 531), the program's only file-error literal. It answers
# every repository failure of the report reads that replace the TDQ job submission.
UNABLE_TO_WRITE_TDQ_MESSAGE = "Unable to Write TDQ (JOBS)..."

# WS-REPORT-NAME values (lines 214, 240 and 433).
MONTHLY_REPORT_NAME = "Monthly"
YEARLY_REPORT_NAME = "Yearly"
CUSTOM_REPORT_NAME = "Custom"

# WS-DATE-FORMAT, passed to CSUTLDTC as CSUTLDTC-DATE-FORMAT.
DATE_MASK = "YYYY-MM-DD"
# WS-NUM-99 PIC 99 and WS-NUM-9999 PIC 9999: the unsigned receivers of the NUMVAL-C results.
MONTH_DIGITS = 2
DAY_DIGITS = 2
YEAR_DIGITS = 4
# The alphanumeric comparisons SDTMMI > '12' and SDTDDI > '31'.
MAX_MONTH_TEXT = "12"
MAX_DAY_TEXT = "31"
# The NUMERIC class test of a PIC X field: every character is a digit 0-9.
NUMERIC_CHARACTERS = frozenset("0123456789")
# The padding a BMS input field carries; SPACES OR LOW-VALUES is a field holding only padding.
FIELD_PADDING = " "
FIRST_DAY_TEXT = "01"
FIRST_MONTH_TEXT = "01"
LAST_MONTH_TEXT = "12"
LAST_DAY_OF_YEAR_TEXT = "31"

# CONFIRMI = 'Y' OR 'y' and CONFIRMI = 'N' OR 'n'.
CONFIRM_YES_VALUES = frozenset({"Y", "y"})
CONFIRM_NO_VALUES = frozenset({"N", "n"})

# The ReportQuery wire names, replacing MOVE -1 TO <field>L. MONTHLYL, the first selector of the
# legacy map, is the single report-type selector.
CURSOR_REPORT_TYPE = "reportType"
CURSOR_START_MM = "startMm"
CURSOR_START_DD = "startDd"
CURSOR_START_YYYY = "startYyyy"
CURSOR_END_MM = "endMm"
CURSOR_END_DD = "endDd"
CURSOR_END_YYYY = "endYyyy"
CURSOR_CONFIRM = "confirm"

EXPORT_FILENAME_TEMPLATE = "TRANREPT_{startDate}_{endDate}.{extension}"


@dataclass(frozen=True)
class ReportRange:
    """The report name and range: WS-REPORT-NAME, WS-START-DATE and WS-END-DATE.

    ``startDate`` and ``endDate`` are the 10-character YYYY-MM-DD texts CORPT00C wrote into the
    job's SYMNAMES and DATEPARM records (PARM-START-DATE and PARM-END-DATE).
    """

    reportName: str
    startDate: str
    endDate: str


@dataclass(frozen=True)
class ReportOutcome:
    """The CORPT00 screen ENTER produced, and whether it carries a built report.

    ``built`` is False for the confirmation prompt and for the screen a confirmation of N
    clears; it is True once the report lines were built.
    """

    response: TransactionReportResponse
    built: bool


class DatePartField(NamedTuple):
    """One CORPT00 date part: its ReportQuery attribute, cursor, width and literals.

    ``maximumText`` is the alphanumeric upper bound of the part check, or None for a year,
    which CORPT00C tests only for NUMERIC.
    """

    attributeName: str
    cursorField: str
    digits: int
    emptyMessage: str
    invalidMessage: str
    maximumText: str | None


class ReportDateField(NamedTuple):
    """One composed CORPT00 date: its three parts, its CSUTLDTC literal and its cursor."""

    yearAttribute: str
    monthAttribute: str
    dayAttribute: str
    invalidMessage: str
    cursorField: str


class ExportRenderer(NamedTuple):
    """The renderer, media type and file extension of one report download format."""

    render: Callable[[Sequence[ReportLine]], bytes]
    mediaType: str
    extension: str


# SDTMM, SDTDD, SDTYYYY, EDTMM, EDTDD, EDTYYYY: the order of the presence EVALUATE, the NUMVAL-C
# moves and the part checks alike (lines 258-379).
DATE_PART_FIELDS = (
    DatePartField(
        "start_mm",
        CURSOR_START_MM,
        MONTH_DIGITS,
        START_MONTH_EMPTY_MESSAGE,
        START_MONTH_INVALID_MESSAGE,
        MAX_MONTH_TEXT,
    ),
    DatePartField(
        "start_dd",
        CURSOR_START_DD,
        DAY_DIGITS,
        START_DAY_EMPTY_MESSAGE,
        START_DAY_INVALID_MESSAGE,
        MAX_DAY_TEXT,
    ),
    DatePartField(
        "start_yyyy",
        CURSOR_START_YYYY,
        YEAR_DIGITS,
        START_YEAR_EMPTY_MESSAGE,
        START_YEAR_INVALID_MESSAGE,
        None,
    ),
    DatePartField(
        "end_mm",
        CURSOR_END_MM,
        MONTH_DIGITS,
        END_MONTH_EMPTY_MESSAGE,
        END_MONTH_INVALID_MESSAGE,
        MAX_MONTH_TEXT,
    ),
    DatePartField(
        "end_dd",
        CURSOR_END_DD,
        DAY_DIGITS,
        END_DAY_EMPTY_MESSAGE,
        END_DAY_INVALID_MESSAGE,
        MAX_DAY_TEXT,
    ),
    DatePartField(
        "end_yyyy",
        CURSOR_END_YYYY,
        YEAR_DIGITS,
        END_YEAR_EMPTY_MESSAGE,
        END_YEAR_INVALID_MESSAGE,
        None,
    ),
)

# The start date is checked before the end date (lines 388-426). Both failures put the cursor on
# the month part: MOVE -1 TO SDTMML and MOVE -1 TO EDTMML.
REPORT_DATE_FIELDS = (
    ReportDateField(
        "start_yyyy", "start_mm", "start_dd", START_DATE_INVALID_MESSAGE, CURSOR_START_MM
    ),
    ReportDateField("end_yyyy", "end_mm", "end_dd", END_DATE_INVALID_MESSAGE, CURSOR_END_MM),
)

EXPORT_RENDERERS: dict[ExportFormat, ExportRenderer] = {
    ExportFormat.CSV: ExportRenderer(RenderCsv, CSV_MEDIA_TYPE, "csv"),
    ExportFormat.PDF: ExportRenderer(RenderPdf, PDF_MEDIA_TYPE, "pdf"),
}


def CurrentDate() -> date:
    """Return today's date in server local time, as ``FUNCTION CURRENT-DATE`` gives it."""
    return datetime.now().date()


def IsBlank(value: str) -> bool:
    """Return True for ``SPACES OR LOW-VALUES``: an empty field or one holding only spaces."""
    return not value.strip(FIELD_PADDING)


def IsNumericText(value: str) -> bool:
    """Return True when ``value IS NUMERIC`` holds for a PIC X field: digits 0-9 only."""
    return value != "" and NUMERIC_CHARACTERS.issuperset(value)


def DateText(yearText: str, monthText: str, dayText: str) -> str:
    """Return the WS-START-DATE / WS-END-DATE layout: YYYY, '-', MM, '-', DD."""
    return f"{yearText}-{monthText}-{dayText}"


def MonthlyRange(today: date) -> ReportRange:
    """Port the Monthly branch of PROCESS-ENTER-KEY (lines 213-238).

    The start is the first of the current month. The end is DATE-OF-INTEGER(INTEGER-OF-DATE of
    the first of the next month, December rolling into January of the next year, minus 1): the
    last day of the current month, which ``calendar.monthrange`` gives directly.
    """
    yearText = f"{today.year:0{YEAR_DIGITS}d}"
    monthText = f"{today.month:0{MONTH_DIGITS}d}"
    lastDay = calendar.monthrange(today.year, today.month)[1]
    return ReportRange(
        MONTHLY_REPORT_NAME,
        DateText(yearText, monthText, FIRST_DAY_TEXT),
        DateText(yearText, monthText, f"{lastDay:0{DAY_DIGITS}d}"),
    )


def YearlyRange(today: date) -> ReportRange:
    """Port the Yearly branch of PROCESS-ENTER-KEY (lines 239-255): 1 January to 31 December."""
    yearText = f"{today.year:0{YEAR_DIGITS}d}"
    return ReportRange(
        YEARLY_REPORT_NAME,
        DateText(yearText, FIRST_MONTH_TEXT, FIRST_DAY_TEXT),
        DateText(yearText, LAST_MONTH_TEXT, LAST_DAY_OF_YEAR_TEXT),
    )


def CheckDatePartsPresent(query: ReportQuery) -> None:
    """Port the presence EVALUATE of the Custom branch (lines 258-303).

    Raises:
        LegacyMessageError: 400 with the first blank part's literal and that part as the
            cursor, in the order start month, day, year, then end month, day, year.
    """
    for part in DATE_PART_FIELDS:
        if IsBlank(getattr(query, part.attributeName)):
            raise LegacyMessageError(
                part.emptyMessage, field=part.cursorField, httpStatus=HTTP_EDIT_FAILURE
            )


def NormaliseDatePart(text: str, digits: int) -> str:
    """Port one ``COMPUTE WS-NUM-99 = FUNCTION NUMVAL-C(...)`` and its MOVE back (lines 305-327).

    The value is stored into the unsigned PIC 99 or PIC 9999 receiver (absolute value, fraction
    truncated, high-order digits dropped) and moved back as zero-padded text: ``5`` gives
    ``05``, ``-5`` gives ``05``, ``1.9`` gives ``01``, and text NUMVAL-C cannot read gives
    ``00`` or ``0000``.
    """
    stored = StoreInto(NumvalC(text), digits, 0, signed=False)
    return f"{int(stored):0{digits}d}"


def NormaliseDateParts(query: ReportQuery) -> dict[str, str]:
    """Return every date part normalised by NUMVAL-C, keyed by its ReportQuery attribute.

    The MOVEs back into SDTMMI and the other input fields also redisplayed the normalised parts
    on the map. The CR00 response contract has no field slot for them, so they feed the checks
    and reach the page only as the composed ``start_date`` and ``end_date``.
    """
    return {
        part.attributeName: NormaliseDatePart(getattr(query, part.attributeName), part.digits)
        for part in DATE_PART_FIELDS
    }


def CheckDatePartValues(partValues: dict[str, str]) -> None:
    """Port the six part checks of the Custom branch (lines 329-379), as written.

    A month fails when NOT NUMERIC or above '12', a day when NOT NUMERIC or above '31', and a
    year when NOT NUMERIC. The parts arrive normalised to digits, so the NOT NUMERIC halves and
    both ``Not a valid Year...`` literals are unreachable from any raw input; a month or day of
    ``00`` passes here and fails the CSUTLDTC date check instead.

    Raises:
        LegacyMessageError: 400 with the first failing part's literal and that part as cursor.
    """
    for part in DATE_PART_FIELDS:
        partText = partValues[part.attributeName]
        isAboveMaximum = part.maximumText is not None and partText > part.maximumText
        if not IsNumericText(partText) or isAboveMaximum:
            raise LegacyMessageError(
                part.invalidMessage, field=part.cursorField, httpStatus=HTTP_EDIT_FAILURE
            )


def IsAcceptedDate(result: DateCheckResult) -> bool:
    """Return True for CSUTLDTC severity '0000', or message 2513 (unsupported range)."""
    if result.severity == CEEDAYS_VALID_SEVERITY:
        return True
    return result.messageNumber == CEEDAYS_MSG_UNSUPPORTED_RANGE


def CheckReportDate(partValues: dict[str, str], dateField: ReportDateField) -> str:
    """Port one composed-date check (lines 381-426): MOVE the parts, then CALL 'CSUTLDTC'.

    Returns:
        The composed YYYY-MM-DD text.

    Raises:
        LegacyMessageError: 400 with the date's literal and its month part as the cursor.
    """
    dateText = DateText(
        partValues[dateField.yearAttribute],
        partValues[dateField.monthAttribute],
        partValues[dateField.dayAttribute],
    )
    if not IsAcceptedDate(CheckDate(dateText, DATE_MASK)):
        raise LegacyMessageError(
            dateField.invalidMessage, field=dateField.cursorField, httpStatus=HTTP_EDIT_FAILURE
        )
    return dateText


def CustomRange(query: ReportQuery) -> ReportRange:
    """Port the Custom branch of PROCESS-ENTER-KEY (lines 256-436) in first-failure mode.

    Presence, NUMVAL-C normalisation, the part checks and the start then end CSUTLDTC checks
    run in source order, and the first failure ends the request.
    """
    CheckDatePartsPresent(query)
    partValues = NormaliseDateParts(query)
    CheckDatePartValues(partValues)
    startDate, endDate = (
        CheckReportDate(partValues, dateField) for dateField in REPORT_DATE_FIELDS
    )
    return ReportRange(CUSTOM_REPORT_NAME, startDate, endDate)


def ResolveReportRange(query: ReportQuery, today: date) -> ReportRange:
    """Port the report-type EVALUATE of PROCESS-ENTER-KEY (lines 212-256 and 437-442).

    The legacy map's three one-character selectors, tested Monthly, Yearly, Custom in that
    order, are the single ``report_type``. Monthly and Yearly run no date edit.

    Raises:
        LegacyMessageError: 400 ``Select a report type to print report...`` with the cursor on
            the report type, or a Custom edit failure.
    """
    if query.report_type == ReportType.MONTHLY:
        return MonthlyRange(today)
    if query.report_type == ReportType.YEARLY:
        return YearlyRange(today)
    if query.report_type == ReportType.CUSTOM:
        return CustomRange(query)
    raise LegacyMessageError(
        SELECT_REPORT_TYPE_MESSAGE, field=CURSOR_REPORT_TYPE, httpStatus=HTTP_EDIT_FAILURE
    )


def PromptResponse(reportRange: ReportRange) -> TransactionReportResponse:
    """Return the blank-confirmation screen of SUBMIT-JOB-TO-INTRDR (lines 464-474).

    A prompt rather than a failure, so it is sent with 200 and no lines; the cursor goes to the
    confirm field (MOVE -1 TO CONFIRML).
    """
    return TransactionReportResponse(
        start_date=reportRange.startDate,
        end_date=reportRange.endDate,
        message=CONFIRM_PROMPT_TEMPLATE.format(reportName=reportRange.reportName),
        cursor_field=CURSOR_CONFIRM,
        lines=[],
    )


def ClearedResponse() -> TransactionReportResponse:
    """Return the screen INITIALIZE-ALL-FIELDS leaves after a confirmation of N (lines 633-646).

    Every field and WS-MESSAGE are initialised and the cursor goes to the report type
    (MOVE -1 TO MONTHLYL); the empty message with no lines tells the page to clear its form.
    """
    return TransactionReportResponse(
        start_date="", end_date="", message="", cursor_field=CURSOR_REPORT_TYPE, lines=[]
    )


def ConfirmationResponse(
    reportRange: ReportRange, confirm: str
) -> TransactionReportResponse | None:
    """Port the confirmation tests of SUBMIT-JOB-TO-INTRDR (lines 464-494).

    Returns:
        The prompt for a blank confirmation, the cleared screen for N or n, and None for Y or
        y, which goes on to build the report.

    Raises:
        LegacyMessageError: 400 ``"<value>" is not a valid value to confirm...`` for any other
            value, the value taken up to its first space, with the cursor on the confirm field.
    """
    if IsBlank(confirm):
        return PromptResponse(reportRange)
    # A PIC X(01) comparison pads the shorter operand with spaces, so trailing padding is equal.
    confirmValue = confirm.rstrip(FIELD_PADDING)
    if confirmValue in CONFIRM_YES_VALUES:
        return None
    if confirmValue in CONFIRM_NO_VALUES:
        return ClearedResponse()
    raise LegacyMessageError(
        INVALID_CONFIRM_TEMPLATE.format(value=confirm.partition(FIELD_PADDING)[0]),
        field=CURSOR_CONFIRM,
        httpStatus=HTTP_EDIT_FAILURE,
    )


def ReportStartBound(startDate: str) -> date:
    """Return the first date whose ISO text is at or after the accepted start-date text.

    The TRANREPT INCLUDE compares TRAN-PROC-DT as 10 characters. CSUTLDTC accepts a year 0000
    date (message 2513) that ``date`` cannot hold; every stored date's text follows it, so the
    bound is ``date.min``.
    """
    if int(startDate[:YEAR_DIGITS]) < MINYEAR:
        return date.min
    return date.fromisoformat(startDate)


def ReportEndBound(endDate: str) -> date | None:
    """Return the last date whose ISO text is at or before the accepted end-date text.

    None means no stored date's text can be at or before it: a year 0000 end date, which
    CSUTLDTC accepts with message 2513, selects no transaction.
    """
    if int(endDate[:YEAR_DIGITS]) < MINYEAR:
        return None
    return date.fromisoformat(endDate)


async def ReadReportRows(
    repository: TransactionRepository, reportRange: ReportRange
) -> list[dict[str, Any]]:
    """Return the TRANREPT STEP05R output: transactions in range, in TRAN-CARD-NUM order.

    The repository filters on the PROC-TS date and sorts; this read never enters the batch
    window. An end bound no stored date can meet skips the read, as the INCLUDE keeps nothing.
    """
    endBound = ReportEndBound(reportRange.endDate)
    if endBound is None:
        return []
    startBound = ReportStartBound(reportRange.startDate)
    return await repository.GetReportTransactions(startBound, endBound)


async def BuildReportLines(context: OnlineContext, reportRange: ReportRange) -> list[ReportLine]:
    """Replace the TDQ job submission (lines 496-535) with the TRANREPT report, built online.

    Only reads run: the STEP01R backup of TRANSACT is superseded. ``cbtrn03c.ReportAbendError``
    (the CBTRN03C abend 999) is deliberately not caught, so it reaches FastAPI's 500 and the
    page shows the legacy abend text.

    Raises:
        LegacyMessageError: 503 ``Unable to Write TDQ (JOBS)...`` with the cursor on the report
            type, the program's WHEN OTHER literal, when a repository read fails.
    """
    referenceRepository = ReferenceRepository(context)
    try:
        typeDescriptions = await referenceRepository.GetTypeDescriptions()
        categoryDescriptions = await referenceRepository.GetCategoryDescriptions()
        rows = await ReadReportRows(TransactionRepository(context), reportRange)
    except RepositoryError as error:
        raise LegacyMessageError(
            UNABLE_TO_WRITE_TDQ_MESSAGE, field=CURSOR_REPORT_TYPE, httpStatus=HTTP_FILE_ERROR
        ) from error
    requestData = ReportRequestData(reportRange.startDate, reportRange.endDate)
    return BuildReport(requestData, rows, typeDescriptions, categoryDescriptions)


def SubmittedResponse(
    reportRange: ReportRange, lines: list[ReportLine]
) -> TransactionReportResponse:
    """Return the success screen of lines 445-455: the green literal and the report lines.

    The cursor goes to the report type (MOVE -1 TO MONTHLYL); the lines are empty when no
    transaction lies in the range.
    """
    return TransactionReportResponse(
        start_date=reportRange.startDate,
        end_date=reportRange.endDate,
        message=REPORT_SUBMITTED_TEMPLATE.format(reportName=reportRange.reportName),
        cursor_field=CURSOR_REPORT_TYPE,
        lines=lines,
    )


async def EvaluateReportRequest(context: OnlineContext, query: ReportQuery) -> ReportOutcome:
    """Port PROCESS-ENTER-KEY and SUBMIT-JOB-TO-INTRDR (lines 208-510) for one ENTER.

    Raises:
        LegacyMessageError: 400 for the first failing edit or an invalid confirmation; 503
            when a report read fails.
    """
    reportRange = ResolveReportRange(query, CurrentDate())
    pendingResponse = ConfirmationResponse(reportRange, query.confirm)
    if pendingResponse is not None:
        return ReportOutcome(response=pendingResponse, built=False)
    lines = await BuildReportLines(context, reportRange)
    return ReportOutcome(response=SubmittedResponse(reportRange, lines), built=True)


async def RunTransactionReport(
    context: OnlineContext, query: ReportQuery
) -> TransactionReportResponse:
    """Run CR00 ENTER: edit the request, confirm it and return the report lines.

    Returns:
        The prompt (blank confirmation), the cleared screen (N) or the submitted literal with
        the CBTRN03C lines (Y), each with 200.

    Raises:
        LegacyMessageError: 400 for an edit or confirmation failure; 503 for a failed read.
    """
    outcome = await EvaluateReportRequest(context, query)
    return outcome.response


async def ExportTransactionReport(
    context: OnlineContext, query: ReportQuery, exportFormat: ExportFormat
) -> tuple[bytes, str, str]:
    """Run the CR00 edits and render the confirmed report as a CSV or PDF download.

    The content is built in memory; no file is written.

    Returns:
        The document bytes, its media type and its ``TRANREPT_<start>_<end>.<ext>`` file name.

    Raises:
        LegacyMessageError: 400 with the prompt or cleared-screen message and cursor when the
            request does not confirm a report; otherwise as ``RunTransactionReport``.
    """
    outcome = await EvaluateReportRequest(context, query)
    response = outcome.response
    if not outcome.built:
        raise LegacyMessageError(
            response.message, field=response.cursor_field or "", httpStatus=HTTP_EDIT_FAILURE
        )
    renderer = EXPORT_RENDERERS[exportFormat]
    fileName = EXPORT_FILENAME_TEMPLATE.format(
        startDate=response.start_date, endDate=response.end_date, extension=renderer.extension
    )
    return renderer.render(response.lines), renderer.mediaType, fileName
