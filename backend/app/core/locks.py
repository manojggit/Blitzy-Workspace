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

This is a leaf module: it imports only the standard library, asyncpg, SQLAlchemy, FastAPI's
Request and app.core.errors, and reads no settings at import, so batch imports the lock keys
without a SECRET_KEY or a database. Batch never calls EnterOnlineWindow, because its job
connections would conflict with its own exclusive window.
"""

import logging
from collections.abc import Mapping
from types import MappingProxyType

import asyncpg
from fastapi import Request
from sqlalchemy import BigInteger, TextClause, bindparam, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.errors import FileIoError, RepositoryError

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

CICS_RESP_NOTOPEN = 19
CICS_RESP2_NOTOPEN_CLOSED = 60

TRY_SHARED_WINDOW_SQL = "SELECT pg_try_advisory_lock_shared(:lockKey)"
RELEASE_SHARED_WINDOW_SQL = "SELECT pg_advisory_unlock_shared(:lockKey)"

WINDOW_CONNECTION_ATTRIBUTE = "onlineWindowConnection"
WINDOW_STATE_ATTRIBUTE = "onlineWindowState"
WINDOW_GRANTED = "GRANTED"
WINDOW_REFUSED = "REFUSED"

# Reported as CICS IOERR: the failure set of app.core.database.UNIT_FAILURES, kept here so this
# module stays a leaf. Catching only DBAPIError and OSError would not do: the asyncpg dialect
# raises connect-time rejections (bad credentials, unknown database, too many connections)
# unwrapped, and a pool checkout that waits past pool_timeout raises PoolTimeoutError.
WINDOW_FAILURES = (
    DBAPIError,
    OSError,
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    PoolTimeoutError,
)

UNLOCK_FAILED_LOG = "Batch-window unlock failed (%s); the connection is discarded instead"


def LockKeyStatement(sql: str) -> TextClause:
    """Bind :lockKey as BIGINT, the single-key form batch locks exclusively."""
    return text(sql).bindparams(bindparam("lockKey", type_=BigInteger))


TRY_SHARED_WINDOW_STATEMENT = LockKeyStatement(TRY_SHARED_WINDOW_SQL)
RELEASE_SHARED_WINDOW_STATEMENT = LockKeyStatement(RELEASE_SHARED_WINDOW_SQL)


class FileNotOpenError(RepositoryError):
    """CICS NOTOPEN (RESP 19, RESP2 60): the batch window has closed the file to online use."""

    def __init__(self, fileName: str) -> None:
        super().__init__(fileName, resp=CICS_RESP_NOTOPEN, resp2=CICS_RESP2_NOTOPEN_CLOSED)


def WindowState(request: Request) -> str | None:
    """Return WINDOW_GRANTED, WINDOW_REFUSED, or None while the request has not entered."""
    return getattr(request.state, WINDOW_STATE_ATTRIBUTE, None)


def StoreWindowState(
    request: Request, state: str | None, connection: AsyncConnection | None
) -> None:
    """Record the request's window state and the connection holding its shared lock."""
    setattr(request.state, WINDOW_STATE_ATTRIBUTE, state)
    setattr(request.state, WINDOW_CONNECTION_ATTRIBUTE, connection)


async def OpenWindowConnection(engine: AsyncEngine, fileName: str) -> AsyncConnection:
    """Check out the request's window connection; a connection failure becomes FileIoError."""
    try:
        return await engine.connect()
    except WINDOW_FAILURES as exc:
        raise FileIoError(fileName) from exc


async def AcquireSharedWindow(connection: AsyncConnection) -> bool:
    """Try the shared window lock without waiting, then end the transaction.

    The commit leaves the connection idle rather than idle in transaction; a session-level
    advisory lock survives it.
    """
    result = await connection.execute(TRY_SHARED_WINDOW_STATEMENT, {"lockKey": BATCH_WINDOW_LOCK})
    granted = bool(result.scalar_one())
    await connection.commit()
    return granted


async def DiscardWindowConnection(connection: AsyncConnection) -> None:
    """Close a connection that may hold the shared lock without returning it to the pool.

    Invalidating closes the driver connection, so PostgreSQL ends the session and drops its
    advisory locks; the pool's reset on return only rolls back and would keep the lock.
    """
    await connection.invalidate()
    await connection.close()


async def LockWindowOrDiscard(connection: AsyncConnection, fileName: str) -> bool:
    """Return whether the shared window lock was granted on the connection.

    Any failure, a task cancellation included, discards the connection, because the lock may
    have been granted before the failure. A database failure becomes FileIoError.
    """
    acquired = False
    try:
        granted = await AcquireSharedWindow(connection)
        acquired = True
        return granted
    except WINDOW_FAILURES as exc:
        raise FileIoError(fileName) from exc
    finally:
        if not acquired:
            await DiscardWindowConnection(connection)


async def EnterOnlineWindow(request: Request, fileName: str) -> None:
    """Gate a repository statement on a window file, as CICS gated a file CLOSEFIL closed.

    ``fileName`` is the legacy CICS file the statement addresses (TRANSACT, CCXREF, CXACAIX,
    ACCTDAT or USRSEC) and is named by the raised condition. The first call of a request takes
    the shared window lock on a connection of its own, ``request.app.state.engine``; later calls
    reuse the outcome without a query. Raises FileNotOpenError (NOTOPEN) while batch holds or
    awaits the window, on this call and every later one in the request, and FileIoError when
    the database cannot be reached; a later call then tries again.
    """
    state = WindowState(request)
    if state == WINDOW_GRANTED:
        return
    if state == WINDOW_REFUSED:
        raise FileNotOpenError(fileName)
    connection = await OpenWindowConnection(request.app.state.engine, fileName)
    if not await LockWindowOrDiscard(connection, fileName):
        await connection.close()
        StoreWindowState(request, WINDOW_REFUSED, None)
        raise FileNotOpenError(fileName)
    StoreWindowState(request, WINDOW_GRANTED, connection)


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
