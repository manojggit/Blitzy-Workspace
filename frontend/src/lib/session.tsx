"use client";
// Session context. Replaces the COMMAREA user ID and type (COCOM01Y CDEMO-USER-ID,
// CDEMO-USER-TYPE) with the server session read from GET /auth/me; next_route follows the
// COSGN00C admin/regular routing (type A to COADM01C, any other type to COMEN01C).
//
// The browser holds no identity of its own: the HttpOnly session cookie travels on every call,
// and the server decides next_route. This module only reads the current user for the screen
// header and the choice of menu. It stores no token, password or role, and user_type is passed
// through as data only.
//
// The sign-on page has no user before login, so a provider mounted there starts signed out
// without asking the server, and the page's RefreshSession after a successful login reads the
// user. A provider mounted on any other page reads /auth/me at once. A 401 there reaches the
// apiClient redirect to sign-on and a 403 its return to the user's own menu; any other failed call
// of that first read (no answer, a timeout, a 5xx or another status) goes to sign-on once through
// the same apiClient redirect, unless the read was abandoned first. When the first read's
// next_route names the other menu tree than the page's, the page tree's menu is read as well, so
// the server refuses it with 403 and the apiClient interceptor returns the user to their own menu
// with the server's message. RefreshSession reads /auth/me on every page and runs neither that
// sign-on fallback nor the menu-tree check. It adds no redirect beyond the shared apiClient
// interceptor, which routes an /auth/me 401 to sign-on (doing nothing on sign-on itself) and a 403
// to the user's own menu; every failed read returns no user, so the caller decides what a failure
// the interceptor leaves unrouted means.
//
// Sign-on is public; every other page's screen is protected. Ending the session (ClearSession, or
// the apiClient redirect to sign-on) and hiding the page (pagehide) tear the protected screen
// down: it is concealed under a new generation, so the DOM a back/forward-cached page keeps holds
// none of its data and none of its state is mounted again. On a protected page only a read
// answering a user shows a concealed screen again, mounted afresh. A restored page runs no effect
// and sends no request, so the restore tears down again and judges the session exactly as the
// first mount does: it shows nothing until its /auth/me read answers, and a session that ended
// meanwhile goes to sign-on. Reaching sign-on (F3 sign-off) ends a concealment instead: the
// concealed screen was discarded when it was concealed, so whatever mounts next, on sign-on or on
// a page reached from it, is a fresh mount whose own calls meet the server's session check. Off
// sign-on, a read answering another user than the one held remounts the screen too.
import {
    createContext,
    Fragment,
    useCallback,
    useContext,
    useEffect,
    useMemo,
    useRef,
    useState,
    type Dispatch,
    type ReactNode,
    type RefObject,
    type SetStateAction,
} from "react";
import { flushSync } from "react-dom";
import axios from "axios";
import { usePathname } from "next/navigation";

import { FetchCurrentUser, type CurrentUserResponse } from "@/lib/api/auth";
import { FetchAdminMenu, FetchMainMenu, type MenuResponse } from "@/lib/api/menu";
import {
    ADMIN_HOME_ROUTE,
    ClearHomeRoute,
    IsCancelledRequest,
    IsInterceptorRoutedRefusal,
    MenuTreeHomeRoute,
    RedirectToSignOn,
    SetHomeRoute,
    SIGN_ON_ROUTE,
    SubscribeToSignOnRedirect,
} from "@/lib/apiClient";

export interface SessionContextValue {
    currentUser: CurrentUserResponse | null;
    isLoading: boolean;
    RefreshSession: () => Promise<CurrentUserResponse | null>;
    ClearSession: () => void;
}

export interface SessionProviderProps {
    children: ReactNode;
}

// The protected screen's lifecycle. generation keys the screen's subtree, so a new generation
// remounts it with fresh state; isConcealed hides it until a read answers with a user or the
// browser reaches sign-on.
interface ScreenLifecycle {
    generation: number;
    isConcealed: boolean;
}

