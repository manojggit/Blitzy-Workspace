"""The carddemo-batch command surface, its legacy JCL aliases and its path resolution.

Pins the CLI that replaces the scheduler/CardDemo.ca7 and scheduler/CardDemo.controlm job
submission and the EXEC PARM= and DD overrides of the job JCL (jcl/POSTTRAN.jcl, jcl/INTCALC.jcl
PARM='2022071800', jcl/ACCTFILE.jcl, jcl/TRANTYPE.jcl), as the Technical Specification,
Sections 0.6.8 ("Local orchestration commands"), 0.7.3 (path and environment-file resolution)
and 0.9.4 (runtime verification), define it:

- ``carddemo-batch --help`` lists run-all, seed-all, job and load; ``job --help`` and
  ``load --help`` list every documented job and loader name, and ``python -m batch.cli`` serves
  the same surface.
- Legacy JCL member names (POSTTRAN, ACCTFILE, TRANTYPE ...) are hidden aliases accepted in any
  letter case, and the run history records the job under its JCL name.
- Malformed --run-date, --occurrence and --as-of values are usage errors, exit 2, and record no
  run.
- A relative CARDDEMO_EBCDIC_DIR or CARDDEMO_OUTPUT_DIR resolves against the repository root,
  whatever directory the command starts in.

The database tests start from the restored seed state of the batchEnvironment fixture, where
the Alembic seed cycle is OPEN with steps 1-5 and 7-12 COMPLETED.
"""

import secrets
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
from app.core.paths import REPO_ROOT
from app.models import JOB_RUNS, Account, Transaction, TransactionType
from sqlalchemy import Engine, Table, select, update

from batch.orchestration.job_context import EXIT_OK, EXIT_REJECTS
from batch.tests.conftest import (
    BACKEND_DIR,
    CLI_TIMEOUT_SECONDS,
    LOADER_ROWS_DIR,
    OUTPUT_DIR_VARIABLE,
    BatchEnvironment,
    CountJobRuns,
    InvokeCli,
    RunCli,
)
from batch.tests.reconciliation import compare

