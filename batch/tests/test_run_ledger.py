"""Run ledger: cycles, step identity, run history, interrupted runs and financial reloads.

Pins batch/orchestration/run_ledger.py and the cycle policy batch/orchestration/batch_chain.py
applies with it. Together they replace the job tracking and restart status of the CA-7 and
Control-M schedules (scheduler/CardDemo.ca7, scheduler/CardDemo.controlm): the TRANBKP job
(jcl/TRANBKP.jcl) that runs twice in one cycle, and the per-write POSTTRAN and INTCALC programs
(jcl/POSTTRAN.jcl, jcl/INTCALC.jcl) whose partial effects an abend leaves behind. The rules are
those of the Technical Specification, Section 0.6.8 ("Cycles and step identity", "Interrupted
runs", "Skip", "Resume", "Failure", "Force", "Standalone commands", "Financial reloads"), and
its commit points those of Section 0.7.2:

- A cycle opens through Alembic revision 0008 (the SEED cycle), seed-all, run-all --new-cycle,
  or run-all when no cycle is OPEN, and becomes COMPLETED when step 18 completes.
- A step's identity is (cycle_id, step_no). batch.cycle_steps holds its authoritative state and
  last_run_id; batch.job_runs is the history, with any number of rows per identity.
- A RUNNING run found when a command starts becomes INTERRUPTED. Its atomic or files-only step
  returns to PENDING; its per-write step (13 or 14) becomes INTERRUPTED and is refused as FAILED.
- A COMPLETED step is skipped with a SKIPPED history row; --force reruns it and moves last_run_id.
- A financial reload is refused while a later step that writes its table is COMPLETED, FAILED
  or INTERRUPTED in the OPEN cycle, and nothing overrides the refusal.

Commands run in process through InvokeCli against the seeded test database, whose restored seed
state is the OPEN SEED cycle. Ledger rows are read and set with SQLAlchemy Core statements.
"""

import inspect
import logging
from collections.abc import Callable
from decimal import Decimal

import pytest
from app.models import CYCLE_STEPS, CYCLES, JOB_RUNS, Base, Card
from sqlalchemy import Connection, Engine, Row, Table, func, insert, select, update

from batch.cli import POST_TRAN_JOB, TRAN_BACKUP_JOB, TRAN_INDEX_JOB
from batch.orchestration import batch_chain, run_ledger
from batch.orchestration.job_context import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_REJECTS,
    JobContext,
    JobResult,
)
from batch.tests.conftest import (
    FIXED_AS_OF_TEXT,
    BatchEnvironment,
    CountJobRuns,
    InjectStepFailure,
    InsertSyntheticTransactions,
    InvokeCli,
    StepRow,
    SyntheticTransactionSpec,
)

# Chain step numbers of the step table (Section 0.6.8), pinned rather than read from the chain.
FIRST_TRANBKP_STEP = 6
TRANTYPE_STEP = 7
DISCGRP_STEP = 9
POSTTRAN_STEP = 13
INTCALC_STEP = 14
SECOND_TRANBKP_STEP = 15
CREASTMT_STEP = 17

TRAN_TYPES_LOADER = "tran-types"
TRANSACTIONS_LOADER = "transactions"
# Click reports an unknown option as a usage error with this exit status.
USAGE_ERROR_EXIT = 2
FORCE_FLAG = "--force"
AS_OF_ARGUMENTS = ("--as-of", FIXED_AS_OF_TEXT)

# The step states the Alembic seed and seed-all leave: the loader steps COMPLETED, the rest PENDING.
LOADER_CYCLE_STATES = {
    stepNo: (
        run_ledger.STEP_COMPLETED
        if stepNo in batch_chain.LOADER_STEP_NUMBERS
        else run_ledger.STEP_PENDING
    )
    for stepNo in run_ledger.CHAIN_STEP_NUMBERS
}
ALL_COMPLETED_STATES = dict.fromkeys(run_ledger.CHAIN_STEP_NUMBERS, run_ledger.STEP_COMPLETED)
# The history of a cycle the chain ran from step 1: one COMPLETED run per step, in chain order.
FULL_CHAIN_HISTORY = [
    (stepNo, run_ledger.RUN_COMPLETED) for stepNo in run_ledger.CHAIN_STEP_NUMBERS
]
# The history run-all adds when it resumes the SEED cycle: the loader steps SKIPPED, the rest run.
RESUMED_SEED_HISTORY = [
    (
        stepNo,
        run_ledger.RUN_SKIPPED if state == run_ledger.STEP_COMPLETED else run_ledger.RUN_COMPLETED,
    )
    for stepNo, state in LOADER_CYCLE_STATES.items()
]

# The states that refuse a financial reload, as Section 0.6.8 lists them.
RELOAD_BLOCKING_STATES = (
    run_ledger.STEP_COMPLETED,
    run_ledger.STEP_FAILED,
    run_ledger.STEP_INTERRUPTED,
)
FINANCIAL_LOADERS = tuple(run_ledger.FINANCIAL_RELOADS)
NON_FINANCIAL_LOADERS = tuple(
    loaderName
    for loaderName in batch_chain.LOADER_JOB_NAMES
    if loaderName not in run_ledger.FINANCIAL_RELOADS
)
# Every refusing state at once, on the later steps that write the financial tables.
WRITER_STEP_STATES = {
    FIRST_TRANBKP_STEP: run_ledger.STEP_FAILED,
    POSTTRAN_STEP: run_ledger.STEP_COMPLETED,
    INTCALC_STEP: run_ledger.STEP_INTERRUPTED,
}

# A value no seed amount holds, so a reload that ran would show in the table's rows.
MARKER_AMOUNT = Decimal("12345.67")
MARKED_AMOUNT_COLUMNS = {"accounts": "curr_balance", "tran-cat-balances": "balance"}

