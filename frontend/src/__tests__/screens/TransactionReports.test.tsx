// Screen tests: BMS CORPT00, program CORPT00C with CBTRN03C lines (CR00),
// page /reports/transactions.
import { alertClasses, inputBaseClasses, inputLabelClasses, tableCellClasses } from "@mui/material";
import { ThemeProvider } from "@mui/material/styles";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
    DownloadReportExport,
    ExportFormat,
    FetchTransactionReport,
    ReportLineKind,
    ReportType,
    type ReportExportFile,
    type ReportLine,
    type ReportQuery,
    type TransactionReportResponse,
} from "@/lib/api/reports";
import {
    ABEND_MESSAGE,
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
    DownloadReportExport: jest.fn(),
}));

const mockFetchTransactionReport = jest.mocked(FetchTransactionReport);
const mockBuildReportExportUrl = jest.mocked(BuildReportExportUrl);
const mockDownloadReportExport = jest.mocked(DownloadReportExport);

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
const REPORT_LINES_REGION_LABEL = "Transaction report lines";
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
const START_MONTH_EMPTY_MESSAGE = "Start Date - Month can NOT be empty...";
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

// FD-REPTFILE-REC PIC X(133): every CBTRN03C print line is padded with blanks to 133 columns.
const PRINT_LINE_COLUMNS = 133;
// The four lines of CBTRN03C 1120-WRITE-HEADERS, as cbtrn03c.py prints them for the Monthly
// range: their inner and trailing blanks must all reach the screen.
interface HeaderLineCase {
    name: string;
    position: number;
    text: string;
}

const HEADER_LINE_CASES: readonly HeaderLineCase[] = [
    {
        name: "REPORT-NAME-HEADER",
        position: 0,
        text: ["DALYREPT".padEnd(38), "Daily Transaction Report".padEnd(41), DATE_RANGE_HEADING]
            .join("")
            .padEnd(PRINT_LINE_COLUMNS),
    },
    { name: "WS-BLANK-LINE", position: 1, text: " ".repeat(PRINT_LINE_COLUMNS) },
    {
        name: "TRANSACTION-HEADER-1",
        position: 2,
        text: [
            "Transaction ID   ",
            "Account ID  ",
            "Transaction Type   ",
            "Tran Category                      ",
            "Tran Source            ",
            "Amount",
        ]
            .join("")
            .padEnd(PRINT_LINE_COLUMNS),
    },
    { name: "TRANSACTION-HEADER-2", position: 3, text: "-".repeat(PRINT_LINE_COLUMNS) },
];
const HEADER_LINES: readonly ReportLine[] = HEADER_LINE_CASES.map(({ text }) =>
    BuildReportLine({ line_kind: ReportLineKind.HEADER, text }),
);
// A print line keeps every blank, never wraps and sets its columns in Courier, as the PDF does.
const PRINT_LINE_CELL_STYLE = {
    fontFamily: '"Courier New", Courier, monospace',
    whiteSpace: "pre",
};

