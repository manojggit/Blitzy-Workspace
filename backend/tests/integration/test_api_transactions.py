"""Integration tests of the transaction screens, through the ASGI app against the test database.

Replaces: CT00 / COTRN00C (Transaction List, BMS COTRN00), CT01 / COTRN01C (Transaction View,
BMS COTRN01) and CT02 / COTRN02C (Transaction Add, BMS COTRN02), with copybook CVTRA05Y and the
CSUTLDTC date check COTRN02C calls.

The seed leaves transactions.transactions empty. Tests that need existing rows commit them through
syncEngine (InsertTransactions: the paging fixture and the view, next TRAN-ID and copy_last
cases), confirmed CT02 adds write theirs through POST /transactions, and restoreSeedState empties
the table again at teardown, undoing both. The cases cover:

- CT00 paging: 10 rows per page in key order, F8 and F7, and the five paging literals of
  cbl/COTRN00C.cbl (lines 248 and 270, the STARTBR NOTFND of line 608, the READNEXT and
  READPREV ENDFILE of lines 642 and 676);
- CT00 search and selection: the inclusive STARTBR GTEQ search, the non-numeric key (line 214),
  S or s routing to the view, and an invalid code (line 199) processed before the search;
- CT01: the stored 26-character timestamp texts of the S, D and T forms, the masked card, and the
  blank and unknown IDs (cbl/COTRN01C.cbl lines 149 and 285);
- CT02 in first-failure mode: the key edits, the presence edits in source order, the format and
  CSUTLDTC date edits, the confirm values, the written record and its next TRAN-ID
  (cbl/COTRN02C.cbl lines 164-470, 442-449, 464-465 and 720-745), and F5 copy_last (lines
  471-499);
- the session, role and Origin guards of every route.

Statuses follow the legacy error contract: an edit failure is 400, a NOTFND 404, a role or Origin
refusal 403 and a missing session 401. CT00 answers its paging literals as 200 list pages, while
its two edit failures, the non-numeric search key and an invalid selection code, are 400s. Every
literal is asserted verbatim from the COBOL source rather than imported from the service under
test; the paging literals are also checked against app.core.messages, which COTRN00C and COUSR00C
share.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, NamedTuple

import httpx
import pytest
from sqlalchemy import Engine, insert, select, text

from app.core import messages
from app.models.transaction import Transaction

pytestmark = pytest.mark.usefixtures("restoreSeedState")

TRANSACTIONS_URL = "/api/v1/transactions"
SELECTION_URL = TRANSACTIONS_URL + "/selection"
# The frontend route of the XCTL to COTRN01C (cbl/COTRN00C.cbl lines 188-195).
VIEW_ROUTE_TEMPLATE = "/transactions/view?tranId={tranId}"

HTTP_OK = 200
HTTP_EDIT_FAILURE = 400
HTTP_UNAUTHENTICATED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404

# The four keys of every legacy error body (schemas.common.LegacyErrorResponse), and the page
# keys a CT00 invalid-selection 400 adds to them.
LEGACY_ERROR_KEYS = frozenset({"message", "info_message", "field_errors", "cursor_field"})
LIST_PAGE_KEYS = frozenset({"rows", "page", "first_key", "last_key", "search_cleared"})
LIST_ROW_KEYS = frozenset({"tran_id", "tran_date", "tran_desc", "tran_amt"})
NO_MESSAGE = ""

# Account 00000000004 of data/EBCDIC/AWS.M2.CARDDEMO.ACCTDATA.PS and its only xref card.
SAMPLE_ACCOUNT_ID = "00000000004"
SAMPLE_CARD_NUMBER = "2988091353094312"
MASKED_SAMPLE_CARD = "************4312"
# Keys no seed record holds.
UNKNOWN_ACCOUNT_ID = "99999999999"
UNKNOWN_CARD_NUMBER = "9999999999999999"
UNKNOWN_TRAN_ID = "0000000000000999"

PAGE_SIZE = 10
# 25 TRAN-IDs from 0000000000000010 to 0000000000000250, a step of 10: three pages.
PAGED_TRAN_IDS = tuple(f"{tranNumber:016d}" for tranNumber in range(10, 260, 10))
SELECTED_TRAN_ID = "0000000000000030"
SEARCH_CURSOR_FIELD = "search_tran_id"
# MOVE HIGH-VALUES: the last_key of a search that finds nothing.
HIGH_VALUES_KEY = "\xff" * 16
ALREADY_AT_TOP_TEXT = "You are already at the top of the page..."
ALREADY_AT_BOTTOM_TEXT = "You are already at the bottom of the page..."
AT_TOP_OF_PAGE_TEXT = "You are at the top of the page..."
REACHED_BOTTOM_TEXT = "You have reached the bottom of the page..."
REACHED_TOP_TEXT = "You have reached the top of the page..."
INVALID_SELECTION_TEXT = "Invalid selection. Valid value is S"
TRAN_ID_NOT_NUMERIC_TEXT = "Tran ID must be Numeric ..."

VIEW_CURSOR_FIELD = "tran_id"
TRAN_ID_EMPTY_TEXT = "Tran ID can NOT be empty..."
TRAN_ID_NOT_FOUND_TEXT = "Transaction ID NOT found..."

# --- Timestamps: the text forms of a 26-byte TRAN-ORIG-TS or TRAN-PROC-TS ----------------------
SECONDS_TIMESTAMP_TEXT = "2022-06-10 19:27:53.000000"
DB2_TIMESTAMP_TEXT = "2022-07-18-00.00.00.000000"
DATE_ONLY_TIMESTAMP_TEXT = "2022-07-17" + " " * 16
SECONDS_FORMAT = "S"
DB2_FORMAT = "D"
DATE_ONLY_FORMAT = "T"
# strptime patterns of each form's text with its trailing spaces removed.
TIMESTAMP_PATTERNS = {
    SECONDS_FORMAT: "%Y-%m-%d %H:%M:%S.%f",
    DB2_FORMAT: "%Y-%m-%d-%H.%M.%S.%f",
    DATE_ONLY_FORMAT: "%Y-%m-%d",
}
# TDATEnn MM/DD/YY of SECONDS_TIMESTAMP_TEXT, and the TDESCnn LENGTH=26 cut of a row.
SEED_LIST_DATE = "06/10/22"
LIST_DESCRIPTION_LENGTH = 26
SEED_DESCRIPTION = "INTEGRATION TEST PURCHASE WITH A LONG DESCRIPTION"
SEED_AMOUNT = Decimal("12.34")

ACCOUNT_OR_CARD_REQUIRED_TEXT = "Account or Card Number must be entered..."
ACCOUNT_NOT_NUMERIC_TEXT = "Account ID must be Numeric..."
CARD_NOT_NUMERIC_TEXT = "Card Number must be Numeric..."
ACCOUNT_NOT_FOUND_TEXT = "Account ID NOT found..."
CARD_NOT_FOUND_TEXT = "Card Number NOT found..."
TYPE_NOT_NUMERIC_TEXT = "Type CD must be Numeric..."
CATEGORY_NOT_NUMERIC_TEXT = "Category CD must be Numeric..."
AMOUNT_FORMAT_TEXT = "Amount should be in format -99999999.99"
ORIG_DATE_FORMAT_TEXT = "Orig Date should be in format YYYY-MM-DD"
PROC_DATE_FORMAT_TEXT = "Proc Date should be in format YYYY-MM-DD"
ORIG_DATE_INVALID_TEXT = "Orig Date - Not a valid date..."
PROC_DATE_INVALID_TEXT = "Proc Date - Not a valid date..."
MERCHANT_ID_NOT_NUMERIC_TEXT = "Merchant ID must be Numeric..."
CONFIRM_PROMPT_TEXT = "Confirm to add this transaction..."
CONFIRM_INVALID_TEXT = "Invalid value. Valid values are (Y/N)..."
# The STRING of lines 728-733 leaves two spaces after the period.
ADDED_TEXT_TEMPLATE = "Transaction added successfully.  Your Tran ID is {tranId}."
FIRST_TRAN_ID = "0000000000000001"
TYPE_CD_FIELD = "type_cd"
TYPE_CD_REQUIRED_TEXT = "Type CD can NOT be empty..."

# Lines 251-320: each WHEN of the presence EVALUATE, in source order, with its cursor field.
REQUIRED_FIELD_MESSAGES = (
    (TYPE_CD_FIELD, TYPE_CD_REQUIRED_TEXT),
    ("cat_cd", "Category CD can NOT be empty..."),
    ("source", "Source can NOT be empty..."),
    ("desc", "Description can NOT be empty..."),
    ("amount", "Amount can NOT be empty..."),
    ("orig_date", "Orig Date can NOT be empty..."),
    ("proc_date", "Proc Date can NOT be empty..."),
    ("merchant_id", "Merchant ID can NOT be empty..."),
    ("merchant_name", "Merchant Name can NOT be empty..."),
    ("merchant_city", "Merchant City can NOT be empty..."),
    ("merchant_zip", "Merchant Zip can NOT be empty..."),
)

# A CT02 ENTER body every COTRN02C edit accepts, keyed by account, with confirm Y.
VALID_ADD_FIELDS = {
    "action": "enter",
    "acct_id": SAMPLE_ACCOUNT_ID,
    "card_num": "",
    "type_cd": "01",
    "cat_cd": "0001",
    "source": "POS TERM",
    "desc": "IT PURCHASE",
    "amount": "+00000050.00",
    "orig_date": "2022-07-17",
    "proc_date": "2022-07-17",
    "merchant_id": "000000001",
    "merchant_name": "IT MERCHANT",
    "merchant_city": "IT CITY",
    "merchant_zip": "00000",
    "confirm": "Y",
}
# ADD-TRANSACTION's record from VALID_ADD_FIELDS: the xref card, NUMVAL-C of the amount, and
# both dates as the date-only T form stored as midnight.
EXPECTED_ADDED_RECORD = {
    "tran_id": FIRST_TRAN_ID,
    "tran_type_cd": "01",
    "tran_cat_cd": 1,
    "tran_source": "POS TERM",
    "tran_desc": "IT PURCHASE",
    "tran_amt": Decimal("50.00"),
    "merchant_id": "000000001",
    "merchant_name": "IT MERCHANT",
    "merchant_city": "IT CITY",
    "merchant_zip": "00000",
    "card_num": SAMPLE_CARD_NUMBER,
    "orig_ts": datetime(2022, 7, 17),
    "orig_ts_format": DATE_ONLY_FORMAT,
    "proc_ts": datetime(2022, 7, 17),
    "proc_ts_format": DATE_ONLY_FORMAT,
}

ACCOUNT_BALANCE_QUERY = text("SELECT curr_balance FROM accounts.accounts WHERE acct_id = :acctId")


@dataclass(frozen=True)
class SeedTransaction:
    """One TRAN-RECORD (CVTRA05Y) committed on the sample card, its timestamps as legacy text."""

    tranId: str
    amount: Decimal = SEED_AMOUNT
    origText: str = SECONDS_TIMESTAMP_TEXT
    origFormat: str = SECONDS_FORMAT
    procText: str = DB2_TIMESTAMP_TEXT
    procFormat: str = DB2_FORMAT
    typeCd: str = "01"
    catCd: int = 1
    source: str = "POS TERM"
    description: str = SEED_DESCRIPTION
    merchantId: str = "000000001"
    merchantName: str = "IT SEED MERCHANT"
    merchantCity: str = "IT SEED CITY"
    merchantZip: str = "10001"


# The two records copy_last reads; the highest in key order holds values no other row has.
EARLIER_SEED = SeedTransaction(tranId="0000000000000005")
LAST_SEED = SeedTransaction(
    tranId="0000000000000009",
    typeCd="03",
    catCd=2,
    source="OPERATOR",
    description="COPIED LAST DESCRIPTION",
    merchantId="123456789",
    merchantName="COPY MERCHANT",
    merchantCity="COPY CITY",
    merchantZip="54321",
)
# COPY-LAST-TRAN-DATA's map from LAST_SEED: the category as 4 digits, the amount through
# +99999999.99, each date the first 10 characters of its stored text; the keys stay as edited.
COPIED_SCREEN_FIELDS = {
    "acct_id": SAMPLE_ACCOUNT_ID,
    "card_num": MASKED_SAMPLE_CARD,
    "type_cd": "03",
    "cat_cd": "0002",
    "source": "OPERATOR",
    "desc": "COPIED LAST DESCRIPTION",
    "amount": "+00000012.34",
    "orig_date": "2022-06-10",
    "proc_date": "2022-07-18",
    "merchant_id": "123456789",
    "merchant_name": "COPY MERCHANT",
    "merchant_city": "COPY CITY",
    "merchant_zip": "54321",
    "confirm": "",
}
# The record a confirmed copy_last adds after LAST_SEED.
COPIED_RECORD = {
    "tran_id": "0000000000000010",
    "tran_type_cd": "03",
    "tran_cat_cd": 2,
    "tran_source": "OPERATOR",
    "tran_desc": "COPIED LAST DESCRIPTION",
    "tran_amt": Decimal("12.34"),
    "merchant_id": "123456789",
    "merchant_name": "COPY MERCHANT",
    "merchant_city": "COPY CITY",
    "merchant_zip": "54321",
    "card_num": SAMPLE_CARD_NUMBER,
    "orig_ts": datetime(2022, 6, 10),
    "orig_ts_format": DATE_ONLY_FORMAT,
    "proc_ts": datetime(2022, 7, 18),
    "proc_ts_format": DATE_ONLY_FORMAT,
}


class EditCase(NamedTuple):
    """One CT02 request the first failing COTRN02C edit refuses: its fields and the outcome."""

    overrides: Mapping[str, str]
    status: int
    message: str
    cursorField: str


def ParseTimestamp(timestampText: str, timestampFormat: str) -> datetime:
    """Return the TIMESTAMP(6) value of a 26-character timestamp text of the given form."""
    return datetime.strptime(timestampText.rstrip(" "), TIMESTAMP_PATTERNS[timestampFormat])


def SeedRow(seed: SeedTransaction) -> dict[str, Any]:
    """Return the transactions column values of ``seed``, its timestamps parsed from their text."""
    return {
        "tran_id": seed.tranId,
        "tran_type_cd": seed.typeCd,
        "tran_cat_cd": seed.catCd,
        "tran_source": seed.source,
        "tran_desc": seed.description,
        "tran_amt": seed.amount,
        "merchant_id": seed.merchantId,
        "merchant_name": seed.merchantName,
        "merchant_city": seed.merchantCity,
        "merchant_zip": seed.merchantZip,
        "card_num": SAMPLE_CARD_NUMBER,
        "orig_ts": ParseTimestamp(seed.origText, seed.origFormat),
        "orig_ts_format": seed.origFormat,
        "proc_ts": ParseTimestamp(seed.procText, seed.procFormat),
        "proc_ts_format": seed.procFormat,
    }


def InsertTransactions(syncEngine: Engine, rows: Sequence[SeedTransaction]) -> None:
    """Commit ``rows`` to transactions.transactions in one transaction."""
    with syncEngine.begin() as connection:
        connection.execute(insert(Transaction.__table__), [SeedRow(seed) for seed in rows])


def ReadTransactions(syncEngine: Engine) -> list[dict[str, Any]]:
    """Return every committed transaction as a column-name dict, in TRAN-ID order."""
    statement = select(Transaction.__table__).order_by(Transaction.tran_id)
    with syncEngine.connect() as connection:
        return [dict(rowMapping) for rowMapping in connection.execute(statement).mappings()]


def ReadAccountBalance(syncEngine: Engine, acctId: str) -> Decimal:
    """Return ACCT-CURR-BAL of account ``acctId``."""
    with syncEngine.connect() as connection:
        return connection.execute(ACCOUNT_BALANCE_QUERY, {"acctId": acctId}).scalar_one()


def StoredValues(record: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
    """Return the columns of ``record`` that ``expected`` names, for a whole-dict comparison."""
    return {column: record[column] for column in expected}


def AssertLegacyError(
    response: httpx.Response, status: int, message: str, cursorField: str = ""
) -> dict[str, Any]:
    """Assert a legacy error: ``status``, the literal verbatim and its field; return the body."""
    assert response.status_code == status, response.text
    body = response.json()
    assert LEGACY_ERROR_KEYS <= set(body)
    assert body["message"] == message
    assert body["cursor_field"] == cursorField
    assert body["field_errors"] == ([cursorField] if cursorField else [])
    return body


def AssertNoRawPan(responseText: str) -> None:
    """Assert the response carries the sample card number only masked, never in full."""
    assert SAMPLE_CARD_NUMBER not in responseText


async def ListPage(client: httpx.AsyncClient, query: Mapping[str, Any]) -> dict[str, Any]:
    """GET one CT00 page for ``query``; assert the 200 list response and return its body."""
    response = await client.get(TRANSACTIONS_URL, params=dict(query))
    assert response.status_code == HTTP_OK, response.text
    AssertNoRawPan(response.text)
    return response.json()


def PageIds(listBody: Mapping[str, Any]) -> list[str]:
    """Return the TRAN-IDs of a list body's rows, in screen order."""
    return [row["tran_id"] for row in listBody["rows"]]


