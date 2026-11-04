"""Chain order, exit policy, resume, failure, rerun and interruption of the 18-step batch chain.

Pins batch/orchestration/batch_chain.py and run_ledger.py to the Technical Specification,
Section 0.6.8: the dependency graph and step table, cycles and step identity, the skip,
resume, failure and force rules, and the publication of a producer step's generation after its
commit, whose error or LOST outcome stops the chain with the step still COMPLETED. The chain
replaces the CardDemo job chains of scheduler/CardDemo.ca7 and scheduler/CardDemo.controlm and
the COND=(4,LT) condition-code chaining of jcl/TRANBKP.jcl STEP10: a step ending 0 or 4 lets
the chain continue, 8 or above stops it, and run-all exits with the highest status of the steps
it ran. The steps exercised beyond the loaders are TRANBKP (jcl/TRANBKP.jcl), POSTTRAN
(jcl/POSTTRAN.jcl, cbl/CBTRN02C.cbl), INTCALC (jcl/INTCALC.jcl), COMBTRAN (jcl/COMBTRAN.jcl),
CREASTMT (jcl/CREASTMT.JCL) and TRANIDX (jcl/TRANIDX.jcl).

Every case starts from the Alembic seed: the SEED cycle OPEN, steps 1-5 and 7-12 COMPLETED. The
carddemo-batch commands run in process, so failure injections apply; the SIGKILL case runs the
CLI as a subprocess and is marked slow. A clean run is judged against the committed oracle
fixtures, so no case runs a second, reference chain: accounts after INTCALC, category balances
after POSTTRAN, the generations COMBTRAN resolves (TRANSACT.BKUP(0) = tranbkp_run2_bkup.dat,
SYSTRAN(0) = intcalc_systran.dat), the TRANSACT.COMBINED(0) it writes and the transactions it
reloads.

Injected failures are scoped with pytest.MonkeyPatch.context() rather than monkeypatch.undo():
the outputDir fixture sets CARDDEMO_OUTPUT_DIR through the same monkeypatch, and undoing it
would send every later command's archives to the repository's var/batch-output.
"""

import errno
import hashlib
import itertools
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from app.core.paths import REPO_ROOT
from app.models import (
    CYCLES,
    GDG_GENERATIONS,
    JOB_RUNS,
    Account,
    Card,
    CardXref,
    Customer,
    DailyTransaction,
    DisclosureGroup,
    Transaction,
    TransactionCategory,
    TransactionCategoryBalance,
    TransactionType,
    User,
)
from app.services.transactions import cbtrn02c
from sqlalchemy import Connection, Engine, Table, Text, cast, column, func, select, text
from sqlalchemy.exc import OperationalError

from batch.jobs import posttran
from batch.orchestration import batch_chain, gdg, run_ledger
from batch.orchestration.batch_window import WINDOW_CLOSED_LOG
from batch.orchestration.job_context import (
    EXIT_ABEND,
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_REJECTS,
    JobContext,
)
from batch.tests.conftest import (
    EXPECTED_DIR,
    FIXED_AS_OF_TEXT,
    BatchEnvironment,
    CliOutcome,
    CountJobRuns,
    InjectAtomicRollbackFailure,
    InjectStepFailure,
    InsertSyntheticTransactions,
    InvokeCli,
    IsBatchWindowHeld,
    KillProcess,
    StartCli,
    StepRow,
    SyntheticTransactionSpec,
    WaitUntil,
    WriteSyntheticDailyImage,
)
from batch.tests.reconciliation import compare, legacy_oracle

EXPECTED_LEGACY_ORDER = (
    "ACCTFILE",
    "CARDFILE",
    "CUSTFILE",
    "XREFFILE",
    "TRANFILE",
    "TRANBKP",
    "TRANTYPE",
    "TRANCATG",
    "DISCGRP",
    "TCATBALF",
    "DUSRSECJ",
    "DALYTRAN",
    "POSTTRAN",
    "INTCALC",
    "TRANBKP",
    "COMBTRAN",
    "CREASTMT",
    "TRANIDX",
)
CHAIN_STEP_NUMBERS = tuple(range(1, len(EXPECTED_LEGACY_ORDER) + 1))
ATOMIC_STEPS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 16, 18)
PER_WRITE_STEPS = (13, 14)
FILES_ONLY_STEPS = (17,)
EXPECTED_DURABILITY = {
    **{stepNo: batch_chain.DURABILITY_ATOMIC for stepNo in ATOMIC_STEPS},
    **{stepNo: batch_chain.DURABILITY_PER_WRITE for stepNo in PER_WRITE_STEPS},
    **{stepNo: batch_chain.DURABILITY_FILES_ONLY for stepNo in FILES_ONLY_STEPS},
}
# The loader steps Alembic 0008 records COMPLETED, and the steps a run-all from the seed executes.
SEEDED_STEPS = (1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12)
UNSEEDED_STEPS = (6, 13, 14, 15, 16, 17, 18)
FIRST_TRANBKP_STEP = 6
POSTTRAN_STEP = 13
SECOND_TRANBKP_STEP = 15
CREASTMT_STEP = 17
FINAL_STEP = 18
# After each step a run-all from the seed executes, the next one it executes. A failure injected
# at that entry point stops the chain right after the step, having written nothing.
NEXT_EXECUTED_STEP = {6: 13, 13: 14, 14: 15, 15: 16, 16: 17, 17: 18}
# The lowest status COND=(4,LT) stops on. A literal, not the chain's own threshold, so the
# boundary case fails if that threshold moves.
LOWEST_STOPPING_EXIT_CODE = 8

# The table each loader step replaces.
LOADER_TABLES: dict[int, Table] = {
    1: Account.__table__,
    2: Card.__table__,
    3: Customer.__table__,
    4: CardXref.__table__,
    5: Transaction.__table__,
    7: TransactionType.__table__,
    8: TransactionCategory.__table__,
    9: DisclosureGroup.__table__,
    10: TransactionCategoryBalance.__table__,
    11: User.__table__,
    12: DailyTransaction.__table__,
}
# The loaded tables no step after step 12 writes.
UNTOUCHED_LOADER_TABLES = (DailyTransaction.__table__, User.__table__)

# The job command that re-invokes each step a run-all from the seed executes.
RERUN_COMMANDS: dict[int, tuple[str, ...]] = {
    6: ("job", "tran-backup", "--occurrence", "1"),
    13: ("job", "post-tran", "--as-of", FIXED_AS_OF_TEXT),
    14: ("job", "interest-calc", "--as-of", FIXED_AS_OF_TEXT),
    15: ("job", "tran-backup", "--occurrence", "2"),
    16: ("job", "comb-tran"),
    17: ("job", "statement-gen"),
    18: ("job", "tran-index"),
}
FORCED_TRAN_BACKUP_COMMAND = (*RERUN_COMMANDS[FIRST_TRANBKP_STEP], "--force")
START_AT_POSTTRAN_VALUES = ("13", "post-tran", "POSTTRAN", "posttran", "PostTran")
# One digit more than Python 3.13 converts with int() by default (int max-str-digits is 4300).
OVERLONG_STEP_DIGITS = 4301
UNKNOWN_START_AT_VALUES = [
    pytest.param("0", id="step-0"),
    pytest.param(str(FINAL_STEP + 1), id="step-after-last"),
    pytest.param("-1", id="negative"),
    pytest.param("NOPE", id="unknown-name"),
    pytest.param("1" * OVERLONG_STEP_DIGITS, id="4301-digits"),
]
# Click's UsageError exit status, and the refusal it shows for an unknown --start-at: the
# option's rule, never the value.
USAGE_ERROR_EXIT = 2
START_AT_USAGE_ERROR = f"Invalid value for '--start-at': {batch_chain.START_AT_RULE_MESSAGE}"
UNKNOWN_START_AT_NAME = "NOPE"
TRACEBACK_HEADER = "Traceback (most recent call last)"

