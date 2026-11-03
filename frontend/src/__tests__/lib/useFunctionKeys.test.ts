// Tests: lib/useFunctionKeys.ts (replaces the EVALUATE EIBAID PF-key dispatch of
// cbl/COUSR02C.cbl and cbl/COTRN00C.cbl; unbound keys keep the CSMSG01Y invalid-key path).
import { Box, Button, TextField, ToggleButton } from "@mui/material";
import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { createElement, type ReactElement } from "react";

import {
    IsScreenEnterKey,
    PF_KEY_ACTIONS,
    SCREEN_ENTER_PROPS,
    useFunctionKeys,
    type FunctionKeyDefinition,
    type PfKey,
} from "@/lib/useFunctionKeys";

// Each PF key, the KeyboardEvent.key it arrives as, and the 3270 AID it replaces.
const EIBAID_CASES: readonly (readonly [PfKey, string, string])[] = [
    ["ENTER", "Enter", "DFHENTER"],
    ["F3", "F3", "DFHPF3"],
    ["F4", "F4", "DFHPF4"],
    ["F5", "F5", "DFHPF5"],
    ["F7", "F7", "DFHPF7"],
    ["F8", "F8", "DFHPF8"],
    ["F12", "F12", "DFHPF12"],
];

const MODIFIER_CASES: readonly { modifier: string; chord: KeyboardEventInit }[] = [
    { modifier: "Ctrl", chord: { ctrlKey: true } },
    { modifier: "Alt", chord: { altKey: true } },
    { modifier: "Meta", chord: { metaKey: true } },
];

// Roles whose focused element acts on Enter itself (MUI Select trigger, menu and options).
const ENTER_OWNING_ROLES: readonly string[] = ["button", "combobox", "listbox", "option"];

interface FocusedControlCase {
    control: string;
    element: ReactElement;
}

// Native controls that act on Enter themselves: a button clicks itself and a link follows itself.
const NATIVE_ENTER_CONTROLS: readonly FocusedControlCase[] = [
    { control: "button", element: createElement(Button, null, "Process") },
    { control: "link", element: createElement(Button, { href: "/export" }, "Download CSV") },
];

// Controls marked as the screen's ENTER: a report-type segment and a row Select trigger.
const MARKED_ENTER_CONTROLS: readonly FocusedControlCase[] = [
    {
        control: "button",
        element: createElement(
            ToggleButton,
            { value: "monthly", ...SCREEN_ENTER_PROPS },
            "Monthly",
        ),
    },
    {
        control: "combobox",
        element: createElement(Box, { role: "combobox", tabIndex: 0, ...SCREEN_ENTER_PROPS }),
    },
];

// A marked control's Enter runs ENTER once, or nothing while ENTER is inactive; it is blocked
// either way, so the control's own Enter action never runs.
const MARKED_ENTER_CASES = MARKED_ENTER_CONTROLS.flatMap((controlCase) =>
    [false, true].map((disabled) => ({ ...controlCase, disabled, pressCount: disabled ? 0 : 1 })),
);

// Plain Enter is the screen's ENTER; a chord, an IME composition or another key is not.
const SCREEN_ENTER_KEY_CASES: readonly { press: string; init: KeyboardEventInit; is: boolean }[] = [
    { press: "Enter", init: { key: "Enter" }, is: true },
    { press: "Ctrl+Enter", init: { key: "Enter", ctrlKey: true }, is: false },
    { press: "Alt+Enter", init: { key: "Enter", altKey: true }, is: false },
    { press: "Meta+Enter", init: { key: "Enter", metaKey: true }, is: false },
    { press: "a composing Enter", init: { key: "Enter", isComposing: true }, is: false },
    { press: "Space", init: { key: " " }, is: false },
];

const UNBOUND_KEYS: readonly string[] = ["F8", "a"];

function BuildDefinition(key: PfKey, disabled = false): FunctionKeyDefinition {
    return { key, label: key, onPress: jest.fn(), disabled };
}

