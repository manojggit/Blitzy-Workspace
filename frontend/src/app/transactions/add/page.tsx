// Replaces: BMS map COTRN02, program COTRN02C (CT02); copybooks COTRN02 (cpy-bms),
//     CVTRA05Y, CVACT01Y, CVACT03Y, COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
"use client";

import {
    Divider,
    Grid,
    Stack,
    TextField,
    Typography,
    type GridSize,
    type TextFieldProps,
    type SxProps,
    type Theme,
} from "@mui/material";
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

import { ConfirmField } from "@/components/ConfirmField";
import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import { MessageLine } from "@/components/MessageLine";
import { ScreenShell } from "@/components/ScreenShell";
import {
    AddTransaction,
    TransactionAddAction,
    type TransactionAddFields,
    type TransactionAddRequest,
    type TransactionAddResponse,
} from "@/lib/api/transactions";
import {
    LegacyErrorBody,
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    type CompleteScreenFeedback,
    type MessageSeverity,
} from "@/lib/apiClient";
import { useFunctionKeys, type FunctionKeyDefinition } from "@/lib/useFunctionKeys";

const TRANSACTION_ID = "CT02";
const PROGRAM_NAME = "COTRN02C";
const SCREEN_TITLE = "Add Transaction";
// F3 returns to CDEMO-FROM-PROGRAM, which for main-menu option 8 is COMEN01C.
const MENU_ROUTE = "/menu";
// The row-21 prompt; ConfirmField adds the (Y/N) hint that follows it on the map.
const CONFIRM_LABEL = "You are about to add this transaction. Please confirm :";
// ACTIDIN carries IC, and INITIALIZE-ALL-FIELDS moves -1 to ACTIDINL.
const FIRST_FIELD = "acct_id";
const CONFIRM_FIELD = "confirm";
const OR_SEPARATOR_TEXT = "(or)";

// ERRMSG is COLOR=RED, and COTRN02C sets WS-ERR-FLG for the confirm prompt too; only a written
// transaction turns it green (DFHGREEN).
const ERROR_SEVERITY = SeverityForOutcome({ isError: true, isCompletedWrite: false });
const ADDED_SEVERITY = SeverityForOutcome({ isError: false, isCompletedWrite: true });

// The theme's 4 dp spacing unit: 4 is 16px.
const FIELD_SPACING = 4;

// A Grid size per breakpoint, as the Grid size prop takes it.
type FieldGridSize = Readonly<Record<string, GridSize>>;

// The map's rows: the two keys side by side around "(or)", three short fields per row, the
// 60-character description alone, then the merchant fields in pairs. Narrow viewports stack them.
const KEY_FIELD_SIZE: FieldGridSize = { xs: 12, md: 5 };
const OR_SEPARATOR_SIZE: FieldGridSize = { xs: 12, md: 2 };
const THIRD_ROW_SIZE: FieldGridSize = { xs: 12, md: 4 };
const HALF_ROW_SIZE: FieldGridSize = { xs: 12, md: 6 };
const FULL_ROW_SIZE: FieldGridSize = { xs: 12 };

const KEY_ROW_STYLE: SxProps<Theme> = { alignItems: "center" };
const OR_SEPARATOR_STYLE: SxProps<Theme> = { textAlign: "center" };

type AddFieldName = keyof TransactionAddFields;

type AddTextFieldName = Exclude<AddFieldName, "confirm">;

type FieldGroup = "keys" | "details" | "amounts" | "merchant";

// Every field is held as keyed, blank included, so each input stays controlled.
type AddFieldValues = Required<TransactionAddFields>;

type AddInputElements = Partial<Record<AddFieldName, HTMLInputElement | null>>;

type AppRouter = ReturnType<typeof useRouter>;

interface AddFieldDefinition {
    name: AddTextFieldName;
    label: string;
    maxLength: number;
    helperText?: string;
    group: FieldGroup;
    gridSize: FieldGridSize;
}

const ACCOUNT_FIELD: AddFieldDefinition = {
    name: "acct_id",
    label: "Enter Acct #:",
    maxLength: 11,
    group: "keys",
    gridSize: KEY_FIELD_SIZE,
};

const CARD_FIELD: AddFieldDefinition = {
    name: "card_num",
    label: "Card #:",
    maxLength: 16,
    group: "keys",
    gridSize: KEY_FIELD_SIZE,
};

