// Screen tests: BMS COUSR03, program COUSR03C (CU03), page /admin/users/delete.
// The literals are copied verbatim from backend/app/services/users/cousr03c.py, which ports
// cbl/COUSR03C.cbl PROCESS-ENTER-KEY, DELETE-USER-INFO, READ-USER-SEC-FILE and
// DELETE-USER-SEC-FILE.
import { ThemeProvider } from "@mui/material/styles";
import {
    act,
    fireEvent,
    render,
    screen,
    waitFor,
    within,
    type RenderOptions,
} from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { AxiosError, AxiosHeaders, CanceledError, type InternalAxiosRequestConfig } from "axios";
import type { ReactNode } from "react";

import AdminUsersDeletePage from "@/app/admin/users/delete/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import {
    PENDING_MESSAGE_KEY,
    StorePendingMessage,
    type LegacyErrorResponse,
    type MessageSeverity,
    type ReadOptions,
} from "@/lib/apiClient";
import {
    DeleteUser,
    FetchUser,
    UserFetchScreen,
    type UserDetailResponse,
    type UserMessageResponse,
} from "@/lib/api/users";
import { PF_KEY_ACTIONS, type PfKey } from "@/lib/useFunctionKeys";

const mockRouterPush = jest.fn();
const mockRouterReplace = jest.fn();
// One router object for every render, as Next.js keeps useRouter stable across renders.
const mockRouter = {
    push: mockRouterPush,
    replace: mockRouterReplace,
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};
// Read by the mocked useSearchParams at render time; each test sets the query it opens with.
let mockSearchParams = new URLSearchParams();

jest.mock("next/navigation", () => ({
    useRouter: () => mockRouter,
    useSearchParams: () => mockSearchParams,
    usePathname: () => "/admin/users/delete",
}));