CLEAN_ACCOUNTS_CSV = EXPECTED_DIR / "intcalc_accounts.csv"
CLEAN_CATEGORY_BALANCES_CSV = EXPECTED_DIR / "posttran_tcatbal.csv"
CLEAN_COMBINED_DAT = EXPECTED_DIR / "combtran_combined.dat"
CLEAN_BACKUP_DAT = EXPECTED_DIR / "tranbkp_run2_bkup.dat"
CLEAN_SYSTRAN_DAT = EXPECTED_DIR / "intcalc_systran.dat"
# Each generation a clean chain leaves as (0), with its fixture: the step 15 backup and the
# INTCALC SYSTRAN that COMBTRAN resolves, then the TRANSACT.COMBINED it writes from them.
CLEAN_GENERATION_FIXTURES = (
    (gdg.TRANSACT_BKUP.name, CLEAN_BACKUP_DAT),
    (gdg.SYSTRAN.name, CLEAN_SYSTRAN_DAT),
    (gdg.TRANSACT_COMBINED.name, CLEAN_COMBINED_DAT),
)
TRANSACTION_LAYOUT_NAME = "TRANSACTION"
STATEMENTS_DIRECTORY_NAME = "statements"
# Each statement file and its fixture; the file name is also its line-only oracle layout.
STATEMENT_FIXTURES = {
    "STATEMNT.PS": EXPECTED_DIR / "creastmt_STATEMNT.PS",
    "STATEMNT.HTML": EXPECTED_DIR / "creastmt_STATEMNT.HTML",
}
GDG_BASE_NAMES = tuple(gdg.GDG_BASES)

SYNTHETIC_RECORD_COUNT = 20_000
SYNTHETIC_DAILY_FILE_NAME = "AWS.M2.CARDDEMO.DALYTRAN.SYNTHETIC.PS"
RELOAD_MARKER_AMOUNT = Decimal("1.00")
POSTING_WAIT_SECONDS = 180
WINDOW_RELEASE_SECONDS = 30
XMIN_COLUMN = "xmin"
# The client sessions on this test's database, other than the probe's own, that are not idle.
# A killed CLI's job session can still be executing a TCATBAL UPDATE or its COMMIT after its
# window session has gone; an idle session holds no transaction, so nothing of it can still
# land. pg_stat_activity lists every database of the server, which sibling clones share.
BUSY_SESSION_COUNT_STATEMENT = text(
    "SELECT count(*) FROM pg_stat_activity"
    " WHERE datname = current_database() AND backend_type = 'client backend'"
    " AND pid <> pg_backend_pid() AND state <> 'idle'"
)
START_AT_POSTTRAN_OPTIONS = ("--start-at", "POSTTRAN")
# Step 6's state and run count while it has never run: the seed leaves it PENDING with no run.
FIRST_BACKUP_NEVER_RUN = (run_ledger.STEP_PENDING, 0)
# Step 6's state and run count after job tran-backup ran it and a later run-all skipped it.
FIRST_BACKUP_RUN_THEN_SKIPPED = (run_ledger.STEP_COMPLETED, 2)

SAMPLE_DAILY_PATH = legacy_oracle.ImagePath(
    REPO_ROOT / "data" / "EBCDIC", legacy_oracle.DALYTRAN_IMAGE
)
# The 150th staged record fails before it changes anything, so records 1-149 stay posted.
FAILING_DAILY_RECORD_NUMBER = 150
VALIDATION_FUNCTION_NAME = "ValidateDailyTransaction"
INJECTED_IO_STATEMENT = "INJECTED"
INJECTED_IO_REASON = "injected I/O error"

INTCALC_STEP = 14
COMBTRAN_STEP = 16
# The generation each producer step writes in a run-all from the seed, whose catalog is empty:
# its base and generation number. Steps 6 and 15 write the first two TRANSACT.BKUP generations.
PRODUCED_GENERATIONS: dict[int, tuple[gdg.GdgBase, int]] = {
    FIRST_TRANBKP_STEP: (gdg.TRANSACT_BKUP, 1),
    POSTTRAN_STEP: (gdg.DALYREJS, 1),
    INTCALC_STEP: (gdg.SYSTRAN, 1),
    SECOND_TRANBKP_STEP: (gdg.TRANSACT_BKUP, 2),
    COMBTRAN_STEP: (gdg.TRANSACT_COMBINED, 1),
}
PRODUCER_STEPS = tuple(PRODUCED_GENERATIONS)
# Each base's PUBLISHED generations after a clean chain from the seed: one per producer step.
CLEAN_GENERATION_COUNTS = {
    gdg.TRANSACT_BKUP.name: 2,
    gdg.DALYREJS.name: 1,
    gdg.SYSTRAN.name: 1,
    gdg.TRANSACT_COMBINED.name: 1,
}
PLACE_GENERATION_FILE_NAME = "PlaceGenerationFile"
# The rename failure injected after a producer step's commit. Its text must never be logged.
INJECTED_PUBLICATION_ERRNO = errno.EIO
INJECTED_PUBLICATION_REASON = "injected rename failure"
# The commands a LOST generation of the OPEN cycle refuses before executing any step.
LOST_BLOCKED_COMMANDS = (("run-all",), RERUN_COMMANDS[COMBTRAN_STEP])


@dataclass(frozen=True)
class ChainSnapshot:
    """The chain's financial state and outputs at one moment, compared before and after a command.

    accounts maps acct_id to (curr_balance, curr_cycle_credit, curr_cycle_debit) and
    categoryBalances maps (acct_id, tran_type_cd, tran_cat_cd) to the balance. generationHashes
    holds the SHA-256 of each GDG base's (0) file, the generation every later step resolves, and
    generationCounts its PUBLISHED generations, so a rerun writing an identical empty generation
    still shows. statementHashes holds the SHA-256 of STATEMNT.PS and STATEMNT.HTML. A file
    that does not exist hashes to None.
    """

    accounts: dict[str, tuple[Decimal, Decimal, Decimal]]
    categoryBalances: dict[tuple[str, str, int], Decimal]
    transactionCount: int
    generationHashes: dict[str, str | None]
    generationCounts: dict[str, int]
    statementHashes: dict[str, str | None]


@dataclass(frozen=True)
class TableContent:
    """One table's rows keyed by primary key, and the sorted xmin of every row version it holds.

    A loader that rolls back its DELETE and INSERT leaves both unchanged. A committed reload of
    the same image would leave the rows equal but give every row a new xmin.
    """

    rowsByKey: dict[tuple[object, ...], dict[str, object]]
    rowVersions: list[str]


@dataclass(frozen=True)
class InterruptionStart:
    """How the SIGKILL case reaches POSTTRAN from the seed, and the step 6 history it keeps.

    backupCommand runs step 6 before the killed run-all, or is None to leave step 6 PENDING.
    runAllOptions are the killed run-all's options. firstBackupHistory is step 6's state and
    run count once the killed run-all has gone, which neither the refused nor the forced
    run-all may change.
    """

    backupCommand: tuple[str, ...] | None
    runAllOptions: tuple[str, ...]
    firstBackupHistory: tuple[str, int]


INTERRUPTION_STARTS = [
    pytest.param(
        InterruptionStart(RERUN_COMMANDS[FIRST_TRANBKP_STEP], (), FIRST_BACKUP_RUN_THEN_SKIPPED),
        id="step-6-completed",
    ),
    pytest.param(
        InterruptionStart(None, START_AT_POSTTRAN_OPTIONS, FIRST_BACKUP_NEVER_RUN),
        id="step-6-pending",
    ),
]


def ReadAccountBalances(connection: Connection) -> dict[str, tuple[Decimal, Decimal, Decimal]]:
    """Return every account's balance, cycle credit and cycle debit, keyed by acct_id."""
    accounts = Account.__table__.c
    statement = select(
        accounts.acct_id,
        accounts.curr_balance,
        accounts.curr_cycle_credit,
        accounts.curr_cycle_debit,
    )
    return {
        row.acct_id: (row.curr_balance, row.curr_cycle_credit, row.curr_cycle_debit)
        for row in connection.execute(statement)
    }


def ReadCategoryBalances(connection: Connection) -> dict[tuple[str, str, int], Decimal]:
    """Return every transaction category balance, keyed by its 17-byte TCATBALF key."""
    balances = TransactionCategoryBalance.__table__.c
    statement = select(
        balances.acct_id, balances.tran_type_cd, balances.tran_cat_cd, balances.balance
    )
    return {
        (row.acct_id, row.tran_type_cd, row.tran_cat_cd): row.balance
        for row in connection.execute(statement)
    }


