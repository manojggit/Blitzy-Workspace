// Screen tests: BMS COMEN01, program COMEN01C (CM00), page /menu.
// Literals verbatim from backend/app/services/menu/comen01c.py, menu_options.py,
// backend/app/core/messages.py and cbl/COMEN01C.cbl; option labels from cpy/COMEN02Y.cpy.
import { Typography } from "@mui/material";
import { ThemeProvider } from "@mui/material/styles";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { AxiosError, AxiosHeaders, CanceledError, type InternalAxiosRequestConfig } from "axios";
import type * as NavigationModule from "next/navigation";
import { StrictMode, Suspense, use, useEffect, useState, type ReactNode } from "react";

import MainMenuPage from "@/app/menu/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import type * as AuthApiModule from "@/lib/api/auth";
import { Logout, type CurrentUserResponse } from "@/lib/api/auth";
import type * as MenuApiModule from "@/lib/api/menu";
import {
    FetchMainMenu,
    SelectMainMenuOption,
    type MenuOption,
    type MenuResponse,
    type MenuSelectionResponse,
} from "@/lib/api/menu";
import {
    SIGN_ON_ROUTE,
    StorePendingMessage,
    TakePendingMessage,
    type LegacyErrorResponse,
    type MessageSeverity,
    type ReadOptions,
} from "@/lib/apiClient";
import type { SessionContextValue } from "@/lib/session";

const mockRouterPush = jest.fn();
const mockRouterReplace = jest.fn();
// One router object for every render, as the App Router keeps useRouter() stable.
const mockRouter = {
    push: mockRouterPush,
    replace: mockRouterReplace,
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};

// A regular user (type U), whose own menu is this one. The ID is synthetic test data.
const MOCK_CURRENT_USER: CurrentUserResponse = {
    user_id: "TESTUSR1",
    user_type: "U",
    first_name: "Test",
    last_name: "User",
    next_route: "/menu",
};
const mockClearSession = jest.fn();
const mockSession: SessionContextValue = {
    currentUser: MOCK_CURRENT_USER,
    isLoading: false,
    RefreshSession: jest.fn(),
    ClearSession: mockClearSession,
};

jest.mock("next/navigation", () => ({
    ...jest.requireActual<typeof NavigationModule>("next/navigation"),
    useRouter: () => mockRouter,
    usePathname: () => "/menu",
}));

jest.mock("@/lib/session", () => ({
    SessionProvider: ({ children }: { children: ReactNode }) => children,
    useSession: () => mockSession,
}));

jest.mock("@/lib/api/menu", () => ({
    ...jest.requireActual<typeof MenuApiModule>("@/lib/api/menu"),
    FetchMainMenu: jest.fn(),
    SelectMainMenuOption: jest.fn(),
}));

jest.mock("@/lib/api/auth", () => ({
    ...jest.requireActual<typeof AuthApiModule>("@/lib/api/auth"),
    Logout: jest.fn(),
}));

const mockFetchMainMenu = jest.mocked(FetchMainMenu);
const mockSelectMainMenuOption = jest.mocked(SelectMainMenuOption);
const mockLogout = jest.mocked(Logout);

// COMEN01 TRNNAME, PGMNAME, the row-4 title, the OPTION prompt and its LENGTH=2.
const TRANSACTION_ID = "CM00";
const PROGRAM_NAME = "COMEN01C";
const SCREEN_TITLE = "Main Menu";
const OPTION_PROMPT = "Please select an option :";
const OPTION_FIELD_LENGTH = "2";
const OPTION_FIELD_NAME = "option";
const MENU_OPTIONS_LABEL = "Main menu options";
// The COMEN01 legend ENTER=Continue  F3=Exit, as FunctionKeyBar labels its buttons.
const CONTINUE_BUTTON_NAME = "Continue (Enter)";
const EXIT_BUTTON_NAME = "Exit (F3)";

// COMEN02Y CDEMO-MENU-OPT-NAME of the 11 active entries, in table order (option 1 first).
const ACCOUNT_VIEW_LABEL = "Account View";
const MAIN_MENU_LABELS: readonly string[] = [
    ACCOUNT_VIEW_LABEL,
    "Account Update",
    "Credit Card List",
    "Credit Card View",
    "Credit Card Update",
    "Transaction List",
    "Transaction View",
    "Transaction Add",
    "Transaction Reports",
    "Bill Payment",
    "Pending Authorization View",
];
const MAIN_MENU_OPTIONS: readonly MenuOption[] = MAIN_MENU_LABELS.map((label, index) => ({
    number: index + 1,
    label,
}));

// COMEN01C line 131; the STRING of lines 161-167 for option 11 (COPAUS0C not installed);
// line 139 (with its trailing blank); CSMSG01Y CCDA-MSG-THANK-YOU without its X(50) padding.
const INVALID_OPTION_MESSAGE = "Please enter a valid option number...";
const NOT_INSTALLED_MESSAGE = "This option Pending Authorization View is not installed...";
const ADMIN_ONLY_MESSAGE = "No access - Admin Only option... ";
const THANK_YOU_MESSAGE = "Thank you for using CardDemo application...";

