// Replaces: BMS map COSGN00, program COSGN00C (CC00); copybooks COSGN00 (cpy-bms), COCOM01Y,
// COTTL01Y, CSDAT01Y, CSMSG01Y, CSUSR01Y
"use client";

import { Alert, Paper, Stack, TextField, Typography } from "@mui/material";
import axios from "axios";
import { useRouter } from "next/navigation";
import {
    Suspense,
    useCallback,
    useEffect,
    useMemo,
    useRef,
    useState,
    type Dispatch,
    type RefObject,
    type SetStateAction,
} from "react";

import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import { ErrorMessageReference, HelperTextIdOf, MessageLine } from "@/components/MessageLine";
import { ScreenShell } from "@/components/ScreenShell";
import { Login, Logout } from "@/lib/api/auth";
import {
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    type MessageSeverity,
} from "@/lib/apiClient";
import { useSession } from "@/lib/session";
import { useFunctionKeys, type FunctionKeyDefinition } from "@/lib/useFunctionKeys";

const TRANSACTION_ID = "CC00";
const PROGRAM_NAME = "COSGN00C";
const SCREEN_TITLE = "This is a Credit Card Demo Application for Mainframe Modernization";
const INSTRUCTION_TEXT = "Type your User ID and Password, then press ENTER:";

// BMS LENGTH=8 of both USERID and PASSWD.
const CREDENTIAL_FIELD_LENGTH = 8;
const CREDENTIAL_HELPER_TEXT = "(8 Char)";
const FIELD_ID_PREFIX = "signon-";

const ERROR_SEVERITY = SeverityForOutcome({ isError: true, isCompletedWrite: false });
const INFORMATION_SEVERITY = SeverityForOutcome({ isError: false, isCompletedWrite: false });

type CredentialName = "user_id" | "password";

type Credentials = Readonly<Record<CredentialName, string>>;

type FieldRefs = Readonly<Record<CredentialName, RefObject<HTMLInputElement | null>>>;

interface CredentialFieldDefinition {
    name: CredentialName;
    label: string;
    helperText: string;
    maxLength: number;
    type: "text" | "password";
    autoComplete: string;
}

// The type="password" input replaces the dark ATTRB=(DRK,FSET,UNPROT) PASSWD field.
const FIELD_DEFINITIONS: readonly CredentialFieldDefinition[] = [
    {
        name: "user_id",
        label: "User ID",
        helperText: CREDENTIAL_HELPER_TEXT,
        maxLength: CREDENTIAL_FIELD_LENGTH,
        type: "text",
        autoComplete: "username",
    },
    {
        name: "password",
        label: "Password",
        helperText: CREDENTIAL_HELPER_TEXT,
        maxLength: CREDENTIAL_FIELD_LENGTH,
        type: "password",
        autoComplete: "current-password",
    },
];

const EMPTY_CREDENTIALS: Credentials = { user_id: "", password: "" };

interface SignOnFeedback {
    message: string;
    severity: MessageSeverity;
    infoMessage: string;
    fieldErrors: readonly string[];
    cursorField: string | null;
}

// A fresh screen puts the cursor on USERID: its IC attribute, and MOVE -1 TO USERIDL when
// EIBCALEN = 0.
const FRESH_SCREEN_FEEDBACK: SignOnFeedback = {
    message: "",
    severity: INFORMATION_SEVERITY,
    infoMessage: "",
    fieldErrors: [],
    cursorField: "user_id",
};

// A call in flight clears the message line but leaves the cursor where the user put it.
const PENDING_CALL_FEEDBACK: SignOnFeedback = { ...FRESH_SCREEN_FEEDBACK, cursorField: null };

interface CredentialState {
    credentials: Credentials;
    SetCredential: (name: CredentialName, value: string) => void;
    ClearPassword: () => void;
    ClearCredentials: () => void;
}

interface ScreenState {
    feedback: SignOnFeedback;
    thankYouText: string;
    isBusy: boolean;
    SetFeedback: Dispatch<SetStateAction<SignOnFeedback>>;
    SetThankYouText: Dispatch<SetStateAction<string>>;
    SetBusy: Dispatch<SetStateAction<boolean>>;
}

interface SignOnForm extends CredentialState, ScreenState {
    fieldRefs: FieldRefs;
}

// Everything a PF-key handler acts on, grouped so that each handler takes one parameter.
interface SignOnContext {
    form: SignOnForm;
    RefreshSession: () => Promise<unknown>;
    ClearSession: () => void;
    Navigate: (route: string) => void;
}

interface SignOnFormProps {
    form: SignOnForm;
}

