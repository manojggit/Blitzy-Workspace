// Screen tests: BMS COTRN02, program COTRN02C (CT02), page /transactions/add. Covers only the
// in-flight write lock: a second ENTER, an F5 or an F4 pressed before the busy render disables the
// keys is refused, so one press sends one AddTransaction and its answer lands on the form that was
// sent, and the lock clears once the request settles, whether answered or rejected.
import { ThemeProvider } from "@mui/material/styles";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import {
    AxiosError,
    AxiosHeaders,
    type AxiosResponse,
    type InternalAxiosRequestConfig,
} from "axios";

import TransactionAddPage from "@/app/transactions/add/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import {
    AddTransaction,
    TransactionAddAction,
    type TransactionAddFields,
    type TransactionAddRequest,
    type TransactionAddResponse,
} from "@/lib/api/transactions";
import type { LegacyErrorResponse } from "@/lib/apiClient";
import type { SessionContextValue } from "@/lib/session";

interface PendingAdd {
    Answer: (response: TransactionAddResponse) => void;
    Fail: (error: unknown) => void;
}

interface BurstCase {
    pressed: string;
    secondKey: string;
}

interface SettleCase {
    outcome: string;
    message: string;
    SettlePending: (pendingAdd: PendingAdd) => void;
}

// Stable router and query doubles; the "mock" prefix lets the hoisted factories read them.
const mockRouter = {
    push: jest.fn(),
    replace: jest.fn(),
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};
// COTRN02C reads no query parameter, so the search parameters stay empty.
const mockSearchParams = new URLSearchParams();

// A regular user (type U), as CT02 is main-menu option 8; the session holds no password.
const mockSessionValue: SessionContextValue = {
    currentUser: {
        user_id: "TESTUSR1",
        user_type: "U",
        first_name: "Test",
        last_name: "User",
        next_route: "/menu",
    },
    isLoading: false,
    RefreshSession: jest.fn(),
    ClearSession: jest.fn(),
};

jest.mock("next/navigation", () => ({
    useRouter: () => mockRouter,
    usePathname: () => "/transactions/add",
    useSearchParams: () => mockSearchParams,
}));

jest.mock("@/lib/session", () => ({
    useSession: () => mockSessionValue,
}));

// Only AddTransaction is stubbed; TransactionAddAction stays the real enum.
jest.mock("@/lib/api/transactions", () => ({
    ...jest.requireActual<Record<string, unknown>>("@/lib/api/transactions"),
    AddTransaction: jest.fn(),
}));

const mockAddTransaction = jest.mocked(AddTransaction);

const TRANSACTIONS_PATH = "/transactions";
const ACCOUNT_LABEL = "Enter Acct #:";
const TYPE_LABEL = "Type CD:";
const CONFIRM_LABEL = "You are about to add this transaction. Please confirm :";
const ENTER_BUTTON_NAME = "Continue (Enter)";
const ENTER_KEY = "Enter";
const TEST_ACCOUNT_ID = "00000000004";
const CONFIRMED = "Y";

// backend/app/services/transactions/cotrn02c.py: the first data-field edit and the prompt.
const TYPE_REQUIRED_MESSAGE = "Type CD can NOT be empty...";
const CONFIRM_PROMPT_MESSAGE = "Confirm to add this transaction...";
const HTTP_EDIT_FAILURE = 400;

// The keyed account and the Y answer, every other field blank.
const KEYED_FIELDS: TransactionAddFields = {
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
    confirm: CONFIRMED,
};

// ENTER sends the 14 fields as keyed beside the key pressed, as BuildAddRequest builds them.
const ENTER_REQUEST: TransactionAddRequest = {
    action: TransactionAddAction.ENTER,
    ...KEYED_FIELDS,
};

// The second press arrives while the first request is still out.
const BURST_CASES: readonly BurstCase[] = [
    { pressed: "ENTER and ENTER", secondKey: ENTER_KEY },
    { pressed: "ENTER and F5 (copy_last)", secondKey: "F5" },
    { pressed: "ENTER and F4 (clear)", secondKey: "F4" },
];

// The legacy error body of the first failing data-field edit: TTYPCD left blank.
const TYPE_REQUIRED_BODY: LegacyErrorResponse = {
    message: TYPE_REQUIRED_MESSAGE,
    info_message: "",
    field_errors: ["type_cd"],
    cursor_field: "type_cd",
};

// The rejection axios raises for the 400 answer to POST /transactions.
function BuildTypeRequiredFailure(): AxiosError {
    const config: InternalAxiosRequestConfig = {
        url: TRANSACTIONS_PATH,
        method: "post",
        headers: new AxiosHeaders(),
    };
    const response: AxiosResponse = {
        status: HTTP_EDIT_FAILURE,
        data: TYPE_REQUIRED_BODY,
        statusText: "",
        headers: {},
        config,
    };
    return new AxiosError("Request failed", "ERR_BAD_REQUEST", config, undefined, response);
}

