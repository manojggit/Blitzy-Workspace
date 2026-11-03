// Tests: app/providers.tsx, the client Providers that app/layout.tsx wraps around every BMS-map
// page: ThemeProvider with the Material Design 3 theme of app/theme.ts (AAP 0.5.3), CssBaseline,
// and the lib/session.tsx SessionProvider that replaces the COCOM01Y CDEMO-USER-ID and
// CDEMO-USER-TYPE COMMAREA fields. Only the GET /auth/me wrapper and the layout's font and
// Emotion-cache boundaries are mocked; the theme, the session context and MUI are the real modules.
import { Typography } from "@mui/material";
import { useTheme, type Theme, type TypographyVariants } from "@mui/material/styles";
import { act, render, screen, waitFor } from "@testing-library/react";
import { useEffect, type ReactElement, type ReactNode } from "react";

import RootLayout from "@/app/layout";
import { Providers } from "@/app/providers";
import { CARD_DEMO_THEME } from "@/app/theme";
import { ScreenShell } from "@/components/ScreenShell";
import { FetchCurrentUser, type CurrentUserResponse } from "@/lib/api/auth";
import { useSession, type SessionContextValue } from "@/lib/session";

jest.mock("@/lib/api/auth");

// The layout's build-time font loader and its server-side Emotion cache: boundaries only. The font
// factory runs while the imports above load, before this file's constants exist, so its class name
// is written out again in ROBOTO_VARIABLE_CLASS.
jest.mock("next/font/google", () => ({
    Roboto: () => ({ className: "roboto-font-class", variable: "roboto-font-variable" }),
}));
jest.mock("@mui/material-nextjs/v16-appRouter", () => ({
    AppRouterCacheProvider: ({ children }: { children: ReactNode }) => children,
}));

const fetchCurrentUserMock = jest.mocked(FetchCurrentUser);

// What a page rendered under Providers reads from the theme and session contexts.
interface ProvidedContexts {
    theme: Theme;
    session: SessionContextValue;
}

interface ContextProbeProps {
    onValue: (contexts: ProvidedContexts) => void;
}

type ThemeReader = (theme: Theme) => string | number | undefined;

interface ThemeValueCase {
    token: string;
    read: ThemeReader;
    expected: string | number;
}

interface TypeRoleCase {
    variant: keyof TypographyVariants;
    fontSize: string;
    lineHeight: string;
    fontWeight: number;
}

interface BodyElementProps {
    children: ReactElement;
}

interface HtmlElementProps {
    lang: string;
    className: string;
    children: ReactElement<BodyElementProps>;
}

// Eight characters, the CDEMO-USER-ID PIC X(08) width; fictitious, not a USRSEC seed user.
const TEST_USER_ID = "TESTUSR1";
const SHELL_USER_LINE = `User: ${TEST_USER_ID}`;
const NO_USER_SHOWN = "none";
const USER_ID_TEST_ID = "provided-user-id";

const SIGNED_ON_USER: CurrentUserResponse = {
    user_id: TEST_USER_ID,
    user_type: "U",
    first_name: "TEST",
    last_name: "USER",
    next_route: "/menu",
};

// The COTRN02 header: TRNNAME, PGMNAME and the row-4 title of bms/COTRN02.bms.
const CT02_SHELL_PROPS = {
    transactionId: "CT02",
    programName: "COTRN02C",
    screenTitle: "Add Transaction",
};

const ROBOTO_FONT_STACK = "var(--font-roboto, Roboto), Roboto, Helvetica, Arial, sans-serif";
const ROBOTO_VARIABLE_CLASS = "roboto-font-variable";

// The header clock reads Date.now(); one fixed local instant keeps the header still, so its
// one-second interval never re-renders outside act.
const FIXED_CLOCK_MILLISECONDS = new Date(2022, 6, 18, 9, 5, 7).getTime();

