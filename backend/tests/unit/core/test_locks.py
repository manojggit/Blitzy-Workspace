"""Unit tests for app.core.locks, the batch window the online repositories observe.

The unit replaces jcl/CLOSEFIL.jcl and jcl/OPENFIL.jcl, whose CEMT SET FIL(...) CLO and OPE close
and reopen TRANSACT, CCXREF, ACCTDAT, CXACAIX and USRSEC in the CICS region, and the CICS NOTOPEN
condition (RESP 19, RESP2 60) an online program meets on those files in between, as specified in
the Technical Specification, Section 0.6.10. The batch CLI holds the window as an exclusive
session-level advisory lock on BATCH_WINDOW_LOCK; these tests take that lock themselves on a
connection of the test database, so the online side is pinned without the batch package.

A write unit a repository gated is fenced: when its request's hold goes after the write and
before the unit's COMMIT, and batch takes the window meanwhile, the COMMIT is refused and the
write rolled back.

pg_locks spans every database of the server, so lock counts are limited to the test database.
Every request a test builds leaves the window in a finally block, and every exclusive lock is
unlocked before its connection goes back to the pool, which would otherwise keep it.
"""

import asyncio
import contextlib

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from starlette.applications import Starlette
from starlette.requests import Request

from app.core.database import WriteUnit
from app.core.errors import LOCK_NOT_AVAILABLE_SQLSTATE, FileIoError, RepositoryError, SqlStateOf
from app.core.locks import (
    BATCH_WINDOW_LOCK,
    TRAN_ID_ALLOCATION_LOCK,
    WINDOW_CONNECTION_ATTRIBUTE,
    WINDOW_LOST,
    WINDOW_TABLES,
    EnterOnlineWindow,
    FileNotOpenError,
    LeaveOnlineWindow,
    WindowState,
)

EXPECTED_WINDOW_TABLES = {
    "transactions.transactions",
    "xref.card_xref",
    "accounts.accounts",
    "security.users",
}
# The files CLOSEFIL closes; CARDDAT, CARDAIX and CUSTDAT stay open to online use.
EXPECTED_WINDOW_FILES = {"TRANSACT", "CCXREF", "CXACAIX", "ACCTDAT", "USRSEC"}
# card_xref replaces both the CCXREF cluster and its CXACAIX alternate-index path.
EXPECTED_XREF_FILES = {"CCXREF", "CXACAIX"}
CICS_RESP_NOTOPEN = 19
CICS_RESP2_NOTOPEN_CLOSED = 60
SIGNED_64_MIN = -(2**63)
SIGNED_64_MAX = 2**63 - 1
LOCK_KEY_BYTES = 8
SHARED_MODE = "ShareLock"
EXCLUSIVE_MODE = "ExclusiveLock"
WINDOW_LOCK_PARAMETERS = {"lockKey": BATCH_WINDOW_LOCK}
BATCH_LOCK_TIMEOUT = "10s"
# Expires while the in-flight request still holds the window, so the batch waiter fails.
EXPIRING_BATCH_LOCK_TIMEOUT = "200ms"
BATCH_WAIT_SECONDS = 10.0
QUEUE_POLL_SECONDS = 0.05
QUEUE_POLL_ATTEMPTS = 200

# pg_locks shows a single bigint key as its high and low 32 bits, classid and objid, with
# objsubid 1. Both keys are positive, so the halves need no sign handling.
ADVISORY_LOCK_COUNT_SQL = text(
    "SELECT COUNT(*) FROM pg_locks WHERE locktype = 'advisory'"
    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    " AND objsubid = 1 AND mode = :lockMode AND granted = :granted"
    " AND ((CAST(classid AS bigint) << 32) | CAST(objid AS bigint)) = :lockKey"
)
TRY_BATCH_WINDOW_SQL = text("SELECT pg_try_advisory_lock(:lockKey)")
WAIT_BATCH_WINDOW_SQL = text("SELECT pg_advisory_lock(:lockKey)")
RELEASE_BATCH_WINDOW_SQL = text("SELECT pg_advisory_unlock(:lockKey)")
SET_LOCK_TIMEOUT_SQL = text("SELECT set_config('lock_timeout', :lockTimeout, false)")
# The session of this database holding BATCH_WINDOW_LOCK shared: one request's window connection.
SHARED_HOLDER_PID_SQL = text(
    "SELECT pid FROM pg_locks WHERE locktype = 'advisory'"
    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    " AND objsubid = 1 AND mode = 'ShareLock' AND granted"
    " AND ((CAST(classid AS bigint) << 32) | CAST(objid AS bigint)) = :lockKey"
)
RELEASE_SHARED_WINDOW_SQL = text("SELECT pg_advisory_unlock_shared(:lockKey)")
# With a timeout, pg_terminate_backend returns once the backend has exited, its locks dropped.
TERMINATE_BACKEND_SQL = text("SELECT pg_terminate_backend(:backendPid, :timeoutMillis)")
TERMINATE_TIMEOUT_MILLISECONDS = 5000
# A USRSEC record a gated write renames; restoreSeedState, under asyncEngine, restores it.
FIRST_USER_SQL = text("SELECT user_id, first_name FROM security.users ORDER BY user_id LIMIT 1")
USER_FIRST_NAME_SQL = text("SELECT first_name FROM security.users WHERE user_id = :userId")
RENAME_USER_SQL = text("UPDATE security.users SET first_name = :firstName WHERE user_id = :userId")
FENCED_FIRST_NAME = "FENCED"


