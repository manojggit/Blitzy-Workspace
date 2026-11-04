// Replaces: BMS map CORPT00, program CORPT00C (CR00); copybooks CORPT00 (cpy-bms), CVTRA05Y,
// CVTRA07Y, COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
"use client";

import {
    Fragment,
    Suspense,
    useCallback,
    useEffect,
    useId,
    useMemo,
    useRef,
    useState,
    useSyncExternalStore,
    type ChangeEvent,
    type Dispatch,
    type KeyboardEvent,
    type MouseEvent,
    type ReactNode,
    type Ref,
    type RefCallback,
    type RefObject,
    type SetStateAction,
} from "react";
import { useRouter } from "next/navigation";
import {
    Button,
    FormHelperText,
    Grid,
    Stack,
    Table,
    TableBody,
    TableCell,
    TableContainer,
    TableHead,
    TablePagination,
    TableRow,
    TextField,
    ToggleButton,
    ToggleButtonGroup,
    Typography,
    tablePaginationClasses,
    toggleButtonClasses,
    useMediaQuery,
    useTheme,
    type GridProps,
    type SxProps,
    type TextFieldProps,
    type Theme,
    type ToggleButtonGroupProps,
} from "@mui/material";
import DownloadIcon from "@mui/icons-material/Download";
import axios, { type AxiosError } from "axios";

import { ConfirmField } from "@/components/ConfirmField";
import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import {
    ErrorMessageReference,
    MessageLine,
    type ErrorMessageReferenceAttributes,
} from "@/components/MessageLine";
import { ScreenShell } from "@/components/ScreenShell";
import {
    IsCancelledRequest,
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    type MessageSeverity,
} from "@/lib/apiClient";
import {
    BuildReportExportUrl,
    DownloadReportExport,
    ExportFormat,
    FetchTransactionReport,
    ReportLineKind,
    ReportType,
    type ReportExportFile,
    type ReportLine,
    type ReportQuery,
    type TransactionReportResponse,
} from "@/lib/api/reports";
import {
    SCREEN_ENTER_PROPS,
    useFunctionKeys,
    type FunctionKeyDefinition,
} from "@/lib/useFunctionKeys";

const TRANSACTION_ID = "CR00";
const PROGRAM_NAME = "CORPT00C";
const SCREEN_TITLE = "Transaction Reports";
// PF3: XCTL COMEN01C (CORPT00C lines 187-189).
const MENU_ROUTE = "/menu";

const CONFIRM_LABEL = "The Report will be submitted for printing. Please confirm:";
const DATE_HINT = "(MM/DD/YYYY)";
// The BMS End Date literal is '  End Date :'; its leading blanks only aligned it on the 3270.
const START_DATE_LEGEND = "Start Date :";
const END_DATE_LEGEND = "End Date :";

// The legend line of the map: ENTER=Continue  F3=Back.
const ENTER_KEY_LABEL = "Continue";
const BACK_KEY_LABEL = "Back";

interface ReportTypeOption {
    value: ReportType;
    label: string;
}

// The MONTHLY, YEARLY and CUSTOM selectors of the map, in BMS order.
const REPORT_TYPE_OPTIONS: readonly ReportTypeOption[] = [
    { value: ReportType.MONTHLY, label: "Monthly (Current Month)" },
    { value: ReportType.YEARLY, label: "Yearly (Current Year)" },
    { value: ReportType.CUSTOM, label: "Custom (Date Range)" },
];
const REPORT_TYPE_GROUP_LABEL = "Report type";
// A report-type group the server names in error: every segment border, the inner ones of either
// orientation included, and the label of each segment not chosen take the error colour. The
// chosen segment keeps its container colours, so its label stays legible.
const FLAGGED_SELECTOR_STYLE: SxProps<Theme> = {
    [`& .${toggleButtonClasses.root}`]: { borderColor: "error.main" },
    [`& .${toggleButtonClasses.root}:not(.${toggleButtonClasses.selected})`]: {
        color: "error.main",
    },
};
// Each date part takes a third of the row on a phone and a sixth of it from the md breakpoint.
const DATE_PART_GRID_SIZE: GridProps["size"] = { xs: 4, md: 2 };
// The three selectors stack, as on the map, where a row of segments would overflow the screen.
const COMPACT_LAYOUT_BREAKPOINT = "md";

// CVTRA07Y TRANSACTION-HEADER-1, trimmed.
const TABLE_HEADINGS = [
    "Transaction ID",
    "Account ID",
    "Transaction Type",
    "Tran Category",
    "Tran Source",
    "Amount",
] as const;
const AMOUNT_HEADING = "Amount";
const REPORT_TABLE_LABEL = "Transaction report";
// The table's only row for a confirmed range that matched no transaction.
const EMPTY_REPORT_TEXT = "No transactions in this date range";
// The scrolling container of the table is a tab stop, so the keyboard can scroll it; it is named
// apart from the table it holds.
const REPORT_LINES_REGION_PROPS = {
    role: "region",
    "aria-label": "Transaction report lines",
    tabIndex: 0,
} as const;
// Bounds the table so its sticky headings stay in view and the PF-key bar stays reachable below a
// long report; theme spacing units, 4 dp each.
const REPORT_TABLE_MAX_HEIGHT_UNITS = 150;
const REPORT_TABLE_STYLE: SxProps<Theme> = {
    maxHeight: (theme) => theme.spacing(REPORT_TABLE_MAX_HEIGHT_UNITS),
};
// The screen mounts the TRANREPT lines a page at a time, so a report of any length mounts a
// bounded number of rows. These pages only view the print report: CBTRN03C's own page breaks stay
// in its lines, and both downloads hold every line.
const REPORT_LINES_PER_PAGE = 50;
// A single page size, so the pagination offers no size choice.
const REPORT_PAGE_SIZE_OPTIONS: readonly number[] = [REPORT_LINES_PER_PAGE];
const FIRST_REPORT_PAGE = 0;

// The CVTRA07Y REPORT-ACCOUNT-TOTALS, REPORT-PAGE-TOTALS and REPORT-GRAND-TOTALS literals.
const TOTAL_LABELS: Readonly<Partial<Record<ReportLineKind, string>>> = {
    [ReportLineKind.ACCOUNT_TOTAL]: "Account Total",
    [ReportLineKind.PAGE_TOTAL]: "Page Total",
    [ReportLineKind.GRAND_TOTAL]: "Grand Total",
};
// The leading sign of the CVTRA07Y total picture +ZZZ,ZZZ,ZZZ.ZZ: '-' for a negative total and
// '+' for any other, zero included.
const NEGATIVE_SIGN = "-";
const POSITIVE_SIGN = "+";
// A total with no digit but zero is zero, whatever sign its decimal text carries.
const NONZERO_DIGIT_PATTERN = /[1-9]/;
// TRAN-REPORT-CAT-CD PIC 9(04): the category code prints zero-padded to four digits.
const CATEGORY_CODE_DIGITS = 4;
// The '-' FILLER between each code and its description in TRANSACTION-DETAIL-REPORT.
const CODE_DESCRIPTION_SEPARATOR = "-";

