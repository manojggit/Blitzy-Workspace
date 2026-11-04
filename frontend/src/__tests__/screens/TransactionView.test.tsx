// Screen tests: BMS COTRN01, program COTRN01C (CT01), page /transactions/view.
// Labels and LENGTHs come from bms/COTRN01.bms; the literals are copied verbatim from
// backend/app/services/transactions/cotrn01c.py, which ports cbl/COTRN01C.cbl lines 149, 285
// and 292. The API module is mocked, so each response body is the one the backend sends.
import { ThemeProvider } from "@mui/material/styles";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import {
    AxiosError,
    AxiosHeaders,
    CanceledError,
    type AxiosResponse,
    type InternalAxiosRequestConfig,
} from "axios";
import { StrictMode, type ReactNode } from "react";

import { CARD_DEMO_THEME } from "@/app/theme";
import TransactionViewPage from "@/app/transactions/view/page";
import { ViewTransaction, type TransactionDetailResponse } from "@/lib/api/transactions";
import type { LegacyErrorResponse, MessageSeverity, ReadOptions } from "@/lib/apiClient";
import type { SessionContextValue } from "@/lib/session";

type CurrentUser = NonNullable<SessionContextValue["currentUser"]>;
type PressMode = "button" | "key";

interface FunctionKeyCase {
    keyName: string;
    buttonName: string;
}

interface DetailFieldCase {
    label: string;
    maxLength: number;
    shownValue: string;
}

interface BackCase {
    mode: PressMode;
    origin: string;
    tranId: string;
    route: string;
}

interface LiteralCase {
    shownText: string;
    status: number;
    body: unknown;
    severity: MessageSeverity;
    typedText: string;
    sentKey: string;
}

// A read running as the screen is left: the arrival read of a CT00 selection (typedText empty) or
// an ENTER read of the typed key. F3 and F5 abort it before they navigate to routes; an unmount,
// which every other navigation brings, aborts it with no route of its own.
interface PendingReadCase {
    origin: string;
    queryTranId: string;
    typedText: string;
    leaving: string;
    LeaveScreen: (user: UserEvent) => Promise<void>;
    routes: readonly string[];
}

interface OverlongQueryCase {
    shape: string;
    queryTranId: string;
    selectedKey: string;
}

interface ScreenStatus {
    detailStatus: HTMLElement;
    messageText: string;
}

interface EmptiedStatusCase {
    action: string;
    messageText: string;
    Reach: (user: UserEvent) => Promise<void>;
}

// Read at render time by the mocked hooks; the "mock" prefix lets the hoisted factories use them.
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
let mockCurrentUser: CurrentUser | null = null;

jest.mock("next/navigation", () => ({
    useRouter: () => mockRouter,
    useSearchParams: () => mockSearchParams,
    usePathname: () => "/transactions/view",
}));

// The mocked keys mirror the exports of @/lib/session and its SessionContextValue.
jest.mock("@/lib/session", () => ({
    SessionProvider: ({ children }: { children: ReactNode }) => children,
    useSession: () => ({
        currentUser: mockCurrentUser,
        isLoading: false,
        RefreshSession: jest.fn(),
        ClearSession: jest.fn(),
    }),
}));

// Every endpoint function is stubbed; CT01 calls only ViewTransaction.
jest.mock("@/lib/api/transactions", () => ({
    ...jest.requireActual<Record<string, unknown>>("@/lib/api/transactions"),
    ListTransactions: jest.fn(),
    SelectTransactions: jest.fn(),
    ViewTransaction: jest.fn(),
    AddTransaction: jest.fn(),
}));

const mockViewTransaction = jest.mocked(ViewTransaction);

// The regular user COSGN00C routes to COMEN01C; the record carries no password.
const SIGNED_ON_USER: CurrentUser = {
    user_id: "TESTUSR1",
    user_type: "U",
    first_name: "Test",
    last_name: "User",
    next_route: "/menu",
};

const TEST_TRAN_ID = "0000000000000001";
const MISSING_TRAN_ID = "0000000000000042";
// TRNIDIN as keyed: COTRN01C neither trims, upper-cases nor otherwise edits it.
const TYPED_TRAN_ID = " tx 0000000042 ";
// A blank TRNIDIN reaches COTRN01C as SPACES, which its empty-key edit rejects.
const BLANK_TRAN_ID = " ";
const MASKED_CARD_NUMBER = "************4312";
// The stored 26-character TRAN-ORIG-TS (format S) and TRAN-PROC-TS (format D) texts.
const ORIG_TS_TEXT = "2022-06-10 19:27:53.000000";
const PROC_TS_TEXT = "2022-07-18-00.00.00.000000";
const TIMESTAMP_TEXT_LENGTH = 26;

const TRAN_ID_LABEL = "Enter Tran ID:";
// The cursor name the backend returns for TRNIDIN (MOVE -1 TO TRNIDINL).
const TRAN_ID_FIELD = "tran_id";
const SIGN_ON_ROUTE = "/signon";
const TRANSACTION_LIST_ROUTE = "/transactions";
const MAIN_MENU_ROUTE = "/menu";

