// Screen tests: BMS COUSR02, program COUSR02C (CU02), page /admin/users/update.
// Sources: bms/COUSR02.bms and cpy-bms/COUSR02.CPY (fields, lengths, PF legend), cbl/COUSR02C.cbl
// (EIBAID dispatch lines 111-126, UPDATE-USER-INFO lines 198-240, READ and REWRITE outcomes
// lines 336-386) and cpy/CSUSR01Y.cpy. The literals are copied verbatim from
// backend/app/services/users/cousr02c.py; the user API module is mocked, so every server outcome
// is a resolved or a rejected call.
import { randomBytes } from "node:crypto";

import { ThemeProvider } from "@mui/material/styles";
import {
    act,
    fireEvent,
    render,
    screen,
    waitFor,
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

import AdminUsersUpdatePage from "@/app/admin/users/update/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import {
    SIGN_ON_ROUTE,
    type LegacyErrorResponse,
    type MessageSeverity,
    type ReadOptions,
} from "@/lib/apiClient";
import {
    FetchUser,
    UpdateUser,
    UserFetchScreen,
    type UserDetailResponse,
    type UserMessageResponse,
    type UserUpdateRequest,
} from "@/lib/api/users";

const mockRouterPush = jest.fn();
const mockRouterReplace = jest.fn();
// One router object for every render, as the App Router hands each render the same instance.
const mockRouter = {
    push: mockRouterPush,
    replace: mockRouterReplace,
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};
// CDEMO-CU02-USR-SELECTED: the user COUSR00C selected arrives as the userId query parameter.
let mockSearchParams = new URLSearchParams();

jest.mock("next/navigation", () => ({
    useRouter: () => mockRouter,
    useSearchParams: () => mockSearchParams,
    usePathname: () => "/admin/users/update",
}));

// ScreenShell reads the signed-on user; COSGN00C sends only user type A to the admin screens.
jest.mock("@/lib/session", () => ({
    useSession: () => ({
        currentUser: {
            user_id: "TESTADM1",
            user_type: "A",
            first_name: "Test",
            last_name: "Admin",
            next_route: "/admin",
        },
        isLoading: false,
        RefreshSession: jest.fn(),
        ClearSession: jest.fn(),
    }),
}));

// The real UserFetchScreen enum is kept; every endpoint function is a mock the test arranges.
jest.mock("@/lib/api/users", () => ({
    ...jest.requireActual<typeof import("@/lib/api/users")>("@/lib/api/users"),
    ListUsers: jest.fn(),
    SelectUsers: jest.fn(),
    AddUser: jest.fn(),
    FetchUser: jest.fn(),
    UpdateUser: jest.fn(),
    DeleteUser: jest.fn(),
}));

const mockFetchUser = jest.mocked(FetchUser);
const mockUpdateUser = jest.mocked(UpdateUser);

interface ScreenView {
    user: UserEvent;
    Unmount: () => void;
    // Renders the page again under the current mockSearchParams, as the App Router does on a
    // same-route push or a history step that changes the query.
    Rerender: () => void;
}

// A fetch left in flight when the screen unmounts: the first-entry read or an ENTER read.
interface PendingFetchCase {
    origin: string;
    StartFetch: () => Promise<ScreenView>;
}

// A PF key of the COUSR02 legend: its KeyboardEvent.key and its button's accessible name.
interface ScreenAction {
    keyName: string;
    buttonName: string;
}

interface CallRecorder {
    mock: { invocationCallOrder: number[] };
}

type TriggerMode = "click" | "key";

// [literal, HTTP status, message-line severity]; HTTP 200 is a resolved call.
type LiteralCase = [string, number, MessageSeverity];

const HTTP_OK = 200;
const HTTP_BAD_REQUEST = 400;
const HTTP_UNAUTHORIZED = 401;
const HTTP_NOT_FOUND = 404;
const HTTP_FILE_ERROR = 503;

const TEST_USER_ID = "TESTUSR1";
// Lower case on purpose: COUSR02C uses USRIDIN exactly as keyed, never upper-cased.
const TYPED_USER_ID = "tstusr02";
const FETCHED_FIRST_NAME = "Test";
const FETCHED_LAST_NAME = "User";
const FETCHED_USER_TYPE = "U";
const EDITED_LAST_NAME = "van Dyke";
// The last name the newer of two overlapping fetches answers with.
const NEWEST_LAST_NAME = "Newest";
// A fresh random 8-character value per run (PASSWD LENGTH=8), so no password is in the test.
const NEW_PASSWORD = randomBytes(4).toString("hex");
const BLANK_USER_ID = " ";
// A second user COUSR00C can select, with details unlike the first user's.
const OTHER_USER_ID = "TESTUSR2";
const OTHER_FIRST_NAME = "Other";
const OTHER_LAST_NAME = "Person";

// COUSR02C literals, verbatim.
const PRESS_PF5_MESSAGE = "Press PF5 key to save your updates ...";
const NO_CHANGE_MESSAGE = "Please modify to update ...";
const UPDATED_MESSAGE = "User TESTUSR1 has been updated ...";
const OTHER_UPDATED_MESSAGE = "User TESTUSR2 has been updated ...";
const USER_ID_EMPTY_MESSAGE = "User ID can NOT be empty...";
const FIRST_NAME_EMPTY_MESSAGE = "First Name can NOT be empty...";
const LAST_NAME_EMPTY_MESSAGE = "Last Name can NOT be empty...";
const USER_TYPE_EMPTY_MESSAGE = "User Type can NOT be empty...";
const USER_NOT_FOUND_MESSAGE = "User ID NOT found...";
const UNABLE_TO_LOOKUP_MESSAGE = "Unable to lookup User...";
const UNABLE_TO_UPDATE_MESSAGE = "Unable to Update User...";

