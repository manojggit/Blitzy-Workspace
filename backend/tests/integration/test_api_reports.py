"""Integration tests of CR00 (CORPT00C, map CORPT00): the transaction report and its exports.

Replaces: CORPT00C (CR00), BMS CORPT00; CBTRN03C report lines (copybook CVTRA07Y); proc
TRANREPT (superseded TDQ job).

Every request goes through the ASGI app to the test database, signed on with the clients of
tests/conftest.py, and restoreSeedState undoes every committed row. The cases cover:

- the Monthly, Yearly and Custom ranges of PROCESS-ENTER-KEY (cbl/CORPT00C.cbl lines 212-436),
  with today frozen at 2022-07-18 through the ``corpt00c.CurrentDate`` seam;
- the TRANREPT STEP05R INCLUDE on PROC-TS positions 305-314 (proc/TRANREPT.prc): both range
  ends are inclusive, and a blank (NULL) PROC-TS never reaches the report;
- the CBTRN03C line sequence: the headers, the details in card order and then EBCDIC TRAN-ID
  order with the cross-reference account and the TRANTYPE and TRANCATG descriptions, a page
  total that holds the last amount twice, and the grand total;
- the first-failure literals and cursors of the report type, the date parts, CSUTLDTC and the
  confirmation (lines 258-494), and the 200 confirmation prompt and cleared screen;
- the CSV and PDF downloads, which carry the same lines, guard formula text and run the same
  edits;
- the superseded TDQ job submission and its TRANREPT STEP01R backup: the screen and both
  downloads leave every TRANSACT row and column, and the batch catalog and ledger
  (batch.gdg_generations, job_runs, cycles, cycle_steps), unchanged, create no TRANSACT.BKUP
  archive directory under an isolated batch output root, and send PostgreSQL only SELECTs
  through the application engine;
- the session and the regular role every CR00 route requires.
"""

import contextlib
import csv
import io
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple

import httpx
import pytest
from batch.config import LoadBatchSettings
from fastapi import FastAPI
from sqlalchemy import Connection, Engine, Table, event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import CYCLE_STEPS, CYCLES, GDG_GENERATIONS, JOB_RUNS, Transaction
from app.services.reports import corpt00c

pytestmark = pytest.mark.usefixtures("restoreSeedState")

API_V1 = "/api/v1"
REPORT_URL = API_V1 + "/reports/transactions"
EXPORT_URL = REPORT_URL + "/export"
HTTP_OK = 200
HTTP_EDIT_FAILURE = 400
HTTP_SIGN_ON_FAILURE = 401
HTTP_ACCESS_REFUSED = 403
# The 401 session refusal and the 403 refusal of an admin carry no message.
NO_MESSAGE = ""

# FUNCTION CURRENT-DATE as the tests see it, and a day in a month holding no report row.
FROZEN_TODAY = "2022-07-18"
EMPTY_MONTH_TODAY = "2022-08-10"

# --- Message literals of cbl/CORPT00C.cbl ---------------------------------------------------------
# WHEN OTHER of the report-type EVALUATE (line 438).
SELECT_REPORT_TYPE_MESSAGE = "Select a report type to print report..."
# The presence EVALUATE of the Custom branch (lines 258-303), in source order, each with the
# CORPT00 field its MOVE -1 TO <field>L marks.
DATE_PART_MESSAGES = (
    ("startMm", "Start Date - Month can NOT be empty..."),
    ("startDd", "Start Date - Day can NOT be empty..."),
    ("startYyyy", "Start Date - Year can NOT be empty..."),
    ("endMm", "End Date - Month can NOT be empty..."),
    ("endDd", "End Date - Day can NOT be empty..."),
    ("endYyyy", "End Date - Year can NOT be empty..."),
)
DATE_PART_NAMES = tuple(partName for partName, partMessage in DATE_PART_MESSAGES)
# The part checks after NUMVAL-C (lines 329-379).
START_MONTH_INVALID_MESSAGE = "Start Date - Not a valid Month..."
START_DAY_INVALID_MESSAGE = "Start Date - Not a valid Day..."
END_MONTH_INVALID_MESSAGE = "End Date - Not a valid Month..."
END_DAY_INVALID_MESSAGE = "End Date - Not a valid Day..."
# The tail of both year literals. Each year is moved through NUMVAL-C into PIC 9999 (lines
# 313-315 and 325-327) before its NOT NUMERIC test (lines 347 and 373), so no keyed year
# reaches either literal.
YEAR_INVALID_LITERAL_TAIL = "Not a valid Year..."
# The CSUTLDTC checks of the composed dates (lines 400 and 420).
START_DATE_INVALID_MESSAGE = "Start Date - Not a valid date..."
END_DATE_INVALID_MESSAGE = "End Date - Not a valid date..."
# SUBMIT-JOB-TO-INTRDR (lines 464-494) and the success STRING (lines 449-452).
CONFIRM_PROMPT_TEMPLATE = "Please confirm to print the {reportName} report..."
REPORT_SUBMITTED_TEMPLATE = "{reportName} report submitted for printing ..."
INVALID_CONFIRM_VALUE = "X"
INVALID_CONFIRM_MESSAGE = '"X" is not a valid value to confirm...'

# The ReportQuery wire names CR00 marks, replacing MOVE -1 TO MONTHLYL and CONFIRML.
CURSOR_REPORT_TYPE = "reportType"
CURSOR_CONFIRM = "confirm"

