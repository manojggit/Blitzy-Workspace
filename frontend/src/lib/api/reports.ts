// Mirrors backend/app/schemas/reports.py and backend/app/api/v1/reports.py for CORPT00C (CR00)
// and CBTRN03C, BMS map CORPT00.
//
// The on-screen report and its CSV and PDF downloads replace the TDQ job submission that ran
// proc/TRANREPT.prc and then CBTRN03C. The server owns every CORPT00C edit and literal: the
// report-type check, the date-part edits, the confirm handling and the CBTRN03C line builder. A
// query property is optional exactly where its Pydantic field has a default. A response property
// is always present, because the server serialises every field of its response model. A 400 edit
// literal rejects the call unchanged, for the page to show.
import { AxiosError, AxiosHeaders, type AxiosResponse, type AxiosResponseHeaders } from "axios";

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

// A downloaded report: the file name the server's attachment gives and the document as received.
export interface ReportExportFile {
    fileName: string;
    content: Blob;
}

// The media type each format must arrive as, compared without its parameters (the CSV comes as
// text/csv; charset=utf-8) and in any letter case.
const EXPORT_MEDIA_TYPES: Readonly<Record<ExportFormat, string>> = {
    [ExportFormat.CSV]: "text/csv",
    [ExportFormat.PDF]: "application/pdf",
};
const CONTENT_TYPE_HEADER = "Content-Type";
const CONTENT_DISPOSITION_HEADER = "Content-Disposition";
const MEDIA_TYPE_PARAMETER_SEPARATOR = ";";
// The server names the file as attachment; filename="TRANREPT_<start>_<end>.<ext>"; the unquoted
// token form of the parameter is read as well.
const ATTACHMENT_FILE_NAME_PATTERN =
    /^\s*attachment\s*;\s*filename\s*=\s*(?:"([^"]*)"|([^\s";]+))\s*$/i;
// A saved file is only named, never placed: a path separator or a control character refuses it.
const UNSAFE_FILE_NAME_CHARACTERS = /[/\\\p{Cc}]/u;
const HTTP_SUCCESS_FIRST = 200;
const HTTP_SUCCESS_END = 300;
// The rejection of a 2xx answer that is not the file asked for. The page never shows this text:
// ToScreenFeedback gives the abend text for an answer that carries no legacy body.
const INVALID_EXPORT_MESSAGE = "The report export answer is not the requested attachment";

function IsSuccessStatus(status: number): boolean {
    return status >= HTTP_SUCCESS_FIRST && status < HTTP_SUCCESS_END;
}

// A non-2xx export answer is the legacy JSON error body, received as bytes like the file it
// stands in for. It is decoded before the client's interceptor and the page read it, so a 403
// refusal message, a legacy literal and its cursor arrive as they do from every other call. A
// body that is not JSON stays as received, which ToScreenFeedback shows as the abend text; a 2xx
// body stays the file.
function DecodeExportErrorBody(
    data: unknown,
    _headers: AxiosResponseHeaders,
    status?: number,
): unknown {
    if (status === undefined || IsSuccessStatus(status) || !(data instanceof ArrayBuffer)) {
        return data;
    }
    try {
        return JSON.parse(new TextDecoder().decode(data)) as unknown;
    } catch (error) {
        if (!(error instanceof SyntaxError)) {
            throw error;
        }
        return data;
    }
}

// One header of the answer as text, or null when the answer does not carry it. axios hands every
// answer's headers over as AxiosHeaders, which reads a name in any letter case; headers in any
// other form name nothing, so such an answer is never taken for a file.
function HeaderText(response: AxiosResponse, name: string): string | null {
    const { headers } = response;
    const value: unknown = headers instanceof AxiosHeaders ? headers.get(name) : null;
    return typeof value === "string" ? value : null;
}

// The media type a Content-Type names, without its parameters, in lower case.
function MediaTypeOf(contentType: string | null): string {
    const mediaType = (contentType ?? "").split(MEDIA_TYPE_PARAMETER_SEPARATOR)[0] ?? "";
    return mediaType.trim().toLowerCase();
}

// The file name of an attachment disposition, or null for any other disposition and for a name
// that is blank or holds a path separator or a control character.
function AttachmentFileName(disposition: string | null): string | null {
    const match = ATTACHMENT_FILE_NAME_PATTERN.exec(disposition ?? "");
    const fileName = match?.[1] ?? match?.[2] ?? "";
    if (fileName.trim() === "" || UNSAFE_FILE_NAME_CHARACTERS.test(fileName)) {
        return null;
    }
    return fileName;
}

// A 2xx answer is a file only in the format's media type under a safe attachment name. Any other
// answer rejects as a bad response, built from that answer, so nothing is saved and the page
// shows the abend text.
function ToReportExportFile(
    response: AxiosResponse<ArrayBuffer>,
    format: ExportFormat,
): ReportExportFile {
    const contentType = HeaderText(response, CONTENT_TYPE_HEADER) ?? "";
    const fileName = AttachmentFileName(HeaderText(response, CONTENT_DISPOSITION_HEADER));
    if (MediaTypeOf(contentType) !== EXPORT_MEDIA_TYPES[format] || fileName === null) {
        throw new AxiosError(
            INVALID_EXPORT_MESSAGE,
            AxiosError.ERR_BAD_RESPONSE,
            response.config,
            response.request,
            response,
        );
    }
    return { fileName, content: new Blob([response.data], { type: contentType }) };
}

// CR00 (CORPT00C): the confirmed report as a CSV or PDF file, fetched through the shared client
// like every other call, so the session cookie, the client's timeout and its 401 and 403
// handling apply, and the call is never retried. A non-2xx answer rejects with its legacy body
// decoded, for the page to show. options carries the owning download's AbortSignal, so a
// superseded or abandoned download stops.
export async function DownloadReportExport(
    query: ReportQuery,
    format: ExportFormat,
    options?: ReadOptions,
): Promise<ReportExportFile> {
    const params: ReportExportQuery = { ...query, format };
    const response = await API_CLIENT.get<ArrayBuffer>(EXPORT_PATH, {
        params,
        responseType: "arraybuffer",
        transformResponse: DecodeExportErrorBody,
        signal: options?.signal,
    });
    return ToReportExportFile(response, format);
}