// F3 returns to CDEMO-FROM-PROGRAM (COUSR00C) or else COADM01C; F12 always to COADM01C.
const USER_LIST_ROUTE = "/admin/users";
const ADMIN_MENU_ROUTE = "/admin";

// The BMS INITIAL labels of USRIDIN, FNAME, LNAME, PASSWD and USRTYPE.
const USER_ID_LABEL = "Enter User ID:";
const FIRST_NAME_LABEL = "First Name:";
const LAST_NAME_LABEL = "Last Name:";
const PASSWORD_LABEL = "Password:";
const USER_TYPE_LABEL = "User Type:";

// [label, BMS LENGTH] of every unprotected COUSR2A field.
const FIELD_LENGTH_CASES: readonly [string, number][] = [
    [USER_ID_LABEL, 8],
    [FIRST_NAME_LABEL, 20],
    [LAST_NAME_LABEL, 20],
    [PASSWORD_LABEL, 8],
    [USER_TYPE_LABEL, 1],
];
const ALL_FIELD_LABELS: readonly string[] = FIELD_LENGTH_CASES.map(([label]) => label);

// The legend ENTER=Fetch  F3=Save&&Exit  F4=Clear  F5=Save  F12=Cancel; && is the BMS escape of &.
const FETCH_ACTION: ScreenAction = { keyName: "Enter", buttonName: "Fetch (Enter)" };
const SAVE_AND_EXIT_ACTION: ScreenAction = { keyName: "F3", buttonName: "Save&Exit (F3)" };
const CLEAR_ACTION: ScreenAction = { keyName: "F4", buttonName: "Clear (F4)" };
const SAVE_ACTION: ScreenAction = { keyName: "F5", buttonName: "Save (F5)" };
const CANCEL_ACTION: ScreenAction = { keyName: "F12", buttonName: "Cancel (F12)" };
const LEGEND_ACTIONS: readonly ScreenAction[] = [
    FETCH_ACTION,
    SAVE_AND_EXIT_ACTION,
    CLEAR_ACTION,
    SAVE_ACTION,
    CANCEL_ACTION,
];

// Every PF action is exercised through its on-screen button and through its physical key.
const TRIGGER_MODES: readonly TriggerMode[] = ["click", "key"];

// Every fetch carries the signal of the AbortController that owns it; a save never does.
const SIGNALLED_READ_OPTIONS = expect.objectContaining({ signal: expect.any(AbortSignal) });

// MUI 9 marks an Alert's colour as MuiAlert-color<Severity> (Alert/alertClasses.d.ts).
const SEVERITY_CLASSES: Readonly<Record<MessageSeverity, string>> = {
    error: "MuiAlert-colorError",
    success: "MuiAlert-colorSuccess",
    info: "MuiAlert-colorInfo",
};

// READ-USER-SEC-FILE: NORMAL in the neutral colour, NOTFND and WHEN OTHER in red.
const FETCH_LITERAL_CASES: readonly LiteralCase[] = [
    [PRESS_PF5_MESSAGE, HTTP_OK, "info"],
    [USER_NOT_FOUND_MESSAGE, HTTP_NOT_FOUND, "error"],
    [UNABLE_TO_LOOKUP_MESSAGE, HTTP_FILE_ERROR, "error"],
];

// UPDATE-USER-INFO and UPDATE-USER-SEC-FILE: the REWRITE in green, every refusal in red.
const SAVE_LITERAL_CASES: readonly LiteralCase[] = [
    [UPDATED_MESSAGE, HTTP_OK, "success"],
    [FIRST_NAME_EMPTY_MESSAGE, HTTP_BAD_REQUEST, "error"],
    [NO_CHANGE_MESSAGE, HTTP_BAD_REQUEST, "error"],
    [USER_NOT_FOUND_MESSAGE, HTTP_NOT_FOUND, "error"],
    [UNABLE_TO_UPDATE_MESSAGE, HTTP_FILE_ERROR, "error"],
];

// The fetched record with a new last name and a new password, both sent exactly as typed.
const EDITED_REQUEST: UserUpdateRequest = {
    first_name: FETCHED_FIRST_NAME,
    last_name: EDITED_LAST_NAME,
    password: NEW_PASSWORD,
    user_type: FETCHED_USER_TYPE,
};

// The fetched record untouched: the never-prefilled password travels blank, keeping the hash.
const UNEDITED_REQUEST: UserUpdateRequest = {
    first_name: FETCHED_FIRST_NAME,
    last_name: FETCHED_LAST_NAME,
    password: "",
    user_type: FETCHED_USER_TYPE,
};

// The fetched record with only the Last Name cleared: FNAME and USRTYPE pass their checks.
const LAST_NAME_CLEARED_REQUEST: UserUpdateRequest = { ...UNEDITED_REQUEST, last_name: "" };

// A screen with nothing keyed sends every COUSR2AI field blank.
const BLANK_REQUEST: UserUpdateRequest = {
    first_name: "",
    last_name: "",
    password: "",
    user_type: "",
};

