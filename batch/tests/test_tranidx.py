"""TRANIDX: chain step 18 rebuilds the PROC-TS alternate index and completes its cycle.

Pins jcl/TRANIDX.jcl (STEP20 DEFINE ALTERNATEINDEX KEYS(26 304) NONUNIQUEKEY on copybook
CVTRA05Y TRAN-PROC-TS, STEP25 DEFINE PATH, STEP30 BLDINDEX; the AWS.M2.CARDDEMO.TRANSACT.VSAM.AIX
entries of catlg/LISTCAT.txt) as batch/jobs/tranidx.py ports it: the non-unique index
ix_transactions_proc_ts on transactions.transactions (proc_ts). Technical Specification Section
0.6.8, step 18: CREATE INDEX IF NOT EXISTS, then REINDEX, the same result on every run, exit 0
or 12, and a cycle becomes COMPLETED when step 18 completes. Section 0.9.3: TRANIDX produces no
records, so its bar is no errors.

Completing step 18 closes its cycle even when the step runs on its own, so a second
``job tran-index`` finds no OPEN cycle and exits 12 (Section 0.6.8, "Standalone commands"). A
rerun of the step therefore runs in a new OPEN cycle, which seed-all opens.
"""

from pathlib import Path

import pytest
from app.models import CYCLE_STEPS, CYCLES, JOB_RUNS, TRANSACTIONS_SCHEMA, Transaction
from sqlalchemy import Engine, func, select, text

from batch.jobs.tranidx import INDEXES_VERIFIED_COUNT_KEY, JOB_NAME, PROC_TS_INDEX_NAME
from batch.tests.conftest import (
    FIXED_AS_OF_TEXT,
    INJECTED_ROLLBACK_TEMPLATE,
    BatchEnvironment,
    CliOutcome,
    CountJobRuns,
    InjectAtomicRollbackFailure,
    InvokeCli,
    StepRow,
)

TRANIDX_STEP_NO = 18
INDEX_SCHEMA = TRANSACTIONS_SCHEMA
INDEXED_TABLE_NAME = "transactions"
PROC_TS_COLUMN = "proc_ts"

# The exit statuses of Section 0.6.8: success, POSTTRAN's legacy RC 4 for rejected records, and
# the recoverable failure.
EXIT_OK = 0
EXIT_REJECTS = 4
EXIT_FAILURE = 12

# The value domains of batch.cycles, batch.cycle_steps and batch.job_runs (Alembic 0008).
CYCLE_OPEN = "OPEN"
CYCLE_COMPLETED = "COMPLETED"
STEP_COMPLETED = "COMPLETED"
STEP_FAILED = "FAILED"
RUN_FAILED = "FAILED"
RUN_SKIPPED = "SKIPPED"

# The refusal batch_chain logs for a job command while no cycle is OPEN.
NO_OPEN_CYCLE_TEXT = "NO OPEN CYCLE"

JOB_COMMAND = ("job", JOB_NAME)
FORCED_JOB_COMMAND = ("job", JOB_NAME, "--force")
SEED_ALL_COMMAND = ("seed-all",)
RUN_ALL_COMMAND = ("run-all", "--as-of", FIXED_AS_OF_TEXT)

# A fixed identifier, never input: the index the job must recreate.
DROP_PROC_TS_INDEX_SQL = text("DROP INDEX transactions.ix_transactions_proc_ts")

# pg_index flags, table, key columns in key order and definition of one index; no row when the
# index does not exist. indkey is an int2vector, so it is cast to an array to unnest it.
INDEX_STATE_SQL = text(
    "SELECT i.indisvalid, i.indisready, t.relname AS table_name,"
    " array_agg(a.attname::text ORDER BY k.ordinality) AS column_names,"
    " pg_get_indexdef(i.indexrelid) AS index_definition"
    " FROM pg_index i"
    " JOIN pg_class c ON c.oid = i.indexrelid"
    " JOIN pg_namespace n ON n.oid = c.relnamespace"
    " JOIN pg_class t ON t.oid = i.indrelid"
    " CROSS JOIN LATERAL unnest(i.indkey::smallint[]) WITH ORDINALITY AS k(attnum, ordinality)"
    " JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum"
    " WHERE n.nspname = :schemaName AND c.relname = :indexName"
    " GROUP BY i.indexrelid, i.indisvalid, i.indisready, t.relname"
)


# --- Catalog and ledger reads --------------------------------------------------------------------
def IndexState(engine: Engine) -> dict[str, object] | None:
    """Return the catalog state of ix_transactions_proc_ts, or None when the index is absent.

    The dict holds indisvalid, indisready, table_name, column_names (in key order) and
    index_definition (pg_get_indexdef).
    """
    parameters = {"schemaName": INDEX_SCHEMA, "indexName": PROC_TS_INDEX_NAME}
    with engine.connect() as connection:
        row = connection.execute(INDEX_STATE_SQL, parameters).mappings().one_or_none()
    if row is None:
        return None
    return dict(row)


