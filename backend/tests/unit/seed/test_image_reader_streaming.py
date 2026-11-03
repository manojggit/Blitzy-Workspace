"""Unit tests for the incremental reading of app.seed.image_reader.StreamImage and ReadImage.

StreamImage reads an EBCDIC image one LRECL-byte record at a time and an ASCII image one line at
a time, and yields each decoded record as it is read; ReadImage collects what StreamImage
yields. These tests pin what that must preserve from the whole-image rules of the Technical
Specification, Section 0.8.2:
- Memory: with every record skipped by the priming check, ReadImage's peak traced allocation
    stays far below the image size, because no copy of the whole image is made; with every
    record decoded, so does a full StreamImage pass, because no list of its records is made.
- Laziness: a stream yields each record right after reading it, so the records before a late
    field error reach the caller before that error is raised.
- ASCII framing: the image minus exactly one final terminator (CRLF, or else a lone LF) is
    split on CRLF. An image holding only that terminator has no records; any other empty piece
    is a record of the wrong length; a lone LF elsewhere, or a final CR, is a bare terminator.
- Precedence: framing errors anywhere in the image outrank a field error in an earlier record,
    and an EBCDIC size error is raised before any record is read, by both entry points alike.
- Causes: unreadable images chain the OSError, charset errors the UnicodeDecodeError, field
    errors the RecordFormatError; size and framing errors have no cause.
The synthetic layout has LRECL 3: a two-digit PIC 9(2) field and a one-byte trailing FILLER, so
"12" and "12 " are its only valid records.
"""

import os
import tempfile
import threading
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pytest

from app.core.paths import REPO_ROOT
from app.seed.image_reader import (
    ENCODING_ASCII,
    ENCODING_EBCDIC,
    SAMPLE_IMAGE_NAMES,
    ImageFormatError,
    ReadImage,
    StreamImage,
)
from app.utils.record_layouts import (
    DAILY_TRANSACTION_LAYOUT,
    KIND_DIGITS,
    FieldSpec,
    RecordFormatError,
    RecordLayout,
)

TINY_LAYOUT = RecordLayout(
    name="TINY",
    lrecl=3,
    fields=(FieldSpec("code", 0, 2, KIND_DIGITS, 0, 0, "TINY-CODE"),),
    trailingFillerLength=1,
)
IMAGE_NAME = "image.dat"
BARE = "bare CR or LF inside record"
LENGTH_REASON = "length {}, expected 3 (or 2 without its trailing FILLER)"

# About 4 MB per image: 12,000 DALYTRAN-sized records, decodable or not, all skipped.
MEMORY_RECORD_COUNT = 12_000
# The peak may reach at most 1/32 of the image size, about 130 kB.
MEMORY_PEAK_DIVISOR = 32
MEMORY_CASES = ((ENCODING_EBCDIC, b""), (ENCODING_ASCII, b"\r\n"))
# The shipped daily file, whose first record every decodable memory-test record copies.
DAILY_SAMPLE_PATH = (
    REPO_ROOT / "data" / "EBCDIC" / SAMPLE_IMAGE_NAMES["daily_transactions"].ebcdicName
)
# "12 12 AB ": two valid TINY records, then a field error in record 3.
LATE_FIELD_ERROR_IMAGE = "12 12 AB ".encode("cp037")

