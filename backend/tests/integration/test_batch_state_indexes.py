"""Partial state indexes of the batch ledger and GDG catalog, and the query plans that use them.

Replaces: no legacy test. It tests the indexes that revisions 0008_batch_job_run_ledger and
0009_batch_gdg_catalog create and app.models.support_tables (CYCLES, JOB_RUNS, GDG_GENERATIONS)
declares, for the run state that replaces CA-7/Control-M job tracking (scheduler/CardDemo.ca7,
scheduler/CardDemo.controlm) and the catalog that replaces the GDG bases of catlg/LISTCAT.txt.

Every command looks up the OPEN cycle, the RUNNING runs and the COMMITTED and LOST catalog rows,
and resolution and retention read a base's PUBLISHED generations newest first, while finished
cycles, runs and rolled-off or abandoned generations stay as history. The tests check that:

- each partial index exists on the migrated database, valid and not unique, with the model's
  key columns and predicate. ``alembic check`` does not compare index predicates, so this is the
  only parity check of them;
- the model declares each index with the expected name, key columns and predicate;
- over thousands of history rows that never match, PostgreSQL plans each lookup of
  batch/orchestration/run_ledger.py and batch/orchestration/gdg.py through its partial index.

The history is inserted, and the tables analyzed, inside one transaction that is rolled back, so
no row outlives a test. Sequential scans are disabled in that transaction only, so the planner
chooses among index paths and the plans do not depend on table sizes elsewhere in the session.
"""

import functools
import re
from collections.abc import Callable, Iterator
from typing import NamedTuple

import pytest
from batch.orchestration import gdg, run_ledger
from sqlalchemy import Connection, Engine, Index, Table, event, func, insert, select, text
from sqlalchemy.sql import ClauseElement

from app.models import CYCLES, GDG_GENERATIONS, JOB_RUNS

BATCH_SCHEMA = "batch"
POSTGRESQL_DIALECT = "postgresql"
WHERE_OPTION = "where"

# Static SQL with identifiers only; every value is bound.
INDEX_DEFINITION_SQL = text(
    """
    SELECT index_row.indisunique AS is_unique,
        index_row.indisvalid AS is_valid,
        pg_get_expr(index_row.indpred, index_row.indrelid) AS predicate,
        ARRAY(
            SELECT pg_get_indexdef(index_row.indexrelid, key_position, TRUE)
            FROM generate_series(1, index_row.indnkeyatts) AS key_position
            ORDER BY key_position
        ) AS column_names
    FROM pg_catalog.pg_index AS index_row
    JOIN pg_catalog.pg_class AS index_class ON index_class.oid = index_row.indexrelid
    JOIN pg_catalog.pg_class AS table_class ON table_class.oid = index_row.indrelid
    JOIN pg_catalog.pg_namespace AS table_schema ON table_schema.oid = table_class.relnamespace
    WHERE table_schema.nspname = :schemaName
        AND table_class.relname = :tableName
        AND index_class.relname = :indexName
    """
)
ANALYZE_BATCH_TABLES_SQL = text("ANALYZE batch.cycles, batch.job_runs, batch.gdg_generations")
EXPLAIN_PREFIX = "EXPLAIN (FORMAT JSON) "
SEQUENTIAL_SCAN_SETTING = "enable_seqscan"
SETTING_OFF = "off"
PLAN_KEY = "Plan"
CHILD_PLANS_KEY = "Plans"
INDEX_NAME_KEY = "Index Name"

# PostgreSQL renders a stored predicate with casts and grouping, for example
# ((status)::text = 'OPEN'::text); both sides are compared without them.
CAST_PATTERN = re.compile(r"::\w+")
GROUPING_CHARACTERS = str.maketrans("", "", "()")

# History that no state lookup selects: closed cycles with two finished runs each, and two
# bases' rolled-off and abandoned generations.
HISTORY_CYCLE_COUNT = 2000
HISTORY_STEP_NUMBERS = (1, 2)
HISTORY_JOB_NAME = "ACCTFILE"
HISTORY_GENERATION_COUNT = 3000
# The rows the lookups select: one OPEN cycle with two RUNNING runs, ten PUBLISHED generations
# (beyond LIMIT 5), three COMMITTED and two LOST.
ACTIVE_STEP_NUMBERS = (13, 14)
ACTIVE_JOB_NAME = "POSTTRAN"
PUBLISHED_GENERATION_COUNT = 10
GENERATION_FILE_PATH = "gdg/G0000V00.dat"
GENERATION_SHA256 = "0" * 64