// The mocked keys mirror the exports of @/lib/session; the signed-in user is an admin (type A).
jest.mock("@/lib/session", () => ({
    SessionProvider: ({ children }: { children: ReactNode }) => children,
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

// COUSR03 calls only FetchUser and DeleteUser; the other endpoint functions are stubbed, and the
// real UserFetchScreen enum is kept.
jest.mock("@/lib/api/users", () => ({
    ...jest.requireActual<typeof import("@/lib/api/users")>("@/lib/api/users"),
    ListUsers: jest.fn(),
    SelectUsers: jest.fn(),
    AddUser: jest.fn(),
    UpdateUser: jest.fn(),
    FetchUser: jest.fn(),
    DeleteUser: jest.fn(),
}));

const mockFetchUser = jest.mocked(FetchUser);
const mockDeleteUser = jest.mocked(DeleteUser);

type ScreenKey = Extract<PfKey, "ENTER" | "F3" | "F4" | "F5" | "F12">;
type PressMethod = "click" | "key";
type ScreenCall = "fetch" | "delete";
type UserField = "user_id" | "first_name" | "last_name" | "user_type";
type ScreenValues = Readonly<Record<UserField, string>>;

interface FieldCase {
    label: string;
    maxLength: number;
    isReadOnly: boolean;
}

interface BackCase {
    method: PressMethod;
    origin: string;
    route: string;
    // The query string the screen opens with, and how many fetches its first entry makes.
    query: string;
    fetchCount: number;
}

interface LiteralCase {
    literal: string;
    status: number;
    severity: MessageSeverity;
    call: ScreenCall;
}

interface FocusCase {
    field: UserField;
    label: string;
    isUserIdKeyed: boolean;
    literal: string;
    status: number;
}

interface ScreenView {
    user: UserEvent;
    Unmount: () => void;
}

// A fetch left in flight when the screen unmounts: the first-entry read or an ENTER read.
interface PendingFetchCase {
    origin: string;
    StartFetch: () => Promise<ScreenView>;
}

interface NavigableView {
    user: UserEvent;
    // Renders the page again under the current mockSearchParams, as the App Router does on a
    // same-route push or a history step that changes the query.
    Rerender: () => void;
}

// A same-route step between the two queries that select no user, and where F3 then returns.
interface BlankArrivalSwitchCase {
    from: string;
    to: string;
    fromQuery: string;
    toQuery: string;
    route: string;
}

const TEST_USER_ID = "TESTUSR1";
// A second user COUSR00C can select, with details unlike the first user's.
const OTHER_USER_ID = "TESTUSR2";
// USRIDIN as keyed: mixed case with an embedded space, never trimmed or case-folded.
const TYPED_USER_ID = "tstUsr 9";
// A hand-typed userId query longer than CDEMO-CU03-USR-SELECTED PIC X(08); its first 8
// characters are TEST_USER_ID.
const OVER_LONG_USER_ID_QUERY = `${TEST_USER_ID}XYZ`;
const PRESS_PF5_MESSAGE = "Press PF5 key to delete this user ...";
const DELETED_MESSAGE = "User TESTUSR1 has been deleted ...";
const OTHER_DELETED_MESSAGE = "User TESTUSR2 has been deleted ...";
// STRING ... DELIMITED BY SPACE keeps the typed ID up to its first space.
const TYPED_DELETED_MESSAGE = "User tstUsr has been deleted ...";
const NOT_FOUND_MESSAGE = "User ID NOT found...";
const USER_ID_EMPTY_MESSAGE = "User ID can NOT be empty...";
const UNABLE_TO_LOOKUP_MESSAGE = "Unable to lookup User...";
// The source's delete-failure literal says "Update" (DELETE-USER-SEC-FILE).
const UNABLE_TO_UPDATE_MESSAGE = "Unable to Update User...";
const ABEND_MESSAGE_TEXT = "UNEXPECTED ABEND OCCURRED.";
// COMEN01C keeps the trailing space of its literal, which a 403 refusal carries.
const ADMIN_ONLY_MESSAGE = "No access - Admin Only option... ";
// The last name the newer of two overlapping fetches answers with.
const NEWEST_LAST_NAME = "Newest";

// A blank USRIDIN travels as SPACES, so the server answers with its own empty-ID literal.
const BLANK_USER_ID = " ";
const USER_ID_FIELD: UserField = "user_id";
const FIRST_NAME_FIELD: UserField = "first_name";

const SIGN_ON_PATH = "/signon";
const USER_LIST_PATH = "/admin/users";
const ADMIN_MENU_PATH = "/admin";

const HTTP_OK = 200;
const HTTP_EDIT_FAILURE = 400;
const HTTP_UNAUTHORIZED = 401;
const HTTP_FORBIDDEN = 403;
const HTTP_NOT_FOUND = 404;
const HTTP_INTERNAL_SERVER_ERROR = 500;
const HTTP_FILE_ERROR = 503;

const USER_ID_LABEL = "Enter User ID:";
const FIRST_NAME_LABEL = "First Name:";
const LAST_NAME_LABEL = "Last Name:";
const USER_TYPE_LABEL = "User Type:";
const USER_TYPE_LEGEND = "(A=Admin, U=User)";

// The BMS legend ENTER=Fetch F3=Back F4=Clear F5=Delete, plus the F12 COUSR03C handles.
const KEY_BUTTON_NAMES: Readonly<Record<ScreenKey, string>> = {
    ENTER: "Fetch (Enter)",
    F3: "Back (F3)",
    F4: "Clear (F4)",
    F5: "Delete (F5)",
    F12: "Cancel (F12)",
};

const CALL_KEYS: Readonly<Record<ScreenCall, ScreenKey>> = { fetch: "ENTER", delete: "F5" };

const SEVERITY_CLASSES: Readonly<Record<MessageSeverity, string>> = {
    error: "MuiAlert-colorError",
    success: "MuiAlert-colorSuccess",
    info: "MuiAlert-colorInfo",
};

const PRESS_METHODS: readonly PressMethod[] = ["click", "key"];
const SCREEN_CALLS: readonly ScreenCall[] = ["fetch", "delete"];

// Every fetch carries the signal of the AbortController that owns it; a delete never does.
const SIGNALLED_READ_OPTIONS = expect.objectContaining({ signal: expect.any(AbortSignal) });

const EMPTY_SCREEN_VALUES: ScreenValues = {
    user_id: "",
    first_name: "",
    last_name: "",
    user_type: "",
};

// The second user's record, as fetched and as shown.
const OTHER_USER_VALUES: ScreenValues = {
    user_id: OTHER_USER_ID,
    first_name: "Grace",
    last_name: "Hopper",
    user_type: "A",
};

// USRIDIN is UNPROT LENGTH 8; FNAME and LNAME are ASKIP LENGTH 20; USRTYPE is ASKIP LENGTH 1.
const FIELD_CASES: readonly FieldCase[] = [
    { label: USER_ID_LABEL, maxLength: 8, isReadOnly: false },
    { label: FIRST_NAME_LABEL, maxLength: 20, isReadOnly: true },
    { label: LAST_NAME_LABEL, maxLength: 20, isReadOnly: true },
    { label: USER_TYPE_LABEL, maxLength: 1, isReadOnly: true },
];

// F3 returns to CDEMO-FROM-PROGRAM (COUSR00C) whenever the userId query is present, even empty
// (CDEMO-CU03-USR-SELECTED blank, so nothing is fetched), else to COADM01C.
const BACK_ORIGINS: readonly Omit<BackCase, "method">[] = [
    { origin: "COUSR00C", route: USER_LIST_PATH, query: `userId=${TEST_USER_ID}`, fetchCount: 1 },
    { origin: "COUSR00C (empty userId)", route: USER_LIST_PATH, query: "userId=", fetchCount: 0 },
    { origin: "COADM01C", route: ADMIN_MENU_PATH, query: "", fetchCount: 0 },
];
const BACK_CASES: readonly BackCase[] = BACK_ORIGINS.flatMap((backOrigin) =>
    PRESS_METHODS.map((method) => ({ ...backOrigin, method })),
);

// A missing and an empty userId both select no user, yet each is an arrival of its own, because
// only the empty one returns F3 to COUSR00C.
const BLANK_ARRIVAL_SWITCH_CASES: readonly BlankArrivalSwitchCase[] = [
    {
        from: "no userId",
        to: "an empty userId",
        fromQuery: "",
        toQuery: "userId=",
        route: USER_LIST_PATH,
    },
    {
        from: "an empty userId",
        to: "no userId",
        fromQuery: "userId=",
        toQuery: "",
        route: ADMIN_MENU_PATH,
    },
];

// Every literal COUSR03C sends, with the status the backend answers it with and its BMS colour.
const LITERAL_CASES: readonly LiteralCase[] = [
    { literal: NOT_FOUND_MESSAGE, status: HTTP_NOT_FOUND, severity: "error", call: "fetch" },
    { literal: NOT_FOUND_MESSAGE, status: HTTP_NOT_FOUND, severity: "error", call: "delete" },
    { literal: USER_ID_EMPTY_MESSAGE, status: HTTP_EDIT_FAILURE, severity: "error", call: "fetch" },
    {
        literal: USER_ID_EMPTY_MESSAGE,
        status: HTTP_EDIT_FAILURE,
        severity: "error",
        call: "delete",
    },
    {
        literal: UNABLE_TO_LOOKUP_MESSAGE,
        status: HTTP_FILE_ERROR,
        severity: "error",
        call: "fetch",
    },
    {
        literal: UNABLE_TO_UPDATE_MESSAGE,
        status: HTTP_FILE_ERROR,
        severity: "error",
        call: "delete",
    },
    {
        literal: ABEND_MESSAGE_TEXT,
        status: HTTP_INTERNAL_SERVER_ERROR,
        severity: "error",
        call: "fetch",
    },
    { literal: PRESS_PF5_MESSAGE, status: HTTP_OK, severity: "info", call: "fetch" },
    { literal: DELETED_MESSAGE, status: HTTP_OK, severity: "success", call: "delete" },
];

// MOVE -1 TO USRIDINL for the edit failure; MOVE -1 TO FNAMEL for the WHEN OTHER file error.
const FOCUS_CASES: readonly FocusCase[] = [
    {
        field: USER_ID_FIELD,
        label: USER_ID_LABEL,
        isUserIdKeyed: false,
        literal: USER_ID_EMPTY_MESSAGE,
        status: HTTP_EDIT_FAILURE,
    },
    {
        field: FIRST_NAME_FIELD,
        label: FIRST_NAME_LABEL,
        isUserIdKeyed: true,
        literal: UNABLE_TO_LOOKUP_MESSAGE,
        status: HTTP_FILE_ERROR,
    },
];

// A fetched user as GET /admin/users/{userId}?screen=delete returns it: never a password or hash.
function BuildUserDetail(overrides: Partial<UserDetailResponse> = {}): UserDetailResponse {
    return {
        user_id: TEST_USER_ID,
        first_name: "Ada",
        last_name: "Lovelace",
        user_type: "U",
        message: PRESS_PF5_MESSAGE,
        ...overrides,
    };
}

function BuildDeleteResponse(message: string): UserMessageResponse {
    return { message, cursor_field: USER_ID_FIELD };
}

// The common.LegacyErrorResponse body of a non-2xx outcome.
function BuildErrorBody(overrides: Partial<LegacyErrorResponse> = {}): LegacyErrorResponse {
    return { message: "", info_message: "", field_errors: [], cursor_field: null, ...overrides };
}

function BuildAxiosError(status: number, body: unknown): AxiosError {
    const config: InternalAxiosRequestConfig = {
        url: `${USER_LIST_PATH}/${TEST_USER_ID}`,
        method: "get",
        headers: new AxiosHeaders(),
    };
    const response = { status, data: body, statusText: "", headers: {}, config };
    return new AxiosError(
        "Request failed",
        AxiosError.ERR_BAD_REQUEST,
        config,
        undefined,
        response,
    );
}

// The screen values the page shows after a fetch: the user ID stays as keyed (USRIDINI).
function FetchedValues(userIdText: string): ScreenValues {
    const { first_name, last_name, user_type } = BuildUserDetail();
    return { user_id: userIdText, first_name, last_name, user_type };
}

function InputValue(label: string): string {
    return screen.getByLabelText<HTMLInputElement>(label).value;
}

function ShownValues(): ScreenValues {
    return {
        user_id: InputValue(USER_ID_LABEL),
        first_name: InputValue(FIRST_NAME_LABEL),
        last_name: InputValue(LAST_NAME_LABEL),
        user_type: InputValue(USER_TYPE_LABEL),
    };
}

function ScreenTree() {
    return (
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <AdminUsersDeletePage />
        </ThemeProvider>
    );
}

function RenderScreenView(renderOptions: RenderOptions = {}): ScreenView {
    const user = userEvent.setup();
    const { unmount } = render(<ScreenTree />, renderOptions);
    return { user, Unmount: unmount };
}

function RenderScreen(): UserEvent {
    return RenderScreenView().user;
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

// Some fetch read the user ID exactly as given, through a signal of its own.
function ExpectFetchOf(userIdText: string): void {
    expect(mockFetchUser).toHaveBeenCalledWith(
        userIdText,
        UserFetchScreen.DELETE,
        SIGNALLED_READ_OPTIONS,
    );
}

// The latest fetch read the user ID exactly as given, through a signal of its own.
function ExpectLastFetchOf(userIdText: string): void {
    expect(mockFetchUser).toHaveBeenLastCalledWith(
        userIdText,
        UserFetchScreen.DELETE,
        SIGNALLED_READ_OPTIONS,
    );
}

// Opened from COUSR00C with the given user, under a query the test changes afterwards.
function RenderNavigable(userId: string): NavigableView {
    mockSearchParams = new URLSearchParams({ userId });
    const user = userEvent.setup();
    const { rerender } = render(<ScreenTree />);
    return { user, Rerender: () => rerender(<ScreenTree />) };
}

// A same-route push or history step to the given userId query.
function NavigateToUser(view: NavigableView, userId: string): void {
    mockSearchParams = new URLSearchParams({ userId });
    view.Rerender();
}

// Opened under the given query string, which may carry no userId, and the test changes later.
function RenderAtQuery(query: string): NavigableView {
    mockSearchParams = new URLSearchParams(query);
    const user = userEvent.setup();
    const { rerender } = render(<ScreenTree />);
    return { user, Rerender: () => rerender(<ScreenTree />) };
}

// A same-route push or history step to the given query string, which may carry no userId.
function NavigateToQuery(view: NavigableView, query: string): void {
    mockSearchParams = new URLSearchParams(query);
    view.Rerender();
}

// The next fetch or delete stays in flight until the returned function settles it with the
// first user's record or the deleted literal.
function DeferCall(call: ScreenCall): () => void {
    let settle: () => void = () => undefined;
    const pending = new Promise<void>((resolve) => {
        settle = resolve;
    });
    if (call === "fetch") {
        mockFetchUser.mockReturnValueOnce(pending.then(() => BuildUserDetail()));
    } else {
        mockDeleteUser.mockReturnValueOnce(
            pending.then(() => BuildDeleteResponse(DELETED_MESSAGE)),
        );
    }
    return () => settle();
}

// The busy bar is gone once a call has settled and every button is pressable again.
async function ExpectIdle(): Promise<void> {
    await waitFor(() => {
        expect(screen.queryByRole("progressbar")).toBeNull();
    });
}

// The message line's alert, compared on its raw textContent so that trailing blanks count.
async function ExpectAlert(text: string, severity: MessageSeverity): Promise<void> {
    await waitFor(() => {
        const alert = screen.getByRole("alert");
        expect(alert.textContent).toBe(text);
        expect(alert).toHaveClass(SEVERITY_CLASSES[severity]);
    });
    await ExpectIdle();
}

// The COUSR00C D selection: the page opens with userId and fetches that user on first entry.
async function RenderFromList(): Promise<UserEvent> {
    mockSearchParams = new URLSearchParams({ userId: TEST_USER_ID });
    mockFetchUser.mockResolvedValue(BuildUserDetail({ message: PRESS_PF5_MESSAGE }));
    const user = RenderScreen();
    await ExpectAlert(PRESS_PF5_MESSAGE, "info");
    return user;
}

// COUSR00C selects the second user while this screen is shown, on the same route.
async function SwitchToOtherUser(view: NavigableView): Promise<void> {
    mockFetchUser.mockResolvedValue(BuildUserDetail(OTHER_USER_VALUES));
    NavigateToUser(view, OTHER_USER_ID);
    await waitFor(() => {
        expect(ShownValues()).toEqual(OTHER_USER_VALUES);
    });
    await ExpectIdle();
}

// Opened from the admin menu, with the user ID keyed on USRIDIN.
async function RenderWithTypedId(userIdText: string): Promise<UserEvent> {
    const user = RenderScreen();
    await user.type(screen.getByLabelText(USER_ID_LABEL), userIdText);
    return user;
}

// Keys another user ID over the whole USRIDIN text, so its LENGTH 8 never blocks the typing.
async function TypeOverUserId(user: UserEvent, userIdText: string): Promise<void> {
    const input = screen.getByLabelText<HTMLInputElement>(USER_ID_LABEL);
    await user.type(input, userIdText, {
        initialSelectionStart: 0,
        initialSelectionEnd: input.value.length,
    });
}

// The physical key, as the 3270 attention key arrives as a KeyboardEvent.key.
async function PressKey(user: UserEvent, key: ScreenKey): Promise<void> {
    await user.keyboard(`{${PF_KEY_ACTIONS[key]}}`);
}

async function PressFunction(user: UserEvent, key: ScreenKey, method: PressMethod): Promise<void> {
    if (method === "click") {
        await user.click(screen.getByRole("button", { name: KEY_BUTTON_NAMES[key] }));
        return;
    }
    await PressKey(user, key);
}

// Every call answers with the case's literal: resolved for a 200, rejected otherwise.
function ArrangeOutcome(literalCase: LiteralCase): void {
    if (literalCase.status === HTTP_OK) {
        mockFetchUser.mockResolvedValue(BuildUserDetail({ message: literalCase.literal }));
        mockDeleteUser.mockResolvedValue(BuildDeleteResponse(literalCase.literal));
        return;
    }
    // FastAPI's own 500 is plain text, never a legacy body, so the page shows the abend text.
    const body =
        literalCase.status === HTTP_INTERNAL_SERVER_ERROR
            ? "Internal Server Error"
            : BuildErrorBody({ message: literalCase.literal, cursor_field: USER_ID_FIELD });
    const error = BuildAxiosError(literalCase.status, body);
    mockFetchUser.mockRejectedValue(error);
    mockDeleteUser.mockRejectedValue(error);
}

function ArrangeRejection(status: number, body: LegacyErrorResponse): void {
    const error = BuildAxiosError(status, body);
    mockFetchUser.mockRejectedValue(error);
    mockDeleteUser.mockRejectedValue(error);
}

// The COUSR00C D selection, with the first-entry fetch still in flight.
async function StartEntryFetch(): Promise<ScreenView> {
    mockSearchParams = new URLSearchParams({ userId: TEST_USER_ID });
    mockFetchUser.mockImplementation(ReadUntilAborted);
    const view = RenderScreenView();
    await waitFor(() => {
        expect(mockFetchUser).toHaveBeenCalledTimes(1);
    });
    return view;
}

// Opened from the admin menu, with an ENTER fetch of a keyed user ID still in flight.
async function StartEnterFetch(): Promise<ScreenView> {
    mockFetchUser.mockImplementation(ReadUntilAborted);
    const view = RenderScreenView();
    await view.user.type(screen.getByLabelText(USER_ID_LABEL), TYPED_USER_ID);
    await PressKey(view.user, "ENTER");
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
        fireEvent.keyDown(window, { key: PF_KEY_ACTIONS.ENTER });
        fireEvent.keyDown(window, { key: PF_KEY_ACTIONS.ENTER });
    });
}

// Runs every continuation already queued, so a discarded answer has had its chance to show.
async function SettleQueuedWork(): Promise<void> {
    await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
    });
}

