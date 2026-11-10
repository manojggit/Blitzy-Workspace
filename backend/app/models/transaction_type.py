"""Replaces: VSAM AWS.M2.CARDDEMO.TRANTYPE.VSAM.KSDS (copybook CVTRA03Y).

``TRAN-TYPE-RECORD`` is 60 bytes: ``TRAN-TYPE X(02)`` at offset 0 is the ``KEYS(2 0)`` prime
key, ``TRAN-TYPE-DESC X(50)`` follows at offset 2, and ``FILLER X(08)`` at offset 52 is dropped
and recorded in the table comment. Revision 0001 is the DDL authority for this table.
"""

from sqlalchemy import CHAR, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import KEY_COLLATION, REFERENCE_SCHEMA, Base


class TransactionType(Base):
    """One TRANTYPE record: a transaction type code and its description."""

    __tablename__ = "transaction_types"
    # A one-element tuple rather than a bare dict keeps the class attribute immutable (ruff
    # RUF012); declarative reads its trailing dict as the Table keyword arguments, so the DDL
    # is the same as for the dict form.
    __table_args__ = (
        {
            "schema": REFERENCE_SCHEMA,
            "comment": "CVTRA03Y 60 bytes; FILLER X(08) at offset 52 dropped",
        },
    )

    tran_type: Mapped[str] = mapped_column(CHAR(2, collation=KEY_COLLATION), primary_key=True)
    description: Mapped[str] = mapped_column(String(50), server_default="")
