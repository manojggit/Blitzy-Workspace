// Mirrors backend/app/schemas/users.py and backend/app/api/v1/admin_users.py for COUSR00C (CU00),
// COUSR01C (CU01), COUSR02C (CU02) and COUSR03C (CU03), BMS maps COUSR00–COUSR03.
//
// A property is optional exactly where its Pydantic field has a default, as the OpenAPI document
// marks it. Every screen field travels as typed, never trimmed or case-folded: the server runs the
// programs' presence checks, the U/D selection, the 10-row paging and the hashed-password change
// detection on the raw text, and every legacy outcome reaches the caller as a rejected call.
import { API_CLIENT, EncodePathKey, type Direction, type PageState } from "@/lib/apiClient";

const USERS_PATH = "/admin/users";
const SELECTION_PATH = `${USERS_PATH}/selection`;

// The screen fetching a user: COUSR02 (update prompt) or COUSR03 (delete prompt).
export enum UserFetchScreen {
    UPDATE = "update",
    DELETE = "delete",
}

// The GET /admin/users query (COUSR00). Without a direction it is ENTER or the first entry,
// browsing from searchUserId; F7 and F8 send the shown page's first_key or last_key as startKey.
export interface UserListQuery {
    searchUserId?: string;
    startKey?: string;
    direction?: Direction;
    page?: number;
}

// The GET /admin/users/{userId} query: the COUSR02 and COUSR03 ENTER fetch.
export interface UserFetchQuery {
    screen?: UserFetchScreen;
}

// One COUSR00 row: USRIDnn, FNAMEnn, LNAMEnn and UTYPEnn. It carries no password or hash.
export interface UserListRow {
    user_id: string;
    first_name: string;
    last_name: string;
    user_type: string;
}

// One COUSR00 page of at most 10 users, with its paging position.
export interface UserListResponse extends PageState {
    rows?: UserListRow[];
    message?: string;
    cursor_field?: string | null;
}

// One COUSR00 row as keyed: its USRIDnn key and its raw SELnnnn code.
export interface UserSelection {
    user_id?: string;
    selection?: string;
}

// The COUSR00 ENTER body: the shown rows with their codes, and the search key applied after them.
export interface UserSelectionRequest {
    selections?: UserSelection[];
    search_user_id?: string;
}

// A route for a valid U or D selection; null when the list keys carry the re-browsed page.
export interface UserSelectionResponse extends UserListResponse {
    route?: string | null;
}

// The COUSR01 add body. user_type is free text, because COUSR01C accepts any character.
export interface UserCreateRequest {
    first_name?: string;
    last_name?: string;
    user_id?: string;
    password?: string;
    user_type?: string;
}

// A fetched user as COUSR02 or COUSR03 shows it, with the fetching screen's prompt.
export interface UserDetailResponse {
    user_id: string;
    first_name: string;
    last_name: string;
    user_type: string;
    message?: string;
}

// The COUSR02 F5 or F3 body; the user ID is the path key. A blank password keeps the stored one.
export interface UserUpdateRequest {
    first_name?: string;
    last_name?: string;
    password?: string;
    user_type?: string;
}

// The ERRMSG line and cursor of an add, update or delete that succeeded.
export interface UserMessageResponse {
    message: string;
    cursor_field?: string | null;
}

function UserPath(userId: string): string {
    return `${USERS_PATH}/${EncodePathKey(userId)}`;
}

// CU00 (COUSR00C): ENTER, F7 and F8 on the user list.
export async function ListUsers(query: UserListQuery): Promise<UserListResponse> {
    const response = await API_CLIENT.get<UserListResponse>(USERS_PATH, { params: query });
    return response.data;
}

// CU00 (COUSR00C): ENTER with selection codes keyed on the list.
export async function SelectUsers(request: UserSelectionRequest): Promise<UserSelectionResponse> {
    const response = await API_CLIENT.post<UserSelectionResponse>(SELECTION_PATH, request);
    return response.data;
}

// CU01 (COUSR01C): ENTER on the add screen.
export async function AddUser(request: UserCreateRequest): Promise<UserMessageResponse> {
    const response = await API_CLIENT.post<UserMessageResponse>(USERS_PATH, request);
    return response.data;
}

// CU02 (COUSR02C) and CU03 (COUSR03C): ENTER on the update or delete screen.
export async function FetchUser(
    userId: string,
    screen: UserFetchScreen,
): Promise<UserDetailResponse> {
    const params: UserFetchQuery = { screen };
    const response = await API_CLIENT.get<UserDetailResponse>(UserPath(userId), { params });
    return response.data;
}

// CU02 (COUSR02C): F5, and F3, which saves before returning.
export async function UpdateUser(
    userId: string,
    request: UserUpdateRequest,
): Promise<UserMessageResponse> {
    const response = await API_CLIENT.put<UserMessageResponse>(UserPath(userId), request);
    return response.data;
}

// CU03 (COUSR03C): F5 on the delete screen.
export async function DeleteUser(userId: string): Promise<UserMessageResponse> {
    const response = await API_CLIENT.delete<UserMessageResponse>(UserPath(userId));
    return response.data;
}
