"""Integration tests of the seed-cycle provenance that revisions 0002 and 0008 share.

Revision 0002 marks a completed sample seed with SEED_MARKER_COMMENT on the batch schema, and
revision 0008 records the seed as the OPEN cycle opened by SEED only when that marker is present
and accounts.accounts still holds rows. Populated tables alone, such as rows loaded while 0002
was skipped under APP_ENV=production, must never produce SEED loader history.

The revisions are loaded from their files. Each case runs them online against one connection of
the migrated test database, inside a transaction that first empties the batch ledger and is
rolled back at teardown, so the session database keeps its migrated state.
"""

import importlib.util
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import NamedTuple

import pytest
import sqlalchemy as sa
from sqlalchemy import Connection, Engine, Table

from app.core.paths import REPO_ROOT
from app.models import Base

VERSIONS_DIR = REPO_ROOT / "backend" / "alembic" / "versions"
SEED_REVISION_PATH = VERSIONS_DIR / "0002_seed_sample_data.py"
LEDGER_REVISION_PATH = VERSIONS_DIR / "0008_batch_job_run_ledger.py"
SEED_REVISION_MODULE_NAME = "provenance_revision_0002_seed_sample_data"
LEDGER_REVISION_MODULE_NAME = "provenance_revision_0008_batch_job_run_ledger"
MIGRATION_LOGGER_NAME = "alembic.runtime.migration"
PRODUCTION_APP_ENV = "production"

BATCH_SCHEMA = "batch"
CYCLES_TABLE = "batch.cycles"
JOB_RUNS_TABLE = "batch.job_runs"
CYCLE_STEPS_TABLE = "batch.cycle_steps"
# The batch ledger, child-first, so no delete leaves a row referring to a deleted one.
LEDGER_TABLES: tuple[str, ...] = (
    CYCLE_STEPS_TABLE,
    "batch.gdg_generations",
    JOB_RUNS_TABLE,
    CYCLES_TABLE,
)
ACCOUNTS_TABLE = "accounts.accounts"

# Static SQL only; the schema name is bound.
SCHEMA_COMMENT_SQL = sa.text(
    "SELECT pg_catalog.obj_description(n.oid, 'pg_namespace')"
    " FROM pg_catalog.pg_namespace AS n WHERE n.nspname = :schemaName"
)
CLEAR_SEED_MARKER_SQL = sa.text("COMMENT ON SCHEMA batch IS NULL")
DEFER_CONSTRAINTS_SQL = sa.text("SET CONSTRAINTS ALL DEFERRED")

# The seed cycle of AAP 0.6.7 and 0.6.8: the loader steps 0002 performed, with the job name and
# counts of each run. rows_loaded is the table's expected row count after alembic upgrade head;
# TRANFILE's only record is the priming record, which the seed skips.
EXPECTED_ACCOUNT_ROWS = 50
EXPECTED_SEED_RUNS: dict[int, tuple[str, dict[str, int]]] = {
    1: ("ACCTFILE", {"rows_loaded": EXPECTED_ACCOUNT_ROWS}),
    2: ("CARDFILE", {"rows_loaded": 50}),
    3: ("CUSTFILE", {"rows_loaded": 50}),
    4: ("XREFFILE", {"rows_loaded": 50}),
    5: ("TRANFILE", {"rows_loaded": 0, "records_skipped": 1}),
    7: ("TRANTYPE", {"rows_loaded": 7}),
    8: ("TRANCATG", {"rows_loaded": 18}),
    9: ("DISCGRP", {"rows_loaded": 51}),
    10: ("TCATBALF", {"rows_loaded": 50}),
    11: ("DUSRSECJ", {"rows_loaded": 10}),
    12: ("DALYTRAN", {"rows_loaded": 300}),
}
CHAIN_STEP_NUMBERS = range(1, 19)
SEED_CYCLE = ("OPEN", "SEED", None)
COMPLETED_RUN_STATUS = "COMPLETED"
COMPLETED_EXIT_CODE = 0
COMPLETED_STEP_STATE = "COMPLETED"
PENDING_STEP_STATE = "PENDING"

NO_MARKER_MESSAGE = (
    "seed cycle skipped: the batch schema has no seed marker, so revision 0002 loaded no"
    " sample data"
)
EMPTIED_SEED_MESSAGE = (
    "seed cycle skipped: revision 0002's seed marker is present but accounts.accounts holds no rows"
)


class ProvenanceHarness(NamedTuple):
    """Revisions 0002 and 0008, and the rolled-back connection whose batch ledger is empty."""

    seedRevision: ModuleType
    ledgerRevision: ModuleType
    connection: Connection
    monkeypatch: pytest.MonkeyPatch


