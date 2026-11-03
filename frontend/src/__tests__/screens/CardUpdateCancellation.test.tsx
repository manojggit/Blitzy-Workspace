// Screen tests: BMS COCRDUP, program COCRDUPC (CCUP), page /cards/update. Covers the entry fetch
// a COCRDLIC selection starts: it is a read with its own AbortController, abandoned when the page
// is left, React's development remount fetches again or the query changes, while ENTER, F5 and
// F12 stay writes that are never given a signal. A query change on the same route is a fresh
// first entry: it shows only the new selection, or the empty key screen, and a write already sent
// for the previous selection ends without reaching the new screen.
import { ThemeProvider } from "@mui/material/styles";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CanceledError } from "axios";
import { StrictMode, type ReactElement, type ReactNode } from "react";

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
    // Renders the page again under the current mockSearchParams, as the App Router does on a
    // same-route navigation.
    Rerender: () => void;
}

// A card list selection and the SHOW_DETAILS screen the server answers its fetch with.
interface TestSelection {
    cardRef: string;
    acctId: string;
    screenToken: string;
    maskedCardNumber: string;
    embossedName: string;
}

// Test keys only: the card number is shown masked, so no card number is written in code.
const TEST_ACCOUNT_ID = "00000000004";
const TEST_CARD_REF = "card-ref-1";
const TEST_SCREEN_TOKEN = "screen-token-1";
const MASKED_CARD_NUMBER = "*".repeat(12) + "4312";
const TEST_EMBOSSED_NAME = "TEST CARD HOLDER";
const FIRST_SELECTION: TestSelection = {
    cardRef: TEST_CARD_REF,
    acctId: TEST_ACCOUNT_ID,
    screenToken: TEST_SCREEN_TOKEN,
    maskedCardNumber: MASKED_CARD_NUMBER,
    embossedName: TEST_EMBOSSED_NAME,
};
// Another card list row, which opens the same route with another query.
const SECOND_SELECTION: TestSelection = {
    cardRef: "card-ref-2",
    acctId: "00000000005",
    screenToken: "screen-token-2",
    maskedCardNumber: "*".repeat(12) + "7718",
    embossedName: "SECOND CARD HOLDER",
};
// An account typed on the empty key screen.
const TYPED_ACCOUNT_ID = "00000000009";

// Stable router and query doubles; the "mock" prefix lets the hoisted factories read them.
const mockRouter = {
    push: jest.fn(),
    replace: jest.fn(),
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};
// Read by the mocked useSearchParams at render time. Each case opens the page from the first
// COCRDLIC selection; a case that navigates sets another query and renders the page again.
let mockSearchParams = new URLSearchParams();
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

// The COCRDUP FKEYS legend button ENTER=Process; F3 returns to the main menu, whether the menu or
// the card list opened the page.
const ENTER_BUTTON_NAME = "Process (Enter)";
const MENU_ROUTE = "/menu";
// The INFOMSG cocrdupc.py sends with a fetched card (FOUND-CARDS-FOR-ACCOUNT).
const DETAILS_SHOWN_INFO = "Details of selected card shown above";
// The ERRMSG an ENTER with no change is answered with.
const NO_CHANGE_MESSAGE = "No change detected with respect to values fetched.";
// The COCRDUP ACCTSID and CARDSID labels, verbatim.
const ACCOUNT_LABEL = "Account Number    :";
const CARD_LABEL = "Card Number       :";
const KEY_LABELS: readonly string[] = [ACCOUNT_LABEL, CARD_LABEL];
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
function BuildCardScreen(selection: TestSelection = FIRST_SELECTION): CardScreen {
    return {
        fields: {
            acct_id: selection.acctId,
            card_num: selection.maskedCardNumber,
            embossed_name: selection.embossedName,
            active_status: "Y",
            expiry_month: "07",
            expiry_year: "2027",
            expiry_day: "31",
        },
        state: UpdateScreenState.SHOW_DETAILS,
        screen_token: selection.screenToken,
        card_ref: selection.cardRef,
        message: "",
        info_message: DETAILS_SHOWN_INFO,
        field_errors: [],
        cursor_field: "embossed_name",
    };
}

