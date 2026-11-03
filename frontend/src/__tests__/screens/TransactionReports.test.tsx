// Screen tests: BMS CORPT00, program CORPT00C with CBTRN03C lines (CR00),
// page /reports/transactions.
import { alertClasses, tableCellClasses } from "@mui/material";
import { ThemeProvider } from "@mui/material/styles";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { PointerEventsCheckLevel, type UserEvent } from "@testing-library/user-event";
import {
    AxiosError,
    AxiosHeaders,
    CanceledError,
    type AxiosResponse,
    type InternalAxiosRequestConfig,
} from "axios";
import type { ReactNode } from "react";

import TransactionReportsPage from "@/app/reports/transactions/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import {
    BuildReportExportUrl,
    ExportFormat,
    FetchTransactionReport,
    ReportLineKind,
    ReportType,
    type ReportLine,
    type ReportQuery,
    type TransactionReportResponse,
} from "@/lib/api/reports";
import {
    SIGN_ON_ROUTE,
    type LegacyErrorResponse,
    type MessageSeverity,
    type ReadOptions,
} from "@/lib/apiClient";
import type { SessionContextValue } from "@/lib/session";

const mockRouterPush = jest.fn();
const mockRouterReplace = jest.fn();
// next/navigation hands every render the same router object.
const mockRouter = {
    push: mockRouterPush,
    replace: mockRouterReplace,
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};
let mockSearchParams = new URLSearchParams();

jest.mock("next/navigation", () => ({
    useRouter: () => mockRouter,
    useSearchParams: () => mockSearchParams,
    usePathname: () => "/reports/transactions",
}));

// Type U: COSGN00C sends every type but A to the main menu, the menu tree that owns CR00.
const MOCK_SIGNED_ON_USER: NonNullable<SessionContextValue["currentUser"]> = {
    user_id: "TESTUSR1",
    user_type: "U",
    first_name: "Test",
    last_name: "User",
    next_route: "/menu",
};

jest.mock("@/lib/session", () => ({
    SessionProvider: ({ children }: { children: ReactNode }) => children,
    useSession: () => ({
        currentUser: MOCK_SIGNED_ON_USER,
        isLoading: false,
        RefreshSession: jest.fn(),
        ClearSession: jest.fn(),
    }),
}));

// The enums stay real, so the page and these tests share ReportType, ExportFormat and
// ReportLineKind.
jest.mock("@/lib/api/reports", () => ({
    ...jest.requireActual<Record<string, unknown>>("@/lib/api/reports"),
    FetchTransactionReport: jest.fn(),
    BuildReportExportUrl: jest.fn(),
}));

const mockFetchTransactionReport = jest.mocked(FetchTransactionReport);
const mockBuildReportExportUrl = jest.mocked(BuildReportExportUrl);

const HTTP_OK = 200;
const HTTP_BAD_REQUEST = 400;
const HTTP_UNAUTHORIZED = 401;
const HTTP_SERVICE_UNAVAILABLE = 503;
const FIRST_SERVER_ERROR_STATUS = 500;

// The CORPT00 map texts as the page renders them; the BMS '  End Date :' loses its alignment
// blanks.
const SCREEN_TITLE = "Transaction Reports";
const MONTHLY_LABEL = "Monthly (Current Month)";
const YEARLY_LABEL = "Yearly (Current Year)";
const CUSTOM_LABEL = "Custom (Date Range)";
const REPORT_TYPE_LABELS = [MONTHLY_LABEL, YEARLY_LABEL, CUSTOM_LABEL] as const;
const START_DATE_LEGEND = "Start Date :";
const END_DATE_LEGEND = "End Date :";
const DATE_HINT = "(MM/DD/YYYY)";
const CONFIRM_LABEL = "The Report will be submitted for printing. Please confirm:";
const MENU_ROUTE = "/menu";

const REPORT_TABLE_LABEL = "Transaction report";
// CVTRA07Y TRANSACTION-HEADER-1, trimmed.
const TABLE_HEADINGS = [
    "Transaction ID",
    "Account ID",
    "Transaction Type",
    "Tran Category",
    "Tran Source",
    "Amount",
] as const;
const DATE_RANGE_HEADING = "Date Range: 2022-07-01 to 2022-07-31";
// Distinct per format, so each download link proves which format it was built for.
const EXPORT_URL_PREFIX = "http://127.0.0.1:8000/api/v1/reports/transactions/export?format=";

// The ENTER=Continue  F3=Back legend: the button name and the KeyboardEvent.key of each key.
interface ScreenKey {
    buttonName: string;
    keyName: string;
}

const ENTER_KEY: ScreenKey = { buttonName: "Continue (Enter)", keyName: "Enter" };
const F3_KEY: ScreenKey = { buttonName: "Back (F3)", keyName: "F3" };

type KeyEntry = "click" | "key";

const KEY_ENTRIES: readonly KeyEntry[] = ["click", "key"];

type DatePartKey = "startMm" | "startDd" | "startYyyy" | "endMm" | "endDd" | "endYyyy";
type DatePartValues = Readonly<Record<DatePartKey, string>>;

// One CORPT00 date part: its accessible name, its row legend, its ReportQuery key and its BMS
// LENGTH (SDTMM 2, SDTDD 2, SDTYYYY 4; EDTMM, EDTDD, EDTYYYY alike).
interface DatePartCase {
    name: string;
    legend: string;
    queryKey: DatePartKey;
    maxLength: number;
}

const START_MONTH_PART: DatePartCase = {
    name: "Start Date month",
    legend: START_DATE_LEGEND,
    queryKey: "startMm",
    maxLength: 2,
};
const START_DAY_PART: DatePartCase = {
    name: "Start Date day",
    legend: START_DATE_LEGEND,
    queryKey: "startDd",
    maxLength: 2,
};
const START_YEAR_PART: DatePartCase = {
    name: "Start Date year",
    legend: START_DATE_LEGEND,
    queryKey: "startYyyy",
    maxLength: 4,
};
const DATE_PARTS: readonly DatePartCase[] = [
    START_MONTH_PART,
    START_DAY_PART,
    START_YEAR_PART,
    { name: "End Date month", legend: END_DATE_LEGEND, queryKey: "endMm", maxLength: 2 },
    { name: "End Date day", legend: END_DATE_LEGEND, queryKey: "endDd", maxLength: 2 },
    { name: "End Date year", legend: END_DATE_LEGEND, queryKey: "endYyyy", maxLength: 4 },
];

// A month CORPT00C rejects and a day with a leading blank: the page must send both untouched.
const CUSTOM_DATE_PARTS: DatePartValues = {
    startMm: "13",
    startDd: "01",
    startYyyy: "2022",
    endMm: "12",
    endDd: " 5",
    endYyyy: "2022",
};
// CUSTOM chosen and every date part keyed; each keyboard-lock case adds the CONFIRM it keys.
const CUSTOM_KEYED_QUERY: ReportQuery = {
    reportType: ReportType.CUSTOM,
    ...CUSTOM_DATE_PARTS,
};