# --- Requests ------------------------------------------------------------------------------------
MONTHLY_CONFIRMED = {"reportType": "monthly", "confirm": "Y"}
YEARLY_CONFIRMED = {"reportType": "yearly", "confirm": "Y"}
CUSTOM_DATE_PARTS = {
    "startMm": "06",
    "startDd": "30",
    "startYyyy": "2022",
    "endMm": "07",
    "endDd": "05",
    "endYyyy": "2022",
}
# A part keyed as SPACES, which the presence EVALUATE treats as empty.
BLANK_PART = " "
EXPORT_FORMATS = ("csv", "pdf")

# --- Report transactions -------------------------------------------------------------------------
# The seeded card of account 00000000004 (the CARDDATA and CARDXREF images), type 01 Purchase
# and category 0001 Regular Sales Draft, all on file in TRANTYPE and TRANCATG.
REPORT_CARD_NUM = "2988091353094312"
REPORT_ACCT_ID = "00000000004"
REPORT_TYPE_CD = "01"
REPORT_CAT_CD = 1
REPORT_DESCRIPTION = "CR00 REPORT TEST"
DEFAULT_SOURCE = "POS TERM"
MERCHANT_ID = "000000001"
MERCHANT_NAME = "REPORT MERCHANT"
MERCHANT_CITY = "REPORT CITY"
MERCHANT_ZIP = "00000"
ORIG_TS = datetime(2022, 6, 10, 19, 27, 53)
ORIG_TS_FORMAT = "S"
# POSTTRAN's YYYY-MM-DD-HH.MM.SS.cc0000 form, and the 26 spaces of a blank PROC-TS.
PROC_TS_FORMAT = "D"
BLANK_TS_FORMAT = " "

# TRAN-IDs whose EBCDIC key order (letters before digits) differs from their ASCII order.
JULY_05_TRAN_ID = "RPTA000000000705"
JUNE_30_TRAN_ID = "RPTB000000000630"
PRIOR_YEAR_TRAN_ID = "RPTC000000001231"
UNPROCESSED_TRAN_ID = "RPTD000000000000"
FORMULA_TRAN_ID = "RPTE000000000710"
JULY_18_TRAN_ID = "RPT1000000000718"
FORMULA_SOURCE = "=SUM(A1)"
GUARDED_FORMULA_SOURCE = "'=SUM(A1)"


@dataclass(frozen=True)
class ReportTransaction:
    """One posted transaction on REPORT_CARD_NUM; ``procTs`` is None for a blank PROC-TS."""

    tranId: str
    procTs: datetime | None
    amount: Decimal
    source: str = DEFAULT_SOURCE


# July 5 closes the custom range at its last hundredth and June 30 opens it at midnight; the
# 2021 row ends the day before the yearly range, and the NULL row is never reported.
REPORT_TRANSACTIONS = (
    ReportTransaction(JULY_18_TRAN_ID, datetime(2022, 7, 18, 12, 30, 45, 670000), Decimal("20.50")),
    ReportTransaction(JULY_05_TRAN_ID, datetime(2022, 7, 5, 23, 59, 59, 990000), Decimal("10.00")),
    ReportTransaction(JUNE_30_TRAN_ID, datetime(2022, 6, 30), Decimal("5.00")),
    ReportTransaction(
        PRIOR_YEAR_TRAN_ID, datetime(2021, 12, 31, 23, 59, 59, 990000), Decimal("7.00")
    ),
    ReportTransaction(UNPROCESSED_TRAN_ID, None, Decimal("3.00")),
)
FORMULA_TRANSACTION = ReportTransaction(
    FORMULA_TRAN_ID, datetime(2022, 7, 10, 8, 0), Decimal("1.00"), FORMULA_SOURCE
)

# --- Expected CBTRN03C lines ---------------------------------------------------------------------
REPORT_NAME = "DALYREPT"
REPORT_TITLE = "Daily Transaction Report"
DATE_RANGE_TEMPLATE = "Date Range: {startDate} to {endDate}"
HEADER_KIND = "header"
DETAIL_KIND = "detail"
PAGE_TOTAL_KIND = "page_total"
GRAND_TOTAL_KIND = "grand_total"
# 1120-WRITE-HEADERS (four lines), two details, then the end-of-file page total, its dash
# rule and the grand total; one card, so no account total is ever written.
MONTHLY_LINE_KINDS = [
    HEADER_KIND,
    HEADER_KIND,
    HEADER_KIND,
    HEADER_KIND,
    DETAIL_KIND,
    DETAIL_KIND,
    PAGE_TOTAL_KIND,
    HEADER_KIND,
    GRAND_TOTAL_KIND,
]
MONTHLY_DETAIL_AMOUNTS = [Decimal("10.00"), Decimal("20.50")]
# 10.00 + 20.50, plus the last amount added again at end of file (cbl/CBTRN03C.cbl lines 196-202).
MONTHLY_TOTAL = Decimal("51.00")
# TRAN-REPORT-TYPE-DESC X(15) and TRAN-REPORT-CAT-DESC X(29) (cpy/CVTRA07Y.cpy).
TYPE_DESC_WIDTH = 15
CAT_DESC_WIDTH = 29

