"""Shared fixtures of the backend test suites: test database, migrations, isolation and clients.

Replaces: the CICS region and VSAM files the CardDemo online programs ran against (the CARDDEMO
group of csd/CARDDEMO.CSD), the USRSEC sign-on records of copybook CSUSR01Y and image
data/EBCDIC/AWS.M2.CARDDEMO.USRSEC.PS, and the COSGN00C sign-on of map COSGN00, for the suites
under backend/tests (unit/, integration/, contract/ and test_code_standards.py).

Environment. Importing this module, before any test module, sets these process variables, so
every Settings object and Alembic run in the session sees them:

- APP_ENV=test, so revision 0002 seeds the sample data and the session cookie is not Secure;
- SECRET_KEY, generated with secrets.token_urlsafe (never a committed value);
- BCRYPT_ROUNDS=4, so hashing in the seed and in createUser stays fast;
- BACKEND_CORS_ORIGINS=http://127.0.0.1:3000, the Origin every test client sends;
- DATABASE_URL and SYNC_DATABASE_URL, set to the test database: TEST_DATABASE_URL and
  TEST_SYNC_DATABASE_URL when set, otherwise the backend URLs with the database renamed to
  carddemo_test (process environment first, then backend/.env).

Missing URLs never stop the import, so database-free unit tests run without backend/.env; the
database fixtures then fail with an explicit message. A test asserting a configuration default
must set or delete its variable with monkeypatch.setenv or monkeypatch.delenv. Tests receive
everything here as fixtures and must not import from this module.

Fixtures (pytest name, scope):

- databaseUrls (session): the resolved test database URLs.
- syncEngine (session): the psycopg2 engine. Before creating it, refuses URLs whose query sets
  a key that redirects the driver (dbname, database, host, hostaddr, port, service, dsn), URLs
  that omit the host or the port (the drivers would read PGHOST, PGPORT or a libpq service
  file), URLs reaching two hosts, ports or databases, and a database whose name lacks ``_test``.
- verifiedDatabaseIdentity (session): the database, server address, port and start time that
  both psycopg2 (read first) and asyncpg reach; fails when they differ, and fails with the
  actionable unreachable message when either cannot connect.
- databaseGuard (session): the guard functions and value types, for the guard's own tests.
- migratedDatabase (session): depends on verifiedDatabaseIdentity, so the comparison runs
  first; then drops the CardDemo schemas, runs ``alembic upgrade head`` and returns the
  snapshot of every table taken right after it.
- seedUsers (session): the ten USRSEC image records, decoded independently of app.seed.
- testSettings: a fresh app Settings; a test module may override it.
- asyncEngine: an unpooled asyncpg engine of the test database.
- dbSession: an AsyncSession inside an outer transaction rolled back at teardown.
- restoreSeedState: depends on migratedDatabase; restores its snapshot at teardown.
- testApp: CreateApp(testSettings) inside its lifespan.
- apiClient: an anonymous httpx client of testApp.
- createUser: ``createUser(userType)`` inserts a generated sign-on user and returns it.
- loginAs: ``await loginAs(userType)`` signs a new user on and returns its client and user.
- adminClient, regularClient: clients signed on as user type A and U.
- makeOnlineContext: ``await makeOnlineContext(userType)`` returns an OnlineContext.

Isolation has two layers, because online writes commit in their own write units and the
batch-window lock holds a connection of its own (app.core.database.WriteUnit,
app.core.locks.EnterOnlineWindow), so no single rolled-back connection can isolate a test that
goes through the app:

- Rollback: dbSession, for repository and direct-SQL tests that never call the app.
- Explicit cleanup: restoreSeedState truncates every table and reinserts the snapshot.
  asyncEngine, dbSession, testApp, apiClient, createUser, loginAs, adminClient, regularClient
  and makeOnlineContext depend on it, so every write a test commits is undone, and every
  client, context, session and engine is closed before the restore starts.

Setup order: syncEngine (URL checks), then verifiedDatabaseIdentity (driver comparison), then
migratedDatabase (reset and migration), then restoreSeedState and the fixtures built on it. So
nothing is dropped, migrated or written before both drivers are known to reach one test
database. The reset's DROP and the restore's TRUNCATE each also run only after their own
transaction has read the validated test database as current_database().

The batch suite (batch/tests/conftest.py) uses the same carddemo_test database. The two suites
run one after the other, never at the same time.
"""

import asyncio
import contextlib
import functools
import logging
import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
from alembic.config import Config
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import (
    Column,
    Connection,
    Engine,
    Table,
    TextClause,
    create_engine,
    func,
    insert,
    select,
    text,
)
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.core.paths import ENV_FILE, REPO_ROOT

if TYPE_CHECKING:
    from fastapi import FastAPI

    from app.core.config import Settings
    from app.core.dependencies import OnlineContext

TEST_DATABASE_NAME = "carddemo_test"
TEST_DATABASE_SUFFIX = "_test"
# Connection keywords of libpq (psycopg2) and asyncpg that name a server or a database. SQLAlchemy
# passes a URL's query to the driver after the URL's own host, port and database, so any of these
# in the query would win over them; compared lower-cased.
CONNECTION_TARGET_QUERY_KEYS = frozenset(
    {"dbname", "database", "host", "hostaddr", "port", "service", "dsn"}
)
ALLOWED_ORIGIN = "http://127.0.0.1:3000"
API_BASE_URL = "http://127.0.0.1:8000"
LOGIN_PATH = "/api/v1/auth/login"
HTTP_OK = 200

