"""Alembic sync migration environment of the CardDemo PostgreSQL schema.

Replaces: the IDCAMS DELETE, DEFINE CLUSTER, DEFINE AIX, BLDINDEX and REPRO provisioning of the
CardDemo VSAM datasets (jcl/ACCTFILE.jcl, CARDFILE, CUSTFILE, XREFFILE, TRANFILE, TRANTYPE,
TRANCATG, DISCGRP, TCATBALF and jcl/DUSRSECJ.jcl), whose clusters and alternate indexes are
catalogued in catlg/LISTCAT.txt and bound as CICS files in csd/CARDDEMO.CSD.

It runs revisions 0001-0009 of alembic/versions/ on the SQLAlchemy sync engine over psycopg2.
The database URL is, in order of precedence:

1. a non-empty ``sqlalchemy.url`` main option, which a test harness sets to its test database;
2. ``SYNC_DATABASE_URL`` from ``MigrationSettings``: the process environment, then backend/.env.

``MigrationSettings`` needs no ``SECRET_KEY``, so migrations run with the backend's secret
unset. Offline mode (``alembic upgrade head --sql``) needs no URL at all. A URL that does not
parse stops either mode with a ValueError naming its source, never repeating its password.
Online runs never log bound parameters or result rows (see RunMigrationsOnline), and stop
connecting after CONNECT_TIMEOUT_SECONDS unless the URL carries its own connect_timeout.

A harness may also set ``config.attributes["connection"]`` to an open ``Connection``, which is
used as given and left open, and ``config.attributes["configure_logger"] = False`` to keep its
own logging configuration. Otherwise the ini file's logger levels apply and every record is
written to stdout through app.core.logging's handler, which masks card numbers. Offline mode
writes its SQL script to stdout too, so there each line of a record starts with ``-- `` and
``alembic upgrade head --sql > migration.sql`` stays a valid SQL script.
"""

import logging
from collections.abc import Mapping
from logging.config import fileConfig

from sqlalchemy import Engine, create_engine, pool
from sqlalchemy.engine import URL, Connection

from alembic import context
from app.core.config import MigrationSettings, ParseDatabaseUrl
from app.core.logging import (
    HANDLER_NAME,
    LOG_LINE_FORMAT,
    ConfigureLogging,
    SuppressEngineStatementLogging,
)
from app.models import ALL_SCHEMAS, Base

CONFIG = context.config
TARGET_METADATA = Base.metadata
MANAGED_SCHEMAS = frozenset(ALL_SCHEMAS)
DIALECT_NAME = "postgresql"
MISSING_URL_MESSAGE = "SYNC_DATABASE_URL is not set: define it in backend/.env or the environment"
HARNESS_URL_OPTION = "sqlalchemy.url"
SYNC_URL_SETTING = "SYNC_DATABASE_URL"
# libpq connect_timeout, as in batch/db.py: an unreachable or stalled database fails the run
# within this many seconds instead of hanging it.
CONNECT_TIMEOUT_ARGUMENT = "connect_timeout"
CONNECT_TIMEOUT_SECONDS = 10


def ResolveDatabaseUrl() -> URL | None:
    """Return the harness URL override, else SYNC_DATABASE_URL, parsed; None when both are blank.

    MigrationSettings is built on every call, so environment changes made by a test harness
    are honoured. The URL carries the database password and is never logged: one that does not
    parse raises ParseDatabaseUrl's ValueError, which names the option or setting that held it
    and never repeats it, in online and offline runs alike.
    """
    harnessUrl = (CONFIG.get_main_option(HARNESS_URL_OPTION) or "").strip()
    if harnessUrl:
        return ParseDatabaseUrl(harnessUrl, HARNESS_URL_OPTION)
    settingsUrl = MigrationSettings().SYNC_DATABASE_URL.strip()
    if not settingsUrl:
        return None
    return ParseDatabaseUrl(settingsUrl, SYNC_URL_SETTING)


def RequireDatabaseUrl() -> URL:
    """Return the database URL of an online run, or raise RuntimeError when none is set."""
    databaseUrl = ResolveDatabaseUrl()
    if databaseUrl is None:
        raise RuntimeError(MISSING_URL_MESSAGE)
    return databaseUrl


def IncludeName(name: str | None, objectType: str, parentNames: Mapping[str, str | None]) -> bool:
    """Limit autogenerate reflection to the CardDemo schemas and the default schema.

    Alembic calls this ``include_name`` hook positionally with (name, type_, parent_names), so
    ``parentNames`` is part of the signature although only schema names are filtered. Without
    the filter, ``include_schemas=True`` would reflect information_schema and report its
    tables as drops.
    """
    if objectType == "schema":
        return name is None or name in MANAGED_SCHEMAS
    return True


def DetachIniRootHandlers() -> None:
    """Detach and close the root handlers that fileConfig installed from the ini file.

    fileConfig has already removed every earlier root handler, so each handler left on the
    root is the ini's own stream handler, which neither masks card numbers nor writes to stdout.
    """
    rootLogger = logging.getLogger()
    for iniHandler in list(rootLogger.handlers):
        rootLogger.removeHandler(iniHandler)
        iniHandler.close()


# An offline run writes its SQL script to stdout, the stream the masking handler writes to, so
# there each log line starts a PostgreSQL line comment and the script stays valid SQL.
SQL_COMMENT_PREFIX = "-- "


