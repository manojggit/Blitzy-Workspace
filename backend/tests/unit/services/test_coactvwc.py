"""Unit tests for app.services.accounts.coactvwc, the port of COACTVWC (transaction CAVW).

COACTVWC shows one account and its customer on map COACTVW. On ENTER, 2200-EDIT-MAP-INPUTS
(cbl/COACTVWC.cbl lines 622-643) treats ACCTSID holding '*' or spaces as not entered, and
2210-EDIT-ACCOUNT (lines 649-685) refuses a blank, non-numeric or all-zero key. The cross-field
edit at lines 640-641 then sets NO-SEARCH-CRITERIA-RECEIVED over the blank-key literal, so a
blank key answers 'No input received'. 9000-READ-ACCT (lines 687-722) reads CXACAIX, ACCTDAT
and CUSTDAT. Their NOTFND branches STRING a message into WS-RETURN-MSG X(75) (lines 745-856),
and their WHEN OTHER branches move WS-FILE-ERROR-MESSAGE (lines 86-105). 1200-SETUP-SCREEN-VARS
(lines 460-535) fills the map and sets the WS-PROMPT-FOR-INPUT information line whenever none
is set; no statement sets WS-INFORM-OUTPUT, so its text is never shown.

Expected values are typed from those lines and from the seed (data/EBCDIC images): account
00000000004 has customer 000000004, card 2988091353094312, balance 40.00 and FICO score 776,
and account 00000000099 does not exist. Each test calls coactvwc.ViewAccount with a regular
user's context against the migrated, seeded test database. CAVW is read-only, so the seed rows
read back unchanged. The account and customer NOTFND outcomes, which the cross-reference
foreign keys make unreachable on real data, and the batch-window refusal are stubbed on the
repository classes.
"""

import json
from collections.abc import Awaitable, Callable
from datetime import date
from decimal import Decimal
from typing import Any, NamedTuple

import pytest
import pytest_asyncio
from sqlalchemy import Engine, text

from app.core.errors import LegacyMessageError
from app.core.locks import FileNotOpenError
from app.repositories.account_repository import AccountRepository
from app.repositories.card_xref_repository import CardXrefRepository
from app.repositories.customer_repository import CustomerRepository
from app.schemas.accounts import AccountViewResponse
from app.services.accounts import coactvwc

REGULAR_USER_TYPE = "U"
# ACCTSID, the map's only input: every failure colours it and 1300 puts the cursor on it.
ACCOUNT_ID_FIELD = "acct_id"
EDIT_FAILURE_STATUS = 400
NOT_FOUND_STATUS = 404
FILE_ERROR_STATUS = 503
# WS-RETURN-MSG PIC X(75) (line 117).
RETURN_MESSAGE_LENGTH = 75

# Line 642, SET NO-SEARCH-CRITERIA-RECEIVED: the final message for a blank or '*' key.
NO_INPUT_MESSAGE = "No input received"
# Line 658, SET WS-PROMPT-FOR-ACCT: set for a blank key, then overwritten at line 642.
ACCOUNT_NOT_PROVIDED_MESSAGE = "Account number not provided"
# Line 673, with two spaces between "must" and "be".
ACCOUNT_FILTER_MESSAGE = "Account Filter must  be a non-zero 11 digit number"
# 88 WS-PROMPT-FOR-INPUT (lines 113-114), set at line 529 for every screen.
PROMPT_INFO = "Enter or update id of account to display"
# 88 WS-INFORM-OUTPUT (lines 115-116): no statement sets it.
INFORM_OUTPUT_INFO = "Displaying details of given Account"
# 88-level WS-RETURN-MSG texts (lines 125-138) that no active statement sets.
NEVER_SET_RETURN_MESSAGES = (
    "Account number must be a non zero 11 digit number",
    "Did not find this account in account card xref file",
    "Did not find this account in account master file",
    "Did not find associated customer in master file",
    "Error reading account card xref File",
)

