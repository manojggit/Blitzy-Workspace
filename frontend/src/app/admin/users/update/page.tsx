// Replaces: BMS map COUSR02, program COUSR02C (CU02); copybooks COUSR02 (cpy-bms), CSUSR01Y,
// COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
"use client";

import {
    Suspense,
    createRef,
    useEffect,
    useRef,
    useState,
    useTransition,
    type Dispatch,
    type FormEvent,
    type RefObject,
    type SetStateAction,
    type TransitionStartFunction,
} from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Divider, Grid, Stack, TextField } from "@mui/material";
import { isAxiosError } from "axios";

import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import { ErrorMessageReference, HelperTextIdOf, MessageLine } from "@/components/MessageLine";
import { ScreenShell } from "@/components/ScreenShell";
import {
    IsCancelledRequest,
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    type MessageSeverity,
    type OutcomeFlags,
} from "@/lib/apiClient";
import {
    FetchUser,
    UpdateUser,
    UserFetchScreen,
    type UserDetailResponse,
    type UserMessageResponse,
} from "@/lib/api/users";
import {
    INVALID_KEY_MESSAGE,
    useFunctionKeys,
    type FunctionKeyDefinition,
    type InvalidKeyAction,
    type PfKey,
} from "@/lib/useFunctionKeys";

const TRANSACTION_ID = "CU02";
const PROGRAM_NAME = "COUSR02C";
const SCREEN_TITLE = "Update User";

// F3 returns to CDEMO-FROM-PROGRAM: the user list (COUSR00C) whenever the userId query parameter
// is present, whatever its value, otherwise the admin menu (COADM01C). COUSR02C tests
// CDEMO-FROM-PROGRAM and CDEMO-CU02-USR-SELECTED as separate COMMAREA fields, so the parameter's
// presence stands for the first and its value for the second. F12 always returns to the admin
// menu.
const USER_LIST_ROUTE = "/admin/users";
const ADMIN_MENU_ROUTE = "/admin";
// CDEMO-CU02-USR-SELECTED: the user COUSR00C selected with U.
const USER_ID_QUERY_KEY = "userId";
// USRIDIN LENGTH=8, the PIC X(08) of CDEMO-CU02-USR-SELECTED.
const USER_ID_LENGTH = 8;

// An empty USRIDIN travels as SPACES, which the server answers with "User ID can NOT be empty...".
const BLANK_USER_ID = " ";
// SPACES as COBOL tests it: spaces only, so the text itself is never trimmed.
const ALL_SPACES_PATTERN = /^ *$/;

const USER_ID_FIELD = "user_id";
const FIELD_GRID_SIZE = { xs: 12, md: 6 } as const;

const ERROR_OUTCOME: OutcomeFlags = { isError: true, isCompletedWrite: false };

type UserFieldName = "user_id" | "first_name" | "last_name" | "password" | "user_type";
type UserFieldValues = Record<UserFieldName, string>;
type FieldRefs = Record<UserFieldName, RefObject<HTMLInputElement | null>>;
type AppRouter = ReturnType<typeof useRouter>;
// The controller of the fetch in flight, null once it settles. A newer fetch and the screen's
// unmount abort it; the saves are writes, never take a signal, and are refused while it is set.
type FetchControllerRef = RefObject<AbortController | null>;

// One COUSR2A input: the BMS INITIAL label, the field LENGTH and the PASSWD and USRTYPE hints.
interface FieldDefinition {
    name: UserFieldName;
    label: string;
    maxLength: number;
    type?: "password";
    autoComplete?: string;
    helperText?: string;
}

interface ScreenFeedbackState {
    message: string;
    severity: MessageSeverity;
    infoMessage: string;
    fieldErrors: readonly string[];
    cursorField: string | null;
}

interface SuccessOutcome {
    message: string;
    cursorField: string | null;
    isCompletedWrite: boolean;
}

// Everything the PF-key handlers read or change. isInFlightRef is the synchronous write lock: true
// from the F5 or F3 that sends a save until that request settles, so a key pressed before the busy
// render reaches the keys is refused.
interface UpdateScreenContext {
    values: UserFieldValues;
    SetValues: Dispatch<SetStateAction<UserFieldValues>>;
    SetFeedback: Dispatch<SetStateAction<ScreenFeedbackState>>;
    SetIsBusy: Dispatch<SetStateAction<boolean>>;
    router: AppRouter;
    // Runs F3's route change as a transition, which keeps the screen busy until the screen it
    // opens has rendered.
    startNavigation: TransitionStartFunction;
    openedFromList: boolean;
    fetchControllerRef: FetchControllerRef;
    isInFlightRef: RefObject<boolean>;
}

