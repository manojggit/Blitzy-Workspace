"""Integration tests for per-write durability of CAUP (COACTUPC) and CB00 (COBIL00C).

Every CardDemo file is RECOVERY(NONE) in csd/CARDDEMO.CSD (lines 9, 21, 33, 46, 59, 72, 84 and
96), so a completed VSAM write survives a later failure of the same task and SYNCPOINT ROLLBACK
undoes nothing. The port keeps this with one app.core.database.WriteUnit per record write, as
the Technical Specification, Section 0.6.12, requires:

- CAUP, cbl/COACTUPC.cbl 9600-WRITE-PROCESSING (lines 3888-4104): the ACCTDAT rewrite commits
  before the CUSTDAT rewrite starts;
- CB00, cbl/COBIL00C.cbl lines 210-240: the payment WRITE to TRANSACT commits before the
  ACCTDAT REWRITE.

Each interruption case stops the request itself between those two writes, at the repository
call of the second one, instead of raising an error the service would catch:

- terminated-backend: the patched call ends the PostgreSQL server process of its write unit
  with pg_terminate_backend and only then runs, so the second write meets a lost connection;
- cancelled-task: the patched call hangs, and the test cancels the task sending the request.

Afterwards the first write is committed, the second is absent, no row lock is left behind and
the same app still serves a request. The control cases send the same requests unpatched and
commit both writes, which shows the patches cause those outcomes. Clients, the test database
and the seed restore are the fixtures of tests/conftest.py.
"""

import asyncio
import functools
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.repositories.account_repository import AccountRepository
from app.repositories.customer_repository import CustomerRepository

pytestmark = pytest.mark.usefixtures("restoreSeedState")

# --- HTTP ----------------------------------------------------------------------------------------
API_V1 = "/api/v1"
ACCOUNTS_PATH = API_V1 + "/accounts"
BILLPAY_PATH = API_V1 + "/billpay"
HTTP_OK = 200

# --- Sample data: data/EBCDIC ACCTDATA, CARDXREF and CUSTDATA images -----------------------------
# Account 00000000004 has one card, 2988091353094312, and a seed balance of 40.00.
SAMPLE_ACCOUNT_ID = "00000000004"
SAMPLE_CARD_NUM = "2988091353094312"
SEED_BALANCE = Decimal("40.00")

# --- CAUP (COACTUPC): one ACCTDAT field and one CUSTDAT field changed ----------------------------
CHANGED_CREDIT_LIMIT = Decimal("2000.00")
CHANGED_FIRST_NAME = "Durable"
ACCOUNT_CHANGES = MappingProxyType({"credit_limit": "2000.00", "first_name": CHANGED_FIRST_NAME})
ENTER_ACTION = "enter"
SAVE_ACTION = "save"
CHANGES_VALIDATED_STATE = "CHANGES_OK_NOT_CONFIRMED"
# The COACTUPC information line of a completed 9600-WRITE-PROCESSING.
CHANGES_COMMITTED_MESSAGE = "Changes committed to database"
# Values every 1200-EDIT-MAP-INPUTS edit passes; the sample account's seed customer fails some,
# such as its state and ZIP pair (MI with a ZIP starting 39).
EDITABLE_ACCOUNT_VALUES = MappingProxyType(
    {
        "active_status": "Y",
        "open_date": date(2012, 11, 17),
        "expiration_date": date(2023, 12, 16),
        "reissue_date": date(2023, 12, 16),
    }
)
EDITABLE_CUSTOMER_VALUES = MappingProxyType(
    {
        "first_name": "Original",
        "middle_name": "Kept",
        "last_name": "Holder",
        "addr_line_1": "1 Durability Way",
        "addr_line_3": "Albany",
        "addr_state_cd": "NY",
        "addr_country_cd": "USA",
        "addr_zip": "10001",
        "phone_num_1": "(212)555-0100",
        "phone_num_2": "(212)555-0100",
        "ssn": "123456789",
        "date_of_birth": date(1985, 1, 13),
        "eft_account_id": "0040802739",
        "primary_holder_ind": "Y",
        "fico_credit_score": 700,
    }
)

# --- CB00 (COBIL00C): the confirmed payment and the transaction it writes ------------------------
PAYMENT_BODY = MappingProxyType({"acct_id": SAMPLE_ACCOUNT_ID, "confirm": "Y"})
PAYMENT_SUCCESS_TEXT = "Payment successful."
PAID_BALANCE = Decimal("0.00")
# TRAN-TYPE-CD, TRAN-DESC, TRAN-AMT (the whole balance) and TRAN-CARD-NUM of lines 219-225.
EXPECTED_PAYMENT_ROW = ("02", "BILL PAYMENT - ONLINE", SEED_BALANCE, SAMPLE_CARD_NUM)

