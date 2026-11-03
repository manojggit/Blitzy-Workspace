// Replaces: BMS map COTRN01, program COTRN01C (CT01); copybooks COTRN01 (cpy-bms),
// CVTRA05Y, COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
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
import { useRouter, useSearchParams } from "next/navigation";
import { Box, Divider, Grid, TextField, type GridProps } from "@mui/material";
import axios from "axios";

import { VISUALLY_HIDDEN_STYLE } from "@/app/theme";
import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import {
    ErrorMessageReference,
    MessageLine,
    type MessageLineProps,
} from "@/components/MessageLine";
import { ScreenShell } from "@/components/ScreenShell";
import { ViewTransaction, type TransactionDetailResponse } from "@/lib/api/transactions";
import {
    IsCancelledRequest,
    SIGN_ON_ROUTE,
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    type MessageSeverity,
} from "@/lib/apiClient";
import { useFunctionKeys, type FunctionKeyDefinition } from "@/lib/useFunctionKeys";

const TRANSACTION_ID = "CT01";
const PROGRAM_NAME = "COTRN01C";
const SCREEN_TITLE = "View Transaction";

// TRNIDIN, the map's only unprotected field and its IC cursor position.
const TRAN_ID_MAX_LENGTH = 16;
const TRAN_ID_INPUT_ID = "transaction-view-tran-id";
const TRAN_ID_LABEL = "Enter Tran ID:";
// The cursor name the server returns for TRNIDIN (MOVE -1 TO TRNIDINL).
const TRAN_ID_FIELD = "tran_id";
// The CDEMO-CT01-TRN-SELECTED key that a CT00 S selection hands over.
const TRAN_ID_QUERY_KEY = "tranId";
// A blank TRNIDINI still reaches COTRN01C, which answers with its empty-key literal.
const BLANK_TRAN_ID = " ";
// COBOL SPACES: only blanks make a key empty, so the text itself is never trimmed.
const BLANK_TEXT_PATTERN = /^ *$/;

// F3 returns to the main menu (COMEN01C), whether the menu or a CT00 selection opened the
// screen; F5 XCTLs to COTRN00C.
const TRANSACTION_LIST_ROUTE = "/transactions";
const MAIN_MENU_ROUTE = "/menu";

// What a screen reader hears once a read fills the display fields. COTRN01C sends a blank ERRMSG
// for a successful read, so the message line stays empty and this status reports the result.
const DETAIL_SHOWN_STATUS = "Transaction details displayed";

// TCATCD receives TRAN-CAT-CD PIC 9(04), so the integer code shows zero-padded to 4 digits.
const CATEGORY_CODE_DIGITS = 4;
const HTTP_UNAUTHORIZED = 401;

// The COTRN01 rows: two fields on map rows 10, 18 and 20, three on rows 12 and 16, and the
// 60-character Description alone on row 14.
const PAIR_SIZE: GridProps["size"] = { xs: 12, md: 6 };
const TRIPLE_SIZE: GridProps["size"] = { xs: 12, md: 4 };
const FULL_SIZE: GridProps["size"] = { xs: 12 };

type DetailKey = Exclude<keyof TransactionDetailResponse, "message">;

interface DetailFieldDefinition {
    responseKey: DetailKey;
    label: string;
    maxLength: number;
    id: string;
    size: GridProps["size"];
}

