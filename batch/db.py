"""Synchronous SQLAlchemy engines of the carddemo-batch CLI, its jobs and its batch window.

Replaces: the DD DISP=SHR allocations of the TRANSACT, CARDXREF, ACCTDATA, TCATBALF and
DISCGRP VSAM clusters in jcl/POSTTRAN.jcl and jcl/INTCALC.jcl, and the exclusive file access
that jcl/CLOSEFIL.jcl established by closing TRANSACT, CCXREF, ACCTDAT, CXACAIX and USRSEC in
the CICS region. Specified in the Technical Specification, Sections 0.6.10, 0.7.3 and 0.8.5.

Batch uses only synchronous psycopg2 engines over SYNC_DATABASE_URL, never the backend's
asyncpg engine or its async repositories: jobs issue SQLAlchemy Core statements against the
Base.metadata tables. Each engine serves one purpose:

- BuildEngine: the pooled engine of the job connections. Each job and batch/orchestration/*
  choose the transaction scope: engine.begin() for an atomic step, or engine.connect() with a
  commit after each write for the per-write steps POSTTRAN and INTCALC.
- BuildAutocommitEngine: the unpooled autocommit engine of the BatchWindow connection, which
  holds the session advisory lock that stands in for CLOSEFIL. Closing that connection ends
  the physical session, so PostgreSQL releases the lock.

Importing this module creates no engine and opens no connection; batch/cli.py builds the
engines of each command and disposes of them when it ends. Connection failures propagate as
sqlalchemy.exc.OperationalError for the caller to map to an exit status. Nothing here logs the
URL; a message that must show it uses url.render_as_string(hide_password=True).

Diagnostics policy: the jobs bind password hashes, SSNs, card numbers and customer data as
parameters and fetch them in rows. Both engines set hide_parameters=True, which keeps bound
values out of SQLAlchemy's error text, and call app.core.logging's
SuppressEngineStatementLogging, so SQLAlchemy never creates a statement, parameter or row log
record, nor a pool record of a connection and its libpq dsn below WARNING, whatever level the
"sqlalchemy" logger is given.
"""

from app.core.logging import SuppressEngineStatementLogging
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool

from batch.config import BatchSettings

# libpq connect_timeout: an unreachable database fails the command within this many seconds
# instead of hanging the chain.
CONNECT_TIMEOUT_SECONDS = 10

PSYCOPG2_DRIVER_NAME = "postgresql+psycopg2"
DEFAULT_POSTGRESQL_DRIVER_NAME = "postgresql"
INVALID_URL_MESSAGE = "SYNC_DATABASE_URL is not a valid database URL"
WRONG_DRIVER_MESSAGE = f"SYNC_DATABASE_URL must use the {PSYCOPG2_DRIVER_NAME} driver"


def SyncDatabaseUrl(settings: BatchSettings) -> URL:
    """Return SYNC_DATABASE_URL as a psycopg2 URL.

    A postgresql+psycopg2 URL is returned unchanged and a bare postgresql URL gets the psycopg2
    driver. Raises ValueError for an unparsable URL or any other driver, such as asyncpg. The
    messages never contain the URL, and the parse error is not chained, because the URL holds
    the database password. Both of SQLAlchemy's parse errors quote the input: ArgumentError
    the whole URL, and the ValueError of a non-numeric port the port slot, which holds the
    password when the URL lacks its "@".
    """
    try:
        databaseUrl = make_url(settings.SYNC_DATABASE_URL)
    except (ArgumentError, ValueError):
        raise ValueError(INVALID_URL_MESSAGE) from None
    if databaseUrl.drivername == PSYCOPG2_DRIVER_NAME:
        return databaseUrl
    if databaseUrl.drivername == DEFAULT_POSTGRESQL_DRIVER_NAME:
        return databaseUrl.set(drivername=PSYCOPG2_DRIVER_NAME)
    raise ValueError(WRONG_DRIVER_MESSAGE)


def BuildEngine(settings: BatchSettings) -> Engine:
    """Build the pooled engine of the job connections, without connecting.

    The isolation level stays PostgreSQL's default, READ COMMITTED, and pool_pre_ping replaces
    a connection the server has dropped. Callers own the transaction scope through
    engine.begin(), or engine.connect() with explicit commits, and dispose of the engine. The
    diagnostics policy of the module docstring applies.
    """
    SuppressEngineStatementLogging()
    return create_engine(
        SyncDatabaseUrl(settings),
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
        hide_parameters=True,
    )


def BuildAutocommitEngine(settings: BatchSettings) -> Engine:
    """Build the unpooled autocommit engine of the BatchWindow connection, without connecting.

    NullPool makes every connect() open a new physical connection and every close() end it, so
    closing the window's connection ends its session and releases the session-level advisory
    lock it holds. AUTOCOMMIT keeps that idle connection outside any transaction, separate from
    every job connection. The diagnostics policy of the module docstring applies.
    """
    SuppressEngineStatementLogging()
    return create_engine(
        SyncDatabaseUrl(settings),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
        hide_parameters=True,
    )
