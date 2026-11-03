"""Replaces: VSAM TRANSACT (AWS.M2.CARDDEMO.TRANSACT.VSAM.KSDS, key TRAN-ID X(16)); copybook
CVTRA05Y; used by COTRN00C (CT00), COTRN01C (CT01), COTRN02C (CT02), COBIL00C (CB00), and as
report input for CORPT00C/CBTRN03C (CR00); a batch-window file (CLOSEFIL).

The cluster is KEYS(16 0) (catlg/LISTCAT.txt) and the CICS file TRANSACT (csd/CARDDEMO.CSD).
Its table is ``transactions.transactions``, and every key-ordered read orders by the generated
``tran_id_sort`` column, which holds EBCDIC key order (``ebcdic_sort_key(tran_id)``).

CLOSEFIL closes TRANSACT, so every method except GetReportTransactions first enters the online
batch window, and a refusal raises FileNotOpenError (NOTOPEN, RESP 19, RESP2 60) unchanged. Reads
run on the request session. AllocateNextTranId and InsertTransaction run on the caller's
``WriteUnit(context.engine, "TRANSACT")`` connection, so the TRAN-ID allocation lock lasts until
that unit commits. The repository never commits, rolls back or opens a unit, and binds every
value as a parameter.

A database error becomes the CICS file condition of app.core.errors.MapDatabaseError: 23505 is
DuplicateKeyError (DUPREC), 55P03 is RecordLockError and any other, or a connection that cannot
be opened, is FileIoError. Records are returned as dictionaries keyed by column name;
``tran_amt`` stays a Decimal and the timestamps stay datetimes beside their format codes. Browse
rows hold only the CT00 list columns (BROWSE_COLUMNS). Report rows are read-only row mappings
keyed the same way, rather than dictionary copies, and hold only the columns the report builder
reads, plus ``xref_acct_id``.
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    Executable,
    Result,
    RowMapping,
    Select,
    Text,
    bindparam,
    func,
    insert,
    select,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.core.database import CONNECTION_FAILURES
from app.core.dependencies import OnlineContext
from app.core.errors import FileIoError, MapDatabaseError
from app.core.locks import TRAN_ID_ALLOCATION_LOCK, EnterOnlineWindow
from app.models import KEY_COLLATION, Card, CardXref, Transaction

TRANSACTION_FILE_NAME = "TRANSACT"
# MOVE HIGH-VALUES TO TRAN-ID: the COTRN00C F8 start key when no last key is shown.
HIGH_VALUES_TRAN_ID = "\xff" * 16
# WS-TRAN-ID-N / WS-TRAN-ID-NUM PIC 9(16): ADD 1 keeps the low-order sixteen digits.
TRAN_ID_DIGITS = 16
TRAN_ID_MODULUS = 10**TRAN_ID_DIGITS
# Computed by PostgreSQL; an INSERT must never name them.
GENERATED_COLUMNS = ("tran_id_sort",)

TRANSACTION_COLUMNS = tuple(Transaction.__table__.columns)
# The TRAN-RECORD fields COTRN00C POPULATE-TRAN-DATA moves to a list row.
BROWSE_COLUMNS = (
    Transaction.tran_id,
    Transaction.orig_ts,
    Transaction.orig_ts_format,
    Transaction.tran_desc,
    Transaction.tran_amt,
)
# The only transaction fields cbtrn03c.BuildReport reads from a report row.
REPORT_COLUMNS = (
    Transaction.tran_id,
    Transaction.card_num,
    Transaction.tran_type_cd,
    Transaction.tran_cat_cd,
    Transaction.tran_source,
    Transaction.tran_amt,
)
# The label stays snake_case like the column names; cbtrn03c.BuildReport reads it.
XREF_ACCT_ID_LABEL = "xref_acct_id"

# The key exceeds 32 bits, so it is bound as BIGINT, the single-key advisory-lock form.
TRAN_ID_ALLOCATION_LOCK_STATEMENT = select(
    func.pg_advisory_xact_lock(
        bindparam("tranIdAllocationLock", TRAN_ID_ALLOCATION_LOCK, type_=BigInteger)
    )
)
HIGHEST_TRAN_ID_STATEMENT = (
    select(Transaction.tran_id).order_by(Transaction.tran_id_sort.desc()).limit(1)
)
HIGHEST_TRANSACTION_STATEMENT = (
    select(*TRANSACTION_COLUMNS).order_by(Transaction.tran_id_sort.desc()).limit(1)
)


def StartKeySort(startKey: str) -> ColumnElement[str]:
    """Return the EBCDIC sort key of a browse start key, comparable with ``tran_id_sort``."""
    return func.ebcdic_sort_key(startKey, type_=Text).collate(KEY_COLLATION)


def ForwardBrowseStatement(startKey: str | None, rowLimit: int) -> Select[Any]:
    """Return the GTEQ forward browse from startKey; None (LOW-VALUES) starts at the lowest key.

    The statement selects only the BROWSE_COLUMNS.
    """
    statement = select(*BROWSE_COLUMNS).order_by(Transaction.tran_id_sort.asc())
    if startKey is not None:
        statement = statement.where(Transaction.tran_id_sort >= StartKeySort(startKey))
    return statement.limit(rowLimit)


def BackwardBrowseStatement(startKey: str, rowLimit: int) -> Select[Any]:
    """Return the records before startKey, nearest first, as successive READPREVs read them.

    The statement selects only the BROWSE_COLUMNS.
    """
    return (
        select(*BROWSE_COLUMNS)
        .where(Transaction.tran_id_sort < StartKeySort(startKey))
        .order_by(Transaction.tran_id_sort.desc())
        .limit(rowLimit)
    )


def StartPositionStatement(startKey: str | None) -> Select[Any]:
    """Return the STARTBR GTEQ probe: the first key at or after startKey in tran_id_sort order.

    The order makes the probe one read of the tran_id_sort index. None (LOW-VALUES) reads the
    lowest key.
    """
    statement = select(Transaction.tran_id).order_by(Transaction.tran_id_sort.asc())
    if startKey is not None:
        statement = statement.where(Transaction.tran_id_sort >= StartKeySort(startKey))
    return statement.limit(1)


def ProcTsEndBound(endDate: date) -> ColumnElement[bool]:
    """Return the end of the report range: proc_ts before the midnight that follows endDate.

    9999-12-31 has no following day, so its end is proc_ts at or before 23:59:59.999999, the
    same set of TIMESTAMP(6) values, which hold microseconds at most.
    """
    if endDate == date.max:
        return Transaction.proc_ts <= datetime.combine(date.max, time.max)
    return Transaction.proc_ts < datetime.combine(endDate + timedelta(days=1), time.min)


def ReportStatement(startDate: date, endDate: date) -> Select[Any]:
    """Return the TRANREPT STEP05R input: the INCLUDE on PROC-TS(1:10) and SORT on TRAN-CARD-NUM.

    Every non-blank stored PROC-TS form begins with its YYYY-MM-DD date, so the INCLUDE's text
    comparison is the date of the stored value lying from startDate to endDate inclusive: proc_ts
    from startDate's midnight up to, not including, the midnight after endDate. Comparing the
    bare column lets ix_transactions_proc_ts serve both bounds. A blank (NULL) PROC-TS never
    passes the INCLUDE.
    """
    # Ordered by the generated cards.card_num_sort column, which every key-ordered read uses for
    # EBCDIC order, rather than recomputing ebcdic_sort_key(card_num). The card foreign key on
    # transactions.card_num means the inner join drops no transaction.
    return (
        select(*REPORT_COLUMNS, CardXref.acct_id.label(XREF_ACCT_ID_LABEL))
        .select_from(Transaction)
        .join(Card, Card.card_num == Transaction.card_num)
        .outerjoin(CardXref, CardXref.card_num == Transaction.card_num)
        .where(Transaction.proc_ts.is_not(None))
        .where(Transaction.proc_ts >= datetime.combine(startDate, time.min))
        .where(ProcTsEndBound(endDate))
        .order_by(Card.card_num_sort, Transaction.tran_id_sort)
    )


class TransactionRepository:
    """The TRANSACT file control of one online request."""

    def __init__(self, context: OnlineContext) -> None:
        self.context = context

    async def _Execute(
        self, executor: AsyncSession | AsyncConnection, statement: Executable
    ) -> Result[Any]:
        """Run one statement; a database error becomes its CICS file condition on TRANSACT.

        A connection that cannot be opened is FileIoError (IOERR) too: the asyncpg dialect
        raises those failures unwrapped (CONNECTION_FAILURES), and the service must still reach
        its WHEN OTHER literal rather than a 500, the ungated report read included.
        """
        try:
            return await executor.execute(statement)
        except DBAPIError as exc:
            raise MapDatabaseError(exc, TRANSACTION_FILE_NAME) from exc
        except CONNECTION_FAILURES as exc:
            raise FileIoError(TRANSACTION_FILE_NAME) from exc

    async def _FetchRecords(self, statement: Executable) -> list[dict[str, Any]]:
        """Run a read on the request session and return every row as a record dictionary."""
        result = await self._Execute(self.context.session, statement)
        return [dict(row) for row in result.mappings().all()]

    async def GetTransaction(self, tranId: str) -> dict[str, Any] | None:
        """Replace COTRN01C EXEC CICS READ ... UPDATE, held for display only, by a plain read.

        Returns the record of TRAN-ID tranId, or None for NOTFND.
        """
        await EnterOnlineWindow(self.context.request, TRANSACTION_FILE_NAME)
        statement = select(*TRANSACTION_COLUMNS).where(Transaction.tran_id == tranId.rstrip(" "))
        result = await self._Execute(self.context.session, statement)
        row = result.mappings().first()
        return dict(row) if row is not None else None

    async def BrowseForward(
        self, startKey: str | None, inclusive: bool, limit: int
    ) -> list[dict[str, Any]] | None:
        """Replace COTRN00C STARTBR (GTEQ) and the READNEXTs of PROCESS-PAGE-FORWARD.

        startKey None is LOW-VALUES. None is returned for STARTBR NOTFND: no record at or after
        startKey, or the HIGH-VALUES key. When not inclusive (F8), the first record at or after
        startKey is discarded, as the extra READNEXT is, even if startKey itself has gone.
        The rest, at most limit + 1, follow in key order: element ``limit`` is the look-ahead
        READNEXT, and an empty list is READNEXT ENDFILE. Each record holds only the
        BROWSE_COLUMNS.
        """
        await EnterOnlineWindow(self.context.request, TRANSACTION_FILE_NAME)
        if startKey == HIGH_VALUES_TRAN_ID:
            return None
        sortStart = startKey.rstrip(" ") if startKey is not None else None
        rowLimit = limit + 1 if inclusive else limit + 2
        records = await self._FetchRecords(ForwardBrowseStatement(sortStart, rowLimit))
        if not records:
            return None
        return records if inclusive else records[1:]

    async def BrowseBackward(self, startKey: str | None, limit: int) -> list[dict[str, Any]] | None:
        """Replace COTRN00C STARTBR and the READPREVs of PROCESS-PAGE-BACKWARD (F7).

        startKey None is LOW-VALUES. None is returned for STARTBR NOTFND, when no record lies
        at or after startKey. Otherwise the records before startKey follow nearest first, the
        record at startKey being the one the first READPREV discards: element ``limit`` is the
        look-ahead READPREV, and fewer than ``limit`` records mean READPREV ENDFILE. Each record
        holds only the BROWSE_COLUMNS.
        """
        await EnterOnlineWindow(self.context.request, TRANSACTION_FILE_NAME)
        sortStart = startKey.rstrip(" ") if startKey is not None else None
        positioned = await self._Execute(self.context.session, StartPositionStatement(sortStart))
        if positioned.scalar_one_or_none() is None:
            return None
        if sortStart is None:
            return []
        return await self._FetchRecords(BackwardBrowseStatement(sortStart, limit + 1))

    async def GetHighestTransaction(self) -> dict[str, Any] | None:
        """Replace COTRN02C STARTBR HIGH-VALUES, READPREV and ENDBR for the F5 copy-last action.

        Returns the record with the highest key in EBCDIC order, or None (READPREV ENDFILE)
        when the table is empty.
        """
        await EnterOnlineWindow(self.context.request, TRANSACTION_FILE_NAME)
        result = await self._Execute(self.context.session, HIGHEST_TRANSACTION_STATEMENT)
        row = result.mappings().first()
        return dict(row) if row is not None else None

    async def AllocateNextTranId(self, unit: AsyncConnection | AsyncSession) -> str:
        """Replace the COTRN02C and COBIL00C STARTBR HIGH-VALUES, READPREV and ADD 1 into 9(16).

        Runs on the caller's TRANSACT write unit. pg_advisory_xact_lock serialises concurrent
        allocations until that unit commits; a wait past the unit's lock_timeout raises
        RecordLockError. An empty table gives TRAN-ID zeros (the ENDFILE branch), so the first
        ID is 0000000000000001, and 9999999999999999 wraps to 0000000000000000.
        """
        await EnterOnlineWindow(self.context.request, TRANSACTION_FILE_NAME)
        await self._Execute(unit, TRAN_ID_ALLOCATION_LOCK_STATEMENT)
        result = await self._Execute(unit, HIGHEST_TRAN_ID_STATEMENT)
        highestId = result.scalar_one_or_none()
        nextId = (int(highestId or "0") + 1) % TRAN_ID_MODULUS
        return str(nextId).zfill(TRAN_ID_DIGITS)

    async def InsertTransaction(
        self, unit: AsyncConnection | AsyncSession, values: Mapping[str, Any]
    ) -> None:
        """Replace COTRN02C WRITE-TRANSACT-FILE and COBIL00C WRITE-TRANSACT-FILE (EXEC CICS WRITE).

        values maps column names to the new record's fields; generated columns are dropped.
        An existing key raises DuplicateKeyError (DUPREC).
        """
        await EnterOnlineWindow(self.context.request, TRANSACTION_FILE_NAME)
        insertValues = {
            name: value for name, value in values.items() if name not in GENERATED_COLUMNS
        }
        await self._Execute(unit, insert(Transaction).values(insertValues))

    async def GetReportTransactions(self, startDate: date, endDate: date) -> Sequence[RowMapping]:
        """Replace the TRANREPT STEP05R SORT and the CBTRN03C 1500-A-LOOKUP-XREF reads.

        Returns the transactions whose PROC-TS date lies from startDate to endDate inclusive,
        in TRAN-CARD-NUM order and then TRANSACT key order. Each row carries only the fields
        cbtrn03c.BuildReport reads, tran_id, card_num, tran_type_cd, tran_cat_cd, tran_source and
        tran_amt, and ``xref_acct_id``, the XREF-ACCT-ID of its card, or None when the card has
        no cross-reference (INVALID KEY). The rows are the read-only mappings the result
        fetched, not dictionary copies, because a yearly report holds many thousands of them.

        This read never enters the batch window: CR00 only submitted a job to TDQ JOBS and never
        opened TRANSACT, so a closed window does not stop the report.
        """
        result = await self._Execute(self.context.session, ReportStatement(startDate, endDate))
        return result.mappings().all()
