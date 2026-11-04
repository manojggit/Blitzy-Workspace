// Replaces: BMS map COACTVW, program COACTVWC (CAVW); copybooks COACTVW (cpy-bms), CVACT01Y,
// CVACT02Y, CVACT03Y, CVCUS01Y, CVCRD01Y, CSMSG02Y, COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
"use client";

import { Grid, Stack, TextField, Typography, type GridProps } from "@mui/material";
import axios from "axios";
import { useRouter, useSearchParams } from "next/navigation";
import {
    Suspense,
    startTransition,
    useCallback,
    useEffect,
    useRef,
    useState,
    type RefObject,
} from "react";

import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import { ErrorMessageReference, MessageLine } from "@/components/MessageLine";
import { ScreenShell } from "@/components/ScreenShell";
import { ViewAccount, type AccountViewResponse } from "@/lib/api/accounts";
import {
    CutToFieldLength,
    IsCancelledRequest,
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    type MessageSeverity,
    type ScreenFeedback,
} from "@/lib/apiClient";
import { useFunctionKeys, type FunctionKeyDefinition } from "@/lib/useFunctionKeys";

const TRANSACTION_ID = "CAVW";
const PROGRAM_NAME = "COACTVWC";
const SCREEN_TITLE = "View Account";
const SCREEN_IDENTITY = {
    transactionId: TRANSACTION_ID,
    programName: PROGRAM_NAME,
    screenTitle: SCREEN_TITLE,
} as const;

const ACCOUNT_ID_MAX_LENGTH = 11;
const ACCOUNT_ID_LABEL = "Account Number :";
const ACCOUNT_ID_INPUT_ID = "account-view-acct_id";
// The name coactvwc.py gives ACCTSID in field_errors and cursor_field.
const ACCOUNT_ID_FIELD = "acct_id";
const ACCOUNT_ID_QUERY_KEY = "acctId";
// A blank ACCTSID still reaches COACTVWC, whose key edit answers it with "No input received".
const BLANK_ACCOUNT_ID = " ";
// WS-PROMPT-FOR-INPUT, which 1200-SETUP-SCREEN-VARS shows on COACTVWC's first entry. An entry
// without an account number sends no request, so the page shows the literal itself.
const FIRST_ENTRY_INFO_MESSAGE = "Enter or update id of account to display";
const MENU_ROUTE = "/menu";
const SEARCH_KEY_LABEL = "Search";
const EXIT_KEY_LABEL = "Exit";
const CUSTOMER_DETAILS_HEADING = "Customer Details";
const CUSTOMER_DETAILS_HEADING_ID = "account-view-customer-details";
const ADDRESS_LABEL = "Address:";
const DISPLAY_FIELD_ID_PREFIX = "account-view-";
// Each display field is one native textarea row, which wraps a value wider than the field.
const DISPLAY_FIELD_ELEMENT = "textarea";
const DISPLAY_FIELD_ROWS = 1;

const HALF_ROW: GridProps["size"] = { xs: 12, md: 6 };
const THIRD_ROW: GridProps["size"] = { xs: 12, md: 4 };
const SIXTH_ROW: GridProps["size"] = { xs: 12, md: 2 };
const FULL_ROW: GridProps["size"] = 12;
// Current Cycle Credit stands alone on map row 9, so it keeps the right-hand amounts column.
const AMOUNTS_COLUMN_OFFSET: GridProps["offset"] = { md: 6 };
// ACSTFCO receives CUST-FICO-CREDIT-SCORE 9(03), so the map always shows three digits.
const FICO_SCORE_DIGITS = 3;

// The message line and cursor a response leaves on the screen.
interface ScreenMessages {
    message: string;
    severity: MessageSeverity;
    infoMessage: string;
    fieldErrors: readonly string[];
    cursorField: string;
}

interface ViewState {
    account: AccountViewResponse | null;
    feedback: ScreenMessages;
    isBusy: boolean;
}

type ViewUpdater = (update: Partial<ViewState>) => void;

type LookupStarter = (acctText: string) => void;

interface AccountLookup {
    StartLookup: LookupStarter;
    AbortLookup: () => void;
}

type AppRouter = ReturnType<typeof useRouter>;

interface AccountViewScreenState extends ViewState {
    acctText: string;
    inputRef: RefObject<HTMLInputElement | null>;
    SetAcctText: (acctText: string) => void;
    Search: () => void;
    AbortLookup: () => void;
}