# The NOTFND STRINGs of 9200, 9300 and 9400 for account 00000000099 and customer 000000099:
# RESP 13 and RESP2 0 moved from S9(09) COMP into X(10), the whole cut at 75 characters.
XREF_NOT_FOUND_99 = "Account:00000000099 not found in Cross ref file.  Resp:000000013  Reas:0000"
ACCOUNT_NOT_FOUND_99 = "Account:00000000099 not found in Acct Master file.Resp:000000013  Reas:0000"
CUSTOMER_NOT_FOUND_99 = (
    "CustId:000000099 not found in customer master.Resp: 000000013  REAS:0000000"
)
# 9200 WHEN OTHER for CICS NOTOPEN (RESP 19, RESP2 60): 'READ' in X(8), CXACAIX in X(9),
# with the trailing blanks of ERROR-RESP2 and the closing FILLER trimmed.
CXACAIX_FILE_NAME = "CXACAIX"
FILE_ERROR_OPERATION = "READ"
CXACAIX_WINDOW_MESSAGE = (
    "File Error: READ     on CXACAIX   returned RESP 000000019 ,RESP2 000000060"
)

# The seed account shown, and its cross-reference record.
VIEWED_ACCT_ID = "00000000004"
VIEWED_CUST_ID = "000000004"
VIEWED_CARD_NUM = "2988091353094312"
SEED_BALANCE = Decimal("40.00")
SEED_BALANCE_JSON = "40.00"
SEED_FICO_SCORE = 776
# An account and customer absent from the seed, with a cross-reference record naming both.
MISSING_ACCT_ID = "00000000099"
MISSING_CUST_ID = "000000099"
MISSING_XREF = {
    "card_num": "4000000000000099",
    "cust_id": MISSING_CUST_ID,
    "acct_id": MISSING_ACCT_ID,
}
SSN_MASK_PREFIX = "***-**-"
SSN_SHOWN_DIGITS = 4

# The CVACT01Y and CVCUS01Y columns 1200-SETUP-SCREEN-VARS moves to COACTVW (lines 471-523).
ACCOUNT_AMOUNT_COLUMNS = (
    "curr_balance",
    "credit_limit",
    "cash_credit_limit",
    "curr_cycle_credit",
    "curr_cycle_debit",
)
ACCOUNT_TEXT_COLUMNS = ("active_status", "group_id")
ACCOUNT_DATE_COLUMNS = ("open_date", "expiration_date", "reissue_date")
CUSTOMER_TEXT_COLUMNS = (
    "cust_id",
    "first_name",
    "middle_name",
    "last_name",
    "addr_line_1",
    "addr_line_2",
    "addr_line_3",
    "addr_state_cd",
    "addr_zip",
    "addr_country_cd",
    "phone_num_1",
    "phone_num_2",
    "govt_issued_id",
    "eft_account_id",
    "primary_holder_ind",
)
# Values COACTVW never carries: the raw SSN, the card number, the CVV and any password.
FORBIDDEN_RESPONSE_FIELDS = frozenset({"ssn", "card_num", "cvv_cd", "password", "password_hash"})

ACCOUNT_ROW_SQL = "SELECT * FROM accounts.accounts WHERE acct_id = :key"
CUSTOMER_ROW_SQL = "SELECT * FROM customers.customers WHERE cust_id = :key"
# xmin changes with every row version, so an equal xmin means the row was never rewritten.
ACCOUNT_VERSION_SQL = (
    "SELECT xmin::text AS row_version, * FROM accounts.accounts WHERE acct_id = :key"
)
CUSTOMER_VERSION_SQL = (
    "SELECT xmin::text AS row_version, * FROM customers.customers WHERE cust_id = :key"
)

# 2200: '*' or spaces mean not entered. ACCTSID arrives as typed or space-padded to 11.
BLANK_KEYS = ["", " " * 11, "*", "*" + " " * 10]
BLANK_KEY_IDS = ["empty", "spaces", "star", "padded-star"]
# 2210: NOT NUMERIC (letters, a short key, a leading space before '*') or EQUAL ZEROES.
INVALID_KEYS = ["abc", "123", "00000000000", "0000000004a", " *"]
INVALID_KEY_IDS = ["letters", "short", "zeros", "trailing-letter", "space-then-star"]


class StubbedReadCase(NamedTuple):
    """A read chain whose ACCTDAT or CUSTDAT record is missing, and the message it leaves."""

    install: Callable[[pytest.MonkeyPatch, dict[str, Any]], None]
    message: str


