"""The carddemo-batch command-line interface: run-all, seed-all, job and load.

Replaces: CardDemo.ca7, CardDemo.controlm (scheduler); CLOSEFIL, OPENFIL, WAITSTEP (jcl)

The CA-7 and Control-M job submission becomes these commands, and the EXEC PARM= and DD
overrides of the job JCL become their options. Every command runs inside BatchWindow, which
supersedes the CLOSEFIL and OPENFIL brackets; the sequential runner needs no WAITSTEP. Inside
the window, interrupted runs are reclassified and the generation catalog is reconciled before
the command's own work. Cycle, skip, resume, failure, force, LOST-generation and reload policy
lives in batch.orchestration.batch_chain and run_ledger. This module parses options and exits
with the status the chain returns: 0 and 4 let a chain continue, 8 or above stops it, and a
usage error exits 2.

The four command forms, with optional arguments in square brackets:

```text
carddemo-batch run-all [--new-cycle] [--start-at STEP] [--force]
carddemo-batch seed-all
carddemo-batch job JOB_NAME [--occurrence OCCURRENCE] [--force]
carddemo-batch load LOADER_NAME
```

Replace STEP with a chain step number from 1 to 18 or a job name, JOB_NAME with a processing
job such as post-tran, OCCURRENCE with 1 or 2 (tran-backup only), and LOADER_NAME with a loader
such as accounts. Legacy JCL names such as POSTTRAN and ACCTFILE are accepted in any letter
case. Each command's --help lists its options.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from app.core.logging import ConfigureLogging
from app.seed.image_reader import ENCODING_ASCII, ENCODING_EBCDIC
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from batch.config import BatchSettings, LoadBatchSettings
from batch.db import BuildEngine
from batch.orchestration import gdg, run_ledger
from batch.orchestration.batch_chain import (
    LOADER_JOB_NAMES,
    PROCESSING_JOB_NAMES,
    ChainOptions,
    FindJobDefinition,
    JobRequest,
    RunChain,
    RunSeedAll,
    RunStandaloneJob,
    RunStandaloneLoad,
)
from batch.orchestration.batch_window import (
    BATCH_WINDOW_UNAVAILABLE_MESSAGE,
    BatchWindow,
    BatchWindowUnavailableError,
)
from batch.orchestration.clock import Clock, ParseAsOf
from batch.orchestration.job_context import (
    DEFAULT_RUN_DATE,
    EXIT_FAILURE,
    RUN_DATE_LENGTH,
    JobContext,
    JobParameters,
)

LOGGER = logging.getLogger(__name__)

# Batch operator messages, not legacy literals. Neither ever carries the database URL or a
# rejected setting value: only exception class names and setting names are logged.
DATABASE_UNAVAILABLE_MESSAGE = "DATABASE NOT AVAILABLE"
SETTINGS_INVALID_MESSAGE = "BATCH SETTINGS INVALID"
DATABASE_UNAVAILABLE_LOG = DATABASE_UNAVAILABLE_MESSAGE + " (%s)"
SETTINGS_INVALID_LOG = SETTINGS_INVALID_MESSAGE + ": %d error(s) in %s"
SETTINGS_INVALID_DETAIL_LOG = SETTINGS_INVALID_MESSAGE + ": %s"
SETTING_NAME_SEPARATOR = ", "

# The canonical names of the processing jobs, as batch_chain.JOB_DEFINITIONS keys them.
POST_TRAN_JOB = "post-tran"
INTEREST_CALC_JOB = "interest-calc"
TRAN_BACKUP_JOB = "tran-backup"
COMB_TRAN_JOB = "comb-tran"
STATEMENT_GEN_JOB = "statement-gen"
TRAN_INDEX_JOB = "tran-index"

# Loaders whose dataset has only an EBCDIC image: TRANFILE's DALYTRAN.PS.INIT and DUSRSECJ's
# USRSEC.PS have no ASCII copy, so their load commands take no --encoding.
EBCDIC_ONLY_LOADERS = ("transactions", "init-users")
DAILY_TRANSACTIONS_LOADER = "daily-transactions"


class EncodingChoice(str, Enum):
    """The --encoding values: the seed-image encodings of app.seed.image_reader."""

    EBCDIC = ENCODING_EBCDIC
    ASCII = ENCODING_ASCII


# token_normalize_func lowercases command and option names, so `job POSTTRAN`, `job PostTran`
# and `load ACCTFILE` resolve to the lowercase alias commands registered below.
NORMALIZED_CONTEXT = {"token_normalize_func": str.lower, "help_option_names": ["-h", "--help"]}

# Plain Click help and error output (rich_markup_mode=None): help text is never read as Rich
# markup, and the output stays the same in a terminal, a pipe and a test runner. Tracebacks of
# unexpected errors never show local variables, which hold the settings and fetched rows.
APP = typer.Typer(
    name="carddemo-batch",
    help="CardDemo batch jobs: the chain, the loaders and the processing jobs, run in the"
    " batch window.",
    no_args_is_help=True,
    add_completion=False,
    context_settings=NORMALIZED_CONTEXT,
    rich_markup_mode=None,
    pretty_exceptions_show_locals=False,
)
JOB_APP = typer.Typer(
    no_args_is_help=True, context_settings=NORMALIZED_CONTEXT, rich_markup_mode=None
)
LOAD_APP = typer.Typer(
    no_args_is_help=True, context_settings=NORMALIZED_CONTEXT, rich_markup_mode=None
)
APP.add_typer(
    JOB_APP,
    name="job",
    help="Run one processing job as its step of the OPEN cycle.",
)
APP.add_typer(
    LOAD_APP,
    name="load",
    help="Run one loader, recorded against its step of the OPEN cycle when there is one.",
)

# The help of every command; each names the legacy job it replaces.
COMMAND_HELP = {
    "run-all": "Run or resume the 18-step chain in legacy order (CA-7 and Control-M chains).",
    "seed-all": "Abandon any OPEN cycle and run loader steps 1-5 and 7-12 in a new cycle.",
    POST_TRAN_JOB: "POSTTRAN (CBTRN02C): post the staged daily transactions.",
    INTEREST_CALC_JOB: "INTCALC (CBACT04C): compute monthly interest and write SYSTRAN(+1).",
    TRAN_BACKUP_JOB: "TRANBKP: archive transactions to TRANSACT.BKUP(+1), then empty them.",
    COMB_TRAN_JOB: "COMBTRAN: merge TRANSACT.BKUP(0) and SYSTRAN(0) into transactions.",
    STATEMENT_GEN_JOB: "CREASTMT (CBSTM03A, CBSTM03B): write STATEMNT.PS and STATEMNT.HTML.",
    TRAN_INDEX_JOB: "TRANIDX: rebuild the PROC-TS alternate index of transactions.",
    "accounts": "ACCTFILE: replace the accounts from the ACCTDATA image.",
    "cards": "CARDFILE: replace the cards from the CARDDATA image.",
    "customers": "CUSTFILE: replace the customers from the CUSTDATA image.",
    "card-xref": "XREFFILE: replace the card cross-reference from the CARDXREF image.",
    "transactions": "TRANFILE: replace the transactions from DALYTRAN.PS.INIT (EBCDIC only).",
    "tran-types": "TRANTYPE: replace the transaction types from the TRANTYPE image.",
    "tran-categories": "TRANCATG: replace the transaction categories from the TRANCATG image.",
    "disclosure-groups": "DISCGRP: replace the disclosure groups from the DISCGRP image.",
    "tran-cat-balances": "TCATBALF: replace the category balances from the TCATBALF image.",
    "init-users": "DUSRSECJ: replace the users from USRSEC.PS, passwords hashed (EBCDIC only).",
    DAILY_TRANSACTIONS_LOADER: "DALYTRAN: stage the daily transaction file for POSTTRAN.",
}


def ParseAsOfOption(text: str) -> datetime:
    """Parse --as-of with ParseAsOf; a malformed value is a usage error, exit 2."""
    try:
        return ParseAsOf(text)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


def ParseRunDateOption(text: str) -> str:
    """Return a --run-date JobParameters accepts; any other value is a usage error, exit 2.

    The rule is JobParameters' own: RUN_DATE_LENGTH printable ASCII characters, the PARM-DATE
    X(10) that CBACT04C reads.
    """
    try:
        return JobParameters(runDate=text).runDate
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


# Option declarations shared by several commands. Every name is declared explicitly, and the
# boolean flags have no negative --no-* form.
AS_OF_OPTION = Annotated[
    datetime | None,
    typer.Option(
        "--as-of",
        parser=ParseAsOfOption,
        metavar="TIMESTAMP",
        help="Freeze the batch clock at YYYY-MM-DDTHH:MM:SS[.ffffff].",
    ),
]
FORCE_OPTION = Annotated[
    bool,
    typer.Option("--force", help="Execute the step whatever its state, COMPLETED included."),
]
ENCODING_OPTION = Annotated[
    EncodingChoice,
    typer.Option("--encoding", help="The seed images to read: ebcdic, as the JCL loads, or ascii."),
]
SOURCE_DIR_OPTION = Annotated[
    Path | None,
    typer.Option(
        "--source-dir",
        metavar="DIRECTORY",
        help="The image directory; a relative path resolves against the repository root.",
    ),
]
DAILY_FILE_OPTION = Annotated[
    Path | None,
    typer.Option(
        "--daily-file",
        metavar="PATH",
        help="The daily transaction image; a relative path resolves against the repository root.",
    ),
]
RUN_DATE_OPTION = Annotated[
    str,
    typer.Option(
        "--run-date",
        parser=ParseRunDateOption,
        metavar="DATE",
        help=f"The {RUN_DATE_LENGTH}-character PARM-DATE that prefixes the interest TRAN-IDs.",
    ),
]
OCCURRENCE_OPTION = Annotated[
    int | None,
    typer.Option(
        "--occurrence",
        min=1,
        max=2,
        metavar="OCCURRENCE",
        help="The TRANBKP occurrence: 1 for chain step 6, 2 for step 15.",
    ),
]


@dataclass(frozen=True)
class CommandInvocation:
    """What one command runs inside the batch window: its action, parameters and clock.

    action is a closure over one batch_chain entry point; asOf freezes Clock.Now() when set.
    """

    action: Callable[[JobContext], int]
    parameters: JobParameters
    asOf: datetime | None = None


def LoadSettingsOrExit() -> BatchSettings:
    """Return the batch settings, or log which settings are invalid and exit 12.

    Only the error count and the setting names are logged: a rejected value may be a database
    URL with its password.
    """
    try:
        return LoadBatchSettings()
    except ValidationError as error:
        settingNames = sorted({str(detail["loc"][0]) for detail in error.errors() if detail["loc"]})
        LOGGER.error(
            SETTINGS_INVALID_LOG, error.error_count(), SETTING_NAME_SEPARATOR.join(settingNames)
        )
        raise typer.Exit(code=EXIT_FAILURE) from None


def BuildEngineOrExit(settings: BatchSettings) -> Engine:
    """Return the job engine, or log why SYNC_DATABASE_URL is unusable and exit 12.

    batch.db raises ValueError for an unparsable URL or a driver other than psycopg2, with a
    message that never contains the URL, so the message itself is logged.
    """
    try:
        return BuildEngine(settings)
    except ValueError as error:
        LOGGER.error(SETTINGS_INVALID_DETAIL_LOG, error)
        raise typer.Exit(code=EXIT_FAILURE) from None


def RunInBatchWindow(settings: BatchSettings, engine: Engine, invocation: CommandInvocation) -> int:
    """Run the invocation's prelude and action inside the batch window; return its status.

    The order is fixed: interrupted runs are classified first, then the generation catalog is
    reconciled, then the action resolves, skips or executes steps. A window not granted within
    its lock timeout, or a database that cannot be reached, gives EXIT_FAILURE; BatchWindow has
    then already released whatever it held.
    """
    context = JobContext(
        engine=engine,
        settings=settings,
        clock=Clock(invocation.asOf),
        parameters=invocation.parameters,
    )
    try:
        with BatchWindow(settings):
            run_ledger.ClassifyInterruptedRuns(context)
            gdg.ReconcileCatalog(context)
            return invocation.action(context)
    except BatchWindowUnavailableError:
        LOGGER.error(BATCH_WINDOW_UNAVAILABLE_MESSAGE)
        return EXIT_FAILURE
    except OperationalError as error:
        LOGGER.error(DATABASE_UNAVAILABLE_LOG, type(error).__name__)
        return EXIT_FAILURE


def ExecuteCommand(invocation: CommandInvocation) -> None:
    """Run one command in the batch window and exit with its status.

    The status, 0 included, is raised as typer.Exit, so the console script, the module form
    and an in-process CliRunner all report the chain's status as the exit code. The job engine
    is disposed of whatever happens.
    """
    ConfigureLogging()
    settings = LoadSettingsOrExit()
    engine = BuildEngineOrExit(settings)
    try:
        exitCode = RunInBatchWindow(settings, engine, invocation)
    finally:
        engine.dispose()
    raise typer.Exit(code=exitCode)


def ExecuteJob(request: JobRequest, parameters: JobParameters, asOf: datetime | None) -> None:
    """Run one processing job as its step of the OPEN cycle and exit with its status."""
    ExecuteCommand(
        CommandInvocation(
            action=lambda context: RunStandaloneJob(context, request),
            parameters=parameters,
            asOf=asOf,
        )
    )


def ExecuteLoad(loaderName: str, parameters: JobParameters) -> None:
    """Run one loader, in the OPEN cycle when there is one, and exit with its status."""
    ExecuteCommand(
        CommandInvocation(
            action=lambda context: RunStandaloneLoad(context, loaderName),
            parameters=parameters,
        )
    )


@APP.command("run-all", help=COMMAND_HELP["run-all"])
def RunAllCommand(
    newCycle: Annotated[
        bool,
        typer.Option("--new-cycle", help="Abandon any OPEN cycle and run steps 1-18 anew."),
    ] = False,
    startAt: Annotated[
        str | None,
        typer.Option(
            "--start-at",
            metavar="STEP",
            help="Start at a step number 1-18 or a job name; for TRANBKP, its first incomplete"
            " occurrence.",
        ),
    ] = None,
    force: FORCE_OPTION = False,
    asOf: AS_OF_OPTION = None,
) -> None:
    """Run or resume the chain with the default parameters: EBCDIC images and PARM 2022071800."""
    options = ChainOptions(newCycle=newCycle, startAt=startAt, force=force)
    ExecuteCommand(
        CommandInvocation(
            action=lambda context: RunChain(context, options),
            parameters=JobParameters(),
            asOf=asOf,
        )
    )


# seed-all takes no --encoding: the TRANFILE and DUSRSECJ images exist in EBCDIC only, so an
# ASCII seed-all could only abandon the OPEN cycle, reload steps 1-4 and stop at step 5.
@APP.command("seed-all", help=COMMAND_HELP["seed-all"])
def SeedAllCommand(
    sourceDir: SOURCE_DIR_OPTION = None, dailyFile: DAILY_FILE_OPTION = None
) -> None:
    """Reload every loaded table in a new cycle from the EBCDIC images."""
    ExecuteCommand(
        CommandInvocation(
            action=RunSeedAll,
            parameters=JobParameters(sourceDir=sourceDir, dailyFile=dailyFile),
        )
    )


def PostTranCommand(force: FORCE_OPTION = False, asOf: AS_OF_OPTION = None) -> None:
    """Run POSTTRAN as chain step 13."""
    ExecuteJob(JobRequest(jobName=POST_TRAN_JOB, force=force), JobParameters(), asOf)


def InterestCalcCommand(
    runDate: RUN_DATE_OPTION = DEFAULT_RUN_DATE,
    asOf: AS_OF_OPTION = None,
    force: FORCE_OPTION = False,
) -> None:
    """Run INTCALC as chain step 14 with the PARM-DATE --run-date."""
    request = JobRequest(jobName=INTEREST_CALC_JOB, force=force)
    ExecuteJob(request, JobParameters(runDate=runDate), asOf)


def TranBackupCommand(occurrence: OCCURRENCE_OPTION = None, force: FORCE_OPTION = False) -> None:
    """Run TRANBKP as chain step 6 or 15: --occurrence, or its first incomplete occurrence."""
    request = JobRequest(jobName=TRAN_BACKUP_JOB, occurrence=occurrence, force=force)
    ExecuteJob(request, JobParameters(occurrence=occurrence), None)


def CombTranCommand(force: FORCE_OPTION = False) -> None:
    """Run COMBTRAN as chain step 16."""
    ExecuteJob(JobRequest(jobName=COMB_TRAN_JOB, force=force), JobParameters(), None)


def StatementGenCommand(force: FORCE_OPTION = False) -> None:
    """Run CREASTMT as chain step 17."""
    ExecuteJob(JobRequest(jobName=STATEMENT_GEN_JOB, force=force), JobParameters(), None)


def TranIndexCommand(force: FORCE_OPTION = False) -> None:
    """Run TRANIDX as chain step 18."""
    ExecuteJob(JobRequest(jobName=TRAN_INDEX_JOB, force=force), JobParameters(), None)


def BuildImageLoadCommand(loaderName: str) -> Callable[..., None]:
    """Return the load command of a loader whose dataset has EBCDIC and ASCII images."""

    def ImageLoadCommand(
        encoding: ENCODING_OPTION = EncodingChoice.EBCDIC, sourceDir: SOURCE_DIR_OPTION = None
    ) -> None:
        """Replace the loader's table from its image in the chosen encoding."""
        ExecuteLoad(loaderName, JobParameters(encoding=encoding.value, sourceDir=sourceDir))

    return ImageLoadCommand


