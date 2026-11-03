// Screen tests: BMS COTRN00, program COTRN00C (CT00), page /transactions.
import { ThemeProvider } from "@mui/material/styles";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { AxiosError, AxiosHeaders } from "axios";
import type { ReactNode } from "react";

import TransactionListPage from "@/app/transactions/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import {
    ListTransactions,
    SelectTransactions,
    type TransactionListQuery,
    type TransactionListResponse,
    type TransactionListRow,
    type TransactionSelectionRequest,
    type TransactionSelectionResponse,
} from "@/lib/api/transactions";
import {
    Direction,
    SIGN_ON_ROUTE,
    type LegacyErrorResponse,
    type MessageSeverity,
} from "@/lib/apiClient";

// Read by the mocked next/navigation hooks at render time; the "mock" prefix lets the hoisted
// factory use them.
const mockRouterPush = jest.fn();
const mockRouterReplace = jest.fn();
const mockRouter = {
    push: mockRouterPush,
    replace: mockRouterReplace,
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};
const mockSearchParams = new URLSearchParams();

jest.mock("next/navigation", () => ({
    useRouter: () => mockRouter,
    usePathname: () => "/transactions",
    useSearchParams: () => mockSearchParams,
}));

// The mocked keys mirror the exports of @/lib/session and its SessionContextValue; the user is
// a regular (type U) user, the role that owns CT00.
jest.mock("@/lib/session", () => ({
    SessionProvider: ({ children }: { children: ReactNode }) => children,
    useSession: () => ({
        currentUser: {
            user_id: "TESTUSR1",
            user_type: "U",
            first_name: "Test",
            last_name: "User",
            next_route: "/menu",
        },
        isLoading: false,
        RefreshSession: jest.fn(),
        ClearSession: jest.fn(),
    }),
}));

// Only the network calls are replaced; the module's types and enums stay real.
jest.mock("@/lib/api/transactions", () => ({
    ...jest.requireActual<object>("@/lib/api/transactions"),
    ListTransactions: jest.fn(),
    SelectTransactions: jest.fn(),
    ViewTransaction: jest.fn(),
    AddTransaction: jest.fn(),
}));

const mockListTransactions = jest.mocked(ListTransactions);
const mockSelectTransactions = jest.mocked(SelectTransactions);

// The PF keys of the COTRN00 legend, and whether a press uses the button or the physical key.
type ListKey = "ENTER" | "F3" | "F7" | "F8";
type PressMode = "button" | "key";

interface PagingCase {
    key: ListKey;
    mode: PressMode;
    query: TransactionListQuery;
}

// A literal, the key whose answer carries it, and the HTTP status of that answer.
interface LiteralCase {
    literal: string;
    key: ListKey;
    status: number;
}

// The row-4 title and TRNIDIN of bms/COTRN00.bms (LENGTH=16).
const SCREEN_TITLE = "List Transactions";
const SEARCH_LABEL = "Search Tran ID:";
const SEARCH_MAX_LENGTH = "16";
// The cursor_field and field_errors name of TRNIDIN (MOVE -1 TO TRNIDINL).
const SEARCH_FIELD = "search_tran_id";
// The Sel column, then the TRNIDnn, TDATEnn, TDESCnn and TAMTnnn headings.
const COLUMN_HEADERS = ["Sel", "Transaction ID", "Date", "Description", "Amount"];
// SEL0001-SEL0010 accept only S; the blank option comes first.
const OFFERED_SELECTION_CODES = ["", "S"];
const PAGE_SIZE = 10;
// The paging keys are opaque to the page: it echoes them back unchanged.
const FIRST_KEY = "first-key-token";
const LAST_KEY = "last-key-token";
const MENU_ROUTE = "/menu";
const SEARCH_KEY = "0000000000000005";
const SELECTED_ROW_NUMBER = 3;
const SELECTED_TRAN_ID = "0000000000000003";
const VIEW_ROUTE = `/transactions/view?tranId=${SELECTED_TRAN_ID}`;
const HTTP_OK = 200;

// The legend ENTER=Continue  F3=Back  F7=Backward  F8=Forward, as FunctionKeyBar labels it.
const KEY_BUTTON_NAMES: Readonly<Record<ListKey, string>> = {
    ENTER: "Continue (Enter)",
    F3: "Back (F3)",
    F7: "Backward (F7)",
    F8: "Forward (F8)",
};
const KEYBOARD_KEYS: Readonly<Record<ListKey, string>> = {
    ENTER: "{Enter}",
    F3: "{F3}",
    F7: "{F7}",
    F8: "{F8}",
};

