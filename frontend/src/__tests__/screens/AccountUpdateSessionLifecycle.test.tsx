// Tests: app/accounts/update/page.tsx (BMS COACTUP, program COACTUPC, CAUP) under the real
// lib/session.tsx SessionProvider and the real lib/apiClient.ts interceptor. CAUP is the one
// screen that shows the SSN unmasked, so its SSN parts must leave the DOM when the page is hidden
// into the back/forward cache, stay out of it while a restore re-reads the session or once the
// server refuses that read, and come back only from a fresh entry fetch for the user the restore
// found, never as the values the page held before.
import { ThemeProvider } from "@mui/material/styles";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { AxiosError, AxiosHeaders, type InternalAxiosRequestConfig } from "axios";

import AccountUpdatePage from "@/app/accounts/update/page";
import { CARD_DEMO_THEME } from "@/app/theme";
import type * as AccountsApiModule from "@/lib/api/accounts";
import {
    FetchAccountForEdit,
    type AccountScreen,
    type AccountScreenFields,
} from "@/lib/api/accounts";
import type * as AuthApiModule from "@/lib/api/auth";
import { FetchCurrentUser, type CurrentUserResponse } from "@/lib/api/auth";
import { HandleResponseError, UpdateScreenState } from "@/lib/apiClient";
import { SessionProvider } from "@/lib/session";

// A restore's re-read of the session answering a user, and the user it answers.
interface RestoredUserCase {
    caseName: string;
    user: CurrentUserResponse;
}

// One router object for every render, as the App Router keeps useRouter stable.
const mockRouter = {
    push: jest.fn(),
    replace: jest.fn(),
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
};
// Read by the mocked useSearchParams at render time: the ?acctId= entry.
let mockSearchParams = new URLSearchParams();

jest.mock("next/navigation", () => ({
    useRouter: () => mockRouter,
    useSearchParams: () => mockSearchParams,
    usePathname: () => "/accounts/update",
}));

// Only the /auth/me read is mocked; the session context and the interceptor run for real.
jest.mock("@/lib/api/auth", () => ({
    ...jest.requireActual<typeof AuthApiModule>("@/lib/api/auth"),
    FetchCurrentUser: jest.fn(),
}));

jest.mock("@/lib/api/accounts", () => ({
    ...jest.requireActual<typeof AccountsApiModule>("@/lib/api/accounts"),
    ViewAccount: jest.fn(),
    FetchAccountForEdit: jest.fn(),
    UpdateAccount: jest.fn(),
}));

const fetchCurrentUserMock = jest.mocked(FetchCurrentUser);
const mockFetchAccountForEdit = jest.mocked(FetchAccountForEdit);

const TEST_ACCOUNT_ID = "00000000004";
const CAUP_PATH = "/accounts/update";
const CURRENT_USER_PATH = "/auth/me";
const HTTP_UNAUTHORIZED = 401;
// The edit fetch is a read that carries its owner's AbortSignal.
const READ_OPTIONS = { signal: expect.any(AbortSignal) };

// The header clock reads Date.now(); one fixed local instant keeps the header still, so its
// one-second interval never re-renders outside act.
const FIXED_CLOCK_MILLISECONDS = new Date(2022, 6, 18, 9, 5, 7).getTime();

// jsdom implements no navigation: every window.location.assign is reported through
// console.error, because window.location cannot be mocked under jsdom 26.
const NAVIGATION_NOT_IMPLEMENTED = "Not implemented: navigation";

// The COACTUP SSN group caption and the labels of its three parts.
const SSN_GROUP_CAPTION = "SSN:";
const LAST_SSN_PART_LABEL = "Last 4 chars";
const SSN_PART_LABELS: readonly string[] = [
    "First 3 chars",
    "4th & 5th chars",
    LAST_SSN_PART_LABEL,
];
// The SSN parts of account 00000000004 as the edit fetch shows them, unmasked, and a last part
// keyed over the fetched one before the page was hidden.
const FETCHED_SSN_PARTS: readonly string[] = ["234", "56", "7890"];
const KEYED_SSN_PART_3 = "7891";
const SSN_VALUES: readonly string[] = [...FETCHED_SSN_PARTS, KEYED_SSN_PART_3];

// Fictitious regular users (type U), as CAUP is a main-menu function; the session holds no
// password. The second is the user another tab signed on as.
const TEST_USER: CurrentUserResponse = {
    user_id: "TESTUSR1",
    user_type: "U",
    first_name: "TEST",
    last_name: "USER",
    next_route: "/menu",
};
const OTHER_USER: CurrentUserResponse = {
    user_id: "TESTUSR2",
    user_type: "U",
    first_name: "OTHER",
    last_name: "USER",
    next_route: "/menu",
};

