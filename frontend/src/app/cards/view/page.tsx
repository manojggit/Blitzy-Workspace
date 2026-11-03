// Replaces: BMS map COCRDSL, program COCRDSLC (CCDL); copybooks COCRDSL (cpy-bms), CVACT02Y,
// CVCUS01Y, CVCRD01Y, CSMSG02Y, CSUSR01Y, COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
// The header wraps to stay within the MK_Modernization Rule's 100-column line limit.
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

// F3 returns to the calling program: COCRDLIC after a list selection, otherwise the main menu.
const CARD_LIST_ROUTE = "/cards";
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

interface LookupRun {
    keys: LookupKeys;
    IsLatest: () => boolean;
    setLookupState: Dispatch<SetStateAction<LookupState>>;
}

type LookupRunner = (keys: LookupKeys) => Promise<void>;

interface CardLookup {
    lookupState: LookupState;
    RunLookup: LookupRunner;
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

// A failed read clears the details, as the map shows them only for a card that was found.
async function FetchLookupState(keys: LookupKeys): Promise<LookupState> {
    try {
        const detail = await ViewCard(keys.cardKey, keys.acctKey);
        return { detail, feedback: ToViewFeedback(detail, false), isBusy: false };
    } catch (error) {
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

// Numbers each lookup, so only the latest may update the screen and a slower earlier answer
// never overwrites a newer one.
function BeginLookup(latestLookupRef: RefObject<number>): () => boolean {
    latestLookupRef.current += 1;
    const lookupNumber = latestLookupRef.current;
    return () => lookupNumber === latestLookupRef.current;
}

async function RunLatestLookup({ keys, IsLatest, setLookupState }: LookupRun): Promise<void> {
    setLookupState(StartLookup);
    try {
        const nextState = await FetchLookupState(keys);
        if (IsLatest()) {
            setLookupState(nextState);
        }
    } finally {
        if (IsLatest()) {
            setLookupState(EndLookup);
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

function BuildFunctionKeys(onSearch: () => void, onExit: () => void): FunctionKeyList {
    return [
        { key: "ENTER", label: SEARCH_KEY_LABEL, onPress: onSearch },
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

function useCardLookup(): CardLookup {
    const [lookupState, setLookupState] = useState<LookupState>(IDLE_LOOKUP_STATE);
    const latestLookupRef = useRef(0);
    const runLookup = useCallback((keys: LookupKeys): Promise<void> => {
        const isLatest = BeginLookup(latestLookupRef);
        return RunLatestLookup({ keys, IsLatest: isLatest, setLookupState });
    }, []);
    usePendingMessage(setLookupState);
    return { lookupState, RunLookup: runLookup };
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

// From the card list the card is read at once; from the menu the empty map waits for ENTER.
function useListEntryLookup({ cardRef, acctId }: EntryKeys, runLookup: LookupRunner): void {
    useEffect(() => {
        if (cardRef !== "") {
            void runLookup({ cardKey: cardRef, acctKey: acctId });
        }
    }, [cardRef, acctId, runLookup]);
}

// Every answer is a new feedback object, so the cursor moves after each response.
function useCursorFocus(inputRefs: KeyInputRefs, feedback: CardViewFeedback): void {
    useEffect(() => {
        FocusCursorField(inputRefs, feedback.cursorField);
    }, [inputRefs, feedback]);
}

// A typed card number reaches only the API path ViewCard builds: never a route, URL or storage.
function useScreenFunctionKeys(inputs: ScreenKeyInputs): FunctionKeyList {
    const router = useRouter();
    const { RunLookup } = inputs;
    const { cardRef, acctId } = inputs.entry;
    const { acctText, cardText } = inputs.typedKeys;
    const handleSearch = useCallback(() => {
        void RunLookup(LookupKeysFor({ cardRef, acctId }, { acctText, cardText }));
    }, [RunLookup, cardRef, acctId, acctText, cardText]);
    const handleExit = useCallback(() => {
        router.push(cardRef !== "" ? CARD_LIST_ROUTE : MENU_ROUTE);
    }, [router, cardRef]);
    return useMemo(() => BuildFunctionKeys(handleSearch, handleExit), [handleSearch, handleExit]);
}

function useCardViewScreen(): CardViewScreenModel {
    const entry = useEntryKeys();
    const typedKeys = useTypedKeys();
    const inputRefs = useKeyInputRefs();
    const { lookupState, RunLookup } = useCardLookup();
    const { detail, feedback, isBusy } = lookupState;
    useListEntryLookup(entry, RunLookup);
    useCursorFocus(inputRefs, feedback);
    const keys = useScreenFunctionKeys({ entry, typedKeys, RunLookup });
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

// useSearchParams needs a Suspense boundary for the static prerender of the route.
export default function CardViewPage() {
    return (
        <Suspense fallback={null}>
            <CardViewScreen />
        </Suspense>
    );
}