// COTRN2A in map order: the INITIAL label texts, the input LENGTHs and the format hints beside
// the amount and the two dates.
const FIELD_DEFINITIONS: readonly AddFieldDefinition[] = [
    ACCOUNT_FIELD,
    CARD_FIELD,
    {
        name: "type_cd",
        label: "Type CD:",
        maxLength: 2,
        group: "details",
        gridSize: THIRD_ROW_SIZE,
    },
    {
        name: "cat_cd",
        label: "Category CD:",
        maxLength: 4,
        group: "details",
        gridSize: THIRD_ROW_SIZE,
    },
    { name: "source", label: "Source:", maxLength: 10, group: "details", gridSize: THIRD_ROW_SIZE },
    {
        name: "desc",
        label: "Description:",
        maxLength: 60,
        group: "details",
        gridSize: FULL_ROW_SIZE,
    },
    {
        name: "amount",
        label: "Amount:",
        maxLength: 12,
        helperText: "(-99999999.99)",
        group: "amounts",
        gridSize: THIRD_ROW_SIZE,
    },
    {
        name: "orig_date",
        label: "Orig Date:",
        maxLength: 10,
        helperText: "(YYYY-MM-DD)",
        group: "amounts",
        gridSize: THIRD_ROW_SIZE,
    },
    {
        name: "proc_date",
        label: "Proc Date:",
        maxLength: 10,
        helperText: "(YYYY-MM-DD)",
        group: "amounts",
        gridSize: THIRD_ROW_SIZE,
    },
    {
        name: "merchant_id",
        label: "Merchant ID:",
        maxLength: 9,
        group: "merchant",
        gridSize: HALF_ROW_SIZE,
    },
    {
        name: "merchant_name",
        label: "Merchant Name:",
        maxLength: 30,
        group: "merchant",
        gridSize: HALF_ROW_SIZE,
    },
    {
        name: "merchant_city",
        label: "Merchant City:",
        maxLength: 25,
        group: "merchant",
        gridSize: HALF_ROW_SIZE,
    },
    {
        name: "merchant_zip",
        label: "Merchant Zip:",
        maxLength: 10,
        group: "merchant",
        gridSize: HALF_ROW_SIZE,
    },
];

const DETAIL_GROUPS: readonly FieldGroup[] = ["details", "amounts"];
const MERCHANT_GROUPS: readonly FieldGroup[] = ["merchant"];

const INPUT_NAMES: readonly AddFieldName[] = [
    ...FIELD_DEFINITIONS.map((definition) => definition.name),
    CONFIRM_FIELD,
];

// INITIALIZE-ALL-FIELDS: every input blank, CONFIRMI included. The keys stay the snake_case JSON
// keys of backend/app/schemas/transactions.py, the AAP's exception to the MK_Modernization Rule's
// camelCase naming.
const EMPTY_FIELDS: Readonly<AddFieldValues> = {
    acct_id: "",
    card_num: "",
    type_cd: "",
    cat_cd: "",
    source: "",
    desc: "",
    amount: "",
    orig_date: "",
    proc_date: "",
    merchant_id: "",
    merchant_name: "",
    merchant_city: "",
    merchant_zip: "",
    confirm: "",
};

interface AddFeedback {
    message: string;
    severity: MessageSeverity;
    infoMessage: string;
    fieldErrors: readonly string[];
    cursorField: string | null;
}

interface AddScreenSetters {
    setValues: Dispatch<SetStateAction<AddFieldValues>>;
    setFeedback: Dispatch<SetStateAction<AddFeedback>>;
    setIsBusy: Dispatch<SetStateAction<boolean>>;
}

interface AddKeyInputs {
    values: AddFieldValues;
    isBusy: boolean;
    setters: AddScreenSetters;
}

interface AddKeyActions {
    isBusy: boolean;
    onContinue: () => void;
    onBack: () => void;
    onClear: () => void;
    onCopyLast: () => void;
}

// What every input of the form reads and writes.
interface AddFieldBinding {
    values: AddFieldValues;
    fieldErrors: readonly string[];
    inputsRef: RefObject<AddInputElements>;
    onFieldChange: (name: AddFieldName, value: string) => void;
}

interface AddScreenModel {
    form: AddFieldBinding;
    feedback: AddFeedback;
    isBusy: boolean;
    keys: readonly FunctionKeyDefinition[];
}

interface FieldGroupProps {
    form: AddFieldBinding;
}

interface FieldGridProps {
    groups: readonly FieldGroup[];
    form: AddFieldBinding;
}

interface AddTextFieldProps {
    definition: AddFieldDefinition;
    form: AddFieldBinding;
}

