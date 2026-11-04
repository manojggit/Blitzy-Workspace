"""Integration tests of the migrated CardDemo schema: revisions 0001-0009, head 0009.

Replaces: no legacy test. It proves the PostgreSQL database that replaces the CardDemo VSAM
clusters catalogued in catlg/LISTCAT.txt and bound as CICS files in csd/CARDDEMO.CSD, and the
IDCAMS DEFINE CLUSTER, DEFINE AIX, BLDINDEX and REPRO steps that built and loaded them:

- 0001: the nine schemas and the eleven dataset tables of copybooks CSUSR01Y (USRSEC),
  CVACT01Y (ACCTDATA), CVCUS01Y (CUSTDATA), CVACT02Y (CARDDATA), CVACT03Y (CARDXREF),
  CVTRA05Y (TRANSACT), CVTRA06Y (DALYTRAN), CVTRA01Y (TCATBALF), CVTRA02Y (DISCGRP),
  CVTRA03Y (TRANTYPE) and CVTRA04Y (TRANCATG), each commented with its dropped FILLER;
- 0002: the sample seed from data/EBCDIC, the USRSEC passwords stored only as bcrypt hashes;
- 0003: public.ebcdic_sort_key, the EBCDIC sort columns and the alternate-index replacements
  of CARDAIX, CXACAIX and the TRANSACT PROC-TS path;
- 0004: the deferrable foreign keys; 0005: the PIC 9 digit checks;
- 0006: the security.users updated_at trigger; 0007: security.login_attempts;
- 0008: the batch cycle, step and run ledger with the seed cycle; 0009: the GDG catalog.

The session's migratedDatabase fixture has rebuilt the test database at head. Alembic is also
run as a subprocess from backend/, as the run instructions do, with the session environment, so
it reaches the same test database. No test downgrades. The one test that writes, the trigger
test, runs under restoreSeedState, so every test leaves the database as migrated.
"""

import os
import subprocess
import sys
from collections.abc import Sequence
from typing import NamedTuple, Protocol

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import (
    CheckConstraint,
    Constraint,
    Engine,
    ForeignKeyConstraint,
    Row,
    Table,
    TextClause,
    func,
    select,
    text,
    update,
)

from app.core.paths import REPO_ROOT
from app.core.security import VerifyPassword
from app.models import ALL_SCHEMAS, CYCLE_STEPS, CYCLES, JOB_RUNS, Base

# --- Alembic -------------------------------------------------------------------------------------
BACKEND_DIRECTORY = REPO_ROOT / "backend"
ALEMBIC_INI_PATH = BACKEND_DIRECTORY / "alembic.ini"
ALEMBIC_TIMEOUT_SECONDS = 120
HEAD_REVISION = "0009"
HEAD_MARKER = "0009 (head)"
REVISION_CHAIN: tuple[str, ...] = (
    "0001",
    "0002",
    "0003",
    "0004",
    "0005",
    "0006",
    "0007",
    "0008",
    "0009",
)

# --- Schemas and tables (AAP 0.6.7) --------------------------------------------------------------
EXPECTED_SCHEMAS = frozenset(
    {
        "security",
        "accounts",
        "customers",
        "cards",
        "xref",
        "transactions",
        "reference",
        "disclosure",
        "batch",
    }
)
EXPECTED_TABLES = frozenset(
    {
        "security.users",
        "security.login_attempts",
        "accounts.accounts",
        "customers.customers",
        "cards.cards",
        "xref.card_xref",
        "transactions.transactions",
        "transactions.daily_transactions",
        "transactions.transaction_category_balances",
        "reference.transaction_types",
        "reference.transaction_categories",
        "disclosure.disclosure_groups",
        "batch.cycles",
        "batch.cycle_steps",
        "batch.job_runs",
        "batch.gdg_generations",
    }
)

# Multi-column keys: the VSAM composite KEYS clauses and the batch ledger identities.
COMPOSITE_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "transactions.transaction_category_balances": ("acct_id", "tran_type_cd", "tran_cat_cd"),
    "disclosure.disclosure_groups": ("group_id", "tran_type_cd", "tran_cat_cd"),
    "reference.transaction_categories": ("tran_type_cd", "tran_cat_cd"),
    "batch.cycle_steps": ("cycle_id", "step_no"),
    "batch.gdg_generations": ("base_name", "generation"),
}
PRIMARY_KEY_PREFIX = "pk_"


# --- Indexes: the EBCDIC sort columns, the AIX replacements and the run-history index ------------
class IndexDefinition(NamedTuple):
    """A planned index: its table full name, its columns in key order and whether it is partial."""

    tableName: str
    keyColumns: tuple[str, ...]
    isPartial: bool = False


# Stated here from the plan, not read from revisions 0003 and 0008 or from app.models, so an
# index that keeps its name on another table or column fails. Each covers its whole table.
INDEX_DEFINITIONS: dict[str, IndexDefinition] = {
    "ix_users_user_id_sort": IndexDefinition("security.users", ("user_id_sort",)),
    "ix_cards_card_num_sort": IndexDefinition("cards.cards", ("card_num_sort",)),
    "ix_transactions_tran_id_sort": IndexDefinition("transactions.transactions", ("tran_id_sort",)),
    # CARDAIX KEYS(11 16): CARD-ACCT-ID.
    "ix_cards_acct_id": IndexDefinition("cards.cards", ("acct_id",)),
    # CXACAIX KEYS(11,25): XREF-ACCT-ID.
    "ix_card_xref_acct_id": IndexDefinition("xref.card_xref", ("acct_id",)),
    "ix_card_xref_cust_id": IndexDefinition("xref.card_xref", ("cust_id",)),
    "ix_transactions_card_num": IndexDefinition("transactions.transactions", ("card_num",)),
    # TRANSACT AIX KEYS(26 304): TRAN-PROC-TS.
    "ix_transactions_proc_ts": IndexDefinition("transactions.transactions", ("proc_ts",)),
    "ix_daily_transactions_tran_id": IndexDefinition(
        "transactions.daily_transactions", ("tran_id",)
    ),
    "ix_job_runs_cycle_id_step_no": IndexDefinition("batch.job_runs", ("cycle_id", "step_no")),
}
EXPECTED_INDEXES = frozenset(INDEX_DEFINITIONS)

# --- Constraints ---------------------------------------------------------------------------------
PRIMARY_KEY_TYPE = "p"
FOREIGN_KEY_TYPE = "f"
CHECK_TYPE = "c"


