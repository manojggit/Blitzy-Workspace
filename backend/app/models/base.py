"""Replaces: VSAM catalog dataset groups (catlg/LISTCAT.txt) as PostgreSQL schemas.

Each VSAM dataset group becomes one PostgreSQL schema, plus ``batch`` for orchestration state,
all on the single ``Base.metadata`` that every model in this package shares. Alembic revisions
0001-0009 are the DDL authority: ``create_all`` is never called, and those revisions write the
constraint and index names that ``NAMING_CONVENTION`` produces here.

This module imports only SQLAlchemy and the standard library, so batch, Alembic, the seed and
tests import the models without settings or a ``SECRET_KEY``.
"""

from typing import Final

from sqlalchemy import CheckConstraint, MetaData
from sqlalchemy.orm import DeclarativeBase

# The migrations name every constraint and index explicitly with these same patterns, and
# tests/integration/test_migrations.py compares the reflected names with Base.metadata.
NAMING_CONVENTION: dict[str, str] = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
}

SECURITY_SCHEMA: Final[str] = "security"
ACCOUNTS_SCHEMA: Final[str] = "accounts"
CUSTOMERS_SCHEMA: Final[str] = "customers"
CARDS_SCHEMA: Final[str] = "cards"
XREF_SCHEMA: Final[str] = "xref"
TRANSACTIONS_SCHEMA: Final[str] = "transactions"
REFERENCE_SCHEMA: Final[str] = "reference"
DISCLOSURE_SCHEMA: Final[str] = "disclosure"
BATCH_SCHEMA: Final[str] = "batch"

# Revision 0001 creates the schemas in this order.
ALL_SCHEMAS: tuple[str, ...] = (
    SECURITY_SCHEMA,
    ACCOUNTS_SCHEMA,
    CUSTOMERS_SCHEMA,
    CARDS_SCHEMA,
    XREF_SCHEMA,
    TRANSACTIONS_SCHEMA,
    REFERENCE_SCHEMA,
    DISCLOSURE_SCHEMA,
    BATCH_SCHEMA,
)

# Passed as ForeignKey("<schema>.<table>.<column>", **FOREIGN_KEY_OPTIONS). Deferrable keys let
# a loader replace a parent table inside one transaction with SET CONSTRAINTS ALL DEFERRED.
FOREIGN_KEY_OPTIONS: dict[str, object] = {"deferrable": True, "initially": "IMMEDIATE"}

# Collation of every key and join text column: byte order, never locale order.
KEY_COLLATION: Final[str] = "C"


class Base(DeclarativeBase):
    """Declarative base whose metadata holds every CardDemo table under NAMING_CONVENTION."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def DigitCheck(columnName: str, digits: int) -> CheckConstraint:
    """Return the PIC 9(n) identifier check on ``columnName``.

    The SQL is ``<column> ~ '^[0-9]{<digits>}$'`` and, through NAMING_CONVENTION, the
    constraint is named ``ck_<table>_<column>_digits`` (for example
    ``ck_accounts_acct_id_digits``), as revision 0005_pic9_digit_checks creates it.
    """
    if digits < 1:
        raise ValueError(f"digit check on {columnName} needs at least one digit, got {digits}")
    return CheckConstraint(f"{columnName} ~ '^[0-9]{{{digits}}}$'", name=f"{columnName}_digits")


def ValueCheck(columnName: str, allowedValues: tuple[str, ...]) -> CheckConstraint:
    """Return the enumerated-value check on ``columnName``.

    The SQL is ``<column> IN ('A', 'B', ...)`` with the values in the given order, and the
    constraint is named ``ck_<table>_<column>``.
    """
    if not allowedValues:
        raise ValueError(f"value check on {columnName} needs at least one allowed value")
    quotedValues = ", ".join("'" + value.replace("'", "''") + "'" for value in allowedValues)
    return CheckConstraint(f"{columnName} IN ({quotedValues})", name=columnName)
