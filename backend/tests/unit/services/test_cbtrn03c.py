"""Unit tests for app.services.reports.cbtrn03c, the CBTRN03C transaction report of TRANREPT.

cbl/CBTRN03C.cbl, run by proc/TRANREPT.prc, prints the TRANREPT file in the CVTRA07Y line
layouts from TRAN-RECORD (CVTRA05Y) input that the procedure has already filtered by date and
sorted by card. BuildReport is pure, so these tests call it directly with no database. Expected
lines and totals are typed from the cited COBOL and worked by hand with Decimal literals, or read
from the reconciliation oracle's tranrept_lines.txt; none is taken from the module:
- lines 127-137: WS-LINE-COUNTER starts at 0 and WS-PAGE-SIZE is 20;
- lines 274-290: 1100-WRITE-TRANSACTION-REPORT writes the headers on the first record, then,
    whenever MOD(WS-LINE-COUNTER, 20) = 0 before a detail, the page totals and the headers, and
    then adds TRAN-AMT to the page and account totals and writes the detail;
- lines 293-341: page and account totals each write their line and a dash rule, the headers
    write four lines, and all of them are counted; the grand total (lines 318-322) is not;
- lines 181-188: on a change of card the previous account total is written, never before the
    first card;
- lines 197-203: at end of file the last TRAN-AMT is added a second time, the page and grand
    totals follow, and the final account total is never written;
- lines 170-178: empty input meets end of file on the first READ and writes nothing;
- lines 484-512 and 626-630: a card, type or category key not on file abends with code 999;
- CVTRA07Y: -ZZZ,ZZZ,ZZZ.ZZ on detail lines and +ZZZ,ZZZ,ZZZ.ZZ on total lines, 133 columns.
"""

import csv
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.paths import REPO_ROOT
from app.schemas.reports import ReportLine, ReportLineKind
from app.services.reports.cbtrn03c import BuildReport, ReportAbendError, ReportRequestData
from app.utils.record_layouts import TRANSACTION_LAYOUT, DecodeRecord

# FD-REPTFILE-REC PIC X(133): every TRANREPT record is padded with spaces to this width.
REPORT_WIDTH = 133
# MOVE 999 TO ABCODE in 9999-ABEND-PROGRAM (CBTRN03C lines 626-630).
ABEND_CODE = 999

HEADER = ReportLineKind.HEADER
DETAIL = ReportLineKind.DETAIL
ACCOUNT_TOTAL = ReportLineKind.ACCOUNT_TOTAL
PAGE_TOTAL = ReportLineKind.PAGE_TOTAL
GRAND_TOTAL = ReportLineKind.GRAND_TOTAL

QUERY = ReportRequestData("2022-01-01", "2022-12-31")
# Sample cards and their accounts from the expected xref.card_xref rows (loader_rows).
CARD_A = "2988091353094312"
ACCOUNT_A = "00000000004"
CARD_B = "0683586198171516"
ACCOUNT_B = "00000000027"
FIRST_TRAN_ID = "0000000000000001"

# The TRANTYPE and TRANCATG lookups of every row BuildRow makes.
TYPE_DESCRIPTIONS = {"01": "Purchase"}
CATEGORY_DESCRIPTIONS = {("01", 1): "Regular Sales Draft"}

# TRANSACTION-HEADER-2 PIC X(133) VALUE ALL '-', and WS-BLANK-LINE PIC X(133) VALUE SPACES.
DASH_LINE = "-" * REPORT_WIDTH
BLANK_LINE = " " * REPORT_WIDTH
# TRANSACTION-HEADER-1: the six headings in X(17), X(12), X(19), X(35), X(14), X, X(16).
COLUMN_HEADINGS = (
    "Transaction ID".ljust(17)
    + "Account ID".ljust(12)
    + "Transaction Type".ljust(19)
    + "Tran Category".ljust(35)
    + "Tran Source".ljust(14)
    + " "
    + "        Amount".ljust(16)
).ljust(REPORT_WIDTH)
# The label and dot FILLERs in front of each +ZZZ,ZZZ,ZZZ.ZZ total field.
PAGE_TOTAL_PREFIX = "Page Total".ljust(11) + "." * 86
ACCOUNT_TOTAL_PREFIX = "Account Total".ljust(13) + "." * 84
GRAND_TOTAL_PREFIX = "Grand Total".ljust(11) + "." * 86

