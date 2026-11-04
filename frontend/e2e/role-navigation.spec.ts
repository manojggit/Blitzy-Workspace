// CardDemo role-navigation end-to-end flow (Technical Specification 0.9.1 success item 4, 0.9.2).
//
// Exercises: COSGN00C (CC00), whose sign-on sends user type A to the admin menu and every other
// type to the main menu; COMEN01C with the COMEN02Y option table (CM00); COADM01C with the
// COADM02Y option table (CA00); and the CSMSG01Y thank-you literal of the F3 sign-off. Each XCTL
// to an option's program is a page navigation here, and each option of an excluded module answers
// with its menu's legacy "not installed" literal. The spec only reads data, so it does not depend
// on day-in-the-life.spec.ts, and each test runs in a fresh browser context with its own session
// cookie. Real credentials come from the environment through AdminUser() and RegularUser() only;
// the credential-entry case uses throwaway markers generated at run time, and its entry fails
// before anything is submitted, so it sends no sign-on request.
import { randomBytes } from "node:crypto";

import { expect, test, type Page, type Response } from "@playwright/test";

import {
    AdminUser,
    ExpectMessage,
    RegularUser,
    RequireEnv,
    SIGN_ON_ROUTE,
    SignOn,
    WaitForPath,
    type Credentials,
} from "./global-setup";

const MENU_ROUTE = "/menu";
const ADMIN_ROUTE = "/admin";
const ADMIN_USERS_ROUTE = "/admin/users";

/** The ScreenShell header of a BMS-map page: TRNNAME, PGMNAME and the map's screen title. */
interface ScreenHeader {
    tranId: string;
    program: string;
    title: string;
}

/** The roles of the controls that can hold the cursor when a screen is first sent. */
type EntryControlRole = "textbox" | "button";

/** The control that holds the cursor when the map is first sent, by role and exact name. */
interface EntryControl {
    role: EntryControlRole;
    name: string;
}

/**
 * One installed menu option: its verbatim label, the page that replaces its program's XCTL, and
 * the screen that page shows once it is ready for input: its header, the exact name of its ENTER
 * key button and its entry control.
 */
interface MenuOptionRoute {
    label: string;
    route: string;
    header: ScreenHeader;
    enterKeyName: string;
    entryControl: EntryControl;
}

/** An option whose program is not installed, with the literal its menu answers. */
interface NotInstalledOption {
    label: string;
    message: string;
}

/** A signed-on page and the menu it returns to between options. */
interface MenuSession {
    page: Page;
    menuRoute: string;
}

// COADM02Y options 1-4, CDEMO-ADMIN-OPT-NAME with its trailing blanks trimmed. Each screen is
// its map's TRNNAME, PGMNAME and title, its legend's ENTER key as FunctionKeyBar names it, and
// the field that holds the cursor on first send, by its BMS label: the map's IC field, or on
// COUSR00 and COTRN00, whose maps mark none, the field the program's MOVE -1 points at.
const ADMIN_OPTION_ROUTES: readonly MenuOptionRoute[] = [
    {
        label: "User List (Security)",
        route: "/admin/users",
        header: { tranId: "CU00", program: "COUSR00C", title: "List Users" },
        enterKeyName: "Continue (Enter)",
        entryControl: { role: "textbox", name: "Search User ID:" },
    },
    {
        label: "User Add (Security)",
        route: "/admin/users/add",
        header: { tranId: "CU01", program: "COUSR01C", title: "Add User" },
        enterKeyName: "Add User (Enter)",
        entryControl: { role: "textbox", name: "First Name:" },
    },
    {
        label: "User Update (Security)",
        route: "/admin/users/update",
        header: { tranId: "CU02", program: "COUSR02C", title: "Update User" },
        enterKeyName: "Fetch (Enter)",
        entryControl: { role: "textbox", name: "Enter User ID:" },
    },
    {
        label: "User Delete (Security)",
        route: "/admin/users/delete",
        header: { tranId: "CU03", program: "COUSR03C", title: "Delete User" },
        enterKeyName: "Fetch (Enter)",
        entryControl: { role: "textbox", name: "Enter User ID:" },
    },
];

// COADM02Y options 5 and 6: COTRTLIC and COTRTUPC, programs of the excluded Db2 extension.
const ADMIN_NOT_INSTALLED_OPTIONS = [
    "Transaction Type List/Update (Db2)",
    "Transaction Type Maintenance (Db2)",
] as const;

