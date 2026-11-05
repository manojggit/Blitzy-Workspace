// Replaces: BMS map COSGN00, program COSGN00C (CC00); copybooks COSGN00 (cpy-bms), COCOM01Y,
// COTTL01Y, CSDAT01Y, CSMSG01Y, CSUSR01Y
"use client";

import {
    Alert,
    Paper,
    Stack,
    TextField,
    Typography,
    type SxProps,
    type Theme,
} from "@mui/material";
import axios from "axios";
import { useRouter } from "next/navigation";
import {
    Suspense,
    useCallback,
    useEffect,
    useMemo,
    useRef,
    useState,
    useTransition,
    type Dispatch,
    type RefObject,
    type SetStateAction,
} from "react";

import { START_ALIGNED_COLUMN_STYLE } from "@/app/theme";
import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import { ErrorMessageReference, HelperTextIdOf, MessageLine } from "@/components/MessageLine";
import { ScreenShell, useScreenBusy } from "@/components/ScreenShell";
import { Login, Logout } from "@/lib/api/auth";
import {
    ABEND_MESSAGE,
    HOME_ROUTE_ALLOWANCE,
    SeverityForOutcome,
    TakePendingMessage,
    ToAllowedRoute,
    ToScreenFeedback,
    type MessageSeverity,
} from "@/lib/apiClient";
import { useSession } from "@/lib/session";
import {
    INVALID_KEY_MESSAGE,
    useFunctionKeys,
    type FunctionKeyDefinition,
    type InvalidKeyAction,
} from "@/lib/useFunctionKeys";

const TRANSACTION_ID = "CC00";
const PROGRAM_NAME = "COSGN00C";
const SCREEN_TITLE = "This is a Credit Card Demo Application for Mainframe Modernization";
const INSTRUCTION_TEXT = "Type your User ID and Password, then press ENTER:";
// The BMS INITIAL texts of the COSGN00 USERID and PASSWD labels, verbatim.
const USER_ID_LABEL = "User ID     :";
const PASSWORD_LABEL = "Password    :";

// BMS LENGTH=8 of both USERID and PASSWD.
const CREDENTIAL_FIELD_LENGTH = 8;
const CREDENTIAL_HELPER_TEXT = "(8 Char)";
const FIELD_ID_PREFIX = "signon-";

const ERROR_SEVERITY = SeverityForOutcome({ isError: true, isCompletedWrite: false });
const INFORMATION_SEVERITY = SeverityForOutcome({ isError: false, isCompletedWrite: false });

