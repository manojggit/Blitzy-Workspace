"""Unit tests for CR00, the transaction report screen: ``corpt00c.RunTransactionReport``.

Replaces: CORPT00C (CR00), BMS map CORPT00, the CSUTLDTC date check it calls, and the TRANREPT
job (``proc/TRANREPT.prc``) its TDQ ``JOBS`` write submitted, which the online report supersedes.

Each case calls the service directly with a regular user's context, the clock frozen at
2022-07-18 and three committed transactions on card 2988091353094312 (proc dates 2022-06-30,
2022-07-01 and 2022-07-31). It asserts the exact ERRMSG literal, the cursor (the ReportQuery
wire name replacing ``MOVE -1 TO <field>L``), the status (a raised LegacyMessageError's
httpStatus, or a returned 200 screen), and that TRANSACT keeps every row: the TRANREPT STEP01R
backup that emptied TRANSACT is not reproduced. Expected literals are typed from
``cbl/CORPT00C.cbl``, never imported from the service.
"""

from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import NamedTuple, NoReturn

import pytest
import pytest_asyncio
from sqlalchemy import Engine, text

from app.core.errors import FileIoError, LegacyMessageError
from app.repositories.transaction_repository import TransactionRepository
from app.schemas.common import ReportType
from app.schemas.reports import ReportLine, ReportLineKind, ReportQuery, TransactionReportResponse
from app.services.reports import corpt00c
from app.utils.date_validation import CheckDate

REGULAR_USER_TYPE = "U"
EDIT_FAILURE_STATUS = 400
FILE_ERROR_STATUS = 503
TRANSACT_FILE_NAME = "TRANSACT"

# FUNCTION CURRENT-DATE as the frozen clock answers it.
REPORT_TODAY = date(2022, 7, 18)

# PROCESS-ENTER-KEY WHEN OTHER (line 438).
SELECT_REPORT_TYPE_MESSAGE = "Select a report type to print report..."
# The presence EVALUATE of the Custom branch (lines 261-296).
START_MONTH_EMPTY_MESSAGE = "Start Date - Month can NOT be empty..."
START_DAY_EMPTY_MESSAGE = "Start Date - Day can NOT be empty..."
START_YEAR_EMPTY_MESSAGE = "Start Date - Year can NOT be empty..."
END_MONTH_EMPTY_MESSAGE = "End Date - Month can NOT be empty..."
END_DAY_EMPTY_MESSAGE = "End Date - Day can NOT be empty..."
END_YEAR_EMPTY_MESSAGE = "End Date - Year can NOT be empty..."
# The part checks: NOT NUMERIC or above '12' for a month, above '31' for a day (lines 329-371).
START_MONTH_INVALID_MESSAGE = "Start Date - Not a valid Month..."
START_DAY_INVALID_MESSAGE = "Start Date - Not a valid Day..."
END_MONTH_INVALID_MESSAGE = "End Date - Not a valid Month..."
END_DAY_INVALID_MESSAGE = "End Date - Not a valid Day..."
# The CSUTLDTC checks of the composed dates, unless the message number is 2513 (lines 396-426).
START_DATE_INVALID_MESSAGE = "Start Date - Not a valid date..."
END_DATE_INVALID_MESSAGE = "End Date - Not a valid date..."
# SUBMIT-JOB-TO-INTRDR's STRINGs (lines 465-470 and 485-490), WS-REPORT-NAME and CONFIRMI
# DELIMITED BY SPACE, and the success STRING of lines 449-452.
MONTHLY_PROMPT_MESSAGE = "Please confirm to print the Monthly report..."
CUSTOM_PROMPT_MESSAGE = "Please confirm to print the Custom report..."
INVALID_CONFIRM_VALUE = "Q"
INVALID_CONFIRM_MESSAGE = '"Q" is not a valid value to confirm...'
MONTHLY_SUBMITTED_MESSAGE = "Monthly report submitted for printing ..."
YEARLY_SUBMITTED_MESSAGE = "Yearly report submitted for printing ..."
# INITIALIZE-ALL-FIELDS (lines 633-646) clears every field and WS-MESSAGE.
CLEARED_TEXT = ""
# WIRTE-JOBSUB-TDQ WHEN OTHER (line 531).
UNABLE_TO_WRITE_TDQ_MESSAGE = "Unable to Write TDQ (JOBS)..."