# (ASCII image bytes, record count or (record number, reason)).
ASCII_FRAMING_CASES = (
    (b"", 0),
    (b"\r\n", 0),
    (b"\n", 0),
    (b"12\r\n12 \n", 2),
    (b"12\r\n12 \r\n", 2),
    (b"12\r\n12", 2),
    (b"\r\n\n", (1, LENGTH_REASON.format(0))),
    (b"\r\nAB\r\n", (1, LENGTH_REASON.format(0))),
    (b"12\r\n\r\n", (2, LENGTH_REASON.format(0))),
    (b"12\r\n\r\n\n", (2, LENGTH_REASON.format(0))),
    (b"12\n12\r\n", (1, BARE)),
    (b"12\r\n12\n\n", (2, BARE)),
    (b"12\r\n1\r2\r\n", (2, BARE)),
    (b"12\r", (1, BARE)),
    (b"12 \r\r\n", (1, BARE)),
    (b"1\r\n", (1, LENGTH_REASON.format(1))),
    (b"1234\r\n", (1, LENGTH_REASON.format(4))),
    (b"12345678\r\n", (1, LENGTH_REASON.format(8))),
    (b"12345678\n", (1, LENGTH_REASON.format(8))),
    (b"1234567", (1, LENGTH_REASON.format(7))),
    (b"1234\r" + b"5" * 100_000 + b"\r\n", (1, BARE)),
    (b"12345678\n12\r\n", (1, BARE)),
    (b"1" * 200_000 + b"\r\n", (1, LENGTH_REASON.format(200_000))),
)
ASCII_FRAMING_IDS = (
    "empty",
    "crlf-only",
    "lf-only",
    "final-lone-lf",
    "final-crlf",
    "no-final-terminator",
    "crlf-then-lf",
    "leading-empty-record",
    "trailing-empty-record",
    "empty-record-before-final-lf",
    "lone-lf-before-more-data",
    "lone-lf-then-final-lf",
    "bare-cr-mid-record",
    "final-cr",
    "cr-before-crlf",
    "short-not-filler-short",
    "over-long",
    "longer-than-a-line-read",
    "longer-than-a-line-read-final-lf",
    "longer-than-a-line-read-unterminated",
    "bare-cr-in-chunked-line",
    "lone-lf-after-long-line",
    "chunked-over-long",
)


class ErrorCase(NamedTuple):
    """An image whose ImageFormatError is pinned: its record number, reason and cause type."""

    encoding: str
    imageBytes: bytes
    recordNumber: int
    reason: str
    causeType: type[BaseException] | None


EBCDIC_BAD_THEN_SHORT = "AB 12 1".encode("cp037")
ERROR_CASES = (
    ErrorCase(ENCODING_ASCII, b"12\r\nAB\r\n12\r\n1\r2\r\n", 4, BARE, None),
    ErrorCase(ENCODING_ASCII, b"AB\r\n1\r\n", 2, LENGTH_REASON.format(1), None),
    ErrorCase(ENCODING_ASCII, b"AB\r\n" + b"1" * 100 + b"\r\n", 2, LENGTH_REASON.format(100), None),
    ErrorCase(ENCODING_ASCII, b"12\r\nAB\r\n12\r\n", 2, "TINY field code", RecordFormatError),
    ErrorCase(ENCODING_ASCII, b"1\x80\r\n", 1, "byte 2 is not ascii text", UnicodeDecodeError),
    ErrorCase(ENCODING_EBCDIC, EBCDIC_BAD_THEN_SHORT, 0, "size 7 is not a multiple", None),
    ErrorCase(ENCODING_EBCDIC, "12 AB ".encode("cp037"), 2, "TINY field code", RecordFormatError),
)
ERROR_CASE_IDS = (
    "ascii-bare-cr-outranks-earlier-field-error",
    "ascii-length-outranks-earlier-field-error",
    "ascii-over-long-outranks-earlier-field-error",
    "ascii-field-error-without-framing-error",
    "ascii-charset-error",
    "ebcdic-size-outranks-earlier-field-error",
    "ebcdic-field-error-with-whole-records",
)

# A FIFO feeder still blocked after this long means ReadImage never opened the FIFO.
FIFO_FEED_TIMEOUT_SECONDS = 10

# (path under the image directory, the OSError subclass ReadImage chains).
UNREADABLE_CASES = (("missing.dat", FileNotFoundError), ("", IsADirectoryError))
UNREADABLE_IDS = ("missing-file", "directory")


@pytest.fixture(name="imageDir")
def ImageDir():
    """Yield a fresh directory for synthetic images, under TMPDIR, removed afterwards."""
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