// Keys the user ID on USRIDIN and presses ENTER.
async function KeyAndFetch(user: UserEvent, userIdText: string): Promise<void> {
    await user.type(screen.getByLabelText(USER_ID_LABEL), userIdText);
    await PressKey(user, "ENTER");
}

// Opened under the given query, with TEST_USER_ID keyed and its record fetched by ENTER.
async function RenderWithFetchedUser(query: string): Promise<NavigableView> {
    const view = RenderAtQuery(query);
    await KeyAndFetch(view.user, TEST_USER_ID);
    await ExpectAlert(PRESS_PF5_MESSAGE, "info");
    expect(ShownValues()).toEqual(FetchedValues(TEST_USER_ID));
    return view;
}

// A first entry that selected no user: every COUSR3A field blank and no message.
function ExpectBlankEntry(): void {
    expect(ShownValues()).toEqual(EMPTY_SCREEN_VALUES);
    expect(screen.queryByRole("alert")).toBeNull();
}

beforeEach(() => {
    mockSearchParams = new URLSearchParams();
    mockFetchUser.mockResolvedValue(BuildUserDetail());
    mockDeleteUser.mockResolvedValue(BuildDeleteResponse(DELETED_MESSAGE));
    window.sessionStorage.clear();
});

describe("COUSR03C screen header and fields (BMS COUSR03)", () => {
    it("COUSR03 shows the Delete User title and the CU03 / COUSR03C header", () => {
        RenderScreen();
        expect(screen.getByRole("heading", { name: "Delete User" })).toBeInTheDocument();
        expect(screen.getByText("Tran: CU03")).toBeInTheDocument();
        expect(screen.getByText("Prog: COUSR03C")).toBeInTheDocument();
    });

    it.each(FIELD_CASES)(
        "COUSR03 field $label has BMS LENGTH $maxLength, read-only $isReadOnly",
        ({ label, maxLength, isReadOnly }) => {
            RenderScreen();
            const input = screen.getByLabelText(label);
            expect(input).toHaveAttribute("maxlength", String(maxLength));
            expect(input.hasAttribute("readonly")).toBe(isReadOnly);
        },
    );
});