class StateIndexSpec(NamedTuple):
    """One expected partial index: its model table, name, key columns and predicate."""

    table: Table
    indexName: str
    columnNames: tuple[str, ...]
    predicate: str


STATE_INDEX_SPECS: tuple[StateIndexSpec, ...] = (
    StateIndexSpec(CYCLES, "ix_cycles_cycle_id_open", ("cycle_id",), "status = 'OPEN'"),
    StateIndexSpec(JOB_RUNS, "ix_job_runs_run_id_running", ("run_id",), "status = 'RUNNING'"),
    StateIndexSpec(
        GDG_GENERATIONS, "ix_gdg_generations_run_id_committed", ("run_id",), "state = 'COMMITTED'"
    ),
    StateIndexSpec(
        GDG_GENERATIONS, "ix_gdg_generations_run_id_lost", ("run_id",), "state = 'LOST'"
    ),
    StateIndexSpec(
        GDG_GENERATIONS,
        "ix_gdg_generations_base_name_generation_published",
        ("base_name", "generation"),
        "state = 'PUBLISHED'",
    ),
)
STATE_INDEX_IDS = [indexSpec.indexName for indexSpec in STATE_INDEX_SPECS]


class GenerationBatch(NamedTuple):
    """Catalog rows of one base in one state, numbered firstGeneration to lastGeneration."""

    base: gdg.GdgBase
    firstGeneration: int
    lastGeneration: int
    state: str


# Produced by a finished run of a closed cycle: the history, then SYSTRAN's newest generations.
CLOSED_RUN_GENERATIONS: tuple[GenerationBatch, ...] = (
    GenerationBatch(gdg.SYSTRAN, 1, HISTORY_GENERATION_COUNT, run_ledger.GDG_ROLLED_OFF),
    GenerationBatch(gdg.DALYREJS, 1, HISTORY_GENERATION_COUNT, run_ledger.GDG_ABANDONED),
    GenerationBatch(
        gdg.SYSTRAN,
        HISTORY_GENERATION_COUNT + 1,
        HISTORY_GENERATION_COUNT + PUBLISHED_GENERATION_COUNT,
        run_ledger.GDG_PUBLISHED,
    ),
)
# Produced by a RUNNING run of the OPEN cycle.
ACTIVE_RUN_GENERATIONS: tuple[GenerationBatch, ...] = (
    GenerationBatch(gdg.TRANSACT_COMBINED, 1, 3, run_ledger.GDG_COMMITTED),
    GenerationBatch(gdg.TRANSACT_BKUP, 1, 2, run_ledger.GDG_LOST),
)


class StateHistory(NamedTuple):
    """The connection whose open transaction holds the history, and the ids lookups select by."""

    connection: Connection
    openCycleId: int
    activeRunId: int


def NormalizedPredicate(predicateText: str) -> str:
    """Return a predicate without casts, grouping parentheses or repeated spaces."""
    withoutCasts = CAST_PATTERN.sub("", predicateText).translate(GROUPING_CHARACTERS)
    return " ".join(withoutCasts.split())


def ModelIndex(indexSpec: StateIndexSpec) -> Index:
    """Return the Index the model table declares under the spec's name; fail when it has none."""
    modelIndexes = {index.name: index for index in indexSpec.table.indexes}
    if indexSpec.indexName not in modelIndexes:
        pytest.fail(f"{indexSpec.table.fullname} declares no index {indexSpec.indexName}")
    return modelIndexes[indexSpec.indexName]


def ModelPredicate(modelIndex: Index) -> str:
    """Return the SQL text of a model index's postgresql_where predicate, or "" without one."""
    predicate = modelIndex.dialect_options[POSTGRESQL_DIALECT][WHERE_OPTION]
    return "" if predicate is None else str(predicate)


def InsertClosedHistory(connection: Connection) -> int:
    """Insert COMPLETED cycles, each with COMPLETED runs, and return one history run's id."""
    cycleRows = [
        {"status": run_ledger.CYCLE_COMPLETED, "opened_by": run_ledger.OPENED_BY_RUN_ALL}
        for _ in range(HISTORY_CYCLE_COUNT)
    ]
    cycleStatement = insert(CYCLES).returning(CYCLES.c.cycle_id)
    cycleIds = connection.execute(cycleStatement, cycleRows).scalars().all()
    runRows = [
        {
            "cycle_id": cycleId,
            "step_no": stepNo,
            "job_name": HISTORY_JOB_NAME,
            "status": run_ledger.RUN_COMPLETED,
        }
        for cycleId in cycleIds
        for stepNo in HISTORY_STEP_NUMBERS
    ]
    runStatement = insert(JOB_RUNS).returning(JOB_RUNS.c.run_id)
    return connection.execute(runStatement, runRows).scalars().first()


