// Replaces: BMS map COUSR00, program COUSR00C (CU00); copybooks COUSR00 (cpy-bms), CSUSR01Y,
// COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
"use client";

import { Box, Grid, TextField, Typography } from "@mui/material";
import { isAxiosError } from "axios";
import { useRouter } from "next/navigation";
import {
    Suspense,
    useEffect,
    useMemo,
    useRef,
    useState,
    useTransition,
    type Dispatch,
    type Ref,
    type RefObject,
    type SetStateAction,
    type TransitionStartFunction,
} from "react";

import { CENTRE_ALIGNED_ROW_STYLE, VISUALLY_HIDDEN_STYLE } from "@/app/theme";
import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import { ErrorMessageReference, MessageLine } from "@/components/MessageLine";
import { ScreenShell, useScreenBusy } from "@/components/ScreenShell";
import {
    SelectionTable,
    type SelectionColumn,
    type SelectionRow,
} from "@/components/SelectionTable";
import {
    ABEND_MESSAGE,
    Direction,
    IsCancelledRequest,
    LegacyErrorBody,
    SeverityForOutcome,
    TakePendingMessage,
    ToAllowedRoute,
    ToScreenFeedback,
    type CompleteScreenFeedback,
    type MessageSeverity,
    type PageState,
    type RouteAllowance,
} from "@/lib/apiClient";
import {
    ListUsers,
    SelectUsers,
    type UserListQuery,
    type UserListResponse,
    type UserListRow,
    type UserSelection,
    type UserSelectionErrorResponse,
} from "@/lib/api/users";
import {
    INVALID_KEY_MESSAGE,
    useFunctionKeys,
    type FunctionKeyDefinition,
    type InvalidKeyAction,
} from "@/lib/useFunctionKeys";

const TRANSACTION_ID = "CU00";
const PROGRAM_NAME = "COUSR00C";
const SCREEN_TITLE = "List Users";
const SCREEN_IDENTITY = {
    transactionId: TRANSACTION_ID,
    programName: PROGRAM_NAME,
    screenTitle: SCREEN_TITLE,
} as const;
// USRIDIN LENGTH=8 (USRIDINI X(8)).
const SEARCH_MAX_LENGTH = 8;
const SEARCH_LABEL = "Search User ID:";
const PAGE_LABEL = "Page:";
// The polite page status, built from the screen's own Page:, row and user ID fields so that no
// ERRMSG literal is invented or changed: "Page: 2, 10 users shown, QAUSR006 to QAUSR015", and
// for a single row "Page: 4, 1 user shown, 1A to 1A".
const SHOWN_COUNT_SUFFIX = "users shown";
const SINGLE_SHOWN_COUNT_SUFFIX = "user shown";
const STATUS_PART_SEPARATOR = ", ";
const SHOWN_RANGE_SEPARATOR = " to ";
// The JSON name the server gives USRIDIN in cursor_field and field_errors.
const SEARCH_FIELD = "search_user_id";
const ALLOWED_SELECTION_CODES = ["U", "D"] as const;
// The row-8 column headings, their centring blanks trimmed, bound to the UserListRow keys. Each
// width, in theme spacing units, holds the wider of the heading and the BMS LENGTH (USRIDnn 8,
// FNAMEnn and LNAMEnn 20, UTYPEnn 1), and together they fit the 768 px screen without scrolling.
const LIST_COLUMNS: SelectionColumn[] = [
    { field: "user_id", header: "User ID", widthUnits: 28 },
    { field: "first_name", header: "First Name", widthUnits: 52 },
    { field: "last_name", header: "Last Name", widthUnits: 52 },
    { field: "user_type", header: "Type", widthUnits: 16 },
];
const SELECTION_INSTRUCTION = "Type 'U' to Update or 'D' to Delete a User from the list";
// SEL0001-SEL0010: one selection code field per screen row.
const SELECTION_FIELD_COUNT = 10;
// PF3 moves COADM01C to CDEMO-TO-PROGRAM.
const ADMIN_MENU_ROUTE = "/admin";
// The only pages a U or D selection may open, with the selected user's userId: UPDATE_ROUTE,
// DELETE_ROUTE and ROUTE_USER_ID_PARAMETER in backend/app/services/users/cousr00c.py (XCTL
// COUSR02C and COUSR03C).
const SELECTION_ROUTE_ALLOWANCE: RouteAllowance = {
    pathnames: ["/admin/users/update", "/admin/users/delete"],
    queryKey: "userId",
};
const BLANK_TEXT = "";
// COUSR00C never changes ERRMSGC, so every message keeps the map's COLOR=RED.
const LIST_MESSAGE_SEVERITY: MessageSeverity = SeverityForOutcome({
    isError: true,
    isCompletedWrite: false,
});