def ForwardQuery(shownPage: Mapping[str, Any]) -> dict[str, Any]:
    """Return the F8 query of a shown page: its last key as startKey, its first key and number."""
    return {
        "direction": "forward",
        "startKey": shownPage["last_key"],
        "firstKey": shownPage["first_key"],
        "page": shownPage["page"],
    }


def BackwardQuery(shownPage: Mapping[str, Any]) -> dict[str, Any]:
    """Return the F7 query of a shown page: its first key as startKey and its page number."""
    return {"direction": "backward", "startKey": shownPage["first_key"], "page": shownPage["page"]}


async def PageForward(client: httpx.AsyncClient, pressCount: int) -> dict[str, Any]:
    """Return the page shown after the first entry and ``pressCount`` presses of F8."""
    shownPage = await ListPage(client, {})
    for _ in range(pressCount):
        shownPage = await ListPage(client, ForwardQuery(shownPage))
    return shownPage


def SelectionBody(code: str, selectedId: str, searchTranId: str = "") -> dict[str, Any]:
    """Return ENTER on the first CT00 page with ``code`` keyed on the row of ``selectedId``."""
    selections = [
        {"tran_id": tranId, "selection": code if tranId == selectedId else ""}
        for tranId in PAGED_TRAN_IDS[:PAGE_SIZE]
    ]
    return {
        "selections": selections,
        "search_tran_id": searchTranId,
        "first_key": PAGED_TRAN_IDS[0],
        "page": 1,
    }


