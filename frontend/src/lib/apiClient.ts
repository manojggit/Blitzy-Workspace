// Shared API client for every CardDemo page. Replaces the COMMAREA identity hand-off (COCOM01Y
// CDEMO-USER-ID, CDEMO-USER-TYPE), which the HttpOnly session cookie now carries, the XCTL back
// to the user's own menu (COSGN00C: type A to COADM01C, any other type to COMEN01C), and the BMS
// ERRMSG line contract. The common types mirror backend/app/schemas/common.py; the abend text is
// COACTUPC ABEND-ROUTINE (CSMSG02Y ABEND-MSG).
import axios, { type AxiosError, type AxiosResponse } from "axios";

function StripTrailingSlashes(origin: string): string {
    let end = origin.length;
    while (end > 0 && origin[end - 1] === "/") {
        end -= 1;
    }
    return origin.slice(0, end);
}

// The backend origin only. The property is written out literally so that Next.js inlines it into
// the client bundle at build time. A trailing "/" is dropped, so every path below starts with the
// backend's API_V1_PREFIX exactly, never "//api/v1".
export const API_BASE_URL: string = StripTrailingSlashes(process.env.NEXT_PUBLIC_API_URL ?? "");
export const API_V1_PATH = "/api/v1";
// The root of every call, the bare /auth/me lookup included.
export const API_V1_URL = API_BASE_URL + API_V1_PATH;

// Longer than the backend's bounds on a connection attempt and on one statement (by default
// DB_CONNECT_TIMEOUT_SECONDS 10 and DB_COMMAND_TIMEOUT_SECONDS 30), yet a stalled response still
// ends. Those bounds do not cover a whole request, which can run several statements, so a timed-out
// call, a write especially, has an unknown outcome: the server may still have committed it. No
// call is therefore retried automatically. It is a code constant because the frontend reads only
// NEXT_PUBLIC_API_URL from the environment.
export const REQUEST_TIMEOUT_MILLISECONDS = 60000;

// The session cookie travels on every call; no token, role or credential is held client-side, and
// the browser supplies the Origin header that the backend OriginGuard checks. A caller abandons a
// call through axios's per-call signal (an AbortController). A timed-out or cancelled call
// rejects with an axios error without a response, which the interceptor rejects unchanged and
// ToScreenFeedback shows as the abend text, as it does a network failure.
export const API_CLIENT = axios.create({
    baseURL: API_V1_URL,
    withCredentials: true,
    timeout: REQUEST_TIMEOUT_MILLISECONDS,
});

// Path segments a URL cannot carry as typed: an empty one reaches another route, and "." or ".."
// is removed by URL normalisation ("/accounts/./edit" would become the view of "edit", and
// "/transactions/.." the API root). Each travels with one trailing space instead, so a blank key
// arrives as SPACES (%20); the server cannot tell the difference, because it compares every key
// padded with spaces to its BMS LENGTH.
const UNSENDABLE_PATH_SEGMENTS: readonly string[] = ["", ".", ".."];

// One raw screen key (ACCTSID, CARDSID, TRNIDIN, USRIDIN) or card_ref as one path segment. The
// text is never validated, trimmed or case-folded: "/" and "%" are percent-encoded, and the
// backend's LosslessKeyRoute hands the key to the program's edits exactly as keyed.
export function EncodePathKey(value: string): string {
    const sendableValue = UNSENDABLE_PATH_SEGMENTS.includes(value) ? `${value} ` : value;
    return encodeURIComponent(sendableValue);
}

export const ABEND_MESSAGE = "UNEXPECTED ABEND OCCURRED.";
export const SIGN_ON_ROUTE = "/signon";
export const DEFAULT_HOME_ROUTE = "/menu";
export const ADMIN_HOME_ROUTE = "/admin";
export const PENDING_MESSAGE_KEY = "carddemo.pendingMessage";
export const HOME_ROUTE_KEY = "carddemo.homeRoute";

