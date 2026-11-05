// Screen tests: BMS COTRN02, program COTRN02C (CT02), page /transactions/add.
// Literals and HTTP statuses are copied verbatim from cbl/COTRN02C.cbl and
// backend/app/services/transactions/cotrn02c.py; labels, hints and input lengths from
// bms/COTRN02.bms. The in-flight lock, the ERRMSG description tie and the restored focus outline
// have their own TransactionAdd*.test.tsx suites.
import { alertClasses, inputBaseClasses } from "@mui/material";
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
import TransactionAddPage from "@/app/transactions/add/page";
import {
    AddTransaction,
    TransactionAddAction,
    type TransactionAddFields,
    type TransactionAddRequest,
    type TransactionAddResponse,
} from "@/lib/api/transactions";
import {
    HandleResponseError,
    SIGN_ON_ROUTE,
    type LegacyErrorResponse,
    type MessageSeverity,
} from "@/lib/apiClient";
import type { SessionContextValue } from "@/lib/session";

// Every COTRN2A input, by its JSON key.
type AddInputName = keyof TransactionAddFields;

// The whole screen: every input with its text, blank included.
type AddScreenValues = Required<TransactionAddFields>;

type MapKey = "ENTER" | "F3" | "F4" | "F5";

type KeyMethod = "button" | "physical key";

// One COTRN2A input: its BMS LENGTH and the format hint the map shows beside it, if any.
interface FieldCase {
    name: AddInputName;
    maxLength: number;
    hint: string | null;
}

// How ENTER is pressed and which CONFIRMI character is keyed.
interface EnterCase {
    method: KeyMethod;
    confirm: string;
}

// One COTRN02C outcome: the screen keyed, the HTTP status, the literal, its colour and cursor,
// and the fields the answer marks in error.
interface LiteralCase {
    input: AddScreenValues;
    status: number;
    message: string;
    severity: MessageSeverity;
    cursorField: AddInputName;
    fieldErrors: readonly AddInputName[];
}

// One required map field and the literal its blank entry returns.
interface RequiredFieldMessage {
    name: AddInputName;
    message: string;
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
// COTRN02C reads no query parameter, so the search parameters stay empty.
const mockSearchParams = new URLSearchParams();

// A regular user (type U), as CT02 is main-menu option 8; the session holds no password.
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
    usePathname: () => "/transactions/add",
    useSearchParams: () => mockSearchParams,
}));

jest.mock("@/lib/session", () => ({
    useSession: () => mockSessionValue,
}));

// Only the network calls are replaced; TransactionAddAction and the types stay real.
jest.mock("@/lib/api/transactions", () => ({
    ...jest.requireActual<object>("@/lib/api/transactions"),
    ListTransactions: jest.fn(),
    SelectTransactions: jest.fn(),
    ViewTransaction: jest.fn(),
    AddTransaction: jest.fn(),
}));

const mockAddTransaction = jest.mocked(AddTransaction);

const PAGE_ROUTE = "/transactions/add";
const MENU_ROUTE = "/menu";
const TRANSACTIONS_PATH = "/transactions";

// The map's row-4 title, TRNNAME, PGMNAME and the ASKIP text between the two keys.
const SCREEN_TITLE = "Add Transaction";
const TRANSACTION_LINE = "Tran: CT02";
const PROGRAM_LINE = "Prog: COTRN02C";
const OR_SEPARATOR_TEXT = "(or)";

// The COTRN2A INITIAL label texts; CONFIRMI is named by the row-21 prompt.
const FIELD_LABELS: Readonly<Record<AddInputName, string>> = {
    acct_id: "Enter Acct #:",
    card_num: "Card #:",
    type_cd: "Type CD:",
    cat_cd: "Category CD:",
    source: "Source:",
    desc: "Description:",
    amount: "Amount:",
    orig_date: "Orig Date:",
    proc_date: "Proc Date:",
    merchant_id: "Merchant ID:",
    merchant_name: "Merchant Name:",
    merchant_city: "Merchant City:",
    merchant_zip: "Merchant Zip:",
    confirm: "You are about to add this transaction. Please confirm :",
};

const AMOUNT_HINT = "(-99999999.99)";
const DATE_HINT = "(YYYY-MM-DD)";
// ConfirmField shows the map's (Y/N) text as the field's helper text.
const CONFIRM_HINT = "Y/N";

// Every input in map order, with the LENGTH of cpy-bms/COTRN02.CPY and its hint.
const FIELD_CASES: readonly FieldCase[] = [
    { name: "acct_id", maxLength: 11, hint: null },
    { name: "card_num", maxLength: 16, hint: null },
    { name: "type_cd", maxLength: 2, hint: null },
    { name: "cat_cd", maxLength: 4, hint: null },
    { name: "source", maxLength: 10, hint: null },
    { name: "desc", maxLength: 60, hint: null },
    { name: "amount", maxLength: 12, hint: AMOUNT_HINT },
    { name: "orig_date", maxLength: 10, hint: DATE_HINT },
    { name: "proc_date", maxLength: 10, hint: DATE_HINT },
    { name: "merchant_id", maxLength: 9, hint: null },
    { name: "merchant_name", maxLength: 30, hint: null },
    { name: "merchant_city", maxLength: 25, hint: null },
    { name: "merchant_zip", maxLength: 10, hint: null },
    { name: "confirm", maxLength: 1, hint: CONFIRM_HINT },
];

const FIELD_NAMES: readonly AddInputName[] = FIELD_CASES.map((fieldCase) => fieldCase.name);

