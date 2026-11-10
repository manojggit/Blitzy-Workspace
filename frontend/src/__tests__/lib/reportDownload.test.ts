// Tests: DownloadReportExport in lib/api/reports.ts, the CORPT00C (CR00) CSV and PDF downloads
// that replace the TRANREPT print file. The real wrapper runs through API_CLIENT against a capture
// adapter, so the request it sends, the file it returns, the legacy error bodies it decodes and
// the 401 and 403 handling of the client's interceptor are all observed as the page meets them.
import { TextEncoder as NodeTextEncoder } from "node:util";

import axios, {
    AxiosError,
    AxiosHeaders,
    type AxiosAdapter,
    type AxiosResponse,
    type InternalAxiosRequestConfig,
} from "axios";

import {
    DownloadReportExport,
    ExportFormat,
    ReportType,
    type ReportQuery,
} from "@/lib/api/reports";
import {
    ABEND_MESSAGE,
    ADMIN_HOME_ROUTE,
    API_CLIENT,
    API_V1_URL,
    ClearHomeRoute,
    REQUEST_TIMEOUT_MILLISECONDS,
    SIGN_ON_ROUTE,
    SetHomeRoute,
    TakePendingMessage,
    ToScreenFeedback,
    type CompleteScreenFeedback,
} from "@/lib/apiClient";

// jsdom implements no navigation: every window.location.assign is reported through
// console.error, because window.location cannot be mocked under jsdom 26.
const NAVIGATION_NOT_IMPLEMENTED = "Not implemented: navigation";
const REPORT_ROUTE = "/reports/transactions";
const EXPORT_PATH = "/reports/transactions/export";
const CONFIRMED_QUERY: ReportQuery = {
    reportType: ReportType.CUSTOM,
    startMm: "07",
    startDd: "01",
    startYyyy: "2022",
    endMm: "07",
    endDd: "31",
    endYyyy: "2022",
    confirm: "Y",
};
const CSV_FILE_NAME = "TRANREPT_2022-07-01_2022-07-31.csv";
const PDF_FILE_NAME = "TRANREPT_2022-07-01_2022-07-31.pdf";
// The headers backend/app/api/v1/reports.py sends with each attachment.
const CSV_MEDIA_TYPE = "text/csv; charset=utf-8";
const PDF_MEDIA_TYPE = "application/pdf";
const CSV_TEXT = "line_kind,tran_id,acct_id\r\ndetail,0000000000000001,00000000004\r\n";
const PDF_TEXT = "%PDF-1.4\n%TRANREPT\n";
const ABEND_FEEDBACK: CompleteScreenFeedback = {
    message: ABEND_MESSAGE,
    info_message: "",
    field_errors: [],
    cursor_field: "",
};
// The body of the backend's session refusal (401) and of an admin refused on a regular endpoint
// (403): every legacy key, the message empty.
const EMPTY_REFUSAL_FEEDBACK: CompleteScreenFeedback = {
    message: "",
    info_message: "",
    field_errors: [],
    cursor_field: null,
};
const UTF8_ENCODER = new NodeTextEncoder();
const ORIGINAL_ADAPTER = API_CLIENT.defaults.adapter;

// One answer of the export endpoint: its status, its headers and its body text.
interface ExportAnswer {
    status: number;
    headers: Readonly<Record<string, string>>;
    body: string;
}

function AttachmentHeaders(mediaType: string, disposition: string): Record<string, string> {
    return { "content-type": mediaType, "content-disposition": disposition };
}

function AttachmentOf(fileName: string): string {
    return `attachment; filename="${fileName}"`;
}

function LegacyAnswer(status: number, feedback: CompleteScreenFeedback): ExportAnswer {
    const JSON_CONTENT_HEADERS = { "content-type": "application/json" };
    return { status, headers: JSON_CONTENT_HEADERS, body: JSON.stringify(feedback) };
}

