// Screen tests: BMS COCRDUP, program COCRDUPC (CCUP), page /cards/update. Covers how the map
// fields render: EXPMON, the "/" literal and EXPYEAR grouped under the Expiry Date label exactly
// as COCRDSL groups them; CRDNAME on a full grid row with CRDSTCD and the expiry group beside each
// other from md, as on COCRDSL; CRDNAME wrapping in one native textarea row, protected or
// editable, with no hidden copy of the field rendered to measure it, that never holds a line
// break, with Enter in it running the screen's ENTER, while every other field keeps
// its text as keyed; and every field the server names in field_errors tied to the ERRMSG line,
// ahead of the Y/N hint CRDSTCD carries, while a field not in error keeps only its own hint.
import { ThemeProvider } from "@mui/material/styles";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import type { ReactNode } from "react";

import CardUpdatePage from "@/app/cards/update/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import {
    FetchCardForEdit,
    UpdateCard,
    type CardScreen,
    type CardScreenFields,
} from "@/lib/api/cards";
import { UpdateAction, UpdateScreenState, type LegacyErrorResponse } from "@/lib/apiClient";
import type { SessionContextValue } from "@/lib/session";

type SignedOnUser = NonNullable<SessionContextValue["currentUser"]>;
type ScreenUser = ReturnType<typeof userEvent.setup>;
type FieldDescriptions = Readonly<Record<string, string | null>>;

// The 400 body of a failed COCRDUPC edit: the legacy keys beside the screen it redisplays.
type CardErrorBody = LegacyErrorResponse & Pick<CardScreen, "fields" | "state" | "screen_token">;

// One refused ENTER: the text keyed into a field first, the server's answer, and the
// aria-describedby each field in error carries afterwards.
interface FieldErrorCase {
    caseName: string;
    // A detail field is keyed once a card is shown; a key error is answered on the key screen.
    isAfterFetch: boolean;
    keyed: readonly (readonly [string, string])[];
    body: CardErrorBody;
    descriptions: FieldDescriptions;
}

// One screen state the first ENTER is answered with, and whether it protects CRDNAME.
interface NameElementCase {
    state: UpdateScreenState;
    isReadOnly: boolean;
}

// One text pasted into an editable detail field of the shown card, and the text the field then
// holds and the next ENTER sends.
interface KeyedTextCase {
    caseName: string;
    label: string;
    name: keyof CardScreenFields;
    pasted: string;
    sent: string;
}

// One Enter press in the editable CRDNAME, as user-event key descriptors.
interface EnterPressCase {
    keyName: string;
    keys: string;
}

// Test values only: the card number is shown masked, so no card number is written in code.
const TEST_ACCOUNT_ID = "00000000004";
const TEST_CARD_REF = "card-ref-1";
const TEST_SCREEN_TOKEN = "screen-token-1";
const MASKED_CARD_NUMBER = "*".repeat(12) + "4312";
const TEST_EMBOSSED_NAME = "TEST CARD HOLDER";
// A CRDNAME at its BMS LENGTH of 50, wider than a half-row field.
const LONG_EMBOSSED_NAME = "ABCDEFGHIJKLMNOPQRSTUVWXYZ ABCDEFGHIJKLMNOPQRSTUVW";

// Stable router and query doubles; the "mock" prefix lets the hoisted factories read them. Every
// case enters from the main menu, with no query, and fetches its card by the keys it sends.
const mockRouter = {
    push: jest.fn(),
    replace: jest.fn(),
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};
const mockSearchParams = new URLSearchParams();
// A regular user (type U), as CCUP is a main-menu function; the session holds no password.
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
    usePathname: () => "/cards/update",
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

const mockFetchCardForEdit = jest.mocked(FetchCardForEdit);
const mockUpdateCard = jest.mocked(UpdateCard);