// One CORPT00C outcome: the literal, its HTTP status, the message-line severity, the field the
// cursor goes to and, for a 200, the report lines returned with it. isCleared marks the screen
// INITIALIZE-ALL-FIELDS leaves after confirm N, which corpt00c answers with blank dates.
interface LiteralCase {
    literal: string;
    status: number;
    severity: MessageSeverity;
    cursorField: string;
    lines: readonly ReportLine[];
    isCleared?: boolean;
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
    EditFailure(START_MONTH_EMPTY_MESSAGE, "startMm"),
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
    // Confirm N clears every field, both dates and the message (INITIALIZE-ALL-FIELDS).
    {
        answer: "the confirm N reset",
        confirm: "N",
        outcome: {
            literal: "",
            status: HTTP_OK,
            severity: "info",
            cursorField: "reportType",
            lines: [],
            isCleared: true,
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

const PREVIOUS_PAGE_BUTTON_NAME = "Go to previous page";

// The page arrows pressed in turn through the three-page long report, the page they end on and
// the arrow that then holds the focus: an arrow that still pages keeps it, and one its own press
// disables on the first or the last page hands it to the other.
interface PageArrowFocusCase {
    pressedArrows: readonly string[];
    rangeLabel: string;
    focusedArrow: string;
}

const PAGE_ARROW_FOCUS_CASES: readonly PageArrowFocusCase[] = [
    {
        pressedArrows: [NEXT_PAGE_BUTTON_NAME],
        rangeLabel: SECOND_PAGE_CASE.rangeLabel,
        focusedArrow: NEXT_PAGE_BUTTON_NAME,
    },
    {
        pressedArrows: [NEXT_PAGE_BUTTON_NAME, NEXT_PAGE_BUTTON_NAME],
        rangeLabel: "101–103 of 103",
        focusedArrow: PREVIOUS_PAGE_BUTTON_NAME,
    },
    {
        pressedArrows: [NEXT_PAGE_BUTTON_NAME, PREVIOUS_PAGE_BUTTON_NAME],
        rangeLabel: FIRST_PAGE_CASE.rangeLabel,
        focusedArrow: NEXT_PAGE_BUTTON_NAME,
    },
];

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

// An ENTER outcome that names one text control, that control, its own hint and the hint's id.
interface FlaggedControlCase {
    control: string;
    outcome: LiteralCase;
    FlaggedControl: () => HTMLElement;
    hint: string;
    HintId: (control: HTMLElement) => string;
}

// MUI TextField names a field's helper text by the field's id.
function HelperTextId(control: HTMLElement): string {
    return `${control.id}-helper-text`;
}

// The date edits and the invalid-confirm edit name their field in field_errors; the blank-confirm
// prompt names CONFIRM as its cursor. A date part keeps its row's (MM/DD/YYYY) hint, and CONFIRM
// its Y/N hint, after the ERRMSG line.
const FLAGGED_CONTROL_CASES: readonly FlaggedControlCase[] = [
    {
        control: "the start month",
        outcome: EditFailure(START_MONTH_INVALID_MESSAGE, "startMm"),
        FlaggedControl: () => DatePartInput(START_MONTH_PART),
        hint: DATE_HINT,
        HintId: () => DateHint(START_DATE_LEGEND).id,
    },
    {
        control: "CONFIRM",
        outcome: EditFailure('"X" is not a valid value to confirm...', "confirm"),
        FlaggedControl: () => ConfirmInput(),
        hint: "Y/N",
        HintId: HelperTextId,
    },
    {
        control: "CONFIRM",
        outcome: PromptOutcome("Please confirm to print the Monthly report..."),
        FlaggedControl: () => ConfirmInput(),
        hint: "Y/N",
        HintId: HelperTextId,
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

// The 200 body of an outcome. The prompt and a submitted report carry the range; the cleared
// screen carries blank dates, as corpt00c's ClearedResponse sends them.
function OutcomeResponse(outcome: LiteralCase): TransactionReportResponse {
    const { literal, cursorField, lines, isCleared } = outcome;
    const dates = isCleared === true ? { start_date: "", end_date: "" } : {};
    return BuildReportResponse({
        ...dates,
        message: literal,
        cursor_field: cursorField,
        lines: [...lines],
    });
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

// The (MM/DD/YYYY) hint of one date row.
function DateHint(legend: string): HTMLElement {
    return within(screen.getByRole("group", { name: legend })).getByText(DATE_HINT);
}

// A date part the server does not name is described by its own row's hint alone.
function ExpectDescribedByDateHintOnly(part: DatePartCase): void {
    const hintId = DateHint(part.legend).id;
    expect(hintId).not.toBe("");
    expect(DatePartInput(part)).toHaveAttribute("aria-describedby", hintId);
    expect(DatePartInput(part)).toHaveAccessibleDescription(DATE_HINT);
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

// The group reads as valid, is described by nothing and shows no error border.
function ExpectReportTypeUnflagged(): void {
    expect(ReportTypeGroup()).toHaveAttribute("aria-invalid", "false");
    expect(ReportTypeGroup()).not.toHaveAccessibleDescription();
    for (const label of REPORT_TYPE_LABELS) {
        expect(ReportTypeButton(label)).not.toHaveStyle({
            borderColor: CARD_DEMO_THEME.palette.error.main,
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

// MUI's focus state matches the browser focus: the input shows the focused outline and its label
// floats above the keyed text.
function ExpectFocusedField(input: HTMLElement): void {
    expect(input).toHaveFocus();
    expect(input.closest(`.${inputBaseClasses.root}`)).toHaveClass(inputBaseClasses.focused);
    const label = document.querySelector(`label[for="${input.id}"]`);
    expect(label).toHaveClass(inputLabelClasses.focused, inputLabelClasses.shrink);
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
    mockDownloadReportExport.mockReset();
});

// The download tests stub the object-URL functions jsdom lacks and record the saving link's
// click; each test removes them again and returns to real timers.
afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
    for (const name of OBJECT_URL_FUNCTIONS) {
        Reflect.deleteProperty(URL, name);
    }
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

interface EnterPressCase {
    entry: string;
    PressEnter: (user: UserEvent) => Promise<void>;
}

// ENTER by its button, by the key from the start month part, and by the key on MONTHLY, which
// holds the cursor on first display: Enter on a segment is the map's ENTER and chooses nothing.
const NO_TYPE_ENTER_CASES: readonly EnterPressCase[] = [
    ...KEY_ENTRIES.map((entry) => ({
        entry,
        PressEnter: (user: UserEvent) => PressScreenKey(user, ENTER_KEY, entry),
    })),
    {
        entry: "key on the focused MONTHLY",
        PressEnter: (user: UserEvent) => PressKey(user, ENTER_KEY.keyName),
    },
];

it.each(NO_TYPE_ENTER_CASES)(
    "CORPT00C ENTER by $entry with no report type sends no reportType",
    async ({ PressEnter }) => {
        MockReportOutcome(EditFailure(SELECT_REPORT_TYPE_MESSAGE, "reportType"));
        const user = await RenderScreen();
        await PressEnter(user);
        await ExpectAlert(SELECT_REPORT_TYPE_MESSAGE, "error");
        expect(MonthlyButton()).toHaveAttribute("aria-pressed", "false");
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

async function PressSegmentBySpace(user: UserEvent, label: string): Promise<void> {
    await act(async () => {
        ReportTypeButton(label).focus();
    });
    await user.keyboard("[Space]");
}

interface SegmentPressCase {
    press: string;
    PressSegment: (user: UserEvent, label: string) => Promise<void>;
}

// A report type is pressed by pointer, or by Space on the segment holding the cursor; neither
// sends anything.
const SEGMENT_PRESS_CASES: readonly SegmentPressCase[] = [
    { press: "click", PressSegment: (user, label) => user.click(ReportTypeButton(label)) },
    { press: "Space", PressSegment: PressSegmentBySpace },
];

it.each(SEGMENT_PRESS_CASES)(
    "CORPT00C pressing the chosen report type again by $press clears the selection",
    async ({ PressSegment }) => {
        MockReportOutcome(EditFailure(SELECT_REPORT_TYPE_MESSAGE, "reportType"));
        const user = await RenderScreen();
        await PressSegment(user, YEARLY_LABEL);
        expect(ReportTypeButton(YEARLY_LABEL)).toHaveAttribute("aria-pressed", "true");
        await PressSegment(user, YEARLY_LABEL);
        expect(ReportTypeButton(YEARLY_LABEL)).toHaveAttribute("aria-pressed", "false");
        expect(mockFetchTransactionReport).not.toHaveBeenCalled();
        await PressScreenKey(user, ENTER_KEY, "click");
        await ExpectAlert(SELECT_REPORT_TYPE_MESSAGE, "error");
        expect(mockFetchTransactionReport).toHaveBeenCalledWith(
            expect.not.objectContaining({ reportType: expect.anything() }),
            READ_SIGNAL_OPTIONS,
        );
    },
);

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

const YEARLY_CONFIRMED_QUERY: ReportQuery = {
    ...BLANK_TEXT_QUERY,
    reportType: ReportType.YEARLY,
    confirm: "Y",
};
const YEARLY_SUBMITTED_MESSAGE = "Yearly report submitted for printing ...";
// corpt00c's SubmittedResponse for Yearly in 2022: 1 January to 31 December, with the lines.
const YEARLY_SUBMITTED_RESPONSE: TransactionReportResponse = BuildReportResponse({
    start_date: "2022-01-01",
    end_date: "2022-12-31",
    message: YEARLY_SUBMITTED_MESSAGE,
    cursor_field: "reportType",
});
const YEARLY_RANGE_HEADING = "Date Range: 2022-01-01 to 2022-12-31";
// An invalid CONFIRM in either case, so neither case folding nor filtering can pass unseen.
const INVALID_CONFIRM_VALUES: readonly string[] = ["X", "x"];

// corpt00c's invalid-CONFIRM literal, which echoes the value as keyed.
function InvalidConfirmMessage(confirm: string): string {
    return `"${confirm}" is not a valid value to confirm...`;
}

// corpt00c's 400 for an invalid CONFIRM, with the cursor on CONFIRM.
function InvalidConfirmError(confirm: string): AxiosError {
    const errorBody = BuildErrorBody({
        message: InvalidConfirmMessage(confirm),
        field_errors: ["confirm"],
        cursor_field: "confirm",
    });
    return BuildAxiosError(HTTP_BAD_REQUEST, errorBody);
}

it("CORPT00C ENTER sends Yearly confirmed with Y and shows the year's range", async () => {
    mockFetchTransactionReport.mockResolvedValue(YEARLY_SUBMITTED_RESPONSE);
    const user = await RenderScreen();
    await user.click(ReportTypeButton(YEARLY_LABEL));
    await user.type(ConfirmInput(), "Y");
    await PressScreenKey(user, ENTER_KEY, "click");
    await ExpectAlert(YEARLY_SUBMITTED_MESSAGE, "success");
    expect(mockFetchTransactionReport).toHaveBeenCalledTimes(1);
    expect(mockFetchTransactionReport.mock.calls[0]?.[0]).toStrictEqual(YEARLY_CONFIRMED_QUERY);
    expect(mockFetchTransactionReport).toHaveBeenCalledWith(
        YEARLY_CONFIRMED_QUERY,
        READ_SIGNAL_OPTIONS,
    );
    expect(
        screen.getByRole("heading", { level: 2, name: YEARLY_RANGE_HEADING }),
    ).toBeInTheDocument();
    expect(screen.getByRole("table", { name: REPORT_TABLE_LABEL })).toBeInTheDocument();
});

it.each(INVALID_CONFIRM_VALUES)(
    "CORPT00C ENTER sends CONFIRM %s as keyed and shows its invalid-value literal",
    async (confirm) => {
        mockFetchTransactionReport.mockRejectedValue(InvalidConfirmError(confirm));
        const user = await RenderScreen();
        await user.click(MonthlyButton());
        await user.type(ConfirmInput(), confirm);
        await PressScreenKey(user, ENTER_KEY, "click");
        await ExpectAlert(InvalidConfirmMessage(confirm), "error");
        expect(mockFetchTransactionReport.mock.calls[0]?.[0]).toStrictEqual({
            ...BLANK_TEXT_QUERY,
            reportType: ReportType.MONTHLY,
            confirm,
        });
        expect(ConfirmInput()).toHaveValue(confirm);
        expect(ConfirmInput()).toHaveAttribute("aria-invalid", "true");
        expect(MonthlyButton()).toHaveAttribute("aria-pressed", "true");
        await waitFor(() => expect(ConfirmInput()).toHaveFocus());
    },
);

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

// The lock disables the blank Start MM part ENTER is pressed in, which keeps the browser focus
// through the call; the answer naming that part gives it back its focused outline and label.
it("CORPT00C an edit failure naming the part ENTER was pressed in keeps it focused", async () => {
    MockReportOutcome(EditFailure(START_MONTH_EMPTY_MESSAGE, "startMm"));
    const user = await RenderScreen();
    await user.click(ReportTypeButton(CUSTOM_LABEL));
    await PressScreenKey(user, ENTER_KEY, "key");
    await ExpectAlert(START_MONTH_EMPTY_MESSAGE, "error");
    expect(mockFetchTransactionReport).toHaveBeenCalledTimes(1);
    ExpectFocusedField(DatePartInput(START_MONTH_PART));
});

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

// Each date part is described by its own row's (MM/DD/YYYY) hint from the first display.
it.each(DATE_PARTS)(
    "CORPT00 $name is described by the (MM/DD/YYYY) hint of the $legend row",
    async (part) => {
        await RenderScreen();
        ExpectDescribedByDateHintOnly(part);
    },
);

// The ERRMSG Alert comes first in the description, then the control's own hint; a part the
// answer does not name is described by its row's hint alone.
it.each(FLAGGED_CONTROL_CASES)(
    "CORPT00C describes $control by the ERRMSG line after $outcome.literal",
    async ({ outcome, FlaggedControl, hint, HintId }) => {
        MockReportOutcome(outcome);
        const user = await RenderScreen();
        await PressScreenKey(user, ENTER_KEY, "click");
        await ExpectAlert(outcome.literal, outcome.severity);
        const control = FlaggedControl();
        expect(control).toHaveAttribute("aria-invalid", "true");
        expect(control).toHaveAttribute("aria-describedby", `${ERRMSG_ID} ${HintId(control)}`);
        expect(document.getElementById(ERRMSG_ID)).toBe(screen.getByRole("alert"));
        expect(control).toHaveAccessibleDescription(`${outcome.literal} ${hint}`);
        ExpectDescribedByDateHintOnly(START_YEAR_PART);
    },
);

it.each(UNFLAGGED_REPORT_TYPE_CASES)(
    "CORPT00C leaves the Report type group unflagged $name",
    async ({ outcomes }) => {
        const user = await RenderScreen();
        for (const outcome of outcomes) {
            await SubmitOutcome(user, outcome);
        }
        ExpectReportTypeUnflagged();
    },
);

// A choice made after the flag answers it, while the message line keeps the literal; the next
// ENTER outcome that names the report type flags the group again.
it.each(FLAGGED_REPORT_TYPE_CASES)(
    "CORPT00C a choice after $name clears the Report type flag until ENTER flags it again",
    async ({ status, body, chosenLabel }) => {
        mockFetchTransactionReport.mockRejectedValue(BuildAxiosError(status, body));
        const user = await RenderScreen();
        if (chosenLabel !== null) {
            await user.click(ReportTypeButton(chosenLabel));
        }
        await PressScreenKey(user, ENTER_KEY, "click");
        await ExpectAlert(body.message, "error");
        expect(ReportTypeGroup()).toHaveAttribute("aria-invalid", "true");
        await user.click(ReportTypeButton(YEARLY_LABEL));
        ExpectReportTypeUnflagged();
        await ExpectAlert(body.message, "error");
        await PressScreenKey(user, ENTER_KEY, "click");
        await waitFor(() => expect(ReportTypeGroup()).toHaveAttribute("aria-invalid", "true"));
        expect(mockFetchTransactionReport).toHaveBeenCalledTimes(2);
        ExpectErrorSegments(YEARLY_LABEL);
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

// Any range heading, the old one or one built from another answer's dates.
const DATE_RANGE_HEADING_PATTERN = /^Date Range: /;

// The ClearedResponse corpt00c sends for confirm N: no dates, no message, no lines.
const CONFIRM_N_CLEARED_RESPONSE: TransactionReportResponse = BuildReportResponse({
    start_date: "",
    end_date: "",
    message: "",
    cursor_field: "reportType",
    lines: [],
});

// The report ShowMonthlyReport put on screen: its range heading, its table and both downloads.
function ExpectMonthlyReportShown(): void {
    expect(screen.getByRole("heading", { level: 2, name: DATE_RANGE_HEADING })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: REPORT_TABLE_LABEL })).toBeInTheDocument();
    for (const [, linkName] of DOWNLOAD_CASES) {
        expect(screen.getByRole("link", { name: linkName })).toBeInTheDocument();
    }
}

// Nothing of a report is left to read or download.
function ExpectReportRemoved(): void {
    const rangeHeading = screen.queryByRole("heading", { name: DATE_RANGE_HEADING_PATTERN });
    expect(rangeHeading).not.toBeInTheDocument();
    expect(screen.queryByRole("table", { name: REPORT_TABLE_LABEL })).not.toBeInTheDocument();
    for (const [, linkName] of DOWNLOAD_CASES) {
        expect(screen.queryByRole("link", { name: linkName })).not.toBeInTheDocument();
    }
}

// The map as INITIALIZE-ALL-FIELDS leaves it: no report type pressed, every date part and
// CONFIRM blank.
function ExpectCriteriaCleared(): void {
    for (const label of REPORT_TYPE_LABELS) {
        expect(ReportTypeButton(label)).toHaveAttribute("aria-pressed", "false");
    }
    for (const part of DATE_PARTS) {
        expect(DatePartInput(part)).toHaveValue("");
    }
    expect(ConfirmInput()).toHaveValue("");
}

// An ENTER refused after a shown report: the keys pressed on the cleared form, the query they
// send and the CORPT00C outcome answering it.
interface RefusalAfterReportCase {
    name: string;
    KeyAndSubmit: (user: UserEvent) => Promise<void>;
    query: ReportQuery;
    outcome: LiteralCase;
}

async function SubmitMonthlyUnconfirmed(user: UserEvent): Promise<void> {
    await user.click(MonthlyButton());
    await PressScreenKey(user, ENTER_KEY, "click");
}

const REFUSALS_AFTER_REPORT: readonly RefusalAfterReportCase[] = [
    {
        name: "the no-selection edit (400) on the cleared form",
        KeyAndSubmit: (user) => PressScreenKey(user, ENTER_KEY, "click"),
        query: BLANK_TEXT_QUERY,
        outcome: EditFailure(SELECT_REPORT_TYPE_MESSAGE, "reportType"),
    },
    {
        name: "the blank-confirm prompt (200) for Monthly",
        KeyAndSubmit: SubmitMonthlyUnconfirmed,
        query: { ...BLANK_TEXT_QUERY, reportType: ReportType.MONTHLY },
        outcome: PromptOutcome("Please confirm to print the Monthly report..."),
    },
    {
        name: "the file error (503) for Monthly confirmed again",
        KeyAndSubmit: SubmitMonthlyReport,
        query: MONTHLY_CONFIRMED_QUERY,
        outcome: {
            literal: "Unable to Write TDQ (JOBS)...",
            status: HTTP_SERVICE_UNAVAILABLE,
            severity: "error",
            cursorField: "reportType",
            lines: [],
        },
    },
];

it("CORPT00C confirm N after a shown report clears the report with every field", async () => {
    const user = await ShowMonthlyReport();
    ExpectMonthlyReportShown();
    mockFetchTransactionReport.mockResolvedValue(CONFIRM_N_CLEARED_RESPONSE);
    await user.click(ReportTypeButton(CUSTOM_LABEL));
    await FillDateParts(user, CUSTOM_DATE_PARTS);
    await user.type(ConfirmInput(), "N");
    await PressScreenKey(user, ENTER_KEY, "click");
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(mockFetchTransactionReport).toHaveBeenCalledTimes(2);
    ExpectReportRemoved();
    ExpectCriteriaCleared();
    await waitFor(() => expect(MonthlyButton()).toHaveFocus());
});

it.each(REFUSALS_AFTER_REPORT)(
    "CORPT00C $name removes the shown report and its downloads",
    async ({ KeyAndSubmit, query, outcome }) => {
        const user = await ShowMonthlyReport();
        ExpectMonthlyReportShown();
        MockReportOutcome(outcome);
        await KeyAndSubmit(user);
        await screen.findByText(outcome.literal);
        await ExpectAlert(outcome.literal, outcome.severity);
        ExpectReportRemoved();
        expect(mockFetchTransactionReport).toHaveBeenCalledTimes(2);
        expect(mockFetchTransactionReport.mock.calls[1]?.[0]).toStrictEqual(query);
    },
);

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

it.each(HEADER_LINE_CASES)(
    "CBTRN03C $name keeps its print text in one fixed-pitch cell across every column",
    async ({ position, text }) => {
        await ShowMonthlyReport(HEADER_LINES);
        const rowCells = within(ReportBodyRow(position)).getAllByRole("cell");
        expect(rowCells).toHaveLength(1);
        expect(rowCells[0]?.textContent).toBe(text);
        expect(rowCells[0]).toHaveAttribute("colspan", String(TABLE_HEADINGS.length));
        expect(rowCells[0]).toHaveStyle(PRINT_LINE_CELL_STYLE);
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

// The table scrolls inside its container, which the keyboard reaches after CONFIRM as a named tab
// stop.
it("CORPT00C report table scrolls in a named region, the tab stop after CONFIRM", async () => {
    const user = await ShowMonthlyReport();
    const region = screen.getByRole("region", { name: REPORT_LINES_REGION_LABEL });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region).toContainElement(screen.getByRole("table", { name: REPORT_TABLE_LABEL }));
    await user.click(ConfirmInput());
    await user.tab();
    expect(region).toHaveFocus();
});

// MUI's start-icon wrapper puts a zero-width space ahead of each link's text, so each link is
// named by its label itself rather than by its content.
it.each(DOWNLOAD_CASES)(
    "CORPT00C report download as %s link is named %s alone",
    async (_format, linkName) => {
        await ShowMonthlyReport();
        const link = screen.getByText(linkName).closest("a");
        expect(link).toHaveAttribute("aria-label", linkName);
        expect(link).toHaveAccessibleName(linkName);
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

it.each(PAGE_ARROW_FOCUS_CASES)(
    "CORPT00C paging to $rangeLabel leaves the focus on $focusedArrow",
    async ({ pressedArrows, rangeLabel, focusedArrow }) => {
        const user = await ShowMonthlyReport(LONG_REPORT_LINES);
        for (const arrowName of pressedArrows) {
            await user.click(screen.getByRole("button", { name: arrowName }));
        }
        expect(screen.getByText(rangeLabel)).toBeInTheDocument();
        await waitFor(() =>
            expect(screen.getByRole("button", { name: focusedArrow })).toHaveFocus(),
        );
        expect(screen.getByRole("button", { name: focusedArrow })).toBeEnabled();
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

// ENTER drops the report it supersedes as its read starts, so no render holds both reports; the
// answer then opens on its first page.
it("CORPT00C a new ENTER removes the shown report while its read runs", async () => {
    const user = await ShowMonthlyReport(LONG_REPORT_LINES);
    await PressNextPage(user);
    expect(screen.getByText(SECOND_PAGE_CASE.rangeLabel)).toBeInTheDocument();
    const pendingReport = DeferReport();
    await SubmitMonthlyReport(user);
    await screen.findByRole("progressbar");
    ExpectReportRemoved();
    await act(async () => {
        pendingReport.Answer(BuildReportResponse({ lines: [...LONG_REPORT_LINES] }));
    });
    expect(await screen.findByText(FIRST_PAGE_CASE.rangeLabel)).toBeInTheDocument();
    expect(ShownTranIds()).toEqual(LongReportTranIds(0, FIRST_PAGE_CASE.lineCount));
    expect(mockFetchTransactionReport).toHaveBeenCalledTimes(2);
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
// message line keeps its message and ENTER is active again. The read had already removed the
// report it was to replace.
it("CORPT00C discards a cancelled report read and keeps the message shown", async () => {
    const user = await ShowMonthlyReport();
    mockFetchTransactionReport.mockRejectedValue(new CanceledError());
    await PressScreenKey(user, ENTER_KEY, "click");
    await waitFor(() => expect(mockFetchTransactionReport).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole("progressbar")).not.toBeInTheDocument());
    await ExpectAlert(MONTHLY_SUBMITTED_MESSAGE, "success");
    ExpectReportRemoved();
    expect(screen.getByRole("button", { name: ENTER_KEY.buttonName })).toBeEnabled();
});

// The CR00 downloads. Activating a link runs DownloadReportExport, the API-client wrapper, with
// the shown report's confirmed query, and the page saves the file the server names. jsdom has no
// object URLs and does not navigate, so the probe stubs both object-URL functions and records the
// saving link's click instead of following it.
const OBJECT_URL_FUNCTIONS = ["createObjectURL", "revokeObjectURL"] as const;
const SAVED_FILE_URL = "blob:http://127.0.0.1:8001/2f6c1d0e-cr00-report";
const EXPORT_FILE_NAMES: Readonly<Record<ExportFormat, string>> = {
    [ExportFormat.CSV]: "TRANREPT_2022-07-01_2022-07-31.csv",
    [ExportFormat.PDF]: "TRANREPT_2022-07-01_2022-07-31.pdf",
};
const CSV_LINK_NAME = "Download CSV";
const PDF_LINK_NAME = "Download PDF";

interface FileSavingProbe {
    createObjectUrl: jest.Mock;
    revokeObjectUrl: jest.Mock;
    linkClick: jest.SpyInstance;
}

function ProbeFileSaving(): FileSavingProbe {
    const createObjectUrl = jest.fn(() => SAVED_FILE_URL);
    const revokeObjectUrl = jest.fn();
    for (const [name, value] of [
        [OBJECT_URL_FUNCTIONS[0], createObjectUrl],
        [OBJECT_URL_FUNCTIONS[1], revokeObjectUrl],
    ] as const) {
        Object.defineProperty(URL, name, { configurable: true, writable: true, value });
    }
    const linkClick = jest.spyOn(HTMLAnchorElement.prototype, "click");
    linkClick.mockImplementation(() => undefined);
    return { createObjectUrl, revokeObjectUrl, linkClick };
}

function BuildExportFile(format: ExportFormat): ReportExportFile {
    return { fileName: EXPORT_FILE_NAMES[format], content: new Blob([`TRANREPT ${format}`]) };
}

function DownloadLink(linkName: string): HTMLElement {
    return screen.getByRole("link", { name: linkName });
}

// A download link is activated by pointer, or by Enter or Space while it holds the focus.
type LinkActivation = "click" | "Enter" | "Space";

const LINK_ACTIVATIONS: readonly LinkActivation[] = ["click", "Enter", "Space"];
const LINK_ACTIVATION_KEYS: Readonly<Record<Exclude<LinkActivation, "click">, string>> = {
    Enter: "{Enter}",
    Space: "[Space]",
};

const DOWNLOAD_ACTIVATION_CASES = DOWNLOAD_CASES.flatMap(([format, linkName]) =>
    LINK_ACTIVATIONS.map((activation) => ({ format, linkName, activation })),
);

async function ActivateLink(
    user: UserEvent,
    linkName: string,
    activation: LinkActivation,
): Promise<void> {
    if (activation === "click") {
        await user.click(DownloadLink(linkName));
        return;
    }
    await act(async () => {
        DownloadLink(linkName).focus();
    });
    await user.keyboard(LINK_ACTIVATION_KEYS[activation]);
}

// A download that never answers: like axios, it rejects with CanceledError once its signal is
// aborted.
function PendDownloadUntilAborted(
    _query: ReportQuery,
    _format: ExportFormat,
    options?: ReadOptions,
): Promise<ReportExportFile> {
    return new Promise((_resolve, reject) => {
        options?.signal?.addEventListener("abort", () => reject(new CanceledError()));
    });
}

// The signal the page handed to its first download.
function FirstDownloadSignal(): AbortSignal | undefined {
    return mockDownloadReportExport.mock.calls[0]?.[2]?.signal;
}

// Both links locked while a request runs, or both free to start a download.
function ExpectDownloadLinksLocked(isLocked: boolean): void {
    for (const [, linkName] of DOWNLOAD_CASES) {
        const link = DownloadLink(linkName);
        if (isLocked) {
            expect(link).toHaveAttribute("aria-disabled", "true");
        } else {
            expect(link).not.toHaveAttribute("aria-disabled");
        }
    }
}

// The report ShowMonthlyReport confirmed: its range, its table and both downloads, ready again.
async function ExpectMonthlyReportKept(): Promise<void> {
    expect(screen.getByRole("heading", { level: 2, name: DATE_RANGE_HEADING })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: REPORT_TABLE_LABEL })).toBeInTheDocument();
    ExpectCompleteDownloads(MONTHLY_CONFIRMED_QUERY);
    await waitFor(() => ExpectDownloadLinksLocked(false));
}

// A rejected download, the message line it leaves and the control that takes the cursor.
interface DownloadFailureCase {
    name: string;
    error: AxiosError;
    shown: string;
    FocusedControl: () => HTMLElement;
}

const DOWNLOAD_FAILURE_CASES: readonly DownloadFailureCase[] = [
    {
        name: "a 400 date edit literal",
        error: OutcomeError(EditFailure(START_MONTH_INVALID_MESSAGE, "startMm")),
        shown: START_MONTH_INVALID_MESSAGE,
        FocusedControl: () => DatePartInput(START_MONTH_PART),
    },
    {
        name: "the 503 file-error literal",
        error: BuildAxiosError(
            HTTP_SERVICE_UNAVAILABLE,
            BuildErrorBody({
                message: "Unable to Write TDQ (JOBS)...",
                cursor_field: "reportType",
            }),
        ),
        shown: "Unable to Write TDQ (JOBS)...",
        FocusedControl: MonthlyButton,
    },
    {
        name: "a 500",
        error: BuildAxiosError(FIRST_SERVER_ERROR_STATUS, BuildErrorBody({ message: "Boom" })),
        shown: ABEND_MESSAGE,
        FocusedControl: MonthlyButton,
    },
    {
        name: "a timeout with no answer",
        error: new AxiosError("timeout of 60000ms exceeded", AxiosError.ECONNABORTED),
        shown: ABEND_MESSAGE,
        FocusedControl: MonthlyButton,
    },
];

// The page's own transient link saved the file under the server's name; its blob URL is not yet
// released.
function ExpectSavedThroughLink(probe: FileSavingProbe, file: ReportExportFile): void {
    expect(probe.createObjectUrl.mock.calls).toEqual([[file.content]]);
    const savingLink = probe.linkClick.mock.contexts[0] as HTMLAnchorElement;
    expect(savingLink.getAttribute("href")).toBe(SAVED_FILE_URL);
    expect(savingLink.download).toBe(file.fileName);
    expect(probe.revokeObjectUrl).not.toHaveBeenCalled();
}

// The saving link is the page's own transient link; its blob URL outlives the click. Enter and
// Space on the focused link download as a click does, and the screen's ENTER never runs on the
// criteria the confirmed report cleared.
it.each(DOWNLOAD_ACTIVATION_CASES)(
    "CORPT00C $activation on the $linkName link fetches the confirmed report and saves its file",
    async ({ format, linkName, activation }) => {
        jest.useFakeTimers({ advanceTimers: true });
        const probe = ProbeFileSaving();
        const file = BuildExportFile(format);
        mockDownloadReportExport.mockResolvedValue(file);
        const user = await ShowMonthlyReport();
        await ActivateLink(user, linkName, activation);
        await waitFor(() => expect(probe.linkClick).toHaveBeenCalledTimes(1));
        expect(mockDownloadReportExport.mock.calls).toEqual([
            [MONTHLY_CONFIRMED_QUERY, format, READ_SIGNAL_OPTIONS],
        ]);
        ExpectSavedThroughLink(probe, file);
        act(() => jest.runOnlyPendingTimers());
        expect(probe.revokeObjectUrl.mock.calls).toEqual([[SAVED_FILE_URL]]);
        await ExpectAlert(MONTHLY_SUBMITTED_MESSAGE, "success");
        ExpectMonthlyReportShown();
        expect(mockFetchTransactionReport).toHaveBeenCalledTimes(1);
    },
);

// The link keeps the export address, yet its activation is never followed: the browser would
// replace the screen with any error body the export answers.
it("CORPT00C a download link activation never follows the export address", async () => {
    mockDownloadReportExport.mockImplementation(PendDownloadUntilAborted);
    await ShowMonthlyReport();
    const isFollowed = fireEvent.click(DownloadLink(CSV_LINK_NAME));
    expect(isFollowed).toBe(false);
    expect(mockDownloadReportExport).toHaveBeenCalledTimes(1);
    expect(DownloadLink(CSV_LINK_NAME)).toHaveAttribute("href", EXPORT_URL_PREFIX + "csv");
});

it.each(DOWNLOAD_FAILURE_CASES)(
    "CORPT00C a download failing with $name shows $shown in red and keeps the report",
    async ({ error, shown, FocusedControl }) => {
        mockDownloadReportExport.mockRejectedValue(error);
        const user = await ShowMonthlyReport();
        await user.click(DownloadLink(PDF_LINK_NAME));
        await screen.findByText(shown);
        await ExpectAlert(shown, "error");
        await ExpectMonthlyReportKept();
        await waitFor(() => expect(FocusedControl()).toHaveFocus());
        expect(mockDownloadReportExport).toHaveBeenCalledTimes(1);
        expect(mockRouterPush).not.toHaveBeenCalled();
    },
);

// The apiClient interceptor owns the 401 redirect to sign-on, which the mocked wrapper bypasses,
// so the page neither routes nor shows a message for the empty 401 body.
it("CORPT00C a download refused with 401 leaves the redirect to the interceptor", async () => {
    mockDownloadReportExport.mockRejectedValue(
        BuildAxiosError(HTTP_UNAUTHORIZED, BuildErrorBody()),
    );
    const user = await ShowMonthlyReport();
    await user.click(DownloadLink(CSV_LINK_NAME));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(mockDownloadReportExport).toHaveBeenCalledTimes(1);
    expect(mockRouterPush).not.toHaveBeenCalled();
    expect(mockRouterReplace).not.toHaveBeenCalled();
});

// A cancelled download is discarded before ToScreenFeedback, which would show the abend text.
it("CORPT00C discards a cancelled download and keeps the report and its message", async () => {
    const probe = ProbeFileSaving();
    mockDownloadReportExport.mockRejectedValue(new CanceledError());
    const user = await ShowMonthlyReport();
    await user.click(DownloadLink(CSV_LINK_NAME));
    await waitFor(() => expect(mockDownloadReportExport).toHaveBeenCalledTimes(1));
    await ExpectMonthlyReportKept();
    await ExpectAlert(MONTHLY_SUBMITTED_MESSAGE, "success");
    expect(probe.createObjectUrl).not.toHaveBeenCalled();
});

// One activation is one request: while it runs, both links, ENTER and the criteria are locked and
// the busy bar shows, and a click, Enter or Space on the locked links starts nothing.
it("CORPT00C locks both downloads and ENTER while a download runs", async () => {
    mockDownloadReportExport.mockImplementation(PendDownloadUntilAborted);
    const user = await ShowMonthlyReport();
    await user.click(DownloadLink(CSV_LINK_NAME));
    await screen.findByRole("progressbar");
    ExpectDownloadLinksLocked(true);
    ExpectKeyboardLocked();
    const lockedUser = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    for (const { linkName, activation } of DOWNLOAD_ACTIVATION_CASES) {
        await ActivateLink(lockedUser, linkName, activation);
    }
    expect(mockFetchTransactionReport).toHaveBeenCalledTimes(1);
    expect(mockDownloadReportExport).toHaveBeenCalledTimes(1);
    expect(mockDownloadReportExport).toHaveBeenCalledWith(
        MONTHLY_CONFIRMED_QUERY,
        ExportFormat.CSV,
        READ_SIGNAL_OPTIONS,
    );
    expect(FirstDownloadSignal()?.aborted).toBe(false);
});

// One ENTER activation dispatched straight to the page, as a script or a fast double press
// delivers it: a click on the Continue button, or the physical key in the CONFIRM field.
function ActivateEnter(entry: KeyEntry): void {
    if (entry === "click") {
        fireEvent.click(screen.getByRole("button", { name: ENTER_KEY.buttonName }));
        return;
    }
    fireEvent.keyDown(ConfirmInput(), { key: ENTER_KEY.keyName });
}

function ActivateCsvDownload(): void {
    fireEvent.click(DownloadLink(CSV_LINK_NAME));
}

// Both activations run in one act scope, so React renders neither before the second arrives: the
// ENTER key and links the first one's render disables cannot refuse it, only the screen lock can.
async function ActivateBeforeRender(first: () => void, second: () => void): Promise<void> {
    await act(async () => {
        first();
        second();
    });
}

it.each(KEY_ENTRIES)(
    "CORPT00C two ENTER activations by %s before a render send one request",
    async (entry) => {
        mockFetchTransactionReport.mockResolvedValue(BuildReportResponse());
        const user = await RenderScreen();
        await user.click(MonthlyButton());
        await user.type(ConfirmInput(), "Y");
        await ActivateBeforeRender(
            () => ActivateEnter(entry),
            () => ActivateEnter(entry),
        );
        await ExpectAlert(MONTHLY_SUBMITTED_MESSAGE, "success");
        expect(mockFetchTransactionReport.mock.calls).toEqual([
            [MONTHLY_CONFIRMED_QUERY, READ_SIGNAL_OPTIONS],
        ]);
        expect(FirstReadSignal()?.aborted).toBe(false);
        ExpectMonthlyReportShown();
    },
);

// ENTER's read and a download, started one right after the other before a render: the request
// started first holds the screen lock, so the other sends nothing.
interface LockedPairCase {
    name: string;
    ActivateFirst: () => void;
    ActivateSecond: () => void;
    reads: number;
    downloads: number;
}

// The reads count ShowMonthlyReport's own read.
const LOCKED_PAIR_CASES: readonly LockedPairCase[] = [
    {
        name: "ENTER right after a download link",
        ActivateFirst: ActivateCsvDownload,
        ActivateSecond: () => ActivateEnter("click"),
        reads: 1,
        downloads: 1,
    },
    {
        name: "a download link right after ENTER",
        ActivateFirst: () => ActivateEnter("click"),
        ActivateSecond: ActivateCsvDownload,
        reads: 2,
        downloads: 0,
    },
];

it.each(LOCKED_PAIR_CASES)(
    "CORPT00C $name sends nothing while the first request runs",
    async ({ ActivateFirst, ActivateSecond, reads, downloads }) => {
        await ShowMonthlyReport();
        mockFetchTransactionReport.mockImplementation(PendUntilAborted);
        mockDownloadReportExport.mockImplementation(PendDownloadUntilAborted);
        await ActivateBeforeRender(ActivateFirst, ActivateSecond);
        expect(mockFetchTransactionReport).toHaveBeenCalledTimes(reads);
        expect(mockDownloadReportExport).toHaveBeenCalledTimes(downloads);
        await screen.findByRole("progressbar");
        ExpectKeyboardLocked();
    },
);

// Leaving the screen while a download runs aborts it, so no file arrives for a screen nobody sees.
it.each(LEAVE_CASES)(
    "CORPT00C $leaving during a download abandons it without a message or a file",
    async ({ LeaveScreen, routes }) => {
        const probe = ProbeFileSaving();
        mockDownloadReportExport.mockImplementation(PendDownloadUntilAborted);
        const user = await ShowMonthlyReport();
        await user.click(DownloadLink(PDF_LINK_NAME));
        await screen.findByRole("progressbar");
        expect(FirstDownloadSignal()?.aborted).toBe(false);
        await LeaveScreen(user);
        expect(FirstDownloadSignal()?.aborted).toBe(true);
        expect(mockRouterPush.mock.calls).toEqual(routes.map((route) => [route]));
        expect(screen.queryByText(ABEND_MESSAGE)).not.toBeInTheDocument();
        expect(probe.createObjectUrl).not.toHaveBeenCalled();
    },
);

// The page tells a confirmed report from the prompt and the cleared screen by the dates and the
// cursor corpt00c sets, never by the message, which it only shows: a confirmed range with no
// transaction keeps its range and downloads whatever words its message uses.
const UNSUFFIXED_SUBMITTED_MESSAGE = "Custom report ready ...";
const UNSUFFIXED_EMPTY_RANGE_RESPONSE: TransactionReportResponse = BuildReportResponse({
    start_date: "2021-01-01",
    end_date: "2021-01-31",
    message: UNSUFFIXED_SUBMITTED_MESSAGE,
    cursor_field: "reportType",
    lines: [],
});

it("CORPT00C tells a confirmed empty range by its dates and cursor, not its message", async () => {
    mockFetchTransactionReport.mockResolvedValue(UNSUFFIXED_EMPTY_RANGE_RESPONSE);
    const user = await RenderScreen();
    await SubmitMonthlyReport(user);
    await screen.findByText(UNSUFFIXED_SUBMITTED_MESSAGE);
    await ExpectAlert(UNSUFFIXED_SUBMITTED_MESSAGE, "success");
    expect(
        screen.getByRole("heading", { level: 2, name: EMPTY_RANGE_HEADING }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    ExpectCompleteDownloads(MONTHLY_CONFIRMED_QUERY);
});