# The sample-report parity inputs and the oracle's expected TRANREPT lines.
EXPECTED_DIR = REPO_ROOT / "batch" / "tests" / "fixtures" / "expected"
COMBINED_PATH = EXPECTED_DIR / "combtran_combined.dat"
CARD_XREF_PATH = EXPECTED_DIR / "loader_rows" / "card_xref.csv"
TRANSACTION_TYPES_PATH = EXPECTED_DIR / "loader_rows" / "transaction_types.csv"
TRANSACTION_CATEGORIES_PATH = EXPECTED_DIR / "loader_rows" / "transaction_categories.csv"
TRANREPT_LINES_PATH = EXPECTED_DIR / "tranrept_lines.txt"
SAMPLE_QUERY = ReportRequestData("2022-07-18", "2022-07-18")
SAMPLE_START_DATE = date(2022, 7, 18)
SAMPLE_END_DATE = date(2022, 7, 18)
# TRANREPT sorts by TRAN-CARD-NUM over TRANSACT key order, both EBCDIC byte orders.
EBCDIC_CODEC = "cp037"


def BuildRow(tranId: str, cardNum: str, amount: str) -> dict[str, object]:
    """One report input row: a POS TERM purchase (type 01, category 1) on account 4."""
    return {
        "tran_id": tranId,
        "card_num": cardNum,
        "tran_type_cd": "01",
        "tran_cat_cd": 1,
        "tran_source": "POS TERM",
        "tran_amt": Decimal(amount),
        "xref_acct_id": ACCOUNT_A,
    }


def BuildCardRows(cardNum: str, amounts: Sequence[str]) -> list[dict[str, object]]:
    """BuildRow for each amount on one card, with TRAN-IDs numbered from 1 in key order."""
    return [
        BuildRow(f"{sequence:016d}", cardNum, amount)
        for sequence, amount in enumerate(amounts, start=1)
    ]


def KindsOf(lines: Sequence[ReportLine]) -> list[str]:
    """The line kind of every report line, in print order."""
    return [line.line_kind for line in lines]


def RunReport(rows: Sequence[Mapping[str, object]]) -> list[ReportLine]:
    """BuildReport over rows with QUERY and the BuildRow type and category lookups."""
    return BuildReport(QUERY, rows, TYPE_DESCRIPTIONS, CATEGORY_DESCRIPTIONS)


def HeaderTexts(query: ReportRequestData) -> list[str]:
    """The four 1120-WRITE-HEADERS lines: REPORT-NAME-HEADER, blank, HEADER-1 and HEADER-2."""
    nameHeader = (
        "DALYREPT".ljust(38)
        + "Daily Transaction Report".ljust(41)
        + "Date Range: "
        + query.startDate
        + " to "
        + query.endDate
    )
    return [nameHeader.ljust(REPORT_WIDTH), BLANK_LINE, COLUMN_HEADINGS, DASH_LINE]


def TotalText(prefix: str, editedAmount: str) -> str:
    """A total line: its label and dot FILLERs, then the edited +ZZZ,ZZZ,ZZZ.ZZ amount."""
    return (prefix + editedAmount).ljust(REPORT_WIDTH)


def DetailText(tranId: str, acctId: str, editedAmount: str) -> str:
    """TRANSACTION-DETAIL-REPORT for a BuildRow row, with its edited -ZZZ,ZZZ,ZZZ.ZZ amount."""
    return (
        tranId
        + " "
        + acctId
        + " 01-"
        + "Purchase".ljust(15)
        + " 0001-"
        + "Regular Sales Draft".ljust(29)
        + " "
        + "POS TERM".ljust(10)
        + " " * 4
        + editedAmount
        + " " * 2
    ).ljust(REPORT_WIDTH)


def test_one_card_prints_headers_details_and_totals_with_last_amount_added_twice() -> None:
    lines = RunReport(BuildCardRows(CARD_A, ["1.00", "1.00", "1.00"]))

    assert KindsOf(lines) == [HEADER] * 4 + [DETAIL] * 3 + [PAGE_TOTAL, HEADER, GRAND_TOTAL]
    assert [line.text for line in lines[:4]] == HeaderTexts(QUERY)
    # 1.00 + 1.00 + 1.00, plus the last 1.00 again at end of file; no account total is written.
    assert lines[7].amount == Decimal("4.00")
    assert lines[7].text == TotalText(PAGE_TOTAL_PREFIX, "+          4.00")
    assert lines[8].text == DASH_LINE
    assert lines[9].amount == Decimal("4.00")
    assert lines[9].text == TotalText(GRAND_TOTAL_PREFIX, "+          4.00")


