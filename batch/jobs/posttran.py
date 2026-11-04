"""Replaces: POSTTRAN (jcl/POSTTRAN.jcl STEP15, PGM=CBTRN02C) and program CBTRN02C, the daily
transaction posting; copybooks CVTRA06Y, CVTRA05Y, CVACT01Y, CVACT03Y, CVTRA01Y.

Chain step 13, a per-write step, ported from the CBTRN02C PROCEDURE DIVISION (cbl/CBTRN02C.cbl
lines 190-231 and 365-600). The posting rules (1500-VALIDATE-TRAN, 2000-POST-TRANSACTION and
WS-VALIDATION-TRAILER) live once in app.services.transactions.cbtrn02c; this module performs the
reads and writes in the source's order. The staged rows of transactions.daily_transactions (the
DALYTRAN DD) are read in record_seq order, never updated or deleted, and each one is:

- validated against its cross-reference (XREFFILE) and account (ACCTFILE). A reject is written
  to DALYREJS(+1) (RECFM F, LRECL 430) as the 350-byte daily record and its 80-byte trailer;
- otherwise posted: its category balance (TCATBALF) is created or rewritten, its account
  rewritten, and its transaction (TRANFILE) inserted. An account rewrite that finds no row is
  reason 109: the transaction is still inserted and no reject is counted.

docs/batch.md ("Step 13: POSTTRAN", "POSTTRAN: posting") transcribes this contract.

Durability: every CardDemo file is RECOVERY(NONE), so each write commits as it is made and an
abend leaves the earlier records posted. The category-balance write commits with the validation
reads that lock its account; the account rewrite and the transaction insert follow in units of
their own. The DALYREJS generation's COMMITTED catalog row and the run's completion share one
final transaction, committed after the last posting write, and the generation is published once
that has committed. An abend deletes the .part file and catalogs nothing, as
DISP=(NEW,CATLG,DELETE) does.

Exit statuses: EXIT_OK; REJECTS_RETURN_CODE (4) when the stored reject count is above zero; or
JobFailure with EXIT_ABEND (9999-ABEND-PROGRAM, CEE3ABD 999) on a duplicate key or any database,
file or record-encoding error. Every amount is a Decimal computed by ApplyPosting. The CLI holds
the batch window around the command, so this job never enters the online window.
"""

import contextlib
import errno
import logging
from collections.abc import Iterator
from dataclasses import dataclass, replace

from app.models import Account, CardXref, DailyTransaction, Transaction, TransactionCategoryBalance
from app.services.transactions.cbtrn02c import (
    ACCOUNT_REWRITE_NOT_FOUND,
    PROCESSED_COUNT_LABEL,
    REJECTED_COUNT_LABEL,
    REJECTS_RETURN_CODE,
    ApplyPosting,
    BuildValidationTrailer,
    PostingEffects,
    PostingInput,
    RejectOutcome,
    ValidateDailyTransaction,
)
from app.utils.record_layouts import (
    DAILY_TRANSACTION_LAYOUT,
    EncodeRecord,
    RecordFormatError,
    TimestampFormatError,
)
from sqlalchemy import Select, insert, select, update
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import DBAPIError

from batch.orchestration import gdg, run_ledger
from batch.orchestration.job_context import EXIT_ABEND, EXIT_OK, JobContext, JobFailure, JobResult

LOGGER = logging.getLogger(__name__)

# The canonical CLI name and the legacy JCL name of batch_chain.JOB_DEFINITIONS["post-tran"].
JOB_NAME = "post-tran"
JCL_NAME = "POSTTRAN"
PROGRAM_NAME = "CBTRN02C"

# Staged rows per keyset read of the DALYTRAN file. Each batch is read on a short connection of
# its own, so no read transaction stays open while the records are posted.
STAGED_BATCH_SIZE = 500
# ADD 1 has no ON SIZE ERROR, so WS-TRANSACTION-COUNT and WS-REJECT-COUNT 9(09) wrap to zero.
COUNTER_MODULUS = 10**9
# FD-TRAN-CAT-KEY (CVTRA01Y): TRANCAT-ACCT-ID 9(11), TRANCAT-TYPE-CD X(02), TRANCAT-CD 9(04).
CATEGORY_TYPE_LENGTH = 2
CATEGORY_CODE_DIGITS = 4