// The query a COCRDLIC U selection opens the page with: the card_ref token and the account.
function SelectionQuery(selection: TestSelection): URLSearchParams {
    return new URLSearchParams({ cardRef: selection.cardRef, acctId: selection.acctId });
}

// A new element on every call, so a rerender renders the page again instead of bailing out.
function PageTree(isStrict: boolean): ReactElement {
    const page = (
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <CardUpdatePage />
        </ThemeProvider>
    );
    return isStrict ? <StrictMode>{page}</StrictMode> : page;
}

// The page opened from the card list; under React StrictMode it mounts, cleans up and mounts
// again, as next.config.ts reactStrictMode does in development.
function RenderPage(isStrict: boolean): RenderedPage {
    const user = userEvent.setup();
    const { rerender, unmount } = render(PageTree(isStrict));
    return { user, Unmount: unmount, Rerender: () => rerender(PageTree(isStrict)) };
}

// A same-route navigation to /cards/update with another query, or with none.
function NavigateTo(query: URLSearchParams, page: RenderedPage): void {
    mockSearchParams = query;
    page.Rerender();
}

function KeepRawText(text: string): string {
    return text;
}

function InputFor(label: string): HTMLElement {
    return screen.getByLabelText(label, { normalizer: KeepRawText });
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

// The last ENTER sent: the selection's card_ref token and screen_token, and no signal.
function ExpectLastEnterFor(selection: TestSelection): void {
    expect(mockUpdateCard.mock.lastCall).toEqual([
        selection.cardRef,
        expect.objectContaining({
            action: UpdateAction.ENTER,
            screen_token: selection.screenToken,
        }),
    ]);
}

// The one ENTER from the empty key screen: no card_ref is held, so the path is CARDSID as typed
// (blank here), with no screen_token and only the two key fields.
function ExpectTypedKeysEnter(acctId: string): void {
    expect(mockUpdateCard.mock.calls).toEqual([
        [
            "",
            {
                action: UpdateAction.ENTER,
                screen_token: "",
                fields: { acct_id: acctId, card_num: "" },
            },
        ],
    ]);
}

// The replacement screen's own entry fetch, sent for the new selection with a live signal.
function ExpectSecondFetchFor(selection: TestSelection): void {
    expect(mockFetchCardForEdit).toHaveBeenCalledTimes(2);
    expect(mockFetchCardForEdit).toHaveBeenLastCalledWith(
        selection.cardRef,
        selection.acctId,
        READ_SIGNAL_OPTIONS,
    );
    expect(SignalOfCall(1)?.aborted).toBe(false);
}

// The fields show the shown selection's card and nothing of the replaced one.
function ExpectCardShown(shown: TestSelection, replaced: TestSelection): void {
    expect(InputFor(ACCOUNT_LABEL)).toHaveValue(shown.acctId);
    expect(InputFor(CARD_LABEL)).toHaveValue(shown.maskedCardNumber);
    expect(screen.getByDisplayValue(shown.embossedName)).toBeInTheDocument();
    expect(screen.queryByDisplayValue(replaced.embossedName)).not.toBeInTheDocument();
}

// DETAILS_NOT_FETCHED as entered from the menu: both keys blank and typable, and neither the
// details nor the information line of the card shown before.
function ExpectEmptyKeyScreen(): void {
    for (const label of KEY_LABELS) {
        expect(InputFor(label)).toHaveValue("");
        expect(InputFor(label)).not.toHaveAttribute("readonly");
    }
    expect(screen.queryByDisplayValue(TEST_EMBOSSED_NAME)).not.toBeInTheDocument();
    expect(screen.queryByText(DETAILS_SHOWN_INFO)).not.toBeInTheDocument();
}

// Both cards' fetches answered at once, and the first card's ENTER held until the case answers it.
function QueueHeldFirstEnter(): PromiseWithResolvers<CardScreen> {
    const firstEnter = Promise.withResolvers<CardScreen>();
    mockFetchCardForEdit.mockResolvedValueOnce(BuildCardScreen());
    mockFetchCardForEdit.mockResolvedValueOnce(BuildCardScreen(SECOND_SELECTION));
    mockUpdateCard.mockReturnValueOnce(firstEnter.promise);
    return firstEnter;
}

async function WaitForRequestEnd(): Promise<void> {
    await waitFor(() => {
        expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    });
}

// clearMocks keeps queued answers, so each case starts from empty fetch and write queues.
beforeEach(() => {
    jest.useFakeTimers(CLOCK_ONLY_FAKE_TIMERS);
    mockSearchParams = SelectionQuery(FIRST_SELECTION);
    mockFetchCardForEdit.mockReset();
    mockUpdateCard.mockReset();
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

// Each arrival with another query is a fresh COCRDUPC first entry, as every XCTL is.
describe("COCRDUPC another card list selection on the same /cards/update route", () => {
    it("COCRDUPC aborts the first card's fetch, then fetches and updates the second", async () => {
        const secondFetch = Promise.withResolvers<CardScreen>();
        mockFetchCardForEdit.mockImplementationOnce((_cardNum, _acctId, options) =>
            PendUntilAborted(options),
        );
        mockFetchCardForEdit.mockReturnValueOnce(secondFetch.promise);
        const page = RenderPage(false);
        await waitFor(() => expect(mockFetchCardForEdit).toHaveBeenCalledTimes(1));
        NavigateTo(SelectionQuery(SECOND_SELECTION), page);
        expect(SignalOfCall(0)?.aborted).toBe(true);
        ExpectSecondFetchFor(SECOND_SELECTION);
        expect(InputFor(ACCOUNT_LABEL)).toHaveValue(SECOND_SELECTION.acctId);
        await act(async () => secondFetch.resolve(BuildCardScreen(SECOND_SELECTION)));
        ExpectCardShown(SECOND_SELECTION, FIRST_SELECTION);
        await ClickEnter(page.user);
        ExpectLastEnterFor(SECOND_SELECTION);
    });
});

describe("COCRDUPC entry without a selection after a fetched card on /cards/update", () => {
    it("COCRDUPC shows the empty key screen, fetches nothing and sends typed keys", async () => {
        mockFetchCardForEdit.mockResolvedValueOnce(BuildCardScreen());
        const page = RenderPage(false);
        expect(await screen.findByText(DETAILS_SHOWN_INFO)).toBeInTheDocument();
        NavigateTo(new URLSearchParams(), page);
        ExpectEmptyKeyScreen();
        expect(mockFetchCardForEdit).toHaveBeenCalledTimes(1);
        await page.user.type(InputFor(ACCOUNT_LABEL), TYPED_ACCOUNT_ID);
        await ClickEnter(page.user);
        ExpectTypedKeysEnter(TYPED_ACCOUNT_ID);
    });
});

// The ENTER already sent for the first card is a write: it is never aborted, and its late answer
// reaches only the screen it was sent from, which no longer exists.
describe("COCRDUPC another selection while the first card's ENTER is in flight", () => {
    it("COCRDUPC fetches the second card and keeps it when the first ENTER ends", async () => {
        const firstEnter = QueueHeldFirstEnter();
        const page = RenderPage(false);
        await screen.findByDisplayValue(TEST_EMBOSSED_NAME);
        await ClickEnter(page.user);
        ExpectEnterSentWithoutSignal();
        NavigateTo(SelectionQuery(SECOND_SELECTION), page);
        ExpectSecondFetchFor(SECOND_SELECTION);
        await screen.findByDisplayValue(SECOND_SELECTION.embossedName);
        await act(async () =>
            firstEnter.resolve({ ...BuildCardScreen(), message: NO_CHANGE_MESSAGE }),
        );
        ExpectCardShown(SECOND_SELECTION, FIRST_SELECTION);
        expect(screen.queryByText(NO_CHANGE_MESSAGE)).not.toBeInTheDocument();
        await ClickEnter(page.user);
        ExpectLastEnterFor(SECOND_SELECTION);
    });
});
