"""Replaces: the IDCAMS REPRO input of ACCTFILE, CARDFILE, CUSTFILE, XREFFILE, TRANFILE,
TRANTYPE, TRANCATG, DISCGRP, TCATBALF and DUSRSECJ; copybooks CVACT01Y, CVACT02Y, CVACT03Y,
CVCUS01Y, CVTRA01Y, CVTRA02Y, CVTRA03Y, CVTRA04Y, CVTRA05Y, CVTRA06Y, CSUSR01Y.

ReadImage is the only reader of the legacy seed images in data/EBCDIC and data/ASCII, and of
the DALYTRAN daily file. It reads an image in place, never writes it, splits it into records,
skips a priming record when the caller supplies a check, and decodes every other record by
copybook offset through record_layouts.DecodeRecord, in file order:
- EBCDIC images are fixed-length code page 037 records with no terminators.
- ASCII images are CRLF-separated lines; the final terminator may be CRLF or a lone LF.
    A line that lacks only its layout's trailing FILLER is padded with spaces.
Every length, terminator, charset or field problem raises ImageFormatError naming the file and
the record. Records are never logged: USRSEC records carry plaintext passwords.

The module is synchronous, imports no database or settings code, and reads no SECRET_KEY.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
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
FILLER_PADDING_BYTE = b" "


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
    is ENCODING_EBCDIC or ENCODING_ASCII. primingCheck, when given, sees each record's raw bytes
    before any decoding; a record it accepts is skipped and counted in skippedCount. Raises
    ImageFormatError for any file, framing or field problem, and ValueError or TypeError for an
    unsupported encoding or a layout that is not a RecordLayout.
    """
    _RequireEncoding(encoding)
    if not isinstance(layout, RecordLayout):
        raise TypeError(f"layout must be a RecordLayout, not {type(layout).__name__}")
    source = ImageSource(ResolveRepoPath(path), layout, encoding)
    rawRecords = SPLITTERS[encoding](source.path, ReadImageBytes(source.path), layout)
    records: list[dict[str, object]] = []
    skippedCount = 0
    for recordNumber, rawRecord in enumerate(rawRecords, start=1):
        if primingCheck is not None and primingCheck(rawRecord):
            skippedCount += 1
            continue
        records.append(DecodeRawRecord(source, recordNumber, rawRecord))
    return ImageContents(tuple(records), skippedCount)


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


def ReadImageBytes(path: str | Path) -> bytes:
    """Return the whole image at path, resolved under the repository root when relative.

    Raises ImageFormatError with record number 0 when the file cannot be read.
    """
    try:
        return ResolveRepoPath(path).read_bytes()
    except OSError as error:
        reason = f"cannot read image: {error.strerror or error}"
        raise ImageFormatError(path, 0, reason) from error


def SplitEbcdicRecords(path: str | Path, data: bytes, layout: RecordLayout) -> list[bytes]:
    """Split a terminator-free fixed-length image into LRECL-byte records.

    An empty image gives no records. Raises ImageFormatError with record number 0 when the
    size is not a multiple of the layout's LRECL.
    """
    lrecl = layout.lrecl
    if len(data) % lrecl != 0:
        reason = f"size {len(data)} is not a multiple of LRECL {lrecl}"
        raise ImageFormatError(path, 0, reason)
    return [data[offset : offset + lrecl] for offset in range(0, len(data), lrecl)]


def SplitAsciiRecords(path: str | Path, data: bytes, layout: RecordLayout) -> list[bytes]:
    """Split a CRLF-separated image into records checked by CheckAsciiRecord.

    Exactly one terminator, CRLF or a lone LF, is removed at end of file, and a file without a
    final terminator is accepted unchanged. A file holding nothing else gives no records.
    """
    body = _StripFinalTerminator(data)
    if body == b"":
        return []
    lines = body.split(CRLF)
    return [
        CheckAsciiRecord(path, recordNumber, line, layout)
        for recordNumber, line in enumerate(lines, start=1)
    ]


def CheckAsciiRecord(
    path: str | Path, recordNumber: int, record: bytes, layout: RecordLayout
) -> bytes:
    """Return one ASCII record of exactly LRECL bytes, or raise ImageFormatError.

    A record holding a bare CR or LF is refused. A record missing exactly its layout's trailing
    FILLER, as data/ASCII/cardxref.txt lines miss the 14-byte FILLER of CVACT03Y, is padded
    with spaces; every other length is refused.
    """
    if CR in record or LF in record:
        raise ImageFormatError(path, recordNumber, "bare CR or LF inside record")
    if len(record) == layout.lrecl:
        return record
    shortLength = layout.lrecl - layout.trailingFillerLength
    if layout.trailingFillerLength > 0 and len(record) == shortLength:
        return record.ljust(layout.lrecl, FILLER_PADDING_BYTE)
    reason = f"length {len(record)}, expected {layout.lrecl}"
    if layout.trailingFillerLength > 0:
        reason += f" (or {shortLength} without its trailing FILLER)"
    raise ImageFormatError(path, recordNumber, reason)


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


def _StripFinalTerminator(data: bytes) -> bytes:
    """Remove one final CRLF, or else one final lone LF; other data is returned unchanged."""
    if data.endswith(CRLF):
        return data[: -len(CRLF)]
    if data.endswith(LF):
        return data[: -len(LF)]
    return data


SPLITTERS: dict[str, Callable[[str | Path, bytes, RecordLayout], list[bytes]]] = {
    ENCODING_EBCDIC: SplitEbcdicRecords,
    ENCODING_ASCII: SplitAsciiRecords,
}