# The completion a TRANBKP run commits in its own transaction, before it publishes its generation.
COMMITTED_COUNTS = {"records_archived": 3}
COMMITTED_RESULT = JobResult(EXIT_OK, COMMITTED_COUNTS)
# The counts column of a run that never completed: the column's '{}' default.
UNFINISHED_COUNTS: dict[str, int] = {}
# How long a transaction runs before the run it finishes is recorded finished in it.
LONG_TRANSACTION_SECONDS = 1.0


def OpenCycleId(engine: Engine) -> int:
    """Return the OPEN cycle's id; fail the test when no cycle is OPEN."""
    with engine.connect() as connection:
        cycleId = run_ledger.FindOpenCycle(connection)
    assert cycleId is not None, "no batch cycle is OPEN"
    return cycleId


def CycleRows(engine: Engine) -> list[Row]:
    """Return every batch.cycles row, oldest first."""
    statement = select(CYCLES).order_by(CYCLES.c.cycle_id)
    with engine.connect() as connection:
        return connection.execute(statement).all()


def CycleStepRows(engine: Engine, cycleId: int) -> dict[int, Row]:
    """Return the batch.cycle_steps rows of one cycle, keyed by step number."""
    statement = select(CYCLE_STEPS).where(CYCLE_STEPS.c.cycle_id == cycleId)
    with engine.connect() as connection:
        return {row.step_no: row for row in connection.execute(statement)}


def CycleStepStates(engine: Engine, cycleId: int) -> dict[int, str]:
    """Return the state of every step of one cycle, keyed by step number."""
    return {stepNo: row.state for stepNo, row in CycleStepRows(engine, cycleId).items()}


def RunRow(engine: Engine, runId: int) -> Row:
    """Return one batch.job_runs row."""
    statement = select(JOB_RUNS).where(JOB_RUNS.c.run_id == runId)
    with engine.connect() as connection:
        return connection.execute(statement).one()


def NewestRunId(engine: Engine) -> int:
    """Return the id of the newest batch.job_runs row, of any cycle or of none."""
    statement = select(func.max(JOB_RUNS.c.run_id))
    with engine.connect() as connection:
        return connection.execute(statement).scalar_one()


def CycleRunHistory(engine: Engine, cycleId: int) -> list[tuple[int, str]]:
    """Return the (step_no, status) of every batch.job_runs row of one cycle, in run order."""
    statement = (
        select(JOB_RUNS.c.step_no, JOB_RUNS.c.status)
        .where(JOB_RUNS.c.cycle_id == cycleId)
        .order_by(JOB_RUNS.c.run_id)
    )
    with engine.connect() as connection:
        return [(row.step_no, row.status) for row in connection.execute(statement)]


def StepAndLastRun(engine: Engine, stepNo: int) -> tuple[dict[str, object], Row]:
    """Return a step's row in the current cycle and the batch.job_runs row its last_run_id names."""
    stepRow = StepRow(engine, stepNo)
    return stepRow, RunRow(engine, stepRow["last_run_id"])


def LastRunOutcome(engine: Engine, stepNo: int) -> tuple[object, ...]:
    """Return (cycle_id, step_no, step state, status, exit_code, finished) of a step's last run."""
    stepRow, lastRun = StepAndLastRun(engine, stepNo)
    isFinished = lastRun.finished_at is not None
    runColumns = (lastRun.cycle_id, lastRun.step_no)
    return (*runColumns, stepRow["state"], lastRun.status, lastRun.exit_code, isFinished)


def SetStepState(engine: Engine, cycleId: int, stepNo: int, state: str) -> None:
    """Set one step identity's state directly, as a run of the step would have left it."""
    statement = (
        update(CYCLE_STEPS)
        .where(CYCLE_STEPS.c.cycle_id == cycleId, CYCLE_STEPS.c.step_no == stepNo)
        .values(state=state)
    )
    with engine.begin() as connection:
        connection.execute(statement)


def MarkCycleCompleted(engine: Engine, cycleId: int) -> None:
    """Close a cycle COMPLETED, as its step 18 completing does, so that no cycle is OPEN."""
    statement = (
        update(CYCLES)
        .where(CYCLES.c.cycle_id == cycleId)
        .values(status=run_ledger.CYCLE_COMPLETED, closed_at=func.now())
    )
    with engine.begin() as connection:
        connection.execute(statement)


def InsertRunningRun(engine: Engine, cycleId: int, stepNo: int) -> int:
    """Insert the RUNNING row a batch process that died during the step leaves; return its id."""
    statement = (
        insert(JOB_RUNS)
        .values(
            cycle_id=cycleId,
            step_no=stepNo,
            job_name=LegacyJobName(stepNo),
            status=run_ledger.RUN_RUNNING,
        )
        .returning(JOB_RUNS.c.run_id)
    )
    with engine.begin() as connection:
        return connection.execute(statement).scalar_one()


def StartStepRun(jobContext: JobContext, stepNo: int) -> JobContext:
    """Start a RUNNING run of a chain step in the OPEN cycle; return the context bound to it."""
    cycleId = OpenCycleId(jobContext.engine)
    runStart = run_ledger.RunStart(
        cycleId=cycleId,
        stepNo=stepNo,
        jobName=LegacyJobName(stepNo),
        params=run_ledger.BuildRunParams(jobContext),
    )
    runId = run_ledger.StartRun(jobContext, runStart)
    return jobContext.ForRun(cycleId, stepNo, runId)


def CommitStepCompletion(stepContext: JobContext) -> None:
    """Commit the run and its step COMPLETED, as an atomic step's own transaction ends."""
    with stepContext.engine.begin() as connection:
        run_ledger.FinishAtomicRun(connection, stepContext, COMMITTED_RESULT)