// Every text field of the map left blank.
const BLANK_TEXT_QUERY: ReportQuery = {
    startMm: "",
    startDd: "",
    startYyyy: "",
    endMm: "",
    endDd: "",
    endYyyy: "",
    confirm: "",
};
const MONTHLY_CONFIRMED_QUERY: ReportQuery = {
    ...BLANK_TEXT_QUERY,
    reportType: ReportType.MONTHLY,
    confirm: "Y",
};
// Every report read carries the AbortSignal of the controller that owns it.
const READ_SIGNAL_OPTIONS = expect.objectContaining({ signal: expect.any(AbortSignal) });

// A valid Custom range that matches no posted transaction, confirmed with Y.
const EMPTY_RANGE_DATE_PARTS: DatePartValues = {
    startMm: "01",
    startDd: "01",
    startYyyy: "2021",
    endMm: "01",
    endDd: "31",
    endYyyy: "2021",
};
const EMPTY_RANGE_CONFIRMED_QUERY: ReportQuery = {
    reportType: ReportType.CUSTOM,
    ...EMPTY_RANGE_DATE_PARTS,
    confirm: "Y",
};
const EMPTY_RANGE_HEADING = "Date Range: 2021-01-01 to 2021-01-31";

// The corpt00c.py literals (CORPT00C lines 213-531).
const SELECT_REPORT_TYPE_MESSAGE = "Select a report type to print report...";
const START_MONTH_INVALID_MESSAGE = "Start Date - Not a valid Month...";
const MONTHLY_SUBMITTED_MESSAGE = "Monthly report submitted for printing ...";
const CUSTOM_SUBMITTED_MESSAGE = "Custom report submitted for printing ...";

function BuildReportLine(fields: Partial<ReportLine> & Pick<ReportLine, "line_kind">): ReportLine {
    return {
        tran_id: "",
        acct_id: "",
        type_cd: "",
        type_desc: "",
        cat_cd: null,
        cat_desc: "",
        source: "",
        amount: null,
        text: "",
        ...fields,
    };
}

// The REPORT-NAME-HEADER print line, its 133-column padding kept, so the page must show it raw.
const HEADER_TEXT = "DALYREPT                    Daily Transaction Report                    ";

// One line of each CVTRA07Y kind, in CBTRN03C print order, with distinct texts and amounts.
const REPORT_LINES: readonly ReportLine[] = [
    BuildReportLine({ line_kind: ReportLineKind.HEADER, text: HEADER_TEXT }),
    BuildReportLine({
        line_kind: ReportLineKind.DETAIL,
        tran_id: "0000000000000001",
        acct_id: "00000000004",
        type_cd: "01",
        type_desc: "Purchase",
        cat_cd: 1,
        cat_desc: "Regular Sales Draft",
        source: "POS TERM",
        amount: "50.00",
        text: "0000000000000001 00000000004 01-Purchase       0001-Regular Sales Draft",
    }),
    BuildReportLine({
        line_kind: ReportLineKind.ACCOUNT_TOTAL,
        amount: "150.00",
        text: "Account Total........................................ +150.00",
    }),
    BuildReportLine({
        line_kind: ReportLineKind.PAGE_TOTAL,
        amount: "250.00",
        text: "Page Total........................................... +250.00",
    }),
    BuildReportLine({
        line_kind: ReportLineKind.GRAND_TOTAL,
        amount: "350.00",
        text: "Grand Total.......................................... +350.00",
    }),
];

// The rendered cells of each line kind: the header is its print line, a detail line its six
// columns, and a total its CVTRA07Y label and amount in footer cells.
interface LineKindCase {
    kind: ReportLineKind;
    position: number;
    cells: readonly string[];
    isFooter: boolean;
}

const LINE_KIND_CASES: readonly LineKindCase[] = [
    { kind: ReportLineKind.HEADER, position: 0, cells: [HEADER_TEXT], isFooter: false },
    {
        kind: ReportLineKind.DETAIL,
        position: 1,
        cells: [
            "0000000000000001",
            "00000000004",
            "01-Purchase",
            "0001-Regular Sales Draft",
            "POS TERM",
            "50.00",
        ],
        isFooter: false,
    },
    {
        kind: ReportLineKind.ACCOUNT_TOTAL,
        position: 2,
        cells: ["Account Total", "150.00"],
        isFooter: true,
    },
    {
        kind: ReportLineKind.PAGE_TOTAL,
        position: 3,
        cells: ["Page Total", "250.00"],
        isFooter: true,
    },
    {
        kind: ReportLineKind.GRAND_TOTAL,
        position: 4,
        cells: ["Grand Total", "350.00"],
        isFooter: true,
    },
];

// One CORPT00C outcome: the literal, its HTTP status, the message-line severity, the field the
// cursor goes to and, for a 200, the report lines returned with it.
interface LiteralCase {
    literal: string;
    status: number;
    severity: MessageSeverity;
    cursorField: string;
    lines: readonly ReportLine[];
}

function EditFailure(literal: string, cursorField: string): LiteralCase {
    return { literal, status: HTTP_BAD_REQUEST, severity: "error", cursorField, lines: [] };
}

// Neither 200 outcome is neutral. The confirm prompt is red: CORPT00C sets WS-ERR-FLG with it
// (line 471), so ERRMSG keeps COLOR=RED. The submission is green: CORPT00C moves DFHGREEN to
// ERRMSGC (line 448). The expected severities follow that source, which the page ports.
function PromptOutcome(literal: string): LiteralCase {
    return { literal, status: HTTP_OK, severity: "error", cursorField: "confirm", lines: [] };
}

function SubmittedOutcome(literal: string, lines: readonly ReportLine[]): LiteralCase {
    return { literal, status: HTTP_OK, severity: "success", cursorField: "reportType", lines };
}

// Every literal CORPT00C sends on ENTER, in source order.
const LITERAL_CASES: readonly LiteralCase[] = [
    EditFailure(SELECT_REPORT_TYPE_MESSAGE, "reportType"),
    EditFailure("Start Date - Month can NOT be empty...", "startMm"),
    EditFailure("Start Date - Day can NOT be empty...", "startDd"),
    EditFailure("Start Date - Year can NOT be empty...", "startYyyy"),
    EditFailure("End Date - Month can NOT be empty...", "endMm"),
    EditFailure("End Date - Day can NOT be empty...", "endDd"),
    EditFailure("End Date - Year can NOT be empty...", "endYyyy"),
    EditFailure(START_MONTH_INVALID_MESSAGE, "startMm"),
    EditFailure("Start Date - Not a valid Day...", "startDd"),
    EditFailure("Start Date - Not a valid Year...", "startYyyy"),
    EditFailure("End Date - Not a valid Month...", "endMm"),
    EditFailure("End Date - Not a valid Day...", "endDd"),
    EditFailure("End Date - Not a valid Year...", "endYyyy"),
    EditFailure("Start Date - Not a valid date...", "startMm"),
    EditFailure("End Date - Not a valid date...", "endMm"),
    PromptOutcome("Please confirm to print the Monthly report..."),
    PromptOutcome("Please confirm to print the Yearly report..."),
    PromptOutcome("Please confirm to print the Custom report..."),
    EditFailure('"X" is not a valid value to confirm...', "confirm"),
    SubmittedOutcome(MONTHLY_SUBMITTED_MESSAGE, REPORT_LINES),
    SubmittedOutcome("Yearly report submitted for printing ...", REPORT_LINES),
    // A confirmed range that matched no transaction answers the same literal with no lines.
    SubmittedOutcome("Custom report submitted for printing ...", []),
    {
        literal: "Unable to Write TDQ (JOBS)...",
        status: HTTP_SERVICE_UNAVAILABLE,
        severity: "error",
        cursorField: "reportType",
        lines: [],
    },
];

