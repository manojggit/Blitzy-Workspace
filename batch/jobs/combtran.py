"""Replaces: COMBTRAN (jcl/COMBTRAN.jcl: STEP05R SORT, STEP10 IDCAMS REPRO), copybook CVTRA05Y.

Merge the backed-up and the interest transactions into a new TRANSACT.COMBINED generation and
load them into the transaction master. Chain step 16 runs it, after the second TRANBKP (step 15)
archived and emptied transactions.transactions.

- STEP05R sorts the SORTIN concatenation of AWS.M2.CARDDEMO.TRANSACT.BKUP(0) and then
  AWS.M2.CARDDEMO.SYSTRAN(0) with SORT FIELDS=(TRAN-ID,A), where SYMNAMES defines TRAN-ID as
  1,16,CH, into TRANSACT.COMBINED(+1) (DCB=(*.SORTIN): the 350-byte CVTRA05Y record). CH
  compares the field's EBCDIC bytes, so the port sorts on the code page 037 bytes of the first 16
  characters. The sort is stable, so equal keys keep their SORTIN order. Records are written as
  read, so the generation is the byte-exact sorted concatenation of the two inputs.
- STEP10 REPRO INFILE(TRANSACT) OUTFILE(TRANVSAM) adds every combined record to the TRANSACT
  KSDS; the port inserts each one into transactions.transactions in file order.

Both steps run in one transaction with the generation's COMMITTED catalog row and the run's
completion, so the step is atomic, and the generation is published after the commit. A duplicate
key, or any other database, file or record failure, rolls everything back: the table is left as
it was, no generation is cataloged and the .part file is deleted. This replaces the REPRO
failure path (Technical Specification 0.6.12, "Utility steps").

Exit statuses: EXIT_OK; or JobFailure with EXIT_FAILURE when an input generation is missing or
LOST, on a duplicate key or another constraint violation, and on a database, file or record
error. Failure texts name generations, record numbers, fields, constraints, SQLSTATEs and errno
symbols, never a field's content (the batch/db.py diagnostics policy). docs/batch.md ("Step 16:
COMBTRAN") transcribes this contract. Step identity in the run ledger, not this module, keeps a
completed run from repeating in its cycle. The CLI holds the batch window around the command, so
this job never enters the online window.
"""

import errno
import logging
from collections.abc import Sequence

from app.models import Transaction
from app.utils.record_layouts import TRANSACTION_LAYOUT, DecodeRecord, RecordFormatError
from sqlalchemy import insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError

from batch.orchestration import gdg, run_ledger
from batch.orchestration.job_context import (
    EXIT_FAILURE,
    EXIT_OK,
    JobContext,
    JobFailure,
    JobResult,
)

LOGGER = logging.getLogger(__name__)

# The canonical CLI name and the legacy JCL name of batch_chain.JOB_DEFINITIONS["comb-tran"].
JOB_NAME = "comb-tran"
JCL_NAME = "COMBTRAN"

# SYMNAMES TRAN-ID,1,16,CH of STEP05R: the sort key is the first 16 bytes, compared as EBCDIC.
TRAN_ID_LENGTH = 16
EBCDIC_CODEC = "cp037"
# Rows per executemany statement of the STEP10 load.
INSERT_BATCH_SIZE = 500

TRANSACTION_TABLE = Transaction.__table__
# Every stored column except the generated sort key, which PostgreSQL computes from tran_id.
INSERT_COLUMN_NAMES = frozenset(
    column.name
    for column in TRANSACTION_TABLE.columns
    if column is not TRANSACTION_TABLE.c.tran_id_sort
)

RECORDS_COUNT_KEY = "records"
BACKUP_RECORDS_COUNT_KEY = "backup_records"
INTEREST_RECORDS_COUNT_KEY = "interest_records"

