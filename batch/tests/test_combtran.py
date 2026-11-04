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

- The sort streams: its result equals the oracle's stable sort even when an input is out of
  TRAN-ID order (a SYSTRAN whose suffix wrapped at 999999), its memory does not grow with the
  generations, and the load writes, decodes and inserts in one pass of bounded batches. Its
  spilled runs go to a work directory of the TRANSACT.COMBINED base, which the job removes and
  the next command's catalog recovery deletes after a killed run. A base directory that is a
  symbolic link is refused before any run is spilled through it.
- Publication order, pinned for every GDG producer since this module's setup runs all of them:
  the COMMITTED catalog row is the final write of the producer's transaction, an fsync failure
  as the generation closes rolls the run's completion back with everything else, and the
  atomic producers (TRANBKP, COMBTRAN) leave publication to batch_chain, so a rename error after
  the commit keeps the step COMPLETED and an unforced rerun skips it.
- A file error's failure text names its file, a str, bytes or descriptor, with every control
  character escaped, so a line feed in a file name cannot split the failure line.

Each CLI test first runs chain steps 6, 13, 14 and 15, or as many of them as its producer
needs, through the CLI on the seeded test database with the reconciliation clock frozen, so
COMBTRAN reads the generations those steps published.
"""

import concurrent.futures
import contextlib
import errno
import hashlib
import itertools
import multiprocessing
import tracemalloc
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest
from app.models import GDG_GENERATIONS, Transaction
from sqlalchemy import Engine, delete, event, func, insert, select
from sqlalchemy.engine import Connection, Row

from batch.jobs import combtran, tranbkp
from batch.orchestration import gdg
from batch.orchestration.job_context import JobContext, JobResult
from batch.orchestration.run_ledger import RUN_SKIPPED
from batch.tests.conftest import (
    EXPECTED_DIR,
    BatchEnvironment,
    CliOutcome,
    CountJobRuns,
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

# A transaction inserted before any step runs, so the TRANBKP case archives a record.
ARCHIVE_MARKER_TRAN_ID = "0000000000000001"
INJECTED_PUBLICATION_MESSAGE = "injected rename failure"
INJECTED_SYNC_MESSAGE = "injected fsync failure"
TRAN_BACKUP_FIRST_ARGUMENTS = ("job", "tran-backup", "--occurrence", "1")
TRAN_BACKUP_FIRST_STEP_NO = 6
# No setup command, or all four of SetUpCommands: chain steps 6, 13, 14 and 15.
NO_SET_UP_COMMANDS = 0
ALL_SET_UP_COMMANDS = 4


@dataclass(frozen=True)
class AtomicProducerCase:
    """An atomic GDG producer, run after the first setUpCount commands of SetUpCommands."""

    setUpCount: int
    arguments: tuple[str, ...]
    stepNo: int
    module: ModuleType
    baseName: str


@dataclass(frozen=True)
class ProducerState:
    """The step state, the newest catalog row's state and the counts a rerun must not change."""

    stepState: str
    generationState: str
    catalogRowCount: int
    transactionCount: int


@dataclass(frozen=True)
class ArchiveState:
    """What a rolled-back producer leaves unchanged: its base's catalog and files, and TRANSACT."""

    catalogRowCount: int
    archiveFileNames: tuple[str, ...]
    transactionCount: int


# Statement capture of one command: transaction boundaries are recorded as these markers, and
# the producer's final transaction must end with its catalog INSERT right before COMMIT.
BEGIN_MARKER = "BEGIN"
COMMIT_MARKER = "COMMIT"
CATALOG_INSERT_PREFIX = "INSERT INTO batch.gdg_generations"
RUN_COMPLETION_PREFIX = "UPDATE batch.job_runs"
# The GDG producers by their position in SetUpCommands followed by COMBTRAN: chain steps 6
# (TRANSACT.BKUP), 13 (DALYREJS), 14 (SYSTRAN) and 16 (TRANSACT.COMBINED).
FINAL_WRITE_CASES = (
    pytest.param(0, id="tran-backup"),
    pytest.param(1, id="post-tran"),
    pytest.param(2, id="interest-calc"),
    pytest.param(ALL_SET_UP_COMMANDS, id="comb-tran"),
)

ATOMIC_PRODUCER_CASES = (
    pytest.param(
        AtomicProducerCase(
            NO_SET_UP_COMMANDS,
            TRAN_BACKUP_FIRST_ARGUMENTS,
            TRAN_BACKUP_FIRST_STEP_NO,
            tranbkp,
            BKUP_BASE_NAME,
        ),
        id="tran-backup",
    ),
    pytest.param(
        AtomicProducerCase(
            ALL_SET_UP_COMMANDS, COMB_TRAN_ARGUMENTS, COMBTRAN_STEP_NO, combtran, COMBINED_BASE_NAME
        ),
        id="comb-tran",
    ),
)


