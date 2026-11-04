// Screen tests: BMS COADM01, program COADM01C (CA00), page /admin.
// Copybooks COADM02Y (the six admin options) and CSMSG01Y (the sign-off literal). The literals are
// copied verbatim from backend/app/services/menu/coadm01c.py, menu_options.py and core/messages.py.
import { Typography } from "@mui/material";
import { ThemeProvider } from "@mui/material/styles";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { AxiosError, AxiosHeaders, CanceledError, type InternalAxiosRequestConfig } from "axios";
import { StrictMode, Suspense, use, useEffect, useState, type ReactNode } from "react";

import AdminMenuPage from "@/app/admin/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import { Logout, type CurrentUserResponse } from "@/lib/api/auth";
import {
    FetchAdminMenu,
    SelectAdminMenuOption,
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

// The "mock" prefix lets the hoisted jest.mock factories read these at call time.
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
let mockSearchParams = new URLSearchParams();
const mockClearSession = jest.fn();
const mockRefreshSession = jest.fn();
// A fictitious admin: user type A is the one COSGN00C sends to COADM01C.
const mockSessionUser: CurrentUserResponse = {
    user_id: "TESTADM1",
    user_type: "A",
    first_name: "Test",
    last_name: "Admin",
    next_route: "/admin",
};

jest.mock("next/navigation", () => ({
    useRouter: () => mockRouter,
    useSearchParams: () => mockSearchParams,
    usePathname: () => "/admin",
}));

// The mocked keys mirror the exports of @/lib/session and its SessionContextValue.
jest.mock("@/lib/session", () => ({
    SessionProvider: ({ children }: { children: ReactNode }) => children,
    useSession: () => ({
        currentUser: mockSessionUser,
        isLoading: false,
        RefreshSession: mockRefreshSession,
        ClearSession: mockClearSession,
    }),
}));

jest.mock("@/lib/api/menu", () => ({
    ...jest.requireActual<Record<string, unknown>>("@/lib/api/menu"),
    FetchAdminMenu: jest.fn(),
    SelectAdminMenuOption: jest.fn(),
}));

jest.mock("@/lib/api/auth", () => ({
    ...jest.requireActual<Record<string, unknown>>("@/lib/api/auth"),
    Logout: jest.fn(),
}));

const mockFetchAdminMenu = jest.mocked(FetchAdminMenu);
const mockSelectAdminMenuOption = jest.mocked(SelectAdminMenuOption);
const mockLogout = jest.mocked(Logout);

type KeyPress = (user: UserEvent) => Promise<void>;

interface LoadFailureCase {
    status: number;
    body: unknown;
    message: string;
}

// The COADM01 map: row-4 title, OPTION prompt (LENGTH 2) and the "ENTER=Continue  F3=Exit" legend.
const SCREEN_TITLE = "Admin Menu";
const OPTION_PROMPT = "Please select an option :";
const OPTION_LIST_LABEL = "Admin menu options";
const CONTINUE_BUTTON = "Continue (Enter)";
const EXIT_BUTTON = "Exit (F3)";
// The field_errors and cursor_field name of the OPTION map field.
const OPTION_FIELD = "option";

// COADM02Y CDEMO-ADMIN-OPT-NAME, right-trimmed, for options 1 to 6.
const USER_LIST_LABEL = "User List (Security)";
const USER_ADD_LABEL = "User Add (Security)";
const TRAN_TYPE_LIST_LABEL = "Transaction Type List/Update (Db2)";
const TRAN_TYPE_MAINTENANCE_LABEL = "Transaction Type Maintenance (Db2)";
const ADMIN_MENU_LABELS: readonly string[] = [
    USER_LIST_LABEL,
    USER_ADD_LABEL,
    "User Update (Security)",
    "User Delete (Security)",
    TRAN_TYPE_LIST_LABEL,
    TRAN_TYPE_MAINTENANCE_LABEL,
];
const LABEL_CASES: readonly [number, string][] = ADMIN_MENU_LABELS.map((label, index) => [
    index + 1,
    label,
]);

const USER_LIST_ROUTE = "/admin/users";
const USER_ADD_ROUTE = "/admin/users/add";

// COADM01C line 135; lines 152-155 and 273-276, whose option-name operand is commented out, so
// one blank separates "option" from "is"; CSMSG01Y CCDA-MSG-THANK-YOU right-trimmed; CSMSG02Y
// ABEND-MSG; COMEN01C line 140, whose trailing blank the literal keeps.
const INVALID_OPTION_MESSAGE = "Please enter a valid option number...";
const NOT_INSTALLED_MESSAGE = "This option is not installed ...";
const THANK_YOU_MESSAGE = "Thank you for using CardDemo application...";
const ABEND_MESSAGE = "UNEXPECTED ABEND OCCURRED.";
const ADMIN_ONLY_MESSAGE = "No access - Admin Only option... ";

// MUI 9 marks the Alert colour as MuiAlert-color<Severity> (Alert/alertClasses.d.ts).
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

// The header clock (CURDATE/CURTIME) reads Date.now(); a fixed local instant keeps the header
// still, so its one-second interval never re-renders outside act.
const HEADER_CLOCK_MILLISECONDS = new Date(2022, 6, 18, 9, 5, 7).getTime();

// FastAPI's 500 has no legacy body, so the page shows the abend text.
const SERVER_ERROR_BODY = "Internal Server Error";
const NOT_INSTALLED_RESPONSE: MenuSelectionResponse = {
    route: null,
    message: NOT_INSTALLED_MESSAGE,
};

function BuildMenuResponse(): MenuResponse {
    const options: MenuOption[] = LABEL_CASES.map(([number, label]) => ({ number, label }));
    return { options };
}

function BuildRouteResponse(route: string): MenuSelectionResponse {
    return { route, message: "" };
}

// The common.LegacyErrorResponse body as backend/app/core/errors.ErrorBody builds it.
function BuildErrorBody(overrides: Partial<LegacyErrorResponse> = {}): LegacyErrorResponse {
    return { message: "", info_message: "", field_errors: [], cursor_field: "", ...overrides };
}

function BuildAxiosError(status: number, body: unknown): AxiosError {
    const config: InternalAxiosRequestConfig = { headers: new AxiosHeaders() };
    const response = { status, statusText: "", headers: {}, config, data: body };
    return new AxiosError(
        "Request failed",
        AxiosError.ERR_BAD_RESPONSE,
        config,
        undefined,
        response,
    );
}

// SelectOption's LegacyMessageError: 400 with the cursor on OPTION.
const INVALID_OPTION_ERROR_BODY = BuildErrorBody({
    message: INVALID_OPTION_MESSAGE,
    field_errors: [OPTION_FIELD],
    cursor_field: OPTION_FIELD,
});

function RenderScreen(): UserEvent {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <AdminMenuPage />
        </ThemeProvider>,
    );
    return user;
}

