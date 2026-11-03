// Screen tests: BMS COCRDSL, program COCRDSLC (CCDL), page /cards/view.
// Labels and lengths come from bms/COCRDSL.bms; every literal is copied verbatim from
// backend/app/services/cards/cocrdslc.py, which ports cbl/COCRDSLC.cbl lines 639-760.
import { ThemeProvider } from "@mui/material/styles";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
    AxiosError,
    AxiosHeaders,
    type AxiosResponse,
    type InternalAxiosRequestConfig,
} from "axios";
import type { ReactNode } from "react";

import CardViewPage from "@/app/cards/view/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import { ViewCard, type CardDetailResponse } from "@/lib/api/cards";
import {
    ABEND_MESSAGE,
    SIGN_ON_ROUTE,
    type LegacyErrorResponse,
    type MessageSeverity,
} from "@/lib/apiClient";
import type { SessionContextValue } from "@/lib/session";

type SignedOnUser = NonNullable<SessionContextValue["currentUser"]>;
type ScreenUser = ReturnType<typeof userEvent.setup>;
type ScreenAction = (user: ScreenUser) => Promise<void>;

interface FieldErrorCase {
    body: LegacyErrorResponse;
    cursorLabel: string;
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
    FetchCardForEdit: jest.fn(),
    UpdateCard: jest.fn(),
}));

const mockViewCard = jest.mocked(ViewCard);

// Test keys only: the card digits are built at runtime, so no card number is written in code.
const TEST_ACCOUNT_ID = "00000000004";
const TEST_CARD_REF = "card-ref-1";
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

// F3 returns to the calling program: COCRDLIC after a list selection, otherwise the main menu.
const CARD_LIST_ROUTE = "/cards";
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

const HTTP_EDIT_FAILURE = 400;
const HTTP_UNAUTHORIZED = 401;
const HTTP_NOT_FOUND = 404;
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

// The ENTER and F3 legend actions, each by its button and by its physical key.
const SEARCH_ACTIONS: readonly [string, ScreenAction][] = [
    [SEARCH_BUTTON_NAME, (user) => ClickButton(user, SEARCH_BUTTON_NAME)],
    ["the Enter key", (user) => PressKey(user, "Enter")],
];
const EXIT_ACTIONS: readonly [string, ScreenAction][] = [
    [EXIT_BUTTON_NAME, (user) => ClickButton(user, EXIT_BUTTON_NAME)],
    ["the F3 key", (user) => PressKey(user, "F3")],
];

// The default normalizer trims and collapses the blanks the COCRDSL labels and literals keep.
function KeepRawText(text: string): string {
    return text;
}

function InputFor(label: string): HTMLElement {
    return screen.getByLabelText(label, { normalizer: KeepRawText });
}

function RenderScreen(): ScreenUser {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <CardViewPage />
        </ThemeProvider>,
    );
    return user;
}

