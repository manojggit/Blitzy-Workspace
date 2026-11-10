// Tests MessageLine, which replaces the BMS ERRMSG line (COLOR=RED, ERRMSGI PIC X(78)) and the
// INFOMSG line (INFOMSGI PIC X(45)) of bms/COACTUP.bms, cpy-bms/COACTUP.CPY and COCRDLI.CPY.
import { Button, Portal, TextField, formControlClasses, inputLabelClasses } from "@mui/material";
import { ThemeProvider } from "@mui/material/styles";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode, type CSSProperties, type ReactElement, type ReactNode } from "react";

import { CARD_DEMO_THEME } from "@/app/theme";
import {
    ErrorMessageReference,
    HelperTextIdOf,
    MESSAGE_LINE_ERROR_ID,
    MESSAGE_LINE_INFO_ID,
    MessageLine,
    type ErrorMessageReferenceAttributes,
    type MessageLineProps,
} from "@/components/MessageLine";
import type { MessageSeverity } from "@/lib/apiClient";

// Legacy literals, verbatim: COACTVWC keeps two blanks after "must", COMEN01C a trailing blank.
const ACCOUNT_FILTER_MESSAGE = "Account Filter must  be a non-zero 11 digit number";
const ADMIN_ONLY_MESSAGE = "No access - Admin Only option... ";
const CHANGES_VALIDATED_INFO = "Changes validated.Press F5 to save";
// COCRDLIC's standing INFOMSG prompt, shown with every card-list page.
const CARD_LIST_PROMPT_INFO = "TYPE S FOR DETAIL, U TO UPDATE ANY RECORD";
// COUSR01C's duplicate-key literal, which flags USERID.
const USER_ID_EXISTS_MESSAGE = "User ID already exist...";

// The ids pages reference, pinned so a rename breaks every field association loudly.
const ERRMSG_ID = "message-line-errmsg";
const INFOMSG_ID = "message-line-infomsg";

// A COUSR01 field and the id MUI gives its helper text, `${id}-helper-text`.
const FIELD_ID = "user-add-user_id";
const FIELD_LABEL = "User ID:";
const FIELD_HINT = "(8 Char)";
const FIELD_HINT_ID = "user-add-user_id-helper-text";

interface ReferenceCase {
    hasError: boolean;
    helperTextId?: string;
    expected: ErrorMessageReferenceAttributes;
}

// Not in error: nothing, so MUI's own helper description stands. In error: ERRMSG, then the hint.
const REFERENCE_CASES: readonly ReferenceCase[] = [
    { hasError: false, expected: {} },
    { hasError: false, helperTextId: FIELD_HINT_ID, expected: {} },
    { hasError: true, expected: { "aria-describedby": ERRMSG_ID } },
    { hasError: true, helperTextId: "", expected: { "aria-describedby": ERRMSG_ID } },
    {
        hasError: true,
        helperTextId: FIELD_HINT_ID,
        expected: { "aria-describedby": `${ERRMSG_ID} ${FIELD_HINT_ID}` },
    },
];

// MUI renders helper text, and so its id, only when there is some.
const HELPER_ID_CASES: readonly (readonly [string | undefined, string | undefined])[] = [
    [FIELD_HINT, FIELD_HINT_ID],
    [undefined, undefined],
    ["", undefined],
];

// The accessible description of a field beside a shown ERRMSG line, in error and not.
const DESCRIPTION_CASES: readonly (readonly [boolean, string])[] = [
    [true, `${USER_ID_EXISTS_MESSAGE} ${FIELD_HINT}`],
    [false, FIELD_HINT],
];

interface SeverityCase {
    severity: MessageSeverity;
    className: string;
    message: string;
}

// MUI 9 marks the colour as MuiAlert-color<Severity>; it has no MuiAlert-standardError class.
const SEVERITY_CASES: readonly SeverityCase[] = [
    {
        severity: "error",
        className: "MuiAlert-colorError",
        message: ACCOUNT_FILTER_MESSAGE,
    },
    {
        severity: "success",
        className: "MuiAlert-colorSuccess",
        message: "Changes committed to database",
    },
    {
        severity: "info",
        className: "MuiAlert-colorInfo",
        message: "Enter or update id of account to display",
    },
];

// An all-spaces BMS field shows nothing on the map, so each of these renders no Alert.
const BLANK_TEXTS: readonly (string | undefined)[] = [undefined, "", "   "];

interface LiveRegionCase {
    line: string;
    props: MessageLineProps;
    role: "alert" | "status";
    politeness: "assertive" | "polite";
}

// ERRMSG answers the key just pressed; INFOMSG is the standing prompt.
const LIVE_REGION_CASES: readonly LiveRegionCase[] = [
    {
        line: "ERRMSG",
        props: { message: ACCOUNT_FILTER_MESSAGE },
        role: "alert",
        politeness: "assertive",
    },
    {
        line: "INFOMSG",
        props: { infoMessage: CHANGES_VALIDATED_INFO },
        role: "status",
        politeness: "polite",
    },
];

const LIVE_REGION_SELECTOR = '[role="alert"], [role="status"], [aria-live]';

// Keeps every blank of a literal visible on screen while long lines still wrap at blanks.
const PRESERVED_WHITE_SPACE = "pre-wrap";

// The least scroll that shows the whole line, and none while it is already in view.
const REVEAL_SCROLL_OPTIONS: ScrollIntoViewOptions = { block: "nearest" };
// One theme spacing unit (4 dp) of scroll margin on the line's top edge, and none on its bottom.
const REVEAL_MARGIN = "4px";
const REVEAL_MARGIN_PIXELS = Number.parseFloat(REVEAL_MARGIN);

// Two responses of one screen, as CAUP passes each response's feedback object as outcomeKey.
const FIRST_OUTCOME = { response: 1 };
const SECOND_OUTCOME = { response: 2 };

interface WrapperProps {
    children: ReactNode;
}

// How a screen mounts the line: plainly, or under StrictMode, which repeats the mount effects.
interface MountCase {
    mode: string;
    Wrapper: (props: WrapperProps) => ReactElement;
}

// One outcome replacing another on the same screen, and whether the line scrolls into view with
// nothing focused: before the user has acted on the screen, and after a keydown.
interface RevealCase {
    caseName: string;
    before: MessageLineProps;
    after: MessageLineProps;
    revealsBeforeAction: boolean;
    revealsAfterAction: boolean;
}

// The line is cleared to each blank text, which shows nothing and so is not brought into view.
const CLEARED_CASES: readonly RevealCase[] = BLANK_TEXTS.map((text) => ({
    caseName: `ERRMSG cleared to ${JSON.stringify(text)}`,
    before: { message: ADMIN_ONLY_MESSAGE },
    after: { message: text },
    revealsBeforeAction: false,
    revealsAfterAction: false,
}));