# --- Expected downloads --------------------------------------------------------------------------
CSV_HEADER = "line_kind,tran_id,acct_id,type_cd,type_desc,cat_cd,cat_desc,source,amount"
CSV_COLUMN_NAMES = CSV_HEADER.split(",")
CSV_CONTENT_TYPE = "text/csv"
PDF_CONTENT_TYPE = "application/pdf"
PDF_SIGNATURE = b"%PDF"
MONTHLY_DISPOSITION_TEMPLATE = 'attachment; filename="TRANREPT_2022-07-01_2022-07-31.{extension}"'
PLAIN_AMOUNT_PATTERN = re.compile(r"-?\d+\.\d{2}")
CAT_CD_DIGITS = 4

# --- SQL: values are always bound ----------------------------------------------------------------
INSERT_TRANSACTION_SQL = text(
    "INSERT INTO transactions.transactions (tran_id, tran_type_cd, tran_cat_cd, tran_source,"
    " tran_desc, tran_amt, merchant_id, merchant_name, merchant_city, merchant_zip, card_num,"
    " orig_ts, proc_ts, orig_ts_format, proc_ts_format) VALUES (:tranId, :typeCd, :catCd,"
    " :source, :description, :amount, :merchantId, :merchantName, :merchantCity, :merchantZip,"
    " :cardNum, :origTs, :procTs, :origTsFormat, :procTsFormat)"
)
TYPE_DESCRIPTION_SQL = text(
    "SELECT description FROM reference.transaction_types WHERE tran_type = :typeCd"
)
CATEGORY_DESCRIPTION_SQL = text(
    "SELECT description FROM reference.transaction_categories"
    " WHERE tran_type_cd = :typeCd AND tran_cat_cd = :catCd"
)

# --- The superseded backup -----------------------------------------------------------------------
# TRANREPT STEP01R unloaded TRANSACT to AWS.M2.CARDDEMO.TRANSACT.BKUP(+1) (proc/TRANREPT.prc): a
# backup would add a TRANSACT.BKUP generation to the batch catalog, a job run to its ledger and
# an archive under the base's directory of the batch output root.
TRANSACT_BACKUP_BASE = "TRANSACT.BKUP"
OUTPUT_DIR_VARIABLE = "CARDDEMO_OUTPUT_DIR"
OUTPUT_ROOT_NAME = "batch-output"
TRANSACT_TABLE = Transaction.__table__
STORED_STATE_TABLES = (TRANSACT_TABLE, CYCLES, JOB_RUNS, CYCLE_STEPS, GDG_GENERATIONS)
# The engine event every Core and ORM statement passes on its way to the driver, and the verb of
# the only statements a report may send.
CURSOR_EXECUTE_EVENT = "before_cursor_execute"
READ_STATEMENT_VERB = "SELECT"

type TableRows = tuple[tuple[Any, ...], ...]


class RangeCase(NamedTuple):
    """A confirmed report request, the range CORPT00C derives and the TRAN-IDs it reports."""

    query: dict[str, str]
    reportName: str
    startDate: str
    endDate: str
    tranIds: tuple[str, ...]


class EditCase(NamedTuple):
    """A request field keyed with ``value`` and the literal and cursor the edit answers."""

    fieldName: str
    value: str
    message: str
    cursorField: str


class YearCase(NamedTuple):
    """A non-numeric year part, the range its NUMVAL-C value gives and the TRAN-IDs reported."""

    fieldName: str
    startDate: str
    endDate: str
    tranIds: tuple[str, ...]


# --- Request and data helpers --------------------------------------------------------------------
def CustomQuery(overrides: dict[str, str]) -> dict[str, str]:
    """Return a confirmed Custom request over 06/30/2022 to 07/05/2022 with ``overrides``."""
    return {"reportType": "custom", **CUSTOM_DATE_PARTS, "confirm": "Y", **overrides}


def BlankedPartsQuery(partName: str) -> dict[str, str]:
    """Return the confirmed Custom request with ``partName`` keyed as spaces, later parts empty.

    Every part after ``partName`` is blank as well, so its literal can only win when the
    presence EVALUATE tests the parts in source order.
    """
    laterParts = DATE_PART_NAMES[DATE_PART_NAMES.index(partName) + 1 :]
    blankedParts = {laterPart: "" for laterPart in laterParts}
    return CustomQuery({**blankedParts, partName: BLANK_PART})


async def RunReport(client: httpx.AsyncClient, query: dict[str, str]) -> httpx.Response:
    """Send ENTER on CORPT00: GET /api/v1/reports/transactions with ``query``."""
    return await client.get(REPORT_URL, params=query)


async def DownloadReport(
    client: httpx.AsyncClient, query: dict[str, str], exportFormat: str
) -> httpx.Response:
    """Download the report of ``query`` as ``exportFormat`` (csv or pdf)."""
    return await client.get(EXPORT_URL, params={**query, "format": exportFormat})


def FreezeToday(monkeypatch: pytest.MonkeyPatch, isoDate: str) -> None:
    """Make ``corpt00c.CurrentDate`` (FUNCTION CURRENT-DATE) return ``isoDate``."""
    frozenDate = date.fromisoformat(isoDate)
    monkeypatch.setattr(corpt00c, "CurrentDate", lambda: frozenDate)


