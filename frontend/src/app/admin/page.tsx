// Replaces: BMS map COADM01, program COADM01C (CA00); copybooks COADM01 (cpy-bms), COADM02Y,
// COCOM01Y, COTTL01Y, CSDAT01Y, CSMSG01Y, CSUSR01Y
"use client";

import { Box, Grid, List, ListItem, ListItemButton, ListItemText, TextField } from "@mui/material";
import axios from "axios";
import { useRouter } from "next/navigation";
import {
    Suspense,
    useCallback,
    useEffect,
    useRef,
    useState,
    useTransition,
    type RefObject,
} from "react";

import { FunctionKeyBar, KeepCursorOnPointerPress } from "@/components/FunctionKeyBar";
import { ErrorMessageReference, MessageLine } from "@/components/MessageLine";
import { ScreenShell, useScreenBusy } from "@/components/ScreenShell";
import { Logout } from "@/lib/api/auth";
import { FetchAdminMenu, SelectAdminMenuOption, type MenuOption } from "@/lib/api/menu";
import {
    ABEND_MESSAGE,
    IsCancelledRequest,
    SIGN_ON_ROUTE,
    SeverityForOutcome,
    StorePendingMessage,
    TakePendingMessage,
    ToAllowedRoute,
    ToScreenFeedback,
    type MessageSeverity,
    type RouteAllowance,
} from "@/lib/apiClient";
import { useSession } from "@/lib/session";
import { useFunctionKeys, type FunctionKeyDefinition } from "@/lib/useFunctionKeys";

const TRANSACTION_ID = "CA00";
const PROGRAM_NAME = "COADM01C";
const SCREEN_TITLE = "Admin Menu";
const OPTION_PROMPT = "Please select an option :";
const OPTION_MAX_LENGTH = 2;
// The cursor_field and field_errors name of the OPTION map field.
const OPTION_FIELD = "option";
const OPTION_LIST_LABEL = "Admin menu options";
const CONTINUE_LABEL = "Continue";
const EXIT_LABEL = "Exit";
// The only pages a selection may open: the routes of the installed options in ADMIN_MENU_OPTIONS
// (backend/app/services/menu/menu_options.py, COADM02Y); options 5 and 6 have none.
const ADMIN_MENU_ROUTE_ALLOWANCE: RouteAllowance = {
    pathnames: ["/admin/users", "/admin/users/add", "/admin/users/update", "/admin/users/delete"],
};

// ERRMSG is COLOR=RED on the map, so edit failures and carried-over refusals render as errors.
const ERROR_SEVERITY: MessageSeverity = SeverityForOutcome({
    isError: true,
    isCompletedWrite: false,
});
// COADM01C sets ERRMSGC to DFHGREEN for "This option is not installed ..." (lines 151 and 272),
// the green every page gives a confirmation.
const NOT_INSTALLED_SEVERITY: MessageSeverity = SeverityForOutcome({
    isError: false,
    isCompletedWrite: true,
});

// The ERRMSG line, and whether the server put the cursor on OPTION (MOVE -1 TO OPTIONL).
interface ScreenMessage {
    message: string;
    severity: MessageSeverity;
    hasOptionError: boolean;
}

// Every entry into COADM01C starts with MOVE SPACES TO WS-MESSAGE.
const NO_MESSAGE: ScreenMessage = { message: "", severity: ERROR_SEVERITY, hasOptionError: false };

interface MenuLoadState {
    menuOptions: MenuOption[];
    isLoading: boolean;
}

const LOADING_MENU: MenuLoadState = { menuOptions: [], isLoading: true };

interface MenuLoadResult {
    menuOptions: MenuOption[];
    loadMessage: ScreenMessage;
}

// A route replaces the XCTL to the option's program; anything else stays on the menu.
type SelectionOutcome = { route: string } | { screenMessage: ScreenMessage };

// The thank-you literal once Logout resolves, the session cookie cleared or no session left to
// end; otherwise the failure's message, shown while the session stays signed on.
type SignOffOutcome = { thankYouMessage: string } | { screenMessage: ScreenMessage };

interface BusyTracker {
    isBusy: boolean;
    TrackBusy: <T>(task: () => Promise<T>) => Promise<T>;
}

interface MenuActions {
    Navigate: (route: string) => void;
    ShowMessage: (screenMessage: ScreenMessage) => void;
    TrackBusy: BusyTracker["TrackBusy"];
    ClearSession: () => void;
}