def InsertActiveLedger(connection: Connection) -> tuple[int, int]:
    """Insert an OPEN cycle with RUNNING runs; return the cycle's id and its first run's id."""
    cycleStatement = (
        insert(CYCLES)
        .values(status=run_ledger.CYCLE_OPEN, opened_by=run_ledger.OPENED_BY_RUN_ALL)
        .returning(CYCLES.c.cycle_id)
    )
    openCycleId = connection.execute(cycleStatement).scalar_one()
    runRows = [
        {
            "cycle_id": openCycleId,
            "step_no": stepNo,
            "job_name": ACTIVE_JOB_NAME,
            "status": run_ledger.RUN_RUNNING,
        }
        for stepNo in ACTIVE_STEP_NUMBERS
    ]
    runStatement = insert(JOB_RUNS).returning(JOB_RUNS.c.run_id)
    return openCycleId, connection.execute(runStatement, runRows).scalars().first()


def GenerationRows(batches: tuple[GenerationBatch, ...], runId: int) -> list[dict[str, object]]:
    """Return the catalog rows of every batch, each produced by the run runId."""
    return [
        {
            "base_name": batch.base.name,
            "generation": generation,
            "file_path": GENERATION_FILE_PATH,
            "record_count": 0,
            "sha256": GENERATION_SHA256,
            "state": batch.state,
            "run_id": runId,
        }
        for batch in batches
        for generation in range(batch.firstGeneration, batch.lastGeneration + 1)
    ]


def InsertStateHistory(connection: Connection) -> StateHistory:
    """Insert the history and the active rows, analyze the tables and disable sequential scans."""
    historyRunId = InsertClosedHistory(connection)
    openCycleId, activeRunId = InsertActiveLedger(connection)
    generationRows = GenerationRows(CLOSED_RUN_GENERATIONS, historyRunId) + GenerationRows(
        ACTIVE_RUN_GENERATIONS, activeRunId
    )
    connection.execute(insert(GDG_GENERATIONS), generationRows)
    connection.execute(ANALYZE_BATCH_TABLES_SQL)
    connection.execute(select(func.set_config(SEQUENTIAL_SCAN_SETTING, SETTING_OFF, True)))
    return StateHistory(connection, openCycleId, activeRunId)


def ExecutedStatement(
    connection: Connection, action: Callable[[Connection], object]
) -> ClauseElement:
    """Run action on connection and return the one statement it executed."""
    executedStatements: list[ClauseElement] = []

    def RecordStatement(**eventArguments: object) -> None:
        executedStatements.append(eventArguments["clauseelement"])

    event.listen(connection, "before_execute", RecordStatement, named=True)
    try:
        action(connection)
    finally:
        event.remove(connection, "before_execute", RecordStatement)
    assert len(executedStatements) == 1, executedStatements
    return executedStatements[0]


def PlanIndexNames(planNode: dict[str, object]) -> set[str]:
    """Return the index names of a JSON plan node and of every node beneath it."""
    indexNames = {planNode[INDEX_NAME_KEY]} if INDEX_NAME_KEY in planNode else set()
    for childNode in planNode.get(CHILD_PLANS_KEY, []):
        indexNames |= PlanIndexNames(childNode)
    return indexNames


def PlannedIndexNames(connection: Connection, statement: ClauseElement) -> set[str]:
    """Return every index the plan of statement scans, explained with its bound parameters."""
    compiled = statement.compile(
        dialect=connection.dialect, compile_kwargs={"render_postcompile": True}
    )
    explainResult = connection.exec_driver_sql(EXPLAIN_PREFIX + str(compiled), compiled.params)
    return PlanIndexNames(explainResult.scalar_one()[0][PLAN_KEY])


def RunningRunsStatement(history: StateHistory) -> ClauseElement:
    """ClassifyInterruptedRuns' query of the RUNNING runs."""
    return run_ledger.RUNNING_ROWS_STATEMENT


def OpenCycleStatement(history: StateHistory) -> ClauseElement:
    """The query FindOpenCycle executes for the newest OPEN cycle."""
    return ExecutedStatement(history.connection, run_ledger.FindOpenCycle)


def CommittedStatement(history: StateHistory) -> ClauseElement:
    """ReconcileCatalog's query of every COMMITTED generation."""
    return gdg.CommittedRowsStatement()