def TransactionParameters(row: ReportTransaction) -> dict[str, object]:
    """Return the bound INSERT values of ``row``: type 01, category 1, PROC-TS form D or blank."""
    return {
        "tranId": row.tranId,
        "typeCd": REPORT_TYPE_CD,
        "catCd": REPORT_CAT_CD,
        "source": row.source,
        "description": REPORT_DESCRIPTION,
        "amount": row.amount,
        "merchantId": MERCHANT_ID,
        "merchantName": MERCHANT_NAME,
        "merchantCity": MERCHANT_CITY,
        "merchantZip": MERCHANT_ZIP,
        "cardNum": REPORT_CARD_NUM,
        "origTs": ORIG_TS,
        "procTs": row.procTs,
        "origTsFormat": ORIG_TS_FORMAT,
        "procTsFormat": BLANK_TS_FORMAT if row.procTs is None else PROC_TS_FORMAT,
    }


def InsertReportTransactions(syncEngine: Engine, rows: Sequence[ReportTransaction]) -> None:
    """Commit ``rows`` to transactions.transactions on card REPORT_CARD_NUM."""
    with syncEngine.begin() as connection:
        connection.execute(INSERT_TRANSACTION_SQL, [TransactionParameters(row) for row in rows])


def ReadTableRows(connection: Connection, table: Table) -> TableRows:
    """Return every row of ``table``, every column included, in primary-key order."""
    statement = select(table).order_by(*table.primary_key.columns)
    return tuple(tuple(row) for row in connection.execute(statement))


def ReadStoredState(syncEngine: Engine) -> dict[str, TableRows]:
    """Return the rows of TRANSACT and of the batch catalog and ledger, keyed by table name."""
    with syncEngine.connect() as connection:
        return {table.fullname: ReadTableRows(connection, table) for table in STORED_STATE_TABLES}


@contextlib.contextmanager
def RecordedStatements(engine: AsyncEngine) -> Iterator[list[str]]:
    """Yield the SQL of every statement ``engine`` sends PostgreSQL while the block runs.

    The listener sits on the engine's sync_engine, which every statement of the async engine
    passes, and is removed however the block exits.
    """
    issuedStatements: list[str] = []

    def RecordStatement(**eventArguments: Any) -> None:
        issuedStatements.append(eventArguments["statement"])

    event.listen(engine.sync_engine, CURSOR_EXECUTE_EVENT, RecordStatement, named=True)
    try:
        yield issuedStatements
    finally:
        event.remove(engine.sync_engine, CURSOR_EXECUTE_EVENT, RecordStatement)


def WriteStatements(issuedStatements: list[str]) -> list[str]:
    """Return the statements whose first word is not SELECT, an empty statement included."""
    return [
        statement
        for statement in issuedStatements
        if statement.upper().split(maxsplit=1)[:1] != [READ_STATEMENT_VERB]
    ]


async def RequestReportAndDownloads(client: httpx.AsyncClient, query: dict[str, str]) -> None:
    """Send ``query`` to the screen and to both downloads, asserting each answers 200."""
    screenResponse = await RunReport(client, query)
    assert screenResponse.status_code == HTTP_OK, screenResponse.text
    for exportFormat in EXPORT_FORMATS:
        exportResponse = await DownloadReport(client, query, exportFormat)
        assert exportResponse.status_code == HTTP_OK, exportResponse.text


def ReadReferenceDescriptions(syncEngine: Engine) -> tuple[str, str]:
    """Return the TRANTYPE and TRANCATG descriptions of type 01, category 0001."""
    keyValues = {"typeCd": REPORT_TYPE_CD, "catCd": REPORT_CAT_CD}
    with syncEngine.connect() as connection:
        typeDescription = connection.execute(TYPE_DESCRIPTION_SQL, keyValues).scalar_one()
        categoryDescription = connection.execute(CATEGORY_DESCRIPTION_SQL, keyValues).scalar_one()
    return typeDescription, categoryDescription


def ParseCsv(csvText: str) -> list[list[str]]:
    """Return the rows of a CSV document, header row included."""
    return list(csv.reader(io.StringIO(csvText)))


def LinesOfKind(body: dict[str, Any], lineKind: str) -> list[dict[str, Any]]:
    """Return the report lines of ``body`` whose line_kind is ``lineKind``, in print order."""
    return [line for line in body["lines"] if line["line_kind"] == lineKind]


def DetailTranIds(body: dict[str, Any]) -> list[str]:
    """Return the TRAN-IDs of the detail lines of ``body``, in print order."""
    return [line["tran_id"] for line in LinesOfKind(body, DETAIL_KIND)]


# --- Assertions ----------------------------------------------------------------------------------
def AssertLegacyError(
    response: httpx.Response, status: int, message: str, cursorField: str = ""
) -> None:
    """Assert a legacy error body: ``message`` verbatim, and ``cursorField`` as the only field.

    CR00 names its fields by their ReportQuery wire names; a body naming no field has an empty
    cursor and no field errors.
    """
    assert response.status_code == status, response.text
    body = response.json()
    assert body["message"] == message
    assert body["info_message"] == ""
    assert body["cursor_field"] == cursorField
    assert body["field_errors"] == ([cursorField] if cursorField else [])


def AssertSubmitted(response: httpx.Response, rangeCase: RangeCase) -> None:
    """Assert the 200 success screen of ``rangeCase``: its literal, range and report-type cursor."""
    assert response.status_code == HTTP_OK, response.text
    body = response.json()
    assert body["message"] == REPORT_SUBMITTED_TEMPLATE.format(reportName=rangeCase.reportName)
    assert body["start_date"] == rangeCase.startDate
    assert body["end_date"] == rangeCase.endDate
    assert body["cursor_field"] == CURSOR_REPORT_TYPE


