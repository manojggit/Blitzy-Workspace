// Replaces: BMS map CORPT00, program CORPT00C (CR00); copybooks CORPT00 (cpy-bms), CVTRA05Y,
// CVTRA07Y, COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
"use client";

import {
    Fragment,
    Suspense,
    useCallback,
    useEffect,
    useMemo,
    useRef,
    useState,
    useSyncExternalStore,
    type ChangeEvent,
    type Dispatch,
    type MouseEvent,
    type ReactNode,
    type Ref,
    type RefCallback,
    type SetStateAction,
} from "react";
import { useRouter } from "next/navigation";
import {
    Button,
    Grid,
    Stack,
    Table,
    TableBody,
    TableCell,
    TableContainer,
    TableHead,
    TableRow,
    TextField,
    ToggleButton,
    ToggleButtonGroup,
    Typography,
    useMediaQuery,
    useTheme,
    type GridProps,
    type SxProps,
    type TextFieldProps,
    type Theme,
} from "@mui/material";
import DownloadIcon from "@mui/icons-material/Download";
import axios, { type AxiosError } from "axios";

import { ConfirmField } from "@/components/ConfirmField";
import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import { MessageLine } from "@/components/MessageLine";
import { ScreenShell } from "@/components/ScreenShell";
import {
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    type MessageSeverity,
} from "@/lib/apiClient";
import {
    BuildReportExportUrl,
    ExportFormat,
    FetchTransactionReport,
    ReportLineKind,
    ReportType,
    type ReportLine,
    type ReportQuery,
    type TransactionReportResponse,
} from "@/lib/api/reports";
import { useFunctionKeys, type FunctionKeyDefinition } from "@/lib/useFunctionKeys";

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
// Bounds the table so its sticky headings stay in view and the PF-key bar stays reachable below a
// long report; theme spacing units, 4 dp each.
const REPORT_TABLE_MAX_HEIGHT_UNITS = 150;
const REPORT_TABLE_STYLE: SxProps<Theme> = {
    maxHeight: (theme) => theme.spacing(REPORT_TABLE_MAX_HEIGHT_UNITS),
};

// The CVTRA07Y REPORT-ACCOUNT-TOTALS, REPORT-PAGE-TOTALS and REPORT-GRAND-TOTALS literals.
const TOTAL_LABELS: Readonly<Partial<Record<ReportLineKind, string>>> = {
    [ReportLineKind.ACCOUNT_TOTAL]: "Account Total",
    [ReportLineKind.PAGE_TOTAL]: "Page Total",
    [ReportLineKind.GRAND_TOTAL]: "Grand Total",
};
// TRAN-REPORT-CAT-CD PIC 9(04): the category code prints zero-padded to four digits.
const CATEGORY_CODE_DIGITS = 4;
// The '-' FILLER between each code and its description in TRANSACTION-DETAIL-REPORT.
const CODE_DESCRIPTION_SEPARATOR = "-";

// CVTRA07Y REPORT-NAME-HEADER: 'Date Range: ' <start> ' to ' <end>.
const DATE_RANGE_PREFIX = "Date Range: ";
const DATE_RANGE_SEPARATOR = " to ";
// The STRING suffix of the success literal (CORPT00C line 450). A confirmed range that matched no
// transaction still answers it, with no lines.
const REPORT_SUBMITTED_SUFFIX = " report submitted for printing ...";

interface DownloadOption {
    format: ExportFormat;
    label: string;
}

const DOWNLOAD_OPTIONS: readonly DownloadOption[] = [
    { format: ExportFormat.CSV, label: "Download CSV" },
    { format: ExportFormat.PDF, label: "Download PDF" },
];

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
    ariaLabel: string;
    // The BMS LENGTH of the part (SDTMM 2, SDTDD 2, SDTYYYY 4; EDTMM, EDTDD, EDTYYYY alike).
    maxLength: number;
}