// One ENTER made with the Custom criteria and the CONFIRM value keyed, the answer CORPT00C gives
// it while the keyboard is locked, and the control that then takes the cursor.
interface LockedCallCase {
    answer: string;
    confirm: string;
    outcome: LiteralCase;
    FocusedControl: () => HTMLElement;
}

const LOCKED_CALL_CASES: readonly LockedCallCase[] = [
    {
        answer: "the blank-confirm prompt",
        confirm: "",
        outcome: PromptOutcome("Please confirm to print the Custom report..."),
        FocusedControl: ConfirmInput,
    },
    {
        answer: "a date edit failure",
        confirm: "",
        outcome: EditFailure(START_MONTH_INVALID_MESSAGE, "startMm"),
        FocusedControl: () => DatePartInput(START_MONTH_PART),
    },
    {
        answer: "the submitted report",
        confirm: "Y",
        outcome: SubmittedOutcome("Custom report submitted for printing ...", REPORT_LINES),
        FocusedControl: MonthlyButton,
    },
    // Confirm N clears every field and the message (INITIALIZE-ALL-FIELDS).
    {
        answer: "the confirm N reset",
        confirm: "N",
        outcome: {
            literal: "",
            status: HTTP_OK,
            severity: "info",
            cursorField: "reportType",
            lines: [],
        },
        FocusedControl: MonthlyButton,
    },
    // An error body that names no cursor_field puts the cursor on MONTHLY, the map's IC field.
    {
        answer: "a 503 naming no cursor_field",
        confirm: "Y",
        outcome: {
            literal: "Unable to Write TDQ (JOBS)...",
            status: HTTP_SERVICE_UNAVAILABLE,
            severity: "error",
            cursorField: "",
            lines: [],
        },
        FocusedControl: MonthlyButton,
    },
];

interface PendingReport {
    Answer: (response: TransactionReportResponse) => void;
    Fail: (error: unknown) => void;
}

const SEVERITY_CLASSES: Readonly<Record<MessageSeverity, string>> = {
    error: alertClasses.colorError,
    success: alertClasses.colorSuccess,
    info: alertClasses.colorInfo,
};

const DOWNLOAD_CASES: readonly (readonly [ExportFormat, string])[] = [
    [ExportFormat.CSV, "Download CSV"],
    [ExportFormat.PDF, "Download PDF"],
];

// Leaving the screen while a report read runs: F3 aborts the read before it navigates to the
// menu; an unmount, which every other navigation brings, aborts it with no route of its own. The
// mocked router does not navigate, so cleanup() stands in for the unmount.
interface LeaveCase {
    leaving: string;
    LeaveScreen: (user: UserEvent) => Promise<void>;
    routes: readonly string[];
}

const LEAVE_CASES: readonly LeaveCase[] = [
    {
        leaving: "F3 by click",
        LeaveScreen: (user) => PressScreenKey(user, F3_KEY, "click"),
        routes: [MENU_ROUTE],
    },
    {
        leaving: "F3 by key",
        LeaveScreen: (user) => PressScreenKey(user, F3_KEY, "key"),
        routes: [MENU_ROUTE],
    },
    { leaving: "an unmount", LeaveScreen: async () => cleanup(), routes: [] },
];

// The on-screen page size of the report table and the MUI TablePagination next-page control.
const REPORT_LINES_PER_PAGE = 50;
const NEXT_PAGE_BUTTON_NAME = "Go to next page";
const TRAN_ID_DIGITS = 16;

// Detail lines whose transaction IDs count their print position from 1, so each row shows where
// it sits in the report.
function BuildDetailLines(count: number): ReportLine[] {
    return Array.from({ length: count }, (_unused, position) =>
        BuildReportLine({
            line_kind: ReportLineKind.DETAIL,
            tran_id: String(position + 1).padStart(TRAN_ID_DIGITS, "0"),
        }),
    );
}

// Two full pages and a last page of three lines.
const LONG_REPORT_LINES: readonly ReportLine[] = BuildDetailLines(2 * REPORT_LINES_PER_PAGE + 3);

// The page shown after pressing next a number of times: its first line's position, its line count
// and the TablePagination range label.
interface ReportPageCase {
    nextPageClicks: number;
    firstPosition: number;
    lineCount: number;
    rangeLabel: string;
}

const FIRST_PAGE_CASE: ReportPageCase = {
    nextPageClicks: 0,
    firstPosition: 0,
    lineCount: 50,
    rangeLabel: "1–50 of 103",
};
const SECOND_PAGE_CASE: ReportPageCase = {
    nextPageClicks: 1,
    firstPosition: 50,
    lineCount: 50,
    rangeLabel: "51–100 of 103",
};
const LONG_REPORT_PAGE_CASES: readonly ReportPageCase[] = [
    FIRST_PAGE_CASE,
    SECOND_PAGE_CASE,
    { nextPageClicks: 2, firstPosition: 100, lineCount: 3, rangeLabel: "101–103 of 103" },
];
// A short report and one of exactly one full page.
const ONE_PAGE_LINE_COUNTS: readonly number[] = [REPORT_LINES.length, REPORT_LINES_PER_PAGE];

const REPORT_TYPE_GROUP_LABEL = "Report type";
// The id of the MessageLine ERRMSG Alert, which every control the server names is described by.
const ERRMSG_ID = "message-line-errmsg";

// An error outcome that names reportType, with the report type chosen before ENTER, if any.
interface FlaggedReportTypeCase {
    name: string;
    status: number;
    body: LegacyErrorResponse;
    chosenLabel: string | null;
}

const FLAGGED_REPORT_TYPE_CASES: readonly FlaggedReportTypeCase[] = [
    {
        name: "the no-selection edit (400, field_errors and cursor_field)",
        status: HTTP_BAD_REQUEST,
        body: BuildErrorBody({
            message: SELECT_REPORT_TYPE_MESSAGE,
            field_errors: ["reportType"],
            cursor_field: "reportType",
        }),
        chosenLabel: null,
    },
    {
        name: "the file error (503, cursor_field only) with Monthly chosen",
        status: HTTP_SERVICE_UNAVAILABLE,
        body: BuildErrorBody({
            message: "Unable to Write TDQ (JOBS)...",
            cursor_field: "reportType",
        }),
        chosenLabel: MONTHLY_LABEL,
    },
];