def AssertReportHeader(body: dict[str, Any], rangeCase: RangeCase) -> None:
    """Assert REPORT-NAME-HEADER opens the report and prints the range of ``rangeCase``."""
    nameHeader = body["lines"][0]
    dateRange = DATE_RANGE_TEMPLATE.format(startDate=rangeCase.startDate, endDate=rangeCase.endDate)
    assert nameHeader["line_kind"] == HEADER_KIND
    assert nameHeader["text"].startswith(REPORT_NAME)
    assert REPORT_TITLE in nameHeader["text"]
    assert dateRange in nameHeader["text"]


def AssertDetailFields(body: dict[str, Any], descriptions: tuple[str, str]) -> None:
    """Assert every detail line shows the xref account and the reference descriptions."""
    typeDescription, categoryDescription = descriptions
    assert typeDescription and categoryDescription
    for detailLine in LinesOfKind(body, DETAIL_KIND):
        assert detailLine["acct_id"] == REPORT_ACCT_ID
        assert detailLine["type_cd"] == REPORT_TYPE_CD
        assert detailLine["type_desc"] == typeDescription[:TYPE_DESC_WIDTH].rstrip()
        assert detailLine["cat_cd"] == REPORT_CAT_CD
        assert detailLine["cat_desc"] == categoryDescription[:CAT_DESC_WIDTH].rstrip()
        assert detailLine["source"] == DEFAULT_SOURCE


def AssertCsvRowMatchesLine(csvRow: list[str], line: dict[str, Any]) -> None:
    """Assert one CSV row carries the fields of its report line, the amount as a plain decimal."""
    cells = dict(zip(CSV_COLUMN_NAMES, csvRow, strict=True))
    expectedCatCd = "" if line["cat_cd"] is None else f"{line['cat_cd']:0{CAT_CD_DIGITS}d}"
    expectedAmount = "" if line["amount"] is None else line["amount"]
    assert cells["line_kind"] == line["line_kind"]
    assert cells["tran_id"] == line["tran_id"]
    assert cells["acct_id"] == line["acct_id"]
    assert cells["cat_cd"] == expectedCatCd
    assert cells["amount"] == expectedAmount
    assert cells["amount"] == "" or PLAIN_AMOUNT_PATTERN.fullmatch(cells["amount"])


# --- Fixtures ------------------------------------------------------------------------------------
@pytest.fixture(name="reportData")
def ReportDataFixture(
    syncEngine: Engine, monkeypatch: pytest.MonkeyPatch, restoreSeedState: None
) -> None:
    """Commit REPORT_TRANSACTIONS and freeze today at FROZEN_TODAY."""
    InsertReportTransactions(syncEngine, REPORT_TRANSACTIONS)
    FreezeToday(monkeypatch, FROZEN_TODAY)