describe("COUSR03C user type legend and PF keys (BMS COUSR03)", () => {
    it("COUSR03 User Type carries the (A=Admin, U=User) legend", () => {
        RenderScreen();
        expect(screen.getByLabelText(USER_TYPE_LABEL)).toHaveAccessibleDescription(
            USER_TYPE_LEGEND,
        );
    });

    it("COUSR03 PF legend lists Fetch, Back, Clear, Delete and Cancel in order", () => {
        RenderScreen();
        const group = screen.getByRole("group", { name: "Function keys" });
        const names = within(group)
            .getAllByRole("button")
            .map((button) => button.textContent);
        expect(names).toEqual(Object.values(KEY_BUTTON_NAMES));
    });
});

describe("COUSR03C first entry from COUSR00C (CDEMO-CU03-USR-SELECTED)", () => {
    it("COUSR03C opened from COUSR00C fetches the selected user once for delete", async () => {
        await RenderFromList();
        expect(mockFetchUser).toHaveBeenCalledTimes(1);
        ExpectFetchOf(TEST_USER_ID);
        expect(ShownValues()).toEqual(FetchedValues(TEST_USER_ID));
    });
});

describe("COUSR03C first entry with a userId query longer than USRIDIN", () => {
    it("COUSR03C shows, fetches and deletes only the query's first 8 characters", async () => {
        mockSearchParams = new URLSearchParams({ userId: OVER_LONG_USER_ID_QUERY });
        const notFound = BuildErrorBody({ message: NOT_FOUND_MESSAGE });
        mockFetchUser.mockRejectedValue(BuildAxiosError(HTTP_NOT_FOUND, notFound));
        const user = RenderScreen();
        await ExpectAlert(NOT_FOUND_MESSAGE, "error");
        expect(mockFetchUser).toHaveBeenCalledTimes(1);
        ExpectFetchOf(TEST_USER_ID);
        expect(ShownValues()).toEqual({ ...EMPTY_SCREEN_VALUES, user_id: TEST_USER_ID });
        await PressFunction(user, "F5", "key");
        await ExpectAlert(DELETED_MESSAGE, "success");
        expect(mockDeleteUser).toHaveBeenCalledWith(TEST_USER_ID);
    });
});

// Every arrival with another CDEMO-CU03-USR-SELECTED is a fresh first entry, as every XCTL is.
describe("COUSR03C same-route push that selects another user", () => {
    it("COUSR03C fetches the newly selected user and PF5 deletes that user", async () => {
        const view = RenderNavigable(TEST_USER_ID);
        await ExpectAlert(PRESS_PF5_MESSAGE, "info");
        await SwitchToOtherUser(view);
        ExpectLastFetchOf(OTHER_USER_ID);
        mockDeleteUser.mockResolvedValue(BuildDeleteResponse(OTHER_DELETED_MESSAGE));
        await PressFunction(view.user, "F5", "key");
        await ExpectAlert(OTHER_DELETED_MESSAGE, "success");
        expect(mockDeleteUser).toHaveBeenCalledTimes(1);
        expect(mockDeleteUser).toHaveBeenCalledWith(OTHER_USER_ID);
    });
});

describe("COUSR03C late answer for the user selected before a same-route push", () => {
    it("COUSR03C discards the previous user's answer that arrives after the switch", async () => {
        const settleFirstFetch = DeferCall("fetch");
        const view = RenderNavigable(TEST_USER_ID);
        await SwitchToOtherUser(view);
        expect([FetchSignal(0)?.aborted, FetchSignal(1)?.aborted]).toEqual([true, false]);
        await act(async () => {
            settleFirstFetch();
        });
        expect(ShownValues()).toEqual(OTHER_USER_VALUES);
        expect(screen.queryByRole("progressbar")).toBeNull();
    });
});

