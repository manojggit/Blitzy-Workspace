// Replaces: BMS map COMEN01, program COMEN01C (CM00); copybooks COMEN01 (cpy-bms), COMEN02Y,
// COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y, CSUSR01Y
"use client";

import { List, ListItem, ListItemButton, ListItemText, Stack, TextField } from "@mui/material";
import axios from "axios";
import { useRouter } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react";

import { FunctionKeyBar } from "@/components/FunctionKeyBar";
import { ErrorMessageReference, MessageLine } from "@/components/MessageLine";
import { ScreenShell } from "@/components/ScreenShell";
import { Logout } from "@/lib/api/auth";
import { FetchMainMenu, SelectMainMenuOption, type MenuOption } from "@/lib/api/menu";
import {
    SeverityForOutcome,
    SIGN_ON_ROUTE,
    StorePendingMessage,
    TakePendingMessage,
    ToScreenFeedback,
    type MessageSeverity,
    type OutcomeFlags,
} from "@/lib/apiClient";
import { useSession } from "@/lib/session";
import { useFunctionKeys, type FunctionKeyDefinition } from "@/lib/useFunctionKeys";

const TRANSACTION_ID = "CM00";
const PROGRAM_NAME = "COMEN01C";
// The COMEN01 row-4 title.
const SCREEN_TITLE = "Main Menu";
// The COMEN01 prompt in front of the OPTION field, verbatim with its blank before the colon.
const OPTION_PROMPT = "Please select an option :";
// BMS OPTION LENGTH=2.
const OPTION_FIELD_LENGTH = 2;
// The cursor_field the server names for an OPTION edit failure (MOVE -1 TO OPTIONL).
const OPTION_FIELD_NAME = "option";
const MENU_OPTIONS_LABEL = "Main menu options";
// The COMEN01 legend: ENTER=Continue  F3=Exit.
const CONTINUE_KEY_LABEL = "Continue";
const EXIT_KEY_LABEL = "Exit";
// BMS ERRMSG is COLOR=RED: a refusal carried over from another screen shows as an error.
const PENDING_MESSAGE_SEVERITY: MessageSeverity = "error";
const FAILED_CALL_OUTCOME: OutcomeFlags = { isError: true, isCompletedWrite: false };
// The selection answer carries no colour. COMEN01C paints the not-installed literal DFHRED; the
// green coming-soon literal belongs to DUMMY entries, and no active COMEN02Y entry is one.
const STAYED_ON_MENU_OUTCOME: OutcomeFlags = { isError: true, isCompletedWrite: false };

type MenuRouter = ReturnType<typeof useRouter>;

// The ERRMSG line and the cursor of the screen; isError moves the cursor to the OPTION field.
interface MenuFeedback {
    isError: boolean;
    message: string;
    severity: MessageSeverity;
    infoMessage: string;
    fieldErrors: readonly string[];
    cursorField: string | null;
}

const NO_FEEDBACK: MenuFeedback = {
    isError: false,
    message: "",
    severity: "info",
    infoMessage: "",
    fieldErrors: [],
    cursorField: null,
};

// What one API call on this screen may change: the progress bar and the message line.
interface ScreenCallContext {
    SetIsBusy: (isBusy: boolean) => void;
    ShowFeedback: (feedback: MenuFeedback) => void;
    ClearFeedback: () => void;
}

interface ScreenCallState {
    isBusy: boolean;
    feedback: MenuFeedback;
    inputRef: RefObject<HTMLInputElement | null>;
    callContext: ScreenCallContext;
}

interface MenuFeedbackState {
    feedback: MenuFeedback;
    inputRef: RefObject<HTMLInputElement | null>;
    ShowFeedback: (feedback: MenuFeedback) => void;
    ClearFeedback: () => void;
}

interface MenuLoad {
    callContext: ScreenCallContext;
    SetOptions: (options: readonly MenuOption[]) => void;
    IsIgnored: () => boolean;
}

interface MenuActionContext {
    callContext: ScreenCallContext;
    router: MenuRouter;
    ClearSession: () => void;
}

interface FunctionKeyInputs {
    actionContext: MenuActionContext;
    optionText: string;
    isBusy: boolean;
}

interface MenuControls {
    keys: readonly FunctionKeyDefinition[];
    SelectListedOption: (option: MenuOption) => void;
}

interface MainMenuState {
    options: readonly MenuOption[];
    optionText: string;
    isBusy: boolean;
    isOptionInError: boolean;
    feedback: MenuFeedback;
    inputRef: RefObject<HTMLInputElement | null>;
    keys: readonly FunctionKeyDefinition[];
    SetOptionText: (optionText: string) => void;
    SelectListedOption: (option: MenuOption) => void;
}

interface MainMenuContentProps {
    menu: MainMenuState;
}

interface MenuOptionListProps {
    options: readonly MenuOption[];
    onSelect: (option: MenuOption) => void;
    disabled: boolean;
}

interface OptionEntryFieldProps {
    value: string;
    onChange: (value: string) => void;
    error: boolean;
    inputRef: RefObject<HTMLInputElement | null>;
}

