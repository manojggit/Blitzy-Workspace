"""Unit tests for the CR00 report slots, output budget, admissions, row batches, bounded chunk
buffers, client-disconnect stops and the screen JSON.

app.services.reports.corpt00c runs CORPT00C's edits and confirmation first, waiting for
nothing. A confirmed report then takes one of REPORT_WORKER_LIMIT report slots, then a request
admission (core.database.AdmittedReads), and reads TRANTYPE, TRANCATG and a validation pass of
TRANSACT, so a read failure, an abend or a gone client still sets the status before any byte is
sent; a report refused admission within DB_CONNECT_TIMEOUT_SECONDS reads nothing and answers
the WHEN OTHER literal. Its build pass then reads TRANSACT again and builds, and for the screen
JSON or a download renders, the report on a report thread. Both passes fetch their rows a batch
at a time, and the renderers yield the document in chunks as it is built. A streamed report
takes a REPORT_OUTPUT_BUDGET token before its slot, holding nothing else while it waits, and
keeps it until its response ends; its build waits for room while its buffer of
REPORT_BUFFER_CHUNKS is full, and frees the slot, the admission and the connection when it ends,
before the response has sent every chunk. A report whose client has gone stops when its slot is
granted or after its validation pass, with the 499 no client receives and one INFO record; a
listed report also stops at the next line check on the thread, and a streamed one at its next
chunk, or at once while it waits for room, logging the same record. A client that takes no
message within REPORT_SEND_TIMEOUT_SECONDS stops the report the same way, with one INFO record
of its own, and its body never ends. A build pass that fails after the response started
raises, so the body never ends normally. cbtrn03c.IterateReport
yields BuildReport's lines lazily, and the report_export renderers read them once. The screen
JSON is byte for byte what FastAPI sends for the response model RunTransactionReport returns
(TypeAdapter.dump_json by alias), built without a line list.

The repositories and the ASGI receive channel are stubbed, and ChunkCollector plays the
response that drains a report stream, or a stub ASGI client drives the router's
ReportStreamingResponse, so no HTTP request is made, except in the first case, which reads the
seeded test database (holding no transaction) through makeOnlineContext.
Expected literals are typed from cbl/CORPT00C.cbl: ``Please confirm to print the Custom
report...`` (lines 464-474), ``"X" is not a valid value to confirm...`` (lines 489-494),
``Start Date - Not a valid Month...`` (lines 329-337), ``Custom report submitted for printing
...`` (lines 445-455) and the WHEN OTHER ``Unable to Write TDQ (JOBS)...`` (line 531).
"""

import asyncio
import contextlib
import functools
import itertools
import logging
import threading
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
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
from starlette.types import Message

from app.api.v1.reports import ReportStreamingResponse
from app.core.config import Settings
from app.core.database import ADMITTED_REQUEST_LIMIT, RequestAdmission
from app.core.dependencies import CurrentUser, OnlineContext
from app.core.errors import FileIoError, LegacyMessageError
from app.repositories.transaction_repository import TransactionRepository
from app.schemas.common import ExportFormat
from app.schemas.reports import ReportLine, ReportQuery, TransactionReportResponse
from app.services.reports import cbtrn03c, corpt00c, report_export
from app.services.reports.report_export import (
    CSV_MEDIA_TYPE,
    JSON_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    RenderCsv,
    RenderPdf,
)

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
STALL_LOG_MESSAGE = "CR00 report stopped: the client stopped reading"

SCREEN_ENTRY = "screen"
JSON_ENTRY = "json"
ENTRIES = (SCREEN_ENTRY, JSON_ENTRY, ExportFormat.CSV.value, ExportFormat.PDF.value)
# The entries whose confirmed report is a stream the response sends while it is built, each
# sending its first chunk while the rows are still being read: the PDF one page at a time.
STREAM_ENTRIES = ENTRIES[1:]
# FastAPI 0.136 sends a response model as its TypeAdapter's dump_json by alias, nothing excluded.
RESPONSE_MODEL_ADAPTER = TypeAdapter(TransactionReportResponse)
CONFIRMED_RANGE = corpt00c.ReportRange("Custom", "2022-01-01", "2022-12-31")

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
# Rows whose report runs to 151 lines, three PDF pages, so even a PDF has lines left to build
# when the chunk after its first, a page later, finds that the response stopped reading.
THREE_PDF_PAGE_ROW_COUNT = 90
STUB_ROW_COUNT = 12

