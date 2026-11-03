// Screen tests: BMS COBIL00, program COBIL00C (CB00), page /billpay.
// Literals and HTTP statuses are copied verbatim from backend/app/services/billpay/cobil00c.py
// and cbl/COBIL00C.cbl; field prompts and lengths from bms/COBIL00.bms.
import { alertClasses } from "@mui/material";
import { ThemeProvider } from "@mui/material/styles";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import {
    AxiosError,
    AxiosHeaders,
    type AxiosResponse,
    type InternalAxiosRequestConfig,
} from "axios";

import { CARD_DEMO_THEME } from "@/app/theme";
import BillPayPage from "@/app/billpay/page";
import { PayBill, type BillPayRequest, type BillPayResponse } from "@/lib/api/billpay";
import {
    HandleResponseError,
    SIGN_ON_ROUTE,
    type LegacyErrorResponse,
    type MessageSeverity,
} from "@/lib/apiClient";
import type { SessionContextValue } from "@/lib/session";

type BillPayInputName = "acct_id" | "confirm";

type MapKey = "ENTER" | "F3" | "F4";

type KeyMethod = "button" | "physical key";

interface LiteralCase {
    request: BillPayRequest;
    status: number;
    message: string;
    severity: MessageSeverity;
    cursorField: BillPayInputName;
    tranId: string | null;
}

interface PendingPayBill {
    Answer: (response: BillPayResponse) => void;
    Fail: (error: unknown) => void;
}

// How the payment request out settles, what the screen then shows, and what the next ENTER sends.
interface LockReleaseCase {
    outcome: string;
    SettlePayBill: (pendingPayBill: PendingPayBill) => void;
    message: string;
    severity: MessageSeverity;
    nextRequest: BillPayRequest;
}

const mockRouterPush = jest.fn();
const mockRouterReplace = jest.fn();
// One router object for every render, as Next.js keeps the App Router instance stable.
const mockRouter = {
    push: mockRouterPush,
    replace: mockRouterReplace,
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};
// COBIL00C reads no query parameter, so the search parameters stay empty.
const mockSearchParams = new URLSearchParams();
const mockPathname = "/billpay";

// The session user carries an ID and names only; type U is a regular user (COMEN01C tree).
const mockSessionValue: SessionContextValue = {
    currentUser: {
        user_id: "TESTUSR1",
        user_type: "U",
        first_name: "Test",
        last_name: "User",
        next_route: "/menu",
    },
    isLoading: false,
    RefreshSession: jest.fn(),
    ClearSession: jest.fn(),
};

jest.mock("next/navigation", () => ({
    useRouter: () => mockRouter,
    usePathname: () => mockPathname,
    useSearchParams: () => mockSearchParams,
}));

jest.mock("@/lib/session", () => ({
    useSession: () => mockSessionValue,
}));

jest.mock("@/lib/api/billpay", () => ({
    ...jest.requireActual<object>("@/lib/api/billpay"),
    PayBill: jest.fn(),
}));

const mockPayBill = jest.mocked(PayBill);

const PAGE_ROUTE = "/billpay";
const MENU_ROUTE = "/menu";
const BILL_PAY_PATH = "/billpay";

// The map's row-4 title, TRNNAME, PGMNAME and its prompts without the trailing blanks.
const SCREEN_TITLE = "Bill Payment";
const TRANSACTION_LINE = "Tran: CB00";
const PROGRAM_LINE = "Prog: COBIL00C";
const FIELD_LABELS: Readonly<Record<BillPayInputName, string>> = {
    acct_id: "Enter Acct ID:",
    confirm: "Do you want to pay your balance now. Please confirm:",
};
const BALANCE_LABEL = "Your current balance is:";
const CONFIRM_HINT = "Y/N";

// ENTER=Continue  F3=Back  F4=Clear, as labelled and keyed on the page.
const MAP_KEYS: Readonly<Record<MapKey, { button: string; key: string }>> = {
    ENTER: { button: "Continue (Enter)", key: "Enter" },
    F3: { button: "Back (F3)", key: "F3" },
    F4: { button: "Clear (F4)", key: "F4" },
};
const LEGEND_LABELS = [MAP_KEYS.ENTER.button, MAP_KEYS.F3.button, MAP_KEYS.F4.button];
const KEY_METHODS: readonly KeyMethod[] = ["button", "physical key"];

const TEST_ACCOUNT_ID = "00000000004";
const UNKNOWN_ACCOUNT_ID = "99999999999";
const TEST_BALANCE = "194.00";
const TEST_TRAN_ID = "0000000000000002";