interface RoutedNavigation {
    router: AppRouter;
    isNavigating: boolean;
    startNavigation: TransitionStartFunction;
}

interface UserUpdateModel {
    context: UpdateScreenContext;
    feedback: ScreenFeedbackState;
    isBusy: boolean;
    fieldRefs: FieldRefs;
}

interface KeyAction {
    key: PfKey;
    label: string;
    Run: (context: UpdateScreenContext) => Promise<void> | void;
}

interface UserDetailFieldsProps {
    model: UserUpdateModel;
}

interface FieldGroupProps {
    definitions: readonly FieldDefinition[];
    model: UserUpdateModel;
}

interface UserDetailFieldProps {
    definition: FieldDefinition;
    model: UserUpdateModel;
}

interface FeedbackLineProps {
    feedback: ScreenFeedbackState;
}

interface UserUpdateScreenProps {
    selectedUserId: string | null;
}

// USRIDIN sits above the map's row of asterisks; the user details sit below it.
const KEY_FIELD_DEFINITIONS: readonly FieldDefinition[] = [
    { name: "user_id", label: "Enter User ID:", maxLength: USER_ID_LENGTH, autoComplete: "off" },
];
// The name and type fields hold another user's record, so the browser never autofills them.
// The stored password is a bcrypt hash, so PASSWD is never prefilled; left blank it keeps the
// stored one.
const DETAIL_FIELD_DEFINITIONS: readonly FieldDefinition[] = [
    { name: "first_name", label: "First Name:", maxLength: 20, autoComplete: "off" },
    { name: "last_name", label: "Last Name:", maxLength: 20, autoComplete: "off" },
    // The autocomplete token is the one the design-system mapping gives every password field
    // (COSGN00, COUSR01, COUSR02).
    {
        name: "password",
        label: "Password:",
        maxLength: 8,
        type: "password",
        autoComplete: "current-password",
        helperText: "(8 Char)",
    },
    {
        name: "user_type",
        label: "User Type:",
        maxLength: 1,
        autoComplete: "off",
        helperText: "(A=Admin, U=User)",
    },
];
const FIELD_DEFINITIONS: readonly FieldDefinition[] = [
    ...KEY_FIELD_DEFINITIONS,
    ...DETAIL_FIELD_DEFINITIONS,
];

const EMPTY_FIELD_VALUES: UserFieldValues = {
    user_id: "",
    first_name: "",
    last_name: "",
    password: "",
    user_type: "",
};

const EMPTY_FEEDBACK: ScreenFeedbackState = {
    message: "",
    severity: "info",
    infoMessage: "",
    fieldErrors: [],
    cursorField: null,
};

function IsBlank(text: string): boolean {
    return ALL_SPACES_PATTERN.test(text);
}

function IsMissingQueryValue(value: string | null): value is "" | null {
    return value === null || value === "";
}

function IsUserFieldName(name: string): name is UserFieldName {
    return FIELD_DEFINITIONS.some((definition) => definition.name === name);
}

function InitialValues(userId: string | null): UserFieldValues {
    return { ...EMPTY_FIELD_VALUES, user_id: userId ?? "" };
}

function CreateFieldRefs(): FieldRefs {
    return {
        user_id: createRef(),
        first_name: createRef(),
        last_name: createRef(),
        password: createRef(),
        user_type: createRef(),
    };
}

function ChangeFieldValue(context: UpdateScreenContext, name: UserFieldName, value: string): void {
    context.SetValues((current) => ({ ...current, [name]: value }));
}

// The typed password lives only until the next response, whatever that response is.
function ClearPassword(context: UpdateScreenContext): void {
    context.SetValues((current) => ({ ...current, password: "" }));
}

function ApplySuccess(context: UpdateScreenContext, outcome: SuccessOutcome): void {
    context.SetFeedback({
        ...EMPTY_FEEDBACK,
        message: outcome.message,
        severity: SeverityForOutcome({
            isError: false,
            isCompletedWrite: outcome.isCompletedWrite,
        }),
        cursorField: outcome.cursorField,
    });
    ClearPassword(context);
}