// No option name: both STRING statements of COADM01C (lines 150-156 and 268-276) have the
// CDEMO-ADMIN-OPT-NAME operand commented out, so only 'This option ' and 'is not installed ...'
// are concatenated.
const ADMIN_NOT_INSTALLED_MESSAGE = "This option is not installed ...";

// COMEN02Y options 1-10, CDEMO-MENU-OPT-NAME with its trailing blanks trimmed, with their
// screens as for the admin options. CORPT00's IC is on the MONTHLY selector, which the page
// renders as the first segment of its report-type choice.
const MAIN_OPTION_ROUTES: readonly MenuOptionRoute[] = [
    {
        label: "Account View",
        route: "/accounts/view",
        header: { tranId: "CAVW", program: "COACTVWC", title: "View Account" },
        enterKeyName: "Search (Enter)",
        entryControl: { role: "textbox", name: "Account Number :" },
    },
    {
        label: "Account Update",
        route: "/accounts/update",
        header: { tranId: "CAUP", program: "COACTUPC", title: "Update Account" },
        enterKeyName: "Process (Enter)",
        entryControl: { role: "textbox", name: "Account Number :" },
    },
    {
        label: "Credit Card List",
        route: "/cards",
        header: { tranId: "CCLI", program: "COCRDLIC", title: "List Credit Cards" },
        enterKeyName: "Continue (Enter)",
        entryControl: { role: "textbox", name: "Account Number    :" },
    },
    {
        label: "Credit Card View",
        route: "/cards/view",
        header: { tranId: "CCDL", program: "COCRDSLC", title: "View Credit Card Detail" },
        enterKeyName: "Search Cards (Enter)",
        entryControl: { role: "textbox", name: "Account Number    :" },
    },
    {
        label: "Credit Card Update",
        route: "/cards/update",
        header: { tranId: "CCUP", program: "COCRDUPC", title: "Update Credit Card Details" },
        enterKeyName: "Process (Enter)",
        entryControl: { role: "textbox", name: "Account Number    :" },
    },
    {
        label: "Transaction List",
        route: "/transactions",
        header: { tranId: "CT00", program: "COTRN00C", title: "List Transactions" },
        enterKeyName: "Continue (Enter)",
        entryControl: { role: "textbox", name: "Search Tran ID:" },
    },
    {
        label: "Transaction View",
        route: "/transactions/view",
        header: { tranId: "CT01", program: "COTRN01C", title: "View Transaction" },
        enterKeyName: "Fetch (Enter)",
        entryControl: { role: "textbox", name: "Enter Tran ID:" },
    },
    {
        label: "Transaction Add",
        route: "/transactions/add",
        header: { tranId: "CT02", program: "COTRN02C", title: "Add Transaction" },
        enterKeyName: "Continue (Enter)",
        entryControl: { role: "textbox", name: "Enter Acct #:" },
    },
    {
        label: "Transaction Reports",
        route: "/reports/transactions",
        header: { tranId: "CR00", program: "CORPT00C", title: "Transaction Reports" },
        enterKeyName: "Continue (Enter)",
        entryControl: { role: "button", name: "Monthly (Current Month)" },
    },
    {
        label: "Bill Payment",
        route: "/billpay",
        header: { tranId: "CB00", program: "COBIL00C", title: "Bill Payment" },
        enterKeyName: "Continue (Enter)",
        entryControl: { role: "textbox", name: "Enter Acct ID:" },
    },
];

// COMEN02Y option 11: COPAUS0C, the program of the excluded authorization extension.
const AUTHORIZATION_OPTION = "Pending Authorization View";

// COMEN01C lines 147-168: 'This option ', the option name DELIMITED BY '  ', then
// ' is not installed...', with no blank before the dots.
const MAIN_NOT_INSTALLED_MESSAGE = "This option Pending Authorization View is not installed...";

const AUTHORIZATION_NOT_INSTALLED: NotInstalledOption = {
    label: AUTHORIZATION_OPTION,
    message: MAIN_NOT_INSTALLED_MESSAGE,
};

// CSMSG01Y CCDA-MSG-THANK-YOU, as the logout response returns it: the copybook's trailing
// padding is not part of the message, and ExpectMessage compares the whole text.
const THANK_YOU_MESSAGE = "Thank you for using CardDemo application...";

