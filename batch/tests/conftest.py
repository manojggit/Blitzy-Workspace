"""Shared infrastructure of the batch suites: test database, frozen clock, CLI runners, builders.

Replaces: the batch operating environment of the CardDemo JCL chain for the suites under
batch/tests: the AWS.M2.CARDDEMO.* VSAM clusters and the sequential DALYTRAN.PS file that
jcl/POSTTRAN.jcl allocates (copybooks CVTRA05Y and CVTRA06Y), the USRSEC.PS image, and the
CLOSEFIL/OPENFIL batch window of jcl/CLOSEFIL.jcl. Specified in the Technical Specification,
Sections 0.6.8, 0.6.10, 0.7.3, 0.7.6, 0.9.2 and 0.9.3.

Environment. Importing this module, before any test module, points every batch component of the
session at the test database. The URL is TEST_SYNC_DATABASE_URL when set, otherwise
SYNC_DATABASE_URL with the database renamed carddemo_test; each is read from the process
environment first, then backend/.env, by a pydantic-settings model with the env-file options of
the settings classes, so the file has their dotenv syntax. No URL, or a database whose name does
not end in _test, stops the session at import, before anything can be dropped. The URL is then
written to SYNC_DATABASE_URL and TEST_SYNC_DATABASE_URL, with APP_ENV=test (so Alembic revision
0002 seeds) and BCRYPT_ROUNDS=4 (fast DUSRSECJ and seed hashing). In-process LoadBatchSettings()
calls, the Alembic subprocess and every carddemo-batch subprocess inherit os.environ, so all of
them reach the test database. Database work happens only inside fixtures, so collecting the
suite needs no database.

Fixtures (pytest name, scope):

- testDatabaseUrl (session): the psycopg2 URL of the test database.
- syncEngine (session): the batch job engine (batch.db.BuildEngine) on that database.
- migratedDatabase (session): drops the CardDemo schemas, runs ``alembic upgrade head`` and
  returns the SeedSnapshot of every table taken right after it.
- freshlyMigratedDatabase: the same drop and migration again, for a test that needs a database
  no earlier test has touched; returns the new snapshot.
- seedState: restores the migratedDatabase snapshot before the test, so a failed earlier test
  cannot leak state: the seed rows and the SEED cycle, steps 1-5 and 7-12 COMPLETED.
- outputDir: this test's CARDDEMO_OUTPUT_DIR, under tmp_path.
- fixedAsOf: the reconciliation clock text, 2022-07-18T00:00:00.
- jobContext: a JobContext for calling a runner directly: frozen clock, default parameters, no
  cycle, step or run.
- batchEnvironment: the BatchEnvironment composite, which keeps test signatures short.

Test modules import the helpers by name: InvokeCli (in process, so monkeypatches apply),
RunCli and StartCli (the carddemo-batch subprocess), KillProcess, WaitUntil, IsBatchWindowHeld,
CountJobRuns, StepRow, InjectStepFailure, InjectAtomicRollbackFailure, WriteSyntheticDailyImage,
InsertSyntheticCards and InsertSyntheticTransactions.

The backend suite (backend/tests/conftest.py) uses the same test database. The two suites run
one after the other, never at the same time.
"""

import importlib
import logging
import os
import signal
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from app.core.locks import BATCH_WINDOW_LOCK
from app.core.paths import ENV_FILE, REPO_ROOT
from app.models import (
    ALL_SCHEMAS,
    CYCLE_STEPS,
    CYCLES,
    JOB_RUNS,
    Account,
    Base,
    Card,
    CardXref,
    Customer,
    Transaction,
)
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import (
    BigInteger,
    Column,
    Connection,
    Engine,
    Table,
    TextClause,
    bindparam,
    cast,
    column,
    func,
    insert,
    select,
    table,
    text,
)
from sqlalchemy.dialects.postgresql import REGCLASS
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from typer.testing import CliRunner

from batch.cli import APP
from batch.config import LoadBatchSettings
from batch.db import BuildEngine
from batch.orchestration import batch_chain, run_ledger
from batch.orchestration.clock import Clock, ParseAsOf
from batch.orchestration.job_context import (
    EXIT_ABEND,
    EXIT_FAILURE,
    JobContext,
    JobFailure,
    JobParameters,
    JobResult,
)
from batch.tests.reconciliation import legacy_oracle

LOGGER = logging.getLogger(__name__)

TEST_DATABASE_NAME = "carddemo_test"
TEST_DATABASE_SUFFIX = "_test"
TEST_SYNC_URL_VARIABLE = "TEST_SYNC_DATABASE_URL"
SYNC_URL_VARIABLE = "SYNC_DATABASE_URL"
APP_ENV_VARIABLE = "APP_ENV"
BCRYPT_ROUNDS_VARIABLE = "BCRYPT_ROUNDS"
OUTPUT_DIR_VARIABLE = "CARDDEMO_OUTPUT_DIR"
TEST_APP_ENV = "test"
TEST_BCRYPT_ROUNDS = "4"
SLOW_MARKER_LINE = "slow: long-running interruption and kill tests"

FIXED_AS_OF_TEXT = "2022-07-18T00:00:00"
TESTS_DIR = Path(__file__).resolve().parent
EXPECTED_DIR = TESTS_DIR / "fixtures" / "expected"
LOADER_ROWS_DIR = EXPECTED_DIR / "loader_rows"
BACKEND_DIR = REPO_ROOT / "backend"
OUTPUT_SUBDIRECTORY = "batch-output"
BATCH_EXECUTABLE = Path(sys.executable).parent / "carddemo-batch"
ALEMBIC_UPGRADE_ARGUMENTS = ("-m", "alembic", "upgrade", "head")
CLI_TIMEOUT_SECONDS = 600
MIGRATION_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 0.1
KILL_WAIT_SECONDS = 30
# Bounds each lock wait of the drop and the restore, so a lock a test leaked fails the fixture
# instead of hanging the session.
SCHEMA_LOCK_TIMEOUT = "30s"
LOCK_TIMEOUT_SETTING = "lock_timeout"

