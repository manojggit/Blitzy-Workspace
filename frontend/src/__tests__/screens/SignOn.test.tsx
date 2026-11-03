// Screen tests: BMS COSGN00, program COSGN00C (CC00), page /signon.
// Literals are copied verbatim from backend/app/services/auth/cosgn00c.py (cbl/COSGN00C.cbl
// lines 120, 125, 242, 249 and 254), login_throttle.py and backend/app/core/messages.py, whose
// thank-you text is CSMSG01Y CCDA-MSG-THANK-YOU without its PIC X(50) padding.
import { randomBytes } from "node:crypto";

import { ThemeProvider } from "@mui/material/styles";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { AxiosError, AxiosHeaders, type InternalAxiosRequestConfig } from "axios";
import type { ReactNode } from "react";

import SignOnPage from "@/app/signon/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import type * as AuthModule from "@/lib/api/auth";
import {
    FetchCurrentUser,
    Login,
    Logout,
    type CurrentUserResponse,
    type LoginRequest,
    type LoginResponse,
    type LogoutResponse,
} from "@/lib/api/auth";
import {
    HandleResponseError,
    PENDING_MESSAGE_KEY,
    SIGN_ON_ROUTE,
    StorePendingMessage,
    type LegacyErrorResponse,
    type MessageSeverity,
} from "@/lib/apiClient";
import type { SessionContextValue } from "@/lib/session";

const mockRouterPush = jest.fn();
const mockRouterReplace = jest.fn();
const mockRouter = {
    push: mockRouterPush,
    replace: mockRouterReplace,
    back: jest.fn(),
    prefetch: jest.fn(),
    refresh: jest.fn(),
};
let mockSearchParams = new URLSearchParams();

// The page reads the session only through this context; the mock records its two actions.
const mockSession: SessionContextValue = {
    currentUser: null,
    isLoading: false,
    RefreshSession: jest.fn(),
    ClearSession: jest.fn(),
};

jest.mock("next/navigation", () => ({
    useRouter: () => mockRouter,
    useSearchParams: () => mockSearchParams,
    usePathname: () => "/signon",
}));

jest.mock("@/lib/session", () => ({
    useSession: () => mockSession,
    SessionProvider: ({ children }: { children: ReactNode }) => children,
}));

// The server owns every sign-on edit; apiClient stays real, so the page's feedback mapping runs.
jest.mock("@/lib/api/auth", () => ({
    ...jest.requireActual<typeof AuthModule>("@/lib/api/auth"),
    Login: jest.fn(),
    Logout: jest.fn(),
    FetchCurrentUser: jest.fn(),
}));

const loginMock = jest.mocked(Login);
const logoutMock = jest.mocked(Logout);
const fetchCurrentUserMock = jest.mocked(FetchCurrentUser);
const refreshSessionMock = jest.mocked(mockSession.RefreshSession);
const clearSessionMock = jest.mocked(mockSession.ClearSession);

// COSGN00 map content: the row-5 title, the instruction line, the field labels, the (8 Char)
// hints, the LENGTH=8 fields and the legend ENTER=Sign-on  F3=Exit.
const SCREEN_TITLE = "This is a Credit Card Demo Application for Mainframe Modernization";
const INSTRUCTION_TEXT = "Type your User ID and Password, then press ENTER:";
const TRANSACTION_TEXT = "Tran: CC00";
const PROGRAM_TEXT = "Prog: COSGN00C";
// The BMS INITIAL texts, verbatim: the colon and the inner blanks are part of each label.
const USER_ID_LABEL = "User ID     :";
const PASSWORD_LABEL = "Password    :";
const CREDENTIAL_HELPER_TEXT = "(8 Char)";
const CREDENTIAL_MAX_LENGTH = "8";
const SIGN_ON_BUTTON = "Sign-on (Enter)";
const EXIT_BUTTON = "Exit (F3)";
const FUNCTION_KEY_GROUP = "Function keys";
const BUSY_INDICATOR = "Processing";
// No AID key other than ENTER and PF3 reaches COSGN00C from this page.
const UNBOUND_KEYS: readonly string[] = ["F4", "F5", "F7", "F8", "F12"];

const USER_ID_REQUIRED_MESSAGE = "Please enter User ID ...";
const PASSWORD_REQUIRED_MESSAGE = "Please enter Password ...";
const USER_NOT_FOUND_MESSAGE = "User not found. Try again ...";
const WRONG_PASSWORD_MESSAGE = "Wrong Password. Try again ...";
const UNABLE_TO_VERIFY_MESSAGE = "Unable to verify the User ...";
const LOGIN_LOCKED_MESSAGE = "Too many failed attempts. Try again later ...";
const THANK_YOU_MESSAGE = "Thank you for using CardDemo application...";
// CSMSG02Y ABEND-MSG: a 500 has no legacy body, because the backend has no catch-all handler.
const ABEND_MESSAGE = "UNEXPECTED ABEND OCCURRED.";