describe("COUSR03C history step back to the previously selected user", () => {
    it("COUSR03C re-enters and fetches the first user again on a history step back", async () => {
        const view = RenderNavigable(TEST_USER_ID);
        await ExpectAlert(PRESS_PF5_MESSAGE, "info");
        await SwitchToOtherUser(view);
        mockFetchUser.mockResolvedValue(BuildUserDetail());
        NavigateToUser(view, TEST_USER_ID);
        await waitFor(() => {
            expect(ShownValues()).toEqual(FetchedValues(TEST_USER_ID));
        });
        await ExpectIdle();
        expect(mockFetchUser).toHaveBeenCalledTimes(3);
        ExpectLastFetchOf(TEST_USER_ID);
    });
});

// Neither arrival fetches, so the step shows only through the state the new entry starts with.
describe("COUSR03C same-route step between a missing and an empty userId", () => {
    it.each(BLANK_ARRIVAL_SWITCH_CASES)(
        "COUSR03C from $from to $to re-enters blank; PF5 deletes SPACES, PF3 goes to $route",
        async ({ fromQuery, toQuery, route }) => {
            const view = await RenderWithFetchedUser(fromQuery);
            NavigateToQuery(view, toQuery);
            ExpectBlankEntry();
            ArrangeRejection(HTTP_EDIT_FAILURE, BuildErrorBody({ message: USER_ID_EMPTY_MESSAGE }));
            await PressFunction(view.user, "F5", "key");
            await ExpectAlert(USER_ID_EMPTY_MESSAGE, "error");
            expect(mockDeleteUser).toHaveBeenCalledTimes(1);
            expect(mockDeleteUser).toHaveBeenCalledWith(BLANK_USER_ID);
            await PressFunction(view.user, "F3", "key");
            expect(mockRouterPush).toHaveBeenCalledTimes(1);
            expect(mockRouterPush).toHaveBeenCalledWith(route);
            expect(mockFetchUser).toHaveBeenCalledTimes(1);
        },
    );
});

describe("COUSR03C late answer for a fetch in flight across a missing/empty userId step", () => {
    it.each(BLANK_ARRIVAL_SWITCH_CASES)(
        "COUSR03C from $from to $to aborts the ENTER fetch; its answer leaves the entry blank",
        async ({ fromQuery, toQuery }) => {
            const settleFetch = DeferCall("fetch");
            const view = RenderAtQuery(fromQuery);
            await KeyAndFetch(view.user, TEST_USER_ID);
            expect(mockFetchUser).toHaveBeenCalledTimes(1);
            NavigateToQuery(view, toQuery);
            expect(FetchSignal(0)?.aborted).toBe(true);
            await act(async () => {
                settleFetch();
            });
            ExpectBlankEntry();
            expect(screen.queryByRole("progressbar")).toBeNull();
        },
    );
});

describe("COUSR03C first entry from COADM01C (no user selected)", () => {
    it("COUSR03C opened from COADM01C fetches nothing and puts the cursor on USRIDIN", () => {
        RenderScreen();
        expect(mockFetchUser).not.toHaveBeenCalled();
        expect(ShownValues()).toEqual(EMPTY_SCREEN_VALUES);
        expect(screen.getByLabelText(USER_ID_LABEL)).toHaveFocus();
        expect(screen.queryByRole("alert")).toBeNull();
    });

    it("COUSR03C opened from COADM01C shows the message another screen left, in red", async () => {
        StorePendingMessage(ADMIN_ONLY_MESSAGE);
        RenderScreen();
        await ExpectAlert(ADMIN_ONLY_MESSAGE, "error");
        expect(window.sessionStorage.getItem(PENDING_MESSAGE_KEY)).toBeNull();
    });
});

describe("COUSR03C ENTER fetch (PROCESS-ENTER-KEY)", () => {
    it.each(PRESS_METHODS)("COUSR03C ENTER by %s fetches the user ID as keyed", async (method) => {
        const user = await RenderWithTypedId(TYPED_USER_ID);
        await PressFunction(user, "ENTER", method);
        await ExpectAlert(PRESS_PF5_MESSAGE, "info");
        expect(mockFetchUser).toHaveBeenCalledTimes(1);
        ExpectFetchOf(TYPED_USER_ID);
        expect(ShownValues()).toEqual(FetchedValues(TYPED_USER_ID));
    });
});

describe("COUSR03C ENTER edit and blanking (PROCESS-ENTER-KEY)", () => {
    it("COUSR03C ENTER with a blank user ID sends SPACES for the server's empty edit", async () => {
        ArrangeRejection(HTTP_EDIT_FAILURE, BuildErrorBody({ message: USER_ID_EMPTY_MESSAGE }));
        const user = RenderScreen();
        await PressFunction(user, "ENTER", "click");
        await ExpectAlert(USER_ID_EMPTY_MESSAGE, "error");
        ExpectFetchOf(BLANK_USER_ID);
    });

    it("COUSR03C ENTER blanks the previous user's details before USRSEC is read", async () => {
        const user = await RenderFromList();
        ArrangeRejection(HTTP_NOT_FOUND, BuildErrorBody({ message: NOT_FOUND_MESSAGE }));
        await PressFunction(user, "ENTER", "click");
        await ExpectAlert(NOT_FOUND_MESSAGE, "error");
        expect(ShownValues()).toEqual({ ...EMPTY_SCREEN_VALUES, user_id: TEST_USER_ID });
    });
});

describe("COUSR03C PF5 delete (DELETE-USER-INFO)", () => {
    it.each(PRESS_METHODS)("COUSR03C PF5 by %s deletes and clears the screen", async (method) => {
        const user = await RenderFromList();
        await PressFunction(user, "F5", method);
        await ExpectAlert(DELETED_MESSAGE, "success");
        expect(mockDeleteUser).toHaveBeenCalledTimes(1);
        expect(mockDeleteUser).toHaveBeenCalledWith(TEST_USER_ID);
        expect(ShownValues()).toEqual(EMPTY_SCREEN_VALUES);
        expect(screen.getByLabelText(USER_ID_LABEL)).toHaveFocus();
    });

    it("COUSR03C PF5 deletes the user ID as keyed on USRIDIN, without a fetch", async () => {
        mockDeleteUser.mockResolvedValue(BuildDeleteResponse(TYPED_DELETED_MESSAGE));
        const user = await RenderWithTypedId(TYPED_USER_ID);
        await PressFunction(user, "F5", "key");
        await ExpectAlert(TYPED_DELETED_MESSAGE, "success");
        expect(mockDeleteUser).toHaveBeenCalledWith(TYPED_USER_ID);
        expect(mockFetchUser).not.toHaveBeenCalled();
    });
});

// The 3270 keyboard locks from send to response, so nothing keyed meanwhile reaches USRIDIN.
describe("COUSR03C USRIDIN keyboard lock while a call runs", () => {
    it.each(SCREEN_CALLS)("COUSR03C locks USRIDIN until the %s call answers", async (call) => {
        const settleCall = DeferCall(call);
        const user = await RenderWithTypedId(TEST_USER_ID);
        await PressFunction(user, CALL_KEYS[call], "key");
        const userIdInput = screen.getByLabelText(USER_ID_LABEL);
        expect(userIdInput).toHaveAttribute("readonly");
        await TypeOverUserId(user, OTHER_USER_ID);
        expect(userIdInput).toHaveValue(TEST_USER_ID);
        await act(async () => {
            settleCall();
        });
        await ExpectIdle();
        expect(userIdInput).not.toHaveAttribute("readonly");
    });
});

