"""Replaces: the IDCAMS REPRO input of ACCTFILE, CARDFILE, CUSTFILE, XREFFILE, TRANFILE,
TRANTYPE, TRANCATG, DISCGRP, TCATBALF and DUSRSECJ; copybooks CVACT01Y, CVACT02Y, CVACT03Y,
CVCUS01Y, CVTRA01Y, CVTRA02Y, CVTRA03Y, CVTRA04Y, CVTRA05Y, CVTRA06Y, CSUSR01Y.

ReadImage is the only reader of the legacy seed images in data/EBCDIC and data/ASCII, and of
the DALYTRAN daily file. It reads an image in place, never writes it, frames it into records,
skips a priming record when the caller supplies a check, and decodes every other record by
copybook offset through record_layouts.DecodeRecord, in file order:
- EBCDIC images are fixed-length code page 037 records with no terminators, read one
    LRECL-byte record at a time.
- ASCII images are CRLF-separated lines; the final terminator may be CRLF or a lone LF. They
    are read one line at a time, and a line too long for any record is measured in chunks.
    A line that lacks only its layout's trailing FILLER is padded with spaces.
Only one record's raw bytes are held at a time; the decoded records are kept. Every length,
terminator, charset or field problem raises ImageFormatError naming the file and the record.
A framing problem anywhere in the image outranks a field problem in an earlier record. Records
are never logged: USRSEC records carry plaintext passwords.

The module is synchronous, imports no database or settings code, and reads no SECRET_KEY.
"""

import os
import stat
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from io import BufferedReader
from pathlib import Path
from typing import NamedTuple

from app.core.paths import ResolveRepoPath
from app.utils.record_layouts import DecodeRecord, RecordLayout

ENCODING_EBCDIC = "ebcdic"
ENCODING_ASCII = "ascii"
SUPPORTED_ENCODINGS = (ENCODING_EBCDIC, ENCODING_ASCII)

# Python codec names. The ascii codec is strict: any byte above X'7F' raises UnicodeDecodeError.
EBCDIC_CODEC = "cp037"
ASCII_CODEC = "ascii"
CODECS = {ENCODING_EBCDIC: EBCDIC_CODEC, ENCODING_ASCII: ASCII_CODEC}

# DALYTRAN.PS.INIT, the TRANFILE priming record: bytes 0-341 are X'00'.
TRANFILE_PRIMING_NUL_LENGTH = 342
TRANFILE_PRIMING_PREFIX = bytes(TRANFILE_PRIMING_NUL_LENGTH)

CR = b"\r"
LF = b"\n"
CRLF = b"\r\n"
NO_TERMINATOR = b""
FILLER_PADDING_BYTE = b" "
BARE_TERMINATOR_REASON = "bare CR or LF inside record"
# The rest of an ASCII line longer than any record is read this many bytes at a time.
OVERLONG_LINE_CHUNK_SIZE = 64 * 1024


class SampleImageNames(NamedTuple):
    """The file names of one dataset's seed image: EBCDIC always, ASCII where a copy exists."""

    ebcdicName: str
    asciiName: str | None


# Keyed by target table, in foreign-key parent order. AWS.M2.CARDDEMO.ACCDATA.PS duplicates
# ACCTDATA.PS byte for byte, and EXPORT.DATA.PS belongs to the out-of-scope CBEXPORT, so
# neither has an entry.
SAMPLE_IMAGE_NAMES: dict[str, SampleImageNames] = {
    "accounts": SampleImageNames("AWS.M2.CARDDEMO.ACCTDATA.PS", "acctdata.txt"),
    "customers": SampleImageNames("AWS.M2.CARDDEMO.CUSTDATA.PS", "custdata.txt"),
    "cards": SampleImageNames("AWS.M2.CARDDEMO.CARDDATA.PS", "carddata.txt"),
    "card_xref": SampleImageNames("AWS.M2.CARDDEMO.CARDXREF.PS", "cardxref.txt"),
    "transaction_types": SampleImageNames("AWS.M2.CARDDEMO.TRANTYPE.PS", "trantype.txt"),
    "transaction_categories": SampleImageNames("AWS.M2.CARDDEMO.TRANCATG.PS", "trancatg.txt"),
    "disclosure_groups": SampleImageNames("AWS.M2.CARDDEMO.DISCGRP.PS", "discgrp.txt"),
    "transaction_category_balances": SampleImageNames("AWS.M2.CARDDEMO.TCATBALF.PS", "tcatbal.txt"),
    "users": SampleImageNames("AWS.M2.CARDDEMO.USRSEC.PS", None),
    "daily_transactions": SampleImageNames("AWS.M2.CARDDEMO.DALYTRAN.PS", "dailytran.txt"),
    "transactions": SampleImageNames("AWS.M2.CARDDEMO.DALYTRAN.PS.INIT", None),
}


