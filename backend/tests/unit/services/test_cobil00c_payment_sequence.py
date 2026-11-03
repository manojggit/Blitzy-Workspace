"""Unit tests for the COBIL00C (CB00) payment sequence when the TRAN-ID allocation fails.

cbl/COBIL00C.cbl PROCESS-ENTER-KEY runs the IF CONF-PAY-YES branch (lines 210-235) with no
ERR-FLG test between its steps: READ-CXACAIX-FILE, STARTBR/READPREV/ENDBR and ADD 1 (lines
212-217), WRITE-TRANSACT-FILE (line 233), the COMPUTE (line 234) and UPDATE-ACCTDAT-FILE (line
235). The final SEND-BILLPAY-SCREEN (line 242) shows the last WS-MESSAGE set on COBIL00. A failed
browse sets ``Unable to lookup Transaction...`` (lines 460-466, 489-495) and the sequence goes
on, with no TRAN-ID allocated, so the WRITE fails and its WHEN OTHER literal follows (lines
540-546). Expected values are typed from those lines and from the seed, where account
00000000004 holds 40.00 and no transaction exists:
- an allocation raising IOERR or RECORDBUSY, a real allocation-lock wait that times out inside
    the TRANSACT unit, and a TRANSACT unit that cannot open: 503
    ``Unable to Add Bill pay Transaction...`` on ACTIDIN, the balance still reduced to 0.00 and
    rewritten, and no transaction stored;
- a failed allocation and then a failed REWRITE (lines 396-402): 503
    ``Unable to Update Account...``, the balance still 40.00;
- a duplicate-key WRITE (lines 533-539), from the ID after 9999999999999999 wrapping to an
    existing 0000000000000000: 409 ``Tran ID already exist...``, the balance still reduced;
- a good payment (lines 523-532): ``Payment successful.  Your Transaction ID is
    0000000000000001.``, with the whole balance as the amount.

Each test pays through cobil00c.PayBill against the migrated, seeded test database, with the
clock frozen, and the seed is restored afterwards.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from functools import partial
from typing import NamedTuple
from unittest import mock

import pytest
import pytest_asyncio
from sqlalchemy import Engine, insert, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.database import WriteUnit
from app.core.dependencies import OnlineContext
from app.core.errors import FileIoError, LegacyMessageError, RecordLockError, RepositoryError
from app.models import Transaction
from app.repositories.account_repository import ACCOUNT_FILE_NAME, AccountRepository
from app.repositories.transaction_repository import (
    TRAN_ID_ALLOCATION_LOCK_STATEMENT,
    TRANSACTION_FILE_NAME,
    TransactionRepository,
)
from app.schemas.billpay import BillPayRequest, BillPayResponse
from app.services.billpay import cobil00c

# The seed account paid in full, its ACCT-CURR-BAL before and after the COMPUTE.
PAYING_ACCT_ID = "00000000004"
SEED_BALANCE = Decimal("40.00")
PAID_BALANCE = Decimal("0.00")
CONFIRM_YES = "Y"
REGULAR_USER_TYPE = "U"
FIXED_TIMESTAMP = datetime(2026, 11, 3, 9, 30, 15)

# The WS-MESSAGE literals of lines 543, 399, 536 and 527-530, and MOVE -1 TO ACTIDINL.
ADD_PAYMENT_FAILED_MESSAGE = "Unable to Add Bill pay Transaction..."
UPDATE_ACCOUNT_FAILED_MESSAGE = "Unable to Update Account..."
TRAN_ID_EXISTS_MESSAGE = "Tran ID already exist..."
FIRST_PAYMENT_MESSAGE = "Payment successful.  Your Transaction ID is 0000000000000001."
ACCT_ID_CURSOR_FIELD = "acct_id"
FILE_ERROR_STATUS = 503
DUPLICATE_KEY_STATUS = 409

# TRAN-ID 9(16): the first ID of an empty file, and the wrap of ADD 1 past the highest ID.
FIRST_TRAN_ID = "0000000000000001"
HIGHEST_TRAN_ID = "9999999999999999"
WRAPPED_TRAN_ID = "0000000000000000"
EXISTING_TRAN_AMT = Decimal("1.00")
EXISTING_TRAN_FIELDS = {
    "tran_type_cd": "02",
    "tran_cat_cd": 2,
    "merchant_id": "999999999",
    "tran_amt": EXISTING_TRAN_AMT,
}

# The real allocation's pg_advisory_xact_lock waits this long behind another holder, not 5s.
SHORT_LOCK_TIMEOUT_SQL = text("SET LOCAL lock_timeout = '100ms'")
BALANCE_QUERY = text("SELECT curr_balance FROM accounts.accounts WHERE acct_id = :acctId")
XREF_CARD_QUERY = text("SELECT card_num FROM xref.card_xref WHERE acct_id = :acctId")
STORED_TRANSACTIONS_QUERY = text(
    "SELECT tran_id, tran_amt FROM transactions.transactions ORDER BY tran_id"
)

REAL_ALLOCATE_NEXT_TRAN_ID = TransactionRepository.AllocateNextTranId


class PaymentHarness(NamedTuple):
    """The paying context, the test's monkeypatch, and spies on the WRITE and the REWRITE."""

    context: OnlineContext
    monkeypatch: pytest.MonkeyPatch
    insertSpy: mock.MagicMock
    updateSpy: mock.MagicMock