// StrictMode mounts, cleans up and mounts again, as next dev does under reactStrictMode.
function RenderStrictModeScreen(): void {
    render(
        <StrictMode>
            <ThemeProvider theme={CARD_DEMO_THEME}>
                <AdminMenuPage />
            </ThemeProvider>
        </StrictMode>,
    );
}

// Busy ends when the last call settles: the progress bar goes and the keys unlock.
async function WaitForIdle(): Promise<void> {
    await waitFor(() => expect(screen.queryByRole("progressbar")).toBeNull());
}

// SEND-MENU-SCREEN after the menu read: every option listed and the keys unlocked.
async function RenderLoadedScreen(): Promise<UserEvent> {
    const user = RenderScreen();
    await screen.findByText(USER_LIST_LABEL);
    await WaitForIdle();
    return user;
}

function OptionField(): HTMLElement {
    return screen.getByRole("textbox", { name: OPTION_PROMPT });
}

function OptionList(): HTMLElement {
    return screen.getByRole("navigation", { name: OPTION_LIST_LABEL });
}

// The ListItemButton of one option, named by its label followed by its number.
function OptionButton(number: number, label: string): HTMLElement {
    return within(OptionList()).getByRole("button", { name: `${label} ${number}` });
}

// The Grid item holding the OPTION field: its nearest Grid ancestor.
function OptionGridItem(): Element {
    const gridItem = OptionField().closest(`.${GRID_ROOT_CLASS}`);
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

// The message line's Alert, compared on its raw text so every blank of the literal counts.
async function ExpectAlert(text: string, severity: MessageSeverity): Promise<void> {
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(text);
    expect(alert).toHaveClass(SEVERITY_CLASSES[severity]);
}

// A physical key pressed in the focused field bubbles to the useFunctionKeys window listener.
// user-event, not a bare keyDown, so the call the key starts settles inside its act wrapper.
async function PressKey(user: UserEvent, key: string): Promise<void> {
    await user.keyboard(`{${key}}`);
}

function ClickKey(buttonName: string): KeyPress {
    return async (user) => {
        await user.click(screen.getByRole("button", { name: buttonName }));
    };
}

function TypeKey(key: string): KeyPress {
    return (user) => PressKey(user, key);
}

// Keys the OPTION field as given, then presses the Continue (Enter) button. user-event cannot
// type an empty string, so a blank field is simply left untouched.
async function SubmitOption(user: UserEvent, optionText: string): Promise<void> {
    if (optionText !== "") {
        await user.type(OptionField(), optionText);
    }
    await user.click(screen.getByRole("button", { name: CONTINUE_BUTTON }));
}

// A menu read that stays in flight until its signal is aborted, then rejects as axios does.
function RejectOnAbort(options?: ReadOptions): Promise<MenuResponse> {
    return new Promise((_resolve, reject) => {
        options?.signal?.addEventListener("abort", () => {
            reject(new CanceledError());
        });
    });
}

// The AbortSignal the page gave one FetchAdminMenu call (0 = the first).
function MenuReadSignal(callIndex: number): AbortSignal {
    const signal = mockFetchAdminMenu.mock.calls[callIndex]?.[0]?.signal;
    if (signal === undefined) {
        throw new Error(`FetchAdminMenu call ${callIndex} carried no AbortSignal`);
    }
    return signal;
}

// A selection call that stays in flight until the returned function answers it.
function DeferSelection(): (response: MenuSelectionResponse) => void {
    let answerSelection: (response: MenuSelectionResponse) => void = () => undefined;
    const pendingSelection = new Promise<MenuSelectionResponse>((resolve) => {
        answerSelection = resolve;
    });
    mockSelectAdminMenuOption.mockReturnValue(pendingSelection);
    return (response) => answerSelection(response);
}

const ENTER_PRESSES: readonly [string, KeyPress][] = [
    ["Continue (Enter) button", ClickKey(CONTINUE_BUTTON)],
    ["Enter key", TypeKey("Enter")],
];

const F3_PRESSES: readonly [string, KeyPress][] = [
    ["Exit (F3) button", ClickKey(EXIT_BUTTON)],
    ["F3 key", TypeKey("F3")],
];

// Options 5 and 6 reach the excluded Db2 programs COTRTLIC and COTRTUPC (PGMIDERR).
const NOT_INSTALLED_CASES: readonly [number, string][] = [
    [5, TRAN_TYPE_LIST_LABEL],
    [6, TRAN_TYPE_MAINTENANCE_LABEL],
];

// Blank, zero, beyond CDEMO-ADMIN-OPT-COUNT 6, and not NUMERIC: each sent as keyed.
const INVALID_OPTION_TEXTS: readonly string[] = ["", "0", "7", "x"];

// The menu read refused: a regular user's role refusal, and a server failure.
const LOAD_FAILURE_CASES: readonly LoadFailureCase[] = [
    {
        status: 403,
        body: BuildErrorBody({ message: ADMIN_ONLY_MESSAGE }),
        message: ADMIN_ONLY_MESSAGE,
    },
    { status: 500, body: SERVER_ERROR_BODY, message: ABEND_MESSAGE },
];

let headerClockSpy: jest.SpyInstance<number, []>;

beforeEach(() => {
    headerClockSpy = jest.spyOn(Date, "now").mockReturnValue(HEADER_CLOCK_MILLISECONDS);
    mockSearchParams = new URLSearchParams();
    window.sessionStorage.clear();
    mockSelectAdminMenuOption.mockReset();
    mockFetchAdminMenu.mockReset().mockResolvedValue(BuildMenuResponse());
    mockLogout.mockReset().mockResolvedValue({ message: THANK_YOU_MESSAGE });
});

afterEach(() => {
    headerClockSpy.mockRestore();
});

describe("COADM01C BMS COADM01 header and OPTION field", () => {
    it("COADM01C /admin shows the CA00 header, title, OPTION field and legend", async () => {
        await RenderLoadedScreen();
        const banner = screen.getByRole("banner");
        expect(within(banner).getByText("Tran: CA00")).toBeInTheDocument();
        expect(within(banner).getByText("Prog: COADM01C")).toBeInTheDocument();
        expect(within(banner).getByText("Date: 07/18/22")).toBeInTheDocument();
        expect(screen.getByRole("heading", { level: 1, name: SCREEN_TITLE })).toBeVisible();
        expect(OptionField()).toHaveAttribute("maxlength", "2");
        expect(OptionField()).toHaveFocus();
        expect(screen.getByRole("button", { name: CONTINUE_BUTTON })).toBeEnabled();
        expect(screen.getByRole("button", { name: EXIT_BUTTON })).toBeEnabled();
        expect(screen.queryByRole("alert")).toBeNull();
    });
});

describe("COADM01C COADM02Y option list", () => {
    it("COADM01C /admin lists the six COADM02Y options in table order", async () => {
        await RenderLoadedScreen();
        const optionTexts = within(OptionList())
            .getAllByRole("button")
            .map((optionButton) => optionButton.textContent);
        expect(optionTexts).toEqual(LABEL_CASES.map(([number, label]) => `${label}${number}`));
        expect(mockFetchAdminMenu).toHaveBeenCalledTimes(1);
    });

    it.each(LABEL_CASES)("COADM02Y option %d shows %p beside its number", async (number, label) => {
        await RenderLoadedScreen();
        const optionButton = OptionButton(number, label);
        expect(within(optionButton).getByText(label)).toBeInTheDocument();
        expect(within(optionButton).getByText(String(number))).toBeInTheDocument();
    });
});

describe("COADM01C menu layout", () => {
    it("COADM01C nests one list item per option in the named nav; OPTION xs 12, md 6", async () => {
        await RenderLoadedScreen();
        expect(screen.queryByRole("list", { name: OPTION_LIST_LABEL })).toBeNull();
        const optionList = within(OptionList()).getByRole("list");
        const optionItems = within(optionList).getAllByRole("listitem");
        expect(optionItems).toHaveLength(LABEL_CASES.length);
        optionItems.forEach((optionItem) => {
            expect(within(optionItem).getAllByRole("button")).toHaveLength(1);
        });
        const gridItem = OptionGridItem();
        expect(gridItem.parentElement).toHaveClass(GRID_CONTAINER_CLASS);
        expect(GridSizeClasses(gridItem)).toEqual(OPTION_GRID_SIZE_CLASSES);
    });
});

describe("COADM01C option click", () => {
    it("COADM01C option 1 click sends 1 and routes to /admin/users", async () => {
        mockSelectAdminMenuOption.mockResolvedValue(BuildRouteResponse(USER_LIST_ROUTE));
        const user = await RenderLoadedScreen();
        await user.click(OptionButton(1, USER_LIST_LABEL));
        await waitFor(() => expect(mockRouterPush).toHaveBeenCalledWith(USER_LIST_ROUTE));
        expect(mockSelectAdminMenuOption).toHaveBeenCalledWith({ option: "1" });
        expect(mockRouterPush).toHaveBeenCalledTimes(1);
        expect(mockRouterReplace).not.toHaveBeenCalled();
        expect(screen.queryByRole("alert")).toBeNull();
    });
});

describe("COADM01C ENTER (PROCESS-ENTER-KEY)", () => {
    it.each(ENTER_PRESSES)("COADM01C ENTER sends keyed 2 via the %s", async (_via, press) => {
        mockSelectAdminMenuOption.mockResolvedValue(BuildRouteResponse(USER_ADD_ROUTE));
        const user = await RenderLoadedScreen();
        await user.type(OptionField(), "2");
        await press(user);
        await waitFor(() => expect(mockRouterPush).toHaveBeenCalledWith(USER_ADD_ROUTE));
        expect(mockSelectAdminMenuOption).toHaveBeenCalledTimes(1);
        expect(mockSelectAdminMenuOption).toHaveBeenCalledWith({ option: "2" });
    });

    it("COADM01C ENTER sends the OPTION text raw, leading blank kept", async () => {
        mockSelectAdminMenuOption.mockResolvedValue(BuildRouteResponse(USER_LIST_ROUTE));
        const user = await RenderLoadedScreen();
        await SubmitOption(user, " 1");
        await waitFor(() => expect(mockRouterPush).toHaveBeenCalledWith(USER_LIST_ROUTE));
        expect(mockSelectAdminMenuOption).toHaveBeenCalledWith({ option: " 1" });
    });
});

describe("COADM01C keyboard lock", () => {
    it("COADM01C locks the keys while the selection is in flight", async () => {
        const answerSelection = DeferSelection();
        const user = await RenderLoadedScreen();
        await user.click(OptionButton(1, USER_LIST_LABEL));
        expect(screen.getByRole("progressbar", { name: "Processing" })).toBeInTheDocument();
        expect(screen.getByRole("button", { name: CONTINUE_BUTTON })).toBeDisabled();
        expect(screen.getByRole("button", { name: EXIT_BUTTON })).toBeDisabled();
        expect(OptionButton(2, USER_ADD_LABEL)).toHaveAttribute("aria-disabled", "true");
        answerSelection(BuildRouteResponse(USER_LIST_ROUTE));
        await waitFor(() => expect(mockRouterPush).toHaveBeenCalledWith(USER_LIST_ROUTE));
        await WaitForIdle();
    });
});

describe("COADM01C not-installed literal (PGMIDERR-ERR-PARA)", () => {
    it.each(NOT_INSTALLED_CASES)(
        "COADM01C option %d shows the not-installed literal in green",
        async (number, label) => {
            mockSelectAdminMenuOption.mockResolvedValue(NOT_INSTALLED_RESPONSE);
            const user = await RenderLoadedScreen();
            await user.click(OptionButton(number, label));
            await ExpectAlert(NOT_INSTALLED_MESSAGE, "success");
            expect(mockSelectAdminMenuOption).toHaveBeenCalledWith({ option: String(number) });
            expect(mockRouterPush).not.toHaveBeenCalled();
            expect(OptionField()).toHaveAttribute("aria-invalid", "false");
        },
    );
});

describe("COADM01C invalid-option literal", () => {
    it.each(INVALID_OPTION_TEXTS)(
        "COADM01C option %p shows the invalid-option literal in red",
        async (optionText) => {
            mockSelectAdminMenuOption.mockRejectedValue(
                BuildAxiosError(400, INVALID_OPTION_ERROR_BODY),
            );
            const user = await RenderLoadedScreen();
            await SubmitOption(user, optionText);
            await ExpectAlert(INVALID_OPTION_MESSAGE, "error");
            expect(mockSelectAdminMenuOption).toHaveBeenCalledWith({ option: optionText });
            expect(mockRouterPush).not.toHaveBeenCalled();
        },
    );
});

describe("COADM01C abend text", () => {
    it("COADM01C selection failure without a legacy body shows the abend text", async () => {
        mockSelectAdminMenuOption.mockRejectedValue(BuildAxiosError(500, SERVER_ERROR_BODY));
        const user = await RenderLoadedScreen();
        await user.click(OptionButton(1, USER_LIST_LABEL));
        await ExpectAlert(ABEND_MESSAGE, "error");
        expect(OptionField()).toHaveAttribute("aria-invalid", "false");
        expect(mockRouterPush).not.toHaveBeenCalled();
    });
});

describe("COADM01C menu read failure", () => {
    it.each(LOAD_FAILURE_CASES)(
        "COADM01C menu read failure $status shows $message and no options",
        async ({ status, body, message }) => {
            mockFetchAdminMenu.mockRejectedValue(BuildAxiosError(status, body));
            RenderScreen();
            await ExpectAlert(message, "error");
            expect(within(OptionList()).queryAllByRole("button")).toHaveLength(0);
            expect(mockRouterPush).not.toHaveBeenCalled();
        },
    );
});

describe("COADM01C OPTION field error", () => {
    it("COADM01C marks OPTION in error and returns the cursor to it", async () => {
        mockSelectAdminMenuOption.mockRejectedValue(
            BuildAxiosError(400, INVALID_OPTION_ERROR_BODY),
        );
        const user = await RenderLoadedScreen();
        expect(OptionField()).toHaveAttribute("aria-invalid", "false");
        await SubmitOption(user, "9");
        await ExpectAlert(INVALID_OPTION_MESSAGE, "error");
        expect(OptionField()).toHaveAttribute("aria-invalid", "true");
        expect(OptionField()).toHaveAccessibleDescription(INVALID_OPTION_MESSAGE);
        expect(OptionField()).toHaveFocus();
        expect(OptionField()).toHaveValue("9");
    });
});

describe("COADM01C PF3 sign-off", () => {
    it.each(F3_PRESSES)("COADM01C F3 signs off to /signon via the %s", async (_via, press) => {
        const user = await RenderLoadedScreen();
        await press(user);
        await waitFor(() => expect(mockRouterPush).toHaveBeenCalledWith(SIGN_ON_ROUTE));
        expect(SIGN_ON_ROUTE).toBe("/signon");
        expect(mockLogout).toHaveBeenCalledTimes(1);
        expect(mockClearSession).toHaveBeenCalledTimes(1);
        expect(mockSelectAdminMenuOption).not.toHaveBeenCalled();
        expect(TakePendingMessage()).toBe(THANK_YOU_MESSAGE);
    });
});

interface SignOffFailureCase {
    cause: string;
    error: AxiosError;
    message: string;
}

// The request never reached the server, so axios rejects without a response.
function BuildNetworkError(): AxiosError {
    const config: InternalAxiosRequestConfig = { headers: new AxiosHeaders() };
    return new AxiosError("Network Error", AxiosError.ERR_NETWORK, config);
}

// Sign-off calls whose session cookie the server never cleared, each with the message shown.
const SIGN_OFF_FAILURE_CASES: readonly SignOffFailureCase[] = [
    {
        cause: "a 500 without a legacy body",
        error: BuildAxiosError(500, SERVER_ERROR_BODY),
        message: ABEND_MESSAGE,
    },
    { cause: "a network failure", error: BuildNetworkError(), message: ABEND_MESSAGE },
    {
        cause: "a 403 with a legacy body",
        error: BuildAxiosError(403, BuildErrorBody({ message: ADMIN_ONLY_MESSAGE })),
        message: ADMIN_ONLY_MESSAGE,
    },
];

describe("COADM01C PF3 sign-off failure", () => {
    it.each(SIGN_OFF_FAILURE_CASES)(
        "COADM01C F3 after $cause stays signed on and shows $message",
        async ({ error, message }) => {
            mockLogout.mockRejectedValue(error);
            const user = await RenderLoadedScreen();
            await user.click(screen.getByRole("button", { name: EXIT_BUTTON }));
            await ExpectAlert(message, "error");
            await WaitForIdle();
            expect(mockLogout).toHaveBeenCalledTimes(1);
            expect(mockClearSession).not.toHaveBeenCalled();
            expect(mockRouterPush).not.toHaveBeenCalled();
            expect(TakePendingMessage()).toBe("");
            expect(OptionField()).toHaveFocus();
            expect(screen.getByRole("button", { name: EXIT_BUTTON })).toBeEnabled();
            expect(screen.getByRole("button", { name: CONTINUE_BUTTON })).toBeEnabled();
        },
    );
});

describe("COADM01C PF3 sign-off retry", () => {
    it("COADM01C F3 pressed again after a failed sign-off signs off to /signon", async () => {
        mockLogout.mockRejectedValueOnce(BuildNetworkError());
        const user = await RenderLoadedScreen();
        await PressKey(user, "F3");
        await ExpectAlert(ABEND_MESSAGE, "error");
        await WaitForIdle();
        await PressKey(user, "F3");
        await waitFor(() => expect(mockRouterPush).toHaveBeenCalledWith(SIGN_ON_ROUTE));
        expect(mockLogout).toHaveBeenCalledTimes(2);
        expect(mockClearSession).toHaveBeenCalledTimes(1);
        expect(TakePendingMessage()).toBe(THANK_YOU_MESSAGE);
        expect(screen.queryByRole("alert")).toBeNull();
    });
});

// The page never routes a 401 itself: the apiClient interceptor sends it to SIGN_ON_ROUTE.
describe("COADM01C 401 session refusal on the menu read", () => {
    it("COADM01C 401 on the menu read shows no message and no options", async () => {
        mockFetchAdminMenu.mockRejectedValue(BuildAxiosError(401, BuildErrorBody()));
        RenderScreen();
        await WaitForIdle();
        expect(mockFetchAdminMenu).toHaveBeenCalledTimes(1);
        expect(screen.queryByRole("alert")).toBeNull();
        expect(within(OptionList()).queryAllByRole("button")).toHaveLength(0);
        expect(mockRouterPush).not.toHaveBeenCalled();
        expect(SIGN_ON_ROUTE).toBe("/signon");
    });
});

describe("COADM01C 401 session refusal on a selection", () => {
    it("COADM01C 401 on a selection shows no message and stays put", async () => {
        mockSelectAdminMenuOption.mockRejectedValue(BuildAxiosError(401, BuildErrorBody()));
        const user = await RenderLoadedScreen();
        await user.click(OptionButton(1, USER_LIST_LABEL));
        await WaitForIdle();
        expect(mockSelectAdminMenuOption).toHaveBeenCalledWith({ option: "1" });
        expect(screen.queryByRole("alert")).toBeNull();
        expect(mockRouterPush).not.toHaveBeenCalled();
    });
});

describe("COADM01C menu read abandoned on leaving the screen", () => {
    it("COADM01C leaving the menu while its read is pending aborts the menu GET", async () => {
        mockFetchAdminMenu.mockImplementation(RejectOnAbort);
        RenderScreen();
        expect(mockFetchAdminMenu).toHaveBeenCalledTimes(1);
        expect(mockFetchAdminMenu).toHaveBeenCalledWith({ signal: expect.any(AbortSignal) });
        expect(MenuReadSignal(0).aborted).toBe(false);
        cleanup();
        expect(MenuReadSignal(0).aborted).toBe(true);
        await expect(mockFetchAdminMenu.mock.results[0]?.value).rejects.toBeInstanceOf(
            CanceledError,
        );
        expect(mockRouterPush).not.toHaveBeenCalled();
    });
});

// StrictMode's mount, cleanup and mount: the first read is aborted and the re-run's read loads.
describe("COADM01C menu read under StrictMode", () => {
    it("COADM01C StrictMode re-run reloads the menu and keeps the pending message", async () => {
        StorePendingMessage(ADMIN_ONLY_MESSAGE);
        mockFetchAdminMenu
            .mockImplementationOnce(RejectOnAbort)
            .mockResolvedValue(BuildMenuResponse());
        RenderStrictModeScreen();
        await WaitForIdle();
        expect(mockFetchAdminMenu).toHaveBeenCalledTimes(2);
        expect(MenuReadSignal(0).aborted).toBe(true);
        expect(MenuReadSignal(1).aborted).toBe(false);
        expect(within(OptionList()).getAllByRole("button")).toHaveLength(LABEL_CASES.length);
        await ExpectAlert(ADMIN_ONLY_MESSAGE, "error");
    });
});

// The routes of options 1-4 in menu_options.ADMIN_MENU_OPTIONS.
const INSTALLED_OPTION_ROUTES: readonly [number, string][] = [
    [1, USER_LIST_ROUTE],
    [2, USER_ADD_ROUTE],
    [3, "/admin/users/update"],
    [4, "/admin/users/delete"],
];

// Routes ADMIN_MENU_OPTIONS never answers: other sites, a script, a main-menu page, spellings of
// an installed page that are not exactly its route, and a non-string.
const UNEXPECTED_ROUTES: readonly unknown[] = [
    "https://evil.example/admin/users",
    "//evil.example",
    "javascript:alert(1)",
    "/accounts/view",
    "/admin/users/",
    "/admin/Users",
    "/admin/users?userId=TESTUSR1",
    {},
];

// A 200 selection answer carrying a route its TypeScript type rules out, as unchecked JSON can.
function SelectionAnswering(route: unknown): MenuSelectionResponse {
    const answer = { route, message: "" };
    return answer as MenuSelectionResponse;
}

describe("COADM01C XCTL to the installed admin pages", () => {
    it.each(INSTALLED_OPTION_ROUTES)("COADM01C option %d opens %s", async (number, route) => {
        mockSelectAdminMenuOption.mockResolvedValue(BuildRouteResponse(route));
        const user = await RenderLoadedScreen();
        await SubmitOption(user, String(number));
        await waitFor(() => expect(mockRouterPush).toHaveBeenCalledWith(route));
        expect(mockRouterPush).toHaveBeenCalledTimes(1);
        expect(mockSelectAdminMenuOption).toHaveBeenCalledWith({ option: String(number) });
    });
});

describe("COADM01C unexpected selection route", () => {
    it.each(UNEXPECTED_ROUTES)(
        "COADM01C route %p shows the abend text in red and stays on /admin",
        async (route) => {
            mockSelectAdminMenuOption.mockResolvedValue(SelectionAnswering(route));
            const user = await RenderLoadedScreen();
            await SubmitOption(user, "1");
            await ExpectAlert(ABEND_MESSAGE, "error");
            await WaitForIdle();
            expect(mockRouterPush).not.toHaveBeenCalled();
            expect(OptionField()).toHaveFocus();
            expect(OptionField()).toHaveAttribute("aria-invalid", "false");
            expect(screen.getByRole("button", { name: CONTINUE_BUTTON })).toBeEnabled();
        },
    );
});

// A menu read held in flight until the test answers it, inside act, through the returned function.
function HoldMenuRead(): (response: MenuResponse) => Promise<void> {
    let answerRead: (response: MenuResponse) => void = () => undefined;
    mockFetchAdminMenu.mockImplementation(
        () =>
            new Promise<MenuResponse>((resolve) => {
                answerRead = resolve;
            }),
    );
    return async (response) => {
        await act(async () => {
            answerRead(response);
        });
    };
}

// One SEND-MENU-SCREEN paints the whole map, so nothing already shown moves when it arrives.
describe("COADM01C map painted once the menu read answers", () => {
    it("COADM01C paints the map at once when the read answers; OPTION has the cursor", async () => {
        const answerMenuRead = HoldMenuRead();
        RenderScreen();
        expect(screen.getByRole("heading", { level: 1, name: SCREEN_TITLE })).toBeVisible();
        expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "true");
        expect(screen.queryByRole("navigation", { name: OPTION_LIST_LABEL })).toBeNull();
        expect(screen.queryByRole("textbox", { name: OPTION_PROMPT })).toBeNull();
        expect(screen.queryByRole("group", { name: "Function keys" })).toBeNull();
        await answerMenuRead(BuildMenuResponse());
        expect(within(OptionList()).getAllByRole("button")).toHaveLength(LABEL_CASES.length);
        expect(OptionField()).toHaveFocus();
        expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "false");
    });
});

