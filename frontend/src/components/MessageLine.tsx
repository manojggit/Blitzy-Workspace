// Replaces: BMS ERRMSG/INFOMSG fields; copybooks CSMSG01Y, CSMSG02Y
"use client";

import { Alert, Stack, type SxProps, type Theme } from "@mui/material";
import { useEffect, useRef, type RefObject } from "react";
import type { MessageSeverity } from "@/lib/apiClient";

// BMS ERRMSG is defined COLOR=RED, so a message the page passes without a severity renders red.
const DEFAULT_SEVERITY: MessageSeverity = "error";
const INFO_SEVERITY: MessageSeverity = "info";

export interface MessageLineProps {
    // The server's ERRMSG literal, shown exactly as received.
    message?: string;
    severity?: MessageSeverity;
    // The server's INFOMSG literal (CAUP, CAVW, CCLI, CCDL, CCUP), shown exactly as received.
    infoMessage?: string;
    // The identity of the screen outcome shown; a new identity brings the line into view even
    // when the literal repeats.
    outcomeKey?: unknown;
}

/** The ERRMSG line's element id. A screen renders one MessageLine, so the id is unique. */
export const MESSAGE_LINE_ERROR_ID = "message-line-errmsg";
/** The INFOMSG line's element id. */
export const MESSAGE_LINE_INFO_ID = "message-line-infomsg";

// MUI TextField gives its helper text this id suffix and describes the input by that element.
const MUI_HELPER_TEXT_ID_SUFFIX = "-helper-text";

// The least scroll that shows the whole message line, and none while it is already in view.
const REVEAL_SCROLL_OPTIONS: ScrollIntoViewOptions = { block: "nearest" };
// The browser keeps whole-pixel scroll offsets, so a line at a fractional position could keep a
// sliver of its edge outside the viewport. A scroll margin of one theme spacing unit (4 dp) on
// each side, which only scrolling reads, keeps the whole line inside.
const REVEAL_SCROLL_MARGIN_UNITS = 1;
const MESSAGE_LINE_STYLE: SxProps<Theme> = {
    scrollMarginBlock: (theme) => theme.spacing(REVEAL_SCROLL_MARGIN_UNITS),
};

/** The input attribute that ties a field in error to the ERRMSG line. */
export interface ErrorMessageReferenceAttributes {
    "aria-describedby"?: string;
}

// The live-region semantics of one message line, which its Alert carries itself.
interface MessageRegion {
    id: string;
    testId: string;
    role: "alert" | "status";
    politeness: "assertive" | "polite";
}

// ERRMSG reports the outcome of the key just pressed, so it is announced at once.
const ERROR_REGION: MessageRegion = {
    id: MESSAGE_LINE_ERROR_ID,
    testId: "message-line-error",
    role: "alert",
    politeness: "assertive",
};

// INFOMSG is the screen's standing prompt, announced once the reader is idle.
const INFO_REGION: MessageRegion = {
    id: MESSAGE_LINE_INFO_ID,
    testId: "message-line-info",
    role: "status",
    politeness: "polite",
};

interface MessageAlertProps {
    text?: string;
    severity: MessageSeverity;
    region: MessageRegion;
}

// The trim only decides whether a line is shown; the text itself is rendered untouched.
function IsBlank(text?: string): boolean {
    return text === undefined || text.trim() === "";
}

// What the message line shows for one outcome; a blank line is held as "", as it shows nothing.
interface ShownOutcome {
    message: string;
    infoMessage: string;
    outcomeKey: unknown;
}

function ShownText(text?: string): string {
    if (text === undefined || IsBlank(text)) {
        return "";
    }
    return text;
}

// Another outcome: either line shows other text, or the page names another outcome identity.
function IsNewOutcome(previous: ShownOutcome, current: ShownOutcome): boolean {
    return (
        previous.message !== current.message ||
        previous.infoMessage !== current.infoMessage ||
        !Object.is(previous.outcomeKey, current.outcomeKey)
    );
}

function ShowsAnyLine(outcome: ShownOutcome): boolean {
    return outcome.message !== "" || outcome.infoMessage !== "";
}

// Whether the ERRMSG line reports the new outcome: it shows text, and that text or the outcome
// identity changed. Other INFOMSG text alone only updates the screen's standing prompt.
function ErrorLineReportsNewOutcome(previous: ShownOutcome, current: ShownOutcome): boolean {
    if (current.message === "") {
        return false;
    }
    return (
        previous.message !== current.message || !Object.is(previous.outcomeKey, current.outcomeKey)
    );
}

// The document events by which the user acts on a screen: a key pressed, or a pointer pressed.
const USER_ACTION_EVENTS: readonly (keyof DocumentEventMap)[] = ["keydown", "pointerdown"];
// Capture-phase listeners see every action, including one a field stops from propagating.
const LISTEN_IN_CAPTURE_PHASE = true;

