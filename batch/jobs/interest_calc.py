"""Replaces: INTCALC (jcl/INTCALC.jcl STEP15, PGM=CBACT04C,PARM='2022071800') and program
CBACT04C, the interest calculator; copybooks CVTRA01Y, CVTRA02Y, CVACT01Y, CVACT03Y, CVTRA05Y.

Chain step 14, a per-write step, ported from cbl/CBACT04C.cbl lines 180-520. The category
balances (TCATBALF, opened INPUT) are read in key order. On each change of account after the
first, 1050-UPDATE-ACCOUNT rewrites the previous account: the total interest is added to its
balance and both cycle fields become zero. The new account (ACCTFILE) and its first
cross-reference card (XREFFILE through the XREFFIL1 AIX path) are then read. Each balance's rate
comes from DISCGRP by (group, type, category), retried with group DEFAULT; a non-zero rate gives
the monthly interest and one record of SYSTRAN(+1), the TRANSACT DD (RECFM F, LRECL 350). The
job never writes transactions.transactions, which COMBTRAN loads from SYSTRAN, nor the category
balances. docs/batch.md ("Step 14: INTCALC", "INTCALC: interest calculation") transcribes this.

Durability: each account rewrite commits in its own transaction as it is made, as RECOVERY(NONE)
VSAM does, so an abend leaves the earlier rewrites in place. The run's completion and then the
SYSTRAN generation's COMMITTED catalog row, the job's final database write, share one final
transaction, committed after the last rewrite; the chain (batch_chain) publishes the generation
once that has committed. An abend deletes the .part file and catalogs nothing, as
DISP=(NEW,CATLG,DELETE) does.

Exit statuses: EXIT_OK, or JobFailure with EXIT_ABEND (9999-ABEND-PROGRAM, CEE3ABD 999) when an
account, its cross-reference or the DEFAULT disclosure group is missing, a rewrite finds no
account, or a database, file or record-encoding error occurs. Amounts are Decimal only, stored
with StoreInto as COBOL stores them without ROUNDED or ON SIZE ERROR. The CLI holds the batch
window around the command, so this job never enters the online window.
"""

import errno
import logging
from dataclasses import dataclass
from decimal import Decimal, localcontext

from app.models import (
    KEY_COLLATION,
    Account,
    Card,
    CardXref,
    DisclosureGroup,
    TransactionCategoryBalance,
)
from app.utils.decimal_utils import COBOL_CONTEXT, StoreInto
from app.utils.record_layouts import (
    TIMESTAMP_FORMAT_DB2,
    TRANSACTION_LAYOUT,
    EncodeRecord,
    RecordFormatError,
    TimestampFormatError,
)
from sqlalchemy import Select, Update, func, select, update
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import DBAPIError

from batch.orchestration import gdg, run_ledger
from batch.orchestration.job_context import EXIT_ABEND, EXIT_OK, JobContext, JobFailure, JobResult

LOGGER = logging.getLogger(__name__)

JOB_NAME = "interest-calc"
JCL_NAME = "INTCALC"
PROGRAM_NAME = "CBACT04C"

# The constant fields of 1300-B-WRITE-TX (lines 482-494). MOVE '05' into TRAN-CAT-CD PIC 9(04)
# stores 0005, and MOVE 0 into TRAN-MERCHANT-ID PIC 9(09) stores nine zeros.
INTEREST_TRAN_TYPE_CD = "01"
INTEREST_TRAN_CAT_CD = 5
INTEREST_TRAN_SOURCE = "System"
INTEREST_DESC_PREFIX = "Int. for a/c "
ZERO_MERCHANT_ID = "000000000"
BLANK_MERCHANT_FIELD = ""
# The group 1200-GET-INTEREST-RATE retries with when the account's group has no record (line 437).
DEFAULT_GROUP_ID = "DEFAULT"
# COMPUTE WS-MONTHLY-INT = (TRAN-CAT-BAL * DIS-INT-RATE) / 1200 (lines 464-465).
INTEREST_DIVISOR = Decimal(1200)
# ADD 1 has no ON SIZE ERROR, so WS-TRANID-SUFFIX 9(06) and WS-RECORD-COUNT 9(09) wrap to zero.
TRANID_SUFFIX_MODULUS = 10**6
COUNTER_MODULUS = 10**9
ZERO_AMOUNT = Decimal("0.00")
# Receiver pictures: WS-MONTHLY-INT and WS-TOTAL-INT S9(09)V99, ACCT-CURR-BAL S9(10)V99.
INTEREST_INT_DIGITS = 9
BALANCE_INT_DIGITS = 10
AMOUNT_DEC_DIGITS = 2

