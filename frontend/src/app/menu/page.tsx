// Replaces: BMS map COMEN01, program COMEN01C (CM00); copybooks COMEN01 (cpy-bms), COMEN02Y,
// COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y, CSUSR01Y
"use client";

import {
    Box,
    Grid,
    List,
    ListItem,
    ListItemButton,
    ListItemText,
    Stack,
    TextField,
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
    useTransition,
    type Dispatch,
    type RefObject,
    type SetStateAction,
} from "react";

import { FunctionKeyBar, KeepCursorOnPointerPress } from "@/components/FunctionKeyBar";
import { ErrorMessageReference, MessageLine } from "@/components/MessageLine";
import { ScreenShell, useScreenBusy } from "@/components/ScreenShell";
import { Logout } from "@/lib/api/auth";
import { FetchMainMenu, SelectMainMenuOption, type MenuOption } from "@/lib/api/menu";
import {
    ABEND_MESSAGE,
    IsCancelledRequest,
    SeverityForOutcome,
    SIGN_ON_ROUTE,
    StorePendingMessage,
    TakePendingMessage,
    ToAllowedRoute,
    ToScreenFeedback,
    type MessageSeverity,
    type OutcomeFlags,
    type RouteAllowance,
} from "@/lib/apiClient";
import { useSession } from "@/lib/session";
import {
    INVALID_KEY_MESSAGE,
    useFunctionKeys,
    type FunctionKeyDefinition,
    type InvalidKeyAction,
} from "@/lib/useFunctionKeys";

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
const NO_OPTIONS: readonly MenuOption[] = [];
// The only pages a selection may open: the routes of the installed options in MAIN_MENU_OPTIONS
// (backend/app/services/menu/menu_options.py, COMEN02Y); option 11 has none.
const MAIN_MENU_ROUTE_ALLOWANCE: RouteAllowance = {
    pathnames: [
        "/accounts/view",
        "/accounts/update",
        "/cards",
        "/cards/view",
        "/cards/update",
        "/transactions",
        "/transactions/view",
        "/transactions/add",
        "/reports/transactions",
        "/billpay",
    ],
};

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

// The listed options, or null until the first read settles and the map paints.
type ShownOptions = readonly MenuOption[] | null;

interface MenuLoad {
    callContext: ScreenCallContext;
    SetOptions: Dispatch<SetStateAction<ShownOptions>>;
    // The refusal another screen's 403 left for this menu, or "" when there is none.
    pendingMessage: string;
    signal: AbortSignal;
}

interface MenuActionContext {
    callContext: ScreenCallContext;
    Navigate: (route: string) => void;
    ClearSession: () => void;
}

interface MenuActionState {
    actionContext: MenuActionContext;
    isNavigating: boolean;
}

interface FunctionKeyInputs {
    actionContext: MenuActionContext;
    optionText: string;
    isBusy: boolean;
}

interface MenuControls {
    keys: readonly FunctionKeyDefinition[];
    invalidKeyAction: InvalidKeyAction;
    SelectListedOption: (option: MenuOption) => void;
}

