"""Unit tests for app.services.reports.report_export, the CR00 report CSV and PDF downloads.

cbl/CBTRN03C.cbl wrote the CR00 transaction report to the TRANREPT print file
(proc/TRANREPT.prc: LRECL=133, RECFM=FB) in the cpy/CVTRA07Y.cpy line layouts. The report now
renders online, and report_export turns the same report lines into the two downloads that
replace the TRANREPT file. The module is pure, so these tests call it directly with no
database. Expected values are typed by hand from that download contract, never taken from the
module:
- CSV: UTF-8, a header row line_kind, tran_id, acct_id, type_cd, type_desc, cat_cd, cat_desc,
    source, amount, then one row per report line in report order; amounts are plain decimals;
    a text cell starting with =, +, - or @ gets a ' prefix so a spreadsheet keeps it as text,
    while a negative amount is numeric data and stays unguarded;
- PDF: every 133-column print line drawn unchanged in Courier, as TRANREPT printed it, with a
    new physical page only when a page is full; the document is deterministic and uncompressed,
    so a small decoder here follows the page tree from the trailer's /Root and reads back the
    strings each page's content stream shows. The tests compare those drawn lines, page by
    page, with the expected print text; metadata and unreferenced objects never count. Each
    page's content stream is sent once its lines are read, the cross-reference table locates
    every object, and each page defines the Courier font its content selects;
- both downloads are built in memory: rendering them with every file-system audit event
    refused and recorded must succeed with no attempt recorded.
"""

import contextlib
import contextvars
import csv
import io
import itertools
import os
import pathlib
import re
import sys
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from app.schemas.reports import ReportLine, ReportLineKind
from app.services.reports import report_export

EXPECTED_HEADER = "line_kind,tran_id,acct_id,type_cd,type_desc,cat_cd,cat_desc,source,amount"
EXPECTED_COLUMNS = tuple(EXPECTED_HEADER.split(","))
CSV_LINE_END = "\r\n"

# FD-REPTFILE-REC PIC X(133): every TRANREPT print line is 133 columns.
PRINT_LINE_WIDTH = 133
REPORT_DATE = "2022-07-18"
# REPORT-NAME-HEADER (CVTRA07Y lines 4-13): REPT-SHORT-NAME X(38), REPT-LONG-NAME X(41),
# 'Date Range: ', REPT-START-DATE X(10), ' to ', REPT-END-DATE X(10).
HEADER_TEXT = (
    "DALYREPT".ljust(38)
    + "Daily Transaction Report".ljust(41)
    + f"Date Range: {REPORT_DATE} to {REPORT_DATE}"
).ljust(PRINT_LINE_WIDTH)
# REPORT-*-TOTALS (CVTRA07Y lines 50-66): the label FILLER and its dot-leader FILLER together
# fill 97 columns (X(11) + X(86), or X(13) + X(84)) before the +ZZZ,ZZZ,ZZZ.ZZ total.
TOTAL_FIELD_START = 97

# Card 2988091353094312 of account 00000000004 (data/ASCII/cardxref.txt record 13), type 01
# Purchase (trantype.txt) and category 0001 Regular Sales Draft (trancatg.txt).
DETAIL_DEFAULTS = {
    "tran_id": "0000000000000001",
    "acct_id": "00000000004",
    "type_cd": "01",
    "type_desc": "Purchase",
    "cat_cd": 1,
    "cat_desc": "Regular Sales Draft",
    "source": "POS TERM",
}
# 1234.50 + 5.00 - 10.00 + 0.50, edited as +ZZZ,ZZZ,ZZZ.ZZ.
REPORT_TOTAL = Decimal("1230.00")
EDITED_REPORT_TOTAL = "+      1,230.00"


def DetailText(fields: dict[str, Any], editedAmount: str) -> str:
    """Return the TRANSACTION-DETAIL-REPORT print line (CVTRA07Y lines 15-31) for fields."""
    text = (
        f"{fields['tran_id']:<16} {fields['acct_id']:<11} {fields['type_cd']:<2}-"
        f"{fields['type_desc']:<15} {fields['cat_cd']:04d}-{fields['cat_desc']:<29} "
        f"{fields['source']:<10}    {editedAmount}  "
    )
    return text.ljust(PRINT_LINE_WIDTH)


def TotalText(label: str, labelWidth: int, editedTotal: str) -> str:
    """Return a REPORT-*-TOTALS print line: label in its field, a dot leader, then the total."""
    leader = "." * (TOTAL_FIELD_START - labelWidth)
    return (label.ljust(labelWidth) + leader + editedTotal).ljust(PRINT_LINE_WIDTH)