const START_DATE_PARTS: readonly DatePartDefinition[] = [
    { name: "startMm", label: "MM", ariaLabel: "Start Date month", maxLength: 2 },
    { name: "startDd", label: "DD", ariaLabel: "Start Date day", maxLength: 2 },
    { name: "startYyyy", label: "YYYY", ariaLabel: "Start Date year", maxLength: 4 },
];
const END_DATE_PARTS: readonly DatePartDefinition[] = [
    { name: "endMm", label: "MM", ariaLabel: "End Date month", maxLength: 2 },
    { name: "endDd", label: "DD", ariaLabel: "End Date day", maxLength: 2 },
    { name: "endYyyy", label: "YYYY", ariaLabel: "End Date year", maxLength: 4 },
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
    fieldErrors: string[];
    cursorField: string;
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
    cursorField: "",
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

// The fields shown in error: the cursor field and every field the server flags.
function FlaggedFields(feedback: FeedbackState): readonly string[] {
    return [feedback.cursorField, ...feedback.fieldErrors];
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

// A confirmed report, including a range that matched no transaction (no lines, green literal).
function IsSubmittedReport(response: TransactionReportResponse): boolean {
    return response.lines.length > 0 || response.message.endsWith(REPORT_SUBMITTED_SUFFIX);
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
// the keyed fields are echoed and the cursor goes to CONFIRM.
function ApplyPrompt(response: TransactionReportResponse, setters: ScreenSetters): void {
    const cursorField = response.cursor_field ?? "";
    setters.setResult(null);
    setters.setFeedback({
        ...EMPTY_FEEDBACK,
        message: response.message,
        severity: ERROR_SEVERITY,
        cursorField,
    });
    setters.requestFocus(response.cursor_field);
}

function ApplyResponse(
    response: TransactionReportResponse,
    query: ReportQuery,
    setters: ScreenSetters,
): void {
    if (IsSubmittedReport(response)) {
        ApplySuccess(response, query, setters);
    } else if (IsBlank(response.message)) {
        ApplyReset(response, setters);
    } else {
        ApplyPrompt(response, setters);
    }
}

// A 400 edit literal or the 503 file-error literal: red, the keyed fields kept and the cursor on
// the field the server names. The interceptor has already redirected a 401 or 403.
function ApplyError(error: AxiosError, setters: ScreenSetters): void {
    const feedback = ToScreenFeedback(error);
    setters.setResult(null);
    setters.setFeedback({
        message: feedback.message,
        severity: ERROR_SEVERITY,
        infoMessage: feedback.info_message,
        fieldErrors: feedback.field_errors,
        cursorField: feedback.cursor_field ?? "",
    });
    setters.requestFocus(feedback.cursor_field);
}

interface FunctionKeyHandlers {
    onEnter: () => void;
    onBack: () => void;
    isBusy: boolean;
}

// ENTER=Continue  F3=Back, the map's whole legend. ENTER is inactive while a request runs, as the
// 3270 keyboard stays locked until the program answers, so one ENTER is one request.
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
    submit: SubmitAction;
}

interface ReportForm {
    inputs: ReportInputs;
    setReportType: ReportTypeSetter;
    setTextInput: TextInputSetter;
    feedback: FeedbackState;
    result: ReportResult | null;
    isBusy: boolean;
    bindRef: FieldRefBinder;
    keys: readonly FunctionKeyDefinition[];
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

// The first focus request is the report type, as MONTHLY carries IC on first display.
function useFieldFocus(): FieldFocus {
    const fieldRefs = useRef<Partial<Record<CursorField, HTMLElement | null>>>({});
    const [focusRequest, setFocusRequest] = useState<FocusRequest>(INITIAL_FOCUS_REQUEST);
    useEffect(() => {
        fieldRefs.current[focusRequest.field]?.focus();
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

// Only axios errors carry a legacy outcome; any other error is a defect and propagates.
function useReportSubmit(inputs: ReportInputs, setters: ScreenSetters): ReportSubmission {
    const [isBusy, setIsBusy] = useState(false);
    const submit = useCallback(async () => {
        const query = BuildQueryFromInputs(inputs);
        setIsBusy(true);
        try {
            ApplyResponse(await FetchTransactionReport(query), query, setters);
        } catch (error) {
            if (!axios.isAxiosError(error)) {
                throw error;
            }
            ApplyError(error, setters);
        } finally {
            setIsBusy(false);
        }
    }, [inputs, setters]);
    return { isBusy, submit };
}

// The same definitions drive the physical keys and the on-screen buttons.
function useScreenKeys(submit: SubmitAction, isBusy: boolean): readonly FunctionKeyDefinition[] {
    const router = useRouter();
    const keys = useMemo(
        () =>
            BuildFunctionKeys({
                onEnter: () => {
                    void submit();
                },
                onBack: () => router.push(MENU_ROUTE),
                isBusy,
            }),
        [submit, router, isBusy],
    );
    useFunctionKeys(keys);
    return keys;
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
    const { isBusy, submit } = useReportSubmit(inputs, setters);
    const keys = useScreenKeys(submit, isBusy);
    return { inputs, setReportType, setTextInput, feedback, result, isBusy, bindRef, keys };
}

interface ReportTypeSelectorProps {
    value: ReportType | null;
    onChange: ReportTypeSetter;
    firstButtonRef: Ref<HTMLButtonElement>;
}

type SelectorOrientation = "horizontal" | "vertical";

function useSelectorOrientation(): SelectorOrientation {
    const theme = useTheme();
    const isCompact = useMediaQuery(theme.breakpoints.down(COMPACT_LAYOUT_BREAKPOINT));
    return isCompact ? "vertical" : "horizontal";
}

// ToggleButtonGroup places its segments by position among its direct children, so each button is
// an element of the group rather than a component of its own.
function RenderReportTypeButton(
    option: ReportTypeOption,
    buttonRef: Ref<HTMLButtonElement> | undefined,
): ReactNode {
    return (
        <ToggleButton key={option.value} value={option.value} ref={buttonRef}>
            {option.label}
        </ToggleButton>
    );
}

// One exclusive choice replaces the three one-byte selectors. Pressing the chosen type again
// clears the choice, as blanking the field did, so the no-selection literal stays reachable.
function ReportTypeSelector({ value, onChange, firstButtonRef }: ReportTypeSelectorProps) {
    const orientation = useSelectorOrientation();
    const handleChange = (_event: MouseEvent<HTMLElement>, next: ReportType | null): void => {
        onChange(next);
    };
    return (
        <ToggleButtonGroup
            exclusive
            value={value}
            orientation={orientation}
            onChange={handleChange}
            aria-label={REPORT_TYPE_GROUP_LABEL}
        >
            {REPORT_TYPE_OPTIONS.map((option, position) =>
                RenderReportTypeButton(option, position === 0 ? firstButtonRef : undefined),
            )}
        </ToggleButtonGroup>
    );
}

// Only the BMS LENGTH limits the part: the keyed text is never trimmed, padded or checked here,
// so every CORPT00C date literal stays reachable.
function DatePartSlotProps(part: DatePartDefinition): TextFieldProps["slotProps"] {
    return {
        htmlInput: {
            maxLength: part.maxLength,
            inputMode: "numeric",
            "aria-label": part.ariaLabel,
        },
    };
}

interface DatePartInputProps {
    part: DatePartDefinition;
    value: string;
    onChange: TextInputSetter;
    isFlagged: boolean;
    inputRef: RefCallback<HTMLElement>;
}

function DatePartInput({ part, value, onChange, isFlagged, inputRef }: DatePartInputProps) {
    return (
        <TextField
            label={part.label}
            value={value}
            onChange={(event: ChangeEvent<HTMLInputElement>) =>
                onChange(part.name, event.target.value)
            }
            error={isFlagged}
            variant="outlined"
            size="small"
            fullWidth
            inputRef={inputRef}
            slotProps={DatePartSlotProps(part)}
        />
    );
}

interface DatePartsFieldProps {
    legend: string;
    parts: readonly DatePartDefinition[];
    inputs: ReportInputs;
    onChange: TextInputSetter;
    errorFields: readonly string[];
    bindRef: FieldRefBinder;
}

// The MM, DD and YYYY parts of one map row.
function DatePartsRow(props: DatePartsFieldProps) {
    return (
        <Grid container spacing={2}>
            {props.parts.map((part) => (
                <Grid key={part.name} size={DATE_PART_GRID_SIZE}>
                    <DatePartInput
                        part={part}
                        value={props.inputs[part.name]}
                        onChange={props.onChange}
                        isFlagged={props.errorFields.includes(part.name)}
                        inputRef={props.bindRef(part.name)}
                    />
                </Grid>
            ))}
        </Grid>
    );
}

// The legend, the date parts and the (MM/DD/YYYY) hint of one map row.
function DatePartsField(props: DatePartsFieldProps) {
    return (
        <Stack spacing={2} useFlexGap role="group" aria-label={props.legend}>
            <Typography variant="titleSmall" component="p">
                {props.legend}
            </Typography>
            <DatePartsRow {...props} />
            <Typography variant="bodySmall" component="p" color="text.secondary">
                {DATE_HINT}
            </Typography>
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
            inputRef={form.bindRef("confirm")}
        />
    );
}

interface ReportFormSectionProps {
    form: ReportForm;
}

// The report type, both date ranges and the confirm flag, in map order.
function ReportCriteriaSection({ form }: ReportFormSectionProps) {
    const errorFields = FlaggedFields(form.feedback);
    return (
        <Stack spacing={4} useFlexGap>
            <ReportTypeSelector
                value={form.inputs.reportType}
                onChange={form.setReportType}
                firstButtonRef={form.bindRef("reportType")}
            />
            <DateRangeFields form={form} errorFields={errorFields} />
            <ReportConfirmField form={form} errorFields={errorFields} />
        </Stack>
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

// REPORT-ACCOUNT-TOTALS, REPORT-PAGE-TOTALS and REPORT-GRAND-TOTALS.
function TotalRow({ line }: ReportLineRowProps) {
    return (
        <TableRow>
            <TableCell variant="footer" colSpan={TABLE_HEADINGS.length - 1}>
                {TOTAL_LABELS[line.line_kind] ?? line.text}
            </TableCell>
            <TableCell variant="footer" align="right">
                {line.amount ?? ""}
            </TableCell>
        </TableRow>
    );
}

// The page headers CBTRN03C repeats: REPORT-NAME-HEADER, WS-BLANK-LINE, TRANSACTION-HEADER-1 and
// the TRANSACTION-HEADER-2 dash rule, each shown as printed. The blank line stays a blank row
// rather than a copy of the column headings, which the next header line already prints.
function HeaderRow({ line }: ReportLineRowProps) {
    return (
        <TableRow>
            <TableCell colSpan={TABLE_HEADINGS.length}>{line.text ?? ""}</TableCell>
        </TableRow>
    );
}

const ROW_RENDERERS: Readonly<Record<ReportLineKind, (line: ReportLine) => ReactNode>> = {
    [ReportLineKind.HEADER]: (line) => <HeaderRow line={line} />,
    [ReportLineKind.DETAIL]: (line) => <DetailRow line={line} />,
    [ReportLineKind.ACCOUNT_TOTAL]: (line) => <TotalRow line={line} />,
    [ReportLineKind.PAGE_TOTAL]: (line) => <TotalRow line={line} />,
    [ReportLineKind.GRAND_TOTAL]: (line) => <TotalRow line={line} />,
};

// A kind this page does not know is shown as its print line, like a header.
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
                    <TableCell key={heading} align={heading === AMOUNT_HEADING ? "right" : "left"}>
                        {heading}
                    </TableCell>
                ))}
            </TableRow>
        </TableHead>
    );
}

// The TRANREPT lines in print order; the order never changes, so a position key is stable.
function ReportLinesTable({ lines }: ReportLinesTableProps) {
    return (
        <TableContainer sx={REPORT_TABLE_STYLE}>
            <Table stickyHeader size="small" aria-label={REPORT_TABLE_LABEL}>
                <ReportTableHead />
                <TableBody>
                    {lines.map((line, position) => (
                        <Fragment key={`${line.line_kind}-${position}`}>
                            {RenderReportLine(line)}
                        </Fragment>
                    ))}
                </TableBody>
            </Table>
        </TableContainer>
    );
}

interface ReportDownloadButtonsProps {
    query: ReportQuery;
}

// The confirmed query again, as CSV or PDF; the session cookie travels on the same-site download
// and the server names the file.
function ReportDownloadButtons({ query }: ReportDownloadButtonsProps) {
    return (
        <Stack direction="row" spacing={2} useFlexGap>
            {DOWNLOAD_OPTIONS.map((option) => (
                <Button
                    key={option.format}
                    component="a"
                    href={BuildReportExportUrl(query, option.format)}
                    download
                    variant="outlined"
                    startIcon={<DownloadIcon />}
                >
                    {option.label}
                </Button>
            ))}
        </Stack>
    );
}

interface ReportResultSectionProps {
    result: ReportResult | null;
}

// A confirmed range that matched no transaction shows its range and no table, as CBTRN03C
// prints nothing for an empty input.
function ReportResultSection({ result }: ReportResultSectionProps) {
    if (result === null) {
        return null;
    }
    return (
        <Stack spacing={4} useFlexGap>
            <Typography variant="titleSmall" component="h2">
                {DATE_RANGE_PREFIX + result.startDate + DATE_RANGE_SEPARATOR + result.endDate}
            </Typography>
            {result.lines.length > 0 ? (
                <>
                    <ReportLinesTable lines={result.lines} />
                    <ReportDownloadButtons query={result.submittedQuery} />
                </>
            ) : null}
        </Stack>
    );
}

function TransactionReportsScreen() {
    const form = useReportForm();
    return (
        <ScreenShell
            transactionId={TRANSACTION_ID}
            programName={PROGRAM_NAME}
            screenTitle={SCREEN_TITLE}
            isBusy={form.isBusy}
        >
            <ReportCriteriaSection form={form} />
            <MessageLine
                message={form.feedback.message}
                severity={form.feedback.severity}
                infoMessage={form.feedback.infoMessage}
            />
            <ReportResultSection result={form.result} />
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
