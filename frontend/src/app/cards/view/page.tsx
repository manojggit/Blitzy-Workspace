// Replaces: BMS map COCRDSL, program COCRDSLC (CCDL); copybooks COCRDSL (cpy-bms), CVACT02Y,
// CVCUS01Y, CVCRD01Y, CSMSG02Y, CSUSR01Y, COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
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

import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import { MessageLine, type MessageLineProps } from "@/components/MessageLine";
import { ScreenShell } from "@/components/ScreenShell";
import { ViewCard, type CardDetailResponse } from "@/lib/api/cards";
import {
    IsCancelledRequest,
    LegacyErrorBody,
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    type LegacyErrorResponse,
    type MessageSeverity,
    type ScreenFeedback,
} from "@/lib/apiClient";
import { useFunctionKeys, type FunctionKeyDefinition } from "@/lib/useFunctionKeys";

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

// Each key field takes half the row on wider viewports and a full row on narrow ones.
const KEY_FIELD_SIZE = { xs: 12, md: 6 };
// The month and year fields sit level with the separator between them.
const EXPIRY_ROW_STYLE: SxProps<Theme> = { alignItems: "center" };

interface CardViewFeedback {
    message: string;
    severity: MessageSeverity;
    infoMessage: string;
    fieldErrors: readonly string[];
    cursorField: string;
}

// The details are shown only after a successful read (FOUND-CARDS-FOR-ACCOUNT).
interface LookupState {
    detail: CardDetailResponse | null;
    feedback: CardViewFeedback;
    isBusy: boolean;
}