interface CredentialFieldProps {
    definition: CredentialFieldDefinition;
    value: string;
    hasError: boolean;
    inputRef: RefObject<HTMLInputElement | null>;
    onValueChange: (name: CredentialName, value: string) => void;
}

interface ThankYouNoticeProps {
    text: string;
}

function IsCredentialName(fieldName: string | null): fieldName is CredentialName {
    return FIELD_DEFINITIONS.some((definition) => definition.name === fieldName);
}

// The server's literal, field flags and cursor, unchanged. Anything other than an axios error
// is a defect and propagates.
function FeedbackFromError(error: unknown): SignOnFeedback {
    if (!axios.isAxiosError(error)) {
        throw error;
    }
    const screenFeedback = ToScreenFeedback(error);
    return {
        message: screenFeedback.message,
        severity: ERROR_SEVERITY,
        infoMessage: screenFeedback.info_message,
        fieldErrors: screenFeedback.field_errors,
        cursorField: screenFeedback.cursor_field,
    };
}

// ENTER (PROCESS-ENTER-KEY). The values go as typed: the server edits, upper-cases and verifies
// them, and its next_route replaces the XCTL to COADM01C or COMEN01C. A failed sign-on re-sends
// PASSWD empty, as the map did.
async function SubmitSignOn(context: SignOnContext): Promise<void> {
    const { form } = context;
    form.SetThankYouText("");
    form.SetFeedback(PENDING_CALL_FEEDBACK);
    form.SetBusy(true);
    try {
        const response = await Login({ ...form.credentials });
        await context.RefreshSession();
        context.Navigate(response.next_route);
    } catch (error) {
        form.SetFeedback(FeedbackFromError(error));
        form.ClearPassword();
    } finally {
        form.SetBusy(false);
    }
}

// F3 sign-off. The server's CSMSG01Y thank-you literal replaces SEND-PLAIN-TEXT, and the session
// context forgets the signed-off user.
async function SubmitExit(context: SignOnContext): Promise<void> {
    const { form } = context;
    form.SetThankYouText("");
    form.SetFeedback(PENDING_CALL_FEEDBACK);
    form.SetBusy(true);
    try {
        const response = await Logout();
        context.ClearSession();
        form.ClearCredentials();
        form.SetFeedback({ ...FRESH_SCREEN_FEEDBACK });
        form.SetThankYouText(response.message);
    } catch (error) {
        form.SetFeedback(FeedbackFromError(error));
    } finally {
        form.SetBusy(false);
    }
}

// The COSGN00 legend ENTER=Sign-on  F3=Exit; no other key is bound.
function BuildSignOnKeys(context: SignOnContext): FunctionKeyDefinition[] {
    const { isBusy } = context.form;
    return [
        {
            key: "ENTER",
            label: "Sign-on",
            onPress: () => void SubmitSignOn(context),
            disabled: isBusy,
        },
        { key: "F3", label: "Exit", onPress: () => void SubmitExit(context), disabled: isBusy },
    ];
}

// React recognises hooks only by the camelCase "use" prefix, so the hook names below are a
// framework-mandated exception to the MK_Modernization Rule's PascalCase function naming.
function useCredentials(): CredentialState {
    const [credentials, setCredentials] = useState<Credentials>(EMPTY_CREDENTIALS);
    const setCredential = useCallback((name: CredentialName, value: string) => {
        setCredentials((current) => ({ ...current, [name]: value }));
    }, []);
    const clearPassword = useCallback(() => {
        setCredentials((current) => ({ ...current, password: "" }));
    }, []);
    const clearCredentials = useCallback(() => setCredentials(EMPTY_CREDENTIALS), []);
    return useMemo(
        () => ({
            credentials,
            SetCredential: setCredential,
            ClearPassword: clearPassword,
            ClearCredentials: clearCredentials,
        }),
        [credentials, setCredential, clearPassword, clearCredentials],
    );
}

function useScreenState(): ScreenState {
    const [feedback, setFeedback] = useState<SignOnFeedback>(FRESH_SCREEN_FEEDBACK);
    const [thankYouText, setThankYouText] = useState("");
    const [isBusy, setBusy] = useState(false);
    return useMemo(
        () => ({
            feedback,
            thankYouText,
            isBusy,
            SetFeedback: setFeedback,
            SetThankYouText: setThankYouText,
            SetBusy: setBusy,
        }),
        [feedback, thankYouText, isBusy],
    );
}

function useFieldRefs(): FieldRefs {
    const userIdRef = useRef<HTMLInputElement | null>(null);
    const passwordRef = useRef<HTMLInputElement | null>(null);
    return useMemo(() => ({ user_id: userIdRef, password: passwordRef }), []);
}

