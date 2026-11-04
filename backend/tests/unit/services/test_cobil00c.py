"""Unit tests for COBIL00C (transaction CB00, map COBIL00): the online bill payment.

Covers: cbl/COBIL00C.cbl, map bms/COBIL00.bms, copybooks CVACT01Y, CVACT03Y, CVTRA05Y and
CSDAT01Y, as ported by app.services.billpay.cobil00c.PayBill.

PROCESS-ENTER-KEY (lines 154-244) edits in first-failure mode: a blank ACTIDIN (lines 159-164),
then CONFIRM (lines 173-191, checked before the account is read), then READ-ACCTDAT-FILE NOTFND
and WHEN OTHER (lines 359-371), then a balance not above zero (lines 197-205). ACTIDIN has no
digit edit (line 170), so a nonnumeric or short account ID is read as keyed and is NOTFND.
A blank CONFIRM asks for confirmation with CURBAL (lines 236-240); N or n clears the screen
(lines 177-180).
Y or y pays the whole balance (lines 210-235): MOVE ACCT-CURR-BAL TO TRAN-AMT stores the
S9(10)V99 balance into S9(09)V99, the BILL PAYMENT - ONLINE record is written with TRAN-ID
highest + 1, and the COMPUTE and REWRITE always follow the WRITE, so the last literal set is
the one sent: a duplicate-key WRITE still reduces the balance, and a failed REWRITE keeps the
committed transaction.

Each case calls PayBill directly with a regular user's context against the migrated, seeded
test database, with GET-CURRENT-TIMESTAMP frozen, and re-reads accounts.accounts and
transactions.transactions with plain SQL. Every write unit commits, so the seed is restored
after each case. Seed facts: account 00000000004 holds 40.00 on card 2988091353094312, and no
transaction exists.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Any, NamedTuple
from unittest import mock

import pytest
import pytest_asyncio
from sqlalchemy import Engine, text

from app.core.errors import DuplicateKeyError, FileIoError, LegacyMessageError, RepositoryError
from app.core.locks import FileNotOpenError
from app.repositories.account_repository import ACCOUNT_FILE_NAME, AccountRepository
from app.repositories.transaction_repository import TRANSACTION_FILE_NAME, TransactionRepository
from app.schemas.billpay import BillPayRequest, BillPayResponse
from app.services.billpay import cobil00c

# --- Seed and request values -----------------------------------------------------------------
SEED_ACCT_ID = "00000000004"
SEED_CARD_NUM = "2988091353094312"
SEED_BALANCE = Decimal("40.00")
UNKNOWN_ACCT_ID = "00000000099"
BLANK_ACCT_ID = "           "
# ACTIDIN is never digit-checked or zero-padded: MOVE ACTIDINI TO ACCT-ID (line 170) keys
# READ-ACCTDAT-FILE (lines 343-372) with the text as keyed, so each of these is NOTFND.
NONNUMERIC_ACCT_ID = "ABCDEFGHIJK"
MIXED_ACCT_ID = "0000000000A"
SHORT_ACCT_ID = "4"
TEN_DIGIT_ACCT_ID = "0000000004"
REGULAR_USER_TYPE = "U"
FIXED_TIMESTAMP = datetime(2022, 7, 18, 10, 11, 12)

# --- Balances: COMPUTE ACCT-CURR-BAL = ACCT-CURR-BAL - TRAN-AMT (line 234) ---------------------
PAID_BALANCE = Decimal("0.00")
NEGATIVE_BALANCE = Decimal("-5.00")
# MOVE ACCT-CURR-BAL TO TRAN-AMT (line 224): S9(10)V99 into S9(09)V99 loses the high-order
# digit, so a balance of 1,000,000,000.50 pays 0.50 and leaves 1,000,000,000.00.
WIDE_BALANCE = Decimal("1000000000.50")
WIDE_TRAN_AMT = Decimal("0.50")
WIDE_BALANCE_AFTER = Decimal("1000000000.00")

# --- WS-MESSAGE literals, verbatim (source lines in brackets) ----------------------------------
ACCT_ID_EMPTY_MESSAGE = "Acct ID can NOT be empty..."  # [161]
INVALID_CONFIRM_MESSAGE = "Invalid value. Valid values are (Y/N)..."  # [187]
ACCOUNT_NOT_FOUND_MESSAGE = "Account ID NOT found..."  # [361]
ACCOUNT_LOOKUP_FAILED_MESSAGE = "Unable to lookup Account..."  # [368]
NOTHING_TO_PAY_MESSAGE = "You have nothing to pay..."  # [201]
CONFIRM_PAYMENT_MESSAGE = "Confirm to make a bill payment..."  # [237]
TRAN_ID_EXISTS_MESSAGE = "Tran ID already exist..."  # [536]
UPDATE_ACCOUNT_FAILED_MESSAGE = "Unable to Update Account..."  # [399]
# The STRING of lines 527-530: 'Payment successful. ' and ' Your Transaction ID is '.
FIRST_TRAN_ID = "0000000000000001"
FIRST_PAYMENT_MESSAGE = "Payment successful.  Your Transaction ID is 0000000000000001."

# --- Cursor fields (MOVE -1 TO ACTIDINL / CONFIRML) and HTTP statuses ---------------------------
CURSOR_ACCT_ID = "acct_id"
CURSOR_CONFIRM = "confirm"
HTTP_EDIT_FAILURE = 400
HTTP_NOT_FOUND = 404
HTTP_RECORD_CHANGED = 409
HTTP_FILE_ERROR = 503

# --- The payment record of lines 218-232, as transactions.transactions stores it ----------------
PAYMENT_RECORD = {
    "tran_id": FIRST_TRAN_ID,
    "tran_type_cd": "02",
    "tran_cat_cd": 2,
    "tran_source": "POS TERM",
    "tran_desc": "BILL PAYMENT - ONLINE",
    "tran_amt": SEED_BALANCE,
    "merchant_id": "999999999",
    "merchant_name": "BILL PAYMENT",
    "merchant_city": "N/A",
    "merchant_zip": "N/A",
    "card_num": SEED_CARD_NUM,
    "orig_ts": FIXED_TIMESTAMP,
    "proc_ts": FIXED_TIMESTAMP,
    "orig_ts_format": "S",
    "proc_ts_format": "S",
}

# --- Plain SQL with bound values ---------------------------------------------------------------
ACCOUNT_QUERY = text("SELECT * FROM accounts.accounts WHERE acct_id = :acctId")
SET_BALANCE_STATEMENT = text(
    "UPDATE accounts.accounts SET curr_balance = :amount WHERE acct_id = :acctId"
)
TRANSACTIONS_QUERY = text(
    "SELECT tran_id, tran_type_cd, tran_cat_cd, tran_source, tran_desc, tran_amt,"
    " merchant_id, merchant_name, merchant_city, merchant_zip, card_num,"
    " orig_ts, proc_ts, orig_ts_format, proc_ts_format"
    " FROM transactions.transactions ORDER BY tran_id"
)


class BillPayScenario(NamedTuple):
    """A regular user's OnlineContext and the engine every outcome is re-read through."""

    context: Any
    syncEngine: Engine