// ERRMSG reports an outcome, so it is revealed before the user acts; INFOMSG, the standing
// prompt, waits for the user's first action.
const REVEAL_CASES: readonly RevealCase[] = [
    {
        caseName: "a first ERRMSG",
        before: {},
        after: { message: ADMIN_ONLY_MESSAGE },
        revealsBeforeAction: true,
        revealsAfterAction: true,
    },
    {
        caseName: "another ERRMSG",
        before: { message: ACCOUNT_FILTER_MESSAGE },
        after: { message: ADMIN_ONLY_MESSAGE },
        revealsBeforeAction: true,
        revealsAfterAction: true,
    },
    {
        caseName: "the same ERRMSG under a new outcomeKey",
        before: { message: ADMIN_ONLY_MESSAGE, outcomeKey: FIRST_OUTCOME },
        after: { message: ADMIN_ONLY_MESSAGE, outcomeKey: SECOND_OUTCOME },
        revealsBeforeAction: true,
        revealsAfterAction: true,
    },
    {
        caseName: "a first INFOMSG alone",
        before: {},
        after: { infoMessage: CARD_LIST_PROMPT_INFO },
        revealsBeforeAction: false,
        revealsAfterAction: true,
    },
    {
        caseName: "a new INFOMSG beside the same ERRMSG",
        before: { message: ACCOUNT_FILTER_MESSAGE },
        after: { message: ACCOUNT_FILTER_MESSAGE, infoMessage: CHANGES_VALIDATED_INFO },
        revealsBeforeAction: false,
        revealsAfterAction: true,
    },
    {
        caseName: "the same INFOMSG under a new outcomeKey",
        before: { infoMessage: CHANGES_VALIDATED_INFO, outcomeKey: FIRST_OUTCOME },
        after: { infoMessage: CHANGES_VALIDATED_INFO, outcomeKey: SECOND_OUTCOME },
        revealsBeforeAction: false,
        revealsAfterAction: true,
    },
    {
        caseName: "the same ERRMSG under the same outcomeKey",
        before: { message: ADMIN_ONLY_MESSAGE, outcomeKey: FIRST_OUTCOME },
        after: { message: ADMIN_ONLY_MESSAGE, outcomeKey: FIRST_OUTCOME },
        revealsBeforeAction: false,
        revealsAfterAction: false,
    },
    {
        caseName: "a new outcomeKey with both lines blank",
        before: { outcomeKey: FIRST_OUTCOME },
        after: { message: "", outcomeKey: SECOND_OUTCOME },
        revealsBeforeAction: false,
        revealsAfterAction: false,
    },
    ...CLEARED_CASES,
];

// The outcome a screen mounts with, and the ones that arrive after it: an ERRMSG refusal, and
// the standing INFOMSG prompt alone.
const NO_OUTCOME: MessageLineProps = {};
const ADMIN_ONLY_OUTCOME: MessageLineProps = { message: ADMIN_ONLY_MESSAGE };
const PROMPT_ONLY_OUTCOME: MessageLineProps = { infoMessage: CARD_LIST_PROMPT_INFO };

// A new outcome by the line that shows it.
interface OutcomeLineCase {
    outcome: string;
    nextOutcome: MessageLineProps;
    showsErrorLine: boolean;
}

const OUTCOME_LINE_CASES: readonly OutcomeLineCase[] = [
    { outcome: "an ERRMSG", nextOutcome: ADMIN_ONLY_OUTCOME, showsErrorLine: true },
    { outcome: "an INFOMSG alone", nextOutcome: PROMPT_ONLY_OUTCOME, showsErrorLine: false },
];

// The jsdom viewport, against which the laid-out rows below are placed.
const VIEWPORT_HEIGHT = window.innerHeight;
// An outlined field's height, a one-line Alert's height, and offsets in CSS pixels.
const FIELD_HEIGHT = 56;
const LINE_HEIGHT = 48;
const EDGE_GAP = 16;
const FIELD_ABOVE_FOLD = 200;
const FAR_OUTSIDE_VIEWPORT = 600;
// The top of a one-line Alert stuck to the viewport bottom.
const STUCK_LINE_TOP = VIEWPORT_HEIGHT - LINE_HEIGHT;

// A row laid out in the viewport from its top edge; width plays no part in a block reveal.
function RowAt(top: number, height: number): DOMRect {
    return new DOMRect(0, top, 0, height);
}

// Where the focused field and the message line sit.
interface RowLayout {
    layout: string;
    field: DOMRect;
    line: DOMRect;
}

// A layout and whether a reveal happens in it: only when the field stays wholly in view after
// the scroll the reveal needs. In no layout does the line cover the field, so the reveal frame
// scrolls the window by nothing but the scrollIntoView it asks for.
interface FocusLayout extends RowLayout {
    reveals: boolean;
}

// CAUP entered with ?acctId=: Account Number focused at the top, the line far below the fold.
const FIELD_LEFT_ABOVE: FocusLayout = {
    layout: "a field at the top, the line far below the fold",
    field: RowAt(EDGE_GAP, FIELD_HEIGHT),
    line: RowAt(VIEWPORT_HEIGHT + FAR_OUTSIDE_VIEWPORT, LINE_HEIGHT),
    reveals: false,
};

// CAVW after ENTER: Account Number focused at the top, the line stuck to the viewport bottom,
// as it is while its place in the layout is below the fold.
const LINE_STUCK_BELOW_FIELD: FocusLayout = {
    layout: "a field at the top, the line stuck to the viewport bottom",
    field: RowAt(EDGE_GAP, FIELD_HEIGHT),
    line: RowAt(STUCK_LINE_TOP, LINE_HEIGHT),
    reveals: true,
};

// CAUP after a refusal: the cursor field ends one scroll margin above the line stuck flush to the
// viewport bottom, where the clearance leaves it. A stuck line needs no reveal scroll, so the line
// is revealed even when the field reaches up to the viewport top; a bottom scroll margin would
// scroll that field's top out of view, and the page further on every outcome.
const CLEARED_FIELD_BOTTOM = STUCK_LINE_TOP - REVEAL_MARGIN_PIXELS;
const FIELDS_CLEARED_ABOVE_STUCK_LINE: readonly FocusLayout[] = [
    {
        layout: "a field one scroll margin above the stuck line",
        field: RowAt(CLEARED_FIELD_BOTTOM - FIELD_HEIGHT, FIELD_HEIGHT),
        line: RowAt(STUCK_LINE_TOP, LINE_HEIGHT),
        reveals: true,
    },
    {
        layout: "a field from the viewport top to one scroll margin above the stuck line",
        field: RowAt(0, CLEARED_FIELD_BOTTOM),
        line: RowAt(STUCK_LINE_TOP, LINE_HEIGHT),
        reveals: true,
    },
];

