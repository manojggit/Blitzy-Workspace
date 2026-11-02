"""Create the CardDemo dataset schemas and the eleven copybook-derived dataset tables.

Replaces: copybooks CSUSR01Y, CVACT01Y, CVCUS01Y, CVACT02Y, CVACT03Y, CVTRA05Y, CVTRA06Y,
    CVTRA01Y, CVTRA02Y, CVTRA03Y, CVTRA04Y; VSAM clusters AWS.M2.CARDDEMO.{USRSEC,ACCTDATA,
    CUSTDATA,CARDDATA,CARDXREF,TRANSACT,TCATBALF,DISCGRP,TRANTYPE,TRANCATG}.VSAM.KSDS and
    sequential DALYTRAN.PS; jobs ACCTFILE, CARDFILE, CUSTFILE, XREFFILE, TRANFILE, TRANTYPE,
    TRANCATG, DISCGRP, TCATBALF, DUSRSECJ (DEFINE CLUSTER steps).

Each table's primary key replaces its cluster's KEYS clause, and its table comment records the
copybook record length and the dropped FILLER, so record-length parity checks against the legacy
images still add up. Every column is NOT NULL except the dates and timestamps that a blank legacy
field leaves NULL. Money fields are zoned DISPLAY S9(n)V99, so they are exact NUMERIC columns and
never floating point.

This revision creates no foreign key, check, index or generated column: revisions 0003-0005 add
the EBCDIC sort columns, the alternate-index replacements, the foreign keys and the PIC 9 digit
checks. The ``batch`` schema is created here for the tables of revisions 0008 and 0009. The
revision never imports ``app.models``, so it keeps creating exactly this schema as the models
evolve.

Revision ID: 0001
Revises:
Create Date: 2026-11-02 22:20:30.000000
"""

from collections.abc import Sequence
from typing import NamedTuple

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# Revision identifiers, used by Alembic. Alembic looks these names, and upgrade and downgrade,
# up exactly as spelt, so they keep them instead of the MK_Modernization Rule's naming clause.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# One schema per dataset group, plus batch for orchestration state, in creation order.
DATASET_SCHEMAS: tuple[str, ...] = (
    "security",
    "accounts",
    "customers",
    "cards",
    "xref",
    "transactions",
    "reference",
    "disclosure",
    "batch",
)

# Key and join text columns compare in byte order, never locale order (app.models.base).
KEY_COLLATION = "C"

# Server-default SQL texts, matching the models' server_default values.
EMPTY_DEFAULT = "''"
SPACE_DEFAULT = "' '"
NOW_DEFAULT = "now()"


class ColumnSpec(NamedTuple):
    """One dataset column: name, SQL type, nullability and the server default's SQL text."""

    name: str
    columnType: sa.types.TypeEngine
    nullable: bool = False
    serverDefault: str | None = None


class TableSpec(NamedTuple):
    """One dataset table: schema, name, columns, primary-key columns and table comment."""

    schema: str
    name: str
    columns: tuple[ColumnSpec, ...]
    primaryKey: tuple[str, ...]
    comment: str


