"""Replaces: WAITSTEP (COBSWAIT), scheduler/CardDemo.ca7, scheduler/CardDemo.controlm (job
chains and COND=(4,LT) condition-code chaining).

The sequential in-process runner needs no timed wait: WAITSTEP's 36-second COBSWAIT between
the Control-M jobs is superseded.

The 18-step chain and the entry points of the carddemo-batch commands, specified in the
Technical Specification, Sections 0.6.8 and 0.8.2, and transcribed in docs/batch.md ("Chain
order", "Cycles and step identity", "CLI usage"): RunChain (run-all), RunSeedAll (seed-all),
RunStandaloneJob (job) and RunStandaloneLoad (load). batch/cli.py calls each inside the batch
window, after run_ledger.ClassifyInterruptedRuns and gdg.ReconcileCatalog, and exits with the
status it returns: 0 and 4 continue the chain, 8 or above stops it, as COND=(4,LT) did.

A step's generations are published by FinishStep once its runner has returned with the final
transaction committed, outside InvokeRunner's runner-failure mapping. A publication error, or a
generation found LOST, ends the step with EXIT_FAILURE and stops the chain while the step stays
COMPLETED: the next command's ReconcileCatalog completes the publication, and a LOST generation
blocks its cycle, which never completes, until seed-all or run-all --new-cycle abandons it.

Runners are named by module path and imported only when a step runs. Loaders and jobs import
job_context, run_ledger and gdg, so importing them here would form a cycle, would load every
job for `carddemo-batch --help`, and would keep a test's monkeypatched RunLoader or RunJob from
being the function the chain calls. All SQL lives in run_ledger and gdg; this module issues none.
"""

import errno
import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace

from sqlalchemy.exc import DBAPIError

from batch.orchestration import gdg, run_ledger
from batch.orchestration.job_context import (
    CHAIN_STOP_THRESHOLD,
    EXIT_ABEND,
    EXIT_FAILURE,
    EXIT_OK,
    JobContext,
    JobFailure,
    JobResult,
)

LOGGER = logging.getLogger(__name__)

# The durability and rerun class of a step, which decides how a failure or an interruption
# leaves it (docs/batch.md, "Step table").
DURABILITY_ATOMIC = "atomic"
DURABILITY_PER_WRITE = "per-write"
DURABILITY_FILES_ONLY = "files-only"

# The function each runner module exposes.
LOADER_ENTRY_POINT = "RunLoader"
JOB_ENTRY_POINT = "RunJob"

# What the chain does with a step, from its state in the cycle.
DECISION_EXECUTE = "execute"
DECISION_SKIP = "skip"
DECISION_REFUSE = "refuse"
# A per-write step in these states keeps the partial effects of its failed or interrupted run.
PER_WRITE_REFUSED_STATES = (run_ledger.STEP_FAILED, run_ledger.STEP_INTERRUPTED)

# Operator messages. Each is logged as exactly this text, refusals and failures at ERROR and
# progress at INFO, so subprocess tests can look for it in the output.
NO_OPEN_CYCLE_MESSAGE = "NO OPEN CYCLE; RUN run-all OR seed-all"
# RunChain logs UNKNOWN STEP for a ChainOptions built in code. The CLI refuses the same values
# while parsing --start-at, as a usage error stating this rule only, never the value: a mistyped
# card number is not repeated.
UNKNOWN_STEP_TEMPLATE = "UNKNOWN STEP: {name}"
START_AT_RULE_MESSAGE = (
    f"--start-at must be a step number 1-{run_ledger.CHAIN_STEP_COUNT} or a job or loader name,"
    " such as 13, post-tran or POSTTRAN"
)
NEW_CYCLE_START_REFUSED_TEMPLATE = (
    "A NEW CYCLE STARTS AT STEP 1, NOT STEP {stepNo} {jobName}; --start-at RESUMES ONLY AN"
    " OPEN CYCLE"
)
UNKNOWN_JOB_TEMPLATE = "UNKNOWN JOB: {name}"
UNKNOWN_LOADER_TEMPLATE = "UNKNOWN LOADER: {name}"
OCCURRENCE_NOT_ALLOWED_TEMPLATE = "--occurrence APPLIES TO tran-backup ONLY, NOT {name}"
OCCURRENCE_OUT_OF_RANGE_TEMPLATE = "--occurrence OF {name} MUST BE 1 TO {count}, NOT {occurrence}"
PER_WRITE_REFUSED_TEMPLATE = (
    "STEP {stepNo} {jobName} OF CYCLE {cycleId} IS {state}; RESTORE WITH seed-all OR"
    " run-all --new-cycle, OR RERUN FROM THE PARTIAL STATE WITH --force"
)
STEP_SKIPPED_TEMPLATE = "STEP {stepNo} {jobName} ALREADY COMPLETED IN CYCLE {cycleId}; SKIPPED"
STEP_ENDED_TEMPLATE = "STEP {stepNo} {jobName} ENDED RC={exitCode}"
# A load made with no OPEN cycle has no step, so its line names the run, as its STARTED line does.
LOAD_ENDED_TEMPLATE = "RUN {runId} {jobName} ENDED RC={exitCode}; NO CYCLE STEP"
RETURNED_FAILURE_TEMPLATE = "{jobName} RETURNED RC={exitCode} INSTEAD OF RAISING JobFailure"
# The database and file errors a runner did not map to JobFailure itself.
DATABASE_ERROR_TEMPLATE = (
    "{jobName}: DATABASE ERROR {errorClass} ({driverClass}) SQLSTATE {sqlState}"
)
FILE_ERROR_TEMPLATE = "{jobName}: FILE ERROR {errorClass} ERRNO {errnoName}"
UNKNOWN_CODE = "UNKNOWN"
# A database or file error publishing the generation of a run that has already committed.
PUBLICATION_ERROR_TEMPLATE = (
    "STEP {stepNo} {ioError} WHILE PUBLISHING ITS GENERATION; THE STEP STAYS COMPLETED AND THE"
    " NEXT COMMAND RECONCILES THE GENERATION"
)


