// Screen tests: BMS CORPT00, program CORPT00C with CBTRN03C lines (CR00),
// page /reports/transactions.
import { alertClasses, tableCellClasses } from "@mui/material";
import { ThemeProvider } from "@mui/material/styles";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import {
    AxiosError,
    AxiosHeaders,
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
import { SIGN_ON_ROUTE, type LegacyErrorResponse, type MessageSeverity } from "@/lib/apiClient";
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

// The corpt00c.py literals (CORPT00C lines 213-531).
const SELECT_REPORT_TYPE_MESSAGE = "Select a report type to print report...";
const START_MONTH_INVALID_MESSAGE = "Start Date - Not a valid Month...";
const MONTHLY_SUBMITTED_MESSAGE = "Monthly report submitted for printing ...";

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

const SEVERITY_CLASSES: Readonly<Record<MessageSeverity, string>> = {
    error: alertClasses.colorError,
    success: alertClasses.colorSuccess,
    info: alertClasses.colorInfo,
};

const DOWNLOAD_CASES: readonly (readonly [ExportFormat, string])[] = [
    [ExportFormat.CSV, "Download CSV"],
    [ExportFormat.PDF, "Download PDF"],
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

function MockReportOutcome({ literal, status, cursorField, lines }: LiteralCase): void {
    if (status === HTTP_OK) {
        mockFetchTransactionReport.mockResolvedValue(
            BuildReportResponse({ message: literal, cursor_field: cursorField, lines: [...lines] }),
        );
        return;
    }
    const errorBody = BuildErrorBody({
        message: literal,
        field_errors: [cursorField],
        cursor_field: cursorField,
    });
    mockFetchTransactionReport.mockRejectedValue(BuildAxiosError(status, errorBody));
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

// MONTHLY chosen, Y confirmed and ENTER pressed, answered with the five report lines.
async function ShowMonthlyReport(): Promise<void> {
    mockFetchTransactionReport.mockResolvedValue(BuildReportResponse());
    const user = await RenderScreen();
    await user.click(ReportTypeButton(MONTHLY_LABEL));
    await user.type(ConfirmInput(), "Y");
    await PressScreenKey(user, ENTER_KEY, "click");
    await screen.findByRole("table", { name: REPORT_TABLE_LABEL });
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
        );
        expect(mockFetchTransactionReport).toHaveBeenCalledWith(BLANK_TEXT_QUERY);
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

it("CORPT00C confirmed range with no transactions shows its range and no table", async () => {
    MockReportOutcome(SubmittedOutcome("Custom report submitted for printing ...", []));
    const user = await RenderScreen();
    await PressScreenKey(user, ENTER_KEY, "click");
    await ExpectAlert("Custom report submitted for printing ...", "success");
    expect(screen.getByRole("heading", { level: 2, name: DATE_RANGE_HEADING })).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(mockBuildReportExportUrl).not.toHaveBeenCalled();
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
