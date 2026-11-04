// Replaces: the BMS PF-key legend line (ENTER/F3/F4/F5/F7/F8/F12) of every mapset
"use client";

import { Button, Stack, type ButtonProps } from "@mui/material";
import ChevronLeft from "@mui/icons-material/ChevronLeft";
import ChevronRight from "@mui/icons-material/ChevronRight";
import type { MouseEvent } from "react";
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

/**
 * A pointer press runs the key without taking focus from the field the cursor is in, as a PF key
 * never moved the 3270 cursor; the click still fires. Without it the pressed control holds focus,
 * and the browser drops it to the page body once the busy screen disables that control.
 */
export function KeepCursorOnPointerPress(event: MouseEvent<HTMLElement>): void {
    event.preventDefault();
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
// would otherwise lead the name of the F7 button.
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
 * bound by useFunctionKeys with the same definitions, so this component adds no key listener.
 */
export function FunctionKeyBar(props: FunctionKeyBarProps) {
    return (
        <Stack
            direction="row"
            spacing={2}
            useFlexGap
            sx={{ flexWrap: "wrap" }}
            role="group"
            aria-label="Function keys"
        >
            {props.keys.map((definition) => (
                <KeyButton key={definition.key} definition={definition} />
            ))}
        </Stack>
    );
}
