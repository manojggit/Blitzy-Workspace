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

import { CENTRE_ALIGNED_ROW_STYLE, CENTRED_TEXT_STYLE } from "@/app/theme";
import { ConfirmField } from "@/components/ConfirmField";
import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import { ErrorMessageReference, HelperTextIdOf, MessageLine } from "@/components/MessageLine";
import { ScreenShell, useScreenBusy } from "@/components/ScreenShell";
import {
    AddTransaction,
    TransactionAddAction,
    type TransactionAddFields,
    type TransactionAddRequest,
    type TransactionAddResponse,
} from "@/lib/api/transactions";
import {
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    type CompleteScreenFeedback,
    type MessageSeverity,
} from "@/lib/apiClient";
import {
    INVALID_KEY_MESSAGE,
    useFunctionKeys,
    type FunctionKeyDefinition,
} from "@/lib/useFunctionKeys";

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

// The two keys of VALIDATE-INPUT-KEY-FIELDS; the card the server resolved belongs to them.
const KEY_FIELD_NAMES: readonly AddFieldName[] = [ACCOUNT_FIELD.name, CARD_FIELD.name];

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

// values is what ENTER and F5 send, its card_num only ever the card as the user keyed it;
// servedCard is the masked card the server resolved, which is only ever displayed.
// isInFlightRef is the synchronous write lock: true from the press that sends a request until
// that request settles, so a key pressed before the busy render reaches the keys is refused.
interface AddScreenSetters {
    setValues: Dispatch<SetStateAction<AddFieldValues>>;
    setServedCard: Dispatch<SetStateAction<string>>;
    setFeedback: Dispatch<SetStateAction<AddFeedback>>;
    setIsBusy: Dispatch<SetStateAction<boolean>>;
    isInFlightRef: RefObject<boolean>;
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

// Replaces MOVE -1 TO <field>L: the input the server names takes the cursor. Any other name, or
// none (the abend text), puts it on ACTIDIN, the map's IC field. The keyboard lock leaves the
// inputs read-only rather than disabled, so an input that kept the cursor through the call keeps
// its focused outline and shrunk label, and focusing it again changes nothing.
function FocusCursorField(inputs: AddInputElements, cursorField: string | null): void {
    const cursorInput = inputs[IsInputName(cursorField) ? cursorField : FIRST_FIELD];
    if (cursorInput === null || cursorInput === undefined) {
        return;
    }
    cursorInput.focus();
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

// The keyed values after the confirm prompt: they adopt the screen as COTRN02C sends it back (the
// resolved account, the reformatted amount and the F5-copied values) except the card. The server
// masks every card it returns, so card_num keeps the card as keyed and the next ENTER never sends
// a masked value; the resolved card is shown masked through servedCard instead. Only the add's
// INITIALIZE-ALL-FIELDS blanks every field.
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

// The card COTRN02C moved into CARDNINI (XREF-CARD-NUM for an account key), masked by the server,
// shown until either key is edited; the add's INITIALIZE-ALL-FIELDS blanks it.
function ServedCardAfterScreen(
    current: string,
    served: TransactionAddFields | undefined,
    isAdded: boolean,
): string {
    if (isAdded) {
        return "";
    }
    if (served === undefined) {
        return current;
    }
    return served.card_num ?? "";
}

// The inputs show the keyed values, with the resolved masked card in place of the keyed card
// while there is one.
function DisplayedValues(values: AddFieldValues, servedCard: string): AddFieldValues {
    if (servedCard === "") {
        return values;
    }
    return { ...values, card_num: servedCard };
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
    setters.setServedCard((current) => ServedCardAfterScreen(current, response.fields, isAdded));
    setters.setFeedback(FeedbackAfterResponse(response, isAdded));
}

// Only an axios error is a legacy outcome; anything else is a defect and propagates. A legacy
// error body carries only message, info_message, field_errors and cursor_field, so the page keeps
// its own keyed values and any resolved card it shows.
function ShowFailure(error: unknown, setters: AddScreenSetters): void {
    if (!axios.isAxiosError(error)) {
        throw error;
    }
    setters.setFeedback(FeedbackFromError(ToScreenFeedback(error)));
}

// One helper for ENTER and F5. The request sends the keyed values, the card as keyed rather than
// the masked card shown; at the server a non-blank account, such as the one the prompt resolved,
// takes precedence over the card. Every input stays locked until the answer is applied, so the
// form it updates is the form that was sent. The disabled keys take effect only a render after
// the press, so the in-flight lock, taken before anything else, refuses a second ENTER or F5 in
// that gap: each add allocates a new TRAN-ID, so a second request would write a second record.
// The lock clears when the request settles, a rethrown defect included.
async function SubmitAdd(
    action: TransactionAddAction,
    values: AddFieldValues,
    setters: AddScreenSetters,
): Promise<void> {
    if (setters.isInFlightRef.current) {
        return;
    }
    setters.isInFlightRef.current = true;
    setters.setIsBusy(true);
    try {
        ShowResponse(await AddTransaction(BuildAddRequest(action, values)), setters);
    } catch (error) {
        ShowFailure(error, setters);
    } finally {
        setters.isInFlightRef.current = false;
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

// F4 (CLEAR-CURRENT-SCREEN) reads no file, so it never reaches the server. It is refused while a
// request is in flight, so the answer still lands on the form that was sent.
function HandleClear(setters: AddScreenSetters): void {
    if (setters.isInFlightRef.current) {
        return;
    }
    setters.setValues(EMPTY_FIELDS);
    setters.setServedCard("");
    setters.setFeedback(ClearedFeedback());
}

// WHEN OTHER: MOVE 'Y' TO WS-ERR-FLG and CCDA-MSG-INVALID-KEY in red. Nothing is written, so the
// keyed values and the resolved card stay; no field is in error, and the cursor goes to ACTIDIN,
// the map's IC field. Like F4, it is refused while a request is in flight.
function HandleInvalidKey(setters: AddScreenSetters): void {
    if (setters.isInFlightRef.current) {
        return;
    }
    setters.setFeedback({ ...ClearedFeedback(), message: INVALID_KEY_MESSAGE });
}

function HandleBack(router: AppRouter): void {
    router.push(MENU_ROUTE);
}

// The keyed text goes into the form unchanged: no trimming, case folding or filtering, so an edit
// of the shown masked card is kept as the text now in the field. The BMS LENGTH, as maxLength, is
// the only input limit. Editing either key drops the resolved card, which belonged to them.
function HandleFieldChange(setters: AddScreenSetters, name: AddFieldName, value: string): void {
    setters.setValues((current) => ({ ...current, [name]: value }));
    if (KEY_FIELD_NAMES.includes(name)) {
        setters.setServedCard("");
    }
}

// The map's legend, in its order: ENTER=Continue  F3=Back  F4=Clear  F5=Copy Last Tran. ENTER, F4
// and F5 are disabled while a call runs, as the 3270 keyboard locked. Disabling takes effect a
// render after the press, so SubmitAdd and HandleClear also refuse a press made in that gap
// through the in-flight lock: one press is one request and a late answer never refills a cleared
// screen.
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
    const invalidKeyAction = useMemo(
        () => ({ onPress: () => HandleInvalidKey(setters), disabled: isBusy }),
        [setters, isBusy],
    );
    useFunctionKeys(keys, invalidKeyAction);
    return keys;
}

// The keyed values, the resolved masked card, the message line and the busy flag, with one stable
// set of setters that also carries the in-flight lock.
function useAddScreenState() {
    const [values, setValues] = useState<AddFieldValues>(EMPTY_FIELDS);
    const [servedCard, setServedCard] = useState("");
    const [feedback, setFeedback] = useState<AddFeedback>(ClearedFeedback);
    const [isBusy, setIsBusy] = useState(false);
    const isInFlightRef = useRef(false);
    const setters = useMemo<AddScreenSetters>(
        () => ({ setValues, setServedCard, setFeedback, setIsBusy, isInFlightRef }),
        [],
    );
    return { values, servedCard, feedback, isBusy, setters };
}

// The keys send the keyed values; the inputs show them with the resolved masked card.
function useTransactionAdd(): AddScreenModel {
    const { values, servedCard, feedback, isBusy, setters } = useAddScreenState();
    const inputsRef = useRef<AddInputElements>({});
    usePendingMessage(setters.setFeedback);
    useCursorFocus(inputsRef, feedback);
    const keys = useTransactionAddKeys({ values, isBusy, setters });
    const onFieldChange = useCallback(
        (name: AddFieldName, value: string) => HandleFieldChange(setters, name, value),
        [setters],
    );
    const shownValues = useMemo(() => DisplayedValues(values, servedCard), [values, servedCard]);
    const { fieldErrors } = feedback;
    const form = useMemo<AddFieldBinding>(
        () => ({ values: shownValues, fieldErrors, inputsRef, onFieldChange }),
        [shownValues, fieldErrors, onFieldChange],
    );
    return { form, feedback, isBusy, keys };
}

// The attributes a field takes from its BMS definition alone. No native validation attribute is
// set: the server decides validity, and each accessible name stays the BMS label text.
// Autocomplete is off because each value belongs to a new transaction record.
function StaticFieldProps(definition: AddFieldDefinition): TextFieldProps {
    return {
        id: FieldIdOf(definition.name),
        name: definition.name,
        label: definition.label,
        helperText: definition.helperText,
        variant: "outlined",
        autoComplete: "off",
        fullWidth: true,
    };
}

function FieldIdOf(name: AddTextFieldName): string {
    return `transaction-add-${name}`;
}

// The BMS LENGTH and, while the server names the field in error, its tie to the ERRMSG line
// ahead of the field's format hint, so the amount and date hints stay part of the description.
function HtmlInputProps(definition: AddFieldDefinition, hasError: boolean) {
    const helperTextId = HelperTextIdOf(FieldIdOf(definition.name), definition.helperText);
    return { maxLength: definition.maxLength, ...ErrorMessageReference(hasError, helperTextId) };
}

// The input element's BMS attributes, and the ScreenShell keyboard lock: read-only, never
// disabled, so the field holding the cursor keeps it through the call.
function AddFieldSlotProps(definition: AddFieldDefinition, hasError: boolean, isBusy: boolean) {
    return { htmlInput: HtmlInputProps(definition, hasError), input: { readOnly: isBusy } };
}

// No form element: useFunctionKeys handles ENTER from any input, and a native submit would add a
// second action. While the call runs the field keeps the cursor but refuses keystrokes, as the
// 3270 keyboard locked, so the answer always applies to the form that was sent.
function AddTextField({ definition, form }: AddTextFieldProps) {
    const { name } = definition;
    const { inputsRef } = form;
    const hasError = form.fieldErrors.includes(name);
    const isBusy = useScreenBusy();
    return (
        <TextField
            {...StaticFieldProps(definition)}
            value={form.values[name]}
            onChange={(event) => form.onFieldChange(name, event.target.value)}
            error={hasError}
            inputRef={(element: HTMLInputElement | null) => {
                inputsRef.current[name] = element;
            }}
            slotProps={AddFieldSlotProps(definition, hasError, isBusy)}
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
        <Grid container spacing={FIELD_SPACING} sx={CENTRE_ALIGNED_ROW_STYLE}>
            <GridField definition={ACCOUNT_FIELD} form={form} />
            <Grid size={OR_SEPARATOR_SIZE}>
                <Typography variant="bodyMedium" component="p" sx={CENTRED_TEXT_STYLE}>
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

// ScreenShell's Stack spaces the form, the message line and the key bar. Every answer, F4 and
// refused key sets a new feedback object, and typing never does, so its identity is the outcome:
// one repeating the lines shown, such as a second F4, still keeps the cursor field, which it
// focuses again without scrolling, in view.
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
                outcomeKey={feedback}
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