// The blank runs CVTRA07Y pads between the parts of a header line: two blanks or more.
const PRINT_SEGMENT_SEPARATOR = /\s{2,}/;
// TRANSACTION-HEADER-2, which CBTRN03C also writes after every account and page total.
const DASH_RULE_PATTERN = /^-+$/;
// WS-BLANK-LINE: a gap between the report title and the column headings, with no divider.
const BLANK_LINE_CELL_STYLE: SxProps<Theme> = { borderBottom: "none" };
// A dash rule: no text, only a divider in the outline role, stronger than the row dividers.
const DASH_RULE_CELL_STYLE: SxProps<Theme> = { padding: 0, borderBottomColor: "m3.outline" };
// TRANSACTION-HEADER-1 repeated in the body, in the M3 title-small role of column headings.
const COLUMN_HEADING_CELL_STYLE: SxProps<Theme> = { typography: "titleSmall" };
// The parts of a title line sit side by side and wrap on a narrow screen; theme spacing units.
const TITLE_SEGMENTS_STYLE: SxProps<Theme> = { flexWrap: "wrap", columnGap: 8, rowGap: 1 };

// CVTRA07Y REPORT-NAME-HEADER: 'Date Range: ' <start> ' to ' <end>.
const DATE_RANGE_PREFIX = "Date Range: ";
const DATE_RANGE_SEPARATOR = " to ";

interface DownloadOption {
    format: ExportFormat;
    label: string;
}

const DOWNLOAD_OPTIONS: readonly DownloadOption[] = [
    { format: ExportFormat.CSV, label: "Download CSV" },
    { format: ExportFormat.PDF, label: "Download PDF" },
];
// Safari fails a download whose blob URL is revoked while the click is still starting it, so a
// saved file's URL is released only once the browser has had ample time to read it.
const OBJECT_URL_RELEASE_DELAY_MILLISECONDS = 40000;

// The ReportQuery wire names, which corpt00c names as cursor_field (MOVE -1 TO <field>L).
type CursorField =
    "reportType" | "startMm" | "startDd" | "startYyyy" | "endMm" | "endDd" | "endYyyy" | "confirm";
type DatePartName = Exclude<CursorField, "reportType" | "confirm">;
type TextInputName = Exclude<CursorField, "reportType">;

const CURSOR_FIELDS: readonly CursorField[] = [
    "reportType",
    "startMm",
    "startDd",
    "startYyyy",
    "endMm",
    "endDd",
    "endYyyy",
    "confirm",
];
// MONTHLY carries IC, and CORPT00C moves -1 to MONTHLYL whenever it names no other field.
const DEFAULT_CURSOR_FIELD: CursorField = "reportType";
// Of the 200 screens, only the blank-confirm prompt puts the cursor on CONFIRM (MOVE -1 TO
// CONFIRML, line 472); the invalid-value literal that also does is a 400.
const CONFIRM_CURSOR_FIELD: CursorField = "confirm";

interface ReportInputs {
    reportType: ReportType | null;
    startMm: string;
    startDd: string;
    startYyyy: string;
    endMm: string;
    endDd: string;
    endYyyy: string;
    confirm: string;
}

interface DatePartDefinition {
    name: DatePartName;
    label: string;
    // The row legend, the visible label and the part it stands for, so the accessible name
    // contains the visible label a speech-input user says.
    ariaLabel: string;
    // The BMS LENGTH of the part (SDTMM 2, SDTDD 2, SDTYYYY 4; EDTMM, EDTDD, EDTYYYY alike).
    maxLength: number;
}

const START_DATE_PARTS: readonly DatePartDefinition[] = [
    { name: "startMm", label: "MM", ariaLabel: "Start Date MM (month)", maxLength: 2 },
    { name: "startDd", label: "DD", ariaLabel: "Start Date DD (day)", maxLength: 2 },
    { name: "startYyyy", label: "YYYY", ariaLabel: "Start Date YYYY (year)", maxLength: 4 },
];
const END_DATE_PARTS: readonly DatePartDefinition[] = [
    { name: "endMm", label: "MM", ariaLabel: "End Date MM (month)", maxLength: 2 },
    { name: "endDd", label: "DD", ariaLabel: "End Date DD (day)", maxLength: 2 },
    { name: "endYyyy", label: "YYYY", ariaLabel: "End Date YYYY (year)", maxLength: 4 },
];

// CORPT00C INITIALIZE-ALL-FIELDS (line 633): every selector, date part and the confirm flag.
const EMPTY_REPORT_INPUTS: ReportInputs = {
    reportType: null,
    startMm: "",
    startDd: "",
    startYyyy: "",
    endMm: "",
    endDd: "",
    endYyyy: "",
    confirm: "",
};

interface FeedbackState {
    message: string;
    severity: MessageSeverity;
    infoMessage: string;
    // The server's field_errors: the only fields shown in error. Its cursor_field only places
    // the cursor (requestFocus).
    fieldErrors: string[];
}

// ERRMSG is COLOR=RED on the map: every message CORPT00C sends with its error flag set is red.
const ERROR_SEVERITY = SeverityForOutcome({ isError: true, isCompletedWrite: false });
// The submission is CORPT00C's one completed write (the TDQ job the online report replaces), and
// the only message it turns green: MOVE DFHGREEN TO ERRMSGC (line 448).
const SUBMITTED_SEVERITY = SeverityForOutcome({ isError: false, isCompletedWrite: true });

const EMPTY_FEEDBACK: FeedbackState = {
    message: "",
    severity: ERROR_SEVERITY,
    infoMessage: "",
    fieldErrors: [],
};

interface ReportResult {
    startDate: string;
    endDate: string;
    lines: ReportLine[];
    // The confirmed query the lines were built from; the downloads repeat it.
    submittedQuery: ReportQuery;
}

interface FocusRequest {
    field: CursorField;
    // Advances on every request, so naming the same field again still refocuses it.
    seq: number;
}

const INITIAL_FOCUS_REQUEST: FocusRequest = { field: DEFAULT_CURSOR_FIELD, seq: 0 };

type FieldRefBinder = (name: CursorField) => RefCallback<HTMLElement>;
// Takes the server's cursor_field; an unknown or absent name focuses the report type.
type FocusRequester = (field: string | null) => void;
type ReportTypeSetter = (reportType: ReportType | null) => void;
type TextInputSetter = (name: TextInputName, value: string) => void;

// The state updates an ENTER outcome makes.
interface ScreenSetters {
    setInputs: Dispatch<SetStateAction<ReportInputs>>;
    setFeedback: Dispatch<SetStateAction<FeedbackState | null>>;
    setResult: Dispatch<SetStateAction<ReportResult | null>>;
    requestFocus: FocusRequester;
}

// The trim only decides whether a message is present; the text is shown untouched.
function IsBlank(text: string): boolean {
    return text.trim() === "";
}

function ToCursorField(name: string | null): CursorField {
    return CURSOR_FIELDS.find((field) => field === name) ?? DEFAULT_CURSOR_FIELD;
}

// The fields shown in error: every field the server names in field_errors. A cursor_field it does
// not also name there, such as CONFIRM on the blank-confirm prompt, takes the cursor unmarked.
function FlaggedFields(feedback: FeedbackState): readonly string[] {
    return feedback.fieldErrors;
}