// The second user's record as fetched, and saved untouched.
const OTHER_USER_DETAIL: Partial<UserDetailResponse> = {
    user_id: OTHER_USER_ID,
    first_name: OTHER_FIRST_NAME,
    last_name: OTHER_LAST_NAME,
};
const OTHER_UNEDITED_REQUEST: UserUpdateRequest = {
    ...UNEDITED_REQUEST,
    first_name: OTHER_FIRST_NAME,
    last_name: OTHER_LAST_NAME,
};

// UserDetailResponse carries no password or hash, so neither does the fixture.
function BuildUserDetail(overrides: Partial<UserDetailResponse> = {}): UserDetailResponse {
    return {
        user_id: TEST_USER_ID,
        first_name: FETCHED_FIRST_NAME,
        last_name: FETCHED_LAST_NAME,
        user_type: FETCHED_USER_TYPE,
        message: PRESS_PF5_MESSAGE,
        ...overrides,
    };
}

// A successful REWRITE: the message and the cursor on FNAME, as UPDATE-USER-INFO leaves them.
function BuildSaveResponse(message: string): UserMessageResponse {
    return { message, cursor_field: "first_name" };
}

// The common.LegacyErrorResponse body, every key present as the backend serialises it.
function BuildErrorBody(overrides: Partial<LegacyErrorResponse> = {}): LegacyErrorResponse {
    return { message: "", info_message: "", field_errors: [], cursor_field: null, ...overrides };
}

function BuildAxiosError(status: number, body: LegacyErrorResponse): AxiosError {
    const config: InternalAxiosRequestConfig = { headers: new AxiosHeaders() };
    const response: AxiosResponse<LegacyErrorResponse> = {
        status,
        statusText: "",
        headers: {},
        config,
        data: body,
    };
    return new AxiosError(
        "Request failed",
        AxiosError.ERR_BAD_REQUEST,
        config,
        undefined,
        response,
    );
}

function LegacyRejection(literal: string, status: number): AxiosError {
    return BuildAxiosError(status, BuildErrorBody({ message: literal }));
}

function ArrangeFetchOutcome(literal: string, status: number): void {
    if (status === HTTP_OK) {
        mockFetchUser.mockResolvedValue(BuildUserDetail({ message: literal }));
        return;
    }
    mockFetchUser.mockRejectedValue(LegacyRejection(literal, status));
}

function ArrangeSaveOutcome(literal: string, status: number): void {
    if (status === HTTP_OK) {
        mockUpdateUser.mockResolvedValue(BuildSaveResponse(literal));
        return;
    }
    mockUpdateUser.mockRejectedValue(LegacyRejection(literal, status));
}

// Like axios, a read whose signal is aborted rejects with CanceledError; until then it is pending.
function ReadUntilAborted(
    userId: string,
    fetchScreen: UserFetchScreen,
    options?: ReadOptions,
): Promise<UserDetailResponse> {
    return new Promise((_resolve, reject) => {
        options?.signal?.addEventListener("abort", () => {
            reject(new CanceledError());
        });
    });
}

// The next fetch stays pending until the test resolves it through the returned function.
function DeferNextFetch(): (detail: UserDetailResponse) => void {
    let resolveFetch: (detail: UserDetailResponse) => void = () => undefined;
    mockFetchUser.mockImplementationOnce(
        () =>
            new Promise((resolve) => {
                resolveFetch = resolve;
            }),
    );
    return (detail) => resolveFetch(detail);
}

// The signal the page passed to the given FetchUser call.
function FetchSignal(callIndex: number): AbortSignal | undefined {
    return mockFetchUser.mock.calls[callIndex]?.[2]?.signal;
}

// Some fetch read the User ID exactly as given, through a signal of its own.
function ExpectFetchOf(userIdText: string): void {
    expect(mockFetchUser).toHaveBeenCalledWith(
        userIdText,
        UserFetchScreen.UPDATE,
        SIGNALLED_READ_OPTIONS,
    );
}

// The latest fetch read the User ID exactly as given, through a signal of its own.
function ExpectLastFetchOf(userIdText: string): void {
    expect(mockFetchUser).toHaveBeenLastCalledWith(
        userIdText,
        UserFetchScreen.UPDATE,
        SIGNALLED_READ_OPTIONS,
    );
}

function ScreenTree() {
    return (
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <AdminUsersUpdatePage />
        </ThemeProvider>
    );
}

function RenderScreen(renderOptions: RenderOptions = {}): ScreenView {
    const user = userEvent.setup();
    const { rerender, unmount } = render(<ScreenTree />, renderOptions);
    return { user, Unmount: unmount, Rerender: () => rerender(<ScreenTree />) };
}

// A same-route push or history step to the given userId query.
function NavigateToUser(view: ScreenView, userId: string): void {
    mockSearchParams = new URLSearchParams({ userId });
    view.Rerender();
}

// The screen is idle once the LinearProgress shown while a call runs is gone.
async function WaitForIdle(): Promise<void> {
    await waitFor(() => {
        expect(screen.queryByRole("progressbar")).toBeNull();
    });
}

// Opened from COUSR00 with a selected user, which the first entry fetches at once.
async function RenderFromList(): Promise<ScreenView> {
    mockSearchParams = new URLSearchParams({ userId: TEST_USER_ID });
    mockFetchUser.mockResolvedValue(BuildUserDetail({ message: PRESS_PF5_MESSAGE }));
    const view = RenderScreen();
    await screen.findByDisplayValue(FETCHED_LAST_NAME);
    await WaitForIdle();
    return view;
}

