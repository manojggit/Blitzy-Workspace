// Replaces: BMS map COCRDUP, program COCRDUPC (CCUP); copybooks COCRDUP (cpy-bms), CVACT02Y,
// CVCUS01Y, CVCRD01Y, CSMSG02Y, CSUSR01Y, COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
"use client";

import {
    FormControl,
    FormLabel,
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
    useCallback,
    useEffect,
    useMemo,
    useRef,
    useState,
    type Dispatch,
    type ReactNode,
    type RefObject,
    type SetStateAction,
} from "react";

import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import { MessageLine } from "@/components/MessageLine";
import { ScreenShell } from "@/components/ScreenShell";
import {
    FetchCardForEdit,
    UpdateCard,
    type CardScreen,
    type CardScreenFields,
    type CardUpdateRequest,
} from "@/lib/api/cards";
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

const TRANSACTION_ID = "CCUP";
const PROGRAM_NAME = "COCRDUPC";
// The BMS INITIAL texts of COCRDUP, verbatim.
const SCREEN_TITLE = "Update Credit Card Details";
const EXPIRY_DATE_LABEL = "Expiry Date       :";
const EXPIRY_SEPARATOR = "/";
const MENU_ROUTE = "/menu";
const CARD_REF_QUERY_KEY = "cardRef";
const ACCT_ID_QUERY_KEY = "acctId";
const YES_NO_HELPER_TEXT = "Y/N";

// The PF-key legend "ENTER=Process F3=Exit" (FKEYS) and "F5=Save F12=Cancel" (FKEYSC).
const ENTER_LABEL = "Process";
const EXIT_LABEL = "Exit";
const SAVE_LABEL = "Save";
const CANCEL_LABEL = "Cancel";

const ENTRY_GRID_SIZE = { xs: 12, md: 6 } as const;
// Theme spacing units: room between fields for an outlined field's raised label.
const FIELD_SPACING = 4;
const EXPIRY_PART_SPACING = 2;
const LEGEND_SPACING = 3;
const EDITABLE_FIELD_KEY = "editable";
const PROTECTED_FIELD_KEY = "protected";
const EXPIRY_ROW_STYLE: SxProps<Theme> = {
    alignItems: "center",
    paddingBlockStart: LEGEND_SPACING,
};

type CardFieldName = keyof CardScreenFields;

// EXPDAY is dark and protected on the map: it is never rendered and never sent, so the server
// keeps the before-image day.
type RenderedFieldName = Exclude<CardFieldName, "expiry_day">;

interface CardFieldDefinition {
    name: RenderedFieldName;
    label: string;
    maxLength: number;
    helperText?: string;
    inputMode?: "numeric";
}

const ACCT_ID_FIELD: CardFieldName = "acct_id";
const CARD_NUM_FIELD: CardFieldName = "card_num";

// The COCRDUP DFHMDF labels (INITIAL, trailing blank dropped) and LENGTHs.
const CARD_UPDATE_FIELDS: Readonly<Record<RenderedFieldName, CardFieldDefinition>> = Object.freeze({
    acct_id: { name: "acct_id", label: "Account Number    :", maxLength: 11 },
    card_num: { name: "card_num", label: "Card Number       :", maxLength: 16 },
    embossed_name: { name: "embossed_name", label: "Name on card      :", maxLength: 50 },
    active_status: {
        name: "active_status",
        label: "Card Active Y/N   :",
        maxLength: 1,
        helperText: YES_NO_HELPER_TEXT,
    },
    expiry_month: {
        name: "expiry_month",
        label: "Expiry Month",
        maxLength: 2,
        inputMode: "numeric",
    },
    expiry_year: { name: "expiry_year", label: "Expiry Year", maxLength: 4, inputMode: "numeric" },
});

// ACCTSID and CARDSID (rows 7 and 8); CRDNAME and CRDSTCD (rows 11 and 13); EXPMON / EXPYEAR.
const KEY_FIELDS: readonly CardFieldDefinition[] = [
    CARD_UPDATE_FIELDS.acct_id,
    CARD_UPDATE_FIELDS.card_num,
];
const DETAIL_TEXT_FIELDS: readonly CardFieldDefinition[] = [
    CARD_UPDATE_FIELDS.embossed_name,
    CARD_UPDATE_FIELDS.active_status,
];
const EXPIRY_FIELDS: readonly CardFieldDefinition[] = [
    CARD_UPDATE_FIELDS.expiry_month,
    CARD_UPDATE_FIELDS.expiry_year,
];