// The context's user and loading flag, and the lifecycle of the screen the provider renders.
interface SessionState {
    currentUser: CurrentUserResponse | null;
    isLoading: boolean;
    screen: ScreenLifecycle;
}

// What the provider renders: the context value, and the lifecycle its children are shown under.
interface ProvidedSession {
    value: SessionContextValue;
    screen: ScreenLifecycle;
}

// The provider's children and the lifecycle they are shown under.
interface ProtectedScreenProps {
    screen: ScreenLifecycle;
    children: ReactNode;
}

// Every read but the latest is aborted: a newer read aborts the one it replaces, and every
// teardown (ClearSession, the redirect to sign-on, hiding or restoring the page) aborts the one in
// flight, so a sign-off is never undone by its answer, and disposing of the provider aborts it
// too. A read may update the context only if, when it answers, its signal is unaborted and the
// provider is mounted.
interface SessionReadTracker {
    BeginRead: () => SessionRead;
    InvalidateReads: () => void;
}

// One session read: the signal its /auth/me call is sent with, and whether its answer may update
// the context.
interface SessionRead {
    signal: AbortSignal;
    IsCurrent: () => boolean;
}

// "pending" until the provider's first mount effect, which runs after its children's effects, and
// "disposed" once a cleanup runs with no re-mount after it (StrictMode re-mounts once).
type ProviderMountState = "pending" | "mounted" | "disposed";

// Answers one session read begun through the tracker.
type CurrentUserLoader = (read: SessionRead) => Promise<CurrentUserResponse | null>;

const SESSION_CONTEXT = createContext<SessionContextValue | null>(null);
// The server render and the first client render show the children, so hydration matches.
const INITIAL_SCREEN: ScreenLifecycle = { generation: 0, isConcealed: false };
const LOADING_SESSION_STATE: SessionState = {
    currentUser: null,
    isLoading: true,
    screen: INITIAL_SCREEN,
};

// As in the apiClient home-route lookup, the route is recorded only while the read is still
// current.
async function FetchSessionUser(read: SessionRead): Promise<CurrentUserResponse> {
    const user = await FetchCurrentUser({ signal: read.signal });
    if (read.IsCurrent()) {
        SetHomeRoute(user.next_route);
    }
    return user;
}

// A read the tracker aborted is discarded silently. An axios error (a 401 when signed out, another
// status, or no response) leaves no known user. Any other error is a defect and propagates.
function NoKnownUser(error: unknown): null {
    if (IsCancelledRequest(error) || axios.isAxiosError(error)) {
        return null;
    }
    throw error;
}

// RefreshSession's read adds no redirect beyond the shared apiClient interceptor, which routes a
// 401 to sign-on and a 403 to the user's own menu. Every failed read returns no user, so its
// caller decides what a failure the interceptor leaves unrouted means, and each page's own calls
// show their legacy feedback.
async function LoadCurrentUser(read: SessionRead): Promise<CurrentUserResponse | null> {
    try {
        return await FetchSessionUser(read);
    } catch (error) {
        return NoKnownUser(error);
    }
}

// A failure the apiClient interceptor does not already route: an axios error that is neither a
// cancellation nor a 401 or 403. That is no answer at all (a network failure or a timeout), a 5xx,
// or any other status.
function IsUnroutedReadFailure(error: unknown): boolean {
    if (!axios.isAxiosError(error) || IsCancelledRequest(error)) {
        return false;
    }
    return !IsInterceptorRoutedRefusal(error);
}

// The provider's first read on any page but sign-on, whose page cannot be used without its user.
// An unrouted failure sends the browser to sign-on only while the read is still current, so a read
// abandoned by a newer read, a teardown (ClearSession included) or the provider's disposal never
// navigates. No read is retried.
async function LoadCurrentUserOrSignOn(read: SessionRead): Promise<CurrentUserResponse | null> {
    try {
        return await FetchSessionUser(read);
    } catch (error) {
        if (IsUnroutedReadFailure(error) && read.IsCurrent()) {
            RedirectToSignOn();
        }
        return NoKnownUser(error);
    }
}