// The Material Design 3 baseline colour roles, written out here rather than read from app/theme.ts.
const M3_COLOUR_CASES: readonly ThemeValueCase[] = [
    { token: "primary", read: (theme) => theme.palette.primary.main, expected: "#6750A4" },
    {
        token: "onPrimary",
        read: (theme) => theme.palette.primary.contrastText,
        expected: "#FFFFFF",
    },
    {
        token: "primaryContainer",
        read: (theme) => theme.palette.m3.primaryContainer,
        expected: "#EADDFF",
    },
    {
        token: "onPrimaryContainer",
        read: (theme) => theme.palette.m3.onPrimaryContainer,
        expected: "#4F378B",
    },
    { token: "secondary", read: (theme) => theme.palette.secondary.main, expected: "#625B71" },
    { token: "tertiary", read: (theme) => theme.palette.m3.tertiary, expected: "#7D5260" },
    { token: "error", read: (theme) => theme.palette.error.main, expected: "#B3261E" },
    {
        token: "errorContainer",
        read: (theme) => theme.palette.m3.errorContainer,
        expected: "#F9DEDC",
    },
    { token: "surface", read: (theme) => theme.palette.background.default, expected: "#FEF7FF" },
    {
        token: "surfaceContainer",
        read: (theme) => theme.palette.background.paper,
        expected: "#F3EDF7",
    },
    { token: "onSurface", read: (theme) => theme.palette.text.primary, expected: "#1D1B20" },
    {
        token: "onSurfaceVariant",
        read: (theme) => theme.palette.text.secondary,
        expected: "#49454F",
    },
    { token: "outline", read: (theme) => theme.palette.m3.outline, expected: "#79747E" },
    { token: "outlineVariant", read: (theme) => theme.palette.divider, expected: "#CAC4D0" },
];

// The Material Design 3 corner-radius scale (px) and the 4 dp spacing grid.
const M3_MEASURE_CASES: readonly ThemeValueCase[] = [
    {
        token: "corner medium (borderRadius)",
        read: (theme) => theme.shape.borderRadius,
        expected: 12,
    },
    { token: "corner extra-small", read: (theme) => theme.shape.m3.extraSmall, expected: 4 },
    { token: "corner small", read: (theme) => theme.shape.m3.small, expected: 8 },
    { token: "corner medium", read: (theme) => theme.shape.m3.medium, expected: 12 },
    { token: "corner large", read: (theme) => theme.shape.m3.large, expected: 16 },
    { token: "corner extra-large", read: (theme) => theme.shape.m3.extraLarge, expected: 28 },
    { token: "spacing(4)", read: (theme) => theme.spacing(4), expected: "16px" },
];

// The M3 type scale (m3.material.io): size, line height and weight of each role.
const M3_TYPE_ROLE_CASES: readonly TypeRoleCase[] = [
    { variant: "displayLarge", fontSize: "57px", lineHeight: "64px", fontWeight: 400 },
    { variant: "displayMedium", fontSize: "45px", lineHeight: "52px", fontWeight: 400 },
    { variant: "displaySmall", fontSize: "36px", lineHeight: "44px", fontWeight: 400 },
    { variant: "headlineLarge", fontSize: "32px", lineHeight: "40px", fontWeight: 400 },
    { variant: "headlineMedium", fontSize: "28px", lineHeight: "36px", fontWeight: 400 },
    { variant: "headlineSmall", fontSize: "24px", lineHeight: "32px", fontWeight: 400 },
    { variant: "titleLarge", fontSize: "22px", lineHeight: "28px", fontWeight: 400 },
    { variant: "titleMedium", fontSize: "16px", lineHeight: "24px", fontWeight: 500 },
    { variant: "titleSmall", fontSize: "14px", lineHeight: "20px", fontWeight: 500 },
    { variant: "bodyLarge", fontSize: "16px", lineHeight: "24px", fontWeight: 400 },
    { variant: "bodyMedium", fontSize: "14px", lineHeight: "20px", fontWeight: 400 },
    { variant: "bodySmall", fontSize: "12px", lineHeight: "16px", fontWeight: 400 },
    { variant: "labelLarge", fontSize: "14px", lineHeight: "20px", fontWeight: 500 },
    { variant: "labelMedium", fontSize: "12px", lineHeight: "16px", fontWeight: 500 },
    { variant: "labelSmall", fontSize: "11px", lineHeight: "16px", fontWeight: 500 },
];

let capturedContexts: ProvidedContexts | null = null;
let consoleErrorSpy: jest.SpyInstance;

