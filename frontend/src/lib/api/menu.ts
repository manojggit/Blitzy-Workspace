// Mirrors backend/app/schemas/menu.py and backend/app/api/v1/menu.py for COMEN01C (CM00) and
// COADM01C (CA00), BMS maps COMEN01 and COADM01.
// The server owns every option edit and literal. A 400 option literal or a 403 role refusal
// rejects the call unchanged: the page shows it, and the apiClient.ts interceptor returns a
// refused user to their own menu.
import { API_CLIENT, type ReadOptions } from "@/lib/apiClient";

// One entry of the COMEN02Y (11 options) or COADM02Y (6 options) table, label verbatim.
export interface MenuOption {
    number: number;
    label: string;
}

export interface MenuResponse {
    options: MenuOption[];
}

// The OPTION field (BMS LENGTH 2) as keyed. It is never trimmed, padded or converted to a number,
// because COMEN01C and COADM01C run their numeric and range edits on the raw text. The server
// default is blank, so an absent option reaches that edit as a blank field.
export interface MenuSelectionRequest {
    option?: string;
}

// A route replaces the XCTL to the option's program; a null route keeps the menu and carries the
// not-installed or coming-soon literal in message.
export interface MenuSelectionResponse {
    route: string | null;
    message: string;
}

// options carries the owning read's AbortSignal, as on FetchAdminMenu; a selection is a write and
// takes none.
export async function FetchMainMenu(options?: ReadOptions): Promise<MenuResponse> {
    const response = await API_CLIENT.get<MenuResponse>("/menu/main", { signal: options?.signal });
    return response.data;
}

export async function SelectMainMenuOption(
    request: MenuSelectionRequest,
): Promise<MenuSelectionResponse> {
    const response = await API_CLIENT.post<MenuSelectionResponse>("/menu/main/selection", request);
    return response.data;
}

export async function FetchAdminMenu(options?: ReadOptions): Promise<MenuResponse> {
    const response = await API_CLIENT.get<MenuResponse>("/menu/admin", { signal: options?.signal });
    return response.data;
}

export async function SelectAdminMenuOption(
    request: MenuSelectionRequest,
): Promise<MenuSelectionResponse> {
    const response = await API_CLIENT.post<MenuSelectionResponse>("/menu/admin/selection", request);
    return response.data;
}
