// Replaces: BMS map COTRN00, program COTRN00C (CT00); copybooks COTRN00 (cpy-bms), CVTRA05Y,
// COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
"use client";

import { Grid, Stack, TextField, Typography, type SxProps, type Theme } from "@mui/material";
import axios from "axios";
import { useRouter } from "next/navigation";
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

import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import { MessageLine } from "@/components/MessageLine";
import { ScreenShell } from "@/components/ScreenShell";
import {
    SelectionTable,
    type SelectionColumn,
    type SelectionRow,
} from "@/components/SelectionTable";
import {
    ListTransactions,
    SelectTransactions,
    type TransactionListQuery,
    type TransactionListResponse,
    type TransactionListRow,
    type TransactionSelection,
    type TransactionSelectionResponse,
} from "@/lib/api/transactions";
import {
    Direction,
    IsCancelledRequest,
    LegacyErrorBody,
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    type LegacyErrorResponse,
    type MessageSeverity,
    type PageState,
    type ReadOptions,
} from "@/lib/apiClient";
import { useFunctionKeys, type FunctionKeyDefinition } from "@/lib/useFunctionKeys";

const TRANSACTION_ID = "CT00";
const PROGRAM_NAME = "COTRN00C";
const SCREEN_TITLE = "List Transactions";
// PF3 transfers to COMEN01C, the main menu that owns CT00.
const MENU_ROUTE = "/menu";
// TRNIDIN: LENGTH=16, UNPROT.
const SEARCH_FIELD_LENGTH = 16;
const SEARCH_LABEL = "Search Tran ID:";
const PAGE_LABEL = "Page:";
// The cursor_field the server names for TRNIDIN (COTRN00C's MOVE -1 TO TRNIDINL).
const SEARCH_FIELD = "search_tran_id";
const ALLOWED_SELECTION_CODES: readonly string[] = ["S"];
const SELECTION_INSTRUCTION = "Type 'S' to View Transaction details from the list";
// The TRNIDnn, TDATEnn, TDESCnn and TAMTnnn columns, keyed by the API row's field names.
const LIST_COLUMNS: readonly SelectionColumn[] = [
    { field: "tran_id", header: "Transaction ID" },
    { field: "tran_date", header: "Date" },
    { field: "tran_desc", header: "Description" },
    { field: "tran_amt", header: "Amount" },
];
// ERRMSG is COLOR=RED and COTRN00C never sets its colour, so every literal on this map is red.
const ERRMSG_SEVERITY: MessageSeverity = SeverityForOutcome({
    isError: true,
    isCompletedWrite: false,
});
// An empty startKey is ENTER on the server. F8 over an empty map sends CDEMO-CT00-TRNID-LAST as
// the spaces it holds, which PROCESS-PF8-KEY reads as HIGH-VALUES.
const BLANK_LAST_KEY = " ";

// The search field and the page number share one row, centred on each other.
const SEARCH_ROW_STYLE: SxProps<Theme> = { alignItems: "center" };

type SelectionCodes = Readonly<Record<string, string>>;

// The ERRMSG line and the cursor of the last response.
interface ListFeedback {
    message: string;
    severity: MessageSeverity;
    infoMessage: string;
    fieldErrors: readonly string[];
    cursorField: string | null;
}

// The rows shown and the CDEMO-CT00-INFO keys the server returned with them; the keys are
// echoed back unchanged and never parsed.
interface ListScreen {
    rows: readonly TransactionListRow[];
    pageState: PageState | null;
    feedback: ListFeedback;
}

// A 400 body: the four legacy keys, beside which an invalid selection code adds the page the
// browse built and whether that browse blanked TRNIDIN. The non-numeric search key's body carries
// the four keys only.
interface ListRefusalBody extends LegacyErrorResponse, PageState {
    rows?: TransactionListRow[];
    search_cleared?: boolean;
}

// Every request is offered the signal of the runner's current controller; only the
// ListTransactions reads forward it, so the ENTER selection is never aborted.
type ListRequest = (readOptions: ReadOptions) => Promise<TransactionSelectionResponse>;

