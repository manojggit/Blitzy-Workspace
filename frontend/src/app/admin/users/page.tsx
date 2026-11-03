// Replaces: BMS map COUSR00, program COUSR00C (CU00); copybooks COUSR00 (cpy-bms), CSUSR01Y,
// COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
"use client";

import { Grid, TextField, Typography } from "@mui/material";
import { isAxiosError } from "axios";
import { useRouter } from "next/navigation";
import {
    Suspense,
    useEffect,
    useRef,
    useState,
    type Dispatch,
    type Ref,
    type RefObject,
    type SetStateAction,
} from "react";

import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import { ErrorMessageReference, MessageLine } from "@/components/MessageLine";
import { ScreenShell } from "@/components/ScreenShell";
import {
    SelectionTable,
    type SelectionColumn,
    type SelectionRow,
} from "@/components/SelectionTable";
import {
    Direction,
    LegacyErrorBody,
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    type CompleteScreenFeedback,
    type LegacyErrorResponse,
    type MessageSeverity,
    type PageState,
} from "@/lib/apiClient";
import {
    ListUsers,
    SelectUsers,
    type UserListQuery,
    type UserListResponse,
    type UserListRow,
    type UserSelection,
} from "@/lib/api/users";
import { useFunctionKeys, type FunctionKeyDefinition } from "@/lib/useFunctionKeys";

const TRANSACTION_ID = "CU00";
const PROGRAM_NAME = "COUSR00C";
const SCREEN_TITLE = "List Users";
// USRIDIN LENGTH=8 (USRIDINI X(8)).
const SEARCH_MAX_LENGTH = 8;
const SEARCH_LABEL = "Search User ID:";
const PAGE_LABEL = "Page:";
// The JSON name the server gives USRIDIN in cursor_field and field_errors.
const SEARCH_FIELD = "search_user_id";
const ALLOWED_SELECTION_CODES = ["U", "D"] as const;
// The row-8 column headings, their centring blanks trimmed, bound to the UserListRow keys.
const LIST_COLUMNS: SelectionColumn[] = [
    { field: "user_id", header: "User ID" },
    { field: "first_name", header: "First Name" },
    { field: "last_name", header: "Last Name" },
    { field: "user_type", header: "Type" },
];
const SELECTION_INSTRUCTION = "Type 'U' to Update or 'D' to Delete a User from the list";
// PF3 moves COADM01C to CDEMO-TO-PROGRAM.
const ADMIN_MENU_ROUTE = "/admin";
const BLANK_TEXT = "";
// COUSR00C never changes ERRMSGC, so every message keeps the map's COLOR=RED.
const LIST_MESSAGE_SEVERITY: MessageSeverity = SeverityForOutcome({
    isError: true,
    isCompletedWrite: false,
});

type FieldRefs = Partial<Record<string, HTMLInputElement | null>>;
type PageRouter = ReturnType<typeof useRouter>;

// The COUSR00 screen the page holds. Selection codes are keyed by the user ID of their row.
interface ListScreenState {
    rows: readonly UserListRow[];
    pageState: PageState;
    selectionCodes: Readonly<Record<string, string>>;
    searchText: string;
    feedback: CompleteScreenFeedback;
    isBusy: boolean;
}

type ScreenUpdate = Dispatch<SetStateAction<ListScreenState>>;

interface ScreenActions {
    setScreen: ScreenUpdate;
    router: PageRouter;
}

// The body of ENTER's STARTBR NOTFND refusal, which carries page 0 beside the legacy keys.
type EnterRefusalBody = LegacyErrorResponse & PageState;

const EMPTY_FEEDBACK: CompleteScreenFeedback = {
    message: BLANK_TEXT,
    info_message: BLANK_TEXT,
    field_errors: [],
    cursor_field: BLANK_TEXT,
};

// The keys stay locked until the first entry's browse answers, as the 3270 keyboard stays
// locked until the first SEND MAP arrives.
const INITIAL_SCREEN: ListScreenState = {
    rows: [],
    pageState: {},
    selectionCodes: {},
    searchText: BLANK_TEXT,
    feedback: EMPTY_FEEDBACK,
    isBusy: true,
};

function PatchScreen(setScreen: ScreenUpdate, patch: Partial<ListScreenState>): void {
    setScreen((current) => ({ ...current, ...patch }));
}

// The trim only decides whether a message is shown; shown text is never altered.
function IsBlank(text: string): boolean {
    return text.trim() === BLANK_TEXT;
}

function FeedbackOf(response: UserListResponse): CompleteScreenFeedback {
    return {
        ...EMPTY_FEEDBACK,
        message: response.message ?? BLANK_TEXT,
        cursor_field: response.cursor_field === undefined ? BLANK_TEXT : response.cursor_field,
    };
}

