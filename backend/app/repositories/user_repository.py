"""Replaces: VSAM USRSEC (AWS.M2.CARDDEMO.USRSEC.VSAM.KSDS, key SEC-USR-ID X(08)); copybook
CSUSR01Y; used by COSGN00C (CC00) and COUSR00C-COUSR03C (CU00-CU03); a batch-window file
(CLOSEFIL).

The CICS file USRSEC (csd/CARDDEMO.CSD ``DEFINE FILE(USRSEC)``, KEYS(8,0) in
catlg/LISTCAT.txt) is the table ``security.users``. Each method reproduces one CICS file
command of the programs that use it:

- READ: COSGN00C READ-USER-SEC-FILE, and the CU02 and CU03 fetch (GetUser).
- READ UPDATE, then REWRITE or DELETE: COUSR02C and COUSR03C (GetUserForUpdate, UpdateUser,
  DeleteUser). CU02 is last writer wins, with no before-image compare.
- WRITE: COUSR01C WRITE-USER-SEC-FILE (InsertUser); DUPKEY/DUPREC is DuplicateKeyError.
- STARTBR, READNEXT, READPREV: the COUSR00C browse (BrowseForward, BrowseBackward).

CLOSEFIL closes USRSEC, so every method first enters the online batch window, which raises
FileNotOpenError (NOTOPEN) while batch holds it. Neither that nor any other RepositoryError is
caught here: each service maps it to its program's WHEN OTHER literal. Keyed reads and browses
run on the request session; locked reads and writes run on the caller's
``core.database.WriteUnit`` connection, which commits. Nothing here commits, rolls back or
opens a unit. Key order is EBCDIC order through the generated ``user_id_sort`` column.
"""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import Text, delete, func, insert, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.sql import ColumnElement, Delete, Executable, Insert, Select, Update

from app.core.database import CONNECTION_FAILURES
from app.core.dependencies import OnlineContext
from app.core.errors import FileIoError, MapDatabaseError, RecordNotFoundError
from app.core.locks import EnterOnlineWindow
from app.models import User
from app.models.base import KEY_COLLATION

USER_FILE_NAME = "USRSEC"
# COUSR00C PROCESS-PF8-KEY moves HIGH-VALUES to SEC-USR-ID when no last key was shown; no key
# reaches it, so the STARTBR is NOTFND.
HIGH_VALUES_USER_ID = "\xff" * 8
# Filled by PostgreSQL: the generated EBCDIC sort key, the column default and the revision 0006
# trigger. Callers' values for them are never written.
SERVER_MANAGED_COLUMNS = ("user_id_sort", "created_at", "updated_at")
KEY_COLUMN = "user_id"

USERS_TABLE = User.__table__
SORT_COLUMN = USERS_TABLE.c.user_id_sort
# The four SEC-USER-DATA fields COUSR00C POPULATE-USER-DATA moves to a list row.
BROWSE_COLUMNS = (
    USERS_TABLE.c.user_id,
    USERS_TABLE.c.first_name,
    USERS_TABLE.c.last_name,
    USERS_TABLE.c.user_type,
)


def TrimmedKey(userId: str) -> str:
    """Return the key without the space padding of the 8-byte SEC-USR-ID field.

    The key is not upper-cased: COSGN00C upper-cases its input in the sign-on service.
    """
    return userId.rstrip(" ")


def SortKeyOf(startKey: str) -> ColumnElement[str]:
    """Return ``ebcdic_sort_key(:startKey) COLLATE "C"``, the start key in user_id_sort order.

    The explicit collation matches the column's, so the comparison is byte order and can use
    the user_id_sort index.
    """
    return func.ebcdic_sort_key(TrimmedKey(startKey), type_=Text).collate(KEY_COLLATION)


def WritableValues(values: Mapping[str, Any]) -> dict[str, Any]:
    """Return the record values without the server-managed columns, with the key trimmed."""
    writable = {
        column: value for column, value in values.items() if column not in SERVER_MANAGED_COLUMNS
    }
    if KEY_COLUMN in writable:
        writable[KEY_COLUMN] = TrimmedKey(writable[KEY_COLUMN])
    return writable


def KeyedSelect(userId: str) -> Select[Any]:
    """Return the SELECT of the one record whose SEC-USR-ID is ``userId``."""
    return select(USERS_TABLE).where(USERS_TABLE.c.user_id == TrimmedKey(userId))