const EMPTY_CARD_FIELDS: Readonly<CardScreenFields> = Object.freeze({
    acct_id: "",
    card_num: "",
    embossed_name: "",
    active_status: "",
    expiry_month: "",
    expiry_year: "",
    expiry_day: "",
});

const KEY_FIELD_NAMES: readonly CardFieldName[] = Object.freeze(
    KEY_FIELDS.map((definition) => definition.name),
);
const DETAIL_FIELD_NAMES: readonly CardFieldName[] = Object.freeze(
    [...DETAIL_TEXT_FIELDS, ...EXPIRY_FIELDS].map((definition) => definition.name),
);
const NO_FIELD_NAMES: readonly CardFieldName[] = Object.freeze([]);

// 3300-SETUP-SCREEN-ATTRS: WHEN OTHER (CHANGES_FAILED) opens the keys, as before the fetch.
const EDITABLE_FIELDS_BY_STATE: Readonly<Record<UpdateScreenState, readonly CardFieldName[]>> =
    Object.freeze({
        [UpdateScreenState.DETAILS_NOT_FETCHED]: KEY_FIELD_NAMES,
        [UpdateScreenState.SHOW_DETAILS]: DETAIL_FIELD_NAMES,
        [UpdateScreenState.CHANGES_NOT_OK]: DETAIL_FIELD_NAMES,
        [UpdateScreenState.CHANGES_OK_NOT_CONFIRMED]: NO_FIELD_NAMES,
        [UpdateScreenState.CHANGES_DONE]: NO_FIELD_NAMES,
        [UpdateScreenState.CHANGES_FAILED]: KEY_FIELD_NAMES,
    });

// What the COMMAREA kept between two CCUP tasks: the CCUP-CHANGE-ACTION state, the encrypted
// screen_token and card_ref the server sends back unchanged, and the map fields as last shown
// or keyed. It lives in React state only; neither token is ever stored, decoded or logged.
interface CardUpdateModel {
    state: UpdateScreenState;
    screenToken: string;
    cardRef: string | null;
    fields: CardScreenFields;
}

// The query a card list selection opens the page with (COCOM01Y CDEMO-ACCT-ID and the card,
// as an opaque card_ref token); the screen is keyed by it, so it describes the caller for the
// lifetime of each screen.
interface EntryKeys {
    cardRef: string | null;
    acctId: string;
}

// The ERRMSG and INFOMSG lines, the failing fields and the cursor of the last response.
interface ScreenFeedbackView {
    message: string;
    severity: MessageSeverity;
    infoMessage: string;
    fieldErrors: readonly string[];
    cursorField: string;
}

interface FeedbackOutcome {
    isError: boolean;
    state: UpdateScreenState | null;
}

interface ScreenSetters {
    setModel: Dispatch<SetStateAction<CardUpdateModel>>;
    setFeedback: Dispatch<SetStateAction<ScreenFeedbackView>>;
    setIsBusy: Dispatch<SetStateAction<boolean>>;
}

interface ScreenStore {
    model: CardUpdateModel;
    feedback: ScreenFeedbackView;
    isBusy: boolean;
    setters: ScreenSetters;
}

// The 4xx and 5xx CCUP bodies carry the screen beside the standard legacy keys.
type CardErrorBody = LegacyErrorResponse &
    Partial<Pick<CardScreen, "fields" | "state" | "screen_token">>;

type CardRequest = () => Promise<CardScreen>;

// signal is given only with a read, by the AbortController that owns it; a write never has one,
// so a save, an ENTER or a cancel is never abandoned once sent.
type RequestRunner = (request: CardRequest, signal?: AbortSignal) => void;

// The request that holds the screen while it runs, or null when a key may start one.
type BusyHolderRef = RefObject<object | null>;

type InputRefMap = Partial<Record<CardFieldName, HTMLInputElement | null>>;

