// Replaces: BMS map COUSR03, program COUSR03C (CU03); copybooks COUSR03 (cpy-bms), CSUSR01Y,
// COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
"use client";

import {
    Suspense,
    useCallback,
    useEffect,
    useMemo,
    useRef,
    useState,
    type ChangeEvent,
    type Dispatch,
    type RefObject,
    type SetStateAction,
} from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Divider, Grid, Stack, TextField } from "@mui/material";
import { isAxiosError } from "axios";

import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import { MessageLine, type MessageLineProps } from "@/components/MessageLine";
import { ScreenShell } from "@/components/ScreenShell";
import {
    ADMIN_HOME_ROUTE,
    SIGN_ON_ROUTE,
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    type MessageSeverity,
    type OutcomeFlags,
} from "@/lib/apiClient";
import { DeleteUser, FetchUser, UserFetchScreen, type UserDetailResponse } from "@/lib/api/users";
import { useFunctionKeys, type FunctionKeyDefinition } from "@/lib/useFunctionKeys";

const TRANSACTION_ID = "CU03";
const PROGRAM_NAME = "COUSR03C";
const SCREEN_TITLE = "Delete User";
const SCREEN_IDENTITY = {
    transactionId: TRANSACTION_ID,
    programName: PROGRAM_NAME,
    screenTitle: SCREEN_TITLE,
} as const;

// F3 returns to CDEMO-FROM-PROGRAM: COUSR00C when the list's D selection opened this screen with
// its userId, otherwise COADM01C. F12 always returns to COADM01C.
const USER_LIST_ROUTE = "/admin/users";
const ADMIN_MENU_ROUTE = ADMIN_HOME_ROUTE;
const USER_ID_QUERY_KEY = "userId";

// A blank USRIDIN travels as SPACES, so COUSR03C itself answers "User ID can NOT be empty...".
const BLANK_USER_ID = " ";

const HTTP_UNAUTHORIZED = 401;
const HTTP_FORBIDDEN = 403;

type UserDeleteFieldName = "user_id" | "first_name" | "last_name" | "user_type";

type UserDeleteValues = Readonly<Record<UserDeleteFieldName, string>>;

type FieldRefs = Readonly<Record<UserDeleteFieldName, RefObject<HTMLInputElement | null>>>;

type AppRouter = ReturnType<typeof useRouter>;

interface FieldDefinition {
    name: UserDeleteFieldName;
    label: string;
    maxLength: number;
    isEditable: boolean;
    helperText?: string;
}

interface FeedbackState {
    message: string;
    severity: MessageSeverity;
    infoMessage: string;
    fieldErrors: string[];
    cursorField: string | null;
}

// The setters and router the fetch, delete and clear operations update the screen through.
interface DeleteScreenContext {
    setValues: Dispatch<SetStateAction<UserDeleteValues>>;
    setFeedback: Dispatch<SetStateAction<FeedbackState>>;
    setIsBusy: Dispatch<SetStateAction<boolean>>;
    router: AppRouter;
}

interface DeleteScreenState extends DeleteScreenContext {
    values: UserDeleteValues;
    feedback: FeedbackState;
    isBusy: boolean;
    fieldRefs: FieldRefs;
    onUserIdChange: (userIdText: string) => void;
}

interface UserDeleteActions {
    HandleFetch: () => void;
    HandleDelete: () => void;
    HandleBack: () => void;
    HandleClear: () => void;
    HandleCancel: () => void;
}

interface UserDeleteScreenModel extends DeleteScreenState {
    actions: UserDeleteActions;
}

interface UserDetailFieldsProps {
    values: UserDeleteValues;
    onUserIdChange: (userIdText: string) => void;
    fieldErrors: readonly string[];
    fieldRefs: FieldRefs;
    isBusy: boolean;
}

interface FieldGridProps extends UserDetailFieldsProps {
    definitions: readonly FieldDefinition[];
}

interface UserDetailFieldProps {
    definition: FieldDefinition;
    value: string;
    isError: boolean;
    inputRef: RefObject<HTMLInputElement | null>;
    onUserIdChange: (userIdText: string) => void;
    isBusy: boolean;
}

interface UserDeleteScreenProps {
    selectedUserId: string;
    cameFromList: boolean;
}

const USER_ID_FIELD: UserDeleteFieldName = "user_id";

// USRIDIN is the only unprotected field (UNPROT, IC); FNAME, LNAME and USRTYPE are ASKIP.
const USER_ID_FIELD_DEFINITION: FieldDefinition = {
    name: USER_ID_FIELD,
    label: "Enter User ID:",
    maxLength: 8,
    isEditable: true,
};