TEST_APP_ENV = "test"
SECRET_KEY_BYTES = 48
TEST_BCRYPT_ROUNDS = "4"
RESTORE_LOCK_TIMEOUT = "10s"
RESET_LOCK_TIMEOUT = "30s"

ALEMBIC_INI_PATH = REPO_ROOT / "backend" / "alembic.ini"
ALEMBIC_HEAD = "head"
ALEMBIC_URL_OPTION = "sqlalchemy.url"
# env.py's harness switch: keep pytest's logging instead of applying alembic.ini's.
ALEMBIC_LOGGING_ATTRIBUTE = "configure_logger"
USRSEC_IMAGE_PATH = REPO_ROOT / "data" / "EBCDIC" / "AWS.M2.CARDDEMO.USRSEC.PS"

USRSEC_RECORD_LENGTH = 80
USRSEC_ID_SLICE = slice(0, 8)
USRSEC_FIRST_NAME_SLICE = slice(8, 28)
USRSEC_LAST_NAME_SLICE = slice(28, 48)
USRSEC_PASSWORD_SLICE = slice(48, 56)
USRSEC_TYPE_SLICE = slice(56, 57)
EBCDIC_CODEC = "cp037"
FIELD_PADDING = " "

# COSGN00C upper-cases USERID and PASSWD (cbl/COSGN00C.cbl lines 132-136), and map COSGN00 keys
# at most 8 characters of each, so generated IDs (7 characters) and passwords (8) are
# upper-case and cp037-printable: they sign on exactly as created.
TEST_USER_ID_PREFIX = "T"
USER_ID_HEX_BYTES = 3
CREDENTIAL_TOKEN_BYTES = 6
TEST_USER_FIRST_NAME = "Pytest"
TEST_USER_LAST_NAME_PREFIX = "Type "
ADMIN_USER_TYPE = "A"
REGULAR_USER_TYPE = "U"

LOCK_TIMEOUT_SETTING = "lock_timeout"
DROP_VERSION_TABLE_SQL = "DROP TABLE IF EXISTS public.alembic_version"
DROP_SORT_KEY_FUNCTION_SQL = "DROP FUNCTION IF EXISTS public.ebcdic_sort_key(text)"
DEFER_CONSTRAINTS_SQL = "SET CONSTRAINTS ALL DEFERRED"

MISSING_URLS_MESSAGE = (
    "The test database URLs are not set or not valid. Copy backend/.env.example to backend/.env"
    " and set DATABASE_URL and SYNC_DATABASE_URL, or TEST_DATABASE_URL and"
    " TEST_SYNC_DATABASE_URL."
)
INVALID_URL_MESSAGE = "A test database URL does not parse or names no database; check backend/.env."
MISMATCHED_DATABASES_MESSAGE = (
    "The async and sync test database URLs name different databases; they must name one."
)
TARGET_OVERRIDE_MESSAGE = (
    "Refusing a test database URL whose query sets {keyNames}: the driver would connect to the"
    " server or database they name instead of the URL's own. Remove them from the URL."
)
MISSING_ENDPOINT_MESSAGE = (
    "Refusing the {driverName} test database URL: it names no {missingParts}, which the driver"
    " would then take from PGHOST, PGPORT or a libpq service file. Name both the host and the"
    " port in the URL, as DB_HOST:DB_PORT in backend/.env.example."
)
MISMATCHED_TARGETS_MESSAGE = (
    "The async and sync test database URLs reach different servers, {asyncTarget} and"
    " {syncTarget}; they must name one host, port and database."
)
NOT_TEST_DATABASE_MESSAGE = (
    "Refusing to reset database {databaseName!r}: a test database name must end with {suffix!r}."
)
WRONG_CONNECTED_DATABASE_MESSAGE = (
    "Refusing to use database {connectedName!r}: the connection must reach the validated test"
    " database {expectedName!r}, whose name ends with {suffix!r}."
)
MISMATCHED_SERVERS_MESSAGE = (
    "The asyncpg and psycopg2 test connections reached different databases or servers:"
    " {asyncIdentity} and {syncIdentity}."
)
UNREACHABLE_DATABASE_MESSAGE = (
    "Cannot prepare the test database {databaseName!r}: {reason}. Start PostgreSQL with"
    " 'docker compose up -d db'; docker/postgres/init-test-db.sql creates carddemo_test."
)
USRSEC_SIZE_MESSAGE = "{path} holds {size} bytes, not a multiple of the 80-byte USRSEC record."

DATABASE_IDENTITY_QUERY = select(
    func.current_database(),
    func.host(func.inet_server_addr()),
    func.inet_server_port(),
    func.pg_postmaster_start_time(),
)


@dataclass(frozen=True)
class DatabaseUrls:
    """The asyncpg and psycopg2 URLs of the test database; repr hides their passwords."""

    asyncUrl: str = field(repr=False)
    syncUrl: str = field(repr=False)

    def IsConfigured(self) -> bool:
        """Return True when both URLs are set."""
        return bool(self.asyncUrl and self.syncUrl)