class AllocationFailure(NamedTuple):
    """A way the TRAN-ID allocation fails, and how many WRITEs then reach TRANSACT."""

    install: Callable[[pytest.MonkeyPatch], None]
    writeAttempts: int


def FixedTimestamp() -> datetime:
    """Stand in for GET-CURRENT-TIMESTAMP (lines 249-267) with one whole-second instant."""
    return FIXED_TIMESTAMP


def SpyOn(monkeypatch: pytest.MonkeyPatch, owner: type, methodName: str) -> mock.MagicMock:
    """Replace owner.methodName by a spy that records each call and runs the real method."""
    realMethod = getattr(owner, methodName)
    spy = mock.create_autospec(realMethod, side_effect=realMethod)
    monkeypatch.setattr(owner, methodName, spy)
    return spy


@pytest_asyncio.fixture(name="payment")
async def PaymentHarnessFixture(
    makeOnlineContext: Callable[..., Awaitable[OnlineContext]], monkeypatch: pytest.MonkeyPatch
) -> PaymentHarness:
    """Return a regular user's context, with the clock frozen and the WRITE and REWRITE spied."""
    monkeypatch.setattr(cobil00c, "CurrentTimestamp", FixedTimestamp)
    insertSpy = SpyOn(monkeypatch, TransactionRepository, "InsertTransaction")
    updateSpy = SpyOn(monkeypatch, AccountRepository, "UpdateAccount")
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    return PaymentHarness(context, monkeypatch, insertSpy, updateSpy)


def RaiseOnAllocation(errorType: type[RepositoryError], monkeypatch: pytest.MonkeyPatch) -> None:
    """Make AllocateNextTranId raise errorType on TRANSACT before it runs any statement."""
    failure = mock.AsyncMock(side_effect=errorType(TRANSACTION_FILE_NAME))
    monkeypatch.setattr(TransactionRepository, "AllocateNextTranId", failure)


async def AllocateWhileLockIsHeld(
    repository: TransactionRepository, transactionUnit: AsyncConnection
) -> str:
    """Run the real allocation while another connection holds the TRAN-ID allocation lock.

    The wait ends in SQLSTATE 55P03, which aborts the unit's transaction before the WRITE.
    """
    engine: AsyncEngine = repository.context.engine
    async with engine.connect() as lockHolder:
        await lockHolder.execute(TRAN_ID_ALLOCATION_LOCK_STATEMENT)
        await transactionUnit.execute(SHORT_LOCK_TIMEOUT_SQL)
        return await REAL_ALLOCATE_NEXT_TRAN_ID(repository, transactionUnit)


def TimeOutAllocationLock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every allocation wait for the allocation lock past its lock timeout."""
    monkeypatch.setattr(TransactionRepository, "AllocateNextTranId", AllocateWhileLockIsHeld)


@asynccontextmanager
async def WriteUnitFailingToOpenTransact(
    engine: AsyncEngine, fileName: str
) -> AsyncIterator[AsyncConnection]:
    """Open an ACCTDAT unit as WriteUnit does; fail entering a TRANSACT unit with IOERR."""
    if fileName == TRANSACTION_FILE_NAME:
        raise FileIoError(fileName)
    async with WriteUnit(engine, fileName) as unit:
        yield unit


def RefuseTransactUnit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make cobil00c's TRANSACT write unit fail to open, before any allocation."""
    monkeypatch.setattr(cobil00c, "WriteUnit", WriteUnitFailingToOpenTransact)


ALLOCATION_FAILURES = [
    AllocationFailure(partial(RaiseOnAllocation, FileIoError), 1),
    AllocationFailure(partial(RaiseOnAllocation, RecordLockError), 1),
    AllocationFailure(TimeOutAllocationLock, 1),
    AllocationFailure(RefuseTransactUnit, 0),
]
ALLOCATION_FAILURE_IDS = [
    "allocation-ioerr",
    "allocation-recordbusy",
    "allocation-lock-wait-timed-out",
    "transact-unit-cannot-open",
]


async def PayConfirmed(context: OnlineContext) -> BillPayResponse:
    """Press ENTER on COBIL00 with the paying account and confirm ``Y``."""
    request = BillPayRequest(acct_id=PAYING_ACCT_ID, confirm=CONFIRM_YES)
    return await cobil00c.PayBill(context, request)


async def PayExpectingError(context: OnlineContext) -> LegacyMessageError:
    """Pay as PayConfirmed does and return the legacy error the screen is sent with."""
    with pytest.raises(LegacyMessageError) as raised:
        await PayConfirmed(context)
    return raised.value


