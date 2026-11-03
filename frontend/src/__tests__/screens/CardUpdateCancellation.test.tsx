// Screen tests: BMS COCRDUP, program COCRDUPC (CCUP), page /cards/update. Covers only the entry
// fetch a COCRDLIC selection starts: it is a read with its own AbortController, abandoned when the
// page is left or React's development remount fetches again, while ENTER, F5 and F12 stay writes
// that are never given a signal.
import { ThemeProvider } from "@mui/material/styles";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CanceledError } from "axios";
import { StrictMode, type ReactNode } from "react";

import CardUpdatePage from "@/app/cards/update/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import { FetchCardForEdit, UpdateCard, type CardScreen } from "@/lib/api/cards";
import { ABEND_MESSAGE, UpdateAction, UpdateScreenState, type ReadOptions } from "@/lib/apiClient";
import type { SessionContextValue } from "@/lib/session";

type SignedOnUser = NonNullable<SessionContextValue["currentUser"]>;
type ScreenUser = ReturnType<typeof userEvent.setup>;

interface RenderedPage {
    user: ScreenUser;
    Unmount: () => void;
}

// Test keys only: the card number is shown masked, so no card number is written in code.
const TEST_ACCOUNT_ID = "00000000004";
const TEST_CARD_REF = "card-ref-1";
const TEST_SCREEN_TOKEN = "screen-token-1";
const MASKED_CARD_NUMBER = "*".repeat(12) + "4312";
const TEST_EMBOSSED_NAME = "TEST CARD HOLDER";

// Stable router and query doubles; the "mock" prefix lets the hoisted factories read them.
const mockRouter = {
    push: jest.fn(),
    replace: jest.fn(),
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};
// A COCRDLIC selection opens the page with the card_ref token and the account as its query.
const MOCK_SEARCH_PARAMS = new URLSearchParams({ cardRef: TEST_CARD_REF, acctId: TEST_ACCOUNT_ID });
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
    useSearchParams: () => MOCK_SEARCH_PARAMS,
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

// The COCRDUP FKEYS legend button ENTER=Process; F3 returns to the main menu, whether the menu or
// the card list opened the page.
const ENTER_BUTTON_NAME = "Process (Enter)";
const MENU_ROUTE = "/menu";
// The INFOMSG cocrdupc.py sends with a fetched card (FOUND-CARDS-FOR-ACCOUNT).
const DETAILS_SHOWN_INFO = "Details of selected card shown above";
// The entry fetch is handed the signal of the AbortController that owns it.
const READ_SIGNAL_OPTIONS = expect.objectContaining({ signal: expect.any(AbortSignal) });

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

// The SHOW_DETAILS screen of a fetched card: the number masked and the day kept dark.
function BuildCardScreen(): CardScreen {
    return {
        fields: {
            acct_id: TEST_ACCOUNT_ID,
            card_num: MASKED_CARD_NUMBER,
            embossed_name: TEST_EMBOSSED_NAME,
            active_status: "Y",
            expiry_month: "07",
            expiry_year: "2027",
            expiry_day: "31",
        },
        state: UpdateScreenState.SHOW_DETAILS,
        screen_token: TEST_SCREEN_TOKEN,
        card_ref: TEST_CARD_REF,
        message: "",
        info_message: DETAILS_SHOWN_INFO,
        field_errors: [],
        cursor_field: "embossed_name",
    };
}

// The page opened from the card list; under React StrictMode it mounts, cleans up and mounts
// again, as next.config.ts reactStrictMode does in development.
function RenderPage(isStrict: boolean): RenderedPage {
    const user = userEvent.setup();
    const page = (
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <CardUpdatePage />
        </ThemeProvider>
    );
    const { unmount } = render(isStrict ? <StrictMode>{page}</StrictMode> : page);
    return { user, Unmount: unmount };
}

// A read still in flight: it settles only as axios does once its signal is aborted.
function PendUntilAborted(options?: ReadOptions): Promise<never> {
    return new Promise((_resolve, reject) => {
        options?.signal?.addEventListener("abort", () => reject(new CanceledError()));
    });
}

// The signal FetchCardForEdit was handed on the call with this index.
function SignalOfCall(callIndex: number): AbortSignal | undefined {
    return mockFetchCardForEdit.mock.calls[callIndex]?.[2]?.signal;
}

// React's development remount: the first fetch aborted, the second sent with its own signal.
function ExpectFirstFetchAbandoned(): void {
    expect(mockFetchCardForEdit).toHaveBeenLastCalledWith(
        TEST_CARD_REF,
        TEST_ACCOUNT_ID,
        READ_SIGNAL_OPTIONS,
    );
    expect([SignalOfCall(0)?.aborted, SignalOfCall(1)?.aborted]).toEqual([true, false]);
}

async function ClickEnter(user: ScreenUser): Promise<void> {
    await user.click(screen.getByRole("button", { name: ENTER_BUTTON_NAME }));
}