# Synthetic SORTIN records: the TRAN-ID, then a tag naming the input and the record's position
# in it, padded with spaces to LRECL 350, so equal keys stay distinguishable.
BKUP_TAG = "BKUP"
SYSTRAN_TAG = "SYST"
SYNTHETIC_GENERATION_NUMBER = 1
SYNTHETIC_FILE_TEMPLATE = "{baseName}.dat"
SPILL_ROOT_NAME = "spill"
INPUT_DIRECTORY_TEMPLATE = "inputs-{recordCount}"
# The parity cases spill runs of two records, merged two at a time, so even five records take
# more than one merge pass.
TINY_RUN_RECORD_COUNT = 2
TINY_MERGE_FAN_IN = 2
# STEP05R's SORTIN concatenation: BKUP(0), then SYSTRAN(0).
SORTIN_INPUT_COUNT = 2
# The job case sorts the sample inputs in runs of 50, so its larger input spills more runs than
# one merge takes.
FORCED_SORT_RUN_RECORD_COUNT = 50
# The spill directory a killed COMBTRAN left in the TRANSACT.COMBINED base directory.
ORPHAN_SPILL_DIRECTORY_NAME = f"{combtran.SPILL_DIRECTORY_PREFIX}killed{gdg.WORK_DIRECTORY_SUFFIX}"
ORPHAN_RUN_FILE_NAME = f"killed{combtran.RUN_FILE_SUFFIX}"
# The memory cases: inputs ten times the size of the first ones, runs of 500 merged 8 at a time,
# and the most the traced peak may grow between them, far below the 14 MB or so that the 36,000
# extra records would add held whole. Each size is merged MEMORY_MEASUREMENT_COUNT times in a
# fresh interpreter, which the spawn start method gives.
SMALL_INPUT_RECORD_COUNT = 2_000
LARGE_INPUT_RECORD_COUNT = 20_000
MEMORY_INPUT_RECORD_COUNTS = (SMALL_INPUT_RECORD_COUNT, LARGE_INPUT_RECORD_COUNT)
MEMORY_RUN_RECORD_COUNT = 500
MEMORY_MERGE_FAN_IN = 8
MERGE_PEAK_GROWTH_LIMIT = 1024 * 1024
MEMORY_MEASUREMENT_COUNT = 3
SPAWN_START_METHOD = "spawn"
# Per input record count, the (traced peak in bytes, records merged) of each measurement.
type MergeMeasurements = dict[int, list[tuple[int, int]]]
# INTCALC's TRAN-ID: PARM-DATE 2022071800, then WS-TRANID-SUFFIX 9(06), which wraps to 000000.
INTEREST_TRAN_ID_PREFIX = "2022071800"
TRANID_SUFFIX_MODULUS = 10**6
# The one-pass load case: an insert batch of seven rows over the fixture's records.
SMALL_INSERT_BATCH_SIZE = 7
# The containment case: a SYSTRAN of four records wrapping at 999999, so its sort must spill, and
# a TRANSACT.COMBINED base directory under LINKED_OUTPUT_NAME that is a link to a directory
# outside the archive.
WRAPPED_RECORD_COUNT = 4
LINKED_OUTPUT_NAME = "linked-output"
OUTSIDE_DIRECTORY_NAME = "outside-sentinel"


@dataclass(frozen=True)
class MergeCase:
    """Two SORTIN inputs by TRAN-ID, and whether one is out of order, so the merge spills it."""

    bkupTranIds: tuple[str, ...]
    systranTranIds: tuple[str, ...]
    spills: bool


MERGE_CASES = (
    pytest.param(
        MergeCase(
            ("0000000000000005", "2022071800000001", "2022071800000003"),
            ("2022071800000001", "2022071800000002", "2022071800000003"),
            False,
        ),
        id="ordered-inputs-tied-across-inputs",
    ),
    pytest.param(
        MergeCase(
            ("2022071800000001", "2022071800999999"),
            (
                "2022071800999997",
                "2022071800999998",
                "2022071800999999",
                "2022071800000000",
                "2022071800000001",
            ),
            True,
        ),
        id="systran-suffix-wraps-at-999999",
    ),
    pytest.param(
        MergeCase(
            (
                "2022071800000002",
                "A000000000000001",
                "2022071800000002",
                "a000000000000001",
                "0000000000000009",
            ),
            ("2022071800000002",),
            True,
        ),
        id="backup-out-of-order-with-equal-keys",
    ),
)
MEMORY_CASES = (
    pytest.param(False, id="inputs-in-order"),
    pytest.param(True, id="systran-suffix-wrapped"),
)


