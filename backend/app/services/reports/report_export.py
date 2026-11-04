"""Replaces: CBTRN03C TRANREPT print file (133 columns), copybook CVTRA07Y

The CR00 report renderers. CBTRN03C wrote its lines to the TRANREPT print file
(``proc/TRANREPT.prc``: LRECL=133, RECFM=FB); the report now renders on screen, and this module
renders the same lines, as ``cbtrn03c.BuildReport`` returns them or ``cbtrn03c.IterateReport``
yields them, as the screen's JSON, as CSV and as PDF. Nothing is rebuilt or re-totalled here.

``IterateJson`` writes a ``TransactionReportResponse`` with the lines as its ``lines`` member,
each line encoded as it is read, giving the bytes FastAPI's response-model serialisation would.
``IterateCsv`` writes one row per report line, every line kind included, with the structured
fields as columns. ``IteratePdf`` draws each 133-column print line unchanged in Courier, so the
PDF reproduces the TRANREPT print file. Each is a generator of the document's chunks, built in
memory as the lines are read, so the router streams them as they come: the backend reads and
writes no files. reportlab formats a PDF file only when a whole document is saved, so for the
PDF it only draws each page, and this module writes the file around the pages itself, page by
page. ``RenderJson``, ``RenderCsv`` and ``RenderPdf`` join the same chunks into one document.
The module reads no database, setting or clock and logs nothing.
"""

import array
import csv
import io
import itertools
from collections.abc import Iterable, Iterator, Sequence
from decimal import Decimal

from pydantic import TypeAdapter
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.rl_accel import escapePDF, fp_str
from reportlab.pdfbase.pdfdoc import pdfdocEnc
from reportlab.pdfgen.canvas import Canvas

from app.schemas.reports import ReportLine, TransactionReportResponse

CSV_COLUMNS = (
    "line_kind",
    "tran_id",
    "acct_id",
    "type_cd",
    "type_desc",
    "cat_cd",
    "cat_desc",
    "source",
    "amount",
)
CSV_AMOUNT_COLUMN = "amount"
CSV_TEXT_COLUMNS = tuple(column for column in CSV_COLUMNS if column != CSV_AMOUNT_COLUMN)
# TRAN-REPORT-CAT-CD PIC 9(04): the CSV shows the code zero-padded, as the print line does.
CSV_CAT_CD_COLUMN = "cat_cd"
CAT_CD_DIGITS = 4
CSV_ENCODING = "utf-8"
# A spreadsheet runs a cell starting with one of these as a formula; the apostrophe makes it text.
FORMULA_PREFIXES = ("=", "+", "-", "@")
FORMULA_GUARD = "'"

CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
PDF_MEDIA_TYPE = "application/pdf"

# The size of the chunks a document is sent in. The CSV, JSON and PDF generators join the pieces
# they write, one row, one line, or one PDF object or cross-reference entry each, into chunks of
# at least this many bytes, so a document goes out in a few hundred writes rather than one per
# line, while a chunk in hand stays small beside the report.
REPORT_CHUNK_BYTES = 16 * 1024

# One chunk of a document, as every renderer yields it: encoded bytes.
type ReportChunk = bytes

# 133 Courier columns at 8 pt take 133 x 4.8 = 638.4 pt, inside the 720 pt between the 36 pt
# margins of a landscape letter page.
PDF_FONT_NAME = "Courier"
PDF_FONT_SIZE = 8
PDF_LEADING = 10
PDF_MARGIN = 36
PDF_PAGE_SIZE = landscape(letter)
PDF_TITLE = "DALYREPT"