# The ACCOUNT-RECORD fields CBACT04C uses: ACCT-ID (the description), ACCT-CURR-BAL (the
# rewrite) and ACCT-GROUP-ID (the DISCGRP key).
ACCOUNT_IMAGE_COLUMNS = (Account.acct_id, Account.curr_balance, Account.group_id)
# Rows per fetch of the server-side cursor that streams TCATBALF.
CATEGORY_BALANCE_FETCH_BATCH_SIZE = 500

# The DISPLAY texts of the mainline and its paragraphs.
START_LOG = "START OF EXECUTION OF PROGRAM %s"
END_LOG = "END OF EXECUTION OF PROGRAM %s: %d RECORDS, %d INTEREST TRANSACTIONS, %d REWRITES"
# DISPLAY TRAN-CAT-BAL-RECORD (line 193), each field after its CVTRA01Y name: run together or
# joined by spaces, the key and balance digits would form one chain of 16 or more digits, which
# the log masking filter hides but its last four.
RECORD_LOG = (
    "TRAN-CAT-BAL-RECORD TRANCAT-ACCT-ID=%s TRANCAT-TYPE-CD=%s TRANCAT-CD=%04d TRAN-CAT-BAL=%s"
)
GROUP_MISSING_LOG = "DISCLOSURE GROUP RECORD MISSING"
TRY_DEFAULT_LOG = "TRY WITH DEFAULT GROUP CODE"
ACCOUNT_NOT_FOUND_TEMPLATE = "ACCOUNT NOT FOUND: {acctId}"
ACCOUNT_READ_ERROR = "ERROR READING ACCOUNT FILE"
XREF_READ_ERROR = "ERROR READING XREF FILE"
DEFAULT_GROUP_READ_ERROR = "ERROR READING DEFAULT DISCLOSURE GROUP"
ACCOUNT_REWRITE_ERROR = "ERROR RE-WRITING ACCOUNT FILE"
# What 9910-DISPLAY-IO-STATUS shows for VSAM status 23, record not found.
NOT_FOUND_FILE_STATUS = "FILE STATUS IS: NNNN0023"
ABEND_LOG = "ABENDING PROGRAM"
ABEND_MESSAGE_TEMPLATE = "ABENDING PROGRAM {program}: {reason}"
ABEND_REASON_SEPARATOR = "; "

# Every failure that ends CBACT04C with 9999-ABEND-PROGRAM besides the not-found reads: a
# database error, a generation file error, and a record that cannot be encoded or written.
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

RECORDS_COUNT_KEY = "records"
INTEREST_TRANSACTIONS_COUNT_KEY = "interest_transactions"
ACCOUNTS_REWRITTEN_COUNT_KEY = "accounts_rewritten"


@dataclass(frozen=True)
class InterestFiles:
    """The files CBACT04C holds open while it runs.

    readConnection serves TCATBALF, XREFFILE, ACCTFILE and DISCGRP, and systranWriter is the
    TRANSACT DD, SYSTRAN(+1). Account rewrites take connections of their own from context.engine.
    """

    context: JobContext
    readConnection: Connection
    systranWriter: gdg.GenerationWriter


