// Replaces: BMS map COCRDSL, program COCRDSLC (CCDL); copybooks COCRDSL (cpy-bms), CVACT02Y,
// CVCUS01Y, CVCRD01Y, CSMSG02Y, CSUSR01Y, COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
"use client";

import { Grid, Stack, TextField, Typography, type TextFieldProps } from "@mui/material";
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
    type ChangeEvent,
    type Dispatch,
    type RefObject,
    type SetStateAction,
} from "react";

import { CENTRE_ALIGNED_ROW_STYLE } from "@/app/theme";
import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import {
    ErrorMessageReference,
    MessageLine,
    type MessageLineProps,
} from "@/components/MessageLine";
import { ScreenShell } from "@/components/ScreenShell";
import {
    PROMPT_FOR_KEYS_INFO,
    ViewCard,
    ViewSelectedCard,
    type CardDetailResponse,
} from "@/lib/api/cards";
import {
    CutToFieldLength,
    IsCancelledRequest,
    LegacyErrorBody,
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    type LegacyErrorResponse,
    type MessageSeverity,
    type ScreenFeedback,
} from "@/lib/apiClient";
import { EnterKeyOf, useFunctionKeys, type FunctionKeyDefinition } from "@/lib/useFunctionKeys";

const TRANSACTION_ID = "CCDL";
const PROGRAM_NAME = "COCRDSLC";
const SCREEN_TITLE = "View Credit Card Detail";

// The COCRDSL INITIAL texts, internal spacing kept and trailing blanks trimmed.
const ACCOUNT_LABEL = "Account Number    :";
const CARD_LABEL = "Card Number       :";
const NAME_LABEL = "Name on card      :";
const STATUS_LABEL = "Card Active Y/N   :";
const EXPIRY_LABEL = "Expiry Date       :";
const EXPIRY_MONTH_LABEL = "Month";
const EXPIRY_YEAR_LABEL = "Year";
const EXPIRY_SEPARATOR = "/";

// The BMS LENGTH of ACCTSID, CARDSID, CRDNAME, CRDSTCD, EXPMON and EXPYEAR.
const ACCOUNT_MAX_LENGTH = 11;
const CARD_MAX_LENGTH = 16;
const NAME_MAX_LENGTH = 50;
const STATUS_MAX_LENGTH = 1;
const MONTH_MAX_LENGTH = 2;
const YEAR_MAX_LENGTH = 4;

// The FKEYS legend: ENTER=Search Cards  F3=Exit.
const SEARCH_KEY_LABEL = "Search Cards";
const EXIT_KEY_LABEL = "Exit";

// F3 returns to the main menu, whether the menu or a card list selection opened the screen.
const MENU_ROUTE = "/menu";

// The COMMAREA keys a card list selection hands over; cardRef is an opaque card_ref token.
const CARD_REF_PARAM = "cardRef";
const ACCT_ID_PARAM = "acctId";

// The field names the server's field_errors and cursor_field carry (cocrdslc.py).
const ACCT_ID_FIELD = "acct_id";
const CARD_NUM_FIELD = "card_num";

// The answers that refuse the keys sent rather than the read: a key edit literal (400), Did not
// find cards for this search condition (404) and a structural rejection of the query (422). The
// same card_ref is never read on a retry; after a 503, a 500 or a lost connection it may be.
const KEY_REFUSAL_STATUSES: readonly number[] = [400, 404, 422];

// Each key field takes half the row on wider viewports and a full row on narrow ones.
const KEY_FIELD_SIZE = { xs: 12, md: 6 };
// The detail rows lay out as on COCRDUP: Name on card takes the whole row at every width, and
// Card Active and the Expiry Date group share the next row on wider viewports.
const NAME_FIELD_SIZE = { xs: 12 };
const DETAIL_FIELD_SIZE = { xs: 12, md: 6 };
// Theme spacing units between the detail fields: room for a field's raised label.
const DETAIL_FIELD_SPACING = 4;
// A multiline field is one native textarea row, which wraps a value wider than the field.
const MULTILINE_FIELD_ELEMENT = "textarea";
const MULTILINE_FIELD_ROWS = 1;

