"""Replaces: CA-7/Control-M job tracking and restart status (scheduler/CardDemo.ca7,
scheduler/CardDemo.controlm), JCL condition codes.

The run ledger of the batch chain, kept in batch.cycles, batch.cycle_steps and batch.job_runs.
A cycle is one pass of the 18-step chain. The step identity is (cycle_id, step_no), whose
authoritative state lives in batch.cycle_steps; batch.job_runs is the history, with any number of
rows per identity. Specified in the Technical Specification, Sections 0.6.8 and 0.7.2, and
transcribed in docs/batch.md ("Cycles and step identity", "Financial reloads").

Commit points (Section 0.7.2):

- StartRun commits the RUNNING row in its own transaction before the step starts.
- FinishRun writes the final run status and the step state on the caller's connection, so both
  commit together. Atomic steps finish with FinishAtomicRun inside their own transaction. The
  per-write producers POSTTRAN and INTCALC finish with FinishAtomicRun in their final
  transaction, after their last posting write and before the generation's COMMITTED row, so
  that row stays the step's final database write. The files-only CREASTMT finishes with
  FinishIfRunning, in its own transaction, once its files are in place.
- RecordFailure commits FAILED in its own transaction after the failure, for a run still RUNNING
  and its step together. A run already COMPLETED, or otherwise no longer RUNNING, keeps its
  status and its step its state, so a committed completion is never overwritten FAILED.
- ClassifyInterruptedRuns reclassifies the RUNNING rows a dead process left behind.

This module is the single source of the ledger's value constants, which gdg and batch_chain
import; it imports neither of them, because gdg imports this module and batch_chain imports both.
Ledger timestamps come from the database clock, never the batch Clock: they are run metadata and
are excluded from reconciliation. Finish times are clock_timestamp(), the moment the statement
runs: an atomic run finishes inside the step's own transaction, where now() would give the time
that transaction began. Every statement is a SQLAlchemy Core construct with bound parameters,
and database errors propagate for batch_chain and the CLI to map to exit statuses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.models import CYCLE_STEPS, CYCLES, GDG_GENERATIONS, JOB_RUNS
from sqlalchemy import ColumnElement, Update, and_, func, insert, select, update
from sqlalchemy.engine import Connection, Row

from batch.orchestration.job_context import (
    CHAIN_STOP_THRESHOLD,
    EXIT_OK,
    JobContext,
    JobResult,
)

LOGGER = logging.getLogger(__name__)

# Value domains of app.models.support_tables (CYCLE_STATUSES, CYCLE_OPENERS, STEP_STATES,
# RUN_STATUSES, GENERATION_STATES) and of the check constraints of Alembic 0008 and 0009.
CYCLE_OPEN = "OPEN"
CYCLE_COMPLETED = "COMPLETED"
CYCLE_ABANDONED = "ABANDONED"

# SEED is written only by Alembic revision 0008, which records the sample seed as a cycle.
OPENED_BY_SEED = "SEED"
OPENED_BY_SEED_ALL = "seed-all"
OPENED_BY_RUN_ALL = "run-all"
COMMAND_CYCLE_OPENERS = (OPENED_BY_SEED_ALL, OPENED_BY_RUN_ALL)

STEP_PENDING = "PENDING"
STEP_COMPLETED = "COMPLETED"
STEP_FAILED = "FAILED"
STEP_INTERRUPTED = "INTERRUPTED"

RUN_RUNNING = "RUNNING"
RUN_COMPLETED = "COMPLETED"
RUN_FAILED = "FAILED"
RUN_SKIPPED = "SKIPPED"
RUN_INTERRUPTED = "INTERRUPTED"

GDG_COMMITTED = "COMMITTED"
GDG_PUBLISHED = "PUBLISHED"
GDG_ROLLED_OFF = "ROLLED_OFF"
GDG_LOST = "LOST"
GDG_ABANDONED = "ABANDONED"

# Must agree with batch_chain.CHAIN, which a test asserts.
CHAIN_STEP_COUNT = 18
CHAIN_STEP_NUMBERS = range(1, CHAIN_STEP_COUNT + 1)
# POSTTRAN and INTCALC commit each write as they make it, so an interruption may leave partial
# effects; every other step is atomic or writes files only.
PER_WRITE_STEP_NUMBERS = (13, 14)

# The loaders whose tables later steps write, keyed by canonical loader name, with those steps.
FINANCIAL_RELOADS: dict[str, tuple[int, ...]] = {
    "accounts": (13, 14),
    "tran-cat-balances": (13,),
    "transactions": (6, 13, 15, 16),
}
FINANCIAL_RELOAD_TABLES: dict[str, str] = {
    "accounts": "accounts.accounts",
    "tran-cat-balances": "transactions.transaction_category_balances",
    "transactions": "transactions.transactions",
}
RELOAD_BLOCKING_STATES = (STEP_COMPLETED, STEP_FAILED, STEP_INTERRUPTED)
RELOAD_REFUSED_TEMPLATE = (
    "RELOAD REFUSED: {table} WAS CHANGED BY STEPS {steps} OF CYCLE {cycleId};"
    " RUN seed-all OR run-all --new-cycle"
)
RELOAD_STEP_SEPARATOR = ","

UNBOUND_RUN_MESSAGE = "the job context is not bound to a started run"
UNBOUND_CYCLE_MESSAGE = "the job context is not bound to a cycle"

CYCLE_OPENED_LOG = "CYCLE %d OPENED BY %s"
CYCLE_ABANDONED_LOG = "CYCLE %d ABANDONED"
CYCLE_COMPLETED_LOG = "CYCLE %d COMPLETED"
RUN_STARTED_LOG = "RUN %d STEP %d %s STARTED"
RUN_STARTED_LOAD_LOG = "RUN %d %s STARTED; NO CYCLE STEP"
RUN_SKIPPED_LOG = "RUN %d STEP %d %s SKIPPED; STEP ALREADY COMPLETED"
INTERRUPTED_STEP_LOG = "RUN %d STEP %d %s INTERRUPTED; STEP NOW %s"
INTERRUPTED_LOAD_LOG = "RUN %d %s INTERRUPTED; NO CYCLE STEP"
FAILURE_NOT_RECORDED_LOG = "RUN %d IS %s, NOT RUNNING; FAILURE NOT RECORDED"

RUNNING_ROWS_STATEMENT = (
    select(JOB_RUNS.c.run_id, JOB_RUNS.c.cycle_id, JOB_RUNS.c.step_no, JOB_RUNS.c.job_name)
    .where(JOB_RUNS.c.status == RUN_RUNNING)
    .order_by(JOB_RUNS.c.run_id)
    .with_for_update()
)


def CheckStepNo(stepNo: int | None) -> None:
    """Refuse a step number outside the chain; None is the step of a load with no OPEN cycle."""
    if stepNo is not None and stepNo not in CHAIN_STEP_NUMBERS:
        raise ValueError(f"step number must be 1 to {CHAIN_STEP_COUNT}, got {stepNo!r}")


def CheckCompletedExitCode(exitCode: int) -> None:
    """Refuse a completion status a runner must raise as JobFailure instead."""
    if not EXIT_OK <= exitCode < CHAIN_STOP_THRESHOLD:
        raise ValueError(
            f"a completed run exits {EXIT_OK} to {CHAIN_STOP_THRESHOLD - 1}, got {exitCode!r}"
        )


def CheckFailureExitCode(exitCode: int) -> None:
    """Refuse a failure status below CHAIN_STOP_THRESHOLD, which the chain would continue past."""
    if exitCode < CHAIN_STOP_THRESHOLD:
        raise ValueError(f"a failed run exits {CHAIN_STOP_THRESHOLD} or above, got {exitCode!r}")


@dataclass(frozen=True)
class RunStart:
    """The batch.job_runs row StartRun records.

    cycleId and stepNo are None for a load made with no OPEN cycle. jobName is the legacy JCL
    name (ACCTFILE, DUSRSECJ, DALYTRAN, TRANBKP, POSTTRAN, ...), as Alembic 0008 writes it, and
    params is BuildRunParams(context).
    """

    cycleId: int | None
    stepNo: int | None
    jobName: str
    params: dict[str, object]

    def __post_init__(self) -> None:
        CheckStepNo(self.stepNo)


@dataclass(frozen=True)
class RunFinish:
    """The completion FinishRun records for a started run and, when it has one, its step."""

    runId: int
    cycleId: int | None
    stepNo: int | None
    exitCode: int
    counts: dict[str, int]

    @classmethod
    def FromResult(cls, context: JobContext, result: JobResult) -> RunFinish:
        """Build the completion of the run the context is bound to, from the runner's result."""
        if context.runId is None:
            raise ValueError(UNBOUND_RUN_MESSAGE)
        return cls(
            runId=context.runId,
            cycleId=context.cycleId,
            stepNo=context.stepNo,
            exitCode=result.exitCode,
            counts=dict(result.counts),
        )