// menu_options.MAIN_MENU_OPTIONS routes replacing XCTL COACTVWC and XCTL COTRN02C.
const ACCOUNT_VIEW_ANSWER: MenuSelectionResponse = { route: "/accounts/view", message: "" };
const TRANSACTION_ADD_ANSWER: MenuSelectionResponse = {
    route: "/transactions/add",
    message: "",
};
const NOT_INSTALLED_ANSWER: MenuSelectionResponse = {
    route: null,
    message: NOT_INSTALLED_MESSAGE,
};

const HTTP_EDIT_FAILURE = 400;
const HTTP_UNAUTHORIZED = 401;
const REQUEST_CONFIG: InternalAxiosRequestConfig = { headers: new AxiosHeaders() };

// The MUI Alert colour classes (alertClasses colorError, colorSuccess, colorInfo).
const SEVERITY_CLASSES: Readonly<Record<MessageSeverity, string>> = {
    error: "MuiAlert-colorError",
    success: "MuiAlert-colorSuccess",
    info: "MuiAlert-colorInfo",
};

// MUI 9 Grid classes: every Grid has MuiGrid-root, a container MuiGrid-container, and each size
// breakpoint MuiGrid-grid-<breakpoint>-<columns>. The OPTION field is full width below md and
// half width from md (size xs 12, md 6), with no other breakpoint set; sorted for comparison.
const GRID_ROOT_CLASS = "MuiGrid-root";
const GRID_CONTAINER_CLASS = "MuiGrid-container";
const GRID_SIZE_CLASS_PREFIX = "MuiGrid-grid-";
const OPTION_GRID_SIZE_CLASSES: readonly string[] = ["MuiGrid-grid-md-6", "MuiGrid-grid-xs-12"];

interface CallOrderSource {
    mock: { invocationCallOrder: number[] };
}

// One way of pressing a PF key: its FunctionKeyBar button or the physical key.
interface KeyPressCase {
    keyName: string;
    Press: (user: UserEvent) => Promise<void>;
}

// One option keyed and submitted with ENTER, and the literal the menu then shows.
interface LiteralCase {
    caseName: string;
    optionText: string;
    ArrangeAnswer: () => void;
    message: string;
    severity: MessageSeverity;
}

// One call answered 401, and how the screen reaches it.
interface UnauthorizedCase {
    callName: string;
    ArrangeAndTrigger: () => Promise<void>;
}

function BuildMenuResponse(): MenuResponse {
    return { options: [...MAIN_MENU_OPTIONS] };
}

// Every key of common.LegacyErrorResponse at its server default, then the case's values.
function BuildErrorBody(overrides: Partial<LegacyErrorResponse> = {}): LegacyErrorResponse {
    return { message: "", info_message: "", field_errors: [], cursor_field: "", ...overrides };
}

// The rejection API_CLIENT gives for a non-2xx answer carrying a legacy error body.
function BuildAxiosError(status: number, body: LegacyErrorResponse): AxiosError {
    return new AxiosError(
        `Request failed with status code ${status}`,
        AxiosError.ERR_BAD_REQUEST,
        REQUEST_CONFIG,
        null,
        { status, statusText: "", headers: {}, config: REQUEST_CONFIG, data: body },
    );
}

// The COMEN01C option edit (lines 127-134): 400, cursor on OPTION as MOVE -1 TO OPTIONL put it.
function BuildInvalidOptionError(): AxiosError {
    const body = BuildErrorBody({
        message: INVALID_OPTION_MESSAGE,
        field_errors: [OPTION_FIELD_NAME],
        cursor_field: OPTION_FIELD_NAME,
    });
    return BuildAxiosError(HTTP_EDIT_FAILURE, body);
}

// The backend's 401 session refusal carries a blank message (dependencies.SessionRefusal).
function BuildUnauthorizedError(): AxiosError {
    return BuildAxiosError(HTTP_UNAUTHORIZED, BuildErrorBody());
}

function RenderScreen(): UserEvent {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <MainMenuPage />
        </ThemeProvider>,
    );
    return user;
}

// The busy bar shows from mount until FetchMainMenu settles, and during every key's call; the
// keys and the option list are disabled while it shows.
async function WaitForIdle(): Promise<void> {
    await waitFor(() => {
        expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    });
}

async function RenderLoadedScreen(): Promise<UserEvent> {
    const user = RenderScreen();
    await WaitForIdle();
    return user;
}

function OptionInput(): HTMLElement {
    return screen.getByRole("textbox", { name: OPTION_PROMPT });
}

function MenuNavigation(): HTMLElement {
    return screen.getByRole("navigation", { name: MENU_OPTIONS_LABEL });
}

function MenuList(): HTMLElement {
    return within(MenuNavigation()).getByRole("list");
}

// The ListItemButton at one list position (1 = first), in the order the server listed them.
function OptionButton(position: number): HTMLElement {
    const optionButton = within(MenuList()).getAllByRole("button")[position - 1];
    if (optionButton === undefined) {
        throw new Error(`No option button at list position ${position}`);
    }
    return optionButton;
}

// The Grid item holding the OPTION field: its nearest Grid ancestor.
function OptionGridItem(): Element {
    const gridItem = OptionInput().closest(`.${GRID_ROOT_CLASS}`);
    if (gridItem === null) {
        throw new Error("The OPTION field sits in no Grid item");
    }
    return gridItem;
}

// The Grid size classes the item carries, MuiGrid-grid-<breakpoint>-<columns>, sorted.
function GridSizeClasses(gridItem: Element): string[] {
    return Array.from(gridItem.classList)
        .filter((className) => className.startsWith(GRID_SIZE_CLASS_PREFIX))
        .sort();
}