@dataclass(frozen=True)
class MergeMemoryProbe:
    """What MeasureMergePeaks needs in a fresh interpreter, which no monkeypatch reaches.

    workDirectory receives both input sizes and the spill root; wrapped makes the SYSTRAN
    TRAN-IDs wrap at 999999 halfway; runRecordCount and mergeFanIn stand for combtran's
    SORT_RUN_RECORD_COUNT and MERGE_FAN_IN during the measurements.
    """

    workDirectory: Path
    wrapped: bool
    runRecordCount: int
    mergeFanIn: int


class RecordingWriter:
    """Stands in for gdg.GenerationWriter: keeps and counts every record written to it."""

    def __init__(self) -> None:
        self.records: list[str] = []
        self.recordCount = 0

    def WriteRecord(self, record: str) -> None:
        """Keep one record and count it, as GenerationWriter numbers the records it writes."""
        self.records.append(record)
        self.recordCount += 1


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


def RunSetUpCommands(batchEnvironment: BatchEnvironment, commandCount: int) -> None:
    """Run the first commandCount commands of SetUpCommands through the CLI, checking each."""
    for arguments, expectedExitCode in SetUpCommands(batchEnvironment.asOfText)[:commandCount]:
        outcome = InvokeCli(arguments)
        assert outcome.exitCode == expectedExitCode, outcome.output


def RunChainThroughSecondBackup(batchEnvironment: BatchEnvironment) -> None:
    """Run chain steps 6, 13, 14 and 15 in order through the CLI, asserting each exit status.

    COMBTRAN's inputs are then the PUBLISHED TRANSACT.BKUP(0) of step 15 and SYSTRAN(0) of step
    14, and transactions.transactions is empty.
    """
    RunSetUpCommands(batchEnvironment, ALL_SET_UP_COMMANDS)


def ProducerCommands(asOfText: str) -> tuple[tuple[tuple[str, ...], int], ...]:
    """Return SetUpCommands followed by COMBTRAN, each with the exit status it gives."""
    return (*SetUpCommands(asOfText), (COMB_TRAN_ARGUMENTS, legacy_oracle.EXIT_OK))


@contextlib.contextmanager
def CapturedStatements() -> Iterator[list[tuple[Connection, str]]]:
    """Record every engine's BEGIN, statement and COMMIT, in order, with its connection."""
    events: list[tuple[Connection, str]] = []

    def RecordBegin(connection: Connection) -> None:
        events.append((connection, BEGIN_MARKER))

    def RecordStatement(**eventArguments: object) -> None:
        events.append((eventArguments["conn"], eventArguments["statement"]))

    def RecordCommit(connection: Connection) -> None:
        events.append((connection, COMMIT_MARKER))

    event.listen(Engine, "begin", RecordBegin)
    event.listen(Engine, "before_cursor_execute", RecordStatement, named=True)
    event.listen(Engine, "commit", RecordCommit)
    try:
        yield events
    finally:
        event.remove(Engine, "begin", RecordBegin)
        event.remove(Engine, "before_cursor_execute", RecordStatement)
        event.remove(Engine, "commit", RecordCommit)


def CatalogInsertTransaction(events: list[tuple[Connection, str]]) -> list[str]:
    """Return, from BEGIN to COMMIT, the transaction holding the command's one catalog INSERT."""
    (insertIndex,) = [
        index
        for index, (connection, statement) in enumerate(events)
        if statement.startswith(CATALOG_INSERT_PREFIX)
    ]
    owner = events[insertIndex][0]
    ownerEvents = [
        (index, statement)
        for index, (connection, statement) in enumerate(events)
        if connection is owner
    ]
    beginIndex = max(
        index for index, text in ownerEvents if text == BEGIN_MARKER and index < insertIndex
    )
    commitIndex = min(
        index for index, text in ownerEvents if text == COMMIT_MARKER and index > insertIndex
    )
    return [text for index, text in ownerEvents if beginIndex <= index <= commitIndex]


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


def ReadProducerState(
    batchEnvironment: BatchEnvironment, case: AtomicProducerCase
) -> ProducerState:
    """Return the producer's step state, its base's newest row state and the guarded counts."""
    with batchEnvironment.engine.connect() as connection:
        transactionCount = CountTransactions(connection)
    return ProducerState(
        stepState=StepRow(batchEnvironment.engine, case.stepNo)["state"],
        generationState=NewestCatalogRow(batchEnvironment, case.baseName).state,
        catalogRowCount=CountCatalogRows(batchEnvironment, case.baseName),
        transactionCount=transactionCount,
    )


def ReadArchiveState(batchEnvironment: BatchEnvironment, baseName: str) -> ArchiveState:
    """Return baseName's catalog row count and archive file names, and the TRANSACT row count."""
    with batchEnvironment.engine.connect() as connection:
        transactionCount = CountTransactions(connection)
    return ArchiveState(
        catalogRowCount=CountCatalogRows(batchEnvironment, baseName),
        archiveFileNames=tuple(ListArchiveFiles(batchEnvironment, baseName)),
        transactionCount=transactionCount,
    )


