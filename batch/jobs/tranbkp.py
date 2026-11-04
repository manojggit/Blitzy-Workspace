"""Back up the transaction master to a new TRANSACT.BKUP generation and leave it empty.

Replaces: TRANBKP (jcl/TRANBKP.jcl), REPROC (proc/REPROC.prc), REPROCT (ctl/REPROCT.ctl),
copybook CVTRA05Y.

Chain steps 6 and 15 run this module through one batch_chain job definition. The legacy job
unloads the TRANSACT KSDS in key order with REPRO INFILE(FILEIN) OUTFILE(FILEOUT) into
AWS.M2.CARDDEMO.TRANSACT.BKUP(+1) (STEP05R, DCB LRECL=350, DISP=(NEW,CATLG,DELETE)), deletes the
cluster and its AIX (STEP05) and defines it again empty (STEP10, KEYS(16 0)). The port keeps that
net effect in one transaction: every row of transactions.transactions, in tran_id_sort order
(the EBCDIC order of the KEYS(16 0) prime key, which REPRO unloads in), becomes one 350-byte
CVTRA05Y record of the new generation, and the rows are then deleted. The generation's COMMITTED
catalog row and the run's completion commit together with the delete, so a failure leaves the
table, the catalog and the archive directory as they were. The generation is published after the
commit. TRANSACT.BKUP retention (LIMIT 5, NOSCRATCH) belongs to batch.orchestration.gdg.

Exit statuses: EXIT_OK, also for an empty table, whose generation holds no records; or
JobFailure with EXIT_FAILURE when a database statement, the archive file or a record encoding
fails. docs/batch.md ("Steps 6 and 15: TRANBKP") transcribes this contract. Step identity in the
run ledger, not this module, keeps a completed occurrence from running again in its cycle. The
CLI holds the batch window around the command, so this job never enters the online window.
"""

import logging
from collections.abc import Callable, Sequence

from app.models import Transaction
from app.utils.record_layouts import (
    TRANSACTION_LAYOUT,
    EncodeRecord,
    RecordFormatError,
    TimestampFormatError,
)
from sqlalchemy import delete, select
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import DBAPIError

from batch.orchestration import gdg, run_ledger
from batch.orchestration.job_context import (
    EXIT_FAILURE,
    EXIT_OK,
    JobContext,
    JobFailure,
    JobResult,
)

LOGGER = logging.getLogger(__name__)

# The canonical CLI name and the legacy JCL name of batch_chain.JOB_DEFINITIONS["tran-backup"].
JOB_NAME = "tran-backup"
JCL_NAME = "TRANBKP"

# DCB=(LRECL=350,RECFM=FB) of the STEP05R FILEOUT DD, which the CVTRA05Y layout must fill.
TRANSACTION_RECORD_LENGTH = 350
RECORDS_ARCHIVED_COUNT_KEY = "records_archived"

# Test hooks called, in order, right after the archive read and before any record is written or
# any row deleted. Production code never appends to the list; the concurrency tests append a
# callable that blocks to hold TRANBKP at that point.
AFTER_ARCHIVE_READ_HOOKS: list[Callable[[], None]] = []

TRANSACTION_TABLE = Transaction.__table__
# Every stored column except the generated sort key, which orders the read but is no record field.
ARCHIVE_COLUMNS = tuple(
    column for column in TRANSACTION_TABLE.columns if column is not TRANSACTION_TABLE.c.tran_id_sort
)

# The failures that roll the job back: database statements, the .part file write and fsync, and
# records the CVTRA05Y layout or the generation cannot hold. A ValueError of any other origin,
# such as gdg's refusal of a context with no started run, is a caller defect and propagates.
ARCHIVE_FAILURES = (
    DBAPIError,
    OSError,
    RecordFormatError,
    TimestampFormatError,
    gdg.GenerationRecordError,
)

FAILURE_TEMPLATE = "TRANBKP FAILED: {reason}"
RECORD_LENGTH_TEMPLATE = "the CVTRA05Y record is {length} characters; the LRECL is {lrecl}"
DELETE_MISMATCH_TEMPLATE = (
    "{deletedCount} TRANSACTIONS DELETED BUT {archivedCount} ARCHIVED;"
    " TRANSACT CHANGED DURING THE BACKUP"
)
ARCHIVED_LOG = "TRANBKP: %d RECORDS ARCHIVED TO %s; TRANSACT EMPTIED"