// The menu tree of a page that next_route places outside the user's own tree, or null for
// sign-on, for a page in the user's own tree, and for a tree's menu page, which reads its own menu
// on mount and so is refused or allowed by the server without a second read.
function ForeignPageTree(user: CurrentUserResponse): string | null {
    const { pathname } = window.location;
    const pageTree = MenuTreeHomeRoute(pathname);
    if (pageTree === null || pageTree === pathname || pageTree === user.next_route) {
        return null;
    }
    return pageTree;
}

// The page tree's menu read, answered by the server's role guard on that menu (RequireAdmin on
// CA00, RequireRegular on CM00). It carries the session read's signal, so a sign-off or the
// provider's disposal abandons it before its refusal can navigate.
function ReadPageTreeMenu(pageTree: string, read: SessionRead): Promise<MenuResponse> {
    const options = { signal: read.signal };
    return pageTree === ADMIN_HOME_ROUTE ? FetchAdminMenu(options) : FetchMainMenu(options);
}

// The legacy system had no path from one menu tree to the other, yet the browser decides no role:
// the server refuses the page tree's menu with 403, and the apiClient interceptor returns the user
// to their own menu with the server's message. A 200 means the server allows the page, so nothing
// changes. Only a read still current asks. An axios error, a cancellation included, has already
// been routed by the interceptor or needs nothing; any other error is a defect and propagates.
async function ConfirmPageMenuTree(user: CurrentUserResponse, read: SessionRead): Promise<void> {
    const pageTree = ForeignPageTree(user);
    if (pageTree === null || !read.IsCurrent()) {
        return;
    }
    try {
        await ReadPageTreeMenu(pageTree, read);
    } catch (error) {
        if (!IsCancelledRequest(error) && !axios.isAxiosError(error)) {
            throw error;
        }
    }
}

// The provider's first read on any page but sign-on, with its sign-on fallback, then the check
// that the page lies in the user's own menu tree. RefreshSession's read runs neither: it adds no
// redirect beyond the shared apiClient interceptor and returns no user for every failed read.
async function LoadPageUser(read: SessionRead): Promise<CurrentUserResponse | null> {
    const user = await LoadCurrentUserOrSignOn(read);
    if (user !== null) {
        await ConfirmPageMenuTree(user, read);
    }
    return user;
}

// Aborts the read it replaces. A read begun after the provider is disposed can never be current,
// so its signal is aborted before it is sent. One begun before the first mount effect, as from a
// child's own mount effect, is sent and judged when it answers.
function ReplaceRead(
    latestReadRef: RefObject<AbortController | null>,
    mountStateRef: RefObject<ProviderMountState>,
): SessionRead {
    latestReadRef.current?.abort();
    const controller = new AbortController();
    latestReadRef.current = controller;
    if (mountStateRef.current === "disposed") {
        controller.abort();
    }
    const { signal } = controller;
    return { signal, IsCurrent: () => !signal.aborted && mountStateRef.current === "mounted" };
}

// Before login there is no user to read, so the server is not asked. The read is still begun
// through the tracker, so it aborts the read it replaces and is itself replaced like any other.
async function NoUserBeforeLogin(): Promise<null> {
    return null;
}

// Runs in the mount effect, so only in the browser; the page is read from window.location, as
// the apiClient 401 handler reads it.
function MountUserLoader(): CurrentUserLoader {
    return window.location.pathname === SIGN_ON_ROUTE ? NoUserBeforeLogin : LoadPageUser;
}

// Changes the loading flag only, so the user and the screen stay as they are: RefreshSession keeps
// its user shown, and a restored page's read starts after the restore has torn both down. An
// unchanged flag returns the same state, so React skips the render.
function LoadingUpdate(isLoading: boolean): SetStateAction<SessionState> {
    return (state) => (state.isLoading === isLoading ? state : { ...state, isLoading });
}