// The body bytes as a browser receives them: UTF-8, in this realm's own ArrayBuffer.
function Utf8Body(text: string): ArrayBuffer {
    return Uint8Array.from(UTF8_ENCODER.encode(text)).buffer;
}

let capturedConfigs: InternalAxiosRequestConfig[] = [];
let consoleErrorSpy: jest.SpyInstance;

// Answers as axios's own adapters do: a 2xx resolves, any other status rejects with the response,
// as settle() does, and the body stays the bytes received.
function AnswerWith(answer: ExportAnswer): AxiosAdapter {
    return (config) => {
        capturedConfigs.push(config);
        const { status, headers } = answer;
        const response: AxiosResponse = {
            data: Utf8Body(answer.body),
            status,
            statusText: "",
            headers,
            config,
            request: {},
        };
        if (status >= 200 && status < 300) {
            return Promise.resolve(response);
        }
        const code = status >= 500 ? AxiosError.ERR_BAD_RESPONSE : AxiosError.ERR_BAD_REQUEST;
        const message = `Request failed with status code ${status}`;
        return Promise.reject(new AxiosError(message, code, config, {}, response));
    };
}

// The rejection of a download, which must be the axios error the page shows.
async function RejectionOf(format: ExportFormat): Promise<AxiosError> {
    const rejection: unknown = await DownloadReportExport(CONFIRMED_QUERY, format).then(
        () => undefined,
        (error: unknown) => error,
    );
    expect(rejection).toBeInstanceOf(AxiosError);
    return rejection as AxiosError;
}

// The client's bare /auth/me lookup, through which a 403 finds the user's own menu.
function AnswerHomeRouteLookup(nextRoute: string): jest.SpyInstance {
    const config: InternalAxiosRequestConfig = {
        url: API_V1_URL + "/auth/me",
        headers: new AxiosHeaders(),
    };
    const data = { next_route: nextRoute };
    const answer: AxiosResponse = { data, status: 200, statusText: "", headers: {}, config };
    return jest.spyOn(axios, "get").mockResolvedValue(answer);
}

function NavigationWasAttempted(): boolean {
    return consoleErrorSpy.mock.calls.some((call: unknown[]) => {
        const firstArgument = call[0];
        const text = firstArgument instanceof Error ? firstArgument.message : String(firstArgument);
        return text.includes(NAVIGATION_NOT_IMPLEMENTED);
    });
}

// jsdom's Blob has no text(); its FileReader reads the saved content back.
function ReadBlobText(blob: Blob): Promise<string> {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(reader.error);
        reader.readAsText(blob);
    });
}

// A 2xx answer and the file the page saves from it.
interface SavedFileCase {
    label: string;
    format: ExportFormat;
    answer: ExportAnswer;
    fileName: string;
}

const SAVED_FILE_CASES: readonly SavedFileCase[] = [
    {
        label: "the CSV attachment",
        format: ExportFormat.CSV,
        answer: {
            status: 200,
            headers: AttachmentHeaders(CSV_MEDIA_TYPE, AttachmentOf(CSV_FILE_NAME)),
            body: CSV_TEXT,
        },
        fileName: CSV_FILE_NAME,
    },
    {
        label: "the PDF attachment",
        format: ExportFormat.PDF,
        answer: {
            status: 200,
            headers: AttachmentHeaders(PDF_MEDIA_TYPE, AttachmentOf(PDF_FILE_NAME)),
            body: PDF_TEXT,
        },
        fileName: PDF_FILE_NAME,
    },
    {
        label: "a CSV typed in capitals with an unquoted file name",
        format: ExportFormat.CSV,
        answer: {
            status: 200,
            headers: AttachmentHeaders(
                "TEXT/CSV;Charset=UTF-8",
                `Attachment; filename=${CSV_FILE_NAME}`,
            ),
            body: CSV_TEXT,
        },
        fileName: CSV_FILE_NAME,
    },
];

// A 2xx answer that is not the file asked for, which nothing may save.
interface InvalidFileCase {
    label: string;
    format: ExportFormat;
    headers: Readonly<Record<string, string>>;
}

