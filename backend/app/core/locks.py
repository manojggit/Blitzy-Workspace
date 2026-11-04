"""Batch-window advisory locks of the online programs, and the TRAN-ID allocation lock key.

Replaces CLOSEFIL/OPENFIL (CEMT SET FIL CLO/OPE for TRANSACT, CCXREF, ACCTDAT, CXACAIX, USRSEC)
and the CICS NOTOPEN condition (RESP 19, RESP2 60). Specified in the Technical Specification,
Section 0.6.10.

A file CLOSEFIL closes is also UNENABLED: every online READ, STARTBR, WRITE or REWRITE on it
returns NOTOPEN until OPENFIL runs, and the program takes its WHEN OTHER branch for that file.
CARDDAT, CARDAIX and CUSTDAT stay open, so cards.cards and customers.customers are not window
tables.

The batch CLI (batch/orchestration/batch_window.py) holds pg_advisory_lock(BATCH_WINDOW_LOCK),
session-level and exclusive, for each command. The repositories of WINDOW_TABLES await
EnterOnlineWindow before their statements. The first call of a request checks out one connection
and takes the same key shared, without waiting; the lock is then held on that connection for the
whole request, whatever the request session and its write units commit. PostgreSQL also refuses
the shared request while an exclusive one is queued, so once the window starts closing, new
online access fails at once while in-flight requests finish, as a CEMT close waits for current
users. A refusal raises FileNotOpenError, a RepositoryError like any database failure, so each
service returns its program's WHEN OTHER literal with HTTP 503. LeaveOnlineWindow, in the
request dependency's finally, releases the lock and the connection.

A grant is not trusted for the rest of the request: every later call first confirms, on the
window connection, that its session still holds the shared lock. Should that session end, by a
network failure or pg_terminate_backend, PostgreSQL drops the lock and batch may close the
window; the call then discards the connection and raises FileIoError (IOERR), as does every
later call of the request, which never takes the lock again. No statement on a window file
therefore runs once the request's hold has gone.

Nor does a write admitted before the loss become durable after it. A repository whose statement
runs on a write unit passes that unit, and the unit is fenced: just before its COMMIT, WriteUnit
calls ConfirmWindowForCommit, which asks on the unit's own connection, inside its transaction,
whether the session granted the request's window, named by its pid and backend_start, still
holds the shared lock. When it does not, the unit is rolled back with FileIoError and the
request's window is recorded lost. The unit itself never takes the lock.

This is a leaf module: it imports only the standard library, anyio, asyncpg, SQLAlchemy,
FastAPI's Request and app.core.errors, and reads no settings at import, so batch imports the
lock keys without a SECRET_KEY or a database. Batch never calls EnterOnlineWindow, because its
job connections would conflict with its own exclusive window.
"""

import logging
import weakref
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import NamedTuple

import asyncpg
from anyio import fail_after
from fastapi import Request
from sqlalchemy import BigInteger, DateTime, Integer, TextClause, bindparam, text
from sqlalchemy.exc import DBAPIError, PendingRollbackError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.errors import CICS_RESP_NOTOPEN, FileIoError, RepositoryError

LOGGER = logging.getLogger(__name__)

# Keys in PostgreSQL's single-bigint advisory-lock space, spelling the ASCII bytes "CDMOBATC"
# and "CDMOTRAN". The batch CLI and the backend must agree on them, so they never change.
BATCH_WINDOW_LOCK = 0x43444D4F42415443
TRAN_ID_ALLOCATION_LOCK = 0x43444D4F5452414E

# Qualified table name -> the CICS files CLOSEFIL closes that the table replaces.
WINDOW_TABLES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "transactions.transactions": ("TRANSACT",),
        "xref.card_xref": ("CCXREF", "CXACAIX"),
        "accounts.accounts": ("ACCTDAT",),
        "security.users": ("USRSEC",),
    }
)

CICS_RESP2_NOTOPEN_CLOSED = 60

TRY_SHARED_WINDOW_SQL = "SELECT pg_try_advisory_lock_shared(:lockKey)"
RELEASE_SHARED_WINDOW_SQL = "SELECT pg_advisory_unlock_shared(:lockKey)"
# pg_locks shows a single-bigint key as classid (its high 32 bits) and objid (its low 32 bits)
# with objsubid 1, and lists the locks of every database.
HOLDS_SHARED_WINDOW_SQL = (
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND objsubid = 1"
    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    " AND ((classid::bigint << 32) | objid::bigint) = :lockKey"
    " AND mode = 'ShareLock' AND granted AND pid = pg_backend_pid())"
)
# PostgreSQL gives a pid to a new session once the old one ends, so a session is named by its pid
# and its backend_start together.
WINDOW_HOLDER_SQL = "SELECT pid, backend_start FROM pg_stat_get_activity(pg_backend_pid())"
# Run on a write unit's connection: whether the session so named still holds the window shared.
UNIT_WINDOW_HELD_SQL = (
    "SELECT EXISTS (SELECT 1 FROM pg_locks"
    " JOIN pg_stat_get_activity(:holderPid) AS holder USING (pid)"
    " WHERE locktype = 'advisory' AND objsubid = 1"
    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    " AND ((classid::bigint << 32) | objid::bigint) = :lockKey"
    " AND mode = 'ShareLock' AND granted AND holder.backend_start = :backendStart)"
)