class UnprovenSeedCase(NamedTuple):
    """A database state 0008 must not record as a seed, and the skip message it must log."""

    prepareDatabase: Callable[[ProvenanceHarness], None]
    expectedMessage: str


def LoadRevision(moduleName: str, revisionPath: Path) -> ModuleType:
    """Return the revision at revisionPath, loaded from its file; its name starts with a digit."""
    revisionSpec = importlib.util.spec_from_file_location(moduleName, revisionPath)
    revisionModule = importlib.util.module_from_spec(revisionSpec)
    revisionSpec.loader.exec_module(revisionModule)
    return revisionModule


def ModelTable(tableName: str) -> Table:
    """Return the model table named tableName, its schema included."""
    return Base.metadata.tables[tableName]


def SchemaComment(connection: Connection) -> str | None:
    """Return the comment of the batch schema, or None when it has none."""
    return connection.execute(SCHEMA_COMMENT_SQL, {"schemaName": BATCH_SCHEMA}).scalar_one()


def CountAccounts(connection: Connection) -> int:
    """Return the number of rows in accounts.accounts."""
    countStatement = sa.select(sa.func.count()).select_from(ModelTable(ACCOUNTS_TABLE))
    return connection.execute(countStatement).scalar_one()


def SelectRows(connection: Connection, tableName: str) -> list[sa.Row]:
    """Return every row of tableName in primary-key order."""
    table = ModelTable(tableName)
    return connection.execute(sa.select(table).order_by(*table.primary_key.columns)).all()


def StubSeedRevisionOnline(harness: ProvenanceHarness, appEnv: str) -> None:
    """Point revision 0002 at harness.connection, online, reading APP_ENV appEnv."""
    connection = harness.connection

    def ExecuteStatement(statement: str) -> None:
        connection.execute(sa.text(statement))

    opStub = SimpleNamespace(get_bind=lambda: connection, execute=ExecuteStatement)
    settingsStub = SimpleNamespace(APP_ENV=appEnv)
    harness.monkeypatch.setattr(harness.seedRevision, "op", opStub)
    harness.monkeypatch.setattr(harness.seedRevision, "MigrationSettings", lambda: settingsStub)
    contextStub = SimpleNamespace(is_offline_mode=lambda: False)
    harness.monkeypatch.setattr(harness.seedRevision, "context", contextStub)


def SkipSeedUnderProduction(harness: ProvenanceHarness) -> None:
    """Keep the seeded rows without a marker, as a database 0002 skipped and others filled.

    The marker of the session's own seed is cleared first; 0002's production upgrade must then
    load nothing and leave the batch schema unmarked.
    """
    harness.connection.execute(CLEAR_SEED_MARKER_SQL)
    StubSeedRevisionOnline(harness, PRODUCTION_APP_ENV)
    harness.seedRevision.upgrade()
    assert SchemaComment(harness.connection) is None
    assert CountAccounts(harness.connection) == EXPECTED_ACCOUNT_ROWS


def EmptyMarkedSeed(harness: ProvenanceHarness) -> None:
    """Keep the marker but empty accounts.accounts; its deferred references are never checked."""
    harness.connection.execute(DEFER_CONSTRAINTS_SQL)
    harness.connection.execute(sa.delete(ModelTable(ACCOUNTS_TABLE)))
    assert CountAccounts(harness.connection) == 0
    assert SchemaComment(harness.connection) == harness.seedRevision.SEED_MARKER_COMMENT


UNPROVEN_SEED_CASES = (
    pytest.param(
        UnprovenSeedCase(SkipSeedUnderProduction, NO_MARKER_MESSAGE),
        id="populated-tables-production-skipped-seed",
    ),
    pytest.param(UnprovenSeedCase(EmptyMarkedSeed, EMPTIED_SEED_MESSAGE), id="marked-seed-emptied"),
)


def ExpectedRunRows(cycleId: int) -> list[tuple[object, ...]]:
    """Return the expected job_runs rows of the seed cycle cycleId, in step order."""
    return [
        (cycleId, stepNo, jobName, COMPLETED_RUN_STATUS, COMPLETED_EXIT_CODE, counts)
        for stepNo, (jobName, counts) in EXPECTED_SEED_RUNS.items()
    ]


def ExpectedStepRows(cycleId: int, runIdsByStep: dict[int, int]) -> list[tuple[object, ...]]:
    """Return the 18 expected cycle_steps rows: seeded steps COMPLETED by their run, else PENDING.

    runIdsByStep maps each seeded step to the run_id of its job_runs row.
    """
    return [
        (cycleId, stepNo, COMPLETED_STEP_STATE, runIdsByStep[stepNo])
        if stepNo in EXPECTED_SEED_RUNS
        else (cycleId, stepNo, PENDING_STEP_STATE, None)
        for stepNo in CHAIN_STEP_NUMBERS
    ]


