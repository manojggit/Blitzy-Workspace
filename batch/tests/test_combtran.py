"""COMBTRAN: TRANSACT.BKUP(0) and SYSTRAN(0) merged into TRANSACT.COMBINED(+1) and TRANSACT.

Pins jcl/COMBTRAN.jcl as batch/jobs/combtran.py ports it (copybook CVTRA05Y; Technical
Specification, Sections 0.6.8 and 0.9.1):

- STEP05R sorts the SORTIN concatenation, BKUP(0) then SYSTRAN(0), with SORT FIELDS=(TRAN-ID,A)
  and SYMNAMES TRAN-ID,1,16,CH, so in EBCDIC byte order, into TRANSACT.COMBINED(+1). The
  generation equals the oracle fixture combtran_combined.dat byte for byte.
- STEP10 REPROs the generation into TRANSACT, which then holds exactly its records.
- The step is atomic and not repeatable: exit 0, or 12 on a duplicate key with full rollback (no
  row, no catalog entry, no archive file), after which the next command retries it.
- Only PUBLISHED generations are resolved. A COMMITTED SYSTRAN generation whose file is gone
  becomes LOST at the next command and blocks the cycle with exit 12 (catlg/LISTCAT.txt LIMIT 5
  SCRATCH bases, as the GDG archives of Section 0.6.8 replace them).

Each test first runs chain steps 6, 13, 14 and 15 through the CLI on the seeded test database
with the reconciliation clock frozen, so COMBTRAN reads the generations those steps published.
"""

import hashlib
import itertools
from decimal import Decimal
from pathlib import Path

from app.models import GDG_GENERATIONS, Transaction
from sqlalchemy import delete, func, insert, select
from sqlalchemy.engine import Connection, Row

from batch.orchestration import gdg
from batch.tests.conftest import (
    EXPECTED_DIR,
    BatchEnvironment,
    CliOutcome,
    InvokeCli,
    StepRow,
    SyntheticTransactionRow,
)
from batch.tests.reconciliation import compare, legacy_oracle

COMBINED_BASE_NAME = "TRANSACT.COMBINED"
BKUP_BASE_NAME = "TRANSACT.BKUP"
SYSTRAN_BASE_NAME = "SYSTRAN"
TRANSACTION_LAYOUT_NAME = "TRANSACTION"
TRANSACTIONS_TABLE_NAME = legacy_oracle.TRANSACTIONS_TABLE
CARDS_TABLE_NAME = legacy_oracle.CARDS_TABLE
TRANSACTION_TABLE = Transaction.__table__
COMBINED_FIXTURE = EXPECTED_DIR / "combtran_combined.dat"
RECORD_TERMINATOR = b"\n"

COMB_TRAN_ARGUMENTS = ("job", "comb-tran")
INTCALC_STEP_NO = 14
COMBTRAN_STEP_NO = 16
STEP_PENDING_STATE = "PENDING"
STEP_COMPLETED_STATE = "COMPLETED"
STEP_FAILED_STATE = "FAILED"

# PARM-DATE 2022071800 of jcl/INTCALC.jcl followed by WS-TRANID-SUFFIX 000001: the first SYSTRAN
# record, which a transaction already in TRANSACT collides with.
FIRST_INTEREST_TRAN_ID = "2022071800000001"
DUPLICATE_TRAN_AMOUNT = Decimal("1.00")
DUPLICATE_KEY_TEXT = "DUPLICATE KEY"
# The catalog checksum of a generation that was never written: the SHA-256 of no bytes.
UNWRITTEN_FILE_SHA256 = hashlib.sha256(b"").hexdigest()
NO_GENERATION_MESSAGE = "no PUBLISHED {baseName} generation was resolved"


def SetUpCommands(asOfText: str) -> tuple[tuple[tuple[str, ...], int], ...]:
    """Return chain steps 6, 13, 14 and 15 as CLI arguments, each with the exit status it gives.

    POSTTRAN exits 4 because the sample daily file holds rejects (the legacy RC 4).
    """
    return (
        (("job", "tran-backup", "--occurrence", "1"), legacy_oracle.EXIT_OK),
        (("job", "post-tran", "--as-of", asOfText), legacy_oracle.EXIT_REJECTS),
        (("job", "interest-calc", "--as-of", asOfText), legacy_oracle.EXIT_OK),
        (("job", "tran-backup", "--occurrence", "2"), legacy_oracle.EXIT_OK),
    )