def ValidAddBody(**overrides: str) -> dict[str, str]:
    """Return a CT02 body every COTRN02C edit accepts, with ``overrides`` applied."""
    return {**VALID_ADD_FIELDS, **overrides}


def ExpectedPromptFields(overrides: Mapping[str, str]) -> dict[str, str]:
    """Return the map the confirm prompt re-sends: the keys as the xref read resolved them."""
    screenFields = ValidAddBody(**overrides)
    del screenFields["action"]
    screenFields.update(acct_id=SAMPLE_ACCOUNT_ID, card_num=MASKED_SAMPLE_CARD)
    return screenFields


async def AddTransaction(client: httpx.AsyncClient, body: Mapping[str, str]) -> httpx.Response:
    """POST one CT02 request and return its response."""
    return await client.post(TRANSACTIONS_URL, json=dict(body))


@pytest.fixture(name="pagedTransactions")
def PagedTransactionsFixture(syncEngine: Engine) -> tuple[str, ...]:
    """Commit the 25 rows of PAGED_TRAN_IDS and return their TRAN-IDs in key order."""
    InsertTransactions(syncEngine, [SeedTransaction(tranId) for tranId in PAGED_TRAN_IDS])
    return PAGED_TRAN_IDS


async def test_first_page_lists_ten_rows_in_key_order(
    regularClient: httpx.AsyncClient, pagedTransactions: tuple[str, ...]
) -> None:
    firstPage = await ListPage(regularClient, {})
    assert PageIds(firstPage) == list(pagedTransactions[:PAGE_SIZE])
    assert (firstPage["page"], firstPage["message"]) == (1, NO_MESSAGE)
    assert firstPage["cursor_field"] == SEARCH_CURSOR_FIELD
    assert (firstPage["first_key"], firstPage["last_key"]) == (
        pagedTransactions[0],
        pagedTransactions[PAGE_SIZE - 1],
    )
    for row in firstPage["rows"]:
        assert set(row) == LIST_ROW_KEYS
        assert row["tran_date"] == SEED_LIST_DATE
        assert row["tran_desc"] == SEED_DESCRIPTION[:LIST_DESCRIPTION_LENGTH]
        assert Decimal(row["tran_amt"]) == SEED_AMOUNT


