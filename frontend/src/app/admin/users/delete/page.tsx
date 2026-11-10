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
import {
    ErrorMessageReference,
    HelperTextIdOf,
    MessageLine,
    type MessageLineProps,
} from "@/components/MessageLine";
import { ScreenShell } from "@/components/ScreenShell";
import {
    ADMIN_HOME_ROUTE,
    IsCancelledRequest,
    SIGN_ON_ROUTE,
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    type MessageSeverity,
    type OutcomeFlags,
} from "@/lib/apiClient";
import { DeleteUser, FetchUser, UserFetchScreen, type UserDetailResponse } from "@/lib/api/users";
import {
    INVALID_KEY_MESSAGE,
    useFunctionKeys,
    type FunctionKeyDefinition,
    type InvalidKeyAction,
} from "@/lib/useFunctionKeys";

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
// USRIDIN LENGTH=8, the PIC X(08) of CDEMO-CU03-USR-SELECTED.
const USER_ID_LENGTH = 8;

// A blank USRIDIN travels as SPACES, so COUSR03C itself answers "User ID can NOT be empty...".
const BLANK_USER_ID = " ";

const HTTP_UNAUTHORIZED = 401;
const HTTP_FORBIDDEN = 403;

type UserDeleteFieldName = "user_id" | "first_name" | "last_name" | "user_type";

type UserDeleteValues = Readonly<Record<UserDeleteFieldName, string>>;

type FieldRefs = Readonly<Record<UserDeleteFieldName, RefObject<HTMLInputElement | null>>>;

type AppRouter = ReturnType<typeof useRouter>;

// The controller of the fetch in flight, null once it settles. A newer fetch and the screen's
// unmount abort it; the delete is a write, never takes a signal, and is refused while it is set.
type FetchControllerRef = RefObject<AbortController | null>;

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

