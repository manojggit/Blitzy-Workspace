// PF-key bindings. Replaces the 3270 attention keys dispatched by EVALUATE EIBAID (DFHENTER,
// DFHPF3/4/5/7/8/12) in the CardDemo online programs; legends from the BMS maps.
// Sources: cbl/COUSR02C.cbl and cbl/COACTUPC.cbl (EIBAID dispatch); bms/COACTUP.bms,
// bms/COCRDLI.bms, bms/COTRN02.bms and bms/COUSR02.bms (PF-key legends).
import { useEffect, useRef } from "react";

export type PfKey = "ENTER" | "F3" | "F4" | "F5" | "F7" | "F8" | "F12";

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

// FunctionKeyBar renders one button per definition from the same array a page passes here, so
// the physical keys and the on-screen buttons always run the same onPress.
export interface FunctionKeyDefinition {
    key: PfKey;
    label: string;
    onPress: () => void;
    disabled?: boolean;
}

// Elements that act on Enter themselves: a focused button clicks itself, an open Select menu picks
// its option, and a closed MUI Select (role "combobox" since MUI v6, "button" before) opens its
// menu. The screen's ENTER must not run a second action on top of that.
const ENTER_EXCLUDED_SELECTOR =
    "button, [role='button'], [role='combobox'], [role='option'], [role='listbox']";

function FindDefinition(
    definitions: readonly FunctionKeyDefinition[],
    eventKey: string,
): FunctionKeyDefinition | undefined {
    return definitions.find((definition) => PF_KEY_ACTIONS[definition.key] === eventKey);
}

function IsEnterExcludedTarget(target: EventTarget | null): boolean {
    return target instanceof Element && target.closest(ENTER_EXCLUDED_SELECTOR) !== null;
}

// Modifier chords belong to the browser or the OS, and an IME composition owns its Enter.
function IsChordOrComposing(event: KeyboardEvent): boolean {
    return event.isComposing || event.ctrlKey || event.altKey || event.metaKey;
}

function HandleKeyDown(event: KeyboardEvent, definitions: readonly FunctionKeyDefinition[]): void {
    if (IsChordOrComposing(event)) {
        return;
    }
    const definition = FindDefinition(definitions, event.key);
    if (definition === undefined) {
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
// exception to the MK_Modernization Rule's PascalCase function naming.
export function useFunctionKeys(definitions: readonly FunctionKeyDefinition[]): void {
    // Pages pass a new array on every render; the listener reads the latest one through the ref
    // instead of being removed and added again.
    const definitionsRef = useRef(definitions);
    useEffect(() => {
        definitionsRef.current = definitions;
    }, [definitions]);
    useEffect(() => {
        const listener = (event: KeyboardEvent): void => {
            HandleKeyDown(event, definitionsRef.current);
        };
        window.addEventListener("keydown", listener);
        return () => {
            window.removeEventListener("keydown", listener);
        };
    }, []);
}
