// Replaces: BMS map COCRDLI, program COCRDLIC (CCLI); copybooks COCRDLI (cpy-bms),
// CVACT02Y, CVCRD01Y, CSUSR01Y, COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
"use client";

import { Box, Grid, TextField, Typography } from "@mui/material";
import axios, { type AxiosError } from "axios";
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
import { ErrorMessageReference, MessageLine } from "@/components/MessageLine";
import { ScreenShell } from "@/components/ScreenShell";
import {
    SelectionLabelFor,
    SelectionTable,
    type SelectionColumn,
    type SelectionRow,
} from "@/components/SelectionTable";
import {
    ListCards,
    SelectCards,
    type CardListQuery,
    type CardListResponse,
    type CardListRow,
    type CardSelectionAction,
    type CardSelectionResponse,
    type CardSelectionRow,
} from "@/lib/api/cards";
import {
    Direction,
    IsCancelledRequest,
    LegacyErrorBody,
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    type LegacyErrorResponse,
    type MessageSeverity,
    type OutcomeFlags,
    type PageState,
    type ScreenFeedback,
} from "@/lib/apiClient";
import { useFunctionKeys, type FunctionKeyDefinition, type PfKey } from "@/lib/useFunctionKeys";

const TRANSACTION_ID = "CCLI";
const PROGRAM_NAME = "COCRDLIC";
// The BMS INITIAL texts of COCRDLI, verbatim.
const SCREEN_TITLE = "List Credit Cards";
const ACCOUNT_FILTER_LABEL = "Account Number    :";
const CARD_FILTER_LABEL = "Credit Card Number:";
const ACCOUNT_FILTER_MAX_LENGTH = 11;
const CARD_FILTER_MAX_LENGTH = 16;
const SELECTION_HEADER = "Select";
const SELECTION_CODES = ["S", "U"] as const;
const PAGE_LABEL = "Page";
const MENU_ROUTE = "/menu";
const VIEW_ROUTE = "/cards/view";
const UPDATE_ROUTE = "/cards/update";

// The PF-key labels; FunctionKeyBar appends the key name, as in "Backward (F7)".
const ENTER_LABEL = "Continue";
const EXIT_LABEL = "Exit";
const BACKWARD_LABEL = "Backward";
const FORWARD_LABEL = "Forward";

// The field names the server reports in field_errors and cursor_field for ACCTSID and CARDSID.
const ACCT_ID_FIELD = "acct_id";
const CARD_NUM_FIELD = "card_num";

// The column headings of COCRDLI row 9, trimmed of their padding blanks.
const CARD_LIST_COLUMNS: readonly SelectionColumn[] = [
    { field: "acct_id", header: "Account Number" },
    { field: "card_num_masked", header: "Card Number" },
    { field: "active_status", header: "Active" },
];

// The first entry from the menu reads the first page with no filters.
const FIRST_ENTRY_QUERY: CardListQuery = {};

// COCRDLIC never changes the BMS colour of ERRMSG (COLOR=RED) and writes no record, so every
// message it sets, a paging literal on a 200 included, is a red error line.
const ERRMSG_OUTCOME: OutcomeFlags = { isError: true, isCompletedWrite: false };

// cursor_field names a row's selection code as rows[<0-based index>].selection.
const SELECTION_CURSOR_PATTERN = /^rows\[(\d+)\]\.selection$/;

const BLANK_CODE = "";

type FilterFieldName = typeof ACCT_ID_FIELD | typeof CARD_NUM_FIELD;

type CardFilterTexts = Record<FilterFieldName, string>;

type FilterInputs = Record<string, HTMLInputElement | null>;

type AppRouter = ReturnType<typeof useRouter>;

// The controller of the list read in flight: the first page, an ENTER, F7 or F8 read, or the
// re-read after a selection the server answers with no action.
type ListReadRef = RefObject<AbortController | null>;

interface FilterFieldDefinition {
    name: FilterFieldName;
    label: string;
    maxLength: number;
    autoFocus: boolean;
}