type FieldRefs = Partial<Record<string, HTMLInputElement | null>>;
type PageRouter = ReturnType<typeof useRouter>;

// The COUSR00 screen the page holds. selectionCodes holds SEL0001-SEL0010 by screen position,
// index 0 being row 1, whichever user each row shows. isAwaitingFirstPage holds the map back
// until the first entry's browse settles, so the whole map paints at once.
interface ListScreenState {
    rows: readonly UserListRow[];
    pageState: PageState;
    selectionCodes: readonly string[];
    searchText: string;
    feedback: CompleteScreenFeedback;
    isBusy: boolean;
    isAwaitingFirstPage: boolean;
}

type ScreenUpdate = Dispatch<SetStateAction<ListScreenState>>;
// Starts an F7 or F8 browse: aborts any earlier one left in flight and returns the new one's
// signal, which leaving the screen aborts.
type PageReadStarter = () => AbortSignal;
type ReadControllerRef = RefObject<AbortController | null>;

interface ScreenActions {
    setScreen: ScreenUpdate;
    router: PageRouter;
    startPageRead: PageReadStarter;
    // Runs a selection's route change as a transition, which keeps the screen busy until the
    // routed screen has rendered.
    startNavigation: TransitionStartFunction;
    // The synchronous call lock: true from the ENTER, F7 or F8 that sends a call until that call
    // settles, so a key pressed before the busy render reaches the keys is refused.
    isInFlightRef: RefObject<boolean>;
}

const EMPTY_FEEDBACK: CompleteScreenFeedback = {
    message: BLANK_TEXT,
    info_message: BLANK_TEXT,
    field_errors: [],
    cursor_field: BLANK_TEXT,
};

// Every SELnnnn field blank, as the first entry's MOVE LOW-VALUES TO COUSR0AO leaves them.
const BLANK_SELECTION_CODES: readonly string[] = Array.from(
    { length: SELECTION_FIELD_COUNT },
    () => BLANK_TEXT,
);

// The keys stay locked until the first entry's browse answers, as the 3270 keyboard stays
// locked until the first SEND MAP arrives.
const INITIAL_SCREEN: ListScreenState = {
    rows: [],
    pageState: {},
    selectionCodes: BLANK_SELECTION_CODES,
    searchText: BLANK_TEXT,
    feedback: EMPTY_FEEDBACK,
    isBusy: true,
    isAwaitingFirstPage: true,
};

// What a settled browse changes: a key press unlocks the keys; the first entry also paints the map.
const CALL_SETTLED: Partial<ListScreenState> = { isBusy: false };
const FIRST_ENTRY_SETTLED: Partial<ListScreenState> = { isBusy: false, isAwaitingFirstPage: false };

function PatchScreen(setScreen: ScreenUpdate, patch: Partial<ListScreenState>): void {
    setScreen((current) => ({ ...current, ...patch }));
}

// The trim only decides whether a message is shown; shown text is never altered.
function IsBlank(text: string): boolean {
    return text.trim() === BLANK_TEXT;
}

// The row count with its noun: singular for one row, plural for none or several.
function ShownCountText(rowCount: number): string {
    const countSuffix = rowCount === 1 ? SINGLE_SHOWN_COUNT_SUFFIX : SHOWN_COUNT_SUFFIX;
    return `${rowCount} ${countSuffix}`;
}