class HeldLock:
    """The batch side of the window: an exclusive BATCH_WINDOW_LOCK on one pooled connection."""

    def __init__(self, connection):
        self.connection = connection
        self.granted = False
        self.closed = False

    def TryTake(self):
        """Try the exclusive window without waiting; return and keep whether it was granted.

        ``granted`` is None while the answer is unknown, so a failed attempt is discarded.
        """
        self.granted = None
        self.granted = bool(
            self.connection.execute(TRY_BATCH_WINDOW_SQL, WINDOW_LOCK_PARAMETERS).scalar_one()
        )
        self.connection.commit()
        return self.granted

    def Release(self):
        """Unlock a granted window and close the connection, once; later calls do nothing.

        The pool keeps a session-level lock on a returned connection, so a connection that may
        still hold it is invalidated, which ends its session, instead of being returned.
        """
        if self.closed:
            return
        self.closed = True
        released = self.granted is False
        try:
            if self.granted:
                self.connection.execute(RELEASE_BATCH_WINDOW_SQL, WINDOW_LOCK_PARAMETERS)
                self.connection.commit()
                released = True
        finally:
            if not released:
                self.connection.invalidate()
            self.connection.close()


@pytest.fixture(name="heldBatchWindow")
def HeldBatchWindow(syncEngine):
    """Yield the window held exclusively, as the batch CLI holds it; release it at teardown."""
    heldLock = HeldLock(syncEngine.connect())
    try:
        assert heldLock.TryTake(), "BATCH_WINDOW_LOCK is already held in the test database"
        yield heldLock
    finally:
        heldLock.Release()


def BuildRequest(asyncEngine):
    """Return a bare GET request of an application whose state.engine is asyncEngine."""
    stubApp = Starlette()
    stubApp.state.engine = asyncEngine
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "app": stubApp,
            "state": {},
        }
    )


def CountAdvisoryLocks(syncEngine, lockMode, granted=True):
    """Return the test database's BATCH_WINDOW_LOCK locks in lockMode, granted or awaited."""
    with syncEngine.connect() as connection:
        lockParameters = {"lockMode": lockMode, "granted": granted, **WINDOW_LOCK_PARAMETERS}
        return connection.execute(ADVISORY_LOCK_COUNT_SQL, lockParameters).scalar_one()


def ReadFirstUser(syncEngine):
    """Return the committed user_id and first_name of the first USRSEC record in key order."""
    with syncEngine.connect() as connection:
        return connection.execute(FIRST_USER_SQL).one()


def ReadFirstName(syncEngine, userId):
    """Return the committed first_name of the USRSEC record ``userId``."""
    with syncEngine.connect() as connection:
        return connection.execute(USER_FIRST_NAME_SQL, {"userId": userId}).scalar_one()


def TryBatchWindow(syncEngine):
    """Return whether batch could take the window now; a granted window is released at once."""
    batchLock = HeldLock(syncEngine.connect())
    try:
        return batchLock.TryTake()
    finally:
        batchLock.Release()