# The PDF objects have fixed numbers, because the file is written around pages drawn one at a
# time: the catalog, the page tree, the document information and the one font take the first
# four. The page tree is written last, once the page count is known, but every page names it as
# its parent. Page n, counting from 0, is its content stream, object PDF_FIRST_PAGE_OBJECT + 2n,
# then its page dictionary, the next object.
PDF_CATALOG_OBJECT = 1
PDF_PAGES_OBJECT = 2
PDF_INFO_OBJECT = 3
PDF_FONT_OBJECT = 4
PDF_FIRST_PAGE_OBJECT = 5
PDF_OBJECTS_PER_PAGE = 2
# A page canvas whose initial font is PDF_FONT_NAME registers that font first and alone, and
# reportlab names the first font a canvas registers F1, so every page's content selects the
# report font as /F1 (test_report_export checks that each selected font is the one defined).
PDF_FONT_RESOURCE = "F1"
# The encoding reportlab writes the standard fonts' text in: Windows code page 1252.
PDF_FONT_ENCODING = "WinAnsiEncoding"
# PDF 1.3, then a comment of bytes above 127 that marks the file as binary.
PDF_HEADER = b"%PDF-1.3\n%\x93\x8c\x8b\x9e\n"
PDF_CATALOG = b"<< /Type /Catalog /Pages %d 0 R >>" % PDF_PAGES_OBJECT
PDF_INFO = b"<< /Title (%s) >>" % pdfdocEnc(escapePDF(PDF_TITLE))
PDF_FONT = b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /%s /Name /%s >>" % (
    pdfdocEnc(PDF_FONT_NAME),
    pdfdocEnc(PDF_FONT_ENCODING),
    pdfdocEnc(PDF_FONT_RESOURCE),
)
PDF_MEDIA_BOX = pdfdocEnc(fp_str(0, 0, *PDF_PAGE_SIZE))
# Entry 0 of the cross-reference table heads the list of free objects.
PDF_XREF_FREE_HEAD = b"0000000000 65535 f \n"

JSON_MEDIA_TYPE = "application/json"
# FastAPI sends a response model as its TypeAdapter's dump_json by alias, compact and with
# nothing excluded; these adapters write the screen JSON the same way, a part at a time.
REPORT_RESPONSE_ADAPTER = TypeAdapter(TransactionReportResponse)
REPORT_LINE_ADAPTER = TypeAdapter(ReportLine)
# ``lines`` is the last TransactionReportResponse field and has no alias. A JSON string escapes
# every quote it holds, so these bytes can occur only as the member itself.
EMPTY_LINES_MEMBER = b'"lines":[]'
LINES_MEMBER_OPEN = b'"lines":['
LINES_MEMBER_CLOSE = b"]"
JSON_LINE_SEPARATOR = b","


def GuardCsvCell(text: str) -> str:
    """Return text prefixed with an apostrophe when a spreadsheet would run it as a formula.

    Descriptions and sources keyed online in CT02 reach the report as entered, so a text cell
    starting with ``=``, ``+``, ``-`` or ``@`` gets ``'`` in front. Amounts are never guarded:
    a negative amount such as ``-12.34`` is numeric data.

    Args:
        text: The text of one CSV cell.

    Returns:
        ``'`` plus text when text starts with a formula prefix, otherwise text unchanged.
    """
    if text.startswith(FORMULA_PREFIXES):
        return FORMULA_GUARD + text
    return text


def CsvTextCell(line: ReportLine, column: str) -> str:
    """Return the text of one non-amount column of line, before the formula guard.

    ``line_kind`` gives its enum value, such as ``detail``; ``cat_cd`` gives the 4-digit code;
    an absent value gives an empty cell.
    """
    value = getattr(line, column)
    if value is None:
        return ""
    if column == CSV_CAT_CD_COLUMN:
        return f"{value:0{CAT_CD_DIGITS}d}"
    return str(value)


def CsvAmountCell(amount: Decimal | None) -> str:
    """Return amount as a plain two-place decimal, such as ``-12.34``, or empty when absent.

    The text never uses exponent form, the edited picture of the print line or a float.
    """
    if amount is None:
        return ""
    return f"{amount:.2f}"


def CsvRow(line: ReportLine) -> list[str]:
    """Return the CSV cells of one report line, in ``CSV_COLUMNS`` order.

    Every text cell passes through ``GuardCsvCell``; the amount does not.
    """
    textCells = [GuardCsvCell(CsvTextCell(line, column)) for column in CSV_TEXT_COLUMNS]
    return [*textCells, CsvAmountCell(line.amount)]


def GroupChunks(pieces: Iterable[bytes]) -> Iterator[bytes]:
    """Yield pieces, in order, joined into chunks of at least REPORT_CHUNK_BYTES each.

    Only the last chunk may be shorter; no pieces give no chunk. Only the pieces of the chunk
    being filled are held.
    """
    pendingPieces: list[bytes] = []
    pendingBytes = 0
    for piece in pieces:
        pendingPieces.append(piece)
        pendingBytes += len(piece)
        if pendingBytes >= REPORT_CHUNK_BYTES:
            yield b"".join(pendingPieces)
            pendingPieces = []
            pendingBytes = 0
    if pendingPieces:
        yield b"".join(pendingPieces)


