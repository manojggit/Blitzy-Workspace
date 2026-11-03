// Shared API client for every CardDemo page. Replaces the COMMAREA identity hand-off (COCOM01Y
// CDEMO-USER-ID, CDEMO-USER-TYPE), which the HttpOnly session cookie now carries, the XCTL back
// to the user's own menu (COSGN00C: type A to COADM01C, any other type to COMEN01C), and the BMS
// ERRMSG line contract. The common types mirror backend/app/schemas/common.py; the abend text is
// COACTUPC ABEND-ROUTINE (CSMSG02Y ABEND-MSG).
import axios, { type AxiosError } from "axios";

// The backend origin only. The property is written out literally so that Next.js inlines it into
// the client bundle at build time.
export const API_BASE_URL: string = process.env.NEXT_PUBLIC_API_URL ?? "";
export const API_V1_PATH = "/api/v1";

// Longer than the backend's own bound on a statement (by default DB_CONNECT_TIMEOUT_SECONDS 10
// plus DB_COMMAND_TIMEOUT_SECONDS 30), so the client never abandons work the server may still
// finish, yet a stalled response still ends. It is a code constant because the frontend reads
// only NEXT_PUBLIC_API_URL from the environment.
export const REQUEST_TIMEOUT_MILLISECONDS = 60000;

// The session cookie travels on every call; no token, role or credential is held client-side, and
// the browser supplies the Origin header that the backend OriginGuard checks. A caller abandons a
// call through axios's per-call signal (an AbortController). A timed-out or cancelled call
// rejects with an axios error without a response, which the interceptor rejects unchanged and
// ToScreenFeedback shows as the abend text, as it does a network failure.
export const API_CLIENT = axios.create({
    baseURL: API_BASE_URL + API_V1_PATH,
    withCredentials: true,
    timeout: REQUEST_TIMEOUT_MILLISECONDS,
});

export const ABEND_MESSAGE = "UNEXPECTED ABEND OCCURRED.";
export const SIGN_ON_ROUTE = "/signon";
export const DEFAULT_HOME_ROUTE = "/menu";
export const ADMIN_HOME_ROUTE = "/admin";
export const PENDING_MESSAGE_KEY = "carddemo.pendingMessage";
export const HOME_ROUTE_KEY = "carddemo.homeRoute";

// The only routes a home route may name, as next_route is Literal["/admin", "/menu"] in
// backend/app/schemas/auth.py. Any other stored or served value never reaches navigation.
const HOME_ROUTES: readonly string[] = [DEFAULT_HOME_ROUTE, ADMIN_HOME_ROUTE];

// Their 401 bodies carry the sign-on literals (Wrong Password. Try again ...), which the sign-on
// page shows instead of being redirected.
const UNREDIRECTED_AUTH_PATHS = ["/auth/login", "/auth/logout"] as const;
const CURRENT_USER_URL = API_BASE_URL + API_V1_PATH + "/auth/me";

const HTTP_UNAUTHORIZED = 401;
const HTTP_FORBIDDEN = 403;
const HTTP_INTERNAL_SERVER_ERROR = 500;

// The ERRMSG and INFOMSG lines and the cursor of a screen response (common.ScreenFeedback).
export interface ScreenFeedback {
    message: string;
    info_message: string;
    field_errors: string[];
    cursor_field: string | null;
}

// The error body of every non-2xx legacy outcome (common.LegacyErrorResponse).
export type LegacyErrorResponse = ScreenFeedback;

export interface MessageResponse {
    message: string;
}

// The paging position of the CCLI, CT00 and CU00 lists (common.PageState).
export interface PageState {
    page: number;
    first_key: string;
    last_key: string;
}

// Decimal amounts arrive as JSON strings and are never parsed to a floating-point number.
export type MoneyAmount = string;

// The F8 (READNEXT) and F7 (READPREV) paging keys.
export enum Direction {
    FORWARD = "forward",
    BACKWARD = "backward",
}

// The CAUP and CCUP keys: ENTER, F5 save and F12 cancel.
export enum UpdateAction {
    ENTER = "enter",
    SAVE = "save",
    CANCEL = "cancel",
}

// ACUP-CHANGE-ACTION and CCUP-CHANGE-ACTION.
export enum UpdateScreenState {
    DETAILS_NOT_FETCHED = "DETAILS_NOT_FETCHED",
    SHOW_DETAILS = "SHOW_DETAILS",
    CHANGES_NOT_OK = "CHANGES_NOT_OK",
    CHANGES_OK_NOT_CONFIRMED = "CHANGES_OK_NOT_CONFIRMED",
    CHANGES_DONE = "CHANGES_DONE",
    CHANGES_FAILED = "CHANGES_FAILED",
}

// Red errors, green confirmations and neutral information on the message line.
export type MessageSeverity = "error" | "success" | "info";

