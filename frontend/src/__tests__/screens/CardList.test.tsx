// Screen tests: BMS COCRDLI, program COCRDLIC (CCLI), page /cards.
// The literals are copied verbatim from backend/app/services/cards/cocrdlic.py, which ports
// cbl/COCRDLIC.cbl lines 903, 908, 1022, 1058, 1086, 1112, 1219, 1239 and 1244.
import { ThemeProvider } from "@mui/material/styles";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AxiosError, AxiosHeaders, CanceledError, type AxiosResponse } from "axios";
import { StrictMode, type ReactNode } from "react";

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
    StorePendingMessage,
    type LegacyErrorResponse,
    type MessageSeverity,
    type ReadOptions,
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
const NO_MORE_RECORDS_MESSAGE = "NO MORE RECORDS TO SHOW";
// READPREV_ENDFILE_MESSAGE: the READ/CARDDAT file-error literal of the ENDFILE condition (RESP 20,
// RESP2 90) that 9100-READ-BACKWARDS takes as WHEN OTHER, its blanks as BuildFileErrorMessage
// pads them.
const READPREV_ENDFILE_MESSAGE =
    "File Error: READ     on CARDDAT   returned RESP 000000020 ,RESP2 000000090";
// FileErrorOf for any other CARDDAT browse failure: core/errors.FileIoError, IOERR (RESP 17).
const CARD_FILE_ERROR_MESSAGE =
    "File Error: READ     on CARDDAT   returned RESP 000000017 ,RESP2 000000000";
// The cocrdslc.NotFoundError TransferToCard raises for an invalid or expired card_ref, with its
// PROMPT_FOR_KEYS_INFO information line.
const CARD_NOT_FOUND_MESSAGE = "Did not find cards for this search condition";
const PROMPT_FOR_KEYS_INFO = "Please enter Account and Card Number";
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
// ACCTNOn LENGTH=11 shows the cards.acct_id VARCHAR(11) of each row; CRDNUMn LENGTH=16 shows its
// card number masked as twelve asterisks and the last 4 digits (core/security.MaskCardNumber).
const ACCT_ID_LENGTH = 11;
const ACCOUNT_CELL_PATTERN = /^\d{11}$/;
const MASKED_CARD_CELL_PATTERN = /^\*{12}\d{4}$/;
// CRDSTSn holds Y or N; every third card of a fixture page is inactive.
const INACTIVE_ROW_INTERVAL = 3;
// WS-MAX-SCREEN-LINES (cbl/COCRDLIC.cbl lines 177-178).
const PAGE_SIZE = 7;
// The blank option and 88 SELECT-OK VALUES 'S', 'U'.
const OFFERED_CODES = ["", "S", "U"];

const ACCT_ID_FIELD = "acct_id";
const CARD_NUM_FIELD = "card_num";
const FIRST_KEY = "first-key-token";
const LAST_KEY = "last-key-token";
const SHOWN_PAGE = 2;
// CA-FIRST-PAGE, the page F7 reads back to from SHOWN_PAGE.
const FIRST_PAGE = 1;
// The page F8 reads after SHOWN_PAGE, holding cards 8 to 14.
const NEXT_PAGE = 3;
const NEXT_PAGE_FIRST_ROW = 8;
// The page a GET /cards refusal reports after its redisplay, distinct from SHOWN_PAGE.
const REDISPLAYED_PAGE = 1;
const SELECTED_ROW = 3;
const SELECTED_CARD_REF = "card-ref-3";
const SELECTED_ACCT_ID = "00000000003";
// The card of row 4, account 00000000004 and card number ending 4304, which valid filters find.
const FILTERED_ROW = 4;
const FILTERED_FIRST_KEY = "filtered-first-key";
const FILTERED_LAST_KEY = "filtered-last-key";
const MENU_ROUTE = "/menu";
const URL_BASE = "http://127.0.0.1";

const HTTP_OK = 200;
const HTTP_BAD_REQUEST = 400;
const HTTP_UNAUTHORIZED = 401;
const HTTP_NOT_FOUND = 404;
const HTTP_FILE_ERROR = 503;

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

// Every list read carries the AbortSignal of the controller that owns it.
const READ_OPTIONS: ReadOptions = { signal: expect.any(AbortSignal) };

// The ACCTSID and CARDSID filters as the user keys them.
interface FilterTexts {
    acctId: string;
    cardNum: string;
}

interface FilterCase {
    filterName: string;
    filters: FilterTexts;
}

const BLANK_FILTERS: FilterTexts = { acctId: "", cardNum: "" };
// Valid filters naming FILTERED_ROW's card; the 16-digit card number is assembled at run time.
const KEYED_FILTERS: FilterTexts = {
    acctId: String(FILTERED_ROW).padStart(ACCT_ID_LENGTH, "0"),
    cardNum: "4000".repeat(3) + "4304",
};
// The server alone edits and applies the filters, so each request carries them as keyed.
const FILTER_CASES: readonly FilterCase[] = [
    { filterName: "blank", filters: BLANK_FILTERS },
    { filterName: "keyed", filters: KEYED_FILTERS },
];

// A refused selection redisplays rows and page keys beside the legacy feedback, and on GET /cards
// the page number after the redisplay.
interface CardListErrorBody extends LegacyErrorResponse {
    rows?: CardListRow[];
    first_key?: string;
    last_key?: string;
    page?: number;
}

// Row n of a page: its card_ref token, an 11-digit account, a masked card number and a status.
// Each row's account and card differ from every other row's, and statuses mix Y and N, so a cell
// bound to the wrong field or left blank fails.
function BuildListRow(rowNumber: number): CardListRow {
    const rowDigits = String(rowNumber).padStart(2, "0");
    return {
        card_ref: `card-ref-${rowNumber}`,
        acct_id: String(rowNumber).padStart(ACCT_ID_LENGTH, "0"),
        card_num_masked: `************43${rowDigits}`,
        active_status: rowNumber % INACTIVE_ROW_INTERVAL === 0 ? "N" : "Y",
    };
}