// ENTER=Continue  F3=Back  F4=Clear  F5=Copy Last Tran., as labelled and keyed on the page.
const MAP_KEYS: Readonly<Record<MapKey, { button: string; key: string }>> = {
    ENTER: { button: "Continue (Enter)", key: "Enter" },
    F3: { button: "Back (F3)", key: "F3" },
    F4: { button: "Clear (F4)", key: "F4" },
    F5: { button: "Copy Last Tran. (F5)", key: "F5" },
};
const LEGEND_LABELS = Object.values(MAP_KEYS).map((mapKey) => mapKey.button);
const KEY_METHODS: readonly KeyMethod[] = ["button", "physical key"];

const TEST_ACCOUNT_ID = "00000000004";
const UNKNOWN_ACCOUNT_ID = "99999999999";
// Key texts at their full BMS LENGTH (11 and 16): IS NUMERIC fails on the last character.
const NON_NUMERIC_ACCOUNT_ID = "0000000000A";
const NON_NUMERIC_CARD_NUMBER = "000000000000000A";
// Neutral 16-digit card keys that pass IS NUMERIC: one keyed card and one on no xref record.
const KEYED_CARD_NUMBER = "0000000000000004";
const UNKNOWN_CARD_NUMBER = "9999999999999999";
// The date-format edit fails on the separators; CSUTLDTC rejects 30 February (CEEDAYS 2508).
const NOT_ISO_FORMAT_DATE = "2022/07/17";
const NON_CALENDAR_DATE = "2022-02-30";
const ADDED_TRAN_ID = "0000000000000001";
// The xref card COTRN02C moves into CARDNINI, as the server's MaskCardNumber sends it back.
const SERVED_CARD_MASKED = "************4312";

// cbl/COTRN02C.cbl lines 164-470 and 600-745, as the cotrn02c.py module constants hold them.
const ACCOUNT_OR_CARD_REQUIRED_MESSAGE = "Account or Card Number must be entered...";
const ACCOUNT_NOT_NUMERIC_MESSAGE = "Account ID must be Numeric...";
const CARD_NOT_NUMERIC_MESSAGE = "Card Number must be Numeric...";
const ACCOUNT_NOT_FOUND_MESSAGE = "Account ID NOT found...";
const CARD_NOT_FOUND_MESSAGE = "Card Number NOT found...";
const ACCOUNT_XREF_LOOKUP_FAILED_MESSAGE = "Unable to lookup Acct in XREF AIX file...";
const CARD_XREF_LOOKUP_FAILED_MESSAGE = "Unable to lookup Card # in XREF file...";
const TYPE_REQUIRED_MESSAGE = "Type CD can NOT be empty...";
const TYPE_NOT_NUMERIC_MESSAGE = "Type CD must be Numeric...";
const CATEGORY_NOT_NUMERIC_MESSAGE = "Category CD must be Numeric...";
const AMOUNT_FORMAT_MESSAGE = "Amount should be in format -99999999.99";
const ORIG_DATE_FORMAT_MESSAGE = "Orig Date should be in format YYYY-MM-DD";
const PROC_DATE_FORMAT_MESSAGE = "Proc Date should be in format YYYY-MM-DD";
const ORIG_DATE_INVALID_MESSAGE = "Orig Date - Not a valid date...";
const PROC_DATE_INVALID_MESSAGE = "Proc Date - Not a valid date...";
const MERCHANT_ID_NOT_NUMERIC_MESSAGE = "Merchant ID must be Numeric...";
const CONFIRM_PROMPT_MESSAGE = "Confirm to add this transaction...";
const CONFIRM_INVALID_MESSAGE = "Invalid value. Valid values are (Y/N)...";
const TRAN_LOOKUP_FAILED_MESSAGE = "Unable to lookup Transaction...";
const DUPLICATE_TRAN_ID_MESSAGE = "Tran ID already exist...";
const ADD_FAILED_MESSAGE = "Unable to Add Transaction...";
// The presence edits after Type CD, in PRESENCE_CHECKS order, each literal as the
// REQUIRED_FIELD_TEMPLATE writes it for the field's label.
const REQUIRED_FIELD_MESSAGES: readonly RequiredFieldMessage[] = [
    { name: "cat_cd", message: "Category CD can NOT be empty..." },
    { name: "source", message: "Source can NOT be empty..." },
    { name: "desc", message: "Description can NOT be empty..." },
    { name: "amount", message: "Amount can NOT be empty..." },
    { name: "orig_date", message: "Orig Date can NOT be empty..." },
    { name: "proc_date", message: "Proc Date can NOT be empty..." },
    { name: "merchant_id", message: "Merchant ID can NOT be empty..." },
    { name: "merchant_name", message: "Merchant Name can NOT be empty..." },
    { name: "merchant_city", message: "Merchant City can NOT be empty..." },
    { name: "merchant_zip", message: "Merchant Zip can NOT be empty..." },
];
// The STRING of 'Transaction added successfully. ' and ' Your Tran ID is ' keeps two spaces.
const ADDED_MESSAGE = `Transaction added successfully.  Your Tran ID is ${ADDED_TRAN_ID}.`;
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

// A frozen clock keeps the ScreenShell CURDATE/CURTIME store unchanged, so its one-second tick
// never re-renders the header outside act().
const FIXED_NOW_MILLISECONDS = new Date(2022, 6, 18, 9, 5, 7).getTime();

const SEVERITY_ALERT_CLASSES: Readonly<Record<MessageSeverity, string>> = {
    error: alertClasses.colorError,
    success: alertClasses.colorSuccess,
    info: alertClasses.colorInfo,
};