const DETAIL_FIELD_DEFINITIONS: readonly FieldDefinition[] = [
    { name: "first_name", label: "First Name:", maxLength: 20, isEditable: false },
    { name: "last_name", label: "Last Name:", maxLength: 20, isEditable: false },
    {
        name: "user_type",
        label: "User Type:",
        maxLength: 1,
        isEditable: false,
        helperText: "(A=Admin, U=User)",
    },
];

const FIELD_DEFINITIONS: readonly FieldDefinition[] = [
    USER_ID_FIELD_DEFINITION,
    ...DETAIL_FIELD_DEFINITIONS,
];

const EMPTY_VALUES: UserDeleteValues = {
    user_id: "",
    first_name: "",
    last_name: "",
    user_type: "",
};

const EMPTY_FEEDBACK: FeedbackState = {
    message: "",
    severity: "error",
    infoMessage: "",
    fieldErrors: [],
    cursorField: null,
};

// The fetch prompt is neutral (DFHNEUTR), the deleted literal green (DFHGREEN), errors red.
const PROMPT_OUTCOME: OutcomeFlags = { isError: false, isCompletedWrite: false };
const DELETED_OUTCOME: OutcomeFlags = { isError: false, isCompletedWrite: true };
const ERROR_OUTCOME: OutcomeFlags = { isError: true, isCompletedWrite: false };

function OutcomeFeedback(
    message: string,
    outcome: OutcomeFlags,
    cursorField: string | null,
): FeedbackState {
    return { ...EMPTY_FEEDBACK, message, severity: SeverityForOutcome(outcome), cursorField };
}

// The server's literal, information line, failing fields and cursor, verbatim.
function ErrorFeedback(error: unknown): FeedbackState {
    const serverFeedback = ToScreenFeedback(error);
    return {
        message: serverFeedback.message,
        severity: SeverityForOutcome(ERROR_OUTCOME),
        infoMessage: serverFeedback.info_message,
        fieldErrors: serverFeedback.field_errors,
        cursorField: serverFeedback.cursor_field,
    };
}

function MessageLinePropsOf(feedback: FeedbackState): MessageLineProps {
    return {
        message: feedback.message,
        severity: feedback.severity,
        infoMessage: feedback.infoMessage,
    };
}

function IsFieldName(name: string): name is UserDeleteFieldName {
    return FIELD_DEFINITIONS.some((definition) => definition.name === name);
}

// A 401 returns to sign-on, as the apiClient interceptor does. A 403 shows nothing here, because
// the interceptor carries its literal to the user's own menu. Anything else is a defect.
function ShowCallError(context: DeleteScreenContext, error: unknown): void {
    if (!isAxiosError(error)) {
        throw error;
    }
    const status = error.response?.status;
    if (status === HTTP_UNAUTHORIZED) {
        context.router.replace(SIGN_ON_ROUTE);
        return;
    }
    if (status !== HTTP_FORBIDDEN) {
        context.setFeedback(ErrorFeedback(error));
    }
}

// Every key press starts with a blank message line, as MAIN-PARA moves SPACES to ERRMSGO.
async function RunScreenCall(
    context: DeleteScreenContext,
    operation: () => Promise<void>,
): Promise<void> {
    context.setFeedback(EMPTY_FEEDBACK);
    context.setIsBusy(true);
    try {
        await operation();
    } catch (error) {
        ShowCallError(context, error);
    } finally {
        context.setIsBusy(false);
    }
}

// The typed user ID stays as keyed, as USRIDINI does; only the protected fields are filled.
function ShowFetchedUser(context: DeleteScreenContext, response: UserDetailResponse): void {
    context.setValues((current) => ({
        user_id: current.user_id,
        first_name: response.first_name,
        last_name: response.last_name,
        user_type: response.user_type,
    }));
    context.setFeedback(OutcomeFeedback(response.message ?? "", PROMPT_OUTCOME, USER_ID_FIELD));
}

// PROCESS-ENTER-KEY: the protected fields are blanked before USRSEC is read.
async function FetchForDelete(context: DeleteScreenContext, userIdText: string): Promise<void> {
    context.setValues((current) => ({ ...EMPTY_VALUES, user_id: current.user_id }));
    await RunScreenCall(context, async () => {
        const response = await FetchUser(userIdText || BLANK_USER_ID, UserFetchScreen.DELETE);
        ShowFetchedUser(context, response);
    });
}

