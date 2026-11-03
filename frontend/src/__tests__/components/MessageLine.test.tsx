// Tests MessageLine, which replaces the BMS ERRMSG line (COLOR=RED, ERRMSGI PIC X(78)) and the
// INFOMSG line (INFOMSGI PIC X(45)) of bms/COACTUP.bms, cpy-bms/COACTUP.CPY and COCRDLI.CPY.
import { TextField } from "@mui/material";
import { ThemeProvider } from "@mui/material/styles";
import { fireEvent, render, screen } from "@testing-library/react";
import { StrictMode, type ReactElement, type ReactNode } from "react";

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
// One theme spacing unit (4 dp) of scroll margin around the line.
const REVEAL_MARGIN = "4px";

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

// A row laid out in the viewport from its top edge; width plays no part in a block reveal.
function RowAt(top: number, height: number): DOMRect {
    return new DOMRect(0, top, 0, height);
}

// Where the focused field and the message line sit, and whether an ERRMSG reveal before any user
// action happens: only when the field stays wholly in view after the scroll the reveal needs.
interface FocusLayout {
    layout: string;
    field: DOMRect;
    line: DOMRect;
    reveals: boolean;
}

// CAUP entered with ?acctId=: Account Number focused at the top, the line far below the fold.
const FIELD_LEFT_ABOVE: FocusLayout = {
    layout: "a field at the top, the line far below the fold",
    field: RowAt(EDGE_GAP, FIELD_HEIGHT),
    line: RowAt(VIEWPORT_HEIGHT + FAR_OUTSIDE_VIEWPORT, LINE_HEIGHT),
    reveals: false,
};

const PRE_ACTION_LAYOUTS: readonly FocusLayout[] = [
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
];

// Each layout with each new outcome line: an ERRMSG is revealed where the layout allows it, an
// INFOMSG alone in no layout before the user acts.
const PRE_ACTION_CASES = PRE_ACTION_LAYOUTS.flatMap((focusLayout) =>
    OUTCOME_LINE_CASES.map((outcomeCase) => ({
        ...focusLayout,
        ...outcomeCase,
        reveals: focusLayout.reveals && outcomeCase.showsErrorLine,
    })),
);

// An action of the user's on the screen, after which every new outcome is brought into view.
interface UserActionCase {
    action: string;
    Act: (field: HTMLElement) => void;
}

const USER_ACTION_CASES: readonly UserActionCase[] = [
    { action: "a keydown", Act: (field) => fireEvent.keyDown(field, { key: "Enter" }) },
    { action: "a pointerdown", Act: (field) => fireEvent.pointerDown(field) },
];

// Each action followed by each new outcome line, INFOMSG alone included.
const ACTED_OUTCOME_CASES = USER_ACTION_CASES.flatMap((actionCase) =>
    OUTCOME_LINE_CASES.map((outcomeCase) => ({ ...actionCase, ...outcomeCase })),
);

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

// The wrapper around both lines, which the providers render as the container's only element.
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

// The message line's wrapper, which follows the field as the container's last element.
function WrapperAfterField(container: HTMLElement): Element {
    const wrapper = container.lastElementChild;
    if (wrapper === null) {
        throw new Error("The message line wrapper is not rendered.");
    }
    return wrapper;
}

// Mounts the field and a blank line, then lays both out as focusLayout places them.
function RenderLaidOutScreen(focusLayout: FocusLayout) {
    const view = render(<FieldBesideLine line={NO_OUTCOME} />, { wrapper: ThemeWrapper });
    const field = screen.getByLabelText(FIELD_LABEL);
    const wrapper = WrapperAfterField(view.container);
    jest.spyOn(field, "getBoundingClientRect").mockReturnValue(focusLayout.field);
    jest.spyOn(wrapper, "getBoundingClientRect").mockReturnValue(focusLayout.line);
    return { field, wrapper, rerender: view.rerender };
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

beforeEach(() => {
    scrollIntoViewSpy = jest.spyOn(Element.prototype, "scrollIntoView");
});

afterEach(() => {
    scrollIntoViewSpy.mockRestore();
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
    it("keeps a scroll margin of one spacing unit, so whole-pixel scrolling clips no edge", () => {
        const { container } = RenderWithTheme(<MessageLine message={ADMIN_ONLY_MESSAGE} />);
        expect(MessageLineWrapper(container)).toHaveStyle({ scrollMarginBlock: REVEAL_MARGIN });
    });
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

describe("MessageLine before the user acts on the screen", () => {
    it.each(PRE_ACTION_CASES)(
        "$layout: brings $outcome into view = $reveals",
        async (preActionCase) => {
            const { field, wrapper, rerender } = RenderLaidOutScreen(preActionCase);
            expect(field).toHaveFocus();
            rerender(<FieldBesideLine line={preActionCase.nextOutcome} />);
            await NextAnimationFrame();
            ExpectRevealed(wrapper, preActionCase.reveals);
        },
    );
});

describe("MessageLine after the user acts on the screen", () => {
    it.each(ACTED_OUTCOME_CASES)(
        "after $action $outcome is brought into view, though the field leaves it",
        async ({ Act, nextOutcome }) => {
            const { field, wrapper, rerender } = RenderLaidOutScreen(FIELD_LEFT_ABOVE);
            Act(field);
            rerender(<FieldBesideLine line={nextOutcome} />);
            await NextAnimationFrame();
            ExpectRevealed(wrapper, true);
        },
    );
});