type InputRefCallback = (element: HTMLInputElement | null) => void;

type InputRegistration = (name: CardFieldName, element: HTMLInputElement | null) => void;

interface FieldChange {
    name: CardFieldName;
    value: string;
}

type FieldChangeHandler = (change: FieldChange) => void;

interface ActionContext {
    model: CardUpdateModel;
    runRequest: RequestRunner;
}

interface KeyInputs {
    model: CardUpdateModel;
    isBusy: boolean;
    runRequest: RequestRunner;
    AbandonEntryFetch: () => void;
}

interface KeyHandlers {
    onAction: (action: UpdateAction) => void;
    onExit: () => void;
}

interface InputRegistry {
    inputRefs: RefObject<InputRefMap>;
    registerInput: InputRegistration;
}

interface FieldView {
    fields: CardScreenFields;
    state: UpdateScreenState;
    fieldErrors: readonly string[];
    registerInput: InputRegistration;
    changeField: FieldChangeHandler;
}

interface CardUpdateScreenModel {
    view: FieldView;
    feedback: ScreenFeedbackView;
    isBusy: boolean;
    keys: readonly FunctionKeyDefinition[];
}

interface CardTextFieldProps {
    definition: CardFieldDefinition;
    value: string;
    readOnly: boolean;
    error: boolean;
    inputRef: InputRefCallback;
    onChange: (value: string) => void;
}

interface FieldSectionProps {
    view: FieldView;
}

interface FieldGridProps {
    definitions: readonly CardFieldDefinition[];
    view: FieldView;
    children?: ReactNode;
}

// The BMS IC attribute: the cursor starts on ACCTSID.
const INITIAL_FEEDBACK: ScreenFeedbackView = {
    message: "",
    severity: SeverityForOutcome({ isError: false, isCompletedWrite: false }),
    infoMessage: "",
    fieldErrors: [],
    cursorField: ACCT_ID_FIELD,
};

// A pending message is the 403 refusal literal another page stored; it shows as an error.
const PENDING_MESSAGE_SEVERITY = SeverityForOutcome({ isError: true, isCompletedWrite: false });

function IsCardFieldName(name: string): name is CardFieldName {
    return Object.hasOwn(EMPTY_CARD_FIELDS, name);
}

function EditableFieldsFor(state: UpdateScreenState): readonly CardFieldName[] {
    if (!Object.hasOwn(EDITABLE_FIELDS_BY_STATE, state)) {
        return KEY_FIELD_NAMES;
    }
    return EDITABLE_FIELDS_BY_STATE[state];
}

function IsEditable(state: UpdateScreenState, name: CardFieldName): boolean {
    return EditableFieldsFor(state).includes(name);
}

// A protected 3270 field returns nothing on RECEIVE MAP; the server takes its value from the
// screen_token, so only the inputs the state leaves editable are sent, exactly as keyed: the
// JUSTIFY=(RIGHT) of EXPMON and EXPYEAR and every edit run on the server.
function BuildEditableFields(
    state: UpdateScreenState,
    fields: CardScreenFields,
): Partial<CardScreenFields> {
    const editableFields: Partial<CardScreenFields> = {};
    for (const name of EditableFieldsFor(state)) {
        editableFields[name] = fields[name];
    }
    return editableFields;
}

// Responses mask CARDSID, so a key edit or not-found answer during key entry keeps the keys the
// user typed. Every other response shows the server's fields, among them the empty key screen
// that discards the input after a done or failed change.
function MergeScreenFields(
    previous: CardUpdateModel,
    nextState: UpdateScreenState,
    screenFields: CardScreenFields | undefined,
): CardScreenFields {
    if (screenFields === undefined) {
        return previous.fields;
    }
    const fields = { ...EMPTY_CARD_FIELDS, ...screenFields };
    const keepsTypedKeys =
        previous.state === UpdateScreenState.DETAILS_NOT_FETCHED &&
        nextState === UpdateScreenState.DETAILS_NOT_FETCHED;
    if (!keepsTypedKeys) {
        return fields;
    }
    return { ...fields, acct_id: previous.fields.acct_id, card_num: previous.fields.card_num };
}