// COTRN01C literals, verbatim, and the abend text the frontend shows for FastAPI's 500.
const TRAN_ID_EMPTY_MESSAGE = "Tran ID can NOT be empty...";
const TRAN_ID_NOT_FOUND_MESSAGE = "Transaction ID NOT found...";
const TRAN_LOOKUP_FAILED_MESSAGE = "Unable to lookup Transaction...";
const ABEND_MESSAGE = "UNEXPECTED ABEND OCCURRED.";
// The page's own polite status while a record is shown; COTRN01C leaves ERRMSG blank then.
const DETAIL_SHOWN_STATUS = "Transaction details displayed";
// The id of the MessageLine ERRMSG Alert, which a field named in field_errors is described by.
const ERRMSG_ID = "message-line-errmsg";

// The row-24 legend: ENTER=Fetch  F3=Back  F4=Clear  F5=Browse Tran.
const FETCH_KEY: FunctionKeyCase = { keyName: "Enter", buttonName: "Fetch (Enter)" };
const BACK_KEY: FunctionKeyCase = { keyName: "F3", buttonName: "Back (F3)" };
const CLEAR_KEY: FunctionKeyCase = { keyName: "F4", buttonName: "Clear (F4)" };
const BROWSE_KEY: FunctionKeyCase = { keyName: "F5", buttonName: "Browse Tran. (F5)" };
const LEGEND_BUTTON_NAMES: readonly string[] = [FETCH_KEY, BACK_KEY, CLEAR_KEY, BROWSE_KEY].map(
    (functionKey) => functionKey.buttonName,
);
const PRESS_MODES: readonly PressMode[] = ["button", "key"];

// MUI 9 marks the Alert colour as MuiAlert-color<Severity> (Alert/alertClasses.d.ts).
const SEVERITY_CLASSES: Readonly<Record<MessageSeverity, string>> = {
    error: "MuiAlert-colorError",
    success: "MuiAlert-colorSuccess",
    info: "MuiAlert-colorInfo",
};

// A CVTRA05Y record as the backend serves it: PAN masked, amount and timestamps as stored text.
const SAMPLE_DETAIL: Readonly<TransactionDetailResponse> = Object.freeze({
    tran_id: TEST_TRAN_ID,
    card_num_masked: MASKED_CARD_NUMBER,
    tran_type_cd: "01",
    tran_cat_cd: 1,
    tran_source: "POS TERM",
    tran_desc: "Purchase at Test Merchant",
    tran_amt: "50.47",
    orig_ts: ORIG_TS_TEXT,
    proc_ts: PROC_TS_TEXT,
    merchant_id: "800000001",
    merchant_name: "Test Merchant",
    merchant_city: "Test City",
    merchant_zip: "12345",
    message: "",
});

// The protected COTRN01 fields in map order, with their BMS INITIAL labels and LENGTHs, and the
// text each shows for SAMPLE_DETAIL; TCATCD shows the 9(04) category code zero-padded.
const DETAIL_FIELD_CASES: readonly DetailFieldCase[] = [
    { label: "Transaction ID:", maxLength: 16, shownValue: TEST_TRAN_ID },
    { label: "Card Number:", maxLength: 16, shownValue: MASKED_CARD_NUMBER },
    { label: "Type CD:", maxLength: 2, shownValue: "01" },
    { label: "Category CD:", maxLength: 4, shownValue: "0001" },
    { label: "Source:", maxLength: 10, shownValue: "POS TERM" },
    { label: "Description:", maxLength: 60, shownValue: "Purchase at Test Merchant" },
    { label: "Amount:", maxLength: 12, shownValue: "50.47" },
    { label: "Orig Date:", maxLength: 10, shownValue: ORIG_TS_TEXT },
    { label: "Proc Date:", maxLength: 10, shownValue: PROC_TS_TEXT },
    { label: "Merchant ID:", maxLength: 9, shownValue: "800000001" },
    { label: "Merchant Name:", maxLength: 30, shownValue: "Test Merchant" },
    { label: "Merchant City:", maxLength: 25, shownValue: "Test City" },
    { label: "Merchant Zip:", maxLength: 10, shownValue: "12345" },
];

function BuildTransactionDetail(
    overrides: Partial<TransactionDetailResponse> = {},
): TransactionDetailResponse {
    return { ...SAMPLE_DETAIL, ...overrides };
}

// A LegacyMessageError body as backend/app/core/errors.py builds it for COTRN01C: every outcome
// names TRNIDIN in field_errors and as the cursor field.
function BuildErrorBody(overrides: Partial<LegacyErrorResponse> = {}): LegacyErrorResponse {
    return {
        message: "",
        info_message: "",
        field_errors: [TRAN_ID_FIELD],
        cursor_field: TRAN_ID_FIELD,
        ...overrides,
    };
}

