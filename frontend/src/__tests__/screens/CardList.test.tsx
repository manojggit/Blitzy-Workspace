// Screen tests: BMS COCRDLI, program COCRDLIC (CCLI), page /cards.
// The literals are copied verbatim from backend/app/services/cards/cocrdlic.py, which ports
// cbl/COCRDLIC.cbl lines 903, 908, 1022, 1058, 1086, 1112, 1219, 1239 and 1244.
import { ThemeProvider } from "@mui/material/styles";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import type { ReactNode } from "react";

import CardListPage from "@/app/cards/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import { SelectionLabelFor } from "@/components/SelectionTable";
import {
    ListCards,
    SelectCards,
    type CardListQuery,
    type CardListResponse,
    type CardListRow,
    type CardSelectionAction,
    type CardSelectionRequest,
    type CardSelectionResponse,
} from "@/lib/api/cards";
import {
    Direction,
    SIGN_ON_ROUTE,
    type LegacyErrorResponse,
    type MessageSeverity,
} from "@/lib/apiClient";
import type { SessionContextValue } from "@/lib/session";

type UserSession = ReturnType<typeof userEvent.setup>;
type ScreenKey = "Enter" | "F3" | "F7" | "F8";
type KeyTrigger = "click" | "key";
type SignedInUser = NonNullable<SessionContextValue["currentUser"]>;

const mockRouterPush = jest.fn();
const mockRouterReplace = jest.fn();
// One router object for every render, as the Next.js App Router returns a stable instance.
const mockRouter = {
    push: mockRouterPush,
    replace: mockRouterReplace,
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};
let mockSearchParams = new URLSearchParams();

// A regular user: COSGN00C sends every user type other than A to the main menu that owns CCLI.
const mockCurrentUser: SignedInUser = {
    user_id: "TESTU001",
    user_type: "U",
    first_name: "Test",
    last_name: "Regular",
    next_route: "/menu",
};

