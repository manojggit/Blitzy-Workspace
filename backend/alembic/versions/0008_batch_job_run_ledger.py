"""Batch cycle, step and job-run ledger.

Replaces: CA-7 and Control-M job tracking and condition codes (scheduler/CardDemo.ca7,
    scheduler/CardDemo.controlm) and the JES job history of the CardDemo batch chain.

- ``batch.cycles``: one row per pass of the 18-step chain, OPEN until step 18 completes. The
  partial index ix_cycles_cycle_id_open covers only OPEN rows, so finding or abandoning the
  OPEN cycle reads no closed history.
- ``batch.job_runs``: run history, one row per job execution with its status, exit code and
  counts. Any number of rows may share a (cycle_id, step_no), so ix_job_runs_cycle_id_step_no
  is deliberately not unique. The partial index ix_job_runs_run_id_running covers only RUNNING
  rows, so classifying interrupted runs at the start of every command reads no finished runs.
  Neither partial index is unique.
- ``batch.cycle_steps``: the authoritative state of each (cycle_id, step_no) identity and its
  last run. The two TRANBKP occurrences are steps 6 and 15, so each has its own identity.

Every name, type, default, check condition, foreign-key option and table comment equals
app.models.support_tables, so ``alembic check`` finds nothing to change. The revision imports
nothing from app or batch, so it keeps creating exactly these tables as the models evolve.

When revision 0002 seeded the dataset tables, the seed is recorded as the first cycle: OPEN,
opened by SEED, with one COMPLETED run for each seeded loader (steps 1-5 and 7-12) and steps 6
and 13-18 PENDING, so ``carddemo-batch run-all`` resumes at the first TRANBKP. Revision 0002
proves its seed by the marker it leaves as the batch schema's comment in the seed's own
transaction; rows alone prove nothing, since tables can be filled without it. The cycle is
recorded only when that marker is present and accounts.accounts still holds rows. Under
APP_ENV=production, where 0002 loads nothing and leaves no marker whatever the tables hold, and
in offline mode (``alembic upgrade --sql``), where nothing can be read, the three tables are
created empty.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-03 00:10:00.000000
"""

import logging
from collections.abc import Mapping, Sequence
from typing import NamedTuple

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import context, op

# Revision identifiers, used by Alembic. Alembic looks these names, and upgrade and downgrade,
# up exactly as spelt, so they keep them instead of the MK_Modernization Rule's naming clause.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic's own migration logger, which alembic.ini shows at INFO, as revision 0002 uses.
LOGGER = logging.getLogger("alembic.runtime.migration")

BATCH_SCHEMA = "batch"
CYCLES_TABLE = "cycles"
JOB_RUNS_TABLE = "job_runs"
CYCLE_STEPS_TABLE = "cycle_steps"
JOB_RUNS_STEP_INDEX = "ix_job_runs_cycle_id_step_no"

# Partial indexes of the active states every command looks up, with the exact predicate texts of
# app.models.support_tables. Each predicate is a complete static SQL literal, never formatted from
# variables; each name passes through op.f(), so the naming convention keeps it as written.
OPEN_CYCLE_INDEX = "ix_cycles_cycle_id_open"
OPEN_CYCLE_PREDICATE = "status = 'OPEN'"
RUNNING_RUN_INDEX = "ix_job_runs_run_id_running"
RUNNING_RUN_PREDICATE = "status = 'RUNNING'"

CYCLES_COMMENT = "One pass of the 18-step batch chain; replaces scheduler cycle state"
JOB_RUNS_COMMENT = "Batch run history, one row per job execution; replaces job condition codes"
CYCLE_STEPS_COMMENT = "Authoritative state of each chain step within a batch cycle"

# Frozen copies of the value domains in app.models.support_tables. Each check condition below
# lists its domain's values in this order.
CYCLE_STATUSES: tuple[str, ...] = ("OPEN", "COMPLETED", "ABANDONED")
CYCLE_OPENERS: tuple[str, ...] = ("SEED", "seed-all", "run-all")
RUN_STATUSES: tuple[str, ...] = ("RUNNING", "COMPLETED", "FAILED", "SKIPPED", "INTERRUPTED")
STEP_STATES: tuple[str, ...] = ("PENDING", "COMPLETED", "FAILED", "INTERRUPTED")

# Frozen copy of app.models.base.FOREIGN_KEY_OPTIONS.
FOREIGN_KEY_OPTIONS: dict[str, object] = {"deferrable": True, "initially": "IMMEDIATE"}

# Server-default SQL texts, matching the models' server_default values.
NOW_DEFAULT = "now()"
EMPTY_JSONB_DEFAULT = "'{}'::jsonb"


