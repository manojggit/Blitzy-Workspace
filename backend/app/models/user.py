"""Replaces: VSAM AWS.M2.CARDDEMO.USRSEC.VSAM.KSDS (copybook CSUSR01Y).

The CICS file USRSEC, keyed ``KEYS(8,0)`` on SEC-USR-ID, becomes ``security.users``. The
plaintext SEC-USR-PWD ``X(08)`` is replaced by ``password_hash``, a bcrypt hash: the password
itself is never stored. ``user_type`` carries no check constraint because COUSR01C accepts any
user type; the role guards derive the role from it (exactly ``A`` is an administrator, every
other value a regular user), so this table restricts nothing.

Revisions 0001 (table, key and comment), 0003 (the generated sort column and its index) and
0006 (the ``updated_at`` trigger) create this table; the model mirrors them name for name.
"""

from datetime import datetime
from typing import ClassVar

from sqlalchemy import CHAR, Computed, DateTime, FetchedValue, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import KEY_COLLATION, SECURITY_SCHEMA, Base


class User(Base):
    """One USRSEC record (SEC-USER-DATA, 80 bytes) without the plaintext password."""

    __tablename__ = "users"
    __table_args__: ClassVar[dict[str, str]] = {
        "schema": SECURITY_SCHEMA,
        "comment": "CSUSR01Y 80 bytes; FILLER X(23) at offset 57 dropped",
    }

    # Attributes are named as the snake_case PostgreSQL columns they map, which the schema fixes;
    # the MK_Modernization Rule's camelCase variable naming does not apply to mapped columns.
    user_id: Mapped[str] = mapped_column(String(8, collation=KEY_COLLATION), primary_key=True)
    # EBCDIC key order for the CU00 browse: VSAM orders USRSEC keys by their cp037 bytes.
    user_id_sort: Mapped[str] = mapped_column(
        Text(collation=KEY_COLLATION),
        Computed("ebcdic_sort_key(user_id)", persisted=True),
        index=True,
    )
    first_name: Mapped[str] = mapped_column(String(20), server_default="")
    last_name: Mapped[str] = mapped_column(String(20), server_default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    user_type: Mapped[str] = mapped_column(CHAR(1))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The revision 0006 trigger sets updated_at; FetchedValue makes the ORM re-read it after an
    # UPDATE and adds no DDL of its own.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        server_onupdate=FetchedValue(),
    )