// The shown page's number, row count and first and last user IDs; blank before any page shows.
function PageStatusText(rows: readonly UserListRow[], page?: number): string {
    if (page === undefined) {
        return BLANK_TEXT;
    }
    const statusParts = [`${PAGE_LABEL} ${page}`, ShownCountText(rows.length)];
    const firstRow = rows[0];
    const lastRow = rows[rows.length - 1];
    if (firstRow !== undefined && lastRow !== undefined) {
        statusParts.push(`${firstRow.user_id}${SHOWN_RANGE_SEPARATOR}${lastRow.user_id}`);
    }
    return statusParts.join(STATUS_PART_SEPARATOR);
}

function FeedbackOf(response: UserListResponse): CompleteScreenFeedback {
    return {
        ...EMPTY_FEEDBACK,
        message: response.message ?? BLANK_TEXT,
        cursor_field: response.cursor_field === undefined ? BLANK_TEXT : response.cursor_field,
    };
}

// A shown page replaces the rows, keys and message and leaves every selection code at its
// screen position: SEL0001-SEL0010 are FSET UNPROT fields that INITIALIZE-USER-DATA and
// POPULATE-USER-DATA never touch (cbl/COUSR00C.cbl lines 384-500), so a typed code is sent back
// beside whichever user the new page puts on its row. A browse that PROCESS-PAGE-FORWARD runs to
// its end also blanks USRIDIN (MOVE SPACE TO USRIDINO): the first entry, ENTER and F8. F7's
// PROCESS-PAGE-BACKWARD leaves the typed search text.
function ShownPageUpdate(
    response: UserListResponse,
    blanksSearch: boolean,
): Partial<ListScreenState> {
    const pageState: PageState = {
        page: response.page,
        first_key: response.first_key,
        last_key: response.last_key,
    };
    const rows = response.rows ?? [];
    const shownPage = { rows, pageState, feedback: FeedbackOf(response) };
    return blanksSearch ? { ...shownPage, searchText: BLANK_TEXT } : shownPage;
}

// Only an axios error is a legacy outcome; anything else is a defect and propagates.
function FailureFeedback(error: unknown): CompleteScreenFeedback {
    if (!isAxiosError(error)) {
        throw error;
    }
    return ToScreenFeedback(error);
}

// A failed ENTER leaves every selection code where it was typed. The invalid-selection 400
// carries the re-browsed page: that literal sets no ERR-FLG, so PROCESS-PAGE-FORWARD shows the
// page and PROCESS-ENTER-KEY blanks USRIDIN (cbl/COUSR00C.cbl lines 210-232). Any other failure,
// such as a file error that sets ERR-FLG, keeps the rows, keys and typed search text.
function EnterFailureUpdate(error: unknown): SetStateAction<ListScreenState> {
    const feedback = FailureFeedback(error);
    const refusalBody = LegacyErrorBody<UserSelectionErrorResponse>(error);
    if (refusalBody?.rows === undefined) {
        return (current) => ({ ...current, feedback });
    }
    const browsedPage = ShownPageUpdate(refusalBody, true);
    return (current) => ({ ...current, ...browsedPage, feedback });
}

// Every shown row in display order beside the code at its screen position, blank included, as
// PROCESS-ENTER-KEY reads SELnnnnI beside USRIDnnI and its EVALUATE TRUE takes the first
// non-blank code (cbl/COUSR00C.cbl lines 152-180).
function ToSelections(screen: ListScreenState): UserSelection[] {
    return screen.rows.map((row, position) => ({
        user_id: row.user_id,
        selection: screen.selectionCodes[position] ?? BLANK_TEXT,
    }));
}

// The XCTL to COUSR02C or COUSR03C opens the route rebuilt from its checked pathname and userId,
// never the server's own text. Any other route is an unexpected answer: the abend text, with the
// cursor on USRIDIN as every COUSR00C send puts it, and the rows and codes kept.
function FollowSelectionRoute(actions: ScreenActions, route: unknown): void {
    const allowedRoute = ToAllowedRoute(route, SELECTION_ROUTE_ALLOWANCE);
    if (allowedRoute === null) {
        const feedback = { ...EMPTY_FEEDBACK, message: ABEND_MESSAGE, cursor_field: SEARCH_FIELD };
        PatchScreen(actions.setScreen, { feedback });
        return;
    }
    actions.startNavigation(() => actions.router.push(allowedRoute));
}

