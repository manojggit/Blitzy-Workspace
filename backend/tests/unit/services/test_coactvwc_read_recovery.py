"""Unit tests for the COACTVWC (CAVW) read chain after the ACCTDAT read fails on the server.

cbl/COACTVWC.cbl 9000-READ-ACCT (lines 687-722) performs 9300-GETACCTDATA-BYACCT and then
9400-GETCUSTDATA-BYCUST, because the SET of DID-NOT-FIND-ACCT-IN-ACCTDAT is commented out
(line 792). An ACCTDAT read ending WHEN OTHER (lines 809-816) moves WS-FILE-ERROR-MESSAGE to
WS-RETURN-MSG; a NORMAL CUSTDAT read (lines 836-838) leaves it, and only a CUSTDAT WHEN OTHER
(lines 858-865) replaces it. CICS files fail independently, so the customer is still read.

Both reads run on the request session. A SELECT that fails on the server aborts its
transaction, and a dropped connection leaves the session pending rollback, so these failures are
real: another connection holds ACCESS EXCLUSIVE on accounts.accounts, and the blocked account
SELECT ends by statement_timeout (SQLSTATE 57014) or by pg_terminate_backend of the session's
server process. Expected values are typed from the source:

- WS-FILE-ERROR-MESSAGE (lines 86-105) for READ on ACCTDAT with IOERR, RESP 17 and RESP2 0,
    cut to WS-RETURN-MSG X(75) and right-trimmed, with HTTP 503;
- the cross-reference and customer records of account 00000000001, as committed;
- the ViewAccount 503 body: that message, the 1200 information line, and ACCTSID as the
    failing field and the cursor.

The fallback of core.database.RecoverRequestSession, where the rollback itself fails, is tested
with a stub session, since it concerns only the session calls the helper makes.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.database import RecoverRequestSession
from app.core.dependencies import OnlineContext
from app.core.errors import LegacyMessageError
from app.services.accounts import coactvwc

# A seeded account whose CXACAIX record names customer 000000001 (data/ASCII/cardxref.txt).
ACCOUNT_ID = "00000000001"
# 9300 WHEN OTHER: 'READ' X(8), LIT-ACCTFILENAME X(9), WS-RESP-CD 17 and WS-REAS-CD 0 as X(10).
ACCOUNT_FILE_ERROR_MESSAGE = (
    "File Error: READ     on ACCTDAT   returned RESP 000000017 ,RESP2 000000000"
)
FILE_ERROR_STATUS = 503
# WS-PROMPT-FOR-INPUT (lines 113-114) and ACCTSID, the map's only input.
INFO_PROMPT_FOR_INPUT = "Enter or update id of account to display"
ACCOUNT_ID_FIELD = "acct_id"

LOCK_ACCOUNTS_SQL = "LOCK TABLE accounts.accounts IN ACCESS EXCLUSIVE MODE"
SHORT_STATEMENT_TIMEOUT_SQL = "SET LOCAL statement_timeout = '300ms'"
BACKEND_PID_SQL = "SELECT pg_backend_pid()"
BLOCKED_BY_LOCK_SQL = "SELECT cardinality(pg_blocking_pids(:backendPid)) > 0"
TERMINATE_BACKEND_SQL = "SELECT pg_terminate_backend(:backendPid)"
XREF_BY_ACCOUNT_SQL = "SELECT * FROM xref.card_xref WHERE acct_id = :acctId"
CUSTOMER_BY_ID_SQL = "SELECT * FROM customers.customers WHERE cust_id = :custId"
# The terminator polls for the blocked SELECT for at most LOCK_WAIT_POLLS * the interval.
LOCK_WAIT_POLLS = 200
LOCK_WAIT_POLL_SECONDS = 0.025
NEVER_BLOCKED_MESSAGE = "the account SELECT never waited on the accounts.accounts lock"


async def ReadCommittedRecords(asyncEngine: AsyncEngine) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the CXACAIX record of ACCOUNT_ID and the CUSTDAT record it names."""
    async with asyncEngine.connect() as reader:
        xrefResult = await reader.execute(text(XREF_BY_ACCOUNT_SQL), {"acctId": ACCOUNT_ID})
        xref = dict(xrefResult.mappings().one())
        customerResult = await reader.execute(text(CUSTOMER_BY_ID_SQL), {"custId": xref["cust_id"]})
        customer = dict(customerResult.mappings().one())
    return xref, customer


@contextlib.asynccontextmanager
async def LockedAccountsTable(asyncEngine: AsyncEngine) -> AsyncIterator[None]:
    """Hold ACCESS EXCLUSIVE on accounts.accounts on a connection of its own until exit.

    The rollback always runs, so the restore's TRUNCATE at teardown is never blocked.
    """
    async with asyncEngine.connect() as locker:
        await locker.execute(text(LOCK_ACCOUNTS_SQL))
        try:
            yield
        finally:
            await locker.rollback()