// The raw screen fields. The date parts and the confirm flag are sent exactly as keyed, because
// CORPT00C edits the raw text; an empty selection sends no reportType, which CORPT00C answers
// with "Select a report type to print report...".
function BuildQueryFromInputs(inputs: ReportInputs): ReportQuery {
    const { reportType, ...textInputs } = inputs;
    if (reportType === null) {
        return { ...textInputs };
    }
    return { reportType, ...textInputs };
}

// CORPT00C lines 445-453: INITIALIZE-ALL-FIELDS, the green literal and the cursor on MONTHLY. The
// report stays shown below the cleared form.
function ApplySuccess(
    response: TransactionReportResponse,
    query: ReportQuery,
    setters: ScreenSetters,
): void {
    setters.setResult({
        startDate: response.start_date,
        endDate: response.end_date,
        lines: response.lines,
        submittedQuery: query,
    });
    setters.setInputs(EMPTY_REPORT_INPUTS);
    setters.setFeedback({
        ...EMPTY_FEEDBACK,
        message: response.message,
        severity: SUBMITTED_SEVERITY,
    });
    setters.requestFocus(response.cursor_field);
}

// Confirm N (CORPT00C lines 480-483): INITIALIZE-ALL-FIELDS also clears the message.
function ApplyReset(response: TransactionReportResponse, setters: ScreenSetters): void {
    setters.setResult(null);
    setters.setInputs(EMPTY_REPORT_INPUTS);
    setters.setFeedback(EMPTY_FEEDBACK);
    setters.requestFocus(response.cursor_field);
}

// The blank-confirm prompt (CORPT00C lines 464-474): the error flag is set, so ERRMSG stays red,
// the keyed fields are echoed and the cursor goes to CONFIRM. The prompt names no field_errors,
// so no field is marked in error.
function ApplyPrompt(response: TransactionReportResponse, setters: ScreenSetters): void {
    setters.setResult(null);
    setters.setFeedback({
        ...EMPTY_FEEDBACK,
        message: response.message,
        severity: ERROR_SEVERITY,
    });
    setters.requestFocus(response.cursor_field);
}

// INITIALIZE-ALL-FIELDS after a confirm of N leaves both dates blank.
function IsClearedScreen(response: TransactionReportResponse): boolean {
    return response.start_date === "" && response.end_date === "";
}

function IsConfirmPrompt(response: TransactionReportResponse): boolean {
    return response.cursor_field === CONFIRM_CURSOR_FIELD;
}

// The three 200 screens are told apart by the state the server sets, never by the message, which
// is shown as sent: the cleared screen has no dates, the prompt keeps the range with the cursor on
// CONFIRM, and a confirmed report keeps the range with the cursor elsewhere, its lines empty for
// a range that matched no transaction.
function ApplyResponse(
    response: TransactionReportResponse,
    query: ReportQuery,
    setters: ScreenSetters,
): void {
    if (IsClearedScreen(response)) {
        ApplyReset(response, setters);
    } else if (IsConfirmPrompt(response)) {
        ApplyPrompt(response, setters);
    } else {
        ApplySuccess(response, query, setters);
    }
}

// A 400 edit literal or the 503 file-error literal: red, the keyed fields kept and the cursor on
// the field the server names. The interceptor has already redirected a 401 or 403.
function ApplyError(error: AxiosError, setters: ScreenSetters): void {
    setters.setResult(null);
    ShowErrorFeedback(error, setters);
}

// The legacy outcome of a failed call, or the abend text, red: every field named in field_errors
// is marked and the cursor goes to its cursor_field. Nothing else on the screen changes.
function ShowErrorFeedback(error: AxiosError, setters: ScreenSetters): void {
    const feedback = ToScreenFeedback(error);
    setters.setFeedback({
        message: feedback.message,
        severity: ERROR_SEVERITY,
        infoMessage: feedback.info_message,
        fieldErrors: feedback.field_errors,
    });
    setters.requestFocus(feedback.cursor_field);
}

interface FunctionKeyHandlers {
    onEnter: () => void;
    onBack: () => void;
    isBusy: boolean;
}

// ENTER=Continue  F3=Back, the map's whole legend. ENTER is inactive while a request runs, a
// report read or a download, as the 3270 keyboard stays locked until the program answers, so one
// ENTER is one request; the criteria fields are locked with it (ReportCriteriaSection). F3 still
// leaves the screen.
function BuildFunctionKeys({
    onEnter,
    onBack,
    isBusy,
}: FunctionKeyHandlers): readonly FunctionKeyDefinition[] {
    return [
        { key: "ENTER", label: ENTER_KEY_LABEL, onPress: onEnter, disabled: isBusy },
        { key: "F3", label: BACK_KEY_LABEL, onPress: onBack },
    ];
}

type SubmitAction = () => Promise<void>;

interface ReportInputsState {
    inputs: ReportInputs;
    setInputs: Dispatch<SetStateAction<ReportInputs>>;
    setReportType: ReportTypeSetter;
    setTextInput: TextInputSetter;
}

interface FeedbackStore {
    feedback: FeedbackState;
    setFeedback: Dispatch<SetStateAction<FeedbackState | null>>;
}

interface FieldFocus {
    bindRef: FieldRefBinder;
    requestFocus: FocusRequester;
}

interface ReportSubmission {
    isBusy: boolean;
    // Counts ENTER's report reads. The report shown always answers the latest one, so a new
    // number is a new report.
    readNumber: number;
    submit: SubmitAction;
    abortRead: ReadAborter;
}

// Downloads the shown report's confirmed query in one format.
type DownloadStarter = (query: ReportQuery, format: ExportFormat) => Promise<void>;

interface ReportDownload {
    isDownloading: boolean;
    startDownload: DownloadStarter;
    abortDownload: ReadAborter;
}

// What the download links need: whether a request runs, which locks them, and how to download.
interface ReportDownloads {
    disabled: boolean;
    startDownload: DownloadStarter;
}

// The requests of the screen: ENTER's report read and the downloads share one lock.
interface ReportRequests {
    isBusy: boolean;
    readNumber: number;
    keys: readonly FunctionKeyDefinition[];
    downloads: ReportDownloads;
}

interface ReportForm extends ReportRequests {
    inputs: ReportInputs;
    setReportType: ReportTypeSetter;
    setTextInput: TextInputSetter;
    feedback: FeedbackState;
    result: ReportResult | null;
    bindRef: FieldRefBinder;
}

// React recognises hooks only by the camelCase "use" prefix, so the hook names below are a
// framework-mandated exception to the MK_Modernization Rule's PascalCase function naming.
function useReportInputs(): ReportInputsState {
    const [inputs, setInputs] = useState<ReportInputs>(EMPTY_REPORT_INPUTS);
    const setReportType = useCallback((reportType: ReportType | null) => {
        setInputs((current) => ({ ...current, reportType }));
    }, []);
    const setTextInput = useCallback((name: TextInputName, value: string) => {
        setInputs((current) => ({ ...current, [name]: value }));
    }, []);
    return { inputs, setInputs, setReportType, setTextInput };
}

