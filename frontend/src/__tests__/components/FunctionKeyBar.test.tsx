// Tests FunctionKeyBar, which replaces the PF-key legend line of every BMS map
// (bms/COSGN00.bms, bms/COACTUP.bms, bms/COCRDLI.bms, bms/COTRN00.bms, bms/COBIL00.bms).
import { TextField } from "@mui/material";
import { ThemeProvider } from "@mui/material/styles";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent, { PointerEventsCheckLevel } from "@testing-library/user-event";
import { useState, type ReactElement, type ReactNode } from "react";

import { CARD_DEMO_THEME } from "@/app/theme";
import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import type { FunctionKeyDefinition, PfKey } from "@/lib/useFunctionKeys";

interface LegendCase {
    key: PfKey;
    label: string;
    name: string;
    variantClass: string;
}

interface PagingCase {
    legend: string;
    name: string;
    iconTestId: string;
    wrapperSelector: string;
}

// One entry per PF key a legend can carry, as in "ENTER=Sign-on  F3=Exit" (COSGN00) or
// "F3=Exit F7=Backward  F8=Forward" (COCRDLI); name is the button's accessible name.
const LEGEND_CASES: readonly LegendCase[] = [
    {
        key: "ENTER",
        label: "Sign-on",
        name: "Sign-on (Enter)",
        variantClass: "MuiButton-contained",
    },
    { key: "F3", label: "Exit", name: "Exit (F3)", variantClass: "MuiButton-text" },
    { key: "F4", label: "Clear", name: "Clear (F4)", variantClass: "MuiButton-outlined" },
    { key: "F5", label: "Save", name: "Save (F5)", variantClass: "MuiButton-outlined" },
    { key: "F7", label: "Backward", name: "Backward (F7)", variantClass: "MuiButton-outlined" },
    { key: "F8", label: "Forward", name: "Forward (F8)", variantClass: "MuiButton-outlined" },
    { key: "F12", label: "Cancel", name: "Cancel (F12)", variantClass: "MuiButton-outlined" },
];

const VARIANT_CLASSES: readonly string[] = [
    "MuiButton-contained",
    "MuiButton-outlined",
    "MuiButton-text",
];

// F7/F8 page the COCRDLI, COTRN00 and COUSR00 lists, so their chevrons point the way it moves.
const PAGING_CASES: readonly PagingCase[] = [
    {
        legend: "F7=Backward",
        name: "Backward (F7)",
        iconTestId: "ChevronLeftIcon",
        wrapperSelector: ".MuiButton-startIcon",
    },
    {
        legend: "F8=Forward",
        name: "Forward (F8)",
        iconTestId: "ChevronRightIcon",
        wrapperSelector: ".MuiButton-endIcon",
    },
];

const PLAIN_KEY_CASES: readonly LegendCase[] = LEGEND_CASES.filter(
    (legend) => legend.key !== "F7" && legend.key !== "F8",
);

function RenderWithTheme(ui: ReactElement) {
    return render(<ThemeProvider theme={CARD_DEMO_THEME}>{ui}</ThemeProvider>);
}

function BuildDefinitions(): FunctionKeyDefinition[] {
    return LEGEND_CASES.map(({ key, label }) => ({ key, label, onPress: jest.fn() }));
}

function RenderLegend(definitions: readonly FunctionKeyDefinition[]) {
    return RenderWithTheme(<FunctionKeyBar keys={definitions} />);
}

// How often each definition's onPress ran, in definition order.
function PressCounts(definitions: readonly FunctionKeyDefinition[]): number[] {
    return definitions.map((definition) => jest.mocked(definition.onPress).mock.calls.length);
}

describe("FunctionKeyBar PF legend names, variants and group", () => {
    it.each(LEGEND_CASES)("PF legend renders $name as $variantClass only", (legendCase) => {
        RenderLegend(BuildDefinitions());
        const button = screen.getByRole("button", { name: legendCase.name });
        const variants = VARIANT_CLASSES.filter((variant) => button.classList.contains(variant));
        expect(variants).toEqual([legendCase.variantClass]);
    });

    it("PF legend is a group named Function keys, in definition order", () => {
        RenderLegend(BuildDefinitions());
        const group = screen.getByRole("group", { name: "Function keys" });
        const buttonTexts = within(group)
            .getAllByRole("button")
            .map((button) => button.textContent);
        expect(buttonTexts).toEqual(LEGEND_CASES.map((legendCase) => legendCase.name));
    });
});