def StepKey(cycleId: int, stepNo: int) -> ColumnElement[bool]:
    """Return the condition selecting the batch.cycle_steps row of one step identity."""
    return and_(CYCLE_STEPS.c.cycle_id == cycleId, CYCLE_STEPS.c.step_no == stepNo)


def RunKey(runId: int) -> ColumnElement[bool]:
    """Return the condition selecting one batch.job_runs row."""
    return JOB_RUNS.c.run_id == runId


def SetStepStatement(cycleId: int, stepNo: int, state: str, lastRunId: int) -> Update:
    """Return the update giving a step identity its new state and the run that set it."""
    return (
        update(CYCLE_STEPS)
        .where(StepKey(cycleId, stepNo))
        .values(state=state, last_run_id=lastRunId)
    )


def FindOpenCycle(connection: Connection) -> int | None:
    """Return the newest OPEN cycle's id, the Alembic seed cycle included, or None."""
    statement = (
        select(CYCLES.c.cycle_id)
        .where(CYCLES.c.status == CYCLE_OPEN)
        .order_by(CYCLES.c.cycle_id.desc())
        .limit(1)
    )
    return connection.execute(statement).scalar_one_or_none()


def PendingStepRows(cycleId: int) -> list[dict[str, object]]:
    """Return the batch.cycle_steps rows of a new cycle: every chain step PENDING, never run."""
    return [
        {"cycle_id": cycleId, "step_no": stepNo, "state": STEP_PENDING, "last_run_id": None}
        for stepNo in CHAIN_STEP_NUMBERS
    ]