async def test_forward_paging_reaches_the_bottom_then_refuses(
    regularClient: httpx.AsyncClient, pagedTransactions: tuple[str, ...]
) -> None:
    secondPage = await PageForward(regularClient, 1)
    assert PageIds(secondPage) == list(pagedTransactions[PAGE_SIZE : 2 * PAGE_SIZE])
    assert (secondPage["page"], secondPage["message"]) == (2, NO_MESSAGE)
    thirdPage = await ListPage(regularClient, ForwardQuery(secondPage))
    assert PageIds(thirdPage) == list(pagedTransactions[2 * PAGE_SIZE :])
    assert (thirdPage["page"], thirdPage["message"]) == (3, REACHED_BOTTOM_TEXT)
    refusedPage = await ListPage(regularClient, ForwardQuery(thirdPage))
    assert PageIds(refusedPage) == list(pagedTransactions[2 * PAGE_SIZE :])
    assert (refusedPage["page"], refusedPage["message"]) == (3, ALREADY_AT_BOTTOM_TEXT)


async def test_backward_paging_from_the_third_page_shows_the_second(
    regularClient: httpx.AsyncClient, pagedTransactions: tuple[str, ...]
) -> None:
    thirdPage = await PageForward(regularClient, 2)
    secondPage = await ListPage(regularClient, BackwardQuery(thirdPage))
    assert PageIds(secondPage) == list(pagedTransactions[PAGE_SIZE : 2 * PAGE_SIZE])
    assert (secondPage["page"], secondPage["message"]) == (2, NO_MESSAGE)