// A shown page replaces the rows, keys and message. Its selection codes start blank: a code
// belongs to the user it was chosen for, whereas a 3270 SELnnnn field kept its text by screen
// position and would have offered it against whichever user the new page put there. A browse
// that PROCESS-PAGE-FORWARD runs to its end also blanks USRIDIN (MOVE SPACE TO USRIDINO): the
// first entry, ENTER and F8. F7's PROCESS-PAGE-BACKWARD leaves the typed search text.
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
    const shownPage = { rows, pageState, selectionCodes: {}, feedback: FeedbackOf(response) };
    return blanksSearch ? { ...shownPage, searchText: BLANK_TEXT } : shownPage;
}

// Only an axios error is a legacy outcome; anything else is a defect and propagates.
function FailureFeedback(error: unknown): CompleteScreenFeedback {
    if (!isAxiosError(error)) {
        throw error;
    }
    return ToScreenFeedback(error);
}

// A failed ENTER keeps the rows and keys and clears the selection codes. Its STARTBR NOTFND
// refusal carries page 0 and sets no ERR-FLG, so the browse still ends by blanking USRIDIN; a
// file error sets ERR-FLG and leaves the search text.
function EnterFailureUpdate(error: unknown): SetStateAction<ListScreenState> {
    const feedback = FailureFeedback(error);
    const refusedPage = LegacyErrorBody<EnterRefusalBody>(error)?.page;
    if (refusedPage === undefined) {
        return (current) => ({ ...current, feedback, selectionCodes: {} });
    }
    return (current) => ({
        ...current,
        feedback,
        selectionCodes: {},
        searchText: BLANK_TEXT,
        pageState: { ...current.pageState, page: refusedPage },
    });
}

// Every shown row in display order with its code as chosen, blank included: the server takes
// the first non-blank code, as PROCESS-ENTER-KEY's EVALUATE TRUE does.
function ToSelections(screen: ListScreenState): UserSelection[] {
    return screen.rows.map((row) => ({
        user_id: row.user_id,
        selection: screen.selectionCodes[row.user_id] ?? BLANK_TEXT,
    }));
}

