"""Replaces: VSAM AWS.M2.CARDDEMO.TCATBALF.VSAM.KSDS (copybook CVTRA01Y).

The 17-byte TRAN-CAT-KEY (KEYS(17 0)) becomes a three-column primary key: TRANCAT-ACCT-ID
9(11), TRANCAT-TYPE-CD X(02) and TRANCAT-CD 9(04). There is no foreign key to
reference.transaction_categories, because CBTRN02C creates a balance for any type and category
code (cbl/CBTRN02C.cbl lines 500-512). TRAN-CAT-BAL S9(09)V99 is exact NUMERIC(11,2), read as
Decimal and never as float. ``acct_id`` carries the ``^[0-9]{11}$`` digit check of every PIC
9(n) identifier, which revision 0005 creates as ``ck_transaction_category_balances_acct_id_digits``.
"""

from decimal import Decimal

from sqlalchemy import CHAR, ForeignKey, Numeric, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    FOREIGN_KEY_OPTIONS,
    KEY_COLLATION,
    TRANSACTIONS_SCHEMA,
    Base,
    DigitCheck,
)


class TransactionCategoryBalance(Base):
    """One TRAN-CAT-BAL-RECORD: the balance of one account for one type and category code."""

    __tablename__ = "transaction_category_balances"
    __table_args__ = (
        DigitCheck("acct_id", 11),
        {
            "schema": TRANSACTIONS_SCHEMA,
            "comment": "CVTRA01Y 50 bytes; FILLER X(22) at offset 28 dropped",
        },
    )

    # Attributes keep the snake_case column names rather than the MK_Modernization Rule's
    # camelCase, because the schema fixes them and every query and API key uses them.
    acct_id: Mapped[str] = mapped_column(
        String(11, collation=KEY_COLLATION),
        ForeignKey("accounts.accounts.acct_id", **FOREIGN_KEY_OPTIONS),
        primary_key=True,
    )
    tran_type_cd: Mapped[str] = mapped_column(CHAR(2, collation=KEY_COLLATION), primary_key=True)
    tran_cat_cd: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(11, 2, asdecimal=True))