interface CardViewFeedback {
    message: string;
    severity: MessageSeverity;
    infoMessage: string;
    fieldErrors: readonly string[];
    cursorField: string;
}

// The details are shown only after a successful read (FOUND-CARDS-FOR-ACCOUNT). isListEntry
// holds while the keys are a card list selection's: protected, and read again by card_ref.
interface LookupState {
    detail: CardDetailResponse | null;
    feedback: CardViewFeedback;
    isBusy: boolean;
    isListEntry: boolean;
}

// The keys of one CCDL ENTER: the CARDSID text as keyed or a card_ref token, and ACCTSID.
// isCardRef marks a card list selection's card_ref, which the server decodes whatever its length.
interface LookupKeys {
    cardKey: string;
    acctKey: string;
    isCardRef: boolean;
}

// The query a screen was opened with; a non-empty cardRef means a card list selection.
interface EntryKeys {
    cardRef: string;
    acctId: string;
}

// signal belongs to the lookup's own AbortController; it is aborted once the lookup is abandoned
// (F3, leaving the screen, React's development remount or another card list selection), so an
// aborted signal marks an answer the screen must not show.
interface LookupRun {
    keys: LookupKeys;
    signal: AbortSignal;
    setLookupState: Dispatch<SetStateAction<LookupState>>;
}

// Holds the AbortController of the lookup in flight, or null when no read is pending.
type LookupControllerRef = RefObject<AbortController | null>;

interface GuardedLookupRun {
    keys: LookupKeys;
    lookupControllerRef: LookupControllerRef;
    setLookupState: Dispatch<SetStateAction<LookupState>>;
}

type LookupRunner = (keys: LookupKeys) => Promise<void>;

type LookupCanceller = () => void;

// True while a lookup holds the one-at-a-time guard, read at once rather than from a render.
type LookupPendingCheck = () => boolean;

interface CardLookup {
    lookupState: LookupState;
    RunLookup: LookupRunner;
    CancelLookup: LookupCanceller;
    IsLookupPending: LookupPendingCheck;
}

// The ACCTSID and CARDSID texts exactly as keyed: no trim, case change or client validation.
interface TypedTexts {
    acctText: string;
    cardText: string;
}

interface TypedKeys extends TypedTexts {
    SetAcctText: (text: string) => void;
    SetCardText: (text: string) => void;
}

type KeyInputRefs = Readonly<Record<string, RefObject<HTMLInputElement | null>>>;
type FunctionKeyList = readonly FunctionKeyDefinition[];

interface ScreenKeyInputs {
    entry: EntryKeys;
    typedKeys: TypedKeys;
    RunLookup: LookupRunner;
    CancelLookup: LookupCanceller;
    isBusy: boolean;
    isListEntry: boolean;
}

interface KeyFieldDefinition {
    fieldName: string;
    label: string;
    maxLength: number;
    // The BMS IC attribute: the cursor starts here when the keys can be typed.
    isInitialCursor: boolean;
}

interface KeyFieldModel {
    definition: KeyFieldDefinition;
    value: string;
    inputRef: RefObject<HTMLInputElement | null> | undefined;
    onTextChange: (text: string) => void;
}

interface KeyFieldsSource {
    entry: EntryKeys;
    typedKeys: TypedKeys;
    detail: CardDetailResponse | null;
    inputRefs: KeyInputRefs;
    isListEntry: boolean;
}

interface CardKeyFieldsProps {
    // A card list selection protects both keys (1300-SETUP-SCREEN-ATTRS, DFHBMPRF) until the
    // server refuses them.
    isListEntry: boolean;
    // The 3270 keyboard locks while the host runs the task, so typed keys wait for the answer.
    isBusy: boolean;
    fieldErrors: readonly string[];
    fields: readonly KeyFieldModel[];
}

