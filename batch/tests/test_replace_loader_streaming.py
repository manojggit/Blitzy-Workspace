"""The streamed, batched replace of batch.loaders.replace_loader.ReplaceTable.

ReplaceTable opens the image with StreamImage before any database work, then, inside one
engine.begin() transaction, defers the constraints, deletes the table's rows and inserts the
image's rows INSERT_BATCH_SIZE at a time as they are decoded, finishing the ledger run last
(Technical Specification, Sections 0.6.8, 0.6.12 and 0.7.2). A recording engine stands in for
PostgreSQL, so no database is needed: it records each statement, each insert batch with its
record_seq values and how many records the row builder had pulled when it ran, and whether the
transaction committed or rolled back. run_ledger.FinishAtomicRun is replaced by a recorder.
The daily images are synthetic copies of the first record of
data/EBCDIC/AWS.M2.CARDDEMO.DALYTRAN.PS, in that EBCDIC form or re-encoded as the CRLF-separated
lines of an ASCII image; the priming-only TRANFILE image is the shipped
AWS.M2.CARDDEMO.DALYTRAN.PS.INIT. A rejected image's exit-12 text names the image, the record
and, for a framing error, image_reader's own reason, which holds only fixed text and lengths.
That text, and the traceback rendered from it, must quote none of the refused text: the field
text of a malformed record, the bytes the image's codec or a row builder could not decode, or
the message of the error that caused the rejection.
"""