def BuildLine(
    kind: ReportLineKind,
    text: str,
    amount: Decimal | None = None,
    detailFields: dict[str, Any] | None = None,
) -> ReportLine:
    """Return a ReportLine of kind with its print text, amount and, on detail lines, fields."""
    return ReportLine(line_kind=kind, text=text, amount=amount, **(detailFields or {}))


def BuildDetailLine(overrides: dict[str, Any], amount: Decimal, editedAmount: str) -> ReportLine:
    """Return a detail line from DETAIL_DEFAULTS with overrides applied, text matching fields."""
    detailFields = DETAIL_DEFAULTS | overrides
    text = DetailText(detailFields, editedAmount)
    return BuildLine(ReportLineKind.DETAIL, text, amount, detailFields)


# Lines as cbtrn03c.BuildReport returns them. The descriptions and sources keyed online in CT02
# reach the report as entered, so formula-like text and PDF string delimiters are reachable.
SAMPLE_LINES = (
    BuildLine(ReportLineKind.HEADER, HEADER_TEXT),
    BuildDetailLine({}, Decimal("1234.50"), "       1,234.50"),
    BuildDetailLine(
        {"tran_id": "0000000000000002", "type_desc": "=SUM(A1)", "source": "@cmd"},
        Decimal("5.00"),
        "           5.00",
    ),
    BuildDetailLine(
        {"tran_id": "0000000000000003", "type_cd": "03", "type_desc": "Credit", "cat_desc": "+1"},
        Decimal("-10.00"),
        "-         10.00",
    ),
    BuildDetailLine(
        {"tran_id": "-000000000000004", "cat_desc": "Refund\\Reversal"},
        Decimal("0.50"),
        "            .50",
    ),
    BuildLine(
        ReportLineKind.ACCOUNT_TOTAL,
        TotalText("Account Total", 13, EDITED_REPORT_TOTAL),
        REPORT_TOTAL,
    ),
    BuildLine(
        ReportLineKind.PAGE_TOTAL,
        TotalText("Page Total", 11, EDITED_REPORT_TOTAL),
        REPORT_TOTAL,
    ),
    BuildLine(
        ReportLineKind.GRAND_TOTAL,
        TotalText("Grand Total", 11, EDITED_REPORT_TOTAL),
        REPORT_TOTAL,
    ),
)
HEADER_LINE_INDEX = 0
PURCHASE_LINE_INDEX = 1
FORMULA_LINE_INDEX = 2
CREDIT_LINE_INDEX = 3
DASH_ID_LINE_INDEX = 4
GRAND_TOTAL_LINE_INDEX = 7

EXPECTED_AMOUNT_CELLS = ("", "1234.50", "5.00", "-10.00", "0.50", "1230.00", "1230.00", "1230.00")

GUARDED_CELL_CASES = (
    (FORMULA_LINE_INDEX, "type_desc", "'=SUM(A1)"),
    (FORMULA_LINE_INDEX, "source", "'@cmd"),
    (CREDIT_LINE_INDEX, "cat_desc", "'+1"),
    (DASH_ID_LINE_INDEX, "tran_id", "'-000000000000004"),
    (PURCHASE_LINE_INDEX, "type_desc", "Purchase"),
)
GUARDED_CELL_IDS = (
    "equals-type-desc",
    "at-source",
    "plus-cat-desc",
    "minus-tran-id",
    "plain-text-unchanged",
)

FULL_ROW_CASES = (
    (HEADER_LINE_INDEX, ["header", "", "", "", "", "", "", "", ""]),
    (
        PURCHASE_LINE_INDEX,
        [
            "detail",
            "0000000000000001",
            "00000000004",
            "01",
            "Purchase",
            "0001",
            "Regular Sales Draft",
            "POS TERM",
            "1234.50",
        ],
    ),
    (GRAND_TOTAL_LINE_INDEX, ["grand_total", "", "", "", "", "", "", "", "1230.00"]),
)
FULL_ROW_IDS = ("header-line", "detail-line", "grand-total-line")

GUARD_CASES = (
    ("=x", "'=x"),
    ("+x", "'+x"),
    ("-x", "'-x"),
    ("@x", "'@x"),
    ("x", "x"),
    ("a-b", "a-b"),
    ("", ""),
)
GUARD_IDS = ("equals", "plus", "minus", "at", "plain", "inner-prefix", "empty")