WINDOW_CONNECTION_ATTRIBUTE = "onlineWindowConnection"
WINDOW_HOLDER_ATTRIBUTE = "onlineWindowHolder"
WINDOW_STATE_ATTRIBUTE = "onlineWindowState"
WINDOW_GRANTED = "GRANTED"
WINDOW_REFUSED = "REFUSED"
# The window connection's session no longer held the shared lock it was granted.
WINDOW_LOST = "LOST"

# Reported as CICS IOERR: the failure set of app.core.database.UNIT_FAILURES, kept here so this
# module stays a leaf. Catching only DBAPIError and OSError would not do: the asyncpg dialect
# raises connect-time rejections (bad credentials, unknown database, too many connections)
# unwrapped, and a pool checkout that waits past pool_timeout raises PoolTimeoutError. asyncpg
# raises InternalClientError, untranslated, when PostgreSQL ends the session mid-operation
# (server shutdown, pg_terminate_backend), and SQLAlchemy raises PendingRollbackError for a
# statement on a connection a lost connection invalidated inside its transaction.
WINDOW_FAILURES = (
    DBAPIError,
    OSError,
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    asyncpg.InternalClientError,
    PendingRollbackError,
    PoolTimeoutError,
)

UNLOCK_FAILED_LOG = "Batch-window unlock failed (%s); the connection is discarded instead"

# app.core.database.DEADLINE_EXECUTION_OPTION, kept here so this module stays a leaf: the engine
# execution option holding the seconds a checkout may take, absent from engines without one.
DEADLINE_EXECUTION_OPTION = "carddemo_deadline_seconds"
# The request.state attribute of the engine a request takes its own connections from when it is
# not app.state.engine: app.core.database stores its refusing engine there for a request that
# must never check out a pooled connection. Defined here so this module stays a leaf.
REQUEST_ENGINE_ATTRIBUTE = "requestEngine"


def LockKeyStatement(sql: str) -> TextClause:
    """Bind :lockKey as BIGINT, the single-key form batch locks exclusively."""
    return text(sql).bindparams(bindparam("lockKey", type_=BigInteger))


TRY_SHARED_WINDOW_STATEMENT = LockKeyStatement(TRY_SHARED_WINDOW_SQL)
RELEASE_SHARED_WINDOW_STATEMENT = LockKeyStatement(RELEASE_SHARED_WINDOW_SQL)
HOLDS_SHARED_WINDOW_STATEMENT = LockKeyStatement(HOLDS_SHARED_WINDOW_SQL)
WINDOW_HOLDER_STATEMENT = text(WINDOW_HOLDER_SQL)
UNIT_WINDOW_HELD_STATEMENT = text(UNIT_WINDOW_HELD_SQL).bindparams(
    bindparam("lockKey", type_=BigInteger),
    bindparam("holderPid", type_=Integer),
    bindparam("backendStart", type_=DateTime(timezone=True)),
)


class WindowHolder(NamedTuple):
    """The PostgreSQL session granted a request's shared window lock."""

    pid: int
    backendStart: datetime


class UnitFence(NamedTuple):
    """The window hold a gated write unit's COMMIT depends on, and the file a refusal names."""

    request: Request
    holder: WindowHolder | None
    fileName: str


# Write-unit connection -> its UnitFence. The keys are weak, so an entry ends with its unit's
# AsyncConnection, a new object at every checkout; connection.info, kept by the pooled driver
# connection, would carry the fence onto a later checkout.
UNIT_FENCES: weakref.WeakKeyDictionary[AsyncConnection, UnitFence] = weakref.WeakKeyDictionary()


class FileNotOpenError(RepositoryError):
    """CICS NOTOPEN (RESP 19, RESP2 60): the batch window has closed the file to online use."""

    def __init__(self, fileName: str) -> None:
        super().__init__(fileName, resp=CICS_RESP_NOTOPEN, resp2=CICS_RESP2_NOTOPEN_CLOSED)