// The page is read from window.location, as the apiClient 401 handler reads it, and always before
// a state update rather than inside its updater. Called in the browser only.
function IsSignOnPage(): boolean {
    return window.location.pathname === SIGN_ON_ROUTE;
}

function IsOtherIdentity(heldUser: CurrentUserResponse, user: CurrentUserResponse): boolean {
    return heldUser.user_id !== user.user_id || heldUser.user_type !== user.user_type;
}

// A user is the fresh validation a concealed screen waits for, so the screen shows. Off sign-on,
// a user other than the one held remounts the screen, so nothing built for the previous identity
// survives. No user leaves the screen as it is: off sign-on, a first or restore read that found
// none has already been routed away (a redirect to sign-on tears the screen down on its way). On
// sign-on the screen is never left concealed, because reaching sign-on ends any concealment, so
// the page a login navigates to after its read found none still mounts and meets the server's
// session check with its own calls. RefreshSession's caller decides what no user means.
function AnsweredScreen(
    state: SessionState,
    user: CurrentUserResponse | null,
    isPublicPage: boolean,
): ScreenLifecycle {
    if (user === null) {
        return state.screen;
    }
    const heldUser = state.currentUser;
    const isNewIdentity = !isPublicPage && heldUser !== null && IsOtherIdentity(heldUser, user);
    return { generation: state.screen.generation + (isNewIdentity ? 1 : 0), isConcealed: false };
}

function AnswerUpdate(user: CurrentUserResponse | null): SetStateAction<SessionState> {
    const isPublicPage = IsSignOnPage();
    return (state) => ({
        currentUser: user,
        isLoading: false,
        screen: AnsweredScreen(state, user, isPublicPage),
    });
}

// Drops the user. Off the public sign-on page the screen is also concealed under a new
// generation, so whatever it held is discarded and only a user a later read answers, or reaching
// sign-on (RevealedOnSignOn), shows it again, mounted afresh. Sign-on keeps its own screen, whose
// sign-off and login steps go on setting its state after ClearSession and RefreshSession.
function TeardownUpdate(isLoading: boolean): SetStateAction<SessionState> {
    const isPublicPage = IsSignOnPage();
    return (state) => ({
        currentUser: null,
        isLoading,
        screen: isPublicPage
            ? state.screen
            : { generation: state.screen.generation + 1, isConcealed: true },
    });
}

// The one teardown of every trigger. The read in flight is abandoned first, so its answer can
// neither restore the user nor show the screen again.
function TearDownScreen(
    setSessionState: Dispatch<SetStateAction<SessionState>>,
    readTracker: SessionReadTracker,
    isLoading: boolean,
): void {
    readTracker.InvalidateReads();
    setSessionState(TeardownUpdate(isLoading));
}

// Hiding or restoring a page: flushSync commits the teardown before the event handler returns,
// so the DOM the browser freezes into its back/forward cache, or shows again on restore, holds no
// protected content. The loading flag stays set until a restore's read answers.
function TearDownSynchronously(
    setSessionState: Dispatch<SetStateAction<SessionState>>,
    readTracker: SessionReadTracker,
): void {
    flushSync(() => {
        TearDownScreen(setSessionState, readTracker, true);
    });
}

// A sign-off or a redirect to sign-on ends the session, so the home route goes with it.
function EndSession(
    setSessionState: Dispatch<SetStateAction<SessionState>>,
    readTracker: SessionReadTracker,
): void {
    TearDownScreen(setSessionState, readTracker, false);
    ClearHomeRoute();
}

// Reaching the public sign-on page ends a concealment. The concealed generation's subtree was
// discarded when it was concealed, so what mounts next is a fresh mount whose own calls meet the
// server's session check. The generation stays, so what sign-on has mounted under it stays
// mounted. A screen not concealed keeps the same state, so React skips the render.
function RevealedOnSignOn(state: SessionState): SessionState {
    if (!state.screen.isConcealed) {
        return state;
    }
    return { ...state, screen: { generation: state.screen.generation, isConcealed: false } };
}