interface KeyFieldProps {
    field: KeyFieldModel;
    isProtected: boolean;
    // Read-only while a read is pending; a typable key keeps its outlined look.
    isLocked: boolean;
    fieldErrors: readonly string[];
}

type DetailFieldName = "embossed_name" | "active_status" | "expiry_month" | "expiry_year";

interface DetailFieldDefinition {
    fieldName: DetailFieldName;
    label: string;
    maxLength: number;
    // A value that can outgrow its field wraps onto further rows instead of being cut off.
    isMultiline?: boolean;
}

interface DetailFieldProps {
    definition: DetailFieldDefinition;
    detail: CardDetailResponse | null;
}

interface CardDetailFieldsProps {
    detail: CardDetailResponse | null;
}

interface ExpiryPartsProps {
    labelId: string;
    detail: CardDetailResponse | null;
}

interface CardViewScreenModel {
    keyFields: CardKeyFieldsProps;
    detail: CardDetailResponse | null;
    messageLine: MessageLineProps;
    isBusy: boolean;
    keys: FunctionKeyList;
}

const ACCOUNT_KEY_FIELD: KeyFieldDefinition = {
    fieldName: ACCT_ID_FIELD,
    label: ACCOUNT_LABEL,
    maxLength: ACCOUNT_MAX_LENGTH,
    isInitialCursor: true,
};
const CARD_KEY_FIELD: KeyFieldDefinition = {
    fieldName: CARD_NUM_FIELD,
    label: CARD_LABEL,
    maxLength: CARD_MAX_LENGTH,
    isInitialCursor: false,
};

const NAME_FIELD: DetailFieldDefinition = {
    fieldName: "embossed_name",
    label: NAME_LABEL,
    maxLength: NAME_MAX_LENGTH,
    isMultiline: true,
};
const STATUS_FIELD: DetailFieldDefinition = {
    fieldName: "active_status",
    label: STATUS_LABEL,
    maxLength: STATUS_MAX_LENGTH,
};
const EXPIRY_MONTH_FIELD: DetailFieldDefinition = {
    fieldName: "expiry_month",
    label: EXPIRY_MONTH_LABEL,
    maxLength: MONTH_MAX_LENGTH,
};
const EXPIRY_YEAR_FIELD: DetailFieldDefinition = {
    fieldName: "expiry_year",
    label: EXPIRY_YEAR_LABEL,
    maxLength: YEAR_MAX_LENGTH,
};

const EMPTY_FEEDBACK: CardViewFeedback = {
    message: "",
    severity: "info",
    infoMessage: "",
    fieldErrors: [],
    cursorField: "",
};

const IDLE_LOOKUP_STATE: LookupState = {
    detail: null,
    feedback: EMPTY_FEEDBACK,
    isBusy: false,
    isListEntry: false,
};

// 1200-SETUP-SCREEN-VARS sets the key prompt whenever no information line is set, so a first
// entry from the menu shows it without a read.
const MENU_ENTRY_LOOKUP_STATE: LookupState = {
    ...IDLE_LOOKUP_STATE,
    feedback: { ...EMPTY_FEEDBACK, infoMessage: PROMPT_FOR_KEYS_INFO },
};
const LIST_ENTRY_LOOKUP_STATE: LookupState = { ...IDLE_LOOKUP_STATE, isListEntry: true };

// The server's lines verbatim: the info line keeps the leading spaces of its literal.
function ToViewFeedback(screenFeedback: ScreenFeedback, isError: boolean): CardViewFeedback {
    return {
        message: screenFeedback.message ?? "",
        severity: SeverityForOutcome({ isError, isCompletedWrite: false }),
        infoMessage: screenFeedback.info_message ?? "",
        fieldErrors: screenFeedback.field_errors ?? [],
        cursorField: screenFeedback.cursor_field ?? "",
    };
}

