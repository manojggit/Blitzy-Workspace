// Screen tests: BMS COACTVW, program COACTVWC (CAVW), page /accounts/view.
// Every literal and builder output below is copied verbatim from
// backend/app/services/accounts/coactvwc.py (cbl/COACTVWC.cbl lines 111-137, 641-672, 744-856).
import { ThemeProvider } from "@mui/material/styles";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { AxiosError, AxiosHeaders, type InternalAxiosRequestConfig } from "axios";
import type { ReactNode } from "react";

import AccountViewPage from "@/app/accounts/view/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import { ViewAccount, type AccountViewResponse } from "@/lib/api/accounts";
import {
    HandleResponseError,
    SIGN_ON_ROUTE,
    type LegacyErrorResponse,
    type MessageSeverity,
} from "@/lib/apiClient";

const mockRouterPush = jest.fn();
const mockRouterReplace = jest.fn();
// One router object for every render, as Next.js keeps useRouter() stable.
const mockRouter = {
    push: mockRouterPush,
    replace: mockRouterReplace,
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};
// Read by the mocked useSearchParams at render time; each test sets its own query.
let mockSearchParams = new URLSearchParams();

jest.mock("next/navigation", () => ({
    useRouter: () => mockRouter,
    useSearchParams: () => mockSearchParams,
    usePathname: () => "/accounts/view",
}));

// The mocked keys mirror the exports of @/lib/session and its SessionContextValue.
jest.mock("@/lib/session", () => ({
    SessionProvider: ({ children }: { children: ReactNode }) => children,
    useSession: () => ({
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
    }),
}));

// CAVW calls only ViewAccount; the CAUP calls are stubbed so no test can reach the network.
jest.mock("@/lib/api/accounts", () => ({
    ...jest.requireActual<object>("@/lib/api/accounts"),
    ViewAccount: jest.fn(),
    FetchAccountForEdit: jest.fn(),
    UpdateAccount: jest.fn(),
}));

const mockViewAccount = jest.mocked(ViewAccount);

type DisplayFieldKey = Exclude<
    keyof AccountViewResponse,
    "acct_id" | "message" | "info_message" | "field_errors" | "cursor_field"
>;

interface KeyPressMethod {
    name: string;
    Press: (user: UserEvent) => Promise<void>;
}

const TEST_ACCOUNT_ID = "00000000004";
const TEST_ACCOUNT_URL = `/accounts/${TEST_ACCOUNT_ID}`;
// The account key the not-found builders below are rendered for.
const NOT_FOUND_ACCOUNT_ID = "00000000099";
const SCREEN_TITLE = "View Account";
const ACCOUNT_ID_LABEL = "Account Number :";
// BMS COACTVW ACCTSID LENGTH=11.
const ACCOUNT_ID_MAX_LENGTH = "11";
// The name coactvwc.py gives ACCTSID in field_errors and cursor_field.
const ACCOUNT_ID_FIELD = "acct_id";
// The page sends a blank ACCTSID as one space, so it still reaches the COACTVWC key edit.
const BLANK_ACCOUNT_ID = " ";
const SEARCH_BUTTON_NAME = "Search (Enter)";
const EXIT_BUTTON_NAME = "Exit (F3)";
const MENU_ROUTE = "/menu";
const SSN_LABEL = "SSN:";
const FICO_SCORE_LABEL = "FICO Score:";
const BALANCE_LABEL = "Current Balance     :";
const MASKED_SSN = "***-**-6789";
// An unmasked SSN in COACTVWC's 999-99-9999 display form. A bare nine-digit pattern would also
// match CUST-ID 9(09), which the screen shows legitimately.
const UNMASKED_SSN_PATTERN = /\d{3}-\d{2}-\d{4}/;
// jsdom implements no navigation, so the interceptor's window.location.assign reaches
// console.error with this text.
const NAVIGATION_NOT_IMPLEMENTED = "Not implemented: navigation";