@pytest.fixture(name="backupDirectory")
def BackupDirectoryFixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the batch output root under tmp_path; return its TRANSACT.BKUP base directory.

    A backup written through the batch settings would create that directory, so its absence
    shows no archive was written, and the shared var/ tree is never touched.
    """
    monkeypatch.setenv(OUTPUT_DIR_VARIABLE, str(tmp_path / OUTPUT_ROOT_NAME))
    return LoadBatchSettings().GdgDir() / TRANSACT_BACKUP_BASE


# --- Cases ---------------------------------------------------------------------------------------
RANGE_CASES = [
    pytest.param(
        RangeCase(
            MONTHLY_CONFIRMED,
            "Monthly",
            "2022-07-01",
            "2022-07-31",
            (JULY_05_TRAN_ID, JULY_18_TRAN_ID),
        ),
        id="monthly",
    ),
    pytest.param(
        RangeCase(
            YEARLY_CONFIRMED,
            "Yearly",
            "2022-01-01",
            "2022-12-31",
            (JULY_05_TRAN_ID, JUNE_30_TRAN_ID, JULY_18_TRAN_ID),
        ),
        id="yearly",
    ),
    pytest.param(
        RangeCase(
            CustomQuery({}),
            "Custom",
            "2022-06-30",
            "2022-07-05",
            (JULY_05_TRAN_ID, JUNE_30_TRAN_ID),
        ),
        id="custom-inclusive",
    ),
]
MISSING_REPORT_TYPE_QUERIES = [
    pytest.param({}, id="empty-screen"),
    pytest.param({**CUSTOM_DATE_PARTS, "confirm": "Y"}, id="dates-without-type"),
]
INVALID_PART_CASES = [
    pytest.param(EditCase("startMm", "13", START_MONTH_INVALID_MESSAGE, "startMm"), id="start-mm"),
    pytest.param(EditCase("startDd", "32", START_DAY_INVALID_MESSAGE, "startDd"), id="start-dd"),
    pytest.param(EditCase("endMm", "13", END_MONTH_INVALID_MESSAGE, "endMm"), id="end-mm"),
    pytest.param(EditCase("endDd", "32", END_DAY_INVALID_MESSAGE, "endDd"), id="end-dd"),
]
# A month of 00 passes the part check (not above '12') and fails CSUTLDTC instead. Both date
# failures put the cursor on the month part (MOVE -1 TO SDTMML / EDTMML).
INVALID_DATE_CASES = [
    pytest.param(
        ({"startMm": "02", "startDd": "30"}, START_DATE_INVALID_MESSAGE, "startMm"), id="start"
    ),
    pytest.param(({"endMm": "02", "endDd": "30"}, END_DATE_INVALID_MESSAGE, "endMm"), id="end"),
    pytest.param(({"startMm": "00"}, START_DATE_INVALID_MESSAGE, "startMm"), id="start-month-00"),
]
# NUMVAL-C of ABCD stores 0000; CSUTLDTC answers year 0000 with message 2513, which CORPT00C
# accepts. A year-0000 start reaches back to the earliest row, and a year-0000 end reaches none.
YEAR_CASES = [
    pytest.param(
        YearCase(
            "startYyyy",
            "0000-06-30",
            "2022-07-05",
            (JULY_05_TRAN_ID, JUNE_30_TRAN_ID, PRIOR_YEAR_TRAN_ID),
        ),
        id="start-year",
    ),
    pytest.param(YearCase("endYyyy", "2022-06-30", "0000-07-05", ()), id="end-year"),
]
REFUSAL_TARGETS = [
    pytest.param((REPORT_URL, {}), id="screen"),
    pytest.param((EXPORT_URL, {"format": "csv"}), id="csv"),
    pytest.param((EXPORT_URL, {"format": "pdf"}), id="pdf"),
]
EXPORT_EDIT_FAILURE_QUERIES = [
    pytest.param({"confirm": "Y"}, id="no-report-type"),
    pytest.param(CustomQuery({"startMm": "13"}), id="invalid-month"),
    pytest.param({"reportType": "monthly", "confirm": INVALID_CONFIRM_VALUE}, id="invalid-confirm"),
]
UNCONFIRMED_EXPORT_CASES = [
    pytest.param(
        (
            {"reportType": "monthly"},
            CONFIRM_PROMPT_TEMPLATE.format(reportName="Monthly"),
            CURSOR_CONFIRM,
        ),
        id="blank-confirm",
    ),
    pytest.param(({"reportType": "monthly", "confirm": "N"}, "", CURSOR_REPORT_TYPE), id="n"),
]


# --- Ranges and success --------------------------------------------------------------------------
@pytest.mark.usefixtures("reportData")
@pytest.mark.parametrize("rangeCase", RANGE_CASES)
async def test_blank_confirmation_prompts_without_lines(
    regularClient: httpx.AsyncClient, rangeCase: RangeCase
) -> None:
    """A blank CONFIRM answers 200 with the prompt, the range, no lines and the confirm cursor."""
    response = await RunReport(regularClient, {**rangeCase.query, "confirm": ""})
    assert response.status_code == HTTP_OK, response.text
    assert response.json() == {
        "start_date": rangeCase.startDate,
        "end_date": rangeCase.endDate,
        "message": CONFIRM_PROMPT_TEMPLATE.format(reportName=rangeCase.reportName),
        "cursor_field": CURSOR_CONFIRM,
        "lines": [],
    }


@pytest.mark.usefixtures("reportData")
@pytest.mark.parametrize("rangeCase", RANGE_CASES)
async def test_confirmed_range_reports_exactly_its_transactions(
    regularClient: httpx.AsyncClient, rangeCase: RangeCase
) -> None:
    """Y builds the report over the range, both ends included, in EBCDIC TRAN-ID order.

    The blank PROC-TS row and every row outside the range are absent.
    """
    response = await RunReport(regularClient, rangeCase.query)
    AssertSubmitted(response, rangeCase)
    body = response.json()
    AssertReportHeader(body, rangeCase)
    assert DetailTranIds(body) == list(rangeCase.tranIds)
    reportedIds = {line["tran_id"] for line in body["lines"]}
    assert UNPROCESSED_TRAN_ID not in reportedIds


@pytest.mark.usefixtures("reportData")
async def test_monthly_report_follows_the_cbtrn03c_line_sequence(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """Headers, details with xref account and descriptions, then the page and grand totals.

    The totals hold the last detail amount twice, as the CBTRN03C end-of-file branch adds it.
    """
    body = (await RunReport(regularClient, MONTHLY_CONFIRMED)).json()
    assert [line["line_kind"] for line in body["lines"]] == MONTHLY_LINE_KINDS
    AssertDetailFields(body, ReadReferenceDescriptions(syncEngine))
    detailAmounts = [Decimal(line["amount"]) for line in LinesOfKind(body, DETAIL_KIND)]
    assert detailAmounts == MONTHLY_DETAIL_AMOUNTS
    for totalKind in (PAGE_TOTAL_KIND, GRAND_TOTAL_KIND):
        totalLine = LinesOfKind(body, totalKind)[0]
        assert Decimal(totalLine["amount"]) == MONTHLY_TOTAL
        assert totalLine["tran_id"] == ""


@pytest.mark.usefixtures("reportData")
async def test_confirmed_range_without_transactions_has_no_lines(
    regularClient: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A confirmed month holding no transaction answers the success literal and no lines."""
    FreezeToday(monkeypatch, EMPTY_MONTH_TODAY)
    response = await RunReport(regularClient, MONTHLY_CONFIRMED)
    AssertSubmitted(
        response, RangeCase(MONTHLY_CONFIRMED, "Monthly", "2022-08-01", "2022-08-31", ())
    )
    assert response.json()["lines"] == []