// The server sends an empty card_ref once no card is held, as after the reset that follows a
// completed or failed change; a body without the key keeps the token already held.
function CardRefOf(previous: CardUpdateModel, screen: Partial<CardScreen>): string | null {
    if (!("card_ref" in screen)) {
        return previous.cardRef;
    }
    return screen.card_ref ? screen.card_ref : null;
}

function ApplyCardScreen(previous: CardUpdateModel, screen: Partial<CardScreen>): CardUpdateModel {
    const state = screen.state ?? previous.state;
    return {
        state,
        screenToken: screen.screen_token ?? previous.screenToken,
        cardRef: CardRefOf(previous, screen),
        fields: MergeScreenFields(previous, state, screen.fields),
    };
}

// The {cardNum} path segment: the card_ref token while one is held, else CARDSID as typed.
// UpdateCard sends an empty segment as one space, which the key edits answer with
// "Card number not provided".
function ResolveCardKey(model: CardUpdateModel): string {
    return model.cardRef ?? model.fields.card_num;
}

// Typing a card number while the keys are editable replaces the card the list handed over.
function WithFieldValue(model: CardUpdateModel, change: FieldChange): CardUpdateModel {
    const fields = { ...model.fields, [change.name]: change.value };
    const replacesCard = change.name === CARD_NUM_FIELD && IsEditable(model.state, CARD_NUM_FIELD);
    return { ...model, fields, cardRef: replacesCard ? null : model.cardRef };
}

// The account the list handed over (CDEMO-ACCT-ID) shows on ACCTSID from the first render; the
// card number never reaches the page, only its card_ref token.
function InitialModel(entry: EntryKeys): CardUpdateModel {
    return {
        state: UpdateScreenState.DETAILS_NOT_FETCHED,
        screenToken: "",
        cardRef: entry.cardRef,
        fields: { ...EMPTY_CARD_FIELDS, acct_id: entry.acctId },
    };
}

// "Changes committed to database" arrives as the INFOMSG of a completed save with ERRMSG blank;
// it is shown once, as the green confirmation of the write.
function BuildFeedback(source: ScreenFeedback, outcome: FeedbackOutcome): ScreenFeedbackView {
    const message = source.message ?? "";
    const infoMessage = source.info_message ?? "";
    const isCompletedWrite = !outcome.isError && outcome.state === UpdateScreenState.CHANGES_DONE;
    const showsInfoAsConfirmation = isCompletedWrite && message.trim() === "";
    return {
        message: showsInfoAsConfirmation ? infoMessage : message,
        severity: SeverityForOutcome({ isError: outcome.isError, isCompletedWrite }),
        infoMessage: showsInfoAsConfirmation ? "" : infoMessage,
        fieldErrors: source.field_errors ?? [],
        cursorField: source.cursor_field ?? "",
    };
}

function ApplyScreenResponse(screen: CardScreen, setters: ScreenSetters): void {
    setters.setModel((model) => ApplyCardScreen(model, screen));
    setters.setFeedback(BuildFeedback(screen, { isError: false, state: screen.state }));
}

// An abandoned read (axios's CanceledError) is no legacy outcome and is discarded silently. Only an
// axios error is a legacy outcome; anything else is a defect and propagates. A 500, a network
// failure or a non-legacy body keeps the screen and shows the abend text.
function ApplyErrorResponse(error: unknown, setters: ScreenSetters): void {
    if (IsCancelledRequest(error)) {
        return;
    }
    if (!axios.isAxiosError(error)) {
        throw error;
    }
    const body = LegacyErrorBody<CardErrorBody>(error);
    if (body !== null) {
        setters.setModel((model) => ApplyCardScreen(model, body));
    }
    const outcome = { isError: true, state: body?.state ?? null };
    setters.setFeedback(BuildFeedback(ToScreenFeedback(error), outcome));
}

async function RunCardRequest(request: CardRequest, setters: ScreenSetters): Promise<void> {
    try {
        ApplyScreenResponse(await request(), setters);
    } catch (error) {
        ApplyErrorResponse(error, setters);
    }
}

function ShowPendingMessage(setters: ScreenSetters): void {
    const pendingMessage = TakePendingMessage();
    if (pendingMessage === "") {
        return;
    }
    setters.setFeedback({
        ...INITIAL_FEEDBACK,
        message: pendingMessage,
        severity: PENDING_MESSAGE_SEVERITY,
    });
}