interface MenuActionsState {
    actions: MenuActions;
    isCalling: boolean;
    isNavigating: boolean;
}

interface FunctionKeyHandlers {
    isBusy: boolean;
    onContinue: () => void;
    onExit: () => void;
}

interface MenuCommandInputs {
    optionText: string;
    isLoading: boolean;
    ShowMessage: (screenMessage: ScreenMessage) => void;
}

interface MenuCommands {
    isBusy: boolean;
    keys: FunctionKeyDefinition[];
    onSelectOption: (optionText: string) => void;
}

interface OptionEntryFieldProps {
    optionText: string;
    hasError: boolean;
    inputRef: RefObject<HTMLInputElement | null>;
    onOptionTextChange: (optionText: string) => void;
}

interface AdminMenuState extends MenuCommands {
    menuOptions: MenuOption[];
    isLoading: boolean;
    screenMessage: ScreenMessage;
    optionEntry: OptionEntryFieldProps;
}

interface AdminMenuOptionsProps {
    menuOptions: readonly MenuOption[];
    isBusy: boolean;
    onSelectOption: (optionText: string) => void;
}

interface AdminMenuBodyProps {
    adminMenu: AdminMenuState;
}

// Only an axios error is a legacy outcome; anything else is a defect and propagates.
function ErrorMessageOf(error: unknown): ScreenMessage {
    if (!axios.isAxiosError(error)) {
        throw error;
    }
    const feedback = ToScreenFeedback(error);
    const hasOptionError =
        feedback.cursor_field === OPTION_FIELD || feedback.field_errors.includes(OPTION_FIELD);
    return { message: feedback.message, severity: ERROR_SEVERITY, hasOptionError };
}

// The refusal another screen left for this menu, shown red like every ERRMSG text. A blank one
// shows nothing, as MessageLine renders no blank line.
function PendingMessageOf(pendingMessage: string): ScreenMessage {
    return { message: pendingMessage, severity: ERROR_SEVERITY, hasOptionError: false };
}

// SEND-MENU-SCREEN after BUILD-MENU-OPTIONS: every COADM02Y option with the carried-over message,
// or the failure's message in its place, as the screen has one ERRMSG line. A read aborted through
// its signal gives null and is discarded silently.
async function ReadAdminMenu(
    pendingMessage: ScreenMessage,
    signal: AbortSignal,
): Promise<MenuLoadResult | null> {
    try {
        const response = await FetchAdminMenu({ signal });
        return { menuOptions: response.options, loadMessage: pendingMessage };
    } catch (error) {
        if (IsCancelledRequest(error)) {
            return null;
        }
        return { menuOptions: [], loadMessage: ErrorMessageOf(error) };
    }
}

// The XCTL to the option's program, only to a page the admin menu offers; any other route is an
// unexpected answer and stays on the menu with the abend text in red.
function RouteOutcome(route: unknown): SelectionOutcome {
    const allowedRoute = ToAllowedRoute(route, ADMIN_MENU_ROUTE_ALLOWANCE);
    if (allowedRoute === null) {
        const screenMessage = {
            message: ABEND_MESSAGE,
            severity: ERROR_SEVERITY,
            hasOptionError: false,
        };
        return { screenMessage };
    }
    return { route: allowedRoute };
}

// PROCESS-ENTER-KEY: the option text goes to the server raw, which runs the JUSTIFY, zero-fill
// and NUMERIC edit itself.
async function SelectOption(optionText: string): Promise<SelectionOutcome> {
    try {
        const response = await SelectAdminMenuOption({ option: optionText });
        if (response.route !== null) {
            return RouteOutcome(response.route);
        }
        const screenMessage = {
            message: response.message,
            severity: NOT_INSTALLED_SEVERITY,
            hasOptionError: false,
        };
        return { screenMessage };
    } catch (error) {
        return { screenMessage: ErrorMessageOf(error) };
    }
}

// POST /auth/logout clears the HttpOnly session cookie and answers the CSMSG01Y thank-you
// literal. A failed call may never have reached the server, so its cookie can still be valid and
// the failure is shown instead of a sign-off.
async function SignOff(): Promise<SignOffOutcome> {
    try {
        const response = await Logout();
        return { thankYouMessage: response.message };
    } catch (error) {
        return { screenMessage: ErrorMessageOf(error) };
    }
}

async function HandleSelectOption(actions: MenuActions, optionText: string): Promise<void> {
    actions.ShowMessage(NO_MESSAGE);
    const outcome = await actions.TrackBusy(() => SelectOption(optionText));
    if ("route" in outcome) {
        actions.Navigate(outcome.route);
        return;
    }
    actions.ShowMessage(outcome.screenMessage);
}