// The legacy feedback of a failed call, verbatim. Anything but an axios error is a defect and
// propagates.
function FeedbackFromError(error: unknown): MenuFeedback {
    if (!axios.isAxiosError(error)) {
        throw error;
    }
    const screenFeedback = ToScreenFeedback(error);
    return {
        isError: true,
        message: screenFeedback.message,
        severity: SeverityForOutcome(FAILED_CALL_OUTCOME),
        infoMessage: screenFeedback.info_message,
        fieldErrors: screenFeedback.field_errors,
        cursorField: screenFeedback.cursor_field,
    };
}

function PendingFeedback(pendingMessage: string): MenuFeedback {
    return {
        ...NO_FEEDBACK,
        isError: true,
        message: pendingMessage,
        severity: PENDING_MESSAGE_SEVERITY,
    };
}

function StayedOnMenuFeedback(message: string): MenuFeedback {
    return {
        ...NO_FEEDBACK,
        isError: true,
        message,
        severity: SeverityForOutcome(STAYED_ON_MENU_OUTCOME),
    };
}

function IsOptionInError(feedback: MenuFeedback): boolean {
    return (
        feedback.cursorField === OPTION_FIELD_NAME ||
        feedback.fieldErrors.includes(OPTION_FIELD_NAME)
    );
}

// React recognises hooks only by the camelCase "use" prefix, so the hook names below are a
// framework-mandated exception to the MK_Modernization Rule's PascalCase function naming.
//
// An error moves the cursor to the OPTION field, as the BMS IC attribute and MOVE -1 TO OPTIONL
// placed it on every send of the map.
function useMenuFeedback(): MenuFeedbackState {
    const [feedback, setFeedback] = useState<MenuFeedback>(NO_FEEDBACK);
    const inputRef = useRef<HTMLInputElement>(null);
    useEffect(() => {
        if (feedback.isError) {
            inputRef.current?.focus();
        }
    }, [feedback]);
    const clearFeedback = useCallback(() => {
        setFeedback(NO_FEEDBACK);
    }, []);
    return { feedback, inputRef, ShowFeedback: setFeedback, ClearFeedback: clearFeedback };
}

function useScreenCall(): ScreenCallState {
    const [isBusy, setIsBusy] = useState(false);
    const { feedback, inputRef, ShowFeedback, ClearFeedback } = useMenuFeedback();
    const callContext = useMemo<ScreenCallContext>(
        () => ({ SetIsBusy: setIsBusy, ShowFeedback, ClearFeedback }),
        [ShowFeedback, ClearFeedback],
    );
    return { isBusy, feedback, inputRef, callContext };
}

// SEND-MENU-SCREEN's option list. An answer that arrives after the effect is cleaned up is
// dropped, so no state is set once the screen is gone.
async function LoadMenuOptions(load: MenuLoad): Promise<void> {
    try {
        const response = await FetchMainMenu();
        if (!load.IsIgnored()) {
            load.SetOptions(response.options);
        }
    } catch (error) {
        const feedback = FeedbackFromError(error);
        if (!load.IsIgnored()) {
            load.callContext.ShowFeedback(feedback);
        }
    } finally {
        if (!load.IsIgnored()) {
            load.callContext.SetIsBusy(false);
        }
    }
}

// A refusal carried over from another screen by apiClient's 403 handling. Taking it empties the
// store, so it is set only when present: a second run of the mount effect never clears it.
function ShowPendingMessage(callContext: ScreenCallContext): void {
    const pendingMessage = TakePendingMessage();
    if (pendingMessage.trim() !== "") {
        callContext.ShowFeedback(PendingFeedback(pendingMessage));
    }
}

function useMenuOptions(callContext: ScreenCallContext): readonly MenuOption[] {
    const [options, setOptions] = useState<readonly MenuOption[]>([]);
    useEffect(() => {
        let isIgnored = false;
        ShowPendingMessage(callContext);
        callContext.SetIsBusy(true);
        void LoadMenuOptions({ callContext, SetOptions: setOptions, IsIgnored: () => isIgnored });
        return () => {
            isIgnored = true;
        };
    }, [callContext]);
    return options;
}

// One call with the progress bar shown and the previous message cleared; an axios failure becomes
// the message line.
async function RunScreenCall(
    callContext: ScreenCallContext,
    call: () => Promise<void>,
): Promise<void> {
    callContext.ClearFeedback();
    callContext.SetIsBusy(true);
    try {
        await call();
    } catch (error) {
        callContext.ShowFeedback(FeedbackFromError(error));
    } finally {
        callContext.SetIsBusy(false);
    }
}

// PROCESS-ENTER-KEY: the server edits the raw option text and answers with the route replacing the
// XCTL to the option's program, or with the message the menu shows instead.
async function SubmitOption(context: MenuActionContext, optionText: string): Promise<void> {
    await RunScreenCall(context.callContext, async () => {
        const response = await SelectMainMenuOption({ option: optionText });
        if (response.route !== null) {
            context.router.push(response.route);
            return;
        }
        context.callContext.ShowFeedback(StayedOnMenuFeedback(response.message));
    });
}

