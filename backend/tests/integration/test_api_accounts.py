"""Integration tests of CAVW / COACTVWC (Account View) and CAUP / COACTUPC (Account Update).

Replaces: COACTVWC (CAVW), BMS COACTVW; COACTUPC (CAUP), BMS COACTUP; copybooks CVACT01Y,
CVCUS01Y, CSLKPCDY, CSUTLDPY.

Every case goes through the ASGI app against the test database:

- CAVW, ``GET /api/v1/accounts/{acctId}``: the 2210-EDIT-ACCOUNT key edit, the
  9200-GETCARDXREF-BYACCT not-found builder cut to the 75-byte WS-RETURN-MSG, the values
  1200-SETUP-SCREEN-VARS shows, the masked SSN and the regular-role guard.
- CAUP, ``GET /api/v1/accounts/{acctId}/edit`` and ``PUT /api/v1/accounts/{acctId}``: the action
  state machine of the CAUP contract (Technical Specification, Section 0.6.9) in which ENTER,
  F5 save and F12 cancel act per screen state; the no-change short-circuit; the all-field edit
  mode of 1200-EDIT-MAP-INPUTS; the 3250-SETUP-INFOMSG line of each state; the screen_token as
  the COMMAREA, whose forged or expired form restarts the flow; the token's validated values as
  what F5 writes; and the unmasked SSN parts the edit endpoints alone return.

A blank key answers ``No input received`` in both programs: their cross-field edits (COACTVWC
2200-EDIT-MAP-INPUTS, COACTUPC 1200-EDIT-MAP-INPUTS) set NO-SEARCH-CRITERIA-RECEIVED over the
``Account number not provided`` that 2210 and 1210 set, so that literal never reaches the screen.

Seed customers fail several CAUP edits (customer 000000004 has state MI with ZIP 39035-0455 and a
phone 2 area code of 156), so ``MakeAccountEditable`` commits values passing every edit first.
Test data is committed through ``syncEngine`` and undone by ``restoreSeedState``.
"""

import secrets
import time
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Any

import cryptography.fernet
import httpx
import pytest
from sqlalchemy import Engine, text

from app.schemas.accounts import AccountScreenFields
from app.schemas.common import UpdateAction, UpdateScreenState

# pytest reads a module's marks only under this exact lower-case name.
pytestmark = pytest.mark.usefixtures("restoreSeedState")

ACCOUNTS_URL = "/api/v1/accounts"
EDIT_SUFFIX = "/edit"
HTTP_OK = 200
HTTP_EDIT_FAILURE = 400
HTTP_UNAUTHENTICATED = 401
HTTP_ACCESS_REFUSED = 403
HTTP_NOT_FOUND = 404

# Seeded account 00000000004 (data/EBCDIC/AWS.M2.CARDDEMO.ACCTDATA.PS), with one card.
SAMPLE_ACCOUNT_ID = "00000000004"
UNKNOWN_ACCOUNT_ID = "99999999999"
# A blank ACCTSID: one space, sent as %20 in the path.
BLANK_KEY_TEXT = " "
BLANK_KEY_SEGMENT = "%20"
# Customer 000000001 scores 274, below the 300-850 FICO-RANGE-IS-VALID range.
LOW_FICO_CUSTOMER_ID = "000000001"
LOWEST_VALID_FICO_SCORE = 300
DISALLOWED_ORIGIN = "http://attacker.example"
# The Fernet TTL of screen_token is ACCESS_TOKEN_EXPIRE_MINUTES; the clock moves past it.
SECONDS_PER_MINUTE = 60
TOKEN_EXPIRY_MARGIN_SECONDS = 60
FORGED_TOKEN_BYTES = 80

# The error body of every legacy outcome (schemas.common.LegacyErrorResponse).
LEGACY_ERROR_KEYS = frozenset({"message", "info_message", "field_errors", "cursor_field"})

# schemas.accounts attribute names that field_errors and cursor_field use.
ACCOUNT_ID_FIELD = "acct_id"
STATUS_FIELD = "active_status"
CREDIT_LIMIT_FIELD = "credit_limit"
FICO_FIELD = "fico_credit_score"
FIRST_NAME_FIELD = "first_name"

# cbl/COACTVWC.cbl lines 113-124 and 672: two spaces between "must" and "be".
MSG_NO_INPUT_RECEIVED = "No input received"
MSG_VIEW_KEY_INVALID = "Account Filter must  be a non-zero 11 digit number"
INFO_VIEW_PROMPT = "Enter or update id of account to display"
# 9200-GETCARDXREF-BYACCT NOTFND: the STRING fills the 75-byte WS-RETURN-MSG after four
# characters of ERROR-RESP2, so "000000000 " shows as "0000".
MSG_XREF_NOT_FOUND = "Account:99999999999 not found in Cross ref file.  Resp:000000013  Reas:0000"
# cbl/COACTUPC.cbl: 1210-EDIT-ACCOUNT, 1205-COMPARE-OLD-NEW and the 1200 field edits.
MSG_UPDATE_KEY_INVALID = "Account Number if supplied must be a 11 digit Non-Zero Number"
MSG_NO_CHANGES = "No change detected with respect to values fetched."
MSG_CREDIT_LIMIT_NOT_VALID = "Credit Limit is not valid"
MSG_FICO_OUT_OF_RANGE = "FICO Score: should be between 300 and 850"

# 3250-SETUP-INFOMSG: the information line of each ACUP-CHANGE-ACTION state.
INFO_BY_STATE: Mapping[UpdateScreenState, str] = MappingProxyType(
    {
        UpdateScreenState.DETAILS_NOT_FETCHED: "Enter or update id of account to update",
        UpdateScreenState.SHOW_DETAILS: "Update account details presented above.",
        UpdateScreenState.CHANGES_NOT_OK: "Update account details presented above.",
        UpdateScreenState.CHANGES_OK_NOT_CONFIRMED: "Changes validated.Press F5 to save",
        UpdateScreenState.CHANGES_DONE: "Changes committed to database",
        UpdateScreenState.CHANGES_FAILED: "Changes unsuccessful. Please try again",
    }
)

