// Replaces: BMS map COUSR01, program COUSR01C (CU01); copybooks COUSR01 (cpy-bms),
//     CSUSR01Y, COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
"use client";

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
import { useRouter } from "next/navigation";
import { isAxiosError } from "axios";
import { Grid, TextField, type TextFieldProps } from "@mui/material";

import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import { ErrorMessageReference, HelperTextIdOf, MessageLine } from "@/components/MessageLine";
import { ScreenShell, useScreenBusy } from "@/components/ScreenShell";
import {
    ADMIN_HOME_ROUTE,
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    type CompleteScreenFeedback,
    type MessageSeverity,
} from "@/lib/apiClient";
import { AddUser, type UserCreateRequest, type UserMessageResponse } from "@/lib/api/users";
import {
    INVALID_KEY_MESSAGE,
    useFunctionKeys,
    type FunctionKeyDefinition,
} from "@/lib/useFunctionKeys";

const TRANSACTION_ID = "CU01";
const PROGRAM_NAME = "COUSR01C";
const SCREEN_TITLE = "Add User";
const SHELL_HEADER = {
    transactionId: TRANSACTION_ID,
    programName: PROGRAM_NAME,
    screenTitle: SCREEN_TITLE,
} as const;

// ERRMSG is COLOR=RED; only a completed write turns it green (DFHGREEN).
const ERROR_SEVERITY = SeverityForOutcome({ isError: true, isCompletedWrite: false });
const ADDED_SEVERITY = SeverityForOutcome({ isError: false, isCompletedWrite: true });

// The theme's 4 dp spacing unit: 4 is 16px.
const FIELD_SPACING = 4;
// BMS rows 8 and 11 pair their fields side by side; narrow viewports stack them.
const FIELD_GRID_SIZE = { xs: 12, md: 6 } as const;

type UserFieldName = keyof UserCreateRequest;

// Every field is held as keyed, blank included, so each input stays controlled.
type UserFieldValues = Required<UserCreateRequest>;

type UserFieldElements = Partial<Record<UserFieldName, HTMLInputElement | null>>;

interface UserFieldDefinition {
    name: UserFieldName;
    label: string;
    maxLength: number;
    helperText?: string;
    inputType?: "password";
    autoComplete?: string;
}

// COUSR1A in map order: the INITIAL label texts, the input LENGTHs and the hint text beside them.
// The name, ID and type fields hold another user's record, so the browser never autofills them.
const FIELD_DEFINITIONS: readonly UserFieldDefinition[] = [
    { name: "first_name", label: "First Name:", maxLength: 20, autoComplete: "off" },
    { name: "last_name", label: "Last Name:", maxLength: 20, autoComplete: "off" },
    {
        name: "user_id",
        label: "User ID:",
        maxLength: 8,
        helperText: "(8 Char)",
        autoComplete: "off",
    },
    // PASSWD carries the DRK attribute, so the keyed text is never shown. The autocomplete token
    // is the one the design-system mapping gives every password field (COSGN00, COUSR01, COUSR02).
    {
        name: "password",
        label: "Password:",
        maxLength: 8,
        helperText: "(8 Char)",
        inputType: "password",
        autoComplete: "current-password",
    },
    // Free one-character text rather than a choice: COUSR01C stores any non-blank user type.
    {
        name: "user_type",
        label: "User Type:",
        maxLength: 1,
        helperText: "(A=Admin, U=User)",
        autoComplete: "off",
    },
];

const EMPTY_FIELD_VALUES: UserFieldValues = {
    first_name: "",
    last_name: "",
    user_id: "",
    password: "",
    user_type: "",
};

// FNAME carries IC, and INITIALIZE-ALL-FIELDS, WHEN OTHER and the failed write all MOVE -1 TO
// FNAMEL.
const INITIAL_FOCUS_FIELD: UserFieldName = "first_name";

interface UserAddFeedback {
    message: string;
    severity: MessageSeverity;
    fieldErrors: readonly UserFieldName[];
}

const NO_FEEDBACK: UserAddFeedback = { message: "", severity: ERROR_SEVERITY, fieldErrors: [] };

const INVALID_KEY_FEEDBACK: UserAddFeedback = {
    message: INVALID_KEY_MESSAGE,
    severity: ERROR_SEVERITY,
    fieldErrors: [],
};

// A new nonce refocuses a field the cursor is already on, as every SEND ... CURSOR re-places it.
interface FocusRequest {
    field: UserFieldName;
    nonce: number;
}

