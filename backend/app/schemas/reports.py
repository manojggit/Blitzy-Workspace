"""CR00 transaction report contracts: the CORPT00 request fields and the CBTRN03C report lines.

Replaces: CORPT00C (CR00), BMS map CORPT00 (``cpy-bms/CORPT00.CPY``); CBTRN03C report lines,
copybook CVTRA07Y (report line layouts); ``proc/TRANREPT.prc`` (superseded TDQ job).

CORPT00C wrote inline JCL to TDQ ``JOBS``, which ran ``proc/TRANREPT.prc`` and then CBTRN03C to
print the TRANREPT file. The report is rendered online instead: ``GET /reports/transactions``
returns the CBTRN03C lines as JSON, and ``GET /reports/transactions/export`` streams the same
lines as CSV or PDF. CR00 edits run in first-failure mode, and a failed edit answers with
``common.LegacyErrorResponse`` carrying the program's literal and ``cursor_field``.
"""

from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.common import ExportFormat, MoneyAmount, QueryModel, ReportType, ScreenText


class ReportQuery(QueryModel):
    """The CORPT00 input fields that CORPT00C edits in ``PROCESS-ENTER-KEY`` (lines 208-443).

    ``report_type`` replaces the three one-character MONTHLY, YEARLY and CUSTOM selectors. The
    date parts and the confirm flag are raw text limited to their BMS LENGTH, never ``int`` or
    ``date``, so the service reaches every CORPT00C literal, for example
    ``Start Date - Month can NOT be empty...``, ``Start Date - Not a valid Month...``,
    ``Please confirm to print the Monthly report...`` and
    ``"<value>" is not a valid value to confirm...``.
    """

    report_type: ReportType = Field(
        default=ReportType.NONE,
        alias="reportType",
        description=(
            "The report type, replacing the MONTHLY, YEARLY and CUSTOM fields: monthly is the "
            "current month, yearly the current year and custom the entered date range. Empty "
            "or absent selects none, and CORPT00C answers "
            "'Select a report type to print report...'."
        ),
    )
    start_mm: ScreenText(2) = Field(
        default="",
        alias="startMm",
        description="Start date month as keyed (SDTMM, 2 characters); edited for custom only.",
    )
    start_dd: ScreenText(2) = Field(
        default="",
        alias="startDd",
        description="Start date day as keyed (SDTDD, 2 characters); edited for custom only.",
    )
    start_yyyy: ScreenText(4) = Field(
        default="",
        alias="startYyyy",
        description="Start date year as keyed (SDTYYYY, 4 characters); edited for custom only.",
    )
    end_mm: ScreenText(2) = Field(
        default="",
        alias="endMm",
        description="End date month as keyed (EDTMM, 2 characters); edited for custom only.",
    )
    end_dd: ScreenText(2) = Field(
        default="",
        alias="endDd",
        description="End date day as keyed (EDTDD, 2 characters); edited for custom only.",
    )
    end_yyyy: ScreenText(4) = Field(
        default="",
        alias="endYyyy",
        description="End date year as keyed (EDTYYYY, 4 characters); edited for custom only.",
    )
    confirm: ScreenText(1) = Field(
        default="",
        alias="confirm",
        description=(
            "Confirmation as keyed (CONFIRM, 1 character): Y or y builds the report, N or n "
            "clears the screen, blank prompts for confirmation and any other value is refused."
        ),
    )


class ReportExportQuery(ReportQuery):
    """The CORPT00 fields plus the download format of ``GET /reports/transactions/export``.

    The same CORPT00C edits run as for the on-screen report, and the confirmed CBTRN03C lines
    are rendered as CSV or PDF instead of the superseded TRANREPT print file.
    """

    # Named export_format so the attribute does not shadow the format builtin; the wire name
    # stays "format".
    export_format: ExportFormat = Field(
        alias="format",
        description="The download format: csv (one row per report line) or pdf (print lines).",
    )


class ReportLineKind(StrEnum):
    """The kind of a CBTRN03C TRANREPT line, as written by the paragraphs of CBTRN03C.

    ``HEADER`` covers every line that is neither a detail nor a total: REPORT-NAME-HEADER
    (``DALYREPT``, ``Daily Transaction Report``, ``Date Range: <start> to <end>``), the blank
    line, the TRANSACTION-HEADER-1 column headings and every TRANSACTION-HEADER-2 dash rule,
    including the rules written after a page or account total [``cpy/CVTRA07Y.cpy``;
    ``cbl/CBTRN03C.cbl``:lines 293-341]. ``DETAIL`` is TRANSACTION-DETAIL-REPORT, and the three
    totals are REPORT-ACCOUNT-TOTALS, REPORT-PAGE-TOTALS and REPORT-GRAND-TOTALS.
    """

    HEADER = "header"
    DETAIL = "detail"
    ACCOUNT_TOTAL = "account_total"
    PAGE_TOTAL = "page_total"
    GRAND_TOTAL = "grand_total"