// ENTER sends the search key exactly as typed. A U or D selection answers with the route of
// the update or delete screen, replacing XCTL COUSR02C or COUSR03C.
async function HandleEnter(actions: ScreenActions, screen: ListScreenState): Promise<void> {
    const request = { selections: ToSelections(screen), search_user_id: screen.searchText };
    try {
        const response = await SelectUsers(request);
        if (response.route) {
            FollowSelectionRoute(actions, response.route);
            return;
        }
        PatchScreen(actions.setScreen, ShownPageUpdate(response, true));
    } catch (error) {
        actions.setScreen(EnterFailureUpdate(error));
    }
}

// F7 browses back from the first key shown, F8 on from the last, both from the page shown.
function PageQuery(pageState: PageState, direction: Direction): UserListQuery {
    const isBackward = direction === Direction.BACKWARD;
    const startKey = isBackward ? pageState.first_key : pageState.last_key;
    return { startKey, direction, page: pageState.page };
}

// A refused page keeps the rows, keys and selection codes on the screen, as
// SEND-USRLST-SCREEN's SEND without ERASE did (cbl/COUSR00C.cbl lines 522-543). An abandoned
// browse changes nothing.
async function HandlePage(
    actions: ScreenActions,
    screen: ListScreenState,
    direction: Direction,
    signal: AbortSignal,
): Promise<void> {
    try {
        const response = await ListUsers(PageQuery(screen.pageState, direction), { signal });
        const blanksSearch = direction === Direction.FORWARD;
        PatchScreen(actions.setScreen, ShownPageUpdate(response, blanksSearch));
    } catch (error) {
        if (IsCancelledRequest(error)) {
            return;
        }
        PatchScreen(actions.setScreen, { feedback: FailureFeedback(error) });
    }
}

// The first entry runs PROCESS-ENTER-KEY with a blank search key. A message carried here from
// another screen shows unless the browse sets one of its own. An abandoned browse changes nothing.
async function LoadFirstPage(
    setScreen: ScreenUpdate,
    pendingMessage: string,
    signal: AbortSignal,
): Promise<void> {
    try {
        const response = await ListUsers({}, { signal });
        const shownPage = ShownPageUpdate(response, true);
        const responseMessage = response.message ?? BLANK_TEXT;
        const message = IsBlank(responseMessage) ? pendingMessage : responseMessage;
        PatchScreen(setScreen, { ...shownPage, feedback: { ...FeedbackOf(response), message } });
    } catch (error) {
        if (IsCancelledRequest(error)) {
            return;
        }
        setScreen(EnterFailureUpdate(error));
    }
}

// A browse abandoned through its signal leaves the keys locked, and a first entry's map unpainted:
// the screen has gone, or the browse that replaced it settles them when it answers.
async function SettleBusy(
    setScreen: ScreenUpdate,
    task: () => Promise<void>,
    signal?: AbortSignal,
    settled: Partial<ListScreenState> = CALL_SETTLED,
): Promise<void> {
    try {
        await task();
    } finally {
        if (signal?.aborted !== true) {
            PatchScreen(setScreen, settled);
        }
    }
}

async function RunBusy(
    setScreen: ScreenUpdate,
    task: () => Promise<void>,
    signal?: AbortSignal,
): Promise<void> {
    PatchScreen(setScreen, { isBusy: true });
    await SettleBusy(setScreen, task, signal);
}

// One ENTER, F7 or F8 call at a time. The disabled keys take effect only a render after the
// press, so the lock, taken before the call changes anything, refuses a second press in that gap.
// It clears when the call settles, a rethrown defect included.
async function RunExclusive(
    isInFlightRef: RefObject<boolean>,
    call: () => Promise<void>,
): Promise<void> {
    if (isInFlightRef.current) {
        return;
    }
    isInFlightRef.current = true;
    try {
        await call();
    } finally {
        isInFlightRef.current = false;
    }
}

// One server round trip per key press, with every key locked until it answers.
function BusyKeyPress(actions: ScreenActions, task: () => Promise<void>): () => void {
    return () => {
        void RunExclusive(actions.isInFlightRef, () => RunBusy(actions.setScreen, task));
    };
}