jest.mock("next/navigation", () => ({
    useRouter: () => mockRouter,
    useSearchParams: () => mockSearchParams,
    usePathname: () => "/cards",
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

jest.mock("@/lib/api/cards", () => ({
    ...jest.requireActual<object>("@/lib/api/cards"),
    ListCards: jest.fn(),
    SelectCards: jest.fn(),
    ViewCard: jest.fn(),
    FetchCardForEdit: jest.fn(),
    UpdateCard: jest.fn(),
}));

const mockListCards = jest.mocked(ListCards);
const mockSelectCards = jest.mocked(SelectCards);

const PAN_PATTERN = /\d{16}/;
const ACCOUNT_FILTER_MESSAGE = "ACCOUNT FILTER,IF SUPPLIED MUST BE A 11 DIGIT NUMBER";
const CARD_FILTER_MESSAGE = "CARD ID FILTER,IF SUPPLIED MUST BE A 16 DIGIT NUMBER";
const MULTI_SELECT_MESSAGE = "PLEASE SELECT ONLY ONE RECORD TO VIEW OR UPDATE";
const INVALID_ACTION_MESSAGE = "INVALID ACTION CODE";
const NO_PREVIOUS_MESSAGE = "NO PREVIOUS PAGES TO DISPLAY";
const NO_MORE_PAGES_MESSAGE = "NO MORE PAGES TO DISPLAY";
const NO_RECORDS_MESSAGE = "NO RECORDS FOUND FOR THIS SEARCH CONDITION.";
// The COCRDLIC information line of every shown page (88 WS-INFORM-REC-ACTIONS).
const RECORD_ACTIONS_INFO = "TYPE S FOR DETAIL, U TO UPDATE ANY RECORD";

// The BMS INITIAL texts of COCRDLI as the page renders them.
const SCREEN_TITLE = "List Credit Cards";
const ACCOUNT_FILTER_LABEL = "Account Number    :";
const CARD_FILTER_LABEL = "Credit Card Number:";
const COLUMN_HEADERS = ["Select", "Account Number", "Card Number", "Active"];
// ACCTSID LENGTH=11 and CARDSID LENGTH=16.
const ACCOUNT_FILTER_MAX_LENGTH = "11";
const CARD_FILTER_MAX_LENGTH = "16";
// WS-MAX-SCREEN-LINES (cbl/COCRDLIC.cbl lines 177-178).
const PAGE_SIZE = 7;
// The blank option and 88 SELECT-OK VALUES 'S', 'U'.
const OFFERED_CODES = ["", "S", "U"];

const ACCT_ID_FIELD = "acct_id";
const CARD_NUM_FIELD = "card_num";
const FIRST_KEY = "first-key-token";
const LAST_KEY = "last-key-token";
const SHOWN_PAGE = 2;
const SELECTED_ROW = 3;
const SELECTED_CARD_REF = "card-ref-3";
const SELECTED_ACCT_ID = "00000000003";
const MENU_ROUTE = "/menu";
const URL_BASE = "http://127.0.0.1";

const HTTP_OK = 200;
const HTTP_BAD_REQUEST = 400;
const HTTP_UNAUTHORIZED = 401;

const KEY_BUTTON_NAMES: Readonly<Record<ScreenKey, string>> = {
    Enter: "Continue (Enter)",
    F3: "Exit (F3)",
    F7: "Backward (F7)",
    F8: "Forward (F8)",
};

// The MUI Alert root class of each severity (alertClasses colorError, colorSuccess, colorInfo).
const SEVERITY_CLASSES: Readonly<Record<MessageSeverity, string>> = {
    error: "MuiAlert-colorError",
    success: "MuiAlert-colorSuccess",
    info: "MuiAlert-colorInfo",
};

const KEY_TRIGGERS: readonly KeyTrigger[] = ["click", "key"];

// A refused selection redisplays rows and page keys beside the legacy feedback.
interface CardListErrorBody extends LegacyErrorResponse {
    rows?: CardListRow[];
    first_key?: string;
    last_key?: string;
}

// Row n of the shown page: its card_ref token, an 11-digit account and a masked card number.
function BuildListRow(rowNumber: number): CardListRow {
    const rowDigits = String(rowNumber).padStart(2, "0");
    return {
        card_ref: `card-ref-${rowNumber}`,
        acct_id: `0000000000${rowNumber}`,
        card_num_masked: `************43${rowDigits}`,
        active_status: "Y",
    };
}

function BuildListRows(rowCount: number): CardListRow[] {
    return Array.from({ length: rowCount }, (_unused, index) => BuildListRow(index + 1));
}

function BuildListResponse(overrides: Partial<CardListResponse> = {}): CardListResponse {
    return {
        rows: BuildListRows(PAGE_SIZE),
        page: SHOWN_PAGE,
        first_key: FIRST_KEY,
        last_key: LAST_KEY,
        message: "",
        info_message: RECORD_ACTIONS_INFO,
        field_errors: [],
        cursor_field: ACCT_ID_FIELD,
        ...overrides,
    };
}

function BuildSelectionResponse(action: CardSelectionAction): CardSelectionResponse {
    return {
        action,
        card_ref: SELECTED_CARD_REF,
        message: "",
        info_message: "",
        field_errors: [],
        cursor_field: null,
    };
}

function BuildErrorBody(overrides: Partial<CardListErrorBody> = {}): CardListErrorBody {
    return { message: "", info_message: "", field_errors: [], cursor_field: null, ...overrides };
}

function BuildAxiosError(status: number, body: CardListErrorBody): AxiosError {
    const config = { headers: new AxiosHeaders() };
    const response: AxiosResponse<CardListErrorBody> = {
        data: body,
        status,
        statusText: "",
        headers: {},
        config,
    };
    const message = `Request failed with status code ${status}`;
    return new AxiosError(message, AxiosError.ERR_BAD_REQUEST, config, null, response);
}

// The keys are disabled and the progress bar shown while a call runs.
async function WaitForIdle(): Promise<void> {
    await waitFor(() => {
        expect(screen.queryByRole("progressbar", { name: "Processing" })).toBeNull();
    });
}

async function RenderScreen(): Promise<UserSession> {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <CardListPage />
        </ThemeProvider>,
    );
    await WaitForIdle();
    return user;
}

// The filter whose label renders exactly as the BMS INITIAL text, padding blanks included.
function FilterInput(label: string): HTMLInputElement {
    const input = screen
        .getAllByRole<HTMLInputElement>("textbox")
        .find((candidate) => candidate.labels?.[0]?.textContent === label);
    if (input === undefined) {
        throw new Error(`No filter field is labelled "${label}"`);
    }
    return input;
}

function SelectionField(rowNumber: number): HTMLElement {
    return screen.getByRole("combobox", { name: SelectionLabelFor(rowNumber) });
}

function BodyRows(): HTMLElement[] {
    return screen.getAllByRole("row").slice(1);
}

function ColumnHeaderTexts(): (string | null)[] {
    return screen.getAllByRole("columnheader").map((header) => header.textContent);
}

// The cells after the selection field: account number, card number, active status.
function CardNumbersShown(): (string | null | undefined)[] {
    return BodyRows().map((row) => within(row).getAllByRole("cell")[2]?.textContent);
}

// The input root carries MUI's error state, as the map colours a flagged CRDSELn field red.
function FlaggedRowNumbers(): number[] {
    return BodyRows()
        .map((_row, index) => index + 1)
        .filter((rowNumber) =>
            SelectionField(rowNumber)
                .closest(".MuiInputBase-root")
                ?.classList.contains("Mui-error"),
        );
}

function AlertWithText(text: string): HTMLElement | undefined {
    return screen.queryAllByRole("alert").find((alert) => alert.textContent === text);
}

// The text is compared raw, so the server literal must arrive unchanged, blanks included.
async function ExpectAlert(text: string, severity: MessageSeverity): Promise<void> {
    await waitFor(() => {
        expect(AlertWithText(text)).toBeDefined();
    });
    expect(AlertWithText(text)).toHaveClass(SEVERITY_CLASSES[severity]);
}

async function PressKey(user: UserSession, key: ScreenKey): Promise<void> {
    await user.keyboard(`{${key}}`);
}

// ENTER from the keyboard needs the focus outside the Select fields, which act on Enter
// themselves, so the keyboard path presses its key from the account filter.
async function PressScreenKey(user: UserSession, key: ScreenKey, trigger: KeyTrigger) {
    if (trigger === "click") {
        await user.click(screen.getByRole("button", { name: KEY_BUTTON_NAMES[key] }));
    } else {
        await user.click(FilterInput(ACCOUNT_FILTER_LABEL));
        await PressKey(user, key);
    }
    await WaitForIdle();
}

async function WaitForListboxClosed(): Promise<void> {
    await waitFor(() => {
        expect(screen.queryByRole("listbox")).toBeNull();
    });
}

async function ChooseSelection(user: UserSession, rowNumber: number, code: string) {
    await user.click(SelectionField(rowNumber));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: code }));
    await WaitForListboxClosed();
}