export interface OutcomeFlags {
    isError: boolean;
    isCompletedWrite: boolean;
}

// A body whose message is a string; the other standard keys may be absent.
interface LegacyBody extends Partial<LegacyErrorResponse> {
    message: string;
}

// The DOMException names of a browser that refuses storage (SecurityError, raised even by reading
// window.sessionStorage) or has no room left in it (QuotaExceededError).
const STORAGE_FAILURE_NAMES: readonly string[] = ["SecurityError", "QuotaExceededError"];

// The home route recorded by this page load; sessionStorage keeps it across full page loads. It
// stays authoritative for this page load when the browser refuses storage.
let recordedHomeRoute: string | null = null;
// The /auth/me lookup in flight, shared by every refusal that arrives while it runs.
let homeRouteLookup: Promise<string> | null = null;

// Storage is optional here, so a refusal leaves only what the page holds in memory. Any other
// error is a defect and propagates.
function IsStorageFailure(error: unknown): error is DOMException {
    return error instanceof DOMException && STORAGE_FAILURE_NAMES.includes(error.name);
}

function ReadSessionValue(key: string): string | null {
    if (typeof window === "undefined") {
        return null;
    }
    try {
        return window.sessionStorage.getItem(key);
    } catch (error) {
        if (!IsStorageFailure(error)) {
            throw error;
        }
        return null;
    }
}

function WriteSessionValue(key: string, value: string): void {
    if (typeof window === "undefined") {
        return;
    }
    try {
        window.sessionStorage.setItem(key, value);
    } catch (error) {
        if (!IsStorageFailure(error)) {
            throw error;
        }
    }
}

function RemoveSessionValue(key: string): void {
    if (typeof window === "undefined") {
        return;
    }
    try {
        window.sessionStorage.removeItem(key);
    } catch (error) {
        if (!IsStorageFailure(error)) {
            throw error;
        }
    }
}

// Carries a message across a navigation: the 403 refusal literal, or the F3 sign-off literal.
// Where the browser refuses storage, the message is lost but the navigation still happens.
export function StorePendingMessage(message: string): void {
    WriteSessionValue(PENDING_MESSAGE_KEY, message);
}

export function TakePendingMessage(): string {
    const message = ReadSessionValue(PENDING_MESSAGE_KEY) ?? "";
    RemoveSessionValue(PENDING_MESSAGE_KEY);
    return message;
}

// Exact comparison only: a home route is never trimmed, case-folded or parsed as a URL.
function IsHomeRoute(route: unknown): route is string {
    return typeof route === "string" && HOME_ROUTES.includes(route);
}

function ForgetHomeRoute(): void {
    recordedHomeRoute = null;
    RemoveSessionValue(HOME_ROUTE_KEY);
}

// The route is the server's next_route. Any value other than a home route is not recorded and
// discards the earlier record. On the server the call does nothing, because a module-level value
// there would be shared by every request.
export function SetHomeRoute(route: string): void {
    if (typeof window === "undefined") {
        return;
    }
    if (!IsHomeRoute(route)) {
        ForgetHomeRoute();
        return;
    }
    recordedHomeRoute = route;
    WriteSessionValue(HOME_ROUTE_KEY, route);
}

// A stored value that is not a home route is removed, so the server is asked instead.
function RecordedHomeRoute(): string | null {
    if (recordedHomeRoute !== null) {
        return recordedHomeRoute;
    }
    const storedRoute = ReadSessionValue(HOME_ROUTE_KEY);
    if (IsHomeRoute(storedRoute)) {
        return storedRoute;
    }
    if (storedRoute !== null) {
        RemoveSessionValue(HOME_ROUTE_KEY);
    }
    return null;
}

export function GetHomeRoute(): string {
    return RecordedHomeRoute() ?? DEFAULT_HOME_ROUTE;
}

// Sign-off also abandons a lookup in flight, so its answer is never recorded for the next user.
export function ClearHomeRoute(): void {
    homeRouteLookup = null;
    ForgetHomeRoute();
}

// A 422 body carries FastAPI's detail list beside empty legacy keys. Its input is structurally
// malformed, which a 3270 terminal could never send, so it is not a legacy outcome.
function IsLegacyBody(body: unknown): body is LegacyBody {
    if (typeof body !== "object" || body === null || "detail" in body) {
        return false;
    }
    return "message" in body && typeof body.message === "string";
}

// Rethrows anything that is not an axios error; null means the outcome is the abend text.
function LegacyBodyOf(error: unknown): LegacyBody | null {
    if (!axios.isAxiosError(error)) {
        throw error;
    }
    const body: unknown = error.response?.data;
    if (error.response?.status === HTTP_INTERNAL_SERVER_ERROR || !IsLegacyBody(body)) {
        return null;
    }
    return body;
}