LINES_PER_PAGE = report_export.LinesPerPage()
# (detail line count, the [start, stop) span of those lines each PDF page must draw)
PAGE_SPLIT_CASES = (
    (0, ((0, 0),)),
    (LINES_PER_PAGE, ((0, LINES_PER_PAGE),)),
    (LINES_PER_PAGE + 1, ((0, LINES_PER_PAGE), (LINES_PER_PAGE, LINES_PER_PAGE + 1))),
)
PAGE_SPLIT_IDS = ("empty-report", "full-page", "one-line-over")
# (detail line count, how many lines had been read when each page's content stream was sent)
PAGE_BY_PAGE_CASES = (
    (2 * LINES_PER_PAGE + 1, (LINES_PER_PAGE, 2 * LINES_PER_PAGE, 2 * LINES_PER_PAGE + 1)),
    (3 * LINES_PER_PAGE, (LINES_PER_PAGE, 2 * LINES_PER_PAGE, 3 * LINES_PER_PAGE)),
)
PAGE_BY_PAGE_IDS = ("two-pages-and-a-line", "three-full-pages")
# (detail line count, page count)
PDF_STRUCTURE_CASES = ((0, 1), (LINES_PER_PAGE, 1), (2 * LINES_PER_PAGE + 1, 3))
PDF_STRUCTURE_IDS = ("empty-report", "full-page", "three-pages")
# Every report page selects Courier, as the TRANREPT print lines are drawn in it.
EXPECTED_PAGE_FONT_FACES = {(b"Courier", b"WinAnsiEncoding")}
EXPECTED_TITLE = b"DALYREPT"

# The report fonts use /WinAnsiEncoding, which is Windows code page 1252.
PDF_TEXT_ENCODING = "cp1252"
PDF_OBJECT_PATTERN = re.compile(rb"^(\d+) 0 obj\r?\n(.*?)\r?\nendobj\r?$", re.DOTALL | re.MULTILINE)
PDF_REFERENCE_PATTERN = re.compile(rb"(\d+)\s+0\s+R")
PDF_STREAM_HEAD_PATTERN = re.compile(rb"<<(.*?)>>\s*stream\r?\n", re.DOTALL)
# A direct /Length only: an indirect one, such as /Length 9 0 R, does not match.
PDF_STREAM_LENGTH_PATTERN = re.compile(rb"/Length\s+(\d+)\b(?!\s+\d+\s+R)")
# A content-stream token: a literal string, escapes included, or a run of other non-blank
# bytes, which is an operator, a number or a name.
PDF_CONTENT_TOKEN_PATTERN = re.compile(rb"\((?:[^\\()]|\\.)*\)|[^\s()]+", re.DOTALL)
# A literal-string escape: one to three octal digits, a line end (a continuation), or one byte.
PDF_ESCAPE_PATTERN = re.compile(rb"\\(?:([0-7]{1,3})|(\r\n|\r|\n)|(.))", re.DOTALL)
PDF_ESCAPED_BYTES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
# The last startxref of the file, which gives the offset of its cross-reference table.
PDF_STARTXREF_PATTERN = re.compile(rb"startxref\s+(\d+)\s+%%EOF\s*\Z")
# The table's one subsection, from object 0, and its entry count.
PDF_XREF_SUBSECTION_PATTERN = re.compile(rb"xref\r?\n0 (\d+)\r?\n")
# A 20-byte table entry: offset, generation, n (in use) or f (free), and a two-byte line end.
PDF_XREF_ENTRY_BYTES = 20
PDF_XREF_ENTRY_PATTERN = re.compile(rb"(\d{10}) (\d{5}) ([nf])(?: \r| \n|\r\n)")
# A name and the object it refers to, as /F1 4 0 R in a font resource dictionary.
PDF_NAMED_REFERENCE_PATTERN = re.compile(rb"/([^\s/\[\]()<>]+)\s+(\d+)\s+0\s+R")
# A font selection in a content stream: the resource name, then the size, then Tf.
PDF_FONT_SELECTION_PATTERN = re.compile(rb"/([^\s/\[\]()<>]+)\s+[-+.\d]+\s+Tf\b")

