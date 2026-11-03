"""The streamed, batched replace of batch.loaders.replace_loader.ReplaceTable.

ReplaceTable opens the image with StreamImage before any database work, then, inside one
engine.begin() transaction, defers the constraints, deletes the table's rows and inserts the
image's rows INSERT_BATCH_SIZE at a time as they are decoded, finishing the ledger run last
(Technical Specification, Sections 0.6.8, 0.6.12 and 0.7.2). A recording engine stands in for
PostgreSQL, so no database is needed: it records each statement, each insert batch with its
record_seq values and how many records the row builder had pulled when it ran, and whether the
transaction committed or rolled back. run_ledger.FinishAtomicRun is replaced by a recorder.
The daily images are synthetic copies of the first record of
data/EBCDIC/AWS.M2.CARDDEMO.DALYTRAN.PS; the priming-only TRANFILE image is the shipped
AWS.M2.CARDDEMO.DALYTRAN.PS.INIT.
"""

import itertools
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.core.paths import REPO_ROOT
from app.models import DailyTransaction, Transaction
from app.seed.image_reader import (
    ENCODING_EBCDIC,
    SAMPLE_IMAGE_NAMES,
    ImageFormatError,
    IsTranfilePrimingRecord,
)
from app.seed.sample_seed import DAILY_SEQUENCE_COLUMN, BuildDailyTransactionRows
from app.utils.record_layouts import DAILY_TRANSACTION_LAYOUT, TRANSACTION_LAYOUT
from sqlalchemy import Delete, Insert, TextClause

from batch.loaders.replace_loader import (
    DEFER_CONSTRAINTS_SQL,
    IMAGE_NOT_FOUND_TEMPLATE,
    IMAGE_REJECTED_TEMPLATE,
    INSERT_BATCH_SIZE,
    RECORDS_SKIPPED_KEY,
    ROWS_LOADED_KEY,
    LoaderSpec,
    ReplaceTable,
)
from batch.orchestration import run_ledger
from batch.orchestration.clock import Clock
from batch.orchestration.job_context import EXIT_FAILURE, JobContext, JobFailure

SHIPPED_EBCDIC_DIR = REPO_ROOT / "data" / "EBCDIC"
DAILY_SAMPLE_PATH = SHIPPED_EBCDIC_DIR / SAMPLE_IMAGE_NAMES["daily_transactions"].ebcdicName
TRANFILE_IMAGE_PATH = SHIPPED_EBCDIC_DIR / SAMPLE_IMAGE_NAMES["transactions"].ebcdicName
DAILY_DATASET = "daily_transactions"
DAILY_IMAGE_NAME = "daily.dat"
# The lowest BCRYPT_ROUNDS the settings accept; the daily builder hashes nothing.
TEST_BCRYPT_ROUNDS = 4

# Two full insert batches and one single-row batch.
STREAMED_RECORD_COUNT = 2 * INSERT_BATCH_SIZE + 1
# A record in the second batch, so the first batch has been inserted when it is decoded.
MALFORMED_RECORD_NUMBER = INSERT_BATCH_SIZE + INSERT_BATCH_SIZE // 2
# CVTRA06Y DALYTRAN-AMT S9(09)V99 at offset 132; letters are not zoned digits.
TRAN_AMT_OFFSET = 132
MALFORMED_AMOUNT = "XYZ!".encode("cp037")

EVENT_DEFER = "DEFER CONSTRAINTS"
EVENT_DELETE = "DELETE {table}"
EVENT_INSERT = "INSERT {table}"
EVENT_FINISH = "FINISH RUN"
EVENT_COMMIT = "COMMIT"
EVENT_ROLLBACK = "ROLLBACK"
DAILY_TABLE = DailyTransaction.__table__.fullname
TRANSACTIONS_TABLE = Transaction.__table__.fullname

# TRANFILE as its loader declares it: EBCDIC only, the priming record skipped, rows as decoded.
TRANFILE_SPEC = LoaderSpec(
    Transaction.__table__,
    TRANSACTION_LAYOUT,
    "transactions",
    encodings=(ENCODING_EBCDIC,),
    primingCheck=IsTranfilePrimingRecord,
)


@dataclass(frozen=True)
class InsertBatch:
    """One executed INSERT: its rows' record_seq values and the records pulled when it ran."""

    recordSeqs: tuple[object, ...]
    pulledCount: int


@dataclass
class LoadRecord:
    """What one load did to the recording engine, in order."""

    events: list[str] = field(default_factory=list)
    batches: list[InsertBatch] = field(default_factory=list)
    finishedCounts: list[dict[str, int]] = field(default_factory=list)
    beginCount: int = 0
    pulledCount: int = 0


@dataclass(frozen=True)
class SettingsStub:
    """The BatchSettings member the row builders read, without backend/.env."""

    BCRYPT_ROUNDS: int = TEST_BCRYPT_ROUNDS


