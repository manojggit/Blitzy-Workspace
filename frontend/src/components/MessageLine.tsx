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

interface MessageAlertProps {
    text?: string;
    severity: MessageSeverity;
    testId: string;
}

// The trim only decides whether a line is shown; the text itself is rendered untouched.
function IsBlank(text?: string): boolean {
    return text === undefined || text.trim() === "";
}

// A blank line renders nothing, as an all-spaces BMS field shows nothing. Alert keeps its own
// role="alert", by which the screen and end-to-end tests find the message.
function MessageAlert(props: MessageAlertProps) {
    if (IsBlank(props.text)) {
        return null;
    }
    return (
        <Alert severity={props.severity} data-testid={props.testId}>
            {props.text}
        </Alert>
    );
}

/**
 * The screen's message line. The live region is always rendered, so text that arrives later is
 * announced. The error or confirmation message comes first and the information line after it:
 * the maps place INFOMSG above ERRMSG, but the severity line is the screen's primary feedback.
 */
export function MessageLine(props: MessageLineProps) {
    return (
        <Stack role="status" aria-live="polite" spacing={2} useFlexGap>
            <MessageAlert
                text={props.message}
                severity={props.severity ?? DEFAULT_SEVERITY}
                testId="message-line-error"
            />
            <MessageAlert
                text={props.infoMessage}
                severity={INFO_SEVERITY}
                testId="message-line-info"
            />
        </Stack>
    );
}