class ColumnSpec(NamedTuple):
    """One column: name, SQL type, nullability, server default SQL text and autoincrement.

    Only cycles.cycle_id and job_runs.run_id autoincrement, which renders them BIGSERIAL.
    """

    name: str
    columnType: sa.types.TypeEngine
    nullable: bool = False
    serverDefault: str | None = None
    autoincrement: bool = False


class ValueCheckSpec(NamedTuple):
    """One enumerated-value check: its convention name and its static SQL condition."""

    name: str
    condition: str


class ForeignKeySpec(NamedTuple):
    """One single-column foreign key: its convention name, local column and referenced column."""

    name: str
    localColumn: str
    referencedColumn: str


CYCLES_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("cycle_id", sa.BigInteger(), autoincrement=True),
    ColumnSpec("status", sa.String(16)),
    ColumnSpec("opened_by", sa.String(16)),
    ColumnSpec("opened_at", sa.DateTime(timezone=True), serverDefault=NOW_DEFAULT),
    ColumnSpec("closed_at", sa.DateTime(timezone=True), nullable=True),
)

# cycle_id and step_no are NULL on the run of a standalone load made with no OPEN cycle.
JOB_RUNS_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("run_id", sa.BigInteger(), autoincrement=True),
    ColumnSpec("cycle_id", sa.BigInteger(), nullable=True),
    ColumnSpec("step_no", sa.SmallInteger(), nullable=True),
    ColumnSpec("job_name", sa.String(32)),
    ColumnSpec("params", postgresql.JSONB(), serverDefault=EMPTY_JSONB_DEFAULT),
    ColumnSpec("status", sa.String(16)),
    ColumnSpec("exit_code", sa.SmallInteger(), nullable=True),
    ColumnSpec("counts", postgresql.JSONB(), serverDefault=EMPTY_JSONB_DEFAULT),
    ColumnSpec("started_at", sa.DateTime(timezone=True), serverDefault=NOW_DEFAULT),
    ColumnSpec("finished_at", sa.DateTime(timezone=True), nullable=True),
)

CYCLE_STEPS_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("cycle_id", sa.BigInteger()),
    ColumnSpec("step_no", sa.SmallInteger()),
    ColumnSpec("state", sa.String(16)),
    ColumnSpec("last_run_id", sa.BigInteger(), nullable=True),
)

# Each condition is a complete static SQL literal, never formatted from variables, with the
# exact text app.models.base.ValueCheck renders; each name is the naming convention's output.
CYCLES_CHECKS: tuple[ValueCheckSpec, ...] = (
    ValueCheckSpec("ck_cycles_status", "status IN ('OPEN', 'COMPLETED', 'ABANDONED')"),
    ValueCheckSpec("ck_cycles_opened_by", "opened_by IN ('SEED', 'seed-all', 'run-all')"),
)
JOB_RUNS_CHECKS: tuple[ValueCheckSpec, ...] = (
    ValueCheckSpec(
        "ck_job_runs_status",
        "status IN ('RUNNING', 'COMPLETED', 'FAILED', 'SKIPPED', 'INTERRUPTED')",
    ),
)
CYCLE_STEPS_CHECKS: tuple[ValueCheckSpec, ...] = (
    ValueCheckSpec(
        "ck_cycle_steps_state", "state IN ('PENDING', 'COMPLETED', 'FAILED', 'INTERRUPTED')"
    ),
)

JOB_RUNS_FOREIGN_KEYS: tuple[ForeignKeySpec, ...] = (
    ForeignKeySpec("fk_job_runs_cycle_id_cycles", "cycle_id", "batch.cycles.cycle_id"),
)
CYCLE_STEPS_FOREIGN_KEYS: tuple[ForeignKeySpec, ...] = (
    ForeignKeySpec("fk_cycle_steps_cycle_id_cycles", "cycle_id", "batch.cycles.cycle_id"),
    ForeignKeySpec("fk_cycle_steps_last_run_id_job_runs", "last_run_id", "batch.job_runs.run_id"),
)

# The seed cycle. The step identity is (cycle_id, step_no); job_name is informational and holds
# the legacy JCL member name.
SEED_CYCLE_STATUS = "OPEN"
SEED_CYCLE_OPENER = "SEED"
SEED_RUN_STATUS = "COMPLETED"
SEED_RUN_EXIT_CODE = 0
SEED_RUN_PARAMS: dict[str, str] = {"source": "alembic:0002", "encoding": "ebcdic"}
COMPLETED_STEP_STATE = "COMPLETED"
PENDING_STEP_STATE = "PENDING"
ROWS_LOADED_COUNT = "rows_loaded"