// COUSR00C selects the second user while this screen is shown, on the same route.
async function SwitchToOtherUser(view: ScreenView): Promise<void> {
    mockFetchUser.mockResolvedValue(BuildUserDetail(OTHER_USER_DETAIL));
    NavigateToUser(view, OTHER_USER_ID);
    await screen.findByDisplayValue(OTHER_LAST_NAME);
    await WaitForIdle();
}

// The identity normalizer: the default one trims and collapses blanks a literal keeps.
function KeepRawText(text: string): string {
    return text;
}

// The message-line Alert holding exactly the literal, in the colour of the given severity.
async function ExpectAlert(text: string, severity: MessageSeverity): Promise<void> {
    const textElement = await screen.findByText(text, { normalizer: KeepRawText });
    const alert = textElement.closest<HTMLElement>('[role="alert"]');
    expect(alert).toHaveClass(SEVERITY_CLASSES[severity]);
    expect(alert?.textContent).toBe(text);
}

// The apiClient interceptor owns the 401 redirect, so the page neither routes nor shows text.
function ExpectInterceptorOwnedSignOn(): void {
    expect(screen.queryByRole("alert")).toBeNull();
    expect(mockRouterPush).not.toHaveBeenCalled();
    expect(mockRouterReplace).not.toHaveBeenCalled();
    expect(SIGN_ON_ROUTE).toBe("/signon");
}

async function PressKey(user: UserEvent, keyName: string): Promise<void> {
    await user.keyboard(`{${keyName}}`);
}

async function Trigger(view: ScreenView, action: ScreenAction, mode: TriggerMode): Promise<void> {
    if (mode === "click") {
        await view.user.click(screen.getByRole("button", { name: action.buttonName }));
        return;
    }
    await PressKey(view.user, action.keyName);
}

function FieldInput(label: string): HTMLInputElement {
    return screen.getByLabelText<HTMLInputElement>(label);
}

// The field with the given label is marked in error, and no other COUSR2A input is.
function ExpectOnlyFieldInvalid(invalidLabel: string): void {
    for (const label of ALL_FIELD_LABELS) {
        const expectedInvalid = String(label === invalidLabel);
        expect(FieldInput(label)).toHaveAttribute("aria-invalid", expectedInvalid);
    }
}

// A new last name and a new password, typed over the fetched record.
async function EditDetails(user: UserEvent): Promise<void> {
    await user.clear(FieldInput(LAST_NAME_LABEL));
    await user.type(FieldInput(LAST_NAME_LABEL), EDITED_LAST_NAME);
    await user.type(FieldInput(PASSWORD_LABEL), NEW_PASSWORD);
}

// The global order of a mock's first call, for asserting which of two calls came first.
function FirstCallOrder(recorder: CallRecorder): number {
    const [firstCall] = recorder.mock.invocationCallOrder;
    expect(firstCall).toBeDefined();
    return firstCall ?? 0;
}

async function ExpectNavigationTo(route: string): Promise<void> {
    await waitFor(() => {
        expect(mockRouterPush).toHaveBeenCalledWith(route);
    });
    await WaitForIdle();
}

// Opened from COUSR00, with the first-entry fetch still in flight.
async function StartEntryFetch(): Promise<ScreenView> {
    mockSearchParams = new URLSearchParams({ userId: TEST_USER_ID });
    mockFetchUser.mockImplementation(ReadUntilAborted);
    const view = RenderScreen();
    await waitFor(() => {
        expect(mockFetchUser).toHaveBeenCalledTimes(1);
    });
    return view;
}

// Opened from the admin menu, with an ENTER fetch of a typed User ID still in flight.
async function StartEnterFetch(): Promise<ScreenView> {
    mockFetchUser.mockImplementation(ReadUntilAborted);
    const view = RenderScreen();
    await view.user.type(FieldInput(USER_ID_LABEL), TYPED_USER_ID);
    await Trigger(view, FETCH_ACTION, "key");
    expect(mockFetchUser).toHaveBeenCalledTimes(1);
    return view;
}

const PENDING_FETCH_CASES: readonly PendingFetchCase[] = [
    { origin: "first entry from COUSR00C", StartFetch: StartEntryFetch },
    { origin: "ENTER", StartFetch: StartEnterFetch },
];

// Two ENTER presses before React re-renders: the second finds Fetch still enabled.
function PressEnterTwiceBeforeRender(): void {
    act(() => {
        fireEvent.keyDown(window, { key: "Enter" });
        fireEvent.keyDown(window, { key: "Enter" });
    });
}

// Runs every continuation already queued, so a discarded answer has had its chance to show.
async function SettleQueuedWork(): Promise<void> {
    await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
    });
}

beforeEach(() => {
    mockSearchParams = new URLSearchParams();
    mockFetchUser.mockReset();
    mockUpdateUser.mockReset();
});

afterEach(() => {
    window.sessionStorage.clear();
});

