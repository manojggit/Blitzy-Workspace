// Screen tests: BMS COACTUP, program COACTUPC (CAUP), page /accounts/update.
// Sources: bms/COACTUP.bms and cpy-bms/COACTUP.CPY (labels and LENGTHs); cbl/COACTUPC.cbl
// (2000-DECIDE-ACTION, 3250-SETUP-INFOMSG, 3300-SETUP-SCREEN-ATTRS, 3320-UNPROTECT-FEW-ATTRS);
// the literals of backend/app/services/accounts/coactupc.py, copied verbatim.
import { ThemeProvider } from "@mui/material/styles";
import {
    act,
    fireEvent,
    render,
    screen,
    waitFor,
    within,
    type RenderResult,
} from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import {
    AxiosError,
    AxiosHeaders,
    CanceledError,
    type AxiosResponse,
    type InternalAxiosRequestConfig,
} from "axios";
import { StrictMode, type ReactNode } from "react";

import AccountUpdatePage from "@/app/accounts/update/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import type * as AccountsApiModule from "@/lib/api/accounts";
import {
    FetchAccountForEdit,
    UpdateAccount,
    type AccountScreen,
    type AccountScreenFields,
    type AccountUpdateRequest,
} from "@/lib/api/accounts";
import {
    SIGN_ON_ROUTE,
    UpdateAction,
    UpdateScreenState,
    type LegacyErrorResponse,
    type MessageSeverity,
    type ReadOptions,
} from "@/lib/apiClient";
import type { SessionContextValue } from "@/lib/session";

type AccountFieldName = keyof AccountScreenFields;

type ThreePartNames = readonly [AccountFieldName, AccountFieldName, AccountFieldName];

// The CAUP error body: the legacy keys plus the screen the page keeps the state machine on.
type AccountErrorBody = LegacyErrorResponse &
    Partial<Pick<AccountScreen, "fields" | "state" | "screen_token">>;

// One Alert of the message line: its raw text and its severity.
type ShownAlert = readonly [text: string, severity: string];

type InputMethod = "click" | "key";

// One COACTUP input: the label the page renders, the BMS LENGTH and, for a part of a map field
// keyed in parts (a date, the SSN, a phone number, the address lines), its group caption.
interface FieldCase {
    name: AccountFieldName;
    label: string;
    maxLength: number;
    group?: string;
}

interface EditableByStateCase {
    state: UpdateScreenState;
    description: string;
    editableNames: readonly AccountFieldName[];
}

interface FunctionKey {
    buttonName: string;
    key: string;
}

interface UpdateKey extends FunctionKey {
    action: UpdateAction;
}

interface ActionKeyCase extends UpdateKey {
    method: InputMethod;
}

interface OutcomeCase {
    description: string;
    startState: UpdateScreenState;
    functionKey: FunctionKey;
    status: number;
    accountScreen: AccountScreen;
    expectedAlerts: readonly ShownAlert[];
}

interface AdoptionCase {
    status: number;
    message: string;
    adoptedState: UpdateScreenState;
    adoptedFields: AccountScreenFields;
    expectedFieldNames: readonly AccountFieldName[];
}

const mockRouterPush = jest.fn();
const mockRouterReplace = jest.fn();
// One router object for every render, as the App Router keeps useRouter stable.
const mockRouter = {
    push: mockRouterPush,
    replace: mockRouterReplace,
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};
// Read by the mocked useSearchParams at render time: the ?acctId= entry, or none.
let mockSearchParams = new URLSearchParams();
// The signed-on regular user of the header; it carries an ID and names only.
const mockCurrentUser: SessionContextValue["currentUser"] = {
    user_id: "TESTUSR1",
    user_type: "U",
    first_name: "TEST",
    last_name: "USER",
    next_route: "/menu",
};

jest.mock("next/navigation", () => ({
    useRouter: () => mockRouter,
    useSearchParams: () => mockSearchParams,
    usePathname: () => "/accounts/update",
}));

jest.mock("@/lib/session", () => ({
    SessionProvider: ({ children }: { children: ReactNode }) => children,
    useSession: () => ({
        currentUser: mockCurrentUser,
        isLoading: false,
        RefreshSession: jest.fn(),
        ClearSession: jest.fn(),
    }),
}));

jest.mock("@/lib/api/accounts", () => ({
    ...jest.requireActual<typeof AccountsApiModule>("@/lib/api/accounts"),
    ViewAccount: jest.fn(),
    FetchAccountForEdit: jest.fn(),
    UpdateAccount: jest.fn(),
}));

const mockFetchAccountForEdit = jest.mocked(FetchAccountForEdit);
const mockUpdateAccount = jest.mocked(UpdateAccount);

const TEST_ACCOUNT_ID = "00000000004";
const FIRST_SCREEN_TOKEN = "screen-token-1";
const NEXT_SCREEN_TOKEN = "screen-token-2";
const ADOPTED_SCREEN_TOKEN = "screen-token-3";
const FINAL_SCREEN_TOKEN = "screen-token-4";
const MENU_ROUTE = "/menu";
// A blank ACCTSID travels as one space, so it still reaches the server's key edit.
const BLANK_ACCOUNT_PATH = " ";
// AAP 0.6.4: COACTUPC's TEST-NUMVAL-C accepts this text, so the page sends it as keyed.
const KEYED_CREDIT_LIMIT = "$1,234.567";
// The edit fetch is a read that carries its owner's AbortSignal; the PUT carries none.
const READ_OPTIONS = { signal: expect.any(AbortSignal) };

// The statuses coactupc.py assigns (app.core.errors).
const HTTP_OK = 200;
const HTTP_EDIT_FAILURE = 400;
const HTTP_UNAUTHORIZED = 401;
const HTTP_RECORD_CHANGED = 409;
const HTTP_RECORD_LOCKED = 423;
const HTTP_FILE_ERROR = 503;

// WS-RETURN-MSG texts of coactupc.py. A blank key answers "No input received": 1200 sets
// NO-SEARCH-CRITERIA-RECEIVED after 1210 set "Account number not provided".
const MSG_NO_INPUT_RECEIVED = "No input received";
const MSG_ACCOUNT_KEY_INVALID = "Account Number if supplied must be a 11 digit Non-Zero Number";
const MSG_NO_CHANGES = "No change detected with respect to values fetched.";
const MSG_CREDIT_LIMIT_NOT_VALID = "Credit Limit is not valid";
const MSG_LOCK_ACCOUNT = "Could not lock account record for update";
const MSG_RECORD_CHANGED = "Record changed by some one else. Please review";
const MSG_UPDATE_FAILED = "Update of record failed";

// WS-INFO-MSG texts of coactupc.py; "validated.Press" has no space in the source.
const INFO_PROMPT_FOR_SEARCH_KEYS = "Enter or update id of account to update";
const INFO_PROMPT_FOR_CHANGES = "Update account details presented above.";
const INFO_PROMPT_FOR_CONFIRMATION = "Changes validated.Press F5 to save";
const INFO_CONFIRM_UPDATE_SUCCESS = "Changes committed to database";
const INFO_INFORM_FAILURE = "Changes unsuccessful. Please try again";

// 3250-SETUP-INFOMSG: the information line the server sends with each state.
const INFO_BY_STATE: Readonly<Record<UpdateScreenState, string>> = {
    [UpdateScreenState.DETAILS_NOT_FETCHED]: INFO_PROMPT_FOR_SEARCH_KEYS,
    [UpdateScreenState.SHOW_DETAILS]: INFO_PROMPT_FOR_CHANGES,
    [UpdateScreenState.CHANGES_NOT_OK]: INFO_PROMPT_FOR_CHANGES,
    [UpdateScreenState.CHANGES_OK_NOT_CONFIRMED]: INFO_PROMPT_FOR_CONFIRMATION,
    [UpdateScreenState.CHANGES_DONE]: INFO_CONFIRM_UPDATE_SUCCESS,
    [UpdateScreenState.CHANGES_FAILED]: INFO_INFORM_FAILURE,
};

// MUI 9 marks an Alert's colour as MuiAlert-color<Severity> (Alert/alertClasses.d.ts).
const SEVERITY_CLASS_NAMES: Readonly<Record<MessageSeverity, string>> = {
    error: "MuiAlert-colorError",
    success: "MuiAlert-colorSuccess",
    info: "MuiAlert-colorInfo",
};
const SEVERITIES: readonly MessageSeverity[] = ["error", "success", "info"];

// OPNYEAR/OPNMON/OPNDAY and the other dates: LENGTH 4, 2 and 2.
function DateCases(group: string, [year, month, day]: ThreePartNames): FieldCase[] {
    return [
        { name: year, label: "Year", maxLength: 4, group },
        { name: month, label: "Month", maxLength: 2, group },
        { name: day, label: "Day", maxLength: 2, group },
    ];
}

// ACSPH1A/ACSPH1B/ACSPH1C and ACSPH2A/ACSPH2B/ACSPH2C: LENGTH 3, 3 and 4.
function PhoneCases(group: string, [area, prefix, line]: ThreePartNames): FieldCase[] {
    return [
        { name: area, label: "Area code", maxLength: 3, group },
        { name: prefix, label: "Prefix code", maxLength: 3, group },
        { name: line, label: "Line number code", maxLength: 4, group },
    ];
}

