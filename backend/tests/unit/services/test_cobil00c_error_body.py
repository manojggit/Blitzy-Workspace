"""Unit tests for the shape of the COBIL00C (CB00) error bodies, map COBIL00.

Every screen cbl/COBIL00C.cbl sends with ERR-FLG set is a legacy error whose body holds exactly
the four standard keys ``message``, ``info_message``, ``field_errors`` and ``cursor_field``
(app.core.errors.STANDARD_BODY_KEYS). CURBAL travels only on the screens sent with HTTP 200: the
confirmation prompt keeps ``current_balance``. Expected literals, statuses and cursors are typed
from the source lines:

- PROCESS-ENTER-KEY: a blank ACTIDIN (lines 159-164) and a CONFIRM other than Y, y, N, n or
  blank (lines 185-190), both 400;
- READ-ACCTDAT-FILE: NOTFND (lines 359-364), 404, and WHEN OTHER (lines 365-371), 503;
- the balance check (lines 197-205): a balance not above zero, 400;
- the final SEND-BILLPAY-SCREEN (line 242) after a failure recorded later in the payment
  sequence: the CXACAIX NOTFND 404, the DUPKEY 409 and the REWRITE WHEN OTHER 503;
- a blank CONFIRM (lines 236-240): the prompt with the balance read, HTTP 200.

app.services.billpay.cobil00c's AccountRepository and WriteUnit are replaced by stubs, so no
database is needed.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import NamedTuple
from unittest.mock import AsyncMock

import pytest

from app.core.dependencies import OnlineContext
from app.core.errors import STANDARD_BODY_KEYS, FileIoError, LegacyMessageError
from app.schemas.billpay import BillPayRequest, BillPayResponse
from app.services.billpay import cobil00c

KEYED_ACCOUNT_ID = "00000000001"
POSITIVE_BALANCE = Decimal("194.00")
ZERO_BALANCE = Decimal("0.00")
# The connection a stubbed write unit yields; the stub repository never touches it.
STUB_CONNECTION = SimpleNamespace()

# The WS-MESSAGE literals of lines 161, 187, 361, 368, 201, 237, 536 and 399.
ACCT_ID_EMPTY_MESSAGE = "Acct ID can NOT be empty..."
INVALID_CONFIRM_MESSAGE = "Invalid value. Valid values are (Y/N)..."
ACCOUNT_NOT_FOUND_MESSAGE = "Account ID NOT found..."
ACCOUNT_LOOKUP_FAILED_MESSAGE = "Unable to lookup Account..."
NOTHING_TO_PAY_MESSAGE = "You have nothing to pay..."
CONFIRM_PAYMENT_MESSAGE = "Confirm to make a bill payment..."
TRAN_ID_EXISTS_MESSAGE = "Tran ID already exist..."
UPDATE_ACCOUNT_FAILED_MESSAGE = "Unable to Update Account..."
# MOVE -1 TO ACTIDINL and MOVE -1 TO CONFIRML.
CURSOR_ACCT_ID = "acct_id"
CURSOR_CONFIRM = "confirm"

HTTP_OK = 200
HTTP_EDIT_FAILURE = 400
HTTP_NOT_FOUND = 404
HTTP_RECORD_CHANGED = 409
HTTP_FILE_ERROR = 503


class StubAccountRepository:
    """AccountRepository's READ UPDATE as an AsyncMock; it answers the account by default."""

    def __init__(self) -> None:
        self.GetAccountForUpdate = AsyncMock(return_value=PAYABLE_ACCOUNT)

    def ForContext(self, context: OnlineContext) -> "StubAccountRepository":
        """Return this stub as the repository the service builds from its context."""
        return self


class ScreenErrorCase(NamedTuple):
    """An ENTER, the READ UPDATE answer it meets and the error screen COBIL00C sends."""

    request: BillPayRequest
    accountAnswer: object
    httpStatus: int
    message: str
    cursorField: str


class RecordedFailure(NamedTuple):
    """A failure literal a payment step records after the account was read."""

    httpStatus: int
    message: str


def AccountRecord(balance: Decimal) -> dict[str, object]:
    """Return the ACCOUNT-RECORD fields COBIL00C reads before the payment: the balance."""
    return {"acct_id": KEYED_ACCOUNT_ID, "curr_balance": balance}


@asynccontextmanager
async def StubWriteUnit(engine: object, fileName: str) -> AsyncIterator[SimpleNamespace]:
    """Stand in for core.database.WriteUnit: yield a connection, let every exception through."""
    yield STUB_CONNECTION


@pytest.fixture(name="accountRepository")
def AccountRepositoryStub(monkeypatch) -> StubAccountRepository:
    """Make cobil00c build the stub repository and the stub write unit, and return the stub."""
    stubRepository = StubAccountRepository()
    monkeypatch.setattr(cobil00c, "AccountRepository", stubRepository.ForContext)
    monkeypatch.setattr(cobil00c, "WriteUnit", StubWriteUnit)
    return stubRepository


def BuildContext() -> OnlineContext:
    """Return an online context whose app state holds an engine only the stubs receive."""
    # The request's own state is where core.locks.RequestEngine looks before app.state.engine.
    appState = SimpleNamespace(engine=None)
    request = SimpleNamespace(app=SimpleNamespace(state=appState), state=SimpleNamespace())
    return OnlineContext(request=request, session=SimpleNamespace(), settings=None)