// Only an axios error carries a legacy outcome; anything else is a defect and propagates.
function ApplyFailure(context: UpdateScreenContext, error: unknown): void {
    if (!isAxiosError(error)) {
        throw error;
    }
    const feedback = ToScreenFeedback(error);
    context.SetFeedback({
        message: feedback.message,
        severity: SeverityForOutcome(ERROR_OUTCOME),
        infoMessage: feedback.info_message,
        fieldErrors: feedback.field_errors,
        cursorField: feedback.cursor_field,
    });
    ClearPassword(context);
}

// A save in flight holds the in-flight lock; a fetch in flight holds fetchControllerRef, which
// ENTER sets as it sends the read and the owning read clears as it settles. Both are refs, so they
// hold in the same turn as the press that started the call, before the busy render.
function IsCallInFlight(context: UpdateScreenContext): boolean {
    return context.isInFlightRef.current || context.fetchControllerRef.current !== null;
}

// F5 and F3 each send one UPDATE-USER-INFO, and only while no call is in flight, as the 3270
// keyboard stays locked from the AID until the answer. The disabled keys take effect only a render
// after the press, so in that gap a second save is refused, and one press sends one PUT; a save
// pressed in ENTER's turn is refused too, so no PUT carries the values from before the read and
// the read's answer never replaces the save's. The lock clears when the save settles, a rethrown
// defect included.
async function RunBusy(context: UpdateScreenContext, work: () => Promise<void>): Promise<void> {
    if (IsCallInFlight(context)) {
        return;
    }
    context.isInFlightRef.current = true;
    context.SetIsBusy(true);
    try {
        await work();
    } finally {
        context.isInFlightRef.current = false;
        context.SetIsBusy(false);
    }
}

// READ-USER-SEC-FILE answered NORMAL: the record fills the map, the cursor stays on USRIDIN, and
// "Press PF5 key to save your updates ..." shows in the neutral colour.
function ShowFetchedUser(context: UpdateScreenContext, user: UserDetailResponse): void {
    context.SetValues({
        user_id: user.user_id,
        first_name: user.first_name,
        last_name: user.last_name,
        password: "",
        user_type: user.user_type,
    });
    ApplySuccess(context, {
        message: user.message ?? "",
        cursorField: USER_ID_FIELD,
        isCompletedWrite: false,
    });
}

// A newer fetch aborts the one in flight, so only the newest read changes the screen.
function TakeOverFetch(context: UpdateScreenContext): AbortController {
    context.fetchControllerRef.current?.abort();
    const controller = new AbortController();
    context.fetchControllerRef.current = controller;
    return controller;
}

// Only the fetch still owning the screen ends the busy state, never one a newer fetch replaced.
function ReleaseFetch(context: UpdateScreenContext, controller: AbortController): void {
    if (context.fetchControllerRef.current !== controller) {
        return;
    }
    context.fetchControllerRef.current = null;
    context.SetIsBusy(false);
}

// An aborted read rejects with axios's CanceledError. It is not a server outcome, so it is
// dropped before ApplyFailure would show it as the abend text, and the fields stay as they are.
async function ReadUser(
    context: UpdateScreenContext,
    userIdText: string,
    signal: AbortSignal,
): Promise<void> {
    try {
        const user = await FetchUser(userIdText || BLANK_USER_ID, UserFetchScreen.UPDATE, {
            signal,
        });
        ShowFetchedUser(context, user);
    } catch (error) {
        if (IsCancelledRequest(error)) {
            return;
        }
        ApplyFailure(context, error);
    }
}

// PROCESS-ENTER-KEY: a non-blank user ID blanks the details before the read, so a failed read
// shows them blank; a blank one keeps what was typed.
async function RunFetch(context: UpdateScreenContext, userIdText: string): Promise<void> {
    const controller = TakeOverFetch(context);
    if (!IsBlank(userIdText)) {
        context.SetValues({ ...EMPTY_FIELD_VALUES, user_id: userIdText });
    }
    context.SetIsBusy(true);
    try {
        await ReadUser(context, userIdText, controller.signal);
    } finally {
        ReleaseFetch(context, controller);
    }
}

// UPDATE-USER-INFO with the text exactly as typed; the server runs every COUSR02C edit. The
// destructured names are the snake_case JSON keys of the users schema, the API-key exception to
// the MK_Modernization Rule's camelCase variables.
function SendUpdate(context: UpdateScreenContext): Promise<UserMessageResponse> {
    const { user_id, first_name, last_name, password, user_type } = context.values;
    return UpdateUser(user_id || BLANK_USER_ID, { first_name, last_name, password, user_type });
}

