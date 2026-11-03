"""Replaces: CORPT00C (CR00), BMS CORPT00, copybooks CVTRA05Y, CSUTLDTC.

The CR00 transaction report screen. CORPT00C edited the request and wrote inline JCL to TDQ
``JOBS``, which ran ``proc/TRANREPT.prc`` (backup, sort and filter, then CBTRN03C) to print the
TRANREPT file; here the report is built within the request by ``cbtrn03c`` and rendered by
``report_export`` as the screen's JSON (``RenderTransactionReportJson``, which the screen route
sends) or as a CSV or PDF download (``ExportTransactionReport``). ``RunTransactionReport``
returns the same screen as a ``TransactionReportResponse`` holding every line.

The edits and the confirmation run first and wait for nothing. A confirmed report then takes one
of the REPORT_WORKER_LIMIT report slots (``REPORT_SLOTS``) and holds it until it is built and
rendered, so at most that many fetched row sets are in memory at once. Only its three reads run
under a request admission (``core.database.AdmittedReads``), which they free, together with the
session's connection, as soon as they return. The CPU-bound line building, and the JSON, CSV or
PDF rendering in the same single pass, then run on a report thread, so neither a queued nor a
running report keeps other online requests waiting. A report whose client has disconnected
stops at the next check: when its slot is granted, after its reads, and every
DISCONNECT_CHECK_LINES lines while it is built.

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
import functools
import logging
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import MINYEAR, date, datetime
from typing import Any, NamedTuple

from anyio import CapacityLimiter, from_thread, to_thread
from starlette.requests import Request, empty_receive

from app.core.database import AdmittedReads
from app.core.dependencies import OnlineContext
from app.core.errors import (
    HTTP_EDIT_FAILURE,
    HTTP_FILE_ERROR,
    LegacyMessageError,
    LogFileCondition,
    RepositoryError,
)
from app.repositories.reference_repository import ReferenceRepository
from app.repositories.transaction_repository import TransactionRepository
from app.schemas.common import ExportFormat, ReportType
from app.schemas.reports import ReportLine, ReportQuery, TransactionReportResponse
from app.services.reports.cbtrn03c import IterateReport, ReportRequestData
from app.services.reports.report_export import (
    CSV_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    RenderCsv,
    RenderJson,
    RenderPdf,
)
from app.utils.cobol_intrinsics import NumvalC
from app.utils.date_validation import (
    CEEDAYS_MSG_UNSUPPORTED_RANGE,
    CEEDAYS_VALID_SEVERITY,
    CheckDate,
    DateCheckResult,
)
from app.utils.decimal_utils import StoreInto

LOGGER = logging.getLogger(__name__)

# WS-TRANID (line 38).
TRANSACTION_ID = "CR00"
# The TRANREPT reads of TRANTYPE, TRANCATG and TRANSACT that replace the TDQ write, as their
# logged file conditions name them.
READ_OPERATION = "READ"

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
# The lines of a prompt or a cleared screen, which CORPT00C sends before any report is built.
NO_REPORT_LINES: tuple[ReportLine, ...] = ()


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
    """What the edits and the confirmation of one CORPT00 ENTER decided, before any read.

    ``pendingResponse`` is the confirmation prompt, or the screen a confirmation of N clears;
    it is None once Y confirms the report over ``reportRange``.
    """

    reportRange: ReportRange
    pendingResponse: TransactionReportResponse | None


@dataclass(frozen=True)
class ReportJob:
    """The fetched TRANREPT input a report thread builds from, and the request it serves.

    The reads are over: ``rows`` are the STEP05R output and the two maps the TRANTYPE and
    TRANCATG records, as ``cbtrn03c.IterateReport`` takes them.
    """

    request: Request
    requestData: ReportRequestData
    rows: Sequence[Mapping[str, Any]]
    typeDescriptions: Mapping[str, str]
    categoryDescriptions: Mapping[tuple[str, int], str]


class ReportAbandonedError(Exception):
    """Internal: the report's client has disconnected, so the report is not finished."""


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

    render: Callable[[Iterable[ReportLine]], bytes]
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

