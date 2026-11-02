"""Replaces: CBTRN03C (TRANREPT), copybooks CVTRA07Y, CVTRA05Y, PROC TRANREPT

The CBTRN03C transaction detail report as a pure line builder. BuildReport turns the report input
into the 133-column TRANREPT print lines (CVTRA07Y layouts), each with the structured fields the
CR00 screen table, the CSV export and the PDF export show. It reads no database, file, clock or
setting and logs nothing: the CBTRN03C DISPLAY of every TRAN-RECORD and TRAN-AMT has no
counterpart, because it would log card numbers.

The caller supplies the input already filtered and ordered, replacing the proc/TRANREPT.prc
STEP05R INCLUDE on TRAN-PROC-TS positions 305-314 and its SORT FIELDS=(TRAN-CARD-NUM,A). The
ELSE NEXT SENTENCE that CBTRN03C takes for a record outside the date range (lines 173-178) jumps
past END-PERFORM and ends the report without totals; no record can reach it here. Its one
surviving path is empty input, where the first READ meets end of file and the job writes nothing,
so BuildReport returns no lines for no rows.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from typing import Any

from app.core.security import MaskCardNumber
from app.schemas.reports import ReportLine, ReportLineKind
from app.utils.decimal_utils import COBOL_CONTEXT, StoreInto
from app.utils.record_layouts import (
    REPORT_ACCOUNT_TOTALS_LINE,
    REPORT_GRAND_TOTALS_LINE,
    REPORT_LINE_WIDTH,
    REPORT_NAME_HEADER_LINE,
    REPORT_PAGE_TOTALS_LINE,
    TRANSACTION_DETAIL_LINE,
    TRANSACTION_HEADER_1_LINE,
    TRANSACTION_HEADER_2_LINE,
    LineLayout,
    RenderLine,
)

# WS-PAGE-SIZE PIC 9(03) COMP-3 VALUE 20 (line 131).
LINES_PER_PAGE = 20
# WS-LINE-COUNTER PIC 9(09) COMP-3 (line 129): every ADD 1 keeps the low-order nine digits.
LINE_COUNTER_MODULUS = 10**9
# WS-PAGE-TOTAL, WS-ACCOUNT-TOTAL and WS-GRAND-TOTAL PIC S9(09)V99 (lines 134-136), which is
# also the picture of TRAN-AMT (CVTRA05Y).
TOTAL_INT_DIGITS = 9
TOTAL_DEC_DIGITS = 2
ZERO_TOTAL = Decimal("0.00")
# FD-REPTFILE-REC PIC X(133).
REPORT_LINE_LENGTH = REPORT_LINE_WIDTH
# TRAN-CARD-NUM and WS-CURR-CARD-NUM PIC X(16); WS-CURR-CARD-NUM starts as SPACES (line 137).
CARD_NUM_LENGTH = 16
INITIAL_CARD_NUM = " " * CARD_NUM_LENGTH
# TRAN-REPORT-TYPE-DESC X(15), TRAN-REPORT-CAT-DESC X(29) and TRAN-REPORT-CAT-CD 9(04).
TYPE_DESC_WIDTH = 15
CAT_DESC_WIDTH = 29
CAT_CD_DIGITS = 4
# 9999-ABEND-PROGRAM: MOVE 999 TO ABCODE, then CALL 'CEE3ABD' (lines 626-630).
ABEND_CODE = 999
# The INVALID KEY DISPLAY texts of 1500-A, 1500-B and 1500-C (lines 487, 497 and 507).
INVALID_CARD_NUMBER_PREFIX = "INVALID CARD NUMBER : "
INVALID_TRANSACTION_TYPE_PREFIX = "INVALID TRANSACTION TYPE : "
INVALID_TRAN_CATG_KEY_PREFIX = "INVALID TRAN CATG KEY : "
# WS-BLANK-LINE PIC X(133) VALUE SPACES (line 133) and the two constant CVTRA07Y heading lines.
BLANK_LINE_TEXT = " " * REPORT_LINE_LENGTH
COLUMN_HEADINGS_TEXT = RenderLine({}, TRANSACTION_HEADER_1_LINE)
DASH_LINE_TEXT = RenderLine({}, TRANSACTION_HEADER_2_LINE)


@dataclass(frozen=True)
class ReportRequestData:
    """The report date range, replacing the DATEPARM record CORPT00C wrote into the job stream.

    startDate and endDate are the 10-character YYYY-MM-DD texts of WS-START-DATE and WS-END-DATE
    (WS-DATEPARM-RECORD, lines 122-125), printed in REPORT-NAME-HEADER.
    """

    startDate: str
    endDate: str


@dataclass(frozen=True)
class ReportLookups:
    """The TRANTYPE and TRANCATG files that 1500-B and 1500-C read by key.

    typeDescriptions maps TRAN-TYPE to TRAN-TYPE-DESC (CVTRA03Y), and categoryDescriptions maps
    (TRAN-TYPE-CD, TRAN-CAT-CD) to TRAN-CAT-TYPE-DESC (CVTRA04Y).
    """

    typeDescriptions: Mapping[str, str]
    categoryDescriptions: Mapping[tuple[str, int], str]


class ReportAbendError(RuntimeError):
    """CBTRN03C abend 999: an INVALID KEY read of CARDXREF, TRANTYPE or TRANCATG (lines 484-512).

    displayText is the legacy DISPLAY line, except that the card number is masked, so no PAN
    reaches a log. TRANREPT is DISP=(NEW,CATLG,DELETE), so an abended job left no report, and
    BuildReport returns no partial report either.
    """

    def __init__(self, displayText: str) -> None:
        super().__init__(f"CBTRN03C ABEND {ABEND_CODE}: {displayText}")
        self.abendCode = ABEND_CODE
        self.displayText = displayText


@dataclass
class ReportState:
    """Internal accumulator: CBTRN03C WS-REPORT-VARS, WS-CURR-CARD-NUM and the last XREF-ACCT-ID.

    lines holds the TRANREPT records written so far, in print order.
    """

    query: ReportRequestData
    lines: list[ReportLine] = field(default_factory=list)
    lineCounter: int = 0
    pageTotal: Decimal = ZERO_TOTAL
    accountTotal: Decimal = ZERO_TOTAL
    grandTotal: Decimal = ZERO_TOTAL
    currentCardNum: str = INITIAL_CARD_NUM
    firstTime: bool = True
    currentAcctId: str = ""


@dataclass(frozen=True)
class DetailFields:
    """Internal: the TRANSACTION-DETAIL-REPORT fields 1120-WRITE-DETAIL moves (lines 361-374)."""

    tranId: str
    acctId: str
    typeCd: str
    typeDesc: str
    catCd: int
    catDesc: str
    source: str
    amount: Decimal


def BuildReport(
    query: ReportRequestData,
    rows: Sequence[Mapping[str, Any]],
    typeDescriptions: Mapping[str, str],
    categoryDescriptions: Mapping[tuple[str, int], str],
) -> list[ReportLine]:
    """Return the CBTRN03C TRANREPT lines for rows, ported from the mainline (lines 159-217).

    Each row is a transactions.transactions row (tran_id, card_num, tran_type_cd, tran_cat_cd,
    tran_source, tran_amt) plus xref_acct_id, the XREF-ACCT-ID of its card or None when the card
    has no cross-reference. Preconditions, not re-checked: rows hold only transactions whose
    proc_ts is not NULL and whose date lies from query.startDate to query.endDate inclusive (the
    TRANREPT INCLUDE on positions 305-314), ordered by card_num_sort then tran_id_sort (SORT
    FIELDS=(TRAN-CARD-NUM,A) over TRANSACT key order). BuildReport neither filters nor sorts.

    No rows give no lines: the ELSE NEXT SENTENCE of lines 173-178 ends an empty run before any
    header or total is written. After the last row the end-of-file branch adds its amount a second
    time and the final account total is never written, as in the source.

    Raises:
        ReportAbendError: A card, transaction type or category key is not on file (abend 999).
    """
    if not rows:
        return []
    lookups = ReportLookups(typeDescriptions, categoryDescriptions)
    state = ReportState(query=query)
    for row in rows:
        ProcessRecord(state, row, lookups)
    FinishReport(state, rows[-1])
    return state.lines


def ProcessRecord(state: ReportState, row: Mapping[str, Any], lookups: ReportLookups) -> None:
    """Port of the mainline's record branch (lines 179-196): card break, lookups and detail.

    On a change of card the previous account total is written (not before the first card) and
    the new card's cross-reference is read; the type and category are read for every record.
    """
    # COBOL compares X(16) fields padded with spaces, so a short value still matches its record.
    cardNum = row["card_num"].ljust(CARD_NUM_LENGTH)
    if cardNum != state.currentCardNum:
        if not state.firstTime:
            WriteAccountTotals(state)
        state.currentCardNum = cardNum
        state.currentAcctId = LookupXref(row)
    typeDesc = LookupTransactionType(row, lookups)
    catDesc = LookupTransactionCategory(row, lookups)
    WriteTransactionReport(state, BuildDetailFields(state, row, typeDesc, catDesc))


def LookupXref(row: Mapping[str, Any]) -> str:
    """Port of 1500-A-LOOKUP-XREF (lines 484-492): the XREF-ACCT-ID of the row's card.

    Raises:
        ReportAbendError: The card has no cross-reference (INVALID KEY); the card is masked.
    """
    acctId = row["xref_acct_id"]
    if acctId is None:
        raise ReportAbendError(INVALID_CARD_NUMBER_PREFIX + MaskCardNumber(row["card_num"]))
    return acctId


def LookupTransactionType(row: Mapping[str, Any], lookups: ReportLookups) -> str:
    """Port of 1500-B-LOOKUP-TRANTYPE (lines 494-502): TRAN-TYPE-DESC of the row's type.

    Raises:
        ReportAbendError: The type is not on TRANTYPE (INVALID KEY).
    """
    typeCd = row["tran_type_cd"]
    typeDesc = lookups.typeDescriptions.get(typeCd)
    if typeDesc is None:
        raise ReportAbendError(INVALID_TRANSACTION_TYPE_PREFIX + typeCd)
    return typeDesc


def LookupTransactionCategory(row: Mapping[str, Any], lookups: ReportLookups) -> str:
    """Port of 1500-C-LOOKUP-TRANCATG (lines 504-512): TRAN-CAT-TYPE-DESC of the row's category.

    Raises:
        ReportAbendError: FD-TRAN-CAT-KEY, the type plus the 4-digit category, is not on TRANCATG.
    """
    typeCd = row["tran_type_cd"]
    catCd = int(row["tran_cat_cd"])
    catDesc = lookups.categoryDescriptions.get((typeCd, catCd))
    if catDesc is None:
        categoryKey = typeCd + str(catCd).zfill(CAT_CD_DIGITS)
        raise ReportAbendError(INVALID_TRAN_CATG_KEY_PREFIX + categoryKey)
    return catDesc


def BuildDetailFields(
    state: ReportState, row: Mapping[str, Any], typeDesc: str, catDesc: str
) -> DetailFields:
    """Collect the values 1120-WRITE-DETAIL moves into TRANSACTION-DETAIL-REPORT (lines 361-370).

    The account is the last XREF-ACCT-ID read, which changes only on a change of card.
    """
    return DetailFields(
        tranId=row["tran_id"],
        acctId=state.currentAcctId,
        typeCd=row["tran_type_cd"],
        typeDesc=typeDesc,
        catCd=int(row["tran_cat_cd"]),
        catDesc=catDesc,
        source=row["tran_source"],
        amount=StoreInto(row["tran_amt"], TOTAL_INT_DIGITS, TOTAL_DEC_DIGITS),
    )


def WriteTransactionReport(state: ReportState, detail: DetailFields) -> None:
    """Port of 1100-WRITE-TRANSACTION-REPORT (lines 274-290).

    The headers come first on the first record. Whenever MOD(WS-LINE-COUNTER, 20) is zero the
    page total and the headers are written; totals add 2 lines and headers 4, so the counter can
    step over a multiple of 20, and the page then runs on as in the source. The amount is then
    added to the page and account totals and the detail line written.
    """
    if state.firstTime:
        state.firstTime = False
        WriteHeaders(state)
    if state.lineCounter % LINES_PER_PAGE == 0:
        WritePageTotals(state)
        WriteHeaders(state)
    state.pageTotal = AddToTotal(state.pageTotal, detail.amount)
    state.accountTotal = AddToTotal(state.accountTotal, detail.amount)
    WriteDetail(state, detail)


def WritePageTotals(state: ReportState) -> None:
    """Port of 1110-WRITE-PAGE-TOTALS (lines 293-304): the page total, then a dash line.

    The page total is added to WS-GRAND-TOTAL and reset; both lines are counted.
    """
    pageTotalLine = TotalLine(REPORT_PAGE_TOTALS_LINE, ReportLineKind.PAGE_TOTAL, state.pageTotal)
    WriteCountedLine(state, pageTotalLine)
    state.grandTotal = AddToTotal(state.grandTotal, state.pageTotal)
    state.pageTotal = ZERO_TOTAL
    WriteCountedLine(state, HeaderLine(DASH_LINE_TEXT))


def WriteAccountTotals(state: ReportState) -> None:
    """Port of 1120-WRITE-ACCOUNT-TOTALS (lines 306-316): the account total, then a dash line.

    The account total is reset; both lines are counted.
    """
    accountTotalLine = TotalLine(
        REPORT_ACCOUNT_TOTALS_LINE, ReportLineKind.ACCOUNT_TOTAL, state.accountTotal
    )
    WriteCountedLine(state, accountTotalLine)
    state.accountTotal = ZERO_TOTAL
    WriteCountedLine(state, HeaderLine(DASH_LINE_TEXT))


def WriteGrandTotals(state: ReportState) -> None:
    """Port of 1110-WRITE-GRAND-TOTALS (lines 318-322): the grand total, not counted."""
    grandTotalLine = TotalLine(
        REPORT_GRAND_TOTALS_LINE, ReportLineKind.GRAND_TOTAL, state.grandTotal
    )
    state.lines.append(grandTotalLine)


def WriteHeaders(state: ReportState) -> None:
    """Port of 1120-WRITE-HEADERS (lines 324-341): four counted header lines.

    REPORT-NAME-HEADER with the date range, WS-BLANK-LINE, TRANSACTION-HEADER-1 and
    TRANSACTION-HEADER-2.
    """
    dateRange = {"start_date": state.query.startDate, "end_date": state.query.endDate}
    nameHeaderText = RenderLine(dateRange, REPORT_NAME_HEADER_LINE)
    for headerText in (nameHeaderText, BLANK_LINE_TEXT, COLUMN_HEADINGS_TEXT, DASH_LINE_TEXT):
        WriteCountedLine(state, HeaderLine(headerText))


def WriteDetail(state: ReportState, detail: DetailFields) -> None:
    """Port of 1120-WRITE-DETAIL (lines 361-374): one counted TRANSACTION-DETAIL-REPORT line."""
    WriteCountedLine(state, DetailLine(detail))


def FinishReport(state: ReportState, lastRow: Mapping[str, Any]) -> None:
    """Port of the mainline's end-of-file branch (lines 197-203).

    TRAN-RECORD still holds the last record, so its amount is added to the page and account
    totals a second time; the page totals and the grand totals follow, and the final account
    total is never written. This looks defective and is ported unchanged.
    """
    lastAmount = lastRow["tran_amt"]
    state.pageTotal = AddToTotal(state.pageTotal, lastAmount)
    state.accountTotal = AddToTotal(state.accountTotal, lastAmount)
    WritePageTotals(state)
    WriteGrandTotals(state)


def AddToTotal(total: Decimal, amount: Decimal) -> Decimal:
    """ADD amount TO total into a PIC S9(09)V99 receiver, truncating without ON SIZE ERROR."""
    with localcontext(COBOL_CONTEXT):
        return StoreInto(total + amount, TOTAL_INT_DIGITS, TOTAL_DEC_DIGITS)


def AdvanceLineCounter(state: ReportState) -> None:
    """ADD 1 TO WS-LINE-COUNTER, a PIC 9(09) receiver that keeps its low-order nine digits."""
    state.lineCounter = (state.lineCounter + 1) % LINE_COUNTER_MODULUS


def WriteCountedLine(state: ReportState, line: ReportLine) -> None:
    """Port of 1111-WRITE-REPORT-REC (lines 343-359) followed by ADD 1 TO WS-LINE-COUNTER."""
    state.lines.append(line)
    AdvanceLineCounter(state)


def HeaderLine(text: str) -> ReportLine:
    """A header-kind line: REPORT-NAME-HEADER, the blank line or a TRANSACTION-HEADER line."""
    return ReportLine(line_kind=ReportLineKind.HEADER, text=text)


def TotalLine(layout: LineLayout, lineKind: ReportLineKind, total: Decimal) -> ReportLine:
    """A total line: the total moved to its +ZZZ,ZZZ,ZZZ.ZZ field in the CVTRA07Y layout."""
    return ReportLine(line_kind=lineKind, amount=total, text=RenderLine({"amount": total}, layout))


def DetailLine(detail: DetailFields) -> ReportLine:
    """The TRANSACTION-DETAIL-REPORT line, with its fields trimmed as printed."""
    return ReportLine(
        line_kind=ReportLineKind.DETAIL,
        tran_id=detail.tranId,
        acct_id=detail.acctId,
        type_cd=detail.typeCd,
        type_desc=detail.typeDesc[:TYPE_DESC_WIDTH].rstrip(" "),
        cat_cd=detail.catCd,
        cat_desc=detail.catDesc[:CAT_DESC_WIDTH].rstrip(" "),
        source=detail.source.rstrip(" "),
        amount=detail.amount,
        text=RenderLine(DetailLineValues(detail), TRANSACTION_DETAIL_LINE),
    )


def DetailLineValues(detail: DetailFields) -> dict[str, object]:
    """The TRANSACTION_DETAIL_LINE field values; RenderLine applies -ZZZ,ZZZ,ZZZ.ZZ and 9(4)."""
    return {
        "tran_id": detail.tranId,
        "acct_id": detail.acctId,
        "type_cd": detail.typeCd,
        "type_desc": detail.typeDesc,
        "cat_cd": detail.catCd,
        "cat_desc": detail.catDesc,
        "source": detail.source,
        "amount": detail.amount,
    }