def RequestEngine(request: Request) -> AsyncEngine:
    """Return the engine of the request's window connection and write units.

    That is the engine stored under REQUEST_ENGINE_ATTRIBUTE, whose every connection fails at
    once, for a request app.core.database refused or a route that does no file I/O, and
    ``request.app.state.engine`` for every other request.
    """
    requestEngine: AsyncEngine | None = getattr(request.state, REQUEST_ENGINE_ATTRIBUTE, None)
    if requestEngine is None:
        return request.app.state.engine
    return requestEngine


def WindowState(request: Request) -> str | None:
    """Return WINDOW_GRANTED, WINDOW_REFUSED, WINDOW_LOST, or None before the request enters."""
    return getattr(request.state, WINDOW_STATE_ATTRIBUTE, None)


def StoreWindowState(
    request: Request,
    state: str | None,
    connection: AsyncConnection | None,
    holder: WindowHolder | None = None,
) -> None:
    setattr(request.state, WINDOW_STATE_ATTRIBUTE, state)
    setattr(request.state, WINDOW_CONNECTION_ATTRIBUTE, connection)
    setattr(request.state, WINDOW_HOLDER_ATTRIBUTE, holder)


async def OpenWindowConnection(engine: AsyncEngine, fileName: str) -> AsyncConnection:
    """Check out the request's window connection; a connection failure becomes FileIoError.

    The checkout and its pre-ping run within the engine's DEADLINE_EXECUTION_OPTION seconds,
    whose expiry is a TimeoutError and so a connection failure too.
    """
    try:
        with fail_after(engine.get_execution_options().get(DEADLINE_EXECUTION_OPTION)):
            return await engine.connect()
    except WINDOW_FAILURES as exc:
        raise FileIoError(fileName) from exc


async def AcquireSharedWindow(connection: AsyncConnection) -> WindowHolder | None:
    """Try the shared window lock without waiting, then end the transaction.

    Returns the connection's session when the lock is granted, None when it is refused. The
    commit leaves the connection idle rather than idle in transaction; a session-level advisory
    lock survives it.
    """
    result = await connection.execute(TRY_SHARED_WINDOW_STATEMENT, {"lockKey": BATCH_WINDOW_LOCK})
    holder = None
    if result.scalar_one():
        holder = WindowHolder(*(await connection.execute(WINDOW_HOLDER_STATEMENT)).one())
    await connection.commit()
    return holder


async def DiscardWindowConnection(connection: AsyncConnection) -> None:
    """Close a connection that may hold the shared lock without returning it to the pool.

    Invalidating closes the driver connection, so PostgreSQL ends the session and drops its
    advisory locks; the pool's reset on return only rolls back and would keep the lock.
    """
    await connection.invalidate()
    await connection.close()


async def LockWindowOrDiscard(connection: AsyncConnection, fileName: str) -> WindowHolder | None:
    """Return the session granted the shared window lock on the connection, or None if refused.

    Any failure, a task cancellation included, discards the connection, because the lock may
    have been granted before the failure. A database failure becomes FileIoError.
    """
    acquired = False
    try:
        holder = await AcquireSharedWindow(connection)
        acquired = True
        return holder
    except WINDOW_FAILURES as exc:
        raise FileIoError(fileName) from exc
    finally:
        if not acquired:
            await DiscardWindowConnection(connection)


async def HoldsSharedWindow(connection: AsyncConnection) -> bool:
    """Return whether the connection's session still holds the shared window lock.

    The commit leaves the connection idle rather than idle in transaction.
    """
    result = await connection.execute(HOLDS_SHARED_WINDOW_STATEMENT, {"lockKey": BATCH_WINDOW_LOCK})
    held = bool(result.scalar_one())
    await connection.commit()
    return held


async def ConfirmWindowOrDiscard(request: Request, fileName: str) -> None:
    """Confirm the request's granted window still holds; else record it lost and refuse.

    A session found without the lock, or a database failure in the check, discards the
    connection, records WINDOW_LOST and raises FileIoError, chained to the failure if any.
    """
    connection: AsyncConnection = getattr(request.state, WINDOW_CONNECTION_ATTRIBUTE)
    failure: Exception | None = None
    try:
        if await HoldsSharedWindow(connection):
            return
    except WINDOW_FAILURES as exc:
        failure = exc
    await RecordWindowLost(request, connection)
    raise FileIoError(fileName) from failure


async def RecordWindowLost(request: Request, connection: AsyncConnection) -> None:
    StoreWindowState(request, WINDOW_LOST, None)
    await DiscardWindowConnection(connection)


async def TakeSharedWindow(request: Request, fileName: str) -> None:
    """Take the request's shared window on a connection of its own; refused, raise NOTOPEN."""
    connection = await OpenWindowConnection(RequestEngine(request), fileName)
    holder = await LockWindowOrDiscard(connection, fileName)
    if holder is None:
        await connection.close()
        StoreWindowState(request, WINDOW_REFUSED, None)
        raise FileNotOpenError(fileName)
    StoreWindowState(request, WINDOW_GRANTED, connection, holder)