// A legacy error body (400, 404, 503) is shown as sent; a 500, a network failure or a non-legacy
// body shows the abend text. Anything that is not an axios error is a defect and propagates.
function FeedbackFromError(error: unknown): CardViewFeedback {
    if (!axios.isAxiosError(error)) {
        throw error;
    }
    const screenFeedback = LegacyErrorBody<LegacyErrorResponse>(error) ?? ToScreenFeedback(error);
    return ToViewFeedback(screenFeedback, true);
}

function IsKeyRefusal(error: unknown): boolean {
    const status = axios.isAxiosError(error) ? error.response?.status : undefined;
    return status !== undefined && KEY_REFUSAL_STATUSES.includes(status);
}

// A card list selection is read by its card_ref; typed keys go to the server as keyed.
function ReadCard(keys: LookupKeys, signal: AbortSignal): Promise<CardDetailResponse> {
    const readKeys = keys.isCardRef ? ViewSelectedCard : ViewCard;
    return readKeys(keys.cardKey, keys.acctKey, { signal });
}

// A failed read clears the details, as the map shows them only for a card that was found. A
// refusal of a card list selection's keys ends list-entry mode, so the keys can be typed; any
// other failure keeps it, and ENTER retries the selection. An abandoned read answers null: it is
// neither a legacy outcome nor the abend, so nothing is shown.
async function FetchLookupState(
    keys: LookupKeys,
    signal: AbortSignal,
): Promise<LookupState | null> {
    try {
        const detail = await ReadCard(keys, signal);
        const feedback = ToViewFeedback(detail, false);
        return { detail, feedback, isBusy: false, isListEntry: keys.isCardRef };
    } catch (error) {
        if (IsCancelledRequest(error)) {
            return null;
        }
        const isListEntry = keys.isCardRef && !IsKeyRefusal(error);
        return { detail: null, feedback: FeedbackFromError(error), isBusy: false, isListEntry };
    }
}

// Each request starts with a clear message line; the details shown and the mode stay until the
// answer.
function StartLookup(previous: LookupState): LookupState {
    return { ...previous, feedback: EMPTY_FEEDBACK, isBusy: true };
}

function EndLookup(previous: LookupState): LookupState {
    return previous.isBusy ? { ...previous, isBusy: false } : previous;
}

// A card list selection opens in list-entry mode with a clear line, as its read on arrival brings
// the information line; the menu opens with the key prompt.
function InitialLookupState(entry: EntryKeys): LookupState {
    return entry.cardRef === "" ? MENU_ENTRY_LOOKUP_STATE : LIST_ENTRY_LOOKUP_STATE;
}

// A pending message is the refusal literal another screen stored: a red ERRMSG line beside the
// information line the screen opened with.
function WithPendingMessage(previous: LookupState, message: string): LookupState {
    const severity = SeverityForOutcome({ isError: true, isCompletedWrite: false });
    const { infoMessage } = previous.feedback;
    return { ...previous, feedback: { ...EMPTY_FEEDBACK, message, severity, infoMessage } };
}

// Each lookup owns an AbortController, held in the ref while its read is in flight. Abandoning
// the lookup aborts it, so the read stops on the network and its answer never updates the screen.
function BeginLookup(lookupControllerRef: LookupControllerRef): AbortController {
    const lookupController = new AbortController();
    lookupControllerRef.current = lookupController;
    return lookupController;
}

// The aborted-signal check is a second guard, behind the one-at-a-time guard, that no abandoned
// answer overwrites the screen.
async function RunLatestLookup({ keys, signal, setLookupState }: LookupRun): Promise<void> {
    setLookupState(StartLookup);
    try {
        const nextState = await FetchLookupState(keys, signal);
        if (nextState !== null && !signal.aborted) {
            setLookupState(nextState);
        }
    } finally {
        if (!signal.aborted) {
            setLookupState(EndLookup);
        }
    }
}