// React recognises hooks only by the camelCase "use" prefix, so the hook names below are a
// framework-mandated exception to the MK_Modernization Rule's PascalCase function naming.
function useMountStateRef(): RefObject<ProviderMountState> {
    const mountStateRef = useRef<ProviderMountState>("pending");
    useEffect(() => {
        mountStateRef.current = "mounted";
        return () => {
            mountStateRef.current = "disposed";
        };
    }, []);
    return mountStateRef;
}

function useSessionReadTracker(): SessionReadTracker {
    const mountStateRef = useMountStateRef();
    const latestReadRef = useRef<AbortController | null>(null);
    const readTracker = useMemo(
        () => ({
            BeginRead: () => ReplaceRead(latestReadRef, mountStateRef),
            InvalidateReads: () => {
                latestReadRef.current?.abort();
            },
        }),
        [mountStateRef],
    );
    // Disposing of the provider aborts the read in flight, as ClearSession does.
    useEffect(() => readTracker.InvalidateReads, [readTracker]);
    return readTracker;
}

// The user and the screen stay as they are until the answer arrives; the answer of a read still
// current then sets the user and shows or remounts the screen (AnsweredScreen). A defect ends the
// loading state, keeps that user and screen, and still rejects to the caller. A read aborted in
// flight changes nothing and gives null. The mount read starts in the loading state, which the
// loading flag leaves unchanged, so its state changes only once the loader answers.
async function ReadSession(
    setSessionState: Dispatch<SetStateAction<SessionState>>,
    readTracker: SessionReadTracker,
    loadUser: CurrentUserLoader,
): Promise<CurrentUserResponse | null> {
    const read = readTracker.BeginRead();
    setSessionState(LoadingUpdate(true));
    try {
        const user = await loadUser(read);
        if (read.IsCurrent()) {
            setSessionState(AnswerUpdate(user));
        }
        return user;
    } finally {
        if (read.IsCurrent()) {
            setSessionState(LoadingUpdate(false));
        }
    }
}

function useRefreshSession(
    setSessionState: Dispatch<SetStateAction<SessionState>>,
    readTracker: SessionReadTracker,
): () => Promise<CurrentUserResponse | null> {
    return useCallback(
        () => ReadSession(setSessionState, readTracker, LoadCurrentUser),
        [setSessionState, readTracker],
    );
}

function useClearSession(
    setSessionState: Dispatch<SetStateAction<SessionState>>,
    readTracker: SessionReadTracker,
): () => void {
    return useCallback(() => {
        EndSession(setSessionState, readTracker);
    }, [setSessionState, readTracker]);
}

// The apiClient's redirect to sign-on (a 401 from any call but login and logout, or the first
// read's unrouted failure) ends the session before the browser leaves the page, so the page left
// behind, cached or not, holds nothing of it.
function useSignOnRedirectTeardown(
    setSessionState: Dispatch<SetStateAction<SessionState>>,
    readTracker: SessionReadTracker,
): void {
    useEffect(
        () =>
            SubscribeToSignOnRedirect(() => {
                EndSession(setSessionState, readTracker);
            }),
        [setSessionState, readTracker],
    );
}

// Every pagehide, whether the page enters the back/forward cache or unloads, tears the screen
// down. Hiding a page does not end the session, so the home route stays: the 403 return to the
// user's own menu hides the page too.
function useHiddenPageTeardown(
    setSessionState: Dispatch<SetStateAction<SessionState>>,
    readTracker: SessionReadTracker,
): void {
    useEffect(() => {
        const tearDownHiddenPage = (): void => {
            TearDownSynchronously(setSessionState, readTracker);
        };
        window.addEventListener("pagehide", tearDownHiddenPage);
        return () => {
            window.removeEventListener("pagehide", tearDownHiddenPage);
        };
    }, [setSessionState, readTracker]);
}