// A fresh object each time, so a repeated F4 still moves the cursor back to ACTIDIN.
function ClearedFeedback(): AddFeedback {
    return {
        message: "",
        severity: ERROR_SEVERITY,
        infoMessage: "",
        fieldErrors: [],
        cursorField: FIRST_FIELD,
    };
}

function IsInputName(name: string | null): name is AddFieldName {
    return INPUT_NAMES.some((inputName) => inputName === name);
}

// Replaces MOVE -1 TO <field>L: the input the server names takes the cursor; any other name, or
// none, leaves the focus where it is.
function FocusCursorField(inputs: AddInputElements, cursorField: string | null): void {
    if (IsInputName(cursorField)) {
        inputs[cursorField]?.focus();
    }
}

// The flat COTRN02 body: the key pressed beside the 14 fields exactly as keyed, because every
// COTRN02C edit (numeric keys, the -99999999.99 amount, YYYY-MM-DD dates, Y/N) runs on the raw
// text at the server.
function BuildAddRequest(
    action: TransactionAddAction,
    values: AddFieldValues,
): TransactionAddRequest {
    return { action, ...values };
}

// Only a written transaction carries its new TRAN-ID.
function IsAddedResponse(response: TransactionAddResponse): boolean {
    return (response.tran_id ?? "") !== "";
}

// The screen as COTRN02C sends it back: the resolved account, the reformatted amount and the
// F5-copied values. The server masks a 16-digit card number in every response, so the card number
// keyed here stays in its field and the next ENTER never sends a masked value; only the add's
// INITIALIZE-ALL-FIELDS blanks it, together with every other field.
function ValuesAfterScreen(
    current: AddFieldValues,
    served: TransactionAddFields | undefined,
    isAdded: boolean,
): AddFieldValues {
    if (served === undefined) {
        return current;
    }
    if (isAdded) {
        return { ...EMPTY_FIELDS, ...served };
    }
    return { ...current, ...served, card_num: current.card_num };
}

// A 200 is the confirm prompt (red, as WS-ERR-FLG is set) or the green added literal.
function FeedbackAfterResponse(response: TransactionAddResponse, isAdded: boolean): AddFeedback {
    return {
        message: response.message,
        severity: isAdded ? ADDED_SEVERITY : ERROR_SEVERITY,
        infoMessage: "",
        fieldErrors: [],
        cursorField: response.cursor_field ?? null,
    };
}

// The legacy literal verbatim; a 500, a network failure or a malformed request shows the abend
// text.
function FeedbackFromError(feedback: CompleteScreenFeedback): AddFeedback {
    return {
        message: feedback.message,
        severity: ERROR_SEVERITY,
        infoMessage: feedback.info_message,
        fieldErrors: feedback.field_errors,
        cursorField: feedback.cursor_field,
    };
}

function ShowResponse(response: TransactionAddResponse, setters: AddScreenSetters): void {
    const isAdded = IsAddedResponse(response);
    setters.setValues((current) => ValuesAfterScreen(current, response.fields, isAdded));
    setters.setFeedback(FeedbackAfterResponse(response, isAdded));
}

// Only an axios error is a legacy outcome; anything else is a defect and propagates. A legacy
// body re-sends the screen in its fields key; without one the keyed values stay shown.
function ShowFailure(error: unknown, setters: AddScreenSetters): void {
    if (!axios.isAxiosError(error)) {
        throw error;
    }
    const body = LegacyErrorBody<TransactionAddResponse>(error);
    if (body !== null) {
        setters.setValues((current) => ValuesAfterScreen(current, body.fields, false));
    }
    setters.setFeedback(FeedbackFromError(ToScreenFeedback(error)));
}

// One helper for ENTER and F5. The form is updated from its latest state, so the card number
// keyed while the call runs is kept.
async function SubmitAdd(
    action: TransactionAddAction,
    values: AddFieldValues,
    setters: AddScreenSetters,
): Promise<void> {
    setters.setIsBusy(true);
    try {
        ShowResponse(await AddTransaction(BuildAddRequest(action, values)), setters);
    } catch (error) {
        ShowFailure(error, setters);
    } finally {
        setters.setIsBusy(false);
    }
}

// ENTER: PROCESS-ENTER-KEY, the key and data edits, then the CONFIRMI evaluation.
function HandleEnter(values: AddFieldValues, setters: AddScreenSetters): void {
    void SubmitAdd(TransactionAddAction.ENTER, values, setters);
}