def OpenCycle(context: JobContext, openedBy: str) -> int:
    """Open a cycle for seed-all or run-all, with all 18 steps PENDING, and return its id."""
    if openedBy not in COMMAND_CYCLE_OPENERS:
        raise ValueError(f"a cycle is opened by one of {COMMAND_CYCLE_OPENERS}, got {openedBy!r}")
    cycleStatement = (
        insert(CYCLES)
        .values(status=CYCLE_OPEN, opened_by=openedBy, opened_at=func.now())
        .returning(CYCLES.c.cycle_id)
    )
    with context.engine.begin() as connection:
        cycleId = connection.execute(cycleStatement).scalar_one()
        connection.execute(insert(CYCLE_STEPS), PendingStepRows(cycleId))
    LOGGER.info(CYCLE_OPENED_LOG, cycleId, openedBy)
    return cycleId


def AbandonLostGenerations(connection: Connection, cycleIds: list[int]) -> None:
    """Mark ABANDONED the LOST generations written by any run of the given cycles."""
    cycleRunIds = select(JOB_RUNS.c.run_id).where(JOB_RUNS.c.cycle_id.in_(cycleIds))
    statement = (
        update(GDG_GENERATIONS)
        .where(and_(GDG_GENERATIONS.c.state == GDG_LOST, GDG_GENERATIONS.c.run_id.in_(cycleRunIds)))
        .values(state=GDG_ABANDONED)
    )
    connection.execute(statement)