// F3 performs UPDATE-USER-INFO and then XCTLs back whatever it answered, so its message is
// never shown.
async function SaveIgnoringOutcome(context: UpdateScreenContext): Promise<void> {
    try {
        await SendUpdate(context);
    } catch (error) {
        if (!isAxiosError(error)) {
            throw error;
        }
    }
}

// ENTER is refused while a save is in flight, so the save's answer lands on the screen that sent
// it. A fetch takes no lock: a newer ENTER supersedes the fetch still in flight, and only the saves
// wait for it.
async function HandleFetch(context: UpdateScreenContext): Promise<void> {
    if (context.isInFlightRef.current) {
        return;
    }
    await RunFetch(context, context.values.user_id);
}

async function HandleSave(context: UpdateScreenContext): Promise<void> {
    await RunBusy(context, async () => {
        try {
            const outcome = await SendUpdate(context);
            ApplySuccess(context, {
                message: outcome.message,
                cursorField: outcome.cursor_field ?? null,
                isCompletedWrite: true,
            });
        } catch (error) {
            ApplyFailure(context, error);
        }
    });
}

async function HandleSaveAndExit(context: UpdateScreenContext): Promise<void> {
    await RunBusy(context, async () => {
        await SaveIgnoringOutcome(context);
        const route = context.openedFromList ? USER_LIST_ROUTE : ADMIN_MENU_ROUTE;
        context.startNavigation(() => context.router.push(route));
    });
}

// CLEAR-CURRENT-SCREEN: every field and the message blank, the cursor on USRIDIN. It is refused
// while a save is in flight, so the save's answer still lands on the screen that sent it.
function HandleClear(context: UpdateScreenContext): void {
    if (context.isInFlightRef.current) {
        return;
    }
    context.SetValues(EMPTY_FIELD_VALUES);
    context.SetFeedback({ ...EMPTY_FEEDBACK, cursorField: USER_ID_FIELD });
}

// PF12 leaves the screen. It is refused while a save is in flight, so the answer is always shown.
function HandleCancel(context: UpdateScreenContext): void {
    if (context.isInFlightRef.current) {
        return;
    }
    context.router.push(ADMIN_MENU_ROUTE);
}

// The map's legend: ENTER=Fetch  F3=Save&Exit  F4=Clear  F5=Save  F12=Cancel.
const KEY_ACTIONS: readonly KeyAction[] = [
    { key: "ENTER", label: "Fetch", Run: HandleFetch },
    { key: "F3", label: "Save&Exit", Run: HandleSaveAndExit },
    { key: "F4", label: "Clear", Run: HandleClear },
    { key: "F5", label: "Save", Run: HandleSave },
    { key: "F12", label: "Cancel", Run: HandleCancel },
];

// Every key waits while a call runs, so one F5 or F3 is one write. Disabling takes effect a render
// after the press, so each handler also refuses a press made in that gap while a save is in flight
// through the in-flight lock.
function BuildFunctionKeys(context: UpdateScreenContext, isBusy: boolean): FunctionKeyDefinition[] {
    return KEY_ACTIONS.map(({ key, label, Run }) => ({
        key,
        label,
        disabled: isBusy,
        onPress: () => {
            void Run(context);
        },
    }));
}

// COUSR02C WHEN OTHER (lines 127-129): MOVE 'Y' TO WS-ERR-FLG and CCDA-MSG-INVALID-KEY in red. No
// file is read or written, so every keyed value, the typed password included, stays; the SEND's
// CURSOR, with no field's length at -1, puts the cursor on USRIDIN, the map's IC field. It is
// refused while a save is in flight, so the save's answer is the message shown.
function ShowInvalidKey(context: UpdateScreenContext): void {
    if (context.isInFlightRef.current) {
        return;
    }
    context.SetFeedback({
        ...EMPTY_FEEDBACK,
        message: INVALID_KEY_MESSAGE,
        severity: SeverityForOutcome(ERROR_OUTCOME),
        cursorField: USER_ID_FIELD,
    });
}

// A PF key the legend lacks answers with the WHEN OTHER branch, and like every key it waits while a
// call runs; a press made before the busy render meets the in-flight lock instead.
function BuildInvalidKeyAction(context: UpdateScreenContext, isBusy: boolean): InvalidKeyAction {
    return { onPress: () => ShowInvalidKey(context), disabled: isBusy };
}

