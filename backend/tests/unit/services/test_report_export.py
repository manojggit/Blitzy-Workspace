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
- PDF: every 133-column print line drawn unchanged in Courier, as TRANREPT printed it; the
    document is deterministic and uncompressed, so each line's text is readable in the raw
    bytes as a PDF literal string.
"""

import csv
import io
import re
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


def ParseCsv(data: bytes) -> list[list[str]]:
    """Return every row of the UTF-8 CSV document data, header row included."""
    return list(csv.reader(io.StringIO(data.decode("utf-8"))))


def SampleCsvRows() -> list[list[str]]:
    """Return the data rows of the SAMPLE_LINES CSV, header row dropped."""
    return ParseCsv(report_export.RenderCsv(SAMPLE_LINES))[1:]


def SampleCsvCell(lineIndex: int, column: str) -> str:
    """Return the CSV cell of column for the SAMPLE_LINES entry at lineIndex."""
    return SampleCsvRows()[lineIndex][EXPECTED_COLUMNS.index(column)]


def EscapePdfText(text: str) -> bytes:
    """Return text as a PDF literal string body: backslash first, then both parentheses."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return escaped.encode("ascii")


def PdfPageCount(pdfBytes: bytes) -> int:
    """Return the /Count of the page tree of the PDF document pdfBytes."""
    pageCount = re.search(rb"/Count (\d+)", pdfBytes)
    assert pageCount is not None
    return int(pageCount.group(1))


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
    pdfBytes = report_export.RenderPdf(SAMPLE_LINES)
    literals = [b"(" + EscapePdfText(line.text) + b")" for line in SAMPLE_LINES]
    positions = [pdfBytes.find(literal) for literal in literals]

    assert -1 not in positions
    assert positions == sorted(positions)


@pytest.mark.parametrize(
    "lineCount, pageCount",
    (
        (0, 1),
        (report_export.LinesPerPage(), 1),
        (report_export.LinesPerPage() + 1, 2),
    ),
    ids=("empty-report", "full-page", "one-line-over"),
)
def test_pdf_starts_a_new_page_only_when_a_page_is_full(lineCount, pageCount):
    lines = [SAMPLE_LINES[PURCHASE_LINE_INDEX]] * lineCount

    assert PdfPageCount(report_export.RenderPdf(lines)) == pageCount
