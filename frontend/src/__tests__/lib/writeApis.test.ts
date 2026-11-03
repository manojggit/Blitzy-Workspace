// Tests: the write wrappers of lib/api/*.ts (the COACTUPC, COSGN00C, COCRDLIC, COCRDUPC, COMEN01C,
// COADM01C, COTRN00C, COTRN02C, COUSR00C-COUSR03C and COBIL00C writes) and the CORPT00C export
// URL. Each write runs through the real API_CLIENT pipeline (base URL, request transform and
// response interceptor) to a capture adapter. It sends its method, path and JSON body unchanged,
// with the session cookie and no AbortSignal; a raw path key travels as EncodePathKey defines;
// the answer's data, or a legacy error, reaches the caller unchanged.
import {
    AxiosError,
    AxiosHeaders,
    type AxiosResponse,
    type InternalAxiosRequestConfig,
} from "axios";

import { UpdateAccount, type AccountUpdateRequest } from "@/lib/api/accounts";
import { Login, Logout, type LoginRequest } from "@/lib/api/auth";
import { PayBill, type BillPayRequest } from "@/lib/api/billpay";
import {
    SelectCards,
    UpdateCard,
    type CardSelectionRequest,
    type CardUpdateRequest,
} from "@/lib/api/cards";
import {
    SelectAdminMenuOption,
    SelectMainMenuOption,
    type MenuSelectionRequest,
} from "@/lib/api/menu";
import {
    BuildReportExportUrl,
    ExportFormat,
    ReportType,
    type ReportQuery,
} from "@/lib/api/reports";
import {
    AddTransaction,
    SelectTransactions,
    TransactionAddAction,
    type TransactionAddRequest,
    type TransactionSelectionRequest,
} from "@/lib/api/transactions";
import {
    AddUser,
    DeleteUser,
    SelectUsers,
    UpdateUser,
    type UserCreateRequest,
    type UserSelectionRequest,
    type UserUpdateRequest,
} from "@/lib/api/users";
import { API_CLIENT, API_V1_URL, UpdateAction } from "@/lib/apiClient";

const ACCOUNT_ID = "00000000001";
const CARD_REF = "card-ref-1";
const TRAN_ID = "0000000000000001";
const USER_ID = "TSTUSR01";
const ANSWER_DATA = { answer: "write" };
const EXPORT_PATH = "/reports/transactions/export";
const HTTP_OK = 200;
const HTTP_BAD_REQUEST = 400;

// Generated per run, so no password value is typed into the code (MK_Modernization Rule).
const generatedPassword = Math.random().toString(36).slice(2, 10);

const ACCOUNT_UPDATE_REQUEST: AccountUpdateRequest = {
    action: UpdateAction.SAVE,
    screen_token: "screen-token-1",
    fields: { credit_limit: "$1,234.567", ssn_part_1: "000", first_name: "Ada " },
};
const LOGIN_REQUEST: LoginRequest = { user_id: "tstusr01", password: generatedPassword };
const CARD_SELECTION_REQUEST: CardSelectionRequest = {
    acct_id: ACCOUNT_ID,
    card_num: "",
    rows: [
        { card_ref: CARD_REF, selection: "S" },
        { card_ref: "card-ref-2", selection: "" },
    ],
};
const CARD_UPDATE_REQUEST: CardUpdateRequest = {
    action: UpdateAction.ENTER,
    screen_token: "screen-token-2",
    fields: { embossed_name: "TEST HOLDER", active_status: "y", expiry_month: "7" },
};
const MAIN_MENU_REQUEST: MenuSelectionRequest = { option: " 7" };
const ADMIN_MENU_REQUEST: MenuSelectionRequest = { option: "4 " };
const TRANSACTION_SELECTION_REQUEST: TransactionSelectionRequest = {
    selections: [
        { tran_id: TRAN_ID, selection: "s" },
        { tran_id: "0000000000000002", selection: "" },
    ],
    search_tran_id: " ",
    first_key: TRAN_ID,
    page: 2,
};
const TRANSACTION_ADD_REQUEST: TransactionAddRequest = {
    action: TransactionAddAction.COPY_LAST,
    acct_id: ACCOUNT_ID,
    card_num: "",
    type_cd: "01",
    cat_cd: "0001",
    source: "POS TERM",
    desc: "TEST PURCHASE",
    amount: "+00000050.00",
    orig_date: "2022-07-17",
    proc_date: "2022-07-17",
    merchant_id: "000000001",
    merchant_name: "TEST MERCHANT",
    merchant_city: "TEST CITY",
    merchant_zip: "00000",
    confirm: "Y",
};
const USER_SELECTION_REQUEST: UserSelectionRequest = {
    selections: [
        { user_id: USER_ID, selection: "U" },
        { user_id: "TSTUSR02", selection: "d" },
    ],
    search_user_id: "",
};
const USER_CREATE_REQUEST: UserCreateRequest = {
    first_name: "Test",
    last_name: "Holder",
    user_id: "TSTUSR03",
    password: generatedPassword,
    user_type: "u",
};
// A blank password keeps the stored hash.
const USER_UPDATE_REQUEST: UserUpdateRequest = {
    first_name: "Test ",
    last_name: "Holder",
    password: "",
    user_type: "A",
};
const BILL_PAY_REQUEST: BillPayRequest = { acct_id: ACCOUNT_ID, confirm: "y" };

