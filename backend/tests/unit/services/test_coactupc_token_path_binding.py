"""Unit tests for the COACTUPC (CAUP) screen_token bound to the account the PUT path names.

``PUT /api/v1/accounts/{acctId}`` presses ENTER, F5 or F12 on the COACTUP screen, whose
COMMAREA state rides in the screen_token. The CAUP screen-token rule is that an invalid or
expired token is treated as DETAILS_NOT_FETCHED, so an invalid screen_token restarts the flow.
A token whose state shows fetched details (SHOW_DETAILS, CHANGES_NOT_OK,
CHANGES_OK_NOT_CONFIRMED) acts on its own account key, so one issued for account A is invalid
on the path of account B. Expected values follow that rule and the COACTUP state table:

- a fetched-state token for A on B's path, with enter, save or cancel, is the
    DETAILS_NOT_FETCHED ENTER of B (1210-EDIT-ACCOUNT, then 9000-READ-ACCT): B is read once and
    shown in SHOW_DETAILS with a token for B, A is never read and 9600-WRITE-PROCESSING never
    runs;
- the path must be A's 11-character key itself: ``11`` is another key, which the restart's key
    edit refuses with nothing read or written;
- on A's own path the token keeps its state: F5 writes A's validated values and ENTER keeps
    the confirmation screen;
- CHANGES_DONE and CHANGES_FAILED tokens act on no key: any key, a newly typed one included,
    returns the empty key screen in DETAILS_NOT_FETCHED without reading.

app.services.accounts.coactupc's ReadAccountChain (9000-READ-ACCT) and RunWriteProcessing
(9600-WRITE-PROCESSING) are replaced by AsyncMocks, so no database is needed.
"""

import secrets
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType, SimpleNamespace
from typing import Any, NamedTuple
from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings
from app.core.dependencies import OnlineContext
from app.core.errors import LegacyMessageError
from app.schemas.accounts import AccountScreen, AccountScreenFields, AccountUpdateRequest
from app.schemas.common import UpdateAction, UpdateScreenState
from app.services.accounts import coactupc
from app.services.accounts.coactvwc import AccountReadResult

# LIT-THISTRANID: the transaction 9000-READ-ACCT logs its file conditions under.
TRANSACTION_ID = "CAUP"
# CDEMO-ACCT-ID and CDEMO-CUST-ID of the account the replayed screen was fetched for (A) and of
# the account the PUT path names (B).
ISSUED_ACCOUNT_ID = "00000000011"
ISSUED_CUSTOMER_ID = "000000011"
PATH_ACCOUNT_ID = "00000000022"
PATH_CUSTOMER_ID = "000000022"
CUSTOMER_OF_ACCOUNT = MappingProxyType(
    {ISSUED_ACCOUNT_ID: ISSUED_CUSTOMER_ID, PATH_ACCOUNT_ID: PATH_CUSTOMER_ID}
)
# A's account number without its leading zeros: text that is not an 11-digit key.
SHORT_ACCOUNT_KEY = "11"
ACCOUNT_ID_FIELD = "acct_id"
CUSTOMER_ID_FIELD = "cust_id"
# The 1210-EDIT-ACCOUNT literal for a key that is not 11 digits, answered with 400.
MSG_ACCOUNT_KEY_INVALID = "Account Number if supplied must be a 11 digit Non-Zero Number"
EDIT_FAILURE_STATUS = 400
# The 3250-SETUP-INFOMSG lines of DETAILS_NOT_FETCHED and SHOW_DETAILS.
INFO_PROMPT_FOR_SEARCH_KEYS = "Enter or update id of account to update"
INFO_PROMPT_FOR_CHANGES = "Update account details presented above."
ZERO_AMOUNT_TEXT = "0.00"
STORED_AMOUNT = Decimal("3819.00")
STORED_FICO_SCORE = 700
# The credit limit A's screen validated, which only a save on A's own path may write.
VALIDATED_CREDIT_LIMIT = "7777.00"
# The detail edit the page sends from a fetched screen: never the protected account id.
DETAIL_EDITS = AccountScreenFields(credit_limit=VALIDATED_CREDIT_LIMIT)
FETCHED_SCREEN_STATES = [
    UpdateScreenState.SHOW_DETAILS,
    UpdateScreenState.CHANGES_NOT_OK,
    UpdateScreenState.CHANGES_OK_NOT_CONFIRMED,
]
FINISHED_SCREEN_STATES = [UpdateScreenState.CHANGES_DONE, UpdateScreenState.CHANGES_FAILED]
ALL_ACTIONS = list(UpdateAction)
SECRET_KEY_BYTES = 32
DATABASE_PASSWORD_BYTES = 12


