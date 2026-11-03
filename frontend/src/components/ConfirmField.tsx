// Replaces: the CONFIRM field of COTRN02, COBIL00 and CORPT00
"use client";

import { FormLabel, Stack, TextField } from "@mui/material";
import { useId, type ChangeEvent, type Ref } from "react";

import { ErrorMessageReference, HelperTextIdOf } from "@/components/MessageLine";

// BMS CONFIRM is LENGTH=1, followed on every map by the ASKIP text (Y/N).
const CONFIRM_MAX_LENGTH = 1;
const CONFIRM_HELPER_TEXT = "Y/N";
// Theme spacing units of 4 px between the prompt and the field: 8 px.
const PROMPT_SPACING = 2;

export interface ConfirmFieldProps {
    // The map's confirm prompt, passed verbatim by the page and shown above the field as its label.
    label: string;
    value: string;
    onChange: (value: string) => void;
    // Set while the server names CONFIRM in error: the field reads as invalid and is described by
    // the ERRMSG line ahead of its Y/N hint. Not in error when omitted.
    error?: boolean;
    // The 3270 keyboard locks while the program runs, so a page freezes the field during its call
    // and the answer always belongs to the value that was sent. Enabled when omitted.
    disabled?: boolean;
    // Lets the page focus this input when the server names it as cursor_field.
    inputRef?: Ref<HTMLInputElement>;
}

// The BMS LENGTH and, while the field is in error, its tie to the ERRMSG line ahead of the Y/N
// hint, so the hint stays part of the description. MUI applies these after its own attributes.
function ConfirmInputProps(fieldId: string, hasError: boolean) {
    const helperTextId = HelperTextIdOf(fieldId, CONFIRM_HELPER_TEXT);
    return { maxLength: CONFIRM_MAX_LENGTH, ...ErrorMessageReference(hasError, helperTextId) };
}

interface ConfirmTextFieldProps extends Omit<ConfirmFieldProps, "label"> {
    // The generated id the prompt's label names; MUI derives the Y/N helper text id from it, so
    // the ERRMSG tie can name that hint too.
    fieldId: string;
}

// Free one-character entry: COTRN02C, COBIL00C and CORPT00C decide what blank, N, Y and any other
// character mean, so the keyed value is forwarded without case folding, trimming or filtering.
// It has no label of its own: the prompt above it names it.
function ConfirmTextField(props: ConfirmTextFieldProps) {
    const { fieldId, value, onChange, error = false, disabled, inputRef } = props;
    return (
        <TextField
            id={fieldId}
            value={value}
            onChange={(event: ChangeEvent<HTMLInputElement>) => onChange(event.target.value)}
            error={error}
            disabled={disabled ?? false}
            helperText={CONFIRM_HELPER_TEXT}
            variant="outlined"
            size="small"
            inputRef={inputRef}
            slotProps={{ htmlInput: ConfirmInputProps(fieldId, error) }}
        />
    );
}

// The map's ASKIP prompt literal, then CONFIRM. The prompt is a form label above the field, not
// the outlined field's floating label, which keeps to one line and cuts a long prompt with an
// ellipsis on narrow screens; this one wraps, so the whole BMS text stays readable. Its for=
// names the input, so the prompt remains the input's accessible name.
export function ConfirmField(props: ConfirmFieldProps) {
    const { label, error = false, disabled } = props;
    const fieldId = useId();
    return (
        <Stack spacing={PROMPT_SPACING}>
            <FormLabel htmlFor={fieldId} error={error} disabled={disabled ?? false}>
                {label}
            </FormLabel>
            <ConfirmTextField {...props} fieldId={fieldId} />
        </Stack>
    );
}