def RequireValidProcTsIndex(engine: Engine) -> dict[str, object]:
    """Assert the index exists, valid and ready, on transactions.transactions (proc_ts) alone."""
    indexState = IndexState(engine)
    assert indexState is not None, f"{INDEX_SCHEMA}.{PROC_TS_INDEX_NAME} does not exist"
    assert indexState["indisvalid"] is True
    assert indexState["indisready"] is True
    assert indexState["table_name"] == INDEXED_TABLE_NAME
    assert indexState["column_names"] == [PROC_TS_COLUMN]
    return indexState


def DropProcTsIndex(engine: Engine) -> None:
    """Drop the PROC-TS index, as a lost AIX; seedState recreates it before the next test."""
    with engine.begin() as connection:
        connection.execute(DROP_PROC_TS_INDEX_SQL)


def InvokeExpecting(arguments: tuple[str, ...], exitCode: int) -> CliOutcome:
    """Run a carddemo-batch command in process and assert its exit status."""
    outcome = InvokeCli(arguments)
    assert outcome.exitCode == exitCode, outcome.output
    return outcome


def CycleRow(engine: Engine, cycleId: int) -> dict[str, object]:
    """Return the status and closed_at of one batch.cycles row."""
    statement = select(CYCLES.c.status, CYCLES.c.closed_at).where(CYCLES.c.cycle_id == cycleId)
    with engine.connect() as connection:
        return dict(connection.execute(statement).mappings().one())


def OpenCycleCount(engine: Engine) -> int:
    """Return the number of OPEN batch cycles."""
    statement = select(func.count()).select_from(CYCLES).where(CYCLES.c.status == CYCLE_OPEN)
    with engine.connect() as connection:
        return connection.execute(statement).scalar_one()


def CycleStepStates(engine: Engine, cycleId: int) -> dict[int, str]:
    """Return the state of every chain step of one cycle, keyed by step number."""
    statement = select(CYCLE_STEPS.c.step_no, CYCLE_STEPS.c.state).where(
        CYCLE_STEPS.c.cycle_id == cycleId
    )
    with engine.connect() as connection:
        return {row.step_no: row.state for row in connection.execute(statement)}


def RunCounts(engine: Engine, runId: int) -> dict[str, int]:
    """Return the counts JSON of one batch.job_runs row."""
    statement = select(JOB_RUNS.c.counts).where(JOB_RUNS.c.run_id == runId)
    with engine.connect() as connection:
        return connection.execute(statement).scalar_one()


def TransactionCount(engine: Engine) -> int:
    """Return the number of rows of transactions.transactions."""
    statement = select(func.count()).select_from(Transaction.__table__)
    with engine.connect() as connection:
        return connection.execute(statement).scalar_one()


def WrittenFiles(outputDir: Path) -> list[Path]:
    """Return every file under the batch output directory, in path order."""
    return sorted(path for path in outputDir.rglob("*") if path.is_file())


# --- Tests ---------------------------------------------------------------------------------------
def test_tran_index_exits_0_and_index_is_valid_on_proc_ts(
    batchEnvironment: BatchEnvironment,
) -> None:
    """From the seed, job tran-index exits 0, leaves a valid index and writes no records."""
    engine = batchEnvironment.engine
    seedTransactionCount = len(batchEnvironment.snapshot.tableRows[Transaction.__table__.fullname])
    InvokeExpecting(JOB_COMMAND, EXIT_OK)
    RequireValidProcTsIndex(engine)
    stepRow = StepRow(engine, TRANIDX_STEP_NO)
    assert stepRow["state"] == STEP_COMPLETED
    assert RunCounts(engine, stepRow["last_run_id"]) == {INDEXES_VERIFIED_COUNT_KEY: 1}
    assert TransactionCount(engine) == seedTransactionCount
    assert WrittenFiles(batchEnvironment.outputDir) == []


def test_dropped_proc_ts_index_is_recreated(batchEnvironment: BatchEnvironment) -> None:
    """A missing index is created again (STEP20 and STEP25) and built (STEP30)."""
    engine = batchEnvironment.engine
    DropProcTsIndex(engine)
    assert IndexState(engine) is None
    InvokeExpecting(JOB_COMMAND, EXIT_OK)
    RequireValidProcTsIndex(engine)
    assert StepRow(engine, TRANIDX_STEP_NO)["state"] == STEP_COMPLETED


def test_forced_rerun_gives_same_result(batchEnvironment: BatchEnvironment) -> None:
    """A forced second run leaves the same index state and definition, with one more history row.

    The first run closes the seed cycle, so seed-all opens the OPEN cycle the forced job needs.
    """
    engine = batchEnvironment.engine
    InvokeExpecting(JOB_COMMAND, EXIT_OK)
    firstState = RequireValidProcTsIndex(engine)
    firstHistoryCount = CountJobRuns(engine, stepNo=TRANIDX_STEP_NO)
    InvokeExpecting(SEED_ALL_COMMAND, EXIT_OK)
    InvokeExpecting(FORCED_JOB_COMMAND, EXIT_OK)
    assert RequireValidProcTsIndex(engine) == firstState
    assert CountJobRuns(engine, stepNo=TRANIDX_STEP_NO) == firstHistoryCount + 1
    assert StepRow(engine, TRANIDX_STEP_NO)["state"] == STEP_COMPLETED


