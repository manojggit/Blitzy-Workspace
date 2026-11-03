// Mirrors backend/app/schemas/billpay.py and backend/app/api/v1/billpay.py for COBIL00C (CB00),
// BMS map COBIL00.
// The server owns every edit, literal and write. A legacy outcome sent as an error (400, 404, 409
// or 503) rejects the call unchanged; its body also carries current_balance, which the page reads
// through LegacyErrorBody. The apiClient.ts interceptor handles 401 and 403.
import { API_CLIENT, type MoneyAmount } from "@/lib/apiClient";

const BILL_PAY_PATH = "/billpay";

// ACTIDIN (BMS LENGTH 11) and CONFIRM (LENGTH 1) as keyed. Neither is trimmed, padded or
// case-folded, because COBIL00C runs its blank, Y/y, N/n and invalid-value edits on the raw text.
// Both server defaults are blank, so an absent field reaches those edits as a blank field.
export interface BillPayRequest {
    acct_id?: string;
    confirm?: string;
}

// The COBIL00 screen sent back with HTTP 200. FastAPI serialises every field of the response
// model, defaults included, so every key is present. current_balance is CURBAL as exact decimal
// text, null when no account was read or the program cleared the screen; tran_id is null until
// the BILL PAYMENT - ONLINE transaction is written.
export interface BillPayResponse {
    current_balance: MoneyAmount | null;
    message: string;
    cursor_field: string | null;
    tran_id: string | null;
}

// CB00 (COBIL00C): ENTER on the bill payment screen.
export async function PayBill(request: BillPayRequest): Promise<BillPayResponse> {
    const response = await API_CLIENT.post<BillPayResponse>(BILL_PAY_PATH, request);
    return response.data;
}