class ImageFormatError(ValueError):
    """A seed image that cannot be read, framed or decoded; loaders map it to exit 12.

    recordNumber is the 1-based record number, or 0 for a problem with the whole file such as
    a missing file or a size that is not a multiple of the LRECL.
    """

    def __init__(self, path: str | Path, recordNumber: int, reason: str) -> None:
        super().__init__(f"{path}: record {recordNumber}: {reason}")
        self.path = path
        self.recordNumber = recordNumber
        self.reason = reason


@dataclass(frozen=True)
class ImageContents:
    """The decoded records of one image in file order, and how many priming records were skipped.

    Iterating yields the record dicts, and len() counts them.
    """

    records: tuple[dict[str, object], ...]
    skippedCount: int = 0

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)


@dataclass(frozen=True)
class ImageSource:
    """The image a record came from: its resolved path, its copybook layout and its encoding."""

    path: Path
    layout: RecordLayout
    encoding: str


def ReadImage(
    path: str | Path,
    layout: RecordLayout,
    encoding: str,
    primingCheck: Callable[[bytes], bool] | None = None,
) -> ImageContents:
    """Read and decode every record of the image at path, in file order.

    A relative path resolves under the repository root, never the working directory. encoding
    is ENCODING_EBCDIC or ENCODING_ASCII. The image is read one record at a time through its
    RECORD_READERS entry, so only the decoded records accumulate. primingCheck, when given,
    sees each record's raw bytes before any decoding; a record it accepts is skipped and
    counted in skippedCount. Raises ImageFormatError for any file, framing or field problem,
    and ValueError or TypeError for an unsupported encoding or a layout that is not a
    RecordLayout, before the file is opened.
    """
    _RequireEncoding(encoding)
    if not isinstance(layout, RecordLayout):
        raise TypeError(f"layout must be a RecordLayout, not {type(layout).__name__}")
    source = ImageSource(ResolveRepoPath(path), layout, encoding)
    with _ImageIo(source.path, lambda: source.path.open("rb")) as image:
        return _CollectRecords(source, RECORD_READERS[encoding](source, image), primingCheck)


def SampleImagePath(sourceDir: str | Path, dataset: str, encoding: str) -> tuple[Path, str]:
    """Return (image path, encoding to read it with) for a SAMPLE_IMAGE_NAMES dataset.

    sourceDir resolves under the repository root when relative. The ASCII copy is chosen when
    encoding is ENCODING_ASCII and the dataset has one; otherwise the EBCDIC image is. Raises
    ValueError for an unknown dataset or encoding.
    """
    names = SAMPLE_IMAGE_NAMES.get(dataset)
    if names is None:
        knownDatasets = ", ".join(SAMPLE_IMAGE_NAMES)
        raise ValueError(f"unknown sample dataset {dataset!r}; expected one of {knownDatasets}")
    _RequireEncoding(encoding)
    directory = ResolveRepoPath(sourceDir)
    if encoding == ENCODING_ASCII and names.asciiName is not None:
        return directory / names.asciiName, ENCODING_ASCII
    # USRSEC and the TRANFILE priming image exist only in EBCDIC, so they are read as EBCDIC
    # from the directory the caller named, whichever encoding it asked for.
    return directory / names.ebcdicName, ENCODING_EBCDIC


def IsTranfilePrimingRecord(rawRecordBytes: bytes) -> bool:
    """Return True for the TRANFILE priming record: its first 342 bytes are all X'00'.

    jcl/TRANFILE.jcl STEP15 REPROs AWS.M2.CARDDEMO.DALYTRAN.PS.INIT, whose single record is
    X'00' in bytes 0-341 with "00000100" in bytes 342-349, only to prime the new cluster. A
    PostgreSQL text column cannot hold NUL and a table needs no priming, so loaders skip it.
    """
    return rawRecordBytes[:TRANFILE_PRIMING_NUL_LENGTH] == TRANFILE_PRIMING_PREFIX