def RunCommittedStatement(history: StateHistory) -> ClauseElement:
    """PublishRunGenerations' query of one run's COMMITTED generations."""
    return gdg.CommittedRowsStatement().where(GDG_GENERATIONS.c.run_id == history.activeRunId)


def LostStatement(history: StateHistory) -> ClauseElement:
    """The query of the LOST generations of the OPEN cycle's runs, every base."""
    return gdg.LostRowsStatement(history.openCycleId, None)


def AbandonLostStatement(history: StateHistory) -> ClauseElement:
    """The update AbandonLostGenerations executes for the OPEN cycle's LOST generations."""
    abandonLost = functools.partial(
        run_ledger.AbandonLostGenerations, cycleIds=[history.openCycleId]
    )
    return ExecutedStatement(history.connection, abandonLost)


def RetentionStatement(history: StateHistory) -> ClauseElement:
    """RollOff's query of a base's PUBLISHED generations beyond LIMIT 5."""
    return gdg.PublishedNewestFirstStatement(gdg.SYSTRAN).offset(gdg.GDG_LIMIT)


LOOKUP_PLAN_CASES = (
    pytest.param(RunningRunsStatement, "ix_job_runs_run_id_running", id="running-runs"),
    pytest.param(OpenCycleStatement, "ix_cycles_cycle_id_open", id="open-cycle"),
    pytest.param(CommittedStatement, "ix_gdg_generations_run_id_committed", id="committed"),
    pytest.param(
        RunCommittedStatement, "ix_gdg_generations_run_id_committed", id="committed-of-run"
    ),
    pytest.param(LostStatement, "ix_gdg_generations_run_id_lost", id="lost-of-cycle"),
    pytest.param(AbandonLostStatement, "ix_gdg_generations_run_id_lost", id="abandon-lost"),
    pytest.param(
        RetentionStatement,
        "ix_gdg_generations_base_name_generation_published",
        id="published-beyond-limit",
    ),
)


@pytest.fixture(name="stateHistory")
def StateHistoryFixture(syncEngine: Engine, migratedDatabase: object) -> Iterator[StateHistory]:
    """Yield the analyzed history inside one transaction, rolled back at teardown."""
    with syncEngine.connect() as connection:
        transaction = connection.begin()
        try:
            yield InsertStateHistory(connection)
        finally:
            transaction.rollback()


@pytest.mark.parametrize("indexSpec", STATE_INDEX_SPECS, ids=STATE_INDEX_IDS)
def test_state_index_is_migrated_as_modelled(
    syncEngine: Engine, migratedDatabase: object, indexSpec: StateIndexSpec
) -> None:
    """The migrated index is valid and not unique, with the model's columns and predicate."""
    modelIndex = ModelIndex(indexSpec)
    with syncEngine.connect() as connection:
        definition = connection.execute(
            INDEX_DEFINITION_SQL,
            {
                "schemaName": BATCH_SCHEMA,
                "tableName": indexSpec.table.name,
                "indexName": indexSpec.indexName,
            },
        ).one_or_none()
    assert definition is not None, f"{indexSpec.indexName} is not on the migrated database"
    assert definition.is_unique is False
    assert definition.is_valid is True
    assert tuple(definition.column_names) == tuple(column.name for column in modelIndex.columns)
    assert NormalizedPredicate(definition.predicate) == NormalizedPredicate(
        ModelPredicate(modelIndex)
    )


@pytest.mark.parametrize("indexSpec", STATE_INDEX_SPECS, ids=STATE_INDEX_IDS)
def test_state_index_is_declared_on_model(indexSpec: StateIndexSpec) -> None:
    """The model table declares the index, not unique, with the expected columns and predicate."""
    modelIndex = ModelIndex(indexSpec)
    assert modelIndex.unique is False
    assert tuple(column.name for column in modelIndex.columns) == indexSpec.columnNames
    assert NormalizedPredicate(ModelPredicate(modelIndex)) == NormalizedPredicate(
        indexSpec.predicate
    )


@pytest.mark.parametrize(("buildStatement", "expectedIndexName"), LOOKUP_PLAN_CASES)
def test_state_lookup_is_planned_through_its_partial_index(
    stateHistory: StateHistory,
    buildStatement: Callable[[StateHistory], ClauseElement],
    expectedIndexName: str,
) -> None:
    """Over thousands of history rows that never match, the lookup scans its partial index."""
    statement = buildStatement(stateHistory)
    plannedIndexNames = PlannedIndexNames(stateHistory.connection, statement)
    assert expectedIndexName in plannedIndexNames, plannedIndexNames