class EditCase(NamedTuple):
    """An ENTER on COBIL00 refused before any write, with the screen COBIL00C sends."""

    acctId: str
    confirm: str
    httpStatus: int
    message: str
    cursorField: str


EDIT_CASES = [
    EditCase("", "Y", HTTP_EDIT_FAILURE, ACCT_ID_EMPTY_MESSAGE, CURSOR_ACCT_ID),
    EditCase(BLANK_ACCT_ID, "Y", HTTP_EDIT_FAILURE, ACCT_ID_EMPTY_MESSAGE, CURSOR_ACCT_ID),
    EditCase(UNKNOWN_ACCT_ID, "Y", HTTP_NOT_FOUND, ACCOUNT_NOT_FOUND_MESSAGE, CURSOR_ACCT_ID),
    EditCase(NONNUMERIC_ACCT_ID, "Y", HTTP_NOT_FOUND, ACCOUNT_NOT_FOUND_MESSAGE, CURSOR_ACCT_ID),
    EditCase(MIXED_ACCT_ID, "Y", HTTP_NOT_FOUND, ACCOUNT_NOT_FOUND_MESSAGE, CURSOR_ACCT_ID),
    EditCase(SHORT_ACCT_ID, "Y", HTTP_NOT_FOUND, ACCOUNT_NOT_FOUND_MESSAGE, CURSOR_ACCT_ID),
    EditCase(TEN_DIGIT_ACCT_ID, "Y", HTTP_NOT_FOUND, ACCOUNT_NOT_FOUND_MESSAGE, CURSOR_ACCT_ID),
    # A blank CONFIRM also performs READ-ACCTDAT-FILE (line 184), so NOTFND precedes the prompt.
    EditCase(NONNUMERIC_ACCT_ID, "", HTTP_NOT_FOUND, ACCOUNT_NOT_FOUND_MESSAGE, CURSOR_ACCT_ID),
    EditCase(SEED_ACCT_ID, "X", HTTP_EDIT_FAILURE, INVALID_CONFIRM_MESSAGE, CURSOR_CONFIRM),
    # The CONFIRM EVALUATE runs before READ-ACCTDAT-FILE, so an unknown account is not reached.
    EditCase(UNKNOWN_ACCT_ID, "X", HTTP_EDIT_FAILURE, INVALID_CONFIRM_MESSAGE, CURSOR_CONFIRM),
]
EDIT_CASE_IDS = [
    "empty-acct-id-400",
    "spaces-acct-id-400",
    "unknown-acct-id-404",
    "nonnumeric-acct-id-404",
    "mixed-acct-id-404",
    "short-acct-id-404",
    "ten-digit-acct-id-404",
    "nonnumeric-acct-id-before-the-prompt-404",
    "invalid-confirm-400",
    "invalid-confirm-before-account-read-400",
]


