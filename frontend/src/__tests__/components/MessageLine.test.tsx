// Tests MessageLine, which replaces the BMS ERRMSG line (COLOR=RED, ERRMSGI PIC X(78)) and the
// INFOMSG line (INFOMSGI PIC X(45)) of bms/COACTUP.bms, cpy-bms/COACTUP.CPY and COCRDLI.CPY.
import { TextField } from "@mui/material";
import { ThemeProvider } from "@mui/material/styles";
import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";

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