describe("COUSR02C BMS COUSR02 content", () => {
    it("COUSR02 shows the Update User title under the CU02 / COUSR02C header", () => {
        RenderScreen();
        expect(screen.getByRole("heading", { level: 1, name: "Update User" })).toBeVisible();
        expect(screen.getByText("Tran: CU02")).toBeInTheDocument();
        expect(screen.getByText("Prog: COUSR02C")).toBeInTheDocument();
        expect(mockFetchUser).not.toHaveBeenCalled();
    });

    it.each(FIELD_LENGTH_CASES)("COUSR02 field %p keeps its BMS LENGTH %p", (label, length) => {
        RenderScreen();
        expect(FieldInput(label)).toHaveAttribute("maxlength", String(length));
        expect(FieldInput(label)).toHaveValue("");
    });

    // The design-system mapping gives every password field (COSGN00, COUSR01, COUSR02) one token.
    it("COUSR02 PASSWD is a password input with the current-password autocomplete token", () => {
        RenderScreen();
        expect(FieldInput(PASSWORD_LABEL)).toHaveAttribute("type", "password");
        expect(FieldInput(PASSWORD_LABEL)).toHaveAttribute("autocomplete", "current-password");
        expect(FieldInput(PASSWORD_LABEL)).toHaveValue("");
    });
});

describe("COUSR02C BMS COUSR02 hints and PF legend", () => {
    it("COUSR02 PASSWD is a dark (masked) field; PASSWD and USRTYPE keep their hints", () => {
        RenderScreen();
        expect(FieldInput(PASSWORD_LABEL)).toHaveAttribute("type", "password");
        expect(FieldInput(PASSWORD_LABEL)).toHaveAccessibleDescription("(8 Char)");
        expect(FieldInput(USER_TYPE_LABEL)).toHaveAccessibleDescription("(A=Admin, U=User)");
    });

    it.each(LEGEND_ACTIONS)("COUSR02 legend shows the $buttonName button", ({ buttonName }) => {
        RenderScreen();
        expect(screen.getByRole("button", { name: buttonName })).toBeEnabled();
    });
});

describe("COUSR02C first entry from COUSR00C", () => {
    it("COUSR02C fetches the user COUSR00C selected, once, on first entry", async () => {
        await RenderFromList();
        expect(mockFetchUser).toHaveBeenCalledTimes(1);
        ExpectFetchOf(TEST_USER_ID);
        await ExpectAlert(PRESS_PF5_MESSAGE, "info");
    });

    it("COUSR02C shows the names and type but never prefills the hashed password", async () => {
        await RenderFromList();
        expect(FieldInput(USER_ID_LABEL)).toHaveValue(TEST_USER_ID);
        expect(FieldInput(FIRST_NAME_LABEL)).toHaveValue(FETCHED_FIRST_NAME);
        expect(FieldInput(LAST_NAME_LABEL)).toHaveValue(FETCHED_LAST_NAME);
        expect(FieldInput(USER_TYPE_LABEL)).toHaveValue(FETCHED_USER_TYPE);
        expect(FieldInput(PASSWORD_LABEL)).toHaveValue("");
        expect(FieldInput(USER_ID_LABEL)).toHaveFocus();
    });
});

// Every arrival with another CDEMO-CU02-USR-SELECTED is a fresh first entry, as every XCTL is.
describe("COUSR02C same-route push that selects another user", () => {
    it("COUSR02C fetches the newly selected user and PF5 saves that user", async () => {
        const view = await RenderFromList();
        await SwitchToOtherUser(view);
        ExpectLastFetchOf(OTHER_USER_ID);
        expect(FieldInput(USER_ID_LABEL)).toHaveValue(OTHER_USER_ID);
        expect(FieldInput(FIRST_NAME_LABEL)).toHaveValue(OTHER_FIRST_NAME);
        mockUpdateUser.mockResolvedValue(BuildSaveResponse(OTHER_UPDATED_MESSAGE));
        await Trigger(view, SAVE_ACTION, "key");
        await ExpectAlert(OTHER_UPDATED_MESSAGE, "success");
        expect(mockUpdateUser).toHaveBeenCalledTimes(1);
        expect(mockUpdateUser).toHaveBeenCalledWith(OTHER_USER_ID, OTHER_UNEDITED_REQUEST);
    });
});

describe("COUSR02C late answer for the user selected before a same-route push", () => {
    it("COUSR02C discards the previous user's answer that arrives after the switch", async () => {
        mockSearchParams = new URLSearchParams({ userId: TEST_USER_ID });
        const settleFirstFetch = DeferNextFetch();
        const view = RenderScreen();
        await SwitchToOtherUser(view);
        expect([FetchSignal(0)?.aborted, FetchSignal(1)?.aborted]).toEqual([true, false]);
        await act(async () => {
            settleFirstFetch(BuildUserDetail());
        });
        expect(FieldInput(USER_ID_LABEL)).toHaveValue(OTHER_USER_ID);
        expect(FieldInput(LAST_NAME_LABEL)).toHaveValue(OTHER_LAST_NAME);
        expect(screen.queryByRole("progressbar")).toBeNull();
    });
});

describe("COUSR02C history step back to the previously selected user", () => {
    it("COUSR02C re-enters and fetches the first user again on a history step back", async () => {
        const view = await RenderFromList();
        await SwitchToOtherUser(view);
        mockFetchUser.mockResolvedValue(BuildUserDetail());
        NavigateToUser(view, TEST_USER_ID);
        await screen.findByDisplayValue(FETCHED_LAST_NAME);
        await WaitForIdle();
        expect(mockFetchUser).toHaveBeenCalledTimes(3);
        ExpectLastFetchOf(TEST_USER_ID);
        expect(FieldInput(USER_ID_LABEL)).toHaveValue(TEST_USER_ID);
    });
});

