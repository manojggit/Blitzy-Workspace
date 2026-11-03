"""Replaces: VSAM CCXREF (AWS.M2.CARDDEMO.CARDXREF.VSAM.KSDS, key XREF-CARD-NUM X(16)) and
AIX path CXACAIX (XREF-ACCT-ID 9(11), KEYS(11,25), non-unique); copybook CVACT03Y; used by
COACTVWC, COACTUPC, COTRN02C, COBIL00C; batch-window files (CLOSEFIL).

Table ``xref.card_xref`` serves both paths: its primary key replaces the CCXREF cluster key
(LISTCAT KEYLEN 16, RKP 0) and ``ix_card_xref_acct_id`` the CXACAIX path (KEYLEN 11, AXRKP 25,
NONUNIQKEY). No online program writes the cross-reference, so this repository only reads: it
has no write or ``FOR UPDATE`` method and never opens a write unit.

CLOSEFIL closes CCXREF and CXACAIX, so every read first enters the online batch window under
the name of the path it reads; FileNotOpenError (NOTOPEN) then names that path, and the service
builds its program's WHEN OTHER literal from it. A found record is a dictionary keyed by the
column names ``card_num``, ``cust_id`` and ``acct_id``; NOTFND is ``None``; a CXACAIX DUPKEY
raises DuplicateAlternateKeyError carrying the first record in AIX order.
"""

from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.exc import DBAPIError

from app.core.dependencies import OnlineContext
from app.core.errors import FileIoError, MapDatabaseError, RepositoryError
from app.core.locks import WINDOW_FAILURES, EnterOnlineWindow
from app.models import Card, CardXref

XREF_FILE_NAME = "CCXREF"
XREF_ACCOUNT_PATH_NAME = "CXACAIX"

CARD_XREF_TABLE = CardXref.__table__

# CICS DUPKEY on a READ through a NONUNIQUEKEY path: RESP 15, and the RESP2 value 140 that the
# CICS READ command reference gives for "retrieved by way of an alternate index in which the
# key that is used is not unique".
CICS_RESP_DUPKEY = 15
CICS_RESP2_DUPKEY_NONUNIQUE_PATH = 140
# A CXACAIX read needs only to learn whether a second record shares the account ID.
ACCOUNT_PATH_READ_LIMIT = 2


class DuplicateAlternateKeyError(RepositoryError):
    """CICS DUPKEY (RESP 15, RESP2 140): another record has the alternate key just read.

    CICS reads the first record of a non-unique alternate key into the INTO area and raises
    DUPKEY when another follows; ``firstRecord`` is that record. COACTVWC, COACTUPC, COTRN02C
    and COBIL00C test only NORMAL and NOTFND after their CXACAIX READ, so DUPKEY takes each
    program's WHEN OTHER branch. The first three stop there; COBIL00C goes on to WRITE the
    payment with XREF-CARD-NUM, which that READ ... INTO(CARD-XREF-RECORD) has filled from it.
    The exception text keeps to the file name and codes; the record is only an attribute.
    """

    def __init__(self, fileName: str, firstRecord: dict[str, Any]) -> None:
        super().__init__(fileName, resp=CICS_RESP_DUPKEY, resp2=CICS_RESP2_DUPKEY_NONUNIQUE_PATH)
        self.firstRecord = firstRecord


class CardXrefRepository:
    """Keyed reads of the card cross-reference by card number (CCXREF) and account (CXACAIX)."""

    def __init__(self, context: OnlineContext) -> None:
        self.context = context

    async def _Execute(self, statement: Select[Any], fileName: str) -> list[dict[str, Any]]:
        """Run a read on the request session and return its rows as dictionaries.

        A database error becomes the CICS condition MapDatabaseError gives for ``fileName``.
        A connection that cannot be checked out (an unwrapped asyncpg connect-time rejection,
        OSError or a pool timeout) is IOERR, as the batch window reports it.
        """
        try:
            result = await self.context.session.execute(statement)
        except DBAPIError as exc:
            raise MapDatabaseError(exc, fileName) from exc
        # Not only DBAPIError, as the shared repository convention has it: the asyncpg
        # dialect raises connect-time failures unwrapped, which would otherwise end the
        # request with a 500 instead of the program's WHEN OTHER literal.
        except WINDOW_FAILURES as exc:
            raise FileIoError(fileName) from exc
        return [dict(row) for row in result.mappings().all()]

    async def GetXrefByCard(self, cardNum: str) -> dict[str, Any] | None:
        """Replace ``EXEC CICS READ DATASET(CCXREF) RIDFLD(XREF-CARD-NUM)``.

        COTRN02C READ-CCXREF-FILE. The card number is the unique cluster key, so the read
        returns that card's record, or None for NOTFND (``Card Number NOT found...``).
        """
        await EnterOnlineWindow(self.context.request, XREF_FILE_NAME)
        statement = select(CARD_XREF_TABLE).where(CardXref.card_num == cardNum.rstrip(" "))
        rows = await self._Execute(statement, XREF_FILE_NAME)
        return rows[0] if rows else None

    async def GetXrefByAccount(self, acctId: str) -> dict[str, Any] | None:
        """Replace ``EXEC CICS READ DATASET(CXACAIX) RIDFLD(XREF-ACCT-ID)``.

        COACTVWC and COACTUPC 9200-GETCARDXREF-BYACCT, COTRN02C and COBIL00C READ-CXACAIX-FILE.
        Returns the account's only record, or None for NOTFND. CXACAIX is non-unique: when a
        second record has the account ID, CICS raises DUPKEY, so this raises
        DuplicateAlternateKeyError with the record CICS reads with it, the first in AIX order.
        XREFFILE's BLDINDEX lists an alternate key's base records in prime-key order, so that
        record is the account's lowest XREF-CARD-NUM in EBCDIC order: the card COBIL00C pays on.
        """
        await EnterOnlineWindow(self.context.request, XREF_ACCOUNT_PATH_NAME)
        # EBCDIC order comes from the generated cards.card_num_sort column, as every key-ordered
        # read takes it; the card foreign key on card_xref.card_num means the join drops no row.
        statement = (
            select(CARD_XREF_TABLE)
            .join(Card, Card.card_num == CardXref.card_num)
            .where(CardXref.acct_id == acctId.rstrip(" "))
            .order_by(Card.card_num_sort)
            .limit(ACCOUNT_PATH_READ_LIMIT)
        )
        rows = await self._Execute(statement, XREF_ACCOUNT_PATH_NAME)
        if len(rows) > 1:
            raise DuplicateAlternateKeyError(XREF_ACCOUNT_PATH_NAME, rows[0])
        return rows[0] if rows else None