// ACCTSID carries the initial cursor (BMS IC); CARDSID follows it.
const FILTER_FIELDS: readonly FilterFieldDefinition[] = [
    {
        name: ACCT_ID_FIELD,
        label: ACCOUNT_FILTER_LABEL,
        maxLength: ACCOUNT_FILTER_MAX_LENGTH,
        autoFocus: true,
    },
    {
        name: CARD_NUM_FIELD,
        label: CARD_FILTER_LABEL,
        maxLength: CARD_FILTER_MAX_LENGTH,
        autoFocus: false,
    },
];

const EMPTY_FILTERS: CardFilterTexts = { acct_id: "", card_num: "" };

// The rows shown, the page position the server returned, and the CRDSELn code keyed on each
// row. Codes are kept by row position, as WS-EDIT-SELECT holds them, because every response
// issues fresh card_ref tokens.
interface ListState {
    rows: readonly CardListRow[];
    pageState: PageState | null;
    selectionCodes: readonly string[];
}

const EMPTY_LIST: ListState = { rows: [], pageState: null, selectionCodes: [] };

interface ScreenMessages {
    message: string;
    severity: MessageSeverity;
    infoMessage: string;
    fieldErrors: readonly string[];
    cursorField: string;
}

const NO_MESSAGES: ScreenMessages = {
    message: "",
    severity: SeverityForOutcome(ERRMSG_OUTCOME),
    infoMessage: "",
    fieldErrors: [],
    cursorField: "",
};

// A refused selection redisplays rows and page keys, and on GET /cards the page number after the
// redisplay; a refused filter carries none of them.
interface CardListErrorBody extends LegacyErrorResponse {
    rows?: CardListRow[];
    first_key?: string;
    last_key?: string;
    page?: number;
}

interface ScreenSetters {
    setList: Dispatch<SetStateAction<ListState>>;
    setMessages: Dispatch<SetStateAction<ScreenMessages>>;
    setIsBusy: Dispatch<SetStateAction<boolean>>;
}

// Each screen call takes a ticket. Only the latest ticket, while the page is mounted, may change
// the screen, navigate or end the busy state, so a slower earlier response never replaces a
// newer page or its keys.
interface RequestGate {
    Begin: () => number;
    IsCurrent: (ticket: number) => boolean;
}

interface ScreenCalls {
    setters: ScreenSetters;
    gate: RequestGate;
}

// What a settled call does to the screen, applied only when its ticket is still current.
type ScreenUpdate = () => void;

interface ScreenContext extends ScreenCalls {
    filters: CardFilterTexts;
    list: ListState;
    router: AppRouter;
    listReadRef: ListReadRef;
}

interface CursorTargets {
    fieldInputs: FilterInputs;
    tableElement: HTMLElement | null;
}

const SELECTION_ROUTES: Readonly<Record<CardSelectionAction, string>> = {
    view: VIEW_ROUTE,
    update: UPDATE_ROUTE,
};

// The server's text is shown verbatim; an absent key or a null cursor becomes empty.
function MessagesFrom(feedback: ScreenFeedback): ScreenMessages {
    return {
        message: feedback.message ?? "",
        severity: SeverityForOutcome(ERRMSG_OUTCOME),
        infoMessage: feedback.info_message ?? "",
        fieldErrors: feedback.field_errors ?? [],
        cursorField: feedback.cursor_field ?? "",
    };
}

// A new page keeps each selection code on its row position, as 1200-SCREEN-ARRAY-INIT moves
// WS-EDIT-SELECT(n) to CRDSELnO of every row it sends; rows the new page lacks drop their codes.
function ListStateFrom(response: CardListResponse, selectionCodes: readonly string[]): ListState {
    return {
        rows: response.rows,
        pageState: {
            page: response.page,
            first_key: response.first_key,
            last_key: response.last_key,
        },
        selectionCodes: selectionCodes.slice(0, response.rows.length),
    };
}

// A GET /cards refusal carries the page shown after its redisplay; a POST /cards/selection
// refusal carries none, so the page shown is kept.
function RedisplayedListState(current: ListState, body: CardListErrorBody): ListState {
    if (body.rows === undefined) {
        return current;
    }
    return {
        rows: body.rows,
        pageState: {
            page: body.page ?? current.pageState?.page,
            first_key: body.first_key,
            last_key: body.last_key,
        },
        selectionCodes: current.selectionCodes.slice(0, body.rows.length),
    };
}

