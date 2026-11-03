"""Unit tests for the CR00 report slots, admission release, client-disconnect stops and the
screen JSON.

app.services.reports.corpt00c runs CORPT00C's edits and confirmation first, waiting for
nothing. A confirmed report then takes one of REPORT_WORKER_LIMIT report slots before its three
TRANREPT reads, holds a request admission for those reads only (core.database.AdmittedReads,
which ends the read transaction and returns the connection first), and builds, and for the
screen JSON or a download renders, the report on a report thread holding neither. A report
whose client has gone stops when its slot is granted, after its reads, or at the next line check
on the thread, with the 499 no client receives and one INFO record. cbtrn03c.IterateReport
yields BuildReport's lines lazily, and the report_export renderers read them once. The screen
JSON of RenderTransactionReportJson is byte for byte what FastAPI sends for the response model
RunTransactionReport returns (TypeAdapter.dump_json by alias), built without a line list.

The repositories and the ASGI receive channel are stubbed, so no HTTP request is made, except
in the first case, which reads the seeded test database (holding no transaction) through
makeOnlineContext. Expected literals are typed from cbl/CORPT00C.cbl: ``Please confirm to print
the Custom report...`` (lines 464-474), ``"X" is not a valid value to confirm...`` (lines
489-494), ``Start Date - Not a valid Month...`` (lines 329-337), ``Custom report submitted for
printing ...`` (lines 445-455) and the WHEN OTHER ``Unable to Write TDQ (JOBS)...`` (line 531).
"""

import asyncio
import functools
import logging
import threading
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, NamedTuple

import anyio
import pytest
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import State
from starlette.requests import Request

from app.core.config import Settings
from app.core.database import ADMITTED_REQUEST_LIMIT, RequestAdmission
from app.core.dependencies import CurrentUser, OnlineContext
from app.core.errors import FileIoError, LegacyMessageError
from app.repositories.transaction_repository import TransactionRepository
from app.schemas.common import ExportFormat
from app.schemas.reports import ReportLine, ReportQuery, TransactionReportResponse
from app.services.reports import cbtrn03c, corpt00c, report_export
from app.services.reports.report_export import RenderCsv, RenderPdf

REGULAR_USER_TYPE = "U"
TEST_USER_ID = "SLOTTEST"
REPORT_PATH = "/api/v1/reports/transactions"
CONFIRMED_QUERY_FIELDS = {
    "reportType": "custom",
    "startMm": "01",
    "startDd": "01",
    "startYyyy": "2022",
    "endMm": "12",
    "endDd": "31",
    "endYyyy": "2022",
    "confirm": "Y",
}
CONFIRMED_QUERY = ReportQuery.model_validate(CONFIRMED_QUERY_FIELDS)
QUERY_DATA = cbtrn03c.ReportRequestData("2022-01-01", "2022-12-31")
SUBMITTED_MESSAGE = "Custom report submitted for printing ..."
CONFIRM_PROMPT_MESSAGE = "Please confirm to print the Custom report..."
UNABLE_TO_WRITE_TDQ_MESSAGE = "Unable to Write TDQ (JOBS)..."
# nginx's Client Closed Request: the status of a report stopped for a gone client.
CLIENT_CLOSED_STATUS = 499
SCREEN_STATUS = 200
EDIT_FAILURE_STATUS = 400
FILE_ERROR_STATUS = 503
STOP_LOG_MESSAGE = "CR00 report stopped: the client disconnected"

SCREEN_ENTRY = "screen"
JSON_ENTRY = "json"
ENTRIES = (SCREEN_ENTRY, JSON_ENTRY, ExportFormat.CSV.value, ExportFormat.PDF.value)
# FastAPI 0.136 sends a response model as its TypeAdapter's dump_json by alias, nothing excluded.
RESPONSE_MODEL_ADAPTER = TypeAdapter(TransactionReportResponse)

TYPE_CODE = "01"
CATEGORY_CODE = 1
UNKNOWN_TYPE_CODE = "99"
TYPE_DESCRIPTIONS = {TYPE_CODE: "Purchase"}
CATEGORY_DESCRIPTIONS = {(TYPE_CODE, CATEGORY_CODE): "Regular Sales Draft"}
ROWS_PER_CARD = 7
CARD_NUM_DIGITS = 16
ACCT_ID_DIGITS = 11
AMOUNT_STEP = Decimal("1.25")
ROW_SOURCE = "POS TERM"
# A 10-character TRAN-SOURCE that is the very member the screen JSON is split at.
LINES_MEMBER_SOURCE = '"lines":[]'
# Report sizes: none, one detail, and seven cards across several report and PDF pages.
ROW_COUNTS = (0, 1, 45)
LARGEST_ROW_COUNT = max(ROW_COUNTS)
STUB_ROW_COUNT = 12