const INVALID_FILE_CASES: readonly InvalidFileCase[] = [
    {
        label: "a CSV answered for the PDF",
        format: ExportFormat.PDF,
        headers: AttachmentHeaders(CSV_MEDIA_TYPE, AttachmentOf(PDF_FILE_NAME)),
    },
    {
        label: "a JSON body answered for the CSV",
        format: ExportFormat.CSV,
        headers: AttachmentHeaders("application/json", AttachmentOf(CSV_FILE_NAME)),
    },
    {
        label: "a media type that only starts like the CSV's",
        format: ExportFormat.CSV,
        headers: AttachmentHeaders("text/csvx", AttachmentOf(CSV_FILE_NAME)),
    },
    {
        label: "no Content-Disposition",
        format: ExportFormat.CSV,
        headers: { "content-type": CSV_MEDIA_TYPE },
    },
    {
        label: "an inline disposition",
        format: ExportFormat.PDF,
        headers: AttachmentHeaders(PDF_MEDIA_TYPE, `inline; filename="${PDF_FILE_NAME}"`),
    },
    {
        label: "a blank file name",
        format: ExportFormat.CSV,
        headers: AttachmentHeaders(CSV_MEDIA_TYPE, AttachmentOf("")),
    },
    {
        label: "a file name with a parent path",
        format: ExportFormat.CSV,
        headers: AttachmentHeaders(CSV_MEDIA_TYPE, AttachmentOf(`../${CSV_FILE_NAME}`)),
    },
    {
        label: "a file name with a backslash",
        format: ExportFormat.PDF,
        headers: AttachmentHeaders(PDF_MEDIA_TYPE, AttachmentOf(`reports\\${PDF_FILE_NAME}`)),
    },
    {
        label: "a file name with a tab, the one control character axios keeps in a header",
        format: ExportFormat.PDF,
        headers: AttachmentHeaders(PDF_MEDIA_TYPE, AttachmentOf("TRANREPT\t.pdf")),
    },
];

// A non-2xx legacy body, received as bytes, and the feedback the page shows for it.
interface LegacyFailureCase {
    label: string;
    status: number;
    feedback: CompleteScreenFeedback;
}

const LEGACY_FAILURE_CASES: readonly LegacyFailureCase[] = [
    {
        label: "a 400 date edit literal",
        status: 400,
        feedback: {
            message: "Start Date - Not a valid Month...",
            info_message: "",
            field_errors: ["startMm"],
            cursor_field: "startMm",
        },
    },
    {
        label: "the 503 file-error literal",
        status: 503,
        feedback: {
            message: "Unable to Write TDQ (JOBS)...",
            info_message: "",
            field_errors: [],
            cursor_field: "reportType",
        },
    },
    {
        label: "a 400 literal echoing a keyed code-page-037 character",
        status: 400,
        feedback: {
            message: '"\u00a2" is not a valid value to confirm...',
            info_message: "",
            field_errors: ["confirm"],
            cursor_field: "confirm",
        },
    },
];

// A non-2xx answer that carries no legacy body, which the page shows as the abend text.
const ABEND_ANSWERS: readonly ExportAnswer[] = [
    { status: 500, headers: { "content-type": "text/plain" }, body: "Internal Server Error" },
    { status: 502, headers: { "content-type": "text/html" }, body: "<html>Bad Gateway</html>" },
    {
        status: 422,
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ detail: [{ loc: ["query", "format"], msg: "invalid" }] }),
    },
];

beforeEach(() => {
    capturedConfigs = [];
    window.sessionStorage.clear();
    ClearHomeRoute();
    window.history.pushState({}, "", REPORT_ROUTE);
    consoleErrorSpy = jest.spyOn(console, "error").mockImplementation(() => undefined);
});

afterEach(() => {
    API_CLIENT.defaults.adapter = ORIGINAL_ADAPTER;
    jest.restoreAllMocks();
});