const INITIAL_FOCUS_REQUEST: FocusRequest = { field: INITIAL_FOCUS_FIELD, nonce: 0 };

// isInFlightRef is the synchronous write lock: true from the ENTER that sends the add until that
// request settles, so a key pressed before the busy render reaches the keys is refused.
interface UserAddContext {
    fieldValues: UserFieldValues;
    setFieldValues: Dispatch<SetStateAction<UserFieldValues>>;
    setFeedback: Dispatch<SetStateAction<UserAddFeedback>>;
    setIsBusy: Dispatch<SetStateAction<boolean>>;
    isInFlightRef: RefObject<boolean>;
    RequestFocus: (field: UserFieldName) => void;
}

interface UserAddScreenModel {
    fieldValues: UserFieldValues;
    feedback: UserAddFeedback;
    isBusy: boolean;
    keys: readonly FunctionKeyDefinition[];
    fieldElementsRef: RefObject<UserFieldElements>;
    SetFieldValue: (name: UserFieldName, value: string) => void;
}

interface UserAddFieldsProps {
    fieldValues: UserFieldValues;
    fieldErrors: readonly UserFieldName[];
    fieldElementsRef: RefObject<UserFieldElements>;
    onFieldChange: (name: UserFieldName, value: string) => void;
}

interface UserAddFieldProps {
    definition: UserFieldDefinition;
    value: string;
    hasError: boolean;
    fieldElementsRef: RefObject<UserFieldElements>;
    onFieldChange: (name: UserFieldName, value: string) => void;
}

function IsUserFieldName(value: unknown): value is UserFieldName {
    return FIELD_DEFINITIONS.some((definition) => definition.name === value);
}

// The server's cursor field, or FNAME where it names none of this screen's fields.
function CursorFieldOf(cursorField: string | null | undefined): UserFieldName {
    return IsUserFieldName(cursorField) ? cursorField : INITIAL_FOCUS_FIELD;
}

// WRITE-USER-SEC-FILE DFHRESP(NORMAL): INITIALIZE-ALL-FIELDS has blanked every input before the
// green confirmation is sent.
function ApplyAddSuccess(context: UserAddContext, response: UserMessageResponse): void {
    context.setFieldValues(EMPTY_FIELD_VALUES);
    context.setFeedback({ message: response.message, severity: ADDED_SEVERITY, fieldErrors: [] });
    context.RequestFocus(CursorFieldOf(response.cursor_field));
}

// A blank required field, DUPKEY/DUPREC or another file condition: the keyed values stay shown.
function ApplyAddFailure(context: UserAddContext, feedback: CompleteScreenFeedback): void {
    context.setFeedback({
        message: feedback.message,
        severity: ERROR_SEVERITY,
        fieldErrors: feedback.field_errors.filter(IsUserFieldName),
    });
    context.RequestFocus(CursorFieldOf(feedback.cursor_field));
}

// ENTER: PROCESS-ENTER-KEY and the write run on the server, which receives the fields exactly as
// keyed. Nothing is trimmed, case-folded or checked here, because COUSR01C keeps the password's
// case, accepts any user type and owns every presence literal. The disabled keys take effect only
// a render after the press, so the in-flight lock, taken before anything else, refuses a second
// ENTER in that gap and one press sends one write. The lock clears when the request settles, a
// rethrown defect included.
async function HandleAddUser(context: UserAddContext): Promise<void> {
    if (context.isInFlightRef.current) {
        return;
    }
    context.isInFlightRef.current = true;
    context.setIsBusy(true);
    try {
        ApplyAddSuccess(context, await AddUser(context.fieldValues));
    } catch (error) {
        if (!isAxiosError(error)) {
            throw error;
        }
        ApplyAddFailure(context, ToScreenFeedback(error));
    } finally {
        context.isInFlightRef.current = false;
        context.setIsBusy(false);
    }
}

// PF4, CLEAR-CURRENT-SCREEN: INITIALIZE-ALL-FIELDS blanks the inputs and the message. It is refused
// while an add is in flight, so the answer still lands on the form that was sent.
function HandleClear(context: UserAddContext): void {
    if (context.isInFlightRef.current) {
        return;
    }
    context.setFieldValues(EMPTY_FIELD_VALUES);
    context.setFeedback(NO_FEEDBACK);
    context.RequestFocus(INITIAL_FOCUS_FIELD);
}