@pytest.mark.usefixtures("reportData")
async def test_report_and_exports_leave_transact_unchanged(
    regularClient: httpx.AsyncClient, syncEngine: Engine, testApp: FastAPI, backupDirectory: Path
) -> None:
    """The superseded TRANREPT backup: the screen and both downloads only read.

    TRANSACT keeps every row with every column; the batch cycles, job runs, cycle steps and GDG
    catalog are unchanged, so no TRANSACT.BKUP generation or job run was recorded; no
    TRANSACT.BKUP archive directory exists; and the application engine sent PostgreSQL no
    statement but a SELECT, the TRANSACT read among them.
    """
    storedBefore = ReadStoredState(syncEngine)
    with RecordedStatements(testApp.state.engine) as issuedStatements:
        await RequestReportAndDownloads(regularClient, YEARLY_CONFIRMED)
    assert len(storedBefore[TRANSACT_TABLE.fullname]) == len(REPORT_TRANSACTIONS)
    assert ReadStoredState(syncEngine) == storedBefore
    assert not backupDirectory.exists()
    assert any(TRANSACT_TABLE.fullname in statement for statement in issuedStatements)
    assert WriteStatements(issuedStatements) == []


# --- First-failure literals ----------------------------------------------------------------------
@pytest.mark.parametrize("query", MISSING_REPORT_TYPE_QUERIES)
async def test_missing_report_type_is_refused(
    regularClient: httpx.AsyncClient, query: dict[str, str]
) -> None:
    """No report type selected: the WHEN OTHER literal with the cursor on the report type."""
    response = await RunReport(regularClient, query)
    AssertLegacyError(response, HTTP_EDIT_FAILURE, SELECT_REPORT_TYPE_MESSAGE, CURSOR_REPORT_TYPE)


@pytest.mark.parametrize(("partName", "message"), DATE_PART_MESSAGES)
async def test_blank_custom_date_part_is_refused_in_source_order(
    regularClient: httpx.AsyncClient, partName: str, message: str
) -> None:
    """A blank part answers its own literal and cursor, ahead of every later blank part."""
    response = await RunReport(regularClient, BlankedPartsQuery(partName))
    AssertLegacyError(response, HTTP_EDIT_FAILURE, message, partName)


@pytest.mark.parametrize("editCase", INVALID_PART_CASES)
async def test_out_of_range_date_part_is_refused_with_its_cursor(
    regularClient: httpx.AsyncClient, editCase: EditCase
) -> None:
    """A month above '12' or a day above '31' answers the part's literal on that part."""
    response = await RunReport(regularClient, CustomQuery({editCase.fieldName: editCase.value}))
    AssertLegacyError(response, HTTP_EDIT_FAILURE, editCase.message, editCase.cursorField)


@pytest.mark.parametrize("invalidDateCase", INVALID_DATE_CASES)
async def test_impossible_calendar_date_is_refused_by_csutldtc(
    regularClient: httpx.AsyncClient, invalidDateCase: tuple[dict[str, str], str, str]
) -> None:
    """A composed date CSUTLDTC rejects answers its date literal with the month cursor."""
    overrides, message, cursorField = invalidDateCase
    response = await RunReport(regularClient, CustomQuery(overrides))
    AssertLegacyError(response, HTTP_EDIT_FAILURE, message, cursorField)


# CORPT00C normalises each year through NUMVAL-C into PIC 9999 before its NOT NUMERIC test, so
# "Not a valid Year..." is unreachable from any keyed year; the source behaviour is pinned here
# rather than that literal, which porting the program unchanged never returns.
@pytest.mark.usefixtures("reportData")
@pytest.mark.parametrize("yearCase", YEAR_CASES)
async def test_non_numeric_year_is_normalised_never_refused(
    regularClient: httpx.AsyncClient, yearCase: YearCase
) -> None:
    """A year keyed ABCD becomes 0000, which CSUTLDTC accepts (2513), and the report runs."""
    response = await RunReport(regularClient, CustomQuery({yearCase.fieldName: "ABCD"}))
    rangeCase = RangeCase({}, "Custom", yearCase.startDate, yearCase.endDate, yearCase.tranIds)
    AssertSubmitted(response, rangeCase)
    assert DetailTranIds(response.json()) == list(yearCase.tranIds)
    assert YEAR_INVALID_LITERAL_TAIL not in response.text


async def test_invalid_confirmation_value_is_refused(regularClient: httpx.AsyncClient) -> None:
    """A CONFIRM other than Y, N or blank is quoted in the literal, with the confirm cursor."""
    query = {"reportType": "monthly", "confirm": INVALID_CONFIRM_VALUE}
    response = await RunReport(regularClient, query)
    AssertLegacyError(response, HTTP_EDIT_FAILURE, INVALID_CONFIRM_MESSAGE, CURSOR_CONFIRM)


@pytest.mark.usefixtures("reportData")
@pytest.mark.parametrize("confirmValue", ["N", "n"])
async def test_confirmation_n_clears_the_screen(
    regularClient: httpx.AsyncClient, confirmValue: str
) -> None:
    """N runs INITIALIZE-ALL-FIELDS: 200, no message, no range, no lines, report-type cursor."""
    response = await RunReport(regularClient, CustomQuery({"confirm": confirmValue}))
    assert response.status_code == HTTP_OK, response.text
    assert response.json() == {
        "start_date": "",
        "end_date": "",
        "message": "",
        "cursor_field": CURSOR_REPORT_TYPE,
        "lines": [],
    }