# The DISPLAY texts of the mainline and of 2700-UPDATE-TCATBAL. COUNT_LOG shows a PIC 9(09)
# counter after its label, as DISPLAY does.
START_LOG = "START OF EXECUTION OF PROGRAM %s"
END_LOG = "END OF EXECUTION OF PROGRAM %s"
COUNT_LOG = "%s%09d"
TCATBAL_CREATE_LOG = "TCATBAL record not found for key : %s.. Creating."
ACCOUNT_REWRITE_NOT_FOUND_LOG = (
    "DALYTRAN RECORD %d: REWRITE OF ACCOUNT %s FOUND NO RECORD, REASON %04d %s;"
    " THE TRANSACTION IS STILL WRITTEN AND NO REJECT IS COUNTED"
)

# The failing paragraph each abend names. The DALYTRAN, rejects, transaction-balance and
# transaction-file texts are the source's DISPLAY literals; the source checks no status after
# its other reads and rewrites, so those texts name the file in the source's own style.
DALYTRAN_READ_ERROR = "ERROR READING DALYTRAN FILE"
XREF_READ_ERROR = "ERROR READING CROSS REF FILE"
ACCOUNT_READ_ERROR = "ERROR READING ACCOUNT MASTER FILE"
TCATBAL_READ_ERROR = "ERROR READING TRANSACTION BALANCE FILE"
TCATBAL_WRITE_ERROR = "ERROR WRITING TRANSACTION BALANCE FILE"
TCATBAL_REWRITE_ERROR = "ERROR REWRITING TRANSACTION BALANCE FILE"
VALIDATION_UNIT_ERROR = "ERROR ACCESSING CROSS REF, ACCOUNT OR TRANSACTION BALANCE FILE"
ACCOUNT_REWRITE_ERROR = "ERROR REWRITING ACCOUNT MASTER FILE"
TRANSACTION_WRITE_ERROR = "ERROR WRITING TO TRANSACTION FILE"
REJECT_WRITE_ERROR = "ERROR WRITING TO REJECTS FILE"
REJECTS_FILE_ERROR = "ERROR OPENING OR CLOSING DALY REJECTS FILE"
ABEND_LOG = "ABENDING PROGRAM"
ABEND_DISPLAY_LOG = "%s: %s"
ABEND_MESSAGE_TEMPLATE = "ABENDING PROGRAM {program}: {site}: {description}"
RECORD_SITE_TEMPLATE = "{reason} AT DALYTRAN RECORD {recordSeq}"

# Every failure that ends CBTRN02C with 9999-ABEND-PROGRAM: a database error (a duplicate key
# among them), a DALYREJS file error, and a reject record that cannot be encoded or written.
ABEND_ERRORS = (
    DBAPIError,
    OSError,
    RecordFormatError,
    TimestampFormatError,
    gdg.GenerationRecordError,
)
# Those failures are described by exception class, SQLSTATE and errno symbol only, because a
# driver message or an encoding error can quote row values (the batch/db.py diagnostics policy).
ABEND_DATABASE_TEMPLATE = "DATABASE ERROR {errorClass} ({driverClass}) SQLSTATE {sqlState}"
ABEND_FILE_TEMPLATE = "FILE ERROR {errorClass} ERRNO {errnoName}"
ABEND_RECORD_TEMPLATE = "RECORD ENCODING ERROR {errorClass}"
ABEND_UNKNOWN_CODE = "UNKNOWN"

# The batch.job_runs.counts keys.
PROCESSED_COUNT_KEY = "processed"
REJECTED_COUNT_KEY = "rejected"
POSTED_COUNT_KEY = "posted"


@dataclass
class PostingCounts:
    """WS-COUNTERS (lines 184-186): WS-TRANSACTION-COUNT and WS-REJECT-COUNT, both PIC 9(09)."""

    processed: int = 0
    rejected: int = 0

    def CountProcessed(self) -> None:
        """ADD 1 TO WS-TRANSACTION-COUNT (line 206), which wraps from 999999999 to zero."""
        self.processed = (self.processed + 1) % COUNTER_MODULUS

    def CountRejected(self) -> None:
        """ADD 1 TO WS-REJECT-COUNT (line 214), which wraps from 999999999 to zero."""
        self.rejected = (self.rejected + 1) % COUNTER_MODULUS

    def Posted(self) -> int:
        """Return the records posted: processed less rejected, within the same nine digits."""
        return (self.processed - self.rejected) % COUNTER_MODULUS