def ReadArchiveRows(connection: Connection) -> Sequence[RowMapping]:
    """Return every transaction in tran_id_sort order, the order REPRO unloads the KSDS in."""
    statement = select(*ARCHIVE_COLUMNS).order_by(TRANSACTION_TABLE.c.tran_id_sort)
    return connection.execute(statement).mappings().all()


def RunAfterArchiveReadHooks() -> None:
    """Call every AFTER_ARCHIVE_READ_HOOKS callable in order.

    A snapshot is iterated, so a test thread that removes its hook while TRANBKP waits in it
    cannot make the loop skip another hook.
    """
    for hook in tuple(AFTER_ARCHIVE_READ_HOOKS):
        hook()


def EncodeTransactionRecord(archiveRow: RowMapping) -> str:
    """Return the 350-character CVTRA05Y record of one transaction row, FILLER as spaces.

    Each timestamp is rebuilt from its value and its stored format code, so the record carries
    the 26 bytes the transaction was posted with.
    """
    recordText = EncodeRecord(dict(archiveRow), TRANSACTION_LAYOUT)
    if len(recordText) != TRANSACTION_RECORD_LENGTH:
        raise RecordFormatError(
            RECORD_LENGTH_TEMPLATE.format(length=len(recordText), lrecl=TRANSACTION_RECORD_LENGTH)
        )
    return recordText


def WriteArchiveRecords(writer: gdg.GenerationWriter, archiveRows: Sequence[RowMapping]) -> None:
    """Write one generation record per transaction row, in the order the rows were read."""
    for archiveRow in archiveRows:
        writer.WriteRecord(EncodeTransactionRecord(archiveRow))


def DeleteAllTransactions(connection: Connection, archivedCount: int) -> None:
    """Empty transactions.transactions, as STEP05 DELETE and STEP10 DEFINE leave the cluster.

    The batch window keeps every other writer out, so the delete removes exactly the archived
    rows; a different count means the table changed during the backup, and the job is refused
    rather than lose a transaction that is in neither the archive nor the table.
    """
    deletedCount = connection.execute(delete(TRANSACTION_TABLE)).rowcount
    if deletedCount != archivedCount:
        reason = DELETE_MISMATCH_TEMPLATE.format(
            deletedCount=deletedCount, archivedCount=archivedCount
        )
        raise JobFailure(EXIT_FAILURE, FAILURE_TEMPLATE.format(reason=reason))


def ArchiveAndClearTransactions(
    context: JobContext, connection: Connection
) -> gdg.GenerationWriter:
    """Write TRANSACT.BKUP(+1) from the table, then empty it, on the job's transaction.

    The delete runs inside the generation block, so a failed delete discards the .part file.
    Given the connection, the generation's COMMITTED row joins the job's transaction.
    """
    with gdg.NewGeneration(context, gdg.TRANSACT_BKUP, connection=connection) as writer:
        archiveRows = ReadArchiveRows(connection)
        RunAfterArchiveReadHooks()
        WriteArchiveRecords(writer, archiveRows)
        DeleteAllTransactions(connection, len(archiveRows))
    return writer


def DescribeFailure(error: Exception) -> str:
    """Return the failure text: the error's first line, without a database statement echo."""
    errorLines = str(error).splitlines()
    reason = errorLines[0] if errorLines else type(error).__name__
    return FAILURE_TEMPLATE.format(reason=reason)


def RunJob(context: JobContext) -> JobResult:
    """Archive every transaction to TRANSACT.BKUP(+1) and empty the table (chain steps 6 and 15).

    Returns EXIT_OK with the records_archived count. Raises JobFailure with EXIT_FAILURE on a
    database, file or record error, after which nothing is archived, cataloged or deleted.
    """
    try:
        with context.engine.begin() as connection:
            writer = ArchiveAndClearTransactions(context, connection)
            result = JobResult(EXIT_OK, {RECORDS_ARCHIVED_COUNT_KEY: writer.recordCount})
            run_ledger.FinishAtomicRun(connection, context, result)
    except ARCHIVE_FAILURES as error:
        raise JobFailure(EXIT_FAILURE, DescribeFailure(error)) from error
    # Outside the try: the step has committed, so a publication error must never be recorded as
    # a failed, retryable step; the next ReconcileCatalog completes the publication.
    gdg.PublishRunGenerations(context)
    generationName = gdg.GenerationName(gdg.TRANSACT_BKUP.name, writer.generation)
    LOGGER.info(ARCHIVED_LOG, writer.recordCount, generationName)
    return result