// The shared paging literals of backend/app/core/messages.py that COTRN00C assigns.
const TOP_ALREADY = "You are already at the top of the page...";
const BOTTOM_ALREADY = "You are already at the bottom of the page...";
const AT_TOP = "You are at the top of the page...";
const REACHED_BOTTOM = "You have reached the bottom of the page...";
const REACHED_TOP = "You have reached the top of the page...";
// COTRN00C PROCESS-ENTER-KEY lines 199 and 214 (cotrn00c.py constants).
const INVALID_SELECTION = "Invalid selection. Valid value is S";
const NUMERIC_SEARCH = "Tran ID must be Numeric ...";
// The 503 WHEN OTHER literal of the TRANSACT browse, with the lower-case "t" of the source.
const LOOKUP_FAILED = "Unable to lookup transaction...";
// A 500 shows the CSMSG02Y abend text.
const ABEND = "UNEXPECTED ABEND OCCURRED.";

const ALERT_COLOR_CLASSES: Readonly<Record<MessageSeverity, string>> = {
    error: "MuiAlert-colorError",
    success: "MuiAlert-colorSuccess",
    info: "MuiAlert-colorInfo",
};

// A frozen clock keeps the ScreenShell CURDATE/CURTIME store unchanged, so its one-second tick
// never re-renders the header outside act().
const FIXED_NOW_MILLISECONDS = new Date(2022, 6, 18, 9, 5, 7).getTime();

const PRESS_MODES: readonly PressMode[] = ["button", "key"];

const FORWARD_QUERY: TransactionListQuery = {
    startKey: LAST_KEY,
    firstKey: FIRST_KEY,
    direction: Direction.FORWARD,
    page: 1,
};
const BACKWARD_QUERY: TransactionListQuery = {
    startKey: FIRST_KEY,
    direction: Direction.BACKWARD,
    page: 1,
};
const PAGING_CASES: readonly PagingCase[] = [
    { key: "F8", mode: "button", query: FORWARD_QUERY },
    { key: "F8", mode: "key", query: FORWARD_QUERY },
    { key: "F7", mode: "button", query: BACKWARD_QUERY },
    { key: "F7", mode: "key", query: BACKWARD_QUERY },
];

// Every CT00 literal arrives in a 200 list page; only a TRANSACT file condition is a 503. The
// COTRN00 ERRMSG field is COLOR=RED and COTRN00C never sets its colour, so each shows as an error.
const LITERAL_CASES: readonly LiteralCase[] = [
    { literal: TOP_ALREADY, key: "F7", status: HTTP_OK },
    { literal: BOTTOM_ALREADY, key: "F8", status: HTTP_OK },
    { literal: AT_TOP, key: "ENTER", status: HTTP_OK },
    { literal: REACHED_BOTTOM, key: "F8", status: HTTP_OK },
    { literal: REACHED_TOP, key: "F7", status: HTTP_OK },
    { literal: INVALID_SELECTION, key: "ENTER", status: HTTP_OK },
    { literal: NUMERIC_SEARCH, key: "ENTER", status: HTTP_OK },
    { literal: LOOKUP_FAILED, key: "F8", status: 503 },
    { literal: ABEND, key: "F7", status: 500 },
];

// An exact TRAN-ID and a key between two TRAN-IDs both browse from the first key at or after it.
const SEARCH_CASES: readonly [string, string][] = [
    ["an exact match", "0000000000000013"],
    ["a key between two TRAN-IDs", "0000000000000012"],
];

// Row numbers count from firstRowNumber, one per SEL0001-SEL0010 row.
function RowNumbersFrom(firstRowNumber: number): number[] {
    return Array.from(Array(PAGE_SIZE).keys(), (index) => firstRowNumber + index);
}

function TranIdFor(rowNumber: number): string {
    return String(rowNumber).padStart(16, "0");
}

function TranIdsFrom(firstRowNumber: number): string[] {
    return RowNumbersFrom(firstRowNumber).map(TranIdFor);
}

// One COTRN00 row as the server sends it: TRNIDnn, TDATEnn (MM/DD/YY), TDESCnn and TAMTnnn.
function BuildRow(rowNumber: number): TransactionListRow {
    return {
        tran_id: TranIdFor(rowNumber),
        tran_date: "06/10/22",
        tran_desc: `Purchase at merchant ${rowNumber}`,
        tran_amt: `${rowNumber}.50`,
    };
}

function BuildRows(firstRowNumber = 1): TransactionListRow[] {
    return RowNumbersFrom(firstRowNumber).map(BuildRow);
}