// Replaces MOVE -1 TO USERIDL / PASSWDL with SEND ... CURSOR. Every new feedback moves the focus,
// even one that names the same field again after the user tabbed or clicked away.
function useCursorFocus(fieldRefs: FieldRefs, feedback: SignOnFeedback): void {
    useEffect(() => {
        const { cursorField } = feedback;
        if (IsCredentialName(cursorField)) {
            fieldRefs[cursorField].current?.focus();
        }
    }, [fieldRefs, feedback]);
}

// A menu's F3 sign-off stores its CSMSG01Y literal before it returns here. The stored text
// carries no severity, and a sign-off is information, so it shows as info.
function usePendingMessage(setFeedback: Dispatch<SetStateAction<SignOnFeedback>>): void {
    useEffect(() => {
        const pendingMessage = TakePendingMessage();
        if (pendingMessage !== "") {
            setFeedback({ ...FRESH_SCREEN_FEEDBACK, message: pendingMessage });
        }
    }, [setFeedback]);
}

function useSignOnForm(): SignOnForm {
    const credentialState = useCredentials();
    const screenState = useScreenState();
    const fieldRefs = useFieldRefs();
    useCursorFocus(fieldRefs, screenState.feedback);
    usePendingMessage(screenState.SetFeedback);
    return useMemo(
        () => ({ ...credentialState, ...screenState, fieldRefs }),
        [credentialState, screenState, fieldRefs],
    );
}

function useSignOnKeys(form: SignOnForm): FunctionKeyDefinition[] {
    const router = useRouter();
    const { RefreshSession, ClearSession } = useSession();
    return useMemo(
        () =>
            BuildSignOnKeys({
                form,
                RefreshSession,
                ClearSession,
                Navigate: (route) => router.push(route),
            }),
        [form, router, RefreshSession, ClearSession],
    );
}

function CredentialFieldId(name: CredentialName): string {
    return FIELD_ID_PREFIX + name;
}

// The BMS LENGTH and, while the server names the credential in error, its tie to the ERRMSG line
// ahead of the field's hint.
function CredentialInputProps({ definition, hasError }: CredentialFieldProps) {
    const helperTextId = HelperTextIdOf(CredentialFieldId(definition.name), definition.helperText);
    return { maxLength: definition.maxLength, ...ErrorMessageReference(hasError, helperTextId) };
}

function CredentialField(props: CredentialFieldProps) {
    const { definition } = props;
    return (
        <TextField
            id={CredentialFieldId(definition.name)}
            variant="outlined"
            label={definition.label}
            helperText={definition.helperText}
            type={definition.type}
            autoComplete={definition.autoComplete}
            value={props.value}
            error={props.hasError}
            inputRef={props.inputRef}
            onChange={(event) => props.onValueChange(definition.name, event.target.value)}
            slotProps={{ htmlInput: CredentialInputProps(props) }}
        />
    );
}

// The fields keep their intrinsic width rather than stretching across the screen.
function CredentialFields({ form }: SignOnFormProps) {
    return (
        <Stack spacing={4} sx={{ alignItems: "flex-start" }}>
            {FIELD_DEFINITIONS.map((definition) => (
                <CredentialField
                    key={definition.name}
                    definition={definition}
                    value={form.credentials[definition.name]}
                    hasError={form.feedback.fieldErrors.includes(definition.name)}
                    inputRef={form.fieldRefs[definition.name]}
                    onValueChange={form.SetCredential}
                />
            ))}
        </Stack>
    );
}

function ThankYouNotice({ text }: ThankYouNoticeProps) {
    if (text === "") {
        return null;
    }
    return (
        <Paper>
            <Alert severity="info">{text}</Alert>
        </Paper>
    );
}

// The thank-you notice, then the ERRMSG line below the form.
function SignOnMessages({ form }: SignOnFormProps) {
    return (
        <>
            <ThankYouNotice text={form.thankYouText} />
            <MessageLine
                message={form.feedback.message}
                severity={form.feedback.severity}
                infoMessage={form.feedback.infoMessage}
            />
        </>
    );
}

function SignOnScreen() {
    const form = useSignOnForm();
    const keys = useSignOnKeys(form);
    useFunctionKeys(keys);
    return (
        <ScreenShell
            transactionId={TRANSACTION_ID}
            programName={PROGRAM_NAME}
            screenTitle={SCREEN_TITLE}
            isBusy={form.isBusy}
        >
            <Typography variant="bodyLarge">{INSTRUCTION_TEXT}</Typography>
            <CredentialFields form={form} />
            <SignOnMessages form={form} />
            <FunctionKeyBar keys={keys} />
        </ScreenShell>
    );
}

export default function SignOnPage() {
    return (
        <Suspense fallback={null}>
            <SignOnScreen />
        </Suspense>
    );
}