class ForeignKeyDefinition(NamedTuple):
    """A planned relationship: the referencing table and columns, the referenced table and key."""

    tableName: str
    localColumns: tuple[str, ...]
    referencedTable: str
    referencedColumns: tuple[str, ...]


# Revision 0004 (the eight dataset relationships), 0008 and 0009 (the batch ledger and catalog),
# stated here from the plan rather than read from those revisions or app.models, so a key that
# keeps its name on another column or referenced key fails.
FOREIGN_KEY_DEFINITIONS: dict[str, ForeignKeyDefinition] = {
    "fk_cards_acct_id_accounts": ForeignKeyDefinition(
        "cards.cards", ("acct_id",), "accounts.accounts", ("acct_id",)
    ),
    "fk_card_xref_card_num_cards": ForeignKeyDefinition(
        "xref.card_xref", ("card_num",), "cards.cards", ("card_num",)
    ),
    "fk_card_xref_cust_id_customers": ForeignKeyDefinition(
        "xref.card_xref", ("cust_id",), "customers.customers", ("cust_id",)
    ),
    "fk_card_xref_acct_id_accounts": ForeignKeyDefinition(
        "xref.card_xref", ("acct_id",), "accounts.accounts", ("acct_id",)
    ),
    "fk_transactions_card_num_cards": ForeignKeyDefinition(
        "transactions.transactions", ("card_num",), "cards.cards", ("card_num",)
    ),
    "fk_transaction_category_balances_acct_id_accounts": ForeignKeyDefinition(
        "transactions.transaction_category_balances",
        ("acct_id",),
        "accounts.accounts",
        ("acct_id",),
    ),
    "fk_disclosure_groups_tran_type_cd_transaction_categories": ForeignKeyDefinition(
        "disclosure.disclosure_groups",
        ("tran_type_cd", "tran_cat_cd"),
        "reference.transaction_categories",
        ("tran_type_cd", "tran_cat_cd"),
    ),
    # The TRANTYPE key column is tran_type, not tran_type_cd.
    "fk_transaction_categories_tran_type_cd_transaction_types": ForeignKeyDefinition(
        "reference.transaction_categories",
        ("tran_type_cd",),
        "reference.transaction_types",
        ("tran_type",),
    ),
    "fk_job_runs_cycle_id_cycles": ForeignKeyDefinition(
        "batch.job_runs", ("cycle_id",), "batch.cycles", ("cycle_id",)
    ),
    "fk_cycle_steps_cycle_id_cycles": ForeignKeyDefinition(
        "batch.cycle_steps", ("cycle_id",), "batch.cycles", ("cycle_id",)
    ),
    "fk_cycle_steps_last_run_id_job_runs": ForeignKeyDefinition(
        "batch.cycle_steps", ("last_run_id",), "batch.job_runs", ("run_id",)
    ),
    "fk_gdg_generations_run_id_job_runs": ForeignKeyDefinition(
        "batch.gdg_generations", ("run_id",), "batch.job_runs", ("run_id",)
    ),
}
EXPECTED_FOREIGN_KEYS = frozenset(FOREIGN_KEY_DEFINITIONS)

# Revision 0005: each PIC 9(n) identifier and its exact-width digit pattern. The tenth check,
# on the TCATBALF account key, is the revision's and the model's own.
DIGIT_CHECKS: dict[str, str] = {
    "ck_accounts_acct_id_digits": "^[0-9]{11}$",
    "ck_customers_cust_id_digits": "^[0-9]{9}$",
    "ck_customers_ssn_digits": "^[0-9]{9}$",
    "ck_cards_acct_id_digits": "^[0-9]{11}$",
    "ck_cards_cvv_cd_digits": "^[0-9]{3}$",
    "ck_card_xref_cust_id_digits": "^[0-9]{9}$",
    "ck_card_xref_acct_id_digits": "^[0-9]{11}$",
    "ck_transactions_merchant_id_digits": "^[0-9]{9}$",
    "ck_daily_transactions_merchant_id_digits": "^[0-9]{9}$",
    "ck_transaction_category_balances_acct_id_digits": "^[0-9]{11}$",
}
# Revisions 0008 and 0009: the enumerated batch states.
VALUE_CHECKS = frozenset(
    {
        "ck_cycles_status",
        "ck_cycles_opened_by",
        "ck_job_runs_status",
        "ck_cycle_steps_state",
        "ck_gdg_generations_state",
    }
)


class UnconstrainedColumn(NamedTuple):
    """A column that deliberately carries no constraint of ``constraintType``."""

    tableName: str
    columnName: str
    constraintType: str


# COUSR01C accepts any user type and COACTUPC alone edits the FICO range, so neither has a
# check. ACCT-GROUP-ID falls back to DEFAULT in CBACT04C, the daily card must stay unmatched for
# reject 100, and COTRN02C and CBTRN02C accept any type and category codes: no foreign keys.
UNCONSTRAINED_COLUMNS: tuple[UnconstrainedColumn, ...] = (
    UnconstrainedColumn("security.users", "user_type", CHECK_TYPE),
    UnconstrainedColumn("customers.customers", "fico_credit_score", CHECK_TYPE),
    UnconstrainedColumn("accounts.accounts", "group_id", FOREIGN_KEY_TYPE),
    UnconstrainedColumn("transactions.daily_transactions", "card_num", FOREIGN_KEY_TYPE),
    UnconstrainedColumn("transactions.transactions", "tran_type_cd", FOREIGN_KEY_TYPE),
    UnconstrainedColumn("transactions.transactions", "tran_cat_cd", FOREIGN_KEY_TYPE),
    UnconstrainedColumn(
        "transactions.transaction_category_balances", "tran_type_cd", FOREIGN_KEY_TYPE
    ),
    UnconstrainedColumn(
        "transactions.transaction_category_balances", "tran_cat_cd", FOREIGN_KEY_TYPE
    ),
)


# --- Negative controls: a reflected definition changed while its name is kept --------------------
class DefinitionChange(NamedTuple):
    """One field of a reflected row replaced, and the definition field the comparison must name."""

    caseName: str
    objectName: str
    rowField: str
    changedValue: object
    reportedField: str