// INITIALIZE-ALL-FIELDS: every input blank, CONFIRMI included.
const BLANK_SCREEN: AddScreenValues = {
    acct_id: "",
    card_num: "",
    type_cd: "",
    cat_cd: "",
    source: "",
    desc: "",
    amount: "",
    orig_date: "",
    proc_date: "",
    merchant_id: "",
    merchant_name: "",
    merchant_city: "",
    merchant_zip: "",
    confirm: "",
};

// The day-in-the-life add: keyed by account with the card left blank, and confirmed.
const E2E_LIKE_INPUT: AddScreenValues = {
    acct_id: TEST_ACCOUNT_ID,
    card_num: "",
    type_cd: "01",
    cat_cd: "0001",
    source: "POS TERM",
    desc: "TEST PURCHASE",
    amount: "+00000050.00",
    orig_date: "2022-07-17",
    proc_date: "2022-07-17",
    merchant_id: "000000001",
    merchant_name: "TEST MERCHANT",
    merchant_city: "TEST CITY",
    merchant_zip: "00000",
    confirm: "Y",
};

// F5 keyed with the account alone: every other field blank, CONFIRMI included.
const COPY_LAST_INPUT: AddScreenValues = { ...BLANK_SCREEN, acct_id: TEST_ACCOUNT_ID };

// The screen COPY-LAST-TRAN-DATA sends back with the prompt: the validated account, the masked
// xref card and the last transaction's data fields, its amount edited as +99999999.99.
const COPIED_SCREEN: AddScreenValues = {
    acct_id: TEST_ACCOUNT_ID,
    card_num: SERVED_CARD_MASKED,
    type_cd: "02",
    cat_cd: "0002",
    source: "OPERATOR",
    desc: "LAST TRANSACTION",
    amount: "+00000012.34",
    orig_date: "2022-06-10",
    proc_date: "2022-06-11",
    merchant_id: "800000001",
    merchant_name: "LAST MERCHANT",
    merchant_city: "LAST CITY",
    merchant_zip: "12345",
    confirm: "",
};

// F5 keyed with the card alone: the blank account sends VALIDATE-INPUT-KEY-FIELDS down the card
// path, every other field blank, CONFIRMI included.
const CARD_COPY_LAST_INPUT: AddScreenValues = { ...BLANK_SCREEN, card_num: KEYED_CARD_NUMBER };

// KEYED_CARD_NUMBER as MaskCardNumber sends it back: twelve asterisks and its last four digits.
const KEYED_CARD_MASKED = "************0004";

// The screen the card-keyed F5 gets back: the account the CCXREF read resolved and the copied
// data fields, beside the keyed card masked.
const CARD_COPIED_SCREEN: AddScreenValues = { ...COPIED_SCREEN, card_num: KEYED_CARD_MASKED };

// CONFIRMI WHEN 'Y' WHEN 'y' both add, so either character goes out exactly as keyed.
const ENTER_CASES: readonly EnterCase[] = [
    { method: "button", confirm: "Y" },
    { method: "physical key", confirm: "Y" },
    { method: "button", confirm: "y" },
    { method: "physical key", confirm: "y" },
];

// The flat COTRN02 body the page sends: the key pressed beside the 14 fields as keyed.
function RequestOf(action: TransactionAddAction, values: AddScreenValues): TransactionAddRequest {
    return { action, ...values };
}

// The confirm prompt by default: E2E_LIKE_INPUT with CONFIRMI blank, as COTRN02C sends it back
// with the xref card masked and nothing written.
function BuildAddResponse(overrides: Partial<TransactionAddResponse> = {}): TransactionAddResponse {
    return {
        message: CONFIRM_PROMPT_MESSAGE,
        cursor_field: "confirm",
        fields: { ...E2E_LIKE_INPUT, card_num: SERVED_CARD_MASKED, confirm: "" },
        tran_id: null,
        ...overrides,
    };
}

// The written transaction: INITIALIZE-ALL-FIELDS blanked the screen, the cursor on ACTIDIN.
const ADDED_RESPONSE = BuildAddResponse({
    message: ADDED_MESSAGE,
    cursor_field: "acct_id",
    fields: BLANK_SCREEN,
    tran_id: ADDED_TRAN_ID,
});

const COPIED_PROMPT_RESPONSE = BuildAddResponse({ fields: COPIED_SCREEN });

const CARD_COPIED_PROMPT_RESPONSE = BuildAddResponse({ fields: CARD_COPIED_SCREEN });

// A CT02 legacy error body holds exactly the four standard keys; the screen stays as keyed.
function BuildErrorBody(overrides: Partial<LegacyErrorResponse> = {}): LegacyErrorResponse {
    return { message: "", info_message: "", field_errors: [], cursor_field: "", ...overrides };
}

// The rejection axios raises for a non-2xx answer to POST /transactions.
function BuildAxiosError(status: number, body: unknown): AxiosError {
    const config: InternalAxiosRequestConfig = {
        url: TRANSACTIONS_PATH,
        method: "post",
        headers: new AxiosHeaders(),
    };
    const response: AxiosResponse = { status, data: body, statusText: "", headers: {}, config };
    const code = status < HTTP_INTERNAL_SERVER_ERROR ? "ERR_BAD_REQUEST" : "ERR_BAD_RESPONSE";
    return new AxiosError("Request failed", code, config, undefined, response);
}