// While a request holds the screen, ENTER is disabled and its physical key is swallowed.
async function ExpectEnterRefused(user: ScreenUser): Promise<void> {
    expect(screen.getByRole("button", { name: ENTER_BUTTON_NAME })).toBeDisabled();
    await user.keyboard("{Enter}");
    expect(mockUpdateCard).not.toHaveBeenCalled();
}

// One ENTER from SHOW_DETAILS: the card_ref token and the held screen_token, and no signal.
function ExpectEnterSentWithoutSignal(): void {
    expect(mockUpdateCard).toHaveBeenCalledTimes(1);
    expect(mockUpdateCard.mock.calls[0]).toEqual([
        TEST_CARD_REF,
        expect.objectContaining({ action: UpdateAction.ENTER, screen_token: TEST_SCREEN_TOKEN }),
    ]);
}

async function WaitForRequestEnd(): Promise<void> {
    await waitFor(() => {
        expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    });
}

// clearMocks keeps queued answers, so each case starts from an empty fetch queue.
beforeEach(() => {
    jest.useFakeTimers(CLOCK_ONLY_FAKE_TIMERS);
    mockFetchCardForEdit.mockReset();
    mockUpdateCard.mockResolvedValue(BuildCardScreen());
});

afterEach(() => {
    jest.useRealTimers();
});

// The abandoned fetch settles late, as axios's CanceledError can arrive after the remount's fetch
// has started; its settling must not free the keys the second fetch holds.
describe("COCRDUPC entry fetch under React StrictMode on /cards/update", () => {
    it("COCRDUPC abandons the first fetch and holds the keys until the second ends", async () => {
        const abandonedFetch = Promise.withResolvers<CardScreen>();
        const entryFetch = Promise.withResolvers<CardScreen>();
        mockFetchCardForEdit.mockReturnValueOnce(abandonedFetch.promise);
        mockFetchCardForEdit.mockReturnValueOnce(entryFetch.promise);
        const { user } = RenderPage(true);
        await waitFor(() => expect(mockFetchCardForEdit).toHaveBeenCalledTimes(2));
        ExpectFirstFetchAbandoned();
        await act(async () => abandonedFetch.reject(new CanceledError()));
        await ExpectEnterRefused(user);
        await act(async () => entryFetch.resolve(BuildCardScreen()));
        expect(screen.getByDisplayValue(TEST_EMBOSSED_NAME)).toBeInTheDocument();
        await ClickEnter(user);
        ExpectEnterSentWithoutSignal();
    });
});

describe("COCRDUPC leaving the page during the entry fetch on /cards/update", () => {
    it("COCRDUPC leaving the page aborts the pending fetch and logs nothing", async () => {
        const consoleErrorSpy = jest.spyOn(console, "error");
        mockFetchCardForEdit.mockImplementationOnce((_cardNum, _acctId, options) =>
            PendUntilAborted(options),
        );
        const { Unmount } = RenderPage(false);
        await waitFor(() => expect(mockFetchCardForEdit).toHaveBeenCalledTimes(1));
        expect(SignalOfCall(0)?.aborted).toBe(false);
        Unmount();
        expect(SignalOfCall(0)?.aborted).toBe(true);
        await act(async () => undefined);
        expect(screen.queryAllByRole("alert")).toHaveLength(0);
        expect(screen.queryAllByRole("status")).toHaveLength(0);
        expect(consoleErrorSpy).not.toHaveBeenCalled();
        consoleErrorSpy.mockRestore();
    });
});

describe("COCRDUPC F3 during the entry fetch on /cards/update", () => {
    it("COCRDUPC F3 aborts the pending fetch before the route changes", async () => {
        mockFetchCardForEdit.mockImplementationOnce((_cardNum, _acctId, options) =>
            PendUntilAborted(options),
        );
        const { user } = RenderPage(false);
        await waitFor(() => expect(mockFetchCardForEdit).toHaveBeenCalledTimes(1));
        await user.keyboard("{F3}");
        expect(SignalOfCall(0)?.aborted).toBe(true);
        expect(mockRouter.push).toHaveBeenCalledWith(MENU_ROUTE);
        await WaitForRequestEnd();
        expect(screen.queryAllByRole("alert")).toHaveLength(0);
        expect(screen.queryAllByRole("status")).toHaveLength(0);
    });
});

describe("COCRDUPC a cancelled entry fetch on /cards/update", () => {
    it("COCRDUPC a CanceledError shows no message and leaves the keys working", async () => {
        mockFetchCardForEdit.mockRejectedValueOnce(new CanceledError());
        const { user } = RenderPage(false);
        await WaitForRequestEnd();
        expect(screen.queryAllByRole("alert")).toHaveLength(0);
        expect(screen.queryAllByRole("status")).toHaveLength(0);
        expect(screen.queryByText(ABEND_MESSAGE)).not.toBeInTheDocument();
        await ClickEnter(user);
        await waitFor(() => expect(mockUpdateCard).toHaveBeenCalledTimes(1));
    });
});