// COUSR01C WHEN OTHER (lines 98-102): the legend shows F12=Exit, but PF12 has no handler, so PF12
// and every other PF key the legend lacks send the keyed values back unchanged with the
// invalid-key literal. It is refused while an add is in flight, so the answer's message is the one
// shown.
function HandleInvalidKey(context: UserAddContext): void {
    if (context.isInFlightRef.current) {
        return;
    }
    context.setFeedback(INVALID_KEY_FEEDBACK);
    context.RequestFocus(INITIAL_FOCUS_FIELD);
}

// PF3 leaves the screen. It is refused while an add is in flight, so the answer is always shown.
function HandleBack(context: UserAddContext, returnToAdminMenu: () => void): void {
    if (context.isInFlightRef.current) {
        return;
    }
    returnToAdminMenu();
}

// The COUSR01 legend in map order: ENTER=Add User  F3=Back  F4=Clear  F12=Exit.
function BuildFunctionKeys(
    context: UserAddContext,
    isBusy: boolean,
    returnToAdminMenu: () => void,
): FunctionKeyDefinition[] {
    const definitions: Omit<FunctionKeyDefinition, "disabled">[] = [
        { key: "ENTER", label: "Add User", onPress: () => void HandleAddUser(context) },
        { key: "F3", label: "Back", onPress: () => HandleBack(context, returnToAdminMenu) },
        { key: "F4", label: "Clear", onPress: () => HandleClear(context) },
        { key: "F12", label: "Exit", onPress: () => HandleInvalidKey(context) },
    ];
    // Every key waits while an add is in flight, so one ENTER is one write. Disabling takes effect
    // a render after the press, so each handler also refuses a press made in that gap through the
    // in-flight lock.
    return definitions.map((definition) => ({ ...definition, disabled: isBusy }));
}

// React recognises hooks only by the camelCase "use" prefix, so the hook names below are a
// framework-mandated exception to the MK_Modernization Rule's PascalCase function naming.

// Replaces MOVE -1 TO <field>L: the requested field takes the cursor after the render that
// follows the request.
function useFocusRequest(
    fieldElementsRef: RefObject<UserFieldElements>,
): (field: UserFieldName) => void {
    const [focusRequest, setFocusRequest] = useState<FocusRequest>(INITIAL_FOCUS_REQUEST);
    useEffect(() => {
        fieldElementsRef.current[focusRequest.field]?.focus();
    }, [fieldElementsRef, focusRequest]);
    return useCallback((field: UserFieldName) => {
        setFocusRequest((request) => ({ field, nonce: request.nonce + 1 }));
    }, []);
}

// A message carried across a navigation (StorePendingMessage) shows once, red as ERRMSG is.
function usePendingMessage(setFeedback: Dispatch<SetStateAction<UserAddFeedback>>): void {
    useEffect(() => {
        const pendingMessage = TakePendingMessage();
        if (pendingMessage.trim() !== "") {
            setFeedback({ message: pendingMessage, severity: ERROR_SEVERITY, fieldErrors: [] });
        }
    }, [setFeedback]);
}

// PF3 is XCTL COADM01C: back to the admin menu, saving nothing. Every PF key the legend lacks takes
// the WHEN OTHER branch as PF12 does, and like every key it waits while an add is in flight. The
// same definitions drive the physical keys and the on-screen buttons.
function useUserAddKeys(context: UserAddContext, isBusy: boolean): FunctionKeyDefinition[] {
    const router = useRouter();
    const keys = useMemo(
        () => BuildFunctionKeys(context, isBusy, () => router.push(ADMIN_HOME_ROUTE)),
        [context, isBusy, router],
    );
    const invalidKeyAction = useMemo(
        () => ({ onPress: () => HandleInvalidKey(context), disabled: isBusy }),
        [context, isBusy],
    );
    useFunctionKeys(keys, invalidKeyAction);
    return keys;
}

// The keyed values with the setters, cursor request and in-flight lock the key handlers act
// through. The lock is one ref for the screen's life, so every context rebuilt from new keyed
// values holds the same lock.
function useUserAddContext(
    setFeedback: Dispatch<SetStateAction<UserAddFeedback>>,
    setIsBusy: Dispatch<SetStateAction<boolean>>,
    requestFocus: (field: UserFieldName) => void,
): UserAddContext {
    const [fieldValues, setFieldValues] = useState<UserFieldValues>(EMPTY_FIELD_VALUES);
    const isInFlightRef = useRef(false);
    return useMemo(
        () => ({
            fieldValues,
            setFieldValues,
            setFeedback,
            setIsBusy,
            isInFlightRef,
            RequestFocus: requestFocus,
        }),
        [fieldValues, setFeedback, setIsBusy, requestFocus],
    );
}