// cbl/COBIL00C.cbl lines 161-543, as the cobil00c.py module constants hold them.
const ACCT_ID_EMPTY_MESSAGE = "Acct ID can NOT be empty...";
const INVALID_CONFIRM_MESSAGE = "Invalid value. Valid values are (Y/N)...";
const ACCOUNT_NOT_FOUND_MESSAGE = "Account ID NOT found...";
const ACCOUNT_LOOKUP_FAILED_MESSAGE = "Unable to lookup Account...";
const NOTHING_TO_PAY_MESSAGE = "You have nothing to pay...";
const CONFIRM_PAYMENT_MESSAGE = "Confirm to make a bill payment...";
const XREF_LOOKUP_FAILED_MESSAGE = "Unable to lookup XREF AIX file...";
const TRANSACTION_LOOKUP_FAILED_MESSAGE = "Unable to lookup Transaction...";
const TRAN_ID_EXISTS_MESSAGE = "Tran ID already exist...";
const ADD_PAYMENT_FAILED_MESSAGE = "Unable to Add Bill pay Transaction...";
const UPDATE_ACCOUNT_FAILED_MESSAGE = "Unable to Update Account...";
// The STRING of 'Payment successful. ' and ' Your Transaction ID is ' keeps two spaces.
const PAYMENT_SUCCESS_MESSAGE = `Payment successful.  Your Transaction ID is ${TEST_TRAN_ID}.`;
// CSMSG02Y ABEND-MSG, shown for a 500 because the backend registers no catch-all handler.
const ABEND_MESSAGE_TEXT = "UNEXPECTED ABEND OCCURRED.";
// jsdom implements no navigation, so window.location.assign reports this through console.error.
const NAVIGATION_NOT_IMPLEMENTED = "Not implemented: navigation";

// backend/app/core/errors.py statuses, by message kind.
const HTTP_OK = 200;
const HTTP_EDIT_FAILURE = 400;
const HTTP_SIGN_ON_FAILURE = 401;
const HTTP_NOT_FOUND = 404;
const HTTP_RECORD_CHANGED = 409;
const HTTP_INTERNAL_SERVER_ERROR = 500;
const HTTP_FILE_ERROR = 503;

const SEVERITY_ALERT_CLASSES: Readonly<Record<MessageSeverity, string>> = {
    error: alertClasses.colorError,
    success: alertClasses.colorSuccess,
    info: alertClasses.colorInfo,
};

// The screens as keyed: blank account, prompt (blank CONFIRM), pay (Y) and an invalid answer.
const BLANK_REQUEST: BillPayRequest = { acct_id: "", confirm: "" };
const PROMPT_REQUEST: BillPayRequest = { acct_id: TEST_ACCOUNT_ID, confirm: "" };
const PAY_REQUEST: BillPayRequest = { acct_id: TEST_ACCOUNT_ID, confirm: "Y" };
const INVALID_CONFIRM_REQUEST: BillPayRequest = { acct_id: TEST_ACCOUNT_ID, confirm: "X" };
const UNKNOWN_ACCOUNT_REQUEST: BillPayRequest = { acct_id: UNKNOWN_ACCOUNT_ID, confirm: "" };

// A screen sent with ERR-FLG set: the literal, its status and ACTIDIN or CONFIRM as the cursor.
function ErrorCase(
    status: number,
    message: string,
    cursorField: BillPayInputName,
    request: BillPayRequest,
): LiteralCase {
    return { request, status, message, severity: "error", cursorField, tranId: null };
}

// Every literal COBIL00C sends: first-failure edits, file conditions, the prompt and the payment.
// ERRMSG is COLOR=RED, so the prompt sent with HTTP 200 is red too; only the written payment
// moves DFHGREEN to ERRMSGC.
const LITERAL_CASES: readonly LiteralCase[] = [
    ErrorCase(HTTP_EDIT_FAILURE, ACCT_ID_EMPTY_MESSAGE, "acct_id", BLANK_REQUEST),
    ErrorCase(HTTP_EDIT_FAILURE, INVALID_CONFIRM_MESSAGE, "confirm", INVALID_CONFIRM_REQUEST),
    ErrorCase(HTTP_NOT_FOUND, ACCOUNT_NOT_FOUND_MESSAGE, "acct_id", UNKNOWN_ACCOUNT_REQUEST),
    ErrorCase(HTTP_FILE_ERROR, ACCOUNT_LOOKUP_FAILED_MESSAGE, "acct_id", PROMPT_REQUEST),
    ErrorCase(HTTP_EDIT_FAILURE, NOTHING_TO_PAY_MESSAGE, "acct_id", PROMPT_REQUEST),
    ErrorCase(HTTP_FILE_ERROR, XREF_LOOKUP_FAILED_MESSAGE, "acct_id", PAY_REQUEST),
    ErrorCase(HTTP_FILE_ERROR, TRANSACTION_LOOKUP_FAILED_MESSAGE, "acct_id", PAY_REQUEST),
    ErrorCase(HTTP_RECORD_CHANGED, TRAN_ID_EXISTS_MESSAGE, "acct_id", PAY_REQUEST),
    ErrorCase(HTTP_FILE_ERROR, ADD_PAYMENT_FAILED_MESSAGE, "acct_id", PAY_REQUEST),
    ErrorCase(HTTP_FILE_ERROR, UPDATE_ACCOUNT_FAILED_MESSAGE, "acct_id", PAY_REQUEST),
    {
        request: PROMPT_REQUEST,
        status: HTTP_OK,
        message: CONFIRM_PAYMENT_MESSAGE,
        severity: "error",
        cursorField: "confirm",
        tranId: null,
    },
    {
        request: PAY_REQUEST,
        status: HTTP_OK,
        message: PAYMENT_SUCCESS_MESSAGE,
        severity: "success",
        cursorField: "acct_id",
        tranId: TEST_TRAN_ID,
    },
];