def CountTransactions(connection: Connection) -> int:
    """Return the number of rows in transactions.transactions."""
    statement = select(func.count()).select_from(Transaction.__table__)
    return connection.execute(statement).scalar_one()


def HasPostedTransaction(engine: Engine) -> bool:
    """Return whether transactions.transactions holds a row, as POSTTRAN's first write leaves."""
    with engine.connect() as connection:
        return CountTransactions(connection) > 0


def FileSha256(path: Path | None) -> str | None:
    """Return the SHA-256 of a file's bytes, or None when there is no such file."""
    if path is None or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def GenerationHashes(connection: Connection, outputDir: Path) -> dict[str, str | None]:
    """Return the SHA-256 of each GDG base's (0) file, resolved over PUBLISHED catalog rows."""
    return {
        baseName: FileSha256(compare.LatestPublishedGeneration(connection, baseName, outputDir))
        for baseName in GDG_BASE_NAMES
    }


def PublishedGenerationCounts(connection: Connection) -> dict[str, int]:
    """Return each GDG base's number of PUBLISHED generations."""
    statement = (
        select(GDG_GENERATIONS.c.base_name, func.count())
        .where(GDG_GENERATIONS.c.state == run_ledger.GDG_PUBLISHED)
        .group_by(GDG_GENERATIONS.c.base_name)
    )
    publishedCounts = dict(connection.execute(statement).tuples().all())
    return {baseName: publishedCounts.get(baseName, 0) for baseName in GDG_BASE_NAMES}


def StatementPath(outputDir: Path, fileName: str) -> Path:
    """Return the path CREASTMT writes a statement file to, under CARDDEMO_OUTPUT_DIR."""
    return outputDir / STATEMENTS_DIRECTORY_NAME / fileName


def ChainOutcomeSnapshot(engine: Engine, outputDir: Path) -> ChainSnapshot:
    """Read the chain's balances, transaction count, generations and statements now."""
    with engine.connect() as connection:
        snapshot = ChainSnapshot(
            accounts=ReadAccountBalances(connection),
            categoryBalances=ReadCategoryBalances(connection),
            transactionCount=CountTransactions(connection),
            generationHashes=GenerationHashes(connection, outputDir),
            generationCounts=PublishedGenerationCounts(connection),
            statementHashes={
                fileName: FileSha256(StatementPath(outputDir, fileName))
                for fileName in STATEMENT_FIXTURES
            },
        )
    return snapshot


def CleanGenerationDifferences(connection: Connection, outputDir: Path) -> list[str]:
    """Return how each CLEAN_GENERATION_FIXTURES (0) generation differs from its fixture.

    Each generation is resolved over PUBLISHED catalog rows and compared byte for byte after
    removing the "\\n" terminators; one that is not published is reported missing. Every message
    is prefixed with its base and relative generation, for example "SYSTRAN(0): ".
    """
    differences = []
    for baseName, fixturePath in CLEAN_GENERATION_FIXTURES:
        generationPath = compare.LatestPublishedGeneration(connection, baseName, outputDir)
        generationDifferences = compare.CompareFixedWidthFiles(
            generationPath, fixturePath, TRANSACTION_LAYOUT_NAME
        )
        differences.extend(f"{baseName}(0): {difference}" for difference in generationDifferences)
    return differences


def CleanChainDifferences(connection: Connection, outputDir: Path) -> list[str]:
    """Return how the database and the (0) generations differ from a clean chain's fixtures.

    The generations are TRANSACT.BKUP(0) and SYSTRAN(0), which COMBTRAN resolves, and the
    TRANSACT.COMBINED(0) it writes from them; the tables are accounts, category balances and
    the transactions COMBTRAN reloads.
    """
    combinedRecords = compare.ReadFixedWidthRecords(CLEAN_COMBINED_DAT)
    return [
        *compare.CompareTableWithCsv(connection, legacy_oracle.ACCOUNTS_TABLE, CLEAN_ACCOUNTS_CSV),
        *compare.CompareTableWithCsv(
            connection, legacy_oracle.CATEGORY_BALANCES_TABLE, CLEAN_CATEGORY_BALANCES_CSV
        ),
        *CleanGenerationDifferences(connection, outputDir),
        *compare.CompareTableWithRecords(
            connection, legacy_oracle.TRANSACTIONS_TABLE, combinedRecords, TRANSACTION_LAYOUT_NAME
        ),
    ]


def AssertCleanChainResult(engine: Engine, outputDir: Path) -> None:
    """Assert the outcome a clean chain over the sample data has, from the committed fixtures.

    Accounts equal intcalc_accounts.csv and category balances posttran_tcatbal.csv. COMBTRAN's
    inputs, TRANSACT.BKUP(0) and SYSTRAN(0), equal tranbkp_run2_bkup.dat and
    intcalc_systran.dat, and its output TRANSACT.COMBINED(0) equals combtran_combined.dat, each
    byte for byte. transactions holds exactly the COMBINED records: their count first, then
    every field.
    """
    with engine.connect() as connection:
        differences = CleanChainDifferences(connection, outputDir)
    assert differences == []


def StatementDifferences(outputDir: Path) -> list[str]:
    """Return how STATEMNT.PS and STATEMNT.HTML differ from the CREASTMT fixtures."""
    return [
        difference
        for fileName, expectedPath in STATEMENT_FIXTURES.items()
        for difference in compare.CompareFixedWidthFiles(
            StatementPath(outputDir, fileName), expectedPath, fileName
        )
    ]


def SeedRunCeiling(environment: BatchEnvironment) -> int:
    """Return the highest run_id the Alembic seed recorded; every later run is this test's."""
    seedRuns = environment.snapshot.tableRows[JOB_RUNS.fullname]
    return max(seedRun["run_id"] for seedRun in seedRuns)


def NewRuns(environment: BatchEnvironment) -> list[dict[str, object]]:
    """Return the batch.job_runs rows recorded after the seed, oldest first."""
    statement = (
        select(JOB_RUNS.c.run_id, JOB_RUNS.c.step_no, JOB_RUNS.c.status, JOB_RUNS.c.exit_code)
        .where(JOB_RUNS.c.run_id > SeedRunCeiling(environment))
        .order_by(JOB_RUNS.c.run_id)
    )
    with environment.engine.connect() as connection:
        return [dict(row) for row in connection.execute(statement).mappings()]


def StepsWithStatus(runs: list[dict[str, object]], status: str) -> list[int]:
    """Return, in run order, the step numbers of the runs that ended with status."""
    return [run["step_no"] for run in runs if run["status"] == status]


def RunStatus(engine: Engine, runId: int) -> str:
    """Return the status of one batch.job_runs row."""
    statement = select(JOB_RUNS.c.status).where(JOB_RUNS.c.run_id == runId)
    with engine.connect() as connection:
        return connection.execute(statement).scalar_one()


def StepStates(engine: Engine, stepNumbers: range | tuple[int, ...]) -> dict[int, str]:
    """Return the state of each step in the current cycle, keyed by step number."""
    return {stepNo: StepRow(engine, stepNo)["state"] for stepNo in stepNumbers}


def FirstBackupHistory(engine: Engine) -> tuple[str, int]:
    """Return step 6's state in the current cycle and its number of runs, SKIPPED ones included."""
    return StepRow(engine, FIRST_TRANBKP_STEP)["state"], CountJobRuns(engine, FIRST_TRANBKP_STEP)


def BackupGenerationCount(engine: Engine) -> int:
    """Return the number of PUBLISHED TRANSACT.BKUP generations."""
    with engine.connect() as connection:
        return PublishedGenerationCounts(connection)[gdg.TRANSACT_BKUP.name]


def AssertCyclesAtSeed(environment: BatchEnvironment) -> None:
    """Assert batch.cycles still holds exactly the seed's rows: no cycle opened or abandoned."""
    with environment.engine.connect() as connection:
        currentRows = ReadModelRows(connection, CYCLES)
    seedRows = environment.snapshot.tableRows[CYCLES.fullname]
    assert RowsByPrimaryKey(currentRows, CYCLES) == RowsByPrimaryKey(seedRows, CYCLES)


