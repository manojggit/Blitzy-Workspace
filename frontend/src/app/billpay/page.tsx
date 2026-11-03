// Replaces: BMS map COBIL00, program COBIL00C (CB00); copybooks COBIL00 (cpy-bms),
//     CVACT01Y, CVACT03Y, CVTRA05Y, COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y
"use client";

import { Divider, Stack, TextField, type SxProps, type Theme } from "@mui/material";
import { isAxiosError } from "axios";
import { useRouter } from "next/navigation";
import {
    Suspense,
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
import { PayBill, type BillPayResponse } from "@/lib/api/billpay";
import {
    LegacyErrorBody,
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    type MessageSeverity,
} from "@/lib/apiClient";
import { useFunctionKeys, type FunctionKeyDefinition } from "@/lib/useFunctionKeys";

const TRANSACTION_ID = "CB00";
const PROGRAM_NAME = "COBIL00C";
const SCREEN_TITLE = "Bill Payment";
// F3 returns to CDEMO-FROM-PROGRAM, which for this main-menu option is COMEN01C.
const MENU_ROUTE = "/menu";

// The map's prompts and field LENGTHs: ACTIDIN (UNPROT, IC), CURBAL (ASKIP) and CONFIRM.
const FIELD_DEFINITIONS = {
    acct_id: { label: "Enter Acct ID:", maxLength: 11 },
    current_balance: { label: "Your current balance is:", maxLength: 14 },
    confirm: { label: "Do you want to pay your balance now. Please confirm:", maxLength: 1 },
} as const;

// Keeps the three short fields to a form-sized column; narrower viewports use the full width.
const FIELD_STACK_STYLE: SxProps<Theme> = { maxWidth: "sm" };

type BillPayInputName = "acct_id" | "confirm";

const INPUT_NAMES: readonly BillPayInputName[] = ["acct_id", "confirm"];

interface BillPayFormState {
    acctId: string;
    confirm: string;
    currentBalance: string | null;
}

interface BillPayFeedback {
    message: string;
    severity: MessageSeverity;
    infoMessage: string;
    fieldErrors: readonly string[];
    cursorField: string | null;
}

type BillPayInputElements = Record<BillPayInputName, HTMLInputElement | null>;

type BillPayInputRefs = Record<BillPayInputName, RefObject<HTMLInputElement | null>>;

// The form state each keyed input edits.
const FORM_FIELD_OF_INPUT = {
    acct_id: "acctId",
    confirm: "confirm",
} as const satisfies Record<BillPayInputName, keyof BillPayFormState>;

interface BillPayScreenSetters {
    setForm: Dispatch<SetStateAction<BillPayFormState>>;
    setFeedback: Dispatch<SetStateAction<BillPayFeedback>>;
    setIsBusy: Dispatch<SetStateAction<boolean>>;
}

interface BillPayKeyActions {
    isBusy: boolean;
    onContinue: () => void;
    onBack: () => void;
    onClear: () => void;
}

interface BillPayKeyInputs {
    form: BillPayFormState;
    isBusy: boolean;
    setters: BillPayScreenSetters;
}

interface BillPayScreenModel {
    form: BillPayFormState;
    feedback: BillPayFeedback;
    isBusy: boolean;
    inputRefs: BillPayInputRefs;
    keys: readonly FunctionKeyDefinition[];
    setForm: Dispatch<SetStateAction<BillPayFormState>>;
}

interface BillPayFieldsProps {
    screen: BillPayScreenModel;
}

interface KeyedInputProps {
    value: string;
    isInvalid: boolean;
    onValueChange: (value: string) => void;
    inputRef: RefObject<HTMLInputElement | null>;
}

interface CurrentBalanceDisplayProps {
    currentBalance: string | null;
}

const EMPTY_FORM: Readonly<BillPayFormState> = { acctId: "", confirm: "", currentBalance: null };

// INITIALIZE-ALL-FIELDS: every field and the message blank, the cursor on ACTIDIN.
const EMPTY_FEEDBACK: Readonly<BillPayFeedback> = {
    message: "",
    severity: "info",
    infoMessage: "",
    fieldErrors: [],
    cursorField: "acct_id",
};

// A fresh object each time, so a repeated F4 still moves the cursor back to ACTIDIN.
function ClearedForm(): BillPayFormState {
    return { ...EMPTY_FORM };
}

function ClearedFeedback(): BillPayFeedback {
    return { ...EMPTY_FEEDBACK };
}

function IsInputName(name: string | null): name is BillPayInputName {
    return INPUT_NAMES.some((inputName) => inputName === name);
}

// Replaces MOVE -1 TO <field>L: the input the server names takes the cursor; any other name,
// or none, leaves the focus where it is.
function FocusCursorField(inputs: BillPayInputElements, cursorField: string | null): void {
    if (IsInputName(cursorField)) {
        inputs[cursorField]?.focus();
    }
}

// The N/n answer runs CLEAR-CURRENT-SCREEN, which the server sends back with every field and
// the message blank.
function IsClearedScreen(response: BillPayResponse): boolean {
    const isMessageBlank = response.message.trim() === "";
    return isMessageBlank && response.current_balance === null && response.tran_id === null;
}

// A written payment (tran_id set) ran INITIALIZE-ALL-FIELDS, so the keyed fields are blanked;
// the balance always shows what the server sent.
function FormAfterResponse(response: BillPayResponse, form: BillPayFormState): BillPayFormState {
    if (IsClearedScreen(response)) {
        return ClearedForm();
    }
    const keyedFields = response.tran_id === null ? form : EMPTY_FORM;
    return {
        acctId: keyedFields.acctId,
        confirm: keyedFields.confirm,
        currentBalance: response.current_balance,
    };
}

// Only a written payment is a completed write, shown green as COBIL00C sets DFHGREEN.
function FeedbackAfterResponse(response: BillPayResponse): BillPayFeedback {
    if (IsClearedScreen(response)) {
        return ClearedFeedback();
    }
    const isCompletedWrite = response.tran_id !== null;
    return {
        message: response.message,
        severity: SeverityForOutcome({ isError: false, isCompletedWrite }),
        infoMessage: "",
        fieldErrors: [],
        cursorField: response.cursor_field,
    };
}

// The legacy message verbatim; a 500, a network failure or a malformed request shows the abend
// text.
function FeedbackFromError(error: unknown): BillPayFeedback {
    const feedback = ToScreenFeedback(error);
    return {
        message: feedback.message,
        severity: SeverityForOutcome({ isError: true, isCompletedWrite: false }),
        infoMessage: feedback.info_message,
        fieldErrors: feedback.field_errors,
        cursorField: feedback.cursor_field,
    };
}

// A legacy error body carries CURBAL as the server sent it. Without that key the balance shown
// is kept, as the FSET CURBAL field kept its value.
function FormUpdateFromError(error: unknown): SetStateAction<BillPayFormState> {
    const body = LegacyErrorBody<BillPayResponse>(error);
    if (body === null || !("current_balance" in body)) {
        return (form) => form;
    }
    const currentBalance = body.current_balance;
    return (form) => ({ ...form, currentBalance });
}

// Only an axios error is a legacy outcome; anything else is a defect and propagates.
function ShowFailure(error: unknown, setters: BillPayScreenSetters): void {
    if (!isAxiosError(error)) {
        throw error;
    }
    setters.setForm(FormUpdateFromError(error));
    setters.setFeedback(FeedbackFromError(error));
}

// ENTER: both inputs reach COBIL00C exactly as keyed, because the program runs its blank, Y/y,
// N/n and invalid-value edits on the raw text. The form is updated from its latest state, so
// nothing keyed while the call runs is lost.
async function SubmitBillPay(form: BillPayFormState, setters: BillPayScreenSetters) {
    setters.setIsBusy(true);
    try {
        const response = await PayBill({ acct_id: form.acctId, confirm: form.confirm });
        setters.setForm((current) => FormAfterResponse(response, current));
        setters.setFeedback(FeedbackAfterResponse(response));
    } catch (error) {
        ShowFailure(error, setters);
    } finally {
        setters.setIsBusy(false);
    }
}

// F4 (CLEAR-CURRENT-SCREEN) reads no file, so it never reaches the server.
function ClearScreen(setters: BillPayScreenSetters): void {
    setters.setForm(ClearedForm());
    setters.setFeedback(ClearedFeedback());
}

// The map's legend, in its order: ENTER=Continue  F3=Back  F4=Clear.
function BuildKeyDefinitions(actions: BillPayKeyActions): FunctionKeyDefinition[] {
    return [
        { key: "ENTER", label: "Continue", onPress: actions.onContinue, disabled: actions.isBusy },
        { key: "F3", label: "Back", onPress: actions.onBack },
        { key: "F4", label: "Clear", onPress: actions.onClear },
    ];
}

// The keyed character goes into the form unchanged: no trimming, case folding or filtering.
function BindKeyedInput(screen: BillPayScreenModel, name: BillPayInputName): KeyedInputProps {
    const formField = FORM_FIELD_OF_INPUT[name];
    return {
        value: screen.form[formField],
        isInvalid: screen.feedback.fieldErrors.includes(name),
        onValueChange: (value) => screen.setForm((current) => ({ ...current, [formField]: value })),
        inputRef: screen.inputRefs[name],
    };
}

// React recognises hooks only by the camelCase "use" prefix, so the hook names below are a
// framework-mandated exception to the MK_Modernization Rule's PascalCase function naming.

// A message carried across the navigation into this screen (StorePendingMessage) is shown once.
// The setter comes in as an argument: reading sessionStorage after mount keeps the server render
// and the hydrating render identical.
function usePendingMessage(setFeedback: Dispatch<SetStateAction<BillPayFeedback>>): void {
    useEffect(() => {
        const pendingMessage = TakePendingMessage();
        if (pendingMessage === "") {
            return;
        }
        const severity = SeverityForOutcome({ isError: true, isCompletedWrite: false });
        setFeedback((current) => ({ ...current, message: pendingMessage, severity }));
    }, [setFeedback]);
}

// Every new feedback object places the cursor, the first one on ACTIDIN (BMS IC).
function useCursorFocus(feedback: BillPayFeedback): BillPayInputRefs {
    const acctIdRef = useRef<HTMLInputElement>(null);
    const confirmRef = useRef<HTMLInputElement>(null);
    useEffect(() => {
        const inputs = { acct_id: acctIdRef.current, confirm: confirmRef.current };
        FocusCursorField(inputs, feedback.cursorField);
    }, [feedback]);
    return { acct_id: acctIdRef, confirm: confirmRef };
}

// The same definitions drive the physical keys and the on-screen buttons.
function useBillPayKeys({ form, isBusy, setters }: BillPayKeyInputs) {
    const router = useRouter();
    const keys = useMemo(
        () =>
            BuildKeyDefinitions({
                isBusy,
                onContinue: () => void SubmitBillPay(form, setters),
                onBack: () => router.push(MENU_ROUTE),
                onClear: () => ClearScreen(setters),
            }),
        [form, isBusy, setters, router],
    );
    useFunctionKeys(keys);
    return keys;
}

function useBillPayScreen(): BillPayScreenModel {
    const [form, setForm] = useState<BillPayFormState>(ClearedForm);
    const [feedback, setFeedback] = useState<BillPayFeedback>(ClearedFeedback);
    const [isBusy, setIsBusy] = useState(false);
    const setters = useMemo<BillPayScreenSetters>(() => ({ setForm, setFeedback, setIsBusy }), []);
    usePendingMessage(setFeedback);
    const inputRefs = useCursorFocus(feedback);
    const keys = useBillPayKeys({ form, isBusy, setters });
    return { form, feedback, isBusy, inputRefs, keys, setForm };
}

function AccountIdField({ value, isInvalid, onValueChange, inputRef }: KeyedInputProps) {
    const { label, maxLength } = FIELD_DEFINITIONS.acct_id;
    return (
        <TextField
            label={label}
            value={value}
            onChange={(event) => onValueChange(event.target.value)}
            error={isInvalid}
            variant="outlined"
            size="small"
            inputRef={inputRef}
            slotProps={{ htmlInput: { maxLength } }}
        />
    );
}

// CURBAL is ASKIP: shown exactly as the server sent it, read-only but still focusable.
function CurrentBalanceDisplay({ currentBalance }: CurrentBalanceDisplayProps) {
    const { label, maxLength } = FIELD_DEFINITIONS.current_balance;
    return (
        <TextField
            label={label}
            value={currentBalance ?? ""}
            variant="filled"
            size="small"
            slotProps={{ input: { readOnly: true }, htmlInput: { maxLength } }}
        />
    );
}

function PaymentConfirmField({ value, isInvalid, onValueChange, inputRef }: KeyedInputProps) {
    return (
        <ConfirmField
            label={FIELD_DEFINITIONS.confirm.label}
            value={value}
            onChange={onValueChange}
            error={isInvalid}
            inputRef={inputRef}
        />
    );
}

// The dashed row-8 separator divides the account key from the payment prompt.
function BillPayFields({ screen }: BillPayFieldsProps) {
    return (
        <Stack spacing={4} sx={FIELD_STACK_STYLE}>
            <AccountIdField {...BindKeyedInput(screen, "acct_id")} />
            <Divider />
            <CurrentBalanceDisplay currentBalance={screen.form.currentBalance} />
            <PaymentConfirmField {...BindKeyedInput(screen, "confirm")} />
        </Stack>
    );
}

function BillPayScreen() {
    const screen = useBillPayScreen();
    const { feedback } = screen;
    return (
        <ScreenShell
            transactionId={TRANSACTION_ID}
            programName={PROGRAM_NAME}
            screenTitle={SCREEN_TITLE}
            isBusy={screen.isBusy}
        >
            <BillPayFields screen={screen} />
            <MessageLine
                message={feedback.message}
                severity={feedback.severity}
                infoMessage={feedback.infoMessage}
            />
            <FunctionKeyBar keys={screen.keys} />
        </ScreenShell>
    );
}

export default function BillPayPage() {
    return (
        <Suspense fallback={null}>
            <BillPayScreen />
        </Suspense>
    );
}