CONCURRENT_REPORTS = 6
READ_DELAY_SECONDS = 0.05
EDIT_DEADLINE_SECONDS = 1.0
MID_BUILD_CHECK_LINES = 3
# No report slot borrowed and every request admission free.
NOTHING_HELD = (0, ADMITTED_REQUEST_LIMIT)


class ReportProgress(NamedTuple):
    """How far a report got: row reads run, builds started and builds finished."""

    rowReads: int
    buildsStarted: int
    buildsFinished: int


class Holdings(NamedTuple):
    """What a report holds at one moment, and whether it runs on the event-loop thread."""

    freeAdmissions: int
    checkedOutConnections: int
    sessionInTransaction: bool
    onMainThread: bool


def BuildRows(
    rowCount: int, typeCode: str = TYPE_CODE, source: str = ROW_SOURCE
) -> list[dict[str, Any]]:
    """Return rowCount report rows, ROWS_PER_CARD per card, in GetReportTransactions order.

    Card numbers and accounts are built from the card index, and the amounts step through
    negative, zero and positive values.
    """
    return [
        {
            "tran_id": f"{rowIndex + 1:0{CARD_NUM_DIGITS}d}",
            "card_num": f"{rowIndex // ROWS_PER_CARD + 1:0{CARD_NUM_DIGITS}d}",
            "tran_type_cd": typeCode,
            "tran_cat_cd": CATEGORY_CODE,
            "tran_source": source,
            "tran_amt": AMOUNT_STEP * (rowIndex % 5 - 2),
            "xref_acct_id": f"{rowIndex // ROWS_PER_CARD + 1:0{ACCT_ID_DIGITS}d}",
        }
        for rowIndex in range(rowCount)
    ]


def ReportLinesOf(rows: Sequence[Mapping[str, Any]]) -> list[ReportLine]:
    """Return BuildReport's whole line list for rows over the stub descriptions."""
    return cbtrn03c.BuildReport(QUERY_DATA, rows, TYPE_DESCRIPTIONS, CATEGORY_DESCRIPTIONS)


class ClientChannel:
    """The ASGI receive channel of one stub request; Disconnect makes it report a gone client."""

    def __init__(self) -> None:
        self.gone = False

    def Disconnect(self) -> None:
        """Make every later receive return http.disconnect."""
        self.gone = True

    async def Receive(self) -> dict[str, str]:
        """Return http.disconnect once the client is gone; until then no message arrives."""
        if not self.gone:
            await anyio.sleep_forever()
        return {"type": "http.disconnect"}


@dataclass
class ReadLog:
    """What the stub repositories of one test return, and what their row reads observed."""

    rows: Sequence[Mapping[str, Any]]
    readDelaySeconds: float = 0.0
    failure: Exception | None = None
    onReadStart: Callable[[], None] | None = None
    afterRowsRead: Callable[[], None] | None = None
    rowReads: int = 0
    freeAdmissions: list[int] = field(default_factory=list)


class StubRepository:
    """Stands in for ReferenceRepository and TransactionRepository, reading from a ReadLog."""

    def __init__(self, readLog: ReadLog, context: OnlineContext) -> None:
        self.readLog = readLog
        self.context = context

    async def GetTypeDescriptions(self) -> dict[str, str]:
        """Return the TRANTYPE descriptions of the stub rows."""
        return dict(TYPE_DESCRIPTIONS)

    async def GetCategoryDescriptions(self) -> dict[tuple[str, int], str]:
        """Return the TRANCATG descriptions of the stub rows."""
        return dict(CATEGORY_DESCRIPTIONS)

    async def GetReportTransactions(
        self, startDate: date, endDate: date
    ) -> Sequence[Mapping[str, Any]]:
        """Record the free admissions, wait, then return the rows or raise the failure."""
        readLog = self.readLog
        readLog.freeAdmissions.append(RequestAdmission(self.context.request).value)
        if readLog.onReadStart is not None:
            readLog.onReadStart()
        await anyio.sleep(readLog.readDelaySeconds)
        readLog.rowReads += 1
        if readLog.failure is not None:
            raise readLog.failure
        if readLog.afterRowsRead is not None:
            readLog.afterRowsRead()
        return readLog.rows


