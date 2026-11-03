// Tests: lib/session.tsx (replaces COCOM01Y CDEMO-USER-ID / CDEMO-USER-TYPE, which COSGN00C
// filled from USRSEC before its XCTL to COADM01C for type A or to COMEN01C otherwise).
import { Stack, Typography } from "@mui/material";
import { act, render, screen, waitFor, type RenderResult } from "@testing-library/react";
import { AxiosError, AxiosHeaders, type InternalAxiosRequestConfig } from "axios";
import { useEffect, type ReactElement } from "react";

import { FetchCurrentUser, type CurrentUserResponse } from "@/lib/api/auth";
import type * as ApiClientModule from "@/lib/apiClient";
import { ClearHomeRoute, HOME_ROUTE_KEY, SetHomeRoute, SIGN_ON_ROUTE } from "@/lib/apiClient";
import { SessionProvider, useSession, type SessionContextValue } from "@/lib/session";

jest.mock("@/lib/api/auth");

// The real home-route bookkeeping still runs; the wrappers only record the calls session.tsx makes.
jest.mock("@/lib/apiClient", () => {
    const actual = jest.requireActual<typeof ApiClientModule>("@/lib/apiClient");
    return {
        ...actual,
        SetHomeRoute: jest.fn(actual.SetHomeRoute),
        ClearHomeRoute: jest.fn(actual.ClearHomeRoute),
    };
});

const fetchCurrentUserMock = jest.mocked(FetchCurrentUser);
const setHomeRouteMock = jest.mocked(SetHomeRoute);
const clearHomeRouteMock = jest.mocked(ClearHomeRoute);

const MAIN_MENU_ROUTE = "/menu";
const ADMIN_MENU_ROUTE = "/admin";
const HTTP_UNAUTHORIZED = 401;

// Eight characters, the CDEMO-USER-ID PIC X(08) width.
const TEST_USER_ID = "TESTUSR1";
const TEST_ADMIN_ID = "TESTADM1";

const REGULAR_USER: CurrentUserResponse = {
    user_id: TEST_USER_ID,
    user_type: "U",
    first_name: "TEST",
    last_name: "USER",
    next_route: MAIN_MENU_ROUTE,
};

const ADMIN_USER: CurrentUserResponse = {
    user_id: TEST_ADMIN_ID,
    user_type: "A",
    first_name: "TEST",
    last_name: "ADMIN",
    next_route: ADMIN_MENU_ROUTE,
};

const SESSION_CONTEXT_KEYS = ["ClearSession", "RefreshSession", "currentUser", "isLoading"];
const TAMPERED_USER_TYPE_KEY = "user_type";

const LOADING_TEST_ID = "session-loading";
const USER_ID_TEST_ID = "session-user-id";
const USER_TYPE_TEST_ID = "session-user-type";
const NO_USER_SHOWN = "none";

interface SessionConsumerProps {
    onValue?: (value: SessionContextValue) => void;
}

interface MountReadCase {
    path: string;
    reads: number;
    expectedUser: CurrentUserResponse | null;
}

// The page the provider mounts on, with /auth/me answering REGULAR_USER. COSGN00C reads USRSEC
// only once ENTER is pressed, so the sign-on page has no user to read before login; every other
// page reads /auth/me at once.
const MOUNT_READ_CASES: readonly MountReadCase[] = [
    { path: SIGN_ON_ROUTE, reads: 0, expectedUser: null },
    { path: MAIN_MENU_ROUTE, reads: 1, expectedUser: REGULAR_USER },
];

// jsdom's starting URL, restored after a test moves to another page.
const jsdomStartUrl = window.location.href;

let capturedSession: SessionContextValue | null = null;

function BuildUnauthorizedError(): AxiosError {
    const config: InternalAxiosRequestConfig = { url: "/auth/me", headers: new AxiosHeaders() };
    const response = {
        status: HTTP_UNAUTHORIZED,
        data: { message: "", info_message: "", field_errors: [], cursor_field: null },
        statusText: "",
        headers: {},
        config,
    };
    return new AxiosError(
        "Request failed",
        AxiosError.ERR_BAD_REQUEST,
        config,
        undefined,
        response,
    );
}

function SessionConsumer({ onValue }: SessionConsumerProps): ReactElement {
    const session = useSession();
    useEffect(() => {
        onValue?.(session);
    }, [onValue, session]);
    return (
        <Stack>
            <Typography data-testid={LOADING_TEST_ID}>{String(session.isLoading)}</Typography>
            <Typography data-testid={USER_ID_TEST_ID}>
                {session.currentUser?.user_id ?? NO_USER_SHOWN}
            </Typography>
            <Typography data-testid={USER_TYPE_TEST_ID}>
                {session.currentUser?.user_type ?? ""}
            </Typography>
        </Stack>
    );
}