function DispatchKey(init: KeyboardEventInit, target: EventTarget = window): KeyboardEvent {
    const keyEvent = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
    act(() => {
        target.dispatchEvent(keyEvent);
    });
    return keyEvent;
}

// The hook is passed by reference: rules-of-hooks reads any PascalCase function as a component
// and refuses a hook call inside a callback nested in one.
function BindKeys(definitions: readonly FunctionKeyDefinition[]) {
    return renderHook(useFunctionKeys, { initialProps: definitions });
}

// The MUI Button focus ripple mounts lazily after a microtask, so focus is awaited inside act.
async function RenderFocused(element: ReactElement, role: string): Promise<HTMLElement> {
    render(element);
    const focusedElement = screen.getByRole(role);
    await act(async () => {
        focusedElement.focus();
    });
    return focusedElement;
}

it("PF_KEY_ACTIONS maps exactly the seven PF keys and is frozen", () => {
    const expectedActions = Object.fromEntries(
        EIBAID_CASES.map(([pfKey, eventKey]) => [pfKey, eventKey]),
    );
    expect(PF_KEY_ACTIONS).toStrictEqual(expectedActions);
    expect(Object.isFrozen(PF_KEY_ACTIONS)).toBe(true);
});

it.each(EIBAID_CASES)(
    "%s (key %s) runs its handler once and blocks the browser default (EIBAID %s)",
    (pfKey, eventKey) => {
        const definition = BuildDefinition(pfKey);
        BindKeys([definition]);
        const keyEvent = DispatchKey({ key: eventKey });
        expect(definition.onPress).toHaveBeenCalledTimes(1);
        expect(keyEvent.defaultPrevented).toBe(true);
    },
);

it("only the pressed key's handler runs (one WHEN branch per EIBAID)", () => {
    const enterDefinition = BuildDefinition("ENTER");
    const exitDefinition = BuildDefinition("F3");
    const saveDefinition = BuildDefinition("F5");
    BindKeys([enterDefinition, exitDefinition, saveDefinition]);
    fireEvent.keyDown(window, { key: "F3" });
    expect(exitDefinition.onPress).toHaveBeenCalledTimes(1);
    expect(enterDefinition.onPress).not.toHaveBeenCalled();
    expect(saveDefinition.onPress).not.toHaveBeenCalled();
});

it("an auto-repeated F8 is blocked but not run again (one key press is one AID)", () => {
    const definition = BuildDefinition("F8");
    BindKeys([definition]);
    const keyEvent = DispatchKey({ key: "F8", repeat: true });
    expect(definition.onPress).not.toHaveBeenCalled();
    expect(keyEvent.defaultPrevented).toBe(true);
});

it("Enter that commits an IME composition is left alone (not a 3270 AID)", () => {
    const definition = BuildDefinition("ENTER");
    BindKeys([definition]);
    const keyEvent = DispatchKey({ key: "Enter", isComposing: true });
    expect(definition.onPress).not.toHaveBeenCalled();
    expect(keyEvent.defaultPrevented).toBe(false);
});

it.each(MODIFIER_CASES)(
    "$modifier+F5 is left to the browser (a chord is not a PF key)",
    ({ chord }) => {
        const definition = BuildDefinition("F5");
        BindKeys([definition]);
        const keyEvent = DispatchKey({ key: "F5", ...chord });
        expect(definition.onPress).not.toHaveBeenCalled();
        expect(keyEvent.defaultPrevented).toBe(false);
    },
);

it.each(NATIVE_ENTER_CONTROLS)(
    "Enter on a focused $control is left to the $control (no double ENTER AID)",
    async ({ control, element }) => {
        const definition = BuildDefinition("ENTER");
        BindKeys([definition]);
        const focusedControl = await RenderFocused(element, control);
        const keyEvent = DispatchKey({ key: "Enter" }, focusedControl);
        expect(definition.onPress).not.toHaveBeenCalled();
        expect(keyEvent.defaultPrevented).toBe(false);
    },
);

