// Screen tests: BMS COCRDSL, program COCRDSLC (CCDL), page /cards/view. Covers how the detail
// rows lay out: CRDNAME (row 11) on a full grid row, CRDSTCD (row 13) and the expiry parts
// (row 15) side by side from md, in the grid COCRDUP uses, and every ASKIP detail field filling
// its cell, with the 50-character CRDNAME shown whole in a wrapping read-only field: one native
// textarea row, with no hidden copy of the field rendered to measure it.
import { ThemeProvider } from "@mui/material/styles";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";

import CardViewPage from "@/app/cards/view/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import { ViewSelectedCard, type CardDetailResponse } from "@/lib/api/cards";
import type { SessionContextValue } from "@/lib/session";

type SignedOnUser = NonNullable<SessionContextValue["currentUser"]>;

// One detail field: its label and the element it renders, textarea for a wrapping field.
interface DetailElementCase {
    label: string;
    tagName: "INPUT" | "TEXTAREA";
}

// Stable router and query doubles; the "mock" prefix lets the hoisted factories read them. Every
// case arrives from a COCRDLIC selection, so the card is read on arrival.
const mockRouter = {
    push: jest.fn(),
    replace: jest.fn(),
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};
// Test keys only: the card number is shown masked, so no card number is written in code.
const TEST_ACCOUNT_ID = "00000000004";
const TEST_CARD_REF = "card-ref-1";
const MASKED_CARD_NUMBER = "*".repeat(12) + "4312";
const mockSearchParams = new URLSearchParams({ cardRef: TEST_CARD_REF, acctId: TEST_ACCOUNT_ID });
// A regular user (type U), as CCDL is a main-menu function; the session holds no password.
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

// The arrival from a COCRDLIC selection reads the card by its card_ref.
const mockViewSelectedCard = jest.mocked(ViewSelectedCard);

// A CRDNAME at its BMS LENGTH of 50, wider than a single-line field at narrow widths.
const LONG_EMBOSSED_NAME = "ABCDEFGHIJKLMNOPQRSTUVWXYZ ABCDEFGHIJKLMNOPQRSTUVW";
const NAME_MAX_LENGTH = "50";
// CRDNAME keeps one row while its value fits; the browser grows it to a longer value.
const NAME_ROWS = "1";
const DETAILS_SHOWN_INFO = "   Displaying requested details";

// The COCRDSL INITIAL texts, internal blanks kept, and the expiry part labels.
const NAME_LABEL = "Name on card      :";
const STATUS_LABEL = "Card Active Y/N   :";
const MONTH_LABEL = "Month";
const YEAR_LABEL = "Year";
// Accessible names collapse the Expiry Date label's internal blanks.
const EXPIRY_GROUP_NAME = /^Expiry Date\s+:$/;

// MUI 9 Grid classes: every Grid has MuiGrid-root, a container MuiGrid-container and its spacing
// MuiGrid-spacing-xs-<units>, and each size breakpoint MuiGrid-grid-<breakpoint>-<columns>.
const GRID_ROOT_CLASS = "MuiGrid-root";
const GRID_CONTAINER_CLASS = "MuiGrid-container";
const DETAIL_GRID_SPACING_CLASS = "MuiGrid-spacing-xs-4";
const GRID_SIZE_CLASS_PREFIX = "MuiGrid-grid-";
// Sorted for comparison: a full row at every width, and half a row from md.
const FULL_ROW_SIZE_CLASSES: readonly string[] = ["MuiGrid-grid-xs-12"];
const HALF_ROW_SIZE_CLASSES: readonly string[] = ["MuiGrid-grid-md-6", "MuiGrid-grid-xs-12"];
// A fullWidth TextField marks its FormControl root, and a multiline one its InputBase root.
const FORM_CONTROL_ROOT_CLASS = "MuiFormControl-root";
const FULL_WIDTH_FIELD_CLASS = "MuiFormControl-fullWidth";
const INPUT_BASE_ROOT_CLASS = "MuiInputBase-root";
const MULTILINE_INPUT_CLASS = "MuiInputBase-multiline";

// Every timer API but the interval pair stays real, so the Testing Library waits keep their real
// setTimeout while the ScreenShell clock never ticks outside act on a slow run.
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

