// Screen tests: BMS COACTVW, program COACTVWC (CAVW), page /accounts/view.
// Every literal and builder output below is copied verbatim from
// backend/app/services/accounts/coactvwc.py (cbl/COACTVWC.cbl lines 111-137, 641-672, 744-856).
import { ThemeProvider } from "@mui/material/styles";
import { act, render, screen, waitFor, within, type RenderResult } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { AxiosError, AxiosHeaders, CanceledError, type InternalAxiosRequestConfig } from "axios";
import { StrictMode, type ReactNode } from "react";

import AccountViewPage from "@/app/accounts/view/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import { ViewAccount, type AccountViewResponse } from "@/lib/api/accounts";
import {
    ABEND_MESSAGE,
    HandleResponseError,
    PENDING_MESSAGE_KEY,
    SIGN_ON_ROUTE,
    StorePendingMessage,
    type LegacyErrorResponse,
    type MessageSeverity,
    type ReadOptions,
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

// A lookup the page starts and leaves in flight; Start returns the unmount that leaves the page.
interface PendingLookupCase {
    name: string;
    Start: () => Promise<() => void>;
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
// The section captions of bms/COACTVW.bms: line 199 INITIAL='Customer Details' and line 264
// INITIAL='Address:', which stands before ACSADL1 and ACSADL2.
const CUSTOMER_DETAILS_CAPTION = "Customer Details";
const ADDRESS_CAPTION = "Address:";
// ACSADL1 and ACSADL2 carry no INITIAL of their own; these are their labels on the page.
const ADDRESS_LINE_LABELS: readonly string[] = ["Line 1", "Line 2"];
const MASKED_SSN = "***-**-6789";
// An unmasked SSN in COACTVWC's 999-99-9999 display form. A bare nine-digit pattern would also
// match CUST-ID 9(09), which the screen shows legitimately.
const UNMASKED_SSN_PATTERN = /\d{3}-\d{2}-\d{4}/;
// jsdom implements no navigation, so the interceptor's window.location.assign reaches
// console.error with this text.
const NAVIGATION_NOT_IMPLEMENTED = "Not implemented: navigation";
// Every lookup is a read that carries the AbortSignal of the controller owning it.
const READ_OPTIONS = { signal: expect.any(AbortSignal) };
// A refusal the apiClient interceptor carries across a navigation (backend ADMIN_ONLY_MESSAGE).
const PENDING_REFUSAL = "No access - Admin Only option... ";

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
// showing another key's value is caught. Each *_edited text is the amount as coactvwc.py edits it
// into the COACTVW PICOUT +ZZZ,ZZZ,ZZZ.99.
const TEST_ACCOUNT_VIEW: AccountViewResponse = {
    acct_id: TEST_ACCOUNT_ID,
    active_status: "Y",
    open_date: "2012-11-17",
    credit_limit: "3503.00",
    credit_limit_edited: "+      3,503.00",
    expiration_date: "2023-12-16",
    cash_credit_limit: "2789.00",
    cash_credit_limit_edited: "+      2,789.00",
    reissue_date: "2021-12-16",
    curr_balance: "194.00",
    curr_balance_edited: "+        194.00",
    curr_cycle_credit: "120.00",
    curr_cycle_credit_edited: "+        120.00",
    group_id: "DEFAULT",
    curr_cycle_debit: "75.25",
    curr_cycle_debit_edited: "+         75.25",
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
// blanks) and the AccountViewResponse key it shows. An amount shows its edited text verbatim.
const DISPLAY_FIELD_CASES: readonly (readonly [string, DisplayFieldKey])[] = [
    ["Active Y/N:", "active_status"],
    ["Opened:", "open_date"],
    ["Credit Limit        :", "credit_limit_edited"],
    ["Expiry:", "expiration_date"],
    ["Cash credit Limit   :", "cash_credit_limit_edited"],
    ["Reissue:", "reissue_date"],
    [BALANCE_LABEL, "curr_balance_edited"],
    ["Current Cycle Credit:", "curr_cycle_credit_edited"],
    ["Account Group:", "group_id"],
    ["Current Cycle Debit :", "curr_cycle_debit_edited"],
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

function PageTree(): ReactNode {
    return (
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <AccountViewPage />
        </ThemeProvider>
    );
}

// Strict Mode, on in development (next.config reactStrictMode), runs every mount effect, its
// cleanup, then the effect again.
function RenderPage(isStrictMode = false): RenderResult {
    return render(isStrictMode ? <StrictMode>{PageTree()}</StrictMode> : PageTree());
}

function RenderScreen(): UserEvent {
    const user = userEvent.setup();
    RenderPage();
    return user;
}

// ACCTSID is an input; every display field is a textarea, so a long value wraps.
function FieldByLabel(label: string): HTMLInputElement | HTMLTextAreaElement {
    return screen.getByLabelText<HTMLInputElement | HTMLTextAreaElement>(label, {
        normalizer: KeepRawText,
    });
}

function AccountIdInput(): HTMLInputElement {
    return screen.getByLabelText<HTMLInputElement>(ACCOUNT_ID_LABEL, { normalizer: KeepRawText });
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

// The message line's Alerts in screen order: ERRMSG has role alert and INFOMSG role status.
function ShownAlerts(): HTMLElement[] {
    return [...screen.queryAllByRole("alert"), ...screen.queryAllByRole("status")].sort(
        InDocumentOrder,
    );
}

// The Alerts whose raw text is exactly the literal, blanks included, in screen order.
function AlertsHolding(text: string): HTMLElement[] {
    return ShownAlerts().filter((alert) => alert.textContent === text);
}

// Every Alert's raw text in screen order.
function AlertTexts(): (string | null)[] {
    return ShownAlerts().map((alert) => alert.textContent);
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

// Waits for the response to settle the screen, so no state update lands after the test. ACURBAL
// shows the balance's edited text, leading blanks included.
async function WaitForBalance(editedBalance: string): Promise<void> {
    await waitFor(() => expect(FieldByLabel(BALANCE_LABEL)).toHaveValue(editedBalance));
}

// Opens /accounts/view?acctId=<view.acct_id>, whose mount fetch answers with the view.
async function RenderPrefilledScreen(view: AccountViewResponse): Promise<UserEvent> {
    mockSearchParams = new URLSearchParams({ acctId: view.acct_id });
    mockViewAccount.mockResolvedValue(view);
    const user = RenderScreen();
    await WaitForBalance(view.curr_balance_edited);
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

// A lookup that stays in flight until its signal aborts it, then rejects as axios does.
function PendingUntilAborted(_acctId: string, options?: ReadOptions): Promise<AccountViewResponse> {
    return new Promise((_resolve, reject) => {
        options?.signal?.addEventListener("abort", () => reject(new CanceledError()));
    });
}

// The AbortSignal the page passed with its n-th lookup (0-based).
function SignalOfCall(callIndex: number): AbortSignal {
    const signal = mockViewAccount.mock.calls[callIndex]?.[1]?.signal;
    if (signal === undefined) {
        throw new Error(`ViewAccount call ${callIndex} carried no AbortSignal`);
    }
    return signal;
}

// Lets an aborted lookup's rejection reach the page before the test inspects the outcome.
async function SettleRejections(): Promise<void> {
    await act(async () => undefined);
}

// Holds the next lookup in flight; the returned function answers it with a view.
function HoldNextLookup(): (view: AccountViewResponse) => Promise<void> {
    let answer: (view: AccountViewResponse) => void = () => undefined;
    mockViewAccount.mockImplementationOnce(
        () =>
            new Promise<AccountViewResponse>((resolve) => {
                answer = resolve;
            }),
    );
    return async (view) => {
        await act(async () => answer(view));
    };
}

async function StartMountLookup(): Promise<() => void> {
    mockSearchParams = new URLSearchParams({ acctId: TEST_ACCOUNT_ID });
    const { unmount } = RenderPage();
    await waitFor(() => expect(mockViewAccount).toHaveBeenCalledTimes(1));
    return unmount;
}

async function StartEnterLookup(): Promise<() => void> {
    const user = userEvent.setup();
    const { unmount } = RenderPage();
    await user.type(AccountIdInput(), TEST_ACCOUNT_ID);
    await ClickButton(user, SEARCH_BUTTON_NAME);
    return unmount;
}

const PENDING_LOOKUP_CASES: readonly PendingLookupCase[] = [
    { name: "?acctId= entry", Start: StartMountLookup },
    { name: "ENTER", Start: StartEnterLookup },
];

beforeEach(() => {
    mockSearchParams = new URLSearchParams();
    mockViewAccount.mockReset();
});

afterEach(() => {
    jest.restoreAllMocks();
    window.sessionStorage.clear();
});

function ShowsCustomerDetailsCaption(): void {
    RenderScreen();
    const caption = screen.getByRole("heading", { level: 2, name: CUSTOMER_DETAILS_CAPTION });
    expect(caption).toBeVisible();
    expect(caption.textContent).toBe(CUSTOMER_DETAILS_CAPTION);
}

// The caption is shown inside the group it names, and that group holds the two address lines.
function ShowsAddressCaption(): void {
    RenderScreen();
    const addressGroup = screen.getByRole("group", { name: ADDRESS_CAPTION });
    const caption = within(addressGroup).getByText(ADDRESS_CAPTION, { normalizer: KeepRawText });
    expect(caption).toBeVisible();
    const fieldLabels = within(addressGroup)
        .getAllByRole<HTMLInputElement>("textbox")
        .map((field) => field.labels?.[0]?.textContent);
    expect(fieldLabels).toEqual(ADDRESS_LINE_LABELS);
}

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

    it("COACTVWC shows the Customer Details caption as a heading", ShowsCustomerDetailsCaption);

    it("COACTVWC shows the Address: caption over ACSADL1 and ACSADL2", ShowsAddressCaption);
});

const GROUP_LABEL = "Account Group:";

// MUI InputLabel marks a label raised above its value with data-shrink="true".
function ExpectLabelRaised(label: string): void {
    expect(FieldByLabel(label).labels?.[0]).toHaveAttribute("data-shrink", "true");
}

const FIELD_BOX_SELECTOR = ".MuiInputBase-root";
const FILLED_FIELD_CLASS = "MuiFilledInput-root";
const MULTILINE_FIELD_CLASS = "MuiInputBase-multiline";

// A protected map field shows its value in a filled textarea that grows a row for each line the
// value wraps onto, so a 25-character name or a 50-character address line is never cut off.
function ShowsWrappingDisplayField(label: string): void {
    RenderScreen();
    const field = FieldByLabel(label);
    expect(field).toBeInstanceOf(HTMLTextAreaElement);
    const fieldBox = field.closest(FIELD_BOX_SELECTOR);
    expect(fieldBox).toHaveClass(FILLED_FIELD_CLASS, MULTILINE_FIELD_CLASS);
}

// ACCTSID is keyed, so it stays one line: a textarea would take a newline.
function KeepsAccountIdSingleLine(): void {
    RenderScreen();
    expect(AccountIdInput()).toBeInstanceOf(HTMLInputElement);
}

// ACCT-GROUP-ID is blank in every seed account: its label stays above the empty value, as every
// other label stays above its value, instead of standing where the value goes.
async function RaisesBlankGroupLabel(): Promise<void> {
    await RenderPrefilledScreen(BuildAccountView({ group_id: "" }));
    expect(FieldByLabel(GROUP_LABEL)).toHaveValue("");
    for (const [label] of DISPLAY_FIELD_CASES) {
        ExpectLabelRaised(label);
    }
}

describe("COACTVWC COACTVW display fields on /accounts/view", () => {
    it.each(DISPLAY_FIELD_CASES)("COACTVWC first entry shows %p empty and read-only", (label) => {
        RenderScreen();
        expect(FieldByLabel(label)).toHaveValue("");
        expect(FieldByLabel(label)).toHaveAttribute("readonly");
    });
    it.each(DISPLAY_FIELD_CASES)("COACTVWC first entry raises %p over its empty value", (label) => {
        RenderScreen();
        ExpectLabelRaised(label);
    });
    it(
        "COACTVWC a blank ACCT-GROUP-ID keeps its label raised like the others",
        RaisesBlankGroupLabel,
    );
});

describe("COACTVWC COACTVW long values on /accounts/view", () => {
    it.each(DISPLAY_FIELD_CASES)(
        "COACTVWC shows %p filled, wrapping a long value",
        ShowsWrappingDisplayField,
    );
    it("COACTVWC keeps ACCTSID, the field keyed, a single-line input", KeepsAccountIdSingleLine);
});

const DISPLAY_FIELD_ID_PREFIX = "account-view-";
const DISPLAY_FIELD_ROWS = "1";
const HIDDEN_TEXTAREA_SELECTOR = "textarea[aria-hidden]";

// How one display field's textarea renders: its id, its rows and whether it takes an input type.
interface DisplayTextarea {
    id: string;
    rows: string | null;
    hasType: boolean;
}

function DisplayTextareaOf(field: HTMLElement): DisplayTextarea {
    return { id: field.id, rows: field.getAttribute("rows"), hasType: field.hasAttribute("type") };
}

function ExpectedDisplayTextarea(key: DisplayFieldKey): DisplayTextarea {
    return { id: DISPLAY_FIELD_ID_PREFIX + key, rows: DISPLAY_FIELD_ROWS, hasType: false };
}

// The browser sizes each native textarea to its value, so no hidden measuring copy of a field is
// rendered: every textarea on the screen is a display field, one row, with its own id and no type.
async function RendersNativeDisplayTextareas(): Promise<void> {
    await RenderPrefilledScreen(TEST_ACCOUNT_VIEW);
    const displayFields = DISPLAY_FIELD_CASES.map(([label]) => FieldByLabel(label));
    expect(Array.from(document.querySelectorAll("textarea"))).toEqual(displayFields);
    expect(document.querySelectorAll(HIDDEN_TEXTAREA_SELECTOR)).toHaveLength(0);
    expect(displayFields.map(DisplayTextareaOf)).toEqual(
        DISPLAY_FIELD_CASES.map(([, key]) => ExpectedDisplayTextarea(key)),
    );
}

describe("COACTVWC COACTVW display-field elements on /accounts/view", () => {
    it(
        "COACTVWC shows each fetched value in one identified textarea row and no hidden copy",
        RendersNativeDisplayTextareas,
    );
});

describe("COACTVWC acctId prefill on /accounts/view?acctId=", () => {
    it("COACTVWC prefill fetches the account once and shows every field", async () => {
        await RenderPrefilledScreen(BuildAccountView());
        expect(AccountIdInput()).toHaveValue(TEST_ACCOUNT_ID);
        expect(mockViewAccount).toHaveBeenCalledTimes(1);
        expect(mockViewAccount).toHaveBeenCalledWith(TEST_ACCOUNT_ID, READ_OPTIONS);
        expect(DisplayedValues()).toEqual(ExpectedValues(TEST_ACCOUNT_VIEW));
        await ExpectAlert(INFO_PROMPT, "info");
        expect(screen.queryByTestId("message-line-error")).toBeNull();
    });
});

interface OverlongAccountIdCase {
    shape: string;
    queryAcctId: string;
    keptAcctId: string;
}

// acctId values longer than the 9(11) CDEMO-ACCT-ID and ACCTSID, and the 11 characters each
// keeps; a character the cut would split in two is dropped whole.
const SPLIT_CHARACTER_PREFIX = "0".repeat(10);
const OVERLONG_ACCOUNT_ID_CASES: readonly OverlongAccountIdCase[] = [
    { shape: "12 digits", queryAcctId: "000000000016", keptAcctId: "00000000001" },
    { shape: "markup", queryAcctId: '"><svg onload=alert(2)>', keptAcctId: '"><svg onlo' },
    {
        shape: "10 digits and a two-unit character",
        queryAcctId: `${SPLIT_CHARACTER_PREFIX}\u{1F600}`,
        keptAcctId: SPLIT_CHARACTER_PREFIX,
    },
];

describe("COACTVWC first entry with an over-long acctId on /accounts/view", () => {
    it.each(OVERLONG_ACCOUNT_ID_CASES)(
        "COACTVWC cuts an acctId of $shape to the ACCTSID LENGTH before it looks it up",
        async ({ queryAcctId, keptAcctId }) => {
            mockSearchParams = new URLSearchParams({ acctId: queryAcctId });
            mockViewAccount.mockResolvedValue(BuildAccountView());
            RenderScreen();
            await WaitForBalance(TEST_ACCOUNT_VIEW.curr_balance_edited);
            expect(mockViewAccount).toHaveBeenCalledTimes(1);
            expect(mockViewAccount).toHaveBeenCalledWith(keptAcctId, READ_OPTIONS);
            expect(AccountIdInput()).toHaveValue(keptAcctId);
        },
    );
});

// One arrival on the page: its description and the query it carries.
interface EntryCase {
    entry: string;
    query: string;
}

interface PendingEntryCase extends EntryCase {
    infoLine: string;
    expectedAlerts: readonly string[];
}

// The arrivals that send no lookup: the page treats an empty acctId as none.
const FIRST_ENTRY_CASES: readonly EntryCase[] = [
    { entry: "from the menu", query: "" },
    { entry: "with an empty ?acctId=", query: "acctId=" },
];

// Only an arrival that sends no lookup shows the first-entry prompt beside the refusal; a lookup
// in flight leaves INFOMSG blank until it answers.
const PENDING_ENTRY_CASES: readonly PendingEntryCase[] = [
    {
        entry: "from the menu",
        query: "",
        infoLine: "WS-PROMPT-FOR-INPUT",
        expectedAlerts: [PENDING_REFUSAL, INFO_PROMPT],
    },
    {
        entry: "with ?acctId= in flight",
        query: `acctId=${TEST_ACCOUNT_ID}`,
        infoLine: "no information line",
        expectedAlerts: [PENDING_REFUSAL],
    },
];

// 1200-SETUP-SCREEN-VARS sets WS-PROMPT-FOR-INPUT on the first entry, before any key.
function ShowsFirstEntryPrompt({ query }: EntryCase): void {
    mockSearchParams = new URLSearchParams(query);
    RenderScreen();
    expect(AlertTexts()).toEqual([INFO_PROMPT]);
    expect(screen.getByRole("status").textContent).toBe(INFO_PROMPT);
    expect(screen.getByRole("status")).toHaveClass(SEVERITY_CLASSES.info);
    expect(AccountIdInput()).toHaveFocus();
    expect(mockViewAccount).not.toHaveBeenCalled();
}

async function ShowsPendingRefusalOnEntry(entryCase: PendingEntryCase): Promise<void> {
    mockViewAccount.mockImplementation(PendingUntilAborted);
    StorePendingMessage(PENDING_REFUSAL);
    mockSearchParams = new URLSearchParams(entryCase.query);
    RenderScreen();
    await ExpectAlert(PENDING_REFUSAL, "error");
    expect(AlertTexts()).toEqual(entryCase.expectedAlerts);
}

// The server's information line is the first one an arrival with an account number shows.
async function ShowsNoPromptUntilLookupAnswers(): Promise<void> {
    const answerLookup = HoldNextLookup();
    mockSearchParams = new URLSearchParams({ acctId: TEST_ACCOUNT_ID });
    RenderPage();
    await waitFor(() => expect(mockViewAccount).toHaveBeenCalledTimes(1));
    expect(AlertTexts()).toEqual([]);
    await answerLookup(BuildAccountView());
    await WaitForBalance(TEST_ACCOUNT_VIEW.curr_balance_edited);
    expect(AlertTexts()).toEqual([INFO_PROMPT]);
}

describe("COACTVWC first-entry information line on /accounts/view", () => {
    it.each(FIRST_ENTRY_CASES)(
        "COACTVWC first entry $entry shows WS-PROMPT-FOR-INPUT before any key",
        ShowsFirstEntryPrompt,
    );
    it.each(PENDING_ENTRY_CASES)(
        "COACTVWC entry $entry shows the pending refusal in red with $infoLine",
        ShowsPendingRefusalOnEntry,
    );
    it(
        "COACTVWC entry with ?acctId= shows no information line until the lookup answers",
        ShowsNoPromptUntilLookupAnswers,
    );
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
        await WaitForBalance(TEST_ACCOUNT_VIEW.curr_balance_edited);
        expect(mockViewAccount).toHaveBeenCalledTimes(1);
        expect(mockViewAccount).toHaveBeenCalledWith("4", READ_OPTIONS);
    });

    it.each(ENTER_METHODS)("COACTVWC blank ENTER by $name sends one space", async (method) => {
        const body = BuildErrorBody({ message: NO_INPUT_MESSAGE });
        mockViewAccount.mockRejectedValue(BuildAxiosError(HTTP_BAD_REQUEST, body));
        const user = RenderScreen();
        await method.Press(user);
        await ExpectAlert(NO_INPUT_MESSAGE, "error");
        expect(mockViewAccount).toHaveBeenCalledTimes(1);
        expect(mockViewAccount).toHaveBeenCalledWith(BLANK_ACCOUNT_ID, READ_OPTIONS);
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
        expect(mockViewAccount).toHaveBeenLastCalledWith(NOT_FOUND_ACCOUNT_ID, READ_OPTIONS);
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

// The MessageLine ERRMSG element id, which an input the server names in error references.
const ERRMSG_ID = "message-line-errmsg";

// A rejected ENTER whose body names ACCTSID: the keys typed into ACCTSID, ending with ENTER, and
// the literal the 400 answers them with.
const ACCOUNT_ID_ERROR_CASES: readonly (readonly [string, string])[] = [
    ["{Enter}", NO_INPUT_MESSAGE],
    ["ABC{Enter}", ACCOUNT_FILTER_MESSAGE],
];

// ACCTSID has no description until an answer names it, then references the ERRMSG line holding
// the literal; the next answer, which names no field, drops the reference again.
async function TiesAccountIdErrorToMessageLine(keys: string, literal: string): Promise<void> {
    const body = BuildErrorBody({ message: literal });
    mockViewAccount.mockRejectedValueOnce(BuildAxiosError(HTTP_BAD_REQUEST, body));
    const user = RenderScreen();
    expect(AccountIdInput()).not.toHaveAttribute("aria-describedby");
    await user.type(AccountIdInput(), keys);
    await ExpectAlert(literal, "error");
    expect(AccountIdInput()).toHaveAttribute("aria-describedby", ERRMSG_ID);
    expect(document.getElementById(ERRMSG_ID)?.textContent).toBe(literal);
    mockViewAccount.mockResolvedValueOnce(BuildAccountView());
    await ClickButton(user, SEARCH_BUTTON_NAME);
    await WaitForBalance(TEST_ACCOUNT_VIEW.curr_balance_edited);
    expect(AccountIdInput()).not.toHaveAttribute("aria-describedby");
}

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
    it.each(ACCOUNT_ID_ERROR_CASES)(
        "COACTVWC ACCTSID keyed %p references the ERRMSG line holding %p",
        TiesAccountIdErrorToMessageLine,
    );
});

// Two ACCTSID texts COACTVWC's key edit answers with the same literal: all zeros and non-digits.
const SAME_LITERAL_KEYS: readonly string[] = ["00000000000", "abc"];

// Each answer is a new outcome, so ENTER answered again with the literal already shown brings
// the message line into view again, as the 3270 sent the whole map after every key.
async function RevealsRepeatedLiteralEachTime(method: KeyPressMethod): Promise<void> {
    const scrollIntoViewSpy = jest.spyOn(Element.prototype, "scrollIntoView");
    const body = BuildErrorBody({ message: ACCOUNT_FILTER_MESSAGE });
    mockViewAccount.mockRejectedValue(BuildAxiosError(HTTP_BAD_REQUEST, body));
    const user = RenderScreen();
    for (const [answerIndex, keyedText] of SAME_LITERAL_KEYS.entries()) {
        await user.clear(AccountIdInput());
        await user.type(AccountIdInput(), keyedText);
        await method.Press(user);
        await waitFor(() => expect(mockViewAccount).toHaveBeenCalledTimes(answerIndex + 1));
        await ExpectAlert(ACCOUNT_FILTER_MESSAGE, "error");
        await waitFor(() => expect(scrollIntoViewSpy).toHaveBeenCalledTimes(answerIndex + 1));
    }
    const lineWrapper = document.getElementById(ERRMSG_ID)?.parentElement;
    expect(scrollIntoViewSpy.mock.contexts).toEqual([lineWrapper, lineWrapper]);
}

describe("COACTVWC message line on every answer on /accounts/view", () => {
    it.each(ENTER_METHODS)(
        "COACTVWC the same literal answering two ENTERs by $name is brought into view twice",
        RevealsRepeatedLiteralEachTime,
    );
});

// Leaving the page aborts the lookup in flight; its rejection then reaches no screen and no log.
async function AbortsLookupOnLeaving(lookup: PendingLookupCase): Promise<void> {
    const consoleErrorSpy = jest.spyOn(console, "error");
    mockViewAccount.mockImplementation(PendingUntilAborted);
    const leavePage = await lookup.Start();
    expect(SignalOfCall(0).aborted).toBe(false);
    leavePage();
    expect(SignalOfCall(0).aborted).toBe(true);
    await SettleRejections();
    expect(mockViewAccount).toHaveBeenCalledTimes(1);
    expect(consoleErrorSpy).not.toHaveBeenCalled();
}

// The answer of the lookup that supersedes another, told apart by its balance.
const LATEST_LOOKUP_VIEW = BuildAccountView({
    curr_balance: "250.00",
    curr_balance_edited: "+        250.00",
});

// ENTER is disabled while a lookup is in flight, so a new ?acctId= is what supersedes one.
async function ShowsOnlyLatestLookup(): Promise<void> {
    mockViewAccount.mockImplementationOnce(PendingUntilAborted);
    const answerLatest = HoldNextLookup();
    mockSearchParams = new URLSearchParams({ acctId: NOT_FOUND_ACCOUNT_ID });
    const { rerender } = RenderPage();
    await waitFor(() => expect(mockViewAccount).toHaveBeenCalledTimes(1));
    mockSearchParams = new URLSearchParams({ acctId: TEST_ACCOUNT_ID });
    rerender(PageTree());
    await SettleRejections();
    expect([SignalOfCall(0).aborted, SignalOfCall(1).aborted]).toEqual([true, false]);
    // The aborted lookup shows nothing and leaves the busy state to the one in flight.
    expect(SearchButton()).toBeDisabled();
    expect(AlertTexts()).toEqual([]);
    await answerLatest(LATEST_LOOKUP_VIEW);
    await WaitForBalance(LATEST_LOOKUP_VIEW.curr_balance_edited);
    expect(mockViewAccount).toHaveBeenNthCalledWith(1, NOT_FOUND_ACCOUNT_ID, READ_OPTIONS);
    expect(mockViewAccount).toHaveBeenNthCalledWith(2, TEST_ACCOUNT_ID, READ_OPTIONS);
    expect(AlertTexts()).toEqual([INFO_PROMPT]);
    expect(SearchButton()).toBeEnabled();
}

async function IgnoresCancelledLookup(): Promise<void> {
    const user = await RenderPrefilledScreen(BuildAccountView());
    mockViewAccount.mockRejectedValueOnce(new CanceledError());
    await user.clear(AccountIdInput());
    await user.type(AccountIdInput(), NOT_FOUND_ACCOUNT_ID);
    await ClickButton(user, SEARCH_BUTTON_NAME);
    await waitFor(() => expect(SearchButton()).toBeEnabled());
    expect(mockViewAccount).toHaveBeenLastCalledWith(NOT_FOUND_ACCOUNT_ID, READ_OPTIONS);
    expect(AlertsHolding(ABEND_MESSAGE)).toHaveLength(0);
    expect(AlertTexts()).toEqual([INFO_PROMPT]);
    expect(DisplayedValues()).toEqual(ExpectedValues(TEST_ACCOUNT_VIEW));
}

// The App Router keeps the page mounted until the menu has loaded, so F3 itself aborts the lookup
// in flight before it navigates. The aborted lookup shows nothing and leaves ENTER free.
async function AbortsLookupOnExit(method: KeyPressMethod): Promise<void> {
    const consoleErrorSpy = jest.spyOn(console, "error");
    mockViewAccount.mockImplementation(PendingUntilAborted);
    await StartMountLookup();
    const pushesBeforeAbort: number[] = [];
    SignalOfCall(0).addEventListener("abort", () => {
        pushesBeforeAbort.push(mockRouterPush.mock.calls.length);
    });
    await method.Press(userEvent.setup());
    await SettleRejections();
    expect(pushesBeforeAbort).toEqual([0]);
    expect(mockRouterPush.mock.calls).toEqual([[MENU_ROUTE]]);
    expect(AlertTexts()).toEqual([]);
    expect(SearchButton()).toBeEnabled();
    expect(mockViewAccount).toHaveBeenCalledTimes(1);
    expect(consoleErrorSpy).not.toHaveBeenCalled();
}

// Development: the first mount lookup is aborted by the simulated unmount, the second answers.
async function StrictModeLooksUpAgain(): Promise<void> {
    mockSearchParams = new URLSearchParams({ acctId: TEST_ACCOUNT_ID });
    mockViewAccount.mockImplementationOnce(PendingUntilAborted);
    mockViewAccount.mockResolvedValueOnce(BuildAccountView());
    RenderPage(true);
    await WaitForBalance(TEST_ACCOUNT_VIEW.curr_balance_edited);
    expect(mockViewAccount.mock.calls).toEqual([
        [TEST_ACCOUNT_ID, READ_OPTIONS],
        [TEST_ACCOUNT_ID, READ_OPTIONS],
    ]);
    expect([SignalOfCall(0).aborted, SignalOfCall(1).aborted]).toEqual([true, false]);
    expect(DisplayedValues()).toEqual(ExpectedValues(TEST_ACCOUNT_VIEW));
    await ExpectAlert(INFO_PROMPT, "info");
    expect(SearchButton()).toBeEnabled();
}

async function StrictModeShowsPendingMessage(): Promise<void> {
    StorePendingMessage(PENDING_REFUSAL);
    RenderPage(true);
    await ExpectAlert(PENDING_REFUSAL, "error");
    expect(AlertTexts()).toEqual([PENDING_REFUSAL, INFO_PROMPT]);
    expect(window.sessionStorage.getItem(PENDING_MESSAGE_KEY)).toBeNull();
    expect(mockViewAccount).not.toHaveBeenCalled();
}

describe("COACTVWC lookup cancellation on /accounts/view", () => {
    it.each(PENDING_LOOKUP_CASES)(
        "COACTVWC leaving aborts the $name lookup",
        AbortsLookupOnLeaving,
    );
    it(
        "COACTVWC a new ?acctId= aborts the pending lookup; only its answer shows",
        ShowsOnlyLatestLookup,
    );
    it(
        "COACTVWC a cancelled lookup shows no message and keeps the account",
        IgnoresCancelledLookup,
    );
    it.each(EXIT_METHODS)(
        "COACTVWC F3 by $name aborts the pending lookup before returning to /menu",
        AbortsLookupOnExit,
    );
});

describe("COACTVWC under React Strict Mode on /accounts/view", () => {
    it(
        "COACTVWC Strict Mode aborts the first ?acctId= lookup and shows the second",
        StrictModeLooksUpAgain,
    );
    it(
        "COACTVWC Strict Mode takes the pending refusal once and shows it",
        StrictModeShowsPendingMessage,
    );
});

// Account 00000000005 with a value unlike TEST_ACCOUNT_VIEW's in every displayed field, so a
// field still showing account 00000000004 after a navigation is caught.
const NEXT_ACCOUNT_ID = "00000000005";
const NEXT_ACCOUNT_VIEW = BuildAccountView({
    acct_id: NEXT_ACCOUNT_ID,
    active_status: "N",
    open_date: "2015-04-22",
    credit_limit: "6120.00",
    credit_limit_edited: "+      6,120.00",
    expiration_date: "2026-08-31",
    cash_credit_limit: "1450.00",
    cash_credit_limit_edited: "+      1,450.00",
    reissue_date: "2024-08-31",
    curr_balance: "158.00",
    curr_balance_edited: "+        158.00",
    curr_cycle_credit: "42.10",
    curr_cycle_credit_edited: "+         42.10",
    group_id: "GOLDCARD",
    curr_cycle_debit: "18.75",
    curr_cycle_debit_edited: "+         18.75",
    cust_id: "000000005",
    ssn_masked: "***-**-4321",
    date_of_birth: "1979-09-02",
    fico_credit_score: 641,
    first_name: "Marla",
    middle_name: "Jo",
    last_name: "Okafor",
    addr_line_1: "12 Harbor View Road",
    addr_state_cd: "OR",
    addr_line_2: "Suite 300",
    addr_zip: "97201",
    addr_line_3: "Portland",
    addr_country_cd: "CAN",
    phone_num_1: "(503)555-0142",
    govt_issued_id: "00000000000012345678",
    phone_num_2: "(503)555-0199",
    eft_account_id: "0051234567",
    primary_holder_ind: "Y",
});

// Moves the mounted page to another query, as a same-path client navigation does: the App Router
// keeps the route mounted and only useSearchParams changes.
type QueryNavigator = (query: URLSearchParams) => void;

// Opens /accounts/view?acctId=<view.acct_id> and waits until its entry lookup shows the view.
async function RenderShownAccountRoute(view: AccountViewResponse): Promise<QueryNavigator> {
    mockSearchParams = new URLSearchParams({ acctId: view.acct_id });
    mockViewAccount.mockResolvedValueOnce(view);
    const { rerender } = RenderPage();
    await WaitForBalance(view.curr_balance_edited);
    return (query) => {
        mockSearchParams = query;
        rerender(PageTree());
    };
}

function ExpectEveryDisplayFieldBlank(): void {
    expect(Object.values(DisplayedValues()).every((value) => value === "")).toBe(true);
}

// A to B replaces ACCTSID and the record together: B's entry screen starts blank under B, shows
// B's answer, and its next ENTER looks B up.
async function ReplacesScreenOnNewAcctId(): Promise<void> {
    const navigate = await RenderShownAccountRoute(BuildAccountView());
    const answerNext = HoldNextLookup();
    navigate(new URLSearchParams({ acctId: NEXT_ACCOUNT_ID }));
    expect(AccountIdInput()).toHaveValue(NEXT_ACCOUNT_ID);
    ExpectEveryDisplayFieldBlank();
    await answerNext(NEXT_ACCOUNT_VIEW);
    await WaitForBalance(NEXT_ACCOUNT_VIEW.curr_balance_edited);
    expect(DisplayedValues()).toEqual(ExpectedValues(NEXT_ACCOUNT_VIEW));
    mockViewAccount.mockResolvedValueOnce(NEXT_ACCOUNT_VIEW);
    await ClickButton(userEvent.setup(), SEARCH_BUTTON_NAME);
    await waitFor(() => expect(mockViewAccount).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(SearchButton()).toBeEnabled());
    expect(mockViewAccount.mock.calls).toEqual([
        [TEST_ACCOUNT_ID, READ_OPTIONS],
        [NEXT_ACCOUNT_ID, READ_OPTIONS],
        [NEXT_ACCOUNT_ID, READ_OPTIONS],
    ]);
}

// Dropping ?acctId= is a fresh entry from the menu: the empty map, only the first-entry prompt
// and no lookup.
async function StartsFreshEntryWithoutAcctId(): Promise<void> {
    const navigate = await RenderShownAccountRoute(BuildAccountView());
    navigate(new URLSearchParams());
    expect(AccountIdInput()).toHaveValue("");
    expect(AccountIdInput()).toHaveFocus();
    ExpectEveryDisplayFieldBlank();
    expect(AlertTexts()).toEqual([INFO_PROMPT]);
    await SettleRejections();
    expect(mockViewAccount).toHaveBeenCalledTimes(1);
    expect(SearchButton()).toBeEnabled();
}

describe("COACTVWC same-route ?acctId= change on /accounts/view", () => {
    it(
        "COACTVWC a new ?acctId= shows the new account under its own number and ENTER sends it",
        ReplacesScreenOnNewAcctId,
    );
    it(
        "COACTVWC dropping ?acctId= shows the empty first-entry map with no lookup",
        StartsFreshEntryWithoutAcctId,
    );
});