function KeyButton(name: string): HTMLElement {
    return screen.getByRole("button", { name });
}

// useFunctionKeys listens on window, as the 3270 AID key belonged to the whole screen. The async
// act also covers the state updates of the call the key starts, which settle in later microtasks.
async function PressKey(key: string): Promise<void> {
    await act(async () => {
        fireEvent.keyDown(window, { key });
    });
}

// Raw textContent, so a double or trailing blank of the literal has to survive to match.
function AlertsWithText(text: string): HTMLElement[] {
    return screen.queryAllByRole("alert").filter((alert) => alert.textContent === text);
}

async function ExpectAlert(text: string, severity: MessageSeverity): Promise<void> {
    await waitFor(() => {
        expect(AlertsWithText(text)).toHaveLength(1);
    });
    expect(AlertsWithText(text)[0]).toHaveClass(SEVERITY_CLASSES[severity]);
}

function SingleCallOrder(source: CallOrderSource): number {
    expect(source.mock.invocationCallOrder).toHaveLength(1);
    return source.mock.invocationCallOrder[0] ?? Number.NaN;
}

function ExpectCalledOnceInOrder(sources: readonly CallOrderSource[]): void {
    const callOrders = sources.map(SingleCallOrder);
    expect(callOrders).toEqual([...callOrders].sort((first, second) => first - second));
}

// A menu read that stays in flight until its signal is aborted, then rejects as axios does.
function RejectOnAbort(options?: ReadOptions): Promise<MenuResponse> {
    return new Promise((_resolve, reject) => {
        options?.signal?.addEventListener("abort", () => {
            reject(new CanceledError());
        });
    });
}

// A menu read held in flight until the test answers it through the returned function.
function HoldMenuRead(): (response: MenuResponse) => void {
    let answerRead: (response: MenuResponse) => void = () => undefined;
    mockFetchMainMenu.mockImplementation(
        () =>
            new Promise<MenuResponse>((resolve) => {
                answerRead = resolve;
            }),
    );
    return (response) => answerRead(response);
}

// The AbortSignal the page gave one FetchMainMenu call (0 = the first).
function MenuReadSignal(callIndex: number): AbortSignal {
    const signal = mockFetchMainMenu.mock.calls[callIndex]?.[0]?.signal;
    if (signal === undefined) {
        throw new Error(`FetchMainMenu call ${callIndex} carried no AbortSignal`);
    }
    return signal;
}

const ENTER_CASES: readonly KeyPressCase[] = [
    {
        keyName: "the Continue (Enter) button",
        Press: (user) => user.click(KeyButton(CONTINUE_BUTTON_NAME)),
    },
    { keyName: "the Enter key", Press: () => PressKey("Enter") },
];

const EXIT_CASES: readonly KeyPressCase[] = [
    { keyName: "the Exit (F3) button", Press: (user) => user.click(KeyButton(EXIT_BUTTON_NAME)) },
    { keyName: "the F3 key", Press: () => PressKey("F3") },
];

// Both literals are red. COMEN01C moves DFHRED to ERRMSGC for the not-installed literal (line
// 162) and BMS ERRMSG is COLOR=RED, so a 200 answer with a null route shows as an error, as
// the page and comen01c.SelectMainOption define it.
const LITERAL_CASES: readonly LiteralCase[] = [
    {
        caseName: "option 99 shows the invalid-option literal",
        optionText: "99",
        ArrangeAnswer: () => {
            mockSelectMainMenuOption.mockRejectedValue(BuildInvalidOptionError());
        },
        message: INVALID_OPTION_MESSAGE,
        severity: "error",
    },
    {
        caseName: "option 11 shows the not-installed literal",
        optionText: "11",
        ArrangeAnswer: () => {
            mockSelectMainMenuOption.mockResolvedValue(NOT_INSTALLED_ANSWER);
        },
        message: NOT_INSTALLED_MESSAGE,
        severity: "error",
    },
];

const UNAUTHORIZED_CASES: readonly UnauthorizedCase[] = [
    {
        callName: "FetchMainMenu",
        ArrangeAndTrigger: async () => {
            mockFetchMainMenu.mockRejectedValue(BuildUnauthorizedError());
            await RenderLoadedScreen();
            expect(mockFetchMainMenu).toHaveBeenCalledTimes(1);
        },
    },
    {
        callName: "SelectMainMenuOption",
        ArrangeAndTrigger: async () => {
            mockSelectMainMenuOption.mockRejectedValue(BuildUnauthorizedError());
            const user = await RenderLoadedScreen();
            await user.click(screen.getByText(ACCOUNT_VIEW_LABEL));
            await WaitForIdle();
            expect(mockSelectMainMenuOption).toHaveBeenCalledWith({ option: "1" });
        },
    },
];

beforeEach(() => {
    jest.resetAllMocks();
    window.sessionStorage.clear();
    window.localStorage.clear();
    mockFetchMainMenu.mockResolvedValue(BuildMenuResponse());
});