def FinishAtomicRunInTransaction(connection: Connection, stepContext: JobContext) -> None:
    """Finish the run COMPLETED as the last statement of the step's own transaction."""
    run_ledger.FinishAtomicRun(connection, stepContext, COMMITTED_RESULT)


def ReclassifyRunInTransaction(connection: Connection, stepContext: JobContext) -> None:
    """Reclassify the run INTERRUPTED, as ClassifyInterruptedRuns does in its own transaction."""
    runKey = JOB_RUNS.c.run_id == stepContext.runId
    runningRow = connection.execute(run_ledger.RUNNING_ROWS_STATEMENT.where(runKey)).one()
    run_ledger.ReclassifyRun(connection, runningRow)


def LegacyJobName(stepNo: int) -> str:
    """Return the legacy JCL name of a chain step's job, as batch.job_runs.job_name records it."""
    return batch_chain.CHAIN[stepNo - 1].job.LegacyName()


def LoaderStepNo(loaderName: str) -> int:
    """Return the chain step a loader runs as."""
    (loaderStep,) = batch_chain.StepsForJob(batch_chain.JOB_DEFINITIONS[loaderName])
    return loaderStep.stepNo


def ExpectedPerWriteRefusal(cycleId: int, stepNo: int, state: str) -> str:
    """Return the refusal text of a FAILED or INTERRUPTED per-write step, naming cycle and step."""
    return batch_chain.PER_WRITE_REFUSED_TEMPLATE.format(
        stepNo=stepNo, jobName=LegacyJobName(stepNo), cycleId=cycleId, state=state
    )


def ExpectedNewCycleStartRefusal(stepNo: int) -> str:
    """Return the refusal text of a --start-at that names a step after step 1 for a new cycle."""
    return batch_chain.NEW_CYCLE_START_REFUSED_TEMPLATE.format(
        stepNo=stepNo, jobName=LegacyJobName(stepNo)
    )


def ExpectedRunLines(run: Row) -> tuple[str, str]:
    """Return the STARTED and ENDED lines of a finished run, as its batch.job_runs row records it.

    A run of a cycle step names its step; a load made with no OPEN cycle names its run only.
    """
    if run.cycle_id is None:
        return (
            run_ledger.RUN_STARTED_LOAD_LOG % (run.run_id, run.job_name),
            batch_chain.LOAD_ENDED_TEMPLATE.format(
                runId=run.run_id, jobName=run.job_name, exitCode=run.exit_code
            ),
        )
    return (
        run_ledger.RUN_STARTED_LOG % (run.run_id, run.step_no, run.job_name),
        batch_chain.STEP_ENDED_TEMPLATE.format(
            stepNo=run.step_no, jobName=run.job_name, exitCode=run.exit_code
        ),
    )


def ReloadTable(loaderName: str) -> Table:
    """Return the table a financial loader replaces."""
    return Base.metadata.tables[run_ledger.FINANCIAL_RELOAD_TABLES[loaderName]]


def TableRows(engine: Engine, sourceTable: Table) -> list[Row]:
    """Return every row of a table in primary-key order."""
    statement = select(sourceTable).order_by(*sourceTable.primary_key.columns)
    with engine.connect() as connection:
        return connection.execute(statement).all()


def FirstSampleCardNum(engine: Engine) -> str:
    """Return the lowest sample card number."""
    with engine.connect() as connection:
        return connection.execute(select(func.min(Card.__table__.c.card_num))).scalar_one()


def MarkFinancialTable(engine: Engine, loaderName: str) -> None:
    """Make a financial table differ from its seed image, so a reload that ran would show.

    The account and category balances are all set to MARKER_AMOUNT; the empty transactions
    table gets one synthetic transaction, which a TRANFILE reload would delete.
    """
    if loaderName == TRANSACTIONS_LOADER:
        cardNums = [FirstSampleCardNum(engine)]
        spec = SyntheticTransactionSpec(cardNums=cardNums, perCard=1, amount=MARKER_AMOUNT)
        InsertSyntheticTransactions(engine, spec)
        return
    markedValues = {MARKED_AMOUNT_COLUMNS[loaderName]: MARKER_AMOUNT}
    with engine.begin() as connection:
        connection.execute(update(ReloadTable(loaderName)).values(markedValues))


def ExpectedReloadRefusal(loaderName: str, blockingSteps: list[int], cycleId: int) -> str:
    """Return the RELOAD REFUSED text naming the loader's table, the steps and the cycle."""
    return run_ledger.RELOAD_REFUSED_TEMPLATE.format(
        table=run_ledger.FINANCIAL_RELOAD_TABLES[loaderName],
        steps=run_ledger.RELOAD_STEP_SEPARATOR.join(str(stepNo) for stepNo in blockingSteps),
        cycleId=cycleId,
    )


def test_alembic_seed_records_open_seed_cycle_with_loader_steps_completed(
    batchEnvironment: BatchEnvironment,
) -> None:
    """Revision 0008 records one OPEN SEED cycle: each loader step COMPLETED by its seed run."""
    engine = batchEnvironment.engine
    cycleRows = CycleRows(engine)
    assert [(row.status, row.opened_by) for row in cycleRows] == [
        (run_ledger.CYCLE_OPEN, run_ledger.OPENED_BY_SEED)
    ]
    seedCycleId = cycleRows[0].cycle_id
    stepRows = CycleStepRows(engine, seedCycleId)
    assert len(stepRows) == run_ledger.CHAIN_STEP_COUNT
    assert CycleStepStates(engine, seedCycleId) == LOADER_CYCLE_STATES
    for stepNo in batch_chain.LOADER_STEP_NUMBERS:
        seedRun = RunRow(engine, stepRows[stepNo].last_run_id)
        seedRunColumns = (seedRun.cycle_id, seedRun.step_no, seedRun.job_name, seedRun.status)
        expectedColumns = (seedCycleId, stepNo, LegacyJobName(stepNo), run_ledger.RUN_COMPLETED)
        assert seedRunColumns == expectedColumns
    pendingSteps = set(run_ledger.CHAIN_STEP_NUMBERS) - set(batch_chain.LOADER_STEP_NUMBERS)
    assert all(stepRows[stepNo].last_run_id is None for stepNo in pendingSteps)


