// Screen tests: BMS COUSR01, program COUSR01C (CU01), page /admin/users/add.
// Literals are copied from backend/app/services/users/cousr01c.py, cbl/COUSR01C.cbl
// (PROCESS-ENTER-KEY and WRITE-USER-SEC-FILE, lines 120-270) and cpy/CSMSG01Y.cpy.
import { randomBytes } from "node:crypto";

import { ThemeProvider } from "@mui/material/styles";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { AxiosError, AxiosHeaders } from "axios";
import type { ReactNode } from "react";

import AdminUsersAddPage from "@/app/admin/users/add/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import type { CurrentUserResponse } from "@/lib/api/auth";
import { AddUser, type UserCreateRequest, type UserMessageResponse } from "@/lib/api/users";
import { SIGN_ON_ROUTE, type LegacyErrorResponse, type MessageSeverity } from "@/lib/apiClient";

const mockRouterPush = jest.fn();
const mockRouterReplace = jest.fn();
// One router object for every render, as next/navigation returns a stable instance.
const mockRouter = {
    push: mockRouterPush,
    replace: mockRouterReplace,
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};
// Replaced before every test; COUSR01 takes no context key, so it stays empty.
let mockSearchParams = new URLSearchParams();

jest.mock("next/navigation", () => ({
    useRouter: () => mockRouter,
    useSearchParams: () => mockSearchParams,
    usePathname: () => "/admin/users/add",
}));

// CU01 is reached from the admin menu (COADM01C), so the session user has type A.
const MOCK_CURRENT_USER: CurrentUserResponse = {
    user_id: "TESTADM1",
    user_type: "A",
    first_name: "Test",
    last_name: "Admin",
    next_route: "/admin",
};

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

// COUSR01 reaches AddUser only; the other CU00-CU03 calls are stubbed so none leaves the test.
jest.mock("@/lib/api/users", () => ({
    ...jest.requireActual("@/lib/api/users"),
    ListUsers: jest.fn(),
    SelectUsers: jest.fn(),
    AddUser: jest.fn(),
    FetchUser: jest.fn(),
    UpdateUser: jest.fn(),
    DeleteUser: jest.fn(),
}));

const mockAddUser = jest.mocked(AddUser);

type UserFieldName = keyof UserCreateRequest;
type UserFieldValues = Required<UserCreateRequest>;
type LegendKey = "ENTER" | "F3" | "F4" | "F12";
type PressMethod = "click" | "key";

interface FieldCase {
    name: UserFieldName;
    maxLength: number;
    helperText: string;
}

// [ERRMSG literal, HTTP status, message-line severity, field the program moves -1 to].
type OutcomeCase = readonly [string, number, MessageSeverity, UserFieldName];

// [input, autocomplete token it renders].
type AutoCompleteCase = readonly [UserFieldName, string];

const ADMIN_MENU_ROUTE = "/admin";
const TEST_USER_ID = "TESTUSR1";
// A fresh random 8-character value per run (PASSWD LENGTH=8), so no password is in the test.
const TEST_PASSWORD = randomBytes(4).toString("hex");

// CSMSG01Y CCDA-MSG-INVALID-KEY without its X(50) padding.
const INVALID_KEY_MESSAGE = "Invalid key pressed. Please see below...";
// WRITE-USER-SEC-FILE DFHRESP(NORMAL): 'User ' SEC-USR-ID ' has been added ...'.
const ADDED_MESSAGE = "User TESTUSR1 has been added ...";
const FIRST_NAME_EMPTY_MESSAGE = "First Name can NOT be empty...";
const USER_ID_EXISTS_MESSAGE = "User ID already exist...";
// The COUSR1A hint beside USERID and PASSWD.
const EIGHT_CHAR_HINT = "(8 Char)";

// The statuses cousr01c.py raises: an edit failure, DUPKEY/DUPREC and the WHEN OTHER file error.
const HTTP_OK = 200;
const HTTP_FIRST_NON_SUCCESS = 300;
const HTTP_EDIT_FAILURE = 400;
const HTTP_UNAUTHORIZED = 401;
const HTTP_USER_ID_EXISTS = 409;
const HTTP_FILE_ERROR = 503;

