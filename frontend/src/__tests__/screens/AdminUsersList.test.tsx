// Screen tests: BMS COUSR00, program COUSR00C (CU00), page /admin/users.
import { ThemeProvider } from "@mui/material/styles";
import {
    act,
    cleanup,
    render,
    screen,
    waitFor,
    within,
    type RenderOptions,
} from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import {
    AxiosError,
    AxiosHeaders,
    CanceledError,
    type AxiosResponse,
    type InternalAxiosRequestConfig,
} from "axios";
import type { ReactNode } from "react";

import AdminUsersListPage from "@/app/admin/users/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import { SelectionLabelFor } from "@/components/SelectionTable";
import type { CurrentUserResponse } from "@/lib/api/auth";
import type * as UsersApiModule from "@/lib/api/users";
import {
    ListUsers,
    SelectUsers,
    type UserListQuery,
    type UserListResponse,
    type UserListRow,
    type UserSelection,
    type UserSelectionRequest,
    type UserSelectionResponse,
} from "@/lib/api/users";
import {
    Direction,
    PENDING_MESSAGE_KEY,
    SIGN_ON_ROUTE,
    type LegacyErrorResponse,
    type MessageSeverity,
    type PageState,
    type ReadOptions,
} from "@/lib/apiClient";
import { PF_KEY_ACTIONS, type PfKey } from "@/lib/useFunctionKeys";

const mockRouterPush = jest.fn();
const mockRouterReplace = jest.fn();
// One router object for every render, as the App Router keeps a stable instance.
const mockRouter = {
    push: mockRouterPush,
    replace: mockRouterReplace,
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};
// COUSR00 is entered with no query keys; the page reads none.
const mockSearchParams = new URLSearchParams();
// The signed-on administrator, type A as COSGN00C routes to COADM01C and its user list.
const MOCK_CURRENT_USER: CurrentUserResponse = {
    user_id: "TESTUSRZ",
    user_type: "A",
    first_name: "Testadmin",
    last_name: "Tester",
    next_route: "/admin",
};

jest.mock("next/navigation", () => ({
    useRouter: () => mockRouter,
    useSearchParams: () => mockSearchParams,
    usePathname: () => "/admin/users",
}));

// The mocked keys mirror the exports of @/lib/session and its SessionContextValue.
jest.mock("@/lib/session", () => ({
    SessionProvider: ({ children }: { children: ReactNode }) => children,
    useSession: () => ({
        currentUser: MOCK_CURRENT_USER,
        isLoading: false,
        RefreshSession: jest.fn(),
        ClearSession: jest.fn(),
    }),
}));

// Every call is stubbed; the module's real enums stay.
jest.mock("@/lib/api/users", () => ({
    ...jest.requireActual<typeof UsersApiModule>("@/lib/api/users"),
    ListUsers: jest.fn(),
    SelectUsers: jest.fn(),
    AddUser: jest.fn(),
    FetchUser: jest.fn(),
    UpdateUser: jest.fn(),
    DeleteUser: jest.fn(),
}));

const mockListUsers = jest.mocked(ListUsers);
const mockSelectUsers = jest.mocked(SelectUsers);

type ListKey = Extract<PfKey, "ENTER" | "F3" | "F7" | "F8">;
type Trigger = "button" | "key";
type ErrorBody = LegacyErrorResponse & Pick<UserListResponse, "rows" | keyof PageState>;

// The legend ENTER=Continue  F3=Back  F7=Backward  F8=Forward as FunctionKeyBar names it.
const KEY_BUTTON_NAMES: Readonly<Record<ListKey, string>> = {
    ENTER: "Continue (Enter)",
    F3: "Back (F3)",
    F7: "Backward (F7)",
    F8: "Forward (F8)",
};
const TRIGGERS: readonly Trigger[] = ["button", "key"];

const SCREEN_TITLE = "List Users";
const SEARCH_LABEL = "Search User ID:";
const PAGE_LABEL = "Page:";
// MUI renders a blank Select value as one zero-width space.
const ZERO_WIDTH_SPACE = "\u200b";
// USRIDIN LENGTH=8 (USRIDINI PIC X(8)).
const SEARCH_MAX_LENGTH = "8";
// The JSON name of USRIDIN in cursor_field and field_errors.
const SEARCH_FIELD = "search_user_id";
const COLUMN_HEADERS = ["Sel", "User ID", "First Name", "Last Name", "Type"];
const SELECTION_INSTRUCTION = "Type 'U' to Update or 'D' to Delete a User from the list";
const OFFERED_SELECTION_CODES = ["", "U", "D"];
const ADMIN_MENU_ROUTE = "/admin";
const TEST_USER_ID = "TESTUSR1";
const SEARCH_TEXT = "TESTUSR";
// Typed with a leading blank and in lower case: ENTER must send it untrimmed and unfolded.
const RAW_SEARCH_TEXT = " testusr";
// The first and last user IDs each page shows: the first_key and last_key cousr00c._BuildPage
// answers, which F7 and F8 send back as an at-most-8-character startKey.
const FIRST_PAGE_FIRST_KEY = "TESTUSR0";
const FIRST_PAGE_LAST_KEY = "TESTUSR9";
const OTHER_PAGE_FIRST_KEY = "TESTUSRA";
const OTHER_PAGE_LAST_KEY = "TESTUSRC";

// cbl/COUSR00C.cbl line 212 (INVALID_SELECTION_MESSAGE in services/users/cousr00c.py).
const INVALID_SELECTION = "Invalid selection. Valid values are U and D";
// The shared paging literals of backend/app/core/messages.py.
const ALREADY_AT_TOP = "You are already at the top of the page...";
const ALREADY_AT_BOTTOM = "You are already at the bottom of the page...";
const AT_TOP_OF_PAGE = "You are at the top of the page...";
const REACHED_BOTTOM = "You have reached the bottom of the page...";
const REACHED_TOP = "You have reached the top of the page...";
// The WHEN OTHER branches of the USRSEC browse paragraphs (cbl/COUSR00C.cbl lines 610, 644, 678).
const UNABLE_TO_LOOKUP_USER = "Unable to lookup User...";
// CSMSG02Y ABEND-MSG, shown for a 500 because the backend registers no catch-all handler.
const ABEND_MESSAGE = "UNEXPECTED ABEND OCCURRED.";
// A message StorePendingMessage carried across a navigation: the apiClient 403 refusal literal,
// whose trailing space is part of it (cbl/COMEN01C.cbl).
const CARRIED_MESSAGE = "No access - Admin Only option... ";
// COUSR00C never changes ERRMSGC, so every message keeps the map's ERRMSG COLOR=RED.
const ERRMSG_SEVERITY: MessageSeverity = "error";

const HTTP_OK = 200;
const HTTP_BAD_REQUEST = 400;
const HTTP_UNAUTHORIZED = 401;
const HTTP_INTERNAL_SERVER_ERROR = 500;
const HTTP_SERVICE_UNAVAILABLE = 503;

// Every ListUsers browse carries the signal that lets the page abandon it.
const READ_OPTIONS: ReadOptions = { signal: expect.any(AbortSignal) };

// The header clock re-renders only when the epoch second changes; a frozen clock keeps its
// one-second tick from rendering outside act().
const FROZEN_CLOCK_MS = new Date(2022, 6, 18, 9, 5, 7).getTime();