// cosgn00c.SIGNON_FAILURE_STATUS, login_throttle.LOCKED_STATUS and
// cosgn00c.UNABLE_TO_VERIFY_STATUS (backend/app/services/auth).
const HTTP_SIGN_ON_FAILURE = 401;
const HTTP_LOGIN_LOCKED = 429;
const HTTP_FILE_ERROR = 503;
const HTTP_INTERNAL_SERVER_ERROR = 500;
const LOGIN_PATH = "/auth/login";
// jsdom reports every window.location.assign through console.error instead of navigating.
const NAVIGATION_NOT_IMPLEMENTED = "Not implemented: navigation";

// Lower case, so the call proves the page sends the user ID as typed: COSGN00C upper-cases it.
const TEST_USER_ID = "testusr1";
// A fresh random 8-character value per run (PASSWD LENGTH=8), so no password is in the test.
const TEST_PASSWORD = randomBytes(4).toString("hex");
const TYPED_CREDENTIALS: Required<LoginRequest> = {
    user_id: TEST_USER_ID,
    password: TEST_PASSWORD,
};

const REGULAR_SIGN_ON: LoginResponse = {
    user_id: "TESTUSR1",
    user_type: "U",
    first_name: "TEST",
    last_name: "USER",
    next_route: "/menu",
};
const ADMIN_SIGN_ON: LoginResponse = {
    ...REGULAR_SIGN_ON,
    user_type: "A",
    last_name: "ADMIN",
    next_route: "/admin",
};
// Only type exactly A is CDEMO-USRTYP-ADMIN; the page follows next_route and never the type.
const LOWER_CASE_TYPE_SIGN_ON: LoginResponse = { ...REGULAR_SIGN_ON, user_type: "a" };
const SIGNED_ON_USER: CurrentUserResponse = { ...REGULAR_SIGN_ON };
const SIGN_OFF_RESPONSE: LogoutResponse = { message: THANK_YOU_MESSAGE };

const ROUTE_CASES: readonly LoginResponse[] = [
    REGULAR_SIGN_ON,
    ADMIN_SIGN_ON,
    LOWER_CASE_TYPE_SIGN_ON,
];

// MUI 9 marks an Alert's colour as MuiAlert-color<Severity>.
const SEVERITY_CLASSES: Readonly<Record<MessageSeverity, string>> = {
    error: "MuiAlert-colorError",
    success: "MuiAlert-colorSuccess",
    info: "MuiAlert-colorInfo",
};

type CredentialField = "user_id" | "password";

const CREDENTIAL_LABELS: Readonly<Record<CredentialField, string>> = {
    user_id: USER_ID_LABEL,
    password: PASSWORD_LABEL,
};
const OTHER_CREDENTIAL: Readonly<Record<CredentialField, CredentialField>> = {
    user_id: "password",
    password: "user_id",
};

interface CredentialFieldCase {
    label: string;
    inputType: string;
}

// USERID is UNPROT; PASSWD is the dark ATTRB=(DRK,FSET,UNPROT) field.
const CREDENTIAL_FIELD_CASES: readonly CredentialFieldCase[] = [
    { label: USER_ID_LABEL, inputType: "text" },
    { label: PASSWORD_LABEL, inputType: "password" },
];

interface SignOnFailureCase {
    message: string;
    status: number;
    cursorField: CredentialField;
    typed: Required<LoginRequest>;
}

// The one failure the 401 interceptor test sends through the real apiClient.
const WRONG_PASSWORD_CASE: SignOnFailureCase = {
    message: WRONG_PASSWORD_MESSAGE,
    status: HTTP_SIGN_ON_FAILURE,
    cursorField: "password",
    typed: TYPED_CREDENTIALS,
};

