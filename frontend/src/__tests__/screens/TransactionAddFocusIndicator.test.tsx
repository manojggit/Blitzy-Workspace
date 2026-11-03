// Screen tests: BMS COTRN02, program COTRN02C (CT02), page /transactions/add. Covers only the
// focus indicator when the answer's cursor_field names the input that held the cursor as ENTER
// was pressed: the keyboard lock disables that input while the call runs, and once the answer is
// applied the input is again the focused element in MUI's focused state, with the focused outline
// and, where the field has a floating label, that label shrunk. Literals are copied verbatim from
// backend/app/services/transactions/cotrn02c.py; labels from bms/COTRN02.bms.
import { ThemeProvider } from "@mui/material/styles";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import type { ReactNode } from "react";

import { CARD_DEMO_THEME } from "@/app/theme";
import TransactionAddPage from "@/app/transactions/add/page";
import {
    AddTransaction,
    type TransactionAddFields,
    type TransactionAddResponse,
} from "@/lib/api/transactions";
import type { LegacyErrorResponse } from "@/lib/apiClient";
import type { SessionContextValue } from "@/lib/session";

type SignedOnUser = NonNullable<SessionContextValue["currentUser"]>;

interface PendingAdd {
    Answer: (response: TransactionAddResponse) => void;
    Fail: (error: unknown) => void;
}

// The input focused before ENTER, the account keyed first ("" for none), and the COTRN02C answer
// that names that same input as cursor_field.
interface SameFieldCase {
    field: string;
    label: string;
    keyedAccount: string;
    message: string;
    // The data-shrink of the field's own floating label: "true" for acct_id, whose blank value
    // leaves only the focus to shrink it; null for confirm, whose prompt is a form label above the
    // field, so its outlined field has no floating label for the caret to overlap.
    floatingLabelShrink: string | null;
    SettlePending: (pendingAdd: PendingAdd) => void;
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

const ACCOUNT_LABEL = "Enter Acct #:";
const CONFIRM_LABEL = "You are about to add this transaction. Please confirm :";
const TEST_ACCOUNT_ID = "00000000004";
const HTTP_EDIT_FAILURE = 400;
// MUI's class on the InputBase root of the input that holds the focus.
const MUI_INPUT_ROOT_SELECTOR = ".MuiInputBase-root";
const MUI_FOCUSED_CLASS = "Mui-focused";
// MUI's class on the root of each TextField, which holds that field's floating label, if any.
const MUI_FORM_CONTROL_SELECTOR = ".MuiFormControl-root";

// backend/app/services/transactions/cotrn02c.py: the blank-key edit and the confirm prompt.
const KEY_REQUIRED_MESSAGE = "Account or Card Number must be entered...";
const CONFIRM_PROMPT_MESSAGE = "Confirm to add this transaction...";

// A frozen clock keeps the ScreenShell CURDATE/CURTIME store unchanged, so its one-second tick
// never re-renders the header outside act().
const FIXED_NOW_MILLISECONDS = new Date(2022, 6, 18, 9, 5, 7).getTime();

// The screen COTRN02C sends back with the prompt: the keyed account, every other field blank.
const PROMPTED_FIELDS: TransactionAddFields = {
    acct_id: TEST_ACCOUNT_ID,
    card_num: "",
    type_cd: "",
    cat_cd: "",
    source: "",
    desc: "",
    amount: "",
    orig_date: "",
    proc_date: "",
    merchant_id: "",
    merchant_name: "",
    merchant_city: "",
    merchant_zip: "",
    confirm: "",
};

// The 400 LegacyMessageError body of VALIDATE-INPUT-KEY-FIELDS with both keys blank.
const KEY_REQUIRED_BODY: LegacyErrorResponse = {
    message: KEY_REQUIRED_MESSAGE,
    info_message: "",
    field_errors: ["acct_id"],
    cursor_field: "acct_id",
};

// The rejection axios raises for the 400 answer to POST /transactions.
function BuildKeyRequiredFailure(): AxiosError {
    const config = { headers: new AxiosHeaders() };
    const response: AxiosResponse = {
        status: HTTP_EDIT_FAILURE,
        statusText: "",
        headers: {},
        config,
        data: KEY_REQUIRED_BODY,
    };
    return new AxiosError("Request failed", AxiosError.ERR_BAD_REQUEST, config, null, response);
}

function FailKeyRequired(pendingAdd: PendingAdd): void {
    pendingAdd.Fail(BuildKeyRequiredFailure());
}

// The confirm prompt COTRN02C sends with HTTP 200 while CONFIRMI is blank.
function AnswerWithPrompt(pendingAdd: PendingAdd): void {
    pendingAdd.Answer({
        message: CONFIRM_PROMPT_MESSAGE,
        cursor_field: "confirm",
        fields: PROMPTED_FIELDS,
    });
}

// Each value left blank, so only the focus can shrink a floating label.
const SAME_FIELD_CASES: readonly SameFieldCase[] = [
    {
        field: "acct_id",
        label: ACCOUNT_LABEL,
        keyedAccount: "",
        message: KEY_REQUIRED_MESSAGE,
        floatingLabelShrink: "true",
        SettlePending: FailKeyRequired,
    },
    {
        field: "confirm",
        label: CONFIRM_LABEL,
        keyedAccount: TEST_ACCOUNT_ID,
        message: CONFIRM_PROMPT_MESSAGE,
        floatingLabelShrink: null,
        SettlePending: AnswerWithPrompt,
    },
];

function RenderScreen(): UserEvent {
    const user = userEvent.setup();
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <TransactionAddPage />
        </ThemeProvider>,
    );
    return user;
}