function RenderSessionConsumer(onValue?: (value: SessionContextValue) => void): RenderResult {
    return render(
        <SessionProvider>
            <SessionConsumer onValue={onValue} />
        </SessionProvider>,
    );
}

function CaptureSession(value: SessionContextValue): void {
    capturedSession = value;
}

function LatestSession(): SessionContextValue {
    if (capturedSession === null) {
        throw new Error("SessionConsumer has not rendered a session value");
    }
    return capturedSession;
}

function ShownValue(testId: string): string | null {
    return screen.getByTestId(testId).textContent;
}

async function WaitForSessionLoaded(): Promise<void> {
    await waitFor(() => {
        expect(ShownValue(LOADING_TEST_ID)).toBe("false");
    });
}

// The resolver of a /auth/me read that stays pending until the test settles it.
function DeferCurrentUser(): (user: CurrentUserResponse) => void {
    let resolveUser: (user: CurrentUserResponse) => void = () => undefined;
    fetchCurrentUserMock.mockReturnValue(
        new Promise<CurrentUserResponse>((resolve) => {
            resolveUser = resolve;
        }),
    );
    return (user) => {
        resolveUser(user);
    };
}

async function RenderSignedOnSession(user: CurrentUserResponse): Promise<SessionContextValue> {
    fetchCurrentUserMock.mockResolvedValue(user);
    RenderSessionConsumer(CaptureSession);
    await WaitForSessionLoaded();
    return LatestSession();
}

beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
    capturedSession = null;
});

afterEach(() => {
    window.history.replaceState(null, "", jsdomStartUrl);
});

it("shows no user while /auth/me is pending, then the user it returns", async () => {
    const resolveUser = DeferCurrentUser();
    RenderSessionConsumer();
    expect(ShownValue(LOADING_TEST_ID)).toBe("true");
    expect(ShownValue(USER_ID_TEST_ID)).toBe(NO_USER_SHOWN);
    expect(setHomeRouteMock).not.toHaveBeenCalled();
    await act(async () => {
        resolveUser(REGULAR_USER);
    });
    expect(ShownValue(LOADING_TEST_ID)).toBe("false");
    expect(ShownValue(USER_ID_TEST_ID)).toBe(TEST_USER_ID);
});

it("exposes the /auth/me user and sets the home menu (COCOM01Y CDEMO-USER-ID)", async () => {
    fetchCurrentUserMock.mockResolvedValue(REGULAR_USER);
    RenderSessionConsumer();
    await WaitForSessionLoaded();
    expect(ShownValue(USER_ID_TEST_ID)).toBe(TEST_USER_ID);
    expect(ShownValue(USER_TYPE_TEST_ID)).toBe("U");
    expect(fetchCurrentUserMock).toHaveBeenCalledTimes(1);
    expect(setHomeRouteMock).toHaveBeenCalledTimes(1);
    expect(setHomeRouteMock).toHaveBeenCalledWith(MAIN_MENU_ROUTE);
    expect(sessionStorage.getItem(HOME_ROUTE_KEY)).toBe(MAIN_MENU_ROUTE);
});

it("records /admin for type A (COSGN00C CDEMO-USRTYP-ADMIN, XCTL to COADM01C)", async () => {
    await RenderSignedOnSession(ADMIN_USER);
    expect(ShownValue(USER_TYPE_TEST_ID)).toBe("A");
    expect(setHomeRouteMock).toHaveBeenCalledTimes(1);
    expect(setHomeRouteMock).toHaveBeenCalledWith(ADMIN_MENU_ROUTE);
    expect(sessionStorage.getItem(HOME_ROUTE_KEY)).toBe(ADMIN_MENU_ROUTE);
});

it("401 from /auth/me leaves no current user (no COMMAREA, EIBCALEN = 0)", async () => {
    fetchCurrentUserMock.mockRejectedValue(BuildUnauthorizedError());
    RenderSessionConsumer(CaptureSession);
    await WaitForSessionLoaded();
    expect(ShownValue(USER_ID_TEST_ID)).toBe(NO_USER_SHOWN);
    expect(LatestSession().currentUser).toBeNull();
    expect(fetchCurrentUserMock).toHaveBeenCalledTimes(1);
    expect(setHomeRouteMock).not.toHaveBeenCalled();
});

it("RefreshSession re-reads /auth/me, as COSGN00C refilled the COMMAREA", async () => {
    const session = await RenderSignedOnSession(REGULAR_USER);
    fetchCurrentUserMock.mockResolvedValueOnce(ADMIN_USER);
    let refreshedUser: CurrentUserResponse | null = null;
    await act(async () => {
        refreshedUser = await session.RefreshSession();
    });
    expect(refreshedUser).toEqual(ADMIN_USER);
    expect(fetchCurrentUserMock).toHaveBeenCalledTimes(2);
    expect(ShownValue(USER_ID_TEST_ID)).toBe(TEST_ADMIN_ID);
    expect(ShownValue(LOADING_TEST_ID)).toBe("false");
    expect(setHomeRouteMock).toHaveBeenLastCalledWith(ADMIN_MENU_ROUTE);
});