@dataclass(frozen=True)
class ModuleEntryPoint:
    """A runner named by module path and function, resolved each time a step calls it."""

    moduleName: str
    attributeName: str

    def __call__(self, context: JobContext) -> JobResult:
        """Import the module when first needed and run its entry point with the step's context."""
        runner = getattr(importlib.import_module(self.moduleName), self.attributeName)
        return runner(context)


@dataclass(frozen=True)
class JobDefinition:
    """A loader or processing job: its canonical CLI name, its runner and its legacy aliases.

    aliases[0] is the legacy JCL name, which batch.job_runs.job_name records, as Alembic 0008
    does for the seed cycle (ACCTFILE ... DALYTRAN).
    """

    name: str
    runner: Callable[[JobContext], JobResult]
    aliases: tuple[str, ...]

    def LegacyName(self) -> str:
        """Return the legacy JCL name of the job, for the run history and the step log lines."""
        return self.aliases[0]


def LoaderDefinition(name: str, moduleName: str, legacyName: str) -> JobDefinition:
    """Return the definition of a loader module, which exposes RunLoader(context)."""
    return JobDefinition(name, ModuleEntryPoint(moduleName, LOADER_ENTRY_POINT), (legacyName,))


def ProcessingJobDefinition(name: str, moduleName: str, legacyName: str) -> JobDefinition:
    """Return the definition of a processing job module, which exposes RunJob(context)."""
    return JobDefinition(name, ModuleEntryPoint(moduleName, JOB_ENTRY_POINT), (legacyName,))


# Every loader and processing job, keyed by canonical CLI name. Each module's JOB_NAME and
# JCL_NAME equal the canonical name and the legacy alias here.
JOB_DEFINITIONS: dict[str, JobDefinition] = {
    definition.name: definition
    for definition in (
        LoaderDefinition("accounts", "batch.loaders.acctfile", "ACCTFILE"),
        LoaderDefinition("cards", "batch.loaders.cardfile", "CARDFILE"),
        LoaderDefinition("customers", "batch.loaders.custfile", "CUSTFILE"),
        LoaderDefinition("card-xref", "batch.loaders.xreffile", "XREFFILE"),
        LoaderDefinition("transactions", "batch.loaders.tranfile", "TRANFILE"),
        LoaderDefinition("tran-types", "batch.loaders.trantype", "TRANTYPE"),
        LoaderDefinition("tran-categories", "batch.loaders.trancatg", "TRANCATG"),
        LoaderDefinition("disclosure-groups", "batch.loaders.discgrp", "DISCGRP"),
        LoaderDefinition("tran-cat-balances", "batch.loaders.tcatbalf", "TCATBALF"),
        LoaderDefinition("init-users", "batch.loaders.security_init", "DUSRSECJ"),
        LoaderDefinition("daily-transactions", "batch.loaders.dalytran", "DALYTRAN"),
        ProcessingJobDefinition("tran-backup", "batch.jobs.tranbkp", "TRANBKP"),
        ProcessingJobDefinition("post-tran", "batch.jobs.posttran", "POSTTRAN"),
        ProcessingJobDefinition("interest-calc", "batch.jobs.interest_calc", "INTCALC"),
        ProcessingJobDefinition("comb-tran", "batch.jobs.combtran", "COMBTRAN"),
        ProcessingJobDefinition("statement-gen", "batch.jobs.statement_gen.creastmt", "CREASTMT"),
        ProcessingJobDefinition("tran-index", "batch.jobs.tranidx", "TRANIDX"),
    )
}