def RunJob(context: JobContext) -> JobResult:
    """Run POSTTRAN: post the staged daily transactions and write DALYREJS(+1) (step 13).

    Returns EXIT_OK, or REJECTS_RETURN_CODE when any record was rejected, with the processed,
    rejected and posted counts, once the final transaction has recorded the run COMPLETED and
    the generation is published. Raises JobFailure with EXIT_ABEND on any abend: the postings
    committed before it stay, and no DALYREJS generation is cataloged.
    """
    LOGGER.info(START_LOG, PROGRAM_NAME)
    with AbendOn(REJECTS_FILE_ERROR, None):
        result = PostDailyTransactions(context)
    # Outside the abend mapping: the run is already COMPLETED, so a publication failure is not
    # an abend, and the next command's gdg.ReconcileCatalog publishes the COMMITTED row.
    gdg.PublishRunGenerations(context)
    LOGGER.info(END_LOG, PROGRAM_NAME)
    return result


def PostDailyTransactions(context: JobContext) -> JobResult:
    """The mainline, lines 193-234: open the files, post every record, close the files.

    The DALYREJS catalog row and the run completion commit together on finalConnection, after
    the last posting write has committed in its own unit (Technical Specification 0.7.2).
    """
    with context.engine.connect() as finalConnection:
        with gdg.NewGeneration(context, gdg.DALYREJS, connection=finalConnection) as rejectWriter:
            counts = PostAllRecords(context, rejectWriter)
        LogCounts(counts)
        result = BuildPostingResult(counts)
        run_ledger.FinishAtomicRun(finalConnection, context, result)
        finalConnection.commit()
    return result


def PostAllRecords(context: JobContext, rejectWriter: gdg.GenerationWriter) -> PostingCounts:
    """PERFORM UNTIL END-OF-FILE = 'Y', lines 202-220, over every staged record in file order."""
    counts = PostingCounts()
    for stagedRow in StagedRecordsInOrder(context):
        ProcessStagedRecord(context, stagedRow, rejectWriter, counts)
    return counts


def StagedRecordsInOrder(context: JobContext) -> Iterator[RowMapping]:
    """1000-DALYTRAN-GET-NEXT, lines 345-368: the staged records in record_seq order.

    Each keyset batch is read and its connection closed before its records are posted, so a
    daily file of any size is read without a long transaction. POSTTRAN never changes the
    staged rows, so the keyset pages stay consistent.
    """
    lastSeq = None
    while True:
        stagedBatch = ReadStagedBatch(context, lastSeq)
        if not stagedBatch:
            return
        yield from stagedBatch
        lastSeq = stagedBatch[-1]["record_seq"]


def ReadStagedBatch(context: JobContext, lastSeq: int | None) -> list[RowMapping]:
    """READ DALYTRAN-FILE INTO DALYTRAN-RECORD for the next batch after lastSeq."""
    statement = StagedBatchStatement(lastSeq)
    with AbendOn(DALYTRAN_READ_ERROR, None), context.engine.connect() as readConnection:
        return list(readConnection.execute(statement).mappings())


def StagedBatchStatement(lastSeq: int | None) -> Select:
    """Return the next STAGED_BATCH_SIZE staged rows after lastSeq, or from the first one."""
    stagedTable = DailyTransaction.__table__
    statement = select(stagedTable).order_by(stagedTable.c.record_seq).limit(STAGED_BATCH_SIZE)
    if lastSeq is None:
        return statement
    return statement.where(stagedTable.c.record_seq > lastSeq)


def ProcessStagedRecord(
    context: JobContext,
    stagedRow: RowMapping,
    rejectWriter: gdg.GenerationWriter,
    counts: PostingCounts,
) -> None:
    """One DALYTRAN-RECORD of the mainline, lines 205-217: validate, then post or reject it."""
    counts.CountProcessed()
    validation = RunValidationUnit(context, stagedRow)
    if isinstance(validation, RejectOutcome):
        counts.CountRejected()
        WriteRejectRecord(rejectWriter, stagedRow, validation)
        return
    PostValidRecord(context, validation, stagedRow["record_seq"])