type ListOutcome =
    | { response: TransactionSelectionResponse }
    | { screenUpdate: SetStateAction<ListScreen>; searchCleared: boolean };

interface ListUpdaters {
    setScreen: Dispatch<SetStateAction<ListScreen>>;
    setSelectionCodes: Dispatch<SetStateAction<SelectionCodes>>;
    setIsBusy: Dispatch<SetStateAction<boolean>>;
    setSearchTranId: Dispatch<SetStateAction<string>>;
    Navigate: (route: string) => void;
}

type ScreenSetters = Omit<ListUpdaters, "Navigate">;

interface ListRequestRunner {
    Run: (request: ListRequest) => Promise<void>;
    Invalidate: () => void;
    AbortRead: () => void;
}

// The number of the latest request, the AbortController offered to the request in flight (null
// while none is, so it is also the keyboard lock), and where its answer goes.
interface RequestRunnerContext {
    latestRequestRef: RefObject<number>;
    readControllerRef: RefObject<AbortController | null>;
    updaters: ListUpdaters;
}

interface ListRequestInput {
    screen: ListScreen;
    searchTranId: string;
    selectionCodes: SelectionCodes;
}

interface ListActions {
    HandleEnter: () => void;
    HandleBack: () => void;
    HandlePageBackward: () => void;
    HandlePageForward: () => void;
}

interface ListState extends ListRequestInput {
    isBusy: boolean;
    updaters: ListUpdaters;
    setSearchTranId: Dispatch<SetStateAction<string>>;
    onSelectionChange: (rowKey: string, code: string) => void;
}

interface TransactionListView extends ListState {
    keys: readonly FunctionKeyDefinition[];
    searchInputRef: RefObject<HTMLInputElement | null>;
}

interface ListViewProps {
    view: TransactionListView;
}

const EMPTY_FEEDBACK: ListFeedback = {
    message: "",
    severity: ERRMSG_SEVERITY,
    infoMessage: "",
    fieldErrors: [],
    cursorField: null,
};

const INITIAL_SCREEN: ListScreen = { rows: [], pageState: null, feedback: EMPTY_FEEDBACK };

function FeedbackFromResponse(response: TransactionListResponse): ListFeedback {
    return {
        message: response.message ?? "",
        severity: ERRMSG_SEVERITY,
        infoMessage: "",
        fieldErrors: [],
        cursorField: response.cursor_field ?? null,
    };
}

// The 400 edit-failure literals (Invalid selection. Valid value is S, Tran ID must be
// Numeric ...), the 503 file-error literal (Unable to lookup transaction...), or the abend text
// that ToScreenFeedback gives a 500 or a network failure.
function FeedbackFromError(error: unknown): ListFeedback {
    const feedback = ToScreenFeedback(error);
    return {
        message: feedback.message,
        severity: ERRMSG_SEVERITY,
        infoMessage: feedback.info_message,
        fieldErrors: feedback.field_errors,
        cursorField: feedback.cursor_field,
    };
}

function ScreenFromResponse(response: TransactionListResponse): ListScreen {
    const { page, first_key, last_key } = response;
    return {
        rows: response.rows ?? [],
        pageState: { page, first_key, last_key },
        feedback: FeedbackFromResponse(response),
    };
}

// The 400 of an invalid selection code shows the page the browse built, as COTRN00C sends the
// rebuilt map under the literal, and reports whether that browse blanked TRNIDIN. Every other
// failure, the four-key 400 of a non-numeric search key included, re-sends the map as it stood,
// so the rows, paging keys and search text stay.
function RefusalOutcome(error: unknown): ListOutcome {
    const feedback = FeedbackFromError(error);
    const body = LegacyErrorBody<ListRefusalBody>(error);
    if (body === null || body.rows === undefined) {
        return { screenUpdate: (screen) => ({ ...screen, feedback }), searchCleared: false };
    }
    const { rows, page, first_key, last_key } = body;
    return {
        screenUpdate: { rows, pageState: { page, first_key, last_key }, feedback },
        searchCleared: body.search_cleared === true,
    };
}

