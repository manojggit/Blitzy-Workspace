"""Replaces: VSAM AWS.M2.CARDDEMO.CARDXREF.VSAM.KSDS and AIX CXACAIX (copybook CVACT03Y).

The cross-reference is a table rather than a view over ``cards.cards``: CARD-RECORD carries no
customer ID, so this record is the only link from a card to its customer. The primary key
replaces the CCXREF cluster key ``KEYS(16 0)``, and ``ix_card_xref_acct_id`` replaces the
non-unique CXACAIX path ``KEYS(11,25)``. INTCALC takes an account's first cross-reference card
in ``card_num`` order, which is the AIX prime-key order.
"""

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    FOREIGN_KEY_OPTIONS,
    KEY_COLLATION,
    XREF_SCHEMA,
    Base,
    DigitCheck,
)


class CardXref(Base):
    """CARD-XREF-RECORD: card number to customer and account, one row per card."""

    __tablename__ = "card_xref"
    __table_args__ = (
        DigitCheck("cust_id", 9),
        DigitCheck("acct_id", 11),
        {"schema": XREF_SCHEMA, "comment": "CVACT03Y 50 bytes; FILLER X(14) at offset 36 dropped"},
    )

    # Attributes keep the snake_case column names rather than the MK_Modernization Rule's
    # camelCase, because the schema fixes them and every query and API key uses them.
    # XREF-CARD-NUM X(16) at offset 0.
    card_num: Mapped[str] = mapped_column(
        String(16, collation=KEY_COLLATION),
        ForeignKey("cards.cards.card_num", **FOREIGN_KEY_OPTIONS),
        primary_key=True,
        server_default="",
    )
    # XREF-CUST-ID 9(09) at offset 16.
    cust_id: Mapped[str] = mapped_column(
        String(9, collation=KEY_COLLATION),
        ForeignKey("customers.customers.cust_id", **FOREIGN_KEY_OPTIONS),
        index=True,
    )
    # XREF-ACCT-ID 9(11) at offset 25.
    acct_id: Mapped[str] = mapped_column(
        String(11, collation=KEY_COLLATION),
        ForeignKey("accounts.accounts.acct_id", **FOREIGN_KEY_OPTIONS),
        index=True,
    )