// F7 or F8: a busy key press whose browse starts only once the lock is taken, so a second press
// before it answers is refused rather than superseding it.
function PageKeyPress(
    actions: ScreenActions,
    screen: ListScreenState,
    direction: Direction,
): () => void {
    const browse = (): Promise<void> => {
        const signal = actions.startPageRead();
        const task = (): Promise<void> => HandlePage(actions, screen, direction, signal);
        return RunBusy(actions.setScreen, task, signal);
    };
    return () => {
        void RunExclusive(actions.isInFlightRef, browse);
    };
}

// PF3 is XCTL COADM01C. It is refused while a call is in flight, so the call's answer is shown.
function HandleBack(actions: ScreenActions): void {
    if (actions.isInFlightRef.current) {
        return;
    }
    actions.router.push(ADMIN_MENU_ROUTE);
}

// The legend ENTER=Continue  F3=Back  F7=Backward  F8=Forward, in its order.
function BuildKeys(
    actions: ScreenActions,
    screen: ListScreenState,
    isBusy: boolean,
): FunctionKeyDefinition[] {
    const disabled = isBusy;
    const enter = BusyKeyPress(actions, () => HandleEnter(actions, screen));
    const backward = PageKeyPress(actions, screen, Direction.BACKWARD);
    const forward = PageKeyPress(actions, screen, Direction.FORWARD);
    const back = (): void => HandleBack(actions);
    return [
        { key: "ENTER", label: "Continue", onPress: enter, disabled },
        { key: "F3", label: "Back", onPress: back, disabled },
        { key: "F7", label: "Backward", onPress: backward, disabled },
        { key: "F8", label: "Forward", onPress: forward, disabled },
    ];
}

// COUSR00C WHEN OTHER (lines 132-135): MOVE 'Y' TO WS-ERR-FLG, MOVE -1 TO USRIDINL and
// CCDA-MSG-INVALID-KEY. No file is browsed, so the rows, the selection codes, the search text and
// the page stay as they stood; a new feedback object puts the cursor back on USRIDIN. It is
// refused while a call is in flight, so the call's answer is the message shown.
function ShowInvalidKey(setScreen: ScreenUpdate, isInFlightRef: RefObject<boolean>): void {
    if (isInFlightRef.current) {
        return;
    }
    const feedback: CompleteScreenFeedback = {
        ...EMPTY_FEEDBACK,
        message: INVALID_KEY_MESSAGE,
        cursor_field: SEARCH_FIELD,
    };
    PatchScreen(setScreen, { feedback });
}

// Each row is keyed by its screen position, the SELnnnn field it shows, never by its user.
function ToSelectionRows(screen: ListScreenState): SelectionRow[] {
    return screen.rows.map((row, position) => ({
        rowKey: String(position),
        cells: {
            user_id: row.user_id,
            first_name: row.first_name,
            last_name: row.last_name,
            user_type: row.user_type,
        },
        selectedCode: screen.selectionCodes[position] ?? BLANK_TEXT,
    }));
}

// Only the user changes a selection code, and only at the position it is typed.
function WithSelectionCode(position: number, code: string): SetStateAction<ListScreenState> {
    return (current) => ({
        ...current,
        selectionCodes: current.selectionCodes.map((shownCode, index) =>
            index === position ? code : shownCode,
        ),
    });
}

function StartPageRead(controllerRef: ReadControllerRef): AbortSignal {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    return controller.signal;
}

function AbortPageRead(controllerRef: ReadControllerRef): void {
    controllerRef.current?.abort();
}

// React recognises hooks only by the camelCase "use" prefix, so useFirstEntry, usePageRead,
// useCursorFocus, useInvalidKeyAction, useScreenActions and useUserList are a framework-mandated
// exception to the MK_Modernization Rule's PascalCase function naming.