function BuildAxiosError(status: number, body: unknown): AxiosError {
    const config: InternalAxiosRequestConfig = {
        url: `/transactions/${TEST_TRAN_ID}`,
        method: "get",
        headers: new AxiosHeaders(),
    };
    const response: AxiosResponse = { status, data: body, statusText: "", headers: {}, config };
    return new AxiosError(
        "Request failed",
        AxiosError.ERR_BAD_RESPONSE,
        config,
        undefined,
        response,
    );
}

// The four READ-TRANSACT-FILE and PROCESS-ENTER-KEY outcomes: the backend's legacy bodies, and
// FastAPI's plain-text 500, which the page shows as the abend text.
const LITERAL_CASES: readonly LiteralCase[] = [
    {
        shownText: TRAN_ID_EMPTY_MESSAGE,
        status: 400,
        body: BuildErrorBody({ message: TRAN_ID_EMPTY_MESSAGE }),
        severity: "error",
        typedText: "",
        sentKey: BLANK_TRAN_ID,
    },
    {
        shownText: TRAN_ID_NOT_FOUND_MESSAGE,
        status: 404,
        body: BuildErrorBody({ message: TRAN_ID_NOT_FOUND_MESSAGE }),
        severity: "error",
        typedText: MISSING_TRAN_ID,
        sentKey: MISSING_TRAN_ID,
    },
    {
        shownText: TRAN_LOOKUP_FAILED_MESSAGE,
        status: 503,
        body: BuildErrorBody({ message: TRAN_LOOKUP_FAILED_MESSAGE }),
        severity: "error",
        typedText: TEST_TRAN_ID,
        sentKey: TEST_TRAN_ID,
    },
    {
        shownText: ABEND_MESSAGE,
        status: 500,
        body: "Internal Server Error",
        severity: "error",
        typedText: TEST_TRAN_ID,
        sentKey: TEST_TRAN_ID,
    },
];

// The legacy bodies, each naming TRNIDIN in field_errors; the plain-text 500 names no field.
const FLAGGED_LITERAL_CASES: readonly LiteralCase[] = LITERAL_CASES.filter(
    (literalCase) => typeof literalCase.body !== "string",
);

// F3 returns to the main menu (COMEN01C), whether the menu or a CT00 selection opened the screen.
const BACK_CASES: readonly BackCase[] = PRESS_MODES.flatMap((mode) => [
    { mode, origin: "a CT00 selection", tranId: TEST_TRAN_ID, route: MAIN_MENU_ROUTE },
    { mode, origin: "the main menu", tranId: "", route: MAIN_MENU_ROUTE },
]);

// tranId values longer than the X(16) CDEMO-CT01-TRN-SELECTED field, and the 16 characters it
// keeps of each; a character the cut would split in two is dropped whole.
const SPLIT_CHARACTER_PREFIX = "0".repeat(15);
const OVERLONG_QUERY_CASES: readonly OverlongQueryCase[] = [
    { shape: "20 digits", queryTranId: `${TEST_TRAN_ID}0042`, selectedKey: TEST_TRAN_ID },
    {
        shape: "28 characters of markup",
        queryTranId: "<img src=x onerror=alert(1)>",
        selectedKey: "<img src=x onerr",
    },
    {
        shape: "15 digits and a two-unit character",
        queryTranId: `${SPLIT_CHARACTER_PREFIX}\u{1F600}`,
        selectedKey: SPLIT_CHARACTER_PREFIX,
    },
];

// The mocked router does not navigate, so cleanup() stands in for the unmount.
const PENDING_READ_CASES: readonly PendingReadCase[] = [
    {
        origin: "the arrival read",
        queryTranId: TEST_TRAN_ID,
        typedText: "",
        leaving: "F3",
        LeaveScreen: (user) => PressFunctionKey(user, BACK_KEY, "key"),
        routes: [MAIN_MENU_ROUTE],
    },
    {
        origin: "an ENTER read",
        queryTranId: "",
        typedText: TEST_TRAN_ID,
        leaving: "F3",
        LeaveScreen: (user) => PressFunctionKey(user, BACK_KEY, "key"),
        routes: [MAIN_MENU_ROUTE],
    },
    {
        origin: "an ENTER read",
        queryTranId: "",
        typedText: TEST_TRAN_ID,
        leaving: "F5",
        LeaveScreen: (user) => PressFunctionKey(user, BROWSE_KEY, "key"),
        routes: [TRANSACTION_LIST_ROUTE],
    },
    {
        origin: "the arrival read",
        queryTranId: TEST_TRAN_ID,
        typedText: "",
        leaving: "an unmount",
        LeaveScreen: async () => cleanup(),
        routes: [],
    },
];

// Every read carries the AbortSignal of the controller that owns it.
const READ_SIGNAL_OPTIONS = expect.objectContaining({ signal: expect.any(AbortSignal) });

// A read that never answers: like axios, it rejects with CanceledError once its signal is
// aborted.
function PendUntilAborted(
    _tranId: string,
    options?: ReadOptions,
): Promise<TransactionDetailResponse> {
    return new Promise((_resolve, reject) => {
        options?.signal?.addEventListener("abort", () => reject(new CanceledError()));
    });
}