// F5: COPY-LAST-TRAN-DATA validates the keyed account or card, copies the last transaction's data
// fields, then runs the ENTER flow with the confirm value keyed here.
function HandleCopyLast(values: AddFieldValues, setters: AddScreenSetters): void {
    void SubmitAdd(TransactionAddAction.COPY_LAST, values, setters);
}

// F4 (CLEAR-CURRENT-SCREEN) reads no file, so it never reaches the server.
function HandleClear(setters: AddScreenSetters): void {
    setters.setValues(EMPTY_FIELDS);
    setters.setFeedback(ClearedFeedback());
}

function HandleBack(router: AppRouter): void {
    router.push(MENU_ROUTE);
}

// The keyed text goes into the form unchanged: no trimming, case folding or filtering. The BMS
// LENGTH, as maxLength, is the only input limit.
function HandleFieldChange(
    setValues: Dispatch<SetStateAction<AddFieldValues>>,
    name: AddFieldName,
    value: string,
): void {
    setValues((current) => ({ ...current, [name]: value }));
}

// The map's legend, in its order: ENTER=Continue  F3=Back  F4=Clear  F5=Copy Last Tran. ENTER, F4
// and F5 wait while a call runs, as the 3270 keyboard locked, so one press is one request and a
// late answer never refills a cleared screen.
function BuildKeyDefinitions(actions: AddKeyActions): FunctionKeyDefinition[] {
    return [
        { key: "ENTER", label: "Continue", onPress: actions.onContinue, disabled: actions.isBusy },
        { key: "F3", label: "Back", onPress: actions.onBack },
        { key: "F4", label: "Clear", onPress: actions.onClear, disabled: actions.isBusy },
        {
            key: "F5",
            label: "Copy Last Tran.",
            onPress: actions.onCopyLast,
            disabled: actions.isBusy,
        },
    ];
}

// React recognises hooks only by the camelCase "use" prefix, so the hook names below are a
// framework-mandated exception to the MK_Modernization Rule's PascalCase function naming.

// A message carried across the navigation into this screen (StorePendingMessage) is shown once,
// red as ERRMSG is. Reading sessionStorage after mount keeps the server render and the hydrating
// render identical.
function usePendingMessage(setFeedback: Dispatch<SetStateAction<AddFeedback>>): void {
    useEffect(() => {
        const pendingMessage = TakePendingMessage();
        if (pendingMessage === "") {
            return;
        }
        setFeedback((current) => ({
            ...current,
            message: pendingMessage,
            severity: ERROR_SEVERITY,
        }));
    }, [setFeedback]);
}

// Every new feedback object places the cursor, the first one on ACTIDIN (BMS IC), so a repeated
// failure naming the same field moves the cursor back to it.
function useCursorFocus(inputsRef: RefObject<AddInputElements>, feedback: AddFeedback): void {
    useEffect(() => {
        FocusCursorField(inputsRef.current, feedback.cursorField);
    }, [inputsRef, feedback]);
}

// The same definitions drive the physical keys and the on-screen buttons.
function useTransactionAddKeys({ values, isBusy, setters }: AddKeyInputs) {
    const router = useRouter();
    const keys = useMemo(
        () =>
            BuildKeyDefinitions({
                isBusy,
                onContinue: () => HandleEnter(values, setters),
                onBack: () => HandleBack(router),
                onClear: () => HandleClear(setters),
                onCopyLast: () => HandleCopyLast(values, setters),
            }),
        [values, isBusy, setters, router],
    );
    useFunctionKeys(keys);
    return keys;
}

function useTransactionAdd(): AddScreenModel {
    const [values, setValues] = useState<AddFieldValues>(EMPTY_FIELDS);
    const [feedback, setFeedback] = useState<AddFeedback>(ClearedFeedback);
    const [isBusy, setIsBusy] = useState(false);
    const setters = useMemo<AddScreenSetters>(() => ({ setValues, setFeedback, setIsBusy }), []);
    const inputsRef = useRef<AddInputElements>({});
    usePendingMessage(setFeedback);
    useCursorFocus(inputsRef, feedback);
    const keys = useTransactionAddKeys({ values, isBusy, setters });
    const onFieldChange = useCallback((name: AddFieldName, value: string) => {
        HandleFieldChange(setValues, name, value);
    }, []);
    const form = useMemo<AddFieldBinding>(
        () => ({ values, fieldErrors: feedback.fieldErrors, inputsRef, onFieldChange }),
        [values, feedback.fieldErrors, onFieldChange],
    );
    return { form, feedback, isBusy, keys };
}

