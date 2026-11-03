// Tests ConfirmField, which replaces the one-byte CONFIRM field of COTRN02, COBIL00 and CORPT00
// (bms/*.bms, cpy-bms/*.CPY).
import { ThemeProvider } from "@mui/material/styles";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef, type ReactElement, type Ref } from "react";

import { CARD_DEMO_THEME } from "@/app/theme";
import { ConfirmField } from "@/components/ConfirmField";
import { MessageLine } from "@/components/MessageLine";

// Each map's ASKIP INITIAL prompt before CONFIRM, verbatim: trailing blanks and " :" are kept.
const COBIL00_PROMPT = "Do you want to pay your balance now. Please confirm: ";
// The COBIL00C literal for an answer other than Y or N, and the id of the ERRMSG Alert showing it.
const INVALID_CONFIRM_MESSAGE = "Invalid value. Valid values are (Y/N)...";
const ERRMSG_ID = "message-line-errmsg";
const CONFIRM_HINT = "Y/N";

const CONFIRM_PROMPTS: readonly { mapName: string; prompt: string }[] = [
    { mapName: "COBIL00", prompt: COBIL00_PROMPT },
    { mapName: "COTRN02", prompt: "You are about to add this transaction. Please confirm :" },
    { mapName: "CORPT00", prompt: "The Report will be submitted for printing. Please confirm: " },
];

// Y and N, a lower-case y, a code the programs reject and a blank: the server judges each one.
const CONFIRM_KEYSTROKES = ["Y", "N", "y", "X", " "] as const;

// The ERRMSG ids aria-describedby names ahead of the Y/N hint's id, and the description they give.
interface ErrorStateCase {
    errorState: string;
    error?: boolean;
    ariaInvalid: string;
    errmsgIds: readonly string[];
    description: string;
}

// Only a field in error is tied to the ERRMSG line; otherwise MUI's hint description stands.
const ERROR_STATE_CASES: readonly ErrorStateCase[] = [
    {
        errorState: "error set",
        error: true,
        ariaInvalid: "true",
        errmsgIds: [ERRMSG_ID],
        description: `${INVALID_CONFIRM_MESSAGE} ${CONFIRM_HINT}`,
    },
    {
        errorState: "error false",
        error: false,
        ariaInvalid: "false",
        errmsgIds: [],
        description: CONFIRM_HINT,
    },
    { errorState: "error omitted", ariaInvalid: "false", errmsgIds: [], description: CONFIRM_HINT },
];

interface DisabledStateCase {
    disabledState: string;
    disabled?: boolean;
    isEnabled: boolean;
}

// The keyboard lock while a page's call runs: set freezes the field, false or omitted keys it.
const DISABLED_STATE_CASES: readonly DisabledStateCase[] = [
    { disabledState: "disabled set", disabled: true, isEnabled: false },
    { disabledState: "disabled false", disabled: false, isEnabled: true },
    { disabledState: "disabled omitted", isEnabled: true },
];

interface ConfirmRenderOptions {
    label?: string;
    onChange?: (value: string) => void;
    error?: boolean;
    disabled?: boolean;
    inputRef?: Ref<HTMLInputElement>;
}

function RenderWithTheme(ui: ReactElement) {
    return render(<ThemeProvider theme={CARD_DEMO_THEME}>{ui}</ThemeProvider>);
}

// The field stays controlled at value="", so each keystroke reaches onChange as the whole value.
function RenderConfirm(options: ConfirmRenderOptions = {}): HTMLInputElement {
    const label = options.label ?? COBIL00_PROMPT;
    RenderWithTheme(
        <ConfirmField
            label={label}
            value=""
            onChange={options.onChange ?? jest.fn()}
            error={options.error}
            disabled={options.disabled}
            inputRef={options.inputRef}
        />,
    );
    // The outlined notch repeats the label in a legend, so the input is found by role and name;
    // the accessible name is the label text trimmed.
    return screen.getByRole<HTMLInputElement>("textbox", { name: label.trim() });
}

describe("ConfirmField field shape", () => {
    it("COBIL00 CONFIRM is a one-byte field with the Y/N hint", () => {
        const input = RenderConfirm();
        expect(input).toHaveAttribute("maxlength", "1");
        expect(input).toHaveAccessibleDescription("Y/N");
        expect(screen.getByText("Y/N")).toBeInTheDocument();
    });

    it.each(CONFIRM_PROMPTS)("$mapName CONFIRM is labelled with the map prompt", ({ prompt }) => {
        const input = RenderConfirm({ label: prompt });
        expect(input.labels?.[0]?.textContent).toBe(prompt);
    });
});

describe("ConfirmField keystrokes", () => {
    it.each(CONFIRM_KEYSTROKES)("COBIL00 CONFIRM forwards %p unchanged", async (keystroke) => {
        const user = userEvent.setup();
        const onChange = jest.fn();
        const input = RenderConfirm({ onChange });
        await user.type(input, keystroke);
        expect(onChange).toHaveBeenCalledTimes(1);
        expect(onChange).toHaveBeenCalledWith(keystroke);
    });
});

describe("ConfirmField keyboard lock", () => {
    it.each(DISABLED_STATE_CASES)(
        "COBIL00 CONFIRM with $disabledState takes keystrokes: $isEnabled",
        async ({ disabled, isEnabled }) => {
            const user = userEvent.setup();
            const onChange = jest.fn();
            const input = RenderConfirm({ onChange, disabled });
            expect(input.disabled).toBe(!isEnabled);
            await user.type(input, "Y");
            expect(onChange).toHaveBeenCalledTimes(isEnabled ? 1 : 0);
        },
    );
});

describe("ConfirmField error state and focus target", () => {
    it.each(ERROR_STATE_CASES)(
        "COBIL00 CONFIRM with $errorState has aria-invalid $ariaInvalid",
        ({ error, ariaInvalid }) => {
            expect(RenderConfirm({ error })).toHaveAttribute("aria-invalid", ariaInvalid);
        },
    );

    // Pages move focus to the server's cursor_field through this ref.
    it("COBIL00 CONFIRM inputRef receives the input element", () => {
        const inputRef = createRef<HTMLInputElement>();
        const input = RenderConfirm({ inputRef });
        expect(inputRef.current).toBe(input);
    });
});

// The page's MessageLine shows the server literal under ERRMSG_ID beside the field.
describe("ConfirmField error description", () => {
    it.each(ERROR_STATE_CASES)(
        "COBIL00 CONFIRM with $errorState is described as '$description'",
        ({ error, errmsgIds, description }) => {
            RenderWithTheme(<MessageLine message={INVALID_CONFIRM_MESSAGE} />);
            const input = RenderConfirm({ error });
            const helperTextId = `${input.id}-helper-text`;
            expect(input.id).not.toBe("");
            expect(document.getElementById(helperTextId)?.textContent).toBe(CONFIRM_HINT);
            expect(input).toHaveAttribute(
                "aria-describedby",
                [...errmsgIds, helperTextId].join(" "),
            );
            expect(input).toHaveAccessibleDescription(description);
        },
    );
});