def test_seed_all_abandons_open_cycle_and_opens_new_with_loader_steps(
    batchEnvironment: BatchEnvironment,
) -> None:
    """seed-all abandons the SEED cycle and runs steps 1-5 and 7-12 in a new OPEN cycle."""
    engine = batchEnvironment.engine
    seedCycleId = OpenCycleId(engine)
    outcome = InvokeCli(["seed-all"])
    assert outcome.exitCode == EXIT_OK, outcome.output
    seedCycle, newCycle = CycleRows(engine)
    assert (seedCycle.cycle_id, seedCycle.status) == (seedCycleId, run_ledger.CYCLE_ABANDONED)
    assert seedCycle.closed_at is not None
    assert (newCycle.status, newCycle.opened_by) == (
        run_ledger.CYCLE_OPEN,
        run_ledger.OPENED_BY_SEED_ALL,
    )
    assert CycleStepStates(engine, newCycle.cycle_id) == LOADER_CYCLE_STATES
    assert CycleRunHistory(engine, newCycle.cycle_id) == [
        (stepNo, run_ledger.RUN_COMPLETED) for stepNo in batch_chain.LOADER_STEP_NUMBERS
    ]


def test_run_all_new_cycle_abandons_open_cycle_and_completes_new_one(
    batchEnvironment: BatchEnvironment,
) -> None:
    """run-all --new-cycle abandons the SEED cycle, runs steps 1-18 and closes its cycle."""
    engine = batchEnvironment.engine
    outcome = InvokeCli(["run-all", "--new-cycle", *AS_OF_ARGUMENTS])
    assert outcome.exitCode == EXIT_REJECTS, outcome.output
    seedCycle, newCycle = CycleRows(engine)
    assert seedCycle.status == run_ledger.CYCLE_ABANDONED
    assert (newCycle.opened_by, newCycle.status) == (
        run_ledger.OPENED_BY_RUN_ALL,
        run_ledger.CYCLE_COMPLETED,
    )
    assert newCycle.closed_at is not None
    assert CycleStepStates(engine, newCycle.cycle_id) == ALL_COMPLETED_STATES
    assert CycleRunHistory(engine, newCycle.cycle_id) == FULL_CHAIN_HISTORY


def test_run_all_resumes_open_cycle_and_skips_its_completed_steps(
    batchEnvironment: BatchEnvironment,
) -> None:
    """run-all resumes the OPEN SEED cycle: SKIPPED rows for 1-5 and 7-12, runs for the rest."""
    engine = batchEnvironment.engine
    seedCycleId = OpenCycleId(engine)
    seedHistory = CycleRunHistory(engine, seedCycleId)
    outcome = InvokeCli(["run-all", *AS_OF_ARGUMENTS])
    assert outcome.exitCode == EXIT_REJECTS, outcome.output
    assert CycleRunHistory(engine, seedCycleId) == seedHistory + RESUMED_SEED_HISTORY
    assert [row.status for row in CycleRows(engine)] == [run_ledger.CYCLE_COMPLETED]
    assert CycleStepStates(engine, seedCycleId) == ALL_COMPLETED_STATES


def test_run_all_without_open_cycle_opens_one(batchEnvironment: BatchEnvironment) -> None:
    """With the last cycle COMPLETED and none OPEN, run-all opens a cycle and runs from step 1."""
    engine = batchEnvironment.engine
    completedCycleId = OpenCycleId(engine)
    MarkCycleCompleted(engine, completedCycleId)
    outcome = InvokeCli(["run-all", *AS_OF_ARGUMENTS])
    assert outcome.exitCode == EXIT_REJECTS, outcome.output
    completedCycle, newCycle = CycleRows(engine)
    assert (completedCycle.cycle_id, completedCycle.status) == (
        completedCycleId,
        run_ledger.CYCLE_COMPLETED,
    )
    assert (newCycle.opened_by, newCycle.status) == (
        run_ledger.OPENED_BY_RUN_ALL,
        run_ledger.CYCLE_COMPLETED,
    )
    assert CycleRunHistory(engine, newCycle.cycle_id) == FULL_CHAIN_HISTORY


@pytest.mark.parametrize(
    ("closeOpenCycle", "cycleOptions"),
    [
        pytest.param(False, ["--new-cycle"], id="new-cycle-over-open-seed-cycle"),
        pytest.param(True, [], id="no-open-cycle"),
    ],
)
def test_new_cycle_refuses_later_start_step_before_any_cycle_change(
    batchEnvironment: BatchEnvironment, closeOpenCycle: bool, cycleOptions: list[str]
) -> None:
    """A cycle run-all would open starts at step 1: --start-at POSTTRAN exits 12, changing nothing.

    Both ways run-all opens a cycle, --new-cycle and no cycle OPEN, refuse the later start step
    before any cycle is abandoned or opened, so the new cycle can never skip the loaders.
    """
    engine = batchEnvironment.engine
    if closeOpenCycle:
        MarkCycleCompleted(engine, OpenCycleId(engine))
    cyclesBefore = CycleRows(engine)
    runCount = CountJobRuns(engine)
    startAt = ["--start-at", LegacyJobName(POSTTRAN_STEP)]
    outcome = InvokeCli(["run-all", *cycleOptions, *startAt, *AS_OF_ARGUMENTS])
    refusal = ExpectedNewCycleStartRefusal(POSTTRAN_STEP)
    assert (outcome.exitCode, refusal in outcome.output) == (EXIT_FAILURE, True), outcome.output
    assert CycleRows(engine) == cyclesBefore
    assert CountJobRuns(engine) == runCount