@pytest.fixture(name="seedRevision", scope="module")
def SeedRevision() -> ModuleType:
    """Return revision 0002 loaded from its file."""
    return LoadRevision(SEED_REVISION_MODULE_NAME, SEED_REVISION_PATH)


@pytest.fixture(name="ledgerRevision", scope="module")
def LedgerRevision() -> ModuleType:
    """Return revision 0008 loaded from its file."""
    return LoadRevision(LEDGER_REVISION_MODULE_NAME, LEDGER_REVISION_PATH)


@pytest.fixture(name="ledgerConnection")
def LedgerConnection(syncEngine: Engine, migratedDatabase: object) -> Iterator[Connection]:
    """Yield a connection in a transaction that emptied the batch ledger; roll it back after."""
    with syncEngine.connect() as connection:
        transaction = connection.begin()
        for tableName in LEDGER_TABLES:
            connection.execute(sa.delete(ModelTable(tableName)))
        yield connection
        transaction.rollback()


@pytest.fixture(name="provenanceHarness")
def ProvenanceHarnessFixture(
    seedRevision: ModuleType,
    ledgerRevision: ModuleType,
    ledgerConnection: Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> ProvenanceHarness:
    """Return the harness, with revision 0008 running online on ledgerConnection."""
    monkeypatch.setattr(ledgerRevision, "context", SimpleNamespace(is_offline_mode=lambda: False))
    monkeypatch.setattr(ledgerRevision, "op", SimpleNamespace(get_bind=lambda: ledgerConnection))
    return ProvenanceHarness(seedRevision, ledgerRevision, ledgerConnection, monkeypatch)


@pytest.mark.parametrize("unprovenSeedCase", UNPROVEN_SEED_CASES)
def test_unproven_seed_records_no_cycle_runs_or_steps(provenanceHarness, unprovenSeedCase, caplog):
    unprovenSeedCase.prepareDatabase(provenanceHarness)
    caplog.set_level(logging.INFO, logger=MIGRATION_LOGGER_NAME)
    provenanceHarness.ledgerRevision.RecordSeedCycle()
    for tableName in (CYCLES_TABLE, JOB_RUNS_TABLE, CYCLE_STEPS_TABLE):
        assert SelectRows(provenanceHarness.connection, tableName) == []
    ledgerMessages = [
        logRecord.getMessage()
        for logRecord in caplog.records
        if logRecord.getMessage().startswith("seed cycle")
    ]
    assert ledgerMessages == [unprovenSeedCase.expectedMessage]


def test_marked_seed_records_one_open_seed_cycle_with_its_loader_history(provenanceHarness):
    connection = provenanceHarness.connection
    provenanceHarness.ledgerRevision.RecordSeedCycle()
    cycleRows = SelectRows(connection, CYCLES_TABLE)
    assert [(row.status, row.opened_by, row.closed_at) for row in cycleRows] == [SEED_CYCLE]
    cycleId = cycleRows[0].cycle_id
    runRows = sorted(SelectRows(connection, JOB_RUNS_TABLE), key=lambda row: row.step_no)
    assert [
        (row.cycle_id, row.step_no, row.job_name, row.status, row.exit_code, row.counts)
        for row in runRows
    ] == ExpectedRunRows(cycleId)
    runIdsByStep = {row.step_no: row.run_id for row in runRows}
    stepRows = SelectRows(connection, CYCLE_STEPS_TABLE)
    assert [
        (row.cycle_id, row.step_no, row.state, row.last_run_id) for row in stepRows
    ] == ExpectedStepRows(cycleId, runIdsByStep)


def test_migrated_database_carries_the_seed_marker_and_the_seed_cycle(
    seedRevision, ledgerRevision, syncEngine, migratedDatabase
):
    # 0008 holds a frozen copy of 0002's marker, since a revision imports neither app nor batch.
    assert ledgerRevision.SEED_MARKER_COMMENT == seedRevision.SEED_MARKER_COMMENT
    with syncEngine.connect() as connection:
        assert SchemaComment(connection) == seedRevision.SEED_MARKER_COMMENT
    migratedRows = migratedDatabase.rowsByTable
    cycleRows = migratedRows[CYCLES_TABLE]
    assert [(row["status"], row["opened_by"]) for row in cycleRows] == [SEED_CYCLE[:2]]
    assert len(migratedRows[JOB_RUNS_TABLE]) == len(EXPECTED_SEED_RUNS)
    assert len(migratedRows[CYCLE_STEPS_TABLE]) == len(CHAIN_STEP_NUMBERS)
