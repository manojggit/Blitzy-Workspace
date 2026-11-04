"""Integration tests of online requests while the database stops answering or goes away.

Replaces: the WHEN OTHER file-error branches of COCRDSLC 9100-GETCARD-BYACCTCARD and COCRDUPC
9100-GETCARD-BYACCTCARD (cbl/COCRDSLC.cbl, cbl/COCRDUPC.cbl: the WS-FILE-ERROR-MESSAGE READ
literal), of the COTRN02C STARTBR/READPREV on TRANSACT (cbl/COTRN02C.cbl: ``Unable to lookup
Transaction...``), of the COBIL00C REWRITE of ACCTDAT (cbl/COBIL00C.cbl line 399: ``Unable to
Update Account...``) and of the CORPT00C TDQ write that the CR00 report reads replace
(cbl/CORPT00C.cbl line 531: ``Unable to Write TDQ (JOBS)...``). Covers the Technical
Specification, Section 0.6.5 (asyncpg runs under DB_CONNECT_TIMEOUT_SECONDS and
DB_COMMAND_TIMEOUT_SECONDS; WHEN OTHER literals answer 503 with the legacy body) and Section
0.6.13 (bounded database timeouts), for QA findings QA2A-F01, QA2B-F02 and QA2C-F01:

- a database that accepts connections and never answers (``docker pause``): two consecutive
  card reads, CCDL and the CCUP fetch, each answer 503 with the CARDDAT READ literal within
  EngineDeadline of app.core.database plus a margin, whether the stall meets the pool's
  pre-ping at its BEGIN or inside its transaction, and the app answers again once the database
  does; so do two consecutive confirmed CR00 reports, the screen and the CSV download, with the
  TDQ literal, although their snapshot checkout and cursor start bypass DeadlineSession.execute;
  a statement past the command timeout discards its connection within GRACEFUL_CLOSE_SECONDS
  and closes its socket while the database is still stalled (QAPF2-F01);
- a database that goes away mid-request (``docker stop``, or pg_terminate_backend of the app's
  backends, which sends PostgreSQL's FATAL admin-shutdown): CT02 and CB00 waiting on the TRAN-ID
  allocation lock answer their program's 503 literal, never a plain-text 500 from request
  teardown. CT02 writes nothing; CB00 runs every step and keeps what its last step committed,
  whether all its connections are lost or only its TRANSACT unit's;
- the outage helper itself failing, its wait for the lock waiter giving up or its outage step
  raising: the CT02 request is cancelled and reaped while the TRAN-ID lock is still held, so
  that failure propagates with no task, lock wait or write of the request left.

The app reaches the test database only through StallableProxy (tests/database_proxy.py), so
no test pauses, stops or restarts the shared PostgreSQL server; pg_terminate_backend ends only
client backends of this test database other than the test's own. The TRAN-ID lock is held on a
sync connection of the test, and waits are observed in pg_locks of this test database only.
Clients, users and the database are fixtures of tests/conftest.py, so no password appears
here; restoreSeedState undoes every write.
"""

import asyncio
import contextlib
import re
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import BigInteger, Engine, TextClause, bindparam, text

from app.core.config import Settings
from app.core.database import GRACEFUL_CLOSE_SECONDS
from app.core.locks import TRAN_ID_ALLOCATION_LOCK
from tests.database_proxy import ProxiedDatabaseUrl, ProxyOf, StallableProxy

# pytest reads a module's marks only under this exact lower-case name. Usefixtures are set up
# first and torn down last, so the app engine is disposed of before the seed restore runs.
pytestmark = pytest.mark.usefixtures("restoreSeedState")

# --- Driver timeouts of the proxied app, and the bounds asserted -------------------------------
CONNECT_TIMEOUT_SECONDS = 1.0
COMMAND_TIMEOUT_SECONDS = 3.0
# EngineDeadline of BuildEngine: one connection plus one statement.
DEADLINE_SECONDS = CONNECT_TIMEOUT_SECONDS + COMMAND_TIMEOUT_SECONDS
# How long after its bound an answer may still arrive.
RESPONSE_MARGIN_SECONDS = 1.5
# A request still running after this long fails the test instead of hanging it.
REQUEST_LIMIT_SECONDS = 15.0
WAIT_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.05
# Shorter than one poll: the wait gives up after its first poll, with the request on its way.
SHORT_WAIT_TIMEOUT_SECONDS = 0.01
# pg_terminate_backend waits this long for each backend to exit.
TERMINATE_TIMEOUT_MILLISECONDS = 5000