def CsvPieces(lines: Iterable[ReportLine]) -> Iterator[bytes]:
    """Yield the encoded ``CSV_COLUMNS`` row, then one encoded row per line as it is read.

    Each row is written into a one-row text buffer, encoded and then cleared, so no text copy
    of the document is held.
    """
    rowText = io.StringIO(newline="")
    csvWriter = csv.writer(rowText)
    for row in itertools.chain((CSV_COLUMNS,), map(CsvRow, lines)):
        csvWriter.writerow(row)
        yield rowText.getvalue().encode(CSV_ENCODING)
        rowText.seek(0)
        rowText.truncate()


def IterateCsv(lines: Iterable[ReportLine]) -> Iterator[bytes]:
    """Yield the UTF-8 CSV document of the report lines, without a byte-order mark, in chunks.

    The first row holds ``CSV_COLUMNS``. Each line then gives exactly one row, in report order
    and with every line kind included, so the CSV carries the line sequence of the printed
    report. Rows use the default dialect: commas, minimal quoting and CRLF line ends. A chunk is
    yielded as soon as its rows are written (``GroupChunks``).

    Args:
        lines: The report lines from ``cbtrn03c.BuildReport`` or ``IterateReport``, in print
            order; they are read once, as the chunks are taken.

    Returns:
        The chunks of the CSV document, served with ``CSV_MEDIA_TYPE``.
    """
    return GroupChunks(CsvPieces(lines))


def RenderCsv(lines: Iterable[ReportLine]) -> bytes:
    """Return the whole CSV document ``IterateCsv`` yields for the report lines."""
    return b"".join(IterateCsv(lines))


def LinesPerPage() -> int:
    """Return how many print lines fit on one PDF page between the top and bottom margins.

    The first baseline sits one font size below the top margin and each line is
    ``PDF_LEADING`` below the one before; the last baseline may not cross the bottom margin.
    """
    pageHeight = PDF_PAGE_SIZE[1]
    baselineSpan = pageHeight - 2 * PDF_MARGIN - PDF_FONT_SIZE
    return int(baselineSpan // PDF_LEADING) + 1


def ChunkLines(lines: Iterable[ReportLine], size: int) -> Iterator[Sequence[ReportLine]]:
    """Yield consecutive pages of at most size lines each, reading lines once.

    No lines still give one empty page, so an empty report renders as a valid one-page PDF.
    """
    pages = itertools.batched(lines, size)
    yield next(pages, ())
    yield from pages


def DrawPage(pdfCanvas: Canvas, pageLines: Sequence[ReportLine]) -> None:
    """Draw the print text of pageLines top to bottom on the current page of pdfCanvas.

    Each line's 133-column ``text`` is drawn unchanged in Courier, so the columns of the
    CVTRA07Y layouts stay aligned. The first baseline sits one font size below the top margin,
    which keeps the first line's glyphs inside the margin.
    """
    firstBaseline = PDF_PAGE_SIZE[1] - PDF_MARGIN - PDF_FONT_SIZE
    textObject = pdfCanvas.beginText(PDF_MARGIN, firstBaseline)
    textObject.setFont(PDF_FONT_NAME, PDF_FONT_SIZE, leading=PDF_LEADING)
    for line in pageLines:
        textObject.textLine(line.text)
    pdfCanvas.drawText(textObject)


def DrawPageContent(pageLines: Sequence[ReportLine]) -> bytes:
    """Return the content stream data of one PDF page that draws pageLines.

    reportlab draws the page on a canvas of its own whose initial font is the report font, so
    the content selects it as PDF_FONT_RESOURCE. The canvas is drawn on and never saved, and
    its content is encoded as reportlab encodes a page stream, uncompressed.
    """
    pageCanvas = Canvas(
        None,
        pagesize=PDF_PAGE_SIZE,
        invariant=1,
        pageCompression=0,
        initialFontName=PDF_FONT_NAME,
    )
    DrawPage(pageCanvas, pageLines)
    return pdfdocEnc(pageCanvas.getCurrentPageContent())


class PdfLayout:
    """How many bytes of a PDF have been written, and where each object written starts.

    ``objectOffsets[n - 1]`` is the offset of object n, eight bytes per object: all the file
    keeps of the pages already written, for its cross-reference table.
    """

    def __init__(self) -> None:
        self.byteCount = 0
        self.objectOffsets = array.array("q")

    def Write(self, piece: bytes) -> bytes:
        """Return piece, counted as the next bytes of the file."""
        self.byteCount += len(piece)
        return piece

    def WriteObject(self, objectNumber: int, body: bytes) -> bytes:
        """Return indirect object objectNumber holding body, recording where it starts."""
        missingSlots = objectNumber - len(self.objectOffsets)
        self.objectOffsets.extend(itertools.repeat(0, missingSlots))
        self.objectOffsets[objectNumber - 1] = self.byteCount
        return self.Write(b"%d 0 obj\n%s\nendobj\n" % (objectNumber, body))


def PdfPage(contentObject: int) -> bytes:
    """Return the dictionary of the page whose content stream is object contentObject."""
    return (
        b"<< /Type /Page /Parent %d 0 R /MediaBox [ %s ] /Resources << /Font << /%s %d 0 R >>"
        b" /ProcSet [ /PDF /Text ] >> /Contents %d 0 R >>"
        % (
            PDF_PAGES_OBJECT,
            PDF_MEDIA_BOX,
            pdfdocEnc(PDF_FONT_RESOURCE),
            PDF_FONT_OBJECT,
            contentObject,
        )
    )


def PdfPageObjects(
    layout: PdfLayout, pageIndex: int, pageLines: Sequence[ReportLine]
) -> Iterator[bytes]:
    """Yield page pageIndex, from 0, drawing pageLines: its content stream, then its dictionary."""
    contentObject = PDF_FIRST_PAGE_OBJECT + PDF_OBJECTS_PER_PAGE * pageIndex
    content = DrawPageContent(pageLines)
    contentStream = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content)
    yield layout.WriteObject(contentObject, contentStream)
    yield layout.WriteObject(contentObject + 1, PdfPage(contentObject))