// The ENTER outcomes answered, in order, before the Report type group is checked.
interface UnflaggedReportTypeCase {
    name: string;
    outcomes: readonly LiteralCase[];
}

const UNFLAGGED_REPORT_TYPE_CASES: readonly UnflaggedReportTypeCase[] = [
    { name: "on first display", outcomes: [] },
    {
        name: "after an edit naming startMm",
        outcomes: [EditFailure(START_MONTH_INVALID_MESSAGE, "startMm")],
    },
    {
        name: "once an edit naming startMm follows the no-selection edit",
        outcomes: [
            EditFailure(SELECT_REPORT_TYPE_MESSAGE, "reportType"),
            EditFailure(START_MONTH_INVALID_MESSAGE, "startMm"),
        ],
    },
    {
        name: "once the submission, cursor on MONTHLY, follows the no-selection edit",
        outcomes: [
            EditFailure(SELECT_REPORT_TYPE_MESSAGE, "reportType"),
            SubmittedOutcome(MONTHLY_SUBMITTED_MESSAGE, REPORT_LINES),
        ],
    },
];

// An ENTER outcome that names one text control, that control and its own hint, if any.
interface FlaggedControlCase {
    control: string;
    outcome: LiteralCase;
    FlaggedControl: () => HTMLElement;
    hint: string | null;
}

// The date edits and the invalid-confirm edit name their field in field_errors; the blank-confirm
// prompt names CONFIRM as its cursor. CONFIRM keeps its Y/N hint after the ERRMSG line.
const FLAGGED_CONTROL_CASES: readonly FlaggedControlCase[] = [
    {
        control: "the start month",
        outcome: EditFailure(START_MONTH_INVALID_MESSAGE, "startMm"),
        FlaggedControl: () => DatePartInput(START_MONTH_PART),
        hint: null,
    },
    {
        control: "CONFIRM",
        outcome: EditFailure('"X" is not a valid value to confirm...', "confirm"),
        FlaggedControl: () => ConfirmInput(),
        hint: "Y/N",
    },
    {
        control: "CONFIRM",
        outcome: PromptOutcome("Please confirm to print the Monthly report..."),
        FlaggedControl: () => ConfirmInput(),
        hint: "Y/N",
    },
];

// Resolves once the first-display cursor sits on MONTHLY (IC): that focus starts a MUI focus
// ripple asynchronously, which must settle inside the testing-library wrapper, not after a test.
async function RenderScreen(): Promise<UserEvent> {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <TransactionReportsPage />
        </ThemeProvider>,
    );
    await waitFor(() => expect(ReportTypeButton(MONTHLY_LABEL)).toHaveFocus());
    return user;
}

function BuildReportResponse(
    overrides: Partial<TransactionReportResponse> = {},
): TransactionReportResponse {
    return {
        start_date: "2022-07-01",
        end_date: "2022-07-31",
        message: MONTHLY_SUBMITTED_MESSAGE,
        cursor_field: "reportType",
        lines: [...REPORT_LINES],
        ...overrides,
    };
}

// The backend 401 session refusal and every legacy error body carry these four keys.
function BuildErrorBody(overrides: Partial<LegacyErrorResponse> = {}): LegacyErrorResponse {
    return { message: "", info_message: "", field_errors: [], cursor_field: "", ...overrides };
}

// The rejection axios produces for a non-2xx answer, with the code axios's settle step gives it.
function BuildAxiosError(status: number, body: LegacyErrorResponse): AxiosError {
    const config: InternalAxiosRequestConfig = { headers: new AxiosHeaders() };
    const response: AxiosResponse<LegacyErrorResponse> = {
        data: body,
        status,
        statusText: "",
        headers: {},
        config,
    };
    const code =
        status >= FIRST_SERVER_ERROR_STATUS
            ? AxiosError.ERR_BAD_RESPONSE
            : AxiosError.ERR_BAD_REQUEST;
    return new AxiosError("Request failed", code, config, null, response);
}

// The 200 body of an outcome.
function OutcomeResponse({ literal, cursorField, lines }: LiteralCase): TransactionReportResponse {
    return BuildReportResponse({ message: literal, cursor_field: cursorField, lines: [...lines] });
}

// The rejection of a non-2xx outcome; a blank cursor field is a body that names none.
function OutcomeError({ literal, status, cursorField }: LiteralCase): AxiosError {
    const errorBody = BuildErrorBody({
        message: literal,
        field_errors: cursorField === "" ? [] : [cursorField],
        cursor_field: cursorField,
    });
    return BuildAxiosError(status, errorBody);
}

function MockReportOutcome(literalCase: LiteralCase): void {
    if (literalCase.status === HTTP_OK) {
        mockFetchTransactionReport.mockResolvedValue(OutcomeResponse(literalCase));
        return;
    }
    mockFetchTransactionReport.mockRejectedValue(OutcomeError(literalCase));
}

// The next FetchTransactionReport call stays pending until the test answers or fails it.
function DeferReport(): PendingReport {
    const pending: PendingReport = { Answer: () => undefined, Fail: () => undefined };
    mockFetchTransactionReport.mockImplementationOnce(
        () =>
            new Promise<TransactionReportResponse>((resolve, reject) => {
                pending.Answer = resolve;
                pending.Fail = reject;
            }),
    );
    return pending;
}

// Settles the pending call inside act, so the page applies the outcome before the assertions.
async function SettleOutcome(
    pendingReport: PendingReport,
    literalCase: LiteralCase,
): Promise<void> {
    await act(async () => {
        if (literalCase.status === HTTP_OK) {
            pendingReport.Answer(OutcomeResponse(literalCase));
        } else {
            pendingReport.Fail(OutcomeError(literalCase));
        }
    });
}

// A report read that never answers: like axios, it rejects with CanceledError once its signal is
// aborted.
function PendUntilAborted(
    _query: ReportQuery,
    options?: ReadOptions,
): Promise<TransactionReportResponse> {
    return new Promise((_resolve, reject) => {
        options?.signal?.addEventListener("abort", () => reject(new CanceledError()));
    });
}

// The signal the page handed to its first report read.
function FirstReadSignal(): AbortSignal | undefined {
    return mockFetchTransactionReport.mock.calls[0]?.[1]?.signal;
}

// The message line's one alert: its raw text and its MUI severity colour class.
async function ExpectAlert(text: string, severity: MessageSeverity): Promise<void> {
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(text);
    expect(alert).toHaveClass(SEVERITY_CLASSES[severity]);
}

async function PressKey(user: UserEvent, keyName: string): Promise<void> {
    await user.keyboard(`{${keyName}}`);
}

function DatePartInput(part: DatePartCase): HTMLElement {
    return screen.getByRole("textbox", { name: part.name });
}

function ConfirmInput(): HTMLElement {
    return screen.getByRole("textbox", { name: CONFIRM_LABEL });
}

function ReportTypeButton(label: string): HTMLElement {
    return screen.getByRole("button", { name: label });
}