// MUI's start-icon wrapper puts a zero-width space ahead of the F7 label, so every button is named
// by its legend label itself rather than by its content.
describe("FunctionKeyBar PF legend accessible names", () => {
    it.each(LEGEND_CASES)("PF legend $name is named by its label alone", (legendCase) => {
        RenderLegend(BuildDefinitions());
        const button = screen.getByText(legendCase.name).closest("button");
        expect(button).toHaveAttribute("aria-label", legendCase.name);
        expect(button).toHaveAccessibleName(legendCase.name);
    });
});

describe("FunctionKeyBar paging icons (F7=Backward, F8=Forward)", () => {
    it.each(PAGING_CASES)("$legend shows $iconTestId in $wrapperSelector", (pagingCase) => {
        RenderLegend(BuildDefinitions());
        const button = screen.getByRole("button", { name: pagingCase.name });
        const icon = within(button).getByTestId(pagingCase.iconTestId);
        expect(button.querySelector(pagingCase.wrapperSelector)).toContainElement(icon);
    });

    it.each(PLAIN_KEY_CASES)("PF legend $name shows no paging chevron", (legendCase) => {
        RenderLegend(BuildDefinitions());
        const button = screen.getByRole("button", { name: legendCase.name });
        expect(within(button).queryByTestId("ChevronLeftIcon")).toBeNull();
        expect(within(button).queryByTestId("ChevronRightIcon")).toBeNull();
    });
});

describe("FunctionKeyBar PF legend clicks", () => {
    it.each(LEGEND_CASES)(
        "PF legend click on $name calls only its onPress once",
        async (legendCase) => {
            const user = userEvent.setup();
            const definitions = BuildDefinitions();
            RenderLegend(definitions);
            const button = screen.getByRole("button", { name: legendCase.name });
            expect(button).toBeEnabled();
            await user.click(button);
            const expectedCounts = definitions.map((definition) =>
                definition.key === legendCase.key ? 1 : 0,
            );
            expect(PressCounts(definitions)).toEqual(expectedCounts);
        },
    );
});

describe("FunctionKeyBar disabled PF key", () => {
    it("F5=Save disabled is passed through and not pressable", async () => {
        // A disabled MUI button sets pointer-events: none, which user-event refuses by default.
        const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
        const onPress = jest.fn();
        RenderLegend([{ key: "F5", label: "Save", onPress, disabled: true }]);
        const button = screen.getByRole("button", { name: "Save (F5)" });
        expect(button).toBeDisabled();
        await user.click(button);
        expect(onPress).not.toHaveBeenCalled();
    });
});

// The unprotected field the cursor rests in while a PF key is pressed with the pointer.
const CURSOR_FIELD_LABEL = "Enter Acct #:";

// The legend beside the field the cursor rests in; returns that field's input.
function RenderLegendBesideField(definitions: readonly FunctionKeyDefinition[]): HTMLElement {
    RenderWithTheme(
        <>
            <TextField label={CURSOR_FIELD_LABEL} />
            <FunctionKeyBar keys={definitions} />
        </>,
    );
    return screen.getByRole("textbox", { name: CURSOR_FIELD_LABEL });
}

// A PF key never moved the 3270 cursor: a pointer press runs the key and leaves the focus where
// it was, so a key the busy screen then disables cannot drop the focus to the page body.
describe("FunctionKeyBar pointer press", () => {
    it.each(LEGEND_CASES)(
        "PF legend pointer press on $name keeps the cursor in its field and runs its onPress",
        async (legendCase) => {
            const user = userEvent.setup();
            const definitions = BuildDefinitions();
            const field = RenderLegendBesideField(definitions);
            await user.click(field);
            const button = screen.getByRole("button", { name: legendCase.name });
            expect(fireEvent.mouseDown(button)).toBe(false);
            await user.click(button);
            expect(field).toHaveFocus();
            const expectedCounts = definitions.map((definition) =>
                definition.key === legendCase.key ? 1 : 0,
            );
            expect(PressCounts(definitions)).toEqual(expectedCounts);
        },
    );
});

// The gaps between the keys belong to the legend group, which a pointer press does not focus.
describe("FunctionKeyBar pointer press between the keys", () => {
    it("PF legend pointer press between the keys keeps the cursor in its field", async () => {
        const user = userEvent.setup();
        const field = RenderLegendBesideField(BuildDefinitions());
        await user.click(field);
        const group = screen.getByRole("group", { name: "Function keys" });
        expect(group).toHaveAttribute("tabindex", "-1");
        expect(fireEvent.mouseDown(group)).toBe(false);
        await user.click(group);
        expect(field).toHaveFocus();
    });
});

// The key a keyboard user tabs to first, and the key the busy screen leaves enabled, as CT00
// leaves F3 enabled while ENTER, F7 and F8 are disabled.
const FIRST_KEY_NAME = "Sign-on (Enter)";
const EXIT_KEY_NAME = "Exit (F3)";

