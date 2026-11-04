"""Unit tests for the CUSTDAT, TRANTYPE, TRANCATG and CR00 TRANSACT reads when the database fails.

app.repositories.customer_repository ports the CICS READ and READ UPDATE of CUSTDAT (COACTVWC,
COACTUPC), app.repositories.reference_repository the CBTRN03C keyed reads of TRANTYPE and
TRANCATG, and app.repositories.transaction_repository the CR00 report's snapshot and its
server-side cursor over TRANSACT. A legacy read that fails returns a RESP other than NORMAL, and
the program takes its WHEN OTHER file-error branch, which the API answers with that program's
literal and status 503.

SQLAlchemy's asyncpg dialect wraps only the driver errors it registers in DBAPIError. A refused
or timed-out connect, a server that rejects the connection, a closed connection, the
command_timeout TimeoutError and a pool checkout past pool_timeout reach the repository
unwrapped (core.database.CONNECTION_FAILURES). These tests pin that each of them becomes
FileIoError (IOERR, RESP 17) of the file the read addressed, chained to the original failure,
rather than an unexpected 500. They also pin that a wrapped 55P03 lock timeout still maps by
SQLSTATE to RecordLockError, and that cancellation and programming errors propagate unchanged,
because no catch-all handler may turn them into a file condition.

The request session is a stub whose execute and connection raise the failure, and whose report
cursor raises it from every fetch, so no database is needed.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date
from functools import partial
from types import SimpleNamespace
from typing import NamedTuple
from unittest.mock import AsyncMock, Mock

import asyncpg
import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.core.dependencies import OnlineContext
from app.core.errors import CICS_RESP_IOERR, FileIoError, RecordLockError
from app.repositories.customer_repository import CUSTOMER_FILE_NAME, CustomerRepository
from app.repositories.reference_repository import (
    TRANCATG_FILE_NAME,
    TRANTYPE_FILE_NAME,
    ReferenceRepository,
)
from app.repositories.transaction_repository import TRANSACTION_FILE_NAME, TransactionRepository

# CUST-ID 9(09) of a sample customer; the stub session never looks it up.
CUSTOMER_ID = "000000001"
# A report range and batch size; the stub cursor never reads them.
REPORT_START_DATE = date(2022, 1, 1)
REPORT_END_DATE = date(2022, 12, 31)
REPORT_BATCH_ROWS = 1000
# SQLSTATE lock_not_available, which ends a wait for a row locked past the lock timeout.
LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"


class LockNotAvailableDriverError(Exception):
    """A driver error carrying SQLSTATE 55P03 where asyncpg's error carries it."""

    sqlstate = LOCK_NOT_AVAILABLE_SQLSTATE


class ReadCase(NamedTuple):
    """One repository read and the CICS file name every condition it raises carries."""

    readFunction: Callable[[OnlineContext], Awaitable[object]]
    fileName: str


def BuildContext(failure):
    """Return an online context whose request session raises failure from every read.

    execute and connection raise it, and the result stream opens a cursor whose every fetch
    raises it.
    """
    reportRows = SimpleNamespace(fetchmany=AsyncMock(side_effect=failure), close=AsyncMock())
    reportResult = SimpleNamespace(mappings=Mock(return_value=reportRows))
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=failure),
        connection=AsyncMock(side_effect=failure),
        stream=AsyncMock(return_value=reportResult),
    )
    return OnlineContext(request=None, session=session, settings=None)


async def ReadCustomer(context):
    """Run the CUSTDAT keyed read of COACTVWC and COACTUPC 9400-GETCUSTDATA-BYCUST."""
    return await CustomerRepository(context).GetCustomer(CUSTOMER_ID)


async def ReadCustomerForUpdate(context):
    """Run the CUSTDAT READ UPDATE of COACTUPC 9600-WRITE-PROCESSING, the stub as its unit."""
    return await CustomerRepository(context).GetCustomerForUpdate(context.session, CUSTOMER_ID)


async def ReadTypeDescriptions(context):
    """Run the TRANTYPE read of CBTRN03C 1500-B-LOOKUP-TRANTYPE."""
    return await ReferenceRepository(context).GetTypeDescriptions()