// Account 00000000004 as the edit fetch shows it; the SSN is unmasked on this screen only,
// because COACTUPC edits it.
const TEST_FIELDS: Readonly<AccountScreenFields> = {
    acct_id: TEST_ACCOUNT_ID,
    active_status: "Y",
    open_year: "2014",
    open_month: "03",
    open_day: "11",
    credit_limit: "5,600.00",
    expiration_year: "2025",
    expiration_month: "10",
    expiration_day: "01",
    cash_credit_limit: "1,200.00",
    reissue_year: "2024",
    reissue_month: "10",
    reissue_day: "01",
    curr_balance: "2,450.00",
    curr_cycle_credit: "0.00",
    group_id: "",
    curr_cycle_debit: "0.00",
    cust_id: "000000004",
    ssn_part_1: "234",
    ssn_part_2: "56",
    ssn_part_3: "7890",
    dob_year: "1985",
    dob_month: "07",
    dob_day: "15",
    fico_credit_score: "712",
    first_name: "TESTFIRST",
    middle_name: "TESTMIDDLE",
    last_name: "TESTLAST",
    addr_line_1: "100 TEST STREET",
    addr_state_cd: "NY",
    addr_line_2: "APT 1",
    addr_zip: "10001",
    addr_line_3: "TESTCITY",
    addr_country_cd: "USA",
    phone_num_1_area: "212",
    phone_num_1_prefix: "555",
    phone_num_1_line: "0100",
    govt_issued_id: "TESTGOVT0001",
    phone_num_2_area: "212",
    phone_num_2_prefix: "555",
    phone_num_2_line: "0101",
    eft_account_id: "0000000004",
    primary_holder_ind: "Y",
};

// The SHOW_DETAILS screen the ?acctId= entry fetch answers with.
const FETCHED_SCREEN: AccountScreen = {
    fields: TEST_FIELDS,
    state: UpdateScreenState.SHOW_DETAILS,
    screen_token: "screen-token-1",
    message: "",
    info_message: "Update account details presented above.",
    field_errors: [],
    cursor_field: "",
};

// The same user, or the user another tab signed on as meanwhile: either way the restored page is
// mounted afresh and fetches its account again.
const RESTORED_USER_CASES: readonly RestoredUserCase[] = [
    { caseName: "the same user", user: TEST_USER },
    { caseName: "another user", user: OTHER_USER },
];

// The console.error each test's spy forwards every report but jsdom's navigation reports to.
const unpatchedConsoleError = console.error.bind(console);
let consoleErrorSpy: jest.SpyInstance;

function IsNavigationReport(report: unknown): boolean {
    const text = report instanceof Error ? report.message : String(report);
    return text.includes(NAVIGATION_NOT_IMPLEMENTED);
}

// jsdom's navigation reports are counted, not printed. Every other report still prints, so an
// act() warning stays visible.
function ReportUnlessNavigation(...reports: unknown[]): void {
    if (!IsNavigationReport(reports[0])) {
        unpatchedConsoleError(...reports);
    }
}

// The window.location.assign calls jsdom reported during this test.
function NavigationAttempts(): number {
    const reports: unknown[][] = consoleErrorSpy.mock.calls;
    return reports.filter((report) => IsNavigationReport(report[0])).length;
}

// The /auth/me 401 of a session another tab ended, as the apiClient interceptor meets it.
function BuildSessionEndedError(): AxiosError {
    const config: InternalAxiosRequestConfig = {
        url: CURRENT_USER_PATH,
        headers: new AxiosHeaders(),
    };
    const data = { message: "", info_message: "", field_errors: [], cursor_field: null };
    const response = { status: HTTP_UNAUTHORIZED, data, statusText: "", headers: {}, config };
    return new AxiosError(
        "Request failed",
        AxiosError.ERR_BAD_REQUEST,
        config,
        undefined,
        response,
    );
}

function KeepRawText(text: string): string {
    return text;
}

function SsnPartInput(label: string): HTMLInputElement {
    const group = screen.getByRole("group", { name: SSN_GROUP_CAPTION });
    return within(group).getByLabelText<HTMLInputElement>(label, { normalizer: KeepRawText });
}

function ShownSsnParts(): string[] {
    return SSN_PART_LABELS.map((label) => SsnPartInput(label).value);
}

// Every SSN value, fetched or keyed, that any element of the document still holds.
function SsnValuesInDom(): string[] {
    return SSN_VALUES.filter((value) => screen.queryAllByDisplayValue(value).length > 0);
}

function SignedInUserLine(user: CurrentUserResponse): string {
    return `User: ${user.user_id}`;
}

async function WaitForFetchedScreen(user: CurrentUserResponse): Promise<void> {
    await waitFor(() => {
        expect(ShownSsnParts()).toEqual(FETCHED_SSN_PARTS);
    });
    expect(screen.getByText(SignedInUserLine(user))).toBeInTheDocument();
}

