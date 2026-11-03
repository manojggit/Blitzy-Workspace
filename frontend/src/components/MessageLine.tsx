// Replaces: BMS ERRMSG/INFOMSG fields; copybooks CSMSG01Y, CSMSG02Y
"use client";

import { Alert, Stack } from "@mui/material";
import type { MessageSeverity } from "@/lib/apiClient";

// BMS ERRMSG is defined COLOR=RED, so a message the page passes without a severity renders red.
const DEFAULT_SEVERITY: MessageSeverity = "error";
const INFO_SEVERITY: MessageSeverity = "info";

export interface MessageLineProps {
    // The server's ERRMSG literal, shown exactly as received.
    message?: string;
    severity?: MessageSeverity;
    // The server's INFOMSG literal (CAUP, CAVW, CCLI, CCDL, CCUP), shown exactly as received.
    infoMessage?: string;
}

/** The ERRMSG line's element id. A screen renders one MessageLine, so the id is unique. */
export const MESSAGE_LINE_ERROR_ID = "message-line-errmsg";
/** The INFOMSG line's element id. */
export const MESSAGE_LINE_INFO_ID = "message-line-infomsg";

// MUI TextField gives its helper text this id suffix and describes the input by that element.
const MUI_HELPER_TEXT_ID_SUFFIX = "-helper-text";

/** The input attribute that ties a field in error to the ERRMSG line. */
export interface ErrorMessageReferenceAttributes {
    "aria-describedby"?: string;
}

// The live-region semantics of one message line, which its Alert carries itself.
interface MessageRegion {
    id: string;
    testId: string;
    role: "alert" | "status";
    politeness: "assertive" | "polite";
}

// ERRMSG reports the outcome of the key just pressed, so it is announced at once.
const ERROR_REGION: MessageRegion = {
    id: MESSAGE_LINE_ERROR_ID,
    testId: "message-line-error",
    role: "alert",
    politeness: "assertive",
};

// INFOMSG is the screen's standing prompt, announced once the reader is idle.
const INFO_REGION: MessageRegion = {
    id: MESSAGE_LINE_INFO_ID,
    testId: "message-line-info",
    role: "status",
    politeness: "polite",
};

interface MessageAlertProps {
    text?: string;
    severity: MessageSeverity;
    region: MessageRegion;
}

// The trim only decides whether a line is shown; the text itself is rendered untouched.
function IsBlank(text?: string): boolean {
    return text === undefined || text.trim() === "";
}

/**
 * The id MUI TextField gives a field's helper text, `${fieldId}-helper-text`. MUI renders the
 * helper text only when there is some, so a field without it has no id to reference.
 */
export function HelperTextIdOf(fieldId: string, helperText?: string): string | undefined {
    if (helperText === undefined || helperText === "") {
        return undefined;
    }
    return fieldId + MUI_HELPER_TEXT_ID_SUFFIX;
}

/**
 * The aria-describedby of a field the server names in field_errors: the ERRMSG line first, then
 * the field's helper text when it has one. A field not in error gets nothing, so MUI's own
 * helper-text description stands. Spread it into the TextField's slotProps.htmlInput, whose
 * attributes MUI applies after its own.
 */
export function ErrorMessageReference(
    hasError: boolean,
    helperTextId?: string,
): ErrorMessageReferenceAttributes {
    if (!hasError) {
        return {};
    }
    if (helperTextId === undefined || helperTextId === "") {
        return { "aria-describedby": MESSAGE_LINE_ERROR_ID };
    }
    return { "aria-describedby": `${MESSAGE_LINE_ERROR_ID} ${helperTextId}` };
}

// A blank line renders nothing, as an all-spaces BMS field shows nothing. Each shown line is its
// own live region, so no announcement is nested inside another.
function MessageAlert({ text, severity, region }: MessageAlertProps) {
    if (IsBlank(text)) {
        return null;
    }
    return (
        <Alert
            id={region.id}
            severity={severity}
            role={region.role}
            aria-live={region.politeness}
            data-testid={region.testId}
        >
            {text}
        </Alert>
    );
}

/**
 * The screen's message line: two separate live regions, the ERRMSG Alert (role alert,
 * assertive) and the INFOMSG Alert (role status, polite), inside a wrapper that only spaces them.
 * Each carries its stable id, by which ErrorMessageReference ties fields in error to ERRMSG.
 * The error or confirmation message comes first and the information line after it: the maps
 * place INFOMSG above ERRMSG, but the severity line is the screen's primary feedback. Repeated
 * blanks in a literal stay visible through the theme's MuiAlert message white-space override.
 */
export function MessageLine(props: MessageLineProps) {
    return (
        <Stack spacing={2} useFlexGap>
            <MessageAlert
                text={props.message}
                severity={props.severity ?? DEFAULT_SEVERITY}
                region={ERROR_REGION}
            />
            <MessageAlert text={props.infoMessage} severity={INFO_SEVERITY} region={INFO_REGION} />
        </Stack>
    );
}
