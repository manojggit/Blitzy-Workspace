"""Replaces: CBTRN03C TRANREPT print file (133 columns), copybook CVTRA07Y

The CR00 report renderers. CBTRN03C wrote its lines to the TRANREPT print file
(``proc/TRANREPT.prc``: LRECL=133, RECFM=FB); the report now renders on screen, and this module
renders the same lines, as ``cbtrn03c.BuildReport`` returns them or ``cbtrn03c.IterateReport``
yields them, as the screen's JSON, as CSV and as PDF. Nothing is rebuilt or re-totalled here.

``RenderJson`` writes a ``TransactionReportResponse`` with the lines as its ``lines`` member,
each line encoded as it is read, giving the bytes FastAPI's response-model serialisation would.
``RenderCsv`` writes one row per report line, every line kind included, with the structured
fields as columns. ``RenderPdf`` draws each 133-column print line unchanged in Courier, so the
PDF reproduces the TRANREPT print file. Each builds its output in memory and returns bytes: the
backend reads and writes no files, and the router sends the result. The module reads no
database, setting or clock and logs nothing.
"""

import csv
import io
import itertools
from collections.abc import Iterable, Iterator, Sequence
from decimal import Decimal

from pydantic import TypeAdapter
from reportlab.lib.pagesizes import landscape, letter
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

# 133 Courier columns at 8 pt take 133 x 4.8 = 638.4 pt, inside the 720 pt between the 36 pt
# margins of a landscape letter page.
PDF_FONT_NAME = "Courier"
PDF_FONT_SIZE = 8
PDF_LEADING = 10
PDF_MARGIN = 36
PDF_PAGE_SIZE = landscape(letter)
PDF_TITLE = "DALYREPT"

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


def RenderCsv(lines: Iterable[ReportLine]) -> bytes:
    """Render the report lines as a UTF-8 CSV document without a byte-order mark.

    The first row holds ``CSV_COLUMNS``. Each line then gives exactly one row, in report order
    and with every line kind included, so the CSV carries the line sequence of the printed
    report. Rows use the default dialect: commas, minimal quoting and CRLF line ends. Each row
    is encoded into the byte buffer as it is written, so no text copy of the whole document is
    held besides the bytes.

    Args:
        lines: The report lines from ``cbtrn03c.BuildReport`` or ``IterateReport``, in print
            order; they are read once.

    Returns:
        The CSV document, served with ``CSV_MEDIA_TYPE``.
    """
    csvBuffer = io.BytesIO()
    csvText = io.TextIOWrapper(csvBuffer, encoding=CSV_ENCODING, newline="")
    csvWriter = csv.writer(csvText)
    csvWriter.writerow(CSV_COLUMNS)
    csvWriter.writerows(CsvRow(line) for line in lines)
    # Flushes the encoded rows and leaves csvBuffer open, which closing the wrapper would not.
    csvText.detach()
    return csvBuffer.getvalue()


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


def RenderPdf(lines: Iterable[ReportLine]) -> bytes:
    """Render the report print lines as a PDF that reproduces the TRANREPT print file.

    The legacy page breaks, page totals and repeated headers are already in the lines, so the
    physical pages simply flow: a new page starts when the next line would cross the bottom
    margin. The canvas is invariant, so the same lines always give the same bytes, and page
    streams are left uncompressed, so the drawn text stays readable in the raw document. The
    lines of one page at a time are held, never the whole line sequence.

    Args:
        lines: The report lines from ``cbtrn03c.BuildReport`` or ``IterateReport``, in print
            order; they are read once.

    Returns:
        The PDF document, served with ``PDF_MEDIA_TYPE``.
    """
    pdfBuffer = io.BytesIO()
    pdfCanvas = Canvas(pdfBuffer, pagesize=PDF_PAGE_SIZE, invariant=1, pageCompression=0)
    pdfCanvas.setTitle(PDF_TITLE)
    for pageLines in ChunkLines(lines, LinesPerPage()):
        DrawPage(pdfCanvas, pageLines)
        pdfCanvas.showPage()
    pdfCanvas.save()
    return pdfBuffer.getvalue()


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


def WriteJsonLines(jsonBuffer: io.BytesIO, lines: Iterable[ReportLine]) -> None:
    """Write the JSON of each line into ``jsonBuffer`` as it is read, separated by commas."""
    separator = b""
    for line in lines:
        jsonBuffer.write(separator)
        jsonBuffer.write(REPORT_LINE_ADAPTER.dump_json(line, by_alias=True))
        separator = JSON_LINE_SEPARATOR


def RenderJson(envelope: TransactionReportResponse, lines: Iterable[ReportLine]) -> bytes:
    """Render the CR00 screen: ``envelope`` as JSON with ``lines`` as its ``lines`` member.

    The bytes equal ``REPORT_RESPONSE_ADAPTER.dump_json`` of ``envelope`` holding the lines,
    which is what FastAPI sends for the ``TransactionReportResponse`` response model. Each line
    is encoded into the byte buffer as it is read and then dropped, and ``getvalue`` hands over
    the finished buffer without copying it, so neither the line list nor a second copy of the
    document is ever held.

    Args:
        envelope: The screen without lines: the submitted, prompt or cleared response.
        lines: The report lines from ``cbtrn03c.BuildReport`` or ``IterateReport``, in print
            order; they are read once.

    Returns:
        The JSON document, served with ``JSON_MEDIA_TYPE``.

    Raises:
        ReportJsonEnvelopeError: As ``SplitJsonEnvelope``.
    """
    headJson, tailJson = SplitJsonEnvelope(envelope)
    jsonBuffer = io.BytesIO()
    jsonBuffer.write(headJson)
    jsonBuffer.write(LINES_MEMBER_OPEN)
    WriteJsonLines(jsonBuffer, lines)
    jsonBuffer.write(LINES_MEMBER_CLOSE)
    jsonBuffer.write(tailJson)
    return jsonBuffer.getvalue()