// MUI 9 marks an Alert's colour as MuiAlert-color<Severity> (Alert/alertClasses.d.ts).
const ALERT_CLASS_BY_SEVERITY: Readonly<Record<MessageSeverity, string>> = {
    error: "MuiAlert-colorError",
    success: "MuiAlert-colorSuccess",
    info: "MuiAlert-colorInfo",
};

// USRIDnn, FNAMEnn, LNAMEnn and UTYPEnn rows; a user record never carries a password or hash.
function BuildRows(suffixes: readonly string[]): UserListRow[] {
    return suffixes.map((suffix, index) => ({
        user_id: `TESTUSR${suffix}`,
        first_name: `Fname${suffix}`,
        last_name: `Lname${suffix}`,
        user_type: index % 2 === 0 ? "U" : "A",
    }));
}

const FIRST_PAGE_ROWS = BuildRows(["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]);
const OTHER_PAGE_ROWS = BuildRows(["A", "B", "C"]);

// The keys cousr00c._BuildPage answers with a page: its first and last user IDs, blank for none.
function PageKeysOf(rows: readonly UserListRow[]): Pick<PageState, "first_key" | "last_key"> {
    return {
        first_key: rows[0]?.user_id ?? "",
        last_key: rows.at(-1)?.user_id ?? "",
    };
}

// A 200 page whose keys are its own rows', unless the overrides set them.
function BuildListResponse(overrides: Partial<UserListResponse> = {}): UserListResponse {
    const rows = overrides.rows ?? FIRST_PAGE_ROWS;
    return {
        rows,
        page: 1,
        ...PageKeysOf(rows),
        message: "",
        ...overrides,
    };
}

// ENTER's STARTBR NOTFND page (_SearchNotFound in services/users/cousr00c.py): page 1, the
// shown rows read again with their first key, and a blank last key, from which F8 answers
// NEXT-PAGE-NO.
const NOT_FOUND_PAGE: Partial<UserListResponse> = { page: 1, last_key: "" };
// The re-browsed page an invalid-selection 400 body adds to the four standard keys, keyed by
// its own first and last user IDs.
const REBROWSED_PAGE: Partial<UserListResponse> = {
    rows: OTHER_PAGE_ROWS,
    page: 1,
    ...PageKeysOf(OTHER_PAGE_ROWS),
};

// The legacy error body a COUSR00C refusal carries: the cursor on USRIDIN. A paging or
// invalid-selection refusal sets no ERR-FLG and only moves the cursor, so it lists no field.
function BuildErrorBody(overrides: Partial<ErrorBody> = {}): ErrorBody {
    return {
        message: "",
        info_message: "",
        field_errors: [],
        cursor_field: SEARCH_FIELD,
        ...overrides,
    };
}

// The WHEN OTHER lookup failure (503) sets ERR-FLG and lists USRIDIN as its failing field.
function FieldErrorsFor(status: number): string[] {
    return status === HTTP_SERVICE_UNAVAILABLE ? [SEARCH_FIELD] : [];
}

function BuildAxiosError(status: number, body: unknown): AxiosError {
    const config: InternalAxiosRequestConfig = {
        url: "/admin/users",
        method: "get",
        headers: new AxiosHeaders(),
    };
    const response: AxiosResponse = { status, data: body, statusText: "", headers: {}, config };
    return new AxiosError(
        "Request failed",
        AxiosError.ERR_BAD_REQUEST,
        config,
        undefined,
        response,
    );
}

interface ChosenCode {
    rowNumber: number;
    code: string;
}

// Every shown row in display order beside the code at its screen position: blank for every row
// but the chosen one.
function ExpectedSelections(rows: readonly UserListRow[], chosen?: ChosenCode): UserSelection[] {
    return rows.map((row, index) => ({
        user_id: row.user_id,
        selection: index + 1 === chosen?.rowNumber ? chosen.code : "",
    }));
}

function RenderScreen(options: Pick<RenderOptions, "reactStrictMode"> = {}): UserEvent {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <AdminUsersListPage />
        </ThemeProvider>,
        options,
    );
    return user;
}

// The keys unlock once a browse has answered and the page has settled.
async function ExpectKeysReady(): Promise<void> {
    await waitFor(() => {
        expect(screen.getByRole("button", { name: KEY_BUTTON_NAMES.ENTER })).toBeEnabled();
    });
}

async function RenderLoadedScreen(): Promise<UserEvent> {
    const user = RenderScreen();
    await ExpectKeysReady();
    return user;
}

async function PressKey(user: UserEvent, key: PfKey): Promise<void> {
    await user.keyboard(`{${PF_KEY_ACTIONS[key]}}`);
}

async function TriggerKey(user: UserEvent, key: ListKey, trigger: Trigger): Promise<void> {
    if (trigger === "button") {
        await user.click(screen.getByRole("button", { name: KEY_BUTTON_NAMES[key] }));
        return;
    }
    await PressKey(user, key);
}

async function ChooseSelection(user: UserEvent, rowNumber: number, code: string): Promise<void> {
    await user.click(screen.getByRole("combobox", { name: SelectionLabelFor(rowNumber) }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: code }));
}

// The shown text is compared raw, so a literal's spacing must arrive unchanged.
async function ExpectAlert(text: string, severity: MessageSeverity): Promise<void> {
    await waitFor(() => {
        expect(screen.getByRole("alert").textContent).toBe(text);
    });
    expect(screen.getByRole("alert")).toHaveClass(ALERT_CLASS_BY_SEVERITY[severity]);
}

function SearchField(): HTMLElement {
    return screen.getByRole("textbox", { name: SEARCH_LABEL });
}

// The display cells of each body row; the first cell holds the selection field.
function BodyRowTexts(): string[][] {
    return screen
        .getAllByRole("row")
        .slice(1)
        .map((row) =>
            within(row)
                .getAllByRole("cell")
                .slice(1)
                .map((cell) => cell.textContent ?? ""),
        );
}

function RowTextsOf(rows: readonly UserListRow[]): string[][] {
    return rows.map((row) => [row.user_id, row.first_name, row.last_name, row.user_type]);
}

// The code each shown row's SELnnnn field displays, top to bottom, a blank as "".
function ShownSelectionCodes(): string[] {
    return screen
        .getAllByRole("combobox")
        .map((field) => (field.textContent ?? "").replaceAll(ZERO_WIDTH_SPACE, ""));
}

// The page's own polite status, told apart from MessageLine's live region by its Page: text.
function PageStatusRegion(): HTMLElement {
    const pageStatus = screen
        .getAllByRole("status")
        .find((region) => region.textContent?.startsWith(PAGE_LABEL));
    if (pageStatus === undefined) {
        throw new Error("No page status region is shown");
    }
    return pageStatus;
}

beforeEach(() => {
    mockListUsers.mockReset();
    mockSelectUsers.mockReset();
    mockListUsers.mockResolvedValue(BuildListResponse());
    window.sessionStorage.clear();
    jest.spyOn(Date, "now").mockReturnValue(FROZEN_CLOCK_MS);
});

it("COUSR00C first entry browses from a blank key and shows the COUSR00 header", async () => {
    await RenderLoadedScreen();
    expect(mockListUsers).toHaveBeenCalledTimes(1);
    expect(mockListUsers).toHaveBeenCalledWith({}, READ_OPTIONS);
    expect(screen.getByRole("heading", { level: 1, name: SCREEN_TITLE })).toBeVisible();
    expect(screen.getByText("Tran: CU00")).toBeVisible();
    expect(screen.getByText("Prog: COUSR00C")).toBeVisible();
    expect(SearchField()).toHaveAttribute("maxlength", SEARCH_MAX_LENGTH);
    expect(SearchField()).toHaveValue("");
    expect(screen.getByText("Page: 1")).toBeVisible();
});

