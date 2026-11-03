// Screen tests: BMS COADM01, program COADM01C (CA00), page /admin.
// Copybooks COADM02Y (the six admin options) and CSMSG01Y (the sign-off literal). The literals are
// copied verbatim from backend/app/services/menu/coadm01c.py, menu_options.py and core/messages.py.
import { ThemeProvider } from "@mui/material/styles";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { AxiosError, AxiosHeaders, type InternalAxiosRequestConfig } from "axios";
import type { ReactNode } from "react";

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
    TakePendingMessage,
    type LegacyErrorResponse,
    type MessageSeverity,
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

    it("COADM01C F3 still signs off when the sign-off call fails", async () => {
        mockLogout.mockRejectedValue(BuildAxiosError(500, SERVER_ERROR_BODY));
        const user = await RenderLoadedScreen();
        await PressKey(user, "F3");
        await waitFor(() => expect(mockRouterPush).toHaveBeenCalledWith(SIGN_ON_ROUTE));
        expect(mockClearSession).toHaveBeenCalledTimes(1);
        expect(TakePendingMessage()).toBe("");
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