def ReadModelRows(connection: Connection, sourceTable: Table) -> list[dict[str, object]]:
    """Return every row of sourceTable without its generated *_sort columns."""
    storedColumns = [
        tableColumn for tableColumn in sourceTable.columns if tableColumn.computed is None
    ]
    return [dict(row) for row in connection.execute(select(*storedColumns)).mappings()]


def RowsByPrimaryKey(
    rows: list[dict[str, object]], sourceTable: Table
) -> dict[tuple[object, ...], dict[str, object]]:
    """Index rows of sourceTable by their primary-key values."""
    keyNames = [keyColumn.name for keyColumn in sourceTable.primary_key.columns]
    return {tuple(row[keyName] for keyName in keyNames): row for row in rows}


def ReadTableContent(engine: Engine, sourceTable: Table) -> TableContent:
    """Return sourceTable's rows and the xmin of each row version, read in one transaction."""
    versionStatement = select(cast(column(XMIN_COLUMN), Text)).select_from(sourceTable)
    with engine.connect() as connection:
        rows = ReadModelRows(connection, sourceTable)
        rowVersions = sorted(connection.execute(versionStatement).scalars().all())
    return TableContent(RowsByPrimaryKey(rows, sourceTable), rowVersions)


def AssertLoaderTablesUnchanged(environment: BatchEnvironment) -> None:
    """Assert the staged daily file and the users still hold exactly the Alembic seed rows."""
    for sourceTable in UNTOUCHED_LOADER_TABLES:
        with environment.engine.connect() as connection:
            currentRows = ReadModelRows(connection, sourceTable)
        seedRows = environment.snapshot.tableRows[sourceTable.fullname]
        assert RowsByPrimaryKey(currentRows, sourceTable) == RowsByPrimaryKey(seedRows, sourceTable)


def SampleCardNumbers(environment: BatchEnvironment) -> list[str]:
    """Return the seeded cross-reference card numbers, ascending."""
    xrefRows = environment.snapshot.tableRows[CardXref.__table__.fullname]
    return sorted(xrefRow["card_num"] for xrefRow in xrefRows)


def RunAll(environment: BatchEnvironment, *options: str) -> CliOutcome:
    """Run run-all with the given options and the frozen reconciliation clock."""
    return InvokeCli(["run-all", *options, "--as-of", environment.asOfText])


def InjectedExitCode(stepNo: int) -> int:
    """Return the status InjectStepFailure raises by default: abend 16 when per-write, else 12."""
    if stepNo in PER_WRITE_STEPS:
        return EXIT_ABEND
    return EXIT_FAILURE


def RecoveryExitCode(failedStepNo: int) -> int:
    """Return the status of the run-all that finishes the chain from a failed step onwards.

    POSTTRAN rejects sample records, so the run exits 4 when it executes POSTTRAN, that is when
    the failed step is POSTTRAN or earlier; otherwise every step it executes ends 0.
    """
    if failedStepNo <= POSTTRAN_STEP:
        return EXIT_REJECTS
    return EXIT_OK


def ChainFinishOptions(failedStepNo: int) -> tuple[str, ...]:
    """Return the run-all options that resume past a step that failed at its entry point.

    A FAILED per-write step is refused until --force, which here reruns from a state the
    entry-point failure left untouched; a FAILED atomic or files-only step is simply retried.
    """
    if failedStepNo in PER_WRITE_STEPS:
        return ("--force",)
    return ()


def StepId(stepNo: int) -> str:
    """Return the pytest id of a chain step parameter, for example step-06."""
    return f"step-{stepNo:02d}"


def ReloadMarkerSpec(environment: BatchEnvironment) -> SyntheticTransactionSpec:
    """Return one transaction on the first sample card; a committed TRANFILE reload deletes it."""
    return SyntheticTransactionSpec(
        cardNums=SampleCardNumbers(environment)[:1], perCard=1, amount=RELOAD_MARKER_AMOUNT
    )


def FailLoaderStepInSeedAll(stepNo: int, environment: BatchEnvironment) -> None:
    """Fail seed-all inside loader step stepNo's transaction; assert its table rolled back.

    The marker transaction makes step 5's rollback visible too: a committed TRANFILE reload,
    whose image holds only the skipped priming record, would delete it.
    """
    sourceTable = LOADER_TABLES[stepNo]
    InsertSyntheticTransactions(environment.engine, ReloadMarkerSpec(environment))
    contentBefore = ReadTableContent(environment.engine, sourceTable)
    with pytest.MonkeyPatch.context() as failurePatch:
        InjectAtomicRollbackFailure(failurePatch, stepNo)
        outcome = InvokeCli(["seed-all"])
    assert outcome.exitCode == EXIT_FAILURE, outcome.output
    assert ReadTableContent(environment.engine, sourceTable) == contentBefore


def FailStepInRunAll(stepNo: int, environment: BatchEnvironment) -> None:
    """Fail step stepNo at its runner's entry point in a run-all from the seed."""
    with pytest.MonkeyPatch.context() as failurePatch:
        InjectStepFailure(failurePatch, stepNo)
        outcome = RunAll(environment)
    assert outcome.exitCode == InjectedExitCode(stepNo), outcome.output


def FailStep(stepNo: int, environment: BatchEnvironment) -> None:
    """Fail step stepNo where it runs: in seed-all for a seeded loader, else in run-all."""
    if stepNo in SEEDED_STEPS:
        FailLoaderStepInSeedAll(stepNo, environment)
    else:
        FailStepInRunAll(stepNo, environment)
    assert StepRow(environment.engine, stepNo)["state"] == run_ledger.STEP_FAILED


def AssertPerWriteRefused(stepNo: int, environment: BatchEnvironment, state: str) -> None:
    """Assert run-all refuses a FAILED or INTERRUPTED per-write step by name and changes nothing.

    No step executes before the refusal: no run, not even a SKIPPED one, is recorded, and no
    balance, category balance, transaction, generation or statement moves.
    """
    snapshotBefore = ChainOutcomeSnapshot(environment.engine, environment.outputDir)
    runCountBefore = len(NewRuns(environment))
    outcome = RunAll(environment)
    stepRow = StepRow(environment.engine, stepNo)
    refusal = batch_chain.PER_WRITE_REFUSED_TEMPLATE.format(
        stepNo=stepNo,
        jobName=EXPECTED_LEGACY_ORDER[stepNo - 1],
        cycleId=stepRow["cycle_id"],
        state=state,
    )
    assert outcome.exitCode == EXIT_FAILURE, outcome.output
    assert refusal in outcome.output
    assert stepRow["state"] == state
    assert len(NewRuns(environment)) == runCountBefore
    assert ChainOutcomeSnapshot(environment.engine, environment.outputDir) == snapshotBefore


def RecoverFromFailure(stepNo: int, environment: BatchEnvironment) -> None:
    """Resume the cycle past failed step stepNo and assert the clean chain result.

    An atomic or files-only step is retried by the next run-all. A per-write step is first
    refused, then rerun with --force, which here starts from untouched data.
    """
    if stepNo in PER_WRITE_STEPS:
        AssertPerWriteRefused(stepNo, environment, run_ledger.STEP_FAILED)
    outcome = RunAll(environment, *ChainFinishOptions(stepNo))
    assert outcome.exitCode == RecoveryExitCode(stepNo), outcome.output
    assert StepRow(environment.engine, stepNo)["state"] == run_ledger.STEP_COMPLETED
    assert StepRow(environment.engine, FINAL_STEP)["cycle_status"] == run_ledger.CYCLE_COMPLETED
    AssertCleanChainResult(environment.engine, environment.outputDir)


def StopChainAfterStep(stepNo: int, environment: BatchEnvironment) -> None:
    """Run the chain from the seed until step stepNo has completed, and stop it right there."""
    stopStepNo = NEXT_EXECUTED_STEP.get(stepNo)
    with pytest.MonkeyPatch.context() as failurePatch:
        if stopStepNo is not None:
            InjectStepFailure(failurePatch, stopStepNo)
        outcome = RunAll(environment)
    expectedExitCode = EXIT_REJECTS if stopStepNo is None else InjectedExitCode(stopStepNo)
    assert outcome.exitCode == expectedExitCode, outcome.output
    assert CountJobRuns(environment.engine, stepNo, run_ledger.RUN_COMPLETED) == 1