it("COMEN01C shows the CM00 header, the Main Menu title, OPTION and the legend", async () => {
    await RenderLoadedScreen();
    expect(screen.getByRole("heading", { level: 1, name: SCREEN_TITLE })).toBeInTheDocument();
    expect(screen.getByText(`Tran: ${TRANSACTION_ID}`)).toBeInTheDocument();
    expect(screen.getByText(`Prog: ${PROGRAM_NAME}`)).toBeInTheDocument();
    expect(OptionInput()).toHaveAttribute("maxlength", OPTION_FIELD_LENGTH);
    expect(OptionInput()).toHaveValue("");
    const legend = screen.getByRole("group", { name: "Function keys" });
    const legendNames = within(legend)
        .getAllByRole("button")
        .map((button) => button.textContent);
    expect(legendNames).toEqual([CONTINUE_BUTTON_NAME, EXIT_BUTTON_NAME]);
});

it("COMEN01C lists all 11 COMEN02Y options in table order", async () => {
    await RenderLoadedScreen();
    expect(mockFetchMainMenu).toHaveBeenCalledTimes(1);
    expect(within(MenuList()).getAllByRole("button")).toHaveLength(MAIN_MENU_LABELS.length);
});

it.each(MAIN_MENU_OPTIONS)("COMEN01C lists option $number $label", async ({ number, label }) => {
    await RenderLoadedScreen();
    const optionButton = OptionButton(number);
    expect(within(optionButton).getByText(label)).toBeInTheDocument();
    expect(within(optionButton).getByText(String(number))).toBeInTheDocument();
});

it("COMEN01C nests one list item per option in the named nav; OPTION is xs 12, md 6", async () => {
    await RenderLoadedScreen();
    expect(screen.queryByRole("list", { name: MENU_OPTIONS_LABEL })).toBeNull();
    const optionItems = within(MenuList()).getAllByRole("listitem");
    expect(optionItems).toHaveLength(MAIN_MENU_LABELS.length);
    optionItems.forEach((optionItem) => {
        expect(within(optionItem).getAllByRole("button")).toHaveLength(1);
    });
    const gridItem = OptionGridItem();
    expect(gridItem.parentElement).toHaveClass(GRID_CONTAINER_CLASS);
    expect(GridSizeClasses(gridItem)).toEqual(OPTION_GRID_SIZE_CLASSES);
});

it("COMEN01C option click posts option number 1 and routes to /accounts/view", async () => {
    mockSelectMainMenuOption.mockResolvedValue(ACCOUNT_VIEW_ANSWER);
    const user = await RenderLoadedScreen();
    await user.click(screen.getByText(ACCOUNT_VIEW_LABEL));
    await waitFor(() => {
        expect(mockRouterPush).toHaveBeenCalledWith(ACCOUNT_VIEW_ANSWER.route);
    });
    expect(mockSelectMainMenuOption).toHaveBeenCalledTimes(1);
    expect(mockSelectMainMenuOption).toHaveBeenCalledWith({ option: "1" });
});

it.each(ENTER_CASES)(
    "COMEN01C ENTER by $keyName sends the keyed option 8 and routes to /transactions/add",
    async ({ Press }) => {
        mockSelectMainMenuOption.mockResolvedValue(TRANSACTION_ADD_ANSWER);
        const user = await RenderLoadedScreen();
        await user.type(OptionInput(), "8");
        await Press(user);
        await waitFor(() => {
            expect(mockRouterPush).toHaveBeenCalledWith(TRANSACTION_ADD_ANSWER.route);
        });
        expect(mockSelectMainMenuOption).toHaveBeenCalledTimes(1);
        expect(mockSelectMainMenuOption).toHaveBeenCalledWith({ option: "8" });
    },
);

it.each(LITERAL_CASES)(
    "COMEN01C $caseName and stays on /menu",
    async ({ optionText, ArrangeAnswer, message, severity }) => {
        ArrangeAnswer();
        const user = await RenderLoadedScreen();
        await user.type(OptionInput(), optionText);
        await user.click(KeyButton(CONTINUE_BUTTON_NAME));
        await ExpectAlert(message, severity);
        expect(mockSelectMainMenuOption).toHaveBeenCalledWith({ option: optionText });
        expect(mockRouterPush).not.toHaveBeenCalled();
    },
);

it("COMEN01C option edit failure marks OPTION invalid and returns the cursor", async () => {
    mockSelectMainMenuOption.mockRejectedValue(BuildInvalidOptionError());
    const user = await RenderLoadedScreen();
    await user.type(OptionInput(), "0");
    expect(OptionInput()).toHaveAttribute("aria-invalid", "false");
    await user.click(KeyButton(CONTINUE_BUTTON_NAME));
    await ExpectAlert(INVALID_OPTION_MESSAGE, "error");
    expect(OptionInput()).toHaveAttribute("aria-invalid", "true");
    expect(OptionInput()).toHaveAccessibleDescription(INVALID_OPTION_MESSAGE);
    expect(OptionInput()).toHaveFocus();
});

it("COMEN01C shows the refusal another screen's 403 stored, once, as an error", async () => {
    StorePendingMessage(ADMIN_ONLY_MESSAGE);
    await RenderLoadedScreen();
    await ExpectAlert(ADMIN_ONLY_MESSAGE, "error");
    expect(TakePendingMessage()).toBe("");
});