# Keyed amounts: TEST-NUMVAL-C accepts the currency text and NUMVAL-C stores it truncated into
# S9(10)V99; "abc" fails it; the other two are valid amounts differing from the validated one.
CURRENCY_CREDIT_LIMIT = "$1,234.567"
STORED_CREDIT_LIMIT = Decimal("1234.56")
INVALID_AMOUNT_TEXT = "abc"
OTHER_CREDIT_LIMIT = "9999.00"
LOW_FICO_TEXT = "299"
CHANGED_FIRST_NAME = "Changed"

# Account values passing every COACTUPC edit: status Y, and past dates of century 19 or 20.
VALID_ACCOUNT_VALUES: Mapping[str, object] = MappingProxyType(
    {
        "active_status": "Y",
        "open_date": date(2010, 1, 15),
        "expiration_date": date(2020, 6, 30),
        "reissue_date": date(2018, 3, 1),
    }
)
# Customer values passing every COACTUPC edit. NY10 is a VALID-US-STATE-ZIP-CD2-COMBO and 212 a
# VALID-GENERAL-PURP-CODE (cpy/CSLKPCDY.cpy); the phones keep the seed form (999)999-9999.
VALID_CUSTOMER_VALUES: Mapping[str, object] = MappingProxyType(
    {
        "first_name": "Ada",
        "middle_name": "Maria",
        "last_name": "Lovelace",
        "addr_line_1": "100 Main Street",
        "addr_line_2": "Suite 5",
        "addr_line_3": "New York",
        "addr_state_cd": "NY",
        "addr_zip": "10001",
        "addr_country_cd": "USA",
        "phone_num_1": "(212)555-0100",
        "phone_num_2": "(212)555-0100",
        "ssn": "123456789",
        "date_of_birth": date(1980, 5, 15),
        "fico_credit_score": 700,
        "eft_account_id": "5550001111",
        "primary_holder_ind": "Y",
    }
)
VALID_SSN_PARTS: Mapping[str, str] = MappingProxyType(
    {"ssn_part_1": "123", "ssn_part_2": "45", "ssn_part_3": "6789"}
)

# The five S9(10)V99 amounts, shown on COACTUP as +ZZZ,ZZZ,ZZZ.99.
AMOUNT_FIELDS = (
    "credit_limit",
    "cash_credit_limit",
    "curr_balance",
    "curr_cycle_credit",
    "curr_cycle_debit",
)
# Every other COACTUP field: (stored column, offset, length) of the text 3202 shows in it.
SCREEN_FIELD_SLICES: Mapping[str, tuple[str, int, int]] = MappingProxyType(
    {
        "acct_id": ("acct_id", 0, 11),
        "active_status": ("active_status", 0, 1),
        "open_year": ("open_date", 0, 4),
        "open_month": ("open_date", 5, 2),
        "open_day": ("open_date", 8, 2),
        "expiration_year": ("expiration_date", 0, 4),
        "expiration_month": ("expiration_date", 5, 2),
        "expiration_day": ("expiration_date", 8, 2),
        "reissue_year": ("reissue_date", 0, 4),
        "reissue_month": ("reissue_date", 5, 2),
        "reissue_day": ("reissue_date", 8, 2),
        "group_id": ("group_id", 0, 10),
        "cust_id": ("cust_id", 0, 9),
        "ssn_part_1": ("ssn", 0, 3),
        "ssn_part_2": ("ssn", 3, 2),
        "ssn_part_3": ("ssn", 5, 4),
        "dob_year": ("date_of_birth", 0, 4),
        "dob_month": ("date_of_birth", 5, 2),
        "dob_day": ("date_of_birth", 8, 2),
        "fico_credit_score": ("fico_credit_score", 0, 3),
        "first_name": ("first_name", 0, 25),
        "middle_name": ("middle_name", 0, 25),
        "last_name": ("last_name", 0, 25),
        "addr_line_1": ("addr_line_1", 0, 50),
        "addr_state_cd": ("addr_state_cd", 0, 2),
        "addr_line_2": ("addr_line_2", 0, 50),
        "addr_zip": ("addr_zip", 0, 5),
        "addr_line_3": ("addr_line_3", 0, 50),
        "addr_country_cd": ("addr_country_cd", 0, 3),
        "phone_num_1_area": ("phone_num_1", 1, 3),
        "phone_num_1_prefix": ("phone_num_1", 5, 3),
        "phone_num_1_line": ("phone_num_1", 9, 4),
        "govt_issued_id": ("govt_issued_id", 0, 20),
        "phone_num_2_area": ("phone_num_2", 1, 3),
        "phone_num_2_prefix": ("phone_num_2", 5, 3),
        "phone_num_2_line": ("phone_num_2", 9, 4),
        "eft_account_id": ("eft_account_id", 0, 10),
        "primary_holder_ind": ("primary_holder_ind", 0, 1),
    }
)
# CAVW response keys compared with the stored row: as stored, as ISO dates, and as amounts.
VIEW_TEXT_COLUMNS = (
    "acct_id",
    "active_status",
    "group_id",
    "cust_id",
    "first_name",
    "middle_name",
    "last_name",
    "addr_line_1",
    "addr_state_cd",
    "addr_zip",
    "addr_country_cd",
    "phone_num_1",
    "phone_num_2",
    "eft_account_id",
    "primary_holder_ind",
    "fico_credit_score",
)
VIEW_DATE_COLUMNS = ("open_date", "expiration_date", "reissue_date", "date_of_birth")
# The CAVW text of each amount of seed account SAMPLE_ACCOUNT_ID, as the COACTVW PICOUT
# '+ZZZ,ZZZ,ZZZ.99' shows it after 1200's MOVE (cbl/COACTVWC.cbl lines 475-485).
VIEW_EDITED_AMOUNTS: Mapping[str, str] = MappingProxyType(
    {
        "credit_limit_edited": "+      3,503.00",
        "cash_credit_limit_edited": "+      2,789.00",
        "curr_balance_edited": "+         40.00",
        "curr_cycle_credit_edited": "+           .00",
        "curr_cycle_debit_edited": "+           .00",
    }
)
# WS-RETURN-MSG PIC X(75) (cbl/COACTVWC.cbl line 117).
RETURN_MESSAGE_LENGTH = 75
MASKED_SSN_PREFIX = "***-**-"
SSN_VISIBLE_DIGITS = 4