// The COUSR1A INITIAL label texts in map order.
const FIELD_LABELS: Readonly<Record<UserFieldName, string>> = {
    first_name: "First Name:",
    last_name: "Last Name:",
    user_id: "User ID:",
    password: "Password:",
    user_type: "User Type:",
};

// Each COUSR1A input with its BMS LENGTH and the hint the map shows beside it.
const FIELD_CASES: readonly FieldCase[] = [
    { name: "first_name", maxLength: 20, helperText: "" },
    { name: "last_name", maxLength: 20, helperText: "" },
    { name: "user_id", maxLength: 8, helperText: "(8 Char)" },
    { name: "password", maxLength: 8, helperText: "(8 Char)" },
    { name: "user_type", maxLength: 1, helperText: "(A=Admin, U=User)" },
];

const FIELD_NAMES: readonly UserFieldName[] = FIELD_CASES.map((fieldCase) => fieldCase.name);

// The admin keys another user's record, so autofill is off everywhere but PASSWD, which takes
// the token the design-system mapping gives every password field.
const AUTO_COMPLETE_CASES: readonly AutoCompleteCase[] = [
    ["first_name", "off"],
    ["last_name", "off"],
    ["user_id", "off"],
    ["password", "current-password"],
    ["user_type", "off"],
];

// The legend ENTER=Add User  F3=Back  F4=Clear  F12=Exit: button names and physical keys.
const LEGEND_KEYS: readonly LegendKey[] = ["ENTER", "F3", "F4", "F12"];
const LEGEND_BUTTONS: Readonly<Record<LegendKey, string>> = {
    ENTER: "Add User (Enter)",
    F3: "Back (F3)",
    F4: "Clear (F4)",
    F12: "Exit (F12)",
};
const KEYBOARD_KEYS: Readonly<Record<LegendKey, string>> = {
    ENTER: "Enter",
    F3: "F3",
    F4: "F4",
    F12: "F12",
};
const PRESS_METHODS: readonly PressMethod[] = ["click", "key"];
// A second legend key pressed with ENTER before the busy render disables the keys.
const BURST_SECOND_KEYS: readonly LegendKey[] = ["ENTER", "F3", "F4", "F12"];

// 2022-07-18 09:05:07 UTC, the instant the header clock is held at.
const FIXED_CLOCK_MILLISECONDS = Date.UTC(2022, 6, 18, 9, 5, 7);

// Every value exactly as keyed: COUSR01C keeps the case and stores any user type, "x" included.
const KEYED_USER: UserFieldValues = {
    first_name: "Mary Ann",
    last_name: "smith",
    user_id: TEST_USER_ID,
    password: TEST_PASSWORD,
    user_type: "x",
};

const BLANK_USER: UserFieldValues = {
    first_name: "",
    last_name: "",
    user_id: "",
    password: "",
    user_type: "",
};

// Every ERRMSG outcome of COUSR01C ENTER, presence checks in PROCESS-ENTER-KEY source order.
const OUTCOME_CASES: readonly OutcomeCase[] = [
    [FIRST_NAME_EMPTY_MESSAGE, HTTP_EDIT_FAILURE, "error", "first_name"],
    ["Last Name can NOT be empty...", HTTP_EDIT_FAILURE, "error", "last_name"],
    ["User ID can NOT be empty...", HTTP_EDIT_FAILURE, "error", "user_id"],
    ["Password can NOT be empty...", HTTP_EDIT_FAILURE, "error", "password"],
    ["User Type can NOT be empty...", HTTP_EDIT_FAILURE, "error", "user_type"],
    [USER_ID_EXISTS_MESSAGE, HTTP_USER_ID_EXISTS, "error", "user_id"],
    ["Unable to Add User...", HTTP_FILE_ERROR, "error", "first_name"],
    [ADDED_MESSAGE, HTTP_OK, "success", "first_name"],
];

// MUI 9 marks the Alert colour as MuiAlert-color<Severity>.
const SEVERITY_CLASSES: Readonly<Record<MessageSeverity, string>> = {
    error: "MuiAlert-colorError",
    success: "MuiAlert-colorSuccess",
    info: "MuiAlert-colorInfo",
};

function RenderScreen(): UserEvent {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <AdminUsersAddPage />
        </ThemeProvider>,
    );
    return user;
}