# --- SQL: literal statements with bound values only ----------------------------------------------
CUSTOMER_OF_ACCOUNT_SQL = "SELECT cust_id FROM xref.card_xref WHERE acct_id = :acctId"
MAKE_ACCOUNT_EDITABLE_SQL = (
    "UPDATE accounts.accounts SET active_status = :active_status, open_date = :open_date,"
    " expiration_date = :expiration_date, reissue_date = :reissue_date"
    " WHERE acct_id = :acct_id"
)
MAKE_CUSTOMER_EDITABLE_SQL = (
    "UPDATE customers.customers SET first_name = :first_name, middle_name = :middle_name,"
    " last_name = :last_name, addr_line_1 = :addr_line_1, addr_line_3 = :addr_line_3,"
    " addr_state_cd = :addr_state_cd, addr_country_cd = :addr_country_cd,"
    " addr_zip = :addr_zip, phone_num_1 = :phone_num_1, phone_num_2 = :phone_num_2,"
    " ssn = :ssn, date_of_birth = :date_of_birth, eft_account_id = :eft_account_id,"
    " primary_holder_ind = :primary_holder_ind, fico_credit_score = :fico_credit_score"
    " WHERE cust_id = :cust_id"
)
ACCOUNT_STATE_SQL = (
    "SELECT credit_limit, curr_balance FROM accounts.accounts WHERE acct_id = :acctId"
)
CUSTOMER_STATE_SQL = "SELECT * FROM customers.customers WHERE cust_id = :custId"
TRANSACTION_ROWS_SQL = (
    "SELECT tran_type_cd, tran_desc, tran_amt, card_num FROM transactions.transactions"
    " ORDER BY tran_id"
)
BACKEND_PID_SQL = "SELECT pg_backend_pid()"
# With a timeout, pg_terminate_backend returns only once the process has exited (true) or the
# timeout has passed (false), so the patched call never races the termination.
TERMINATE_BACKEND_SQL = "SELECT pg_terminate_backend(:backendPid, :timeoutMilliseconds)"
TERMINATE_TIMEOUT_MILLISECONDS = 5000

# --- Row-lock probes: the only tables and key columns a probe may name ---------------------------
ACCOUNTS_TABLE_NAME = "accounts.accounts"
CUSTOMERS_TABLE_NAME = "customers.customers"
LOCKABLE_TABLES = MappingProxyType(
    {
        ACCOUNTS_TABLE_NAME: (
            "SELECT 1 FROM accounts.accounts WHERE acct_id = :key FOR UPDATE NOWAIT"
        ),
        CUSTOMERS_TABLE_NAME: (
            "SELECT 1 FROM customers.customers WHERE cust_id = :key FOR UPDATE NOWAIT"
        ),
    }
)
LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"
UNLOCK_TIMEOUT_SECONDS = 10.0
UNLOCK_POLL_SECONDS = 0.1
GATE_TIMEOUT_SECONDS = 30.0

# --- Failure messages ----------------------------------------------------------------------------
GATE_NOT_REACHED_MESSAGE = "the request never reached the patched second write"
TERMINATION_UNCONFIRMED_MESSAGE = "server process {backendPid} did not exit within the timeout"
ROW_STILL_LOCKED_MESSAGE = "{table} row {key!r} is still locked after the request ended"

# A repository write method as the services call it: (repository, unit, values).
RepositoryCall = Callable[[Any, AsyncConnection, Mapping[str, Any]], Awaitable[None]]
# One HTTP request, sent when awaited.
RequestCall = Callable[[], Awaitable[httpx.Response]]


# --- Value objects -------------------------------------------------------------------------------
@dataclass
class Gate:
    """What the patched second write tells the test: that it ran, and the backend it ended."""

    reached: asyncio.Event = field(default_factory=asyncio.Event)
    pid: int | None = None


@dataclass(frozen=True)
class Interruption:
    """One way of stopping a request at its second write.

    ``wrapCall(original, syncEngine, gate)`` returns the replacement of the second write's
    repository method; ``driveRequest(requestCall, gate)`` sends the request and returns its
    response, or None when the request task was cancelled.
    """

    wrapCall: Callable[[RepositoryCall, Engine, Gate], RepositoryCall]
    driveRequest: Callable[[RequestCall, Gate], Awaitable[httpx.Response | None]]


# --- Paths and database helpers (sync, psycopg2) -------------------------------------------------
def AccountPath(acctId: str) -> str:
    """Return the CAUP update path of ``acctId``, ``PUT /api/v1/accounts/{acctId}``."""
    return f"{ACCOUNTS_PATH}/{acctId}"