// The signal the page handed to the read made by its call at that position.
function ReadSignal(callPosition: number): AbortSignal | undefined {
    return mockViewTransaction.mock.calls[callPosition]?.[1]?.signal;
}

// Outcomes that take a displayed record off the screen, each starting from a shown record.
const EMPTIED_STATUS_CASES: readonly EmptiedStatusCase[] = [
    {
        action: "a Transaction ID NOT found read",
        messageText: TRAN_ID_NOT_FOUND_MESSAGE,
        Reach: async (user) => {
            const notFound = BuildErrorBody({ message: TRAN_ID_NOT_FOUND_MESSAGE });
            mockViewTransaction.mockRejectedValue(BuildAxiosError(404, notFound));
            await TypeTranId(user, MISSING_TRAN_ID);
            await PressFunctionKey(user, FETCH_KEY, "key");
            await ExpectAlert(TRAN_ID_NOT_FOUND_MESSAGE, "error");
        },
    },
    {
        action: "F4 Clear",
        messageText: "",
        Reach: (user) => PressFunctionKey(user, CLEAR_KEY, "key"),
    },
];

function RenderScreen(): UserEvent {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <TransactionViewPage />
        </ThemeProvider>,
    );
    return user;
}

// The page as next dev renders it, inside StrictMode (reactStrictMode in next.config.ts).
function RenderStrictScreen(): void {
    render(
        <StrictMode>
            <ThemeProvider theme={CARD_DEMO_THEME}>
                <TransactionViewPage />
            </ThemeProvider>
        </StrictMode>,
    );
}

// A non-blank tranId is a CT00 S selection, which COTRN01C displays on its first entry; the
// render settles once that record is shown.
async function RenderFrom(tranId: string): Promise<UserEvent> {
    mockSearchParams = tranId === "" ? new URLSearchParams() : new URLSearchParams({ tranId });
    const user = RenderScreen();
    if (tranId !== "") {
        await screen.findByDisplayValue(MASKED_CARD_NUMBER);
    }
    return user;
}

function TranIdInput(): HTMLInputElement {
    return screen.getByLabelText<HTMLInputElement>(TRAN_ID_LABEL);
}

function DetailInput(label: string): HTMLTextAreaElement {
    return screen.getByLabelText<HTMLTextAreaElement>(label);
}

function DetailValues(): string[] {
    return DETAIL_FIELD_CASES.map((field) => DetailInput(field.label).value);
}

const EMPTY_DETAIL_VALUES: readonly string[] = DETAIL_FIELD_CASES.map(() => "");

async function PressKey(user: UserEvent, keyName: string): Promise<void> {
    await user.keyboard(`{${keyName}}`);
}

// The legend button, or the physical key bound by useFunctionKeys.
async function PressFunctionKey(
    user: UserEvent,
    functionKey: FunctionKeyCase,
    mode: PressMode,
): Promise<void> {
    if (mode === "button") {
        await user.click(screen.getByRole("button", { name: functionKey.buttonName }));
        return;
    }
    await PressKey(user, functionKey.keyName);
}

async function TypeTranId(user: UserEvent, text: string): Promise<void> {
    await user.clear(TranIdInput());
    if (text !== "") {
        await user.type(TranIdInput(), text);
    }
}

// Renders the entry and leaves its read running: the arrival read, or ENTER on the typed key.
async function StartPendingRead(readCase: PendingReadCase): Promise<UserEvent> {
    mockViewTransaction.mockImplementation(PendUntilAborted);
    mockSearchParams = new URLSearchParams(
        readCase.queryTranId === "" ? {} : { tranId: readCase.queryTranId },
    );
    const user = RenderScreen();
    if (readCase.typedText !== "") {
        await TypeTranId(user, readCase.typedText);
        await PressFunctionKey(user, FETCH_KEY, "button");
    }
    await screen.findByRole("progressbar");
    return user;
}

// The ERRMSG line holds exactly the server's text, blanks included, in its severity colour.
async function ExpectAlert(text: string, severity: MessageSeverity): Promise<void> {
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(text);
    expect(alert).toHaveClass(SEVERITY_CLASSES[severity]);
}

// CT01 always renders exactly one role="status" region, its own polite loaded-status region:
// it has no INFOMSG line, and MessageLine shows ERRMSG as a role="alert" Alert only while the
// message is non-blank, so the ERRMSG text is "" when no alert is shown.
function StatusRegions(): ScreenStatus {
    const regions = screen.getAllByRole("status");
    expect(regions).toHaveLength(1);
    const [detailStatus] = regions as [HTMLElement];
    expect(detailStatus).toHaveAttribute("aria-live", "polite");
    const messageText = screen.queryByRole("alert")?.textContent ?? "";
    return { detailStatus, messageText };
}

// The next read stays pending until the test settles it with the returned function.
function DeferRead(): (detail: TransactionDetailResponse) => void {
    let resolveRead: (detail: TransactionDetailResponse) => void = () => undefined;
    mockViewTransaction.mockReturnValue(
        new Promise<TransactionDetailResponse>((resolve) => {
            resolveRead = resolve;
        }),
    );
    return resolveRead;
}