// MONTHLY, the map's IC field.
function MonthlyButton(): HTMLElement {
    return ReportTypeButton(MONTHLY_LABEL);
}

function ReportTypeGroup(): HTMLElement {
    return screen.getByRole("group", { name: REPORT_TYPE_GROUP_LABEL });
}

// The error colour on every segment border and on each label not chosen; the chosen segment keeps
// its container colour, so its label stays legible.
function ExpectErrorSegments(chosenLabel: string | null): void {
    const { error, m3 } = CARD_DEMO_THEME.palette;
    for (const label of REPORT_TYPE_LABELS) {
        const isChosen = label === chosenLabel;
        const button = ReportTypeButton(label);
        expect(button).toHaveAttribute("aria-pressed", String(isChosen));
        expect(button).toHaveStyle({
            borderColor: error.main,
            color: isChosen ? m3.onPrimaryContainer : error.main,
        });
    }
}

// ENTER answered with one outcome; resolves once the message line shows its literal.
async function SubmitOutcome(user: UserEvent, outcome: LiteralCase): Promise<void> {
    MockReportOutcome(outcome);
    await PressScreenKey(user, ENTER_KEY, "click");
    await screen.findByText(outcome.literal);
    await ExpectAlert(outcome.literal, outcome.severity);
}

// A focused button acts on Enter itself, so the physical key is pressed from the start month
// part, as the 3270 cursor sat in an input field.
async function PressScreenKey(user: UserEvent, key: ScreenKey, entry: KeyEntry): Promise<void> {
    if (entry === "click") {
        await user.click(screen.getByRole("button", { name: key.buttonName }));
        return;
    }
    await user.click(DatePartInput(START_MONTH_PART));
    await PressKey(user, key.keyName);
}

async function FillDateParts(user: UserEvent, values: DatePartValues): Promise<void> {
    for (const part of DATE_PARTS) {
        await user.type(DatePartInput(part), values[part.queryKey]);
    }
}

// MONTHLY chosen, Y confirmed and ENTER pressed on the screen shown.
async function SubmitMonthlyReport(user: UserEvent): Promise<void> {
    await user.click(ReportTypeButton(MONTHLY_LABEL));
    await user.type(ConfirmInput(), "Y");
    await PressScreenKey(user, ENTER_KEY, "click");
}

// MONTHLY confirmed with Y, answered with the given report lines: the five line kinds unless
// another report is given.
async function ShowMonthlyReport(lines: readonly ReportLine[] = REPORT_LINES): Promise<UserEvent> {
    mockFetchTransactionReport.mockResolvedValue(BuildReportResponse({ lines: [...lines] }));
    const user = await RenderScreen();
    await SubmitMonthlyReport(user);
    await screen.findByRole("table", { name: REPORT_TABLE_LABEL });
    return user;
}

async function PressNextPage(user: UserEvent): Promise<void> {
    await user.click(screen.getByRole("button", { name: NEXT_PAGE_BUTTON_NAME }));
}

// The transaction ID cell of every body row the report table shows, top to bottom.
function ShownTranIds(): string[] {
    const table = screen.getByRole("table", { name: REPORT_TABLE_LABEL });
    const bodyRows = within(table).getAllByRole("row").slice(1);
    return bodyRows.map((row) => within(row).getAllByRole("cell")[0]?.textContent ?? "");
}

function LongReportTranIds(firstPosition: number, lineCount: number): string[] {
    const pageLines = LONG_REPORT_LINES.slice(firstPosition, firstPosition + lineCount);
    return pageLines.map((line) => line.tran_id);
}

// CUSTOM chosen for a range with no posted transaction, Y confirmed and ENTER pressed.
async function ShowEmptyCustomReport(): Promise<void> {
    mockFetchTransactionReport.mockResolvedValue(
        BuildReportResponse({
            start_date: "2021-01-01",
            end_date: "2021-01-31",
            message: CUSTOM_SUBMITTED_MESSAGE,
            lines: [],
        }),
    );
    const user = await RenderScreen();
    await user.click(ReportTypeButton(CUSTOM_LABEL));
    await FillDateParts(user, EMPTY_RANGE_DATE_PARTS);
    await user.type(ConfirmInput(), "Y");
    await PressScreenKey(user, ENTER_KEY, "click");
    await ExpectAlert(CUSTOM_SUBMITTED_MESSAGE, "success");
}

// Both download links repeat the whole confirmed query, and every link built so far was built
// from it.
function ExpectCompleteDownloads(query: ReportQuery): void {
    for (const [format, linkName] of DOWNLOAD_CASES) {
        expect(mockBuildReportExportUrl).toHaveBeenCalledWith(query, format);
        const link = screen.getByRole("link", { name: linkName });
        expect(link).toHaveAttribute("href", EXPORT_URL_PREFIX + format);
        expect(link).toHaveAttribute("download");
    }
    for (const [builtQuery] of mockBuildReportExportUrl.mock.calls) {
        expect(builtQuery).toStrictEqual(query);
    }
}

// The body row of one report line; the first row of the table holds the column headings.
function ReportBodyRow(position: number): HTMLElement {
    const table = screen.getByRole("table", { name: REPORT_TABLE_LABEL });
    const row = within(table).getAllByRole("row")[position + 1];
    if (row === undefined) {
        throw new Error(`The report shows no line at position ${position}`);
    }
    return row;
}

// The request ends once the busy bar is gone; ENTER sets it before the call is made.
async function WaitForRequestEnd(): Promise<void> {
    await waitFor(() => expect(mockFetchTransactionReport).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole("progressbar")).not.toBeInTheDocument());
}

// The three report types, the six date parts and CONFIRM, in map order.
function CriteriaControls(): HTMLElement[] {
    return [
        ...REPORT_TYPE_LABELS.map((label) => ReportTypeButton(label)),
        ...DATE_PARTS.map((part) => DatePartInput(part)),
        ConfirmInput(),
    ];
}

// The 3270 keyboard lock: every criteria control and ENTER are frozen, F3 still leaves.
function ExpectKeyboardLocked(): void {
    for (const control of CriteriaControls()) {
        expect(control).toBeDisabled();
    }
    expect(screen.getByRole("button", { name: ENTER_KEY.buttonName })).toBeDisabled();
    expect(screen.getByRole("button", { name: F3_KEY.buttonName })).toBeEnabled();
}

// The keyboard unlocked again: every criteria control and ENTER accept input.
function ExpectKeyboardUnlocked(): void {
    for (const control of CriteriaControls()) {
        expect(control).toBeEnabled();
    }
    expect(screen.getByRole("button", { name: ENTER_KEY.buttonName })).toBeEnabled();
}

// Each keyed field is blank or full to its BMS LENGTH, so each edit deletes the last character
// before typing; an unlocked field would change.
const LOCKED_EDIT_KEYS = "{Backspace}9";

