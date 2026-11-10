"""Replaces: VSAM TRANTYPE (AWS.M2.CARDDEMO.TRANTYPE.VSAM.KSDS, key 2) and TRANCATG
(AWS.M2.CARDDEMO.TRANCATG.VSAM.KSDS, key 6); copybooks CVTRA03Y, CVTRA04Y; keyed reads of
CBTRN03C 1500-B/1500-C used by CORPT00C (CR00).

CBTRN03C reads TRANTYPE by TRAN-TYPE X(02) and TRANCATG by TRAN-CAT-KEY (TRAN-TYPE-CD X(02) and
TRAN-CAT-CD 9(04)) once per report detail line. Each method here reads one whole reference table
into a dictionary keyed like its VSAM record, and the report service looks each detail line's
key up in it. A key with no record is absent from the dictionary: the report service, not this
repository, decides the outcome of the CBTRN03C ``INVALID KEY`` branch.

Neither table is a batch-window file and CR00 is not gated by the window, so these reads never
call ``core.locks.EnterOnlineWindow``. They run on the request session, lock nothing and never
commit, roll back or open a ``WriteUnit``.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, Select, select
from sqlalchemy.exc import DBAPIError

from app.core.database import CONNECTION_FAILURES
from app.core.dependencies import OnlineContext
from app.core.errors import FileIoError, MapDatabaseError
from app.models import TransactionCategory, TransactionType

# The legacy file names (TRANTYPE and TRANCATG DD names of proc/TRANREPT.prc) that a database
# error is reported against.
TRANTYPE_FILE_NAME = "TRANTYPE"
TRANCATG_FILE_NAME = "TRANCATG"


class ReferenceRepository:
    """Reads the TRANTYPE and TRANCATG reference tables for one online request."""

    def __init__(self, context: OnlineContext) -> None:
        self.context = context

    async def _ReadRows(self, statement: Select[Any], fileName: str) -> Sequence[Row[Any]]:
        """Run an unlocked read on the request session and return its column rows.

        A database error is raised as the CICS file condition of ``fileName``, TRANTYPE or
        TRANCATG (``core.errors.MapDatabaseError``). The asyncpg dialect passes connect-time
        rejections, pool timeouts and the ``command_timeout`` TimeoutError through unwrapped
        (``core.database.CONNECTION_FAILURES``); they are FileIoError (IOERR) of ``fileName``
        too, so the report service meets the RepositoryError a database I/O error raises
        rather than an unexpected 500.
        """
        try:
            result = await self.context.session.execute(statement)
            return result.all()
        except DBAPIError as exc:
            raise MapDatabaseError(exc, fileName) from exc
        except CONNECTION_FAILURES as exc:
            raise FileIoError(fileName) from exc

    async def GetTypeDescriptions(self) -> dict[str, str]:
        """Return every TRANTYPE description keyed by its two-character type code.

        Replaces ``READ TRANTYPE-FILE INTO TRAN-TYPE-RECORD`` of CBTRN03C
        1500-B-LOOKUP-TRANTYPE, keyed by TRAN-TYPE X(02). Codes and descriptions are returned
        exactly as stored.
        """
        statement = select(TransactionType.tran_type, TransactionType.description)
        rows = await self._ReadRows(statement, TRANTYPE_FILE_NAME)
        return {tranType: description for tranType, description in rows}

    async def GetCategoryDescriptions(self) -> dict[tuple[str, int], str]:
        """Return every TRANCATG description keyed by (type code, category code).

        Replaces ``READ TRANCATG-FILE INTO TRAN-CAT-RECORD`` of CBTRN03C
        1500-C-LOOKUP-TRANCATG, keyed by TRAN-CAT-KEY: TRAN-TYPE-CD X(02) and TRAN-CAT-CD 9(04),
        the latter as its integer value. Codes and descriptions are returned exactly as stored.
        """
        statement = select(
            TransactionCategory.tran_type_cd,
            TransactionCategory.tran_cat_cd,
            TransactionCategory.description,
        )
        rows = await self._ReadRows(statement, TRANCATG_FILE_NAME)
        return {(tranTypeCd, tranCatCd): description for tranTypeCd, tranCatCd, description in rows}