// Every COACTUP input (ACCTSID through ACSPFLG) in page order, with the DFHMDF INITIAL label as
// the page renders it (trimmed at both ends, inner blanks kept) and the BMS LENGTH.
const FIELD_CASES: readonly FieldCase[] = [
    { name: "acct_id", label: "Account Number :", maxLength: 11 },
    { name: "active_status", label: "Active Y/N:", maxLength: 1 },
    { name: "credit_limit", label: "Credit Limit        :", maxLength: 15 },
    { name: "cash_credit_limit", label: "Cash credit Limit   :", maxLength: 15 },
    { name: "curr_balance", label: "Current Balance     :", maxLength: 15 },
    { name: "curr_cycle_credit", label: "Current Cycle Credit:", maxLength: 15 },
    { name: "curr_cycle_debit", label: "Current Cycle Debit :", maxLength: 15 },
    { name: "group_id", label: "Account Group:", maxLength: 10 },
    ...DateCases("Opened :", ["open_year", "open_month", "open_day"]),
    ...DateCases("Expiry :", ["expiration_year", "expiration_month", "expiration_day"]),
    ...DateCases("Reissue:", ["reissue_year", "reissue_month", "reissue_day"]),
    { name: "cust_id", label: "Customer id  :", maxLength: 9 },
    { name: "ssn_part_1", label: "First 3 chars", maxLength: 3, group: "SSN:" },
    { name: "ssn_part_2", label: "4th & 5th chars", maxLength: 2, group: "SSN:" },
    { name: "ssn_part_3", label: "Last 4 chars", maxLength: 4, group: "SSN:" },
    ...DateCases("Date of birth:", ["dob_year", "dob_month", "dob_day"]),
    { name: "fico_credit_score", label: "FICO Score:", maxLength: 3 },
    { name: "first_name", label: "First Name", maxLength: 25 },
    { name: "middle_name", label: "Middle Name:", maxLength: 25 },
    { name: "last_name", label: "Last Name :", maxLength: 25 },
    { name: "addr_line_1", label: "Line 1", maxLength: 50, group: "Address:" },
    { name: "addr_line_2", label: "Line 2", maxLength: 50, group: "Address:" },
    { name: "addr_line_3", label: "City", maxLength: 50 },
    { name: "addr_state_cd", label: "State", maxLength: 2 },
    { name: "addr_zip", label: "Zip", maxLength: 5 },
    { name: "addr_country_cd", label: "Country", maxLength: 3 },
    ...PhoneCases("Phone 1:", ["phone_num_1_area", "phone_num_1_prefix", "phone_num_1_line"]),
    { name: "govt_issued_id", label: "Government Issued Id Ref    :", maxLength: 20 },
    ...PhoneCases("Phone 2:", ["phone_num_2_area", "phone_num_2_prefix", "phone_num_2_line"]),
    { name: "eft_account_id", label: "EFT Account Id:", maxLength: 10 },
    { name: "primary_holder_ind", label: "Primary Card Holder Y/N:", maxLength: 1 },
];

const ALL_FIELD_NAMES: readonly AccountFieldName[] = FIELD_CASES.map((fieldCase) => fieldCase.name);
const GROUP_CAPTIONS: readonly string[] = [
    ...new Set(FIELD_CASES.flatMap((fieldCase) => fieldCase.group ?? [])),
];

// 3320-UNPROTECT-FEW-ATTRS opens every detail field but leaves ACSTNUM (customer ID) and
// ACSCTRY (country) at DFHBMPRF; the key ACCTSID is protected once details are shown.
const PROTECTED_WITH_DETAILS: readonly AccountFieldName[] = [
    "acct_id",
    "cust_id",
    "addr_country_cd",
];
const DETAIL_FIELD_NAMES: readonly AccountFieldName[] = ALL_FIELD_NAMES.filter(
    (name) => !PROTECTED_WITH_DETAILS.includes(name),
);
const KEY_FIELD_NAMES: readonly AccountFieldName[] = ["acct_id"];
const NO_FIELD_NAMES: readonly AccountFieldName[] = [];

// The AAP 0.6.9 state table, as 3300-SETUP-SCREEN-ATTRS sets the attribute bytes.
const EDITABLE_BY_STATE_CASES: readonly EditableByStateCase[] = [
    {
        state: UpdateScreenState.DETAILS_NOT_FETCHED,
        description: "the account ID only",
        editableNames: KEY_FIELD_NAMES,
    },
    {
        state: UpdateScreenState.SHOW_DETAILS,
        description: "every detail but the customer ID and country",
        editableNames: DETAIL_FIELD_NAMES,
    },
    {
        state: UpdateScreenState.CHANGES_NOT_OK,
        description: "every detail but the customer ID and country",
        editableNames: DETAIL_FIELD_NAMES,
    },
    {
        state: UpdateScreenState.CHANGES_OK_NOT_CONFIRMED,
        description: "no field",
        editableNames: NO_FIELD_NAMES,
    },
    {
        state: UpdateScreenState.CHANGES_DONE,
        description: "no field",
        editableNames: NO_FIELD_NAMES,
    },
    {
        state: UpdateScreenState.CHANGES_FAILED,
        description: "the account ID only",
        editableNames: KEY_FIELD_NAMES,
    },
];

// Account 00000000004 as the edit fetch shows it; the SSN is unmasked on this screen only,
// because COACTUPC edits it (AAP 0.7.1).
const TEST_FIELDS: Readonly<AccountScreenFields> = {
    acct_id: TEST_ACCOUNT_ID,
    active_status: "Y",
    open_year: "2014",
    open_month: "03",
    open_day: "11",
    credit_limit: "5,600.00",
    expiration_year: "2025",
    expiration_month: "10",
    expiration_day: "01",
    cash_credit_limit: "1,200.00",
    reissue_year: "2024",
    reissue_month: "10",
    reissue_day: "01",
    curr_balance: "2,450.00",
    curr_cycle_credit: "0.00",
    group_id: "",
    curr_cycle_debit: "0.00",
    cust_id: "000000004",
    ssn_part_1: "234",
    ssn_part_2: "56",
    ssn_part_3: "7890",
    dob_year: "1985",
    dob_month: "07",
    dob_day: "15",
    fico_credit_score: "712",
    first_name: "TESTFIRST",
    middle_name: "TESTMIDDLE",
    last_name: "TESTLAST",
    addr_line_1: "100 TEST STREET",
    addr_state_cd: "NY",
    addr_line_2: "APT 1",
    addr_zip: "10001",
    addr_line_3: "TESTCITY",
    addr_country_cd: "USA",
    phone_num_1_area: "212",
    phone_num_1_prefix: "555",
    phone_num_1_line: "0100",
    govt_issued_id: "TESTGOVT0001",
    phone_num_2_area: "212",
    phone_num_2_prefix: "555",
    phone_num_2_line: "0101",
    eft_account_id: "0000000004",
    primary_holder_ind: "Y",
};

// 9700-CHECK-CHANGE-IN-REC found another task's rewrite: the 409 screen shows the fresh record.
const REFRESHED_FIELDS: Readonly<AccountScreenFields> = {
    ...TEST_FIELDS,
    curr_balance: "2,500.00",
};

const ENTER_KEY: UpdateKey = {
    buttonName: "Process (Enter)",
    key: "Enter",
    action: UpdateAction.ENTER,
};
const SAVE_KEY: UpdateKey = { buttonName: "Save (F5)", key: "F5", action: UpdateAction.SAVE };
const CANCEL_KEY: UpdateKey = {
    buttonName: "Cancel (F12)",
    key: "F12",
    action: UpdateAction.CANCEL,
};
const EXIT_KEY: FunctionKey = { buttonName: "Exit (F3)", key: "F3" };
// The keys that send a PUT, as opposed to F3, which only navigates.
const REQUEST_KEYS: readonly UpdateKey[] = [ENTER_KEY, SAVE_KEY, CANCEL_KEY];
const INPUT_METHODS: readonly InputMethod[] = ["click", "key"];
// ENTER, F5 and F12, each by its on-screen button and by its physical key.
const ACTION_KEY_CASES: readonly ActionKeyCase[] = [ENTER_KEY, SAVE_KEY, CANCEL_KEY].flatMap(
    (updateKey) => INPUT_METHODS.map((method) => ({ ...updateKey, method })),
);

function BlankFields(): AccountScreenFields {
    const fields: AccountScreenFields = { ...TEST_FIELDS };
    for (const name of ALL_FIELD_NAMES) {
        fields[name] = "";
    }
    return fields;
}

// The inputs a request carries: the names the state leaves editable, with their values.
function PickFields(
    fields: Readonly<AccountScreenFields>,
    names: readonly AccountFieldName[],
): Partial<AccountScreenFields> {
    const picked: Partial<AccountScreenFields> = {};
    for (const name of names) {
        picked[name] = fields[name];
    }
    return picked;
}

function BuildAccountScreen(
    state: UpdateScreenState,
    overrides: Partial<AccountScreen> = {},
): AccountScreen {
    return {
        fields: { ...TEST_FIELDS },
        state,
        screen_token: FIRST_SCREEN_TOKEN,
        message: "",
        info_message: INFO_BY_STATE[state],
        field_errors: [],
        cursor_field: "",
        ...overrides,
    };
}

// KeyErrorOutcome: a failed 1210-EDIT-ACCOUNT keeps the empty key screen, the key flagged.
function KeyErrorScreen(message: string): AccountScreen {
    return BuildAccountScreen(UpdateScreenState.DETAILS_NOT_FETCHED, {
        fields: BlankFields(),
        message,
        field_errors: ["acct_id"],
        cursor_field: "acct_id",
    });
}

// The body RaiseScreenError sends: the four legacy keys, plus fields, state and screen_token.
function BuildErrorBody(overrides: Partial<AccountErrorBody>): AccountErrorBody {
    return { message: "", info_message: "", field_errors: [], cursor_field: "", ...overrides };
}