# The failures, besides a constraint violation, that roll the job back: database statements,
# the generation files and records the CVTRA05Y layout or the generation cannot hold. A
# ValueError of any other origin, such as gdg's refusal of a context with no started run, is a
# caller defect and propagates.
COMBTRAN_FAILURES = (DBAPIError, OSError, RecordFormatError, gdg.GenerationRecordError)

UNIQUE_VIOLATION_SQLSTATE = "23505"
UNKNOWN_CODE = "UNKNOWN"
FAILURE_TEMPLATE = "COMBTRAN FAILED: {reason}"
DUPLICATE_KEY_TEMPLATE = (
    "DUPLICATE KEY (SQLSTATE {sqlState}, CONSTRAINT {constraintName}); TRANSACT UNCHANGED AND"
    " NO TRANSACT.COMBINED GENERATION CATALOGED"
)
CONSTRAINT_VIOLATION_TEMPLATE = (
    "CONSTRAINT VIOLATION (SQLSTATE {sqlState}, CONSTRAINT {constraintName}); TRANSACT UNCHANGED"
    " AND NO TRANSACT.COMBINED GENERATION CATALOGED"
)
DATABASE_ERROR_TEMPLATE = "DATABASE ERROR {errorClass} ({driverClass}) SQLSTATE {sqlState}"
FILE_ERROR_TEMPLATE = "FILE ERROR {errorClass} ERRNO {errnoName}{fileName}"
FILE_NAME_TEMPLATE = " ON {fileName}"
RECORD_FIELD_TEMPLATE = (
    "TRANSACT.COMBINED(+1) record {recordNumber}: field {fieldName} at offset {fieldOffset}"
    " is not valid CVTRA05Y content"
)
RECORD_LAYOUT_TEMPLATE = "TRANSACT.COMBINED(+1) record {recordNumber} is not a CVTRA05Y record"
COMBINED_LOG = (
    "COMBTRAN: %d RECORDS (%d FROM TRANSACT.BKUP(0), %d FROM SYSTRAN(0)) SORTED INTO %s"
    " AND LOADED INTO TRANSACT"
)


def ResolveInputGenerations(context: JobContext) -> tuple[gdg.Generation, gdg.Generation]:
    """Resolve the STEP05R SORTIN concatenation: TRANSACT.BKUP(0), then SYSTRAN(0).

    A missing generation, a JCL error on MVS, or a LOST one in the OPEN cycle stops the job
    before anything is written, with EXIT_FAILURE and a message naming the base.
    """
    try:
        bkupGeneration = gdg.ResolveGeneration(context, gdg.TRANSACT_BKUP, 0)
        systranGeneration = gdg.ResolveGeneration(context, gdg.SYSTRAN, 0)
    except gdg.MissingGenerationError as error:
        raise JobFailure(EXIT_FAILURE, FAILURE_TEMPLATE.format(reason=error)) from error
    except gdg.LostGenerationError as error:
        raise JobFailure(EXIT_FAILURE, FAILURE_TEMPLATE.format(reason=error.message)) from error
    return bkupGeneration, systranGeneration


def TranIdSortKey(record: str) -> bytes:
    """Return the STEP05R sort key: TRAN-ID (1,16) as code page 037 bytes, compared as CH.

    Generation records are Latin-1 text, and code page 037 maps every Latin-1 character to one
    byte, so the key is defined for every record a generation can hold.
    """
    return record[:TRAN_ID_LENGTH].encode(EBCDIC_CODEC)


def ReadMergedRecords(
    bkupGeneration: gdg.Generation, systranGeneration: gdg.Generation
) -> list[str]:
    """STEP05R: concatenate BKUP(0) then SYSTRAN(0) and sort by TRAN-ID ascending.

    sorted() is stable, so records with equal keys keep their SORTIN order. The records are
    returned as read, never re-encoded.
    """
    concatenatedRecords = [
        *gdg.ReadGeneration(bkupGeneration),
        *gdg.ReadGeneration(systranGeneration),
    ]
    return sorted(concatenatedRecords, key=TranIdSortKey)