# The most reports reading, building or rendering at once, across all requests. The work is pure
# Python that holds the GIL, so more at once adds no throughput and only competes with the event
# loop; and each report in a slot holds its fetched rows, so the limit also bounds report memory.
REPORT_WORKER_LIMIT = 2
# The report slots, taken before the reads and held until the report is built and rendered. A
# report waits for its slot holding no admission and no connection. Created at import, before
# any event loop runs, so anyio creates the backend limiter on first use.
REPORT_SLOTS = CapacityLimiter(REPORT_WORKER_LIMIT)
# The worker threads of the reports in a slot. anyio lends a task one token of a limiter at a
# time, and the task already holds its REPORT_SLOTS token, so the thread needs a limiter of its
# own; it is never contended, because only reports in a slot borrow from it.
REPORT_THREADS = CapacityLimiter(REPORT_WORKER_LIMIT)
# The report lines a report thread builds between two checks that its client is still connected.
DISCONNECT_CHECK_LINES = 2000
# nginx's 499 Client Closed Request, the status of a report stopped because its client
# disconnected. The client is gone and never receives it, so it shows only in the server log.
HTTP_CLIENT_CLOSED_REQUEST = 499
CLIENT_CLOSED_MESSAGE = ""
# One INFO record per stopped report; it names the transaction only, never the query or a PAN.
REPORT_ABANDONED_LOG = "%s report stopped: the client disconnected"


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
) -> Sequence[Mapping[str, Any]]:
    """Return the TRANREPT STEP05R output: transactions in range, in TRAN-CARD-NUM order.

    The repository filters on the PROC-TS date and sorts; this read never enters the batch
    window. An end bound no stored date can meet skips the read, as the INCLUDE keeps nothing.
    """
    endBound = ReportEndBound(reportRange.endDate)
    if endBound is None:
        return []
    startBound = ReportStartBound(reportRange.startDate)
    return await repository.GetReportTransactions(startBound, endBound)


async def ReadReportJob(context: OnlineContext, reportRange: ReportRange) -> ReportJob:
    """Run the TRANTYPE, TRANCATG and TRANSACT reads that replace the TDQ job (lines 496-535).

    Only reads run: the STEP01R backup of TRANSACT is superseded. They run under one request
    admission, which ``AdmittedReads`` frees, after ending the read transaction and returning
    the session's connection, as soon as they return or fail.

    Raises:
        LegacyMessageError: 503 ``Unable to Write TDQ (JOBS)...`` with the cursor on the report
            type, the program's WHEN OTHER literal, when a repository read fails.
    """
    referenceRepository = ReferenceRepository(context)
    try:
        async with AdmittedReads(context.request, context.session):
            typeDescriptions = await referenceRepository.GetTypeDescriptions()
            categoryDescriptions = await referenceRepository.GetCategoryDescriptions()
            rows = await ReadReportRows(TransactionRepository(context), reportRange)
    except RepositoryError as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, READ_OPERATION, error)
        raise LegacyMessageError(
            UNABLE_TO_WRITE_TDQ_MESSAGE, field=CURSOR_REPORT_TYPE, httpStatus=HTTP_FILE_ERROR
        ) from error
    requestData = ReportRequestData(reportRange.startDate, reportRange.endDate)
    return ReportJob(context.request, requestData, rows, typeDescriptions, categoryDescriptions)


async def ClientDisconnected(request: Request) -> bool:
    """Return True once the client of ``request`` has closed its connection.

    A request made without a receive channel, such as a service-level test context, has no
    client that could disconnect, so it never reports one.
    """
    if request.receive is empty_receive:
        return False
    return await request.is_disconnected()


async def StopIfClientGone(request: Request) -> None:
    """Raise ReportAbandonedError when the client of ``request`` has disconnected."""
    if await ClientDisconnected(request):
        raise ReportAbandonedError