@dataclass
class InterestState:
    """CBACT04C working storage (WS-MISC-VARS, WS-COUNTERS) and the records last read.

    lastAcctId None stands for WS-LAST-ACCT-NUM VALUE SPACES. account is ACCOUNT-RECORD as read
    at the last change of account, and xrefCardNum that account's XREF-CARD-NUM.
    """

    lastAcctId: str | None = None
    firstTime: bool = True
    totalInterest: Decimal = ZERO_AMOUNT
    account: RowMapping | None = None
    xrefCardNum: str = ""
    tranIdSuffix: int = 0
    recordCount: int = 0
    interestTransactions: int = 0
    accountsRewritten: int = 0


def RunJob(context: JobContext) -> JobResult:
    """Run INTCALC: write SYSTRAN(+1) and rewrite the accounts with their interest (step 14).

    Returns EXIT_OK with the record, interest-transaction and rewrite counts, once the final
    transaction has recorded the run COMPLETED and cataloged the generation COMMITTED. Raises
    JobFailure with EXIT_ABEND on any abend: the rewrites committed before it stay, and no
    SYSTRAN generation is cataloged.
    """
    LOGGER.info(START_LOG, PROGRAM_NAME)
    try:
        result = CalculateInterest(context)
    except ABEND_ERRORS as error:
        raise AbendProgram((DescribeAbend(error),)) from error
    # No publication here: the run is already COMPLETED, so the chain (batch_chain.FinishStep)
    # publishes the COMMITTED generation outside its runner-failure mapping, and a publication
    # error there leaves the step COMPLETED for the next command's gdg.ReconcileCatalog.
    counts = result.counts
    LOGGER.info(
        END_LOG,
        PROGRAM_NAME,
        counts[RECORDS_COUNT_KEY],
        counts[INTEREST_TRANSACTIONS_COUNT_KEY],
        counts[ACCOUNTS_REWRITTEN_COUNT_KEY],
    )
    return result


def CalculateInterest(context: JobContext) -> JobResult:
    """The mainline, lines 180-232: open the files, process every balance, close the files.

    The run completion and the SYSTRAN catalog row commit together on finalConnection, after
    the last account rewrite has committed in its own unit (Technical Specification 0.7.2). The
    completion runs inside the generation block, so the COMMITTED row, inserted as the block
    exits, is the final write, and a failure there or in the close discards the .part file.
    """
    with context.engine.connect() as finalConnection:
        with gdg.NewGeneration(context, gdg.SYSTRAN, connection=finalConnection) as systranWriter:
            state = ProcessCategoryBalances(context, systranWriter)
            result = BuildInterestResult(state)
            run_ledger.FinishAtomicRun(finalConnection, context, result)
        finalConnection.commit()
    return result


def ProcessCategoryBalances(
    context: JobContext, systranWriter: gdg.GenerationWriter
) -> InterestState:
    """PERFORM UNTIL END-OF-FILE = 'Y', lines 188-222, over TCATBALF in key order."""
    state = InterestState()
    streamOptions = {"yield_per": CATEGORY_BALANCE_FETCH_BATCH_SIZE}
    with context.engine.connect() as readConnection:
        files = InterestFiles(context, readConnection, systranWriter)
        statement = CategoryBalancesInKeyOrder()
        with readConnection.execute(statement, execution_options=streamOptions) as balances:
            for balance in balances.mappings():
                ProcessCategoryBalance(files, state, balance)
    # Deliberately no 1050-UPDATE-ACCOUNT after the loop. The source tests END-OF-FILE before the
    # READ that sets it, so at end of file the inner IF skips processing, PERFORM UNTIL ends and
    # the outer ELSE that would rewrite the last account is never reached (cbl/CBACT04C.cbl
    # lines 187-221, 325-347). The last account keeps its balance and cycle fields, an empty
    # file rewrites nothing, and a single-account file rewrites nothing.
    return state