// The scroll margin the browser adds above and below the line when it scrolls the line into view.
interface BlockScrollMargin {
    top: number;
    bottom: number;
}

// A computed length in pixels; a value the engine leaves unset adds nothing.
function PixelsOf(cssLength: string): number {
    const pixels = Number.parseFloat(cssLength);
    if (!Number.isFinite(pixels)) {
        return 0;
    }
    return pixels;
}

function ScrollMarginOf(element: Element): BlockScrollMargin {
    const style = window.getComputedStyle(element);
    return { top: PixelsOf(style.scrollMarginTop), bottom: PixelsOf(style.scrollMarginBottom) };
}

// How far a block "nearest" reveal scrolls the window down (negative: up). The line, widened by
// its scroll margin, is aligned with the viewport edge it crosses, and is left alone when inside.
function NearestRevealScroll(
    line: DOMRect,
    margin: BlockScrollMargin,
    viewportHeight: number,
): number {
    const overflowBelow = line.bottom + margin.bottom - viewportHeight;
    if (overflowBelow > 0) {
        return overflowBelow;
    }
    const overflowAbove = line.top - margin.top;
    if (overflowAbove < 0) {
        return overflowAbove;
    }
    return 0;
}

// Whether the element is still wholly inside the viewport once the window scrolls by scrollBy.
function StaysInView(element: DOMRect, scrollBy: number, viewportHeight: number): boolean {
    return element.top - scrollBy >= 0 && element.bottom - scrollBy <= viewportHeight;
}

// Whether revealing the line keeps the focused element wholly in view. With nothing focused
// there is no cursor to lose sight of.
function RevealKeepsFocusInView(wrapper: HTMLElement): boolean {
    const focused = document.activeElement;
    if (focused === null || focused === document.body) {
        return true;
    }
    const viewportHeight = window.innerHeight;
    const line = wrapper.getBoundingClientRect();
    const scrollBy = NearestRevealScroll(line, ScrollMarginOf(wrapper), viewportHeight);
    return StaysInView(focused.getBoundingClientRect(), scrollBy, viewportHeight);
}

// Once the user has acted on the screen the line is always revealed. Before that, only an
// outcome the ERRMSG line reports is, and only when the reveal leaves the cursor in sight, as the
// whole reveal or none of it; a change of the INFOMSG prompt alone waits for the user to act.
function RevealLine(
    wrapper: HTMLElement | null,
    userHasActed: boolean,
    errorLineReportsOutcome: boolean,
): void {
    if (wrapper === null) {
        return;
    }
    if (userHasActed || (errorLineReportsOutcome && RevealKeepsFocusInView(wrapper))) {
        wrapper.scrollIntoView(REVEAL_SCROLL_OPTIONS);
    }
}

// Reveals the line in the next animation frame, deciding then from the focus and the actions
// seen so far; the returned cleanup cancels a frame not yet run.
function ScheduleReveal(
    wrapperRef: RefObject<HTMLDivElement | null>,
    userHasActedRef: RefObject<boolean>,
    errorLineReportsOutcome: boolean,
): () => void {
    const frame = window.requestAnimationFrame(() => {
        RevealLine(wrapperRef.current, userHasActedRef.current, errorLineReportsOutcome);
    });
    return () => window.cancelAnimationFrame(frame);
}

// React recognises hooks only by the camelCase "use" prefix, so the hook names below are a
// framework-mandated exception to the MK_Modernization Rule's PascalCase function naming.
//
// Whether the user has pressed a key or a pointer anywhere on the screen since the line mounted.
// An outcome that arrives before then answers no action of the user's: it came with entering the
// screen, as a first page, a fetch the URL's keys started or a refusal carried over.
function useUserHasActed(): RefObject<boolean> {
    const userHasActedRef = useRef(false);
    useEffect(() => {
        const recordUserAction = () => {
            userHasActedRef.current = true;
        };
        for (const eventName of USER_ACTION_EVENTS) {
            document.addEventListener(eventName, recordUserAction, LISTEN_IN_CAPTURE_PHASE);
        }
        return () => {
            for (const eventName of USER_ACTION_EVENTS) {
                document.removeEventListener(eventName, recordUserAction, LISTEN_IN_CAPTURE_PHASE);
            }
        };
    }, []);
    return userHasActedRef;
}