// PF3: XCTL to COSGN00C, which the sign-on page replaces by showing the thank-you literal. The
// identity is cleared only after Logout confirms the sign-off; a failure stays on the menu
// with its message, signed on, so F3 can be pressed again.
async function HandleSignOff(actions: MenuActions): Promise<void> {
    actions.ShowMessage(NO_MESSAGE);
    const outcome = await actions.TrackBusy(SignOff);
    if ("screenMessage" in outcome) {
        actions.ShowMessage(outcome.screenMessage);
        return;
    }
    actions.ClearSession();
    StorePendingMessage(outcome.thankYouMessage);
    actions.Navigate(SIGN_ON_ROUTE);
}

// The map legend "ENTER=Continue  F3=Exit"; the keyboard is locked while a call is in flight.
function BuildFunctionKeys(handlers: FunctionKeyHandlers): FunctionKeyDefinition[] {
    return [
        {
            key: "ENTER",
            label: CONTINUE_LABEL,
            onPress: handlers.onContinue,
            disabled: handlers.isBusy,
        },
        { key: "F3", label: EXIT_LABEL, onPress: handlers.onExit, disabled: handlers.isBusy },
    ];
}

// React recognises hooks only by the camelCase "use" prefix, so the hook names below are a
// framework-mandated exception to the MK_Modernization Rule's PascalCase function naming.
function useBusyTracker(): BusyTracker {
    const [pendingCalls, setPendingCalls] = useState(0);
    const trackBusy = useCallback(async <T,>(task: () => Promise<T>): Promise<T> => {
        setPendingCalls((count) => count + 1);
        try {
            return await task();
        } finally {
            setPendingCalls((count) => count - 1);
        }
    }, []);
    return { isBusy: pendingCalls > 0, TrackBusy: trackBusy };
}

// The OPTION field keeps the text as keyed. OPTION carries the map's IC attribute, so every
// SEND-MENU-SCREEN ERASE puts the cursor back on it, the field error (MOVE -1 TO OPTIONL)
// included; focus also stays on the screen when a disabled Continue button drops it.
function useOptionEntry(screenMessage: ScreenMessage): OptionEntryFieldProps {
    const [optionText, setOptionText] = useState("");
    const inputRef = useRef<HTMLInputElement | null>(null);
    useEffect(() => {
        if (screenMessage !== NO_MESSAGE) {
            inputRef.current?.focus();
        }
    }, [screenMessage]);
    const hasError = screenMessage.hasOptionError;
    return { optionText, hasError, inputRef, onOptionTextChange: setOptionText };
}

// The options and the message line arrive together, as one SEND-MENU-SCREEN paints both. The
// pending message is taken once and kept in a ref, so a StrictMode re-run of the effect still
// shows it. Cleaning up the effect aborts its read, so the GET stops and its answer is never shown.
function useMenuLoad(showMessage: (screenMessage: ScreenMessage) => void): MenuLoadState {
    const [loadState, setLoadState] = useState<MenuLoadState>(LOADING_MENU);
    const pendingMessageRef = useRef<string | null>(null);
    useEffect(() => {
        const controller = new AbortController();
        pendingMessageRef.current ??= TakePendingMessage();
        const pendingMessage = PendingMessageOf(pendingMessageRef.current);
        void ReadAdminMenu(pendingMessage, controller.signal).then((loadResult) => {
            if (loadResult !== null && !controller.signal.aborted) {
                setLoadState({ menuOptions: loadResult.menuOptions, isLoading: false });
                showMessage(loadResult.loadMessage);
            }
        });
        return () => {
            controller.abort();
        };
    }, [showMessage]);
    return loadState;
}

// The XCTL ends with the next screen: the route change runs as a transition, so the menu stays
// busy until the routed page has rendered and a second ENTER cannot send another call.
function useMenuActions(showMessage: MenuActions["ShowMessage"]): MenuActionsState {
    const router = useRouter();
    const { ClearSession } = useSession();
    const { isBusy, TrackBusy } = useBusyTracker();
    const [isNavigating, startNavigation] = useTransition();
    const actions: MenuActions = {
        Navigate: (route) => startNavigation(() => router.push(route)),
        ShowMessage: showMessage,
        TrackBusy,
        ClearSession,
    };
    return { actions, isCalling: isBusy, isNavigating };
}