// The first full page: rows 1-10, page 1, its opaque keys, no literal and the cursor on TRNIDIN.
function BuildListResponse(
    overrides: Partial<TransactionListResponse> = {},
): TransactionListResponse {
    return {
        rows: BuildRows(),
        page: 1,
        first_key: FIRST_KEY,
        last_key: LAST_KEY,
        message: "",
        cursor_field: SEARCH_FIELD,
        ...overrides,
    };
}

function BuildErrorBody(overrides: Partial<LegacyErrorResponse> = {}): LegacyErrorResponse {
    return { message: "", info_message: "", field_errors: [], cursor_field: "", ...overrides };
}

// The rejection axios raises for a non-2xx answer carrying the given body.
function BuildAxiosError(status: number, body: LegacyErrorResponse): AxiosError {
    const config = { headers: new AxiosHeaders() };
    const response = { status, statusText: "", headers: {}, config, data: body };
    const code = status >= 500 ? AxiosError.ERR_BAD_RESPONSE : AxiosError.ERR_BAD_REQUEST;
    return new AxiosError(
        `Request failed with status code ${status}`,
        code,
        config,
        null,
        response,
    );
}

// The body of LegacyMessageError(field=search_tran_id), which the TRANSACT lookup failure raises.
function BuildSearchErrorBody(message: string): LegacyErrorResponse {
    return BuildErrorBody({ message, field_errors: [SEARCH_FIELD], cursor_field: SEARCH_FIELD });
}

function RenderScreen(): UserEvent {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <TransactionListPage />
        </ThemeProvider>,
    );
    return user;
}

// Mounts the page over the first page of rows and waits until its last row is shown.
async function RenderFirstPage(): Promise<UserEvent> {
    mockListTransactions.mockResolvedValueOnce(BuildListResponse());
    const user = RenderScreen();
    await screen.findByText(TranIdFor(PAGE_SIZE));
    return user;
}

function SearchField(): HTMLElement {
    return screen.getByRole("textbox", { name: SEARCH_LABEL });
}

function SelectionField(rowNumber: number): HTMLElement {
    return screen.getByRole("combobox", { name: `Selection for row ${rowNumber}` });
}

// The Transaction ID cell of every body row, in screen order.
function ShownTranIds(): string[] {
    const bodyRows = screen.getAllByRole("row").slice(1);
    return bodyRows.map((row) => within(row).getAllByRole("cell")[1]?.textContent ?? "");
}

// A legend key pressed through its FunctionKeyBar button or as the physical key.
async function PressKey(user: UserEvent, key: ListKey, mode: PressMode): Promise<void> {
    if (mode === "key") {
        await user.keyboard(KEYBOARD_KEYS[key]);
        return;
    }
    await user.click(screen.getByRole("button", { name: KEY_BUTTON_NAMES[key] }));
}

// The open menu hides the rest of the screen from the accessibility tree, so the helper waits
// for it to close.
async function ChooseSelection(user: UserEvent, rowNumber: number, code: string): Promise<void> {
    await user.click(SelectionField(rowNumber));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: code }));
    await waitFor(() => expect(screen.queryByRole("listbox")).toBeNull());
}

// The message line shows the server's literal verbatim, in the colour of its severity.
async function ExpectAlert(text: string, severity: MessageSeverity): Promise<void> {
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(text);
    expect(alert).toHaveClass(ALERT_COLOR_CLASSES[severity]);
}

// A 200 answer carries the literal over the shown page; any other status rejects the call.
function OutcomeFor({ literal, status }: LiteralCase): Promise<TransactionSelectionResponse> {
    if (status === HTTP_OK) {
        return Promise.resolve(BuildListResponse({ message: literal }));
    }
    return Promise.reject(BuildAxiosError(status, BuildSearchErrorBody(literal)));
}

// ENTER with no Sel code: no selection, the search key as typed, and the shown page's keys.
function ExpectSearchOnlyRequest(searchKey: string): void {
    const request: TransactionSelectionRequest = {
        selections: [],
        search_tran_id: searchKey,
        first_key: FIRST_KEY,
        page: 1,
    };
    expect(mockSelectTransactions).toHaveBeenCalledWith(request);
}

// ENTER answers through SelectTransactions, F7 and F8 through ListTransactions.
function MockKeyOutcome(literalCase: LiteralCase): void {
    const answer = () => OutcomeFor(literalCase);
    if (literalCase.key === "ENTER") {
        mockSelectTransactions.mockImplementationOnce(answer);
        return;
    }
    mockListTransactions.mockImplementationOnce(answer);
}