// ENTER sends the search key exactly as typed. A U or D selection answers with the route of
// the update or delete screen, replacing XCTL COUSR02C or COUSR03C.
async function HandleEnter(actions: ScreenActions, screen: ListScreenState): Promise<void> {
    const request = { selections: ToSelections(screen), search_user_id: screen.searchText };
    try {
        const response = await SelectUsers(request);
        if (response.route) {
            actions.router.push(response.route);
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

// A refused page keeps the rows and codes on the screen, as the SEND without ERASE did.
async function HandlePage(
    actions: ScreenActions,
    screen: ListScreenState,
    direction: Direction,
): Promise<void> {
    try {
        const response = await ListUsers(PageQuery(screen.pageState, direction));
        const blanksSearch = direction === Direction.FORWARD;
        PatchScreen(actions.setScreen, ShownPageUpdate(response, blanksSearch));
    } catch (error) {
        PatchScreen(actions.setScreen, { feedback: FailureFeedback(error) });
    }
}

// The first entry runs PROCESS-ENTER-KEY with a blank search key. A message carried here from
// another screen shows unless the browse sets one of its own.
async function LoadFirstPage(setScreen: ScreenUpdate, pendingMessage: string): Promise<void> {
    try {
        const response = await ListUsers({});
        const shownPage = ShownPageUpdate(response, true);
        const responseMessage = response.message ?? BLANK_TEXT;
        const message = IsBlank(responseMessage) ? pendingMessage : responseMessage;
        PatchScreen(setScreen, { ...shownPage, feedback: { ...FeedbackOf(response), message } });
    } catch (error) {
        setScreen(EnterFailureUpdate(error));
    }
}

async function SettleBusy(setScreen: ScreenUpdate, task: () => Promise<void>): Promise<void> {
    try {
        await task();
    } finally {
        PatchScreen(setScreen, { isBusy: false });
    }
}

async function RunBusy(setScreen: ScreenUpdate, task: () => Promise<void>): Promise<void> {
    PatchScreen(setScreen, { isBusy: true });
    await SettleBusy(setScreen, task);
}

// One server round trip per key press, with every key locked until it answers.
function BusyKeyPress(actions: ScreenActions, task: () => Promise<void>): () => void {
    return () => {
        void RunBusy(actions.setScreen, task);
    };
}

// The legend ENTER=Continue  F3=Back  F7=Backward  F8=Forward, in its order.
function BuildKeys(actions: ScreenActions, screen: ListScreenState): FunctionKeyDefinition[] {
    const disabled = screen.isBusy;
    const enter = BusyKeyPress(actions, () => HandleEnter(actions, screen));
    const backward = BusyKeyPress(actions, () => HandlePage(actions, screen, Direction.BACKWARD));
    const forward = BusyKeyPress(actions, () => HandlePage(actions, screen, Direction.FORWARD));
    const back = (): void => actions.router.push(ADMIN_MENU_ROUTE);
    return [
        { key: "ENTER", label: "Continue", onPress: enter, disabled },
        { key: "F3", label: "Back", onPress: back, disabled },
        { key: "F7", label: "Backward", onPress: backward, disabled },
        { key: "F8", label: "Forward", onPress: forward, disabled },
    ];
}

function ToSelectionRows(screen: ListScreenState): SelectionRow[] {
    return screen.rows.map((row) => ({
        rowKey: row.user_id,
        cells: {
            user_id: row.user_id,
            first_name: row.first_name,
            last_name: row.last_name,
            user_type: row.user_type,
        },
        selectedCode: screen.selectionCodes[row.user_id] ?? BLANK_TEXT,
    }));
}

function WithSelectionCode(rowKey: string, code: string): SetStateAction<ListScreenState> {
    return (current) => ({
        ...current,
        selectionCodes: { ...current.selectionCodes, [rowKey]: code },
    });
}

// React recognises hooks only by the camelCase "use" prefix, so useFirstEntry, useCursorFocus
// and useUserList are a framework-mandated exception to the MK_Modernization Rule's PascalCase
// function naming.

// The pending message is taken once, so an effect React runs twice still shows it.
function useFirstEntry(setScreen: ScreenUpdate): void {
    const pendingMessageRef = useRef<string | null>(null);
    useEffect(() => {
        pendingMessageRef.current ??= TakePendingMessage();
        const pendingMessage = pendingMessageRef.current;
        void SettleBusy(setScreen, () => LoadFirstPage(setScreen, pendingMessage));
    }, [setScreen]);
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

interface UserListModel {
    screen: ListScreenState;
    setScreen: ScreenUpdate;
    keys: FunctionKeyDefinition[];
    searchInputRef: Ref<HTMLInputElement>;
}

function useUserList(): UserListModel {
    const router = useRouter();
    const [screen, setScreen] = useState<ListScreenState>(INITIAL_SCREEN);
    const fieldRefs = useRef<FieldRefs>({});
    useFirstEntry(setScreen);
    useCursorFocus(screen.feedback, fieldRefs);
    const searchInputRef = (element: HTMLInputElement | null): void => {
        fieldRefs.current[SEARCH_FIELD] = element;
    };
    const keys = BuildKeys({ setScreen, router }, screen);
    return { screen, setScreen, keys, searchInputRef };
}

interface UserSearchBarProps {
    searchText: string;
    page?: number;
    hasError: boolean;
    inputRef: Ref<HTMLInputElement>;
    onSearchChange: (searchText: string) => void;
}

type SearchFieldProps = Omit<UserSearchBarProps, "page">;

// USRIDIN: free text sent as typed; the server's ported edits are its only check.
function SearchField(props: SearchFieldProps) {
    return (
        <TextField
            label={SEARCH_LABEL}
            value={props.searchText}
            error={props.hasError}
            inputRef={props.inputRef}
            onChange={(event) => props.onSearchChange(event.target.value)}
            autoComplete="off"
            fullWidth
            slotProps={{
                htmlInput: {
                    maxLength: SEARCH_MAX_LENGTH,
                    ...ErrorMessageReference(props.hasError),
                },
            }}
        />
    );
}

// Row 4's Page: indicator sits beside the row-6 search field. It shows the number without the
// zero padding PAGENUM got from CDEMO-CU00-PAGE-NUM 9(08), as the modernized layout shows it.
function UserSearchBar({ page, ...searchFieldProps }: UserSearchBarProps) {
    return (
        <Grid container spacing={4} sx={{ alignItems: "center" }}>
            <Grid size={{ xs: 12, md: 6 }}>
                <SearchField {...searchFieldProps} />
            </Grid>
            <Grid size={{ xs: 12, md: 6 }}>
                <Typography variant="bodyLarge" component="p">
                    {`${PAGE_LABEL} ${page ?? BLANK_TEXT}`}
                </Typography>
            </Grid>
        </Grid>
    );
}

type UserListBodyProps = Omit<UserListModel, "keys">;

type UserRowsProps = Pick<UserListBodyProps, "screen" | "setScreen">;

// The ten USRIDnn/FNAMEnn/LNAMEnn/UTYPEnn rows with their SELnnnn codes, and the row-21 hint.
function UserRows({ screen, setScreen }: UserRowsProps) {
    return (
        <>
            <SelectionTable
                columns={LIST_COLUMNS}
                rows={ToSelectionRows(screen)}
                allowedCodes={ALLOWED_SELECTION_CODES}
                onSelectionChange={(rowKey, code) => setScreen(WithSelectionCode(rowKey, code))}
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
                hasError={screen.feedback.field_errors.includes(SEARCH_FIELD)}
                inputRef={searchInputRef}
                onSearchChange={(searchText) => PatchScreen(setScreen, { searchText })}
            />
            <UserRows screen={screen} setScreen={setScreen} />
        </>
    );
}

function UserListScreen() {
    const { keys, ...model } = useUserList();
    useFunctionKeys(keys);
    return (
        <ScreenShell
            transactionId={TRANSACTION_ID}
            programName={PROGRAM_NAME}
            screenTitle={SCREEN_TITLE}
            isBusy={model.screen.isBusy}
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