# The audit events (sys.addaudithook) raised when a file is opened, a directory is read, or a
# file-system entry is created, changed, copied, moved or removed. In-memory buffers such as
# io.BytesIO and io.StringIO raise none of them.
FILESYSTEM_AUDIT_EVENTS = frozenset(
    {
        # builtins.open, io.open, io.open_code, io.FileIO and os.open.
        "open",
        # Directory reads.
        "os.listdir",
        "os.scandir",
        "os.walk",
        "os.fwalk",
        "glob.glob",
        "glob.glob/2",
        "pathlib.Path.glob",
        "pathlib.Path.rglob",
        "pathlib.Path.walk",
        # Entry attributes and permissions.
        "os.getxattr",
        "os.listxattr",
        "os.setxattr",
        "os.removexattr",
        "os.chmod",
        "os.chown",
        "os.chflags",
        "os.utime",
        "shutil.chown",
        # Entry creation, change, copy, move and removal.
        "os.mkdir",
        "os.remove",
        "os.rename",
        "os.rmdir",
        "os.truncate",
        "os.link",
        "os.symlink",
        "shutil.copyfile",
        "shutil.copymode",
        "shutil.copystat",
        "shutil.copytree",
        "shutil.move",
        "shutil.rmtree",
        "shutil.make_archive",
        "shutil.unpack_archive",
        "tempfile.mkstemp",
        "tempfile.mkdtemp",
    }
)
# The refused-event list of the active FilesystemDenied block, or None outside every block.
# None leaves the audit hook inert, which matters because a hook, once added, stays for the
# rest of the process.
FILESYSTEM_GUARD_ATTEMPTS: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "filesystemGuardAttempts", default=None
)


def ParseCsv(data: bytes) -> list[list[str]]:
    """Return every row of the UTF-8 CSV document data, header row included."""
    return list(csv.reader(io.StringIO(data.decode("utf-8"))))


def SampleCsvRows() -> list[list[str]]:
    """Return the data rows of the SAMPLE_LINES CSV, header row dropped."""
    return ParseCsv(report_export.RenderCsv(SAMPLE_LINES))[1:]


def SampleCsvCell(lineIndex: int, column: str) -> str:
    """Return the CSV cell of column for the SAMPLE_LINES entry at lineIndex."""
    return SampleCsvRows()[lineIndex][EXPECTED_COLUMNS.index(column)]


def NumberedDetailLines(lineCount: int) -> list[ReportLine]:
    """Return lineCount detail lines with tran_ids 1, 2, ... so every print text is distinct."""
    return [
        BuildDetailLine({"tran_id": f"{lineNumber:016d}"}, Decimal("1.00"), "           1.00")
        for lineNumber in range(1, lineCount + 1)
    ]


def PdfObjects(pdfBytes: bytes) -> dict[int, bytes]:
    """Return the body of every indirect object of the PDF document pdfBytes, by number."""
    return {int(number): body for number, body in PDF_OBJECT_PATTERN.findall(pdfBytes)}


def PdfReference(dictionary: bytes, key: bytes) -> int:
    """Return the object number dictionary refers to under key, as 5 in /Root 5 0 R."""
    reference = re.search(rb"/" + re.escape(key) + rb"\s+(\d+)\s+0\s+R", dictionary)
    assert reference is not None, key
    return int(reference.group(1))


def PdfHasType(dictionary: bytes, typeName: bytes) -> bool:
    """Return whether dictionary declares /Type /typeName; /Page never matches /Pages."""
    return re.search(rb"/Type\s*/" + re.escape(typeName) + rb"\b", dictionary) is not None


def PdfStreamData(streamObject: bytes) -> bytes:
    """Return the data of the stream object body streamObject, read by its direct /Length.

    A /Filter, such as compression, fails the assertion, so encoded bytes are never read as
    text.
    """
    streamHead = PDF_STREAM_HEAD_PATTERN.match(streamObject)
    assert streamHead is not None
    assert b"/Filter" not in streamHead.group(1)
    streamLength = PDF_STREAM_LENGTH_PATTERN.search(streamHead.group(1))
    assert streamLength is not None
    dataEnd = streamHead.end() + int(streamLength.group(1))
    assert re.fullmatch(rb"\s*endstream", streamObject[dataEnd:])
    return streamObject[streamHead.end() : dataEnd]


def PdfPageContent(objects: dict[int, bytes], page: bytes) -> bytes:
    """Return the content of the page dictionary page: its /Contents streams joined in order."""
    contents = re.search(rb"/Contents\s*(\[[^\]]*\]|\d+\s+0\s+R)", page)
    assert contents is not None
    streamNumbers = PDF_REFERENCE_PATTERN.findall(contents.group(1))
    return b"\n".join(PdfStreamData(objects[int(number)]) for number in streamNumbers)