def FailGenerationSync(monkeypatch: pytest.MonkeyPatch, baseName: str) -> None:
    """Make the fsync that closes a new baseName generation raise EIO, as a full disk can."""
    originalSync = gdg.SyncAndClose

    def SyncOrFail(writer: gdg.GenerationWriter) -> None:
        if writer.base.name == baseName:
            raise OSError(errno.EIO, INJECTED_SYNC_MESSAGE)
        originalSync(writer)

    monkeypatch.setattr(gdg, "SyncAndClose", SyncOrFail)


def FailPublication(monkeypatch: pytest.MonkeyPatch, baseName: str) -> None:
    """Make every rename of a baseName generation into place raise EIO, as os.replace can."""
    originalPlace = gdg.PlaceGenerationFile

    def PlaceOrFail(finalPath: Path, expectedSha256: str) -> str:
        if finalPath.parent.name == baseName:
            raise OSError(errno.EIO, INJECTED_PUBLICATION_MESSAGE, str(finalPath))
        return originalPlace(finalPath, expectedSha256)

    monkeypatch.setattr(gdg, "PlaceGenerationFile", PlaceOrFail)


def ObserveStateOnReturn(
    monkeypatch: pytest.MonkeyPatch,
    batchEnvironment: BatchEnvironment,
    case: AtomicProducerCase,
    observedStates: list[str],
) -> None:
    """Wrap the producer's RunJob to record its base's newest catalog row state as it returns."""
    originalRunJob = case.module.RunJob

    def ObservingRunJob(context: JobContext) -> JobResult:
        result = originalRunJob(context)
        observedStates.append(NewestCatalogRow(batchEnvironment, case.baseName).state)
        return result

    monkeypatch.setattr(case.module, "RunJob", ObservingRunJob)


def RunWithFailingPublication(
    batchEnvironment: BatchEnvironment, case: AtomicProducerCase
) -> tuple[list[str], CliOutcome]:
    """Run the producer while its generation cannot be renamed into place.

    Returns the catalog state its RunJob left on returning, empty when it raised, and the outcome.
    """
    observedStates: list[str] = []
    with pytest.MonkeyPatch.context() as failurePatch:
        ObserveStateOnReturn(failurePatch, batchEnvironment, case, observedStates)
        FailPublication(failurePatch, case.baseName)
        outcome = InvokeCli(case.arguments)
    return observedStates, outcome


def SyntheticRecords(tranIds: Iterable[str], inputTag: str) -> list[str]:
    """Return one LRECL 350 record per TRAN-ID, tagged with its input and 1-based position."""
    return [
        f"{tranId}{inputTag}{position:06d}".ljust(legacy_oracle.TRANSACTION_LRECL)
        for position, tranId in enumerate(tranIds, start=1)
    ]


def SyntheticGeneration(
    directory: Path, base: gdg.GdgBase, records: Sequence[str]
) -> gdg.Generation:
    """Write records as a generation file of base under directory; return it as resolved."""
    path = directory / SYNTHETIC_FILE_TEMPLATE.format(baseName=base.name)
    with path.open("wb") as generationFile:
        for record in records:
            generationFile.write(record.encode(gdg.GDG_FILE_ENCODING) + gdg.RECORD_TERMINATOR)
    return gdg.Generation(
        baseName=base.name,
        generation=SYNTHETIC_GENERATION_NUMBER,
        path=path,
        recordCount=len(records),
        sha256=gdg.FileSha256(path),
        state=gdg.GDG_PUBLISHED,
    )


def UseExternalSort(monkeypatch: pytest.MonkeyPatch, runRecordCount: int, mergeFanIn: int) -> None:
    """Make COMBTRAN's external sort spill runs of runRecordCount, merged mergeFanIn at a time."""
    monkeypatch.setattr(combtran, "SORT_RUN_RECORD_COUNT", runRecordCount)
    monkeypatch.setattr(combtran, "MERGE_FAN_IN", mergeFanIn)


def ForceExternalSort(monkeypatch: pytest.MonkeyPatch) -> set[Path]:
    """Send every COMBTRAN input through ExternalSort; return the directories its runs go to.

    An input already in TRAN-ID order sorts to itself, so the job's output is unchanged. The
    returned set fills as the job writes run files, one directory per input sorted.
    """
    spillDirectories: set[Path] = set()
    originalWriteRun = combtran.WriteRun

    def ReportOutOfOrder(generation: gdg.Generation) -> bool:
        return False

    def RecordingWriteRun(records: Iterable[str], spillDirectory: gdg.ManagedDirectory) -> str:
        spillDirectories.add(spillDirectory.path)
        return originalWriteRun(records, spillDirectory)

    UseExternalSort(monkeypatch, FORCED_SORT_RUN_RECORD_COUNT, TINY_MERGE_FAN_IN)
    monkeypatch.setattr(combtran, "IsInKeyOrder", ReportOutOfOrder)
    monkeypatch.setattr(combtran, "WriteRun", RecordingWriteRun)
    return spillDirectories