def CategoryBalancesInKeyOrder() -> Select:
    """READ TCATBAL-FILE (1000-TCATBALF-GET-NEXT, lines 325-348) in TRAN-CAT-KEY order.

    The key is TRANCAT-ACCT-ID, TRANCAT-TYPE-CD, TRANCAT-CD. The account ID is digits only, so
    its text order is its EBCDIC order, and TRANCAT-CD is a numeric 9(04). The type code has no
    stored sort column, so it is ordered by ebcdic_sort_key under the "C" collation, whose
    code-point order is the EBCDIC byte order.
    """
    balances = TransactionCategoryBalance
    typeSortKey = func.ebcdic_sort_key(balances.tran_type_cd).collate(KEY_COLLATION)
    return select(
        balances.acct_id, balances.tran_type_cd, balances.tran_cat_cd, balances.balance
    ).order_by(balances.acct_id, typeSortKey, balances.tran_cat_cd)


def ProcessCategoryBalance(files: InterestFiles, state: InterestState, balance: RowMapping) -> None:
    """One TRAN-CAT-BAL-RECORD of the mainline, lines 191-217."""
    state.recordCount = (state.recordCount + 1) % COUNTER_MODULUS
    LOGGER.debug(
        RECORD_LOG,
        balance["acct_id"],
        balance["tran_type_cd"],
        balance["tran_cat_cd"],
        balance["balance"],
    )
    if balance["acct_id"] != state.lastAcctId:
        StartAccount(files, state, balance["acct_id"])
    rate = ReadInterestRate(files.readConnection, state.account, balance)
    if rate != 0:
        ComputeInterest(files, state, balance["balance"], rate)
        ComputeFees()


def StartAccount(files: InterestFiles, state: InterestState, acctId: str) -> None:
    """The change-of-account branch, lines 194-206: rewrite the previous account, read the next."""
    if not state.firstTime:
        RewriteAccount(files.context, state)
    else:
        state.firstTime = False
    state.totalInterest = ZERO_AMOUNT
    state.lastAcctId = acctId
    state.account = ReadAccount(files.readConnection, acctId)
    state.xrefCardNum = ReadFirstXrefCard(files.readConnection, acctId)


def RewriteAccount(context: JobContext, state: InterestState) -> None:
    """1050-UPDATE-ACCOUNT, lines 350-370: add WS-TOTAL-INT to the balance, zero the cycle fields.

    REWRITE FD-ACCTFILE-REC FROM ACCOUNT-RECORD writes the image read at the change of account,
    in a unit of its own that locks the row and commits before the next account is read. A row
    that is gone (status 23) abends.
    """
    acctId = state.account["acct_id"]
    newBalance = AddInto(state.account["curr_balance"], state.totalInterest, BALANCE_INT_DIGITS)
    with context.engine.begin() as connection:
        if connection.execute(LockAccountStatement(acctId)).scalar_one_or_none() is None:
            raise AbendProgram((ACCOUNT_REWRITE_ERROR, NOT_FOUND_FILE_STATUS))
        connection.execute(RewriteAccountStatement(acctId, newBalance))
    state.accountsRewritten += 1


def LockAccountStatement(acctId: str) -> Select:
    """Return the SELECT ... FOR UPDATE that locks one account row for its rewrite."""
    return select(Account.acct_id).where(Account.acct_id == acctId).with_for_update()


def RewriteAccountStatement(acctId: str, newBalance: Decimal) -> Update:
    """Return the UPDATE of 1050-UPDATE-ACCOUNT: the new balance and both cycle fields at zero."""
    return (
        update(Account)
        .where(Account.acct_id == acctId)
        .values(
            curr_balance=newBalance,
            curr_cycle_credit=ZERO_AMOUNT,
            curr_cycle_debit=ZERO_AMOUNT,
        )
    )


def ReadAccount(readConnection: Connection, acctId: str) -> RowMapping:
    """1100-GET-ACCT-DATA, lines 372-391: READ ACCOUNT-FILE INTO ACCOUNT-RECORD by ACCT-ID."""
    statement = select(*ACCOUNT_IMAGE_COLUMNS).where(Account.acct_id == acctId)
    account = readConnection.execute(statement).mappings().one_or_none()
    if account is None:
        notFound = ACCOUNT_NOT_FOUND_TEMPLATE.format(acctId=acctId)
        raise AbendProgram((notFound, ACCOUNT_READ_ERROR, NOT_FOUND_FILE_STATUS))
    return account