class OwnPathCase(NamedTuple):
    """A key pressed on A's own path with A's confirmation token, and what it must give."""

    action: UpdateAction
    expectedState: UpdateScreenState
    expectedWrites: int


OWN_PATH_CASES = [
    OwnPathCase(UpdateAction.SAVE, UpdateScreenState.CHANGES_DONE, 1),
    OwnPathCase(UpdateAction.ENTER, UpdateScreenState.CHANGES_OK_NOT_CONFIRMED, 0),
]
OWN_PATH_CASE_IDS = ["save-writes", "enter-keeps"]


@dataclass(frozen=True)
class StubbedFlow:
    """What one CAUP request runs against: its online context, the read chain and the write."""

    context: OnlineContext
    readChain: AsyncMock
    writeProcessing: AsyncMock


def BuildDatabaseUrl(driverName: str, databasePassword: str) -> str:
    """Return a PostgreSQL URL on host 127.0.0.1 that no stubbed request ever connects to."""
    return f"postgresql+{driverName}://coactupc_test:{databasePassword}@127.0.0.1/coactupc_test"


def BuildSettings() -> Settings:
    """Return backend Settings with a generated SECRET_KEY for the screen token."""
    databasePassword = secrets.token_urlsafe(DATABASE_PASSWORD_BYTES)
    return Settings(
        _env_file=None,
        SECRET_KEY=secrets.token_urlsafe(SECRET_KEY_BYTES),
        DATABASE_URL=BuildDatabaseUrl("asyncpg", databasePassword),
        SYNC_DATABASE_URL=BuildDatabaseUrl("psycopg2", databasePassword),
    )


def BuildContext() -> OnlineContext:
    """Return an online context that no stubbed read or write uses for I/O."""
    appState = SimpleNamespace(engine=SimpleNamespace())
    request = SimpleNamespace(app=SimpleNamespace(state=appState))
    return OnlineContext(request=request, session=SimpleNamespace(), settings=BuildSettings())


