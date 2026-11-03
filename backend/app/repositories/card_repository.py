"""Replaces: VSAM CARDDAT (AWS.M2.CARDDEMO.CARDDATA.VSAM.KSDS, key CARD-NUM X(16)); copybook
CVACT02Y; used by COCRDLIC (CCLI), COCRDSLC (CCDL), COCRDUPC (CCUP).

Each public method reproduces one CICS file-control command of those programs against
``cards.cards``:

- ``GetCard``: the ``READ FILE(CARDDAT) RIDFLD(card number)`` of COCRDSLC and COCRDUPC
  9100-GETCARD-BYACCTCARD.
- ``GetCardForUpdate`` and ``UpdateCard``: the ``READ ... UPDATE`` and ``REWRITE`` of COCRDUPC
  9200-WRITE-PROCESSING, inside the caller's ``WriteUnit(context.engine, "CARDDAT")``.
- ``BrowseCards``: the ``STARTBR GTEQ`` and the ``READNEXT`` loop of COCRDLIC 9000-READ-FORWARD
  or the ``READPREV`` loop of 9100-READ-BACKWARDS, applying 9500-FILTER-RECORDS. Its records
  hold only the BROWSE_COLUMNS; the keyed reads return the whole record.

Key order is EBCDIC order, through the generated ``card_num_sort`` column (C collation). The
CARDAIX path has no method, because no reachable paragraph reads it.

CLOSEFIL leaves CARDDAT open, so no method enters the batch window of core.locks, and CCLI,
CCDL and CCUP keep working while batch runs. Durability belongs to the caller's unit: nothing
here commits, rolls back or begins a transaction. A failed statement raises the CICS file
condition the program would see: SQLSTATE 55P03 RecordLockError, 23505 DuplicateKeyError, and
any other database or connection failure FileIoError.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Executable, Result, RowMapping, Text, func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.core.database import CONNECTION_FAILURES
from app.core.dependencies import OnlineContext
from app.core.errors import FileIoError, MapDatabaseError, RecordNotFoundError
from app.models import KEY_COLLATION, Card

CARD_FILE_NAME = "CARDDAT"
BROWSE_FORWARD = "forward"
BROWSE_BACKWARD = "backward"
# PostgreSQL computes these from card_num, so a REWRITE never sets them.
GENERATED_COLUMNS = ("card_num_sort",)
KEY_COLUMN = "card_num"
CARD_TABLE = Card.__table__
# The CARD-RECORD fields COCRDLIC 1200-SCREEN-ARRAY-INIT moves to a list row, plus the sort key
# the unfiltered look-ahead READNEXT continues from.
BROWSE_COLUMNS = (
    CARD_TABLE.c.card_num,
    CARD_TABLE.c.card_num_sort,
    CARD_TABLE.c.acct_id,
    CARD_TABLE.c.active_status,
)


@dataclass(frozen=True)
class CardFilters:
    """The COCRDLIC list filters that 9500-FILTER-RECORDS applies to every browsed record.

    ``acctId`` replaces CC-ACCT-ID under FLG-ACCTFILTER-ISVALID, and ``cardNum`` replaces
    CC-CARD-NUM-N under FLG-CARDFILTER-ISVALID. None means the filter is not applied: the
    service passes only the filters its edits marked valid.
    """

    acctId: str | None = None
    cardNum: str | None = None


def _SortKeyOf(key: str) -> ColumnElement[str]:
    """Return ``ebcdic_sort_key(:key) COLLATE "C"``, the EBCDIC position of a browse key.

    The key is right-trimmed first, as stored keys are. Space is the lowest code-page-037
    printable character, so the trimmed comparison equals the space-padded VSAM comparison.
    """
    return func.ebcdic_sort_key(key.rstrip(" "), type_=Text).collate(KEY_COLLATION)


def _FilterConditions(filters: CardFilters) -> list[ColumnElement[bool]]:
    """Return 9500-FILTER-RECORDS as WHERE conditions, one per filter that is set.

    A record is kept only when its account and card number equal every set filter.
    """
    conditions: list[ColumnElement[bool]] = []
    if filters.acctId is not None:
        conditions.append(CARD_TABLE.c.acct_id == filters.acctId.rstrip(" "))
    if filters.cardNum is not None:
        conditions.append(CARD_TABLE.c.card_num == filters.cardNum.rstrip(" "))
    return conditions


def _RecordOf(row: RowMapping | None) -> dict[str, Any] | None:
    """Return a fetched row as a record keyed by column name, or None for no row (NOTFND)."""
    return dict(row) if row is not None else None


def _RewriteValues(values: Mapping[str, Any]) -> dict[str, Any]:
    """Return the columns a REWRITE sets: every given column except the key and generated ones.

    Raises ValueError when no column is left to set or a name is not a CARDDAT column.
    """
    setValues = {
        columnName: value
        for columnName, value in values.items()
        if columnName != KEY_COLUMN and columnName not in GENERATED_COLUMNS
    }
    unknownColumns = sorted(set(setValues) - set(CARD_TABLE.c.keys()))
    if unknownColumns:
        raise ValueError(f"REWRITE of {CARD_FILE_NAME} names unknown columns {unknownColumns}")
    if not setValues:
        raise ValueError(f"REWRITE of {CARD_FILE_NAME} needs at least one column to set")
    return setValues


class CardRepository:
    """CARDDAT file control for one online request.

    Unlocked reads and browses run on the request session. The locked read and the rewrite run
    on the ``WriteUnit`` connection the service opens and passes in as ``unit``.
    """

    def __init__(self, context: OnlineContext) -> None:
        self.context = context

    async def GetCard(self, cardNum: str) -> dict[str, Any] | None:
        """``EXEC CICS READ FILE(CARDDAT) RIDFLD(cardNum)``, locking nothing.

        Returns None for NOTFND, for which the service sets
        ``Did not find cards for this search condition``.
        """
        statement = select(CARD_TABLE).where(CARD_TABLE.c.card_num == cardNum.rstrip(" "))
        result = await self._Execute(self.context.session, statement)
        return _RecordOf(result.mappings().first())

    async def GetCardForUpdate(self, unit: AsyncConnection, cardNum: str) -> dict[str, Any] | None:
        """``EXEC CICS READ FILE(CARDDAT) UPDATE``: read and lock the record in ``unit``.

        The row lock lasts until the unit ends. A wait beyond the unit's lock timeout raises
        RecordLockError, for which the service sets ``Could not lock record for update``.
        Returns None for NOTFND.
        """
        statement = (
            select(CARD_TABLE).where(CARD_TABLE.c.card_num == cardNum.rstrip(" ")).with_for_update()
        )
        result = await self._Execute(unit, statement)
        return _RecordOf(result.mappings().first())

    async def UpdateCard(self, unit: AsyncConnection, values: Mapping[str, Any]) -> None:
        """``EXEC CICS REWRITE FILE(CARDDAT)`` of the record ``GetCardForUpdate`` locked.

        ``values`` holds ``card_num`` and the columns to set. The key and ``card_num_sort`` are
        never set, and ``cvv_cd`` is set only when the caller passes it. Raises
        RecordNotFoundError when no record has the key, and ValueError for a missing key or an
        empty or unknown column list.
        """
        cardNum = str(values.get(KEY_COLUMN) or "").rstrip(" ")
        if not cardNum:
            raise ValueError(f"REWRITE of {CARD_FILE_NAME} needs a non-blank {KEY_COLUMN} key")
        statement = (
            update(CARD_TABLE)
            .where(CARD_TABLE.c.card_num == cardNum)
            .values(_RewriteValues(values))
        )
        result = await self._Execute(unit, statement)
        if result.rowcount == 0:
            raise RecordNotFoundError(CARD_FILE_NAME)

    async def BrowseCards(
        self, filters: CardFilters, startKey: str | None, direction: str, limit: int
    ) -> list[dict[str, Any]]:
        """``EXEC CICS STARTBR FILE(CARDDAT) GTEQ`` and a filtered READNEXT or READPREV loop.

        ``startKey`` None is LOW-VALUES. ``limit`` is the page size, WS-MAX-SCREEN-LINES (7).
        Each record holds only the BROWSE_COLUMNS: never the CVV, embossed name or expiry.

        - ``BROWSE_FORWARD`` (9000-READ-FORWARD) returns up to ``limit`` matching records in
          ascending key order from ``startKey`` inclusive. After a full page it appends one
          more element, index ``limit``: the next record in key order whatever the filters,
          the look-ahead READNEXT whose key starts the next page. The list has ``limit + 1``
          elements only when that record exists.
        - ``BROWSE_BACKWARD`` (9100-READ-BACKWARDS) returns up to ``limit`` matching records
          before ``startKey``, nearest first. Fewer than ``limit`` means READPREV reached the
          start of the file.

        Raises ValueError for any other direction or a limit below 1.
        """
        if limit < 1:
            raise ValueError(f"{CARD_FILE_NAME} browse limit must be at least 1, got {limit}")
        if direction == BROWSE_FORWARD:
            return await self._BrowseForward(filters, startKey, limit)
        if direction == BROWSE_BACKWARD:
            return await self._BrowseBackward(filters, startKey, limit)
        raise ValueError(
            f"{CARD_FILE_NAME} browse direction must be {BROWSE_FORWARD!r} or "
            f"{BROWSE_BACKWARD!r}, got {direction!r}"
        )

    async def _BrowseForward(
        self, filters: CardFilters, startKey: str | None, limit: int
    ) -> list[dict[str, Any]]:
        """9000-READ-FORWARD: filtered READNEXT from ``startKey`` (GTEQ), then the look-ahead."""
        conditions = _FilterConditions(filters)
        if startKey is not None:
            conditions.append(CARD_TABLE.c.card_num_sort >= _SortKeyOf(startKey))
        statement = (
            select(*BROWSE_COLUMNS)
            .where(*conditions)
            .order_by(CARD_TABLE.c.card_num_sort.asc())
            .limit(limit)
        )
        records = await self._ReadRecords(statement)
        if len(records) == limit:
            lookAhead = await self._ReadLookAhead(records[-1])
            if lookAhead is not None:
                records.append(lookAhead)
        return records

    async def _ReadLookAhead(self, lastRecord: Mapping[str, Any]) -> dict[str, Any] | None:
        """The READNEXT after a full page, which 9500-FILTER-RECORDS never sees.

        Returns the first record after ``lastRecord`` in key order, matching the filters or
        not, or None at end of file (CA-NEXT-PAGE-NOT-EXISTS).
        """
        statement = (
            select(*BROWSE_COLUMNS)
            .where(CARD_TABLE.c.card_num_sort > lastRecord["card_num_sort"])
            .order_by(CARD_TABLE.c.card_num_sort.asc())
            .limit(1)
        )
        records = await self._ReadRecords(statement)
        return records[0] if records else None

    async def _BrowseBackward(
        self, filters: CardFilters, startKey: str | None, limit: int
    ) -> list[dict[str, Any]]:
        """9100-READ-BACKWARDS: filtered READPREV from the first key shown, nearest first.

        The legacy first READPREV re-reads the start record and discards it, so the start key
        itself is excluded. No record precedes LOW-VALUES, so a None start key reads nothing.
        """
        if startKey is None:
            return []
        conditions = _FilterConditions(filters)
        conditions.append(CARD_TABLE.c.card_num_sort < _SortKeyOf(startKey))
        statement = (
            select(*BROWSE_COLUMNS)
            .where(*conditions)
            .order_by(CARD_TABLE.c.card_num_sort.desc())
            .limit(limit)
        )
        return await self._ReadRecords(statement)

    async def _ReadRecords(self, statement: Executable) -> list[dict[str, Any]]:
        """Run an unlocked read on the request session and return its rows as records."""
        result = await self._Execute(self.context.session, statement)
        return [dict(row) for row in result.mappings().all()]

    async def _Execute(
        self, executor: AsyncConnection | AsyncSession, statement: Executable
    ) -> Result[Any]:
        """Run one statement and raise the CICS file condition its failure gives.

        A database error maps by SQLSTATE through MapDatabaseError. A connection that cannot
        be opened is FileIoError (IOERR) too: the asyncpg dialect raises those failures
        unwrapped (CONNECTION_FAILURES), and the service must still reach its WHEN OTHER
        literal rather than a 500.
        """
        try:
            return await executor.execute(statement)
        except DBAPIError as exc:
            raise MapDatabaseError(exc, CARD_FILE_NAME) from exc
        except CONNECTION_FAILURES as exc:
            raise FileIoError(CARD_FILE_NAME) from exc