it("COMEN01C shows a stored refusal only once its options load, in one update", async () => {
    StorePendingMessage(ADMIN_ONLY_MESSAGE);
    const answerMenuRead = HoldMenuRead();
    RenderScreen();
    expect(screen.getByRole("progressbar")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: MENU_OPTIONS_LABEL })).toBeNull();
    expect(screen.queryAllByRole("alert")).toHaveLength(0);
    await act(async () => {
        answerMenuRead(BuildMenuResponse());
    });
    await ExpectAlert(ADMIN_ONLY_MESSAGE, "error");
    expect(within(MenuList()).getAllByRole("button")).toHaveLength(MAIN_MENU_LABELS.length);
    expect(TakePendingMessage()).toBe("");
});

it.each(EXIT_CASES)(
    "COMEN01C F3 by $keyName signs off to /signon and carries the thank-you literal",
    async ({ Press }) => {
        mockLogout.mockResolvedValue({ message: THANK_YOU_MESSAGE });
        const user = await RenderLoadedScreen();
        await Press(user);
        await waitFor(() => {
            expect(mockRouterPush).toHaveBeenCalledWith(SIGN_ON_ROUTE);
        });
        ExpectCalledOnceInOrder([mockLogout, mockClearSession, mockRouterPush]);
        expect(TakePendingMessage()).toBe(THANK_YOU_MESSAGE);
        expect(mockSelectMainMenuOption).not.toHaveBeenCalled();
    },
);

// COMEN01C WHEN OTHER: MOVE 'Y' TO WS-ERR-FLG and CCDA-MSG-INVALID-KEY (cpy/CSMSG01Y.cpy), then
// SEND-MENU-SCREEN. No option is sent, OPTION keeps its keyed text and is not flagged, and the
// cursor goes back to OPTION, the IC field. The literal is core/messages.py INVALID_KEY_MESSAGE.
const INVALID_KEY_MESSAGE = "Invalid key pressed. Please see below...";
// The PF keys outside the COMEN01 legend ENTER=Continue  F3=Exit.
const UNBOUND_KEYS: readonly string[] = ["F4", "F5", "F7", "F8", "F12"];

it.each(UNBOUND_KEYS)(
    "COMEN01C %s shows the invalid-key literal in red with the cursor on OPTION",
    async (key) => {
        const user = await RenderLoadedScreen();
        await user.type(OptionInput(), "8");
        await PressKey(key);
        await ExpectAlert(INVALID_KEY_MESSAGE, "error");
        expect(OptionInput()).toHaveValue("8");
        expect(OptionInput()).toHaveFocus();
        expect(OptionInput()).toHaveAttribute("aria-invalid", "false");
        expect(mockSelectMainMenuOption).not.toHaveBeenCalled();
        expect(mockLogout).not.toHaveBeenCalled();
        expect(mockRouterPush).not.toHaveBeenCalled();
        expect(mockRouterReplace).not.toHaveBeenCalled();
    },
);

it("COMEN01C F5 again after the cursor left OPTION puts it back on OPTION", async () => {
    const user = await RenderLoadedScreen();
    await PressKey("F5");
    await ExpectAlert(INVALID_KEY_MESSAGE, "error");
    await user.tab();
    expect(OptionInput()).not.toHaveFocus();
    await PressKey("F5");
    expect(OptionInput()).toHaveFocus();
    await ExpectAlert(INVALID_KEY_MESSAGE, "error");
});

it.each(UNAUTHORIZED_CASES)(
    "COMEN01C 401 on $callName shows no message and leaves the redirect to apiClient",
    async ({ ArrangeAndTrigger }) => {
        await ArrangeAndTrigger();
        expect(screen.queryAllByRole("alert")).toHaveLength(0);
        expect(screen.queryAllByRole("status")).toHaveLength(0);
        expect(mockRouterPush).not.toHaveBeenCalled();
        expect(mockRouterReplace).not.toHaveBeenCalled();
        expect(SIGN_ON_ROUTE).toBe("/signon");
    },
);

it("COMEN01C leaving the menu while its read is pending aborts the menu GET", async () => {
    mockFetchMainMenu.mockImplementation(RejectOnAbort);
    RenderScreen();
    expect(mockFetchMainMenu).toHaveBeenCalledTimes(1);
    expect(mockFetchMainMenu).toHaveBeenCalledWith({ signal: expect.any(AbortSignal) });
    expect(MenuReadSignal(0).aborted).toBe(false);
    cleanup();
    expect(MenuReadSignal(0).aborted).toBe(true);
    await expect(mockFetchMainMenu.mock.results[0]?.value).rejects.toBeInstanceOf(CanceledError);
    expect(mockRouterPush).not.toHaveBeenCalled();
});

it("COMEN01C StrictMode re-run reloads the menu; the aborted read shows no message", async () => {
    StorePendingMessage(ADMIN_ONLY_MESSAGE);
    mockFetchMainMenu.mockImplementationOnce(RejectOnAbort).mockResolvedValue(BuildMenuResponse());
    render(
        <StrictMode>
            <ThemeProvider theme={CARD_DEMO_THEME}>
                <MainMenuPage />
            </ThemeProvider>
        </StrictMode>,
    );
    await WaitForIdle();
    expect(mockFetchMainMenu).toHaveBeenCalledTimes(2);
    expect(MenuReadSignal(0).aborted).toBe(true);
    expect(MenuReadSignal(1).aborted).toBe(false);
    expect(within(MenuList()).getAllByRole("button")).toHaveLength(MAIN_MENU_LABELS.length);
    const alertTexts = screen.queryAllByRole("alert").map((alert) => alert.textContent);
    expect(alertTexts).toEqual([ADMIN_ONLY_MESSAGE]);
});