async function OfferedCodes(user: UserSession, rowNumber: number): Promise<(string | null)[]> {
    await user.click(SelectionField(rowNumber));
    const options = within(screen.getByRole("listbox")).getAllByRole("option");
    const codes = options.map((option) => option.getAttribute("data-value"));
    await user.keyboard("{Escape}");
    await WaitForListboxClosed();
    return codes;
}

function RouterArgumentsWithPan(): string[] {
    const routerCalls = [...mockRouterPush.mock.calls, ...mockRouterReplace.mock.calls];
    return routerCalls
        .flat()
        .map(String)
        .filter((argument) => PAN_PATTERN.test(argument));
}

// The card travels only as its card_ref token, beside the selected row's account number.
function ExpectCardRoute(route: string): void {
    expect(mockRouterPush).toHaveBeenCalledTimes(1);
    const pushedUrl = new URL(String(mockRouterPush.mock.calls[0]?.[0]), URL_BASE);
    expect(pushedUrl.pathname).toBe(route);
    expect([...pushedUrl.searchParams.keys()]).toEqual(["cardRef", "acctId"]);
    expect(pushedUrl.searchParams.get("cardRef")).toBe(SELECTED_CARD_REF);
    expect(pushedUrl.searchParams.get("acctId")).toBe(SELECTED_ACCT_ID);
}

// Every row shown is sent with its code, blank for the rows the user left untouched.
function ExpectedSelectionRequest(code: string): CardSelectionRequest {
    return {
        acct_id: "",
        card_num: "",
        rows: BuildListRows(PAGE_SIZE).map((row, index) => ({
            card_ref: row.card_ref,
            selection: index + 1 === SELECTED_ROW ? code : "",
        })),
    };
}

beforeEach(() => {
    mockListCards.mockReset();
    mockSelectCards.mockReset();
    mockListCards.mockResolvedValue(BuildListResponse());
    mockSearchParams = new URLSearchParams();
    window.sessionStorage.clear();
});

