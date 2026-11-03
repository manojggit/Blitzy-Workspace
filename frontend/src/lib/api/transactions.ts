// Mirrors backend/app/schemas/transactions.py and backend/app/api/v1/transactions.py for COTRN00C
// (CT00), COTRN01C (CT01) and COTRN02C (CT02), BMS maps COTRN00, COTRN01 and COTRN02.
//
// A property is optional exactly where its Pydantic field has a default, as the OpenAPI document
// marks it. Every screen field travels as typed, never trimmed, padded or case-folded: the server
// runs the search-key edit, the 10-row paging, the S selection, every CT02 first-failure edit, the
// F5 copy of the last transaction and the TRAN-ID allocation on the raw text. CT00 literals arrive
// in a 200 list page; every CT01 and CT02 failure reaches the caller as a rejected call.
import { API_CLIENT, type Direction, type MoneyAmount, type PageState } from "@/lib/apiClient";

const TRANSACTIONS_PATH = "/transactions";
const SELECTION_PATH = `${TRANSACTIONS_PATH}/selection`;

// The CT02 keys (common.TransactionAddAction): ENTER adds, F5 copies the last transaction.
export enum TransactionAddAction {
    ENTER = "enter",
    COPY_LAST = "copy_last",
}

// The GET /transactions query (COTRN00). A search sends searchTranId with no startKey; F7 and F8
// send the shown page's first_key or last_key as startKey, and forward requests carry firstKey.
export interface TransactionListQuery {
    searchTranId?: string;
    startKey?: string;
    firstKey?: string;
    direction?: Direction;
    page?: number;
}

// One COTRN00 row: TRNIDnn, TDATEnn (MM/DD/YY), TDESCnn (26 characters) and TAMTnnn.
export interface TransactionListRow {
    tran_id: string;
    tran_date: string;
    tran_desc: string;
    tran_amt: MoneyAmount;
}

// One COTRN00 page of at most 10 transactions, with its paging position and ERRMSG line.
export interface TransactionListResponse extends PageState {
    rows?: TransactionListRow[];
    message?: string;
    cursor_field?: string | null;
}

// One COTRN00 row as keyed: its TRNIDnn key and its raw SELnnnn code.
export interface TransactionSelection {
    tran_id?: string;
    selection?: string;
}

// The COTRN00 ENTER body: the shown rows with their codes, the search key applied after them, and
// the shown page's first key and number.
export interface TransactionSelectionRequest {
    selections?: TransactionSelection[];
    search_tran_id?: string;
    first_key?: string;
    page?: number;
}

// A route for a valid S selection; null when the list keys carry the re-browsed page.
export interface TransactionSelectionResponse extends TransactionListResponse {
    route?: string | null;
}

// The COTRN01 screen. The card number arrives masked, and both timestamps arrive as their stored
// 26-character text, of which the map shows the first 10 characters.
export interface TransactionDetailResponse {
    tran_id: string;
    card_num_masked: string;
    tran_type_cd: string;
    tran_cat_cd: number;
    tran_source: string;
    tran_desc: string;
    tran_amt: MoneyAmount;
    orig_ts: string;
    proc_ts: string;
    merchant_id: string;
    merchant_name: string;
    merchant_city: string;
    merchant_zip: string;
    message?: string;
}

// The 14 COTRN02 inputs as keyed, amount included (for example +00000050.00). As a response they
// are the screen as COTRN02C leaves it, with the values F5 copied from the last transaction.
export interface TransactionAddFields {
    acct_id?: string;
    card_num?: string;
    type_cd?: string;
    cat_cd?: string;
    source?: string;
    desc?: string;
    amount?: string;
    orig_date?: string;
    proc_date?: string;
    merchant_id?: string;
    merchant_name?: string;
    merchant_city?: string;
    merchant_zip?: string;
    confirm?: string;
}

// The COTRN02 body is flat: the key pressed sits beside the 14 screen fields.
export interface TransactionAddRequest extends TransactionAddFields {
    action?: TransactionAddAction;
}

// The confirm prompt or the added literal; tran_id is set only after a successful add.
export interface TransactionAddResponse {
    message: string;
    cursor_field?: string | null;
    fields?: TransactionAddFields;
    tran_id?: string | null;
}

// An empty segment would reach the list route; a blank TRNIDIN travels as SPACES (%20), so
// COTRN01C answers it with its empty-key literal.
function EncodePathKey(value: string): string {
    return encodeURIComponent(value === "" ? " " : value);
}

// CT00 (COTRN00C): the first entry, a search, F7 and F8 on the transaction list.
export async function ListTransactions(
    query: TransactionListQuery,
): Promise<TransactionListResponse> {
    const response = await API_CLIENT.get<TransactionListResponse>(TRANSACTIONS_PATH, {
        params: query,
    });
    return response.data;
}

// CT00 (COTRN00C): ENTER with selection codes or a search key keyed on the list.
export async function SelectTransactions(
    request: TransactionSelectionRequest,
): Promise<TransactionSelectionResponse> {
    const response = await API_CLIENT.post<TransactionSelectionResponse>(SELECTION_PATH, request);
    return response.data;
}

// CT01 (COTRN01C): ENTER on the view screen, or the arrival from a CT00 S selection.
export async function ViewTransaction(tranId: string): Promise<TransactionDetailResponse> {
    const path = `${TRANSACTIONS_PATH}/${EncodePathKey(tranId)}`;
    const response = await API_CLIENT.get<TransactionDetailResponse>(path);
    return response.data;
}

// CT02 (COTRN02C): ENTER, and F5, which copies the last transaction before the confirm flow.
export async function AddTransaction(
    request: TransactionAddRequest,
): Promise<TransactionAddResponse> {
    const response = await API_CLIENT.post<TransactionAddResponse>(TRANSACTIONS_PATH, request);
    return response.data;
}