def DescribeRecordError(error: RecordFormatError, recordNumber: int) -> str:
    """Name the combined record and, when known, the field a decode refused; never its content."""
    if error.fieldName is None:
        return RECORD_LAYOUT_TEMPLATE.format(recordNumber=recordNumber)
    return RECORD_FIELD_TEMPLATE.format(
        recordNumber=recordNumber, fieldName=error.fieldName, fieldOffset=error.fieldOffset
    )


def TransactionRow(record: str, recordNumber: int) -> dict[str, object]:
    """Decode one combined CVTRA05Y record into the transactions.transactions columns it fills.

    FILLER is never decoded and the generated tran_id_sort is never supplied. A refusal is
    raised again as RecordFormatError naming the 1-based record number.
    """
    try:
        decodedRecord = DecodeRecord(record, TRANSACTION_LAYOUT)
    except RecordFormatError as error:
        reason = DescribeRecordError(error, recordNumber)
        raise RecordFormatError(reason, error.fieldName, error.fieldOffset) from error
    return {name: value for name, value in decodedRecord.items() if name in INSERT_COLUMN_NAMES}


def InsertTransactions(connection: Connection, mergedRecords: Sequence[str]) -> None:
    """STEP10 REPRO: insert every combined record into transactions.transactions, in file order.

    Records are not deduplicated: a TRAN-ID that repeats, or that the table already holds,
    raises IntegrityError, as the KSDS refuses a duplicate key.
    """
    for batchStart in range(0, len(mergedRecords), INSERT_BATCH_SIZE):
        recordBatch = mergedRecords[batchStart : batchStart + INSERT_BATCH_SIZE]
        transactionRows = [
            TransactionRow(record, batchStart + batchOffset + 1)
            for batchOffset, record in enumerate(recordBatch)
        ]
        connection.execute(insert(TRANSACTION_TABLE), transactionRows)


def WriteAndLoadCombined(
    context: JobContext, connection: Connection, mergedRecords: Sequence[str]
) -> gdg.GenerationWriter:
    """Write TRANSACT.COMBINED(+1) (STEP05R SORTOUT), then load it (STEP10), on one transaction.

    The load runs inside the generation block, so a failed insert discards the .part file.
    Given the connection, the generation's COMMITTED row joins the job's transaction.
    """
    with gdg.NewGeneration(context, gdg.TRANSACT_COMBINED, connection=connection) as writer:
        for record in mergedRecords:
            writer.WriteRecord(record)
        InsertTransactions(connection, mergedRecords)
    return writer


def BuildResult(
    mergedRecords: Sequence[str], bkupGeneration: gdg.Generation, systranGeneration: gdg.Generation
) -> JobResult:
    """Return EXIT_OK with the combined count and the cataloged count of each input."""
    counts = {
        RECORDS_COUNT_KEY: len(mergedRecords),
        BACKUP_RECORDS_COUNT_KEY: bkupGeneration.recordCount,
        INTEREST_RECORDS_COUNT_KEY: systranGeneration.recordCount,
    }
    return JobResult(EXIT_OK, counts)


def CombineAndReload(context: JobContext) -> tuple[JobResult, gdg.GenerationWriter]:
    """Read and sort the inputs, then write, load and finish the run in one transaction.

    The inputs are resolved and read before the write transaction opens. The run's completion
    is the transaction's last statement, after the generation's COMMITTED row.
    """
    bkupGeneration, systranGeneration = ResolveInputGenerations(context)
    mergedRecords = ReadMergedRecords(bkupGeneration, systranGeneration)
    with context.engine.begin() as connection:
        writer = WriteAndLoadCombined(context, connection, mergedRecords)
        result = BuildResult(mergedRecords, bkupGeneration, systranGeneration)
        run_ledger.FinishAtomicRun(connection, context, result)
    return result, writer