// Errors keyed on the prompt screen: an invalid answer, and a duplicate TRAN-ID after Y.
const INVALID_CONFIRM_CASE = ErrorCase(
    HTTP_EDIT_FAILURE,
    INVALID_CONFIRM_MESSAGE,
    "confirm",
    INVALID_CONFIRM_REQUEST,
);
const DUPLICATE_TRAN_ID_CASE = ErrorCase(
    HTTP_RECORD_CHANGED,
    TRAN_ID_EXISTS_MESSAGE,
    "acct_id",
    PAY_REQUEST,
);
const KEPT_BALANCE_CASES: readonly LiteralCase[] = [INVALID_CONFIRM_CASE, DUPLICATE_TRAN_ID_CASE];

// The id of the MessageLine ERRMSG Alert, which a field named in field_errors is described by.
const ERRMSG_ID = "message-line-errmsg";

// An edit failure naming its cursor field, that field's hint, if any, and the other input with
// the description it keeps.
interface FlaggedFieldCase extends LiteralCase {
    hint: string | null;
    unflaggedName: BillPayInputName;
    unflaggedDescription: string;
}

// ACTIDIN has no hint; CONFIRM keeps its Y/N hint after the ERRMSG line, and as the unflagged
// field it is described by that hint alone.
const FLAGGED_FIELD_CASES: readonly FlaggedFieldCase[] = [
    {
        ...ErrorCase(HTTP_EDIT_FAILURE, ACCT_ID_EMPTY_MESSAGE, "acct_id", BLANK_REQUEST),
        hint: null,
        unflaggedName: "confirm",
        unflaggedDescription: CONFIRM_HINT,
    },
    {
        ...INVALID_CONFIRM_CASE,
        hint: CONFIRM_HINT,
        unflaggedName: "acct_id",
        unflaggedDescription: "",
    },
];

// The confirmation prompt by default: CURBAL shown, the cursor on CONFIRM, nothing written.
function BuildBillPayResponse(overrides: Partial<BillPayResponse> = {}): BillPayResponse {
    return {
        current_balance: TEST_BALANCE,
        message: CONFIRM_PAYMENT_MESSAGE,
        cursor_field: "confirm",
        tran_id: null,
        ...overrides,
    };
}

// A CB00 legacy error body holds exactly the four standard keys; CURBAL is not among them.
function BuildErrorBody(overrides: Partial<LegacyErrorResponse> = {}): LegacyErrorResponse {
    return {
        message: "",
        info_message: "",
        field_errors: [],
        cursor_field: "",
        ...overrides,
    };
}

// The rejection axios raises for a non-2xx answer to POST /billpay.
function BuildAxiosError(status: number, body: unknown): AxiosError {
    const config: InternalAxiosRequestConfig = {
        url: BILL_PAY_PATH,
        method: "post",
        headers: new AxiosHeaders(),
    };
    const response: AxiosResponse = { status, data: body, statusText: "", headers: {}, config };
    const code = status < HTTP_INTERNAL_SERVER_ERROR ? "ERR_BAD_REQUEST" : "ERR_BAD_RESPONSE";
    return new AxiosError("Request failed", code, config, undefined, response);
}

// An error body names its one failing field, as COBIL00C edits in first-failure mode.
function FailureOf({ status, message, cursorField }: LiteralCase): AxiosError {
    const body = BuildErrorBody({
        message,
        field_errors: [cursorField],
        cursor_field: cursorField,
    });
    return BuildAxiosError(status, body);
}