# --- HTTP ----------------------------------------------------------------------------------------
CARDS_URL = "/api/v1/cards"
TRANSACTIONS_URL = "/api/v1/transactions"
BILLPAY_URL = "/api/v1/billpay"
REPORTS_URL = "/api/v1/reports/transactions"
REPORT_EXPORT_URL = REPORTS_URL + "/export"
EDIT_SUFFIX = "/edit"
HTTP_OK = 200
HTTP_FILE_ERROR = 503
LEGACY_ERROR_KEYS = frozenset({"message", "info_message", "field_errors", "cursor_field"})
ACCT_ID_FIELD = "acct_id"
# CORPT00C's cursor on the report type (MOVE -1 TO MONTHLYL), as the ReportQuery wire name.
REPORT_TYPE_FIELD = "reportType"

# --- Sample data: data/EBCDIC ACCTDATA, CARDDATA and CARDXREF images -----------------------------
# Account 00000000004 has one card, 2988091353094312, and a seed balance of 40.00.
SAMPLE_ACCOUNT_ID = "00000000004"
SAMPLE_CARD_NUMBER = "2988091353094312"
CARD_KEY_QUERY: Mapping[str, str] = MappingProxyType({"acctId": SAMPLE_ACCOUNT_ID})
CARD_VIEW_URL = f"{CARDS_URL}/{SAMPLE_CARD_NUMBER}"
CARD_READ_URLS = (CARD_VIEW_URL, CARD_VIEW_URL + EDIT_SUFFIX)

# --- Literals, verbatim from the COBOL sources ---------------------------------------------------
# WS-FILE-ERROR-MESSAGE of COCRDSLC and COCRDUPC for READ on CARDDAT with IOERR (RESP 17).
CARD_READ_FILE_ERROR = "File Error: READ     on CARDDAT   returned RESP 000000017 ,RESP2 000000000"
TRAN_LOOKUP_FAILED_MESSAGE = "Unable to lookup Transaction..."
# CORPT00C WIRTE-JOBSUB-TDQ WHEN OTHER (line 531), which answers every failed report read.
REPORT_WRITE_FAILED_MESSAGE = "Unable to Write TDQ (JOBS)..."
UPDATE_ACCOUNT_FAILED_MESSAGE = "Unable to Update Account..."
ADD_PAYMENT_FAILED_MESSAGE = "Unable to Add Bill pay Transaction..."
SEED_BALANCE = Decimal("40.00")
PAID_BALANCE = Decimal("0.00")

# --- Request bodies ------------------------------------------------------------------------------
# A Transaction Add ENTER every COTRN02C edit accepts, keyed by account, confirmed with Y.
TRANSACTION_ADD_BODY: Mapping[str, str] = MappingProxyType(
    {
        "action": "enter",
        "acct_id": SAMPLE_ACCOUNT_ID,
        "card_num": "",
        "type_cd": "01",
        "cat_cd": "0001",
        "source": "POS TERM",
        "desc": "OUTAGE PURCHASE",
        "amount": "+00000050.00",
        "orig_date": "2022-07-17",
        "proc_date": "2022-07-17",
        "merchant_id": "000000001",
        "merchant_name": "OUTAGE MERCHANT",
        "merchant_city": "OUTAGE CITY",
        "merchant_zip": "00000",
        "confirm": "Y",
    }
)
PAY_BODY: Mapping[str, str] = MappingProxyType({"acct_id": SAMPLE_ACCOUNT_ID, "confirm": "Y"})
# A CORPT00 ENTER confirming the monthly report (the current month) with Y.
MONTHLY_REPORT_QUERY: Mapping[str, str] = MappingProxyType(
    {REPORT_TYPE_FIELD: "monthly", "confirm": "Y"}
)
# The confirmed report's screen and its CSV download: two consecutive CR00 requests.
REPORT_REQUESTS: tuple[tuple[str, Mapping[str, str]], ...] = (
    (REPORTS_URL, MONTHLY_REPORT_QUERY),
    (REPORT_EXPORT_URL, MappingProxyType({**MONTHLY_REPORT_QUERY, "format": "csv"})),
)