FOREIGN_KEY_CHANGES: tuple[DefinitionChange, ...] = (
    DefinitionChange(
        "wrong_local_table",
        "fk_transactions_card_num_cards",
        "tableName",
        "transactions.daily_transactions",
        "tableName",
    ),
    DefinitionChange(
        "wrong_local_column",
        "fk_card_xref_acct_id_accounts",
        "keyColumns",
        ("cust_id",),
        "localColumns",
    ),
    DefinitionChange(
        "wrong_local_column_order",
        "fk_disclosure_groups_tran_type_cd_transaction_categories",
        "keyColumns",
        ("tran_cat_cd", "tran_type_cd"),
        "localColumns",
    ),
    DefinitionChange(
        "wrong_referenced_table",
        "fk_card_xref_card_num_cards",
        "referencedTable",
        "transactions.transactions",
        "referencedTable",
    ),
    DefinitionChange(
        "wrong_referenced_column",
        "fk_transaction_categories_tran_type_cd_transaction_types",
        "referencedColumns",
        ("tran_type_cd",),
        "referencedColumns",
    ),
)
INDEX_CHANGES: tuple[DefinitionChange, ...] = (
    DefinitionChange(
        "wrong_table",
        "ix_transactions_card_num",
        "tableName",
        "transactions.daily_transactions",
        "tableName",
    ),
    DefinitionChange(
        "wrong_column", "ix_cards_acct_id", "indexColumns", ("card_num",), "keyColumns"
    ),
    DefinitionChange(
        "wrong_column_order",
        "ix_job_runs_cycle_id_step_no",
        "indexColumns",
        ("step_no", "cycle_id"),
        "keyColumns",
    ),
    DefinitionChange(
        "expression_key",
        "ix_card_xref_acct_id",
        "indexColumns",
        ("lower(acct_id::text)",),
        "keyColumns",
    ),
    DefinitionChange(
        "included_column",
        "ix_card_xref_cust_id",
        "indexColumns",
        ("cust_id", "acct_id"),
        "keyColumns",
    ),
    DefinitionChange(
        "partial_predicate", "ix_transactions_proc_ts", "isPartial", True, "isPartial"
    ),
)

# --- Columns -------------------------------------------------------------------------------------


class SortColumn(NamedTuple):
    """A generated EBCDIC sort column and the key column its expression reads."""

    schemaName: str
    tableName: str
    columnName: str
    keyColumn: str


SORT_COLUMNS: tuple[SortColumn, ...] = (
    SortColumn("security", "users", "user_id_sort", "user_id"),
    SortColumn("cards", "cards", "card_num_sort", "card_num"),
    SortColumn("transactions", "transactions", "tran_id_sort", "tran_id"),
)
GENERATED_ALWAYS = "ALWAYS"
STORED_GENERATION = "s"
KEY_COLLATION = "C"
SORT_KEY_FUNCTION = "ebcdic_sort_key"
IMMUTABLE_VOLATILITY = "i"


class SortOrderCase(NamedTuple):
    """Keys in ASCII order and the cp037 byte order ebcdic_sort_key gives them."""

    asciiOrder: tuple[str, ...]
    ebcdicOrder: tuple[str, ...]


# Lower case sorts before upper case before digits; the mixed keys are the CU00 paging keys.
SORT_ORDER_CASES: tuple[SortOrderCase, ...] = (
    SortOrderCase(("0", "A", "a"), ("a", "A", "0")),
    SortOrderCase(("1A", "A1", "AA", "ADMIN001", "a1"), ("a1", "AA", "ADMIN001", "A1", "1A")),
)


class ColumnType(NamedTuple):
    """A column and its PostgreSQL type as format_type renders it."""

    tableName: str
    columnName: str
    sqlType: str


COLUMN_TYPES: tuple[ColumnType, ...] = (
    ColumnType("accounts.accounts", "curr_balance", "numeric(12,2)"),
    ColumnType("transactions.transactions", "tran_amt", "numeric(11,2)"),
    ColumnType("disclosure.disclosure_groups", "interest_rate", "numeric(6,2)"),
    ColumnType("transactions.transactions", "orig_ts", "timestamp(6) without time zone"),
    ColumnType("transactions.transactions", "proc_ts", "timestamp(6) without time zone"),
    ColumnType("security.users", "password_hash", "character varying(255)"),
    ColumnType("cards.cards", "cvv_cd", "character(3)"),
)
# Money is exact NUMERIC, never floating point.
FLOATING_POINT_TYPES: tuple[str, ...] = ("double precision", "real")

# --- FILLER comments: copybook, record length and the dropped FILLER picture and offset ---------
FILLER_COMMENTS: dict[str, str] = {
    "security.users": "CSUSR01Y 80 bytes; FILLER X(23) at offset 57 dropped",
    "accounts.accounts": "CVACT01Y 300 bytes; FILLER X(178) at offset 122 dropped",
    "customers.customers": "CVCUS01Y 500 bytes; FILLER X(168) at offset 332 dropped",
    "cards.cards": "CVACT02Y 150 bytes; FILLER X(59) at offset 91 dropped",
    "xref.card_xref": "CVACT03Y 50 bytes; FILLER X(14) at offset 36 dropped",
    "transactions.transactions": "CVTRA05Y 350 bytes; FILLER X(20) at offset 330 dropped",
    "transactions.daily_transactions": "CVTRA06Y 350 bytes; FILLER X(20) at offset 330 dropped",
    "transactions.transaction_category_balances": (
        "CVTRA01Y 50 bytes; FILLER X(22) at offset 28 dropped"
    ),
    "disclosure.disclosure_groups": "CVTRA02Y 50 bytes; FILLER X(28) at offset 22 dropped",
    "reference.transaction_types": "CVTRA03Y 60 bytes; FILLER X(08) at offset 52 dropped",
    "reference.transaction_categories": "CVTRA04Y 60 bytes; FILLER X(04) at offset 56 dropped",
}

# --- Audit trigger (revision 0006) ---------------------------------------------------------------
USERS_TABLE_NAME = "security.users"
UPDATED_AT_TRIGGER = "users_set_updated_at"
TRIGGER_TIMING = "BEFORE UPDATE"
TRIGGER_LEVEL = "FOR EACH ROW"
TRIGGER_FUNCTION = "security.set_updated_at()"

# --- Sample seed (revision 0002) and its cycle (revision 0008) -----------------------------------
EXPECTED_SEED_COUNTS: dict[str, int] = {
    "security.users": 10,
    "accounts.accounts": 50,
    "customers.customers": 50,
    "cards.cards": 50,
    "xref.card_xref": 50,
    "reference.transaction_types": 7,
    "reference.transaction_categories": 18,
    "disclosure.disclosure_groups": 51,
    "transactions.transaction_category_balances": 50,
    "transactions.daily_transactions": 300,
    # TRANFILE's only record is the priming record, which the seed skips.
    "transactions.transactions": 0,
}
BCRYPT_PREFIX = "$2"
DAILY_TABLE_NAME = "transactions.daily_transactions"
DAILY_RECORD_COUNT = 300
FIRST_RECORD_SEQ = 1
# DALYTRAN ORIG-TS is YYYY-MM-DD HH:MM:SS.ffffff in every sample record; PROC-TS is blank.
SPACED_TIMESTAMP_FORMAT = "S"

