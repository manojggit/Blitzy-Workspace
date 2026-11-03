// Replaces: the BMS map header fields (TITLE01, TITLE02, TRNNAME, PGMNAME, CURDATE,
// CURTIME) common to all 17 mapsets; copybooks COTTL01Y, CSDAT01Y
"use client";

import {
    AppBar,
    Container,
    LinearProgress,
    Paper,
    Stack,
    Toolbar,
    Typography,
    type SxProps,
    type Theme,
} from "@mui/material";
import { useSyncExternalStore, type ReactNode } from "react";

import { useSession } from "@/lib/session";

// COTTL01Y CCDA-TITLE01 and CCDA-TITLE02 without their X(40) centring blanks: the header layout
// centres them instead.
const APP_TITLE_LINE_1 = "AWS Mainframe Modernization";
const APP_TITLE_LINE_2 = "CardDemo";

const CLOCK_REFRESH_MS = 1000;
const MILLISECONDS_PER_SECOND = 1000;

// The BMS INITIAL values of CURDATE and CURTIME. They show until the client reads the clock
// after hydration, so the server render and the hydrating client render produce the same text.
const DATE_PLACEHOLDER = "mm/dd/yy";
const TIME_PLACEHOLDER = "hh:mm:ss";

// The three header groups (Tran/Prog, titles, Date/Time) share one row and wrap onto further
// rows on narrow viewports.
const HEADER_ROW_STYLE: SxProps<Theme> = {
    width: 1,
    flexWrap: "wrap",
    justifyContent: "space-between",
    alignItems: "center",
};

// Keeps wrapped header rows off the app bar's top and bottom edges.
const HEADER_TOOLBAR_STYLE: SxProps<Theme> = { py: 2 };

// The titles take the row space the side groups leave, so they stay centred there, and on a
// row of their own once the header wraps.
const APP_TITLE_STYLE: SxProps<Theme> = { flexGrow: 1, alignItems: "center", textAlign: "center" };

export interface ScreenShellProps {
    // The legacy transaction ID shown in TRNNAME, for example CT02.
    transactionId: string;
    // The legacy program name shown in PGMNAME, for example COTRN02C.
    programName: string;
    // The map's row-4 screen title, shown verbatim, for example "Add Transaction".
    screenTitle: string;
    isBusy?: boolean;
    children: ReactNode;
}

interface ShellHeaderProps {
    transactionId: string;
    programName: string;
}

interface HeaderMetaProps {
    lines: readonly string[];
    children?: ReactNode;
}

interface HeaderClock {
    dateText: string;
    timeText: string;
}

function PadTwo(value: number): string {
    return String(value).padStart(2, "0");
}

// WS-CURDATE-MM-DD-YY in local time; the year keeps its last two digits, as
// WS-CURDATE-YEAR(3:2) does.
export function FormatHeaderDate(now: Date): string {
    const yearText = PadTwo(now.getFullYear() % 100);
    return `${PadTwo(now.getMonth() + 1)}/${PadTwo(now.getDate())}/${yearText}`;
}

// WS-CURTIME-HH-MM-SS: 24-hour local time.
export function FormatHeaderTime(now: Date): string {
    return `${PadTwo(now.getHours())}:${PadTwo(now.getMinutes())}:${PadTwo(now.getSeconds())}`;
}

// The clock as whole epoch seconds: the value changes once a second, so React reads the same
// snapshot between ticks and re-renders only when the shown time changes.
function ReadClockSecond(): number {
    return Math.floor(Date.now() / MILLISECONDS_PER_SECOND);
}

// The server render and the hydrating client render read no clock, so both show the
// placeholders and hydration matches.
function ReadServerClockSecond(): null {
    return null;
}

// The interval lives only while a header is subscribed; unsubscribing on unmount clears it.
function SubscribeToClock(onTick: () => void): () => void {
    const intervalId = window.setInterval(onTick, CLOCK_REFRESH_MS);
    return () => window.clearInterval(intervalId);
}

// The browser's local time, as CICS ASKTIME gave the region's local time. An external store
// rather than state set in an effect, which the react-hooks lint gate rejects. React recognises
// hooks only by the camelCase "use" prefix, so this name is a framework-mandated exception to
// the MK_Modernization Rule's PascalCase function naming.
function useHeaderClock(): HeaderClock {
    const clockSecond = useSyncExternalStore<number | null>(
        SubscribeToClock,
        ReadClockSecond,
        ReadServerClockSecond,
    );
    if (clockSecond === null) {
        return { dateText: DATE_PLACEHOLDER, timeText: TIME_PLACEHOLDER };
    }
    const now = new Date(clockSecond * MILLISECONDS_PER_SECOND);
    return { dateText: FormatHeaderDate(now), timeText: FormatHeaderTime(now) };
}

// Each line is one text node, label and value together, for example "Tran: CT02". Lines keep
// fixed positions, so the position is the key: a key holding the text would remount the Time
// line on every tick.
function HeaderMeta({ lines, children }: HeaderMetaProps) {
    return (
        <Stack>
            {lines.map((line, linePosition) => (
                <Typography key={linePosition} variant="labelMedium" component="p">
                    {line}
                </Typography>
            ))}
            {children}
        </Stack>
    );
}

// Only the signed-in user ID fills the header: never the user type, role or other identity data.
function SignedInUser() {
    const { currentUser } = useSession();
    if (currentUser === null) {
        return null;
    }
    return (
        <Typography variant="labelMedium" component="p">
            {`User: ${currentUser.user_id}`}
        </Typography>
    );
}

function AppTitle() {
    return (
        <Stack sx={APP_TITLE_STYLE}>
            <Typography variant="titleLarge" component="p">
                {APP_TITLE_LINE_1}
            </Typography>
            <Typography variant="titleLarge" component="p">
                {APP_TITLE_LINE_2}
            </Typography>
        </Stack>
    );
}

// The AppBar text inherits primary.contrastText from color="primary".
function ShellHeader({ transactionId, programName }: ShellHeaderProps) {
    const { dateText, timeText } = useHeaderClock();
    return (
        <AppBar position="static" color="primary">
            <Toolbar sx={HEADER_TOOLBAR_STYLE}>
                <Stack direction="row" spacing={4} useFlexGap sx={HEADER_ROW_STYLE}>
                    <HeaderMeta lines={[`Tran: ${transactionId}`, `Prog: ${programName}`]}>
                        <SignedInUser />
                    </HeaderMeta>
                    <AppTitle />
                    <HeaderMeta lines={[`Date: ${dateText}`, `Time: ${timeText}`]} />
                </Stack>
            </Toolbar>
        </AppBar>
    );
}

// The page frame every BMS-map page renders inside; each page places its own MessageLine and
// FunctionKeyBar in children.
export function ScreenShell(props: ScreenShellProps) {
    return (
        <>
            <ShellHeader transactionId={props.transactionId} programName={props.programName} />
            {props.isBusy ? (
                <LinearProgress variant="indeterminate" aria-label="Processing" />
            ) : null}
            <Container component="main" maxWidth="lg" sx={{ py: 6 }}>
                <Paper sx={{ p: 6 }}>
                    <Stack spacing={4}>
                        <Typography component="h1" variant="headlineSmall">
                            {props.screenTitle}
                        </Typography>
                        {props.children}
                    </Stack>
                </Paper>
            </Container>
        </>
    );
}