beforeEach(() => {
    mockSearchParams = new URLSearchParams();
    mockCurrentUser = SIGNED_ON_USER;
    // The reset drops any one-time answer a failed test left unconsumed.
    mockViewTransaction.mockReset();
    mockViewTransaction.mockResolvedValue(BuildTransactionDetail());
    window.sessionStorage.clear();
});

describe("COTRN01C BMS COTRN01 header and legend", () => {
    it("COTRN01C shows the CT01 header, the View Transaction title and the PF legend", () => {
        RenderScreen();
        const banner = screen.getByRole("banner");
        expect(within(banner).getByText("Tran: CT01")).toBeInTheDocument();
        expect(within(banner).getByText("Prog: COTRN01C")).toBeInTheDocument();
        expect(screen.getByRole("heading", { level: 1, name: "View Transaction" })).toBeVisible();
        const legend = screen.getByRole("group", { name: "Function keys" });
        const buttonNames = within(legend)
            .getAllByRole("button")
            .map((button) => button.textContent);
        expect(buttonNames).toEqual(LEGEND_BUTTON_NAMES);
    });
});

describe("COTRN01C BMS COTRN01 fields", () => {
    it("TRNIDIN is the empty 16-character Enter Tran ID: input holding the cursor", () => {
        RenderScreen();
        const input = TranIdInput();
        expect(input).toHaveAttribute("maxlength", "16");
        expect(input).not.toHaveAttribute("readonly");
        expect(input).toHaveValue("");
        expect(input).toHaveFocus();
        expect(mockViewTransaction).not.toHaveBeenCalled();
    });

    // A one-row text box wraps a value wider than the field onto further rows instead of clipping
    // it.
    it.each(DETAIL_FIELD_CASES)("$label is a protected field of LENGTH $maxLength", (field) => {
        RenderScreen();
        const input = DetailInput(field.label);
        expect(input).toBeInstanceOf(HTMLTextAreaElement);
        expect(input).toHaveAttribute("maxlength", String(field.maxLength));
        expect(input).toHaveAttribute("readonly");
        expect(input).toHaveAttribute("rows", "1");
        expect(input).toHaveValue("");
    });
});

// The browser sizes each native textarea to its value, so no hidden measuring copy of a field is
// rendered, and every textarea on the screen is a labelled field with its own id and no input type.
describe("COTRN01C BMS COTRN01 display-field elements", () => {
    it("COTRN01C renders one identified textarea per protected field and no hidden copy", () => {
        RenderScreen();
        const detailInputs = DETAIL_FIELD_CASES.map((field) => DetailInput(field.label));
        expect(Array.from(document.querySelectorAll("textarea"))).toEqual(detailInputs);
        expect(document.querySelectorAll("textarea[aria-hidden]")).toHaveLength(0);
        expect(detailInputs.filter((input) => input.hasAttribute("type"))).toEqual([]);
        const ids = detailInputs.map((input) => input.id);
        expect(ids).not.toContain("");
        expect(new Set(ids).size).toBe(DETAIL_FIELD_CASES.length);
    });
});

describe("COTRN01C first entry from a CT00 selection (/transactions/view?tranId)", () => {
    it("COTRN01C reads the selected transaction once and shows its key in TRNIDIN", async () => {
        await RenderFrom(TEST_TRAN_ID);
        expect(mockViewTransaction).toHaveBeenCalledTimes(1);
        expect(mockViewTransaction).toHaveBeenCalledWith(TEST_TRAN_ID, READ_SIGNAL_OPTIONS);
        expect(TranIdInput()).toHaveValue(TEST_TRAN_ID);
        expect(screen.queryByRole("alert")).toBeNull();
    });

    it.each(DETAIL_FIELD_CASES)("$label shows $shownValue as the server sent it", async (field) => {
        await RenderFrom(TEST_TRAN_ID);
        expect(DetailInput(field.label)).toHaveValue(field.shownValue);
    });
});

describe("COTRN01C first entry with an over-long tranId", () => {
    it.each(OVERLONG_QUERY_CASES)(
        "COTRN01C cuts a tranId of $shape to the TRNIDIN LENGTH before it reads",
        async (queryCase) => {
            await RenderFrom(queryCase.queryTranId);
            expect(mockViewTransaction).toHaveBeenCalledTimes(1);
            expect(mockViewTransaction).toHaveBeenCalledWith(
                queryCase.selectedKey,
                READ_SIGNAL_OPTIONS,
            );
            expect(TranIdInput()).toHaveValue(queryCase.selectedKey);
        },
    );
});

describe("COTRN01C masked card number and stored timestamp text", () => {
    it("Card Number shows the masked PAN and both dates keep their 26-character text", async () => {
        await RenderFrom(TEST_TRAN_ID);
        const cardNumber = DetailInput("Card Number:").value;
        expect(cardNumber).toMatch(/^\*{12}\d{4}$/);
        expect(cardNumber).not.toMatch(/\d{16}/);
        expect(DetailInput("Orig Date:").value).toHaveLength(TIMESTAMP_TEXT_LENGTH);
        expect(DetailInput("Proc Date:").value).toHaveLength(TIMESTAMP_TEXT_LENGTH);
    });
});

