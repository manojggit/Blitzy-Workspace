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
import { ErrorMessageReference, MessageLine } from "@/components/MessageLine";
import { ScreenShell } from "@/components/ScreenShell";
import { PayBill, type BillPayResponse } from "@/lib/api/billpay";
import {
    SeverityForOutcome,
    TakePendingMessage,
    ToScreenFeedback,
    type MessageSeverity,
} from "@/lib/apiClient";
import {
    INVALID_KEY_MESSAGE,
    useFunctionKeys,
    type FunctionKeyDefinition,
} from "@/lib/useFunctionKeys";

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
// ACTIDIN carries the BMS IC attribute.
const FIRST_INPUT_NAME: BillPayInputName = "acct_id";

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

// isInFlightRef is the synchronous write lock: true from the press that sends a payment request
// until that request settles, so a key pressed before the busy render reaches the keys is refused.
interface BillPayScreenSetters {
    setForm: Dispatch<SetStateAction<BillPayFormState>>;
    setFeedback: Dispatch<SetStateAction<BillPayFeedback>>;
    setIsBusy: Dispatch<SetStateAction<boolean>>;
    isInFlightRef: RefObject<boolean>;
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
    isKeyboardLocked: boolean;
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

// Replaces MOVE -1 TO <field>L: the input the server names takes the cursor. Any other name, or
// none (the abend text), puts it on ACTIDIN, the map's IC field, because the keyboard lock took
// the focus from whatever held it while the call ran. That lock clears MUI's focused state while
// the browser keeps a disabled input focused, and focusing that input again fires no focus event,
// so it is blurred first and MUI restores its focused outline and floated label.
function FocusCursorField(inputs: BillPayInputElements, cursorField: string | null): void {
    const cursorInput = inputs[IsInputName(cursorField) ? cursorField : FIRST_INPUT_NAME];
    if (cursorInput === null) {
        return;
    }
    if (cursorInput === document.activeElement) {
        cursorInput.blur();
    }
    cursorInput.focus();
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

// ERRMSG is COLOR=RED, and only WRITE-TRANSACT-FILE NORMAL moves DFHGREEN to ERRMSGC: a written
// payment shows green, and the confirm prompt sent with HTTP 200 stays red.
function FeedbackAfterResponse(response: BillPayResponse): BillPayFeedback {
    if (IsClearedScreen(response)) {
        return ClearedFeedback();
    }
    const isCompletedWrite = response.tran_id !== null;
    return {
        message: response.message,
        severity: SeverityForOutcome({ isError: !isCompletedWrite, isCompletedWrite }),
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

// Only an axios error is a legacy outcome; anything else is a defect and propagates. The legacy
// error body holds only the four standard keys, so the form is left as sent: the FSET CURBAL
// field and the keyed fields keep what the screen showed.
function ShowFailure(error: unknown, setters: BillPayScreenSetters): void {
    if (!isAxiosError(error)) {
        throw error;
    }
    setters.setFeedback(FeedbackFromError(error));
}

// ENTER: both inputs reach COBIL00C exactly as keyed, because the program runs its blank, Y/y,
// N/n and invalid-value edits on the raw text. The keyboard stays locked until the answer is
// applied, so the answer always lands on the form that was sent. The disabled keys take effect
// only a render after the press, so the in-flight lock, taken before anything else, refuses a
// second ENTER in that gap and one press sends one payment command. The lock clears when the
// request settles, a rethrown defect included.
async function SubmitBillPay(form: BillPayFormState, setters: BillPayScreenSetters) {
    if (setters.isInFlightRef.current) {
        return;
    }
    setters.isInFlightRef.current = true;
    setters.setIsBusy(true);
    try {
        const response = await PayBill({ acct_id: form.acctId, confirm: form.confirm });
        setters.setForm(FormAfterResponse(response, form));
        setters.setFeedback(FeedbackAfterResponse(response));
    } catch (error) {
        ShowFailure(error, setters);
    } finally {
        setters.isInFlightRef.current = false;
        setters.setIsBusy(false);
    }
}

// F4 (CLEAR-CURRENT-SCREEN) reads no file, so it never reaches the server. It is refused while a
// payment request is in flight, so the answer still lands on the form that was sent.
function ClearScreen(setters: BillPayScreenSetters): void {
    if (setters.isInFlightRef.current) {
        return;
    }
    setters.setForm(ClearedForm());
    setters.setFeedback(ClearedFeedback());
}

// WHEN OTHER: MOVE 'Y' TO WS-ERR-FLG and CCDA-MSG-INVALID-KEY in red. No file is read or written,
// so the keyed fields and the balance shown stay; no field is in error, and the cursor goes to
// ACTIDIN, the map's IC field. Like F4, it is refused while a payment request is in flight.
function ShowInvalidKey(setters: BillPayScreenSetters): void {
    if (setters.isInFlightRef.current) {
        return;
    }
    const severity = SeverityForOutcome({ isError: true, isCompletedWrite: false });
    setters.setFeedback({ ...ClearedFeedback(), message: INVALID_KEY_MESSAGE, severity });
}

// The map's legend, in its order: ENTER=Continue  F3=Back  F4=Clear. ENTER and F4 are disabled
// while the call runs; disabling takes effect a render after the press, so SubmitBillPay and
// ClearScreen also refuse a press made in that gap through the in-flight lock. A Clear can never
// be undone by a late answer; F3 still leaves the screen.
function BuildKeyDefinitions(actions: BillPayKeyActions): FunctionKeyDefinition[] {
    return [
        { key: "ENTER", label: "Continue", onPress: actions.onContinue, disabled: actions.isBusy },
        { key: "F3", label: "Back", onPress: actions.onBack },
        { key: "F4", label: "Clear", onPress: actions.onClear, disabled: actions.isBusy },
    ];
}

// The keyed character goes into the form unchanged: no trimming, case folding or filtering. The
// field is locked while the call runs, as the 3270 keyboard is.
function BindKeyedInput(screen: BillPayScreenModel, name: BillPayInputName): KeyedInputProps {
    const formField = FORM_FIELD_OF_INPUT[name];
    return {
        value: screen.form[formField],
        isInvalid: screen.feedback.fieldErrors.includes(name),
        isKeyboardLocked: screen.isBusy,
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
    const invalidKeyAction = useMemo(
        () => ({ onPress: () => ShowInvalidKey(setters), disabled: isBusy }),
        [setters, isBusy],
    );
    useFunctionKeys(keys, invalidKeyAction);
    return keys;
}

function useBillPayScreen(): BillPayScreenModel {
    const [form, setForm] = useState<BillPayFormState>(ClearedForm);
    const [feedback, setFeedback] = useState<BillPayFeedback>(ClearedFeedback);
    const [isBusy, setIsBusy] = useState(false);
    // The in-flight lock travels with the one stable set of setters.
    const isInFlightRef = useRef(false);
    const setters = useMemo<BillPayScreenSetters>(
        () => ({ setForm, setFeedback, setIsBusy, isInFlightRef }),
        [],
    );
    usePendingMessage(setFeedback);
    const inputRefs = useCursorFocus(feedback);
    const keys = useBillPayKeys({ form, isBusy, setters });
    return { form, feedback, isBusy, inputRefs, keys, setForm };
}

// While the server names ACTIDIN in error, the field is described by the ERRMSG line that holds
// its literal.
function AccountIdField(props: KeyedInputProps) {
    const { value, isInvalid, isKeyboardLocked, onValueChange, inputRef } = props;
    const { label, maxLength } = FIELD_DEFINITIONS.acct_id;
    return (
        <TextField
            label={label}
            value={value}
            onChange={(event) => onValueChange(event.target.value)}
            error={isInvalid}
            disabled={isKeyboardLocked}
            variant="outlined"
            inputRef={inputRef}
            slotProps={{ htmlInput: { maxLength, ...ErrorMessageReference(isInvalid) } }}
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
            slotProps={{ input: { readOnly: true }, htmlInput: { maxLength } }}
        />
    );
}

function PaymentConfirmField(props: KeyedInputProps) {
    const { value, isInvalid, isKeyboardLocked, onValueChange, inputRef } = props;
    return (
        <ConfirmField
            label={FIELD_DEFINITIONS.confirm.label}
            value={value}
            onChange={onValueChange}
            error={isInvalid}
            disabled={isKeyboardLocked}
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