// The protected COTRN01 display fields in map order, with their BMS INITIAL labels and LENGTHs.
const DETAIL_FIELDS: readonly DetailFieldDefinition[] = [
    {
        responseKey: "tran_id",
        label: "Transaction ID:",
        maxLength: TRAN_ID_MAX_LENGTH,
        id: "transaction-view-transaction-id",
        size: PAIR_SIZE,
    },
    {
        responseKey: "card_num_masked",
        label: "Card Number:",
        maxLength: 16,
        id: "transaction-view-card-number",
        size: PAIR_SIZE,
    },
    {
        responseKey: "tran_type_cd",
        label: "Type CD:",
        maxLength: 2,
        id: "transaction-view-type-cd",
        size: TRIPLE_SIZE,
    },
    {
        responseKey: "tran_cat_cd",
        label: "Category CD:",
        maxLength: CATEGORY_CODE_DIGITS,
        id: "transaction-view-category-cd",
        size: TRIPLE_SIZE,
    },
    {
        responseKey: "tran_source",
        label: "Source:",
        maxLength: 10,
        id: "transaction-view-source",
        size: TRIPLE_SIZE,
    },
    {
        responseKey: "tran_desc",
        label: "Description:",
        maxLength: 60,
        id: "transaction-view-description",
        size: FULL_SIZE,
    },
    {
        responseKey: "tran_amt",
        label: "Amount:",
        maxLength: 12,
        id: "transaction-view-amount",
        size: TRIPLE_SIZE,
    },
    {
        responseKey: "orig_ts",
        label: "Orig Date:",
        maxLength: 10,
        id: "transaction-view-orig-date",
        size: TRIPLE_SIZE,
    },
    {
        responseKey: "proc_ts",
        label: "Proc Date:",
        maxLength: 10,
        id: "transaction-view-proc-date",
        size: TRIPLE_SIZE,
    },
    {
        responseKey: "merchant_id",
        label: "Merchant ID:",
        maxLength: 9,
        id: "transaction-view-merchant-id",
        size: PAIR_SIZE,
    },
    {
        responseKey: "merchant_name",
        label: "Merchant Name:",
        maxLength: 30,
        id: "transaction-view-merchant-name",
        size: PAIR_SIZE,
    },
    {
        responseKey: "merchant_city",
        label: "Merchant City:",
        maxLength: 25,
        id: "transaction-view-merchant-city",
        size: PAIR_SIZE,
    },
    {
        responseKey: "merchant_zip",
        label: "Merchant Zip:",
        maxLength: 10,
        id: "transaction-view-merchant-zip",
        size: PAIR_SIZE,
    },
];

interface ViewFeedback {
    message: string;
    severity: MessageSeverity;
    infoMessage: string;
    fieldErrors: readonly string[];
    cursorField: string;
}

// COTRN01C blanks WS-MESSAGE at the start of every task.
const EMPTY_FEEDBACK: ViewFeedback = Object.freeze({
    message: "",
    severity: SeverityForOutcome({ isError: false, isCompletedWrite: false }),
    infoMessage: "",
    fieldErrors: [],
    cursorField: "",
});

interface ViewScreen {
    detail: TransactionDetailResponse | null;
    isBusy: boolean;
    feedback: ViewFeedback;
}

type ScreenSetter = Dispatch<SetStateAction<ViewScreen>>;
type AppRouter = ReturnType<typeof useRouter>;
type FieldRefs = Readonly<Record<string, RefObject<HTMLInputElement | null>>>;

// INITIALIZE-ALL-FIELDS, and the display fields PROCESS-ENTER-KEY moves SPACES to before the
// READ: the detail stays empty until a record is read.
const CLEARED_SCREEN: ViewScreen = Object.freeze({
    detail: null,
    isBusy: false,
    feedback: EMPTY_FEEDBACK,
});
const FETCHING_SCREEN: ViewScreen = Object.freeze({ ...CLEARED_SCREEN, isBusy: true });

// signal belongs to the read's own AbortController; once it is aborted, a newer read or no screen
// at all owns the display.
interface FetchContext {
    router: AppRouter;
    setScreen: ScreenSetter;
    signal: AbortSignal;
}

// StartRead aborts the read it replaces and returns the new read's signal; AbortRead aborts the
// read in flight, if any.
interface ReadController {
    StartRead: () => AbortSignal;
    AbortRead: () => void;
}

interface TransactionFetcher {
    Fetch: (text: string) => void;
    AbortFetch: () => void;
}

interface TransactionView extends TransactionFetcher {
    tranIdText: string;
    screen: ViewScreen;
    tranIdInputRef: RefObject<HTMLInputElement | null>;
    ChangeTranIdText: (text: string) => void;
    Clear: () => void;
}

interface ViewKeyActions {
    isBusy: boolean;
    onFetch: () => void;
    onBack: () => void;
    onClear: () => void;
    onBrowse: () => void;
}

interface KeyFieldProps {
    value: string;
    isInvalid: boolean;
    inputRef: RefObject<HTMLInputElement | null>;
    onChange: (text: string) => void;
}

interface DetailFieldsProps {
    detail: TransactionDetailResponse | null;
}

interface DetailFieldProps extends DetailFieldsProps {
    field: DetailFieldDefinition;
}

interface DetailStatusProps {
    isDetailShown: boolean;
}

interface ViewContentProps {
    view: TransactionView;
    functionKeys: readonly FunctionKeyDefinition[];
}

interface TransactionViewScreenProps {
    initialTranId: string;
}

function IsBlank(text: string): boolean {
    return BLANK_TEXT_PATTERN.test(text);
}