// A read abandoned through its signal settles as null, so nothing of it is shown; it is checked
// first because axios's CanceledError is itself an axios error, which ToScreenFeedback would show
// as the abend text. Only other axios errors are screen outcomes; any other error propagates.
async function SettleRequest(
    request: ListRequest,
    readOptions: ReadOptions,
): Promise<ListOutcome | null> {
    try {
        return { response: await request(readOptions) };
    } catch (error) {
        if (IsCancelledRequest(error)) {
            return null;
        }
        if (!axios.isAxiosError(error)) {
            throw error;
        }
        return RefusalOutcome(error);
    }
}

// A 200 answer is a route or a list page; a rejected call updates the screen as its body allows.
// The selection codes are cleared after every answer, so no code carries over onto the rows of
// another page. The search text is blanked only when the answer reports search_cleared. A
// cancelled read (null) only unlocks the keyboard, so the keys never stay locked behind it.
function ApplyOutcome(outcome: ListOutcome | null, updaters: ListUpdaters): void {
    updaters.setIsBusy(false);
    if (outcome === null) {
        return;
    }
    updaters.setSelectionCodes({});
    if ("screenUpdate" in outcome) {
        updaters.setScreen(outcome.screenUpdate);
        ApplySearchCleared(outcome.searchCleared, updaters);
        return;
    }
    const { response } = outcome;
    if (response.route) {
        updaters.Navigate(response.route);
        return;
    }
    updaters.setScreen(ScreenFromResponse(response));
    ApplySearchCleared(response.search_cleared === true, updaters);
}

// COTRN00C's MOVE SPACE TO TRNIDINO: an answer that blanked TRNIDIN empties the search field.
function ApplySearchCleared(searchCleared: boolean, updaters: ListUpdaters): void {
    if (searchCleared) {
        updaters.setSearchTranId("");
    }
}

// Every shown row with a code, in screen order; the server takes the first, as the EVALUATE
// over SEL0001-SEL0010 does.
function BuildSelections(
    rows: readonly TransactionListRow[],
    selectionCodes: SelectionCodes,
): TransactionSelection[] {
    return rows
        .map((row) => ({ tran_id: row.tran_id, selection: selectionCodes[row.tran_id] ?? "" }))
        .filter((selection) => selection.selection !== "");
}

// ENTER: the selection codes, then the TRNIDIN search key exactly as typed.
function SelectionRequestFor(input: ListRequestInput): ListRequest {
    const { screen, searchTranId, selectionCodes } = input;
    return () =>
        SelectTransactions({
            selections: BuildSelections(screen.rows, selectionCodes),
            search_tran_id: searchTranId,
            first_key: screen.pageState?.first_key,
            page: screen.pageState?.page,
        });
}

// F7 (PROCESS-PF7-KEY) reads backward from the first key shown.
function BackwardQuery(pageState: PageState | null): TransactionListQuery {
    return { startKey: pageState?.first_key, direction: Direction.BACKWARD, page: pageState?.page };
}

// F8 (PROCESS-PF8-KEY) reads forward after the last key shown; the first key lets the server
// re-read the shown rows when there is no next page.
function ForwardQuery(pageState: PageState | null): TransactionListQuery {
    return {
        startKey: pageState?.last_key || BLANK_LAST_KEY,
        firstKey: pageState?.first_key,
        direction: Direction.FORWARD,
        page: pageState?.page,
    };
}

// F7 and F8 are reads, so they forward the runner's signal and are aborted when replaced.
function ReadRequestFor(query: TransactionListQuery): ListRequest {
    return (readOptions) => ListTransactions(query, readOptions);
}

function BuildListActions(
    input: ListRequestInput,
    runner: ListRequestRunner,
    updaters: ListUpdaters,
): ListActions {
    const { pageState } = input.screen;
    return {
        HandleEnter: () => void runner.Run(SelectionRequestFor(input)),
        HandleBack: () => {
            runner.Invalidate();
            updaters.Navigate(MENU_ROUTE);
        },
        HandlePageBackward: () => void runner.Run(ReadRequestFor(BackwardQuery(pageState))),
        HandlePageForward: () => void runner.Run(ReadRequestFor(ForwardQuery(pageState))),
    };
}