// A page's view of Providers: the theme and session its MUI descendants receive.
function ContextProbe({ onValue }: ContextProbeProps): ReactElement {
    const theme = useTheme();
    const session = useSession();
    useEffect(() => {
        onValue({ theme, session });
    }, [onValue, theme, session]);
    return (
        <Typography data-testid={USER_ID_TEST_ID}>
            {session.currentUser?.user_id ?? NO_USER_SHOWN}
        </Typography>
    );
}

function CaptureContexts(contexts: ProvidedContexts): void {
    capturedContexts = contexts;
}

function LatestContexts(): ProvidedContexts {
    if (capturedContexts === null) {
        throw new Error("ContextProbe has not rendered under Providers");
    }
    return capturedContexts;
}

function RenderProbe(): void {
    render(
        <Providers>
            <ContextProbe onValue={CaptureContexts} />
        </Providers>,
    );
}

async function WaitForSessionLoaded(): Promise<void> {
    await waitFor(() => {
        expect(LatestContexts().session.isLoading).toBe(false);
    });
}

async function RenderSignedOnProbe(): Promise<ProvidedContexts> {
    fetchCurrentUserMock.mockResolvedValue(SIGNED_ON_USER);
    RenderProbe();
    await WaitForSessionLoaded();
    return LatestContexts();
}

// The resolver of a /auth/me read that stays pending until the test settles it.
function DeferCurrentUser(): (user: CurrentUserResponse) => void {
    let resolveUser: (user: CurrentUserResponse) => void = () => undefined;
    fetchCurrentUserMock.mockReturnValue(
        new Promise<CurrentUserResponse>((resolve) => {
            resolveUser = resolve;
        }),
    );
    return (user) => {
        resolveUser(user);
    };
}

function RenderShellInProviders(): void {
    render(
        <Providers>
            <ScreenShell {...CT02_SHELL_PROPS}>
                <Typography>Enter Acct #:</Typography>
            </ScreenShell>
        </Providers>,
    );
}

// createTheme keeps the case the author wrote, so colours compare case-insensitively.
function NormaliseColour(value: string | number | undefined): string {
    return String(value).toUpperCase();
}

// Emotion drops the blank after each comma of a font stack; one blank is restored for comparison.
function NormaliseFontStack(value: string): string {
    return value.replace(/,\s*/g, ", ");
}

// MUI's ThemeProvider hands descendants a shallow copy of a theme without CSS variables, with
// vars set to null, so CARD_DEMO_THEME is recognised by its own members rather than by identity.
function ExpectCardDemoTheme(theme: Theme): void {
    expect(theme).toMatchObject(CARD_DEMO_THEME);
    expect(theme.palette).toBe(CARD_DEMO_THEME.palette);
    expect(theme.typography).toBe(CARD_DEMO_THEME.typography);
    expect(theme.components).toBe(CARD_DEMO_THEME.components);
}

function CustomPropertyColour(element: Element, propertyName: string): string {
    return NormaliseColour(getComputedStyle(element).getPropertyValue(propertyName).trim());
}

beforeEach(() => {
    jest.spyOn(Date, "now").mockReturnValue(FIXED_CLOCK_MILLISECONDS);
    consoleErrorSpy = jest.spyOn(console, "error");
    fetchCurrentUserMock.mockReset();
    sessionStorage.clear();
    capturedContexts = null;
});

// React reports act(), DOM-nesting and context misuse through console.error.
afterEach(() => {
    expect(consoleErrorSpy).not.toHaveBeenCalled();
    jest.restoreAllMocks();
});

it("a descendant of Providers receives CARD_DEMO_THEME through useTheme()", async () => {
    const { theme } = await RenderSignedOnProbe();
    ExpectCardDemoTheme(theme);
});

it.each(M3_COLOUR_CASES)(
    "a descendant of Providers reads M3 colour role $token as $expected",
    async ({ read, expected }) => {
        const { theme } = await RenderSignedOnProbe();
        expect(NormaliseColour(read(theme))).toBe(expected);
    },
);

it.each(M3_MEASURE_CASES)(
    "a descendant of Providers reads M3 $token as $expected",
    async ({ read, expected }) => {
        const { theme } = await RenderSignedOnProbe();
        expect(read(theme)).toBe(expected);
    },
);