// The confirm prompt COTRN02C sends with HTTP 200, the screen sent back as keyed.
function AnswerWithPrompt(pendingAdd: PendingAdd): void {
    pendingAdd.Answer({
        message: CONFIRM_PROMPT_MESSAGE,
        cursor_field: "confirm",
        fields: KEYED_FIELDS,
    });
}

function FailTypeRequired(pendingAdd: PendingAdd): void {
    pendingAdd.Fail(BuildTypeRequiredFailure());
}

const SETTLE_CASES: readonly SettleCase[] = [
    { outcome: "an answer", message: CONFIRM_PROMPT_MESSAGE, SettlePending: AnswerWithPrompt },
    {
        outcome: "an axios rejection",
        message: TYPE_REQUIRED_MESSAGE,
        SettlePending: FailTypeRequired,
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

// The presses go to the window listener from anywhere on the page, so the pointer then leaves
// the inputs: an input still focused when the answer moves the cursor to another field would
// have MUI's FormControl update its focus state during act's final flush, which React reports as
// outside act.
async function KeyAccountAndConfirm(user: UserEvent): Promise<void> {
    await user.type(screen.getByLabelText(ACCOUNT_LABEL), TEST_ACCOUNT_ID);
    await user.type(screen.getByLabelText(CONFIRM_LABEL), CONFIRMED);
    await user.click(document.body);
}

// Physical keys reach the useFunctionKeys window listener. All of them run inside one act, so no
// render, and no effect handing the keys their disabled state, runs between two presses.
async function PressInOneTurn(keys: readonly string[]): Promise<void> {
    await act(async () => {
        for (const key of keys) {
            window.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
        }
    });
}

// Settles the first request inside act, so the page applies the outcome before the assertions.
async function SettleFirstAdd(
    pendingAdds: readonly PendingAdd[],
    settlePending: (pendingAdd: PendingAdd) => void,
): Promise<void> {
    const [firstAdd] = pendingAdds;
    expect(firstAdd).toBeDefined();
    await act(async () => {
        if (firstAdd !== undefined) {
            settlePending(firstAdd);
        }
    });
}

// The raw textContent keeps the literal as the server sent it.
async function ExpectAlert(text: string): Promise<void> {
    await waitFor(() => {
        expect(screen.getByRole("alert").textContent).toBe(text);
    });
}

// The busy state of the one request out: the progress bar shows and ENTER is disabled.
function ExpectOneRequestOut(): void {
    expect(mockAddTransaction).toHaveBeenCalledTimes(1);
    expect(mockAddTransaction).toHaveBeenCalledWith(ENTER_REQUEST);
    expect(screen.getByRole("progressbar")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: ENTER_BUTTON_NAME })).toBeDisabled();
}

beforeEach(() => {
    mockAddTransaction.mockReset();
});

it.each(BURST_CASES)(
    "COTRN02C $pressed in one turn send one add, answered on the form sent",
    async ({ secondKey }) => {
        const pendingAdds = DeferEveryAdd();
        const user = RenderScreen();
        await KeyAccountAndConfirm(user);
        await PressInOneTurn([ENTER_KEY, secondKey]);
        ExpectOneRequestOut();
        await SettleFirstAdd(pendingAdds, FailTypeRequired);
        await ExpectAlert(TYPE_REQUIRED_MESSAGE);
        expect(screen.getByLabelText(ACCOUNT_LABEL)).toHaveValue(TEST_ACCOUNT_ID);
        expect(screen.getByLabelText(CONFIRM_LABEL)).toHaveValue(CONFIRMED);
        expect(screen.getByLabelText(TYPE_LABEL)).toHaveFocus();
        expect(mockAddTransaction).toHaveBeenCalledTimes(1);
    },
);

it.each(SETTLE_CASES)(
    "COTRN02C the in-flight lock clears after $outcome, so the next ENTER sends again",
    async ({ message, SettlePending }) => {
        const pendingAdds = DeferEveryAdd();
        const user = RenderScreen();
        await KeyAccountAndConfirm(user);
        await PressInOneTurn([ENTER_KEY]);
        ExpectOneRequestOut();
        await SettleFirstAdd(pendingAdds, SettlePending);
        await ExpectAlert(message);
        await PressInOneTurn([ENTER_KEY]);
        expect(mockAddTransaction).toHaveBeenCalledTimes(2);
        expect(mockAddTransaction).toHaveBeenLastCalledWith(ENTER_REQUEST);
    },
);