def RunChainThroughSecondBackup(batchEnvironment: BatchEnvironment) -> None:
    """Run chain steps 6, 13, 14 and 15 in order through the CLI, asserting each exit status.

    COMBTRAN's inputs are then the PUBLISHED TRANSACT.BKUP(0) of step 15 and SYSTRAN(0) of step
    14, and transactions.transactions is empty.
    """
    for arguments, expectedExitCode in SetUpCommands(batchEnvironment.asOfText):
        outcome = InvokeCli(arguments)
        assert outcome.exitCode == expectedExitCode, outcome.output


def RunCombtranJob() -> CliOutcome:
    """Run `job comb-tran`, chain step 16 of the OPEN cycle."""
    return InvokeCli(COMB_TRAN_ARGUMENTS)


def NewestGenerationPath(batchEnvironment: BatchEnvironment, baseName: str) -> Path:
    """Resolve baseName(0) through the plain-SQL catalog read, which counts PUBLISHED rows only."""
    with batchEnvironment.engine.connect() as connection:
        generationPath = compare.LatestPublishedGeneration(
            connection, baseName, batchEnvironment.outputDir
        )
    assert generationPath is not None, NO_GENERATION_MESSAGE.format(baseName=baseName)
    return generationPath


def ReadNewestGenerationRecords(batchEnvironment: BatchEnvironment, baseName: str) -> list[str]:
    """Return the records of baseName(0), terminators removed."""
    return compare.ReadFixedWidthRecords(NewestGenerationPath(batchEnvironment, baseName))


def CountTransactions(connection: Connection) -> int:
    """Return the number of rows in transactions.transactions."""
    statement = select(func.count()).select_from(TRANSACTION_TABLE)
    return connection.execute(statement).scalar_one()


def ReadTranIds(batchEnvironment: BatchEnvironment) -> list[str]:
    """Return every TRAN-ID of transactions.transactions, in ascending order."""
    tranIdColumn = TRANSACTION_TABLE.c.tran_id
    with batchEnvironment.engine.connect() as connection:
        return list(connection.execute(select(tranIdColumn).order_by(tranIdColumn)).scalars())


def CountCatalogRows(batchEnvironment: BatchEnvironment, baseName: str) -> int:
    """Return the number of batch.gdg_generations rows of a base, in any state."""
    statement = (
        select(func.count())
        .select_from(GDG_GENERATIONS)
        .where(GDG_GENERATIONS.c.base_name == baseName)
    )
    with batchEnvironment.engine.connect() as connection:
        return connection.execute(statement).scalar_one()


def NewestCatalogRow(batchEnvironment: BatchEnvironment, baseName: str) -> Row:
    """Return the base's catalog row with the highest generation number, whatever its state."""
    statement = (
        select(GDG_GENERATIONS)
        .where(GDG_GENERATIONS.c.base_name == baseName)
        .order_by(GDG_GENERATIONS.c.generation.desc())
        .limit(1)
    )
    with batchEnvironment.engine.connect() as connection:
        return connection.execute(statement).one()


def ListArchiveFiles(batchEnvironment: BatchEnvironment, baseName: str) -> list[str]:
    """Return the names of every file, .part or .dat, in the base's archive directory."""
    baseDirectory = batchEnvironment.outputDir / compare.GDG_DIRECTORY_NAME / baseName
    return sorted(path.name for path in baseDirectory.glob("*"))


def InsertTransaction(batchEnvironment: BatchEnvironment, tranId: str) -> None:
    """Insert one posted transaction with tranId on the lowest seed card, merchant 000000001."""
    seedCards = batchEnvironment.snapshot.tableRows[CARDS_TABLE_NAME]
    cardNum = min(cardRow["card_num"] for cardRow in seedCards)
    transactionRow = SyntheticTransactionRow(1, cardNum, DUPLICATE_TRAN_AMOUNT)
    with batchEnvironment.engine.begin() as connection:
        connection.execute(insert(TRANSACTION_TABLE), {**transactionRow, "tran_id": tranId})


def DeleteTransaction(batchEnvironment: BatchEnvironment, tranId: str) -> None:
    """Delete the transaction with tranId."""
    with batchEnvironment.engine.begin() as connection:
        connection.execute(delete(TRANSACTION_TABLE).where(TRANSACTION_TABLE.c.tran_id == tranId))


def FailCombtranOnDuplicateKey(batchEnvironment: BatchEnvironment) -> tuple[str, CliOutcome]:
    """Set up, put the first SYSTRAN(0) TRAN-ID in the emptied TRANSACT, then run COMBTRAN.

    Returns the colliding TRAN-ID and the outcome of `job comb-tran`.
    """
    RunChainThroughSecondBackup(batchEnvironment)
    systranRecords = ReadNewestGenerationRecords(batchEnvironment, SYSTRAN_BASE_NAME)
    duplicateTranId = systranRecords[0][legacy_oracle.TRAN_ID_SLICE]
    InsertTransaction(batchEnvironment, duplicateTranId)
    return duplicateTranId, RunCombtranJob()


