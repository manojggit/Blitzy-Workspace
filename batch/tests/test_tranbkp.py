"""TRANBKP: archive transactions.transactions to TRANSACT.BKUP(+1), then leave the table empty.

Pins batch/jobs/tranbkp.py, the port of TRANBKP (jcl/TRANBKP.jcl). Its STEP05R runs REPROC
(proc/REPROC.prc) with REPROCT (ctl/REPROCT.ctl), REPRO INFILE(FILEIN) OUTFILE(FILEOUT), which
unloads the TRANSACT KSDS in KEYS(16 0) order into a new LRECL 350 generation of CVTRA05Y
records; STEP05 and STEP10 then delete and define the cluster empty. Technical Specification
Section 0.6.8 makes the port one atomic transaction, ordered by the EBCDIC TRAN-ID sort key and
not repeatable within a cycle, and Section 0.9.1 sets its parity bar: chain step 6 after the seed
writes an empty generation, and step 15 after POSTTRAN and INTCALC equals
fixtures/expected/tranbkp_run2_bkup.dat byte for byte. The generation is cataloged as the
TRANSACT.BKUP base of catlg/LISTCAT.txt.

Every command runs in process through InvokeCli, so the monkeypatched hook and the injected
rollback reach the job.
"""

import contextlib
import hashlib
import tracemalloc
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from app.models import GDG_GENERATIONS, Transaction
from sqlalchemy import Engine, event, func, insert, select
from sqlalchemy.engine import Row

from batch.cli import INTEREST_CALC_JOB, POST_TRAN_JOB
from batch.jobs import tranbkp
from batch.orchestration.run_ledger import (
    GDG_COMMITTED,
    GDG_PUBLISHED,
    RUN_SKIPPED,
    STEP_COMPLETED,
    STEP_FAILED,
)
from batch.tests.conftest import (
    EXPECTED_DIR,
    BatchEnvironment,
    CountJobRuns,
    InjectAtomicRollbackFailure,
    InsertSyntheticCards,
    InsertSyntheticTransactions,
    InvokeCli,
    StepRow,
    SyntheticTransactionRow,
    SyntheticTransactionSpec,
)
from batch.tests.reconciliation.compare import (
    GDG_DIRECTORY_NAME,
    CompareFixedWidthFiles,
    LatestPublishedGeneration,
    ReadFixedWidthRecords,
)

# AWS.M2.CARDDEMO.TRANSACT.BKUP, the generation data group of the STEP05R FILEOUT DD.
BKUP_BASE_NAME = "TRANSACT.BKUP"
# DCB=(LRECL=350,RECFM=FB) of FILEOUT, and the TRAN-ID X(16) prime key at offset 0 of CVTRA05Y.
TRANSACTION_LRECL = 350
TRAN_ID_LENGTH = 16
TRANSACTION_LAYOUT_NAME = "TRANSACTION"
EXPECTED_RUN2_BACKUP = EXPECTED_DIR / "tranbkp_run2_bkup.dat"

# The two TRANBKP occurrences of the chain: --occurrence 1 is step 6, --occurrence 2 is step 15.
FIRST_OCCURRENCE = 1
SECOND_OCCURRENCE = 2
FIRST_OCCURRENCE_STEP = 6
SECOND_OCCURRENCE_STEP = 15

# carddemo-batch exit statuses: success, POSTTRAN's legacy RC 4 for rejects, and a failure.
EXIT_OK = 0
EXIT_RECORDS_REJECTED = 4
EXIT_FAILURE = 12

# The catalog digest of a generation that holds no record.
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
# Catalog states under which a generation is archived: cataloged, or cataloged and renamed.
ARCHIVED_STATES = (GDG_COMMITTED, GDG_PUBLISHED)
# The final G####V00.dat file and its G####V00.dat.part predecessor.
GENERATION_FILE_SUFFIXES = (".dat", ".part")

# Code page 037 orders lower case before upper case before digits; ASCII and the C collation of
# the tran_id column order them the other way round.
EBCDIC_ORDERED_TRAN_IDS = ("a000000000000001", "A000000000000001", "0000000000000001")
SYNTHETIC_AMOUNT = Decimal("12.34")
SYNTHETIC_ROW_COUNT = 2

NO_PUBLISHED_BACKUP_MESSAGE = "No PUBLISHED TRANSACT.BKUP generation was cataloged"
RECORD_LENGTH_MESSAGE = "A TRANSACT.BKUP record is not {lrecl} characters: {lengths!r}"