const FOCUS_LAYOUTS: readonly FocusLayout[] = [
    FIELD_LEFT_ABOVE,
    {
        layout: "a field near the bottom, the line far above the top",
        field: RowAt(VIEWPORT_HEIGHT - FIELD_HEIGHT - EDGE_GAP, FIELD_HEIGHT),
        line: RowAt(-FAR_OUTSIDE_VIEWPORT, LINE_HEIGHT),
        reveals: false,
    },
    // The menu after a 403: the OPTION field just above a line that crosses the fold.
    {
        layout: "a field just above the line crossing the fold",
        field: RowAt(VIEWPORT_HEIGHT - FIELD_ABOVE_FOLD, FIELD_HEIGHT),
        line: RowAt(VIEWPORT_HEIGHT - LINE_HEIGHT / 2, LINE_HEIGHT),
        reveals: true,
    },
    LINE_STUCK_BELOW_FIELD,
    ...FIELDS_CLEARED_ABOVE_STUCK_LINE,
];

// Each layout with each new outcome line: an ERRMSG is revealed where the layout allows it, an
// INFOMSG alone in no layout before the user acts.
const PRE_ACTION_CASES = FOCUS_LAYOUTS.flatMap((focusLayout) =>
    OUTCOME_LINE_CASES.map((outcomeCase) => ({
        ...focusLayout,
        ...outcomeCase,
        reveals: focusLayout.reveals && outcomeCase.showsErrorLine,
    })),
);

// An action of the user's on the screen, after which an INFOMSG change is brought into view too.
interface UserActionCase {
    action: string;
    Act: (field: HTMLElement) => void;
}

const USER_ACTION_CASES: readonly UserActionCase[] = [
    { action: "a keydown", Act: (field) => fireEvent.keyDown(field, { key: "Enter" }) },
    { action: "a pointerdown", Act: (field) => fireEvent.pointerDown(field) },
];

// Each action followed by each new outcome line, INFOMSG alone included, in each layout: the line
// is revealed exactly where the layout keeps the focused field in view.
const ACTED_OUTCOME_CASES = USER_ACTION_CASES.flatMap((actionCase) =>
    OUTCOME_LINE_CASES.flatMap((outcomeCase) =>
        FOCUS_LAYOUTS.map((focusLayout) => ({ ...actionCase, ...outcomeCase, ...focusLayout })),
    ),
);

// A field the stuck line covers half of, as a browser's focus scroll, which takes no account of
// the line, can leave one.
const FIELD_HALF_UNDER_LINE: RowLayout = {
    layout: "a field half under the stuck line",
    field: RowAt(STUCK_LINE_TOP - FIELD_HEIGHT / 2, FIELD_HEIGHT),
    line: RowAt(STUCK_LINE_TOP, LINE_HEIGHT),
};

// A field that comes before the stuck line but waits below the fold: scrolling it into view
// brings it up beneath the line, which stays stuck while its own place is still below the fold.
const FIELD_BELOW_STUCK_LINE: RowLayout = {
    layout: "a field below the viewport that comes before the stuck line",
    field: RowAt(VIEWPORT_HEIGHT + EDGE_GAP, FIELD_HEIGHT),
    line: RowAt(STUCK_LINE_TOP, LINE_HEIGHT),
};

// Layouts whose focused field the clearance moves to one scroll margin above the stuck line.
const FIELDS_CLEARED_TO_LINE: readonly RowLayout[] = [
    FIELD_HALF_UNDER_LINE,
    FIELD_BELOW_STUCK_LINE,
];

const FIELD_TALLER_THAN_ROOM: RowLayout = {
    layout: "a field taller than the room above the stuck line",
    field: RowAt(EDGE_GAP, VIEWPORT_HEIGHT),
    line: RowAt(STUCK_LINE_TOP, LINE_HEIGHT),
};

// The line in its own place in the layout, not stuck, with a function key laid out below it.
const LINE_IN_PLACE_TOP = VIEWPORT_HEIGHT - FIELD_ABOVE_FOLD;

// Layouts in which the stuck line covers no part of the focused element.
const UNCOVERED_LAYOUTS: readonly RowLayout[] = [
    LINE_STUCK_BELOW_FIELD,
    {
        layout: "a field one gap above the stuck line",
        field: RowAt(STUCK_LINE_TOP - EDGE_GAP - FIELD_HEIGHT, FIELD_HEIGHT),
        line: RowAt(STUCK_LINE_TOP, LINE_HEIGHT),
    },
    {
        layout: "a key below the line in its place",
        field: RowAt(LINE_IN_PLACE_TOP + LINE_HEIGHT + EDGE_GAP, FIELD_HEIGHT),
        line: RowAt(LINE_IN_PLACE_TOP, LINE_HEIGHT),
    },
];

// A cursor field that already has focus, left above the viewport when the user scrolled down to
// press a key, so focusing it again for the next outcome scrolls nothing.
const FIELD_ABOVE_VIEWPORT_TOP = -368;
const FIELD_ABOVE_VIEWPORT = RowAt(FIELD_ABOVE_VIEWPORT_TOP, FIELD_HEIGHT);

// A resubmission from the focused field: the outcome that answers it, and the gap the field
// keeps below the viewport top, the line's scroll margin while a line is shown.
interface ResubmissionCase {
    caseName: string;
    before: MessageLineProps;
    after: MessageLineProps;
    margin: number;
}

const RESUBMISSION_CASES: readonly ResubmissionCase[] = [
    {
        caseName: "the same ERRMSG under a new outcomeKey",
        before: { message: ADMIN_ONLY_MESSAGE, outcomeKey: FIRST_OUTCOME },
        after: { message: ADMIN_ONLY_MESSAGE, outcomeKey: SECOND_OUTCOME },
        margin: REVEAL_MARGIN_PIXELS,
    },
    {
        caseName: "another ERRMSG",
        before: { message: ACCOUNT_FILTER_MESSAGE, outcomeKey: FIRST_OUTCOME },
        after: { message: ADMIN_ONLY_MESSAGE, outcomeKey: SECOND_OUTCOME },
        margin: REVEAL_MARGIN_PIXELS,
    },
    {
        caseName: "a new outcomeKey with both lines blank",
        before: { outcomeKey: FIRST_OUTCOME },
        after: { message: "", outcomeKey: SECOND_OUTCOME },
        margin: 0,
    },
];

// A field whose form control root and outlined label reach beyond its input: MUI pads the input
// inside the root, draws a filled field's underline at the root's bottom edge and raises the
// outlined label above the root's top edge.
interface ControlPartsLayout extends RowLayout {
    root: DOMRect;
    label: DOMRect;
    expectedScroll: number;
}