// A written payment ran INITIALIZE-ALL-FIELDS, so its screen carries no CURBAL.
function MockOutcome(literalCase: LiteralCase): void {
    const { status, message, cursorField, tranId } = literalCase;
    if (status === HTTP_OK) {
        const balance = tranId === null ? TEST_BALANCE : null;
        const overrides = { message, cursor_field: cursorField, tran_id: tranId };
        mockPayBill.mockResolvedValue(
            BuildBillPayResponse({ ...overrides, current_balance: balance }),
        );
        return;
    }
    mockPayBill.mockRejectedValue(FailureOf(literalCase));
}

function RenderScreen(): UserEvent {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <BillPayPage />
        </ThemeProvider>,
    );
    return user;
}

function InputOf(name: BillPayInputName): HTMLElement {
    return screen.getByLabelText(FIELD_LABELS[name]);
}

function BalanceField(): HTMLElement {
    return screen.getByLabelText(BALANCE_LABEL);
}

// Nothing typed for a blank value: the field keeps its initial empty text.
async function KeyInto(user: UserEvent, name: BillPayInputName, text: string): Promise<void> {
    if (text === "") {
        return;
    }
    await user.type(InputOf(name), text);
}

async function KeyRequest(user: UserEvent, request: BillPayRequest): Promise<void> {
    await KeyInto(user, "acct_id", request.acct_id ?? "");
    await KeyInto(user, "confirm", request.confirm ?? "");
}

// A physical key goes to the focused element and bubbles to the useFunctionKeys listener.
async function PressKey(user: UserEvent, keyName: string): Promise<void> {
    await user.keyboard(`{${keyName}}`);
}

async function UseMapKey(user: UserEvent, mapKey: MapKey, method: KeyMethod): Promise<void> {
    if (method === "button") {
        await user.click(screen.getByRole("button", { name: MAP_KEYS[mapKey].button }));
        return;
    }
    await PressKey(user, MAP_KEYS[mapKey].key);
}

// The raw textContent keeps the literal's double spaces, which text matchers normalise.
async function ExpectAlert(text: string, severity: MessageSeverity): Promise<void> {
    await waitFor(() => {
        expect(screen.getByRole("alert").textContent).toBe(text);
    });
    expect(screen.getByRole("alert")).toHaveClass(SEVERITY_ALERT_CLASSES[severity]);
}

// The busy bar shows from ENTER until the answer has been applied.
async function WaitUntilIdle(): Promise<void> {
    await waitFor(() => {
        expect(screen.queryByRole("progressbar")).toBeNull();
    });
}

function IsNavigationAttempt(call: unknown[]): boolean {
    const firstArgument = call[0];
    const text = firstArgument instanceof Error ? firstArgument.message : String(firstArgument);
    return text.includes(NAVIGATION_NOT_IMPLEMENTED);
}

// Shows the confirm prompt with CURBAL, so a later key can be seen to clear it.
async function ShowConfirmPrompt(user: UserEvent): Promise<void> {
    mockPayBill.mockResolvedValueOnce(BuildBillPayResponse());
    await KeyRequest(user, PROMPT_REQUEST);
    await UseMapKey(user, "ENTER", "button");
    await ExpectAlert(CONFIRM_PAYMENT_MESSAGE, "error");
}

// The next PayBill call stays pending until the test answers or fails it through these handles.
function DeferPayBill(): PendingPayBill {
    const pending: PendingPayBill = { Answer: () => undefined, Fail: () => undefined };
    mockPayBill.mockImplementationOnce(
        () =>
            new Promise<BillPayResponse>((resolve, reject) => {
                pending.Answer = resolve;
                pending.Fail = reject;
            }),
    );
    return pending;
}

// Settles the pending call inside act, so the page applies the outcome before the assertions.
async function Settle(settleCall: () => void): Promise<void> {
    await act(async () => {
        settleCall();
    });
}

// The 3270 keyboard lock: ACTIDIN, CONFIRM, ENTER and F4 are frozen, F3 still leaves.
function ExpectKeyboardLocked(): void {
    expect(InputOf("acct_id")).toBeDisabled();
    expect(InputOf("confirm")).toBeDisabled();
    expect(screen.getByRole("button", { name: MAP_KEYS.ENTER.button })).toBeDisabled();
    expect(screen.getByRole("button", { name: MAP_KEYS.F4.button })).toBeDisabled();
    expect(screen.getByRole("button", { name: MAP_KEYS.F3.button })).toBeEnabled();
}