// Each value shows as the server sends it: the masked card number, the amount and the stored
// 26-character timestamp text are never trimmed, cut to the BMS LENGTH or re-edited here. The
// integer category code is the one exception, padded as its 9(04) receiver shows it.
function DisplayValue(detail: TransactionDetailResponse | null, responseKey: DetailKey): string {
    if (detail === null) {
        return "";
    }
    const value = detail[responseKey];
    if (responseKey === "tran_cat_cd") {
        return String(value).padStart(CATEGORY_CODE_DIGITS, "0");
    }
    return String(value ?? "");
}

// The legacy literal of a failed call, verbatim; ToScreenFeedback gives the abend text for a
// 500, a network failure or a non-legacy body, and rethrows anything that is not an axios error.
function FeedbackOfError(error: unknown): ViewFeedback {
    const screenFeedback = ToScreenFeedback(error);
    return {
        message: screenFeedback.message,
        severity: SeverityForOutcome({ isError: true, isCompletedWrite: false }),
        infoMessage: screenFeedback.info_message,
        fieldErrors: screenFeedback.field_errors,
        cursorField: screenFeedback.cursor_field ?? "",
    };
}

// A fresh object on every display, so the cursor effect returns focus to TRNIDIN each time.
function FeedbackOfDetail(detail: TransactionDetailResponse): ViewFeedback {
    return { ...EMPTY_FEEDBACK, message: detail.message ?? "" };
}

function PendingFeedback(message: string): ViewFeedback {
    return {
        ...EMPTY_FEEDBACK,
        message,
        severity: SeverityForOutcome({ isError: true, isCompletedWrite: false }),
    };
}

function EndBusy(screen: ViewScreen): ViewScreen {
    return screen.isBusy ? { ...screen, isBusy: false } : screen;
}

// An abandoned read is discarded silently, before ToScreenFeedback would show it as the abend
// text. A 401 also reaches the apiClient interceptor; routing it here keeps the sign-on redirect
// when the API module is replaced. A 403 shows its refusal literal while the interceptor returns
// to the user's own menu. Every other legacy outcome leaves the display cleared.
function ApplyFetchError(error: unknown, context: FetchContext): void {
    if (IsCancelledRequest(error)) {
        return;
    }
    if (!axios.isAxiosError(error)) {
        throw error;
    }
    if (context.signal.aborted) {
        return;
    }
    if (error.response?.status === HTTP_UNAUTHORIZED) {
        context.router.replace(SIGN_ON_ROUTE);
        return;
    }
    context.setScreen({ detail: null, isBusy: false, feedback: FeedbackOfError(error) });
}

// PROCESS-ENTER-KEY: the message and the display fields are blanked, then TRANSACT is read with
// the key exactly as typed. COTRN01C's blank-key edit runs on the server. An answer that arrives
// after its read was aborted changes neither the display nor the busy state.
async function HandleFetch(text: string, context: FetchContext): Promise<void> {
    context.setScreen(FETCHING_SCREEN);
    try {
        const detail = await ViewTransaction(text || BLANK_TRAN_ID, { signal: context.signal });
        if (!context.signal.aborted) {
            context.setScreen({ detail, isBusy: false, feedback: FeedbackOfDetail(detail) });
        }
    } catch (error) {
        ApplyFetchError(error, context);
    } finally {
        if (!context.signal.aborted) {
            context.setScreen(EndBusy);
        }
    }
}

// The 3270 keyboard is locked while the host runs the task, so ENTER and F4 wait for the
// response; F3 and F5 navigate away, abandoning a read in flight.
function BuildFunctionKeys(actions: ViewKeyActions): readonly FunctionKeyDefinition[] {
    return [
        { key: "ENTER", label: "Fetch", onPress: actions.onFetch, disabled: actions.isBusy },
        { key: "F3", label: "Back", onPress: actions.onBack },
        { key: "F4", label: "Clear", onPress: actions.onClear, disabled: actions.isBusy },
        { key: "F5", label: "Browse Tran.", onPress: actions.onBrowse },
    ];
}

// React recognises hooks only by the camelCase "use" prefix, so the hook names below are a
// framework-mandated exception to the MK_Modernization Rule's PascalCase function naming.

// Each read owns an AbortController. A new read aborts the one it replaces; F3 and F5 abort the
// one in flight as they leave, and the unmount does for any other navigation. The browser then
// stops waiting for an answer nobody will see; the server may still complete the read.
function useReadController(): ReadController {
    const controllerRef = useRef<AbortController | null>(null);
    const abortRead = useCallback(() => controllerRef.current?.abort(), []);
    useEffect(() => abortRead, [abortRead]);
    const startRead = useCallback(() => {
        abortRead();
        const controller = new AbortController();
        controllerRef.current = controller;
        return controller.signal;
    }, [abortRead]);
    return { StartRead: startRead, AbortRead: abortRead };
}