describe("COUSR02C ENTER (PROCESS-ENTER-KEY)", () => {
    it.each(TRIGGER_MODES)("COUSR02C ENTER by %s fetches the User ID as typed", async (mode) => {
        mockFetchUser.mockResolvedValue(BuildUserDetail({ user_id: TYPED_USER_ID }));
        const view = RenderScreen();
        await view.user.type(FieldInput(USER_ID_LABEL), TYPED_USER_ID);
        await Trigger(view, FETCH_ACTION, mode);
        await ExpectAlert(PRESS_PF5_MESSAGE, "info");
        expect(mockFetchUser).toHaveBeenCalledTimes(1);
        ExpectFetchOf(TYPED_USER_ID);
        expect(FieldInput(FIRST_NAME_LABEL)).toHaveValue(FETCHED_FIRST_NAME);
        expect(FieldInput(PASSWORD_LABEL)).toHaveValue("");
    });

    it("COUSR02C ENTER on a blank User ID sends SPACES and shows the server literal", async () => {
        mockFetchUser.mockRejectedValue(LegacyRejection(USER_ID_EMPTY_MESSAGE, HTTP_BAD_REQUEST));
        const view = RenderScreen();
        await Trigger(view, FETCH_ACTION, "key");
        await ExpectAlert(USER_ID_EMPTY_MESSAGE, "error");
        ExpectFetchOf(BLANK_USER_ID);
    });
});

describe("COUSR02C ENTER on a read that fails", () => {
    it("COUSR02C ENTER blanks the shown details before a read that answers NOTFND", async () => {
        const view = await RenderFromList();
        mockFetchUser.mockRejectedValue(LegacyRejection(USER_NOT_FOUND_MESSAGE, HTTP_NOT_FOUND));
        await view.user.clear(FieldInput(USER_ID_LABEL));
        await view.user.type(FieldInput(USER_ID_LABEL), TYPED_USER_ID);
        await Trigger(view, FETCH_ACTION, "key");
        await ExpectAlert(USER_NOT_FOUND_MESSAGE, "error");
        ExpectLastFetchOf(TYPED_USER_ID);
        expect(FieldInput(USER_ID_LABEL)).toHaveValue(TYPED_USER_ID);
        expect(FieldInput(FIRST_NAME_LABEL)).toHaveValue("");
        expect(FieldInput(LAST_NAME_LABEL)).toHaveValue("");
        expect(FieldInput(USER_TYPE_LABEL)).toHaveValue("");
    });
});

describe("COUSR02C PF5 (UPDATE-USER-INFO)", () => {
    it.each(TRIGGER_MODES)("COUSR02C PF5 by %s saves the edits as typed", async (mode) => {
        const view = await RenderFromList();
        mockUpdateUser.mockResolvedValue(BuildSaveResponse(UPDATED_MESSAGE));
        await EditDetails(view.user);
        await Trigger(view, SAVE_ACTION, mode);
        await ExpectAlert(UPDATED_MESSAGE, "success");
        expect(mockUpdateUser).toHaveBeenCalledTimes(1);
        expect(mockUpdateUser).toHaveBeenCalledWith(TEST_USER_ID, EDITED_REQUEST);
        expect(FieldInput(PASSWORD_LABEL)).toHaveValue("");
        expect(mockRouterPush).not.toHaveBeenCalled();
    });

    it("COUSR02C PF5 with the Password left blank sends it blank (hash kept)", async () => {
        const view = await RenderFromList();
        mockUpdateUser.mockRejectedValue(LegacyRejection(NO_CHANGE_MESSAGE, HTTP_BAD_REQUEST));
        await Trigger(view, SAVE_ACTION, "key");
        await ExpectAlert(NO_CHANGE_MESSAGE, "error");
        expect(mockUpdateUser).toHaveBeenCalledWith(TEST_USER_ID, UNEDITED_REQUEST);
    });
});

describe("COUSR02C PF3 (UPDATE-USER-INFO, then RETURN-TO-PREV-SCREEN)", () => {
    it.each(TRIGGER_MODES)(
        "COUSR02C PF3 by %s saves pending changes before returning",
        async (mode) => {
            const view = await RenderFromList();
            mockUpdateUser.mockResolvedValue(BuildSaveResponse(UPDATED_MESSAGE));
            await EditDetails(view.user);
            await Trigger(view, SAVE_AND_EXIT_ACTION, mode);
            await ExpectNavigationTo(USER_LIST_ROUTE);
            expect(mockUpdateUser).toHaveBeenCalledWith(TEST_USER_ID, EDITED_REQUEST);
            expect(FirstCallOrder(mockUpdateUser)).toBeLessThan(FirstCallOrder(mockRouterPush));
            expect(mockRouterPush).toHaveBeenCalledTimes(1);
        },
    );
});

describe("COUSR02C PF3 return target (CDEMO-FROM-PROGRAM)", () => {
    it("COUSR02C PF3 returns whatever the save answered, without showing it", async () => {
        const view = await RenderFromList();
        mockUpdateUser.mockRejectedValue(LegacyRejection(NO_CHANGE_MESSAGE, HTTP_BAD_REQUEST));
        await Trigger(view, SAVE_AND_EXIT_ACTION, "key");
        await ExpectNavigationTo(USER_LIST_ROUTE);
        expect(mockUpdateUser).toHaveBeenCalledWith(TEST_USER_ID, UNEDITED_REQUEST);
        expect(screen.queryByText(NO_CHANGE_MESSAGE)).toBeNull();
    });

    it("COUSR02C PF3 saves, then returns to COADM01C (/admin) when no list opened it", async () => {
        mockUpdateUser.mockRejectedValue(LegacyRejection(USER_ID_EMPTY_MESSAGE, HTTP_BAD_REQUEST));
        const view = RenderScreen();
        await Trigger(view, SAVE_AND_EXIT_ACTION, "key");
        await ExpectNavigationTo(ADMIN_MENU_ROUTE);
        expect(mockUpdateUser).toHaveBeenCalledWith(BLANK_USER_ID, BLANK_REQUEST);
        expect(FirstCallOrder(mockUpdateUser)).toBeLessThan(FirstCallOrder(mockRouterPush));
    });
});