# The `load` and `job` command names, in the order batch/cli.py lists them.
LOADER_JOB_NAMES = (
    "accounts",
    "cards",
    "customers",
    "card-xref",
    "transactions",
    "tran-types",
    "tran-categories",
    "disclosure-groups",
    "tran-cat-balances",
    "init-users",
    "daily-transactions",
)
PROCESSING_JOB_NAMES = (
    "post-tran",
    "interest-calc",
    "tran-backup",
    "comb-tran",
    "statement-gen",
    "tran-index",
)


@dataclass(frozen=True)
class ChainStep:
    """One step of the chain: its number, its job and its durability and rerun class."""

    stepNo: int
    job: JobDefinition
    durability: str


# The chain in legacy README order (docs/batch.md, "Chain order"). Steps 6 and 15 share the one
# TRANBKP definition; the step number, not the job, is the step's identity in a cycle.
CHAIN: tuple[ChainStep, ...] = (
    ChainStep(1, JOB_DEFINITIONS["accounts"], DURABILITY_ATOMIC),
    ChainStep(2, JOB_DEFINITIONS["cards"], DURABILITY_ATOMIC),
    ChainStep(3, JOB_DEFINITIONS["customers"], DURABILITY_ATOMIC),
    ChainStep(4, JOB_DEFINITIONS["card-xref"], DURABILITY_ATOMIC),
    ChainStep(5, JOB_DEFINITIONS["transactions"], DURABILITY_ATOMIC),
    ChainStep(6, JOB_DEFINITIONS["tran-backup"], DURABILITY_ATOMIC),
    ChainStep(7, JOB_DEFINITIONS["tran-types"], DURABILITY_ATOMIC),
    ChainStep(8, JOB_DEFINITIONS["tran-categories"], DURABILITY_ATOMIC),
    ChainStep(9, JOB_DEFINITIONS["disclosure-groups"], DURABILITY_ATOMIC),
    ChainStep(10, JOB_DEFINITIONS["tran-cat-balances"], DURABILITY_ATOMIC),
    ChainStep(11, JOB_DEFINITIONS["init-users"], DURABILITY_ATOMIC),
    ChainStep(12, JOB_DEFINITIONS["daily-transactions"], DURABILITY_ATOMIC),
    ChainStep(13, JOB_DEFINITIONS["post-tran"], DURABILITY_PER_WRITE),
    ChainStep(14, JOB_DEFINITIONS["interest-calc"], DURABILITY_PER_WRITE),
    ChainStep(15, JOB_DEFINITIONS["tran-backup"], DURABILITY_ATOMIC),
    ChainStep(16, JOB_DEFINITIONS["comb-tran"], DURABILITY_ATOMIC),
    ChainStep(17, JOB_DEFINITIONS["statement-gen"], DURABILITY_FILES_ONLY),
    ChainStep(18, JOB_DEFINITIONS["tran-index"], DURABILITY_ATOMIC),
)
FIRST_STEP = CHAIN[0]
FINAL_STEP = CHAIN[-1]

# The loader steps, 1-5 and 7-12, which seed-all runs.
LOADER_STEP_NUMBERS = tuple(step.stepNo for step in CHAIN if step.job.name in LOADER_JOB_NAMES)


@dataclass(frozen=True)
class ChainOptions:
    """The run-all options: --new-cycle, --start-at STEP and --force."""

    newCycle: bool = False
    startAt: str | None = None
    force: bool = False


@dataclass(frozen=True)
class JobRequest:
    """The job command: the job's name or alias, --occurrence and --force."""

    jobName: str
    occurrence: int | None = None
    force: bool = False


def FindJobDefinition(nameOrAlias: str) -> JobDefinition:
    """Return the job a canonical name or legacy alias names, in any letter case.

    Raises KeyError(nameOrAlias) when no loader or job has that name.
    """
    wantedName = nameOrAlias.lower()
    for definition in JOB_DEFINITIONS.values():
        if wantedName in (name.lower() for name in (definition.name, *definition.aliases)):
            return definition
    raise KeyError(nameOrAlias)


def StepsForJob(definition: JobDefinition) -> tuple[ChainStep, ...]:
    """Return the chain steps a job runs as: steps 6 and 15 for TRANBKP, one step otherwise."""
    return tuple(step for step in CHAIN if step.job.name == definition.name)