// CSMSG02Y ABEND-MSG: the menu's answer to a route no installed option has.
const ABEND_MESSAGE = "UNEXPECTED ABEND OCCURRED.";

// The routes of options 1-10 in menu_options.MAIN_MENU_OPTIONS, in option order.
const INSTALLED_OPTION_ROUTES: readonly [number, string][] = [
    "/accounts/view",
    "/accounts/update",
    "/cards",
    "/cards/view",
    "/cards/update",
    "/transactions",
    "/transactions/view",
    "/transactions/add",
    "/reports/transactions",
    "/billpay",
].map((route, index) => [index + 1, route]);

// Routes MAIN_MENU_OPTIONS never answers: other sites, a script, an admin page, spellings of an
// installed page that are not exactly its route, and a non-string.
const UNEXPECTED_ROUTES: readonly unknown[] = [
    "https://evil.example/accounts/view",
    "//evil.example",
    "javascript:alert(1)",
    "/admin/users",
    "/accounts/view/",
    "/ACCOUNTS/VIEW",
    "/accounts/view?acctId=00000000001",
    "/accounts/view#top",
    42,
];

// A 200 selection answer carrying a route its TypeScript type rules out, as unchecked JSON can.
function SelectionAnswering(route: unknown): MenuSelectionResponse {
    const answer = { route, message: "" };
    return answer as MenuSelectionResponse;
}

it.each(INSTALLED_OPTION_ROUTES)("COMEN01C option %d opens its page %s", async (number, route) => {
    mockSelectMainMenuOption.mockResolvedValue({ route, message: "" });
    const user = await RenderLoadedScreen();
    await user.click(OptionButton(number));
    await waitFor(() => {
        expect(mockRouterPush).toHaveBeenCalledWith(route);
    });
    expect(mockRouterPush).toHaveBeenCalledTimes(1);
    expect(mockSelectMainMenuOption).toHaveBeenCalledWith({ option: String(number) });
});

it.each(UNEXPECTED_ROUTES)(
    "COMEN01C route %p shows the abend text in red and stays on /menu",
    async (route) => {
        mockSelectMainMenuOption.mockResolvedValue(SelectionAnswering(route));
        const user = await RenderLoadedScreen();
        await user.type(OptionInput(), "1");
        await user.click(KeyButton(CONTINUE_BUTTON_NAME));
        await ExpectAlert(ABEND_MESSAGE, "error");
        await WaitForIdle();
        expect(mockRouterPush).not.toHaveBeenCalled();
        expect(OptionInput()).toHaveFocus();
        expect(KeyButton(CONTINUE_BUTTON_NAME)).toBeEnabled();
    },
);

const HTTP_INTERNAL_SERVER_ERROR = 500;
// FastAPI's own text answer to an unhandled exception: not the legacy error model.
const SERVER_ERROR_BODY = "Internal Server Error";

interface SignOffFailureCase {
    cause: string;
    error: AxiosError;
    message: string;
}

// A logout that never reached the server: an axios error with no response.
function BuildNetworkError(): AxiosError {
    return new AxiosError("Network Error", AxiosError.ERR_NETWORK, REQUEST_CONFIG);
}

// The logout answered 500 by FastAPI itself, with no legacy body.
function BuildServerError(): AxiosError {
    const status = HTTP_INTERNAL_SERVER_ERROR;
    const config = REQUEST_CONFIG;
    const response = { status, statusText: "", headers: {}, config, data: SERVER_ERROR_BODY };
    const message = `Request failed with status code ${status}`;
    return new AxiosError(message, AxiosError.ERR_BAD_RESPONSE, config, null, response);
}

// Ordinary F3 sign-offs the server never confirmed, each with the message line it shows.
const SIGN_OFF_FAILURE_CASES: readonly SignOffFailureCase[] = [
    { cause: "a network failure", error: BuildNetworkError(), message: ABEND_MESSAGE },
    { cause: "a 500 without a legacy body", error: BuildServerError(), message: ABEND_MESSAGE },
];

// An unconfirmed sign-off keeps the session, stores no thank-you literal for /signon, stays on
// /menu with the cursor back on OPTION, and unlocks both PF keys.
function ExpectSignOffUnconfirmed(): void {
    expect(mockLogout).toHaveBeenCalledTimes(1);
    expect(mockClearSession).not.toHaveBeenCalled();
    expect(TakePendingMessage()).toBe("");
    expect(mockRouterPush).not.toHaveBeenCalled();
    expect(mockRouterReplace).not.toHaveBeenCalled();
    expect(OptionInput()).toHaveFocus();
    expect(KeyButton(EXIT_BUTTON_NAME)).toBeEnabled();
    expect(KeyButton(CONTINUE_BUTTON_NAME)).toBeEnabled();
}

it.each(SIGN_OFF_FAILURE_CASES)(
    "COMEN01C F3 after $cause stays signed on and shows $message in red",
    async ({ error, message }) => {
        mockLogout.mockRejectedValue(error);
        const user = await RenderLoadedScreen();
        await user.click(KeyButton(EXIT_BUTTON_NAME));
        await ExpectAlert(message, "error");
        await WaitForIdle();
        ExpectSignOffUnconfirmed();
    },
);