// One read at a time, as one CICS task per key press: a lookup asked for while another holds the
// ref is refused at once, before React re-renders the disabled Search key. Only the holder frees
// the ref, so an abandoned read that settles late never frees the guard of the read after it.
async function RunGuardedLookup(run: GuardedLookupRun): Promise<void> {
    const { keys, lookupControllerRef, setLookupState } = run;
    if (lookupControllerRef.current !== null) {
        return;
    }
    const lookupController = BeginLookup(lookupControllerRef);
    try {
        await RunLatestLookup({ keys, signal: lookupController.signal, setLookupState });
    } finally {
        if (lookupControllerRef.current === lookupController) {
            lookupControllerRef.current = null;
        }
    }
}

function ListEntryLookupKeys({ cardRef, acctId }: EntryKeys): LookupKeys {
    return { cardKey: cardRef, acctKey: acctId, isCardRef: true };
}

// In list-entry mode ENTER re-reads the selected card; otherwise both keys go to the server as
// typed.
function LookupKeysFor(entry: EntryKeys, typedTexts: TypedTexts, isListEntry: boolean): LookupKeys {
    if (isListEntry) {
        return ListEntryLookupKeys(entry);
    }
    return { cardKey: typedTexts.cardText, acctKey: typedTexts.acctText, isCardRef: false };
}

// The 3270 keyboard locks while the host runs the task, so Search waits for the answer; Exit
// stays available and leaves at any time.
function BuildFunctionKeys(
    onSearch: () => void,
    onExit: () => void,
    isBusy: boolean,
): FunctionKeyList {
    return [
        { key: "ENTER", label: SEARCH_KEY_LABEL, onPress: onSearch, disabled: isBusy },
        { key: "F3", label: EXIT_KEY_LABEL, onPress: onExit },
    ];
}

// Protected keys show the selected card: the account as read, or the query's until the answer
// arrives, and the card number only masked. Typable keys show the text exactly as keyed.
function BuildKeyFields(source: KeyFieldsSource): KeyFieldModel[] {
    const { entry, typedKeys, detail, inputRefs, isListEntry } = source;
    const account: KeyFieldModel = {
        definition: ACCOUNT_KEY_FIELD,
        value: isListEntry ? (detail?.acct_id ?? entry.acctId) : typedKeys.acctText,
        inputRef: inputRefs[ACCT_ID_FIELD],
        onTextChange: typedKeys.SetAcctText,
    };
    const card: KeyFieldModel = {
        definition: CARD_KEY_FIELD,
        value: isListEntry ? (detail?.card_num_masked ?? "") : typedKeys.cardText,
        inputRef: inputRefs[CARD_NUM_FIELD],
        onTextChange: typedKeys.SetCardText,
    };
    return [account, card];
}

function MessageLineFor({ message, severity, infoMessage }: CardViewFeedback): MessageLineProps {
    return { message, severity, infoMessage };
}

// BMS PROT and ASKIP fields become read-only inputs; every field keeps its BMS LENGTH. A field
// the server names in field_errors is tied to the ERRMSG line, so assistive technology reads the
// literal when the cursor lands on it. A multiline field renders a native textarea, which takes
// no type, so the input type MUI passes is dropped.
function FieldSlotProps(
    isReadOnly: boolean,
    maxLength: number,
    isInvalid: boolean,
    isMultiline = false,
): TextFieldProps["slotProps"] {
    const htmlInput = { maxLength, ...ErrorMessageReference(isInvalid) };
    return {
        input: {
            readOnly: isReadOnly,
            inputComponent: isMultiline ? MULTILINE_FIELD_ELEMENT : undefined,
        },
        htmlInput: isMultiline ? { ...htmlInput, type: undefined } : htmlInput,
    };
}

// Read-only fields stay focusable, so the server's cursor reaches a protected key too.
function FocusCursorField(inputRefs: KeyInputRefs, cursorField: string): void {
    inputRefs[cursorField]?.current?.focus();
}