// Each COSGN00C outcome with its MOVE -1 TO USERIDL / PASSWDL cursor. The blank-field cases
// type nothing into that field: the page sends it blank, and the server edits it.
const SIGN_ON_FAILURE_CASES: readonly SignOnFailureCase[] = [
    {
        message: USER_ID_REQUIRED_MESSAGE,
        status: HTTP_SIGN_ON_FAILURE,
        cursorField: "user_id",
        typed: { user_id: "", password: TEST_PASSWORD },
    },
    {
        message: PASSWORD_REQUIRED_MESSAGE,
        status: HTTP_SIGN_ON_FAILURE,
        cursorField: "password",
        typed: { user_id: TEST_USER_ID, password: "" },
    },
    {
        message: USER_NOT_FOUND_MESSAGE,
        status: HTTP_SIGN_ON_FAILURE,
        cursorField: "user_id",
        typed: TYPED_CREDENTIALS,
    },
    WRONG_PASSWORD_CASE,
    {
        message: UNABLE_TO_VERIFY_MESSAGE,
        status: HTTP_FILE_ERROR,
        cursorField: "user_id",
        typed: TYPED_CREDENTIALS,
    },
    {
        message: LOGIN_LOCKED_MESSAGE,
        status: HTTP_LOGIN_LOCKED,
        cursorField: "user_id",
        typed: TYPED_CREDENTIALS,
    },
];

interface KeyTriggerCase {
    name: string;
    Press: (user: UserEvent) => Promise<void>;
}

const ENTER_TRIGGERS: readonly KeyTriggerCase[] = [
    { name: "the Sign-on (Enter) button", Press: (user) => user.click(SignOnButton()) },
    { name: "the Enter key on the screen", Press: () => PressKey("Enter") },
    { name: "the Enter key in the Password field", Press: (user) => PressEnterInPassword(user) },
];

const EXIT_TRIGGERS: readonly KeyTriggerCase[] = [
    { name: "the Exit (F3) button", Press: (user) => user.click(ExitButton()) },
    { name: "the F3 key", Press: () => PressKey("F3") },
];

let consoleErrorSpy: jest.SpyInstance;

async function RenderScreen(): Promise<{ user: UserEvent }> {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <SignOnPage />
        </ThemeProvider>,
    );
    await screen.findByRole("heading", { level: 1, name: SCREEN_TITLE });
    return { user };
}

// The identity normalizer: the default one collapses the blanks the BMS labels keep.
function KeepRawText(text: string): string {
    return text;
}

function LabelledInput(label: string): HTMLInputElement {
    return screen.getByLabelText<HTMLInputElement>(label, { normalizer: KeepRawText });
}

// The raw text of every label element attached to the input.
function LabelTexts(input: HTMLInputElement): (string | null)[] {
    return Array.from(input.labels ?? [], (labelElement) => labelElement.textContent);
}

function CredentialInput(field: CredentialField): HTMLElement {
    return LabelledInput(CREDENTIAL_LABELS[field]);
}

function SignOnButton(): HTMLElement {
    return screen.getByRole("button", { name: SIGN_ON_BUTTON });
}

function ExitButton(): HTMLElement {
    return screen.getByRole("button", { name: EXIT_BUTTON });
}

// A blank field is left untouched, as on the map.
async function TypeCredentials(user: UserEvent, typed: Required<LoginRequest>): Promise<void> {
    if (typed.user_id !== "") {
        await user.type(CredentialInput("user_id"), typed.user_id);
    }
    if (typed.password !== "") {
        await user.type(CredentialInput("password"), typed.password);
    }
}

// The async act also covers the state updates of the call the key starts, which settle in
// later microtasks.
async function PressKey(key: string): Promise<void> {
    await act(async () => {
        fireEvent.keyDown(window, { key });
    });
}

async function PressEnterInPassword(user: UserEvent): Promise<void> {
    await user.click(CredentialInput("password"));
    await user.keyboard("{Enter}");
}

// A call has settled once the PF keys, disabled while it is in flight, are enabled again.
async function WaitForIdle(): Promise<void> {
    await waitFor(() => {
        expect(SignOnButton()).toBeEnabled();
    });
}

function BuildErrorBody(overrides: Partial<LegacyErrorResponse>): LegacyErrorResponse {
    return { message: "", info_message: "", field_errors: [], cursor_field: "", ...overrides };
}

function BuildAxiosError(status: number, body: unknown): AxiosError {
    const config: InternalAxiosRequestConfig = {
        url: LOGIN_PATH,
        method: "post",
        headers: new AxiosHeaders(),
    };
    const response = { status, statusText: "", headers: {}, config, data: body };
    const message = "Request failed with status code " + status;
    return new AxiosError(message, AxiosError.ERR_BAD_REQUEST, config, null, response);
}

// The body backend ErrorBody builds for a sign-on failure: its one field is flagged and has
// the cursor.
function BuildSignOnFailure(failureCase: SignOnFailureCase): AxiosError {
    const { message, status, cursorField } = failureCase;
    const body = BuildErrorBody({
        message,
        field_errors: [cursorField],
        cursor_field: cursorField,
    });
    return BuildAxiosError(status, body);
}

