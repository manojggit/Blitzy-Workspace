"""Replaces: CLOSEFIL, OPENFIL (CEMT SET FIL(TRANSACT|CCXREF|ACCTDAT|CXACAIX|USRSEC) CLO/OPE).

The batch window that every carddemo-batch command runs inside. Control-M bracketed each batch
folder with CLOSEFIL -> job -> WAITSTEP -> OPENFIL (scheduler/CardDemo.controlm: DAILY TRANBKP,
WEEKLY DISCGRP, MONTHLY INTCALC -> COMBTRAN), and CA-7 ran CLOSEFIL before POSTTRAN and CREASTMT
(scheduler/CardDemo.ca7). While the files were closed, online programs got NOTOPEN (RESP 19,
RESP2 60). Specified in the Technical Specification, Section 0.6.10.

BatchWindow opens one dedicated connection from batch.db.BuildAutocommitEngine, separate from
every job connection, and takes pg_advisory_lock(BATCH_WINDOW_LOCK), session-level and
exclusive. It holds that connection idle until the command ends, then unlocks and closes it.
The online repositories take the same key shared without waiting (app.core.locks): the
exclusive request waits for in-flight online holders, as a CEMT close waits for current users,
and while it waits or holds, every new shared request is refused, so online programs take their
WHEN OTHER branch. The connection is unpooled, so
closing it ends the session, and a process that dies drops the lock with its connection: a
crashed run never leaves the window closed.

The wait is bounded by lock_timeout, BATCH_WINDOW_LOCK_TIMEOUT unless the caller passes another
value. When it expires, because another batch command holds the window or an online request does
not finish, BatchWindowUnavailableError is raised and nothing has changed; batch/cli.py prints
BATCH_WINDOW_UNAVAILABLE_MESSAGE and exits 12. Only the lock key is imported from
app.core.locks: batch never enters the online side of the window, because its job connections
would conflict with its own exclusive lock. Alembic migrations do not take the window.

Ownership is checked, never assumed. The guard session that took the lock is identified by its
pid and backend_start, and the command's job engine is fenced (WindowFence): its connections
are closed, and every session it opens afterwards takes BATCH_JOB_SESSION_LOCK shared and is
admitted only while the guard still holds the window. Every job transaction checks the guard
again just before its COMMIT, and RequireWindow does before each file effect no commit covers:
a generation's rename, the statement publication and the deletion of managed files. Should the
guard session end alone, by a network failure or pg_terminate_backend, PostgreSQL drops the
window while the job sessions live on; the next check then rolls its transaction back and
raises BatchWindowLostError. The loss is final: no later check queries again and the window is
never taken anew. batch/cli.py prints BATCH_WINDOW_LOST_MESSAGE and exits 12, and the run left
RUNNING is classified INTERRUPTED by the next command.

A command entering the window takes BATCH_JOB_SESSION_LOCK exclusively once, on its guard
connection and within the same lock timeout, so it waits until the job sessions of a former
owner have ended before it classifies runs or cleans files: recovery never takes live work for
a dead process's. A former owner still running past the timeout makes the entrant raise
BatchWindowUnavailableError, having changed nothing. On exit the job connections are closed
before the unlock, so the next command never waits for a finished one.
"""

import logging
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Self

import psycopg2
from app.core.locks import BATCH_WINDOW_LOCK
from sqlalchemy import BigInteger, bindparam, event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.pool import ConnectionPoolEntry

from batch.config import BatchSettings
from batch.db import BuildAutocommitEngine

LOGGER = logging.getLogger(__name__)

# A module constant rather than a setting. It is read each time a window is entered, so
# batch/tests/test_batch_window.py can shorten it by monkeypatching this name.
BATCH_WINDOW_LOCK_TIMEOUT = "60s"
BATCH_WINDOW_UNAVAILABLE_MESSAGE = "BATCH WINDOW NOT AVAILABLE"
BATCH_WINDOW_LOST_MESSAGE = "BATCH WINDOW LOST"
# PostgreSQL lock_not_available, raised when lock_timeout expires before the lock is granted.
LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"
# A batch-only key, spelling the ASCII bytes "CDMOJOBS": every job session of a held window
# holds it shared for its life, and a command entering the window takes it exclusively once.
BATCH_JOB_SESSION_LOCK = 0x43444D4F4A4F4253
# The connection-record info key under which a fenced job connection keeps its WindowFence.
WINDOW_FENCE_INFO_KEY = "carddemoBatchWindowFence"