// The header of every map: TRNNAME, PGMNAME, the row-4 title and PAGENO.
function ExpectScreenHeader(): void {
    expect(screen.getByRole("heading", { level: 1, name: SCREEN_TITLE })).toBeVisible();
    expect(screen.getByText("Tran: CCLI")).toBeVisible();
    expect(screen.getByText("Prog: COCRDLIC")).toBeVisible();
    expect(screen.getByText(`Page ${SHOWN_PAGE}`)).toBeVisible();
}

function ExpectFilterFields(): void {
    const accountFilter = FilterInput(ACCOUNT_FILTER_LABEL);
    expect(accountFilter).toHaveAttribute("maxlength", ACCOUNT_FILTER_MAX_LENGTH);
    expect(FilterInput(CARD_FILTER_LABEL)).toHaveAttribute("maxlength", CARD_FILTER_MAX_LENGTH);
}

// Only masked card numbers are shown, and no 16-digit run appears anywhere on the page.
function ExpectMaskedCardRows(): void {
    const maskedNumbers = BuildListRows(PAGE_SIZE).map((row) => row.card_num_masked);
    expect(ColumnHeaderTexts()).toEqual(COLUMN_HEADERS);
    expect(CardNumbersShown()).toEqual(maskedNumbers);
    expect(document.body.textContent).not.toMatch(PAN_PATTERN);
}

describe("COCRDLIC BMS COCRDLI content on /cards", () => {
    it("COCRDLIC first entry shows the title, filters, headers and 7 masked cards", async () => {
        await RenderScreen();
        expect(mockListCards).toHaveBeenCalledTimes(1);
        expect(mockListCards).toHaveBeenCalledWith({});
        ExpectScreenHeader();
        ExpectFilterFields();
        ExpectMaskedCardRows();
        await ExpectAlert(RECORD_ACTIONS_INFO, "info");
    });
});

describe("COCRDLIC CRDSEL1-7 codes on /cards", () => {
    it("COCRDLIC every row offers only blank, S and U", async () => {
        const user = await RenderScreen();
        expect(BodyRows()).toHaveLength(PAGE_SIZE);
        for (let rowNumber = 1; rowNumber <= PAGE_SIZE; rowNumber += 1) {
            expect(await OfferedCodes(user, rowNumber)).toEqual(OFFERED_CODES);
        }
    });
});

interface SelectionRouteCase {
    code: string;
    action: CardSelectionAction;
    route: string;
    trigger: KeyTrigger;
}

const SELECTION_ROUTES: readonly Omit<SelectionRouteCase, "trigger">[] = [
    { code: "S", action: "view", route: "/cards/view" },
    { code: "U", action: "update", route: "/cards/update" },
];

const SELECTION_ROUTE_CASES: readonly SelectionRouteCase[] = SELECTION_ROUTES.flatMap((routeCase) =>
    KEY_TRIGGERS.map((trigger) => ({ ...routeCase, trigger })),
);

describe("COCRDLIC selection on /cards", () => {
    it.each(SELECTION_ROUTE_CASES)(
        "COCRDLIC selection $code with ENTER by $trigger routes to $route with cardRef only",
        async ({ code, action, route, trigger }) => {
            mockSelectCards.mockResolvedValueOnce(BuildSelectionResponse(action));
            const user = await RenderScreen();
            await ChooseSelection(user, SELECTED_ROW, code);
            await PressScreenKey(user, "Enter", trigger);
            expect(mockSelectCards).toHaveBeenCalledTimes(1);
            expect(mockSelectCards).toHaveBeenCalledWith(ExpectedSelectionRequest(code));
            expect(mockListCards).toHaveBeenCalledTimes(1);
            ExpectCardRoute(route);
            expect(mockRouterReplace).not.toHaveBeenCalled();
            expect(RouterArgumentsWithPan()).toEqual([]);
        },
    );
});

interface PagingCase {
    key: ScreenKey;
    readName: string;
    startKey: string;
    direction?: Direction;
    trigger: KeyTrigger;
}

// ENTER re-reads the shown page from first_key; F7 and F8 page from first_key and last_key.
const PAGING_KEYS: readonly Omit<PagingCase, "trigger">[] = [
    { key: "F8", readName: "forward", startKey: LAST_KEY, direction: Direction.FORWARD },
    { key: "F7", readName: "backward", startKey: FIRST_KEY, direction: Direction.BACKWARD },
    { key: "Enter", readName: "re-read", startKey: FIRST_KEY },
];