# The 18 chain steps; the cycle_steps rows of a cycle are created when it opens.
CHAIN_STEP_NUMBERS = range(1, 19)

# Frozen copy of revision 0002's SEED_MARKER_COMMENT, which 0002 sets as the batch schema's
# comment in the transaction that loads the seed, and only then. The query is static and bound.
SEED_MARKER_COMMENT = "CardDemo sample seed loaded by Alembic revision 0002"
SCHEMA_COMMENT_QUERY = sa.text(
    "SELECT pg_catalog.obj_description(n.oid, 'pg_namespace')"
    " FROM pg_catalog.pg_namespace AS n WHERE n.nspname = :schemaName"
)

# The marker proves 0002 loaded the seed; the probe proves the seed is still there. Revision 0002
# loads every dataset table in one atomic transaction, so a populated accounts.accounts under the
# marker means the whole seed is present.
SEED_PROBE_SCHEMA = "accounts"
SEED_PROBE_TABLE = "accounts"
NO_SEED_MARKER_REASON = (
    "the batch schema has no seed marker, so revision 0002 loaded no sample data"
)
EMPTIED_SEED_REASON = "revision 0002's seed marker is present but accounts.accounts holds no rows"


class SeededLoaderStep(NamedTuple):
    """A chain loader step that revision 0002 performed, and the table its rows_loaded counts."""

    stepNo: int
    jobName: str
    schema: str
    table: str
    extraCounts: Mapping[str, int]


# Steps 1-5 and 7-12 of the chain. TRANFILE's only record is the DALYTRAN.PS.INIT priming
# record, which the seed skips, so it loads 0 rows and skips 1.
SEEDED_LOADER_STEPS: tuple[SeededLoaderStep, ...] = (
    SeededLoaderStep(1, "ACCTFILE", "accounts", "accounts", {}),
    SeededLoaderStep(2, "CARDFILE", "cards", "cards", {}),
    SeededLoaderStep(3, "CUSTFILE", "customers", "customers", {}),
    SeededLoaderStep(4, "XREFFILE", "xref", "card_xref", {}),
    SeededLoaderStep(5, "TRANFILE", "transactions", "transactions", {"records_skipped": 1}),
    SeededLoaderStep(7, "TRANTYPE", "reference", "transaction_types", {}),
    SeededLoaderStep(8, "TRANCATG", "reference", "transaction_categories", {}),
    SeededLoaderStep(9, "DISCGRP", "disclosure", "disclosure_groups", {}),
    SeededLoaderStep(10, "TCATBALF", "transactions", "transaction_category_balances", {}),
    SeededLoaderStep(11, "DUSRSECJ", "security", "users", {}),
    SeededLoaderStep(12, "DALYTRAN", "transactions", "daily_transactions", {}),
)

# Lightweight table stubs for the seed-cycle inserts. The JSONB columns are typed so that the
# params and counts dictionaries are serialized as JSON.
CYCLES_LEDGER = sa.table(
    CYCLES_TABLE,
    sa.column("cycle_id", sa.BigInteger()),
    sa.column("status", sa.String()),
    sa.column("opened_by", sa.String()),
    schema=BATCH_SCHEMA,
)
JOB_RUNS_LEDGER = sa.table(
    JOB_RUNS_TABLE,
    sa.column("run_id", sa.BigInteger()),
    sa.column("cycle_id", sa.BigInteger()),
    sa.column("step_no", sa.SmallInteger()),
    sa.column("job_name", sa.String()),
    sa.column("params", postgresql.JSONB()),
    sa.column("status", sa.String()),
    sa.column("exit_code", sa.SmallInteger()),
    sa.column("counts", postgresql.JSONB()),
    sa.column("started_at", sa.DateTime(timezone=True)),
    sa.column("finished_at", sa.DateTime(timezone=True)),
    schema=BATCH_SCHEMA,
)
CYCLE_STEPS_LEDGER = sa.table(
    CYCLE_STEPS_TABLE,
    sa.column("cycle_id", sa.BigInteger()),
    sa.column("step_no", sa.SmallInteger()),
    sa.column("state", sa.String()),
    sa.column("last_run_id", sa.BigInteger()),
    schema=BATCH_SCHEMA,
)


def BuildColumns(columnSpecs: tuple[ColumnSpec, ...]) -> list[sa.Column]:
    return [
        sa.Column(
            spec.name,
            spec.columnType,
            nullable=spec.nullable,
            server_default=None if spec.serverDefault is None else sa.text(spec.serverDefault),
            autoincrement=spec.autoincrement,
        )
        for spec in columnSpecs
    ]