// Exactly one alert holds the literal, compared raw so that no blank is trimmed or collapsed.
async function ExpectAlert(text: string, severity: MessageSeverity): Promise<void> {
    await waitFor(() => {
        const alerts = screen.getAllByRole("alert");
        const matching = alerts.filter((alert) => alert.textContent === text);
        expect(matching).toHaveLength(1);
        expect(matching[0]).toHaveClass(SEVERITY_CLASSES[severity]);
    });
}

function ExpectCursorOn(cursorField: CredentialField): void {
    expect(CredentialInput(cursorField)).toHaveFocus();
    expect(CredentialInput(cursorField)).toHaveAttribute("aria-invalid", "true");
    expect(CredentialInput(OTHER_CREDENTIAL[cursorField])).toHaveAttribute("aria-invalid", "false");
}

function ExpectNoNavigation(): void {
    expect(mockRouterPush).not.toHaveBeenCalled();
    expect(mockRouterReplace).not.toHaveBeenCalled();
}

function NavigationWasAttempted(): boolean {
    return consoleErrorSpy.mock.calls.some((call: unknown[]) => {
        const firstArgument = call[0];
        const text = firstArgument instanceof Error ? firstArgument.message : String(firstArgument);
        return text.includes(NAVIGATION_NOT_IMPLEMENTED);
    });
}

// The XCTL replacement runs only once the session context has read the new user.
function ExpectSessionRefreshedBeforeNavigation(): void {
    const refreshOrder = refreshSessionMock.mock.invocationCallOrder[0] ?? Infinity;
    const navigationOrder = mockRouterPush.mock.invocationCallOrder[0] ?? -Infinity;
    expect(refreshOrder).toBeLessThan(navigationOrder);
}

// An accepted sign-on keeps the session its login opened: the page reads no user itself, sends no
// logout, and leaves the session context to RefreshSession.
function ExpectLoginSessionKept(): void {
    expect(fetchCurrentUserMock).not.toHaveBeenCalled();
    expect(logoutMock).not.toHaveBeenCalled();
    expect(clearSessionMock).not.toHaveBeenCalled();
}

// The session lives only in the HttpOnly cookie: the page keeps no token, role or password.
function ExpectNothingStored(): void {
    expect(window.localStorage).toHaveLength(0);
    expect(window.sessionStorage).toHaveLength(0);
}

// The returned function settles the pending Login inside act.
function DeferLogin(): (response: LoginResponse) => Promise<void> {
    let resolveLogin: (response: LoginResponse) => void = () => undefined;
    loginMock.mockImplementation(
        () =>
            new Promise<LoginResponse>((resolve) => {
                resolveLogin = resolve;
            }),
    );
    return async (response) => {
        await act(async () => {
            resolveLogin(response);
        });
    };
}

beforeEach(() => {
    mockSearchParams = new URLSearchParams();
    window.sessionStorage.clear();
    window.localStorage.clear();
    window.history.replaceState(null, "", SIGN_ON_ROUTE);
    loginMock.mockResolvedValue(REGULAR_SIGN_ON);
    logoutMock.mockResolvedValue(SIGN_OFF_RESPONSE);
    fetchCurrentUserMock.mockResolvedValue(SIGNED_ON_USER);
    refreshSessionMock.mockResolvedValue(SIGNED_ON_USER);
    consoleErrorSpy = jest.spyOn(console, "error");
});

afterEach(() => {
    consoleErrorSpy.mockRestore();
});

describe("COSGN00C BMS COSGN00 content", () => {
    it("COSGN00 shows Tran CC00, Prog COSGN00C, the title and the instruction line", async () => {
        await RenderScreen();
        const banner = screen.getByRole("banner");
        expect(within(banner).getByText(TRANSACTION_TEXT)).toBeInTheDocument();
        expect(within(banner).getByText(PROGRAM_TEXT)).toBeInTheDocument();
        expect(screen.getByText(INSTRUCTION_TEXT)).toBeInTheDocument();
        expect(screen.getAllByText(CREDENTIAL_HELPER_TEXT)).toHaveLength(2);
    });

    it.each(CREDENTIAL_FIELD_CASES)(
        "COSGN00 $label field has its verbatim label, LENGTH=8 and type $inputType",
        async ({ label, inputType }) => {
            await RenderScreen();
            const input = LabelledInput(label);
            expect(LabelTexts(input)).toEqual([label]);
            expect(input).toHaveAttribute("maxlength", CREDENTIAL_MAX_LENGTH);
            expect(input).toHaveAttribute("type", inputType);
        },
    );
});