def BuildEbcdicLoadCommand(loaderName: str) -> Callable[..., None]:
    """Return the load command of a loader whose dataset has an EBCDIC image only."""

    def EbcdicLoadCommand(sourceDir: SOURCE_DIR_OPTION = None) -> None:
        """Replace the loader's table from its EBCDIC image."""
        ExecuteLoad(loaderName, JobParameters(sourceDir=sourceDir))

    return EbcdicLoadCommand


def DailyTransactionsCommand(
    dailyFile: DAILY_FILE_OPTION = None, encoding: ENCODING_OPTION = EncodingChoice.EBCDIC
) -> None:
    """Stage the daily file: --daily-file, or CARDDEMO_EBCDIC_DIR/AWS.M2.CARDDEMO.DALYTRAN.PS."""
    parameters = JobParameters(encoding=encoding.value, dailyFile=dailyFile)
    ExecuteLoad(DAILY_TRANSACTIONS_LOADER, parameters)


def LoadCommandFor(loaderName: str) -> Callable[..., None]:
    """Return a loader's command, whose options follow the images its dataset has."""
    if loaderName == DAILY_TRANSACTIONS_LOADER:
        return DailyTransactionsCommand
    if loaderName in EBCDIC_ONLY_LOADERS:
        return BuildEbcdicLoadCommand(loaderName)
    return BuildImageLoadCommand(loaderName)