def RequireSkippedRerun(engine: Engine) -> None:
    """Assert a rerun in a still OPEN cycle is skipped: exit 0 and one more SKIPPED row."""
    skippedCount = CountJobRuns(engine, stepNo=TRANIDX_STEP_NO, status=RUN_SKIPPED)
    InvokeExpecting(JOB_COMMAND, EXIT_OK)
    assert CountJobRuns(engine, stepNo=TRANIDX_STEP_NO, status=RUN_SKIPPED) == skippedCount + 1


def RequireNoOpenCycleRefusal(engine: Engine) -> None:
    """Assert a rerun with no OPEN cycle exits 12, names the refusal and records no run."""
    assert OpenCycleCount(engine) == 0
    historyCount = CountJobRuns(engine, stepNo=TRANIDX_STEP_NO)
    outcome = InvokeExpecting(JOB_COMMAND, EXIT_FAILURE)
    assert NO_OPEN_CYCLE_TEXT in outcome.output
    assert CountJobRuns(engine, stepNo=TRANIDX_STEP_NO) == historyCount


def test_unforced_rerun_after_completion_is_skipped_with_exit_0(
    batchEnvironment: BatchEnvironment,
) -> None:
    """An unforced second job tran-index takes the outcome the ledger state after the first sets.

    The cycle of step 18 is read from batch.cycles after the first run, and decides:

    - OPEN: step 18 is COMPLETED in an OPEN cycle, so the rerun is skipped with exit 0 and a
      SKIPPED history row (Section 0.6.8, "Skip").
    - COMPLETED: completing step 18 closed the cycle, so no cycle is OPEN and the rerun exits 12
      without a history row (Section 0.6.8, "Standalone commands"). This is the path the chain
      takes, because a completed step 18 always closes its cycle.

    Either way the index stays valid.
    """
    engine = batchEnvironment.engine
    InvokeExpecting(JOB_COMMAND, EXIT_OK)
    cycleStatus = CycleRow(engine, StepRow(engine, TRANIDX_STEP_NO)["cycle_id"])["status"]
    if cycleStatus == CYCLE_OPEN:
        RequireSkippedRerun(engine)
    else:
        assert cycleStatus == CYCLE_COMPLETED
        RequireNoOpenCycleRefusal(engine)
    RequireValidProcTsIndex(engine)


def test_completing_step_18_completes_the_open_cycle(batchEnvironment: BatchEnvironment) -> None:
    """run-all resumes the seed cycle through step 18, which marks the cycle COMPLETED.

    The chain exits 4, the POSTTRAN rejects of the sample daily file.
    """
    engine = batchEnvironment.engine
    seedStepRow = StepRow(engine, TRANIDX_STEP_NO)
    assert seedStepRow["cycle_status"] == CYCLE_OPEN
    InvokeExpecting(RUN_ALL_COMMAND, EXIT_REJECTS)
    cycleRow = CycleRow(engine, seedStepRow["cycle_id"])
    assert cycleRow["status"] == CYCLE_COMPLETED
    assert cycleRow["closed_at"] is not None
    stepStates = CycleStepStates(engine, seedStepRow["cycle_id"])
    assert stepStates == {stepNo: STEP_COMPLETED for stepNo in range(1, TRANIDX_STEP_NO + 1)}
    assert OpenCycleCount(engine) == 0
    RequireValidProcTsIndex(engine)


def test_injected_failure_in_tran_index_transaction_exits_12_and_keeps_index(
    batchEnvironment: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure of the final ledger write rolls the job back: exit 12, the index as before."""
    engine = batchEnvironment.engine
    stateBefore = RequireValidProcTsIndex(engine)
    failedCount = CountJobRuns(engine, stepNo=TRANIDX_STEP_NO, status=RUN_FAILED)
    InjectAtomicRollbackFailure(monkeypatch, TRANIDX_STEP_NO)
    outcome = InvokeExpecting(JOB_COMMAND, EXIT_FAILURE)
    assert INJECTED_ROLLBACK_TEMPLATE.format(stepNo=TRANIDX_STEP_NO) in outcome.output
    assert RequireValidProcTsIndex(engine) == stateBefore
    stepRow = StepRow(engine, TRANIDX_STEP_NO)
    assert stepRow["state"] == STEP_FAILED
    assert stepRow["cycle_status"] == CYCLE_OPEN
    assert CountJobRuns(engine, stepNo=TRANIDX_STEP_NO, status=RUN_FAILED) == failedCount + 1
