// Mirrors backend/app/schemas/reports.py and backend/app/api/v1/reports.py for CORPT00C (CR00)
// and CBTRN03C, BMS map CORPT00.
//
// The on-screen report and its CSV and PDF downloads replace the TDQ job submission that ran
// proc/TRANREPT.prc and then CBTRN03C. The server owns every CORPT00C edit and literal: the
// report-type check, the date-part edits, the confirm handling and the CBTRN03C line builder. A
// query property is optional exactly where its Pydantic field has a default. A response property
// is always present, because the server serialises every field of its response model. A 400 edit
// literal rejects the call unchanged, for the page to show.
import { API_CLIENT, type MoneyAmount, type ReadOptions } from "@/lib/apiClient";

const REPORT_PATH = "/reports/transactions";
const EXPORT_PATH = `${REPORT_PATH}/export`;

// The CORPT00 MONTHLY, YEARLY and CUSTOM selectors. NONE is an empty selection, which the server
// answers with "Select a report type to print report..."; omitting reportType does the same.
export enum ReportType {
    NONE = "",
    MONTHLY = "monthly",
    YEARLY = "yearly",
    CUSTOM = "custom",
}

// The download formats replacing the TRANREPT print file.
export enum ExportFormat {
    CSV = "csv",
    PDF = "pdf",
}

// The CVTRA07Y record a TRANREPT line was written from. HEADER covers the report name, date range,
// blank, column-heading and dash-rule lines.
export enum ReportLineKind {
    HEADER = "header",
    DETAIL = "detail",
    ACCOUNT_TOTAL = "account_total",
    PAGE_TOTAL = "page_total",
    GRAND_TOTAL = "grand_total",
}

// The GET /reports/transactions query: the CORPT00 fields as keyed, at most their BMS LENGTH
// (SDTMM 2, SDTDD 2, SDTYYYY 4, EDTMM 2, EDTDD 2, EDTYYYY 4, CONFIRM 1). The date parts and the
// confirm flag are never trimmed, padded or converted, because CORPT00C edits the raw text.
export interface ReportQuery {
    reportType?: ReportType;
    startMm?: string;
    startDd?: string;
    startYyyy?: string;
    endMm?: string;
    endDd?: string;
    endYyyy?: string;
    confirm?: string;
}

// The GET /reports/transactions/export query: the same fields plus the download format.
export interface ReportExportQuery extends ReportQuery {
    format: ExportFormat;
}

// One TRANREPT line in CBTRN03C print order. The code and text fields are empty, and cat_cd null,
// on every line but a detail line; amount is null on header lines only. text is the 133-column
// print line with the edited amount pictures applied.
export interface ReportLine {
    line_kind: ReportLineKind;
    tran_id: string;
    acct_id: string;
    type_cd: string;
    type_desc: string;
    cat_cd: number | null;
    cat_desc: string;
    source: string;
    amount: MoneyAmount | null;
    text: string;
}

// The CORPT00 screen after ENTER. The dates are YYYY-MM-DD text, empty once N clears the screen;
// lines stay empty until Y confirms the report. cursor_field is a ReportQuery wire name.
export interface TransactionReportResponse {
    start_date: string;
    end_date: string;
    message: string;
    cursor_field: string | null;
    lines: ReportLine[];
}

// CR00 (CORPT00C): ENTER on the report screen. options carries the owning read's AbortSignal, so
// an abandoned report stops downloading.
export async function FetchTransactionReport(
    query: ReportQuery,
    options?: ReadOptions,
): Promise<TransactionReportResponse> {
    const response = await API_CLIENT.get<TransactionReportResponse>(REPORT_PATH, {
        params: query,
        signal: options?.signal,
    });
    return response.data;
}

// The confirmed report as a CSV or PDF attachment, for a download link. axios encodes every query
// value and skips absent ones after the API base URL. The browser sends the session cookie on the
// same-site download, so no token is added.
export function BuildReportExportUrl(query: ReportQuery, format: ExportFormat): string {
    const params: ReportExportQuery = { ...query, format };
    return API_CLIENT.getUri({ url: EXPORT_PATH, params });
}