it("COUSR00C lists USRID01-10 with names and types under the row-8 headings", async () => {
    await RenderLoadedScreen();
    const headers = screen.getAllByRole("columnheader").map((header) => header.textContent);
    expect(headers).toEqual(COLUMN_HEADERS);
    expect(BodyRowTexts()).toEqual(RowTextsOf(FIRST_PAGE_ROWS));
    expect(screen.getAllByRole("combobox")).toHaveLength(FIRST_PAGE_ROWS.length);
    expect(screen.getByText(SELECTION_INSTRUCTION)).toBeVisible();
});

it("COUSR00C legend ENTER=Continue  F3=Back  F7=Backward  F8=Forward is the key bar", async () => {
    await RenderLoadedScreen();
    const keyBar = screen.getByRole("group", { name: "Function keys" });
    const buttonNames = within(keyBar)
        .getAllByRole("button")
        .map((button) => button.textContent);
    expect(buttonNames).toEqual(Object.values(KEY_BUTTON_NAMES));
});

it("COUSR00C SEL0001 offers only a blank, U and D", async () => {
    const user = await RenderLoadedScreen();
    await user.click(screen.getByRole("combobox", { name: SelectionLabelFor(1) }));
    const offeredCodes = within(screen.getByRole("listbox"))
        .getAllByRole("option")
        .map((option) => option.getAttribute("data-value"));
    expect(offeredCodes).toEqual(OFFERED_SELECTION_CODES);
});

// The map, keys included, paints only once the first browse answers, as the first SEND MAP
// painted the whole screen; until then only the header, the busy bar and the title show.
it("COUSR00C locks every key and holds the map back until the first browse answers", async () => {
    let answerFirstBrowse: (response: UserListResponse) => void = () => undefined;
    mockListUsers.mockReturnValueOnce(
        new Promise((resolve) => {
            answerFirstBrowse = resolve;
        }),
    );
    const user = RenderScreen();
    expect(screen.getByRole("heading", { level: 1, name: SCREEN_TITLE })).toBeVisible();
    expect(screen.queryByRole("group", { name: "Function keys" })).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByRole("progressbar", { name: "Processing" })).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "true");
    await PressKey(user, "F3");
    expect(mockRouterPush).not.toHaveBeenCalled();
    answerFirstBrowse(BuildListResponse());
    await ExpectKeysReady();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(BodyRowTexts()).toEqual(RowTextsOf(FIRST_PAGE_ROWS));
});

it("COUSR00C first entry puts the cursor on USRIDIN (MOVE -1 TO USRIDINL)", async () => {
    mockListUsers.mockResolvedValueOnce(BuildListResponse({ cursor_field: SEARCH_FIELD }));
    await RenderLoadedScreen();
    await waitFor(() => {
        expect(SearchField()).toHaveFocus();
    });
});

interface SelectionRouteCase {
    code: string;
    screenName: string;
    route: string;
    trigger: Trigger;
}

// TESTUSR1 is row 2 of the first page: the update (XCTL COUSR02C) and delete (XCTL COUSR03C)
// routes the server answers for a U or D on it.
const SELECTED_ROW_NUMBER = 2;
const SELECTION_ROUTES: readonly Omit<SelectionRouteCase, "trigger">[] = [
    { code: "U", screenName: "update", route: `/admin/users/update?userId=${TEST_USER_ID}` },
    { code: "D", screenName: "delete", route: `/admin/users/delete?userId=${TEST_USER_ID}` },
];
const SELECTION_ROUTE_CASES: readonly SelectionRouteCase[] = SELECTION_ROUTES.flatMap((routeCase) =>
    TRIGGERS.map((trigger) => ({ ...routeCase, trigger })),
);

it.each(SELECTION_ROUTE_CASES)(
    "COUSR00C ENTER by $trigger with $code routes to the $screenName screen ($route)",
    async ({ code, route, trigger }) => {
        const user = await RenderLoadedScreen();
        const response: UserSelectionResponse = { route };
        mockSelectUsers.mockResolvedValueOnce(response);
        await ChooseSelection(user, SELECTED_ROW_NUMBER, code);
        await user.type(SearchField(), SEARCH_TEXT);
        await TriggerKey(user, "ENTER", trigger);
        const expectedRequest: UserSelectionRequest = {
            selections: ExpectedSelections(FIRST_PAGE_ROWS, {
                rowNumber: SELECTED_ROW_NUMBER,
                code,
            }),
            search_user_id: SEARCH_TEXT,
        };
        expect(mockSelectUsers).toHaveBeenCalledWith(expectedRequest);
        expect(mockRouterPush).toHaveBeenCalledWith(route);
    },
);

it("COUSR00C ENTER sends the search key as typed and shows the page browsed from it", async () => {
    const user = await RenderLoadedScreen();
    const searched = BuildListResponse({ rows: OTHER_PAGE_ROWS, message: REACHED_BOTTOM });
    mockSelectUsers.mockResolvedValueOnce(searched);
    await user.type(SearchField(), RAW_SEARCH_TEXT);
    await PressKey(user, "ENTER");
    const expectedRequest: UserSelectionRequest = {
        selections: ExpectedSelections(FIRST_PAGE_ROWS),
        search_user_id: RAW_SEARCH_TEXT,
    };
    expect(mockSelectUsers).toHaveBeenCalledWith(expectedRequest);
    expect(BodyRowTexts()).toEqual(RowTextsOf(OTHER_PAGE_ROWS));
    expect(SearchField()).toHaveValue("");
    expect(mockRouterPush).not.toHaveBeenCalled();
});

interface PageCase {
    key: "F7" | "F8";
    trigger: Trigger;
    startKey: string;
    direction: Direction;
    resultPage: number;
    searchAfter: string;
    nextStartKey: string;
}

// Both keys page from the shown page 2: F8 from its last user ID, F7 from its first. Pressed
// again, each pages from the same end of the page it brought, with that page's number. F8's
// PROCESS-PAGE-FORWARD runs to its end and blanks USRIDIN; F7's PROCESS-PAGE-BACKWARD leaves
// the typed search key.
const SHOWN_PAGE = 2;
const PAGE_DIRECTIONS: readonly Omit<PageCase, "trigger">[] = [
    {
        key: "F8",
        startKey: FIRST_PAGE_LAST_KEY,
        direction: Direction.FORWARD,
        resultPage: 3,
        searchAfter: "",
        nextStartKey: OTHER_PAGE_LAST_KEY,
    },
    {
        key: "F7",
        startKey: FIRST_PAGE_FIRST_KEY,
        direction: Direction.BACKWARD,
        resultPage: 1,
        searchAfter: SEARCH_TEXT,
        nextStartKey: OTHER_PAGE_FIRST_KEY,
    },
];
const PAGE_CASES: readonly PageCase[] = PAGE_DIRECTIONS.flatMap((pageCase) =>
    TRIGGERS.map((trigger) => ({ ...pageCase, trigger })),
);