@dataclass(frozen=True)
class DatabaseTarget:
    """The host, port and database a test database URL connects to; no credential is kept.

    ``host`` is lower-cased. Both come from the URL itself, which must name them.
    """

    host: str
    port: int
    databaseName: str

    def __str__(self) -> str:
        """Return ``host:port/databaseName``, the credential-free form failure texts quote."""
        return f"{self.host}:{self.port}/{self.databaseName}"


@dataclass(frozen=True)
class ServerIdentity:
    """What one connection reached: its database, the server address and port, the start time.

    The postmaster start time tells two servers listening on one address and port apart.
    """

    databaseName: str
    serverAddress: str | None
    serverPort: int | None
    startedAt: datetime


@dataclass(frozen=True)
class DatabaseGuard:
    """The test-database guard of this module, handed to its own tests as a fixture.

    Tests never import this module, so they reach the guard functions and the value types they
    take through this object.
    """

    requireTestDatabase: Callable[[DatabaseUrls], DatabaseTarget]
    requireConnectedTestDatabase: Callable[[Connection, str], None]
    requireOneServer: Callable[[ServerIdentity, ServerIdentity], None]
    urlsType: type[DatabaseUrls]
    identityType: type[ServerIdentity]


@dataclass(frozen=True)
class SignOnUser:
    """A generated user committed to security.users, with the password it signs on with."""

    userId: str
    password: str = field(repr=False)
    userType: str


@dataclass(frozen=True)
class SeedUser:
    """One USRSEC image record (CSUSR01Y), each field right-trimmed of its space padding."""

    userId: str
    firstName: str
    lastName: str
    password: str = field(repr=False)
    userType: str


@dataclass(frozen=True)
class SignedOnClient:
    """An httpx client holding the session cookie of ``user``."""

    client: httpx.AsyncClient
    user: SignOnUser


@dataclass(frozen=True)
class SeedSnapshot:
    """The insertable columns of every row of every table, keyed by ``Table.fullname``."""

    rowsByTable: dict[str, tuple[dict[str, object], ...]] = field(repr=False)


class DatabaseUrlSettings(BaseSettings):
    """The database URLs as configured: the process environment, then backend/.env.

    Field names are the environment-variable names, as in app.core.config.Settings.
    """

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore", env_ignore_empty=True)

    DATABASE_URL: str = Field(default="", repr=False)
    SYNC_DATABASE_URL: str = Field(default="", repr=False)
    TEST_DATABASE_URL: str = Field(default="", repr=False)
    TEST_SYNC_DATABASE_URL: str = Field(default="", repr=False)


def RenameDatabase(url: str, databaseName: str) -> str:
    """Return ``url`` naming ``databaseName``; "" for a blank URL or one that does not parse."""
    if not url.strip():
        return ""
    try:
        renamedUrl = make_url(url).set(database=databaseName)
    except (ArgumentError, ValueError):
        return ""
    return renamedUrl.render_as_string(hide_password=False)


def ResolveDatabaseUrls() -> DatabaseUrls:
    """Return TEST_*_DATABASE_URL, or else the backend URL renamed to carddemo_test, per driver.

    Read before BootstrapTestEnvironment overwrites DATABASE_URL and SYNC_DATABASE_URL.
    """
    configuredUrls = DatabaseUrlSettings()
    asyncUrl = configuredUrls.TEST_DATABASE_URL or RenameDatabase(
        configuredUrls.DATABASE_URL, TEST_DATABASE_NAME
    )
    syncUrl = configuredUrls.TEST_SYNC_DATABASE_URL or RenameDatabase(
        configuredUrls.SYNC_DATABASE_URL, TEST_DATABASE_NAME
    )
    return DatabaseUrls(asyncUrl=asyncUrl, syncUrl=syncUrl)


def BootstrapTestEnvironment() -> DatabaseUrls:
    """Point every settings object of the session at the test database; never raises.

    APP_ENV, SECRET_KEY, BCRYPT_ROUNDS and BACKEND_CORS_ORIGINS override any backend/.env
    value. The database URLs are set only when they resolved.
    """
    testUrls = ResolveDatabaseUrls()
    os.environ["APP_ENV"] = TEST_APP_ENV
    os.environ["SECRET_KEY"] = secrets.token_urlsafe(SECRET_KEY_BYTES)
    os.environ["BCRYPT_ROUNDS"] = TEST_BCRYPT_ROUNDS
    os.environ["BACKEND_CORS_ORIGINS"] = ALLOWED_ORIGIN
    if testUrls.asyncUrl:
        os.environ["DATABASE_URL"] = testUrls.asyncUrl
    if testUrls.syncUrl:
        os.environ["SYNC_DATABASE_URL"] = testUrls.syncUrl
    return testUrls


TEST_DATABASE_URLS = BootstrapTestEnvironment()