it("COMEN01C F3 again after a failed sign-off signs off to /signon", async () => {
    mockLogout.mockRejectedValueOnce(BuildNetworkError());
    mockLogout.mockResolvedValue({ message: THANK_YOU_MESSAGE });
    await RenderLoadedScreen();
    await PressKey("F3");
    await ExpectAlert(ABEND_MESSAGE, "error");
    await WaitForIdle();
    expect(mockClearSession).not.toHaveBeenCalled();
    await PressKey("F3");
    await waitFor(() => {
        expect(mockRouterPush).toHaveBeenCalledWith(SIGN_ON_ROUTE);
    });
    await WaitForIdle();
    expect(mockLogout).toHaveBeenCalledTimes(2);
    expect(mockClearSession).toHaveBeenCalledTimes(1);
    expect(TakePendingMessage()).toBe(THANK_YOU_MESSAGE);
    expect(screen.queryAllByRole("alert")).toHaveLength(0);
});

interface MapPaintCase {
    readName: string;
    ArrangeRead: () => void;
    optionCount: number;
    alertTexts: readonly string[];
}

// The menu read answered with every COMEN02Y option, and failed without a legacy body.
const MAP_PAINT_CASES: readonly MapPaintCase[] = [
    {
        readName: "answered",
        ArrangeRead: () => mockFetchMainMenu.mockResolvedValue(BuildMenuResponse()),
        optionCount: MAIN_MENU_LABELS.length,
        alertTexts: [],
    },
    {
        readName: "failed",
        ArrangeRead: () => mockFetchMainMenu.mockRejectedValue(BuildServerError()),
        optionCount: 0,
        alertTexts: [ABEND_MESSAGE],
    },
];

// One SEND-MENU-SCREEN paints the whole map, so nothing already shown moves when it arrives.
it.each(MAP_PAINT_CASES)(
    "COMEN01C menu read $readName: the map paints at once, with the cursor on OPTION",
    async ({ ArrangeRead, optionCount, alertTexts }) => {
        ArrangeRead();
        RenderScreen();
        expect(screen.getByRole("heading", { level: 1, name: SCREEN_TITLE })).toBeVisible();
        expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "true");
        expect(screen.queryByRole("navigation", { name: MENU_OPTIONS_LABEL })).toBeNull();
        expect(screen.queryByRole("textbox", { name: OPTION_PROMPT })).toBeNull();
        expect(screen.queryByRole("group", { name: "Function keys" })).toBeNull();
        await WaitForIdle();
        expect(within(MenuList()).queryAllByRole("button")).toHaveLength(optionCount);
        expect(OptionInput()).toHaveFocus();
        expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "false");
        const shownAlertTexts = screen.queryAllByRole("alert").map((alert) => alert.textContent);
        expect(shownAlertTexts).toEqual(alertTexts);
    },
);

// A selection held in flight until the test answers it through the returned function.
function HoldSelection(): (answer: MenuSelectionResponse) => Promise<void> {
    let answerSelection: (answer: MenuSelectionResponse) => void = () => undefined;
    mockSelectMainMenuOption.mockImplementation(
        () =>
            new Promise<MenuSelectionResponse>((resolve) => {
                answerSelection = resolve;
            }),
    );
    return async (answer) => {
        await act(async () => {
            answerSelection(answer);
        });
    };
}

// The two pointer presses that send a selection: the ENTER button and a listed option.
const POINTER_SELECTION_CASES: readonly KeyPressCase[] = [
    ENTER_CASES[0] ?? { keyName: "", Press: async () => undefined },
    {
        keyName: "listed option 11",
        Press: (user) => user.click(OptionButton(MAIN_MENU_LABELS.length)),
    },
];

// The 3270 keyboard locked from send to response: the cursor stays in OPTION, which refuses
// keystrokes until the answer arrives.
it.each(POINTER_SELECTION_CASES)(
    "COMEN01C a selection sent by $keyName locks OPTION, keeping the cursor, until it answers",
    async ({ Press }) => {
        const answerSelection = HoldSelection();
        const user = await RenderLoadedScreen();
        await user.type(OptionInput(), "1");
        await Press(user);
        expect(OptionInput()).toHaveFocus();
        expect(OptionInput()).toHaveAttribute("readonly");
        expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "true");
        await user.keyboard("2");
        expect(OptionInput()).toHaveValue("1");
        await answerSelection(NOT_INSTALLED_ANSWER);
        await WaitForIdle();
        expect(OptionInput()).not.toHaveAttribute("readonly");
        expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "false");
        await user.keyboard("1");
        expect(OptionInput()).toHaveValue("11");
    },
);

// The keyboard lock covers every PF key: one pressed during a selection is answered by nothing.
it("COMEN01C answers no other PF key while a selection is in flight", async () => {
    const answerSelection = HoldSelection();
    const user = await RenderLoadedScreen();
    await user.type(OptionInput(), "11");
    await user.click(KeyButton(CONTINUE_BUTTON_NAME));
    expect(KeyButton(CONTINUE_BUTTON_NAME)).toBeDisabled();
    for (const key of UNBOUND_KEYS) {
        await PressKey(key);
    }
    expect(screen.queryAllByRole("alert")).toHaveLength(0);
    await answerSelection(NOT_INSTALLED_ANSWER);
    await WaitForIdle();
    const alertTexts = screen.queryAllByRole("alert").map((alert) => alert.textContent);
    expect(alertTexts).toEqual([NOT_INSTALLED_MESSAGE]);
    expect(mockSelectMainMenuOption).toHaveBeenCalledTimes(1);
});