// A PF key never moved the 3270 cursor: a pointer press on the key leaves it in USRIDIN for the
// whole call, rather than on a key the busy screen disables.
describe("COUSR03C cursor while a call started by pointer runs", () => {
    it.each(SCREEN_CALLS)("COUSR03C keeps the cursor in USRIDIN while %s runs", async (call) => {
        const settleCall = DeferCall(call);
        const user = await RenderWithTypedId(TEST_USER_ID);
        await PressFunction(user, CALL_KEYS[call], "click");
        const userIdInput = screen.getByLabelText(USER_ID_LABEL);
        expect(userIdInput).toHaveFocus();
        expect(userIdInput).toHaveAttribute("readonly");
        expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "true");
        await act(async () => {
            settleCall();
        });
        await ExpectIdle();
        expect(userIdInput).toHaveFocus();
        expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "false");
    });
});

describe("COUSR03C PF5 after keys typed while FetchUser ran", () => {
    it("COUSR03C shows the fetched user beside its own ID and PF5 deletes that user", async () => {
        const settleFetch = DeferCall("fetch");
        const user = await RenderWithTypedId(TEST_USER_ID);
        await PressFunction(user, "ENTER", "key");
        await TypeOverUserId(user, OTHER_USER_ID);
        await act(async () => {
            settleFetch();
        });
        await ExpectAlert(PRESS_PF5_MESSAGE, "info");
        expect(ShownValues()).toEqual(FetchedValues(TEST_USER_ID));
        await PressFunction(user, "F5", "key");
        await ExpectAlert(DELETED_MESSAGE, "success");
        expect(mockDeleteUser).toHaveBeenCalledTimes(1);
        expect(mockDeleteUser).toHaveBeenCalledWith(TEST_USER_ID);
    });
});

describe("COUSR03C PF5 refusal (DELETE-USER-SEC-FILE WHEN OTHER)", () => {
    it("COUSR03C PF5 refused by USRSEC keeps the fetched details on screen", async () => {
        const user = await RenderFromList();
        ArrangeRejection(HTTP_FILE_ERROR, BuildErrorBody({ message: UNABLE_TO_UPDATE_MESSAGE }));
        await PressFunction(user, "F5", "click");
        await ExpectAlert(UNABLE_TO_UPDATE_MESSAGE, "error");
        expect(ShownValues()).toEqual(FetchedValues(TEST_USER_ID));
    });
});

describe("COUSR03C PF12 cancel (RETURN-TO-PREV-SCREEN to COADM01C)", () => {
    it.each(PRESS_METHODS)(
        "COUSR03C PF12 by %s returns to the admin menu without deleting",
        async (method) => {
            const user = await RenderFromList();
            await PressFunction(user, "F12", method);
            expect(mockRouterPush).toHaveBeenCalledTimes(1);
            expect(mockRouterPush).toHaveBeenCalledWith(ADMIN_MENU_PATH);
            expect(mockDeleteUser).not.toHaveBeenCalled();
        },
    );
});

describe("COUSR03C PF3 back (RETURN-TO-PREV-SCREEN to CDEMO-FROM-PROGRAM)", () => {
    it.each(BACK_CASES)(
        "COUSR03C PF3 by $method from $origin returns to $route without deleting",
        async ({ method, route, query, fetchCount }) => {
            mockSearchParams = new URLSearchParams(query);
            const user = RenderScreen();
            await ExpectIdle();
            await PressFunction(user, "F3", method);
            expect(mockRouterPush).toHaveBeenCalledTimes(1);
            expect(mockRouterPush).toHaveBeenCalledWith(route);
            expect(mockFetchUser).toHaveBeenCalledTimes(fetchCount);
            expect(mockDeleteUser).not.toHaveBeenCalled();
        },
    );
});

describe("COUSR03C PF4 clear (CLEAR-CURRENT-SCREEN)", () => {
    it.each(PRESS_METHODS)("COUSR03C PF4 by %s clears without a server call", async (method) => {
        const user = await RenderFromList();
        await PressFunction(user, "F4", method);
        await waitFor(() => {
            expect(screen.queryByRole("alert")).toBeNull();
        });
        expect(ShownValues()).toEqual(EMPTY_SCREEN_VALUES);
        expect(screen.getByLabelText(USER_ID_LABEL)).toHaveFocus();
        expect(mockFetchUser).toHaveBeenCalledTimes(1);
        expect(mockDeleteUser).not.toHaveBeenCalled();
        expect(mockRouterPush).not.toHaveBeenCalled();
    });
});

// COUSR03C WHEN OTHER (lines 126-128): an AID outside ENTER, PF3, PF4, PF5 and PF12 sets ERR-FLG
// and sends CCDA-MSG-INVALID-KEY (cpy/CSMSG01Y.cpy) over the map as received. Nothing is read or
// deleted, and the SEND's CURSOR puts the cursor on USRIDIN, the map's IC field.
const INVALID_KEY_MESSAGE = "Invalid key pressed. Please see below...";
const UNBOUND_KEYS: readonly string[] = ["F7", "F8"];

// The first-entry fetch is the only call, and the user it showed is still shown.
function ExpectFetchedUserKept(): void {
    expect(ShownValues()).toEqual(FetchedValues(TEST_USER_ID));
    expect(mockFetchUser).toHaveBeenCalledTimes(1);
    expect(mockDeleteUser).not.toHaveBeenCalled();
    expect(mockRouterPush).not.toHaveBeenCalled();
}

describe("COUSR03C WHEN OTHER (a PF key the legend lacks)", () => {
    it.each(UNBOUND_KEYS)(
        "COUSR03C %s shows the invalid-key literal in red and keeps the user shown",
        async (keyName) => {
            const user = await RenderFromList();
            await user.click(screen.getByLabelText(FIRST_NAME_LABEL));
            const firstNameInput = screen.getByLabelText(FIRST_NAME_LABEL);
            expect(fireEvent.keyDown(firstNameInput, { key: keyName })).toBe(false);
            await ExpectAlert(INVALID_KEY_MESSAGE, "error");
            await waitFor(() => expect(screen.getByLabelText(USER_ID_LABEL)).toHaveFocus());
            expect(screen.getByLabelText(USER_ID_LABEL)).toHaveAttribute("aria-invalid", "false");
            ExpectFetchedUserKept();
            await PressFunction(user, "F5", "key");
            await ExpectAlert(DELETED_MESSAGE, "success");
            expect(mockDeleteUser).toHaveBeenCalledWith(TEST_USER_ID);
        },
    );
});