// The legend ENTER=Continue  F3=Back  F7=Backward  F8=Forward. No key is disabled except while a
// request is out: the program answers every press, the paging literals included, but ENTER, F7
// and F8 stay locked until it has answered, as the 3270 keyboard does. F3 stays available.
function BuildFunctionKeys(actions: ListActions, isBusy: boolean): FunctionKeyDefinition[] {
    return [
        { key: "ENTER", label: "Continue", onPress: actions.HandleEnter, disabled: isBusy },
        { key: "F3", label: "Back", onPress: actions.HandleBack },
        { key: "F7", label: "Backward", onPress: actions.HandlePageBackward, disabled: isBusy },
        { key: "F8", label: "Forward", onPress: actions.HandlePageForward, disabled: isBusy },
    ];
}

// A message carried across a navigation shows on the first page only when the server sends no
// literal of its own, so no server literal is replaced.
function WithPendingMessage(
    response: TransactionListResponse,
    pendingMessage: string,
): TransactionListResponse {
    if (pendingMessage === "" || (response.message ?? "") !== "") {
        return response;
    }
    return { ...response, message: pendingMessage };
}

// PAGENUM shows the page number the server returns, blank until the first page arrives.
function PageNumberText(pageState: PageState | null): string {
    return pageState?.page === undefined ? "" : String(pageState.page);
}

function ToSelectionRows(
    rows: readonly TransactionListRow[],
    selectionCodes: SelectionCodes,
): SelectionRow[] {
    return rows.map((row) => ({
        rowKey: row.tran_id,
        cells: {
            tran_id: row.tran_id,
            tran_date: row.tran_date,
            tran_desc: row.tran_desc,
            tran_amt: row.tran_amt,
        },
        selectedCode: selectionCodes[row.tran_id] ?? "",
    }));
}

// A settled request frees the lock only while it still holds it: a read that unmount already
// aborted and released settles later, and must not free the lock of the request after it.
function ReleaseLock(
    readControllerRef: RefObject<AbortController | null>,
    readController: AbortController,
): void {
    if (readControllerRef.current === readController) {
        readControllerRef.current = null;
    }
}

// Unmount aborts the read in flight and frees the lock at once, so Strict Mode's re-run of the
// first-page effect reads anew instead of being refused by the read it abandoned.
function AbortAndRelease(readControllerRef: RefObject<AbortController | null>): void {
    readControllerRef.current?.abort();
    readControllerRef.current = null;
}

// The 3270 keyboard stays locked from a press until the program answers. Here ENTER, F7 and F8
// are disabled while a request is out, and a second request is refused while one holds the
// controller: the disabled keys take effect a render after the press, so a second press in the
// same frame is stopped by the lock, and no live read is ever superseded. The lock clears when
// the request settles, a rethrown defect included. Only the latest request's answer is applied,
// so an answer Invalidate discarded never replaces the screen or navigates away.
async function RunExclusive(request: ListRequest, runner: RequestRunnerContext): Promise<void> {
    if (runner.readControllerRef.current !== null) {
        return;
    }
    const readController = new AbortController();
    runner.readControllerRef.current = readController;
    runner.latestRequestRef.current += 1;
    const requestNumber = runner.latestRequestRef.current;
    runner.updaters.setIsBusy(true);
    try {
        const outcome = await SettleRequest(request, { signal: readController.signal });
        if (requestNumber === runner.latestRequestRef.current) {
            ApplyOutcome(outcome, runner.updaters);
        }
    } finally {
        ReleaseLock(runner.readControllerRef, readController);
    }
}

// React recognises hooks only by the camelCase "use" prefix, so every hook name below is a
// framework-mandated exception to the MK_Modernization Rule's PascalCase function naming.