def RunValidationUnit(context: JobContext, stagedRow: RowMapping) -> RejectOutcome | PostingEffects:
    """1500-VALIDATE-TRAN, then 2700-UPDATE-TCATBAL for a valid record, in one committed unit.

    The cross-reference and the account (locked FOR UPDATE) are read and the record validated.
    A reject returns its outcome. A valid record reads and locks its category balance, as only
    2700 reads TCATBALF, and writes it; the unit commits before the account is rewritten.
    """
    recordSeq = stagedRow["record_seq"]
    with AbendOn(VALIDATION_UNIT_ERROR, recordSeq), context.engine.begin() as connection:
        postingInput = ReadValidationInput(connection, context, stagedRow)
        outcome = ValidateDailyTransaction(postingInput)
        if outcome is not None:
            return outcome
        acctId = postingInput.xref["acct_id"]
        categoryBalance = ReadCategoryBalanceForUpdate(connection, acctId, stagedRow)
        effects = ApplyPosting(replace(postingInput, categoryBalance=categoryBalance))
        WriteCategoryBalance(connection, effects, recordSeq)
    return effects


def ReadValidationInput(
    connection: Connection, context: JobContext, stagedRow: RowMapping
) -> PostingInput:
    """The records 1500-VALIDATE-TRAN (lines 370-378) reads: the xref, then its account.

    The account is read only when the cross-reference was found. The clock is read once per
    record; ApplyPosting uses it as Z-GET-DB2-FORMAT-TIMESTAMP for a posted record's PROC-TS.
    """
    xref = ReadXref(connection, stagedRow)
    account = None
    if xref is not None:
        account = ReadAccountForUpdate(connection, xref["acct_id"], stagedRow["record_seq"])
    return PostingInput(
        staged=stagedRow,
        xref=xref,
        account=account,
        categoryBalance=None,
        now=context.clock.Now(),
    )


def ReadXref(connection: Connection, stagedRow: RowMapping) -> RowMapping | None:
    """1500-A-LOOKUP-XREF, lines 380-391: READ XREF-FILE by DALYTRAN-CARD-NUM.

    Returns the CARD-XREF-RECORD row, or None for INVALID KEY (reason 100).
    """
    statement = select(CardXref.__table__).where(CardXref.card_num == stagedRow["card_num"])
    with AbendOn(XREF_READ_ERROR, stagedRow["record_seq"]):
        return connection.execute(statement).mappings().one_or_none()


def ReadAccountForUpdate(connection: Connection, acctId: str, recordSeq: int) -> RowMapping | None:
    """1500-B-LOOKUP-ACCT, lines 393-422: READ ACCOUNT-FILE by XREF-ACCT-ID, locked FOR UPDATE.

    Returns the whole ACCOUNT-RECORD row, or None for INVALID KEY (reason 101).
    """
    statement = select(Account.__table__).where(Account.acct_id == acctId).with_for_update()
    with AbendOn(ACCOUNT_READ_ERROR, recordSeq):
        return connection.execute(statement).mappings().one_or_none()


def ReadCategoryBalanceForUpdate(
    connection: Connection, acctId: str, stagedRow: RowMapping
) -> RowMapping | None:
    """The READ TCATBAL-FILE of 2700-UPDATE-TCATBAL, lines 467-492, locked FOR UPDATE.

    The key is XREF-ACCT-ID, DALYTRAN-TYPE-CD and DALYTRAN-CAT-CD. Returns the
    TRAN-CAT-BAL-RECORD row, or None for INVALID KEY (status 23), which creates the record.
    """
    statement = (
        select(TransactionCategoryBalance.__table__)
        .where(
            TransactionCategoryBalance.acct_id == acctId,
            TransactionCategoryBalance.tran_type_cd == stagedRow["tran_type_cd"],
            TransactionCategoryBalance.tran_cat_cd == stagedRow["tran_cat_cd"],
        )
        .with_for_update()
    )
    with AbendOn(TCATBAL_READ_ERROR, stagedRow["record_seq"]):
        return connection.execute(statement).mappings().one_or_none()


def WriteCategoryBalance(connection: Connection, effects: PostingEffects, recordSeq: int) -> None:
    """2700-UPDATE-TCATBAL, lines 494-499: create the record (2700-A) or rewrite it (2700-B)."""
    balanceRow = effects.categoryBalance
    if effects.isNewCategoryBalance:
        LOGGER.info(TCATBAL_CREATE_LOG, CategoryBalanceKeyText(balanceRow))
        CreateCategoryBalance(connection, balanceRow, recordSeq)
    else:
        RewriteCategoryBalance(connection, balanceRow, recordSeq)