def EditPath(acctId: str) -> str:
    """Return the CAUP fetch path of ``acctId``, ``GET /api/v1/accounts/{acctId}/edit``."""
    return AccountPath(acctId) + "/edit"


def MakeAccountEditable(syncEngine: Engine, acctId: str) -> str:
    """Commit values that pass every CAUP edit to the account and its customer; return its ID.

    The customer is the one the account's cross-reference names, as COACTUPC reads CXACAIX.
    """
    with syncEngine.begin() as connection:
        custId = connection.scalar(text(CUSTOMER_OF_ACCOUNT_SQL), {"acctId": acctId})
        accountValues = {**EDITABLE_ACCOUNT_VALUES, "acct_id": acctId}
        connection.execute(text(MAKE_ACCOUNT_EDITABLE_SQL), accountValues)
        customerValues = {**EDITABLE_CUSTOMER_VALUES, "cust_id": custId}
        connection.execute(text(MAKE_CUSTOMER_EDITABLE_SQL), customerValues)
    return custId


def ReadAccountState(syncEngine: Engine, acctId: str) -> dict[str, Any]:
    """Return the committed credit limit and balance of ``acctId``."""
    with syncEngine.connect() as connection:
        result = connection.execute(text(ACCOUNT_STATE_SQL), {"acctId": acctId})
        return dict(result.mappings().one())


def ReadCustomerState(syncEngine: Engine, custId: str) -> dict[str, Any]:
    """Return every committed column of customer ``custId``."""
    with syncEngine.connect() as connection:
        result = connection.execute(text(CUSTOMER_STATE_SQL), {"custId": custId})
        return dict(result.mappings().one())


def ReadTransactionRows(syncEngine: Engine) -> list[tuple[Any, ...]]:
    """Return the type, description, amount and card of every committed transaction."""
    with syncEngine.connect() as connection:
        return [tuple(row) for row in connection.execute(text(TRANSACTION_ROWS_SQL))]


def TerminateBackend(syncEngine: Engine, backendPid: int) -> None:
    """End server process ``backendPid`` and wait until it has exited; fail if it has not."""
    parameters = {"backendPid": backendPid, "timeoutMilliseconds": TERMINATE_TIMEOUT_MILLISECONDS}
    with syncEngine.begin() as connection:
        terminated = connection.scalar(text(TERMINATE_BACKEND_SQL), parameters)
    assert terminated, TERMINATION_UNCONFIRMED_MESSAGE.format(backendPid=backendPid)


def IsRowUnlocked(syncEngine: Engine, probeSql: str, key: str) -> bool:
    """Return True when the row ``key`` exists and could be locked at once, in its own transaction.

    Only SQLSTATE 55P03 (lock not available) means the row is still locked; any other database
    error is raised, and a missing row raises NoResultFound.
    """
    try:
        with syncEngine.begin() as connection:
            connection.execute(text(probeSql), {"key": key}).scalar_one()
    except OperationalError as error:
        if getattr(error.orig, "pgcode", None) != LOCK_NOT_AVAILABLE_SQLSTATE:
            raise
        return False
    return True


async def WaitUntilRowUnlocked(syncEngine: Engine, table: str, key: str) -> None:
    """Wait until the ``table`` row ``key`` is unlocked, probing every 0.1 s; fail after 10 s.

    ``table`` must be a LOCKABLE_TABLES name. Probes run off the event loop, so a lock the app
    still has to release on this loop is never held up by the wait.
    """
    probeSql = LOCKABLE_TABLES[table]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + UNLOCK_TIMEOUT_SECONDS
    while not await asyncio.to_thread(IsRowUnlocked, syncEngine, probeSql, key):
        if loop.time() >= deadline:
            pytest.fail(ROW_STILL_LOCKED_MESSAGE.format(table=table, key=key))
        await asyncio.sleep(UNLOCK_POLL_SECONDS)


# --- Interruptions of the second write -----------------------------------------------------------
def TerminatingCall(original: RepositoryCall, syncEngine: Engine, gate: Gate) -> RepositoryCall:
    """Return a write method that ends its unit's server process, then runs ``original``.

    The pid is read on the unit, the write unit's own pooled connection, and the termination
    runs on a psycopg2 connection of the same role. ``gate.pid`` is set only once the process
    has exited, so a failed termination cannot pass for an interrupted write.
    """

    async def TerminateThenWrite(
        repository: Any, unit: AsyncConnection, values: Mapping[str, Any]
    ) -> None:
        backendPid = await unit.scalar(text(BACKEND_PID_SQL))
        await asyncio.to_thread(TerminateBackend, syncEngine, backendPid)
        gate.pid = backendPid
        gate.reached.set()
        await original(repository, unit, values)

    return TerminateThenWrite