# SQL with bound values only. The account's customer is the one its cross-reference names, as
# COACTUPC reads CXACAIX before CUSTDAT.
UPDATE_ACCOUNT_SQL = text(
    "UPDATE accounts.accounts SET active_status = :active_status, open_date = :open_date,"
    " expiration_date = :expiration_date, reissue_date = :reissue_date"
    " WHERE acct_id = :acct_id"
)
UPDATE_CUSTOMER_SQL = text(
    "UPDATE customers.customers SET first_name = :first_name, middle_name = :middle_name,"
    " last_name = :last_name, addr_line_1 = :addr_line_1, addr_line_2 = :addr_line_2,"
    " addr_line_3 = :addr_line_3, addr_state_cd = :addr_state_cd, addr_zip = :addr_zip,"
    " addr_country_cd = :addr_country_cd, phone_num_1 = :phone_num_1,"
    " phone_num_2 = :phone_num_2, ssn = :ssn, date_of_birth = :date_of_birth,"
    " fico_credit_score = :fico_credit_score, eft_account_id = :eft_account_id,"
    " primary_holder_ind = :primary_holder_ind"
    " WHERE cust_id IN (SELECT cust_id FROM xref.card_xref WHERE acct_id = :acct_id)"
)
LOW_FICO_ACCOUNT_SQL = text(
    "SELECT xref.acct_id FROM xref.card_xref AS xref"
    " JOIN customers.customers AS customer ON customer.cust_id = xref.cust_id"
    " WHERE xref.cust_id = :custId AND customer.fico_credit_score < :lowestValidScore"
    " ORDER BY xref.card_num LIMIT 1"
)
ACCOUNT_ROW_SQL = text(
    "SELECT account.acct_id, account.active_status, account.curr_balance,"
    " account.credit_limit, account.cash_credit_limit, account.open_date,"
    " account.expiration_date, account.reissue_date, account.curr_cycle_credit,"
    " account.curr_cycle_debit, account.group_id, customer.cust_id, customer.first_name,"
    " customer.middle_name, customer.last_name, customer.addr_line_1, customer.addr_line_2,"
    " customer.addr_line_3, customer.addr_state_cd, customer.addr_zip,"
    " customer.addr_country_cd, customer.phone_num_1, customer.phone_num_2, customer.ssn,"
    " customer.govt_issued_id, customer.date_of_birth, customer.eft_account_id,"
    " customer.primary_holder_ind, customer.fico_credit_score"
    " FROM accounts.accounts AS account"
    " JOIN xref.card_xref AS xref ON xref.acct_id = account.acct_id"
    " JOIN customers.customers AS customer ON customer.cust_id = xref.cust_id"
    " WHERE account.acct_id = :acctId ORDER BY xref.card_num LIMIT 1"
)


@dataclass(frozen=True)
class AccountAction:
    """One key press on the COACTUP screen: ENTER, F5 or F12, the token and the keyed fields."""

    action: UpdateAction = UpdateAction.ENTER
    screenToken: str = ""
    fields: Mapping[str, str] = field(default_factory=dict)

    def Body(self) -> dict[str, object]:
        """Return the AccountUpdateRequest JSON body of this key press."""
        return {
            "action": self.action.value,
            "screen_token": self.screenToken,
            "fields": dict(self.fields),
        }


# --- Test data, committed through syncEngine and undone by restoreSeedState ---------------------
def MakeAccountEditable(syncEngine: Engine, acctId: str) -> str:
    """Commit account and customer values passing every COACTUPC edit; return ``acctId``."""
    with syncEngine.begin() as connection:
        connection.execute(UPDATE_ACCOUNT_SQL, {"acct_id": acctId, **VALID_ACCOUNT_VALUES})
        connection.execute(UPDATE_CUSTOMER_SQL, {"acct_id": acctId, **VALID_CUSTOMER_VALUES})
    return acctId


def FindLowFicoAccount(syncEngine: Engine) -> str:
    """Return the account of customer 000000001, found through the cross-reference.

    The customer's FICO score is below 300, which no ENTER that changes a field can pass.
    """
    parameters = {"custId": LOW_FICO_CUSTOMER_ID, "lowestValidScore": LOWEST_VALID_FICO_SCORE}
    with syncEngine.connect() as connection:
        return connection.execute(LOW_FICO_ACCOUNT_SQL, parameters).scalar_one()


def ReadAccountRow(syncEngine: Engine, acctId: str) -> dict[str, Any]:
    """Return the stored account and its cross-referenced customer as one column dict."""
    with syncEngine.connect() as connection:
        rowMapping = connection.execute(ACCOUNT_ROW_SQL, {"acctId": acctId}).mappings().one()
    return dict(rowMapping)


# --- Expected screen values ----------------------------------------------------------------------
def StoredText(value: object) -> str:
    """Return a stored column as display text: ISO for a date, "" for NULL, no trailing pad."""
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    return str(value).rstrip(" ")


def RowTexts(row: Mapping[str, Any]) -> dict[str, str]:
    """Return every column of ``row`` as text, the FICO score as its three 9(03) digits."""
    rowTexts = {column: StoredText(value) for column, value in row.items()}
    rowTexts[FICO_FIELD] = f"{row[FICO_FIELD]:03d}"
    return rowTexts


