"""Replaces: VSAM CUSTDAT (AWS.M2.CARDDEMO.CUSTDATA.VSAM.KSDS, key CUST-ID 9(09)); copybook
CVCUS01Y; used by COACTVWC (CAVW) and COACTUPC (CAUP).

CUSTDAT is the table ``customers.customers`` (model ``Customer``), keyed by ``cust_id`` as the
cluster is by ``KEYS(9 0)`` in catlg/LISTCAT.txt. The file-control commands reproduced are:

- ``EXEC CICS READ DATASET(CUSTDAT)`` of COACTVWC and COACTUPC 9400-GETCUSTDATA-BYCUST, on the
  request session: GetCustomer.
- ``EXEC CICS READ FILE(CUSTDAT) UPDATE`` of COACTUPC 9600-WRITE-PROCESSING: GetCustomerForUpdate.
- ``EXEC CICS REWRITE FILE(CUSTDAT)`` of COACTUPC 9600-WRITE-PROCESSING: UpdateCustomer.

The locked read and the rewrite run on the CUSTDAT unit that the CAUP service opens with
core.database.WriteUnit, which commits after the rewrite because CUSTDAT is RECOVERY(NONE) in
csd/CARDDEMO.CSD. This module therefore never begins, commits or rolls back. CLOSEFIL leaves
CUSTDAT open, so no read here enters the batch window (core.locks.EnterOnlineWindow).

Records are dictionaries keyed by snake_case column names. A database error is raised as the
CICS file condition core.errors.MapDatabaseError gives it: RecordLockError when the unit's lock
timeout ends a wait for the record, FileIoError for any other failure. The asyncpg dialect
passes connect-time rejections, pool timeouts and the ``command_timeout`` TimeoutError through
unwrapped (core.database.CONNECTION_FAILURES); they are FileIoError (IOERR) too, so the service
still answers with its program's ``WHEN OTHER`` literal rather than an abend.
"""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import CursorResult, Executable, Select, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.core.database import CONNECTION_FAILURES
from app.core.dependencies import OnlineContext
from app.core.errors import FileIoError, MapDatabaseError, RecordNotFoundError
from app.models import Customer

CUSTOMER_FILE_NAME = "CUSTDAT"
CUSTOMERS_TABLE = Customer.__table__
# CUST-ID, the RIDFLD of every CUSTDAT command; REWRITE identifies its record by it.
CUSTOMER_KEY_COLUMN = "cust_id"


def SelectCustomer(custId: str) -> Select[Any]:
    """Build the keyed read of one customer; the key loses its X(n) right padding first."""
    keyColumn = CUSTOMERS_TABLE.c[CUSTOMER_KEY_COLUMN]
    return select(CUSTOMERS_TABLE).where(keyColumn == custId.rstrip(" "))


def RecordOf(result: CursorResult[Any]) -> dict[str, Any] | None:
    """Return the first row as a column-name dictionary, or None when the read found none."""
    row = result.mappings().first()
    if row is None:
        return None
    return dict(row)


class CustomerRepository:
    """Keyed reads and the rewrite of CUSTDAT for the account view and update services."""

    def __init__(self, context: OnlineContext) -> None:
        self.context = context

    async def _Execute(
        self, executor: AsyncConnection | AsyncSession, statement: Executable
    ) -> CursorResult[Any]:
        """Run one statement, raising a database failure as its CUSTDAT file condition.

        A wrapped database error maps by SQLSTATE through MapDatabaseError (55P03 gives
        RecordLockError). The asyncpg dialect passes connect-time rejections, pool timeouts and
        the ``command_timeout`` TimeoutError through unwrapped; they are CICS IOERR too, as in
        ``WriteUnit``, so the service still answers with its program's ``WHEN OTHER`` literal
        rather than an abend.
        """
        try:
            return await executor.execute(statement)
        except DBAPIError as exc:
            raise MapDatabaseError(exc, CUSTOMER_FILE_NAME) from exc
        except CONNECTION_FAILURES as exc:
            raise FileIoError(CUSTOMER_FILE_NAME) from exc

    async def GetCustomer(self, custId: str) -> dict[str, Any] | None:
        """Replace ``EXEC CICS READ DATASET(CUSTDAT) RIDFLD(cust id)`` of 9400-GETCUSTDATA-BYCUST.

        Returns the record, or None for NOTFND; the service builds the not-found message.
        """
        result = await self._Execute(self.context.session, SelectCustomer(custId))
        return RecordOf(result)

    async def GetCustomerForUpdate(
        self, unit: AsyncConnection, custId: str
    ) -> dict[str, Any] | None:
        """Replace ``EXEC CICS READ FILE(CUSTDAT) UPDATE`` of 9600-WRITE-PROCESSING.

        Locks the row with ``SELECT ... FOR UPDATE`` on the CUSTDAT write unit until the unit
        ends. Returns the record, or None when it no longer exists. A lock held past the unit's
        lock timeout raises RecordLockError.
        """
        statement = SelectCustomer(custId).with_for_update()
        result = await self._Execute(unit, statement)
        return RecordOf(result)

    async def UpdateCustomer(self, unit: AsyncConnection, values: Mapping[str, Any]) -> None:
        """Replace ``EXEC CICS REWRITE FILE(CUSTDAT)`` of 9600-WRITE-PROCESSING.

        ``values`` holds ``cust_id``, which names the record, and the columns to rewrite. Runs
        on the unit that holds the GetCustomerForUpdate lock. Raises RecordNotFoundError when
        no record has the key, and the mapped file condition when the database refuses the row.
        """
        changes = {key: value for key, value in values.items() if key != CUSTOMER_KEY_COLUMN}
        if not changes:
            raise ValueError("UpdateCustomer needs at least one CUSTDAT column to rewrite")
        keyColumn = CUSTOMERS_TABLE.c[CUSTOMER_KEY_COLUMN]
        custId = str(values[CUSTOMER_KEY_COLUMN]).rstrip(" ")
        statement = update(CUSTOMERS_TABLE).where(keyColumn == custId).values(**changes)
        result = await self._Execute(unit, statement)
        if result.rowcount == 0:
            raise RecordNotFoundError(CUSTOMER_FILE_NAME)