import itertools
import tempfile
import traceback
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.core.paths import REPO_ROOT
from app.models import DailyTransaction, Transaction
from app.seed.image_reader import (
    ENCODING_ASCII,
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
from batch.orchestration.job_context import EXIT_FAILURE, JobContext, JobFailure, JobParameters

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
TRAN_AMT_LENGTH = 11
DAILY_IMAGE_CODEC = "cp037"
MALFORMED_AMOUNT_TEXT = "XYZ!"
MALFORMED_AMOUNT = MALFORMED_AMOUNT_TEXT.encode(DAILY_IMAGE_CODEC)
# The withheld reason of the malformed record: the column, its offset and the cause's class.
MALFORMED_AMOUNT_REASON = (
    "FIELD tran_amt AT OFFSET 132, CAUSE RecordFormatError; RECORD TEXT WITHHELD"
)
# The framing reasons image_reader gives a daily image hold only fixed text and lengths, so a
# rejection reports each as it is, followed by the FRAMING cause. CVTRA06Y is a 350-byte record
# ending in a 20-byte FILLER, so an ASCII record of 330 bytes is accepted too.
DAILY_LRECL = 350
LENGTH_FRAMING_REASON = (
    "length {length}, expected 350 (or 330 without its trailing FILLER), CAUSE FRAMING"
)
BARE_TERMINATOR_FRAMING_REASON = "bare CR or LF inside record, CAUSE FRAMING"
# An image cut short of a whole record, STREAMED_RECORD_COUNT records and then the malformed
# record up to the end of its amount, is refused on its size before any field is read.
TRUNCATED_IMAGE_SIZE = STREAMED_RECORD_COUNT * DAILY_LRECL + TRAN_AMT_OFFSET + TRAN_AMT_LENGTH
FRAMING_REASON = (
    f"size {TRUNCATED_IMAGE_SIZE} is not a multiple of LRECL {DAILY_LRECL}, CAUSE FRAMING"
)
WHOLE_IMAGE_RECORD_NUMBER = 0

# Bytes a row builder decodes as ASCII for the tran_desc column of one record, and that one
# ASCII image record holds at the start of tran_desc: the sentinel letters, then X'C1', byte 9,
# which the strict ascii codec refuses.
UNDECODABLE_COLUMN = "tran_desc"
UNDECODABLE_CODEC = "ascii"
UNDECODABLE_TEXT = b"SENTINEL\xc1"
UNDECODABLE_REASON = "BYTE 9 IS NOT ascii TEXT, CAUSE UnicodeDecodeError; IMAGE TEXT WITHHELD"
# The sentinel, the refused byte as the codec's message and repr show it, and that message's
# own reason.
UNDECODABLE_SENTINELS = ("SENTINEL", "0xc1", "\\xc1", "ordinal not in range")
# CVTRA06Y DALYTRAN-DESC X(100) at offset 32, so the image record's refused X'C1' is its byte 41.
TRAN_DESC_OFFSET = 32
REFUSED_BYTE_REASON = "BYTE 41 IS NOT ascii TEXT, CAUSE UnicodeDecodeError; RECORD TEXT WITHHELD"
# Decodes each byte to the character of the same ordinal, as a raw byte would show in text.
RAW_BYTE_CODEC = "latin-1"
ASCII_RECORD_TERMINATOR = b"\r\n"
BARE_CR = b"\r"
LONE_LF = b"\n"
# One byte past the LRECL makes a daily record over-long.
OVERLONG_EXTRA_BYTE = b" "

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


@dataclass(frozen=True)
class MalformedImageCase:
    """A daily image with one malformed record, the run's --encoding, and how it is withheld.

    writeImage writes the image as WriteDailyImage does. withheldReason is the exact reason the
    rejection reports, and refusedTexts returns the texts neither its message nor its rendered
    traceback may hold.
    """

    encoding: str
    writeImage: Callable[[Path, int, int | None], Path]
    withheldReason: str
    refusedTexts: Callable[[], Iterable[str]]


@dataclass(frozen=True)
class FramingImageCase:
    """An ASCII daily image framed wrongly at MALFORMED_RECORD_NUMBER, and how it is rejected.

    imageBytes returns the whole image. framingReason is the exact reason the rejection reports:
    image_reader's own reason for that record, followed by the FRAMING cause.
    """

    imageBytes: Callable[[], bytes]
    framingReason: str


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


def LoadContext(loadRecord: LoadRecord, encoding: str = ENCODING_EBCDIC) -> JobContext:
    """Return a context, bound to no run, whose engine records into loadRecord.

    Its parameters give encoding as the run's --encoding.
    """
    engine = SimpleNamespace(begin=partial(RecordedTransaction, loadRecord))
    return JobContext(engine, SettingsStub(), Clock(), JobParameters(encoding=encoding))


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


def UndecodableDailySpec(loadRecord: LoadRecord) -> LoaderSpec:
    """Return the DALYTRAN staging spec, its builder decoding UNDECODABLE_TEXT as ASCII.

    The bytes are decoded for MALFORMED_RECORD_NUMBER's row, after the first batch's insert.
    """
    dailySpec = DailySpec(loadRecord)

    def BuildUndecodableRows(records: Iterable[dict[str, object]], context: JobContext):
        rows = dailySpec.rowBuilder(records, context)
        for recordNumber, row in enumerate(rows, start=1):
            if recordNumber == MALFORMED_RECORD_NUMBER:
                row[UNDECODABLE_COLUMN] = UNDECODABLE_TEXT.decode(UNDECODABLE_CODEC)
            yield row

    return replace(dailySpec, rowBuilder=BuildUndecodableRows)


def SampleDailyRecord() -> bytes:
    """Return the first record of the shipped daily image."""
    return DAILY_SAMPLE_PATH.read_bytes()[: DAILY_TRANSACTION_LAYOUT.lrecl]


def MalformedDailyRecord() -> bytes:
    """Return the sample daily record with MALFORMED_AMOUNT over the start of its amount."""
    sampleRecord = SampleDailyRecord()
    amountEnd = TRAN_AMT_OFFSET + len(MALFORMED_AMOUNT)
    return sampleRecord[:TRAN_AMT_OFFSET] + MALFORMED_AMOUNT + sampleRecord[amountEnd:]


def MalformedAmountTexts() -> tuple[str, str]:
    """Return the malformed record's sentinel and its whole decoded 11-character amount field."""
    amountEnd = TRAN_AMT_OFFSET + TRAN_AMT_LENGTH
    amountText = MalformedDailyRecord()[TRAN_AMT_OFFSET:amountEnd].decode(DAILY_IMAGE_CODEC)
    assert amountText.startswith(MALFORMED_AMOUNT_TEXT) and len(amountText) == TRAN_AMT_LENGTH
    return MALFORMED_AMOUNT_TEXT, amountText


def WriteDailyImage(
    directory: Path, recordCount: int, malformedRecordNumber: int | None = None
) -> Path:
    """Write recordCount copies of the sample daily record, one with a non-numeric amount."""
    sampleRecord = SampleDailyRecord()
    malformedRecord = MalformedDailyRecord()
    imagePath = directory / DAILY_IMAGE_NAME
    with imagePath.open("wb") as image:
        for recordNumber in range(1, recordCount + 1):
            isMalformed = recordNumber == malformedRecordNumber
            image.write(malformedRecord if isMalformed else sampleRecord)
    return imagePath


def AsciiDailyRecord() -> bytes:
    """Return the sample daily record as an ASCII image holds it: its text in the ascii codec."""
    return SampleDailyRecord().decode(DAILY_IMAGE_CODEC).encode(UNDECODABLE_CODEC)


def RefusedByteDailyRecord() -> bytes:
    """Return the ASCII sample daily record with UNDECODABLE_TEXT over the start of tran_desc."""
    asciiRecord = AsciiDailyRecord()
    descEnd = TRAN_DESC_OFFSET + len(UNDECODABLE_TEXT)
    return asciiRecord[:TRAN_DESC_OFFSET] + UNDECODABLE_TEXT + asciiRecord[descEnd:]


def RefusedByteTexts() -> tuple[str, ...]:
    """Return UNDECODABLE_SENTINELS, the bytes' repr, and the refused byte as a raw character."""
    refusedByte = UNDECODABLE_TEXT[-1:].decode(RAW_BYTE_CODEC)
    return (*UNDECODABLE_SENTINELS, repr(UNDECODABLE_TEXT), refusedByte)


def WriteAsciiDailyImage(
    directory: Path, recordCount: int, malformedRecordNumber: int | None = None
) -> Path:
    """Write recordCount CRLF-terminated ASCII sample daily records, one with a refused byte."""
    records = [AsciiDailyRecord()] * recordCount
    if malformedRecordNumber is not None:
        records[malformedRecordNumber - 1] = RefusedByteDailyRecord()
    imagePath = directory / DAILY_IMAGE_NAME
    imagePath.write_bytes(b"".join(record + ASCII_RECORD_TERMINATOR for record in records))
    return imagePath


def AppendPartialRecord(imagePath: Path) -> None:
    """Append the malformed record's bytes up to the end of its amount, short of a whole record."""
    with imagePath.open("ab") as image:
        image.write(MalformedDailyRecord()[: TRAN_AMT_OFFSET + TRAN_AMT_LENGTH])


def AsciiDailyLines(records: Iterable[bytes]) -> bytes:
    """Return records as the CRLF-terminated lines of an ASCII daily image."""
    return b"".join(record + ASCII_RECORD_TERMINATOR for record in records)


def FaultyRecordImage(faultyRecord: bytes) -> bytes:
    """Return STREAMED_RECORD_COUNT ASCII sample records, MALFORMED_RECORD_NUMBER's faultyRecord."""
    records = [AsciiDailyRecord()] * STREAMED_RECORD_COUNT
    records[MALFORMED_RECORD_NUMBER - 1] = faultyRecord
    return AsciiDailyLines(records)


def BareCrImage() -> bytes:
    """Return the ASCII image whose faulty record has a CR over its first tran_desc byte."""
    asciiRecord = AsciiDailyRecord()
    descRest = asciiRecord[TRAN_DESC_OFFSET + len(BARE_CR) :]
    return FaultyRecordImage(asciiRecord[:TRAN_DESC_OFFSET] + BARE_CR + descRest)


def OverlongRecordImage() -> bytes:
    """Return the ASCII image whose faulty record is one byte longer than its LRECL."""
    return FaultyRecordImage(AsciiDailyRecord() + OVERLONG_EXTRA_BYTE)


def DoubledFinalCrlfImage() -> bytes:
    """Return the records before MALFORMED_RECORD_NUMBER, the final CRLF doubled."""
    records = [AsciiDailyRecord()] * (MALFORMED_RECORD_NUMBER - 1)
    return AsciiDailyLines(records) + ASCII_RECORD_TERMINATOR


def DoubledFinalLoneLfImage() -> bytes:
    """Return MALFORMED_RECORD_NUMBER records, the last ending in a doubled lone LF."""
    records = [AsciiDailyRecord()] * MALFORMED_RECORD_NUMBER
    return AsciiDailyLines(records[:-1]) + records[-1] + LONE_LF + LONE_LF


def RejectedPrefix(imagePath: Path, recordNumber: int) -> str:
    """Return the start of the exit-12 text for a malformed daily record."""
    reason = f"{imagePath}: record {recordNumber}: "
    return IMAGE_REJECTED_TEMPLATE.format(dataset=DAILY_DATASET, reason=reason)


def AssertRejection(
    failure: JobFailure, imagePath: Path, recordNumber: int, reportedReason: str
) -> None:
    """Assert failure is the exit-12 rejection of imagePath's record by reportedReason alone.

    It is chained to an ImageFormatError with the same path and record number and that reason.
    """
    assert failure.exitCode == EXIT_FAILURE
    assert failure.message == RejectedPrefix(imagePath, recordNumber) + reportedReason
    reportedError = failure.__cause__
    assert isinstance(reportedError, ImageFormatError)
    reportedLocation = (reportedError.path, reportedError.recordNumber, reportedError.reason)
    assert reportedLocation == (imagePath, recordNumber, reportedReason)


def AssertQuotesNone(failure: JobFailure, refusedTexts: Iterable[str]) -> None:
    """Assert neither failure's message nor its rendered traceback holds a text, raw or repr."""
    renderedTraceback = "".join(traceback.format_exception(failure))
    for refusedText in refusedTexts:
        for form in (refusedText, repr(refusedText)):
            assert form not in failure.message
            assert form not in renderedTraceback


def AssertAmountWithheld(failure: JobFailure, imagePath: Path, recordNumber: int) -> None:
    """Assert failure rejects the malformed amount by record, column, offset and cause only."""
    AssertRejection(failure, imagePath, recordNumber, MALFORMED_AMOUNT_REASON)
    AssertQuotesNone(failure, MalformedAmountTexts())


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


@pytest.mark.parametrize(
    "malformedCase",
    [
        pytest.param(
            MalformedImageCase(
                ENCODING_EBCDIC, WriteDailyImage, MALFORMED_AMOUNT_REASON, MalformedAmountTexts
            ),
            id="ebcdic-malformed-amount",
        ),
        pytest.param(
            MalformedImageCase(
                ENCODING_ASCII, WriteAsciiDailyImage, REFUSED_BYTE_REASON, RefusedByteTexts
            ),
            id="ascii-refused-byte",
        ),
    ],
)
def test_malformed_record_after_a_batch_rolls_back_the_whole_load(
    imageDir, loadRecord, malformedCase
):
    imagePath = malformedCase.writeImage(imageDir, STREAMED_RECORD_COUNT, MALFORMED_RECORD_NUMBER)
    context = LoadContext(loadRecord, malformedCase.encoding)
    with pytest.raises(JobFailure) as raised:
        ReplaceTable(context, DailySpec(loadRecord), imagePath)
    withheldReason = malformedCase.withheldReason
    AssertRejection(raised.value, imagePath, MALFORMED_RECORD_NUMBER, withheldReason)
    AssertQuotesNone(raised.value, malformedCase.refusedTexts())
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
    AssertAmountWithheld(raised.value, imagePath, STREAMED_RECORD_COUNT)
    assert loadRecord.pulledCount == INSERT_BATCH_SIZE
    assert loadRecord.events[-2:] == [EVENT_INSERT.format(table=DAILY_TABLE), EVENT_ROLLBACK]
    assert loadRecord.finishedCounts == []


def test_image_cut_short_of_a_record_is_rejected_as_framing(imageDir, loadRecord):
    imagePath = WriteDailyImage(imageDir, STREAMED_RECORD_COUNT)
    AppendPartialRecord(imagePath)
    with pytest.raises(JobFailure) as raised:
        ReplaceTable(LoadContext(loadRecord), DailySpec(loadRecord), imagePath)
    AssertRejection(raised.value, imagePath, WHOLE_IMAGE_RECORD_NUMBER, FRAMING_REASON)
    AssertQuotesNone(raised.value, MalformedAmountTexts())
    assert loadRecord.events == [
        EVENT_DEFER,
        EVENT_DELETE.format(table=DAILY_TABLE),
        EVENT_ROLLBACK,
    ]
    assert loadRecord.finishedCounts == []


@pytest.mark.parametrize(
    "framingCase",
    [
        pytest.param(
            FramingImageCase(BareCrImage, BARE_TERMINATOR_FRAMING_REASON),
            id="bare-cr-inside-record",
        ),
        pytest.param(
            FramingImageCase(
                OverlongRecordImage, LENGTH_FRAMING_REASON.format(length=DAILY_LRECL + 1)
            ),
            id="over-long-record",
        ),
        pytest.param(
            FramingImageCase(DoubledFinalCrlfImage, LENGTH_FRAMING_REASON.format(length=0)),
            id="doubled-final-crlf",
        ),
        pytest.param(
            FramingImageCase(DoubledFinalLoneLfImage, BARE_TERMINATOR_FRAMING_REASON),
            id="doubled-final-lone-lf",
        ),
    ],
)
def test_ascii_framing_error_after_a_batch_is_rejected_with_its_reason(
    imageDir, loadRecord, framingCase
):
    imagePath = imageDir / DAILY_IMAGE_NAME
    imagePath.write_bytes(framingCase.imageBytes())
    with pytest.raises(JobFailure) as raised:
        ReplaceTable(LoadContext(loadRecord, ENCODING_ASCII), DailySpec(loadRecord), imagePath)
    framingReason = framingCase.framingReason
    AssertRejection(raised.value, imagePath, MALFORMED_RECORD_NUMBER, framingReason)
    assert loadRecord.events == [
        EVENT_DEFER,
        EVENT_DELETE.format(table=DAILY_TABLE),
        EVENT_INSERT.format(table=DAILY_TABLE),
        EVENT_ROLLBACK,
    ]
    assert loadRecord.finishedCounts == []


def test_bytes_a_row_builder_cannot_decode_roll_back_without_being_quoted(imageDir, loadRecord):
    imagePath = WriteDailyImage(imageDir, STREAMED_RECORD_COUNT)
    with pytest.raises(JobFailure) as raised:
        ReplaceTable(LoadContext(loadRecord), UndecodableDailySpec(loadRecord), imagePath)
    failure = raised.value
    AssertRejection(failure, imagePath, WHOLE_IMAGE_RECORD_NUMBER, UNDECODABLE_REASON)
    AssertQuotesNone(failure, UNDECODABLE_SENTINELS + (repr(UNDECODABLE_TEXT),))
    assert loadRecord.events == [
        EVENT_DEFER,
        EVENT_DELETE.format(table=DAILY_TABLE),
        EVENT_INSERT.format(table=DAILY_TABLE),
        EVENT_ROLLBACK,
    ]
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