def ForwardSelect(startKey: str | None, readCount: int) -> Select[Any]:
    """Return up to ``readCount`` browse rows at or after ``startKey``, in ascending key order.

    Each row holds only the BROWSE_COLUMNS. A ``startKey`` of None is LOW-VALUES: the browse
    starts at the first record.
    """
    statement = select(*BROWSE_COLUMNS).order_by(SORT_COLUMN.asc()).limit(readCount)
    if startKey is None:
        return statement
    return statement.where(SORT_COLUMN >= SortKeyOf(startKey))


def PositionSelect(startKey: str | None) -> Select[Any]:
    """Return the existence probe of STARTBR GTEQ: one key at or after ``startKey``."""
    statement = select(USERS_TABLE.c.user_id).limit(1)
    if startKey is None:
        return statement
    return statement.where(SORT_COLUMN >= SortKeyOf(startKey))


def BackwardSelect(startKey: str, readCount: int) -> Select[Any]:
    """Return up to ``readCount`` browse rows before ``startKey``, nearest first.

    Each row holds only the BROWSE_COLUMNS.
    """
    return (
        select(*BROWSE_COLUMNS)
        .where(SORT_COLUMN < SortKeyOf(startKey))
        .order_by(SORT_COLUMN.desc())
        .limit(readCount)
    )


def KeyedUpdate(values: Mapping[str, Any]) -> Update:
    """Return the REWRITE of the record ``values["user_id"]`` names, setting its other columns.

    Raises ValueError when ``values`` names no column to set, a caller error: REWRITE always
    writes the whole record.
    """
    writable = WritableValues(values)
    userId = writable.pop(KEY_COLUMN)
    if not writable:
        raise ValueError("UpdateUser needs at least one USRSEC column besides user_id")
    return update(USERS_TABLE).where(USERS_TABLE.c.user_id == userId).values(writable)


def KeyedDelete(userId: str) -> Delete:
    """Return the DELETE of the one record whose SEC-USR-ID is ``userId``."""
    return delete(USERS_TABLE).where(USERS_TABLE.c.user_id == TrimmedKey(userId))


def RecordInsert(values: Mapping[str, Any]) -> Insert:
    """Return the WRITE of one new record from ``values``."""
    return insert(USERS_TABLE).values(WritableValues(values))