describe("COSGN00C BMS COSGN00 legend and cursor", () => {
    it("COSGN00 legend ENTER=Sign-on  F3=Exit is the screen's only PF-key group", async () => {
        await RenderScreen();
        const legend = screen.getByRole("group", { name: FUNCTION_KEY_GROUP });
        const buttonNames = within(legend)
            .getAllByRole("button")
            .map((button) => button.textContent);
        expect(buttonNames).toEqual([SIGN_ON_BUTTON, EXIT_BUTTON]);
    });

    it("COSGN00C puts the cursor on User ID on a fresh screen (USERID IC)", async () => {
        await RenderScreen();
        expect(CredentialInput("user_id")).toHaveFocus();
        expect(screen.queryAllByRole("alert")).toHaveLength(0);
        expect(screen.queryAllByRole("status")).toHaveLength(0);
    });
});

describe("COSGN00C ENTER (PROCESS-ENTER-KEY) on /signon", () => {
    it.each(ENTER_TRIGGERS)(
        "COSGN00C ENTER by $name sends the raw typed user ID and password once",
        async ({ Press }) => {
            const { user } = await RenderScreen();
            await TypeCredentials(user, TYPED_CREDENTIALS);
            await Press(user);
            await waitFor(() => {
                expect(mockRouterPush).toHaveBeenCalledWith(REGULAR_SIGN_ON.next_route);
            });
            await WaitForIdle();
            expect(loginMock).toHaveBeenCalledTimes(1);
            expect(loginMock).toHaveBeenCalledWith(TYPED_CREDENTIALS);
            ExpectNothingStored();
        },
    );
});

describe("COSGN00C XCTL to COADM01C or COMEN01C on /signon", () => {
    it.each(ROUTE_CASES)(
        "COSGN00C user type $user_type opens the server's next_route $next_route",
        async (response) => {
            loginMock.mockResolvedValue(response);
            const { user } = await RenderScreen();
            await TypeCredentials(user, TYPED_CREDENTIALS);
            await user.click(SignOnButton());
            await waitFor(() => {
                expect(mockRouterPush).toHaveBeenCalledWith(response.next_route);
            });
            await WaitForIdle();
            expect(mockRouterPush).toHaveBeenCalledTimes(1);
            expect(refreshSessionMock).toHaveBeenCalledTimes(1);
            ExpectSessionRefreshedBeforeNavigation();
            expect(mockRouterReplace).not.toHaveBeenCalled();
            ExpectLoginSessionKept();
        },
    );
});

describe("COSGN00C ENTER while the sign-on call is in flight on /signon", () => {
    it("COSGN00C disables both PF keys and sends ENTER only once", async () => {
        const resolveLogin = DeferLogin();
        const { user } = await RenderScreen();
        await TypeCredentials(user, TYPED_CREDENTIALS);
        await user.click(SignOnButton());
        expect(SignOnButton()).toBeDisabled();
        expect(ExitButton()).toBeDisabled();
        expect(screen.getByRole("progressbar", { name: BUSY_INDICATOR })).toBeInTheDocument();
        await PressKey("Enter");
        await PressKey("F3");
        expect(loginMock).toHaveBeenCalledTimes(1);
        expect(logoutMock).not.toHaveBeenCalled();
        await resolveLogin(REGULAR_SIGN_ON);
        await WaitForIdle();
        expect(mockRouterPush).toHaveBeenCalledWith(REGULAR_SIGN_ON.next_route);
    });
});

describe("COSGN00C sign-on failure literals on /signon", () => {
    it.each(SIGN_ON_FAILURE_CASES)(
        "COSGN00C ENTER shows $message ($status) in red with the cursor on $cursorField",
        async (failureCase) => {
            loginMock.mockRejectedValue(BuildSignOnFailure(failureCase));
            const { user } = await RenderScreen();
            await TypeCredentials(user, failureCase.typed);
            await user.click(SignOnButton());
            await ExpectAlert(failureCase.message, "error");
            await WaitForIdle();
            expect(loginMock).toHaveBeenCalledWith(failureCase.typed);
            ExpectCursorOn(failureCase.cursorField);
            expect(CredentialInput("password")).toHaveValue("");
            ExpectNoNavigation();
        },
    );
});

