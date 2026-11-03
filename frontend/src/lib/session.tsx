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
// user. A provider mounted on any other page reads /auth/me at once, so a 401 there still reaches
// the apiClient redirect to sign-on. RefreshSession reads /auth/me on every page.
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
import { ClearHomeRoute, IsCancelledRequest, SetHomeRoute, SIGN_ON_ROUTE } from "@/lib/apiClient";

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

// A read the tracker aborted is discarded silently. An axios error (a 401 when signed out, another
// status, or no response) leaves no known user: the apiClient interceptor already sends a 401 to
// sign-on, and each page's own calls show their legacy feedback. Any other error is a defect and
// propagates. As in the apiClient home-route lookup, the route is recorded only while the read is
// still current.
async function LoadCurrentUser(read: SessionRead): Promise<CurrentUserResponse | null> {
    try {
        const user = await FetchCurrentUser({ signal: read.signal });
        if (read.IsCurrent()) {
            SetHomeRoute(user.next_route);
        }
        return user;
    } catch (error) {
        if (IsCancelledRequest(error)) {
            return null;
        }
        if (axios.isAxiosError(error)) {
            return null;
        }
        throw error;
    }
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
    return window.location.pathname === SIGN_ON_ROUTE ? NoUserBeforeLogin : LoadCurrentUser;
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

function useSessionState(): SessionContextValue {
    const [sessionState, setSessionState] = useState<SessionState>(LOADING_SESSION_STATE);
    const readTracker = useSessionReadTracker();
    const refreshSession = useRefreshSession(setSessionState, readTracker);
    const clearSession = useClearSession(setSessionState, readTracker);
    useEffect(() => {
        void ReadSession(setSessionState, readTracker, MountUserLoader());
    }, [readTracker]);
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