// The two pointer presses that send a selection: the ENTER button and a listed option.
const POINTER_SELECTIONS: readonly [string, KeyPress][] = [
    ["Continue (Enter) button", ClickKey(CONTINUE_BUTTON)],
    ["listed option 5", (user) => user.click(OptionButton(5, TRAN_TYPE_LIST_LABEL))],
];

// The 3270 keyboard locked from send to response: the cursor stays in OPTION, which refuses
// keystrokes until the answer arrives.
describe("COADM01C OPTION locked while a selection is in flight", () => {
    it.each(POINTER_SELECTIONS)(
        "COADM01C a selection sent by the %s keeps the cursor in a read-only OPTION",
        async (_via, press) => {
            const answerSelection = DeferSelection();
            const user = await RenderLoadedScreen();
            await user.type(OptionField(), "5");
            await press(user);
            expect(OptionField()).toHaveFocus();
            expect(OptionField()).toHaveAttribute("readonly");
            expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "true");
            await user.keyboard("6");
            expect(OptionField()).toHaveValue("5");
            answerSelection(NOT_INSTALLED_RESPONSE);
            await WaitForIdle();
            expect(OptionField()).not.toHaveAttribute("readonly");
            expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "false");
        },
    );
});

// The routed page's text once its payload has loaded.
const NEXT_PAGE_TEXT = "User Update page";
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
            <AdminMenuPage />
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
describe("COADM01C busy until the routed page renders", () => {
    it("COADM01C option 3 then ENTER again before the page renders sends one selection", async () => {
        const loadNextPage = HoldNextPage();
        mockSelectAdminMenuOption.mockResolvedValue(BuildRouteResponse("/admin/users/update"));
        const user = RenderScreenBesideNextPage();
        await screen.findByText(USER_LIST_LABEL);
        await user.type(OptionField(), "3");
        await PressKey(user, "Enter");
        await waitFor(() => expect(mockRouterPush).toHaveBeenCalledWith("/admin/users/update"));
        await RunScheduledWork();
        expect(screen.getByRole("progressbar", { name: "Processing" })).toBeInTheDocument();
        expect(screen.getByRole("button", { name: CONTINUE_BUTTON })).toBeDisabled();
        expect(OptionField()).toHaveAttribute("readonly");
        await PressKey(user, "Enter");
        expect(mockSelectAdminMenuOption).toHaveBeenCalledTimes(1);
        await loadNextPage(NEXT_PAGE_TEXT);
        expect(screen.getByText(NEXT_PAGE_TEXT)).toBeInTheDocument();
        await WaitForIdle();
    });
});