async def test_backward_paging_to_the_first_page_reaches_the_top(
    regularClient: httpx.AsyncClient, pagedTransactions: tuple[str, ...]
) -> None:
    secondPage = await PageForward(regularClient, 1)
    firstPage = await ListPage(regularClient, BackwardQuery(secondPage))
    assert PageIds(firstPage) == list(pagedTransactions[:PAGE_SIZE])
    assert (firstPage["page"], firstPage["message"]) == (1, REACHED_TOP_TEXT)


async def test_backward_paging_on_the_first_page_is_refused(
    regularClient: httpx.AsyncClient, pagedTransactions: tuple[str, ...]
) -> None:
    firstPage = await ListPage(regularClient, {})
    refusedPage = await ListPage(regularClient, BackwardQuery(firstPage))
    assert PageIds(refusedPage) == list(pagedTransactions[:PAGE_SIZE])
    assert (refusedPage["page"], refusedPage["message"]) == (1, ALREADY_AT_TOP_TEXT)


async def test_empty_file_lists_the_top_of_page_literal(regularClient: httpx.AsyncClient) -> None:
    emptyPage = await ListPage(regularClient, {})
    assert (emptyPage["rows"], emptyPage["page"]) == ([], 0)
    assert emptyPage["message"] == AT_TOP_OF_PAGE_TEXT
    assert emptyPage["cursor_field"] == SEARCH_CURSOR_FIELD


@pytest.mark.parametrize(
    ("legacyText", "sharedConstant"),
    [
        pytest.param(ALREADY_AT_TOP_TEXT, messages.ALREADY_AT_TOP_MESSAGE, id="already-top"),
        pytest.param(
            ALREADY_AT_BOTTOM_TEXT, messages.ALREADY_AT_BOTTOM_MESSAGE, id="already-bottom"
        ),
        pytest.param(AT_TOP_OF_PAGE_TEXT, messages.AT_TOP_OF_PAGE_MESSAGE, id="at-top"),
        pytest.param(REACHED_BOTTOM_TEXT, messages.REACHED_BOTTOM_MESSAGE, id="reached-bottom"),
        pytest.param(REACHED_TOP_TEXT, messages.REACHED_TOP_MESSAGE, id="reached-top"),
    ],
)
def test_paging_literal_is_the_shared_constant(legacyText: str, sharedConstant: str) -> None:
    assert sharedConstant == legacyText


@pytest.mark.parametrize(
    ("searchTranId", "expectedFirstId"),
    [
        pytest.param("0000000000000050", "0000000000000050", id="exact-match-inclusive"),
        pytest.param("0000000000000055", "0000000000000060", id="between-keys"),
        pytest.param("", "0000000000000010", id="blank-starts-lowest"),
    ],
)
async def test_search_starts_at_the_first_key_at_or_after_it(
    regularClient: httpx.AsyncClient,
    pagedTransactions: tuple[str, ...],
    searchTranId: str,
    expectedFirstId: str,
) -> None:
    searchPage = await ListPage(regularClient, {"searchTranId": searchTranId})
    firstIndex = pagedTransactions.index(expectedFirstId)
    assert PageIds(searchPage) == list(pagedTransactions[firstIndex : firstIndex + PAGE_SIZE])
    assert (searchPage["page"], searchPage["message"]) == (1, NO_MESSAGE)
    assert searchPage["search_cleared"] is True


async def test_non_numeric_search_is_refused_with_the_list_unchanged(
    regularClient: httpx.AsyncClient, pagedTransactions: tuple[str, ...]
) -> None:
    response = await regularClient.get(TRANSACTIONS_URL, params={"searchTranId": "ABC"})
    refusal = AssertLegacyError(
        response, HTTP_EDIT_FAILURE, TRAN_ID_NOT_NUMERIC_TEXT, SEARCH_CURSOR_FIELD
    )
    assert set(refusal) == LEGACY_ERROR_KEYS


async def test_non_numeric_search_on_an_empty_file_is_the_top_of_page(
    regularClient: httpx.AsyncClient,
) -> None:
    emptyPage = await ListPage(regularClient, {"searchTranId": "ABC"})
    assert (emptyPage["rows"], emptyPage["page"]) == ([], 0)
    assert emptyPage["message"] == AT_TOP_OF_PAGE_TEXT


