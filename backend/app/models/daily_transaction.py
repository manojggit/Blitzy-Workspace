"""Replaces: sequential dataset AWS.M2.CARDDEMO.DALYTRAN.PS (copybook CVTRA06Y), staged for
POSTTRAN.

The daily file (DD DALYTRAN in jcl/POSTTRAN.jcl) is staged in this table of its own rather than
as unposted rows of ``transactions.transactions``. CT00, CT01, the CT02 and CB00 highest-ID
allocation and TRANBKP read that table and must see posted transactions only, which unposted
rows would break unless each read gained a filter the source never had.
``batch/loaders/dalytran.py`` replaces the rows from the image, and POSTTRAN (CBTRN02C) reads
them in ``record_seq`` order and never updates or deletes them.

The columns carry the ``transactions.transactions`` names rather than the DALYTRAN- copybook
names, with the same types and widths, so POSTTRAN copies a staged record field for field.

- ``record_seq`` is the record's position in the daily file, starting at 1.
- ``tran_id`` is indexed but not unique, because a sequential file may repeat an ID.
- ``card_num`` deliberately has no foreign key, so CBTRN02C reject 100
  (``INVALID CARD NUMBER FOUND``) stays reachable for a card the cross-reference lacks.
- ``orig_ts_format`` and ``proc_ts_format`` hold the text form of each 26-byte timestamp
  (``S``, ``D``, ``T`` or blank); a blank timestamp is stored as NULL.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CHAR, Integer, Numeric, SmallInteger, String
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import KEY_COLLATION, TRANSACTIONS_SCHEMA, Base, DigitCheck


class DailyTransaction(Base):
    """One DALYTRAN-RECORD of the daily transaction file, awaiting POSTTRAN."""

    __tablename__ = "daily_transactions"
    __table_args__ = (
        DigitCheck("merchant_id", 9),
        {
            "schema": TRANSACTIONS_SCHEMA,
            "comment": "CVTRA06Y 350 bytes; FILLER X(20) at offset 330 dropped",
        },
    )

    # autoincrement=False keeps the column a plain INTEGER: the loader numbers the records.
    record_seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    # DALYTRAN-ID X(16) @0
    tran_id: Mapped[str] = mapped_column(String(16, collation=KEY_COLLATION), index=True)
    # DALYTRAN-TYPE-CD X(02) @16
    tran_type_cd: Mapped[str] = mapped_column(CHAR(2, collation=KEY_COLLATION))
    # DALYTRAN-CAT-CD 9(04) @18
    tran_cat_cd: Mapped[int] = mapped_column(SmallInteger)
    # DALYTRAN-SOURCE X(10) @22
    tran_source: Mapped[str] = mapped_column(String(10), server_default="")
    # DALYTRAN-DESC X(100) @32
    tran_desc: Mapped[str] = mapped_column(String(100), server_default="")
    # DALYTRAN-AMT S9(09)V99 @132, zoned DISPLAY
    tran_amt: Mapped[Decimal] = mapped_column(Numeric(11, 2, asdecimal=True))
    # DALYTRAN-MERCHANT-ID 9(09) @143
    merchant_id: Mapped[str] = mapped_column(String(9))
    # DALYTRAN-MERCHANT-NAME X(50) @152
    merchant_name: Mapped[str] = mapped_column(String(50), server_default="")
    # DALYTRAN-MERCHANT-CITY X(50) @202
    merchant_city: Mapped[str] = mapped_column(String(50), server_default="")
    # DALYTRAN-MERCHANT-ZIP X(10) @252
    merchant_zip: Mapped[str] = mapped_column(String(10), server_default="")
    # DALYTRAN-CARD-NUM X(16) @262; collated "C" for POSTTRAN's join to xref.card_xref.card_num
    card_num: Mapped[str] = mapped_column(String(16, collation=KEY_COLLATION))
    # DALYTRAN-ORIG-TS X(26) @278
    orig_ts: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=False, precision=6), nullable=True
    )
    # DALYTRAN-PROC-TS X(26) @304, blank in every sample record
    proc_ts: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=False, precision=6), nullable=True
    )
    orig_ts_format: Mapped[str] = mapped_column(CHAR(1), server_default=" ")
    proc_ts_format: Mapped[str] = mapped_column(CHAR(1), server_default=" ")