def PdfPageContents(objects: dict[int, bytes], nodeNumber: int) -> list[bytes]:
    """Return the content of every page under page-tree node nodeNumber, in /Kids order.

    A /Pages node is walked kid by kid, into nested /Pages nodes too; a /Page leaf gives its
    own content.
    """
    node = objects[nodeNumber]
    if PdfHasType(node, b"Page"):
        return [PdfPageContent(objects, node)]
    assert PdfHasType(node, b"Pages")
    kids = re.search(rb"/Kids\s*\[([^\]]*)\]", node)
    assert kids is not None
    pageContents: list[bytes] = []
    for kidNumber in PDF_REFERENCE_PATTERN.findall(kids.group(1)):
        pageContents.extend(PdfPageContents(objects, int(kidNumber)))
    return pageContents


def PdfEscapeValue(escape: re.Match[bytes]) -> bytes:
    """Return the bytes one backslash escape of a PDF literal string stands for.

    An octal escape gives its byte, a backslash before a line end continues the string with
    nothing, and any other escaped byte gives itself, \\n, \\r, \\t, \\b and \\f excepted.
    """
    octalDigits, lineEnd, escapedByte = escape.groups()
    if octalDigits is not None:
        return bytes([int(octalDigits, 8) & 0xFF])
    if lineEnd is not None:
        return b""
    return PDF_ESCAPED_BYTES.get(escapedByte, escapedByte)


def UnescapePdfLiteral(literal: bytes) -> str:
    """Return the text of literal, a PDF literal string with its enclosing parentheses.

    An unescaped line end inside the string reads as one line feed.
    """
    body = re.sub(rb"\r\n?", b"\n", literal[1:-1])
    return PDF_ESCAPE_PATTERN.sub(PdfEscapeValue, body).decode(PDF_TEXT_ENCODING)


def ShownStrings(content: bytes) -> list[str]:
    """Return, in order, the literal strings content shows with Tj inside BT ... ET."""
    shownStrings: list[str] = []
    inTextObject = False
    previousToken = b""
    for token in PDF_CONTENT_TOKEN_PATTERN.findall(content):
        if token in (b"BT", b"ET"):
            inTextObject = token == b"BT"
        elif token == b"Tj" and inTextObject:
            assert previousToken.startswith(b"(")
            shownStrings.append(UnescapePdfLiteral(previousToken))
        previousToken = token
    return shownStrings


def PdfPageLines(pdfBytes: bytes) -> list[list[str]]:
    """Return, page by page, the text lines the PDF document pdfBytes draws.

    The walk starts at the trailer's /Root catalog and follows its page tree in /Kids order, so
    only text a page's content stream shows counts: document metadata and objects no page
    refers to never do. Each string shown with Tj is one drawn line, as RenderPdf draws them.
    """
    objects = PdfObjects(pdfBytes)
    trailer = pdfBytes[pdfBytes.rindex(b"trailer") :]
    catalog = objects[PdfReference(trailer, b"Root")]
    assert PdfHasType(catalog, b"Catalog")
    rootNodeNumber = PdfReference(catalog, b"Pages")
    pageContents = PdfPageContents(objects, rootNodeNumber)
    pageCount = re.search(rb"/Count\s+(\d+)", objects[rootNodeNumber])
    assert pageCount is not None
    assert int(pageCount.group(1)) == len(pageContents)
    return [ShownStrings(content) for content in pageContents]


class LineReader:
    """Hands out report lines one at a time, counting how many have been taken."""

    def __init__(self, lines: list[ReportLine]) -> None:
        self.lines = lines
        self.linesTaken = 0

    def Lines(self) -> Iterator[ReportLine]:
        """Yield the lines in order, counting each one as it is taken."""
        for line in self.lines:
            self.linesTaken += 1
            yield line


def PdfXrefEntries(pdfBytes: bytes) -> list[tuple[int, bytes]]:
    """Return the (offset, n or f) of each entry of the table the PDF's startxref points at.

    The table must sit at that offset, hold one subsection from object 0, and end at the
    trailer.
    """
    startxref = PDF_STARTXREF_PATTERN.search(pdfBytes)
    assert startxref is not None
    subsection = PDF_XREF_SUBSECTION_PATTERN.match(pdfBytes, int(startxref.group(1)))
    assert subsection is not None
    tableEnd = subsection.end() + int(subsection.group(1)) * PDF_XREF_ENTRY_BYTES
    assert pdfBytes.startswith(b"trailer", tableEnd)
    entries: list[tuple[int, bytes]] = []
    for entryStart in range(subsection.end(), tableEnd, PDF_XREF_ENTRY_BYTES):
        entryEnd = entryStart + PDF_XREF_ENTRY_BYTES
        entry = PDF_XREF_ENTRY_PATTERN.fullmatch(pdfBytes, entryStart, entryEnd)
        assert entry is not None
        entries.append((int(entry.group(1)), entry.group(3)))
    return entries