CONCURRENT_REPORTS = 6
READ_DELAY_SECONDS = 0.05
EDIT_DEADLINE_SECONDS = 1.0
MID_BUILD_CHECK_LINES = 3
# No report slot and no output token borrowed, and every request admission free.
NOTHING_HELD = (0, 0, ADMITTED_REQUEST_LIMIT)
# The admission bound of a report whose admissions are all held, far below the default.
SHORT_ADMISSION_WAIT_SECONDS = 0.05
# CORPT00C's cursor on the report type (MOVE -1 TO MONTHLYL), as the ReportQuery wire name.
CURSOR_REPORT_TYPE = "reportType"
# The WHEN OTHER file condition of a report refused admission: IOERR of TRANTYPE, its first read.
ADMISSION_REFUSED_TEXT = (
    "Transaction CR00 READ on TRANTYPE returned RESP:17 REAS:0"
    " (cause AdmissionTimeoutError, SQLSTATE none)"
)
ADMISSION_REFUSED_LOG = (logging.ERROR, ADMISSION_REFUSED_TEXT)
# Rows per fetch and bytes per chunk small enough that a LARGEST_ROW_COUNT report spans many.
SMALL_BATCH_ROWS = 5
SMALL_CHUNK_BYTES = 256
# A report's first TRANSACT pass is its validation pass and its second its build pass.
BUILD_PASS_READ = 2
BUILD_DEADLINE_SECONDS = 5.0
POLL_SECONDS = 0.005
TRANSACT_FILE_NAME = "TRANSACT"
# A report buffer so small that even a THREE_PDF_PAGE_ROW_COUNT PDF fills it with lines unbuilt.
ONE_CHUNK_BUFFER = 1
# A send bound short enough that a client that stops reading stops its report within a test.
SHORT_SEND_TIMEOUT_SECONDS = 0.05
RESPONSE_START_MESSAGE: Message = {"type": "http.response.start", "status": SCREEN_STATUS}
REPORT_MEDIA_TYPES = {
    JSON_ENTRY: JSON_MEDIA_TYPE,
    ExportFormat.CSV.value: CSV_MEDIA_TYPE,
    ExportFormat.PDF.value: PDF_MEDIA_TYPE,
}
# The scope uvicorn gives an HTTP request: below ASGI spec 2.4, StreamingResponse sends in a task
# that runs beside its wait for a disconnect.
UVICORN_HTTP_SCOPE = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}}


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
    """Return rowCount report rows, ROWS_PER_CARD per card, in StreamReportTransactions order.

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


class TrackedRow(dict[str, Any]):
    """A stub report row that, unlike a plain dict, a weak reference can watch."""


class BatchPosition(NamedTuple):
    """Where a row fetch is: the report's read number, the batch index and the batch count."""

    readNumber: int
    batchIndex: int
    batchCount: int


def RunHook(hook: Callable[[], None] | None) -> None:
    """Run hook when one is set."""
    if hook is not None:
        hook()


@dataclass
class ReadLog:
    """What the stub repositories of one test return, and what their row reads observed.

    ``failure`` fails every row read as it starts; ``beforeBatch`` runs before each batch.
    """

    rows: Sequence[Mapping[str, Any]]
    readDelaySeconds: float = 0.0
    failure: Exception | None = None
    onReadsStart: Callable[[], None] | None = None
    afterRowsRead: Callable[[], None] | None = None
    beforeBatch: Callable[[BatchPosition], Awaitable[None]] | None = None
    rowReads: int = 0
    freeAdmissions: list[int] = field(default_factory=list)
    rowReferences: list[weakref.ref[TrackedRow]] = field(default_factory=list)
    liveRowCounts: list[int] = field(default_factory=list)

    def TrackBatch(self, batch: Sequence[Mapping[str, Any]]) -> list[TrackedRow]:
        """Return watched copies of batch, recording how many watched rows are then alive."""
        trackedRows = [TrackedRow(row) for row in batch]
        self.rowReferences.extend(weakref.ref(row) for row in trackedRows)
        liveRows = sum(rowReference() is not None for rowReference in self.rowReferences)
        self.liveRowCounts.append(liveRows)
        return trackedRows

    async def BeforeBatch(self, position: BatchPosition) -> None:
        """Run the beforeBatch hook for the fetch at position, when one is set."""
        if self.beforeBatch is not None:
            await self.beforeBatch(position)