function FieldInput(name: UserFieldName): HTMLElement {
    return screen.getByLabelText(FIELD_LABELS[name]);
}

function LegendButton(key: LegendKey): HTMLElement {
    return screen.getByRole("button", { name: LEGEND_BUTTONS[key] });
}

// Keys each value into its field in map order, so the cursor ends on USRTYPE.
async function FillUser(user: UserEvent, values: UserFieldValues): Promise<void> {
    for (const name of FIELD_NAMES) {
        await user.type(FieldInput(name), values[name]);
    }
}

async function PressKey(user: UserEvent, key: LegendKey): Promise<void> {
    await user.keyboard(`{${KEYBOARD_KEYS[key]}}`);
}

// A legend action by its on-screen button or by its physical key; useFunctionKeys binds both.
async function PressLegend(user: UserEvent, key: LegendKey, method: PressMethod): Promise<void> {
    if (method === "click") {
        await user.click(LegendButton(key));
        return;
    }
    await PressKey(user, key);
}

// Physical keys reach the useFunctionKeys window listener. They all run inside one act, so no
// render, and no effect handing the keys their disabled state, runs between two presses.
async function PressInOneTurn(keys: readonly LegendKey[]): Promise<void> {
    await act(async () => {
        for (const key of keys) {
            const init = { key: KEYBOARD_KEYS[key], bubbles: true, cancelable: true };
            window.dispatchEvent(new KeyboardEvent("keydown", init));
        }
    });
}

// The backend legacy error body (core/errors.ErrorBody) with every key present.
function BuildErrorBody(overrides: Partial<LegacyErrorResponse>): LegacyErrorResponse {
    return { message: "", info_message: "", field_errors: [], cursor_field: "", ...overrides };
}

// The rejection axios raises for a non-2xx answer carrying a legacy error body.
function BuildAxiosError(
    status: number,
    body: LegacyErrorResponse,
): AxiosError<LegacyErrorResponse> {
    const config = { headers: new AxiosHeaders() };
    const response = { status, statusText: "", headers: {}, config, data: body };
    const message = `Request failed with status code ${status}`;
    return new AxiosError(message, undefined, config, undefined, response);
}

// A 2xx status resolves AddUser with the success body; any other status rejects it as the
// service's LegacyMessageError reaches the client, naming the cursor field as its one error.
function ArrangeAddUser(status: number, message: string, cursorField: UserFieldName): void {
    if (status < HTTP_FIRST_NON_SUCCESS) {
        const response: UserMessageResponse = { message, cursor_field: cursorField };
        mockAddUser.mockResolvedValue(response);
        return;
    }
    const body = BuildErrorBody({
        message,
        field_errors: [cursorField],
        cursor_field: cursorField,
    });
    mockAddUser.mockRejectedValue(BuildAxiosError(status, body));
}

// An AddUser call that stays in flight until the returned function settles it.
function DeferAddUser(): (response: UserMessageResponse) => void {
    let settle: (response: UserMessageResponse) => void = () => undefined;
    mockAddUser.mockReturnValue(
        new Promise<UserMessageResponse>((resolve) => {
            settle = resolve;
        }),
    );
    return (response) => settle(response);
}

// Answers the deferred add with the green confirmation inside act, so the page applies it first.
async function AnswerAdded(settleAdd: (response: UserMessageResponse) => void): Promise<void> {
    await act(async () => {
        settleAdd({ message: ADDED_MESSAGE, cursor_field: "first_name" });
    });
}

// The message line holds exactly the literal, untrimmed, in its severity's colour.
async function ExpectAlert(text: string, severity: MessageSeverity): Promise<void> {
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(text);
    expect(alert).toHaveClass(SEVERITY_CLASSES[severity]);
}

function ExpectFieldValues(values: UserFieldValues): void {
    for (const name of FIELD_NAMES) {
        expect(FieldInput(name)).toHaveValue(values[name]);
    }
}

// MUI marks a TextField in error with aria-invalid="true" on its input.
function ExpectInvalidFields(invalidNames: readonly UserFieldName[]): void {
    for (const name of FIELD_NAMES) {
        const isInvalid = invalidNames.includes(name);
        expect(FieldInput(name)).toHaveAttribute("aria-invalid", String(isInvalid));
    }
}

