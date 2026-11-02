// Replaces: the CONFIRM field of COTRN02, COBIL00 and CORPT00
"use client";

import { TextField } from "@mui/material";
import type { ChangeEvent, Ref } from "react";

// BMS CONFIRM is LENGTH=1, followed on every map by the ASKIP text (Y/N).
const CONFIRM_MAX_LENGTH = 1;
const CONFIRM_HELPER_TEXT = "Y/N";

export interface ConfirmFieldProps {
    // The map's confirm prompt, passed verbatim by the page.
    label: string;
    value: string;
    onChange: (value: string) => void;
    error?: boolean;
    // Lets the page focus this input when the server names it as cursor_field.
    inputRef?: Ref<HTMLInputElement>;
}

// Free one-character entry: COTRN02C, COBIL00C and CORPT00C decide what blank, N, Y and any other
// character mean, so the keyed value is forwarded without case folding, trimming or filtering.
export function ConfirmField({ label, value, onChange, error, inputRef }: ConfirmFieldProps) {
    return (
        <TextField
            label={label}
            value={value}
            onChange={(event: ChangeEvent<HTMLInputElement>) => onChange(event.target.value)}
            error={error ?? false}
            helperText={CONFIRM_HELPER_TEXT}
            variant="outlined"
            size="small"
            inputRef={inputRef}
            slotProps={{ htmlInput: { maxLength: CONFIRM_MAX_LENGTH } }}
        />
    );
}
