"""Alternate-index replacements and EBCDIC key ordering.

Replaces: AIX AWS.M2.CARDDEMO.CARDDATA.VSAM.AIX KEYS(11 16), CARDXREF.VSAM.AIX KEYS(11,25),
    TRANSACT.VSAM.AIX KEYS(26 304) with their PATHs and BLDINDEX steps (jobs CARDFILE,
    XREFFILE, TRANFILE, TRANIDX); the EBCDIC (cp037) key collating sequence of USRSEC, CARDDAT
    and TRANSACT.

Each NONUNIQUEKEY alternate index becomes a non-unique B-tree index on the same field, and a
PATH plus BLDINDEX needs no counterpart because PostgreSQL maintains an index on every write.
``ix_transactions_proc_ts`` is the name batch/jobs/tranidx.py re-creates and re-indexes.

VSAM orders keys by their cp037 bytes, so lower case sorts before upper case and both before
digits. ``public.ebcdic_sort_key`` maps every cp037 printable character (bytes X'40'-X'FE') to
the code point equal to its byte; under ``COLLATE "C"`` UTF-8 byte order is code-point order,
so ordering by the stored, indexed ``*_sort`` columns is EBCDIC key order for any key a 3270
field can carry. The digit-checked keys acct_id and cust_id sort alike in both code pages and
get no sort column. The generated expressions call the function unqualified, as the models do,
which relies on ``public`` being on the default search_path. Seeded users and cards receive
their sort values when the columns are added.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-02 23:30:00.000000
"""

from collections.abc import Callable, Sequence
from typing import NamedTuple

import sqlalchemy as sa

from alembic import op

# Revision identifiers, used by Alembic. Alembic looks these names, and upgrade and downgrade,
# up exactly as spelt, so they keep them instead of the MK_Modernization Rule's naming clause.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The cp037 printable bytes X'40'-X'FE': 191 characters, X'40' being the space.
EBCDIC_PRINTABLE_BYTES = range(0x40, 0xFF)
EBCDIC_CODE_PAGE = "cp037"

# Sort and key columns compare in byte order, never locale order (app.models.base).
KEY_COLLATION = "C"

# Escape character of the U&'' constants that offline mode renders the translation strings as.
UNICODE_ESCAPE = "!"
PRINTABLE_ASCII_FIRST = " "
PRINTABLE_ASCII_LAST = "~"

# The translation strings hold ', % and \, so they are bound, never written into the SQL text.
# psycopg2 interpolates binds client-side, which a CREATE FUNCTION statement requires.
CREATE_SORT_KEY_FUNCTION = """\
CREATE FUNCTION public.ebcdic_sort_key(value text)
    RETURNS text
    LANGUAGE sql
    IMMUTABLE STRICT PARALLEL SAFE
    RETURN translate(value, :fromChars, :toChars)"""
DROP_SORT_KEY_FUNCTION = "DROP FUNCTION public.ebcdic_sort_key(text)"


class SortColumnSpec(NamedTuple):
    """One generated EBCDIC sort column and the exact expression text its model declares."""

    schema: str
    table: str
    sortColumn: str
    sortExpression: str


class AixIndexSpec(NamedTuple):
    """One non-unique index: its naming-convention name, schema, table and key columns."""

    indexName: str
    schema: str
    table: str
    columns: tuple[str, ...]


# The keys that VSAM browses in cp037 byte order: USRSEC, CARDDAT and TRANSACT.
SORT_COLUMNS: tuple[SortColumnSpec, ...] = (
    SortColumnSpec("security", "users", "user_id_sort", "ebcdic_sort_key(user_id)"),
    SortColumnSpec("cards", "cards", "card_num_sort", "ebcdic_sort_key(card_num)"),
    SortColumnSpec("transactions", "transactions", "tran_id_sort", "ebcdic_sort_key(tran_id)"),
)

# Names follow the ix_<table>_<column_0> naming convention of app.models.base.
AIX_INDEXES: tuple[AixIndexSpec, ...] = (
    AixIndexSpec("ix_users_user_id_sort", "security", "users", ("user_id_sort",)),
    AixIndexSpec("ix_cards_card_num_sort", "cards", "cards", ("card_num_sort",)),
    AixIndexSpec("ix_transactions_tran_id_sort", "transactions", "transactions", ("tran_id_sort",)),
    # CARDAIX: CARD-ACCT-ID, KEYS(11 16).
    AixIndexSpec("ix_cards_acct_id", "cards", "cards", ("acct_id",)),
    # CXACAIX: XREF-ACCT-ID, KEYS(11,25).
    AixIndexSpec("ix_card_xref_acct_id", "xref", "card_xref", ("acct_id",)),
    AixIndexSpec("ix_card_xref_cust_id", "xref", "card_xref", ("cust_id",)),
    AixIndexSpec("ix_transactions_card_num", "transactions", "transactions", ("card_num",)),
    # TRANSACT AIX: TRAN-PROC-TS, KEYS(26 304).
    AixIndexSpec("ix_transactions_proc_ts", "transactions", "transactions", ("proc_ts",)),
    # Not unique: the sequential daily file may repeat a transaction ID.
    AixIndexSpec(
        "ix_daily_transactions_tran_id", "transactions", "daily_transactions", ("tran_id",)
    ),
)