describe("COTRN01C ENTER (PROCESS-ENTER-KEY)", () => {
    it.each(PRESS_MODES)(
        "COTRN01C ENTER by %s reads TRANSACT with the key as typed",
        async (mode) => {
            const user = RenderScreen();
            await TypeTranId(user, TYPED_TRAN_ID);
            await PressFunctionKey(user, FETCH_KEY, mode);
            await screen.findByDisplayValue(MASKED_CARD_NUMBER);
            expect(mockViewTransaction).toHaveBeenCalledTimes(1);
            expect(mockViewTransaction).toHaveBeenCalledWith(TYPED_TRAN_ID, READ_SIGNAL_OPTIONS);
            expect(DetailValues()).toEqual(DETAIL_FIELD_CASES.map((field) => field.shownValue));
        },
    );
});

describe("COTRN01C legacy literals on ERRMSG", () => {
    it.each(LITERAL_CASES)("COTRN01C $status shows $shownText as $severity", async (literal) => {
        mockViewTransaction.mockRejectedValue(BuildAxiosError(literal.status, literal.body));
        const user = RenderScreen();
        await TypeTranId(user, literal.typedText);
        await PressFunctionKey(user, FETCH_KEY, "button");
        await ExpectAlert(literal.shownText, literal.severity);
        expect(mockViewTransaction).toHaveBeenCalledTimes(1);
        expect(mockViewTransaction).toHaveBeenCalledWith(literal.sentKey, READ_SIGNAL_OPTIONS);
        expect(DetailValues()).toEqual(EMPTY_DETAIL_VALUES);
    });
});

describe("COTRN01C failed read after a display", () => {
    it("COTRN01C blanks the transaction shown before a failed read", async () => {
        const user = await RenderFrom(TEST_TRAN_ID);
        const notFound = BuildErrorBody({ message: TRAN_ID_NOT_FOUND_MESSAGE });
        mockViewTransaction.mockRejectedValue(BuildAxiosError(404, notFound));
        await TypeTranId(user, MISSING_TRAN_ID);
        await PressFunctionKey(user, FETCH_KEY, "key");
        await ExpectAlert(TRAN_ID_NOT_FOUND_MESSAGE, "error");
        expect(DetailValues()).toEqual(EMPTY_DETAIL_VALUES);
        expect(TranIdInput()).toHaveValue(MISSING_TRAN_ID);
    });
});

describe("COTRN01C MOVE -1 TO TRNIDINL", () => {
    it("COTRN01C marks TRNIDIN invalid and returns the cursor to it", async () => {
        const emptyKey = BuildErrorBody({ message: TRAN_ID_EMPTY_MESSAGE });
        mockViewTransaction.mockRejectedValue(BuildAxiosError(400, emptyKey));
        const user = RenderScreen();
        expect(TranIdInput()).toHaveAttribute("aria-invalid", "false");
        await PressFunctionKey(user, FETCH_KEY, "button");
        await ExpectAlert(TRAN_ID_EMPTY_MESSAGE, "error");
        expect(TranIdInput()).toHaveAttribute("aria-invalid", "true");
        expect(TranIdInput()).toHaveFocus();
    });
});

// Nothing describes TRNIDIN before an answer; once one names it, the ERRMSG Alert does.
describe("COTRN01C TRNIDIN described by ERRMSG", () => {
    it.each(FLAGGED_LITERAL_CASES)(
        "COTRN01C $status describes TRNIDIN by the ERRMSG line showing $shownText",
        async (literal) => {
            mockViewTransaction.mockRejectedValue(BuildAxiosError(literal.status, literal.body));
            const user = RenderScreen();
            expect(TranIdInput()).not.toHaveAttribute("aria-describedby");
            await TypeTranId(user, literal.typedText);
            await PressFunctionKey(user, FETCH_KEY, "button");
            await ExpectAlert(literal.shownText, literal.severity);
            expect(TranIdInput()).toHaveAttribute("aria-describedby", ERRMSG_ID);
            expect(document.getElementById(ERRMSG_ID)).toBe(screen.getByRole("alert"));
            expect(TranIdInput()).toHaveAccessibleDescription(literal.shownText);
        },
    );
});

describe("COTRN01C F4 (CLEAR-CURRENT-SCREEN) after a display", () => {
    it.each(PRESS_MODES)("COTRN01C F4 by %s blanks the key and the display", async (mode) => {
        const user = await RenderFrom(TEST_TRAN_ID);
        await PressFunctionKey(user, CLEAR_KEY, mode);
        expect(TranIdInput()).toHaveValue("");
        expect(DetailValues()).toEqual(EMPTY_DETAIL_VALUES);
        expect(TranIdInput()).toHaveFocus();
        expect(mockViewTransaction).toHaveBeenCalledTimes(1);
    });
});