// A restore from the back/forward cache (pageshow with persisted set) runs no mount effect and
// sends no request. It tears the screen down again, in case the teardown on pagehide never reached
// the cached page, then repeats the first mount's read: only a user it answers shows the screen,
// remounted afresh, and a session that ended meanwhile goes to sign-on. An ordinary load's
// pageshow is left to the mount effect.
function useRestoredPageRecheck(
    setSessionState: Dispatch<SetStateAction<SessionState>>,
    readTracker: SessionReadTracker,
): void {
    useEffect(() => {
        const recheckRestoredPage = (event: PageTransitionEvent): void => {
            if (event.persisted) {
                TearDownSynchronously(setSessionState, readTracker);
                void ReadSession(setSessionState, readTracker, MountUserLoader());
            }
        };
        window.addEventListener("pageshow", recheckRestoredPage);
        return () => {
            window.removeEventListener("pageshow", recheckRestoredPage);
        };
    }, [setSessionState, readTracker]);
}

// Keyed on whether the page is sign-on, so each arrival there ends a concealment once. No protected
// page reveals one: there only a read answering a user shows the screen, so a back/forward-cache
// restore still waits for its /auth/me answer.
function useSignOnReveal(setSessionState: Dispatch<SetStateAction<SessionState>>): void {
    const isSignOnPage = usePathname() === SIGN_ON_ROUTE;
    useEffect(() => {
        if (isSignOnPage) {
            setSessionState(RevealedOnSignOn);
        }
    }, [isSignOnPage, setSessionState]);
}

// The context value holds only the user, the loading flag and the two functions; the screen
// lifecycle stays inside the provider.
function useSessionContextValue(
    { currentUser, isLoading }: SessionState,
    refreshSession: SessionContextValue["RefreshSession"],
    clearSession: SessionContextValue["ClearSession"],
): SessionContextValue {
    return useMemo(
        () => ({
            currentUser,
            isLoading,
            RefreshSession: refreshSession,
            ClearSession: clearSession,
        }),
        [currentUser, isLoading, refreshSession, clearSession],
    );
}

function useSessionState(): ProvidedSession {
    const [sessionState, setSessionState] = useState<SessionState>(LOADING_SESSION_STATE);
    const readTracker = useSessionReadTracker();
    const refreshSession = useRefreshSession(setSessionState, readTracker);
    const clearSession = useClearSession(setSessionState, readTracker);
    useEffect(() => {
        void ReadSession(setSessionState, readTracker, MountUserLoader());
    }, [readTracker]);
    useSignOnRedirectTeardown(setSessionState, readTracker);
    useHiddenPageTeardown(setSessionState, readTracker);
    useRestoredPageRecheck(setSessionState, readTracker);
    useSignOnReveal(setSessionState);
    const value = useSessionContextValue(sessionState, refreshSession, clearSession);
    return { value, screen: sessionState.screen };
}

// Sign-on is public and always shows; any other page shows only while the screen is not
// concealed, which reaching sign-on ends (useSignOnReveal). The generation keys the subtree, so a
// new one remounts it with fresh state. The element is the same on every page, so a client
// navigation between sign-on and another page never remounts the app: only a new generation does.
function ProtectedScreen({ screen, children }: ProtectedScreenProps) {
    const isPublicPage = usePathname() === SIGN_ON_ROUTE;
    const isShown = isPublicPage || !screen.isConcealed;
    return <Fragment key={screen.generation}>{isShown ? children : null}</Fragment>;
}

export function SessionProvider({ children }: SessionProviderProps) {
    const { value, screen } = useSessionState();
    return (
        <SESSION_CONTEXT.Provider value={value}>
            <ProtectedScreen screen={screen}>{children}</ProtectedScreen>
        </SESSION_CONTEXT.Provider>
    );
}

export function useSession(): SessionContextValue {
    const value = useContext(SESSION_CONTEXT);
    if (value === null) {
        throw new Error("useSession must be used within SessionProvider");
    }
    return value;
}