// The keyboard lock disables the criteria during a call, which clears MUI's focused state though
// the browser keeps the disabled control focused. Focusing that control again fires no focus
// event, so it is blurred first and MUI restores its focused outline, label or focus ring.
function PlaceCursor(control: HTMLElement | null | undefined): void {
    if (control === null || control === undefined) {
        return;
    }
    if (control === document.activeElement) {
        control.blur();
    }
    control.focus();
}

// The first focus request is the report type, as MONTHLY carries IC on first display.
function useFieldFocus(): FieldFocus {
    const fieldRefs = useRef<Partial<Record<CursorField, HTMLElement | null>>>({});
    const [focusRequest, setFocusRequest] = useState<FocusRequest>(INITIAL_FOCUS_REQUEST);
    useEffect(() => {
        PlaceCursor(fieldRefs.current[focusRequest.field]);
    }, [focusRequest]);
    const bindRef = useCallback((name: CursorField): RefCallback<HTMLElement> => {
        return (element) => {
            fieldRefs.current[name] = element;
        };
    }, []);
    const requestFocus = useCallback((field: string | null) => {
        setFocusRequest((current) => ({ field: ToCursorField(field), seq: current.seq + 1 }));
    }, []);
    return { bindRef, requestFocus };
}

type StoreChangeListener = () => void;
type StoreSubscriber = (onStoreChange: StoreChangeListener) => StoreChangeListener;
type SnapshotReader = () => string;

interface PendingMessageStore {
    subscribe: StoreSubscriber;
    getSnapshot: SnapshotReader;
}

// The store holds no listener, so ending a subscription has nothing to release.
function ReleaseNoListener(): void {
    return undefined;
}

function ReadNoPendingMessage(): string {
    return "";
}

// A message carried across a navigation, such as a 403 refusal, is taken from sessionStorage once,
// by the first subscription after mount: storage exists only in the browser, so the server render
// and hydration show no message. A repeated subscription (React StrictMode) keeps the message.
function CreatePendingMessageStore(): PendingMessageStore {
    let takenMessage: string | null = null;
    return {
        subscribe: (onStoreChange) => {
            if (takenMessage === null) {
                takenMessage = TakePendingMessage();
                onStoreChange();
            }
            return ReleaseNoListener;
        },
        getSnapshot: () => takenMessage ?? "",
    };
}

function PendingFeedback(pendingMessage: string): FeedbackState {
    if (IsBlank(pendingMessage)) {
        return EMPTY_FEEDBACK;
    }
    return { ...EMPTY_FEEDBACK, message: pendingMessage };
}

// The pending message shows, red, until the first ENTER outcome replaces it.
function useFeedback(): FeedbackStore {
    const [pendingStore] = useState(CreatePendingMessageStore);
    const pendingMessage = useSyncExternalStore(
        pendingStore.subscribe,
        pendingStore.getSnapshot,
        ReadNoPendingMessage,
    );
    const [feedback, setFeedback] = useState<FeedbackState | null>(null);
    return { feedback: feedback ?? PendingFeedback(pendingMessage), setFeedback };
}

// Starts a read: aborts the read it replaces and returns the new read's signal.
type ReadStarter = () => AbortSignal;
// Aborts the read in flight, if any.
type ReadAborter = () => void;

interface ReadController {
    startRead: ReadStarter;
    abortRead: ReadAborter;
}

// Each report read owns an AbortController. A new read aborts the one it replaces; F3 aborts the
// one in flight as it leaves, and the unmount does for any other navigation. The browser then
// stops downloading and parsing a report nobody will see; the server may still finish building it.
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
    return { startRead, abortRead };
}

// Shows the outcome of a failed call on the screen.
type ErrorApplier = (error: AxiosError, setters: ScreenSetters) => void;

// An abandoned read is discarded silently, before ToScreenFeedback would show it as the abend
// text. Only axios errors carry a legacy outcome, which applyError shows; any other error is a
// defect and propagates.
function ApplyReadError(
    error: unknown,
    signal: AbortSignal,
    setters: ScreenSetters,
    applyError: ErrorApplier,
): void {
    if (IsCancelledRequest(error)) {
        return;
    }
    if (!axios.isAxiosError(error)) {
        throw error;
    }
    if (!signal.aborted) {
        applyError(error, setters);
    }
}

// An answer that arrives after its read was aborted belongs to a disposed or replaced screen, so
// it changes neither the message, the lines nor the dates.
async function RunReportRead(
    query: ReportQuery,
    signal: AbortSignal,
    setters: ScreenSetters,
): Promise<void> {
    try {
        const response = await FetchTransactionReport(query, { signal });
        if (!signal.aborted) {
            ApplyResponse(response, query, setters);
        }
    } catch (error) {
        ApplyReadError(error, signal, setters, ApplyError);
    }
}

// The browser saves the received document under the server's file name through a transient
// link to it; the screen does not navigate.
function SaveReportFile(file: ReportExportFile): void {
    const objectUrl = URL.createObjectURL(file.content);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = file.fileName;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), OBJECT_URL_RELEASE_DELAY_MILLISECONDS);
}

interface DownloadRequest {
    query: ReportQuery;
    format: ExportFormat;
    signal: AbortSignal;
}

// A file that arrives after its download was aborted belongs to a disposed or replaced screen, so
// it is not saved. A failed download shows its legacy outcome, or the abend text, and leaves the
// report shown: the confirmed report it repeats is unchanged.
async function RunReportDownload(request: DownloadRequest, setters: ScreenSetters): Promise<void> {
    const { query, format, signal } = request;
    try {
        const file = await DownloadReportExport(query, format, { signal });
        if (!signal.aborted) {
            SaveReportFile(file);
        }
    } catch (error) {
        ApplyReadError(error, signal, setters, ShowErrorFeedback);
    }
}

// The screen's one-request-at-a-time lock, shared by ENTER's report read and the downloads.
type RequestLock = RefObject<boolean>;
type BusySetter = Dispatch<SetStateAction<boolean>>;
// The call a request makes, with the signal of the read that owns it.
type SignalledCall = (signal: AbortSignal) => Promise<void>;

// What one request runs with: the screen's lock, and the request's own read and busy flag.
interface ScreenRequestRunner {
    lock: RequestLock;
    startRead: ReadStarter;
    setIsBusy: BusySetter;
}

// The disabled ENTER key and download links take effect only in the render after a press, so the
// lock, taken before anything else, refuses a second ENTER or download made in that gap: one press
// is one request. The lock is released once the request settles, answered, failed or aborted, a
// rethrown defect included. An aborted request leaves the busy state set, because only F3 and the
// unmount abort one, and both leave the screen.
async function RunScreenRequest(runner: ScreenRequestRunner, call: SignalledCall): Promise<void> {
    if (runner.lock.current) {
        return;
    }
    runner.lock.current = true;
    const signal = runner.startRead();
    runner.setIsBusy(true);
    try {
        await call(signal);
    } finally {
        runner.lock.current = false;
        if (!signal.aborted) {
            runner.setIsBusy(false);
        }
    }
}