type AccountDisplayField = Exclude<keyof AccountViewResponse, keyof ScreenFeedback>;

interface DisplayFieldDefinition {
    label: string;
    field: AccountDisplayField;
    size?: GridProps["size"];
    offset?: GridProps["offset"];
    // The PIC width of a 9(n) code the response carries as a number; it shows zero-padded.
    digits?: number;
}

interface ReadOnlyFieldProps {
    definition: DisplayFieldDefinition;
    value: string;
}

interface FieldCellsProps {
    definitions: readonly DisplayFieldDefinition[];
    account: AccountViewResponse | null;
}

interface ScreenProps {
    screen: AccountViewScreenState;
}

interface RecordProps {
    account: AccountViewResponse | null;
}

// COACTVW rows 5-10 in reading order. Active Y/N shares the Account Number row, then the dates
// fill the left column and the amounts the right, as on the map. Each amount shows the server's
// *_edited text, the PICOUT +ZZZ,ZZZ,ZZZ.99 picture of its map field.
const ACCOUNT_FIELD_DEFINITIONS: readonly DisplayFieldDefinition[] = [
    { label: "Active Y/N:", field: "active_status" },
    { label: "Opened:", field: "open_date" },
    { label: "Credit Limit        :", field: "credit_limit_edited" },
    { label: "Expiry:", field: "expiration_date" },
    { label: "Cash credit Limit   :", field: "cash_credit_limit_edited" },
    { label: "Reissue:", field: "reissue_date" },
    { label: "Current Balance     :", field: "curr_balance_edited" },
    {
        label: "Current Cycle Credit:",
        field: "curr_cycle_credit_edited",
        offset: AMOUNTS_COLUMN_OFFSET,
    },
    { label: "Account Group:", field: "group_id" },
    { label: "Current Cycle Debit :", field: "curr_cycle_debit_edited" },
];

// The SSN is only ever the server's masked value.
const CUSTOMER_FIELD_DEFINITIONS: readonly DisplayFieldDefinition[] = [
    { label: "Customer id  :", field: "cust_id" },
    { label: "SSN:", field: "ssn_masked" },
    { label: "Date of birth:", field: "date_of_birth" },
    { label: "FICO Score:", field: "fico_credit_score", digits: FICO_SCORE_DIGITS },
    { label: "First Name", field: "first_name", size: THIRD_ROW },
    { label: "Middle Name:", field: "middle_name", size: THIRD_ROW },
    { label: "Last Name :", field: "last_name", size: THIRD_ROW },
];

const ADDRESS_FIELD_DEFINITIONS: readonly DisplayFieldDefinition[] = [
    { label: "Line 1", field: "addr_line_1" },
    { label: "Line 2", field: "addr_line_2" },
];

// COACTVWC moves CUST-ADDR-LINE-3 to ACSCITY, so City shows address line 3.
const CONTACT_FIELD_DEFINITIONS: readonly DisplayFieldDefinition[] = [
    { label: "City", field: "addr_line_3" },
    { label: "State", field: "addr_state_cd", size: SIXTH_ROW },
    { label: "Zip", field: "addr_zip", size: SIXTH_ROW },
    { label: "Country", field: "addr_country_cd", size: SIXTH_ROW },
    { label: "Phone 1:", field: "phone_num_1" },
    { label: "Government Issued Id Ref    :", field: "govt_issued_id" },
    { label: "Phone 2:", field: "phone_num_2", size: THIRD_ROW },
    { label: "EFT Account Id:", field: "eft_account_id", size: THIRD_ROW },
    { label: "Primary Card Holder Y/N:", field: "primary_holder_ind", size: THIRD_ROW },
];

const EMPTY_FEEDBACK: ScreenMessages = {
    message: "",
    severity: SeverityForOutcome({ isError: false, isCompletedWrite: false }),
    infoMessage: "",
    fieldErrors: [],
    cursorField: "",
};

const INITIAL_VIEW: ViewState = { account: null, feedback: EMPTY_FEEDBACK, isBusy: false };

// An entry with an account number shows no information line until its lookup answers.
function EntryInfoMessage(initialAcctId: string): string {
    return initialAcctId === "" ? FIRST_ENTRY_INFO_MESSAGE : "";
}