def PdfPageTree(pageCount: int) -> bytes:
    """Return the page tree of pageCount pages, their dictionaries in page order."""
    pageObjects = range(
        PDF_FIRST_PAGE_OBJECT + 1,
        PDF_FIRST_PAGE_OBJECT + PDF_OBJECTS_PER_PAGE * pageCount,
        PDF_OBJECTS_PER_PAGE,
    )
    kids = b" ".join(b"%d 0 R" % pageObject for pageObject in pageObjects)
    return b"<< /Type /Pages /Count %d /Kids [ %s ] >>" % (pageCount, kids)


def PdfCrossReference(layout: PdfLayout) -> Iterator[bytes]:
    """Yield the cross-reference table of every object layout has written, then the trailer.

    Entry n gives the offset of object n; ``startxref`` gives the table's own offset.
    """
    xrefOffset = layout.byteCount
    tableSize = len(layout.objectOffsets) + 1
    yield layout.Write(b"xref\n0 %d\n" % tableSize + PDF_XREF_FREE_HEAD)
    for objectOffset in layout.objectOffsets:
        yield layout.Write(b"%010d 00000 n \n" % objectOffset)
    yield layout.Write(
        b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (tableSize, PDF_CATALOG_OBJECT, PDF_INFO_OBJECT, xrefOffset)
    )


def PdfPieces(lines: Iterable[ReportLine]) -> Iterator[bytes]:
    """Yield the PDF file of the report print lines piece by piece, each page once drawn.

    The header, the catalog, the document information and the font come first, then each
    page's content stream and dictionary, then the page tree, the cross-reference table and
    the trailer, which need the page count and the object offsets.
    """
    layout = PdfLayout()
    yield layout.Write(PDF_HEADER)
    yield layout.WriteObject(PDF_CATALOG_OBJECT, PDF_CATALOG)
    yield layout.WriteObject(PDF_INFO_OBJECT, PDF_INFO)
    yield layout.WriteObject(PDF_FONT_OBJECT, PDF_FONT)
    pageCount = 0
    for pageLines in ChunkLines(lines, LinesPerPage()):
        yield from PdfPageObjects(layout, pageCount, pageLines)
        pageCount += 1
    yield layout.WriteObject(PDF_PAGES_OBJECT, PdfPageTree(pageCount))
    yield from PdfCrossReference(layout)