def PlantOrphanWorkDirectory(baseDirectory: Path) -> Path:
    """Leave the spill directory of a killed COMBTRAN, with one run file, in baseDirectory."""
    orphanDirectory = baseDirectory / ORPHAN_SPILL_DIRECTORY_NAME
    orphanDirectory.mkdir(parents=True)
    runRecord = SyntheticRecords([FIRST_INTEREST_TRAN_ID], SYSTRAN_TAG)[0]
    (orphanDirectory / ORPHAN_RUN_FILE_NAME).write_bytes(
        runRecord.encode(gdg.GDG_FILE_ENCODING) + gdg.RECORD_TERMINATOR
    )
    return orphanDirectory


def IsSpillDirectoryName(name: str) -> bool:
    """Return whether name is a COMBTRAN spill directory's: its prefix, then the work suffix."""
    return name.startswith(combtran.SPILL_DIRECTORY_PREFIX) and name.endswith(
        gdg.WORK_DIRECTORY_SUFFIX
    )


def InterestTranIds(recordCount: int, wrapped: bool) -> list[str]:
    """Return INTCALC TRAN-IDs in write order: from suffix 000001, or wrapping at 999999 halfway."""
    firstSuffix = TRANID_SUFFIX_MODULUS - recordCount // 2 if wrapped else 1
    return [
        f"{INTEREST_TRAN_ID_PREFIX}{(firstSuffix + index) % TRANID_SUFFIX_MODULUS:06d}"
        for index in range(recordCount)
    ]


def WriteMergeInputs(
    workDirectory: Path, recordCount: int, wrapped: bool
) -> tuple[gdg.Generation, gdg.Generation]:
    """Write a recordCount-record BKUP and SYSTRAN in a new directory of workDirectory.

    The BKUP TRAN-IDs ascend; the SYSTRAN ones ascend, or wrap at 999999 halfway.
    """
    directory = workDirectory / INPUT_DIRECTORY_TEMPLATE.format(recordCount=recordCount)
    directory.mkdir()
    bkupTranIds = [f"{index:016d}" for index in range(recordCount)]
    systranTranIds = InterestTranIds(recordCount, wrapped)
    return (
        SyntheticGeneration(directory, gdg.TRANSACT_BKUP, SyntheticRecords(bkupTranIds, BKUP_TAG)),
        SyntheticGeneration(directory, gdg.SYSTRAN, SyntheticRecords(systranTranIds, SYSTRAN_TAG)),
    )


def MeasureMerge(
    inputGenerations: tuple[gdg.Generation, gdg.Generation], spillRoot: Path
) -> tuple[int, int]:
    """Merge the inputs under tracemalloc, keeping no record, spilling under spillRoot.

    Returns the traced peak in bytes and the number of records merged.
    """
    tracemalloc.start()
    try:
        with contextlib.ExitStack() as cleanup:
            mergedRecords = combtran.MergedRecords(inputGenerations, cleanup, spillRoot)
            mergedCount = sum(1 for record in mergedRecords)
        peakBytes = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return peakBytes, mergedCount


def MeasureEachSize(
    inputsBySize: dict[int, tuple[gdg.Generation, gdg.Generation]], spillRoot: Path
) -> MergeMeasurements:
    """Merge every input size MEMORY_MEASUREMENT_COUNT times, the sizes taking turns."""
    measurements: MergeMeasurements = {recordCount: [] for recordCount in inputsBySize}
    for _ in range(MEMORY_MEASUREMENT_COUNT):
        for recordCount, inputGenerations in inputsBySize.items():
            measurements[recordCount].append(MeasureMerge(inputGenerations, spillRoot))
    return measurements


def MeasureMergePeaks(probe: MergeMemoryProbe) -> MergeMeasurements:
    """Measure the merge of every MEMORY_INPUT_RECORD_COUNTS size, each more than once.

    The memory test runs this in a fresh interpreter, so the traced peak is the merge's own,
    never what earlier tests, plugins or logging left in the pytest process. An interpreter
    still keeps process-wide tables that grow by reallocation, such as the table of interned
    strings, to which pathlib adds every new run file name. A table reallocated whole lands
    once, in whichever measurement fills it, and adds megabytes to that peak alone, so each
    size is measured MEMORY_MEASUREMENT_COUNT times, the sizes taking turns, and the caller
    compares each size's lowest peak. A merge that holds records raises every peak of the
    larger size, so the lowest one still shows it. Spilled runs go under workDirectory's spill
    root.
    """
    spillRoot = probe.workDirectory / SPILL_ROOT_NAME
    spillRoot.mkdir()
    inputsBySize = {
        recordCount: WriteMergeInputs(probe.workDirectory, recordCount, probe.wrapped)
        for recordCount in MEMORY_INPUT_RECORD_COUNTS
    }
    with (
        mock.patch.object(combtran, "SORT_RUN_RECORD_COUNT", probe.runRecordCount),
        mock.patch.object(combtran, "MERGE_FAN_IN", probe.mergeFanIn),
    ):
        return MeasureEachSize(inputsBySize, spillRoot)