// A message another page left for this one (StorePendingMessage) is shown once, on mount.
function usePendingMessage(setLookupState: Dispatch<SetStateAction<LookupState>>): void {
    useEffect(() => {
        const pendingMessage = TakePendingMessage();
        if (pendingMessage !== "") {
            setLookupState((previous) => WithPendingMessage(previous, pendingMessage));
        }
    }, [setLookupState]);
}

// Leaving the screen abandons the lookup in flight. Cancelling also frees the one-at-a-time guard
// and ends the busy state, which no aborted lookup ends itself, so the screen never stays busy
// once its read is abandoned, and the read React's development remount starts is not refused.
function useLookupCanceller(
    lookupControllerRef: LookupControllerRef,
    setLookupState: Dispatch<SetStateAction<LookupState>>,
): LookupCanceller {
    const cancelLookup = useCallback(() => {
        lookupControllerRef.current?.abort();
        lookupControllerRef.current = null;
        setLookupState(EndLookup);
    }, [lookupControllerRef, setLookupState]);
    useEffect(() => cancelLookup, [cancelLookup]);
    return cancelLookup;
}

function useCardLookup(entry: EntryKeys): CardLookup {
    const [lookupState, setLookupState] = useState<LookupState>(() => InitialLookupState(entry));
    const lookupControllerRef = useRef<AbortController | null>(null);
    const runLookup = useCallback(
        (keys: LookupKeys) => RunGuardedLookup({ keys, lookupControllerRef, setLookupState }),
        [],
    );
    const cancelLookup = useLookupCanceller(lookupControllerRef, setLookupState);
    const isLookupPending = useCallback(() => lookupControllerRef.current !== null, []);
    usePendingMessage(setLookupState);
    return {
        lookupState,
        RunLookup: runLookup,
        CancelLookup: cancelLookup,
        IsLookupPending: isLookupPending,
    };
}

// A key setter that refuses text keyed while a lookup is pending, at once and before React
// re-renders the locked keys, so an answer is always shown under the keys that were sent.
function GuardedKeySetter(
    setText: Dispatch<SetStateAction<string>>,
    isLookupPending: LookupPendingCheck,
): (text: string) => void {
    return (text: string) => {
        if (!isLookupPending()) {
            setText(text);
        }
    };
}

// A card list selection hands over its account, which ACCTSID shows once a refusal of the
// selection's keys hands the keys to the user. The card number is never seeded: only its
// card_ref travels.
function SeededAccountText({ cardRef, acctId }: EntryKeys): string {
    return cardRef === "" ? "" : acctId;
}

function useTypedKeys(entry: EntryKeys, isLookupPending: LookupPendingCheck): TypedKeys {
    const [acctText, setAcctText] = useState(() => SeededAccountText(entry));
    const [cardText, setCardText] = useState("");
    return useMemo(
        () => ({
            acctText,
            cardText,
            SetAcctText: GuardedKeySetter(setAcctText, isLookupPending),
            SetCardText: GuardedKeySetter(setCardText, isLookupPending),
        }),
        [acctText, cardText, isLookupPending],
    );
}

function useKeyInputRefs(): KeyInputRefs {
    const acctInputRef = useRef<HTMLInputElement | null>(null);
    const cardInputRef = useRef<HTMLInputElement | null>(null);
    return useMemo(() => ({ [ACCT_ID_FIELD]: acctInputRef, [CARD_NUM_FIELD]: cardInputRef }), []);
}

// The query names the screen's entry, the account as ACCTSID holds it; missing values are blank.
function useEntryKeys(): EntryKeys {
    const searchParams = useSearchParams();
    return {
        cardRef: searchParams.get(CARD_REF_PARAM) ?? "",
        acctId: CutToFieldLength(searchParams.get(ACCT_ID_PARAM) ?? "", ACCOUNT_MAX_LENGTH),
    };
}