WINDOW_CLOSED_LOG = "BATCH WINDOW CLOSED TO ONLINE (CLOSEFIL)"
WINDOW_OPENED_LOG = "BATCH WINDOW OPEN TO ONLINE (OPENFIL)"
UNLOCK_FAILED_LOG = "Batch-window unlock failed (%s); closing the connection releases the lock"

# is_local FALSE: the autocommit connection has no transaction for a local setting to live in.
SET_LOCK_TIMEOUT_STATEMENT = text("SELECT set_config('lock_timeout', :timeout, FALSE)")
ACQUIRE_WINDOW_STATEMENT = text("SELECT pg_advisory_lock(:lockKey)").bindparams(
    bindparam("lockKey", type_=BigInteger)
)
RELEASE_WINDOW_STATEMENT = text("SELECT pg_advisory_unlock(:lockKey)").bindparams(
    bindparam("lockKey", type_=BigInteger)
)
# pg_stat_get_activity(pid) reads that one backend; backend_start tells a reused pid apart.
GUARD_TOKEN_STATEMENT = text(
    "SELECT pid, backend_start FROM pg_stat_get_activity(pg_backend_pid())"
)
# In psycopg2's paramstyle: they run on the raw connection of a new job session, and through
# Connection.exec_driver_sql. pg_locks shows a single-bigint key as classid (its high 32 bits)
# and objid (its low 32 bits) with objsubid 1, and lists the locks of every database.
JOIN_JOB_SESSIONS_SQL = "SELECT pg_try_advisory_lock_shared(%(lockKey)s::bigint)"
GUARD_HOLDS_WINDOW_SQL = (
    "SELECT EXISTS (SELECT 1 FROM pg_locks AS held, pg_stat_get_activity(%(guardPid)s) AS guard"
    " WHERE held.locktype = 'advisory' AND held.objsubid = 1 AND held.granted"
    " AND held.mode = 'ExclusiveLock' AND held.pid = %(guardPid)s"
    " AND held.database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    " AND ((held.classid::bigint << 32) | held.objid::bigint) = %(lockKey)s::bigint"
    " AND guard.backend_start IS NOT DISTINCT FROM %(guardStart)s)"
)


class BatchWindowUnavailableError(Exception):
    """The batch window was not granted within the lock timeout; the command changed nothing."""

    def __init__(self) -> None:
        super().__init__(BATCH_WINDOW_UNAVAILABLE_MESSAGE)


class BatchWindowLostError(Exception):
    """The guard session ended while the command ran; nothing after it commits or publishes.

    Deliberately no DBAPIError, OSError or JobFailure, so no runner or chain mapping turns it
    into a step outcome: it reaches batch/cli.py, and the run stays RUNNING.
    """

    def __init__(self) -> None:
        super().__init__(BATCH_WINDOW_LOST_MESSAGE)


def AdmitJobSession(dbapiConnection: DBAPIConnection, guardParameters: dict[str, object]) -> bool:
    """Join the job sessions on a new raw connection; return whether the guard holds the window.

    BATCH_JOB_SESSION_LOCK is taken shared first, so a command entering the window after the
    guard check waits for this session. It is refused while such a command holds or awaits the
    key exclusively, which it does only once it holds the window. The checks' transaction is
    committed, leaving the connection idle; the session lock outlives it.
    """
    with closing(dbapiConnection.cursor()) as cursor:
        cursor.execute(JOIN_JOB_SESSIONS_SQL, {"lockKey": BATCH_JOB_SESSION_LOCK})
        admitted = bool(cursor.fetchone()[0])
        if admitted:
            cursor.execute(GUARD_HOLDS_WINDOW_SQL, guardParameters)
            admitted = bool(cursor.fetchone()[0])
    dbapiConnection.commit()
    return admitted


def RollBackOrInvalidate(connection: Connection) -> None:
    try:
        connection.connection.dbapi_connection.rollback()
    except psycopg2.Error:
        connection.invalidate()