# The bounded-read probe: a small fetch batch, a table 20 times the size of the first one, and
# the most the traced peak may grow between them, far below the roughly 1 KB per row that a
# result held whole would add (3.8 MB for the 3,800 extra rows).
SMALL_FETCH_BATCH_SIZE = 50
SMALL_TABLE_ROW_COUNT = 200
LARGE_TABLE_ROW_COUNT = 4000
ARCHIVE_PEAK_GROWTH_LIMIT = 256 * 1024


@dataclass(frozen=True)
class ArchiveReadMeasure:
    """One traced pass of the archive read: its peak, the records it wrote, its cursors' names."""

    peakBytes: int
    recordCount: int
    cursorNames: tuple[str | None, ...]


class CountingWriter:
    """Stands in for gdg.GenerationWriter: counts the records written and keeps none of them."""

    def __init__(self) -> None:
        self.recordCount = 0

    def WriteRecord(self, record: str) -> None:
        """Count one record; its text is not kept."""
        self.recordCount += 1


def TranBackupArguments(occurrence: int) -> list[str]:
    """Return the carddemo-batch arguments of one unforced TRANBKP occurrence."""
    return ["job", tranbkp.JOB_NAME, "--occurrence", str(occurrence)]


def InvokeExpecting(arguments: Sequence[str], expectedExitCode: int) -> None:
    """Run a carddemo-batch command in process and assert its exit status, showing its output."""
    outcome = InvokeCli(arguments)
    assert outcome.exitCode == expectedExitCode, outcome.output


def InsertSyntheticRows(engine: Engine, rowCount: int) -> None:
    """Insert rowCount posted transactions on one new synthetic card."""
    cardNums = InsertSyntheticCards(engine, 1)
    spec = SyntheticTransactionSpec(cardNums=cardNums, perCard=rowCount, amount=SYNTHETIC_AMOUNT)
    InsertSyntheticTransactions(engine, spec)


def InsertTransactions(engine: Engine, tranIds: Sequence[str]) -> None:
    """Insert one posted transaction per TRAN-ID, in the given order, on a new synthetic card."""
    (cardNum,) = InsertSyntheticCards(engine, 1)
    rows = [
        {**SyntheticTransactionRow(tranNumber, cardNum, SYNTHETIC_AMOUNT), "tran_id": tranId}
        for tranNumber, tranId in enumerate(tranIds, start=1)
    ]
    with engine.begin() as connection:
        connection.execute(insert(Transaction.__table__), rows)


def CountTransactions(engine: Engine) -> int:
    """Return the committed row count of transactions.transactions, on a connection of its own."""
    statement = select(func.count()).select_from(Transaction.__table__)
    with engine.connect() as connection:
        return connection.execute(statement).scalar_one()


def BackupGenerationRows(engine: Engine) -> list[Row]:
    """Return every TRANSACT.BKUP row of batch.gdg_generations, oldest generation first."""
    statement = (
        select(GDG_GENERATIONS)
        .where(GDG_GENERATIONS.c.base_name == BKUP_BASE_NAME)
        .order_by(GDG_GENERATIONS.c.generation)
    )
    with engine.connect() as connection:
        return list(connection.execute(statement).all())


def LatestBackupPath(environment: BatchEnvironment) -> Path:
    """Return the TRANSACT.BKUP(0) file; fail the test when no generation is PUBLISHED."""
    with environment.engine.connect() as connection:
        backupPath = LatestPublishedGeneration(connection, BKUP_BASE_NAME, environment.outputDir)
    assert backupPath is not None, NO_PUBLISHED_BACKUP_MESSAGE
    return backupPath


def BackupKeys(environment: BatchEnvironment) -> list[str]:
    """Return the TRAN-ID, the first 16 bytes, of every TRANSACT.BKUP(0) record in file order."""
    records = ReadFixedWidthRecords(LatestBackupPath(environment))
    lengths = {len(record) for record in records}
    assert lengths <= {TRANSACTION_LRECL}, RECORD_LENGTH_MESSAGE.format(
        lrecl=TRANSACTION_LRECL, lengths=sorted(lengths)
    )
    return [record[:TRAN_ID_LENGTH] for record in records]


def GenerationFileNames(outputDir: Path) -> list[str]:
    """Return the names of the generation and .part files under the TRANSACT.BKUP directory."""
    baseDirectory = outputDir / GDG_DIRECTORY_NAME / BKUP_BASE_NAME
    return sorted(
        path.name for path in baseDirectory.glob("*") if path.suffix in GENERATION_FILE_SUFFIXES
    )