def PrintableText(text: str) -> str:
    """Return operator input for a log line, every non-printable character escaped."""
    return "".join(
        character if character.isprintable() else repr(character)[1:-1] for character in text
    )


def OpenCycleId(context: JobContext) -> int | None:
    """Return the OPEN cycle's id, the Alembic seed cycle included, or None."""
    with context.engine.connect() as connection:
        return run_ledger.FindOpenCycle(connection)


def LogStepEnded(stepContext: JobContext, step: ChainStep, exitCode: int) -> None:
    """Log a run's exit status: at INFO when the chain continues, at ERROR when it stops.

    A run of a cycle step names the run's step number. A load made with no OPEN cycle, whose
    history row has no cycle and no step, names its run instead, as its STARTED line does.
    """
    jobName = step.job.LegacyName()
    if stepContext.cycleId is None or stepContext.stepNo is None:
        message = LOAD_ENDED_TEMPLATE.format(
            runId=stepContext.runId, jobName=jobName, exitCode=exitCode
        )
    else:
        message = STEP_ENDED_TEMPLATE.format(
            stepNo=stepContext.stepNo, jobName=jobName, exitCode=exitCode
        )
    if exitCode >= CHAIN_STOP_THRESHOLD:
        LOGGER.error(message)
    else:
        LOGGER.info(message)


def DescribeIoError(step: ChainStep, error: DBAPIError | OSError) -> str:
    """Describe a database or file error by class, SQLSTATE or errno symbol.

    Never str(error): a driver message can quote row values such as card numbers and SSNs,
    which the batch diagnostics policy of batch/db.py keeps out of every log line.
    """
    jobName = step.job.LegacyName()
    errorClass = type(error).__name__
    if isinstance(error, DBAPIError):
        driverError = error.orig
        sqlState = getattr(driverError, "pgcode", None) or UNKNOWN_CODE
        driverClass = UNKNOWN_CODE if driverError is None else type(driverError).__name__
        return DATABASE_ERROR_TEMPLATE.format(
            jobName=jobName, errorClass=errorClass, driverClass=driverClass, sqlState=sqlState
        )
    errnoName = errno.errorcode.get(error.errno, UNKNOWN_CODE) if error.errno else UNKNOWN_CODE
    return FILE_ERROR_TEMPLATE.format(jobName=jobName, errorClass=errorClass, errnoName=errnoName)


def HandleFailure(stepContext: JobContext, step: ChainStep, exitCode: int, message: str) -> int:
    """Record the run and its step FAILED and return the failure's exit status.

    A status below CHAIN_STOP_THRESHOLD, which a runner must never fail with, is recorded as
    EXIT_FAILURE so the chain still stops. The catalog is then reconciled, which deletes any
    .part file a rolled-back atomic step left behind, and the run's ENDED line is logged.
    """
    failureExitCode = exitCode if exitCode >= CHAIN_STOP_THRESHOLD else EXIT_FAILURE
    LOGGER.error(message)
    run_ledger.RecordFailure(stepContext, stepContext.runId, failureExitCode)
    gdg.ReconcileCatalog(stepContext)
    LogStepEnded(stepContext, step, failureExitCode)
    return failureExitCode


def PublishStepGenerations(stepContext: JobContext, step: ChainStep) -> int:
    """Publish the generations of a committed run; return EXIT_OK, or EXIT_FAILURE to stop.

    The run and its step stay COMPLETED either way, so no failure is recorded. A database or
    file error leaves the generation COMMITTED for the next command's ReconcileCatalog, and a
    generation found LOST blocks the OPEN cycle until seed-all or run-all --new-cycle. Either
    stops the chain, so no later step resolves an older generation in its place.
    """
    try:
        gdg.PublishRunGenerations(stepContext)
        gdg.CheckNoLostGenerations(stepContext)
    except (DBAPIError, OSError) as error:
        message = PUBLICATION_ERROR_TEMPLATE.format(
            stepNo=step.stepNo, ioError=DescribeIoError(step, error)
        )
        LOGGER.error(message)
        return EXIT_FAILURE
    except gdg.LostGenerationError as error:
        LOGGER.error(error.message)
        return EXIT_FAILURE
    return EXIT_OK