class UserRepository:
    """The USRSEC access paths of one online request.

    Records are returned as ``dict`` objects keyed by snake_case column name, or None when
    absent. Keyed and locked reads return the full record, ``password_hash`` included: services
    verify or replace it, and never put it in a response. Browse rows carry only ``user_id``,
    ``first_name``, ``last_name`` and ``user_type``.
    """

    def __init__(self, context: OnlineContext) -> None:
        self.context = context

    async def _Execute(
        self, executor: AsyncConnection | AsyncSession, statement: Executable
    ) -> CursorResult[Any]:
        """Run one bound statement; a database failure becomes its CICS file condition.

        SQLSTATE 23505 is DuplicateKeyError (DUPREC), 55P03 RecordLockError and any other
        FileIoError (IOERR). The request session connects on its first statement, so a failure
        to connect is FileIoError too, as it is for a write unit.
        """
        try:
            return await executor.execute(statement)
        except DBAPIError as exc:
            raise MapDatabaseError(exc, USER_FILE_NAME) from exc
        except CONNECTION_FAILURES as exc:
            raise FileIoError(USER_FILE_NAME) from exc

    async def _FetchAll(self, statement: Select[Any]) -> list[dict[str, Any]]:
        """Run a SELECT on the request session and return every record it reads."""
        result = await self._Execute(self.context.session, statement)
        return [dict(row) for row in result.mappings().all()]

    async def _FetchFirst(
        self, executor: AsyncConnection | AsyncSession, statement: Select[Any]
    ) -> dict[str, Any] | None:
        """Run a SELECT and return its first record, or None when it reads none."""
        result = await self._Execute(executor, statement)
        row = result.mappings().first()
        return dict(row) if row is not None else None

    async def _PositionExists(self, startKey: str | None) -> bool:
        """Return whether STARTBR GTEQ at ``startKey`` positions on a record (not NOTFND)."""
        return await self._FetchFirst(self.context.session, PositionSelect(startKey)) is not None

    async def GetUser(self, userId: str) -> dict[str, Any] | None:
        """READ DATASET(USRSEC) RIDFLD(SEC-USR-ID): the record, or None for NOTFND."""
        await EnterOnlineWindow(self.context.request, USER_FILE_NAME)
        return await self._FetchFirst(self.context.session, KeyedSelect(userId))

    async def GetUserForUpdate(self, unit: AsyncConnection, userId: str) -> dict[str, Any] | None:
        """READ ... UPDATE: lock the record in ``unit`` until it commits; None for NOTFND.

        A record another task holds past the unit's lock timeout raises RecordLockError.
        """
        await EnterOnlineWindow(self.context.request, USER_FILE_NAME)
        return await self._FetchFirst(unit, KeyedSelect(userId).with_for_update())

    async def InsertUser(self, unit: AsyncConnection, values: Mapping[str, Any]) -> None:
        """WRITE DATASET(USRSEC): add the record; an existing key raises DuplicateKeyError."""
        await EnterOnlineWindow(self.context.request, USER_FILE_NAME)
        await self._Execute(unit, RecordInsert(values))

    async def UpdateUser(self, unit: AsyncConnection, values: Mapping[str, Any]) -> None:
        """REWRITE DATASET(USRSEC): replace the record ``values["user_id"]`` names.

        The other keys of ``values`` are the columns written (``first_name``, ``last_name``,
        ``password_hash``, ``user_type``). No such record raises RecordNotFoundError (NOTFND).
        """
        await EnterOnlineWindow(self.context.request, USER_FILE_NAME)
        result = await self._Execute(unit, KeyedUpdate(values))
        if result.rowcount == 0:
            raise RecordNotFoundError(USER_FILE_NAME)

    async def DeleteUser(self, unit: AsyncConnection, userId: str) -> int:
        """DELETE DATASET(USRSEC): remove the record; return 0 for NOTFND, else 1."""
        await EnterOnlineWindow(self.context.request, USER_FILE_NAME)
        result = await self._Execute(unit, KeyedDelete(userId))
        return result.rowcount

    async def BrowseForward(
        self, startKey: str | None, inclusive: bool, limit: int
    ) -> list[dict[str, Any]] | None:
        """STARTBR GTEQ at ``startKey``, then READNEXT: COUSR00C PROCESS-PAGE-FORWARD.

        Returns None for STARTBR NOTFND: no record at or after ``startKey``, or the HIGH-VALUES
        key. When ``inclusive`` is False (F8), the first record at or after ``startKey`` is the
        discarded READNEXT and is dropped, even when ``startKey`` itself no longer exists. The
        list then holds up to ``limit`` page rows plus element ``limit``, the look-ahead
        READNEXT; an empty list is READNEXT ENDFILE on the discarded read. Each row is a browse
        row: ``user_id``, ``first_name``, ``last_name`` and ``user_type`` only.
        """
        await EnterOnlineWindow(self.context.request, USER_FILE_NAME)
        if startKey == HIGH_VALUES_USER_ID:
            return None
        readCount = limit + 1 if inclusive else limit + 2
        records = await self._FetchAll(ForwardSelect(startKey, readCount))
        if not records:
            return None
        return records if inclusive else records[1:]

    async def BrowseBackward(self, startKey: str | None, limit: int) -> list[dict[str, Any]] | None:
        """STARTBR GTEQ at ``startKey``, then READPREV: COUSR00C PROCESS-PAGE-BACKWARD (F7).

        Returns None for STARTBR NOTFND. The discarded READPREV re-reads the positioned record,
        so the list holds the browse rows before ``startKey``, nearest first: up to ``limit``
        page rows plus element ``limit``, the look-ahead READPREV. Fewer than ``limit`` rows is
        READPREV ENDFILE. A ``startKey`` of None (LOW-VALUES) has no record before it. Each row
        carries ``user_id``, ``first_name``, ``last_name`` and ``user_type`` only.
        """
        await EnterOnlineWindow(self.context.request, USER_FILE_NAME)
        if not await self._PositionExists(startKey):
            return None
        if startKey is None:
            return []
        return await self._FetchAll(BackwardSelect(startKey, limit + 1))