const HTTP_BAD_REQUEST = 400;
const HTTP_UNAUTHORIZED = 401;
const HTTP_NOT_FOUND = 404;
const HTTP_FILE_ERROR = 503;

// 2210-EDIT-ACCOUNT sets ACCOUNT_NOT_PROVIDED_MESSAGE for a blank key; the 2200 cross-field edit
// then overwrites it, so a blank ENTER is answered with NO_INPUT_MESSAGE.
const ACCOUNT_NOT_PROVIDED_MESSAGE = "Account number not provided";
const NO_INPUT_MESSAGE = "No input received";
// Two spaces between "must" and "be", as in the source literal.
const ACCOUNT_FILTER_MESSAGE = "Account Filter must  be a non-zero 11 digit number";
// WS-INFO-MSG WS-PROMPT-FOR-INPUT: the information line of every CAVW response.
const INFO_PROMPT = "Enter or update id of account to display";
// The NOTFND builders for account 00000000099 and customer 000000099: RESP 13 and RESP2 0 as
// their X(10) fields, the whole STRING cut to WS-RETURN-MSG X(75).
const XREF_NOT_FOUND_MESSAGE =
    "Account:00000000099 not found in Cross ref file.  Resp:000000013  Reas:0000";
const ACCOUNT_NOT_FOUND_MESSAGE =
    "Account:00000000099 not found in Acct Master file.Resp:000000013  Reas:0000";
const CUSTOMER_NOT_FOUND_MESSAGE =
    "CustId:000000099 not found in customer master.Resp: 000000013  REAS:0000000";
// The CXACAIX READ refused during the batch window (RESP 19, RESP2 60).
const XREF_FILE_ERROR_MESSAGE =
    "File Error: READ     on CXACAIX   returned RESP 000000019 ,RESP2 000000060";

// MUI 9 marks an Alert's colour as MuiAlert-color<Severity> (Alert/alertClasses.d.ts).
const SEVERITY_CLASSES: Readonly<Record<MessageSeverity, string>> = {
    error: "MuiAlert-colorError",
    success: "MuiAlert-colorSuccess",
    info: "MuiAlert-colorInfo",
};

// Account 00000000004 and its customer, with a distinct value in every field so that a field
// showing another key's value is caught.
const TEST_ACCOUNT_VIEW: AccountViewResponse = {
    acct_id: TEST_ACCOUNT_ID,
    active_status: "Y",
    open_date: "2012-11-17",
    credit_limit: "3503.00",
    expiration_date: "2023-12-16",
    cash_credit_limit: "2789.00",
    reissue_date: "2021-12-16",
    curr_balance: "194.00",
    curr_cycle_credit: "120.00",
    group_id: "DEFAULT",
    curr_cycle_debit: "75.25",
    cust_id: "000000004",
    ssn_masked: MASKED_SSN,
    date_of_birth: "1985-01-13",
    fico_credit_score: 776,
    first_name: "Delbert",
    middle_name: "Kaia",
    last_name: "Parisian",
    addr_line_1: "638 Blanda Gateway",
    addr_state_cd: "MI",
    addr_line_2: "Apt. 076",
    addr_zip: "39035-0455",
    addr_line_3: "Lake Virginie",
    addr_country_cd: "USA",
    phone_num_1: "(801)603-4121",
    govt_issued_id: "00000000000068579249",
    phone_num_2: "(156)074-6837",
    eft_account_id: "0040802739",
    primary_holder_ind: "N",
    message: "",
    info_message: INFO_PROMPT,
    field_errors: [],
    cursor_field: ACCOUNT_ID_FIELD,
};