const PAGING_CASES: readonly PagingCase[] = PAGING_KEYS.flatMap((pagingKey) =>
    KEY_TRIGGERS.map((trigger) => ({ ...pagingKey, trigger })),
);

describe("COCRDLIC paging on /cards", () => {
    it.each(PAGING_CASES)(
        "COCRDLIC $key by $trigger sends the $readName query with $startKey",
        async ({ key, startKey, direction, trigger }) => {
            const user = await RenderScreen();
            await PressScreenKey(user, key, trigger);
            const expectedQuery: CardListQuery = {
                acctId: "",
                cardNum: "",
                startKey,
                direction,
                page: SHOWN_PAGE,
            };
            expect(mockListCards).toHaveBeenCalledTimes(2);
            expect(mockListCards).toHaveBeenLastCalledWith(expectedQuery);
            expect(mockSelectCards).not.toHaveBeenCalled();
        },
    );
});

interface ListLiteralCase {
    literal: string;
    status: number;
    key: ScreenKey;
    severity: MessageSeverity;
    rowCount: number;
}

// COCRDLIC never changes the colour of ERRMSG (COLOR=RED), so every literal it sets is red, the
// paging literals that arrive on a 200 included.
const ERRMSG_SEVERITY: MessageSeverity = "error";

// A refused filter keeps the rows shown; NO RECORDS FOUND arrives with an empty page.
const LIST_LITERALS: readonly Omit<ListLiteralCase, "severity">[] = [
    {
        literal: ACCOUNT_FILTER_MESSAGE,
        status: HTTP_BAD_REQUEST,
        key: "Enter",
        rowCount: PAGE_SIZE,
    },
    { literal: CARD_FILTER_MESSAGE, status: HTTP_BAD_REQUEST, key: "Enter", rowCount: PAGE_SIZE },
    { literal: NO_PREVIOUS_MESSAGE, status: HTTP_OK, key: "F7", rowCount: PAGE_SIZE },
    { literal: NO_MORE_PAGES_MESSAGE, status: HTTP_OK, key: "F8", rowCount: PAGE_SIZE },
    { literal: NO_RECORDS_MESSAGE, status: HTTP_OK, key: "Enter", rowCount: 0 },
];

const LIST_LITERAL_CASES: readonly ListLiteralCase[] = LIST_LITERALS.map((literalCase) => ({
    ...literalCase,
    severity: ERRMSG_SEVERITY,
}));

function SettleNextListCall({ literal, status, rowCount }: ListLiteralCase): void {
    if (status === HTTP_OK) {
        const rows = BuildListRows(rowCount);
        mockListCards.mockResolvedValueOnce(
            BuildListResponse({ message: literal, info_message: "", rows }),
        );
        return;
    }
    const body = BuildErrorBody({ message: literal, cursor_field: ACCT_ID_FIELD });
    mockListCards.mockRejectedValueOnce(BuildAxiosError(status, body));
}

describe("COCRDLIC list literals on /cards", () => {
    it.each(LIST_LITERAL_CASES)(
        "COCRDLIC $key shows $literal as $severity on HTTP $status",
        async (literalCase) => {
            const user = await RenderScreen();
            SettleNextListCall(literalCase);
            await PressScreenKey(user, literalCase.key, "click");
            await ExpectAlert(literalCase.literal, literalCase.severity);
            expect(BodyRows()).toHaveLength(literalCase.rowCount);
            expect(mockRouterPush).not.toHaveBeenCalled();
        },
    );
});

interface SelectionLiteralCase {
    literal: string;
    selections: readonly (readonly [number, string])[];
    flaggedRows: readonly number[];
    cursorRow: number;
}

// The server alone edits the codes (2250-EDIT-ARRAY): its 400 names each flagged row as
// rows[<0-based index>].selection, puts the cursor on the first of them and redisplays the rows.
const SELECTION_LITERAL_CASES: readonly SelectionLiteralCase[] = [
    {
        literal: MULTI_SELECT_MESSAGE,
        selections: [
            [2, "S"],
            [4, "U"],
        ],
        flaggedRows: [2, 4],
        cursorRow: 2,
    },
    { literal: INVALID_ACTION_MESSAGE, selections: [[5, "S"]], flaggedRows: [5], cursorRow: 5 },
];