def ReadRecord(syncEngine: Engine, sql: str, key: str) -> dict[str, Any]:
    """Return the one committed row ``sql`` selects for ``key``, keyed by column name."""
    with syncEngine.connect() as connection:
        return dict(connection.execute(text(sql), {"key": key}).mappings().one())


def DisplayedDate(value: date | None) -> str:
    """Return a DATE column as the YYYY-MM-DD text the map shows, or "" for a blank date."""
    if value is None:
        return ""
    return value.isoformat()


def ExpectedErrorBody(message: str) -> dict[str, object]:
    """The COACTVW error body: the message, the input prompt, and ACCTSID failing and cursor."""
    return {
        "message": message,
        "info_message": PROMPT_INFO,
        "field_errors": [ACCOUNT_ID_FIELD],
        "cursor_field": ACCOUNT_ID_FIELD,
    }


async def RaisedViewError(context: Any, acctIdText: str) -> LegacyMessageError:
    """Run the CAVW ENTER path for ``acctIdText`` and return the screen error it raises."""
    with pytest.raises(LegacyMessageError) as excInfo:
        await coactvwc.ViewAccount(context, acctIdText)
    return excInfo.value


def StubReturning(record: dict[str, Any] | None) -> Callable[..., Awaitable[Any]]:
    """Return a repository read stub that answers every key with ``record`` (None is NOTFND)."""

    async def Stub(self: Any, key: str) -> dict[str, Any] | None:
        return record

    return Stub


async def StubXrefWindowClosed(self: Any, acctId: str) -> dict[str, Any] | None:
    """Refuse the CXACAIX read as the batch window does: CICS NOTOPEN, RESP 19, RESP2 60."""
    raise FileNotOpenError(CXACAIX_FILE_NAME)


def InstallAccountMissing(monkeypatch: pytest.MonkeyPatch, seedAccountRow: dict[str, Any]) -> None:
    """Find the cross-reference of account 99, then answer the ACCTDAT read with NOTFND."""
    monkeypatch.setattr(CardXrefRepository, "GetXrefByAccount", StubReturning(MISSING_XREF))
    monkeypatch.setattr(AccountRepository, "GetAccount", StubReturning(None))


def InstallCustomerMissing(monkeypatch: pytest.MonkeyPatch, seedAccountRow: dict[str, Any]) -> None:
    """Find account 99 through account 4's record, then answer the CUSTDAT read with NOTFND."""
    accountRecord = {**seedAccountRow, "acct_id": MISSING_ACCT_ID}
    monkeypatch.setattr(CardXrefRepository, "GetXrefByAccount", StubReturning(MISSING_XREF))
    monkeypatch.setattr(AccountRepository, "GetAccount", StubReturning(accountRecord))
    monkeypatch.setattr(CustomerRepository, "GetCustomer", StubReturning(None))


STUBBED_READ_CASES = [
    StubbedReadCase(InstallAccountMissing, ACCOUNT_NOT_FOUND_99),
    StubbedReadCase(InstallCustomerMissing, CUSTOMER_NOT_FOUND_99),
]
STUBBED_READ_CASE_IDS = ["account-master", "customer-master"]


@pytest_asyncio.fixture(name="regularContext")
async def RegularContext(makeOnlineContext: Callable[[str], Awaitable[Any]]) -> Any:
    """Return the OnlineContext of a regular user (type U), who reaches CAVW from CM00."""
    return await makeOnlineContext(REGULAR_USER_TYPE)


@pytest.fixture(name="seedAccountRow")
def SeedAccountRow(migratedDatabase: Any, syncEngine: Engine) -> dict[str, Any]:
    """Return the committed seed row of account 00000000004."""
    return ReadRecord(syncEngine, ACCOUNT_ROW_SQL, VIEWED_ACCT_ID)


@pytest.fixture(name="seedCustomerRow")
def SeedCustomerRow(migratedDatabase: Any, syncEngine: Engine) -> dict[str, Any]:
    """Return the committed seed row of customer 000000004."""
    return ReadRecord(syncEngine, CUSTOMER_ROW_SQL, VIEWED_CUST_ID)


@pytest_asyncio.fixture(name="viewedAccount")
async def ViewedAccount(regularContext: Any) -> AccountViewResponse:
    """Return the COACTVW screen of account 00000000004, keyed as 11 digits."""
    return await coactvwc.ViewAccount(regularContext, VIEWED_ACCT_ID)