def ReadEbcdicRecords(source: ImageSource, image: BufferedReader) -> Iterator[bytes]:
    """Yield the LRECL-byte records of a terminator-free fixed-length image, one read each.

    A regular file's size is checked against the layout's LRECL before the first record is
    read; a short final read, from a file that changed or a stream with no size, raises the
    same error naming every byte read. An empty image gives no records. Raises
    ImageFormatError with record number 0 when the size is not a multiple of the LRECL.
    """
    lrecl = source.layout.lrecl
    imageStatus = _ImageIo(source.path, lambda: os.fstat(image.fileno()))
    if stat.S_ISREG(imageStatus.st_mode):
        _RequireWholeRecords(source, imageStatus.st_size)
    bytesRead = 0
    while True:
        rawRecord = _ImageIo(source.path, lambda: image.read(lrecl))
        bytesRead += len(rawRecord)
        if len(rawRecord) < lrecl:
            _RequireWholeRecords(source, bytesRead)
            return
        yield rawRecord


def ReadAsciiRecords(source: ImageSource, image: BufferedReader) -> Iterator[bytes]:
    """Yield the records of a CRLF-separated image, checked by CheckAsciiRecord, line by line.

    Exactly one terminator, CRLF or a lone LF, is removed at end of file, and a file without a
    final terminator is accepted unchanged. A file holding nothing else gives no records. A
    lone LF anywhere else is a bare LF inside its record.
    """
    recordNumber = 1
    while (record := _ReadAsciiRecord(source, image, recordNumber)) is not None:
        yield CheckAsciiRecord(source.path, recordNumber, record, source.layout)
        recordNumber += 1


def CheckAsciiRecord(
    path: str | Path, recordNumber: int, record: bytes, layout: RecordLayout
) -> bytes:
    """Return one ASCII record of exactly LRECL bytes, or raise ImageFormatError.

    A record holding a bare CR or LF is refused. A record missing exactly its layout's trailing
    FILLER, as data/ASCII/cardxref.txt lines miss the 14-byte FILLER of CVACT03Y, is padded
    with spaces; every other length is refused.
    """
    if CR in record or LF in record:
        raise ImageFormatError(path, recordNumber, BARE_TERMINATOR_REASON)
    if len(record) == layout.lrecl:
        return record
    shortLength = layout.lrecl - layout.trailingFillerLength
    if layout.trailingFillerLength > 0 and len(record) == shortLength:
        return record.ljust(layout.lrecl, FILLER_PADDING_BYTE)
    raise ImageFormatError(path, recordNumber, _AsciiLengthReason(layout, len(record)))


def DecodeRawRecord(source: ImageSource, recordNumber: int, rawRecord: bytes) -> dict[str, object]:
    """Decode one LRECL-byte record with its image's codec and copybook layout.

    Raises ImageFormatError naming the image and record for a byte the codec refuses or a
    field DecodeRecord refuses.
    """
    codec = CODECS[source.encoding]
    try:
        return DecodeRecord(rawRecord.decode(codec), source.layout)
    except UnicodeDecodeError as error:
        reason = f"byte {error.start + 1} is not {codec} text ({error.reason})"
        raise ImageFormatError(source.path, recordNumber, reason) from error
    except ValueError as error:
        # DecodeRecord reports every field problem, zoned-decimal and timestamp ones included,
        # as RecordFormatError; all of these record_layouts errors are ValueErrors.
        raise ImageFormatError(source.path, recordNumber, str(error)) from error


def _RequireEncoding(encoding: str) -> None:
    """Refuse an encoding other than ENCODING_EBCDIC and ENCODING_ASCII."""
    if encoding not in SUPPORTED_ENCODINGS:
        supported = ", ".join(SUPPORTED_ENCODINGS)
        raise ValueError(f"unsupported image encoding {encoding!r}; expected one of {supported}")


def _ImageIo[ResultT](path: Path, operation: Callable[[], ResultT]) -> ResultT:
    """Run one open, status or read call on the image at path and return its result.

    Raises ImageFormatError with record number 0, chained to the OSError, when it fails.
    """
    try:
        return operation()
    except OSError as error:
        reason = f"cannot read image: {error.strerror or error}"
        raise ImageFormatError(path, 0, reason) from error


