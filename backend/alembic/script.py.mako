## Alembic revision template for the CardDemo PostgreSQL migrations in backend/alembic/versions/.
## Lines starting with ## are Mako comments and are never rendered into a revision.
## Create a revision from backend/ with an explicit string ID that follows the chain head:
##     ../.venv/bin/alembic revision --rev-id 0010 -m "short description"
## Before committing the rendered file:
## - Overwrite the upper-case placeholders on the Replaces: line with the copybooks, VSAM clusters
##   and legacy jobs the revision replaces. Wrap a long list onto continuation lines indented
##   4 spaces, so every line stays within 100 columns.
## - Hold column definitions in UPPER_CASE data constants (ACCOUNTS_COLUMNS) and write one
##   PascalCase helper per table (CreateAccountsTable). Locals are camelCase; SQL names stay
##   snake_case. upgrade, downgrade, revision, down_revision, branch_labels and depends_on keep
##   the exact names Alembic looks up: a framework-mandated exception to the MK_Modernization
##   Rule's naming clause.
## - Keep every function within 20 lines and 4 parameters (ruff PLR0913, test_code_standards.py).
## - Never write a database URL or credential here: env.py reads SYNC_DATABASE_URL.
## - Delete the sqlalchemy or alembic.op import if the revision never uses it (ruff F401).
## repr() quotes strings with ', and ruff format (quote-style = "double") would rewrite every
## identifier, so PythonLiteral renders them double-quoted. Revision IDs never contain quotes.
<%!
    def PythonLiteral(value):
        return repr(value).replace("'", '"')
%>\
"""${message}

Replaces: copybooks COPYBOOK_NAMES; VSAM clusters VSAM_CLUSTER_NAMES; jobs LEGACY_JOB_NAMES.

Revision ID: ${up_revision}
## A root revision has no parent; a bare "Revises:" avoids the trailing space ruff format strips.
% if down_revision:
Revises: ${down_revision | comma,n}
% else:
Revises:
% endif
Create Date: ${create_date}
"""

## Ruff's isort (I001) counts alembic as first-party, because backend/alembic/ sits in the ruff
## src root (backend/), so the op import is a section of its own after sqlalchemy. Autogenerate
## imports are sqlalchemy dialect imports, so they join the sqlalchemy section.
from collections.abc import Sequence

import sqlalchemy as sa
% if imports:
${imports}
% endif

from alembic import op

# Revision identifiers, used by Alembic.
revision: str = ${PythonLiteral(up_revision)}
down_revision: str | Sequence[str] | None = ${PythonLiteral(down_revision)}
branch_labels: str | Sequence[str] | None = ${PythonLiteral(branch_labels)}
depends_on: str | Sequence[str] | None = ${PythonLiteral(depends_on)}


## The docstring is a complete function body, so an empty revision renders no "pass", which
## ruff's PIE790 would flag as unnecessary.
def upgrade() -> None:
    """Apply this revision; call one PascalCase helper per table."""
% if upgrades:
    ${upgrades}
% endif


def downgrade() -> None:
    """Reverse exactly the objects upgrade() created."""
% if downgrades:
    ${downgrades}
% endif
