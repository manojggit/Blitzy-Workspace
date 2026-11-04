"""Replaces: TRANIDX (jcl/TRANIDX.jcl: STEP20 IDCAMS DEFINE ALTERNATEINDEX, STEP25 DEFINE PATH,
STEP30 BLDINDEX) and its AIX AWS.M2.CARDDEMO.TRANSACT.VSAM.AIX, KEYS(26 304) NONUNIQUEKEY
UPGRADE on copybook CVTRA05Y TRAN-PROC-TS (offset 304, length 26).

Chain step 18, the last step of a cycle. The AIX is the non-unique index ix_transactions_proc_ts
on transactions.transactions (proc_ts), which Alembic revision 0003 creates and
app.models.transaction declares. In one transaction the job ensures the index exists (DEFINE
AIX and DEFINE PATH: PostgreSQL relates an index to its table by itself), rebuilds it
(BLDINDEX), confirms in the catalog that it is valid and has that definition, and finishes its
run ledger row as the last statement, so it is an atomic step. A same-named index of another
definition (another table, column, order, operator class or access method, a unique, partial or
expression index, or more than one column) is dropped and created again, with a warning, inside
the same transaction. It writes no records and gives the same state on every run.

Exit statuses: EXIT_OK, or JobFailure with EXIT_FAILURE when a database statement fails or the
index is not valid or not the proc_ts index, after which the transaction, the drop of a
misdefined index included, has rolled back. docs/batch.md ("Step 18: TRANIDX") transcribes
this contract.

The DDL is constant text over fixed identifiers, never input, and the state query binds the
names as parameters. CREATE INDEX CONCURRENTLY and REINDEX CONCURRENTLY are not used, because
neither can run inside a transaction block. The CLI holds the batch window around the command,
so this job never enters the online window.
"""

import logging

from app.models import TRANSACTIONS_SCHEMA
from sqlalchemy import text
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import DBAPIError

from batch.orchestration import run_ledger
from batch.orchestration.job_context import (
    EXIT_FAILURE,
    EXIT_OK,
    JobContext,
    JobFailure,
    JobResult,
)

LOGGER = logging.getLogger(__name__)

JOB_NAME = "tran-index"
JCL_NAME = "TRANIDX"

# The index name of Alembic revision 0003 and of the model's ix_<table>_<column> convention.
PROC_TS_INDEX_NAME = "ix_transactions_proc_ts"

# The definition the AIX KEYS(26 304) NONUNIQUEKEY maps to: one ascending key column, proc_ts,
# of the indexed table, in a non-unique B-tree with the column type's default operator class.
INDEXED_TABLE_NAME = "transactions"
INDEXED_COLUMN_NAME = "proc_ts"
INDEX_ACCESS_METHOD = "btree"

# STEP20 DEFINE ALTERNATEINDEX and STEP25 DEFINE PATH.
CREATE_INDEX_SQL = text(
    "CREATE INDEX IF NOT EXISTS ix_transactions_proc_ts ON transactions.transactions (proc_ts)"
)
# Removes a same-named index of another definition, which CREATE INDEX IF NOT EXISTS would keep.
DROP_INDEX_SQL = text("DROP INDEX transactions.ix_transactions_proc_ts")
# STEP30 BLDINDEX.
REINDEX_SQL = text("REINDEX INDEX transactions.ix_transactions_proc_ts")
# pg_index validity and definition of the named index; no row when no index has the name. An
# index lives in its table's schema, so the schema filter also fixes the table's schema. The
# catalog columns are compared, not pg_get_indexdef text, whose table qualification depends on
# search_path. The outer joins keep a row, defined FALSE, for an expression index (indkey 0).
INDEX_STATE_SQL = text(
    "SELECT i.indisvalid AS is_valid,"
    " COALESCE(t.relname = :tableName AND am.amname = :accessMethod AND NOT i.indisunique"
    " AND i.indnatts = 1 AND i.indexprs IS NULL AND i.indpred IS NULL"
    " AND a.attname = :columnName AND i.indoption[0] = 0 AND opc.opcdefault, FALSE)"
    " AS has_proc_ts_definition"
    " FROM pg_index i"
    " JOIN pg_class c ON c.oid = i.indexrelid"
    " JOIN pg_namespace n ON n.oid = c.relnamespace"
    " JOIN pg_class t ON t.oid = i.indrelid"
    " JOIN pg_am am ON am.oid = c.relam"
    " LEFT JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]"
    " LEFT JOIN pg_opclass opc ON opc.oid = i.indclass[0]"
    " WHERE n.nspname = :schemaName AND c.relname = :indexName"
)

