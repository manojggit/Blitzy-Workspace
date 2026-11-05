// Screen tests: BMS COCRDSL, program COCRDSLC (CCDL), page /cards/view.
// Labels and lengths come from bms/COCRDSL.bms; every literal is copied verbatim from
// backend/app/services/cards/cocrdslc.py, which ports cbl/COCRDSLC.cbl lines 639-760.
import { ThemeProvider } from "@mui/material/styles";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
    AxiosError,
    AxiosHeaders,
    CanceledError,
    type AxiosResponse,
    type InternalAxiosRequestConfig,
} from "axios";
import type { ReactElement, ReactNode } from "react";

import CardViewPage from "@/app/cards/view/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import { ViewCard, ViewSelectedCard, type CardDetailResponse } from "@/lib/api/cards";
import {
    ABEND_MESSAGE,
    SIGN_ON_ROUTE,
    StorePendingMessage,
    type LegacyErrorResponse,
    type MessageSeverity,
    type ReadOptions,
} from "@/lib/apiClient";
import type { SessionContextValue } from "@/lib/session";

type SignedOnUser = NonNullable<SessionContextValue["currentUser"]>;
type ScreenUser = ReturnType<typeof userEvent.setup>;
type ScreenAction = (user: ScreenUser) => Promise<void>;

interface FieldErrorCase {
    body: LegacyErrorResponse;
    cursorLabel: string;
}

// A card list selection's read refused for its keys: the answer, the literal and marked keys.
interface KeyRefusalCase {
    status: number;
    body: object;
    message: string;
    fieldErrors: readonly string[];
}

// Stable router and query doubles; the "mock" prefix lets the hoisted factories read them.
const mockRouterPush = jest.fn();
const mockRouterReplace = jest.fn();
const mockRouter = {
    push: mockRouterPush,
    replace: mockRouterReplace,
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};
let mockSearchParams = new URLSearchParams();
// A regular user (type U), as CCDL is a main-menu function; the session holds no password. A
// literal constant, so UPPER_CASE; its MOCK prefix still lets the hoisted factory read it.
const MOCK_CURRENT_USER: SignedOnUser = {
    user_id: "TESTUSR1",
    user_type: "U",
    first_name: "Test",
    last_name: "User",
    next_route: "/menu",
};

jest.mock("next/navigation", () => ({
    useRouter: () => mockRouter,
    useSearchParams: () => mockSearchParams,
    usePathname: () => "/cards/view",
}));

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

jest.mock("@/lib/api/cards", () => ({
    ...jest.requireActual<Record<string, unknown>>("@/lib/api/cards"),
    ListCards: jest.fn(),
    SelectCards: jest.fn(),
    ViewCard: jest.fn(),
    ViewSelectedCard: jest.fn(),
    FetchCardForEdit: jest.fn(),
    UpdateCard: jest.fn(),
}));

// Typed keys are read by ViewCard; a COCRDLIC selection, in list-entry mode, by ViewSelectedCard.
const mockViewCard = jest.mocked(ViewCard);
const mockViewSelectedCard = jest.mocked(ViewSelectedCard);
type CardReadMock = typeof mockViewCard;

// Test keys only: the card digits are built at runtime, so no card number is written in code.
const TEST_ACCOUNT_ID = "00000000004";
const TEST_CARD_REF = "card-ref-1";
// A second COCRDLIC selection, for an arrival that names another card.
const OTHER_ACCOUNT_ID = "00000000005";
const OTHER_CARD_REF = "card-ref-2";
const TYPED_CARD_NUMBER = "4".repeat(16);
const MASKED_CARD_NUMBER = "*".repeat(12) + "4312";
const PAN_PATTERN = /\d{16}/;
// Keys the server edits reject; the page must send them exactly as keyed, blanks included.
const SHORT_ACCOUNT_TEXT = "123 ";
const SHORT_CARD_TEXT = " 12";
const TEST_EMBOSSED_NAME = "TEST CARD HOLDER";
const TEST_ACTIVE_STATUS = "Y";
const TEST_EXPIRY_MONTH = "07";
const TEST_EXPIRY_YEAR = "2027";
// The name an answer abandoned by a newer lookup carries; it must never reach the screen.
const STALE_EMBOSSED_NAME = "STALE CARD HOLDER";
// A cardRef query value no server issued, as short as a typed card number.
const GARBLED_CARD_REF = "garbage";
// Text keyed into a key while its read is pending; the key must keep the text that was sent.
const KEYED_DURING_READ = "9";
// MUI 9 marks an outlined (typable) and a filled (protected) TextField on its InputBase root.
const OUTLINED_INPUT_CLASS = "MuiOutlinedInput-root";
// Every read is handed the signal of the AbortController that owns it.
const READ_SIGNAL_OPTIONS = expect.objectContaining({ signal: expect.any(AbortSignal) });

// The COCRDSL title, INITIAL texts (internal blanks kept) and FKEYS legend buttons.
const SCREEN_TITLE = "View Credit Card Detail";
const ACCOUNT_LABEL = "Account Number    :";
const CARD_LABEL = "Card Number       :";
const NAME_LABEL = "Name on card      :";
const STATUS_LABEL = "Card Active Y/N   :";
const EXPIRY_LABEL = "Expiry Date       :";
const EXPIRY_MONTH_LABEL = "Month";
const EXPIRY_YEAR_LABEL = "Year";
// Accessible names collapse the label's internal blanks.
const EXPIRY_GROUP_NAME = /^Expiry Date\s+:$/;
const SEARCH_BUTTON_NAME = "Search Cards (Enter)";
const EXIT_BUTTON_NAME = "Exit (F3)";
const KEY_LABELS: readonly string[] = [ACCOUNT_LABEL, CARD_LABEL];
const DETAIL_LABELS: readonly string[] = [
    NAME_LABEL,
    STATUS_LABEL,
    EXPIRY_MONTH_LABEL,
    EXPIRY_YEAR_LABEL,
];

// F3 returns to the main menu, whether the menu or a card list selection opened the screen.
const MENU_ROUTE = "/menu";

// The field names cocrdslc.py sends in field_errors and cursor_field.
const ACCT_ID_FIELD = "acct_id";
const CARD_NUM_FIELD = "card_num";

// WS-RETURN-MSG literals and the WS-INFO-MSG lines; DETAILS_SHOWN_INFO keeps 3 leading blanks.
const ACCOUNT_NOT_PROVIDED_MESSAGE = "Account number not provided";
const CARD_NOT_PROVIDED_MESSAGE = "Card number not provided";
const NO_INPUT_RECEIVED_MESSAGE = "No input received";
const ACCOUNT_FILTER_MESSAGE = "ACCOUNT FILTER,IF SUPPLIED MUST BE A 11 DIGIT NUMBER";
const CARD_FILTER_MESSAGE = "CARD ID FILTER,IF SUPPLIED MUST BE A 16 DIGIT NUMBER";
const CARD_NOT_FOUND_MESSAGE = "Did not find cards for this search condition";
// 9100-GETCARD-BYACCTCARD WHEN OTHER: WS-FILE-ERROR-MESSAGE for a CARDDAT IOERR (RESP 17).
const CARD_FILE_ERROR_MESSAGE =
    "File Error: READ     on CARDDAT   returned RESP 000000017 ,RESP2 000000000";