// Each read-only field's label on the page (the COACTVW INITIAL without its trailing layout
// blanks) and the AccountViewResponse key it shows.
const DISPLAY_FIELD_CASES: readonly (readonly [string, DisplayFieldKey])[] = [
    ["Active Y/N:", "active_status"],
    ["Opened:", "open_date"],
    ["Credit Limit        :", "credit_limit"],
    ["Expiry:", "expiration_date"],
    ["Cash credit Limit   :", "cash_credit_limit"],
    ["Reissue:", "reissue_date"],
    [BALANCE_LABEL, "curr_balance"],
    ["Current Cycle Credit:", "curr_cycle_credit"],
    ["Account Group:", "group_id"],
    ["Current Cycle Debit :", "curr_cycle_debit"],
    ["Customer id  :", "cust_id"],
    [SSN_LABEL, "ssn_masked"],
    ["Date of birth:", "date_of_birth"],
    [FICO_SCORE_LABEL, "fico_credit_score"],
    ["First Name", "first_name"],
    ["Middle Name:", "middle_name"],
    ["Last Name :", "last_name"],
    ["Line 1", "addr_line_1"],
    ["Line 2", "addr_line_2"],
    // COACTVWC moves CUST-ADDR-LINE-3 to ACSCITY.
    ["City", "addr_line_3"],
    ["State", "addr_state_cd"],
    ["Zip", "addr_zip"],
    ["Country", "addr_country_cd"],
    ["Phone 1:", "phone_num_1"],
    ["Government Issued Id Ref    :", "govt_issued_id"],
    ["Phone 2:", "phone_num_2"],
    ["EFT Account Id:", "eft_account_id"],
    ["Primary Card Holder Y/N:", "primary_holder_ind"],
];

// A rejected ENTER: the message literal and the status of its LegacyMessageError.
const LEGACY_MESSAGE_CASES: readonly (readonly [string, number])[] = [
    [ACCOUNT_NOT_PROVIDED_MESSAGE, HTTP_BAD_REQUEST],
    [NO_INPUT_MESSAGE, HTTP_BAD_REQUEST],
    [ACCOUNT_FILTER_MESSAGE, HTTP_BAD_REQUEST],
    [XREF_NOT_FOUND_MESSAGE, HTTP_NOT_FOUND],
    [ACCOUNT_NOT_FOUND_MESSAGE, HTTP_NOT_FOUND],
    [CUSTOMER_NOT_FOUND_MESSAGE, HTTP_NOT_FOUND],
    [XREF_FILE_ERROR_MESSAGE, HTTP_FILE_ERROR],
];

// The identity normalizer: the default one trims and collapses the blanks the labels keep.
function KeepRawText(text: string): string {
    return text;
}

function BuildAccountView(overrides: Partial<AccountViewResponse> = {}): AccountViewResponse {
    return { ...TEST_ACCOUNT_VIEW, ...overrides };
}

// The body COACTVWC's ScreenError sends: the prompt information line, cursor on ACCTSID.
function BuildErrorBody(overrides: Partial<LegacyErrorResponse> = {}): LegacyErrorResponse {
    return {
        message: "",
        info_message: INFO_PROMPT,
        field_errors: [ACCOUNT_ID_FIELD],
        cursor_field: ACCOUNT_ID_FIELD,
        ...overrides,
    };
}