def ReadFirstXrefCard(readConnection: Connection, acctId: str) -> str:
    """1110-GET-XREF-DATA, lines 393-413: READ XREF-FILE KEY IS FD-XREF-ACCT-ID.

    The XREFFIL1 path returns the account's first cross-reference in prime-key order, the EBCDIC
    order of the card number, which the stored, indexed cards.cards.card_num_sort column gives
    as every key-ordered read uses it; the card_xref foreign key guarantees the joined card.
    """
    statement = (
        select(CardXref.card_num)
        .join(Card, Card.card_num == CardXref.card_num)
        .where(CardXref.acct_id == acctId)
        .order_by(Card.card_num_sort)
        .limit(1)
    )
    cardNum = readConnection.execute(statement).scalar_one_or_none()
    if cardNum is None:
        notFound = ACCOUNT_NOT_FOUND_TEMPLATE.format(acctId=acctId)
        raise AbendProgram((notFound, XREF_READ_ERROR, NOT_FOUND_FILE_STATUS))
    return cardNum


def ReadInterestRate(
    readConnection: Connection, account: RowMapping, balance: RowMapping
) -> Decimal:
    """1200-GET-INTEREST-RATE, lines 415-440: DIS-INT-RATE for ACCT-GROUP-ID, type and category.

    Status 23 (INVALID KEY) displays the two retry lines and performs
    1200-A-GET-DEFAULT-INT-RATE. Stored text keys are right-trimmed, so trimmed equality is the
    space-padded key equality of the VSAM read.
    """
    groupId = str(account["group_id"]).rstrip(" ")
    rate = ReadDisclosureRate(readConnection, groupId, balance)
    if rate is not None:
        return rate
    LOGGER.info(GROUP_MISSING_LOG)
    LOGGER.info(TRY_DEFAULT_LOG)
    return ReadDefaultInterestRate(readConnection, balance)


def ReadDefaultInterestRate(readConnection: Connection, balance: RowMapping) -> Decimal:
    """1200-A-GET-DEFAULT-INT-RATE, lines 443-460: the DEFAULT group's rate, or an abend."""
    rate = ReadDisclosureRate(readConnection, DEFAULT_GROUP_ID, balance)
    if rate is None:
        raise AbendProgram((DEFAULT_GROUP_READ_ERROR, NOT_FOUND_FILE_STATUS))
    return rate


def ReadDisclosureRate(
    readConnection: Connection, groupId: str, balance: RowMapping
) -> Decimal | None:
    """READ DISCGRP-FILE by DIS-GROUP-KEY: groupId with the balance's type and category.

    Returns the DIS-INT-RATE, or None when no record has the key (status 23).
    """
    statement = select(DisclosureGroup.interest_rate).where(
        DisclosureGroup.group_id == groupId,
        DisclosureGroup.tran_type_cd == balance["tran_type_cd"],
        DisclosureGroup.tran_cat_cd == balance["tran_cat_cd"],
    )
    return readConnection.execute(statement).scalar_one_or_none()


def ComputeInterest(
    files: InterestFiles, state: InterestState, categoryBalance: Decimal, rate: Decimal
) -> None:
    """1300-COMPUTE-INTEREST, lines 462-470: the monthly interest, its total and its record.

    COMPUTE without ROUNDED truncates the quotient into WS-MONTHLY-INT S9(09)V99, and ADD
    stores the sum into WS-TOTAL-INT S9(09)V99.
    """
    with localcontext(COBOL_CONTEXT):
        quotient = categoryBalance * rate / INTEREST_DIVISOR
        monthlyInterest = StoreInto(quotient, INTEREST_INT_DIGITS, AMOUNT_DEC_DIGITS)
    state.totalInterest = AddInto(state.totalInterest, monthlyInterest, INTEREST_INT_DIGITS)
    WriteInterestTransaction(files, state, monthlyInterest)