// CAUP entered with ?acctId=, as the app renders every page: the theme around the session
// provider. Its SSN shows as fetched, then a last part is keyed over the fetched one.
async function RenderPopulatedScreen(): Promise<void> {
    fetchCurrentUserMock.mockResolvedValue(TEST_USER);
    mockFetchAccountForEdit.mockResolvedValue(FETCHED_SCREEN);
    render(
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <SessionProvider>
                <AccountUpdatePage />
            </SessionProvider>
        </ThemeProvider>,
    );
    await WaitForFetchedScreen(TEST_USER);
    fireEvent.change(SsnPartInput(LAST_SSN_PART_LABEL), { target: { value: KEYED_SSN_PART_3 } });
    expect(ShownSsnParts()).toEqual([...FETCHED_SSN_PARTS.slice(0, 2), KEYED_SSN_PART_3]);
    expect(mockFetchAccountForEdit).toHaveBeenCalledTimes(1);
}

function DispatchPageHide(persisted: boolean): void {
    window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted }));
}

async function DispatchPageShow(persisted: boolean): Promise<void> {
    await act(async () => {
        window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted }));
    });
}

// The restore's /auth/me read stays pending until the returned function releases it to answer.
function DeferRestoreRead(answer: () => Promise<CurrentUserResponse>): () => Promise<void> {
    let release: () => void = () => undefined;
    const released = new Promise<void>((resolve) => {
        release = resolve;
    });
    fetchCurrentUserMock.mockImplementationOnce(() => released.then(answer));
    return async () => {
        await act(async () => {
            release();
        });
    };
}

beforeEach(() => {
    jest.spyOn(Date, "now").mockReturnValue(FIXED_CLOCK_MILLISECONDS);
    consoleErrorSpy = jest.spyOn(console, "error").mockImplementation(ReportUnlessNavigation);
    window.sessionStorage.clear();
    window.history.replaceState(null, "", `${CAUP_PATH}?acctId=${TEST_ACCOUNT_ID}`);
    mockSearchParams = new URLSearchParams({ acctId: TEST_ACCOUNT_ID });
    fetchCurrentUserMock.mockReset();
    mockFetchAccountForEdit.mockReset();
});

afterEach(() => {
    jest.restoreAllMocks();
});

// flushSync commits the teardown inside the handler, so the assertion inside act sees it before
// act flushes anything.
async function RemovesSsnPartsOnPageHide(persisted: boolean): Promise<void> {
    await RenderPopulatedScreen();
    act(() => {
        DispatchPageHide(persisted);
        expect(SsnValuesInDom()).toEqual([]);
    });
    expect(SsnValuesInDom()).toEqual([]);
    expect(NavigationAttempts()).toBe(0);
}

async function RefetchesForRestoredUser({ user }: RestoredUserCase): Promise<void> {
    await RenderPopulatedScreen();
    act(() => {
        DispatchPageHide(true);
    });
    const releaseRead = DeferRestoreRead(() => Promise.resolve(user));
    await DispatchPageShow(true);
    expect(SsnValuesInDom()).toEqual([]);
    await releaseRead();
    await WaitForFetchedScreen(user);
    expect(screen.queryByDisplayValue(KEYED_SSN_PART_3)).not.toBeInTheDocument();
    expect(mockFetchAccountForEdit).toHaveBeenCalledTimes(2);
    expect(mockFetchAccountForEdit).toHaveBeenLastCalledWith(TEST_ACCOUNT_ID, READ_OPTIONS);
    expect(NavigationAttempts()).toBe(0);
}

// No pagehide runs first, so the restore's own teardown is what removes the cached SSN parts. The
// interceptor's redirect is the one navigation; the page neither refetches nor routes itself.
async function StaysConcealedWhenSessionEnded(): Promise<void> {
    await RenderPopulatedScreen();
    const releaseRead = DeferRestoreRead(() => HandleResponseError(BuildSessionEndedError()));
    await DispatchPageShow(true);
    expect(SsnValuesInDom()).toEqual([]);
    await releaseRead();
    expect(SsnValuesInDom()).toEqual([]);
    expect(NavigationAttempts()).toBe(1);
    expect(mockFetchAccountForEdit).toHaveBeenCalledTimes(1);
    expect(mockRouter.replace).not.toHaveBeenCalled();
}

describe("COACTUPC /accounts/update across the back/forward cache", () => {
    it.each([true, false])(
        "COACTUPC pagehide (persisted %p) removes the SSN parts before its handler returns",
        RemovesSsnPartsOnPageHide,
    );
    it.each(RESTORED_USER_CASES)(
        "COACTUPC restored for $caseName shows no SSN part until a fresh entry fetch fills it",
        RefetchesForRestoredUser,
    );
    it(
        "COACTUPC restored after the session ended shows no SSN part and goes to sign-on once",
        StaysConcealedWhenSessionEnded,
    );
});