it.each(PAGE_CASES)(
    "COUSR00C $key by $trigger browses $direction from $startKey, then from $nextStartKey",
    async ({ key, trigger, startKey, direction, resultPage, searchAfter, nextStartKey }) => {
        mockListUsers.mockResolvedValueOnce(BuildListResponse({ page: SHOWN_PAGE }));
        const user = await RenderLoadedScreen();
        const nextPage = BuildListResponse({ rows: OTHER_PAGE_ROWS, page: resultPage });
        mockListUsers.mockResolvedValueOnce(nextPage);
        await user.type(SearchField(), SEARCH_TEXT);
        await TriggerKey(user, key, trigger);
        const expectedQuery: UserListQuery = { startKey, direction, page: SHOWN_PAGE };
        expect(mockListUsers).toHaveBeenLastCalledWith(expectedQuery, READ_OPTIONS);
        expect(BodyRowTexts()).toEqual(RowTextsOf(OTHER_PAGE_ROWS));
        expect(screen.getByText(`Page: ${resultPage}`)).toBeVisible();
        expect(SearchField()).toHaveValue(searchAfter);
        await ExpectKeysReady();
        await TriggerKey(user, key, trigger);
        const nextQuery: UserListQuery = { startKey: nextStartKey, direction, page: resultPage };
        expect(mockListUsers).toHaveBeenLastCalledWith(nextQuery, READ_OPTIONS);
    },
);

interface LiteralCase {
    literal: string;
    key: "ENTER" | "F7" | "F8";
    status: number;
    listPage?: Partial<UserListResponse>;
}

// Each literal on the path services/users/cousr00c.py answers it: a 200 page carrying it, or
// a refusal whose body carries it.
const LITERAL_CASES: readonly LiteralCase[] = [
    // PROCESS-ENTER-KEY WHEN OTHER: an edit failure whose 400 body carries the re-browsed page.
    {
        literal: INVALID_SELECTION,
        key: "ENTER",
        status: HTTP_BAD_REQUEST,
        listPage: REBROWSED_PAGE,
    },
    // READNEXT ENDFILE on the last page.
    { literal: REACHED_BOTTOM, key: "ENTER", status: HTTP_OK },
    { literal: REACHED_BOTTOM, key: "F8", status: HTTP_OK },
    // ENTER's STARTBR NOTFND: page 1 with the shown rows read again and a blank last key.
    { literal: AT_TOP_OF_PAGE, key: "ENTER", status: HTTP_OK, listPage: NOT_FOUND_PAGE },
    { literal: ALREADY_AT_BOTTOM, key: "F8", status: HTTP_BAD_REQUEST },
    { literal: ALREADY_AT_TOP, key: "F7", status: HTTP_BAD_REQUEST },
    { literal: AT_TOP_OF_PAGE, key: "F7", status: HTTP_BAD_REQUEST },
    { literal: REACHED_TOP, key: "F7", status: HTTP_BAD_REQUEST },
    { literal: REACHED_TOP, key: "F7", status: HTTP_OK },
    { literal: UNABLE_TO_LOOKUP_USER, key: "ENTER", status: HTTP_SERVICE_UNAVAILABLE },
    { literal: UNABLE_TO_LOOKUP_USER, key: "F8", status: HTTP_SERVICE_UNAVAILABLE },
];

function OutcomeOf({ literal, status, listPage }: LiteralCase): Promise<UserListResponse> {
    if (status === HTTP_OK) {
        return Promise.resolve(BuildListResponse({ ...listPage, message: literal }));
    }
    const body = BuildErrorBody({
        ...listPage,
        message: literal,
        field_errors: FieldErrorsFor(status),
    });
    return Promise.reject(BuildAxiosError(status, body));
}

// The outcome is built when the page calls, so no rejected promise waits unhandled.
function ArrangeOutcome(literalCase: LiteralCase): void {
    const answer = (): Promise<UserListResponse> => OutcomeOf(literalCase);
    if (literalCase.key === "ENTER") {
        mockSelectUsers.mockImplementationOnce(answer);
        return;
    }
    mockListUsers.mockImplementationOnce(answer);
}

it.each(LITERAL_CASES)(
    "COUSR00C $key answered $status shows $literal on the red ERRMSG line",
    async (literalCase) => {
        const user = await RenderLoadedScreen();
        ArrangeOutcome(literalCase);
        await TriggerKey(user, literalCase.key, "button");
        await ExpectAlert(literalCase.literal, ERRMSG_SEVERITY);
    },
);

it("COUSR00C F8 answered 500 shows the abend text and keeps the rows", async () => {
    const user = await RenderLoadedScreen();
    const failure = BuildAxiosError(HTTP_INTERNAL_SERVER_ERROR, "Internal Server Error");
    mockListUsers.mockRejectedValueOnce(failure);
    await TriggerKey(user, "F8", "button");
    await ExpectAlert(ABEND_MESSAGE, ERRMSG_SEVERITY);
    expect(BodyRowTexts()).toEqual(RowTextsOf(FIRST_PAGE_ROWS));
});

// Both ENTER outcomes start from page 2, so the page-1 reset of every ENTER search shows.
async function RenderSecondPage(): Promise<UserEvent> {
    mockListUsers.mockResolvedValueOnce(BuildListResponse({ page: SHOWN_PAGE }));
    return RenderLoadedScreen();
}

it("COUSR00C ENTER STARTBR NOTFND shows page 1 with rows read again and refuses F8", async () => {
    const user = await RenderSecondPage();
    const notFound = BuildListResponse({ ...NOT_FOUND_PAGE, message: AT_TOP_OF_PAGE });
    mockSelectUsers.mockResolvedValueOnce(notFound);
    await user.type(SearchField(), SEARCH_TEXT);
    await PressKey(user, "ENTER");
    await ExpectAlert(AT_TOP_OF_PAGE, ERRMSG_SEVERITY);
    expect(screen.getByText("Page: 1")).toBeVisible();
    expect(SearchField()).toHaveValue("");
    expect(BodyRowTexts()).toEqual(RowTextsOf(FIRST_PAGE_ROWS));
    const refusal = BuildErrorBody({ message: ALREADY_AT_BOTTOM });
    mockListUsers.mockRejectedValueOnce(BuildAxiosError(HTTP_BAD_REQUEST, refusal));
    await PressKey(user, "F8");
    const expectedQuery: UserListQuery = { startKey: "", direction: Direction.FORWARD, page: 1 };
    expect(mockListUsers).toHaveBeenLastCalledWith(expectedQuery, READ_OPTIONS);
    await ExpectAlert(ALREADY_AT_BOTTOM, ERRMSG_SEVERITY);
});

// SEL0003 shows TESTUSR2 on the first page and TESTUSRC on the other page.
const KEPT_CODE_ROW = 3;
// F8 from the last user ID of the re-browsed page an invalid-selection 400 body carries.
const REBROWSED_F8_QUERY: UserListQuery = {
    startKey: OTHER_PAGE_LAST_KEY,
    direction: Direction.FORWARD,
    page: 1,
};