it.each(SAVED_FILE_CASES)(
    "DownloadReportExport saves $label under the server's name, through the shared client",
    async ({ format, answer, fileName }) => {
        API_CLIENT.defaults.adapter = AnswerWith(answer);
        const { signal } = new AbortController();
        const file = await DownloadReportExport(CONFIRMED_QUERY, format, { signal });
        expect(file.fileName).toBe(fileName);
        expect(file.content.type).toBe(answer.headers["content-type"]?.toLowerCase());
        await expect(ReadBlobText(file.content)).resolves.toBe(answer.body);
        expect(capturedConfigs).toHaveLength(1);
        expect(capturedConfigs[0]).toMatchObject({
            method: "get",
            baseURL: API_V1_URL,
            url: EXPORT_PATH,
            responseType: "arraybuffer",
            withCredentials: true,
            timeout: REQUEST_TIMEOUT_MILLISECONDS,
        });
        expect(capturedConfigs[0]?.params).toStrictEqual({ ...CONFIRMED_QUERY, format });
        expect(capturedConfigs[0]?.signal).toBe(signal);
    },
);

it.each(INVALID_FILE_CASES)(
    "DownloadReportExport refuses $label, which the page shows as the abend text",
    async ({ format, headers }) => {
        API_CLIENT.defaults.adapter = AnswerWith({ status: 200, headers, body: CSV_TEXT });
        const error = await RejectionOf(format);
        expect(error.code).toBe(AxiosError.ERR_BAD_RESPONSE);
        expect(error.response?.status).toBe(200);
        expect(ToScreenFeedback(error)).toStrictEqual(ABEND_FEEDBACK);
        expect(NavigationWasAttempted()).toBe(false);
    },
);

it.each(LEGACY_FAILURE_CASES)(
    "DownloadReportExport rejects $label with its decoded legacy body ($status)",
    async ({ status, feedback }) => {
        API_CLIENT.defaults.adapter = AnswerWith(LegacyAnswer(status, feedback));
        const error = await RejectionOf(ExportFormat.PDF);
        expect(error.response?.status).toBe(status);
        expect(ToScreenFeedback(error)).toStrictEqual(feedback);
        expect(NavigationWasAttempted()).toBe(false);
    },
);

it.each(ABEND_ANSWERS)(
    "DownloadReportExport rejects a $status without a legacy body as the abend text",
    async (answer) => {
        API_CLIENT.defaults.adapter = AnswerWith(answer);
        const error = await RejectionOf(ExportFormat.CSV);
        expect(error.response?.status).toBe(answer.status);
        expect(ToScreenFeedback(error)).toStrictEqual(ABEND_FEEDBACK);
    },
);

it("DownloadReportExport leaves a 401 to the client's redirect to /signon", async () => {
    API_CLIENT.defaults.adapter = AnswerWith(LegacyAnswer(401, EMPTY_REFUSAL_FEEDBACK));
    const error = await RejectionOf(ExportFormat.CSV);
    expect(error.response?.status).toBe(401);
    expect(NavigationWasAttempted()).toBe(true);
    expect(SIGN_ON_ROUTE).toBe("/signon");
    expect(ToScreenFeedback(error).message).toBe("");
});

// An admin refused on the regular CR00 endpoint gets an empty message and returns to /admin.
it("DownloadReportExport leaves a 403 to the client's return to the user's own menu", async () => {
    SetHomeRoute(ADMIN_HOME_ROUTE);
    const lookupSpy = AnswerHomeRouteLookup(ADMIN_HOME_ROUTE);
    API_CLIENT.defaults.adapter = AnswerWith(LegacyAnswer(403, EMPTY_REFUSAL_FEEDBACK));
    const error = await RejectionOf(ExportFormat.PDF);
    expect(error.response?.status).toBe(403);
    expect(NavigationWasAttempted()).toBe(true);
    expect(TakePendingMessage()).toBe("");
    expect(lookupSpy).toHaveBeenCalledTimes(1);
});
