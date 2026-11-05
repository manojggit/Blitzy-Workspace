// Replaces: the BMS PF-key legend line (ENTER/F3/F4/F5/F7/F8/F12) of every mapset
"use client";

import { Button, Stack, type ButtonProps } from "@mui/material";
import ChevronLeft from "@mui/icons-material/ChevronLeft";
import ChevronRight from "@mui/icons-material/ChevronRight";
import { useEffect, useLayoutEffect, useRef, type MouseEvent, type RefObject } from "react";
import { WRAPPING_ROW_STYLE } from "@/app/theme";
import type { FunctionKeyDefinition, PfKey } from "@/lib/useFunctionKeys";

export interface FunctionKeyBarProps {
    keys: readonly FunctionKeyDefinition[];
}

type KeyButtonVariant = NonNullable<ButtonProps["variant"]>;

type KeyIcons = Pick<ButtonProps, "startIcon" | "endIcon">;

// The key name each button label ends with, as in "Save (F5)" or "Sign-on (Enter)".
const KEY_DISPLAY_NAMES: Readonly<Record<PfKey, string>> = Object.freeze({
    ENTER: "Enter",
    F3: "F3",
    F4: "F4",
    F5: "F5",
    F7: "F7",
    F8: "F8",
    F12: "F12",
});

// ENTER is the screen's primary action; F3 leaves the screen; the other keys are secondary.
const KEY_BUTTON_VARIANTS: Readonly<Record<PfKey, KeyButtonVariant>> = Object.freeze({
    ENTER: "contained",
    F3: "text",
    F4: "outlined",
    F5: "outlined",
    F7: "outlined",
    F8: "outlined",
    F12: "outlined",
});

/** The visible text, and so the accessible name, of the button for one PF-key definition. */
export function FormatKeyLabel(definition: FunctionKeyDefinition): string {
    return `${definition.label} (${KEY_DISPLAY_NAMES[definition.key]})`;
}

// A key the busy screen has disabled can no longer hold the focus.
const DISABLED_SELECTOR = ":disabled";

// Focus moves are recorded before any handler on the way to the field can stop their propagation.
const LISTEN_IN_CAPTURE_PHASE = true;

/**
 * A pointer press runs the key without taking focus from the field the cursor is in, as a PF key
 * never moved the 3270 cursor; the click still fires. The bar applies it to the gaps between its
 * keys as well. Only a key reached by Tab holds the focus itself, and useCursorKeeper hands the
 * cursor back to its field once the busy screen disables that key.
 */
export function KeepCursorOnPointerPress(event: MouseEvent<HTMLElement>): void {
    event.preventDefault();
}

// Whether an element lies outside the bar and is not the page body, so it can hold the cursor.
function IsOutsideBar(bar: HTMLElement, element: EventTarget | null): element is HTMLElement {
    if (!(element instanceof HTMLElement)) {
        return false;
    }
    return element !== document.body && !bar.contains(element);
}

// Records the element focus moved to when it lies outside the bar: the field the cursor is in.
function RecordCursorHolder(
    holderRef: RefObject<HTMLElement | null>,
    bar: HTMLElement,
    focused: EventTarget | null,
): void {
    if (IsOutsideBar(bar, focused)) {
        holderRef.current = focused;
    }
}

// Whether the focus sits on a key of the bar that has just been disabled.
function IsCursorOnDisabledKey(bar: HTMLElement): boolean {
    const focused = document.activeElement;
    return focused !== null && bar.contains(focused) && focused.matches(DISABLED_SELECTOR);
}

// Puts the cursor back in the field it was in before a key took the focus; with no such field
// still on the screen and enabled, the bar itself keeps the focus rather than the page body.
function RestoreCursor(bar: HTMLElement, holder: HTMLElement | null): void {
    if (holder !== null && holder.isConnected && !holder.matches(DISABLED_SELECTOR)) {
        holder.focus();
    }
    if (bar.contains(document.activeElement)) {
        bar.focus();
    }
}