# --- How the database stops answering or goes away -----------------------------------------------
STALL_EVERYTHING = "stall-everything"
# The pool's pre-ping runs BEGIN, then its ping inside that transaction: the second statement.
STALL_INSIDE_PRE_PING = "stall-inside-pre-ping"
PRE_PING_STATEMENT_NUMBER = 2
CONNECTIONS_DROPPED = "connections-dropped"
BACKENDS_TERMINATED = "backends-terminated"
# Only the TRANSACT unit's backend, the one waiting on the TRAN-ID lock, is terminated.
UNIT_BACKEND_TERMINATED = "unit-backend-terminated"
OUTAGE_KINDS = (CONNECTIONS_DROPPED, BACKENDS_TERMINATED, UNIT_BACKEND_TERMINATED)
# The pg_terminate_backend passes of each termination outage: False ends the app's backends that
# wait on no lock, True the one waiting on the TRAN-ID lock. Ending the waiter last holds the
# request until every other backend of it has ended, so its later steps meet them gone.
TERMINATION_PASSES: Mapping[str, tuple[bool, ...]] = MappingProxyType(
    {BACKENDS_TERMINATED: (False, True), UNIT_BACKEND_TERMINATED: (True,)}
)


@dataclass(frozen=True)
class PaymentOutage:
    """A CB00 outage at the TRAN-ID lock, with the literal and balance its sequence ends in."""

    outageKind: str
    message: str
    balance: Decimal


PAYMENT_OUTAGES = tuple(
    pytest.param(PaymentOutage(outageKind, message, balance), id=outageKind)
    for outageKind, message, balance in (
        (CONNECTIONS_DROPPED, UPDATE_ACCOUNT_FAILED_MESSAGE, SEED_BALANCE),
        (BACKENDS_TERMINATED, UPDATE_ACCOUNT_FAILED_MESSAGE, SEED_BALANCE),
        (UNIT_BACKEND_TERMINATED, ADD_PAYMENT_FAILED_MESSAGE, PAID_BALANCE),
    )
)

# --- SQL with bound values only ------------------------------------------------------------------
ALLOCATION_LOCK_STATEMENT = text("SELECT pg_advisory_xact_lock(:lockKey)").bindparams(
    bindparam("lockKey", type_=BigInteger)
)
# pg_locks shows a single-BIGINT advisory key as its high 32 bits in classid and its low 32
# bits in objid.
ALLOCATION_WAITERS_STATEMENT = text(
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    " AND classid::bigint = :highKey AND objid::bigint = :lowKey"
)
ALLOCATION_LOCK_PARTS: Mapping[str, int] = MappingProxyType(
    {
        "highKey": (TRAN_ID_ALLOCATION_LOCK >> 32) & 0xFFFFFFFF,
        "lowKey": TRAN_ID_ALLOCATION_LOCK & 0xFFFFFFFF,
    }
)
# PostgreSQL sees the proxy's connections through the container's port mapping, so they are
# told apart from the test's own by elimination: the terminating session and the lock holder.
TERMINATE_APP_BACKENDS_STATEMENT = text(
    "SELECT pg_terminate_backend(pid, :timeoutMilliseconds) FROM pg_stat_activity"
    " WHERE datname = current_database() AND backend_type = 'client backend'"
    " AND pid <> pg_backend_pid() AND pid <> :holderPid"
    " AND (wait_event_type IS NOT DISTINCT FROM 'Lock') = :waitingOnLock"
)
BACKEND_PID_STATEMENT = text("SELECT pg_backend_pid()")
TRANSACTION_COUNT_STATEMENT = text("SELECT count(*) FROM transactions.transactions")
ACCOUNT_BALANCE_STATEMENT = text(
    "SELECT curr_balance FROM accounts.accounts WHERE acct_id = :acctId"
)
PROBE_STATEMENT = text("SELECT 1")