def CategoryBalanceKeyText(balanceRow: dict[str, object]) -> str:
    """FD-TRAN-CAT-KEY as DISPLAY shows it: account 9(11), type X(02) and category 9(04)."""
    typeText = str(balanceRow["tran_type_cd"]).ljust(CATEGORY_TYPE_LENGTH)
    categoryText = f"{balanceRow['tran_cat_cd']:0{CATEGORY_CODE_DIGITS}d}"
    return f"{balanceRow['acct_id']}{typeText}{categoryText}"


def CreateCategoryBalance(
    connection: Connection, balanceRow: dict[str, object], recordSeq: int
) -> None:
    """2700-A-CREATE-TCATBAL-REC, lines 503-524: WRITE the new TRAN-CAT-BAL-RECORD."""
    statement = insert(TransactionCategoryBalance).values(**balanceRow)
    with AbendOn(TCATBAL_WRITE_ERROR, recordSeq):
        connection.execute(statement)


def RewriteCategoryBalance(
    connection: Connection, balanceRow: dict[str, object], recordSeq: int
) -> None:
    """2700-B-UPDATE-TCATBAL-REC, lines 526-542: REWRITE TRAN-CAT-BAL with the new balance."""
    statement = (
        update(TransactionCategoryBalance)
        .where(
            TransactionCategoryBalance.acct_id == balanceRow["acct_id"],
            TransactionCategoryBalance.tran_type_cd == balanceRow["tran_type_cd"],
            TransactionCategoryBalance.tran_cat_cd == balanceRow["tran_cat_cd"],
        )
        .values(balance=balanceRow["balance"])
    )
    with AbendOn(TCATBAL_REWRITE_ERROR, recordSeq):
        connection.execute(statement)


def WriteRejectRecord(
    rejectWriter: gdg.GenerationWriter, stagedRow: RowMapping, outcome: RejectOutcome
) -> None:
    """2500-WRITE-REJECT-REC, lines 446-465: WRITE FD-REJS-RECORD FROM REJECT-RECORD."""
    with AbendOn(REJECT_WRITE_ERROR, stagedRow["record_seq"]):
        rejectWriter.WriteRecord(BuildRejectRecord(stagedRow, outcome))


def BuildRejectRecord(stagedRow: RowMapping, outcome: RejectOutcome) -> str:
    """REJECT-RECORD (lines 176-178): the 350-byte DALYTRAN-RECORD, then WS-VALIDATION-TRAILER.

    The daily record is rebuilt from the staged row in the CVTRA06Y layout, each timestamp in
    its stored text form and the FILLER as spaces, so it equals the image's record; the trailer
    adds the reason 9(04) and its X(76) description, 430 characters in all.
    """
    dailyRecord = EncodeRecord(dict(stagedRow), DAILY_TRANSACTION_LAYOUT)
    return dailyRecord + BuildValidationTrailer(outcome)


def PostValidRecord(context: JobContext, effects: PostingEffects, recordSeq: int) -> None:
    """The rest of 2000-POST-TRANSACTION, lines 441-442: 2800, then 2900 whatever 2800 found.

    A rewrite that finds no account is reason 109 (REWRITE INVALID KEY, lines 554-558): it is
    logged, the transaction is still written, and the record is not counted as a reject.
    """
    if not RewriteAccount(context, effects.account, recordSeq):
        LOGGER.warning(
            ACCOUNT_REWRITE_NOT_FOUND_LOG,
            recordSeq,
            effects.account["acct_id"],
            ACCOUNT_REWRITE_NOT_FOUND.reasonCode,
            ACCOUNT_REWRITE_NOT_FOUND.reasonDescription,
        )
    InsertTransaction(context, effects.transaction, recordSeq)


def RewriteAccount(context: JobContext, accountValues: dict[str, object], recordSeq: int) -> bool:
    """2800-UPDATE-ACCOUNT-REC, lines 545-560: REWRITE the account, in a unit of its own.

    The balance and cycle fields are the absolute values ApplyPosting stored from the record
    read in validation, as REWRITE FD-ACCTFILE-REC FROM ACCOUNT-RECORD writes that image, never
    increments. Returns False when no row matched, the INVALID KEY of reason 109.
    """
    statement = (
        update(Account)
        .where(Account.acct_id == accountValues["acct_id"])
        .values(
            curr_balance=accountValues["curr_balance"],
            curr_cycle_credit=accountValues["curr_cycle_credit"],
            curr_cycle_debit=accountValues["curr_cycle_debit"],
        )
    )
    with AbendOn(ACCOUNT_REWRITE_ERROR, recordSeq), context.engine.begin() as connection:
        return connection.execute(statement).rowcount > 0