function WithSelectionCode(list: ListState, rowKey: string, code: string): ListState {
    const rowIndex = list.rows.findIndex((row) => row.card_ref === rowKey);
    if (rowIndex < 0) {
        return list;
    }
    const selectionCodes = list.rows.map((_row, index) =>
        index === rowIndex ? code : (list.selectionCodes[index] ?? BLANK_CODE),
    );
    return { ...list, selectionCodes };
}

function HasSelection(list: ListState): boolean {
    return list.selectionCodes.some((code) => code !== BLANK_CODE);
}

// Every row shown is sent, a blank code included, so the server can refuse more than one
// selection or an invalid code.
function BuildSelectionRows(list: ListState): CardSelectionRow[] {
    return list.rows.map((row, index) => ({
        card_ref: row.card_ref,
        selection: list.selectionCodes[index] ?? BLANK_CODE,
    }));
}

// The server accepts only one selected row, so the row holding a code is the one it transfers.
function SelectedRow(list: ListState): CardListRow | undefined {
    const rowIndex = list.selectionCodes.findIndex((code) => code !== BLANK_CODE);
    return list.rows[rowIndex];
}

// The server's field_errors name for a row's CRDSELn code, e.g. rows[2].selection.
function SelectionFieldName(rowIndex: number): string {
    return `rows[${rowIndex}].selection`;
}

function SelectionRowsFor(list: ListState, fieldErrors: readonly string[]): SelectionRow[] {
    return list.rows.map((row, index) => ({
        rowKey: row.card_ref,
        cells: {
            acct_id: row.acct_id,
            card_num_masked: row.card_num_masked,
            active_status: row.active_status,
        },
        selectedCode: list.selectionCodes[index] ?? BLANK_CODE,
        isError: fieldErrors.includes(SelectionFieldName(index)),
    }));
}

// The CRDSELn code of every row shown, in row order, blank where none was keyed.
function RowSelectionCodes(list: ListState): string[] {
    return list.rows.map((_row, index) => list.selectionCodes[index] ?? BLANK_CODE);
}

// ENTER and F7 re-read from the first card shown, F8 from the last; each echoes the page number.
// The keys are opaque server tokens, absent before the first page arrives. Once any row holds a
// code, every row's code goes too, so the server refuses more than one selection or an invalid
// code before paging; all-blank codes are left out, as the server reads them as blank fields.
function ListQuery(context: ScreenContext, direction?: Direction): CardListQuery {
    const { list, filters } = context;
    const pageState = list.pageState;
    const startKey = direction === Direction.FORWARD ? pageState?.last_key : pageState?.first_key;
    return {
        acctId: filters.acct_id,
        cardNum: filters.card_num,
        startKey,
        direction,
        page: pageState?.page,
        selections: HasSelection(list) ? RowSelectionCodes(list) : undefined,
    };
}

// The card_ref token travels in the URL instead of the card number; URLSearchParams encodes the
// "=" padding of the token.
function CardRoute(action: CardSelectionAction, cardRef: string, acctId: string): string {
    const query = new URLSearchParams({ cardRef, acctId });
    return `${SELECTION_ROUTES[action]}?${query.toString()}`;
}

function ApplyListResponse(setters: ScreenSetters, response: CardListResponse): void {
    setters.setList((current) => ListStateFrom(response, current.selectionCodes));
    setters.setMessages(MessagesFrom(response));
}

function ListUpdate(setters: ScreenSetters, response: CardListResponse): ScreenUpdate {
    return () => ApplyListResponse(setters, response);
}

// A legacy outcome keeps the rows shown unless its body redisplays them; a 500, a network
// failure or a non-legacy body shows the abend text.
function ApplyCallFailure(setters: ScreenSetters, error: AxiosError): void {
    const body = LegacyErrorBody<CardListErrorBody>(error);
    if (body === null) {
        setters.setMessages(MessagesFrom(ToScreenFeedback(error)));
        return;
    }
    setters.setList((current) => RedisplayedListState(current, body));
    setters.setMessages(MessagesFrom(body));
}