def AbandonOpenCycle(context: JobContext) -> int | None:
    """Abandon the OPEN cycle and its LOST generations, for seed-all and run-all --new-cycle.

    Returns the abandoned cycle's id, or None when no cycle was OPEN. Every OPEN cycle is
    abandoned, so a new cycle never starts beside an older one.
    """
    cycleStatement = (
        update(CYCLES)
        .where(CYCLES.c.status == CYCLE_OPEN)
        .values(status=CYCLE_ABANDONED, closed_at=func.now())
        .returning(CYCLES.c.cycle_id)
    )
    with context.engine.begin() as connection:
        cycleIds = sorted(connection.execute(cycleStatement).scalars().all())
        if not cycleIds:
            return None
        AbandonLostGenerations(connection, cycleIds)
    for cycleId in cycleIds:
        LOGGER.info(CYCLE_ABANDONED_LOG, cycleId)
    return cycleIds[-1]


def CompleteCycle(context: JobContext) -> None:
    """Mark the context's cycle COMPLETED, as step 18 completing does; only an OPEN cycle moves."""
    if context.cycleId is None:
        raise ValueError(UNBOUND_CYCLE_MESSAGE)
    statement = (
        update(CYCLES)
        .where(and_(CYCLES.c.cycle_id == context.cycleId, CYCLES.c.status == CYCLE_OPEN))
        .values(status=CYCLE_COMPLETED, closed_at=func.now())
    )
    with context.engine.begin() as connection:
        completedCount = connection.execute(statement).rowcount
    if completedCount:
        LOGGER.info(CYCLE_COMPLETED_LOG, context.cycleId)


def ReadStepStates(connection: Connection, cycleId: int) -> dict[int, str]:
    """Return the state of every step of a cycle, keyed by step number in chain order."""
    statement = (
        select(CYCLE_STEPS.c.step_no, CYCLE_STEPS.c.state)
        .where(CYCLE_STEPS.c.cycle_id == cycleId)
        .order_by(CYCLE_STEPS.c.step_no)
    )
    return {stepNo: state for stepNo, state in connection.execute(statement)}


def StepState(context: JobContext, cycleId: int, stepNo: int) -> str:
    """Return the authoritative state of one step identity."""
    statement = select(CYCLE_STEPS.c.state).where(StepKey(cycleId, stepNo))
    with context.engine.connect() as connection:
        return connection.execute(statement).scalar_one()


def StepStates(context: JobContext, cycleId: int) -> dict[int, str]:
    """Return the states of all 18 steps of a cycle, keyed by step number."""
    with context.engine.connect() as connection:
        return ReadStepStates(connection, cycleId)


def BuildRunParams(context: JobContext) -> dict[str, object]:
    """Return the batch.job_runs.params JSON: the command parameters and the --as-of value."""
    return {**context.parameters.AsLedgerParams(), "as_of": context.clock.AsOfText()}


def StartRun(context: JobContext, runStart: RunStart) -> int:
    """Record a RUNNING run, committed before the step starts (Section 0.7.2); return its id.

    The step's state is left as it stands: there is no RUNNING step state. The STARTED line
    names the run's cycle step, or, for a load made with no OPEN cycle, says it has none.
    """
    statement = (
        insert(JOB_RUNS)
        .values(
            cycle_id=runStart.cycleId,
            step_no=runStart.stepNo,
            job_name=runStart.jobName,
            params=runStart.params,
            status=RUN_RUNNING,
            started_at=func.now(),
        )
        .returning(JOB_RUNS.c.run_id)
    )
    with context.engine.begin() as connection:
        runId = connection.execute(statement).scalar_one()
    if runStart.cycleId is None or runStart.stepNo is None:
        LOGGER.info(RUN_STARTED_LOAD_LOG, runId, runStart.jobName)
    else:
        LOGGER.info(RUN_STARTED_LOG, runId, runStart.stepNo, runStart.jobName)
    return runId


def FinishRun(connection: Connection, runFinish: RunFinish) -> None:
    """Record a run COMPLETED and, when it has one, its step COMPLETED, in the caller's transaction.

    The run status and the step state therefore commit together (Section 0.7.2).
    """
    CheckCompletedExitCode(runFinish.exitCode)
    runStatement = (
        update(JOB_RUNS)
        .where(RunKey(runFinish.runId))
        .values(
            status=RUN_COMPLETED,
            exit_code=runFinish.exitCode,
            counts=runFinish.counts,
            finished_at=func.clock_timestamp(),
        )
    )
    connection.execute(runStatement)
    if runFinish.cycleId is not None and runFinish.stepNo is not None:
        stepStatement = SetStepStatement(
            runFinish.cycleId, runFinish.stepNo, STEP_COMPLETED, runFinish.runId
        )
        connection.execute(stepStatement)