// The COCRDUP FKEYS legend button ENTER=Process.
const ENTER_BUTTON_NAME = "Process (Enter)";
// The COCRDUP INITIAL texts, internal blanks kept, and the expiry part labels.
const ACCOUNT_LABEL = "Account Number    :";
const CARD_LABEL = "Card Number       :";
const NAME_LABEL = "Name on card      :";
const STATUS_LABEL = "Card Active Y/N   :";
const MONTH_LABEL = "Month";
const YEAR_LABEL = "Year";
const FIELD_LABELS: readonly string[] = [
    ACCOUNT_LABEL,
    CARD_LABEL,
    NAME_LABEL,
    STATUS_LABEL,
    MONTH_LABEL,
    YEAR_LABEL,
];
// The Expiry Date INITIAL COCRDUP and COCRDSL share; accessible names collapse its blanks.
const EXPIRY_LABEL = "Expiry Date       :";
const EXPIRY_GROUP_NAME = /^Expiry Date\s+:$/;
const EXPIRY_SEPARATOR = "/";
// [part label, BMS LENGTH] of EXPMON and EXPYEAR, both keyed from a numeric keypad.
const EXPIRY_PART_CASES: readonly (readonly [string, string])[] = [
    [MONTH_LABEL, "2"],
    [YEAR_LABEL, "4"],
];

// The id of the MessageLine ERRMSG Alert, which a field named in field_errors is described by,
// and the id of the CRDSTCD hint, which MUI derives from the field's own id.
const ERRMSG_ID = "message-line-errmsg";
const STATUS_HELPER_ID = "card-update-active_status-helper-text";
const STATUS_HELPER_TEXT = "Y/N";
// CRDNAME keeps its id and BMS LENGTH whether it renders an input or a textarea.
const NAME_FIELD_ID = "card-update-embossed_name";
const NAME_MAX_LENGTH = "50";

// MUI 9 Grid classes: every Grid has MuiGrid-root, a container MuiGrid-container and its spacing
// MuiGrid-spacing-xs-<units>, and each size breakpoint MuiGrid-grid-<breakpoint>-<columns>.
const GRID_ROOT_CLASS = "MuiGrid-root";
const GRID_CONTAINER_CLASS = "MuiGrid-container";
const DETAIL_GRID_SPACING_CLASS = "MuiGrid-spacing-xs-4";
const GRID_SIZE_CLASS_PREFIX = "MuiGrid-grid-";
// Sorted for comparison: a full row at every width, and half a row from md.
const FULL_ROW_SIZE_CLASSES: readonly string[] = ["MuiGrid-grid-xs-12"];
const HALF_ROW_SIZE_CLASSES: readonly string[] = ["MuiGrid-grid-md-6", "MuiGrid-grid-xs-12"];

// CRDNAME wraps in one native textarea row whether the state leaves it editable or protects it;
// the browser grows the row to a longer value, so no hidden copy of the field measures it.
const NAME_ELEMENT_TAG = "TEXTAREA";
const NAME_ROWS = "1";
const NAME_ELEMENT_CASES: readonly NameElementCase[] = [
    { state: UpdateScreenState.SHOW_DETAILS, isReadOnly: false },
    { state: UpdateScreenState.CHANGES_NOT_OK, isReadOnly: false },
    { state: UpdateScreenState.CHANGES_OK_NOT_CONFIRMED, isReadOnly: true },
    { state: UpdateScreenState.CHANGES_DONE, isReadOnly: true },
];

// CRDNAME loses every CR and LF, as an <input type="text"> strips them, and keeps everything else;
// the other detail fields keep their text exactly as keyed.
const KEYED_TEXT_CASES: readonly KeyedTextCase[] = [
    {
        caseName: "CRDNAME drops an LF",
        label: NAME_LABEL,
        name: "embossed_name",
        pasted: "TEST\nHOLDER",
        sent: "TESTHOLDER",
    },
    {
        caseName: "CRDNAME drops a CR LF pair",
        label: NAME_LABEL,
        name: "embossed_name",
        pasted: "TEST\r\nHOLDER",
        sent: "TESTHOLDER",
    },
    {
        caseName: "CRDNAME drops trailing line breaks",
        label: NAME_LABEL,
        name: "embossed_name",
        pasted: "TEST HOLDER\r\n\n",
        sent: "TEST HOLDER",
    },
    {
        caseName: "CRDNAME keeps its blanks",
        label: NAME_LABEL,
        name: "embossed_name",
        pasted: " TEST  HOLDER ",
        sent: " TEST  HOLDER ",
    },
    {
        caseName: "CRDSTCD keeps a lower-case code",
        label: STATUS_LABEL,
        name: "active_status",
        pasted: "n",
        sent: "n",
    },
    {
        caseName: "EXPMON keeps a trailing blank",
        label: MONTH_LABEL,
        name: "expiry_month",
        pasted: "7 ",
        sent: "7 ",
    },
    {
        caseName: "EXPYEAR keeps a leading blank",
        label: YEAR_LABEL,
        name: "expiry_year",
        pasted: " 030",
        sent: " 030",
    },
];