# The ReportQuery wire names, the CORPT00 fields MOVE -1 TO <field>L puts the cursor on.
CURSOR_REPORT_TYPE = "reportType"
CURSOR_START_MM = "startMm"
CURSOR_START_DD = "startDd"
CURSOR_START_YYYY = "startYyyy"
CURSOR_END_MM = "endMm"
CURSOR_END_DD = "endDd"
CURSOR_END_YYYY = "endYyyy"
CURSOR_CONFIRM = "confirm"
DATE_PART_NAMES = (
    CURSOR_START_MM,
    CURSOR_START_DD,
    CURSOR_START_YYYY,
    CURSOR_END_MM,
    CURSOR_END_DD,
    CURSOR_END_YYYY,
)

# WS-START-DATE and WS-END-DATE of the Monthly (lines 217-236) and Yearly (lines 243-252)
# branches on REPORT_TODAY.
MONTHLY_START_DATE = "2022-07-01"
MONTHLY_END_DATE = "2022-07-31"
YEARLY_START_DATE = "2022-01-01"
YEARLY_END_DATE = "2022-12-31"

# A valid, unconfirmed Custom request over 2022, keyed by wire name.
CUSTOM_START_DATE = "2022-01-01"
CUSTOM_END_DATE = "2022-12-31"
BASE_QUERY_FIELDS = {
    CURSOR_REPORT_TYPE: ReportType.CUSTOM.value,
    CURSOR_START_MM: "01",
    CURSOR_START_DD: "01",
    CURSOR_START_YYYY: "2022",
    CURSOR_END_MM: "12",
    CURSOR_END_DD: "31",
    CURSOR_END_YYYY: "2022",
    CURSOR_CONFIRM: "",
}
MONTHLY_FIELDS = {CURSOR_REPORT_TYPE: ReportType.MONTHLY.value}
YEARLY_FIELDS = {CURSOR_REPORT_TYPE: ReportType.YEARLY.value}

# CEEDAYS refuses a date before Lilian day 1 (1582-10-15) with message 2513, which CORPT00C
# accepts.
ISO_DATE_MASK = "YYYY-MM-DD"
UNSUPPORTED_RANGE_MESSAGE_NUMBER = 2513
PRE_LILIAN_START_DATE = "1582-10-14"
PRE_LILIAN_START_FIELDS = {CURSOR_START_YYYY: "1582", CURSOR_START_MM: "10", CURSOR_START_DD: "14"}
# A year NUMVAL-C cannot read; its WS-NUM-9999 receiver holds 0000.
UNREADABLE_YEAR_TEXT = "20X2"

# The report transactions: one card (account 00000000004 in the seeded cross-reference), the
# June row the last second before the monthly range, the July rows on both of its edges.
REPORT_CARD_NUM = "2988091353094312"
REPORT_ACCT_ID = "00000000004"
JUNE_TRAN_ID = "0000000000000100"
JULY_FIRST_TRAN_ID = "0000000000000101"
JULY_LAST_TRAN_ID = "0000000000000102"
REPORT_ROWS = (
    (JUNE_TRAN_ID, datetime(2022, 6, 30, 23, 59, 59), "S"),
    (JULY_FIRST_TRAN_ID, datetime(2022, 7, 1), "D"),
    (JULY_LAST_TRAN_ID, datetime(2022, 7, 31, 23, 59, 59, 990000), "D"),
)
TRANSACTION_FIELDS = {
    "tran_type_cd": "01",
    "tran_cat_cd": 1,
    "tran_source": "POS TERM",
    "tran_desc": "CR00 REPORT TEST",
    "tran_amt": Decimal("12.34"),
    "merchant_id": "000000001",
    "card_num": REPORT_CARD_NUM,
}
INSERT_TRANSACTION_SQL = text(
    "INSERT INTO transactions.transactions"
    " (tran_id, tran_type_cd, tran_cat_cd, tran_source, tran_desc, tran_amt, merchant_id,"
    " card_num, orig_ts, proc_ts, orig_ts_format, proc_ts_format)"
    " VALUES (:tran_id, :tran_type_cd, :tran_cat_cd, :tran_source, :tran_desc, :tran_amt,"
    " :merchant_id, :card_num, :proc_ts, :proc_ts, :ts_format, :ts_format)"
)
COUNT_TRANSACTIONS_SQL = text("SELECT count(*) FROM transactions.transactions")


