// Replaces: BMS map COTRN00, program COTRN00C (CT00); copybooks COTRN00 (cpy-bms), CVTRA05Y,
// COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
"use client";

import { Box, Grid, TextField, Typography } from "@mui/material";
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

import { CENTRE_ALIGNED_ROW_STYLE, VISUALLY_HIDDEN_STYLE } from "@/app/theme";
import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import { ErrorMessageReference, MessageLine } from "@/components/MessageLine";
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
    type TransactionSelectionErrorResponse,
    type TransactionSelectionResponse,
} from "@/lib/api/transactions";
import {
    ABEND_MESSAGE,
    Direction,
    IsCancelledRequest,
    LegacyErrorBody,
    SeverityForOutcome,
    TakePendingMessage,
    ToAllowedRoute,
    ToScreenFeedback,
    type MessageSeverity,
    type PageState,
    type ReadOptions,
    type RouteAllowance,
} from "@/lib/apiClient";
import {
    INVALID_KEY_MESSAGE,
    useFunctionKeys,
    type FunctionKeyDefinition,
    type InvalidKeyAction,
} from "@/lib/useFunctionKeys";

const TRANSACTION_ID = "CT00";
const PROGRAM_NAME = "COTRN00C";
const SCREEN_TITLE = "List Transactions";
// PF3 transfers to COMEN01C, the main menu that owns CT00.
const MENU_ROUTE = "/menu";
// The only page an S selection may open, with the selected transaction's tranId: VIEW_ROUTE and
// ROUTE_TRAN_ID_PARAMETER in backend/app/services/transactions/cotrn00c.py (XCTL COTRN01C).
const SELECTION_ROUTE_ALLOWANCE: RouteAllowance = {
    pathnames: ["/transactions/view"],
    queryKey: "tranId",
};
// TRNIDIN: LENGTH=16, UNPROT.
const SEARCH_FIELD_LENGTH = 16;
const SEARCH_LABEL = "Search Tran ID:";
const PAGE_LABEL = "Page:";
// The cursor_field the server names for TRNIDIN (COTRN00C's MOVE -1 TO TRNIDINL).
const SEARCH_FIELD = "search_tran_id";
const ALLOWED_SELECTION_CODES: readonly string[] = ["S"];
const SELECTION_INSTRUCTION = "Type 'S' to View Transaction details from the list";
// The TRNIDnn, TDATEnn, TDESCnn and TAMTnnn columns, keyed by the API row's field names. Each
// width, in theme spacing units, holds the wider of the heading and the BMS LENGTH (16, 8, 26 and
// 12), save that a long description wraps onto a second line rather than widen the list, which
// fits a 768 px screen without scrolling. Amounts and their heading are right-aligned, so their
// decimals line up as in the report table.
const LIST_COLUMNS: readonly SelectionColumn[] = [
    { field: "tran_id", header: "Transaction ID", widthUnits: 42 },
    { field: "tran_date", header: "Date", widthUnits: 24 },
    { field: "tran_desc", header: "Description", widthUnits: 51 },
    { field: "tran_amt", header: "Amount", widthUnits: 32, align: "right" },
];
// ERRMSG is COLOR=RED and COTRN00C never sets its colour, so every literal on this map is red.
const ERRMSG_SEVERITY: MessageSeverity = SeverityForOutcome({
    isError: true,
    isCompletedWrite: false,
});
// An empty startKey is ENTER on the server. F8 over an empty map sends CDEMO-CT00-TRNID-LAST as
// the spaces it holds, which PROCESS-PF8-KEY reads as HIGH-VALUES.
const BLANK_LAST_KEY = " ";
// The polite result status is client accessibility text, never a COTRN00C literal: a shown page
// usually carries a blank ERRMSG, so this text reports the page number, the row count and the
// first and last TRAN-ID shown, as in "Page 2: 10 transactions, 0000000000000011 to
// 0000000000000020", or "Page 0: no transactions" for a page without rows.
const RESULT_STATUS_PAGE_PREFIX = "Page";
const RESULT_STATUS_ONE_ROW = "transaction";
const RESULT_STATUS_ROWS = "transactions";
const RESULT_STATUS_NO_ROWS = "no transactions";
const RESULT_STATUS_RANGE_SEPARATOR = " to ";
const NO_RESULT_STATUS = "";

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
// echoed back unchanged and never parsed. resultStatus is set only from a 200 list page and is
// emptied whenever a request starts, so every other answer leaves it empty.
interface ListScreen {
    rows: readonly TransactionListRow[];
    pageState: PageState | null;
    feedback: ListFeedback;
    resultStatus: string;
}