// USRIDIN kept focused while the user scrolled away from it lies above the viewport, and focusing
// it again for the next answer scrolls nothing by itself.
const FIELD_ABOVE_VIEWPORT_TOP = -368;
const FIELD_HEIGHT = 56;
// The gap the message line leaves between the viewport top and the cursor field it scrolls back
// to: its scroll margin, one theme spacing unit, while a line shows, and none while it is blank.
const SHOWN_LINE_SCROLL_MARGIN = 4;
const BLANK_LINE_SCROLL_MARGIN = 0;
// One of the PF keys the legend lacks.
const REPEATED_UNBOUND_KEY = "F7";

interface RepeatedAnswerCase {
    press: string;
    answer: string;
    Press: (user: UserEvent) => Promise<void>;
    scrollMargin: number;
}

// A key pressed twice and answered alike with no call between, the cursor on USRIDIN both times:
// PF4's blank message line by pointer, and the invalid-key literal of a PF key the legend lacks,
// pressed where the cursor is. ENTER and PF5 blank the line as they send, so no answer of theirs
// repeats the line shown before it.
const REPEATED_ANSWER_CASES: readonly RepeatedAnswerCase[] = [
    {
        press: "PF4 by click",
        answer: "a blank message line",
        Press: (user) => PressFunction(user, "F4", "click"),
        scrollMargin: BLANK_LINE_SCROLL_MARGIN,
    },
    {
        press: REPEATED_UNBOUND_KEY,
        answer: INVALID_KEY_MESSAGE,
        Press: (user) => user.keyboard(`{${REPEATED_UNBOUND_KEY}}`),
        scrollMargin: SHOWN_LINE_SCROLL_MARGIN,
    },
];

// Resolves in an animation frame requested after the message line's own, which jsdom runs in
// request order.
function NextAnimationFrame(): Promise<void> {
    return new Promise((resolve) => {
        window.requestAnimationFrame(() => resolve());
    });
}

// The case's key pressed, then the frame in which the message line handles its answer.
async function PressAndSettle(user: UserEvent, repeatedCase: RepeatedAnswerCase): Promise<void> {
    await repeatedCase.Press(user);
    await NextAnimationFrame();
}

function LayOutAboveViewport(field: HTMLElement): void {
    const aboveViewport = new DOMRect(0, FIELD_ABOVE_VIEWPORT_TOP, 0, FIELD_HEIGHT);
    jest.spyOn(field, "getBoundingClientRect").mockReturnValue(aboveViewport);
}

// The window.scrollBy calls made while the steps run; jsdom implements no scrolling, so a spy
// stands in for it until they end.
async function WindowScrollsDuring(steps: () => Promise<void>): Promise<unknown[]> {
    const scrollBySpy = jest.spyOn(window, "scrollBy").mockImplementation(() => undefined);
    try {
        await steps();
        return [...scrollBySpy.mock.calls];
    } finally {
        scrollBySpy.mockRestore();
    }
}

describe("COUSR03C cursor brought back into view by a repeated answer", () => {
    it.each(REPEATED_ANSWER_CASES)(
        "COUSR03C $press answered by $answer again scrolls the focused USRIDIN back into view",
        async (repeatedCase) => {
            const user = await RenderFromList();
            await PressAndSettle(user, repeatedCase);
            const userIdInput = screen.getByLabelText(USER_ID_LABEL);
            expect(userIdInput).toHaveFocus();
            LayOutAboveViewport(userIdInput);
            const scrolls = await WindowScrollsDuring(() => PressAndSettle(user, repeatedCase));
            const top = FIELD_ABOVE_VIEWPORT_TOP - repeatedCase.scrollMargin;
            expect(scrolls).toEqual([[{ top }]]);
            expect(userIdInput).toHaveFocus();
        },
    );
});

// The keys disable while the delete runs, the invalid-key action with them, so the delete's answer
// is the message shown.
describe("COUSR03C a PF key the legend lacks while a delete is in flight", () => {
    it.each(UNBOUND_KEYS)(
        "COUSR03C %s while a PF5 delete is in flight answers nothing; the delete's answer shows",
        async (keyName) => {
            const settleDelete = DeferCall("delete");
            const user = await RenderFromList();
            await PressFunction(user, "F5", "key");
            expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "true");
            expect(fireEvent.keyDown(window, { key: keyName })).toBe(false);
            expect(screen.queryByRole("alert")).toBeNull();
            await act(async () => {
                settleDelete();
            });
            await ExpectAlert(DELETED_MESSAGE, "success");
            expect(mockDeleteUser).toHaveBeenCalledTimes(1);
        },
    );
});

// A second key pressed with PF5 before the busy render disables the keys: a second delete, a
// fetch, a return to either menu, a clear and the invalid-key literal.
const SECOND_KEYS_AFTER_DELETE: readonly string[] = ["F5", "Enter", "F3", "F4", "F12", "F7"];

// Two keys pressed before React re-renders: the second finds every key still enabled.
function PressTwoKeysBeforeRender(firstKeyName: string, secondKeyName: string): void {
    act(() => {
        fireEvent.keyDown(window, { key: firstKeyName });
        fireEvent.keyDown(window, { key: secondKeyName });
    });
}

// One delete is out for the fetched user, and nothing else has happened: no fetch, no clear, no
// message and no navigation.
function ExpectOneDeleteInFlight(): void {
    expect(mockDeleteUser).toHaveBeenCalledTimes(1);
    expect(mockDeleteUser).toHaveBeenCalledWith(TEST_USER_ID);
    expect(mockFetchUser).toHaveBeenCalledTimes(1);
    expect(ShownValues()).toEqual(FetchedValues(TEST_USER_ID));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(mockRouterPush).not.toHaveBeenCalled();
}

// The one delete's green answer clears the screen, and a later PF5 sends the next DELETE.
async function ExpectDeleteAnsweredThenUnlocked(user: UserEvent): Promise<void> {
    await ExpectAlert(DELETED_MESSAGE, "success");
    expect(ShownValues()).toEqual(EMPTY_SCREEN_VALUES);
    expect(mockDeleteUser).toHaveBeenCalledTimes(1);
    expect(mockRouterPush).not.toHaveBeenCalled();
    await PressFunction(user, "F5", "key");
    await ExpectIdle();
    expect(mockDeleteUser).toHaveBeenCalledTimes(2);
}

// The in-flight lock refuses the second key, so one press sends one DELETE and its green answer is
// the one shown, never a second DELETE's "User ID NOT found..."; the lock then clears.
describe("COUSR03C keys pressed in the same turn as PF5", () => {
    it.each(SECOND_KEYS_AFTER_DELETE)(
        "COUSR03C PF5 and %s in one turn send one DELETE, answered on the screen sent",
        async (secondKeyName) => {
            const settleDelete = DeferCall("delete");
            const user = await RenderFromList();
            PressTwoKeysBeforeRender(PF_KEY_ACTIONS.F5, secondKeyName);
            ExpectOneDeleteInFlight();
            await act(async () => {
                settleDelete();
            });
            await ExpectDeleteAnsweredThenUnlocked(user);
        },
    );
});

