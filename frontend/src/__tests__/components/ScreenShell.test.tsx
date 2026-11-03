// Tests ScreenShell, which replaces the common BMS header (TITLE01/TITLE02, TRNNAME, PGMNAME,
// CURDATE, CURTIME; cpy/COTTL01Y.cpy, cpy/CSDAT01Y.cpy).
import { Typography } from "@mui/material";
import { ThemeProvider } from "@mui/material/styles";
import { render, screen } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";

import { CARD_DEMO_THEME } from "@/app/theme";
import {
    FormatHeaderDate,
    FormatHeaderTime,
    ScreenShell,
    type ScreenShellProps,
} from "@/components/ScreenShell";
import type { CurrentUserResponse } from "@/lib/api/auth";

// Read by the mocked useSession at render time; the "mock" prefix lets the hoisted factory use it.
let mockCurrentUser: CurrentUserResponse | null = null;

// The mocked keys mirror the exports of @/lib/session and its SessionContextValue.
jest.mock("@/lib/session", () => ({
    SessionProvider: ({ children }: { children: ReactNode }) => children,
    useSession: () => ({
        currentUser: mockCurrentUser,
        isLoading: false,
        RefreshSession: jest.fn(),
        ClearSession: jest.fn(),
    }),
}));

interface ClockCase {
    now: Date;
    date: string;
    time: string;
}

// COTTL01Y CCDA-TITLE01 and CCDA-TITLE02 without their X(40) centring blanks.
const TITLE_LINE_1 = "AWS Mainframe Modernization";
const TITLE_LINE_2 = "CardDemo";

// The COTRN02 header: TRNNAME, PGMNAME and the row-4 title of bms/COTRN02.bms.
const CT02_SHELL_PROPS = {
    transactionId: "CT02",
    programName: "COTRN02C",
    screenTitle: "Add Transaction",
};

const CHILD_TEXT = "Enter Acct #:";

// Local time, as the header shows the browser's local clock.
const FIXED_NOW = new Date(2022, 6, 18, 9, 5, 7);

// WS-CURDATE-MM-DD-YY and WS-CURTIME-HH-MM-SS: two-digit year, 24-hour time, zero padding.
const CLOCK_CASES: readonly ClockCase[] = [
    { now: FIXED_NOW, date: "07/18/22", time: "09:05:07" },
    { now: new Date(2023, 11, 31, 23, 59, 59), date: "12/31/23", time: "23:59:59" },
    { now: new Date(2000, 0, 1, 0, 0, 0), date: "01/01/00", time: "00:00:00" },
];

// isBusy, then the expected number of progress bars.
const BUSY_CASES: readonly [boolean | undefined, number][] = [
    [true, 1],
    [false, 0],
    [undefined, 0],
];

// The session user carries an ID and names only; CurrentUserResponse has no password.
const SIGNED_ON_USER: CurrentUserResponse = {
    user_id: "TESTUSR1",
    user_type: "U",
    first_name: "Test",
    last_name: "User",
    next_route: "/menu",
};

function RenderWithTheme(ui: ReactElement) {
    return render(<ThemeProvider theme={CARD_DEMO_THEME}>{ui}</ThemeProvider>);
}

function RenderShell(overrides: Partial<ScreenShellProps> = {}) {
    return RenderWithTheme(
        <ScreenShell {...CT02_SHELL_PROPS} {...overrides}>
            <Typography>{CHILD_TEXT}</Typography>
        </ScreenShell>,
    );
}

afterEach(() => {
    mockCurrentUser = null;
});

describe("ScreenShell COTTL01Y titles, TRNNAME and PGMNAME", () => {
    it("COTTL01Y TITLE01/TITLE02 render in the header", () => {
        RenderShell();
        const banner = screen.getByRole("banner");
        expect(banner).toContainElement(screen.getByText(TITLE_LINE_1));
        expect(banner).toContainElement(screen.getByText(TITLE_LINE_2));
    });

    it("TRNNAME and PGMNAME render as Tran: and Prog:", () => {
        RenderShell();
        const banner = screen.getByRole("banner");
        expect(banner).toContainElement(screen.getByText("Tran: CT02"));
        expect(banner).toContainElement(screen.getByText("Prog: COTRN02C"));
    });

    it("screen title is the level-1 heading and children render", () => {
        RenderShell();
        expect(screen.getByRole("heading", { level: 1, name: "Add Transaction" })).toBeVisible();
        expect(screen.getByText(CHILD_TEXT)).toBeInTheDocument();
    });
});

describe("ScreenShell CSDAT01Y CURDATE/CURTIME", () => {
    beforeEach(() => {
        jest.useFakeTimers();
        jest.setSystemTime(FIXED_NOW);
    });

    afterEach(() => {
        jest.useRealTimers();
    });

    it.each(CLOCK_CASES)("CURDATE/CURTIME format $date $time", ({ now, date, time }) => {
        expect(FormatHeaderDate(now)).toBe(date);
        expect(FormatHeaderTime(now)).toBe(time);
    });

    it("CURDATE and CURTIME render Date: and Time: from the clock", () => {
        RenderShell();
        const banner = screen.getByRole("banner");
        expect(banner).toContainElement(screen.getByText("Date: 07/18/22"));
        expect(banner).toContainElement(screen.getByText("Time: 09:05:07"));
    });
});

describe("ScreenShell busy indicator", () => {
    it.each(BUSY_CASES)(
        "LinearProgress shows only while busy (isBusy %p)",
        (isBusy, progressBarCount) => {
            RenderShell({ isBusy });
            expect(screen.queryAllByRole("progressbar")).toHaveLength(progressBarCount);
        },
    );
});

describe("ScreenShell signed-on user", () => {
    it("header shows User: with a signed-on user", () => {
        mockCurrentUser = SIGNED_ON_USER;
        RenderShell();
        expect(screen.getByRole("banner")).toContainElement(screen.getByText("User: TESTUSR1"));
    });

    it("header omits User: without a session user", () => {
        mockCurrentUser = null;
        RenderShell();
        expect(screen.queryByText(/^User:/)).toBeNull();
    });
});