def ParseDatabaseUrl(url: str) -> URL:
    """Return ``url`` parsed; fail the test run when it does not parse or names no database.

    The failure is raised outside the except block: the parse error quotes the URL, password
    included, and must not be chained into the report.
    """
    try:
        parsedUrl = make_url(url)
    except (ArgumentError, ValueError):
        parsedUrl = None
    if parsedUrl is None or not parsedUrl.database:
        pytest.fail(INVALID_URL_MESSAGE, pytrace=False)
    return parsedUrl


def RefuseTargetOverrides(parsedUrl: URL) -> None:
    """Fail when the query of ``parsedUrl`` sets a CONNECTION_TARGET_QUERY_KEYS key.

    Only the key names are reported, never their values or the URL.
    """
    queryKeys = {queryKey.lower() for queryKey in parsedUrl.query}
    overrideKeys = sorted(queryKeys & CONNECTION_TARGET_QUERY_KEYS)
    if overrideKeys:
        keyNames = ", ".join(repr(overrideKey) for overrideKey in overrideKeys)
        pytest.fail(TARGET_OVERRIDE_MESSAGE.format(keyNames=keyNames), pytrace=False)


def RequireExplicitEndpoint(parsedUrl: URL) -> None:
    """Fail unless ``parsedUrl`` names both its host and its port.

    psycopg2 (libpq) and asyncpg fill an omitted host or port from PGHOST and PGPORT, and libpq
    also from a service file, so such a URL could reach a server no comparison of the two URLs
    sees. Only the driver name and the missing parts are reported, never the URL.
    """
    endpointParts = (("host", parsedUrl.host), ("port", parsedUrl.port))
    missingParts = [partName for partName, partValue in endpointParts if partValue in (None, "")]
    if missingParts:
        failureText = MISSING_ENDPOINT_MESSAGE.format(
            driverName=parsedUrl.drivername, missingParts=" and ".join(missingParts)
        )
        pytest.fail(failureText, pytrace=False)


def UrlTarget(url: str) -> DatabaseTarget:
    """Return the host, port and database ``url`` connects to; fail on a redirecting query.

    A URL that leaves its host or port to the driver's environment is refused as well.
    """
    parsedUrl = ParseDatabaseUrl(url)
    RefuseTargetOverrides(parsedUrl)
    RequireExplicitEndpoint(parsedUrl)
    return DatabaseTarget(
        host=parsedUrl.host.lower(), port=parsedUrl.port, databaseName=parsedUrl.database
    )


def RequireTestDatabaseName(databaseName: str) -> None:
    """Fail unless ``databaseName`` ends in TEST_DATABASE_SUFFIX."""
    if not databaseName.endswith(TEST_DATABASE_SUFFIX):
        failureText = NOT_TEST_DATABASE_MESSAGE.format(
            databaseName=databaseName, suffix=TEST_DATABASE_SUFFIX
        )
        pytest.fail(failureText, pytrace=False)


def RequireTestDatabase(urls: DatabaseUrls) -> DatabaseTarget:
    """Return the one target both URLs reach; fail unless it is a database ending in _test.

    The fixtures drop and truncate that database, so the local carddemo database, a query key
    that redirects either driver, a URL that omits its host or port, and URLs reaching two hosts
    or ports are all refused before any engine exists. No failure text quotes a username or
    password.
    """
    if not urls.IsConfigured():
        pytest.fail(MISSING_URLS_MESSAGE, pytrace=False)
    asyncTarget = UrlTarget(urls.asyncUrl)
    syncTarget = UrlTarget(urls.syncUrl)
    if asyncTarget.databaseName != syncTarget.databaseName:
        pytest.fail(MISMATCHED_DATABASES_MESSAGE, pytrace=False)
    if asyncTarget != syncTarget:
        failureText = MISMATCHED_TARGETS_MESSAGE.format(
            asyncTarget=asyncTarget, syncTarget=syncTarget
        )
        pytest.fail(failureText, pytrace=False)
    RequireTestDatabaseName(syncTarget.databaseName)
    return syncTarget


def RequireConnectedTestDatabase(connection: Connection, expectedName: str) -> None:
    """Fail unless ``connection`` is open on ``expectedName`` and that name ends in _test.

    The reset and the restore run it inside the transaction that drops or truncates, before
    that statement, so the statement can only reach the database read here, whatever the
    driver did with the URL.
    """
    connectedName = connection.execute(select(func.current_database())).scalar_one()
    if connectedName != expectedName or not connectedName.endswith(TEST_DATABASE_SUFFIX):
        failureText = WRONG_CONNECTED_DATABASE_MESSAGE.format(
            connectedName=connectedName, expectedName=expectedName, suffix=TEST_DATABASE_SUFFIX
        )
        pytest.fail(failureText, pytrace=False)


def SetLocalLockTimeout(connection: Connection, timeout: str) -> None:
    """Bound every lock wait of the current transaction, as SET LOCAL lock_timeout does."""
    connection.execute(select(func.set_config(LOCK_TIMEOUT_SETTING, timeout, True)))


def DropManagedObjects(connection: Connection, schemaNames: Sequence[str]) -> None:
    """Drop the CardDemo schemas, the Alembic version table and public.ebcdic_sort_key."""
    preparer = connection.dialect.identifier_preparer
    for schemaName in schemaNames:
        quotedSchema = preparer.quote_schema(schemaName)
        connection.execute(text(f"DROP SCHEMA IF EXISTS {quotedSchema} CASCADE"))
    connection.execute(text(DROP_VERSION_TABLE_SQL))
    connection.execute(text(DROP_SORT_KEY_FUNCTION_SQL))


