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
"""

import logging
from types import TracebackType
from typing import Self

from app.core.locks import BATCH_WINDOW_LOCK
from sqlalchemy import BigInteger, bindparam, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError, OperationalError

from batch.config import BatchSettings
from batch.db import BuildAutocommitEngine

LOGGER = logging.getLogger(__name__)

# A module constant rather than a setting. It is read each time a window is entered, so
# batch/tests/test_batch_window.py can shorten it by monkeypatching this name.
BATCH_WINDOW_LOCK_TIMEOUT = "60s"
BATCH_WINDOW_UNAVAILABLE_MESSAGE = "BATCH WINDOW NOT AVAILABLE"
# PostgreSQL lock_not_available, raised when lock_timeout expires before the lock is granted.
LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"

WINDOW_CLOSED_LOG = "BATCH WINDOW CLOSED TO ONLINE (CLOSEFIL)"
WINDOW_OPENED_LOG = "BATCH WINDOW OPEN TO ONLINE (OPENFIL)"
UNLOCK_FAILED_LOG = "Batch-window unlock failed (%s); closing the connection releases the lock"

# is_local false: the autocommit connection has no transaction for a local setting to live in.
SET_LOCK_TIMEOUT_STATEMENT = text("SELECT set_config('lock_timeout', :timeout, false)")
ACQUIRE_WINDOW_STATEMENT = text("SELECT pg_advisory_lock(:lockKey)").bindparams(
    bindparam("lockKey", type_=BigInteger)
)
RELEASE_WINDOW_STATEMENT = text("SELECT pg_advisory_unlock(:lockKey)").bindparams(
    bindparam("lockKey", type_=BigInteger)
)


class BatchWindowUnavailableError(Exception):
    """The batch window was not granted within the lock timeout; the command changed nothing."""

    def __init__(self) -> None:
        super().__init__(BATCH_WINDOW_UNAVAILABLE_MESSAGE)


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
    constant as it stands when the window is entered. Entering raises
    BatchWindowUnavailableError when the lock is not granted in time, and lets any other
    database failure propagate; either way the connection is already closed. Run no statement
    on ``connection``: jobs use their own engine.
    """

    def __init__(self, settings: BatchSettings, lockTimeout: str | None = None) -> None:
        self.settings = settings
        self.lockTimeout = lockTimeout
        self.engine: Engine | None = None
        self.connection: Connection | None = None

    def __enter__(self) -> Self:
        """Close the window to online access: connect, bound the wait, take the lock."""
        self.engine = BuildAutocommitEngine(self.settings)
        windowClosed = False
        try:
            self.connection = self.engine.connect()
            SetLockTimeout(self.connection, self.lockTimeout or BATCH_WINDOW_LOCK_TIMEOUT)
            AcquireWindowLock(self.connection)
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

        An unlock that fails on a broken connection is logged and the connection is still
        closed: PostgreSQL releases a session advisory lock when its connection drops.
        """
        connection = self.connection
        if connection is None:
            return
        try:
            ReleaseWindowLock(connection)
        except DBAPIError as error:
            LOGGER.warning(UNLOCK_FAILED_LOG, type(error).__name__)
        finally:
            self.Detach()
            LOGGER.info(WINDOW_OPENED_LOG)

    def Detach(self) -> None:
        """Close the window connection and dispose of the engine, leaving neither attached."""
        connection, engine = self.connection, self.engine
        self.connection = None
        self.engine = None
        CloseQuietly(connection, engine)
