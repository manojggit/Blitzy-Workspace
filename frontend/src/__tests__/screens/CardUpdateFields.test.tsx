// Screen tests: BMS COCRDUP, program COCRDUPC (CCUP), page /cards/update. Covers how the map
// fields render: EXPMON, the "/" literal and EXPYEAR grouped under the Expiry Date label exactly
// as COCRDSL groups them, and every field the server names in field_errors tied to the ERRMSG
// line, ahead of the Y/N hint CRDSTCD carries, while a field not in error keeps only its own hint.
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
import { UpdateScreenState, type LegacyErrorResponse } from "@/lib/apiClient";
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

// Test values only: the card number is shown masked, so no card number is written in code.
const TEST_ACCOUNT_ID = "00000000004";
const TEST_CARD_REF = "card-ref-1";
const TEST_SCREEN_TOKEN = "screen-token-1";
const MASKED_CARD_NUMBER = "*".repeat(12) + "4312";
const TEST_EMBOSSED_NAME = "TEST CARD HOLDER";

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