// The setters and router the fetch, delete and clear operations update the screen through, the
// fetch that owns the screen, and the synchronous write lock: isInFlightRef is true from the F5
// that sends the delete until that request settles, so a key pressed before the busy render
// reaches the keys is refused.
interface DeleteScreenContext {
    setValues: Dispatch<SetStateAction<UserDeleteValues>>;
    setFeedback: Dispatch<SetStateAction<FeedbackState>>;
    setIsBusy: Dispatch<SetStateAction<boolean>>;
    router: AppRouter;
    fetchControllerRef: FetchControllerRef;
    isInFlightRef: RefObject<boolean>;
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
    HandleInvalidKey: () => void;
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
    maxLength: USER_ID_LENGTH,
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

// Every answer, PF4 and the invalid-key literal set a new feedback object, which typing and the
// busy state keep, so its identity is the outcome: PF4 or a PF key the legend lacks pressed again
// brings the focused cursor field back into view, though the line shows the same text.
function MessageLinePropsOf(feedback: FeedbackState): MessageLineProps {
    return {
        message: feedback.message,
        severity: feedback.severity,
        infoMessage: feedback.infoMessage,
        outcomeKey: feedback,
    };
}

function IsFieldName(name: string): name is UserDeleteFieldName {
    return FIELD_DEFINITIONS.some((definition) => definition.name === name);
}

// A 401 returns to sign-on, as the apiClient interceptor does. A 403 shows nothing here, because
// the interceptor carries its literal to the user's own menu. Any other axios failure is shown as
// the screen's feedback; anything that is not an axios error is a defect and propagates.
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
function BeginScreenCall(context: DeleteScreenContext): void {
    context.setFeedback(EMPTY_FEEDBACK);
    context.setIsBusy(true);
}

// A delete in flight holds the in-flight lock; a fetch in flight holds fetchControllerRef, which
// ENTER sets as it sends the read and the owning read clears as it settles. Both are refs, so they
// hold in the same turn as the press that started the call, before the busy render.
function IsCallInFlight(context: DeleteScreenContext): boolean {
    return context.isInFlightRef.current || context.fetchControllerRef.current !== null;
}

// F5 sends one DELETE-USER-INFO, and only while no call is in flight, as the 3270 keyboard stays
// locked from the AID until the answer. The disabled keys take effect only a render after the
// press, so in that gap a second F5 is refused, and one press sends one DELETE; an F5 pressed in
// ENTER's turn is refused too, so the read's late answer never shows a user just deleted. The lock
// clears when the delete settles, a rethrown defect included.
async function RunScreenCall(
    context: DeleteScreenContext,
    operation: () => Promise<void>,
): Promise<void> {
    if (IsCallInFlight(context)) {
        return;
    }
    context.isInFlightRef.current = true;
    BeginScreenCall(context);
    try {
        await operation();
    } catch (error) {
        ShowCallError(context, error);
    } finally {
        context.isInFlightRef.current = false;
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

// A newer fetch aborts the one in flight, so only the newest read changes the screen.
function TakeOverFetch(context: DeleteScreenContext): AbortController {
    context.fetchControllerRef.current?.abort();
    const controller = new AbortController();
    context.fetchControllerRef.current = controller;
    return controller;
}

// Only the fetch still owning the screen ends the busy state, never one a newer fetch replaced.
function ReleaseFetch(context: DeleteScreenContext, controller: AbortController): void {
    if (context.fetchControllerRef.current !== controller) {
        return;
    }
    context.fetchControllerRef.current = null;
    context.setIsBusy(false);
}

// An aborted read rejects with axios's CanceledError. It is not a server outcome, so it is
// dropped before ShowCallError would show it as the abend text, and the fields stay as they are.
async function ReadForDelete(
    context: DeleteScreenContext,
    userIdText: string,
    signal: AbortSignal,
): Promise<void> {
    try {
        const response = await FetchUser(userIdText || BLANK_USER_ID, UserFetchScreen.DELETE, {
            signal,
        });
        ShowFetchedUser(context, response);
    } catch (error) {
        if (IsCancelledRequest(error)) {
            return;
        }
        ShowCallError(context, error);
    }
}

// PROCESS-ENTER-KEY: the protected fields are blanked before USRSEC is read. ENTER is refused while
// a delete is in flight, so the delete's answer lands on the screen that sent it. A fetch takes no
// lock: a newer ENTER supersedes the fetch still in flight, and only the delete waits for it.
async function FetchForDelete(context: DeleteScreenContext, userIdText: string): Promise<void> {
    if (context.isInFlightRef.current) {
        return;
    }
    const controller = TakeOverFetch(context);
    context.setValues((current) => ({ ...EMPTY_VALUES, user_id: current.user_id }));
    BeginScreenCall(context);
    try {
        await ReadForDelete(context, userIdText, controller.signal);
    } finally {
        ReleaseFetch(context, controller);
    }
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

// CLEAR-CURRENT-SCREEN: client only, with the cursor back on the user ID. It is refused while a
// delete is in flight, so the delete's answer still lands on the screen that sent it.
function ClearScreen(context: DeleteScreenContext): void {
    if (context.isInFlightRef.current) {
        return;
    }
    context.setValues(EMPTY_VALUES);
    context.setFeedback(OutcomeFeedback("", PROMPT_OUTCOME, USER_ID_FIELD));
}

// COUSR03C WHEN OTHER (lines 126-128): MOVE 'Y' TO WS-ERR-FLG and CCDA-MSG-INVALID-KEY in red. No
// file is read or deleted, so the keyed user ID and the details shown stay; the SEND's CURSOR, with
// no field's length at -1, puts the cursor on USRIDIN, the map's IC field. It is refused while a
// delete is in flight, so the delete's answer is the message shown.
function ShowInvalidKey(context: DeleteScreenContext): void {
    if (context.isInFlightRef.current) {
        return;
    }
    context.setFeedback(OutcomeFeedback(INVALID_KEY_MESSAGE, ERROR_OUTCOME, USER_ID_FIELD));
}

// PF3 and PF12 leave the screen. Each is refused while a delete is in flight, so the answer is
// always shown.
function LeaveScreen(context: DeleteScreenContext, route: string): void {
    if (context.isInFlightRef.current) {
        return;
    }
    context.router.push(route);
}

// The first entry shows the message another screen left for it, in the ERRMSG default red.
function ShowPendingMessage(context: DeleteScreenContext): void {
    const pendingMessage = TakePendingMessage();
    if (pendingMessage !== "") {
        context.setFeedback(OutcomeFeedback(pendingMessage, ERROR_OUTCOME, USER_ID_FIELD));
    }
}

// Reads the controller at unmount time, so it aborts the fetch in flight then, if any, and
// reopens the first-entry guard for a remount.
function AbortPendingFetch(
    fetchControllerRef: FetchControllerRef,
    hasEnteredRef: RefObject<boolean>,
): void {
    const pendingFetch = fetchControllerRef.current;
    if (pendingFetch === null) {
        return;
    }
    pendingFetch.abort();
    hasEnteredRef.current = false;
}

// Every key waits while a call runs, so one F5 is one delete. Disabling takes effect a render after
// the press, so each action also refuses a press made in that gap while a delete is in flight
// through the in-flight lock.
function BuildFunctionKeys(actions: UserDeleteActions, isBusy: boolean): FunctionKeyDefinition[] {
    return [
        { key: "ENTER", label: "Fetch", onPress: actions.HandleFetch, disabled: isBusy },
        { key: "F3", label: "Back", onPress: actions.HandleBack, disabled: isBusy },
        { key: "F4", label: "Clear", onPress: actions.HandleClear, disabled: isBusy },
        { key: "F5", label: "Delete", onPress: actions.HandleDelete, disabled: isBusy },
        { key: "F12", label: "Cancel", onPress: actions.HandleCancel, disabled: isBusy },
    ];
}

// A PF key the legend lacks answers with the WHEN OTHER branch, and like every key it waits while a
// call runs; a press made before the busy render meets the in-flight lock instead.
function BuildInvalidKeyAction(actions: UserDeleteActions, isBusy: boolean): InvalidKeyAction {
    return { onPress: actions.HandleInvalidKey, disabled: isBusy };
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
    const fetchControllerRef = useRef<AbortController | null>(null);
    const isInFlightRef = useRef(false);
    const router = useRouter();
    const onUserIdChange = useCallback((userIdText: string) => {
        setValues((current) => ({ ...current, user_id: userIdText }));
    }, []);
    const screenState = { values, setValues, feedback, setFeedback, isBusy, setIsBusy, router };
    return { ...screenState, fieldRefs, fetchControllerRef, isInFlightRef, onUserIdChange };
}

// The in-flight lock is one ref for the screen's life, so the context holds the same lock however
// often it is rebuilt.
function useDeleteScreenContext(state: DeleteScreenState): DeleteScreenContext {
    const { setValues, setFeedback, setIsBusy, router, fetchControllerRef, isInFlightRef } = state;
    return useMemo(
        () => ({ setValues, setFeedback, setIsBusy, router, fetchControllerRef, isInFlightRef }),
        [setValues, setFeedback, setIsBusy, router, fetchControllerRef, isInFlightRef],
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
                LeaveScreen(context, cameFromList ? USER_LIST_ROUTE : ADMIN_MENU_ROUTE),
            HandleClear: () => ClearScreen(context),
            HandleCancel: () => LeaveScreen(context, ADMIN_MENU_ROUTE),
            HandleInvalidKey: () => ShowInvalidKey(context),
        }),
        [context, userIdText, cameFromList],
    );
}

// Leaving the screen aborts the fetch in flight. React Strict Mode's rehearsal unmount aborts the
// first-entry fetch that way, so the entry guard is reopened and the remount fetches again.
function useAbortFetchOnUnmount(
    fetchControllerRef: FetchControllerRef,
    hasEnteredRef: RefObject<boolean>,
): void {
    useEffect(() => {
        return () => AbortPendingFetch(fetchControllerRef, hasEnteredRef);
    }, [fetchControllerRef, hasEnteredRef]);
}

// First entry (CDEMO-PGM-REENTER off): the user COUSR00C selected is fetched once, unless an
// unmount aborts that fetch before it answers.
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
    useAbortFetchOnUnmount(context.fetchControllerRef, hasEnteredRef);
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

function FieldIdOf(name: UserDeleteFieldName): string {
    return `user-delete-${name}`;
}

// The BMS LENGTH, and the attribute byte: ASKIP fields are read-only but stay focusable, so the
// cursor can rest on FNAME after a file error. USRIDIN also refuses keystrokes while a fetch or
// delete runs, as the 3270 keyboard locks from send to response, so the details shown always
// belong to the user ID that was sent. A field in error is described by the ERRMSG line, then its
// hint.
function FieldSlotProps({ definition, isError, isBusy }: UserDetailFieldProps) {
    const helperTextId = HelperTextIdOf(FieldIdOf(definition.name), definition.helperText);
    return {
        input: { readOnly: !definition.isEditable || isBusy },
        htmlInput: {
            maxLength: definition.maxLength,
            ...ErrorMessageReference(isError, helperTextId),
        },
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
            id={FieldIdOf(definition.name)}
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
            slotProps={FieldSlotProps(props)}
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
    useFunctionKeys(keys, BuildInvalidKeyAction(screen.actions, screen.isBusy));
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

// CDEMO-CU03-USR-SELECTED is PIC X(08), so only the first 8 characters of a longer userId query
// value fill USRIDIN and are fetched; the text is otherwise kept exactly as given. Characters are
// counted as code points, so a surrogate pair is never split into text the path encoding refuses.
function SelectedUserIdOf(queryValue: string): string {
    return Array.from(queryValue).slice(0, USER_ID_LENGTH).join("");
}

// Each arrival with another ?userId=, or with none, is a fresh COUSR03C first entry, as every
// XCTL is, so the fields, the first-entry fetch and the F3 and F5 targets start over together.
// The discarded screen's unmount aborts a fetch still in flight for the previous arrival, so its
// answer never reaches the new one. A missing userId stays distinct from an empty one, because
// only the latter returns F3 to the user list.
function UserDeleteEntry() {
    const userIdQueryValue = useSearchParams().get(USER_ID_QUERY_KEY);
    return (
        <UserDeleteScreen
            key={JSON.stringify([userIdQueryValue])}
            selectedUserId={SelectedUserIdOf(userIdQueryValue ?? "")}
            cameFromList={userIdQueryValue !== null}
        />
    );
}

// useSearchParams needs a Suspense boundary wherever Next.js prerenders a page. The route layout
// renders this one on demand, so the query is known on the first render and the empty fallback
// never shows.
export default function AdminUsersDeletePage() {
    return (
        <Suspense fallback={null}>
            <UserDeleteEntry />
        </Suspense>
    );
}