function ExpectNoCallOrNavigation(): void {
    expect(mockAddUser).not.toHaveBeenCalled();
    expect(mockRouterPush).not.toHaveBeenCalled();
    expect(mockRouterReplace).not.toHaveBeenCalled();
}

// One add is out with the form as keyed, and nothing else has happened: no second write, no clear,
// no message and no navigation.
function ExpectOneAddInFlight(): void {
    expect(mockAddUser).toHaveBeenCalledTimes(1);
    expect(mockAddUser).toHaveBeenCalledWith(KEYED_USER);
    expect(screen.getByRole("progressbar", { name: "Processing" })).toBeInTheDocument();
    ExpectFieldValues(KEYED_USER);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(mockRouterPush).not.toHaveBeenCalled();
}

// The ScreenShell header clock reads Date.now() on every one-second tick. Holding it at one
// instant makes each tick find no change, so no tick re-renders the header outside act(), while
// every timer stays real for user-event and the async queries.
beforeEach(() => {
    jest.spyOn(Date, "now").mockReturnValue(FIXED_CLOCK_MILLISECONDS);
    mockSearchParams = new URLSearchParams();
    mockAddUser.mockReset();
    window.sessionStorage.clear();
});

afterEach(() => {
    jest.restoreAllMocks();
});

describe("COUSR01C BMS COUSR01 header and legend on /admin/users/add", () => {
    it("COUSR01C shows TRNNAME CU01, PGMNAME COUSR01C and the title Add User", () => {
        RenderScreen();
        expect(screen.getByRole("heading", { level: 1, name: "Add User" })).toBeInTheDocument();
        expect(screen.getByText("Tran: CU01")).toBeInTheDocument();
        expect(screen.getByText("Prog: COUSR01C")).toBeInTheDocument();
    });

    it("COUSR01C lists ENTER=Add User  F3=Back  F4=Clear  F12=Exit in legend order", () => {
        RenderScreen();
        const legend = screen.getByRole("group", { name: "Function keys" });
        const buttonNames = within(legend)
            .getAllByRole("button")
            .map((button) => button.textContent);
        expect(buttonNames).toEqual(LEGEND_KEYS.map((key) => LEGEND_BUTTONS[key]));
    });
});

describe("COUSR01C BMS COUSR01 fields on /admin/users/add", () => {
    it.each(FIELD_CASES)(
        "COUSR01C $name keeps its INITIAL label, LENGTH $maxLength and hint '$helperText'",
        ({ name, maxLength, helperText }) => {
            RenderScreen();
            const input = FieldInput(name);
            expect(input).toHaveAttribute("maxlength", String(maxLength));
            expect(input).toHaveAccessibleDescription(helperText);
        },
    );

    it("COUSR01C PASSWD (DRK) is a password field and every other input is text", () => {
        RenderScreen();
        for (const name of FIELD_NAMES) {
            const expectedType = name === "password" ? "password" : "text";
            expect(FieldInput(name)).toHaveAttribute("type", expectedType);
        }
    });
});

describe("COUSR01C BMS COUSR01 field autofill on /admin/users/add", () => {
    it.each(AUTO_COMPLETE_CASES)("COUSR01C %s carries autocomplete '%s'", (name, token) => {
        RenderScreen();
        expect(FieldInput(name)).toHaveAttribute("autocomplete", token);
    });
});

describe("COUSR01C first send (MOVE LOW-VALUES TO COUSR1AO) on /admin/users/add", () => {
    it("COUSR01C opens blank, without a message, with the cursor on FNAME (IC)", () => {
        RenderScreen();
        ExpectFieldValues(BLANK_USER);
        expect(screen.queryByRole("alert")).toBeNull();
        expect(FieldInput("first_name")).toHaveFocus();
    });
});