it("RefreshSession rejects a non-axios defect and keeps the user shown", async () => {
    const session = await RenderSignedOnSession(REGULAR_USER);
    const defect = new TypeError("defect in the /auth/me read");
    fetchCurrentUserMock.mockRejectedValueOnce(defect);
    await act(async () => {
        await expect(session.RefreshSession()).rejects.toBe(defect);
    });
    expect(ShownValue(USER_ID_TEST_ID)).toBe(TEST_USER_ID);
    expect(ShownValue(LOADING_TEST_ID)).toBe("false");
    expect(setHomeRouteMock).toHaveBeenCalledTimes(1);
});

it("ClearSession drops the user and the home route (F3 sign-off)", async () => {
    const session = await RenderSignedOnSession(REGULAR_USER);
    act(() => {
        session.ClearSession();
    });
    expect(ShownValue(USER_ID_TEST_ID)).toBe(NO_USER_SHOWN);
    expect(LatestSession().currentUser).toBeNull();
    expect(LatestSession().isLoading).toBe(false);
    expect(clearHomeRouteMock).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem(HOME_ROUTE_KEY)).toBeNull();
});

it("an /auth/me answer arriving after ClearSession does not undo the sign-off", async () => {
    const resolveUser = DeferCurrentUser();
    RenderSessionConsumer(CaptureSession);
    act(() => {
        LatestSession().ClearSession();
    });
    await act(async () => {
        resolveUser(REGULAR_USER);
    });
    expect(ShownValue(USER_ID_TEST_ID)).toBe(NO_USER_SHOWN);
    expect(ShownValue(LOADING_TEST_ID)).toBe("false");
    expect(setHomeRouteMock).not.toHaveBeenCalled();
});

it("holds no client-side role: user type is USRSEC data only (COSGN00C SEC-USR-TYPE)", async () => {
    const session = await RenderSignedOnSession(ADMIN_USER);
    expect(Object.keys(session).sort()).toEqual(SESSION_CONTEXT_KEYS);
    expect(session).not.toHaveProperty("role");
    expect(session).not.toHaveProperty("isAdmin");
    expect(session.currentUser).toEqual(ADMIN_USER);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(1);
    expect(sessionStorage.key(0)).toBe(HOME_ROUTE_KEY);
});

it("stored values never change the server's user type (USRSEC, not the terminal)", async () => {
    sessionStorage.setItem(TAMPERED_USER_TYPE_KEY, "A");
    localStorage.setItem(TAMPERED_USER_TYPE_KEY, "A");
    sessionStorage.setItem(HOME_ROUTE_KEY, ADMIN_MENU_ROUTE);
    await RenderSignedOnSession(REGULAR_USER);
    expect(ShownValue(USER_TYPE_TEST_ID)).toBe("U");
    expect(setHomeRouteMock).toHaveBeenCalledWith(MAIN_MENU_ROUTE);
    expect(sessionStorage.getItem(HOME_ROUTE_KEY)).toBe(MAIN_MENU_ROUTE);
});

it.each(MOUNT_READ_CASES)(
    "mounted on $path reads /auth/me $reads time(s) and ends loading",
    async ({ path, reads, expectedUser }) => {
        window.history.replaceState(null, "", path);
        fetchCurrentUserMock.mockResolvedValue(REGULAR_USER);
        RenderSessionConsumer(CaptureSession);
        await WaitForSessionLoaded();
        expect(fetchCurrentUserMock).toHaveBeenCalledTimes(reads);
        expect(setHomeRouteMock).toHaveBeenCalledTimes(reads);
        expect(LatestSession().currentUser).toEqual(expectedUser);
        expect(LatestSession().isLoading).toBe(false);
    },
);

it("on the sign-on page RefreshSession after login reads /auth/me once", async () => {
    window.history.replaceState(null, "", SIGN_ON_ROUTE);
    fetchCurrentUserMock.mockResolvedValue(ADMIN_USER);
    RenderSessionConsumer(CaptureSession);
    await WaitForSessionLoaded();
    let refreshedUser: CurrentUserResponse | null = null;
    await act(async () => {
        refreshedUser = await LatestSession().RefreshSession();
    });
    expect(refreshedUser).toEqual(ADMIN_USER);
    expect(fetchCurrentUserMock).toHaveBeenCalledTimes(1);
    expect(ShownValue(USER_ID_TEST_ID)).toBe(TEST_ADMIN_ID);
    expect(ShownValue(LOADING_TEST_ID)).toBe("false");
    expect(setHomeRouteMock).toHaveBeenCalledWith(ADMIN_MENU_ROUTE);
});