function BuildListRows(rowCount: number, firstRowNumber = 1): CardListRow[] {
    return Array.from({ length: rowCount }, (_unused, index) =>
        BuildListRow(firstRowNumber + index),
    );
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

interface RenderedPage {
    user: UserSession;
    Unmount: () => void;
}

// next.config.ts sets reactStrictMode, so the development page renders inside StrictMode, which
// runs every effect as mount, cleanup and mount again, as next dev does.
function RenderPage(isStrictMode: boolean): RenderedPage {
    const user = userEvent.setup();
    const page = (
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <CardListPage />
        </ThemeProvider>
    );
    const { unmount } = render(isStrictMode ? <StrictMode>{page}</StrictMode> : page);
    return { user, Unmount: unmount };
}

// Settles a call the test held pending, then waits for the screen to unlock its keys.
async function AnswerPendingCall<T>(pending: PromiseWithResolvers<T>, answer: T): Promise<void> {
    await act(async () => pending.resolve(answer));
    await WaitForIdle();
}

async function RenderScreen(): Promise<UserSession> {
    const { user } = RenderPage(false);
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

// Keys each non-blank filter as typed; a blank filter is left as the first entry shows it.
async function KeyFilters(user: UserSession, filters: FilterTexts): Promise<void> {
    const keyedTexts: readonly (readonly [string, string])[] = [
        [ACCOUNT_FILTER_LABEL, filters.acctId],
        [CARD_FILTER_LABEL, filters.cardNum],
    ];
    for (const [label, text] of keyedTexts) {
        if (text !== "") {
            await user.type(FilterInput(label), text);
        }
    }
}

function SelectionField(rowNumber: number): HTMLElement {
    return screen.getByRole("combobox", { name: SelectionLabelFor(rowNumber) });
}

// The code each shown row holds: the value of the Select's native input, blank when none.
function SelectionCodesShown(): string[] {
    return BodyRows().map(
        (_row, index) =>
            SelectionField(index + 1)
                .closest(".MuiInputBase-root")
                ?.querySelector<HTMLInputElement>("input")?.value ?? "",
    );
}

function BodyRows(): HTMLElement[] {
    return screen.getAllByRole("row").slice(1);
}

function ColumnHeaderTexts(): (string | null)[] {
    return screen.getAllByRole("columnheader").map((header) => header.textContent);
}

// The cells of one row after its selection field: ACCTNOn, CRDNUMn and CRDSTSn.
type ShownCardRow = Omit<CardListRow, "card_ref">;

function ShownCellsOf(row: CardListRow): ShownCardRow {
    const { acct_id, card_num_masked, active_status } = row;
    return { acct_id, card_num_masked, active_status };
}

// Each body row's account number, card number and active status, read from its own cells.
function ShownRows(): ShownCardRow[] {
    return BodyRows().map((row) => {
        const cellTexts = within(row)
            .getAllByRole("cell")
            .map((cell) => cell.textContent ?? "");
        return {
            acct_id: cellTexts[1] ?? "",
            card_num_masked: cellTexts[2] ?? "",
            active_status: cellTexts[3] ?? "",
        };
    });
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

// The id of the MessageLine ERRMSG Alert, which a field named in field_errors is described by.
const ERRMSG_ID = "message-line-errmsg";

// The rows whose CRDSELn field is described by the ERRMSG line: exactly the flagged rows.
function DescribedRowNumbers(): number[] {
    return BodyRows()
        .map((_row, index) => index + 1)
        .filter(
            (rowNumber) => SelectionField(rowNumber).getAttribute("aria-describedby") === ERRMSG_ID,
        );
}

function InDocumentOrder(first: Node, second: Node): number {
    return first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
}

// Every Alert shown, in screen order: ERRMSG has role alert and INFOMSG role status. The page's
// own result status is a status region too, but no Alert.
function ShownAlerts(): HTMLElement[] {
    return [...screen.queryAllByRole("alert"), ...screen.queryAllByRole("status")]
        .filter((region) => region.classList.contains("MuiAlert-root"))
        .sort(InDocumentOrder);
}

// The page's polite result status: the one status region that is not the INFOMSG Alert.
function ResultStatusRegion(): HTMLElement {
    const regions = screen
        .getAllByRole("status")
        .filter((region) => !region.classList.contains("MuiAlert-root"));
    expect(regions).toHaveLength(1);
    const [resultStatus] = regions as [HTMLElement];
    expect(resultStatus).toHaveAttribute("aria-live", "polite");
    return resultStatus;
}

// The first Alert in screen order whose raw text is the literal.
function AlertWithText(text: string): HTMLElement | undefined {
    return ShownAlerts().find((alert) => alert.textContent === text);
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

// Every row shown is sent with its code, blank for the rows the user left untouched, beside the
// filters as keyed.
function ExpectedSelectionRequest(
    code: string,
    filters: FilterTexts = BLANK_FILTERS,
): CardSelectionRequest {
    return {
        acct_id: filters.acctId,
        card_num: filters.cardNum,
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

// Every account cell fits ACCTNOn and every card cell is a masked CRDNUMn.
function ExpectMapWidths(shownRows: readonly ShownCardRow[]): void {
    for (const shownRow of shownRows) {
        expect(shownRow.acct_id).toMatch(ACCOUNT_CELL_PATTERN);
        expect(shownRow.card_num_masked).toMatch(MASKED_CARD_CELL_PATTERN);
    }
}

// Every row shows its own account, masked card number and status, in the order returned, and no
// 16-digit run appears anywhere on the page.
function ExpectShownRows(expectedRows: readonly CardListRow[]): void {
    const shownRows = ShownRows();
    expect(shownRows).toEqual(expectedRows.map(ShownCellsOf));
    ExpectMapWidths(shownRows);
    expect(document.body.textContent).not.toMatch(PAN_PATTERN);
}

// The rows appear under the COCRDLI column headings.
function ExpectMaskedCardRows(expectedRows: readonly CardListRow[]): void {
    expect(ColumnHeaderTexts()).toEqual(COLUMN_HEADERS);
    ExpectShownRows(expectedRows);
}

describe("COCRDLIC BMS COCRDLI content on /cards", () => {
    it("COCRDLIC first entry shows the title, filters, headers and 7 masked cards", async () => {
        await RenderScreen();
        expect(mockListCards).toHaveBeenCalledTimes(1);
        expect(mockListCards).toHaveBeenCalledWith({}, READ_OPTIONS);
        ExpectScreenHeader();
        ExpectFilterFields();
        ExpectMaskedCardRows(BuildListRows(PAGE_SIZE));
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

// ENTER by its button, by the key from the account filter, or by the key on the row Select that
// keeps the cursor once its code is chosen.
type SelectionTrigger = KeyTrigger | "key on the row Select";

const SELECTION_TRIGGERS: readonly SelectionTrigger[] = [...KEY_TRIGGERS, "key on the row Select"];

interface SelectionRouteCase extends FilterCase {
    code: string;
    action: CardSelectionAction;
    route: string;
    trigger: SelectionTrigger;
}

const SELECTION_ROUTES: readonly Pick<SelectionRouteCase, "code" | "action" | "route">[] = [
    { code: "S", action: "view", route: "/cards/view" },
    { code: "U", action: "update", route: "/cards/update" },
];

// The keyed filters travel only in the selection body; the route names the card_ref and the
// selected row's own account, never a filter.
const SELECTION_ROUTE_CASES: readonly SelectionRouteCase[] = SELECTION_ROUTES.flatMap((routeCase) =>
    SELECTION_TRIGGERS.flatMap((trigger) =>
        FILTER_CASES.map((filterCase) => ({ ...routeCase, trigger, ...filterCase })),
    ),
);

// Enter on the focused row Select is the screen's ENTER: the menu stays closed.
async function PressSelectionEnter(user: UserSession, trigger: SelectionTrigger): Promise<void> {
    if (trigger !== "key on the row Select") {
        await PressScreenKey(user, "Enter", trigger);
        return;
    }
    expect(SelectionField(SELECTED_ROW)).toHaveFocus();
    await PressKey(user, "Enter");
    await WaitForIdle();
    expect(screen.queryByRole("listbox")).toBeNull();
}

describe("COCRDLIC selection on /cards", () => {
    it.each(SELECTION_ROUTE_CASES)(
        "COCRDLIC $code and ENTER by $trigger, $filterName filters, routes to $route by cardRef",
        async ({ code, action, route, trigger, filters }) => {
            mockSelectCards.mockResolvedValueOnce(BuildSelectionResponse(action));
            const user = await RenderScreen();
            await KeyFilters(user, filters);
            await ChooseSelection(user, SELECTED_ROW, code);
            await PressSelectionEnter(user, trigger);
            expect(mockSelectCards).toHaveBeenCalledTimes(1);
            expect(mockSelectCards).toHaveBeenCalledWith(ExpectedSelectionRequest(code, filters));
            expect(mockListCards).toHaveBeenCalledTimes(1);
            ExpectCardRoute(route);
            expect(mockRouterReplace).not.toHaveBeenCalled();
            expect(RouterArgumentsWithPan()).toEqual([]);
        },
    );
});

interface PagingCase extends FilterCase {
    key: ScreenKey;
    readName: string;
    startKeyField: "first_key" | "last_key";
    direction?: Direction;
    answer: CardListResponse;
    trigger: KeyTrigger;
}

// The one-row page a paging key returns here: FILTERED_ROW's card, which the keyed filters name,
// with fresh page keys and the page number and literals the service gives the key.
function FilteredPage(page: number, message: string, infoMessage: string): CardListResponse {
    return BuildListResponse({
        rows: [BuildListRow(FILTERED_ROW)],
        page,
        first_key: FILTERED_FIRST_KEY,
        last_key: FILTERED_LAST_KEY,
        message,
        info_message: infoMessage,
    });
}

// ENTER re-reads the shown page from first_key; F7 and F8 page from first_key and last_key. Each
// answer is the one-row page the service returns: F8 reads page 3 and ENTER page 2 to the end of
// CARDDAT (NO MORE RECORDS TO SHOW beside the record-actions line); F7 reads back to page 1, where
// NO PREVIOUS PAGES TO DISPLAY replaces the ENDFILE literal and blanks the information line.
const PAGING_KEYS: readonly Omit<PagingCase, "trigger" | keyof FilterCase>[] = [
    {
        key: "F8",
        readName: "forward",
        startKeyField: "last_key",
        direction: Direction.FORWARD,
        answer: FilteredPage(NEXT_PAGE, NO_MORE_RECORDS_MESSAGE, RECORD_ACTIONS_INFO),
    },
    {
        key: "F7",
        readName: "backward",
        startKeyField: "first_key",
        direction: Direction.BACKWARD,
        answer: FilteredPage(FIRST_PAGE, NO_PREVIOUS_MESSAGE, ""),
    },
    {
        key: "Enter",
        readName: "re-read",
        startKeyField: "first_key",
        answer: FilteredPage(SHOWN_PAGE, NO_MORE_RECORDS_MESSAGE, RECORD_ACTIONS_INFO),
    },
];

const PAGING_CASES: readonly PagingCase[] = PAGING_KEYS.flatMap((pagingKey) =>
    KEY_TRIGGERS.flatMap((trigger) =>
        FILTER_CASES.map((filterCase) => ({ ...pagingKey, trigger, ...filterCase })),
    ),
);

// The query a paging key sends from the page shown: the filters as keyed, the shown page's
// first_key or last_key as the key takes it, and the shown page's number.
function PagingQuery(pagingCase: PagingCase, shownPage: CardListResponse): CardListQuery {
    return {
        acctId: pagingCase.filters.acctId,
        cardNum: pagingCase.filters.cardNum,
        startKey: shownPage[pagingCase.startKeyField],
        direction: pagingCase.direction,
        page: shownPage.page,
    };
}

// The second press pages from the keys and page number of the page the first one returned.
describe("COCRDLIC paging on /cards", () => {
    it.each(PAGING_CASES)(
        "COCRDLIC $key by $trigger with $filterName filters pages from $startKeyField",
        async (pagingCase) => {
            const { key, trigger, answer } = pagingCase;
            const user = await RenderScreen();
            await KeyFilters(user, pagingCase.filters);
            mockListCards.mockResolvedValueOnce(answer);
            await PressScreenKey(user, key, trigger);
            ExpectLastListRead(PagingQuery(pagingCase, BuildListResponse()));
            ExpectShownRows(answer.rows);
            await PressScreenKey(user, key, trigger);
            expect(mockListCards).toHaveBeenCalledTimes(3);
            ExpectLastListRead(PagingQuery(pagingCase, answer));
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
            expect(DescribedRowNumbers()).toEqual(literalCase.flaggedRows);
            expect(SelectionField(literalCase.cursorRow)).toHaveFocus();
            expect(mockRouterPush).not.toHaveBeenCalled();
        },
    );
});

// The ERRMSG literal in red, then the information line as info when there is one, and no other
// message line.
async function ExpectFeedback(literal: string, infoMessage: string): Promise<void> {
    await ExpectAlert(literal, ERRMSG_SEVERITY);
    if (infoMessage === "") {
        expect(AlertTexts()).toEqual([literal]);
        return;
    }
    await ExpectAlert(infoMessage, "info");
    expect(AlertTexts()).toEqual([literal, infoMessage]);
}

interface PagingLiteralCase {
    literal: string;
    key: Extract<ScreenKey, "F7" | "F8">;
    shownPage: CardListResponse;
    query: CardListQuery;
    answer: CardListResponse;
}

// Fewer rows than the 7 of a full page, read up to an end of CARDDAT.
const SHORT_PAGE_ROW_COUNT = 3;
const SHORT_PAGE_FIRST_KEY = "short-page-first-key";
const SHORT_PAGE_LAST_KEY = "short-page-last-key";

// The 200 page that ends at an end of CARDDAT: its literal beside the record-actions line, the
// short page's rows from firstRowNumber, its page number and fresh page keys.
function ShortPageResponse(
    literal: string,
    page: number,
    firstRowNumber: number,
): CardListResponse {
    return BuildListResponse({
        rows: BuildListRows(SHORT_PAGE_ROW_COUNT, firstRowNumber),
        page,
        first_key: SHORT_PAGE_FIRST_KEY,
        last_key: SHORT_PAGE_LAST_KEY,
        message: literal,
        info_message: RECORD_ACTIONS_INFO,
    });
}

// 9000-READ-FORWARD sets NO MORE RECORDS TO SHOW when F8 reads the last cards with no look-ahead
// card. 9100-READ-BACKWARDS moves the ENDFILE file error when F7 finds fewer than 7 cards before
// the first card shown, and still shows them. 1400-SETUP-MESSAGE keeps the record-actions line
// with both. F7 starts on page 3: from page 2 it reads back to page 1, where 1400-SETUP-MESSAGE
// replaces the file error with NO PREVIOUS PAGES TO DISPLAY.
const PAGING_LITERAL_CASES: readonly PagingLiteralCase[] = [
    {
        literal: NO_MORE_RECORDS_MESSAGE,
        key: "F8",
        shownPage: BuildListResponse(),
        query: {
            acctId: "",
            cardNum: "",
            startKey: LAST_KEY,
            direction: Direction.FORWARD,
            page: SHOWN_PAGE,
        },
        answer: ShortPageResponse(NO_MORE_RECORDS_MESSAGE, NEXT_PAGE, NEXT_PAGE_FIRST_ROW),
    },
    {
        literal: READPREV_ENDFILE_MESSAGE,
        key: "F7",
        shownPage: NextPageResponse(PAGE_SIZE),
        query: {
            acctId: "",
            cardNum: "",
            startKey: FIRST_KEY,
            direction: Direction.BACKWARD,
            page: NEXT_PAGE,
        },
        answer: ShortPageResponse(READPREV_ENDFILE_MESSAGE, SHOWN_PAGE, 1),
    },
];

describe("COCRDLIC end-of-file literals on /cards", () => {
    it.each(PAGING_LITERAL_CASES)(
        "COCRDLIC $key answered 200 shows $literal in red over the rows read",
        async ({ literal, key, shownPage, query, answer }) => {
            mockListCards.mockResolvedValueOnce(shownPage).mockResolvedValueOnce(answer);
            const user = await RenderScreen();
            await PressScreenKey(user, key, "click");
            ExpectLastListRead(query);
            await ExpectFeedback(literal, RECORD_ACTIONS_INFO);
            ExpectShownRows(answer.rows);
            expect(screen.getByText(`Page ${answer.page}`)).toBeVisible();
            expect(mockRouterPush).not.toHaveBeenCalled();
        },
    );
});

interface RefusedLiteralCase {
    literal: string;
    status: number;
    key: Extract<ScreenKey, "Enter" | "F8">;
    selections: readonly (readonly [number, string])[];
    body: CardListErrorBody;
    listReads: number;
    selectionPosts: number;
}

// ACCTSID and CARDSID by the field_errors name of each.
const FILTER_LABELS: Readonly<Record<string, string>> = {
    acct_id: ACCOUNT_FILTER_LABEL,
    card_num: CARD_FILTER_LABEL,
};

// A CARDDAT browse failure is FileErrorOf's 503, LegacyMessageError(field="acct_id"), with no
// information line. A selection whose card_ref is invalid or expired is TransferToCard's 404
// cocrdslc.NotFoundError, which flags both filters. Neither body redisplays rows.
const REFUSED_LITERAL_CASES: readonly RefusedLiteralCase[] = [
    {
        literal: CARD_FILE_ERROR_MESSAGE,
        status: HTTP_FILE_ERROR,
        key: "F8",
        selections: [],
        body: BuildErrorBody({
            message: CARD_FILE_ERROR_MESSAGE,
            field_errors: [ACCT_ID_FIELD],
            cursor_field: ACCT_ID_FIELD,
        }),
        listReads: 2,
        selectionPosts: 0,
    },
    {
        literal: CARD_NOT_FOUND_MESSAGE,
        status: HTTP_NOT_FOUND,
        key: "Enter",
        selections: [[SELECTED_ROW, "S"]],
        body: BuildErrorBody({
            message: CARD_NOT_FOUND_MESSAGE,
            info_message: PROMPT_FOR_KEYS_INFO,
            field_errors: [ACCT_ID_FIELD, CARD_NUM_FIELD],
            cursor_field: ACCT_ID_FIELD,
        }),
        listReads: 1,
        selectionPosts: 1,
    },
];

// ENTER over a keyed code is the selection POST; any other key is a GET /cards read.
function RefuseNextCall({ status, key, body }: RefusedLiteralCase): void {
    const refusal = BuildAxiosError(status, body);
    if (key === "Enter") {
        mockSelectCards.mockRejectedValueOnce(refusal);
        return;
    }
    mockListCards.mockRejectedValueOnce(refusal);
}

// The code each of the seven rows holds after the keyed selections, blank elsewhere.
function RowCodesFor(selections: readonly (readonly [number, string])[]): string[] {
    const codes = Array.from({ length: PAGE_SIZE }, () => "");
    for (const [rowNumber, code] of selections) {
        codes[rowNumber - 1] = code;
    }
    return codes;
}

// A refusal without rows keeps the page shown: its rows, its page number and the codes keyed.
function ExpectShownPageKept(selections: readonly (readonly [number, string])[]): void {
    ExpectShownRows(BuildListRows(PAGE_SIZE));
    expect(SelectionCodesShown()).toEqual(RowCodesFor(selections));
    expect(screen.getByText(`Page ${SHOWN_PAGE}`)).toBeVisible();
}

// aria-invalid marks exactly the filters field_errors names, as the map colours a flagged field,
// and only those are described by the ERRMSG line.
function ExpectFlaggedFilters(flaggedFields: readonly string[]): void {
    for (const [field, label] of Object.entries(FILTER_LABELS)) {
        const isFlagged = flaggedFields.includes(field);
        expect(FilterInput(label)).toHaveAttribute("aria-invalid", String(isFlagged));
        const description = FilterInput(label).getAttribute("aria-describedby");
        expect(description).toBe(isFlagged ? ERRMSG_ID : null);
    }
}

describe("COCRDLIC refused reads and selections on /cards", () => {
    it.each(REFUSED_LITERAL_CASES)(
        "COCRDLIC $key refused $status shows $literal in red and keeps the page shown",
        async (refusedCase) => {
            const { selections, body } = refusedCase;
            const user = await RenderScreen();
            await ChooseSelections(user, selections);
            RefuseNextCall(refusedCase);
            await PressScreenKey(user, refusedCase.key, "click");
            await ExpectFeedback(refusedCase.literal, body.info_message ?? "");
            ExpectShownPageKept(selections);
            expect(mockListCards).toHaveBeenCalledTimes(refusedCase.listReads);
            expect(mockSelectCards).toHaveBeenCalledTimes(refusedCase.selectionPosts);
            ExpectFlaggedFilters(body.field_errors ?? []);
            expect(FilterInput(ACCOUNT_FILTER_LABEL)).toHaveFocus();
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

// COCRDLIC PFK-INVALID: an AID outside ENTER, PF3, PF7 and PF8 does SET CCARD-AID-ENTER, so the
// key never reaches the browser and re-reads the page shown exactly as ENTER does.
const PFK_INVALID_KEYS: readonly string[] = ["F4", "F5", "F12"];

// ENTER's re-read of the page shown, with the account filter keyed and the card filter blank.
const ENTER_REREAD_QUERY: CardListQuery = {
    acctId: KEYED_FILTERS.acctId,
    cardNum: "",
    startKey: FIRST_KEY,
    page: SHOWN_PAGE,
};

// A key the legend lacks, pressed in the focused field; the event tells whether its browser
// default was blocked. The cases press it from the account filter, where the answer puts the
// cursor.
async function PressUnboundKey(keyName: string): Promise<KeyboardEvent> {
    const keyEvent = new KeyboardEvent("keydown", {
        key: keyName,
        bubbles: true,
        cancelable: true,
    });
    await act(async () => {
        document.activeElement?.dispatchEvent(keyEvent);
    });
    return keyEvent;
}

describe("COCRDLIC PFK-INVALID keys on /cards", () => {
    it.each(PFK_INVALID_KEYS)(
        "COCRDLIC %s is processed as ENTER: the same re-read, the browser default blocked",
        async (keyName) => {
            const user = await RenderScreen();
            await user.type(FilterInput(ACCOUNT_FILTER_LABEL), KEYED_FILTERS.acctId);
            const keyEvent = await PressUnboundKey(keyName);
            await WaitForIdle();
            expect(keyEvent.defaultPrevented).toBe(true);
            expect(mockListCards).toHaveBeenCalledTimes(2);
            ExpectLastListRead(ENTER_REREAD_QUERY);
            await PressScreenKey(user, "Enter", "key");
            expect(mockListCards).toHaveBeenCalledTimes(3);
            ExpectLastListRead(ENTER_REREAD_QUERY);
            expect(mockSelectCards).not.toHaveBeenCalled();
        },
    );
});

describe("COCRDLIC session refusal on /cards", () => {
    // The API client interceptor owns the 401 redirect to /signon, so the page shows nothing.
    it("COCRDLIC 401 on the first page leaves the /signon redirect to the client", async () => {
        const body = BuildErrorBody();
        mockListCards.mockRejectedValueOnce(BuildAxiosError(HTTP_UNAUTHORIZED, body));
        await RenderScreen();
        expect(mockListCards).toHaveBeenCalledTimes(1);
        expect(ShownAlerts()).toEqual([]);
        expect(ResultStatusRegion().textContent).toBe("");
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
    it("COCRDLIC flags both filters, ties them to ERRMSG and focuses the account", async () => {
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
        expect(mockListCards).toHaveBeenLastCalledWith(expectedQuery, READ_OPTIONS);
        ExpectFlaggedFilters([ACCT_ID_FIELD, CARD_NUM_FIELD]);
        expect(FilterInput(ACCOUNT_FILTER_LABEL)).toHaveFocus();
    });
});

interface PendingReadCase {
    readName: string;
    key?: ScreenKey;
}

// The list reads a key starts. F3 starts none, and like every key it is locked while a read runs,
// so a page with a read in flight is left by browser navigation.
const KEYED_READS: readonly Required<PendingReadCase>[] = [
    { readName: "forward", key: "F8" },
    { readName: "backward", key: "F7" },
    { readName: "re-read", key: "Enter" },
];

const PENDING_READ_CASES: readonly PendingReadCase[] = [{ readName: "first-page" }, ...KEYED_READS];

// Shown on entry, as a refusal or sign-off literal carried across a navigation would be.
const PENDING_MESSAGE = "MESSAGE CARRIED TO THE CARD LIST";
const LATEST_PAGE = 1;
const LATEST_ROW_COUNT = 3;

// Settles only when its signal is aborted, rejecting with axios's CanceledError as the API client
// does.
function PendingUntilAborted(
    _query: CardListQuery,
    options?: ReadOptions,
): Promise<CardListResponse> {
    return new Promise((_resolve, reject) => {
        options?.signal?.addEventListener("abort", () => reject(new CanceledError()));
    });
}

// The next list read stays in flight until the returned function settles it.
function DeferNextListRead(): (response: CardListResponse) => void {
    let settleRead: (response: CardListResponse) => void = () => undefined;
    mockListCards.mockImplementationOnce(
        () =>
            new Promise<CardListResponse>((resolve) => {
                settleRead = resolve;
            }),
    );
    return (response) => settleRead(response);
}

function ListReadSignal(callIndex: number): AbortSignal | undefined {
    return mockListCards.mock.calls[callIndex]?.[1]?.signal;
}

function AlertTexts(): (string | null)[] {
    return ShownAlerts().map((alert) => alert.textContent);
}

function ExpectBusy(): void {
    expect(screen.getByRole("progressbar", { name: "Processing" })).toBeVisible();
}

// A macrotask runs only once every settled promise has run its handlers, so by then a cancelled
// read has done everything it will do.
async function FlushSettledReads(): Promise<void> {
    await act(() => new Promise<void>((resolve) => setTimeout(resolve, 0)));
}

// Leaves the case's read in flight: the first page itself, or the key's read once the first page
// is shown.
async function StartPendingRead({ key }: PendingReadCase): Promise<RenderedPage> {
    if (key === undefined) {
        mockListCards.mockImplementationOnce(PendingUntilAborted);
        return RenderPage(false);
    }
    const page = RenderPage(false);
    await WaitForIdle();
    mockListCards.mockImplementationOnce(PendingUntilAborted);
    await page.user.click(screen.getByRole("button", { name: KEY_BUTTON_NAMES[key] }));
    return page;
}

describe("COCRDLIC leaving /cards with a list read in flight", () => {
    it.each(PENDING_READ_CASES)(
        "COCRDLIC leaving the page aborts the pending $readName read and shows no message",
        async (readCase) => {
            const { Unmount } = await StartPendingRead(readCase);
            const signal = ListReadSignal(mockListCards.mock.calls.length - 1);
            expect(signal).toBeInstanceOf(AbortSignal);
            expect(signal?.aborted).toBe(false);
            ExpectBusy();
            Unmount();
            expect(signal?.aborted).toBe(true);
            await FlushSettledReads();
            expect(AlertTexts()).toEqual([]);
            expect(mockRouterPush).not.toHaveBeenCalled();
        },
    );
});

// The last list read sent the query with the signal of the controller that owns it.
function ExpectLastListRead(query: CardListQuery): void {
    expect(mockListCards).toHaveBeenLastCalledWith(query, READ_OPTIONS);
}

function NextPageResponse(rowCount: number): CardListResponse {
    return BuildListResponse({
        page: NEXT_PAGE,
        rows: BuildListRows(rowCount, NEXT_PAGE_FIRST_ROW),
    });
}

// The remount aborts the first read; its answer, arriving last as one already on the wire would,
// must not replace the F8 page.
describe("COCRDLIC first page under React Strict Mode on /cards", () => {
    it("COCRDLIC drops the aborted first read; the F8 page that follows stays shown", async () => {
        const abortedPage = Promise.withResolvers<CardListResponse>();
        const firstPage = Promise.withResolvers<CardListResponse>();
        const nextPage = Promise.withResolvers<CardListResponse>();
        mockListCards.mockReturnValueOnce(abortedPage.promise);
        mockListCards.mockReturnValueOnce(firstPage.promise).mockReturnValueOnce(nextPage.promise);
        const { user } = RenderPage(true);
        expect([ListReadSignal(0)?.aborted, ListReadSignal(1)?.aborted]).toEqual([true, false]);
        await AnswerPendingCall(firstPage, BuildListResponse());
        await user.click(screen.getByRole("button", { name: KEY_BUTTON_NAMES.F8 }));
        await AnswerPendingCall(nextPage, NextPageResponse(PAGE_SIZE));
        await AnswerPendingCall(abortedPage, BuildListResponse());
        expect(mockListCards).toHaveBeenCalledTimes(3);
        expect(screen.getByText(`Page ${NEXT_PAGE}`)).toBeVisible();
        ExpectShownRows(NextPageResponse(PAGE_SIZE).rows);
    });
});

describe("COCRDLIC first page arriving after unmount on /cards", () => {
    it("COCRDLIC a first page arriving after unmount applies nothing and raises nothing", async () => {
        const consoleError = jest.spyOn(console, "error");
        const firstPage = Promise.withResolvers<CardListResponse>();
        mockListCards.mockReturnValueOnce(firstPage.promise);
        const { Unmount } = RenderPage(false);
        Unmount();
        await AnswerPendingCall(firstPage, BuildListResponse());
        expect(mockListCards).toHaveBeenCalledTimes(1);
        expect(consoleError).not.toHaveBeenCalled();
        expect(screen.queryAllByRole("row")).toEqual([]);
        consoleError.mockRestore();
    });
});

describe("COCRDLIC selection answer arriving after unmount on /cards", () => {
    it("COCRDLIC a selection answer arriving after unmount does not navigate", async () => {
        const selection = Promise.withResolvers<CardSelectionResponse>();
        mockSelectCards.mockReturnValueOnce(selection.promise);
        const { user, Unmount } = RenderPage(false);
        await WaitForIdle();
        await ChooseSelection(user, SELECTED_ROW, "S");
        await user.click(screen.getByRole("button", { name: KEY_BUTTON_NAMES.Enter }));
        Unmount();
        await AnswerPendingCall(selection, BuildSelectionResponse("view"));
        expect(mockSelectCards).toHaveBeenCalledTimes(1);
        expect(mockRouterPush).not.toHaveBeenCalled();
    });
});

interface PagingRefusalCase {
    key: Extract<ScreenKey, "F7" | "F8">;
    startKey: string;
    direction: Direction;
    literal: string;
}

// The server overrides the selection literal with NO PREVIOUS PAGES TO DISPLAY for F7 on page 1.
const PAGING_REFUSAL_CASES: readonly PagingRefusalCase[] = [
    { key: "F8", startKey: LAST_KEY, direction: Direction.FORWARD, literal: MULTI_SELECT_MESSAGE },
    { key: "F7", startKey: FIRST_KEY, direction: Direction.BACKWARD, literal: NO_PREVIOUS_MESSAGE },
];

// S on row 2 and U on row 4, as SELECTION_LITERAL_CASES refuses them on ENTER.
const TWO_SELECTIONS: readonly (readonly [number, string])[] = [
    [2, "S"],
    [4, "U"],
];
const TWO_SELECTION_CODES = ["", "S", "", "U", "", "", ""];

function PagingRefusalBody(literal: string): CardListErrorBody {
    const literalCase = { literal, selections: TWO_SELECTIONS, flaggedRows: [2, 4], cursorRow: 2 };
    return { ...SelectionRefusalBody(literalCase), page: REDISPLAYED_PAGE };
}

// The shown page's key and number, the filters as keyed and the seven positional codes.
function PagingRefusalQuery({ startKey, direction }: PagingRefusalCase): CardListQuery {
    return {
        acctId: "",
        cardNum: "",
        startKey,
        direction,
        page: SHOWN_PAGE,
        selections: TWO_SELECTION_CODES,
    };
}

async function ChooseSelections(
    user: UserSession,
    selections: readonly (readonly [number, string])[],
): Promise<void> {
    for (const [rowNumber, code] of selections) {
        await ChooseSelection(user, rowNumber, code);
    }
}

describe("COCRDLIC selection codes on the paging keys on /cards", () => {
    it.each(PAGING_REFUSAL_CASES)(
        "COCRDLIC $key sends every row code and shows the refused $literal",
        async (refusalCase) => {
            const user = await RenderScreen();
            await ChooseSelections(user, TWO_SELECTIONS);
            const body = PagingRefusalBody(refusalCase.literal);
            mockListCards.mockRejectedValueOnce(BuildAxiosError(HTTP_BAD_REQUEST, body));
            await PressScreenKey(user, refusalCase.key, "click");
            ExpectLastListRead(PagingRefusalQuery(refusalCase));
            expect(mockSelectCards).not.toHaveBeenCalled();
            await ExpectAlert(refusalCase.literal, ERRMSG_SEVERITY);
            expect(FlaggedRowNumbers()).toEqual([2, 4]);
            expect(SelectionField(2)).toHaveFocus();
            expect(screen.getByText(`Page ${REDISPLAYED_PAGE}`)).toBeVisible();
            expect(mockRouterPush).not.toHaveBeenCalled();
        },
    );
});

// The first read is aborted and has changed nothing: the pending message still shows and the keys
// stay locked for the remount's read, the only one left in flight.
function ExpectOnlyRemountReadInFlight(): void {
    expect(mockListCards.mock.calls).toEqual([
        [{}, READ_OPTIONS],
        [{}, READ_OPTIONS],
    ]);
    expect([ListReadSignal(0)?.aborted, ListReadSignal(1)?.aborted]).toEqual([true, false]);
    ExpectBusy();
    expect(AlertTexts()).toEqual([PENDING_MESSAGE]);
}

describe("COCRDLIC first page under the development remount on /cards", () => {
    it("COCRDLIC the remount aborts the first read and applies only the second", async () => {
        StorePendingMessage(PENDING_MESSAGE);
        mockListCards.mockImplementationOnce(PendingUntilAborted);
        const settleLatestRead = DeferNextListRead();
        RenderPage(true);
        await FlushSettledReads();
        ExpectOnlyRemountReadInFlight();
        const rows = BuildListRows(LATEST_ROW_COUNT);
        act(() =>
            settleLatestRead(BuildListResponse({ page: LATEST_PAGE, rows, info_message: "" })),
        );
        await WaitForIdle();
        ExpectShownRows(rows);
        expect(screen.getByText(`Page ${LATEST_PAGE}`)).toBeVisible();
        expect(AlertTexts()).toEqual([PENDING_MESSAGE]);
    });
});

describe("COCRDLIC cancelled list reads on /cards", () => {
    it.each(KEYED_READS)(
        "COCRDLIC a cancelled $readName read shows no message and keeps the page shown",
        async ({ key }) => {
            const user = await RenderScreen();
            mockListCards.mockRejectedValueOnce(new CanceledError());
            await PressScreenKey(user, key, "click");
            expect(mockListCards).toHaveBeenCalledTimes(2);
            expect(AlertTexts()).toEqual([RECORD_ACTIONS_INFO]);
            ExpectMaskedCardRows(BuildListRows(PAGE_SIZE));
            expect(screen.getByText(`Page ${SHOWN_PAGE}`)).toBeVisible();
            expect(screen.getByRole("button", { name: KEY_BUTTON_NAMES[key] })).toBeEnabled();
        },
    );
});

interface CodeCarryCase {
    rowCount: number;
    expectedCodes: readonly string[];
}

// S on SELECTED_ROW (row 3) of the seven rows shown, blank elsewhere.
const SELECTED_ROW_CODES = ["", "", "S", "", "", "", ""];

// 1200-SCREEN-ARRAY-INIT moves WS-EDIT-SELECT(n) to every row sent, so a code stays on its row
// position; a shorter page has no row 3 to carry it.
const CODE_CARRY_CASES: readonly CodeCarryCase[] = [
    { rowCount: PAGE_SIZE, expectedCodes: SELECTED_ROW_CODES },
    { rowCount: 2, expectedCodes: ["", ""] },
];

describe("COCRDLIC single selection code on F8 on /cards", () => {
    it.each(CODE_CARRY_CASES)(
        "COCRDLIC S then F8 to a $rowCount-row page shows codes $expectedCodes",
        async ({ rowCount, expectedCodes }) => {
            const user = await RenderScreen();
            await ChooseSelection(user, SELECTED_ROW, "S");
            mockListCards.mockResolvedValueOnce(NextPageResponse(rowCount));
            await PressScreenKey(user, "F8", "click");
            expect(mockListCards.mock.lastCall?.[0].selections).toEqual(SELECTED_ROW_CODES);
            expect(mockSelectCards).not.toHaveBeenCalled();
            expect(screen.getByText(`Page ${NEXT_PAGE}`)).toBeVisible();
            expect(SelectionCodesShown()).toEqual(expectedCodes);
            expect(mockRouterPush).not.toHaveBeenCalled();
        },
    );
});

// A selection the server answers with no action is COCRDLIC's WHEN OTHER path: the page shown is
// re-read from first_key with every row's code.
const UNACTIONED_SELECTION: CardSelectionResponse = {
    ...BuildSelectionResponse("view"),
    action: null,
    card_ref: "",
};
const UNACTIONED_REREAD_QUERY: CardListQuery = {
    acctId: "",
    cardNum: "",
    startKey: FIRST_KEY,
    page: SHOWN_PAGE,
    selections: SELECTED_ROW_CODES,
};

// S on SELECTED_ROW and ENTER, once the first page is shown.
async function SubmitSelectedRow(): Promise<RenderedPage> {
    const page = RenderPage(false);
    await WaitForIdle();
    await ChooseSelection(page.user, SELECTED_ROW, "S");
    await page.user.click(screen.getByRole("button", { name: KEY_BUTTON_NAMES.Enter }));
    return page;
}

describe("COCRDLIC re-read after an unactioned selection on /cards", () => {
    it("COCRDLIC sends the re-read with its signal, and leaving the page aborts it", async () => {
        mockSelectCards.mockResolvedValueOnce(UNACTIONED_SELECTION);
        mockListCards.mockResolvedValueOnce(BuildListResponse());
        mockListCards.mockImplementationOnce(PendingUntilAborted);
        const { Unmount } = await SubmitSelectedRow();
        await waitFor(() => {
            expect(mockListCards).toHaveBeenCalledTimes(2);
        });
        ExpectLastListRead(UNACTIONED_REREAD_QUERY);
        expect(ListReadSignal(1)?.aborted).toBe(false);
        ExpectBusy();
        Unmount();
        expect(ListReadSignal(1)?.aborted).toBe(true);
        await FlushSettledReads();
        expect(mockSelectCards).toHaveBeenCalledWith(ExpectedSelectionRequest("S"));
        expect(mockRouterPush).not.toHaveBeenCalled();
    });
});

// Leaving while the selection runs aborts the re-read's controller, so axios rejects that read
// without sending it; the selection itself carries no signal to abort.
describe("COCRDLIC leaving /cards while a selection runs", () => {
    it("COCRDLIC hands the re-read of an unactioned selection an aborted signal", async () => {
        const selection = Promise.withResolvers<CardSelectionResponse>();
        mockSelectCards.mockReturnValueOnce(selection.promise);
        const { Unmount } = await SubmitSelectedRow();
        expect(mockSelectCards).toHaveBeenCalledWith(ExpectedSelectionRequest("S"));
        Unmount();
        await AnswerPendingCall(selection, UNACTIONED_SELECTION);
        expect(mockListCards).toHaveBeenCalledTimes(2);
        ExpectLastListRead(UNACTIONED_REREAD_QUERY);
        expect(ListReadSignal(1)?.aborted).toBe(true);
        expect(mockRouterPush).not.toHaveBeenCalled();
    });
});

const FILTER_LABEL_LIST: readonly string[] = [ACCOUNT_FILTER_LABEL, CARD_FILTER_LABEL];

// While a read is out both filters are read-only, so typing changes neither, and every CRDSELn
// field is aria-readonly, so a click opens no menu: the answer is shown under the filters and
// codes it was read with.
async function ExpectFiltersAndCodesLocked(user: UserSession): Promise<void> {
    for (const label of FILTER_LABEL_LIST) {
        expect(FilterInput(label)).toHaveAttribute("readonly");
    }
    await user.type(FilterInput(ACCOUNT_FILTER_LABEL), KEYED_FILTERS.acctId);
    expect(FilterInput(ACCOUNT_FILTER_LABEL)).toHaveValue("");
    for (const field of screen.getAllByRole("combobox")) {
        expect(field).toHaveAttribute("aria-readonly", "true");
    }
    await user.click(SelectionField(SELECTED_ROW));
    expect(screen.queryByRole("listbox")).toBeNull();
}

function ExpectFiltersAndCodesUnlocked(): void {
    for (const label of FILTER_LABEL_LIST) {
        expect(FilterInput(label)).not.toHaveAttribute("readonly");
    }
    for (const field of screen.getAllByRole("combobox")) {
        expect(field).not.toHaveAttribute("aria-readonly");
    }
}

describe("COCRDLIC filters and codes locked while a list read is out on /cards", () => {
    it.each(KEYED_READS)(
        "COCRDLIC the $readName read keeps the filters and CRDSEL1-7 read-only until it answers",
        async ({ key }) => {
            const user = await RenderScreen();
            const settleRead = DeferNextListRead();
            await user.click(screen.getByRole("button", { name: KEY_BUTTON_NAMES[key] }));
            ExpectBusy();
            await ExpectFiltersAndCodesLocked(user);
            act(() => settleRead(NextPageResponse(PAGE_SIZE)));
            await WaitForIdle();
            ExpectFiltersAndCodesUnlocked();
            ExpectShownRows(NextPageResponse(PAGE_SIZE).rows);
            expect(SelectionCodesShown()).toEqual(RowCodesFor([]));
            expect(mockListCards).toHaveBeenCalledTimes(2);
        },
    );
});

// A physical key press dispatched straight to the window, where useFunctionKeys listens.
function DispatchKeyDown(key: ScreenKey): void {
    window.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
}

// Both presses land before React re-renders the disabled keys, so the request gate alone refuses
// the second: the first read is neither aborted nor joined by another.
describe("COCRDLIC keys pressed twice in one frame on /cards", () => {
    it.each(KEYED_READS)(
        "COCRDLIC two $key presses in one frame send one $readName read",
        async ({ key }) => {
            await RenderScreen();
            const settleRead = DeferNextListRead();
            act(() => {
                DispatchKeyDown(key);
                DispatchKeyDown(key);
            });
            expect(mockListCards).toHaveBeenCalledTimes(2);
            expect(ListReadSignal(1)?.aborted).toBe(false);
            act(() => settleRead(NextPageResponse(PAGE_SIZE)));
            await WaitForIdle();
            expect(mockListCards).toHaveBeenCalledTimes(2);
            expect(screen.getByText(`Page ${NEXT_PAGE}`)).toBeVisible();
        },
    );
});

interface ResultStatusCase {
    readName: string;
    key: ScreenKey;
    rowCount: number;
    statusText: string;
}

// The page number and row count of the page each key shows, a full, a one-row and an empty page.
const RESULT_STATUS_CASES: readonly ResultStatusCase[] = [
    { readName: "forward", key: "F8", rowCount: PAGE_SIZE, statusText: "Page 3: 7 cards" },
    { readName: "backward", key: "F7", rowCount: 1, statusText: "Page 3: 1 card" },
    { readName: "re-read", key: "Enter", rowCount: 0, statusText: "Page 3: no cards" },
];

// The first entry's page 2 of 7 cards, as BuildListResponse answers it.
const FIRST_ENTRY_STATUS = "Page 2: 7 cards";

// The first entry announces its page; the key's read empties the status while it is out and
// announces the page it shows. The status is client text, so ERRMSG and INFOMSG stay as answered.
async function AnnouncesEachPageShown({ key, rowCount, statusText }: ResultStatusCase) {
    const user = await RenderScreen();
    expect(ResultStatusRegion().textContent).toBe(FIRST_ENTRY_STATUS);
    expect(ResultStatusRegion()).toHaveStyle({ position: "absolute", width: "1px" });
    const settleRead = DeferNextListRead();
    await user.click(screen.getByRole("button", { name: KEY_BUTTON_NAMES[key] }));
    expect(ResultStatusRegion().textContent).toBe("");
    act(() => settleRead(NextPageResponse(rowCount)));
    await WaitForIdle();
    expect(ResultStatusRegion().textContent).toBe(statusText);
    expect(AlertTexts()).toEqual([RECORD_ACTIONS_INFO]);
}

async function LeavesStatusEmptyOnRefusal() {
    const user = await RenderScreen();
    mockListCards.mockRejectedValueOnce(BuildAxiosError(HTTP_BAD_REQUEST, FilterRefusalBody()));
    await PressScreenKey(user, "Enter", "click");
    await ExpectAlert(ACCOUNT_FILTER_MESSAGE, ERRMSG_SEVERITY);
    expect(ResultStatusRegion().textContent).toBe("");
}

async function AnnouncesUnactionedSelectionReread() {
    mockSelectCards.mockResolvedValueOnce(UNACTIONED_SELECTION);
    mockListCards
        .mockResolvedValueOnce(BuildListResponse())
        .mockResolvedValueOnce(NextPageResponse(PAGE_SIZE));
    await SubmitSelectedRow();
    await WaitForIdle();
    ExpectLastListRead(UNACTIONED_REREAD_QUERY);
    expect(ResultStatusRegion().textContent).toBe("Page 3: 7 cards");
}

describe("COCRDLIC polite result status on /cards", () => {
    it.each(RESULT_STATUS_CASES)(
        "COCRDLIC the $readName read empties the status, then announces $statusText",
        AnnouncesEachPageShown,
    );
    it("COCRDLIC a refused read leaves the status empty", LeavesStatusEmptyOnRefusal);
    it(
        "COCRDLIC the re-read after an unactioned selection announces its page",
        AnnouncesUnactionedSelectionReread,
    );
});