DROP_SCHEMA_TEMPLATE = "DROP SCHEMA IF EXISTS {quotedSchema} CASCADE"
DROP_VERSION_TABLE_SQL = "DROP TABLE IF EXISTS public.alembic_version"
DROP_SORT_KEY_FUNCTION_SQL = "DROP FUNCTION IF EXISTS public.ebcdic_sort_key(text) CASCADE"
TRUNCATE_TEMPLATE = "TRUNCATE TABLE {tableNames} RESTART IDENTITY CASCADE"
DEFER_CONSTRAINTS_SQL = "SET CONSTRAINTS ALL DEFERRED"
# test_tranidx.py drops the TRANIDX index, so every restore recreates it.
PROC_TS_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS ix_transactions_proc_ts ON transactions.transactions (proc_ts)"
)
# The sequence behind a serial column; no row when the column has none.
SEQUENCE_IDENTITY_STATEMENT = text(
    "SELECT n.nspname, c.relname FROM pg_class c"
    " JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE c.oid = CAST(pg_get_serial_sequence(:tableName, :columnName) AS regclass)"
)
# pg_advisory_lock(bigint) shows its key as classid (high 32 bits) and objid (low 32 bits) with
# objsubid 1. pg_locks lists every database of the server, and the sibling databases on it use
# the same key, so only the current database's exclusive lock is the batch window.
WINDOW_LOCK_COUNT_STATEMENT = text(
    "SELECT count(*) FROM pg_locks"
    " WHERE locktype = 'advisory' AND granted AND mode = 'ExclusiveLock' AND objsubid = 1"
    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    " AND ((classid::bigint << 32) | objid::bigint) = :lockKey"
).bindparams(bindparam("lockKey", type_=BigInteger))

INJECTED_FAILURE_TEMPLATE = "INJECTED FAILURE AT STEP {stepNo}"
INJECTED_ROLLBACK_TEMPLATE = "INJECTED ROLLBACK AT STEP {stepNo}"
FINISH_ATOMIC_RUN_NAME = "FinishAtomicRun"
REPLACE_LOADER_MODULE = "batch.loaders.replace_loader"

MISSING_URL_MESSAGE = (
    "No test database URL: set TEST_SYNC_DATABASE_URL or SYNC_DATABASE_URL in backend/.env or"
    " the environment; without TEST_SYNC_DATABASE_URL the tests use SYNC_DATABASE_URL's server"
    " with the database carddemo_test"
)
INVALID_URL_MESSAGE = "{variableName} is not a valid database URL"
NOT_TEST_DATABASE_MESSAGE = (
    "The batch tests drop and recreate every CardDemo schema of their database, so its name must"
    " end in {suffix}; TEST_SYNC_DATABASE_URL names database {databaseName!r}"
)
WRONG_CONNECTED_DATABASE_MESSAGE = (
    "Connected to database {connectedName!r}, not the test database {expectedName!r} ending in"
    " {suffix}; nothing was dropped or truncated"
)
ENGINE_MISMATCH_MESSAGE = (
    "The batch settings target database {engineName!r}, not the test database {expectedName!r};"
    " a SYNC_DATABASE_URL override reached the session"
)
MIGRATION_FAILED_MESSAGE = (
    "alembic upgrade head failed with exit status {returnCode}\n--- stdout ---\n{stdout}"
    "\n--- stderr ---\n{stderr}"
)
MIGRATION_TIMEOUT_MESSAGE = "alembic upgrade head did not end within {timeout} s\n{output}"
CLI_MISSING_MESSAGE = (
    "carddemo-batch is not installed at {path}; install the editable packages with"
    " pip install -e backend -e batch"
)
CLI_TIMEOUT_MESSAGE = "carddemo-batch {arguments} did not end within {timeout} s\n{output}"
WAIT_TIMEOUT_MESSAGE = "Timed out after {timeout} s waiting for {condition}"
NO_CYCLE_STEP_MESSAGE = "No batch cycle holds chain step {stepNo}"
UNKNOWN_STEP_MESSAGE = "Chain step {stepNo} does not exist; the chain has steps 1 to {stepCount}"
PROCESS_ALREADY_EXITED_LOG = "carddemo-batch process %d had already exited before SIGKILL"

# Every synthetic key starts with a prefix no sample key of its width uses: the sample accounts
# and customers are 1-50 with leading zeros, the sample cards starting with 9 continue with 5 to
# 8, and the sample daily and interest TRAN-IDs start with 0 and 2.
SYNTHETIC_KEY_PREFIX = "9"
SYNTHETIC_DAILY_ID_PREFIX = "9"
SYNTHETIC_TRAN_ID_PREFIX = "8"
SYNTHETIC_SSN_PREFIX = "1"
ACCOUNT_ID_DIGITS = 11
CUSTOMER_ID_DIGITS = 9
CARD_NUMBER_DIGITS = 16
TRAN_ID_LENGTH = 16
SSN_DIGITS = 9
CVV_MODULUS = 1000
SYNTHETIC_TYPE_CD = "01"
SYNTHETIC_CAT_CD = 1
SYNTHETIC_SOURCE = "POS TERM"
SYNTHETIC_DAILY_DESC_TEMPLATE = "SYNTHETIC PURCHASE {recordIndex}"
SYNTHETIC_TRAN_DESC_TEMPLATE = "SYNTHETIC TRANSACTION {tranNumber}"
SYNTHETIC_DAILY_AMOUNT = Decimal("1.00")
SYNTHETIC_MERCHANT_ID = "000000001"
SYNTHETIC_MERCHANT_NAME = "SYNTHETIC MERCHANT"
SYNTHETIC_CITY = "SYNTHETIC CITY"
SYNTHETIC_ZIP = "00000"
SYNTHETIC_STATE_CD = "NC"
SYNTHETIC_COUNTRY_CD = "USA"
SYNTHETIC_FIRST_NAME = "SYNTHETIC"
SYNTHETIC_MIDDLE_NAME = "TEST"
SYNTHETIC_LAST_NAME_TEMPLATE = "HOLDER{index}"
SYNTHETIC_ADDRESS_TEMPLATE = "{index} SYNTHETIC STREET"
SYNTHETIC_EMBOSSED_NAME_TEMPLATE = "SYNTHETIC HOLDER{index}"
ACTIVE_STATUS = "Y"
PRIMARY_HOLDER = "Y"
SYNTHETIC_FICO_SCORE = 700
SYNTHETIC_CREDIT_LIMIT = Decimal("100000.00")
SYNTHETIC_CASH_CREDIT_LIMIT = Decimal("10000.00")
ZERO_AMOUNT = Decimal("0.00")
SYNTHETIC_OPEN_DATE = date(2020, 1, 1)
SYNTHETIC_EXPIRY_DATE = date(2025, 12, 31)
SYNTHETIC_BIRTH_DATE = date(1980, 1, 1)
# The ORIG-TS of every sample DALYTRAN record.
SYNTHETIC_DAILY_ORIG_TS = datetime(2022, 6, 10, 19, 27, 53)
SYNTHETIC_TRAN_TIMESTAMP = datetime(2022, 7, 17, 10, 0, 0)
FORMAT_SPACE = "S"
FORMAT_DB2 = "D"
FORMAT_BLANK = " "
SYNTHETIC_KEY_OVERFLOW_MESSAGE = "Synthetic index {index} does not fit a {digits}-character key"
NO_CARDS_MESSAGE = "A synthetic daily image needs at least one card number"
SYNTHETIC_COUNT_MESSAGE = "The synthetic card count must be at least 1, got {count}"
SPEC_AMOUNT_MESSAGE = "amount must be a Decimal: money is never floating point"
SPEC_PER_CARD_MESSAGE = "perCard must not be negative, got {perCard}"