// A screen sent with WS-ERR-FLG set. ERRMSG is COLOR=RED and the confirm prompt sets the flag as
// well, so every outcome but the written transaction (DFHGREEN) is red. A rejection's
// field_errors names its one failing field, the cursor field.
function ErrorFlagCase(
    status: number,
    message: string,
    cursorField: AddInputName,
    input: AddScreenValues,
): LiteralCase {
    return { input, status, message, severity: "error", cursorField, fieldErrors: [cursorField] };
}

// The 200 confirm prompt is red too, but a 200 answer names no field in error.
function ConfirmPromptCase(input: AddScreenValues): LiteralCase {
    return {
        ...ErrorFlagCase(HTTP_OK, CONFIRM_PROMPT_MESSAGE, "confirm", input),
        fieldErrors: [],
    };
}

// A blank account sends VALIDATE-INPUT-KEY-FIELDS down the card path, as a keyed account
// takes precedence over the card.
function CardKeyedInput(cardNumber: string): AddScreenValues {
    return { ...E2E_LIKE_INPUT, acct_id: "", card_num: cardNumber };
}

// Each later required field left blank, every earlier edit passing.
const REQUIRED_FIELD_CASES: readonly LiteralCase[] = REQUIRED_FIELD_MESSAGES.map(
    ({ name, message }) =>
        ErrorFlagCase(HTTP_EDIT_FAILURE, message, name, { ...E2E_LIKE_INPUT, [name]: "" }),
);

// Every literal COTRN02C returns but the green added literal, which the ENTER cases assert, in
// source order, each keyed so that every earlier edit passes: the key edits, NOTFND and xref
// WHEN OTHER literals on the account path, then on the card path; the eleven required fields;
// the type, category, amount, date-format, CSUTLDTC and merchant ID edits; the invalid confirm
// value; the TRANSACT browse, DUPREC and WRITE literals; and the prompt sent with 200.
const LITERAL_CASES: readonly LiteralCase[] = [
    ErrorFlagCase(HTTP_EDIT_FAILURE, ACCOUNT_OR_CARD_REQUIRED_MESSAGE, "acct_id", {
        ...E2E_LIKE_INPUT,
        acct_id: "",
    }),
    ErrorFlagCase(HTTP_EDIT_FAILURE, ACCOUNT_NOT_NUMERIC_MESSAGE, "acct_id", {
        ...E2E_LIKE_INPUT,
        acct_id: NON_NUMERIC_ACCOUNT_ID,
    }),
    ErrorFlagCase(HTTP_NOT_FOUND, ACCOUNT_NOT_FOUND_MESSAGE, "acct_id", {
        ...E2E_LIKE_INPUT,
        acct_id: UNKNOWN_ACCOUNT_ID,
    }),
    ErrorFlagCase(HTTP_FILE_ERROR, ACCOUNT_XREF_LOOKUP_FAILED_MESSAGE, "acct_id", E2E_LIKE_INPUT),
    ErrorFlagCase(
        HTTP_EDIT_FAILURE,
        CARD_NOT_NUMERIC_MESSAGE,
        "card_num",
        CardKeyedInput(NON_NUMERIC_CARD_NUMBER),
    ),
    ErrorFlagCase(
        HTTP_NOT_FOUND,
        CARD_NOT_FOUND_MESSAGE,
        "card_num",
        CardKeyedInput(UNKNOWN_CARD_NUMBER),
    ),
    ErrorFlagCase(
        HTTP_FILE_ERROR,
        CARD_XREF_LOOKUP_FAILED_MESSAGE,
        "card_num",
        CardKeyedInput(KEYED_CARD_NUMBER),
    ),
    ErrorFlagCase(HTTP_EDIT_FAILURE, TYPE_REQUIRED_MESSAGE, "type_cd", {
        ...E2E_LIKE_INPUT,
        type_cd: "",
    }),
    ...REQUIRED_FIELD_CASES,
    ErrorFlagCase(HTTP_EDIT_FAILURE, TYPE_NOT_NUMERIC_MESSAGE, "type_cd", {
        ...E2E_LIKE_INPUT,
        type_cd: "0A",
    }),
    ErrorFlagCase(HTTP_EDIT_FAILURE, CATEGORY_NOT_NUMERIC_MESSAGE, "cat_cd", {
        ...E2E_LIKE_INPUT,
        cat_cd: "000A",
    }),
    ErrorFlagCase(HTTP_EDIT_FAILURE, AMOUNT_FORMAT_MESSAGE, "amount", {
        ...E2E_LIKE_INPUT,
        amount: "50.00",
    }),
    ErrorFlagCase(HTTP_EDIT_FAILURE, ORIG_DATE_FORMAT_MESSAGE, "orig_date", {
        ...E2E_LIKE_INPUT,
        orig_date: NOT_ISO_FORMAT_DATE,
    }),
    ErrorFlagCase(HTTP_EDIT_FAILURE, PROC_DATE_FORMAT_MESSAGE, "proc_date", {
        ...E2E_LIKE_INPUT,
        proc_date: NOT_ISO_FORMAT_DATE,
    }),
    ErrorFlagCase(HTTP_EDIT_FAILURE, ORIG_DATE_INVALID_MESSAGE, "orig_date", {
        ...E2E_LIKE_INPUT,
        orig_date: NON_CALENDAR_DATE,
    }),
    ErrorFlagCase(HTTP_EDIT_FAILURE, PROC_DATE_INVALID_MESSAGE, "proc_date", {
        ...E2E_LIKE_INPUT,
        proc_date: NON_CALENDAR_DATE,
    }),
    ErrorFlagCase(HTTP_EDIT_FAILURE, MERCHANT_ID_NOT_NUMERIC_MESSAGE, "merchant_id", {
        ...E2E_LIKE_INPUT,
        merchant_id: "00000000A",
    }),
    ErrorFlagCase(HTTP_EDIT_FAILURE, CONFIRM_INVALID_MESSAGE, "confirm", {
        ...E2E_LIKE_INPUT,
        confirm: "X",
    }),
    ErrorFlagCase(HTTP_FILE_ERROR, TRAN_LOOKUP_FAILED_MESSAGE, "acct_id", E2E_LIKE_INPUT),
    ErrorFlagCase(HTTP_RECORD_CHANGED, DUPLICATE_TRAN_ID_MESSAGE, "acct_id", E2E_LIKE_INPUT),
    ErrorFlagCase(HTTP_FILE_ERROR, ADD_FAILED_MESSAGE, "acct_id", E2E_LIKE_INPUT),
    ConfirmPromptCase({ ...E2E_LIKE_INPUT, confirm: "" }),
];