function InitialViewFor(initialAcctId: string): ViewState {
    const feedback = { ...EMPTY_FEEDBACK, infoMessage: EntryInfoMessage(initialAcctId) };
    return { ...INITIAL_VIEW, feedback };
}

// The server's lines verbatim. CAVW only reads, so no outcome is a completed write.
function FeedbackFrom(source: ScreenFeedback, isError: boolean): ScreenMessages {
    return {
        message: source.message ?? "",
        severity: SeverityForOutcome({ isError, isCompletedWrite: false }),
        infoMessage: source.info_message ?? "",
        fieldErrors: source.field_errors ?? [],
        cursorField: source.cursor_field ?? "",
    };
}

function DisplayText(value: string | number | null | undefined): string {
    if (value === null || value === undefined) {
        return "";
    }
    return String(value);
}

// Amounts (their edited text) and dates are shown exactly as served, never parsed or reformatted;
// only a numeric PIC 9(n) code is zero-padded.
function FieldText(
    account: AccountViewResponse | null,
    definition: DisplayFieldDefinition,
): string {
    const text = DisplayText(account?.[definition.field]);
    if (definition.digits === undefined || text === "") {
        return text;
    }
    return text.padStart(definition.digits, "0");
}

function ApplyViewResult(response: AccountViewResponse, updateView: ViewUpdater): void {
    updateView({ account: response, feedback: FeedbackFrom(response, false) });
}

// COACTVWC sends the map back initialised and the error body carries no record, so the details
// are cleared; the typed account number stays in its field. An aborted lookup was superseded or
// left behind, so it shows nothing.
function ApplyViewError(error: unknown, updateView: ViewUpdater): void {
    if (IsCancelledRequest(error)) {
        return;
    }
    if (!axios.isAxiosError(error)) {
        throw error;
    }
    updateView({ account: null, feedback: FeedbackFrom(ToScreenFeedback(error), true) });
}

// The ENTER path: the ACCTSID text goes to the server exactly as typed. An aborted lookup no
// longer owns the busy state: the lookup that superseded it does, or AbortLookup has ended it.
async function RunSearch(
    acctText: string,
    updateView: ViewUpdater,
    signal: AbortSignal,
): Promise<void> {
    updateView({ isBusy: true });
    try {
        const response = await ViewAccount(acctText === "" ? BLANK_ACCOUNT_ID : acctText, {
            signal,
        });
        ApplyViewResult(response, updateView);
    } catch (error) {
        ApplyViewError(error, updateView);
    } finally {
        if (!signal.aborted) {
            updateView({ isBusy: false });
        }
    }
}

// A refusal message carried across the navigation shows, beside the entry's information line,
// until the first response replaces both.
function ShowPendingMessage(initialAcctId: string, updateView: ViewUpdater): void {
    const pendingMessage = TakePendingMessage();
    if (pendingMessage !== "") {
        const pendingFeedback = {
            message: pendingMessage,
            info_message: EntryInfoMessage(initialAcctId),
        };
        updateView({ feedback: FeedbackFrom(pendingFeedback, true) });
    }
}

// React recognises hooks only by the camelCase "use" prefix, so the hook names below are a
// framework-mandated exception to the MK_Modernization Rule's PascalCase function naming.
//
// Each lookup owns an AbortController: a new lookup aborts the one before it, and leaving the
// page or F3 aborts the one in flight. AbortLookup also ends the busy state, which no aborted
// lookup ends itself, so the keys stay free if the page is not left after all. It does so as a
// transition, so the F3 task ends at once and the browser cancels the request without waiting
// for a re-render.
function useAccountLookup(updateView: ViewUpdater): AccountLookup {
    const controllerRef = useRef<AbortController | null>(null);
    const abortLookup = useCallback(() => {
        controllerRef.current?.abort();
        startTransition(() => updateView({ isBusy: false }));
    }, [updateView]);
    useEffect(() => abortLookup, [abortLookup]);
    const startLookup = useCallback(
        (acctText: string) => {
            controllerRef.current?.abort();
            const controller = new AbortController();
            controllerRef.current = controller;
            void RunSearch(acctText, updateView, controller.signal);
        },
        [updateView],
    );
    return { StartLookup: startLookup, AbortLookup: abortLookup };
}

