"""Replaces: IDCAMS DELETE/DEFINE/REPRO of ACCTFILE, CUSTFILE, CARDFILE, XREFFILE, TRANTYPE,
TRANCATG, DISCGRP, TCATBALF, DUSRSECJ and TRANFILE, plus the DALYTRAN staging load; copybooks
CVACT01Y, CVCUS01Y, CVACT02Y, CVACT03Y, CVTRA03Y, CVTRA04Y, CVTRA02Y, CVTRA01Y, CSUSR01Y,
CVTRA06Y, CVTRA05Y.

LoadSampleData is the sample seed that Alembic revision 0002 runs. It replaces the eleven
dataset tables from the legacy seed images, as each load job's DELETE, DEFINE and REPRO
INFILE(<PS>) OUTFILE(<KSDS>) steps rebuilt its cluster, and stages the DALYTRAN daily file that
jcl/POSTTRAN.jcl reads through its DALYTRAN DD:
- DUSRSECJ's plaintext SEC-USR-PWD is stored only as a bcrypt hash in password_hash.
- TRANFILE's single DALYTRAN.PS.INIT priming record is skipped, so transactions starts empty.
- Daily records are numbered record_seq from 1 in file order, the order POSTTRAN reads them.

The module is synchronous and reads no settings and no SECRET_KEY: the APP_ENV gate that keeps
the sample users out of production lives in revision 0002. The batch loaders replace tables
through batch/loaders/replace_loader.py, which shares app.seed.image_reader and the row builders
with this module. The builders are lazy, one row per record in record order, so a loader
streaming an image holds one insert batch at a time while the seed and the loaders build
identical rows; the seed materialises every table's rows before its first DELETE.
"""

import logging
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Table, delete, insert, text
from sqlalchemy.engine import Connection

from app.core.security import HashPassword
from app.models import (
    Account,
    Card,
    CardXref,
    Customer,
    DailyTransaction,
    DisclosureGroup,
    Transaction,
    TransactionCategory,
    TransactionCategoryBalance,
    TransactionType,
    User,
)
from app.seed.image_reader import (
    ImageContents,
    IsTranfilePrimingRecord,
    ReadImage,
    SampleImagePath,
)
from app.utils.record_layouts import (
    ACCOUNT_LAYOUT,
    CARD_LAYOUT,
    CARD_XREF_LAYOUT,
    CUSTOMER_LAYOUT,
    DAILY_TRANSACTION_LAYOUT,
    DISCLOSURE_GROUP_LAYOUT,
    TRAN_CAT_BALANCE_LAYOUT,
    TRANSACTION_CATEGORY_LAYOUT,
    TRANSACTION_LAYOUT,
    TRANSACTION_TYPE_LAYOUT,
    USER_SECURITY_LAYOUT,
    RecordLayout,
)

LOGGER = logging.getLogger(__name__)

# The BCRYPT_ROUNDS default; revision 0002 passes the configured value.
DEFAULT_BCRYPT_ROUNDS = 12

# USER_SECURITY_LAYOUT's key for SEC-USR-PWD, and the security.users column replacing it.
USRSEC_PLAINTEXT_KEY = "password"
USER_HASH_COLUMN = "password_hash"
# The daily file's record position, numbered from 1 in file order.
DAILY_SEQUENCE_COLUMN = "record_seq"
# The padding character of a PIC X field such as SEC-USR-PWD X(08).
FIELD_PADDING = " "

DEFER_CONSTRAINTS_SQL = "SET CONSTRAINTS ALL DEFERRED"
RESTORE_CONSTRAINTS_SQL = "SET CONSTRAINTS ALL IMMEDIATE"

DecodedRecords = Iterable[Mapping[str, object]]
RowBuilder = Callable[[DecodedRecords, int], Iterator[dict[str, object]]]


@dataclass(frozen=True)
class SeedTable:
    """One seeded dataset: its SAMPLE_IMAGE_NAMES key, target table, layout and priming check.

    dataset equals the target table's name, which is also the key of LoadSampleData's counts.
    """

    dataset: str
    table: Table
    layout: RecordLayout
    primingCheck: Callable[[bytes], bool] | None = None