const INPUT_INSET = 16;
const INPUT_HEIGHT = 24;
const RAISED_LABEL_OFFSET = 9;
const LABEL_HEIGHT = 18;

// The input, root and label rows of a field whose root starts at rootTop.
function ControlPartsAt(rootTop: number): Pick<ControlPartsLayout, "field" | "root" | "label"> {
    return {
        field: RowAt(rootTop + INPUT_INSET, INPUT_HEIGHT),
        root: RowAt(rootTop, FIELD_HEIGHT),
        label: RowAt(rootTop - RAISED_LABEL_OFFSET, LABEL_HEIGHT),
    };
}

// The root ends under the stuck line though the input ends above it; the label reaches above the
// viewport top though the input and the root start inside it.
const ROOT_UNDER_LINE_TOP = STUCK_LINE_TOP - FIELD_HEIGHT + EDGE_GAP / 2;
const ROOT_BELOW_TOP = EDGE_GAP / 8;
const CONTROL_PARTS_LAYOUTS: readonly ControlPartsLayout[] = [
    {
        layout: "the root's bottom edge under the stuck line",
        ...ControlPartsAt(ROOT_UNDER_LINE_TOP),
        line: RowAt(STUCK_LINE_TOP, LINE_HEIGHT),
        expectedScroll: ROOT_UNDER_LINE_TOP + FIELD_HEIGHT + REVEAL_MARGIN_PIXELS - STUCK_LINE_TOP,
    },
    {
        layout: "the raised label above the viewport top",
        ...ControlPartsAt(ROOT_BELOW_TOP),
        line: RowAt(STUCK_LINE_TOP, LINE_HEIGHT),
        expectedScroll: Math.floor(ROOT_BELOW_TOP - RAISED_LABEL_OFFSET - REVEAL_MARGIN_PIXELS),
    },
];

// A function key after the message line, as FunctionKeyBar places the PF legend, with the focus
// ring the theme draws on buttons declared as the longhands jsdom computes.
const KEY_LABEL = "Forward (F8)";
const KEY_HEIGHT = 40;
const RING_WIDTH = "3px";
const RING_OFFSET = "2px";
const RING_REACH = Number.parseFloat(RING_WIDTH) + Number.parseFloat(RING_OFFSET);
const OUTSET_RING: CSSProperties = {
    outlineStyle: "solid",
    outlineWidth: RING_WIDTH,
    outlineOffset: RING_OFFSET,
};
// A ring drawn inside the key's box, as the theme draws one on list items and segments.
const INSET_RING: CSSProperties = {
    outlineStyle: "solid",
    outlineWidth: RING_WIDTH,
    outlineOffset: `-${RING_WIDTH}`,
};
const NO_RING: CSSProperties = { outlineStyle: "none" };

// A focused key, its ring, the line beside it, and the window scroll that keeps it wholly in
// view: none where nothing of it lies outside the viewport.
interface KeyCase {
    caseName: string;
    line: MessageLineProps;
    lineBox?: DOMRect;
    ringStyle: CSSProperties;
    key: DOMRect;
    expectedScroll: number;
}

const KEY_FLUSH_WITH_BOTTOM = RowAt(VIEWPORT_HEIGHT - KEY_HEIGHT, KEY_HEIGHT);
const KEY_BELOW_VIEWPORT = RowAt(VIEWPORT_HEIGHT + EDGE_GAP, KEY_HEIGHT);
const KEY_JUST_BELOW_TOP = EDGE_GAP / 8;

const KEY_CASES: readonly KeyCase[] = [
    {
        caseName: "no line, a key flush with the viewport bottom whose ring reaches past it",
        line: NO_OUTCOME,
        ringStyle: OUTSET_RING,
        key: KEY_FLUSH_WITH_BOTTOM,
        expectedScroll: RING_REACH,
    },
    {
        caseName: "no line, a key just below the viewport top whose ring reaches above it",
        line: NO_OUTCOME,
        ringStyle: OUTSET_RING,
        key: RowAt(KEY_JUST_BELOW_TOP, KEY_HEIGHT),
        expectedScroll: Math.floor(KEY_JUST_BELOW_TOP - RING_REACH),
    },
    {
        caseName: "no line, a key flush with the viewport bottom whose ring is inset",
        line: NO_OUTCOME,
        ringStyle: INSET_RING,
        key: KEY_FLUSH_WITH_BOTTOM,
        expectedScroll: 0,
    },
    {
        caseName: "no line, a ring-less key wholly inside the viewport",
        line: NO_OUTCOME,
        ringStyle: NO_RING,
        key: RowAt(VIEWPORT_HEIGHT - KEY_HEIGHT - EDGE_GAP, KEY_HEIGHT),
        expectedScroll: 0,
    },
    {
        caseName: "no line, a ring-less key below the viewport",
        line: NO_OUTCOME,
        ringStyle: NO_RING,
        key: KEY_BELOW_VIEWPORT,
        expectedScroll: KEY_BELOW_VIEWPORT.bottom - VIEWPORT_HEIGHT,
    },
    {
        caseName: "a key below the viewport after the stuck line, which no longer covers it",
        line: ADMIN_ONLY_OUTCOME,
        lineBox: RowAt(STUCK_LINE_TOP, LINE_HEIGHT),
        ringStyle: NO_RING,
        key: KEY_BELOW_VIEWPORT,
        expectedScroll: KEY_BELOW_VIEWPORT.bottom + REVEAL_MARGIN_PIXELS - VIEWPORT_HEIGHT,
    },
];

// A field in a portal, as a Select menu is, which stacks above the line it overlaps.
const PORTAL_FIELD_ID = "selection-menu-field";
const PORTAL_FIELD_LABEL = "Selection:";

// The style that keeps a shown line in view: stuck to the viewport bottom, above the fields'
// outlined labels, on the screen frame's paper colour.
const STUCK_LINE_STYLE = {
    position: "sticky",
    bottom: "0px",
    zIndex: String(CARD_DEMO_THEME.zIndex.mobileStepper),
    backgroundColor: CARD_DEMO_THEME.palette.background.paper,
};

function RenderWithTheme(ui: ReactElement) {
    return render(<ThemeProvider theme={CARD_DEMO_THEME}>{ui}</ThemeProvider>);
}

// The identity normalizer: the default one trims and collapses the blanks the literals keep.
function KeepRawText(text: string): string {
    return text;
}

// The Alert root holding the literal, found by its exact raw text.
function AlertHolding(literal: string): HTMLElement | null {
    const textElement = screen.getByText(literal, { normalizer: KeepRawText });
    return textElement.closest<HTMLElement>('[role="alert"], [role="status"]');
}

// A live region above the element would nest its announcements inside another region's.
function LiveRegionAncestorOf(element: HTMLElement): Element | null {
    return element.parentElement?.closest(LIVE_REGION_SELECTOR) ?? null;
}