// A message carried across the navigation that opened the screen shows on first entry.
function ShowPendingMessage(context: UpdateScreenContext): void {
    const pendingMessage = TakePendingMessage();
    if (pendingMessage === "") {
        return;
    }
    context.SetFeedback({
        ...EMPTY_FEEDBACK,
        message: pendingMessage,
        severity: SeverityForOutcome(ERROR_OUTCOME),
    });
}

// Reads the controller at unmount time, so it aborts the fetch in flight then, if any, and
// reopens the first-entry guard for a remount.
function AbortPendingFetch(
    fetchControllerRef: FetchControllerRef,
    hasStartedRef: RefObject<boolean>,
): void {
    const pendingFetch = fetchControllerRef.current;
    if (pendingFetch === null) {
        return;
    }
    pendingFetch.abort();
    hasStartedRef.current = false;
}

// React recognises hooks only by the camelCase "use" prefix, so the six hook names below are a
// framework-mandated exception to the MK_Modernization Rule's PascalCase function naming.

// Leaving the screen aborts the fetch in flight. React Strict Mode's rehearsal unmount aborts the
// first-entry fetch that way, so the entry guard is reopened and the remount fetches again.
function useAbortFetchOnUnmount(
    fetchControllerRef: FetchControllerRef,
    hasStartedRef: RefObject<boolean>,
): void {
    useEffect(() => {
        return () => AbortPendingFetch(fetchControllerRef, hasStartedRef);
    }, [fetchControllerRef, hasStartedRef]);
}

// First entry: a user selected on COUSR00 is fetched at once, as COUSR02C performs
// PROCESS-ENTER-KEY when CDEMO-CU02-USR-SELECTED is set. The effect runs after every render, so
// the ref limits it to one fetch, until an unmount aborts that fetch.
function useInitialFetch(context: UpdateScreenContext, userId: string | null): void {
    const hasStartedRef = useRef(false);
    useEffect(() => {
        if (hasStartedRef.current) {
            return;
        }
        hasStartedRef.current = true;
        if (IsMissingQueryValue(userId)) {
            ShowPendingMessage(context);
        } else {
            void RunFetch(context, userId);
        }
    }, [context, userId]);
    useAbortFetchOnUnmount(context.fetchControllerRef, hasStartedRef);
}

// Each response moves the cursor to the field it names, as MOVE -1 TO <field>L did.
function useCursorFocus(feedback: ScreenFeedbackState, fieldRefs: FieldRefs): void {
    useEffect(() => {
        const { cursorField } = feedback;
        if (cursorField !== null && IsUserFieldName(cursorField)) {
            fieldRefs[cursorField].current?.focus();
        }
    }, [feedback, fieldRefs]);
}

function useRoutedNavigation(): RoutedNavigation {
    const router = useRouter();
    const [isNavigating, startNavigation] = useTransition();
    return { router, isNavigating, startNavigation };
}

// The context is rebuilt on every render, so the fetch that owns the screen and the in-flight lock
// are each one ref for the screen's life, which every rebuilt context holds.
function useScreenCallRefs(): Pick<UpdateScreenContext, "fetchControllerRef" | "isInFlightRef"> {
    const fetchControllerRef = useRef<AbortController | null>(null);
    const isInFlightRef = useRef(false);
    return { fetchControllerRef, isInFlightRef };
}

// The screen is busy while a call runs and, after F3's save, until the routed screen renders.
function useUserUpdate(userId: string | null): UserUpdateModel {
    const { router, isNavigating, startNavigation } = useRoutedNavigation();
    const [values, setValues] = useState<UserFieldValues>(() => InitialValues(userId));
    const [feedback, setFeedback] = useState<ScreenFeedbackState>(EMPTY_FEEDBACK);
    const [isBusy, setIsBusy] = useState(false);
    const [fieldRefs] = useState<FieldRefs>(CreateFieldRefs);
    const context: UpdateScreenContext = {
        values,
        SetValues: setValues,
        SetFeedback: setFeedback,
        SetIsBusy: setIsBusy,
        router,
        startNavigation,
        openedFromList: userId !== null,
        ...useScreenCallRefs(),
    };
    useInitialFetch(context, userId);
    useCursorFocus(feedback, fieldRefs);
    return { context, feedback, isBusy: isBusy || isNavigating, fieldRefs };
}

function PreventFormSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
}

// While the server names the field in error, it is described by the ERRMSG line, then its hint.
function ErrorReferenceOf(definition: FieldDefinition, model: UserUpdateModel) {
    const hasError = model.feedback.fieldErrors.includes(definition.name);
    return ErrorMessageReference(hasError, HelperTextIdOf(definition.name, definition.helperText));
}