def InsertRowsOnCards(engine: Engine, cardNums: Sequence[str], rowCount: int) -> None:
    """Insert rowCount more posted transactions on each of the given existing cards."""
    spec = SyntheticTransactionSpec(cardNums=cardNums, perCard=rowCount, amount=SYNTHETIC_AMOUNT)
    InsertSyntheticTransactions(engine, spec)


@contextlib.contextmanager
def CapturedCursorNames(engine: Engine) -> Iterator[list[str | None]]:
    """Record the DBAPI cursor name of every statement the engine runs; None means client-side."""
    cursorNames: list[str | None] = []

    def RecordCursorName(**eventArguments: object) -> None:
        cursorNames.append(getattr(eventArguments["cursor"], "name", None))

    event.listen(engine, "before_cursor_execute", RecordCursorName, named=True)
    try:
        yield cursorNames
    finally:
        event.remove(engine, "before_cursor_execute", RecordCursorName)


def MeasureArchiveRead(engine: Engine) -> ArchiveReadMeasure:
    """Stream the table through the archive read into a CountingWriter under tracemalloc.

    An untraced first pass warms the statement cache, so passes over tables of different sizes
    trace the same code; the transaction is rolled back, so nothing changes.
    """
    writer = CountingWriter()
    with engine.connect() as connection, CapturedCursorNames(engine) as cursorNames:
        tranbkp.WriteArchiveRecords(connection, CountingWriter())
        tracemalloc.start()
        try:
            tranbkp.WriteArchiveRecords(connection, writer)
            peakBytes = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    return ArchiveReadMeasure(peakBytes, writer.recordCount, tuple(cursorNames))


def BuildCountingHook(engine: Engine, observedCounts: list[int]) -> Callable[[], None]:
    """Return an archive-read hook that records the row count a separate connection sees."""

    def RecordHookCall() -> None:
        observedCounts.append(CountTransactions(engine))

    return RecordHookCall


def test_first_backup_after_seed_publishes_empty_generation(
    batchEnvironment: BatchEnvironment,
) -> None:
    """Step 6 after the seed archives the empty table to an empty, PUBLISHED generation."""
    InvokeExpecting(TranBackupArguments(FIRST_OCCURRENCE), EXIT_OK)
    backupPath = LatestBackupPath(batchEnvironment)
    generationRows = BackupGenerationRows(batchEnvironment.engine)
    (catalogRow,) = [row for row in generationRows if row.state == GDG_PUBLISHED]
    assert backupPath.stat().st_size == 0
    assert catalogRow.record_count == 0
    assert catalogRow.sha256 == EMPTY_SHA256
    assert StepRow(batchEnvironment.engine, FIRST_OCCURRENCE_STEP)["state"] == STEP_COMPLETED


def test_second_backup_equals_expected_fixture_and_empties_table(
    batchEnvironment: BatchEnvironment,
) -> None:
    """Step 15 after POSTTRAN and INTCALC archives exactly the oracle's posted records."""
    asOfArguments = ["--as-of", batchEnvironment.asOfText]
    InvokeExpecting(TranBackupArguments(FIRST_OCCURRENCE), EXIT_OK)
    InvokeExpecting(["job", POST_TRAN_JOB, *asOfArguments], EXIT_RECORDS_REJECTED)
    InvokeExpecting(["job", INTEREST_CALC_JOB, *asOfArguments], EXIT_OK)
    InvokeExpecting(TranBackupArguments(SECOND_OCCURRENCE), EXIT_OK)
    differences = CompareFixedWidthFiles(
        LatestBackupPath(batchEnvironment), EXPECTED_RUN2_BACKUP, TRANSACTION_LAYOUT_NAME
    )
    assert differences == []
    assert CountTransactions(batchEnvironment.engine) == 0
    assert StepRow(batchEnvironment.engine, SECOND_OCCURRENCE_STEP)["state"] == STEP_COMPLETED


def test_backup_records_are_in_ebcdic_tran_id_order(batchEnvironment: BatchEnvironment) -> None:
    """Records follow the code page 037 order of TRAN-ID, the order REPRO unloads KEYS(16 0)."""
    asciiOrderedTranIds = sorted(EBCDIC_ORDERED_TRAN_IDS)
    assert asciiOrderedTranIds != list(EBCDIC_ORDERED_TRAN_IDS)
    InsertTransactions(batchEnvironment.engine, asciiOrderedTranIds)
    InvokeExpecting(TranBackupArguments(FIRST_OCCURRENCE), EXIT_OK)
    assert BackupKeys(batchEnvironment) == list(EBCDIC_ORDERED_TRAN_IDS)
    assert CountTransactions(batchEnvironment.engine) == 0