async def test_search_beyond_every_key_keeps_the_shown_page(
    regularClient: httpx.AsyncClient, pagedTransactions: tuple[str, ...]
) -> None:
    query = {"searchTranId": "9999999999999999", "firstKey": pagedTransactions[0], "page": 1}
    searchPage = await ListPage(regularClient, query)
    assert searchPage["message"] == AT_TOP_OF_PAGE_TEXT
    assert PageIds(searchPage) == list(pagedTransactions[:PAGE_SIZE])
    assert (searchPage["page"], searchPage["last_key"]) == (1, HIGH_VALUES_KEY)


@pytest.mark.parametrize("selectionCode", ["S", "s"])
async def test_selection_s_routes_to_the_transaction_view(
    regularClient: httpx.AsyncClient, pagedTransactions: tuple[str, ...], selectionCode: str
) -> None:
    selectionBody = SelectionBody(selectionCode, SELECTED_TRAN_ID)
    response = await regularClient.post(SELECTION_URL, json=selectionBody)
    assert response.status_code == HTTP_OK, response.text
    assert response.json()["route"] == VIEW_ROUTE_TEMPLATE.format(tranId=SELECTED_TRAN_ID)


async def test_selection_is_processed_before_the_search(
    regularClient: httpx.AsyncClient, pagedTransactions: tuple[str, ...]
) -> None:
    selectionBody = SelectionBody("S", SELECTED_TRAN_ID, pagedTransactions[PAGE_SIZE - 1])
    response = await regularClient.post(SELECTION_URL, json=selectionBody)
    assert response.status_code == HTTP_OK, response.text
    assert response.json()["route"] == VIEW_ROUTE_TEMPLATE.format(tranId=SELECTED_TRAN_ID)


async def test_invalid_selection_continues_with_the_search(
    regularClient: httpx.AsyncClient, pagedTransactions: tuple[str, ...]
) -> None:
    searchIndex = PAGE_SIZE - 1
    selectionBody = SelectionBody("X", SELECTED_TRAN_ID, pagedTransactions[searchIndex])
    response = await regularClient.post(SELECTION_URL, json=selectionBody)
    refusal = AssertLegacyError(
        response, HTTP_EDIT_FAILURE, INVALID_SELECTION_TEXT, SEARCH_CURSOR_FIELD
    )
    assert LIST_PAGE_KEYS <= set(refusal)
    assert PageIds(refusal) == list(pagedTransactions[searchIndex : searchIndex + PAGE_SIZE])
    assert refusal["page"] == 1


def ExpectedDetail(seed: SeedTransaction) -> dict[str, Any]:
    """Return the COTRN01 map of ``seed`` without its amount: masked card, stored texts."""
    return {
        "tran_id": seed.tranId,
        "card_num_masked": MASKED_SAMPLE_CARD,
        "tran_type_cd": seed.typeCd,
        "tran_cat_cd": seed.catCd,
        "tran_source": seed.source,
        "tran_desc": seed.description,
        "orig_ts": seed.origText,
        "proc_ts": seed.procText,
        "merchant_id": seed.merchantId,
        "merchant_name": seed.merchantName,
        "merchant_city": seed.merchantCity,
        "merchant_zip": seed.merchantZip,
        "message": NO_MESSAGE,
    }


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param(SeedTransaction(tranId="0000000000000077"), id="seconds-and-db2-forms"),
        pytest.param(
            SeedTransaction(
                tranId="0000000000000078",
                amount=Decimal("-7.50"),
                origText=DATE_ONLY_TIMESTAMP_TEXT,
                origFormat=DATE_ONLY_FORMAT,
                procText=DATE_ONLY_TIMESTAMP_TEXT,
                procFormat=DATE_ONLY_FORMAT,
            ),
            id="date-only-form",
        ),
    ],
)
async def test_view_shows_the_stored_record(
    regularClient: httpx.AsyncClient, syncEngine: Engine, seed: SeedTransaction
) -> None:
    InsertTransactions(syncEngine, [seed])
    response = await regularClient.get(f"{TRANSACTIONS_URL}/{seed.tranId}")
    assert response.status_code == HTTP_OK, response.text
    AssertNoRawPan(response.text)
    detail = response.json()
    assert Decimal(detail.pop("tran_amt")) == seed.amount
    assert detail == ExpectedDetail(seed)


async def test_view_of_a_blank_id_is_refused(regularClient: httpx.AsyncClient) -> None:
    response = await regularClient.get(f"{TRANSACTIONS_URL}/%20")
    AssertLegacyError(response, HTTP_EDIT_FAILURE, TRAN_ID_EMPTY_TEXT, VIEW_CURSOR_FIELD)


async def test_view_of_an_unknown_id_is_not_found(
    regularClient: httpx.AsyncClient, pagedTransactions: tuple[str, ...]
) -> None:
    response = await regularClient.get(f"{TRANSACTIONS_URL}/{UNKNOWN_TRAN_ID}")
    AssertLegacyError(response, HTTP_NOT_FOUND, TRAN_ID_NOT_FOUND_TEXT, VIEW_CURSOR_FIELD)