interface WriteCase {
    wrapper: string;
    CallWrite: () => Promise<unknown>;
    method: string;
    path: string;
    // Absent for a write that sends no body.
    body?: object;
}

const WRITE_CASES: readonly WriteCase[] = [
    {
        wrapper: "UpdateAccount",
        CallWrite: () => UpdateAccount(ACCOUNT_ID, ACCOUNT_UPDATE_REQUEST),
        method: "put",
        path: `/accounts/${ACCOUNT_ID}`,
        body: ACCOUNT_UPDATE_REQUEST,
    },
    {
        wrapper: "Login",
        CallWrite: () => Login(LOGIN_REQUEST),
        method: "post",
        path: "/auth/login",
        body: LOGIN_REQUEST,
    },
    { wrapper: "Logout", CallWrite: Logout, method: "post", path: "/auth/logout" },
    {
        wrapper: "SelectCards",
        CallWrite: () => SelectCards(CARD_SELECTION_REQUEST),
        method: "post",
        path: "/cards/selection",
        body: CARD_SELECTION_REQUEST,
    },
    {
        wrapper: "UpdateCard",
        CallWrite: () => UpdateCard(CARD_REF, CARD_UPDATE_REQUEST),
        method: "put",
        path: `/cards/${CARD_REF}`,
        body: CARD_UPDATE_REQUEST,
    },
    {
        wrapper: "SelectMainMenuOption",
        CallWrite: () => SelectMainMenuOption(MAIN_MENU_REQUEST),
        method: "post",
        path: "/menu/main/selection",
        body: MAIN_MENU_REQUEST,
    },
    {
        wrapper: "SelectAdminMenuOption",
        CallWrite: () => SelectAdminMenuOption(ADMIN_MENU_REQUEST),
        method: "post",
        path: "/menu/admin/selection",
        body: ADMIN_MENU_REQUEST,
    },
    {
        wrapper: "SelectTransactions",
        CallWrite: () => SelectTransactions(TRANSACTION_SELECTION_REQUEST),
        method: "post",
        path: "/transactions/selection",
        body: TRANSACTION_SELECTION_REQUEST,
    },
    {
        wrapper: "AddTransaction",
        CallWrite: () => AddTransaction(TRANSACTION_ADD_REQUEST),
        method: "post",
        path: "/transactions",
        body: TRANSACTION_ADD_REQUEST,
    },
    {
        wrapper: "SelectUsers",
        CallWrite: () => SelectUsers(USER_SELECTION_REQUEST),
        method: "post",
        path: "/admin/users/selection",
        body: USER_SELECTION_REQUEST,
    },
    {
        wrapper: "AddUser",
        CallWrite: () => AddUser(USER_CREATE_REQUEST),
        method: "post",
        path: "/admin/users",
        body: USER_CREATE_REQUEST,
    },
    {
        wrapper: "UpdateUser",
        CallWrite: () => UpdateUser(USER_ID, USER_UPDATE_REQUEST),
        method: "put",
        path: `/admin/users/${USER_ID}`,
        body: USER_UPDATE_REQUEST,
    },
    {
        wrapper: "DeleteUser",
        CallWrite: () => DeleteUser(USER_ID),
        method: "delete",
        path: `/admin/users/${USER_ID}`,
    },
    {
        wrapper: "PayBill",
        CallWrite: () => PayBill(BILL_PAY_REQUEST),
        method: "post",
        path: "/billpay",
        body: BILL_PAY_REQUEST,
    },
];

interface RawKeySegment {
    rawKey: string;
    segment: string;
}