SEED_OPENER = "SEED"
OPEN_CYCLE_STATUS = "OPEN"
COMPLETED_STEP_STATE = "COMPLETED"
PENDING_STEP_STATE = "PENDING"
COMPLETED_RUN_STATUS = "COMPLETED"
CHAIN_STEP_NUMBERS = range(1, 19)
# The seed performs the loaders of steps 1-5 and 7-12; TRANBKP (6) and steps 13-18 wait.
UNSEEDED_STEPS = frozenset({6, 13, 14, 15, 16, 17, 18})
EXPECTED_SEED_STEP_STATES: dict[int, str] = {
    stepNo: PENDING_STEP_STATE if stepNo in UNSEEDED_STEPS else COMPLETED_STEP_STATE
    for stepNo in CHAIN_STEP_NUMBERS
}

USERS_TABLE = Base.metadata.tables[USERS_TABLE_NAME]
DAILY_TABLE = Base.metadata.tables[DAILY_TABLE_NAME]

# --- Catalog SQL: static text, every value bound; :schemas is always list(ALL_SCHEMAS) ----------
VERSION_SQL = text("SELECT version_num FROM public.alembic_version")
SCHEMAS_SQL = text(
    "SELECT n.nspname::text FROM pg_catalog.pg_namespace AS n WHERE n.nspname = ANY(:schemas)"
)
TABLES_SQL = text(
    "SELECT n.nspname || '.' || c.relname"
    " FROM pg_catalog.pg_class AS c"
    " JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace"
    " WHERE n.nspname = ANY(:schemas) AND c.relkind IN ('r', 'p')"
)
CONSTRAINTS_SQL = text(
    "SELECT n.nspname || '.' || c.relname AS table_name, con.conname::text AS constraint_name,"
    " con.contype::text AS constraint_type, con.condeferrable AS is_deferrable,"
    " con.condeferred AS is_initially_deferred,"
    " array_agg(a.attname::text ORDER BY k.ordinality) AS key_columns,"
    " pg_catalog.pg_get_constraintdef(con.oid) AS definition,"
    " coalesce((SELECT rn.nspname || '.' || rc.relname FROM pg_catalog.pg_class AS rc"
    " JOIN pg_catalog.pg_namespace AS rn ON rn.oid = rc.relnamespace"
    " WHERE rc.oid = con.confrelid), '') AS referenced_table,"
    " ARRAY(SELECT ra.attname::text"
    " FROM unnest(con.confkey) WITH ORDINALITY AS rk(attnum, ordinality)"
    " JOIN pg_catalog.pg_attribute AS ra ON ra.attrelid = con.confrelid AND ra.attnum = rk.attnum"
    " ORDER BY rk.ordinality) AS referenced_columns"
    " FROM pg_catalog.pg_constraint AS con"
    " JOIN pg_catalog.pg_class AS c ON c.oid = con.conrelid"
    " JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace"
    " CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ordinality)"
    " JOIN pg_catalog.pg_attribute AS a ON a.attrelid = con.conrelid AND a.attnum = k.attnum"
    " WHERE n.nspname = ANY(:schemas)"
    " GROUP BY n.nspname, c.relname, con.oid, con.conname, con.contype, con.condeferrable,"
    " con.condeferred, con.confrelid, con.confkey"
)
# Each column position renders as its column name, or as its expression for an expression key;
# INCLUDE columns follow the key columns.
INDEXES_SQL = text(
    "SELECT ic.relname::text AS index_name, ix.indisunique AS is_unique,"
    " ix.indisvalid AS is_valid, ix.indisprimary AS is_primary,"
    " tn.nspname || '.' || tc.relname AS table_name,"
    " ARRAY(SELECT pg_catalog.pg_get_indexdef(ix.indexrelid, indexed.column_no, TRUE)"
    " FROM generate_series(1, ix.indnatts) AS indexed(column_no)"
    " ORDER BY indexed.column_no) AS index_columns,"
    " ix.indpred IS NOT NULL AS is_partial"
    " FROM pg_catalog.pg_index AS ix"
    " JOIN pg_catalog.pg_class AS ic ON ic.oid = ix.indexrelid"
    " JOIN pg_catalog.pg_namespace AS n ON n.oid = ic.relnamespace"
    " JOIN pg_catalog.pg_class AS tc ON tc.oid = ix.indrelid"
    " JOIN pg_catalog.pg_namespace AS tn ON tn.oid = tc.relnamespace"
    " WHERE n.nspname = ANY(:schemas)"
)
COLUMN_TYPE_SQL = text(
    "SELECT pg_catalog.format_type(a.atttypid, a.atttypmod)"
    " FROM pg_catalog.pg_attribute AS a"
    " WHERE a.attrelid = to_regclass(:tableName) AND a.attname = :columnName"
    " AND NOT a.attisdropped"
)
FLOATING_POINT_COLUMNS_SQL = text(
    "SELECT col.table_schema || '.' || col.table_name || '.' || col.column_name"
    " FROM information_schema.columns AS col"
    " WHERE col.table_schema = ANY(:schemas) AND col.data_type = ANY(:floatingPointTypes)"
)
SORT_COLUMN_SQL = text(
    "SELECT col.is_generated::text AS is_generated, col.collation_name::text AS collation_name,"
    " col.generation_expression::text AS generation_expression,"
    " a.attgenerated::text AS generated_kind"
    " FROM information_schema.columns AS col"
    " JOIN pg_catalog.pg_namespace AS n ON n.nspname = col.table_schema"
    " JOIN pg_catalog.pg_class AS c ON c.relnamespace = n.oid AND c.relname = col.table_name"
    " JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid AND a.attname = col.column_name"
    " WHERE col.table_schema = :schemaName AND col.table_name = :tableName"
    " AND col.column_name = :columnName"
)
FUNCTION_VOLATILITY_SQL = text(
    "SELECT p.provolatile::text FROM pg_catalog.pg_proc AS p"
    " JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace"
    " WHERE n.nspname = 'public' AND p.proname = :functionName"
)
EBCDIC_ORDER_SQL = text(
    "SELECT sort_input.key_text FROM unnest(CAST(:keys AS text[])) AS sort_input(key_text)"
    ' ORDER BY public.ebcdic_sort_key(sort_input.key_text) COLLATE "C"'
)
TABLE_COMMENT_SQL = text("SELECT pg_catalog.obj_description(to_regclass(:tableName), 'pg_class')")
TRIGGERS_SQL = text(
    "SELECT t.tgname::text AS trigger_name, pg_catalog.pg_get_triggerdef(t.oid) AS definition"
    " FROM pg_catalog.pg_trigger AS t"
    " WHERE t.tgrelid = to_regclass(:tableName) AND NOT t.tgisinternal"
)