function useUserAdd(): UserAddScreenModel {
    const [feedback, setFeedback] = useState<UserAddFeedback>(NO_FEEDBACK);
    const [isBusy, setIsBusy] = useState(false);
    const fieldElementsRef = useRef<UserFieldElements>({});
    const requestFocus = useFocusRequest(fieldElementsRef);
    usePendingMessage(setFeedback);
    const context = useUserAddContext(setFeedback, setIsBusy, requestFocus);
    const keys = useUserAddKeys(context, isBusy);
    const { fieldValues, setFieldValues } = context;
    // The keyed text is stored unchanged; maxLength (the BMS LENGTH) is the only input limit.
    const setFieldValue = useCallback(
        (name: UserFieldName, value: string) => {
            setFieldValues((values) => ({ ...values, [name]: value }));
        },
        [setFieldValues],
    );
    return { fieldValues, feedback, isBusy, keys, fieldElementsRef, SetFieldValue: setFieldValue };
}

function FieldIdOf(name: UserFieldName): string {
    return `user-add-${name}`;
}

// The attributes a field takes from its BMS definition alone. No native validation attribute is
// set: the server decides validity, and each accessible name stays the BMS label text.
function StaticFieldProps(definition: UserFieldDefinition): TextFieldProps {
    return {
        id: FieldIdOf(definition.name),
        name: definition.name,
        label: definition.label,
        type: definition.inputType,
        autoComplete: definition.autoComplete,
        helperText: definition.helperText,
    };
}

// The BMS LENGTH and, while the server names the field in error, its tie to the ERRMSG line
// ahead of the field's hint.
function HtmlInputProps(definition: UserFieldDefinition, hasError: boolean) {
    const helperTextId = HelperTextIdOf(FieldIdOf(definition.name), definition.helperText);
    return { maxLength: definition.maxLength, ...ErrorMessageReference(hasError, helperTextId) };
}

// The field keeps the cursor but refuses keystrokes while the add is in flight.
function UserAddField(props: UserAddFieldProps) {
    const { definition, fieldElementsRef, onFieldChange } = props;
    const isBusy = useScreenBusy();
    return (
        <TextField
            {...StaticFieldProps(definition)}
            value={props.value}
            onChange={(event) => onFieldChange(definition.name, event.target.value)}
            error={props.hasError}
            inputRef={(element: HTMLInputElement | null) => {
                fieldElementsRef.current[definition.name] = element;
            }}
            fullWidth
            slotProps={{
                htmlInput: HtmlInputProps(definition, props.hasError),
                input: { readOnly: isBusy },
            }}
        />
    );
}

function UserAddFields(props: UserAddFieldsProps) {
    return (
        <Grid container spacing={FIELD_SPACING}>
            {FIELD_DEFINITIONS.map((definition) => (
                <Grid key={definition.name} size={FIELD_GRID_SIZE}>
                    <UserAddField
                        definition={definition}
                        value={props.fieldValues[definition.name]}
                        hasError={props.fieldErrors.includes(definition.name)}
                        fieldElementsRef={props.fieldElementsRef}
                        onFieldChange={props.onFieldChange}
                    />
                </Grid>
            ))}
        </Grid>
    );
}

// No form element: useFunctionKeys handles ENTER from any input, and a native submit would add a
// second action. ScreenShell's Stack spaces the three sections, so they need no wrapper of their
// own.
function UserAddScreen() {
    const screen = useUserAdd();
    return (
        <ScreenShell {...SHELL_HEADER} isBusy={screen.isBusy}>
            <UserAddFields
                fieldValues={screen.fieldValues}
                fieldErrors={screen.feedback.fieldErrors}
                fieldElementsRef={screen.fieldElementsRef}
                onFieldChange={screen.SetFieldValue}
            />
            <MessageLine message={screen.feedback.message} severity={screen.feedback.severity} />
            <FunctionKeyBar keys={screen.keys} />
        </ScreenShell>
    );
}

export default function AdminUsersAddPage() {
    return (
        <Suspense fallback={null}>
            <UserAddScreen />
        </Suspense>
    );
}