// Clicks, typing and the physical ENTER while the call runs change nothing on the criteria that
// were sent. A disabled MUI button sets pointer-events: none, which user-event refuses by default.
async function TryToEditWhileLocked(confirmSent: string): Promise<void> {
    const lockedUser = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    await lockedUser.click(ReportTypeButton(YEARLY_LABEL));
    for (const control of [...DATE_PARTS.map((part) => DatePartInput(part)), ConfirmInput()]) {
        await lockedUser.type(control, LOCKED_EDIT_KEYS);
    }
    await PressKey(lockedUser, ENTER_KEY.keyName);
    expect(ReportTypeButton(CUSTOM_LABEL)).toHaveAttribute("aria-pressed", "true");
    expect(ReportTypeButton(YEARLY_LABEL)).toHaveAttribute("aria-pressed", "false");
    for (const part of DATE_PARTS) {
        expect(DatePartInput(part)).toHaveValue(CUSTOM_DATE_PARTS[part.queryKey]);
    }
    expect(ConfirmInput()).toHaveValue(confirmSent);
    expect(mockFetchTransactionReport).toHaveBeenCalledTimes(1);
}

beforeEach(() => {
    mockSearchParams = new URLSearchParams();
    window.sessionStorage.clear();
    mockFetchTransactionReport.mockReset();
    mockBuildReportExportUrl.mockImplementation((_query, format) => EXPORT_URL_PREFIX + format);
});

it("CORPT00 shows the CR00 header, the map title and the ENTER and F3 legend", async () => {
    await RenderScreen();
    expect(screen.getByText("Tran: CR00")).toBeInTheDocument();
    expect(screen.getByText("Prog: CORPT00C")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: SCREEN_TITLE })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: ENTER_KEY.buttonName })).toBeEnabled();
    expect(screen.getByRole("button", { name: F3_KEY.buttonName })).toBeEnabled();
});

it("CORPT00 shows the report types unselected, the date rows and the confirm prompt", async () => {
    await RenderScreen();
    for (const label of REPORT_TYPE_LABELS) {
        expect(ReportTypeButton(label)).toHaveAttribute("aria-pressed", "false");
    }
    expect(ReportTypeButton(MONTHLY_LABEL)).toHaveFocus();
    for (const legend of [START_DATE_LEGEND, END_DATE_LEGEND]) {
        const legendGroup = screen.getByRole("group", { name: legend });
        expect(within(legendGroup).getByText(legend)).toBeInTheDocument();
        expect(within(legendGroup).getByText(DATE_HINT)).toBeInTheDocument();
    }
    expect(ConfirmInput()).toHaveAttribute("maxlength", "1");
    expect(ConfirmInput()).toHaveValue("");
});

it.each(DATE_PARTS)(
    "CORPT00 $name in the $legend row holds at most its BMS LENGTH $maxLength",
    async ({ name, legend, maxLength }) => {
        await RenderScreen();
        const legendGroup = screen.getByRole("group", { name: legend });
        const input = within(legendGroup).getByRole("textbox", { name });
        expect(input).toHaveAttribute("maxlength", String(maxLength));
        expect(input).toHaveValue("");
    },
);

it.each(KEY_ENTRIES)(
    "CORPT00C ENTER by %s with no report type sends no reportType",
    async (entry) => {
        MockReportOutcome(EditFailure(SELECT_REPORT_TYPE_MESSAGE, "reportType"));
        const user = await RenderScreen();
        await PressScreenKey(user, ENTER_KEY, entry);
        await ExpectAlert(SELECT_REPORT_TYPE_MESSAGE, "error");
        expect(mockFetchTransactionReport).toHaveBeenCalledTimes(1);
        expect(mockFetchTransactionReport).toHaveBeenCalledWith(
            expect.not.objectContaining({ reportType: expect.anything() }),
            READ_SIGNAL_OPTIONS,
        );
        expect(mockFetchTransactionReport).toHaveBeenCalledWith(
            BLANK_TEXT_QUERY,
            READ_SIGNAL_OPTIONS,
        );
    },
);

it("CORPT00C pressing the chosen report type again clears the selection", async () => {
    MockReportOutcome(EditFailure(SELECT_REPORT_TYPE_MESSAGE, "reportType"));
    const user = await RenderScreen();
    await user.click(ReportTypeButton(YEARLY_LABEL));
    expect(ReportTypeButton(YEARLY_LABEL)).toHaveAttribute("aria-pressed", "true");
    await user.click(ReportTypeButton(YEARLY_LABEL));
    expect(ReportTypeButton(YEARLY_LABEL)).toHaveAttribute("aria-pressed", "false");
    await PressScreenKey(user, ENTER_KEY, "click");
    await ExpectAlert(SELECT_REPORT_TYPE_MESSAGE, "error");
    expect(mockFetchTransactionReport).toHaveBeenCalledWith(
        expect.not.objectContaining({ reportType: expect.anything() }),
        READ_SIGNAL_OPTIONS,
    );
});

it("CORPT00C ENTER sends the Custom date parts and confirm exactly as keyed", async () => {
    MockReportOutcome(EditFailure(START_MONTH_INVALID_MESSAGE, "startMm"));
    const user = await RenderScreen();
    await user.click(ReportTypeButton(CUSTOM_LABEL));
    await FillDateParts(user, CUSTOM_DATE_PARTS);
    await user.type(ConfirmInput(), "y");
    await PressScreenKey(user, ENTER_KEY, "key");
    await ExpectAlert(START_MONTH_INVALID_MESSAGE, "error");
    expect(mockFetchTransactionReport.mock.calls[0]?.[0]).toStrictEqual({
        reportType: ReportType.CUSTOM,
        ...CUSTOM_DATE_PARTS,
        confirm: "y",
    });
});

it.each(LITERAL_CASES)(
    "CORPT00C ENTER shows $literal ($status) as $severity on /reports/transactions",
    async (literalCase) => {
        MockReportOutcome(literalCase);
        const user = await RenderScreen();
        await PressScreenKey(user, ENTER_KEY, "click");
        await ExpectAlert(literalCase.literal, literalCase.severity);
    },
);

it("CORPT00C keeps the keyed fields and marks the cursor field after an edit failure", async () => {
    MockReportOutcome(EditFailure(START_MONTH_INVALID_MESSAGE, "startMm"));
    const user = await RenderScreen();
    await user.click(ReportTypeButton(CUSTOM_LABEL));
    await FillDateParts(user, CUSTOM_DATE_PARTS);
    await PressScreenKey(user, ENTER_KEY, "click");
    await ExpectAlert(START_MONTH_INVALID_MESSAGE, "error");
    expect(DatePartInput(START_MONTH_PART)).toHaveValue(CUSTOM_DATE_PARTS.startMm);
    expect(DatePartInput(START_MONTH_PART)).toHaveAttribute("aria-invalid", "true");
    expect(ReportTypeButton(CUSTOM_LABEL)).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
});

