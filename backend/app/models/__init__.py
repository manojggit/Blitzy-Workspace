"""Replaces: the VSAM dataset groups of the CardDemo base application (model package barrel).

Importing this package registers every table on ``Base.metadata`` and does nothing else: it reads
no settings and imports nothing from ``app.core``, so batch and Alembic import it without a
``SECRET_KEY``.
"""

# Imports stay in ruff's isort order (I001) rather than parent tables first: string foreign keys
# such as "accounts.accounts.acct_id" resolve lazily, so module order never affects them.
from app.models.account import Account
from app.models.base import (
    ACCOUNTS_SCHEMA,
    ALL_SCHEMAS,
    BATCH_SCHEMA,
    CARDS_SCHEMA,
    CUSTOMERS_SCHEMA,
    DISCLOSURE_SCHEMA,
    FOREIGN_KEY_OPTIONS,
    KEY_COLLATION,
    NAMING_CONVENTION,
    REFERENCE_SCHEMA,
    SECURITY_SCHEMA,
    TRANSACTIONS_SCHEMA,
    XREF_SCHEMA,
    Base,
)
from app.models.card import Card
from app.models.card_xref import CardXref
from app.models.customer import Customer
from app.models.daily_transaction import DailyTransaction
from app.models.disclosure_group import DisclosureGroup
from app.models.support_tables import (
    CYCLE_OPENERS,
    CYCLE_STATUSES,
    CYCLE_STEPS,
    CYCLES,
    GDG_GENERATIONS,
    GENERATION_STATES,
    JOB_RUNS,
    LOGIN_ATTEMPTS,
    RUN_STATUSES,
    STEP_STATES,
)
from app.models.transaction import Transaction
from app.models.transaction_category import TransactionCategory
from app.models.transaction_category_balance import TransactionCategoryBalance
from app.models.transaction_type import TransactionType
from app.models.user import User

__all__ = [
    "ACCOUNTS_SCHEMA",
    "ALL_SCHEMAS",
    "BATCH_SCHEMA",
    "CARDS_SCHEMA",
    "CUSTOMERS_SCHEMA",
    "CYCLES",
    "CYCLE_OPENERS",
    "CYCLE_STATUSES",
    "CYCLE_STEPS",
    "DISCLOSURE_SCHEMA",
    "FOREIGN_KEY_OPTIONS",
    "GDG_GENERATIONS",
    "GENERATION_STATES",
    "JOB_RUNS",
    "KEY_COLLATION",
    "LOGIN_ATTEMPTS",
    "NAMING_CONVENTION",
    "REFERENCE_SCHEMA",
    "RUN_STATUSES",
    "SECURITY_SCHEMA",
    "STEP_STATES",
    "TRANSACTIONS_SCHEMA",
    "XREF_SCHEMA",
    "Account",
    "Base",
    "Card",
    "CardXref",
    "Customer",
    "DailyTransaction",
    "DisclosureGroup",
    "Transaction",
    "TransactionCategory",
    "TransactionCategoryBalance",
    "TransactionType",
    "User",
]