def SqlState(error: DBAPIError) -> str:
    """Return the driver's SQLSTATE of a database error, or UNKNOWN when it has none."""
    sqlState = getattr(error.orig, "pgcode", None)
    if isinstance(sqlState, str) and sqlState:
        return sqlState
    return UNKNOWN_CODE


def ConstraintName(error: IntegrityError) -> str:
    """Return the name of the constraint a database error violated, or UNKNOWN."""
    diagnostics = getattr(error.orig, "diag", None)
    constraintName = getattr(diagnostics, "constraint_name", None)
    if isinstance(constraintName, str) and constraintName:
        return constraintName
    return UNKNOWN_CODE


def DescribeConstraintViolation(error: IntegrityError) -> str:
    """Return the failure text of a refused insert: a duplicate key or another constraint."""
    sqlState = SqlState(error)
    template = CONSTRAINT_VIOLATION_TEMPLATE
    if sqlState == UNIQUE_VIOLATION_SQLSTATE:
        template = DUPLICATE_KEY_TEMPLATE
    reason = template.format(sqlState=sqlState, constraintName=ConstraintName(error))
    return FAILURE_TEMPLATE.format(reason=reason)


def DescribeDatabaseError(error: DBAPIError) -> str:
    """Return a database error's wrapper and driver classes and its SQLSTATE."""
    driverClass = UNKNOWN_CODE if error.orig is None else type(error.orig).__name__
    return DATABASE_ERROR_TEMPLATE.format(
        errorClass=type(error).__name__, driverClass=driverClass, sqlState=SqlState(error)
    )


def DescribeFileError(error: OSError) -> str:
    """Return a file error's class, errno symbol and, when known, the file it concerns."""
    errnoName = UNKNOWN_CODE
    if isinstance(error.errno, int):
        errnoName = errno.errorcode.get(error.errno, UNKNOWN_CODE)
    fileName = "" if error.filename is None else FILE_NAME_TEMPLATE.format(fileName=error.filename)
    return FILE_ERROR_TEMPLATE.format(
        errorClass=type(error).__name__, errnoName=errnoName, fileName=fileName
    )


def DescribeFailure(error: Exception) -> str:
    """Return the failure text of a COMBTRAN_FAILURES error, free of any field's content.

    GenerationRecordError and the RecordFormatError TransactionRow raises name generations,
    record numbers, fields and offsets only, so their own text is shown.
    """
    if isinstance(error, DBAPIError):
        reason = DescribeDatabaseError(error)
    elif isinstance(error, OSError):
        reason = DescribeFileError(error)
    else:
        reason = str(error)
    return FAILURE_TEMPLATE.format(reason=reason)


def RunJob(context: JobContext) -> JobResult:
    """Run COMBTRAN: sort BKUP(0) and SYSTRAN(0) into COMBINED(+1) and load it (chain step 16).

    Returns EXIT_OK with the records, backup_records and interest_records counts, once the
    transaction has committed and the generation is published. Raises JobFailure with
    EXIT_FAILURE on a missing or LOST input, a duplicate key or another failure, after which
    the table, the catalog and the archive directory are as they were.
    """
    try:
        result, writer = CombineAndReload(context)
    except IntegrityError as error:
        raise JobFailure(EXIT_FAILURE, DescribeConstraintViolation(error)) from error
    except COMBTRAN_FAILURES as error:
        raise JobFailure(EXIT_FAILURE, DescribeFailure(error)) from error
    # Outside the try: the step has committed, so a publication error must never be recorded as
    # a failed, retryable step; the next ReconcileCatalog completes the publication.
    gdg.PublishRunGenerations(context)
    counts = result.counts
    generationName = gdg.GenerationName(gdg.TRANSACT_COMBINED.name, writer.generation)
    LOGGER.info(
        COMBINED_LOG,
        counts[RECORDS_COUNT_KEY],
        counts[BACKUP_RECORDS_COUNT_KEY],
        counts[INTEREST_RECORDS_COUNT_KEY],
        generationName,
    )
    return result
