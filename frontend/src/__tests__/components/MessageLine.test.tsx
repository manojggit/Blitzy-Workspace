// Tests MessageLine, which replaces the BMS ERRMSG line (COLOR=RED, ERRMSGI PIC X(78)) and the
// INFOMSG line (INFOMSGI PIC X(45)) of bms/COACTUP.bms, cpy-bms/COACTUP.CPY and COCRDLI.CPY.
import { ThemeProvider } from "@mui/material/styles";
import { render, screen, within } from "@testing-library/react";
import type { ReactElement } from "react";

import { CARD_DEMO_THEME } from "@/app/theme";
import { MessageLine } from "@/components/MessageLine";
import type { MessageSeverity } from "@/lib/apiClient";

// Legacy literals, verbatim: COACTVWC keeps two blanks after "must", COMEN01C a trailing blank.
const ACCOUNT_FILTER_MESSAGE = "Account Filter must  be a non-zero 11 digit number";
const ADMIN_ONLY_MESSAGE = "No access - Admin Only option... ";
const CHANGES_VALIDATED_INFO = "Changes validated.Press F5 to save";

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
    return textElement.closest<HTMLElement>('[role="alert"]');
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

    it("ERRMSG wrapper is a polite live region", () => {
        RenderWithTheme(<MessageLine message={ACCOUNT_FILTER_MESSAGE} />);
        const status = screen.getByRole("status");
        expect(status).toHaveAttribute("aria-live", "polite");
        expect(status).toContainElement(screen.getByRole("alert"));
    });
});

describe("MessageLine blank ERRMSG and INFOMSG", () => {
    it.each(BLANK_TEXTS)("ERRMSG renders no alert for blank message %p", (message) => {
        RenderWithTheme(<MessageLine message={message} />);
        expect(screen.queryByRole("alert")).toBeNull();
        expect(screen.getByRole("status")).toBeInTheDocument();
    });

    it.each(BLANK_TEXTS)("INFOMSG renders no info alert for blank text %p", (infoMessage) => {
        RenderWithTheme(<MessageLine message={ACCOUNT_FILTER_MESSAGE} infoMessage={infoMessage} />);
        expect(screen.getByRole("alert")).toHaveClass("MuiAlert-colorError");
    });
});

describe("MessageLine INFOMSG line", () => {
    it("INFOMSG renders a second info Alert", () => {
        RenderWithTheme(
            <MessageLine message={ACCOUNT_FILTER_MESSAGE} infoMessage={CHANGES_VALIDATED_INFO} />,
        );
        expect(within(screen.getByRole("status")).getAllByRole("alert")).toHaveLength(2);
        expect(AlertHolding(CHANGES_VALIDATED_INFO)).toHaveClass("MuiAlert-colorInfo");
        expect(AlertHolding(ACCOUNT_FILTER_MESSAGE)).toHaveClass("MuiAlert-colorError");
    });

    it("INFOMSG alone renders one info Alert", () => {
        RenderWithTheme(<MessageLine infoMessage={CHANGES_VALIDATED_INFO} />);
        expect(screen.getAllByRole("alert")).toHaveLength(1);
        const alert = screen.getByRole("alert");
        expect(alert).toHaveClass("MuiAlert-colorInfo");
        expect(alert.textContent).toBe(CHANGES_VALIDATED_INFO);
    });
});