// The only routes a home route may name, as next_route is Literal["/admin", "/menu"] in
// backend/app/schemas/auth.py. Any other stored or served value never reaches navigation.
const HOME_ROUTES: readonly string[] = [DEFAULT_HOME_ROUTE, ADMIN_HOME_ROUTE];

// The routes a server answer may navigate to: exactly one of the pathnames, with exactly one
// queryKey parameter when a queryKey is given and with no query at all otherwise.
export interface RouteAllowance {
    readonly pathnames: readonly string[];
    readonly queryKey?: string;
}

// The sign-on page's next_route and the recorded home route are checked against this one list.
export const HOME_ROUTE_ALLOWANCE: RouteAllowance = { pathnames: HOME_ROUTES };

// Their 401 bodies carry the sign-on literals (Wrong Password. Try again ...), which the sign-on
// page shows instead of being redirected.
const UNREDIRECTED_AUTH_PATHS = ["/auth/login", "/auth/logout"] as const;
const CURRENT_USER_URL = API_V1_URL + "/auth/me";

const HTTP_UNAUTHORIZED = 401;
const HTTP_FORBIDDEN = 403;
const HTTP_INTERNAL_SERVER_ERROR = 500;

// The ERRMSG and INFOMSG lines and the cursor of a screen response (common.ScreenFeedback).
// Every key has a server default, so the wire may omit any of them.
export interface ScreenFeedback {
    message?: string;
    info_message?: string;
    field_errors?: string[];
    cursor_field?: string | null;
}

// The feedback a page shows: every line present, an absent key filled with its empty value, the
// server's text verbatim and a null cursor kept.
export type CompleteScreenFeedback = Required<ScreenFeedback>;

// The error body of every non-2xx legacy outcome (common.LegacyErrorResponse). Only message is
// required on the wire; the other keys may be absent.
export interface LegacyErrorResponse extends ScreenFeedback {
    message: string;
}

export interface MessageResponse {
    message: string;
}

