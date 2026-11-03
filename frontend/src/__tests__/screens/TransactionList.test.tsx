// Screen tests: BMS COTRN00, program COTRN00C (CT00), page /transactions.
import { ThemeProvider } from "@mui/material/styles";
import {
    act,
    cleanup,
    fireEvent,
    render,
    screen,
    waitFor,
    within,
    type RenderOptions,
} from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { AxiosError, AxiosHeaders, CanceledError } from "axios";
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
    StorePendingMessage,
    type LegacyErrorResponse,
    type MessageSeverity,
    type ReadOptions,
} from "@/lib/apiClient";
import { PF_KEY_ACTIONS } from "@/lib/useFunctionKeys";

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

// The list page fields of a 200 answer, which an invalid-selection 400 adds to the legacy keys.
type ListPageBody = Pick<
    TransactionListResponse,
    "rows" | "page" | "first_key" | "last_key" | "search_cleared"
>;

// A literal, the key whose answer carries it, the HTTP status of that answer, and the list page
// a rejection's body adds to the four legacy keys.
interface LiteralCase {
    literal: string;
    key: ListKey;
    status: number;
    refusedPage?: ListPageBody;
}

// An edit failure, the list page its 400 body carries, if any, and the F8 query sent after it.
interface RefusalPagingCase {
    literal: string;
    refusedPage?: ListPageBody;
    query: TransactionListQuery;
}

// A key pressed over typed search text, the search_cleared and literal of its 200 answer, and
// the text TRNIDIN shows afterwards.
interface SearchLifecycleCase {
    key: ListKey;
    searchCleared: boolean;
    literal: string;
    shownSearch: string;
}