// The keys of one CCDL ENTER: the CARDSID text as keyed or a card_ref token, and ACCTSID.
interface LookupKeys {
    cardKey: string;
    acctKey: string;
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

interface CardLookup {
    lookupState: LookupState;
    RunLookup: LookupRunner;
    CancelLookup: LookupCanceller;
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
}

interface CardKeyFieldsProps {
    // A card list selection protects both keys (1300-SETUP-SCREEN-ATTRS, DFHBMPRF).
    isListEntry: boolean;
    fieldErrors: readonly string[];
    fields: readonly KeyFieldModel[];
}

interface KeyFieldProps {
    field: KeyFieldModel;
    isProtected: boolean;
    fieldErrors: readonly string[];
}

type DetailFieldName = "embossed_name" | "active_status" | "expiry_month" | "expiry_year";

interface DetailFieldDefinition {
    fieldName: DetailFieldName;
    label: string;
    maxLength: number;
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

const IDLE_LOOKUP_STATE: LookupState = { detail: null, feedback: EMPTY_FEEDBACK, isBusy: false };

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

// A failed read clears the details, as the map shows them only for a card that was found. An
// abandoned read answers null: it is neither a legacy outcome nor the abend, so nothing is shown.
async function FetchLookupState(
    keys: LookupKeys,
    signal: AbortSignal,
): Promise<LookupState | null> {
    try {
        const detail = await ViewCard(keys.cardKey, keys.acctKey, { signal });
        return { detail, feedback: ToViewFeedback(detail, false), isBusy: false };
    } catch (error) {
        if (IsCancelledRequest(error)) {
            return null;
        }
        return { detail: null, feedback: FeedbackFromError(error), isBusy: false };
    }
}

// Each request starts with a clear message line; the details shown stay until the answer.
function StartLookup(previous: LookupState): LookupState {
    return { detail: previous.detail, feedback: EMPTY_FEEDBACK, isBusy: true };
}

function EndLookup(previous: LookupState): LookupState {
    return previous.isBusy ? { ...previous, isBusy: false } : previous;
}

// A pending message is the refusal literal another screen stored: a red ERRMSG line.
function WithPendingMessage(previous: LookupState, message: string): LookupState {
    const severity = SeverityForOutcome({ isError: true, isCompletedWrite: false });
    return { ...previous, feedback: { ...EMPTY_FEEDBACK, message, severity } };
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

// A card list selection re-reads its card; otherwise both keys go to the server as typed.
function LookupKeysFor(entry: EntryKeys, typedTexts: TypedTexts): LookupKeys {
    if (entry.cardRef !== "") {
        return { cardKey: entry.cardRef, acctKey: entry.acctId };
    }
    return { cardKey: typedTexts.cardText, acctKey: typedTexts.acctText };
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
function BuildKeyFields({ entry, typedKeys, detail, inputRefs }: KeyFieldsSource): KeyFieldModel[] {
    const isListEntry = entry.cardRef !== "";
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

// BMS PROT and ASKIP fields become read-only inputs; every field keeps its BMS LENGTH.
function FieldSlotProps(isReadOnly: boolean, maxLength: number): TextFieldProps["slotProps"] {
    return { input: { readOnly: isReadOnly }, htmlInput: { maxLength } };
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
function useCardLookup(): CardLookup {
    const [lookupState, setLookupState] = useState<LookupState>(IDLE_LOOKUP_STATE);
    const lookupControllerRef = useRef<AbortController | null>(null);
    const runLookup = useCallback(
        (keys: LookupKeys) => RunGuardedLookup({ keys, lookupControllerRef, setLookupState }),
        [],
    );
    const cancelLookup = useCallback(() => {
        lookupControllerRef.current?.abort();
        lookupControllerRef.current = null;
        setLookupState(EndLookup);
    }, []);
    useEffect(() => cancelLookup, [cancelLookup]);
    usePendingMessage(setLookupState);
    return { lookupState, RunLookup: runLookup, CancelLookup: cancelLookup };
}

function useTypedKeys(): TypedKeys {
    const [acctText, setAcctText] = useState("");
    const [cardText, setCardText] = useState("");
    return { acctText, cardText, SetAcctText: setAcctText, SetCardText: setCardText };
}

function useKeyInputRefs(): KeyInputRefs {
    const acctInputRef = useRef<HTMLInputElement | null>(null);
    const cardInputRef = useRef<HTMLInputElement | null>(null);
    return useMemo(() => ({ [ACCT_ID_FIELD]: acctInputRef, [CARD_NUM_FIELD]: cardInputRef }), []);
}

// The query names the screen's entry; missing values are blank.
function useEntryKeys(): EntryKeys {
    const searchParams = useSearchParams();
    return {
        cardRef: searchParams.get(CARD_REF_PARAM) ?? "",
        acctId: searchParams.get(ACCT_ID_PARAM) ?? "",
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
        void runLookup({ cardKey: cardRef, acctKey: acctId });
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
    const { RunLookup, CancelLookup, isBusy } = inputs;
    const { cardRef, acctId } = inputs.entry;
    const { acctText, cardText } = inputs.typedKeys;
    const handleSearch = useCallback(() => {
        void RunLookup(LookupKeysFor({ cardRef, acctId }, { acctText, cardText }));
    }, [RunLookup, cardRef, acctId, acctText, cardText]);
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
    const typedKeys = useTypedKeys();
    const inputRefs = useKeyInputRefs();
    const { lookupState, RunLookup, CancelLookup } = useCardLookup();
    const { detail, feedback, isBusy } = lookupState;
    useListEntryLookup(entry, RunLookup, CancelLookup);
    useCursorFocus(inputRefs, feedback);
    const keys = useScreenFunctionKeys({ entry, typedKeys, RunLookup, CancelLookup, isBusy });
    const fields = BuildKeyFields({ entry, typedKeys, detail, inputRefs });
    const isListEntry = entry.cardRef !== "";
    const keyFields = { isListEntry, fieldErrors: feedback.fieldErrors, fields };
    return { keyFields, detail, messageLine: MessageLineFor(feedback), isBusy, keys };
}

function KeyField({ field, isProtected, fieldErrors }: KeyFieldProps) {
    const { definition } = field;
    return (
        <TextField
            label={definition.label}
            value={field.value}
            onChange={(event: ChangeEvent<HTMLInputElement>) =>
                field.onTextChange(event.target.value)
            }
            variant={isProtected ? "filled" : "outlined"}
            error={fieldErrors.includes(definition.fieldName)}
            autoFocus={definition.isInitialCursor && !isProtected}
            inputRef={field.inputRef}
            fullWidth
            slotProps={FieldSlotProps(isProtected, definition.maxLength)}
        />
    );
}

function CardKeyFields({ isListEntry, fieldErrors, fields }: CardKeyFieldsProps) {
    return (
        <Grid container spacing={4}>
            {fields.map((field) => (
                <Grid key={field.definition.fieldName} size={KEY_FIELD_SIZE}>
                    <KeyField field={field} isProtected={isListEntry} fieldErrors={fieldErrors} />
                </Grid>
            ))}
        </Grid>
    );
}

// Every detail field is ASKIP on the map, so it is shown read-only.
function DetailField({ definition, detail }: DetailFieldProps) {
    return (
        <TextField
            label={definition.label}
            value={detail?.[definition.fieldName] ?? ""}
            variant="filled"
            slotProps={FieldSlotProps(true, definition.maxLength)}
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

// EXPMON, the "/" literal and EXPYEAR, announced together under the Expiry Date label.
function ExpiryParts({ labelId, detail }: ExpiryPartsProps) {
    return (
        <Stack
            direction="row"
            spacing={2}
            sx={EXPIRY_ROW_STYLE}
            role="group"
            aria-labelledby={labelId}
        >
            <DetailField definition={EXPIRY_MONTH_FIELD} detail={detail} />
            <Typography component="span">{EXPIRY_SEPARATOR}</Typography>
            <DetailField definition={EXPIRY_YEAR_FIELD} detail={detail} />
        </Stack>
    );
}

function CardDetailFields({ detail }: CardDetailFieldsProps) {
    return (
        <Stack spacing={4}>
            <DetailField definition={NAME_FIELD} detail={detail} />
            <DetailField definition={STATUS_FIELD} detail={detail} />
            <ExpiryFields detail={detail} />
        </Stack>
    );
}

function CardViewScreen() {
    const screen = useCardViewScreen();
    useFunctionKeys(screen.keys);
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
// screen state starts over and its pending read is abandoned when the query changes.
function CardViewEntry() {
    const { cardRef, acctId } = useEntryKeys();
    return <CardViewScreen key={JSON.stringify([cardRef, acctId])} />;
}

// useSearchParams needs a Suspense boundary for the static prerender of the route.
export default function CardViewPage() {
    return (
        <Suspense fallback={null}>
            <CardViewEntry />
        </Suspense>
    );
}
