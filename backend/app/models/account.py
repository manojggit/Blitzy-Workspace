"""Replaces: VSAM AWS.M2.CARDDEMO.ACCTDATA.VSAM.KSDS (copybook CVACT01Y).

CICS file ACCTDAT, KEYS(11 0), with no alternate index. One row holds one 300-byte
ACCOUNT-RECORD. The FILLER X(178) at offset 122 is dropped and named in the table comment, so
record-length parity checks against the legacy images still add up.

The money fields are zoned DISPLAY S9(10)V99, not COMP-3. They are exact NUMERIC(12, 2) columns
read as Decimal and are never floating point. ``group_id`` has no foreign key:
``disclosure.disclosure_groups.group_id`` is not unique, and CBACT04C retries with the ``DEFAULT``
group when an account's group, type and category key is not found. ``addr_zip`` and ``group_id``
are decoded strictly by copybook offset (102 and 112), even though the sample ZIP field holds
group-like codes.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import CHAR, Date, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import ACCOUNTS_SCHEMA, KEY_COLLATION, Base, DigitCheck


class Account(Base):
    """ACCOUNT-RECORD of the ACCTDATA KSDS, keyed by the 11-digit ACCT-ID."""

    __tablename__ = "accounts"
    __table_args__ = (
        DigitCheck("acct_id", 11),
        {
            "schema": ACCOUNTS_SCHEMA,
            "comment": "CVACT01Y 300 bytes; FILLER X(178) at offset 122 dropped",
        },
    )

    # Columns follow the copybook order; each comment gives the source field, PIC and offset.
    # Attributes keep the snake_case column names the schema fixes, not the camelCase that
    # MK_Modernization Rule clause 1 sets for variables, so ORM and SQL names stay identical.
    # ACCT-ID 9(11) @0: the KSDS key, KEYS(11 0).
    acct_id: Mapped[str] = mapped_column(String(11, collation=KEY_COLLATION), primary_key=True)
    # ACCT-ACTIVE-STATUS X(01) @11.
    active_status: Mapped[str] = mapped_column(CHAR(1))
    # ACCT-CURR-BAL, ACCT-CREDIT-LIMIT, ACCT-CASH-CREDIT-LIMIT S9(10)V99 @12, @24, @36.
    curr_balance: Mapped[Decimal] = mapped_column(Numeric(12, 2, asdecimal=True))
    credit_limit: Mapped[Decimal] = mapped_column(Numeric(12, 2, asdecimal=True))
    cash_credit_limit: Mapped[Decimal] = mapped_column(Numeric(12, 2, asdecimal=True))
    # ACCT-OPEN-DATE, ACCT-EXPIRAION-DATE (sic), ACCT-REISSUE-DATE X(10) @48, @58, @68.
    # A blank date text is stored as NULL.
    open_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiration_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    reissue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # ACCT-CURR-CYC-CREDIT, ACCT-CURR-CYC-DEBIT S9(10)V99 @78, @90.
    curr_cycle_credit: Mapped[Decimal] = mapped_column(Numeric(12, 2, asdecimal=True))
    curr_cycle_debit: Mapped[Decimal] = mapped_column(Numeric(12, 2, asdecimal=True))
    # ACCT-ADDR-ZIP X(10) @102.
    addr_zip: Mapped[str] = mapped_column(String(10), server_default="")
    # ACCT-GROUP-ID X(10) @112. Collated "C" like disclosure.disclosure_groups.group_id, which
    # INTCALC joins it to: PostgreSQL refuses to compare text columns whose implicit collations
    # differ. Deliberately no ForeignKey, as the module docstring explains.
    group_id: Mapped[str] = mapped_column(String(10, collation=KEY_COLLATION), server_default="")
