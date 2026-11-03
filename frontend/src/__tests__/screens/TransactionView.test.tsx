// Screen tests: BMS COTRN01, program COTRN01C (CT01), page /transactions/view.
// Labels and LENGTHs come from bms/COTRN01.bms; the literals are copied verbatim from
// backend/app/services/transactions/cotrn01c.py, which ports cbl/COTRN01C.cbl lines 149, 285
// and 292. The API module is mocked, so each response body is the one the backend sends.
import { ThemeProvider } from "@mui/material/styles";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import {
    AxiosError,
    AxiosHeaders,
    type AxiosResponse,
    type InternalAxiosRequestConfig,
} from "axios";
import type { ReactNode } from "react";

import { CARD_DEMO_THEME } from "@/app/theme";
import TransactionViewPage from "@/app/transactions/view/page";
import { ViewTransaction, type TransactionDetailResponse } from "@/lib/api/transactions";
import type { LegacyErrorResponse, MessageSeverity } from "@/lib/apiClient";
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

interface StatusRegionPair {
    detailStatus: HTMLElement;
    messageLine: HTMLElement;
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

// F3 returns to CDEMO-FROM-PROGRAM: COTRN00C after a CT00 selection, otherwise COMEN01C.
const BACK_CASES: readonly BackCase[] = PRESS_MODES.flatMap((mode) => [
    { mode, origin: "a CT00 selection", tranId: TEST_TRAN_ID, route: TRANSACTION_LIST_ROUTE },
    { mode, origin: "the main menu", tranId: "", route: MAIN_MENU_ROUTE },
]);

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

function DetailInput(label: string): HTMLInputElement {
    return screen.getByLabelText<HTMLInputElement>(label);
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

// The ERRMSG line holds exactly the server's text, blanks included, in its severity colour.
async function ExpectAlert(text: string, severity: MessageSeverity): Promise<void> {
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(text);
    expect(alert).toHaveClass(SEVERITY_CLASSES[severity]);
}

// CT01 always renders two polite role="status" regions: its own loaded-status region, then the
// MessageLine's below the display fields.
function StatusRegions(): StatusRegionPair {
    const regions = screen.getAllByRole("status");
    expect(regions).toHaveLength(2);
    regions.forEach((region) => expect(region).toHaveAttribute("aria-live", "polite"));
    const [detailStatus, messageLine] = regions as [HTMLElement, HTMLElement];
    return { detailStatus, messageLine };
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

    it.each(DETAIL_FIELD_CASES)("$label is a protected field of LENGTH $maxLength", (field) => {
        RenderScreen();
        const input = DetailInput(field.label);
        expect(input).toHaveAttribute("maxlength", String(field.maxLength));
        expect(input).toHaveAttribute("readonly");
        expect(input).toHaveValue("");
    });
});

describe("COTRN01C first entry from a CT00 selection (/transactions/view?tranId)", () => {
    it("COTRN01C reads the selected transaction once and shows its key in TRNIDIN", async () => {
        await RenderFrom(TEST_TRAN_ID);
        expect(mockViewTransaction).toHaveBeenCalledTimes(1);
        expect(mockViewTransaction).toHaveBeenCalledWith(TEST_TRAN_ID);
        expect(TranIdInput()).toHaveValue(TEST_TRAN_ID);
        expect(screen.queryByRole("alert")).toBeNull();
    });

    it.each(DETAIL_FIELD_CASES)("$label shows $shownValue as the server sent it", async (field) => {
        await RenderFrom(TEST_TRAN_ID);
        expect(DetailInput(field.label)).toHaveValue(field.shownValue);
    });
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
            expect(mockViewTransaction).toHaveBeenCalledWith(TYPED_TRAN_ID);
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
        expect(mockViewTransaction).toHaveBeenCalledWith(literal.sentKey);
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

describe("COTRN01C displayed-record status after a read", () => {
    it("COTRN01C announces a read record in its own status and leaves ERRMSG blank", async () => {
        const user = RenderScreen();
        expect(StatusRegions().detailStatus.textContent).toBe("");
        await TypeTranId(user, TEST_TRAN_ID);
        await PressFunctionKey(user, FETCH_KEY, "key");
        await screen.findByDisplayValue(MASKED_CARD_NUMBER);
        const { detailStatus, messageLine } = StatusRegions();
        expect(detailStatus.textContent).toBe(DETAIL_SHOWN_STATUS);
        expect(messageLine.textContent).toBe("");
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
        const { detailStatus, messageLine } = StatusRegions();
        expect(detailStatus.textContent).toBe("");
        expect(messageLine.textContent).toBe(status.messageText);
        expect(DetailValues()).toEqual(EMPTY_DETAIL_VALUES);
    });
});