// A 400 body: the four legacy keys, beside which an invalid selection code adds the page the
// browse built and whether that browse blanked TRNIDIN. The non-numeric search key's body carries
// the four keys only.
type ListRefusalBody = TransactionSelectionErrorResponse;

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
    IsLocked: () => boolean;
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

const INITIAL_SCREEN: ListScreen = {
    rows: [],
    pageState: null,
    feedback: EMPTY_FEEDBACK,
    resultStatus: NO_RESULT_STATUS,
};

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

function RowCountText(rowCount: number): string {
    return `${rowCount} ${rowCount === 1 ? RESULT_STATUS_ONE_ROW : RESULT_STATUS_ROWS}`;
}

// The page number PAGENUM shows, the row count and the TRAN-IDs of the first and last rows shown;
// only those two rows are read, so the status never reads out the whole list.
function ResultStatusText(response: TransactionListResponse): string {
    const rows = response.rows ?? [];
    const pageText = `${RESULT_STATUS_PAGE_PREFIX} ${PageNumberText(response)}`.trimEnd();
    const firstRow = rows[0];
    const lastRow = rows[rows.length - 1];
    if (firstRow === undefined || lastRow === undefined) {
        return `${pageText}: ${RESULT_STATUS_NO_ROWS}`;
    }
    const keyRange = `${firstRow.tran_id}${RESULT_STATUS_RANGE_SEPARATOR}${lastRow.tran_id}`;
    return `${pageText}: ${RowCountText(rows.length)}, ${keyRange}`;
}

function ScreenFromResponse(response: TransactionListResponse): ListScreen {
    const { page, first_key, last_key } = response;
    return {
        rows: response.rows ?? [],
        pageState: { page, first_key, last_key },
        feedback: FeedbackFromResponse(response),
        resultStatus: ResultStatusText(response),
    };
}