@dataclass
class WindowFence:
    """The guard session holding one batch window, and whether that window has been lost.

    Once a check finds the window lost, lost stays True: no later check queries again, and the
    window is never taken anew.
    """

    guardPid: int
    guardStart: datetime | None
    lost: bool = False

    def GuardParameters(self) -> dict[str, object]:
        return {
            "guardPid": self.guardPid,
            "guardStart": self.guardStart,
            "lockKey": BATCH_WINDOW_LOCK,
        }

    def Verify(self, connection: Connection) -> None:
        """Raise BatchWindowLostError unless the guard still holds the window, checked on it."""
        if not self.lost:
            result = connection.exec_driver_sql(GUARD_HOLDS_WINDOW_SQL, self.GuardParameters())
            self.lost = not result.scalar()
        if self.lost:
            raise BatchWindowLostError()

    def OnCommit(self, connection: Connection) -> None:
        """The job engine's commit listener: verify, inside the transaction, before its COMMIT.

        SQLAlchemy has already marked the transaction ended, so neither its context nor the
        pool's reset on return would roll it back. A refusal therefore rolls it back on the
        driver connection, keeping the session and its BATCH_JOB_SESSION_LOCK until the window
        exits, so a command entering after the loss waits until this one has unwound. A database
        error, from the check or that rollback, invalidates the connection, ending the session.
        """
        try:
            self.Verify(connection)
        except BatchWindowLostError:
            RollBackOrInvalidate(connection)
            raise
        except DBAPIError:
            connection.invalidate()
            raise

    def OnConnect(
        self, dbapiConnection: DBAPIConnection, connectionRecord: ConnectionPoolEntry
    ) -> None:
        """The job pool's connect listener: admit a new session only while the window holds.

        A refused admission marks the window lost. A refused or failed one closes the new
        connection, which drops whatever lock it took; an admitted one keeps the fence in its
        record's info, where RequireWindow finds it.
        """
        admitted = False
        try:
            admitted = not self.lost and AdmitJobSession(dbapiConnection, self.GuardParameters())
        finally:
            if not admitted:
                dbapiConnection.close()
        if not admitted:
            self.lost = True
            raise BatchWindowLostError()
        connectionRecord.info[WINDOW_FENCE_INFO_KEY] = self

    def Listeners(self) -> tuple[tuple[str, Callable[..., None]], ...]:
        return (("connect", self.OnConnect), ("commit", self.OnCommit))


def RequireWindow(connection: Connection) -> None:
    """Raise BatchWindowLostError unless the batch window of the connection's command holds.

    Call it on a job connection just before a file effect that no commit fences: a generation's
    rename, the statement publication, or the deletion of a managed file. A connection without
    a fence, as a runner called outside any window has, passes unchecked.
    """
    fence = connection.info.get(WINDOW_FENCE_INFO_KEY)
    if fence is not None:
        fence.Verify(connection)


def SetLockTimeout(connection: Connection, timeout: str) -> None:
    """Bound every later lock wait of the connection's session, for example to '60s'."""
    connection.scalar(SET_LOCK_TIMEOUT_STATEMENT, {"timeout": timeout})


def AcquireWindowLock(connection: Connection) -> None:
    """Take the exclusive session lock on BATCH_WINDOW_LOCK, waiting up to the lock timeout.

    Raises sqlalchemy.exc.OperationalError with SQLSTATE LOCK_NOT_AVAILABLE_SQLSTATE when the
    timeout expires first.
    """
    connection.scalar(ACQUIRE_WINDOW_STATEMENT, {"lockKey": BATCH_WINDOW_LOCK})


def ReleaseWindowLock(connection: Connection) -> None:
    """Release the exclusive session lock on BATCH_WINDOW_LOCK held by the connection."""
    connection.scalar(RELEASE_WINDOW_STATEMENT, {"lockKey": BATCH_WINDOW_LOCK})


def AwaitFormerJobSessions(connection: Connection) -> None:
    """Wait, up to the lock timeout, until no job session of a former window owner is left.

    BATCH_JOB_SESSION_LOCK is taken exclusively, which waits for every session holding it
    shared, and released at once. Raises sqlalchemy.exc.OperationalError with SQLSTATE
    LOCK_NOT_AVAILABLE_SQLSTATE when the timeout expires first.
    """
    connection.scalar(ACQUIRE_WINDOW_STATEMENT, {"lockKey": BATCH_JOB_SESSION_LOCK})
    connection.scalar(RELEASE_WINDOW_STATEMENT, {"lockKey": BATCH_JOB_SESSION_LOCK})


def ReadGuardFence(connection: Connection) -> WindowFence:
    """Return the fence of the window the connection's session has just taken."""
    guard = connection.execute(GUARD_TOKEN_STATEMENT).one()
    return WindowFence(guardPid=guard.pid, guardStart=guard.backend_start)