// Enter alone, and with Shift held, which in a textarea would otherwise insert a line break.
const ENTER_PRESS_CASES: readonly EnterPressCase[] = [
    { keyName: "Enter", keys: "{Enter}" },
    { keyName: "Shift+Enter", keys: "{Shift>}{Enter}{/Shift}" },
];

// The cocrdupc.py literals of the failing edits.
const NO_INPUT_RECEIVED_MESSAGE = "No input received";
const CARD_STATUS_MESSAGE = "Card Active Status must be Y or N";
const EXPIRY_YEAR_MESSAGE = "Invalid card expiry year";
const DETAILS_SHOWN_INFO = "Details of selected card shown above";
const HTTP_EDIT_FAILURE = 400;

// Before and between errors only CRDSTCD is described, by its own hint.
const DESCRIPTIONS_WITHOUT_ERRORS: FieldDescriptions = Object.fromEntries(
    FIELD_LABELS.map((label) => [label, label === STATUS_LABEL ? STATUS_HELPER_ID : null]),
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

// The fetched card's fields: the number masked and the day kept dark.
function BuildCardFields(overrides: Partial<CardScreenFields> = {}): CardScreenFields {
    return {
        acct_id: TEST_ACCOUNT_ID,
        card_num: MASKED_CARD_NUMBER,
        embossed_name: TEST_EMBOSSED_NAME,
        active_status: "Y",
        expiry_month: "07",
        expiry_year: "2027",
        expiry_day: "31",
        ...overrides,
    };
}

// The SHOW_DETAILS screen the first ENTER on the typed keys is answered with.
function BuildCardScreen(): CardScreen {
    return {
        fields: BuildCardFields(),
        state: UpdateScreenState.SHOW_DETAILS,
        screen_token: TEST_SCREEN_TOKEN,
        card_ref: TEST_CARD_REF,
        message: "",
        info_message: DETAILS_SHOWN_INFO,
        field_errors: [],
        cursor_field: "embossed_name",
    };
}

// The 50-character name shown in the state the first ENTER is answered with.
function BuildLongNameScreen(state: UpdateScreenState): CardScreen {
    return {
        ...BuildCardScreen(),
        fields: BuildCardFields({ embossed_name: LONG_EMBOSSED_NAME }),
        state,
    };
}

// A detail edit refused with CHANGES_NOT_OK, the keyed value redisplayed in its field.
function DetailErrorBody(
    field: keyof CardScreenFields,
    message: string,
    value: string,
): CardErrorBody {
    return {
        message,
        info_message: "",
        field_errors: [field],
        cursor_field: field,
        fields: BuildCardFields({ [field]: value }),
        state: UpdateScreenState.CHANGES_NOT_OK,
        screen_token: TEST_SCREEN_TOKEN,
    };
}

// Both keys blank on the key screen fail both key edits, the cursor on the account.
const BLANK_KEYS_BODY: CardErrorBody = {
    message: NO_INPUT_RECEIVED_MESSAGE,
    info_message: "",
    field_errors: ["acct_id", "card_num"],
    cursor_field: "acct_id",
    fields: BuildCardFields({ acct_id: "", card_num: "", embossed_name: "" }),
    state: UpdateScreenState.DETAILS_NOT_FETCHED,
    screen_token: "",
};

const FIELD_ERROR_CASES: readonly FieldErrorCase[] = [
    {
        caseName: "both keys blank",
        isAfterFetch: false,
        keyed: [],
        body: BLANK_KEYS_BODY,
        descriptions: { [ACCOUNT_LABEL]: ERRMSG_ID, [CARD_LABEL]: ERRMSG_ID },
    },
    {
        caseName: "expiry year 1949",
        isAfterFetch: true,
        keyed: [[YEAR_LABEL, "1949"]],
        body: DetailErrorBody("expiry_year", EXPIRY_YEAR_MESSAGE, "1949"),
        descriptions: { [YEAR_LABEL]: ERRMSG_ID },
    },
    {
        caseName: "active status X",
        isAfterFetch: true,
        keyed: [[STATUS_LABEL, "X"]],
        body: DetailErrorBody("active_status", CARD_STATUS_MESSAGE, "X"),
        descriptions: { [STATUS_LABEL]: `${ERRMSG_ID} ${STATUS_HELPER_ID}` },
    },
];

function BuildAxiosError(status: number, body: CardErrorBody): AxiosError {
    const config = { headers: new AxiosHeaders() };
    const response: AxiosResponse<CardErrorBody> = {
        data: body,
        status,
        statusText: "",
        headers: {},
        config,
    };
    const message = `Request failed with status code ${status}`;
    return new AxiosError(message, AxiosError.ERR_BAD_REQUEST, config, null, response);
}

function RenderPage(): ScreenUser {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <CardUpdatePage />
        </ThemeProvider>,
    );
    return user;
}