class BuildObserver:
    """Wraps cbtrn03c.IterateReport to count builds and lines and to run hooks on the thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.buildsStarted = 0
        self.buildsFinished = 0
        self.linesBuilt = 0
        self.lineReferences: list[weakref.ref[ReportLine]] = []
        self.onBuildStart: Callable[[], None] | None = None
        self.onLine: Callable[[int], None] | None = None
        self.onBuildEnd: Callable[[], None] | None = None

    def RunHook(self, hook: Callable[..., None] | None, *hookArguments: Any) -> None:
        """Run hook with hookArguments when one is set."""
        if hook is not None:
            hook(*hookArguments)

    def Observe(self, *reportInput: Any) -> Iterator[ReportLine]:
        """Yield IterateReport's lines for reportInput, recording the build as it runs."""
        with self.lock:
            self.buildsStarted += 1
        self.RunHook(self.onBuildStart)
        for line in cbtrn03c.IterateReport(*reportInput):
            with self.lock:
                self.linesBuilt += 1
                self.lineReferences.append(weakref.ref(line))
            self.RunHook(self.onLine, self.linesBuilt)
            yield line
        with self.lock:
            self.buildsFinished += 1
        self.RunHook(self.onBuildEnd)

    def LiveLines(self) -> int:
        """Return how many of the lines built so far are still referenced anywhere."""
        with self.lock:
            return sum(lineReference() is not None for lineReference in self.lineReferences)


class SlotGauge:
    """Counts the reports between the start of their row read and the end of their build."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def Enter(self) -> None:
        """Count one more report in progress and keep the peak."""
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def Leave(self) -> None:
        """Count one report less in progress."""
        with self.lock:
            self.active -= 1


@dataclass
class ReportHarness:
    """The stubbed reads, the observed build and the context builder of one test."""

    readLog: ReadLog
    buildObserver: BuildObserver
    application: SimpleNamespace
    settings: Settings
    monkeypatch: pytest.MonkeyPatch

    def Context(self, channel: ClientChannel | None = None) -> OnlineContext:
        """Return a regular user's context whose request receives from channel."""
        scope = {
            "type": "http",
            "app": self.application,
            "method": "GET",
            "path": REPORT_PATH,
            "headers": [],
            "query_string": b"",
        }
        receiveChannel = channel if channel is not None else ClientChannel()
        return OnlineContext(
            request=Request(scope, receiveChannel.Receive),
            session=AsyncSession(),
            settings=self.settings,
            user=CurrentUser(user_id=TEST_USER_ID, user_type=REGULAR_USER_TYPE),
        )

    def FreeAdmissions(self) -> int:
        """Return how many request admissions of the stub application are free."""
        return RequestAdmission(self.Context().request).value

    def Held(self) -> tuple[int, int]:
        """Return the borrowed report slots and the free admissions; NOTHING_HELD when idle."""
        return corpt00c.REPORT_SLOTS.borrowed_tokens, self.FreeAdmissions()


@pytest.fixture(name="buildObserver")
def BuildObserverFixture(monkeypatch: pytest.MonkeyPatch) -> BuildObserver:
    """Return the observer that corpt00c's report threads build through."""
    buildObserver = BuildObserver()
    monkeypatch.setattr(corpt00c, "IterateReport", buildObserver.Observe)
    return buildObserver


@pytest.fixture(name="harness")
def ReportHarnessFixture(
    buildObserver: BuildObserver, monkeypatch: pytest.MonkeyPatch, testSettings: Settings
) -> ReportHarness:
    """Return a harness whose stub repositories replace both report repositories of corpt00c."""
    readLog = ReadLog(rows=BuildRows(STUB_ROW_COUNT))
    stubRepository = functools.partial(StubRepository, readLog)
    monkeypatch.setattr(corpt00c, "ReferenceRepository", stubRepository)
    monkeypatch.setattr(corpt00c, "TransactionRepository", stubRepository)
    application = SimpleNamespace(state=State())
    return ReportHarness(readLog, buildObserver, application, testSettings, monkeypatch)