describe("COSGN00C abend on /signon", () => {
    it("COSGN00C ENTER shows the abend text in red for a 500 without a legacy body", async () => {
        const fastApiBody = { detail: "Internal Server Error" };
        loginMock.mockRejectedValue(BuildAxiosError(HTTP_INTERNAL_SERVER_ERROR, fastApiBody));
        const { user } = await RenderScreen();
        await TypeCredentials(user, TYPED_CREDENTIALS);
        await user.click(SignOnButton());
        await ExpectAlert(ABEND_MESSAGE, "error");
        await WaitForIdle();
        ExpectNoNavigation();
    });
});

describe("COSGN00C 401 through the real apiClient interceptor on /signon", () => {
    it("COSGN00C 401 shows its sign-on literal and never redirects to /signon", async () => {
        const unauthorized = BuildSignOnFailure(WRONG_PASSWORD_CASE);
        loginMock.mockImplementation(() => HandleResponseError(unauthorized));
        const { user } = await RenderScreen();
        await TypeCredentials(user, TYPED_CREDENTIALS);
        await user.click(SignOnButton());
        await ExpectAlert(WRONG_PASSWORD_MESSAGE, "error");
        await WaitForIdle();
        expect(SIGN_ON_ROUTE).toBe("/signon");
        expect(window.location.pathname).toBe(SIGN_ON_ROUTE);
        expect(NavigationWasAttempted()).toBe(false);
        ExpectNoNavigation();
    });
});

describe("COSGN00C PF3 sign-off (SEND-PLAIN-TEXT) on /signon", () => {
    it.each(EXIT_TRIGGERS)(
        "COSGN00C F3 by $name signs off with the CSMSG01Y thank-you literal",
        async ({ Press }) => {
            const { user } = await RenderScreen();
            await TypeCredentials(user, TYPED_CREDENTIALS);
            await Press(user);
            await ExpectAlert(THANK_YOU_MESSAGE, "info");
            await WaitForIdle();
            expect(logoutMock).toHaveBeenCalledTimes(1);
            expect(clearSessionMock).toHaveBeenCalledTimes(1);
            expect(loginMock).not.toHaveBeenCalled();
            expect(CredentialInput("user_id")).toHaveValue("");
            expect(CredentialInput("user_id")).toHaveFocus();
            ExpectNoNavigation();
        },
    );
});

describe("COSGN00C keys outside the COSGN00 legend on /signon", () => {
    it("COSGN00C binds no PF key beyond ENTER=Sign-on  F3=Exit", async () => {
        await RenderScreen();
        for (const key of UNBOUND_KEYS) {
            await PressKey(key);
        }
        expect(loginMock).not.toHaveBeenCalled();
        expect(logoutMock).not.toHaveBeenCalled();
        expect(screen.queryAllByRole("alert")).toHaveLength(0);
        expect(screen.queryAllByRole("status")).toHaveLength(0);
    });
});

describe("COSGN00C field errors and cursor on /signon", () => {
    it("COSGN00C flags every field_errors field and focuses cursor_field", async () => {
        const body = BuildErrorBody({
            message: USER_NOT_FOUND_MESSAGE,
            field_errors: ["user_id", "password"],
            cursor_field: "user_id",
        });
        loginMock.mockRejectedValue(BuildAxiosError(HTTP_SIGN_ON_FAILURE, body));
        const { user } = await RenderScreen();
        await TypeCredentials(user, TYPED_CREDENTIALS);
        await user.click(SignOnButton());
        await ExpectAlert(USER_NOT_FOUND_MESSAGE, "error");
        await WaitForIdle();
        expect(CredentialInput("user_id")).toHaveAttribute("aria-invalid", "true");
        expect(CredentialInput("password")).toHaveAttribute("aria-invalid", "true");
        expect(CredentialInput("user_id")).toHaveFocus();
    });
});

describe("COSGN00C field error description on /signon", () => {
    it("COSGN00C describes the flagged field by the ERRMSG literal, then its hint", async () => {
        loginMock.mockRejectedValue(BuildSignOnFailure(WRONG_PASSWORD_CASE));
        const { user } = await RenderScreen();
        await TypeCredentials(user, WRONG_PASSWORD_CASE.typed);
        await user.click(SignOnButton());
        await ExpectAlert(WRONG_PASSWORD_MESSAGE, "error");
        expect(CredentialInput("password")).toHaveAccessibleDescription(
            `${WRONG_PASSWORD_MESSAGE} ${CREDENTIAL_HELPER_TEXT}`,
        );
        expect(CredentialInput("user_id")).toHaveAccessibleDescription(CREDENTIAL_HELPER_TEXT);
    });
});