// The default normalizer trims and collapses the blanks the COCRDUP labels keep.
function KeepRawText(text: string): string {
    return text;
}

function InputFor(label: string): HTMLElement {
    return screen.getByLabelText(label, { normalizer: KeepRawText });
}

function DescriptionsByLabel(): FieldDescriptions {
    return Object.fromEntries(
        FIELD_LABELS.map((label) => [label, InputFor(label).getAttribute("aria-describedby")]),
    );
}

// Each field in error is described by ERRMSG, CRDSTCD's hint after it; every other field keeps
// the description it had before the answer.
function DescriptionsAfterErrors(descriptions: FieldDescriptions): FieldDescriptions {
    return { ...DESCRIPTIONS_WITHOUT_ERRORS, ...descriptions };
}

function InvalidLabels(): string[] {
    return FIELD_LABELS.filter((label) => InputFor(label).getAttribute("aria-invalid") === "true");
}

// Every id a field is described by names an element on the screen: ERRMSG or the CRDSTCD hint.
function ExpectDescriptionsResolve(descriptions: FieldDescriptions): void {
    const ids = Object.values(descriptions).flatMap((description) => description?.split(" ") ?? []);
    for (const id of ids) {
        expect(document.getElementById(id)).not.toBeNull();
    }
    expect(document.getElementById(STATUS_HELPER_ID)).toHaveTextContent(STATUS_HELPER_TEXT);
}

async function ClickEnter(user: ScreenUser): Promise<void> {
    await user.click(screen.getByRole("button", { name: ENTER_BUTTON_NAME }));
}

// The first ENTER from the menu's key screen answered with the card in SHOW_DETAILS.
async function ShowFetchedCard(user: ScreenUser): Promise<void> {
    mockUpdateCard.mockResolvedValueOnce(BuildCardScreen());
    await ClickEnter(user);
    expect(await screen.findByDisplayValue(TEST_EMBOSSED_NAME)).toBeInTheDocument();
}

// The field keeps its focus after it is cleared, so the paste lands in it.
async function PasteInto(user: ScreenUser, label: string, text: string): Promise<void> {
    await user.clear(InputFor(label));
    await user.paste(text);
}

// A request holds the screen, ENTER disabled, until its answer is shown.
async function WaitForScreenFree(): Promise<void> {
    await waitFor(() => {
        expect(screen.getByRole("button", { name: ENTER_BUTTON_NAME })).toBeEnabled();
    });
}

