// PF-key bindings. Replaces the 3270 attention keys dispatched by EVALUATE EIBAID (DFHENTER,
// DFHPF3/4/5/7/8/12, WHEN OTHER) in the CardDemo online programs; legends from the BMS maps.
// Sources: cbl/COUSR02C.cbl and cbl/COACTUPC.cbl (EIBAID dispatch); cbl/COCRDLIC.cbl
// (PFK-INVALID); cpy/CSMSG01Y.cpy (invalid-key literal); bms/COACTUP.bms, bms/COCRDLI.bms,
// bms/COTRN02.bms and bms/COUSR02.bms (PF-key legends).
import { useEffect, useRef } from "react";

export type PfKey = "ENTER" | "F3" | "F4" | "F5" | "F7" | "F8" | "F12";

// CSMSG01Y CCDA-MSG-INVALID-KEY without its X(50) padding, the same text as INVALID_KEY_MESSAGE
// in backend/app/core/messages.py. No route answers an invalid key, so the screens whose WHEN
// OTHER branch sends it show it themselves.
export const INVALID_KEY_MESSAGE = "Invalid key pressed. Please see below...";

// The 3270 PF1-PF24 keys, as KeyboardEvent.key names them.
const PF_KEY_PATTERN = /^F([1-9]|1[0-9]|2[0-4])$/;

// The KeyboardEvent.key each PF key arrives as; NumpadEnter also reports "Enter".
export const PF_KEY_ACTIONS: Readonly<Record<PfKey, string>> = Object.freeze({
    ENTER: "Enter",
    F3: "F3",
    F4: "F4",
    F5: "F5",
    F7: "F7",
    F8: "F8",
    F12: "F12",
});

// FunctionKeyBar renders one button per definition a page passes here, or per definition of the
// subset its map's legend shows, so a physical key and its on-screen button run the same onPress.
export interface FunctionKeyDefinition {
    key: PfKey;
    label: string;
    onPress: () => void;
    disabled?: boolean;
}

// The screen's EVALUATE EIBAID WHEN OTHER action, which answers a PF key no definition binds.
export type InvalidKeyAction = Pick<FunctionKeyDefinition, "onPress" | "disabled">;

// What the listener reads on each press: the screen's definitions and its invalid-key action.
interface KeyBindings {
    definitions: readonly FunctionKeyDefinition[];
    invalidKeyAction?: InvalidKeyAction;
}

// Elements that act on Enter themselves: a focused button clicks itself, a link follows itself, an
// open Select menu picks its option, and a closed MUI Select (role "combobox" since MUI v6,
// "button" before) opens its menu. The screen's ENTER must not run a second action on top of that.
const ENTER_EXCLUDED_SELECTOR =
    "a[href], button, [role='button'], [role='combobox'], [role='option'], [role='listbox']";

// Marks one of those elements as taking Enter as the screen's ENTER: the hook blocks the element's
// own Enter action and runs ENTER alone. An element that acts on Enter in its own handler, as a
// Select opening its menu does, must skip each IsScreenEnterKey press.
export const SCREEN_ENTER_ATTRIBUTE = "data-screen-enter";

// Spread onto the marked element, or onto the props a component passes to that element.
export const SCREEN_ENTER_PROPS: Readonly<Record<typeof SCREEN_ENTER_ATTRIBUTE, string>> =
    Object.freeze({ [SCREEN_ENTER_ATTRIBUTE]: "true" });

function FindDefinition(
    definitions: readonly FunctionKeyDefinition[],
    eventKey: string,
): FunctionKeyDefinition | undefined {
    return definitions.find((definition) => PF_KEY_ACTIONS[definition.key] === eventKey);
}

// The screen's ENTER, as the invalid-key action of the screens whose PFK-INVALID check does SET
// CCARD-AID-ENTER (COACTVWC, COACTUPC, COCRDLIC, COCRDSLC, COCRDUPC): an unbound PF key runs
// ENTER, ENTER's own disabled state included.
export function EnterKeyOf(
    definitions: readonly FunctionKeyDefinition[],
): FunctionKeyDefinition | undefined {
    return definitions.find((definition) => definition.key === "ENTER");
}

function IsPfKey(eventKey: string): boolean {
    return PF_KEY_PATTERN.test(eventKey);
}

// The nearest element that acts on Enter itself keeps Enter, unless it is marked as the screen's.
function IsEnterExcludedTarget(target: EventTarget | null): boolean {
    if (!(target instanceof Element)) {
        return false;
    }
    const enterOwner = target.closest(ENTER_EXCLUDED_SELECTOR);
    return enterOwner !== null && !enterOwner.hasAttribute(SCREEN_ENTER_ATTRIBUTE);
}

// Modifier chords belong to the browser or the OS, and an IME composition owns its Enter.
function IsChordOrComposing(event: KeyboardEvent): boolean {
    return event.isComposing || event.ctrlKey || event.altKey || event.metaKey;
}

// The Enter press the hook takes as the screen's ENTER: no modifier chord, no IME composition.
export function IsScreenEnterKey(event: KeyboardEvent): boolean {
    return event.key === PF_KEY_ACTIONS.ENTER && !IsChordOrComposing(event);
}

// WHEN OTHER: a PF key the screen does not bind is still an AID, so the browser never acts on it
// (no F5 reload, no F12 devtools) and the screen's invalid-key action answers it once per press.
// Any other unbound key, such as a letter, Tab or Escape, stays with the page and the browser.
function HandleUnboundKey(event: KeyboardEvent, invalidKeyAction?: InvalidKeyAction): void {
    if (!IsPfKey(event.key)) {
        return;
    }
    event.preventDefault();
    if (event.repeat || invalidKeyAction === undefined || invalidKeyAction.disabled) {
        return;
    }
    invalidKeyAction.onPress();
}

function HandleKeyDown(event: KeyboardEvent, bindings: KeyBindings): void {
    if (IsChordOrComposing(event)) {
        return;
    }
    const definition = FindDefinition(bindings.definitions, event.key);
    if (definition === undefined) {
        HandleUnboundKey(event, bindings.invalidKeyAction);
        return;
    }
    if (definition.key === "ENTER" && IsEnterExcludedTarget(event.target)) {
        return;
    }
    // Stops browser F3 search, F5 reload and F12 devtools, and native form submission on Enter.
    event.preventDefault();
    // One physical press is one action. An auto-repeat and a disabled key are still swallowed, so a
    // held or inactive F5 cannot reload the page and lose a screen_token.
    if (event.repeat || definition.disabled) {
        return;
    }
    definition.onPress();
}

// React recognises hooks only by the camelCase "use" prefix, so this name is a framework-mandated
// exception to the MK_Modernization Rule's PascalCase function naming. Without an invalid-key
// action, an unbound PF key is swallowed and answers nothing.
export function useFunctionKeys(
    definitions: readonly FunctionKeyDefinition[],
    invalidKeyAction?: InvalidKeyAction,
): void {
    // Pages pass a new array, and may pass a new action, on every render; the listener reads the
    // latest pair through the ref, written after each commit, instead of being removed and added
    // again.
    const bindingsRef = useRef<KeyBindings>({ definitions, invalidKeyAction });
    useEffect(() => {
        bindingsRef.current = { definitions, invalidKeyAction };
    }, [definitions, invalidKeyAction]);
    useEffect(() => {
        const listener = (event: KeyboardEvent): void => {
            HandleKeyDown(event, bindingsRef.current);
        };
        window.addEventListener("keydown", listener);
        return () => {
            window.removeEventListener("keydown", listener);
        };
    }, []);
}
