// Replaces: BMS map COCRDUP, program COCRDUPC (CCUP); copybooks COCRDUP (cpy-bms), CVACT02Y,
// CVCUS01Y, CVCRD01Y, CSMSG02Y, CSUSR01Y, COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
"use client";

import {
    Grid,
    Stack,
    TextField,
    Typography,
    type GridProps,
    type TextFieldProps,
} from "@mui/material";
import axios from "axios";
import { useRouter, useSearchParams } from "next/navigation";
import {
    Suspense,
    useCallback,
    useEffect,
    useId,
    useMemo,
    useRef,
    useState,
    type Dispatch,
    type ReactNode,
    type RefObject,
    type SetStateAction,
} from "react";

import { CENTRE_ALIGNED_ROW_STYLE } from "@/app/theme";
import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import { ErrorMessageReference, HelperTextIdOf, MessageLine } from "@/components/MessageLine";
import { ScreenShell } from "@/components/ScreenShell";
import {
    FetchCardForEdit,
    PROMPT_FOR_KEYS_INFO,
    UpdateCard,
    type CardScreen,
    type CardScreenErrorResponse,
    type CardScreenFields,
    type CardUpdateRequest,
} from "@/lib/api/cards";
import {
    CutToFieldLength,
    IsCancelledRequest,
    LegacyErrorBody,
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    UpdateAction,
    UpdateScreenState,
    type MessageSeverity,
    type ScreenFeedback,
} from "@/lib/apiClient";
import {
    EnterKeyOf,
    useFunctionKeys,
    type FunctionKeyDefinition,
    type PfKey,
} from "@/lib/useFunctionKeys";

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
// Name on card takes the whole row at every width, as on COCRDSL.
const FULL_ROW_GRID_SIZE = { xs: 12 } as const;
// Theme spacing units: room between fields for an outlined field's raised label.
const FIELD_SPACING = 4;
const EXPIRY_LABEL_SPACING = 2;
const EXPIRY_PART_SPACING = 2;
const EDITABLE_FIELD_KEY = "editable";
const PROTECTED_FIELD_KEY = "protected";
// Every CR and LF, which an <input type="text"> strips from its value.
const LINE_BREAK_PATTERN = /[\r\n]/g;
// A multiline field is one native textarea row, which wraps a value wider than the field.
const MULTILINE_FIELD_ELEMENT = "textarea";
const MULTILINE_FIELD_ROWS = 1;

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
    // The grid cell the field fills; ENTRY_GRID_SIZE when not given.
    gridSize?: GridProps["size"];
    // The value wraps onto further rows instead of being cut off, protected or editable; it never
    // holds a line break (KeyedValueOf).
    isMultiline?: boolean;
}

const ACCT_ID_FIELD: CardFieldName = "acct_id";
const CARD_NUM_FIELD: CardFieldName = "card_num";
// The ACCTSID LENGTH.
const ACCT_ID_MAX_LENGTH = 11;

