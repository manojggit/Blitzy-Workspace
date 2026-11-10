// Mirrors backend/app/schemas/auth.py and backend/app/api/v1/auth.py for COSGN00C (CC00), BMS
// map COSGN00.
//
// The session travels only in the HttpOnly carddemo_session cookie, so no request or response
// body here carries a session credential. The server owns every sign-on edit and literal: a 401
// sign-on literal, the 429 lockout literal or the 503 "Unable to verify the User ..." literal
// rejects the login unchanged, so the sign-on page shows it. The apiClient.ts interceptor exempts
// login and logout from its 401 redirect: Logout answers its own 401 with the thank-you literal.
import axios from "axios";

import { API_CLIENT, type ReadOptions } from "@/lib/apiClient";

// CSMSG01Y CCDA-MSG-THANK-YOU, the same text as THANK_YOU_MESSAGE in backend/app/core/messages.py.
// The server answers it only to a signed-on session. With no session left to end it refuses the
// logout with 401, so the sign-off has already happened and Logout shows the literal itself.
export const THANK_YOU_MESSAGE = "Thank you for using CardDemo application...";

const HTTP_UNAUTHORIZED = 401;

// The user's own menu, decided by the server: /admin (COADM01C) for user type exactly A,
// /menu (COMEN01C) for every other type.
type NextRoute = "/admin" | "/menu";

// The COSGN00 USERID and PASSWD fields (BMS LENGTH 8) as keyed. They are never trimmed or
// upper-cased here, because COSGN00C upper-cases both before its lookup and compare. The server
// default is blank, so an absent field reaches the "Please enter ..." edits as a blank field.
export interface LoginRequest {
    user_id?: string;
    password?: string;
}

// A successful sign-on: the COMMAREA user fields COSGN00C filled before its XCTL.
export interface LoginResponse {
    user_id: string;
    user_type: string;
    first_name: string;
    last_name: string;
    next_route: NextRoute;
}

// The PF3 sign-off answer: the CSMSG01Y thank-you literal.
export interface LogoutResponse {
    message: string;
}

// The user of the current session (GET /auth/me), for the screen header and the choice of menu.
export interface CurrentUserResponse {
    user_id: string;
    user_type: string;
    first_name: string;
    last_name: string;
    next_route: NextRoute;
}

// ENTER on COSGN00; on success the server sets the session cookie.
export async function Login(request: LoginRequest): Promise<LoginResponse> {
    const response = await API_CLIENT.post<LoginResponse>("/auth/login", request);
    return response.data;
}

// PF3 on COSGN00 or a menu; the server clears the session cookie. A 401 means no session was open
// or it had expired, which is already the sign-off, so it resolves with THANK_YOU_MESSAGE. Every
// other failure rejects unchanged.
export async function Logout(): Promise<LogoutResponse> {
    try {
        const response = await API_CLIENT.post<LogoutResponse>("/auth/logout");
        return response.data;
    } catch (error) {
        if (axios.isAxiosError(error) && error.response?.status === HTTP_UNAUTHORIZED) {
            return { message: THANK_YOU_MESSAGE };
        }
        throw error;
    }
}

// options carries the owning read's AbortSignal; login and logout are writes and take none.
export async function FetchCurrentUser(options?: ReadOptions): Promise<CurrentUserResponse> {
    const response = await API_CLIENT.get<CurrentUserResponse>("/auth/me", {
        signal: options?.signal,
    });
    return response.data;
}
