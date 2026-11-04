// Screen tests: BMS COCRDUP, program COCRDUPC (CCUP), page /cards/update.
// Sources: bms/COCRDUP.bms and cpy-bms/COCRDUP.CPY (labels, LENGTHs, the FKEYS and FKEYSC
// legend); cbl/COCRDUPC.cbl (the AID check and 0000-MAIN dispatch, 3300-SETUP-SCREEN-ATTRS); the
// literals, states and statuses of backend/app/services/cards/cocrdupc.py, copied verbatim. The
// expiry group and the ERRMSG descriptions are covered by CardUpdateFields.test.tsx, and the
// entry fetch's abort and remount lifecycle by CardUpdateCancellation.test.tsx.
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

import CardUpdatePage from "@/app/cards/update/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import type * as CardsApiModule from "@/lib/api/cards";
import {
    FetchCardForEdit,
    UpdateCard,
    type CardScreen,
    type CardScreenFields,
    type CardUpdateRequest,
} from "@/lib/api/cards";
import {
    SIGN_ON_ROUTE,
    UpdateAction,
    UpdateScreenState,
    type LegacyErrorResponse,
    type MessageSeverity,
} from "@/lib/apiClient";
import type { SessionContextValue } from "@/lib/session";

type CardFieldName = keyof CardScreenFields;

// The CCUP error body: the four legacy keys beside the screen the page keeps the state machine on.
type CardErrorBody = LegacyErrorResponse &
    Partial<Pick<CardScreen, "fields" | "state" | "screen_token">>;

// One Alert of the message line: its raw text and its severity.
type ShownAlert = readonly [text: string, severity: string];

type InputMethod = "click" | "key";

type EntryScreen = "card list" | "main menu";