describe("COUSR01C ENTER (PROCESS-ENTER-KEY) on /admin/users/add", () => {
    it.each(PRESS_METHODS)("COUSR01C ENTER by %s sends every field as keyed", async (method) => {
        ArrangeAddUser(HTTP_OK, ADDED_MESSAGE, "first_name");
        const user = RenderScreen();
        await FillUser(user, KEYED_USER);
        await PressLegend(user, "ENTER", method);
        await ExpectAlert(ADDED_MESSAGE, "success");
        expect(mockAddUser).toHaveBeenCalledTimes(1);
        expect(mockAddUser).toHaveBeenCalledWith(KEYED_USER);
    });

    it("COUSR01C ENTER on a blank screen leaves the presence checks to the server", async () => {
        ArrangeAddUser(HTTP_EDIT_FAILURE, FIRST_NAME_EMPTY_MESSAGE, "first_name");
        const user = RenderScreen();
        await PressLegend(user, "ENTER", "key");
        await ExpectAlert(FIRST_NAME_EMPTY_MESSAGE, "error");
        expect(mockAddUser).toHaveBeenCalledWith(BLANK_USER);
    });
});

describe("COUSR01C ENTER while the write is in flight on /admin/users/add", () => {
    it("COUSR01C swallows a second ENTER until the add answers: one ENTER, one write", async () => {
        const settleAdd = DeferAddUser();
        const user = RenderScreen();
        await FillUser(user, KEYED_USER);
        await PressKey(user, "ENTER");
        await PressKey(user, "ENTER");
        expect(screen.getByRole("progressbar", { name: "Processing" })).toBeInTheDocument();
        expect(LegendButton("ENTER")).toBeDisabled();
        expect(mockAddUser).toHaveBeenCalledTimes(1);
        await act(async () => {
            settleAdd({ message: ADDED_MESSAGE, cursor_field: "first_name" });
        });
        await ExpectAlert(ADDED_MESSAGE, "success");
        expect(screen.queryByRole("progressbar")).toBeNull();
    });
});

function ExpectFieldsReadOnly(isReadOnly: boolean): void {
    for (const name of FIELD_NAMES) {
        expect(FieldInput(name).hasAttribute("readonly")).toBe(isReadOnly);
    }
}

// The 3270 keyboard locked from send to response: the cursor stays in USRTYPE, where it was
// keyed, and no field takes keystrokes until the add answers.
describe("COUSR01C fields while the write is in flight on /admin/users/add", () => {
    it("COUSR01C Add User by pointer keeps the cursor in USRTYPE, locks every field", async () => {
        const settleAdd = DeferAddUser();
        const user = RenderScreen();
        await FillUser(user, KEYED_USER);
        await user.click(LegendButton("ENTER"));
        expect(FieldInput("user_type")).toHaveFocus();
        ExpectFieldsReadOnly(true);
        expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "true");
        await user.keyboard("{Backspace}");
        expect(FieldInput("user_type")).toHaveValue(KEYED_USER.user_type);
        await act(async () => {
            settleAdd({ message: ADDED_MESSAGE, cursor_field: "first_name" });
        });
        await ExpectAlert(ADDED_MESSAGE, "success");
        ExpectFieldsReadOnly(false);
        expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "false");
    });
});

// The keys disable a render after ENTER, so a key pressed in that gap meets the in-flight lock:
// one write is sent, its answer lands on the form that was sent, and the lock then clears.
describe("COUSR01C keys pressed in the same turn as ENTER on /admin/users/add", () => {
    it.each(BURST_SECOND_KEYS)(
        "COUSR01C ENTER and %s in one turn send one write, answered on the form sent",
        async (secondKey) => {
            const settleAdd = DeferAddUser();
            await FillUser(RenderScreen(), KEYED_USER);
            await PressInOneTurn(["ENTER", secondKey]);
            ExpectOneAddInFlight();
            await AnswerAdded(settleAdd);
            await ExpectAlert(ADDED_MESSAGE, "success");
            ExpectFieldValues(BLANK_USER);
            expect(mockRouterPush).not.toHaveBeenCalled();
            await PressInOneTurn(["ENTER"]);
            expect(mockAddUser).toHaveBeenCalledTimes(2);
            expect(mockAddUser).toHaveBeenLastCalledWith(BLANK_USER);
        },
    );
});

describe("COUSR01C ERRMSG literals on /admin/users/add", () => {
    it.each(OUTCOME_CASES)(
        "COUSR01C shows %p (HTTP %i) as %s with the cursor on %s",
        async (message, status, severity, cursorField) => {
            ArrangeAddUser(status, message, cursorField);
            const user = RenderScreen();
            await FillUser(user, KEYED_USER);
            await PressLegend(user, "ENTER", "click");
            await ExpectAlert(message, severity);
            await waitFor(() => expect(FieldInput(cursorField)).toHaveFocus());
        },
    );
});