// A page's field wired as the screens wire theirs, beside the screen's message line.
function RenderFieldBesideMessage(hasError: boolean) {
    const helperTextId = HelperTextIdOf(FIELD_ID, FIELD_HINT);
    return RenderWithTheme(
        <>
            <TextField
                id={FIELD_ID}
                label={FIELD_LABEL}
                helperText={FIELD_HINT}
                error={hasError}
                slotProps={{ htmlInput: ErrorMessageReference(hasError, helperTextId) }}
            />
            <MessageLine message={USER_ID_EXISTS_MESSAGE} />
        </>,
    );
}

function ThemeWrapper({ children }: WrapperProps) {
    return <ThemeProvider theme={CARD_DEMO_THEME}>{children}</ThemeProvider>;
}

function StrictThemeWrapper({ children }: WrapperProps) {
    return (
        <StrictMode>
            <ThemeWrapper>{children}</ThemeWrapper>
        </StrictMode>
    );
}

const MOUNT_CASES: readonly MountCase[] = [
    { mode: "a plain mount", Wrapper: ThemeWrapper },
    { mode: "a StrictMode mount", Wrapper: StrictThemeWrapper },
];

// Resolves in a frame requested after the line's own, which jsdom runs in request order.
function NextAnimationFrame(): Promise<void> {
    return new Promise((resolve) => {
        window.requestAnimationFrame(() => resolve());
    });
}

// The wrapper around both lines, which the providers render as the container's only element;
// null while both lines are blank, as the line then renders nothing.
function MessageLineWrapper(container: HTMLElement): Element | null {
    return container.firstElementChild;
}

interface FieldBesideLineProps {
    line: MessageLineProps;
}

// A screen's focused cursor field above its message line.
function FieldBesideLine({ line }: FieldBesideLineProps) {
    return (
        <>
            <TextField id={FIELD_ID} label={FIELD_LABEL} autoFocus />
            <MessageLine {...line} />
        </>
    );
}

// The message line's wrapper, which follows the field as the container's last element once a
// line shows; null while both lines are blank.
function ShownWrapperAfter(container: HTMLElement, field: HTMLElement): Element | null {
    const wrapper = container.lastElementChild;
    if (wrapper === null || wrapper.contains(field)) {
        return null;
    }
    return wrapper;
}

function WrapperAfterField(container: HTMLElement, field: HTMLElement): Element {
    const wrapper = ShownWrapperAfter(container, field);
    if (wrapper === null) {
        throw new Error("The message line wrapper is not rendered.");
    }
    return wrapper;
}

// A screen whose focused field is laid out, beside a line that may not be rendered yet.
interface LaidOutScreen {
    field: HTMLElement;
    container: HTMLElement;
    Rerender: (ui: ReactElement) => void;
}

// Mounts the field beside a blank line, which renders nothing, and lays the field out.
function RenderLaidOutScreen(rowLayout: RowLayout): LaidOutScreen {
    const view = render(<FieldBesideLine line={NO_OUTCOME} />, { wrapper: ThemeWrapper });
    const field = screen.getByLabelText(FIELD_LABEL);
    jest.spyOn(field, "getBoundingClientRect").mockReturnValue(rowLayout.field);
    return { field, container: view.container, Rerender: view.rerender };
}

// Shows the next outcome, lays its line out as rowLayout places it before the frame the reveal
// runs in, then waits for that frame.
async function ShowLaidOutOutcome(
    laidOut: LaidOutScreen,
    rowLayout: RowLayout,
    nextOutcome: MessageLineProps,
): Promise<Element> {
    laidOut.Rerender(<FieldBesideLine line={nextOutcome} />);
    const wrapper = WrapperAfterField(laidOut.container, laidOut.field);
    jest.spyOn(wrapper, "getBoundingClientRect").mockReturnValue(rowLayout.line);
    await NextAnimationFrame();
    return wrapper;
}

// A screen whose field and shown line are laid out.
interface ShownLineScreen {
    field: HTMLElement;
    wrapper: Element;
    Unmount: () => void;
}

// Mounts the field beside a shown line and lays both out as rowLayout places them.
function RenderShownLineScreen(rowLayout: RowLayout): ShownLineScreen {
    const view = render(<FieldBesideLine line={ADMIN_ONLY_OUTCOME} />, { wrapper: ThemeWrapper });
    const field = screen.getByLabelText(FIELD_LABEL);
    const wrapper = WrapperAfterField(view.container, field);
    jest.spyOn(field, "getBoundingClientRect").mockReturnValue(rowLayout.field);
    jest.spyOn(wrapper, "getBoundingClientRect").mockReturnValue(rowLayout.line);
    return { field, wrapper, Unmount: view.unmount };
}

// Moves focus off the field and back, as Tab and Shift+Tab do.
function MoveFocusTo(field: HTMLElement): void {
    act(() => field.blur());
    act(() => field.focus());
}

// Focus moving to the laid-out field, then the frame the clearance runs in.
async function FocusLaidOutField(rowLayout: RowLayout): Promise<Element> {
    const { field, wrapper } = RenderShownLineScreen(rowLayout);
    MoveFocusTo(field);
    await NextAnimationFrame();
    return wrapper;
}

// A new ERRMSG outcome beside the laid-out field, whose reveal frame runs the clearance.
function ShowOutcomeBesideField(rowLayout: RowLayout): Promise<Element> {
    return ShowLaidOutOutcome(RenderLaidOutScreen(rowLayout), rowLayout, ADMIN_ONLY_OUTCOME);
}

// What runs the clearance: a move of focus, or a new outcome's reveal.
interface ClearanceTrigger {
    trigger: string;
    Show: (rowLayout: RowLayout) => Promise<Element>;
}

const CLEARANCE_TRIGGERS: readonly ClearanceTrigger[] = [
    { trigger: "focus moves to the field", Show: FocusLaidOutField },
    { trigger: "a new outcome is revealed", Show: ShowOutcomeBesideField },
];

const UNCOVERED_CASES = CLEARANCE_TRIGGERS.flatMap((triggerCase) =>
    UNCOVERED_LAYOUTS.map((rowLayout) => ({ ...triggerCase, ...rowLayout })),
);

const CLEARED_TO_LINE_CASES = CLEARANCE_TRIGGERS.flatMap((triggerCase) =>
    FIELDS_CLEARED_TO_LINE.map((rowLayout) => ({ ...triggerCase, ...rowLayout })),
);