describe("COTRN01C F4 (CLEAR-CURRENT-SCREEN) after a failed read", () => {
    it.each(PRESS_MODES)("COTRN01C F4 by %s blanks the message line", async (mode) => {
        const notFound = BuildErrorBody({ message: TRAN_ID_NOT_FOUND_MESSAGE });
        mockViewTransaction.mockRejectedValue(BuildAxiosError(404, notFound));
        const user = RenderScreen();
        await TypeTranId(user, MISSING_TRAN_ID);
        await PressFunctionKey(user, FETCH_KEY, "button");
        await ExpectAlert(TRAN_ID_NOT_FOUND_MESSAGE, "error");
        await PressFunctionKey(user, CLEAR_KEY, mode);
        expect(screen.queryByRole("alert")).toBeNull();
        expect(TranIdInput()).toHaveAttribute("aria-invalid", "false");
        expect(mockViewTransaction).toHaveBeenCalledTimes(1);
    });
});

// COTRN01C WHEN OTHER: an AID outside ENTER, PF3, PF4 and PF5 sets ERR-FLG and sends
// CCDA-MSG-INVALID-KEY (cpy/CSMSG01Y.cpy); TRANSACT is not read, the key and the record shown
// stay, and the cursor returns to TRNIDIN, the IC field, which is not flagged.
const INVALID_KEY_MESSAGE = "Invalid key pressed. Please see below...";
const UNBOUND_KEYS: readonly string[] = ["F7", "F8", "F12"];

// A key the legend lacks, pressed with the cursor on a protected detail field; the event tells
// whether its browser default was blocked.
async function PressUnboundKey(user: UserEvent, keyName: string): Promise<KeyboardEvent> {
    const amountField = DetailInput("Amount:");
    await user.click(amountField);
    const keyEvent = new KeyboardEvent("keydown", {
        key: keyName,
        bubbles: true,
        cancelable: true,
    });
    act(() => {
        amountField.dispatchEvent(keyEvent);
    });
    return keyEvent;
}

describe("COTRN01C WHEN OTHER (invalid key)", () => {
    it.each(UNBOUND_KEYS)(
        "COTRN01C %s shows the invalid-key literal in red and keeps the record shown",
        async (keyName) => {
            const user = await RenderFrom(TEST_TRAN_ID);
            const keyEvent = await PressUnboundKey(user, keyName);
            await ExpectAlert(INVALID_KEY_MESSAGE, "error");
            expect(keyEvent.defaultPrevented).toBe(true);
            expect(mockViewTransaction).toHaveBeenCalledTimes(1);
            expect(TranIdInput()).toHaveValue(TEST_TRAN_ID);
            expect(DetailValues()).toEqual(DETAIL_FIELD_CASES.map((field) => field.shownValue));
            expect(TranIdInput()).toHaveAttribute("aria-invalid", "false");
            expect(TranIdInput()).toHaveFocus();
        },
    );
});

describe("COTRN01C F5 (XCTL to COTRN00C)", () => {
    it.each(PRESS_MODES)("COTRN01C F5 by %s browses to the transaction list", async (mode) => {
        const user = RenderScreen();
        await PressFunctionKey(user, BROWSE_KEY, mode);
        expect(mockRouterPush).toHaveBeenCalledTimes(1);
        expect(mockRouterPush).toHaveBeenCalledWith(TRANSACTION_LIST_ROUTE);
        expect(mockViewTransaction).not.toHaveBeenCalled();
    });
});

describe("COTRN01C F3 (RETURN-TO-PREV-SCREEN)", () => {
    it.each(BACK_CASES)("COTRN01C F3 by $mode from $origin goes to $route", async (backCase) => {
        const user = await RenderFrom(backCase.tranId);
        await PressFunctionKey(user, BACK_KEY, backCase.mode);
        expect(mockRouterPush).toHaveBeenCalledTimes(1);
        expect(mockRouterPush).toHaveBeenCalledWith(backCase.route);
        expect(mockRouterReplace).not.toHaveBeenCalled();
    });
});

describe("COTRN01C unauthenticated read", () => {
    it("COTRN01C sends a 401 on the read to sign-on without a message", async () => {
        const noSession = BuildErrorBody({ field_errors: [], cursor_field: "" });
        mockViewTransaction.mockRejectedValue(BuildAxiosError(401, noSession));
        const user = RenderScreen();
        await TypeTranId(user, TEST_TRAN_ID);
        await PressFunctionKey(user, FETCH_KEY, "button");
        await waitFor(() => expect(mockRouterReplace).toHaveBeenCalledWith(SIGN_ON_ROUTE));
        expect(mockRouterReplace).toHaveBeenCalledTimes(1);
        expect(mockRouterPush).not.toHaveBeenCalled();
        expect(screen.queryByRole("alert")).toBeNull();
    });
});