describe("COUSR01C WRITE-USER-SEC-FILE results on /admin/users/add", () => {
    it("COUSR01C DUPKEY keeps the keyed values and marks USERID only", async () => {
        ArrangeAddUser(HTTP_USER_ID_EXISTS, USER_ID_EXISTS_MESSAGE, "user_id");
        const user = RenderScreen();
        await FillUser(user, KEYED_USER);
        await PressLegend(user, "ENTER", "click");
        await ExpectAlert(USER_ID_EXISTS_MESSAGE, "error");
        ExpectFieldValues(KEYED_USER);
        ExpectInvalidFields(["user_id"]);
    });

    it("COUSR01C NORMAL clears every field (INITIALIZE-ALL-FIELDS) and marks none", async () => {
        ArrangeAddUser(HTTP_OK, ADDED_MESSAGE, "first_name");
        const user = RenderScreen();
        await FillUser(user, KEYED_USER);
        await PressLegend(user, "ENTER", "click");
        await ExpectAlert(ADDED_MESSAGE, "success");
        ExpectFieldValues(BLANK_USER);
        ExpectInvalidFields([]);
    });
});

describe("COUSR01C field errors and cursor on /admin/users/add", () => {
    it("COUSR01C marks every field_errors field and focuses cursor_field", async () => {
        const body = BuildErrorBody({
            message: FIRST_NAME_EMPTY_MESSAGE,
            field_errors: ["first_name", "last_name"],
            cursor_field: "first_name",
        });
        mockAddUser.mockRejectedValue(BuildAxiosError(HTTP_EDIT_FAILURE, body));
        const user = RenderScreen();
        await FillUser(user, KEYED_USER);
        await PressLegend(user, "ENTER", "key");
        await ExpectAlert(FIRST_NAME_EMPTY_MESSAGE, "error");
        ExpectInvalidFields(["first_name", "last_name"]);
        await waitFor(() => expect(FieldInput("first_name")).toHaveFocus());
    });
});

describe("COUSR01C field error description on /admin/users/add", () => {
    it("COUSR01C describes the flagged USERID by the ERRMSG literal, then its hint", async () => {
        ArrangeAddUser(HTTP_USER_ID_EXISTS, USER_ID_EXISTS_MESSAGE, "user_id");
        const user = RenderScreen();
        await FillUser(user, KEYED_USER);
        await PressLegend(user, "ENTER", "click");
        await ExpectAlert(USER_ID_EXISTS_MESSAGE, "error");
        expect(FieldInput("user_id")).toHaveAccessibleDescription(
            `${USER_ID_EXISTS_MESSAGE} ${EIGHT_CHAR_HINT}`,
        );
        expect(FieldInput("password")).toHaveAccessibleDescription(EIGHT_CHAR_HINT);
    });
});

describe("COUSR01C WHEN OTHER (PF12) on /admin/users/add", () => {
    it.each(PRESS_METHODS)(
        "COUSR01C PF12 shows the invalid-key literal (by %s) and calls nothing",
        async (method) => {
            const user = RenderScreen();
            await FillUser(user, KEYED_USER);
            await PressLegend(user, "F12", method);
            await ExpectAlert(INVALID_KEY_MESSAGE, "error");
            ExpectFieldValues(KEYED_USER);
            expect(FieldInput("first_name")).toHaveFocus();
            ExpectNoCallOrNavigation();
        },
    );
});

// Every PF key the legend lacks takes the same WHEN OTHER branch as PF12.
const UNBOUND_KEYS: readonly string[] = ["F5", "F7"];

function KeyDownOf(keyName: string): KeyboardEvent {
    return new KeyboardEvent("keydown", { key: keyName, bubbles: true, cancelable: true });
}

// A PF key the legend lacks, pressed where the cursor is; the event tells whether its browser
// default was blocked.
async function PressUnboundKey(keyName: string): Promise<KeyboardEvent> {
    const keyEvent = KeyDownOf(keyName);
    await act(async () => {
        (document.activeElement ?? document.body).dispatchEvent(keyEvent);
    });
    return keyEvent;
}