# --- The documented command surface (Technical Specification 0.6.8) ------------------------------
CLI_COMMANDS = ("run-all", "seed-all", "job", "load")
JOB_NAMES = (
    "post-tran",
    "interest-calc",
    "tran-backup",
    "comb-tran",
    "statement-gen",
    "tran-index",
)
LOADER_NAMES = (
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
# The legacy JCL member each command replaces, accepted as a hidden alias.
JOB_ALIASES = {
    "post-tran": "POSTTRAN",
    "interest-calc": "INTCALC",
    "tran-backup": "TRANBKP",
    "comb-tran": "COMBTRAN",
    "statement-gen": "CREASTMT",
    "tran-index": "TRANIDX",
}
LOADER_ALIASES = {
    "accounts": "ACCTFILE",
    "cards": "CARDFILE",
    "customers": "CUSTFILE",
    "card-xref": "XREFFILE",
    "transactions": "TRANFILE",
    "tran-types": "TRANTYPE",
    "tran-categories": "TRANCATG",
    "disclosure-groups": "DISCGRP",
    "tran-cat-balances": "TCATBALF",
    "init-users": "DUSRSECJ",
    "daily-transactions": "DALYTRAN",
}
ALIAS_CASES = [
    *(("job", canonicalName, jclName) for canonicalName, jclName in JOB_ALIASES.items()),
    *(("load", canonicalName, jclName) for canonicalName, jclName in LOADER_ALIASES.items()),
]
ALIAS_CASE_IDS = [jclName for _, _, jclName in ALIAS_CASES]
MODULE_NAME = "batch.cli"
HELP_OPTION = "--help"


@dataclass(frozen=True)
class HelpCase:
    """A command's arguments, the options its help must list and those it must not."""

    arguments: tuple[str, ...]
    presentOptions: tuple[str, ...]
    absentOptions: tuple[str, ...] = ()


# TRANFILE's DALYTRAN.PS.INIT and DUSRSECJ's USRSEC.PS exist in EBCDIC only, so their loads take
# no --encoding; only INTCALC reads a PARM, and only TRANBKP runs twice in a cycle.
HELP_CASES = (
    HelpCase(("run-all",), ("--new-cycle", "--start-at", "--force", "--as-of")),
    HelpCase(("seed-all",), ("--source-dir", "--daily-file"), ("--encoding",)),
    HelpCase(("job", "post-tran"), ("--as-of", "--force"), ("--run-date", "--occurrence")),
    HelpCase(("job", "interest-calc"), ("--run-date", "--as-of", "--force"), ("--occurrence",)),
    HelpCase(("job", "tran-backup"), ("--occurrence", "--force"), ("--run-date", "--as-of")),
    HelpCase(("job", "comb-tran"), ("--force",), ("--occurrence",)),
    HelpCase(("job", "statement-gen"), ("--force",), ("--occurrence",)),
    HelpCase(("job", "tran-index"), ("--force",), ("--occurrence",)),
    HelpCase(("load", "accounts"), ("--encoding", "--source-dir")),
    HelpCase(("load", "transactions"), ("--source-dir",), ("--encoding",)),
    HelpCase(("load", "init-users"), ("--source-dir",), ("--encoding",)),
    HelpCase(("load", "daily-transactions"), ("--daily-file", "--encoding")),
)
HELP_CASE_IDS = ["-".join(case.arguments) for case in HELP_CASES]

# --- Click help and usage-error output -----------------------------------------------------------
COMMANDS_HEADING = "Commands:"
# Click indents each command entry by two spaces and a wrapped continuation line further.
ENTRY_INDENT = "  "
# Click's UsageError exit status: an unknown command or an invalid option value.
USAGE_ERROR_EXIT = 2
NO_SUCH_COMMAND_TEXT = "No such command"
INVALID_VALUE_TEMPLATE = "Invalid value for '{optionName}'"

# --- Chain steps, run history and generations ----------------------------------------------------
ACCOUNTS_STEP = 1
TRAN_BACKUP_FIRST_STEP = 6
TRAN_TYPES_STEP = 7
POST_TRAN_STEP = 13
INTEREST_CALC_STEP = 14
RUN_COMPLETED = "COMPLETED"
TRANSACT_BKUP_BASE = "TRANSACT.BKUP"
SYSTRAN_BASE = "SYSTRAN"
GDG_DIRECTORY = "gdg"

# --- Option values -------------------------------------------------------------------------------
# A PARM-DATE other than the INTCALC JCL's 2022071800, so its TRAN-ID prefix proves it was used.
ALTERNATE_RUN_DATE = "2022071900"
SHORT_RUN_DATE = "202207180"
LONG_RUN_DATE = "20220718000"
OUT_OF_RANGE_OCCURRENCES = ("0", "3")
MALFORMED_AS_OF_TEXTS = ("2022-07-18", "2022/07/18T00:00:00", "not-a-date")
MICROSECOND_AS_OF_TEXT = "2022-07-18T00:00:00.123456"
# Clock.Now() keeps hundredths of a second, as FUNCTION CURRENT-DATE does.
TRUNCATED_AS_OF = datetime(2022, 7, 18, 0, 0, 0, 120000)

# --- Working directories and relative paths ------------------------------------------------------
FRONTEND_DIR = REPO_ROOT / "frontend"
EBCDIC_DIR_VARIABLE = "CARDDEMO_EBCDIC_DIR"
RELATIVE_EBCDIC_DIR = "data/EBCDIC"
VAR_DIRECTORY = "var"
RELATIVE_OUTPUT_PREFIX = "batch-test-"
OUTPUT_TOKEN_BYTES = 4
# Written over a loaded text column, so only a reload brings back the fixture's values.
DISTURBED_TEXT = "DISTURBED"


@dataclass(frozen=True)
class LoaderTarget:
    """A loader's chain step and table, and a text column a test overwrites before loading.

    A match with the loader_rows fixture after the overwrite proves the load replaced the
    table, rather than that the seed rows were left in place.
    """

    stepNo: int
    table: Table
    textColumn: str

    def CsvPath(self) -> Path:
        """Return the expected loader_rows CSV of the table."""
        return LOADER_ROWS_DIR / f"{self.table.name}.csv"


ACCOUNTS_TARGET = LoaderTarget(ACCOUNTS_STEP, Account.__table__, "addr_zip")
TRAN_TYPES_TARGET = LoaderTarget(TRAN_TYPES_STEP, TransactionType.__table__, "description")
LOAD_ALIAS_CASES = (
    ("acctfile", ACCOUNTS_TARGET),
    ("ACCTFILE", ACCOUNTS_TARGET),
    ("trantype", TRAN_TYPES_TARGET),
    ("TRANTYPE", TRAN_TYPES_TARGET),
)


# --- Helpers -------------------------------------------------------------------------------------
def ListedCommands(helpOutput: str) -> list[str]:
    """Return the command names a Click help page lists under "Commands:", in listed order.

    Only a line indented by exactly two spaces starts an entry; a deeper one continues it. A
    page with no commands section gives [].
    """
    commandLines = helpOutput.partition(COMMANDS_HEADING)[2].splitlines()
    return [
        line.split()[0]
        for line in commandLines
        if line.startswith(ENTRY_INDENT) and not line.startswith(ENTRY_INDENT + " ")
    ]


def LatestStepRun(engine: Engine, stepNo: int) -> dict[str, object]:
    """Return the newest batch.job_runs row of a chain step: job_name, status, exit_code, params."""
    statement = (
        select(JOB_RUNS.c.job_name, JOB_RUNS.c.status, JOB_RUNS.c.exit_code, JOB_RUNS.c.params)
        .where(JOB_RUNS.c.step_no == stepNo)
        .order_by(JOB_RUNS.c.run_id.desc())
        .limit(1)
    )
    with engine.connect() as connection:
        return dict(connection.execute(statement).mappings().one())


def DisturbTable(engine: Engine, target: LoaderTarget) -> None:
    """Overwrite the target's text column in every row, so only a reload restores the seed."""
    with engine.begin() as connection:
        connection.execute(update(target.table).values({target.textColumn: DISTURBED_TEXT}))


def TableDifferences(engine: Engine, target: LoaderTarget) -> list[str]:
    """Compare the target table with its loader_rows CSV; [] when every row and field matches."""
    with engine.connect() as connection:
        return compare.CompareTableWithCsv(connection, target.table.fullname, target.CsvPath())


def AssertLoaderRan(engine: Engine, target: LoaderTarget, runsBefore: int) -> None:
    """Assert the load restored the table and added one COMPLETED run against its step."""
    assert TableDifferences(engine, target) == []
    assert CountJobRuns(engine, target.stepNo) == runsBefore + 1
    assert LatestStepRun(engine, target.stepNo)["status"] == RUN_COMPLETED


def AssertUsageErrorWithoutRuns(engine: Engine, arguments: Sequence[str], optionName: str) -> None:
    """Assert the command is a usage error naming optionName and records no batch.job_runs row."""
    runsBefore = CountJobRuns(engine)
    outcome = InvokeCli(arguments)
    assert outcome.exitCode == USAGE_ERROR_EXIT, outcome.output
    assert INVALID_VALUE_TEMPLATE.format(optionName=optionName) in outcome.output
    assert CountJobRuns(engine) == runsBefore


def NewestGenerationRecords(environment: BatchEnvironment, baseName: str) -> list[str]:
    """Return the records of a base's newest PUBLISHED generation under the test's output root."""
    with environment.engine.connect() as connection:
        generationPath = compare.LatestPublishedGeneration(
            connection, baseName, environment.outputDir
        )
    assert generationPath is not None, f"no PUBLISHED {baseName} generation"
    return compare.ReadFixedWidthRecords(generationPath)


def PostedProcTimestamps(engine: Engine) -> set[datetime]:
    """Return the distinct PROC-TS values of the posted transactions."""
    procTs = Transaction.__table__.c.proc_ts
    with engine.connect() as connection:
        return set(connection.execute(select(procTs).distinct()).scalars())


def RemoveTreeIfPresent(path: Path) -> None:
    """Delete a directory tree a test created; nothing when it does not exist."""
    if path.is_dir():
        shutil.rmtree(path)


@pytest.fixture(name="relativeOutputDir")
def RelativeOutputDir(
    batchEnvironment: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Point CARDDEMO_OUTPUT_DIR at a new relative var/batch-test-<token>; delete it afterwards.

    Requested after batchEnvironment, so this value overrides that fixture's absolute one. The
    copy under backend/ exists only if the CLI resolved against its working directory.
    """
    directoryName = RELATIVE_OUTPUT_PREFIX + secrets.token_hex(OUTPUT_TOKEN_BYTES)
    relativeDir = Path(VAR_DIRECTORY, directoryName)
    monkeypatch.setenv(OUTPUT_DIR_VARIABLE, relativeDir.as_posix())
    yield relativeDir
    RemoveTreeIfPresent(REPO_ROOT / relativeDir)
    RemoveTreeIfPresent(BACKEND_DIR / relativeDir)


# --- Phase 1: help and naming --------------------------------------------------------------------
def test_help_lists_run_all_seed_all_job_and_load() -> None:
    """The console script's --help lists exactly the four command groups (Section 0.9.4)."""
    outcome = RunCli([HELP_OPTION])
    assert outcome.exitCode == EXIT_OK, outcome.output
    assert ListedCommands(outcome.output) == list(CLI_COMMANDS)


@pytest.mark.parametrize(
    ("group", "expectedNames"),
    [("job", JOB_NAMES), ("load", LOADER_NAMES)],
    ids=["job", "load"],
)
def test_job_and_load_help_list_every_documented_name(
    group: str, expectedNames: tuple[str, ...]
) -> None:
    """job --help and load --help list every documented name, in chain order, and no alias."""
    outcome = RunCli([group, HELP_OPTION])
    assert outcome.exitCode == EXIT_OK, outcome.output
    assert ListedCommands(outcome.output) == list(expectedNames)


def test_python_module_form_runs_cli() -> None:
    """python -m batch.cli serves the same command groups as the console script."""
    completed = subprocess.run(
        [sys.executable, "-m", MODULE_NAME, HELP_OPTION],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == EXIT_OK, completed.stdout + completed.stderr
    assert ListedCommands(completed.stdout) == list(CLI_COMMANDS)


@pytest.mark.parametrize("helpCase", HELP_CASES, ids=HELP_CASE_IDS)
def test_command_help_lists_its_documented_options(helpCase: HelpCase) -> None:
    """Each command's help lists the options Section 0.6.8 gives it, and only those it takes."""
    outcome = InvokeCli([*helpCase.arguments, HELP_OPTION])
    assert outcome.exitCode == EXIT_OK, outcome.output
    for optionName in helpCase.presentOptions:
        assert optionName in outcome.output
    for optionName in helpCase.absentOptions:
        assert optionName not in outcome.output


# --- Phase 2: legacy JCL aliases -----------------------------------------------------------------
@pytest.mark.parametrize(("group", "canonicalName", "jclName"), ALIAS_CASES, ids=ALIAS_CASE_IDS)
def test_every_legacy_jcl_name_is_an_alias_of_its_command(
    group: str, canonicalName: str, jclName: str
) -> None:
    """Every JCL member name opens its command's help; only the usage line shows the alias."""
    canonicalHelp = InvokeCli([group, canonicalName, HELP_OPTION])
    aliasHelp = InvokeCli([group, jclName, HELP_OPTION])
    assert canonicalHelp.exitCode == EXIT_OK, canonicalHelp.output
    assert aliasHelp.exitCode == EXIT_OK, aliasHelp.output
    # Click names the command as the normalised token the operator typed, lowercase.
    aliasUsage = aliasHelp.output.replace(f" {jclName.lower()} ", f" {canonicalName} ", 1)
    assert aliasUsage == canonicalHelp.output


@pytest.mark.parametrize("alias", ["POSTTRAN", "posttran", "PostTran"])
def test_job_jcl_alias_is_case_insensitive(batchEnvironment: BatchEnvironment, alias: str) -> None:
    """job POSTTRAN in any letter case runs chain step 13 of the OPEN seed cycle.

    The run history records the job under its JCL name, POSTTRAN, as batch_chain fills
    batch.job_runs.job_name and Alembic 0008 names the seed runs; the sample's rejects give 4.
    """
    engine = batchEnvironment.engine
    runsBefore = CountJobRuns(engine, POST_TRAN_STEP)
    outcome = InvokeCli(["job", alias, "--as-of", batchEnvironment.asOfText])
    assert outcome.exitCode == EXIT_REJECTS, outcome.output
    assert CountJobRuns(engine, POST_TRAN_STEP) == runsBefore + 1
    latestRun = LatestStepRun(engine, POST_TRAN_STEP)
    assert latestRun["job_name"] == JOB_ALIASES["post-tran"]
    assert (latestRun["status"], latestRun["exit_code"]) == (RUN_COMPLETED, EXIT_REJECTS)


@pytest.mark.parametrize(
    ("alias", "target"), LOAD_ALIAS_CASES, ids=[alias for alias, _ in LOAD_ALIAS_CASES]
)
def test_load_jcl_alias_is_case_insensitive(
    batchEnvironment: BatchEnvironment, alias: str, target: LoaderTarget
) -> None:
    """load ACCTFILE or TRANTYPE in any letter case replaces the canonical loader's table."""
    engine = batchEnvironment.engine
    DisturbTable(engine, target)
    assert TableDifferences(engine, target) != []
    runsBefore = CountJobRuns(engine, target.stepNo)
    outcome = InvokeCli(["load", alias])
    assert outcome.exitCode == EXIT_OK, outcome.output
    AssertLoaderRan(engine, target, runsBefore)
    assert LatestStepRun(engine, target.stepNo)["job_name"] == alias.upper()


@pytest.mark.parametrize("jobName", ["NOSUCHJOB", "ACCTFILE"])
def test_unknown_job_name_is_rejected_without_ledger_rows(
    batchEnvironment: BatchEnvironment, jobName: str
) -> None:
    """A name that is no processing job, a loader's JCL name included, is a usage error."""
    engine = batchEnvironment.engine
    runsBefore = CountJobRuns(engine)
    outcome = InvokeCli(["job", jobName])
    assert outcome.exitCode == USAGE_ERROR_EXIT, outcome.output
    assert NO_SUCH_COMMAND_TEXT in outcome.output
    assert CountJobRuns(engine) == runsBefore


# --- Phase 3: option validation ------------------------------------------------------------------
@pytest.mark.parametrize("runDate", [SHORT_RUN_DATE, LONG_RUN_DATE])
def test_run_date_must_be_ten_characters(batchEnvironment: BatchEnvironment, runDate: str) -> None:
    """A --run-date other than the 10-character PARM-DATE is refused before any run starts."""
    arguments = ["job", "interest-calc", "--run-date", runDate]
    AssertUsageErrorWithoutRuns(batchEnvironment.engine, arguments, "--run-date")


def test_ten_character_run_date_prefixes_the_interest_tran_ids(
    batchEnvironment: BatchEnvironment,
) -> None:
    """--run-date 2022071900 replaces the INTCALC PARM: every SYSTRAN TRAN-ID starts with it."""
    asOfArguments = ["--as-of", batchEnvironment.asOfText]
    postTran = InvokeCli(["job", "post-tran", *asOfArguments])
    assert postTran.exitCode == EXIT_REJECTS, postTran.output
    runDateArguments = ["--run-date", ALTERNATE_RUN_DATE]
    outcome = InvokeCli(["job", "interest-calc", *runDateArguments, *asOfArguments])
    assert outcome.exitCode == EXIT_OK, outcome.output
    records = NewestGenerationRecords(batchEnvironment, SYSTRAN_BASE)
    assert records
    assert all(record.startswith(ALTERNATE_RUN_DATE) for record in records)
    latestRun = LatestStepRun(batchEnvironment.engine, INTEREST_CALC_STEP)
    assert latestRun["params"]["run_date"] == ALTERNATE_RUN_DATE


@pytest.mark.parametrize("occurrence", OUT_OF_RANGE_OCCURRENCES)
def test_occurrence_must_be_1_or_2(batchEnvironment: BatchEnvironment, occurrence: str) -> None:
    """TRANBKP runs as step 6 or 15 only, so --occurrence accepts 1 and 2 alone."""
    arguments = ["job", "tran-backup", "--occurrence", occurrence]
    AssertUsageErrorWithoutRuns(batchEnvironment.engine, arguments, "--occurrence")


@pytest.mark.parametrize("asOfText", MALFORMED_AS_OF_TEXTS)
def test_as_of_must_be_iso_seconds_timestamp(
    batchEnvironment: BatchEnvironment, asOfText: str
) -> None:
    """--as-of is YYYY-MM-DDTHH:MM:SS[.ffffff]; a date alone or another shape is refused."""
    arguments = ["job", "post-tran", "--as-of", asOfText]
    AssertUsageErrorWithoutRuns(batchEnvironment.engine, arguments, "--as-of")


def test_as_of_with_microseconds_is_accepted_and_truncated(
    batchEnvironment: BatchEnvironment,
) -> None:
    """--as-of with microseconds freezes the clock, which keeps hundredths of a second."""
    outcome = InvokeCli(["job", "post-tran", "--as-of", MICROSECOND_AS_OF_TEXT])
    assert outcome.exitCode == EXIT_REJECTS, outcome.output
    assert PostedProcTimestamps(batchEnvironment.engine) == {TRUNCATED_AS_OF}


# --- Phase 4: working-directory independence (Section 0.7.3) -------------------------------------
@pytest.mark.parametrize("workingDir", [FRONTEND_DIR, BACKEND_DIR], ids=["frontend", "backend"])
def test_load_tran_types_subprocess_from_frontend_and_backend_dirs(
    batchEnvironment: BatchEnvironment, monkeypatch: pytest.MonkeyPatch, workingDir: Path
) -> None:
    """load tran-types started in frontend/ or backend/ reads data/EBCDIC under the root.

    CARDDEMO_EBCDIC_DIR is the relative default, so the 7 TRANTYPE rows load only if it
    resolves against REPO_ROOT, not the working directory.
    """
    monkeypatch.setenv(EBCDIC_DIR_VARIABLE, RELATIVE_EBCDIC_DIR)
    workingDir.mkdir(exist_ok=True)
    engine = batchEnvironment.engine
    DisturbTable(engine, TRAN_TYPES_TARGET)
    runsBefore = CountJobRuns(engine, TRAN_TYPES_STEP)
    outcome = RunCli(["load", "tran-types"], workingDir=workingDir)
    assert outcome.exitCode == EXIT_OK, outcome.output
    AssertLoaderRan(engine, TRAN_TYPES_TARGET, runsBefore)


def test_relative_output_dir_resolves_against_repo_root(
    batchEnvironment: BatchEnvironment, relativeOutputDir: Path
) -> None:
    """A relative CARDDEMO_OUTPUT_DIR puts the TRANSACT.BKUP generation under REPO_ROOT.

    The command starts in backend/, so a working-directory resolution would write there.
    """
    arguments = ["job", "tran-backup", "--occurrence", "1"]
    outcome = RunCli(arguments, workingDir=BACKEND_DIR)
    assert outcome.exitCode == EXIT_OK, outcome.output
    expectedOutputRoot = REPO_ROOT / relativeOutputDir
    with batchEnvironment.engine.connect() as connection:
        generationPath = compare.LatestPublishedGeneration(
            connection, TRANSACT_BKUP_BASE, expectedOutputRoot
        )
    assert generationPath is not None and generationPath.is_file()
    assert generationPath.is_relative_to(expectedOutputRoot / GDG_DIRECTORY)
    assert not (BACKEND_DIR / relativeOutputDir).exists()
    assert LatestStepRun(batchEnvironment.engine, TRAN_BACKUP_FIRST_STEP)["job_name"] == "TRANBKP"