class ReportLine(BaseModel):
    """One line of the CBTRN03C TRANREPT print file (CVTRA07Y layouts), in CBTRN03C order.

    Built by the CBTRN03C port for the CORPT00C (CR00) online report. The structured fields
    feed the on-screen table and the CSV columns ``line_kind, tran_id, acct_id, type_cd,
    type_desc, cat_cd, cat_desc, source, amount``; ``text`` is the print line the PDF draws.
    """

    line_kind: ReportLineKind = Field(
        description="The CVTRA07Y record the line was written from: header, detail or a total.",
    )
    tran_id: str = Field(
        default="",
        description="TRAN-REPORT-TRANS-ID (X(16)) on detail lines; empty on other lines.",
    )
    acct_id: str = Field(
        default="",
        description=(
            "TRAN-REPORT-ACCOUNT-ID (X(11)): the XREF-ACCT-ID of the transaction's card, from "
            "the card cross-reference; empty on other lines."
        ),
    )
    type_cd: str = Field(
        default="",
        description="TRAN-REPORT-TYPE-CD (X(02)) on detail lines; empty on other lines.",
    )
    type_desc: str = Field(
        default="",
        description=(
            "TRAN-REPORT-TYPE-DESC: the transaction type description as printed (its first "
            "15 characters, right-trimmed) on detail lines; empty on other lines."
        ),
    )
    cat_cd: int | None = Field(
        default=None,
        description=(
            "TRAN-REPORT-CAT-CD (9(04)) on detail lines, rendered zero-padded to 4 digits as "
            "printed; null on other lines."
        ),
    )
    cat_desc: str = Field(
        default="",
        description=(
            "TRAN-REPORT-CAT-DESC: the transaction category description as printed (its first "
            "29 characters, right-trimmed) on detail lines; empty on other lines."
        ),
    )
    source: str = Field(
        default="",
        description="TRAN-REPORT-SOURCE (X(10)), right-trimmed, on detail lines; empty otherwise.",
    )
    amount: MoneyAmount(9) | None = Field(
        default=None,
        description=(
            "The detail amount (TRAN-AMT) or the account, page or grand total (S9(09)V99), as a "
            "decimal string, never a float; null on header lines."
        ),
    )
    text: str = Field(
        description=(
            "The full 133-column print line as CBTRN03C writes it, with the CVTRA07Y layout and "
            "the edited pictures -ZZZ,ZZZ,ZZZ.ZZ (detail) and +ZZZ,ZZZ,ZZZ.ZZ (totals) applied."
        ),
    )


class TransactionReportResponse(BaseModel):
    """The CORPT00 screen after ENTER, with the CBTRN03C report lines once CR00 is confirmed.

    CORPT00C answers a blank confirmation with a prompt and no lines, and a confirmation of N
    by clearing the screen. A confirmation of Y returns the lines that CBTRN03C printed from
    the superseded TRANREPT job, built online instead.
    """

    start_date: str = Field(
        description=(
            "Start of the report range as YYYY-MM-DD text (WS-START-DATE): computed for monthly "
            "and yearly, entered for custom; empty after a confirmation of N clears the screen."
        ),
    )
    end_date: str = Field(
        description=(
            "End of the report range as YYYY-MM-DD text (WS-END-DATE): computed for monthly "
            "and yearly, entered for custom; empty after a confirmation of N clears the screen."
        ),
    )
    message: str = Field(
        description=(
            "The ERRMSG line, verbatim: '<name> report submitted for printing ...' with the "
            "lines, the prompt 'Please confirm to print the <name> report...' without lines, "
            "or empty after a confirmation of N."
        ),
    )
    # CR00 reports focus by the ReportQuery wire name rather than the snake_case attribute
    # name common.ScreenFeedback describes, because the query keys are the page's field names.
    cursor_field: str | None = Field(
        default=None,
        description=(
            "ReportQuery wire name of the field that receives focus (reportType, startMm, "
            "startDd, startYyyy, endMm, endDd, endYyyy or confirm), replacing "
            "MOVE -1 TO <field>L; null when no field is named."
        ),
    )
    lines: list[ReportLine] = Field(
        default_factory=list,
        description=(
            "The CBTRN03C report lines in print order; empty until the report is confirmed, "
            "and empty for a confirmed range that matched no transaction."
        ),
    )