function useMenuCommands(inputs: MenuCommandInputs): MenuCommands {
    const { actions, isCalling, isNavigating } = useMenuActions(inputs.ShowMessage);
    const isBusy = inputs.isLoading || isCalling || isNavigating;
    const onSelectOption = (optionText: string): void => {
        void HandleSelectOption(actions, optionText);
    };
    const keys = BuildFunctionKeys({
        isBusy,
        onContinue: () => onSelectOption(inputs.optionText),
        onExit: () => void HandleSignOff(actions),
    });
    return { isBusy, keys, onSelectOption };
}

function useAdminMenu(): AdminMenuState {
    const [screenMessage, setScreenMessage] = useState<ScreenMessage>(NO_MESSAGE);
    const optionEntry = useOptionEntry(screenMessage);
    const { menuOptions, isLoading } = useMenuLoad(setScreenMessage);
    const { optionText } = optionEntry;
    const commands = useMenuCommands({ optionText, isLoading, ShowMessage: setScreenMessage });
    return { ...commands, menuOptions, isLoading, screenMessage, optionEntry };
}

// BUILD-MENU-OPTIONS (OPTN001-OPTN012): every server option, label verbatim, number beside it.
// Keyboard focus on an option shows the theme's M3 focus ring, drawn inside the row.
function AdminMenuOptions({ menuOptions, isBusy, onSelectOption }: AdminMenuOptionsProps) {
    return (
        <Box component="nav" aria-label={OPTION_LIST_LABEL}>
            <List dense>
                {menuOptions.map(({ number, label }) => (
                    <ListItem key={number} disablePadding>
                        <ListItemButton
                            disabled={isBusy}
                            onClick={() => onSelectOption(String(number))}
                            onMouseDown={KeepCursorOnPointerPress}
                        >
                            <ListItemText primary={label} secondary={String(number)} />
                        </ListItemButton>
                    </ListItem>
                ))}
            </List>
        </Box>
    );
}

// The BMS LENGTH and, while COADM01C rejects the option, the field's tie to the ERRMSG line.
function OptionInputProps(hasError: boolean) {
    return {
        maxLength: OPTION_MAX_LENGTH,
        inputMode: "numeric" as const,
        ...ErrorMessageReference(hasError),
    };
}

// The field keeps the cursor but refuses keystrokes while a call is in flight.
function OptionSlotProps(hasError: boolean, isBusy: boolean) {
    return { htmlInput: OptionInputProps(hasError), input: { readOnly: isBusy } };
}

// The OPTION field: BMS LENGTH=2 and NUM, with the initial cursor (IC). The text is sent as keyed.
// It mounts with the listed options, so its autoFocus brings it into view once the map shows.
function OptionEntryField(props: OptionEntryFieldProps) {
    const isBusy = useScreenBusy();
    return (
        <Grid container>
            <Grid size={{ xs: 12, md: 6 }}>
                <TextField
                    label={OPTION_PROMPT}
                    value={props.optionText}
                    onChange={(event) => props.onOptionTextChange(event.target.value)}
                    error={props.hasError}
                    inputRef={props.inputRef}
                    autoFocus
                    fullWidth
                    slotProps={OptionSlotProps(props.hasError, isBusy)}
                />
            </Grid>
        </Grid>
    );
}

function AdminMenuBody({ adminMenu }: AdminMenuBodyProps) {
    return (
        <>
            <AdminMenuOptions
                menuOptions={adminMenu.menuOptions}
                isBusy={adminMenu.isBusy}
                onSelectOption={adminMenu.onSelectOption}
            />
            <OptionEntryField {...adminMenu.optionEntry} />
            <MessageLine
                message={adminMenu.screenMessage.message}
                severity={adminMenu.screenMessage.severity}
            />
            <FunctionKeyBar keys={adminMenu.keys} />
        </>
    );
}

function AdminMenuScreen() {
    const adminMenu = useAdminMenu();
    useFunctionKeys(adminMenu.keys);
    return (
        <ScreenShell
            transactionId={TRANSACTION_ID}
            programName={PROGRAM_NAME}
            screenTitle={SCREEN_TITLE}
            isBusy={adminMenu.isBusy}
            isMapPending={adminMenu.isLoading}
        >
            <AdminMenuBody adminMenu={adminMenu} />
        </ScreenShell>
    );
}

export default function AdminMenuPage() {
    return (
        <Suspense fallback={null}>
            <AdminMenuScreen />
        </Suspense>
    );
}