def WatchClient(lines: Iterable[ReportLine], request: Request) -> Iterator[ReportLine]:
    """Yield lines on a report thread, checking the client every DISCONNECT_CHECK_LINES lines.

    Each check runs ``ClientDisconnected`` on the event loop and waits for its answer.

    Raises:
        ReportAbandonedError: The client has disconnected, so no further line is built.
    """
    for lineNumber, line in enumerate(lines, start=1):
        isCheckLine = lineNumber % DISCONNECT_CHECK_LINES == 0
        if isCheckLine and from_thread.run(ClientDisconnected, request):
            raise ReportAbandonedError
        yield line


def JobLines(job: ReportJob) -> Iterator[ReportLine]:
    """Yield the CBTRN03C lines of ``job`` one at a time, stopping if its client goes away."""
    lines = IterateReport(job.requestData, job.rows, job.typeDescriptions, job.categoryDescriptions)
    return WatchClient(lines, job.request)


def ListReportLines(job: ReportJob) -> list[ReportLine]:
    """Build every report line of ``job`` on a report thread, for ``RunTransactionReport``."""
    return list(JobLines(job))


def RenderReportLines(render: Callable[[Iterable[ReportLine]], bytes], job: ReportJob) -> bytes:
    """Build and render the lines of ``job`` in one pass on a report thread.

    ``render`` reads each line as it is built, so the whole line list never exists.
    """
    return render(JobLines(job))


async def RunReportJob[WorkResult](
    context: OnlineContext, reportRange: ReportRange, work: Callable[[ReportJob], WorkResult]
) -> WorkResult:
    """Read the confirmed report in a report slot, then run ``work`` over it on a report thread.

    The slot is taken before the reads and held until ``work`` returns, so at most
    REPORT_WORKER_LIMIT row sets exist at once; the request admission is held for the reads
    only. A gone client is checked for when the slot is granted, after the reads and while
    ``work`` builds lines. ``cbtrn03c.ReportAbendError`` (the CBTRN03C abend 999) is
    deliberately not caught, so it reaches FastAPI's 500 and the page shows the legacy abend.

    Raises:
        LegacyMessageError: 503 when a read fails, as ``ReadReportJob``; 499
            (HTTP_CLIENT_CLOSED_REQUEST), logged once at INFO, when the client disconnected.
    """
    try:
        async with REPORT_SLOTS:
            await StopIfClientGone(context.request)
            job = await ReadReportJob(context, reportRange)
            await StopIfClientGone(context.request)
            return await to_thread.run_sync(work, job, limiter=REPORT_THREADS)
    except ReportAbandonedError:
        LOGGER.info(REPORT_ABANDONED_LOG, TRANSACTION_ID)
        raise LegacyMessageError(
            CLIENT_CLOSED_MESSAGE, httpStatus=HTTP_CLIENT_CLOSED_REQUEST
        ) from None


async def BuildReportLines(context: OnlineContext, reportRange: ReportRange) -> list[ReportLine]:
    """Replace the TDQ job submission (lines 496-535) with the TRANREPT report, built online.

    Raises:
        LegacyMessageError: As ``RunReportJob``.
    """
    return await RunReportJob(context, reportRange, ListReportLines)


async def RenderReportDocument(
    context: OnlineContext, reportRange: ReportRange, renderer: ExportRenderer
) -> bytes:
    """Build the confirmed report and render it as ``renderer`` in the same single pass.

    Raises:
        LegacyMessageError: As ``RunReportJob``.
    """
    renderWork = functools.partial(RenderReportLines, renderer.render)
    return await RunReportJob(context, reportRange, renderWork)


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


