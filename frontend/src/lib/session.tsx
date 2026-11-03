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
import { ClearHomeRoute, SetHomeRoute, SIGN_ON_ROUTE } from "@/lib/apiClient";

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

// Only the latest read, while the provider is mounted, may update the context: a slower earlier
// read, or one overtaken by ClearSession, is ignored, so a sign-off is never undone by an answer
// still in flight.
interface SessionReadTracker {
    BeginRead: () => () => boolean;
    InvalidateReads: () => void;
}

// Answers one session read; isCurrent reports whether that read is still the latest.
type CurrentUserLoader = (isCurrent: () => boolean) => Promise<CurrentUserResponse | null>;

const SESSION_CONTEXT = createContext<SessionContextValue | null>(null);
const LOADING_SESSION_STATE: SessionState = { currentUser: null, isLoading: true };
const SIGNED_OUT_SESSION_STATE: SessionState = { currentUser: null, isLoading: false };

// An axios error (a 401 when signed out, another status, or no response) leaves no known user:
// the apiClient interceptor already sends a 401 to sign-on, and each page's own calls show their
// legacy feedback. Any other error is a defect and propagates. As in the apiClient home-route
// lookup, the route is recorded only while the read is still current.
async function LoadCurrentUser(isCurrent: () => boolean): Promise<CurrentUserResponse | null> {
    try {
        const user = await FetchCurrentUser();
        if (isCurrent()) {
            SetHomeRoute(user.next_route);
        }
        return user;
    } catch (error) {
        if (axios.isAxiosError(error)) {
            return null;
        }
        throw error;
    }
}

// Before login there is no user to read, so the server is not asked.
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
function useIsMountedRef(): RefObject<boolean> {
    const isMountedRef = useRef(false);
    useEffect(() => {
        isMountedRef.current = true;
        return () => {
            isMountedRef.current = false;
        };
    }, []);
    return isMountedRef;
}

function useSessionReadTracker(): SessionReadTracker {
    const isMountedRef = useIsMountedRef();
    const latestReadRef = useRef(0);
    return useMemo(
        () => ({
            BeginRead: () => {
                latestReadRef.current += 1;
                const readNumber = latestReadRef.current;
                return () => isMountedRef.current && latestReadRef.current === readNumber;
            },
            InvalidateReads: () => {
                latestReadRef.current += 1;
            },
        }),
        [isMountedRef],
    );
}

// The user stays shown until the answer arrives. A defect ends the loading state, keeps that
// user, and still rejects to the caller. The mount read starts in the loading state, which the
// loading flag leaves unchanged, so its state changes only once the loader answers.
async function ReadSession(
    setSessionState: Dispatch<SetStateAction<SessionState>>,
    readTracker: SessionReadTracker,
    loadUser: CurrentUserLoader,
): Promise<CurrentUserResponse | null> {
    const isCurrent = readTracker.BeginRead();
    setSessionState(LoadingUpdate(true));
    try {
        const user = await loadUser(isCurrent);
        if (isCurrent()) {
            setSessionState({ currentUser: user, isLoading: false });
        }
        return user;
    } finally {
        if (isCurrent()) {
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