async def WaitForBatchWindow(asyncEngine, lockTimeout):
    """Queue for the window exclusively, as the batch CLI does, then release it once granted.

    A wait longer than lockTimeout fails with SQLSTATE 55P03. asyncEngine is unpooled, so
    closing the connection on any failure also ends its session.
    """
    async with asyncEngine.connect() as connection:
        await connection.execute(SET_LOCK_TIMEOUT_SQL, {"lockTimeout": lockTimeout})
        await connection.execute(WAIT_BATCH_WINDOW_SQL, WINDOW_LOCK_PARAMETERS)
        releaseResult = await connection.execute(RELEASE_BATCH_WINDOW_SQL, WINDOW_LOCK_PARAMETERS)
        await connection.commit()
        return releaseResult.scalar_one()


async def WaitUntilBatchQueues(syncEngine):
    """Return once an exclusive request for the window is waiting; fail after the poll limit."""
    for _ in range(QUEUE_POLL_ATTEMPTS):
        if CountAdvisoryLocks(syncEngine, EXCLUSIVE_MODE, granted=False) == 1:
            return
        await asyncio.sleep(QUEUE_POLL_SECONDS)
    pytest.fail("No exclusive request for BATCH_WINDOW_LOCK started waiting")


async def CancelAndReap(task):
    """Cancel the task and wait for it to end, suppressing only its cancellation.

    A task that has already failed re-raises its error here, so the caller sees it.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def PendingTasks():
    """Return the event loop's unfinished tasks other than the running one."""
    return asyncio.all_tasks() - {asyncio.current_task()}


async def TerminateWindowSession(request, syncEngine):
    """End the request's window session from outside, as a dropped connection would."""
    with syncEngine.connect() as connection:
        holderPid = connection.execute(SHARED_HOLDER_PID_SQL, WINDOW_LOCK_PARAMETERS).scalar_one()
        parameters = {"backendPid": holderPid, "timeoutMillis": TERMINATE_TIMEOUT_MILLISECONDS}
        assert connection.execute(TERMINATE_BACKEND_SQL, parameters).scalar_one()


async def ReleaseWindowBehindTheRequest(request, syncEngine):
    """Drop the shared lock on the request's own window connection, its session left open."""
    windowConnection = getattr(request.state, WINDOW_CONNECTION_ATTRIBUTE)
    await windowConnection.execute(RELEASE_SHARED_WINDOW_SQL, WINDOW_LOCK_PARAMETERS)
    await windowConnection.commit()


# The two ways a granted request's hold can go: its session ends, or only its lock does.
WINDOW_LOSSES = [
    pytest.param(TerminateWindowSession, id="session-terminated"),
    pytest.param(ReleaseWindowBehindTheRequest, id="lock-released"),
]


@contextlib.asynccontextmanager
async def ClosingWindow(asyncEngine, batchLockTimeout):
    """Yield an in-flight request inside the window, a new request and a batch waiter's task.

    The waiter queues for the window exclusively for at most batchLockTimeout. At exit it is
    reaped first, inside a try whose finally leaves both requests' windows, so an error the
    waiter raised surfaces only once no shared lock or connection of the requests is left.
    """
    inFlightRequest = BuildRequest(asyncEngine)
    newRequest = BuildRequest(asyncEngine)
    await EnterOnlineWindow(inFlightRequest, "ACCTDAT")
    batchWait = asyncio.create_task(WaitForBatchWindow(asyncEngine, batchLockTimeout))
    try:
        yield inFlightRequest, newRequest, batchWait
    finally:
        try:
            await CancelAndReap(batchWait)
        finally:
            await LeaveOnlineWindow(inFlightRequest)
            await LeaveOnlineWindow(newRequest)


def test_window_tables_are_the_closefil_files():
    assert set(WINDOW_TABLES) == EXPECTED_WINDOW_TABLES
    windowFiles = {fileName for fileNames in WINDOW_TABLES.values() for fileName in fileNames}
    assert windowFiles == EXPECTED_WINDOW_FILES
    assert set(WINDOW_TABLES["xref.card_xref"]) == EXPECTED_XREF_FILES


# Batch and the backend must agree on each key, so each is pinned to the ASCII name it spells.
@pytest.mark.parametrize(
    ("lockKey", "otherKey", "spelledName"),
    [
        (BATCH_WINDOW_LOCK, TRAN_ID_ALLOCATION_LOCK, b"CDMOBATC"),
        (TRAN_ID_ALLOCATION_LOCK, BATCH_WINDOW_LOCK, b"CDMOTRAN"),
    ],
    ids=["batch-window", "tran-id-allocation"],
)
def test_lock_keys_are_distinct_signed_64_bit_integers(lockKey, otherKey, spelledName):
    assert type(lockKey) is int
    assert SIGNED_64_MIN <= lockKey <= SIGNED_64_MAX
    assert lockKey != otherKey
    assert lockKey.to_bytes(LOCK_KEY_BYTES, "big") == spelledName


