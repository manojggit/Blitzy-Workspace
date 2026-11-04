"""Chain order, exit policy, resume, failure, rerun and interruption of the 18-step batch chain.

Pins batch/orchestration/batch_chain.py and run_ledger.py to the Technical Specification,
Section 0.6.8: the dependency graph and step table, cycles and step identity, and the skip,
resume, failure and force rules. The chain replaces the CardDemo job chains of
scheduler/CardDemo.ca7 and scheduler/CardDemo.controlm and the COND=(4,LT) condition-code
chaining of jcl/TRANBKP.jcl STEP10: a step ending 0 or 4 lets the chain continue, 8 or above
stops it, and run-all exits with the highest status of the steps it ran. The steps exercised
beyond the loaders are TRANBKP (jcl/TRANBKP.jcl), POSTTRAN (jcl/POSTTRAN.jcl, cbl/CBTRN02C.cbl),
INTCALC (jcl/INTCALC.jcl), COMBTRAN (jcl/COMBTRAN.jcl), CREASTMT (jcl/CREASTMT.JCL) and TRANIDX
(jcl/TRANIDX.jcl).

Every case starts from the Alembic seed: the SEED cycle OPEN, steps 1-5 and 7-12 COMPLETED. The
carddemo-batch commands run in process, so failure injections apply; the SIGKILL case runs the
CLI as a subprocess and is marked slow. A clean run is judged against the committed oracle
fixtures (accounts after INTCALC, category balances after POSTTRAN, TRANSACT.COMBINED(0) and the
transactions COMBTRAN reloads), so no case runs a second, reference chain.

Injected failures are scoped with pytest.MonkeyPatch.context() rather than monkeypatch.undo():
the outputDir fixture sets CARDDEMO_OUTPUT_DIR through the same monkeypatch, and undoing it
would send every later command's archives to the repository's var/batch-output.
"""

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from app.models import (
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
from sqlalchemy import Connection, Engine, Table, Text, cast, column, func, select

from batch.orchestration import batch_chain, gdg, run_ledger
from batch.orchestration.job_context import EXIT_ABEND, EXIT_FAILURE, EXIT_OK, EXIT_REJECTS
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

# --- The chain as Section 0.6.8 defines it -------------------------------------------------------
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

# --- Clean-run fixtures and outputs --------------------------------------------------------------
CLEAN_ACCOUNTS_CSV = EXPECTED_DIR / "intcalc_accounts.csv"
CLEAN_CATEGORY_BALANCES_CSV = EXPECTED_DIR / "posttran_tcatbal.csv"
CLEAN_COMBINED_DAT = EXPECTED_DIR / "combtran_combined.dat"
TRANSACTION_LAYOUT_NAME = "TRANSACTION"
STATEMENTS_DIRECTORY_NAME = "statements"
# Each statement file and its fixture; the file name is also its line-only oracle layout.
STATEMENT_FIXTURES = {
    "STATEMNT.PS": EXPECTED_DIR / "creastmt_STATEMNT.PS",
    "STATEMNT.HTML": EXPECTED_DIR / "creastmt_STATEMNT.HTML",
}
GDG_BASE_NAMES = tuple(gdg.GDG_BASES)

# --- Synthetic interruption case -----------------------------------------------------------------
SYNTHETIC_RECORD_COUNT = 20_000
SYNTHETIC_DAILY_FILE_NAME = "AWS.M2.CARDDEMO.DALYTRAN.SYNTHETIC.PS"
RELOAD_MARKER_AMOUNT = Decimal("1.00")
POSTING_WAIT_SECONDS = 180
WINDOW_RELEASE_SECONDS = 30
XMIN_COLUMN = "xmin"


# --- Value types ---------------------------------------------------------------------------------
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


# --- Snapshots -----------------------------------------------------------------------------------
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


# --- The clean-run reference ---------------------------------------------------------------------
def CleanChainDifferences(connection: Connection, outputDir: Path) -> list[str]:
    """Return how the database and TRANSACT.COMBINED(0) differ from a clean chain's fixtures."""
    combinedPath = compare.LatestPublishedGeneration(
        connection, gdg.TRANSACT_COMBINED.name, outputDir
    )
    combinedRecords = compare.ReadFixedWidthRecords(CLEAN_COMBINED_DAT)
    return [
        *compare.CompareTableWithCsv(connection, legacy_oracle.ACCOUNTS_TABLE, CLEAN_ACCOUNTS_CSV),
        *compare.CompareTableWithCsv(
            connection, legacy_oracle.CATEGORY_BALANCES_TABLE, CLEAN_CATEGORY_BALANCES_CSV
        ),
        *compare.CompareFixedWidthFiles(combinedPath, CLEAN_COMBINED_DAT, TRANSACTION_LAYOUT_NAME),
        *compare.CompareTableWithRecords(
            connection, legacy_oracle.TRANSACTIONS_TABLE, combinedRecords, TRANSACTION_LAYOUT_NAME
        ),
    ]


def AssertCleanChainResult(engine: Engine, outputDir: Path) -> None:
    """Assert the outcome a clean chain over the sample data has, from the committed fixtures.

    Accounts equal intcalc_accounts.csv, category balances posttran_tcatbal.csv,
    TRANSACT.COMBINED(0) equals combtran_combined.dat byte for byte, and transactions holds
    exactly the COMBINED records: their count first, then every field.
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


# --- Ledger reads --------------------------------------------------------------------------------
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


# --- Table contents ------------------------------------------------------------------------------
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


# --- Commands ------------------------------------------------------------------------------------
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


# --- Failure and recovery scenarios --------------------------------------------------------------
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

    No balance, category balance, transaction, generation or statement moves.
    """
    snapshotBefore = ChainOutcomeSnapshot(environment.engine, environment.outputDir)
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


# --- Interruption scenario -----------------------------------------------------------------------
def StageSyntheticDailyFile(environment: BatchEnvironment) -> Path:
    """Write 20,000 synthetic daily records over the sample cards and stage them as DALYTRAN."""
    dailyPath = environment.outputDir.parent / SYNTHETIC_DAILY_FILE_NAME
    WriteSyntheticDailyImage(dailyPath, SYNTHETIC_RECORD_COUNT, SampleCardNumbers(environment))
    outcome = InvokeCli(["load", "daily-transactions", "--daily-file", str(dailyPath)])
    assert outcome.exitCode == EXIT_OK, outcome.output
    return dailyPath


def KillRunAllDuringPosttran(environment: BatchEnvironment) -> None:
    """SIGKILL a run-all subprocess once POSTTRAN has posted, and wait for its window to close.

    PostgreSQL rolls back the killed process's open transaction and releases its batch window
    when its connections drop; the writes POSTTRAN already committed stay.
    """
    engine = environment.engine
    process = StartCli(["run-all", "--as-of", environment.asOfText])
    try:
        WaitUntil(lambda: HasPostedTransaction(engine), POSTING_WAIT_SECONDS, "a posting")
    finally:
        KillProcess(process)
    WaitUntil(lambda: not IsBatchWindowHeld(engine), WINDOW_RELEASE_SECONDS, "the window release")


def ReadOracleState(engine: Engine) -> legacy_oracle.OracleState:
    """Read every dataset as the reconciliation oracle holds it."""
    with engine.connect() as connection:
        return compare.ReadTableAsOracleState(connection)


def AssertKilledPosttranInterrupted(environment: BatchEnvironment) -> None:
    """Assert the killed POSTTRAN run, step 13's only run, was reclassified INTERRUPTED."""
    stepRow = StepRow(environment.engine, POSTTRAN_STEP)
    assert RunStatus(environment.engine, stepRow["last_run_id"]) == run_ledger.RUN_INTERRUPTED
    assert CountJobRuns(environment.engine, POSTTRAN_STEP) == 1


def AssertPostingStateMatchesOracle(engine: Engine, expected: legacy_oracle.OracleState) -> None:
    """Assert the accounts, category balances and transactions equal the oracle's state."""
    actual = ReadOracleState(engine)
    assert actual.accounts == expected.accounts
    assert actual.categoryBalances == expected.categoryBalances
    assert actual.transactions == expected.transactions


# --- Order and shared definitions ----------------------------------------------------------------
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


# --- Resume from the seed, exit policy and --start-at --------------------------------------------
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
    [(SECOND_TRANBKP_STEP, EXIT_FAILURE), (POSTTRAN_STEP + 1, EXIT_ABEND)],
    ids=["step-15-rc12", "step-14-rc16"],
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


# --- Failure at every step, reruns and force -----------------------------------------------------
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


# --- SIGKILL of a per-write step -----------------------------------------------------------------
@pytest.mark.slow
def test_sigkilled_posttran_is_interrupted_refused_then_forced_from_partial_state(
    batchEnvironment: BatchEnvironment,
) -> None:
    engine = batchEnvironment.engine
    dailyPath = StageSyntheticDailyFile(batchEnvironment)
    assert InvokeCli(RERUN_COMMANDS[FIRST_TRANBKP_STEP]).exitCode == EXIT_OK
    KillRunAllDuringPosttran(batchEnvironment)
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