def ExpectedBody(message: str, cursorField: str) -> dict[str, object]:
    """Return the four-key body of a first-failure screen: one failing field, the cursor on it."""
    return {
        "message": message,
        "info_message": "",
        "field_errors": [cursorField],
        "cursor_field": cursorField,
    }


def KeyedRequest(confirm: str, acctId: str = KEYED_ACCOUNT_ID) -> BillPayRequest:
    """Return the ENTER of map COBIL00 with ACTIDIN and CONFIRM as keyed."""
    return BillPayRequest(acct_id=acctId, confirm=confirm)


# An account with a balance to pay: the edit failures are sent before it would be read.
PAYABLE_ACCOUNT = AccountRecord(POSITIVE_BALANCE)


SCREEN_ERROR_CASES = [
    ScreenErrorCase(
        KeyedRequest("", acctId=""),
        PAYABLE_ACCOUNT,
        HTTP_EDIT_FAILURE,
        ACCT_ID_EMPTY_MESSAGE,
        CURSOR_ACCT_ID,
    ),
    ScreenErrorCase(
        KeyedRequest("X"),
        PAYABLE_ACCOUNT,
        HTTP_EDIT_FAILURE,
        INVALID_CONFIRM_MESSAGE,
        CURSOR_CONFIRM,
    ),
    ScreenErrorCase(
        KeyedRequest(""), None, HTTP_NOT_FOUND, ACCOUNT_NOT_FOUND_MESSAGE, CURSOR_ACCT_ID
    ),
    ScreenErrorCase(
        KeyedRequest(""),
        FileIoError(cobil00c.ACCOUNT_FILE_NAME),
        HTTP_FILE_ERROR,
        ACCOUNT_LOOKUP_FAILED_MESSAGE,
        CURSOR_ACCT_ID,
    ),
    ScreenErrorCase(
        KeyedRequest(""),
        AccountRecord(ZERO_BALANCE),
        HTTP_EDIT_FAILURE,
        NOTHING_TO_PAY_MESSAGE,
        CURSOR_ACCT_ID,
    ),
]
SCREEN_ERROR_IDS = [
    "blank-acct-400",
    "invalid-confirm-400",
    "not-found-404",
    "account-read-ioerr-503",
    "nothing-to-pay-400",
]

RECORDED_FAILURES = [
    RecordedFailure(HTTP_NOT_FOUND, ACCOUNT_NOT_FOUND_MESSAGE),
    RecordedFailure(HTTP_RECORD_CHANGED, TRAN_ID_EXISTS_MESSAGE),
    RecordedFailure(HTTP_FILE_ERROR, UPDATE_ACCOUNT_FAILED_MESSAGE),
]
RECORDED_FAILURE_IDS = ["xref-not-found-404", "duplicate-tran-id-409", "rewrite-failed-503"]


def AnswerReadUpdate(accountRepository: StubAccountRepository, accountAnswer: object) -> None:
    """Make the READ UPDATE raise a file condition or return the record (None is NOTFND)."""
    if isinstance(accountAnswer, Exception):
        accountRepository.GetAccountForUpdate.side_effect = accountAnswer
        return
    accountRepository.GetAccountForUpdate.return_value = accountAnswer


@pytest.mark.parametrize("errorCase", SCREEN_ERROR_CASES, ids=SCREEN_ERROR_IDS)
async def test_screen_sent_with_err_flg_has_only_the_four_standard_keys(
    accountRepository, errorCase
):
    """Edit, READ UPDATE and balance failures: the literal and cursor, no current_balance."""
    AnswerReadUpdate(accountRepository, errorCase.accountAnswer)
    with pytest.raises(LegacyMessageError) as raised:
        await cobil00c.PayBill(BuildContext(), errorCase.request)
    assert raised.value.httpStatus == errorCase.httpStatus
    assert set(raised.value.Body()) == set(STANDARD_BODY_KEYS)
    assert raised.value.Body() == ExpectedBody(errorCase.message, errorCase.cursorField)


@pytest.mark.parametrize("recordedFailure", RECORDED_FAILURES, ids=RECORDED_FAILURE_IDS)
def test_failure_recorded_after_the_read_has_only_the_four_standard_keys(recordedFailure):
    """The final SEND after a payment-step failure: the balance read is not in the body."""
    state = cobil00c.PaymentState(
        acctId=KEYED_ACCOUNT_ID, payRequested=True, screenBalance=POSITIVE_BALANCE
    )
    cobil00c.RecordMessage(state, recordedFailure.message, recordedFailure.httpStatus)
    with pytest.raises(LegacyMessageError) as raised:
        cobil00c.BuildOutcome(state)
    assert raised.value.httpStatus == recordedFailure.httpStatus
    assert set(raised.value.Body()) == set(STANDARD_BODY_KEYS)
    assert raised.value.Body() == ExpectedBody(recordedFailure.message, CURSOR_ACCT_ID)


async def test_confirm_prompt_keeps_current_balance(accountRepository):
    """A blank CONFIRM: HTTP 200 with CURBAL, the prompt literal and the cursor on CONFIRM."""
    response = await cobil00c.PayBill(BuildContext(), KeyedRequest(""))
    assert response == BillPayResponse(
        current_balance=POSITIVE_BALANCE,
        message=CONFIRM_PAYMENT_MESSAGE,
        cursor_field=CURSOR_CONFIRM,
        tran_id=None,
    )
    assert response.model_dump(mode="json")["current_balance"] == str(POSITIVE_BALANCE)
    accountRepository.GetAccountForUpdate.assert_awaited_once_with(
        STUB_CONNECTION, KEYED_ACCOUNT_ID
    )