function BuildAxiosError(status: number, body: AccountErrorBody): AxiosError<AccountErrorBody> {
    const config: InternalAxiosRequestConfig = { headers: new AxiosHeaders() };
    const response: AxiosResponse<AccountErrorBody> = {
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

// A 2xx screen resolves the next PUT; any other status rejects it with the screen as its body.
function QueueUpdateOutcome(status: number, accountScreen: AccountScreen): void {
    if (status === HTTP_OK) {
        mockUpdateAccount.mockResolvedValueOnce(accountScreen);
        return;
    }
    mockUpdateAccount.mockRejectedValueOnce(BuildAxiosError(status, BuildErrorBody(accountScreen)));
}

function PageTree(): ReactNode {
    return (
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <AccountUpdatePage />
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

// The ScreenShell progress bar shows while a request is in flight.
async function WaitForIdle(): Promise<void> {
    await waitFor(() => {
        expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    });
}

// Menu entry shows the empty key screen; entry with ?acctId= fetches the screen in the state.
async function RenderInState(state: UpdateScreenState): Promise<UserEvent> {
    if (state === UpdateScreenState.DETAILS_NOT_FETCHED) {
        return RenderScreen();
    }
    mockSearchParams = new URLSearchParams({ acctId: TEST_ACCOUNT_ID });
    mockFetchAccountForEdit.mockResolvedValueOnce(BuildAccountScreen(state));
    const user = RenderScreen();
    await WaitForIdle();
    return user;
}

// The identity normalizer: the default one collapses the blanks the BMS labels keep.
function KeepRawText(text: string): string {
    return text;
}

function FieldInput(fieldCase: FieldCase): HTMLInputElement {
    const container =
        fieldCase.group === undefined
            ? document.body
            : screen.getByRole("group", { name: fieldCase.group });
    return within(container).getByLabelText<HTMLInputElement>(fieldCase.label, {
        normalizer: KeepRawText,
    });
}

function InputOf(name: AccountFieldName): HTMLInputElement {
    const fieldCase = FIELD_CASES.find((candidate) => candidate.name === name);
    if (fieldCase === undefined) {
        throw new Error(`No COACTUP field case for ${name}`);
    }
    return FieldInput(fieldCase);
}

// A protected BMS field renders read-only: still focusable, but it cannot be changed.
function EditableFieldNames(): AccountFieldName[] {
    return FIELD_CASES.filter((fieldCase) => !FieldInput(fieldCase).readOnly).map(
        (fieldCase) => fieldCase.name,
    );
}

function ExpectEditable(name: AccountFieldName, isEditable: boolean): void {
    expect(InputOf(name).readOnly).toBe(!isEditable);
}

function SeverityOf(alert: HTMLElement): string {
    const severity = SEVERITIES.find((candidate) =>
        alert.classList.contains(SEVERITY_CLASS_NAMES[candidate]),
    );
    return severity ?? "";
}

function InDocumentOrder(first: Node, second: Node): number {
    return first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
}

// The message line's Alerts in screen order, each with its raw text: ERRMSG has role alert and
// INFOMSG role status.
function ShownAlerts(): ShownAlert[] {
    return [...screen.queryAllByRole("alert"), ...screen.queryAllByRole("status")]
        .sort(InDocumentOrder)
        .map((alert) => [alert.textContent ?? "", SeverityOf(alert)]);
}

function ExpectAlert(text: string, severity: MessageSeverity): void {
    expect(ShownAlerts()).toContainEqual([text, severity]);
}

// The page listens for keydown on window; the async act settles the request it starts.
async function PressKey(key: string): Promise<void> {
    await act(async () => {
        fireEvent.keyDown(window, { key });
    });
}

async function PressFunctionKey(
    user: UserEvent,
    functionKey: FunctionKey,
    method: InputMethod = "click",
): Promise<void> {
    if (method === "key") {
        await PressKey(functionKey.key);
    } else {
        await user.click(screen.getByRole("button", { name: functionKey.buttonName }));
    }
    await WaitForIdle();
}

function LastUpdateCall(): Parameters<typeof UpdateAccount> {
    const lastCall = mockUpdateAccount.mock.lastCall;
    if (lastCall === undefined) {
        throw new Error("UpdateAccount was not called");
    }
    return lastCall;
}

function ExpectUpdateRequest(acctId: string, request: AccountUpdateRequest): void {
    expect(LastUpdateCall()).toStrictEqual([acctId, request]);
}

// One ENTER or F5 and the message line it leaves, at the status coactupc.py assigns. The first
// failing edit's message wins; a completed save shows its information line as the confirmation.
const OUTCOME_CASES: readonly OutcomeCase[] = [
    {
        description: "an edit failure (CHANGES_NOT_OK) as an error",
        startState: UpdateScreenState.SHOW_DETAILS,
        functionKey: ENTER_KEY,
        status: HTTP_EDIT_FAILURE,
        accountScreen: BuildAccountScreen(UpdateScreenState.CHANGES_NOT_OK, {
            message: MSG_CREDIT_LIMIT_NOT_VALID,
            field_errors: ["credit_limit"],
            cursor_field: "credit_limit",
        }),
        expectedAlerts: [
            [MSG_CREDIT_LIMIT_NOT_VALID, "error"],
            [INFO_PROMPT_FOR_CHANGES, "info"],
        ],
    },
    {
        description: "no change detected as an error",
        startState: UpdateScreenState.SHOW_DETAILS,
        functionKey: ENTER_KEY,
        status: HTTP_EDIT_FAILURE,
        accountScreen: BuildAccountScreen(UpdateScreenState.SHOW_DETAILS, {
            message: MSG_NO_CHANGES,
            field_errors: ["active_status"],
            cursor_field: "active_status",
        }),
        expectedAlerts: [
            [MSG_NO_CHANGES, "error"],
            [INFO_PROMPT_FOR_CHANGES, "info"],
        ],
    },
    {
        description: "validated changes (CHANGES_OK_NOT_CONFIRMED) as information",
        startState: UpdateScreenState.SHOW_DETAILS,
        functionKey: ENTER_KEY,
        status: HTTP_OK,
        accountScreen: BuildAccountScreen(UpdateScreenState.CHANGES_OK_NOT_CONFIRMED),
        expectedAlerts: [[INFO_PROMPT_FOR_CONFIRMATION, "info"]],
    },
    {
        description: "a committed save (CHANGES_DONE) as a confirmation",
        startState: UpdateScreenState.CHANGES_OK_NOT_CONFIRMED,
        functionKey: SAVE_KEY,
        status: HTTP_OK,
        accountScreen: BuildAccountScreen(UpdateScreenState.CHANGES_DONE),
        expectedAlerts: [[INFO_CONFIRM_UPDATE_SUCCESS, "success"]],
    },
    {
        description: "a failed rewrite (CHANGES_FAILED) as an error",
        startState: UpdateScreenState.CHANGES_OK_NOT_CONFIRMED,
        functionKey: SAVE_KEY,
        status: HTTP_FILE_ERROR,
        accountScreen: BuildAccountScreen(UpdateScreenState.CHANGES_FAILED, {
            message: MSG_UPDATE_FAILED,
        }),
        expectedAlerts: [
            [MSG_UPDATE_FAILED, "error"],
            [INFO_INFORM_FAILURE, "info"],
        ],
    },
    {
        description: "an invalid account key as an error",
        startState: UpdateScreenState.DETAILS_NOT_FETCHED,
        functionKey: ENTER_KEY,
        status: HTTP_EDIT_FAILURE,
        accountScreen: KeyErrorScreen(MSG_ACCOUNT_KEY_INVALID),
        expectedAlerts: [
            [MSG_ACCOUNT_KEY_INVALID, "error"],
            [INFO_PROMPT_FOR_SEARCH_KEYS, "info"],
        ],
    },
];

// 2000-DECIDE-ACTION after F5: the 409 body re-shows the fresh record (SHOW_DETAILS), and the
// account lock failure ends the update (CHANGES_FAILED, which opens only the key).
const ADOPTION_CASES: readonly AdoptionCase[] = [
    {
        status: HTTP_RECORD_CHANGED,
        message: MSG_RECORD_CHANGED,
        adoptedState: UpdateScreenState.SHOW_DETAILS,
        adoptedFields: REFRESHED_FIELDS,
        expectedFieldNames: DETAIL_FIELD_NAMES,
    },
    {
        status: HTTP_RECORD_LOCKED,
        message: MSG_LOCK_ACCOUNT,
        adoptedState: UpdateScreenState.CHANGES_FAILED,
        adoptedFields: TEST_FIELDS,
        expectedFieldNames: KEY_FIELD_NAMES,
    },
];

// The remaining COACTUPC message builders, each mocked as coactupc.py sends it: the status, the
// state, info_message, field_errors in 3300-SETUP-SCREEN-ATTRS cursor order (FIELD_ORDER) and
// cursor_field, the first of them. Not covered, because no reachable path sends them: "Account
// number not provided" (1200 replaces it with "No input received"), "Details of selected account
// shown above" (3250-SETUP-INFOMSG replaces it), the " can have numbers or alphabets only." suffix
// (no PERFORM reaches 1230 or 1240) and the CSUTLDTC " validation error Sev code: " text (a date
// that passes the CSUTLDPY edits always passes CEEDAYS).

// One failing 1200-EDIT-MAP-INPUTS run on ENTER in SHOW_DETAILS: the values keyed over the
// fetched screen, the first failing edit's TRIM(label(1:25)) + suffix cut to 75 characters, and
// every failing field. storedFields replaces a protected value of the fetched screen.
interface FieldEditCase {
    message: string;
    keyedFields: Partial<AccountScreenFields>;
    fieldErrors: readonly AccountFieldName[];
    storedFields?: Partial<AccountScreenFields>;
}

// The message line and field attributes a failed request leaves; the cursor is the first field.
interface ExpectedFailure {
    message: string;
    infoMessage: string;
    fieldErrors: readonly AccountFieldName[];
}

// ACCT-GROUP-ID: no 1200 edit checks it, so it is never a failing field.
const UNEDITED_FIELD_NAME: AccountFieldName = "group_id";
// MUI marks an input whose TextField is in error with aria-invalid="true".
const MARKED_INPUT_SELECTOR = 'input[aria-invalid="true"]';

// 3203-SHOW-UPDATED-VALUES: a CHANGES_NOT_OK screen shows the values as received, except that an
// amount which passed TEST-NUMVAL-C is shown edited (WS-EDIT-CURRENCY-9-2-F).
const EDITED_TEST_AMOUNTS: Readonly<Partial<AccountScreenFields>> = {
    credit_limit: "+      5,600.00",
    cash_credit_limit: "+      1,200.00",
    curr_balance: "+      2,450.00",
    curr_cycle_credit: "+           .00",
    curr_cycle_debit: "+           .00",
};

// Every reachable field-edit suffix and every edited label, the AAP 0.6.9 pinned cases included.
const FIELD_EDIT_CASES: readonly FieldEditCase[] = [
    // 1220-EDIT-YESNO: supplied (not a space or zero), then upper-case Y or N.
    {
        message: "Account Status must be supplied.",
        keyedFields: { active_status: "" },
        fieldErrors: ["active_status"],
    },
    {
        message: "Account Status must be Y or N.",
        keyedFields: { active_status: "X" },
        fieldErrors: ["active_status"],
    },
    {
        message: "Primary Card Holder must be supplied.",
        keyedFields: { primary_holder_ind: "" },
        fieldErrors: ["primary_holder_ind"],
    },
    {
        message: "Primary Card Holder must be Y or N.",
        keyedFields: { primary_holder_ind: "y" },
        fieldErrors: ["primary_holder_ind"],
    },
    // EDIT-DATE-CCYYMMDD (cpy/CSUTLDPY.cpy) under each date's label: a 30- or 31-day failure flags
    // the month and day, a leap-year failure every part. EDIT-DATE-OF-BIRTH, run only on a valid
    // date, also flags every part.
    {
        message: "Open Date : Year must be supplied.",
        keyedFields: { open_year: "" },
        fieldErrors: ["open_year"],
    },
    {
        message: "Open Date : Century is not valid.",
        keyedFields: { open_year: "1814" },
        fieldErrors: ["open_year"],
    },
    {
        message: "Open Date:Cannot have 31 days in this month.",
        keyedFields: { open_month: "04", open_day: "31" },
        fieldErrors: ["open_month", "open_day"],
    },
    {
        message: "Expiry Date must be 4 digit number.",
        keyedFields: { expiration_year: "20A5" },
        fieldErrors: ["expiration_year"],
    },
    {
        message: "Expiry Date : Day must be supplied.",
        keyedFields: { expiration_day: "" },
        fieldErrors: ["expiration_day"],
    },
    {
        message: "Expiry Date:Cannot have 30 days in this month.",
        keyedFields: { expiration_month: "02", expiration_day: "30" },
        fieldErrors: ["expiration_month", "expiration_day"],
    },
    {
        message: "Reissue Date : Month must be supplied.",
        keyedFields: { reissue_month: "" },
        fieldErrors: ["reissue_month"],
    },
    {
        message: "Reissue Date:day must be a number between 1 and 31.",
        keyedFields: { reissue_day: "32" },
        fieldErrors: ["reissue_day"],
    },
    {
        message: "Reissue Date:Not a leap year.Cannot have 29 days in this month.",
        keyedFields: { reissue_year: "2023", reissue_month: "02", reissue_day: "29" },
        fieldErrors: ["reissue_year", "reissue_month", "reissue_day"],
    },
    {
        message: "Date of Birth: Month must be a number between 1 and 12.",
        keyedFields: { dob_month: "13" },
        fieldErrors: ["dob_month"],
    },
    {
        message: "Date of Birth:cannot be in the future ",
        keyedFields: { dob_year: "2099" },
        fieldErrors: ["dob_year", "dob_month", "dob_day"],
    },
    // 1250-EDIT-SIGNED-9V2: supplied, then TEST-NUMVAL-C. WS-EDIT-VARIABLE-NAME X(25) cuts the
    // label; "Credit Limit is not valid" is the first OUTCOME_CASES case.
    {
        message: "Credit Limit must be supplied.",
        keyedFields: { credit_limit: "" },
        fieldErrors: ["credit_limit"],
    },
    {
        message: "Cash Credit Limit must be supplied.",
        keyedFields: { cash_credit_limit: "" },
        fieldErrors: ["cash_credit_limit"],
    },
    {
        message: "Cash Credit Limit is not valid",
        keyedFields: { cash_credit_limit: "12A" },
        fieldErrors: ["cash_credit_limit"],
    },
    {
        message: "Current Balance must be supplied.",
        keyedFields: { curr_balance: "" },
        fieldErrors: ["curr_balance"],
    },
    {
        message: "Current Balance is not valid",
        keyedFields: { curr_balance: "1.2.3" },
        fieldErrors: ["curr_balance"],
    },
    {
        message: "Current Cycle Credit Limi must be supplied.",
        keyedFields: { curr_cycle_credit: "" },
        fieldErrors: ["curr_cycle_credit"],
    },
    {
        message: "Current Cycle Credit Limi is not valid",
        keyedFields: { curr_cycle_credit: "ABC" },
        fieldErrors: ["curr_cycle_credit"],
    },
    {
        message: "Current Cycle Debit Limit must be supplied.",
        keyedFields: { curr_cycle_debit: "" },
        fieldErrors: ["curr_cycle_debit"],
    },
    {
        message: "Current Cycle Debit Limit is not valid",
        keyedFields: { curr_cycle_debit: "--5" },
        fieldErrors: ["curr_cycle_debit"],
    },
    // 1265-EDIT-US-SSN: each part under its own label. Parts 2 and 3 are edited once part 1 passes
    // 1245-EDIT-NUM-REQD, so a part 1 of 9xx still has its part 3 flagged.
    {
        message: "SSN: First 3 chars must be supplied.",
        keyedFields: { ssn_part_1: "" },
        fieldErrors: ["ssn_part_1"],
    },
    {
        message: "SSN: First 3 chars must be all numeric.",
        keyedFields: { ssn_part_1: "2A4" },
        fieldErrors: ["ssn_part_1"],
    },
    {
        message: "SSN: First 3 chars must not be zero.",
        keyedFields: { ssn_part_1: "000" },
        fieldErrors: ["ssn_part_1"],
    },
    {
        message: "SSN: First 3 chars: should not be 000, 666, or between 900 and 999",
        keyedFields: { ssn_part_1: "666" },
        fieldErrors: ["ssn_part_1"],
    },
    {
        message: "SSN: First 3 chars: should not be 000, 666, or between 900 and 999",
        keyedFields: { ssn_part_1: "950", ssn_part_3: "0000" },
        fieldErrors: ["ssn_part_1", "ssn_part_3"],
    },
    {
        message: "SSN 4th & 5th chars must be supplied.",
        keyedFields: { ssn_part_2: "" },
        fieldErrors: ["ssn_part_2"],
    },
    {
        message: "SSN 4th & 5th chars must be all numeric.",
        keyedFields: { ssn_part_2: "5A" },
        fieldErrors: ["ssn_part_2"],
    },
    {
        message: "SSN 4th & 5th chars must not be zero.",
        keyedFields: { ssn_part_2: "00" },
        fieldErrors: ["ssn_part_2"],
    },
    {
        message: "SSN Last 4 chars must be supplied.",
        keyedFields: { ssn_part_3: "" },
        fieldErrors: ["ssn_part_3"],
    },
    {
        message: "SSN Last 4 chars must be all numeric.",
        keyedFields: { ssn_part_3: "78A0" },
        fieldErrors: ["ssn_part_3"],
    },
    {
        message: "SSN Last 4 chars must not be zero.",
        keyedFields: { ssn_part_3: "0000" },
        fieldErrors: ["ssn_part_3"],
    },
    // 1245-EDIT-NUM-REQD, then 1275-EDIT-FICO-SCORE.
    {
        message: "FICO Score must be supplied.",
        keyedFields: { fico_credit_score: "" },
        fieldErrors: ["fico_credit_score"],
    },
    {
        message: "FICO Score must be all numeric.",
        keyedFields: { fico_credit_score: "7A2" },
        fieldErrors: ["fico_credit_score"],
    },
    {
        message: "FICO Score must not be zero.",
        keyedFields: { fico_credit_score: "000" },
        fieldErrors: ["fico_credit_score"],
    },
    {
        message: "FICO Score: should be between 300 and 850",
        keyedFields: { fico_credit_score: "299" },
        fieldErrors: ["fico_credit_score"],
    },
    // 1225-EDIT-ALPHA-REQD, 1235-EDIT-ALPHA-OPT (Middle Name) and 1215-EDIT-MANDATORY.
    {
        message: "First Name must be supplied.",
        keyedFields: { first_name: "" },
        fieldErrors: ["first_name"],
    },
    {
        message: "First Name can have alphabets only.",
        keyedFields: { first_name: "TEST1" },
        fieldErrors: ["first_name"],
    },
    {
        message: "Middle Name can have alphabets only.",
        keyedFields: { middle_name: "TEST-M" },
        fieldErrors: ["middle_name"],
    },
    {
        message: "Last Name must be supplied.",
        keyedFields: { last_name: "" },
        fieldErrors: ["last_name"],
    },
    {
        message: "Last Name can have alphabets only.",
        keyedFields: { last_name: "O'TEST" },
        fieldErrors: ["last_name"],
    },
    {
        message: "Address Line 1 must be supplied.",
        keyedFields: { addr_line_1: "" },
        fieldErrors: ["addr_line_1"],
    },
    {
        message: "City must be supplied.",
        keyedFields: { addr_line_3: "" },
        fieldErrors: ["addr_line_3"],
    },
    {
        message: "City can have alphabets only.",
        keyedFields: { addr_line_3: "TESTCITY9" },
        fieldErrors: ["addr_line_3"],
    },
    // 1225 then 1270-EDIT-US-STATE-CD, 1245-EDIT-NUM-REQD for the ZIP, and
    // 1280-EDIT-US-STATE-ZIP-CD, whose fixed literal flags the state and the ZIP.
    {
        message: "State must be supplied.",
        keyedFields: { addr_state_cd: "" },
        fieldErrors: ["addr_state_cd"],
    },
    {
        message: "State can have alphabets only.",
        keyedFields: { addr_state_cd: "N1" },
        fieldErrors: ["addr_state_cd"],
    },
    {
        message: "State: is not a valid state code",
        keyedFields: { addr_state_cd: "XX" },
        fieldErrors: ["addr_state_cd"],
    },
    {
        message: "Zip must be supplied.",
        keyedFields: { addr_zip: "" },
        fieldErrors: ["addr_zip"],
    },
    {
        message: "Zip must be all numeric.",
        keyedFields: { addr_zip: "1000A" },
        fieldErrors: ["addr_zip"],
    },
    {
        message: "Zip must not be zero.",
        keyedFields: { addr_zip: "00000" },
        fieldErrors: ["addr_zip"],
    },
    {
        message: "Invalid zip code for state",
        keyedFields: { addr_zip: "90001" },
        fieldErrors: ["addr_state_cd", "addr_zip"],
    },
    // ACSCTRY is protected, so 1225 edits the country the fetch stored. A keyed change lets the
    // edits run, and the cursor still goes to the read-only input.
    {
        message: "Country must be supplied.",
        keyedFields: { middle_name: "TESTMIDDLEB" },
        fieldErrors: ["addr_country_cd"],
        storedFields: { addr_country_cd: "" },
    },
    {
        message: "Country can have alphabets only.",
        keyedFields: { middle_name: "TESTMIDDLEB" },
        fieldErrors: ["addr_country_cd"],
        storedFields: { addr_country_cd: "U5A" },
    },
    // 1260-EDIT-US-PHONE-NUM: each failing part is flagged alone.
    {
        message: "Phone Number 1: Area code must be supplied.",
        keyedFields: { phone_num_1_area: "" },
        fieldErrors: ["phone_num_1_area"],
    },
    {
        message: "Phone Number 1: Area code must be A 3 digit number.",
        keyedFields: { phone_num_1_area: "2A2" },
        fieldErrors: ["phone_num_1_area"],
    },
    {
        message: "Phone Number 1: Area code cannot be zero",
        keyedFields: { phone_num_1_area: "000" },
        fieldErrors: ["phone_num_1_area"],
    },
    {
        message: "Phone Number 1: Prefix code must be supplied.",
        keyedFields: { phone_num_1_prefix: "" },
        fieldErrors: ["phone_num_1_prefix"],
    },
    {
        message: "Phone Number 1: Line number code must be A 4 digit number.",
        keyedFields: { phone_num_1_line: "01A0" },
        fieldErrors: ["phone_num_1_line"],
    },
    {
        message: "Phone Number 2: Not valid North America general purpose area code",
        keyedFields: { phone_num_2_area: "123" },
        fieldErrors: ["phone_num_2_area"],
    },
    {
        message: "Phone Number 2: Prefix code must be A 3 digit number.",
        keyedFields: { phone_num_2_prefix: "5A5" },
        fieldErrors: ["phone_num_2_prefix"],
    },
    {
        message: "Phone Number 2: Prefix code cannot be zero",
        keyedFields: { phone_num_2_prefix: "000" },
        fieldErrors: ["phone_num_2_prefix"],
    },
    {
        message: "Phone Number 2: Line number code must be supplied.",
        keyedFields: { phone_num_2_line: "" },
        fieldErrors: ["phone_num_2_line"],
    },
    {
        message: "Phone Number 2: Line number code cannot be zero",
        keyedFields: { phone_num_2_line: "0000" },
        fieldErrors: ["phone_num_2_line"],
    },
    // 1245-EDIT-NUM-REQD.
    {
        message: "EFT Account Id must be supplied.",
        keyedFields: { eft_account_id: "" },
        fieldErrors: ["eft_account_id"],
    },
    {
        message: "EFT Account Id must be all numeric.",
        keyedFields: { eft_account_id: "000000000A" },
        fieldErrors: ["eft_account_id"],
    },
    {
        message: "EFT Account Id must not be zero.",
        keyedFields: { eft_account_id: "0000000000" },
        fieldErrors: ["eft_account_id"],
    },
];

// 9600-WRITE-PROCESSING's WRITE_OUTCOMES text when READ UPDATE of CUSTDAT fails.
const MSG_LOCK_CUSTOMER = "Could not lock customer record for update";

// The status of the three NOTFND builders (app.core.errors); a READ file error is a 503.
const HTTP_NOT_FOUND = 404;
// The ACCTSID of the read failures. The seed data has no cross-reference for it, so a live read
// gives the CXACAIX NOTFND; the other failures need a failing file.
const READ_FAILURE_ACCOUNT_ID = "00000000099";

// One failed 9000-READ-ACCT (coactvwc.ReadAccountChain), with the text its builder composes for
// READ_FAILURE_ACCOUNT_ID and the customer its cross-reference names.
interface ReadFailureCase {
    source: string;
    status: number;
    message: string;
}

const READ_FAILURE_CASES: readonly ReadFailureCase[] = [
    {
        source: "9200-GETCARDXREF-BYACCT NOTFND",
        status: HTTP_NOT_FOUND,
        message: "Account:00000000099 not found in Cross ref file.  Resp:000000013  Reas:0000",
    },
    {
        source: "9300-GETACCTDATA-BYACCT NOTFND",
        status: HTTP_NOT_FOUND,
        message: "Account:00000000099 not found in Acct Master file.Resp:000000013  Reas:0000",
    },
    {
        source: "9400-GETCUSTDATA-BYCUST NOTFND",
        status: HTTP_NOT_FOUND,
        message: "CustId:000000099 not found in customer master.Resp: 000000013  REAS:0000000",
    },
    {
        source: "batch-window CXACAIX NOTOPEN",
        status: HTTP_FILE_ERROR,
        message: "File Error: READ     on CXACAIX   returned RESP 000000019 ,RESP2 000000060",
    },
    {
        source: "ACCTDAT IOERR",
        status: HTTP_FILE_ERROR,
        message: "File Error: READ     on ACCTDAT   returned RESP 000000017 ,RESP2 000000000",
    },
    {
        source: "CUSTDAT IOERR",
        status: HTTP_FILE_ERROR,
        message: "File Error: READ     on CUSTDAT   returned RESP 000000017 ,RESP2 000000000",
    },
];
// The file errors name no key, so they also stand for a failed F12 re-read of the fetched account.
const READ_FILE_ERROR_CASES: readonly ReadFailureCase[] = READ_FAILURE_CASES.filter(
    (readCase) => readCase.status === HTTP_FILE_ERROR,
);

// No catch-all handler is registered (AAP 0.6.5): an unexpected error is Starlette's plain-text
// 500, which the page shows as the ABEND-ROUTINE text (CSMSG02Y ABEND-MSG).
const HTTP_INTERNAL_SERVER_ERROR = 500;
const INTERNAL_SERVER_ERROR_TEXT = "Internal Server Error";
const MSG_ABEND = "UNEXPECTED ABEND OCCURRED.";

// Entry with ?acctId= fetches the SHOW_DETAILS screen, with the stored values a case replaces.
async function RenderFetchedDetails(
    storedFields: Partial<AccountScreenFields> = {},
): Promise<UserEvent> {
    mockSearchParams = new URLSearchParams({ acctId: TEST_ACCOUNT_ID });
    const fetchedFields: AccountScreenFields = { ...TEST_FIELDS, ...storedFields };
    mockFetchAccountForEdit.mockResolvedValueOnce(
        BuildAccountScreen(UpdateScreenState.SHOW_DETAILS, { fields: fetchedFields }),
    );
    const user = RenderScreen();
    await WaitForIdle();
    return user;
}

// Keys each value over the fetched text: the field is cleared, then the value typed.
async function KeyFields(
    user: UserEvent,
    keyedFields: Partial<AccountScreenFields>,
): Promise<void> {
    for (const name of ALL_FIELD_NAMES) {
        const value = keyedFields[name];
        if (value === undefined) {
            continue;
        }
        await user.clear(InputOf(name));
        if (value !== "") {
            await user.type(InputOf(name), value);
        }
    }
}

// The screen values the ENTER carries: the fetched ones with the case's keyed values.
function KeyedScreenFields(editCase: FieldEditCase): AccountScreenFields {
    return { ...TEST_FIELDS, ...editCase.storedFields, ...editCase.keyedFields };
}

function CursorFieldOf(fieldErrors: readonly AccountFieldName[]): AccountFieldName {
    const cursorField = fieldErrors[0];
    if (cursorField === undefined) {
        throw new Error("A failed COACTUPC screen names at least one field");
    }
    return cursorField;
}

// EditFailureOutcome: the 400 CHANGES_NOT_OK screen, which keeps the before-image in its token.
function FieldEditScreen(editCase: FieldEditCase): AccountScreen {
    return BuildAccountScreen(UpdateScreenState.CHANGES_NOT_OK, {
        fields: {
            ...TEST_FIELDS,
            ...EDITED_TEST_AMOUNTS,
            ...editCase.storedFields,
            ...editCase.keyedFields,
        },
        message: editCase.message,
        field_errors: [...editCase.fieldErrors],
        cursor_field: CursorFieldOf(editCase.fieldErrors),
    });
}

// AAP 0.6.3: ERRMSG in red with INFOMSG after it, as sent; every field the server names is marked
// and no other; the cursor is on the first of them.
function ExpectFailureShown(expected: ExpectedFailure): void {
    expect(ShownAlerts()).toEqual([
        [expected.message, "error"],
        [expected.infoMessage, "info"],
    ]);
    const markedInputs = [...document.querySelectorAll(MARKED_INPUT_SELECTOR)];
    expect(markedInputs).toHaveLength(expected.fieldErrors.length);
    for (const name of expected.fieldErrors) {
        expect(markedInputs).toContain(InputOf(name));
    }
    expect(InputOf(UNEDITED_FIELD_NAME)).toHaveAttribute("aria-invalid", "false");
    expect(InputOf(CursorFieldOf(expected.fieldErrors))).toHaveFocus();
}

async function ShowsFieldEditFailure(editCase: FieldEditCase): Promise<void> {
    const user = await RenderFetchedDetails(editCase.storedFields);
    await KeyFields(user, editCase.keyedFields);
    QueueUpdateOutcome(HTTP_EDIT_FAILURE, FieldEditScreen(editCase));
    await PressFunctionKey(user, ENTER_KEY);
    ExpectUpdateRequest(TEST_ACCOUNT_ID, {
        action: UpdateAction.ENTER,
        screen_token: FIRST_SCREEN_TOKEN,
        fields: PickFields(KeyedScreenFields(editCase), DETAIL_FIELD_NAMES),
    });
    ExpectFailureShown({
        message: editCase.message,
        infoMessage: INFO_PROMPT_FOR_CHANGES,
        fieldErrors: editCase.fieldErrors,
    });
}

// The 423 body of a CUSTDAT lock failure: nothing is written and the update ends in
// CHANGES_FAILED. No field failed, so app.core.errors.ErrorBody names the cursor field ACCTSID
// in field_errors.
function CustomerLockScreen(): AccountScreen {
    return BuildAccountScreen(UpdateScreenState.CHANGES_FAILED, {
        screen_token: ADOPTED_SCREEN_TOKEN,
        message: MSG_LOCK_CUSTOMER,
        field_errors: [...KEY_FIELD_NAMES],
        cursor_field: "acct_id",
    });
}

// CHANGES_FAILED opens only the key: the next ENTER sends ACCTSID alone, with the failed save's
// screen_token, and the server answers with the empty key screen.
async function SendsKeyAfterFailedSave(user: UserEvent): Promise<void> {
    const keyScreen = BuildAccountScreen(UpdateScreenState.DETAILS_NOT_FETCHED, {
        fields: BlankFields(),
    });
    QueueUpdateOutcome(HTTP_OK, keyScreen);
    await PressFunctionKey(user, ENTER_KEY);
    ExpectUpdateRequest(TEST_ACCOUNT_ID, {
        action: UpdateAction.ENTER,
        screen_token: ADOPTED_SCREEN_TOKEN,
        fields: { acct_id: TEST_ACCOUNT_ID },
    });
}

async function ShowsCustomerLockFailure(): Promise<void> {
    const user = await RenderInState(UpdateScreenState.CHANGES_OK_NOT_CONFIRMED);
    QueueUpdateOutcome(HTTP_RECORD_LOCKED, CustomerLockScreen());
    await PressFunctionKey(user, SAVE_KEY);
    ExpectUpdateRequest(TEST_ACCOUNT_ID, {
        action: UpdateAction.SAVE,
        screen_token: FIRST_SCREEN_TOKEN,
        fields: {},
    });
    ExpectFailureShown({
        message: MSG_LOCK_CUSTOMER,
        infoMessage: INFO_INFORM_FAILURE,
        fieldErrors: KEY_FIELD_NAMES,
    });
    await SendsKeyAfterFailedSave(user);
}

// ReadFailureOutcome: the DETAILS_NOT_FETCHED screen shows only the key as received. Whichever
// file failed, the error body names ACCTSID as the failing field and the cursor.
function ReadFailureScreen(readCase: ReadFailureCase): AccountScreen {
    return BuildAccountScreen(UpdateScreenState.DETAILS_NOT_FETCHED, {
        fields: { ...BlankFields(), acct_id: READ_FAILURE_ACCOUNT_ID },
        message: readCase.message,
        field_errors: [...KEY_FIELD_NAMES],
        cursor_field: "acct_id",
    });
}

// The key stays on the screen, open for the next ENTER.
function ExpectReadFailureShown(readCase: ReadFailureCase): void {
    ExpectFailureShown({
        message: readCase.message,
        infoMessage: INFO_PROMPT_FOR_SEARCH_KEYS,
        fieldErrors: KEY_FIELD_NAMES,
    });
    expect(InputOf("acct_id")).toHaveValue(READ_FAILURE_ACCOUNT_ID);
    ExpectEditable("acct_id", true);
}

// 1210-EDIT-ACCOUNT passes the ?acctId= key, then its read fails.
async function ShowsEntryFetchFailure(readCase: ReadFailureCase): Promise<void> {
    mockSearchParams = new URLSearchParams({ acctId: READ_FAILURE_ACCOUNT_ID });
    const errorBody = BuildErrorBody(ReadFailureScreen(readCase));
    mockFetchAccountForEdit.mockRejectedValueOnce(BuildAxiosError(readCase.status, errorBody));
    RenderScreen();
    await WaitForIdle();
    expect(mockFetchAccountForEdit).toHaveBeenCalledWith(READ_FAILURE_ACCOUNT_ID, READ_OPTIONS);
    ExpectReadFailureShown(readCase);
}

// ENTER on the empty key screen with the key typed reads the same chain.
async function ShowsKeyEnterFailure(readCase: ReadFailureCase): Promise<void> {
    const user = await RenderInState(UpdateScreenState.DETAILS_NOT_FETCHED);
    await user.type(InputOf("acct_id"), READ_FAILURE_ACCOUNT_ID);
    QueueUpdateOutcome(readCase.status, ReadFailureScreen(readCase));
    await PressFunctionKey(user, ENTER_KEY);
    ExpectUpdateRequest(READ_FAILURE_ACCOUNT_ID, {
        action: UpdateAction.ENTER,
        screen_token: "",
        fields: { acct_id: READ_FAILURE_ACCOUNT_ID },
    });
    ExpectReadFailureShown(readCase);
}

// F12 re-reads the token's account; CancelReadFailureOutcome keeps the provisional SHOW_DETAILS
// screen, its values and its open fields, and flags the key.
async function ShowsCancelReadFailure(readCase: ReadFailureCase): Promise<void> {
    const user = await RenderInState(UpdateScreenState.SHOW_DETAILS);
    const provisionalScreen = BuildAccountScreen(UpdateScreenState.SHOW_DETAILS, {
        message: readCase.message,
        field_errors: [...KEY_FIELD_NAMES],
        cursor_field: "acct_id",
    });
    QueueUpdateOutcome(readCase.status, provisionalScreen);
    await PressFunctionKey(user, CANCEL_KEY);
    ExpectFailureShown({
        message: readCase.message,
        infoMessage: INFO_PROMPT_FOR_CHANGES,
        fieldErrors: KEY_FIELD_NAMES,
    });
    ExpectEditable("credit_limit", true);
}

function BuildServerError(): AxiosError<string> {
    const config: InternalAxiosRequestConfig = { headers: new AxiosHeaders() };
    const response: AxiosResponse<string> = {
        status: HTTP_INTERNAL_SERVER_ERROR,
        statusText: "",
        headers: {},
        config,
        data: INTERNAL_SERVER_ERROR_TEXT,
    };
    return new AxiosError(
        "Request failed",
        AxiosError.ERR_BAD_RESPONSE,
        config,
        undefined,
        response,
    );
}

// The 500 carries no screen, so the page keeps the fetched one and marks no field.
async function ShowsAbendOnServerError(): Promise<void> {
    const user = await RenderInState(UpdateScreenState.SHOW_DETAILS);
    mockUpdateAccount.mockRejectedValueOnce(BuildServerError());
    await PressFunctionKey(user, ENTER_KEY);
    expect(ShownAlerts()).toEqual([[MSG_ABEND, "error"]]);
    expect(document.querySelectorAll(MARKED_INPUT_SELECTOR)).toHaveLength(0);
    expect(InputOf("credit_limit")).toHaveValue(TEST_FIELDS.credit_limit);
    ExpectEditable("credit_limit", true);
}

function ShowsTitleAndCaptions(): void {
    RenderScreen();
    expect(screen.getByRole("heading", { level: 1, name: "Update Account" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Customer Details" })).toBeInTheDocument();
    expect(screen.getByText("Tran: CAUP")).toBeInTheDocument();
    expect(screen.getByText("Prog: COACTUPC")).toBeInTheDocument();
    for (const caption of GROUP_CAPTIONS) {
        expect(screen.getByRole("group", { name: caption })).toBeInTheDocument();
    }
}

function RendersEveryScreenInput(): void {
    RenderScreen();
    expect(screen.getAllByRole("textbox")).toHaveLength(FIELD_CASES.length);
    expect([...ALL_FIELD_NAMES].sort()).toEqual(Object.keys(TEST_FIELDS).sort());
}

function LabelsInputWithLength(fieldCase: FieldCase): void {
    RenderScreen();
    expect(FieldInput(fieldCase)).toHaveAttribute("maxlength", String(fieldCase.maxLength));
}

async function OpensFieldsOfState(editableCase: EditableByStateCase): Promise<void> {
    await RenderInState(editableCase.state);
    expect(EditableFieldNames()).toEqual(editableCase.editableNames);
}

async function StartsOnEmptyKeyScreen(): Promise<void> {
    await RenderInState(UpdateScreenState.DETAILS_NOT_FETCHED);
    expect(mockFetchAccountForEdit).not.toHaveBeenCalled();
    expect(InputOf("acct_id")).toHaveValue("");
    expect(InputOf("acct_id")).toHaveFocus();
    expect(ShownAlerts()).toEqual([]);
}

async function FetchesAccountOfEntryKey(): Promise<void> {
    await RenderInState(UpdateScreenState.SHOW_DETAILS);
    expect(mockFetchAccountForEdit).toHaveBeenCalledWith(TEST_ACCOUNT_ID, READ_OPTIONS);
    expect(InputOf("acct_id")).toHaveValue(TEST_ACCOUNT_ID);
    const ssnParts = [InputOf("ssn_part_1"), InputOf("ssn_part_2"), InputOf("ssn_part_3")];
    expect(ssnParts.map((input) => input.value)).toEqual(["234", "56", "7890"]);
    expect(ShownAlerts()).toEqual([[INFO_PROMPT_FOR_CHANGES, "info"]]);
}

async function SendsOnlyKeyBeforeFetch(): Promise<void> {
    const user = await RenderInState(UpdateScreenState.DETAILS_NOT_FETCHED);
    await user.type(InputOf("acct_id"), TEST_ACCOUNT_ID);
    QueueUpdateOutcome(HTTP_OK, BuildAccountScreen(UpdateScreenState.SHOW_DETAILS));
    await PressFunctionKey(user, ENTER_KEY);
    ExpectUpdateRequest(TEST_ACCOUNT_ID, {
        action: UpdateAction.ENTER,
        screen_token: "",
        fields: { acct_id: TEST_ACCOUNT_ID },
    });
    ExpectEditable("acct_id", false);
    ExpectEditable("credit_limit", true);
}

async function SendsBlankKeyToKeyEdit(): Promise<void> {
    const user = await RenderInState(UpdateScreenState.DETAILS_NOT_FETCHED);
    QueueUpdateOutcome(HTTP_EDIT_FAILURE, KeyErrorScreen(MSG_NO_INPUT_RECEIVED));
    await PressFunctionKey(user, ENTER_KEY);
    ExpectUpdateRequest(BLANK_ACCOUNT_PATH, {
        action: UpdateAction.ENTER,
        screen_token: "",
        fields: { acct_id: "" },
    });
    expect(ShownAlerts()).toEqual([
        [MSG_NO_INPUT_RECEIVED, "error"],
        [INFO_PROMPT_FOR_SEARCH_KEYS, "info"],
    ]);
    expect(InputOf("acct_id")).toHaveAttribute("aria-invalid", "true");
}

async function SendsActionWithHeldToken(actionKeyCase: ActionKeyCase): Promise<void> {
    const user = await RenderInState(UpdateScreenState.SHOW_DETAILS);
    QueueUpdateOutcome(HTTP_OK, BuildAccountScreen(UpdateScreenState.SHOW_DETAILS));
    await PressFunctionKey(user, actionKeyCase, actionKeyCase.method);
    ExpectUpdateRequest(TEST_ACCOUNT_ID, {
        action: actionKeyCase.action,
        screen_token: FIRST_SCREEN_TOKEN,
        fields: PickFields(TEST_FIELDS, DETAIL_FIELD_NAMES),
    });
}

async function SendsKeyedTextUnchanged(): Promise<void> {
    const user = await RenderInState(UpdateScreenState.SHOW_DETAILS);
    await user.clear(InputOf("credit_limit"));
    await user.type(InputOf("credit_limit"), KEYED_CREDIT_LIMIT);
    QueueUpdateOutcome(HTTP_OK, BuildAccountScreen(UpdateScreenState.CHANGES_OK_NOT_CONFIRMED));
    await PressFunctionKey(user, ENTER_KEY);
    ExpectUpdateRequest(TEST_ACCOUNT_ID, {
        action: UpdateAction.ENTER,
        screen_token: FIRST_SCREEN_TOKEN,
        fields: PickFields(
            { ...TEST_FIELDS, credit_limit: KEYED_CREDIT_LIMIT },
            DETAIL_FIELD_NAMES,
        ),
    });
}

async function SendsTokenOfLatestResponse(): Promise<void> {
    const user = await RenderInState(UpdateScreenState.SHOW_DETAILS);
    const validatedScreen = BuildAccountScreen(UpdateScreenState.CHANGES_OK_NOT_CONFIRMED, {
        screen_token: NEXT_SCREEN_TOKEN,
    });
    QueueUpdateOutcome(HTTP_OK, validatedScreen);
    await PressFunctionKey(user, ENTER_KEY);
    QueueUpdateOutcome(
        HTTP_OK,
        BuildAccountScreen(UpdateScreenState.CHANGES_DONE, { screen_token: FINAL_SCREEN_TOKEN }),
    );
    await PressFunctionKey(user, SAVE_KEY);
    ExpectUpdateRequest(TEST_ACCOUNT_ID, {
        action: UpdateAction.SAVE,
        screen_token: NEXT_SCREEN_TOKEN,
        fields: {},
    });
}

function ExpectRequestKeysEnabled(isEnabled: boolean): void {
    for (const requestKey of REQUEST_KEYS) {
        const button = screen.getByRole("button", { name: requestKey.buttonName });
        expect(button).toHaveProperty("disabled", !isEnabled);
    }
}

// The PUT stays in flight until the test settles it: meanwhile ENTER, F5 and F12 are disabled
// and ignore their physical key, while F3 stays available.
async function DisablesRequestKeysWhileBusy(updateKey: UpdateKey): Promise<void> {
    const user = await RenderInState(UpdateScreenState.SHOW_DETAILS);
    let settleUpdate: (accountScreen: AccountScreen) => void = () => undefined;
    mockUpdateAccount.mockReturnValueOnce(new Promise((resolve) => (settleUpdate = resolve)));
    await user.click(screen.getByRole("button", { name: updateKey.buttonName }));
    ExpectRequestKeysEnabled(false);
    expect(screen.getByRole("button", { name: EXIT_KEY.buttonName })).toBeEnabled();
    await PressKey(updateKey.key);
    expect(mockUpdateAccount).toHaveBeenCalledTimes(1);
    await act(async () => settleUpdate(BuildAccountScreen(UpdateScreenState.SHOW_DETAILS)));
    await WaitForIdle();
    ExpectRequestKeysEnabled(true);
}

async function AdoptsScreenOfErrorBody(adoptionCase: AdoptionCase): Promise<void> {
    const user = await RenderInState(UpdateScreenState.CHANGES_OK_NOT_CONFIRMED);
    const errorScreen = BuildAccountScreen(adoptionCase.adoptedState, {
        fields: { ...adoptionCase.adoptedFields },
        screen_token: ADOPTED_SCREEN_TOKEN,
        message: adoptionCase.message,
    });
    QueueUpdateOutcome(adoptionCase.status, errorScreen);
    await PressFunctionKey(user, SAVE_KEY);
    ExpectAlert(adoptionCase.message, "error");
    ExpectAlert(INFO_BY_STATE[adoptionCase.adoptedState], "info");
    QueueUpdateOutcome(HTTP_OK, BuildAccountScreen(adoptionCase.adoptedState));
    await PressFunctionKey(user, ENTER_KEY);
    ExpectUpdateRequest(TEST_ACCOUNT_ID, {
        action: UpdateAction.ENTER,
        screen_token: ADOPTED_SCREEN_TOKEN,
        fields: PickFields(adoptionCase.adoptedFields, adoptionCase.expectedFieldNames),
    });
}

async function ShowsOutcomeLiterals(outcomeCase: OutcomeCase): Promise<void> {
    const user = await RenderInState(outcomeCase.startState);
    QueueUpdateOutcome(outcomeCase.status, outcomeCase.accountScreen);
    await PressFunctionKey(user, outcomeCase.functionKey);
    expect(ShownAlerts()).toEqual(outcomeCase.expectedAlerts);
}

// All-field edit mode (AAP 0.6.5): the first message, every failing field, the first one's cursor.
async function MarksEveryFailingField(): Promise<void> {
    const user = await RenderInState(UpdateScreenState.SHOW_DETAILS);
    const failedScreen = BuildAccountScreen(UpdateScreenState.CHANGES_NOT_OK, {
        message: MSG_CREDIT_LIMIT_NOT_VALID,
        field_errors: ["credit_limit", "fico_credit_score"],
        cursor_field: "credit_limit",
    });
    QueueUpdateOutcome(HTTP_EDIT_FAILURE, failedScreen);
    await PressFunctionKey(user, ENTER_KEY);
    expect(InputOf("credit_limit")).toHaveAttribute("aria-invalid", "true");
    expect(InputOf("fico_credit_score")).toHaveAttribute("aria-invalid", "true");
    expect(InputOf("first_name")).toHaveAttribute("aria-invalid", "false");
    expect(InputOf("credit_limit")).toHaveFocus();
    ExpectAlert(MSG_CREDIT_LIMIT_NOT_VALID, "error");
}

async function ExitsToMainMenu(method: InputMethod): Promise<void> {
    const user = await RenderInState(UpdateScreenState.SHOW_DETAILS);
    await PressFunctionKey(user, EXIT_KEY, method);
    expect(mockRouterPush).toHaveBeenCalledWith(MENU_ROUTE);
    expect(mockUpdateAccount).not.toHaveBeenCalled();
}

// The apiClient interceptor, which the mocked API module bypasses, assigns SIGN_ON_ROUTE on a
// 401; the page itself shows the blank 401 message as nothing and does not route.
function ExpectSignOnLeftToInterceptor(): void {
    expect(ShownAlerts()).toEqual([]);
    expect(mockRouterPush).not.toHaveBeenCalled();
    expect(mockRouterReplace).not.toHaveBeenCalled();
    expect(SIGN_ON_ROUTE).toBe("/signon");
}

async function LeavesFetch401ToInterceptor(): Promise<void> {
    mockSearchParams = new URLSearchParams({ acctId: TEST_ACCOUNT_ID });
    const unauthorized = BuildAxiosError(HTTP_UNAUTHORIZED, BuildErrorBody({}));
    mockFetchAccountForEdit.mockRejectedValueOnce(unauthorized);
    RenderScreen();
    await WaitForIdle();
    expect(mockFetchAccountForEdit).toHaveBeenCalledWith(TEST_ACCOUNT_ID, READ_OPTIONS);
    ExpectSignOnLeftToInterceptor();
}

async function LeavesUpdate401ToInterceptor(): Promise<void> {
    const user = await RenderInState(UpdateScreenState.SHOW_DETAILS);
    const unauthorized = BuildAxiosError(HTTP_UNAUTHORIZED, BuildErrorBody({}));
    mockUpdateAccount.mockRejectedValueOnce(unauthorized);
    await PressFunctionKey(user, ENTER_KEY);
    expect(mockUpdateAccount).toHaveBeenCalledTimes(1);
    ExpectSignOnLeftToInterceptor();
}

// An edit fetch that stays in flight until its signal aborts it, then rejects as axios does.
function PendingUntilAborted(_acctId: string, options?: ReadOptions): Promise<AccountScreen> {
    return new Promise((_resolve, reject) => {
        options?.signal?.addEventListener("abort", () => reject(new CanceledError()));
    });
}

// Holds the next edit fetch in flight; the returned function answers it with a screen.
function HoldNextFetch(): (accountScreen: AccountScreen) => Promise<void> {
    let answer: (accountScreen: AccountScreen) => void = () => undefined;
    mockFetchAccountForEdit.mockImplementationOnce(
        () =>
            new Promise<AccountScreen>((resolve) => {
                answer = resolve;
            }),
    );
    return async (accountScreen) => {
        await act(async () => answer(accountScreen));
    };
}

// The AbortSignal the page passed with its n-th edit fetch (0-based).
function FetchSignalOfCall(callIndex: number): AbortSignal {
    const signal = mockFetchAccountForEdit.mock.calls[callIndex]?.[1]?.signal;
    if (signal === undefined) {
        throw new Error(`FetchAccountForEdit call ${callIndex} carried no AbortSignal`);
    }
    return signal;
}

// Lets an aborted fetch's rejection reach the page before the test inspects the outcome.
async function SettleRejections(): Promise<void> {
    await act(async () => undefined);
}

// Leaving the page aborts the entry fetch in flight; its rejection reaches no screen or log.
async function AbortsEntryFetchOnLeaving(): Promise<void> {
    const consoleErrorSpy = jest.spyOn(console, "error");
    mockSearchParams = new URLSearchParams({ acctId: TEST_ACCOUNT_ID });
    mockFetchAccountForEdit.mockImplementation(PendingUntilAborted);
    const { unmount } = RenderPage();
    await waitFor(() => expect(mockFetchAccountForEdit).toHaveBeenCalledTimes(1));
    expect(FetchSignalOfCall(0).aborted).toBe(false);
    unmount();
    expect(FetchSignalOfCall(0).aborted).toBe(true);
    await SettleRejections();
    expect(mockFetchAccountForEdit).toHaveBeenCalledTimes(1);
    expect(consoleErrorSpy).not.toHaveBeenCalled();
    consoleErrorSpy.mockRestore();
}

// The App Router keeps the page mounted until the menu has loaded, so F3 itself aborts the edit
// fetch in flight before it navigates. The aborted fetch shows nothing and frees the screen.
async function AbortsEntryFetchOnExit(method: InputMethod): Promise<void> {
    const consoleErrorSpy = jest.spyOn(console, "error");
    mockSearchParams = new URLSearchParams({ acctId: TEST_ACCOUNT_ID });
    mockFetchAccountForEdit.mockImplementation(PendingUntilAborted);
    const user = RenderScreen();
    await waitFor(() => expect(mockFetchAccountForEdit).toHaveBeenCalledTimes(1));
    const pushesBeforeAbort: number[] = [];
    FetchSignalOfCall(0).addEventListener("abort", () => {
        pushesBeforeAbort.push(mockRouterPush.mock.calls.length);
    });
    await PressFunctionKey(user, EXIT_KEY, method);
    expect(pushesBeforeAbort).toEqual([0]);
    expect(mockRouterPush.mock.calls).toEqual([[MENU_ROUTE]]);
    expect(ShownAlerts()).toEqual([]);
    expect(mockUpdateAccount).not.toHaveBeenCalled();
    expect(consoleErrorSpy).not.toHaveBeenCalled();
    consoleErrorSpy.mockRestore();
}

// A cancelled edit fetch shows nothing and leaves the key screen free for the next ENTER.
async function IgnoresCancelledEntryFetch(): Promise<void> {
    mockSearchParams = new URLSearchParams({ acctId: TEST_ACCOUNT_ID });
    mockFetchAccountForEdit.mockRejectedValueOnce(new CanceledError());
    const user = RenderScreen();
    await WaitForIdle();
    expect(mockFetchAccountForEdit).toHaveBeenCalledWith(TEST_ACCOUNT_ID, READ_OPTIONS);
    expect(ShownAlerts()).toEqual([]);
    expect(EditableFieldNames()).toEqual(KEY_FIELD_NAMES);
    await user.type(InputOf("acct_id"), TEST_ACCOUNT_ID);
    QueueUpdateOutcome(HTTP_OK, BuildAccountScreen(UpdateScreenState.SHOW_DETAILS));
    await PressFunctionKey(user, ENTER_KEY);
    ExpectUpdateRequest(TEST_ACCOUNT_ID, {
        action: UpdateAction.ENTER,
        screen_token: "",
        fields: { acct_id: TEST_ACCOUNT_ID },
    });
}

// Development: Strict Mode's simulated unmount aborts the first entry fetch and the repeated
// effect fetches again; the aborted fetch shows nothing and leaves the busy state to the second.
async function StrictModeFetchesAgain(): Promise<void> {
    mockSearchParams = new URLSearchParams({ acctId: TEST_ACCOUNT_ID });
    mockFetchAccountForEdit.mockImplementationOnce(PendingUntilAborted);
    const answerFetch = HoldNextFetch();
    RenderPage(true);
    await waitFor(() => expect(mockFetchAccountForEdit).toHaveBeenCalledTimes(2));
    await SettleRejections();
    expect([FetchSignalOfCall(0).aborted, FetchSignalOfCall(1).aborted]).toEqual([true, false]);
    expect(mockFetchAccountForEdit).toHaveBeenNthCalledWith(2, TEST_ACCOUNT_ID, READ_OPTIONS);
    expect(screen.getByRole("progressbar")).toBeInTheDocument();
    expect(ShownAlerts()).toEqual([]);
    await answerFetch(BuildAccountScreen(UpdateScreenState.SHOW_DETAILS));
    await WaitForIdle();
    expect(InputOf("acct_id")).toHaveValue(TEST_ACCOUNT_ID);
    expect(ShownAlerts()).toEqual([[INFO_PROMPT_FOR_CHANGES, "info"]]);
}

// The screen the second Strict Mode fetch shows takes keys: ENTER's PUT carries its screen_token
// and, being a write, no signal.
async function StrictModeScreenTakesKeys(): Promise<void> {
    mockSearchParams = new URLSearchParams({ acctId: TEST_ACCOUNT_ID });
    mockFetchAccountForEdit.mockImplementationOnce(PendingUntilAborted);
    mockFetchAccountForEdit.mockResolvedValueOnce(
        BuildAccountScreen(UpdateScreenState.SHOW_DETAILS),
    );
    const user = userEvent.setup();
    RenderPage(true);
    await waitFor(() => expect(InputOf("acct_id")).toHaveValue(TEST_ACCOUNT_ID));
    await WaitForIdle();
    QueueUpdateOutcome(HTTP_OK, BuildAccountScreen(UpdateScreenState.SHOW_DETAILS));
    await PressFunctionKey(user, ENTER_KEY);
    ExpectUpdateRequest(TEST_ACCOUNT_ID, {
        action: UpdateAction.ENTER,
        screen_token: FIRST_SCREEN_TOKEN,
        fields: PickFields(TEST_FIELDS, DETAIL_FIELD_NAMES),
    });
}

beforeEach(() => {
    mockSearchParams = new URLSearchParams();
    mockFetchAccountForEdit.mockReset();
    mockUpdateAccount.mockReset();
    window.sessionStorage.clear();
});

describe("COACTUPC /accounts/update COACTUP content", () => {
    it("COACTUPC shows the COACTUP title, header and field-group captions", ShowsTitleAndCaptions);
    it("COACTUPC renders one input per AccountScreenFields key", RendersEveryScreenInput);
    it.each(FIELD_CASES)(
        "COACTUP $name is labelled '$label' and holds $maxLength characters",
        LabelsInputWithLength,
    );
});

describe("COACTUPC /accounts/update field protection", () => {
    it.each(EDITABLE_BY_STATE_CASES)(
        "COACTUPC 3300-SETUP-SCREEN-ATTRS in $state opens $description",
        OpensFieldsOfState,
    );
    it("COACTUPC entry from the menu shows the empty key screen", StartsOnEmptyKeyScreen);
    it("COACTUPC entry with ?acctId fetches the account, SSN unmasked", FetchesAccountOfEntryKey);
});

describe("COACTUPC /accounts/update actions", () => {
    it.each(ACTION_KEY_CASES)(
        "COACTUPC $key by $method sends $action with the held screen_token",
        SendsActionWithHeldToken,
    );
    it("COACTUPC ENTER before the fetch sends only ACCTSID", SendsOnlyKeyBeforeFetch);
    it("COACTUPC ENTER with a blank ACCTSID reaches the key edit", SendsBlankKeyToKeyEdit);
    it("COACTUPC sends keyed text unchanged to TEST-NUMVAL-C", SendsKeyedTextUnchanged);
    it("COACTUPC F5 sends the screen_token of the latest response", SendsTokenOfLatestResponse);
    it.each(REQUEST_KEYS)(
        "COACTUPC $key disables ENTER, F5 and F12 but not F3 until its PUT settles",
        DisablesRequestKeysWhileBusy,
    );
    it.each(INPUT_METHODS)("COACTUPC F3 by %s returns to /menu without a PUT", ExitsToMainMenu);
});

describe("COACTUPC /accounts/update outcomes", () => {
    it.each(OUTCOME_CASES)("COACTUPC shows $description", ShowsOutcomeLiterals);
    it.each(ADOPTION_CASES)(
        "COACTUPC adopts the $status body's $adoptedState screen and screen_token",
        AdoptsScreenOfErrorBody,
    );
    it("COACTUPC flags every failing field and focuses the first", MarksEveryFailingField);
    it("COACTUPC leaves an edit-fetch 401 to the sign-on redirect", LeavesFetch401ToInterceptor);
    it("COACTUPC leaves an update 401 to the sign-on redirect", LeavesUpdate401ToInterceptor);
});

describe("COACTUPC /accounts/update message builders", () => {
    it.each(FIELD_EDIT_CASES)(
        "COACTUPC ENTER shows '$message' and flags $fieldErrors",
        ShowsFieldEditFailure,
    );
    it(
        "COACTUPC F5 shows the CUSTDAT lock failure, then sends only ACCTSID",
        ShowsCustomerLockFailure,
    );
    it("COACTUPC shows a plain 500 as the red ABEND-ROUTINE text", ShowsAbendOnServerError);
});

describe("COACTUPC /accounts/update read failures", () => {
    it.each(READ_FAILURE_CASES)(
        "COACTUPC ?acctId= fetch shows the $source text on the key screen",
        ShowsEntryFetchFailure,
    );
    it.each(READ_FAILURE_CASES)(
        "COACTUPC ENTER on the key screen shows the $source text",
        ShowsKeyEnterFailure,
    );
    it.each(READ_FILE_ERROR_CASES)(
        "COACTUPC F12 re-read shows the $source text and keeps SHOW_DETAILS",
        ShowsCancelReadFailure,
    );
});

describe("COACTUPC /accounts/update edit fetch cancellation", () => {
    it("COACTUPC leaving aborts the ?acctId= edit fetch", AbortsEntryFetchOnLeaving);
    it.each(INPUT_METHODS)(
        "COACTUPC F3 by %s aborts the ?acctId= edit fetch before returning to /menu",
        AbortsEntryFetchOnExit,
    );
    it("COACTUPC a cancelled edit fetch shows no message", IgnoresCancelledEntryFetch);
    it(
        "COACTUPC Strict Mode aborts the first edit fetch and shows the second",
        StrictModeFetchesAgain,
    );
    it(
        "COACTUPC Strict Mode's fetched screen sends ENTER with its token",
        StrictModeScreenTakesKeys,
    );
});