interface KeyboardActivationCase {
    activation: string;
    keys: string;
}

// A focused button clicks itself on Enter and on Space, with the focus left on it.
const KEYBOARD_ACTIVATION_CASES: readonly KeyboardActivationCase[] = [
    { activation: "Enter", keys: "{Enter}" },
    { activation: "Space", keys: " " },
];

// Where the focus rests when a rerender disables keys, and the one key it leaves enabled.
interface KeptFocusCase {
    focused: string;
    enabledKey: PfKey | null;
    FocusedElement: () => HTMLElement;
}

function CursorField(): HTMLElement {
    return screen.getByRole("textbox", { name: CURSOR_FIELD_LABEL });
}

const KEPT_FOCUS_CASES: readonly KeptFocusCase[] = [
    { focused: "its field", enabledKey: null, FocusedElement: CursorField },
    {
        focused: "the enabled F3 key",
        enabledKey: "F3",
        FocusedElement: () => screen.getByRole("button", { name: EXIT_KEY_NAME }),
    },
];

function ThemeWrapper(props: { children: ReactNode }) {
    return <ThemeProvider theme={CARD_DEMO_THEME}>{props.children}</ThemeProvider>;
}

// A busy screen: any key's press disables every key, as a page's busy render disables the keys
// of the request the press starts. withField puts the cursor's field ahead of the legend.
function BusyLegend(props: { withField: boolean }) {
    const [isBusy, setIsBusy] = useState(false);
    const definitions = LEGEND_CASES.map(({ key, label }) => ({
        key,
        label,
        onPress: () => setIsBusy(true),
        disabled: isBusy,
    }));
    return (
        <>
            {props.withField ? <TextField label={CURSOR_FIELD_LABEL} /> : null}
            <FunctionKeyBar keys={definitions} />
        </>
    );
}

function LegendBesideField(props: { definitions: readonly FunctionKeyDefinition[] }) {
    return (
        <>
            <TextField label={CURSOR_FIELD_LABEL} />
            <FunctionKeyBar keys={props.definitions} />
        </>
    );
}

// Every legend key disabled except enabledKey, if one is given.
function DefinitionsEnabling(enabledKey: PfKey | null): FunctionKeyDefinition[] {
    return BuildDefinitions().map((definition) => ({
        ...definition,
        disabled: definition.key !== enabledKey,
    }));
}

// Tabs from where the focus rests to the first key and presses it from the keyboard.
async function PressFirstKeyByKeyboard(keys: string): Promise<HTMLElement> {
    const user = userEvent.setup();
    await user.tab();
    const button = screen.getByRole("button", { name: FIRST_KEY_NAME });
    expect(button).toHaveFocus();
    await user.keyboard(keys);
    expect(button).toBeDisabled();
    return button;
}

// A PF key never moved the 3270 cursor: Enter or Space on a key reached by Tab runs it, and when
// the busy render disables that key the cursor is back in the field it was in, never on the
// disabled key or the page body. With no such field the legend group itself keeps the focus.
describe("FunctionKeyBar keyboard press of a key the press disables", () => {
    it.each(KEYBOARD_ACTIVATION_CASES)(
        "PF legend $activation on a key reached by Tab returns the cursor to its field",
        async ({ keys }) => {
            RenderWithTheme(<BusyLegend withField />);
            act(() => CursorField().focus());
            const button = await PressFirstKeyByKeyboard(keys);
            expect(CursorField()).toHaveFocus();
            expect(button).not.toHaveFocus();
        },
    );

    it("PF legend Enter with no field focused first leaves the focus on the group", async () => {
        RenderWithTheme(<BusyLegend withField={false} />);
        await PressFirstKeyByKeyboard("{Enter}");
        expect(screen.getByRole("group", { name: "Function keys" })).toHaveFocus();
    });
});

// Only a disabled key that holds the focus gives it up: a field or a key left enabled keeps it.
describe("FunctionKeyBar keys disabled by a rerender", () => {
    it.each(KEPT_FOCUS_CASES)(
        "PF legend disabled by a rerender leaves the focus on $focused",
        ({ enabledKey, FocusedElement }) => {
            const { rerender } = render(<LegendBesideField definitions={BuildDefinitions()} />, {
                wrapper: ThemeWrapper,
            });
            act(() => CursorField().focus());
            act(() => FocusedElement().focus());
            rerender(<LegendBesideField definitions={DefinitionsEnabling(enabledKey)} />);
            expect(FocusedElement()).toHaveFocus();
            expect(screen.getByRole("button", { name: FIRST_KEY_NAME })).toBeDisabled();
        },
    );
});