// A COCRDLIC selection opens the page with the card_ref token and the account as its query.
function RenderFromCardList(): ScreenUser {
    mockSearchParams = new URLSearchParams({ cardRef: TEST_CARD_REF, acctId: TEST_ACCOUNT_ID });
    mockViewCard.mockResolvedValue(BuildCardDetail());
    return RenderScreen();
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

// Waits for the alert holding the exact raw text and checks its message-line colour.
async function ExpectAlert(text: string, severity: MessageSeverity): Promise<void> {
    const textElement = await screen.findByText(text, { normalizer: KeepRawText });
    const alert = textElement.closest('[role="alert"]');
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

// A key named in field_errors is shown in error (aria-invalid); the other key is not.
function ExpectInvalidKeys(fieldErrors: readonly string[]): void {
    for (const [fieldName, label] of KEY_FIELD_LABELS) {
        const isInvalid = String(fieldErrors.includes(fieldName));
        expect(InputFor(label)).toHaveAttribute("aria-invalid", isInvalid);
    }
}

// The lookup is over once the busy bar is gone: its answer and the busy flag arrive together.
async function WaitForLookupEnd(): Promise<void> {
    await waitFor(() => {
        expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    });
}

beforeEach(() => {
    jest.useFakeTimers(CLOCK_ONLY_FAKE_TIMERS);
    mockSearchParams = new URLSearchParams();
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
        expect(within(expiryGroup).getByText("/")).toBeInTheDocument();
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

    it("COCRDSLC places the cursor on ACCTSID (IC) and reads nothing before ENTER", () => {
        RenderScreen();
        expect(InputFor(ACCOUNT_LABEL)).toHaveFocus();
        expect(mockViewCard).not.toHaveBeenCalled();
        expect(screen.queryAllByRole("alert")).toHaveLength(0);
    });
});

describe("COCRDSLC entered from the COCRDLIC card list on /cards/view", () => {
    it("COCRDSLC reads the selected card once on mount by card_ref and account", async () => {
        RenderFromCardList();
        await ExpectAlert(DETAILS_SHOWN_INFO, "info");
        expect(mockViewCard).toHaveBeenCalledTimes(1);
        expect(mockViewCard).toHaveBeenCalledWith(TEST_CARD_REF, TEST_ACCOUNT_ID);
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
            await waitFor(() => expect(mockViewCard).toHaveBeenCalledTimes(2));
            await ExpectAlert(DETAILS_SHOWN_INFO, "info");
            expect(mockViewCard).toHaveBeenLastCalledWith(TEST_CARD_REF, TEST_ACCOUNT_ID);
        },
    );
});

describe("COCRDSLC F3 after a COCRDLIC selection on /cards/view", () => {
    it.each(EXIT_ACTIONS)(
        "COCRDSLC F3 by %s returns to the card list /cards",
        async (_, action) => {
            const user = RenderFromCardList();
            await ExpectAlert(DETAILS_SHOWN_INFO, "info");
            await action(user);
            expect(mockRouterPush).toHaveBeenCalledTimes(1);
            expect(mockRouterPush).toHaveBeenCalledWith(CARD_LIST_ROUTE);
        },
    );
});

describe("COCRDSLC PF keys from the main menu on /cards/view", () => {
    it.each(SEARCH_ACTIONS)("COCRDSLC ENTER by %s sends both keys as typed", async (_, action) => {
        mockViewCard.mockResolvedValue(BuildCardDetail());
        const user = RenderScreen();
        await TypeKeys(user, TEST_ACCOUNT_ID, TYPED_CARD_NUMBER);
        await action(user);
        await ExpectAlert(DETAILS_SHOWN_INFO, "info");
        expect(mockViewCard).toHaveBeenCalledTimes(1);
        expect(mockViewCard).toHaveBeenCalledWith(TYPED_CARD_NUMBER, TEST_ACCOUNT_ID);
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

describe("COCRDSLC key edits stay on the server on /cards/view", () => {
    it("COCRDSLC ENTER sends short, blank-padded keys untrimmed to 2200-EDIT", async () => {
        const body = BuildErrorBody({ message: ACCOUNT_FILTER_MESSAGE });
        mockViewCard.mockRejectedValue(BuildAxiosError(HTTP_EDIT_FAILURE, body));
        const user = RenderScreen();
        await TypeKeys(user, SHORT_ACCOUNT_TEXT, SHORT_CARD_TEXT);
        await ClickButton(user, SEARCH_BUTTON_NAME);
        await ExpectAlert(ACCOUNT_FILTER_MESSAGE, "error");
        expect(mockViewCard).toHaveBeenCalledWith(SHORT_CARD_TEXT, SHORT_ACCOUNT_TEXT);
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
        const body = { detail: "Internal Server Error" };
        mockViewCard.mockRejectedValue(BuildAxiosError(HTTP_INTERNAL_SERVER_ERROR, body));
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
        mockViewCard.mockRejectedValue(BuildAxiosError(HTTP_NOT_FOUND, body));
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