function BuildAxiosError(status: number, body: LegacyErrorResponse): AxiosError {
    const config: InternalAxiosRequestConfig = {
        url: TEST_ACCOUNT_URL,
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

function RenderScreen(): UserEvent {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <AccountViewPage />
        </ThemeProvider>,
    );
    return user;
}

function FieldByLabel(label: string): HTMLInputElement {
    return screen.getByLabelText<HTMLInputElement>(label, { normalizer: KeepRawText });
}

function AccountIdInput(): HTMLInputElement {
    return FieldByLabel(ACCOUNT_ID_LABEL);
}

function SearchButton(): HTMLElement {
    return screen.getByRole("button", { name: SEARCH_BUTTON_NAME });
}

function PressKey(user: UserEvent, key: string): Promise<void> {
    return user.keyboard(`{${key}}`);
}

function ClickButton(user: UserEvent, name: string): Promise<void> {
    return user.click(screen.getByRole("button", { name }));
}

function InDocumentOrder(first: Node, second: Node): number {
    return first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
}

// The Alerts whose raw text is exactly the literal, blanks included, in screen order: ERRMSG has
// role alert and INFOMSG role status.
function AlertsHolding(text: string): HTMLElement[] {
    return [...screen.queryAllByRole("alert"), ...screen.queryAllByRole("status")]
        .sort(InDocumentOrder)
        .filter((alert) => alert.textContent === text);
}

async function ExpectAlert(text: string, severity: MessageSeverity): Promise<void> {
    await waitFor(() => expect(AlertsHolding(text)).toHaveLength(1));
    expect(AlertsHolding(text)[0]).toHaveClass(SEVERITY_CLASSES[severity]);
}

// The value each read-only field shows, keyed by its label.
function DisplayedValues(): Record<string, string> {
    return Object.fromEntries(
        DISPLAY_FIELD_CASES.map(([label]) => [label, FieldByLabel(label).value]),
    );
}

function ExpectedValues(view: AccountViewResponse): Record<string, string> {
    return Object.fromEntries(
        DISPLAY_FIELD_CASES.map(([label, key]) => [label, String(view[key])]),
    );
}

function NavigationWasAttempted(spy: jest.SpyInstance): boolean {
    return spy.mock.calls.some((call: unknown[]) => {
        const firstArgument = call[0];
        const text = firstArgument instanceof Error ? firstArgument.message : String(firstArgument);
        return text.includes(NAVIGATION_NOT_IMPLEMENTED);
    });
}

// Waits for the response to settle the screen, so no state update lands after the test.
async function WaitForBalance(balance: string): Promise<void> {
    await waitFor(() => expect(FieldByLabel(BALANCE_LABEL)).toHaveValue(balance));
}

// Opens /accounts/view?acctId=<view.acct_id>, whose mount fetch answers with the view.
async function RenderPrefilledScreen(view: AccountViewResponse): Promise<UserEvent> {
    mockSearchParams = new URLSearchParams({ acctId: view.acct_id });
    mockViewAccount.mockResolvedValue(view);
    const user = RenderScreen();
    await WaitForBalance(view.curr_balance);
    return user;
}

const ENTER_METHODS: readonly KeyPressMethod[] = [
    { name: "the Search button", Press: (user) => ClickButton(user, SEARCH_BUTTON_NAME) },
    { name: "the Enter key", Press: (user) => PressKey(user, "Enter") },
];

const EXIT_METHODS: readonly KeyPressMethod[] = [
    { name: "the Exit button", Press: (user) => ClickButton(user, EXIT_BUTTON_NAME) },
    { name: "the F3 key", Press: (user) => PressKey(user, "F3") },
];

beforeEach(() => {
    mockSearchParams = new URLSearchParams();
    mockViewAccount.mockReset();
});

afterEach(() => {
    jest.restoreAllMocks();
    window.sessionStorage.clear();
});

describe("COACTVWC COACTVW map content on /accounts/view", () => {
    it("COACTVWC first entry shows the View Account title and ACCTSID with LENGTH 11", () => {
        RenderScreen();
        expect(screen.getByRole("heading", { level: 1, name: SCREEN_TITLE })).toBeVisible();
        expect(AccountIdInput()).toHaveAttribute("maxlength", ACCOUNT_ID_MAX_LENGTH);
        expect(AccountIdInput()).toHaveFocus();
        expect(mockViewAccount).not.toHaveBeenCalled();
    });

    it("COACTVWC first entry shows only the F3=Exit and ENTER keys", () => {
        RenderScreen();
        const keyGroup = screen.getByRole("group", { name: "Function keys" });
        const keyNames = within(keyGroup)
            .getAllByRole("button")
            .map((key) => key.textContent);
        expect(keyNames).toEqual([SEARCH_BUTTON_NAME, EXIT_BUTTON_NAME]);
    });
});

describe("COACTVWC COACTVW display fields on /accounts/view", () => {
    it.each(DISPLAY_FIELD_CASES)("COACTVWC first entry shows %p empty and read-only", (label) => {
        RenderScreen();
        expect(FieldByLabel(label)).toHaveValue("");
        expect(FieldByLabel(label)).toHaveAttribute("readonly");
    });
});

describe("COACTVWC acctId prefill on /accounts/view?acctId=", () => {
    it("COACTVWC prefill fetches the account once and shows every field", async () => {
        await RenderPrefilledScreen(BuildAccountView());
        expect(AccountIdInput()).toHaveValue(TEST_ACCOUNT_ID);
        expect(mockViewAccount).toHaveBeenCalledTimes(1);
        expect(mockViewAccount).toHaveBeenCalledWith(TEST_ACCOUNT_ID);
        expect(DisplayedValues()).toEqual(ExpectedValues(TEST_ACCOUNT_VIEW));
        await ExpectAlert(INFO_PROMPT, "info");
        expect(screen.queryByTestId("message-line-error")).toBeNull();
    });
});

describe("COACTVWC customer display rules on /accounts/view", () => {
    it("COACTVWC shows the SSN only as the server's masked value", async () => {
        await RenderPrefilledScreen(BuildAccountView());
        expect(FieldByLabel(SSN_LABEL)).toHaveValue(MASKED_SSN);
        expect(screen.queryByDisplayValue(UNMASKED_SSN_PATTERN)).toBeNull();
        expect(screen.queryByText(UNMASKED_SSN_PATTERN)).toBeNull();
    });

    it("COACTVWC shows CUST-FICO-CREDIT-SCORE 9(03) zero-padded to three digits", async () => {
        await RenderPrefilledScreen(BuildAccountView({ fico_credit_score: 85 }));
        expect(FieldByLabel(FICO_SCORE_LABEL)).toHaveValue("085");
    });
});

describe("COACTVWC ENTER on /accounts/view", () => {
    it.each(ENTER_METHODS)("COACTVWC ENTER by $name sends ACCTSID as typed", async (method) => {
        mockViewAccount.mockResolvedValue(BuildAccountView());
        const user = RenderScreen();
        await user.type(AccountIdInput(), "4");
        await method.Press(user);
        await WaitForBalance(TEST_ACCOUNT_VIEW.curr_balance);
        expect(mockViewAccount).toHaveBeenCalledTimes(1);
        expect(mockViewAccount).toHaveBeenCalledWith("4");
    });

    it.each(ENTER_METHODS)("COACTVWC blank ENTER by $name sends one space", async (method) => {
        const body = BuildErrorBody({ message: NO_INPUT_MESSAGE });
        mockViewAccount.mockRejectedValue(BuildAxiosError(HTTP_BAD_REQUEST, body));
        const user = RenderScreen();
        await method.Press(user);
        await ExpectAlert(NO_INPUT_MESSAGE, "error");
        expect(mockViewAccount).toHaveBeenCalledTimes(1);
        expect(mockViewAccount).toHaveBeenCalledWith(BLANK_ACCOUNT_ID);
    });
});

describe("COACTVWC legacy messages on /accounts/view", () => {
    it.each(LEGACY_MESSAGE_CASES)("COACTVWC shows %p (HTTP %i) in red", async (literal, status) => {
        const body = BuildErrorBody({ message: literal });
        mockViewAccount.mockRejectedValue(BuildAxiosError(status, body));
        const user = RenderScreen();
        await user.type(AccountIdInput(), TEST_ACCOUNT_ID);
        await ClickButton(user, SEARCH_BUTTON_NAME);
        await ExpectAlert(literal, "error");
        await ExpectAlert(INFO_PROMPT, "info");
    });
});

describe("COACTVWC failed ENTER after a shown account on /accounts/view", () => {
    it("COACTVWC failure clears the details and keeps the typed ACCTSID", async () => {
        const user = await RenderPrefilledScreen(BuildAccountView());
        const body = BuildErrorBody({ message: XREF_NOT_FOUND_MESSAGE });
        mockViewAccount.mockRejectedValueOnce(BuildAxiosError(HTTP_NOT_FOUND, body));
        await user.clear(AccountIdInput());
        await user.type(AccountIdInput(), NOT_FOUND_ACCOUNT_ID);
        await ClickButton(user, SEARCH_BUTTON_NAME);
        await ExpectAlert(XREF_NOT_FOUND_MESSAGE, "error");
        expect(mockViewAccount).toHaveBeenLastCalledWith(NOT_FOUND_ACCOUNT_ID);
        expect(Object.values(DisplayedValues()).every((value) => value === "")).toBe(true);
        expect(AccountIdInput()).toHaveValue(NOT_FOUND_ACCOUNT_ID);
    });
});

describe("COACTVWC F3 on /accounts/view", () => {
    it.each(EXIT_METHODS)("COACTVWC F3 by $name returns to /menu", async (method) => {
        const user = RenderScreen();
        await method.Press(user);
        expect(mockRouterPush).toHaveBeenCalledTimes(1);
        expect(mockRouterPush).toHaveBeenCalledWith(MENU_ROUTE);
        expect(mockRouterReplace).not.toHaveBeenCalled();
        expect(mockViewAccount).not.toHaveBeenCalled();
    });
});

// The backend refuses a missing or expired session cookie with 401 and an empty message. The
// page owns no 401 route: the apiClient interceptor sends it to sign-on, so the mocked call runs
// the real HandleResponseError, as API_CLIENT would.
const SESSION_REFUSAL_BODY = BuildErrorBody({
    info_message: "",
    field_errors: [],
    cursor_field: null,
});

describe("COACTVWC 401 on /accounts/view", () => {
    it("COACTVWC 401 on ENTER goes to /signon through the apiClient interceptor", async () => {
        const consoleErrorSpy = jest.spyOn(console, "error").mockImplementation(() => undefined);
        const refusal = BuildAxiosError(HTTP_UNAUTHORIZED, SESSION_REFUSAL_BODY);
        mockViewAccount.mockImplementation(() => HandleResponseError(refusal));
        const user = RenderScreen();
        await user.type(AccountIdInput(), TEST_ACCOUNT_ID);
        await ClickButton(user, SEARCH_BUTTON_NAME);
        await waitFor(() => expect(NavigationWasAttempted(consoleErrorSpy)).toBe(true));
        await waitFor(() => expect(SearchButton()).toBeEnabled());
        expect(SIGN_ON_ROUTE).toBe("/signon");
        expect(screen.queryAllByRole("alert")).toHaveLength(0);
        expect(screen.queryAllByRole("status")).toHaveLength(0);
        expect(mockRouterPush).not.toHaveBeenCalled();
        expect(mockRouterReplace).not.toHaveBeenCalled();
    });
});

describe("COACTVWC ACCTSID field error and cursor on /accounts/view", () => {
    it("COACTVWC non-numeric filter marks ACCTSID invalid and returns the cursor", async () => {
        const body = BuildErrorBody({ message: ACCOUNT_FILTER_MESSAGE });
        mockViewAccount.mockRejectedValue(BuildAxiosError(HTTP_BAD_REQUEST, body));
        const user = RenderScreen();
        await user.type(AccountIdInput(), "ABC");
        expect(AccountIdInput()).toHaveAttribute("aria-invalid", "false");
        await ClickButton(user, SEARCH_BUTTON_NAME);
        await ExpectAlert(ACCOUNT_FILTER_MESSAGE, "error");
        expect(AccountIdInput()).toHaveAttribute("aria-invalid", "true");
        expect(AccountIdInput()).toHaveFocus();
        expect(AccountIdInput()).toHaveValue("ABC");
    });
});