// The server alone validates codes, so its invalid-selection 400 is mocked whatever is chosen.
it("COUSR00C ENTER invalid-selection 400 shows its body's page and keeps the codes", async () => {
    const user = await RenderSecondPage();
    const body = BuildErrorBody({ ...REBROWSED_PAGE, message: INVALID_SELECTION });
    mockSelectUsers.mockRejectedValueOnce(BuildAxiosError(HTTP_BAD_REQUEST, body));
    await ChooseSelection(user, KEPT_CODE_ROW, "D");
    await user.type(SearchField(), SEARCH_TEXT);
    await PressKey(user, "ENTER");
    await ExpectAlert(INVALID_SELECTION, ERRMSG_SEVERITY);
    expect(BodyRowTexts()).toEqual(RowTextsOf(OTHER_PAGE_ROWS));
    expect(screen.getByText("Page: 1")).toBeVisible();
    expect(SearchField()).toHaveValue("");
    expect(SearchField()).toHaveAttribute("aria-invalid", "false");
    expect(SearchField()).toHaveFocus();
    expect(ShownSelectionCodes()).toEqual(["", "", "D"]);
    await PressKey(user, "F8");
    expect(mockListUsers).toHaveBeenLastCalledWith(REBROWSED_F8_QUERY, READ_OPTIONS);
});

it("COUSR00C keeps a code at its row across F8, sent beside the user shown there", async () => {
    const user = await RenderLoadedScreen();
    await ChooseSelection(user, KEPT_CODE_ROW, "U");
    mockListUsers.mockResolvedValueOnce(BuildListResponse({ rows: OTHER_PAGE_ROWS, page: 2 }));
    await PressKey(user, "F8");
    await ExpectKeysReady();
    expect(BodyRowTexts()).toEqual(RowTextsOf(OTHER_PAGE_ROWS));
    expect(ShownSelectionCodes()).toEqual(["", "", "U"]);
    const UPDATE_USER_ROUTE = "/admin/users/update?userId=TESTUSRC";
    mockSelectUsers.mockResolvedValueOnce({ route: UPDATE_USER_ROUTE });
    await TriggerKey(user, "ENTER", "button");
    const expectedRequest: UserSelectionRequest = {
        selections: ExpectedSelections(OTHER_PAGE_ROWS, { rowNumber: KEPT_CODE_ROW, code: "U" }),
        search_user_id: "",
    };
    expect(mockSelectUsers).toHaveBeenCalledWith(expectedRequest);
    expect(mockRouterPush).toHaveBeenCalledWith(UPDATE_USER_ROUTE);
});

// A refusal or a file error re-sends the screen with every SELnnnn field as it was typed.
const CODE_KEEPING_CASES: readonly LiteralCase[] = [
    { literal: ALREADY_AT_BOTTOM, key: "F8", status: HTTP_BAD_REQUEST },
    { literal: UNABLE_TO_LOOKUP_USER, key: "F8", status: HTTP_SERVICE_UNAVAILABLE },
    { literal: UNABLE_TO_LOOKUP_USER, key: "ENTER", status: HTTP_SERVICE_UNAVAILABLE },
];

it.each(CODE_KEEPING_CASES)(
    "COUSR00C $key answered $status keeps the chosen code at its row for the next ENTER",
    async (literalCase) => {
        const user = await RenderLoadedScreen();
        const chosen: ChosenCode = { rowNumber: KEPT_CODE_ROW, code: "D" };
        await ChooseSelection(user, chosen.rowNumber, chosen.code);
        ArrangeOutcome(literalCase);
        await TriggerKey(user, literalCase.key, "button");
        await ExpectAlert(literalCase.literal, ERRMSG_SEVERITY);
        const selections = ExpectedSelections(FIRST_PAGE_ROWS, chosen);
        expect(ShownSelectionCodes()).toEqual(selections.map((row) => row.selection));
        mockSelectUsers.mockResolvedValueOnce({ route: "/admin/users/delete?userId=TESTUSR2" });
        await TriggerKey(user, "ENTER", "button");
        expect(mockSelectUsers).toHaveBeenLastCalledWith({ selections, search_user_id: "" });
    },
);

interface PageStatusCase {
    rowsShown: string;
    firstPage: UserListResponse;
    statusText: string;
}

// The first entry's page: ten users, a single user, or an empty table's STARTBR NOTFND page 1.
const PAGE_STATUS_CASES: readonly PageStatusCase[] = [
    {
        rowsShown: "ten rows",
        firstPage: BuildListResponse(),
        statusText: "Page: 1, 10 users shown, TESTUSR0 to TESTUSR9",
    },
    {
        rowsShown: "one row",
        firstPage: BuildListResponse({ rows: OTHER_PAGE_ROWS.slice(0, 1) }),
        statusText: "Page: 1, 1 user shown, TESTUSRA to TESTUSRA",
    },
    {
        rowsShown: "no rows",
        firstPage: BuildListResponse({ rows: [], first_key: "", last_key: "" }),
        statusText: "Page: 1, 0 users shown",
    },
];

it.each(PAGE_STATUS_CASES)(
    "COUSR00C first entry with $rowsShown announces '$statusText' in a hidden polite status",
    async ({ firstPage, statusText }) => {
        mockListUsers.mockResolvedValueOnce(firstPage);
        await RenderLoadedScreen();
        const pageStatus = PageStatusRegion();
        expect(pageStatus.textContent).toBe(statusText);
        expect(pageStatus).toHaveAttribute("aria-live", "polite");
        expect(pageStatus).toHaveAttribute("aria-atomic", "true");
        expect(pageStatus).toHaveStyle({ position: "absolute", width: "1px", overflow: "hidden" });
        expect(screen.getByText("Page: 1")).toBeVisible();
        expect(screen.queryByRole("alert")).toBeNull();
    },
);

it("COUSR00C F8 changes the page status and leaves the ERRMSG line to the server", async () => {
    const user = await RenderLoadedScreen();
    const nextPage = BuildListResponse({ rows: OTHER_PAGE_ROWS, page: 2, message: REACHED_BOTTOM });
    mockListUsers.mockResolvedValueOnce(nextPage);
    await PressKey(user, "F8");
    await ExpectAlert(REACHED_BOTTOM, ERRMSG_SEVERITY);
    expect(PageStatusRegion().textContent).toBe("Page: 2, 3 users shown, TESTUSRA to TESTUSRC");
    expect(within(PageStatusRegion()).queryByRole("alert")).toBeNull();
});

// A refusal that sets ERR-FLG re-sends the screen as it was, the typed search key included.
const KEPT_SEARCH_CASES: readonly LiteralCase[] = [
    { literal: ALREADY_AT_TOP, key: "F7", status: HTTP_BAD_REQUEST },
    { literal: UNABLE_TO_LOOKUP_USER, key: "ENTER", status: HTTP_SERVICE_UNAVAILABLE },
];

it.each(KEPT_SEARCH_CASES)(
    "COUSR00C $key refused with $literal keeps the rows and the typed search key",
    async (literalCase) => {
        const user = await RenderLoadedScreen();
        ArrangeOutcome(literalCase);
        await user.type(SearchField(), SEARCH_TEXT);
        await TriggerKey(user, literalCase.key, "button");
        await ExpectAlert(literalCase.literal, ERRMSG_SEVERITY);
        expect(SearchField()).toHaveValue(SEARCH_TEXT);
        expect(BodyRowTexts()).toEqual(RowTextsOf(FIRST_PAGE_ROWS));
        expect(screen.getByText("Page: 1")).toBeVisible();
    },
);