def UnreachableMessage(databaseName: str, driverError: BaseException) -> str:
    """Return the failure text of a driver's connection error, without the URL or its password.

    ``driverError`` is psycopg2's error (an OperationalError's ``orig``) or the OSError asyncpg
    raises; the reason is its first line, or its type name when it has no text.
    """
    reasonLines = str(driverError).strip().splitlines() or [type(driverError).__name__]
    return UNREACHABLE_DATABASE_MESSAGE.format(databaseName=databaseName, reason=reasonLines[0])


def ResetTestDatabase(syncEngine: Engine) -> None:
    """Remove every object the migrations create, so ``upgrade head`` starts from nothing.

    The drop runs only after the same transaction has read the validated test database as its
    current database. The failure is reported after the except block, so it shows only the
    actionable message rather than the chained driver traceback.
    """
    from app.models import ALL_SCHEMAS

    failureText = ""
    try:
        with syncEngine.begin() as connection:
            SetLocalLockTimeout(connection, RESET_LOCK_TIMEOUT)
            RequireConnectedTestDatabase(connection, syncEngine.url.database)
            DropManagedObjects(connection, ALL_SCHEMAS)
    except OperationalError as error:
        failureText = UnreachableMessage(syncEngine.url.database, error.orig)
    if failureText:
        pytest.fail(failureText, pytrace=False)


def ReenableLoggers() -> None:
    """Re-enable every logger a logging.config.fileConfig call may have disabled."""
    for registeredLogger in logging.root.manager.loggerDict.values():
        if isinstance(registeredLogger, logging.Logger):
            registeredLogger.disabled = False


def UpgradeToHead(syncUrl: str) -> None:
    """Run ``alembic upgrade head`` on the test database; APP_ENV=test makes 0002 seed it.

    The ini path is absolute, so the run does not depend on the working directory. The URL is
    escaped for configparser interpolation.
    """
    migrationConfig = Config(str(ALEMBIC_INI_PATH))
    migrationConfig.set_main_option(ALEMBIC_URL_OPTION, syncUrl.replace("%", "%%"))
    migrationConfig.attributes[ALEMBIC_LOGGING_ATTRIBUTE] = False
    command.upgrade(migrationConfig, ALEMBIC_HEAD)
    ReenableLoggers()


def InsertableColumns(table: Table) -> list[Column]:
    """Return the columns of ``table`` except the generated *_sort columns."""
    return [tableColumn for tableColumn in table.columns if tableColumn.computed is None]


def ReadTableRows(connection: Connection, table: Table) -> tuple[dict[str, object], ...]:
    """Return every row of ``table`` as a column-name dict, in the table's stored order."""
    result = connection.execute(select(*InsertableColumns(table)))
    return tuple(dict(rowMapping) for rowMapping in result.mappings())


def TakeSeedSnapshot(syncEngine: Engine) -> SeedSnapshot:
    """Return every row of the eleven dataset tables and the five support tables."""
    from app.models import Base

    with syncEngine.connect() as connection:
        rowsByTable = {
            table.fullname: ReadTableRows(connection, table)
            for table in Base.metadata.sorted_tables
        }
    return SeedSnapshot(rowsByTable=rowsByTable)


def TruncateStatement(connection: Connection, tables: Sequence[Table]) -> TextClause:
    """Return one TRUNCATE of ``tables`` that restarts identities and cascades."""
    preparer = connection.dialect.identifier_preparer
    tableNames = ", ".join(preparer.format_table(table) for table in tables)
    return text(f"TRUNCATE TABLE {tableNames} RESTART IDENTITY CASCADE")


def InsertSnapshotRows(
    connection: Connection, tables: Sequence[Table], snapshot: SeedSnapshot
) -> None:
    """Insert each table's snapshot rows in one executemany, parents first."""
    for table in tables:
        tableRows = snapshot.rowsByTable[table.fullname]
        if tableRows:
            connection.execute(insert(table), list(tableRows))


def ResetSerialSequences(connection: Connection) -> None:
    """Point each BIGSERIAL sequence (cycle_id, run_id) past the highest restored value."""
    from app.models import Base

    for table in Base.metadata.sorted_tables:
        serialColumn = table.autoincrement_column
        if serialColumn is None:
            continue
        sequenceName = func.pg_get_serial_sequence(table.fullname, serialColumn.name)
        nextValue = func.coalesce(func.max(serialColumn), 0) + 1
        connection.execute(select(func.setval(sequenceName, nextValue, False)))


def RestoreSeedSnapshot(syncEngine: Engine, snapshot: SeedSnapshot) -> None:
    """Replace the contents of every table with ``snapshot`` in one transaction.

    The truncate runs only after that transaction has read the validated test database as its
    current database. The lock timeout makes a lock a test leaked fail the teardown instead of
    hanging it.
    """
    from app.models import Base

    tables = Base.metadata.sorted_tables
    with syncEngine.begin() as connection:
        SetLocalLockTimeout(connection, RESTORE_LOCK_TIMEOUT)
        RequireConnectedTestDatabase(connection, syncEngine.url.database)
        connection.execute(TruncateStatement(connection, tables))
        connection.execute(text(DEFER_CONSTRAINTS_SQL))
        InsertSnapshotRows(connection, tables, snapshot)
        ResetSerialSequences(connection)


