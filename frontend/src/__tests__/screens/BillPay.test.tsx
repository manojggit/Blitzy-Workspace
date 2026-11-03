// Screen tests: BMS COBIL00, program COBIL00C (CB00), page /billpay.
// Literals and HTTP statuses are copied verbatim from backend/app/services/billpay/cobil00c.py
// and cbl/COBIL00C.cbl; field prompts and lengths from bms/COBIL00.bms.
import { alertClasses } from "@mui/material";
import { ThemeProvider } from "@mui/material/styles";
import { render, screen, waitFor, within } from "@testing-library/react";
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
    type MoneyAmount,
} from "@/lib/apiClient";
import type { SessionContextValue } from "@/lib/session";

type BillPayInputName = "acct_id" | "confirm";

type MapKey = "ENTER" | "F3" | "F4";

type KeyMethod = "button" | "physical key";

// Every CB00 legacy error body carries CURBAL beside the four standard keys.
interface BillPayErrorBody extends LegacyErrorResponse {
    current_balance: MoneyAmount | null;
}

interface LiteralCase {
    request: BillPayRequest;
    status: number;
    message: string;
    severity: MessageSeverity;
    cursorField: BillPayInputName;
    tranId: string | null;
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
const ZERO_BALANCE = "0.00";
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
        severity: "info",
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

function BuildErrorBody(overrides: Partial<BillPayErrorBody> = {}): BillPayErrorBody {
    return {
        message: "",
        info_message: "",
        field_errors: [],
        cursor_field: "",
        current_balance: null,
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

// A written payment ran INITIALIZE-ALL-FIELDS, so its screen carries no CURBAL; an error body
// names its one failing field, as COBIL00C edits in first-failure mode.
function MockOutcome({ status, message, cursorField, tranId }: LiteralCase): void {
    if (status === HTTP_OK) {
        const balance = tranId === null ? TEST_BALANCE : null;
        const overrides = { message, cursor_field: cursorField, tran_id: tranId };
        mockPayBill.mockResolvedValue(
            BuildBillPayResponse({ ...overrides, current_balance: balance }),
        );
        return;
    }
    const body = BuildErrorBody({
        message,
        field_errors: [cursorField],
        cursor_field: cursorField,
    });
    mockPayBill.mockRejectedValue(BuildAxiosError(status, body));
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
    await ExpectAlert(CONFIRM_PAYMENT_MESSAGE, "info");
}

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

it("COBIL00 ACTIDIN (11), CURBAL (ASKIP) and CONFIRM (1, Y/N) keep their BMS attributes", () => {
    RenderScreen();
    expect(InputOf("acct_id")).toHaveAttribute("maxlength", "11");
    expect(InputOf("confirm")).toHaveAttribute("maxlength", "1");
    expect(screen.getByText(CONFIRM_HINT)).toBeInTheDocument();
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
    await ExpectAlert(CONFIRM_PAYMENT_MESSAGE, "info");
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
        await ExpectAlert(CONFIRM_PAYMENT_MESSAGE, "info");
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

it("COBIL00C nothing to pay keeps CURBAL from the error body", async () => {
    const body = BuildErrorBody({
        message: NOTHING_TO_PAY_MESSAGE,
        field_errors: ["acct_id"],
        cursor_field: "acct_id",
        current_balance: ZERO_BALANCE,
    });
    mockPayBill.mockRejectedValue(BuildAxiosError(HTTP_EDIT_FAILURE, body));
    const user = RenderScreen();
    await KeyRequest(user, PROMPT_REQUEST);
    await UseMapKey(user, "ENTER", "button");
    await ExpectAlert(NOTHING_TO_PAY_MESSAGE, "error");
    expect(BalanceField()).toHaveValue(ZERO_BALANCE);
    expect(InputOf("acct_id")).toHaveValue(TEST_ACCOUNT_ID);
});

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

it("COBIL00C N answer: CLEAR-CURRENT-SCREEN blanks every field and the message", async () => {
    const user = RenderScreen();
    await ShowConfirmPrompt(user);
    const cleared = { current_balance: null, message: "", cursor_field: "acct_id" };
    mockPayBill.mockResolvedValue(BuildBillPayResponse(cleared));
    await KeyInto(user, "confirm", "N");
    await UseMapKey(user, "ENTER", "button");
    await waitFor(() => {
        expect(screen.queryByRole("alert")).toBeNull();
    });
    expect(mockPayBill).toHaveBeenLastCalledWith({ acct_id: TEST_ACCOUNT_ID, confirm: "N" });
    expect(InputOf("acct_id")).toHaveValue("");
    expect(BalanceField()).toHaveValue("");
    expect(InputOf("acct_id")).toHaveFocus();
});

it("COBIL00C unexpected 500 shows the CSMSG02Y abend text", async () => {
    mockPayBill.mockRejectedValue(
        BuildAxiosError(HTTP_INTERNAL_SERVER_ERROR, "Internal Server Error"),
    );
    const user = RenderScreen();
    await KeyRequest(user, PAY_REQUEST);
    await UseMapKey(user, "ENTER", "button");
    await ExpectAlert(ABEND_MESSAGE_TEXT, "error");
    expect(InputOf("acct_id")).toHaveValue(TEST_ACCOUNT_ID);
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