it("COUSR00C F8 refusal puts the cursor on USRIDIN without marking it in error", async () => {
    const user = await RenderLoadedScreen();
    expect(SearchField()).not.toHaveFocus();
    expect(SearchField()).toHaveAttribute("aria-invalid", "false");
    const body = BuildErrorBody({ message: ALREADY_AT_BOTTOM });
    mockListUsers.mockRejectedValueOnce(BuildAxiosError(HTTP_BAD_REQUEST, body));
    await PressKey(user, "F8");
    await ExpectAlert(ALREADY_AT_BOTTOM, ERRMSG_SEVERITY);
    expect(SearchField()).toHaveAttribute("aria-invalid", "false");
    expect(SearchField()).not.toHaveAccessibleDescription(ALREADY_AT_BOTTOM);
    expect(SearchField()).toHaveFocus();
    expect(BodyRowTexts()).toEqual(RowTextsOf(FIRST_PAGE_ROWS));
});

it.each(TRIGGERS)(
    "COUSR00C F3 by %s returns to the admin menu (XCTL COADM01C)",
    async (trigger) => {
        const user = await RenderLoadedScreen();
        await TriggerKey(user, "F3", trigger);
        expect(mockRouterPush).toHaveBeenCalledTimes(1);
        expect(mockRouterPush).toHaveBeenCalledWith(ADMIN_MENU_ROUTE);
        expect(mockListUsers).toHaveBeenCalledTimes(1);
        expect(mockSelectUsers).not.toHaveBeenCalled();
    },
);

// COUSR00C WHEN OTHER (lines 132-135): an AID outside ENTER, PF3, PF7 and PF8 sets ERR-FLG, moves
// -1 to USRIDINL and sends CCDA-MSG-INVALID-KEY (cpy/CSMSG01Y.cpy) over the map as it stood; no
// file is browsed.
const INVALID_KEY_MESSAGE = "Invalid key pressed. Please see below...";
const UNBOUND_KEYS: readonly string[] = ["F4", "F5", "F12"];

// A PF key the legend lacks, pressed where the cursor is; the event tells whether its browser
// default was blocked.
async function PressUnboundKey(keyName: string): Promise<KeyboardEvent> {
    const keyInit = { key: keyName, bubbles: true, cancelable: true };
    const keyEvent = new KeyboardEvent("keydown", keyInit);
    await act(async () => {
        (document.activeElement ?? document.body).dispatchEvent(keyEvent);
    });
    return keyEvent;
}

// Types the search key, then picks U on row 2, which leaves the cursor in SEL0002.
async function KeyListInput(user: UserEvent): Promise<void> {
    await user.type(SearchField(), SEARCH_TEXT);
    await ChooseSelection(user, SELECTED_ROW_NUMBER, "U");
}

// The next ENTER sends the U code beside its row's user and the search key, so the invalid key
// left both as they were.
async function ExpectKeyedInputSentByEnter(user: UserEvent): Promise<void> {
    mockSelectUsers.mockResolvedValueOnce({ route: `/admin/users/update?userId=${TEST_USER_ID}` });
    await PressKey(user, "ENTER");
    const chosen = { rowNumber: SELECTED_ROW_NUMBER, code: "U" };
    const selections = ExpectedSelections(FIRST_PAGE_ROWS, chosen);
    expect(mockSelectUsers).toHaveBeenCalledWith({ selections, search_user_id: SEARCH_TEXT });
}

it.each(UNBOUND_KEYS)(
    "COUSR00C %s shows the invalid-key literal in red, browses nothing and keeps the map",
    async (keyName) => {
        const user = await RenderLoadedScreen();
        await KeyListInput(user);
        const keyEvent = await PressUnboundKey(keyName);
        await ExpectAlert(INVALID_KEY_MESSAGE, ERRMSG_SEVERITY);
        expect(keyEvent.defaultPrevented).toBe(true);
        await waitFor(() => expect(SearchField()).toHaveFocus());
        expect(SearchField()).toHaveAttribute("aria-invalid", "false");
        expect(mockListUsers).toHaveBeenCalledTimes(1);
        expect(mockSelectUsers).not.toHaveBeenCalled();
        expect(mockRouterPush).not.toHaveBeenCalled();
        expect(BodyRowTexts()).toEqual(RowTextsOf(FIRST_PAGE_ROWS));
        expect(SearchField()).toHaveValue(SEARCH_TEXT);
        expect(screen.getByText("Page: 1")).toBeVisible();
        await ExpectKeyedInputSentByEnter(user);
    },
);

// The keys wait while a browse runs, the invalid-key action with them, so the browse's answer is
// what the screen shows.
it.each(UNBOUND_KEYS)("COUSR00C %s while an F8 browse runs answers nothing", async (keyName) => {
    const user = await RenderLoadedScreen();
    const answerBrowse = HoldNextBrowse();
    await PressKey(user, "F8");
    expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "true");
    const keyEvent = await PressUnboundKey(keyName);
    expect(keyEvent.defaultPrevented).toBe(true);
    expect(screen.queryByRole("alert")).toBeNull();
    await answerBrowse(BuildListResponse({ rows: OTHER_PAGE_ROWS, page: 2 }));
    await ExpectKeysReady();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(BodyRowTexts()).toEqual(RowTextsOf(OTHER_PAGE_ROWS));
    expect(mockListUsers).toHaveBeenCalledTimes(2);
});

// A second ENTER, the bound F3 or F8, or a PF key outside the legend, pressed in ENTER's turn.
const SAME_TURN_SECOND_KEYS: readonly PfKey[] = ["ENTER", "F3", "F8", "F12"];
// The delete route the server answers for D on TESTUSR1 (XCTL COUSR03C).
const DELETE_ROUTE = `/admin/users/delete?userId=${TEST_USER_ID}`;
// F8 from the last key of the loaded page 1.
const FIRST_FORWARD_QUERY: UserListQuery = {
    startKey: FIRST_PAGE_LAST_KEY,
    direction: Direction.FORWARD,
    page: 1,
};
// F8 again, from the last key of the page 2 the first F8 brought.
const NEXT_FORWARD_QUERY: UserListQuery = {
    startKey: OTHER_PAGE_LAST_KEY,
    direction: Direction.FORWARD,
    page: 2,
};

// Keys dispatched on window inside one act, so no render, and no effect handing the keys their
// disabled state, runs between two presses.
async function PressInOneTurn(keys: readonly PfKey[]): Promise<void> {
    await act(async () => {
        for (const key of keys) {
            const init = { key: PF_KEY_ACTIONS[key], bubbles: true, cancelable: true };
            window.dispatchEvent(new KeyboardEvent("keydown", init));
        }
    });
}

// An ENTER selection held in flight until the test answers it, inside act, through the returned
// function.
function HoldSelection(): (response: UserSelectionResponse) => Promise<void> {
    let answerSelection: (response: UserSelectionResponse) => void = () => undefined;
    mockSelectUsers.mockImplementation(
        () =>
            new Promise((resolve) => {
                answerSelection = resolve;
            }),
    );
    return async (response) => {
        await act(async () => {
            answerSelection(response);
        });
    };
}