// The ENTER after the fetch is the second write, carrying the shown card's screen_token.
function ExpectSecondEnterSent(fields: Partial<CardScreenFields>): void {
    expect(mockUpdateCard).toHaveBeenCalledTimes(2);
    expect(mockUpdateCard.mock.lastCall?.[1]).toMatchObject({
        action: UpdateAction.ENTER,
        screen_token: TEST_SCREEN_TOKEN,
        fields,
    });
}

async function KeyTexts(user: ScreenUser, keyed: FieldErrorCase["keyed"]): Promise<void> {
    for (const [label, text] of keyed) {
        await user.clear(InputFor(label));
        await user.type(InputFor(label), text);
    }
}

async function WaitForErrorMessage(message: string): Promise<void> {
    await waitFor(() => {
        expect(screen.getByRole("alert")).toHaveTextContent(message);
    });
}

function ClosestOf(element: Element, className: string): Element {
    const ancestor = element.closest(`.${className}`);
    if (ancestor === null) {
        throw new Error(`The element sits in no .${className}`);
    }
    return ancestor;
}

// The Grid items of CRDNAME, CRDSTCD and the expiry group, in map order.
function DetailGridItems(): Element[] {
    const expiryGroup = screen.getByRole("group", { name: EXPIRY_GROUP_NAME });
    const elements = [InputFor(NAME_LABEL), InputFor(STATUS_LABEL), expiryGroup];
    return elements.map((element) => ClosestOf(element, GRID_ROOT_CLASS));
}

// The Grid size classes the item carries, MuiGrid-grid-<breakpoint>-<columns>, sorted.
function GridSizeClasses(gridItem: Element): string[] {
    return Array.from(gridItem.classList)
        .filter((className) => className.startsWith(GRID_SIZE_CLASS_PREFIX))
        .sort();
}

// clearMocks keeps queued answers, so each case starts from empty fetch and write queues, and
// from a session store that holds nothing another screen left behind.
beforeEach(() => {
    jest.useFakeTimers(CLOCK_ONLY_FAKE_TIMERS);
    window.sessionStorage.clear();
    mockFetchCardForEdit.mockReset();
    mockUpdateCard.mockReset();
});

afterEach(() => {
    jest.useRealTimers();
});

// The same group COCRDSL renders: a labelLarge paragraph that labels the row of parts.
describe("COCRDUPC BMS COCRDUP expiry group on /cards/update", () => {
    it("COCRDUPC groups EXPMON, the / literal and EXPYEAR under the Expiry Date label", () => {
        RenderPage();
        const groupLabel = screen.getByText(EXPIRY_LABEL, { normalizer: KeepRawText });
        expect(groupLabel.tagName).toBe("P");
        expect(groupLabel).toHaveClass("MuiTypography-labelLarge");
        const expiryGroup = screen.getByRole("group", { name: EXPIRY_GROUP_NAME });
        expect(expiryGroup).toHaveAttribute("aria-labelledby", groupLabel.id);
        const separator = within(expiryGroup).getByText(EXPIRY_SEPARATOR);
        expect(separator).toHaveAttribute("aria-hidden", "true");
        for (const [label, maxLength] of EXPIRY_PART_CASES) {
            const part = within(expiryGroup).getByLabelText(label);
            expect(part).toHaveAttribute("maxlength", maxLength);
            expect(part).toHaveAttribute("inputmode", "numeric");
        }
    });
});

describe("COCRDUPC BMS COCRDUP detail rows on /cards/update", () => {
    it("COCRDUPC puts CRDNAME on a full row and CRDSTCD beside the expiry group", () => {
        RenderPage();
        const gridItems = DetailGridItems();
        expect(gridItems.map(GridSizeClasses)).toEqual([
            FULL_ROW_SIZE_CLASSES,
            HALF_ROW_SIZE_CLASSES,
            HALF_ROW_SIZE_CLASSES,
        ]);
        const container = gridItems[0]?.parentElement;
        expect(container).toHaveClass(GRID_CONTAINER_CLASS, DETAIL_GRID_SPACING_CLASS);
        for (const gridItem of gridItems) {
            expect(gridItem.parentElement).toBe(container);
        }
    });
});