async def BuildReportJson(context: OnlineContext, reportRange: ReportRange) -> bytes:
    """Build the confirmed report and write its success screen as JSON in the same single pass.

    ``report_export.RenderJson`` encodes each line on the report thread as it is built, so the
    whole line list never exists.

    Raises:
        LegacyMessageError: As ``RunReportJob``.
    """
    renderScreen = functools.partial(RenderJson, SubmittedResponse(reportRange, []))
    renderWork = functools.partial(RenderReportLines, renderScreen)
    return await RunReportJob(context, reportRange, renderWork)


def EvaluateReportRequest(query: ReportQuery) -> ReportOutcome:
    """Port the PROCESS-ENTER-KEY edits and SUBMIT-JOB-TO-INTRDR's confirmation (lines 208-494).

    They run before any report slot, admission or read is taken, so a prompt, a cleared screen
    or an edit failure never waits behind running reports.

    Raises:
        LegacyMessageError: 400 for the first failing edit or an invalid confirmation.
    """
    reportRange = ResolveReportRange(query, CurrentDate())
    return ReportOutcome(reportRange, ConfirmationResponse(reportRange, query.confirm))


async def RunTransactionReport(
    context: OnlineContext, query: ReportQuery
) -> TransactionReportResponse:
    """Run CR00 ENTER: edit the request, confirm it and return the report lines.

    Returns:
        The prompt (blank confirmation), the cleared screen (N) or the submitted literal with
        the CBTRN03C lines (Y), each with 200.

    Raises:
        LegacyMessageError: 400 for an edit or confirmation failure; 503 for a failed read;
            499 when the client disconnected before the report was built.
    """
    outcome = EvaluateReportRequest(query)
    if outcome.pendingResponse is not None:
        return outcome.pendingResponse
    lines = await BuildReportLines(context, outcome.reportRange)
    return SubmittedResponse(outcome.reportRange, lines)


async def RenderTransactionReportJson(context: OnlineContext, query: ReportQuery) -> bytes:
    """Run CR00 ENTER as ``RunTransactionReport`` does and return its screen as JSON bytes.

    The bytes are those FastAPI sends for the ``TransactionReportResponse`` that
    ``RunTransactionReport`` returns, but a confirmed report's lines are encoded one at a time
    as they are built, so neither the line list nor a response model holding it exists. The
    screen route sends them as they are.

    Returns:
        The JSON of the prompt (blank confirmation), the cleared screen (N) or the submitted
        literal with the CBTRN03C lines (Y), each sent with 200.

    Raises:
        LegacyMessageError: As ``RunTransactionReport``.
    """
    outcome = EvaluateReportRequest(query)
    if outcome.pendingResponse is not None:
        return RenderJson(outcome.pendingResponse, NO_REPORT_LINES)
    return await BuildReportJson(context, outcome.reportRange)


async def ExportTransactionReport(
    context: OnlineContext, query: ReportQuery, exportFormat: ExportFormat
) -> tuple[bytes, str, str]:
    """Run the CR00 edits and render the confirmed report as a CSV or PDF download.

    The lines are built and rendered in memory in one pass on a report thread, so rendering
    never holds the event loop and the whole line list never exists; no file is written.

    Returns:
        The document bytes, its media type and its ``TRANREPT_<start>_<end>.<ext>`` file name.

    Raises:
        LegacyMessageError: 400 with the prompt or cleared-screen message and cursor when the
            request does not confirm a report; otherwise as ``RunTransactionReport``.
    """
    outcome = EvaluateReportRequest(query)
    pendingResponse = outcome.pendingResponse
    if pendingResponse is not None:
        raise LegacyMessageError(
            pendingResponse.message,
            field=pendingResponse.cursor_field or "",
            httpStatus=HTTP_EDIT_FAILURE,
        )
    reportRange = outcome.reportRange
    renderer = EXPORT_RENDERERS[exportFormat]
    fileName = EXPORT_FILENAME_TEMPLATE.format(
        startDate=reportRange.startDate, endDate=reportRange.endDate, extension=renderer.extension
    )
    documentBytes = await RenderReportDocument(context, reportRange, renderer)
    return documentBytes, renderer.mediaType, fileName
