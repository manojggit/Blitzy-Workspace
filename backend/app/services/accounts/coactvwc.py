"""Replaces: COACTVWC (CAVW), BMS COACTVW, copybooks CVACT01Y, CVACT02Y, CVACT03Y, CVCUS01Y,
CVCRD01Y, CSMSG02Y

The CAVW account view, served as ``GET /api/v1/accounts/{acctId}``. The account key is edited
as 2200-EDIT-MAP-INPUTS edits ACCTSID, then 9000-READ-ACCT reads the card cross-reference
through CXACAIX, the account master ACCTDAT and the customer master CUSTDAT, and the screen is
built as 1200-SETUP-SCREEN-VARS fills COACTVW, with the SSN masked.

COACTUPC's 9000-READ-ACCT and its 9200, 9300 and 9400 paragraphs are textually the same, so
coactupc.py reuses ``ReadAccountChain``, ``AccountReadResult``, the not-found and file-error
message builders and ``FormatRespCode`` from here.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.core.database import RecoverRequestSession
from app.core.dependencies import OnlineContext
from app.core.errors import (
    HTTP_EDIT_FAILURE,
    HTTP_FILE_ERROR,
    HTTP_NOT_FOUND,
    LegacyErrorDetails,
    LegacyMessageError,
    LogFileCondition,
    RepositoryError,
)
from app.core.security import MaskSsn
from app.repositories.account_repository import ACCOUNT_FILE_NAME, AccountRepository
from app.repositories.card_xref_repository import XREF_ACCOUNT_PATH_NAME, CardXrefRepository
from app.repositories.customer_repository import CUSTOMER_FILE_NAME, CustomerRepository
from app.schemas.accounts import AccountViewResponse

LOGGER = logging.getLogger(__name__)

# LIT-THISPGM and LIT-THISTRANID (lines 143-146).
PROGRAM_NAME = "COACTVWC"
TRANSACTION_ID = "CAVW"

# BMS COACTVW ACCTSID LENGTH=11, received into CC-ACCT-ID X(11) (copybook CVCRD01Y).
ACCOUNT_KEY_LENGTH = 11
# WS-RETURN-MSG PIC X(75) (line 117): every STRING or MOVE into it stops at 75 characters.
RETURN_MESSAGE_LENGTH = 75
# DFHRESP(NOTFND). The repositories return None for a missing record, so the service supplies
# the RESP and RESP2 values the not-found builders show.
NOTFND_RESP = 13
NOTFND_RESP2 = 0
# The ERROR-OPNAME every 9200, 9300 and 9400 WHEN OTHER branch moves (lines 762, 812, 861).
FILE_ERROR_OPERATION_READ = "READ"

# WS-RETURN-MSG values set by an active statement on a reachable path (lines 121-124, 672).
# The other 88-level texts of WS-RETURN-MSG are never set, so they are never emitted.
MSG_ACCOUNT_NOT_PROVIDED = "Account number not provided"
MSG_NO_INPUT_RECEIVED = "No input received"
# Two spaces between "must" and "be", as in the source literal.
MSG_ACCOUNT_FILTER_INVALID = "Account Filter must  be a non-zero 11 digit number"
# WS-INFO-MSG 88 WS-PROMPT-FOR-INPUT (lines 113-114): the information line of every response.
INFO_PROMPT_FOR_INPUT = "Enter or update id of account to display"

# The schemas.accounts attribute name of the ACCTSID input, for field_errors and cursor_field.
ACCOUNT_ID_FIELD = "acct_id"
# 2200-EDIT-MAP-INPUTS: ACCTSID holding '*' (space-padded) is treated as not entered.
BLANK_INPUT_MARKER = "*"
# 2210-EDIT-ACCOUNT ``CC-ACCT-ID EQUAL ZEROES``.
ZERO_ACCOUNT_KEY = "0" * ACCOUNT_KEY_LENGTH
# WS-EDIT-ACCT-FLAG 88 values (lines 58-61).
FLAG_NOT_OK = "0"
FLAG_VALID = "1"
FLAG_BLANK = " "
# The status of a read chain that left WS-RETURN-MSG off.
HTTP_OK = 200

# WS-FILE-ERROR-MESSAGE (lines 86-105): its fixed literals and the widths of its variable parts.
FILE_ERROR_PREFIX = "File Error: "
FILE_ERROR_ON = " on "
FILE_ERROR_RETURNED = " returned RESP "
FILE_ERROR_RESP2 = ",RESP2 "
FILE_ERROR_TRAILER = "     "
ERROR_OPNAME_LENGTH = 8
ERROR_FILE_LENGTH = 9
ERROR_RESP_LENGTH = 10
# WS-RESP-CD and WS-REAS-CD are S9(09) COMP: the MOVE into X(10) shows nine unsigned digits.
RESP_CODE_MODULUS = 10**9

# The STRING literals of the three NOTFND builders (lines 747-757, 796-806, 846-856).
XREF_NOT_FOUND_TEXT = " not found in Cross ref file.  Resp:"
ACCOUNT_NOT_FOUND_TEXT = " not found in Acct Master file.Resp:"
CUSTOMER_NOT_FOUND_TEXT = " not found in customer master.Resp: "
ACCOUNT_KEY_LABEL = "Account:"
CUSTOMER_KEY_LABEL = "CustId:"
ACCOUNT_REASON_LABEL = " Reas:"
CUSTOMER_REASON_LABEL = " REAS:"

# 1200-SETUP-SCREEN-VARS moves (lines 471-523): account columns shown as stored, account
# DATE columns shown as YYYY-MM-DD text, and customer columns shown as stored.
ACCOUNT_DISPLAY_COLUMNS = (
    "active_status",
    "curr_balance",
    "credit_limit",
    "cash_credit_limit",
    "curr_cycle_credit",
    "curr_cycle_debit",
    "group_id",
)
ACCOUNT_DATE_COLUMNS = ("open_date", "expiration_date", "reissue_date")
CUSTOMER_DISPLAY_COLUMNS = (
    "cust_id",
    "fico_credit_score",
    "first_name",
    "middle_name",
    "last_name",
    "addr_line_1",
    "addr_line_2",
    "addr_line_3",
    "addr_state_cd",
    "addr_zip",
    "addr_country_cd",
    "phone_num_1",
    "phone_num_2",
    "govt_issued_id",
    "eft_account_id",
    "primary_holder_ind",
)


def FitReturnMessage(text: str) -> str:
    """Return text as it fits WS-RETURN-MSG X(75): a STRING or MOVE stops when it is full."""
    return text[:RETURN_MESSAGE_LENGTH]


def FormatRespCode(code: int) -> str:
    """Port of ``MOVE WS-RESP-CD TO ERROR-RESP``: S9(09) COMP into X(10).

    The receiver shows the unsigned value in nine digits, left-justified, then one space:
    13 gives ``000000013 `` and 0 gives ``000000000 ``.
    """
    return f"{abs(code) % RESP_CODE_MODULUS:09d}".ljust(ERROR_RESP_LENGTH)


def BuildFileErrorMessage(operation: str, fileName: str, resp: int, resp2: int) -> str:
    """Port of WS-FILE-ERROR-MESSAGE (lines 86-105) moved into WS-RETURN-MSG.

    The operation and file name are left-justified in X(8) and X(9), the codes are shown as
    ERROR-RESP and ERROR-RESP2, and the 80-byte group is cut to 75 and right-trimmed. During
    the batch window, ``("READ", "CXACAIX", 19, 60)`` gives
    ``File Error: READ     on CXACAIX   returned RESP 000000019 ,RESP2 000000060``.
    """
    fullMessage = (
        FILE_ERROR_PREFIX
        + operation.ljust(ERROR_OPNAME_LENGTH)[:ERROR_OPNAME_LENGTH]
        + FILE_ERROR_ON
        + fileName.ljust(ERROR_FILE_LENGTH)[:ERROR_FILE_LENGTH]
        + FILE_ERROR_RETURNED
        + FormatRespCode(resp)
        + FILE_ERROR_RESP2
        + FormatRespCode(resp2)
        + FILE_ERROR_TRAILER
    )
    return FitReturnMessage(fullMessage).rstrip(" ")


def BuildNotFoundMessage(keyLabel: str, keyText: str, fileText: str, reasonLabel: str) -> str:
    """The shared STRING of the three NOTFND builders, with the NOTFND RESP and RESP2 codes.

    The pieces are joined DELIMITED BY SIZE into WS-RETURN-MSG, so the text stops at 75
    characters; the X(75) padding is right-trimmed for display.
    """
    fullMessage = (
        keyLabel
        + keyText
        + fileText
        + FormatRespCode(NOTFND_RESP)
        + reasonLabel
        + FormatRespCode(NOTFND_RESP2)
    )
    return FitReturnMessage(fullMessage).rstrip(" ")


def BuildXrefNotFoundMessage(acctId: str) -> str:
    """Port of the 9200-GETCARDXREF-BYACCT NOTFND message (lines 747-757).

    For account 00000000099 it is
    ``Account:00000000099 not found in Cross ref file.  Resp:000000013  Reas:0000``.
    """
    return BuildNotFoundMessage(
        ACCOUNT_KEY_LABEL, acctId, XREF_NOT_FOUND_TEXT, ACCOUNT_REASON_LABEL
    )


def BuildAccountNotFoundMessage(acctId: str) -> str:
    """Port of the 9300-GETACCTDATA-BYACCT NOTFND message (lines 796-806).

    For account 00000000099 it is
    ``Account:00000000099 not found in Acct Master file.Resp:000000013  Reas:0000``.
    """
    return BuildNotFoundMessage(
        ACCOUNT_KEY_LABEL, acctId, ACCOUNT_NOT_FOUND_TEXT, ACCOUNT_REASON_LABEL
    )


def BuildCustomerNotFoundMessage(custId: str) -> str:
    """Port of the 9400-GETCUSTDATA-BYCUST NOTFND message (lines 846-856).

    For customer 000000099 it is
    ``CustId:000000099 not found in customer master.Resp: 000000013  REAS:0000000``.
    """
    return BuildNotFoundMessage(
        CUSTOMER_KEY_LABEL, custId, CUSTOMER_NOT_FOUND_TEXT, CUSTOMER_REASON_LABEL
    )


def IsBlankScreenInput(text: str) -> bool:
    """Port of the 2200-EDIT-MAP-INPUTS ``'*' OR SPACES -> LOW-VALUES`` test (lines 628-633).

    True for empty text, all spaces, or ``*`` followed only by spaces. The comparison pads
    ``'*'`` on the right, so text with a leading space before ``*`` is not blank and reaches
    the numeric edit. LOW-VALUES and SPACES both mean "not entered" here, because screen text
    cannot hold the NUL bytes of an untouched 3270 field.
    """
    return text.rstrip(" ") in ("", BLANK_INPUT_MARKER)


@dataclass
class AccountReadResult:
    """What 9000-READ-ACCT leaves behind: the records read, WS-RETURN-MSG and the filter flag.

    ``message`` and ``httpStatus`` replace WS-RETURN-MSG; "" means WS-RETURN-MSG-OFF.
    ``acctFilterOk`` is False once FLG-ACCTFILTER-NOT-OK is set, which marks the account key
    as the failing field. ``customerFound`` replaces FOUND-CUST-IN-MASTER. Records are the
    repositories' column-name dictionaries, or None when not read or not found.
    ``transactionId`` is the transaction running the chain (CAVW, or CAUP for COACTUPC), which
    its logged file conditions name.
    """

    acctId: str
    xref: dict[str, Any] | None = None
    account: dict[str, Any] | None = None
    customer: dict[str, Any] | None = None
    message: str = ""
    httpStatus: int = HTTP_OK
    acctFilterOk: bool = True
    customerFound: bool = False
    transactionId: str = TRANSACTION_ID

    def SetGuarded(self, message: str, httpStatus: int) -> None:
        """Set the message only while none is set: the NOTFND ``IF WS-RETURN-MSG-OFF`` guard."""
        if not self.message:
            self.message = message
            self.httpStatus = httpStatus

    def SetUnconditional(self, message: str, httpStatus: int) -> None:
        """Overwrite any message, as each WHEN OTHER branch moves WS-FILE-ERROR-MESSAGE."""
        self.message = message
        self.httpStatus = httpStatus


async def ReadAccountChain(
    context: OnlineContext, acctId: str, transactionId: str = TRANSACTION_ID
) -> AccountReadResult:
    """Port of 9000-READ-ACCT (lines 687-722): CXACAIX, then ACCTDAT, then CUSTDAT.

    ``acctId`` is the validated 11-digit key and ``transactionId`` the transaction whose file
    conditions are logged (CAUP passes its own). A failed cross-reference read ends the chain
    (``IF FLG-ACCTFILTER-NOT-OK``). A failed account read does not: the
    DID-NOT-FIND-ACCT-IN-ACCTDAT test is dead because its SET is commented out (line 792), so
    the customer of the cross-reference is still read. After an account file error the request
    session's transaction is recovered first, so CUSTDAT is read independently, as CICS files
    fail independently: a NORMAL customer read keeps the ACCTDAT message, and only a CUSTDAT
    failure replaces it. Against the real schema an account or customer NOTFND after a found
    cross-reference cannot happen, because ``xref.card_xref`` has foreign keys to both masters;
    those branches are reached only with stubbed repositories. Only RepositoryError is caught;
    FileNotOpenError from the batch window is one.
    """
    readResult = AccountReadResult(acctId=acctId, transactionId=transactionId)
    await ReadXrefStep(context, readResult)
    if not readResult.acctFilterOk:
        return readResult
    await ReadAccountStep(context, readResult)
    await ReadCustomerStep(context, readResult)
    return readResult


async def ReadXrefStep(context: OnlineContext, readResult: AccountReadResult) -> None:
    """Port of 9200-GETCARDXREF-BYACCT (lines 723-770): READ DATASET(CXACAIX) by account."""
    try:
        xref = await CardXrefRepository(context).GetXrefByAccount(readResult.acctId)
    except RepositoryError as error:
        LogFileCondition(LOGGER, readResult.transactionId, FILE_ERROR_OPERATION_READ, error)
        readResult.acctFilterOk = False
        readResult.SetUnconditional(FileErrorFor(XREF_ACCOUNT_PATH_NAME, error), HTTP_FILE_ERROR)
        return
    if xref is None:
        readResult.acctFilterOk = False
        readResult.SetGuarded(BuildXrefNotFoundMessage(readResult.acctId), HTTP_NOT_FOUND)
        return
    readResult.xref = xref


async def ReadAccountStep(context: OnlineContext, readResult: AccountReadResult) -> None:
    """Port of 9300-GETACCTDATA-BYACCT (lines 774-820): READ DATASET(ACCTDAT) by account.

    A WHEN OTHER failure (lines 809-816) can leave the request session's transaction aborted
    or its connection lost, so the session is recovered before 9400 reads CUSTDAT on it.
    """
    try:
        account = await AccountRepository(context).GetAccount(readResult.acctId)
    except RepositoryError as error:
        LogFileCondition(LOGGER, readResult.transactionId, FILE_ERROR_OPERATION_READ, error)
        readResult.acctFilterOk = False
        readResult.SetUnconditional(FileErrorFor(ACCOUNT_FILE_NAME, error), HTTP_FILE_ERROR)
        await RecoverRequestSession(context.session)
        return
    if account is None:
        readResult.acctFilterOk = False
        readResult.SetGuarded(BuildAccountNotFoundMessage(readResult.acctId), HTTP_NOT_FOUND)
        return
    readResult.account = account


async def ReadCustomerStep(context: OnlineContext, readResult: AccountReadResult) -> None:
    """Port of 9400-GETCUSTDATA-BYCUST (lines 825-869): READ DATASET(CUSTDAT) by customer.

    The key is the cross-reference's XREF-CUST-ID (CDEMO-CUST-ID). A failure sets only
    FLG-CUSTFILTER-NOT-OK, so the account key stays a valid field.
    """
    custId = str(readResult.xref["cust_id"]) if readResult.xref is not None else ""
    try:
        customer = await CustomerRepository(context).GetCustomer(custId)
    except RepositoryError as error:
        LogFileCondition(LOGGER, readResult.transactionId, FILE_ERROR_OPERATION_READ, error)
        readResult.SetUnconditional(FileErrorFor(CUSTOMER_FILE_NAME, error), HTTP_FILE_ERROR)
        return
    if customer is None:
        readResult.SetGuarded(BuildCustomerNotFoundMessage(custId), HTTP_NOT_FOUND)
        return
    readResult.customer = customer
    readResult.customerFound = True


def FileErrorFor(fileName: str, error: RepositoryError) -> str:
    """Return the WHEN OTHER READ message for the paragraph's file and the error's codes.

    The file name is the paragraph's literal (LIT-CARDXREFNAME-ACCT-PATH, LIT-ACCTFILENAME or
    LIT-CUSTFILENAME), as each branch moves it to ERROR-FILE.
    """
    return BuildFileErrorMessage(FILE_ERROR_OPERATION_READ, fileName, error.resp, error.resp2)


def EditAccountField(acctIdText: str) -> tuple[str, str]:
    """Port of 2210-EDIT-ACCOUNT (lines 649-685): return the filter flag and its message.

    ACCTSID arrives space-padded to 11 characters, as RECEIVE MAP gives it. Blank gives
    FLG-ACCTFILTER-BLANK with ``Account number not provided``. Anything but 11 ASCII digits
    (``IS NOT NUMERIC``, so a short key or longer text fails), or all zeros, gives
    FLG-ACCTFILTER-NOT-OK with the filter literal. Otherwise the key is valid.
    """
    paddedKey = acctIdText.ljust(ACCOUNT_KEY_LENGTH)
    if IsBlankScreenInput(acctIdText):
        return (FLAG_BLANK, MSG_ACCOUNT_NOT_PROVIDED)
    isElevenDigits = len(paddedKey) == ACCOUNT_KEY_LENGTH and IsAsciiDigits(paddedKey)
    if not isElevenDigits or paddedKey == ZERO_ACCOUNT_KEY:
        return (FLAG_NOT_OK, MSG_ACCOUNT_FILTER_INVALID)
    return (FLAG_VALID, "")


def IsAsciiDigits(text: str) -> bool:
    """Return True when text is non-empty and every character is 0-9 (COBOL IS NUMERIC).

    ``str.isdigit`` alone would also accept non-ASCII digits such as superscripts.
    """
    return text.isascii() and text.isdigit()


def ValidateViewAccountKey(acctIdText: str) -> str:
    """Port of 2200-EDIT-MAP-INPUTS (lines 622-643): return the validated 11-digit key.

    2210 sets ``Account number not provided`` for a blank key, but the cross-field edit then
    sets NO-SEARCH-CRITERIA-RECEIVED unconditionally, so a blank key answers
    ``No input received``. Raises the 400 LegacyMessageError of either failure.
    """
    flag, message = EditAccountField(acctIdText)
    if flag == FLAG_BLANK:
        message = MSG_NO_INPUT_RECEIVED
    if flag != FLAG_VALID:
        raise ScreenError(message, HTTP_EDIT_FAILURE)
    return acctIdText.ljust(ACCOUNT_KEY_LENGTH)


def ScreenError(message: str, httpStatus: int) -> LegacyMessageError:
    """Return the error of a COACTVW screen sent back with a message (1000-SEND-MAP).

    The information line is always the input prompt (1200, lines 528-530) and the cursor is
    always ACCTSID (1300-SETUP-SCREEN-ATTRS, lines 546-552).
    """
    # ACCTSID is the map's only input, and the error contract cannot place the cursor on a
    # field without listing it as failing. So it is listed for every failure, including the
    # CUSTDAT-only failures (FLG-CUSTFILTER-NOT-OK), where COACTVWC leaves ACCTSID uncoloured.
    return LegacyMessageError(
        message,
        field=ACCOUNT_ID_FIELD,
        httpStatus=httpStatus,
        details=LegacyErrorDetails(
            infoMessage=INFO_PROMPT_FOR_INPUT, fieldErrors=(ACCOUNT_ID_FIELD,)
        ),
    )


async def ViewAccount(context: OnlineContext, acctId: str) -> AccountViewResponse:
    """Port of the CAVW ENTER path (lines 361-374): 2000-PROCESS-INPUTS, then 9000-READ-ACCT.

    ``acctId`` is the ACCTSID text as keyed. Raises 400 for a key edit failure, 404 for a
    NOTFND message and 503 for a file error, batch-window refusals included; each body carries
    the message 9000-READ-ACCT left in WS-RETURN-MSG.
    """
    validAcctId = ValidateViewAccountKey(acctId)
    readResult = await ReadAccountChain(context, validAcctId)
    if readResult.message:
        raise ScreenError(readResult.message, readResult.httpStatus)
    return BuildViewResponse(readResult)


def BuildViewResponse(readResult: AccountReadResult) -> AccountViewResponse:
    """Port of 1200-SETUP-SCREEN-VARS (lines 460-535) with the account and customer found.

    Called only when WS-RETURN-MSG is off, which holds only when the cross-reference, account
    and customer were all read. ACCTSID shows the key as entered (CC-ACCT-ID). The response
    carries the SSN masked, and no card number, CVV or password.
    """
    return AccountViewResponse(
        acct_id=readResult.acctId,
        **AccountScreenValues(readResult.account),
        **CustomerScreenValues(readResult.customer),
        message="",
        info_message=INFO_PROMPT_FOR_INPUT,
        field_errors=[],
        cursor_field=ACCOUNT_ID_FIELD,
    )


def AccountScreenValues(account: Mapping[str, Any]) -> dict[str, Any]:
    """Return the CVACT01Y values 1200 moves to the map (lines 471-491).

    Amounts stay the exact ``Decimal`` of their NUMERIC(12,2) columns. ACCT-GROUP-ID is the
    group shown, never ACCT-ADDR-ZIP.
    """
    screenValues = {column: account[column] for column in ACCOUNT_DISPLAY_COLUMNS}
    for column in ACCOUNT_DATE_COLUMNS:
        screenValues[column] = IsoDateText(account[column])
    return screenValues


def CustomerScreenValues(customer: Mapping[str, Any]) -> dict[str, Any]:
    """Return the CVCUS01Y values 1200 moves to the map (lines 493-523).

    The source shows CUST-SSN as ``999-99-9999``; the response masks it with MaskSsn. City is
    CUST-ADDR-LINE-3 (``addr_line_3``), as the map's ACSCITY field shows it.
    """
    screenValues = {column: customer[column] for column in CUSTOMER_DISPLAY_COLUMNS}
    screenValues["date_of_birth"] = IsoDateText(customer["date_of_birth"])
    screenValues["ssn_masked"] = MaskSsn(customer["ssn"])
    return screenValues


def IsoDateText(value: date | None) -> str:
    """Return a DATE column as the YYYY-MM-DD text it replaces, or "" for NULL (a blank date)."""
    if value is None:
        return ""
    return value.isoformat()
