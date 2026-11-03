// Replaces: BMS map COACTUP, program COACTUPC (CAUP); copybooks COACTUP (cpy-bms), CVACT01Y,
// CVACT03Y, CVCUS01Y, CSLKPCDY, CSUTLDWY, CSSETATY, CVCRD01Y, CSMSG02Y, COCOM01Y, COTTL01Y,
// CSDAT01Y, CSMSG01Y
"use client";

import {
    Grid,
    Stack,
    TextField,
    Typography,
    type SxProps,
    type TextFieldProps,
    type Theme,
} from "@mui/material";
import axios from "axios";
import { useRouter, useSearchParams } from "next/navigation";
import {
    Suspense,
    startTransition,
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
import {
    FetchAccountForEdit,
    UpdateAccount,
    type AccountScreen,
    type AccountScreenFields,
    type AccountUpdateRequest,
} from "@/lib/api/accounts";
import {
    IsCancelledRequest,
    LegacyErrorBody,
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    UpdateAction,
    UpdateScreenState,
    type LegacyErrorResponse,
    type MessageSeverity,
    type ScreenFeedback,
} from "@/lib/apiClient";
import { useFunctionKeys, type FunctionKeyDefinition, type PfKey } from "@/lib/useFunctionKeys";

type AccountFieldName = keyof AccountScreenFields;

interface ScreenFieldDefinition {
    name: AccountFieldName;
    label: string;
    maxLength: number;
    helperText?: string;
    inputMode?: "numeric";
}

interface FieldGroupDefinition {
    groupLabel: string;
    parts: readonly ScreenFieldDefinition[];
}

type SectionEntry = ScreenFieldDefinition | FieldGroupDefinition;

interface SectionDefinitions {
    accountFields: readonly SectionEntry[];
    dateFields: readonly SectionEntry[];
    customerFields: readonly SectionEntry[];
    addressFields: readonly SectionEntry[];
    phoneFields: readonly SectionEntry[];
}

type ThreePartNames = readonly [AccountFieldName, AccountFieldName, AccountFieldName];

// What the COMMAREA kept between two CAUP tasks: the ACUP-CHANGE-ACTION state, the encrypted
// screen_token the server sends back unchanged, the map fields as keyed, and the map fields as
// the server last sent them.
interface ScreenModel {
    state: UpdateScreenState;
    screenToken: string | null;
    fields: AccountScreenFields;
    shownFields: AccountScreenFields;
}

// The ERRMSG and INFOMSG lines and the CSSETATY field attributes of the last response.
interface ScreenFeedbackView {
    message: string;
    severity: MessageSeverity;
    infoMessage: string;
    fieldErrors: readonly string[];
    cursorField: string;
}

interface ScreenSetters {
    setModel: Dispatch<SetStateAction<ScreenModel>>;
    setFeedback: Dispatch<SetStateAction<ScreenFeedbackView>>;
    setIsBusy: Dispatch<SetStateAction<boolean>>;
}

interface ScreenStore {
    model: ScreenModel;
    feedback: ScreenFeedbackView;
    isBusy: boolean;
    setters: ScreenSetters;
}

type InputRefMap = Partial<Record<AccountFieldName, HTMLInputElement | null>>;

type AccountRequest = () => Promise<AccountScreen>;

// signal is passed for a read only: the entry fetch carries its owner's AbortSignal, while a PUT
// takes none and always runs to its answer.
type RequestRunner = (request: AccountRequest, signal?: AbortSignal) => void;

interface InFlightRequest {
    signal?: AbortSignal;
}

// The 4xx and 5xx CAUP bodies carry the screen beside the standard legacy keys.
type AccountErrorBody = LegacyErrorResponse &
    Partial<Pick<AccountScreen, "fields" | "state" | "screen_token">>;

interface ActionContext {
    model: ScreenModel;
    runRequest: RequestRunner;
}

interface KeyHandlers {
    onAction: (action: UpdateAction) => void;
    onExit: () => void;
}

interface FocusRequest {
    inputs: InputRefMap;
    cursorField: string;
    state: UpdateScreenState;
}

interface InputRegistry {
    inputRefs: RefObject<InputRefMap>;
    RegisterInput: (name: AccountFieldName, element: HTMLInputElement | null) => void;
}

interface FieldView {
    fields: AccountScreenFields;
    editableFields: ReadonlySet<AccountFieldName>;
    fieldErrors: ReadonlySet<string>;
    RegisterInput: (name: AccountFieldName, element: HTMLInputElement | null) => void;
    onFieldChange: (name: AccountFieldName, value: string) => void;
}

interface AccountUpdateScreenModel {
    view: FieldView;
    feedback: ScreenFeedbackView;
    isBusy: boolean;
    keys: readonly FunctionKeyDefinition[];
}

interface SectionProps {
    view: FieldView;
}

interface FieldSectionProps {
    entries: readonly SectionEntry[];
    view: FieldView;
}

interface SectionEntryProps {
    entry: SectionEntry;
    view: FieldView;
}

interface FieldGroupProps {
    group: FieldGroupDefinition;
    view: FieldView;
}

interface ScreenFieldProps {
    definition: ScreenFieldDefinition;
    view: FieldView;
}

interface AccountMessageLineProps {
    feedback: ScreenFeedbackView;
}

const TRANSACTION_ID = "CAUP";
const PROGRAM_NAME = "COACTUPC";
const SCREEN_TITLE = "Update Account";
const MENU_ROUTE = "/menu";
const ACCOUNT_ID_QUERY_KEY = "acctId";
// A blank ACCTSID still reaches the server's key edit, which answers it.
const BLANK_ACCOUNT_PATH = " ";
// PROMPT-FOR-SEARCH-KEYS, which 3250-SETUP-INFOMSG shows WHEN CDEMO-PGM-ENTER. An entry without
// ?acctId= sends no request, so the page shows the literal itself.
const FIRST_ENTRY_INFO_MESSAGE = "Enter or update id of account to update";
const CUSTOMER_DETAILS_HEADING = "Customer Details";
const YES_NO_HELPER_TEXT = "Y/N";
// Each input's id, so its helper text has an id that a field in error can reference.
const FIELD_ID_PREFIX = "account-update-";

const ACCOUNT_ID_FIELD: AccountFieldName = "acct_id";

const ENTRY_GRID_SIZE = { xs: 12, md: 6 } as const;
// Theme spacing units: room between fields for an outlined field's raised label.
const FIELD_SPACING = 4;
const CAPTION_SPACING = 3;
const EDITABLE_FIELD_KEY = "editable";
const PROTECTED_FIELD_KEY = "protected";
// The parts of one map field sit side by side, as on the 3270, and stack on narrow viewports.
const GROUP_DIRECTION = { xs: "column", sm: "row" } as const;
// A group's inputs sit below its caption, so a section holding a group aligns each row by its
// bottom edge, which keeps a single field level with the group's inputs. A section without a
// group keeps the top edge, where a Y/N field's helper text hangs below its input.
const GROUP_ROW_STYLE: SxProps<Theme> = { alignItems: "flex-end" };

// The DFHMDF ACCTSID ... ACSPFLG inputs before any detail is fetched: all blank.
const EMPTY_FIELDS: Readonly<AccountScreenFields> = Object.freeze({
    acct_id: "",
    active_status: "",
    open_year: "",
    open_month: "",
    open_day: "",
    credit_limit: "",
    expiration_year: "",
    expiration_month: "",
    expiration_day: "",
    cash_credit_limit: "",
    reissue_year: "",
    reissue_month: "",
    reissue_day: "",
    curr_balance: "",
    curr_cycle_credit: "",
    group_id: "",
    curr_cycle_debit: "",
    cust_id: "",
    ssn_part_1: "",
    ssn_part_2: "",
    ssn_part_3: "",
    dob_year: "",
    dob_month: "",
    dob_day: "",
    fico_credit_score: "",
    first_name: "",
    middle_name: "",
    last_name: "",
    addr_line_1: "",
    addr_state_cd: "",
    addr_line_2: "",
    addr_zip: "",
    addr_line_3: "",
    addr_country_cd: "",
    phone_num_1_area: "",
    phone_num_1_prefix: "",
    phone_num_1_line: "",
    govt_issued_id: "",
    phone_num_2_area: "",
    phone_num_2_prefix: "",
    phone_num_2_line: "",
    eft_account_id: "",
    primary_holder_ind: "",
});

// OPNYEAR/OPNMON/OPNDAY, EXPYEAR..., RISYEAR... and DOBYEAR...: LENGTH 4, 2 and 2.
function DateGroup(groupLabel: string, [year, month, day]: ThreePartNames): FieldGroupDefinition {
    return {
        groupLabel,
        parts: [
            { name: year, label: "Year", maxLength: 4, inputMode: "numeric" },
            { name: month, label: "Month", maxLength: 2, inputMode: "numeric" },
            { name: day, label: "Day", maxLength: 2, inputMode: "numeric" },
        ],
    };
}

// ACSPH1A/ACSPH1B/ACSPH1C and ACSPH2A/ACSPH2B/ACSPH2C: LENGTH 3, 3 and 4.
function PhoneGroup(
    groupLabel: string,
    [area, prefix, line]: ThreePartNames,
): FieldGroupDefinition {
    return {
        groupLabel,
        parts: [
            { name: area, label: "Area code", maxLength: 3 },
            { name: prefix, label: "Prefix code", maxLength: 3 },
            { name: line, label: "Line number code", maxLength: 4 },
        ],
    };
}

// The COACTUP DFHMDF labels (INITIAL trimmed at both ends) and LENGTHs, by screen section.
const FIELD_DEFINITIONS: Readonly<SectionDefinitions> = Object.freeze<SectionDefinitions>({
    accountFields: [
        { name: "acct_id", label: "Account Number :", maxLength: 11 },
        {
            name: "active_status",
            label: "Active Y/N:",
            maxLength: 1,
            helperText: YES_NO_HELPER_TEXT,
        },
        { name: "credit_limit", label: "Credit Limit        :", maxLength: 15 },
        { name: "cash_credit_limit", label: "Cash credit Limit   :", maxLength: 15 },
        { name: "curr_balance", label: "Current Balance     :", maxLength: 15 },
        { name: "curr_cycle_credit", label: "Current Cycle Credit:", maxLength: 15 },
        { name: "curr_cycle_debit", label: "Current Cycle Debit :", maxLength: 15 },
        { name: "group_id", label: "Account Group:", maxLength: 10 },
    ],
    dateFields: [
        DateGroup("Opened :", ["open_year", "open_month", "open_day"]),
        DateGroup("Expiry :", ["expiration_year", "expiration_month", "expiration_day"]),
        DateGroup("Reissue:", ["reissue_year", "reissue_month", "reissue_day"]),
    ],
    customerFields: [
        { name: "cust_id", label: "Customer id  :", maxLength: 9 },
        {
            groupLabel: "SSN:",
            parts: [
                { name: "ssn_part_1", label: "First 3 chars", maxLength: 3 },
                { name: "ssn_part_2", label: "4th & 5th chars", maxLength: 2 },
                { name: "ssn_part_3", label: "Last 4 chars", maxLength: 4 },
            ],
        },
        DateGroup("Date of birth:", ["dob_year", "dob_month", "dob_day"]),
        { name: "fico_credit_score", label: "FICO Score:", maxLength: 3 },
        { name: "first_name", label: "First Name", maxLength: 25 },
        { name: "middle_name", label: "Middle Name:", maxLength: 25 },
        { name: "last_name", label: "Last Name :", maxLength: 25 },
    ],
    addressFields: [
        {
            groupLabel: "Address:",
            parts: [
                { name: "addr_line_1", label: "Line 1", maxLength: 50 },
                { name: "addr_line_2", label: "Line 2", maxLength: 50 },
            ],
        },
        { name: "addr_line_3", label: "City", maxLength: 50 },
        { name: "addr_state_cd", label: "State", maxLength: 2 },
        { name: "addr_zip", label: "Zip", maxLength: 5 },
        { name: "addr_country_cd", label: "Country", maxLength: 3 },
    ],
    phoneFields: [
        PhoneGroup("Phone 1:", ["phone_num_1_area", "phone_num_1_prefix", "phone_num_1_line"]),
        { name: "govt_issued_id", label: "Government Issued Id Ref    :", maxLength: 20 },
        PhoneGroup("Phone 2:", ["phone_num_2_area", "phone_num_2_prefix", "phone_num_2_line"]),
        { name: "eft_account_id", label: "EFT Account Id:", maxLength: 10 },
        {
            name: "primary_holder_ind",
            label: "Primary Card Holder Y/N:",
            maxLength: 1,
            helperText: YES_NO_HELPER_TEXT,
        },
    ],
});

function IsFieldGroup(entry: SectionEntry): entry is FieldGroupDefinition {
    return "parts" in entry;
}

function PartsOf(entry: SectionEntry): readonly ScreenFieldDefinition[] {
    return IsFieldGroup(entry) ? entry.parts : [entry];
}

// Every COACTUP input in the order the page shows it.
const SCREEN_FIELD_NAMES: readonly AccountFieldName[] = Object.values(FIELD_DEFINITIONS)
    .flatMap((entries: readonly SectionEntry[]) => entries.flatMap(PartsOf))
    .map((definition) => definition.name);

// 3320-UNPROTECT-FEW-ATTRS leaves ACSTNUM and ACSCTRY at DFHBMPRF and the key protected.
const DETAIL_PROTECTED_FIELDS: readonly AccountFieldName[] = [
    ACCOUNT_ID_FIELD,
    "cust_id",
    "addr_country_cd",
];
const DETAIL_EDITABLE_FIELDS: readonly AccountFieldName[] = Object.freeze(
    SCREEN_FIELD_NAMES.filter((name) => !DETAIL_PROTECTED_FIELDS.includes(name)),
);
const KEY_EDITABLE_FIELDS: readonly AccountFieldName[] = Object.freeze([ACCOUNT_ID_FIELD]);
const NO_EDITABLE_FIELDS: readonly AccountFieldName[] = Object.freeze([]);

// 3300-SETUP-SCREEN-ATTRS: WHEN OTHER (CHANGES_FAILED) opens the key, as before the fetch.
const EDITABLE_FIELDS_BY_STATE: Readonly<Record<UpdateScreenState, readonly AccountFieldName[]>> =
    Object.freeze({
        [UpdateScreenState.DETAILS_NOT_FETCHED]: KEY_EDITABLE_FIELDS,
        [UpdateScreenState.SHOW_DETAILS]: DETAIL_EDITABLE_FIELDS,
        [UpdateScreenState.CHANGES_NOT_OK]: DETAIL_EDITABLE_FIELDS,
        [UpdateScreenState.CHANGES_OK_NOT_CONFIRMED]: NO_EDITABLE_FIELDS,
        [UpdateScreenState.CHANGES_DONE]: NO_EDITABLE_FIELDS,
        [UpdateScreenState.CHANGES_FAILED]: KEY_EDITABLE_FIELDS,
    });

const INITIAL_MODEL: ScreenModel = {
    state: UpdateScreenState.DETAILS_NOT_FETCHED,
    screenToken: null,
    fields: EMPTY_FIELDS,
    shownFields: EMPTY_FIELDS,
};

// The BMS IC attribute: the cursor starts on ACCTSID.
const INITIAL_FEEDBACK: ScreenFeedbackView = {
    message: "",
    severity: SeverityForOutcome({ isError: false, isCompletedWrite: false }),
    infoMessage: "",
    fieldErrors: [],
    cursorField: ACCOUNT_ID_FIELD,
};

// An entry with ?acctId= shows no information line until its fetch answers.
function EntryFeedback(acctId: string | null): ScreenFeedbackView {
    const infoMessage = acctId === null ? FIRST_ENTRY_INFO_MESSAGE : "";
    return { ...INITIAL_FEEDBACK, infoMessage };
}

function IsAccountFieldName(name: string): name is AccountFieldName {
    return Object.hasOwn(EMPTY_FIELDS, name);
}

function EditableFieldsFor(
    state: UpdateScreenState | null | undefined,
): readonly AccountFieldName[] {
    if (state === null || state === undefined || !Object.hasOwn(EDITABLE_FIELDS_BY_STATE, state)) {
        return KEY_EDITABLE_FIELDS;
    }
    return EDITABLE_FIELDS_BY_STATE[state];
}

// A protected 3270 field returns nothing on RECEIVE MAP; the server takes its value from the
// screen_token, so only the inputs the state leaves editable are sent, their text unaltered.
function PickEditableFields(
    fields: AccountScreenFields,
    state: UpdateScreenState,
): Partial<AccountScreenFields> {
    const picked: Partial<AccountScreenFields> = {};
    for (const name of EditableFieldsFor(state)) {
        picked[name] = fields[name];
    }
    return picked;
}

// "Changes committed to database" arrives as the INFOMSG of a completed save with ERRMSG blank;
// it is shown once, as the green confirmation.
function BuildFeedback(
    source: ScreenFeedback,
    isError: boolean,
    state: UpdateScreenState | null,
): ScreenFeedbackView {
    const message = source.message ?? "";
    const infoMessage = source.info_message ?? "";
    const isCompletedWrite = !isError && state === UpdateScreenState.CHANGES_DONE;
    const showsInfoAsConfirmation = isCompletedWrite && message.trim() === "";
    return {
        message: showsInfoAsConfirmation ? infoMessage : message,
        severity: SeverityForOutcome({ isError, isCompletedWrite }),
        infoMessage: showsInfoAsConfirmation ? "" : infoMessage,
        fieldErrors: source.field_errors ?? [],
        cursorField: source.cursor_field ?? "",
    };
}

function ApplyScreen(screen: AccountScreen, setters: ScreenSetters): void {
    const shownFields = { ...EMPTY_FIELDS, ...screen.fields };
    setters.setModel({
        state: screen.state,
        screenToken: screen.screen_token,
        fields: shownFields,
        shownFields,
    });
    setters.setFeedback(BuildFeedback(screen, false, screen.state));
}

// A body without fields sent no map, so the keyed and the shown fields both stay.
function AdoptErrorScreen(model: ScreenModel, body: AccountErrorBody): ScreenModel {
    const shownFields =
        body.fields === undefined ? model.shownFields : { ...EMPTY_FIELDS, ...body.fields };
    return {
        state: body.state ?? model.state,
        screenToken: body.screen_token ?? model.screenToken,
        fields: body.fields === undefined ? model.fields : shownFields,
        shownFields,
    };
}

// A read its owner aborted shows nothing; a PUT carries no signal, so no save ends here. Only an
// axios error is a legacy outcome; anything else is a defect and propagates.
function ApplyErrorBody(error: unknown, setters: ScreenSetters): void {
    if (IsCancelledRequest(error)) {
        return;
    }
    if (!axios.isAxiosError(error)) {
        throw error;
    }
    const body = LegacyErrorBody<AccountErrorBody>(error);
    if (body !== null) {
        setters.setModel((model) => AdoptErrorScreen(model, body));
    }
    setters.setFeedback(BuildFeedback(ToScreenFeedback(error), true, body?.state ?? null));
}

async function RunAccountRequest(request: AccountRequest, setters: ScreenSetters): Promise<void> {
    try {
        ApplyScreen(await request(), setters);
    } catch (error) {
        ApplyErrorBody(error, setters);
    }
}

// A refusal message carried across the navigation shows, beside the entry's information line,
// until the first response replaces both.
function ShowPendingMessage(setters: ScreenSetters, acctId: string | null): void {
    const pendingMessage = TakePendingMessage();
    if (pendingMessage === "") {
        return;
    }
    setters.setFeedback({
        ...EntryFeedback(acctId),
        message: pendingMessage,
        severity: SeverityForOutcome({ isError: true, isCompletedWrite: false }),
    });
}

// Once details are fetched, F12 is the COACTUPC 2000-DECIDE-ACTION PFK12 re-read, which discards
// the keyed edits, so its key and fields are those the server last showed, and an edit the
// request model rejects never blocks it. Before the fetch, NormaliseAction processes F12 as ENTER
// on the typed key, so it sends the fields as keyed, as ENTER and F5 always do.
function FieldsSentBy(action: UpdateAction, model: ScreenModel): AccountScreenFields {
    const isDiscardingCancel =
        action === UpdateAction.CANCEL && model.state !== UpdateScreenState.DETAILS_NOT_FETCHED;
    return isDiscardingCancel ? model.shownFields : model.fields;
}

// ENTER, F5 and F12 on COACTUP: the server's state machine decides what each key does.
function HandleAction(action: UpdateAction, context: ActionContext): void {
    const { model, runRequest } = context;
    const sentFields = FieldsSentBy(action, model);
    const accountKey = sentFields.acct_id || BLANK_ACCOUNT_PATH;
    const request: AccountUpdateRequest = {
        action,
        screen_token: model.screenToken ?? "",
        fields: PickEditableFields(sentFields, model.state),
    };
    runRequest(() => UpdateAccount(accountKey, request));
}

// The FKEYS legend ENTER=Process F3=Exit, with F5=Save and F12=Cancel offered in every state: the
// server processes a save or cancel its state does not allow as ENTER. ENTER, F5 and F12 each
// send a request, so they are disabled while one is in flight; F3 sends none and stays enabled.
function BuildFunctionKeys(handlers: KeyHandlers, isBusy: boolean): FunctionKeyDefinition[] {
    const requestKey = (key: PfKey, label: string, action: UpdateAction) => ({
        key,
        label,
        onPress: () => handlers.onAction(action),
        disabled: isBusy,
    });
    return [
        requestKey("ENTER", "Process", UpdateAction.ENTER),
        { key: "F3", label: "Exit", onPress: handlers.onExit },
        requestKey("F5", "Save", UpdateAction.SAVE),
        requestKey("F12", "Cancel", UpdateAction.CANCEL),
    ];
}

// The MOVE -1 TO <field>L cursor: the server's cursor_field, else the first editable input.
function FocusCursorField({ inputs, cursorField, state }: FocusRequest): void {
    const targetName = IsAccountFieldName(cursorField) ? cursorField : EditableFieldsFor(state)[0];
    if (targetName === undefined) {
        return;
    }
    inputs[targetName]?.focus();
}

function WithFieldValue(model: ScreenModel, name: AccountFieldName, value: string): ScreenModel {
    return { ...model, fields: { ...model.fields, [name]: value } };
}

// A request holds the screen until it settles, unless it is a read its owner has aborted.
function HoldsScreen(inFlight: InFlightRequest | null): boolean {
    return inFlight !== null && inFlight.signal?.aborted !== true;
}

// An aborted read ends the busy state as a transition, so the F3 task that aborted it ends at
// once and the browser cancels the request without waiting for a re-render.
function EndBusyState(inFlight: InFlightRequest, setters: ScreenSetters): void {
    if (inFlight.signal?.aborted === true) {
        startTransition(() => setters.setIsBusy(false));
        return;
    }
    setters.setIsBusy(false);
}

// React recognises hooks only by the camelCase "use" prefix, so the hook names below are a
// framework-mandated exception to the MK_Modernization Rule's PascalCase function naming.
function useScreenStore(acctId: string | null): ScreenStore {
    const [model, setModel] = useState<ScreenModel>(INITIAL_MODEL);
    const [feedback, setFeedback] = useState<ScreenFeedbackView>(() => EntryFeedback(acctId));
    const [isBusy, setIsBusy] = useState(false);
    const setters = useMemo(() => ({ setModel, setFeedback, setIsBusy }), []);
    return { model, feedback, isBusy, setters };
}

// One request at a time, as one CICS task per key press: a key pressed while a request is in
// flight is ignored. An aborted read gives the screen to the next request, and only the request
// in flight ends the busy state.
function useRequestRunner(setters: ScreenSetters): RequestRunner {
    const inFlightRef = useRef<InFlightRequest | null>(null);
    return useCallback(
        (request: AccountRequest, signal?: AbortSignal) => {
            if (HoldsScreen(inFlightRef.current)) {
                return;
            }
            const inFlight: InFlightRequest = { signal };
            inFlightRef.current = inFlight;
            setters.setIsBusy(true);
            void RunAccountRequest(request, setters).finally(() => {
                if (inFlightRef.current === inFlight) {
                    inFlightRef.current = null;
                    EndBusyState(inFlight, setters);
                }
            });
        },
        [setters],
    );
}

// Entry from the menu shows the empty key screen and its prompt; entry with ?acctId= fetches that
// account. The fetch is a read: leaving the page or a new ?acctId= aborts it, so Strict Mode's
// repeated mount effect fetches again. The returned function aborts it for F3, as the page stays
// mounted until the next route has loaded.
function useEntryRequest(acctId: string | null, runRequest: RequestRunner, setters: ScreenSetters) {
    const controllerRef = useRef<AbortController | null>(null);
    useEffect(() => {
        ShowPendingMessage(setters, acctId);
        const controller = new AbortController();
        controllerRef.current = controller;
        if (acctId !== null) {
            const { signal } = controller;
            runRequest(() => FetchAccountForEdit(acctId, { signal }), signal);
        }
        return () => controller.abort();
    }, [acctId, runRequest, setters]);
    return useCallback(() => controllerRef.current?.abort(), []);
}

// Every response moves the cursor, a repeated cursor field included.
function useCursorFocus(store: ScreenStore, inputRefs: RefObject<InputRefMap>) {
    const { feedback, model } = store;
    useEffect(() => {
        FocusCursorField({
            inputs: inputRefs.current,
            cursorField: feedback.cursorField,
            state: model.state,
        });
    }, [feedback, model.state, inputRefs]);
}

// F3 aborts an entry fetch still in flight, then leaves; the aborted read stops holding the
// screen at once (HoldsScreen). A PUT carries no signal, so a save in flight is never aborted.
function useFunctionKeyDefinitions(
    store: ScreenStore,
    runRequest: RequestRunner,
    abortEntryFetch: () => void,
) {
    const router = useRouter();
    const { model, isBusy } = store;
    const onAction = useCallback(
        (action: UpdateAction) => HandleAction(action, { model, runRequest }),
        [model, runRequest],
    );
    const onExit = useCallback(() => {
        abortEntryFetch();
        router.push(MENU_ROUTE);
    }, [abortEntryFetch, router]);
    return useMemo(
        () => BuildFunctionKeys({ onAction, onExit }, isBusy),
        [onAction, onExit, isBusy],
    );
}

// Each input registers its element, so a response can move the cursor to it.
function useInputRegistry(): InputRegistry {
    const inputRefs = useRef<InputRefMap>({});
    const registerInput = useCallback(
        (name: AccountFieldName, element: HTMLInputElement | null) => {
            inputRefs.current[name] = element;
        },
        [],
    );
    return { inputRefs, RegisterInput: registerInput };
}

function useFieldView(store: ScreenStore, registry: InputRegistry): FieldView {
    const { model, feedback, setters } = store;
    const onFieldChange = useCallback(
        (name: AccountFieldName, value: string) =>
            setters.setModel((current) => WithFieldValue(current, name, value)),
        [setters],
    );
    return useMemo(
        () => ({
            fields: model.fields,
            editableFields: new Set(EditableFieldsFor(model.state)),
            fieldErrors: new Set(feedback.fieldErrors),
            RegisterInput: registry.RegisterInput,
            onFieldChange,
        }),
        [model, feedback, registry.RegisterInput, onFieldChange],
    );
}

function useAccountUpdateScreen(): AccountUpdateScreenModel {
    const acctId = useSearchParams().get(ACCOUNT_ID_QUERY_KEY);
    const store = useScreenStore(acctId);
    const runRequest = useRequestRunner(store.setters);
    const registry = useInputRegistry();
    const abortEntryFetch = useEntryRequest(acctId, runRequest, store.setters);
    useCursorFocus(store, registry.inputRefs);
    const keys = useFunctionKeyDefinitions(store, runRequest, abortEntryFetch);
    const view = useFieldView(store, registry);
    return { view, keys, isBusy: store.isBusy, feedback: store.feedback };
}

function FieldIdOf(name: AccountFieldName): string {
    return FIELD_ID_PREFIX + name;
}

// The attributes a field takes from its BMS definition alone.
function StaticFieldProps(
    definition: ScreenFieldDefinition,
): Pick<TextFieldProps, "id" | "label" | "helperText"> {
    return {
        id: FieldIdOf(definition.name),
        label: definition.label,
        helperText: definition.helperText,
    };
}

// The BMS UNPROT/ASKIP attribute: a protected field stays focusable, but cannot be changed, and
// its label stays raised, so a blank protected value shows as an empty field under it.
// While the server names the field in error, its input is tied to the ERRMSG line ahead of the
// field's hint.
function FieldSlotProps(
    definition: ScreenFieldDefinition,
    isEditable: boolean,
    hasError: boolean,
): TextFieldProps["slotProps"] {
    const helperTextId = HelperTextIdOf(FieldIdOf(definition.name), definition.helperText);
    return {
        htmlInput: {
            maxLength: definition.maxLength,
            inputMode: definition.inputMode,
            ...ErrorMessageReference(hasError, helperTextId),
        },
        input: { readOnly: !isEditable },
        inputLabel: isEditable ? {} : { shrink: true },
    };
}

// The value is kept exactly as keyed: COACTUPC's edits run on the server, in source order.
// The key remounts the field when its protection changes, because an input replaced while it
// has focus never blurs, and MUI would keep showing the old field as focused.
function ScreenField({ definition, view }: ScreenFieldProps) {
    const isEditable = view.editableFields.has(definition.name);
    const hasError = view.fieldErrors.has(definition.name);
    return (
        <TextField
            key={isEditable ? EDITABLE_FIELD_KEY : PROTECTED_FIELD_KEY}
            {...StaticFieldProps(definition)}
            fullWidth
            value={view.fields[definition.name]}
            variant={isEditable ? "outlined" : "filled"}
            error={hasError}
            inputRef={(element: HTMLInputElement | null) =>
                view.RegisterInput(definition.name, element)
            }
            onChange={(event) => view.onFieldChange(definition.name, event.target.value)}
            slotProps={FieldSlotProps(definition, isEditable, hasError)}
        />
    );
}

// One map field keyed in parts (a date, the SSN, a phone number or the address lines).
function FieldGroup({ group, view }: FieldGroupProps) {
    return (
        <Stack spacing={CAPTION_SPACING}>
            <Typography component="p" variant="labelLarge">
                {group.groupLabel}
            </Typography>
            <Stack
                direction={GROUP_DIRECTION}
                spacing={FIELD_SPACING}
                role="group"
                aria-label={group.groupLabel}
            >
                {group.parts.map((part) => (
                    <ScreenField key={part.name} definition={part} view={view} />
                ))}
            </Stack>
        </Stack>
    );
}

function SectionEntryField({ entry, view }: SectionEntryProps) {
    if (IsFieldGroup(entry)) {
        return <FieldGroup group={entry} view={view} />;
    }
    return <ScreenField definition={entry} view={view} />;
}

function EntryKey(entry: SectionEntry): string {
    return IsFieldGroup(entry) ? entry.groupLabel : entry.name;
}

function FieldSection({ entries, view }: FieldSectionProps) {
    const rowStyle = entries.some(IsFieldGroup) ? GROUP_ROW_STYLE : undefined;
    return (
        <Grid container spacing={FIELD_SPACING} sx={rowStyle}>
            {entries.map((entry) => (
                <Grid key={EntryKey(entry)} size={ENTRY_GRID_SIZE}>
                    <SectionEntryField entry={entry} view={view} />
                </Grid>
            ))}
        </Grid>
    );
}

function AccountFields({ view }: SectionProps) {
    return <FieldSection entries={FIELD_DEFINITIONS.accountFields} view={view} />;
}

function DateFields({ view }: SectionProps) {
    return <FieldSection entries={FIELD_DEFINITIONS.dateFields} view={view} />;
}

function CustomerFields({ view }: SectionProps) {
    return (
        <Stack spacing={CAPTION_SPACING}>
            <Typography component="h2" variant="titleMedium">
                {CUSTOMER_DETAILS_HEADING}
            </Typography>
            <FieldSection entries={FIELD_DEFINITIONS.customerFields} view={view} />
        </Stack>
    );
}

function AddressFields({ view }: SectionProps) {
    return <FieldSection entries={FIELD_DEFINITIONS.addressFields} view={view} />;
}

function PhoneFields({ view }: SectionProps) {
    return <FieldSection entries={FIELD_DEFINITIONS.phoneFields} view={view} />;
}

// COACTUP rows 5 to 20, top to bottom.
function AccountDetailSections({ view }: SectionProps) {
    return (
        <>
            <AccountFields view={view} />
            <DateFields view={view} />
            <CustomerFields view={view} />
            <AddressFields view={view} />
            <PhoneFields view={view} />
        </>
    );
}

// The ERRMSG and INFOMSG lines. Every response sets a new feedback object, so its identity is
// the outcome: ENTER answered again with the same literal still brings the line into view.
function AccountMessageLine({ feedback }: AccountMessageLineProps) {
    return (
        <MessageLine
            message={feedback.message}
            severity={feedback.severity}
            infoMessage={feedback.infoMessage}
            outcomeKey={feedback}
        />
    );
}

function AccountUpdateScreen() {
    const { view, feedback, isBusy, keys } = useAccountUpdateScreen();
    useFunctionKeys(keys);
    return (
        <ScreenShell
            transactionId={TRANSACTION_ID}
            programName={PROGRAM_NAME}
            screenTitle={SCREEN_TITLE}
            isBusy={isBusy}
        >
            <AccountDetailSections view={view} />
            <AccountMessageLine feedback={feedback} />
            <FunctionKeyBar keys={keys} />
        </ScreenShell>
    );
}

// Each arrival with another ?acctId=, or with none, is a fresh COACTUPC first entry, as every
// XCTL is, so the fields, state and screen_token start over together with their own request
// runner. The replaced screen's unmount aborts its entry fetch; a PUT it already sent runs to its
// answer, which reaches only that screen. A missing acctId stays distinct from an empty one,
// because only the latter is fetched.
function AccountUpdateEntry() {
    const acctId = useSearchParams().get(ACCOUNT_ID_QUERY_KEY);
    return <AccountUpdateScreen key={JSON.stringify([acctId])} />;
}

// useSearchParams needs a Suspense boundary for the static prerender of npm run build.
export default function AccountUpdatePage() {
    return (
        <Suspense fallback={null}>
            <AccountUpdateEntry />
        </Suspense>
    );
}