const DETAILS_SHOWN_INFO = "   Displaying requested details";
const PROMPT_FOR_KEYS_INFO = "Please enter Account and Card Number";
// A refusal literal another page leaves for the next screen (StorePendingMessage), trailing
// blank kept.
const PENDING_REFUSAL = "No access - Admin Only option... ";

const HTTP_EDIT_FAILURE = 400;
const HTTP_UNAUTHORIZED = 401;
const HTTP_NOT_FOUND = 404;
const HTTP_UNPROCESSABLE_CONTENT = 422;
const HTTP_INTERNAL_SERVER_ERROR = 500;
const HTTP_FILE_ERROR = 503;

// MUI 9 marks an Alert's colour as MuiAlert-color<Severity> (Alert/alertClasses.d.ts).
const SEVERITY_CLASSES: Readonly<Record<MessageSeverity, string>> = {
    error: "MuiAlert-colorError",
    success: "MuiAlert-colorSuccess",
    info: "MuiAlert-colorInfo",
};

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

// [field label, BMS LENGTH] of ACCTSID, CARDSID, CRDNAME, CRDSTCD, EXPMON and EXPYEAR.
const FIELD_LENGTH_CASES: readonly [string, number][] = [
    [ACCOUNT_LABEL, 11],
    [CARD_LABEL, 16],
    [NAME_LABEL, 50],
    [STATUS_LABEL, 1],
    [EXPIRY_MONTH_LABEL, 2],
    [EXPIRY_YEAR_LABEL, 4],
];

// [literal, HTTP status, message-line severity] of every COCRDSLC error outcome.
const LITERAL_CASES: readonly [string, number, MessageSeverity][] = [
    [ACCOUNT_NOT_PROVIDED_MESSAGE, HTTP_EDIT_FAILURE, "error"],
    [CARD_NOT_PROVIDED_MESSAGE, HTTP_EDIT_FAILURE, "error"],
    [NO_INPUT_RECEIVED_MESSAGE, HTTP_EDIT_FAILURE, "error"],
    [ACCOUNT_FILTER_MESSAGE, HTTP_EDIT_FAILURE, "error"],
    [CARD_FILTER_MESSAGE, HTTP_EDIT_FAILURE, "error"],
    [CARD_NOT_FOUND_MESSAGE, HTTP_NOT_FOUND, "error"],
    [CARD_FILE_ERROR_MESSAGE, HTTP_FILE_ERROR, "error"],
];

// The id of the MessageLine ERRMSG Alert, which a field named in field_errors is described by.
const ERRMSG_ID = "message-line-errmsg";

// [field name, label] of the two keys the server can name in field_errors and cursor_field.
const KEY_FIELD_LABELS: readonly [string, string][] = [
    [ACCT_ID_FIELD, ACCOUNT_LABEL],
    [CARD_NUM_FIELD, CARD_LABEL],
];

// Both keys blank fail both edits (cursor on the account); a bad card number fails only its own.
const FIELD_ERROR_CASES: readonly FieldErrorCase[] = [
    {
        body: BuildErrorBody({
            message: NO_INPUT_RECEIVED_MESSAGE,
            field_errors: [ACCT_ID_FIELD, CARD_NUM_FIELD],
            cursor_field: ACCT_ID_FIELD,
        }),
        cursorLabel: ACCOUNT_LABEL,
    },
    {
        body: BuildErrorBody({
            message: CARD_FILTER_MESSAGE,
            field_errors: [CARD_NUM_FIELD],
            cursor_field: CARD_NUM_FIELD,
        }),
        cursorLabel: CARD_LABEL,
    },
];

// A card_ref no server issued: Did not find cards for this search condition, both keys failing.
const NOT_FOUND_REFUSAL: KeyRefusalCase = {
    status: HTTP_NOT_FOUND,
    body: BuildErrorBody({
        message: CARD_NOT_FOUND_MESSAGE,
        field_errors: [ACCT_ID_FIELD, CARD_NUM_FIELD],
    }),
    message: CARD_NOT_FOUND_MESSAGE,
    fieldErrors: [ACCT_ID_FIELD, CARD_NUM_FIELD],
};
// The answers that refuse the selection's keys: a retry of the same card_ref cannot be read. A
// 422 names the query parameter it rejects, which is no screen field.
const KEY_REFUSAL_CASES: readonly KeyRefusalCase[] = [
    NOT_FOUND_REFUSAL,
    {
        status: HTTP_EDIT_FAILURE,
        body: BuildErrorBody({ message: ACCOUNT_FILTER_MESSAGE, field_errors: [ACCT_ID_FIELD] }),
        message: ACCOUNT_FILTER_MESSAGE,
        fieldErrors: [ACCT_ID_FIELD],
    },
    {
        status: HTTP_UNPROCESSABLE_CONTENT,
        body: { detail: [], message: "", field_errors: ["acctId"], cursor_field: "acctId" },
        message: ABEND_MESSAGE,
        fieldErrors: [],
    },
];
// [failure, its rejection, the literal shown] of a selection's read that a retry may still read.
const RETRYABLE_READ_FAILURES: readonly [string, () => AxiosError, string][] = [
    [
        "HTTP 503",
        () =>
            BuildAxiosError(HTTP_FILE_ERROR, BuildErrorBody({ message: CARD_FILE_ERROR_MESSAGE })),
        CARD_FILE_ERROR_MESSAGE,
    ],
    [
        "HTTP 500",
        () => BuildAxiosError(HTTP_INTERNAL_SERVER_ERROR, { detail: "Internal Server Error" }),
        ABEND_MESSAGE,
    ],
    ["a lost connection", BuildNetworkError, ABEND_MESSAGE],
];

// The ENTER and F3 legend actions, each by its button and by its physical key.
const SEARCH_ACTIONS: readonly [string, ScreenAction][] = [
    [SEARCH_BUTTON_NAME, (user) => ClickButton(user, SEARCH_BUTTON_NAME)],
    ["the Enter key", (user) => PressKey(user, "Enter")],
];
const EXIT_ACTIONS: readonly [string, ScreenAction][] = [
    [EXIT_BUTTON_NAME, (user) => ClickButton(user, EXIT_BUTTON_NAME)],
    ["the F3 key", (user) => PressKey(user, "F3")],
];
// One synchronous ENTER by its button and by its physical key. Two of them inside one act both
// land before React re-renders the disabled button, so only the page's in-flight guard can
// refuse the second.
const SAME_TURN_SEARCH_PRESSES: readonly [string, () => void][] = [
    [
        SEARCH_BUTTON_NAME,
        () => fireEvent.click(screen.getByRole("button", { name: SEARCH_BUTTON_NAME })),
    ],
    ["the Enter key", () => fireEvent.keyDown(InputFor(CARD_LABEL), { key: "Enter" })],
];