def FixedTimestamp() -> datetime:
    """Stand in for GET-CURRENT-TIMESTAMP (lines 249-267): one whole-second instant."""
    return FIXED_TIMESTAMP


def ReadAccount(syncEngine: Engine, acctId: str) -> dict[str, Any]:
    """Return the committed ACCOUNT-RECORD of ``acctId`` as a column-name dictionary."""
    with syncEngine.connect() as connection:
        return dict(connection.execute(ACCOUNT_QUERY, {"acctId": acctId}).mappings().one())


def SetBalance(syncEngine: Engine, acctId: str, amount: Decimal) -> None:
    """Commit ``amount`` as the ACCT-CURR-BAL of ``acctId``."""
    with syncEngine.begin() as connection:
        connection.execute(SET_BALANCE_STATEMENT, {"amount": amount, "acctId": acctId})


def ReadTransactions(syncEngine: Engine) -> list[dict[str, Any]]:
    """Return every committed TRAN-RECORD with the payment record's columns, in TRAN-ID order."""
    with syncEngine.connect() as connection:
        return [dict(row) for row in connection.execute(TRANSACTIONS_QUERY).mappings()]


def FailingStub(error: RepositoryError) -> mock.AsyncMock:
    """Return a repository method stand-in that raises the file condition ``error``."""
    return mock.AsyncMock(side_effect=error)


def ErrorOutcome(error: LegacyMessageError) -> tuple[str, int, str]:
    """Return the sent error body's message and cursor field, with the HTTP status.

    A legacy error body holds the four standard keys only, so CURBAL never travels with a
    failure; the balance a failure leaves is read back from accounts.accounts instead.
    """
    body = error.Body()
    return body["message"], error.httpStatus, body["cursor_field"]


async def Pay(scenario: BillPayScenario, acctId: str, confirm: str) -> BillPayResponse:
    """Press ENTER on COBIL00 with ACTIDIN and CONFIRM as keyed."""
    request = BillPayRequest(acct_id=acctId, confirm=confirm)
    return await cobil00c.PayBill(scenario.context, request)


async def PayExpectingError(
    scenario: BillPayScenario, acctId: str, confirm: str
) -> LegacyMessageError:
    """Pay as Pay does and return the legacy error the screen is sent with."""
    with pytest.raises(LegacyMessageError) as raised:
        await Pay(scenario, acctId, confirm)
    return raised.value


def AssertNothingWritten(scenario: BillPayScenario, accountBefore: dict[str, Any]) -> None:
    """Assert the account record is as it was before ENTER and no transaction exists."""
    assert ReadAccount(scenario.syncEngine, SEED_ACCT_ID) == accountBefore
    assert ReadTransactions(scenario.syncEngine) == []