async def RunEntry(entryName: str, context: OnlineContext, query: ReportQuery) -> object:
    """Run CR00 through the entry point entryName names.

    The entries are the screen model, the screen JSON the route sends, and each download format.
    """
    if entryName == SCREEN_ENTRY:
        return await corpt00c.RunTransactionReport(context, query)
    if entryName == JSON_ENTRY:
        return await corpt00c.RenderTransactionReportJson(context, query)
    return await corpt00c.ExportTransactionReport(context, query, ExportFormat(entryName))


def HoldingsOf(context: OnlineContext) -> Holdings:
    """Return the admissions, pooled connections and transaction the request holds now."""
    return Holdings(
        freeAdmissions=RequestAdmission(context.request).value,
        checkedOutConnections=context.engine.pool.checkedout(),
        sessionInTransaction=context.session.in_transaction(),
        onMainThread=threading.current_thread() is threading.main_thread(),
    )


async def test_only_the_reads_hold_an_admission_and_a_connection(
    makeOnlineContext, buildObserver, monkeypatch
):
    """The reads hold one admission and one connection; the report thread holds neither."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    readHoldings: list[Holdings] = []
    buildHoldings: list[Holdings] = []
    readRows = TransactionRepository.GetReportTransactions

    async def ObservedRead(repository, startDate, endDate):
        readHoldings.append(HoldingsOf(context))
        return await readRows(repository, startDate, endDate)

    monkeypatch.setattr(TransactionRepository, "GetReportTransactions", ObservedRead)
    buildObserver.onBuildStart = lambda: buildHoldings.append(HoldingsOf(context))

    response = await corpt00c.RunTransactionReport(context, CONFIRMED_QUERY)

    assert (response.message, response.lines) == (SUBMITTED_MESSAGE, [])
    assert readHoldings == [Holdings(ADMITTED_REQUEST_LIMIT - 1, 1, True, True)]
    assert buildHoldings == [Holdings(ADMITTED_REQUEST_LIMIT, 0, False, False)]
    assert HoldingsOf(context) == Holdings(ADMITTED_REQUEST_LIMIT, 0, False, True)


async def test_at_most_report_worker_limit_reports_read_and_build_at_once(harness):
    """Concurrent reports pass through the slots; a report waiting for one holds no admission."""
    gauge = SlotGauge()
    harness.readLog.readDelaySeconds = READ_DELAY_SECONDS
    harness.readLog.onReadStart = gauge.Enter
    harness.buildObserver.onBuildEnd = gauge.Leave
    contexts = [harness.Context() for _ in range(CONCURRENT_REPORTS)]

    responses = await asyncio.gather(
        *(corpt00c.RunTransactionReport(context, CONFIRMED_QUERY) for context in contexts)
    )

    assert corpt00c.REPORT_SLOTS.total_tokens == corpt00c.REPORT_WORKER_LIMIT
    assert gauge.peak == corpt00c.REPORT_WORKER_LIMIT
    expectedLowest = ADMITTED_REQUEST_LIMIT - corpt00c.REPORT_WORKER_LIMIT
    assert min(harness.readLog.freeAdmissions) == expectedLowest
    expectedLines = ReportLinesOf(harness.readLog.rows)
    assert [response.lines for response in responses] == [expectedLines] * CONCURRENT_REPORTS
    assert harness.Held() == NOTHING_HELD


def DisconnectOnFirstLine(channel: ClientChannel, lineNumber: int) -> None:
    """Make the client go away once the first report line has been built."""
    if lineNumber == 1:
        channel.Disconnect()


class DisconnectCase(NamedTuple):
    """Where the client goes away, and how far the report then gets."""

    stage: str
    expectedProgress: ReportProgress
    expectedLinesBuilt: int


# The client is gone when the slot is granted, goes during the reads, or goes once the first
# line is built, with a line check every MID_BUILD_CHECK_LINES lines.
DISCONNECT_CASES = [
    DisconnectCase("slot-granted", ReportProgress(0, 0, 0), 0),
    DisconnectCase("after-reads", ReportProgress(1, 0, 0), 0),
    DisconnectCase("mid-build", ReportProgress(1, 1, 0), MID_BUILD_CHECK_LINES),
]


def ArmDisconnect(stage: str, harness: ReportHarness, channel: ClientChannel) -> None:
    """Make the client of channel go away at the DISCONNECT_CASES stage named stage."""
    if stage == "slot-granted":
        channel.Disconnect()
    elif stage == "after-reads":
        harness.readLog.afterRowsRead = channel.Disconnect
    else:
        harness.monkeypatch.setattr(corpt00c, "DISCONNECT_CHECK_LINES", MID_BUILD_CHECK_LINES)
        harness.buildObserver.onLine = functools.partial(DisconnectOnFirstLine, channel)


@pytest.mark.parametrize("entryName", ENTRIES)
@pytest.mark.parametrize(
    "disconnectCase", DISCONNECT_CASES, ids=[case.stage for case in DISCONNECT_CASES]
)
async def test_a_gone_client_stops_the_report_with_499_and_one_info_record(
    harness, caplog, disconnectCase, entryName
):
    """The report stops at the stage the client left, logs once and frees slot and admission."""
    caplog.set_level(logging.INFO, logger=corpt00c.LOGGER.name)
    channel = ClientChannel()
    ArmDisconnect(disconnectCase.stage, harness, channel)

    with pytest.raises(LegacyMessageError) as raised:
        await RunEntry(entryName, harness.Context(channel), CONFIRMED_QUERY)

    assert (raised.value.httpStatus, raised.value.message) == (CLIENT_CLOSED_STATUS, "")
    observer = harness.buildObserver
    progress = ReportProgress(
        harness.readLog.rowReads, observer.buildsStarted, observer.buildsFinished
    )
    expected = (disconnectCase.expectedProgress, disconnectCase.expectedLinesBuilt)
    assert (progress, observer.linesBuilt) == expected
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.INFO, STOP_LOG_MESSAGE)
    ]
    assert harness.Held() == NOTHING_HELD


def ArmFailure(failureName: str, harness: ReportHarness) -> None:
    """Make the report fail as failureName says: a failed TRANSACT read or a CBTRN03C abend."""
    if failureName == "read-failure":
        harness.readLog.failure = FileIoError("TRANSACT")
    else:
        harness.readLog.rows = BuildRows(STUB_ROW_COUNT, typeCode=UNKNOWN_TYPE_CODE)


async def RaisedBy(entryName: str, context: OnlineContext) -> BaseException | None:
    """Return the exception the confirmed report raises through entryName, or None."""
    try:
        await RunEntry(entryName, context, CONFIRMED_QUERY)
    except (LegacyMessageError, cbtrn03c.ReportAbendError) as error:
        return error
    return None


@pytest.mark.parametrize("entryName", ENTRIES)
@pytest.mark.parametrize("failureName", ["read-failure", "abend"])
async def test_a_failed_read_or_an_abend_keeps_its_outcome_and_frees_slot_and_admission(
    harness, failureName, entryName
):
    """The 503 literal and the abend 999 propagate unwrapped, and nothing stays held."""
    ArmFailure(failureName, harness)

    raisedError = await RaisedBy(entryName, harness.Context())

    if failureName == "read-failure":
        assert isinstance(raisedError, LegacyMessageError)
        outcome = (raisedError.httpStatus, raisedError.message)
        assert outcome == (FILE_ERROR_STATUS, UNABLE_TO_WRITE_TDQ_MESSAGE)
    else:
        assert type(raisedError) is cbtrn03c.ReportAbendError
    assert harness.Held() == NOTHING_HELD


class EditCase(NamedTuple):
    """One unconfirmed or failing ENTER and the outcome CORPT00C gives it."""

    entryName: str
    queryFields: dict[str, str]
    expectedOutcome: tuple[int, str]


EDIT_CASES = [
    EditCase(SCREEN_ENTRY, {"confirm": ""}, (SCREEN_STATUS, CONFIRM_PROMPT_MESSAGE)),
    EditCase(SCREEN_ENTRY, {"confirm": "N"}, (SCREEN_STATUS, "")),
    EditCase(
        SCREEN_ENTRY,
        {"confirm": "X"},
        (EDIT_FAILURE_STATUS, '"X" is not a valid value to confirm...'),
    ),
    EditCase(
        SCREEN_ENTRY, {"startMm": "13"}, (EDIT_FAILURE_STATUS, "Start Date - Not a valid Month...")
    ),
    EditCase(JSON_ENTRY, {"confirm": ""}, (SCREEN_STATUS, CONFIRM_PROMPT_MESSAGE)),
    EditCase(JSON_ENTRY, {"confirm": "n"}, (SCREEN_STATUS, "")),
    EditCase(
        JSON_ENTRY,
        {"confirm": "X"},
        (EDIT_FAILURE_STATUS, '"X" is not a valid value to confirm...'),
    ),
    EditCase("csv", {"confirm": ""}, (EDIT_FAILURE_STATUS, CONFIRM_PROMPT_MESSAGE)),
    EditCase("pdf", {"confirm": "N"}, (EDIT_FAILURE_STATUS, "")),
]
EDIT_IDS = [
    "screen-prompt",
    "screen-cleared",
    "screen-invalid-confirm",
    "screen-invalid-month",
    "json-prompt",
    "json-cleared",
    "json-invalid-confirm",
    "csv-prompt",
    "pdf-cleared",
]


async def EditOutcome(editCase: EditCase, context: OnlineContext) -> tuple[int, str]:
    """Return the status and message ENTER gives editCase's query through its entry point."""
    query = ReportQuery.model_validate({**CONFIRMED_QUERY_FIELDS, **editCase.queryFields})
    try:
        response = await RunEntry(editCase.entryName, context, query)
    except LegacyMessageError as error:
        return error.httpStatus, error.message
    if isinstance(response, bytes):
        response = TransactionReportResponse.model_validate_json(response)
    return SCREEN_STATUS, response.message