class StubRepository:
    """Stands in for ReferenceRepository and TransactionRepository, reading from a ReadLog."""

    def __init__(self, readLog: ReadLog, context: OnlineContext) -> None:
        self.readLog = readLog
        self.context = context
        self.readsStarted = 0

    async def BeginReportSnapshot(self) -> None:
        """Run the onReadsStart hook: the report, in its slot, begins its reads."""
        RunHook(self.readLog.onReadsStart)

    async def GetTypeDescriptions(self) -> dict[str, str]:
        """Return the TRANTYPE descriptions of the stub rows."""
        return dict(TYPE_DESCRIPTIONS)

    async def GetCategoryDescriptions(self) -> dict[tuple[str, int], str]:
        """Return the TRANCATG descriptions of the stub rows."""
        return dict(CATEGORY_DESCRIPTIONS)

    async def StartRowRead(self) -> int:
        """Record the free admissions, wait, then count the read or raise the failure.

        Returns the read's number among this report's TRANSACT passes.
        """
        readLog = self.readLog
        readLog.freeAdmissions.append(RequestAdmission(self.context.request).value)
        await anyio.sleep(readLog.readDelaySeconds)
        readLog.rowReads += 1
        if readLog.failure is not None:
            raise readLog.failure
        self.readsStarted += 1
        return self.readsStarted

    async def StreamReportTransactions(
        self, startDate: date, endDate: date, batchRows: int
    ) -> AsyncIterator[list[TrackedRow]]:
        """Yield watched copies of the stub rows in batches of batchRows, running the hooks."""
        readNumber = await self.StartRowRead()
        batches = list(itertools.batched(self.readLog.rows, batchRows))
        for batchIndex, batch in enumerate(batches):
            await self.readLog.BeforeBatch(BatchPosition(readNumber, batchIndex, len(batches)))
            yield self.readLog.TrackBatch(batch)
        RunHook(self.readLog.afterRowsRead)


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
    """Counts the reports between the start of their reads and the end of their build."""

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
            user=CurrentUser(userId=TEST_USER_ID, userType=REGULAR_USER_TYPE),
        )

    def FreeAdmissions(self) -> int:
        """Return how many request admissions of the stub application are free."""
        return RequestAdmission(self.Context().request).value

    def Held(self) -> tuple[int, int, int]:
        """Return the borrowed report slots and output tokens and the free admissions."""
        return (
            corpt00c.REPORT_SLOTS.borrowed_tokens,
            corpt00c.REPORT_OUTPUT_BUDGET.borrowed_tokens,
            self.FreeAdmissions(),
        )


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
    # The settings bound the reads' admission wait, as main.CreateApp keeps them on app.state.
    application = SimpleNamespace(state=State({"settings": testSettings}))
    return ReportHarness(readLog, buildObserver, application, testSettings, monkeypatch)


@dataclass
class ChunkCollector:
    """Plays the response that drains one report stream through corpt00c.StreamReport.

    ``stopAfterChunks`` makes it stop reading early, as the response does once its client has
    gone; ``beforeReceiving`` runs before it reads the first chunk.
    """

    reportStream: corpt00c.ReportStream
    stopAfterChunks: int | None = None
    beforeReceiving: Callable[[], Awaitable[None]] | None = None
    chunks: list[bytes] = field(default_factory=list)
    endedNormally: bool = False
    firstChunk: anyio.Event = field(default_factory=anyio.Event)

    async def Run(self) -> None:
        """Run the report with this collector as its response."""
        await corpt00c.StreamReport(self.reportStream, self.Receive)

    async def Receive(self) -> None:
        """Read the chunks as they come, noting whether the stream ended normally."""
        if self.beforeReceiving is not None:
            await self.beforeReceiving()
        async for chunk in self.reportStream.chunks:
            self.chunks.append(bytes(chunk))
            self.firstChunk.set()
            if len(self.chunks) == self.stopAfterChunks:
                return
        self.endedNormally = True

    def Document(self) -> bytes:
        """Return the chunks received so far, joined."""
        return b"".join(self.chunks)


def StreamOf(
    entryName: str, context: OnlineContext, query: ReportQuery
) -> bytes | corpt00c.ReportStream:
    """Return what the route of a stream entry sends: a pending screen's bytes or a stream."""
    if entryName == JSON_ENTRY:
        return corpt00c.PrepareTransactionReportJson(context, query)
    exportFormat = ExportFormat(entryName)
    return corpt00c.ExportTransactionReport(context, query, exportFormat).reportStream


async def RunEntry(entryName: str, context: OnlineContext, query: ReportQuery) -> object:
    """Run CR00 through the entry point entryName names.

    The entries are the screen model, the screen JSON the route sends, and each download format;
    a confirmed report's stream is drained as its response would drain it.
    """
    if entryName == SCREEN_ENTRY:
        return await corpt00c.RunTransactionReport(context, query)
    screen = StreamOf(entryName, context, query)
    if isinstance(screen, bytes):
        return screen
    collector = ChunkCollector(screen)
    await collector.Run()
    return collector.Document()


def ExpectedResult(entryName: str, rows: Sequence[Mapping[str, Any]]) -> object:
    """Return what RunEntry gives for the confirmed report over rows through entryName."""
    screen = corpt00c.SubmittedResponse(CONFIRMED_RANGE, ReportLinesOf(rows))
    if entryName == SCREEN_ENTRY:
        return screen
    if entryName == JSON_ENTRY:
        return RESPONSE_MODEL_ADAPTER.dump_json(screen, by_alias=True)
    render = RenderCsv if entryName == ExportFormat.CSV.value else RenderPdf
    return render(screen.lines)


def UseSmallBatchesAndChunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fetch SMALL_BATCH_ROWS rows at a time and send SMALL_CHUNK_BYTES chunks."""
    monkeypatch.setattr(corpt00c, "REPORT_BATCH_ROWS", SMALL_BATCH_ROWS)
    monkeypatch.setattr(report_export, "REPORT_CHUNK_BYTES", SMALL_CHUNK_BYTES)


def HoldingsOf(context: OnlineContext) -> Holdings:
    """Return the admissions, pooled connections and transaction the request holds now."""
    return Holdings(
        freeAdmissions=RequestAdmission(context.request).value,
        checkedOutConnections=context.engine.pool.checkedout(),
        sessionInTransaction=context.session.in_transaction(),
        onMainThread=threading.current_thread() is threading.main_thread(),
    )


def ObserveReadHoldings(
    monkeypatch: pytest.MonkeyPatch, context: OnlineContext, readHoldings: list[Holdings]
) -> None:
    """Record what the request holds as each TRANSACT pass of its report starts."""
    streamRead = TransactionRepository.StreamReportTransactions

    async def ObservedRead(repository, startDate, endDate, batchRows):
        readHoldings.append(HoldingsOf(context))
        async for rowBatch in streamRead(repository, startDate, endDate, batchRows):
            yield rowBatch

    monkeypatch.setattr(TransactionRepository, "StreamReportTransactions", ObservedRead)


async def WaitUntil(condition: Callable[[], bool]) -> None:
    """Poll condition every POLL_SECONDS until it holds, failing after BUILD_DEADLINE_SECONDS."""
    with anyio.fail_after(BUILD_DEADLINE_SECONDS):
        while not condition():
            await anyio.sleep(POLL_SECONDS)


async def WaitForBuildEnd(context: OnlineContext, idleHoldings: list[tuple[Holdings, int]]) -> None:
    """Wait, reading no chunk, until the report frees its slot; then record what it holds.

    Each record pairs the request's holdings with the borrowed REPORT_OUTPUT_BUDGET tokens.
    """
    await WaitUntil(lambda: corpt00c.REPORT_SLOTS.borrowed_tokens == 0)
    idleHoldings.append((HoldingsOf(context), corpt00c.REPORT_OUTPUT_BUDGET.borrowed_tokens))


@pytest.mark.parametrize("entryName", STREAM_ENTRIES)
async def test_reads_and_build_hold_one_admission_and_connection_until_the_build_ends(
    makeOnlineContext, buildObserver, monkeypatch, entryName
):
    """Both passes and the build share one admission and one connection in a transaction.

    Once the build ends, before the response has read a chunk, only the report's output token is
    held, and the response's end returns it.
    """
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    readHoldings: list[Holdings] = []
    buildHoldings: list[Holdings] = []
    idleHoldings: list[tuple[Holdings, int]] = []
    ObserveReadHoldings(monkeypatch, context, readHoldings)
    buildObserver.onBuildStart = lambda: buildHoldings.append(HoldingsOf(context))
    reportStream = StreamOf(entryName, context, CONFIRMED_QUERY)
    waitForBuildEnd = functools.partial(WaitForBuildEnd, context, idleHoldings)
    collector = ChunkCollector(reportStream, beforeReceiving=waitForBuildEnd)

    await collector.Run()

    readHolding = Holdings(ADMITTED_REQUEST_LIMIT - 1, 1, True, True)
    assert readHoldings == [readHolding, readHolding]
    assert buildHoldings == [readHolding._replace(onMainThread=False)]
    idleHolding = Holdings(ADMITTED_REQUEST_LIMIT, 0, False, True)
    assert idleHoldings == [(idleHolding, 1)]
    assert collector.endedNormally
    assert collector.Document() == ExpectedResult(entryName, [])
    assert (HoldingsOf(context), corpt00c.REPORT_OUTPUT_BUDGET.borrowed_tokens) == (idleHolding, 0)


async def test_at_most_report_worker_limit_reports_read_and_build_at_once(harness):
    """Concurrent reports pass through the slots; a report waiting for one holds no admission."""
    gauge = SlotGauge()
    harness.readLog.readDelaySeconds = READ_DELAY_SECONDS
    harness.readLog.onReadsStart = gauge.Enter
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


# The client is gone when the slot is granted or goes during the validation pass, through every
# entry; or, for the listed screen, whose response has not started, it goes once the first line
# is built, with a line check every MID_BUILD_CHECK_LINES lines of the build pass.
EARLY_DISCONNECT_CASES = (
    DisconnectCase("slot-granted", ReportProgress(0, 0, 0), 0),
    DisconnectCase("after-reads", ReportProgress(1, 0, 0), 0),
)
MID_BUILD_DISCONNECT_CASE = DisconnectCase(
    "mid-build", ReportProgress(BUILD_PASS_READ, 1, 0), MID_BUILD_CHECK_LINES
)
CLIENT_CLOSED_CASES = [
    *(
        pytest.param(case, entryName, id=f"{case.stage}-{entryName}")
        for case in EARLY_DISCONNECT_CASES
        for entryName in ENTRIES
    ),
    pytest.param(MID_BUILD_DISCONNECT_CASE, SCREEN_ENTRY, id=f"mid-build-{SCREEN_ENTRY}"),
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


@pytest.mark.parametrize(("disconnectCase", "entryName"), CLIENT_CLOSED_CASES)
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
    """The 503 literal and the abend 999 propagate unwrapped, and nothing stays held.

    No line is built: the validation pass meets both before the build pass starts.
    """
    ArmFailure(failureName, harness)

    raisedError = await RaisedBy(entryName, harness.Context())

    if failureName == "read-failure":
        assert isinstance(raisedError, LegacyMessageError)
        outcome = (raisedError.httpStatus, raisedError.message)
        assert outcome == (FILE_ERROR_STATUS, UNABLE_TO_WRITE_TDQ_MESSAGE)
    else:
        assert type(raisedError) is cbtrn03c.ReportAbendError
    assert harness.buildObserver.buildsStarted == 0
    assert harness.Held() == NOTHING_HELD


@pytest.mark.parametrize("entryName", ENTRIES)
async def test_a_report_refused_admission_reads_nothing_and_answers_the_tdq_literal(
    harness, caplog, entryName
):
    """Past the admission bound the reads never run: 503, the CR00 log line, nothing held."""
    caplog.set_level(logging.INFO, logger=corpt00c.LOGGER.name)
    shortWait = {"DB_CONNECT_TIMEOUT_SECONDS": SHORT_ADMISSION_WAIT_SECONDS}
    harness.application.state.settings = harness.settings.model_copy(update=shortWait)
    admission = RequestAdmission(harness.Context().request)
    async with contextlib.AsyncExitStack() as heldAdmissions:
        for _ in range(ADMITTED_REQUEST_LIMIT):
            await heldAdmissions.enter_async_context(admission)
        raisedError = await RaisedBy(entryName, harness.Context())

    assert isinstance(raisedError, LegacyMessageError)
    outcome = (raisedError.httpStatus, raisedError.message, raisedError.field)
    assert outcome == (FILE_ERROR_STATUS, UNABLE_TO_WRITE_TDQ_MESSAGE, CURSOR_REPORT_TYPE)
    assert (harness.readLog.rowReads, harness.readLog.freeAdmissions) == (0, [])
    logged = [(record.levelno, record.getMessage()) for record in caplog.records]
    assert logged == [ADMISSION_REFUSED_LOG]
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


@contextlib.asynccontextmanager
async def EveryTokenBorrowed(limiter: anyio.CapacityLimiter) -> AsyncIterator[list[object]]:
    """Borrow every token of limiter on behalf of plain objects and yield those borrowers.

    On exit each borrower still in the list returns its token; one the caller pops is its own
    to release.
    """
    borrowers: list[object] = []
    try:
        for _ in range(int(limiter.total_tokens)):
            borrower = object()
            await limiter.acquire_on_behalf_of(borrower)
            borrowers.append(borrower)
        yield borrowers
    finally:
        for borrower in borrowers:
            limiter.release_on_behalf_of(borrower)


@pytest.mark.parametrize("editCase", EDIT_CASES, ids=EDIT_IDS)
async def test_edits_and_confirmation_answer_while_every_report_slot_and_output_token_is_taken(
    harness, editCase
):
    """A prompt, a cleared screen or an edit failure waits for no report slot or output token."""
    async with (
        EveryTokenBorrowed(corpt00c.REPORT_SLOTS),
        EveryTokenBorrowed(corpt00c.REPORT_OUTPUT_BUDGET),
    ):
        with anyio.fail_after(EDIT_DEADLINE_SECONDS):
            outcome = await EditOutcome(editCase, harness.Context())

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
    """The screen route's JSON is the dump_json of RunTransactionReport's response."""
    harness.readLog.rows = parityCase.rows
    query = ReportQuery.model_validate({**CONFIRMED_QUERY_FIELDS, **parityCase.queryFields})

    screenJson = await RunEntry(JSON_ENTRY, harness.Context(), query)
    response = await corpt00c.RunTransactionReport(harness.Context(), query)

    assert response.lines == parityCase.expectedLines
    assert screenJson == RESPONSE_MODEL_ADAPTER.dump_json(response, by_alias=True)
    assert harness.Held() == NOTHING_HELD


def FailListing(job: corpt00c.ReportJob, rows: corpt00c.PassRows) -> list[ReportLine]:
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

    screenJson = await RunEntry(JSON_ENTRY, harness.Context(), CONFIRMED_QUERY)

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


async def AwaitFirstChunkBeforeLastBuildBatch(
    collector: ChunkCollector, position: BatchPosition
) -> None:
    """Hold the build pass's last row batch back until the response has received a chunk."""
    isLastBuildBatch = position.readNumber == BUILD_PASS_READ and (
        position.batchIndex == position.batchCount - 1
    )
    if isLastBuildBatch:
        with anyio.fail_after(BUILD_DEADLINE_SECONDS):
            await collector.firstChunk.wait()


