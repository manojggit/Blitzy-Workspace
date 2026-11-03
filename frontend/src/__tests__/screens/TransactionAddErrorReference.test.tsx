// Screen tests: BMS COTRN02, program COTRN02C (CT02), page /transactions/add. Covers only the
// description of a field the server names in field_errors: the ERRMSG line holding the COTRN02C
// literal comes first, then the field's own format or Y/N hint, while every field the answer does
// not name keeps only its hint. Literals are copied verbatim from
// backend/app/services/transactions/cotrn02c.py; labels and hints from bms/COTRN02.bms.
import { ThemeProvider } from "@mui/material/styles";
import { render, screen } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import type { ReactNode } from "react";

import { CARD_DEMO_THEME } from "@/app/theme";
import TransactionAddPage from "@/app/transactions/add/page";
import { AddTransaction } from "@/lib/api/transactions";
import type { LegacyErrorResponse } from "@/lib/apiClient";
import type { SessionContextValue } from "@/lib/session";

type SignedOnUser = NonNullable<SessionContextValue["currentUser"]>;

// A COTRN02C edit failure: the field it names, that field's label and hint, and the literal.
interface FieldErrorCase {
    field: string;
    label: string;
    hint: string | null;
    message: string;
}

// Stable router double; the "mock" prefix lets the hoisted factory read it.
const mockRouter = {
    push: jest.fn(),
    replace: jest.fn(),
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};
// A regular user (type U), the role that owns CT02; the session holds no password.
const MOCK_CURRENT_USER: SignedOnUser = {
    user_id: "TESTUSR1",
    user_type: "U",
    first_name: "Test",
    last_name: "User",
    next_route: "/menu",
};

jest.mock("next/navigation", () => ({
    useRouter: () => mockRouter,
    useSearchParams: () => new URLSearchParams(),
    usePathname: () => "/transactions/add",
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

// Only the network calls are replaced; the module's types and enums stay real.
jest.mock("@/lib/api/transactions", () => ({
    ...jest.requireActual<object>("@/lib/api/transactions"),
    ListTransactions: jest.fn(),
    SelectTransactions: jest.fn(),
    ViewTransaction: jest.fn(),
    AddTransaction: jest.fn(),
}));

const mockAddTransaction = jest.mocked(AddTransaction);

// The id of the MessageLine ERRMSG Alert, and the ENTER=Continue legend button.
const ERRMSG_ID = "message-line-errmsg";
const ENTER_BUTTON_NAME = "Continue (Enter)";
const HTTP_EDIT_FAILURE = 400;

// A frozen clock keeps the ScreenShell CURDATE/CURTIME store unchanged, so its one-second tick
// never re-renders the header outside act().
const FIXED_NOW_MILLISECONDS = new Date(2022, 6, 18, 9, 5, 7).getTime();

// ACTIDIN has no hint, AMTI the (-99999999.99) picture beside it, CONFIRMI the (Y/N) text.
const FIELD_ERROR_CASES: readonly FieldErrorCase[] = [
    {
        field: "acct_id",
        label: "Enter Acct #:",
        hint: null,
        message: "Account or Card Number must be entered...",
    },
    {
        field: "amount",
        label: "Amount:",
        hint: "(-99999999.99)",
        message: "Amount should be in format -99999999.99",
    },
    {
        field: "confirm",
        label: "You are about to add this transaction. Please confirm :",
        hint: "Y/N",
        message: "Invalid value. Valid values are (Y/N)...",
    },
];

// The 400 LegacyMessageError(message, field) body COTRN02C's first failing edit raises.
function BuildFieldError({ field, message }: FieldErrorCase): AxiosError {
    const body: LegacyErrorResponse = {
        message,
        info_message: "",
        field_errors: [field],
        cursor_field: field,
    };
    const config = { headers: new AxiosHeaders() };
    const response: AxiosResponse = {
        status: HTTP_EDIT_FAILURE,
        statusText: "",
        headers: {},
        config,
        data: body,
    };
    return new AxiosError("Request failed", AxiosError.ERR_BAD_REQUEST, config, null, response);
}

function RenderScreen(): UserEvent {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <TransactionAddPage />
        </ThemeProvider>,
    );
    return user;
}

// The outlined notch repeats each label in a legend, so the input is found by role and name.
function FieldInput({ label }: FieldErrorCase): HTMLElement {
    return screen.getByRole("textbox", { name: label.trim() });
}

// The flagged field names the ERRMSG Alert first, then the MUI helper text of its hint, if any.
function ExpectFlaggedDescription(fieldCase: FieldErrorCase): void {
    const input = FieldInput(fieldCase);
    const helperTextId = `${input.id}-helper-text`;
    const hintIds = fieldCase.hint === null ? [] : [helperTextId];
    const hintTexts = fieldCase.hint === null ? [] : [fieldCase.hint];
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAttribute("aria-describedby", [ERRMSG_ID, ...hintIds].join(" "));
    expect(document.getElementById(helperTextId)?.textContent).toBe(fieldCase.hint ?? undefined);
    expect(input).toHaveAccessibleDescription([fieldCase.message, ...hintTexts].join(" "));
}

// Each field the answer does not name keeps only its own hint, or no description at all.
function ExpectOtherFieldsUnflagged(flaggedField: string): void {
    const otherCases = FIELD_ERROR_CASES.filter((fieldCase) => fieldCase.field !== flaggedField);
    for (const fieldCase of otherCases) {
        expect(FieldInput(fieldCase)).toHaveAttribute("aria-invalid", "false");
        expect(FieldInput(fieldCase)).toHaveAccessibleDescription(fieldCase.hint ?? "");
    }
}

beforeEach(() => {
    mockAddTransaction.mockReset();
    jest.spyOn(Date, "now").mockReturnValue(FIXED_NOW_MILLISECONDS);
    window.sessionStorage.clear();
});

afterEach(() => {
    jest.restoreAllMocks();
});

describe("COTRN02C field named in field_errors", () => {
    it.each(FIELD_ERROR_CASES)(
        "COTRN02C 400 naming $field puts ERRMSG ahead of its hint ($hint) in its description",
        async (fieldCase) => {
            mockAddTransaction.mockRejectedValue(BuildFieldError(fieldCase));
            const user = RenderScreen();
            await user.click(screen.getByRole("button", { name: ENTER_BUTTON_NAME }));
            const alert = await screen.findByRole("alert");
            expect(alert.textContent).toBe(fieldCase.message);
            expect(document.getElementById(ERRMSG_ID)).toBe(alert);
            ExpectFlaggedDescription(fieldCase);
            ExpectOtherFieldsUnflagged(fieldCase.field);
        },
    );
});