# Foreign-key parent order: rows are inserted in this order and deleted in reverse.
SEED_TABLES: tuple[SeedTable, ...] = (
    SeedTable("accounts", Account.__table__, ACCOUNT_LAYOUT),
    SeedTable("customers", Customer.__table__, CUSTOMER_LAYOUT),
    SeedTable("cards", Card.__table__, CARD_LAYOUT),
    SeedTable("card_xref", CardXref.__table__, CARD_XREF_LAYOUT),
    SeedTable("transaction_types", TransactionType.__table__, TRANSACTION_TYPE_LAYOUT),
    SeedTable("transaction_categories", TransactionCategory.__table__, TRANSACTION_CATEGORY_LAYOUT),
    SeedTable("disclosure_groups", DisclosureGroup.__table__, DISCLOSURE_GROUP_LAYOUT),
    SeedTable(
        "transaction_category_balances",
        TransactionCategoryBalance.__table__,
        TRAN_CAT_BALANCE_LAYOUT,
    ),
    SeedTable("users", User.__table__, USER_SECURITY_LAYOUT),
    SeedTable("daily_transactions", DailyTransaction.__table__, DAILY_TRANSACTION_LAYOUT),
    # TRANFILE REPROs only DALYTRAN.PS.INIT, whose one record primes the empty cluster.
    SeedTable(
        "transactions",
        Transaction.__table__,
        TRANSACTION_LAYOUT,
        primingCheck=IsTranfilePrimingRecord,
    ),
)


def BuildPlainRows(records: DecodedRecords, bcryptRounds: int) -> Iterator[dict[str, object]]:
    """Yield each decoded record unchanged as an insert row; its keys are the column names.

    Keys are deliberately not filtered, so a layout and table that disagree fail the insert
    with SQLAlchemy's unconsumed-column error rather than losing a field. bcryptRounds is
    accepted only so that every RowBuilder shares one signature.
    """
    for record in records:
        yield dict(record)


def BuildUserRows(records: DecodedRecords, bcryptRounds: int) -> Iterator[dict[str, object]]:
    """Yield USRSEC rows with SEC-USR-PWD replaced by its bcrypt hash at cost bcryptRounds.

    The password is hashed exactly as the image holds it, case kept and only its space padding
    removed, as DUSRSECJ and COUSR01C store it. The plaintext never reaches a row or a log.
    """
    for record in records:
        userRow = {key: value for key, value in record.items() if key != USRSEC_PLAINTEXT_KEY}
        plaintext = str(record[USRSEC_PLAINTEXT_KEY]).rstrip(FIELD_PADDING)
        userRow[USER_HASH_COLUMN] = HashPassword(plaintext, bcryptRounds)
        yield userRow


def BuildDailyTransactionRows(
    records: DecodedRecords, bcryptRounds: int
) -> Iterator[dict[str, object]]:
    """Yield DALYTRAN rows numbered record_seq from 1 in file order, as CBTRN02C reads them.

    The numbering runs over the whole of records, however a caller batches the rows. A repeated
    tran_id is kept: a sequential file may repeat one, and its index is not unique.
    """
    for recordSeq, record in enumerate(records, start=1):
        yield {DAILY_SEQUENCE_COLUMN: recordSeq, **record}


# Datasets whose rows need more than the decoded record; all others use BuildPlainRows.
ROW_BUILDERS: dict[str, RowBuilder] = {
    "users": BuildUserRows,
    "daily_transactions": BuildDailyTransactionRows,
}


def ReadSeedRecords(spec: SeedTable, sourceDir: str | Path, encoding: str) -> ImageContents:
    """Read and decode the seed image of spec's dataset from sourceDir.

    The ASCII copy is read when encoding asks for it and one exists; USRSEC and DALYTRAN.PS.INIT
    exist only in EBCDIC and are read as EBCDIC from the same sourceDir.
    """
    path, effectiveEncoding = SampleImagePath(sourceDir, spec.dataset, encoding)
    return ReadImage(path, spec.layout, effectiveEncoding, spec.primingCheck)