describe("COSGN00C pending sign-off message on /signon", () => {
    it("COSGN00 shows a menu's stored F3 thank-you literal once, as information", async () => {
        StorePendingMessage(THANK_YOU_MESSAGE);
        await RenderScreen();
        await ExpectAlert(THANK_YOU_MESSAGE, "info");
        expect(window.sessionStorage.getItem(PENDING_MESSAGE_KEY)).toBeNull();
        expect(loginMock).not.toHaveBeenCalled();
    });
});

// next_route values the backend's Literal["/admin", "/menu"] never sends: other sites, a script,
// other pages and spellings of a home route that are not exactly it, and a missing route.
const UNEXPECTED_NEXT_ROUTES: readonly unknown[] = [
    "https://evil.example/menu",
    "//evil.example",
    "javascript:alert(1)",
    "/admin/users",
    "/menu/",
    "/ADMIN",
    "/menu?next=//evil.example",
    " /menu",
    null,
];

// A 200 sign-on carrying a route its TypeScript type rules out, as unchecked JSON can.
function SignOnAnswering(nextRoute: unknown): LoginResponse {
    const answer = { ...REGULAR_SIGN_ON, next_route: nextRoute };
    return answer as LoginResponse;
}

const LOGOUT_PATH = "/auth/logout";

// A logout that never reached the server: an axios error with no response.
function BuildLogoutNetworkError(): AxiosError {
    const config: InternalAxiosRequestConfig = {
        url: LOGOUT_PATH,
        method: "post",
        headers: new AxiosHeaders(),
    };
    return new AxiosError("Network Error", AxiosError.ERR_NETWORK, config);
}

// The refused screen: no navigation, PASSWD empty, the cursor on USERID. The logout of the session
// the login opened is sent once, after the login, and its thank-you literal never shows.
function ExpectSignOnRefused(): void {
    ExpectNoNavigation();
    expect(CredentialInput("password")).toHaveValue("");
    expect(CredentialInput("user_id")).toHaveValue(TEST_USER_ID);
    expect(CredentialInput("user_id")).toHaveFocus();
    expect(logoutMock).toHaveBeenCalledTimes(1);
    const loginOrder = loginMock.mock.invocationCallOrder[0] ?? Infinity;
    const logoutOrder = logoutMock.mock.invocationCallOrder[0] ?? -Infinity;
    expect(loginOrder).toBeLessThan(logoutOrder);
    expect(screen.queryByText(THANK_YOU_MESSAGE)).not.toBeInTheDocument();
}

// A confirmed logout ends the session: the context forgets any earlier user and reads none.
function ExpectRefusedSessionLoggedOut(): void {
    expect(clearSessionMock).toHaveBeenCalledTimes(1);
    expect(refreshSessionMock).not.toHaveBeenCalled();
}

// An unconfirmed logout may leave the login's session live: the context is not cleared, and it
// reads the session back once, after the failed logout.
function ExpectRefusedSessionRecovered(): void {
    expect(clearSessionMock).not.toHaveBeenCalled();
    expect(refreshSessionMock).toHaveBeenCalledTimes(1);
    const logoutOrder = logoutMock.mock.invocationCallOrder[0] ?? Infinity;
    const refreshOrder = refreshSessionMock.mock.invocationCallOrder[0] ?? -Infinity;
    expect(logoutOrder).toBeLessThan(refreshOrder);
}

// A refused sign-on whose logout never reaches the server; the next logout succeeds.
async function RefuseSignOnWithFailedLogout(): Promise<void> {
    loginMock.mockResolvedValue(SignOnAnswering(UNEXPECTED_NEXT_ROUTES[0]));
    logoutMock.mockRejectedValueOnce(BuildLogoutNetworkError());
    const { user } = await RenderScreen();
    await TypeCredentials(user, TYPED_CREDENTIALS);
    await user.click(SignOnButton());
    await ExpectAlert(ABEND_MESSAGE, "error");
    await WaitForIdle();
}

describe("COSGN00C unexpected next_route on /signon", () => {
    it.each(UNEXPECTED_NEXT_ROUTES)(
        "COSGN00C next_route %p ends the login's session, shows the abend text, never navigates",
        async (nextRoute) => {
            loginMock.mockResolvedValue(SignOnAnswering(nextRoute));
            const { user } = await RenderScreen();
            await TypeCredentials(user, TYPED_CREDENTIALS);
            await user.click(SignOnButton());
            await ExpectAlert(ABEND_MESSAGE, "error");
            await WaitForIdle();
            ExpectSignOnRefused();
            ExpectRefusedSessionLoggedOut();
        },
    );
});