// The sign-off notice's Paper insets its info Alert, so the two read as one card holding the
// message rather than a shadow hugging the Alert's smaller corners.
const THANK_YOU_NOTICE_STYLE: SxProps<Theme> = { p: 4 };

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
        label: USER_ID_LABEL,
        helperText: CREDENTIAL_HELPER_TEXT,
        maxLength: CREDENTIAL_FIELD_LENGTH,
        type: "text",
        autoComplete: "username",
    },
    {
        name: "password",
        label: PASSWORD_LABEL,
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

// A next_route that is not a home route is an unexpected answer: the abend text in red, with the
// cursor back on USERID as on a fresh screen.
const UNEXPECTED_ROUTE_FEEDBACK: SignOnFeedback = {
    ...FRESH_SCREEN_FEEDBACK,
    message: ABEND_MESSAGE,
    severity: ERROR_SEVERITY,
};

// WHEN OTHER: MOVE 'Y' TO WS-ERR-FLG and CCDA-MSG-INVALID-KEY in red, with the cursor back on
// USERID, the map's IC field. No field is in error.
const INVALID_KEY_FEEDBACK: SignOnFeedback = {
    ...FRESH_SCREEN_FEEDBACK,
    message: INVALID_KEY_MESSAGE,
    severity: ERROR_SEVERITY,
};

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

// The PF keys, the answer to any other PF key, and whether the screen is busy: a call in flight,
// or the route change it started.
interface SignOnKeys {
    keys: FunctionKeyDefinition[];
    invalidKeyAction: InvalidKeyAction;
    isBusy: boolean;
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

// The trim only decides whether the sign-off notice shows, as on the message line; the text itself
// is rendered untouched.
function IsBlankText(text: string): boolean {
    return text.trim() === "";
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

// Logs out the session a refused login opened; neither its thank-you literal nor its failure is
// shown. True once Logout resolves: the server cleared the cookie, or found no session left to
// end. An axios failure gives false: the cookie the server never cleared may still be valid.
// Anything else is a defect and propagates.
async function EndRefusedSession(): Promise<boolean> {
    try {
        await Logout();
        return true;
    } catch (error) {
        if (!axios.isAxiosError(error)) {
            throw error;
        }
        return false;
    }
}

// The login has already set the session cookie, so a refused sign-on logs it out first. Once the
// logout is confirmed, the session context forgets any earlier user as well: /signon reached by
// client-side navigation (Back from a menu) still holds one, and the login replaced its cookie.
// An unconfirmed logout may leave that session live, so the context reads it back from the server
// instead of showing a sign-off that did not happen; F3 then retries the sign-off. Either way the
// sign-on fails with the abend text, PASSWD re-sent empty, and the refused route is never followed.
async function RefuseSignOn(context: SignOnContext): Promise<void> {
    const isLoggedOut = await EndRefusedSession();
    if (isLoggedOut) {
        context.ClearSession();
    } else {
        await context.RefreshSession();
    }
    context.form.SetFeedback({ ...UNEXPECTED_ROUTE_FEEDBACK });
    context.form.ClearPassword();
}

// The XCTL to COADM01C or COMEN01C, once the session context has read the new user. A next_route
// that is not exactly a home route is never navigated to: the sign-on is refused instead.
async function CompleteSignOn(context: SignOnContext, nextRoute: unknown): Promise<void> {
    const homeRoute = ToAllowedRoute(nextRoute, HOME_ROUTE_ALLOWANCE);
    if (homeRoute === null) {
        await RefuseSignOn(context);
        return;
    }
    await context.RefreshSession();
    context.Navigate(homeRoute);
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
        await CompleteSignOn(context, response.next_route);
    } catch (error) {
        form.SetFeedback(FeedbackFromError(error));
        form.ClearPassword();
    } finally {
        form.SetBusy(false);
    }
}

// F3 sign-off. The CSMSG01Y thank-you literal Logout resolves with replaces SEND-PLAIN-TEXT, and
// the session context forgets the signed-off user.
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

// WHEN OTHER reads no file, so nothing reaches the server: the typed User ID and Password stay,
// and the invalid-key literal takes the sign-off notice's place, as ENTER and F3 clear that
// notice. Each press is a new feedback, so the cursor returns to USERID every time.
function ShowInvalidKey(form: SignOnForm): void {
    form.SetThankYouText("");
    form.SetFeedback({ ...INVALID_KEY_FEEDBACK });
}

// The COSGN00 legend ENTER=Sign-on  F3=Exit; no other key is bound.
function BuildSignOnKeys(context: SignOnContext, isBusy: boolean): FunctionKeyDefinition[] {
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

// A menu's F3 sign-off stores its CSMSG01Y literal before it returns here. The stored text is
// taken once and shows as the sign-off notice, as this screen's own F3 shows it; blank text shows
// nothing. The fresh screen's feedback already puts the cursor on USERID.
function usePendingMessage(setThankYouText: Dispatch<SetStateAction<string>>): void {
    useEffect(() => {
        const pendingMessage = TakePendingMessage();
        if (!IsBlankText(pendingMessage)) {
            setThankYouText(pendingMessage);
        }
    }, [setThankYouText]);
}

function useSignOnForm(): SignOnForm {
    const credentialState = useCredentials();
    const screenState = useScreenState();
    const fieldRefs = useFieldRefs();
    useCursorFocus(fieldRefs, screenState.feedback);
    usePendingMessage(screenState.SetThankYouText);
    return useMemo(
        () => ({ ...credentialState, ...screenState, fieldRefs }),
        [credentialState, screenState, fieldRefs],
    );
}

// The XCTL to the user's menu ends with that menu: the route change runs as a transition, so the
// screen stays busy until the menu has rendered and a second ENTER cannot sign on again. Any other
// PF key is refused while busy, like ENTER and F3.
function useSignOnKeys(form: SignOnForm): SignOnKeys {
    const router = useRouter();
    const { RefreshSession, ClearSession } = useSession();
    const [isNavigating, startNavigation] = useTransition();
    const isBusy = form.isBusy || isNavigating;
    const navigate = useCallback(
        (route: string) => startNavigation(() => router.push(route)),
        [router],
    );
    const keys = useMemo(
        () => BuildSignOnKeys({ form, RefreshSession, ClearSession, Navigate: navigate }, isBusy),
        [form, RefreshSession, ClearSession, navigate, isBusy],
    );
    const invalidKeyAction = useMemo(
        () => ({ onPress: () => ShowInvalidKey(form), disabled: isBusy }),
        [form, isBusy],
    );
    return { keys, invalidKeyAction, isBusy };
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

// The field keeps the cursor but refuses keystrokes while a call is in flight.
function CredentialField(props: CredentialFieldProps) {
    const { definition } = props;
    const isBusy = useScreenBusy();
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
            slotProps={{ htmlInput: CredentialInputProps(props), input: { readOnly: isBusy } }}
        />
    );
}

// The fields keep their intrinsic width rather than stretching across the screen.
function CredentialFields({ form }: SignOnFormProps) {
    return (
        <Stack spacing={4} sx={START_ALIGNED_COLUMN_STYLE}>
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
    return (
        <Paper sx={THANK_YOU_NOTICE_STYLE}>
            <Alert severity="info">{text}</Alert>
        </Paper>
    );
}

// The ERRMSG line below the form. A sign-off, from this screen's F3 or a menu's, leaves that line
// blank, and its thank-you notice takes the line's place until the next PF key or ENTER clears it.
function SignOnMessages({ form }: SignOnFormProps) {
    if (!IsBlankText(form.thankYouText)) {
        return <ThankYouNotice text={form.thankYouText} />;
    }
    return (
        <MessageLine
            message={form.feedback.message}
            severity={form.feedback.severity}
            infoMessage={form.feedback.infoMessage}
        />
    );
}

function SignOnScreen() {
    const form = useSignOnForm();
    const { keys, invalidKeyAction, isBusy } = useSignOnKeys(form);
    useFunctionKeys(keys, invalidKeyAction);
    return (
        <ScreenShell
            transactionId={TRANSACTION_ID}
            programName={PROGRAM_NAME}
            screenTitle={SCREEN_TITLE}
            isBusy={isBusy}
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