// Answers a resubmission from the focused field laid out above the viewport, with the answer's
// line, when one shows, stuck to the viewport bottom; then waits for the frame the outcome runs.
async function ResubmitFromFieldAboveViewport(
    resubmission: ResubmissionCase,
): Promise<HTMLElement> {
    const view = render(<FieldBesideLine line={resubmission.before} />, { wrapper: ThemeWrapper });
    const field = screen.getByLabelText(FIELD_LABEL);
    jest.spyOn(field, "getBoundingClientRect").mockReturnValue(FIELD_ABOVE_VIEWPORT);
    view.rerender(<FieldBesideLine line={resubmission.after} />);
    const wrapper = ShownWrapperAfter(view.container, field);
    if (wrapper !== null) {
        jest.spyOn(wrapper, "getBoundingClientRect").mockReturnValue(
            RowAt(STUCK_LINE_TOP, LINE_HEIGHT),
        );
    }
    await NextAnimationFrame();
    return field;
}

// Focus moving, as Tab moves it, to a field whose input, root and label are laid out as
// partsLayout places them beside the stuck line, then the frame the clearance runs in.
async function TabToFieldWithParts(partsLayout: ControlPartsLayout): Promise<void> {
    const { field } = RenderShownLineScreen(partsLayout);
    const root = field.closest(`.${formControlClasses.root}`);
    const label = root === null ? null : root.querySelector(`.${inputLabelClasses.root}`);
    if (root === null || label === null) {
        throw new Error("The field renders no form control root or label.");
    }
    jest.spyOn(root, "getBoundingClientRect").mockReturnValue(partsLayout.root);
    jest.spyOn(label, "getBoundingClientRect").mockReturnValue(partsLayout.label);
    MoveFocusTo(field);
    await NextAnimationFrame();
}

interface KeyBesideLineProps {
    line: MessageLineProps;
    ringStyle: CSSProperties;
}

// A function key after the screen's message line, as each page places FunctionKeyBar.
function KeyBesideLine({ line, ringStyle }: KeyBesideLineProps) {
    return (
        <>
            <MessageLine {...line} />
            <Button style={ringStyle}>{KEY_LABEL}</Button>
        </>
    );
}

// Focus moving to the key laid out as keyCase places it, beside its line when one is shown,
// then the frame the clearance runs in.
async function FocusKeyBesideLine(keyCase: KeyCase): Promise<void> {
    const view = render(<KeyBesideLine line={keyCase.line} ringStyle={keyCase.ringStyle} />, {
        wrapper: ThemeWrapper,
    });
    const key = screen.getByRole("button", { name: KEY_LABEL });
    jest.spyOn(key, "getBoundingClientRect").mockReturnValue(keyCase.key);
    const wrapper = view.container.firstElementChild;
    if (keyCase.lineBox !== undefined && wrapper !== null && wrapper !== key) {
        jest.spyOn(wrapper, "getBoundingClientRect").mockReturnValue(keyCase.lineBox);
    }
    act(() => key.focus());
    await NextAnimationFrame();
}

// The window scrolls the clearance makes: one by a non-zero distance, or none.
function WindowScrollCalls(scroll: number): ScrollToOptions[][] {
    return scroll === 0 ? [] : [[{ top: scroll }]];
}

// The scroll margin of the line's top edge as the engine computes it, 0 where it is unset.
function LineScrollMarginTop(wrapper: Element): number {
    const marginTop = Number.parseFloat(window.getComputedStyle(wrapper).scrollMarginTop);
    return Number.isNaN(marginTop) ? 0 : marginTop;
}

// The computed values that could give the line's bottom edge a scroll margin: the longhand, and
// the shorthands a browser expands into it, which jsdom keeps as declared. Each reads "" unset.
function BottomScrollMarginsOf(wrapper: Element | null): readonly string[] {
    if (wrapper === null) {
        throw new Error("The message line wrapper is not rendered.");
    }
    const style = window.getComputedStyle(wrapper);
    return [style.scrollMarginBottom, style.scrollMarginBlock, style.scrollMargin];
}

const UNDECLARED_BOTTOM_SCROLL_MARGINS: readonly string[] = ["", "", ""];

// A field a portal renders outside the screen's block, beside the screen's shown line.
function PortalFieldBesideLine() {
    return (
        <>
            <MessageLine {...ADMIN_ONLY_OUTCOME} />
            <Portal>
                <TextField id={PORTAL_FIELD_ID} label={PORTAL_FIELD_LABEL} />
            </Portal>
        </>
    );
}

// Mounts the line with the case's first outcome, lets the user press a key first when asked,
// then shows the case's next outcome and waits for the frame the reveal runs in.
async function ShowNextOutcome(
    revealCase: RevealCase,
    userActsFirst: boolean,
): Promise<Element | null> {
    const { container, rerender } = render(<MessageLine {...revealCase.before} />, {
        wrapper: ThemeWrapper,
    });
    if (userActsFirst) {
        fireEvent.keyDown(container, { key: "Enter" });
    }
    rerender(<MessageLine {...revealCase.after} />);
    await NextAnimationFrame();
    return MessageLineWrapper(container);
}

describe("MessageLine ERRMSG severity", () => {
    it.each(SEVERITY_CASES)(
        "ERRMSG renders $severity as $className",
        ({ severity, className, message }) => {
            RenderWithTheme(<MessageLine message={message} severity={severity} />);
            expect(screen.getByRole("alert")).toHaveClass(className, "MuiAlert-standard");
        },
    );

    it("ERRMSG severity defaults to error (red)", () => {
        RenderWithTheme(<MessageLine message={ADMIN_ONLY_MESSAGE} />);
        expect(screen.getByRole("alert")).toHaveClass("MuiAlert-colorError", "MuiAlert-standard");
    });
});

describe("MessageLine ERRMSG text and live region", () => {
    it.each([ACCOUNT_FILTER_MESSAGE, ADMIN_ONLY_MESSAGE])(
        "ERRMSG keeps the legacy literal %p verbatim",
        (literal) => {
            RenderWithTheme(<MessageLine message={literal} />);
            expect(screen.getByRole("alert").textContent).toBe(literal);
            expect(screen.getByText(literal, { normalizer: KeepRawText })).toBeInTheDocument();
        },
    );

    it.each(LIVE_REGION_CASES)(
        "$line is its own role $role region, aria-live $politeness, inside no other region",
        ({ props, role, politeness }) => {
            RenderWithTheme(<MessageLine {...props} />);
            const region = screen.getByRole(role);
            expect(region).toHaveAttribute("aria-live", politeness);
            expect(LiveRegionAncestorOf(region)).toBeNull();
        },
    );
});

describe("MessageLine visible blanks", () => {
    it.each(LIVE_REGION_CASES)(
        "$line shows repeated blanks through the theme's white-space pre-wrap",
        ({ props, role }) => {
            RenderWithTheme(<MessageLine {...props} />);
            const messageSlot = screen.getByRole(role).querySelector(".MuiAlert-message");
            expect(messageSlot).toHaveStyle({ whiteSpace: PRESERVED_WHITE_SPACE });
        },
    );
});