def test_detail_line_carries_the_printed_fields_in_133_columns() -> None:
    lines = RunReport([BuildRow(FIRST_TRAN_ID, CARD_A, "1.00")])
    detail = lines[4]

    assert detail.line_kind == DETAIL
    assert (detail.tran_id, detail.acct_id, detail.type_cd) == (FIRST_TRAN_ID, ACCOUNT_A, "01")
    assert (detail.cat_cd, detail.source, detail.amount) == (1, "POS TERM", Decimal("1.00"))
    assert (detail.type_desc, detail.cat_desc) == ("Purchase", "Regular Sales Draft")
    # A positive amount leaves the fixed '-' sign position blank.
    assert detail.text == DetailText(FIRST_TRAN_ID, ACCOUNT_A, "           1.00")
    assert all(len(line.text) == REPORT_WIDTH for line in lines)


def test_card_change_writes_the_previous_account_total_but_never_the_last() -> None:
    cardBRow = {**BuildRow("0000000000000003", CARD_B, "5.00"), "xref_acct_id": ACCOUNT_B}
    lines = RunReport([*BuildCardRows(CARD_A, ["1.00", "1.00"]), cardBRow])

    cardADetails = [HEADER] * 4 + [DETAIL, DETAIL, ACCOUNT_TOTAL, HEADER]
    assert KindsOf(lines) == cardADetails + [DETAIL, PAGE_TOTAL, HEADER, GRAND_TOTAL]
    assert lines[6].amount == Decimal("2.00")
    assert lines[6].text == TotalText(ACCOUNT_TOTAL_PREFIX, "+          2.00")
    assert lines[7].text == DASH_LINE
    assert [lines[4].acct_id, lines[5].acct_id, lines[8].acct_id] == [
        ACCOUNT_A,
        ACCOUNT_A,
        ACCOUNT_B,
    ]
    # 1.00 + 1.00 + 5.00, plus the last 5.00 again at end of file.
    assert lines[9].text == TotalText(PAGE_TOTAL_PREFIX, "+         12.00")
    assert lines[11].text == TotalText(GRAND_TOTAL_PREFIX, "+         12.00")


def test_page_totals_and_headers_repeat_when_the_line_counter_reaches_twenty() -> None:
    lines = RunReport(BuildCardRows(CARD_A, ["1.00"] * 25))

    # Headers are counted lines 1-4 and details 1-16 are lines 5-20, so before detail 17 the
    # counter is 20: page total and dash (21, 22), headers (23-26), then details 17-25 (27-35).
    firstPage = [HEADER] * 4 + [DETAIL] * 16
    secondPage = [PAGE_TOTAL, HEADER] + [HEADER] * 4 + [DETAIL] * 9
    assert KindsOf(lines) == firstPage + secondPage + [PAGE_TOTAL, HEADER, GRAND_TOTAL]
    assert [line.text for line in lines[20:26]] == [
        TotalText(PAGE_TOTAL_PREFIX, "+         16.00"),
        DASH_LINE,
        *HeaderTexts(QUERY),
    ]
    # Details 17-25 make 9.00, plus the last 1.00 again; the grand total is 16.00 + 10.00.
    assert lines[35].text == TotalText(PAGE_TOTAL_PREFIX, "+         10.00")
    assert lines[37].text == TotalText(GRAND_TOTAL_PREFIX, "+         26.00")
    assert [lines[index].amount for index in (20, 35, 37)] == [
        Decimal("16.00"),
        Decimal("10.00"),
        Decimal("26.00"),
    ]


def test_account_total_lines_can_step_the_counter_over_twenty_without_a_page_break() -> None:
    cardBRows = [BuildRow(f"{sequence:016d}", CARD_B, "1.00") for sequence in (16, 17, 18)]
    lines = RunReport([*BuildCardRows(CARD_A, ["1.00"] * 15), *cardBRows])

    # After 4 headers and 15 details the counter is 19; the account total and its dash rule
    # take it to 21, so MOD(WS-LINE-COUNTER, 20) is never 0 before a detail of card B.
    cardALines = [HEADER] * 4 + [DETAIL] * 15 + [ACCOUNT_TOTAL, HEADER]
    assert KindsOf(lines) == cardALines + [DETAIL] * 3 + [PAGE_TOTAL, HEADER, GRAND_TOTAL]
    assert lines[19].text == TotalText(ACCOUNT_TOTAL_PREFIX, "+         15.00")
    # 18 details of 1.00, plus the last 1.00 again at end of file.
    assert lines[24].text == TotalText(PAGE_TOTAL_PREFIX, "+         19.00")


