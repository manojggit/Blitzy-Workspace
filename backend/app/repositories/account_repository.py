"""ACCTDAT access path: keyed read, read for update and rewrite of the account master.

Replaces: VSAM ACCTDAT (AWS.M2.CARDDEMO.ACCTDATA.VSAM.KSDS, key ACCT-ID 9(11)); copybook
CVACT01Y; used by COACTVWC (CAVW), COACTUPC (CAUP), COBIL00C (CB00); a batch-window file
(CLOSEFIL).

The CSD defines ACCTDAT with KEYS(11 0) and no alternate index, so the only access path is the
11-character ``acct_id`` primary key of ``accounts.accounts``. Records are returned as plain
dictionaries keyed by column name, with the money columns as exact ``Decimal`` values.

CLOSEFIL closes ACCTDAT, so every method first enters the request's online window; while batch
holds it, ``FileNotOpenError`` (NOTOPEN) propagates to the service, which sends its program's
``WHEN OTHER`` literal. Locked reads and rewrites run on the write unit the service opened with
``WriteUnit(context.engine, ACCOUNT_FILE_NAME)``; this module never begins, commits or rolls
back a transaction, so the unit decides when the row lock ends and when the rewrite is durable.
"""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import Executable, Result, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.core.database import CONNECTION_FAILURES
from app.core.dependencies import OnlineContext
from app.core.errors import FileIoError, MapDatabaseError, RecordNotFoundError
from app.core.locks import EnterOnlineWindow
from app.models import Account

# The CICS file name the account programs address, named by every raised file condition.
ACCOUNT_FILE_NAME = "ACCTDAT"

ACCOUNTS_TABLE = Account.__table__
# ACCT-ID 9(11), the KSDS key KEYS(11 0): the column a REWRITE identifies its record by.
ACCOUNT_KEY_COLUMN = "acct_id"

NOTHING_TO_REWRITE_MESSAGE = "UpdateAccount needs at least one column besides acct_id"


class AccountRepository:
    """One request's access to ACCTDAT, through its request session and its write units."""

    def __init__(self, context: OnlineContext) -> None:
        self.context = context

    async def _Execute(
        self, executor: AsyncSession | AsyncConnection, statement: Executable
    ) -> Result[Any]:
        """Run one bound statement, raising a database failure as its CICS file condition.

        A wrapped database error maps by SQLSTATE (55P03 RecordLockError, 23505
        DuplicateKeyError, other FileIoError). The asyncpg dialect passes connect-time
        rejections, pool timeouts and the ``command_timeout`` TimeoutError through unwrapped;
        they are CICS IOERR too, as in ``WriteUnit``, so the service still answers with its
        program's ``WHEN OTHER`` literal rather than an abend.
        """
        try:
            return await executor.execute(statement)
        except DBAPIError as exc:
            raise MapDatabaseError(exc, ACCOUNT_FILE_NAME) from exc
        except CONNECTION_FAILURES as exc:
            raise FileIoError(ACCOUNT_FILE_NAME) from exc

    async def GetAccount(self, acctId: str) -> dict[str, Any] | None:
        """CICS ``READ DATASET(ACCTDAT) RIDFLD(acct id)``: the record, or None for NOTFND.

        Replaces COACTVWC and COACTUPC 9300-GETACCTDATA-BYACCT. The read locks nothing and runs
        on the request session.
        """
        await EnterOnlineWindow(self.context.request, ACCOUNT_FILE_NAME)
        statement = select(ACCOUNTS_TABLE).where(ACCOUNTS_TABLE.c.acct_id == acctId.rstrip(" "))
        result = await self._Execute(self.context.session, statement)
        row = result.mappings().first()
        return dict(row) if row is not None else None

    async def GetAccountForUpdate(
        self, unit: AsyncConnection, acctId: str
    ) -> dict[str, Any] | None:
        """CICS ``READ ... UPDATE`` on ACCTDAT: lock the record in the unit; None for NOTFND.

        Replaces COACTUPC 9600-WRITE-PROCESSING and COBIL00C READ-ACCTDAT-FILE. The row lock
        lasts until the unit ends. A record another unit holds past the unit's lock timeout
        raises RecordLockError.
        """
        await EnterOnlineWindow(self.context.request, ACCOUNT_FILE_NAME)
        statement = (
            select(ACCOUNTS_TABLE)
            .where(ACCOUNTS_TABLE.c.acct_id == acctId.rstrip(" "))
            .with_for_update()
        )
        result = await self._Execute(unit, statement)
        row = result.mappings().first()
        return dict(row) if row is not None else None

    async def UpdateAccount(self, unit: AsyncConnection, values: Mapping[str, Any]) -> None:
        """CICS ``REWRITE`` on ACCTDAT: store ``values`` over the record ``values["acct_id"]``.

        Replaces COACTUPC 9600-WRITE-PROCESSING and COBIL00C UPDATE-ACCTDAT-FILE. ``values``
        is keyed by column name; every key except ``acct_id`` is set exactly as given, because
        callers pass amounts already stored into their COBOL receivers. A record that no longer
        exists raises RecordNotFoundError (NOTFND). The rewrite is durable when the unit
        commits.
        """
        await EnterOnlineWindow(self.context.request, ACCOUNT_FILE_NAME)
        acctId = str(values[ACCOUNT_KEY_COLUMN]).rstrip(" ")
        changes = {key: value for key, value in values.items() if key != ACCOUNT_KEY_COLUMN}
        # An empty SET compiles to invalid SQL, which would surface as a misleading IOERR.
        if not changes:
            raise ValueError(NOTHING_TO_REWRITE_MESSAGE)
        statement = update(ACCOUNTS_TABLE).where(ACCOUNTS_TABLE.c.acct_id == acctId).values(changes)
        result = await self._Execute(unit, statement)
        if result.rowcount == 0:
            raise RecordNotFoundError(ACCOUNT_FILE_NAME)