// The ref keeps Strict Mode's repeated mount effect from taking the message twice. Strict Mode's
// simulated unmount aborts the first lookup, so the repeated effect looks the account up again.
function useInitialScreen(
    initialAcctId: string,
    updateView: ViewUpdater,
    startLookup: LookupStarter,
): void {
    const hasShownMessageRef = useRef(false);
    useEffect(() => {
        if (!hasShownMessageRef.current) {
            hasShownMessageRef.current = true;
            ShowPendingMessage(initialAcctId, updateView);
        }
        if (initialAcctId !== "") {
            startLookup(initialAcctId);
        }
    }, [initialAcctId, updateView, startLookup]);
}

// Replaces the BMS IC attribute and COACTVWC's MOVE -1 TO ACCTSIDL: the cursor starts on ACCTSID
// and returns there whenever a response places it.
function useCursorFocus(
    inputRef: RefObject<HTMLInputElement | null>,
    feedback: ScreenMessages,
): void {
    useEffect(() => {
        inputRef.current?.focus();
    }, [inputRef]);
    useEffect(() => {
        if (feedback.cursorField === ACCOUNT_ID_FIELD) {
            inputRef.current?.focus();
        }
    }, [inputRef, feedback]);
}

// The ?acctId= account number as ACCTSID holds it; a missing one is blank, as an empty one is.
function useQueryAccountId(): string {
    const queryValue = useSearchParams().get(ACCOUNT_ID_QUERY_KEY) ?? "";
    return CutToFieldLength(queryValue, ACCOUNT_ID_MAX_LENGTH);
}

function useAccountViewScreen(): AccountViewScreenState {
    const initialAcctId = useQueryAccountId();
    const [acctText, setAcctText] = useState(initialAcctId);
    const [view, setView] = useState<ViewState>(() => InitialViewFor(initialAcctId));
    const inputRef = useRef<HTMLInputElement>(null);
    const updateView = useCallback<ViewUpdater>(
        (update) => setView((current) => ({ ...current, ...update })),
        [],
    );
    const lookup = useAccountLookup(updateView);
    useInitialScreen(initialAcctId, updateView, lookup.StartLookup);
    useCursorFocus(inputRef, view.feedback);
    const search = (): void => {
        lookup.StartLookup(acctText);
    };
    const actions = { SetAcctText: setAcctText, Search: search, AbortLookup: lookup.AbortLookup };
    return { ...view, acctText, inputRef, ...actions };
}

function BuildFunctionKeys(
    screen: AccountViewScreenState,
    exitToMenu: () => void,
): FunctionKeyDefinition[] {
    return [
        { key: "ENTER", label: SEARCH_KEY_LABEL, onPress: screen.Search, disabled: screen.isBusy },
        { key: "F3", label: EXIT_KEY_LABEL, onPress: exitToMenu },
    ];
}

// Filled and read-only, as the map's protected fields are, yet focusable and labelled. The label
// stays raised, so a blank value shows as an empty field under it. The value is a native textarea,
// which the theme sizes to its content: it keeps one row while it fits and wraps onto further rows
// where it does not, so a 25-character name or a 50-character address line is never cut off, with
// no hidden copy of the field rendered to measure it. A textarea takes no type, so the input type
// MUI passes is dropped.
function ReadOnlyField({ definition, value }: ReadOnlyFieldProps) {
    return (
        <TextField
            id={DISPLAY_FIELD_ID_PREFIX + definition.field}
            label={definition.label}
            value={value}
            variant="filled"
            fullWidth
            multiline
            rows={DISPLAY_FIELD_ROWS}
            slotProps={{
                input: { readOnly: true, inputComponent: DISPLAY_FIELD_ELEMENT },
                htmlInput: { type: undefined },
                inputLabel: { shrink: true },
            }}
        />
    );
}

function FieldCells({ definitions, account }: FieldCellsProps) {
    return (
        <>
            {definitions.map((definition) => (
                <Grid
                    key={definition.field}
                    size={definition.size ?? HALF_ROW}
                    offset={definition.offset}
                >
                    <ReadOnlyField definition={definition} value={FieldText(account, definition)} />
                </Grid>
            ))}
        </>
    );
}

// The BMS LENGTH and, while the server names ACCTSID in error, its tie to the ERRMSG line that
// holds the literal.
function AccountIdHtmlInputProps(hasError: boolean) {
    return { maxLength: ACCOUNT_ID_MAX_LENGTH, ...ErrorMessageReference(hasError) };
}