// EncodePathKey's contract written out: "", "." and ".." gain one trailing space, then every
// character outside the URI component set is percent-encoded.
const RAW_KEY_SEGMENTS: readonly RawKeySegment[] = [
    { rawKey: "", segment: "%20" },
    { rawKey: ".", segment: ".%20" },
    { rawKey: "..", segment: "..%20" },
    { rawKey: "a/b", segment: "a%2Fb" },
    { rawKey: "%", segment: "%25" },
    { rawKey: " 1", segment: "%201" },
];

interface PathKeyWrite {
    wrapper: string;
    CallWithKey: (rawKey: string) => Promise<unknown>;
    method: string;
    collectionPath: string;
}

const PATH_KEY_WRITES: readonly PathKeyWrite[] = [
    {
        wrapper: "UpdateAccount",
        CallWithKey: (rawKey) => UpdateAccount(rawKey, ACCOUNT_UPDATE_REQUEST),
        method: "put",
        collectionPath: "/accounts",
    },
    {
        wrapper: "UpdateCard",
        CallWithKey: (rawKey) => UpdateCard(rawKey, CARD_UPDATE_REQUEST),
        method: "put",
        collectionPath: "/cards",
    },
    {
        wrapper: "UpdateUser",
        CallWithKey: (rawKey) => UpdateUser(rawKey, USER_UPDATE_REQUEST),
        method: "put",
        collectionPath: "/admin/users",
    },
    {
        wrapper: "DeleteUser",
        CallWithKey: DeleteUser,
        method: "delete",
        collectionPath: "/admin/users",
    },
];

// quotedKey names the case, so a blank or space-led key stays visible in the test title.
const PATH_KEY_CASES = PATH_KEY_WRITES.flatMap((pathKeyWrite) =>
    RAW_KEY_SEGMENTS.map((rawKeySegment) => ({
        ...pathKeyWrite,
        ...rawKeySegment,
        quotedKey: JSON.stringify(rawKeySegment.rawKey),
    })),
);

interface ExportQueryCase {
    scenario: string;
    query: ReportQuery;
    // The decoded query the server reads, format excluded, written out as wire text.
    expectedParams: Record<string, string>;
}

const EXPORT_QUERY_CASES: readonly ExportQueryCase[] = [
    {
        scenario: "blank and space-padded parts",
        query: {
            reportType: ReportType.CUSTOM,
            startMm: " 1",
            startDd: "",
            startYyyy: "2022",
            endMm: "12",
            endDd: "3 ",
            endYyyy: "    ",
            confirm: "y",
        },
        expectedParams: {
            reportType: "custom",
            startMm: " 1",
            startDd: "",
            startYyyy: "2022",
            endMm: "12",
            endDd: "3 ",
            endYyyy: "    ",
            confirm: "y",
        },
    },
    {
        scenario: "URL-reserved characters",
        query: {
            reportType: ReportType.YEARLY,
            startMm: "+1",
            startDd: "&",
            startYyyy: "20%2",
            endMm: "=",
            endDd: "#",
            endYyyy: "?/:,",
            confirm: " ",
        },
        expectedParams: {
            reportType: "yearly",
            startMm: "+1",
            startDd: "&",
            startYyyy: "20%2",
            endMm: "=",
            endDd: "#",
            endYyyy: "?/:,",
            confirm: " ",
        },
    },
    {
        scenario: "absent parts",
        query: {
            reportType: ReportType.NONE,
            startMm: undefined,
            endYyyy: "2023",
            confirm: undefined,
        },
        expectedParams: { reportType: "", endYyyy: "2023" },
    },
];

const EXPORT_FORMATS = [
    { format: ExportFormat.CSV, wireFormat: "csv" },
    { format: ExportFormat.PDF, wireFormat: "pdf" },
];

const EXPORT_CASES = EXPORT_QUERY_CASES.flatMap((exportQueryCase) =>
    EXPORT_FORMATS.map((exportFormat) => ({ ...exportQueryCase, ...exportFormat })),
);

// A fresh copy per call, so a body changed in place by the pipeline cannot match itself.
function BuildLegacyErrorBody(): object {
    return {
        message: "Please enter a valid option number...",
        info_message: "",
        field_errors: ["option"],
        cursor_field: "option",
    };
}

function BuildLegacyError(): AxiosError {
    const response: AxiosResponse = {
        status: HTTP_BAD_REQUEST,
        statusText: "Bad Request",
        headers: {},
        config: { headers: new AxiosHeaders() },
        data: BuildLegacyErrorBody(),
    };
    const message = `Request failed with status code ${HTTP_BAD_REQUEST}`;
    return new AxiosError(message, AxiosError.ERR_BAD_REQUEST, undefined, null, response);
}