def _CollectRecords(
    source: ImageSource,
    rawRecords: Iterator[bytes],
    primingCheck: Callable[[bytes], bool] | None,
) -> ImageContents:
    """Skip the raw records primingCheck accepts and decode the others, in file order.

    A framing problem anywhere in the image outranks a field problem in an earlier record: after
    the first field error the rest of the image is only framed, never priming-checked, decoded
    or kept, and the field error is raised once framing reaches end of file without an error.
    """
    records: list[dict[str, object]] = []
    skippedCount = 0
    fieldError: ImageFormatError | None = None
    for recordNumber, rawRecord in enumerate(rawRecords, start=1):
        if primingCheck is not None and primingCheck(rawRecord):
            skippedCount += 1
            continue
        try:
            records.append(DecodeRawRecord(source, recordNumber, rawRecord))
        except ImageFormatError as error:
            fieldError = error
            break
    if fieldError is not None:
        deque(rawRecords, maxlen=0)
        raise fieldError
    return ImageContents(tuple(records), skippedCount)


def _RequireWholeRecords(source: ImageSource, size: int) -> None:
    """Refuse an EBCDIC image size that is not a multiple of the layout's LRECL."""
    if size % source.layout.lrecl != 0:
        reason = f"size {size} is not a multiple of LRECL {source.layout.lrecl}"
        raise ImageFormatError(source.path, 0, reason)


def _ReadAsciiRecord(source: ImageSource, image: BufferedReader, recordNumber: int) -> bytes | None:
    """Read one line and return it as record recordNumber without its terminator.

    Returns None at end of file, and for record 1 when the file holds only a terminator. Raises
    ImageFormatError for a lone LF that does not end the file, and for a line longer than any
    record, which is measured without being held.
    """
    lineLimit = source.layout.lrecl + len(CRLF)
    line = _ImageIo(source.path, lambda: image.readline(lineLimit))
    if line == b"":
        return None
    if len(line) == lineLimit and not line.endswith(LF):
        raise _OverlongLineError(source, image, recordNumber, line)
    terminator = _LineTerminator(line)
    record = line[: len(line) - len(terminator)]
    if terminator == LF and not _AtEndOfImage(source, image):
        raise ImageFormatError(source.path, recordNumber, BARE_TERMINATOR_REASON)
    if recordNumber == 1 and record == b"" and _AtEndOfImage(source, image):
        return None
    return record


def _OverlongLineError(
    source: ImageSource, image: BufferedReader, recordNumber: int, head: bytes
) -> ImageFormatError:
    """Read the rest of a line too long for any record, and return its record's error.

    The rest is read in OVERLONG_LINE_CHUNK_SIZE pieces keeping only the line's length, its CR
    count and its last two bytes. As in CheckAsciiRecord, a bare CR or LF outranks the length.
    """
    lineLength, crCount, lastBytes = len(head), head.count(CR), head[-len(CRLF) :]
    while not lastBytes.endswith(LF):
        chunk = _ImageIo(source.path, lambda: image.readline(OVERLONG_LINE_CHUNK_SIZE))
        if chunk == b"":
            break
        lineLength, crCount = lineLength + len(chunk), crCount + chunk.count(CR)
        lastBytes = (lastBytes + chunk)[-len(CRLF) :]
    terminator = _LineTerminator(lastBytes)
    loneLfInside = terminator == LF and not _AtEndOfImage(source, image)
    if loneLfInside or crCount > terminator.count(CR):
        return ImageFormatError(source.path, recordNumber, BARE_TERMINATOR_REASON)
    reason = _AsciiLengthReason(source.layout, lineLength - len(terminator))
    return ImageFormatError(source.path, recordNumber, reason)


def _LineTerminator(line: bytes) -> bytes:
    """Return the CRLF or lone LF that ends line, or NO_TERMINATOR at an unterminated end."""
    if line.endswith(CRLF):
        return CRLF
    if line.endswith(LF):
        return LF
    return NO_TERMINATOR


def _AtEndOfImage(source: ImageSource, image: BufferedReader) -> bool:
    """Return True when no byte follows the read position, without consuming any."""
    return _ImageIo(source.path, lambda: image.peek(1)) == b""


def _AsciiLengthReason(layout: RecordLayout, length: int) -> str:
    """Describe an ASCII record of the wrong length, naming the lengths the layout accepts."""
    reason = f"length {length}, expected {layout.lrecl}"
    if layout.trailingFillerLength > 0:
        shortLength = layout.lrecl - layout.trailingFillerLength
        reason += f" (or {shortLength} without its trailing FILLER)"
    return reason


# The per-encoding record iterators ReadImage drives over the open image.
RECORD_READERS: dict[str, Callable[[ImageSource, BufferedReader], Iterator[bytes]]] = {
    ENCODING_EBCDIC: ReadEbcdicRecords,
    ENCODING_ASCII: ReadAsciiRecords,
}
