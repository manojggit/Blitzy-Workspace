// Tests: the read wrappers of lib/api/*.ts (the COACTVWC, COACTUPC, COSGN00C, COCRDLIC, COCRDSLC,
// COCRDUPC, COMEN01C, COADM01C, CORPT00C, COTRN00C, COTRN01C and COUSR00C-COUSR03C reads). Each
// forwards its owner's AbortSignal to axios beside its unchanged query, so a disposed or superseded
// read can be abandoned; a call without options sends no signal.
import { AxiosHeaders, type AxiosResponse } from "axios";

import { FetchAccountForEdit, ViewAccount } from "@/lib/api/accounts";
import { FetchCurrentUser } from "@/lib/api/auth";
import {
    FetchCardForEdit,
    ListCards,
    ViewCard,
    ViewSelectedCard,
    type CardListQuery,
} from "@/lib/api/cards";
import { FetchAdminMenu, FetchMainMenu } from "@/lib/api/menu";
import { FetchTransactionReport, ReportType, type ReportQuery } from "@/lib/api/reports";
import {
    ListTransactions,
    ViewTransaction,
    type TransactionListQuery,
} from "@/lib/api/transactions";
import { FetchUser, ListUsers, UserFetchScreen, type UserListQuery } from "@/lib/api/users";
import { API_CLIENT, Direction, type ReadOptions } from "@/lib/apiClient";

const ACCOUNT_ID = "00000000001";
const CARD_REF = "card-ref-1";
const TYPED_CARD_TEXT = "garbage";
const TRAN_ID = "0000000000000001";
const USER_ID = "TSTUSR01";
const ANSWER_DATA = { answer: "read" };

const CARD_LIST_QUERY: CardListQuery = { acctId: ACCOUNT_ID, direction: Direction.FORWARD };
const REPORT_QUERY: ReportQuery = { reportType: ReportType.MONTHLY, confirm: "Y" };
const TRANSACTION_LIST_QUERY: TransactionListQuery = { searchTranId: TRAN_ID, page: 1 };
const USER_LIST_QUERY: UserListQuery = { searchUserId: USER_ID, direction: Direction.BACKWARD };
// CCLI ENTER may carry a null direction, as the server's Direction | None field allows.
const CARD_ENTER_QUERY: CardListQuery = { acctId: ACCOUNT_ID, direction: null };
// The CCLI list sends its selections as one repeated parameter per row, without the "[]" suffix.
const REPEATED_PARAMS_SERIALIZER = { indexes: null };

interface ReadCase {
    reader: string;
    CallRead: (options?: ReadOptions) => Promise<unknown>;
    path: string;
    params?: object;
    paramsSerializer?: object;
}