def test_empty_input_writes_no_lines() -> None:
    assert RunReport([]) == []


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"xref_acct_id": None}, id="card-not-on-cardxref"),
        pytest.param({"tran_type_cd": "09"}, id="type-not-on-trantype"),
        pytest.param({"tran_cat_cd": 2}, id="category-not-on-trancatg"),
    ],
)
def test_key_not_on_file_abends_with_code_999(overrides: dict[str, object]) -> None:
    row = {**BuildRow(FIRST_TRAN_ID, CARD_A, "1.00"), **overrides}

    with pytest.raises(ReportAbendError) as raised:
        RunReport([row])

    assert raised.value.abendCode == ABEND_CODE
    # The abend text names the failing key, but never the full card number.
    assert CARD_A not in str(raised.value)


def test_negative_amounts_print_the_minus_sign_of_both_edited_pictures() -> None:
    lines = RunReport([BuildRow(FIRST_TRAN_ID, CARD_A, "-1234.50")])

    assert lines[4].amount == Decimal("-1234.50")
    # -ZZZ,ZZZ,ZZZ.ZZ: leading zeros and the commas among them become spaces.
    assert lines[4].text == DetailText(FIRST_TRAN_ID, ACCOUNT_A, "-      1,234.50")
    # -1234.50 twice (end of file) is -2469.00; +ZZZ,ZZZ,ZZZ.ZZ prints '-' for a negative.
    assert lines[5].amount == Decimal("-2469.00")
    assert lines[5].text == TotalText(PAGE_TOTAL_PREFIX, "-      2,469.00")


def ReadFixtureLines(path: Path) -> list[str]:
    """The fixture's lines with only their \\n terminators removed, trailing spaces kept."""
    assert path.is_file(), f"missing reconciliation fixture {path}"
    text = path.read_bytes().decode("utf-8")
    assert text.endswith("\n"), f"{path} does not end with a line terminator"
    return text[:-1].split("\n")


def ReadCsvRows(path: Path) -> list[dict[str, str]]:
    """The rows of a loader_rows fixture, keyed by its table column names."""
    assert path.is_file(), f"missing reconciliation fixture {path}"
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def LoadTypeDescriptions() -> dict[str, str]:
    """TRAN-TYPE to TRAN-TYPE-DESC from the expected reference.transaction_types rows."""
    return {row["tran_type"]: row["description"] for row in ReadCsvRows(TRANSACTION_TYPES_PATH)}


def LoadCategoryDescriptions() -> dict[tuple[str, int], str]:
    """(TRAN-TYPE-CD, TRAN-CAT-CD) to TRAN-CAT-TYPE-DESC from transaction_categories rows."""
    return {
        (row["tran_type_cd"], int(row["tran_cat_cd"])): row["description"]
        for row in ReadCsvRows(TRANSACTION_CATEGORIES_PATH)
    }


def LoadXrefAccounts() -> dict[str, str]:
    """XREF-CARD-NUM to XREF-ACCT-ID from the expected xref.card_xref rows."""
    return {row["card_num"]: row["acct_id"] for row in ReadCsvRows(CARD_XREF_PATH)}


def IsInSampleRange(record: Mapping[str, object]) -> bool:
    """The TRANREPT INCLUDE on TRAN-PROC-TS (1:10): a blank PROC-TS never matches."""
    procTs = record["proc_ts"]
    return procTs is not None and SAMPLE_START_DATE <= procTs.date() <= SAMPLE_END_DATE


def SampleSortKey(record: Mapping[str, object]) -> tuple[bytes, bytes]:
    """SORT FIELDS=(TRAN-CARD-NUM,A) over TRANSACT key order, both in EBCDIC byte order."""
    return (record["card_num"].encode(EBCDIC_CODEC), record["tran_id"].encode(EBCDIC_CODEC))


def LoadSampleRows() -> list[dict[str, object]]:
    """The oracle's post-chain TRANSACT records in the sample range, sorted, with their xref."""
    xrefAccounts = LoadXrefAccounts()
    records = [DecodeRecord(line, TRANSACTION_LAYOUT) for line in ReadFixtureLines(COMBINED_PATH)]
    reportRecords = sorted(filter(IsInSampleRange, records), key=SampleSortKey)
    return [
        {**record, "xref_acct_id": xrefAccounts.get(record["card_num"])} for record in reportRecords
    ]


def test_sample_report_matches_the_oracle_tranrept_lines() -> None:
    rows = LoadSampleRows()
    assert rows, "no sample transaction falls in the report range"

    result = BuildReport(SAMPLE_QUERY, rows, LoadTypeDescriptions(), LoadCategoryDescriptions())

    assert [line.text for line in result] == ReadFixtureLines(TRANREPT_LINES_PATH)