def IteratePdf(lines: Iterable[ReportLine]) -> Iterator[bytes]:
    """Yield the PDF of the report print lines in chunks, each as soon as its pages are drawn.

    The legacy page breaks, page totals and repeated headers are already in the lines, so the
    physical pages simply flow: a new page starts when the next line would cross the bottom
    margin. reportlab formats a PDF file only when a whole document is saved, so here it only
    draws each page (``DrawPageContent``), and ``PdfPieces`` writes the file around the pages:
    each page's objects once it is drawn, then the page tree, the cross-reference table and the
    trailer. Page streams are uncompressed, so the drawn text stays readable in the raw
    document, and nothing depends on the clock, so the same lines always give the same bytes.
    One page's lines and content are held at a time, plus eight bytes per object for the
    cross-reference table, and a chunk is yielded as soon as its pieces are written
    (``GroupChunks``). No file is written.

    Args:
        lines: The report lines from ``cbtrn03c.BuildReport`` or ``IterateReport``, in print
            order; they are read once, a page at a time, as the chunks are taken.

    Returns:
        The chunks of the PDF document, served with ``PDF_MEDIA_TYPE``.
    """
    return GroupChunks(PdfPieces(lines))


def RenderPdf(lines: Iterable[ReportLine]) -> bytes:
    """Return the whole PDF document ``IteratePdf`` yields for the report print lines."""
    return b"".join(IteratePdf(lines))


class ReportJsonEnvelopeError(ValueError):
    """The JSON of a line-less TransactionReportResponse lacks one empty ``lines`` member."""


def SplitJsonEnvelope(envelope: TransactionReportResponse) -> tuple[bytes, bytes]:
    """Return the JSON of the line-less ``envelope`` before and after its empty ``lines`` member.

    Raises:
        ReportJsonEnvelopeError: ``EMPTY_LINES_MEMBER`` does not occur exactly once, as when
            ``envelope`` already carries lines.
    """
    envelopeJson = REPORT_RESPONSE_ADAPTER.dump_json(envelope, by_alias=True)
    memberCount = envelopeJson.count(EMPTY_LINES_MEMBER)
    if memberCount != 1:
        raise ReportJsonEnvelopeError(
            f"expected one empty lines member in the report JSON, found {memberCount}"
        )
    headJson, tailJson = envelopeJson.split(EMPTY_LINES_MEMBER)
    return headJson, tailJson


def JsonPieces(headJson: bytes, lines: Iterable[ReportLine], tailJson: bytes) -> Iterator[bytes]:
    """Yield the screen JSON as pieces: headJson, then the ``lines`` member, then tailJson.

    Each line is encoded as it is read, and the lines are separated by commas.
    """
    yield headJson + LINES_MEMBER_OPEN
    separator = b""
    for line in lines:
        yield separator
        yield REPORT_LINE_ADAPTER.dump_json(line, by_alias=True)
        separator = JSON_LINE_SEPARATOR
    yield LINES_MEMBER_CLOSE + tailJson


def IterateJson(
    envelope: TransactionReportResponse, lines: Iterable[ReportLine]
) -> Iterator[bytes]:
    """Yield the CR00 screen in chunks: ``envelope`` as JSON with ``lines`` as its ``lines``.

    The joined chunks equal ``REPORT_RESPONSE_ADAPTER.dump_json`` of ``envelope`` holding the
    lines, which is what FastAPI sends for the ``TransactionReportResponse`` response model.
    Each line is encoded as it is read and dropped once its chunk is yielded
    (``GroupChunks``), so neither the line list nor the whole document is ever held. The
    envelope is split at once, so a malformed one fails before any chunk is taken.

    Args:
        envelope: The screen without lines: the submitted, prompt or cleared response.
        lines: The report lines from ``cbtrn03c.BuildReport`` or ``IterateReport``, in print
            order; they are read once, as the chunks are taken.

    Returns:
        The chunks of the JSON document, served with ``JSON_MEDIA_TYPE``.

    Raises:
        ReportJsonEnvelopeError: As ``SplitJsonEnvelope``.
    """
    headJson, tailJson = SplitJsonEnvelope(envelope)
    return GroupChunks(JsonPieces(headJson, lines, tailJson))


def RenderJson(envelope: TransactionReportResponse, lines: Iterable[ReportLine]) -> bytes:
    """Return the whole screen JSON ``IterateJson`` yields for ``envelope`` and ``lines``.

    Raises:
        ReportJsonEnvelopeError: As ``SplitJsonEnvelope``.
    """
    return b"".join(IterateJson(envelope, lines))