def StatementEvent(statement: object) -> str:
    """Name an executed statement: the constraint deferral, or a DELETE or INSERT and its table."""
    if isinstance(statement, TextClause) and statement.text == DEFER_CONSTRAINTS_SQL.text:
        return EVENT_DEFER
    if isinstance(statement, Delete):
        return EVENT_DELETE.format(table=statement.table.fullname)
    if isinstance(statement, Insert):
        return EVENT_INSERT.format(table=statement.table.fullname)
    raise AssertionError(f"unexpected statement {statement!r}")


def RecordStatement(
    loadRecord: LoadRecord, statement: object, parameters: list[dict] | None = None
) -> None:
    """Record one Connection.execute call, and an INSERT's rows as one InsertBatch."""
    loadRecord.events.append(StatementEvent(statement))
    if isinstance(statement, Insert):
        recordSeqs = tuple(row.get(DAILY_SEQUENCE_COLUMN) for row in parameters)
        loadRecord.batches.append(InsertBatch(recordSeqs, loadRecord.pulledCount))


@contextmanager
def RecordedTransaction(loadRecord: LoadRecord) -> Iterator[SimpleNamespace]:
    """Stand in for Engine.begin(): yield a recording connection, then record the outcome."""
    loadRecord.beginCount += 1
    committed = False
    try:
        yield SimpleNamespace(execute=partial(RecordStatement, loadRecord))
        committed = True
    finally:
        loadRecord.events.append(EVENT_COMMIT if committed else EVENT_ROLLBACK)


@pytest.fixture(name="imageDir")
def ImageDir() -> Iterator[Path]:
    """Yield a fresh directory for synthetic images, under TMPDIR, removed afterwards."""
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


@pytest.fixture(name="loadRecord")
def LoadRecorder(monkeypatch) -> LoadRecord:
    """Return a fresh LoadRecord that also records each FinishAtomicRun call as an event."""
    loadRecord = LoadRecord()

    def RecordFinish(connection: object, context: JobContext, result: object) -> None:
        loadRecord.events.append(EVENT_FINISH)
        loadRecord.finishedCounts.append(dict(result.counts))

    monkeypatch.setattr(run_ledger, "FinishAtomicRun", RecordFinish)
    return loadRecord


def LoadContext(loadRecord: LoadRecord) -> JobContext:
    """Return a context, bound to no run, whose engine records into loadRecord."""
    engine = SimpleNamespace(begin=partial(RecordedTransaction, loadRecord))
    return JobContext(engine, SettingsStub(), Clock())


def CountPulls(records: Iterable[dict[str, object]], loadRecord: LoadRecord) -> Iterator[dict]:
    """Yield records unchanged, counting in loadRecord each one pulled from the stream."""
    for record in records:
        loadRecord.pulledCount += 1
        yield record


def DailySpec(loadRecord: LoadRecord, recordLimit: int | None = None) -> LoaderSpec:
    """Return the DALYTRAN staging spec, its builder counting the records it pulls.

    With recordLimit, the builder stops after that many records, leaving the rest unread.
    """

    def BuildCountedDailyRows(records: Iterable[dict[str, object]], context: JobContext):
        pulledRecords = itertools.islice(CountPulls(records, loadRecord), recordLimit)
        return BuildDailyTransactionRows(pulledRecords, context.settings.BCRYPT_ROUNDS)

    return LoaderSpec(
        DailyTransaction.__table__,
        DAILY_TRANSACTION_LAYOUT,
        DAILY_DATASET,
        rowBuilder=BuildCountedDailyRows,
    )


def WriteDailyImage(
    directory: Path, recordCount: int, malformedRecordNumber: int | None = None
) -> Path:
    """Write recordCount copies of the sample daily record, one with a non-numeric amount."""
    sampleRecord = DAILY_SAMPLE_PATH.read_bytes()[: DAILY_TRANSACTION_LAYOUT.lrecl]
    amountEnd = TRAN_AMT_OFFSET + len(MALFORMED_AMOUNT)
    malformedRecord = sampleRecord[:TRAN_AMT_OFFSET] + MALFORMED_AMOUNT + sampleRecord[amountEnd:]
    imagePath = directory / DAILY_IMAGE_NAME
    with imagePath.open("wb") as image:
        for recordNumber in range(1, recordCount + 1):
            isMalformed = recordNumber == malformedRecordNumber
            image.write(malformedRecord if isMalformed else sampleRecord)
    return imagePath


def RejectedPrefix(imagePath: Path, recordNumber: int) -> str:
    """Return the start of the exit-12 text for a malformed daily record."""
    reason = f"{imagePath}: record {recordNumber}: "
    return IMAGE_REJECTED_TEMPLATE.format(dataset=DAILY_DATASET, reason=reason)