class DatabaseUrlSettings(BaseSettings):
    """The two batch database URLs as configured: the process environment, then backend/.env.

    The env-file options are those of batch.config.BatchSettings, so backend/.env is read with
    the same python-dotenv syntax (export prefixes, quotes, inline comments) and an empty value
    counts as unset. Field names are the environment-variable names.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
        case_sensitive=True,
        hide_input_in_errors=True,
    )

    TEST_SYNC_DATABASE_URL: str = Field(default="", repr=False)
    SYNC_DATABASE_URL: str = Field(default="", repr=False)


def ParseDatabaseUrl(url: str, variableName: str) -> URL:
    """Parse a database URL; the error names the variable and never repeats the URL's password."""
    try:
        return make_url(url)
    except (ArgumentError, ValueError):
        raise RuntimeError(INVALID_URL_MESSAGE.format(variableName=variableName)) from None


def ResolveTestDatabaseUrl(envFile: Path = ENV_FILE) -> str:
    """Return TEST_SYNC_DATABASE_URL, else SYNC_DATABASE_URL with the database carddemo_test.

    Both are read from the process environment, then from envFile (backend/.env by default).
    Raises RuntimeError when neither is set or the URL does not parse.
    """
    configuredUrls = DatabaseUrlSettings(_env_file=envFile)
    if configuredUrls.TEST_SYNC_DATABASE_URL:
        return configuredUrls.TEST_SYNC_DATABASE_URL
    if not configuredUrls.SYNC_DATABASE_URL:
        raise RuntimeError(MISSING_URL_MESSAGE)
    syncUrl = ParseDatabaseUrl(configuredUrls.SYNC_DATABASE_URL, SYNC_URL_VARIABLE)
    renamedUrl = syncUrl.set(database=TEST_DATABASE_NAME)
    return renamedUrl.render_as_string(hide_password=False)


def RequireTestDatabaseName(url: str) -> None:
    """Raise RuntimeError unless the URL's database name ends in _test, so no other is dropped."""
    databaseName = ParseDatabaseUrl(url, TEST_SYNC_URL_VARIABLE).database or ""
    if not databaseName.endswith(TEST_DATABASE_SUFFIX):
        raise RuntimeError(
            NOT_TEST_DATABASE_MESSAGE.format(suffix=TEST_DATABASE_SUFFIX, databaseName=databaseName)
        )


def BootstrapTestEnvironment() -> str:
    """Resolve and check the test database URL, point the process environment at it, return it."""
    testUrl = ResolveTestDatabaseUrl()
    RequireTestDatabaseName(testUrl)
    os.environ[APP_ENV_VARIABLE] = TEST_APP_ENV
    os.environ[SYNC_URL_VARIABLE] = testUrl
    os.environ[TEST_SYNC_URL_VARIABLE] = testUrl
    os.environ[BCRYPT_ROUNDS_VARIABLE] = TEST_BCRYPT_ROUNDS
    return testUrl


# Runs when pytest imports this conftest, before any test module, so every settings object and
# subprocess of the session reaches the test database, and a wrong URL stops the session first.
TEST_SYNC_DATABASE_URL_VALUE = BootstrapTestEnvironment()


def pytest_configure(config: pytest.Config) -> None:
    """Register the slow marker, which batch/pyproject.toml also declares; a repeat is harmless."""
    config.addinivalue_line("markers", SLOW_MARKER_LINE)


@dataclass(frozen=True)
class SeedSnapshot:
    """Every table's rows and every serial sequence's state, read right after a migration.

    tableRows is keyed by qualified table name (for example "accounts.accounts"), without the
    generated *_sort columns. sequenceValues maps each sequence, as a quoted regclass name, to its
    (last_value, is_called).
    """

    tableRows: dict[str, list[dict[str, object]]]
    sequenceValues: dict[str, tuple[int, bool]]


@dataclass(frozen=True)
class BatchEnvironment:
    """A seeded test database and what a batch test needs around it, as one fixture value.

    engine is the session's job engine; outputDir is CARDDEMO_OUTPUT_DIR for this test; asOfText
    is the --as-of value of the frozen clock; snapshot is the restored seed state.
    """

    engine: Engine
    outputDir: Path
    asOfText: str
    snapshot: SeedSnapshot


@dataclass(frozen=True)
class CliOutcome:
    """The exit status of one carddemo-batch command and its combined stdout and stderr."""

    exitCode: int
    output: str


@dataclass(frozen=True)
class SyntheticTransactionSpec:
    """What InsertSyntheticTransactions inserts: perCard rows of amount for each of cardNums."""

    cardNums: Sequence[str]
    perCard: int
    amount: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError(SPEC_AMOUNT_MESSAGE)
        if self.perCard < 0:
            raise ValueError(SPEC_PER_CARD_MESSAGE.format(perCard=self.perCard))


def SetLocalLockTimeout(connection: Connection, timeout: str) -> None:
    """Bound every lock wait of the current transaction, as SET LOCAL lock_timeout does."""
    connection.execute(select(func.set_config(LOCK_TIMEOUT_SETTING, timeout, True)))