def RegisterCommand(group: typer.Typer, canonicalName: str, function: Callable[..., None]) -> None:
    """Register a command under its canonical name and, hidden, under its legacy JCL names.

    Aliases are registered lowercase; NORMALIZED_CONTEXT lowercases what the operator types.
    """
    helpText = COMMAND_HELP[canonicalName]
    group.command(canonicalName, help=helpText)(function)
    for alias in FindJobDefinition(canonicalName).aliases:
        group.command(alias.lower(), help=helpText, hidden=True)(function)


JOB_COMMANDS = {
    POST_TRAN_JOB: PostTranCommand,
    INTEREST_CALC_JOB: InterestCalcCommand,
    TRAN_BACKUP_JOB: TranBackupCommand,
    COMB_TRAN_JOB: CombTranCommand,
    STATEMENT_GEN_JOB: StatementGenCommand,
    TRAN_INDEX_JOB: TranIndexCommand,
}

# Registered in batch_chain's order, so `job --help` and `load --help` list chain order.
for jobName in PROCESSING_JOB_NAMES:
    RegisterCommand(JOB_APP, jobName, JOB_COMMANDS[jobName])
for loaderName in LOADER_JOB_NAMES:
    RegisterCommand(LOAD_APP, loaderName, LoadCommandFor(loaderName))


if __name__ == "__main__":
    APP()