class ReportScreen(NamedTuple):
    """A regular user's CR00 context, the test's monkeypatch, and TRANSACT before the request."""

    context: corpt00c.OnlineContext
    monkeypatch: pytest.MonkeyPatch
    syncEngine: Engine
    storedCount: int


def BuildQuery(overrides: Mapping[str, str]) -> ReportQuery:
    """Return the CORPT00 input: BASE_QUERY_FIELDS with overrides, both keyed by wire name."""
    return ReportQuery.model_validate({**BASE_QUERY_FIELDS, **overrides})


def FreezeClock(monkeypatch: pytest.MonkeyPatch, today: date) -> None:
    """Make ``corpt00c.CurrentDate``, the FUNCTION CURRENT-DATE seam, answer today."""

    def FrozenCurrentDate() -> date:
        return today

    monkeypatch.setattr(corpt00c, "CurrentDate", FrozenCurrentDate)


def CountTransactions(syncEngine: Engine) -> int:
    """Return the number of committed TRANSACT rows."""
    with syncEngine.connect() as connection:
        return connection.execute(COUNT_TRANSACTIONS_SQL).scalar_one()


def StoreReportTransactions(syncEngine: Engine) -> None:
    """Commit REPORT_ROWS, each with its ORIG-TS and PROC-TS set to its proc timestamp."""
    rows = [
        {**TRANSACTION_FIELDS, "tran_id": tranId, "proc_ts": procTs, "ts_format": tsFormat}
        for tranId, procTs, tsFormat in REPORT_ROWS
    ]
    with syncEngine.begin() as connection:
        connection.execute(INSERT_TRANSACTION_SQL, rows)


async def FailReportRead(
    repository: TransactionRepository, startDate: date, endDate: date
) -> NoReturn:
    """Stand in for the TRANSACT report read, failing as a CICS IOERR on TRANSACT."""
    raise FileIoError(TRANSACT_FILE_NAME)


@pytest.fixture(name="reportTransactions")
def ReportTransactionsFixture(restoreSeedState: None, syncEngine: Engine) -> None:
    """Commit the three report transactions; restoreSeedState removes them at teardown."""
    StoreReportTransactions(syncEngine)