// The default normalizer trims and collapses the blanks the COCRDSL labels and literals keep.
function KeepRawText(text: string): string {
    return text;
}

function InputFor(label: string): HTMLElement {
    return screen.getByLabelText(label, { normalizer: KeepRawText });
}

function ScreenTree(): ReactElement {
    return (
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <CardViewPage />
        </ThemeProvider>
    );
}

// reactStrictMode adds the development double render and double effect run of next dev.
function RenderScreen(reactStrictMode = false): ScreenUser {
    const user = userEvent.setup();
    render(ScreenTree(), { reactStrictMode });
    return user;
}

// A COCRDLIC selection opens the page with the card_ref token and the account as its query.
function RenderFromCardList(reactStrictMode = false): ScreenUser {
    mockSearchParams = new URLSearchParams({ cardRef: TEST_CARD_REF, acctId: TEST_ACCOUNT_ID });
    mockViewSelectedCard.mockResolvedValue(BuildCardDetail());
    return RenderScreen(reactStrictMode);
}

// Every read of readMock waits until the test calls the returned release. The executor runs at
// once, so the resolver is assigned before it is returned. The answer then settles inside the
// next Testing Library wait, as the page's other answers do.
function HoldReads(readMock: CardReadMock): (detail: CardDetailResponse) => void {
    let releaseRead: (detail: CardDetailResponse) => void = () => undefined;
    readMock.mockReturnValue(
        new Promise<CardDetailResponse>((resolve) => {
            releaseRead = resolve;
        }),
    );
    return releaseRead;
}

function SelectFromCardList(): void {
    mockSearchParams = new URLSearchParams({ cardRef: TEST_CARD_REF, acctId: TEST_ACCOUNT_ID });
}

// A second COCRDLIC selection while the screen is shown: the query names another card.
function SelectOtherFromCardList(): void {
    mockSearchParams = new URLSearchParams({ cardRef: OTHER_CARD_REF, acctId: OTHER_ACCOUNT_ID });
}

// The screen alone, for a case that leaves it; the answer is the screen's unmount.
function RenderUnmountableScreen(): () => void {
    const { unmount } = render(ScreenTree());
    return unmount;
}

// A read still in flight: it settles only as axios does once its signal is aborted.
function PendUntilAborted(options?: ReadOptions): Promise<never> {
    return new Promise((_resolve, reject) => {
        options?.signal?.addEventListener("abort", () => reject(new CanceledError()));
    });
}

// The arguments of one ViewCard or ViewSelectedCard call: both keys as sent, and the read's own
// signal.
function ReadCall(cardKey: string, acctKey: string): unknown[] {
    return [cardKey, acctKey, READ_SIGNAL_OPTIONS];
}

// The signal readMock was handed on the call with this index.
function SignalOfCall(readMock: CardReadMock, callIndex: number): AbortSignal | undefined {
    return readMock.mock.calls[callIndex]?.[2]?.signal;
}

// The readMock calls whose read is still live: neither abandoned nor sent without a signal.
function LiveReadCount(readMock: CardReadMock): number {
    return readMock.mock.calls.filter(([, , options]) => options?.signal?.aborted === false).length;
}

// The 1200-SETUP-SCREEN-VARS answer for a found card: the number masked and no CVV.
function BuildCardDetail(overrides: Partial<CardDetailResponse> = {}): CardDetailResponse {
    return {
        acct_id: TEST_ACCOUNT_ID,
        card_ref: TEST_CARD_REF,
        card_num_masked: MASKED_CARD_NUMBER,
        embossed_name: TEST_EMBOSSED_NAME,
        active_status: TEST_ACTIVE_STATUS,
        expiry_month: TEST_EXPIRY_MONTH,
        expiry_year: TEST_EXPIRY_YEAR,
        message: "",
        info_message: DETAILS_SHOWN_INFO,
        field_errors: [],
        cursor_field: ACCT_ID_FIELD,
        ...overrides,
    };
}

// The 400, 404 and 503 bodies carry the key prompt as their information line.
function BuildErrorBody(overrides: Partial<LegacyErrorResponse> = {}): LegacyErrorResponse {
    return {
        message: "",
        info_message: PROMPT_FOR_KEYS_INFO,
        field_errors: [],
        cursor_field: ACCT_ID_FIELD,
        ...overrides,
    };
}

// The rejection API_CLIENT gives for a non-2xx answer carrying the body.
function BuildAxiosError(status: number, body: object): AxiosError<object> {
    const config: InternalAxiosRequestConfig = { headers: new AxiosHeaders() };
    const response: AxiosResponse<object> = {
        data: body,
        status,
        statusText: "",
        headers: {},
        config,
    };
    const code = status >= HTTP_INTERNAL_SERVER_ERROR ? "ERR_BAD_RESPONSE" : "ERR_BAD_REQUEST";
    return new AxiosError(
        `Request failed with status code ${status}`,
        code,
        config,
        null,
        response,
    );
}

// The rejection API_CLIENT gives when a request was sent and no answer came back.
function BuildNetworkError(): AxiosError {
    const config: InternalAxiosRequestConfig = { headers: new AxiosHeaders() };
    return new AxiosError("Network Error", AxiosError.ERR_NETWORK, config, {});
}

// Both keys are UNPROT fields the user can type into: editable and outlined.
function ExpectTypableKeys(): void {
    for (const label of KEY_LABELS) {
        expect(InputFor(label)).not.toHaveAttribute("readonly");
        expect(InputFor(label).closest(`.${OUTLINED_INPUT_CLASS}`)).not.toBeNull();
    }
}

// While a read is pending both typable keys are read-only, keeping their outlined look.
function ExpectLockedTypableKeys(): void {
    for (const label of KEY_LABELS) {
        expect(InputFor(label)).toHaveAttribute("readonly");
        expect(InputFor(label).closest(`.${OUTLINED_INPUT_CLASS}`)).not.toBeNull();
    }
}

function ExpectKeyValues(acctText: string, cardText: string): void {
    expect(InputFor(ACCOUNT_LABEL)).toHaveValue(acctText);
    expect(InputFor(CARD_LABEL)).toHaveValue(cardText);
}

// A COCRDLIC arrival whose card_ref the server refuses for its keys, shown with its literal.
async function RenderRefusedSelection(refusal: KeyRefusalCase): Promise<ScreenUser> {
    mockSearchParams = new URLSearchParams({ cardRef: GARBLED_CARD_REF, acctId: TEST_ACCOUNT_ID });
    mockViewSelectedCard.mockRejectedValueOnce(BuildAxiosError(refusal.status, refusal.body));
    const user = RenderScreen();
    await ExpectAlert(refusal.message, "error");
    return user;
}

// Text keyed into both keys while their read is pending: by the keyboard, which a read-only
// input ignores, and by a change event, which only the page's pending-read guard refuses.
async function KeyIntoBothKeys(user: ScreenUser): Promise<void> {
    for (const label of KEY_LABELS) {
        await user.type(InputFor(label), KEYED_DURING_READ);
        fireEvent.change(InputFor(label), { target: { value: KEYED_DURING_READ } });
    }
}