describe("COCRDUPC BMS COCRDUP CRDNAME by protection on /cards/update", () => {
    it.each(NAME_ELEMENT_CASES)(
        "COCRDUPC $state shows all 50 CRDNAME characters in a native textarea, no hidden copy",
        async ({ state, isReadOnly }) => {
            const user = RenderPage();
            mockUpdateCard.mockResolvedValueOnce(BuildLongNameScreen(state));
            await ClickEnter(user);
            expect(await screen.findByDisplayValue(LONG_EMBOSSED_NAME)).toBeInTheDocument();
            const nameField = InputFor(NAME_LABEL);
            expect(nameField.tagName).toBe(NAME_ELEMENT_TAG);
            expect(nameField).toHaveValue(LONG_EMBOSSED_NAME);
            expect(nameField.hasAttribute("readonly")).toBe(isReadOnly);
            expect(nameField).toHaveAttribute("id", NAME_FIELD_ID);
            expect(nameField).toHaveAttribute("maxlength", NAME_MAX_LENGTH);
            expect(nameField).toHaveAttribute("rows", NAME_ROWS);
            expect(nameField).not.toHaveAttribute("type");
            expect(document.querySelectorAll("textarea[aria-hidden]")).toHaveLength(0);
        },
    );
});

// A 3270 field holds no line break and the server refuses CR and LF, so neither is ever sent.
describe("COCRDUPC BMS COCRDUP detail text as keyed on /cards/update", () => {
    it.each(KEYED_TEXT_CASES)(
        "COCRDUPC $caseName in the field and in the ENTER it sends",
        async ({ label, name, pasted, sent }) => {
            const user = RenderPage();
            await ShowFetchedCard(user);
            await PasteInto(user, label, pasted);
            expect(InputFor(label)).toHaveValue(sent);
            mockUpdateCard.mockResolvedValueOnce(BuildCardScreen());
            await ClickEnter(user);
            await WaitForScreenFree();
            ExpectSecondEnterSent({ [name]: sent });
        },
    );
});

// useFunctionKeys takes Enter in the textarea as the screen's ENTER and blocks its line break.
describe("COCRDUPC BMS COCRDUP Enter in CRDNAME on /cards/update", () => {
    it.each(ENTER_PRESS_CASES)(
        "COCRDUPC $keyName in the editable CRDNAME runs ENTER and inserts no line break",
        async ({ keys }) => {
            const user = RenderPage();
            await ShowFetchedCard(user);
            const nameField = InputFor(NAME_LABEL);
            const inputEvents = jest.fn();
            nameField.addEventListener("input", inputEvents);
            await user.click(nameField);
            mockUpdateCard.mockResolvedValueOnce(BuildCardScreen());
            await user.keyboard(keys);
            await WaitForScreenFree();
            expect(inputEvents).not.toHaveBeenCalled();
            ExpectSecondEnterSent({ embossed_name: TEST_EMBOSSED_NAME });
        },
    );
});

describe("COCRDUPC field errors tied to the ERRMSG line on /cards/update", () => {
    it.each(FIELD_ERROR_CASES)(
        "COCRDUPC $caseName: fields in error are invalid and described by ERRMSG",
        async ({ isAfterFetch, keyed, body, descriptions }) => {
            const user = RenderPage();
            if (isAfterFetch) {
                await ShowFetchedCard(user);
            }
            expect(DescriptionsByLabel()).toEqual(DESCRIPTIONS_WITHOUT_ERRORS);
            await KeyTexts(user, keyed);
            mockUpdateCard.mockRejectedValueOnce(BuildAxiosError(HTTP_EDIT_FAILURE, body));
            await ClickEnter(user);
            await WaitForErrorMessage(body.message);
            expect(InvalidLabels()).toEqual(Object.keys(descriptions));
            expect(DescriptionsByLabel()).toEqual(DescriptionsAfterErrors(descriptions));
            ExpectDescriptionsResolve(descriptions);
        },
    );
});