# --- Exports -------------------------------------------------------------------------------------
@pytest.mark.usefixtures("reportData")
async def test_csv_export_writes_one_row_per_report_line(regularClient: httpx.AsyncClient) -> None:
    """The CSV attachment holds the header row, then one row per screen line in print order."""
    screenBody = (await RunReport(regularClient, MONTHLY_CONFIRMED)).json()
    response = await DownloadReport(regularClient, MONTHLY_CONFIRMED, "csv")
    assert response.status_code == HTTP_OK, response.text
    assert response.headers["content-type"].startswith(CSV_CONTENT_TYPE)
    expectedDisposition = MONTHLY_DISPOSITION_TEMPLATE.format(extension="csv")
    assert response.headers["content-disposition"] == expectedDisposition
    assert response.text.splitlines()[0] == CSV_HEADER
    csvRows = ParseCsv(response.text)[1:]
    assert len(csvRows) == len(screenBody["lines"])
    for csvRow, line in zip(csvRows, screenBody["lines"], strict=True):
        AssertCsvRowMatchesLine(csvRow, line)
    amountColumn = CSV_COLUMN_NAMES.index("amount")
    detailAmounts = [row[amountColumn] for row in csvRows if row[0] == DETAIL_KIND]
    assert detailAmounts == ["10.00", "20.50"]


@pytest.mark.usefixtures("reportData")
async def test_csv_export_guards_formula_text(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """A TRAN-SOURCE starting with = gets an apostrophe in the CSV, never on the screen."""
    InsertReportTransactions(syncEngine, (FORMULA_TRANSACTION,))
    screenBody = (await RunReport(regularClient, MONTHLY_CONFIRMED)).json()
    response = await DownloadReport(regularClient, MONTHLY_CONFIRMED, "csv")
    assert response.status_code == HTTP_OK, response.text
    tranIdColumn = CSV_COLUMN_NAMES.index("tran_id")
    sourceColumn = CSV_COLUMN_NAMES.index("source")
    csvRows = ParseCsv(response.text)
    formulaRow = next(row for row in csvRows if row[tranIdColumn] == FORMULA_TRAN_ID)
    assert formulaRow[sourceColumn] == GUARDED_FORMULA_SOURCE
    formulaLine = next(line for line in screenBody["lines"] if line["tran_id"] == FORMULA_TRAN_ID)
    assert formulaLine["source"] == FORMULA_SOURCE


@pytest.mark.usefixtures("reportData")
async def test_pdf_export_draws_the_print_lines(regularClient: httpx.AsyncClient) -> None:
    """The PDF attachment draws the 133-column print lines as uncompressed page text."""
    response = await DownloadReport(regularClient, MONTHLY_CONFIRMED, "pdf")
    assert response.status_code == HTTP_OK, response.text
    assert response.headers["content-type"] == PDF_CONTENT_TYPE
    expectedDisposition = MONTHLY_DISPOSITION_TEMPLATE.format(extension="pdf")
    assert response.headers["content-disposition"] == expectedDisposition
    assert response.content.startswith(PDF_SIGNATURE)
    assert JULY_05_TRAN_ID.encode() in response.content
    assert REPORT_TITLE.encode() in response.content


@pytest.mark.parametrize("exportFormat", EXPORT_FORMATS)
@pytest.mark.parametrize("query", EXPORT_EDIT_FAILURE_QUERIES)
async def test_export_answers_an_edit_failure_as_the_screen_does(
    regularClient: httpx.AsyncClient, query: dict[str, str], exportFormat: str
) -> None:
    """A download runs the CORPT00C edits first and refuses with the screen's 400 body."""
    screenResponse = await RunReport(regularClient, query)
    exportResponse = await DownloadReport(regularClient, query, exportFormat)
    assert screenResponse.status_code == HTTP_EDIT_FAILURE, screenResponse.text
    assert exportResponse.status_code == HTTP_EDIT_FAILURE, exportResponse.text
    assert exportResponse.json() == screenResponse.json()


@pytest.mark.parametrize("unconfirmedCase", UNCONFIRMED_EXPORT_CASES)
async def test_unconfirmed_export_is_refused_with_the_screen_message(
    regularClient: httpx.AsyncClient, unconfirmedCase: tuple[dict[str, str], str, str]
) -> None:
    """Only Y downloads: a blank or N confirmation is a 400 with the prompt or cleared screen."""
    query, message, cursorField = unconfirmedCase
    response = await DownloadReport(regularClient, query, "csv")
    AssertLegacyError(response, HTTP_EDIT_FAILURE, message, cursorField)


# --- Session and role ----------------------------------------------------------------------------
@pytest.mark.parametrize("refusalTarget", REFUSAL_TARGETS)
async def test_request_without_session_is_refused(
    apiClient: httpx.AsyncClient, refusalTarget: tuple[str, dict[str, str]]
) -> None:
    """A client holding no session cookie gets 401 with no message, before any edit runs."""
    url, extraParams = refusalTarget
    response = await apiClient.get(url, params={**MONTHLY_CONFIRMED, **extraParams})
    AssertLegacyError(response, HTTP_SIGN_ON_FAILURE, NO_MESSAGE)


@pytest.mark.parametrize("refusalTarget", REFUSAL_TARGETS)
async def test_admin_is_refused_the_regular_report(
    adminClient: httpx.AsyncClient, refusalTarget: tuple[str, dict[str, str]]
) -> None:
    """CR00 belongs to the main menu: a user of type A gets 403 with an empty message."""
    url, extraParams = refusalTarget
    response = await adminClient.get(url, params={**MONTHLY_CONFIRMED, **extraParams})
    AssertLegacyError(response, HTTP_ACCESS_REFUSED, NO_MESSAGE)