def FinishAtomicRun(connection: Connection, context: JobContext, result: JobResult) -> None:
    """Finish the run inside an atomic step's own transaction, or a producer's final one.

    A GDG producer calls it inside its gdg.NewGeneration block, so the generation's COMMITTED
    row follows it as the transaction's final write. A context with no started run, as unit
    tests build, records nothing.
    """
    if not context.IsLedgered():
        return
    FinishRun(connection, RunFinish.FromResult(context, result))


def FinishIfRunning(context: JobContext, result: JobResult) -> bool:
    """Finish the run in its own transaction if it is still RUNNING; return whether it did.

    The files-only CREASTMT calls it once its files are in place, and it also finishes the run
    of a runner that committed without FinishAtomicRun. batch_chain calls it after every runner;
    for a run its runner already finished, as atomic steps and the per-write producers do with
    FinishAtomicRun and CREASTMT with this call, it changes nothing.
    """
    if not context.IsLedgered():
        return False
    statusStatement = select(JOB_RUNS.c.status).where(RunKey(context.runId)).with_for_update()
    runFinish = RunFinish.FromResult(context, result)
    with context.engine.begin() as connection:
        if connection.execute(statusStatement).scalar_one() != RUN_RUNNING:
            return False
        FinishRun(connection, runFinish)
    return True


def ReadRunStatus(connection: Connection, runId: int) -> str | None:
    """Return one run's status, or None when no batch.job_runs row has that id."""
    statement = select(JOB_RUNS.c.status).where(RunKey(runId))
    return connection.execute(statement).scalar_one_or_none()


def RecordFailure(context: JobContext, runId: int, exitCode: int) -> bool:
    """Record a RUNNING run FAILED and its step FAILED, in their own transaction after the failure.

    Returns whether the failure was recorded. Only a run still RUNNING moves, and its step moves
    with it. A run that is no longer RUNNING, such as one whose runner committed COMPLETED and then
    raised while publishing, keeps its status and its step its state: the committed work is
    never marked FAILED, so it is never retried or refused as if it had not completed. A warning
    names the run and the status it kept.
    """
    CheckFailureExitCode(exitCode)
    runStatement = (
        update(JOB_RUNS)
        .where(and_(RunKey(runId), JOB_RUNS.c.status == RUN_RUNNING))
        .values(status=RUN_FAILED, exit_code=exitCode, finished_at=func.clock_timestamp())
    )
    with context.engine.begin() as connection:
        if connection.execute(runStatement).rowcount == 0:
            LOGGER.warning(FAILURE_NOT_RECORDED_LOG, runId, ReadRunStatus(connection, runId))
            return False
        if context.cycleId is not None and context.stepNo is not None:
            stepStatement = SetStepStatement(context.cycleId, context.stepNo, STEP_FAILED, runId)
            connection.execute(stepStatement)
    return True


def RecordSkip(context: JobContext, stepNo: int, jobName: str) -> int:
    """Record a SKIPPED history row for a COMPLETED step, leaving its state; return the run id."""
    CheckStepNo(stepNo)
    statement = (
        insert(JOB_RUNS)
        .values(
            cycle_id=context.cycleId,
            step_no=stepNo,
            job_name=jobName,
            params=BuildRunParams(context),
            status=RUN_SKIPPED,
            exit_code=EXIT_OK,
            started_at=func.now(),
            finished_at=func.clock_timestamp(),
        )
        .returning(JOB_RUNS.c.run_id)
    )
    with context.engine.begin() as connection:
        runId = connection.execute(statement).scalar_one()
    LOGGER.info(RUN_SKIPPED_LOG, runId, stepNo, jobName)
    return runId