def InsertTransaction(
    context: JobContext, transactionValues: dict[str, object], recordSeq: int
) -> None:
    """2900-WRITE-TRANSACTION-FILE, lines 562-580: WRITE TRAN-RECORD, in a unit of its own.

    The values carry every column but the generated tran_id_sort. A duplicate TRAN-ID (status
    22) or any other failure abends.
    """
    statement = insert(Transaction).values(**transactionValues)
    with AbendOn(TRANSACTION_WRITE_ERROR, recordSeq), context.engine.begin() as connection:
        connection.execute(statement)


def BuildPostingResult(counts: PostingCounts) -> JobResult:
    """MOVE 4 TO RETURN-CODE when WS-REJECT-COUNT > 0 (lines 229-231), with the run's counts."""
    exitCode = REJECTS_RETURN_CODE if counts.rejected > 0 else EXIT_OK
    resultCounts = {
        PROCESSED_COUNT_KEY: counts.processed,
        REJECTED_COUNT_KEY: counts.rejected,
        POSTED_COUNT_KEY: counts.Posted(),
    }
    return JobResult(exitCode, resultCounts)


def LogCounts(counts: PostingCounts) -> None:
    """DISPLAY 'TRANSACTIONS PROCESSED :' and 'TRANSACTIONS REJECTED  :' (lines 227-228)."""
    LOGGER.info(COUNT_LOG, PROCESSED_COUNT_LABEL, counts.processed)
    LOGGER.info(COUNT_LOG, REJECTED_COUNT_LABEL, counts.rejected)


@contextlib.contextmanager
def AbendOn(reason: str, recordSeq: int | None) -> Iterator[None]:
    """Turn an ABEND_ERRORS failure inside the block into 9999-ABEND-PROGRAM (abend 999).

    reason is the DISPLAY of the failing paragraph, and recordSeq the staged record in process,
    or None outside a record. A JobFailure raised inside the block passes through unchanged.
    """
    try:
        yield
    except ABEND_ERRORS as error:
        raise AbendProgram(reason, recordSeq, error) from error


def AbendProgram(reason: str, recordSeq: int | None, error: Exception) -> JobFailure:
    """9999-ABEND-PROGRAM, lines 707-711: DISPLAY the failure, then CEE3ABD 999 (exit 16).

    Returns the JobFailure for the caller to raise, naming the paragraph, the record and the
    error's class, SQLSTATE or errno symbol.
    """
    site = reason
    if recordSeq is not None:
        site = RECORD_SITE_TEMPLATE.format(reason=reason, recordSeq=recordSeq)
    description = DescribeAbend(error)
    LOGGER.error(ABEND_DISPLAY_LOG, site, description)
    LOGGER.error(ABEND_LOG)
    message = ABEND_MESSAGE_TEMPLATE.format(
        program=PROGRAM_NAME, site=site, description=description
    )
    return JobFailure(EXIT_ABEND, message)


def DescribeAbend(error: Exception) -> str:
    """Describe an ABEND_ERRORS failure by class, SQLSTATE or errno symbol, never by row value."""
    errorClass = type(error).__name__
    if isinstance(error, DBAPIError):
        return DescribeDatabaseError(error)
    if isinstance(error, OSError):
        return ABEND_FILE_TEMPLATE.format(errorClass=errorClass, errnoName=ErrnoName(error))
    return ABEND_RECORD_TEMPLATE.format(errorClass=errorClass)


def DescribeDatabaseError(error: DBAPIError) -> str:
    """Return a database error's wrapper and driver classes and the driver's SQLSTATE."""
    driverError = error.orig
    sqlState = getattr(driverError, "pgcode", None)
    if not isinstance(sqlState, str) or not sqlState:
        sqlState = ABEND_UNKNOWN_CODE
    driverClass = ABEND_UNKNOWN_CODE if driverError is None else type(driverError).__name__
    return ABEND_DATABASE_TEMPLATE.format(
        errorClass=type(error).__name__, driverClass=driverClass, sqlState=sqlState
    )


def ErrnoName(error: OSError) -> str:
    """Return the errno symbol of a file error, such as ENOSPC, or UNKNOWN when it has none."""
    if isinstance(error.errno, int):
        return errno.errorcode.get(error.errno, ABEND_UNKNOWN_CODE)
    return ABEND_UNKNOWN_CODE