// F3 (Invalidate) aborts the read in flight and discards any answer still to come; a selection is
// never aborted, only discarded, and the lock clears when it settles. Unmount (AbortRead) aborts
// the read in flight and frees the lock, and Strict Mode's re-run of the first-page effect reads
// anew.
function useListRequestRunner(updaters: ListUpdaters): ListRequestRunner {
    const latestRequestRef = useRef(0);
    const readControllerRef = useRef<AbortController | null>(null);
    const runner = useMemo<ListRequestRunner>(() => {
        const context: RequestRunnerContext = { latestRequestRef, readControllerRef, updaters };
        return {
            Run: (request) => RunExclusive(request, context),
            Invalidate: () => {
                latestRequestRef.current += 1;
                readControllerRef.current?.abort();
            },
            AbortRead: () => AbortAndRelease(readControllerRef),
        };
    }, [updaters]);
    useEffect(() => runner.AbortRead, [runner]);
    return runner;
}

// The updaters never change identity, so the first page is requested once per mount: the
// router is read through a ref rather than being a dependency.
function useListUpdaters(setters: ScreenSetters): ListUpdaters {
    const router = useRouter();
    const routerRef = useRef(router);
    useEffect(() => {
        routerRef.current = router;
    }, [router]);
    const { setScreen, setSelectionCodes, setIsBusy, setSearchTranId } = setters;
    return useMemo<ListUpdaters>(
        () => ({
            setScreen,
            setSelectionCodes,
            setIsBusy,
            setSearchTranId,
            Navigate: (route) => routerRef.current.push(route),
        }),
        [setScreen, setSelectionCodes, setIsBusy, setSearchTranId],
    );
}

function useSelectionChange(
    setSelectionCodes: Dispatch<SetStateAction<SelectionCodes>>,
): (rowKey: string, code: string) => void {
    return useCallback(
        (rowKey: string, code: string) => {
            setSelectionCodes((codes) => ({ ...codes, [rowKey]: code }));
        },
        [setSelectionCodes],
    );
}

function useListState(): ListState {
    const [searchTranId, setSearchTranId] = useState("");
    const [screen, setScreen] = useState<ListScreen>(INITIAL_SCREEN);
    const [selectionCodes, setSelectionCodes] = useState<SelectionCodes>({});
    // The first page is requested on mount, so the screen starts busy.
    const [isBusy, setIsBusy] = useState(true);
    const updaters = useListUpdaters({ setScreen, setSelectionCodes, setIsBusy, setSearchTranId });
    const onSelectionChange = useSelectionChange(setSelectionCodes);
    return {
        screen,
        searchTranId,
        selectionCodes,
        isBusy,
        updaters,
        setSearchTranId,
        onSelectionChange,
    };
}

function useListKeys(state: ListState, runner: ListRequestRunner): FunctionKeyDefinition[] {
    const { screen, searchTranId, selectionCodes, isBusy, updaters } = state;
    return useMemo(() => {
        const input: ListRequestInput = { screen, searchTranId, selectionCodes };
        return BuildFunctionKeys(BuildListActions(input, runner, updaters), isBusy);
    }, [screen, searchTranId, selectionCodes, isBusy, runner, updaters]);
}

// The first entry (EIBCALEN > 0, not re-entered) browses from the lowest TRAN-ID. Under React
// Strict Mode the effect runs twice: the cleanup between the runs aborts the first browse and frees
// the lock, so the second browse is the one answered, and the ref keeps the pending message the
// first run took.
function useFirstPage(runner: ListRequestRunner): void {
    const pendingMessageRef = useRef<string | null>(null);
    useEffect(() => {
        if (pendingMessageRef.current === null) {
            pendingMessageRef.current = TakePendingMessage();
        }
        const pendingMessage = pendingMessageRef.current;
        void runner.Run(async (readOptions) =>
            WithPendingMessage(await ListTransactions({}, readOptions), pendingMessage),
        );
    }, [runner]);
}