def AssertRerunSkipped(stepNo: int, environment: BatchEnvironment) -> None:
    """Assert job <name> skips step stepNo, COMPLETED in the OPEN cycle, with a SKIPPED row."""
    outcome = InvokeCli(RERUN_COMMANDS[stepNo])
    skipped = batch_chain.STEP_SKIPPED_TEMPLATE.format(
        stepNo=stepNo,
        jobName=EXPECTED_LEGACY_ORDER[stepNo - 1],
        cycleId=StepRow(environment.engine, stepNo)["cycle_id"],
    )
    assert outcome.exitCode == EXIT_OK, outcome.output
    assert skipped in outcome.output
    assert CountJobRuns(environment.engine, stepNo, run_ledger.RUN_SKIPPED) == 1


def AssertFinalStepNotRerun(environment: BatchEnvironment) -> None:
    """Assert job tran-index cannot rerun step 18: completing it closed the cycle."""
    outcome = InvokeCli(RERUN_COMMANDS[FINAL_STEP])
    assert outcome.exitCode == EXIT_FAILURE, outcome.output
    assert batch_chain.NO_OPEN_CYCLE_MESSAGE in outcome.output
    assert StepRow(environment.engine, FINAL_STEP)["cycle_status"] == run_ledger.CYCLE_COMPLETED


def RerunCompletedStep(stepNo: int, environment: BatchEnvironment) -> None:
    """Re-invoke completed step stepNo and assert it ran no second time."""
    if stepNo == FINAL_STEP:
        AssertFinalStepNotRerun(environment)
    else:
        AssertRerunSkipped(stepNo, environment)
    assert CountJobRuns(environment.engine, stepNo, run_ledger.RUN_COMPLETED) == 1


def FinishStoppedChain(stepNo: int, environment: BatchEnvironment) -> None:
    """Finish the chain StopChainAfterStep stopped after step stepNo; step 18 left nothing."""
    stopStepNo = NEXT_EXECUTED_STEP.get(stepNo)
    if stopStepNo is None:
        return
    outcome = RunAll(environment, *ChainFinishOptions(stopStepNo))
    assert outcome.exitCode == RecoveryExitCode(stopStepNo), outcome.output


def StartAtTranBackupFromSeed(environment: BatchEnvironment) -> list[dict[str, object]]:
    """Run run-all --start-at TRANBKP from the seed, stopped by a failure at step 15.

    Returns the runs it recorded: step 6 executed, as TRANBKP's first incomplete occurrence,
    then 7-12 skipped, 13 and 14 completed, and 15 failed.
    """
    with pytest.MonkeyPatch.context() as failurePatch:
        InjectStepFailure(failurePatch, SECOND_TRANBKP_STEP)
        outcome = RunAll(environment, "--start-at", "TRANBKP")
    runs = NewRuns(environment)
    assert outcome.exitCode == EXIT_FAILURE, outcome.output
    assert [run["step_no"] for run in runs] == list(
        range(FIRST_TRANBKP_STEP, SECOND_TRANBKP_STEP + 1)
    )
    assert StepsWithStatus(runs, run_ledger.RUN_COMPLETED) == [FIRST_TRANBKP_STEP, *PER_WRITE_STEPS]
    assert StepRow(environment.engine, SECOND_TRANBKP_STEP)["state"] == run_ledger.STEP_FAILED
    return runs


def StageSyntheticDailyFile(environment: BatchEnvironment) -> Path:
    """Write 20,000 synthetic daily records over the sample cards and stage them as DALYTRAN."""
    dailyPath = environment.outputDir.parent / SYNTHETIC_DAILY_FILE_NAME
    WriteSyntheticDailyImage(dailyPath, SYNTHETIC_RECORD_COUNT, SampleCardNumbers(environment))
    outcome = InvokeCli(["load", "daily-transactions", "--daily-file", str(dailyPath)])
    assert outcome.exitCode == EXIT_OK, outcome.output
    return dailyPath


def HasBusySession(engine: Engine) -> bool:
    """Return whether another client session on the engine's database is not idle."""
    with engine.connect() as connection:
        return connection.execute(BUSY_SESSION_COUNT_STATEMENT).scalar_one() > 0


def KillRunAllDuringPosttran(environment: BatchEnvironment, runAllOptions: tuple[str, ...]) -> None:
    """SIGKILL a run-all subprocess once POSTTRAN has posted, and wait for its sessions to settle.

    PostgreSQL rolls back the killed process's open transaction and releases its batch window
    when its connections drop; the writes POSTTRAN already committed stay. The window session
    can end while the job session still executes a write or its COMMIT, so the wait lasts until
    no other session on the test database is busy either.
    """
    engine = environment.engine
    process = StartCli(["run-all", *runAllOptions, "--as-of", environment.asOfText])
    try:
        WaitUntil(lambda: HasPostedTransaction(engine), POSTING_WAIT_SECONDS, "a posting")
    finally:
        KillProcess(process)
    WaitUntil(lambda: not IsBatchWindowHeld(engine), WINDOW_RELEASE_SECONDS, "the window release")
    WaitUntil(lambda: not HasBusySession(engine), WINDOW_RELEASE_SECONDS, "the killed sessions")


def InterruptPosttran(environment: BatchEnvironment, start: InterruptionStart) -> None:
    """Reach POSTTRAN from the seed as start sets out, and SIGKILL the run-all once it posts."""
    if start.backupCommand is not None:
        outcome = InvokeCli(start.backupCommand)
        assert outcome.exitCode == EXIT_OK, outcome.output
    KillRunAllDuringPosttran(environment, start.runAllOptions)


def InjectIoErrorAtValidation(monkeypatch: pytest.MonkeyPatch, recordNumber: int) -> None:
    """Make POSTTRAN's recordNumber-th 1500-VALIDATE-TRAN raise an I/O error.

    The validation runs once per staged record, before the record writes anything, so records
    before it stay posted. The OperationalError is POSTTRAN's 9999-ABEND-PROGRAM, exit 16.
    posttran binds the function by name, so both bindings are replaced.
    """
    originalValidate = cbtrn02c.ValidateDailyTransaction
    callNumbers = itertools.count(1)

    def FailingValidate(postingInput: cbtrn02c.PostingInput) -> cbtrn02c.RejectOutcome | None:
        if next(callNumbers) == recordNumber:
            raise OperationalError(INJECTED_IO_STATEMENT, {}, OSError(INJECTED_IO_REASON))
        return originalValidate(postingInput)

    monkeypatch.setattr(cbtrn02c, VALIDATION_FUNCTION_NAME, FailingValidate)
    monkeypatch.setattr(posttran, VALIDATION_FUNCTION_NAME, FailingValidate)


def FailPosttranAtRecord(environment: BatchEnvironment, recordNumber: int) -> None:
    """Run run-all --start-at POSTTRAN from the seed with an I/O error at record recordNumber.

    POSTTRAN abends with exit 16, leaving step 13 FAILED and step 6 PENDING with no run.
    """
    with pytest.MonkeyPatch.context() as injection:
        InjectIoErrorAtValidation(injection, recordNumber)
        outcome = RunAll(environment, *START_AT_POSTTRAN_OPTIONS)
    assert outcome.exitCode == EXIT_ABEND, outcome.output
    assert StepRow(environment.engine, POSTTRAN_STEP)["state"] == run_ledger.STEP_FAILED
    assert FirstBackupHistory(environment.engine) == FIRST_BACKUP_NEVER_RUN


def ReadOracleState(engine: Engine) -> legacy_oracle.OracleState:
    """Read every dataset as the reconciliation oracle holds it."""
    with engine.connect() as connection:
        return compare.ReadTableAsOracleState(connection)


def AssertKilledPosttranInterrupted(environment: BatchEnvironment) -> None:
    """Assert the killed POSTTRAN run, step 13's only run, was reclassified INTERRUPTED."""
    stepRow = StepRow(environment.engine, POSTTRAN_STEP)
    assert RunStatus(environment.engine, stepRow["last_run_id"]) == run_ledger.RUN_INTERRUPTED
    assert CountJobRuns(environment.engine, POSTTRAN_STEP) == 1