// An error body names its one failing field, as COTRN02C edits in first-failure mode.
function FailureOf({ status, message, cursorField }: LiteralCase): AxiosError {
    const body = BuildErrorBody({
        message,
        field_errors: [cursorField],
        cursor_field: cursorField,
    });
    return BuildAxiosError(status, body);
}

// A 200 is the confirm prompt with the screen sent back; any other status is a rejected call.
function MockOutcome(literalCase: LiteralCase): void {
    const { status, message, cursorField } = literalCase;
    if (status === HTTP_OK) {
        mockAddTransaction.mockResolvedValue(
            BuildAddResponse({ message, cursor_field: cursorField }),
        );
        return;
    }
    mockAddTransaction.mockRejectedValue(FailureOf(literalCase));
}

// delay null keys each character back to back with no timer between keystrokes, because
// FillForm keys a long form: E2E_LIKE_INPUT alone is over 100 characters.
function RenderScreen(): UserEvent {
    const user = userEvent.setup({ delay: null });
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <TransactionAddPage />
        </ThemeProvider>,
    );
    return user;
}

function InputOf(name: AddInputName): HTMLElement {
    return screen.getByLabelText(FIELD_LABELS[name]);
}

// Nothing typed for a blank value: the field keeps its initial empty text.
async function KeyInto(user: UserEvent, name: AddInputName, text: string): Promise<void> {
    if (text === "") {
        return;
    }
    await user.type(InputOf(name), text);
}