def PdfPageDictionaries(objects: dict[int, bytes], nodeNumber: int) -> list[bytes]:
    """Return every page dictionary under page-tree node nodeNumber, in /Kids order."""
    node = objects[nodeNumber]
    if PdfHasType(node, b"Page"):
        return [node]
    assert PdfHasType(node, b"Pages")
    kids = re.search(rb"/Kids\s*\[([^\]]*)\]", node)
    assert kids is not None
    kidNumbers = PDF_REFERENCE_PATTERN.findall(kids.group(1))
    return [page for kid in kidNumbers for page in PdfPageDictionaries(objects, int(kid))]


def PdfNameValue(dictionary: bytes, key: bytes) -> bytes | None:
    """Return the name dictionary holds under key, as Courier for /BaseFont /Courier."""
    value = re.search(rb"/" + re.escape(key) + rb"\s*/([^\s/\[\]()<>]+)", dictionary)
    return None if value is None else value.group(1)


def PageFontFaces(objects: dict[int, bytes], page: bytes) -> set[tuple[bytes | None, bytes | None]]:
    """Return the (/BaseFont, /Encoding) of each font the content of page selects with Tf.

    Each selected name is looked up in the page's /Resources /Font dictionary, written inline
    or as a reference; a name the page does not define gives (None, None).
    """
    fontResource = re.search(rb"/Font\s*(<<.*?>>|\d+\s+0\s+R)", page, re.DOTALL)
    assert fontResource is not None
    fontMap = fontResource.group(1)
    if not fontMap.startswith(b"<<"):
        fontMap = objects[int(fontMap.split()[0])]
    fontReferences = PDF_NAMED_REFERENCE_PATTERN.findall(fontMap)
    fonts = {name: objects[int(number)] for name, number in fontReferences}
    selectedFonts = [
        fonts.get(name, b"")
        for name in PDF_FONT_SELECTION_PATTERN.findall(PdfPageContent(objects, page))
    ]
    return {
        (PdfNameValue(font, b"BaseFont"), PdfNameValue(font, b"Encoding")) for font in selectedFonts
    }


def PdfMediaBox(page: bytes) -> tuple[float, ...]:
    """Return the four numbers of the /MediaBox of the page dictionary page."""
    mediaBox = re.search(rb"/MediaBox\s*\[([^\]]*)\]", page)
    assert mediaBox is not None
    return tuple(float(number) for number in mediaBox.group(1).split())


def RefuseGuardedFilesystemEvent(event: str, eventArgs: tuple[Any, ...]) -> None:
    """Audit hook: inside a FilesystemDenied block, record a file-system event, then refuse it.

    Recording comes before the PermissionError, so an attempt the code under test catches and
    ignores is still seen. Outside every block, and for any other event, the hook does nothing.
    """
    attempts = FILESYSTEM_GUARD_ATTEMPTS.get()
    if attempts is None or event not in FILESYSTEM_AUDIT_EVENTS:
        return
    attempts.append(event)
    raise PermissionError(f"file-system access refused during an in-memory export: {event}")


sys.addaudithook(RefuseGuardedFilesystemEvent)


@contextlib.contextmanager
def FilesystemDenied() -> Iterator[list[str]]:
    """Refuse, and record, every file-system audit event raised in this context until exit.

    Yields:
        The names of the refused events in the order they were raised; empty when nothing in
        the block reached for the file system.
    """
    attempts: list[str] = []
    guardToken = FILESYSTEM_GUARD_ATTEMPTS.set(attempts)
    try:
        yield attempts
    finally:
        FILESYSTEM_GUARD_ATTEMPTS.reset(guardToken)


def ReadDevnullWithOpen() -> None:
    """Read os.devnull through builtins.open."""
    with open(os.devnull, "rb") as devnull:
        devnull.read()


def WriteDevnullWithOpen() -> None:
    """Write to os.devnull through builtins.open."""
    with open(os.devnull, "wb") as devnull:
        devnull.write(b"\n")


def WriteDevnullWithOsOpen() -> None:
    """Write to os.devnull through a file descriptor from os.open."""
    descriptor = os.open(os.devnull, os.O_WRONLY)
    try:
        os.write(descriptor, b"\n")
    finally:
        os.close(descriptor)


def WriteDevnullWithFileIo() -> None:
    """Write to os.devnull through io.FileIO, the raw file object beneath builtins.open."""
    with io.FileIO(os.devnull, "wb") as devnull:
        devnull.write(b"\n")


def ReadDevnullWithOpenCode() -> None:
    """Read os.devnull through io.open_code, the opener the import system uses."""
    with io.open_code(os.devnull) as devnull:
        devnull.read()