@pytest.mark.parametrize("editCase", EDIT_CASES, ids=EDIT_IDS)
async def test_edits_and_confirmation_answer_while_every_report_slot_is_taken(harness, editCase):
    """A prompt, a cleared screen or an edit failure never waits for a report slot."""
    borrowers = [object() for _ in range(corpt00c.REPORT_WORKER_LIMIT)]
    for borrower in borrowers:
        await corpt00c.REPORT_SLOTS.acquire_on_behalf_of(borrower)
    try:
        with anyio.fail_after(EDIT_DEADLINE_SECONDS):
            outcome = await EditOutcome(editCase, harness.Context())
    finally:
        for borrower in borrowers:
            corpt00c.REPORT_SLOTS.release_on_behalf_of(borrower)

    assert outcome == editCase.expectedOutcome
    assert harness.readLog.rowReads == 0


@pytest.mark.parametrize("rowCount", ROW_COUNTS)
def test_iterate_report_lazily_yields_exactly_the_build_report_lines(rowCount):
    """IterateReport over a one-pass row iterator yields BuildReport's lines in order."""
    rows = BuildRows(rowCount)

    iteratedLines = cbtrn03c.IterateReport(
        QUERY_DATA, iter(rows), TYPE_DESCRIPTIONS, CATEGORY_DESCRIPTIONS
    )

    assert isinstance(iteratedLines, Iterator)
    assert list(iteratedLines) == ReportLinesOf(rows)