describe("COSGN00C unexpected next_route with a failed logout on /signon", () => {
    it("COSGN00C keeps the unconfirmed session, rereads it and shows the abend text", async () => {
        await RefuseSignOnWithFailedLogout();
        ExpectSignOnRefused();
        ExpectRefusedSessionRecovered();
    });

    it("COSGN00C F3 retries the sign-off and clears the session once it succeeds", async () => {
        await RefuseSignOnWithFailedLogout();
        await PressKey("F3");
        await ExpectAlert(THANK_YOU_MESSAGE, "info");
        await WaitForIdle();
        expect(logoutMock).toHaveBeenCalledTimes(2);
        expect(clearSessionMock).toHaveBeenCalledTimes(1);
        expect(refreshSessionMock).toHaveBeenCalledTimes(1);
        expect(screen.queryByText(ABEND_MESSAGE)).not.toBeInTheDocument();
        expect(CredentialInput("user_id")).toHaveValue("");
        ExpectNoNavigation();
    });
});

// FastAPI's own text answer to an unhandled exception: not the legacy error model.
const SERVER_ERROR_BODY = "Internal Server Error";

interface SignOffFailureCase {
    cause: string;
    error: AxiosError;
    message: string;
}

// The logout answered 500 by FastAPI itself, with no legacy body.
function BuildLogoutServerError(): AxiosError {
    const config: InternalAxiosRequestConfig = {
        url: LOGOUT_PATH,
        method: "post",
        headers: new AxiosHeaders(),
    };
    const status = HTTP_INTERNAL_SERVER_ERROR;
    const response = { status, statusText: "", headers: {}, config, data: SERVER_ERROR_BODY };
    const message = "Request failed with status code " + status;
    return new AxiosError(message, AxiosError.ERR_BAD_RESPONSE, config, null, response);
}

// Ordinary F3 sign-offs the server never confirmed, each with the message line it shows.
const SIGN_OFF_FAILURE_CASES: readonly SignOffFailureCase[] = [
    { cause: "a network failure", error: BuildLogoutNetworkError(), message: ABEND_MESSAGE },
    {
        cause: "a 500 without a legacy body",
        error: BuildLogoutServerError(),
        message: ABEND_MESSAGE,
    },
];

// An unconfirmed sign-off keeps the session and both typed credentials, shows no thank-you
// notice, stays on /signon and unlocks both PF keys.
function ExpectSignOffUnconfirmed(): void {
    expect(logoutMock).toHaveBeenCalledTimes(1);
    expect(clearSessionMock).not.toHaveBeenCalled();
    expect(loginMock).not.toHaveBeenCalled();
    expect(screen.queryByText(THANK_YOU_MESSAGE)).not.toBeInTheDocument();
    expect(CredentialInput("user_id")).toHaveValue(TEST_USER_ID);
    expect(CredentialInput("password")).toHaveValue(TYPED_CREDENTIALS.password);
    expect(ExitButton()).toBeEnabled();
    expect(SignOnButton()).toBeEnabled();
    ExpectNoNavigation();
}

describe("COSGN00C PF3 sign-off failure on /signon", () => {
    it.each(SIGN_OFF_FAILURE_CASES)(
        "COSGN00C F3 after $cause keeps the session and shows $message in red",
        async ({ error, message }) => {
            logoutMock.mockRejectedValue(error);
            const { user } = await RenderScreen();
            await TypeCredentials(user, TYPED_CREDENTIALS);
            await user.click(ExitButton());
            await ExpectAlert(message, "error");
            await WaitForIdle();
            ExpectSignOffUnconfirmed();
        },
    );
});

describe("COSGN00C PF3 sign-off retry on /signon", () => {
    it("COSGN00C F3 again after a failed sign-off shows the thank-you literal", async () => {
        logoutMock.mockRejectedValueOnce(BuildLogoutNetworkError());
        const { user } = await RenderScreen();
        await TypeCredentials(user, TYPED_CREDENTIALS);
        await PressKey("F3");
        await ExpectAlert(ABEND_MESSAGE, "error");
        await WaitForIdle();
        expect(clearSessionMock).not.toHaveBeenCalled();
        await PressKey("F3");
        await ExpectAlert(THANK_YOU_MESSAGE, "info");
        await WaitForIdle();
        expect(logoutMock).toHaveBeenCalledTimes(2);
        expect(clearSessionMock).toHaveBeenCalledTimes(1);
        expect(screen.queryByText(ABEND_MESSAGE)).not.toBeInTheDocument();
        expect(CredentialInput("user_id")).toHaveValue("");
        expect(CredentialInput("password")).toHaveValue("");
        ExpectNoNavigation();
    });
});