def RequireConnectedTestDatabase(connection: Connection, expectedName: str | None) -> None:
    """Fail unless the connection is open on expectedName, a database whose name ends in _test.

    The drop and the restore call it inside their own transaction, before their statements, so
    those can only reach the database read here.
    """
    connectedName = connection.execute(select(func.current_database())).scalar_one()
    if connectedName != expectedName or not connectedName.endswith(TEST_DATABASE_SUFFIX):
        failureText = WRONG_CONNECTED_DATABASE_MESSAGE.format(
            connectedName=connectedName, expectedName=expectedName, suffix=TEST_DATABASE_SUFFIX
        )
        pytest.fail(failureText, pytrace=False)


def DropApplicationSchema(engine: Engine) -> None:
    """Drop the nine CardDemo schemas, the Alembic version table and public.ebcdic_sort_key.

    The schema names are the app.models.ALL_SCHEMAS constants, quoted by the dialect.
    """
    preparer = engine.dialect.identifier_preparer
    with engine.begin() as connection:
        SetLocalLockTimeout(connection, SCHEMA_LOCK_TIMEOUT)
        RequireConnectedTestDatabase(connection, engine.url.database)
        for schemaName in ALL_SCHEMAS:
            quotedSchema = preparer.quote_schema(schemaName)
            connection.execute(text(DROP_SCHEMA_TEMPLATE.format(quotedSchema=quotedSchema)))
        connection.execute(text(DROP_VERSION_TABLE_SQL))
        connection.execute(text(DROP_SORT_KEY_FUNCTION_SQL))


def OutputText(capturedOutput: str | bytes | None) -> str:
    """Return captured subprocess output as text; a TimeoutExpired may hold bytes or None."""
    if capturedOutput is None:
        return ""
    if isinstance(capturedOutput, bytes):
        return capturedOutput.decode(errors="replace")
    return capturedOutput