# CSUSR01Y SEC-USER-DATA. SEC-USR-PWD X(08) becomes a bcrypt hash; the audit columns are new.
USERS_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("user_id", sa.String(8, collation=KEY_COLLATION), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("first_name", sa.String(20), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("last_name", sa.String(20), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("password_hash", sa.String(255)),
    ColumnSpec("user_type", sa.CHAR(1)),
    ColumnSpec("created_at", sa.DateTime(timezone=True), serverDefault=NOW_DEFAULT),
    ColumnSpec("updated_at", sa.DateTime(timezone=True), serverDefault=NOW_DEFAULT),
)

# CVACT01Y ACCOUNT-RECORD. The S9(10)V99 money fields are zoned DISPLAY, not COMP-3.
ACCOUNTS_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("acct_id", sa.String(11, collation=KEY_COLLATION)),
    ColumnSpec("active_status", sa.CHAR(1)),
    ColumnSpec("curr_balance", sa.Numeric(12, 2)),
    ColumnSpec("credit_limit", sa.Numeric(12, 2)),
    ColumnSpec("cash_credit_limit", sa.Numeric(12, 2)),
    ColumnSpec("open_date", sa.Date(), nullable=True),
    ColumnSpec("expiration_date", sa.Date(), nullable=True),
    ColumnSpec("reissue_date", sa.Date(), nullable=True),
    ColumnSpec("curr_cycle_credit", sa.Numeric(12, 2)),
    ColumnSpec("curr_cycle_debit", sa.Numeric(12, 2)),
    ColumnSpec("addr_zip", sa.String(10), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("group_id", sa.String(10, collation=KEY_COLLATION), serverDefault=EMPTY_DEFAULT),
)

# CVCUS01Y CUSTOMER-RECORD; CUSTREC is the same layout apart from the DOB field name.
CUSTOMERS_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("cust_id", sa.String(9, collation=KEY_COLLATION)),
    ColumnSpec("first_name", sa.String(25), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("middle_name", sa.String(25), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("last_name", sa.String(25), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("addr_line_1", sa.String(50), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("addr_line_2", sa.String(50), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("addr_line_3", sa.String(50), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("addr_state_cd", sa.CHAR(2)),
    ColumnSpec("addr_country_cd", sa.CHAR(3)),
    ColumnSpec("addr_zip", sa.String(10), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("phone_num_1", sa.String(15), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("phone_num_2", sa.String(15), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("ssn", sa.String(9)),
    ColumnSpec("govt_issued_id", sa.String(20), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("date_of_birth", sa.Date(), nullable=True),
    ColumnSpec("eft_account_id", sa.String(10), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("primary_holder_ind", sa.CHAR(1)),
    ColumnSpec("fico_credit_score", sa.SmallInteger()),
)

# CVTRA03Y TRAN-TYPE-RECORD.
TRANSACTION_TYPES_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("tran_type", sa.CHAR(2, collation=KEY_COLLATION)),
    ColumnSpec("description", sa.String(50), serverDefault=EMPTY_DEFAULT),
)

# CVTRA04Y TRAN-CAT-RECORD; TRAN-CAT-CD 9(04) is a code, stored as SMALLINT.
TRANSACTION_CATEGORIES_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("tran_type_cd", sa.CHAR(2, collation=KEY_COLLATION)),
    ColumnSpec("tran_cat_cd", sa.SmallInteger()),
    ColumnSpec("description", sa.String(50), serverDefault=EMPTY_DEFAULT),
)

# CVACT02Y CARD-RECORD; CARD-EXPIRAION-DATE (sic) X(10) becomes expiration_date.
CARDS_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("card_num", sa.String(16, collation=KEY_COLLATION), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("acct_id", sa.String(11, collation=KEY_COLLATION)),
    ColumnSpec("cvv_cd", sa.CHAR(3)),
    ColumnSpec("embossed_name", sa.String(50), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("expiration_date", sa.Date(), nullable=True),
    ColumnSpec("active_status", sa.CHAR(1)),
)

# CVACT03Y CARD-XREF-RECORD.
CARD_XREF_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("card_num", sa.String(16, collation=KEY_COLLATION), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("cust_id", sa.String(9, collation=KEY_COLLATION)),
    ColumnSpec("acct_id", sa.String(11, collation=KEY_COLLATION)),
)

# CVTRA05Y TRAN-RECORD and CVTRA06Y DALYTRAN-RECORD share this layout field for field. Each
# X(26) timestamp is a zone-free TIMESTAMP(6) plus a CHAR(1) code naming its text form.
TRAN_RECORD_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("tran_id", sa.String(16, collation=KEY_COLLATION), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("tran_type_cd", sa.CHAR(2, collation=KEY_COLLATION)),
    ColumnSpec("tran_cat_cd", sa.SmallInteger()),
    ColumnSpec("tran_source", sa.String(10), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("tran_desc", sa.String(100), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("tran_amt", sa.Numeric(11, 2)),
    ColumnSpec("merchant_id", sa.String(9)),
    ColumnSpec("merchant_name", sa.String(50), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("merchant_city", sa.String(50), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("merchant_zip", sa.String(10), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("card_num", sa.String(16, collation=KEY_COLLATION), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("orig_ts", postgresql.TIMESTAMP(timezone=False, precision=6), nullable=True),
    ColumnSpec("proc_ts", postgresql.TIMESTAMP(timezone=False, precision=6), nullable=True),
    ColumnSpec("orig_ts_format", sa.CHAR(1), serverDefault=SPACE_DEFAULT),
    ColumnSpec("proc_ts_format", sa.CHAR(1), serverDefault=SPACE_DEFAULT),
)

# The daily file's record position, starting at 1, keys the staged rows.
DAILY_TRANSACTIONS_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("record_seq", sa.Integer()),
    *TRAN_RECORD_COLUMNS,
)

# CVTRA01Y TRAN-CAT-BAL-RECORD; the 17-byte TRAN-CAT-KEY becomes the three key columns.
TRANSACTION_CATEGORY_BALANCES_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("acct_id", sa.String(11, collation=KEY_COLLATION)),
    ColumnSpec("tran_type_cd", sa.CHAR(2, collation=KEY_COLLATION)),
    ColumnSpec("tran_cat_cd", sa.SmallInteger()),
    ColumnSpec("balance", sa.Numeric(11, 2)),
)

# CVTRA02Y DIS-GROUP-RECORD; DIS-INT-RATE S9(04)V99 has six digits, so NUMERIC(6, 2).
DISCLOSURE_GROUPS_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("group_id", sa.String(10, collation=KEY_COLLATION), serverDefault=EMPTY_DEFAULT),
    ColumnSpec("tran_type_cd", sa.CHAR(2, collation=KEY_COLLATION)),
    ColumnSpec("tran_cat_cd", sa.SmallInteger()),
    ColumnSpec("interest_rate", sa.Numeric(6, 2)),
)

# Parent tables come before the tables that revision 0004 points foreign keys at them from.
DATASET_TABLES: tuple[TableSpec, ...] = (
    TableSpec(
        "security",
        "users",
        USERS_COLUMNS,
        ("user_id",),
        "CSUSR01Y 80 bytes; FILLER X(23) at offset 57 dropped",
    ),
    TableSpec(
        "accounts",
        "accounts",
        ACCOUNTS_COLUMNS,
        ("acct_id",),
        "CVACT01Y 300 bytes; FILLER X(178) at offset 122 dropped",
    ),
    TableSpec(
        "customers",
        "customers",
        CUSTOMERS_COLUMNS,
        ("cust_id",),
        "CVCUS01Y 500 bytes; FILLER X(168) at offset 332 dropped",
    ),
    TableSpec(
        "reference",
        "transaction_types",
        TRANSACTION_TYPES_COLUMNS,
        ("tran_type",),
        "CVTRA03Y 60 bytes; FILLER X(08) at offset 52 dropped",
    ),
    TableSpec(
        "reference",
        "transaction_categories",
        TRANSACTION_CATEGORIES_COLUMNS,
        ("tran_type_cd", "tran_cat_cd"),
        "CVTRA04Y 60 bytes; FILLER X(04) at offset 56 dropped",
    ),
    TableSpec(
        "cards",
        "cards",
        CARDS_COLUMNS,
        ("card_num",),
        "CVACT02Y 150 bytes; FILLER X(59) at offset 91 dropped",
    ),
    TableSpec(
        "xref",
        "card_xref",
        CARD_XREF_COLUMNS,
        ("card_num",),
        "CVACT03Y 50 bytes; FILLER X(14) at offset 36 dropped",
    ),
    TableSpec(
        "transactions",
        "transactions",
        TRAN_RECORD_COLUMNS,
        ("tran_id",),
        "CVTRA05Y 350 bytes; FILLER X(20) at offset 330 dropped",
    ),
    TableSpec(
        "transactions",
        "daily_transactions",
        DAILY_TRANSACTIONS_COLUMNS,
        ("record_seq",),
        "CVTRA06Y 350 bytes; FILLER X(20) at offset 330 dropped",
    ),
    TableSpec(
        "transactions",
        "transaction_category_balances",
        TRANSACTION_CATEGORY_BALANCES_COLUMNS,
        ("acct_id", "tran_type_cd", "tran_cat_cd"),
        "CVTRA01Y 50 bytes; FILLER X(22) at offset 28 dropped",
    ),
    TableSpec(
        "disclosure",
        "disclosure_groups",
        DISCLOSURE_GROUPS_COLUMNS,
        ("group_id", "tran_type_cd", "tran_cat_cd"),
        "CVTRA02Y 50 bytes; FILLER X(28) at offset 22 dropped",
    ),
)


def BuildColumns(columnSpecs: tuple[ColumnSpec, ...]) -> list[sa.Column]:
    """Return one Column per spec, in spec order.

    No column autoincrements: every key, record_seq included, is assigned by the legacy record
    or its loader, so no SERIAL sequence may be created.
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


def CreateDatasetTable(tableSpec: TableSpec) -> None:
    """Create one dataset table with its primary key pk_<table> and its FILLER comment.

    op.f() marks the key name as final, so the naming convention does not prefix it again, and
    Alembic emits the comment as COMMENT ON TABLE after the CREATE TABLE.
    """
    op.create_table(
        tableSpec.name,
        *BuildColumns(tableSpec.columns),
        sa.PrimaryKeyConstraint(*tableSpec.primaryKey, name=op.f(f"pk_{tableSpec.name}")),
        schema=tableSpec.schema,
        comment=tableSpec.comment,
    )


def DropDatasetTable(tableSpec: TableSpec) -> None:
    """Drop one dataset table, with its primary key and comment."""
    op.drop_table(tableSpec.name, schema=tableSpec.schema)


def CreateSchemas() -> None:
    """Create the nine dataset-group schemas in DATASET_SCHEMAS order."""
    for schemaName in DATASET_SCHEMAS:
        op.execute(sa.schema.CreateSchema(schemaName))


def DropSchemas() -> None:
    """Drop the nine schemas in reverse order; each must already be empty."""
    for schemaName in reversed(DATASET_SCHEMAS):
        op.execute(sa.schema.DropSchema(schemaName))


def upgrade() -> None:
    """Create the schemas, then the eleven dataset tables parent-first."""
    CreateSchemas()
    for tableSpec in DATASET_TABLES:
        CreateDatasetTable(tableSpec)


def downgrade() -> None:
    """Drop the eleven dataset tables child-first, then the schemas.

    The downgrades of revisions 0002-0009 have already removed every other object in them.
    """
    for tableSpec in reversed(DATASET_TABLES):
        DropDatasetTable(tableSpec)
    DropSchemas()