// The keys disable a render after ENTER, so a key pressed in that gap meets the in-flight lock:
// one selection is sent, no browse or menu return starts, the selection's route is followed, and
// the lock then clears.
it.each(SAME_TURN_SECOND_KEYS)(
    "COUSR00C ENTER and %s in one turn send one selection, answered as normal",
    async (secondKey) => {
        const answerSelection = HoldSelection();
        const user = await RenderLoadedScreen();
        await ChooseSelection(user, SELECTED_ROW_NUMBER, "D");
        await PressInOneTurn(["ENTER", secondKey]);
        const chosen = { rowNumber: SELECTED_ROW_NUMBER, code: "D" };
        const selections = ExpectedSelections(FIRST_PAGE_ROWS, chosen);
        expect(mockSelectUsers).toHaveBeenCalledTimes(1);
        expect(mockSelectUsers).toHaveBeenCalledWith({ selections, search_user_id: "" });
        expect(mockListUsers).toHaveBeenCalledTimes(1);
        expect(mockRouterPush).not.toHaveBeenCalled();
        expect(screen.queryByRole("alert")).toBeNull();
        await answerSelection({ route: DELETE_ROUTE });
        await ExpectKeysReady();
        expect(mockRouterPush).toHaveBeenCalledTimes(1);
        expect(mockRouterPush).toHaveBeenCalledWith(DELETE_ROUTE);
        await PressInOneTurn(["ENTER"]);
        expect(mockSelectUsers).toHaveBeenCalledTimes(2);
        expect(mockSelectUsers).toHaveBeenLastCalledWith({ selections, search_user_id: "" });
    },
);

// A second F8 in the same turn meets the in-flight lock instead of superseding the first: one
// browse is sent and never aborted, its page is shown, and the lock then clears.
it("COUSR00C F8 twice in one turn sends one browse, answered as normal", async () => {
    await RenderLoadedScreen();
    const answerBrowse = HoldNextBrowse();
    await PressInOneTurn(["F8", "F8"]);
    expect(mockListUsers).toHaveBeenCalledTimes(2);
    expect(mockListUsers).toHaveBeenLastCalledWith(FIRST_FORWARD_QUERY, READ_OPTIONS);
    expect(ReadSignal(1)?.aborted).toBe(false);
    await answerBrowse(BuildListResponse({ rows: OTHER_PAGE_ROWS, page: 2 }));
    await ExpectKeysReady();
    expect(BodyRowTexts()).toEqual(RowTextsOf(OTHER_PAGE_ROWS));
    expect(screen.getByText("Page: 2")).toBeVisible();
    await PressInOneTurn(["F8"]);
    expect(mockListUsers).toHaveBeenCalledTimes(3);
    expect(mockListUsers).toHaveBeenLastCalledWith(NEXT_FORWARD_QUERY, READ_OPTIONS);
});

// The session refusal has a blank message; the apiClient interceptor, not the page, sends the
// browser to sign-on, as EIBCALEN = 0 returned to COSGN00C.
it("COUSR00C 401 on the first browse shows no message; apiClient owns /signon", async () => {
    const body = BuildErrorBody({ field_errors: [], cursor_field: "" });
    mockListUsers.mockRejectedValueOnce(BuildAxiosError(HTTP_UNAUTHORIZED, body));
    RenderScreen();
    await ExpectKeysReady();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(mockRouterPush).not.toHaveBeenCalled();
    expect(mockRouterReplace).not.toHaveBeenCalled();
    expect(SIGN_ON_ROUTE).toBe("/signon");
});

interface CarriedMessageCase {
    responseMessage: string;
    shownMessage: string;
}

const CARRIED_MESSAGE_CASES: readonly CarriedMessageCase[] = [
    { responseMessage: "", shownMessage: CARRIED_MESSAGE },
    { responseMessage: REACHED_BOTTOM, shownMessage: REACHED_BOTTOM },
];

it.each(CARRIED_MESSAGE_CASES)(
    "COUSR00C first entry answered '$responseMessage' shows '$shownMessage'",
    async ({ responseMessage, shownMessage }) => {
        window.sessionStorage.setItem(PENDING_MESSAGE_KEY, CARRIED_MESSAGE);
        mockListUsers.mockResolvedValueOnce(BuildListResponse({ message: responseMessage }));
        await RenderLoadedScreen();
        await ExpectAlert(shownMessage, ERRMSG_SEVERITY);
        expect(window.sessionStorage.getItem(PENDING_MESSAGE_KEY)).toBeNull();
    },
);

type ListRead = "first entry" | "F7" | "F8";

// A browse the server has not answered. Only its signal settles it, rejecting with the
// CanceledError axios raises for an aborted call.
function PendingUntilAborted(
    _query: UserListQuery,
    options?: ReadOptions,
): Promise<UserListResponse> {
    return new Promise((_resolve, reject) => {
        options?.signal?.addEventListener("abort", () => reject(new CanceledError()));
    });
}

// The first entry's browse, or an F7 or F8 browse from the loaded page 1 with the search key
// typed, answered by the given call.
async function StartListRead(read: ListRead, answer: typeof ListUsers): Promise<void> {
    if (read === "first entry") {
        mockListUsers.mockImplementationOnce(answer);
        RenderScreen();
        return;
    }
    const user = await RenderLoadedScreen();
    mockListUsers.mockImplementationOnce(answer);
    await user.type(SearchField(), SEARCH_TEXT);
    await PressKey(user, read);
}

function ReadSignal(callIndex: number): AbortSignal | undefined {
    return mockListUsers.mock.calls[callIndex]?.[1]?.signal;
}

// A macrotask boundary, so every promise reaction an abort or answer queued has run.
async function SettlePendingWork(): Promise<void> {
    await act(
        () =>
            new Promise<void>((resolve) => {
                setTimeout(resolve, 0);
            }),
    );
}

interface PendingReadCase {
    read: ListRead;
    callCount: number;
}

const PENDING_READ_CASES: readonly PendingReadCase[] = [
    { read: "first entry", callCount: 1 },
    { read: "F7", callCount: 2 },
    { read: "F8", callCount: 2 },
];

it.each(PENDING_READ_CASES)(
    "COUSR00C leaving the screen during the $read browse aborts it and sends nothing more",
    async ({ read, callCount }) => {
        await StartListRead(read, PendingUntilAborted);
        expect(mockListUsers).toHaveBeenCalledTimes(callCount);
        const signal = ReadSignal(callCount - 1);
        expect(signal?.aborted).toBe(false);
        expect(screen.queryByRole("alert")).toBeNull();
        cleanup();
        expect(signal?.aborted).toBe(true);
        await SettlePendingWork();
        expect(mockListUsers).toHaveBeenCalledTimes(callCount);
        expect(mockSelectUsers).not.toHaveBeenCalled();
    },
);

