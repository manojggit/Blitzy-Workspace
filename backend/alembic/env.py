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
unset. Offline mode (``alembic upgrade head --sql``) needs no URL at all.

A harness may also set ``config.attributes["connection"]`` to an open ``Connection``, which is
used as given and left open, and ``config.attributes["configure_logger"] = False`` to keep its
own logging configuration.
"""

from collections.abc import Mapping
from logging.config import fileConfig

from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

from alembic import context
from app.core.config import MigrationSettings
from app.models import ALL_SCHEMAS, Base

CONFIG = context.config
TARGET_METADATA = Base.metadata
MANAGED_SCHEMAS = frozenset(ALL_SCHEMAS)
DIALECT_NAME = "postgresql"
MISSING_URL_MESSAGE = "SYNC_DATABASE_URL is not set: define it in backend/.env or the environment"


def ResolveDatabaseUrl() -> str | None:
    """Return the harness URL override, else SYNC_DATABASE_URL, else None when both are blank.

    MigrationSettings is built on every call, so environment changes made by a test harness
    are honoured. The URL carries the database password and is never logged.
    """
    harnessUrl = (CONFIG.get_main_option("sqlalchemy.url") or "").strip()
    if harnessUrl:
        return harnessUrl
    settingsUrl = MigrationSettings().SYNC_DATABASE_URL.strip()
    return settingsUrl or None


def RequireDatabaseUrl() -> str:
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


def ConfigureLoggingFromIni() -> None:
    """Apply the ini file's logging sections unless the harness disabled it or no file exists."""
    if CONFIG.config_file_name is None:
        return
    if CONFIG.attributes.get("configure_logger", True) is False:
        return
    fileConfig(CONFIG.config_file_name, disable_existing_loggers=False)


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


def OfflineTarget() -> dict[str, str]:
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


def RunMigrationsOnline() -> None:
    """Run the migrations on the harness connection, or on a new unpooled sync engine.

    A harness connection is left open for its owner. The engine built from the resolved URL
    holds no pooled connection after the run and is disposed of.
    """
    harnessConnection = CONFIG.attributes.get("connection")
    if isinstance(harnessConnection, Connection):
        RunMigrationsOnConnection(harnessConnection)
        return
    engine = create_engine(RequireDatabaseUrl(), poolclass=pool.NullPool)
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