# --- Value objects -------------------------------------------------------------------------------
class ConstraintRow(NamedTuple):
    """One reflected constraint: its table, name, pg_constraint type, deferral and key columns.

    referencedTable and referencedColumns are a foreign key's referenced table full name and
    ordered key columns; for every other type they are empty.
    """

    tableName: str
    constraintName: str
    constraintType: str
    isDeferrable: bool
    isInitiallyDeferred: bool
    keyColumns: tuple[str, ...]
    definition: str
    referencedTable: str
    referencedColumns: tuple[str, ...]


class IndexRow(NamedTuple):
    """One reflected index of a CardDemo schema, its table full name and its ordered columns."""

    indexName: str
    isUnique: bool
    isValid: bool
    isPrimary: bool
    tableName: str
    indexColumns: tuple[str, ...]
    isPartial: bool


class SeedUserRecord(Protocol):
    """The fields of conftest's SeedUser that the seed check reads."""

    userId: str
    firstName: str
    lastName: str
    password: str
    userType: str


# --- Helpers -------------------------------------------------------------------------------------
def SchemaParameters() -> dict[str, list[str]]:
    """Return the :schemas binding that limits every catalog query to ALL_SCHEMAS."""
    return {"schemas": list(ALL_SCHEMAS)}


def FetchRows(engine: Engine, statement: TextClause, parameters: dict[str, object]) -> list[Row]:
    """Return every row of ``statement`` run with ``parameters``."""
    with engine.connect() as connection:
        return list(connection.execute(statement, parameters))


def FetchNames(engine: Engine, statement: TextClause, parameters: dict[str, object]) -> set[str]:
    """Return the first column of every row of ``statement`` as a set."""
    return {row[0] for row in FetchRows(engine, statement, parameters)}


def FetchScalar(engine: Engine, statement: TextClause, parameters: dict[str, object]) -> object:
    """Return the first column of the first row of ``statement``, or None without a row."""
    with engine.connect() as connection:
        return connection.execute(statement, parameters).scalar()


def RunAlembic(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run ``python -m alembic <arguments>`` from backend/ with the session's environment.

    conftest has pointed SYNC_DATABASE_URL at the test database in os.environ, so alembic.ini
    and env.py resolve that database exactly as a developer's run from backend/ resolves theirs.
    """
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=BACKEND_DIRECTORY,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=ALEMBIC_TIMEOUT_SECONDS,
        check=False,
    )


def ConstraintsOfType(
    constraintRows: Sequence[ConstraintRow], constraintType: str
) -> list[ConstraintRow]:
    """Return the reflected constraints whose pg_constraint type is ``constraintType``."""
    return [row for row in constraintRows if row.constraintType == constraintType]


def NamedForeignKeys(
    constraintRows: Sequence[ConstraintRow], foreignKeyName: str
) -> list[ConstraintRow]:
    """Return the reflected foreign keys named ``foreignKeyName``: one for a planned key."""
    foreignKeys = ConstraintsOfType(constraintRows, FOREIGN_KEY_TYPE)
    return [row for row in foreignKeys if row.constraintName == foreignKeyName]


def FieldMismatches(
    expected: ForeignKeyDefinition | IndexDefinition,
    reflected: ForeignKeyDefinition | IndexDefinition,
) -> list[str]:
    """Return, in field order, the names of the fields ``reflected`` and ``expected`` differ in."""
    return [
        fieldName
        for fieldName, expectedValue, reflectedValue in zip(
            expected._fields, expected, reflected, strict=True
        )
        if expectedValue != reflectedValue
    ]


def ForeignKeyMismatches(
    definition: ForeignKeyDefinition, constraintRow: ConstraintRow
) -> list[str]:
    """Return the fields of ``definition`` that the reflected foreign key does not reproduce."""
    reflected = ForeignKeyDefinition(
        constraintRow.tableName,
        constraintRow.keyColumns,
        constraintRow.referencedTable,
        constraintRow.referencedColumns,
    )
    return FieldMismatches(definition, reflected)


def IndexMismatches(definition: IndexDefinition, indexRow: IndexRow) -> list[str]:
    """Return the fields of ``definition`` that the reflected index does not reproduce."""
    reflected = IndexDefinition(indexRow.tableName, indexRow.indexColumns, indexRow.isPartial)
    return FieldMismatches(definition, reflected)


def ModelConstraintNames(constraintClass: type[Constraint]) -> set[str]:
    """Return the names Base.metadata gives its constraints of ``constraintClass``."""
    return {
        str(constraint.name)
        for table in Base.metadata.sorted_tables
        for constraint in table.constraints
        if isinstance(constraint, constraintClass)
    }


def ModelPrimaryKeys() -> dict[str, tuple[str, tuple[str, ...]]]:
    """Return each model table's primary-key name and key columns, by table full name."""
    return {
        table.fullname: (
            str(table.primary_key.name),
            tuple(keyColumn.name for keyColumn in table.primary_key.columns),
        )
        for table in Base.metadata.sorted_tables
    }


def ModelIndexNames() -> set[str]:
    """Return the names of every index Base.metadata declares."""
    return {str(index.name) for table in Base.metadata.sorted_tables for index in table.indexes}


def CountRows(engine: Engine, table: Table) -> int:
    """Return the number of rows ``table`` holds."""
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(table)).scalar_one()


def ReadUserRows(engine: Engine) -> dict[str, Row]:
    """Return every security.users row with its names, type and hash, by user ID."""
    statement = select(
        USERS_TABLE.c.user_id,
        USERS_TABLE.c.first_name,
        USERS_TABLE.c.last_name,
        USERS_TABLE.c.user_type,
        USERS_TABLE.c.password_hash,
    )
    with engine.connect() as connection:
        return {userRow.user_id: userRow for userRow in connection.execute(statement)}


def SeedUserMismatches(seedUser: SeedUserRecord, userRow: Row) -> list[str]:
    """Return the names of the checks ``userRow`` fails for ``seedUser``.

    Only check names are returned, so a failure never reports the image password or its hash.
    """
    checks = {
        "names": (userRow.first_name, userRow.last_name) == (seedUser.firstName, seedUser.lastName),
        "user_type": userRow.user_type == seedUser.userType,
        "bcrypt_hash": userRow.password_hash.startswith(BCRYPT_PREFIX),
        "not_plaintext": userRow.password_hash != seedUser.password,
        "verifies": VerifyPassword(seedUser.password, userRow.password_hash),
    }
    return [checkName for checkName, passed in checks.items() if not passed]