it("CORPT00C flags every field_errors part and focuses cursor_field", async () => {
    const errorBody = BuildErrorBody({
        message: START_MONTH_INVALID_MESSAGE,
        field_errors: ["startMm", "startDd"],
        cursor_field: "startMm",
    });
    mockFetchTransactionReport.mockRejectedValue(BuildAxiosError(HTTP_BAD_REQUEST, errorBody));
    const user = await RenderScreen();
    await PressScreenKey(user, ENTER_KEY, "click");
    await ExpectAlert(START_MONTH_INVALID_MESSAGE, "error");
    expect(DatePartInput(START_MONTH_PART)).toHaveAttribute("aria-invalid", "true");
    expect(DatePartInput(START_DAY_PART)).toHaveAttribute("aria-invalid", "true");
    expect(DatePartInput(START_YEAR_PART)).toHaveAttribute("aria-invalid", "false");
    await waitFor(() => expect(DatePartInput(START_MONTH_PART)).toHaveFocus());
});

// Nothing keyed while the call runs reaches the screen, so the answer always applies to the
// criteria that were sent; it unlocks them in the render that puts the cursor on its control.
it.each(LOCKED_CALL_CASES)(
    "CORPT00C locks the criteria while ENTER runs; $answer unlocks them and places the cursor",
    async ({ confirm, outcome, FocusedControl }) => {
        const pendingReport = DeferReport();
        const user = await RenderScreen();
        await user.click(ReportTypeButton(CUSTOM_LABEL));
        await FillDateParts(user, CUSTOM_DATE_PARTS);
        // user-event rejects an empty text, so a Backspace on the blank field leads every value.
        await user.type(ConfirmInput(), `{Backspace}${confirm}`);
        await PressScreenKey(user, ENTER_KEY, "key");
        ExpectKeyboardLocked();
        await TryToEditWhileLocked(confirm);
        await SettleOutcome(pendingReport, outcome);
        expect(screen.queryByRole("alert")?.textContent ?? "").toBe(outcome.literal);
        ExpectKeyboardUnlocked();
        expect(FocusedControl()).toHaveFocus();
        expect(mockFetchTransactionReport).toHaveBeenCalledTimes(1);
        expect(mockFetchTransactionReport).toHaveBeenCalledWith(
            { ...CUSTOM_KEYED_QUERY, confirm },
            READ_SIGNAL_OPTIONS,
        );
    },
);

it.each(FLAGGED_REPORT_TYPE_CASES)(
    "CORPT00C marks the Report type group invalid after $name",
    async ({ status, body, chosenLabel }) => {
        mockFetchTransactionReport.mockRejectedValue(BuildAxiosError(status, body));
        const user = await RenderScreen();
        if (chosenLabel !== null) {
            await user.click(ReportTypeButton(chosenLabel));
        }
        await PressScreenKey(user, ENTER_KEY, "click");
        await ExpectAlert(body.message, "error");
        expect(ReportTypeGroup()).toHaveAttribute("aria-invalid", "true");
        expect(ReportTypeGroup()).toHaveAttribute("aria-describedby", ERRMSG_ID);
        expect(ReportTypeGroup()).toHaveAccessibleDescription(body.message);
        ExpectErrorSegments(chosenLabel);
        await waitFor(() => expect(ReportTypeButton(MONTHLY_LABEL)).toHaveFocus());
    },
);

// The ERRMSG Alert comes first in the description, then the control's own hint; a part the
// answer does not name is described by nothing.
it.each(FLAGGED_CONTROL_CASES)(
    "CORPT00C describes $control by the ERRMSG line after $outcome.literal",
    async ({ outcome, FlaggedControl, hint }) => {
        MockReportOutcome(outcome);
        const user = await RenderScreen();
        await PressScreenKey(user, ENTER_KEY, "click");
        await ExpectAlert(outcome.literal, outcome.severity);
        const control = FlaggedControl();
        const hintIds = hint === null ? [] : [`${control.id}-helper-text`];
        const hintTexts = hint === null ? [] : [hint];
        expect(control).toHaveAttribute("aria-invalid", "true");
        expect(control).toHaveAttribute("aria-describedby", [ERRMSG_ID, ...hintIds].join(" "));
        expect(document.getElementById(ERRMSG_ID)).toBe(screen.getByRole("alert"));
        expect(control).toHaveAccessibleDescription([outcome.literal, ...hintTexts].join(" "));
        expect(DatePartInput(START_YEAR_PART)).not.toHaveAttribute("aria-describedby");
    },
);

it.each(UNFLAGGED_REPORT_TYPE_CASES)(
    "CORPT00C leaves the Report type group unflagged $name",
    async ({ outcomes }) => {
        const user = await RenderScreen();
        for (const outcome of outcomes) {
            await SubmitOutcome(user, outcome);
        }
        expect(ReportTypeGroup()).toHaveAttribute("aria-invalid", "false");
        expect(ReportTypeGroup()).not.toHaveAccessibleDescription();
        for (const label of REPORT_TYPE_LABELS) {
            expect(ReportTypeButton(label)).not.toHaveStyle({
                borderColor: CARD_DEMO_THEME.palette.error.main,
            });
        }
    },
);

it("CORPT00C confirm N clears every field and the message (INITIALIZE-ALL-FIELDS)", async () => {
    mockFetchTransactionReport.mockResolvedValue(
        BuildReportResponse({ start_date: "", end_date: "", message: "", lines: [] }),
    );
    const user = await RenderScreen();
    await user.click(ReportTypeButton(CUSTOM_LABEL));
    await FillDateParts(user, CUSTOM_DATE_PARTS);
    await user.type(ConfirmInput(), "N");
    await PressScreenKey(user, ENTER_KEY, "click");
    await waitFor(() => expect(ConfirmInput()).toHaveValue(""));
    for (const part of DATE_PARTS) {
        expect(DatePartInput(part)).toHaveValue("");
    }
    expect(ReportTypeButton(CUSTOM_LABEL)).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
});

it("CORPT00C Y shows the CBTRN03C report below the cleared form", async () => {
    await ShowMonthlyReport();
    await ExpectAlert(MONTHLY_SUBMITTED_MESSAGE, "success");
    expect(screen.getByRole("heading", { level: 2, name: DATE_RANGE_HEADING })).toBeInTheDocument();
    const table = screen.getByRole("table", { name: REPORT_TABLE_LABEL });
    const headings = within(table).getAllByRole("columnheader");
    expect(headings.map((heading) => heading.textContent)).toEqual([...TABLE_HEADINGS]);
    expect(within(table).getAllByRole("row")).toHaveLength(REPORT_LINES.length + 1);
    expect(ConfirmInput()).toHaveValue("");
    expect(ReportTypeButton(MONTHLY_LABEL)).toHaveAttribute("aria-pressed", "false");
    await waitFor(() => expect(ReportTypeButton(MONTHLY_LABEL)).toHaveFocus());
});