def test_file_not_open_error_is_a_repository_error():
    notOpenError = FileNotOpenError("USRSEC")
    assert isinstance(notOpenError, RepositoryError)
    assert (notOpenError.fileName, notOpenError.resp, notOpenError.resp2) == (
        "USRSEC",
        CICS_RESP_NOTOPEN,
        CICS_RESP2_NOTOPEN_CLOSED,
    )


async def test_enter_window_is_refused_while_batch_holds_it(
    asyncEngine, heldBatchWindow, syncEngine
):
    request = BuildRequest(asyncEngine)
    try:
        with pytest.raises(FileNotOpenError) as refusal:
            await EnterOnlineWindow(request, "USRSEC")
        assert (refusal.value.fileName, refusal.value.resp, refusal.value.resp2) == (
            "USRSEC",
            CICS_RESP_NOTOPEN,
            CICS_RESP2_NOTOPEN_CLOSED,
        )
        assert CountAdvisoryLocks(syncEngine, SHARED_MODE) == 0
        assert CountAdvisoryLocks(syncEngine, EXCLUSIVE_MODE) == 1
    finally:
        await LeaveOnlineWindow(request)


async def test_refusal_is_cached_for_the_request(asyncEngine, heldBatchWindow, syncEngine):
    refusedRequest = BuildRequest(asyncEngine)
    laterRequest = BuildRequest(asyncEngine)
    try:
        with pytest.raises(FileNotOpenError):
            await EnterOnlineWindow(refusedRequest, "TRANSACT")
        heldBatchWindow.Release()
        with pytest.raises(FileNotOpenError) as cachedRefusal:
            await EnterOnlineWindow(refusedRequest, "CCXREF")
        assert cachedRefusal.value.fileName == "CCXREF"
        await EnterOnlineWindow(laterRequest, "CCXREF")
        assert CountAdvisoryLocks(syncEngine, SHARED_MODE) == 1
    finally:
        await LeaveOnlineWindow(refusedRequest)
        await LeaveOnlineWindow(laterRequest)
    assert CountAdvisoryLocks(syncEngine, SHARED_MODE) == 0


async def test_enter_window_takes_shared_lock_until_leave(asyncEngine, syncEngine):
    request = BuildRequest(asyncEngine)
    try:
        await EnterOnlineWindow(request, "ACCTDAT")
        assert CountAdvisoryLocks(syncEngine, SHARED_MODE) == 1
        await EnterOnlineWindow(request, "TRANSACT")
        assert CountAdvisoryLocks(syncEngine, SHARED_MODE) == 1
    finally:
        await LeaveOnlineWindow(request)
    assert CountAdvisoryLocks(syncEngine, SHARED_MODE) == 0
    await LeaveOnlineWindow(request)
    assert CountAdvisoryLocks(syncEngine, SHARED_MODE) == 0


@pytest.mark.parametrize("loseWindow", WINDOW_LOSSES)
async def test_lost_hold_fails_every_later_call_without_retaking_the_lock(
    asyncEngine, syncEngine, loseWindow
):
    """A granted request whose hold has gone answers IOERR from then on and never re-locks.

    Batch could take the window at once, so no statement of the request may reach a window file.
    """
    request = BuildRequest(asyncEngine)
    try:
        await EnterOnlineWindow(request, "ACCTDAT")
        await loseWindow(request, syncEngine)
        with pytest.raises(FileIoError) as lostHold:
            await EnterOnlineWindow(request, "TRANSACT")
        assert lostHold.value.fileName == "TRANSACT"
        assert WindowState(request) == WINDOW_LOST
        with pytest.raises(FileIoError):
            await EnterOnlineWindow(request, "USRSEC")
        assert CountAdvisoryLocks(syncEngine, SHARED_MODE) == 0
        assert TryBatchWindow(syncEngine) is True
    finally:
        await LeaveOnlineWindow(request)
    assert CountAdvisoryLocks(syncEngine, SHARED_MODE) == 0