@pytest.mark.parametrize("entryName", STREAM_ENTRIES)
async def test_the_first_chunk_is_sent_before_the_last_row_batch_is_read(
    harness, monkeypatch, entryName
):
    """The screen JSON, the CSV and the PDF go out as they are built.

    The last row batch of the build pass is held back until a chunk arrives, so a build that
    sent nothing before reading every row would time out. The PDF's first page is drawn from
    rows read before that batch.
    """
    UseSmallBatchesAndChunks(monkeypatch)
    harness.readLog.rows = BuildRows(LARGEST_ROW_COUNT)
    collector = ChunkCollector(StreamOf(entryName, harness.Context(), CONFIRMED_QUERY))
    harness.readLog.beforeBatch = functools.partial(AwaitFirstChunkBeforeLastBuildBatch, collector)

    await collector.Run()

    assert collector.endedNormally
    assert collector.Document() == ExpectedResult(entryName, harness.readLog.rows)
    assert harness.Held() == NOTHING_HELD


@pytest.mark.parametrize("entryName", ENTRIES)
async def test_a_report_never_holds_more_than_two_row_batches(harness, monkeypatch, entryName):
    """Each pass holds the batch it reads, and the one before while it fetches the next."""
    UseSmallBatchesAndChunks(monkeypatch)
    harness.readLog.rows = BuildRows(LARGEST_ROW_COUNT)

    result = await RunEntry(entryName, harness.Context(), CONFIRMED_QUERY)

    assert result == ExpectedResult(entryName, harness.readLog.rows)
    assert harness.readLog.rowReads == BUILD_PASS_READ
    assert max(harness.readLog.liveRowCounts) <= 2 * SMALL_BATCH_ROWS < LARGEST_ROW_COUNT


