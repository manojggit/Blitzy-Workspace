// Tests ConfirmField, which replaces the one-byte CONFIRM field of COTRN02, COBIL00 and CORPT00
// (bms/*.bms, cpy-bms/*.CPY).
import { ThemeProvider } from "@mui/material/styles";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef, type ReactElement, type Ref } from "react";

import { CARD_DEMO_THEME } from "@/app/theme";
import { ConfirmField } from "@/components/ConfirmField";

// Each map's ASKIP INITIAL prompt before CONFIRM, verbatim: trailing blanks and " :" are kept.
const COBIL00_PROMPT = "Do you want to pay your balance now. Please confirm: ";

const CONFIRM_PROMPTS: readonly { mapName: string; prompt: string }[] = [
    { mapName: "COBIL00", prompt: COBIL00_PROMPT },
    { mapName: "COTRN02", prompt: "You are about to add this transaction. Please confirm :" },
    { mapName: "CORPT00", prompt: "The Report will be submitted for printing. Please confirm: " },
];

// Y and N, a lower-case y, a code the programs reject and a blank: the server judges each one.
const CONFIRM_KEYSTROKES = ["Y", "N", "y", "X", " "] as const;

interface ErrorStateCase {
    errorState: string;
    error?: boolean;
    ariaInvalid: string;
}

const ERROR_STATE_CASES: readonly ErrorStateCase[] = [
    { errorState: "error set", error: true, ariaInvalid: "true" },
    { errorState: "error omitted", ariaInvalid: "false" },
];

interface ConfirmRenderOptions {
    label?: string;
    onChange?: (value: string) => void;
    error?: boolean;
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