def ReadAuditTimes(engine: Engine, userId: str) -> Row:
    """Return created_at and updated_at of ``userId``, read in a transaction of their own."""
    statement = select(USERS_TABLE.c.created_at, USERS_TABLE.c.updated_at).where(
        USERS_TABLE.c.user_id == userId
    )
    with engine.begin() as connection:
        return connection.execute(statement).one()


def TouchUser(engine: Engine, userId: str) -> Row:
    """Rewrite ``userId`` unchanged in a later transaction; return its audit times after it."""
    statement = (
        update(USERS_TABLE)
        .where(USERS_TABLE.c.user_id == userId)
        .values(first_name=USERS_TABLE.c.first_name)
        .returning(USERS_TABLE.c.created_at, USERS_TABLE.c.updated_at)
    )
    with engine.begin() as connection:
        return connection.execute(statement).one()


def ReadDailyStagingSummary(engine: Engine) -> Row:
    """Return the row count, record_seq range and timestamp-form checks of the daily staging."""
    statement = select(
        func.count(),
        func.min(DAILY_TABLE.c.record_seq),
        func.max(DAILY_TABLE.c.record_seq),
        func.bool_and(DAILY_TABLE.c.orig_ts_format == SPACED_TIMESTAMP_FORMAT),
        func.bool_and(DAILY_TABLE.c.proc_ts.is_(None)),
    )
    with engine.connect() as connection:
        return connection.execute(statement).one()


def ReadCycleSteps(engine: Engine, cycleId: int) -> dict[int, Row]:
    """Return the cycle_steps rows of ``cycleId`` by step number."""
    statement = select(CYCLE_STEPS.c.step_no, CYCLE_STEPS.c.state, CYCLE_STEPS.c.last_run_id).where(
        CYCLE_STEPS.c.cycle_id == cycleId
    )
    with engine.connect() as connection:
        return {stepRow.step_no: stepRow for stepRow in connection.execute(statement)}


def ReadCycleRuns(engine: Engine, cycleId: int) -> list[Row]:
    """Return the job_runs rows of ``cycleId``."""
    statement = select(JOB_RUNS.c.run_id, JOB_RUNS.c.step_no, JOB_RUNS.c.status).where(
        JOB_RUNS.c.cycle_id == cycleId
    )
    with engine.connect() as connection:
        return list(connection.execute(statement))


# --- Fixtures ------------------------------------------------------------------------------------
@pytest.fixture(name="migratedEngine", scope="module")
def MigratedEngine(syncEngine: Engine, migratedDatabase: object) -> Engine:
    """Return the psycopg2 engine of the test database once it has been migrated to head."""
    return syncEngine


@pytest.fixture(name="constraintRows", scope="module")
def ConstraintRows(migratedEngine: Engine) -> tuple[ConstraintRow, ...]:
    """Return every constraint of the CardDemo schemas, its key columns and referenced key."""
    return tuple(
        ConstraintRow(
            *row[:5],
            tuple(row.key_columns),
            row.definition,
            row.referenced_table,
            tuple(row.referenced_columns),
        )
        for row in FetchRows(migratedEngine, CONSTRAINTS_SQL, SchemaParameters())
    )


@pytest.fixture(name="indexRows", scope="module")
def IndexRows(migratedEngine: Engine) -> dict[str, IndexRow]:
    """Return every index of the CardDemo schemas, with its table and ordered columns, by name."""
    return {
        row.index_name: IndexRow(*row[:5], tuple(row.index_columns), row.is_partial)
        for row in FetchRows(migratedEngine, INDEXES_SQL, SchemaParameters())
    }


@pytest.fixture(name="seedCycleId", scope="module")
def SeedCycleId(migratedEngine: Engine) -> int:
    """Return the cycle_id of the cycle revision 0008 recorded for the seed."""
    statement = select(CYCLES.c.cycle_id).where(CYCLES.c.opened_by == SEED_OPENER)
    with migratedEngine.connect() as connection:
        return connection.execute(statement).scalar_one()


# --- Alembic head and subprocess runs ------------------------------------------------------------
def test_revision_chain_is_linear_from_0001_to_head_0009():
    scriptDirectory = ScriptDirectory.from_config(Config(str(ALEMBIC_INI_PATH)))
    assert scriptDirectory.get_heads() == [HEAD_REVISION]
    revisionIds = [script.revision for script in scriptDirectory.walk_revisions()]
    assert tuple(reversed(revisionIds)) == REVISION_CHAIN


def test_alembic_version_table_holds_head_0009(migratedEngine):
    assert FetchNames(migratedEngine, VERSION_SQL, {}) == {HEAD_REVISION}


def test_alembic_current_reports_head_0009(migratedEngine):
    currentResult = RunAlembic(("current",))
    assert currentResult.returncode == 0, currentResult.stderr
    assert HEAD_MARKER in currentResult.stdout, currentResult.stdout


def test_alembic_upgrade_head_runs_from_backend_directory(migratedEngine):
    upgradeResult = RunAlembic(("upgrade", "head"))
    assert upgradeResult.returncode == 0, upgradeResult.stderr
    currentResult = RunAlembic(("current",))
    assert currentResult.returncode == 0, currentResult.stderr
    assert HEAD_MARKER in currentResult.stdout, currentResult.stdout


# --- Schemas, tables and primary keys ------------------------------------------------------------
def test_dataset_group_schemas_exist(migratedEngine):
    assert set(ALL_SCHEMAS) == EXPECTED_SCHEMAS
    assert FetchNames(migratedEngine, SCHEMAS_SQL, SchemaParameters()) == EXPECTED_SCHEMAS


def test_tables_match_the_plan_and_the_models(migratedEngine):
    modelTables = {table.fullname for table in Base.metadata.sorted_tables}
    assert modelTables == EXPECTED_TABLES
    assert FetchNames(migratedEngine, TABLES_SQL, SchemaParameters()) == EXPECTED_TABLES


def test_primary_keys_are_named_pk_table_and_match_the_models(constraintRows):
    reflectedKeys = {
        row.tableName: (row.constraintName, row.keyColumns)
        for row in ConstraintsOfType(constraintRows, PRIMARY_KEY_TYPE)
    }
    expectedNames = {
        tableName: PRIMARY_KEY_PREFIX + tableName.split(".")[1] for tableName in EXPECTED_TABLES
    }
    assert {tableName: key[0] for tableName, key in reflectedKeys.items()} == expectedNames
    assert reflectedKeys == ModelPrimaryKeys()