def test_injected_rollback_keeps_rows_and_catalogs_no_generation(
    batchEnvironment: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure of the step's final ledger write rolls back the archive, catalog and delete."""
    engine = batchEnvironment.engine
    InsertSyntheticRows(engine, SYNTHETIC_ROW_COUNT)
    InjectAtomicRollbackFailure(monkeypatch, FIRST_OCCURRENCE_STEP)
    InvokeExpecting(TranBackupArguments(FIRST_OCCURRENCE), EXIT_FAILURE)
    archivedRows = [row for row in BackupGenerationRows(engine) if row.state in ARCHIVED_STATES]
    assert CountTransactions(engine) == SYNTHETIC_ROW_COUNT
    assert archivedRows == []
    assert GenerationFileNames(batchEnvironment.outputDir) == []
    assert StepRow(engine, FIRST_OCCURRENCE_STEP)["state"] == STEP_FAILED


def test_after_archive_read_hook_fires_once_per_run(
    batchEnvironment: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook runs once per run, while the archived rows are still committed in the table."""
    engine = batchEnvironment.engine
    InsertSyntheticRows(engine, SYNTHETIC_ROW_COUNT)
    observedCounts: list[int] = []
    monkeypatch.setattr(
        tranbkp, "AFTER_ARCHIVE_READ_HOOKS", [BuildCountingHook(engine, observedCounts)]
    )
    InvokeExpecting(TranBackupArguments(FIRST_OCCURRENCE), EXIT_OK)
    assert observedCounts == [SYNTHETIC_ROW_COUNT]
    assert CountTransactions(engine) == 0


def test_archive_read_streams_from_a_server_side_cursor_in_bounded_memory(
    batchEnvironment: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archive read holds one fetch batch, never the table, however large the table grows.

    Every statement runs on a named, server-side cursor, so the driver buffers no whole result,
    and the traced peak for a table 20 times larger stays within ARCHIVE_PEAK_GROWTH_LIMIT.
    """
    engine = batchEnvironment.engine
    monkeypatch.setattr(tranbkp, "ARCHIVE_FETCH_BATCH_SIZE", SMALL_FETCH_BATCH_SIZE)
    cardNums = InsertSyntheticCards(engine, 1)
    InsertRowsOnCards(engine, cardNums, SMALL_TABLE_ROW_COUNT)
    smallMeasure = MeasureArchiveRead(engine)
    InsertRowsOnCards(engine, cardNums, LARGE_TABLE_ROW_COUNT - SMALL_TABLE_ROW_COUNT)
    largeMeasure = MeasureArchiveRead(engine)
    assert (smallMeasure.recordCount, largeMeasure.recordCount) == (
        SMALL_TABLE_ROW_COUNT,
        LARGE_TABLE_ROW_COUNT,
    )
    cursorNames = smallMeasure.cursorNames + largeMeasure.cursorNames
    assert cursorNames and None not in cursorNames, cursorNames
    peakGrowth = largeMeasure.peakBytes - smallMeasure.peakBytes
    assert peakGrowth < ARCHIVE_PEAK_GROWTH_LIMIT, (smallMeasure, largeMeasure)


def test_completed_backup_step_rerun_is_skipped_without_new_generation(
    batchEnvironment: BatchEnvironment,
) -> None:
    """An unforced rerun of a COMPLETED step 6 records SKIPPED and writes no generation."""
    engine = batchEnvironment.engine
    InvokeExpecting(TranBackupArguments(FIRST_OCCURRENCE), EXIT_OK)
    stepBefore = StepRow(engine, FIRST_OCCURRENCE_STEP)
    generationsBefore = BackupGenerationRows(engine)
    skippedBefore = CountJobRuns(engine, FIRST_OCCURRENCE_STEP, RUN_SKIPPED)
    InvokeExpecting(TranBackupArguments(FIRST_OCCURRENCE), EXIT_OK)
    assert CountJobRuns(engine, FIRST_OCCURRENCE_STEP, RUN_SKIPPED) == skippedBefore + 1
    assert BackupGenerationRows(engine) == generationsBefore
    assert StepRow(engine, FIRST_OCCURRENCE_STEP) == stepBefore