def WriteImage(imageDir: Path, imageBytes: bytes) -> Path:
    """Write imageBytes as the image file in imageDir and return its absolute path."""
    imagePath = imageDir / IMAGE_NAME
    imagePath.write_bytes(imageBytes)
    return imagePath


def SkipEveryRecord(rawRecord: bytes) -> bool:
    """A priming check that accepts, and so skips, every record, whatever its bytes."""
    return isinstance(rawRecord, bytes)


def RecordingCheck(seenRecords: list[bytes]) -> Callable[[bytes], bool]:
    """Return a priming check that records every raw record it sees and skips none."""

    def CheckRecord(rawRecord: bytes) -> bool:
        seenRecords.append(rawRecord)
        return False

    return CheckRecord


def ReadImageCount(
    imagePath: Path,
    layout: RecordLayout,
    encoding: str,
    primingCheck: Callable[[bytes], bool] | None = None,
) -> int:
    """Read the whole image with ReadImage and return how many records it kept."""
    return len(ReadImage(imagePath, layout, encoding, primingCheck))


def StreamImageCount(
    imagePath: Path,
    layout: RecordLayout,
    encoding: str,
    primingCheck: Callable[[bytes], bool] | None = None,
) -> int:
    """Iterate the whole image with StreamImage, keeping no record, and return how many it gave."""
    with StreamImage(imagePath, layout, encoding, primingCheck) as stream:
        return sum(1 for record in stream)


# Each test of an error outcome runs against both entry points, which must agree.
ENTRY_POINTS = (
    pytest.param(ReadImageCount, id="read-image"),
    pytest.param(StreamImageCount, id="stream-image"),
)


def FramingOutcome(imagePath: Path) -> int | tuple[int, str]:
    """Return the ASCII image's record count, or the record number and reason it fails on."""
    try:
        return len(ReadImage(imagePath, TINY_LAYOUT, ENCODING_ASCII))
    except ImageFormatError as error:
        return error.recordNumber, error.reason


def FeedFifo(fifoPath: Path, imageBytes: bytes) -> None:
    """Write imageBytes into the FIFO at fifoPath, then close it so the reader sees its end."""
    with fifoPath.open("wb") as fifo:
        fifo.write(imageBytes)


@pytest.mark.parametrize(("encoding", "separator"), MEMORY_CASES, ids=("ebcdic", "ascii"))
def test_skipped_records_keep_peak_memory_far_below_the_image_size(imageDir, encoding, separator):
    recordBytes = b"1" * DAILY_TRANSACTION_LAYOUT.lrecl + separator
    imagePath = WriteImage(imageDir, recordBytes * MEMORY_RECORD_COUNT)
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        contents = ReadImage(imagePath, DAILY_TRANSACTION_LAYOUT, encoding, SkipEveryRecord)
        peakBytes = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert (len(contents), contents.skippedCount) == (0, MEMORY_RECORD_COUNT)
    assert peakBytes < imagePath.stat().st_size // MEMORY_PEAK_DIVISOR


def test_streamed_decodable_records_keep_peak_memory_far_below_the_image_size(imageDir):
    sampleRecord = DAILY_SAMPLE_PATH.read_bytes()[: DAILY_TRANSACTION_LAYOUT.lrecl]
    # One decode before tracing, so lazily built codec and timestamp caches are not counted.
    warmUpPath = WriteImage(imageDir, sampleRecord)
    assert StreamImageCount(warmUpPath, DAILY_TRANSACTION_LAYOUT, ENCODING_EBCDIC) == 1
    imagePath = WriteImage(imageDir, sampleRecord * MEMORY_RECORD_COUNT)
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        recordCount = StreamImageCount(imagePath, DAILY_TRANSACTION_LAYOUT, ENCODING_EBCDIC)
        peakBytes = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert recordCount == MEMORY_RECORD_COUNT
    assert peakBytes < imagePath.stat().st_size // MEMORY_PEAK_DIVISOR