// ENTER, F5 and F12 on COCRDUP. Every key reaches the server, whose state machine processes a
// save or cancel its state does not allow as ENTER (lines 413-424), and answers any key after a
// done or failed change with the empty key screen, whichever screen opened this one.
function HandleAction(action: UpdateAction, context: ActionContext): void {
    const { model, runRequest } = context;
    const request: CardUpdateRequest = {
        action,
        screen_token: model.screenToken,
        fields: BuildEditableFields(model.state, model.fields),
    };
    runRequest(() => UpdateCard(ResolveCardKey(model), request));
}

// The FKEYS and FKEYSC legend. F5 and F12 are offered in every state rather than dimmed by the
// FKEYSC attribute, because the server owns which key is valid in each state. ENTER, F5 and F12
// each send a request, so they are disabled while one is in flight; F3 only navigates.
function BuildFunctionKeys(handlers: KeyHandlers, isBusy: boolean): FunctionKeyDefinition[] {
    const requestKey = (key: PfKey, label: string, action: UpdateAction) => ({
        key,
        label,
        onPress: () => handlers.onAction(action),
        disabled: isBusy,
    });
    return [
        requestKey("ENTER", ENTER_LABEL, UpdateAction.ENTER),
        { key: "F3", label: EXIT_LABEL, onPress: handlers.onExit },
        requestKey("F5", SAVE_LABEL, UpdateAction.SAVE),
        requestKey("F12", CANCEL_LABEL, UpdateAction.CANCEL),
    ];
}

// Replaces MOVE -1 TO <field>L: the cursor goes to the field the server names, read-only or not.
function FocusCursorField(inputs: InputRefMap, cursorField: string): void {
    if (IsCardFieldName(cursorField)) {
        inputs[cursorField]?.focus();
    }
}

// React recognises hooks only by the camelCase "use" prefix, so the hook names below are a
// framework-mandated exception to the MK_Modernization Rule's PascalCase function naming.
function useEntryKeys(): EntryKeys {
    const searchParams = useSearchParams();
    const cardRef = searchParams.get(CARD_REF_QUERY_KEY) || null;
    const acctId = searchParams.get(ACCT_ID_QUERY_KEY) ?? "";
    return useMemo(() => ({ cardRef, acctId }), [cardRef, acctId]);
}

function useScreenStore(entry: EntryKeys): ScreenStore {
    const [model, setModel] = useState<CardUpdateModel>(() => InitialModel(entry));
    const [feedback, setFeedback] = useState<ScreenFeedbackView>(INITIAL_FEEDBACK);
    const [isBusy, setIsBusy] = useState(false);
    const setters = useMemo(() => ({ setModel, setFeedback, setIsBusy }), []);
    return { model, feedback, isBusy, setters };
}

// A request frees the screen only while it still holds it, so an abandoned read never frees the
// screen for a request that started after it.
function ReleaseBusyHolder(busyRef: BusyHolderRef, holder: object, setters: ScreenSetters): void {
    if (busyRef.current !== holder) {
        return;
    }
    busyRef.current = null;
    setters.setIsBusy(false);
}

// One request at a time, as one CICS task per key press: a key pressed while a request is in
// flight is ignored, so no request ever carries a screen_token the server has moved past. A request
// frees the screen when it ends; an abandoned read frees it at once, so the fetch React's
// development remount starts next is not ignored.
function useRequestRunner(setters: ScreenSetters): RequestRunner {
    const busyHolderRef = useRef<object | null>(null);
    return useCallback(
        (request: CardRequest, signal?: AbortSignal) => {
            if (busyHolderRef.current !== null) {
                return;
            }
            const holder = {};
            busyHolderRef.current = holder;
            setters.setIsBusy(true);
            const release = () => ReleaseBusyHolder(busyHolderRef, holder, setters);
            signal?.addEventListener("abort", release, { once: true });
            void RunCardRequest(request, setters).finally(release);
        },
        [setters],
    );
}

