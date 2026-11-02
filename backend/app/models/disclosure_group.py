"""Replaces: VSAM AWS.M2.CARDDEMO.DISCGRP.VSAM.KSDS (copybook CVTRA02Y).

The primary key is the 16-byte DIS-GROUP-KEY (``KEYS(16 0)``): group, transaction type and
transaction category. ``interest_rate`` is ``NUMERIC(6,2)`` because DIS-INT-RATE is
``PIC S9(04)V99``, six digits, rather than the request's provisional ``NUMERIC(5,2)``.

INTCALC (CBACT04C) looks a rate up by (``group_id``, type, category) and retries with group
``DEFAULT`` when none matches, so ``group_id`` is not unique here and ``accounts.group_id``
carries no foreign key to it.
"""

from decimal import Decimal

from sqlalchemy import CHAR, ForeignKeyConstraint, Numeric, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DISCLOSURE_SCHEMA, FOREIGN_KEY_OPTIONS, KEY_COLLATION, Base


class DisclosureGroup(Base):
    """One DIS-GROUP-RECORD: the interest rate for a group, transaction type and category."""

    __tablename__ = "disclosure_groups"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tran_type_cd", "tran_cat_cd"],
            [
                "reference.transaction_categories.tran_type_cd",
                "reference.transaction_categories.tran_cat_cd",
            ],
            **FOREIGN_KEY_OPTIONS,
        ),
        {
            "schema": DISCLOSURE_SCHEMA,
            "comment": "CVTRA02Y 50 bytes; FILLER X(28) at offset 22 dropped",
        },
    )

    # Attributes keep the snake_case column names the schema design fixes, a deliberate
    # departure from the MK_Modernization Rule's camelCase variables for database names.
    # DIS-ACCT-GROUP-ID X(10) at offset 0.
    group_id: Mapped[str] = mapped_column(String(10, collation=KEY_COLLATION), primary_key=True)
    # DIS-TRAN-TYPE-CD X(02) at offset 10.
    tran_type_cd: Mapped[str] = mapped_column(CHAR(2, collation=KEY_COLLATION), primary_key=True)
    # DIS-TRAN-CAT-CD 9(04) at offset 12, a code rendered zero-padded to four digits.
    tran_cat_cd: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    # DIS-INT-RATE S9(04)V99 at offset 16, zoned with a trailing sign overpunch.
    interest_rate: Mapped[Decimal] = mapped_column(Numeric(6, 2, asdecimal=True))