def test_stream_yields_each_record_before_reading_the_next(imageDir):
    imagePath = WriteImage(imageDir, LATE_FIELD_ERROR_IMAGE)
    seenRecords: list[bytes] = []
    yieldedRecords: list[tuple[int, dict[str, object]]] = []
    with (
        pytest.raises(ImageFormatError) as raised,
        StreamImage(imagePath, TINY_LAYOUT, ENCODING_EBCDIC, RecordingCheck(seenRecords)) as stream,
    ):
        for record in stream:
            yieldedRecords.append((len(seenRecords), record))
    assert yieldedRecords == [(1, {"code": "12"}), (2, {"code": "12"})]
    assert raised.value.recordNumber == 3
    assert raised.value.reason.startswith("TINY field code")


@pytest.mark.parametrize(
    ("imageBytes", "expectedOutcome"), ASCII_FRAMING_CASES, ids=ASCII_FRAMING_IDS
)
def test_ascii_framing_follows_the_whole_image_rules(imageDir, imageBytes, expectedOutcome):
    assert FramingOutcome(WriteImage(imageDir, imageBytes)) == expectedOutcome


@pytest.mark.parametrize("readWholeImage", ENTRY_POINTS)
@pytest.mark.parametrize("errorCase", ERROR_CASES, ids=ERROR_CASE_IDS)
def test_error_precedence_and_cause(imageDir, errorCase, readWholeImage):
    imagePath = WriteImage(imageDir, errorCase.imageBytes)
    with pytest.raises(ImageFormatError) as raised:
        readWholeImage(imagePath, TINY_LAYOUT, errorCase.encoding)
    assert (raised.value.path, raised.value.recordNumber) == (imagePath, errorCase.recordNumber)
    assert raised.value.reason.startswith(errorCase.reason)
    causeType = None if raised.value.__cause__ is None else type(raised.value.__cause__)
    assert causeType is errorCase.causeType


@pytest.mark.parametrize("readWholeImage", ENTRY_POINTS)
def test_ebcdic_size_error_precedes_reading_any_record(imageDir, readWholeImage):
    seenRecords: list[bytes] = []
    imagePath = WriteImage(imageDir, EBCDIC_BAD_THEN_SHORT)
    with pytest.raises(ImageFormatError) as raised:
        readWholeImage(imagePath, TINY_LAYOUT, ENCODING_EBCDIC, RecordingCheck(seenRecords))
    assert (raised.value.recordNumber, raised.value.reason) == (
        0,
        "size 7 is not a multiple of LRECL 3",
    )
    assert seenRecords == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a POSIX FIFO")
def test_ebcdic_stream_without_size_fails_on_its_short_final_read(imageDir):
    fifoPath = imageDir / IMAGE_NAME
    os.mkfifo(fifoPath)
    feeder = threading.Thread(target=FeedFifo, args=(fifoPath, EBCDIC_BAD_THEN_SHORT), daemon=True)
    feeder.start()
    try:
        with pytest.raises(ImageFormatError) as raised:
            ReadImage(fifoPath, TINY_LAYOUT, ENCODING_EBCDIC)
    finally:
        feeder.join(FIFO_FEED_TIMEOUT_SECONDS)
    assert not feeder.is_alive()
    assert (raised.value.recordNumber, raised.value.reason) == (
        0,
        "size 7 is not a multiple of LRECL 3",
    )
    assert raised.value.__cause__ is None


@pytest.mark.parametrize("readWholeImage", ENTRY_POINTS)
@pytest.mark.parametrize(("relativePath", "causeType"), UNREADABLE_CASES, ids=UNREADABLE_IDS)
def test_unreadable_image_chains_its_os_error(imageDir, relativePath, causeType, readWholeImage):
    imagePath = imageDir / relativePath
    with pytest.raises(ImageFormatError) as raised:
        readWholeImage(imagePath, TINY_LAYOUT, ENCODING_ASCII)
    assert (raised.value.path, raised.value.recordNumber) == (imagePath, 0)
    assert raised.value.reason.startswith("cannot read image: ")
    assert type(raised.value.__cause__) is causeType