// Lines 482-497: a card list selection fetches the card at once. Entry from the menu shows the
// empty key screen and calls nothing. The fetch is a read with its own AbortController: leaving
// the page abandons it, and React's development remount abandons the first fetch and fetches
// again. A pending message is taken once, so the remount finds none. The answer abandons the
// fetch on F3, as the page stays mounted until the next route has loaded.
function useEntryFetch(
    entry: EntryKeys,
    runRequest: RequestRunner,
    setters: ScreenSetters,
): () => void {
    const entryFetchControllerRef = useRef<AbortController | null>(null);
    useEffect(() => {
        ShowPendingMessage(setters);
        const { cardRef, acctId } = entry;
        if (cardRef === null) {
            return undefined;
        }
        const entryFetchController = new AbortController();
        entryFetchControllerRef.current = entryFetchController;
        const { signal } = entryFetchController;
        runRequest(() => FetchCardForEdit(cardRef, acctId, { signal }), signal);
        return () => entryFetchController.abort();
    }, [entry, runRequest, setters]);
    return useCallback(() => entryFetchControllerRef.current?.abort(), []);
}

// Every response moves the cursor, a repeated cursor field included, so the effect follows the
// feedback object rather than the field name.
function useCursorFocus(feedback: ScreenFeedbackView, inputRefs: RefObject<InputRefMap>): void {
    useEffect(() => {
        FocusCursorField(inputRefs.current, feedback.cursorField);
    }, [feedback, inputRefs]);
}

// PF3 returns to the main menu, whether the menu or the card list opened the page. It abandons
// an entry fetch still in flight; a write in flight is never abandoned.
function useScreenFunctionKeys(inputs: KeyInputs): readonly FunctionKeyDefinition[] {
    const router = useRouter();
    const { model, isBusy, runRequest, AbandonEntryFetch } = inputs;
    const onAction = useCallback(
        (action: UpdateAction) => HandleAction(action, { model, runRequest }),
        [model, runRequest],
    );
    const onExit = useCallback(() => {
        AbandonEntryFetch();
        router.push(MENU_ROUTE);
    }, [AbandonEntryFetch, router]);
    return useMemo(
        () => BuildFunctionKeys({ onAction, onExit }, isBusy),
        [onAction, onExit, isBusy],
    );
}

// Each input registers its element, so a response can move the cursor to it.
function useInputRegistry(): InputRegistry {
    const inputRefs = useRef<InputRefMap>({});
    const registerInput = useCallback((name: CardFieldName, element: HTMLInputElement | null) => {
        inputRefs.current[name] = element;
    }, []);
    return { inputRefs, registerInput };
}

function useFieldView(store: ScreenStore, registerInput: InputRegistration): FieldView {
    const { model, feedback, setters } = store;
    const changeField = useCallback(
        (change: FieldChange) => setters.setModel((current) => WithFieldValue(current, change)),
        [setters],
    );
    return useMemo(
        () => ({
            fields: model.fields,
            state: model.state,
            fieldErrors: feedback.fieldErrors,
            registerInput,
            changeField,
        }),
        [model, feedback, registerInput, changeField],
    );
}

function useCardUpdateScreen(): CardUpdateScreenModel {
    const entry = useEntryKeys();
    const store = useScreenStore(entry);
    const runRequest = useRequestRunner(store.setters);
    const registry = useInputRegistry();
    const abandonEntryFetch = useEntryFetch(entry, runRequest, store.setters);
    useCursorFocus(store.feedback, registry.inputRefs);
    const keys = useScreenFunctionKeys({
        model: store.model,
        isBusy: store.isBusy,
        runRequest,
        AbandonEntryFetch: abandonEntryFetch,
    });
    useFunctionKeys(keys);
    const view = useFieldView(store, registry.registerInput);
    return { view, keys, isBusy: store.isBusy, feedback: store.feedback };
}

// The BMS UNPROT/PROT attribute: a protected field stays focusable, but cannot be changed.
function FieldSlotProps(
    definition: CardFieldDefinition,
    readOnly: boolean,
): TextFieldProps["slotProps"] {
    return {
        htmlInput: { maxLength: definition.maxLength, inputMode: definition.inputMode },
        input: { readOnly },
    };
}