def FinishStep(stepContext: JobContext, step: ChainStep, result: JobResult) -> int:
    """Complete a run whose runner returned, publish its generations and return its status.

    FinishIfRunning finishes per-write and files-only runs and is a no-op for an atomic runner
    that already finished inside its own transaction. Publication then runs once, after that
    commit: a generation it does not publish makes the status EXIT_FAILURE, while the run and
    its step stay COMPLETED. The run's ENDED line is logged last.
    """
    if result.exitCode >= CHAIN_STOP_THRESHOLD:
        message = RETURNED_FAILURE_TEMPLATE.format(
            jobName=step.job.LegacyName(), exitCode=result.exitCode
        )
        return HandleFailure(stepContext, step, result.exitCode, message)
    run_ledger.FinishIfRunning(stepContext, result)
    exitCode = max(result.exitCode, PublishStepGenerations(stepContext, step))
    LogStepEnded(stepContext, step, exitCode)
    return exitCode


def IoFailureExitCode(step: ChainStep) -> int:
    """Return the status of an unmapped I/O error: abend 999 for a per-write step.

    An atomic or files-only step has rolled back completely, so its failure is recoverable.
    """
    if step.durability == DURABILITY_PER_WRITE:
        return EXIT_ABEND
    return EXIT_FAILURE


def InvokeRunner(stepContext: JobContext, step: ChainStep) -> int:
    """Run the step's runner and map its outcome to an exit status.

    Only the documented failures are caught. Any other exception propagates with the run still
    RUNNING, and the next command's ClassifyInterruptedRuns reclassifies it INTERRUPTED.
    """
    try:
        result = step.job.runner(stepContext)
    except JobFailure as failure:
        return HandleFailure(stepContext, step, failure.exitCode, failure.message)
    except gdg.LostGenerationError as error:
        return HandleFailure(stepContext, step, EXIT_FAILURE, error.message)
    except (DBAPIError, OSError) as error:
        message = DescribeIoError(step, error)
        return HandleFailure(stepContext, step, IoFailureExitCode(step), message)
    # Outside the exception mapping: the runner has returned with its writes committed, so
    # neither finishing nor publishing the run is a step failure. FinishStep reports a
    # publication error or a LOST generation itself, leaving the step COMPLETED; an error
    # finishing a RUNNING run propagates, for the next command's ClassifyInterruptedRuns.
    return FinishStep(stepContext, step, result)


def RefusedByFinancialReload(context: JobContext, step: ChainStep) -> bool:
    """Log and return True while the step's loader would undo later steps of the OPEN cycle.

    run_ledger.CheckFinancialReload refuses the accounts, transactions and tran-cat-balances
    loaders while a later step that writes their table is COMPLETED, FAILED or INTERRUPTED in
    the OPEN cycle. Nothing overrides the refusal, --force included.
    """
    refusal = run_ledger.CheckFinancialReload(context, step.job.name)
    if refusal is None:
        return False
    LOGGER.error(refusal)
    return True


def ExecuteStep(context: JobContext, step: ChainStep) -> int:
    """Start a run of the step, committed RUNNING before the runner starts, and run it.

    Every execution of a step, by run-all, job or load, passes through here, so a financial
    reload the OPEN cycle refuses returns EXIT_FAILURE first, having written no run and no row,
    even when run-all forces it with --start-at and --force. A load made with no OPEN cycle is
    recorded with no cycle and no step number.
    """
    if RefusedByFinancialReload(context, step):
        return EXIT_FAILURE
    stepNo = step.stepNo if context.cycleId is not None else None
    runStart = run_ledger.RunStart(
        context.cycleId, stepNo, step.job.LegacyName(), run_ledger.BuildRunParams(context)
    )
    runId = run_ledger.StartRun(context, runStart)
    return InvokeRunner(context.ForRun(context.cycleId, stepNo, runId), step)


def DecideStep(context: JobContext, step: ChainStep, forced: bool) -> str:
    """Return whether the step executes, is skipped or is refused, from its state in the cycle.

    A COMPLETED step is skipped. A per-write step left FAILED or INTERRUPTED carries partial
    effects and is refused. Everything else executes: PENDING, a FAILED atomic step, which
    rolled back and is retried, and any forced step.
    """
    if forced:
        return DECISION_EXECUTE
    stepState = run_ledger.StepState(context, context.cycleId, step.stepNo)
    if stepState == run_ledger.STEP_COMPLETED:
        return DECISION_SKIP
    if step.durability == DURABILITY_PER_WRITE and stepState in PER_WRITE_REFUSED_STATES:
        return DECISION_REFUSE
    return DECISION_EXECUTE


def RefuseStep(context: JobContext, step: ChainStep) -> int:
    """Log the refusal of a failed or interrupted per-write step and return EXIT_FAILURE."""
    message = PER_WRITE_REFUSED_TEMPLATE.format(
        stepNo=step.stepNo,
        jobName=step.job.LegacyName(),
        cycleId=context.cycleId,
        state=run_ledger.StepState(context, context.cycleId, step.stepNo),
    )
    LOGGER.error(message)
    return EXIT_FAILURE