@pytest.mark.parametrize(
    ("tableName", "keyColumns"),
    list(COMPOSITE_PRIMARY_KEYS.items()),
    ids=list(COMPOSITE_PRIMARY_KEYS),
)
def test_composite_primary_key_has_the_vsam_key_columns_in_order(
    constraintRows, tableName, keyColumns
):
    primaryKeys = ConstraintsOfType(constraintRows, PRIMARY_KEY_TYPE)
    reflectedColumns = [row.keyColumns for row in primaryKeys if row.tableName == tableName]
    assert reflectedColumns == [keyColumns]


# --- Indexes -------------------------------------------------------------------------------------
@pytest.mark.parametrize("indexName", sorted(EXPECTED_INDEXES))
def test_alternate_index_replacement_is_valid_and_not_unique(indexRows, indexName):
    indexRow = indexRows[indexName]
    assert (indexRow.isUnique, indexRow.isValid, indexRow.isPrimary) == (False, True, False)


@pytest.mark.parametrize("indexName", sorted(EXPECTED_INDEXES))
def test_planned_index_covers_its_whole_table_on_the_planned_columns(indexRows, indexName):
    indexRow = indexRows[indexName]
    assert IndexMismatches(INDEX_DEFINITIONS[indexName], indexRow) == [], indexRow


@pytest.mark.parametrize("change", INDEX_CHANGES, ids=[item.caseName for item in INDEX_CHANGES])
def test_index_comparison_reports_a_changed_definition_under_the_same_name(indexRows, change):
    definition = INDEX_DEFINITIONS[change.objectName]
    reflectedRow = indexRows[change.objectName]
    assert IndexMismatches(definition, reflectedRow) == []
    changedRow = reflectedRow._replace(**{change.rowField: change.changedValue})
    assert changedRow.indexName == change.objectName
    assert IndexMismatches(definition, changedRow) == [change.reportedField]


# Names only: the predicates of the partial batch-state indexes are compared with the models by
# test_batch_state_indexes.py.
def test_secondary_indexes_match_the_models(indexRows):
    secondaryIndexes = {name for name, indexRow in indexRows.items() if not indexRow.isPrimary}
    assert secondaryIndexes == ModelIndexNames()
    assert EXPECTED_INDEXES <= secondaryIndexes


# --- Foreign keys and checks ---------------------------------------------------------------------
def test_foreign_keys_are_exactly_the_planned_relationships(constraintRows):
    foreignKeys = ConstraintsOfType(constraintRows, FOREIGN_KEY_TYPE)
    assert {row.constraintName for row in foreignKeys} == EXPECTED_FOREIGN_KEYS
    assert ModelConstraintNames(ForeignKeyConstraint) == EXPECTED_FOREIGN_KEYS


@pytest.mark.parametrize("foreignKeyName", sorted(EXPECTED_FOREIGN_KEYS))
def test_foreign_key_joins_the_planned_columns_to_the_planned_key(constraintRows, foreignKeyName):
    definition = FOREIGN_KEY_DEFINITIONS[foreignKeyName]
    namedKeys = NamedForeignKeys(constraintRows, foreignKeyName)
    mismatches = [ForeignKeyMismatches(definition, row) for row in namedKeys]
    assert mismatches == [[]], namedKeys


@pytest.mark.parametrize(
    "change", FOREIGN_KEY_CHANGES, ids=[item.caseName for item in FOREIGN_KEY_CHANGES]
)
def test_foreign_key_comparison_reports_a_changed_definition_under_the_same_name(
    constraintRows, change
):
    definition = FOREIGN_KEY_DEFINITIONS[change.objectName]
    [reflectedRow] = NamedForeignKeys(constraintRows, change.objectName)
    assert ForeignKeyMismatches(definition, reflectedRow) == []
    changedRow = reflectedRow._replace(**{change.rowField: change.changedValue})
    assert changedRow.constraintName == change.objectName
    assert ForeignKeyMismatches(definition, changedRow) == [change.reportedField]


# All twelve, the four batch keys included: every model key passes FOREIGN_KEY_OPTIONS.
@pytest.mark.parametrize("foreignKeyName", sorted(EXPECTED_FOREIGN_KEYS))
def test_foreign_key_is_deferrable_initially_immediate(constraintRows, foreignKeyName):
    foreignKeys = ConstraintsOfType(constraintRows, FOREIGN_KEY_TYPE)
    deferral = [
        (row.isDeferrable, row.isInitiallyDeferred)
        for row in foreignKeys
        if row.constraintName == foreignKeyName
    ]
    assert deferral == [(True, False)]


def test_check_constraints_are_the_digit_and_value_checks(constraintRows):
    expectedChecks = set(DIGIT_CHECKS) | VALUE_CHECKS
    checks = ConstraintsOfType(constraintRows, CHECK_TYPE)
    assert {row.constraintName for row in checks} == expectedChecks
    assert ModelConstraintNames(CheckConstraint) == expectedChecks


@pytest.mark.parametrize(
    ("checkName", "digitPattern"), list(DIGIT_CHECKS.items()), ids=DIGIT_CHECKS
)
def test_digit_check_requires_exactly_n_digits(constraintRows, checkName, digitPattern):
    checks = ConstraintsOfType(constraintRows, CHECK_TYPE)
    definitions = [row.definition for row in checks if row.constraintName == checkName]
    assert len(definitions) == 1
    assert f"'{digitPattern}'" in definitions[0]


@pytest.mark.parametrize(
    "unconstrained",
    UNCONSTRAINED_COLUMNS,
    ids=[f"{item.tableName}.{item.columnName}" for item in UNCONSTRAINED_COLUMNS],
)
def test_column_carries_no_constraint_of_the_excluded_kind(constraintRows, unconstrained):
    assert unconstrained.columnName in Base.metadata.tables[unconstrained.tableName].c
    constrainedColumns = {
        keyColumn
        for row in ConstraintsOfType(constraintRows, unconstrained.constraintType)
        if row.tableName == unconstrained.tableName
        for keyColumn in row.keyColumns
    }
    assert unconstrained.columnName not in constrainedColumns