// The page the server browses for both SEARCH_CASES: rows 13-22, the first at or after the key.
const FOUND_FIRST_ROW_NUMBER = 13;
const FOUND_PAGE: Partial<TransactionListResponse> = {
    rows: BuildRows(FOUND_FIRST_ROW_NUMBER),
    first_key: TranIdFor(FOUND_FIRST_ROW_NUMBER),
    last_key: TranIdFor(FOUND_FIRST_ROW_NUMBER + PAGE_SIZE - 1),
};

beforeEach(() => {
    mockListTransactions.mockReset();
    mockSelectTransactions.mockReset();
    jest.spyOn(Date, "now").mockReturnValue(FIXED_NOW_MILLISECONDS);
    window.sessionStorage.clear();
});

afterEach(() => {
    jest.restoreAllMocks();
});

describe("COTRN00C first entry on /transactions", () => {
    it("COTRN00C first entry browses from the lowest TRAN-ID and shows ten rows", async () => {
        await RenderFirstPage();
        expect(mockListTransactions).toHaveBeenCalledTimes(1);
        expect(mockListTransactions).toHaveBeenCalledWith({});
        expect(screen.getByRole("heading", { level: 1, name: SCREEN_TITLE })).toBeVisible();
        expect(screen.getByText("Tran: CT00")).toBeVisible();
        expect(screen.getByText("Prog: COTRN00C")).toBeVisible();
        expect(ShownTranIds()).toEqual(TranIdsFrom(1));
        expect(screen.getByText("Page:")).toBeVisible();
        expect(screen.getByText("1")).toBeVisible();
        expect(screen.queryByRole("alert")).toBeNull();
    });
});

describe("COTRN00C BMS COTRN00 fields", () => {
    it("COTRN00C TRNIDIN, the column headings and each Sel code follow COTRN00", async () => {
        const user = await RenderFirstPage();
        expect(SearchField()).toHaveAttribute("maxlength", SEARCH_MAX_LENGTH);
        const headers = screen.getAllByRole("columnheader").map((header) => header.textContent);
        expect(headers).toEqual(COLUMN_HEADERS);
        await user.click(SelectionField(1));
        const offeredCodes = within(screen.getByRole("listbox"))
            .getAllByRole("option")
            .map((option) => option.getAttribute("data-value"));
        expect(offeredCodes).toEqual(OFFERED_SELECTION_CODES);
    });
});

const SELECTION_REQUEST: TransactionSelectionRequest = {
    selections: [{ tran_id: SELECTED_TRAN_ID, selection: "S" }],
    search_tran_id: SEARCH_KEY,
    first_key: FIRST_KEY,
    page: 1,
};

describe("COTRN00C ENTER with an S selection and a search key", () => {
    it.each(PRESS_MODES)(
        "COTRN00C ENTER (%s) sends the search key with the S selection",
        async (mode) => {
            const user = await RenderFirstPage();
            mockSelectTransactions.mockResolvedValueOnce({ route: VIEW_ROUTE });
            await ChooseSelection(user, SELECTED_ROW_NUMBER, "S");
            await user.type(SearchField(), SEARCH_KEY);
            await PressKey(user, "ENTER", mode);
            await waitFor(() => expect(mockRouterPush).toHaveBeenCalledWith(VIEW_ROUTE));
            expect(mockSelectTransactions).toHaveBeenCalledTimes(1);
            expect(mockSelectTransactions).toHaveBeenCalledWith(SELECTION_REQUEST);
            expect(mockRouterPush).toHaveBeenCalledTimes(1);
        },
    );
});

describe("COTRN00C ENTER with a non-numeric search key", () => {
    it("COTRN00C ENTER sends the search key raw and keeps the rows shown", async () => {
        const user = await RenderFirstPage();
        const answer = BuildListResponse({ message: NUMERIC_SEARCH, page: 0 });
        mockSelectTransactions.mockResolvedValueOnce(answer);
        await user.type(SearchField(), "abc");
        await PressKey(user, "ENTER", "key");
        await ExpectAlert(NUMERIC_SEARCH, "error");
        ExpectSearchOnlyRequest("abc");
        expect(ShownTranIds()).toEqual(TranIdsFrom(1));
        expect(SearchField()).toHaveValue("abc");
        expect(mockRouterPush).not.toHaveBeenCalled();
    });
});