interface MainMenuState {
    options: readonly MenuOption[];
    isMapPending: boolean;
    optionText: string;
    isBusy: boolean;
    isOptionInError: boolean;
    feedback: MenuFeedback;
    inputRef: RefObject<HTMLInputElement | null>;
    keys: readonly FunctionKeyDefinition[];
    invalidKeyAction: InvalidKeyAction;
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

interface MenuOptionItemProps {
    option: MenuOption;
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

// A route that is not an installed option's page is an unexpected answer: the abend text in red,
// as a failed call shows it, with the cursor back on OPTION.
function UnexpectedRouteFeedback(): MenuFeedback {
    return {
        ...NO_FEEDBACK,
        isError: true,
        message: ABEND_MESSAGE,
        severity: SeverityForOutcome(FAILED_CALL_OUTCOME),
    };
}

// WHEN OTHER: MOVE 'Y' TO WS-ERR-FLG and CCDA-MSG-INVALID-KEY in red. No option is sent, so the
// OPTION text stays as keyed and is not in error; the cursor goes back to OPTION, the IC field.
function InvalidKeyFeedback(): MenuFeedback {
    return {
        ...NO_FEEDBACK,
        isError: true,
        message: INVALID_KEY_MESSAGE,
        severity: SeverityForOutcome(FAILED_CALL_OUTCOME),
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

// The options and the carried-over refusal arrive in one update, as one SEND-MENU-SCREEN paints
// both, so the message line is placed below the whole list when it is shown.
function ShowLoadedMenu(load: MenuLoad, options: readonly MenuOption[]): void {
    load.SetOptions(options);
    if (load.pendingMessage.trim() !== "") {
        load.callContext.ShowFeedback(PendingFeedback(load.pendingMessage));
    }
}

// The read has settled: the map paints, with no options when none arrived, and the keyboard
// unlocks.
function SettleMenuLoad(load: MenuLoad): void {
    load.SetOptions((shownOptions) => shownOptions ?? NO_OPTIONS);
    load.callContext.SetIsBusy(false);
}

// SEND-MENU-SCREEN's option list, with the carried-over refusal or, when the read fails, the
// failure's message in its place, as the screen has one ERRMSG line. Cleaning up the effect aborts
// the read, so its GET stops and nothing it answers is shown; the cancellation is discarded
// silently, and the map, busy state and message line belong to the effect's next run, if any.
async function LoadMenuOptions(load: MenuLoad): Promise<void> {
    try {
        const response = await FetchMainMenu({ signal: load.signal });
        if (!load.signal.aborted) {
            ShowLoadedMenu(load, response.options);
        }
    } catch (error) {
        if (IsCancelledRequest(error)) {
            return;
        }
        const feedback = FeedbackFromError(error);
        if (!load.signal.aborted) {
            load.callContext.ShowFeedback(feedback);
        }
    } finally {
        if (!load.signal.aborted) {
            SettleMenuLoad(load);
        }
    }
}

// A refusal carried over from another screen by apiClient's 403 handling. Taking it empties the
// store, so it is taken once and kept in a ref: a StrictMode re-run of the effect still shows it.
// The options stay null until the read settles, so the whole map paints at once.
function useMenuOptions(callContext: ScreenCallContext): ShownOptions {
    const [options, setOptions] = useState<ShownOptions>(null);
    const pendingMessageRef = useRef<string | null>(null);
    useEffect(() => {
        const controller = new AbortController();
        pendingMessageRef.current ??= TakePendingMessage();
        const pendingMessage = pendingMessageRef.current;
        callContext.SetIsBusy(true);
        const signal = controller.signal;
        void LoadMenuOptions({ callContext, SetOptions: setOptions, pendingMessage, signal });
        return () => {
            controller.abort();
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

// The XCTL to the option's program, only to a page the main menu offers; any other route stays on
// the menu with the abend text.
function FollowRoute(context: MenuActionContext, route: unknown): void {
    const allowedRoute = ToAllowedRoute(route, MAIN_MENU_ROUTE_ALLOWANCE);
    if (allowedRoute === null) {
        context.callContext.ShowFeedback(UnexpectedRouteFeedback());
        return;
    }
    context.Navigate(allowedRoute);
}

// PROCESS-ENTER-KEY: the server edits the raw option text and answers with the route replacing the
// XCTL to the option's program, or with the message the menu shows instead.
async function SubmitOption(context: MenuActionContext, optionText: string): Promise<void> {
    await RunScreenCall(context.callContext, async () => {
        const response = await SelectMainMenuOption({ option: optionText });
        if (response.route !== null) {
            FollowRoute(context, response.route);
            return;
        }
        context.callContext.ShowFeedback(StayedOnMenuFeedback(response.message));
    });
}

// PF3: XCTL to COSGN00C. The sign-on page shows the thank-you literal Logout resolves with.
async function SignOff(context: MenuActionContext): Promise<void> {
    await RunScreenCall(context.callContext, async () => {
        const response = await Logout();
        context.ClearSession();
        StorePendingMessage(response.message);
        context.Navigate(SIGN_ON_ROUTE);
    });
}

// The XCTL ends with the next screen: the route change runs as a transition, so the menu stays
// busy until the routed page has rendered and a second ENTER cannot send another call.
function useMenuActionContext(callContext: ScreenCallContext): MenuActionState {
    const router = useRouter();
    const { ClearSession } = useSession();
    const [isNavigating, startNavigation] = useTransition();
    const actionContext = useMemo(
        () => ({
            callContext,
            Navigate: (route: string) => startNavigation(() => router.push(route)),
            ClearSession,
        }),
        [callContext, router, ClearSession],
    );
    return { actionContext, isNavigating };
}

// The COMEN01 legend; no other key is bound. COMEN01C answers any other PF key in its WHEN OTHER
// branch with the invalid-key literal (InvalidKeyFeedback).
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

// Each invalid key shows a new feedback, so the cursor returns to OPTION even after it left it.
// The key is refused while the screen is busy, like ENTER and F3.
function useMenuControls(inputs: FunctionKeyInputs): MenuControls {
    const { actionContext, optionText, isBusy } = inputs;
    const keys = useMemo(
        () => BuildFunctionKeys({ actionContext, optionText, isBusy }),
        [actionContext, optionText, isBusy],
    );
    const { callContext } = actionContext;
    const invalidKeyAction = useMemo(
        () => ({ onPress: () => callContext.ShowFeedback(InvalidKeyFeedback()), disabled: isBusy }),
        [callContext, isBusy],
    );
    const selectListedOption = useCallback(
        (option: MenuOption) => void SubmitOption(actionContext, String(option.number)),
        [actionContext],
    );
    return { keys, invalidKeyAction, SelectListedOption: selectListedOption };
}

// ENTER sends the OPTION text as keyed; a click on a listed option sends that option's number. The
// screen is busy from the call until the screen it routes to has rendered.
function useMainMenu(): MainMenuState {
    const [optionText, setOptionText] = useState("");
    const { isBusy: isCalling, feedback, inputRef, callContext } = useScreenCall();
    const options = useMenuOptions(callContext);
    const { actionContext, isNavigating } = useMenuActionContext(callContext);
    const isBusy = isCalling || isNavigating;
    const controls = useMenuControls({ actionContext, optionText, isBusy });
    return {
        options: options ?? NO_OPTIONS,
        isMapPending: options === null,
        optionText,
        isBusy,
        isOptionInError: IsOptionInError(feedback),
        feedback,
        inputRef,
        ...controls,
        SetOptionText: setOptionText,
    };
}

// One listed option, the option number as secondary text. A pointer press leaves the cursor in
// the OPTION field.
function MenuOptionItem({ option, onSelect, disabled }: MenuOptionItemProps) {
    return (
        <ListItem disablePadding>
            <ListItemButton
                disabled={disabled}
                onClick={() => onSelect(option)}
                onMouseDown={KeepCursorOnPointerPress}
            >
                <ListItemText primary={option.label} secondary={String(option.number)} />
            </ListItemButton>
        </ListItem>
    );
}

// Every option the server lists, in its order.
function MenuOptionList({ options, onSelect, disabled }: MenuOptionListProps) {
    return (
        <Box component="nav" aria-label={MENU_OPTIONS_LABEL}>
            <List dense>
                {options.map((option) => (
                    <MenuOptionItem
                        key={option.number}
                        option={option}
                        onSelect={onSelect}
                        disabled={disabled}
                    />
                ))}
            </List>
        </Box>
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
// non-numeric, zero or out-of-range option is valid. It keeps the cursor but refuses keystrokes
// while a call is in flight. It mounts with the listed options, so its autoFocus brings it into
// view once the whole map shows and nothing moves it afterwards.
function OptionEntryField({ value, onChange, error, inputRef }: OptionEntryFieldProps) {
    const isBusy = useScreenBusy();
    return (
        <Grid container>
            <Grid size={{ xs: 12, md: 6 }}>
                <TextField
                    label={OPTION_PROMPT}
                    value={value}
                    onChange={(event) => onChange(event.target.value)}
                    error={error}
                    inputRef={inputRef}
                    autoFocus
                    fullWidth
                    slotProps={{ htmlInput: OptionInputProps(error), input: { readOnly: isBusy } }}
                />
            </Grid>
        </Grid>
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
    useFunctionKeys(menu.keys, menu.invalidKeyAction);
    return (
        <ScreenShell
            transactionId={TRANSACTION_ID}
            programName={PROGRAM_NAME}
            screenTitle={SCREEN_TITLE}
            isBusy={menu.isBusy}
            isMapPending={menu.isMapPending}
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