def test_tranbkp_steps_6_and_15_are_distinct_identities(batchEnvironment: BatchEnvironment) -> None:
    """job tran-backup runs step 6, then step 15: each occurrence is its own identity."""
    engine = batchEnvironment.engine
    cycleId = OpenCycleId(engine)
    tranBackupSteps = (FIRST_TRANBKP_STEP, SECOND_TRANBKP_STEP)
    firstOutcome = InvokeCli(["job", TRAN_BACKUP_JOB])
    assert firstOutcome.exitCode == EXIT_OK, firstOutcome.output
    assert [StepRow(engine, stepNo)["state"] for stepNo in tranBackupSteps] == [
        run_ledger.STEP_COMPLETED,
        run_ledger.STEP_PENDING,
    ]
    secondOutcome = InvokeCli(["job", TRAN_BACKUP_JOB])
    assert secondOutcome.exitCode == EXIT_OK, secondOutcome.output
    stepRows = [StepRow(engine, stepNo) for stepNo in tranBackupSteps]
    assert [stepRow["state"] for stepRow in stepRows] == [run_ledger.STEP_COMPLETED] * 2
    lastRuns = [RunRow(engine, stepRow["last_run_id"]) for stepRow in stepRows]
    assert [(run.cycle_id, run.step_no, run.status) for run in lastRuns] == [
        (cycleId, stepNo, run_ledger.RUN_COMPLETED) for stepNo in tranBackupSteps
    ]
    newHistory = CycleRunHistory(engine, cycleId)[len(batch_chain.LOADER_STEP_NUMBERS) :]
    assert newHistory == [(stepNo, run_ledger.RUN_COMPLETED) for stepNo in tranBackupSteps]


def test_completed_step_rerun_records_skipped_row_and_exits_0(
    batchEnvironment: BatchEnvironment,
) -> None:
    """Rerunning COMPLETED step 6 adds a SKIPPED row and leaves its state and last_run_id."""
    engine = batchEnvironment.engine
    assert InvokeCli(["job", TRAN_BACKUP_JOB]).exitCode == EXIT_OK
    completedRow = StepRow(engine, FIRST_TRANBKP_STEP)
    completedRuns = CountJobRuns(engine, FIRST_TRANBKP_STEP, run_ledger.RUN_COMPLETED)
    skippedRuns = CountJobRuns(engine, FIRST_TRANBKP_STEP, run_ledger.RUN_SKIPPED)
    outcome = InvokeCli(["job", TRAN_BACKUP_JOB, "--occurrence", "1"])
    assert outcome.exitCode == EXIT_OK, outcome.output
    skippedMessage = batch_chain.STEP_SKIPPED_TEMPLATE.format(
        stepNo=FIRST_TRANBKP_STEP,
        jobName=LegacyJobName(FIRST_TRANBKP_STEP),
        cycleId=completedRow["cycle_id"],
    )
    assert skippedMessage in outcome.output
    assert CountJobRuns(engine, FIRST_TRANBKP_STEP, run_ledger.RUN_SKIPPED) == skippedRuns + 1
    assert CountJobRuns(engine, FIRST_TRANBKP_STEP, run_ledger.RUN_COMPLETED) == completedRuns
    assert StepRow(engine, FIRST_TRANBKP_STEP) == completedRow
    assert completedRow["state"] == run_ledger.STEP_COMPLETED


def test_forced_rerun_of_completed_step_adds_history_and_moves_last_run_id(
    batchEnvironment: BatchEnvironment,
) -> None:
    """--force reruns COMPLETED step 6: one more COMPLETED row, which last_run_id now names."""
    engine = batchEnvironment.engine
    assert InvokeCli(["job", TRAN_BACKUP_JOB]).exitCode == EXIT_OK
    firstRunId = StepRow(engine, FIRST_TRANBKP_STEP)["last_run_id"]
    completedRuns = CountJobRuns(engine, FIRST_TRANBKP_STEP, run_ledger.RUN_COMPLETED)
    outcome = InvokeCli(["job", TRAN_BACKUP_JOB, "--occurrence", "1", FORCE_FLAG])
    assert outcome.exitCode == EXIT_OK, outcome.output
    assert CountJobRuns(engine, FIRST_TRANBKP_STEP, run_ledger.RUN_COMPLETED) == completedRuns + 1
    forcedRow, forcedRun = StepAndLastRun(engine, FIRST_TRANBKP_STEP)
    assert forcedRow["state"] == run_ledger.STEP_COMPLETED
    assert forcedRun.run_id > firstRunId
    assert (forcedRun.step_no, forcedRun.status) == (FIRST_TRANBKP_STEP, run_ledger.RUN_COMPLETED)
    assert StepRow(engine, SECOND_TRANBKP_STEP)["state"] == run_ledger.STEP_PENDING


def test_job_without_open_cycle_exits_12(batchEnvironment: BatchEnvironment) -> None:
    """With the last cycle COMPLETED and none OPEN, job refuses, records nothing, opens nothing."""
    engine = batchEnvironment.engine
    MarkCycleCompleted(engine, OpenCycleId(engine))
    runCount = CountJobRuns(engine)
    outcome = InvokeCli(["job", POST_TRAN_JOB, *AS_OF_ARGUMENTS])
    assert outcome.exitCode == EXIT_FAILURE, outcome.output
    assert batch_chain.NO_OPEN_CYCLE_MESSAGE in outcome.output
    assert CountJobRuns(engine) == runCount
    assert [row.status for row in CycleRows(engine)] == [run_ledger.CYCLE_COMPLETED]