def MeasureMergePeaksInFreshInterpreter(probe: MergeMemoryProbe) -> MergeMeasurements:
    """Run MeasureMergePeaks in a new spawned interpreter and return what it measured.

    The spawned worker imports this module, and so conftest, whose import only points the
    worker's own environment at the test database; it opens no connection.
    """
    spawnContext = multiprocessing.get_context(SPAWN_START_METHOD)
    with concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=spawnContext) as executor:
        return executor.submit(MeasureMergePeaks, probe).result()


def LowestPeak(measurements: list[tuple[int, int]]) -> int:
    """Return the lowest traced peak of one input size's measurements."""
    return min(peakBytes for peakBytes, _ in measurements)


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


@pytest.mark.parametrize("producerPosition", FINAL_WRITE_CASES)
def test_committed_catalog_row_is_the_final_write_of_each_producer(
    batchEnvironment: BatchEnvironment, producerPosition: int
) -> None:
    """The producer's final transaction completes the run, inserts COMMITTED and commits.

    The catalog INSERT is the last statement before COMMIT, and the run's completion precedes
    it in the same transaction (Technical Specification 0.6.8 "Publication", 0.7.2).
    """
    RunSetUpCommands(batchEnvironment, producerPosition)
    arguments, expectedExitCode = ProducerCommands(batchEnvironment.asOfText)[producerPosition]
    with CapturedStatements() as events:
        outcome = InvokeCli(arguments)
    assert outcome.exitCode == expectedExitCode, outcome.output
    statements = CatalogInsertTransaction(events)
    assert statements[-1] == COMMIT_MARKER
    assert statements[-2].startswith(CATALOG_INSERT_PREFIX), statements
    completions = [text for text in statements[:-2] if text.startswith(RUN_COMPLETION_PREFIX)]
    assert len(completions) == 1, statements


@pytest.mark.parametrize("case", ATOMIC_PRODUCER_CASES)
def test_failed_fsync_closing_the_generation_rolls_back_the_run_completion(
    batchEnvironment: BatchEnvironment, case: AtomicProducerCase
) -> None:
    """An fsync error as the generation closes, after the run's completion, fails the step.

    The completion, the step's writes and the catalog row roll back together and the .part file
    is deleted: exit 12, the step FAILED, and the catalog, archive files and TRANSACT unchanged.
    """
    InsertTransaction(batchEnvironment, ARCHIVE_MARKER_TRAN_ID)
    RunSetUpCommands(batchEnvironment, case.setUpCount)
    stateBefore = ReadArchiveState(batchEnvironment, case.baseName)
    with pytest.MonkeyPatch.context() as failurePatch:
        FailGenerationSync(failurePatch, case.baseName)
        outcome = InvokeCli(case.arguments)
    assert outcome.exitCode == legacy_oracle.EXIT_UTILITY_FAILURE, outcome.output
    assert ReadArchiveState(batchEnvironment, case.baseName) == stateBefore
    assert StepRow(batchEnvironment.engine, case.stepNo)["state"] == STEP_FAILED_STATE


@pytest.mark.parametrize("case", ATOMIC_PRODUCER_CASES)
def test_publication_error_after_commit_keeps_step_completed_and_rerun_skips(
    batchEnvironment: BatchEnvironment, case: AtomicProducerCase
) -> None:
    """The runner returns with its generation COMMITTED, so a rename error never demotes it.

    The step stays COMPLETED; the unforced rerun records SKIPPED and its ReconcileCatalog
    publishes the generation, with no second generation and no repeated delete or insert.
    """
    InsertTransaction(batchEnvironment, ARCHIVE_MARKER_TRAN_ID)
    RunSetUpCommands(batchEnvironment, case.setUpCount)
    observedStates, failedOutcome = RunWithFailingPublication(batchEnvironment, case)
    stateAfterFailure = ReadProducerState(batchEnvironment, case)
    rerunOutcome = InvokeCli(case.arguments)
    assert observedStates == [gdg.GDG_COMMITTED], failedOutcome.output
    assert stateAfterFailure.stepState == STEP_COMPLETED_STATE, failedOutcome.output
    assert stateAfterFailure.generationState == gdg.GDG_COMMITTED, failedOutcome.output
    assert rerunOutcome.exitCode == legacy_oracle.EXIT_OK, rerunOutcome.output
    publishedState = replace(stateAfterFailure, generationState=gdg.GDG_PUBLISHED)
    assert ReadProducerState(batchEnvironment, case) == publishedState
    assert CountJobRuns(batchEnvironment.engine, case.stepNo, RUN_SKIPPED) == 1