// One COCRDUP input: the label the page renders and the BMS LENGTH.
interface FieldCase {
    name: CardFieldName;
    label: string;
    maxLength: number;
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

interface EditableByStateCase {
    state: UpdateScreenState;
    editableNames: readonly CardFieldName[];
}

interface AdoptionCase {
    status: number;
    message: string;
    adoptedState: UpdateScreenState;
    adoptedFields: CardScreenFields;
}

interface FinishedKeyCase extends UpdateKey {
    state: UpdateScreenState;
}

interface LiteralCase {
    text: string;
    severity: MessageSeverity;
    startState: UpdateScreenState;
    functionKey: UpdateKey;
    status: number;
    cardScreen: CardScreen;
    expectedAlerts: readonly ShownAlert[];
}

interface ExitCase {
    entry: EntryScreen;
    method: InputMethod;
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
// Read by the mocked useSearchParams at render time: the card list's ?cardRef=&acctId=, or none.
let mockSearchParams = new URLSearchParams();
// The signed-on regular user (type U), as CCUP is a main-menu function; it holds no password.
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
    usePathname: () => "/cards/update",
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

jest.mock("@/lib/api/cards", () => ({
    ...jest.requireActual<typeof CardsApiModule>("@/lib/api/cards"),
    ListCards: jest.fn(),
    SelectCards: jest.fn(),
    ViewCard: jest.fn(),
    FetchCardForEdit: jest.fn(),
    UpdateCard: jest.fn(),
}));

const mockFetchCardForEdit = jest.mocked(FetchCardForEdit);
const mockUpdateCard = jest.mocked(UpdateCard);

// Test keys only: the server masks CARDSID, and the one typed card number is synthetic.
const TEST_CARD_REF = "card-ref-1";
const TEST_ACCOUNT_ID = "00000000004";
const PAN_PATTERN = /\d{16}/;
const MASKED_CARD_NUMBER = "*".repeat(12) + "4312";
const TYPED_CARD_NUMBER = "0".repeat(12) + "1234";
const TEST_EMBOSSED_NAME = "TEST CARD HOLDER";
const EDITED_EMBOSSED_NAME = "EDITED CARD HOLDER";
const REFRESHED_EMBOSSED_NAME = "REFRESHED CARD HOLDER";
const FIRST_SCREEN_TOKEN = "screen-token-1";
const NEXT_SCREEN_TOKEN = "screen-token-2";
const ADOPTED_SCREEN_TOKEN = "screen-token-3";
const MENU_ROUTE = "/menu";
const READ_OPTIONS = { signal: expect.any(AbortSignal) };

// The statuses cocrdupc.py raises: edit failures and no change, a changed record, a record lock
// and a failed rewrite.
const HTTP_OK = 200;
const HTTP_EDIT_FAILURE = 400;
const HTTP_UNAUTHORIZED = 401;
const HTTP_RECORD_CHANGED = 409;
const HTTP_RECORD_LOCKED = 423;
const HTTP_FILE_ERROR = 503;

// The COCRDUP INITIAL texts, verbatim.
const SCREEN_TITLE = "Update Credit Card Details";
const TRANSACTION_LINE = "Tran: CCUP";
const PROGRAM_LINE = "Prog: COCRDUPC";

// WS-RETURN-MSG literals of cocrdupc.py, and the key edits it shares with cocrdslc.py.
const MSG_CARD_NAME_ALPHA = "Card name can only contain alphabets and spaces";
const MSG_CARD_STATUS = "Card Active Status must be Y or N";
const MSG_EXPIRY_MONTH = "Card expiry month must be between 1 and 12";
const MSG_EXPIRY_YEAR = "Invalid card expiry year";
const MSG_NO_CHANGES = "No change detected with respect to values fetched.";
const MSG_ACCOUNT_NOT_PROVIDED = "Account number not provided";
const MSG_CARD_NOT_PROVIDED = "Card number not provided";
const MSG_LOCK_FAILED = "Could not lock record for update";
const MSG_RECORD_CHANGED = "Record changed by some one else. Please review";
const MSG_UPDATE_FAILED = "Update of record failed";

// WS-INFO-MSG literals of cocrdupc.py; the key prompt is cocrdslc.PROMPT_FOR_KEYS_INFO.
const INFO_PROMPT_FOR_KEYS = "Please enter Account and Card Number";
const INFO_DETAILS_SHOWN = "Details of selected card shown above";
const INFO_PROMPT_FOR_CHANGES = "Update card details presented above.";
const INFO_PROMPT_FOR_CONFIRMATION = "Changes validated.Press F5 to save";
const INFO_UPDATE_SUCCESS = "Changes committed to database";
const INFO_UPDATE_FAILURE = "Changes unsuccessful. Please try again";

// The 3250-SETUP-INFOMSG line the server sends with each CCUP-CHANGE-ACTION state.
const INFO_BY_STATE: Readonly<Record<UpdateScreenState, string>> = {
    [UpdateScreenState.DETAILS_NOT_FETCHED]: INFO_PROMPT_FOR_KEYS,
    [UpdateScreenState.SHOW_DETAILS]: INFO_DETAILS_SHOWN,
    [UpdateScreenState.CHANGES_NOT_OK]: INFO_PROMPT_FOR_CHANGES,
    [UpdateScreenState.CHANGES_OK_NOT_CONFIRMED]: INFO_PROMPT_FOR_CONFIRMATION,
    [UpdateScreenState.CHANGES_DONE]: INFO_UPDATE_SUCCESS,
    [UpdateScreenState.CHANGES_FAILED]: INFO_UPDATE_FAILURE,
};

// MUI 9 marks each Alert with its colour class (Alert/alertClasses.d.ts).
const SEVERITY_CLASS_NAMES: Readonly<Record<MessageSeverity, string>> = {
    error: "MuiAlert-colorError",
    success: "MuiAlert-colorSuccess",
    info: "MuiAlert-colorInfo",
};
const SEVERITIES: readonly MessageSeverity[] = ["error", "success", "info"];

// ACCTSID, CARDSID, CRDNAME and CRDSTCD with their DFHMDF INITIAL labels, and EXPMON and EXPYEAR
// under the Expiry Date label.
const FIELD_CASES: readonly FieldCase[] = [
    { name: "acct_id", label: "Account Number    :", maxLength: 11 },
    { name: "card_num", label: "Card Number       :", maxLength: 16 },
    { name: "embossed_name", label: "Name on card      :", maxLength: 50 },
    { name: "active_status", label: "Card Active Y/N   :", maxLength: 1 },
    { name: "expiry_month", label: "Month", maxLength: 2 },
    { name: "expiry_year", label: "Year", maxLength: 4 },
];

const KEY_FIELD_NAMES: readonly CardFieldName[] = ["acct_id", "card_num"];
const DETAIL_FIELD_NAMES: readonly CardFieldName[] = [
    "embossed_name",
    "active_status",
    "expiry_month",
    "expiry_year",
];
const NO_FIELD_NAMES: readonly CardFieldName[] = [];

// 3300-SETUP-SCREEN-ATTRS: the keys before the fetch and after a failed change (WHEN OTHER), the
// details while they are shown or in error, and nothing while a change awaits F5 or is done.
const EDITABLE_NAMES_BY_STATE: Readonly<Record<UpdateScreenState, readonly CardFieldName[]>> = {
    [UpdateScreenState.DETAILS_NOT_FETCHED]: KEY_FIELD_NAMES,
    [UpdateScreenState.SHOW_DETAILS]: DETAIL_FIELD_NAMES,
    [UpdateScreenState.CHANGES_NOT_OK]: DETAIL_FIELD_NAMES,
    [UpdateScreenState.CHANGES_OK_NOT_CONFIRMED]: NO_FIELD_NAMES,
    [UpdateScreenState.CHANGES_DONE]: NO_FIELD_NAMES,
    [UpdateScreenState.CHANGES_FAILED]: KEY_FIELD_NAMES,
};

const EDITABLE_BY_STATE_CASES: readonly EditableByStateCase[] = Object.values(
    UpdateScreenState,
).map((state) => ({ state, editableNames: EDITABLE_NAMES_BY_STATE[state] }));

// The fetched card: the number masked, and the dark EXPDAY never shown.
const TEST_FIELDS: Readonly<CardScreenFields> = {
    acct_id: TEST_ACCOUNT_ID,
    card_num: MASKED_CARD_NUMBER,
    embossed_name: TEST_EMBOSSED_NAME,
    active_status: "Y",
    expiry_month: "07",
    expiry_year: "2027",
    expiry_day: "",
};

const BLANK_FIELDS: Readonly<CardScreenFields> = {
    acct_id: "",
    card_num: "",
    embossed_name: "",
    active_status: "",
    expiry_month: "",
    expiry_year: "",
    expiry_day: "",
};

// The record 9300-CHECK-CHANGE-IN-REC found changed since the fetch.
const REFRESHED_FIELDS: Readonly<CardScreenFields> = {
    ...TEST_FIELDS,
    embossed_name: REFRESHED_EMBOSSED_NAME,
    active_status: "N",
};

// The FKEYS legend "ENTER=Process F3=Exit" and the FKEYSC legend "F5=Save F12=Cancel".
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
const LEGEND_BUTTON_NAMES: readonly string[] = [
    ENTER_KEY.buttonName,
    EXIT_KEY.buttonName,
    SAVE_KEY.buttonName,
    CANCEL_KEY.buttonName,
];

const REQUEST_KEYS: readonly UpdateKey[] = [ENTER_KEY, SAVE_KEY, CANCEL_KEY];
const INPUT_METHODS: readonly InputMethod[] = ["click", "key"];
const ACTION_KEY_CASES: readonly ActionKeyCase[] = REQUEST_KEYS.flatMap((updateKey) =>
    INPUT_METHODS.map((method) => ({ ...updateKey, method })),
);
const EXIT_CASES: readonly ExitCase[] = (["card list", "main menu"] as const).flatMap((entry) =>
    INPUT_METHODS.map((method) => ({ entry, method })),
);

// Every timer API but the interval pair stays real, so userEvent and the Testing Library waits
// keep their real setTimeout while the ScreenShell clock never ticks outside act on a slow run.
const CLOCK_ONLY_FAKE_TIMERS: Parameters<typeof jest.useFakeTimers>[0] = {
    doNotFake: [
        "Date",
        "hrtime",
        "nextTick",
        "performance",
        "queueMicrotask",
        "requestAnimationFrame",
        "cancelAnimationFrame",
        "requestIdleCallback",
        "cancelIdleCallback",
        "setImmediate",
        "clearImmediate",
        "setTimeout",
        "clearTimeout",
    ],
};

function BuildCardScreen(
    state: UpdateScreenState,
    overrides: Partial<CardScreen> = {},
): CardScreen {
    return {
        fields: { ...TEST_FIELDS },
        state,
        screen_token: FIRST_SCREEN_TOKEN,
        card_ref: TEST_CARD_REF,
        message: "",
        info_message: INFO_BY_STATE[state],
        field_errors: [],
        cursor_field: "",
        ...overrides,
    };
}

// ResetScreen after a done or failed change: the empty key screen, no card held.
function EmptyKeyScreen(): CardScreen {
    return BuildCardScreen(UpdateScreenState.DETAILS_NOT_FETCHED, {
        fields: { ...BLANK_FIELDS },
        screen_token: NEXT_SCREEN_TOKEN,
        card_ref: "",
        cursor_field: "acct_id",
    });
}

function PickFields(
    fields: Readonly<CardScreenFields>,
    names: readonly CardFieldName[],
): Partial<CardScreenFields> {
    const picked: Partial<CardScreenFields> = {};
    for (const name of names) {
        picked[name] = fields[name];
    }
    return picked;
}

function BuildErrorBody(overrides: Partial<CardErrorBody>): CardErrorBody {
    return { message: "", info_message: "", field_errors: [], cursor_field: "", ...overrides };
}

// cocrdupc.ScreenError's body: the four legacy keys beside fields, state and screen_token. It
// carries no card_ref, so the page keeps the card_ref it holds.
function ErrorBodyOf(cardScreen: CardScreen): CardErrorBody {
    return BuildErrorBody({
        message: cardScreen.message,
        info_message: cardScreen.info_message,
        field_errors: cardScreen.field_errors,
        cursor_field: cardScreen.cursor_field,
        fields: cardScreen.fields,
        state: cardScreen.state,
        screen_token: cardScreen.screen_token,
    });
}

function BuildAxiosError(status: number, body: CardErrorBody): AxiosError<CardErrorBody> {
    const config: InternalAxiosRequestConfig = { headers: new AxiosHeaders() };
    const response: AxiosResponse<CardErrorBody> = {
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
function QueueUpdateOutcome(status: number, cardScreen: CardScreen): void {
    if (status === HTTP_OK) {
        mockUpdateCard.mockResolvedValueOnce(cardScreen);
        return;
    }
    mockUpdateCard.mockRejectedValueOnce(BuildAxiosError(status, ErrorBodyOf(cardScreen)));
}

function RenderScreen(): UserEvent {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <CardUpdatePage />
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

// A COCRDLIC U selection: the card_ref token and the row's account, fetched at once into state.
async function RenderFromList(state: UpdateScreenState): Promise<UserEvent> {
    mockSearchParams = new URLSearchParams({ cardRef: TEST_CARD_REF, acctId: TEST_ACCOUNT_ID });
    mockFetchCardForEdit.mockResolvedValueOnce(BuildCardScreen(state));
    const user = RenderScreen();
    await WaitForIdle();
    return user;
}

// Main-menu option 5: no query, so the empty key screen in DETAILS_NOT_FETCHED and no request.
function RenderKeyScreen(): UserEvent {
    mockSearchParams = new URLSearchParams();
    return RenderScreen();
}

async function RenderInState(state: UpdateScreenState): Promise<UserEvent> {
    if (state === UpdateScreenState.DETAILS_NOT_FETCHED) {
        return RenderKeyScreen();
    }
    return RenderFromList(state);
}

async function RenderFromEntry(entry: EntryScreen): Promise<UserEvent> {
    if (entry === "main menu") {
        return RenderKeyScreen();
    }
    return RenderFromList(UpdateScreenState.SHOW_DETAILS);
}

// The identity normalizer: the default one collapses the blanks the COCRDUP labels keep.
function KeepRawText(text: string): string {
    return text;
}

function FieldCaseOf(name: CardFieldName): FieldCase {
    const fieldCase = FIELD_CASES.find((candidate) => candidate.name === name);
    if (fieldCase === undefined) {
        throw new Error(`No COCRDUP field case for ${name}`);
    }
    return fieldCase;
}

function InputOf(name: CardFieldName): HTMLInputElement {
    return screen.getByLabelText<HTMLInputElement>(FieldCaseOf(name).label, {
        normalizer: KeepRawText,
    });
}

// A protected BMS field renders read-only: still focusable, but it cannot be changed.
function ExpectEditable(name: CardFieldName, isEditable: boolean): void {
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

async function TypeKeys(user: UserEvent, acctText: string, cardText: string): Promise<void> {
    await user.type(InputOf("acct_id"), acctText);
    await user.type(InputOf("card_num"), cardText);
}

function LastUpdateCall(): Parameters<typeof UpdateCard> {
    const lastCall = mockUpdateCard.mock.lastCall;
    if (lastCall === undefined) {
        throw new Error("UpdateCard was not called");
    }
    return lastCall;
}

function ExpectUpdateRequest(cardKey: string, request: CardUpdateRequest): void {
    expect(LastUpdateCall()).toStrictEqual([cardKey, request]);
}

// ResetScreen's DETAILS_NOT_FETCHED screen: every field blank, only the keys typable, the key
// prompt alone on the message line.
function ExpectEmptyKeyScreen(): void {
    for (const fieldCase of FIELD_CASES) {
        expect(InputOf(fieldCase.name)).toHaveValue("");
        ExpectEditable(fieldCase.name, KEY_FIELD_NAMES.includes(fieldCase.name));
    }
    expect(ShownAlerts()).toEqual([[INFO_PROMPT_FOR_KEYS, "info"]]);
}

// A card number may reach only the API path: never a route, nor the page's own URL.
function ExpectNoCardNumberInUrls(): void {
    const routeArguments = [...mockRouterPush.mock.calls, ...mockRouterReplace.mock.calls].flat();
    const panArguments = routeArguments.filter((argument) => PAN_PATTERN.test(String(argument)));
    expect(panArguments).toEqual([]);
    expect(window.location.href).not.toMatch(PAN_PATTERN);
}

// The apiClient interceptor, which the mocked API module bypasses, assigns SIGN_ON_ROUTE on a
// 401; the page shows the blank 401 message as nothing and does not route.
function ExpectSignOnLeftToInterceptor(): void {
    expect(ShownAlerts()).toEqual([]);
    expect(mockRouterPush).not.toHaveBeenCalled();
    expect(mockRouterReplace).not.toHaveBeenCalled();
    expect(SIGN_ON_ROUTE).toBe("/signon");
}

// Exactly the named fields accept input; every other one is protected.
function ExpectOnlyEditable(editableNames: readonly CardFieldName[]): void {
    for (const fieldCase of FIELD_CASES) {
        ExpectEditable(fieldCase.name, editableNames.includes(fieldCase.name));
    }
}

// A failed 1230-1260 detail edit: CHANGES_NOT_OK, its message and the field it flags.
function DetailEditCase(text: string, fieldName: CardFieldName): LiteralCase {
    return {
        text,
        severity: "error",
        startState: UpdateScreenState.SHOW_DETAILS,
        functionKey: ENTER_KEY,
        status: HTTP_EDIT_FAILURE,
        cardScreen: BuildCardScreen(UpdateScreenState.CHANGES_NOT_OK, {
            message: text,
            field_errors: [fieldName],
            cursor_field: fieldName,
        }),
        expectedAlerts: [
            [text, "error"],
            [INFO_PROMPT_FOR_CHANGES, "info"],
        ],
    };
}

// The key screen a failed 1210 or 1220 key edit keeps: no card held, the key flagged.
function KeyErrorScreen(message: string, fieldName: CardFieldName): CardScreen {
    return BuildCardScreen(UpdateScreenState.DETAILS_NOT_FETCHED, {
        fields: { ...BLANK_FIELDS },
        screen_token: "",
        card_ref: "",
        message,
        field_errors: [fieldName],
        cursor_field: fieldName,
    });
}

function KeyEditCase(text: string, fieldName: CardFieldName): LiteralCase {
    return {
        text,
        severity: "error",
        startState: UpdateScreenState.DETAILS_NOT_FETCHED,
        functionKey: ENTER_KEY,
        status: HTTP_EDIT_FAILURE,
        cardScreen: KeyErrorScreen(text, fieldName),
        expectedAlerts: [
            [text, "error"],
            [INFO_PROMPT_FOR_KEYS, "info"],
        ],
    };
}

// One key and the message line it leaves, at the status cocrdupc.py answers it with. A completed
// save arrives as the information line of a 200 with ERRMSG blank, and shows as the confirmation.
const LITERAL_CASES: readonly LiteralCase[] = [
    DetailEditCase(MSG_CARD_NAME_ALPHA, "embossed_name"),
    DetailEditCase(MSG_CARD_STATUS, "active_status"),
    DetailEditCase(MSG_EXPIRY_MONTH, "expiry_month"),
    DetailEditCase(MSG_EXPIRY_YEAR, "expiry_year"),
    {
        text: MSG_NO_CHANGES,
        severity: "error",
        startState: UpdateScreenState.SHOW_DETAILS,
        functionKey: ENTER_KEY,
        status: HTTP_EDIT_FAILURE,
        cardScreen: BuildCardScreen(UpdateScreenState.SHOW_DETAILS, {
            message: MSG_NO_CHANGES,
            cursor_field: "embossed_name",
        }),
        expectedAlerts: [
            [MSG_NO_CHANGES, "error"],
            [INFO_DETAILS_SHOWN, "info"],
        ],
    },
    KeyEditCase(MSG_ACCOUNT_NOT_PROVIDED, "acct_id"),
    KeyEditCase(MSG_CARD_NOT_PROVIDED, "card_num"),
    {
        text: INFO_PROMPT_FOR_CONFIRMATION,
        severity: "info",
        startState: UpdateScreenState.SHOW_DETAILS,
        functionKey: ENTER_KEY,
        status: HTTP_OK,
        cardScreen: BuildCardScreen(UpdateScreenState.CHANGES_OK_NOT_CONFIRMED, {
            cursor_field: "acct_id",
        }),
        expectedAlerts: [[INFO_PROMPT_FOR_CONFIRMATION, "info"]],
    },
    {
        text: INFO_UPDATE_SUCCESS,
        severity: "success",
        startState: UpdateScreenState.CHANGES_OK_NOT_CONFIRMED,
        functionKey: SAVE_KEY,
        status: HTTP_OK,
        cardScreen: BuildCardScreen(UpdateScreenState.CHANGES_DONE, { cursor_field: "acct_id" }),
        expectedAlerts: [[INFO_UPDATE_SUCCESS, "success"]],
    },
];

// The refused F5 outcomes of 9200-WRITE-PROCESSING, each carrying the screen the page adopts: a
// changed record shows the fresh record again, a lock or rewrite failure is CHANGES_FAILED.
const ADOPTION_CASES: readonly AdoptionCase[] = [
    {
        status: HTTP_RECORD_CHANGED,
        message: MSG_RECORD_CHANGED,
        adoptedState: UpdateScreenState.SHOW_DETAILS,
        adoptedFields: REFRESHED_FIELDS,
    },
    {
        status: HTTP_RECORD_LOCKED,
        message: MSG_LOCK_FAILED,
        adoptedState: UpdateScreenState.CHANGES_FAILED,
        adoptedFields: TEST_FIELDS,
    },
    {
        status: HTTP_FILE_ERROR,
        message: MSG_UPDATE_FAILED,
        adoptedState: UpdateScreenState.CHANGES_FAILED,
        adoptedFields: TEST_FIELDS,
    },
];

const FINISHED_KEY_CASES: readonly FinishedKeyCase[] = [
    UpdateScreenState.CHANGES_DONE,
    UpdateScreenState.CHANGES_FAILED,
].flatMap((state) => REQUEST_KEYS.map((updateKey) => ({ ...updateKey, state })));

// Two detail edits failing at once (all-field mode): the first message, both fields flagged.
const MULTIPLE_ERROR_NAMES: readonly CardFieldName[] = ["embossed_name", "expiry_year"];
const MULTIPLE_ERROR_SCREEN: CardScreen = BuildCardScreen(UpdateScreenState.CHANGES_NOT_OK, {
    message: MSG_CARD_NAME_ALPHA,
    field_errors: [...MULTIPLE_ERROR_NAMES],
    cursor_field: "embossed_name",
    fields: { ...TEST_FIELDS, embossed_name: "TEST CARD 2", expiry_year: "1949" },
});

function ShowsHeaderAndLegend(): void {
    RenderKeyScreen();
    expect(screen.getByRole("heading", { level: 1, name: SCREEN_TITLE })).toBeInTheDocument();
    expect(screen.getByText(TRANSACTION_LINE)).toBeInTheDocument();
    expect(screen.getByText(PROGRAM_LINE)).toBeInTheDocument();
    const legend = screen.getByRole("group", { name: "Function keys" });
    const buttonNames = within(legend)
        .getAllByRole("button")
        .map((button) => button.textContent);
    expect(buttonNames).toEqual(LEGEND_BUTTON_NAMES);
}

async function FetchesSelectedCard(): Promise<void> {
    await RenderFromList(UpdateScreenState.SHOW_DETAILS);
    expect(mockFetchCardForEdit.mock.calls).toEqual([
        [TEST_CARD_REF, TEST_ACCOUNT_ID, READ_OPTIONS],
    ]);
    expect(InputOf("acct_id")).toHaveValue(TEST_ACCOUNT_ID);
    expect(InputOf("card_num")).toHaveValue(MASKED_CARD_NUMBER);
    expect(InputOf("embossed_name")).toHaveValue(TEST_EMBOSSED_NAME);
    expect(ShownAlerts()).toEqual([[INFO_DETAILS_SHOWN, "info"]]);
    expect(mockUpdateCard).not.toHaveBeenCalled();
}

// The card_ref path segment, the held screen_token, and only the four detail inputs.
async function SendsHeldToken(actionKeyCase: ActionKeyCase): Promise<void> {
    const user = await RenderFromList(UpdateScreenState.SHOW_DETAILS);
    await PressFunctionKey(user, actionKeyCase, actionKeyCase.method);
    expect(mockUpdateCard).toHaveBeenCalledTimes(1);
    ExpectUpdateRequest(TEST_CARD_REF, {
        action: actionKeyCase.action,
        screen_token: FIRST_SCREEN_TOKEN,
        fields: PickFields(TEST_FIELDS, DETAIL_FIELD_NAMES),
    });
}

// Detail inputs travel exactly as keyed; a protected key takes no typing and never travels.
async function SendsDetailsAsKeyed(): Promise<void> {
    const user = await RenderFromList(UpdateScreenState.SHOW_DETAILS);
    await user.type(InputOf("acct_id"), "9");
    expect(InputOf("acct_id")).toHaveValue(TEST_ACCOUNT_ID);
    await user.clear(InputOf("embossed_name"));
    await user.type(InputOf("embossed_name"), EDITED_EMBOSSED_NAME);
    await user.clear(InputOf("expiry_year"));
    await user.type(InputOf("expiry_year"), "2030");
    await PressFunctionKey(user, ENTER_KEY);
    ExpectUpdateRequest(TEST_CARD_REF, {
        action: UpdateAction.ENTER,
        screen_token: FIRST_SCREEN_TOKEN,
        fields: {
            ...PickFields(TEST_FIELDS, DETAIL_FIELD_NAMES),
            embossed_name: EDITED_EMBOSSED_NAME,
            expiry_year: "2030",
        },
    });
}

// Before a fetch the path is CARDSID as typed; once the server holds the card, its card_ref.
async function KeysByTypedCardThenCardRef(): Promise<void> {
    const user = RenderKeyScreen();
    await TypeKeys(user, TEST_ACCOUNT_ID, TYPED_CARD_NUMBER);
    await PressFunctionKey(user, ENTER_KEY);
    ExpectUpdateRequest(TYPED_CARD_NUMBER, {
        action: UpdateAction.ENTER,
        screen_token: "",
        fields: { acct_id: TEST_ACCOUNT_ID, card_num: TYPED_CARD_NUMBER },
    });
    await PressFunctionKey(user, ENTER_KEY);
    ExpectUpdateRequest(TEST_CARD_REF, {
        action: UpdateAction.ENTER,
        screen_token: FIRST_SCREEN_TOKEN,
        fields: PickFields(TEST_FIELDS, DETAIL_FIELD_NAMES),
    });
}

// ENTER moves to CHANGES_OK_NOT_CONFIRMED under a new token, which the F5 that follows sends.
async function SendsEachResponsesToken(): Promise<void> {
    const user = await RenderFromList(UpdateScreenState.SHOW_DETAILS);
    const confirmScreen = BuildCardScreen(UpdateScreenState.CHANGES_OK_NOT_CONFIRMED, {
        screen_token: NEXT_SCREEN_TOKEN,
    });
    QueueUpdateOutcome(HTTP_OK, confirmScreen);
    await PressFunctionKey(user, ENTER_KEY);
    QueueUpdateOutcome(HTTP_OK, BuildCardScreen(UpdateScreenState.CHANGES_DONE));
    await PressFunctionKey(user, SAVE_KEY);
    const sentTokens = mockUpdateCard.mock.calls.map(([, request]) => request.screen_token);
    expect(sentTokens).toEqual([FIRST_SCREEN_TOKEN, NEXT_SCREEN_TOKEN]);
    ExpectUpdateRequest(TEST_CARD_REF, {
        action: UpdateAction.SAVE,
        screen_token: NEXT_SCREEN_TOKEN,
        fields: {},
    });
}

function QueueRefusedSave(adoptionCase: AdoptionCase): void {
    const refusal = BuildCardScreen(adoptionCase.adoptedState, {
        message: adoptionCase.message,
        fields: { ...adoptionCase.adoptedFields },
        screen_token: ADOPTED_SCREEN_TOKEN,
    });
    QueueUpdateOutcome(adoptionCase.status, refusal);
}

// The literal in red over the adopted state's information line, the adopted fields shown and
// protected as that state protects them.
function ExpectAdoptedScreen({ message, adoptedState, adoptedFields }: AdoptionCase): void {
    expect(ShownAlerts()).toEqual([
        [message, "error"],
        [INFO_BY_STATE[adoptedState], "info"],
    ]);
    expect(InputOf("embossed_name")).toHaveValue(adoptedFields.embossed_name);
    ExpectOnlyEditable(EDITABLE_NAMES_BY_STATE[adoptedState]);
}

// A refused F5 is adopted, and the next ENTER is sent under the adopted token.
async function AdoptsRefusedSave(adoptionCase: AdoptionCase): Promise<void> {
    const { adoptedState, adoptedFields } = adoptionCase;
    const user = await RenderFromList(UpdateScreenState.CHANGES_OK_NOT_CONFIRMED);
    QueueRefusedSave(adoptionCase);
    await PressFunctionKey(user, SAVE_KEY);
    ExpectAdoptedScreen(adoptionCase);
    await PressFunctionKey(user, ENTER_KEY);
    ExpectUpdateRequest(TEST_CARD_REF, {
        action: UpdateAction.ENTER,
        screen_token: ADOPTED_SCREEN_TOKEN,
        fields: PickFields(adoptedFields, EDITABLE_NAMES_BY_STATE[adoptedState]),
    });
}

// A key after a done or failed change still reaches the server, which discards the input and
// answers with the empty key screen; the page stays on /cards/update.
async function ResetsAfterFinishedChange(finishedCase: FinishedKeyCase): Promise<void> {
    const user = await RenderFromList(finishedCase.state);
    QueueUpdateOutcome(HTTP_OK, EmptyKeyScreen());
    await PressFunctionKey(user, finishedCase);
    ExpectUpdateRequest(TEST_CARD_REF, {
        action: finishedCase.action,
        screen_token: FIRST_SCREEN_TOKEN,
        fields: PickFields(TEST_FIELDS, EDITABLE_NAMES_BY_STATE[finishedCase.state]),
    });
    ExpectEmptyKeyScreen();
    expect(mockRouterPush).not.toHaveBeenCalled();
    expect(mockRouterReplace).not.toHaveBeenCalled();
}

// The reset screen holds no card_ref, so the next ENTER is keyed by the card number typed.
async function KeysTypedCardAfterReset(): Promise<void> {
    const user = await RenderFromList(UpdateScreenState.CHANGES_DONE);
    QueueUpdateOutcome(HTTP_OK, EmptyKeyScreen());
    await PressFunctionKey(user, ENTER_KEY);
    await TypeKeys(user, TEST_ACCOUNT_ID, TYPED_CARD_NUMBER);
    await PressFunctionKey(user, ENTER_KEY);
    ExpectUpdateRequest(TYPED_CARD_NUMBER, {
        action: UpdateAction.ENTER,
        screen_token: NEXT_SCREEN_TOKEN,
        fields: { acct_id: TEST_ACCOUNT_ID, card_num: TYPED_CARD_NUMBER },
    });
}

async function ShowsLiteral(literalCase: LiteralCase): Promise<void> {
    const user = await RenderInState(literalCase.startState);
    QueueUpdateOutcome(literalCase.status, literalCase.cardScreen);
    await PressFunctionKey(user, literalCase.functionKey);
    expect(mockUpdateCard).toHaveBeenCalledTimes(1);
    expect(ShownAlerts()).toEqual(literalCase.expectedAlerts);
}

// Every field the server names is in error; the cursor goes to cursor_field.
async function MarksEveryFailingField(): Promise<void> {
    const user = await RenderFromList(UpdateScreenState.SHOW_DETAILS);
    QueueUpdateOutcome(HTTP_EDIT_FAILURE, MULTIPLE_ERROR_SCREEN);
    await PressFunctionKey(user, ENTER_KEY);
    for (const fieldCase of FIELD_CASES) {
        const isInvalid = MULTIPLE_ERROR_NAMES.includes(fieldCase.name);
        expect(InputOf(fieldCase.name)).toHaveAttribute("aria-invalid", String(isInvalid));
    }
    expect(InputOf("embossed_name")).toHaveFocus();
    expect(InputOf("expiry_year")).toHaveValue("1949");
    ExpectAlert(MSG_CARD_NAME_ALPHA, "error");
    expect(screen.queryByText(MSG_EXPIRY_YEAR)).not.toBeInTheDocument();
}

async function ExitsToMainMenu(exitCase: ExitCase): Promise<void> {
    const user = await RenderFromEntry(exitCase.entry);
    await PressFunctionKey(user, EXIT_KEY, exitCase.method);
    expect(mockRouterPush.mock.calls).toEqual([[MENU_ROUTE]]);
    expect(mockUpdateCard).not.toHaveBeenCalled();
}

// A typed card number reaches only the API path; the screen shows it masked, and no route the
// page takes carries it.
async function KeepsCardNumberOutOfUrls(): Promise<void> {
    const user = RenderKeyScreen();
    await TypeKeys(user, TEST_ACCOUNT_ID, TYPED_CARD_NUMBER);
    await PressFunctionKey(user, ENTER_KEY);
    expect(LastUpdateCall()[0]).toBe(TYPED_CARD_NUMBER);
    expect(InputOf("card_num")).toHaveValue(MASKED_CARD_NUMBER);
    await PressFunctionKey(user, EXIT_KEY);
    expect(mockRouterPush).toHaveBeenCalledWith(MENU_ROUTE);
    ExpectNoCardNumberInUrls();
}

async function LeavesFetch401ToInterceptor(): Promise<void> {
    mockSearchParams = new URLSearchParams({ cardRef: TEST_CARD_REF, acctId: TEST_ACCOUNT_ID });
    const unauthorized = BuildAxiosError(HTTP_UNAUTHORIZED, BuildErrorBody({}));
    mockFetchCardForEdit.mockRejectedValueOnce(unauthorized);
    RenderScreen();
    await WaitForIdle();
    expect(mockFetchCardForEdit).toHaveBeenCalledTimes(1);
    ExpectSignOnLeftToInterceptor();
}

async function LeavesUpdate401ToInterceptor(): Promise<void> {
    const user = await RenderFromList(UpdateScreenState.SHOW_DETAILS);
    const unauthorized = BuildAxiosError(HTTP_UNAUTHORIZED, BuildErrorBody({}));
    mockUpdateCard.mockRejectedValueOnce(unauthorized);
    await PressFunctionKey(user, ENTER_KEY);
    expect(mockUpdateCard).toHaveBeenCalledTimes(1);
    ExpectSignOnLeftToInterceptor();
}

// clearMocks keeps queued answers, so each case starts from empty fetch and write queues; an
// unqueued write answers with the fetched card, and no stored message is left behind.
beforeEach(() => {
    jest.useFakeTimers(CLOCK_ONLY_FAKE_TIMERS);
    mockSearchParams = new URLSearchParams();
    mockFetchCardForEdit.mockReset();
    mockUpdateCard.mockReset();
    mockUpdateCard.mockResolvedValue(BuildCardScreen(UpdateScreenState.SHOW_DETAILS));
    window.sessionStorage.clear();
});

afterEach(() => {
    jest.useRealTimers();
});

describe("COCRDUPC BMS COCRDUP content on /cards/update", () => {
    it("COCRDUPC shows the title, TRNNAME CCUP, PGMNAME COCRDUPC and the PF-key legend", () => {
        ShowsHeaderAndLegend();
    });

    it.each(FIELD_CASES)(
        "COCRDUPC renders $label with the BMS LENGTH $maxLength",
        ({ name, maxLength }) => {
            RenderKeyScreen();
            expect(InputOf(name)).toHaveAttribute("maxlength", String(maxLength));
        },
    );
});

describe("COCRDUPC entry from a COCRDLIC selection on /cards/update", () => {
    it("COCRDUPC fetches the selected card once by its card_ref and the list's account", () =>
        FetchesSelectedCard());
});

describe("COCRDUPC 3300-SETUP-SCREEN-ATTRS field protection on /cards/update", () => {
    it.each(EDITABLE_BY_STATE_CASES)(
        "COCRDUPC $state leaves exactly $editableNames editable",
        async ({ state, editableNames }) => {
            await RenderInState(state);
            ExpectOnlyEditable(editableNames);
        },
    );
});

describe("COCRDUPC ENTER, F5 and F12 requests on /cards/update", () => {
    it.each(ACTION_KEY_CASES)(
        "COCRDUPC $buttonName by $method sends $action with the held screen_token",
        (actionKeyCase) => SendsHeldToken(actionKeyCase),
    );

    it("COCRDUPC sends the detail inputs exactly as keyed and never a protected key", () =>
        SendsDetailsAsKeyed());

    it("COCRDUPC keys a request by the typed card number, then by the returned card_ref", () =>
        KeysByTypedCardThenCardRef());

    it("COCRDUPC sends the screen_token of the latest response with the next key", () =>
        SendsEachResponsesToken());
});

describe("COCRDUPC refused F5 outcomes of 9200-WRITE-PROCESSING on /cards/update", () => {
    it.each(ADOPTION_CASES)(
        "COCRDUPC $status $message adopts the body's screen and its screen_token",
        (adoptionCase) => AdoptsRefusedSave(adoptionCase),
    );
});

// COCRDUPC lines 435-440 would XCTL a session the card list opened back to COCRDLIC after a done
// or failed change. The CCUP state machine (cocrdupc.ResetScreen) instead answers every key with
// the empty key screen, whichever screen opened this one, so these cases expect the request and
// no navigation; F3 alone leaves.
describe("COCRDUPC keys after a done or failed change on /cards/update", () => {
    it.each(FINISHED_KEY_CASES)(
        "COCRDUPC $state $buttonName shows the empty key screen and stays on the page",
        (finishedCase) => ResetsAfterFinishedChange(finishedCase),
    );

    it("COCRDUPC drops the card_ref with the reset and keys the next ENTER by the typed card", () =>
        KeysTypedCardAfterReset());
});

describe("COCRDUPC message-line literals on /cards/update", () => {
    it.each(LITERAL_CASES)("COCRDUPC $status shows $text as $severity", (literalCase) =>
        ShowsLiteral(literalCase),
    );

    it("COCRDUPC flags every failing detail field and puts the cursor on cursor_field", () =>
        MarksEveryFailingField());
});

// F3 returns to the main menu whichever screen opened the page, as the page and
// cocrdupc.ResetScreen define PF3.
describe("COCRDUPC F3 exit on /cards/update", () => {
    it.each(EXIT_CASES)(
        "COCRDUPC F3 by $method from the $entry entry routes to /menu",
        (exitCase) => ExitsToMainMenu(exitCase),
    );
});

describe("COCRDUPC card numbers in URLs on /cards/update", () => {
    it("COCRDUPC keeps a typed card number out of every route the page takes", () =>
        KeepsCardNumberOutOfUrls());
});

describe("COCRDUPC session refusal on /cards/update", () => {
    it("COCRDUPC a 401 on the entry fetch shows nothing; the interceptor owns /signon", () =>
        LeavesFetch401ToInterceptor());

    it("COCRDUPC a 401 on ENTER shows nothing; the interceptor owns /signon", () =>
        LeavesUpdate401ToInterceptor());
});