// ENTER stays locked while the read runs, so no second read replaces it; the abandoned read's
// CanceledError shows nothing on the screen still mounted.
describe("COTRN01C leaving the screen during a read", () => {
    it.each(PENDING_READ_CASES)(
        "COTRN01C $leaving during $origin abandons the read without a message",
        async (readCase) => {
            const user = await StartPendingRead(readCase);
            expect(screen.getByRole("button", { name: FETCH_KEY.buttonName })).toBeDisabled();
            expect(ReadSignal(0)?.aborted).toBe(false);
            await readCase.LeaveScreen(user);
            expect(ReadSignal(0)?.aborted).toBe(true);
            expect(mockRouterPush.mock.calls).toEqual(readCase.routes.map((route) => [route]));
            expect(screen.queryByRole("alert")).toBeNull();
            expect(mockViewTransaction).toHaveBeenCalledTimes(1);
        },
    );
});

// React StrictMode runs the arrival effect, its cleanup and the effect again in development: the
// simulated unmount abandons the first read, the second displays the selection, and an answer
// the first read still delivers afterwards is ignored.
describe("COTRN01C arrival read under StrictMode", () => {
    it("COTRN01C abandons the first arrival read and shows only the second", async () => {
        let answerFirstRead: (detail: TransactionDetailResponse) => void = () => undefined;
        mockViewTransaction
            .mockImplementationOnce(() => new Promise((resolve) => (answerFirstRead = resolve)))
            .mockResolvedValueOnce(BuildTransactionDetail());
        mockSearchParams = new URLSearchParams({ tranId: TEST_TRAN_ID });
        RenderStrictScreen();
        await screen.findByDisplayValue(MASKED_CARD_NUMBER);
        expect(mockViewTransaction).toHaveBeenCalledTimes(2);
        expect([ReadSignal(0)?.aborted, ReadSignal(1)?.aborted]).toEqual([true, false]);
        await act(async () => answerFirstRead(BuildTransactionDetail({ tran_desc: "Stale" })));
        expect(DetailValues()).toEqual(DETAIL_FIELD_CASES.map((field) => field.shownValue));
        expect(screen.getByRole("button", { name: FETCH_KEY.buttonName })).toBeEnabled();
    });
});

// A cancelled read is discarded before ToScreenFeedback, which would show the abend text.
describe("COTRN01C cancelled read", () => {
    it("COTRN01C shows no message for a read rejected as cancelled", async () => {
        mockViewTransaction.mockRejectedValue(new CanceledError());
        const user = RenderScreen();
        await TypeTranId(user, TEST_TRAN_ID);
        await PressFunctionKey(user, FETCH_KEY, "button");
        await waitFor(() =>
            expect(screen.getByRole("button", { name: FETCH_KEY.buttonName })).toBeEnabled(),
        );
        expect(mockViewTransaction).toHaveBeenCalledTimes(1);
        expect(screen.queryByRole("alert")).toBeNull();
        expect(mockRouterReplace).not.toHaveBeenCalled();
        expect(DetailValues()).toEqual(EMPTY_DETAIL_VALUES);
    });
});

describe("COTRN01C displayed-record status after a read", () => {
    it("COTRN01C announces a read record in its own status and leaves ERRMSG blank", async () => {
        const user = RenderScreen();
        expect(StatusRegions().detailStatus.textContent).toBe("");
        await TypeTranId(user, TEST_TRAN_ID);
        await PressFunctionKey(user, FETCH_KEY, "key");
        await screen.findByDisplayValue(MASKED_CARD_NUMBER);
        const { detailStatus, messageText } = StatusRegions();
        expect(detailStatus.textContent).toBe(DETAIL_SHOWN_STATUS);
        expect(messageText).toBe("");
        expect(screen.queryByRole("alert")).toBeNull();
        expect(TranIdInput()).toHaveFocus();
    });
});

describe("COTRN01C displayed-record status during a read", () => {
    it("COTRN01C empties the status during a pending read and refills it on arrival", async () => {
        const user = await RenderFrom(TEST_TRAN_ID);
        expect(StatusRegions().detailStatus.textContent).toBe(DETAIL_SHOWN_STATUS);
        const resolveRead = DeferRead();
        await PressFunctionKey(user, FETCH_KEY, "button");
        expect(StatusRegions().detailStatus.textContent).toBe("");
        expect(DetailValues()).toEqual(EMPTY_DETAIL_VALUES);
        resolveRead(BuildTransactionDetail());
        await screen.findByDisplayValue(MASKED_CARD_NUMBER);
        expect(StatusRegions().detailStatus.textContent).toBe(DETAIL_SHOWN_STATUS);
        expect(TranIdInput()).toHaveFocus();
    });
});

describe("COTRN01C displayed-record status once the record leaves the screen", () => {
    it.each(EMPTIED_STATUS_CASES)("COTRN01C empties the status after $action", async (status) => {
        const user = await RenderFrom(TEST_TRAN_ID);
        await status.Reach(user);
        const { detailStatus, messageText } = StatusRegions();
        expect(detailStatus.textContent).toBe("");
        expect(messageText).toBe(status.messageText);
        expect(DetailValues()).toEqual(EMPTY_DETAIL_VALUES);
    });
});