describe("COUSR01C WHEN OTHER (a PF key the legend lacks) on /admin/users/add", () => {
    it.each(UNBOUND_KEYS)(
        "COUSR01C %s shows the invalid-key literal in red with the cursor on FNAME, calls nothing",
        async (keyName) => {
            const user = RenderScreen();
            await FillUser(user, KEYED_USER);
            expect(FieldInput("user_type")).toHaveFocus();
            const keyEvent = await PressUnboundKey(keyName);
            await ExpectAlert(INVALID_KEY_MESSAGE, "error");
            expect(keyEvent.defaultPrevented).toBe(true);
            ExpectFieldValues(KEYED_USER);
            ExpectInvalidFields([]);
            await waitFor(() => expect(FieldInput("first_name")).toHaveFocus());
            ExpectNoCallOrNavigation();
        },
    );
});

// In ENTER's own turn the in-flight lock refuses the key; after the busy render the disabled
// invalid-key action does. Either way the add's answer is the message shown.
describe("COUSR01C a PF key the legend lacks during the write on /admin/users/add", () => {
    it.each(UNBOUND_KEYS)(
        "COUSR01C %s while the add is in flight answers nothing; the add's answer shows",
        async (keyName) => {
            const settleAdd = DeferAddUser();
            await FillUser(RenderScreen(), KEYED_USER);
            act(() => {
                window.dispatchEvent(KeyDownOf(KEYBOARD_KEYS.ENTER));
                window.dispatchEvent(KeyDownOf(keyName));
            });
            ExpectOneAddInFlight();
            expect(LegendButton("ENTER")).toBeDisabled();
            expect((await PressUnboundKey(keyName)).defaultPrevented).toBe(true);
            ExpectOneAddInFlight();
            await AnswerAdded(settleAdd);
            await ExpectAlert(ADDED_MESSAGE, "success");
            ExpectFieldValues(BLANK_USER);
        },
    );
});

describe("COUSR01C PF4 (CLEAR-CURRENT-SCREEN) on /admin/users/add", () => {
    it.each(PRESS_METHODS)(
        "COUSR01C PF4 by %s blanks every field and the message",
        async (method) => {
            const user = RenderScreen();
            await FillUser(user, KEYED_USER);
            await PressLegend(user, "F12", "key");
            await ExpectAlert(INVALID_KEY_MESSAGE, "error");
            await PressLegend(user, "F4", method);
            ExpectFieldValues(BLANK_USER);
            expect(screen.queryByRole("alert")).toBeNull();
            expect(FieldInput("first_name")).toHaveFocus();
            ExpectNoCallOrNavigation();
        },
    );
});

describe("COUSR01C PF3 (XCTL COADM01C) on /admin/users/add", () => {
    it.each(PRESS_METHODS)("COUSR01C PF3 by %s returns to the admin menu", async (method) => {
        const user = RenderScreen();
        await FillUser(user, KEYED_USER);
        await PressLegend(user, "F3", method);
        expect(mockRouterPush).toHaveBeenCalledTimes(1);
        expect(mockRouterPush).toHaveBeenCalledWith(ADMIN_MENU_ROUTE);
        expect(mockRouterReplace).not.toHaveBeenCalled();
        expect(mockAddUser).not.toHaveBeenCalled();
    });
});

describe("COUSR01C session refusal (EIBCALEN = 0) on /admin/users/add", () => {
    it("COUSR01C leaves a 401 to the API client's sign-on redirect", async () => {
        mockAddUser.mockRejectedValue(BuildAxiosError(HTTP_UNAUTHORIZED, BuildErrorBody({})));
        const user = RenderScreen();
        await FillUser(user, KEYED_USER);
        await PressLegend(user, "ENTER", "key");
        // The refusal names no field, so the failure path returns the cursor to FNAME.
        await waitFor(() => expect(FieldInput("first_name")).toHaveFocus());
        expect(screen.queryByRole("alert")).toBeNull();
        expect(LegendButton("ENTER")).toBeEnabled();
        expect(mockRouterPush).not.toHaveBeenCalled();
        expect(mockRouterReplace).not.toHaveBeenCalled();
        expect(SIGN_ON_ROUTE).toBe("/signon");
    });
});