// Waits for the message-line Alert holding the exact raw text (ERRMSG has role alert, INFOMSG
// role status) and checks its colour.
async function ExpectAlert(text: string, severity: MessageSeverity): Promise<void> {
    const textElement = await screen.findByText(text, { normalizer: KeepRawText });
    const alert = textElement.closest('[role="alert"], [role="status"]');
    expect(alert).toHaveClass(SEVERITY_CLASSES[severity]);
    expect(alert?.textContent).toBe(text);
}

async function PressKey(user: ScreenUser, key: string): Promise<void> {
    await user.keyboard(`{${key}}`);
}

async function ClickButton(user: ScreenUser, name: string): Promise<void> {
    await user.click(screen.getByRole("button", { name }));
}

async function TypeKeys(user: ScreenUser, acctText: string, cardText: string): Promise<void> {
    await user.type(InputFor(ACCOUNT_LABEL), acctText);
    await user.type(InputFor(CARD_LABEL), cardText);
}

// A typed card number may reach only the API path: never a route, nor the page's own URL.
function ExpectNoCardNumberInUrls(): void {
    const routeArguments = [...mockRouterPush.mock.calls, ...mockRouterReplace.mock.calls].flat();
    const panArguments = routeArguments.filter((argument) => PAN_PATTERN.test(String(argument)));
    expect(panArguments).toEqual([]);
    expect(window.location.href).not.toMatch(PAN_PATTERN);
}

// A key named in field_errors is shown in error (aria-invalid) and described by the ERRMSG line;
// the other key is neither.
function ExpectInvalidKeys(fieldErrors: readonly string[]): void {
    for (const [fieldName, label] of KEY_FIELD_LABELS) {
        const isInvalid = fieldErrors.includes(fieldName);
        expect(InputFor(label)).toHaveAttribute("aria-invalid", String(isInvalid));
        const description = InputFor(label).getAttribute("aria-describedby");
        expect(description).toBe(isInvalid ? ERRMSG_ID : null);
    }
}

// The lookup is over once the busy bar is gone: its answer and the busy flag arrive together.
async function WaitForLookupEnd(): Promise<void> {
    await waitFor(() => {
        expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    });
}

// clearMocks keeps queued answers, so each case starts from empty ViewCard and ViewSelectedCard
// queues.
beforeEach(() => {
    jest.useFakeTimers(CLOCK_ONLY_FAKE_TIMERS);
    mockSearchParams = new URLSearchParams();
    mockViewCard.mockReset();
    mockViewSelectedCard.mockReset();
    window.sessionStorage.clear();
});

afterEach(() => {
    jest.useRealTimers();
});

describe("COCRDSLC BMS COCRDSL header and legend on /cards/view", () => {
    it("COCRDSLC shows TRNNAME CCDL, PGMNAME COCRDSLC and the screen title", () => {
        RenderScreen();
        const banner = screen.getByRole("banner");
        expect(within(banner).getByText("Tran: CCDL")).toBeInTheDocument();
        expect(within(banner).getByText("Prog: COCRDSLC")).toBeInTheDocument();
        expect(screen.getByRole("heading", { level: 1, name: SCREEN_TITLE })).toBeVisible();
    });

    it("COCRDSLC FKEYS legend ENTER=Search Cards  F3=Exit renders as two buttons", () => {
        RenderScreen();
        const keyBar = screen.getByRole("group", { name: "Function keys" });
        const buttonNames = within(keyBar)
            .getAllByRole("button")
            .map((button) => button.textContent);
        expect(buttonNames).toEqual([SEARCH_BUTTON_NAME, EXIT_BUTTON_NAME]);
    });
});

describe("COCRDSLC BMS COCRDSL fields on /cards/view", () => {
    it.each(FIELD_LENGTH_CASES)("COCRDSLC field %p keeps its BMS LENGTH %i", (label, length) => {
        RenderScreen();
        expect(InputFor(label)).toHaveAttribute("maxlength", String(length));
    });

    it("COCRDSLC shows the six COCRDSL fields and no card verification code", () => {
        RenderScreen();
        expect(screen.getAllByRole("textbox")).toHaveLength(FIELD_LENGTH_CASES.length);
        expect(screen.queryByLabelText(/cvv/i)).not.toBeInTheDocument();
    });
});

describe("COCRDSLC BMS COCRDSL expiry group on /cards/view", () => {
    it("COCRDSLC groups EXPMON, the / literal and EXPYEAR under the Expiry Date label", () => {
        RenderScreen();
        expect(screen.getByText(EXPIRY_LABEL, { normalizer: KeepRawText })).toBeInTheDocument();
        const expiryGroup = screen.getByRole("group", { name: EXPIRY_GROUP_NAME });
        expect(within(expiryGroup).getByLabelText(EXPIRY_MONTH_LABEL)).toBeInTheDocument();
        expect(within(expiryGroup).getByText("/")).toHaveAttribute("aria-hidden", "true");
        expect(within(expiryGroup).getByLabelText(EXPIRY_YEAR_LABEL)).toBeInTheDocument();
    });
});

describe("COCRDSLC BMS COCRDSL attributes from the menu on /cards/view", () => {
    it("COCRDSLC detail fields are ASKIP read-only and the UNPROT keys typable", () => {
        RenderScreen();
        for (const label of DETAIL_LABELS) {
            expect(InputFor(label)).toHaveAttribute("readonly");
        }
        for (const label of KEY_LABELS) {
            expect(InputFor(label)).not.toHaveAttribute("readonly");
            expect(InputFor(label)).toHaveValue("");
        }
    });

    it("COCRDSLC places the cursor on ACCTSID (IC) and reads nothing before ENTER", async () => {
        RenderScreen();
        expect(InputFor(ACCOUNT_LABEL)).toHaveFocus();
        await ExpectAlert(PROMPT_FOR_KEYS_INFO, "info");
        expect(mockViewCard).not.toHaveBeenCalled();
        expect(mockViewSelectedCard).not.toHaveBeenCalled();
        expect(screen.queryAllByRole("alert")).toHaveLength(0);
    });
});

// 1200-SETUP-SCREEN-VARS sets WS-PROMPT-FOR-INPUT whenever no information line is set.
describe("COCRDSLC a pending refusal from the main menu on /cards/view", () => {
    it("COCRDSLC shows the stored refusal in red and keeps the key prompt", async () => {
        StorePendingMessage(PENDING_REFUSAL);
        RenderScreen();
        await ExpectAlert(PENDING_REFUSAL, "error");
        await ExpectAlert(PROMPT_FOR_KEYS_INFO, "info");
        expect(mockViewCard).not.toHaveBeenCalled();
    });
});

