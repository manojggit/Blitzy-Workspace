"""Replaces: VSAM AWS.M2.CARDDEMO.TRANSACT.VSAM.KSDS and its PROC-TS AIX (copybook CVTRA05Y).

``transactions.transactions`` holds posted transactions only: the cluster's KEYS(16 0) prime
key becomes the primary key, and the non-unique AIX KEYS(26 304) on TRAN-PROC-TS becomes the
index ``ix_transactions_proc_ts``. The daily file is staged separately in
``daily_transaction.py``, so the CT00 browse, the CT02/CB00 highest-ID allocation and TRANBKP
never see unposted rows. ``tran_id_sort`` gives the EBCDIC key order every key-ordered read uses.

Type and category codes carry no foreign key, because COTRN02C accepts any numeric codes and
CBTRN02C checks none.

Each 26-byte timestamp is stored as a zone-free ``TIMESTAMP(6)`` value plus a ``CHAR(1)`` code
naming its text form, from which ``record_layouts.EncodeTimestamp`` rebuilds the bytes:
``S`` is ``YYYY-MM-DD HH:MM:SS.ffffff``, ``D`` is ``YYYY-MM-DD-HH.MM.SS.cc0000``, ``T`` is a
date followed by 16 spaces (stored as midnight), and blank is 26 spaces (stored as NULL).
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CHAR, Computed, ForeignKey, Numeric, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    FOREIGN_KEY_OPTIONS,
    KEY_COLLATION,
    TRANSACTIONS_SCHEMA,
    Base,
    DigitCheck,
)


class Transaction(Base):
    """One TRAN-RECORD (CVTRA05Y), column for column in copybook order."""

    __tablename__ = "transactions"
    __table_args__ = (
        DigitCheck("merchant_id", 9),
        {
            "schema": TRANSACTIONS_SCHEMA,
            "comment": "CVTRA05Y 350 bytes; FILLER X(20) at offset 330 dropped",
        },
    )

    # Attributes keep the snake_case column names the schema mapping fixes, which the
    # MK_Modernization Rule's camelCase variable convention yields to.
    # TRAN-ID X(16) at offset 0, the KEYS(16 0) prime key.
    tran_id: Mapped[str] = mapped_column(
        String(16, collation=KEY_COLLATION), primary_key=True, server_default=""
    )
    tran_id_sort: Mapped[str] = mapped_column(
        Text(collation=KEY_COLLATION),
        Computed("ebcdic_sort_key(tran_id)", persisted=True),
        index=True,
    )
    # TRAN-TYPE-CD X(02) at 16; byte collation because the report joins it to transaction_types.
    tran_type_cd: Mapped[str] = mapped_column(CHAR(2, collation=KEY_COLLATION))
    # TRAN-CAT-CD 9(04) at 18.
    tran_cat_cd: Mapped[int] = mapped_column(SmallInteger)
    tran_source: Mapped[str] = mapped_column(String(10), server_default="")
    tran_desc: Mapped[str] = mapped_column(String(100), server_default="")
    # TRAN-AMT S9(09)V99 zoned at 132: exact decimal, never floating point.
    tran_amt: Mapped[Decimal] = mapped_column(Numeric(11, 2, asdecimal=True))
    # TRAN-MERCHANT-ID 9(09) at 143, guarded by the digit check above.
    merchant_id: Mapped[str] = mapped_column(String(9))
    merchant_name: Mapped[str] = mapped_column(String(50), server_default="")
    merchant_city: Mapped[str] = mapped_column(String(50), server_default="")
    merchant_zip: Mapped[str] = mapped_column(String(10), server_default="")
    # TRAN-CARD-NUM X(16) at 262.
    card_num: Mapped[str] = mapped_column(
        String(16, collation=KEY_COLLATION),
        ForeignKey("cards.cards.card_num", **FOREIGN_KEY_OPTIONS),
        index=True,
        server_default="",
    )
    # TRAN-ORIG-TS and TRAN-PROC-TS X(26) at 278 and 304; the copybook carries no time zone.
    orig_ts: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=False, precision=6), nullable=True
    )
    proc_ts: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=False, precision=6), nullable=True, index=True
    )
    orig_ts_format: Mapped[str] = mapped_column(CHAR(1), server_default=" ")
    proc_ts_format: Mapped[str] = mapped_column(CHAR(1), server_default=" ")