INDEX_NOT_VALID_MESSAGE = f"INDEX {PROC_TS_INDEX_NAME} IS NOT VALID"
INDEX_NOT_REBUILT_TEMPLATE = "INDEX {indexName} NOT REBUILT: {reason}"
INDEX_VERIFIED_LOG = "INDEX %s.%s REBUILT AND VALID"
INDEX_REPLACED_LOG = "INDEX %s.%s HAS ANOTHER DEFINITION: DROPPED TO BE RECREATED ON %s.%s (%s)"

# JobResult.counts is the integer counts JSON of batch.job_runs; TRANIDX writes no records, so
# it counts the one index it verified.
INDEXES_VERIFIED_COUNT_KEY = "indexes_verified"


def ReadProcTsIndexState(connection: Connection) -> RowMapping | None:
    """Return is_valid and has_proc_ts_definition of the PROC-TS index, or None when absent."""
    parameters = {
        "schemaName": TRANSACTIONS_SCHEMA,
        "indexName": PROC_TS_INDEX_NAME,
        "tableName": INDEXED_TABLE_NAME,
        "columnName": INDEXED_COLUMN_NAME,
        "accessMethod": INDEX_ACCESS_METHOD,
    }
    return connection.execute(INDEX_STATE_SQL, parameters).mappings().one_or_none()


def EnsureProcTsIndex(connection: Connection) -> None:
    """Create the PROC-TS index when it is missing (STEP20 DEFINE AIX, STEP25 DEFINE PATH).

    A same-named index of another definition is dropped first, so the create replaces it. A
    relation of that name that is not an index is left for REINDEX to refuse.
    """
    indexState = ReadProcTsIndexState(connection)
    if indexState is not None and not indexState["has_proc_ts_definition"]:
        LOGGER.warning(
            INDEX_REPLACED_LOG,
            TRANSACTIONS_SCHEMA,
            PROC_TS_INDEX_NAME,
            TRANSACTIONS_SCHEMA,
            INDEXED_TABLE_NAME,
            INDEXED_COLUMN_NAME,
        )
        connection.execute(DROP_INDEX_SQL)
    connection.execute(CREATE_INDEX_SQL)


def RebuildProcTsIndex(connection: Connection) -> None:
    """Rebuild the PROC-TS index from the table's rows (STEP30 BLDINDEX)."""
    connection.execute(REINDEX_SQL)


def IsProcTsIndexValid(connection: Connection) -> bool:
    """Return whether the PROC-TS index exists, pg_index marks it valid and it has the definition.

    The definition is the non-unique ascending B-tree on transactions.transactions (proc_ts).
    """
    indexState = ReadProcTsIndexState(connection)
    if indexState is None:
        return False
    return indexState["is_valid"] is True and indexState["has_proc_ts_definition"] is True


def RebuildAndVerifyProcTsIndex(connection: Connection) -> None:
    """Run STEP20, STEP25 and STEP30 on the connection, then refuse an index that is not valid.

    An index that is not the proc_ts index after the rebuild is refused alike.
    """
    EnsureProcTsIndex(connection)
    RebuildProcTsIndex(connection)
    if not IsProcTsIndexValid(connection):
        raise JobFailure(EXIT_FAILURE, INDEX_NOT_VALID_MESSAGE)


def DescribeDatabaseError(error: DBAPIError) -> str:
    """Return the failure text: the driver error's first line, without the statement echo."""
    errorLines = str(error).splitlines()
    reason = errorLines[0] if errorLines else type(error).__name__
    return INDEX_NOT_REBUILT_TEMPLATE.format(indexName=PROC_TS_INDEX_NAME, reason=reason)


def RunJob(context: JobContext) -> JobResult:
    """Ensure, rebuild and verify ix_transactions_proc_ts in one transaction (chain step 18).

    Raises JobFailure with EXIT_FAILURE on a database error or an index that is not valid or not
    the proc_ts index; the transaction, the ledger completion included, then rolls back and the
    index keeps its previous state, absent or of another definition included.
    """
    result = JobResult(EXIT_OK, {INDEXES_VERIFIED_COUNT_KEY: 1})
    try:
        with context.engine.begin() as connection:
            RebuildAndVerifyProcTsIndex(connection)
            run_ledger.FinishAtomicRun(connection, context, result)
    except DBAPIError as error:
        raise JobFailure(EXIT_FAILURE, DescribeDatabaseError(error)) from error
    LOGGER.info(INDEX_VERIFIED_LOG, TRANSACTIONS_SCHEMA, PROC_TS_INDEX_NAME)
    return result