// The attributes a field takes from its BMS definition alone. No native validation attribute is
// set: the server decides validity, and each accessible name stays the BMS label text.
// Autocomplete is off because each value belongs to a new transaction record.
function StaticFieldProps(definition: AddFieldDefinition): TextFieldProps {
    return {
        id: `transaction-add-${definition.name}`,
        name: definition.name,
        label: definition.label,
        helperText: definition.helperText,
        variant: "outlined",
        size: "small",
        autoComplete: "off",
        fullWidth: true,
        slotProps: { htmlInput: { maxLength: definition.maxLength } },
    };
}

// No form element: useFunctionKeys handles ENTER from any input, and a native submit would add a
// second action.
function AddTextField({ definition, form }: AddTextFieldProps) {
    const { name } = definition;
    const { inputsRef } = form;
    return (
        <TextField
            {...StaticFieldProps(definition)}
            value={form.values[name]}
            onChange={(event) => form.onFieldChange(name, event.target.value)}
            error={form.fieldErrors.includes(name)}
            inputRef={(element: HTMLInputElement | null) => {
                inputsRef.current[name] = element;
            }}
        />
    );
}

function GridField({ definition, form }: AddTextFieldProps) {
    return (
        <Grid size={definition.gridSize}>
            <AddTextField definition={definition} form={form} />
        </Grid>
    );
}

// Row 6: ACTIDIN, the "(or)" text and CARDNIN; either key identifies the account.
function KeyFields({ form }: FieldGroupProps) {
    return (
        <Grid container spacing={FIELD_SPACING} sx={KEY_ROW_STYLE}>
            <GridField definition={ACCOUNT_FIELD} form={form} />
            <Grid size={OR_SEPARATOR_SIZE}>
                <Typography variant="bodyMedium" component="p" sx={OR_SEPARATOR_STYLE}>
                    {OR_SEPARATOR_TEXT}
                </Typography>
            </Grid>
            <GridField definition={CARD_FIELD} form={form} />
        </Grid>
    );
}

function FieldGrid({ groups, form }: FieldGridProps) {
    const definitions = FIELD_DEFINITIONS.filter((definition) => groups.includes(definition.group));
    return (
        <Grid container spacing={FIELD_SPACING}>
            {definitions.map((definition) => (
                <GridField key={definition.name} definition={definition} form={form} />
            ))}
        </Grid>
    );
}

// Rows 10-15: type, category, source, description, then the amount and the two dates.
function DetailFields({ form }: FieldGroupProps) {
    return <FieldGrid groups={DETAIL_GROUPS} form={form} />;
}

// Rows 16-18: the merchant ID, name, city and ZIP.
function MerchantFields({ form }: FieldGroupProps) {
    return <FieldGrid groups={MERCHANT_GROUPS} form={form} />;
}

function TransactionConfirmField({ form }: FieldGroupProps) {
    const { inputsRef } = form;
    return (
        <ConfirmField
            label={CONFIRM_LABEL}
            value={form.values.confirm}
            onChange={(value) => form.onFieldChange(CONFIRM_FIELD, value)}
            error={form.fieldErrors.includes(CONFIRM_FIELD)}
            inputRef={(element: HTMLInputElement | null) => {
                inputsRef.current[CONFIRM_FIELD] = element;
            }}
        />
    );
}

// The dashed row-8 separator divides the two keys from the transaction details.
function TransactionAddFields({ form }: FieldGroupProps) {
    return (
        <Stack spacing={FIELD_SPACING}>
            <KeyFields form={form} />
            <Divider />
            <DetailFields form={form} />
            <MerchantFields form={form} />
            <TransactionConfirmField form={form} />
        </Stack>
    );
}

// ScreenShell's Stack spaces the form, the message line and the key bar.
function TransactionAddScreen() {
    const { form, feedback, isBusy, keys } = useTransactionAdd();
    return (
        <ScreenShell
            transactionId={TRANSACTION_ID}
            programName={PROGRAM_NAME}
            screenTitle={SCREEN_TITLE}
            isBusy={isBusy}
        >
            <TransactionAddFields form={form} />
            <MessageLine
                message={feedback.message}
                severity={feedback.severity}
                infoMessage={feedback.infoMessage}
            />
            <FunctionKeyBar keys={keys} />
        </ScreenShell>
    );
}

export default function TransactionAddPage() {
    return (
        <Suspense fallback={null}>
            <TransactionAddScreen />
        </Suspense>
    );
}