// Each download owns an AbortController, as a report read does, which F3 and the unmount abort. A
// download is never retried.
function useReportDownload(setters: ScreenSetters, lock: RequestLock): ReportDownload {
    const [isDownloading, setIsDownloading] = useState(false);
    const { startRead, abortRead } = useReadController();
    const startDownload = useCallback(
        (query: ReportQuery, format: ExportFormat) =>
            RunScreenRequest({ lock, startRead, setIsBusy: setIsDownloading }, (signal) =>
                RunReportDownload({ query, format, signal }, setters),
            ),
        [setters, startRead, lock],
    );
    return { isDownloading, startDownload, abortDownload: abortRead };
}

// ENTER sends the criteria as they stood when it was pressed. Every ENTER outcome removes or
// replaces the report shown, so the read drops it as it starts: no render then holds the
// superseded report beside the next one, which leaves it free to be collected.
function useReportSubmit(
    inputs: ReportInputs,
    setters: ScreenSetters,
    lock: RequestLock,
): ReportSubmission {
    const [isBusy, setIsBusy] = useState(false);
    const [readNumber, setReadNumber] = useState(0);
    const { startRead, abortRead } = useReadController();
    const submit = useCallback(
        () =>
            RunScreenRequest({ lock, startRead, setIsBusy }, (signal) => {
                setReadNumber((current) => current + 1);
                setters.setResult(null);
                return RunReportRead(BuildQueryFromInputs(inputs), signal, setters);
            }),
        [inputs, setters, startRead, lock],
    );
    return { isBusy, readNumber, submit, abortRead };
}

// What the PF keys act on: ENTER submits; F3 aborts every request in flight before it leaves.
interface ScreenKeyActions {
    submit: SubmitAction;
    abortRequests: ReadAborter;
    isBusy: boolean;
}

// The same definitions drive the physical keys and the on-screen buttons. F3 aborts the read or
// download in flight before it leaves, because the route change keeps this screen mounted until
// the menu has loaded.
function useScreenKeys(actions: ScreenKeyActions): readonly FunctionKeyDefinition[] {
    const router = useRouter();
    const { submit, isBusy, abortRequests } = actions;
    const keys = useMemo(
        () =>
            BuildFunctionKeys({
                onEnter: () => {
                    void submit();
                },
                onBack: () => {
                    abortRequests();
                    router.push(MENU_ROUTE);
                },
                isBusy,
            }),
        [submit, abortRequests, router, isBusy],
    );
    useFunctionKeys(keys);
    return keys;
}

// One request runs at a time: while ENTER's read or a download runs, ENTER, the criteria and the
// download links are all locked and the busy bar shows. The shared lock holds from the press,
// before the render that locks them.
function useReportRequests(inputs: ReportInputs, setters: ScreenSetters): ReportRequests {
    const lock = useRef(false);
    const reading = useReportSubmit(inputs, setters, lock);
    const { submit, abortRead, readNumber } = reading;
    const { isDownloading, startDownload, abortDownload } = useReportDownload(setters, lock);
    const isBusy = reading.isBusy || isDownloading;
    const abortRequests = useCallback(() => {
        abortRead();
        abortDownload();
    }, [abortRead, abortDownload]);
    const keys = useScreenKeys({ submit, abortRequests, isBusy });
    const downloads = useMemo(() => ({ disabled: isBusy, startDownload }), [isBusy, startDownload]);
    return { isBusy, readNumber, keys, downloads };
}

function useReportForm(): ReportForm {
    const { inputs, setInputs, setReportType, setTextInput } = useReportInputs();
    const { feedback, setFeedback } = useFeedback();
    const [result, setResult] = useState<ReportResult | null>(null);
    const { bindRef, requestFocus } = useFieldFocus();
    const setters = useMemo(
        () => ({ setInputs, setFeedback, setResult, requestFocus }),
        [setInputs, setFeedback, setResult, requestFocus],
    );
    const requests = useReportRequests(inputs, setters);
    return { inputs, setReportType, setTextInput, feedback, result, bindRef, ...requests };
}

interface ReportTypeSelectorProps {
    value: ReportType | null;
    onChange: ReportTypeSetter;
    disabled: boolean;
    firstButtonRef: Ref<HTMLButtonElement>;
    // The server names reportType in field_errors.
    isFlagged: boolean;
    // The ENTER outcome that flag comes from.
    feedback: FeedbackState;
}

type SelectorOrientation = "horizontal" | "vertical";

function useSelectorOrientation(): SelectorOrientation {
    const theme = useTheme();
    const isCompact = useMediaQuery(theme.breakpoints.down(COMPACT_LAYOUT_BREAKPOINT));
    return isCompact ? "vertical" : "horizontal";
}

// ToggleButtonGroup places its segments by position among its direct children, so each button is
// an element of the group rather than a component of its own. Enter on a segment is the map's
// ENTER, as it was from the one-byte selector field holding the cursor; Space chooses it.
function RenderReportTypeButton(
    option: ReportTypeOption,
    buttonRef: Ref<HTMLButtonElement> | undefined,
): ReactNode {
    return (
        <ToggleButton
            key={option.value}
            value={option.value}
            ref={buttonRef}
            {...SCREEN_ENTER_PROPS}
        >
            {option.label}
        </ToggleButton>
    );
}

// A flagged group reads as invalid, is described by the server's message on the message line and
// shows the error colour; the message itself is shown once, on the message line.
function FlaggedSelectorProps(isFlagged: boolean): Partial<ToggleButtonGroupProps> {
    if (!isFlagged) {
        return { "aria-invalid": false };
    }
    return {
        "aria-invalid": true,
        ...ErrorMessageReference(isFlagged),
        sx: FLAGGED_SELECTOR_STYLE,
    };
}

type ReportTypeChangeHandler = (event: MouseEvent<HTMLElement>, next: ReportType | null) => void;

interface ReportTypeChoice {
    isFlagged: boolean;
    handleChange: ReportTypeChangeHandler;
}

// A choice made after the server flagged the group answers that flag, so the group no longer
// reads as invalid; the next ENTER outcome that names the report type flags it again.
function useReportTypeChoice(props: ReportTypeSelectorProps): ReportTypeChoice {
    const [answeredFeedback, setAnsweredFeedback] = useState<FeedbackState | null>(null);
    const handleChange: ReportTypeChangeHandler = (_event, next) => {
        setAnsweredFeedback(props.feedback);
        props.onChange(next);
    };
    return { isFlagged: props.isFlagged && answeredFeedback !== props.feedback, handleChange };
}

// One exclusive choice replaces the three one-byte selectors. Pressing the chosen type again
// clears the choice, as blanking the field did, so the no-selection literal stays reachable.
function ReportTypeSelector(props: ReportTypeSelectorProps) {
    const orientation = useSelectorOrientation();
    const choice = useReportTypeChoice(props);
    return (
        <ToggleButtonGroup
            exclusive
            value={props.value}
            orientation={orientation}
            disabled={props.disabled}
            onChange={choice.handleChange}
            aria-label={REPORT_TYPE_GROUP_LABEL}
            {...FlaggedSelectorProps(choice.isFlagged)}
        >
            {REPORT_TYPE_OPTIONS.map((option, position) =>
                RenderReportTypeButton(option, position === 0 ? props.firstButtonRef : undefined),
            )}
        </ToggleButtonGroup>
    );
}