@pytest.mark.parametrize("entryName", STREAM_ENTRIES)
async def test_a_document_larger_than_a_chunk_is_sent_in_chunks_of_the_chunk_size(
    harness, monkeypatch, entryName
):
    """Every chunk but the last holds at least REPORT_CHUNK_BYTES; joined, they are the document."""
    monkeypatch.setattr(report_export, "REPORT_CHUNK_BYTES", SMALL_CHUNK_BYTES)
    harness.readLog.rows = BuildRows(LARGEST_ROW_COUNT)
    collector = ChunkCollector(StreamOf(entryName, harness.Context(), CONFIRMED_QUERY))

    await collector.Run()

    chunkSizes = [len(chunk) for chunk in collector.chunks]
    assert len(chunkSizes) > 1
    assert all(chunkSize >= SMALL_CHUNK_BYTES for chunkSize in chunkSizes[:-1])
    assert chunkSizes[-1] > 0
    assert collector.Document() == ExpectedResult(entryName, harness.readLog.rows)


@pytest.mark.parametrize("entryName", STREAM_ENTRIES)
async def test_a_client_gone_mid_build_stops_the_stream_with_one_info_record(
    harness, caplog, monkeypatch, entryName
):
    """The build stops at its next chunk once the response stops reading and logs once at INFO.

    Slot and admission are freed, and nothing is raised, because the response has started.
    """
    caplog.set_level(logging.INFO, logger=corpt00c.LOGGER.name)
    monkeypatch.setattr(report_export, "REPORT_CHUNK_BYTES", SMALL_CHUNK_BYTES)
    harness.readLog.rows = BuildRows(THREE_PDF_PAGE_ROW_COUNT)
    assert len(ReportLinesOf(harness.readLog.rows)) > 2 * report_export.LinesPerPage()
    reportStream = StreamOf(entryName, harness.Context(), CONFIRMED_QUERY)
    collector = ChunkCollector(reportStream, stopAfterChunks=1)

    await collector.Run()

    observer = harness.buildObserver
    assert (len(collector.chunks), collector.endedNormally) == (1, False)
    assert (observer.buildsStarted, observer.buildsFinished) == (1, 0)
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.INFO, STOP_LOG_MESSAGE)
    ]
    assert harness.Held() == NOTHING_HELD


async def FailTheBuildPassAtItsSecondBatch(position: BatchPosition) -> None:
    """Fail the build pass as a CICS IOERR on TRANSACT once its first batch has been read."""
    if (position.readNumber, position.batchIndex) == (BUILD_PASS_READ, 1):
        raise FileIoError(TRANSACT_FILE_NAME)


@pytest.mark.parametrize("entryName", STREAM_ENTRIES)
async def test_a_failed_build_pass_raises_and_never_ends_the_body(
    harness, caplog, monkeypatch, entryName
):
    """A read failure after the response started logs the file condition and raises.

    The body never gets its last chunk, so the client sees an incomplete transfer; nothing
    stays held.
    """
    UseSmallBatchesAndChunks(monkeypatch)
    harness.readLog.rows = BuildRows(LARGEST_ROW_COUNT)
    harness.readLog.beforeBatch = FailTheBuildPassAtItsSecondBatch
    collector = ChunkCollector(StreamOf(entryName, harness.Context(), CONFIRMED_QUERY))

    with pytest.RaisesGroup(FileIoError):
        await collector.Run()

    assert not collector.endedNormally
    serviceRecords = [record for record in caplog.records if record.name == corpt00c.LOGGER.name]
    assert [record.levelno for record in serviceRecords] == [logging.ERROR]
    assert harness.Held() == NOTHING_HELD