def OraclePosting(engine: Engine, dailyRecords: list[str]) -> legacy_oracle.PosttranOutcome:
    """Return the oracle's POSTTRAN of dailyRecords over the database's state, at the DB2 clock."""
    return legacy_oracle.RunPosttran(
        ReadOracleState(engine), dailyRecords, legacy_oracle.DB2_CLOCK_TEXT
    )


def AssertPostingStateMatchesOracle(engine: Engine, expected: legacy_oracle.OracleState) -> None:
    """Assert the accounts, category balances and transactions equal the oracle's state."""
    actual = ReadOracleState(engine)
    assert actual.accounts == expected.accounts
    assert actual.categoryBalances == expected.categoryBalances
    assert actual.transactions == expected.transactions


def IsProducedGeneration(finalPath: Path, stepNo: int) -> bool:
    """Return whether finalPath is the final file of the generation step stepNo writes."""
    base, generation = PRODUCED_GENERATIONS[stepNo]
    fileName = gdg.GENERATION_FILE_TEMPLATE.format(generation=generation)
    return (finalPath.parent.name, finalPath.name) == (base.name, fileName)


def InjectPublicationFailure(monkeypatch: pytest.MonkeyPatch, stepNo: int) -> None:
    """Make the first publication of step stepNo's generation fail with EIO before its rename.

    It fails after the step has committed, so the catalog row stays COMMITTED and the .part file
    in place, as a failed rename leaves them. Every other generation publishes unchanged.
    """
    originalPlace = gdg.PlaceGenerationFile
    failedPaths: list[Path] = []

    def FailingPlace(finalPath: Path, expectedSha256: str, connection: Connection) -> str:
        if IsProducedGeneration(finalPath, stepNo) and not failedPaths:
            failedPaths.append(finalPath)
            raise OSError(INJECTED_PUBLICATION_ERRNO, INJECTED_PUBLICATION_REASON)
        return originalPlace(finalPath, expectedSha256, connection)

    monkeypatch.setattr(gdg, PLACE_GENERATION_FILE_NAME, FailingPlace)


def InjectLostPublication(monkeypatch: pytest.MonkeyPatch, stepNo: int) -> None:
    """Delete step stepNo's generation file and its .part as publication comes to place them.

    The step has committed, so publication finds neither file and marks the row LOST.
    """
    originalPlace = gdg.PlaceGenerationFile

    def LosingPlace(finalPath: Path, expectedSha256: str, connection: Connection) -> str:
        if IsProducedGeneration(finalPath, stepNo):
            gdg.PartPath(finalPath).unlink(missing_ok=True)
            finalPath.unlink(missing_ok=True)
        return originalPlace(finalPath, expectedSha256, connection)

    monkeypatch.setattr(gdg, PLACE_GENERATION_FILE_NAME, LosingPlace)


def ProducedGenerationState(engine: Engine, stepNo: int) -> str:
    """Return the catalog state of the generation step stepNo writes in a run-all from the seed."""
    base, generation = PRODUCED_GENERATIONS[stepNo]
    statement = select(GDG_GENERATIONS.c.state).where(
        GDG_GENERATIONS.c.base_name == base.name, GDG_GENERATIONS.c.generation == generation
    )
    with engine.connect() as connection:
        return connection.execute(statement).scalar_one()


def PublicationErrorMessage(stepNo: int) -> str:
    """Return the ERROR line the chain logs for the publication failure injected at stepNo."""
    ioError = batch_chain.FILE_ERROR_TEMPLATE.format(
        jobName=EXPECTED_LEGACY_ORDER[stepNo - 1],
        errorClass=OSError.__name__,
        errnoName=errno.errorcode[INJECTED_PUBLICATION_ERRNO],
    )
    return batch_chain.PUBLICATION_ERROR_TEMPLATE.format(stepNo=stepNo, ioError=ioError)


def LostGenerationMessage(stepNo: int, cycleId: int) -> str:
    """Return the message naming step stepNo's generation LOST in cycleId, with the recovery."""
    base, generation = PRODUCED_GENERATIONS[stepNo]
    return gdg.LOST_GENERATION_TEMPLATE.format(
        base=base.name, generation=generation, cycleId=cycleId
    )


def FailPublicationInRunAll(stepNo: int, environment: BatchEnvironment) -> None:
    """Run run-all from the seed with step stepNo's publication failing once after its commit.

    The error is reported by class and errno symbol, never by its text.
    """
    with pytest.MonkeyPatch.context() as failurePatch:
        InjectPublicationFailure(failurePatch, stepNo)
        outcome = RunAll(environment)
    assert outcome.exitCode == EXIT_FAILURE, outcome.output
    assert PublicationErrorMessage(stepNo) in outcome.output
    assert INJECTED_PUBLICATION_REASON not in outcome.output


def LoseGenerationInRunAll(stepNo: int, environment: BatchEnvironment) -> str:
    """Run run-all from the seed with step stepNo's generation lost before it is published.

    Returns the message naming the LOST generation, which the run-all logged.
    """
    with pytest.MonkeyPatch.context() as lossPatch:
        InjectLostPublication(lossPatch, stepNo)
        outcome = RunAll(environment)
    lostMessage = LostGenerationMessage(stepNo, StepRow(environment.engine, stepNo)["cycle_id"])
    assert outcome.exitCode == EXIT_FAILURE, outcome.output
    assert lostMessage in outcome.output
    return lostMessage


def AssertChainStoppedAtCompletedStep(
    stepNo: int, environment: BatchEnvironment, generationState: str
) -> None:
    """Assert the chain stopped at step stepNo, which with its run stayed COMPLETED.

    Its generation is in generationState, no later step has a run, and the cycle is OPEN.
    """
    engine = environment.engine
    stepRow = StepRow(engine, stepNo)
    assert stepRow["state"] == run_ledger.STEP_COMPLETED
    assert RunStatus(engine, stepRow["last_run_id"]) == run_ledger.RUN_COMPLETED
    assert ProducedGenerationState(engine, stepNo) == generationState
    assert max(run["step_no"] for run in NewRuns(environment)) == stepNo
    assert stepRow["cycle_status"] == run_ledger.CYCLE_OPEN


def ResumedExitCode(stepNo: int) -> int:
    """Return the status of the run-all that resumes after step stepNo completed.

    It executes POSTTRAN, which rejects sample records, only when step stepNo precedes it.
    """
    if stepNo < POSTTRAN_STEP:
        return EXIT_REJECTS
    return EXIT_OK


def ResumeAfterPublicationFailure(stepNo: int, environment: BatchEnvironment) -> None:
    """Run run-all unforced and assert it publishes, skips step stepNo and finishes the chain.

    The command's catalog recovery publishes the COMMITTED generation before any step runs.
    """
    engine = environment.engine
    outcome = RunAll(environment)
    assert outcome.exitCode == ResumedExitCode(stepNo), outcome.output
    assert ProducedGenerationState(engine, stepNo) == run_ledger.GDG_PUBLISHED
    assert CountJobRuns(engine, stepNo, run_ledger.RUN_SKIPPED) == 1
    assert CountJobRuns(engine, stepNo, run_ledger.RUN_COMPLETED) == 1
    assert StepRow(engine, FINAL_STEP)["cycle_status"] == run_ledger.CYCLE_COMPLETED


def AssertRefusedByLostGeneration(
    arguments: tuple[str, ...], environment: BatchEnvironment, lostMessage: str
) -> None:
    """Assert a command exits 12 naming the LOST generation and records no run."""
    runCount = len(NewRuns(environment))
    outcome = InvokeCli(arguments)
    assert outcome.exitCode == EXIT_FAILURE, outcome.output
    assert lostMessage in outcome.output
    assert len(NewRuns(environment)) == runCount


def test_chain_has_18_steps_in_legacy_order_with_durability_classes() -> None:
    durabilityByStep = {step.stepNo: step.durability for step in batch_chain.CHAIN}
    assert tuple(step.stepNo for step in batch_chain.CHAIN) == CHAIN_STEP_NUMBERS
    assert tuple(step.job.LegacyName() for step in batch_chain.CHAIN) == EXPECTED_LEGACY_ORDER
    assert durabilityByStep == EXPECTED_DURABILITY
    assert batch_chain.LOADER_STEP_NUMBERS == SEEDED_STEPS
    assert batch_chain.FINAL_STEP.stepNo == FINAL_STEP
    # run_ledger keeps its own copies, which must agree with the chain.
    assert run_ledger.CHAIN_STEP_COUNT == len(batch_chain.CHAIN)
    assert tuple(run_ledger.CHAIN_STEP_NUMBERS) == CHAIN_STEP_NUMBERS
    assert run_ledger.PER_WRITE_STEP_NUMBERS == PER_WRITE_STEPS


