"""Integration tests for CB00 bill payment, ``POST /api/v1/billpay`` (COBIL00C, map COBIL00).

Replaces: COBIL00C (CB00), BMS COBIL00, copybooks CVACT01Y, CVTRA05Y and CSDAT01Y.

Every request goes through the ASGI app to the migrated, seeded test database, and the seed is
restored after each test (restoreSeedState of tests/conftest.py). Expected values are typed
from cbl/COBIL00C.cbl and the seed images, where account 00000000004 holds 40.00, its only
cross-reference is card 2988091353094312 and no transaction exists:

- PROCESS-ENTER-KEY (lines 154-244) edits in first-failure mode. A blank ACTIDIN gives
    ``Acct ID can NOT be empty...`` whatever CONFIRM holds; an unknown account
    ``Account ID NOT found...``; a CONFIRM other than Y, y, N, n or blank
    ``Invalid value. Valid values are (Y/N)...``; a balance not above zero
    ``You have nothing to pay...``. A blank CONFIRM prompts with the current balance, and N or
    n clears the screen without reading the account.
- Y or y writes the payment record of lines 218-232: type 02, category 2, POS TERM,
    BILL PAYMENT - ONLINE, the whole balance moved into TRAN-AMT, the cross-reference card,
    merchant 999999999 BILL PAYMENT N/A N/A, and both timestamps the whole-second CSDAT01Y
    WS-TIMESTAMP (format S). Its TRAN-ID is the highest one plus 1 (lines 212-217), and the
    COMPUTE of line 234 rewrites the balance to zero.
- Every failure is the four-key legacy error body; CURBAL travels only on HTTP 200 screens.
- CB00 is a regular-user transaction behind the session and the Origin guard.

The write-durability and concurrency cases of CB00 live in test_durability.py and
test_concurrency.py.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, text

from app.services.billpay import cobil00c

pytestmark = pytest.mark.usefixtures("restoreSeedState")

BILLPAY_URL = "/api/v1/billpay"
HTTP_OK = 200
HTTP_EDIT_FAILURE = 400
HTTP_SIGN_ON_FAILURE = 401
HTTP_ACCESS_REFUSED = 403
HTTP_NOT_FOUND = 404
# Every COBIL00C failure body holds exactly the four standard keys (app.core.errors): CURBAL,
# current_balance, travels only on the screens sent with HTTP 200.
LEGACY_ERROR_KEYS = frozenset({"message", "info_message", "field_errors", "cursor_field"})

# The seed account paid in full (data/EBCDIC ACCTDATA and CARDXREF images).
SAMPLE_ACCOUNT_ID = "00000000004"
SAMPLE_CARD_NUMBER = "2988091353094312"
SEED_BALANCE = Decimal("40.00")
PAID_BALANCE = Decimal("0.00")
UNKNOWN_ACCOUNT_ID = "99999999999"
# GET-CURRENT-TIMESTAMP (lines 249-267) zeroes WS-TIMESTAMP-TM-MS6: whole seconds only.
FROZEN_PAYMENT_TIME = datetime(2022, 7, 18, 10, 15, 30)
CONFIRM_YES = "Y"
# An origin BACKEND_CORS_ORIGINS does not list: another host name of the same server.
FOREIGN_ORIGIN = "http://localhost:3000"

# The WS-MESSAGE literals of lines 161, 361, 237, 187, 201 and 527-530 (two spaces after ".").
ACCT_ID_EMPTY_MESSAGE = "Acct ID can NOT be empty..."
ACCOUNT_NOT_FOUND_MESSAGE = "Account ID NOT found..."
CONFIRM_PAYMENT_MESSAGE = "Confirm to make a bill payment..."
INVALID_CONFIRM_MESSAGE = "Invalid value. Valid values are (Y/N)..."
NOTHING_TO_PAY_MESSAGE = "You have nothing to pay..."
PAYMENT_SUCCESS_MESSAGE = "Payment successful.  Your Transaction ID is {tranId}."
# MOVE -1 TO ACTIDINL and MOVE -1 TO CONFIRML.
CURSOR_ACCT_ID = "acct_id"
CURSOR_CONFIRM = "confirm"

# WS-TRAN-ID-NUM after ADD 1: from an empty TRANSACT, and after an existing highest TRAN-ID.
FIRST_TRAN_ID = "0000000000000001"
EXISTING_TRAN_ID = "0000000000000041"
FOLLOWING_TRAN_ID = "0000000000000042"
EXISTING_TRAN_TYPE_CD = "01"
EXISTING_TRAN_CAT_CD = 1
EXISTING_TRAN_AMOUNT = Decimal("12.34")
EXISTING_MERCHANT_ID = "000000001"

# The payment record of lines 218-232, keyed by transactions.transactions column name.
EXPECTED_PAYMENT_COLUMNS = {
    "tran_type_cd": "02",
    "tran_cat_cd": 2,
    "tran_source": "POS TERM",
    "tran_desc": "BILL PAYMENT - ONLINE",
    "tran_amt": SEED_BALANCE,
    "card_num": SAMPLE_CARD_NUMBER,
    "merchant_id": "999999999",
    "merchant_name": "BILL PAYMENT",
    "merchant_city": "N/A",
    "merchant_zip": "N/A",
    "orig_ts": FROZEN_PAYMENT_TIME,
    "proc_ts": FROZEN_PAYMENT_TIME,
    "orig_ts_format": "S",
    "proc_ts_format": "S",
}
# The screen CLEAR-CURRENT-SCREEN (lines 552-566) sends back for N or n.
CLEARED_SCREEN = {
    "current_balance": None,
    "message": "",
    "cursor_field": CURSOR_ACCT_ID,
    "tran_id": None,
}

# Static SQL with bound values only.
SET_BALANCE_SQL = text(
    "UPDATE accounts.accounts SET curr_balance = :balance WHERE acct_id = :acctId"
)
READ_BALANCE_SQL = text("SELECT curr_balance FROM accounts.accounts WHERE acct_id = :acctId")
FETCH_TRANSACTIONS_SQL = text(
    "SELECT tran_id, tran_type_cd, tran_cat_cd, tran_source, tran_desc, tran_amt, card_num,"
    " merchant_id, merchant_name, merchant_city, merchant_zip, orig_ts, proc_ts,"
    " orig_ts_format, proc_ts_format"
    " FROM transactions.transactions ORDER BY tran_id_sort"
)
INSERT_TRANSACTION_SQL = text(
    "INSERT INTO transactions.transactions"
    " (tran_id, tran_type_cd, tran_cat_cd, tran_amt, merchant_id, card_num)"
    " VALUES (:tranId, :typeCd, :catCd, :amount, :merchantId, :cardNum)"
)


def AssertLegacyError(
    response: httpx.Response, status: int, message: str, cursorField: str = ""
) -> None:
    """Assert the four-key legacy error body of ``status`` with ``message`` on ``cursorField``.

    COBIL00C marks only the field it moves -1 to, so ``field_errors`` names the cursor field
    alone; a refusal with no cursor, as the session and Origin guards send, marks no field.
    """
    body = response.json()
    assert response.status_code == status, response.text
    assert set(body) == LEGACY_ERROR_KEYS
    assert body["message"] == message
    assert body["info_message"] == ""
    assert body["cursor_field"] == cursorField
    assert body["field_errors"] == ([cursorField] if cursorField else [])


async def PayBill(client: httpx.AsyncClient, acctId: str, confirm: str) -> httpx.Response:
    """Press ENTER on COBIL00 with ACTIDIN ``acctId`` and CONFIRM ``confirm`` as keyed."""
    return await client.post(BILLPAY_URL, json={"acct_id": acctId, "confirm": confirm})


def SetBalance(syncEngine: Engine, acctId: str, amount: Decimal) -> None:
    """Commit ``amount`` as ACCT-CURR-BAL of account ``acctId``."""
    with syncEngine.begin() as connection:
        connection.execute(SET_BALANCE_SQL, {"balance": amount, "acctId": acctId})


def ReadBalance(syncEngine: Engine, acctId: str) -> Decimal:
    """Return the committed ACCT-CURR-BAL of account ``acctId``."""
    with syncEngine.connect() as connection:
        return connection.execute(READ_BALANCE_SQL, {"acctId": acctId}).scalar_one()


def FetchTransactions(syncEngine: Engine) -> list[dict[str, Any]]:
    """Return every committed transaction in TRAN-ID order, as the payment record's columns."""
    with syncEngine.connect() as connection:
        result = connection.execute(FETCH_TRANSACTIONS_SQL)
        return [dict(rowMapping) for rowMapping in result.mappings()]