def SkipStep(context: JobContext, step: ChainStep) -> int:
    """Record a SKIPPED history row for a COMPLETED step and return EXIT_OK."""
    run_ledger.RecordSkip(context, step.stepNo, step.job.LegacyName())
    message = STEP_SKIPPED_TEMPLATE.format(
        stepNo=step.stepNo, jobName=step.job.LegacyName(), cycleId=context.cycleId
    )
    LOGGER.info(message)
    return EXIT_OK


def ProcessStep(context: JobContext, step: ChainStep, forced: bool) -> int:
    """Skip, refuse or execute one step of the context's cycle and return its exit status."""
    decision = DecideStep(context, step, forced)
    if decision == DECISION_SKIP:
        return SkipStep(context, step)
    if decision == DECISION_REFUSE:
        return RefuseStep(context, step)
    return ExecuteStep(context, step)


def CompleteCycleIfDone(context: JobContext) -> None:
    """Mark the context's cycle COMPLETED once its final step, TRANIDX, is COMPLETED.

    A cycle with a LOST generation stays OPEN, so it stays blocked until seed-all or
    run-all --new-cycle abandons it.
    """
    finalState = run_ledger.StepState(context, context.cycleId, FINAL_STEP.stepNo)
    if finalState == run_ledger.STEP_COMPLETED and not gdg.FindLostGenerations(context):
        run_ledger.CompleteCycle(context)


def RunSteps(context: JobContext, steps: tuple[ChainStep, ...], forcedStepNo: int | None) -> int:
    """Process steps in order and return the highest exit status among them.

    Only the step numbered forcedStepNo is forced. As COND=(4,LT) bypassed the later steps,
    the run stops after the first status at or above CHAIN_STOP_THRESHOLD; 0 and 4 continue,
    and a skipped step counts as 0.
    """
    highestExitCode = EXIT_OK
    for step in steps:
        exitCode = ProcessStep(context, step, step.stepNo == forcedStepNo)
        highestExitCode = max(highestExitCode, exitCode)
        if exitCode >= CHAIN_STOP_THRESHOLD:
            break
    CompleteCycleIfDone(context)
    return highestExitCode


def ParseStartAt(startAt: str) -> tuple[ChainStep, ...]:
    """Return the candidate start steps --start-at names: a step number 1-18, or a job name.

    A job name or legacy alias matches in any letter case; TRANBKP names both its steps.
    Raises KeyError for anything else, before any cycle is opened or abandoned. A number is
    bounded to the digits of the last step, leading zeros aside, before int() converts it, so
    a number of any length is refused the same way.
    """
    if startAt.isascii() and startAt.isdigit():
        significantDigits = startAt.lstrip("0")
        if len(significantDigits) > len(str(run_ledger.CHAIN_STEP_COUNT)):
            raise KeyError(startAt)
        stepNo = int(significantDigits or "0")
        if stepNo not in run_ledger.CHAIN_STEP_NUMBERS:
            raise KeyError(startAt)
        return (CHAIN[stepNo - 1],)
    return StepsForJob(FindJobDefinition(startAt))


def ChooseOccurrence(
    context: JobContext, candidates: tuple[ChainStep, ...], occurrence: int | None
) -> ChainStep:
    """Return the step a job runs as in the context's cycle.

    An explicit occurrence names it (1 is step 6, 2 is step 15). Otherwise it is the first
    candidate not yet COMPLETED, or the last candidate when all are.
    """
    if occurrence is not None:
        return candidates[occurrence - 1]
    if len(candidates) == 1:
        return candidates[0]
    stepStates = run_ledger.StepStates(context, context.cycleId)
    return next(
        (step for step in candidates if stepStates[step.stepNo] != run_ledger.STEP_COMPLETED),
        candidates[-1],
    )


def FirstIncompleteStep(context: JobContext) -> ChainStep | None:
    """Return the first chain step not yet COMPLETED in the context's cycle, or None."""
    stepStates = run_ledger.StepStates(context, context.cycleId)
    return next(
        (step for step in CHAIN if stepStates[step.stepNo] != run_ledger.STEP_COMPLETED), None
    )