def HangingCall(gate: Gate) -> RepositoryCall:
    """Return a write method that signals ``gate`` and then waits until it is cancelled."""

    async def HangBeforeWrite(
        repository: Any, unit: AsyncConnection, values: Mapping[str, Any]
    ) -> None:
        gate.reached.set()
        await asyncio.Event().wait()

    return HangBeforeWrite


async def AwaitResponse(requestCall: RequestCall, gate: Gate) -> httpx.Response:
    """Send the request and return its response, once the second write's backend has ended."""
    response = await requestCall()
    assert gate.pid is not None, GATE_NOT_REACHED_MESSAGE
    return response


async def CancelAtGate(requestCall: RequestCall, gate: Gate) -> None:
    """Send the request as a task, cancel it once it hangs at the second write, then reap it.

    The wait gives up after 30 s, or as soon as the request ends without reaching the gate.
    """
    requestTask = asyncio.create_task(requestCall())
    reachedWaiter = asyncio.create_task(gate.reached.wait())
    finished, _ = await asyncio.wait(
        {requestTask, reachedWaiter},
        timeout=GATE_TIMEOUT_SECONDS,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if reachedWaiter not in finished:
        reachedWaiter.cancel()
        requestTask.cancel()
        pytest.fail(GATE_NOT_REACHED_MESSAGE)
    requestTask.cancel()
    with pytest.raises(asyncio.CancelledError):
        await requestTask


INTERRUPTIONS = [
    pytest.param(Interruption(TerminatingCall, AwaitResponse), id="terminated-backend"),
    pytest.param(
        Interruption(lambda original, syncEngine, gate: HangingCall(gate), CancelAtGate),
        id="cancelled-task",
    ),
]


# --- Request helpers and assertions --------------------------------------------------------------
@pytest_asyncio.fixture(name="interruptibleClient")
async def InterruptibleClientFixture(
    testApp: FastAPI, regularClient: httpx.AsyncClient
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client with regularClient's session that answers an unhandled app error as 500.

    httpx.ASGITransport re-raises an app's unhandled exception by default; an interrupted
    request may end with a legacy error status or with FastAPI's 500.
    """
    transport = httpx.ASGITransport(app=testApp, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=regularClient.base_url,
        headers=regularClient.headers,
        cookies=regularClient.cookies,
    ) as client:
        yield client


async def ValidatedAccountSave(
    client: httpx.AsyncClient, acctId: str, changes: Mapping[str, str]
) -> dict[str, Any]:
    """Fetch the CAUP screen, send ENTER with ``changes`` and return the F5 save request body.

    ENTER must validate the changes (CHANGES_OK_NOT_CONFIRMED), so the save writes them.
    """
    fetched = await client.get(EditPath(acctId))
    assert fetched.status_code == HTTP_OK, fetched.text
    fetchedScreen = fetched.json()
    enteredFields = {**fetchedScreen["fields"], **changes}
    enterBody = {
        "action": ENTER_ACTION,
        "screen_token": fetchedScreen["screen_token"],
        "fields": enteredFields,
    }
    validated = await client.put(AccountPath(acctId), json=enterBody)
    assert validated.status_code == HTTP_OK, validated.text
    validatedScreen = validated.json()
    assert validatedScreen["state"] == CHANGES_VALIDATED_STATE, validated.text
    return {**enterBody, "action": SAVE_ACTION, "screen_token": validatedScreen["screen_token"]}


def AssertNoCommitClaimed(response: httpx.Response | None, committedText: str) -> None:
    """Assert an interrupted request neither answered 200 nor claimed its writes were committed.

    A cancelled request has no response.
    """
    if response is None:
        return
    assert response.status_code != HTTP_OK, response.text
    assert committedText not in response.text


async def AssertSameAppStillServes(client: httpx.AsyncClient) -> None:
    """Assert the app's pool recovered: the CAUP fetch reads the account and its customer."""
    response = await client.get(EditPath(SAMPLE_ACCOUNT_ID))
    assert response.status_code == HTTP_OK, response.text


# --- CAUP (COACTUPC): between the ACCTDAT commit and the CUSTDAT rewrite -------------------------
@pytest.mark.parametrize("interruption", INTERRUPTIONS)
async def test_caup_account_write_survives_interrupted_customer_unit(
    interruptibleClient, syncEngine, monkeypatch, interruption
):
    """The committed account rewrite stays; the customer rewrite and both row locks are gone."""
    custId = MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    saveBody = await ValidatedAccountSave(interruptibleClient, SAMPLE_ACCOUNT_ID, ACCOUNT_CHANGES)
    accountBefore = ReadAccountState(syncEngine, SAMPLE_ACCOUNT_ID)
    customerBefore = ReadCustomerState(syncEngine, custId)
    gate = Gate()
    secondWrite = interruption.wrapCall(CustomerRepository.UpdateCustomer, syncEngine, gate)
    monkeypatch.setattr(CustomerRepository, "UpdateCustomer", secondWrite)
    saveCall = functools.partial(
        interruptibleClient.put, AccountPath(SAMPLE_ACCOUNT_ID), json=saveBody
    )
    saveResponse = await interruption.driveRequest(saveCall, gate)
    AssertNoCommitClaimed(saveResponse, CHANGES_COMMITTED_MESSAGE)
    assert accountBefore["credit_limit"] != CHANGED_CREDIT_LIMIT
    assert ReadAccountState(syncEngine, SAMPLE_ACCOUNT_ID)["credit_limit"] == CHANGED_CREDIT_LIMIT
    assert ReadCustomerState(syncEngine, custId) == customerBefore
    await WaitUntilRowUnlocked(syncEngine, ACCOUNTS_TABLE_NAME, SAMPLE_ACCOUNT_ID)
    await WaitUntilRowUnlocked(syncEngine, CUSTOMERS_TABLE_NAME, custId)
    await AssertSameAppStillServes(interruptibleClient)


# --- CB00 (COBIL00C): between the TRANSACT commit and the ACCTDAT rewrite ------------------------
@pytest.mark.parametrize("interruption", INTERRUPTIONS)
async def test_cb00_payment_transaction_survives_interrupted_account_rewrite(
    interruptibleClient, syncEngine, monkeypatch, interruption
):
    """The committed payment transaction stays; the balance rewrite and the row lock are gone."""
    assert ReadAccountState(syncEngine, SAMPLE_ACCOUNT_ID)["curr_balance"] == SEED_BALANCE
    assert ReadTransactionRows(syncEngine) == []
    gate = Gate()
    secondWrite = interruption.wrapCall(AccountRepository.UpdateAccount, syncEngine, gate)
    monkeypatch.setattr(AccountRepository, "UpdateAccount", secondWrite)
    payCall = functools.partial(interruptibleClient.post, BILLPAY_PATH, json=dict(PAYMENT_BODY))
    payResponse = await interruption.driveRequest(payCall, gate)
    AssertNoCommitClaimed(payResponse, PAYMENT_SUCCESS_TEXT)
    assert ReadTransactionRows(syncEngine) == [EXPECTED_PAYMENT_ROW]
    assert ReadAccountState(syncEngine, SAMPLE_ACCOUNT_ID)["curr_balance"] == SEED_BALANCE
    await WaitUntilRowUnlocked(syncEngine, ACCOUNTS_TABLE_NAME, SAMPLE_ACCOUNT_ID)
    await AssertSameAppStillServes(interruptibleClient)


# --- Controls: the same requests with no interruption commit both writes ------------------------
async def test_caup_save_without_interruption_commits_both_writes(interruptibleClient, syncEngine):
    """Unpatched, the save rewrites the account and then the customer."""
    custId = MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    saveBody = await ValidatedAccountSave(interruptibleClient, SAMPLE_ACCOUNT_ID, ACCOUNT_CHANGES)
    response = await interruptibleClient.put(AccountPath(SAMPLE_ACCOUNT_ID), json=saveBody)
    assert response.status_code == HTTP_OK, response.text
    assert response.json()["info_message"] == CHANGES_COMMITTED_MESSAGE
    assert ReadAccountState(syncEngine, SAMPLE_ACCOUNT_ID)["credit_limit"] == CHANGED_CREDIT_LIMIT
    assert ReadCustomerState(syncEngine, custId)["first_name"] == CHANGED_FIRST_NAME


async def test_cb00_payment_without_interruption_rewrites_the_balance(
    interruptibleClient, syncEngine
):
    """Unpatched, the payment writes the transaction and then rewrites the balance to zero."""
    response = await interruptibleClient.post(BILLPAY_PATH, json=dict(PAYMENT_BODY))
    assert response.status_code == HTTP_OK, response.text
    assert response.json()["message"].startswith(PAYMENT_SUCCESS_TEXT)
    assert ReadTransactionRows(syncEngine) == [EXPECTED_PAYMENT_ROW]
    assert ReadAccountState(syncEngine, SAMPLE_ACCOUNT_ID)["curr_balance"] == PAID_BALANCE