def test_tranbkp_definition_is_shared_by_steps_6_and_15() -> None:
    firstOccurrence = batch_chain.CHAIN[FIRST_TRANBKP_STEP - 1]
    secondOccurrence = batch_chain.CHAIN[SECOND_TRANBKP_STEP - 1]
    tranBackup = batch_chain.FindJobDefinition("TRANBKP")
    assert firstOccurrence.job is secondOccurrence.job is tranBackup
    assert batch_chain.FindJobDefinition("tran-backup") is tranBackup
    assert batch_chain.StepsForJob(tranBackup) == (firstOccurrence, secondOccurrence)


def test_run_all_from_alembic_seed_runs_only_steps_6_and_13_to_18_and_exits_4(
    batchEnvironment: BatchEnvironment,
) -> None:
    outcome = RunAll(batchEnvironment)
    runs = NewRuns(batchEnvironment)
    assert outcome.exitCode == EXIT_REJECTS, outcome.output
    assert StepsWithStatus(runs, run_ledger.RUN_COMPLETED) == list(UNSEEDED_STEPS)
    assert StepsWithStatus(runs, run_ledger.RUN_SKIPPED) == list(SEEDED_STEPS)
    assert len(runs) == len(CHAIN_STEP_NUMBERS)
    AssertLoaderTablesUnchanged(batchEnvironment)
    AssertCleanChainResult(batchEnvironment.engine, batchEnvironment.outputDir)
    finalStep = StepRow(batchEnvironment.engine, FINAL_STEP)
    assert finalStep["cycle_status"] == run_ledger.CYCLE_COMPLETED