def WriteDevnullWithPathlib() -> None:
    """Write to os.devnull through pathlib.Path.write_bytes."""
    pathlib.Path(os.devnull).write_bytes(b"\n")


# Each way to reach a file, aimed at os.devnull so a guard that failed to refuse one would
# still touch no real file.
DEVNULL_ACCESS_CASES = (
    ReadDevnullWithOpen,
    WriteDevnullWithOpen,
    WriteDevnullWithOsOpen,
    WriteDevnullWithFileIo,
    ReadDevnullWithOpenCode,
    WriteDevnullWithPathlib,
)
DEVNULL_ACCESS_IDS = (
    "open-read",
    "open-write",
    "os-open-write",
    "fileio-write",
    "open-code-read",
    "pathlib-write-bytes",
)


def test_csv_starts_with_the_header_row_and_no_byte_order_mark():
    data = report_export.RenderCsv(SAMPLE_LINES)

    assert data.startswith((EXPECTED_HEADER + CSV_LINE_END).encode("utf-8"))
    assert list(report_export.CSV_COLUMNS) == EXPECTED_HEADER.split(",")


def test_csv_has_one_row_per_report_line_in_report_order():
    rows = SampleCsvRows()

    assert len(rows) == len(SAMPLE_LINES)
    assert [row[0] for row in rows] == [line.line_kind.value for line in SAMPLE_LINES]


def test_csv_of_an_empty_report_holds_only_the_header_row():
    assert report_export.RenderCsv([]) == (EXPECTED_HEADER + CSV_LINE_END).encode("utf-8")


def test_csv_amount_cells_are_plain_unguarded_decimals():
    amountIndex = EXPECTED_COLUMNS.index("amount")

    assert tuple(row[amountIndex] for row in SampleCsvRows()) == EXPECTED_AMOUNT_CELLS


@pytest.mark.parametrize(
    "lineIndex, column, expectedCell", GUARDED_CELL_CASES, ids=GUARDED_CELL_IDS
)
def test_csv_text_cells_starting_with_a_formula_prefix_are_guarded(lineIndex, column, expectedCell):
    assert SampleCsvCell(lineIndex, column) == expectedCell


@pytest.mark.parametrize("lineIndex, expectedRow", FULL_ROW_CASES, ids=FULL_ROW_IDS)
def test_csv_row_carries_kind_padded_category_and_empty_absent_fields(lineIndex, expectedRow):
    assert SampleCsvRows()[lineIndex] == expectedRow


@pytest.mark.parametrize("cellText, guardedText", GUARD_CASES, ids=GUARD_IDS)
def test_guard_csv_cell_prefixes_only_a_leading_formula_character(cellText, guardedText):
    assert report_export.GuardCsvCell(cellText) == guardedText


def test_pdf_is_a_pdf_document():
    assert report_export.RenderPdf(SAMPLE_LINES).startswith(b"%PDF")


def test_pdf_is_deterministic():
    assert report_export.RenderPdf(SAMPLE_LINES) == report_export.RenderPdf(SAMPLE_LINES)


def test_pdf_draws_in_courier():
    assert b"/Courier" in report_export.RenderPdf(SAMPLE_LINES)


def test_pdf_draws_every_whole_print_line_in_report_order():
    pdfPages = PdfPageLines(report_export.RenderPdf(SAMPLE_LINES))

    assert pdfPages == [[line.text for line in SAMPLE_LINES]]


@pytest.mark.parametrize("lineCount, pageSpans", PAGE_SPLIT_CASES, ids=PAGE_SPLIT_IDS)
def test_pdf_starts_a_new_page_only_when_a_page_is_full(lineCount, pageSpans):
    lines = NumberedDetailLines(lineCount)
    lineTexts = [line.text for line in lines]

    pdfPages = PdfPageLines(report_export.RenderPdf(lines))

    assert pdfPages == [lineTexts[start:stop] for start, stop in pageSpans]