@pytest_asyncio.fixture(name="scenario")
async def BillPayScenarioFixture(
    makeOnlineContext: Callable[..., Awaitable[Any]],
    syncEngine: Engine,
    restoreSeedState: None,
    monkeypatch: pytest.MonkeyPatch,
) -> BillPayScenario:
    """Return a regular user's context with the clock frozen; the seed is restored after."""
    monkeypatch.setattr(cobil00c, "CurrentTimestamp", FixedTimestamp)
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    return BillPayScenario(context, syncEngine)


# --- PROCESS-ENTER-KEY edits: first failure only, nothing written ------------------------------
@pytest.mark.parametrize("editCase", EDIT_CASES, ids=EDIT_CASE_IDS)
async def test_edit_failure_is_sent_before_any_write(scenario, editCase):
    """Blank ACTIDIN, NOTFND for unknown and malformed keys, and an invalid CONFIRM."""
    accountBefore = ReadAccount(scenario.syncEngine, SEED_ACCT_ID)
    error = await PayExpectingError(scenario, editCase.acctId, editCase.confirm)
    expectedOutcome = (editCase.message, editCase.httpStatus, editCase.cursorField)
    assert ErrorOutcome(error) == expectedOutcome
    AssertNothingWritten(scenario, accountBefore)


async def test_account_read_failure_is_unable_to_lookup_account(scenario, monkeypatch):
    """READ-ACCTDAT-FILE WHEN OTHER (lines 365-371), the batch window's NOTOPEN included."""
    readStub = FailingStub(FileNotOpenError(ACCOUNT_FILE_NAME))
    monkeypatch.setattr(AccountRepository, "GetAccountForUpdate", readStub)
    accountBefore = ReadAccount(scenario.syncEngine, SEED_ACCT_ID)
    error = await PayExpectingError(scenario, SEED_ACCT_ID, "Y")
    expectedOutcome = (ACCOUNT_LOOKUP_FAILED_MESSAGE, HTTP_FILE_ERROR, CURSOR_ACCT_ID)
    assert ErrorOutcome(error) == expectedOutcome
    readStub.assert_awaited_once()
    AssertNothingWritten(scenario, accountBefore)


@pytest.mark.parametrize(
    ("balance", "confirm"),
    [(PAID_BALANCE, "Y"), (NEGATIVE_BALANCE, "Y"), (NEGATIVE_BALANCE, "")],
    ids=["zero-confirmed", "negative-confirmed", "negative-before-the-prompt"],
)
async def test_balance_not_above_zero_has_nothing_to_pay(scenario, balance, confirm):
    """ACCT-CURR-BAL <= ZEROS (lines 197-205) refuses the payment and the confirmation prompt."""
    SetBalance(scenario.syncEngine, SEED_ACCT_ID, balance)
    accountBefore = ReadAccount(scenario.syncEngine, SEED_ACCT_ID)
    error = await PayExpectingError(scenario, SEED_ACCT_ID, confirm)
    expectedOutcome = (NOTHING_TO_PAY_MESSAGE, HTTP_EDIT_FAILURE, CURSOR_ACCT_ID)
    assert ErrorOutcome(error) == expectedOutcome
    AssertNothingWritten(scenario, accountBefore)


# --- The confirmation flow (lines 173-191, 236-240) --------------------------------------------
@pytest.mark.parametrize("confirm", ["", " "], ids=["empty", "space"])
async def test_blank_confirm_asks_for_confirmation_with_the_balance(scenario, confirm):
    """CONF-PAY-NO: the prompt is a screen sent with CURBAL and the cursor on CONFIRM."""
    accountBefore = ReadAccount(scenario.syncEngine, SEED_ACCT_ID)
    response = await Pay(scenario, SEED_ACCT_ID, confirm)
    assert response == BillPayResponse(
        current_balance=SEED_BALANCE,
        message=CONFIRM_PAYMENT_MESSAGE,
        cursor_field=CURSOR_CONFIRM,
        tran_id=None,
    )
    AssertNothingWritten(scenario, accountBefore)