// The server's cursor_field replaces MOVE -1 TO TRNIDINL; each answer puts the cursor back.
function useCursorFocus(feedback: ListFeedback): RefObject<HTMLInputElement | null> {
    const searchInputRef = useRef<HTMLInputElement | null>(null);
    const fieldRefs = useMemo<Record<string, RefObject<HTMLInputElement | null>>>(
        () => ({ [SEARCH_FIELD]: searchInputRef }),
        [],
    );
    useEffect(() => {
        if (feedback.cursorField !== null) {
            fieldRefs[feedback.cursorField]?.current?.focus();
        }
    }, [feedback, fieldRefs]);
    return searchInputRef;
}

function useTransactionList(): TransactionListView {
    const state = useListState();
    const runner = useListRequestRunner(state.updaters);
    const keys = useListKeys(state, runner);
    const searchInputRef = useCursorFocus(state.screen.feedback);
    useFirstPage(runner);
    useFunctionKeys(keys);
    return { ...state, keys, searchInputRef };
}

// The typed TRNIDIN text is sent unchanged: the server, not the page, decides whether it is
// blank, numeric or a TRAN-ID to browse from, and whether COTRN00C's MOVE SPACE TO TRNIDINO
// blanked the field. An answer with search_cleared, the 400 of an invalid selection code
// included, empties it, so the next ENTER browses from the lowest key; any other answer or
// failed call keeps the text as keyed. The field is read-only while a request is out, as the
// 3270 keyboard lock, so no text keyed meanwhile is lost.
function SearchField({ view }: ListViewProps) {
    return (
        <TextField
            label={SEARCH_LABEL}
            variant="outlined"
            fullWidth
            value={view.searchTranId}
            error={view.screen.feedback.fieldErrors.includes(SEARCH_FIELD)}
            inputRef={view.searchInputRef}
            onChange={(event) => view.setSearchTranId(event.target.value)}
            slotProps={{ htmlInput: { maxLength: SEARCH_FIELD_LENGTH, readOnly: view.isBusy } }}
        />
    );
}

function PageNumber({ view }: ListViewProps) {
    return (
        <Stack direction="row" spacing={2} useFlexGap>
            <Typography variant="labelLarge" component="p">
                {PAGE_LABEL}
            </Typography>
            <Typography variant="bodyLarge" component="p">
                {PageNumberText(view.screen.pageState)}
            </Typography>
        </Stack>
    );
}

function SearchBar({ view }: ListViewProps) {
    return (
        <Grid container spacing={4} sx={SEARCH_ROW_STYLE}>
            <Grid size={{ xs: 12, md: 6 }}>
                <SearchField view={view} />
            </Grid>
            <Grid size={{ xs: 12, md: 6 }}>
                <PageNumber view={view} />
            </Grid>
        </Grid>
    );
}

// At most 10 rows (SEL0001-SEL0010), each with its Sel code. Amounts show as the server's decimal
// string rather than in TAMT's +99999999.99 picture.
function TransactionRows({ view }: ListViewProps) {
    return (
        <SelectionTable
            columns={LIST_COLUMNS}
            rows={ToSelectionRows(view.screen.rows, view.selectionCodes)}
            allowedCodes={ALLOWED_SELECTION_CODES}
            onSelectionChange={view.onSelectionChange}
        />
    );
}

function TransactionListScreen() {
    const view = useTransactionList();
    const { message, severity, infoMessage } = view.screen.feedback;
    return (
        <ScreenShell
            transactionId={TRANSACTION_ID}
            programName={PROGRAM_NAME}
            screenTitle={SCREEN_TITLE}
            isBusy={view.isBusy}
        >
            <SearchBar view={view} />
            <TransactionRows view={view} />
            <Typography variant="bodyMedium" component="p">
                {SELECTION_INSTRUCTION}
            </Typography>
            <MessageLine message={message} severity={severity} infoMessage={infoMessage} />
            <FunctionKeyBar keys={view.keys} />
        </ScreenShell>
    );
}

// The client hooks render inside a Suspense boundary, which Next.js requires of client pages
// during static prerendering.
export default function TransactionListPage() {
    return (
        <Suspense fallback={null}>
            <TransactionListScreen />
        </Suspense>
    );
}