KEY_EDIT_CASES = [
    pytest.param(
        EditCase({"acct_id": ""}, HTTP_EDIT_FAILURE, ACCOUNT_OR_CARD_REQUIRED_TEXT, "acct_id"),
        id="no-account-or-card",
    ),
    pytest.param(
        EditCase({"acct_id": "ABC"}, HTTP_EDIT_FAILURE, ACCOUNT_NOT_NUMERIC_TEXT, "acct_id"),
        id="account-not-numeric",
    ),
    pytest.param(
        EditCase(
            {"acct_id": "", "card_num": "ABCD567890123456"},
            HTTP_EDIT_FAILURE,
            CARD_NOT_NUMERIC_TEXT,
            "card_num",
        ),
        id="card-not-numeric",
    ),
    pytest.param(
        EditCase(
            {"acct_id": UNKNOWN_ACCOUNT_ID}, HTTP_NOT_FOUND, ACCOUNT_NOT_FOUND_TEXT, "acct_id"
        ),
        id="account-not-found",
    ),
    pytest.param(
        EditCase(
            {"acct_id": "", "card_num": UNKNOWN_CARD_NUMBER},
            HTTP_NOT_FOUND,
            CARD_NOT_FOUND_TEXT,
            "card_num",
        ),
        id="card-not-found",
    ),
    pytest.param(
        EditCase(
            {"acct_id": "ABC", TYPE_CD_FIELD: ""},
            HTTP_EDIT_FAILURE,
            ACCOUNT_NOT_NUMERIC_TEXT,
            "acct_id",
        ),
        id="key-edit-before-data-edits",
    ),
]
REQUIRED_FIELD_CASES = [
    pytest.param(EditCase({fieldName: ""}, HTTP_EDIT_FAILURE, message, fieldName), id=fieldName)
    for fieldName, message in REQUIRED_FIELD_MESSAGES
] + [
    pytest.param(
        EditCase(
            {TYPE_CD_FIELD: "", "amount": ""},
            HTTP_EDIT_FAILURE,
            TYPE_CD_REQUIRED_TEXT,
            TYPE_CD_FIELD,
        ),
        id="first-blank-field-only",
    ),
]
FORMAT_EDIT_CASES = [
    pytest.param(EditCase(overrides, HTTP_EDIT_FAILURE, message, cursorField), id=caseId)
    for overrides, message, cursorField, caseId in (
        ({TYPE_CD_FIELD: "AB"}, TYPE_NOT_NUMERIC_TEXT, TYPE_CD_FIELD, "type-not-numeric"),
        ({"cat_cd": "ABCD"}, CATEGORY_NOT_NUMERIC_TEXT, "cat_cd", "category-not-numeric"),
        ({"amount": "$1,234.567"}, AMOUNT_FORMAT_TEXT, "amount", "amount-currency-text"),
        ({"amount": "50.00"}, AMOUNT_FORMAT_TEXT, "amount", "amount-unsigned-short"),
        ({"amount": "+0000050.000"}, AMOUNT_FORMAT_TEXT, "amount", "amount-three-decimals"),
        ({"orig_date": "2022/07/17"}, ORIG_DATE_FORMAT_TEXT, "orig_date", "orig-date-format"),
        ({"proc_date": "2022/07/17"}, PROC_DATE_FORMAT_TEXT, "proc_date", "proc-date-format"),
        ({"orig_date": "2022-02-30"}, ORIG_DATE_INVALID_TEXT, "orig_date", "orig-date-invalid"),
        ({"proc_date": "2022-13-01"}, PROC_DATE_INVALID_TEXT, "proc_date", "proc-date-invalid"),
        ({"merchant_id": "ABC"}, MERCHANT_ID_NOT_NUMERIC_TEXT, "merchant_id", "merchant-id"),
        ({"confirm": "X"}, CONFIRM_INVALID_TEXT, "confirm", "confirm-invalid"),
    )
]


@pytest.mark.parametrize("editCase", KEY_EDIT_CASES + REQUIRED_FIELD_CASES + FORMAT_EDIT_CASES)
async def test_first_failing_edit_is_the_only_message(
    regularClient: httpx.AsyncClient, syncEngine: Engine, editCase: EditCase
) -> None:
    response = await AddTransaction(regularClient, ValidAddBody(**editCase.overrides))
    refusal = AssertLegacyError(response, editCase.status, editCase.message, editCase.cursorField)
    assert set(refusal) == LEGACY_ERROR_KEYS
    assert ReadTransactions(syncEngine) == []


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"confirm": ""}, id="confirm-blank"),
        pytest.param({"confirm": "N"}, id="confirm-n"),
        pytest.param({"acct_id": "", "card_num": SAMPLE_CARD_NUMBER, "confirm": ""}, id="by-card"),
    ],
)
async def test_unconfirmed_add_prompts_and_writes_nothing(
    regularClient: httpx.AsyncClient, syncEngine: Engine, overrides: dict[str, str]
) -> None:
    response = await AddTransaction(regularClient, ValidAddBody(**overrides))
    assert response.status_code == HTTP_OK, response.text
    AssertNoRawPan(response.text)
    prompt = response.json()
    assert (prompt["message"], prompt["cursor_field"]) == (CONFIRM_PROMPT_TEXT, "confirm")
    assert prompt["fields"] == ExpectedPromptFields(overrides)
    assert prompt["tran_id"] is None
    assert ReadTransactions(syncEngine) == []


