"""PIC 9 identifier digit checks.

Replaces: the PIC 9(n) DISPLAY identifier domains of copybooks CVACT01Y (ACCT-ID 9(11)),
    CVCUS01Y (CUST-ID 9(09), CUST-SSN 9(09)), CVACT02Y (CARD-ACCT-ID 9(11), CARD-CVV-CD 9(03)),
    CVACT03Y (XREF-CUST-ID 9(09), XREF-ACCT-ID 9(11)), CVTRA05Y and CVTRA06Y (TRAN-MERCHANT-ID,
    DALYTRAN-MERCHANT-ID 9(09)), and CVTRA01Y (TRANCAT-ACCT-ID 9(11)).

A COBOL PIC 9(n) DISPLAY field holds exactly n digits. These identifiers are stored as
VARCHAR(n), or CHAR(3) for the CVV, to keep their leading zeros, so each one gets a CHECK
constraint ``<column> ~ '^[0-9]{n}$'`` that restores the digit-only, fixed-width domain. The
condition text and the ck_<table>_<column>_digits names are exactly those that
app.models.base.DigitCheck produces under NAMING_CONVENTION.

The 9(n) codes stored as SMALLINT get no check: the 9(04) category codes, and
CUST-FICO-CREDIT-SCORE 9(03), whose 300-850 range is COACTUPC's online edit only, because seeded
customers below 300 load unchanged.

Every row that revision 0002 seeds satisfies all ten checks, and transactions is still empty, so
each ADD CONSTRAINT validates the existing rows without error. No check is NOT VALID, so the
existing rows and every later INSERT and UPDATE are checked alike.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-02 23:50:00.000000
"""

from collections.abc import Sequence
from typing import NamedTuple

from alembic import op

# Revision identifiers, used by Alembic. Alembic looks these names, and upgrade and downgrade,
# up exactly as spelt, so they keep them instead of the MK_Modernization Rule's naming clause.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class DigitCheckSpec(NamedTuple):
    """One digit check: its convention name, the table it constrains and its SQL condition."""

    name: str
    schema: str
    table: str
    condition: str


# Each condition is a complete static SQL literal, never formatted from variables, with the
# exact text DigitCheck renders; each name is the naming convention's own output. A revision
# is history and keeps creating exactly these checks as the models evolve, so it imports
# nothing from app.models.
DIGIT_CHECKS: tuple[DigitCheckSpec, ...] = (
    # CVACT01Y ACCT-ID 9(11): the ACCTDATA key.
    DigitCheckSpec(
        "ck_accounts_acct_id_digits",
        "accounts",
        "accounts",
        "acct_id ~ '^[0-9]{11}$'",
    ),
    # CVCUS01Y CUST-ID 9(09): the CUSTDATA key.
    DigitCheckSpec(
        "ck_customers_cust_id_digits",
        "customers",
        "customers",
        "cust_id ~ '^[0-9]{9}$'",
    ),
    # CVCUS01Y CUST-SSN 9(09).
    DigitCheckSpec(
        "ck_customers_ssn_digits",
        "customers",
        "customers",
        "ssn ~ '^[0-9]{9}$'",
    ),
    # CVACT02Y CARD-ACCT-ID 9(11): the CARDAIX alternate key.
    DigitCheckSpec(
        "ck_cards_acct_id_digits",
        "cards",
        "cards",
        "acct_id ~ '^[0-9]{11}$'",
    ),
    # CVACT02Y CARD-CVV-CD 9(03).
    DigitCheckSpec(
        "ck_cards_cvv_cd_digits",
        "cards",
        "cards",
        "cvv_cd ~ '^[0-9]{3}$'",
    ),
    # CVACT03Y XREF-CUST-ID 9(09).
    DigitCheckSpec(
        "ck_card_xref_cust_id_digits",
        "xref",
        "card_xref",
        "cust_id ~ '^[0-9]{9}$'",
    ),
    # CVACT03Y XREF-ACCT-ID 9(11): the CXACAIX alternate key.
    DigitCheckSpec(
        "ck_card_xref_acct_id_digits",
        "xref",
        "card_xref",
        "acct_id ~ '^[0-9]{11}$'",
    ),
    # CVTRA05Y TRAN-MERCHANT-ID 9(09).
    DigitCheckSpec(
        "ck_transactions_merchant_id_digits",
        "transactions",
        "transactions",
        "merchant_id ~ '^[0-9]{9}$'",
    ),
    # CVTRA06Y DALYTRAN-MERCHANT-ID 9(09).
    DigitCheckSpec(
        "ck_daily_transactions_merchant_id_digits",
        "transactions",
        "daily_transactions",
        "merchant_id ~ '^[0-9]{9}$'",
    ),
    # CVTRA01Y TRANCAT-ACCT-ID 9(11): the account part of the TCATBALF key. This tenth check is
    # not among the nine identifier fields the PIC mapping names, but it is the same 9(11)
    # account ID and the TransactionCategoryBalance model declares DigitCheck("acct_id", 11).
    # The revision follows the model, so the migrated schema matches Base.metadata.
    DigitCheckSpec(
        "ck_transaction_category_balances_acct_id_digits",
        "transactions",
        "transaction_category_balances",
        "acct_id ~ '^[0-9]{11}$'",
    ),
)


def AddDigitCheck(checkSpec: DigitCheckSpec) -> None:
    """Create one validated digit check; op.f() keeps the convention from renaming it."""
    op.create_check_constraint(
        op.f(checkSpec.name),
        checkSpec.table,
        checkSpec.condition,
        schema=checkSpec.schema,
    )


def DropDigitCheck(checkSpec: DigitCheckSpec) -> None:
    """Drop one digit check created by AddDigitCheck."""
    op.drop_constraint(
        op.f(checkSpec.name),
        checkSpec.table,
        type_="check",
        schema=checkSpec.schema,
    )


def upgrade() -> None:
    """Create the ten PIC 9 digit checks in DIGIT_CHECKS order."""
    for checkSpec in DIGIT_CHECKS:
        AddDigitCheck(checkSpec)


def downgrade() -> None:
    """Drop the ten PIC 9 digit checks in reverse creation order."""
    for checkSpec in reversed(DIGIT_CHECKS):
        DropDigitCheck(checkSpec)
