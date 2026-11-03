// Screen tests: BMS COMEN01, program COMEN01C (CM00), page /menu.
// Literals verbatim from backend/app/services/menu/comen01c.py, menu_options.py,
// backend/app/core/messages.py and cbl/COMEN01C.cbl; option labels from cpy/COMEN02Y.cpy.
import { ThemeProvider } from "@mui/material/styles";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { AxiosError, AxiosHeaders, CanceledError, type InternalAxiosRequestConfig } from "axios";
import type * as NavigationModule from "next/navigation";
import { StrictMode, type ReactNode } from "react";

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
const mockCurrentUser: CurrentUserResponse = {
    user_id: "TESTUSR1",
    user_type: "U",
    first_name: "Test",
    last_name: "User",
    next_route: "/menu",
};
const mockClearSession = jest.fn();
const mockSession: SessionContextValue = {
    currentUser: mockCurrentUser,
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

function MenuList(): HTMLElement {
    return screen.getByRole("list", { name: MENU_OPTIONS_LABEL });
}

// The ListItemButton at one list position (1 = first), in the order the server listed them.
function OptionButton(position: number): HTMLElement {
    const optionButton = within(MenuList()).getAllByRole("button")[position - 1];
    if (optionButton === undefined) {
        throw new Error(`No option button at list position ${position}`);
    }
    return optionButton;
}

function KeyButton(name: string): HTMLElement {
    return screen.getByRole("button", { name });
}

// useFunctionKeys listens on window, as the 3270 AID key belonged to the whole screen.
function PressKey(key: string): void {
    fireEvent.keyDown(window, { key });
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
    { keyName: "the Enter key", Press: async () => PressKey("Enter") },
];

const EXIT_CASES: readonly KeyPressCase[] = [
    { keyName: "the Exit (F3) button", Press: (user) => user.click(KeyButton(EXIT_BUTTON_NAME)) },
    { keyName: "the F3 key", Press: async () => PressKey("F3") },
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