def BuildValueChecks(checkSpecs: tuple[ValueCheckSpec, ...]) -> list[sa.CheckConstraint]:
    """Return one CheckConstraint per spec; op.f() keeps the convention from renaming it."""
    return [sa.CheckConstraint(spec.condition, name=op.f(spec.name)) for spec in checkSpecs]


def BuildForeignKeys(
    foreignKeySpecs: tuple[ForeignKeySpec, ...],
) -> list[sa.ForeignKeyConstraint]:
    """Return one deferrable ForeignKeyConstraint per spec, named exactly by op.f()."""
    return [
        sa.ForeignKeyConstraint(
            [spec.localColumn],
            [spec.referencedColumn],
            name=op.f(spec.name),
            **FOREIGN_KEY_OPTIONS,
        )
        for spec in foreignKeySpecs
    ]


def CreateCyclesTable() -> None:
    """Create batch.cycles: key pk_cycles, two value checks, comment and the OPEN-cycle index."""
    op.create_table(
        CYCLES_TABLE,
        *BuildColumns(CYCLES_COLUMNS),
        sa.PrimaryKeyConstraint("cycle_id", name=op.f("pk_cycles")),
        *BuildValueChecks(CYCLES_CHECKS),
        schema=BATCH_SCHEMA,
        comment=CYCLES_COMMENT,
    )
    op.create_index(
        op.f(OPEN_CYCLE_INDEX),
        CYCLES_TABLE,
        ["cycle_id"],
        schema=BATCH_SCHEMA,
        postgresql_where=sa.text(OPEN_CYCLE_PREDICATE),
    )


def CreateJobRunsTable() -> None:
    """Create batch.job_runs: pk_job_runs, cycle key, status check, step and RUNNING-run indexes.

    The index names are passed through op.f(): the convention alone would name each after its
    first column only, ix_job_runs_cycle_id and ix_job_runs_run_id.
    """
    op.create_table(
        JOB_RUNS_TABLE,
        *BuildColumns(JOB_RUNS_COLUMNS),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_job_runs")),
        *BuildForeignKeys(JOB_RUNS_FOREIGN_KEYS),
        *BuildValueChecks(JOB_RUNS_CHECKS),
        schema=BATCH_SCHEMA,
        comment=JOB_RUNS_COMMENT,
    )
    op.create_index(
        op.f(JOB_RUNS_STEP_INDEX), JOB_RUNS_TABLE, ["cycle_id", "step_no"], schema=BATCH_SCHEMA
    )
    op.create_index(
        op.f(RUNNING_RUN_INDEX),
        JOB_RUNS_TABLE,
        ["run_id"],
        schema=BATCH_SCHEMA,
        postgresql_where=sa.text(RUNNING_RUN_PREDICATE),
    )


def CreateCycleStepsTable() -> None:
    """Create batch.cycle_steps keyed by (cycle_id, step_no), with its two keys and state check."""
    op.create_table(
        CYCLE_STEPS_TABLE,
        *BuildColumns(CYCLE_STEPS_COLUMNS),
        sa.PrimaryKeyConstraint("cycle_id", "step_no", name=op.f("pk_cycle_steps")),
        *BuildForeignKeys(CYCLE_STEPS_FOREIGN_KEYS),
        *BuildValueChecks(CYCLE_STEPS_CHECKS),
        schema=BATCH_SCHEMA,
        comment=CYCLE_STEPS_COMMENT,
    )


def CountRows(connection: sa.Connection, schemaName: str, tableName: str) -> int:
    countStatement = sa.select(sa.func.count()).select_from(sa.table(tableName, schema=schemaName))
    return connection.execute(countStatement).scalar_one()


def SeedMarkerPresent(connection: sa.Connection) -> bool:
    """Return True when the batch schema's comment is revision 0002's seed marker."""
    schemaComment = connection.execute(
        SCHEMA_COMMENT_QUERY, {"schemaName": BATCH_SCHEMA}
    ).scalar_one_or_none()
    return schemaComment == SEED_MARKER_COMMENT


def SeedWasLoaded(connection: sa.Connection) -> bool:
    """Return True when revision 0002's seed marker is present and accounts.accounts has rows."""
    if not SeedMarkerPresent(connection):
        return False
    return CountRows(connection, SEED_PROBE_SCHEMA, SEED_PROBE_TABLE) > 0


def SeedSkipReason(connection: sa.Connection) -> str:
    """Return why SeedWasLoaded is False: no seed marker, or a marked seed since emptied."""
    if SeedMarkerPresent(connection):
        return EMPTIED_SEED_REASON
    return NO_SEED_MARKER_REASON