// ACCTSID, the map's only unprotected field. Its text is never trimmed, padded or checked here:
// COACTVWC's key edit runs on the server.
function AccountIdInput({ screen }: ScreenProps) {
    const hasError = screen.feedback.fieldErrors.includes(ACCOUNT_ID_FIELD);
    return (
        <Grid size={HALF_ROW}>
            <TextField
                id={ACCOUNT_ID_INPUT_ID}
                label={ACCOUNT_ID_LABEL}
                value={screen.acctText}
                onChange={(event) => screen.SetAcctText(event.target.value)}
                inputRef={screen.inputRef}
                error={hasError}
                fullWidth
                slotProps={{ htmlInput: AccountIdHtmlInputProps(hasError) }}
            />
        </Grid>
    );
}

// COACTVW rows 5-10: the account number and the account record (CVACT01Y).
function AccountDetails({ screen }: ScreenProps) {
    return (
        <Grid container spacing={4}>
            <AccountIdInput screen={screen} />
            <FieldCells definitions={ACCOUNT_FIELD_DEFINITIONS} account={screen.account} />
        </Grid>
    );
}

function AddressGroup({ account }: RecordProps) {
    return (
        <Stack role="group" aria-label={ADDRESS_LABEL} spacing={2}>
            <Typography variant="labelLarge">{ADDRESS_LABEL}</Typography>
            {ADDRESS_FIELD_DEFINITIONS.map((definition) => (
                <ReadOnlyField
                    key={definition.field}
                    definition={definition}
                    value={FieldText(account, definition)}
                />
            ))}
        </Stack>
    );
}

// COACTVW rows 11-20: the customer record (CVCUS01Y).
function CustomerDetails({ account }: RecordProps) {
    return (
        <Stack component="section" spacing={4} aria-labelledby={CUSTOMER_DETAILS_HEADING_ID}>
            <Typography variant="titleMedium" component="h2" id={CUSTOMER_DETAILS_HEADING_ID}>
                {CUSTOMER_DETAILS_HEADING}
            </Typography>
            <Grid container spacing={4}>
                <FieldCells definitions={CUSTOMER_FIELD_DEFINITIONS} account={account} />
                <Grid size={FULL_ROW}>
                    <AddressGroup account={account} />
                </Grid>
                <FieldCells definitions={CONTACT_FIELD_DEFINITIONS} account={account} />
            </Grid>
        </Stack>
    );
}

// F3 aborts the lookup in flight before it leaves, because the route change keeps this page
// mounted until the menu has loaded.
function ExitToMenu(screen: AccountViewScreenState, router: AppRouter): void {
    screen.AbortLookup();
    router.push(MENU_ROUTE);
}

// COACTVWC accepts ENTER and PF3 only (any other key is processed as ENTER), so no other key is
// bound. F3 replaces the XCTL back to the main menu. Every response sets a new feedback object, so
// its identity is the outcome: ENTER answered again with the same literal still brings the
// message line into view.
function AccountViewScreen() {
    const router = useRouter();
    const screen = useAccountViewScreen();
    const keys = BuildFunctionKeys(screen, () => ExitToMenu(screen, router));
    useFunctionKeys(keys);
    return (
        <ScreenShell {...SCREEN_IDENTITY} isBusy={screen.isBusy}>
            <AccountDetails screen={screen} />
            <CustomerDetails account={screen.account} />
            <MessageLine
                message={screen.feedback.message}
                severity={screen.feedback.severity}
                infoMessage={screen.feedback.infoMessage}
                outcomeKey={screen.feedback}
            />
            <FunctionKeyBar keys={keys} />
        </ScreenShell>
    );
}

// Each arrival with another ?acctId= account number is a fresh COACTVWC first entry, as every XCTL
// is, so the account number, the record shown and the lookup in flight start over together: the
// replaced screen's unmount aborts its lookup. The screen treats an empty acctId as no acctId.
function AccountViewEntry() {
    const acctId = useQueryAccountId();
    return <AccountViewScreen key={JSON.stringify([acctId])} />;
}

// useSearchParams needs a Suspense boundary, or the static prerender of the route fails.
export default function AccountViewPage() {
    return (
        <Suspense fallback={null}>
            <AccountViewEntry />
        </Suspense>
    );
}