// The first entry with CDEMO-CT01-TRN-SELECTED set displays that transaction. The development
// StrictMode remount aborts the first read with its simulated unmount and runs this effect again,
// so the second read displays it; a production mount reads once.
function useInitialFetch(initialTranId: string, fetchTransaction: (text: string) => void): void {
    useEffect(() => {
        if (IsBlank(initialTranId)) {
            return;
        }
        fetchTransaction(initialTranId);
    }, [initialTranId, fetchTransaction]);
}

// A message another screen left for this one, such as a refusal literal, shows on arrival.
function usePendingMessage(setScreen: ScreenSetter): void {
    useEffect(() => {
        const pendingMessage = TakePendingMessage();
        if (pendingMessage === "") {
            return;
        }
        setScreen((screen) => ({ ...screen, feedback: PendingFeedback(pendingMessage) }));
    }, [setScreen]);
}

// MOVE -1 TO TRNIDINL: focus goes to the field the server names, otherwise to TRNIDIN, the
// map's IC field, on arrival and after every response.
function useCursorFocus(feedback: ViewFeedback, fieldRefs: FieldRefs): void {
    useEffect(() => {
        const cursorRef = fieldRefs[feedback.cursorField] ?? fieldRefs[TRAN_ID_FIELD];
        cursorRef?.current?.focus();
    }, [feedback, fieldRefs]);
}

// CLEAR-CURRENT-SCREEN: the key, the display and the message are blanked on the client, with
// the cursor back on TRNIDIN; COTRN01C reads no file for F4.
function useClearScreen(
    setTranIdText: Dispatch<SetStateAction<string>>,
    setScreen: ScreenSetter,
    tranIdInputRef: RefObject<HTMLInputElement | null>,
): () => void {
    return useCallback(() => {
        setTranIdText("");
        setScreen(CLEARED_SCREEN);
        tranIdInputRef.current?.focus();
    }, [setTranIdText, setScreen, tranIdInputRef]);
}

// A non-axios error rejects the returned promise unhandled: it is a defect, never a screen
// message.
function useFetchTransaction(setScreen: ScreenSetter): TransactionFetcher {
    const router = useRouter();
    const { StartRead, AbortRead } = useReadController();
    const fetchTransaction = useCallback(
        (text: string) => void HandleFetch(text, { router, setScreen, signal: StartRead() }),
        [router, setScreen, StartRead],
    );
    return { Fetch: fetchTransaction, AbortFetch: AbortRead };
}

// The key starts as the CT00 selection, as COTRN01C moves CDEMO-CT01-TRN-SELECTED to TRNIDINI.
function useTransactionView(initialTranId: string): TransactionView {
    const [tranIdText, setTranIdText] = useState(initialTranId);
    const [screen, setScreen] = useState<ViewScreen>(CLEARED_SCREEN);
    const tranIdInputRef = useRef<HTMLInputElement>(null);
    const fieldRefs = useMemo<FieldRefs>(() => ({ [TRAN_ID_FIELD]: tranIdInputRef }), []);
    const fetcher = useFetchTransaction(setScreen);
    const clearScreen = useClearScreen(setTranIdText, setScreen, tranIdInputRef);
    useInitialFetch(initialTranId, fetcher.Fetch);
    usePendingMessage(setScreen);
    useCursorFocus(screen.feedback, fieldRefs);
    return {
        tranIdText,
        screen,
        tranIdInputRef,
        ChangeTranIdText: setTranIdText,
        ...fetcher,
        Clear: clearScreen,
    };
}

// F3 and F5 abort the read in flight before they leave, because the route change keeps this
// screen mounted until the next screen has loaded.
function LeaveTo(route: string, view: TransactionView, router: AppRouter): void {
    view.AbortFetch();
    router.push(route);
}

function useViewFunctionKeys(view: TransactionView): readonly FunctionKeyDefinition[] {
    const router = useRouter();
    const functionKeys = BuildFunctionKeys({
        isBusy: view.screen.isBusy,
        onFetch: () => view.Fetch(view.tranIdText),
        onBack: () => LeaveTo(MAIN_MENU_ROUTE, view, router),
        onClear: view.Clear,
        onBrowse: () => LeaveTo(TRANSACTION_LIST_ROUTE, view, router),
    });
    useFunctionKeys(functionKeys);
    return functionKeys;
}

