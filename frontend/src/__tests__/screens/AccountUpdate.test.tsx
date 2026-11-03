// Screen tests: BMS COACTUP, program COACTUPC (CAUP), page /accounts/update.
// Sources: bms/COACTUP.bms and cpy-bms/COACTUP.CPY (labels and LENGTHs); cbl/COACTUPC.cbl
// (2000-DECIDE-ACTION, 3250-SETUP-INFOMSG, 3300-SETUP-SCREEN-ATTRS, 3320-UNPROTECT-FEW-ATTRS);
// the literals of backend/app/services/accounts/coactupc.py, copied verbatim.
import { ThemeProvider } from "@mui/material/styles";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import {
    AxiosError,
    AxiosHeaders,
    type AxiosResponse,
    type InternalAxiosRequestConfig,
} from "axios";
import type { ReactNode } from "react";

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

function RenderScreen(): UserEvent {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <AccountUpdatePage />
        </ThemeProvider>,
    );
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
    expect(mockFetchAccountForEdit).toHaveBeenCalledWith(TEST_ACCOUNT_ID);
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
    expect(mockFetchAccountForEdit).toHaveBeenCalledWith(TEST_ACCOUNT_ID);
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