def InsertSeedCycle(connection: sa.Connection) -> int:
    """Insert the OPEN cycle opened by SEED and return its cycle_id."""
    cycleStatement = (
        sa.insert(CYCLES_LEDGER)
        .values(status=SEED_CYCLE_STATUS, opened_by=SEED_CYCLE_OPENER)
        .returning(CYCLES_LEDGER.c.cycle_id)
    )
    return connection.execute(cycleStatement).scalar_one()


def InsertLoaderRun(connection: sa.Connection, cycleId: int, loaderStep: SeededLoaderStep) -> int:
    """Insert the COMPLETED run of one seeded loader step and return its run_id.

    counts records the rows its table now holds, plus the step's extra counts.
    """
    rowCount = CountRows(connection, loaderStep.schema, loaderStep.table)
    runStatement = (
        sa.insert(JOB_RUNS_LEDGER)
        .values(
            cycle_id=cycleId,
            step_no=loaderStep.stepNo,
            job_name=loaderStep.jobName,
            params=SEED_RUN_PARAMS,
            status=SEED_RUN_STATUS,
            exit_code=SEED_RUN_EXIT_CODE,
            counts={ROWS_LOADED_COUNT: rowCount, **loaderStep.extraCounts},
            started_at=sa.func.now(),
            finished_at=sa.func.now(),
        )
        .returning(JOB_RUNS_LEDGER.c.run_id)
    )
    return connection.execute(runStatement).scalar_one()


def CycleStepRow(cycleId: int, stepNo: int, runIdsByStep: Mapping[int, int]) -> dict[str, object]:
    """Return the cycle_steps row of stepNo: COMPLETED with its seeded run, else PENDING."""
    lastRunId = runIdsByStep.get(stepNo)
    if lastRunId is None:
        stepState = PENDING_STEP_STATE
    else:
        stepState = COMPLETED_STEP_STATE
    return {"cycle_id": cycleId, "step_no": stepNo, "state": stepState, "last_run_id": lastRunId}


def InsertCycleSteps(
    connection: sa.Connection, cycleId: int, runIdsByStep: Mapping[int, int]
) -> None:
    stepRows = [CycleStepRow(cycleId, stepNo, runIdsByStep) for stepNo in CHAIN_STEP_NUMBERS]
    connection.execute(sa.insert(CYCLE_STEPS_LEDGER), stepRows)


def RecordSeedCycle() -> None:
    """Record revision 0002's seed as the OPEN batch cycle, when a seed was loaded.

    Offline mode has no connection to read through. A database without 0002's seed marker
    (0002 skipped under APP_ENV=production or offline, whatever rows the tables hold) or whose
    marked seed has since been emptied has no seed to record. Each leaves the ledger empty and
    logs why.
    """
    if context.is_offline_mode():
        LOGGER.info("seed cycle skipped: offline mode has no connection to the tables")
        return
    connection = op.get_bind()
    if not SeedWasLoaded(connection):
        LOGGER.info("seed cycle skipped: %s", SeedSkipReason(connection))
        return
    cycleId = InsertSeedCycle(connection)
    runIdsByStep = {
        loaderStep.stepNo: InsertLoaderRun(connection, cycleId, loaderStep)
        for loaderStep in SEEDED_LOADER_STEPS
    }
    InsertCycleSteps(connection, cycleId, runIdsByStep)
    LOGGER.info("seed cycle %d recorded: steps 1-5 and 7-12 COMPLETED, the rest PENDING", cycleId)


def upgrade() -> None:
    """Create the cycle, run and step tables parent-first, then record any seed as a cycle."""
    CreateCyclesTable()
    CreateJobRunsTable()
    CreateCycleStepsTable()
    RecordSeedCycle()


def downgrade() -> None:
    """Drop the three ledger tables and their indexes child-first; any seed-cycle rows go too."""
    op.drop_table(CYCLE_STEPS_TABLE, schema=BATCH_SCHEMA)
    op.drop_index(op.f(RUNNING_RUN_INDEX), table_name=JOB_RUNS_TABLE, schema=BATCH_SCHEMA)
    op.drop_index(op.f(JOB_RUNS_STEP_INDEX), table_name=JOB_RUNS_TABLE, schema=BATCH_SCHEMA)
    op.drop_table(JOB_RUNS_TABLE, schema=BATCH_SCHEMA)
    op.drop_index(op.f(OPEN_CYCLE_INDEX), table_name=CYCLES_TABLE, schema=BATCH_SCHEMA)
    op.drop_table(CYCLES_TABLE, schema=BATCH_SCHEMA)