# The file an OSError names, as a str or bytes name holding a line feed before forged ERROR text
# and an ANSI escape, or as a descriptor, and as the FILE ERROR text shows it: every control
# character escaped, so the name cannot split the failure line or forge another.
FILE_ERROR_NAME_CASES = (
    pytest.param("G0001\nERROR forged\x1b[31m.dat", "G0001\\nERROR forged\\x1b[31m.dat", id="str"),
    pytest.param(
        b"G0001\nERROR forged\x1b[31m.dat", "b'G0001\\nERROR forged\\x1b[31m.dat'", id="bytes"
    ),
    pytest.param(7, "7", id="descriptor"),
)


@pytest.mark.parametrize(("fileName", "shownName"), FILE_ERROR_NAME_CASES)
def test_file_error_names_its_file_with_control_characters_escaped(
    fileName: str | bytes | int, shownName: str
) -> None:
    """A file error's text names the file it concerns on its one line, controls escaped."""
    fileError = OSError(errno.EIO, "injected I/O error", fileName)
    expectedText = f"FILE ERROR OSError ERRNO EIO ON {shownName}"
    assert combtran.DescribeFileError(fileError) == expectedText
    assert combtran.DescribeFailure(fileError) == f"COMBTRAN FAILED: {expectedText}"


@pytest.mark.parametrize("case", MERGE_CASES)
def test_streamed_merge_equals_the_stable_sort_of_the_concatenation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: MergeCase
) -> None:
    """STEP05R: the streamed merge equals the oracle's stable sort of BKUP(0) then SYSTRAN(0).

    Equal keys keep BKUP before SYSTRAN and their order within an input. An input out of order,
    such as a SYSTRAN whose suffix wrapped at 999999, is sorted through spilled runs over more
    than one merge pass, and the spill directory is removed once the merge is closed.
    """
    spillRoot = tmp_path / SPILL_ROOT_NAME
    spillRoot.mkdir()
    UseExternalSort(monkeypatch, TINY_RUN_RECORD_COUNT, TINY_MERGE_FAN_IN)
    bkupRecords = SyntheticRecords(case.bkupTranIds, BKUP_TAG)
    systranRecords = SyntheticRecords(case.systranTranIds, SYSTRAN_TAG)
    inputGenerations = (
        SyntheticGeneration(tmp_path, gdg.TRANSACT_BKUP, bkupRecords),
        SyntheticGeneration(tmp_path, gdg.SYSTRAN, systranRecords),
    )
    with contextlib.ExitStack() as cleanup:
        mergedRecords = list(combtran.MergedRecords(inputGenerations, cleanup, spillRoot))
        spilled = any(spillRoot.iterdir())
    assert mergedRecords == legacy_oracle.RunCombtran(bkupRecords, systranRecords)
    assert spilled == case.spills
    assert list(spillRoot.iterdir()) == []


def test_sort_refuses_a_linked_combined_base_directory_and_spills_nothing_through_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An input out of order spills only beneath the archive root, never through a link.

    A TRANSACT.COMBINED base directory that is a symbolic link raises gdg.ManagedDirectoryError
    naming it before a run is written; the directory it points to keeps exactly what it held,
    and the failure text COMBTRAN logs names the link on one line.
    """
    outsideDirectory = tmp_path / OUTSIDE_DIRECTORY_NAME
    outsideDirectory.mkdir()
    (outsideDirectory / ORPHAN_RUN_FILE_NAME).write_bytes(RECORD_TERMINATOR)
    spillRoot = tmp_path / LINKED_OUTPUT_NAME / compare.GDG_DIRECTORY_NAME / COMBINED_BASE_NAME
    spillRoot.parent.mkdir(parents=True)
    spillRoot.symlink_to(outsideDirectory, target_is_directory=True)
    UseExternalSort(monkeypatch, TINY_RUN_RECORD_COUNT, TINY_MERGE_FAN_IN)
    systranRecords = SyntheticRecords(InterestTranIds(WRAPPED_RECORD_COUNT, True), SYSTRAN_TAG)
    inputGenerations = (
        SyntheticGeneration(tmp_path, gdg.TRANSACT_BKUP, []),
        SyntheticGeneration(tmp_path, gdg.SYSTRAN, systranRecords),
    )
    with pytest.raises(gdg.ManagedDirectoryError) as raised, contextlib.ExitStack() as cleanup:
        combtran.MergedRecords(inputGenerations, cleanup, spillRoot)
    assert raised.value.filename == str(spillRoot)
    assert combtran.DescribeFailure(raised.value).endswith(f" ON {spillRoot}")
    assert [path.name for path in outsideDirectory.iterdir()] == [ORPHAN_RUN_FILE_NAME]
    assert spillRoot.is_symlink()


def test_sort_spills_in_the_combined_base_directory_and_recovery_deletes_a_killed_runs_spill(
    batchEnvironment: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spilled runs go beside the TRANSACT.COMBINED .part file, never to the system TMPDIR.

    The spill directory a killed COMBTRAN left there is deleted, runs and all, by the next
    command's catalog recovery. That command's job, its inputs forced through ExternalSort,
    writes every run to its own carddemo-combtran-*.work directory in the same base directory,
    one per input, still equals the fixture, and leaves only the generation file behind.
    """
    RunChainThroughSecondBackup(batchEnvironment)
    baseDirectory = batchEnvironment.outputDir / compare.GDG_DIRECTORY_NAME / COMBINED_BASE_NAME
    orphanDirectory = PlantOrphanWorkDirectory(baseDirectory)
    spillDirectories = ForceExternalSort(monkeypatch)
    outcome = RunCombtranJob()
    assert outcome.exitCode == legacy_oracle.EXIT_OK, outcome.output
    assert gdg.ORPHAN_WORK_DIRECTORY_DELETED_LOG % orphanDirectory in outcome.output
    assert len(spillDirectories) == SORTIN_INPUT_COUNT
    assert {directory.parent for directory in spillDirectories} == {baseDirectory}
    assert all(IsSpillDirectoryName(directory.name) for directory in spillDirectories)
    combinedPath = NewestGenerationPath(batchEnvironment, COMBINED_BASE_NAME)
    assert ListArchiveFiles(batchEnvironment, COMBINED_BASE_NAME) == [combinedPath.name]
    differences = compare.CompareFixedWidthFiles(
        combinedPath, COMBINED_FIXTURE, TRANSACTION_LAYOUT_NAME
    )
    assert differences == []