@contextlib.asynccontextmanager
async def StatementTimeout(context: OnlineContext, asyncEngine: AsyncEngine) -> AsyncIterator[None]:
    """Make the session's blocked account SELECT end with statement_timeout (57014)."""
    await context.session.execute(text(SHORT_STATEMENT_TIMEOUT_SQL))
    yield


async def TerminateWhenBlocked(asyncEngine: AsyncEngine, backendPid: int) -> None:
    """Terminate the server process ``backendPid`` once it waits on a lock another holds."""
    pidParameter = {"backendPid": backendPid}
    async with asyncEngine.connect() as monitor:
        for _ in range(LOCK_WAIT_POLLS):
            if await monitor.scalar(text(BLOCKED_BY_LOCK_SQL), pidParameter):
                await monitor.execute(text(TERMINATE_BACKEND_SQL), pidParameter)
                return
            await asyncio.sleep(LOCK_WAIT_POLL_SECONDS)
    pytest.fail(NEVER_BLOCKED_MESSAGE)


@contextlib.asynccontextmanager
async def TerminatedBackend(
    context: OnlineContext, asyncEngine: AsyncEngine
) -> AsyncIterator[None]:
    """Make the session's blocked account SELECT end by losing its connection.

    Reading the session's server process opens the session's transaction in that process, so
    the cross-reference and account reads run there too.
    """
    backendPid = await context.session.scalar(text(BACKEND_PID_SQL))
    async with asyncio.TaskGroup() as taskGroup:
        taskGroup.create_task(TerminateWhenBlocked(asyncEngine, backendPid))
        yield


# Each failure is entered with the context and the test engine, around the read chain.
ACCOUNT_READ_FAILURES = [StatementTimeout, TerminatedBackend]
ACCOUNT_READ_FAILURE_IDS = ["statement-timeout", "terminated-backend"]


@pytest.mark.parametrize("injectFailure", ACCOUNT_READ_FAILURES, ids=ACCOUNT_READ_FAILURE_IDS)
async def test_failed_account_read_keeps_its_message_and_still_reads_the_customer(
    makeOnlineContext, asyncEngine, injectFailure
):
    """9300 WHEN OTHER sets the ACCTDAT literal; the NORMAL 9400 read keeps it."""
    expectedXref, expectedCustomer = await ReadCommittedRecords(asyncEngine)
    context = await makeOnlineContext()
    async with LockedAccountsTable(asyncEngine), injectFailure(context, asyncEngine):
        readResult = await coactvwc.ReadAccountChain(context, ACCOUNT_ID)
    assert readResult.message == ACCOUNT_FILE_ERROR_MESSAGE
    assert readResult.httpStatus == FILE_ERROR_STATUS
    assert readResult.acctFilterOk is False
    assert readResult.xref == expectedXref
    assert readResult.account is None
    assert readResult.customerFound is True
    assert readResult.customer == expectedCustomer


@pytest.mark.parametrize("injectFailure", ACCOUNT_READ_FAILURES, ids=ACCOUNT_READ_FAILURE_IDS)
async def test_view_account_answers_the_account_file_error(
    makeOnlineContext, asyncEngine, injectFailure
):
    """The CAVW ENTER path sends the screen with the ACCTDAT literal, not a CUSTDAT one."""
    context = await makeOnlineContext()
    async with LockedAccountsTable(asyncEngine), injectFailure(context, asyncEngine):
        with pytest.raises(LegacyMessageError) as raised:
            await coactvwc.ViewAccount(context, ACCOUNT_ID)
    assert raised.value.httpStatus == FILE_ERROR_STATUS
    assert raised.value.Body() == {
        "message": ACCOUNT_FILE_ERROR_MESSAGE,
        "info_message": INFO_PROMPT_FOR_INPUT,
        "field_errors": [ACCOUNT_ID_FIELD],
        "cursor_field": ACCOUNT_ID_FIELD,
    }


# Rollback outcomes: none (the rollback succeeds), then failures of the dead connection's driver.
ROLLBACK_FAILURE_FACTORIES = {
    "rollback-succeeds": lambda: None,
    "wrapped-driver-error": lambda: DBAPIError("ROLLBACK", {}, OSError("connection reset")),
    "connection-closed": lambda: asyncpg.InterfaceError("connection is closed"),
}


@pytest.mark.parametrize(
    "failureFactory",
    list(ROLLBACK_FAILURE_FACTORIES.values()),
    ids=list(ROLLBACK_FAILURE_FACTORIES),
)
async def test_recover_request_session_invalidates_only_when_the_rollback_fails(failureFactory):
    """A failed rollback discards the session's connection and is not raised."""
    rollbackFailure = failureFactory()
    session = SimpleNamespace(
        rollback=AsyncMock(side_effect=rollbackFailure), invalidate=AsyncMock()
    )
    await RecoverRequestSession(session)
    session.rollback.assert_awaited_once_with()
    assert session.invalidate.await_count == (0 if rollbackFailure is None else 1)