describe("COTRN00C ENTER browses from the search key", () => {
    it.each(SEARCH_CASES)(
        "COTRN00C ENTER with %s shows the page the server returns",
        async (_caseName, searchKey) => {
            const user = await RenderFirstPage();
            mockSelectTransactions.mockResolvedValueOnce(BuildListResponse(FOUND_PAGE));
            await user.type(SearchField(), searchKey);
            await PressKey(user, "ENTER", "button");
            await screen.findByText(TranIdFor(FOUND_FIRST_ROW_NUMBER + PAGE_SIZE - 1));
            ExpectSearchOnlyRequest(searchKey);
            expect(ShownTranIds()).toEqual(TranIdsFrom(FOUND_FIRST_ROW_NUMBER));
            expect(mockRouterPush).not.toHaveBeenCalled();
        },
    );
});

describe("COTRN00C F7 and F8 paging", () => {
    it.each(PAGING_CASES)(
        "COTRN00C $key ($mode) pages from the keys of the page shown",
        async ({ key, mode, query }) => {
            const user = await RenderFirstPage();
            mockListTransactions.mockResolvedValueOnce(
                BuildListResponse({ rows: BuildRows(11), page: 2 }),
            );
            await PressKey(user, key, mode);
            await screen.findByText(TranIdFor(20));
            expect(mockListTransactions).toHaveBeenCalledTimes(2);
            expect(mockListTransactions).toHaveBeenLastCalledWith(query);
            expect(ShownTranIds()).toEqual(TranIdsFrom(11));
            expect(screen.getByText("2")).toBeVisible();
        },
    );
});

describe("COTRN00C ERRMSG literals", () => {
    it.each(LITERAL_CASES)(
        "COTRN00C $key answered $status shows $literal in red",
        async (literalCase) => {
            const user = await RenderFirstPage();
            MockKeyOutcome(literalCase);
            await PressKey(user, literalCase.key, "button");
            await ExpectAlert(literalCase.literal, "error");
            expect(ShownTranIds()).toEqual(TranIdsFrom(1));
            expect(mockRouterPush).not.toHaveBeenCalled();
        },
    );

    it("COTRN00C first entry shows the literal the first page carries", async () => {
        mockListTransactions.mockResolvedValueOnce(BuildListResponse({ message: REACHED_BOTTOM }));
        RenderScreen();
        await ExpectAlert(REACHED_BOTTOM, "error");
        expect(ShownTranIds()).toEqual(TranIdsFrom(1));
    });
});

describe("COTRN00C F3", () => {
    it.each(PRESS_MODES)("COTRN00C F3 (%s) returns to the main menu", async (mode) => {
        const user = await RenderFirstPage();
        await PressKey(user, "F3", mode);
        expect(mockRouterPush).toHaveBeenCalledTimes(1);
        expect(mockRouterPush).toHaveBeenCalledWith(MENU_ROUTE);
        expect(mockListTransactions).toHaveBeenCalledTimes(1);
        expect(mockSelectTransactions).not.toHaveBeenCalled();
    });
});

describe("COTRN00C session refusal", () => {
    it("COTRN00C first entry refused 401 leaves sign-on to the apiClient interceptor", async () => {
        mockListTransactions.mockRejectedValueOnce(BuildAxiosError(401, BuildErrorBody()));
        RenderScreen();
        await waitFor(() => expect(screen.queryByRole("progressbar")).toBeNull());
        expect(screen.queryByRole("alert")).toBeNull();
        expect(mockRouterPush).not.toHaveBeenCalled();
        expect(mockRouterReplace).not.toHaveBeenCalled();
        expect(SIGN_ON_ROUTE).toBe("/signon");
    });
});

describe("COTRN00C TRNIDIN error mark and cursor", () => {
    it("COTRN00C TRANSACT lookup failure marks TRNIDIN and puts the cursor there", async () => {
        const user = await RenderFirstPage();
        expect(SearchField()).toHaveAttribute("aria-invalid", "false");
        const failure = BuildAxiosError(503, BuildSearchErrorBody(LOOKUP_FAILED));
        mockListTransactions.mockRejectedValueOnce(failure);
        await PressKey(user, "F8", "button");
        await ExpectAlert(LOOKUP_FAILED, "error");
        expect(SearchField()).toHaveAttribute("aria-invalid", "true");
        expect(SearchField()).toHaveFocus();
        expect(ShownTranIds()).toEqual(TranIdsFrom(1));
    });
});

describe("COTRN00C cursor without an error mark", () => {
    it("COTRN00C paging literal puts the cursor on TRNIDIN without an error mark", async () => {
        const user = await RenderFirstPage();
        mockListTransactions.mockResolvedValueOnce(BuildListResponse({ message: BOTTOM_ALREADY }));
        await PressKey(user, "F8", "button");
        await ExpectAlert(BOTTOM_ALREADY, "error");
        expect(SearchField()).toHaveFocus();
        expect(SearchField()).toHaveAttribute("aria-invalid", "false");
    });
});