@pytest.mark.parametrize("wrapped", MEMORY_CASES)
def test_merge_memory_does_not_grow_with_the_generations(tmp_path: Path, wrapped: bool) -> None:
    """The merge holds a bounded number of records, whatever the size of its two inputs.

    Inputs ten times larger raise the lowest traced peak by less than MERGE_PEAK_GROWTH_LIMIT,
    both when they stream from their files and when the wrapped SYSTRAN goes through spilled
    runs. The merges are measured in a fresh interpreter (MeasureMergePeaks), so the bound holds
    whatever ran before in the pytest process.
    """
    probe = MergeMemoryProbe(tmp_path, wrapped, MEMORY_RUN_RECORD_COUNT, MEMORY_MERGE_FAN_IN)
    measurements = MeasureMergePeaksInFreshInterpreter(probe)
    mergedCounts = {
        recordCount: {mergedCount for _, mergedCount in sizeMeasurements}
        for recordCount, sizeMeasurements in measurements.items()
    }
    expectedCounts = {recordCount: {2 * recordCount} for recordCount in MEMORY_INPUT_RECORD_COUNTS}
    assert mergedCounts == expectedCounts
    smallPeak = LowestPeak(measurements[SMALL_INPUT_RECORD_COUNT])
    largePeak = LowestPeak(measurements[LARGE_INPUT_RECORD_COUNT])
    assert largePeak - smallPeak < MERGE_PEAK_GROWTH_LIMIT, measurements
    assert list((tmp_path / SPILL_ROOT_NAME).iterdir()) == []


def test_load_writes_decodes_and_inserts_the_merge_in_one_bounded_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STEP05R SORTOUT and STEP10 REPRO consume the merged stream once, a batch at a time.

    Each insert carries at most INSERT_BATCH_SIZE rows, comes right after its records are
    written, and the rows follow the records in file order, so no record is held beyond one
    batch and the stream is never traversed twice.
    """
    monkeypatch.setattr(combtran, "INSERT_BATCH_SIZE", SMALL_INSERT_BATCH_SIZE)
    fixtureRecords = compare.ReadFixedWidthRecords(COMBINED_FIXTURE)
    writer = RecordingWriter()
    insertedBatches: list[tuple[int, list[str]]] = []

    def RecordInsert(statement: object, transactionRows: list[dict[str, object]]) -> None:
        tranIds = [transactionRow["tran_id"] for transactionRow in transactionRows]
        insertedBatches.append((writer.recordCount, tranIds))

    connection = mock.Mock(spec=Connection)
    connection.execute.side_effect = RecordInsert
    combtran.LoadMergedRecords(connection, writer, iter(fixtureRecords))
    insertedTranIds = [tranId for writtenCount, tranIds in insertedBatches for tranId in tranIds]
    insertedCounts = list(itertools.accumulate(len(tranIds) for _, tranIds in insertedBatches))
    assert writer.records == fixtureRecords
    assert insertedTranIds == [record[legacy_oracle.TRAN_ID_SLICE] for record in fixtureRecords]
    assert [writtenCount for writtenCount, _ in insertedBatches] == insertedCounts
    assert max(len(tranIds) for _, tranIds in insertedBatches) == SMALL_INSERT_BATCH_SIZE