@pytest.mark.parametrize("loseWindow", WINDOW_LOSSES)
async def test_gated_unit_whose_hold_goes_before_its_commit_is_rolled_back(
    asyncEngine, syncEngine, loseWindow
):
    """A write admitted inside the window never becomes durable once batch holds the window.

    The hold goes after the gated UPDATE and batch takes the window before the unit's COMMIT,
    which is then refused: the unit raises FileIoError and the request records its window lost.
    """
    request = BuildRequest(asyncEngine)
    user = ReadFirstUser(syncEngine)
    batchLock = HeldLock(syncEngine.connect())
    try:
        with pytest.raises(FileIoError) as refusedCommit:
            async with WriteUnit(asyncEngine, "USRSEC") as unit:
                await EnterOnlineWindow(request, "USRSEC", unit)
                renamed = {"userId": user.user_id, "firstName": FENCED_FIRST_NAME}
                await unit.execute(RENAME_USER_SQL, renamed)
                await loseWindow(request, syncEngine)
                assert batchLock.TryTake() is True
        assert refusedCommit.value.fileName == "USRSEC"
        assert ReadFirstName(syncEngine, user.user_id) == user.first_name
        assert WindowState(request) == WINDOW_LOST
    finally:
        batchLock.Release()
        await LeaveOnlineWindow(request)


async def test_concurrent_requests_share_the_window(asyncEngine, syncEngine):
    firstRequest = BuildRequest(asyncEngine)
    secondRequest = BuildRequest(asyncEngine)
    try:
        await EnterOnlineWindow(firstRequest, "USRSEC")
        await EnterOnlineWindow(secondRequest, "USRSEC")
        assert CountAdvisoryLocks(syncEngine, SHARED_MODE) == 2
    finally:
        await LeaveOnlineWindow(firstRequest)
        await LeaveOnlineWindow(secondRequest)
    assert CountAdvisoryLocks(syncEngine, SHARED_MODE) == 0


async def test_batch_lock_waits_for_online_request(asyncEngine, syncEngine):
    request = BuildRequest(asyncEngine)
    try:
        await EnterOnlineWindow(request, "CXACAIX")
        assert TryBatchWindow(syncEngine) is False
    finally:
        await LeaveOnlineWindow(request)
    assert TryBatchWindow(syncEngine) is True
    assert CountAdvisoryLocks(syncEngine, EXCLUSIVE_MODE) == 0


# A CEMT close waits for current users: the in-flight request keeps the window, and a request
# arriving while batch waits is refused at once instead of queueing behind it.
async def test_closing_window_refuses_new_requests_until_in_flight_ones_leave(
    asyncEngine, syncEngine
):
    closingWindow = ClosingWindow(asyncEngine, BATCH_LOCK_TIMEOUT)
    async with closingWindow as (inFlightRequest, newRequest, batchWait):
        await WaitUntilBatchQueues(syncEngine)
        with pytest.raises(FileNotOpenError):
            await EnterOnlineWindow(newRequest, "ACCTDAT")
        await LeaveOnlineWindow(inFlightRequest)
        assert await asyncio.wait_for(batchWait, BATCH_WAIT_SECONDS) is True
    assert CountAdvisoryLocks(syncEngine, EXCLUSIVE_MODE) == 0


# A batch waiter that fails while the in-flight request still holds the window does not stop
# the cleanup: both requests leave the window, and only then does the waiter's error surface.
async def test_closing_window_cleanup_releases_the_window_when_the_batch_waiter_fails(
    asyncEngine, syncEngine
):
    tasksBefore = PendingTasks()
    closingWindow = ClosingWindow(asyncEngine, EXPIRING_BATCH_LOCK_TIMEOUT)
    with pytest.raises(DBAPIError) as waiterFailure:
        async with closingWindow as (_, _, batchWait):
            await asyncio.wait({batchWait}, timeout=BATCH_WAIT_SECONDS)
            assert batchWait.done()
            assert CountAdvisoryLocks(syncEngine, SHARED_MODE) == 1
    assert SqlStateOf(waiterFailure.value) == LOCK_NOT_AVAILABLE_SQLSTATE
    assert any(entry.name == CancelAndReap.__name__ for entry in waiterFailure.traceback)
    assert CountAdvisoryLocks(syncEngine, SHARED_MODE) == 0
    assert CountAdvisoryLocks(syncEngine, EXCLUSIVE_MODE) == 0
    assert CountAdvisoryLocks(syncEngine, EXCLUSIVE_MODE, granted=False) == 0
    assert PendingTasks() <= tasksBefore