// The paging position of the CCLI, CT00 and CU00 lists (common.PageState). Every key has a
// server default, so the wire may omit any of them.
export interface PageState {
    page?: number;
    first_key?: string;
    last_key?: string;
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

// The DOMException names of a browser that refuses storage (SecurityError, raised even by reading
// window.sessionStorage) or has no room left in it (QuotaExceededError).
const STORAGE_FAILURE_NAMES: readonly string[] = ["SecurityError", "QuotaExceededError"];

// The home route recorded by this page load; sessionStorage keeps it across full page loads. It
// stays authoritative for this page load when the browser refuses storage.
let recordedHomeRoute: string | null = null;
// The /auth/me lookup in flight, shared by every refusal that arrives while it runs.
let homeRouteLookup: Promise<string> | null = null;
// Advances whenever a session ends (sign-off or a new login), so a refusal that began under an
// earlier session neither stores its message nor navigates.
let sessionGeneration = 0;

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

// Only resolves a route for the same-origin check below; nothing is ever requested from it.
const ROUTE_PARSE_ORIGIN = "http://route.invalid";
// Whitespace, control characters, backslashes (which browsers read as "/") and fragments are
// never part of a route the server answers, so a route holding any of them is refused unparsed.
const UNSAFE_ROUTE_CHARACTERS = /[\s\p{Cc}\\#]/u;
const CONTROL_CHARACTERS = /\p{Cc}/u;
const QUERY_SEPARATOR = "?";

interface RouteParts {
    pathname: string;
    query: string | null;
}

// The text before the first "?", and the text after it, or null when the route has no "?".
function SplitRoute(route: string): RouteParts {
    const separatorIndex = route.indexOf(QUERY_SEPARATOR);
    if (separatorIndex === -1) {
        return { pathname: route, query: null };
    }
    return { pathname: route.slice(0, separatorIndex), query: route.slice(separatorIndex + 1) };
}

// The value of the query's one parameter, or null when the query names any other key, repeats the
// key, or gives it a blank value or a control character.
function AllowedQueryValue(query: string, queryKey: string): string | null {
    const parameters = new URLSearchParams(query);
    const value = parameters.get(queryKey);
    const isSingleKey = Array.from(parameters.keys()).length === 1;
    if (!isSingleKey || value === null || value.trim() === "" || CONTROL_CHARACTERS.test(value)) {
        return null;
    }
    return value;
}

// "" for a route with no query that needs none, "?" with the one allowed parameter re-encoded,
// or null for any other query.
function CanonicalQuery(query: string | null, queryKey: string | undefined): string | null {
    if (queryKey === undefined) {
        return query === null ? "" : null;
    }
    const value = query === null ? null : AllowedQueryValue(query, queryKey);
    if (value === null) {
        return null;
    }
    const parameters = new URLSearchParams();
    parameters.set(queryKey, value);
    return QUERY_SEPARATOR + parameters.toString();
}

// Only a pathname that starts with a single "/" is a path on this origin: "//" names a host, and
// anything else a scheme or a path relative to the current page.
function IsOriginPath(pathname: string): boolean {
    return pathname.startsWith("/") && !pathname.startsWith("//");
}

// The URL parser has the final word, as a browser would read the route: it must stay on this
// origin with the pathname unchanged, so no allowed pathname can name another site or be rewritten
// by a dot segment. A path that starts with a single "/" always parses against a base.
function StaysOnPathname(route: string, pathname: string): boolean {
    if (!IsOriginPath(pathname)) {
        return false;
    }
    const resolved = new URL(route, ROUTE_PARSE_ORIGIN);
    return resolved.origin === ROUTE_PARSE_ORIGIN && resolved.pathname === pathname;
}

function CanonicalRoute(route: string, allowance: RouteAllowance): string | null {
    const { pathname, query } = SplitRoute(route);
    const canonicalQuery = CanonicalQuery(query, allowance.queryKey);
    if (!allowance.pathnames.includes(pathname) || canonicalQuery === null) {
        return null;
    }
    const canonicalRoute = pathname + canonicalQuery;
    return StaysOnPathname(canonicalRoute, pathname) ? canonicalRoute : null;
}

// Every route a server answer names passes through here before it reaches navigation, because a
// TypeScript type does not check the JSON that arrives. The answer is the canonical same-origin
// route, rebuilt from the checked parts, or null for a value that is not a string, holds an unsafe
// character, or is not exactly allowed: the pathname is never trimmed, case-folded or given a
// trailing "/" or dot segment, and the query is absent without a queryKey, and with one holds
// exactly that key once with a non-blank value.
export function ToAllowedRoute(route: unknown, allowance: RouteAllowance): string | null {
    if (typeof route !== "string" || UNSAFE_ROUTE_CHARACTERS.test(route)) {
        return null;
    }
    return CanonicalRoute(route, allowance);
}

// Exact comparison only: a home route is never trimmed or case-folded, and carries no query.
function IsHomeRoute(route: unknown): route is string {
    return ToAllowedRoute(route, HOME_ROUTE_ALLOWANCE) !== null;
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

// Ends the session: every refusal still waiting on its home route becomes obsolete, and the lookup
// in flight is abandoned so its answer is never recorded for the next user. Recording a home route
// (SetHomeRoute, or a lookup keeping its own answer) is not a new session, so a refusal waiting
// while the session context records the route still returns to it.
function InvalidatePendingRefusals(): void {
    sessionGeneration += 1;
    homeRouteLookup = null;
}

// Sign-off, and a successful login (HandleResponse): the previous user's refusals and home route
// never reach the next user.
export function ClearHomeRoute(): void {
    InvalidatePendingRefusals();
    ForgetHomeRoute();
}

// A 422 body carries FastAPI's detail list beside empty legacy keys. Its input is structurally
// malformed, which a 3270 terminal could never send, so it is not a legacy outcome.
function IsLegacyBody(body: unknown): body is LegacyErrorResponse {
    if (typeof body !== "object" || body === null || "detail" in body) {
        return false;
    }
    return "message" in body && typeof body.message === "string";
}

// Rethrows anything that is not an axios error; null means the outcome is the abend text.
function LegacyBodyOf(error: unknown): LegacyErrorResponse | null {
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
function FeedbackFromBody(body: LegacyErrorResponse): CompleteScreenFeedback {
    return {
        message: body.message,
        info_message: body.info_message ?? "",
        field_errors: body.field_errors ?? [],
        cursor_field: body.cursor_field === undefined ? "" : body.cursor_field,
    };
}

// The legacy feedback of a failed call. A 500, a network failure or a non-legacy body shows the
// abend text, because the backend registers no catch-all handler.
export function ToScreenFeedback(error: unknown): CompleteScreenFeedback {
    const body = LegacyBodyOf(error);
    if (body === null) {
        return { message: ABEND_MESSAGE, info_message: "", field_errors: [], cursor_field: "" };
    }
    return FeedbackFromBody(body);
}

// The whole legacy error body, with the extra keys of a screen such as state, screen_token or
// fields and the standard keys completed as ToScreenFeedback completes them; null when
// ToScreenFeedback would show the abend text.
export function LegacyErrorBody<T extends LegacyErrorResponse>(
    error: unknown,
): (T & CompleteScreenFeedback) | null {
    const body = LegacyBodyOf(error);
    if (body === null) {
        return null;
    }
    return { ...body, ...FeedbackFromBody(body) } as T & CompleteScreenFeedback;
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
// sign-off or a new login is not kept. Keeping its own answer leaves waiting refusals current; the
// session generation alone decides whether they act. The bare axios call keeps the interceptor
// from re-entering itself.
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

// The lookup stops being current when it settles, or when the session ends and abandons it.
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
// nothing is stored or reloaded: the page shows its own error, and a reload would loop. A refusal
// whose session ended while its home route was resolved (sign-off or a new login) is obsolete, so
// it neither stores its message nor navigates.
async function HandleForbidden(error: AxiosError): Promise<void> {
    if (typeof window === "undefined") {
        return;
    }
    const refusalGeneration = sessionGeneration;
    const homeRoute = await ResolveHomeRoute();
    if (refusalGeneration !== sessionGeneration || window.location.pathname === homeRoute) {
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

// A 2xx answer to POST /auth/login, matched on the path as the 401 exemptions are.
const LOGIN_METHOD = "post";
const LOGIN_PATH = "/auth/login";
const HTTP_SUCCESS_FIRST = 200;
const HTTP_SUCCESS_END = 300;

function IsSuccessfulLogin({ config, status }: AxiosResponse): boolean {
    const isSuccess = status >= HTTP_SUCCESS_FIRST && status < HTTP_SUCCESS_END;
    const url = config.url ?? "";
    return isSuccess && config.method?.toLowerCase() === LOGIN_METHOD && url.endsWith(LOGIN_PATH);
}

// The interceptor's success path. A successful login replaces the identity, so the previous
// user's refusals and home route are dropped; the session context then records the new user's
// route. Every response is returned unchanged.
export function HandleResponse(response: AxiosResponse): AxiosResponse {
    if (IsSuccessfulLogin(response)) {
        ClearHomeRoute();
    }
    return response;
}

API_CLIENT.interceptors.response.use(HandleResponse, HandleResponseError);

// The one severity rule for every page; the page decides whether its response completed a write.
export function SeverityForOutcome({ isError, isCompletedWrite }: OutcomeFlags): MessageSeverity {
    if (isError) {
        return "error";
    }
    return isCompletedWrite ? "success" : "info";
}