def InsertTransaction(syncEngine: Engine, tranId: str) -> None:
    """Commit a posted transaction keyed ``tranId`` on the sample account's card."""
    transactionValues = {
        "tranId": tranId,
        "typeCd": EXISTING_TRAN_TYPE_CD,
        "catCd": EXISTING_TRAN_CAT_CD,
        "amount": EXISTING_TRAN_AMOUNT,
        "merchantId": EXISTING_MERCHANT_ID,
        "cardNum": SAMPLE_CARD_NUMBER,
    }
    with syncEngine.begin() as connection:
        connection.execute(INSERT_TRANSACTION_SQL, transactionValues)


def AssertNothingWritten(syncEngine: Engine, balance: Decimal = SEED_BALANCE) -> None:
    """Assert that no transaction exists and the sample account still holds ``balance``."""
    assert FetchTransactions(syncEngine) == []
    assert ReadBalance(syncEngine, SAMPLE_ACCOUNT_ID) == balance


def FrozenPaymentTime() -> datetime:
    """Return FROZEN_PAYMENT_TIME, standing in for cobil00c.CurrentTimestamp."""
    return FROZEN_PAYMENT_TIME


async def PostWithOrigin(client: httpx.AsyncClient, origin: str | None) -> httpx.Response:
    """POST a Y payment of the sample account with ``origin`` as Origin, or with none.

    The request keeps the client's session cookie, so only the Origin header differs from a
    payment the guard lets through.
    """
    payBody = {"acct_id": SAMPLE_ACCOUNT_ID, "confirm": CONFIRM_YES}
    request = client.build_request("POST", BILLPAY_URL, json=payBody)
    assert "Cookie" in request.headers
    if origin is None:
        del request.headers["Origin"]
    else:
        request.headers["Origin"] = origin
    return await client.send(request)