it.each(MARKED_ENTER_CASES)(
    "Enter on a marked $control runs ENTER $pressCount time(s), disabled $disabled, and is blocked",
    async ({ control, element, disabled, pressCount }) => {
        const definition = BuildDefinition("ENTER", disabled);
        BindKeys([definition]);
        const focusedControl = await RenderFocused(element, control);
        const keyEvent = DispatchKey({ key: "Enter" }, focusedControl);
        expect(definition.onPress).toHaveBeenCalledTimes(pressCount);
        expect(keyEvent.defaultPrevented).toBe(true);
    },
);

it.each(SCREEN_ENTER_KEY_CASES)("IsScreenEnterKey for $press is $is", ({ init, is }) => {
    expect(IsScreenEnterKey(new KeyboardEvent("keydown", init))).toBe(is);
});

it.each(ENTER_OWNING_ROLES)("Enter on a focused role=%s is left to that element", async (role) => {
    const definition = BuildDefinition("ENTER");
    BindKeys([definition]);
    const focusedElement = await RenderFocused(createElement(Box, { role, tabIndex: 0 }), role);
    const keyEvent = DispatchKey({ key: "Enter" }, focusedElement);
    expect(definition.onPress).not.toHaveBeenCalled();
    expect(keyEvent.defaultPrevented).toBe(false);
});

it("F5 on a focused button still runs its handler (only Enter is left to buttons)", async () => {
    const definition = BuildDefinition("F5");
    BindKeys([definition]);
    const focusedButton = await RenderFocused(createElement(Button, null, "Process"), "button");
    const keyEvent = DispatchKey({ key: "F5" }, focusedButton);
    expect(definition.onPress).toHaveBeenCalledTimes(1);
    expect(keyEvent.defaultPrevented).toBe(true);
});

it("Enter in a focused text field runs ENTER instead of submitting a form", async () => {
    const definition = BuildDefinition("ENTER");
    BindKeys([definition]);
    const textField = createElement(TextField, { label: "User ID" });
    const focusedField = await RenderFocused(textField, "textbox");
    const keyEvent = DispatchKey({ key: "Enter" }, focusedField);
    expect(definition.onPress).toHaveBeenCalledTimes(1);
    expect(keyEvent.defaultPrevented).toBe(true);
});

it("disabled F5 is blocked but not run (PF key not active on this map)", () => {
    const definition = BuildDefinition("F5", true);
    BindKeys([definition]);
    const keyEvent = DispatchKey({ key: "F5" });
    expect(definition.onPress).not.toHaveBeenCalled();
    expect(keyEvent.defaultPrevented).toBe(true);
});

it.each(UNBOUND_KEYS)("unbound key %s is left to the page (WHEN OTHER invalid-key path)", (key) => {
    const definition = BuildDefinition("ENTER");
    BindKeys([definition]);
    const keyEvent = DispatchKey({ key });
    expect(definition.onPress).not.toHaveBeenCalled();
    expect(keyEvent.defaultPrevented).toBe(false);
});

it("re-rendered definitions replace the earlier ones (the current screen answers)", () => {
    const firstDefinition = BuildDefinition("F7");
    const nextDefinition = BuildDefinition("F7");
    const { rerender } = BindKeys([firstDefinition]);
    rerender([nextDefinition]);
    fireEvent.keyDown(window, { key: "F7" });
    expect(nextDefinition.onPress).toHaveBeenCalledTimes(1);
    expect(firstDefinition.onPress).not.toHaveBeenCalled();
});

it("unmounting removes the listener (the screen no longer receives AIDs)", () => {
    const definition = BuildDefinition("F3");
    const { unmount } = BindKeys([definition]);
    unmount();
    const keyEvent = DispatchKey({ key: "F3" });
    expect(definition.onPress).not.toHaveBeenCalled();
    expect(keyEvent.defaultPrevented).toBe(false);
});