// The BMS LENGTH and, while the server names TRNIDIN in error, its tie to the ERRMSG line that
// holds the literal.
function KeyInputProps(isInvalid: boolean) {
    return { maxLength: TRAN_ID_MAX_LENGTH, ...ErrorMessageReference(isInvalid) };
}

// TRNIDIN keeps the text exactly as typed: COTRN01C neither upper-cases nor edits it beyond the
// blank test, and the BMS LENGTH is the only client-side limit.
function KeyField(props: KeyFieldProps) {
    return (
        <Grid container spacing={4}>
            <Grid size={PAIR_SIZE}>
                <TextField
                    id={TRAN_ID_INPUT_ID}
                    label={TRAN_ID_LABEL}
                    variant="outlined"
                    fullWidth
                    value={props.value}
                    error={props.isInvalid}
                    inputRef={props.inputRef}
                    onChange={(event) => props.onChange(event.target.value)}
                    slotProps={{ htmlInput: KeyInputProps(props.isInvalid) }}
                />
            </Grid>
        </Grid>
    );
}

// A protected (ASKIP) field: read-only yet focusable, so keyboard and screen-reader users reach
// every value. maxLength only exposes the BMS LENGTH; it never cuts a displayed value.
function DetailField({ field, detail }: DetailFieldProps) {
    return (
        <TextField
            id={field.id}
            label={field.label}
            variant="filled"
            fullWidth
            value={DisplayValue(detail, field.responseKey)}
            slotProps={{
                input: { readOnly: true },
                htmlInput: { maxLength: field.maxLength },
            }}
        />
    );
}

function DetailFields({ detail }: DetailFieldsProps) {
    return (
        <Grid container spacing={4}>
            {DETAIL_FIELDS.map((field) => (
                <Grid key={field.id} size={field.size}>
                    <DetailField field={field} detail={detail} />
                </Grid>
            ))}
        </Grid>
    );
}

// A polite live region that stays rendered while empty. A read clears the display first, so
// every read that fills it changes the text from empty and is announced once, without reading
// the thirteen values or moving the cursor off TRNIDIN.
function DetailStatus({ isDetailShown }: DetailStatusProps) {
    return (
        <Box role="status" aria-live="polite" sx={VISUALLY_HIDDEN_STYLE}>
            {isDetailShown ? DETAIL_SHOWN_STATUS : ""}
        </Box>
    );
}

function MessageLinePropsOf(feedback: ViewFeedback): MessageLineProps {
    return {
        message: feedback.message,
        severity: feedback.severity,
        infoMessage: feedback.infoMessage,
    };
}

function IsTranIdInvalid(feedback: ViewFeedback): boolean {
    return feedback.fieldErrors.includes(TRAN_ID_FIELD) || feedback.cursorField === TRAN_ID_FIELD;
}

// ScreenShell already stacks its children with theme spacing, so the map's parts sit directly
// inside it rather than in a second Stack. The Divider replaces the dashed row-8 separator line,
// and the four buttons replace the row-24 PF legend.
function ViewContent({ view, functionKeys }: ViewContentProps) {
    const { detail, feedback } = view.screen;
    return (
        <>
            <KeyField
                value={view.tranIdText}
                isInvalid={IsTranIdInvalid(feedback)}
                inputRef={view.tranIdInputRef}
                onChange={view.ChangeTranIdText}
            />
            <Divider />
            <DetailFields detail={detail} />
            <DetailStatus isDetailShown={detail !== null} />
            <MessageLine {...MessageLinePropsOf(feedback)} />
            <FunctionKeyBar keys={functionKeys} />
        </>
    );
}

function TransactionViewScreen({ initialTranId }: TransactionViewScreenProps) {
    const view = useTransactionView(initialTranId);
    const functionKeys = useViewFunctionKeys(view);
    return (
        <ScreenShell
            transactionId={TRANSACTION_ID}
            programName={PROGRAM_NAME}
            screenTitle={SCREEN_TITLE}
            isBusy={view.screen.isBusy}
        >
            <ViewContent view={view} functionKeys={functionKeys} />
        </ScreenShell>
    );
}

// Each arrival with another selection is a fresh COTRN01C first entry, as every XCTL is, so the
// screen state starts over whenever the tranId query parameter changes.
function TransactionViewEntry() {
    const initialTranId = useSearchParams().get(TRAN_ID_QUERY_KEY) ?? "";
    return <TransactionViewScreen key={initialTranId} initialTranId={initialTranId} />;
}

// The entry reads the tranId query parameter, so it renders inside a Suspense boundary.
export default function TransactionViewPage() {
    return (
        <Suspense fallback={null}>
            <TransactionViewEntry />
        </Suspense>
    );
}