// Keys every input in map order, as the user tabs down the screen.
async function FillForm(user: UserEvent, values: AddScreenValues): Promise<void> {
    for (const name of FIELD_NAMES) {
        await KeyInto(user, name, values[name]);
    }
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

// The busy bar shows from the key press until the answer has been applied.
async function WaitUntilIdle(): Promise<void> {
    await waitFor(() => {
        expect(screen.queryByRole("progressbar")).toBeNull();
    });
}

function ExpectScreenValues(values: AddScreenValues): void {
    for (const name of FIELD_NAMES) {
        expect(InputOf(name)).toHaveValue(values[name]);
    }
}

// Exactly the fields the answer names in field_errors read as invalid; every other is valid.
function ExpectMarkedFields(fieldErrors: readonly AddInputName[]): void {
    for (const name of FIELD_NAMES) {
        expect(InputOf(name)).toHaveAttribute("aria-invalid", String(fieldErrors.includes(name)));
    }
}

function IsNavigationAttempt(call: unknown[]): boolean {
    const firstArgument = call[0];
    const text = firstArgument instanceof Error ? firstArgument.message : String(firstArgument);
    return text.includes(NAVIGATION_NOT_IMPLEMENTED);
}

// Shows the prompt for E2E_LIKE_INPUT with CONFIRMI blank, the resolved card shown masked.
async function ShowConfirmPrompt(user: UserEvent): Promise<void> {
    mockAddTransaction.mockResolvedValueOnce(BuildAddResponse());
    await FillForm(user, { ...E2E_LIKE_INPUT, confirm: "" });
    await UseMapKey(user, "ENTER", "button");
    await ExpectAlert(CONFIRM_PROMPT_MESSAGE, "error");
    expect(InputOf("card_num")).toHaveValue(SERVED_CARD_MASKED);
}

// F5 keyed with the card alone: the request carries the blank account and the card as keyed, and
// the screen shows the resolved account, the copied fields and the card masked.
async function CopyLastByCard(user: UserEvent, method: KeyMethod): Promise<void> {
    await FillForm(user, CARD_COPY_LAST_INPUT);
    await UseMapKey(user, "F5", method);
    await ExpectAlert(CONFIRM_PROMPT_MESSAGE, "error");
    expect(mockAddTransaction).toHaveBeenCalledTimes(1);
    expect(mockAddTransaction).toHaveBeenCalledWith(
        RequestOf(TransactionAddAction.COPY_LAST, CARD_COPY_LAST_INPUT),
    );
    ExpectScreenValues(CARD_COPIED_SCREEN);
    expect(InputOf("confirm")).toHaveFocus();
}

beforeEach(() => {
    mockAddTransaction.mockReset();
    jest.spyOn(Date, "now").mockReturnValue(FIXED_NOW_MILLISECONDS);
    window.sessionStorage.clear();
    window.history.pushState({}, "", PAGE_ROUTE);
});

afterEach(() => {
    jest.restoreAllMocks();
});

it("COTRN02 shows TRNNAME CT02, PGMNAME COTRN02C and the title Add Transaction", () => {
    RenderScreen();
    const banner = screen.getByRole("banner");
    expect(banner).toContainElement(screen.getByText(TRANSACTION_LINE));
    expect(banner).toContainElement(screen.getByText(PROGRAM_LINE));
    expect(screen.getByRole("heading", { level: 1, name: SCREEN_TITLE })).toBeVisible();
});

it.each(FIELD_CASES)(
    "COTRN02 $name keeps its BMS LENGTH $maxLength and its hint $hint",
    ({ name, maxLength, hint }) => {
        RenderScreen();
        const input = InputOf(name);
        expect(input).toHaveAttribute("maxlength", String(maxLength));
        expect(input).toHaveValue("");
        expect(input).toBeEnabled();
        expect(input).toHaveAccessibleDescription(hint ?? "");
    },
);

// MUI's default medium field is the field height of every other map; no input is size small.
it("COTRN02 shows every input, CONFIRM included, at MUI's default medium size", () => {
    RenderScreen();
    const inputs = FIELD_NAMES.map(InputOf);
    expect(screen.getAllByRole("textbox")).toHaveLength(inputs.length);
    for (const input of inputs) {
        expect(input.closest(`.${inputBaseClasses.root}`)).not.toHaveClass(
            inputBaseClasses.sizeSmall,
        );
    }
});

it("COTRN02 legend ENTER/F3/F4/F5, (or) between the keys, cursor on ACTIDIN (IC)", () => {
    RenderScreen();
    const legend = screen.getByRole("group", { name: "Function keys" });
    const labels = within(legend)
        .getAllByRole("button")
        .map((button) => button.textContent);
    expect(labels).toStrictEqual(LEGEND_LABELS);
    expect(screen.getByText(OR_SEPARATOR_TEXT)).toBeVisible();
    expect(InputOf("acct_id")).toHaveFocus();
});

it.each(ENTER_CASES)(
    "COTRN02C ENTER by $method sends confirm $confirm and every field as keyed",
    async ({ method, confirm }) => {
        mockAddTransaction.mockResolvedValue(ADDED_RESPONSE);
        const keyedScreen: AddScreenValues = { ...E2E_LIKE_INPUT, confirm };
        const user = RenderScreen();
        await FillForm(user, keyedScreen);
        await UseMapKey(user, "ENTER", method);
        await ExpectAlert(ADDED_MESSAGE, "success");
        expect(mockAddTransaction).toHaveBeenCalledTimes(1);
        expect(mockAddTransaction).toHaveBeenCalledWith(
            RequestOf(TransactionAddAction.ENTER, keyedScreen),
        );
    },
);

it("COTRN02C added: INITIALIZE-ALL-FIELDS blanks every field, the cursor on ACTIDIN", async () => {
    const user = RenderScreen();
    await ShowConfirmPrompt(user);
    mockAddTransaction.mockResolvedValueOnce(ADDED_RESPONSE);
    await KeyInto(user, "confirm", "Y");
    await UseMapKey(user, "ENTER", "button");
    await ExpectAlert(ADDED_MESSAGE, "success");
    ExpectScreenValues(BLANK_SCREEN);
    expect(InputOf("acct_id")).toHaveFocus();
});

// A PF key never moved the 3270 cursor: Continue, reached by Tab from CONFIRM and pressed with
// Enter, gives the focus back to CONFIRM once the call disables it, never to the page body, until
// the answer puts the cursor on ACTIDIN.
it("COTRN02C Enter on Continue tabbed to from CONFIRM keeps CONFIRM focused", async () => {
    const { promise, resolve } = Promise.withResolvers<TransactionAddResponse>();
    mockAddTransaction.mockReturnValueOnce(promise);
    const user = RenderScreen();
    await FillForm(user, E2E_LIKE_INPUT);
    const continueButton = screen.getByRole("button", { name: MAP_KEYS.ENTER.button });
    await user.tab();
    expect(continueButton).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(continueButton).toBeDisabled();
    expect(document.activeElement).toBe(InputOf("confirm"));
    await act(async () => {
        resolve(ADDED_RESPONSE);
    });
    await ExpectAlert(ADDED_MESSAGE, "success");
    expect(mockAddTransaction).toHaveBeenCalledWith(
        RequestOf(TransactionAddAction.ENTER, E2E_LIKE_INPUT),
    );
    expect(InputOf("acct_id")).toHaveFocus();
});

it.each(KEY_METHODS)(
    "COTRN02C F5 by %s sends copy_last with the entered account and shows the copied fields",
    async (method) => {
        mockAddTransaction.mockResolvedValue(COPIED_PROMPT_RESPONSE);
        const user = RenderScreen();
        await FillForm(user, COPY_LAST_INPUT);
        await UseMapKey(user, "F5", method);
        await ExpectAlert(CONFIRM_PROMPT_MESSAGE, "error");
        expect(mockAddTransaction).toHaveBeenCalledTimes(1);
        expect(mockAddTransaction).toHaveBeenCalledWith(
            RequestOf(TransactionAddAction.COPY_LAST, COPY_LAST_INPUT),
        );
        ExpectScreenValues(COPIED_SCREEN);
        expect(InputOf("confirm")).toHaveFocus();
    },
);

// The server masks the card it resolved, so the page keeps the card as keyed for the next key.
it("COTRN02C ENTER after F5 sends the copied screen with the card as keyed", async () => {
    mockAddTransaction.mockResolvedValueOnce(COPIED_PROMPT_RESPONSE);
    mockAddTransaction.mockResolvedValueOnce(ADDED_RESPONSE);
    const user = RenderScreen();
    await FillForm(user, COPY_LAST_INPUT);
    await UseMapKey(user, "F5", "button");
    await ExpectAlert(CONFIRM_PROMPT_MESSAGE, "error");
    await KeyInto(user, "confirm", "Y");
    await UseMapKey(user, "ENTER", "button");
    await ExpectAlert(ADDED_MESSAGE, "success");
    const confirmedScreen: AddScreenValues = { ...COPIED_SCREEN, card_num: "", confirm: "Y" };
    expect(mockAddTransaction).toHaveBeenLastCalledWith(
        RequestOf(TransactionAddAction.ENTER, confirmedScreen),
    );
});

it.each(KEY_METHODS)(
    "COTRN02C F5 by %s sends copy_last with the entered card and ENTER resends it as keyed",
    async (method) => {
        mockAddTransaction.mockResolvedValueOnce(CARD_COPIED_PROMPT_RESPONSE);
        mockAddTransaction.mockResolvedValueOnce(ADDED_RESPONSE);
        const user = RenderScreen();
        await CopyLastByCard(user, method);
        await KeyInto(user, "confirm", "Y");
        await UseMapKey(user, "ENTER", "button");
        await ExpectAlert(ADDED_MESSAGE, "success");
        const confirmedScreen: AddScreenValues = {
            ...CARD_COPIED_SCREEN,
            card_num: KEYED_CARD_NUMBER,
            confirm: "Y",
        };
        expect(mockAddTransaction).toHaveBeenCalledTimes(2);
        expect(mockAddTransaction).toHaveBeenLastCalledWith(
            RequestOf(TransactionAddAction.ENTER, confirmedScreen),
        );
    },
);

it.each(LITERAL_CASES)(
    "COTRN02C answers $status with $message as $severity, cursor on $cursorField",
    async (literalCase) => {
        MockOutcome(literalCase);
        const user = RenderScreen();
        await FillForm(user, literalCase.input);
        await UseMapKey(user, "ENTER", "button");
        await ExpectAlert(literalCase.message, literalCase.severity);
        expect(mockAddTransaction).toHaveBeenCalledWith(
            RequestOf(TransactionAddAction.ENTER, literalCase.input),
        );
        expect(InputOf("acct_id")).toHaveValue(literalCase.input.acct_id);
        expect(InputOf("amount")).toHaveValue(literalCase.input.amount);
        expect(InputOf(literalCase.cursorField)).toHaveFocus();
        ExpectMarkedFields(literalCase.fieldErrors);
    },
);

it("COTRN02C field_errors mark every named field and the cursor goes to cursor_field", async () => {
    const body = BuildErrorBody({
        message: AMOUNT_FORMAT_MESSAGE,
        field_errors: ["amount", "orig_date"],
        cursor_field: "amount",
    });
    mockAddTransaction.mockRejectedValue(BuildAxiosError(HTTP_EDIT_FAILURE, body));
    const user = RenderScreen();
    await FillForm(user, E2E_LIKE_INPUT);
    expect(InputOf("amount")).toHaveAttribute("aria-invalid", "false");
    await UseMapKey(user, "ENTER", "button");
    await ExpectAlert(AMOUNT_FORMAT_MESSAGE, "error");
    expect(InputOf("amount")).toHaveAttribute("aria-invalid", "true");
    expect(InputOf("orig_date")).toHaveAttribute("aria-invalid", "true");
    expect(InputOf("proc_date")).toHaveAttribute("aria-invalid", "false");
    expect(InputOf("amount")).toHaveFocus();
});

// CLEAR-CURRENT-SCREEN reads no file: F4 blanks the fields, the masked card and ERRMSG locally.
it.each(KEY_METHODS)(
    "COTRN02C PF4 by %s runs CLEAR-CURRENT-SCREEN without a call",
    async (method) => {
        const user = RenderScreen();
        await ShowConfirmPrompt(user);
        await KeyInto(user, "confirm", "Y");
        await UseMapKey(user, "F4", method);
        ExpectScreenValues(BLANK_SCREEN);
        expect(screen.queryByRole("alert")).toBeNull();
        expect(InputOf("acct_id")).toHaveFocus();
        expect(mockAddTransaction).toHaveBeenCalledTimes(1);
    },
);

// COTRN02C WHEN OTHER: an AID outside ENTER, PF3, PF4 and PF5 sets ERR-FLG and sends
// CCDA-MSG-INVALID-KEY (cpy/CSMSG01Y.cpy). Nothing is written: the prompt screen stays as shown,
// the resolved card masked, no field flagged, and the cursor goes to ACTIDIN, the IC field. The
// prompt is reached from the account alone, the server sending back every other field, and the
// pointer leaves the inputs before the key, so the cursor move is the page's alone.
const INVALID_KEY_MESSAGE = "Invalid key pressed. Please see below...";
const UNBOUND_KEYS: readonly string[] = ["F7", "F8", "F12"];

it.each(UNBOUND_KEYS)(
    "COTRN02C %s shows the invalid-key literal in red without a call",
    async (keyName) => {
        mockAddTransaction.mockResolvedValueOnce(BuildAddResponse());
        const user = RenderScreen();
        await KeyInto(user, "acct_id", TEST_ACCOUNT_ID);
        await UseMapKey(user, "ENTER", "button");
        await ExpectAlert(CONFIRM_PROMPT_MESSAGE, "error");
        await KeyInto(user, "confirm", "Y");
        await user.click(document.body);
        const keyInit = { key: keyName, bubbles: true, cancelable: true };
        const keyEvent = new KeyboardEvent("keydown", keyInit);
        act(() => {
            document.body.dispatchEvent(keyEvent);
        });
        await ExpectAlert(INVALID_KEY_MESSAGE, "error");
        expect(keyEvent.defaultPrevented).toBe(true);
        expect(mockAddTransaction).toHaveBeenCalledTimes(1);
        ExpectScreenValues({ ...E2E_LIKE_INPUT, card_num: SERVED_CARD_MASKED, confirm: "Y" });
        ExpectMarkedFields([]);
        expect(InputOf("acct_id")).toHaveFocus();
    },
);

it.each(KEY_METHODS)("COTRN02C PF3 by %s returns to COMEN01C at /menu", async (method) => {
    const user = RenderScreen();
    await KeyInto(user, "acct_id", TEST_ACCOUNT_ID);
    await UseMapKey(user, "F3", method);
    expect(mockRouterPush).toHaveBeenCalledTimes(1);
    expect(mockRouterPush).toHaveBeenCalledWith(MENU_ROUTE);
    expect(mockRouterReplace).not.toHaveBeenCalled();
    expect(mockAddTransaction).not.toHaveBeenCalled();
});

// The session guard (EIBCALEN = 0) is the backend 401; the apiClient interceptor owns the
// redirect, so the page shows the body's empty message and routes nowhere itself.
it("COTRN02C 401 session refusal goes to sign-on through the apiClient interceptor", async () => {
    const consoleErrorSpy = jest.spyOn(console, "error").mockImplementation(() => undefined);
    const refusal = BuildAxiosError(HTTP_SIGN_ON_FAILURE, BuildErrorBody());
    mockAddTransaction.mockImplementation(() => HandleResponseError(refusal));
    const user = RenderScreen();
    await FillForm(user, E2E_LIKE_INPUT);
    await UseMapKey(user, "ENTER", "button");
    await WaitUntilIdle();
    expect(SIGN_ON_ROUTE).toBe("/signon");
    expect(consoleErrorSpy.mock.calls.some(IsNavigationAttempt)).toBe(true);
    expect(consoleErrorSpy.mock.calls.every(IsNavigationAttempt)).toBe(true);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(mockRouterPush).not.toHaveBeenCalled();
});

// ACTIDIN laid out above the viewport, as after the user scrolled the page down with the cursor
// left in it. jsdom lays nothing out, so the field reports this box, and window.scrollBy moves it
// by the scroll the page asks for, as a browser scrolling the window would.
const FIELD_TOP_ABOVE_VIEWPORT = -400;
const FIELD_BOX_HEIGHT = 56;

function ScrollTopOf(scrollOptions: unknown): number {
    if (typeof scrollOptions === "object" && scrollOptions !== null && "top" in scrollOptions) {
        return Number(scrollOptions.top);
    }
    return 0;
}

function LayOutAboveViewport(field: HTMLElement): void {
    let fieldTop = FIELD_TOP_ABOVE_VIEWPORT;
    jest.spyOn(field, "getBoundingClientRect").mockImplementation(
        () => new DOMRect(0, fieldTop, 0, FIELD_BOX_HEIGHT),
    );
    jest.spyOn(window, "scrollBy").mockImplementation((...scrollArguments: unknown[]) => {
        fieldTop -= ScrollTopOf(scrollArguments[0]);
    });
}

function ExpectWhollyInView(field: HTMLElement): void {
    const box = field.getBoundingClientRect();
    expect(box.top).toBeGreaterThanOrEqual(0);
    expect(box.bottom).toBeLessThanOrEqual(window.innerHeight);
}

// Resolves in a frame requested now, which jsdom runs after every frame requested before it.
async function NextAnimationFrame(): Promise<void> {
    await act(
        () =>
            new Promise<void>((resolve) => {
                window.requestAnimationFrame(() => resolve());
            }),
    );
}

// A key pressed by its button, which leaves the cursor in ACTIDIN, and the outcome it repeats:
// ENTER on the blank form refused again, or F4's blank CLEAR-CURRENT-SCREEN line again.
interface RepeatedOutcomeCase {
    outcome: string;
    mapKey: MapKey;
}

const REPEATED_OUTCOME_CASES: readonly RepeatedOutcomeCase[] = [
    { outcome: ACCOUNT_OR_CARD_REQUIRED_MESSAGE, mapKey: "ENTER" },
    { outcome: "the blank line of CLEAR-CURRENT-SCREEN", mapKey: "F4" },
];

const BLANK_FORM_REFUSAL = ErrorFlagCase(
    HTTP_EDIT_FAILURE,
    ACCOUNT_OR_CARD_REQUIRED_MESSAGE,
    "acct_id",
    BLANK_SCREEN,
);

// Each outcome is new, even one repeating the lines shown. It focuses ACTIDIN again, which
// scrolls nothing while the field already has focus, so the message line brings the field the
// user scrolled away from back into view.
it.each(REPEATED_OUTCOME_CASES)(
    "COTRN02C $mapKey repeating $outcome scrolls the focused ACTIDIN back into view",
    async ({ mapKey }) => {
        MockOutcome(BLANK_FORM_REFUSAL);
        const user = RenderScreen();
        await UseMapKey(user, mapKey, "button");
        await WaitUntilIdle();
        await NextAnimationFrame();
        expect(InputOf("acct_id")).toHaveFocus();
        LayOutAboveViewport(InputOf("acct_id"));
        await UseMapKey(user, mapKey, "button");
        await WaitUntilIdle();
        await waitFor(() => ExpectWhollyInView(InputOf("acct_id")));
        expect(InputOf("acct_id")).toHaveFocus();
    },
);