def CloseQuietly(connection: Connection | None, engine: Engine | None) -> None:
    """Close the window connection, ending its session, and dispose of its engine.

    The engine is unpooled, so closing the connection closes the physical connection and
    PostgreSQL drops any advisory lock the session still holds. SQLAlchemy logs, rather than
    raises, a failure to reset or close a broken connection, so cleanup never replaces the
    command's own outcome. Either argument may be None when a window failed part-way.
    """
    try:
        if connection is not None:
            connection.close()
    finally:
        if engine is not None:
            engine.dispose()


def IsLockNotAvailable(error: OperationalError) -> bool:
    """Return whether the database error is PostgreSQL's lock_not_available (55P03)."""
    return getattr(error.orig, "pgcode", None) == LOCK_NOT_AVAILABLE_SQLSTATE


class BatchWindow:
    """Context manager holding the batch window, closed to online access, for one command.

    ``lockTimeout`` overrides BATCH_WINDOW_LOCK_TIMEOUT for this window; None uses the module
    constant as it stands when the window is entered. ``jobEngine``, the command's pooled job
    engine, is fenced while the window is held; None leaves every engine unfenced. Entering
    raises BatchWindowUnavailableError when the lock, or the end of a former owner's job
    sessions, does not come in time, and lets any other database failure propagate; either way
    the connection is already closed. Run no statement on ``connection``: jobs use their own
    engine.
    """

    def __init__(
        self,
        settings: BatchSettings,
        lockTimeout: str | None = None,
        jobEngine: Engine | None = None,
    ) -> None:
        self.settings = settings
        self.lockTimeout = lockTimeout
        self.jobEngine = jobEngine
        self.engine: Engine | None = None
        self.connection: Connection | None = None
        self.fence: WindowFence | None = None

    def __enter__(self) -> Self:
        """Close the window to online access: take the lock, await former job sessions, fence."""
        self.engine = BuildAutocommitEngine(self.settings)
        windowClosed = False
        try:
            self.connection = self.engine.connect()
            SetLockTimeout(self.connection, self.lockTimeout or BATCH_WINDOW_LOCK_TIMEOUT)
            AcquireWindowLock(self.connection)
            AwaitFormerJobSessions(self.connection)
            self.FenceJobEngine(ReadGuardFence(self.connection))
            windowClosed = True
        except OperationalError as error:
            if IsLockNotAvailable(error):
                raise BatchWindowUnavailableError() from error
            raise
        finally:
            # Any failure, an interrupt included, ends the session, which drops a lock that
            # may have been granted just before the failure.
            if not windowClosed:
                self.Detach()
        LOGGER.info(WINDOW_CLOSED_LOG)
        return self

    def __exit__(
        self,
        excType: type[BaseException] | None,
        excValue: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Reopen the window to online access; the command's own exception is never suppressed.

        The job engine is unfenced and its connections closed first. An unlock that fails on a
        broken connection, a lost window's included, is logged and the connection is still
        closed: PostgreSQL releases a session advisory lock when its connection drops.
        """
        connection = self.connection
        if connection is None:
            return
        try:
            self.UnfenceJobEngine()
            ReleaseWindowLock(connection)
        except DBAPIError as error:
            LOGGER.warning(UNLOCK_FAILED_LOG, type(error).__name__)
        finally:
            self.Detach()
            LOGGER.info(WINDOW_OPENED_LOG)

    def FenceJobEngine(self, fence: WindowFence) -> None:
        """Close the job engine's connections, then fence every one it opens and each commit."""
        self.fence = fence
        if self.jobEngine is None:
            return
        self.jobEngine.dispose()
        for identifier, listener in fence.Listeners():
            event.listen(self.jobEngine, identifier, listener)

    def UnfenceJobEngine(self) -> None:
        """Remove the fence's listeners and close the job connections, ending their sessions.

        A later call, or one for a window without a job engine, does nothing.
        """
        fence, self.fence = self.fence, None
        if fence is None or self.jobEngine is None:
            return
        for identifier, listener in fence.Listeners():
            if event.contains(self.jobEngine, identifier, listener):
                event.remove(self.jobEngine, identifier, listener)
        self.jobEngine.dispose()

    def Detach(self) -> None:
        """Unfence the job engine, close the window connection and dispose of its engine."""
        self.UnfenceJobEngine()
        connection, engine = self.connection, self.engine
        self.connection = None
        self.engine = None
        CloseQuietly(connection, engine)