// React StrictMode runs the first-entry effect, its cleanup and the effect again, as next dev
// does: the second browse supersedes the first, whose cancellation changes nothing.
it("COUSR00C StrictMode's second first-entry browse alone answers the screen", async () => {
    let answerSecondBrowse: (response: UserListResponse) => void = () => undefined;
    mockListUsers.mockImplementationOnce(PendingUntilAborted).mockReturnValueOnce(
        new Promise((resolve) => {
            answerSecondBrowse = resolve;
        }),
    );
    RenderScreen({ reactStrictMode: true });
    expect(mockListUsers).toHaveBeenCalledTimes(2);
    expect(ReadSignal(0)?.aborted).toBe(true);
    expect(ReadSignal(1)?.aborted).toBe(false);
    await SettlePendingWork();
    expect(screen.queryByRole("group", { name: "Function keys" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    answerSecondBrowse(BuildListResponse({ rows: OTHER_PAGE_ROWS, message: REACHED_BOTTOM }));
    await ExpectKeysReady();
    expect(BodyRowTexts()).toEqual(RowTextsOf(OTHER_PAGE_ROWS));
    await ExpectAlert(REACHED_BOTTOM, ERRMSG_SEVERITY);
});

interface CancelledReadCase extends PendingReadCase {
    keptRows: readonly UserListRow[];
    keptSearch: string;
    keptPageLine: string;
}

// No answer came, so the screen keeps what it showed: nothing yet on the first entry, and the
// page 1 rows, the typed search key and the page number on F7 or F8.
const CANCELLED_READ_CASES: readonly CancelledReadCase[] = [
    { read: "first entry", callCount: 1, keptRows: [], keptSearch: "", keptPageLine: "Page:" },
    {
        read: "F7",
        callCount: 2,
        keptRows: FIRST_PAGE_ROWS,
        keptSearch: SEARCH_TEXT,
        keptPageLine: "Page: 1",
    },
    {
        read: "F8",
        callCount: 2,
        keptRows: FIRST_PAGE_ROWS,
        keptSearch: SEARCH_TEXT,
        keptPageLine: "Page: 1",
    },
];

it.each(CANCELLED_READ_CASES)(
    "COUSR00C $read browse rejected as cancelled shows no message and changes no field",
    async ({ read, callCount, keptRows, keptSearch, keptPageLine }) => {
        await StartListRead(read, () => Promise.reject(new CanceledError()));
        await ExpectKeysReady();
        expect(mockListUsers).toHaveBeenCalledTimes(callCount);
        expect(screen.queryByRole("alert")).toBeNull();
        expect(BodyRowTexts()).toEqual(RowTextsOf(keptRows));
        expect(SearchField()).toHaveValue(keptSearch);
        expect(SearchField()).toHaveAttribute("aria-invalid", "false");
        expect(screen.getByText(keptPageLine)).toBeVisible();
    },
);

// Routes cousr00c._RouteWithUserId never builds: other sites, a script, another page, the update
// or delete page without exactly one non-blank userId or with another key, and a non-string.
const UNEXPECTED_SELECTION_ROUTES: readonly unknown[] = [
    `https://evil.example/admin/users/update?userId=${TEST_USER_ID}`,
    `//evil.example/admin/users/update?userId=${TEST_USER_ID}`,
    "javascript:alert(1)",
    `/admin/users/add?userId=${TEST_USER_ID}`,
    "/admin/users/update",
    "/admin/users/update?userId=",
    "/admin/users/update?userId=+",
    `/admin/users/update?userId=${TEST_USER_ID}&userId=TESTUSR2`,
    `/admin/users/delete?userId=${TEST_USER_ID}&next=//evil.example`,
    `/admin/users/delete?userId=${TEST_USER_ID}#top`,
    `/admin/users/update/?userId=${TEST_USER_ID}`,
    42,
];

// A 200 selection answer carrying a route its TypeScript type rules out, as unchecked JSON can.
function SelectionAnswering(route: unknown): UserSelectionResponse {
    const answer = { route };
    return answer as UserSelectionResponse;
}

it.each(UNEXPECTED_SELECTION_ROUTES)(
    "COUSR00C ENTER answered with route %p shows the abend text and never navigates",
    async (route) => {
        const user = await RenderLoadedScreen();
        mockSelectUsers.mockResolvedValueOnce(SelectionAnswering(route));
        await ChooseSelection(user, SELECTED_ROW_NUMBER, "U");
        await user.click(SearchField());
        await TriggerKey(user, "ENTER", "button");
        await ExpectAlert(ABEND_MESSAGE, ERRMSG_SEVERITY);
        await ExpectKeysReady();
        expect(mockRouterPush).not.toHaveBeenCalled();
        expect(SearchField()).toHaveFocus();
        expect(BodyRowTexts()).toEqual(RowTextsOf(FIRST_PAGE_ROWS));
    },
);

// COUSR00C's urlencode writes a blank as "+"; another spelling of the same user ID is rebuilt
// with URLSearchParams before it reaches the router, so the delete page reads the same userId.
it("COUSR00C ENTER opens the selection route re-encoded, not as the server wrote it", async () => {
    const user = await RenderLoadedScreen();
    mockSelectUsers.mockResolvedValueOnce({ route: "/admin/users/delete?userId=TEST%20U%26" });
    await ChooseSelection(user, SELECTED_ROW_NUMBER, "D");
    await user.click(SearchField());
    await PressKey(user, "ENTER");
    await waitFor(() => {
        expect(mockRouterPush).toHaveBeenCalledWith("/admin/users/delete?userId=TEST+U%26");
    });
    expect(mockRouterPush).toHaveBeenCalledTimes(1);
});

// An F7 or F8 browse held in flight until the test answers it, inside act, through the returned
// function.
function HoldNextBrowse(): (response: UserListResponse) => Promise<void> {
    let answerBrowse: (response: UserListResponse) => void = () => undefined;
    mockListUsers.mockReturnValueOnce(
        new Promise((resolve) => {
            answerBrowse = resolve;
        }),
    );
    return async (response) => {
        await act(async () => {
            answerBrowse(response);
        });
    };
}

function ExpectSelectionCodesReadOnly(isReadOnly: boolean): void {
    for (const field of screen.getAllByRole("combobox")) {
        expect(field.getAttribute("aria-readonly") === "true").toBe(isReadOnly);
    }
}

// The 3270 keyboard locked from send to response: USRIDIN and every SELnnnn keep the cursor but
// refuse keystrokes until the browse answers.
it("COUSR00C F8 pressed by pointer locks USRIDIN and SEL0001-10 until it answers", async () => {
    const user = await RenderLoadedScreen();
    const answerBrowse = HoldNextBrowse();
    await user.type(SearchField(), SEARCH_TEXT);
    await TriggerKey(user, "F8", "button");
    expect(SearchField()).toHaveFocus();
    expect(SearchField()).toHaveAttribute("readonly");
    expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "true");
    ExpectSelectionCodesReadOnly(true);
    await user.keyboard("X");
    expect(SearchField()).toHaveValue(SEARCH_TEXT);
    await user.click(screen.getByRole("combobox", { name: SelectionLabelFor(1) }));
    expect(screen.queryByRole("listbox")).toBeNull();
    await answerBrowse(BuildListResponse({ rows: OTHER_PAGE_ROWS, page: 2 }));
    await ExpectKeysReady();
    expect(SearchField()).not.toHaveAttribute("readonly");
    ExpectSelectionCodesReadOnly(false);
});

// A SEL0001 menu opened before F8 is pressed from it closes as the keyboard locks, the cursor back
// on SEL0001, and the answer neither reopens it nor brings a code.
it("COUSR00C F8 from an open SEL0001 menu closes it for good and brings no code", async () => {
    const user = await RenderLoadedScreen();
    const answerBrowse = HoldNextBrowse();
    const field = screen.getByRole("combobox", { name: SelectionLabelFor(1) });
    await user.click(field);
    expect(screen.getByRole("listbox")).toBeVisible();
    await PressKey(user, "F8");
    expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "true");
    expect(field).toHaveAttribute("aria-expanded", "false");
    await waitFor(() => expect(screen.queryByRole("listbox")).toBeNull());
    expect(field).toHaveFocus();
    await answerBrowse(BuildListResponse({ rows: OTHER_PAGE_ROWS, page: 2 }));
    await ExpectKeysReady();
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(ShownSelectionCodes()).toEqual(OTHER_PAGE_ROWS.map(() => ""));
});