function SelectionRefusalBody({ literal, flaggedRows, cursorRow }: SelectionLiteralCase) {
    return BuildErrorBody({
        message: literal,
        info_message: RECORD_ACTIONS_INFO,
        field_errors: flaggedRows.map((rowNumber) => `rows[${rowNumber - 1}].selection`),
        cursor_field: `rows[${cursorRow - 1}].selection`,
        rows: BuildListRows(PAGE_SIZE),
        first_key: FIRST_KEY,
        last_key: LAST_KEY,
    });
}

describe("COCRDLIC selection literals on /cards", () => {
    it.each(SELECTION_LITERAL_CASES)(
        "COCRDLIC refused selection shows $literal and flags rows $flaggedRows",
        async (literalCase) => {
            const user = await RenderScreen();
            for (const [rowNumber, code] of literalCase.selections) {
                await ChooseSelection(user, rowNumber, code);
            }
            const body = SelectionRefusalBody(literalCase);
            mockSelectCards.mockRejectedValueOnce(BuildAxiosError(HTTP_BAD_REQUEST, body));
            await PressScreenKey(user, "Enter", "click");
            await ExpectAlert(literalCase.literal, ERRMSG_SEVERITY);
            expect(FlaggedRowNumbers()).toEqual(literalCase.flaggedRows);
            expect(SelectionField(literalCase.cursorRow)).toHaveFocus();
            expect(mockRouterPush).not.toHaveBeenCalled();
        },
    );
});

describe("COCRDLIC F3 on /cards", () => {
    it.each(KEY_TRIGGERS)("COCRDLIC F3 by %s returns to the main menu /menu", async (trigger) => {
        const user = await RenderScreen();
        await PressScreenKey(user, "F3", trigger);
        expect(mockRouterPush).toHaveBeenCalledTimes(1);
        expect(mockRouterPush).toHaveBeenCalledWith(MENU_ROUTE);
        expect(mockListCards).toHaveBeenCalledTimes(1);
    });
});

describe("COCRDLIC session refusal on /cards", () => {
    // The API client interceptor owns the 401 redirect to /signon, so the page shows nothing.
    it("COCRDLIC 401 on the first page leaves the /signon redirect to the client", async () => {
        const body = BuildErrorBody();
        mockListCards.mockRejectedValueOnce(BuildAxiosError(HTTP_UNAUTHORIZED, body));
        await RenderScreen();
        expect(mockListCards).toHaveBeenCalledTimes(1);
        expect(screen.queryAllByRole("alert")).toEqual([]);
        expect(BodyRows()).toEqual([]);
        expect(mockRouterPush).not.toHaveBeenCalled();
        expect(mockRouterReplace).not.toHaveBeenCalled();
        expect(SIGN_ON_ROUTE).toBe("/signon");
    });
});

function FilterRefusalBody(): CardListErrorBody {
    return BuildErrorBody({
        message: ACCOUNT_FILTER_MESSAGE,
        field_errors: [ACCT_ID_FIELD, CARD_NUM_FIELD],
        cursor_field: ACCT_ID_FIELD,
    });
}

describe("COCRDLIC filter field errors on /cards", () => {
    it("COCRDLIC flags both filters and puts the cursor on the account filter", async () => {
        const user = await RenderScreen();
        mockListCards.mockRejectedValueOnce(BuildAxiosError(HTTP_BAD_REQUEST, FilterRefusalBody()));
        await user.type(FilterInput(ACCOUNT_FILTER_LABEL), "12A");
        await user.type(FilterInput(CARD_FILTER_LABEL), "4000X");
        await PressKey(user, "Enter");
        await ExpectAlert(ACCOUNT_FILTER_MESSAGE, ERRMSG_SEVERITY);
        const expectedQuery: CardListQuery = {
            acctId: "12A",
            cardNum: "4000X",
            startKey: FIRST_KEY,
            page: SHOWN_PAGE,
        };
        expect(mockListCards).toHaveBeenLastCalledWith(expectedQuery);
        expect(FilterInput(ACCOUNT_FILTER_LABEL)).toHaveAttribute("aria-invalid", "true");
        expect(FilterInput(CARD_FILTER_LABEL)).toHaveAttribute("aria-invalid", "true");
        expect(FilterInput(ACCOUNT_FILTER_LABEL)).toHaveFocus();
    });
});