def FirstRefusedStep(
    context: JobContext, steps: tuple[ChainStep, ...], forcedStepNo: int | None
) -> ChainStep | None:
    """Return the first of the steps run-all must refuse in the context's cycle, or None.

    That is a per-write step left FAILED or INTERRUPTED, other than the forced step. Its partial
    effects survive only while no step runs before the refusal: an earlier TRANBKP would archive
    and delete the postings a forced rerun has to start from.
    """
    stepStates = run_ledger.StepStates(context, context.cycleId)
    return next(
        (
            step
            for step in steps
            if step.durability == DURABILITY_PER_WRITE
            and step.stepNo != forcedStepNo
            and stepStates[step.stepNo] in PER_WRITE_REFUSED_STATES
        ),
        None,
    )


def RefusedBeforeExecution(
    context: JobContext, steps: tuple[ChainStep, ...], forcedStepNo: int | None
) -> bool:
    """Log and return True when run-all must refuse one of its steps before executing any.

    The step refused is the first FirstRefusedStep finds. A financial loader that run-all would
    execute before it is refused by the reload rule in its place, as run-all reaches the loader
    first. Either way no step executes and no run is recorded.
    """
    refusedStep = FirstRefusedStep(context, steps, forcedStepNo)
    if refusedStep is None:
        return False
    for step in steps[: steps.index(refusedStep)]:
        decision = DecideStep(context, step, step.stepNo == forcedStepNo)
        if decision == DECISION_EXECUTE and RefusedByFinancialReload(context, step):
            return True
    RefuseStep(context, refusedStep)
    return True


def RefusedByLostGenerations(context: JobContext) -> bool:
    """Log and return True while the OPEN cycle has a LOST generation, which blocks it."""
    try:
        gdg.CheckNoLostGenerations(context)
    except gdg.LostGenerationError as error:
        LOGGER.error(error.message)
        return True
    return False


def RefusedByNewCycleStart(
    context: JobContext, newCycle: bool, candidates: tuple[ChainStep, ...] | None
) -> bool:
    """Log and return True when --start-at names a later step for a cycle run-all would open.

    A new cycle, from --new-cycle or because none is OPEN, always starts from the loaders. Every
    step of it is PENDING, so its start step would be the first candidate, and only step 1 may
    be. Checked before ChainCycle, a refusal abandons and opens no cycle.
    """
    if candidates is None or candidates[0].stepNo == FIRST_STEP.stepNo:
        return False
    if not newCycle and OpenCycleId(context) is not None:
        return False
    startStep = candidates[0]
    message = NEW_CYCLE_START_REFUSED_TEMPLATE.format(
        stepNo=startStep.stepNo, jobName=startStep.job.LegacyName()
    )
    LOGGER.error(message)
    return True


def ChainCycle(context: JobContext, newCycle: bool) -> int:
    """Return the cycle run-all runs in: a new one for --new-cycle or when none is OPEN.

    --new-cycle first abandons the OPEN cycle and its LOST generations.
    """
    if newCycle:
        run_ledger.AbandonOpenCycle(context)
        return run_ledger.OpenCycle(context, run_ledger.OPENED_BY_RUN_ALL)
    openCycleId = OpenCycleId(context)
    if openCycleId is not None:
        return openCycleId
    return run_ledger.OpenCycle(context, run_ledger.OPENED_BY_RUN_ALL)


def PlanChainSteps(
    cycleContext: JobContext, candidates: tuple[ChainStep, ...] | None, force: bool
) -> tuple[tuple[ChainStep, ...], int | None]:
    """Return the steps run-all processes and the one step --force applies to, if any.

    With --start-at the steps run from the start step on, and steps before it are not
    considered. Without it every step is processed, so completed ones get SKIPPED rows. --force
    without --start-at applies to the cycle's first FAILED or INTERRUPTED per-write step, run
    from that step on as --start-at STEP --force runs it, so no earlier step runs over its
    partial state; with none, to the first step not yet COMPLETED. It never applies implicitly.
    """
    if candidates is not None:
        start = ChooseOccurrence(cycleContext, candidates, None)
        return CHAIN[start.stepNo - 1 :], start.stepNo if force else None
    if not force:
        return CHAIN, None
    refusedStep = FirstRefusedStep(cycleContext, CHAIN, None)
    if refusedStep is not None:
        return CHAIN[refusedStep.stepNo - 1 :], refusedStep.stepNo
    firstIncomplete = FirstIncompleteStep(cycleContext)
    return CHAIN, None if firstIncomplete is None else firstIncomplete.stepNo