// The COCRDUP DFHMDF labels (INITIAL, trailing blank dropped) and LENGTHs. EXPMON and EXPYEAR
// have no INITIAL of their own: under the Expiry Date label they are Month and Year, as on COCRDSL.
const CARD_UPDATE_FIELDS: Readonly<Record<RenderedFieldName, CardFieldDefinition>> = Object.freeze({
    acct_id: { name: "acct_id", label: "Account Number    :", maxLength: ACCT_ID_MAX_LENGTH },
    card_num: { name: "card_num", label: "Card Number       :", maxLength: 16 },
    embossed_name: {
        name: "embossed_name",
        label: "Name on card      :",
        maxLength: 50,
        gridSize: FULL_ROW_GRID_SIZE,
        isMultiline: true,
    },
    active_status: {
        name: "active_status",
        label: "Card Active Y/N   :",
        maxLength: 1,
        helperText: YES_NO_HELPER_TEXT,
    },
    expiry_month: { name: "expiry_month", label: "Month", maxLength: 2, inputMode: "numeric" },
    expiry_year: { name: "expiry_year", label: "Year", maxLength: 4, inputMode: "numeric" },
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

// The COCRDUP PF legend: FKEYS (ENTER=Process F3=Exit) is always shown, while FKEYSC (F5=Save
// F12=Cancel) is ATTRB=(ASKIP,DRK) until 3300-SETUP-SCREEN-ATTRS brightens it IF
// PROMPT-FOR-CONFIRMATION, which 3250-SETUP-INFOMSG sets only WHEN CCUP-CHANGES-OK-NOT-CONFIRMED.
const FKEYS_LEGEND: readonly PfKey[] = Object.freeze(["ENTER", "F3"]);
const CONFIRMATION_LEGEND: readonly PfKey[] = Object.freeze(["ENTER", "F3", "F5", "F12"]);

// The legend each returned state shows, by the CCUP-CHANGE-ACTION 88-levels.
const LEGEND_KEYS_BY_STATE: Readonly<Record<UpdateScreenState, readonly PfKey[]>> = Object.freeze({
    [UpdateScreenState.DETAILS_NOT_FETCHED]: FKEYS_LEGEND,
    [UpdateScreenState.SHOW_DETAILS]: FKEYS_LEGEND,
    [UpdateScreenState.CHANGES_NOT_OK]: FKEYS_LEGEND,
    [UpdateScreenState.CHANGES_OK_NOT_CONFIRMED]: CONFIRMATION_LEGEND,
    [UpdateScreenState.CHANGES_DONE]: FKEYS_LEGEND,
    [UpdateScreenState.CHANGES_FAILED]: FKEYS_LEGEND,
});

// What the COMMAREA kept between two CCUP tasks: the CCUP-CHANGE-ACTION state, the latest
// encrypted screen_token and card_ref the server issued (no card_ref while no card is held), and
// the map fields as last shown or keyed. It lives in React state only; neither token is ever
// stored, decoded or logged. The page echoes each token unchanged on the next request that uses
// it and replaces it from each response that carries one.
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

// The 400, 404, 409, 423 and 503 CCUP bodies carry the screen beside the standard legacy keys.
type CardErrorBody = CardScreenErrorResponse;

type CardRequest = () => Promise<CardScreen>;

// signal is given only with a read, by the AbortController that owns it; a write never has one,
// so a save, an ENTER or a cancel is never abandoned once sent.
type RequestRunner = (request: CardRequest, signal?: AbortSignal) => void;

// The request that holds the screen while it runs, or null when a key may start one.
type BusyHolderRef = RefObject<object | null>;

// A multiline field renders a textarea; every other field an input.
type FieldElement = HTMLInputElement | HTMLTextAreaElement;

type InputRefMap = Partial<Record<CardFieldName, FieldElement | null>>;

type InputRefCallback = (element: FieldElement | null) => void;

type InputRegistration = (name: CardFieldName, element: FieldElement | null) => void;

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

// isFrozen is true while a request holds the screen, as a 3270 keyboard locks until the answer.
interface FieldView {
    fields: CardScreenFields;
    state: UpdateScreenState;
    fieldErrors: readonly string[];
    isFrozen: boolean;
    registerInput: InputRegistration;
    changeField: FieldChangeHandler;
}

// legendKeys are the buttons the state's legend shows; useCardUpdateScreen binds every COCRDUP
// PF key, shown or not.
interface CardUpdateScreenModel {
    view: FieldView;
    feedback: ScreenFeedbackView;
    isBusy: boolean;
    legendKeys: readonly FunctionKeyDefinition[];
}

interface CardTextFieldProps {
    definition: CardFieldDefinition;
    value: string;
    readOnly: boolean;
    isFrozen: boolean;
    error: boolean;
    inputRef: InputRefCallback;
    onChange: (value: string) => void;
}

interface FieldSectionProps {
    view: FieldView;
}

interface ExpiryPartsProps {
    labelId: string;
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

// Responses mask CARDSID, so a key edit or not-found answer to keys the user typed keeps them as
// typed. While a card_ref is held no card number was typed: the answer's keys, the held card
// masked, show which card the next key reads again. Every other response shows the server's
// fields, among them the empty key screen that discards the input after a done or failed change.
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
        previous.cardRef === null &&
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

// 3250-SETUP-INFOMSG answers a fresh entry (CDEMO-PGM-ENTER) with the key prompt, so the menu
// entry shows it without a request. A card list selection fetches its card at once, and the
// fetch's answer brings the information line.
function InitialFeedback(entry: EntryKeys): ScreenFeedbackView {
    if (entry.cardRef !== null) {
        return INITIAL_FEEDBACK;
    }
    return { ...INITIAL_FEEDBACK, infoMessage: PROMPT_FOR_KEYS_INFO };
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

// The pending message joins the information line the screen opened with.
function ShowPendingMessage(setters: ScreenSetters): void {
    const pendingMessage = TakePendingMessage();
    if (pendingMessage === "") {
        return;
    }
    setters.setFeedback((feedback) => ({
        ...feedback,
        message: pendingMessage,
        severity: PENDING_MESSAGE_SEVERITY,
    }));
}

// Lines 482-497: a session the card list opened reads its card by CDEMO-ACCT-ID and
// CDEMO-CARD-NUM; the held card_ref stands for that card, or for the card a fetch last showed.
// Until it is fetched, as after an invalid or expired card_ref refusal or a failure that brings no
// screen (a 500 or a network error), the screen shows at most its number masked, which is no card
// number to send; so ENTER, F5 and F12, all processed as ENTER before the fetch, read it again by
// the card_ref. A refused CARDDAT read on the entry fetch or F12 re-read answers in SHOW_DETAILS
// with both keys in its screen_token, so the next key is a PUT by the card_ref. Typing a card
// number drops the card_ref (WithFieldValue), so typed keys go by PUT.
function UnfetchedCardRefOf(model: CardUpdateModel): string | null {
    if (model.state !== UpdateScreenState.DETAILS_NOT_FETCHED) {
        return null;
    }
    return model.cardRef;
}

// Once details are fetched, F12 is the PFK12 re-read (lines 958-966), which takes the keys from
// the screen_token and discards the keyed edits, and after a done or failed change every key
// discards the input; so F12 sends no field, and an edit the request model rejects never blocks
// it. Before the fetch F12 is processed as ENTER on the keys, so it sends them as keyed, as ENTER
// and F5 always send the inputs the state leaves editable.
function FieldsSentBy(action: UpdateAction, model: CardUpdateModel): Partial<CardScreenFields> {
    const isDiscardingCancel =
        action === UpdateAction.CANCEL && model.state !== UpdateScreenState.DETAILS_NOT_FETCHED;
    return isDiscardingCancel ? {} : BuildEditableFields(model.state, model.fields);
}

// ENTER, F5 and F12 on COCRDUP. Every key reaches the server, whose state machine processes a
// save or cancel its state does not allow as ENTER (lines 413-424), and answers any key after a
// done or failed change with the empty key screen, whichever screen opened this one.
function HandleAction(action: UpdateAction, context: ActionContext): void {
    const { model, runRequest } = context;
    const unfetchedCardRef = UnfetchedCardRefOf(model);
    if (unfetchedCardRef !== null) {
        runRequest(() => FetchCardForEdit(unfetchedCardRef, model.fields.acct_id));
        return;
    }
    const request: CardUpdateRequest = {
        action,
        screen_token: model.screenToken,
        fields: FieldsSentBy(action, model),
    };
    runRequest(() => UpdateCard(ResolveCardKey(model), request));
}

// Every COCRDUP key, in legend order: ENTER=Process, F3=Exit, F5=Save and F12=Cancel. All four
// stay bound in every state, as a dark 3270 PF key still sends its AID: the server processes a
// save or cancel its state does not allow as ENTER (lines 413-424). LegendKeysShownIn picks the
// buttons the state's legend shows. ENTER, F5 and F12 each send a request, so they are disabled
// while one is in flight; F3 only navigates.
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

// A state the table does not name shows the DETAILS_NOT_FETCHED legend, as the server processes
// an unreadable screen_token as DETAILS_NOT_FETCHED.
function LegendKeysFor(state: UpdateScreenState): readonly PfKey[] {
    return Object.hasOwn(LEGEND_KEYS_BY_STATE, state) ? LEGEND_KEYS_BY_STATE[state] : FKEYS_LEGEND;
}

// The keys whose legend entry the state shows, in BuildFunctionKeys order.
function LegendKeysShownIn(
    keys: readonly FunctionKeyDefinition[],
    state: UpdateScreenState,
): FunctionKeyDefinition[] {
    const legendPfKeys = LegendKeysFor(state);
    return keys.filter((definition) => legendPfKeys.includes(definition.key));
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
    const acctId = CutToFieldLength(searchParams.get(ACCT_ID_QUERY_KEY) ?? "", ACCT_ID_MAX_LENGTH);
    return useMemo(() => ({ cardRef, acctId }), [cardRef, acctId]);
}

function useScreenStore(entry: EntryKeys): ScreenStore {
    const [model, setModel] = useState<CardUpdateModel>(() => InitialModel(entry));
    const [feedback, setFeedback] = useState<ScreenFeedbackView>(() => InitialFeedback(entry));
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
    const registerInput = useCallback((name: CardFieldName, element: FieldElement | null) => {
        inputRefs.current[name] = element;
    }, []);
    return { inputRefs, registerInput };
}

function useFieldView(store: ScreenStore, registerInput: InputRegistration): FieldView {
    const { model, feedback, isBusy, setters } = store;
    const changeField = useCallback(
        (change: FieldChange) => setters.setModel((current) => WithFieldValue(current, change)),
        [setters],
    );
    return useMemo(
        () => ({
            fields: model.fields,
            state: model.state,
            fieldErrors: feedback.fieldErrors,
            isFrozen: isBusy,
            registerInput,
            changeField,
        }),
        [model, feedback, isBusy, registerInput, changeField],
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
    // PFK-INVALID: an AID outside ENTER, PF3, PF5 and PF12 does SET CCARD-AID-ENTER, so an unbound
    // PF key sends action "enter"; the server turns an F5 or F12 its state refuses into ENTER.
    useFunctionKeys(keys, EnterKeyOf(keys));
    const legendKeys = LegendKeysShownIn(keys, store.model.state);
    const view = useFieldView(store, registry.registerInput);
    return { view, legendKeys, isBusy: store.isBusy, feedback: store.feedback };
}

// A stable id per map field, so a field in error can name its own helper text after ERRMSG.
function FieldIdOf(name: RenderedFieldName): string {
    return `card-update-${name}`;
}

// The BMS UNPROT/PROT attribute: a protected field stays focusable, but cannot be changed. While
// the server names the field in field_errors, it is tied to the ERRMSG line ahead of its hint,
// so assistive technology reads the literal when the cursor lands on it. A multiline field
// renders a native textarea, which takes no type, so the input type MUI passes is dropped.
function FieldSlotProps(
    definition: CardFieldDefinition,
    readOnly: boolean,
    hasError: boolean,
): TextFieldProps["slotProps"] {
    const helperTextId = HelperTextIdOf(FieldIdOf(definition.name), definition.helperText);
    const isMultiline = definition.isMultiline === true;
    const htmlInput = {
        maxLength: definition.maxLength,
        inputMode: definition.inputMode,
        ...ErrorMessageReference(hasError, helperTextId),
    };
    return {
        htmlInput: isMultiline ? { ...htmlInput, type: undefined } : htmlInput,
        input: { readOnly, inputComponent: isMultiline ? MULTILINE_FIELD_ELEMENT : undefined },
    };
}

// A multiline field's textarea wraps a long value, but a 3270 field holds no line break and the
// server refuses CR and LF as outside the code-page-037 repertoire. A pasted or dropped line break
// is therefore removed, as an <input type="text"> strips newlines from its value; Enter never
// inserts one, as useFunctionKeys takes it as the screen's ENTER. Every other field keeps its
// text exactly as keyed.
function KeyedValueOf(definition: CardFieldDefinition, value: string): string {
    if (definition.isMultiline !== true) {
        return value;
    }
    return value.replace(LINE_BREAK_PATTERN, "");
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
        isFrozen: view.isFrozen,
        error: view.fieldErrors.includes(name),
        inputRef: (element) => view.registerInput(name, element),
        onChange: (value) => view.changeField({ name, value: KeyedValueOf(definition, value) }),
    };
}

// The value is kept as keyed, a multiline field's line breaks aside; only the BMS LENGTH is
// enforced. A multiline field is a native textarea, which the theme sizes to its content: it
// keeps one row while its value fits, and wraps a longer value onto further rows, with no hidden
// copy of the field rendered to measure it. The key remounts the field when its protection
// changes, because an input replaced while it has focus never blurs, and MUI would keep showing
// the old field as focused. A frozen field only takes no typing until the answer is adopted, so
// the values sent are the values the answer validates; its look and its element stay as its
// protection sets them, and nothing flickers or remounts per request.
function CardTextField(props: CardTextFieldProps) {
    const { definition, readOnly } = props;
    return (
        <TextField
            key={readOnly ? PROTECTED_FIELD_KEY : EDITABLE_FIELD_KEY}
            id={FieldIdOf(definition.name)}
            fullWidth
            multiline={definition.isMultiline === true}
            rows={definition.isMultiline === true ? MULTILINE_FIELD_ROWS : undefined}
            label={definition.label}
            value={props.value}
            variant={readOnly ? "filled" : "outlined"}
            error={props.error}
            helperText={definition.helperText}
            inputRef={props.inputRef}
            onChange={(event) => props.onChange(event.target.value)}
            slotProps={FieldSlotProps(definition, readOnly || props.isFrozen, props.error)}
        />
    );
}

function FieldGrid({ definitions, view, children }: FieldGridProps) {
    return (
        <Grid container spacing={FIELD_SPACING}>
            {definitions.map((definition) => (
                <Grid key={definition.name} size={definition.gridSize ?? ENTRY_GRID_SIZE}>
                    <CardTextField {...CardTextFieldPropsFor(definition, view)} />
                </Grid>
            ))}
            {children}
        </Grid>
    );
}

// The Expiry Date label over its parts, as COCRDSL shows the same INITIAL.
function ExpiryDateGroup({ view }: FieldSectionProps) {
    const labelId = useId();
    return (
        <Stack spacing={EXPIRY_LABEL_SPACING}>
            <Typography id={labelId} variant="labelLarge" component="p">
                {EXPIRY_DATE_LABEL}
            </Typography>
            <ExpiryParts labelId={labelId} view={view} />
        </Stack>
    );
}

// EXPMON, the "/" literal and EXPYEAR, announced together under the Expiry Date label; the
// literal is only a visual separator, and the month and year fields sit level with it.
function ExpiryParts({ labelId, view }: ExpiryPartsProps) {
    return (
        <Stack
            direction="row"
            spacing={EXPIRY_PART_SPACING}
            sx={CENTRE_ALIGNED_ROW_STYLE}
            role="group"
            aria-labelledby={labelId}
        >
            <CardTextField {...CardTextFieldPropsFor(CARD_UPDATE_FIELDS.expiry_month, view)} />
            <Typography component="span" aria-hidden="true">
                {EXPIRY_SEPARATOR}
            </Typography>
            <CardTextField {...CardTextFieldPropsFor(CARD_UPDATE_FIELDS.expiry_year, view)} />
        </Stack>
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

interface CardUpdateMessageLineProps {
    feedback: ScreenFeedbackView;
}

// The ERRMSG and INFOMSG lines. Each answer, and a pending message on entry, sets a new feedback
// object, which typing never replaces, so its identity is the outcome: a literal answered again
// with the same lines still counts as a new outcome, and the cursor field that kept the focus is
// brought back into view.
function CardUpdateMessageLine({ feedback }: CardUpdateMessageLineProps) {
    return (
        <MessageLine
            message={feedback.message}
            severity={feedback.severity}
            infoMessage={feedback.infoMessage}
            outcomeKey={feedback}
        />
    );
}

function CardUpdateScreen() {
    const { view, feedback, isBusy, legendKeys } = useCardUpdateScreen();
    return (
        <ScreenShell
            transactionId={TRANSACTION_ID}
            programName={PROGRAM_NAME}
            screenTitle={SCREEN_TITLE}
            isBusy={isBusy}
        >
            <CardKeyFields view={view} />
            <CardDetailFields view={view} />
            <CardUpdateMessageLine feedback={feedback} />
            <FunctionKeyBar keys={legendKeys} />
        </ScreenShell>
    );
}

// Each arrival with another selection is a fresh COCRDUPC first entry, as every XCTL is, so the
// model and screen_token start over and the pending entry fetch is abandoned when the selection
// changes; a write already sent still ends, and its answer reaches no later screen.
function CardUpdateEntry() {
    const { cardRef, acctId } = useEntryKeys();
    return <CardUpdateScreen key={JSON.stringify([cardRef, acctId])} />;
}

// CCUP: reached from main-menu option 5 and from a U selection on the card list (CCLI).
// useSearchParams needs a Suspense boundary wherever Next.js prerenders a page. The route layout
// renders this one on demand, so the query is known on the first render and the empty fallback
// never shows.
export default function CardUpdatePage() {
    return (
        <Suspense fallback={null}>
            <CardUpdateEntry />
        </Suspense>
    );
}