def ScreenAmount(screenText: str) -> Decimal:
    """Return the value a +ZZZ,ZZZ,ZZZ.99 amount shows, such as ``+      1,234.56``."""
    return Decimal(screenText.replace(",", "").replace(" ", ""))


def AssertFieldsMatchRow(fields: Mapping[str, str], row: Mapping[str, Any]) -> None:
    """Assert the COACTUP fields show ``row``: every text as sliced, every amount exactly."""
    assert set(fields) == set(AccountScreenFields.model_fields)
    rowTexts = RowTexts(row)
    for fieldName, (column, offset, length) in SCREEN_FIELD_SLICES.items():
        expectedText = rowTexts[column][offset : offset + length].rstrip(" ")
        assert fields[fieldName] == expectedText, fieldName
    for fieldName in AMOUNT_FIELDS:
        assert ScreenAmount(fields[fieldName]) == row[fieldName], fieldName


def MaskedSsn(ssn: str) -> str:
    """Return the SSN as CAVW shows it: ***-**- and its last four digits."""
    return MASKED_SSN_PREFIX + ssn[-SSN_VISIBLE_DIGITS:]


# --- Requests and assertions ---------------------------------------------------------------------
def AssertLegacyError(
    response: httpx.Response, status: int, message: str, cursorField: str = ""
) -> dict[str, Any]:
    """Assert a legacy error: the status, the literal verbatim and, when given, the cursor.

    Returns the body, so callers can check its screen keys as well.
    """
    assert response.status_code == status, response.text
    body = response.json()
    assert LEGACY_ERROR_KEYS <= body.keys()
    assert body["message"] == message
    if cursorField:
        assert body["cursor_field"] == cursorField
    return body


def AssertScreenState(screen: Mapping[str, Any], state: UpdateScreenState) -> None:
    """Assert the screen is in ``state`` and shows that state's 3250 information line."""
    assert screen["state"] == state.value
    assert screen["info_message"] == INFO_BY_STATE[state]


async def FetchForEdit(client: httpx.AsyncClient, acctId: str) -> dict[str, Any]:
    """Return the SHOW_DETAILS screen of ``GET /accounts/{acctId}/edit``."""
    response = await client.get(f"{ACCOUNTS_URL}/{acctId}{EDIT_SUFFIX}")
    assert response.status_code == HTTP_OK, response.text
    screen = response.json()
    AssertScreenState(screen, UpdateScreenState.SHOW_DETAILS)
    return screen


async def PutAccount(
    client: httpx.AsyncClient, acctId: str, request: AccountAction
) -> httpx.Response:
    """Send one COACTUP key press as ``PUT /accounts/{acctId}`` and return the response."""
    return await client.put(f"{ACCOUNTS_URL}/{acctId}", json=request.Body())


def ChangedFields(screen: Mapping[str, Any], overrides: Mapping[str, str]) -> dict[str, str]:
    """Return the screen's fields as shown, with ``overrides`` keyed over them."""
    return {**screen["fields"], **overrides}


def NextAction(
    screen: Mapping[str, Any], action: UpdateAction, overrides: Mapping[str, str]
) -> AccountAction:
    """Return the key press that sends the screen's token back with ``overrides`` keyed."""
    return AccountAction(action, screen["screen_token"], ChangedFields(screen, overrides))


async def ValidateChange(
    client: httpx.AsyncClient, acctId: str, overrides: Mapping[str, str]
) -> dict[str, Any]:
    """Fetch the account, ENTER ``overrides`` and return the CHANGES_OK_NOT_CONFIRMED screen."""
    fetchedScreen = await FetchForEdit(client, acctId)
    response = await PutAccount(
        client, acctId, NextAction(fetchedScreen, UpdateAction.ENTER, overrides)
    )
    assert response.status_code == HTTP_OK, response.text
    confirmScreen = response.json()
    AssertScreenState(confirmScreen, UpdateScreenState.CHANGES_OK_NOT_CONFIRMED)
    return confirmScreen


def RandomToken(validToken: str) -> str:
    """Return a forged screen_token: random URL-safe text the server never issued."""
    assert validToken
    return secrets.token_urlsafe(FORGED_TOKEN_BYTES)


def TamperedToken(validToken: str) -> str:
    """Return ``validToken`` with one character in its middle replaced, breaking its HMAC."""
    position = len(validToken) // 2
    replacement = "B" if validToken[position] == "A" else "A"
    return validToken[:position] + replacement + validToken[position + 1 :]


@dataclass(frozen=True)
class KeyCase:
    """An ACCTSID as sent in the path and keyed in ``fields``, and the literal its edit gives."""

    segment: str
    keyText: str
    message: str = ""


SAMPLE_KEY_CASE = KeyCase(SAMPLE_ACCOUNT_ID, SAMPLE_ACCOUNT_ID)
# 1210-EDIT-ACCOUNT failures before a fetch; the blank key gives the 1200 cross-field literal.
INVALID_KEY_CASES = [
    pytest.param(KeyCase("ABC", "ABC", MSG_UPDATE_KEY_INVALID), id="not-numeric"),
    pytest.param(KeyCase("00000000000", "00000000000", MSG_UPDATE_KEY_INVALID), id="zeros"),
    pytest.param(KeyCase(BLANK_KEY_SEGMENT, BLANK_KEY_TEXT, MSG_NO_INPUT_RECEIVED), id="blank"),
]
EDIT_ENDPOINTS = [pytest.param(True, id="fetch"), pytest.param(False, id="update")]


async def SendEditRequest(
    client: httpx.AsyncClient, keyCase: KeyCase, useFetchEndpoint: bool
) -> httpx.Response:
    """Send the key as ``GET /accounts/{key}/edit`` or as an ENTER ``PUT`` with no token."""
    if useFetchEndpoint:
        return await client.get(f"{ACCOUNTS_URL}/{keyCase.segment}{EDIT_SUFFIX}")
    request = AccountAction(fields={ACCOUNT_ID_FIELD: keyCase.keyText})
    return await PutAccount(client, keyCase.segment, request)