def RunChain(context: JobContext, options: ChainOptions) -> int:
    """Run or resume the 18-step chain for run-all (Technical Specification 0.6.8).

    A FAILED or INTERRUPTED per-write step among the planned steps, other than the forced one,
    is refused before any step executes (RefusedBeforeExecution), so the refusal records no run
    and changes nothing.
    """
    candidates = None
    if options.startAt is not None:
        try:
            candidates = ParseStartAt(options.startAt)
        except KeyError:
            LOGGER.error(UNKNOWN_STEP_TEMPLATE.format(name=PrintableText(options.startAt)))
            return EXIT_FAILURE
    if not options.newCycle and RefusedByLostGenerations(context):
        return EXIT_FAILURE
    if RefusedByNewCycleStart(context, options.newCycle, candidates):
        return EXIT_FAILURE
    cycleContext = replace(context, cycleId=ChainCycle(context, options.newCycle))
    steps, forcedStepNo = PlanChainSteps(cycleContext, candidates, options.force)
    if RefusedBeforeExecution(cycleContext, steps, forcedStepNo):
        return EXIT_FAILURE
    return RunSteps(cycleContext, steps, forcedStepNo)


def RunSeedAll(context: JobContext) -> int:
    """Run loader steps 1-5 and 7-12 in a new cycle for seed-all (Technical Specification 0.6.8)."""
    run_ledger.AbandonOpenCycle(context)
    cycleId = run_ledger.OpenCycle(context, run_ledger.OPENED_BY_SEED_ALL)
    loaderSteps = tuple(step for step in CHAIN if step.stepNo in LOADER_STEP_NUMBERS)
    return RunSteps(replace(context, cycleId=cycleId), loaderSteps, None)


def FindNamedDefinition(name: str, allowedNames: tuple[str, ...]) -> JobDefinition | None:
    """Return the definition a name or alias selects among allowedNames, or None."""
    try:
        definition = FindJobDefinition(name)
    except KeyError:
        return None
    if definition.name not in allowedNames:
        return None
    return definition


def OccurrenceError(definition: JobDefinition, occurrence: int | None) -> str | None:
    """Return why --occurrence does not fit the job, or None when it fits or is absent."""
    if occurrence is None:
        return None
    occurrenceCount = len(StepsForJob(definition))
    if occurrenceCount == 1:
        return OCCURRENCE_NOT_ALLOWED_TEMPLATE.format(name=definition.name)
    if not 1 <= occurrence <= occurrenceCount:
        return OCCURRENCE_OUT_OF_RANGE_TEMPLATE.format(
            name=definition.name, count=occurrenceCount, occurrence=occurrence
        )
    return None


def ValidatedJobDefinition(request: JobRequest) -> JobDefinition | None:
    """Return the processing job the request names, or log why it is refused and return None."""
    definition = FindNamedDefinition(request.jobName, PROCESSING_JOB_NAMES)
    if definition is None:
        LOGGER.error(UNKNOWN_JOB_TEMPLATE.format(name=PrintableText(request.jobName)))
        return None
    occurrenceError = OccurrenceError(definition, request.occurrence)
    if occurrenceError is not None:
        LOGGER.error(occurrenceError)
        return None
    return definition


def RunStandaloneJob(context: JobContext, request: JobRequest) -> int:
    """Run one job as its step of the OPEN cycle for job (Technical Specification 0.6.8)."""
    definition = ValidatedJobDefinition(request)
    if definition is None or RefusedByLostGenerations(context):
        return EXIT_FAILURE
    openCycleId = OpenCycleId(context)
    if openCycleId is None:
        LOGGER.error(NO_OPEN_CYCLE_MESSAGE)
        return EXIT_FAILURE
    cycleContext = replace(context, cycleId=openCycleId)
    step = ChooseOccurrence(cycleContext, StepsForJob(definition), request.occurrence)
    exitCode = ProcessStep(cycleContext, step, request.force)
    CompleteCycleIfDone(cycleContext)
    return exitCode


def RunStandaloneLoad(context: JobContext, loaderName: str) -> int:
    """Run one loader for load, in the OPEN cycle if any (Technical Specification 0.6.8).

    An unknown loader and a LOST generation are refused here; a financial reload the OPEN cycle
    refuses is refused by ExecuteStep, as it is when run-all reaches the loader's step.
    """
    definition = FindNamedDefinition(loaderName, LOADER_JOB_NAMES)
    if definition is None:
        LOGGER.error(UNKNOWN_LOADER_TEMPLATE.format(name=PrintableText(loaderName)))
        return EXIT_FAILURE
    if RefusedByLostGenerations(context):
        return EXIT_FAILURE
    # A replace-load gives the same state every time, so it executes whatever its step's state
    # unless ExecuteStep refuses a financial reload, and it never opens, closes or abandons a
    # cycle.
    loadContext = replace(context, cycleId=OpenCycleId(context))
    return ExecuteStep(loadContext, StepsForJob(definition)[0])