// A read left unanswered, how it is started and its index among the ListTransactions calls.
interface PendingReadCase {
    read: string;
    callIndex: number;
    StartRead: () => Promise<UserEvent>;
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
// A second search key, an exact TRAN-ID of the found page.
const NEXT_SEARCH_KEY = "0000000000000013";
const SELECTED_ROW_NUMBER = 3;
const SELECTED_TRAN_ID = "0000000000000003";
const VIEW_ROUTE = `/transactions/view?tranId=${SELECTED_TRAN_ID}`;
const HTTP_OK = 200;
const HTTP_EDIT_FAILURE = 400;

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
// A message StorePendingMessage carried across a navigation: the apiClient 403 refusal literal,
// whose trailing space is part of it (cbl/COMEN01C.cbl).
const CARRIED_MESSAGE = "No access - Admin Only option... ";

// Every ListTransactions call is a read and carries the AbortSignal of the runner's controller.
const READ_OPTIONS = { signal: expect.any(AbortSignal) };

const ALERT_COLOR_CLASSES: Readonly<Record<MessageSeverity, string>> = {
    error: "MuiAlert-colorError",
    success: "MuiAlert-colorSuccess",
    info: "MuiAlert-colorInfo",
};

// A frozen clock keeps the ScreenShell CURDATE/CURTIME store unchanged, so its one-second tick
// never re-renders the header outside act().
const FIXED_NOW_MILLISECONDS = new Date(2022, 6, 18, 9, 5, 7).getTime();

const PRESS_MODES: readonly PressMode[] = ["button", "key"];
// The paging keys, whose answers come from ListTransactions reads.
const PAGE_KEYS: readonly ListKey[] = ["F7", "F8"];
// The legend keys that send a request; F3 only leaves the screen.
const REQUEST_KEYS: readonly ListKey[] = ["ENTER", "F7", "F8"];
// The keys whose answer can blank TRNIDIN through search_cleared.
const SEARCH_CLEARING_KEYS: readonly ListKey[] = ["ENTER", "F8"];

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

// The page a browse from a blank search key builds: rows 1-10 on page 1, as first entry shows,
// with TRNIDIN blanked as every browse that pages on blanks it.
const FIRST_LIST_PAGE: ListPageBody = {
    rows: BuildRows(),
    page: 1,
    first_key: FIRST_KEY,
    last_key: LAST_KEY,
    search_cleared: true,
};

// The paging literals arrive in a 200 list page. The two edit failures are 400s: the invalid
// selection carries the re-browsed page, the non-numeric search key the four legacy keys only.
// A TRANSACT file condition is a 503. The COTRN00 ERRMSG field is COLOR=RED and COTRN00C never
// sets its colour, so each shows as an error.
const LITERAL_CASES: readonly LiteralCase[] = [
    { literal: TOP_ALREADY, key: "F7", status: HTTP_OK },
    { literal: BOTTOM_ALREADY, key: "F8", status: HTTP_OK },
    { literal: AT_TOP, key: "ENTER", status: HTTP_OK },
    { literal: REACHED_BOTTOM, key: "F8", status: HTTP_OK },
    { literal: REACHED_TOP, key: "F7", status: HTTP_OK },
    {
        literal: INVALID_SELECTION,
        key: "ENTER",
        status: HTTP_EDIT_FAILURE,
        refusedPage: FIRST_LIST_PAGE,
    },
    { literal: NUMERIC_SEARCH, key: "ENTER", status: HTTP_EDIT_FAILURE },
    { literal: LOOKUP_FAILED, key: "F8", status: 503 },
    { literal: ABEND, key: "F7", status: 500 },
];

// The id of the MessageLine ERRMSG Alert, which a field named in field_errors is described by.
const ERRMSG_ID = "message-line-errmsg";

// A literal case and the aria-describedby TRNIDIN carries after its answer, null for none.
interface SearchDescriptionCase extends LiteralCase {
    describedBy: string | null;
}

// Both rejections name TRNIDIN in field_errors; the 200 paging literal names it only as the
// cursor, so TRNIDIN, which has no helper text, is described by nothing.
const SEARCH_DESCRIPTION_CASES: readonly SearchDescriptionCase[] = [
    { literal: NUMERIC_SEARCH, key: "ENTER", status: HTTP_EDIT_FAILURE, describedBy: ERRMSG_ID },
    { literal: LOOKUP_FAILED, key: "F8", status: 503, describedBy: ERRMSG_ID },
    { literal: BOTTOM_ALREADY, key: "F8", status: HTTP_OK, describedBy: null },
];

// Non-numeric TRNIDIN text travels as typed, neither case-folded nor trimmed.
const NON_NUMERIC_SEARCH_CASES: readonly [string, string][] = [
    ["letters", "abc"],
    ["digits around a space", "12 34"],
];

// COTRN00C blanks TRNIDIN after ENTER with a numeric key and after an F8 that pages on, and keeps
// it after every F7. The page follows the answer's search_cleared, never the key or the literal.
const SEARCH_LIFECYCLE_CASES: readonly SearchLifecycleCase[] = [
    { key: "ENTER", searchCleared: true, literal: REACHED_BOTTOM, shownSearch: "" },
    { key: "F8", searchCleared: true, literal: REACHED_BOTTOM, shownSearch: "" },
    { key: "F7", searchCleared: false, literal: TOP_ALREADY, shownSearch: SEARCH_KEY },
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

// TAMTnnn as the server serialises TRAN-AMT, a signed decimal string with two decimals: negative
// on odd rows, so both signs are shown.
function AmountFor(rowNumber: number): string {
    const sign = rowNumber % 2 === 1 ? "-" : "";
    return `${sign}${rowNumber}.${String(rowNumber).padStart(2, "0")}`;
}

// One COTRN00 row as the server sends it: TRNIDnn, TDATEnn (MM/DD/YY), TDESCnn (at most 26
// characters) and TAMTnnn. Every cell value differs from the row's other cells and from every
// other row's, so a cell bound to the wrong property or left blank fails.
function BuildRow(rowNumber: number): TransactionListRow {
    return {
        tran_id: TranIdFor(rowNumber),
        tran_date: `06/${String(rowNumber).padStart(2, "0")}/22`,
        tran_desc: `Purchase at merchant ${rowNumber}`,
        tran_amt: AmountFor(rowNumber),
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

// The body of LegacyMessageError(field=search_tran_id), which the TRANSACT lookup failure and
// both edit failures raise.
function BuildSearchErrorBody(message: string): LegacyErrorResponse {
    return BuildErrorBody({ message, field_errors: [SEARCH_FIELD], cursor_field: SEARCH_FIELD });
}

// A rejection whose body adds the list page, if any, to the four legacy keys.
function BuildRefusal(status: number, literal: string, refusedPage?: ListPageBody): AxiosError {
    return BuildAxiosError(status, { ...BuildSearchErrorBody(literal), ...refusedPage });
}

// options.reactStrictMode mounts the page as next.config's reactStrictMode does in development.
function RenderScreen(options: RenderOptions = {}): UserEvent {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <TransactionListPage />
        </ThemeProvider>,
        options,
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

// The TRNIDnn, TDATEnn, TDESCnn and TAMTnnn cells after the Sel cell of every body row, in
// screen order.
function ShownRows(): TransactionListRow[] {
    const bodyRows = screen.getAllByRole("row").slice(1);
    return bodyRows.map((row) => {
        const cellTexts = within(row)
            .getAllByRole("cell")
            .map((cell) => cell.textContent ?? "");
        return {
            tran_id: cellTexts[1] ?? "",
            tran_date: cellTexts[2] ?? "",
            tran_desc: cellTexts[3] ?? "",
            tran_amt: cellTexts[4] ?? "",
        };
    });
}

// The ten rows from firstRowNumber are shown, every cell as the server sent it.
function ExpectShownRows(firstRowNumber: number): void {
    expect(ShownRows()).toEqual(BuildRows(firstRowNumber));
}

// A legend key pressed through its FunctionKeyBar button or as the physical key.
async function PressKey(user: UserEvent, key: ListKey, mode: PressMode): Promise<void> {
    if (mode === "key") {
        await user.keyboard(KEYBOARD_KEYS[key]);
        return;
    }
    await user.click(screen.getByRole("button", { name: KEY_BUTTON_NAMES[key] }));
}

function KeyButton(key: ListKey): HTMLElement {
    return screen.getByRole("button", { name: KEY_BUTTON_NAMES[key] });
}

// A physical key press dispatched straight to the window, where useFunctionKeys listens.
function DispatchKeyDown(key: ListKey): void {
    const init = { key: PF_KEY_ACTIONS[key], bubbles: true, cancelable: true };
    window.dispatchEvent(new KeyboardEvent("keydown", init));
}

// Every ListTransactions and SelectTransactions call, the first entry's included.
function RequestCount(): number {
    return mockListTransactions.mock.calls.length + mockSelectTransactions.mock.calls.length;
}

function ExpectRequestKeysDisabled(isDisabled: boolean): void {
    for (const key of REQUEST_KEYS) {
        expect(KeyButton(key)).toHaveProperty("disabled", isDisabled);
    }
}

// Presses ENTER, F7 and F8 as physical keys from TRNIDIN and clicks each button. user-event
// refuses to click a disabled MUI button (pointer-events none), so the click event is fired
// directly; focus stays in TRNIDIN, where ENTER belongs to the screen.
async function PressLockedKeys(user: UserEvent): Promise<void> {
    await user.click(SearchField());
    for (const key of REQUEST_KEYS) {
        await PressKey(user, key, "key");
        fireEvent.click(KeyButton(key));
    }
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

// A flagged TRNIDIN names the ERRMSG Alert that holds the literal; an unflagged one names nothing.
function ExpectSearchDescription({ literal, describedBy }: SearchDescriptionCase): void {
    if (describedBy === null) {
        expect(SearchField()).not.toHaveAttribute("aria-describedby");
        return;
    }
    expect(SearchField()).toHaveAttribute("aria-describedby", describedBy);
    expect(document.getElementById(describedBy)).toBe(screen.getByRole("alert"));
    expect(SearchField()).toHaveAccessibleDescription(literal);
}

// A 200 answer carries the literal over the shown page; any other status rejects the call, its
// body adding the case's list page, if any, to the four legacy keys.
function OutcomeFor(literalCase: LiteralCase): Promise<TransactionSelectionResponse> {
    const { literal, status, refusedPage } = literalCase;
    if (status === HTTP_OK) {
        return Promise.resolve(BuildListResponse({ message: literal }));
    }
    return Promise.reject(BuildRefusal(status, literal, refusedPage));
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
function MockKeyAnswer(key: ListKey, answer: () => Promise<TransactionSelectionResponse>): void {
    if (key === "ENTER") {
        mockSelectTransactions.mockImplementationOnce(answer);
        return;
    }
    mockListTransactions.mockImplementationOnce(answer);
}

function MockKeyOutcome(literalCase: LiteralCase): void {
    MockKeyAnswer(literalCase.key, () => OutcomeFor(literalCase));
}

// A read that never answers: once its signal aborts it rejects with axios's CanceledError, as an
// abandoned API_CLIENT call does.
function PendingUntilAborted(
    _query: TransactionListQuery,
    options?: ReadOptions,
): Promise<TransactionListResponse> {
    return new Promise((_resolve, reject) => {
        options?.signal?.addEventListener("abort", () => reject(new CanceledError()));
    });
}

// Leaves the next ListTransactions read unanswered until the returned function answers it; an
// abort of its signal first rejects it with axios's CanceledError instead.
function DeferListAnswer(): (response: TransactionListResponse) => void {
    const { promise, resolve } = Promise.withResolvers<TransactionListResponse>();
    mockListTransactions.mockImplementationOnce((query, options) =>
        Promise.race([promise, PendingUntilAborted(query, options)]),
    );
    return resolve;
}

// Leaves the next ENTER unanswered until the returned function answers it.
function DeferSelectionAnswer(): (response: TransactionSelectionResponse) => void {
    let answer: (response: TransactionSelectionResponse) => void = () => undefined;
    mockSelectTransactions.mockImplementationOnce(
        () =>
            new Promise((resolve) => {
                answer = resolve;
            }),
    );
    return (response) => answer(response);
}

// The signal the page gave the ListTransactions call at callIndex, counted from 0.
function ListCallSignal(callIndex: number): AbortSignal | undefined {
    return mockListTransactions.mock.calls[callIndex]?.[1]?.signal;
}

// One macrotask runs every settled promise chain first, so any answer the page would apply has
// been applied before an assertion that nothing was.
async function FlushPendingAnswers(): Promise<void> {
    await act(() => new Promise<void>((resolve) => setTimeout(resolve, 0)));
}

// The first-entry read, left unanswered.
async function StartPendingFirstEntry(): Promise<UserEvent> {
    mockListTransactions.mockImplementationOnce(PendingUntilAborted);
    const user = RenderScreen();
    await waitFor(() => expect(mockListTransactions).toHaveBeenCalledTimes(1));
    return user;
}

// An F8 read over the first page, left unanswered.
async function StartPendingForward(): Promise<UserEvent> {
    const user = await RenderFirstPage();
    mockListTransactions.mockImplementationOnce(PendingUntilAborted);
    await PressKey(user, "F8", "button");
    return user;
}

const PENDING_READ_CASES: readonly PendingReadCase[] = [
    { read: "the first-entry read", callIndex: 0, StartRead: StartPendingFirstEntry },
    { read: "an F8 read", callIndex: 1, StartRead: StartPendingForward },
];

// The page the server browses for both SEARCH_CASES: rows 13-22, the first at or after the key.
const FOUND_FIRST_ROW_NUMBER = 13;
const FOUND_PAGE: Partial<TransactionListResponse> = {
    rows: BuildRows(FOUND_FIRST_ROW_NUMBER),
    first_key: TranIdFor(FOUND_FIRST_ROW_NUMBER),
    last_key: TranIdFor(FOUND_FIRST_ROW_NUMBER + PAGE_SIZE - 1),
};

// The page a read held open answers with: rows 21-30, page 3.
const LATEST_FIRST_ROW_NUMBER = 21;
const LATEST_PAGE: Partial<TransactionListResponse> = {
    rows: BuildRows(LATEST_FIRST_ROW_NUMBER),
    page: 3,
    first_key: TranIdFor(LATEST_FIRST_ROW_NUMBER),
    last_key: TranIdFor(LATEST_FIRST_ROW_NUMBER + PAGE_SIZE - 1),
};

// The page F8 shows after the first: rows 11-20 on page 2.
const SECOND_PAGE: ListPageBody = { rows: BuildRows(11), page: 2 };
// The page an invalid-selection 400 carries after a search: rows 13-22, back on page 1, with
// TRNIDIN blanked, as the invalid code sets no ERR-FLG.
const REBROWSED_PAGE: ListPageBody = { ...FOUND_PAGE, page: 1, search_cleared: true };
const REBROWSED_FORWARD_QUERY: TransactionListQuery = {
    startKey: FOUND_PAGE.last_key,
    firstKey: FOUND_PAGE.first_key,
    direction: Direction.FORWARD,
    page: 1,
};

// After a non-numeric search key F8 pages from the keys the page kept; after an invalid
// selection code, from the keys of the page its 400 carried.
const REFUSAL_PAGING_CASES: readonly RefusalPagingCase[] = [
    { literal: NUMERIC_SEARCH, query: FORWARD_QUERY },
    { literal: INVALID_SELECTION, refusedPage: REBROWSED_PAGE, query: REBROWSED_FORWARD_QUERY },
];

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
        expect(mockListTransactions).toHaveBeenCalledWith({}, READ_OPTIONS);
        expect(screen.getByRole("heading", { level: 1, name: SCREEN_TITLE })).toBeVisible();
        expect(screen.getByText("Tran: CT00")).toBeVisible();
        expect(screen.getByText("Prog: COTRN00C")).toBeVisible();
        ExpectShownRows(1);
        expect(screen.getByText("Page:")).toBeVisible();
        expect(screen.getByText("1")).toBeVisible();
        expect(screen.queryByRole("alert")).toBeNull();
    });
});

// The row-21 ASKIP instruction of bms/COTRN00.bms lines 444-449 (LENGTH=50): the doubled
// quotes of INITIAL are single quotes, and the column-16 continuation supplies " list".
const SELECTION_INSTRUCTION = "Type 'S' to View Transaction details from the list";

describe("COTRN00C BMS COTRN00 fields", () => {
    it("COTRN00C TRNIDIN, headings, Sel codes and the instruction follow COTRN00", async () => {
        const user = await RenderFirstPage();
        expect(SearchField()).toHaveAttribute("maxlength", SEARCH_MAX_LENGTH);
        const headers = screen.getAllByRole("columnheader").map((header) => header.textContent);
        expect(headers).toEqual(COLUMN_HEADERS);
        expect(screen.getByText(SELECTION_INSTRUCTION)).toBeVisible();
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

// Routes cotrn00c._ViewRoute never builds: other sites, a script, a scheme-relative host, the
// view page without its tranId or with another key beside it, another screen, a dot segment out
// of the view page, and a non-string.
const UNEXPECTED_SELECTION_ROUTES: readonly unknown[] = [
    "https://evil.example/transactions/view?tranId=1",
    "javascript:alert(1)",
    "//evil.example/transactions/view?tranId=1",
    "/transactions/view",
    "/transactions/view?tranId=1&x=2",
    "/admin/users?tranId=1",
    "/transactions/view/../admin?tranId=1",
    42,
];

// cotrn00c's urlencode writes a blank as "+"; another spelling of the same TRAN-ID is rebuilt
// with URLSearchParams before it reaches the router, so the view page reads the same tranId.
const RE_ENCODED_ROUTE_CASES: readonly [string, string][] = [
    ["/transactions/view?tranId=00000000000000%303", VIEW_ROUTE],
    ["/transactions/view?tranId=0000%2000000000003", "/transactions/view?tranId=0000+00000000003"],
];

// CT00 renders exactly one role="status" region, its own polite result status: COTRN00C sends no
// INFOMSG, and MessageLine shows ERRMSG as a role="alert" Alert.
function ResultStatusRegion(): HTMLElement {
    const regions = screen.getAllByRole("status");
    expect(regions).toHaveLength(1);
    const [resultStatus] = regions as [HTMLElement];
    expect(resultStatus).toHaveAttribute("aria-live", "polite");
    return resultStatus;
}

// A 200 selection answer carrying a route its TypeScript type rules out, as unchecked JSON can.
function SelectionAnswering(route: unknown): TransactionSelectionResponse {
    const answer = { route };
    return answer as TransactionSelectionResponse;
}

// ENTER over an S selection and typed search text, answered with the given route.
async function EnterSelectionAnswered(route: unknown): Promise<void> {
    const user = await RenderFirstPage();
    mockSelectTransactions.mockResolvedValueOnce(SelectionAnswering(route));
    await ChooseSelection(user, SELECTED_ROW_NUMBER, "S");
    await user.type(SearchField(), SEARCH_KEY);
    await PressKey(user, "ENTER", "button");
}

describe("COTRN00C ENTER answered with an unexpected route", () => {
    it.each(UNEXPECTED_SELECTION_ROUTES)(
        "COTRN00C ENTER answered with route %p shows the abend text and never navigates",
        async (route) => {
            await EnterSelectionAnswered(route);
            await ExpectAlert(ABEND, "error");
            expect(mockRouterPush).not.toHaveBeenCalled();
            expect(mockRouterReplace).not.toHaveBeenCalled();
            ExpectShownRows(1);
            expect(screen.getByText("1")).toBeVisible();
            expect(SearchField()).toHaveValue(SEARCH_KEY);
            expect(SearchField()).toHaveFocus();
            expect(ResultStatusRegion().textContent).toBe("");
            ExpectRequestKeysDisabled(false);
        },
    );
});

describe("COTRN00C ENTER answered with a differently encoded view route", () => {
    it.each(RE_ENCODED_ROUTE_CASES)(
        "COTRN00C ENTER answered with %s opens %s",
        async (servedRoute, canonicalRoute) => {
            await EnterSelectionAnswered(servedRoute);
            await waitFor(() => expect(mockRouterPush).toHaveBeenCalledWith(canonicalRoute));
            expect(mockRouterPush).toHaveBeenCalledTimes(1);
            expect(screen.queryByRole("alert")).toBeNull();
        },
    );
});

// The four-key 400 leaves the list unchanged; ENTER is clicked, so the focus on TRNIDIN comes
// from cursor_field rather than from the typing.
describe("COTRN00C ENTER with a non-numeric search key", () => {
    it.each(NON_NUMERIC_SEARCH_CASES)(
        "COTRN00C ENTER with %s answered 400 keeps the list, marks TRNIDIN and keeps its text",
        async (_caseName, searchKey) => {
            const user = await RenderFirstPage();
            const refusal = BuildRefusal(HTTP_EDIT_FAILURE, NUMERIC_SEARCH);
            mockSelectTransactions.mockRejectedValueOnce(refusal);
            await user.type(SearchField(), searchKey);
            await PressKey(user, "ENTER", "button");
            await ExpectAlert(NUMERIC_SEARCH, "error");
            ExpectSearchOnlyRequest(searchKey);
            ExpectShownRows(1);
            expect(screen.getByText("1")).toBeVisible();
            expect(SearchField()).toHaveAttribute("aria-invalid", "true");
            expect(SearchField()).toHaveFocus();
            expect(SearchField()).toHaveValue(searchKey);
            expect(mockRouterPush).not.toHaveBeenCalled();
        },
    );
});

// The map offers only S, so the 400 stands for the server's verdict on a code it was sent; the
// browse restarted from the search key, so the page number returns from 2 to 1, and its
// search_cleared blanks TRNIDIN.
describe("COTRN00C ENTER answered 400 with an invalid selection code", () => {
    it("COTRN00C invalid selection shows the re-browsed page under the red literal", async () => {
        const user = await RenderFirstPage();
        mockListTransactions.mockResolvedValueOnce(BuildListResponse(SECOND_PAGE));
        await PressKey(user, "F8", "button");
        await screen.findByText("2");
        const refusal = BuildRefusal(HTTP_EDIT_FAILURE, INVALID_SELECTION, REBROWSED_PAGE);
        mockSelectTransactions.mockRejectedValueOnce(refusal);
        await user.type(SearchField(), SEARCH_KEY);
        await PressKey(user, "ENTER", "button");
        await ExpectAlert(INVALID_SELECTION, "error");
        ExpectShownRows(FOUND_FIRST_ROW_NUMBER);
        expect(screen.getByText("1")).toBeVisible();
        expect(SearchField()).toHaveAttribute("aria-invalid", "true");
        expect(SearchField()).toHaveFocus();
        expect(SearchField()).toHaveValue("");
    });
});

describe("COTRN00C F8 after an edit failure", () => {
    it.each(REFUSAL_PAGING_CASES)(
        "COTRN00C F8 after $literal pages from the keys shown",
        async ({ literal, refusedPage, query }) => {
            const user = await RenderFirstPage();
            const refusal = BuildRefusal(HTTP_EDIT_FAILURE, literal, refusedPage);
            mockSelectTransactions.mockRejectedValueOnce(refusal);
            await PressKey(user, "ENTER", "button");
            await ExpectAlert(literal, "error");
            const pagingAnswer = BuildListResponse({ message: BOTTOM_ALREADY });
            mockListTransactions.mockResolvedValueOnce(pagingAnswer);
            await PressKey(user, "F8", "button");
            await screen.findByText(BOTTOM_ALREADY);
            expect(mockListTransactions).toHaveBeenLastCalledWith(query, READ_OPTIONS);
        },
    );
});

describe("COTRN00C TRNIDIN after an answer", () => {
    it.each(SEARCH_LIFECYCLE_CASES)(
        "COTRN00C $key answered search_cleared $searchCleared shows TRNIDIN '$shownSearch'",
        async ({ key, searchCleared, literal, shownSearch }) => {
            const user = await RenderFirstPage();
            const answer = BuildListResponse({ message: literal, search_cleared: searchCleared });
            MockKeyAnswer(key, () => Promise.resolve(answer));
            await user.type(SearchField(), SEARCH_KEY);
            await PressKey(user, key, "button");
            await ExpectAlert(literal, "error");
            expect(SearchField()).toHaveValue(shownSearch);
        },
    );
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
            ExpectShownRows(FOUND_FIRST_ROW_NUMBER);
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
            expect(mockListTransactions).toHaveBeenLastCalledWith(query, READ_OPTIONS);
            ExpectShownRows(11);
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
            ExpectShownRows(1);
            expect(mockRouterPush).not.toHaveBeenCalled();
        },
    );

    it("COTRN00C first entry shows the literal the first page carries", async () => {
        mockListTransactions.mockResolvedValueOnce(BuildListResponse({ message: REACHED_BOTTOM }));
        RenderScreen();
        await ExpectAlert(REACHED_BOTTOM, "error");
        ExpectShownRows(1);
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

// The page a pending F7, F8 or ENTER finally answers with: rows 11-20 as page 2.
const PENDING_ANSWER = BuildListResponse({ rows: BuildRows(11), page: 2 });

describe("COTRN00C keyboard lock while a request is out", () => {
    it.each(REQUEST_KEYS)(
        "COTRN00C %s locks ENTER, F7 and F8 until it is answered",
        async (key) => {
            const user = await RenderFirstPage();
            const { promise, resolve } = Promise.withResolvers<TransactionSelectionResponse>();
            MockKeyAnswer(key, () => promise);
            await PressKey(user, key, "button");
            ExpectRequestKeysDisabled(true);
            expect(KeyButton("F3")).toBeEnabled();
            await PressLockedKeys(user);
            expect(RequestCount()).toBe(2);
            resolve(PENDING_ANSWER);
            await screen.findByText(TranIdFor(20));
            ExpectRequestKeysDisabled(false);
            expect(RequestCount()).toBe(2);
        },
    );
});

// TRNIDIN is read-only while a request is out: typing into it changes nothing.
async function ExpectSearchLocked(user: UserEvent, heldText: string): Promise<void> {
    expect(SearchField()).toHaveAttribute("readonly");
    await user.type(SearchField(), NEXT_SEARCH_KEY);
    expect(SearchField()).toHaveValue(heldText);
}

// Mounts the page with the first browse held open, checks TRNIDIN is locked meanwhile, then
// answers it with the first page.
async function RenderFirstPageAfterLock(): Promise<UserEvent> {
    const { promise, resolve } = Promise.withResolvers<TransactionListResponse>();
    mockListTransactions.mockReturnValueOnce(promise);
    const user = RenderScreen();
    await ExpectSearchLocked(user, "");
    resolve(BuildListResponse());
    await screen.findByText(TranIdFor(PAGE_SIZE));
    return user;
}

// ENTER over the next search key, answered with the page found from it.
async function SearchFromNextKey(user: UserEvent): Promise<void> {
    mockSelectTransactions.mockResolvedValueOnce(BuildListResponse(FOUND_PAGE));
    await user.type(SearchField(), NEXT_SEARCH_KEY);
    await PressKey(user, "ENTER", "button");
    await screen.findByText(TranIdFor(FOUND_FIRST_ROW_NUMBER + PAGE_SIZE - 1));
}

// The 3270 keyboard is locked from a press until the answer, so no key typed meanwhile can be
// blanked by that answer's search_cleared; the next ENTER sends the key typed after it.
describe("COTRN00C TRNIDIN locked while a request is out", () => {
    it.each(SEARCH_CLEARING_KEYS)(
        "COTRN00C %s keeps TRNIDIN read-only until its search_cleared answer",
        async (key) => {
            const user = await RenderFirstPageAfterLock();
            const { promise, resolve } = Promise.withResolvers<TransactionSelectionResponse>();
            MockKeyAnswer(key, () => promise);
            await user.type(SearchField(), SEARCH_KEY);
            await PressKey(user, key, "button");
            await ExpectSearchLocked(user, SEARCH_KEY);
            resolve(BuildListResponse({ rows: BuildRows(11), search_cleared: true }));
            await screen.findByText(TranIdFor(20));
            expect(SearchField()).toHaveValue("");
            expect(SearchField()).not.toHaveAttribute("readonly");
            await SearchFromNextKey(user);
            ExpectSearchOnlyRequest(NEXT_SEARCH_KEY);
        },
    );
});

// Both presses land before React re-renders, so the disabled F8 cannot stop the second one.
describe("COTRN00C keys pressed twice in one frame", () => {
    it("COTRN00C two F8 presses in one frame send one request", async () => {
        await RenderFirstPage();
        const { promise, resolve } = Promise.withResolvers<TransactionListResponse>();
        mockListTransactions.mockReturnValueOnce(promise);
        act(() => {
            DispatchKeyDown("F8");
            DispatchKeyDown("F8");
        });
        resolve(PENDING_ANSWER);
        await screen.findByText(TranIdFor(20));
        expect(mockListTransactions).toHaveBeenCalledTimes(2);
        expect(mockListTransactions).toHaveBeenLastCalledWith(FORWARD_QUERY, READ_OPTIONS);
    });
});

describe("COTRN00C F3 while a request is out", () => {
    it("COTRN00C F3 returns to the main menu and discards the pending answer", async () => {
        const user = await RenderFirstPage();
        const { promise, resolve } = Promise.withResolvers<TransactionListResponse>();
        mockListTransactions.mockReturnValueOnce(promise);
        await PressKey(user, "F8", "button");
        await PressKey(user, "F3", "key");
        expect(mockRouterPush).toHaveBeenCalledWith(MENU_ROUTE);
        // A macrotask lets the answer settle and run every continuation of the page's request.
        await act(async () => {
            resolve(PENDING_ANSWER);
            await new Promise((resolveTick) => setTimeout(resolveTick, 0));
        });
        ExpectShownRows(1);
        expect(mockRouterPush).toHaveBeenCalledTimes(1);
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
        ExpectShownRows(1);
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

describe("COTRN00C TRNIDIN described by ERRMSG", () => {
    it.each(SEARCH_DESCRIPTION_CASES)(
        "COTRN00C $key answered $status with $literal: TRNIDIN aria-describedby $describedBy",
        async (descriptionCase) => {
            const user = await RenderFirstPage();
            MockKeyOutcome(descriptionCase);
            await PressKey(user, descriptionCase.key, "button");
            await ExpectAlert(descriptionCase.literal, "error");
            ExpectSearchDescription(descriptionCase);
        },
    );
});

// The cleanup between the two effect runs aborts the first read and frees the lock, so the second
// read is sent; the first read's late cancellation leaves the keys locked until the second answers.
describe("COTRN00C first entry under React Strict Mode", () => {
    it("COTRN00C Strict Mode aborts the first read and shows the second one", async () => {
        StorePendingMessage(CARRIED_MESSAGE);
        mockListTransactions.mockImplementationOnce(PendingUntilAborted);
        const answerSecondRead = DeferListAnswer();
        RenderScreen({ reactStrictMode: true });
        await waitFor(() => expect(mockListTransactions).toHaveBeenCalledTimes(2));
        await FlushPendingAnswers();
        expect(ListCallSignal(0)?.aborted).toBe(true);
        ExpectRequestKeysDisabled(true);
        await act(async () => answerSecondRead(BuildListResponse()));
        await screen.findByText(TranIdFor(PAGE_SIZE));
        expect(mockListTransactions).toHaveBeenNthCalledWith(1, {}, READ_OPTIONS);
        expect(mockListTransactions).toHaveBeenNthCalledWith(2, {}, READ_OPTIONS);
        expect(ListCallSignal(1)?.aborted).toBe(false);
        ExpectRequestKeysDisabled(false);
        await ExpectAlert(CARRIED_MESSAGE, "error");
    });
});

describe("COTRN00C unmount with a read pending", () => {
    it.each(PENDING_READ_CASES)(
        "COTRN00C unmount while $read is pending aborts it and applies nothing",
        async ({ callIndex, StartRead }) => {
            await StartRead();
            expect(ListCallSignal(callIndex)?.aborted).toBe(false);
            // cleanup() unmounts the page, as leaving /transactions does.
            cleanup();
            await FlushPendingAnswers();
            expect(ListCallSignal(callIndex)?.aborted).toBe(true);
            expect(mockRouterPush).not.toHaveBeenCalled();
        },
    );
});

describe("COTRN00C F3 with a read pending", () => {
    it.each(PENDING_READ_CASES)(
        "COTRN00C F3 while $read is pending aborts it and returns to the main menu",
        async ({ callIndex, StartRead }) => {
            const user = await StartRead();
            await PressKey(user, "F3", "button");
            await FlushPendingAnswers();
            expect(ListCallSignal(callIndex)?.aborted).toBe(true);
            expect(mockRouterPush).toHaveBeenCalledTimes(1);
            expect(mockRouterPush).toHaveBeenCalledWith(MENU_ROUTE);
            expect(screen.queryByRole("alert")).toBeNull();
        },
    );
});

// A key pressed in the frame of an F8, before its keys disable, reaches the runner, whose lock
// refuses it: the press neither supersedes nor aborts the F8 read, whose answer is then shown.
describe("COTRN00C keys pressed over a pending read", () => {
    it.each(REQUEST_KEYS)(
        "COTRN00C %s pressed in the frame of an F8 leaves the F8 read running",
        async (key) => {
            await RenderFirstPage();
            const answerRead = DeferListAnswer();
            act(() => {
                DispatchKeyDown("F8");
                DispatchKeyDown(key);
            });
            await FlushPendingAnswers();
            expect(RequestCount()).toBe(2);
            expect(ListCallSignal(1)?.aborted).toBe(false);
            await act(async () => answerRead(BuildListResponse(LATEST_PAGE)));
            await screen.findByText(TranIdFor(LATEST_FIRST_ROW_NUMBER + PAGE_SIZE - 1));
            expect(screen.getByText("3")).toBeVisible();
            expect(screen.queryByRole("alert")).toBeNull();
        },
    );
});

// F3 aborts the controller the ENTER selection was offered, but a selection never takes the
// signal, so it runs to its answer, which F3 discards: no view route follows the menu.
describe("COTRN00C F3 over a pending selection", () => {
    it("COTRN00C F3 discards a pending ENTER's answer; the selection takes no signal", async () => {
        const user = await RenderFirstPage();
        const answerSelection = DeferSelectionAnswer();
        await ChooseSelection(user, SELECTED_ROW_NUMBER, "S");
        await PressKey(user, "ENTER", "button");
        await PressKey(user, "F3", "button");
        await act(async () => answerSelection({ route: VIEW_ROUTE }));
        await FlushPendingAnswers();
        expect(mockSelectTransactions.mock.calls[0]).toHaveLength(1);
        expect(mockRouterPush).toHaveBeenCalledTimes(1);
        expect(mockRouterPush).toHaveBeenCalledWith(MENU_ROUTE);
    });
});

// A cancelled read still current shows nothing, but frees the lock and unlocks the keys, so the
// next press is answered.
describe("COTRN00C cancelled read", () => {
    it.each(PAGE_KEYS)(
        "COTRN00C %s rejected with a CanceledError shows no message and unlocks the keys",
        async (key) => {
            const user = await RenderFirstPage();
            mockListTransactions.mockRejectedValueOnce(new CanceledError());
            await PressKey(user, key, "button");
            await FlushPendingAnswers();
            expect(mockListTransactions).toHaveBeenCalledTimes(2);
            expect(screen.queryByRole("alert")).toBeNull();
            ExpectShownRows(1);
            ExpectRequestKeysDisabled(false);
            mockListTransactions.mockResolvedValueOnce(BuildListResponse(LATEST_PAGE));
            await PressKey(user, key, "button");
            await screen.findByText(TranIdFor(LATEST_FIRST_ROW_NUMBER + PAGE_SIZE - 1));
        },
    );
});

// The page's result status reports the page number, the row count and the first and last TRAN-ID
// shown. It is the page's own accessibility text, not a COTRN00C literal.
const FIRST_PAGE_STATUS = "Page 1: 10 transactions, 0000000000000001 to 0000000000000010";
const SECOND_PAGE_STATUS = "Page 2: 10 transactions, 0000000000000011 to 0000000000000020";

// A first-entry answer and the status it gives: a full page, one row, and the page 0 without
// rows that an empty TRANSACT gives, under its literal.
const FIRST_ENTRY_STATUS_CASES: readonly [string, TransactionListResponse, string][] = [
    ["a full page", BuildListResponse(), FIRST_PAGE_STATUS],
    [
        "one row",
        BuildListResponse({ rows: [BuildRow(7)] }),
        "Page 1: 1 transaction, 0000000000000007 to 0000000000000007",
    ],
    [
        "an empty TRANSACT",
        BuildListResponse({ rows: [], page: 0, message: AT_TOP }),
        "Page 0: no transactions",
    ],
];

// The literals a 200 page carries, and the refusals, which leave the status empty.
const PAGE_LITERAL_CASES = LITERAL_CASES.filter((literalCase) => literalCase.status === HTTP_OK);
const REFUSAL_LITERAL_CASES = LITERAL_CASES.filter((literalCase) => literalCase.status !== HTTP_OK);

describe("COTRN00C result status after first entry", () => {
    it.each(FIRST_ENTRY_STATUS_CASES)(
        "COTRN00C first entry answered with %s announces the page shown",
        async (_caseName, answer, statusText) => {
            mockListTransactions.mockResolvedValueOnce(answer);
            RenderScreen();
            await waitFor(() => expect(ResultStatusRegion().textContent).toBe(statusText));
            expect(screen.queryByRole("alert")?.textContent ?? "").toBe(answer.message);
        },
    );

    it("COTRN00C result status is rendered empty while the first entry is pending", async () => {
        await StartPendingFirstEntry();
        expect(ResultStatusRegion().textContent).toBe("");
    });
});

describe("COTRN00C result status after F7 and F8", () => {
    it.each(PAGE_KEYS)("COTRN00C %s announces the page it shows", async (key) => {
        const user = await RenderFirstPage();
        expect(ResultStatusRegion().textContent).toBe(FIRST_PAGE_STATUS);
        mockListTransactions.mockResolvedValueOnce(BuildListResponse(SECOND_PAGE));
        await PressKey(user, key, "button");
        await screen.findByText(TranIdFor(20));
        expect(ResultStatusRegion().textContent).toBe(SECOND_PAGE_STATUS);
        expect(screen.queryByRole("alert")).toBeNull();
    });
});

// The status empties when a request starts, so an answer identical to the page shown changes it
// from empty again and is announced again.
describe("COTRN00C result status while a request is out", () => {
    it.each(REQUEST_KEYS)(
        "COTRN00C %s empties the status until its answer, which is announced again",
        async (key) => {
            const user = await RenderFirstPage();
            const { promise, resolve } = Promise.withResolvers<TransactionSelectionResponse>();
            MockKeyAnswer(key, () => promise);
            await PressKey(user, key, "button");
            expect(ResultStatusRegion().textContent).toBe("");
            await act(async () => resolve(BuildListResponse()));
            await waitFor(() => expect(ResultStatusRegion().textContent).toBe(FIRST_PAGE_STATUS));
            ExpectRequestKeysDisabled(false);
        },
    );
});

describe("COTRN00C result status beside the ERRMSG line", () => {
    it.each(PAGE_LITERAL_CASES)(
        "COTRN00C $key answered 200 with $literal shows it in red and announces the page",
        async (literalCase) => {
            const user = await RenderFirstPage();
            MockKeyOutcome(literalCase);
            await PressKey(user, literalCase.key, "button");
            await ExpectAlert(literalCase.literal, "error");
            expect(ResultStatusRegion().textContent).toBe(FIRST_PAGE_STATUS);
            expect(SearchField()).toHaveFocus();
        },
    );
});

describe("COTRN00C result status after a refusal", () => {
    it.each(REFUSAL_LITERAL_CASES)(
        "COTRN00C $key answered $status shows $literal in red and leaves the status empty",
        async (literalCase) => {
            const user = await RenderFirstPage();
            MockKeyOutcome(literalCase);
            await PressKey(user, literalCase.key, "button");
            await ExpectAlert(literalCase.literal, "error");
            expect(ResultStatusRegion().textContent).toBe("");
        },
    );
});