// A read abandoned through its signal was superseded or its page has gone, so it settles with no
// update at all, not even the abend text. The 401 and 403 redirects belong to the API client
// interceptor. Anything other than an axios error is a defect and propagates.
async function SettleScreenRequest(
    setters: ScreenSetters,
    request: () => Promise<ScreenUpdate>,
): Promise<ScreenUpdate | null> {
    try {
        return await request();
    } catch (error) {
        if (IsCancelledRequest(error)) {
            return null;
        }
        if (axios.isAxiosError(error)) {
            return () => ApplyCallFailure(setters, error);
        }
        throw error;
    }
}

// The keys stay disabled until the latest call settles; a superseded or unmounted call's
// outcome is dropped. An aborted read is never the latest call of a mounted page: it is aborted by
// a newer call, which takes the next ticket, or by the page leaving, which clears the mounted
// mark. So it leaves the busy state to the call in flight, or to no one once the page has gone.
async function RunScreenCall(
    calls: ScreenCalls,
    request: () => Promise<ScreenUpdate>,
): Promise<void> {
    const ticket = calls.gate.Begin();
    calls.setters.setIsBusy(true);
    try {
        const update = await SettleScreenRequest(calls.setters, request);
        if (update !== null && calls.gate.IsCurrent(ticket)) {
            update();
        }
    } finally {
        if (calls.gate.IsCurrent(ticket)) {
            calls.setters.setIsBusy(false);
        }
    }
}

// Aborts whichever list read is in flight at the moment of the call.
function AbortListRead(listReadRef: ListReadRef): void {
    listReadRef.current?.abort();
}

// The new read supersedes the one still in flight, which is aborted.
function BeginListRead(listReadRef: ListReadRef): AbortSignal {
    AbortListRead(listReadRef);
    const controller = new AbortController();
    listReadRef.current = controller;
    return controller.signal;
}

function ReadPage(context: ScreenContext, query: CardListQuery): Promise<void> {
    const signal = BeginListRead(context.listReadRef);
    return RunScreenCall(context, async () =>
        ListUpdate(context.setters, await ListCards(query, { signal })),
    );
}

// A pending message is shown at once and kept when the first page brings no message of its own.
async function LoadFirstPage(
    calls: ScreenCalls,
    pendingMessage: string,
    signal: AbortSignal,
): Promise<void> {
    if (pendingMessage !== "") {
        calls.setters.setMessages(MessagesFrom({ message: pendingMessage }));
    }
    await RunScreenCall(calls, async () => {
        const response = await ListCards(FIRST_ENTRY_QUERY, { signal });
        const message = response.message || pendingMessage;
        return ListUpdate(calls.setters, { ...response, message });
    });
}

// S opens the card view (COCRDSLC) and U the card update (COCRDUPC). No action is the WHEN OTHER
// path, which re-reads the page shown under the signal its selection call was given.
async function FollowSelection(
    context: ScreenContext,
    response: CardSelectionResponse,
    signal: AbortSignal,
): Promise<ScreenUpdate> {
    if (response.action === null) {
        return ListUpdate(context.setters, await ListCards(ListQuery(context), { signal }));
    }
    const acctId = SelectedRow(context.list)?.acct_id ?? "";
    const route = CardRoute(response.action, response.card_ref, acctId);
    return () => context.router.push(route);
}

// The selection is a write and takes no signal. Its WHEN OTHER re-read is owned from the start, so
// leaving the page while the selection runs aborts that read before it is sent.
function SubmitSelection(context: ScreenContext): Promise<void> {
    const rows = BuildSelectionRows(context.list);
    const { acct_id, card_num } = context.filters;
    const rereadSignal = BeginListRead(context.listReadRef);
    return RunScreenCall(context, async () =>
        FollowSelection(context, await SelectCards({ acct_id, card_num, rows }), rereadSignal),
    );
}