def InterruptedStepState(stepNo: int) -> str:
    """Return the state an interrupted run leaves its step in (Section 0.6.8, Interrupted runs).

    A per-write step may have committed writes, so it is INTERRUPTED and treated as FAILED. An
    atomic or files-only step committed nothing, so it returns to PENDING and is retried.
    """
    if stepNo in PER_WRITE_STEP_NUMBERS:
        return STEP_INTERRUPTED
    return STEP_PENDING


def ReclassifyStep(connection: Connection, row: Row) -> str:
    """Give an interrupted run's step its new state, and return that state.

    Only INTERRUPTED points last_run_id at the run, whose partial effects the step now carries.
    PENDING keeps last_run_id: the interrupted run left nothing behind, so the step's previous
    run stays its reference.
    """
    stepState = InterruptedStepState(row.step_no)
    stepValues: dict[str, object] = {"state": stepState}
    if stepState == STEP_INTERRUPTED:
        stepValues["last_run_id"] = row.run_id
    stepStatement = update(CYCLE_STEPS).where(StepKey(row.cycle_id, row.step_no))
    connection.execute(stepStatement.values(**stepValues))
    return stepState


def ReclassifyRun(connection: Connection, row: Row) -> None:
    """Mark one RUNNING run INTERRUPTED and, when it ran as a cycle step, reclassify the step."""
    runStatement = (
        update(JOB_RUNS)
        .where(RunKey(row.run_id))
        .values(status=RUN_INTERRUPTED, finished_at=func.clock_timestamp())
    )
    connection.execute(runStatement)
    if row.cycle_id is None or row.step_no is None:
        LOGGER.warning(INTERRUPTED_LOAD_LOG, row.run_id, row.job_name)
        return
    stepState = ReclassifyStep(connection, row)
    LOGGER.warning(INTERRUPTED_STEP_LOG, row.run_id, row.step_no, row.job_name, stepState)


def ClassifyInterruptedRuns(context: JobContext) -> int:
    """Reclassify every RUNNING run as INTERRUPTED; return how many there were.

    Called first in every command, under the batch window: entering it waited until no job
    session of a former window owner was left, and a process whose window was lost commits
    nothing more, so every RUNNING row belongs to a run that died or was stopped.
    """
    with context.engine.begin() as connection:
        runningRows = connection.execute(RUNNING_ROWS_STATEMENT).all()
        for row in runningRows:
            ReclassifyRun(connection, row)
    return len(runningRows)


def BlockingSteps(stepStates: dict[int, str], laterSteps: tuple[int, ...]) -> list[int]:
    """Return, ascending, the later steps whose state refuses a financial reload."""
    return [
        stepNo for stepNo in sorted(laterSteps) if stepStates.get(stepNo) in RELOAD_BLOCKING_STATES
    ]


def FormatReloadRefusal(loaderName: str, blockingSteps: list[int], cycleId: int) -> str:
    """Return the refusal text of a financial reload, for example STEPS 13,14 OF CYCLE 3."""
    return RELOAD_REFUSED_TEMPLATE.format(
        table=FINANCIAL_RELOAD_TABLES[loaderName],
        steps=RELOAD_STEP_SEPARATOR.join(str(stepNo) for stepNo in blockingSteps),
        cycleId=cycleId,
    )


def CheckFinancialReload(context: JobContext, loaderName: str) -> str | None:
    """Return the refusal text when a load would undo later steps of the OPEN cycle, else None.

    Only the FINANCIAL_RELOADS loaders are ever refused, and only while a later step that writes
    their table is COMPLETED, FAILED or INTERRUPTED in the OPEN cycle (Section 0.6.8, Financial
    reloads). --force never overrides the refusal.
    """
    laterSteps = FINANCIAL_RELOADS.get(loaderName)
    if laterSteps is None:
        return None
    with context.engine.connect() as connection:
        cycleId = FindOpenCycle(connection)
        if cycleId is None:
            return None
        blockingSteps = BlockingSteps(ReadStepStates(connection, cycleId), laterSteps)
    if not blockingSteps:
        return None
    return FormatReloadRefusal(loaderName, blockingSteps, cycleId)