def ReadAllImages(sourceDir: str | Path, encoding: str) -> dict[str, ImageContents]:
    """Read every SEED_TABLES image, keyed by dataset, before any table is touched.

    ImageFormatError (a missing or malformed image) and ValueError (an unsupported encoding)
    propagate unchanged.
    """
    return {spec.dataset: ReadSeedRecords(spec, sourceDir, encoding) for spec in SEED_TABLES}


def BuildAllRows(
    images: Mapping[str, ImageContents], bcryptRounds: int
) -> dict[str, list[dict[str, object]]]:
    """Return the insert rows of every SEED_TABLES dataset, built in full, keyed by dataset."""
    return {
        spec.dataset: list(
            ROW_BUILDERS.get(spec.dataset, BuildPlainRows)(
                images[spec.dataset].records, bcryptRounds
            )
        )
        for spec in SEED_TABLES
    }


def DeleteAll(connection: Connection) -> None:
    """Defer the deferrable constraints, then empty every SEED_TABLES table, children first.

    Revision 0002 runs before revision 0004 adds the foreign keys; when they exist they are
    DEFERRABLE INITIALLY IMMEDIATE, so the deferral lets parents be replaced in any order.
    """
    connection.execute(text(DEFER_CONSTRAINTS_SQL))
    for spec in reversed(SEED_TABLES):
        connection.execute(delete(spec.table))


def InsertRows(connection: Connection, spec: SeedTable, rows: list[dict[str, object]]) -> int:
    """Insert rows into spec's table with bound parameters and return how many were inserted.

    No statement runs for an empty list: SQLAlchemy would execute one parameterless INSERT.
    """
    if not rows:
        return 0
    connection.execute(insert(spec.table), rows)
    return len(rows)


def RestoreImmediateConstraints(connection: Connection) -> None:
    """Return the deferrable constraints to IMMEDIATE, checking the deferred ones now.

    Without this, later statements and revisions in the caller's transaction would stay
    deferred, and pending checks would block a later ALTER TABLE on a seeded table.
    """
    connection.execute(text(RESTORE_CONSTRAINTS_SQL))


def LogSeedCount(dataset: str, rowCount: int, skippedCount: int) -> None:
    """Log one table's inserted row count and any skipped priming records; never row content."""
    if skippedCount:
        LOGGER.info("seed %s: %d rows, %d priming record skipped", dataset, rowCount, skippedCount)
    else:
        LOGGER.info("seed %s: %d rows", dataset, rowCount)


def LoadSampleData(
    connection: Connection,
    sourceDir: str | Path,
    encoding: str,
    bcryptRounds: int = DEFAULT_BCRYPT_ROUNDS,
) -> dict[str, int]:
    """Replace the eleven dataset tables with the sample seed images in sourceDir.

    encoding is image_reader.ENCODING_EBCDIC, the images the JCL loads, or ENCODING_ASCII;
    sourceDir resolves under the repository root when relative. Every image is read, decoded
    and turned into rows before the first DELETE, so ImageFormatError or ValueError leaves the
    database untouched. Each table is then emptied and reloaded, as IDCAMS DELETE, DEFINE and
    REPRO rebuilt its cluster.

    The caller owns the transaction: nothing here begins, commits or rolls back, so the replace
    is atomic with it, and a duplicate key or other database error propagates and fails it.
    Returns {table name: rows inserted} in load order. Revision 0002 decides whether to call
    this at all from APP_ENV; this module reads no settings.
    """
    images = ReadAllImages(sourceDir, encoding)
    rowsByDataset = BuildAllRows(images, bcryptRounds)
    DeleteAll(connection)
    rowCounts: dict[str, int] = {}
    for spec in SEED_TABLES:
        rowCounts[spec.dataset] = InsertRows(connection, spec, rowsByDataset[spec.dataset])
        LogSeedCount(spec.dataset, rowCounts[spec.dataset], images[spec.dataset].skippedCount)
    RestoreImmediateConstraints(connection)
    return rowCounts