// The filters travel as keyed: the server runs every filter edit and returns its literal.
function HandleEnter(context: ScreenContext): Promise<void> {
    if (HasSelection(context.list)) {
        return SubmitSelection(context);
    }
    return ReadPage(context, ListQuery(context));
}

function HandlePageBackward(context: ScreenContext): Promise<void> {
    return ReadPage(context, ListQuery(context, Direction.BACKWARD));
}

function HandlePageForward(context: ScreenContext): Promise<void> {
    return ReadPage(context, ListQuery(context, Direction.FORWARD));
}

// PF3 transfers to the main menu (COMEN01C).
function HandleExit(context: ScreenContext): void {
    context.router.push(MENU_ROUTE);
}

interface ScreenKeyAction {
    key: PfKey;
    label: string;
    Run: (context: ScreenContext) => Promise<void> | void;
}

// The PF-key legend "F3=Exit F7=Backward  F8=Forward", with ENTER implicit on the map.
const SCREEN_KEY_ACTIONS: readonly ScreenKeyAction[] = [
    { key: "ENTER", label: ENTER_LABEL, Run: HandleEnter },
    { key: "F3", label: EXIT_LABEL, Run: HandleExit },
    { key: "F7", label: BACKWARD_LABEL, Run: HandlePageBackward },
    { key: "F8", label: FORWARD_LABEL, Run: HandlePageForward },
];

// The keys are inactive while a call runs, as the 3270 keyboard locks until the screen returns.
function BuildFunctionKeys(context: ScreenContext, isBusy: boolean): FunctionKeyDefinition[] {
    return SCREEN_KEY_ACTIONS.map(({ key, label, Run }) => ({
        key,
        label,
        onPress: () => void Run(context),
        disabled: isBusy,
    }));
}

function SelectionElementFor(cursorField: string, tableElement: HTMLElement | null) {
    const match = SELECTION_CURSOR_PATTERN.exec(cursorField);
    if (match === null || tableElement === null) {
        return null;
    }
    const label = SelectionLabelFor(Number(match[1]) + 1);
    return tableElement.querySelector<HTMLElement>(`[role="combobox"][aria-label="${label}"]`);
}

// Replaces MOVE -1 TO <field>L: the cursor goes to the filter or the row selection the server
// names.
function FocusCursorField(cursorField: string, targets: CursorTargets): void {
    const filterInput = targets.fieldInputs[cursorField];
    const element = filterInput ?? SelectionElementFor(cursorField, targets.tableElement);
    element?.focus();
}

interface ScreenState {
    filters: CardFilterTexts;
    setFilters: Dispatch<SetStateAction<CardFilterTexts>>;
    list: ListState;
    messages: ScreenMessages;
    isBusy: boolean;
    setters: ScreenSetters;
    gate: RequestGate;
}

interface CursorRefs {
    fieldInputsRef: RefObject<FilterInputs>;
    tableRef: RefObject<HTMLDivElement | null>;
}

interface FilterHandlers {
    onFilterChange: (name: FilterFieldName, value: string) => void;
    onInputRef: (name: FilterFieldName, element: HTMLInputElement | null) => void;
}

interface CardListScreenModel {
    filters: CardFilterTexts;
    list: ListState;
    messages: ScreenMessages;
    isBusy: boolean;
    keys: readonly FunctionKeyDefinition[];
    filterHandlers: FilterHandlers;
    tableRef: RefObject<HTMLDivElement | null>;
    onSelectionChange: (rowKey: string, code: string) => void;
}

// React recognises hooks only by the camelCase "use" prefix, so the hook names below are a
// framework-mandated exception to the MK_Modernization Rule's PascalCase function naming.

// Both values are refs, which survive React Strict Mode's simulated unmount and remount; its
// cleanup and re-run leave the page marked mounted, and a real unmount clears the mark.
function useRequestGate(): RequestGate {
    const latestTicketRef = useRef(0);
    const isMountedRef = useRef(false);
    useEffect(() => {
        isMountedRef.current = true;
        return () => {
            isMountedRef.current = false;
        };
    }, []);
    return useMemo<RequestGate>(
        () => ({
            Begin: () => {
                latestTicketRef.current += 1;
                return latestTicketRef.current;
            },
            IsCurrent: (ticket) => isMountedRef.current && ticket === latestTicketRef.current,
        }),
        [],
    );
}

