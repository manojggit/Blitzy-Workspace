"""Replaces: VSAM AWS.M2.CARDDEMO.TRANCATG.VSAM.KSDS (copybook CVTRA04Y).

``TRAN-CAT-RECORD`` is 60 bytes keyed by ``TRAN-CAT-KEY`` (``KEYS(6 0)``): TRAN-TYPE-CD X(02) at
offset 0 and TRAN-CAT-CD 9(04) at offset 2, followed by TRAN-CAT-TYPE-DESC X(50) at offset 6 and
a dropped FILLER X(04) at offset 56. The TRANCATG job loads 18 records.

``tran_cat_cd`` is PIC 9(04) used as a code, so it is stored as SMALLINT and rendered zero-padded
to 4 digits wherever the legacy text form is shown or written.

Revision 0001 creates the table, its primary key and comment; revision 0004 adds the foreign key
to ``reference.transaction_types``.
"""

from typing import ClassVar

from sqlalchemy import CHAR, ForeignKey, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import FOREIGN_KEY_OPTIONS, KEY_COLLATION, REFERENCE_SCHEMA, Base


class TransactionCategory(Base):
    """One TRANCATG record: a transaction category within a transaction type."""

    __tablename__ = "transaction_categories"
    # ClassVar marks this declarative directive as class-level, not a mapped column; ruff RUF012
    # rejects an unannotated mutable class attribute.
    __table_args__: ClassVar[dict[str, str]] = {
        "schema": REFERENCE_SCHEMA,
        "comment": "CVTRA04Y 60 bytes; FILLER X(04) at offset 56 dropped",
    }

    tran_type_cd: Mapped[str] = mapped_column(
        CHAR(2, collation=KEY_COLLATION),
        ForeignKey("reference.transaction_types.tran_type", **FOREIGN_KEY_OPTIONS),
        primary_key=True,
    )
    tran_cat_cd: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    description: Mapped[str] = mapped_column(String(50), server_default="")