it("CORPT00C empty confirmed range shows its range and both downloads, no table", async () => {
    await ShowEmptyCustomReport();
    expect(mockFetchTransactionReport).toHaveBeenCalledWith(
        EMPTY_RANGE_CONFIRMED_QUERY,
        READ_SIGNAL_OPTIONS,
    );
    expect(
        screen.getByRole("heading", { level: 2, name: EMPTY_RANGE_HEADING }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    ExpectCompleteDownloads(EMPTY_RANGE_CONFIRMED_QUERY);
});

it.each(LINE_KIND_CASES)(
    "CBTRN03C $kind line renders its cells on /reports/transactions",
    async ({ position, cells, isFooter }) => {
        await ShowMonthlyReport();
        const rowCells = within(ReportBodyRow(position)).getAllByRole("cell");
        expect(rowCells.map((cell) => cell.textContent)).toEqual([...cells]);
        for (const cell of rowCells) {
            expect(cell.classList.contains(tableCellClasses.footer)).toBe(isFooter);
        }
    },
);

it.each(DOWNLOAD_CASES)(
    "CORPT00C report download as %s repeats the confirmed query",
    async (format, linkName) => {
        await ShowMonthlyReport();
        const fetchedQuery = mockFetchTransactionReport.mock.calls[0]?.[0];
        expect(fetchedQuery).toStrictEqual(MONTHLY_CONFIRMED_QUERY);
        expect(mockBuildReportExportUrl).toHaveBeenCalledWith(fetchedQuery, format);
        const link = screen.getByRole("link", { name: linkName });
        expect(link).toHaveAttribute("href", EXPORT_URL_PREFIX + format);
        expect(link).toHaveAttribute("download");
    },
);

it.each(LONG_REPORT_PAGE_CASES)(
    "CORPT00C long report page $rangeLabel shows its lines in print order, downloads whole",
    async ({ nextPageClicks, firstPosition, lineCount, rangeLabel }) => {
        const user = await ShowMonthlyReport(LONG_REPORT_LINES);
        for (let click = 0; click < nextPageClicks; click += 1) {
            await PressNextPage(user);
        }
        expect(screen.getByText(rangeLabel)).toBeInTheDocument();
        const table = screen.getByRole("table", { name: REPORT_TABLE_LABEL });
        expect(within(table).getAllByRole("columnheader")).toHaveLength(TABLE_HEADINGS.length);
        expect(ShownTranIds()).toEqual(LongReportTranIds(firstPosition, lineCount));
        ExpectCompleteDownloads(MONTHLY_CONFIRMED_QUERY);
    },
);

it.each(ONE_PAGE_LINE_COUNTS)(
    "CORPT00C report of %i lines fits one page and shows no pagination",
    async (lineCount) => {
        await ShowMonthlyReport(BuildDetailLines(lineCount));
        expect(ShownTranIds()).toHaveLength(lineCount);
        const nextPageButton = screen.queryByRole("button", { name: NEXT_PAGE_BUTTON_NAME });
        expect(nextPageButton).not.toBeInTheDocument();
    },
);

it("CORPT00C a new report opens on its first page", async () => {
    const user = await ShowMonthlyReport(LONG_REPORT_LINES);
    await PressNextPage(user);
    expect(screen.getByText(SECOND_PAGE_CASE.rangeLabel)).toBeInTheDocument();
    mockFetchTransactionReport.mockResolvedValueOnce(
        BuildReportResponse({ lines: [...LONG_REPORT_LINES] }),
    );
    await SubmitMonthlyReport(user);
    await waitFor(() => expect(mockFetchTransactionReport).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(FIRST_PAGE_CASE.rangeLabel)).toBeInTheDocument();
    expect(ShownTranIds()).toEqual(LongReportTranIds(0, FIRST_PAGE_CASE.lineCount));
    ExpectCompleteDownloads(MONTHLY_CONFIRMED_QUERY);
});

it("CORPT00C criteria edits do not render the shown report again", async () => {
    const user = await ShowMonthlyReport(LONG_REPORT_LINES);
    mockBuildReportExportUrl.mockClear();
    await user.click(ReportTypeButton(CUSTOM_LABEL));
    await FillDateParts(user, EMPTY_RANGE_DATE_PARTS);
    expect(mockBuildReportExportUrl).not.toHaveBeenCalled();
    expect(ShownTranIds()).toEqual(LongReportTranIds(0, FIRST_PAGE_CASE.lineCount));
});

it.each(KEY_ENTRIES)("CORPT00C F3 by %s returns to the main menu", async (entry) => {
    const user = await RenderScreen();
    await PressScreenKey(user, F3_KEY, entry);
    expect(mockRouterPush).toHaveBeenCalledTimes(1);
    expect(mockRouterPush).toHaveBeenCalledWith(MENU_ROUTE);
    expect(mockRouterReplace).not.toHaveBeenCalled();
    expect(mockFetchTransactionReport).not.toHaveBeenCalled();
});

// The apiClient interceptor owns the 401 redirect to sign-on; the mocked API bypasses it, so the
// page must neither route nor show a message for the empty 401 body the backend sends.
it("CORPT00C 401 leaves the sign-on redirect to the apiClient interceptor", async () => {
    mockFetchTransactionReport.mockRejectedValue(
        BuildAxiosError(HTTP_UNAUTHORIZED, BuildErrorBody()),
    );
    const user = await RenderScreen();
    await PressScreenKey(user, ENTER_KEY, "click");
    await WaitForRequestEnd();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(mockRouterPush).not.toHaveBeenCalled();
    expect(mockRouterReplace).not.toHaveBeenCalled();
    expect(SIGN_ON_ROUTE).toBe("/signon");
});

// The abandoned read's CanceledError shows nothing on the screen still mounted after F3.
it.each(LEAVE_CASES)(
    "CORPT00C $leaving during a report read abandons the read without a message",
    async ({ LeaveScreen, routes }) => {
        mockFetchTransactionReport.mockImplementation(PendUntilAborted);
        const user = await RenderScreen();
        await PressScreenKey(user, ENTER_KEY, "click");
        await screen.findByRole("progressbar");
        expect(FirstReadSignal()?.aborted).toBe(false);
        await LeaveScreen(user);
        expect(FirstReadSignal()?.aborted).toBe(true);
        expect(mockRouterPush.mock.calls).toEqual(routes.map((route) => [route]));
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
        expect(mockFetchTransactionReport).toHaveBeenCalledTimes(1);
    },
);

// A cancelled read is discarded before ToScreenFeedback, which would show the abend text: the
// report shown keeps its message, its date range and its lines, and ENTER is active again.
it("CORPT00C discards a cancelled report read and keeps the report shown", async () => {
    const user = await ShowMonthlyReport();
    mockFetchTransactionReport.mockRejectedValue(new CanceledError());
    await PressScreenKey(user, ENTER_KEY, "click");
    await waitFor(() => expect(mockFetchTransactionReport).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole("progressbar")).not.toBeInTheDocument());
    await ExpectAlert(MONTHLY_SUBMITTED_MESSAGE, "success");
    expect(screen.getByRole("heading", { level: 2, name: DATE_RANGE_HEADING })).toBeInTheDocument();
    const table = screen.getByRole("table", { name: REPORT_TABLE_LABEL });
    expect(within(table).getAllByRole("row")).toHaveLength(REPORT_LINES.length + 1);
    expect(screen.getByRole("button", { name: ENTER_KEY.buttonName })).toBeEnabled();
});