// COMEN01C line 139, trailing blank included; the 403 of the admin-only guard carries it.
const ADMIN_ONLY_MESSAGE = "No access - Admin Only option... ";

// The option-list landmark of both menus: "Main menu options" and "Admin menu options".
const MENU_OPTIONS_LANDMARK_NAME = /menu options/i;

// ScreenShell's busy bar, shown while any call of the screen is in flight.
const BUSY_BAR_NAME = "Processing";

// The FunctionKeyBar group that renders the map's PF-key legend.
const FUNCTION_KEYS_GROUP_NAME = "Function keys";

// The ScreenShell banner lines that carry TRNNAME and PGMNAME, each one text node.
const TRAN_LINE_PATTERN = /^Tran:/;
const PROGRAM_LINE_PATTERN = /^Prog:/;

// The API answers that mean an option's screen did not open: no session or an expired one, the
// role guard, or a server failure. A 400 or 404 is a legacy answer the open screen shows.
const HTTP_UNAUTHORIZED = 401;
const HTTP_FORBIDDEN = 403;
const HTTP_SERVER_ERROR_MIN = 500;

/** The refused API calls seen while a page is watched, and the call that stops watching it. */
interface RefusalRecorder {
    refusals: string[];
    Stop: () => void;
}

async function SignOnAndLand(
    page: Page,
    credentials: Credentials,
    landingRoute: string,
): Promise<void> {
    await SignOn(page, credentials);
    await WaitForPath(page, landingRoute);
}

// A fresh menu screen, as a new send of the map: every option listed and the message line blank.
async function ReturnToMenu(session: MenuSession): Promise<void> {
    await session.page.goto(session.menuRoute);
    await WaitForPath(session.page, session.menuRoute);
}

// A string name matches a case-insensitive substring of the option's accessible name, which also
// holds its number. Each label is unique within its menu, and the strict locator enforces it.
async function ClickMenuOption(page: Page, label: string): Promise<void> {
    await page.getByRole("button", { name: label }).click();
}

// Names an option's screen in failure messages by its menu label and route.
function DescribeScreen(option: MenuOptionRoute): string {
    return `${option.label} screen (${option.route})`;
}

function IsRefusalStatus(status: number): boolean {
    return (
        status === HTTP_UNAUTHORIZED || status === HTTP_FORBIDDEN || status >= HTTP_SERVER_ERROR_MIN
    );
}

// Watches the page's answers from the API origin. Each refusal is named by method, path and
// status only, never by a query or body.
function RecordApiRefusals(page: Page): RefusalRecorder {
    const apiOrigin = new URL(RequireEnv("CARDDEMO_E2E_API_URL")).origin;
    const refusals: string[] = [];
    const recordResponse = (response: Response): void => {
        const url = new URL(response.url());
        if (url.origin === apiOrigin && IsRefusalStatus(response.status())) {
            refusals.push(`${response.request().method()} ${url.pathname} ${response.status()}`);
        }
    };
    page.on("response", recordResponse);
    return { refusals, Stop: () => page.off("response", recordResponse) };
}

// The ScreenShell frame: the map's title as the one h1 of the main landmark, and the banner's
// TRNNAME and PGMNAME lines, each matched whole.
async function ExpectScreenHeader(
    page: Page,
    header: ScreenHeader,
    screenName: string,
): Promise<void> {
    const heading = page.getByRole("main").getByRole("heading", { level: 1 });
    await expect(heading, `${screenName}: title`).toHaveText(header.title);
    const banner = page.getByRole("banner");
    const tranLine = banner.getByText(TRAN_LINE_PATTERN);
    await expect(tranLine, `${screenName}: transaction ID`).toHaveText(`Tran: ${header.tranId}`);
    const programLine = banner.getByText(PROGRAM_LINE_PATTERN);
    await expect(programLine, `${screenName}: program`).toHaveText(`Prog: ${header.program}`);
}

