"""Replaces: COBIL00C (CB00), BMS COBIL00, copybooks CVACT01Y, CVACT03Y, CVTRA05Y, CSDAT01Y.

The online bill payment. ENTER (PROCESS-ENTER-KEY) edits the account ID and the confirm value
in first-failure mode, reads the account for update, and either asks for confirmation or pays
the whole balance: it writes a BILL PAYMENT - ONLINE transaction and rewrites the account with
the balance reduced by the amount paid.

The payment runs every step of lines 210-235 whatever the earlier steps returned, as the source
does: the cross-reference read, the TRAN-ID allocation, the WRITE, the COMPUTE and the REWRITE.
Each failing step records its literal and the last literal recorded is the response, so a
duplicate-key WRITE still reduces and rewrites the balance, and a failed REWRITE after a good
WRITE keeps the transaction. A failed TRAN-ID allocation leaves the record without a key, so its
WRITE fails and the COMPUTE and the REWRITE still run. The account write unit is held from the
READ UPDATE to the REWRITE, while the transaction is allocated, inserted and committed in a unit
of its own first.

Legacy paths with no code here: ``EIBCALEN = 0`` is the session guard and its 401; PF3
(``XCTL`` to the menu) and PF4 (CLEAR-CURRENT-SCREEN) are frontend navigation and form clearing;
the invalid-key branch cannot be reached through the API; POPULATE-HEADER-INFO is the frontend
ScreenShell.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from types import MappingProxyType
from typing import Any, NoReturn

from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import WriteUnit
from app.core.dependencies import OnlineContext
from app.core.errors import (
    HTTP_EDIT_FAILURE,
    HTTP_FILE_ERROR,
    HTTP_NOT_FOUND,
    HTTP_RECORD_CHANGED,
    DuplicateKeyError,
    LegacyMessageError,
    LogFileCondition,
    RecordNotFoundError,
    RepositoryError,
)
from app.repositories.account_repository import ACCOUNT_FILE_NAME, AccountRepository
from app.repositories.card_xref_repository import CardXrefRepository, DuplicateAlternateKeyError
from app.repositories.transaction_repository import TRANSACTION_FILE_NAME, TransactionRepository
from app.schemas.billpay import BillPayRequest, BillPayResponse
from app.utils.decimal_utils import COBOL_CONTEXT, StoreInto
from app.utils.record_layouts import TIMESTAMP_FORMAT_SPACE

LOGGER = logging.getLogger(__name__)

# WS-TRANID (line 38).
TRANSACTION_ID = "CB00"
# The CICS commands of READ-ACCTDAT-FILE, UPDATE-ACCTDAT-FILE, READ-CXACAIX-FILE, the
# STARTBR/READPREV of the TRAN-ID allocation and WRITE-TRANSACT-FILE, as their logged file
# conditions name them.
READ_UPDATE_OPERATION = "READ UPDATE"
REWRITE_OPERATION = "REWRITE"
READ_OPERATION = "READ"
BROWSE_BACKWARD_OPERATION = "STARTBR/READPREV"
WRITE_OPERATION = "WRITE"

# The WS-MESSAGE literals, verbatim (source lines in brackets).
ACCT_ID_EMPTY_MESSAGE = "Acct ID can NOT be empty..."  # [161]
INVALID_CONFIRM_MESSAGE = "Invalid value. Valid values are (Y/N)..."  # [187]
ACCOUNT_NOT_FOUND_MESSAGE = "Account ID NOT found..."  # [361, 392, 425]
ACCOUNT_LOOKUP_FAILED_MESSAGE = "Unable to lookup Account..."  # [368]
NOTHING_TO_PAY_MESSAGE = "You have nothing to pay..."  # [201]
CONFIRM_PAYMENT_MESSAGE = "Confirm to make a bill payment..."  # [237]
XREF_LOOKUP_FAILED_MESSAGE = "Unable to lookup XREF AIX file..."  # [432]
TRANSACTION_LOOKUP_FAILED_MESSAGE = "Unable to lookup Transaction..."  # [463, 492]
TRAN_ID_EXISTS_MESSAGE = "Tran ID already exist..."  # [536]
ADD_PAYMENT_FAILED_MESSAGE = "Unable to Add Bill pay Transaction..."  # [543]
UPDATE_ACCOUNT_FAILED_MESSAGE = "Unable to Update Account..."  # [399]
# The STRING of lines 527-530: both parts DELIMITED BY SIZE, so two spaces follow the stop.
PAYMENT_SUCCESS_PREFIX = "Payment successful. "
PAYMENT_SUCCESS_ID_LABEL = " Your Transaction ID is "
PAYMENT_SUCCESS_SUFFIX = "."

# The payment record of lines 219-229, keyed by transactions.transactions column name.
PAYMENT_TYPE_CD = "02"
PAYMENT_CAT_CD = 2
PAYMENT_SOURCE = "POS TERM"
PAYMENT_DESC = "BILL PAYMENT - ONLINE"
PAYMENT_MERCHANT_ID = "999999999"
PAYMENT_MERCHANT_NAME = "BILL PAYMENT"
PAYMENT_MERCHANT_CITY = "N/A"
PAYMENT_MERCHANT_ZIP = "N/A"
PAYMENT_RECORD_FIXED_VALUES = MappingProxyType(
    {
        "tran_type_cd": PAYMENT_TYPE_CD,
        "tran_cat_cd": PAYMENT_CAT_CD,
        "tran_source": PAYMENT_SOURCE,
        "tran_desc": PAYMENT_DESC,
        "merchant_id": PAYMENT_MERCHANT_ID,
        "merchant_name": PAYMENT_MERCHANT_NAME,
        "merchant_city": PAYMENT_MERCHANT_CITY,
        "merchant_zip": PAYMENT_MERCHANT_ZIP,
    }
)

# The EVALUATE CONFIRMI of lines 173-191.
CONFIRM_YES_VALUES = ("Y", "y")
CONFIRM_NO_VALUES = ("N", "n")
# The BillPayRequest fields that replace MOVE -1 TO ACTIDINL and MOVE -1 TO CONFIRML.
CURSOR_ACCT_ID = "acct_id"
CURSOR_CONFIRM = "confirm"
# The padding a BMS input field carries.
FIELD_PADDING = " "

# TRAN-AMT S9(09)V99 and ACCT-CURR-BAL S9(10)V99.
TRAN_AMT_INT_DIGITS = 9
ACCT_BALANCE_INT_DIGITS = 10
MONEY_DEC_DIGITS = 2
ZERO_BALANCE = Decimal(0)
BALANCE_COLUMN = "curr_balance"
CARD_NUM_COLUMN = "card_num"

# The status of a screen sent without ERR-FLG set; anything from 400 up is a legacy error body.
HTTP_OK = 200
ERROR_STATUS_FLOOR = 400


@dataclass
class PaymentState:
    """The working storage one ENTER carries through PROCESS-ENTER-KEY.

    ``account`` is ACCOUNT-RECORD as read for update; after the COMPUTE it holds the new
    balance. ``screenBalance`` is what lines 193-194 moved to CURBAL, which the COMPUTE never
    updates. ``xrefCardNum`` keeps the record area's initial blank until CXACAIX returns a row:
    the account's only record on NORMAL, or the first in AIX order on DUPKEY.
    ``message``, ``messageStatus`` and ``cursorField`` hold the last literal recorded.
    ``screenCleared`` is set once INITIALIZE-ALL-FIELDS ran, and ``rewriteStarted`` tells a
    REWRITE failure leaving the account unit from a failed READ UPDATE.
    """

    acctId: str
    payRequested: bool
    account: dict[str, Any] | None = None
    screenBalance: Decimal | None = None
    xrefCardNum: str = ""
    tranId: str | None = None
    tranAmt: Decimal = Decimal("0.00")
    message: str = ""
    messageStatus: int = HTTP_OK
    cursorField: str = CURSOR_ACCT_ID
    screenCleared: bool = False
    rewriteStarted: bool = False


async def PayBill(context: OnlineContext, request: BillPayRequest) -> BillPayResponse:
    """PROCESS-ENTER-KEY (lines 154-244): the CB00 ENTER key, ``POST /api/v1/billpay``.

    Raises:
        LegacyMessageError: the legacy error body when the last literal set is a failure:
            400 edit failures, 404 not found, 409 duplicate TRAN-ID, 503 file errors.
    """
    state = EditScreenInput(request)
    if state is None:
        return ClearedScreenResponse()
    await ProcessAccount(context, state)
    return BuildOutcome(state)


def IsBlank(text: str) -> bool:
    """Return True for ``SPACES OR LOW-VALUES``: an empty field or one holding only spaces."""
    return not text.strip(FIELD_PADDING)


def EditScreenInput(request: BillPayRequest) -> PaymentState | None:
    """PROCESS-ENTER-KEY lines 156-191: the account presence edit, then the confirm value.

    Returns None for ``N``/``n`` (CLEAR-CURRENT-SCREEN). The account ID is kept as keyed,
    without its trailing padding: it is the raw ACCT-ID and XREF-ACCT-ID key, never zero-padded
    or checked for digits, so any other text is simply not found.
    """
    if IsBlank(request.acct_id):
        RaiseScreenError(ACCT_ID_EMPTY_MESSAGE, CURSOR_ACCT_ID, HTTP_EDIT_FAILURE)
    acctId = request.acct_id.rstrip(FIELD_PADDING)
    if request.confirm in CONFIRM_YES_VALUES:
        return PaymentState(acctId=acctId, payRequested=True)
    if request.confirm in CONFIRM_NO_VALUES:
        return None
    if IsBlank(request.confirm):
        return PaymentState(acctId=acctId, payRequested=False)
    RaiseScreenError(INVALID_CONFIRM_MESSAGE, CURSOR_CONFIRM, HTTP_EDIT_FAILURE)


async def ProcessAccount(context: OnlineContext, state: PaymentState) -> None:
    """READ-ACCTDAT-FILE (``READ ... UPDATE``, line 351) to UPDATE-ACCTDAT-FILE in one unit.

    The read always locks, on the confirm prompt too; that unit ends with no write, releasing
    the lock as the task's RETURN did. A LegacyMessageError or a task cancellation leaves the
    unit rolled back and propagates; only file conditions are handled here.
    """
    accountRepository = AccountRepository(context)
    try:
        async with WriteUnit(context.engine, ACCOUNT_FILE_NAME) as accountUnit:
            await ReadAccountForUpdate(accountRepository, accountUnit, state)
            CheckBalanceAndConfirm(state)
            if state.payRequested:
                await RunPaymentSequence(context, accountRepository, accountUnit, state)
    except RepositoryError as error:
        HandleAccountUnitError(state, error)


def HandleAccountUnitError(state: PaymentState, error: RepositoryError) -> None:
    """Map a file condition that ended the account unit to its COBIL00C literal.

    Before the REWRITE it is the READ UPDATE ``WHEN OTHER`` branch (lines 365-371): a lock
    timeout, an I/O error, a unit that cannot open, or the batch window's NOTOPEN, as ACCTDAT
    is the first window file CB00 touches. From the REWRITE on, NOTFND (lines 390-395) and any
    other condition, a failed commit included (lines 396-402), are recorded, not raised.

    Raises:
        LegacyMessageError: 503 ``Unable to lookup Account...`` for a failed read.
    """
    if not state.rewriteStarted:
        LogFileCondition(LOGGER, TRANSACTION_ID, READ_UPDATE_OPERATION, error)
        raise ScreenError(ACCOUNT_LOOKUP_FAILED_MESSAGE, CURSOR_ACCT_ID, HTTP_FILE_ERROR) from error
    if isinstance(error, RecordNotFoundError):
        RecordMessage(state, ACCOUNT_NOT_FOUND_MESSAGE, HTTP_NOT_FOUND)
        return
    LogFileCondition(LOGGER, TRANSACTION_ID, REWRITE_OPERATION, error)
    RecordMessage(state, UPDATE_ACCOUNT_FAILED_MESSAGE, HTTP_FILE_ERROR)


async def ReadAccountForUpdate(
    accountRepository: AccountRepository, accountUnit: AsyncConnection, state: PaymentState
) -> None:
    """READ-ACCTDAT-FILE (lines 343-372) and the CURBAL moves of lines 193-194.

    MOVE ACCT-CURR-BAL TO WS-CURR-BAL (``+9999999999.99``) holds every digit, so the screen
    balance is the stored balance unchanged.

    Raises:
        LegacyMessageError: 404 ``Account ID NOT found...``, sent before CURBAL is filled.
    """
    account = await accountRepository.GetAccountForUpdate(accountUnit, state.acctId)
    if account is None:
        RaiseScreenError(ACCOUNT_NOT_FOUND_MESSAGE, CURSOR_ACCT_ID, HTTP_NOT_FOUND)
    state.account = account
    state.screenBalance = account[BALANCE_COLUMN]


def CheckBalanceAndConfirm(state: PaymentState) -> None:
    """Lines 197-206 and 236-240: refuse a balance not above zero, else prompt unless ``Y``.

    The prompt sets no ERR-FLG: it is a screen sent with HTTP 200 and the balance.

    Raises:
        LegacyMessageError: 400 ``You have nothing to pay...``.
    """
    if state.screenBalance <= ZERO_BALANCE:
        RaiseScreenError(NOTHING_TO_PAY_MESSAGE, CURSOR_ACCT_ID, HTTP_EDIT_FAILURE)
    if not state.payRequested:
        RecordMessage(state, CONFIRM_PAYMENT_MESSAGE, HTTP_OK, CURSOR_CONFIRM)


async def RunPaymentSequence(
    context: OnlineContext,
    accountRepository: AccountRepository,
    accountUnit: AsyncConnection,
    state: PaymentState,
) -> None:
    """The ``IF CONF-PAY-YES`` branch (lines 210-235): every step runs, the last literal wins.

    No ERR-FLG test sits between the steps, so no failure skips a later one. The transaction
    unit commits before the COMPUTE, and the account unit commits when ProcessAccount leaves it.
    """
    await LookupXref(context, state)
    # MOVE ACCT-CURR-BAL TO TRAN-AMT: S9(10)V99 into S9(09)V99 drops the high-order digit.
    state.tranAmt = StoreInto(state.account[BALANCE_COLUMN], TRAN_AMT_INT_DIGITS, MONEY_DEC_DIGITS)
    await WritePaymentTransaction(context, state)
    ReduceBalance(state)
    state.rewriteStarted = True
    await RewriteAccount(accountRepository, accountUnit, state)


async def LookupXref(context: OnlineContext, state: PaymentState) -> None:
    """READ-CXACAIX-FILE (lines 408-436): the account's cross-reference, for its card number.

    A failure records its literal and the sequence goes on. DUPKEY is a WHEN OTHER failure, but
    READ ... INTO(CARD-XREF-RECORD) has already filled XREF-CARD-NUM from the first record in
    AIX order, so the WRITE carries that card and the literal of its outcome replaces this one.
    Any other failure leaves the card number at the record area's initial blank, so the insert
    outcome is whatever the database returns.
    """
    try:
        xref = await CardXrefRepository(context).GetXrefByAccount(state.acctId)
    except DuplicateAlternateKeyError as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, READ_OPERATION, error)
        RecordMessage(state, XREF_LOOKUP_FAILED_MESSAGE, HTTP_FILE_ERROR)
        state.xrefCardNum = error.firstRecord[CARD_NUM_COLUMN]
        return
    except RepositoryError as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, READ_OPERATION, error)
        RecordMessage(state, XREF_LOOKUP_FAILED_MESSAGE, HTTP_FILE_ERROR)
        return
    if xref is None:
        RecordMessage(state, ACCOUNT_NOT_FOUND_MESSAGE, HTTP_NOT_FOUND)
        return
    state.xrefCardNum = xref[CARD_NUM_COLUMN]


async def WritePaymentTransaction(context: OnlineContext, state: PaymentState) -> None:
    """STARTBR/READPREV/ENDBR, ADD 1 and WRITE-TRANSACT-FILE (lines 212-233, 441-547).

    The TRAN-ID is allocated and the record inserted in one TRANSACT unit, committed on exit.
    The WRITE is attempted whatever the allocation returned. A failed allocation leaves the
    record without a key, so its WRITE fails as well. A failed WRITE is handled outside the
    unit, after its rollback, because a failed statement aborts the unit's transaction.
    """
    transactionRepository = TransactionRepository(context)
    try:
        async with WriteUnit(context.engine, TRANSACTION_FILE_NAME) as transactionUnit:
            await AllocatePaymentTranId(transactionRepository, transactionUnit, state)
            await transactionRepository.InsertTransaction(
                transactionUnit, BuildPaymentRecord(state, CurrentTimestamp())
            )
    except RepositoryError as error:
        HandleTransactionWriteError(state, error)
        return
    RecordPaymentSuccess(state)


async def AllocatePaymentTranId(
    transactionRepository: TransactionRepository,
    transactionUnit: AsyncConnection,
    state: PaymentState,
) -> None:
    """STARTBR HIGH-VALUES, READPREV, ENDBR and ADD 1 (lines 212-217) into ``state.tranId``.

    A failed browse is the STARTBR or READPREV ``WHEN OTHER`` branch (lines 460-466, 489-495):
    it records ``Unable to lookup Transaction...`` and the sequence goes on, with ``tranId``
    left unset. No TRAN-ID is made up, so the record's WRITE then fails in its own right.
    """
    try:
        state.tranId = await transactionRepository.AllocateNextTranId(transactionUnit)
    except RepositoryError as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, BROWSE_BACKWARD_OPERATION, error)
        RecordMessage(state, TRANSACTION_LOOKUP_FAILED_MESSAGE, HTTP_FILE_ERROR)


def HandleTransactionWriteError(state: PaymentState, error: RepositoryError) -> None:
    """Record the literal of a failed WRITE; the COMPUTE and the REWRITE still run after it.

    DUPKEY/DUPREC (lines 533-539) gives ``Tran ID already exist...``. Any other condition
    (WHEN OTHER, lines 540-546) gives ``Unable to Add Bill pay Transaction...``: a TRANSACT
    unit that cannot open or commit, and the WRITE of a record a failed allocation left
    without a key, included.
    """
    if isinstance(error, DuplicateKeyError):
        RecordMessage(state, TRAN_ID_EXISTS_MESSAGE, HTTP_RECORD_CHANGED)
        return
    LogFileCondition(LOGGER, TRANSACTION_ID, WRITE_OPERATION, error)
    RecordMessage(state, ADD_PAYMENT_FAILED_MESSAGE, HTTP_FILE_ERROR)


def BuildPaymentRecord(state: PaymentState, timestamp: datetime) -> dict[str, Any]:
    """INITIALIZE TRAN-RECORD and the moves of lines 218-232, keyed by column name.

    Both timestamps are the CSDAT01Y WS-TIMESTAMP text form ``YYYY-MM-DD HH:MM:SS.000000``,
    stored as ``timestamp`` with format code S.
    """
    return {
        **PAYMENT_RECORD_FIXED_VALUES,
        "tran_id": state.tranId,
        "tran_amt": state.tranAmt,
        "card_num": state.xrefCardNum,
        "orig_ts": timestamp,
        "proc_ts": timestamp,
        "orig_ts_format": TIMESTAMP_FORMAT_SPACE,
        "proc_ts_format": TIMESTAMP_FORMAT_SPACE,
    }


def CurrentTimestamp() -> datetime:
    """GET-CURRENT-TIMESTAMP (lines 249-267): server local time in whole seconds.

    ASKTIME/FORMATTIME give the date and HH:MM:SS, and WS-TIMESTAMP-TM-MS6 is zeroed.
    """
    return datetime.now().replace(microsecond=0)


def RecordPaymentSuccess(state: PaymentState) -> None:
    """WRITE-TRANSACT-FILE NORMAL (lines 523-532): INITIALIZE-ALL-FIELDS and the success STRING.

    TRAN-ID is DELIMITED BY SPACE, so only its characters before a first space are shown.
    """
    state.screenCleared = True
    tranIdShown = (state.tranId or "").partition(FIELD_PADDING)[0]
    message = PAYMENT_SUCCESS_PREFIX + PAYMENT_SUCCESS_ID_LABEL + tranIdShown
    RecordMessage(state, message + PAYMENT_SUCCESS_SUFFIX, HTTP_OK)


def ReduceBalance(state: PaymentState) -> None:
    """COMPUTE ACCT-CURR-BAL = ACCT-CURR-BAL - TRAN-AMT (line 234), stored into S9(10)V99.

    CURBAL is not moved again, so the screen balance stays the one read.
    """
    with localcontext(COBOL_CONTEXT):
        newBalance = StoreInto(
            state.account[BALANCE_COLUMN] - state.tranAmt,
            ACCT_BALANCE_INT_DIGITS,
            MONEY_DEC_DIGITS,
        )
    state.account = {**state.account, BALANCE_COLUMN: newBalance}


async def RewriteAccount(
    accountRepository: AccountRepository, accountUnit: AsyncConnection, state: PaymentState
) -> None:
    """UPDATE-ACCTDAT-FILE (lines 377-403): REWRITE FROM ACCOUNT-RECORD in the account unit.

    The record is written as read, with only the balance changed.
    """
    await accountRepository.UpdateAccount(accountUnit, state.account)


def RecordMessage(
    state: PaymentState, message: str, httpStatus: int, cursorField: str = CURSOR_ACCT_ID
) -> None:
    """Set WS-MESSAGE, its HTTP status and the cursor; the last literal recorded is sent.

    Every failure literal of COBIL00C moves -1 to ACTIDINL; only the confirm prompt and the
    invalid confirm value put the cursor on CONFIRM.
    """
    state.message = message
    state.messageStatus = httpStatus
    state.cursorField = cursorField


def BuildOutcome(state: PaymentState) -> BillPayResponse:
    """The final SEND-BILLPAY-SCREEN (line 242): the last literal recorded.

    A screen sent with HTTP 200 carries CURBAL, the balance read unless INITIALIZE-ALL-FIELDS
    cleared the screen. A failure is the standard four-key legacy error body.

    Raises:
        LegacyMessageError: when the last literal recorded is a failure (404, 409 or 503).
    """
    if state.messageStatus >= ERROR_STATUS_FLOOR:
        RaiseScreenError(state.message, state.cursorField, state.messageStatus)
    return BillPayResponse(
        current_balance=None if state.screenCleared else state.screenBalance,
        message=state.message,
        cursor_field=state.cursorField,
        tran_id=state.tranId,
    )


def ClearedScreenResponse() -> BillPayResponse:
    """CLEAR-CURRENT-SCREEN (lines 552-566) for ``N``/``n``: every field and the message blank."""
    return BillPayResponse(current_balance=None, message="", cursor_field=CURSOR_ACCT_ID)


def ScreenError(message: str, field: str, httpStatus: int) -> LegacyMessageError:
    """Return the legacy error of a screen sent with ERR-FLG set: the four standard keys only."""
    return LegacyMessageError(message, field=field, httpStatus=httpStatus)


def RaiseScreenError(message: str, field: str, httpStatus: int) -> NoReturn:
    """Raise the legacy error of a screen sent with ERR-FLG set: the four standard keys only.

    Raises:
        LegacyMessageError: always, with ``message``, the cursor ``field`` and ``httpStatus``.
    """
    raise ScreenError(message, field, httpStatus)