# --- Generated sort columns and the EBCDIC sort key ----------------------------------------------
@pytest.mark.parametrize("sortColumn", SORT_COLUMNS, ids=[item.columnName for item in SORT_COLUMNS])
def test_sort_column_is_generated_always_stored_with_c_collation(migratedEngine, sortColumn):
    parameters = sortColumn._asdict()
    columnRows = FetchRows(migratedEngine, SORT_COLUMN_SQL, parameters)
    assert len(columnRows) == 1
    columnRow = columnRows[0]
    assert (columnRow.is_generated, columnRow.generated_kind) == (
        GENERATED_ALWAYS,
        STORED_GENERATION,
    )
    assert columnRow.collation_name == KEY_COLLATION
    assert SORT_KEY_FUNCTION in columnRow.generation_expression
    assert sortColumn.keyColumn in columnRow.generation_expression


def test_ebcdic_sort_key_is_immutable(migratedEngine):
    parameters = {"functionName": SORT_KEY_FUNCTION}
    volatility = FetchScalar(migratedEngine, FUNCTION_VOLATILITY_SQL, parameters)
    assert volatility == IMMUTABLE_VOLATILITY


@pytest.mark.parametrize("sortCase", SORT_ORDER_CASES, ids=["single_characters", "cu00_keys"])
def test_ebcdic_sort_key_orders_lower_before_upper_before_digits(migratedEngine, sortCase):
    parameters = {"keys": list(sortCase.asciiOrder)}
    orderedRows = FetchRows(migratedEngine, EBCDIC_ORDER_SQL, parameters)
    assert tuple(row.key_text for row in orderedRows) == sortCase.ebcdicOrder


# --- Column types --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "columnType",
    COLUMN_TYPES,
    ids=[f"{item.tableName}.{item.columnName}" for item in COLUMN_TYPES],
)
def test_column_type_follows_its_copybook_picture(migratedEngine, columnType):
    parameters = {"tableName": columnType.tableName, "columnName": columnType.columnName}
    assert FetchScalar(migratedEngine, COLUMN_TYPE_SQL, parameters) == columnType.sqlType


def test_no_column_is_floating_point(migratedEngine):
    parameters = {**SchemaParameters(), "floatingPointTypes": list(FLOATING_POINT_TYPES)}
    assert FetchNames(migratedEngine, FLOATING_POINT_COLUMNS_SQL, parameters) == set()


# --- FILLER comments -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("tableName", "fillerComment"), list(FILLER_COMMENTS.items()), ids=list(FILLER_COMMENTS)
)
def test_table_comment_records_the_dropped_filler(migratedEngine, tableName, fillerComment):
    reflectedComment = FetchScalar(migratedEngine, TABLE_COMMENT_SQL, {"tableName": tableName})
    assert reflectedComment == fillerComment
    assert Base.metadata.tables[tableName].comment == fillerComment


# --- Audit trigger -------------------------------------------------------------------------------
def test_users_audit_trigger_runs_before_each_row_update(migratedEngine):
    parameters = {"tableName": USERS_TABLE_NAME}
    triggerRows = FetchRows(migratedEngine, TRIGGERS_SQL, parameters)
    assert [row.trigger_name for row in triggerRows] == [UPDATED_AT_TRIGGER]
    triggerDefinition = triggerRows[0].definition
    assert TRIGGER_TIMING in triggerDefinition
    assert TRIGGER_LEVEL in triggerDefinition
    assert TRIGGER_FUNCTION in triggerDefinition


def test_users_update_trigger_advances_updated_at(migratedEngine, seedUsers, restoreSeedState):
    userId = seedUsers[0].userId
    beforeUpdate = ReadAuditTimes(migratedEngine, userId)
    afterUpdate = TouchUser(migratedEngine, userId)
    assert afterUpdate.created_at == beforeUpdate.created_at
    assert afterUpdate.updated_at > beforeUpdate.updated_at


# --- Sample seed ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("tableName", "expectedCount"),
    list(EXPECTED_SEED_COUNTS.items()),
    ids=list(EXPECTED_SEED_COUNTS),
)
def test_seed_loads_the_sample_row_count(migratedEngine, tableName, expectedCount):
    assert CountRows(migratedEngine, Base.metadata.tables[tableName]) == expectedCount


def test_seed_users_verify_against_image_passwords(migratedEngine, seedUsers):
    userRows = ReadUserRows(migratedEngine)
    assert set(userRows) == {seedUser.userId for seedUser in seedUsers}
    mismatches = {
        seedUser.userId: SeedUserMismatches(seedUser, userRows[seedUser.userId])
        for seedUser in seedUsers
    }
    assert mismatches == {seedUser.userId: [] for seedUser in seedUsers}


def test_daily_staging_runs_in_record_order_with_spaced_orig_ts_and_blank_proc_ts(migratedEngine):
    stagingSummary = ReadDailyStagingSummary(migratedEngine)
    assert tuple(stagingSummary) == (
        DAILY_RECORD_COUNT,
        FIRST_RECORD_SEQ,
        DAILY_RECORD_COUNT,
        True,
        True,
    )


# --- Seed cycle ----------------------------------------------------------------------------------
# restoreSeedState restores every Base.metadata table, batch.* included, so the ledger read here
# is the one migration left, whatever earlier tests of the session ran.
def test_seed_is_recorded_as_the_only_cycle_open_and_opened_by_seed(migratedEngine):
    statement = select(CYCLES.c.status, CYCLES.c.opened_by)
    with migratedEngine.connect() as connection:
        cycleRows = [tuple(cycleRow) for cycleRow in connection.execute(statement)]
    assert cycleRows == [(OPEN_CYCLE_STATUS, SEED_OPENER)]


def test_seed_cycle_completes_only_the_seeded_loader_steps(migratedEngine, seedCycleId):
    stepRows = ReadCycleSteps(migratedEngine, seedCycleId)
    assert {stepNo: stepRow.state for stepNo, stepRow in stepRows.items()} == (
        EXPECTED_SEED_STEP_STATES
    )
    pendingRunIds = [
        stepRow.last_run_id for stepRow in stepRows.values() if stepRow.state == PENDING_STEP_STATE
    ]
    assert pendingRunIds == [None] * len(UNSEEDED_STEPS)


def test_completed_seed_steps_reference_their_completed_runs(migratedEngine, seedCycleId):
    stepRows = ReadCycleSteps(migratedEngine, seedCycleId)
    runRows = ReadCycleRuns(migratedEngine, seedCycleId)
    completedRunIds = {
        stepNo: stepRow.last_run_id
        for stepNo, stepRow in stepRows.items()
        if stepRow.state == COMPLETED_STEP_STATE
    }
    assert len(runRows) == len(completedRunIds)
    assert {runRow.step_no: runRow.run_id for runRow in runRows} == completedRunIds
    assert {runRow.status for runRow in runRows} == {COMPLETED_RUN_STATUS}