// The BMS LENGTH, the tie to the ERRMSG line, and the keyboard lock: the field keeps the cursor
// but refuses keystrokes while a call is in flight.
function DetailSlotProps(maxLength: number, definition: FieldDefinition, model: UserUpdateModel) {
    return {
        htmlInput: { maxLength, ...ErrorReferenceOf(definition, model) },
        input: { readOnly: model.isBusy },
    };
}

// The value is stored exactly as typed; only the BMS LENGTH limits it.
function UserDetailField({ definition, model }: UserDetailFieldProps) {
    const { name, maxLength, ...display } = definition;
    return (
        <Grid size={FIELD_GRID_SIZE}>
            <TextField
                {...display}
                id={name}
                name={name}
                variant="outlined"
                fullWidth
                autoFocus={name === USER_ID_FIELD}
                value={model.context.values[name]}
                error={model.feedback.fieldErrors.includes(name)}
                onChange={(event) => ChangeFieldValue(model.context, name, event.target.value)}
                inputRef={model.fieldRefs[name]}
                slotProps={DetailSlotProps(maxLength, definition, model)}
            />
        </Grid>
    );
}

function FieldGroup({ definitions, model }: FieldGroupProps) {
    return (
        <Grid container spacing={4}>
            {definitions.map((definition) => (
                <UserDetailField key={definition.name} definition={definition} model={model} />
            ))}
        </Grid>
    );
}

// The form element only groups the inputs: the PF keys act on them, so a submit does nothing.
function UserDetailFields({ model }: UserDetailFieldsProps) {
    return (
        <Stack component="form" spacing={4} noValidate onSubmit={PreventFormSubmit}>
            <FieldGroup definitions={KEY_FIELD_DEFINITIONS} model={model} />
            <Divider />
            <FieldGroup definitions={DETAIL_FIELD_DEFINITIONS} model={model} />
        </Stack>
    );
}

function FeedbackLine({ feedback }: FeedbackLineProps) {
    return (
        <MessageLine
            message={feedback.message}
            severity={feedback.severity}
            infoMessage={feedback.infoMessage}
        />
    );
}

function UserUpdateScreen({ selectedUserId }: UserUpdateScreenProps) {
    const model = useUserUpdate(selectedUserId);
    const keys = BuildFunctionKeys(model.context, model.isBusy);
    useFunctionKeys(keys, BuildInvalidKeyAction(model.context, model.isBusy));
    return (
        <ScreenShell
            transactionId={TRANSACTION_ID}
            programName={PROGRAM_NAME}
            screenTitle={SCREEN_TITLE}
            isBusy={model.isBusy}
        >
            <UserDetailFields model={model} />
            <FeedbackLine feedback={model.feedback} />
            <FunctionKeyBar keys={keys} />
        </ScreenShell>
    );
}

// CDEMO-CU02-USR-SELECTED is PIC X(08), so only the first 8 characters of a longer userId query
// value fill USRIDIN and are fetched; the text is otherwise kept exactly as given. Characters are
// counted as code points, so a surrogate pair is never split into text the path encoding refuses.
function SelectedUserIdOf(queryValue: string | null): string | null {
    if (queryValue === null) {
        return null;
    }
    return Array.from(queryValue).slice(0, USER_ID_LENGTH).join("");
}

// Each arrival with another ?userId=, or with none, is a fresh COUSR02C first entry, as every
// XCTL is, so the fields, the first-entry fetch and the F3 and F5 targets start over together.
// The discarded screen's unmount aborts a fetch still in flight for the previous arrival, so its
// answer never reaches the new one. A missing userId stays distinct from an empty one, because
// only the latter returns F3 to the user list.
function UserUpdateEntry() {
    const userIdQueryValue = useSearchParams().get(USER_ID_QUERY_KEY);
    return (
        <UserUpdateScreen
            key={JSON.stringify([userIdQueryValue])}
            selectedUserId={SelectedUserIdOf(userIdQueryValue)}
        />
    );
}

// useSearchParams needs a Suspense boundary wherever Next.js prerenders a page. The route layout
// renders this one on demand, so the query is known on the first render and the empty fallback
// never shows.
export default function AdminUsersUpdatePage() {
    return (
        <Suspense fallback={null}>
            <UserUpdateEntry />
        </Suspense>
    );
}