@pytest.fixture(name="frozenPaymentClock")
def FrozenPaymentClockFixture(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Freeze GET-CURRENT-TIMESTAMP (cobil00c.CurrentTimestamp) and return the frozen time."""
    monkeypatch.setattr(cobil00c, "CurrentTimestamp", FrozenPaymentTime)
    return FROZEN_PAYMENT_TIME


# --- PROCESS-ENTER-KEY edits and the confirm flow ------------------------------------------------
@pytest.mark.parametrize("acctId", ["", " " * 11], ids=["empty", "spaces"])
async def test_blank_account_id_is_refused_before_the_confirm_value(
    regularClient: httpx.AsyncClient, syncEngine: Engine, acctId: str
) -> None:
    """A blank ACTIDIN fails first (lines 159-164), even with an invalid CONFIRM keyed."""
    response = await PayBill(regularClient, acctId, "X")
    AssertLegacyError(response, HTTP_EDIT_FAILURE, ACCT_ID_EMPTY_MESSAGE, CURSOR_ACCT_ID)
    AssertNothingWritten(syncEngine)


@pytest.mark.parametrize("confirm", ["", CONFIRM_YES], ids=["prompt", "pay"])
async def test_unknown_account_is_not_found(
    regularClient: httpx.AsyncClient, syncEngine: Engine, confirm: str
) -> None:
    """READ-ACCTDAT-FILE NOTFND (lines 359-364) ends the ENTER before any write."""
    response = await PayBill(regularClient, UNKNOWN_ACCOUNT_ID, confirm)
    AssertLegacyError(response, HTTP_NOT_FOUND, ACCOUNT_NOT_FOUND_MESSAGE, CURSOR_ACCT_ID)
    AssertNothingWritten(syncEngine)


@pytest.mark.parametrize("confirm", ["", " "], ids=["empty", "space"])
async def test_blank_confirm_prompts_with_the_current_balance(
    regularClient: httpx.AsyncClient, syncEngine: Engine, confirm: str
) -> None:
    """A blank CONFIRM reads the account and asks for confirmation with CURBAL (lines 236-240)."""
    response = await PayBill(regularClient, SAMPLE_ACCOUNT_ID, confirm)
    body = response.json()
    assert response.status_code == HTTP_OK, response.text
    assert body["message"] == CONFIRM_PAYMENT_MESSAGE
    assert body["cursor_field"] == CURSOR_CONFIRM
    assert Decimal(body["current_balance"]) == SEED_BALANCE
    assert body["tran_id"] is None
    AssertNothingWritten(syncEngine)


@pytest.mark.parametrize("confirm", ["N", "n"])
async def test_confirm_no_clears_the_screen_without_writing(
    regularClient: httpx.AsyncClient, syncEngine: Engine, confirm: str
) -> None:
    """N or n performs CLEAR-CURRENT-SCREEN (lines 178-181): every field and the message blank."""
    response = await PayBill(regularClient, SAMPLE_ACCOUNT_ID, confirm)
    assert response.status_code == HTTP_OK, response.text
    assert response.json() == CLEARED_SCREEN
    AssertNothingWritten(syncEngine)


@pytest.mark.parametrize("confirm", ["X", "1"])
async def test_other_confirm_value_is_invalid(
    regularClient: httpx.AsyncClient, syncEngine: Engine, confirm: str
) -> None:
    """Any CONFIRM but Y, y, N, n or blank is refused on CONFIRM (lines 185-190)."""
    response = await PayBill(regularClient, SAMPLE_ACCOUNT_ID, confirm)
    AssertLegacyError(response, HTTP_EDIT_FAILURE, INVALID_CONFIRM_MESSAGE, CURSOR_CONFIRM)
    AssertNothingWritten(syncEngine)


@pytest.mark.parametrize("confirm", ["", CONFIRM_YES], ids=["prompt", "pay"])
@pytest.mark.parametrize("balance", [PAID_BALANCE, Decimal("-5.00")], ids=["zero", "negative"])
async def test_balance_not_above_zero_has_nothing_to_pay(
    regularClient: httpx.AsyncClient, syncEngine: Engine, balance: Decimal, confirm: str
) -> None:
    """ACCT-CURR-BAL <= ZEROS refuses the prompt and the payment alike (lines 197-205)."""
    SetBalance(syncEngine, SAMPLE_ACCOUNT_ID, balance)
    response = await PayBill(regularClient, SAMPLE_ACCOUNT_ID, confirm)
    AssertLegacyError(response, HTTP_EDIT_FAILURE, NOTHING_TO_PAY_MESSAGE, CURSOR_ACCT_ID)
    AssertNothingWritten(syncEngine, balance)


@pytest.mark.usefixtures("frozenPaymentClock")
@pytest.mark.parametrize("confirm", [CONFIRM_YES, "y"])
async def test_payment_writes_the_bill_payment_record(
    regularClient: httpx.AsyncClient, syncEngine: Engine, confirm: str
) -> None:
    """Y or y pays the whole balance: the record of lines 218-232 and the COMPUTE of line 234.

    The success screen is cleared (INITIALIZE-ALL-FIELDS), so it shows no balance, and no
    response carries the card number written to TRAN-CARD-NUM.
    """
    response = await PayBill(regularClient, SAMPLE_ACCOUNT_ID, confirm)
    body = response.json()
    assert response.status_code == HTTP_OK, response.text
    assert body["message"] == PAYMENT_SUCCESS_MESSAGE.format(tranId=FIRST_TRAN_ID)
    assert body["tran_id"] == FIRST_TRAN_ID
    assert body["current_balance"] is None
    assert body["cursor_field"] == CURSOR_ACCT_ID
    assert SAMPLE_CARD_NUMBER not in response.text
    assert ReadBalance(syncEngine, SAMPLE_ACCOUNT_ID) == PAID_BALANCE
    expectedRecord = {"tran_id": FIRST_TRAN_ID, **EXPECTED_PAYMENT_COLUMNS}
    assert FetchTransactions(syncEngine) == [expectedRecord]


async def test_second_payment_has_nothing_to_pay(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    firstResponse = await PayBill(regularClient, SAMPLE_ACCOUNT_ID, CONFIRM_YES)
    assert firstResponse.status_code == HTTP_OK, firstResponse.text
    secondResponse = await PayBill(regularClient, SAMPLE_ACCOUNT_ID, CONFIRM_YES)
    AssertLegacyError(secondResponse, HTTP_EDIT_FAILURE, NOTHING_TO_PAY_MESSAGE, CURSOR_ACCT_ID)
    paidTranIds = [transaction["tran_id"] for transaction in FetchTransactions(syncEngine)]
    assert paidTranIds == [FIRST_TRAN_ID]
    assert ReadBalance(syncEngine, SAMPLE_ACCOUNT_ID) == PAID_BALANCE


async def test_next_tran_id_follows_the_highest_existing_one(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """READPREV from HIGH-VALUES finds the highest TRAN-ID and ADD 1 gives the next one."""
    InsertTransaction(syncEngine, EXISTING_TRAN_ID)
    response = await PayBill(regularClient, SAMPLE_ACCOUNT_ID, CONFIRM_YES)
    assert response.status_code == HTTP_OK, response.text
    assert response.json()["message"] == PAYMENT_SUCCESS_MESSAGE.format(tranId=FOLLOWING_TRAN_ID)
    assert response.json()["tran_id"] == FOLLOWING_TRAN_ID
    storedTranIds = [transaction["tran_id"] for transaction in FetchTransactions(syncEngine)]
    assert storedTranIds == [EXISTING_TRAN_ID, FOLLOWING_TRAN_ID]


async def test_admin_is_refused_the_regular_transaction(
    adminClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """CB00 belongs to the main menu: user type A gets 403 with an empty message."""
    response = await PayBill(adminClient, SAMPLE_ACCOUNT_ID, CONFIRM_YES)
    AssertLegacyError(response, HTTP_ACCESS_REFUSED, "")
    AssertNothingWritten(syncEngine)


async def test_request_without_a_session_is_refused(
    apiClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """A request with no session cookie gets 401, the EIBCALEN = 0 guard, and writes nothing."""
    response = await PayBill(apiClient, SAMPLE_ACCOUNT_ID, CONFIRM_YES)
    AssertLegacyError(response, HTTP_SIGN_ON_FAILURE, "")
    AssertNothingWritten(syncEngine)


@pytest.mark.parametrize("origin", [None, FOREIGN_ORIGIN], ids=["missing", "foreign"])
async def test_payment_without_an_allow_listed_origin_is_refused(
    regularClient: httpx.AsyncClient, syncEngine: Engine, origin: str | None
) -> None:
    """A signed-on POST without an allow-listed Origin is refused with 403 and writes nothing."""
    response = await PostWithOrigin(regularClient, origin)
    AssertLegacyError(response, HTTP_ACCESS_REFUSED, "")
    AssertNothingWritten(syncEngine)