class BufferWait(NamedTuple):
    """What a report holds while its build waits for room in its full buffer."""

    buildsFinished: int
    held: tuple[int, int, int]
    bufferedChunks: int


def BuildWaitsForRoom(reportStream: corpt00c.ReportStream) -> bool:
    """Return True once the buffer is full and the build is waiting to send one more chunk."""
    statistics = reportStream.chunks.statistics()
    isFull = statistics.current_buffer_used == statistics.max_buffer_size
    return isFull and statistics.tasks_waiting_send == 1


def UseOneChunkBuffers(monkeypatch: pytest.MonkeyPatch, harness: ReportHarness) -> None:
    """Buffer ONE_CHUNK_BUFFER chunks of SMALL_CHUNK_BYTES of a THREE_PDF_PAGE_ROW_COUNT report."""
    monkeypatch.setattr(corpt00c, "REPORT_BUFFER_CHUNKS", ONE_CHUNK_BUFFER)
    monkeypatch.setattr(report_export, "REPORT_CHUNK_BYTES", SMALL_CHUNK_BYTES)
    harness.readLog.rows = BuildRows(THREE_PDF_PAGE_ROW_COUNT)


async def RecordBuildWaitingForRoom(
    harness: ReportHarness, reportStream: corpt00c.ReportStream, bufferWaits: list[BufferWait]
) -> None:
    """Wait, reading no chunk, until the build waits for room; then record what is held."""
    await WaitUntil(functools.partial(BuildWaitsForRoom, reportStream))
    bufferedChunks = reportStream.chunks.statistics().current_buffer_used
    buildsFinished = harness.buildObserver.buildsFinished
    bufferWaits.append(BufferWait(buildsFinished, harness.Held(), bufferedChunks))


@pytest.mark.parametrize("entryName", STREAM_ENTRIES)
async def test_a_full_buffer_makes_the_build_wait_until_the_response_reads(
    harness, monkeypatch, entryName
):
    """While its buffer is full the build waits, keeping its slot, admission and output token.

    Once the response reads, the build goes on and the whole document arrives.
    """
    UseOneChunkBuffers(monkeypatch, harness)
    reportStream = StreamOf(entryName, harness.Context(), CONFIRMED_QUERY)
    bufferWaits: list[BufferWait] = []
    recordWait = functools.partial(RecordBuildWaitingForRoom, harness, reportStream, bufferWaits)
    collector = ChunkCollector(reportStream, beforeReceiving=recordWait)

    await collector.Run()

    heldWhileWaiting = (1, 1, ADMITTED_REQUEST_LIMIT - 1)
    assert bufferWaits == [BufferWait(0, heldWhileWaiting, ONE_CHUNK_BUFFER)]
    assert collector.endedNormally
    assert collector.Document() == ExpectedResult(entryName, harness.readLog.rows)
    assert harness.Held() == NOTHING_HELD


async def StopOnceTheBuildWaits(
    reportStream: corpt00c.ReportStream, observer: BuildObserver, linesBuilt: list[int]
) -> None:
    """Play a response that reads nothing and stops once the build waits for room.

    The lines built by then are recorded in linesBuilt.
    """
    await WaitUntil(functools.partial(BuildWaitsForRoom, reportStream))
    linesBuilt.append(observer.linesBuilt)


@pytest.mark.parametrize("entryName", STREAM_ENTRIES)
async def test_a_build_waiting_for_room_stops_at_once_when_the_response_stops_reading(
    harness, caplog, monkeypatch, entryName
):
    """No line is built after the response stops; one INFO record, and nothing stays held."""
    caplog.set_level(logging.INFO, logger=corpt00c.LOGGER.name)
    UseOneChunkBuffers(monkeypatch, harness)
    reportStream = StreamOf(entryName, harness.Context(), CONFIRMED_QUERY)
    observer = harness.buildObserver
    linesBuilt: list[int] = []
    stopReading = functools.partial(StopOnceTheBuildWaits, reportStream, observer, linesBuilt)

    await corpt00c.StreamReport(reportStream, stopReading)

    assert linesBuilt == [observer.linesBuilt]
    assert (observer.buildsStarted, observer.buildsFinished) == (1, 0)
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.INFO, STOP_LOG_MESSAGE)
    ]
    assert harness.Held() == NOTHING_HELD


async def test_a_report_waits_for_an_output_token_holding_no_slot_or_admission(harness):
    """With every output token borrowed, a confirmed download waits before its slot and reads.

    Once one token is returned, it runs to the whole document.
    """
    entryName = ExportFormat.CSV.value
    collector = ChunkCollector(StreamOf(entryName, harness.Context(), CONFIRMED_QUERY))
    budget = corpt00c.REPORT_OUTPUT_BUDGET
    async with EveryTokenBorrowed(budget) as borrowers, anyio.create_task_group() as taskGroup:
        taskGroup.start_soon(collector.Run)
        await WaitUntil(lambda: budget.statistics().tasks_waiting == 1)
        heldWhileWaiting = (harness.readLog.rowReads, harness.Held())
        budget.release_on_behalf_of(borrowers.pop())

    assert heldWhileWaiting == (0, (0, corpt00c.REPORT_STREAM_LIMIT, ADMITTED_REQUEST_LIMIT))
    assert collector.endedNormally
    assert collector.Document() == ExpectedResult(entryName, harness.readLog.rows)
    assert harness.Held() == NOTHING_HELD