// DELETE-USER-INFO: success clears every field (INITIALIZE-ALL-FIELDS); a refusal keeps them.
async function DeleteForScreen(context: DeleteScreenContext, userIdText: string): Promise<void> {
    await RunScreenCall(context, async () => {
        const response = await DeleteUser(userIdText || BLANK_USER_ID);
        const cursorField = response.cursor_field ?? USER_ID_FIELD;
        context.setValues(EMPTY_VALUES);
        context.setFeedback(OutcomeFeedback(response.message, DELETED_OUTCOME, cursorField));
    });
}

// CLEAR-CURRENT-SCREEN: client only, with the cursor back on the user ID.
function ClearScreen(context: DeleteScreenContext): void {
    context.setValues(EMPTY_VALUES);
    context.setFeedback(OutcomeFeedback("", PROMPT_OUTCOME, USER_ID_FIELD));
}

// The first entry shows the message another screen left for it, in the ERRMSG default red.
function ShowPendingMessage(context: DeleteScreenContext): void {
    const pendingMessage = TakePendingMessage();
    if (pendingMessage !== "") {
        context.setFeedback(OutcomeFeedback(pendingMessage, ERROR_OUTCOME, USER_ID_FIELD));
    }
}

function BuildFunctionKeys(actions: UserDeleteActions, isBusy: boolean): FunctionKeyDefinition[] {
    return [
        { key: "ENTER", label: "Fetch", onPress: actions.HandleFetch, disabled: isBusy },
        { key: "F3", label: "Back", onPress: actions.HandleBack, disabled: isBusy },
        { key: "F4", label: "Clear", onPress: actions.HandleClear, disabled: isBusy },
        { key: "F5", label: "Delete", onPress: actions.HandleDelete, disabled: isBusy },
        { key: "F12", label: "Cancel", onPress: actions.HandleCancel, disabled: isBusy },
    ];
}

// React recognises hooks only by the camelCase "use" prefix, so the hook names below are a
// framework-mandated exception to the MK_Modernization Rule's PascalCase function naming.
function useFieldRefs(): FieldRefs {
    const userIdRef = useRef<HTMLInputElement>(null);
    const firstNameRef = useRef<HTMLInputElement>(null);
    const lastNameRef = useRef<HTMLInputElement>(null);
    const userTypeRef = useRef<HTMLInputElement>(null);
    return useMemo(
        () => ({
            user_id: userIdRef,
            first_name: firstNameRef,
            last_name: lastNameRef,
            user_type: userTypeRef,
        }),
        [],
    );
}

function useDeleteScreenState(selectedUserId: string): DeleteScreenState {
    const [values, setValues] = useState<UserDeleteValues>(() => ({
        ...EMPTY_VALUES,
        user_id: selectedUserId,
    }));
    const [feedback, setFeedback] = useState<FeedbackState>(EMPTY_FEEDBACK);
    const [isBusy, setIsBusy] = useState(false);
    const fieldRefs = useFieldRefs();
    const router = useRouter();
    const onUserIdChange = useCallback((userIdText: string) => {
        setValues((current) => ({ ...current, user_id: userIdText }));
    }, []);
    const screenState = { values, setValues, feedback, setFeedback, isBusy, setIsBusy, router };
    return { ...screenState, fieldRefs, onUserIdChange };
}

function useDeleteScreenContext(state: DeleteScreenState): DeleteScreenContext {
    const { setValues, setFeedback, setIsBusy, router } = state;
    return useMemo(
        () => ({ setValues, setFeedback, setIsBusy, router }),
        [setValues, setFeedback, setIsBusy, router],
    );
}

function useUserDeleteActions(
    context: DeleteScreenContext,
    userIdText: string,
    cameFromList: boolean,
): UserDeleteActions {
    return useMemo(
        () => ({
            HandleFetch: () => void FetchForDelete(context, userIdText),
            HandleDelete: () => void DeleteForScreen(context, userIdText),
            HandleBack: () =>
                context.router.push(cameFromList ? USER_LIST_ROUTE : ADMIN_MENU_ROUTE),
            HandleClear: () => ClearScreen(context),
            HandleCancel: () => context.router.push(ADMIN_MENU_ROUTE),
        }),
        [context, userIdText, cameFromList],
    );
}

// First entry (CDEMO-PGM-REENTER off): the user COUSR00C selected is fetched once, React Strict
// Mode's repeated effect included.
function usePrefetchSelectedUser(selectedUserId: string, context: DeleteScreenContext): void {
    const hasEnteredRef = useRef(false);
    useEffect(() => {
        if (hasEnteredRef.current) {
            return;
        }
        hasEnteredRef.current = true;
        if (selectedUserId === "") {
            ShowPendingMessage(context);
            return;
        }
        void FetchForDelete(context, selectedUserId);
    }, [selectedUserId, context]);
}