// From the card list the card is read on arrival; from the menu the empty map waits for ENTER.
// Leaving abandons the entry read, so React's development double effect run abandons the first
// read and reads again, and only the second read's answer is shown.
function useListEntryLookup(
    { cardRef, acctId }: EntryKeys,
    runLookup: LookupRunner,
    cancelLookup: LookupCanceller,
): void {
    useEffect(() => {
        if (cardRef === "") {
            return undefined;
        }
        void runLookup(ListEntryLookupKeys({ cardRef, acctId }));
        return cancelLookup;
    }, [cardRef, acctId, runLookup, cancelLookup]);
}

// Every answer is a new feedback object, so the cursor moves after each response.
function useCursorFocus(inputRefs: KeyInputRefs, feedback: CardViewFeedback): void {
    useEffect(() => {
        FocusCursorField(inputRefs, feedback.cursorField);
    }, [inputRefs, feedback]);
}

// A typed card number reaches only the API path ViewCard builds: never a route, URL or storage.
// F3 abandons the lookup at once, as the screen stays mounted until the next route has loaded.
function useScreenFunctionKeys(inputs: ScreenKeyInputs): FunctionKeyList {
    const router = useRouter();
    const { RunLookup, CancelLookup, isBusy, isListEntry } = inputs;
    const { cardRef, acctId } = inputs.entry;
    const { acctText, cardText } = inputs.typedKeys;
    const handleSearch = useCallback(() => {
        void RunLookup(LookupKeysFor({ cardRef, acctId }, { acctText, cardText }, isListEntry));
    }, [RunLookup, cardRef, acctId, acctText, cardText, isListEntry]);
    const handleExit = useCallback(() => {
        CancelLookup();
        router.push(MENU_ROUTE);
    }, [CancelLookup, router]);
    return useMemo(
        () => BuildFunctionKeys(handleSearch, handleExit, isBusy),
        [handleSearch, handleExit, isBusy],
    );
}

function useCardViewScreen(): CardViewScreenModel {
    const entry = useEntryKeys();
    const inputRefs = useKeyInputRefs();
    const { lookupState, RunLookup, CancelLookup, IsLookupPending } = useCardLookup(entry);
    const typedKeys = useTypedKeys(entry, IsLookupPending);
    const { detail, feedback, isBusy, isListEntry } = lookupState;
    useListEntryLookup(entry, RunLookup, CancelLookup);
    useCursorFocus(inputRefs, feedback);
    const keyInputs = { entry, typedKeys, RunLookup, CancelLookup, isBusy, isListEntry };
    const keys = useScreenFunctionKeys(keyInputs);
    const fields = BuildKeyFields({ entry, typedKeys, detail, inputRefs, isListEntry });
    const keyFields = { isListEntry, isBusy, fieldErrors: feedback.fieldErrors, fields };
    return { keyFields, detail, messageLine: MessageLineFor(feedback), isBusy, keys };
}

function KeyField({ field, isProtected, isLocked, fieldErrors }: KeyFieldProps) {
    const { definition } = field;
    const isInvalid = fieldErrors.includes(definition.fieldName);
    return (
        <TextField
            label={definition.label}
            value={field.value}
            onChange={(event: ChangeEvent<HTMLInputElement>) =>
                field.onTextChange(event.target.value)
            }
            variant={isProtected ? "filled" : "outlined"}
            error={isInvalid}
            autoFocus={definition.isInitialCursor && !isProtected}
            inputRef={field.inputRef}
            fullWidth
            slotProps={FieldSlotProps(isProtected || isLocked, definition.maxLength, isInvalid)}
        />
    );
}

function CardKeyFields({ isListEntry, isBusy, fieldErrors, fields }: CardKeyFieldsProps) {
    return (
        <Grid container spacing={4}>
            {fields.map((field) => (
                <Grid key={field.definition.fieldName} size={KEY_FIELD_SIZE}>
                    <KeyField
                        field={field}
                        isProtected={isListEntry}
                        isLocked={isBusy}
                        fieldErrors={fieldErrors}
                    />
                </Grid>
            ))}
        </Grid>
    );
}