async def NeverAccept(message: Message) -> None:
    """Play a client that has stopped reading without disconnecting: no message is taken."""
    await anyio.sleep_forever()


def BuildSentEveryChunk(reportStream: corpt00c.ReportStream) -> bool:
    """Return True once the build has sent its last chunk and closed its end of the buffer."""
    return reportStream.chunks.statistics().open_send_streams == 0


class StallCase(NamedTuple):
    """Where the client stops reading, and how many builds have finished when the report stops."""

    stage: str
    expectedBuildsFinished: int


# The client stops reading while the build waits for room in a full one-chunk buffer, so the
# build stops unfinished; or once the build has put the whole stub report into its buffer.
STALL_CASES = [StallCase("build-waiting-for-room", 0), StallCase("build-ended", 1)]


def ArmStall(stage: str, harness: ReportHarness) -> Callable[[corpt00c.ReportStream], bool]:
    """Return the condition on which the client of the STALL_CASES stage named stage stalls."""
    if stage == "build-waiting-for-room":
        UseOneChunkBuffers(harness.monkeypatch, harness)
        return BuildWaitsForRoom
    return BuildSentEveryChunk


async def StallOnce(
    reportStream: corpt00c.ReportStream, stallCondition: Callable[[corpt00c.ReportStream], bool]
) -> None:
    """Play a response that, once stallCondition holds, sends its start to a stalled client."""
    await WaitUntil(functools.partial(stallCondition, reportStream))
    await corpt00c.SendWithinTimeout(NeverAccept, RESPONSE_START_MESSAGE)


@pytest.mark.parametrize("entryName", STREAM_ENTRIES)
@pytest.mark.parametrize("stallCase", STALL_CASES, ids=[case.stage for case in STALL_CASES])
async def test_a_client_that_stops_reading_stops_the_report_with_one_info_record(
    harness, caplog, stallCase, entryName
):
    """Past the send bound the response stops, and a build waiting for room stops with it.

    StreamReport returns, only the stall is logged, and nothing stays held.
    """
    caplog.set_level(logging.INFO, logger=corpt00c.LOGGER.name)
    harness.monkeypatch.setattr(corpt00c, "REPORT_SEND_TIMEOUT_SECONDS", SHORT_SEND_TIMEOUT_SECONDS)
    stallCondition = ArmStall(stallCase.stage, harness)
    reportStream = StreamOf(entryName, harness.Context(), CONFIRMED_QUERY)

    with anyio.fail_after(BUILD_DEADLINE_SECONDS):
        await corpt00c.StreamReport(
            reportStream, functools.partial(StallOnce, reportStream, stallCondition)
        )

    observer = harness.buildObserver
    expectedBuilds = (1, stallCase.expectedBuildsFinished)
    assert (observer.buildsStarted, observer.buildsFinished) == expectedBuilds
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.INFO, STALL_LOG_MESSAGE)
    ]
    assert harness.Held() == NOTHING_HELD


@dataclass
class StalledClient:
    """Plays a client that takes the response start, then stops reading without disconnecting."""

    messages: list[Message] = field(default_factory=list)

    async def Send(self, message: Message) -> None:
        """Record message; take the start, and never return for a body message."""
        self.messages.append(message)
        if message["type"] != RESPONSE_START_MESSAGE["type"]:
            await anyio.sleep_forever()


@pytest.mark.parametrize("entryName", STREAM_ENTRIES)
async def test_the_report_response_bounds_every_send_and_never_ends_a_stalled_body(
    harness, caplog, entryName
):
    """Under uvicorn's ASGI spec, the router's response returns once its client stops reading.

    The start is sent and the first chunk never taken, so no last body message follows; the
    stall is logged once and nothing stays held.
    """
    caplog.set_level(logging.INFO, logger=corpt00c.LOGGER.name)
    harness.monkeypatch.setattr(corpt00c, "REPORT_SEND_TIMEOUT_SECONDS", SHORT_SEND_TIMEOUT_SECONDS)
    reportStream = StreamOf(entryName, harness.Context(), CONFIRMED_QUERY)
    response = ReportStreamingResponse(reportStream, REPORT_MEDIA_TYPES[entryName])
    client = StalledClient()

    with anyio.fail_after(BUILD_DEADLINE_SECONDS):
        await response(UVICORN_HTTP_SCOPE, ClientChannel().Receive, client.Send)

    sent = [(message["type"], message.get("more_body")) for message in client.messages]
    assert sent == [(RESPONSE_START_MESSAGE["type"], None), ("http.response.body", True)]
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.INFO, STALL_LOG_MESSAGE)
    ]
    assert harness.Held() == NOTHING_HELD