// The outcome shown at mount is only recorded, so entering a screen keeps its top in view and
// StrictMode's repeated mount effect finds nothing new. Each later outcome that shows a line is
// revealed in the next animation frame, after the page's own effects of the same commit have
// moved focus to cursor_field, which may have scrolled the page elsewhere. Until the user acts on
// the screen it is revealed only when ERRMSG reports it (non-blank message text, either new or
// under a new outcomeKey) and the focused field stays wholly in view after the scroll. ERRMSG
// reports an outcome; INFOMSG is the standing prompt, so a change of it alone is not revealed
// before the user acts. Once the user has acted, every new outcome is revealed, INFOMSG included.
function useRevealOnNewOutcome(props: MessageLineProps): RefObject<HTMLDivElement | null> {
    const wrapperRef = useRef<HTMLDivElement | null>(null);
    const previousOutcomeRef = useRef<ShownOutcome | null>(null);
    const userHasActedRef = useUserHasActed();
    const message = ShownText(props.message);
    const infoMessage = ShownText(props.infoMessage);
    const { outcomeKey } = props;
    useEffect(() => {
        const current: ShownOutcome = { message, infoMessage, outcomeKey };
        const previous = previousOutcomeRef.current;
        previousOutcomeRef.current = current;
        if (previous === null || !IsNewOutcome(previous, current) || !ShowsAnyLine(current)) {
            return undefined;
        }
        const errorLineReportsOutcome = ErrorLineReportsNewOutcome(previous, current);
        return ScheduleReveal(wrapperRef, userHasActedRef, errorLineReportsOutcome);
    }, [message, infoMessage, outcomeKey, userHasActedRef]);
    return wrapperRef;
}

/**
 * The id MUI TextField gives a field's helper text, `${fieldId}-helper-text`. MUI renders the
 * helper text only when there is some, so a field without it has no id to reference.
 */
export function HelperTextIdOf(fieldId: string, helperText?: string): string | undefined {
    if (helperText === undefined || helperText === "") {
        return undefined;
    }
    return fieldId + MUI_HELPER_TEXT_ID_SUFFIX;
}

/**
 * The aria-describedby of a field the server names in field_errors: the ERRMSG line first, then
 * the field's helper text when it has one. A field not in error gets nothing, so MUI's own
 * helper-text description stands. Spread it into the TextField's slotProps.htmlInput, whose
 * attributes MUI applies after its own.
 */
export function ErrorMessageReference(
    hasError: boolean,
    helperTextId?: string,
): ErrorMessageReferenceAttributes {
    if (!hasError) {
        return {};
    }
    if (helperTextId === undefined || helperTextId === "") {
        return { "aria-describedby": MESSAGE_LINE_ERROR_ID };
    }
    return { "aria-describedby": `${MESSAGE_LINE_ERROR_ID} ${helperTextId}` };
}

// A blank line renders nothing, as an all-spaces BMS field shows nothing. Each shown line is its
// own live region, so no announcement is nested inside another.
function MessageAlert({ text, severity, region }: MessageAlertProps) {
    if (IsBlank(text)) {
        return null;
    }
    return (
        <Alert
            id={region.id}
            severity={severity}
            role={region.role}
            aria-live={region.politeness}
            data-testid={region.testId}
        >
            {text}
        </Alert>
    );
}

/**
 * The screen's message line: two separate live regions, the ERRMSG Alert (role alert,
 * assertive) and the INFOMSG Alert (role status, polite), inside a wrapper that only spaces them.
 * Each carries its stable id, by which ErrorMessageReference ties fields in error to ERRMSG.
 * The error or confirmation message comes first and the information line after it: the maps
 * place INFOMSG above ERRMSG, but the severity line is the screen's primary feedback. Repeated
 * blanks in a literal stay visible through the theme's MuiAlert message white-space override.
 * The line brings itself into view on each new outcome, as the 3270 showed the whole map after
 * every key: other text on either line, or a new outcomeKey with the same text. The lines a
 * screen mounts with are left where they are, so entering a screen keeps its top in view. Until
 * the user presses a key or a pointer on the screen, a new outcome is brought into view only when
 * the ERRMSG line reports it (a refusal, an error or a notice such as the thank-you line) and the
 * focused field stays wholly in view after the scroll, so the outcomes that come with entering a
 * screen never scroll its cursor out of sight. INFOMSG is the screen's standing prompt: a change
 * of it alone is brought into view only once the user has acted, as CAUP's info-line results of
 * ENTER and F5 are.
 */
export function MessageLine(props: MessageLineProps) {
    const wrapperRef = useRevealOnNewOutcome(props);
    return (
        <Stack ref={wrapperRef} spacing={2} useFlexGap sx={MESSAGE_LINE_STYLE}>
            <MessageAlert
                text={props.message}
                severity={props.severity ?? DEFAULT_SEVERITY}
                region={ERROR_REGION}
            />
            <MessageAlert text={props.infoMessage} severity={INFO_SEVERITY} region={INFO_REGION} />
        </Stack>
    );
}
