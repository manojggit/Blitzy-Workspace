// Replaces: BMS ERRMSG/INFOMSG fields; copybooks CSMSG01Y, CSMSG02Y
"use client";

import { Alert, Stack, formControlClasses, inputLabelClasses } from "@mui/material";
import { useEffect, useRef, type RefObject } from "react";
import { MESSAGE_LINE_STYLE } from "@/app/theme";
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
// The document event of focus moving to another element; unlike focus, it bubbles.
const FOCUS_MOVE_EVENT: keyof DocumentEventMap = "focusin";

// The scroll margin the browser adds above and below the line when it scrolls the line into view.
// The line's style sets only the top one, so a line stuck flush to the viewport bottom needs no
// reveal scroll.
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

// The rows a box or a control takes on screen, as offsets from the viewport top.
interface BlockExtent {
    top: number;
    bottom: number;
}

// Whether the extent is still wholly inside the viewport once the window scrolls by scrollBy.
function StaysInView(extent: BlockExtent, scrollBy: number, viewportHeight: number): boolean {
    return extent.top - scrollBy >= 0 && extent.bottom - scrollBy <= viewportHeight;
}

// The element holding focus; none while focus rests on the document body.
function FocusedElement(): Element | null {
    const focused = document.activeElement;
    if (focused === null || focused === document.body) {
        return null;
    }
    return focused;
}

// Whether the engine lays the box out; an element that is not rendered reports an all-zero box.
function IsRendered(box: DOMRect): boolean {
    return box.width > 0 || box.height > 0;
}

// How far the focus ring an element draws reaches outside its border box: the outline's width
// and offset together. No outline, or a ring drawn inset within the box, reaches nowhere.
function OutlineReachOf(element: Element): number {
    const style = window.getComputedStyle(element);
    if (style.outlineStyle === "none") {
        return 0;
    }
    return Math.max(0, PixelsOf(style.outlineWidth) + PixelsOf(style.outlineOffset));
}

// The parts of the focused control: the focused element; the form control root around it, which
// pads the input and draws a filled field's underline at its bottom edge; and that root's label,
// which MUI raises above the root's top edge on an outlined field.
function ControlPartsOf(focused: Element): Element[] {
    const root = focused.closest(`.${formControlClasses.root}`);
    if (root === null) {
        return [focused];
    }
    const label = root.querySelector(`.${inputLabelClasses.root}`);
    return label === null ? [focused, root] : [focused, root, label];
}

function ExtentAround(extent: BlockExtent | null, box: BlockExtent): BlockExtent {
    if (extent === null) {
        return box;
    }
    return { top: Math.min(extent.top, box.top), bottom: Math.max(extent.bottom, box.bottom) };
}

// The rows the focused control takes on screen, with the focus ring the focused element draws;
// null while no part of the control is rendered.
function ControlExtentOf(focused: Element): BlockExtent | null {
    let extent: BlockExtent | null = null;
    for (const part of ControlPartsOf(focused)) {
        const box = part.getBoundingClientRect();
        if (IsRendered(box)) {
            const reach = part === focused ? OutlineReachOf(focused) : 0;
            extent = ExtentAround(extent, { top: box.top - reach, bottom: box.bottom + reach });
        }
    }
    return extent;
}

// Whether revealing the line keeps the focused control wholly in view. With nothing focused, or
// no part of the focused control rendered, there is no cursor to lose sight of.
function RevealKeepsFocusInView(wrapper: HTMLElement): boolean {
    const focused = FocusedElement();
    const control = focused === null ? null : ControlExtentOf(focused);
    if (control === null) {
        return true;
    }
    const viewportHeight = window.innerHeight;
    const line = wrapper.getBoundingClientRect();
    const scrollBy = NearestRevealScroll(line, ScrollMarginOf(wrapper), viewportHeight);
    return StaysInView(control, scrollBy, viewportHeight);
}

// Whether the stuck line can pass over the focused element. A sticky element never leaves its
// parent, so only content of the line's own block can pass beneath it; the line itself cannot,
// nor can menus and dialogs, which portals render outside that block and stack above the line.
function LineCanCover(wrapper: HTMLElement, focused: Element): boolean {
    const lineBlock = wrapper.parentElement;
    return lineBlock !== null && lineBlock.contains(focused) && !wrapper.contains(focused);
}

