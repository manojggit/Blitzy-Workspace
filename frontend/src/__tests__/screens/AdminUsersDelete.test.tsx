// Screen tests: BMS COUSR03, program COUSR03C (CU03), page /admin/users/delete.
// The literals are copied verbatim from backend/app/services/users/cousr03c.py, which ports
// cbl/COUSR03C.cbl PROCESS-ENTER-KEY, DELETE-USER-INFO, READ-USER-SEC-FILE and
// DELETE-USER-SEC-FILE.
import { ThemeProvider } from "@mui/material/styles";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { AxiosError, AxiosHeaders, type InternalAxiosRequestConfig } from "axios";
import type { ReactNode } from "react";

import AdminUsersDeletePage from "@/app/admin/users/delete/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import {
    PENDING_MESSAGE_KEY,
    StorePendingMessage,
    type LegacyErrorResponse,
    type MessageSeverity,
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
    isFromList: boolean;
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

const TEST_USER_ID = "TESTUSR1";
// USRIDIN as keyed: mixed case with an embedded space, never trimmed or case-folded.
const TYPED_USER_ID = "tstUsr 9";
const PRESS_PF5_MESSAGE = "Press PF5 key to delete this user ...";
const DELETED_MESSAGE = "User TESTUSR1 has been deleted ...";
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

const EMPTY_SCREEN_VALUES: ScreenValues = {
    user_id: "",
    first_name: "",
    last_name: "",
    user_type: "",
};

// USRIDIN is UNPROT LENGTH 8; FNAME and LNAME are ASKIP LENGTH 20; USRTYPE is ASKIP LENGTH 1.
const FIELD_CASES: readonly FieldCase[] = [
    { label: USER_ID_LABEL, maxLength: 8, isReadOnly: false },
    { label: FIRST_NAME_LABEL, maxLength: 20, isReadOnly: true },
    { label: LAST_NAME_LABEL, maxLength: 20, isReadOnly: true },
    { label: USER_TYPE_LABEL, maxLength: 1, isReadOnly: true },
];

// F3 returns to CDEMO-FROM-PROGRAM (COUSR00C) when the list opened the screen, else COADM01C.
const BACK_CASES: readonly BackCase[] = [
    { method: "click", origin: "COUSR00C", route: USER_LIST_PATH, isFromList: true },
    { method: "key", origin: "COUSR00C", route: USER_LIST_PATH, isFromList: true },
    { method: "click", origin: "COADM01C", route: ADMIN_MENU_PATH, isFromList: false },
    { method: "key", origin: "COADM01C", route: ADMIN_MENU_PATH, isFromList: false },
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

function RenderScreen(): UserEvent {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <AdminUsersDeletePage />
        </ThemeProvider>,
    );
    return user;
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

// Opened from the admin menu, with the user ID keyed on USRIDIN.
async function RenderWithTypedId(userIdText: string): Promise<UserEvent> {
    const user = RenderScreen();
    await user.type(screen.getByLabelText(USER_ID_LABEL), userIdText);
    return user;
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
        expect(mockFetchUser).toHaveBeenCalledWith(TEST_USER_ID, UserFetchScreen.DELETE);
        expect(ShownValues()).toEqual(FetchedValues(TEST_USER_ID));
    });
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
        expect(mockFetchUser).toHaveBeenCalledWith(TYPED_USER_ID, UserFetchScreen.DELETE);
        expect(ShownValues()).toEqual(FetchedValues(TYPED_USER_ID));
    });
});

describe("COUSR03C ENTER edit and blanking (PROCESS-ENTER-KEY)", () => {
    it("COUSR03C ENTER with a blank user ID sends SPACES for the server's empty edit", async () => {
        ArrangeRejection(HTTP_EDIT_FAILURE, BuildErrorBody({ message: USER_ID_EMPTY_MESSAGE }));
        const user = RenderScreen();
        await PressFunction(user, "ENTER", "click");
        await ExpectAlert(USER_ID_EMPTY_MESSAGE, "error");
        expect(mockFetchUser).toHaveBeenCalledWith(BLANK_USER_ID, UserFetchScreen.DELETE);
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
        async ({ method, route, isFromList }) => {
            const user = isFromList ? await RenderFromList() : RenderScreen();
            await PressFunction(user, "F3", method);
            expect(mockRouterPush).toHaveBeenCalledTimes(1);
            expect(mockRouterPush).toHaveBeenCalledWith(route);
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
            expect(input).toHaveFocus();
        },
    );
});