function useScreenState(): ScreenState {
    const [filters, setFilters] = useState<CardFilterTexts>(EMPTY_FILTERS);
    const [list, setList] = useState<ListState>(EMPTY_LIST);
    const [messages, setMessages] = useState<ScreenMessages>(NO_MESSAGES);
    const [isBusy, setIsBusy] = useState(false);
    const setters = useMemo<ScreenSetters>(() => ({ setList, setMessages, setIsBusy }), []);
    const gate = useRequestGate();
    return { filters, setFilters, list, messages, isBusy, setters, gate };
}

// Every response moves the cursor, even to the field it already names, so the effect follows
// the messages object rather than the field name.
function useCursorFocus(messages: ScreenMessages): CursorRefs {
    const fieldInputsRef = useRef<FilterInputs>({});
    const tableRef = useRef<HTMLDivElement>(null);
    useEffect(() => {
        const targets = { fieldInputs: fieldInputsRef.current, tableElement: tableRef.current };
        FocusCursorField(messages.cursorField, targets);
    }, [messages]);
    return { fieldInputsRef, tableRef };
}

function useFilterHandlers(
    setFilters: Dispatch<SetStateAction<CardFilterTexts>>,
    fieldInputsRef: RefObject<FilterInputs>,
): FilterHandlers {
    return useMemo(
        () => ({
            onFilterChange: (name, value) =>
                setFilters((current) => ({ ...current, [name]: value })),
            onInputRef: (name, element) => {
                fieldInputsRef.current[name] = element;
            },
        }),
        [setFilters, fieldInputsRef],
    );
}

// The pending message is taken once per page instance, so React's development remount of the
// effect still shows it. Leaving the page aborts the list read in flight, the first page or a
// later one. The development remount aborts the first read and reads the first page again; the
// request gate drops the aborted read, so only the second one changes the screen or ends the busy
// state, and a production mount reads the first page once.
function useFirstPageLoad(
    setters: ScreenSetters,
    gate: RequestGate,
    listReadRef: ListReadRef,
): void {
    const pendingMessageRef = useRef<string | null>(null);
    useEffect(() => {
        if (pendingMessageRef.current === null) {
            pendingMessageRef.current = TakePendingMessage();
        }
        const signal = BeginListRead(listReadRef);
        void LoadFirstPage({ setters, gate }, pendingMessageRef.current, signal);
        return () => AbortListRead(listReadRef);
    }, [setters, gate, listReadRef]);
}

function useScreenContext(state: ScreenState): ScreenContext {
    const router = useRouter();
    const listReadRef = useRef<AbortController | null>(null);
    const { filters, list, setters, gate } = state;
    return useMemo(
        () => ({ filters, list, setters, gate, router, listReadRef }),
        [filters, list, setters, gate, router],
    );
}

function useCardListScreen(): CardListScreenModel {
    const state = useScreenState();
    const { setters, gate, isBusy } = state;
    const { fieldInputsRef, tableRef } = useCursorFocus(state.messages);
    const filterHandlers = useFilterHandlers(state.setFilters, fieldInputsRef);
    const context = useScreenContext(state);
    const keys = useMemo(() => BuildFunctionKeys(context, isBusy), [context, isBusy]);
    const onSelectionChange = useCallback(
        (rowKey: string, code: string) =>
            setters.setList((current) => WithSelectionCode(current, rowKey, code)),
        [setters],
    );
    useFunctionKeys(keys);
    useFirstPageLoad(setters, gate, context.listReadRef);
    const { filters, list, messages } = state;
    return { filters, list, messages, isBusy, keys, filterHandlers, tableRef, onSelectionChange };
}

interface PageIndicatorProps {
    pageState: PageState | null;
}

interface CardFilterFieldProps {
    definition: FilterFieldDefinition;
    value: string;
    isError: boolean;
    handlers: FilterHandlers;
}