def RunAlembicUpgrade() -> None:
    """Run ``alembic upgrade head`` from backend/, as documented; APP_ENV=test makes 0002 seed.

    The subprocess inherits os.environ, whose SYNC_DATABASE_URL is the test database. A failure
    or a timeout fails the test with the migration's output.
    """
    failureText = ""
    try:
        subprocess.run(
            [sys.executable, *ALEMBIC_UPGRADE_ARGUMENTS],
            cwd=BACKEND_DIR,
            env=dict(os.environ),
            check=True,
            capture_output=True,
            text=True,
            timeout=MIGRATION_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as error:
        failureText = MIGRATION_FAILED_MESSAGE.format(
            returnCode=error.returncode, stdout=error.stdout, stderr=error.stderr
        )
    except subprocess.TimeoutExpired as error:
        capturedText = OutputText(error.stdout) + OutputText(error.stderr)
        failureText = MIGRATION_TIMEOUT_MESSAGE.format(timeout=error.timeout, output=capturedText)
    if failureText:
        pytest.fail(failureText, pytrace=False)


def InsertableColumns(sourceTable: Table) -> list[Column]:
    """Return the columns of sourceTable except the generated *_sort columns."""
    return [tableColumn for tableColumn in sourceTable.columns if tableColumn.computed is None]


def ReadTableRows(connection: Connection, sourceTable: Table) -> list[dict[str, object]]:
    """Return every row of sourceTable as a column-name dict, generated columns left out."""
    result = connection.execute(select(*InsertableColumns(sourceTable)))
    return [dict(rowMapping) for rowMapping in result.mappings()]


def SerialSequenceNames(connection: Connection) -> list[tuple[str, str]]:
    """Return the (schema, name) of the sequence behind each serial column of Base.metadata.

    These are batch.cycles.cycle_id and batch.job_runs.run_id, the only BIGSERIAL columns.
    """
    sequenceNames: list[tuple[str, str]] = []
    for sourceTable in Base.metadata.sorted_tables:
        serialColumn = sourceTable.autoincrement_column
        if serialColumn is None:
            continue
        parameters = {"tableName": sourceTable.fullname, "columnName": serialColumn.name}
        identity = connection.execute(SEQUENCE_IDENTITY_STATEMENT, parameters).one_or_none()
        if identity is not None:
            sequenceNames.append((identity.nspname, identity.relname))
    return sequenceNames


def ReadSequenceState(
    connection: Connection, schemaName: str, sequenceName: str
) -> tuple[int, bool]:
    """Return the (last_value, is_called) of one sequence, read as a relation."""
    stateColumns = (column("last_value"), column("is_called"))
    sequenceTable = table(sequenceName, *stateColumns, schema=schemaName)
    row = connection.execute(select(sequenceTable.c.last_value, sequenceTable.c.is_called)).one()
    return int(row.last_value), bool(row.is_called)


def ReadSequenceValues(connection: Connection) -> dict[str, tuple[int, bool]]:
    """Return the state of every serial sequence, keyed by its quoted regclass name."""
    preparer = connection.dialect.identifier_preparer
    return {
        f"{preparer.quote_schema(schemaName)}.{preparer.quote(sequenceName)}": ReadSequenceState(
            connection, schemaName, sequenceName
        )
        for schemaName, sequenceName in SerialSequenceNames(connection)
    }


def TakeSeedSnapshot(engine: Engine) -> SeedSnapshot:
    """Return the rows of the eleven dataset tables and five support tables, and the sequences."""
    with engine.connect() as connection:
        tableRows = {
            sourceTable.fullname: ReadTableRows(connection, sourceTable)
            for sourceTable in Base.metadata.sorted_tables
        }
        sequenceValues = ReadSequenceValues(connection)
    return SeedSnapshot(tableRows=tableRows, sequenceValues=sequenceValues)


def MigrateFromNothing(engine: Engine) -> SeedSnapshot:
    """Drop every CardDemo object, run ``alembic upgrade head`` and snapshot the result."""
    DropApplicationSchema(engine)
    RunAlembicUpgrade()
    return TakeSeedSnapshot(engine)


def TruncateStatement(connection: Connection, tables: Sequence[Table]) -> TextClause:
    """Return one TRUNCATE of the tables that restarts their identities and cascades."""
    preparer = connection.dialect.identifier_preparer
    tableNames = ", ".join(preparer.format_table(sourceTable) for sourceTable in tables)
    return text(TRUNCATE_TEMPLATE.format(tableNames=tableNames))


def InsertSnapshotRows(
    connection: Connection, tables: Sequence[Table], snapshot: SeedSnapshot
) -> None:
    """Insert each table's snapshot rows in one executemany, parents first."""
    for sourceTable in tables:
        tableRows = snapshot.tableRows[sourceTable.fullname]
        if tableRows:
            connection.execute(insert(sourceTable), tableRows)


def RestoreSequenceValues(connection: Connection, snapshot: SeedSnapshot) -> None:
    """Set every serial sequence back to its snapshot (last_value, is_called)."""
    for sequenceName, (lastValue, isCalled) in snapshot.sequenceValues.items():
        sequenceClass = cast(sequenceName, REGCLASS)
        connection.execute(select(func.setval(sequenceClass, lastValue, isCalled)))


def RestoreSeedSnapshot(engine: Engine, snapshot: SeedSnapshot) -> None:
    """Replace the contents of every table with the snapshot, in one transaction.

    The result equals a fresh ``alembic upgrade head``: the seed rows, the SEED cycle, the
    sequences and the TRANIDX index. The truncate runs only after the transaction has read the
    test database as its current database.
    """
    tables = Base.metadata.sorted_tables
    with engine.begin() as connection:
        SetLocalLockTimeout(connection, SCHEMA_LOCK_TIMEOUT)
        RequireConnectedTestDatabase(connection, engine.url.database)
        connection.execute(TruncateStatement(connection, tables))
        connection.execute(text(DEFER_CONSTRAINTS_SQL))
        InsertSnapshotRows(connection, tables, snapshot)
        RestoreSequenceValues(connection, snapshot)
        connection.execute(text(PROC_TS_INDEX_DDL))


def RequireEngineOnTestDatabase(engine: Engine, testDatabaseUrl: str) -> None:
    """Fail unless the batch settings' engine targets the resolved test database."""
    expectedName = make_url(testDatabaseUrl).database
    if engine.url.database != expectedName:
        failureText = ENGINE_MISMATCH_MESSAGE.format(
            engineName=engine.url.database, expectedName=expectedName
        )
        pytest.fail(failureText, pytrace=False)


@pytest.fixture(name="testDatabaseUrl", scope="session")
def TestDatabaseUrl() -> str:
    """Return the psycopg2 URL of the test database, whose name ends in _test."""
    return TEST_SYNC_DATABASE_URL_VALUE


@pytest.fixture(name="syncEngine", scope="session")
def SyncEngine() -> Iterator[Engine]:
    """Yield the batch job engine on the test database, disposed of when the session ends."""
    engine = BuildEngine(LoadBatchSettings())
    yield engine
    engine.dispose()


@pytest.fixture(name="migratedDatabase", scope="session")
def MigratedDatabase(testDatabaseUrl: str, syncEngine: Engine) -> SeedSnapshot:
    """Drop the CardDemo schemas, run ``alembic upgrade head`` once, and snapshot every table."""
    RequireEngineOnTestDatabase(syncEngine, testDatabaseUrl)
    return MigrateFromNothing(syncEngine)


@pytest.fixture(name="freshlyMigratedDatabase")
def FreshlyMigratedDatabase(
    migratedDatabase: SeedSnapshot, syncEngine: Engine, outputDir: Path
) -> SeedSnapshot:
    """Drop and migrate the test database again for this test, and return its new snapshot.

    The session snapshot of migratedDatabase stays valid, so seedState restores it for later
    tests whatever this test leaves behind.
    """
    return MigrateFromNothing(syncEngine)


@pytest.fixture(name="seedState")
def SeedState(migratedDatabase: SeedSnapshot, syncEngine: Engine) -> SeedSnapshot:
    """Restore the post-migration seed state before the test, and return its snapshot."""
    RestoreSeedSnapshot(syncEngine, migratedDatabase)
    return migratedDatabase


@pytest.fixture(name="outputDir")
def OutputDir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create this test's batch output root and point CARDDEMO_OUTPUT_DIR at it, absolute."""
    outputPath = tmp_path / OUTPUT_SUBDIRECTORY
    outputPath.mkdir()
    monkeypatch.setenv(OUTPUT_DIR_VARIABLE, str(outputPath))
    return outputPath


@pytest.fixture(name="fixedAsOf")
def FixedAsOf() -> str:
    """Return the --as-of text that freezes the batch clock in reconciliation runs."""
    return FIXED_AS_OF_TEXT


@pytest.fixture(name="jobContext")
def BatchJobContext(
    seedState: SeedSnapshot, outputDir: Path, fixedAsOf: str
) -> Iterator[JobContext]:
    """Yield a JobContext on the seeded test database for calling a runner directly.

    Its settings see this test's outputDir, its clock is frozen at fixedAsOf and its parameters
    are the defaults; it has no cycle, step or run, so the runner records no ledger rows. Its
    engine is disposed of after the test.
    """
    settings = LoadBatchSettings()
    engine = BuildEngine(settings)
    yield JobContext(
        engine=engine,
        settings=settings,
        clock=Clock(ParseAsOf(fixedAsOf)),
        parameters=JobParameters(),
    )
    engine.dispose()


@pytest.fixture(name="batchEnvironment")
def BatchEnvironmentFixture(
    seedState: SeedSnapshot, outputDir: Path, syncEngine: Engine, fixedAsOf: str
) -> BatchEnvironment:
    """Return the seeded test database, this test's output directory and the frozen clock text."""
    return BatchEnvironment(
        engine=syncEngine, outputDir=outputDir, asOfText=fixedAsOf, snapshot=seedState
    )


def InvokeCli(arguments: Sequence[str]) -> CliOutcome:
    """Run a carddemo-batch command in this process, so a test's monkeypatches apply.

    output is Click's interleaved stdout and stderr, which carries the CLI's log lines. An
    exception the CLI did not turn into an exit status gives exit status 1, as CliRunner reports
    it, and its traceback is appended to output.
    """
    result = CliRunner().invoke(APP, list(arguments))
    output = result.output
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        output += "".join(traceback.format_exception(result.exception))
    return CliOutcome(exitCode=result.exit_code, output=output)


def RequireBatchExecutable() -> None:
    """Fail with an install hint when the carddemo-batch console script is missing."""
    if not BATCH_EXECUTABLE.is_file():
        pytest.fail(CLI_MISSING_MESSAGE.format(path=BATCH_EXECUTABLE), pytrace=False)


def CliCommand(arguments: Sequence[str]) -> list[str]:
    """Return the argv of a carddemo-batch subprocess."""
    return [str(BATCH_EXECUTABLE), *arguments]


def RunCli(arguments: Sequence[str], workingDir: Path | None = None) -> CliOutcome:
    """Run carddemo-batch as a subprocess, the system boundary, and return its outcome.

    The process starts in workingDir, default the repository root, with this process's
    environment, so it targets the test database and the test's CARDDEMO_OUTPUT_DIR. output is
    its stdout followed by its stderr. A run past CLI_TIMEOUT_SECONDS fails the test.
    """
    RequireBatchExecutable()
    try:
        completed = subprocess.run(
            CliCommand(arguments),
            cwd=workingDir or REPO_ROOT,
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        capturedText = OutputText(error.stdout) + OutputText(error.stderr)
        failureText = CLI_TIMEOUT_MESSAGE.format(
            arguments=" ".join(arguments), timeout=error.timeout, output=capturedText
        )
        pytest.fail(failureText, pytrace=False)
    return CliOutcome(exitCode=completed.returncode, output=completed.stdout + completed.stderr)


def StartCli(arguments: Sequence[str], workingDir: Path | None = None) -> subprocess.Popen:
    """Start carddemo-batch in the background, as RunCli runs it, and return the process.

    Its output is discarded: a pipe nobody reads would stall a long run once its buffer fills.
    Use RunCli for a command whose output a test reads, and KillProcess or wait() to end it.
    """
    RequireBatchExecutable()
    return subprocess.Popen(
        CliCommand(arguments),
        cwd=workingDir or REPO_ROOT,
        env=dict(os.environ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def KillProcess(process: subprocess.Popen) -> int:
    """Kill the process with SIGKILL, as a crash ends it, wait for it, and return its status.

    PostgreSQL then drops the process's connections, with the batch window and any open
    transaction.
    """
    try:
        process.send_signal(signal.SIGKILL)
    except ProcessLookupError:
        LOGGER.info(PROCESS_ALREADY_EXITED_LOG, process.pid)
    return process.wait(timeout=KILL_WAIT_SECONDS)


def WaitUntil(
    predicate: Callable[[], bool], timeoutSeconds: float, description: str | None = None
) -> None:
    """Poll predicate every POLL_INTERVAL_SECONDS until it holds; fail the test at timeoutSeconds.

    description names the awaited condition in the failure; it defaults to the predicate's name.
    """
    deadline = time.monotonic() + timeoutSeconds
    while not predicate():
        if time.monotonic() >= deadline:
            condition = description or getattr(predicate, "__qualname__", repr(predicate))
            failureText = WAIT_TIMEOUT_MESSAGE.format(timeout=timeoutSeconds, condition=condition)
            pytest.fail(failureText, pytrace=False)
        time.sleep(POLL_INTERVAL_SECONDS)


def IsBatchWindowHeld(engine: Engine) -> bool:
    """Return whether a session holds the exclusive batch window on the engine's database."""
    with engine.connect() as connection:
        heldCount = connection.execute(
            WINDOW_LOCK_COUNT_STATEMENT, {"lockKey": BATCH_WINDOW_LOCK}
        ).scalar_one()
    return heldCount > 0


def CountJobRuns(engine: Engine, stepNo: int | None = None, status: str | None = None) -> int:
    """Return the number of batch.job_runs rows, of every cycle, with the given step and status."""
    statement = select(func.count()).select_from(JOB_RUNS)
    if stepNo is not None:
        statement = statement.where(JOB_RUNS.c.step_no == stepNo)
    if status is not None:
        statement = statement.where(JOB_RUNS.c.status == status)
    with engine.connect() as connection:
        return connection.execute(statement).scalar_one()


def StepRow(engine: Engine, stepNo: int) -> dict[str, object]:
    """Return the batch.cycle_steps row of a chain step in the current cycle.

    The current cycle is the OPEN one, else the latest: completing step 18 closes the cycle,
    even when it runs on its own. The row holds cycle_id, cycle_status, step_no, state and
    last_run_id. Fails the test when no cycle holds the step.
    """
    statement = (
        select(
            CYCLE_STEPS.c.cycle_id,
            CYCLES.c.status.label("cycle_status"),
            CYCLE_STEPS.c.step_no,
            CYCLE_STEPS.c.state,
            CYCLE_STEPS.c.last_run_id,
        )
        .join_from(CYCLE_STEPS, CYCLES, CYCLE_STEPS.c.cycle_id == CYCLES.c.cycle_id)
        .where(CYCLE_STEPS.c.step_no == stepNo)
        .order_by((CYCLES.c.status == run_ledger.CYCLE_OPEN).desc(), CYCLES.c.cycle_id.desc())
        .limit(1)
    )
    with engine.connect() as connection:
        row = connection.execute(statement).mappings().one_or_none()
    if row is None:
        pytest.fail(NO_CYCLE_STEP_MESSAGE.format(stepNo=stepNo), pytrace=False)
    return dict(row)


def FindChainStep(stepNo: int) -> batch_chain.ChainStep:
    """Return the chain step numbered stepNo; ValueError for a number outside 1-18."""
    for step in batch_chain.CHAIN:
        if step.stepNo == stepNo:
            return step
    raise ValueError(UNKNOWN_STEP_MESSAGE.format(stepNo=stepNo, stepCount=len(batch_chain.CHAIN)))


def DefaultInjectedExitCode(step: batch_chain.ChainStep) -> int:
    """Return the exit status a failing step raises: abend 16 when per-write, else 12."""
    if step.durability == batch_chain.DURABILITY_PER_WRITE:
        return EXIT_ABEND
    return EXIT_FAILURE


def InjectStepFailure(
    monkeypatch: pytest.MonkeyPatch, stepNo: int, exitCode: int | None = None
) -> None:
    """Make the runner of chain step stepNo raise JobFailure before it writes anything.

    exitCode defaults to 16 for the per-write steps 13 and 14 (the legacy abend) and 12 for the
    others. The wrapper replaces the entry point on the runner's module, which ModuleEntryPoint
    resolves at each call, and fails only when context.stepNo is stepNo, so the other TRANBKP
    occurrence (step 6 or 15), which shares the runner, runs unchanged.
    """
    step = FindChainStep(stepNo)
    entryPoint = step.job.runner
    runnerModule = importlib.import_module(entryPoint.moduleName)
    originalRunner = getattr(runnerModule, entryPoint.attributeName)
    failureExitCode = DefaultInjectedExitCode(step) if exitCode is None else exitCode
    failureMessage = INJECTED_FAILURE_TEMPLATE.format(stepNo=stepNo)

    def FailingRunner(context: JobContext) -> JobResult:
        if context.stepNo == stepNo:
            raise JobFailure(failureExitCode, failureMessage)
        return originalRunner(context)

    monkeypatch.setattr(runnerModule, entryPoint.attributeName, FailingRunner)


def InjectAtomicRollbackFailure(monkeypatch: pytest.MonkeyPatch, stepNo: int) -> None:
    """Make run_ledger.FinishAtomicRun raise JobFailure(12) inside chain step stepNo's transaction.

    The step's writes have run when its final ledger write fails, so the step's own transaction
    rolls them back with it. Steps whose runner finishes through FinishAtomicRun are reached:
    the loaders (through replace_loader), TRANBKP, COMBTRAN, TRANIDX and the final write of
    POSTTRAN and INTCALC; CREASTMT finishes through FinishIfRunning instead. A module that bound
    the name itself is patched too.
    """
    step = FindChainStep(stepNo)
    originalFinish = run_ledger.FinishAtomicRun
    failureMessage = INJECTED_ROLLBACK_TEMPLATE.format(stepNo=stepNo)

    def FailingFinish(connection: Connection, context: JobContext, result: JobResult) -> None:
        if context.stepNo == stepNo:
            raise JobFailure(EXIT_FAILURE, failureMessage)
        originalFinish(connection, context, result)

    monkeypatch.setattr(run_ledger, FINISH_ATOMIC_RUN_NAME, FailingFinish)
    for moduleName in (step.job.runner.moduleName, REPLACE_LOADER_MODULE):
        boundModule = importlib.import_module(moduleName)
        if hasattr(boundModule, FINISH_ATOMIC_RUN_NAME):
            monkeypatch.setattr(boundModule, FINISH_ATOMIC_RUN_NAME, FailingFinish)


def SyntheticKey(index: int, width: int, prefix: str = SYNTHETIC_KEY_PREFIX) -> str:
    """Return prefix followed by index zero-padded to a width-character digit key."""
    keyText = prefix + f"{index:0{width - len(prefix)}d}"
    if index < 0 or len(keyText) != width:
        raise ValueError(SYNTHETIC_KEY_OVERFLOW_MESSAGE.format(index=index, digits=width))
    return keyText


def SyntheticDailyValues(recordIndex: int, cardNum: str) -> dict[str, object]:
    """Return the CVTRA06Y field values of synthetic daily record recordIndex (0-based)."""
    return {
        "tran_id": SyntheticKey(recordIndex, TRAN_ID_LENGTH, SYNTHETIC_DAILY_ID_PREFIX),
        "tran_type_cd": SYNTHETIC_TYPE_CD,
        "tran_cat_cd": SYNTHETIC_CAT_CD,
        "tran_source": SYNTHETIC_SOURCE,
        "tran_desc": SYNTHETIC_DAILY_DESC_TEMPLATE.format(recordIndex=recordIndex),
        "tran_amt": SYNTHETIC_DAILY_AMOUNT,
        "merchant_id": SYNTHETIC_MERCHANT_ID,
        "merchant_name": SYNTHETIC_MERCHANT_NAME,
        "merchant_city": SYNTHETIC_CITY,
        "merchant_zip": SYNTHETIC_ZIP,
        "card_num": cardNum,
        "orig_ts": SYNTHETIC_DAILY_ORIG_TS,
        "orig_ts_format": FORMAT_SPACE,
        "proc_ts": None,
        "proc_ts_format": FORMAT_BLANK,
    }


def WriteSyntheticDailyImage(path: Path, recordCount: int, cardNums: Sequence[str]) -> Path:
    """Write a DALYTRAN image of recordCount 350-byte code page 037 records with no terminators.

    Record i (0-based) has TRAN-ID 9 then i in 15 digits, type 01, category 0001, source POS TERM,
    description SYNTHETIC PURCHASE i, amount +1.00 (0000000010{), merchant 000000001, card
    cardNums[i % len(cardNums)], ORIG-TS 2022-06-10 19:27:53.000000 and a blank PROC-TS. The
    reconciliation oracle encodes the records, so no code under test builds its own input.
    """
    if not cardNums:
        raise ValueError(NO_CARDS_MESSAGE)
    with path.open("wb") as imageFile:
        for recordIndex in range(recordCount):
            values = SyntheticDailyValues(recordIndex, cardNums[recordIndex % len(cardNums)])
            recordText = legacy_oracle.FormatRecord(
                values, legacy_oracle.DAILY_FIELDS, legacy_oracle.TRANSACTION_LRECL
            )
            imageFile.write(legacy_oracle.EncodeCp037(recordText))
    return path


def SyntheticAccountId(index: int) -> str:
    """Return the 11-digit ACCT-ID of synthetic index: 9 then index in 10 digits."""
    return SyntheticKey(index, ACCOUNT_ID_DIGITS)


def SyntheticCustomerId(index: int) -> str:
    """Return the 9-digit CUST-ID of synthetic index: 9 then index in 8 digits."""
    return SyntheticKey(index, CUSTOMER_ID_DIGITS)


def SyntheticCardNum(index: int) -> str:
    """Return the 16-digit CARD-NUM of synthetic index: 9 then index in 15 digits."""
    return SyntheticKey(index, CARD_NUMBER_DIGITS)


def SyntheticAccountRow(index: int) -> dict[str, object]:
    """Return an active, unexpired accounts.accounts row with a zero balance and cycle."""
    return {
        "acct_id": SyntheticAccountId(index),
        "active_status": ACTIVE_STATUS,
        "curr_balance": ZERO_AMOUNT,
        "credit_limit": SYNTHETIC_CREDIT_LIMIT,
        "cash_credit_limit": SYNTHETIC_CASH_CREDIT_LIMIT,
        "open_date": SYNTHETIC_OPEN_DATE,
        "expiration_date": SYNTHETIC_EXPIRY_DATE,
        "reissue_date": SYNTHETIC_OPEN_DATE,
        "curr_cycle_credit": ZERO_AMOUNT,
        "curr_cycle_debit": ZERO_AMOUNT,
        "addr_zip": SYNTHETIC_ZIP,
        "group_id": "",
    }


def SyntheticCustomerRow(index: int) -> dict[str, object]:
    """Return a customers.customers row with non-blank names and a valid digit SSN."""
    return {
        "cust_id": SyntheticCustomerId(index),
        "first_name": SYNTHETIC_FIRST_NAME,
        "middle_name": SYNTHETIC_MIDDLE_NAME,
        "last_name": SYNTHETIC_LAST_NAME_TEMPLATE.format(index=index),
        "addr_line_1": SYNTHETIC_ADDRESS_TEMPLATE.format(index=index),
        "addr_line_2": "",
        "addr_line_3": SYNTHETIC_CITY,
        "addr_state_cd": SYNTHETIC_STATE_CD,
        "addr_country_cd": SYNTHETIC_COUNTRY_CD,
        "addr_zip": SYNTHETIC_ZIP,
        "ssn": SyntheticKey(index, SSN_DIGITS, SYNTHETIC_SSN_PREFIX),
        "date_of_birth": SYNTHETIC_BIRTH_DATE,
        "primary_holder_ind": PRIMARY_HOLDER,
        "fico_credit_score": SYNTHETIC_FICO_SCORE,
    }


def SyntheticCardRow(index: int) -> dict[str, object]:
    """Return an active cards.cards row of the synthetic account, expiring in 2025."""
    return {
        "card_num": SyntheticCardNum(index),
        "acct_id": SyntheticAccountId(index),
        "cvv_cd": f"{index % CVV_MODULUS:03d}",
        "embossed_name": SYNTHETIC_EMBOSSED_NAME_TEMPLATE.format(index=index),
        "expiration_date": SYNTHETIC_EXPIRY_DATE,
        "active_status": ACTIVE_STATUS,
    }


def SyntheticXrefRow(index: int) -> dict[str, object]:
    """Return the xref.card_xref row linking the synthetic card, customer and account."""
    return {
        "card_num": SyntheticCardNum(index),
        "cust_id": SyntheticCustomerId(index),
        "acct_id": SyntheticAccountId(index),
    }


# Each synthetic card's tables and row builders, parents first, as the foreign keys need.
SYNTHETIC_CARD_TABLES: tuple[tuple[Table, Callable[[int], dict[str, object]]], ...] = (
    (Account.__table__, SyntheticAccountRow),
    (Customer.__table__, SyntheticCustomerRow),
    (Card.__table__, SyntheticCardRow),
    (CardXref.__table__, SyntheticXrefRow),
)


def InsertSyntheticCards(engine: Engine, count: int) -> list[str]:
    """Insert count accounts, customers, cards and cross-references in one transaction.

    Synthetic index n (1 to count) gets account 9 then n in 10 digits, customer 9 then n in 8
    and card 9 then n in 15, outside the sample key ranges, with a 100,000.00 credit limit, a
    2025 expiry and a blank group (INTCALC's DEFAULT fallback). Returns the card numbers in
    index order.
    """
    if count < 1:
        raise ValueError(SYNTHETIC_COUNT_MESSAGE.format(count=count))
    indexes = range(1, count + 1)
    with engine.begin() as connection:
        for modelTable, rowBuilder in SYNTHETIC_CARD_TABLES:
            connection.execute(insert(modelTable), [rowBuilder(index) for index in indexes])
    return [SyntheticCardNum(index) for index in indexes]


def NextSyntheticTranNumber(connection: Connection) -> int:
    """Return the number after the highest synthetic TRAN-ID (8 then 15 digits), 1 when none."""
    tranId = Transaction.__table__.c.tran_id
    highestStatement = select(func.max(tranId)).where(tranId.startswith(SYNTHETIC_TRAN_ID_PREFIX))
    highestId = connection.execute(highestStatement).scalar_one()
    if highestId is None:
        return 1
    return int(highestId[len(SYNTHETIC_TRAN_ID_PREFIX) :]) + 1


def SyntheticTransactionRow(tranNumber: int, cardNum: str, amount: Decimal) -> dict[str, object]:
    """Return a posted transactions.transactions row: ORIG-TS format S, PROC-TS format D."""
    return {
        "tran_id": SyntheticKey(tranNumber, TRAN_ID_LENGTH, SYNTHETIC_TRAN_ID_PREFIX),
        "tran_type_cd": SYNTHETIC_TYPE_CD,
        "tran_cat_cd": SYNTHETIC_CAT_CD,
        "tran_source": SYNTHETIC_SOURCE,
        "tran_desc": SYNTHETIC_TRAN_DESC_TEMPLATE.format(tranNumber=tranNumber),
        "tran_amt": amount,
        "merchant_id": SYNTHETIC_MERCHANT_ID,
        "merchant_name": SYNTHETIC_MERCHANT_NAME,
        "merchant_city": SYNTHETIC_CITY,
        "merchant_zip": SYNTHETIC_ZIP,
        "card_num": cardNum,
        "orig_ts": SYNTHETIC_TRAN_TIMESTAMP,
        "orig_ts_format": FORMAT_SPACE,
        "proc_ts": SYNTHETIC_TRAN_TIMESTAMP,
        "proc_ts_format": FORMAT_DB2,
    }


def InsertSyntheticTransactions(engine: Engine, spec: SyntheticTransactionSpec) -> int:
    """Insert spec.perCard transactions of spec.amount for each card of spec.cardNums.

    The cards must exist. TRAN-IDs are 8 then 15 digits, continuing after the highest one already
    present, so repeated calls collide neither with each other nor with sample, daily or interest
    IDs. ORIG-TS and PROC-TS are 2022-07-17 10:00:00. Returns the number of rows inserted.
    """
    cardSequence = [cardNum for cardNum in spec.cardNums for copyIndex in range(spec.perCard)]
    with engine.begin() as connection:
        firstNumber = NextSyntheticTranNumber(connection)
        rows = [
            SyntheticTransactionRow(firstNumber + rowIndex, cardNum, spec.amount)
            for rowIndex, cardNum in enumerate(cardSequence)
        ]
        if rows:
            connection.execute(insert(Transaction.__table__), rows)
    return len(rows)
