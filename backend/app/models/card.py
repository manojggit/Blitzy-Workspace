"""Replaces: VSAM AWS.M2.CARDDEMO.CARDDATA.VSAM.KSDS and AIX CARDAIX (copybook CVACT02Y).

``cards.cards`` holds one row per 150-byte CARD-RECORD. ``card_num`` is the KSDS key,
KEYS(16 0), and the non-unique index on ``acct_id`` replaces the CARDAIX alternate index,
KEYS(11 16).

- The PAN in ``card_num`` is masked in every API response by ``core/security.MaskCardNumber``.
- ``cvv_cd`` is stored only for CARDFILE record parity; no request or response schema carries it.
- ``card_num_sort`` gives EBCDIC key order for the CCLI browse and the report and statement sorts.

Alembic revisions 0001, 0003, 0004 and 0005 create the table, the sort column and indexes, the
foreign key and the digit checks under the names this model produces.
"""

from datetime import date

from sqlalchemy import CHAR, Computed, Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CARDS_SCHEMA, FOREIGN_KEY_OPTIONS, KEY_COLLATION, Base, DigitCheck


class Card(Base):
    """CARD-RECORD, field for field at its copybook offsets.

    CARD-NUM X(16)@0 -> ``card_num``; CARD-ACCT-ID 9(11)@16 -> ``acct_id``;
    CARD-CVV-CD 9(03)@27 -> ``cvv_cd``; CARD-EMBOSSED-NAME X(50)@30 -> ``embossed_name``;
    CARD-EXPIRAION-DATE (sic) X(10)@80 -> ``expiration_date``, NULL when blank;
    CARD-ACTIVE-STATUS X(01)@90 -> ``active_status``.
    """

    __tablename__ = "cards"
    __table_args__ = (
        DigitCheck("acct_id", 11),
        DigitCheck("cvv_cd", 3),
        {
            "schema": CARDS_SCHEMA,
            "comment": "CVACT02Y 150 bytes; FILLER X(59) at offset 91 dropped",
        },
    )

    # Attributes carry the snake_case column names the schema fixes, not the camelCase the
    # MK_Modernization Rule's naming clause gives variables, so ORM and SQL names stay identical.
    card_num: Mapped[str] = mapped_column(
        String(16, collation=KEY_COLLATION), primary_key=True, server_default=""
    )
    card_num_sort: Mapped[str] = mapped_column(
        Text(collation=KEY_COLLATION),
        Computed("ebcdic_sort_key(card_num)", persisted=True),
        index=True,
    )
    acct_id: Mapped[str] = mapped_column(
        String(11, collation=KEY_COLLATION),
        ForeignKey("accounts.accounts.acct_id", **FOREIGN_KEY_OPTIONS),
        index=True,
    )
    cvv_cd: Mapped[str] = mapped_column(CHAR(3))
    embossed_name: Mapped[str] = mapped_column(String(50), server_default="")
    expiration_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    active_status: Mapped[str] = mapped_column(CHAR(1))