@pytest.mark.parametrize("lineCount, linesReadAtEachPage", PAGE_BY_PAGE_CASES, ids=PAGE_BY_PAGE_IDS)
def test_pdf_sends_each_page_once_its_lines_are_read(monkeypatch, lineCount, linesReadAtEachPage):
    """Each page's content stream is sent after its own lines are read, before any later line.

    With one-byte chunks every piece of the file goes out alone, as soon as it is written.
    """
    monkeypatch.setattr(report_export, "REPORT_CHUNK_BYTES", 1)
    lines = NumberedDetailLines(lineCount)
    lineReader = LineReader(lines)
    chunks: list[bytes] = []
    linesReadAtContent: list[int] = []

    for chunk in report_export.IteratePdf(lineReader.Lines()):
        chunks.append(chunk)
        if PDF_STREAM_HEAD_PATTERN.search(chunk):
            linesReadAtContent.append(lineReader.linesTaken)

    assert tuple(linesReadAtContent) == linesReadAtEachPage
    pageSpans = itertools.pairwise((0, *linesReadAtEachPage))
    lineTexts = [line.text for line in lines]
    assert PdfPageLines(b"".join(chunks)) == [lineTexts[start:stop] for start, stop in pageSpans]


@pytest.mark.parametrize("lineCount, pageCount", PDF_STRUCTURE_CASES, ids=PDF_STRUCTURE_IDS)
def test_pdf_cross_reference_table_locates_every_object(lineCount, pageCount):
    """startxref finds the table, whose in-use entries each give the offset of their object.

    /Size counts the entries, and the page tree's /Count counts every page object.
    """
    pdfBytes = report_export.RenderPdf(NumberedDetailLines(lineCount))
    entries = PdfXrefEntries(pdfBytes)
    trailer = pdfBytes[pdfBytes.rindex(b"trailer") :]
    objects = PdfObjects(pdfBytes)

    tableSize = re.search(rb"/Size\s+(\d+)", trailer)
    assert tableSize is not None and int(tableSize.group(1)) == len(entries)
    assert entries[0] == (0, b"f")
    misplacedObjects = [
        objectNumber
        for objectNumber, (objectOffset, entryKind) in enumerate(entries[1:], start=1)
        if entryKind != b"n" or not pdfBytes.startswith(b"%d 0 obj" % objectNumber, objectOffset)
    ]
    assert misplacedObjects == []
    pageTree = objects[PdfReference(objects[PdfReference(trailer, b"Root")], b"Pages")]
    pageObjects = [body for body in objects.values() if PdfHasType(body, b"Page")]
    assert re.search(rb"/Count\s+%d\b" % len(pageObjects), pageTree)
    assert len(pageObjects) == pageCount


@pytest.mark.parametrize("lineCount, pageCount", PDF_STRUCTURE_CASES, ids=PDF_STRUCTURE_IDS)
def test_pdf_pages_define_the_courier_font_they_select_on_the_report_page_size(
    lineCount, pageCount
):
    """Each font a page's content selects is Courier in WinAnsiEncoding in its /Resources.

    Every /MediaBox is the page size the lines are drawn on, and the document is titled
    DALYREPT.
    """
    pdfBytes = report_export.RenderPdf(NumberedDetailLines(lineCount))
    trailer = pdfBytes[pdfBytes.rindex(b"trailer") :]
    objects = PdfObjects(pdfBytes)
    rootNodeNumber = PdfReference(objects[PdfReference(trailer, b"Root")], b"Pages")

    pages = PdfPageDictionaries(objects, rootNodeNumber)

    assert len(pages) == pageCount
    pageFontFaces = [PageFontFaces(objects, page) for page in pages]
    assert pageFontFaces == [EXPECTED_PAGE_FONT_FACES] * pageCount
    pageBox = (0.0, 0.0, *report_export.PDF_PAGE_SIZE)
    assert [PdfMediaBox(page) for page in pages] == [pageBox] * pageCount
    documentInfo = objects[PdfReference(trailer, b"Info")]
    assert re.search(rb"/Title\s*\(" + EXPECTED_TITLE + rb"\)", documentInfo)


def test_csv_and_pdf_exports_are_built_with_every_file_access_refused():
    with FilesystemDenied() as attempts:
        csvData = report_export.RenderCsv(SAMPLE_LINES)
        pdfData = report_export.RenderPdf(SAMPLE_LINES)

    assert attempts == []
    csvRows = ParseCsv(csvData)
    assert csvRows[0] == list(EXPECTED_COLUMNS)
    assert [row[0] for row in csvRows[1:]] == [line.line_kind.value for line in SAMPLE_LINES]
    assert PdfPageLines(pdfData) == [[line.text for line in SAMPLE_LINES]]


@pytest.mark.parametrize("accessDevnull", DEVNULL_ACCESS_CASES, ids=DEVNULL_ACCESS_IDS)
def test_filesystem_guard_refuses_and_records_each_file_entry_point(accessDevnull):
    with FilesystemDenied() as attempts, pytest.raises(PermissionError):
        accessDevnull()

    assert attempts == ["open"]
    assert FILESYSTEM_GUARD_ATTEMPTS.get() is None