// Every AddTransaction call stays pending until the test settles it through its handles.
function DeferEveryAdd(): PendingAdd[] {
    const pendingAdds: PendingAdd[] = [];
    mockAddTransaction.mockImplementation(() => {
        const { promise, resolve, reject } = Promise.withResolvers<TransactionAddResponse>();
        pendingAdds.push({ Answer: resolve, Fail: reject });
        return promise;
    });
    return pendingAdds;
}

// The outlined notch repeats each label in a legend, so the input is found by role and name.
function FieldInput(label: string): HTMLInputElement {
    return screen.getByRole<HTMLInputElement>("textbox", { name: label });
}

// Keys the case's account, if any, then puts the cursor on its input and presses ENTER there.
async function PressEnterInField(user: UserEvent, sameFieldCase: SameFieldCase): Promise<void> {
    if (sameFieldCase.keyedAccount !== "") {
        await user.type(FieldInput(ACCOUNT_LABEL), sameFieldCase.keyedAccount);
    }
    await user.click(FieldInput(sameFieldCase.label));
    await user.keyboard("{Enter}");
}

// The keyboard lock: the input is disabled and MUI has dropped its focused state.
function ExpectLockedWithoutIndicator(label: string): void {
    const input = FieldInput(label);
    expect(input).toBeDisabled();
    expect(input.closest(MUI_INPUT_ROOT_SELECTOR)).not.toHaveClass(MUI_FOCUSED_CLASS);
}

// Settles the one request inside act, so the page applies the answer before the assertions.
async function SettleOnlyAdd(
    pendingAdds: readonly PendingAdd[],
    settlePending: (pendingAdd: PendingAdd) => void,
): Promise<void> {
    expect(pendingAdds).toHaveLength(1);
    await act(async () => {
        pendingAdds.forEach(settlePending);
    });
}

// The data-shrink of the floating label inside the input's own TextField; null when it has none,
// as a label outside that TextField (the confirm prompt) is not one.
function FloatingLabelShrinkOf(input: HTMLInputElement): string | null {
    const formControl = input.closest(MUI_FORM_CONTROL_SELECTOR);
    return formControl?.querySelector("label")?.getAttribute("data-shrink") ?? null;
}

// The focused element shows MUI's focused state: the focused root and the case's floating label
// state for the blank value.
function ExpectFocusIndicator(sameFieldCase: SameFieldCase): void {
    const input = FieldInput(sameFieldCase.label);
    expect(input).toBeEnabled();
    expect(input).toHaveFocus();
    expect(input.closest(MUI_INPUT_ROOT_SELECTOR)).toHaveClass(MUI_FOCUSED_CLASS);
    expect(input).toHaveValue("");
    expect(FloatingLabelShrinkOf(input)).toBe(sameFieldCase.floatingLabelShrink);
}

beforeEach(() => {
    mockAddTransaction.mockReset();
    jest.spyOn(Date, "now").mockReturnValue(FIXED_NOW_MILLISECONDS);
    window.sessionStorage.clear();
});

afterEach(() => {
    jest.restoreAllMocks();
});

it.each(SAME_FIELD_CASES)(
    "COTRN02C answer naming the focused $field as cursor_field restores its focus indicator",
    async (sameFieldCase) => {
        const pendingAdds = DeferEveryAdd();
        const user = RenderScreen();
        await PressEnterInField(user, sameFieldCase);
        ExpectLockedWithoutIndicator(sameFieldCase.label);
        await SettleOnlyAdd(pendingAdds, sameFieldCase.SettlePending);
        await waitFor(() => {
            expect(screen.getByRole("alert").textContent).toBe(sameFieldCase.message);
        });
        ExpectFocusIndicator(sameFieldCase);
    },
);