# COBIL00C lines 177-180 PERFORM CLEAR-CURRENT-SCREEN for N and n, and INITIALIZE-ALL-FIELDS
# blanks WS-MESSAGE and CURBAL: N is pinned as the cleared screen, not as the confirmation prompt.
@pytest.mark.parametrize("confirm", ["N", "n"])
async def test_no_confirm_clears_the_screen(scenario, confirm):
    """CLEAR-CURRENT-SCREEN: every field and the message blank, the cursor on ACTIDIN."""
    accountBefore = ReadAccount(scenario.syncEngine, SEED_ACCT_ID)
    response = await Pay(scenario, SEED_ACCT_ID, confirm)
    assert response == BillPayResponse(
        current_balance=None, message="", cursor_field=CURSOR_ACCT_ID, tran_id=None
    )
    AssertNothingWritten(scenario, accountBefore)


# --- Payments (lines 210-235, 523-532) ---------------------------------------------------------
@pytest.mark.parametrize("confirm", ["Y", "y"])
async def test_confirmed_payment_writes_the_bill_payment_record(scenario, confirm):
    """The whole balance is paid on the xref card, and the account rewritten at 0.00."""
    accountBefore = ReadAccount(scenario.syncEngine, SEED_ACCT_ID)
    response = await Pay(scenario, SEED_ACCT_ID, confirm)
    assert response == BillPayResponse(
        current_balance=None,
        message=FIRST_PAYMENT_MESSAGE,
        cursor_field=CURSOR_ACCT_ID,
        tran_id=FIRST_TRAN_ID,
    )
    accountAfter = ReadAccount(scenario.syncEngine, SEED_ACCT_ID)
    assert accountAfter == {**accountBefore, "curr_balance": PAID_BALANCE}
    assert ReadTransactions(scenario.syncEngine) == [PAYMENT_RECORD]


async def test_width_boundary_payment_drops_the_high_order_digit(scenario):
    """TRAN-AMT S9(09)V99 keeps 0.50 of 1000000000.50, so the balance keeps 1000000000.00."""
    SetBalance(scenario.syncEngine, SEED_ACCT_ID, WIDE_BALANCE)
    response = await Pay(scenario, SEED_ACCT_ID, "Y")
    assert (response.message, response.tran_id) == (FIRST_PAYMENT_MESSAGE, FIRST_TRAN_ID)
    accountAfter = ReadAccount(scenario.syncEngine, SEED_ACCT_ID)
    assert accountAfter["curr_balance"] == WIDE_BALANCE_AFTER
    expectedRecord = {**PAYMENT_RECORD, "tran_amt": WIDE_TRAN_AMT}
    assert ReadTransactions(scenario.syncEngine) == [expectedRecord]


async def test_duplicate_key_write_still_reduces_and_rewrites_the_balance(scenario, monkeypatch):
    """WRITE DUPREC (lines 533-539) is the last literal; the COMPUTE and REWRITE still run."""
    insertStub = FailingStub(DuplicateKeyError(TRANSACTION_FILE_NAME))
    monkeypatch.setattr(TransactionRepository, "InsertTransaction", insertStub)
    error = await PayExpectingError(scenario, SEED_ACCT_ID, "Y")
    expectedOutcome = (TRAN_ID_EXISTS_MESSAGE, HTTP_RECORD_CHANGED, CURSOR_ACCT_ID)
    assert ErrorOutcome(error) == expectedOutcome
    insertStub.assert_awaited_once()
    assert ReadTransactions(scenario.syncEngine) == []
    accountAfter = ReadAccount(scenario.syncEngine, SEED_ACCT_ID)
    assert accountAfter["curr_balance"] == PAID_BALANCE


async def test_failed_rewrite_keeps_the_committed_transaction(scenario, monkeypatch):
    """REWRITE WHEN OTHER (lines 396-402) replaces the success literal; the WRITE stays."""
    rewriteStub = FailingStub(FileIoError(ACCOUNT_FILE_NAME))
    monkeypatch.setattr(AccountRepository, "UpdateAccount", rewriteStub)
    accountBefore = ReadAccount(scenario.syncEngine, SEED_ACCT_ID)
    error = await PayExpectingError(scenario, SEED_ACCT_ID, "Y")
    expectedOutcome = (UPDATE_ACCOUNT_FAILED_MESSAGE, HTTP_FILE_ERROR, CURSOR_ACCT_ID)
    assert ErrorOutcome(error) == expectedOutcome
    rewriteStub.assert_awaited_once()
    assert ReadTransactions(scenario.syncEngine) == [PAYMENT_RECORD]
    assert ReadAccount(scenario.syncEngine, SEED_ACCT_ID) == accountBefore