def StoredRecords(acctId: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the ACCTDAT and CUSTDAT records of ``acctId`` as the repositories map them."""
    account: dict[str, Any] = {column: "" for column in coactupc.ACCOUNT_TEXT_COLUMNS}
    account.update({column: STORED_AMOUNT for column in coactupc.AMOUNT_FIELDS})
    account.update({column: None for column in coactupc.ACCOUNT_DATE_FIELDS})
    account[ACCOUNT_ID_FIELD] = acctId
    customer: dict[str, Any] = {column: "" for column in coactupc.CUSTOMER_TEXT_COLUMNS}
    customer.update({coactupc.BIRTH_DATE_COLUMN: None, coactupc.FICO_FIELD: STORED_FICO_SCORE})
    customer[CUSTOMER_ID_FIELD] = CUSTOMER_OF_ACCOUNT[acctId]
    return account, customer


async def ReadStoredChain(
    context: OnlineContext, acctId: str, transactionId: str
) -> AccountReadResult:
    """9000-READ-ACCT stubbed: the cross-reference, account and customer of ``acctId`` exist."""
    account, customer = StoredRecords(acctId)
    return AccountReadResult(
        acctId=acctId,
        account=account,
        customer=customer,
        customerFound=True,
        transactionId=transactionId,
    )


@pytest.fixture(name="stubbedFlow")
def StubbedFlowFixture(monkeypatch) -> StubbedFlow:
    """Make coactupc read through the stub chain and record its 9600 runs; return the stubs."""
    stubbedFlow = StubbedFlow(
        BuildContext(),
        AsyncMock(side_effect=ReadStoredChain),
        AsyncMock(return_value=coactupc.WriteAttempt(outcome=coactupc.OUTCOME_DONE)),
    )
    monkeypatch.setattr(coactupc, "ReadAccountChain", stubbedFlow.readChain)
    monkeypatch.setattr(coactupc, "RunWriteProcessing", stubbedFlow.writeProcessing)
    return stubbedFlow


def ValidatedValues() -> coactupc.NewAccountData:
    """Return the ACUP-NEW-DETAILS A's screen validated: A's key and a new credit limit."""
    texts = {name: "" for name in coactupc.SCREEN_FIELD_LENGTHS}
    texts.update({ACCOUNT_ID_FIELD: ISSUED_ACCOUNT_ID, "credit_limit": VALIDATED_CREDIT_LIMIT})
    amounts = {name: Decimal(ZERO_AMOUNT_TEXT) for name in coactupc.AMOUNT_FIELDS}
    amounts["credit_limit"] = Decimal(VALIDATED_CREDIT_LIMIT)
    return coactupc.NewAccountData(texts=texts, amounts=amounts)


def IssuedState(screenState: UpdateScreenState) -> coactupc.AccountUpdateState:
    """Return ``screenState`` for account A, complete as its screen_token carries it.

    A fetched state carries A's keys and a blank before-image with zero amounts, and
    CHANGES_OK_NOT_CONFIRMED also the validated values; a finished state carries the keys only.
    """
    if screenState in FINISHED_SCREEN_STATES:
        return coactupc.AccountUpdateState(screenState, ISSUED_ACCOUNT_ID, ISSUED_CUSTOMER_ID)
    beforeImage = {column: "" for column in coactupc.IMAGE_COLUMNS}
    beforeImage.update({column: ZERO_AMOUNT_TEXT for column in coactupc.AMOUNT_FIELDS})
    beforeImage.update({ACCOUNT_ID_FIELD: ISSUED_ACCOUNT_ID, CUSTOMER_ID_FIELD: ISSUED_CUSTOMER_ID})
    newValues = None
    if screenState is UpdateScreenState.CHANGES_OK_NOT_CONFIRMED:
        newValues = ValidatedValues()
    return coactupc.AccountUpdateState(
        screenState, ISSUED_ACCOUNT_ID, ISSUED_CUSTOMER_ID, beforeImage, newValues
    )


def IssuedRequest(
    context: OnlineContext,
    screenState: UpdateScreenState,
    action: UpdateAction,
    keyedFields: AccountScreenFields,
) -> AccountUpdateRequest:
    """Return the PUT body pressing ``action`` with A's ``screenState`` screen_token."""
    screenToken = coactupc.EncodeUpdateState(IssuedState(screenState), context)
    return AccountUpdateRequest(action=action, screen_token=screenToken, fields=keyedFields)


def AssertShowDetailsOf(screen: AccountScreen, context: OnlineContext, acctId: str) -> None:
    """Assert the SHOW_DETAILS screen of ``acctId``, whose new screen_token names that account."""
    assert screen.state == UpdateScreenState.SHOW_DETAILS
    assert (screen.message, screen.info_message) == ("", INFO_PROMPT_FOR_CHANGES)
    assert (screen.fields.acct_id, screen.fields.cust_id) == (acctId, CUSTOMER_OF_ACCOUNT[acctId])
    shownState = coactupc.DecodeUpdateState(screen.screen_token, context)
    assert (shownState.screenState, shownState.acctId) == (screen.state, acctId)


@pytest.mark.parametrize("action", ALL_ACTIONS, ids=[action.value for action in ALL_ACTIONS])
@pytest.mark.parametrize(
    "screenState",
    FETCHED_SCREEN_STATES,
    ids=[screenState.value for screenState in FETCHED_SCREEN_STATES],
)
async def test_fetched_token_on_another_account_path_fetches_the_path_account(
    stubbedFlow, screenState, action
):
    """A's token on B's path restarts the flow: B is fetched, A is never read or written."""
    context = stubbedFlow.context
    replayRequest = IssuedRequest(context, screenState, action, DETAIL_EDITS)
    screen = await coactupc.ProcessAccountUpdate(context, PATH_ACCOUNT_ID, replayRequest)
    AssertShowDetailsOf(screen, context, PATH_ACCOUNT_ID)
    stubbedFlow.readChain.assert_awaited_once_with(context, PATH_ACCOUNT_ID, TRANSACTION_ID)
    stubbedFlow.writeProcessing.assert_not_awaited()


async def test_confirmed_token_on_unpadded_path_is_edited_as_another_key(stubbedFlow):
    """``11`` is not A's 11-character key: the restart refuses it and reads nothing."""
    context = stubbedFlow.context
    replayRequest = IssuedRequest(
        context, UpdateScreenState.CHANGES_OK_NOT_CONFIRMED, UpdateAction.SAVE, DETAIL_EDITS
    )
    with pytest.raises(LegacyMessageError) as raised:
        await coactupc.ProcessAccountUpdate(context, SHORT_ACCOUNT_KEY, replayRequest)
    errorBody = raised.value.Body()
    assert raised.value.httpStatus == EDIT_FAILURE_STATUS
    assert (errorBody["message"], errorBody["state"]) == (
        MSG_ACCOUNT_KEY_INVALID,
        UpdateScreenState.DETAILS_NOT_FETCHED.value,
    )
    stubbedFlow.readChain.assert_not_awaited()
    stubbedFlow.writeProcessing.assert_not_awaited()


@pytest.mark.parametrize("ownPathCase", OWN_PATH_CASES, ids=OWN_PATH_CASE_IDS)
async def test_confirmed_token_on_its_own_path_keeps_its_state(stubbedFlow, ownPathCase):
    """On A's own path the confirmation token holds: F5 writes A, ENTER keeps the screen."""
    context = stubbedFlow.context
    ownRequest = IssuedRequest(
        context, UpdateScreenState.CHANGES_OK_NOT_CONFIRMED, ownPathCase.action, DETAIL_EDITS
    )
    screen = await coactupc.ProcessAccountUpdate(context, ISSUED_ACCOUNT_ID, ownRequest)
    assert (screen.state, screen.fields.acct_id) == (ownPathCase.expectedState, ISSUED_ACCOUNT_ID)
    assert stubbedFlow.writeProcessing.await_count == ownPathCase.expectedWrites
    for writeCall in stubbedFlow.writeProcessing.await_args_list:
        writtenState = writeCall.args[1]
        assert writtenState.acctId == ISSUED_ACCOUNT_ID
        assert writtenState.newValues.amounts["credit_limit"] == Decimal(VALIDATED_CREDIT_LIMIT)
    stubbedFlow.readChain.assert_not_awaited()


@pytest.mark.parametrize("action", ALL_ACTIONS, ids=[action.value for action in ALL_ACTIONS])
@pytest.mark.parametrize(
    "screenState",
    FINISHED_SCREEN_STATES,
    ids=[screenState.value for screenState in FINISHED_SCREEN_STATES],
)
async def test_finished_token_on_another_account_path_returns_empty_key_screen(
    stubbedFlow, screenState, action
):
    """A's finished token on B's path, B's key typed, gives the empty key screen unread."""
    context = stubbedFlow.context
    typedKey = AccountScreenFields(acct_id=PATH_ACCOUNT_ID)
    replayRequest = IssuedRequest(context, screenState, action, typedKey)
    screen = await coactupc.ProcessAccountUpdate(context, PATH_ACCOUNT_ID, replayRequest)
    assert screen.state == UpdateScreenState.DETAILS_NOT_FETCHED
    assert (screen.fields, screen.message) == (AccountScreenFields(), "")
    assert screen.info_message == INFO_PROMPT_FOR_SEARCH_KEYS
    stubbedFlow.readChain.assert_not_awaited()
    stubbedFlow.writeProcessing.assert_not_awaited()