// Every detail field is ASKIP on the map, so it is shown read-only, and the server never names
// one in field_errors. Each fills its grid cell or its share of the expiry row. A multiline one
// is a native textarea, which the theme sizes to its content: it keeps one row while its value
// fits, and wraps a longer value onto further rows, with no hidden copy of the field rendered to
// measure it. maxLength only exposes the BMS LENGTH.
function DetailField({ definition, detail }: DetailFieldProps) {
    const isMultiline = definition.isMultiline === true;
    return (
        <TextField
            label={definition.label}
            value={detail?.[definition.fieldName] ?? ""}
            variant="filled"
            fullWidth
            multiline={isMultiline}
            rows={isMultiline ? MULTILINE_FIELD_ROWS : undefined}
            slotProps={FieldSlotProps(true, definition.maxLength, false, isMultiline)}
        />
    );
}

function ExpiryFields({ detail }: CardDetailFieldsProps) {
    const labelId = useId();
    return (
        <Stack spacing={2}>
            <Typography id={labelId} variant="labelLarge" component="p">
                {EXPIRY_LABEL}
            </Typography>
            <ExpiryParts labelId={labelId} detail={detail} />
        </Stack>
    );
}

// EXPMON, the "/" literal and EXPYEAR, announced together under the Expiry Date label; the
// literal is only a visual separator, and the month and year fields sit level with it.
function ExpiryParts({ labelId, detail }: ExpiryPartsProps) {
    return (
        <Stack
            direction="row"
            spacing={2}
            sx={CENTRE_ALIGNED_ROW_STYLE}
            role="group"
            aria-labelledby={labelId}
        >
            <DetailField definition={EXPIRY_MONTH_FIELD} detail={detail} />
            <Typography component="span" aria-hidden="true">
                {EXPIRY_SEPARATOR}
            </Typography>
            <DetailField definition={EXPIRY_YEAR_FIELD} detail={detail} />
        </Stack>
    );
}

// CRDNAME (row 11), CRDSTCD (row 13) and the expiry parts (row 15), in the grid COCRDUP uses.
function CardDetailFields({ detail }: CardDetailFieldsProps) {
    return (
        <Grid container spacing={DETAIL_FIELD_SPACING}>
            <Grid size={NAME_FIELD_SIZE}>
                <DetailField definition={NAME_FIELD} detail={detail} />
            </Grid>
            <Grid size={DETAIL_FIELD_SIZE}>
                <DetailField definition={STATUS_FIELD} detail={detail} />
            </Grid>
            <Grid size={DETAIL_FIELD_SIZE}>
                <ExpiryFields detail={detail} />
            </Grid>
        </Grid>
    );
}

function CardViewScreen() {
    const screen = useCardViewScreen();
    // PFK-INVALID: any AID but ENTER and PF3 does SET CCARD-AID-ENTER, so an unbound PF key runs
    // the search.
    useFunctionKeys(screen.keys, EnterKeyOf(screen.keys));
    return (
        <ScreenShell
            transactionId={TRANSACTION_ID}
            programName={PROGRAM_NAME}
            screenTitle={SCREEN_TITLE}
            isBusy={screen.isBusy}
        >
            <CardKeyFields {...screen.keyFields} />
            <CardDetailFields detail={screen.detail} />
            <MessageLine {...screen.messageLine} />
            <FunctionKeyBar keys={screen.keys} />
        </ScreenShell>
    );
}

// Each arrival with another selection is a fresh COCRDSLC first entry, as every XCTL is, so the
// screen state starts over and its pending read is abandoned when the selection changes.
function CardViewEntry() {
    const { cardRef, acctId } = useEntryKeys();
    return <CardViewScreen key={JSON.stringify([cardRef, acctId])} />;
}

// useSearchParams needs a Suspense boundary wherever Next.js prerenders a page. The route layout
// renders this one on demand, so the query is known on the first render and the empty fallback
// never shows.
export default function CardViewPage() {
    return (
        <Suspense fallback={null}>
            <CardViewEntry />
        </Suspense>
    );
}