// params is absent for a read whose wrapper sends no query, and paramsSerializer for one that
// keeps axios's default query encoding.
const READ_CASES: readonly ReadCase[] = [
    {
        reader: "ViewAccount",
        CallRead: (options) => ViewAccount(ACCOUNT_ID, options),
        path: `/accounts/${ACCOUNT_ID}`,
    },
    {
        reader: "FetchAccountForEdit",
        CallRead: (options) => FetchAccountForEdit(ACCOUNT_ID, options),
        path: `/accounts/${ACCOUNT_ID}/edit`,
    },
    { reader: "FetchCurrentUser", CallRead: FetchCurrentUser, path: "/auth/me" },
    {
        reader: "ListCards",
        CallRead: (options) => ListCards(CARD_LIST_QUERY, options),
        path: "/cards",
        params: CARD_LIST_QUERY,
        paramsSerializer: REPEATED_PARAMS_SERIALIZER,
    },
    // Typed keys declare no card_ref, so the server edits the card text as CARDSID.
    {
        reader: "ViewCard",
        CallRead: (options) => ViewCard(TYPED_CARD_TEXT, ACCOUNT_ID, options),
        path: `/cards/${TYPED_CARD_TEXT}`,
        params: { acctId: ACCOUNT_ID },
    },
    // A card list selection's card_ref is declared, so the server decodes it whatever its length.
    {
        reader: "ViewSelectedCard",
        CallRead: (options) => ViewSelectedCard(CARD_REF, ACCOUNT_ID, options),
        path: `/cards/${CARD_REF}`,
        params: { acctId: ACCOUNT_ID, isCardRef: true },
    },
    {
        reader: "FetchCardForEdit",
        CallRead: (options) => FetchCardForEdit(CARD_REF, ACCOUNT_ID, options),
        path: `/cards/${CARD_REF}/edit`,
        params: { acctId: ACCOUNT_ID, isCardRef: true },
    },
    { reader: "FetchMainMenu", CallRead: FetchMainMenu, path: "/menu/main" },
    { reader: "FetchAdminMenu", CallRead: FetchAdminMenu, path: "/menu/admin" },
    {
        reader: "FetchTransactionReport",
        CallRead: (options) => FetchTransactionReport(REPORT_QUERY, options),
        path: "/reports/transactions",
        params: REPORT_QUERY,
    },
    {
        reader: "ListTransactions",
        CallRead: (options) => ListTransactions(TRANSACTION_LIST_QUERY, options),
        path: "/transactions",
        params: TRANSACTION_LIST_QUERY,
    },
    {
        reader: "ViewTransaction",
        CallRead: (options) => ViewTransaction(TRAN_ID, options),
        path: `/transactions/${TRAN_ID}`,
    },
    {
        reader: "ListUsers",
        CallRead: (options) => ListUsers(USER_LIST_QUERY, options),
        path: "/admin/users",
        params: USER_LIST_QUERY,
    },
    {
        reader: "FetchUser",
        CallRead: (options) => FetchUser(USER_ID, UserFetchScreen.UPDATE, options),
        path: `/admin/users/${USER_ID}`,
        params: { screen: UserFetchScreen.UPDATE },
    },
];

function BuildAnswer(): AxiosResponse {
    return {
        status: 200,
        statusText: "",
        headers: {},
        config: { headers: new AxiosHeaders() },
        data: ANSWER_DATA,
    };
}

// The axios config a read sends: its query and query serializer, if any, beside the signal key,
// always present.
function ExpectedConfig(readCase: ReadCase, signal: AbortSignal | undefined): object {
    const { params, paramsSerializer } = readCase;
    return {
        ...(params === undefined ? {} : { params }),
        ...(paramsSerializer === undefined ? {} : { paramsSerializer }),
        signal,
    };
}

afterEach(() => {
    jest.restoreAllMocks();
});

it.each(READ_CASES)(
    "$reader forwards its owner's AbortSignal beside its unchanged query",
    async (readCase) => {
        const { CallRead, path } = readCase;
        const getSpy = jest.spyOn(API_CLIENT, "get").mockResolvedValue(BuildAnswer());
        const controller = new AbortController();
        await expect(CallRead({ signal: controller.signal })).resolves.toBe(ANSWER_DATA);
        await expect(CallRead()).resolves.toBe(ANSWER_DATA);
        expect(getSpy.mock.calls).toStrictEqual([
            [path, ExpectedConfig(readCase, controller.signal)],
            [path, ExpectedConfig(readCase, undefined)],
        ]);
        expect(getSpy.mock.calls[0]?.[1]?.signal).toBe(controller.signal);
    },
);

it("ListCards forwards a null ENTER direction, which axios leaves out of the URL", async () => {
    const getSpy = jest.spyOn(API_CLIENT, "get").mockResolvedValue(BuildAnswer());
    const query = { params: CARD_ENTER_QUERY, paramsSerializer: REPEATED_PARAMS_SERIALIZER };
    await expect(ListCards(CARD_ENTER_QUERY)).resolves.toBe(ANSWER_DATA);
    expect(getSpy.mock.calls).toStrictEqual([["/cards", { ...query, signal: undefined }]]);
    const requestUri = API_CLIENT.getUri({ url: "/cards", ...query });
    expect(requestUri).toContain(`/cards?acctId=${ACCOUNT_ID}`);
    expect(requestUri).not.toContain("direction");
});