def FenceUnit(request: Request, unit: AsyncConnection, fileName: str) -> None:
    """Make the unit's COMMIT depend on the request's window hold (ConfirmWindowForCommit)."""
    holder: WindowHolder | None = getattr(request.state, WINDOW_HOLDER_ATTRIBUTE, None)
    UNIT_FENCES[unit] = UnitFence(request, holder, fileName)


async def EnterOnlineWindow(
    request: Request, fileName: str, unit: AsyncConnection | None = None
) -> None:
    """Gate a repository statement on a window file, as CICS gated a file CLOSEFIL closed.

    ``fileName`` is the legacy CICS file the statement addresses (TRANSACT, CCXREF, CXACAIX,
    ACCTDAT or USRSEC) and is named by the raised condition. ``unit`` is the write unit the
    statement runs on, None for the request session. The first call of a request takes the
    shared window lock on a connection of its own from RequestEngine; a later call of a granted
    request first confirms that the connection's session still holds it. Once admitted, a given
    unit is fenced (FenceUnit), so its COMMIT confirms the hold again. Raises FileNotOpenError
    (NOTOPEN) while batch holds or awaits the window, on this call and every later one in the
    request. Raises FileIoError when the database cannot be reached or the request was refused
    admission, and a later call then tries again; and once a granted lock is found gone, on
    this call and every later one, which never takes the lock again.
    """
    state = WindowState(request)
    if state == WINDOW_REFUSED:
        raise FileNotOpenError(fileName)
    if state == WINDOW_LOST:
        raise FileIoError(fileName)
    if state == WINDOW_GRANTED:
        await ConfirmWindowOrDiscard(request, fileName)
    else:
        await TakeSharedWindow(request, fileName)
    if unit is not None:
        FenceUnit(request, unit, fileName)


async def UnitSeesWindowHeld(unit: AsyncConnection, holder: WindowHolder | None) -> bool:
    """Return whether ``holder`` still holds the shared window, asked on the unit's connection.

    A fence recorded without a granted session fails closed.
    """
    if holder is None:
        return False
    parameters = {
        "lockKey": BATCH_WINDOW_LOCK,
        "holderPid": holder.pid,
        "backendStart": holder.backendStart,
    }
    result = await unit.execute(UNIT_WINDOW_HELD_STATEMENT, parameters)
    return bool(result.scalar_one())


async def ConfirmWindowForCommit(unit: AsyncConnection) -> None:
    """Refuse the COMMIT of a gated write unit whose request no longer holds its window.

    WriteUnit calls it just before the COMMIT. A unit no repository gated (CUSTDAT, CARDDAT, the
    login throttle) is not checked. A gated unit's check runs inside its transaction. When the
    session granted the request's window no longer holds it, that window, if the request still
    has it, is recorded lost, and FileIoError of the gated file is raised, so WriteUnit rolls
    the unit back. A database failure in the check propagates for WriteUnit to translate.
    """
    fence = UNIT_FENCES.pop(unit, None)
    if fence is None or await UnitSeesWindowHeld(unit, fence.holder):
        return
    await RecordFencedWindowLost(fence)
    raise FileIoError(fence.fileName)


async def RecordFencedWindowLost(fence: UnitFence) -> None:
    """Record the fence's window lost, unless its request has since recorded it or left it."""
    requestState = fence.request.state
    if fence.holder is None or getattr(requestState, WINDOW_HOLDER_ATTRIBUTE, None) != fence.holder:
        return
    await RecordWindowLost(fence.request, getattr(requestState, WINDOW_CONNECTION_ATTRIBUTE))


async def ReleaseSharedWindow(connection: AsyncConnection) -> None:
    """Release the shared window lock held on the connection and end the transaction."""
    await connection.execute(RELEASE_SHARED_WINDOW_STATEMENT, {"lockKey": BATCH_WINDOW_LOCK})
    await connection.commit()


async def LeaveOnlineWindow(request: Request) -> None:
    """Release the request's shared window lock and its connection; harmless when none is held.

    The state is cleared first, so a repeated call finds nothing to release. When the unlock
    fails or is cancelled, the connection is discarded rather than pooled, and PostgreSQL drops
    the lock with the session. The failure is logged, not raised, so the request's own outcome
    stands.
    """
    connection = getattr(request.state, WINDOW_CONNECTION_ATTRIBUTE, None)
    if connection is None:
        return
    StoreWindowState(request, None, None)
    released = False
    try:
        await ReleaseSharedWindow(connection)
        released = True
    except WINDOW_FAILURES as exc:
        LOGGER.warning(UNLOCK_FAILED_LOG, type(exc).__name__)
    finally:
        if released:
            await connection.close()
        else:
            await DiscardWindowConnection(connection)
