"""Create security.login_attempts for the sign-on throttle.

Replaces: no legacy dataset; the RACF password-revocation control that guarded CICS sign-on
    (COSGN00C against USRSEC, copybook CSUSR01Y), replaced by the login throttle of
    services/auth/login_throttle.py.

One row per upper-cased SEC-USR-ID X(08) that has failed to sign on, keyed whether or not the
user exists, so the table deliberately has no foreign key to security.users. A row starts with
``failure_count`` 0 and NULL ``locked_until`` and ``last_failure_at``; the throttle counts each
failure, locks the ID once LOGIN_MAX_ATTEMPTS is reached and deletes the row on a successful
sign-on. The primary key pk_login_attempts is the conflict target of the throttle's upsert.

Every name, type, default and the table comment equal app.models.support_tables.LOGIN_ATTEMPTS,
so ``alembic check`` finds nothing to change. The revision imports nothing from app.models, so it
keeps creating exactly this table as the models evolve.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-03 00:00:00.000000
"""

from collections.abc import Sequence
from typing import NamedTuple

import sqlalchemy as sa

from alembic import op

# Revision identifiers, used by Alembic. Alembic looks these names, and upgrade and downgrade,
# up exactly as spelt, so they keep them instead of the MK_Modernization Rule's naming clause.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SECURITY_SCHEMA = "security"
LOGIN_ATTEMPTS_TABLE = "login_attempts"

# Key text columns compare in byte order, never locale order (app.models.base).
KEY_COLLATION = "C"

LOGIN_ATTEMPTS_COMMENT = (
    "Sign-on failure count and lockout per upper-cased user ID; no legacy equivalent"
)


class ColumnSpec(NamedTuple):
    """One column: name, SQL type, nullability and the server default's SQL text."""

    name: str
    columnType: sa.types.TypeEngine
    nullable: bool = False
    serverDefault: str | None = None


# user_id is the sign-on ID of BMS COSGN00 (LENGTH=8), upper-cased before lookup as COSGN00C does.
LOGIN_ATTEMPTS_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("user_id", sa.String(8, collation=KEY_COLLATION)),
    ColumnSpec("failure_count", sa.SmallInteger(), serverDefault="0"),
    ColumnSpec("locked_until", sa.DateTime(timezone=True), nullable=True),
    ColumnSpec("last_failure_at", sa.DateTime(timezone=True), nullable=True),
)


def BuildLoginAttemptsColumns() -> list[sa.Column]:
    """Return one Column per LOGIN_ATTEMPTS_COLUMNS spec, in spec order."""
    return [
        sa.Column(
            spec.name,
            spec.columnType,
            nullable=spec.nullable,
            server_default=None if spec.serverDefault is None else sa.text(spec.serverDefault),
        )
        for spec in LOGIN_ATTEMPTS_COLUMNS
    ]


def CreateLoginAttemptsTable() -> None:
    """Create security.login_attempts with primary key pk_login_attempts and its comment.

    op.f() marks the key name as final, so the naming convention does not prefix it again, and
    Alembic emits the comment as COMMENT ON TABLE after the CREATE TABLE.
    """
    op.create_table(
        LOGIN_ATTEMPTS_TABLE,
        *BuildLoginAttemptsColumns(),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_login_attempts")),
        schema=SECURITY_SCHEMA,
        comment=LOGIN_ATTEMPTS_COMMENT,
    )


def upgrade() -> None:
    """Create the sign-on throttle table security.login_attempts."""
    CreateLoginAttemptsTable()


def downgrade() -> None:
    """Drop security.login_attempts, restoring revision 0006's schema exactly."""
    op.drop_table(LOGIN_ATTEMPTS_TABLE, schema=SECURITY_SCHEMA)