interface CardFilterFieldsProps {
    filters: CardFilterTexts;
    fieldErrors: readonly string[];
    handlers: FilterHandlers;
}

interface CardListTableProps {
    list: ListState;
    fieldErrors: readonly string[];
    tableRef: RefObject<HTMLDivElement | null>;
    onSelectionChange: (rowKey: string, code: string) => void;
}

// PAGENO, right of the title as on the map; blank until the first page arrives.
function PageIndicator({ pageState }: PageIndicatorProps) {
    const page = pageState?.page;
    const pageText = page === undefined ? PAGE_LABEL : `${PAGE_LABEL} ${page}`;
    return (
        <Typography variant="labelLarge" component="p" align="right">
            {pageText}
        </Typography>
    );
}

// The BMS LENGTH and, while the server names the filter in field_errors, its tie to the ERRMSG
// line, so assistive technology reads the literal when the cursor lands on the field.
function FilterHtmlInputProps(definition: FilterFieldDefinition, isError: boolean) {
    return { maxLength: definition.maxLength, ...ErrorMessageReference(isError) };
}

// The value is sent as typed: no trim, case change or digit filter, so every server literal
// stays reachable. Only the BMS LENGTH is enforced.
function CardFilterField({ definition, value, isError, handlers }: CardFilterFieldProps) {
    const { name } = definition;
    return (
        <Grid size={{ xs: 12, md: 6 }}>
            <TextField
                variant="outlined"
                fullWidth
                label={definition.label}
                value={value}
                error={isError}
                autoFocus={definition.autoFocus}
                onChange={(event) => handlers.onFilterChange(name, event.target.value)}
                inputRef={(element: HTMLInputElement | null) => handlers.onInputRef(name, element)}
                slotProps={{ htmlInput: FilterHtmlInputProps(definition, isError) }}
            />
        </Grid>
    );
}

function CardFilterFields({ filters, fieldErrors, handlers }: CardFilterFieldsProps) {
    return (
        <Grid container spacing={4}>
            {FILTER_FIELDS.map((definition) => (
                <CardFilterField
                    key={definition.name}
                    definition={definition}
                    value={filters[definition.name]}
                    isError={fieldErrors.includes(definition.name)}
                    handlers={handlers}
                />
            ))}
        </Grid>
    );
}

// List rows carry only the masked card number and an opaque card_ref token; the full number on
// this page is the card filter as the user keyed it.
function CardListTable({ list, fieldErrors, tableRef, onSelectionChange }: CardListTableProps) {
    return (
        <Box ref={tableRef}>
            <SelectionTable
                columns={CARD_LIST_COLUMNS}
                rows={SelectionRowsFor(list, fieldErrors)}
                allowedCodes={SELECTION_CODES}
                onSelectionChange={onSelectionChange}
                selectionHeader={SELECTION_HEADER}
            />
        </Box>
    );
}

interface CardListContentProps {
    model: CardListScreenModel;
}

function CardListContent({ model }: CardListContentProps) {
    return (
        <>
            <PageIndicator pageState={model.list.pageState} />
            <CardFilterFields
                filters={model.filters}
                fieldErrors={model.messages.fieldErrors}
                handlers={model.filterHandlers}
            />
            <CardListTable
                list={model.list}
                fieldErrors={model.messages.fieldErrors}
                tableRef={model.tableRef}
                onSelectionChange={model.onSelectionChange}
            />
        </>
    );
}

function CardListScreen() {
    const model = useCardListScreen();
    const { message, severity, infoMessage } = model.messages;
    return (
        <ScreenShell
            transactionId={TRANSACTION_ID}
            programName={PROGRAM_NAME}
            screenTitle={SCREEN_TITLE}
            isBusy={model.isBusy}
        >
            <CardListContent model={model} />
            <MessageLine message={message} severity={severity} infoMessage={infoMessage} />
            <FunctionKeyBar keys={model.keys} />
        </ScreenShell>
    );
}

// CCLI: the card list, reached from main-menu option 3 and returning to it on F3.
export default function CardListPage() {
    return (
        <Suspense fallback={null}>
            <CardListScreen />
        </Suspense>
    );
}