@pytest.mark.parametrize(
    ("cycleOpen", "absentTexts"),
    [
        (True, ("STEP None", "NO CYCLE STEP")),
        (False, ("STEP None", "STEP 7 TRANTYPE")),
    ],
    ids=["seed-cycle-open", "no-cycle-open"],
)
def test_load_started_and_ended_lines_agree_with_its_history_row(
    batchEnvironment: BatchEnvironment, cycleOpen: bool, absentTexts: tuple[str, ...]
) -> None:
    """load tran-types logs STARTED and ENDED lines that name what its batch.job_runs row holds.

    With the SEED cycle OPEN, the run is step 7 of that cycle. With no cycle OPEN, the row has no
    cycle and no step, so neither line prints "STEP None" or the chain's step number, and the
    load opens no cycle.
    """
    engine = batchEnvironment.engine
    cycleId = OpenCycleId(engine)
    if not cycleOpen:
        MarkCycleCompleted(engine, cycleId)
    outcome = InvokeCli(["load", TRAN_TYPES_LOADER])
    assert outcome.exitCode == EXIT_OK, outcome.output
    newRun = RunRow(engine, NewestRunId(engine))
    expectedStep = (cycleId, TRANTYPE_STEP) if cycleOpen else (None, None)
    runColumns = (newRun.cycle_id, newRun.step_no, newRun.status, newRun.exit_code)
    assert runColumns == (*expectedStep, run_ledger.RUN_COMPLETED, EXIT_OK)
    assert all(line in outcome.output for line in ExpectedRunLines(newRun)), outcome.output
    assert [text for text in absentTexts if text in outcome.output] == [], outcome.output
    cycleStatus = run_ledger.CYCLE_OPEN if cycleOpen else run_ledger.CYCLE_COMPLETED
    assert [row.status for row in CycleRows(engine)] == [cycleStatus]