it.each(M3_TYPE_ROLE_CASES)(
    "a descendant of Providers reads M3 type role $variant in Roboto at $fontSize/$lineHeight",
    async ({ variant, fontSize, lineHeight, fontWeight }) => {
        const { theme } = await RenderSignedOnProbe();
        expect(theme.typography[variant]).toMatchObject({
            fontFamily: ROBOTO_FONT_STACK,
            fontSize,
            lineHeight,
            fontWeight,
        });
    },
);

it("a descendant of Providers is loading, then holds the /auth/me user, read once", async () => {
    const resolveUser = DeferCurrentUser();
    RenderProbe();
    expect(LatestContexts().session).toMatchObject({ isLoading: true, currentUser: null });
    expect(screen.getByTestId(USER_ID_TEST_ID)).toHaveTextContent(NO_USER_SHOWN);
    await act(async () => {
        resolveUser(SIGNED_ON_USER);
    });
    expect(LatestContexts().session).toMatchObject({
        isLoading: false,
        currentUser: SIGNED_ON_USER,
    });
    expect(screen.getByTestId(USER_ID_TEST_ID)).toHaveTextContent(TEST_USER_ID);
    expect(fetchCurrentUserMock).toHaveBeenCalledTimes(1);
    expect(fetchCurrentUserMock).toHaveBeenCalledWith({ signal: expect.any(AbortSignal) });
});

it("CssBaseline inside the theme gives <body> the M3 surface, on-surface and Roboto", async () => {
    await RenderSignedOnProbe();
    expect(document.body).toHaveStyle({
        margin: "0px",
        backgroundColor: "#FEF7FF",
        color: "#1D1B20",
    });
    expect(NormaliseFontStack(getComputedStyle(document.body).fontFamily)).toBe(ROBOTO_FONT_STACK);
});

it("ScreenShell under Providers shows User: from /auth/me once the read answers", async () => {
    const resolveUser = DeferCurrentUser();
    RenderShellInProviders();
    expect(screen.queryByText(/^User:/)).toBeNull();
    await act(async () => {
        resolveUser(SIGNED_ON_USER);
    });
    expect(screen.getByRole("banner")).toContainElement(screen.getByText(SHELL_USER_LINE));
    expect(fetchCurrentUserMock).toHaveBeenCalledTimes(1);
});

it("ScreenShell under Providers renders in the M3 colour roles and type scale", async () => {
    fetchCurrentUserMock.mockResolvedValue(SIGNED_ON_USER);
    RenderShellInProviders();
    await screen.findByText(SHELL_USER_LINE);
    const banner = screen.getByRole("banner");
    expect(CustomPropertyColour(banner, "--AppBar-background")).toBe("#6750A4");
    expect(CustomPropertyColour(banner, "--AppBar-color")).toBe("#FFFFFF");
    expect(screen.getByText("CardDemo")).toHaveStyle({ fontSize: "22px", lineHeight: "28px" });
    const heading = screen.getByRole("heading", { level: 1, name: "Add Transaction" });
    expect(heading).toHaveStyle({ fontSize: "24px", lineHeight: "32px" });
    expect(screen.getByRole("main").firstElementChild).toHaveStyle({ backgroundColor: "#F3EDF7" });
});

// RootLayout uses no hooks, so calling it yields its element tree. Only the <body> content is
// rendered, because the test container already sits inside jsdom's <body>.
it("RootLayout wraps its page in Providers on <html lang=en> with the Roboto class", async () => {
    fetchCurrentUserMock.mockResolvedValue(SIGNED_ON_USER);
    const page = <ContextProbe onValue={CaptureContexts} />;
    const htmlElement = RootLayout({ children: page }) as ReactElement<HtmlElementProps>;
    expect(htmlElement.type).toBe("html");
    expect(htmlElement.props).toMatchObject({ lang: "en", className: ROBOTO_VARIABLE_CLASS });
    const bodyElement = htmlElement.props.children;
    expect(bodyElement.type).toBe("body");
    render(bodyElement.props.children);
    await WaitForSessionLoaded();
    ExpectCardDemoTheme(LatestContexts().theme);
    expect(LatestContexts().session.currentUser).toEqual(SIGNED_ON_USER);
});