@pytest_asyncio.fixture(name="reportScreen")
async def ReportScreenFixture(
    makeOnlineContext: Callable[..., Awaitable[corpt00c.OnlineContext]],
    reportTransactions: None,
    syncEngine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> ReportScreen:
    """Return a regular user's context with the clock at REPORT_TODAY and the rows committed."""
    FreezeClock(monkeypatch, REPORT_TODAY)
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    return ReportScreen(context, monkeypatch, syncEngine, CountTransactions(syncEngine))


async def RunReport(
    screen: ReportScreen, overrides: Mapping[str, str]
) -> TransactionReportResponse:
    """Run CR00 ENTER with BuildQuery(overrides) and return the 200 screen."""
    return await corpt00c.RunTransactionReport(screen.context, BuildQuery(overrides))


async def RunReportExpectingError(
    screen: ReportScreen, overrides: Mapping[str, str]
) -> LegacyMessageError:
    """Run CR00 ENTER with BuildQuery(overrides) and return the LegacyMessageError it raises."""
    with pytest.raises(LegacyMessageError) as errorInfo:
        await RunReport(screen, overrides)
    return errorInfo.value


def ErrorOutcome(error: LegacyMessageError) -> tuple[int, str, str]:
    """Return the status, the ERRMSG literal and the cursor of a refused request."""
    return error.httpStatus, error.message, error.field


def ScreenOutcome(response: TransactionReportResponse) -> tuple[str, str | None, str, str]:
    """Return the ERRMSG literal, the cursor and the report range of a 200 screen."""
    return response.message, response.cursor_field, response.start_date, response.end_date


def DetailLines(response: TransactionReportResponse) -> list[ReportLine]:
    """Return the TRANSACTION-DETAIL-REPORT lines of a screen, in print order."""
    return [line for line in response.lines if line.line_kind == ReportLineKind.DETAIL]


def AssertTransactsKept(screen: ReportScreen) -> None:
    """Assert TRANSACT still holds every report row: nothing was backed up or emptied."""
    assert CountTransactions(screen.syncEngine) == screen.storedCount == len(REPORT_ROWS)


# Every CORPT00 part blanked in turn, in the order of the presence EVALUATE; the cursor is the
# blank part.
BLANK_PART_CASES = (
    (CURSOR_START_MM, START_MONTH_EMPTY_MESSAGE),
    (CURSOR_START_DD, START_DAY_EMPTY_MESSAGE),
    (CURSOR_START_YYYY, START_YEAR_EMPTY_MESSAGE),
    (CURSOR_END_MM, END_MONTH_EMPTY_MESSAGE),
    (CURSOR_END_DD, END_DAY_EMPTY_MESSAGE),
    (CURSOR_END_YYYY, END_YEAR_EMPTY_MESSAGE),
)
# A part beyond its alphanumeric bound, '12' for a month and '31' for a day.
INVALID_PART_CASES = (
    (CURSOR_START_MM, "13", START_MONTH_INVALID_MESSAGE),
    (CURSOR_START_DD, "32", START_DAY_INVALID_MESSAGE),
    (CURSOR_END_MM, "13", END_MONTH_INVALID_MESSAGE),
    (CURSOR_END_DD, "32", END_DAY_INVALID_MESSAGE),
)
# An unreadable year becomes 0000, so the composed date is year 0000 (CEEDAYS message 2513).
UNREADABLE_YEAR_CASES = (
    (CURSOR_START_YYYY, "0000-01-01", CUSTOM_END_DATE),
    (CURSOR_END_YYYY, CUSTOM_START_DATE, "0000-12-31"),
)
# 30 February, a calendar-invalid date CSUTLDTC refuses (message 2508), on each date; both
# failures put the cursor on that date's month.
INVALID_DATE_CASES = (
    ({CURSOR_START_MM: "02", CURSOR_START_DD: "30"}, START_DATE_INVALID_MESSAGE, CURSOR_START_MM),
    ({CURSOR_END_MM: "02", CURSOR_END_DD: "30"}, END_DATE_INVALID_MESSAGE, CURSOR_END_MM),
)
# The Monthly range across a month end, February of a leap year and the December roll into
# the next year (lines 223-230).
MONTHLY_RANGE_CASES = (
    (REPORT_TODAY, MONTHLY_START_DATE, MONTHLY_END_DATE),
    (date(2024, 2, 10), "2024-02-01", "2024-02-29"),
    (date(2022, 12, 5), "2022-12-01", "2022-12-31"),
)


async def test_no_report_type_is_refused_with_the_select_literal(reportScreen):
    """PROCESS-ENTER-KEY WHEN OTHER: no selector answers 400 with the cursor on the type."""
    noTypeFields = {CURSOR_REPORT_TYPE: ReportType.NONE.value, CURSOR_CONFIRM: "Y"}

    error = await RunReportExpectingError(reportScreen, noTypeFields)

    expectedOutcome = (EDIT_FAILURE_STATUS, SELECT_REPORT_TYPE_MESSAGE, CURSOR_REPORT_TYPE)
    assert ErrorOutcome(error) == expectedOutcome
    AssertTransactsKept(reportScreen)


@pytest.mark.parametrize("confirmValue", ["Y", "y"])
async def test_confirmed_monthly_report_lists_only_the_current_months_rows(
    reportScreen, confirmValue
):
    """Monthly reports the first to the last of July; the June row falls outside the range."""
    response = await RunReport(reportScreen, {**MONTHLY_FIELDS, CURSOR_CONFIRM: confirmValue})

    expectedScreen = (
        MONTHLY_SUBMITTED_MESSAGE,
        CURSOR_REPORT_TYPE,
        MONTHLY_START_DATE,
        MONTHLY_END_DATE,
    )
    assert ScreenOutcome(response) == expectedScreen
    detailLines = DetailLines(response)
    assert [line.tran_id for line in detailLines] == [JULY_FIRST_TRAN_ID, JULY_LAST_TRAN_ID]
    assert {line.acct_id for line in detailLines} == {REPORT_ACCT_ID}
    AssertTransactsKept(reportScreen)


async def test_confirmed_yearly_report_lists_every_row_of_the_year(reportScreen):
    """Yearly reports 1 January to 31 December, so all three rows, in TRANSACT key order."""
    response = await RunReport(reportScreen, {**YEARLY_FIELDS, CURSOR_CONFIRM: "Y"})

    expectedScreen = (
        YEARLY_SUBMITTED_MESSAGE,
        CURSOR_REPORT_TYPE,
        YEARLY_START_DATE,
        YEARLY_END_DATE,
    )
    assert ScreenOutcome(response) == expectedScreen
    expectedTranIds = [JUNE_TRAN_ID, JULY_FIRST_TRAN_ID, JULY_LAST_TRAN_ID]
    assert [line.tran_id for line in DetailLines(response)] == expectedTranIds
    AssertTransactsKept(reportScreen)


@pytest.mark.parametrize(("today", "startDate", "endDate"), MONTHLY_RANGE_CASES)
async def test_blank_confirm_prompts_for_the_monthly_range_without_lines(
    reportScreen, today, startDate, endDate
):
    """A blank CONFIRM answers 200 with the prompt and the cursor on confirm; nothing is read."""
    FreezeClock(reportScreen.monkeypatch, today)

    response = await RunReport(reportScreen, MONTHLY_FIELDS)

    expectedScreen = (MONTHLY_PROMPT_MESSAGE, CURSOR_CONFIRM, startDate, endDate)
    assert ScreenOutcome(response) == expectedScreen
    assert response.lines == []
    AssertTransactsKept(reportScreen)


async def test_invalid_confirm_value_is_refused(reportScreen):
    """A CONFIRM other than Y, y, N, n or blank answers 400 with the value quoted."""
    invalidConfirmFields = {**MONTHLY_FIELDS, CURSOR_CONFIRM: INVALID_CONFIRM_VALUE}

    error = await RunReportExpectingError(reportScreen, invalidConfirmFields)

    expectedOutcome = (EDIT_FAILURE_STATUS, INVALID_CONFIRM_MESSAGE, CURSOR_CONFIRM)
    assert ErrorOutcome(error) == expectedOutcome
    AssertTransactsKept(reportScreen)


@pytest.mark.parametrize("confirmValue", ["N", "n"])
async def test_declined_confirm_clears_the_screen(reportScreen, confirmValue):
    """INITIALIZE-ALL-FIELDS: no message, no range, no lines, the cursor on the report type."""
    response = await RunReport(reportScreen, {**MONTHLY_FIELDS, CURSOR_CONFIRM: confirmValue})

    expectedScreen = (CLEARED_TEXT, CURSOR_REPORT_TYPE, CLEARED_TEXT, CLEARED_TEXT)
    assert ScreenOutcome(response) == expectedScreen
    assert response.lines == []
    AssertTransactsKept(reportScreen)


@pytest.mark.parametrize("blankValue", ["", "  "], ids=["empty", "spaces"])
@pytest.mark.parametrize(("blankPart", "expectedMessage"), BLANK_PART_CASES)
async def test_blank_custom_date_part_is_refused(
    reportScreen, blankPart, expectedMessage, blankValue
):
    """SPACES OR LOW-VALUES in one part answers that part's literal, with the cursor on it."""
    error = await RunReportExpectingError(reportScreen, {blankPart: blankValue})

    assert ErrorOutcome(error) == (EDIT_FAILURE_STATUS, expectedMessage, blankPart)
    AssertTransactsKept(reportScreen)


async def test_presence_checks_run_start_month_first(reportScreen):
    """With every part blank, the first WHEN of the presence EVALUATE answers."""
    allBlankFields = dict.fromkeys(DATE_PART_NAMES, "")

    error = await RunReportExpectingError(reportScreen, allBlankFields)

    expectedOutcome = (EDIT_FAILURE_STATUS, START_MONTH_EMPTY_MESSAGE, CURSOR_START_MM)
    assert ErrorOutcome(error) == expectedOutcome
    AssertTransactsKept(reportScreen)


@pytest.mark.parametrize(("invalidPart", "partText", "expectedMessage"), INVALID_PART_CASES)
async def test_custom_part_beyond_its_bound_is_refused(
    reportScreen, invalidPart, partText, expectedMessage
):
    """A month above '12' or a day above '31' answers its Not a valid literal, cursor on it."""
    error = await RunReportExpectingError(reportScreen, {invalidPart: partText})

    assert ErrorOutcome(error) == (EDIT_FAILURE_STATUS, expectedMessage, invalidPart)
    AssertTransactsKept(reportScreen)


# The Not a valid Year literals are unreachable, as in CORPT00C: lines 313-315 and 325-327 move
# the NUMVAL-C result back into SDTYYYYI and EDTYYYYI before the NOT NUMERIC tests of lines 347
# and 373, so an unreadable year arrives as the digits 0000, and CSUTLDTC then accepts year
# 0000 with message 2513.
@pytest.mark.parametrize(("yearPart", "startDate", "endDate"), UNREADABLE_YEAR_CASES)
async def test_unreadable_custom_year_becomes_year_zero_and_passes(
    reportScreen, yearPart, startDate, endDate
):
    """An unreadable year is normalised to 0000 and reaches the confirmation prompt."""
    response = await RunReport(reportScreen, {yearPart: UNREADABLE_YEAR_TEXT})

    assert ScreenOutcome(response) == (CUSTOM_PROMPT_MESSAGE, CURSOR_CONFIRM, startDate, endDate)
    assert response.lines == []
    AssertTransactsKept(reportScreen)


@pytest.mark.parametrize(("dateFields", "expectedMessage", "expectedCursor"), INVALID_DATE_CASES)
async def test_calendar_invalid_custom_date_is_refused(
    reportScreen, dateFields, expectedMessage, expectedCursor
):
    """CSUTLDTC refuses 2022-02-30; the start date is checked first, each cursor on its month."""
    error = await RunReportExpectingError(reportScreen, dateFields)

    assert ErrorOutcome(error) == (EDIT_FAILURE_STATUS, expectedMessage, expectedCursor)
    AssertTransactsKept(reportScreen)


async def test_unsupported_range_message_2513_is_accepted(reportScreen):
    """A start before 1582-10-15 fails CEEDAYS with 2513, which CORPT00C lets through."""
    checkResult = CheckDate(PRE_LILIAN_START_DATE, ISO_DATE_MASK)
    assert checkResult.messageNumber == UNSUPPORTED_RANGE_MESSAGE_NUMBER

    response = await RunReport(reportScreen, PRE_LILIAN_START_FIELDS)

    expectedScreen = (CUSTOM_PROMPT_MESSAGE, CURSOR_CONFIRM, PRE_LILIAN_START_DATE, CUSTOM_END_DATE)
    assert ScreenOutcome(response) == expectedScreen
    assert response.lines == []
    AssertTransactsKept(reportScreen)


async def test_failed_transact_read_answers_the_tdq_write_literal(reportScreen):
    """A repository failure of the report read answers 503 with the WHEN OTHER literal."""
    reportScreen.monkeypatch.setattr(TransactionRepository, "GetReportTransactions", FailReportRead)

    error = await RunReportExpectingError(reportScreen, {**MONTHLY_FIELDS, CURSOR_CONFIRM: "Y"})

    expectedOutcome = (FILE_ERROR_STATUS, UNABLE_TO_WRITE_TDQ_MESSAGE, CURSOR_REPORT_TYPE)
    assert ErrorOutcome(error) == expectedOutcome
    AssertTransactsKept(reportScreen)
