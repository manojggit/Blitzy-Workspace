// CardDemo role-navigation end-to-end flow (Technical Specification 0.9.1 success item 4, 0.9.2).
//
// Exercises: COSGN00C (CC00), whose sign-on sends user type A to the admin menu and every other
// type to the main menu; COMEN01C with the COMEN02Y option table (CM00); COADM01C with the
// COADM02Y option table (CA00); and the CSMSG01Y thank-you literal of the F3 sign-off. Each XCTL
// to an option's program is a page navigation here, and each option of an excluded module answers
// with its menu's legacy "not installed" literal. The spec only reads data, so it does not depend
// on day-in-the-life.spec.ts, and each test runs in a fresh browser context with its own session
// cookie. Credentials come from frontend/.env.e2e through AdminUser() and RegularUser() only.
import { expect, test, type Page } from "@playwright/test";

import {
    AdminUser,
    ExpectMessage,
    RegularUser,
    SIGN_ON_ROUTE,
    SignOn,
    WaitForPath,
    type Credentials,
} from "./global-setup";

const MENU_ROUTE = "/menu";
const ADMIN_ROUTE = "/admin";
const ADMIN_USERS_ROUTE = "/admin/users";

/** One installed menu option: its verbatim label and the page that replaces its program's XCTL. */
interface MenuOptionRoute {
    label: string;
    route: string;
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

// COADM02Y options 1-4, CDEMO-ADMIN-OPT-NAME with its trailing blanks trimmed.
const ADMIN_OPTION_ROUTES: readonly MenuOptionRoute[] = [
    { label: "User List (Security)", route: "/admin/users" },
    { label: "User Add (Security)", route: "/admin/users/add" },
    { label: "User Update (Security)", route: "/admin/users/update" },
    { label: "User Delete (Security)", route: "/admin/users/delete" },
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

// COMEN02Y options 1-10, CDEMO-MENU-OPT-NAME with its trailing blanks trimmed.
const MAIN_OPTION_ROUTES: readonly MenuOptionRoute[] = [
    { label: "Account View", route: "/accounts/view" },
    { label: "Account Update", route: "/accounts/update" },
    { label: "Credit Card List", route: "/cards" },
    { label: "Credit Card View", route: "/cards/view" },
    { label: "Credit Card Update", route: "/cards/update" },
    { label: "Transaction List", route: "/transactions" },
    { label: "Transaction View", route: "/transactions/view" },
    { label: "Transaction Add", route: "/transactions/add" },
    { label: "Transaction Reports", route: "/reports/transactions" },
    { label: "Bill Payment", route: "/billpay" },
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

// CSMSG01Y CCDA-MSG-THANK-YOU. ExpectMessage matches a substring, so the copybook's trailing
// padding does not matter.
const THANK_YOU_MESSAGE = "Thank you for using CardDemo application...";

// COMEN01C line 139, trailing blank included; the 403 of the admin-only guard carries it.
const ADMIN_ONLY_MESSAGE = "No access - Admin Only option... ";

// The option-list landmark of both menus: "Main menu options" and "Admin menu options".
const MENU_OPTIONS_LANDMARK_NAME = /menu options/i;

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

// The menu is reached again by navigation rather than by the option page's F3, which can act on
// its own screen first (COUSR02C's F3 saves pending changes). The session cookie persists.
async function OpenOptionAndReturn(session: MenuSession, option: MenuOptionRoute): Promise<void> {
    await ClickMenuOption(session.page, option.label);
    await WaitForPath(session.page, option.route);
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

// The fixtures a test uses are read from its first parameter, which must stay destructured. The
// bodies are named functions so each describe callback stays within the MK_Modernization Rule's
// 20-line function limit, which counts the lines of nested callbacks.
interface PageFixtures {
    page: Page;
}

// Held apart only because the title is too long to share a line with test.step within 100 columns.
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