// Whether the line lies over the control's rows, or will once the control scrolls into view: it
// reaches below the control's top, or it is stuck to the viewport bottom while a control that
// comes before it waits below the fold, from where scrolling brings it up beneath the line.
function LineReachesControl(wrapper: HTMLElement, focused: Element, control: BlockExtent): boolean {
    const line = wrapper.getBoundingClientRect();
    if (control.top < line.bottom) {
        return true;
    }
    const isStuck = Math.ceil(line.bottom) >= window.innerHeight;
    const focusedPosition = wrapper.compareDocumentPosition(focused);
    return isStuck && (focusedPosition & Node.DOCUMENT_POSITION_PRECEDING) !== 0;
}

// The rows of the viewport the focused control must fit in, and the gap it keeps from their
// edges. Where the line can lie over the control, the room ends at the line's top; otherwise it
// is the whole viewport. A shown line's scroll margin is the gap; with no line there is none.
interface FocusRoom {
    lowerEdge: number;
    margin: number;
}

function FocusRoomOf(
    wrapper: HTMLElement | null,
    focused: Element,
    control: BlockExtent,
): FocusRoom {
    const viewportHeight = window.innerHeight;
    if (wrapper === null) {
        return { lowerEdge: viewportHeight, margin: 0 };
    }
    const margin = ScrollMarginOf(wrapper).top;
    if (!LineCanCover(wrapper, focused) || !LineReachesControl(wrapper, focused, control)) {
        return { lowerEdge: viewportHeight, margin };
    }
    return { lowerEdge: Math.min(wrapper.getBoundingClientRect().top, viewportHeight), margin };
}

// How far the window scrolls down (negative: up) for the control to sit wholly in the room, one
// gap inside its edges. For a control above the viewport it scrolls up until the control's top is
// one gap below the viewport top. For one reaching below the room it scrolls down until the
// control's bottom is one gap above the room's lower edge, but never so far that the control's
// top leaves the viewport, so a control taller than the room keeps its top in sight. A control
// already inside the room needs no scroll.
function FocusScroll(control: BlockExtent, room: FocusRoom): number {
    if (control.top < 0) {
        return Math.floor(control.top - room.margin);
    }
    const overflowBelow = control.bottom + room.margin - room.lowerEdge;
    if (overflowBelow <= 0) {
        return 0;
    }
    return Math.min(Math.ceil(overflowBelow), control.top);
}

// Scrolls the window the least that brings the focused control, with its label, root and focus
// ring, wholly into the viewport and clear of the stuck line, so neither hides the cursor. A line
// not rendered, as both its texts are blank, covers nothing, and the viewport alone bounds it.
function KeepFocusInView(wrapper: HTMLElement | null): void {
    const focused = FocusedElement();
    const control = focused === null ? null : ControlExtentOf(focused);
    if (focused === null || control === null) {
        return;
    }
    const scroll = FocusScroll(control, FocusRoomOf(wrapper, focused, control));
    if (scroll !== 0) {
        window.scrollBy({ top: scroll });
    }
}

// A new outcome's line is brought into view only when the reveal leaves the focused control
// wholly in sight, as the whole reveal or none of it, so the cursor placed on cursor_field never
// scrolls away. Before the user acts, only an outcome the ERRMSG line reports is brought into
// view; a change of the INFOMSG prompt alone waits for the user to act. A blank outcome renders
// no line to reveal. The focused control is then kept in view and clear of the stuck line, whose
// height the outcome may have changed, even when it already had focus, so that focusing it again
// scrolled nothing.
function RevealLine(
    wrapper: HTMLElement | null,
    userHasActed: boolean,
    errorLineReportsOutcome: boolean,
): void {
    const mayReveal = userHasActed || errorLineReportsOutcome;
    if (wrapper !== null && mayReveal && RevealKeepsFocusInView(wrapper)) {
        wrapper.scrollIntoView(REVEAL_SCROLL_OPTIONS);
    }
    KeepFocusInView(wrapper);
}

// A task run in the next animation frame; a request while one is pending replaces it. Frame
// handles are positive, so 0 stands for no pending frame, and cancelling it does nothing.
interface NextFrameTask {
    Request: () => void;
    Cancel: () => void;
}