@pytest.mark.parametrize("render", [RenderCsv, RenderPdf], ids=["csv", "pdf"])
@pytest.mark.parametrize("rowCount", ROW_COUNTS)
def test_renderers_give_the_same_bytes_from_a_line_generator_as_from_the_line_list(
    render, rowCount
):
    """Rendering IterateReport's lines as they are built gives the bytes of the whole list."""
    rows = BuildRows(rowCount)
    lineGenerator = cbtrn03c.IterateReport(
        QUERY_DATA, iter(rows), TYPE_DESCRIPTIONS, CATEGORY_DESCRIPTIONS
    )

    assert render(lineGenerator) == render(ReportLinesOf(rows))


class JsonParityCase(NamedTuple):
    """One CR00 ENTER, the stub rows a confirmed report reads, and the lines it shows."""

    queryFields: dict[str, str]
    rows: list[dict[str, Any]]
    expectedLines: list[ReportLine]


def ConfirmedParityCase(rows: list[dict[str, Any]]) -> JsonParityCase:
    """Return the parity case of a confirmed report over rows, showing BuildReport's lines."""
    return JsonParityCase({}, rows, ReportLinesOf(rows))


# Confirmed reports of none, one and 45 rows and one whose TRAN-SOURCE is the member the JSON is
# split at; then the blank-confirm prompt and the N clear, which read nothing and show no lines.
JSON_PARITY_CASES = [
    *(ConfirmedParityCase(BuildRows(rowCount)) for rowCount in ROW_COUNTS),
    ConfirmedParityCase(BuildRows(LARGEST_ROW_COUNT, source=LINES_MEMBER_SOURCE)),
    JsonParityCase({"confirm": ""}, BuildRows(STUB_ROW_COUNT), []),
    JsonParityCase({"confirm": "N"}, BuildRows(STUB_ROW_COUNT), []),
]
JSON_PARITY_IDS = [
    *(f"rows-{rowCount}" for rowCount in ROW_COUNTS),
    f"rows-{LARGEST_ROW_COUNT}-lines-member-source",
    "prompt",
    "cleared",
]