describe("COCRDSLC entered from the COCRDLIC card list on /cards/view", () => {
    it("COCRDSLC reads the selected card once on mount by card_ref and account", async () => {
        RenderFromCardList();
        await ExpectAlert(DETAILS_SHOWN_INFO, "info");
        expect(mockViewSelectedCard.mock.calls).toEqual([ReadCall(TEST_CARD_REF, TEST_ACCOUNT_ID)]);
        expect(mockViewCard).not.toHaveBeenCalled();
    });

    it("COCRDSLC protects both keys and shows the card number only masked", async () => {
        RenderFromCardList();
        await ExpectAlert(DETAILS_SHOWN_INFO, "info");
        expect(InputFor(ACCOUNT_LABEL)).toHaveValue(TEST_ACCOUNT_ID);
        expect(InputFor(CARD_LABEL)).toHaveValue(MASKED_CARD_NUMBER);
        for (const label of KEY_LABELS) {
            expect(InputFor(label)).toHaveAttribute("readonly");
            expect(InputFor(label)).not.toHaveValue(expect.stringMatching(PAN_PATTERN));
        }
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

// ACCTSID shows the selection's account until the read answers with the account it read.
describe("COCRDSLC a COCRDLIC selection with an over-long acctId on /cards/view", () => {
    it.each(OVERLONG_ACCOUNT_ID_CASES)(
        "COCRDSLC cuts an acctId of $shape to the ACCTSID LENGTH before it reads",
        async ({ queryAcctId, keptAcctId }) => {
            const releaseRead = HoldReads(mockViewSelectedCard);
            mockSearchParams = new URLSearchParams({ cardRef: TEST_CARD_REF, acctId: queryAcctId });
            RenderScreen();
            expect(mockViewSelectedCard.mock.calls).toEqual([ReadCall(TEST_CARD_REF, keptAcctId)]);
            expect(InputFor(ACCOUNT_LABEL)).toHaveValue(keptAcctId);
            releaseRead(BuildCardDetail());
            await ExpectAlert(DETAILS_SHOWN_INFO, "info");
        },
    );
});

// An acctId of exactly the ACCTSID LENGTH is kept whole; the over-long ones are cut as above.
const BARE_ACCOUNT_ID_CASES: readonly OverlongAccountIdCase[] = [
    { shape: "11 digits", queryAcctId: TEST_ACCOUNT_ID, keptAcctId: TEST_ACCOUNT_ID },
    ...OVERLONG_ACCOUNT_ID_CASES,
];

// 1200-SETUP-SCREEN-VARS moves CDEMO-ACCT-ID to ACCTSID with or without a card number, so an
// entry naming an account and no card_ref shows it on typable keys beside the key prompt, with
// CARDSID blank, and waits for ENTER.
describe("COCRDSLC an acctId entry without a card_ref on /cards/view", () => {
    it.each(BARE_ACCOUNT_ID_CASES)(
        "COCRDSLC shows an acctId of $shape on ACCTSID as kept and reads nothing before ENTER",
        async ({ queryAcctId, keptAcctId }) => {
            mockSearchParams = new URLSearchParams({ acctId: queryAcctId });
            RenderScreen();
            await ExpectAlert(PROMPT_FOR_KEYS_INFO, "info");
            ExpectTypableKeys();
            ExpectKeyValues(keptAcctId, "");
            expect(mockViewCard).not.toHaveBeenCalled();
            expect(mockViewSelectedCard).not.toHaveBeenCalled();
        },
    );
});

describe("COCRDSLC ENTER after an acctId entry without a card_ref on /cards/view", () => {
    it("COCRDSLC ENTER sends the typed card number with the seeded account as typed", async () => {
        mockSearchParams = new URLSearchParams({ acctId: TEST_ACCOUNT_ID });
        mockViewCard.mockResolvedValue(BuildCardDetail());
        const user = RenderScreen();
        await user.type(InputFor(CARD_LABEL), TYPED_CARD_NUMBER);
        await PressKey(user, "Enter");
        await ExpectAlert(DETAILS_SHOWN_INFO, "info");
        expect(mockViewCard.mock.calls).toEqual([ReadCall(TYPED_CARD_NUMBER, TEST_ACCOUNT_ID)]);
        expect(mockViewSelectedCard).not.toHaveBeenCalled();
        ExpectKeyValues(TEST_ACCOUNT_ID, TYPED_CARD_NUMBER);
        ExpectNoCardNumberInUrls();
    });
});

// The read on arrival answers with its own information line, so no key prompt is set first.
describe("COCRDSLC information line from the COCRDLIC card list on /cards/view", () => {
    it("COCRDSLC shows no key prompt while or after the selected card is read", async () => {
        const releaseRead = HoldReads(mockViewSelectedCard);
        SelectFromCardList();
        RenderScreen();
        expect(screen.queryAllByRole("status")).toHaveLength(0);
        releaseRead(BuildCardDetail());
        await ExpectAlert(DETAILS_SHOWN_INFO, "info");
        expect(screen.queryByText(PROMPT_FOR_KEYS_INFO)).not.toBeInTheDocument();
    });
});

describe("COCRDSLC entered from the COCRDLIC card list under StrictMode on /cards/view", () => {
    it("COCRDSLC keeps one live read of the selected card in the double effect run", async () => {
        RenderFromCardList(true);
        await ExpectAlert(DETAILS_SHOWN_INFO, "info");
        expect(mockViewSelectedCard.mock.calls).toEqual([
            ReadCall(TEST_CARD_REF, TEST_ACCOUNT_ID),
            ReadCall(TEST_CARD_REF, TEST_ACCOUNT_ID),
        ]);
        expect(LiveReadCount(mockViewSelectedCard)).toBe(1);
    });
});

describe("COCRDSLC entered again with another COCRDLIC selection on /cards/view", () => {
    it("COCRDSLC reads a newly selected card while the previous read is pending", async () => {
        const releaseRead = HoldReads(mockViewSelectedCard);
        SelectFromCardList();
        const { rerender } = render(ScreenTree());
        SelectOtherFromCardList();
        rerender(ScreenTree());
        expect(mockViewSelectedCard).toHaveBeenCalledTimes(2);
        expect(mockViewSelectedCard).toHaveBeenLastCalledWith(
            ...ReadCall(OTHER_CARD_REF, OTHER_ACCOUNT_ID),
        );
        releaseRead(BuildCardDetail());
        await ExpectAlert(DETAILS_SHOWN_INFO, "info");
    });
});

describe("COCRDSLC card details from the COCRDLIC card list on /cards/view", () => {
    it("COCRDSLC shows CRDNAME, CRDSTCD, EXPMON and EXPYEAR as read", async () => {
        RenderFromCardList();
        await ExpectAlert(DETAILS_SHOWN_INFO, "info");
        expect(InputFor(NAME_LABEL)).toHaveValue(TEST_EMBOSSED_NAME);
        expect(InputFor(STATUS_LABEL)).toHaveValue(TEST_ACTIVE_STATUS);
        expect(InputFor(EXPIRY_MONTH_LABEL)).toHaveValue(TEST_EXPIRY_MONTH);
        expect(InputFor(EXPIRY_YEAR_LABEL)).toHaveValue(TEST_EXPIRY_YEAR);
    });
});

describe("COCRDSLC ENTER after a COCRDLIC selection on /cards/view", () => {
    it.each(SEARCH_ACTIONS)(
        "COCRDSLC ENTER by %s re-reads the card by card_ref",
        async (_, action) => {
            const user = RenderFromCardList();
            await ExpectAlert(DETAILS_SHOWN_INFO, "info");
            await action(user);
            await waitFor(() => expect(mockViewSelectedCard).toHaveBeenCalledTimes(2));
            await ExpectAlert(DETAILS_SHOWN_INFO, "info");
            expect(mockViewSelectedCard).toHaveBeenLastCalledWith(
                ...ReadCall(TEST_CARD_REF, TEST_ACCOUNT_ID),
            );
            expect(mockViewCard).not.toHaveBeenCalled();
        },
    );
});

// A refusal of the selection's keys ends list-entry mode: the keys become the user's to type,
// ACCTSID keeps the selection's account, and CARDSID starts blank, never echoing the card_ref.
describe("COCRDSLC a COCRDLIC selection refused for its keys on /cards/view", () => {
    it.each(KEY_REFUSAL_CASES)(
        "COCRDSLC HTTP $status hands typable keys over, account kept and card number blank",
        async (refusal) => {
            await RenderRefusedSelection(refusal);
            expect(mockViewSelectedCard).toHaveBeenCalledWith(
                ...ReadCall(GARBLED_CARD_REF, TEST_ACCOUNT_ID),
            );
            ExpectTypableKeys();
            ExpectKeyValues(TEST_ACCOUNT_ID, "");
            ExpectInvalidKeys(refusal.fieldErrors);
        },
    );
});

describe("COCRDSLC ENTER after a refused COCRDLIC selection on /cards/view", () => {
    it("COCRDSLC ENTER after the refusal reads the keys as typed, not the card_ref", async () => {
        const user = await RenderRefusedSelection(NOT_FOUND_REFUSAL);
        await waitFor(() => expect(InputFor(ACCOUNT_LABEL)).toHaveFocus());
        mockViewCard.mockResolvedValue(BuildCardDetail());
        await user.type(InputFor(CARD_LABEL), TYPED_CARD_NUMBER);
        await PressKey(user, "Enter");
        await ExpectAlert(DETAILS_SHOWN_INFO, "info");
        expect(mockViewCard).toHaveBeenCalledWith(...ReadCall(TYPED_CARD_NUMBER, TEST_ACCOUNT_ID));
        expect(mockViewSelectedCard).toHaveBeenCalledTimes(1);
        ExpectKeyValues(TEST_ACCOUNT_ID, TYPED_CARD_NUMBER);
        ExpectNoCardNumberInUrls();
    });
});

// A 503, a 500 or a lost connection says nothing about the keys, so list-entry mode holds.
describe("COCRDSLC a COCRDLIC selection whose read fails on /cards/view", () => {
    it.each(RETRYABLE_READ_FAILURES)(
        "COCRDSLC after %s keeps both keys protected and ENTER retries the card_ref",
        async (_, buildError, message) => {
            SelectFromCardList();
            mockViewSelectedCard.mockRejectedValueOnce(buildError());
            mockViewSelectedCard.mockResolvedValueOnce(BuildCardDetail());
            const user = RenderScreen();
            await ExpectAlert(message, "error");
            for (const label of KEY_LABELS) {
                expect(InputFor(label)).toHaveAttribute("readonly");
            }
            await PressKey(user, "Enter");
            await ExpectAlert(DETAILS_SHOWN_INFO, "info");
            const selectedRead = ReadCall(TEST_CARD_REF, TEST_ACCOUNT_ID);
            expect(mockViewSelectedCard.mock.calls).toEqual([selectedRead, selectedRead]);
            expect(mockViewCard).not.toHaveBeenCalled();
        },
    );
});

describe("COCRDSLC F3 after a COCRDLIC selection on /cards/view", () => {
    it.each(EXIT_ACTIONS)("COCRDSLC F3 by %s returns to the main menu /menu", async (_, action) => {
        const user = RenderFromCardList();
        await ExpectAlert(DETAILS_SHOWN_INFO, "info");
        await action(user);
        expect(mockRouterPush).toHaveBeenCalledTimes(1);
        expect(mockRouterPush).toHaveBeenCalledWith(MENU_ROUTE);
    });
});

describe("COCRDSLC PF keys from the main menu on /cards/view", () => {
    it.each(SEARCH_ACTIONS)("COCRDSLC ENTER by %s sends both keys as typed", async (_, action) => {
        mockViewCard.mockResolvedValue(BuildCardDetail());
        const user = RenderScreen();
        await TypeKeys(user, TEST_ACCOUNT_ID, TYPED_CARD_NUMBER);
        await action(user);
        await ExpectAlert(DETAILS_SHOWN_INFO, "info");
        expect(mockViewCard).toHaveBeenCalledTimes(1);
        expect(mockViewCard).toHaveBeenCalledWith(...ReadCall(TYPED_CARD_NUMBER, TEST_ACCOUNT_ID));
        ExpectNoCardNumberInUrls();
    });

    it.each(EXIT_ACTIONS)("COCRDSLC F3 by %s returns to the main menu /menu", async (_, action) => {
        const user = RenderScreen();
        await action(user);
        expect(mockRouterPush).toHaveBeenCalledTimes(1);
        expect(mockRouterPush).toHaveBeenCalledWith(MENU_ROUTE);
        expect(mockViewCard).not.toHaveBeenCalled();
    });
});

// COCRDSLC PFK-INVALID: an AID outside ENTER and PF3 does SET CCARD-AID-ENTER, so the key never
// reaches the browser and reads the card exactly as ENTER does.
const PFK_INVALID_KEYS: readonly string[] = ["F4", "F5", "F7", "F12"];

describe("COCRDSLC PFK-INVALID keys from the main menu on /cards/view", () => {
    it.each(PFK_INVALID_KEYS)(
        "COCRDSLC %s is processed as ENTER and sends both keys as typed",
        async (keyName) => {
            mockViewCard.mockResolvedValue(BuildCardDetail());
            const user = RenderScreen();
            await TypeKeys(user, TEST_ACCOUNT_ID, TYPED_CARD_NUMBER);
            const keyInit = { key: keyName, bubbles: true, cancelable: true };
            const keyEvent = new KeyboardEvent("keydown", keyInit);
            fireEvent(InputFor(CARD_LABEL), keyEvent);
            await ExpectAlert(DETAILS_SHOWN_INFO, "info");
            expect(keyEvent.defaultPrevented).toBe(true);
            expect(mockViewCard).toHaveBeenCalledTimes(1);
            expect(mockViewCard).toHaveBeenCalledWith(
                ...ReadCall(TYPED_CARD_NUMBER, TEST_ACCOUNT_ID),
            );
        },
    );
});

describe("COCRDSLC one read at a time from the main menu on /cards/view", () => {
    it.each(SAME_TURN_SEARCH_PRESSES)(
        "COCRDSLC ENTER by %s twice in one turn reads the card once",
        async (_, press) => {
            const releaseRead = HoldReads(mockViewCard);
            const user = RenderScreen();
            await TypeKeys(user, TEST_ACCOUNT_ID, TYPED_CARD_NUMBER);
            act(() => {
                press();
                press();
            });
            expect(mockViewCard).toHaveBeenCalledTimes(1);
            releaseRead(BuildCardDetail());
            await ExpectAlert(DETAILS_SHOWN_INFO, "info");
            expect(mockViewCard).toHaveBeenCalledTimes(1);
        },
    );
});

describe("COCRDSLC busy Search Cards key from the main menu on /cards/view", () => {
    it("COCRDSLC disables Search Cards only while a read is pending", async () => {
        const releaseRead = HoldReads(mockViewCard);
        const user = RenderScreen();
        await TypeKeys(user, TEST_ACCOUNT_ID, TYPED_CARD_NUMBER);
        await PressKey(user, "Enter");
        expect(screen.getByRole("button", { name: SEARCH_BUTTON_NAME })).toBeDisabled();
        expect(screen.getByRole("button", { name: EXIT_BUTTON_NAME })).toBeEnabled();
        await PressKey(user, "Enter");
        expect(mockViewCard).toHaveBeenCalledTimes(1);
        releaseRead(BuildCardDetail());
        await ExpectAlert(DETAILS_SHOWN_INFO, "info");
        expect(screen.getByRole("button", { name: SEARCH_BUTTON_NAME })).toBeEnabled();
    });
});

// The 3270 keyboard locks while the host runs the task: the keys keep the text that was sent, so
// the answer is shown under the keys it was read for.
describe("COCRDSLC typed keys during a pending read from the main menu on /cards/view", () => {
    it("COCRDSLC locks both keys while the read is pending, then shows its answer", async () => {
        const releaseRead = HoldReads(mockViewCard);
        const user = RenderScreen();
        await TypeKeys(user, TEST_ACCOUNT_ID, TYPED_CARD_NUMBER);
        await PressKey(user, "Enter");
        ExpectLockedTypableKeys();
        await KeyIntoBothKeys(user);
        ExpectKeyValues(TEST_ACCOUNT_ID, TYPED_CARD_NUMBER);
        releaseRead(BuildCardDetail());
        await ExpectAlert(DETAILS_SHOWN_INFO, "info");
        ExpectKeyValues(TEST_ACCOUNT_ID, TYPED_CARD_NUMBER);
        expect(InputFor(NAME_LABEL)).toHaveValue(TEST_EMBOSSED_NAME);
        ExpectTypableKeys();
        expect(mockViewCard).toHaveBeenCalledWith(...ReadCall(TYPED_CARD_NUMBER, TEST_ACCOUNT_ID));
    });
});

describe("COCRDSLC text keyed with ENTER from the main menu on /cards/view", () => {
    it("COCRDSLC refuses text keyed in the turn of ENTER, before the keys re-render", async () => {
        const releaseRead = HoldReads(mockViewCard);
        const user = RenderScreen();
        await TypeKeys(user, TEST_ACCOUNT_ID, TYPED_CARD_NUMBER);
        act(() => {
            fireEvent.keyDown(InputFor(CARD_LABEL), { key: "Enter" });
            fireEvent.change(InputFor(CARD_LABEL), { target: { value: KEYED_DURING_READ } });
        });
        ExpectKeyValues(TEST_ACCOUNT_ID, TYPED_CARD_NUMBER);
        expect(mockViewCard).toHaveBeenCalledWith(...ReadCall(TYPED_CARD_NUMBER, TEST_ACCOUNT_ID));
        releaseRead(BuildCardDetail());
        await ExpectAlert(DETAILS_SHOWN_INFO, "info");
        ExpectKeyValues(TEST_ACCOUNT_ID, TYPED_CARD_NUMBER);
    });
});

describe("COCRDSLC F3 during a pending read from the main menu on /cards/view", () => {
    it.each(EXIT_ACTIONS)(
        "COCRDSLC F3 by %s returns to /menu while a read is pending and abandons it",
        async (_, action) => {
            const releaseRead = HoldReads(mockViewCard);
            const user = RenderScreen();
            await TypeKeys(user, TEST_ACCOUNT_ID, TYPED_CARD_NUMBER);
            await PressKey(user, "Enter");
            await action(user);
            expect(mockRouterPush).toHaveBeenCalledTimes(1);
            expect(mockRouterPush).toHaveBeenCalledWith(MENU_ROUTE);
            expect(SignalOfCall(mockViewCard, 0)?.aborted).toBe(true);
            await act(async () => releaseRead(BuildCardDetail()));
            await WaitForLookupEnd();
            expect(InputFor(NAME_LABEL)).toHaveValue("");
            expect(screen.queryAllByRole("alert")).toHaveLength(0);
            expect(screen.queryAllByRole("status")).toHaveLength(0);
        },
    );
});

describe("COCRDSLC key edits stay on the server on /cards/view", () => {
    it("COCRDSLC ENTER sends short, blank-padded keys untrimmed to 2200-EDIT", async () => {
        const body = BuildErrorBody({ message: ACCOUNT_FILTER_MESSAGE });
        mockViewCard.mockRejectedValue(BuildAxiosError(HTTP_EDIT_FAILURE, body));
        const user = RenderScreen();
        await TypeKeys(user, SHORT_ACCOUNT_TEXT, SHORT_CARD_TEXT);
        await ClickButton(user, SEARCH_BUTTON_NAME);
        await ExpectAlert(ACCOUNT_FILTER_MESSAGE, "error");
        expect(mockViewCard).toHaveBeenCalledWith(...ReadCall(SHORT_CARD_TEXT, SHORT_ACCOUNT_TEXT));
    });
});

describe("COCRDSLC WS-RETURN-MSG literals on /cards/view", () => {
    it.each(LITERAL_CASES)(
        "COCRDSLC shows %p (HTTP %i) as %s",
        async (literal, status, severity) => {
            const body = BuildErrorBody({ message: literal });
            mockViewCard.mockRejectedValue(BuildAxiosError(status, body));
            const user = RenderScreen();
            await ClickButton(user, SEARCH_BUTTON_NAME);
            await ExpectAlert(literal, severity);
            await ExpectAlert(PROMPT_FOR_KEYS_INFO, "info");
        },
    );
});

describe("COCRDSLC abend text on /cards/view", () => {
    it("COCRDSLC 500 shows the abend text and no information line", async () => {
        const FASTAPI_BODY = { detail: "Internal Server Error" };
        mockViewCard.mockRejectedValue(BuildAxiosError(HTTP_INTERNAL_SERVER_ERROR, FASTAPI_BODY));
        const user = RenderScreen();
        await ClickButton(user, SEARCH_BUTTON_NAME);
        await ExpectAlert(ABEND_MESSAGE, "error");
        expect(screen.queryByTestId("message-line-info")).not.toBeInTheDocument();
    });
});

describe("COCRDSLC not-found after a shown card on /cards/view", () => {
    it("COCRDSLC not-found shows the search-condition literal and clears the details", async () => {
        const body = BuildErrorBody({ message: CARD_NOT_FOUND_MESSAGE });
        const user = RenderFromCardList();
        await ExpectAlert(DETAILS_SHOWN_INFO, "info");
        mockViewSelectedCard.mockRejectedValue(BuildAxiosError(HTTP_NOT_FOUND, body));
        await ClickButton(user, SEARCH_BUTTON_NAME);
        await ExpectAlert(CARD_NOT_FOUND_MESSAGE, "error");
        for (const label of DETAIL_LABELS) {
            expect(InputFor(label)).toHaveValue("");
        }
    });
});

describe("COCRDSLC session refusal on /cards/view", () => {
    it("COCRDSLC 401 shows no message; the apiClient interceptor owns /signon", async () => {
        const body = BuildErrorBody({ info_message: "", cursor_field: null });
        mockViewCard.mockRejectedValue(BuildAxiosError(HTTP_UNAUTHORIZED, body));
        const user = RenderScreen();
        await ClickButton(user, SEARCH_BUTTON_NAME);
        await WaitForLookupEnd();
        expect(mockViewCard).toHaveBeenCalledTimes(1);
        expect(screen.queryAllByRole("alert")).toHaveLength(0);
        expect(screen.queryAllByRole("status")).toHaveLength(0);
        expect(mockRouterPush).not.toHaveBeenCalled();
        expect(mockRouterReplace).not.toHaveBeenCalled();
        expect(SIGN_ON_ROUTE).toBe("/signon");
    });
});

describe("COCRDSLC field errors and cursor on /cards/view", () => {
    it.each(FIELD_ERROR_CASES)(
        "COCRDSLC marks $body.field_errors invalid and moves the cursor to $body.cursor_field",
        async ({ body, cursorLabel }) => {
            mockViewCard.mockRejectedValue(BuildAxiosError(HTTP_EDIT_FAILURE, body));
            const user = RenderScreen();
            await ClickButton(user, SEARCH_BUTTON_NAME);
            await ExpectAlert(body.message, "error");
            ExpectInvalidKeys(body.field_errors ?? []);
            await waitFor(() => expect(InputFor(cursorLabel)).toHaveFocus());
        },
    );
});

describe("COCRDSLC entry read under React StrictMode on /cards/view", () => {
    it("COCRDSLC under StrictMode abandons the first entry read and shows the second", async () => {
        mockViewSelectedCard.mockImplementationOnce((_cardRef, _acctId, options) =>
            PendUntilAborted(options),
        );
        RenderFromCardList(true);
        await ExpectAlert(DETAILS_SHOWN_INFO, "info");
        await WaitForLookupEnd();
        expect(mockViewSelectedCard).toHaveBeenCalledTimes(2);
        const signals = [0, 1].map((callIndex) => SignalOfCall(mockViewSelectedCard, callIndex));
        expect(signals.map((signal) => signal?.aborted)).toEqual([true, false]);
        expect(screen.queryByText(ABEND_MESSAGE)).not.toBeInTheDocument();
    });
});

describe("COCRDSLC leaving the screen during a read on /cards/view", () => {
    it("COCRDSLC leaving the screen aborts the pending read and logs nothing", async () => {
        const consoleErrorSpy = jest.spyOn(console, "error");
        SelectFromCardList();
        mockViewSelectedCard.mockImplementationOnce((_cardRef, _acctId, options) =>
            PendUntilAborted(options),
        );
        const unmount = RenderUnmountableScreen();
        await waitFor(() => expect(mockViewSelectedCard).toHaveBeenCalledTimes(1));
        expect(SignalOfCall(mockViewSelectedCard, 0)?.aborted).toBe(false);
        unmount();
        expect(SignalOfCall(mockViewSelectedCard, 0)?.aborted).toBe(true);
        await act(async () => undefined);
        expect(screen.queryAllByRole("alert")).toHaveLength(0);
        expect(screen.queryAllByRole("status")).toHaveLength(0);
        expect(consoleErrorSpy).not.toHaveBeenCalled();
        consoleErrorSpy.mockRestore();
    });
});

describe("COCRDSLC F3 during a pending read on /cards/view", () => {
    it("COCRDSLC F3 aborts the pending read before the route changes", async () => {
        SelectFromCardList();
        mockViewSelectedCard.mockImplementationOnce((_cardRef, _acctId, options) =>
            PendUntilAborted(options),
        );
        const user = RenderScreen();
        await waitFor(() => expect(mockViewSelectedCard).toHaveBeenCalledTimes(1));
        await PressKey(user, "F3");
        expect(SignalOfCall(mockViewSelectedCard, 0)?.aborted).toBe(true);
        expect(mockRouterPush).toHaveBeenCalledWith(MENU_ROUTE);
        await WaitForLookupEnd();
        expect(screen.queryAllByRole("alert")).toHaveLength(0);
        expect(screen.queryAllByRole("status")).toHaveLength(0);
    });
});

// ENTER cannot supersede a pending read, as the one-at-a-time guard refuses it; another card
// list selection is the one lookup that replaces a read still in flight.
describe("COCRDSLC a superseded read on /cards/view", () => {
    it("COCRDSLC a new selection aborts the pending read; only its own answer shows", async () => {
        const staleRead = Promise.withResolvers<CardDetailResponse>();
        mockViewSelectedCard.mockReturnValueOnce(staleRead.promise);
        mockViewSelectedCard.mockResolvedValueOnce(BuildCardDetail());
        SelectFromCardList();
        const { rerender } = render(ScreenTree());
        SelectOtherFromCardList();
        rerender(ScreenTree());
        await ExpectAlert(DETAILS_SHOWN_INFO, "info");
        const signals = [0, 1].map((callIndex) => SignalOfCall(mockViewSelectedCard, callIndex));
        expect(signals.map((signal) => signal?.aborted)).toEqual([true, false]);
        await act(async () =>
            staleRead.resolve(BuildCardDetail({ embossed_name: STALE_EMBOSSED_NAME })),
        );
        expect(InputFor(NAME_LABEL)).toHaveValue(TEST_EMBOSSED_NAME);
        await WaitForLookupEnd();
    });
});

describe("COCRDSLC a cancelled read on /cards/view", () => {
    it("COCRDSLC an abandoned read (CanceledError) shows no message and ends busy", async () => {
        mockViewCard.mockRejectedValueOnce(new CanceledError());
        const user = RenderScreen();
        await ClickButton(user, SEARCH_BUTTON_NAME);
        await WaitForLookupEnd();
        expect(mockViewCard).toHaveBeenCalledTimes(1);
        expect(screen.queryAllByRole("alert")).toHaveLength(0);
        expect(screen.queryAllByRole("status")).toHaveLength(0);
        expect(screen.getByRole("button", { name: SEARCH_BUTTON_NAME })).toBeEnabled();
    });
});
