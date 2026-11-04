// Mirrors backend/app/schemas/accounts.py and backend/app/api/v1/accounts.py for COACTVWC (CAVW)
// and COACTUPC (CAUP), BMS maps COACTVW and COACTUP.
//
// A request property is optional exactly where its Pydantic field has a default, because the
// server fills it. A response property is required, because the server always serialises every
// field; only the shared ScreenFeedback keys stay optional, as apiClient.ts defines them. Every
// screen field travels as typed, never trimmed, padded, parsed or masked: the server runs the key
// and field edits, the no-change check, the CAUP state machine and the save, and every legacy
// outcome reaches the caller as a rejected call whose body LegacyErrorBody reads; a CAUP outcome's
// body is an AccountScreenErrorResponse, whose screen keys stay optional as its model declares.
import {
    API_CLIENT,
    EncodePathKey,
    type LegacyErrorResponse,
    type MoneyAmount,
    type ReadOptions,
    type ScreenFeedback,
    type UpdateAction,
    type UpdateScreenState,
} from "@/lib/apiClient";

const ACCOUNTS_PATH = "/accounts";
const EDIT_SUFFIX = "/edit";

// The COACTVW screen: one account (CVACT01Y) and its customer (CVCUS01Y), display only. Amounts
// are exact decimal text, dates YYYY-MM-DD text (empty when not stored) and the SSN is masked.
export interface AccountViewResponse extends ScreenFeedback {
    acct_id: string;
    active_status: string;
    open_date: string;
    credit_limit: MoneyAmount;
    expiration_date: string;
    cash_credit_limit: MoneyAmount;
    reissue_date: string;
    curr_balance: MoneyAmount;
    curr_cycle_credit: MoneyAmount;
    group_id: string;
    curr_cycle_debit: MoneyAmount;
    cust_id: string;
    ssn_masked: string;
    date_of_birth: string;
    fico_credit_score: number;
    first_name: string;
    middle_name: string;
    last_name: string;
    addr_line_1: string;
    addr_state_cd: string;
    addr_line_2: string;
    addr_zip: string;
    addr_line_3: string;
    addr_country_cd: string;
    phone_num_1: string;
    govt_issued_id: string;
    phone_num_2: string;
    eft_account_id: string;
    primary_holder_ind: string;
}

// Every COACTUP input as raw 3270 text, in map order (ACCTSID through ACSPFLG). Amounts, dates,
// the SSN parts and the phone parts stay text, so input such as "$1,234.567" reaches the server's
// TEST-NUMVAL-C edit unchanged. The SSN is unmasked here, because COACTUPC edits it.
export interface AccountScreenFields {
    acct_id: string;
    active_status: string;
    open_year: string;
    open_month: string;
    open_day: string;
    credit_limit: string;
    expiration_year: string;
    expiration_month: string;
    expiration_day: string;
    cash_credit_limit: string;
    reissue_year: string;
    reissue_month: string;
    reissue_day: string;
    curr_balance: string;
    curr_cycle_credit: string;
    group_id: string;
    curr_cycle_debit: string;
    cust_id: string;
    ssn_part_1: string;
    ssn_part_2: string;
    ssn_part_3: string;
    dob_year: string;
    dob_month: string;
    dob_day: string;
    fico_credit_score: string;
    first_name: string;
    middle_name: string;
    last_name: string;
    addr_line_1: string;
    addr_state_cd: string;
    addr_line_2: string;
    addr_zip: string;
    addr_line_3: string;
    addr_country_cd: string;
    phone_num_1_area: string;
    phone_num_1_prefix: string;
    phone_num_1_line: string;
    govt_issued_id: string;
    phone_num_2_area: string;
    phone_num_2_prefix: string;
    phone_num_2_line: string;
    eft_account_id: string;
    primary_holder_ind: string;
}

// The COACTUP screen after the edit fetch, ENTER, F5 or F12, on success and on a legacy outcome
// alike. state decides which fields are editable, and screen_token is sent back unchanged on the
// next PUT.
export interface AccountScreen extends ScreenFeedback {
    fields: AccountScreenFields;
    state: UpdateScreenState;
    screen_token: string;
}

// The 400, 404, 409, 423 and 503 body of the edit fetch and the PUT: the legacy error keys plus
// the screen the outcome sends back. The screen keys are optional, as their Pydantic fields have
// defaults, though every such outcome carries them; none is ever null.
export interface AccountScreenErrorResponse extends LegacyErrorResponse {
    fields?: AccountScreenFields;
    state?: UpdateScreenState;
    screen_token?: string;
}

// One ENTER (enter), F5 (save) or F12 (cancel) on COACTUP. The page sends only the inputs its
// state leaves editable; the server takes every protected value from screen_token.
export interface AccountUpdateRequest {
    action?: UpdateAction;
    screen_token?: string;
    fields?: Partial<AccountScreenFields>;
}

// acctId is the ACCTSID text as keyed; the server's key edit answers a blank or invalid one.
function AccountPath(acctId: string): string {
    return `${ACCOUNTS_PATH}/${EncodePathKey(acctId)}`;
}

// CAVW (COACTVWC): ENTER on the account view. options carries the owning read's AbortSignal, as
// it does on every read below; the update is a write and takes none.
export async function ViewAccount(
    acctId: string,
    options?: ReadOptions,
): Promise<AccountViewResponse> {
    const response = await API_CLIENT.get<AccountViewResponse>(AccountPath(acctId), {
        signal: options?.signal,
    });
    return response.data;
}

// CAUP (COACTUPC): ENTER with an account number, fetching it into the update screen.
export async function FetchAccountForEdit(
    acctId: string,
    options?: ReadOptions,
): Promise<AccountScreen> {
    const editPath = AccountPath(acctId) + EDIT_SUFFIX;
    const response = await API_CLIENT.get<AccountScreen>(editPath, { signal: options?.signal });
    return response.data;
}

// CAUP (COACTUPC): ENTER, F5 and F12 on the update screen.
export async function UpdateAccount(
    acctId: string,
    request: AccountUpdateRequest,
): Promise<AccountScreen> {
    const response = await API_CLIENT.put<AccountScreen>(AccountPath(acctId), request);
    return response.data;
}