@pytest.mark.parametrize("parityCase", JSON_PARITY_CASES, ids=JSON_PARITY_IDS)
async def test_screen_json_is_byte_for_byte_the_response_model_fastapi_would_send(
    harness, parityCase
):
    """RenderTransactionReportJson gives the dump_json of RunTransactionReport's response."""
    harness.readLog.rows = parityCase.rows
    query = ReportQuery.model_validate({**CONFIRMED_QUERY_FIELDS, **parityCase.queryFields})

    screenJson = await corpt00c.RenderTransactionReportJson(harness.Context(), query)
    response = await corpt00c.RunTransactionReport(harness.Context(), query)

    assert response.lines == parityCase.expectedLines
    assert screenJson == RESPONSE_MODEL_ADAPTER.dump_json(response, by_alias=True)
    assert harness.Held() == NOTHING_HELD


def FailListing(job: corpt00c.ReportJob) -> list[ReportLine]:
    """Stand in for ListReportLines, which the screen JSON must never call."""
    raise AssertionError(f"the whole line list of {job.requestData} was built")


class DrainRecorder:
    """Wraps cbtrn03c.DrainLines to record how many lines each record wrote."""

    def __init__(self, drainLines: Callable[[Any], list[ReportLine]]) -> None:
        self.drainLines = drainLines
        self.drainSizes: list[int] = []

    def Drain(self, state: Any) -> list[ReportLine]:
        """Drain the lines state holds, as cbtrn03c does, recording how many there were."""
        writtenLines = self.drainLines(state)
        self.drainSizes.append(len(writtenLines))
        return writtenLines


async def test_screen_json_encodes_each_line_as_it_is_built_without_a_line_list(
    harness, monkeypatch
):
    """No line list is built: only the lines of the record in hand are ever alive at once."""
    drainRecorder = DrainRecorder(cbtrn03c.DrainLines)
    monkeypatch.setattr(cbtrn03c, "DrainLines", drainRecorder.Drain)
    monkeypatch.setattr(corpt00c, "ListReportLines", FailListing)
    harness.readLog.rows = BuildRows(LARGEST_ROW_COUNT)
    observer = harness.buildObserver
    liveCounts: list[int] = []
    observer.onLine = lambda lineNumber: liveCounts.append(observer.LiveLines())

    screenJson = await corpt00c.RenderTransactionReportJson(harness.Context(), CONFIRMED_QUERY)

    screenLines = TransactionReportResponse.model_validate_json(screenJson).lines
    assert observer.linesBuilt == len(screenLines) == len(liveCounts)
    # The lines one record wrote, plus the line before them, which is still being encoded.
    assert max(liveCounts) <= max(drainRecorder.drainSizes) + 1 < len(screenLines)
    assert screenLines == ReportLinesOf(harness.readLog.rows)


class EnvelopeErrorCase(NamedTuple):
    """A screen envelope, the member it is split at, and how often the member occurs in it."""

    envelope: TransactionReportResponse
    linesMember: bytes
    memberCount: int


ENVELOPE_ERROR_CASES = [
    EnvelopeErrorCase(
        corpt00c.SubmittedResponse(
            corpt00c.ReportRange("Custom", "2022-01-01", "2022-12-31"), ReportLinesOf(BuildRows(1))
        ),
        report_export.EMPTY_LINES_MEMBER,
        0,
    ),
    # The cleared screen's three empty texts: start_date, end_date and message.
    EnvelopeErrorCase(corpt00c.ClearedResponse(), b':""', 3),
]


@pytest.mark.parametrize(
    "envelopeCase", ENVELOPE_ERROR_CASES, ids=["envelope-with-lines", "member-thrice"]
)
def test_render_json_refuses_an_envelope_without_exactly_one_empty_lines_member(
    monkeypatch, envelopeCase
):
    """The split fails loudly rather than writing the lines into the wrong place."""
    monkeypatch.setattr(report_export, "EMPTY_LINES_MEMBER", envelopeCase.linesMember)

    with pytest.raises(report_export.ReportJsonEnvelopeError) as raised:
        report_export.RenderJson(envelopeCase.envelope, ReportLinesOf(BuildRows(1)))

    assert str(raised.value).endswith(f"found {envelopeCase.memberCount}")