@pytest.mark.parametrize(
    ("failingStepNo", "injectedExitCode"),
    [
        (POSTTRAN_STEP + 1, LOWEST_STOPPING_EXIT_CODE),
        (SECOND_TRANBKP_STEP, EXIT_FAILURE),
        (POSTTRAN_STEP + 1, EXIT_ABEND),
    ],
    ids=["step-14-rc8", "step-15-rc12", "step-14-rc16"],
)
def test_chain_continues_after_exit_4_and_stops_at_8_or_more(
    failingStepNo: int,
    injectedExitCode: int,
    batchEnvironment: BatchEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = batchEnvironment.engine
    InjectStepFailure(monkeypatch, failingStepNo, injectedExitCode)
    outcome = RunAll(batchEnvironment)
    executedRuns = [
        run for run in NewRuns(batchEnvironment) if run["status"] != run_ledger.RUN_SKIPPED
    ]
    exitCodes = {run["step_no"]: run["exit_code"] for run in executedRuns}
    completedSteps = range(POSTTRAN_STEP, failingStepNo)
    bypassedSteps = range(failingStepNo + 1, FINAL_STEP + 1)
    assert outcome.exitCode == injectedExitCode == max(exitCodes.values()), outcome.output
    assert exitCodes[POSTTRAN_STEP] == EXIT_REJECTS
    assert list(exitCodes) == [FIRST_TRANBKP_STEP, *completedSteps, failingStepNo]
    assert StepStates(engine, completedSteps) == dict.fromkeys(
        completedSteps, run_ledger.STEP_COMPLETED
    )
    assert StepRow(engine, failingStepNo)["state"] == run_ledger.STEP_FAILED
    assert StepStates(engine, bypassedSteps) == dict.fromkeys(
        bypassedSteps, run_ledger.STEP_PENDING
    )


@pytest.mark.parametrize("startAt", START_AT_POSTTRAN_VALUES)
def test_start_at_accepts_step_number_job_name_and_jcl_alias(
    startAt: str, batchEnvironment: BatchEnvironment
) -> None:
    engine = batchEnvironment.engine
    outcome = RunAll(batchEnvironment, "--start-at", startAt)
    runs = NewRuns(batchEnvironment)
    postingSteps = list(range(POSTTRAN_STEP, FINAL_STEP + 1))
    assert outcome.exitCode == EXIT_REJECTS, outcome.output
    # Steps before the start step are not considered: no run, not even a SKIPPED one.
    assert [run["step_no"] for run in runs] == postingSteps
    assert StepsWithStatus(runs, run_ledger.RUN_COMPLETED) == postingSteps
    assert StepRow(engine, FIRST_TRANBKP_STEP)["state"] == run_ledger.STEP_PENDING
    AssertCleanChainResult(engine, batchEnvironment.outputDir)


def test_start_at_tranbkp_resolves_first_incomplete_occurrence(
    batchEnvironment: BatchEnvironment,
) -> None:
    firstRuns = StartAtTranBackupFromSeed(batchEnvironment)
    outcome = RunAll(batchEnvironment, "--start-at", "tran-backup")
    resumedRuns = NewRuns(batchEnvironment)[len(firstRuns) :]
    resumedSteps = list(range(SECOND_TRANBKP_STEP, FINAL_STEP + 1))
    assert outcome.exitCode == EXIT_OK, outcome.output
    # Step 6, COMPLETED, is passed over: the run starts at the FAILED step 15.
    assert [run["step_no"] for run in resumedRuns] == resumedSteps
    assert StepsWithStatus(resumedRuns, run_ledger.RUN_COMPLETED) == resumedSteps
    assert CountJobRuns(batchEnvironment.engine, FIRST_TRANBKP_STEP) == 1
    AssertCleanChainResult(batchEnvironment.engine, batchEnvironment.outputDir)


@pytest.mark.parametrize("startAt", UNKNOWN_START_AT_VALUES)
def test_start_at_unknown_step_is_a_usage_error_before_the_batch_window(
    startAt: str, batchEnvironment: BatchEnvironment
) -> None:
    """An unknown --start-at, a number of any length included, exits 2 and changes nothing.

    Click refuses it while parsing the options, naming --start-at and its accepted forms, so
    the batch window is never taken and no cycle is opened or abandoned.
    """
    outcome = RunAll(batchEnvironment, "--start-at", startAt)
    assert outcome.exitCode == USAGE_ERROR_EXIT, outcome.output
    assert START_AT_USAGE_ERROR in outcome.output
    assert WINDOW_CLOSED_LOG not in outcome.output
    assert TRACEBACK_HEADER not in outcome.output
    assert NewRuns(batchEnvironment) == []
    AssertCyclesAtSeed(batchEnvironment)


def test_run_chain_refuses_unknown_start_at_built_in_code(
    jobContext: JobContext, batchEnvironment: BatchEnvironment, caplog: pytest.LogCaptureFixture
) -> None:
    """RunChain itself refuses a ChainOptions start step the CLI never parsed.

    It logs UNKNOWN STEP at ERROR and returns EXIT_FAILURE before any cycle changes.
    """
    caplog.set_level(logging.ERROR, logger=batch_chain.LOGGER.name)
    options = batch_chain.ChainOptions(startAt=UNKNOWN_START_AT_NAME)
    exitCode = batch_chain.RunChain(jobContext, options)
    expectedMessage = batch_chain.UNKNOWN_STEP_TEMPLATE.format(name=UNKNOWN_START_AT_NAME)
    assert exitCode == EXIT_FAILURE
    assert caplog.record_tuples == [(batch_chain.LOGGER.name, logging.ERROR, expectedMessage)]
    assert NewRuns(batchEnvironment) == []
    AssertCyclesAtSeed(batchEnvironment)


@pytest.mark.parametrize("stepNo", CHAIN_STEP_NUMBERS, ids=StepId)
def test_injected_failure_at_each_step_then_rerun_reaches_clean_result(
    stepNo: int, batchEnvironment: BatchEnvironment
) -> None:
    FailStep(stepNo, batchEnvironment)
    RecoverFromFailure(stepNo, batchEnvironment)


@pytest.mark.parametrize("stepNo", UNSEEDED_STEPS, ids=StepId)
def test_rerun_after_each_successful_step_is_a_no_op(
    stepNo: int, batchEnvironment: BatchEnvironment
) -> None:
    engine, outputDir = batchEnvironment.engine, batchEnvironment.outputDir
    StopChainAfterStep(stepNo, batchEnvironment)
    snapshotBefore = ChainOutcomeSnapshot(engine, outputDir)
    RerunCompletedStep(stepNo, batchEnvironment)
    assert ChainOutcomeSnapshot(engine, outputDir) == snapshotBefore
    FinishStoppedChain(stepNo, batchEnvironment)
    AssertCleanChainResult(engine, outputDir)


def test_forced_tran_backup_after_successful_step_6_adds_history_and_moves_last_run_id(
    batchEnvironment: BatchEnvironment,
) -> None:
    engine = batchEnvironment.engine
    firstRun = InvokeCli(RERUN_COMMANDS[FIRST_TRANBKP_STEP])
    forcedRun = InvokeCli(FORCED_TRAN_BACKUP_COMMAND)
    completedRunIds = [
        run["run_id"]
        for run in NewRuns(batchEnvironment)
        if run["step_no"] == FIRST_TRANBKP_STEP and run["status"] == run_ledger.RUN_COMPLETED
    ]
    stepRow = StepRow(engine, FIRST_TRANBKP_STEP)
    snapshot = ChainOutcomeSnapshot(engine, batchEnvironment.outputDir)
    assert (firstRun.exitCode, forcedRun.exitCode) == (EXIT_OK, EXIT_OK), forcedRun.output
    assert len(completedRunIds) == 2
    assert CountJobRuns(engine, FIRST_TRANBKP_STEP, run_ledger.RUN_SKIPPED) == 0
    assert stepRow["state"] == run_ledger.STEP_COMPLETED
    assert stepRow["last_run_id"] == completedRunIds[-1]
    assert snapshot.generationCounts[gdg.TRANSACT_BKUP.name] == 2


def test_files_only_creastmt_failure_is_retried_by_next_run_all(
    batchEnvironment: BatchEnvironment,
) -> None:
    engine, outputDir = batchEnvironment.engine, batchEnvironment.outputDir
    with pytest.MonkeyPatch.context() as failurePatch:
        InjectStepFailure(failurePatch, CREASTMT_STEP, EXIT_ABEND)
        failedRun = RunAll(batchEnvironment)
    assert failedRun.exitCode == EXIT_ABEND, failedRun.output
    assert StepRow(engine, CREASTMT_STEP)["state"] == run_ledger.STEP_FAILED
    noStatements = dict.fromkeys(STATEMENT_FIXTURES)
    assert ChainOutcomeSnapshot(engine, outputDir).statementHashes == noStatements
    retriedRun = RunAll(batchEnvironment)
    assert retriedRun.exitCode == EXIT_OK, retriedRun.output
    assert StepRow(engine, CREASTMT_STEP)["state"] == run_ledger.STEP_COMPLETED
    assert StatementDifferences(outputDir) == []
    AssertCleanChainResult(engine, outputDir)


def test_failed_posttran_with_step_6_pending_is_refused_then_forced_from_partial_state(
    batchEnvironment: BatchEnvironment,
) -> None:
    """Neither the refused nor the forced run-all runs step 6, which would archive the postings."""
    engine = batchEnvironment.engine
    dailyRecords = legacy_oracle.ReadDailyRecords(SAMPLE_DAILY_PATH)
    expectedPartial = OraclePosting(engine, dailyRecords[: FAILING_DAILY_RECORD_NUMBER - 1])
    FailPosttranAtRecord(batchEnvironment, FAILING_DAILY_RECORD_NUMBER)
    AssertPostingStateMatchesOracle(engine, expectedPartial.state)
    AssertPerWriteRefused(POSTTRAN_STEP, batchEnvironment, run_ledger.STEP_FAILED)
    # A legacy restart re-reads DALYTRAN from record 1, so the first record already posted
    # abends on its duplicate TRANSACT WRITE.
    expected = OraclePosting(engine, dailyRecords)
    assert expected.exitCode == EXIT_ABEND
    backupCountBefore = BackupGenerationCount(engine)
    forcedRun = RunAll(batchEnvironment, "--force")
    assert forcedRun.exitCode == expected.exitCode, forcedRun.output
    AssertPostingStateMatchesOracle(engine, expected.state)
    assert FirstBackupHistory(engine) == FIRST_BACKUP_NEVER_RUN
    assert BackupGenerationCount(engine) == backupCountBefore


@pytest.mark.parametrize("stepNo", PRODUCER_STEPS, ids=StepId)
def test_publication_error_after_commit_keeps_step_completed_and_next_run_all_skips_it(
    stepNo: int, batchEnvironment: BatchEnvironment
) -> None:
    engine, outputDir = batchEnvironment.engine, batchEnvironment.outputDir
    FailPublicationInRunAll(stepNo, batchEnvironment)
    AssertChainStoppedAtCompletedStep(stepNo, batchEnvironment, run_ledger.GDG_COMMITTED)
    ResumeAfterPublicationFailure(stepNo, batchEnvironment)
    # The step never ran again: no second posting or interest, and no empty newer TRANSACT.BKUP
    # in place of the archive COMBTRAN resolves.
    assert ChainOutcomeSnapshot(engine, outputDir).generationCounts == CLEAN_GENERATION_COUNTS
    AssertCleanChainResult(engine, outputDir)


def test_generation_lost_in_publication_stops_chain_and_blocks_cycle_until_new_cycle(
    batchEnvironment: BatchEnvironment,
) -> None:
    engine, outputDir = batchEnvironment.engine, batchEnvironment.outputDir
    lostMessage = LoseGenerationInRunAll(POSTTRAN_STEP, batchEnvironment)
    AssertChainStoppedAtCompletedStep(POSTTRAN_STEP, batchEnvironment, run_ledger.GDG_LOST)
    laterSteps = range(INTCALC_STEP, FINAL_STEP + 1)
    assert StepStates(engine, laterSteps) == dict.fromkeys(laterSteps, run_ledger.STEP_PENDING)
    for arguments in LOST_BLOCKED_COMMANDS:
        AssertRefusedByLostGeneration(arguments, batchEnvironment, lostMessage)
    recovery = RunAll(batchEnvironment, "--new-cycle")
    assert recovery.exitCode == EXIT_REJECTS, recovery.output
    assert ProducedGenerationState(engine, POSTTRAN_STEP) == run_ledger.GDG_ABANDONED
    assert StepRow(engine, FINAL_STEP)["cycle_status"] == run_ledger.CYCLE_COMPLETED
    AssertCleanChainResult(engine, outputDir)


@pytest.mark.slow
@pytest.mark.parametrize("interruptionStart", INTERRUPTION_STARTS)
def test_sigkilled_posttran_is_interrupted_refused_then_forced_from_partial_state(
    interruptionStart: InterruptionStart, batchEnvironment: BatchEnvironment
) -> None:
    engine = batchEnvironment.engine
    dailyPath = StageSyntheticDailyFile(batchEnvironment)
    InterruptPosttran(batchEnvironment, interruptionStart)
    partialState = ReadOracleState(engine)
    assert partialState.transactions
    AssertPerWriteRefused(POSTTRAN_STEP, batchEnvironment, run_ledger.STEP_INTERRUPTED)
    AssertKilledPosttranInterrupted(batchEnvironment)
    # A legacy restart re-reads DALYTRAN from record 1, so the first record already posted
    # abends on its duplicate TRANSACT WRITE, after rewriting its balances once more.
    dailyRecords = legacy_oracle.ReadDailyRecords(dailyPath)
    expected = legacy_oracle.RunPosttran(partialState, dailyRecords, legacy_oracle.DB2_CLOCK_TEXT)
    assert expected.exitCode == EXIT_ABEND
    forcedRun = RunAll(batchEnvironment, "--force")
    posttranRunId = StepRow(engine, POSTTRAN_STEP)["last_run_id"]
    assert forcedRun.exitCode == expected.exitCode, forcedRun.output
    assert RunStatus(engine, posttranRunId) == run_ledger.RUN_FAILED
    AssertPostingStateMatchesOracle(engine, expected.state)
    assert FirstBackupHistory(engine) == interruptionStart.firstBackupHistory