def EscapeLiteralCharacter(character: str) -> str:
    """Return one character as it is written inside a U&'...' UESCAPE '!' constant.

    Only printable ASCII other than the backslash stays as typed, so the rendered constant is
    pure ASCII and holds no backslash for the offline dialect to double.
    """
    if character == "'":
        return "''"
    if character == UNICODE_ESCAPE:
        return UNICODE_ESCAPE + UNICODE_ESCAPE
    if character == "\\" or not PRINTABLE_ASCII_FIRST <= character <= PRINTABLE_ASCII_LAST:
        return f"{UNICODE_ESCAPE}{ord(character):04X}"
    return character


def RenderUnicodeEscapedLiteral(value: str) -> str:
    """Return ``value`` as a PostgreSQL U&'...' UESCAPE '!' string constant."""
    escapedValue = "".join(EscapeLiteralCharacter(character) for character in value)
    return f"U&'{escapedValue}' UESCAPE '{UNICODE_ESCAPE}'"


class UnicodeEscapedText(sa.types.TypeDecorator):
    """Text bind type whose offline literal is a Unicode-escape constant.

    ``alembic upgrade --sql`` renders binds as literals through a dialect that never connected,
    so SQLAlchemy assumes standard_conforming_strings is off and doubles every backslash, which
    a PostgreSQL 17 server (on by default) reads as two characters and translate() would then
    map every later character one place off. Online, the value is bound unchanged.
    """

    # SQLAlchemy looks impl, cache_ok and literal_processor up by name: a framework-mandated
    # exception to the MK_Modernization Rule's naming clause.
    impl = sa.Text
    cache_ok = True

    def literal_processor(self, dialect: sa.engine.Dialect) -> Callable[[str], str]:
        """Return the renderer SQLAlchemy applies to this type's literal binds."""
        return RenderUnicodeEscapedLiteral


def BuildSortKeyTranslation() -> tuple[str, str]:
    """Return translate()'s from and to strings: each cp037 printable character, in byte order,
    and the code point equal to that character's cp037 byte (space to U+0040, a to U+0081).
    """
    fromChars = bytes(EBCDIC_PRINTABLE_BYTES).decode(EBCDIC_CODE_PAGE)
    toChars = "".join(chr(byteValue) for byteValue in EBCDIC_PRINTABLE_BYTES)
    return fromChars, toChars


def CreateSortKeyFunction() -> None:
    """Create public.ebcdic_sort_key with the translation strings as bound parameters."""
    fromChars, toChars = BuildSortKeyTranslation()
    op.execute(
        sa.text(CREATE_SORT_KEY_FUNCTION).bindparams(
            sa.bindparam("fromChars", fromChars, type_=UnicodeEscapedText()),
            sa.bindparam("toChars", toChars, type_=UnicodeEscapedText()),
        )
    )


def AddSortColumn(sortSpec: SortColumnSpec) -> None:
    """Add one stored generated sort column, NOT NULL and collated "C" like its model."""
    op.add_column(
        sortSpec.table,
        sa.Column(
            sortSpec.sortColumn,
            sa.Text(collation=KEY_COLLATION),
            sa.Computed(sortSpec.sortExpression, persisted=True),
            nullable=False,
        ),
        schema=sortSpec.schema,
    )


def DropSortColumn(sortSpec: SortColumnSpec) -> None:
    """Drop one generated sort column."""
    op.drop_column(sortSpec.table, sortSpec.sortColumn, schema=sortSpec.schema)


def CreateAixIndex(indexSpec: AixIndexSpec) -> None:
    """Create one non-unique index; op.f() keeps the convention from prefixing the name again."""
    op.create_index(
        op.f(indexSpec.indexName),
        indexSpec.table,
        list(indexSpec.columns),
        unique=False,
        schema=indexSpec.schema,
    )


def DropAixIndex(indexSpec: AixIndexSpec) -> None:
    """Drop one index created by CreateAixIndex."""
    op.drop_index(op.f(indexSpec.indexName), table_name=indexSpec.table, schema=indexSpec.schema)


def upgrade() -> None:
    """Create the sort-key function, then the three sort columns, then the nine indexes."""
    CreateSortKeyFunction()
    for sortSpec in SORT_COLUMNS:
        AddSortColumn(sortSpec)
    for indexSpec in AIX_INDEXES:
        CreateAixIndex(indexSpec)


def downgrade() -> None:
    """Drop the nine indexes, then the sort columns, then the function their expressions use."""
    for indexSpec in reversed(AIX_INDEXES):
        DropAixIndex(indexSpec)
    for sortSpec in reversed(SORT_COLUMNS):
        DropSortColumn(sortSpec)
    op.execute(DROP_SORT_KEY_FUNCTION)