async def test_confirmed_add_reports_the_first_tran_id(regularClient: httpx.AsyncClient) -> None:
    response = await AddTransaction(regularClient, ValidAddBody())
    assert response.status_code == HTTP_OK, response.text
    added = response.json()
    assert added["message"] == ADDED_TEXT_TEMPLATE.format(tranId=FIRST_TRAN_ID)
    assert added["message"] == "Transaction added successfully.  Your Tran ID is 0000000000000001."
    assert (added["tran_id"], added["cursor_field"]) == (FIRST_TRAN_ID, "acct_id")
    assert set(added["fields"].values()) == {""}


async def test_confirmed_add_writes_the_entered_record_without_a_balance_change(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    balanceBefore = ReadAccountBalance(syncEngine, SAMPLE_ACCOUNT_ID)
    response = await AddTransaction(regularClient, ValidAddBody())
    assert response.status_code == HTTP_OK, response.text
    writtenRecords = ReadTransactions(syncEngine)
    assert len(writtenRecords) == 1
    assert StoredValues(writtenRecords[0], EXPECTED_ADDED_RECORD) == EXPECTED_ADDED_RECORD
    assert ReadAccountBalance(syncEngine, SAMPLE_ACCOUNT_ID) == balanceBefore


async def test_next_tran_id_follows_the_highest_existing_one(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    InsertTransactions(
        syncEngine, [SeedTransaction("0000000000000007"), SeedTransaction("0000000000000041")]
    )
    response = await AddTransaction(regularClient, ValidAddBody())
    assert response.status_code == HTTP_OK, response.text
    added = response.json()
    assert added["tran_id"] == "0000000000000042"
    assert added["message"] == ADDED_TEXT_TEMPLATE.format(tranId="0000000000000042")


async def test_copy_last_prompts_with_the_highest_record(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    InsertTransactions(syncEngine, [EARLIER_SEED, LAST_SEED])
    response = await AddTransaction(regularClient, ValidAddBody(action="copy_last", confirm=""))
    assert response.status_code == HTTP_OK, response.text
    AssertNoRawPan(response.text)
    prompt = response.json()
    assert (prompt["message"], prompt["cursor_field"]) == (CONFIRM_PROMPT_TEXT, "confirm")
    assert prompt["fields"] == COPIED_SCREEN_FIELDS
    assert len(ReadTransactions(syncEngine)) == 2


async def test_confirmed_copy_last_adds_the_copied_record(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    InsertTransactions(syncEngine, [EARLIER_SEED, LAST_SEED])
    response = await AddTransaction(regularClient, ValidAddBody(action="copy_last"))
    assert response.status_code == HTTP_OK, response.text
    assert response.json()["tran_id"] == COPIED_RECORD["tran_id"]
    writtenRecords = ReadTransactions(syncEngine)
    assert len(writtenRecords) == 3
    assert StoredValues(writtenRecords[-1], COPIED_RECORD) == COPIED_RECORD


async def test_copy_last_edits_the_keys_before_copying(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    InsertTransactions(syncEngine, [EARLIER_SEED, LAST_SEED])
    copyBody = ValidAddBody(action="copy_last", acct_id=UNKNOWN_ACCOUNT_ID)
    response = await AddTransaction(regularClient, copyBody)
    refusal = AssertLegacyError(response, HTTP_NOT_FOUND, ACCOUNT_NOT_FOUND_TEXT, "acct_id")
    assert set(refusal) == LEGACY_ERROR_KEYS


async def test_copy_last_on_an_empty_file_copies_the_initial_record(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    response = await AddTransaction(regularClient, ValidAddBody(action="copy_last"))
    AssertLegacyError(response, HTTP_EDIT_FAILURE, TYPE_CD_REQUIRED_TEXT, TYPE_CD_FIELD)
    assert ReadTransactions(syncEngine) == []


GUARDED_REQUESTS = [
    pytest.param("GET", TRANSACTIONS_URL, None, id="ct00-list"),
    pytest.param("POST", SELECTION_URL, {"selections": []}, id="ct00-selection"),
    pytest.param("GET", f"{TRANSACTIONS_URL}/{FIRST_TRAN_ID}", None, id="ct01-view"),
    pytest.param("POST", TRANSACTIONS_URL, VALID_ADD_FIELDS, id="ct02-add"),
]


@pytest.mark.parametrize(("method", "path", "body"), GUARDED_REQUESTS)
async def test_admin_is_refused_with_an_empty_message(
    adminClient: httpx.AsyncClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    response = await adminClient.request(method, path, json=body)
    refusal = AssertLegacyError(response, HTTP_FORBIDDEN, NO_MESSAGE)
    assert set(refusal) == LEGACY_ERROR_KEYS


@pytest.mark.parametrize(("method", "path", "body"), GUARDED_REQUESTS)
async def test_anonymous_client_is_refused_without_a_session(
    apiClient: httpx.AsyncClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    response = await apiClient.request(method, path, json=body)
    AssertLegacyError(response, HTTP_UNAUTHENTICATED, NO_MESSAGE)


@pytest.mark.parametrize("originHeader", [None, "http://127.0.0.1:9"], ids=["missing", "foreign"])
async def test_add_without_an_allowed_origin_is_refused(
    regularClient: httpx.AsyncClient, syncEngine: Engine, originHeader: str | None
) -> None:
    request = regularClient.build_request("POST", TRANSACTIONS_URL, json=ValidAddBody())
    if originHeader is None:
        del request.headers["Origin"]
    else:
        request.headers["Origin"] = originHeader
    response = await regularClient.send(request)
    AssertLegacyError(response, HTTP_FORBIDDEN, NO_MESSAGE)
    assert ReadTransactions(syncEngine) == []
