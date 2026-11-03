"""GDG generation catalog for batch file archives.

Replaces: GDG bases AWS.M2.CARDDEMO.TRANSACT.BKUP (LIMIT 5 NOSCRATCH), AWS.M2.CARDDEMO.DALYREJS,
    AWS.M2.CARDDEMO.SYSTRAN and AWS.M2.CARDDEMO.TRANSACT.COMBINED (LIMIT 5 SCRATCH) defined by
    jcl/DEFGDGB.jcl, jcl/DEFGDGD.jcl and jcl/DALYREJS.jcl (attributes per catlg/LISTCAT.txt).

``batch.gdg_generations`` holds one row per generation file of a base, keyed by
(base_name, generation). The ported producers are TRANBKP (TRANSACT.BKUP, LRECL 350), POSTTRAN
(DALYREJS, 430), INTCALC (SYSTRAN, 350) and COMBTRAN (TRANSACT.COMBINED, 350), each writing
``CARDDEMO_OUTPUT_DIR/gdg/<BASE>/G####V00.dat``. base_name holds the logical names that
batch/orchestration/gdg.py defines and is deliberately not constrained here, so any base,
including the reference-data bases of jcl/DEFGDGD.jcl, needs no schema change.

- Lifecycle: a job writes ``G####V00.dat.part``, catalogs it COMMITTED with its SHA-256 as its
  last database write, renames the file and sets the row PUBLISHED. ReconcileCatalog completes
  an interrupted publication, marks a row whose file is gone LOST, and ``seed-all`` or
  ``run-all --new-cycle`` sets the LOST rows of an abandoned cycle ABANDONED.
- Resolution: relative references (0) and (-n) count only PUBLISHED rows, and a LOST row
  blocks its OPEN cycle.
- Retention: LIMIT 5 rolls off the oldest PUBLISHED row as ROLLED_OFF. SCRATCH bases also
  delete its file; TRANSACT.BKUP keeps it, because catlg/LISTCAT.txt (line 1637) records that
  base NOSCRATCH although jcl/DEFGDGB.jcl defines it SCRATCH: DEFGDGB treats an existing base
  as success, so the cataloged attribute is the one in effect.
- Indexes: rows stay in the catalog as history after roll-off and abandonment, so each state
  lookup has a non-unique partial index covering only the rows it selects.
  ix_gdg_generations_run_id_committed serves COMMITTED discovery and a run's publication,
  ix_gdg_generations_run_id_lost the LOST generations of a cycle's runs, and
  ix_gdg_generations_base_name_generation_published the newest-first PUBLISHED reads of
  resolution and retention.

Every name, type, nullability, default, check condition, index column and predicate,
foreign-key option and the table comment equal app.models.support_tables.GDG_GENERATIONS, so
``alembic check`` finds nothing to change; it does not compare index predicates, which
tests/integration/test_batch_state_indexes.py checks instead. No rows are seeded. The revision
imports nothing from app or batch, so it keeps creating exactly this table as the models evolve.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-03 00:20:00.000000
"""

from collections.abc import Sequence
from typing import NamedTuple

import sqlalchemy as sa

from alembic import op

# Revision identifiers, used by Alembic. Alembic looks these names, and upgrade and downgrade,
# up exactly as spelt, so they keep them instead of the MK_Modernization Rule's naming clause.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BATCH_SCHEMA = "batch"
GDG_GENERATIONS_TABLE = "gdg_generations"
GDG_GENERATIONS_COMMENT = (
    "Catalog of GDG generation files; replaces the GDG bases of catlg/LISTCAT.txt"
)

# Key text columns compare in byte order, never locale order (app.models.base).
KEY_COLLATION = "C"

# Frozen copy of app.models.support_tables.GENERATION_STATES. The state check condition lists
# these values in this order.
GENERATION_STATES: tuple[str, ...] = ("COMMITTED", "PUBLISHED", "ROLLED_OFF", "LOST", "ABANDONED")

# Frozen copy of app.models.base.FOREIGN_KEY_OPTIONS.
FOREIGN_KEY_OPTIONS: dict[str, object] = {"deferrable": True, "initially": "IMMEDIATE"}

# Server-default SQL text, matching the model's func.now().
NOW_DEFAULT = "now()"

# Each name below is the naming convention's output, passed through op.f() so the convention
# does not rename it again. The condition is a complete static SQL literal, never formatted
# from variables, with the exact text app.models.base.ValueCheck renders.
PRIMARY_KEY_NAME = "pk_gdg_generations"
PRIMARY_KEY_COLUMNS: tuple[str, ...] = ("base_name", "generation")
STATE_CHECK_NAME = "ck_gdg_generations_state"
STATE_CHECK_CONDITION = "state IN ('COMMITTED', 'PUBLISHED', 'ROLLED_OFF', 'LOST', 'ABANDONED')"
RUN_FOREIGN_KEY_NAME = "fk_gdg_generations_run_id_job_runs"
RUN_FOREIGN_KEY_COLUMN = "run_id"
RUN_REFERENCED_COLUMN = "batch.job_runs.run_id"