// Only CRDNAME can outgrow its field, so only it wraps; the code and date parts stay inputs.
const DETAIL_ELEMENT_CASES: readonly DetailElementCase[] = [
    { label: NAME_LABEL, tagName: "TEXTAREA" },
    { label: STATUS_LABEL, tagName: "INPUT" },
    { label: MONTH_LABEL, tagName: "INPUT" },
    { label: YEAR_LABEL, tagName: "INPUT" },
];

// The 1200-SETUP-SCREEN-VARS answer for a found card: the number masked and no CVV.
function BuildCardDetail(): CardDetailResponse {
    return {
        acct_id: TEST_ACCOUNT_ID,
        card_ref: TEST_CARD_REF,
        card_num_masked: MASKED_CARD_NUMBER,
        embossed_name: LONG_EMBOSSED_NAME,
        active_status: "Y",
        expiry_month: "07",
        expiry_year: "2027",
        message: "",
        info_message: DETAILS_SHOWN_INFO,
        field_errors: [],
        cursor_field: "acct_id",
    };
}

// The default normalizer trims and collapses the blanks the COCRDSL labels keep.
function KeepRawText(text: string): string {
    return text;
}

function InputFor(label: string): HTMLElement {
    return screen.getByLabelText(label, { normalizer: KeepRawText });
}

// The selected card's details, read on arrival from the card list.
async function RenderShownCard(): Promise<void> {
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <CardViewPage />
        </ThemeProvider>,
    );
    expect(await screen.findByDisplayValue(LONG_EMBOSSED_NAME)).toBeInTheDocument();
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

// Each case reads the card afresh, from a session store that holds nothing another screen left.
beforeEach(() => {
    jest.useFakeTimers(CLOCK_ONLY_FAKE_TIMERS);
    window.sessionStorage.clear();
    mockViewSelectedCard.mockReset();
    mockViewSelectedCard.mockResolvedValue(BuildCardDetail());
});

afterEach(() => {
    jest.useRealTimers();
});

describe("COCRDSLC BMS COCRDSL detail rows on /cards/view", () => {
    it("COCRDSLC puts CRDNAME on a full row and CRDSTCD beside the expiry group", async () => {
        await RenderShownCard();
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

describe("COCRDSLC BMS COCRDSL detail fields on /cards/view", () => {
    it.each(DETAIL_ELEMENT_CASES)(
        "COCRDSLC detail field $label is a read-only full-width $tagName",
        async ({ label, tagName }) => {
            await RenderShownCard();
            const field = InputFor(label);
            expect(field.tagName).toBe(tagName);
            expect(field).toHaveAttribute("readonly");
            expect(ClosestOf(field, FORM_CONTROL_ROOT_CLASS)).toHaveClass(FULL_WIDTH_FIELD_CLASS);
        },
    );
});

describe("COCRDSLC BMS COCRDSL CRDNAME at its BMS LENGTH on /cards/view", () => {
    it("COCRDSLC shows all 50 CRDNAME characters in the wrapping field", async () => {
        await RenderShownCard();
        const nameField = InputFor(NAME_LABEL);
        expect(nameField).toHaveValue(LONG_EMBOSSED_NAME);
        expect(nameField).toHaveAttribute("maxlength", NAME_MAX_LENGTH);
        expect(ClosestOf(nameField, INPUT_BASE_ROOT_CLASS)).toHaveClass(MULTILINE_INPUT_CLASS);
    });
});

// The browser sizes the native CRDNAME textarea to its value, so no hidden measuring copy of the
// field is rendered, and the one textarea on the screen is CRDNAME, with its own id and no type.
describe("COCRDSLC BMS COCRDSL CRDNAME element on /cards/view", () => {
    it("COCRDSLC renders CRDNAME as one native textarea row and no hidden copy", async () => {
        await RenderShownCard();
        const nameField = InputFor(NAME_LABEL);
        expect(Array.from(document.querySelectorAll("textarea"))).toEqual([nameField]);
        expect(document.querySelectorAll("textarea[aria-hidden]")).toHaveLength(0);
        expect(nameField).toHaveAttribute("rows", NAME_ROWS);
        expect(nameField).not.toHaveAttribute("type");
        expect(nameField.id).not.toBe("");
    });
});