def InsertUnwrittenGeneration(batchEnvironment: BatchEnvironment, newestRow: Row) -> int:
    """Catalog a COMMITTED generation above newestRow's whose file was never written.

    The row carries the run_id of INTCALC's run in the OPEN cycle, so once the next command's
    ReconcileCatalog finds no file and marks it LOST, it blocks that cycle. Returns its number.
    """
    generation = newestRow.generation + 1
    catalogRow = {
        "base_name": newestRow.base_name,
        "generation": generation,
        "file_path": gdg.RelativeFilePath(gdg.GDG_BASES[newestRow.base_name], generation),
        "record_count": 0,
        "sha256": UNWRITTEN_FILE_SHA256,
        "state": gdg.GDG_COMMITTED,
        "run_id": StepRow(batchEnvironment.engine, INTCALC_STEP_NO)["last_run_id"],
    }
    with batchEnvironment.engine.begin() as connection:
        connection.execute(insert(GDG_GENERATIONS), catalogRow)
    return generation


def LostSystranBlockMessage(batchEnvironment: BatchEnvironment, generation: int) -> str:
    """Return the refusal that names a LOST SYSTRAN generation of the OPEN cycle.

    The reconciliation log also names the generation, so the refusal's full text is matched.
    """
    cycleId = StepRow(batchEnvironment.engine, COMBTRAN_STEP_NO)["cycle_id"]
    return gdg.LOST_GENERATION_TEMPLATE.format(
        base=SYSTRAN_BASE_NAME, generation=generation, cycleId=cycleId
    )


def test_combined_generation_equals_expected_fixture(batchEnvironment: BatchEnvironment) -> None:
    """STEP05R: COMBINED(+1) equals combtran_combined.dat byte for byte, terminators removed."""
    RunChainThroughSecondBackup(batchEnvironment)
    outcome = RunCombtranJob()
    assert outcome.exitCode == legacy_oracle.EXIT_OK, outcome.output
    combinedPath = NewestGenerationPath(batchEnvironment, COMBINED_BASE_NAME)
    differences = compare.CompareFixedWidthFiles(
        combinedPath, COMBINED_FIXTURE, TRANSACTION_LAYOUT_NAME
    )
    assert differences == []
    assert StepRow(batchEnvironment.engine, COMBTRAN_STEP_NO)["state"] == STEP_COMPLETED_STATE


def test_transactions_table_holds_exactly_the_combined_records(
    batchEnvironment: BatchEnvironment,
) -> None:
    """STEP10: TRANSACT holds the COMBINED(+1) records field for field, one row per line."""
    RunChainThroughSecondBackup(batchEnvironment)
    outcome = RunCombtranJob()
    assert outcome.exitCode == legacy_oracle.EXIT_OK, outcome.output
    combinedPath = NewestGenerationPath(batchEnvironment, COMBINED_BASE_NAME)
    combinedRecords = compare.ReadFixedWidthRecords(combinedPath)
    with batchEnvironment.engine.connect() as connection:
        differences = compare.CompareTableWithRecords(
            connection, TRANSACTIONS_TABLE_NAME, combinedRecords, TRANSACTION_LAYOUT_NAME
        )
        rowCount = CountTransactions(connection)
    assert differences == []
    lineCount = combinedPath.read_bytes().count(RECORD_TERMINATOR)
    assert rowCount == lineCount == len(combinedRecords)


def test_combined_records_are_bkup_and_systran_merged_in_ebcdic_tran_id_order(
    batchEnvironment: BatchEnvironment,
) -> None:
    """STEP05R: COMBINED(+1) is BKUP(0) then SYSTRAN(0), stably sorted by TRAN-ID as cp037 bytes."""
    RunChainThroughSecondBackup(batchEnvironment)
    bkupRecords = ReadNewestGenerationRecords(batchEnvironment, BKUP_BASE_NAME)
    systranRecords = ReadNewestGenerationRecords(batchEnvironment, SYSTRAN_BASE_NAME)
    assert bkupRecords and systranRecords
    outcome = RunCombtranJob()
    assert outcome.exitCode == legacy_oracle.EXIT_OK, outcome.output
    combinedRecords = ReadNewestGenerationRecords(batchEnvironment, COMBINED_BASE_NAME)
    assert combinedRecords == legacy_oracle.RunCombtran(bkupRecords, systranRecords)
    sortKeys = [
        legacy_oracle.EbcdicSortKey(record[legacy_oracle.TRAN_ID_SLICE])
        for record in combinedRecords
    ]
    assert all(earlier < later for earlier, later in itertools.pairwise(sortKeys))