// The pending message is taken once, so an effect React runs twice still shows it. Leaving the
// screen abandons the first browse; StrictMode's second run then browses again.
function useFirstEntry(setScreen: ScreenUpdate): void {
    const pendingMessageRef = useRef<string | null>(null);
    useEffect(() => {
        pendingMessageRef.current ??= TakePendingMessage();
        const pendingMessage = pendingMessageRef.current;
        const controller = new AbortController();
        const { signal } = controller;
        const firstBrowse = (): Promise<void> => LoadFirstPage(setScreen, pendingMessage, signal);
        void SettleBusy(setScreen, firstBrowse, signal, FIRST_ENTRY_SETTLED);
        return () => controller.abort();
    }, [setScreen]);
}

// Holds the F7 or F8 browse in flight, which leaving the screen abandons.
function usePageRead(): PageReadStarter {
    const controllerRef = useRef<AbortController | null>(null);
    useEffect(() => {
        return () => AbortPageRead(controllerRef);
    }, []);
    return () => StartPageRead(controllerRef);
}

// Replaces MOVE -1 TO USRIDINL: every answer puts the cursor on the field the server names.
function useCursorFocus(feedback: CompleteScreenFeedback, fieldRefs: RefObject<FieldRefs>): void {
    useEffect(() => {
        const cursorField = feedback.cursor_field;
        if (cursorField !== null && cursorField !== BLANK_TEXT) {
            fieldRefs.current[cursorField]?.focus();
        }
    }, [feedback, fieldRefs]);
}

// A PF key the legend lacks answers with the WHEN OTHER branch, and like every key it waits while a
// call runs, so the call's answer is the message shown.
function useInvalidKeyAction(
    setScreen: ScreenUpdate,
    isInFlightRef: RefObject<boolean>,
    isBusy: boolean,
): InvalidKeyAction {
    return useMemo(
        () => ({ onPress: () => ShowInvalidKey(setScreen, isInFlightRef), disabled: isBusy }),
        [setScreen, isInFlightRef, isBusy],
    );
}

interface UserListModel {
    screen: ListScreenState;
    setScreen: ScreenUpdate;
    keys: FunctionKeyDefinition[];
    invalidKeyAction: InvalidKeyAction;
    searchInputRef: Ref<HTMLInputElement>;
    isBusy: boolean;
}

// What the key handlers act through. The call lock is one ref for the screen's life, so the keys
// rebuilt on every render share it.
function useScreenActions(
    setScreen: ScreenUpdate,
    startNavigation: TransitionStartFunction,
): ScreenActions {
    const router = useRouter();
    const startPageRead = usePageRead();
    const isInFlightRef = useRef(false);
    return { setScreen, router, startPageRead, startNavigation, isInFlightRef };
}

// The screen is busy while a call runs and, after a selection, until the routed screen renders.
function useUserList(): UserListModel {
    const [screen, setScreen] = useState<ListScreenState>(INITIAL_SCREEN);
    const [isNavigating, startNavigation] = useTransition();
    const fieldRefs = useRef<FieldRefs>({});
    useFirstEntry(setScreen);
    const actions = useScreenActions(setScreen, startNavigation);
    useCursorFocus(screen.feedback, fieldRefs);
    const searchInputRef = (element: HTMLInputElement | null): void => {
        fieldRefs.current[SEARCH_FIELD] = element;
    };
    const isBusy = screen.isBusy || isNavigating;
    const keys = BuildKeys(actions, screen, isBusy);
    const invalidKeyAction = useInvalidKeyAction(setScreen, actions.isInFlightRef, isBusy);
    return { screen, setScreen, keys, invalidKeyAction, searchInputRef, isBusy };
}

interface UserSearchBarProps {
    searchText: string;
    page?: number;
    pageStatus: string;
    hasError: boolean;
    inputRef: Ref<HTMLInputElement>;
    onSearchChange: (searchText: string) => void;
}

type SearchFieldProps = Omit<UserSearchBarProps, "page" | "pageStatus">;

interface PageStatusProps {
    statusText: string;
}

// USRIDIN's LENGTH and its tie to the ERRMSG line while in error. The field keeps the cursor but
// refuses keystrokes while a call is in flight.
function SearchSlotProps(hasError: boolean, isBusy: boolean) {
    return {
        htmlInput: { maxLength: SEARCH_MAX_LENGTH, ...ErrorMessageReference(hasError) },
        input: { readOnly: isBusy },
    };
}