// The option's screen, ready for input: its header, the cursor on its entry control, no call in
// flight and the ENTER key enabled. The path is read again last, so a refusal that redirects
// after the screen rendered (401 to sign-on, 403 to the menu) still fails.
async function ExpectScreenReady(page: Page, option: MenuOptionRoute): Promise<void> {
    const screenName = DescribeScreen(option);
    await WaitForPath(page, option.route);
    await ExpectScreenHeader(page, option.header, screenName);
    const { role, name } = option.entryControl;
    const entryControl = page.getByRole("main").getByRole(role, { name, exact: true });
    await expect(entryControl, `${screenName}: entry control focused`).toBeFocused();
    const busyBar = page.getByRole("progressbar", { name: BUSY_BAR_NAME, exact: true });
    await expect(busyBar, `${screenName}: no call in flight`).toHaveCount(0);
    const enterKey = page
        .getByRole("group", { name: FUNCTION_KEYS_GROUP_NAME, exact: true })
        .getByRole("button", { name: option.enterKeyName, exact: true });
    await expect(enterKey, `${screenName}: ENTER key enabled`).toBeEnabled();
    const readyPath = new URL(page.url()).pathname;
    expect(readyPath, `${screenName}: path once ready`).toBe(option.route);
}

// The menu is reached again by navigation rather than by the option page's F3, which can act on
// its own screen first (COUSR02C's F3 saves pending changes). The session cookie persists. The
// API answers are watched from the selection until the screen is ready, and any refusal fails
// the test; a soft check, so a readiness failure it caused is reported beside it.
async function OpenOptionAndReturn(session: MenuSession, option: MenuOptionRoute): Promise<void> {
    const recorder = RecordApiRefusals(session.page);
    try {
        await ClickMenuOption(session.page, option.label);
        await ExpectScreenReady(session.page, option);
    } finally {
        recorder.Stop();
        expect.soft(recorder.refusals, `${DescribeScreen(option)}: API refusals`).toEqual([]);
    }
    await ReturnToMenu(session);
}

async function OpenEveryOption(
    session: MenuSession,
    options: readonly MenuOptionRoute[],
): Promise<void> {
    for (const option of options) {
        await OpenOptionAndReturn(session, option);
    }
}

// Each check starts from a fresh menu, so a literal that two options share (admin options 5 and
// 6) is proven by each option's own selection rather than left over from the previous one.
async function ExpectNotInstalled(session: MenuSession, option: NotInstalledOption): Promise<void> {
    await ReturnToMenu(session);
    await ClickMenuOption(session.page, option.label);
    await ExpectMessage(session.page, option.message);
    expect(new URL(session.page.url()).pathname).toBe(session.menuRoute);
}

async function ExpectAdminOptionsNotInstalled(session: MenuSession): Promise<void> {
    for (const label of ADMIN_NOT_INSTALLED_OPTIONS) {
        await ExpectNotInstalled(session, { label, message: ADMIN_NOT_INSTALLED_MESSAGE });
    }
}

// A menu ignores F3 while a call is in flight, exactly when its option buttons are disabled, so
// the key is pressed only once they are enabled. After the sign-off the admin menu's own read
// answers 401, which returns the browser to sign-on: the session has ended.
async function SignOutWithF3(page: Page): Promise<void> {
    const optionList = page.getByRole("navigation", { name: MENU_OPTIONS_LANDMARK_NAME });
    const firstOption = optionList.getByRole("button").first();
    await expect(firstOption).toBeVisible();
    await expect(firstOption).toBeEnabled();
    await page.keyboard.press("F3");
    await ExpectMessage(page, THANK_YOU_MESSAGE);
    await page.goto(ADMIN_ROUTE);
    await WaitForPath(page, SIGN_ON_ROUTE);
}

// The admin-only guard refuses the page's read with 403, and the client returns the user to
// their own menu, which shows the refusal.
async function ExpectAdminTreeRefused(page: Page): Promise<void> {
    await page.goto(ADMIN_USERS_ROUTE);
    await WaitForPath(page, MENU_ROUTE);
    await ExpectMessage(page, ADMIN_ONLY_MESSAGE);
}

// The fixtures a test uses are read from its first parameter, which must stay destructured.
interface PageFixtures {
    page: Page;
}

const ADMIN_TREE_REFUSAL_STEP_TITLE =
    "Direct navigation to /admin/users returns to /menu with the admin-only literal";

async function AdminMenuNavigation({ page }: PageFixtures): Promise<void> {
    const session: MenuSession = { page, menuRoute: ADMIN_ROUTE };
    await test.step("Sign on as the admin user and land on /admin", async () => {
        await SignOnAndLand(page, AdminUser(), ADMIN_ROUTE);
    });
    await test.step("Options 1–4 open the user-security pages", async () => {
        await OpenEveryOption(session, ADMIN_OPTION_ROUTES);
    });
    await test.step("Options 5–6 show the legacy not-installed literal", async () => {
        await ExpectAdminOptionsNotInstalled(session);
    });
    await test.step("F3 signs out with the thank-you literal", async () => {
        await SignOutWithF3(page);
    });
}