describe("COUSR02C PF12 (back to COADM01C)", () => {
    it.each(TRIGGER_MODES)("COUSR02C PF12 by %s returns to /admin without saving", async (mode) => {
        const view = await RenderFromList();
        await EditDetails(view.user);
        await Trigger(view, CANCEL_ACTION, mode);
        expect(mockRouterPush).toHaveBeenCalledTimes(1);
        expect(mockRouterPush).toHaveBeenCalledWith(ADMIN_MENU_ROUTE);
        expect(mockUpdateUser).not.toHaveBeenCalled();
    });
});

describe("COUSR02C PF4 (CLEAR-CURRENT-SCREEN)", () => {
    it.each(TRIGGER_MODES)(
        "COUSR02C PF4 by %s blanks every field and the message",
        async (mode) => {
            const view = await RenderFromList();
            await EditDetails(view.user);
            await Trigger(view, CLEAR_ACTION, mode);
            for (const label of ALL_FIELD_LABELS) {
                expect(FieldInput(label)).toHaveValue("");
            }
            expect(screen.queryByRole("alert")).toBeNull();
            expect(FieldInput(USER_ID_LABEL)).toHaveFocus();
            expect(mockFetchUser).toHaveBeenCalledTimes(1);
            expect(mockUpdateUser).not.toHaveBeenCalled();
            expect(mockRouterPush).not.toHaveBeenCalled();
        },
    );
});

describe("COUSR02C READ-USER-SEC-FILE literals", () => {
    it.each(FETCH_LITERAL_CASES)(
        "COUSR02C fetch shows %p (HTTP %p) as %p",
        async (literal, status, severity) => {
            ArrangeFetchOutcome(literal, status);
            mockSearchParams = new URLSearchParams({ userId: TEST_USER_ID });
            RenderScreen();
            await ExpectAlert(literal, severity);
            await WaitForIdle();
        },
    );
});

describe("COUSR02C UPDATE-USER-INFO literals", () => {
    it.each(SAVE_LITERAL_CASES)(
        "COUSR02C PF5 shows %p (HTTP %p) as %p",
        async (literal, status, severity) => {
            const view = await RenderFromList();
            ArrangeSaveOutcome(literal, status);
            await Trigger(view, SAVE_ACTION, "click");
            await ExpectAlert(literal, severity);
            expect(screen.queryByText(PRESS_PF5_MESSAGE)).toBeNull();
        },
    );
});

// EIBCALEN = 0 (back to COSGN00C) is the session guard's 401 with an empty message.
describe("COUSR02C session refusal on the fetch", () => {
    it("COUSR02C fetch answered 401 leaves the sign-on redirect to the client", async () => {
        mockSearchParams = new URLSearchParams({ userId: TEST_USER_ID });
        mockFetchUser.mockRejectedValue(BuildAxiosError(HTTP_UNAUTHORIZED, BuildErrorBody()));
        RenderScreen();
        await waitFor(() => {
            expect(mockFetchUser).toHaveBeenCalledTimes(1);
        });
        await WaitForIdle();
        ExpectInterceptorOwnedSignOn();
    });
});

describe("COUSR02C session refusal on PF5", () => {
    it("COUSR02C PF5 answered 401 clears the message line and does not route", async () => {
        const view = await RenderFromList();
        mockUpdateUser.mockRejectedValue(BuildAxiosError(HTTP_UNAUTHORIZED, BuildErrorBody()));
        await Trigger(view, SAVE_ACTION, "key");
        await waitFor(() => {
            expect(screen.queryByRole("alert")).toBeNull();
        });
        ExpectInterceptorOwnedSignOn();
    });
});

describe("COUSR02C field errors and cursor (MOVE -1 TO <field>L)", () => {
    it("COUSR02C marks each field_errors field and moves the cursor to cursor_field", async () => {
        const view = await RenderFromList();
        const body = BuildErrorBody({
            message: FIRST_NAME_EMPTY_MESSAGE,
            field_errors: ["first_name", "last_name"],
            cursor_field: "first_name",
        });
        mockUpdateUser.mockRejectedValue(BuildAxiosError(HTTP_BAD_REQUEST, body));
        await Trigger(view, SAVE_ACTION, "click");
        await ExpectAlert(FIRST_NAME_EMPTY_MESSAGE, "error");
        expect(FieldInput(FIRST_NAME_LABEL)).toHaveAttribute("aria-invalid", "true");
        expect(FieldInput(LAST_NAME_LABEL)).toHaveAttribute("aria-invalid", "true");
        expect(FieldInput(PASSWORD_LABEL)).not.toHaveAttribute("aria-invalid", "true");
        await waitFor(() => {
            expect(FieldInput(FIRST_NAME_LABEL)).toHaveFocus();
        });
    });
});

