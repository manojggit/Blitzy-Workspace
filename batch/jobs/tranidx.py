"""Replaces: TRANIDX (jcl/TRANIDX.jcl: STEP20 IDCAMS DEFINE ALTERNATEINDEX, STEP25 DEFINE PATH,
STEP30 BLDINDEX) and its AIX AWS.M2.CARDDEMO.TRANSACT.VSAM.AIX, KEYS(26 304) NONUNIQUEKEY
UPGRADE on copybook CVTRA05Y TRAN-PROC-TS (offset 304, length 26).

Chain step 18, the last step of a cycle. The AIX is the non-unique index ix_transactions_proc_ts
on transactions.transactions (proc_ts), which Alembic revision 0003 creates and
app.models.transaction declares. In one transaction the job ensures the index exists (DEFINE
AIX and DEFINE PATH: PostgreSQL relates an index to its table by itself), rebuilds it
(BLDINDEX), confirms in pg_index that it is valid, and finishes its run ledger row as the last
statement, so it is an atomic step. It writes no records and gives the same state on every run.

Exit statuses: EXIT_OK, or JobFailure with EXIT_FAILURE when a database statement fails or the
index is not valid, after which the transaction has rolled back. docs/batch.md ("Step 18:
TRANIDX") transcribes this contract.

The DDL is constant text over fixed identifiers, never input, and the validity query binds the
names as parameters. CREATE INDEX CONCURRENTLY and REINDEX CONCURRENTLY are not used, because
neither can run inside a transaction block. The CLI holds the batch window around the command,
so this job never enters the online window.
"""

import logging

from app.models import TRANSACTIONS_SCHEMA
from sqlalchemy import text
from sqlalchemy.engine import Connection
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

# The canonical CLI name and the legacy JCL name of batch_chain.JOB_DEFINITIONS["tran-index"].
JOB_NAME = "tran-index"
JCL_NAME = "TRANIDX"

# The index name of Alembic revision 0003 and of the model's ix_<table>_<column> convention.
PROC_TS_INDEX_NAME = "ix_transactions_proc_ts"

# STEP20 DEFINE ALTERNATEINDEX and STEP25 DEFINE PATH.
CREATE_INDEX_SQL = text(
    "CREATE INDEX IF NOT EXISTS ix_transactions_proc_ts ON transactions.transactions (proc_ts)"
)
# STEP30 BLDINDEX.
REINDEX_SQL = text("REINDEX INDEX transactions.ix_transactions_proc_ts")
INDEX_VALIDITY_SQL = text(
    "SELECT i.indisvalid"
    " FROM pg_index i"
    " JOIN pg_class c ON c.oid = i.indexrelid"
    " JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = :schemaName AND c.relname = :indexName"
)

INDEX_NOT_VALID_MESSAGE = f"INDEX {PROC_TS_INDEX_NAME} IS NOT VALID"
INDEX_NOT_REBUILT_TEMPLATE = "INDEX {indexName} NOT REBUILT: {reason}"
INDEX_VERIFIED_LOG = "INDEX %s.%s REBUILT AND VALID"

# JobResult.counts is the integer counts JSON of batch.job_runs; TRANIDX writes no records, so
# it counts the one index it verified.
INDEXES_VERIFIED_COUNT_KEY = "indexes_verified"


def EnsureProcTsIndex(connection: Connection) -> None:
    """Create the PROC-TS index when it is missing (STEP20 DEFINE AIX, STEP25 DEFINE PATH)."""
    connection.execute(CREATE_INDEX_SQL)


def RebuildProcTsIndex(connection: Connection) -> None:
    """Rebuild the PROC-TS index from the table's rows (STEP30 BLDINDEX)."""
    connection.execute(REINDEX_SQL)


def IsProcTsIndexValid(connection: Connection) -> bool:
    """Return whether pg_index holds the PROC-TS index and marks it valid."""
    parameters = {"schemaName": TRANSACTIONS_SCHEMA, "indexName": PROC_TS_INDEX_NAME}
    isValid = connection.execute(INDEX_VALIDITY_SQL, parameters).scalar_one_or_none()
    return isValid is True


def RebuildAndVerifyProcTsIndex(connection: Connection) -> None:
    """Run STEP20, STEP25 and STEP30 on the connection, then refuse an index that is not valid."""
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

    Raises JobFailure with EXIT_FAILURE on a database error or an index that is not valid; the
    transaction, the ledger completion included, then rolls back and the index keeps its
    previous state.
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