// Follows the last element focused outside the bar, starting from the one focused at mount.
// React recognises hooks only by the camelCase "use" prefix, so this name is a framework-mandated
// exception to the MK_Modernization Rule's PascalCase function naming.
function useCursorHolder(barRef: RefObject<HTMLDivElement | null>): RefObject<HTMLElement | null> {
    const holderRef = useRef<HTMLElement | null>(null);
    useEffect(() => {
        const bar = barRef.current;
        if (bar === null) {
            return undefined;
        }
        RecordCursorHolder(holderRef, bar, document.activeElement);
        const recordFocus = (event: FocusEvent): void => {
            RecordCursorHolder(holderRef, bar, event.target);
        };
        document.addEventListener("focusin", recordFocus, LISTEN_IN_CAPTURE_PHASE);
        return () => {
            document.removeEventListener("focusin", recordFocus, LISTEN_IN_CAPTURE_PHASE);
        };
    }, [barRef]);
    return holderRef;
}

// A key reached by Tab and pressed with Enter or Space keeps the focus, and the busy render that
// press starts disables it. The layout effect runs in that render's commit, before the browser
// drops the focus of a disabled control to the page body, and moves the cursor back to its field.
// Focus anywhere else, a field or a key left enabled, is never moved. React recognises hooks only
// by the camelCase "use" prefix, so this name is a framework-mandated exception to the
// MK_Modernization Rule's PascalCase function naming.
function useCursorKeeper(keys: readonly FunctionKeyDefinition[]): RefObject<HTMLDivElement | null> {
    const barRef = useRef<HTMLDivElement | null>(null);
    const holderRef = useCursorHolder(barRef);
    useLayoutEffect(() => {
        const bar = barRef.current;
        if (bar !== null && IsCursorOnDisabledKey(bar)) {
            RestoreCursor(bar, holderRef.current);
        }
    }, [keys, holderRef]);
    return barRef;
}

// F7 and F8 page backward and forward, so their chevrons point the way the list moves.
function KeyIconProps(key: PfKey): KeyIcons {
    if (key === "F7") {
        return { startIcon: <ChevronLeft /> };
    }
    if (key === "F8") {
        return { endIcon: <ChevronRight /> };
    }
    return {};
}

// The label is the name as well, because MUI's start-icon wrapper adds a zero-width space that
// would otherwise lead the name of the F7 button. A pointer press leaves the focus in the cursor's
// field. Enter or Space on a key reached by Tab clicks it natively with the focus on it, and the
// bar's useCursorKeeper returns the cursor to its field when that press disables the key.
function KeyButton(props: { definition: FunctionKeyDefinition }) {
    const { definition } = props;
    return (
        <Button
            variant={KEY_BUTTON_VARIANTS[definition.key]}
            onClick={() => definition.onPress()}
            onMouseDown={KeepCursorOnPointerPress}
            disabled={definition.disabled ?? false}
            aria-label={FormatKeyLabel(definition)}
            {...KeyIconProps(definition.key)}
        >
            {FormatKeyLabel(definition)}
        </Button>
    );
}

/**
 * The map's PF-key legend as buttons, in the order the page lists them. The physical keys are
 * bound by useFunctionKeys with the same definitions, so this component adds no key listener. The
 * group takes the focus only from script, never by Tab, when a disabled key leaves the cursor no
 * field to return to.
 */
export function FunctionKeyBar(props: FunctionKeyBarProps) {
    const barRef = useCursorKeeper(props.keys);
    return (
        <Stack
            ref={barRef}
            direction="row"
            spacing={2}
            useFlexGap
            sx={WRAPPING_ROW_STYLE}
            role="group"
            aria-label="Function keys"
            tabIndex={-1}
            onMouseDown={KeepCursorOnPointerPress}
        >
            {props.keys.map((definition) => (
                <KeyButton key={definition.key} definition={definition} />
            ))}
        </Stack>
    );
}