async def ReadCategoryDescriptions(context):
    """Run the TRANCATG read of CBTRN03C 1500-C-LOOKUP-TRANCATG."""
    return await ReferenceRepository(context).GetCategoryDescriptions()


async def BeginReportSnapshot(context):
    """Open the CR00 report's snapshot on the request session (CORPT00C's TRANREPT reads)."""
    return await TransactionRepository(context).BeginReportSnapshot()


async def ReadReportBatches(context):
    """Fetch the CR00 report's TRANSACT rows (TRANREPT STEP05R) from its server-side cursor."""
    repository = TransactionRepository(context)
    reportBatches = repository.StreamReportTransactions(
        REPORT_START_DATE, REPORT_END_DATE, REPORT_BATCH_ROWS
    )
    return [rowBatch async for rowBatch in reportBatches]


READ_CASES = {
    "GetCustomer": ReadCase(ReadCustomer, CUSTOMER_FILE_NAME),
    "GetCustomerForUpdate": ReadCase(ReadCustomerForUpdate, CUSTOMER_FILE_NAME),
    "GetTypeDescriptions": ReadCase(ReadTypeDescriptions, TRANTYPE_FILE_NAME),
    "GetCategoryDescriptions": ReadCase(ReadCategoryDescriptions, TRANCATG_FILE_NAME),
    "BeginReportSnapshot": ReadCase(BeginReportSnapshot, TRANSACTION_FILE_NAME),
    "StreamReportTransactions": ReadCase(ReadReportBatches, TRANSACTION_FILE_NAME),
}

# Failures the asyncpg dialect passes through unwrapped, each built fresh for its case.
CONNECTION_FAILURE_FACTORIES = {
    "command-timeout": partial(TimeoutError),
    "pool-timeout": partial(
        PoolTimeoutError, "QueuePool limit of size 5 overflow 10 reached, connection timed out"
    ),
    "connection-refused": partial(ConnectionRefusedError, 111, "Connection refused"),
    "server-starting-up": partial(
        asyncpg.exceptions.CannotConnectNowError, "the database system is starting up"
    ),
    "connection-closed": partial(asyncpg.InterfaceError, "connection is closed"),
}

# Failures that are neither database nor connection errors and must reach the caller as raised.
UNTRANSLATED_FAILURE_FACTORIES = {
    "cancelled": partial(asyncio.CancelledError),
    "programming-error": partial(ValueError, "not a database failure"),
}


@pytest.mark.parametrize("readCase", list(READ_CASES.values()), ids=list(READ_CASES))
@pytest.mark.parametrize(
    "failureFactory",
    list(CONNECTION_FAILURE_FACTORIES.values()),
    ids=list(CONNECTION_FAILURE_FACTORIES),
)
async def test_connection_failure_is_file_io_error_of_the_read_file(readCase, failureFactory):
    failure = failureFactory()
    with pytest.raises(FileIoError) as raised:
        await readCase.readFunction(BuildContext(failure))
    assert raised.value.fileName == readCase.fileName
    assert raised.value.resp == CICS_RESP_IOERR
    assert raised.value.__cause__ is failure


@pytest.mark.parametrize("readCase", list(READ_CASES.values()), ids=list(READ_CASES))
async def test_wrapped_lock_timeout_still_maps_to_record_lock_error(readCase):
    driverError = LockNotAvailableDriverError("canceling statement due to lock timeout")
    dbError = DBAPIError("SELECT", {}, driverError)
    with pytest.raises(RecordLockError) as raised:
        await readCase.readFunction(BuildContext(dbError))
    assert raised.value.fileName == readCase.fileName
    assert raised.value.__cause__ is dbError


@pytest.mark.parametrize("readCase", list(READ_CASES.values()), ids=list(READ_CASES))
@pytest.mark.parametrize(
    "failureFactory",
    list(UNTRANSLATED_FAILURE_FACTORIES.values()),
    ids=list(UNTRANSLATED_FAILURE_FACTORIES),
)
async def test_cancellation_and_programming_errors_propagate_unchanged(readCase, failureFactory):
    failure = failureFactory()
    with pytest.raises(type(failure)) as raised:
        await readCase.readFunction(BuildContext(failure))
    assert raised.value is failure
