"""Replaces: GDG base catalog (jcl/DEFGDGB.jcl, catlg/LISTCAT.txt) and scheduler run state;
adds the login-throttle table.

- ``security.login_attempts``: sign-on failure count and lockout per upper-cased user ID.
- ``batch.cycles``: one row per pass of the 18-step batch chain.
- ``batch.job_runs``: run history, one row per job execution, replacing condition codes.
- ``batch.cycle_steps``: authoritative state of each (cycle, step) identity.
- ``batch.gdg_generations``: generation files of TRANSACT.BKUP, DALYREJS, SYSTRAN and
    TRANSACT.COMBINED, each base keeping LIMIT 5 as cataloged.

These tables hold application state rather than copybook records, so they are SQLAlchemy Core
tables on ``Base.metadata``, not ORM models. Revisions 0007 (login attempts), 0008 (cycles,
job runs, cycle steps) and 0009 (GDG generations) create them with the names declared here.
"""

from sqlalchemy import (
    CHAR,
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import (
    BATCH_SCHEMA,
    FOREIGN_KEY_OPTIONS,
    KEY_COLLATION,
    SECURITY_SCHEMA,
    Base,
    ValueCheck,
)

CYCLE_STATUSES: tuple[str, ...] = ("OPEN", "COMPLETED", "ABANDONED")
CYCLE_OPENERS: tuple[str, ...] = ("SEED", "seed-all", "run-all")
STEP_STATES: tuple[str, ...] = ("PENDING", "COMPLETED", "FAILED", "INTERRUPTED")
RUN_STATUSES: tuple[str, ...] = ("RUNNING", "COMPLETED", "FAILED", "SKIPPED", "INTERRUPTED")
GENERATION_STATES: tuple[str, ...] = ("COMMITTED", "PUBLISHED", "ROLLED_OFF", "LOST", "ABANDONED")

# Keyed by the upper-cased SEC-USR-ID X(08) of every attempt, known user or not, so there is
# deliberately no foreign key to security.users.
LOGIN_ATTEMPTS = Table(
    "login_attempts",
    Base.metadata,
    Column("user_id", String(8, collation=KEY_COLLATION), primary_key=True),
    Column("failure_count", SmallInteger, nullable=False, server_default=text("0")),
    Column("locked_until", DateTime(timezone=True), nullable=True),
    Column("last_failure_at", DateTime(timezone=True), nullable=True),
    schema=SECURITY_SCHEMA,
    comment="Sign-on failure count and lockout per upper-cased user ID; no legacy equivalent",
)

CYCLES = Table(
    "cycles",
    Base.metadata,
    Column("cycle_id", BigInteger, primary_key=True, autoincrement=True),
    Column("status", String(16), nullable=False),
    Column("opened_by", String(16), nullable=False),
    Column("opened_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("closed_at", DateTime(timezone=True), nullable=True),
    ValueCheck("status", CYCLE_STATUSES),
    ValueCheck("opened_by", CYCLE_OPENERS),
    # The OPEN-cycle lookups (FindOpenCycle, AbandonOpenCycle) read no closed cycles.
    Index("ix_cycles_cycle_id_open", "cycle_id", postgresql_where=text("status = 'OPEN'")),
    schema=BATCH_SCHEMA,
    comment="One pass of the 18-step batch chain; replaces scheduler cycle state",
)

# Declared before CYCLE_STEPS, whose last_run_id references it.
JOB_RUNS = Table(
    "job_runs",
    Base.metadata,
    Column("run_id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "cycle_id",
        BigInteger,
        ForeignKey(f"{BATCH_SCHEMA}.cycles.cycle_id", **FOREIGN_KEY_OPTIONS),
        nullable=True,
    ),
    Column("step_no", SmallInteger, nullable=True),
    Column("job_name", String(32), nullable=False),
    Column("params", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("status", String(16), nullable=False),
    Column("exit_code", SmallInteger, nullable=True),
    Column("counts", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    ValueCheck("status", RUN_STATUSES),
    # Run history: any number of rows per (cycle_id, step_no), so the index is not unique.
    Index("ix_job_runs_cycle_id_step_no", "cycle_id", "step_no"),
    # The RUNNING runs ClassifyInterruptedRuns reclassifies, found without reading run history.
    Index("ix_job_runs_run_id_running", "run_id", postgresql_where=text("status = 'RUNNING'")),
    schema=BATCH_SCHEMA,
    comment="Batch run history, one row per job execution; replaces job condition codes",
)

CYCLE_STEPS = Table(
    "cycle_steps",
    Base.metadata,
    Column(
        "cycle_id",
        BigInteger,
        ForeignKey(f"{BATCH_SCHEMA}.cycles.cycle_id", **FOREIGN_KEY_OPTIONS),
        primary_key=True,
        autoincrement=False,
    ),
    Column("step_no", SmallInteger, primary_key=True, autoincrement=False),
    Column("state", String(16), nullable=False),
    Column(
        "last_run_id",
        BigInteger,
        ForeignKey(f"{BATCH_SCHEMA}.job_runs.run_id", **FOREIGN_KEY_OPTIONS),
        nullable=True,
    ),
    ValueCheck("state", STEP_STATES),
    schema=BATCH_SCHEMA,
    comment="Authoritative state of each chain step within a batch cycle",
)

# base_name allows the 44-character MVS dataset-name maximum.
GDG_GENERATIONS = Table(
    "gdg_generations",
    Base.metadata,
    Column("base_name", String(44, collation=KEY_COLLATION), primary_key=True),
    Column("generation", Integer, primary_key=True, autoincrement=False),
    Column("file_path", Text, nullable=False),
    Column("record_count", BigInteger, nullable=False),
    Column("sha256", CHAR(64), nullable=False),
    Column("state", String(16), nullable=False),
    Column(
        "run_id",
        BigInteger,
        ForeignKey(f"{BATCH_SCHEMA}.job_runs.run_id", **FOREIGN_KEY_OPTIONS),
        nullable=False,
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ValueCheck("state", GENERATION_STATES),
    # Rolled-off and abandoned rows stay as history, so each catalog lookup has a partial index.
    # COMMITTED discovery (ReconcileCatalog) and a run's publication (PublishRunGenerations).
    Index(
        "ix_gdg_generations_run_id_committed",
        "run_id",
        postgresql_where=text("state = 'COMMITTED'"),
    ),
    # The LOST generations of a cycle's runs (CheckNoLostGenerations, AbandonOpenCycle).
    Index("ix_gdg_generations_run_id_lost", "run_id", postgresql_where=text("state = 'LOST'")),
    # A base's PUBLISHED generations newest first (ResolveGeneration, RollOff).
    Index(
        "ix_gdg_generations_base_name_generation_published",
        "base_name",
        "generation",
        postgresql_where=text("state = 'PUBLISHED'"),
    ),
    schema=BATCH_SCHEMA,
    comment="Catalog of GDG generation files; replaces the GDG bases of catlg/LISTCAT.txt",
)