# --- Failure messages ----------------------------------------------------------------------------
REQUEST_ENDED_MESSAGE = "The request ended before it waited on the TRAN-ID lock: {status} {body}"
WAIT_FAILED_MESSAGE = "No request waited on the TRAN-ID lock within {seconds} s."
REAPED_REQUEST_ERROR_NOTE = "The request, cancelled and reaped after this failure, raised {error}"
NOTHING_TERMINATED_MESSAGE = "pg_terminate_backend found no backend of the proxied app."
RELAYS_OPEN_MESSAGE = "{count} discarded connection socket(s) still open on the stalled database."
INJECTED_OUTAGE_MESSAGE = "The outage step failed before it ended any connection."
TASKS_LEFT_MESSAGE = "Tasks still running after SendDuringAllocationOutage failed: {tasks}"


# --- Failures injected into SendDuringAllocationOutage -------------------------------------------
class InjectedOutageError(RuntimeError):
    """The error a test makes the outage step raise, as a failing pg_terminate_backend would."""


def RaiseInjectedOutage(*outageArguments: Any) -> None:
    """Stand in for EndDatabaseConnections: raise InjectedOutageError, ending no connection."""
    raise InjectedOutageError(INJECTED_OUTAGE_MESSAGE)


@dataclass(frozen=True)
class HelperFailure:
    """A failure of SendDuringAllocationOutage, injected by patching one global of this module.

    For one test, ``globalName`` is ``replacement``, and the helper raises ``errorType`` with a
    message ``messagePattern`` matches.
    """

    globalName: str
    replacement: Any
    errorType: type[BaseException]
    messagePattern: str


HELPER_FAILURES = (
    pytest.param(
        HelperFailure(
            "WAIT_TIMEOUT_SECONDS",
            SHORT_WAIT_TIMEOUT_SECONDS,
            pytest.fail.Exception,
            re.escape(WAIT_FAILED_MESSAGE.format(seconds=SHORT_WAIT_TIMEOUT_SECONDS)),
        ),
        id="waiter-never-detected",
    ),
    pytest.param(
        HelperFailure(
            "EndDatabaseConnections",
            RaiseInjectedOutage,
            InjectedOutageError,
            re.escape(INJECTED_OUTAGE_MESSAGE),
        ),
        id="outage-step-raises",
    ),
)


# --- Fixtures ------------------------------------------------------------------------------------
@pytest_asyncio.fixture(name="stallableProxy")
async def StallableProxyFixture(
    databaseUrls: Any, syncEngine: Engine
) -> AsyncIterator[StallableProxy]:
    """Yield a started StallableProxy of the test database; close it at teardown.

    syncEngine refuses a database whose name lacks _test before the proxy relays to it.
    """
    stallableProxy = ProxyOf(databaseUrls.asyncUrl)
    await stallableProxy.Start()
    try:
        yield stallableProxy
    finally:
        await stallableProxy.Close()


@pytest.fixture(name="testSettings")
def ProxiedSettingsFixture(testSettings: Settings, stallableProxy: StallableProxy) -> Settings:
    """Return the conftest settings with the database behind the proxy and short timeouts.

    testApp, and so every client of tests/conftest.py, is built from these settings.
    """
    return testSettings.model_copy(
        update={
            "DATABASE_URL": ProxiedDatabaseUrl(testSettings.DATABASE_URL, stallableProxy.port),
            "DB_CONNECT_TIMEOUT_SECONDS": CONNECT_TIMEOUT_SECONDS,
            "DB_COMMAND_TIMEOUT_SECONDS": COMMAND_TIMEOUT_SECONDS,
        }
    )


@pytest_asyncio.fixture(name="outageProxy")
async def OutageProxyFixture(
    stallableProxy: StallableProxy, testApp: FastAPI
) -> AsyncIterator[StallableProxy]:
    """Yield the proxy of testApp's database; resume it before testApp disposes of its engine.

    It depends on testApp, so its teardown runs first, and a failed test's stalled or refused
    connections are released before the engine closes them.
    """
    try:
        yield stallableProxy
    finally:
        stallableProxy.Resume()