def test_daily_image_replaces_the_table_in_batches_inside_one_transaction(imageDir, loadRecord):
    imagePath = WriteDailyImage(imageDir, STREAMED_RECORD_COUNT)
    result = ReplaceTable(LoadContext(loadRecord), DailySpec(loadRecord), imagePath)
    expectedCounts = {ROWS_LOADED_KEY: STREAMED_RECORD_COUNT, RECORDS_SKIPPED_KEY: 0}
    insertEvent = EVENT_INSERT.format(table=DAILY_TABLE)
    assert result.counts == expectedCounts
    assert loadRecord.beginCount == 1
    assert loadRecord.events == [
        EVENT_DEFER,
        EVENT_DELETE.format(table=DAILY_TABLE),
        insertEvent,
        insertEvent,
        insertEvent,
        EVENT_FINISH,
        EVENT_COMMIT,
    ]
    assert [len(batch.recordSeqs) for batch in loadRecord.batches] == [1000, 1000, 1]
    insertedSeqs = [recordSeq for batch in loadRecord.batches for recordSeq in batch.recordSeqs]
    assert insertedSeqs == list(range(1, STREAMED_RECORD_COUNT + 1))
    assert loadRecord.finishedCounts == [expectedCounts]


def test_each_batch_is_inserted_before_later_records_are_read(imageDir, loadRecord):
    imagePath = WriteDailyImage(imageDir, STREAMED_RECORD_COUNT)
    ReplaceTable(LoadContext(loadRecord), DailySpec(loadRecord), imagePath)
    assert len(loadRecord.batches) == 3
    for batchNumber, batch in enumerate(loadRecord.batches, start=1):
        assert batch.pulledCount <= (batchNumber + 1) * INSERT_BATCH_SIZE
    assert loadRecord.batches[0].pulledCount < STREAMED_RECORD_COUNT


def test_malformed_record_after_a_batch_rolls_back_the_whole_load(imageDir, loadRecord):
    imagePath = WriteDailyImage(imageDir, STREAMED_RECORD_COUNT, MALFORMED_RECORD_NUMBER)
    with pytest.raises(JobFailure) as raised:
        ReplaceTable(LoadContext(loadRecord), DailySpec(loadRecord), imagePath)
    assert raised.value.exitCode == EXIT_FAILURE
    assert raised.value.message.startswith(RejectedPrefix(imagePath, MALFORMED_RECORD_NUMBER))
    assert isinstance(raised.value.__cause__, ImageFormatError)
    assert loadRecord.events == [
        EVENT_DEFER,
        EVENT_DELETE.format(table=DAILY_TABLE),
        EVENT_INSERT.format(table=DAILY_TABLE),
        EVENT_ROLLBACK,
    ]
    assert loadRecord.finishedCounts == []


def test_records_a_builder_leaves_unread_are_validated_before_commit(imageDir, loadRecord):
    imagePath = WriteDailyImage(imageDir, STREAMED_RECORD_COUNT, STREAMED_RECORD_COUNT)
    spec = DailySpec(loadRecord, INSERT_BATCH_SIZE)
    with pytest.raises(JobFailure) as raised:
        ReplaceTable(LoadContext(loadRecord), spec, imagePath)
    assert raised.value.message.startswith(RejectedPrefix(imagePath, STREAMED_RECORD_COUNT))
    assert loadRecord.pulledCount == INSERT_BATCH_SIZE
    assert loadRecord.events[-2:] == [EVENT_INSERT.format(table=DAILY_TABLE), EVENT_ROLLBACK]
    assert loadRecord.finishedCounts == []


def test_missing_image_fails_before_any_transaction(imageDir, loadRecord):
    missingPath = imageDir / DAILY_IMAGE_NAME
    with pytest.raises(JobFailure) as raised:
        ReplaceTable(LoadContext(loadRecord), DailySpec(loadRecord), missingPath)
    expectedMessage = IMAGE_NOT_FOUND_TEMPLATE.format(dataset=DAILY_DATASET, path=missingPath)
    assert (raised.value.exitCode, raised.value.message) == (EXIT_FAILURE, expectedMessage)
    assert isinstance(raised.value.__cause__.__cause__, FileNotFoundError)
    assert (loadRecord.beginCount, loadRecord.events) == (0, [])


def test_priming_only_tranfile_image_inserts_nothing(loadRecord):
    result = ReplaceTable(LoadContext(loadRecord), TRANFILE_SPEC, TRANFILE_IMAGE_PATH)
    expectedCounts = {ROWS_LOADED_KEY: 0, RECORDS_SKIPPED_KEY: 1}
    assert result.counts == expectedCounts
    assert loadRecord.events == [
        EVENT_DEFER,
        EVENT_DELETE.format(table=TRANSACTIONS_TABLE),
        EVENT_FINISH,
        EVENT_COMMIT,
    ]
    assert loadRecord.finishedCounts == [expectedCounts]