class SqlCommentFormatter(logging.Formatter):
    """Formats a record as its format string does, with every line written as a SQL comment.

    A message can span lines (an Alembic step names its revision's first docstring paragraph),
    and so does a traceback. A PostgreSQL line comment ends at a line feed or a carriage return,
    so str.splitlines breaks at both and each resulting line gets SQL_COMMENT_PREFIX.
    """

    # logging calls this exact name, so it stays lower-case: a framework-mandated
    # exception to the MK_Modernization Rule's PascalCase method naming.
    def format(self, record: logging.LogRecord) -> str:
        formattedRecord = super().format(record)
        return "\n".join(SQL_COMMENT_PREFIX + line for line in formattedRecord.splitlines())


def CommentOfflineLogLines() -> None:
    """Make the masking stdout handler write LOG_LINE_FORMAT lines as SQL comments.

    Only the handler's formatter is replaced: its PanMaskingFilter stays, so each record is
    masked before it is formatted.
    """
    for rootHandler in logging.getLogger().handlers:
        if rootHandler.name == HANDLER_NAME:
            rootHandler.setFormatter(SqlCommentFormatter(LOG_LINE_FORMAT))


def ConfigureLoggingFromIni() -> None:
    """Apply the ini file's logger levels, then log to stdout through the shared masking handler.

    Nothing is configured when no ini file exists or the harness set ``configure_logger`` to
    False. Otherwise fileConfig sets the alembic and sqlalchemy.engine levels, its root handlers
    are detached, and app.core.logging.ConfigureLogging installs the masking stdout handler with
    the root at INFO. Migration and seed records are then written once each, to stdout, with
    card numbers masked. Offline mode writes its SQL script to stdout as well, so there every
    line of a record is prefixed with ``-- `` (CommentOfflineLogLines) and is a SQL comment.
    """
    if CONFIG.config_file_name is None:
        return
    if CONFIG.attributes.get("configure_logger", True) is False:
        return
    fileConfig(CONFIG.config_file_name, disable_existing_loggers=False)
    DetachIniRootHandlers()
    ConfigureLogging()
    if context.is_offline_mode():
        CommentOfflineLogLines()


def ConfigureContext(connection: Connection) -> None:
    """Bind the migration context to ``connection`` and the CardDemo metadata.

    The version table stays public.alembic_version, and the whole upgrade runs in a single
    transaction, so PostgreSQL rolls back a failed ``upgrade head`` completely.
    """
    context.configure(
        connection=connection,
        target_metadata=TARGET_METADATA,
        include_schemas=True,
        include_name=IncludeName,
        compare_type=True,
    )


def RunMigrationsOnConnection(connection: Connection) -> None:
    """Run the pending revisions on ``connection`` inside the migration transaction."""
    ConfigureContext(connection)
    with context.begin_transaction():
        context.run_migrations()


def OfflineTarget() -> dict[str, str | URL]:
    """Return the configure() option naming the offline target: the URL, else the dialect.

    Offline SQL generation never connects, so with no URL set the PostgreSQL dialect alone is
    enough and no credentials are needed.
    """
    databaseUrl = ResolveDatabaseUrl()
    if databaseUrl is None:
        return {"dialect_name": DIALECT_NAME}
    return {"url": databaseUrl}


def RunMigrationsOffline() -> None:
    """Emit the migration SQL without a database connection (``alembic upgrade --sql``).

    Revisions 0002 and 0008 detect offline mode themselves and skip their data steps.
    """
    context.configure(
        target_metadata=TARGET_METADATA,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        **OfflineTarget(),
    )
    with context.begin_transaction():
        context.run_migrations()


def MigrationConnectArgs(databaseUrl: URL) -> dict[str, int]:
    """Return the psycopg2 connect arguments: CONNECT_TIMEOUT_SECONDS, unless the URL sets one.

    connect_args override the URL's query, so a connect_timeout written in the URL itself is
    honoured by passing none here.
    """
    if CONNECT_TIMEOUT_ARGUMENT in databaseUrl.query:
        return {}
    return {CONNECT_TIMEOUT_ARGUMENT: CONNECT_TIMEOUT_SECONDS}


def BuildMigrationEngine(databaseUrl: URL) -> Engine:
    """Build the unpooled psycopg2 engine of an online run, without connecting.

    hide_parameters=True keeps bound values, such as the bcrypt hashes and the customer and
    card data that revision 0002 seeds, out of SQLAlchemy's error text. The connect timeout
    of MigrationConnectArgs fails a run against an unreachable database instead of hanging it.
    """
    return create_engine(
        databaseUrl,
        poolclass=pool.NullPool,
        hide_parameters=True,
        connect_args=MigrationConnectArgs(databaseUrl),
    )


def RunMigrationsOnline() -> None:
    """Run the migrations on the harness connection, or on a new unpooled sync engine.

    SuppressEngineStatementLogging runs first, after the ini file's logging configuration
    has reset the engine logger, so no statement, parameter or row log record is created on
    either connection. A harness connection is left open for its owner. The engine built from
    the resolved URL holds no pooled connection after the run and is disposed of.
    """
    SuppressEngineStatementLogging()
    harnessConnection = CONFIG.attributes.get("connection")
    if isinstance(harnessConnection, Connection):
        RunMigrationsOnConnection(harnessConnection)
        return
    engine = BuildMigrationEngine(RequireDatabaseUrl())
    try:
        with engine.connect() as connection:
            RunMigrationsOnConnection(connection)
    finally:
        engine.dispose()


ConfigureLoggingFromIni()
if context.is_offline_mode():
    RunMigrationsOffline()
else:
    RunMigrationsOnline()