// USRIDIN: free text sent as typed; the server's ported edits are its only check.
function SearchField(props: SearchFieldProps) {
    const isBusy = useScreenBusy();
    return (
        <TextField
            label={SEARCH_LABEL}
            value={props.searchText}
            error={props.hasError}
            inputRef={props.inputRef}
            onChange={(event) => props.onSearchChange(event.target.value)}
            autoComplete="off"
            fullWidth
            slotProps={SearchSlotProps(props.hasError, isBusy)}
        />
    );
}

// A visually hidden live region of its own, apart from MessageLine's, so every new page is
// announced to assistive technology even when its ERRMSG line is empty.
function PageStatus({ statusText }: PageStatusProps) {
    return (
        <Box
            component="span"
            role="status"
            aria-live="polite"
            aria-atomic="true"
            sx={VISUALLY_HIDDEN_STYLE}
        >
            {statusText}
        </Box>
    );
}

// Row 4's Page: indicator sits beside the row-6 search field. It shows the number without the
// zero padding PAGENUM got from CDEMO-CU00-PAGE-NUM 9(08), as the modernized layout shows it.
function UserSearchBar({ page, pageStatus, ...searchFieldProps }: UserSearchBarProps) {
    return (
        <Grid container spacing={4} sx={CENTRE_ALIGNED_ROW_STYLE}>
            <Grid size={{ xs: 12, md: 6 }}>
                <SearchField {...searchFieldProps} />
            </Grid>
            <Grid size={{ xs: 12, md: 6 }}>
                <Typography variant="bodyLarge" component="p">
                    {`${PAGE_LABEL} ${page ?? BLANK_TEXT}`}
                </Typography>
                <PageStatus statusText={pageStatus} />
            </Grid>
        </Grid>
    );
}

type UserListBodyProps = Omit<UserListModel, "keys" | "invalidKeyAction" | "isBusy">;

type UserRowsProps = Pick<UserListBodyProps, "screen" | "setScreen">;

// The ten USRIDnn/FNAMEnn/LNAMEnn/UTYPEnn rows with their SELnnnn codes, and the row-21 hint. The
// codes refuse changes while a call is in flight.
function UserRows({ screen, setScreen }: UserRowsProps) {
    const isBusy = useScreenBusy();
    return (
        <>
            <SelectionTable
                columns={LIST_COLUMNS}
                rows={ToSelectionRows(screen)}
                allowedCodes={ALLOWED_SELECTION_CODES}
                onSelectionChange={(rowKey, code) =>
                    setScreen(WithSelectionCode(Number(rowKey), code))
                }
                label={SCREEN_TITLE}
                readOnly={isBusy}
            />
            <Typography variant="bodyLarge" component="p">
                {SELECTION_INSTRUCTION}
            </Typography>
        </>
    );
}

function UserListBody({ screen, setScreen, searchInputRef }: UserListBodyProps) {
    return (
        <>
            <UserSearchBar
                searchText={screen.searchText}
                page={screen.pageState.page}
                pageStatus={PageStatusText(screen.rows, screen.pageState.page)}
                hasError={screen.feedback.field_errors.includes(SEARCH_FIELD)}
                inputRef={searchInputRef}
                onSearchChange={(searchText) => PatchScreen(setScreen, { searchText })}
            />
            <UserRows screen={screen} setScreen={setScreen} />
        </>
    );
}

function UserListScreen() {
    const { keys, invalidKeyAction, isBusy, ...model } = useUserList();
    useFunctionKeys(keys, invalidKeyAction);
    return (
        <ScreenShell
            {...SCREEN_IDENTITY}
            isBusy={isBusy}
            isMapPending={model.screen.isAwaitingFirstPage}
        >
            <UserListBody {...model} />
            <MessageLine
                message={model.screen.feedback.message}
                severity={LIST_MESSAGE_SEVERITY}
                infoMessage={model.screen.feedback.info_message}
            />
            <FunctionKeyBar keys={keys} />
        </ScreenShell>
    );
}

export default function AdminUsersListPage() {
    return (
        <Suspense fallback={null}>
            <UserListScreen />
        </Suspense>
    );
}
