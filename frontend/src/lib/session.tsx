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
// with the server's message. RefreshSession reads /auth/me on every page and never navigates, so
// its caller decides what a missing user means.
//
// A page the browser restores from its back/forward cache runs no effect and sends no request, so
// the restore judges the session again exactly as the first mount does: a session that ended
// meanwhile goes to sign-on instead of leaving the restored page an empty screen.
import {
    createContext,
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
import axios from "axios";

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

interface SessionState {
    currentUser: CurrentUserResponse | null;
    isLoading: boolean;
}

// Every read but the latest is aborted: a newer read aborts the one it replaces, ClearSession
// aborts the one in flight, so a sign-off is never undone by its answer, and disposing of the
// provider aborts it too. A read may update the context only if, when it answers, its signal is
// unaborted and the provider is mounted.
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
const LOADING_SESSION_STATE: SessionState = { currentUser: null, isLoading: true };
const SIGNED_OUT_SESSION_STATE: SessionState = { currentUser: null, isLoading: false };

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

// RefreshSession's read never navigates: its caller handles a missing user, and each page's own
// calls show their legacy feedback.
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
// abandoned by a newer read, ClearSession or the provider's disposal never navigates. No read is
// retried.
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

// The provider's first read on any page but sign-on, then the check that the page lies in the
// user's own menu tree. RefreshSession's read never runs the check.
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

// Keeps the user shown. An unchanged flag returns the same state, so React skips the render.
function LoadingUpdate(isLoading: boolean): SetStateAction<SessionState> {
    return (state) => (state.isLoading === isLoading ? state : { ...state, isLoading });
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

// The user stays shown until the answer arrives. A defect ends the loading state, keeps that
// user, and still rejects to the caller. A read aborted in flight changes nothing and gives null.
// The mount read starts in the loading state, which the loading flag leaves unchanged, so its
// state changes only once the loader answers.
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
            setSessionState({ currentUser: user, isLoading: false });
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
        readTracker.InvalidateReads();
        setSessionState(SIGNED_OUT_SESSION_STATE);
        ClearHomeRoute();
    }, [setSessionState, readTracker]);
}

// A restore from the back/forward cache (pageshow with persisted set) runs no mount effect and
// sends no request, so without this a page restored after a sign-off would never ask the server
// and would stay an empty screen. The restore repeats the first mount's read; an ordinary load's
// pageshow is left to the mount effect.
function useRestoredPageRecheck(
    setSessionState: Dispatch<SetStateAction<SessionState>>,
    readTracker: SessionReadTracker,
): void {
    useEffect(() => {
        const recheckRestoredPage = (event: PageTransitionEvent): void => {
            if (event.persisted) {
                void ReadSession(setSessionState, readTracker, MountUserLoader());
            }
        };
        window.addEventListener("pageshow", recheckRestoredPage);
        return () => {
            window.removeEventListener("pageshow", recheckRestoredPage);
        };
    }, [setSessionState, readTracker]);
}

function useSessionState(): SessionContextValue {
    const [sessionState, setSessionState] = useState<SessionState>(LOADING_SESSION_STATE);
    const readTracker = useSessionReadTracker();
    const refreshSession = useRefreshSession(setSessionState, readTracker);
    const clearSession = useClearSession(setSessionState, readTracker);
    useEffect(() => {
        void ReadSession(setSessionState, readTracker, MountUserLoader());
    }, [readTracker]);
    useRestoredPageRecheck(setSessionState, readTracker);
    return useMemo(
        () => ({ ...sessionState, RefreshSession: refreshSession, ClearSession: clearSession }),
        [sessionState, refreshSession, clearSession],
    );
}

export function SessionProvider({ children }: SessionProviderProps) {
    const value = useSessionState();
    return <SESSION_CONTEXT.Provider value={value}>{children}</SESSION_CONTEXT.Provider>;
}

export function useSession(): SessionContextValue {
    const value = useContext(SESSION_CONTEXT);
    if (value === null) {
        throw new Error("useSession must be used within SessionProvider");
    }
    return value;
}