// Typing and the physical F4 while the call runs change nothing on the screen that was sent.
async function TryToEditWhileLocked(user: UserEvent, confirmSent: string): Promise<void> {
    await user.type(InputOf("acct_id"), UNKNOWN_ACCOUNT_ID);
    await user.type(InputOf("confirm"), "N");
    await PressKey(user, MAP_KEYS.F4.key);
    expect(InputOf("acct_id")).toHaveValue(TEST_ACCOUNT_ID);
    expect(InputOf("confirm")).toHaveValue(confirmSent);
}

// Physical keys reach the useFunctionKeys window listener. They all run inside one act, so no
// render, and no effect handing the keys their disabled state, runs between two presses. The
// pointer first leaves the inputs: an input focused when the busy render disables it would have
// MUI's FormControl update its focus state during act's final flush, outside act.
async function PressInOneTurn(user: UserEvent, keyNames: readonly string[]): Promise<void> {
    await user.click(document.body);
    await act(async () => {
        for (const keyName of keyNames) {
            window.dispatchEvent(new KeyboardEvent("keydown", { key: keyName, bubbles: true }));
        }
    });
}

// The confirmed payment screen as sent: the account, the Y answer and the CURBAL shown.
function ExpectPaymentScreenKept(): void {
    expect(InputOf("acct_id")).toHaveValue(TEST_ACCOUNT_ID);
    expect(InputOf("confirm")).toHaveValue("Y");
    expect(BalanceField()).toHaveValue(TEST_BALANCE);
}

// A second ENTER, or an F4, pressed before the busy render while the confirmed payment is out.
const BURST_SECOND_KEYS: readonly MapKey[] = ["ENTER", "F4"];

const WRITTEN_PAYMENT: Partial<BillPayResponse> = {
    current_balance: null,
    message: PAYMENT_SUCCESS_MESSAGE,
    cursor_field: "acct_id",
    tran_id: TEST_TRAN_ID,
};

// A written payment blanks the screen, so the next ENTER sends blank fields; the duplicate
// TRAN-ID keeps the screen sent, so the next ENTER sends it again.
const LOCK_RELEASE_CASES: readonly LockReleaseCase[] = [
    {
        outcome: "a written payment",
        SettlePayBill: (pendingPayBill) =>
            pendingPayBill.Answer(BuildBillPayResponse(WRITTEN_PAYMENT)),
        message: PAYMENT_SUCCESS_MESSAGE,
        severity: "success",
        nextRequest: BLANK_REQUEST,
    },
    {
        outcome: "a 409 rejection",
        SettlePayBill: (pendingPayBill) => pendingPayBill.Fail(FailureOf(DUPLICATE_TRAN_ID_CASE)),
        message: TRAN_ID_EXISTS_MESSAGE,
        severity: "error",
        nextRequest: PAY_REQUEST,
    },
];

beforeEach(() => {
    mockPayBill.mockReset();
    window.history.pushState({}, "", PAGE_ROUTE);
});

afterEach(() => {
    jest.restoreAllMocks();
});

it("COBIL00 shows TRNNAME CB00, PGMNAME COBIL00C and the title Bill Payment", () => {
    RenderScreen();
    const banner = screen.getByRole("banner");
    expect(banner).toContainElement(screen.getByText(TRANSACTION_LINE));
    expect(banner).toContainElement(screen.getByText(PROGRAM_LINE));
    expect(screen.getByRole("heading", { level: 1, name: SCREEN_TITLE })).toBeVisible();
});

it("COBIL00 ACTIDIN (11), CURBAL (14, ASKIP) and CONFIRM (1, Y/N) keep BMS attributes", () => {
    RenderScreen();
    expect(InputOf("acct_id")).toHaveAttribute("maxlength", "11");
    expect(InputOf("confirm")).toHaveAttribute("maxlength", "1");
    expect(screen.getByText(CONFIRM_HINT)).toBeInTheDocument();
    expect(BalanceField()).toHaveAttribute("maxlength", "14");
    expect(BalanceField()).toHaveAttribute("readonly");
    expect(BalanceField()).toHaveValue("");
});

it("COBIL00 legend ENTER=Continue F3=Back F4=Clear, cursor on ACTIDIN (IC)", () => {
    RenderScreen();
    const legend = screen.getByRole("group", { name: "Function keys" });
    const labels = within(legend)
        .getAllByRole("button")
        .map((button) => button.textContent);
    expect(labels).toStrictEqual(LEGEND_LABELS);
    expect(InputOf("acct_id")).toHaveFocus();
});