async def SaveChange(
    client: httpx.AsyncClient, acctId: str, overrides: Mapping[str, str]
) -> dict[str, Any]:
    """Validate ``overrides``, press F5 and return the CHANGES_DONE screen."""
    confirmScreen = await ValidateChange(client, acctId, overrides)
    response = await PutAccount(client, acctId, NextAction(confirmScreen, UpdateAction.SAVE, {}))
    assert response.status_code == HTTP_OK, response.text
    doneScreen = response.json()
    AssertScreenState(doneScreen, UpdateScreenState.CHANGES_DONE)
    return doneScreen


# --- CAVW / COACTVWC: GET /api/v1/accounts/{acctId} ----------------------------------------------
async def test_view_shows_account_and_customer_as_stored(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """1200-SETUP-SCREEN-VARS: the account and customer as stored, amounts as exact decimals.

    Each amount also comes as the text of its +ZZZ,ZZZ,ZZZ.99 map field.
    """
    storedRow = ReadAccountRow(syncEngine, SAMPLE_ACCOUNT_ID)
    response = await regularClient.get(f"{ACCOUNTS_URL}/{SAMPLE_ACCOUNT_ID}")
    assert response.status_code == HTTP_OK, response.text
    view = response.json()
    for column in VIEW_TEXT_COLUMNS:
        assert view[column] == storedRow[column], column
    for column in VIEW_DATE_COLUMNS:
        assert view[column] == StoredText(storedRow[column]), column
    for column in AMOUNT_FIELDS:
        assert isinstance(view[column], str), column
        assert Decimal(view[column]) == storedRow[column], column
    for editedKey, editedText in VIEW_EDITED_AMOUNTS.items():
        assert view[editedKey] == editedText, editedKey
    assert view["addr_line_3"] == storedRow["addr_line_3"]
    assert view["message"] == ""
    assert view["info_message"] == INFO_VIEW_PROMPT


async def test_view_masks_ssn_and_never_returns_it(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """CAVW shows the SSN as ***-**- plus its last four digits, and nowhere in full."""
    storedSsn = ReadAccountRow(syncEngine, SAMPLE_ACCOUNT_ID)["ssn"]
    response = await regularClient.get(f"{ACCOUNTS_URL}/{SAMPLE_ACCOUNT_ID}")
    assert response.status_code == HTTP_OK, response.text
    assert response.json()["ssn_masked"] == MaskedSsn(storedSsn)
    assert storedSsn not in response.text
    assert f"{storedSsn[:3]}-{storedSsn[3:5]}-{storedSsn[5:]}" not in response.text
    assert "ssn_part_1" not in response.text


@pytest.mark.parametrize(
    "keySegment",
    [pytest.param("00000000000", id="zeros"), pytest.param("ABC", id="not-numeric")],
)
async def test_view_refuses_key_that_is_not_eleven_nonzero_digits(
    regularClient: httpx.AsyncClient, keySegment: str
) -> None:
    """2210-EDIT-ACCOUNT: a zero or non-numeric key gives the filter literal on ACCTSID."""
    response = await regularClient.get(f"{ACCOUNTS_URL}/{keySegment}")
    body = AssertLegacyError(response, HTTP_EDIT_FAILURE, MSG_VIEW_KEY_INVALID, ACCOUNT_ID_FIELD)
    assert body["field_errors"] == [ACCOUNT_ID_FIELD]
    assert body["info_message"] == INFO_VIEW_PROMPT


async def test_view_answers_no_input_for_blank_key(regularClient: httpx.AsyncClient) -> None:
    """2200-EDIT-MAP-INPUTS: a blank key sets NO-SEARCH-CRITERIA-RECEIVED over 2210's prompt."""
    response = await regularClient.get(f"{ACCOUNTS_URL}/{BLANK_KEY_SEGMENT}")
    body = AssertLegacyError(response, HTTP_EDIT_FAILURE, MSG_NO_INPUT_RECEIVED, ACCOUNT_ID_FIELD)
    assert body["field_errors"] == [ACCOUNT_ID_FIELD]


async def test_view_reports_account_missing_from_cross_reference(
    regularClient: httpx.AsyncClient,
) -> None:
    """9200-GETCARDXREF-BYACCT NOTFND: the builder, cut to the 75-byte WS-RETURN-MSG."""
    response = await regularClient.get(f"{ACCOUNTS_URL}/{UNKNOWN_ACCOUNT_ID}")
    body = AssertLegacyError(response, HTTP_NOT_FOUND, MSG_XREF_NOT_FOUND, ACCOUNT_ID_FIELD)
    assert len(body["message"]) == RETURN_MESSAGE_LENGTH
    assert body["info_message"] == INFO_VIEW_PROMPT


async def test_view_refuses_admin_with_empty_message(adminClient: httpx.AsyncClient) -> None:
    """CAVW belongs to the main menu: a type A user is refused with 403 and no message."""
    response = await adminClient.get(f"{ACCOUNTS_URL}/{SAMPLE_ACCOUNT_ID}")
    AssertLegacyError(response, HTTP_ACCESS_REFUSED, "")


async def test_view_requires_session(apiClient: httpx.AsyncClient) -> None:
    """Without a session cookie CAVW answers 401 and reads nothing."""
    response = await apiClient.get(f"{ACCOUNTS_URL}/{SAMPLE_ACCOUNT_ID}")
    assert response.status_code == HTTP_UNAUTHENTICATED, response.text
    assert "acct_id" not in response.json()


# --- CAUP / COACTUPC: GET /accounts/{acctId}/edit and PUT /accounts/{acctId} -------------------
async def test_fetch_shows_details_of_stored_row(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """GET /edit: SHOW_DETAILS, a screen_token, and every field showing the stored row."""
    acctId = MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    screen = await FetchForEdit(regularClient, acctId)
    assert screen["screen_token"]
    assert screen["message"] == ""
    assert screen["field_errors"] == []
    AssertFieldsMatchRow(screen["fields"], ReadAccountRow(syncEngine, acctId))


@pytest.mark.parametrize("keyCase", INVALID_KEY_CASES)
@pytest.mark.parametrize("useFetchEndpoint", EDIT_ENDPOINTS)
async def test_invalid_key_before_fetch_keeps_details_not_fetched(
    regularClient: httpx.AsyncClient, keyCase: KeyCase, useFetchEndpoint: bool
) -> None:
    """1210-EDIT-ACCOUNT before a fetch: the key literal on ACCTSID, and nothing fetched."""
    response = await SendEditRequest(regularClient, keyCase, useFetchEndpoint)
    body = AssertLegacyError(response, HTTP_EDIT_FAILURE, keyCase.message, ACCOUNT_ID_FIELD)
    AssertScreenState(body, UpdateScreenState.DETAILS_NOT_FETCHED)
    assert body["field_errors"] == [ACCOUNT_ID_FIELD]
    assert body["fields"][FIRST_NAME_FIELD] == ""


async def test_enter_before_fetch_runs_only_key_edit(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """ENTER with no token edits the key and reads; keyed detail fields are not read or edited."""
    acctId = MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    storedRow = ReadAccountRow(syncEngine, acctId)
    keyedFields = {
        ACCOUNT_ID_FIELD: acctId,
        CREDIT_LIMIT_FIELD: INVALID_AMOUNT_TEXT,
        FICO_FIELD: LOW_FICO_TEXT,
    }
    response = await PutAccount(regularClient, acctId, AccountAction(fields=keyedFields))
    assert response.status_code == HTTP_OK, response.text
    screen = response.json()
    AssertScreenState(screen, UpdateScreenState.SHOW_DETAILS)
    assert screen["field_errors"] == []
    AssertFieldsMatchRow(screen["fields"], storedRow)
    assert ReadAccountRow(syncEngine, acctId) == storedRow


@pytest.mark.parametrize("action", [UpdateAction.SAVE, UpdateAction.CANCEL], ids=str)
async def test_save_and_cancel_before_fetch_are_processed_as_enter(
    regularClient: httpx.AsyncClient, syncEngine: Engine, action: UpdateAction
) -> None:
    """F5 and F12 are not valid before details are fetched, so each fetches as ENTER does."""
    acctId = MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    storedRow = ReadAccountRow(syncEngine, acctId)
    keyedFields = {ACCOUNT_ID_FIELD: acctId, CREDIT_LIMIT_FIELD: OTHER_CREDIT_LIMIT}
    response = await PutAccount(regularClient, acctId, AccountAction(action, "", keyedFields))
    assert response.status_code == HTTP_OK, response.text
    screen = response.json()
    AssertScreenState(screen, UpdateScreenState.SHOW_DETAILS)
    AssertFieldsMatchRow(screen["fields"], storedRow)
    assert ReadAccountRow(syncEngine, acctId) == storedRow


async def test_enter_with_unchanged_fields_detects_no_change(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """1205-COMPARE-OLD-NEW finds no change: the literal, the state kept, the cursor on status.

    No field failed, so field_errors is empty and the status is only the cursor.
    """
    acctId = MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    screen = await FetchForEdit(regularClient, acctId)
    response = await PutAccount(regularClient, acctId, NextAction(screen, UpdateAction.ENTER, {}))
    body = AssertLegacyError(response, HTTP_EDIT_FAILURE, MSG_NO_CHANGES, STATUS_FIELD)
    assert body["field_errors"] == []
    AssertScreenState(body, UpdateScreenState.SHOW_DETAILS)
    assert body["fields"] == screen["fields"]
    assert body["screen_token"]


async def test_no_change_short_circuits_edits_of_invalid_stored_values(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """Unchanged, an account whose stored FICO score fails its edit still answers no change."""
    acctId = FindLowFicoAccount(syncEngine)
    screen = await FetchForEdit(regularClient, acctId)
    response = await PutAccount(regularClient, acctId, NextAction(screen, UpdateAction.ENTER, {}))
    body = AssertLegacyError(response, HTTP_EDIT_FAILURE, MSG_NO_CHANGES)
    AssertScreenState(body, UpdateScreenState.SHOW_DETAILS)
    assert FICO_FIELD not in body["field_errors"]


async def test_any_change_runs_fico_edit_on_stored_low_score(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """Changing only the first name runs every edit, so the stored score below 300 fails."""
    acctId = FindLowFicoAccount(syncEngine)
    storedRow = ReadAccountRow(syncEngine, acctId)
    screen = await FetchForEdit(regularClient, acctId)
    request = NextAction(screen, UpdateAction.ENTER, {FIRST_NAME_FIELD: CHANGED_FIRST_NAME})
    response = await PutAccount(regularClient, acctId, request)
    body = AssertLegacyError(response, HTTP_EDIT_FAILURE, MSG_FICO_OUT_OF_RANGE)
    AssertScreenState(body, UpdateScreenState.CHANGES_NOT_OK)
    assert FICO_FIELD in body["field_errors"]
    assert body["cursor_field"] == body["field_errors"][0]
    assert ReadAccountRow(syncEngine, acctId) == storedRow


async def test_all_field_mode_keeps_first_message_and_every_failing_field(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """Every edit runs: the first failure's literal, and both failing fields in source order."""
    acctId = MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    screen = await FetchForEdit(regularClient, acctId)
    overrides = {CREDIT_LIMIT_FIELD: INVALID_AMOUNT_TEXT, FICO_FIELD: LOW_FICO_TEXT}
    response = await PutAccount(
        regularClient, acctId, NextAction(screen, UpdateAction.ENTER, overrides)
    )
    body = AssertLegacyError(
        response, HTTP_EDIT_FAILURE, MSG_CREDIT_LIMIT_NOT_VALID, CREDIT_LIMIT_FIELD
    )
    AssertScreenState(body, UpdateScreenState.CHANGES_NOT_OK)
    assert body["field_errors"] == [CREDIT_LIMIT_FIELD, FICO_FIELD]
    assert body["fields"][CREDIT_LIMIT_FIELD] == INVALID_AMOUNT_TEXT


async def test_valid_change_is_validated_without_writing(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """A change passing every edit gives CHANGES_OK_NOT_CONFIRMED; ENTER never writes."""
    acctId = MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    storedRow = ReadAccountRow(syncEngine, acctId)
    overrides = {CREDIT_LIMIT_FIELD: CURRENCY_CREDIT_LIMIT}
    confirmScreen = await ValidateChange(regularClient, acctId, overrides)
    assert confirmScreen["message"] == ""
    assert confirmScreen["field_errors"] == []
    assert ScreenAmount(confirmScreen["fields"][CREDIT_LIMIT_FIELD]) == STORED_CREDIT_LIMIT
    assert ReadAccountRow(syncEngine, acctId) == storedRow


async def test_enter_while_confirming_keeps_validated_values(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """ENTER in CHANGES_OK_NOT_CONFIRMED runs no edit: every field is protected and kept."""
    acctId = MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    storedRow = ReadAccountRow(syncEngine, acctId)
    confirmScreen = await ValidateChange(
        regularClient, acctId, {CREDIT_LIMIT_FIELD: CURRENCY_CREDIT_LIMIT}
    )
    overrides = {CREDIT_LIMIT_FIELD: OTHER_CREDIT_LIMIT, FICO_FIELD: LOW_FICO_TEXT}
    request = NextAction(confirmScreen, UpdateAction.ENTER, overrides)
    response = await PutAccount(regularClient, acctId, request)
    assert response.status_code == HTTP_OK, response.text
    keptScreen = response.json()
    AssertScreenState(keptScreen, UpdateScreenState.CHANGES_OK_NOT_CONFIRMED)
    assert keptScreen["fields"] == confirmScreen["fields"]
    assert keptScreen["field_errors"] == []
    assert ReadAccountRow(syncEngine, acctId) == storedRow


async def test_save_writes_token_values_not_submitted_fields(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """F5 after validation rewrites the token's values, stored truncated, not the request's."""
    acctId = MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    storedRow = ReadAccountRow(syncEngine, acctId)
    confirmScreen = await ValidateChange(
        regularClient, acctId, {CREDIT_LIMIT_FIELD: CURRENCY_CREDIT_LIMIT}
    )
    overrides = {CREDIT_LIMIT_FIELD: OTHER_CREDIT_LIMIT, FIRST_NAME_FIELD: CHANGED_FIRST_NAME}
    request = NextAction(confirmScreen, UpdateAction.SAVE, overrides)
    response = await PutAccount(regularClient, acctId, request)
    assert response.status_code == HTTP_OK, response.text
    doneScreen = response.json()
    AssertScreenState(doneScreen, UpdateScreenState.CHANGES_DONE)
    assert doneScreen["message"] == ""
    expectedRow = {**storedRow, CREDIT_LIMIT_FIELD: STORED_CREDIT_LIMIT}
    assert ReadAccountRow(syncEngine, acctId) == expectedRow


async def test_save_without_validation_only_validates(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """F5 in SHOW_DETAILS is processed as ENTER: the change is validated, not written."""
    acctId = MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    storedRow = ReadAccountRow(syncEngine, acctId)
    screen = await FetchForEdit(regularClient, acctId)
    request = NextAction(screen, UpdateAction.SAVE, {CREDIT_LIMIT_FIELD: CURRENCY_CREDIT_LIMIT})
    response = await PutAccount(regularClient, acctId, request)
    assert response.status_code == HTTP_OK, response.text
    AssertScreenState(response.json(), UpdateScreenState.CHANGES_OK_NOT_CONFIRMED)
    assert ReadAccountRow(syncEngine, acctId) == storedRow


@pytest.mark.parametrize(
    "validateFirst",
    [pytest.param(False, id="show-details"), pytest.param(True, id="confirming")],
)
async def test_cancel_rereads_record_and_discards_edits(
    regularClient: httpx.AsyncClient, syncEngine: Engine, validateFirst: bool
) -> None:
    """F12 once details are fetched re-reads the record: SHOW_DETAILS with the stored values."""
    acctId = MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    storedRow = ReadAccountRow(syncEngine, acctId)
    overrides = {CREDIT_LIMIT_FIELD: CURRENCY_CREDIT_LIMIT}
    if validateFirst:
        screen = await ValidateChange(regularClient, acctId, overrides)
    else:
        screen = await FetchForEdit(regularClient, acctId)
    request = NextAction(screen, UpdateAction.CANCEL, overrides)
    response = await PutAccount(regularClient, acctId, request)
    assert response.status_code == HTTP_OK, response.text
    AssertScreenState(response.json(), UpdateScreenState.SHOW_DETAILS)
    AssertFieldsMatchRow(response.json()["fields"], storedRow)
    assert ReadAccountRow(syncEngine, acctId) == storedRow


@pytest.mark.parametrize("action", list(UpdateAction), ids=str)
async def test_request_after_changes_done_returns_empty_key_screen(
    regularClient: httpx.AsyncClient, syncEngine: Engine, action: UpdateAction
) -> None:
    """After CHANGES_DONE any key re-initialises: the empty key screen, the input discarded."""
    acctId = MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    doneScreen = await SaveChange(
        regularClient, acctId, {CREDIT_LIMIT_FIELD: CURRENCY_CREDIT_LIMIT}
    )
    savedRow = ReadAccountRow(syncEngine, acctId)
    overrides = {ACCOUNT_ID_FIELD: acctId, FIRST_NAME_FIELD: CHANGED_FIRST_NAME}
    response = await PutAccount(regularClient, acctId, NextAction(doneScreen, action, overrides))
    assert response.status_code == HTTP_OK, response.text
    keyScreen = response.json()
    AssertScreenState(keyScreen, UpdateScreenState.DETAILS_NOT_FETCHED)
    assert keyScreen["message"] == ""
    assert set(keyScreen["fields"].values()) == {""}
    assert ReadAccountRow(syncEngine, acctId) == savedRow


@pytest.mark.parametrize(
    "forgeToken",
    [pytest.param(RandomToken, id="random"), pytest.param(TamperedToken, id="tampered")],
)
async def test_forged_token_restarts_flow_even_on_save(
    regularClient: httpx.AsyncClient, syncEngine: Engine, forgeToken: Callable[[str], str]
) -> None:
    """A token that does not decrypt is DETAILS_NOT_FETCHED: F5 only edits the key and fetches."""
    acctId = MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    storedRow = ReadAccountRow(syncEngine, acctId)
    confirmScreen = await ValidateChange(
        regularClient, acctId, {CREDIT_LIMIT_FIELD: CURRENCY_CREDIT_LIMIT}
    )
    forgedToken = forgeToken(confirmScreen["screen_token"])
    request = AccountAction(UpdateAction.SAVE, forgedToken, confirmScreen["fields"])
    response = await PutAccount(regularClient, acctId, request)
    assert response.status_code == HTTP_OK, response.text
    AssertScreenState(response.json(), UpdateScreenState.SHOW_DETAILS)
    AssertFieldsMatchRow(response.json()["fields"], storedRow)
    assert ReadAccountRow(syncEngine, acctId) == storedRow


async def test_expired_token_restarts_flow_even_on_save(
    regularClient: httpx.AsyncClient,
    syncEngine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    testSettings: Any,
) -> None:
    """A token older than ACCESS_TOKEN_EXPIRE_MINUTES fails its Fernet TTL and restarts the flow."""
    acctId = MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    storedRow = ReadAccountRow(syncEngine, acctId)
    confirmScreen = await ValidateChange(
        regularClient, acctId, {CREDIT_LIMIT_FIELD: CURRENCY_CREDIT_LIMIT}
    )
    ttlSeconds = testSettings.ACCESS_TOKEN_EXPIRE_MINUTES * SECONDS_PER_MINUTE
    expiredAt = time.time() + ttlSeconds + TOKEN_EXPIRY_MARGIN_SECONDS
    # Only the clock cryptography.fernet reads is moved; the session cookie's JWT clock is not.
    monkeypatch.setattr(cryptography.fernet, "time", types.SimpleNamespace(time=lambda: expiredAt))
    request = NextAction(confirmScreen, UpdateAction.SAVE, {})
    response = await PutAccount(regularClient, acctId, request)
    assert response.status_code == HTTP_OK, response.text
    AssertScreenState(response.json(), UpdateScreenState.SHOW_DETAILS)
    assert ReadAccountRow(syncEngine, acctId) == storedRow


async def test_only_edit_endpoints_return_unmasked_ssn_parts(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """COACTUPC edits the SSN, so GET /edit and PUT show its parts; CAVW only masks it."""
    acctId = MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    fetchedScreen = await FetchForEdit(regularClient, acctId)
    confirmScreen = await ValidateChange(
        regularClient, acctId, {CREDIT_LIMIT_FIELD: CURRENCY_CREDIT_LIMIT}
    )
    for screen in (fetchedScreen, confirmScreen):
        shownParts = {name: screen["fields"][name] for name in VALID_SSN_PARTS}
        assert shownParts == dict(VALID_SSN_PARTS)
    viewResponse = await regularClient.get(f"{ACCOUNTS_URL}/{acctId}")
    assert viewResponse.status_code == HTTP_OK, viewResponse.text
    assert viewResponse.json()["ssn_masked"] == MaskedSsn(VALID_CUSTOMER_VALUES["ssn"])
    assert VALID_CUSTOMER_VALUES["ssn"] not in viewResponse.text


@pytest.mark.parametrize("useFetchEndpoint", EDIT_ENDPOINTS)
async def test_edit_endpoints_refuse_admin_with_empty_message(
    adminClient: httpx.AsyncClient, useFetchEndpoint: bool
) -> None:
    """CAUP belongs to the main menu: a type A user is refused with 403 and no message."""
    response = await SendEditRequest(adminClient, SAMPLE_KEY_CASE, useFetchEndpoint)
    AssertLegacyError(response, HTTP_ACCESS_REFUSED, "")


@pytest.mark.parametrize("useFetchEndpoint", EDIT_ENDPOINTS)
async def test_edit_endpoints_require_session(
    apiClient: httpx.AsyncClient, useFetchEndpoint: bool
) -> None:
    """Without a session cookie both CAUP endpoints answer 401."""
    response = await SendEditRequest(apiClient, SAMPLE_KEY_CASE, useFetchEndpoint)
    assert response.status_code == HTTP_UNAUTHENTICATED, response.text


async def test_update_without_allowed_origin_is_refused(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """OriginGuard refuses a cookie-authenticated PUT from another origin, even a valid F5."""
    acctId = MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    storedRow = ReadAccountRow(syncEngine, acctId)
    confirmScreen = await ValidateChange(
        regularClient, acctId, {CREDIT_LIMIT_FIELD: CURRENCY_CREDIT_LIMIT}
    )
    request = NextAction(confirmScreen, UpdateAction.SAVE, {})
    response = await regularClient.put(
        f"{ACCOUNTS_URL}/{acctId}", json=request.Body(), headers={"Origin": DISALLOWED_ORIGIN}
    )
    AssertLegacyError(response, HTTP_ACCESS_REFUSED, "")
    assert ReadAccountRow(syncEngine, acctId) == storedRow