def AddInto(augend: Decimal, addend: Decimal, intDigits: int) -> Decimal:
    """ADD addend TO augend, stored into a S9(intDigits)V99 receiver without ON SIZE ERROR."""
    with localcontext(COBOL_CONTEXT):
        return StoreInto(augend + addend, intDigits, AMOUNT_DEC_DIGITS)


def WriteInterestTransaction(
    files: InterestFiles, state: InterestState, monthlyInterest: Decimal
) -> None:
    """1300-B-WRITE-TX, lines 473-515: ADD 1 TO WS-TRANID-SUFFIX, build TRAN-RECORD, WRITE it.

    The suffix is incremented before the TRAN-ID is built, and 999999 wraps to 000000.
    """
    state.tranIdSuffix = (state.tranIdSuffix + 1) % TRANID_SUFFIX_MODULUS
    values = BuildInterestRecord(files.context, state, monthlyInterest)
    files.systranWriter.WriteRecord(EncodeRecord(values, TRANSACTION_LAYOUT))
    state.interestTransactions += 1


def BuildInterestRecord(
    context: JobContext, state: InterestState, monthlyInterest: Decimal
) -> dict[str, object]:
    """The TRAN-RECORD fields 1300-B-WRITE-TX moves, keyed by TRANSACTION_LAYOUT column name.

    STRING PARM-DATE WS-TRANID-SUFFIX gives TRAN-ID, and STRING 'Int. for a/c ' ACCT-ID the
    description; the rest of each field is spaces. Both timestamps take one
    Z-GET-DB2-FORMAT-TIMESTAMP value (lines 496-498, 613-626), the DB2 form "D".
    """
    timestamp = context.clock.Now()
    return {
        "tran_id": context.parameters.runDate + f"{state.tranIdSuffix:06d}",
        "tran_type_cd": INTEREST_TRAN_TYPE_CD,
        "tran_cat_cd": INTEREST_TRAN_CAT_CD,
        "tran_source": INTEREST_TRAN_SOURCE,
        "tran_desc": INTEREST_DESC_PREFIX + state.account["acct_id"],
        "tran_amt": monthlyInterest,
        "merchant_id": ZERO_MERCHANT_ID,
        "merchant_name": BLANK_MERCHANT_FIELD,
        "merchant_city": BLANK_MERCHANT_FIELD,
        "merchant_zip": BLANK_MERCHANT_FIELD,
        "card_num": state.xrefCardNum,
        "orig_ts": timestamp,
        "orig_ts_format": TIMESTAMP_FORMAT_DB2,
        "proc_ts": timestamp,
        "proc_ts_format": TIMESTAMP_FORMAT_DB2,
    }


def ComputeFees() -> None:
    """1400-COMPUTE-FEES, lines 518-520, whose only content is the comment "To be implemented".

    CBACT04C computes no fee, and the port keeps it so: the fee logic stays out of scope because
    the modernization preserves business rules as the source has them.
    """


def BuildInterestResult(state: InterestState) -> JobResult:
    """Return EXIT_OK with the counts batch.job_runs records for the run."""
    counts = {
        RECORDS_COUNT_KEY: state.recordCount,
        INTEREST_TRANSACTIONS_COUNT_KEY: state.interestTransactions,
        ACCOUNTS_REWRITTEN_COUNT_KEY: state.accountsRewritten,
    }
    return JobResult(EXIT_OK, counts)


def AbendProgram(displayLines: tuple[str, ...]) -> JobFailure:
    """9999-ABEND-PROGRAM, lines 628-632: DISPLAY the failure, then CEE3ABD abend 999 (exit 16).

    Returns the JobFailure for the caller to raise, so each raise site reads as the PERFORM.
    """
    for displayLine in displayLines:
        LOGGER.error("%s", displayLine)
    LOGGER.error(ABEND_LOG)
    reason = ABEND_REASON_SEPARATOR.join(displayLines)
    message = ABEND_MESSAGE_TEMPLATE.format(program=PROGRAM_NAME, reason=reason)
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
