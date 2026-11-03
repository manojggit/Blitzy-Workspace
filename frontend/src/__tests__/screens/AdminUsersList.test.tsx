// Screen tests: BMS COUSR00, program COUSR00C (CU00), page /admin/users.
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
type ErrorBody = LegacyErrorResponse & PageState;

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
const FIRST_KEY = "first-key-token";
const LAST_KEY = "last-key-token";

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

function BuildListResponse(overrides: Partial<UserListResponse> = {}): UserListResponse {
    return {
        rows: FIRST_PAGE_ROWS,
        page: 1,
        first_key: FIRST_KEY,
        last_key: LAST_KEY,
        message: "",
        ...overrides,
    };
}

// The legacy error body a COUSR00C refusal carries: the cursor and the error on USRIDIN.
function BuildErrorBody(overrides: Partial<ErrorBody> = {}): ErrorBody {
    return {
        message: "",
        info_message: "",
        field_errors: [SEARCH_FIELD],
        cursor_field: SEARCH_FIELD,
        ...overrides,
    };
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

// Every shown row in display order with its code: blank for every row but the chosen one.
function ExpectedSelections(chosen?: Required<UserSelection>): UserSelection[] {
    return FIRST_PAGE_ROWS.map((row) => ({
        user_id: row.user_id,
        selection: row.user_id === chosen?.user_id ? chosen.selection : "",
    }));
}

function RenderScreen(): UserEvent {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <AdminUsersListPage />
        </ThemeProvider>,
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
    expect(mockListUsers).toHaveBeenCalledWith({});
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

it("COUSR00C locks every key until the first browse answers", async () => {
    let answerFirstBrowse: (response: UserListResponse) => void = () => undefined;
    mockListUsers.mockReturnValueOnce(
        new Promise((resolve) => {
            answerFirstBrowse = resolve;
        }),
    );
    const user = RenderScreen();
    for (const buttonName of Object.values(KEY_BUTTON_NAMES)) {
        expect(screen.getByRole("button", { name: buttonName })).toBeDisabled();
    }
    expect(screen.getByRole("progressbar", { name: "Processing" })).toBeInTheDocument();
    await PressKey(user, "F3");
    expect(mockRouterPush).not.toHaveBeenCalled();
    answerFirstBrowse(BuildListResponse());
    await ExpectKeysReady();
    expect(screen.queryByRole("progressbar")).toBeNull();
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
            selections: ExpectedSelections({ user_id: TEST_USER_ID, selection: code }),
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
        selections: ExpectedSelections(),
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
}

// Both keys page from the shown page 2. F8's PROCESS-PAGE-FORWARD runs to its end and blanks
// USRIDIN; F7's PROCESS-PAGE-BACKWARD leaves the typed search key.
const SHOWN_PAGE = 2;
const PAGE_DIRECTIONS: readonly Omit<PageCase, "trigger">[] = [
    { key: "F8", startKey: LAST_KEY, direction: Direction.FORWARD, resultPage: 3, searchAfter: "" },
    {
        key: "F7",
        startKey: FIRST_KEY,
        direction: Direction.BACKWARD,
        resultPage: 1,
        searchAfter: SEARCH_TEXT,
    },
];
const PAGE_CASES: readonly PageCase[] = PAGE_DIRECTIONS.flatMap((pageCase) =>
    TRIGGERS.map((trigger) => ({ ...pageCase, trigger })),
);

it.each(PAGE_CASES)(
    "COUSR00C $key by $trigger browses $direction from $startKey",
    async ({ key, trigger, startKey, direction, resultPage, searchAfter }) => {
        mockListUsers.mockResolvedValueOnce(BuildListResponse({ page: SHOWN_PAGE }));
        const user = await RenderLoadedScreen();
        const nextPage = BuildListResponse({ rows: OTHER_PAGE_ROWS, page: resultPage });
        mockListUsers.mockResolvedValueOnce(nextPage);
        await user.type(SearchField(), SEARCH_TEXT);
        await TriggerKey(user, key, trigger);
        const expectedQuery: UserListQuery = { startKey, direction, page: SHOWN_PAGE };
        expect(mockListUsers).toHaveBeenLastCalledWith(expectedQuery);
        expect(BodyRowTexts()).toEqual(RowTextsOf(OTHER_PAGE_ROWS));
        expect(screen.getByText(`Page: ${resultPage}`)).toBeVisible();
        expect(SearchField()).toHaveValue(searchAfter);
    },
);

interface LiteralCase {
    literal: string;
    key: "ENTER" | "F7" | "F8";
    status: number;
    page?: number;
}

// Each literal on the path services/users/cousr00c.py answers it: a 200 page carrying it, or
// a refusal whose body carries it.
const LITERAL_CASES: readonly LiteralCase[] = [
    // PROCESS-ENTER-KEY WHEN OTHER: the list is re-browsed and answered with the literal.
    { literal: INVALID_SELECTION, key: "ENTER", status: HTTP_OK },
    // READNEXT ENDFILE on the last page.
    { literal: REACHED_BOTTOM, key: "ENTER", status: HTTP_OK },
    { literal: REACHED_BOTTOM, key: "F8", status: HTTP_OK },
    // ENTER's STARTBR NOTFND, its body carrying page 0.
    { literal: AT_TOP_OF_PAGE, key: "ENTER", status: HTTP_BAD_REQUEST, page: 0 },
    { literal: ALREADY_AT_BOTTOM, key: "F8", status: HTTP_BAD_REQUEST },
    { literal: ALREADY_AT_TOP, key: "F7", status: HTTP_BAD_REQUEST },
    { literal: AT_TOP_OF_PAGE, key: "F7", status: HTTP_BAD_REQUEST },
    { literal: REACHED_TOP, key: "F7", status: HTTP_BAD_REQUEST },
    { literal: REACHED_TOP, key: "F7", status: HTTP_OK },
    { literal: UNABLE_TO_LOOKUP_USER, key: "ENTER", status: HTTP_SERVICE_UNAVAILABLE },
    { literal: UNABLE_TO_LOOKUP_USER, key: "F8", status: HTTP_SERVICE_UNAVAILABLE },
];

function OutcomeOf({ literal, status, page }: LiteralCase): Promise<UserListResponse> {
    if (status === HTTP_OK) {
        return Promise.resolve(BuildListResponse({ message: literal }));
    }
    const body = BuildErrorBody({ message: literal, page });
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

it("COUSR00C ENTER STARTBR NOTFND shows page 0, keeps the rows and blanks USRIDIN", async () => {
    const user = await RenderLoadedScreen();
    const body = BuildErrorBody({ message: AT_TOP_OF_PAGE, page: 0 });
    mockSelectUsers.mockRejectedValueOnce(BuildAxiosError(HTTP_BAD_REQUEST, body));
    await user.type(SearchField(), SEARCH_TEXT);
    await PressKey(user, "ENTER");
    await ExpectAlert(AT_TOP_OF_PAGE, ERRMSG_SEVERITY);
    expect(screen.getByText("Page: 0")).toBeVisible();
    expect(SearchField()).toHaveValue("");
    expect(BodyRowTexts()).toEqual(RowTextsOf(FIRST_PAGE_ROWS));
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

it("COUSR00C F8 refusal marks USRIDIN in error and puts the cursor on it", async () => {
    const user = await RenderLoadedScreen();
    expect(SearchField()).not.toHaveFocus();
    expect(SearchField()).toHaveAttribute("aria-invalid", "false");
    const body = BuildErrorBody({ message: ALREADY_AT_BOTTOM });
    mockListUsers.mockRejectedValueOnce(BuildAxiosError(HTTP_BAD_REQUEST, body));
    await PressKey(user, "F8");
    await ExpectAlert(ALREADY_AT_BOTTOM, ERRMSG_SEVERITY);
    expect(SearchField()).toHaveAttribute("aria-invalid", "true");
    expect(SearchField()).toHaveAccessibleDescription(ALREADY_AT_BOTTOM);
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