describe("MessageLine blank ERRMSG and INFOMSG", () => {
    it.each(BLANK_TEXTS)("blank ERRMSG and INFOMSG %p render no live region", (text) => {
        const { container } = RenderWithTheme(<MessageLine message={text} infoMessage={text} />);
        expect(screen.queryByRole("alert")).toBeNull();
        expect(screen.queryByRole("status")).toBeNull();
        expect(container.querySelector(LIVE_REGION_SELECTOR)).toBeNull();
    });

    it.each(BLANK_TEXTS)("blank ERRMSG and INFOMSG %p render no wrapper either", (text) => {
        const { container } = RenderWithTheme(<MessageLine message={text} infoMessage={text} />);
        expect(container).toBeEmptyDOMElement();
    });

    it.each(BLANK_TEXTS)("INFOMSG renders no info line for blank text %p", (infoMessage) => {
        RenderWithTheme(<MessageLine message={ACCOUNT_FILTER_MESSAGE} infoMessage={infoMessage} />);
        expect(screen.getByRole("alert")).toHaveClass("MuiAlert-colorError");
        expect(screen.queryByRole("status")).toBeNull();
    });
});

describe("MessageLine INFOMSG line", () => {
    it("INFOMSG renders a second info Alert after ERRMSG, neither inside the other", () => {
        RenderWithTheme(
            <MessageLine message={ACCOUNT_FILTER_MESSAGE} infoMessage={CHANGES_VALIDATED_INFO} />,
        );
        const errorLine = screen.getByRole("alert");
        const infoLine = screen.getByRole("status");
        expect(AlertHolding(ACCOUNT_FILTER_MESSAGE)).toBe(errorLine);
        expect(AlertHolding(CHANGES_VALIDATED_INFO)).toBe(infoLine);
        expect(infoLine).toHaveClass("MuiAlert-colorInfo");
        expect(errorLine.compareDocumentPosition(infoLine)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    });

    it("INFOMSG alone renders one info Alert", () => {
        RenderWithTheme(<MessageLine infoMessage={CHANGES_VALIDATED_INFO} />);
        expect(screen.queryByRole("alert")).toBeNull();
        const infoLine = screen.getByRole("status");
        expect(infoLine).toHaveClass("MuiAlert-colorInfo");
        expect(infoLine.textContent).toBe(CHANGES_VALIDATED_INFO);
    });
});

describe("MessageLine stable message ids", () => {
    it("ERRMSG and INFOMSG carry the exported ids and keep their test ids", () => {
        RenderWithTheme(
            <MessageLine message={ACCOUNT_FILTER_MESSAGE} infoMessage={CHANGES_VALIDATED_INFO} />,
        );
        expect([MESSAGE_LINE_ERROR_ID, MESSAGE_LINE_INFO_ID]).toEqual([ERRMSG_ID, INFOMSG_ID]);
        expect(screen.getByRole("alert")).toHaveAttribute("id", ERRMSG_ID);
        expect(screen.getByRole("status")).toHaveAttribute("id", INFOMSG_ID);
        expect(screen.getByTestId("message-line-error")).toBe(screen.getByRole("alert"));
        expect(screen.getByTestId("message-line-info")).toBe(screen.getByRole("status"));
    });
});

describe("MessageLine field error association helpers", () => {
    it.each(REFERENCE_CASES)(
        "ErrorMessageReference(hasError $hasError, helper $helperTextId) is $expected",
        ({ hasError, helperTextId, expected }) => {
            expect(ErrorMessageReference(hasError, helperTextId)).toEqual(expected);
        },
    );

    it.each(HELPER_ID_CASES)("HelperTextIdOf with helper text %p is %p", (helperText, expected) => {
        expect(HelperTextIdOf(FIELD_ID, helperText)).toBe(expected);
    });
});

describe("MessageLine field error description", () => {
    it.each(DESCRIPTION_CASES)("a field with error=%p is described as %p", (hasError, expected) => {
        RenderFieldBesideMessage(hasError);
        expect(screen.getByText(FIELD_HINT)).toHaveAttribute("id", FIELD_HINT_ID);
        expect(screen.getByLabelText(FIELD_LABEL)).toHaveAccessibleDescription(expected);
    });
});

// Element.scrollIntoView, spied afresh for every test; jest.setup.ts stubs it, as jsdom has none.
let scrollIntoViewSpy: jest.SpiedFunction<Element["scrollIntoView"]>;
// window.scrollBy, which the focus clearance calls; jsdom implements none and reports each call.
let scrollBySpy: jest.SpiedFunction<typeof window.scrollBy>;

beforeEach(() => {
    scrollIntoViewSpy = jest.spyOn(Element.prototype, "scrollIntoView");
    scrollBySpy = jest.spyOn(window, "scrollBy").mockImplementation(() => undefined);
});

afterEach(() => {
    scrollIntoViewSpy.mockRestore();
    scrollBySpy.mockRestore();
});

describe("MessageLine on mount", () => {
    it.each(MOUNT_CASES)("the lines of $mode stay where they are", async ({ Wrapper }) => {
        render(<MessageLine message={ADMIN_ONLY_MESSAGE} infoMessage={CHANGES_VALIDATED_INFO} />, {
            wrapper: Wrapper,
        });
        await NextAnimationFrame();
        expect(screen.getByRole("alert")).toBeInTheDocument();
        expect(scrollIntoViewSpy).not.toHaveBeenCalled();
    });
});

describe("MessageLine brings each new outcome into view before the user acts", () => {
    it.each(REVEAL_CASES)(
        "$caseName: brings the line into view = $revealsBeforeAction",
        async (revealCase) => {
            const wrapper = await ShowNextOutcome(revealCase, false);
            ExpectRevealed(wrapper, revealCase.revealsBeforeAction);
        },
    );
});

describe("MessageLine brings each new outcome into view after a keydown", () => {
    it.each(REVEAL_CASES)(
        "$caseName: brings the line into view = $revealsAfterAction",
        async (revealCase) => {
            const wrapper = await ShowNextOutcome(revealCase, true);
            ExpectRevealed(wrapper, revealCase.revealsAfterAction);
        },
    );
});

describe("MessageLine reveal margin", () => {
    it("keeps one spacing unit of scroll margin on the top edge only, none on the bottom", () => {
        const { container } = RenderWithTheme(<MessageLine message={ADMIN_ONLY_MESSAGE} />);
        const wrapper = MessageLineWrapper(container);
        expect(wrapper).toHaveStyle({ scrollMarginTop: REVEAL_MARGIN });
        expect(BottomScrollMarginsOf(wrapper)).toEqual(UNDECLARED_BOTTOM_SCROLL_MARGINS);
    });
});

describe("MessageLine stuck to the viewport bottom", () => {
    it.each(LIVE_REGION_CASES)(
        "a shown $line sticks to the viewport bottom on the paper colour",
        ({ props }) => {
            const { container } = RenderWithTheme(<MessageLine {...props} />);
            expect(MessageLineWrapper(container)).toHaveStyle(STUCK_LINE_STYLE);
        },
    );
});

describe("MessageLine left before the next frame", () => {
    it("a screen left before the next frame cancels the pending scroll", async () => {
        const { rerender, unmount } = render(<MessageLine />, { wrapper: ThemeWrapper });
        rerender(<MessageLine message={ADMIN_ONLY_MESSAGE} />);
        unmount();
        await NextAnimationFrame();
        expect(scrollIntoViewSpy).not.toHaveBeenCalled();
    });
});

// The wrapper's reveal calls: one with the reveal options, or none.
function ExpectRevealed(wrapper: Element | null, reveals: boolean): void {
    const expectedCalls = reveals ? [[REVEAL_SCROLL_OPTIONS]] : [];
    expect(scrollIntoViewSpy.mock.calls).toEqual(expectedCalls);
    const wrapperCalls = scrollIntoViewSpy.mock.contexts.filter((self) => self === wrapper);
    expect(wrapperCalls).toHaveLength(expectedCalls.length);
}

// A laid-out reveal: the wrapper's reveal calls, and no window scroll of the line's own beside
// them, as the line covers the field in no FocusLayout.
function ExpectLaidOutRevealed(wrapper: Element, reveals: boolean): void {
    ExpectRevealed(wrapper, reveals);
    expect(scrollBySpy).not.toHaveBeenCalled();
}

describe("MessageLine before the user acts on the screen", () => {
    it.each(PRE_ACTION_CASES)(
        "$layout: brings $outcome into view = $reveals",
        async (preActionCase) => {
            const laidOut = RenderLaidOutScreen(preActionCase);
            expect(laidOut.field).toHaveFocus();
            const { nextOutcome } = preActionCase;
            const wrapper = await ShowLaidOutOutcome(laidOut, preActionCase, nextOutcome);
            ExpectLaidOutRevealed(wrapper, preActionCase.reveals);
        },
    );
});

describe("MessageLine after the user acts on the screen", () => {
    it.each(ACTED_OUTCOME_CASES)(
        "after $action, $layout: brings $outcome into view = $reveals",
        async (actedCase) => {
            const laidOut = RenderLaidOutScreen(actedCase);
            actedCase.Act(laidOut.field);
            const wrapper = await ShowLaidOutOutcome(laidOut, actedCase, actedCase.nextOutcome);
            ExpectLaidOutRevealed(wrapper, actedCase.reveals);
        },
    );
});

// The clearance scrolls the field up by its overlap with the line, widened by the line's margin.
async function ClearsFieldToLine(clearedCase: ClearanceTrigger & RowLayout): Promise<void> {
    const wrapper = await clearedCase.Show(clearedCase);
    const { field, line } = clearedCase;
    const overlap = field.bottom + LineScrollMarginTop(wrapper) - line.top;
    expect(scrollBySpy.mock.calls).toEqual([[{ top: overlap }]]);
}

async function ClearsTallFieldOnlyToTop({ Show }: ClearanceTrigger): Promise<void> {
    await Show(FIELD_TALLER_THAN_ROOM);
    expect(scrollBySpy.mock.calls).toEqual([[{ top: FIELD_TALLER_THAN_ROOM.field.top }]]);
}

// A Select menu's field, which a portal renders outside the screen's block, over the line.
async function LeavesPortalFieldOverLine(): Promise<void> {
    const { container } = render(<PortalFieldBesideLine />, { wrapper: ThemeWrapper });
    const field = screen.getByLabelText(PORTAL_FIELD_LABEL);
    const wrapper = WrapperAfterField(container, field);
    expect(container).not.toContainElement(field);
    jest.spyOn(field, "getBoundingClientRect").mockReturnValue(FIELD_HALF_UNDER_LINE.field);
    jest.spyOn(wrapper, "getBoundingClientRect").mockReturnValue(FIELD_HALF_UNDER_LINE.line);
    MoveFocusTo(field);
    await NextAnimationFrame();
    expect(scrollBySpy).not.toHaveBeenCalled();
}

describe("MessageLine clears a focused field the stuck line covers", () => {
    it.each(CLEARED_TO_LINE_CASES)(
        "when $trigger, $layout scrolls to just above the line",
        ClearsFieldToLine,
    );
    it.each(CLEARANCE_TRIGGERS)(
        "when $trigger, a field taller than the room above the line scrolls only to the top",
        ClearsTallFieldOnlyToTop,
    );
});

describe("MessageLine leaves a focused field the stuck line does not cover", () => {
    it.each(UNCOVERED_CASES)("when $trigger, $layout: no clearance scroll", async (uncovered) => {
        await uncovered.Show(uncovered);
        expect(scrollBySpy).not.toHaveBeenCalled();
    });
    it(
        "a focused field in a portal, which stacks above the line, gets no clearance",
        LeavesPortalFieldOverLine,
    );
});

describe("MessageLine brings an already focused cursor field back into view", () => {
    it.each(RESUBMISSION_CASES)(
        "$caseName: the field above the viewport scrolls down to one gap below its top",
        async (resubmission) => {
            const field = await ResubmitFromFieldAboveViewport(resubmission);
            const top = Math.floor(FIELD_ABOVE_VIEWPORT_TOP - resubmission.margin);
            expect(scrollBySpy.mock.calls).toEqual([[{ top }]]);
            expect(scrollIntoViewSpy).not.toHaveBeenCalled();
            expect(field).toHaveFocus();
        },
    );
});

describe("MessageLine clears the whole focused control, its root and label included", () => {
    it.each(CONTROL_PARTS_LAYOUTS)(
        "when focus moves to the field, $layout: scrolls by $expectedScroll",
        async (partsLayout) => {
            await TabToFieldWithParts(partsLayout);
            expect(scrollBySpy.mock.calls).toEqual([[{ top: partsLayout.expectedScroll }]]);
        },
    );
});

describe("MessageLine keeps a focused key and its focus ring in view", () => {
    it.each(KEY_CASES)("$caseName: scrolls by $expectedScroll", async (keyCase) => {
        await FocusKeyBesideLine(keyCase);
        expect(scrollBySpy.mock.calls).toEqual(WindowScrollCalls(keyCase.expectedScroll));
    });
});

describe("MessageLine left before the clearance frame", () => {
    it("a screen left before the next frame cancels the pending clearance", async () => {
        const { field, Unmount } = RenderShownLineScreen(FIELD_HALF_UNDER_LINE);
        MoveFocusTo(field);
        Unmount();
        await NextAnimationFrame();
        expect(scrollBySpy).not.toHaveBeenCalled();
    });
});