class PartialIndexSpec(NamedTuple):
    """One non-unique partial index: its name, its key columns and its static SQL predicate."""

    name: str
    columns: tuple[str, ...]
    predicate: str


# The catalog's state lookups, with the exact predicate texts of app.models.support_tables. Each
# predicate is a complete static SQL literal, never formatted from variables; each name passes
# through op.f(), so the naming convention keeps it as written.
GDG_STATE_INDEXES: tuple[PartialIndexSpec, ...] = (
    PartialIndexSpec("ix_gdg_generations_run_id_committed", ("run_id",), "state = 'COMMITTED'"),
    PartialIndexSpec("ix_gdg_generations_run_id_lost", ("run_id",), "state = 'LOST'"),
    PartialIndexSpec(
        "ix_gdg_generations_base_name_generation_published",
        ("base_name", "generation"),
        "state = 'PUBLISHED'",
    ),
)


class ColumnSpec(NamedTuple):
    """One column: name, SQL type, nullability and the server default's SQL text."""

    name: str
    columnType: sa.types.TypeEngine
    nullable: bool = False
    serverDefault: str | None = None


# base_name allows the 44-character MVS dataset-name maximum; sha256 is the 64-character hex
# digest of the published file; run_id is the job run that produced the generation.
GDG_GENERATIONS_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("base_name", sa.String(44, collation=KEY_COLLATION)),
    ColumnSpec("generation", sa.Integer()),
    ColumnSpec("file_path", sa.Text()),
    ColumnSpec("record_count", sa.BigInteger()),
    ColumnSpec("sha256", sa.CHAR(64)),
    ColumnSpec("state", sa.String(16)),
    ColumnSpec("run_id", sa.BigInteger()),
    ColumnSpec("created_at", sa.DateTime(timezone=True), serverDefault=NOW_DEFAULT),
)


def BuildColumns(columnSpecs: tuple[ColumnSpec, ...]) -> list[sa.Column]:
    """Return one Column per spec, in spec order.

    No catalog column is a sequence: gdg.py numbers each base's generations itself, so the
    integer key column generation is never rendered SERIAL.
    """
    return [
        sa.Column(
            spec.name,
            spec.columnType,
            nullable=spec.nullable,
            server_default=None if spec.serverDefault is None else sa.text(spec.serverDefault),
            autoincrement=False,
        )
        for spec in columnSpecs
    ]


def CreateGdgGenerationsTable() -> None:
    """Create batch.gdg_generations with its key, state check, run key and comment."""
    op.create_table(
        GDG_GENERATIONS_TABLE,
        *BuildColumns(GDG_GENERATIONS_COLUMNS),
        sa.PrimaryKeyConstraint(*PRIMARY_KEY_COLUMNS, name=op.f(PRIMARY_KEY_NAME)),
        sa.CheckConstraint(STATE_CHECK_CONDITION, name=op.f(STATE_CHECK_NAME)),
        sa.ForeignKeyConstraint(
            [RUN_FOREIGN_KEY_COLUMN],
            [RUN_REFERENCED_COLUMN],
            name=op.f(RUN_FOREIGN_KEY_NAME),
            **FOREIGN_KEY_OPTIONS,
        ),
        schema=BATCH_SCHEMA,
        comment=GDG_GENERATIONS_COMMENT,
    )


def CreateGdgStateIndexes() -> None:
    """Create the partial indexes of the COMMITTED, LOST and PUBLISHED catalog lookups."""
    for indexSpec in GDG_STATE_INDEXES:
        op.create_index(
            op.f(indexSpec.name),
            GDG_GENERATIONS_TABLE,
            list(indexSpec.columns),
            schema=BATCH_SCHEMA,
            postgresql_where=sa.text(indexSpec.predicate),
        )


def upgrade() -> None:
    """Create the GDG generation catalog batch.gdg_generations, empty, with its state indexes."""
    CreateGdgGenerationsTable()
    CreateGdgStateIndexes()


def downgrade() -> None:
    """Drop the state indexes and batch.gdg_generations, restoring 0008's schema exactly."""
    for indexSpec in reversed(GDG_STATE_INDEXES):
        op.drop_index(op.f(indexSpec.name), table_name=GDG_GENERATIONS_TABLE, schema=BATCH_SCHEMA)
    op.drop_table(GDG_GENERATIONS_TABLE, schema=BATCH_SCHEMA)
