"""Relationship foreign keys between the dataset tables.

Replaces: record relationships of copybooks CVACT02Y (CARD-ACCT-ID), CVACT03Y (XREF-CARD-NUM,
    XREF-CUST-ID, XREF-ACCT-ID; CCXREF/CXACAIX lookups), CVTRA05Y (TRAN-CARD-NUM), CVTRA01Y
    (TRANCAT-ACCT-ID), CVTRA02Y (DIS-TRAN-TYPE-CD, DIS-TRAN-CAT-CD), CVTRA04Y (TRAN-TYPE-CD);
    VSAM clusters AWS.M2.CARDDEMO.{CARDDATA,CARDXREF,TRANSACT,TCATBALF,DISCGRP,TRANCATG}.VSAM.KSDS
    and their parents ACCTDATA, CUSTDATA and TRANTYPE.

VSAM enforced none of these relationships: a program found the account, card or customer a
record names by a keyed READ of another cluster, and handled a missing one in its own code.
Each relationship becomes a foreign key named by the fk_<table>_<column_0>_<referred table>
convention of app.models.base. Every key is DEFERRABLE INITIALLY IMMEDIATE, as the models
declare: a violation fails its statement, and a loader that replaces a parent table inside one
transaction defers the check to commit with SET CONSTRAINTS ALL DEFERRED. No key cascades or
sets null on delete or update, because the models declare neither and no ported program
deletes or rekeys a parent record.

Deliberately not foreign keys, because a legacy program accepts or creates the unmatched value:

- accounts.group_id to disclosure.disclosure_groups: group_id is not unique there, and CBACT04C
  retries an unmatched group with DEFAULT.
- transactions.tran_type_cd and tran_cat_cd: COTRN02C accepts any numeric codes, and CBTRN02C
  checks none.
- daily_transactions.card_num: an unknown card must reach POSTTRAN reject 100 (INVALID CARD
  NUMBER FOUND).
- transaction_category_balances type and category codes: CBTRN02C creates a balance for any
  code.

The rows that revision 0002 seeds satisfy all eight keys and transactions is still empty, so
each ADD CONSTRAINT validates the existing rows without error.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-02 23:40:00.000000
"""

from collections.abc import Sequence
from typing import NamedTuple

from alembic import op

# Revision identifiers, used by Alembic. Alembic looks these names, and upgrade and downgrade,
# up exactly as spelt, so they keep them instead of the MK_Modernization Rule's naming clause.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of app.models.base.FOREIGN_KEY_OPTIONS: a revision is history and keeps creating
# exactly these keys as the models evolve, so it imports nothing from app.models.
FOREIGN_KEY_OPTIONS: dict[str, object] = {"deferrable": True, "initially": "IMMEDIATE"}


class ForeignKeySpec(NamedTuple):
    """One foreign key: its convention name, the referencing columns and the referenced key."""

    name: str
    sourceSchema: str
    sourceTable: str
    localColumns: tuple[str, ...]
    referentSchema: str
    referentTable: str
    remoteColumns: tuple[str, ...]


# Each referenced key is its table's primary key. Names are the convention's own output.
RELATIONSHIP_FOREIGN_KEYS: tuple[ForeignKeySpec, ...] = (
    # CVTRA04Y TRAN-TYPE-CD: a TRANCATG category belongs to a TRANTYPE type.
    ForeignKeySpec(
        "fk_transaction_categories_tran_type_cd_transaction_types",
        "reference",
        "transaction_categories",
        ("tran_type_cd",),
        "reference",
        "transaction_types",
        ("tran_type",),
    ),
    # CVACT02Y CARD-ACCT-ID: the account a CARDDAT record belongs to.
    ForeignKeySpec(
        "fk_cards_acct_id_accounts",
        "cards",
        "cards",
        ("acct_id",),
        "accounts",
        "accounts",
        ("acct_id",),
    ),
    # CVACT03Y XREF-CARD-NUM: the CCXREF record of a card.
    ForeignKeySpec(
        "fk_card_xref_card_num_cards",
        "xref",
        "card_xref",
        ("card_num",),
        "cards",
        "cards",
        ("card_num",),
    ),
    # CVACT03Y XREF-CUST-ID: the only link from a card to its customer.
    ForeignKeySpec(
        "fk_card_xref_cust_id_customers",
        "xref",
        "card_xref",
        ("cust_id",),
        "customers",
        "customers",
        ("cust_id",),
    ),
    # CVACT03Y XREF-ACCT-ID: the CXACAIX path from an account to its cards.
    ForeignKeySpec(
        "fk_card_xref_acct_id_accounts",
        "xref",
        "card_xref",
        ("acct_id",),
        "accounts",
        "accounts",
        ("acct_id",),
    ),
    # CVTRA05Y TRAN-CARD-NUM: the card a posted TRANSACT record was made with.
    ForeignKeySpec(
        "fk_transactions_card_num_cards",
        "transactions",
        "transactions",
        ("card_num",),
        "cards",
        "cards",
        ("card_num",),
    ),
    # CVTRA01Y TRANCAT-ACCT-ID: the account a TCATBALF balance belongs to.
    ForeignKeySpec(
        "fk_transaction_category_balances_acct_id_accounts",
        "transactions",
        "transaction_category_balances",
        ("acct_id",),
        "accounts",
        "accounts",
        ("acct_id",),
    ),
    # CVTRA02Y DIS-TRAN-TYPE-CD and DIS-TRAN-CAT-CD: the TRANCATG category a rate applies to.
    ForeignKeySpec(
        "fk_disclosure_groups_tran_type_cd_transaction_categories",
        "disclosure",
        "disclosure_groups",
        ("tran_type_cd", "tran_cat_cd"),
        "reference",
        "transaction_categories",
        ("tran_type_cd", "tran_cat_cd"),
    ),
)


def AddForeignKey(foreignKeySpec: ForeignKeySpec) -> None:
    """Create one deferrable foreign key; op.f() keeps the convention from renaming it."""
    op.create_foreign_key(
        op.f(foreignKeySpec.name),
        foreignKeySpec.sourceTable,
        foreignKeySpec.referentTable,
        list(foreignKeySpec.localColumns),
        list(foreignKeySpec.remoteColumns),
        source_schema=foreignKeySpec.sourceSchema,
        referent_schema=foreignKeySpec.referentSchema,
        **FOREIGN_KEY_OPTIONS,
    )


def DropForeignKey(foreignKeySpec: ForeignKeySpec) -> None:
    op.drop_constraint(
        op.f(foreignKeySpec.name),
        foreignKeySpec.sourceTable,
        type_="foreignkey",
        schema=foreignKeySpec.sourceSchema,
    )


def upgrade() -> None:
    """Create the eight relationship foreign keys in RELATIONSHIP_FOREIGN_KEYS order."""
    for foreignKeySpec in RELATIONSHIP_FOREIGN_KEYS:
        AddForeignKey(foreignKeySpec)


def downgrade() -> None:
    """Drop the eight relationship foreign keys in reverse creation order."""
    for foreignKeySpec in reversed(RELATIONSHIP_FOREIGN_KEYS):
        DropForeignKey(foreignKeySpec)