// MOVE -1 TO <field>L: each new feedback puts the cursor on the field the server named.
function useCursorFocus(feedback: FeedbackState, fieldRefs: FieldRefs): void {
    useEffect(() => {
        const cursorField = feedback.cursorField;
        if (cursorField !== null && IsFieldName(cursorField)) {
            fieldRefs[cursorField].current?.focus();
        }
    }, [feedback, fieldRefs]);
}

function useUserDelete(selectedUserId: string, cameFromList: boolean): UserDeleteScreenModel {
    const state = useDeleteScreenState(selectedUserId);
    const context = useDeleteScreenContext(state);
    const actions = useUserDeleteActions(context, state.values.user_id, cameFromList);
    usePrefetchSelectedUser(selectedUserId, context);
    useCursorFocus(state.feedback, state.fieldRefs);
    return { ...state, actions };
}

// The BMS LENGTH, and the attribute byte: ASKIP fields are read-only but stay focusable, so the
// cursor can rest on FNAME after a file error. USRIDIN also refuses keystrokes while a fetch or
// delete runs, as the 3270 keyboard locks from send to response, so the details shown always
// belong to the user ID that was sent.
function FieldSlotProps(definition: FieldDefinition, isBusy: boolean) {
    return {
        input: { readOnly: !definition.isEditable || isBusy },
        htmlInput: { maxLength: definition.maxLength },
    };
}

function FieldChangeHandler(props: UserDetailFieldProps) {
    if (!props.definition.isEditable) {
        return undefined;
    }
    return (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
        props.onUserIdChange(event.target.value);
    };
}

function UserDetailField(props: UserDetailFieldProps) {
    const { definition } = props;
    return (
        <TextField
            label={definition.label}
            value={props.value}
            variant={definition.isEditable ? "outlined" : "filled"}
            onChange={FieldChangeHandler(props)}
            error={props.isError}
            helperText={definition.helperText}
            autoFocus={definition.isEditable}
            autoComplete="off"
            inputRef={props.inputRef}
            fullWidth
            slotProps={FieldSlotProps(definition, props.isBusy)}
        />
    );
}

function FieldGrid(props: FieldGridProps) {
    return (
        <Grid container spacing={4}>
            {props.definitions.map((definition) => (
                <Grid key={definition.name} size={{ xs: 12, md: 6 }}>
                    <UserDetailField
                        definition={definition}
                        value={props.values[definition.name]}
                        isError={props.fieldErrors.includes(definition.name)}
                        inputRef={props.fieldRefs[definition.name]}
                        onUserIdChange={props.onUserIdChange}
                        isBusy={props.isBusy}
                    />
                </Grid>
            ))}
        </Grid>
    );
}

// The divider stands in for the row-8 asterisk line between the key and the user's details.
function UserDetailFields(props: UserDetailFieldsProps) {
    return (
        <Stack spacing={4}>
            <FieldGrid {...props} definitions={[USER_ID_FIELD_DEFINITION]} />
            <Divider />
            <FieldGrid {...props} definitions={DETAIL_FIELD_DEFINITIONS} />
        </Stack>
    );
}

function UserDeleteScreen(props: UserDeleteScreenProps) {
    const screen = useUserDelete(props.selectedUserId, props.cameFromList);
    const keys = BuildFunctionKeys(screen.actions, screen.isBusy);
    useFunctionKeys(keys);
    return (
        <ScreenShell {...SCREEN_IDENTITY} isBusy={screen.isBusy}>
            <UserDetailFields
                values={screen.values}
                onUserIdChange={screen.onUserIdChange}
                fieldErrors={screen.feedback.fieldErrors}
                fieldRefs={screen.fieldRefs}
                isBusy={screen.isBusy}
            />
            <MessageLine {...MessageLinePropsOf(screen.feedback)} />
            <FunctionKeyBar keys={keys} />
        </ScreenShell>
    );
}

// Each arrival with another selected user is a fresh COUSR03C first entry, as every XCTL is, so
// the screen state starts over whenever the userId query parameter changes. An answer still in
// flight for the previous user lands on the discarded screen and never reaches the new one.
function UserDeleteEntry() {
    const searchParams = useSearchParams();
    const selectedUserId = searchParams.get(USER_ID_QUERY_KEY) ?? "";
    return (
        <UserDeleteScreen
            key={selectedUserId}
            selectedUserId={selectedUserId}
            cameFromList={searchParams.has(USER_ID_QUERY_KEY)}
        />
    );
}

// useSearchParams needs a Suspense boundary, or the static prerender of the route fails.
export default function AdminUsersDeletePage() {
    return (
        <Suspense fallback={null}>
            <UserDeleteEntry />
        </Suspense>
    );
}