// Only ENTER's read went out, over blanked details: no DELETE and no navigation.
function ExpectReadAloneInFlight(): void {
    expect(mockFetchUser).toHaveBeenCalledTimes(2);
    ExpectLastFetchOf(TEST_USER_ID);
    expect(mockDeleteUser).not.toHaveBeenCalled();
    expect(ShownValues()).toEqual({ ...EMPTY_SCREEN_VALUES, user_id: TEST_USER_ID });
    expect(mockRouterPush).not.toHaveBeenCalled();
}

// The read's prompt shows the user, still undeleted; a later PF5 then sends one DELETE.
async function ExpectReadAnsweredThenDeleteUnlocked(user: UserEvent): Promise<void> {
    await ExpectAlert(PRESS_PF5_MESSAGE, "info");
    expect(ShownValues()).toEqual(FetchedValues(TEST_USER_ID));
    expect(mockDeleteUser).not.toHaveBeenCalled();
    expect(mockRouterPush).not.toHaveBeenCalled();
    await PressFunction(user, "F5", "key");
    await ExpectAlert(DELETED_MESSAGE, "success");
    expect(mockDeleteUser).toHaveBeenCalledTimes(1);
    expect(mockDeleteUser).toHaveBeenCalledWith(TEST_USER_ID);
}

// The 3270 keyboard locks from ENTER until the read answers, so a PF5 keyed in ENTER's turn,
// before the busy render disables the keys, never deletes the user the read then shows.
describe("COUSR03C PF5 pressed in the same turn as ENTER", () => {
    it("COUSR03C ENTER and PF5 in one turn send the read alone; a later PF5 deletes", async () => {
        const user = await RenderFromList();
        const settleFetch = DeferCall("fetch");
        PressTwoKeysBeforeRender(PF_KEY_ACTIONS.ENTER, PF_KEY_ACTIONS.F5);
        ExpectReadAloneInFlight();
        await act(async () => {
            settleFetch();
        });
        await ExpectReadAnsweredThenDeleteUnlocked(user);
    });
});

describe("COUSR03C message literals (ERRMSG)", () => {
    it.each(LITERAL_CASES)(
        "COUSR03C $call answered $status shows '$literal' as $severity",
        async (literalCase) => {
            ArrangeOutcome(literalCase);
            const user = await RenderWithTypedId(TEST_USER_ID);
            await PressFunction(user, CALL_KEYS[literalCase.call], "click");
            await ExpectAlert(literalCase.literal, literalCase.severity);
        },
    );
});

describe("COUSR03C expired session (401)", () => {
    it.each(SCREEN_CALLS)("COUSR03C %s answered 401 returns to sign-on", async (call) => {
        ArrangeRejection(HTTP_UNAUTHORIZED, BuildErrorBody());
        const user = await RenderWithTypedId(TEST_USER_ID);
        await PressFunction(user, CALL_KEYS[call], "click");
        await waitFor(() => {
            expect(mockRouterReplace).toHaveBeenCalledWith(SIGN_ON_PATH);
        });
        await ExpectIdle();
        expect(screen.queryByRole("alert")).toBeNull();
    });
});

describe("COUSR03C role refusal (403)", () => {
    it("COUSR03C answered 403 leaves the refusal to the apiClient interceptor", async () => {
        ArrangeRejection(HTTP_FORBIDDEN, BuildErrorBody({ message: ADMIN_ONLY_MESSAGE }));
        const user = await RenderWithTypedId(TEST_USER_ID);
        await PressFunction(user, "ENTER", "click");
        await ExpectIdle();
        expect(mockFetchUser).toHaveBeenCalledTimes(1);
        expect(screen.queryByRole("alert")).toBeNull();
        expect(mockRouterReplace).not.toHaveBeenCalled();
        expect(mockRouterPush).not.toHaveBeenCalled();
    });
});

describe("COUSR03C field error and cursor (MOVE -1 TO <field>L)", () => {
    it.each(FOCUS_CASES)(
        "COUSR03C $status marks $label in error and puts the cursor on it",
        async ({ field, label, isUserIdKeyed, literal, status }) => {
            const body = BuildErrorBody({ message: literal, field_errors: [field] });
            ArrangeRejection(status, { ...body, cursor_field: field });
            const user = isUserIdKeyed ? await RenderWithTypedId(TEST_USER_ID) : RenderScreen();
            await PressFunction(user, "ENTER", "click");
            await ExpectAlert(literal, "error");
            const input = screen.getByLabelText(label);
            expect(input).toHaveAttribute("aria-invalid", "true");
            expect(input).toHaveAccessibleDescription(literal);
            expect(input).toHaveFocus();
        },
    );
});

describe("COUSR03C leaving the screen during a fetch", () => {
    it.each(PENDING_FETCH_CASES)(
        "COUSR03C unmounted during the $origin fetch aborts it and routes nowhere",
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

describe("COUSR03C ENTER superseding a fetch in flight", () => {
    it("COUSR03C a newer ENTER aborts the earlier fetch; only the newer one answers", async () => {
        mockFetchUser.mockImplementationOnce(ReadUntilAborted);
        const resolveNewest = DeferNextFetch();
        await RenderWithTypedId(TEST_USER_ID);
        PressEnterTwiceBeforeRender();
        await SettleQueuedWork();
        expect([FetchSignal(0)?.aborted, FetchSignal(1)?.aborted]).toEqual([true, false]);
        expect(screen.queryByRole("alert")).toBeNull();
        expect(screen.getByRole("progressbar")).toBeInTheDocument();
        act(() => resolveNewest(BuildUserDetail({ last_name: NEWEST_LAST_NAME })));
        await ExpectAlert(PRESS_PF5_MESSAGE, "info");
        expect(InputValue(LAST_NAME_LABEL)).toBe(NEWEST_LAST_NAME);
    });
});

describe("COUSR03C fetch answered by a cancellation", () => {
    it("COUSR03C fetch rejected with CanceledError shows no message and ends busy", async () => {
        mockFetchUser.mockRejectedValue(new CanceledError());
        const user = await RenderWithTypedId(TEST_USER_ID);
        await PressFunction(user, "ENTER", "key");
        await ExpectIdle();
        expect(mockFetchUser).toHaveBeenCalledTimes(1);
        expect(screen.queryByRole("alert")).toBeNull();
        expect(mockRouterReplace).not.toHaveBeenCalled();
        expect(screen.getByRole("button", { name: KEY_BUTTON_NAMES.ENTER })).toBeEnabled();
    });
});

describe("COUSR03C first entry under React Strict Mode", () => {
    it("COUSR03C aborts the rehearsal mount's fetch and shows the remount's answer", async () => {
        mockSearchParams = new URLSearchParams({ userId: TEST_USER_ID });
        mockFetchUser.mockImplementationOnce(ReadUntilAborted);
        RenderScreenView({ reactStrictMode: true });
        await ExpectAlert(PRESS_PF5_MESSAGE, "info");
        expect(mockFetchUser).toHaveBeenCalledTimes(2);
        expect([FetchSignal(0)?.aborted, FetchSignal(1)?.aborted]).toEqual([true, false]);
        expect(ShownValues()).toEqual(FetchedValues(TEST_USER_ID));
    });
});