// PF3: XCTL to COSGN00C. The sign-on page shows the thank-you literal the server answers with.
async function SignOff(context: MenuActionContext): Promise<void> {
    await RunScreenCall(context.callContext, async () => {
        const response = await Logout();
        context.ClearSession();
        StorePendingMessage(response.message);
        context.router.push(SIGN_ON_ROUTE);
    });
}

function useMenuActionContext(callContext: ScreenCallContext): MenuActionContext {
    const router = useRouter();
    const { ClearSession } = useSession();
    return useMemo(
        () => ({ callContext, router, ClearSession }),
        [callContext, router, ClearSession],
    );
}

// The COMEN01 legend; no other key is bound, so the WHEN OTHER invalid-key path cannot occur.
function BuildFunctionKeys(inputs: FunctionKeyInputs): FunctionKeyDefinition[] {
    const { actionContext, optionText, isBusy } = inputs;
    return [
        {
            key: "ENTER",
            label: CONTINUE_KEY_LABEL,
            onPress: () => void SubmitOption(actionContext, optionText),
            disabled: isBusy,
        },
        {
            key: "F3",
            label: EXIT_KEY_LABEL,
            onPress: () => void SignOff(actionContext),
            disabled: isBusy,
        },
    ];
}

function useMenuControls(inputs: FunctionKeyInputs): MenuControls {
    const { actionContext, optionText, isBusy } = inputs;
    const keys = useMemo(
        () => BuildFunctionKeys({ actionContext, optionText, isBusy }),
        [actionContext, optionText, isBusy],
    );
    const selectListedOption = useCallback(
        (option: MenuOption) => void SubmitOption(actionContext, String(option.number)),
        [actionContext],
    );
    return { keys, SelectListedOption: selectListedOption };
}

// ENTER sends the OPTION text as keyed; a click on a listed option sends that option's number.
function useMainMenu(): MainMenuState {
    const [optionText, setOptionText] = useState("");
    const { isBusy, feedback, inputRef, callContext } = useScreenCall();
    const options = useMenuOptions(callContext);
    const actionContext = useMenuActionContext(callContext);
    const { keys, SelectListedOption } = useMenuControls({ actionContext, optionText, isBusy });
    return {
        options,
        optionText,
        isBusy,
        isOptionInError: IsOptionInError(feedback),
        feedback,
        inputRef,
        keys,
        SetOptionText: setOptionText,
        SelectListedOption,
    };
}

// Every option the server lists, in its order, the option number as secondary text.
function MenuOptionList({ options, onSelect, disabled }: MenuOptionListProps) {
    return (
        <List dense aria-label={MENU_OPTIONS_LABEL}>
            {options.map((option) => (
                <ListItem key={option.number} disablePadding>
                    <ListItemButton disabled={disabled} onClick={() => onSelect(option)}>
                        <ListItemText primary={option.label} secondary={String(option.number)} />
                    </ListItemButton>
                </ListItem>
            ))}
        </List>
    );
}

// The BMS LENGTH and, while COMEN01C rejects the option, the field's tie to the ERRMSG line.
function OptionInputProps(hasError: boolean) {
    return {
        maxLength: OPTION_FIELD_LENGTH,
        inputMode: "numeric" as const,
        ...ErrorMessageReference(hasError),
    };
}

// The OPTION field keeps the text exactly as keyed: COMEN01C's own edit decides whether a blank,
// non-numeric, zero or out-of-range option is valid.
function OptionEntryField({ value, onChange, error, inputRef }: OptionEntryFieldProps) {
    return (
        <TextField
            label={OPTION_PROMPT}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            error={error}
            inputRef={inputRef}
            autoFocus
            slotProps={{ htmlInput: OptionInputProps(error) }}
        />
    );
}

// The OPTN001-OPTN012 lines and the OPTION field.
function MenuSelection({ menu }: MainMenuContentProps) {
    return (
        <>
            <MenuOptionList
                options={menu.options}
                onSelect={menu.SelectListedOption}
                disabled={menu.isBusy}
            />
            <OptionEntryField
                value={menu.optionText}
                onChange={menu.SetOptionText}
                error={menu.isOptionInError}
                inputRef={menu.inputRef}
            />
        </>
    );
}

function MainMenuContent({ menu }: MainMenuContentProps) {
    return (
        <Stack spacing={4}>
            <MenuSelection menu={menu} />
            <MessageLine
                message={menu.feedback.message}
                severity={menu.feedback.severity}
                infoMessage={menu.feedback.infoMessage}
            />
            <FunctionKeyBar keys={menu.keys} />
        </Stack>
    );
}

function MainMenuScreen() {
    const menu = useMainMenu();
    useFunctionKeys(menu.keys);
    return (
        <ScreenShell
            transactionId={TRANSACTION_ID}
            programName={PROGRAM_NAME}
            screenTitle={SCREEN_TITLE}
            isBusy={menu.isBusy}
        >
            <MainMenuContent menu={menu} />
        </ScreenShell>
    );
}

export default function MainMenuPage() {
    return (
        <Suspense fallback={null}>
            <MainMenuScreen />
        </Suspense>
    );
}