// A second ENTER, the bound F3, or a PF key outside the legend, pressed in ENTER's turn.
const SAME_TURN_SECOND_KEYS: readonly string[] = ["Enter", "F3", "F12"];

// Keys dispatched on window inside one act, so no render, and no effect handing the keys their
// disabled state, runs between two presses.
async function PressInOneTurn(keys: readonly string[]): Promise<void> {
    await act(async () => {
        for (const key of keys) {
            const init = { key, bubbles: true, cancelable: true };
            window.dispatchEvent(new KeyboardEvent("keydown", init));
        }
    });
}

// The keys disable a render after ENTER, so a key pressed in that gap meets the in-flight lock:
// one selection is sent, its route is followed, and the lock then clears.
it.each(SAME_TURN_SECOND_KEYS)(
    "COMEN01C ENTER and %s in one turn send one selection, answered as normal",
    async (secondKey) => {
        mockLogout.mockResolvedValue({ message: THANK_YOU_MESSAGE });
        const answerSelection = HoldSelection();
        const user = await RenderLoadedScreen();
        await user.type(OptionInput(), "8");
        await PressInOneTurn(["Enter", secondKey]);
        expect(mockSelectMainMenuOption).toHaveBeenCalledTimes(1);
        expect(mockSelectMainMenuOption).toHaveBeenCalledWith({ option: "8" });
        expect(mockLogout).not.toHaveBeenCalled();
        expect(screen.queryAllByRole("alert")).toHaveLength(0);
        await answerSelection(TRANSACTION_ADD_ANSWER);
        await WaitForIdle();
        expect(mockRouterPush).toHaveBeenCalledTimes(1);
        expect(mockRouterPush).toHaveBeenCalledWith(TRANSACTION_ADD_ANSWER.route);
        await PressInOneTurn(["Enter"]);
        expect(mockSelectMainMenuOption).toHaveBeenCalledTimes(2);
        expect(mockSelectMainMenuOption).toHaveBeenLastCalledWith({ option: "8" });
    },
);

// The routed page's text once its payload has loaded.
const NEXT_PAGE_TEXT = "Account View page";
// A wait long enough for every task an answered call queued to run.
const SCHEDULED_WORK_MS = 20;

// router.push hands the stand-in for the next page its pending payload; set by the stand-in.
let showNextPage: (payload: Promise<string>) => void = () => undefined;

interface NextPageContentProps {
    payload: Promise<string>;
}

function NextPageContent({ payload }: NextPageContentProps) {
    return <Typography>{use(payload)}</Typography>;
}

// Stands in for the App Router's next page: it suspends until its payload loads, as Next.js
// suspends the route transition router.push starts until the routed page can render.
function NextPageStandIn() {
    const [payload, setPayload] = useState<Promise<string> | null>(null);
    useEffect(() => {
        showNextPage = setPayload;
    }, []);
    return payload === null ? null : <NextPageContent payload={payload} />;
}

function RenderScreenBesideNextPage(): UserEvent {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <Suspense fallback={null}>
                <NextPageStandIn />
            </Suspense>
            <MainMenuPage />
        </ThemeProvider>,
    );
    return user;
}

// Lets every task the answered call queued run, outside act, as the browser runs them.
async function RunScheduledWork(): Promise<void> {
    await waitFor(() => new Promise((resolve) => setTimeout(resolve, SCHEDULED_WORK_MS)));
}

// router.push opens a next page whose payload loads, inside act, when the test calls the
// returned function.
function HoldNextPage(): (pageText: string) => Promise<void> {
    let loadPayload: (pageText: string) => void = () => undefined;
    const nextPagePayload = new Promise<string>((resolve) => {
        loadPayload = resolve;
    });
    mockRouterPush.mockImplementation(() => showNextPage(nextPagePayload));
    return async (pageText) => {
        await act(async () => {
            loadPayload(pageText);
        });
    };
}

// The XCTL ends with the next screen: the menu stays locked after the selection answers, until
// the routed page has rendered, so a second ENTER in that time sends no second selection.
it("COMEN01C stays busy until the routed page renders; a second ENTER sends nothing", async () => {
    const loadNextPage = HoldNextPage();
    mockSelectMainMenuOption.mockResolvedValue(ACCOUNT_VIEW_ANSWER);
    const user = RenderScreenBesideNextPage();
    await WaitForIdle();
    await user.type(OptionInput(), "1");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(mockRouterPush).toHaveBeenCalledWith(ACCOUNT_VIEW_ANSWER.route));
    await RunScheduledWork();
    expect(screen.getByRole("progressbar", { name: "Processing" })).toBeInTheDocument();
    expect(KeyButton(CONTINUE_BUTTON_NAME)).toBeDisabled();
    expect(OptionInput()).toHaveAttribute("readonly");
    await user.keyboard("{Enter}");
    expect(mockSelectMainMenuOption).toHaveBeenCalledTimes(1);
    await loadNextPage(NEXT_PAGE_TEXT);
    expect(screen.getByText(NEXT_PAGE_TEXT)).toBeInTheDocument();
    await WaitForIdle();
    expect(mockSelectMainMenuOption).toHaveBeenCalledTimes(1);
});