// Every part is described by its row's (MM/DD/YYYY) hint; a part the server names in error is
// described first by the ERRMSG line that holds its literal.
function DatePartDescription(isFlagged: boolean, hintId: string): ErrorMessageReferenceAttributes {
    if (isFlagged) {
        return ErrorMessageReference(isFlagged, hintId);
    }
    return { "aria-describedby": hintId };
}

// Only the BMS LENGTH limits the part: the keyed text is never trimmed, padded or checked here,
// so every CORPT00C date literal stays reachable.
function DatePartSlotProps(
    part: DatePartDefinition,
    isFlagged: boolean,
    hintId: string,
): TextFieldProps["slotProps"] {
    return {
        htmlInput: {
            maxLength: part.maxLength,
            inputMode: "numeric",
            "aria-label": part.ariaLabel,
            ...DatePartDescription(isFlagged, hintId),
        },
    };
}

interface DatePartInputProps {
    part: DatePartDefinition;
    value: string;
    onChange: TextInputSetter;
    isFlagged: boolean;
    disabled: boolean;
    inputRef: RefCallback<HTMLElement>;
    // The element id of the row's (MM/DD/YYYY) hint.
    hintId: string;
}

function DatePartInput(props: DatePartInputProps) {
    const { part, value, onChange, isFlagged, disabled, inputRef } = props;
    return (
        <TextField
            label={part.label}
            value={value}
            onChange={(event: ChangeEvent<HTMLInputElement>) =>
                onChange(part.name, event.target.value)
            }
            error={isFlagged}
            disabled={disabled}
            variant="outlined"
            size="small"
            fullWidth
            inputRef={inputRef}
            slotProps={DatePartSlotProps(part, isFlagged, props.hintId)}
        />
    );
}

interface DatePartsFieldProps {
    legend: string;
    parts: readonly DatePartDefinition[];
    inputs: ReportInputs;
    onChange: TextInputSetter;
    errorFields: readonly string[];
    disabled: boolean;
    bindRef: FieldRefBinder;
}

interface DatePartsRowProps extends DatePartsFieldProps {
    hintId: string;
}

// The MM, DD and YYYY parts of one map row.
function DatePartsRow(props: DatePartsRowProps) {
    return (
        <Grid container spacing={2}>
            {props.parts.map((part) => (
                <Grid key={part.name} size={DATE_PART_GRID_SIZE}>
                    <DatePartInput
                        part={part}
                        value={props.inputs[part.name]}
                        onChange={props.onChange}
                        isFlagged={props.errorFields.includes(part.name)}
                        disabled={props.disabled}
                        inputRef={props.bindRef(part.name)}
                        hintId={props.hintId}
                    />
                </Grid>
            ))}
        </Grid>
    );
}

// The legend, the date parts and the (MM/DD/YYYY) hint of one map row, which describes each part.
// The hint is the row's helper text, on the outlined fields' inset and directly below them.
function DatePartsField(props: DatePartsFieldProps) {
    const hintId = useId();
    return (
        <Stack spacing={2} useFlexGap role="group" aria-label={props.legend}>
            <Typography variant="titleSmall" component="p">
                {props.legend}
            </Typography>
            <Stack>
                <DatePartsRow {...props} hintId={hintId} />
                <FormHelperText id={hintId} variant="outlined" disabled={props.disabled}>
                    {DATE_HINT}
                </FormHelperText>
            </Stack>
        </Stack>
    );
}

interface DateFieldGroup {
    legend: string;
    parts: readonly DatePartDefinition[];
}

// Rows 13 and 14 of the map.
const DATE_FIELD_GROUPS: readonly DateFieldGroup[] = [
    { legend: START_DATE_LEGEND, parts: START_DATE_PARTS },
    { legend: END_DATE_LEGEND, parts: END_DATE_PARTS },
];

interface ReportFieldsProps {
    form: ReportForm;
    errorFields: readonly string[];
}

function DateRangeFields({ form, errorFields }: ReportFieldsProps) {
    return DATE_FIELD_GROUPS.map((group) => (
        <DatePartsField
            key={group.legend}
            legend={group.legend}
            parts={group.parts}
            inputs={form.inputs}
            onChange={form.setTextInput}
            errorFields={errorFields}
            disabled={form.isBusy}
            bindRef={form.bindRef}
        />
    ));
}

function ReportConfirmField({ form, errorFields }: ReportFieldsProps) {
    return (
        <ConfirmField
            label={CONFIRM_LABEL}
            value={form.inputs.confirm}
            onChange={(value) => form.setTextInput("confirm", value)}
            error={errorFields.includes("confirm")}
            disabled={form.isBusy}
            inputRef={form.bindRef("confirm")}
        />
    );
}

interface ReportFormSectionProps {
    form: ReportForm;
}

// The report type, both date ranges and the confirm flag, in map order. All of them are locked
// while the request runs, as the 3270 keyboard is until CORPT00C answers, so the answer always
// applies to the criteria that were sent; it unlocks them in the render that places the cursor.
function ReportCriteriaSection({ form }: ReportFormSectionProps) {
    const errorFields = FlaggedFields(form.feedback);
    return (
        <Stack spacing={4} useFlexGap>
            <ReportTypeSelector
                value={form.inputs.reportType}
                onChange={form.setReportType}
                disabled={form.isBusy}
                firstButtonRef={form.bindRef("reportType")}
                isFlagged={errorFields.includes("reportType")}
                feedback={form.feedback}
            />
            <DateRangeFields form={form} errorFields={errorFields} />
            <ReportConfirmField form={form} errorFields={errorFields} />
        </Stack>
    );
}

// ERRMSG and INFOMSG. Every flagged control names the ERRMSG Alert as its description.
function ReportMessageLine({ form }: ReportFormSectionProps) {
    return (
        <MessageLine
            message={form.feedback.message}
            severity={form.feedback.severity}
            infoMessage={form.feedback.infoMessage}
        />
    );
}

interface ReportLineRowProps {
    line: ReportLine;
}

function FormatCategoryCode(categoryCode: number | null): string {
    if (categoryCode === null) {
        return "";
    }
    return String(categoryCode).padStart(CATEGORY_CODE_DIGITS, "0");
}

// TRAN-REPORT-TYPE-CD '-' TRAN-REPORT-TYPE-DESC, and the category pairing alike.
function CodeWithDescription(code: string, description: string): string {
    return code + CODE_DESCRIPTION_SEPARATOR + description;
}

// TRANSACTION-DETAIL-REPORT, with the server's texts shown as sent.
function DetailRow({ line }: ReportLineRowProps) {
    return (
        <TableRow>
            <TableCell>{line.tran_id ?? ""}</TableCell>
            <TableCell>{line.acct_id ?? ""}</TableCell>
            <TableCell>{CodeWithDescription(line.type_cd ?? "", line.type_desc ?? "")}</TableCell>
            <TableCell>
                {CodeWithDescription(FormatCategoryCode(line.cat_cd ?? null), line.cat_desc ?? "")}
            </TableCell>
            <TableCell>{line.source ?? ""}</TableCell>
            <TableCell align="right">{line.amount ?? ""}</TableCell>
        </TableRow>
    );
}