// UPDATE-USER-INFO: USRIDIN and FNAME pass, so a blank LNAMEI is the first presence refusal.
describe("COUSR02C PF5 with a blank Last Name (MOVE -1 TO LNAMEL)", () => {
    it("COUSR02C PF5 with only LNAME cleared shows its literal in red at LNAME", async () => {
        const view = await RenderFromList();
        const body = BuildErrorBody({
            message: LAST_NAME_EMPTY_MESSAGE,
            field_errors: ["last_name"],
            cursor_field: "last_name",
        });
        mockUpdateUser.mockRejectedValue(BuildAxiosError(HTTP_BAD_REQUEST, body));
        await view.user.clear(FieldInput(LAST_NAME_LABEL));
        await Trigger(view, SAVE_ACTION, "click");
        await ExpectAlert(LAST_NAME_EMPTY_MESSAGE, "error");
        expect(mockUpdateUser).toHaveBeenCalledTimes(1);
        expect(mockUpdateUser).toHaveBeenCalledWith(TEST_USER_ID, LAST_NAME_CLEARED_REQUEST);
        ExpectOnlyFieldInvalid(LAST_NAME_LABEL);
        await waitFor(() => {
            expect(FieldInput(LAST_NAME_LABEL)).toHaveFocus();
        });
    });
});

describe("COUSR02C leaving the screen during a fetch", () => {
    it.each(PENDING_FETCH_CASES)(
        "COUSR02C unmounted during the $origin fetch aborts it and routes nowhere",
        async ({ StartFetch }) => {
            const view = await StartFetch();
            const signal = FetchSignal(0);
            expect(signal?.aborted).toBe(false);
            view.Unmount();
            expect(signal?.aborted).toBe(true);
            await expect(mockFetchUser.mock.results[0]?.value).rejects.toBeInstanceOf(
                CanceledError,
            );
            expect(mockRouterPush).not.toHaveBeenCalled();
            expect(mockRouterReplace).not.toHaveBeenCalled();
        },
    );
});

describe("COUSR02C ENTER superseding a fetch in flight", () => {
    it("COUSR02C a newer ENTER aborts the earlier fetch; only the newer one answers", async () => {
        mockFetchUser.mockImplementationOnce(ReadUntilAborted);
        const resolveNewest = DeferNextFetch();
        const view = RenderScreen();
        await view.user.type(FieldInput(USER_ID_LABEL), TEST_USER_ID);
        PressEnterTwiceBeforeRender();
        await SettleQueuedWork();
        expect([FetchSignal(0)?.aborted, FetchSignal(1)?.aborted]).toEqual([true, false]);
        expect(screen.queryByRole("alert")).toBeNull();
        expect(screen.getByRole("progressbar")).toBeInTheDocument();
        act(() => resolveNewest(BuildUserDetail({ last_name: NEWEST_LAST_NAME })));
        await ExpectAlert(PRESS_PF5_MESSAGE, "info");
        await WaitForIdle();
        expect(FieldInput(LAST_NAME_LABEL)).toHaveValue(NEWEST_LAST_NAME);
    });
});

describe("COUSR02C fetch answered by a cancellation", () => {
    it("COUSR02C fetch rejected with CanceledError shows no message and ends busy", async () => {
        mockFetchUser.mockRejectedValue(new CanceledError());
        const view = RenderScreen();
        await view.user.type(FieldInput(USER_ID_LABEL), TYPED_USER_ID);
        await Trigger(view, FETCH_ACTION, "key");
        await WaitForIdle();
        expect(mockFetchUser).toHaveBeenCalledTimes(1);
        expect(screen.queryByRole("alert")).toBeNull();
        expect(screen.getByRole("button", { name: FETCH_ACTION.buttonName })).toBeEnabled();
    });
});

describe("COUSR02C first entry under React Strict Mode", () => {
    it("COUSR02C aborts the rehearsal mount's fetch and shows the remount's answer", async () => {
        mockSearchParams = new URLSearchParams({ userId: TEST_USER_ID });
        mockFetchUser.mockImplementationOnce(ReadUntilAborted).mockResolvedValue(BuildUserDetail());
        RenderScreen({ reactStrictMode: true });
        await ExpectAlert(PRESS_PF5_MESSAGE, "info");
        await WaitForIdle();
        expect(mockFetchUser).toHaveBeenCalledTimes(2);
        expect([FetchSignal(0)?.aborted, FetchSignal(1)?.aborted]).toEqual([true, false]);
        expect(FieldInput(LAST_NAME_LABEL)).toHaveValue(FETCHED_LAST_NAME);
    });
});

describe("COUSR02C field error description", () => {
    it("COUSR02C describes the flagged USRTYPE by the ERRMSG literal, then its hint", async () => {
        const view = await RenderFromList();
        const body = BuildErrorBody({
            message: USER_TYPE_EMPTY_MESSAGE,
            field_errors: ["user_type"],
            cursor_field: "user_type",
        });
        mockUpdateUser.mockRejectedValue(BuildAxiosError(HTTP_BAD_REQUEST, body));
        await Trigger(view, SAVE_ACTION, "click");
        await ExpectAlert(USER_TYPE_EMPTY_MESSAGE, "error");
        expect(FieldInput(USER_TYPE_LABEL)).toHaveAccessibleDescription(
            `${USER_TYPE_EMPTY_MESSAGE} (A=Admin, U=User)`,
        );
        expect(FieldInput(PASSWORD_LABEL)).toHaveAccessibleDescription("(8 Char)");
    });
});