it("COBIL00C ENTER key with a blank CONFIRM shows CURBAL and the confirm prompt", async () => {
    mockPayBill.mockResolvedValue(BuildBillPayResponse());
    const user = RenderScreen();
    await KeyRequest(user, PROMPT_REQUEST);
    await PressKey(user, MAP_KEYS.ENTER.key);
    await ExpectAlert(CONFIRM_PAYMENT_MESSAGE, "error");
    expect(mockPayBill).toHaveBeenCalledWith({ acct_id: TEST_ACCOUNT_ID, confirm: "" });
    expect(BalanceField()).toHaveValue(TEST_BALANCE);
    expect(InputOf("confirm")).toHaveFocus();
});

it.each(KEY_METHODS)(
    "COBIL00C ENTER by %s sends the confirm character unchanged",
    async (method) => {
        mockPayBill.mockResolvedValue(BuildBillPayResponse());
        const user = RenderScreen();
        await KeyRequest(user, { acct_id: TEST_ACCOUNT_ID, confirm: "y" });
        await UseMapKey(user, "ENTER", method);
        await ExpectAlert(CONFIRM_PAYMENT_MESSAGE, "error");
        expect(mockPayBill).toHaveBeenCalledTimes(1);
        expect(mockPayBill).toHaveBeenCalledWith({ acct_id: TEST_ACCOUNT_ID, confirm: "y" });
    },
);

it.each(LITERAL_CASES)(
    "COBIL00C answers $status with $message as $severity, cursor on $cursorField",
    async (literalCase) => {
        MockOutcome(literalCase);
        const user = RenderScreen();
        await KeyRequest(user, literalCase.request);
        await UseMapKey(user, "ENTER", "button");
        await ExpectAlert(literalCase.message, literalCase.severity);
        expect(mockPayBill).toHaveBeenCalledWith(literalCase.request);
        expect(InputOf(literalCase.cursorField)).toHaveFocus();
    },
);

// CURBAL is ASKIP,FSET: an error body carries no balance, so the screen keeps the one shown.
it.each(KEPT_BALANCE_CASES)(
    "COBIL00C $status $message keeps the CURBAL shown and the keyed fields",
    async (literalCase) => {
        const user = RenderScreen();
        await ShowConfirmPrompt(user);
        MockOutcome(literalCase);
        await KeyInto(user, "confirm", literalCase.request.confirm ?? "");
        await UseMapKey(user, "ENTER", "button");
        await ExpectAlert(literalCase.message, "error");
        expect(mockPayBill).toHaveBeenLastCalledWith(literalCase.request);
        expect(BalanceField()).toHaveValue(TEST_BALANCE);
        expect(InputOf("acct_id")).toHaveValue(TEST_ACCOUNT_ID);
        expect(InputOf("confirm")).toHaveValue(literalCase.request.confirm);
    },
);

// The account typed while the call runs cannot reach the screen, so the balance shown always
// belongs to the account that was sent; the unlocked CONFIRM then takes the cursor.
it("COBIL00C keyboard lock: nothing is keyed while PayBill runs", async () => {
    const pendingPayBill = DeferPayBill();
    const user = RenderScreen();
    await KeyRequest(user, PROMPT_REQUEST);
    await UseMapKey(user, "ENTER", "button");
    ExpectKeyboardLocked();
    await TryToEditWhileLocked(user, "");
    await Settle(() => pendingPayBill.Answer(BuildBillPayResponse()));
    await ExpectAlert(CONFIRM_PAYMENT_MESSAGE, "error");
    expect(InputOf("acct_id")).toBeEnabled();
    expect(InputOf("confirm")).toBeEnabled();
    expect(InputOf("confirm")).toHaveFocus();
    expect(BalanceField()).toHaveValue(TEST_BALANCE);
    expect(InputOf("acct_id")).toHaveValue(TEST_ACCOUNT_ID);
    expect(mockPayBill).toHaveBeenCalledTimes(1);
});

// F4 cannot clear while the call runs, so a late answer never repaints a cleared screen; the
// cursor goes to the field the error names once the keyboard unlocks.
it("COBIL00C PF4 is locked while PayBill runs, so its answer applies to the screen sent", async () => {
    const user = RenderScreen();
    await ShowConfirmPrompt(user);
    const pendingPayBill = DeferPayBill();
    await KeyInto(user, "confirm", "Y");
    await UseMapKey(user, "ENTER", "button");
    ExpectKeyboardLocked();
    await TryToEditWhileLocked(user, "Y");
    await Settle(() => pendingPayBill.Fail(FailureOf(DUPLICATE_TRAN_ID_CASE)));
    await ExpectAlert(TRAN_ID_EXISTS_MESSAGE, "error");
    expect(mockPayBill).toHaveBeenLastCalledWith(PAY_REQUEST);
    expect(BalanceField()).toHaveValue(TEST_BALANCE);
    expect(InputOf("acct_id")).toHaveFocus();
    expect(screen.getByRole("button", { name: MAP_KEYS.F4.button })).toBeEnabled();
});