// A total's decimal text behind the sign its +ZZZ,ZZZ,ZZZ.ZZ picture prints.
function SignedTotalAmount(amount: ReportLine["amount"]): string {
    if (amount === null) {
        return "";
    }
    const unsignedAmount = amount.startsWith(NEGATIVE_SIGN)
        ? amount.slice(NEGATIVE_SIGN.length)
        : amount;
    const isNegative = unsignedAmount !== amount && NONZERO_DIGIT_PATTERN.test(unsignedAmount);
    return (isNegative ? NEGATIVE_SIGN : POSITIVE_SIGN) + unsignedAmount;
}

// REPORT-ACCOUNT-TOTALS, REPORT-PAGE-TOTALS and REPORT-GRAND-TOTALS.
function TotalRow({ line }: ReportLineRowProps) {
    return (
        <TableRow>
            <TableCell variant="footer" colSpan={TABLE_HEADINGS.length - 1}>
                {TOTAL_LABELS[line.line_kind] ?? line.text}
            </TableCell>
            <TableCell variant="footer" align="right">
                {SignedTotalAmount(line.amount ?? null)}
            </TableCell>
        </TableRow>
    );
}

type ColumnAlignment = "left" | "right";

function HeadingAlignment(heading: string): ColumnAlignment {
    return heading === AMOUNT_HEADING ? "right" : "left";
}

// WS-BLANK-LINE.
function BlankLineRow() {
    return (
        <TableRow>
            <TableCell colSpan={TABLE_HEADINGS.length} sx={BLANK_LINE_CELL_STYLE} />
        </TableRow>
    );
}

// TRANSACTION-HEADER-2: the dashes become a divider across every column.
function DashRuleRow() {
    return (
        <TableRow>
            <TableCell colSpan={TABLE_HEADINGS.length} sx={DASH_RULE_CELL_STYLE} />
        </TableRow>
    );
}

// TRANSACTION-HEADER-1 after each page break: every heading in its own column, aligned as the
// sticky headings are.
function ColumnHeadingsRow() {
    return (
        <TableRow>
            {TABLE_HEADINGS.map((heading) => (
                <TableCell
                    key={heading}
                    align={HeadingAlignment(heading)}
                    sx={COLUMN_HEADING_CELL_STYLE}
                >
                    {heading}
                </TableCell>
            ))}
        </TableRow>
    );
}

interface TitleLineRowProps {
    segments: readonly string[];
}

// REPORT-NAME-HEADER, or any other header text: each part the print line sets apart with blanks,
// side by side across the table.
function TitleLineRow({ segments }: TitleLineRowProps) {
    return (
        <TableRow>
            <TableCell colSpan={TABLE_HEADINGS.length}>
                <Stack direction="row" sx={TITLE_SEGMENTS_STYLE}>
                    {segments.map((segment, position) => (
                        <Typography key={position} variant="titleSmall" component="span">
                            {segment}
                        </Typography>
                    ))}
                </Stack>
            </TableCell>
        </TableRow>
    );
}

function IsColumnHeadings(segments: readonly string[]): boolean {
    return (
        segments.length === TABLE_HEADINGS.length &&
        segments.every((segment, column) => segment === TABLE_HEADINGS[column])
    );
}

// The page headers CBTRN03C repeats, and the dash rules it writes after the totals, read from
// their print text: a blank line, a dash rule, the column headings or a title line.
function HeaderRow({ line }: ReportLineRowProps) {
    const printText = (line.text ?? "").trim();
    if (printText === "") {
        return <BlankLineRow />;
    }
    if (DASH_RULE_PATTERN.test(printText)) {
        return <DashRuleRow />;
    }
    const segments = printText.split(PRINT_SEGMENT_SEPARATOR);
    if (IsColumnHeadings(segments)) {
        return <ColumnHeadingsRow />;
    }
    return <TitleLineRow segments={segments} />;
}

const ROW_RENDERERS: Readonly<Record<ReportLineKind, (line: ReportLine) => ReactNode>> = {
    [ReportLineKind.HEADER]: (line) => <HeaderRow line={line} />,
    [ReportLineKind.DETAIL]: (line) => <DetailRow line={line} />,
    [ReportLineKind.ACCOUNT_TOTAL]: (line) => <TotalRow line={line} />,
    [ReportLineKind.PAGE_TOTAL]: (line) => <TotalRow line={line} />,
    [ReportLineKind.GRAND_TOTAL]: (line) => <TotalRow line={line} />,
};

// A kind this page does not know is shown from its print text, like a header.
function RenderReportLine(line: ReportLine): ReactNode {
    const renderRow = ROW_RENDERERS[line.line_kind] ?? ROW_RENDERERS[ReportLineKind.HEADER];
    return renderRow(line);
}

interface ReportLinesTableProps {
    lines: readonly ReportLine[];
}

function ReportTableHead() {
    return (
        <TableHead>
            <TableRow>
                {TABLE_HEADINGS.map((heading) => (
                    <TableCell key={heading} align={HeadingAlignment(heading)}>
                        {heading}
                    </TableCell>
                ))}
            </TableRow>
        </TableHead>
    );
}

interface ReportLinesPageProps {
    lines: readonly ReportLine[];
    page: number;
}

// CBTRN03C prints no line for a range that matched no transaction, so the table says so.
function EmptyReportRow() {
    return (
        <TableRow>
            <TableCell colSpan={TABLE_HEADINGS.length} align="center">
                {EMPTY_REPORT_TEXT}
            </TableCell>
        </TableRow>
    );
}

interface ReportPageRowsProps {
    pageLines: readonly ReportLine[];
    firstPosition: number;
}

// The order never changes, so each row keys on its position in the whole report.
function ReportPageRows({ pageLines, firstPosition }: ReportPageRowsProps) {
    if (pageLines.length === 0) {
        return <EmptyReportRow />;
    }
    return (
        <>
            {pageLines.map((line, offset) => (
                <Fragment key={`${line.line_kind}-${firstPosition + offset}`}>
                    {RenderReportLine(line)}
                </Fragment>
            ))}
        </>
    );
}

// One on-screen page of the TRANREPT lines, in print order. Each page remounts the container,
// which therefore opens scrolled to the page's first line.
function ReportLinesPageTable({ lines, page }: ReportLinesPageProps) {
    const firstPosition = page * REPORT_LINES_PER_PAGE;
    const pageLines = lines.slice(firstPosition, firstPosition + REPORT_LINES_PER_PAGE);
    return (
        <TableContainer key={page} sx={REPORT_TABLE_STYLE} {...REPORT_LINES_REGION_PROPS}>
            <Table stickyHeader size="small" aria-label={REPORT_TABLE_LABEL}>
                <ReportTableHead />
                <TableBody>
                    <ReportPageRows pageLines={pageLines} firstPosition={firstPosition} />
                </TableBody>
            </Table>
        </TableContainer>
    );
}

type ReportPageChanger = (event: MouseEvent<HTMLButtonElement> | null, page: number) => void;

interface ReportLinesPaginationProps {
    lineCount: number;
    page: number;
    onPageChange: ReportPageChanger;
}

// The page arrows that can still page; the pagination's actions hold only previous and next.
const ENABLED_PAGE_ARROW_SELECTOR = `.${tablePaginationClasses.actions} button:not(:disabled)`;

interface PageArrowFocus {
    paginationRef: RefObject<HTMLDivElement | null>;
    changePage: ReportPageChanger;
}