def ReadServerIdentity(syncEngine: Engine) -> ServerIdentity:
    """Return the ServerIdentity of ``syncEngine``, after checking its connected database.

    A database psycopg2 cannot reach fails the run with UNREACHABLE_DATABASE_MESSAGE, raised
    after the except block so the chained driver traceback is not reported.
    """
    try:
        with syncEngine.connect() as connection:
            RequireConnectedTestDatabase(connection, syncEngine.url.database)
            identityRow = connection.execute(DATABASE_IDENTITY_QUERY).one()
    except OperationalError as error:
        failureText = UnreachableMessage(syncEngine.url.database, error.orig)
    else:
        return ServerIdentity(*identityRow)
    pytest.fail(failureText, pytrace=False)


async def ReadAsyncServerIdentity(asyncUrl: str) -> ServerIdentity:
    """Return the ServerIdentity an unpooled asyncpg engine of ``asyncUrl`` reaches.

    The engine is disposed of before returning, so the check leaves no connection open. Through
    SQLAlchemy, asyncpg reports a refused, unresolvable or timed-out connection as an OSError
    (ConnectionRefusedError, socket.gaierror, TimeoutError); that fails the run with
    UNREACHABLE_DATABASE_MESSAGE, raised after the except block.
    """
    engine = create_async_engine(asyncUrl, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            identityRow = (await connection.execute(DATABASE_IDENTITY_QUERY)).one()
    except OSError as error:
        failureText = UnreachableMessage(engine.url.database, error)
    else:
        return ServerIdentity(*identityRow)
    finally:
        await engine.dispose()
    pytest.fail(failureText, pytrace=False)


def RequireOneServer(asyncIdentity: ServerIdentity, syncIdentity: ServerIdentity) -> None:
    """Fail unless asyncpg and psycopg2 reached one database of one running server."""
    if asyncIdentity != syncIdentity:
        failureText = MISMATCHED_SERVERS_MESSAGE.format(
            asyncIdentity=asyncIdentity, syncIdentity=syncIdentity
        )
        pytest.fail(failureText, pytrace=False)


def DecodeUsrsecRecord(record: bytes) -> SeedUser:
    """Return one 80-byte cp037 SEC-USER-DATA record, sliced by the CSUSR01Y offsets."""
    recordText = record.decode(EBCDIC_CODEC)
    return SeedUser(
        userId=recordText[USRSEC_ID_SLICE].rstrip(FIELD_PADDING),
        firstName=recordText[USRSEC_FIRST_NAME_SLICE].rstrip(FIELD_PADDING),
        lastName=recordText[USRSEC_LAST_NAME_SLICE].rstrip(FIELD_PADDING),
        password=recordText[USRSEC_PASSWORD_SLICE].rstrip(FIELD_PADDING),
        userType=recordText[USRSEC_TYPE_SLICE].rstrip(FIELD_PADDING),
    )


def ReadSeedUsers() -> tuple[SeedUser, ...]:
    """Return every record of the USRSEC image: ADMIN001-005 (type A), USER0001-0005 (U)."""
    imageBytes = USRSEC_IMAGE_PATH.read_bytes()
    if len(imageBytes) % USRSEC_RECORD_LENGTH:
        failureText = USRSEC_SIZE_MESSAGE.format(path=USRSEC_IMAGE_PATH, size=len(imageBytes))
        pytest.fail(failureText, pytrace=False)
    return tuple(
        DecodeUsrsecRecord(imageBytes[offset : offset + USRSEC_RECORD_LENGTH])
        for offset in range(0, len(imageBytes), USRSEC_RECORD_LENGTH)
    )


def BuildClient(application: "FastAPI") -> httpx.AsyncClient:
    """Return a client of ``application`` that sends the allow-listed Origin OriginGuard checks."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url=API_BASE_URL,
        headers={"Origin": ALLOWED_ORIGIN},
    )


def NewTestUserId() -> str:
    """Return a generated 7-character upper-case user ID that no seed user has."""
    return TEST_USER_ID_PREFIX + secrets.token_hex(USER_ID_HEX_BYTES).upper()


def NewTestCredential() -> str:
    """Return a generated 8-character upper-case password, printable in code page 037."""
    return secrets.token_urlsafe(CREDENTIAL_TOKEN_BYTES).upper()


def CreateSignOnUser(syncEngine: Engine, userType: str) -> SignOnUser:
    """Commit a generated user of ``userType`` to security.users and return it.

    The hash is of the upper-case password as created, which COSGN00C's upper-cased sign-on
    input matches.
    """
    from app.core.security import HashPassword
    from app.models import User

    user = SignOnUser(userId=NewTestUserId(), password=NewTestCredential(), userType=userType)
    userRow = {
        "user_id": user.userId,
        "first_name": TEST_USER_FIRST_NAME,
        "last_name": TEST_USER_LAST_NAME_PREFIX + userType,
        "password_hash": HashPassword(user.password, int(os.environ["BCRYPT_ROUNDS"])),
        "user_type": userType,
    }
    with syncEngine.begin() as connection:
        connection.execute(insert(User.__table__).values(userRow))
    return user


class LoggedInClients:
    """Signs generated users on through POST /api/v1/auth/login and closes their clients."""

    def __init__(self, application: "FastAPI", createUser: Callable[[str], SignOnUser]) -> None:
        self.application = application
        self.createUser = createUser
        self.openClients: list[httpx.AsyncClient] = []

    async def LoginAs(self, userType: str) -> SignedOnClient:
        """Create a ``userType`` user off the event loop, sign it on, return its client and user."""
        user = await asyncio.to_thread(self.createUser, userType)
        client = BuildClient(self.application)
        self.openClients.append(client)
        credentials = {"user_id": user.userId, "password": user.password}
        response = await client.post(LOGIN_PATH, json=credentials)
        assert response.status_code == HTTP_OK, response.text
        cookieName = self.application.state.settings.SESSION_COOKIE_NAME
        assert cookieName in client.cookies, f"sign-on set no {cookieName} cookie"
        return SignedOnClient(client=client, user=user)

    async def CloseAll(self) -> None:
        """Close every client LoginAs opened."""
        while self.openClients:
            await self.openClients.pop().aclose()


def OnlineRequestScope(application: "FastAPI") -> dict[str, object]:
    """Return the ASGI scope of a bare GET request to ``application``."""
    return {
        "type": "http",
        "app": application,
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "state": {},
    }


class OnlineContextFactory:
    """Builds service-level OnlineContext objects and releases their batch-window locks."""

    def __init__(self, application: "FastAPI") -> None:
        self.application = application
        self.openContexts: list[OnlineContext] = []

    async def Build(self, userType: str = REGULAR_USER_TYPE) -> "OnlineContext":
        """Return a context with its own request and session, for a user of ``userType``."""
        from starlette.requests import Request

        from app.core.dependencies import CurrentUser, OnlineContext

        context = OnlineContext(
            request=Request(OnlineRequestScope(self.application)),
            session=self.application.state.sessionFactory(),
            settings=self.application.state.settings,
            user=CurrentUser(userId=NewTestUserId(), userType=userType),
        )
        self.openContexts.append(context)
        return context

    async def CloseAll(self) -> None:
        """Release each context's shared batch-window lock, then close its session."""
        from app.core.locks import LeaveOnlineWindow

        while self.openContexts:
            context = self.openContexts.pop()
            try:
                await LeaveOnlineWindow(context.request)
            finally:
                await context.session.close()


@pytest.fixture(name="databaseUrls", scope="session")
def DatabaseUrlsFixture() -> DatabaseUrls:
    """Return the test database URLs resolved when this module was imported."""
    return TEST_DATABASE_URLS


@pytest.fixture(name="syncEngine", scope="session")
def SyncEngineFixture(databaseUrls: DatabaseUrls) -> Iterator[Engine]:
    """Yield the psycopg2 engine of the test database, refusing any non-test database.

    RequireTestDatabase validates both URLs before the engine is created.
    """
    RequireTestDatabase(databaseUrls)
    engine = create_engine(databaseUrls.syncUrl, pool_pre_ping=True, hide_parameters=True)
    yield engine
    engine.dispose()


@pytest_asyncio.fixture(name="verifiedDatabaseIdentity", scope="session")
async def VerifiedDatabaseIdentityFixture(
    databaseUrls: DatabaseUrls, syncEngine: Engine
) -> ServerIdentity:
    """Return the ServerIdentity both drivers reach; fail when asyncpg reaches another.

    migratedDatabase depends on it, so the comparison precedes the reset's DROP and the
    migration. psycopg2 is read first, so a database that is down is reported by psycopg2's
    actionable message; an asyncpg connection error gets the same message. A psycopg2-only
    redirect (PGHOSTADDR, a libpq service file) shows as a different identity, or as asyncpg
    failing to connect. The asyncpg engine is disposed of before the comparison, so no
    connection of this check stays open into the reset's DROP or a restore's TRUNCATE.
    """
    syncIdentity = ReadServerIdentity(syncEngine)
    asyncIdentity = await ReadAsyncServerIdentity(databaseUrls.asyncUrl)
    RequireOneServer(asyncIdentity, syncIdentity)
    return syncIdentity


@pytest.fixture(name="databaseGuard", scope="session")
def DatabaseGuardFixture() -> DatabaseGuard:
    """Return the test-database guard functions and value types, for the guard's own tests."""
    return DatabaseGuard(
        requireTestDatabase=RequireTestDatabase,
        requireConnectedTestDatabase=RequireConnectedTestDatabase,
        requireOneServer=RequireOneServer,
        urlsType=DatabaseUrls,
        identityType=ServerIdentity,
    )


@pytest.fixture(name="migratedDatabase", scope="session")
def MigratedDatabaseFixture(
    databaseUrls: DatabaseUrls, syncEngine: Engine, verifiedDatabaseIdentity: ServerIdentity
) -> SeedSnapshot:
    """Rebuild the test database at head (0009), seeded, and return its snapshot.

    verifiedDatabaseIdentity is set up first, so nothing is dropped or migrated until asyncpg
    and psycopg2 are known to reach one test database of one server.
    """
    ResetTestDatabase(syncEngine)
    UpgradeToHead(databaseUrls.syncUrl)
    return TakeSeedSnapshot(syncEngine)


@pytest.fixture(name="seedUsers", scope="session")
def SeedUsersFixture() -> tuple[SeedUser, ...]:
    """Return the ten USRSEC image users with their image passwords, for seed verification."""
    return ReadSeedUsers()


@pytest.fixture(name="testSettings")
def SettingsFixture() -> "Settings":
    """Return app Settings built fresh from the bootstrapped environment."""
    from app.core.config import Settings

    return Settings()


@pytest.fixture(name="restoreSeedState")
def RestoreSeedStateFixture(migratedDatabase: SeedSnapshot, syncEngine: Engine) -> Iterator[None]:
    """Yield nothing; at teardown, restore every table to the post-migration snapshot.

    Through migratedDatabase, verifiedDatabaseIdentity has made asyncpg known to reach the
    database psycopg2 resets before any fixture built on this one writes through it.
    """
    yield
    RestoreSeedSnapshot(syncEngine, migratedDatabase)


# Depends on restoreSeedState rather than only on migratedDatabase: its connections, and those
# of dbSession, then always close before the restore's TRUNCATE, which a still-open transaction
# would block, and anything committed through this engine is undone as well.
@pytest_asyncio.fixture(name="asyncEngine")
async def AsyncEngineFixture(
    databaseUrls: DatabaseUrls, restoreSeedState: None
) -> AsyncIterator[AsyncEngine]:
    """Yield an unpooled asyncpg engine of the migrated test database; dispose of it after."""
    engine = create_async_engine(databaseUrls.asyncUrl, poolclass=NullPool, hide_parameters=True)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(name="dbSession")
async def DbSessionFixture(asyncEngine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Yield a session joined to an outer transaction that is rolled back at teardown.

    A commit in the test releases a savepoint only, so nothing the session writes persists.
    """
    async with asyncEngine.connect() as connection:
        outerTransaction = await connection.begin()
        session = AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await outerTransaction.rollback()


@pytest_asyncio.fixture(name="testApp")
async def ApplicationFixture(
    testSettings: "Settings", restoreSeedState: None
) -> AsyncIterator["FastAPI"]:
    """Yield CreateApp(testSettings) inside its lifespan; leave it and dispose of its engine.

    httpx.ASGITransport sends no lifespan events, so the lifespan is entered here.
    """
    from app.core.database import DisposeEngine
    from app.main import CreateApp

    application = CreateApp(testSettings)
    async with contextlib.AsyncExitStack() as exitStack:
        exitStack.push_async_callback(DisposeEngine, application.state.engine)
        await exitStack.enter_async_context(application.router.lifespan_context(application))
        yield application


@pytest_asyncio.fixture(name="apiClient")
async def ApiClientFixture(testApp: "FastAPI") -> AsyncIterator[httpx.AsyncClient]:
    """Yield an anonymous client of testApp, closed at teardown."""
    async with BuildClient(testApp) as client:
        yield client


@pytest.fixture(name="createUser")
def CreateUserFixture(syncEngine: Engine, restoreSeedState: None) -> Callable[[str], SignOnUser]:
    """Return ``createUser(userType)``, which commits a generated sign-on user and returns it."""
    return functools.partial(CreateSignOnUser, syncEngine)


@pytest_asyncio.fixture(name="loginAs")
async def LoginAsFixture(
    testApp: "FastAPI", createUser: Callable[[str], SignOnUser]
) -> AsyncIterator[Callable[[str], Awaitable[SignedOnClient]]]:
    """Yield ``await loginAs(userType)``, any type (A, U, u, a, X); close its clients after."""
    loggedInClients = LoggedInClients(testApp, createUser)
    try:
        yield loggedInClients.LoginAs
    finally:
        await loggedInClients.CloseAll()


@pytest_asyncio.fixture(name="adminClient")
async def AdminClientFixture(
    loginAs: Callable[[str], Awaitable[SignedOnClient]],
) -> httpx.AsyncClient:
    """Return a client signed on as a new user of type A (admin)."""
    signedOnClient = await loginAs(ADMIN_USER_TYPE)
    return signedOnClient.client


@pytest_asyncio.fixture(name="regularClient")
async def RegularClientFixture(
    loginAs: Callable[[str], Awaitable[SignedOnClient]],
) -> httpx.AsyncClient:
    """Return a client signed on as a new user of type U (regular)."""
    signedOnClient = await loginAs(REGULAR_USER_TYPE)
    return signedOnClient.client


@pytest_asyncio.fixture(name="makeOnlineContext")
async def MakeOnlineContextFixture(
    testApp: "FastAPI",
) -> AsyncIterator[Callable[..., Awaitable["OnlineContext"]]]:
    """Yield ``await makeOnlineContext(userType="U")``; release and close its contexts after."""
    contextFactory = OnlineContextFactory(testApp)
    try:
        yield contextFactory.Build
    finally:
        await contextFactory.CloseAll()