@pytest.mark.parametrize("acctIdText", BLANK_KEYS, ids=BLANK_KEY_IDS)
async def test_blank_key_answers_no_input_received(regularContext, acctIdText):
    """2210 sets the account prompt for a blank key; 2200's cross-field edit overwrites it."""
    raisedError = await RaisedViewError(regularContext, acctIdText)
    assert raisedError.httpStatus == EDIT_FAILURE_STATUS
    assert raisedError.Body() == ExpectedErrorBody(NO_INPUT_MESSAGE)
    assert raisedError.message != ACCOUNT_NOT_PROVIDED_MESSAGE


@pytest.mark.parametrize("acctIdText", INVALID_KEYS, ids=INVALID_KEY_IDS)
async def test_invalid_key_answers_the_filter_literal(regularContext, acctIdText):
    """2210 refuses a key that is not 11 digits, or is all zeros, with the line 673 literal."""
    raisedError = await RaisedViewError(regularContext, acctIdText)
    assert raisedError.httpStatus == EDIT_FAILURE_STATUS
    assert raisedError.Body() == ExpectedErrorBody(ACCOUNT_FILTER_MESSAGE)
    assert raisedError.message not in NEVER_SET_RETURN_MESSAGES


async def test_account_missing_from_cross_reference(regularContext):
    """9200 NOTFND on CXACAIX: the cross-reference STRING, cut to WS-RETURN-MSG's 75 bytes."""
    raisedError = await RaisedViewError(regularContext, MISSING_ACCT_ID)
    assert raisedError.httpStatus == NOT_FOUND_STATUS
    assert raisedError.Body() == ExpectedErrorBody(XREF_NOT_FOUND_99)
    assert len(raisedError.message) <= RETURN_MESSAGE_LENGTH


@pytest.mark.parametrize("readCase", STUBBED_READ_CASES, ids=STUBBED_READ_CASE_IDS)
async def test_master_record_missing_answers_its_not_found_string(
    regularContext, monkeypatch, seedAccountRow, readCase
):
    """9300 or 9400 NOTFND after a found cross-reference: that master file's STRING message.

    An ACCTDAT NOTFND does not end 9000 (line 792 is commented out), so CUSTDAT is still read;
    its own NOTFND finds WS-RETURN-MSG set and keeps the account message.
    """
    readCase.install(monkeypatch, seedAccountRow)
    raisedError = await RaisedViewError(regularContext, MISSING_ACCT_ID)
    assert raisedError.httpStatus == NOT_FOUND_STATUS
    assert raisedError.Body() == ExpectedErrorBody(readCase.message)
    assert len(raisedError.message) <= RETURN_MESSAGE_LENGTH


async def test_batch_window_refusal_answers_the_cxacaix_file_error(regularContext, monkeypatch):
    """9200 WHEN OTHER: the CXACAIX NOTOPEN of a closed batch window shows the file error."""
    monkeypatch.setattr(CardXrefRepository, "GetXrefByAccount", StubXrefWindowClosed)
    raisedError = await RaisedViewError(regularContext, VIEWED_ACCT_ID)
    assert raisedError.httpStatus == FILE_ERROR_STATUS
    assert raisedError.Body() == ExpectedErrorBody(CXACAIX_WINDOW_MESSAGE)


@pytest.mark.parametrize(
    ("builder", "key", "expectedMessage"),
    [
        (coactvwc.BuildXrefNotFoundMessage, MISSING_ACCT_ID, XREF_NOT_FOUND_99),
        (coactvwc.BuildAccountNotFoundMessage, MISSING_ACCT_ID, ACCOUNT_NOT_FOUND_99),
        (coactvwc.BuildCustomerNotFoundMessage, MISSING_CUST_ID, CUSTOMER_NOT_FOUND_99),
    ],
    ids=["cross-reference", "account-master", "customer-master"],
)
def test_service_not_found_builders_match_the_source_strings(builder, key, expectedMessage):
    """Extra check: the service's NOTFND builders give the strings typed from the source."""
    assert builder(key) == expectedMessage