// Each request empties the result status, so no status describes a page while a request is out
// and a page answered again with the same text is announced again.
function WithoutResultStatus(screen: ListScreen): ListScreen {
    return { ...screen, resultStatus: NO_RESULT_STATUS };
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
        screenUpdate: {
            rows,
            pageState: { page, first_key, last_key },
            feedback,
            resultStatus: NO_RESULT_STATUS,
        },
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

// The XCTL to COTRN01C opens the route rebuilt from its checked pathname and tranId, never the
// server's own text. Any other route is an unexpected answer: the abend text in red with the
// cursor on TRNIDIN, and the rows, page number and search text kept. The feedback is built anew
// for every refusal, so each one puts the cursor back.
function FollowSelectionRoute(route: unknown, updaters: ListUpdaters): void {
    const allowedRoute = ToAllowedRoute(route, SELECTION_ROUTE_ALLOWANCE);
    if (allowedRoute === null) {
        const feedback = { ...EMPTY_FEEDBACK, message: ABEND_MESSAGE, cursorField: SEARCH_FIELD };
        updaters.setScreen((screen) => ({ ...screen, feedback }));
        return;
    }
    updaters.Navigate(allowedRoute);
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
        FollowSelectionRoute(response.route, updaters);
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

// WHEN OTHER: MOVE 'Y' TO WS-ERR-FLG, MOVE -1 TO TRNIDINL and CCDA-MSG-INVALID-KEY in red. No
// file is read, so the rows, page number, selection codes and search text stay as shown; the
// answer is no list page, so the result status is emptied. A fresh feedback object puts the
// cursor back on TRNIDIN at every press. Like ENTER, F7 and F8, the key is refused while a
// request holds the lock.
function ShowInvalidKey(runner: ListRequestRunner, updaters: ListUpdaters): void {
    if (runner.IsLocked()) {
        return;
    }
    const feedback = { ...EMPTY_FEEDBACK, message: INVALID_KEY_MESSAGE, cursorField: SEARCH_FIELD };
    updaters.setScreen((screen) => ({ ...WithoutResultStatus(screen), feedback }));
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
    runner.updaters.setScreen(WithoutResultStatus);
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
            IsLocked: () => readControllerRef.current !== null,
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

// The screen's WHEN OTHER action, inactive while a request is out as ENTER, F7 and F8 are.
function useInvalidKeyAction(state: ListState, runner: ListRequestRunner): InvalidKeyAction {
    const { isBusy, updaters } = state;
    return useMemo(
        () => ({ onPress: () => ShowInvalidKey(runner, updaters), disabled: isBusy }),
        [runner, updaters, isBusy],
    );
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
    const invalidKeyAction = useInvalidKeyAction(state, runner);
    const searchInputRef = useCursorFocus(state.screen.feedback);
    useFirstPage(runner);
    useFunctionKeys(keys, invalidKeyAction);
    return { ...state, keys, searchInputRef };
}

// The typed TRNIDIN text is sent unchanged: the server, not the page, decides whether it is
// blank, numeric or a TRAN-ID to browse from, and whether COTRN00C's MOVE SPACE TO TRNIDINO
// blanked the field. An answer with search_cleared, the 400 of an invalid selection code
// included, empties it, so the next ENTER browses from the lowest key; any other answer or
// failed call keeps the text as keyed. The field is read-only while a request is out, as the
// 3270 keyboard lock, so no text keyed meanwhile is lost. While the server names TRNIDIN in
// field_errors, the field is described by the ERRMSG line that holds its literal.
function SearchField({ view }: ListViewProps) {
    const isFlagged = view.screen.feedback.fieldErrors.includes(SEARCH_FIELD);
    const htmlInput = { maxLength: SEARCH_FIELD_LENGTH, readOnly: view.isBusy };
    return (
        <TextField
            label={SEARCH_LABEL}
            variant="outlined"
            fullWidth
            value={view.searchTranId}
            error={isFlagged}
            inputRef={view.searchInputRef}
            onChange={(event) => view.setSearchTranId(event.target.value)}
            slotProps={{ htmlInput: { ...htmlInput, ...ErrorMessageReference(isFlagged) } }}
        />
    );
}

// The Page: label and PAGENUM in one labelLarge line, the one type role /cards gives its page.
function PageNumber({ view }: ListViewProps) {
    const pageText = `${PAGE_LABEL} ${PageNumberText(view.screen.pageState)}`.trimEnd();
    return (
        <Typography variant="labelLarge" component="p">
            {pageText}
        </Typography>
    );
}

// A visually hidden polite live region of its own, apart from MessageLine's ERRMSG alert, which
// announces refusals and literals. It stays rendered while empty, so each shown page changes its
// text from empty and is announced once, without reading the rows or moving the cursor.
function ResultStatus({ view }: ListViewProps) {
    return (
        <Box role="status" aria-live="polite" sx={VISUALLY_HIDDEN_STYLE}>
            {view.screen.resultStatus}
        </Box>
    );
}

// The search field and the page number share one row, centred on each other.
function SearchBar({ view }: ListViewProps) {
    return (
        <Grid container spacing={4} sx={CENTRE_ALIGNED_ROW_STYLE}>
            <Grid size={{ xs: 12, md: 6 }}>
                <SearchField view={view} />
            </Grid>
            <Grid size={{ xs: 12, md: 6 }}>
                <PageNumber view={view} />
                <ResultStatus view={view} />
            </Grid>
        </Grid>
    );
}

// At most 10 rows (SEL0001-SEL0010), each with its Sel code. Amounts show as the server's decimal
// string rather than in TAMT's +99999999.99 picture. The codes are read-only while a request is
// out, as TRNIDIN is, so no code keyed meanwhile is cleared by that request's answer.
function TransactionRows({ view }: ListViewProps) {
    return (
        <SelectionTable
            columns={LIST_COLUMNS}
            rows={ToSelectionRows(view.screen.rows, view.selectionCodes)}
            allowedCodes={ALLOWED_SELECTION_CODES}
            onSelectionChange={view.onSelectionChange}
            label={SCREEN_TITLE}
            readOnly={view.isBusy}
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
