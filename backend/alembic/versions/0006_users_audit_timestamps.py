"""Maintain security.users.updated_at on every update.

Replaces: USRSEC (AWS.M2.CARDDEMO.USRSEC.VSAM.KSDS, copybook CSUSR01Y); no legacy audit exists,
    this adds the new updated_at maintenance.

Revision 0001 created ``created_at`` and ``updated_at`` as ``TIMESTAMPTZ NOT NULL DEFAULT
now()``. An INSERT, from COUSR01C, DUSRSECJ or the revision 0002 seed, keeps both defaults and
fires nothing. Every UPDATE, from COUSR02C, fires the BEFORE UPDATE row trigger, which sets
``updated_at`` to the transaction's ``now()`` in the same statement, so the audit value commits
with the write it records. ``created_at`` is never touched again.

The trigger function lives in the ``security`` schema that revision 0001 created; this
revision's downgrade drops it before revision 0001's downgrade drops that schema.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-02 23:55:00.000000
"""

from collections.abc import Sequence

from alembic import op

# Revision identifiers, used by Alembic. Alembic looks these names, and upgrade and downgrade,
# up exactly as spelt, so they keep them instead of the MK_Modernization Rule's naming clause.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Static SQL with no bind parameters. op.execute wraps each string in sqlalchemy.text, whose bind
# syntax is a colon followed by a name: the plpgsql assignment ":=" is a colon followed by "=",
# so the dollar-quoted body reaches PostgreSQL, and the offline --sql script, unchanged.
CREATE_SET_UPDATED_AT_FUNCTION = """\
CREATE FUNCTION security.set_updated_at()
    RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$"""
CREATE_USERS_UPDATED_AT_TRIGGER = """\
CREATE TRIGGER users_set_updated_at
    BEFORE UPDATE ON security.users
    FOR EACH ROW
    EXECUTE FUNCTION security.set_updated_at()"""
DROP_USERS_UPDATED_AT_TRIGGER = "DROP TRIGGER users_set_updated_at ON security.users"
DROP_SET_UPDATED_AT_FUNCTION = "DROP FUNCTION security.set_updated_at()"


def CreateUpdatedAtTrigger() -> None:
    """Create security.set_updated_at, then the security.users trigger that executes it."""
    op.execute(CREATE_SET_UPDATED_AT_FUNCTION)
    op.execute(CREATE_USERS_UPDATED_AT_TRIGGER)


def DropUpdatedAtTrigger() -> None:
    """Drop the security.users trigger, then the function it executes."""
    op.execute(DROP_USERS_UPDATED_AT_TRIGGER)
    op.execute(DROP_SET_UPDATED_AT_FUNCTION)


def upgrade() -> None:
    """Create the updated_at trigger function, then the BEFORE UPDATE trigger on users."""
    CreateUpdatedAtTrigger()


def downgrade() -> None:
    """Drop the trigger, then its function, restoring revision 0005's schema exactly."""
    DropUpdatedAtTrigger()