def test_duplicate_key_exits_12_with_full_rollback(batchEnvironment: BatchEnvironment) -> None:
    """A TRAN-ID TRANSACT already holds stops STEP10 with exit 12 and rolls everything back.

    The table keeps only the colliding row, no TRANSACT.COMBINED generation is cataloged, its
    archive directory holds no .part or .dat file, and step 16 is FAILED.
    """
    duplicateTranId, outcome = FailCombtranOnDuplicateKey(batchEnvironment)
    assert duplicateTranId == FIRST_INTEREST_TRAN_ID
    assert outcome.exitCode == legacy_oracle.EXIT_UTILITY_FAILURE, outcome.output
    assert DUPLICATE_KEY_TEXT in outcome.output
    assert ReadTranIds(batchEnvironment) == [duplicateTranId]
    assert CountCatalogRows(batchEnvironment, COMBINED_BASE_NAME) == 0
    assert ListArchiveFiles(batchEnvironment, COMBINED_BASE_NAME) == []
    assert StepRow(batchEnvironment.engine, COMBTRAN_STEP_NO)["state"] == STEP_FAILED_STATE


def test_failed_combtran_is_retried_by_next_command_to_clean_result(
    batchEnvironment: BatchEnvironment,
) -> None:
    """A FAILED atomic COMBTRAN rolled back completely, so the next `job comb-tran` retries it.

    Once the colliding row is removed, the retry gives the clean result: the fixture generation
    and a table holding exactly its records.
    """
    duplicateTranId, failedOutcome = FailCombtranOnDuplicateKey(batchEnvironment)
    assert failedOutcome.exitCode == legacy_oracle.EXIT_UTILITY_FAILURE, failedOutcome.output
    DeleteTransaction(batchEnvironment, duplicateTranId)
    outcome = RunCombtranJob()
    assert outcome.exitCode == legacy_oracle.EXIT_OK, outcome.output
    combinedPath = NewestGenerationPath(batchEnvironment, COMBINED_BASE_NAME)
    fileDifferences = compare.CompareFixedWidthFiles(
        combinedPath, COMBINED_FIXTURE, TRANSACTION_LAYOUT_NAME
    )
    assert fileDifferences == []
    fixtureRecords = compare.ReadFixedWidthRecords(COMBINED_FIXTURE)
    with batchEnvironment.engine.connect() as connection:
        differences = compare.CompareTableWithRecords(
            connection, TRANSACTIONS_TABLE_NAME, fixtureRecords, TRANSACTION_LAYOUT_NAME
        )
    assert differences == []
    assert StepRow(batchEnvironment.engine, COMBTRAN_STEP_NO)["state"] == STEP_COMPLETED_STATE


def test_combtran_resolves_only_published_generations(batchEnvironment: BatchEnvironment) -> None:
    """SYSTRAN(0) is the newest PUBLISHED generation; an unwritten COMMITTED one blocks the cycle.

    Before the extra row, the resolved file is the newest generation and matches its catalog
    SHA-256. The extra COMMITTED row is never resolved; the next command marks it LOST, and
    COMBTRAN exits 12 naming it, without executing.
    """
    RunChainThroughSecondBackup(batchEnvironment)
    newestRow = NewestCatalogRow(batchEnvironment, SYSTRAN_BASE_NAME)
    systranPath = NewestGenerationPath(batchEnvironment, SYSTRAN_BASE_NAME)
    assert newestRow.state == gdg.GDG_PUBLISHED
    assert hashlib.sha256(systranPath.read_bytes()).hexdigest() == newestRow.sha256
    unwrittenGeneration = InsertUnwrittenGeneration(batchEnvironment, newestRow)
    assert NewestGenerationPath(batchEnvironment, SYSTRAN_BASE_NAME) == systranPath
    outcome = RunCombtranJob()
    assert outcome.exitCode == legacy_oracle.EXIT_UTILITY_FAILURE, outcome.output
    assert LostSystranBlockMessage(batchEnvironment, unwrittenGeneration) in outcome.output
    assert NewestCatalogRow(batchEnvironment, SYSTRAN_BASE_NAME).state == gdg.GDG_LOST
    assert CountCatalogRows(batchEnvironment, COMBINED_BASE_NAME) == 0
    assert StepRow(batchEnvironment.engine, COMBTRAN_STEP_NO)["state"] == STEP_PENDING_STATE