function CardTextFieldPropsFor(
    definition: CardFieldDefinition,
    view: FieldView,
): CardTextFieldProps {
    const { name } = definition;
    return {
        definition,
        value: view.fields[name],
        readOnly: !IsEditable(view.state, name),
        error: view.fieldErrors.includes(name),
        inputRef: (element) => view.registerInput(name, element),
        onChange: (value) => view.changeField({ name, value }),
    };
}

// The value is kept exactly as keyed; only the BMS LENGTH is enforced. The key remounts the
// field when its protection changes, because an input replaced while it has focus never blurs,
// and MUI would keep showing the old field as focused.
function CardTextField(props: CardTextFieldProps) {
    const { definition, readOnly } = props;
    return (
        <TextField
            key={readOnly ? PROTECTED_FIELD_KEY : EDITABLE_FIELD_KEY}
            fullWidth
            label={definition.label}
            value={props.value}
            variant={readOnly ? "filled" : "outlined"}
            error={props.error}
            helperText={definition.helperText}
            inputRef={props.inputRef}
            onChange={(event) => props.onChange(event.target.value)}
            slotProps={FieldSlotProps(definition, readOnly)}
        />
    );
}

function FieldGrid({ definitions, view, children }: FieldGridProps) {
    return (
        <Grid container spacing={FIELD_SPACING}>
            {definitions.map((definition) => (
                <Grid key={definition.name} size={ENTRY_GRID_SIZE}>
                    <CardTextField {...CardTextFieldPropsFor(definition, view)} />
                </Grid>
            ))}
            {children}
        </Grid>
    );
}

// EXPMON, the "/" literal and EXPYEAR, announced together under the Expiry Date label.
function ExpiryDateGroup({ view }: FieldSectionProps) {
    return (
        <FormControl component="fieldset" fullWidth>
            <FormLabel component="legend">{EXPIRY_DATE_LABEL}</FormLabel>
            <Stack direction="row" spacing={EXPIRY_PART_SPACING} sx={EXPIRY_ROW_STYLE}>
                <CardTextField {...CardTextFieldPropsFor(CARD_UPDATE_FIELDS.expiry_month, view)} />
                <Typography component="span" aria-hidden="true">
                    {EXPIRY_SEPARATOR}
                </Typography>
                <CardTextField {...CardTextFieldPropsFor(CARD_UPDATE_FIELDS.expiry_year, view)} />
            </Stack>
        </FormControl>
    );
}

function CardKeyFields({ view }: FieldSectionProps) {
    return <FieldGrid definitions={KEY_FIELDS} view={view} />;
}

function CardDetailFields({ view }: FieldSectionProps) {
    return (
        <FieldGrid definitions={DETAIL_TEXT_FIELDS} view={view}>
            <Grid size={ENTRY_GRID_SIZE}>
                <ExpiryDateGroup view={view} />
            </Grid>
        </FieldGrid>
    );
}

function CardUpdateScreen() {
    const { view, feedback, isBusy, keys } = useCardUpdateScreen();
    return (
        <ScreenShell
            transactionId={TRANSACTION_ID}
            programName={PROGRAM_NAME}
            screenTitle={SCREEN_TITLE}
            isBusy={isBusy}
        >
            <CardKeyFields view={view} />
            <CardDetailFields view={view} />
            <MessageLine
                message={feedback.message}
                severity={feedback.severity}
                infoMessage={feedback.infoMessage}
            />
            <FunctionKeyBar keys={keys} />
        </ScreenShell>
    );
}

// Each arrival with another selection is a fresh COCRDUPC first entry, as every XCTL is, so the
// model and screen_token start over and the pending entry fetch is abandoned when the query
// changes; a write already sent still ends, and its answer reaches no later screen.
function CardUpdateEntry() {
    const { cardRef, acctId } = useEntryKeys();
    return <CardUpdateScreen key={JSON.stringify([cardRef, acctId])} />;
}

// CCUP: reached from main-menu option 5 and from a U selection on the card list (CCLI).
// useSearchParams needs a Suspense boundary for the static prerender of npm run build.
export default function CardUpdatePage() {
    return (
        <Suspense fallback={null}>
            <CardUpdateEntry />
        </Suspense>
    );
}