function FocusEnabledPageArrow(pagination: HTMLElement | null): void {
    pagination?.querySelector<HTMLButtonElement>(ENABLED_PAGE_ARROW_SELECTOR)?.focus();
}

// An arrow its own page change disables, on the first or the last page, would leave the focus on
// nothing, so the focus moves to the arrow that still pages. Only an arrow activation moves it:
// neither the first display nor a new report takes the focus.
function usePageArrowFocus(page: number, onPageChange: ReportPageChanger): PageArrowFocus {
    const paginationRef = useRef<HTMLDivElement | null>(null);
    const activatedArrowRef = useRef<HTMLButtonElement | null>(null);
    useEffect(() => {
        const activatedArrow = activatedArrowRef.current;
        activatedArrowRef.current = null;
        if (activatedArrow?.disabled === true) {
            FocusEnabledPageArrow(paginationRef.current);
        }
    }, [page]);
    const changePage: ReportPageChanger = (event, nextPage) => {
        activatedArrowRef.current = event?.currentTarget ?? null;
        onPageChange(event, nextPage);
    };
    return { paginationRef, changePage };
}

// Shown only for a report longer than one page. These pages are not PF-key paging: the CORPT00
// legend is ENTER and F3 alone, and the pages only view the print report.
function ReportLinesPagination({ lineCount, page, onPageChange }: ReportLinesPaginationProps) {
    const { paginationRef, changePage } = usePageArrowFocus(page, onPageChange);
    if (lineCount <= REPORT_LINES_PER_PAGE) {
        return null;
    }
    return (
        <TablePagination
            ref={paginationRef}
            component="div"
            count={lineCount}
            page={page}
            rowsPerPage={REPORT_LINES_PER_PAGE}
            rowsPerPageOptions={REPORT_PAGE_SIZE_OPTIONS}
            onPageChange={changePage}
        />
    );
}

// Each report mounts its own table (TransactionReportsScreen keys the report by its read), so a
// new report opens on its first page with no effect resetting it. The state holds the chosen page
// only, never the lines, so a replaced report is not kept alive by it.
function ReportLinesTable({ lines }: ReportLinesTableProps) {
    const [page, setPage] = useState(FIRST_REPORT_PAGE);
    const changePage: ReportPageChanger = (_event, nextPage) => {
        setPage(nextPage);
    };
    return (
        <Stack>
            <ReportLinesPageTable lines={lines} page={page} />
            <ReportLinesPagination lineCount={lines.length} page={page} onPageChange={changePage} />
        </Stack>
    );
}

interface ReportDownloadButtonProps {
    option: DownloadOption;
    query: ReportQuery;
    downloads: ReportDownloads;
}

type DownloadClickHandler = (event: MouseEvent<HTMLElement>) => void;
type DownloadKeyHandler = (event: KeyboardEvent<HTMLElement>) => void;

// The KeyboardEvent.key of the space bar.
const SPACE_KEY = " ";

// A locked link, still focused from its own activation, starts nothing.
function StartUnlockedDownload(props: ReportDownloadButtonProps): void {
    if (!props.downloads.disabled) {
        void props.downloads.startDownload(props.query, props.option.format);
    }
}

// Activating a link fetches its file through the API client instead of following the address,
// so a refused or failed export redirects or shows its legacy message rather than replacing the
// screen.
function BuildDownloadClickHandler(props: ReportDownloadButtonProps): DownloadClickHandler {
    return (event) => {
        event.preventDefault();
        StartUnlockedDownload(props);
    };
}

function IsModifiedKey(event: KeyboardEvent<HTMLElement>): boolean {
    return event.ctrlKey || event.altKey || event.metaKey || event.shiftKey;
}

// Enter follows the link, which its click handler turns into the download. Space, which presses
// the button the link is styled as, starts the same download instead of scrolling the page; a
// held Space is one activation, and a chord is left to the browser.
function BuildDownloadKeyHandler(props: ReportDownloadButtonProps): DownloadKeyHandler {
    return (event) => {
        if (event.key !== SPACE_KEY || IsModifiedKey(event)) {
            return;
        }
        event.preventDefault();
        if (!event.repeat) {
            StartUnlockedDownload(props);
        }
    };
}

// The link keeps the export address, the download it stands for, as its semantics.
function ReportDownloadButton(props: ReportDownloadButtonProps) {
    return (
        <Button
            component="a"
            href={BuildReportExportUrl(props.query, props.option.format)}
            download
            disabled={props.downloads.disabled}
            onClick={BuildDownloadClickHandler(props)}
            onKeyDown={BuildDownloadKeyHandler(props)}
            variant="outlined"
            startIcon={<DownloadIcon />}
            aria-label={props.option.label}
        >
            {props.option.label}
        </Button>
    );
}

interface ReportDownloadButtonsProps {
    query: ReportQuery;
    downloads: ReportDownloads;
}

// The confirmed query again, as CSV or PDF; the session cookie travels with the client's call and
// the server names the file.
function ReportDownloadButtons({ query, downloads }: ReportDownloadButtonsProps) {
    return (
        <Stack direction="row" spacing={2} useFlexGap sx={{ flexWrap: "wrap" }}>
            {DOWNLOAD_OPTIONS.map((option) => (
                <ReportDownloadButton
                    key={option.format}
                    option={option}
                    query={query}
                    downloads={downloads}
                />
            ))}
        </Stack>
    );
}

interface ReportResultSectionProps {
    result: ReportResult | null;
    downloads: ReportDownloads;
}

// A confirmed report: its range, its table and its downloads. A range that matched no
// transaction keeps its table, with one row saying so, and its downloads give that empty report.
function ReportResultSection({ result, downloads }: ReportResultSectionProps) {
    if (result === null) {
        return null;
    }
    return (
        <Stack spacing={4} useFlexGap>
            <Typography variant="titleSmall" component="h2">
                {DATE_RANGE_PREFIX + result.startDate + DATE_RANGE_SEPARATOR + result.endDate}
            </Typography>
            <ReportLinesTable lines={result.lines} />
            <ReportDownloadButtons query={result.submittedQuery} downloads={downloads} />
        </Stack>
    );
}

// The report renders again only for a new result or when a request locks or frees its downloads,
// never for a criteria keystroke. Each ENTER read mounts a report section of its own, so no part
// of a superseded report outlives it.
function useReportResultView(form: ReportForm): ReactNode {
    const { readNumber, result, downloads } = form;
    return useMemo(
        () => <ReportResultSection key={readNumber} result={result} downloads={downloads} />,
        [readNumber, result, downloads],
    );
}

function TransactionReportsScreen() {
    const form = useReportForm();
    const resultView = useReportResultView(form);
    return (
        <ScreenShell
            transactionId={TRANSACTION_ID}
            programName={PROGRAM_NAME}
            screenTitle={SCREEN_TITLE}
            isBusy={form.isBusy}
        >
            <ReportCriteriaSection form={form} />
            <ReportMessageLine form={form} />
            {resultView}
            <FunctionKeyBar keys={form.keys} />
        </ScreenShell>
    );
}

export default function TransactionReportsPage() {
    return (
        <Suspense fallback={null}>
            <TransactionReportsScreen />
        </Suspense>
    );
}