def test_service_file_error_builder_matches_the_source_layout():
    """Extra check: WS-FILE-ERROR-MESSAGE for READ on CXACAIX with RESP 19 and RESP2 60."""
    windowError = FileNotOpenError(CXACAIX_FILE_NAME)
    builtMessage = coactvwc.BuildFileErrorMessage(
        FILE_ERROR_OPERATION, CXACAIX_FILE_NAME, windowError.resp, windowError.resp2
    )
    assert builtMessage == CXACAIX_WINDOW_MESSAGE


async def test_view_account_shows_the_account_record(viewedAccount, seedAccountRow):
    """1200 moves the CVACT01Y fields: amounts as exact Decimals, dates as YYYY-MM-DD text."""
    assert isinstance(viewedAccount, AccountViewResponse)
    assert viewedAccount.acct_id == VIEWED_ACCT_ID
    assert viewedAccount.curr_balance == SEED_BALANCE
    for column in ACCOUNT_AMOUNT_COLUMNS:
        assert type(getattr(viewedAccount, column)) is Decimal
        assert getattr(viewedAccount, column) == seedAccountRow[column]
    for column in ACCOUNT_TEXT_COLUMNS:
        assert getattr(viewedAccount, column) == seedAccountRow[column]
    for column in ACCOUNT_DATE_COLUMNS:
        assert getattr(viewedAccount, column) == DisplayedDate(seedAccountRow[column])


async def test_view_account_shows_the_customer_with_the_ssn_masked(viewedAccount, seedCustomerRow):
    """1200 moves the CVCUS01Y fields; the SSN shows only its last four digits."""
    expectedSsnMask = SSN_MASK_PREFIX + seedCustomerRow["ssn"][-SSN_SHOWN_DIGITS:]
    assert viewedAccount.cust_id == VIEWED_CUST_ID
    assert viewedAccount.fico_credit_score == SEED_FICO_SCORE
    assert viewedAccount.fico_credit_score == seedCustomerRow["fico_credit_score"]
    for column in CUSTOMER_TEXT_COLUMNS:
        assert getattr(viewedAccount, column) == seedCustomerRow[column]
    assert viewedAccount.date_of_birth == DisplayedDate(seedCustomerRow["date_of_birth"])
    assert viewedAccount.ssn_masked == expectedSsnMask


async def test_view_account_sends_the_prompt_and_no_message(viewedAccount):
    """A found account leaves WS-RETURN-MSG off; 1200 sets the WS-PROMPT-FOR-INPUT line."""
    assert viewedAccount.message == ""
    assert viewedAccount.info_message == PROMPT_INFO
    assert viewedAccount.info_message != INFORM_OUTPUT_INFO
    assert viewedAccount.field_errors == []
    assert viewedAccount.cursor_field == ACCOUNT_ID_FIELD


async def test_view_account_exposes_no_raw_ssn_card_number_or_cvv(viewedAccount, seedCustomerRow):
    """The screen carries no raw SSN, card number, CVV or password, and no float amount."""
    responseJson = viewedAccount.model_dump_json()
    assert seedCustomerRow["ssn"] not in responseJson
    assert VIEWED_CARD_NUM not in responseJson
    assert not FORBIDDEN_RESPONSE_FIELDS & set(viewedAccount.model_dump())
    assert not any("cvv" in fieldName for fieldName in AccountViewResponse.model_fields)
    assert json.loads(responseJson)["curr_balance"] == SEED_BALANCE_JSON


async def test_view_account_writes_nothing(regularContext, syncEngine):
    """CAVW only READs: the account and customer rows keep their values and row versions."""
    accountBefore = ReadRecord(syncEngine, ACCOUNT_VERSION_SQL, VIEWED_ACCT_ID)
    customerBefore = ReadRecord(syncEngine, CUSTOMER_VERSION_SQL, VIEWED_CUST_ID)
    await coactvwc.ViewAccount(regularContext, VIEWED_ACCT_ID)
    await RaisedViewError(regularContext, MISSING_ACCT_ID)
    await RaisedViewError(regularContext, INVALID_KEYS[0])
    assert ReadRecord(syncEngine, ACCOUNT_VERSION_SQL, VIEWED_ACCT_ID) == accountBefore
    assert ReadRecord(syncEngine, CUSTOMER_VERSION_SQL, VIEWED_CUST_ID) == customerBefore
    assert accountBefore["curr_balance"] == SEED_BALANCE