// The server's text is kept verbatim; only an absent key gets its empty value.
function FeedbackFromBody(body: LegacyBody): ScreenFeedback {
    return {
        message: body.message,
        info_message: body.info_message ?? "",
        field_errors: body.field_errors ?? [],
        cursor_field: body.cursor_field === undefined ? "" : body.cursor_field,
    };
}

// The legacy feedback of a failed call. A 500, a network failure or a non-legacy body shows the
// abend text, because the backend registers no catch-all handler.
export function ToScreenFeedback(error: unknown): ScreenFeedback {
    const body = LegacyBodyOf(error);
    if (body === null) {
        return { message: ABEND_MESSAGE, info_message: "", field_errors: [], cursor_field: "" };
    }
    return FeedbackFromBody(body);
}

// The whole legacy error body, with the extra keys of a screen such as state, screen_token or
// fields; null when ToScreenFeedback would show the abend text.
export function LegacyErrorBody<T extends ScreenFeedback>(error: unknown): T | null {
    const body = LegacyBodyOf(error);
    if (body === null) {
        return null;
    }
    return { ...body, ...FeedbackFromBody(body) } as T;
}

function HandleUnauthorized(error: AxiosError): void {
    if (typeof window === "undefined" || window.location.pathname === SIGN_ON_ROUTE) {
        return;
    }
    const url = error.config?.url ?? "";
    if (UNREDIRECTED_AUTH_PATHS.some((path) => url.endsWith(path))) {
        return;
    }
    window.location.assign(SIGN_ON_ROUTE);
}

// A next_route that is not a home route sends the user to the main menu and is not recorded. A
// home route is recorded only while its lookup is still current, so an answer that arrives after
// sign-off is not kept. The bare axios call keeps the interceptor from re-entering itself.
async function FetchServerHomeRoute(isCurrent: () => boolean): Promise<string> {
    const { data } = await axios.get<{ next_route?: unknown } | null>(CURRENT_USER_URL, {
        withCredentials: true,
        timeout: REQUEST_TIMEOUT_MILLISECONDS,
    });
    const route = data?.next_route;
    if (!IsHomeRoute(route)) {
        return DEFAULT_HOME_ROUTE;
    }
    if (isCurrent()) {
        SetHomeRoute(route);
    }
    return route;
}

// A failed lookup sends the user to the main menu. An error that is not an axios error is a
// defect, and it reaches every refusal waiting on the lookup.
async function LookUpHomeRoute(isCurrent: () => boolean): Promise<string> {
    try {
        return await FetchServerHomeRoute(isCurrent);
    } catch (error) {
        if (axios.isAxiosError(error)) {
            return DEFAULT_HOME_ROUTE;
        }
        throw error;
    }
}

// The lookup stops being current when it settles, or when ClearHomeRoute abandons it.
function StartHomeRouteLookup(): Promise<string> {
    const isCurrent = (): boolean => homeRouteLookup === lookup;
    const lookup: Promise<string> = LookUpHomeRoute(isCurrent).finally(() => {
        if (isCurrent()) {
            homeRouteLookup = null;
        }
    });
    homeRouteLookup = lookup;
    return lookup;
}

// Refusals that arrive together share one lookup instead of each asking the server.
async function ResolveHomeRoute(): Promise<string> {
    const knownRoute = RecordedHomeRoute();
    if (knownRoute !== null) {
        return knownRoute;
    }
    return homeRouteLookup ?? StartHomeRouteLookup();
}

// A refusal returns to the user's own menu, which shows the server message. On the menu itself
// nothing is stored or reloaded: the page shows its own error, and a reload would loop.
async function HandleForbidden(error: AxiosError): Promise<void> {
    if (typeof window === "undefined") {
        return;
    }
    const homeRoute = await ResolveHomeRoute();
    if (window.location.pathname === homeRoute) {
        return;
    }
    const body: unknown = error.response?.data;
    if (IsLegacyBody(body) && body.message !== "") {
        StorePendingMessage(body.message);
    }
    window.location.assign(homeRoute);
}

// The response interceptor. Every error is still rejected, so callers show their own feedback.
export async function HandleResponseError(error: unknown): Promise<never> {
    if (axios.isAxiosError(error)) {
        const status = error.response?.status;
        if (status === HTTP_UNAUTHORIZED) {
            HandleUnauthorized(error);
        } else if (status === HTTP_FORBIDDEN) {
            await HandleForbidden(error);
        }
    }
    return Promise.reject(error);
}

API_CLIENT.interceptors.response.use((response) => response, HandleResponseError);

// The one severity rule for every page; the page decides whether its response completed a write.
export function SeverityForOutcome({ isError, isCompletedWrite }: OutcomeFlags): MessageSeverity {
    if (isError) {
        return "error";
    }
    return isCompletedWrite ? "success" : "info";
}