// The keys disable a render after ENTER, so a second ENTER or an F4 pressed in that gap meets the
// in-flight lock: one payment command is sent, and its answer lands on the screen that was sent.
it.each(BURST_SECOND_KEYS)(
    "COBIL00C ENTER and %s in one turn send one payment and keep the screen sent",
    async (secondKey) => {
        const user = RenderScreen();
        await ShowConfirmPrompt(user);
        const pendingPayBill = DeferPayBill();
        await KeyInto(user, "confirm", "Y");
        await PressInOneTurn(user, [MAP_KEYS.ENTER.key, MAP_KEYS[secondKey].key]);
        expect(mockPayBill).toHaveBeenCalledTimes(2);
        expect(mockPayBill).toHaveBeenLastCalledWith(PAY_REQUEST);
        ExpectKeyboardLocked();
        ExpectPaymentScreenKept();
        await Settle(() => pendingPayBill.Fail(FailureOf(DUPLICATE_TRAN_ID_CASE)));
        await ExpectAlert(TRAN_ID_EXISTS_MESSAGE, "error");
        ExpectPaymentScreenKept();
        expect(mockPayBill).toHaveBeenCalledTimes(2);
    },
);

// The lock clears once the request settles, answered or rejected, so the next ENTER is sent.
it.each(LOCK_RELEASE_CASES)(
    "COBIL00C the in-flight lock clears after $outcome, so the next ENTER is sent",
    async ({ SettlePayBill, message, severity, nextRequest }) => {
        const user = RenderScreen();
        await ShowConfirmPrompt(user);
        const pendingPayBill = DeferPayBill();
        await KeyInto(user, "confirm", "Y");
        await PressInOneTurn(user, [MAP_KEYS.ENTER.key]);
        await Settle(() => SettlePayBill(pendingPayBill));
        await ExpectAlert(message, severity);
        DeferPayBill();
        await PressInOneTurn(user, [MAP_KEYS.ENTER.key]);
        expect(mockPayBill).toHaveBeenCalledTimes(3);
        expect(mockPayBill).toHaveBeenLastCalledWith(nextRequest);
    },
);

it("COBIL00C payment written: INITIALIZE-ALL-FIELDS blanks every field", async () => {
    const user = RenderScreen();
    await ShowConfirmPrompt(user);
    const written = { current_balance: null, cursor_field: "acct_id", tran_id: TEST_TRAN_ID };
    mockPayBill.mockResolvedValue(
        BuildBillPayResponse({ ...written, message: PAYMENT_SUCCESS_MESSAGE }),
    );
    await KeyInto(user, "confirm", "Y");
    await UseMapKey(user, "ENTER", "button");
    await ExpectAlert(PAYMENT_SUCCESS_MESSAGE, "success");
    expect(mockPayBill).toHaveBeenLastCalledWith(PAY_REQUEST);
    expect(InputOf("acct_id")).toHaveValue("");
    expect(InputOf("confirm")).toHaveValue("");
    expect(BalanceField()).toHaveValue("");
});

// CONFIRMI WHEN 'N' WHEN 'n' runs CLEAR-CURRENT-SCREEN; INITIALIZE-ALL-FIELDS blanks CONFIRM too.
it.each(["N", "n"])(
    "COBIL00C %s answer: CLEAR-CURRENT-SCREEN blanks every field and the message",
    async (answer) => {
        const user = RenderScreen();
        await ShowConfirmPrompt(user);
        const cleared = { current_balance: null, message: "", cursor_field: "acct_id" };
        mockPayBill.mockResolvedValue(BuildBillPayResponse(cleared));
        await KeyInto(user, "confirm", answer);
        await UseMapKey(user, "ENTER", "button");
        await waitFor(() => {
            expect(screen.queryByRole("alert")).toBeNull();
        });
        expect(mockPayBill).toHaveBeenLastCalledWith({ acct_id: TEST_ACCOUNT_ID, confirm: answer });
        expect(InputOf("acct_id")).toHaveValue("");
        expect(InputOf("confirm")).toHaveValue("");
        expect(BalanceField()).toHaveValue("");
        expect(InputOf("acct_id")).toHaveFocus();
    },
);

// The abend names no field, so the unlocked keyboard puts the cursor on ACTIDIN (BMS IC).
it("COBIL00C unexpected 500 shows the CSMSG02Y abend text", async () => {
    mockPayBill.mockRejectedValue(
        BuildAxiosError(HTTP_INTERNAL_SERVER_ERROR, "Internal Server Error"),
    );
    const user = RenderScreen();
    await KeyRequest(user, PAY_REQUEST);
    await UseMapKey(user, "ENTER", "button");
    await ExpectAlert(ABEND_MESSAGE_TEXT, "error");
    expect(InputOf("acct_id")).toHaveValue(TEST_ACCOUNT_ID);
    expect(InputOf("acct_id")).toHaveFocus();
});