@pytest.fixture(name="helperFailure", params=HELPER_FAILURES)
def HelperFailureFixture(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> HelperFailure:
    """Return the parameter's HelperFailure, its module global patched for this test only."""
    helperFailure: HelperFailure = request.param
    monkeypatch.setitem(globals(), helperFailure.globalName, helperFailure.replacement)
    return helperFailure


# --- Requests and assertions ---------------------------------------------------------------------
async def WithinLimit(request: Awaitable[httpx.Response]) -> httpx.Response:
    """Await a request, raising TimeoutError after REQUEST_LIMIT_SECONDS instead of hanging."""
    async with asyncio.timeout(REQUEST_LIMIT_SECONDS):
        return await request


async def ReadCard(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET a CCDL or CCUP card screen of the sample card, with its account key."""
    return await WithinLimit(client.get(url, params=dict(CARD_KEY_QUERY)))


async def RequestReport(
    client: httpx.AsyncClient, url: str, query: Mapping[str, str]
) -> httpx.Response:
    """GET the confirmed CR00 report of ``query`` through ``url``, its screen or a download."""
    return await WithinLimit(client.get(url, params=dict(query)))


def AssertLegacyError(response: httpx.Response, message: str, cursorField: str = "") -> None:
    """Assert a 503 legacy error body carrying ``message``, and ``cursorField`` when given."""
    assert response.status_code == HTTP_FILE_ERROR, response.text
    body = response.json()
    assert LEGACY_ERROR_KEYS <= body.keys(), body
    assert body["message"] == message
    if cursorField:
        assert body["cursor_field"] == cursorField


async def WaitForRelaysClosed(stallableProxy: StallableProxy) -> None:
    """Poll until the proxy relays no open client socket; fail after WAIT_TIMEOUT_SECONDS."""
    eventLoop = asyncio.get_running_loop()
    deadline = eventLoop.time() + WAIT_TIMEOUT_SECONDS
    while stallableProxy.openClientCount:
        if eventLoop.time() >= deadline:
            pytest.fail(RELAYS_OPEN_MESSAGE.format(count=stallableProxy.openClientCount))
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def StallDatabase(stallPoint: str, stallableProxy: StallableProxy) -> None:
    """Make the proxied database stop answering now, or at the next pre-ping's ping."""
    if stallPoint == STALL_INSIDE_PRE_PING:
        stallableProxy.StallAtQuery(PRE_PING_STATEMENT_NUMBER)
    else:
        stallableProxy.Stall()


# --- The TRAN-ID lock, waits and outages, on sync connections of the test -----------------------
@contextlib.contextmanager
def HoldTranIdAllocationLock(syncEngine: Engine) -> Iterator[int]:
    """Hold pg_advisory_xact_lock(TRAN_ID_ALLOCATION_LOCK) until the block ends; yield its pid.

    The lock is taken in the connection's open transaction, and closing the connection at exit
    rolls it back, releasing the lock whatever the block raises.
    """
    with syncEngine.connect() as connection:
        connection.execute(ALLOCATION_LOCK_STATEMENT, {"lockKey": TRAN_ID_ALLOCATION_LOCK})
        yield connection.execute(BACKEND_PID_STATEMENT).scalar_one()


def CountAllocationWaiters(syncEngine: Engine) -> int:
    """Return how many sessions of the test database wait for the TRAN-ID allocation lock."""
    with syncEngine.connect() as connection:
        return connection.execute(
            ALLOCATION_WAITERS_STATEMENT, dict(ALLOCATION_LOCK_PARTS)
        ).scalar_one()


async def WaitForAllocationWait(
    syncEngine: Engine, requestTask: asyncio.Task[httpx.Response]
) -> None:
    """Poll until the request waits on the TRAN-ID lock; fail if it ends first or never waits."""
    eventLoop = asyncio.get_running_loop()
    deadline = eventLoop.time() + WAIT_TIMEOUT_SECONDS
    while eventLoop.time() < deadline:
        if requestTask.done():
            ended = requestTask.result()
            pytest.fail(REQUEST_ENDED_MESSAGE.format(status=ended.status_code, body=ended.text))
        if CountAllocationWaiters(syncEngine) > 0:
            return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(WAIT_FAILED_MESSAGE.format(seconds=WAIT_TIMEOUT_SECONDS))


def TerminateAppBackends(syncEngine: Engine, holderPid: int, passes: tuple[bool, ...]) -> None:
    """End the app's backends with pg_terminate_backend, as a server shutdown does.

    Each backend sends PostgreSQL's FATAL admin-shutdown error to its client and exits; each
    pass waits for its backends to exit. A pass that ends no lock waiter also ends the idle
    pooled connections of syncEngine, which its pre-ping replaces.
    """
    with syncEngine.connect() as connection:
        for waitingOnLock in passes:
            parameters = {
                "timeoutMilliseconds": TERMINATE_TIMEOUT_MILLISECONDS,
                "holderPid": holderPid,
                "waitingOnLock": waitingOnLock,
            }
            terminated = connection.execute(TERMINATE_APP_BACKENDS_STATEMENT, parameters)
            assert any(terminated.scalars()), NOTHING_TERMINATED_MESSAGE


def EndDatabaseConnections(
    outageKind: str, stallableProxy: StallableProxy, syncEngine: Engine, holderPid: int
) -> None:
    """Take the app's database connections away mid-request, as ``outageKind`` names."""
    if outageKind == CONNECTIONS_DROPPED:
        stallableProxy.DropConnections()
    else:
        TerminateAppBackends(syncEngine, holderPid, TERMINATION_PASSES[outageKind])


async def CancelAndReap(requestTask: asyncio.Task[httpx.Response]) -> None:
    """Cancel the request task unless it has ended, and wait until it has, raising nothing.

    Awaited from the caller's finally, so the failure ending the caller, if any, stays the
    error raised: gather with return_exceptions hands back how the task ended instead of
    raising it. Its cancellation aside, an error the task ended with, unless it is that failure
    itself (``sys.exception()``), is added to the failure as a note. A further cancellation of
    the caller is passed on to the task and propagates only once the task has ended.
    """
    requestTask.cancel()
    (outcome,) = await asyncio.gather(requestTask, return_exceptions=True)
    failure = sys.exception()
    if failure is None or isinstance(outcome, asyncio.CancelledError) or outcome is failure:
        return
    if isinstance(outcome, BaseException):
        failure.add_note(REAPED_REQUEST_ERROR_NOTE.format(error=repr(outcome)))


async def SendDuringAllocationOutage(
    request: Awaitable[httpx.Response],
    outageKind: str,
    stallableProxy: StallableProxy,
    syncEngine: Engine,
) -> httpx.Response:
    """Send a request that allocates a TRAN-ID and end its connections while it waits on the lock.

    The test holds the allocation lock, so the request's TRANSACT unit is blocked on it, with
    its session, window connection and any other unit open, when the database goes away. If
    the wait or the outage step fails, or this call is cancelled, before the request has ended,
    the request is cancelled and reaped while the lock is still held, so it allocates and writes
    nothing, and then that failure is re-raised.
    """
    with HoldTranIdAllocationLock(syncEngine) as holderPid:
        requestTask = asyncio.create_task(WithinLimit(request))
        try:
            await WaitForAllocationWait(syncEngine, requestTask)
            EndDatabaseConnections(outageKind, stallableProxy, syncEngine, holderPid)
            return await requestTask
        finally:
            await CancelAndReap(requestTask)


def ReadScalar(syncEngine: Engine, statement: TextClause, parameters: Mapping[str, str]) -> Any:
    """Return the single value ``statement`` reads now, on a sync connection of the test."""
    with syncEngine.connect() as connection:
        return connection.execute(statement, dict(parameters)).scalar_one()


def ReadDataState(syncEngine: Engine) -> tuple[int, Decimal]:
    """Return the TRANSACT row count and the sample account's committed balance."""
    return (
        ReadScalar(syncEngine, TRANSACTION_COUNT_STATEMENT, {}),
        ReadScalar(syncEngine, ACCOUNT_BALANCE_STATEMENT, {"acctId": SAMPLE_ACCOUNT_ID}),
    )


def PendingTasks() -> set[asyncio.Task[Any]]:
    """Return the event loop's unfinished tasks other than the running one."""
    return asyncio.all_tasks() - {asyncio.current_task()}


# --- Tests ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("stallPoint", (STALL_EVERYTHING, STALL_INSIDE_PRE_PING))
async def test_card_reads_answer_file_error_within_deadline_while_database_is_stalled(
    stallPoint: str, outageProxy: StallableProxy, regularClient: httpx.AsyncClient
) -> None:
    """Each consecutive CCDL and CCUP read answers 503 within the deadline, then 200 again.

    The pool is warm, so each read's checkout pre-pings a pooled connection on the stalled
    database, and every connection it gives up must be discarded without waiting on it.
    """
    assert (await ReadCard(regularClient, CARD_VIEW_URL)).status_code == HTTP_OK
    StallDatabase(stallPoint, outageProxy)
    for cardReadUrl in CARD_READ_URLS:
        startedAt = time.monotonic()
        response = await ReadCard(regularClient, cardReadUrl)
        elapsedSeconds = time.monotonic() - startedAt
        AssertLegacyError(response, CARD_READ_FILE_ERROR)
        assert elapsedSeconds < DEADLINE_SECONDS + RESPONSE_MARGIN_SECONDS, elapsedSeconds
    outageProxy.Resume()
    assert (await ReadCard(regularClient, CARD_VIEW_URL)).status_code == HTTP_OK


@pytest.mark.parametrize("stallPoint", (STALL_EVERYTHING, STALL_INSIDE_PRE_PING))
async def test_confirmed_reports_answer_tdq_literal_within_deadline_while_database_is_stalled(
    stallPoint: str, outageProxy: StallableProxy, regularClient: httpx.AsyncClient
) -> None:
    """Each consecutive CR00 screen and CSV report answers 503 within the deadline, then 200.

    The pool is warm, so the report snapshot's explicit checkout pre-pings a pooled connection
    on the stalled database before any statement runs; the session deadline still ends it, and
    the report's slot and admission are freed for the next request.
    """
    assert (await RequestReport(regularClient, *REPORT_REQUESTS[0])).status_code == HTTP_OK
    StallDatabase(stallPoint, outageProxy)
    for reportUrl, reportQuery in REPORT_REQUESTS:
        startedAt = time.monotonic()
        response = await RequestReport(regularClient, reportUrl, reportQuery)
        elapsedSeconds = time.monotonic() - startedAt
        AssertLegacyError(response, REPORT_WRITE_FAILED_MESSAGE, REPORT_TYPE_FIELD)
        assert elapsedSeconds < DEADLINE_SECONDS + RESPONSE_MARGIN_SECONDS, elapsedSeconds
    outageProxy.Resume()
    assert (await RequestReport(regularClient, *REPORT_REQUESTS[0])).status_code == HTTP_OK


async def test_statement_past_command_timeout_discards_connection_within_close_bound(
    outageProxy: StallableProxy, testApp: FastAPI
) -> None:
    """A statement on a stalled database fails at the command timeout and is discarded at once.

    The connection is used directly, as a write unit's body uses it, outside any deadline, so
    only the bounded graceful close keeps its invalidation from waiting for the whole stall.
    The discarded connection's socket is closed while the database is still stalled, so no
    server session outlives it.
    """
    async with testApp.state.engine.connect() as connection:
        await connection.execute(PROBE_STATEMENT)
        outageProxy.Stall()
        startedAt = time.monotonic()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(REQUEST_LIMIT_SECONDS):
                await connection.execute(PROBE_STATEMENT)
        elapsedSeconds = time.monotonic() - startedAt
        assert connection.invalidated
    bound = COMMAND_TIMEOUT_SECONDS + GRACEFUL_CLOSE_SECONDS + RESPONSE_MARGIN_SECONDS
    assert elapsedSeconds < bound, elapsedSeconds
    await WaitForRelaysClosed(outageProxy)


@pytest.mark.parametrize("outageKind", OUTAGE_KINDS)
async def test_transaction_add_answers_lookup_literal_when_database_goes_away_at_allocation(
    outageKind: str,
    outageProxy: StallableProxy,
    regularClient: httpx.AsyncClient,
    syncEngine: Engine,
) -> None:
    """CT02 waiting on the TRAN-ID lock answers 503 ``Unable to lookup Transaction...``.

    Its STARTBR/READPREV WHEN OTHER literal survives the teardown of its dead session and
    window connection, nothing is written, and the app answers again once the database does.
    """
    stateBefore = ReadDataState(syncEngine)
    addRequest = regularClient.post(TRANSACTIONS_URL, json=dict(TRANSACTION_ADD_BODY))
    response = await SendDuringAllocationOutage(addRequest, outageKind, outageProxy, syncEngine)
    AssertLegacyError(response, TRAN_LOOKUP_FAILED_MESSAGE, ACCT_ID_FIELD)
    assert ReadDataState(syncEngine) == stateBefore
    outageProxy.Resume()
    assert (await WithinLimit(regularClient.get(TRANSACTIONS_URL))).status_code == HTTP_OK


@pytest.mark.parametrize("paymentOutage", PAYMENT_OUTAGES)
async def test_bill_payment_answers_last_literal_when_database_goes_away_mid_payment(
    paymentOutage: PaymentOutage,
    outageProxy: StallableProxy,
    regularClient: httpx.AsyncClient,
    syncEngine: Engine,
) -> None:
    """CB00 losing connections at the TRAN-ID lock answers 503 with its last literal set.

    Every step still runs (COBIL00C lines 210-240). With the account unit gone too, the
    REWRITE's ``Unable to Update Account...`` is last and the balance is unchanged; with only
    the TRANSACT unit gone, the WRITE's ``Unable to Add Bill pay Transaction...`` is last and
    the balance is still reduced and rewritten. TRANSACT never gets the payment, and the app
    answers again once the database does.
    """
    transactionCount, balanceBefore = ReadDataState(syncEngine)
    assert balanceBefore == SEED_BALANCE
    payRequest = regularClient.post(BILLPAY_URL, json=dict(PAY_BODY))
    response = await SendDuringAllocationOutage(
        payRequest, paymentOutage.outageKind, outageProxy, syncEngine
    )
    AssertLegacyError(response, paymentOutage.message, ACCT_ID_FIELD)
    assert ReadDataState(syncEngine) == (transactionCount, paymentOutage.balance)
    outageProxy.Resume()
    assert (await WithinLimit(regularClient.get(TRANSACTIONS_URL))).status_code == HTTP_OK


async def test_failed_allocation_outage_reaps_its_request_before_releasing_the_lock(
    helperFailure: HelperFailure,
    outageProxy: StallableProxy,
    regularClient: httpx.AsyncClient,
    syncEngine: Engine,
) -> None:
    """SendDuringAllocationOutage failing mid-request reaps its CT02 request inside the lock.

    The wait gives up while the request is on its way, or the outage step fails while the
    request waits on the TRAN-ID lock. Checked at once, with no wait: that failure propagates,
    no task of the helper is left running, no session waits on the lock and nothing is written.
    """
    stateBefore = ReadDataState(syncEngine)
    tasksBefore = PendingTasks()
    addRequest = regularClient.post(TRANSACTIONS_URL, json=dict(TRANSACTION_ADD_BODY))
    with pytest.raises(helperFailure.errorType, match=helperFailure.messagePattern):
        await SendDuringAllocationOutage(addRequest, CONNECTIONS_DROPPED, outageProxy, syncEngine)
    tasksLeft = PendingTasks() - tasksBefore
    assert not tasksLeft, TASKS_LEFT_MESSAGE.format(tasks=tasksLeft)
    assert CountAllocationWaiters(syncEngine) == 0
    assert ReadDataState(syncEngine) == stateBefore