def ScreenOutcome(error: LegacyMessageError) -> tuple[str, int, str]:
    """Return the error's message literal, HTTP status and cursor field."""
    return error.message, error.httpStatus, error.field


def PersistedBalance(syncEngine: Engine) -> Decimal:
    """Return the paying account's committed ACCT-CURR-BAL."""
    with syncEngine.connect() as connection:
        return connection.execute(BALANCE_QUERY, {"acctId": PAYING_ACCT_ID}).scalar_one()


def StoredTransactions(syncEngine: Engine) -> list[tuple[str, Decimal]]:
    """Return every committed transaction's TRAN-ID and TRAN-AMT, in TRAN-ID order."""
    with syncEngine.connect() as connection:
        return [tuple(row) for row in connection.execute(STORED_TRANSACTIONS_QUERY)]


def StoreExistingTransactions(syncEngine: Engine, tranIds: tuple[str, ...]) -> None:
    """Commit one transaction of EXISTING_TRAN_AMT per TRAN-ID, on the paying account's card."""
    with syncEngine.begin() as connection:
        cardNum = connection.execute(XREF_CARD_QUERY, {"acctId": PAYING_ACCT_ID}).scalar_one()
        rows = [
            {**EXISTING_TRAN_FIELDS, "tran_id": tranId, "card_num": cardNum} for tranId in tranIds
        ]
        connection.execute(insert(Transaction), rows)


@pytest.mark.parametrize("allocationFailure", ALLOCATION_FAILURES, ids=ALLOCATION_FAILURE_IDS)
async def test_failed_allocation_still_writes_computes_and_rewrites(
    payment, syncEngine, allocationFailure
):
    """The WRITE's WHEN OTHER literal is the last; the COMPUTE and REWRITE still pay it all."""
    allocationFailure.install(payment.monkeypatch)
    error = await PayExpectingError(payment.context)
    expectedOutcome = (ADD_PAYMENT_FAILED_MESSAGE, FILE_ERROR_STATUS, ACCT_ID_CURSOR_FIELD)
    assert ScreenOutcome(error) == expectedOutcome
    assert payment.insertSpy.await_count == allocationFailure.writeAttempts
    payment.updateSpy.assert_awaited_once()
    assert PersistedBalance(syncEngine) == PAID_BALANCE
    assert StoredTransactions(syncEngine) == []


async def test_failed_allocation_then_failed_rewrite_shows_the_rewrite_literal(payment, syncEngine):
    """REWRITE WHEN OTHER (lines 396-402) is the last literal; the balance stays as read."""
    RaiseOnAllocation(FileIoError, payment.monkeypatch)
    rewriteFailure = mock.AsyncMock(side_effect=FileIoError(ACCOUNT_FILE_NAME))
    payment.monkeypatch.setattr(AccountRepository, "UpdateAccount", rewriteFailure)
    error = await PayExpectingError(payment.context)
    expectedOutcome = (UPDATE_ACCOUNT_FAILED_MESSAGE, FILE_ERROR_STATUS, ACCT_ID_CURSOR_FIELD)
    assert ScreenOutcome(error) == expectedOutcome
    payment.insertSpy.assert_awaited_once()
    rewriteFailure.assert_awaited_once()
    assert PersistedBalance(syncEngine) == SEED_BALANCE
    assert StoredTransactions(syncEngine) == []


async def test_duplicate_key_write_still_reduces_the_balance(payment, syncEngine):
    """The ID after 9999999999999999 wraps to the stored 0000000000000000: DUPREC, then pay."""
    StoreExistingTransactions(syncEngine, (HIGHEST_TRAN_ID, WRAPPED_TRAN_ID))
    error = await PayExpectingError(payment.context)
    expectedOutcome = (TRAN_ID_EXISTS_MESSAGE, DUPLICATE_KEY_STATUS, ACCT_ID_CURSOR_FIELD)
    assert ScreenOutcome(error) == expectedOutcome
    payment.updateSpy.assert_awaited_once()
    assert PersistedBalance(syncEngine) == PAID_BALANCE
    assert StoredTransactions(syncEngine) == [
        (WRAPPED_TRAN_ID, EXISTING_TRAN_AMT),
        (HIGHEST_TRAN_ID, EXISTING_TRAN_AMT),
    ]


async def test_good_payment_answers_the_success_literal(payment, syncEngine):
    """The first payment of an empty TRANSACT is 0000000000000001, for the whole balance."""
    response = await PayConfirmed(payment.context)
    assert (response.message, response.tran_id) == (FIRST_PAYMENT_MESSAGE, FIRST_TRAN_ID)
    payment.updateSpy.assert_awaited_once()
    assert PersistedBalance(syncEngine) == PAID_BALANCE
    assert StoredTransactions(syncEngine) == [(FIRST_TRAN_ID, SEED_BALANCE)]