it("COBIL00C field_errors mark every named field and the cursor goes to cursor_field", async () => {
    const body = BuildErrorBody({
        message: ACCT_ID_EMPTY_MESSAGE,
        field_errors: ["acct_id", "confirm"],
        cursor_field: "acct_id",
    });
    mockPayBill.mockRejectedValue(BuildAxiosError(HTTP_EDIT_FAILURE, body));
    const user = RenderScreen();
    expect(InputOf("acct_id")).toHaveAttribute("aria-invalid", "false");
    await UseMapKey(user, "ENTER", "button");
    await ExpectAlert(ACCT_ID_EMPTY_MESSAGE, "error");
    expect(InputOf("acct_id")).toHaveAttribute("aria-invalid", "true");
    expect(InputOf("confirm")).toHaveAttribute("aria-invalid", "true");
    expect(InputOf("acct_id")).toHaveFocus();
});

// The flagged field names the ERRMSG Alert holding the literal first, then its own hint.
it.each(FLAGGED_FIELD_CASES)(
    "COBIL00C $status $message describes $cursorField by the ERRMSG line",
    async (fieldCase) => {
        MockOutcome(fieldCase);
        const user = RenderScreen();
        await KeyRequest(user, fieldCase.request);
        await UseMapKey(user, "ENTER", "button");
        await ExpectAlert(fieldCase.message, "error");
        const input = InputOf(fieldCase.cursorField);
        const hintIds = fieldCase.hint === null ? [] : [`${input.id}-helper-text`];
        const hintTexts = fieldCase.hint === null ? [] : [fieldCase.hint];
        expect(input).toHaveAttribute("aria-describedby", [ERRMSG_ID, ...hintIds].join(" "));
        expect(document.getElementById(ERRMSG_ID)).toBe(screen.getByRole("alert"));
        expect(input).toHaveAccessibleDescription([fieldCase.message, ...hintTexts].join(" "));
        expect(InputOf(fieldCase.unflaggedName)).toHaveAccessibleDescription(
            fieldCase.unflaggedDescription,
        );
    },
);

it.each(KEY_METHODS)(
    "COBIL00C PF4 by %s runs CLEAR-CURRENT-SCREEN without a call",
    async (method) => {
        const user = RenderScreen();
        await ShowConfirmPrompt(user);
        await KeyInto(user, "confirm", "Y");
        await UseMapKey(user, "F4", method);
        expect(InputOf("acct_id")).toHaveValue("");
        expect(InputOf("confirm")).toHaveValue("");
        expect(BalanceField()).toHaveValue("");
        expect(screen.queryByRole("alert")).toBeNull();
        expect(InputOf("acct_id")).toHaveFocus();
        expect(mockPayBill).toHaveBeenCalledTimes(1);
    },
);

it.each(KEY_METHODS)("COBIL00C PF3 by %s returns to COMEN01C at /menu", async (method) => {
    const user = RenderScreen();
    await KeyRequest(user, PAY_REQUEST);
    await UseMapKey(user, "F3", method);
    expect(mockRouterPush).toHaveBeenCalledTimes(1);
    expect(mockRouterPush).toHaveBeenCalledWith(MENU_ROUTE);
    expect(mockRouterReplace).not.toHaveBeenCalled();
    expect(mockPayBill).not.toHaveBeenCalled();
});

// The session guard (EIBCALEN = 0) is the backend 401; the apiClient interceptor owns the
// redirect, so the page shows the body's empty message and routes nowhere itself.
it("COBIL00C 401 session refusal goes to sign-on through the apiClient interceptor", async () => {
    const consoleErrorSpy = jest.spyOn(console, "error").mockImplementation(() => undefined);
    const refusal = BuildAxiosError(HTTP_SIGN_ON_FAILURE, BuildErrorBody());
    mockPayBill.mockImplementation(() => HandleResponseError(refusal));
    const user = RenderScreen();
    await KeyRequest(user, PAY_REQUEST);
    await UseMapKey(user, "ENTER", "button");
    await WaitUntilIdle();
    expect(SIGN_ON_ROUTE).toBe("/signon");
    expect(consoleErrorSpy.mock.calls.some(IsNavigationAttempt)).toBe(true);
    expect(consoleErrorSpy.mock.calls.every(IsNavigationAttempt)).toBe(true);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(mockRouterPush).not.toHaveBeenCalled();
});