async function MainMenuNavigation({ page }: PageFixtures): Promise<void> {
    const session: MenuSession = { page, menuRoute: MENU_ROUTE };
    await test.step("Sign on as the regular user and land on /menu", async () => {
        await SignOnAndLand(page, RegularUser(), MENU_ROUTE);
    });
    await test.step("Options 1–10 open their pages", async () => {
        await OpenEveryOption(session, MAIN_OPTION_ROUTES);
    });
    await test.step("Option 11 shows the legacy not-installed literal", async () => {
        await ExpectNotInstalled(session, AUTHORIZATION_NOT_INSTALLED);
    });
    await test.step(ADMIN_TREE_REFUSAL_STEP_TITLE, async () => {
        await ExpectAdminTreeRefused(page);
    });
}

// Random markers that no page, fixture or log holds, so a marker found in a failure's text can
// only have come from the entry. USERID is BMS LENGTH=8, so its marker is 8 hex digits.
const USER_ID_MARKER_BYTES = 4;
const PASSWORD_MARKER_BYTES = 8;

// The COSGN00 USERID field by its verbatim BMS label, and every password input of the page.
const SIGN_ON_USER_ID_NAME = "User ID     :";
const PASSWORD_INPUT_SELECTOR = 'input[type="password"]';

// A read-only field fails the entry after this long instead of at the test timeout.
const LOCKED_ENTRY_TIMEOUT_MS = 5_000;

function MarkerCredentials(): Credentials {
    return {
        userId: randomBytes(USER_ID_MARKER_BYTES).toString("hex"),
        password: randomBytes(PASSWORD_MARKER_BYTES).toString("hex"),
    };
}

// Runs in the page before its own scripts. Each password input is made read-only when it
// appears, and again if the page lifts the attribute; setting it only on an editable input
// keeps the observer's own change from triggering it without end.
function LockPasswordInputs(selector: string): void {
    const lockAll = (): void => {
        for (const input of document.querySelectorAll<HTMLInputElement>(selector)) {
            if (!input.readOnly) {
                input.readOnly = true;
            }
        }
    };
    new MutationObserver(lockAll).observe(document, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ["readonly"],
    });
}

// What a reporter prints for a failure: the message and stack of the error and of each Error in
// its cause chain. A chain that loops back on itself is read once.
function ErrorChainText(error: unknown): string {
    const chainParts: string[] = [];
    const seenErrors = new Set<Error>();
    let current = error;
    while (current instanceof Error && !seenErrors.has(current)) {
        seenErrors.add(current);
        chainParts.push(current.message, current.stack ?? "");
        current = current.cause;
    }
    return chainParts.join("\n");
}

// SignOn must fail at the password, with the user ID already entered, and the text of that
// failure must hold neither marker.
async function LockedPasswordEntry({ page }: PageFixtures): Promise<void> {
    const markers = MarkerCredentials();
    await page.addInitScript(LockPasswordInputs, PASSWORD_INPUT_SELECTOR);
    page.setDefaultTimeout(LOCKED_ENTRY_TIMEOUT_MS);
    const entryError = await SignOn(page, markers).then(
        () => undefined,
        (error: unknown) => error,
    );
    expect(entryError, "sign-on with a read-only password field").toBeInstanceOf(Error);
    const userIdField = page.getByRole("textbox", { name: SIGN_ON_USER_ID_NAME, exact: true });
    await expect(userIdField, "user ID entered before the failure").toHaveValue(markers.userId);
    const failureText = ErrorChainText(entryError);
    expect(failureText, "failure text holds no user ID").not.toContain(markers.userId);
    expect(failureText, "failure text holds no password").not.toContain(markers.password);
}

test.describe("admin user (type A) on the CA00 admin menu", () => {
    test(
        "admin menu opens options 1–4, reports 5–6 not installed, and F3 signs out",
        AdminMenuNavigation,
    );
});

test.describe("regular user on the CM00 main menu", () => {
    test(
        "main menu opens options 1–10, reports 11 not installed, and refuses /admin/users",
        MainMenuNavigation,
    );
});

test.describe("sign-on credential entry", () => {
    test(
        "a failed password entry leaves no credential in the error, its call log or causes",
        LockedPasswordEntry,
    );
});
