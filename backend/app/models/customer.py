"""Replaces: VSAM AWS.M2.CARDDEMO.CUSTDATA.VSAM.KSDS (copybook CVCUS01Y).

``Customer`` maps ``customers.customers``, the CICS file CUSTDAT keyed by CUST-ID, ``KEYS(9 0)``.
Columns follow the 500-byte CUSTOMER-RECORD in copybook order; ``cpy/CUSTREC.cpy`` is the same
layout apart from the DOB field name. The trailing ``FILLER X(168)`` at offset 332 is dropped and
recorded in the table comment.

The SSN is stored in full because COACTUPC edits it. API responses mask it with
``core/security.MaskSsn``; this model never masks or renders it, and has no ``__repr__`` so a row
cannot carry the SSN into a log line. The FICO score is a code that responses render zero-padded
to three digits. It has no range check: seed customers below 300 exist and load unchanged.
"""

from datetime import date

from sqlalchemy import CHAR, Date, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CUSTOMERS_SCHEMA, KEY_COLLATION, Base, DigitCheck


class Customer(Base):
    """One CUSTDAT record (CVCUS01Y CUSTOMER-RECORD)."""

    __tablename__ = "customers"
    __table_args__ = (
        DigitCheck("cust_id", 9),
        DigitCheck("ssn", 9),
        {
            "schema": CUSTOMERS_SCHEMA,
            "comment": "CVCUS01Y 500 bytes; FILLER X(168) at offset 332 dropped",
        },
    )

    # CUST-ID 9(09) @0
    cust_id: Mapped[str] = mapped_column(String(9, collation=KEY_COLLATION), primary_key=True)
    # CUST-FIRST-NAME, CUST-MIDDLE-NAME, CUST-LAST-NAME X(25) @9, @34, @59
    first_name: Mapped[str] = mapped_column(String(25), server_default="")
    middle_name: Mapped[str] = mapped_column(String(25), server_default="")
    last_name: Mapped[str] = mapped_column(String(25), server_default="")
    # CUST-ADDR-LINE-1, -2, -3 X(50) @84, @134, @184
    addr_line_1: Mapped[str] = mapped_column(String(50), server_default="")
    addr_line_2: Mapped[str] = mapped_column(String(50), server_default="")
    addr_line_3: Mapped[str] = mapped_column(String(50), server_default="")
    # CUST-ADDR-STATE-CD X(02) @234, CUST-ADDR-COUNTRY-CD X(03) @236, CUST-ADDR-ZIP X(10) @239
    addr_state_cd: Mapped[str] = mapped_column(CHAR(2))
    addr_country_cd: Mapped[str] = mapped_column(CHAR(3))
    addr_zip: Mapped[str] = mapped_column(String(10), server_default="")
    # CUST-PHONE-NUM-1, -2 X(15) @249, @264
    phone_num_1: Mapped[str] = mapped_column(String(15), server_default="")
    phone_num_2: Mapped[str] = mapped_column(String(15), server_default="")
    # CUST-SSN 9(09) @279
    ssn: Mapped[str] = mapped_column(String(9))
    # CUST-GOVT-ISSUED-ID X(20) @288
    govt_issued_id: Mapped[str] = mapped_column(String(20), server_default="")
    # CUST-DOB-YYYY-MM-DD X(10) @308; a blank date is NULL
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    # CUST-EFT-ACCOUNT-ID X(10) @318
    eft_account_id: Mapped[str] = mapped_column(String(10), server_default="")
    # CUST-PRI-CARD-HOLDER-IND X(01) @328
    primary_holder_ind: Mapped[str] = mapped_column(CHAR(1))
    # CUST-FICO-CREDIT-SCORE 9(03) @329
    fico_credit_score: Mapped[int] = mapped_column(SmallInteger)