function AnswerWithSuccess(config: InternalAxiosRequestConfig): Promise<AxiosResponse> {
    return Promise.resolve({
        status: HTTP_OK,
        statusText: "OK",
        headers: {},
        config,
        data: ANSWER_DATA,
    });
}

const originalAdapter = API_CLIENT.defaults.adapter;
const transportAdapter = jest.fn(AnswerWithSuccess);

beforeEach(() => {
    transportAdapter.mockImplementation(AnswerWithSuccess);
    API_CLIENT.defaults.adapter = transportAdapter;
});

afterEach(() => {
    API_CLIENT.defaults.adapter = originalAdapter;
});

// The one request the call handed to the transport, after axios merged and transformed it.
function SentRequest(): InternalAxiosRequestConfig {
    expect(transportAdapter).toHaveBeenCalledTimes(1);
    const sentConfig = transportAdapter.mock.calls[0]?.[0];
    if (sentConfig === undefined) {
        throw new Error("No request reached the transport adapter.");
    }
    return sentConfig;
}

// A body travels as JSON text; a write without one sends no data at all.
function ExpectSentBody(sentConfig: InternalAxiosRequestConfig, body: object | undefined): void {
    if (body === undefined) {
        expect(sentConfig.data).toBeUndefined();
        return;
    }
    expect(sentConfig.headers.get("Content-Type")).toBe("application/json");
    expect(typeof sentConfig.data).toBe("string");
    expect(JSON.parse(sentConfig.data)).toStrictEqual(body);
}

// Key order is not part of the query, so both sides are compared sorted by key; a repeated key
// still shows as an extra entry.
function SortedEntries(entries: Iterable<[string, string]>): [string, string][] {
    return Array.from(entries).sort(([leftKey], [rightKey]) => leftKey.localeCompare(rightKey));
}

it.each(WRITE_CASES)(
    "$wrapper sends $method $path with its exact body or none, the session cookie and no signal",
    async ({ CallWrite, method, path, body }) => {
        await expect(CallWrite()).resolves.toBe(ANSWER_DATA);
        const sentConfig = SentRequest();
        expect(sentConfig.method).toBe(method);
        expect(API_CLIENT.getUri(sentConfig)).toBe(API_V1_URL + path);
        expect(sentConfig.withCredentials).toBe(true);
        expect(sentConfig.signal).toBeUndefined();
        ExpectSentBody(sentConfig, body);
    },
);

it.each(WRITE_CASES)(
    "$wrapper rejects with a legacy 400 error unchanged",
    async ({ CallWrite }) => {
        const legacyError = BuildLegacyError();
        transportAdapter.mockRejectedValue(legacyError);
        await expect(CallWrite()).rejects.toBe(legacyError);
        expect(legacyError.response?.status).toBe(HTTP_BAD_REQUEST);
        expect(legacyError.response?.data).toStrictEqual(BuildLegacyErrorBody());
    },
);

it.each(PATH_KEY_CASES)(
    "$wrapper sends the raw key $quotedKey as the one path segment $segment",
    async ({ CallWithKey, rawKey, method, collectionPath, segment }) => {
        await expect(CallWithKey(rawKey)).resolves.toBe(ANSWER_DATA);
        const sentConfig = SentRequest();
        expect(sentConfig.method).toBe(method);
        expect(API_CLIENT.getUri(sentConfig)).toBe(`${API_V1_URL}${collectionPath}/${segment}`);
    },
);

// The address is compared as exact text, which also fixes its origin and pathname, so the check
// holds whether NEXT_PUBLIC_API_URL names an origin or is unset.
it.each(EXPORT_CASES)(
    "BuildReportExportUrl keeps $scenario unchanged in the $wireFormat export URL",
    ({ query, format, wireFormat, expectedParams }) => {
        const exportUrl = BuildReportExportUrl(query, format);
        const separatorIndex = exportUrl.indexOf("?");
        expect(separatorIndex).toBeGreaterThan(0);
        expect(exportUrl.slice(0, separatorIndex)).toBe(API_V1_URL + EXPORT_PATH);
        const exportParams = new URLSearchParams(exportUrl.slice(separatorIndex + 1));
        const expectedEntries = Object.entries({ ...expectedParams, format: wireFormat });
        expect(SortedEntries(exportParams)).toStrictEqual(SortedEntries(expectedEntries));
        expect(transportAdapter).not.toHaveBeenCalled();
    },
);