def test_failed_and_completed_status_commit_with_step_state(
    batchEnvironment: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed load leaves its run and step FAILED together; its rerun leaves both COMPLETED.

    run_ledger.RecordFailure marks the step of a standalone load made in the OPEN cycle FAILED,
    naming the failed run; FinishRun then marks it COMPLETED, naming the run that completed it.
    """
    engine = batchEnvironment.engine
    cycleId = OpenCycleId(engine)
    with monkeypatch.context() as failurePatch:
        InjectStepFailure(failurePatch, TRANTYPE_STEP, EXIT_FAILURE)
        assert InvokeCli(["load", TRAN_TYPES_LOADER]).exitCode == EXIT_FAILURE
    failedRunId = StepRow(engine, TRANTYPE_STEP)["last_run_id"]
    failedOutcome = (run_ledger.STEP_FAILED, run_ledger.RUN_FAILED, EXIT_FAILURE)
    assert LastRunOutcome(engine, TRANTYPE_STEP) == (cycleId, TRANTYPE_STEP, *failedOutcome, True)
    assert InvokeCli(["load", TRAN_TYPES_LOADER]).exitCode == EXIT_OK
    completedOutcome = (run_ledger.STEP_COMPLETED, run_ledger.RUN_COMPLETED, EXIT_OK)
    assert LastRunOutcome(engine, TRANTYPE_STEP) == (
        cycleId,
        TRANTYPE_STEP,
        *completedOutcome,
        True,
    )
    assert StepRow(engine, TRANTYPE_STEP)["last_run_id"] > failedRunId


@pytest.mark.parametrize(
    ("completedFirst", "expectedOutcome"),
    [
        pytest.param(
            False,
            (True, run_ledger.RUN_FAILED, EXIT_FAILURE, UNFINISHED_COUNTS, run_ledger.STEP_FAILED),
            id="running-run-recorded-failed",
        ),
        pytest.param(
            True,
            (False, run_ledger.RUN_COMPLETED, EXIT_OK, COMMITTED_COUNTS, run_ledger.STEP_COMPLETED),
            id="committed-completion-kept",
        ),
    ],
)
def test_record_failure_fails_a_running_run_and_never_overwrites_a_committed_completion(
    jobContext: JobContext,
    caplog: pytest.LogCaptureFixture,
    completedFirst: bool,
    expectedOutcome: tuple[object, ...],
) -> None:
    """RecordFailure fails a RUNNING step 15 run and leaves one committed COMPLETED unchanged.

    A failure raised after TRANBKP committed its completion, while publishing its generation,
    must leave the run's status, exit code and counts and the step's state and last_run_id as
    committed, so the next command skips the step rather than archiving the emptied table.
    """
    engine = jobContext.engine
    stepContext = StartStepRun(jobContext, SECOND_TRANBKP_STEP)
    if completedFirst:
        CommitStepCompletion(stepContext)
    caplog.set_level(logging.WARNING, logger=run_ledger.LOGGER.name)
    recorded = run_ledger.RecordFailure(stepContext, stepContext.runId, EXIT_FAILURE)
    failedRun, stepRow = RunRow(engine, stepContext.runId), StepRow(engine, SECOND_TRANBKP_STEP)
    runColumns = (failedRun.status, failedRun.exit_code, failedRun.counts)
    assert (recorded, *runColumns, stepRow["state"]) == expectedOutcome
    assert (stepRow["last_run_id"], failedRun.finished_at is not None) == (stepContext.runId, True)
    keptWarning = run_ledger.FAILURE_NOT_RECORDED_LOG % (stepContext.runId, failedRun.status)
    assert (keptWarning in caplog.messages) == completedFirst


@pytest.mark.parametrize(
    "finishInTransaction",
    [
        pytest.param(FinishAtomicRunInTransaction, id="finish-atomic-run"),
        pytest.param(ReclassifyRunInTransaction, id="reclassify-interrupted-run"),
    ],
)
def test_run_finished_late_in_a_long_transaction_records_when_it_finished(
    jobContext: JobContext, finishInTransaction: Callable[[Connection, JobContext], None]
) -> None:
    """finished_at is when the run finished, not when the transaction finishing it began.

    The transaction runs LONG_TRANSACTION_SECONDS before the run is finished in it, as a long
    atomic load such as DUSRSECJ's bcrypt hashing does, so the recorded duration covers that time.
    """
    stepContext = StartStepRun(jobContext, TRANTYPE_STEP)
    with jobContext.engine.begin() as connection:
        connection.execute(select(func.pg_sleep(LONG_TRANSACTION_SECONDS)))
        finishInTransaction(connection, stepContext)
    finishedRun = RunRow(jobContext.engine, stepContext.runId)
    recordedSeconds = (finishedRun.finished_at - finishedRun.started_at).total_seconds()
    assert recordedSeconds >= LONG_TRANSACTION_SECONDS


@pytest.mark.parametrize(
    ("stepNo", "durability"),
    [
        pytest.param(TRANTYPE_STEP, batch_chain.DURABILITY_ATOMIC, id="atomic-step-7"),
        pytest.param(CREASTMT_STEP, batch_chain.DURABILITY_FILES_ONLY, id="files-only-step-17"),
    ],
)
def test_stale_running_atomic_or_files_only_step_becomes_interrupted_and_returns_to_pending(
    jobContext: JobContext, stepNo: int, durability: str
) -> None:
    """A dead atomic or files-only run is INTERRUPTED; its step is PENDING, last_run_id kept."""
    engine = jobContext.engine
    assert batch_chain.CHAIN[stepNo - 1].durability == durability
    stepBefore = StepRow(engine, stepNo)
    runId = InsertRunningRun(engine, OpenCycleId(engine), stepNo)
    assert run_ledger.ClassifyInterruptedRuns(jobContext) == 1
    interruptedRun = RunRow(engine, runId)
    assert interruptedRun.status == run_ledger.RUN_INTERRUPTED
    assert interruptedRun.finished_at is not None
    assert StepRow(engine, stepNo) == {**stepBefore, "state": run_ledger.STEP_PENDING}


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["job", POST_TRAN_JOB, *AS_OF_ARGUMENTS], id="job-post-tran"),
        pytest.param(["run-all", *AS_OF_ARGUMENTS], id="run-all"),
    ],
)
def test_stale_running_per_write_step_becomes_interrupted_and_is_refused(
    jobContext: JobContext, arguments: list[str]
) -> None:
    """A dead POSTTRAN run leaves step 13 INTERRUPTED, naming it; job and run-all refuse it."""
    engine = jobContext.engine
    cycleId = OpenCycleId(engine)
    assert POSTTRAN_STEP in run_ledger.PER_WRITE_STEP_NUMBERS
    runId = InsertRunningRun(engine, cycleId, POSTTRAN_STEP)
    assert run_ledger.ClassifyInterruptedRuns(jobContext) == 1
    assert RunRow(engine, runId).status == run_ledger.RUN_INTERRUPTED
    interruptedRow = StepRow(engine, POSTTRAN_STEP)
    assert (interruptedRow["state"], interruptedRow["last_run_id"]) == (
        run_ledger.STEP_INTERRUPTED,
        runId,
    )
    outcome = InvokeCli(arguments)
    refusal = ExpectedPerWriteRefusal(cycleId, POSTTRAN_STEP, run_ledger.STEP_INTERRUPTED)
    assert (outcome.exitCode, refusal in outcome.output) == (EXIT_FAILURE, True), outcome.output
    assert CountJobRuns(engine, POSTTRAN_STEP) == 1
    assert StepRow(engine, POSTTRAN_STEP) == interruptedRow


def test_interrupted_per_write_step_executes_with_force(jobContext: JobContext) -> None:
    """--force reruns INTERRUPTED step 13 from its partial state: a new run completes it."""
    engine = jobContext.engine
    interruptedRunId = InsertRunningRun(engine, OpenCycleId(engine), POSTTRAN_STEP)
    run_ledger.ClassifyInterruptedRuns(jobContext)
    assert StepRow(engine, POSTTRAN_STEP)["state"] == run_ledger.STEP_INTERRUPTED
    outcome = InvokeCli(["job", POST_TRAN_JOB, FORCE_FLAG, *AS_OF_ARGUMENTS])
    assert outcome.exitCode == EXIT_REJECTS, outcome.output
    assert CountJobRuns(engine, POSTTRAN_STEP) == 2
    forcedRow, forcedRun = StepAndLastRun(engine, POSTTRAN_STEP)
    assert forcedRow["state"] == run_ledger.STEP_COMPLETED
    assert forcedRun.run_id > interruptedRunId
    assert (forcedRun.status, forcedRun.exit_code) == (run_ledger.RUN_COMPLETED, EXIT_REJECTS)


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["load", TRAN_TYPES_LOADER], id="load"),
        pytest.param(["job", TRAN_BACKUP_JOB], id="job"),
        pytest.param(["run-all", "--start-at", TRAN_INDEX_JOB], id="run-all"),
        pytest.param(["seed-all"], id="seed-all"),
    ],
)
def test_every_command_classifies_interrupted_runs_first(
    batchEnvironment: BatchEnvironment, arguments: list[str]
) -> None:
    """Every command form reclassifies a stale RUNNING row of an unrelated step INTERRUPTED."""
    engine = batchEnvironment.engine
    runId = InsertRunningRun(engine, OpenCycleId(engine), DISCGRP_STEP)
    outcome = InvokeCli(arguments)
    assert outcome.exitCode == EXIT_OK, outcome.output
    interruptedRun = RunRow(engine, runId)
    assert interruptedRun.status == run_ledger.RUN_INTERRUPTED
    assert interruptedRun.finished_at is not None


@pytest.mark.parametrize("loaderName", FINANCIAL_LOADERS)
@pytest.mark.parametrize("writerState", RELOAD_BLOCKING_STATES)
def test_financial_reload_refused_while_later_writer_step_completed_failed_or_interrupted(
    batchEnvironment: BatchEnvironment, loaderName: str, writerState: str
) -> None:
    """With step 13 COMPLETED, FAILED or INTERRUPTED, each financial load is refused unrun."""
    engine = batchEnvironment.engine
    cycleId = OpenCycleId(engine)
    SetStepState(engine, cycleId, POSTTRAN_STEP, writerState)
    MarkFinancialTable(engine, loaderName)
    rowsBefore = TableRows(engine, ReloadTable(loaderName))
    runCount = CountJobRuns(engine)
    outcome = InvokeCli(["load", loaderName])
    assert outcome.exitCode == EXIT_FAILURE, outcome.output
    assert ExpectedReloadRefusal(loaderName, [POSTTRAN_STEP], cycleId) in outcome.output
    assert TableRows(engine, ReloadTable(loaderName)) == rowsBefore
    assert CountJobRuns(engine) == runCount


@pytest.mark.parametrize("loaderName", FINANCIAL_LOADERS)
@pytest.mark.parametrize("writerState", RELOAD_BLOCKING_STATES)
@pytest.mark.parametrize(
    "restart",
    [
        pytest.param((run_ledger.STEP_COMPLETED, (FORCE_FLAG,)), id="forced"),
        pytest.param((run_ledger.STEP_FAILED, ()), id="failed-retry"),
    ],
)
def test_run_all_start_at_financial_loader_step_refused_even_with_force(
    batchEnvironment: BatchEnvironment,
    loaderName: str,
    writerState: str,
    restart: tuple[str, tuple[str, ...]],
) -> None:
    """run-all reaching a financial loader's step is refused as load is, --force included.

    With step 13 COMPLETED, FAILED or INTERRUPTED, run-all --start-at the loader's step either
    forces the COMPLETED loader step or retries it FAILED. Either way it exits 12 with the
    refusal and moves no table row, run history row or step state.
    """
    engine = batchEnvironment.engine
    cycleId = OpenCycleId(engine)
    loaderStepNo = LoaderStepNo(loaderName)
    loaderState, forceOptions = restart
    SetStepState(engine, cycleId, POSTTRAN_STEP, writerState)
    SetStepState(engine, cycleId, loaderStepNo, loaderState)
    MarkFinancialTable(engine, loaderName)
    rowsBefore = TableRows(engine, ReloadTable(loaderName))
    stepBefore, runCount = StepRow(engine, loaderStepNo), CountJobRuns(engine)
    startAt = ["--start-at", str(loaderStepNo), *forceOptions]
    outcome = InvokeCli(["run-all", *startAt, *AS_OF_ARGUMENTS])
    assert outcome.exitCode == EXIT_FAILURE, outcome.output
    assert ExpectedReloadRefusal(loaderName, [POSTTRAN_STEP], cycleId) in outcome.output
    assert TableRows(engine, ReloadTable(loaderName)) == rowsBefore
    assert (StepRow(engine, loaderStepNo), CountJobRuns(engine)) == (stepBefore, runCount)


@pytest.mark.parametrize("loaderName", FINANCIAL_LOADERS)
def test_financial_reload_refusal_is_not_overridden_by_force(
    jobContext: JobContext, loaderName: str
) -> None:
    """Nothing overrides the refusal: load has no --force, CheckFinancialReload no force flag.

    The CLI declares no --force on load, so `load NAME --force` is a usage error that runs
    nothing; and CheckFinancialReload, which ExecuteStep consults before every loader run of
    load and run-all alike, takes only the context and the loader name, so no caller can pass it
    an override.
    """
    engine = jobContext.engine
    cycleId = OpenCycleId(engine)
    SetStepState(engine, cycleId, POSTTRAN_STEP, run_ledger.STEP_COMPLETED)
    runCount = CountJobRuns(engine)
    outcome = InvokeCli(["load", loaderName, FORCE_FLAG])
    assert (outcome.exitCode, FORCE_FLAG in outcome.output) == (USAGE_ERROR_EXIT, True)
    assert CountJobRuns(engine) == runCount
    checkParameters = inspect.signature(run_ledger.CheckFinancialReload).parameters
    assert list(checkParameters) == ["context", "loaderName"]
    refusal = run_ledger.CheckFinancialReload(jobContext, loaderName)
    assert refusal == ExpectedReloadRefusal(loaderName, [POSTTRAN_STEP], cycleId)


@pytest.mark.parametrize("loaderName", NON_FINANCIAL_LOADERS)
def test_non_financial_loads_execute_in_any_state(
    batchEnvironment: BatchEnvironment, loaderName: str
) -> None:
    """With the writer steps COMPLETED, FAILED and INTERRUPTED, every other load still runs."""
    engine = batchEnvironment.engine
    cycleId = OpenCycleId(engine)
    for writerStep, writerState in WRITER_STEP_STATES.items():
        SetStepState(engine, cycleId, writerStep, writerState)
    loaderStepNo = LoaderStepNo(loaderName)
    seedRunId = StepRow(engine, loaderStepNo)["last_run_id"]
    outcome = InvokeCli(["load", loaderName])
    assert outcome.exitCode == EXIT_OK, outcome.output
    completedOutcome = (run_ledger.STEP_COMPLETED, run_ledger.RUN_COMPLETED, EXIT_OK, True)
    assert LastRunOutcome(engine, loaderStepNo) == (cycleId, loaderStepNo, *completedOutcome)
    assert StepRow(engine, loaderStepNo)["last_run_id"] > seedRunId
    assert CycleStepStates(engine, cycleId) == {**LOADER_CYCLE_STATES, **WRITER_STEP_STATES}