function NextFrameTaskOf(task: () => void): NextFrameTask {
    let pendingFrame = 0;
    const cancel = () => window.cancelAnimationFrame(pendingFrame);
    const request = () => {
        cancel();
        pendingFrame = window.requestAnimationFrame(task);
    };
    return { Request: request, Cancel: cancel };
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

// Each control focus moves to is kept wholly in view, with its label, root and focus ring, and
// clear of the stuck line in the next animation frame, after the browser's own focus scroll,
// which aligns only the focused element's border box and takes no account of the line. This
// holds whether or not a line is shown. Leaving the screen cancels a clearance not yet run.
function useFocusClearance(wrapperRef: RefObject<HTMLDivElement | null>): void {
    useEffect(() => {
        const clearance = NextFrameTaskOf(() => KeepFocusInView(wrapperRef.current));
        document.addEventListener(FOCUS_MOVE_EVENT, clearance.Request, LISTEN_IN_CAPTURE_PHASE);
        return () => {
            document.removeEventListener(
                FOCUS_MOVE_EVENT,
                clearance.Request,
                LISTEN_IN_CAPTURE_PHASE,
            );
            clearance.Cancel();
        };
    }, [wrapperRef]);
}

// The outcome shown at mount is only recorded, so entering a screen keeps its top in view and
// StrictMode's repeated mount effect finds nothing new. Each later outcome is handled in the next
// animation frame, after the page's own effects of the same commit have moved focus to
// cursor_field, which may have scrolled the page elsewhere or, on a field that already had focus,
// nothing at all. Its line is revealed only when the focused control stays wholly in view after
// the scroll. Until the user acts on the screen it is revealed only when ERRMSG reports it
// (non-blank message text, either new or under a new outcomeKey). ERRMSG reports an outcome;
// INFOMSG is the standing prompt, so a change of it alone is not revealed before the user acts.
// Once the user has acted, INFOMSG changes count too. After every new outcome, blank ones
// included, and every move of focus, the focused control is kept wholly in view and clear of
// the stuck line.
function useRevealOnNewOutcome(props: MessageLineProps): RefObject<HTMLDivElement | null> {
    const wrapperRef = useRef<HTMLDivElement | null>(null);
    const previousOutcomeRef = useRef<ShownOutcome | null>(null);
    const userHasActedRef = useUserHasActed();
    useFocusClearance(wrapperRef);
    const message = ShownText(props.message);
    const infoMessage = ShownText(props.infoMessage);
    const { outcomeKey } = props;
    useEffect(() => {
        const current: ShownOutcome = { message, infoMessage, outcomeKey };
        const previous = previousOutcomeRef.current;
        previousOutcomeRef.current = current;
        if (previous === null || !IsNewOutcome(previous, current)) {
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
 * assertive) and the INFOMSG Alert (role status, polite), inside a wrapper that carries no live
 * region of its own. Each carries its stable id, by which ErrorMessageReference ties fields in
 * error to ERRMSG. With both lines blank it renders nothing, so an empty line takes no room in
 * the screen layout. The error or confirmation message comes first and the information line
 * after it: the maps place INFOMSG above ERRMSG, but the severity line is the screen's primary
 * feedback. Repeated blanks in a literal stay visible through the theme's MuiAlert message
 * white-space override.
 *
 * A shown line sticks to the viewport bottom while its place in the layout is below the fold,
 * so the outcome of the last key stays in view as the 3270 showed the whole map after every key,
 * and the cursor on cursor_field stays where the page put it. On each new outcome (other text on
 * either line, or a new outcomeKey with the same text) the line also scrolls itself into view,
 * but only when the focused field stays wholly in view after the scroll; the lines a screen
 * mounts with are left where they are, so entering a screen keeps its top in view. Until the
 * user presses a key or a pointer on the screen, only an outcome the ERRMSG line reports (a
 * refusal, an error or a notice such as the thank-you line) is scrolled into view. INFOMSG is the
 * screen's standing prompt: a change of it alone is scrolled into view only once the user has
 * acted, as CAUP's info-line results of ENTER and F5 are. After every new outcome and every move
 * of focus, the focused control, with its label, its form control root and its focus ring, is
 * scrolled wholly inside the viewport and to just above the stuck line, so neither the viewport
 * edges nor the line hide the cursor. That holds for a cursor field that already had focus, which
 * focusing again does not scroll, and on a screen whose line is blank.
 */
export function MessageLine(props: MessageLineProps) {
    const wrapperRef = useRevealOnNewOutcome(props);
    if (IsBlank(props.message) && IsBlank(props.infoMessage)) {
        return null;
    }
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
