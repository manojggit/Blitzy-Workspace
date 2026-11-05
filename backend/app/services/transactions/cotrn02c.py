"""Replaces: COTRN02C (CT02), BMS COTRN02, copybooks CVTRA05Y, CVACT01Y, CVACT03Y
and the called date-validation program CSUTLDTC (app.utils.date_validation).

The Transaction Add screen, served as ``POST /api/v1/transactions``. ENTER (``enter``) runs
PROCESS-ENTER-KEY: the key edits (VALIDATE-INPUT-KEY-FIELDS), the data edits
(VALIDATE-INPUT-DATA-FIELDS), then the CONFIRMI evaluation, which adds the record
(ADD-TRANSACTION, WRITE-TRANSACT-FILE) only for ``Y`` or ``y``. F5 (``copy_last``) runs
COPY-LAST-TRAN-DATA: the key edits, the copy of the highest TRANSACT record into the form, then
the same ENTER flow, key edits included.

The edits run in first-failure mode on the text exactly as keyed: every failing branch ends in
SEND-TRNADD-SCREEN, whose EXEC CICS RETURN (lines 530-534) ends the task, so the first failure
is the only message. Its error body is the four standard legacy keys only (message,
info_message, field_errors, cursor_field), and the client keeps the screen as keyed. Only the
200 confirm prompt re-sends the map in ``fields``, as the program sends it back: the key the
xref read resolved, the amount reformatted to ``+99999999.99`` and any values F5 copied, with
the card number masked.

The add is one TRANSACT write unit (core.database.WriteUnit): the TRAN-ID allocation, which is
the STARTBR HIGH-VALUES / READPREV / ADD 1 of lines 444-449, and the WRITE share its
transaction, so the allocation lock lasts until the insert commits. A CT02 add changes no
balance. Both timestamps hold the entered date followed by 16 spaces, stored as midnight with
format code ``T``.

Legacy paths with no code here: EIBCALEN = 0 is the regular-user guard; PF3 (back) and PF4
(CLEAR-CURRENT-SCREEN) are frontend navigation and form clearing; POPULATE-HEADER-INFO is the
frontend ScreenShell; the invalid-key WHEN OTHER cannot occur, because ``action`` is an enum.
The first-entry PROCESS-ENTER-KEY call on CDEMO-CT02-TRN-SELECTED is unreachable, as no program
sets that field, and the ``IF ERR-FLG-ON`` block opening VALIDATE-INPUT-DATA-FIELDS is
unreachable, as every earlier failure has already returned.
"""

import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, NamedTuple

from app.core.database import WriteUnit
from app.core.dependencies import OnlineContext
from app.core.errors import (
    HTTP_EDIT_FAILURE,
    HTTP_FILE_ERROR,
    HTTP_NOT_FOUND,
    HTTP_RECORD_CHANGED,
    DuplicateKeyError,
    FileIoError,
    LegacyMessageError,
    LogFileCondition,
    RepositoryError,
)
from app.core.security import MaskCardNumber
from app.repositories.card_xref_repository import CardXrefRepository
from app.repositories.transaction_repository import TRANSACTION_FILE_NAME, TransactionRepository
from app.schemas.common import TransactionAddAction
from app.schemas.transactions import (
    TransactionAddFields,
    TransactionAddRequest,
    TransactionAddResponse,
)
from app.utils.cobol_format import ONLINE_AMOUNT_PICTURE, FormatEdited
from app.utils.cobol_intrinsics import Numval, NumvalC
from app.utils.date_validation import (
    CEEDAYS_MSG_UNSUPPORTED_RANGE,
    CEEDAYS_VALID_SEVERITY,
    ISO_DATE_MASK,
    CheckDate,
)
from app.utils.decimal_utils import StoreInto
from app.utils.record_layouts import (
    TIMESTAMP_FORMAT_COLUMN_SUFFIX,
    TIMESTAMP_LENGTH,
    DecodeTimestamp,
    EncodeTimestamp,
    TimestampFormatError,
)

LOGGER = logging.getLogger(__name__)

# WS-TRANID (line 37).
TRANSACTION_ID = "CT02"
# The CICS commands of READ-CXACAIX-FILE and READ-CCXREF-FILE, of the STARTBR HIGH-VALUES /
# READPREV that finds the highest TRAN-ID, and of WRITE-TRANSACT-FILE, as their logged file
# conditions name them.
READ_OPERATION = "READ"
BROWSE_BACKWARD_OPERATION = "STARTBR/READPREV"
WRITE_OPERATION = "WRITE"

# BMS COTRN02 input field LENGTHs (cpy-bms/COTRN02.CPY).
ACCT_ID_LENGTH = 11
CARD_NUM_LENGTH = 16
TYPE_CD_LENGTH = 2
CAT_CD_LENGTH = 4
SOURCE_LENGTH = 10
DESC_LENGTH = 60
AMOUNT_LENGTH = 12
DATE_FIELD_LENGTH = 10
MERCHANT_ID_LENGTH = 9
MERCHANT_NAME_LENGTH = 30
MERCHANT_CITY_LENGTH = 25
MERCHANT_ZIP_LENGTH = 10
CONFIRM_LENGTH = 1

# VALIDATE-INPUT-KEY-FIELDS (lines 193-230) and the xref reads (lines 576-637).
ACCOUNT_OR_CARD_REQUIRED_MESSAGE = "Account or Card Number must be entered..."
ACCOUNT_NOT_NUMERIC_MESSAGE = "Account ID must be Numeric..."
CARD_NOT_NUMERIC_MESSAGE = "Card Number must be Numeric..."
ACCOUNT_NOT_FOUND_MESSAGE = "Account ID NOT found..."
CARD_NOT_FOUND_MESSAGE = "Card Number NOT found..."
ACCOUNT_XREF_LOOKUP_FAILED_MESSAGE = "Unable to lookup Acct in XREF AIX file..."
CARD_XREF_LOOKUP_FAILED_MESSAGE = "Unable to lookup Card # in XREF file..."
# VALIDATE-INPUT-DATA-FIELDS (lines 251-436).
REQUIRED_FIELD_TEMPLATE = "{label} can NOT be empty..."
TYPE_NOT_NUMERIC_MESSAGE = "Type CD must be Numeric..."
CATEGORY_NOT_NUMERIC_MESSAGE = "Category CD must be Numeric..."
AMOUNT_FORMAT_MESSAGE = "Amount should be in format -99999999.99"
ORIG_DATE_FORMAT_MESSAGE = "Orig Date should be in format YYYY-MM-DD"
PROC_DATE_FORMAT_MESSAGE = "Proc Date should be in format YYYY-MM-DD"
ORIG_DATE_INVALID_MESSAGE = "Orig Date - Not a valid date..."
PROC_DATE_INVALID_MESSAGE = "Proc Date - Not a valid date..."
MERCHANT_ID_NOT_NUMERIC_MESSAGE = "Merchant ID must be Numeric..."
# PROCESS-ENTER-KEY (lines 169-188).
CONFIRM_PROMPT_MESSAGE = "Confirm to add this transaction..."
CONFIRM_INVALID_MESSAGE = "Invalid value. Valid values are (Y/N)..."
# The TRANSACT browse (lines 664, 693) and WRITE-TRANSACT-FILE (lines 728-745).
TRAN_LOOKUP_FAILED_MESSAGE = "Unable to lookup Transaction..."
DUPLICATE_TRAN_ID_MESSAGE = "Tran ID already exist..."
ADD_FAILED_MESSAGE = "Unable to Add Transaction..."
# The STRING of lines 728-733: the two literal parts leave two spaces after the period.
ADD_SUCCESS_TEMPLATE = "Transaction added successfully. " + " Your Tran ID is {tranId}."

# WS-DATE-FORMAT, the mask COTRN02C passes to CSUTLDTC.
DATE_MASK = ISO_DATE_MASK
# WS-TRAN-AMT-N and TRAN-AMT, both PIC S9(9)V99.
TRAN_AMT_INT_DIGITS = 9
TRAN_AMT_DEC_DIGITS = 2
# EVALUATE CONFIRMI: WHEN 'Y' 'y' adds; WHEN 'N' 'n' SPACES prompts (a one-character field
# keyed blank is a space). WHEN LOW-VALUES needs no entry: an untouched field arrives as ""
# and pads to a space, and ScreenText's code-page-037 printable check refuses NUL with 422.
CONFIRM_ADD_VALUES = frozenset({"Y", "y"})
CONFIRM_PROMPT_VALUES = frozenset({"N", "n", " "})

# 409 Conflict for DUPKEY/DUPREC: the key exists already. errors.py names the status after
# its other literal, "Record changed by some one else. Please review".
DUPLICATE_TRAN_ID_STATUS = HTTP_RECORD_CHANGED

# The padding of a BMS input field; IS NUMERIC sees the field at its full LENGTH.
FIELD_PADDING = " "
NUMERIC_FIELD_PATTERN = re.compile(r"[0-9]+")
# Lines 340-343 on TRNAMTI(1:1), (2:8), (10:1) and (11:2).
AMOUNT_FORMAT_PATTERN = re.compile(r"[+-][0-9]{8}\.[0-9]{2}")
# Lines 354-358 (and 369-373) on positions (1:4), (5:1), (6:2), (8:1) and (9:2).
DATE_FORMAT_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

# COTRN2AI field (ScreenFields attribute) -> TransactionAddFields attribute, which is the JSON
# key and the cursor_field name that replaces MOVE -1 TO <field>L.
SCREEN_FIELD_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "acctId": "acct_id",
        "cardNum": "card_num",
        "typeCd": "type_cd",
        "catCd": "cat_cd",
        "source": "source",
        "desc": "desc",
        "amount": "amount",
        "origDate": "orig_date",
        "procDate": "proc_date",
        "merchantId": "merchant_id",
        "merchantName": "merchant_name",
        "merchantCity": "merchant_city",
        "merchantZip": "merchant_zip",
        "confirm": "confirm",
    }
)
ACCT_ID_FIELD = SCREEN_FIELD_NAMES["acctId"]
CARD_NUM_FIELD = SCREEN_FIELD_NAMES["cardNum"]
AMOUNT_FIELD = SCREEN_FIELD_NAMES["amount"]
CONFIRM_FIELD = SCREEN_FIELD_NAMES["confirm"]


@dataclass
class ScreenFields:
    """The COTRN2AI input area: the 14 map fields as text, edited in place as COTRN02C does."""

    acctId: str = ""
    cardNum: str = ""
    typeCd: str = ""
    catCd: str = ""
    source: str = ""
    desc: str = ""
    amount: str = ""
    origDate: str = ""
    procDate: str = ""
    merchantId: str = ""
    merchantName: str = ""
    merchantCity: str = ""
    merchantZip: str = ""
    confirm: str = ""


class RequiredField(NamedTuple):
    """One WHEN of the presence EVALUATE (lines 251-320): the field and its message label."""

    attributeName: str
    label: str


# Lines 251-320, in source order: the first blank field ends validation.
PRESENCE_CHECKS = (
    RequiredField("typeCd", "Type CD"),
    RequiredField("catCd", "Category CD"),
    RequiredField("source", "Source"),
    RequiredField("desc", "Description"),
    RequiredField("amount", "Amount"),
    RequiredField("origDate", "Orig Date"),
    RequiredField("procDate", "Proc Date"),
    RequiredField("merchantId", "Merchant ID"),
    RequiredField("merchantName", "Merchant Name"),
    RequiredField("merchantCity", "Merchant City"),
    RequiredField("merchantZip", "Merchant Zip"),
)


class NumericField(NamedTuple):
    """One IS NUMERIC edit on a map field: the field, its BMS LENGTH and its message."""

    attributeName: str
    length: int
    message: str


TYPE_CD_NUMERIC = NumericField("typeCd", TYPE_CD_LENGTH, TYPE_NOT_NUMERIC_MESSAGE)
CAT_CD_NUMERIC = NumericField("catCd", CAT_CD_LENGTH, CATEGORY_NOT_NUMERIC_MESSAGE)
MERCHANT_ID_NUMERIC = NumericField(
    "merchantId", MERCHANT_ID_LENGTH, MERCHANT_ID_NOT_NUMERIC_MESSAGE
)


class DateField(NamedTuple):
    """One date field: its format edit (lines 353-381) and CSUTLDTC edit (lines 389-427)."""

    attributeName: str
    formatMessage: str
    invalidMessage: str


# Orig date is edited before proc date in both groups of edits.
DATE_FIELDS = (
    DateField("origDate", ORIG_DATE_FORMAT_MESSAGE, ORIG_DATE_INVALID_MESSAGE),
    DateField("procDate", PROC_DATE_FORMAT_MESSAGE, PROC_DATE_INVALID_MESSAGE),
)

type XrefLookup = Callable[[CardXrefRepository, str], Awaitable[dict[str, Any] | None]]


class KeyPath(NamedTuple):
    """One WHEN of VALIDATE-INPUT-KEY-FIELDS: the key entered, its xref read, the field it fills.

    ``lookup`` is READ-CXACAIX-FILE or READ-CCXREF-FILE; ``filledColumn`` of the record found is
    moved into ``filledAttribute``, as XREF-CARD-NUM to CARDNINI or XREF-ACCT-ID to ACTIDINI.
    The lookups call the repository method by name when they run, not a function captured at
    import, so a test that replaces the repository or its methods reaches them.
    """

    keyAttribute: str
    keyLength: int
    lookup: XrefLookup
    filledAttribute: str
    filledColumn: str
    notNumericMessage: str
    notFoundMessage: str
    lookupFailedMessage: str


ACCOUNT_KEY_PATH = KeyPath(
    keyAttribute="acctId",
    keyLength=ACCT_ID_LENGTH,
    lookup=lambda repository, acctId: repository.GetXrefByAccount(acctId),
    filledAttribute="cardNum",
    filledColumn="card_num",
    notNumericMessage=ACCOUNT_NOT_NUMERIC_MESSAGE,
    notFoundMessage=ACCOUNT_NOT_FOUND_MESSAGE,
    lookupFailedMessage=ACCOUNT_XREF_LOOKUP_FAILED_MESSAGE,
)
CARD_KEY_PATH = KeyPath(
    keyAttribute="cardNum",
    keyLength=CARD_NUM_LENGTH,
    lookup=lambda repository, cardNum: repository.GetXrefByCard(cardNum),
    filledAttribute="acctId",
    filledColumn="acct_id",
    notNumericMessage=CARD_NOT_NUMERIC_MESSAGE,
    notFoundMessage=CARD_NOT_FOUND_MESSAGE,
    lookupFailedMessage=CARD_XREF_LOOKUP_FAILED_MESSAGE,
)

# ADD-TRANSACTION lines 454-463: TRAN-RECORD text column <- COTRN2AI field, moved as keyed.
TEXT_COLUMNS = (
    ("tran_source", "source"),
    ("tran_desc", "desc"),
    ("merchant_name", "merchantName"),
    ("merchant_city", "merchantCity"),
    ("merchant_zip", "merchantZip"),
)

# COPY-LAST-TRAN-DATA lines 482-492: COTRN2AI field <- TRAN-RECORD column, cut to the map field.
COPIED_TEXT_FIELDS = (
    ("typeCd", "tran_type_cd", TYPE_CD_LENGTH),
    ("source", "tran_source", SOURCE_LENGTH),
    ("desc", "tran_desc", DESC_LENGTH),
    ("merchantId", "merchant_id", MERCHANT_ID_LENGTH),
    ("merchantName", "merchant_name", MERCHANT_NAME_LENGTH),
    ("merchantCity", "merchant_city", MERCHANT_CITY_LENGTH),
    ("merchantZip", "merchant_zip", MERCHANT_ZIP_LENGTH),
)

# The READPREV ENDFILE branch copies the record area as initialised: spaces in the X fields,
# zeros in TRAN-CAT-CD 9(04), TRAN-MERCHANT-ID 9(09) and TRAN-AMT S9(09)V99.
EMPTY_RECORD_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        "typeCd": "",
        "catCd": "0" * CAT_CD_LENGTH,
        "source": "",
        "amount": FormatEdited(Decimal(0), ONLINE_AMOUNT_PICTURE),
        "desc": "",
        "origDate": "",
        "procDate": "",
        "merchantId": "0" * MERCHANT_ID_LENGTH,
        "merchantName": "",
        "merchantCity": "",
        "merchantZip": "",
    }
)


def IsBlank(text: str) -> bool:
    """Return True for ``SPACES OR LOW-VALUES``: an empty field or one holding only spaces."""
    return not text.strip(FIELD_PADDING)


def IsNumericField(text: str, length: int) -> bool:
    """Return True when the map field, padded to its BMS LENGTH, holds only digits 0-9.

    COBOL ``IS NUMERIC`` tests the whole PIC X(length) field, whose unkeyed positions hold
    spaces, so a short entry such as ``4`` in the 11-character account field is not numeric.
    """
    return NUMERIC_FIELD_PATTERN.fullmatch(text.ljust(length, FIELD_PADDING)) is not None


def TrimPadding(text: str) -> str:
    return text.rstrip(FIELD_PADDING)


def CursorField(attributeName: str) -> str:
    """Return the cursor_field name of a COTRN2AI field, replacing MOVE -1 TO <field>L."""
    return SCREEN_FIELD_NAMES[attributeName]


def FromRequest(fields: TransactionAddFields) -> ScreenFields:
    """Return the COTRN2AI area that RECEIVE-TRNADD-SCREEN fills from the keyed fields."""
    return ScreenFields(
        **{
            attributeName: getattr(fields, fieldName)
            for attributeName, fieldName in SCREEN_FIELD_NAMES.items()
        }
    )


def ToResponseFields(screen: ScreenFields) -> TransactionAddFields:
    """Return the map as SEND-TRNADD-SCREEN sends it back, with the card number masked.

    Any non-blank card field text, whatever its length or characters, leaves the server only as
    MaskCardNumber returns it: twelve asterisks plus the last four characters of the trimmed
    text. A blank field stays blank. Masking is safe for the dialogue: the account the xref read
    resolved is sent back too, and a non-blank account takes precedence over the card on the
    next ENTER, as in VALIDATE-INPUT-KEY-FIELDS.
    """
    values = {
        fieldName: getattr(screen, attributeName)
        for attributeName, fieldName in SCREEN_FIELD_NAMES.items()
    }
    values[CARD_NUM_FIELD] = MaskCardNumber(screen.cardNum)
    return TransactionAddFields(**values)


def ScreenError(message: str, cursorField: str, httpStatus: int) -> LegacyMessageError:
    """Return the error of one failing branch: its literal, cursor and status.

    Statuses: 400 for edits and the invalid confirm value, 404 for the NOTFND literals, 409 for
    DUPKEY/DUPREC and 503 for the WHEN OTHER file literals, batch-window refusals included.
    The body is the four standard legacy keys only (message, info_message, field_errors,
    cursor_field); the screen stays as the client keyed it. Callers ``raise`` it, chaining any
    repository error.
    """
    return LegacyMessageError(message, field=cursorField, httpStatus=httpStatus)


def NumvalKey(keyText: str, keyLength: int) -> str:
    """Return ``FUNCTION NUMVAL`` of a numeric key moved into PIC 9(keyLength), as text."""
    keyValue = int(Numval(keyText)) % 10**keyLength
    return f"{keyValue:0{keyLength}d}"


async def ResolveKeyField(context: OnlineContext, screen: ScreenFields, keyPath: KeyPath) -> None:
    """Port one WHEN of VALIDATE-INPUT-KEY-FIELDS with READ-CXACAIX-FILE or READ-CCXREF-FILE.

    The key must be numeric; its NUMVAL value is moved back into the field and read through the
    xref path, and the record's other key fills the other field (lines 196-223). RepositoryError
    covers the batch window's FileNotOpenError and the CXACAIX DUPKEY, both WHEN OTHER.
    """
    keyText = getattr(screen, keyPath.keyAttribute)
    cursorField = CursorField(keyPath.keyAttribute)
    if not IsNumericField(keyText, keyPath.keyLength):
        raise ScreenError(keyPath.notNumericMessage, cursorField, HTTP_EDIT_FAILURE)
    keyValue = NumvalKey(keyText, keyPath.keyLength)
    setattr(screen, keyPath.keyAttribute, keyValue)
    try:
        xref = await keyPath.lookup(CardXrefRepository(context), keyValue)
    except RepositoryError as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, READ_OPERATION, error)
        raise ScreenError(keyPath.lookupFailedMessage, cursorField, HTTP_FILE_ERROR) from error
    if xref is None:
        raise ScreenError(keyPath.notFoundMessage, cursorField, HTTP_NOT_FOUND)
    setattr(screen, keyPath.filledAttribute, xref[keyPath.filledColumn])


async def ValidateInputKeyFields(context: OnlineContext, screen: ScreenFields) -> None:
    """Port VALIDATE-INPUT-KEY-FIELDS (lines 193-230): the account, else the card, else neither.

    Raises:
        LegacyMessageError: the first failing key edit, NOTFND or xref file error.
    """
    if not IsBlank(screen.acctId):
        await ResolveKeyField(context, screen, ACCOUNT_KEY_PATH)
    elif not IsBlank(screen.cardNum):
        await ResolveKeyField(context, screen, CARD_KEY_PATH)
    else:
        raise ScreenError(ACCOUNT_OR_CARD_REQUIRED_MESSAGE, ACCT_ID_FIELD, HTTP_EDIT_FAILURE)


def CheckPresence(screen: ScreenFields) -> None:
    """Port the presence EVALUATE (lines 251-320): ``<label> can NOT be empty...``."""
    for requiredField in PRESENCE_CHECKS:
        if IsBlank(getattr(screen, requiredField.attributeName)):
            message = REQUIRED_FIELD_TEMPLATE.format(label=requiredField.label)
            cursorField = CursorField(requiredField.attributeName)
            raise ScreenError(message, cursorField, HTTP_EDIT_FAILURE)


def CheckNumeric(screen: ScreenFields, numericField: NumericField) -> None:
    """Port one IS NUMERIC edit: type and category (lines 322-337) or merchant ID (430-436)."""
    fieldText = getattr(screen, numericField.attributeName)
    if not IsNumericField(fieldText, numericField.length):
        cursorField = CursorField(numericField.attributeName)
        raise ScreenError(numericField.message, cursorField, HTTP_EDIT_FAILURE)


def CheckAmountFormat(screen: ScreenFields) -> None:
    """Port the amount EVALUATE (lines 339-351): sign, 8 digits, point, 2 digits."""
    amountText = screen.amount.ljust(AMOUNT_LENGTH, FIELD_PADDING)
    if AMOUNT_FORMAT_PATTERN.fullmatch(amountText) is None:
        raise ScreenError(AMOUNT_FORMAT_MESSAGE, AMOUNT_FIELD, HTTP_EDIT_FAILURE)


def CheckDateFormat(screen: ScreenFields, dateField: DateField) -> None:
    """Port one date-format EVALUATE (lines 353-381): YYYY-MM-DD by position."""
    dateText = getattr(screen, dateField.attributeName).ljust(DATE_FIELD_LENGTH, FIELD_PADDING)
    if DATE_FORMAT_PATTERN.fullmatch(dateText) is None:
        cursorField = CursorField(dateField.attributeName)
        raise ScreenError(dateField.formatMessage, cursorField, HTTP_EDIT_FAILURE)


def NormaliseAmount(screen: ScreenFields) -> None:
    """Port lines 383-386: NUMVAL-C into WS-TRAN-AMT-N, then WS-TRAN-AMT-E back into TRNAMTI."""
    amountValue = StoreInto(NumvalC(screen.amount), TRAN_AMT_INT_DIGITS, TRAN_AMT_DEC_DIGITS)
    screen.amount = FormatEdited(amountValue, ONLINE_AMOUNT_PICTURE)


def CheckDateValid(screen: ScreenFields, dateField: DateField) -> None:
    """Port one CALL 'CSUTLDTC' check (lines 389-427).

    The date is valid at severity 0; any other result passes only with CEEDAYS message 2513
    (unsupported range, a date before 1582-10-15).
    """
    result = CheckDate(getattr(screen, dateField.attributeName), DATE_MASK)
    if result.severity == CEEDAYS_VALID_SEVERITY:
        return
    if result.messageNumber != CEEDAYS_MSG_UNSUPPORTED_RANGE:
        cursorField = CursorField(dateField.attributeName)
        raise ScreenError(dateField.invalidMessage, cursorField, HTTP_EDIT_FAILURE)


def ValidateInputDataFields(screen: ScreenFields) -> None:
    """Port VALIDATE-INPUT-DATA-FIELDS (lines 235-437) in source order.

    Presence, type and category numeric, amount format, both date formats, the amount
    reformatted (no message), both CSUTLDTC checks, then merchant ID numeric.

    Raises:
        LegacyMessageError: 400 with the first failing edit's literal and field.
    """
    CheckPresence(screen)
    CheckNumeric(screen, TYPE_CD_NUMERIC)
    CheckNumeric(screen, CAT_CD_NUMERIC)
    CheckAmountFormat(screen)
    for dateField in DATE_FIELDS:
        CheckDateFormat(screen, dateField)
    NormaliseAmount(screen)
    for dateField in DATE_FIELDS:
        CheckDateValid(screen, dateField)
    CheckNumeric(screen, MERCHANT_ID_NUMERIC)


def TimestampColumns(column: str, dateText: str) -> dict[str, Any]:
    """Return a timestamp column and its format code for MOVE TORIGDTI/TPROCDTI TO TRAN-*-TS.

    The 10-character date padded to the 26-byte field is the ``T`` form, stored as midnight.

    Raises:
        TimestampFormatError: the date cannot be stored, which only year 0000 reaches.
    """
    value, timestampFormat = DecodeTimestamp(dateText.ljust(TIMESTAMP_LENGTH, FIELD_PADDING))
    return {column: value, column + TIMESTAMP_FORMAT_COLUMN_SUFFIX: timestampFormat}


def BuildTransactionValues(screen: ScreenFields, tranId: str) -> dict[str, Any]:
    """Return the TRAN-RECORD of ADD-TRANSACTION (lines 450-465), keyed by column name.

    INITIALIZE TRAN-RECORD then the moves: text columns as keyed without trailing padding, the
    category as its 9(04) value, TRAN-AMT as NUMVAL-C of the reformatted amount, the card the
    key edits resolved, and both dates as ``T`` timestamps.
    """
    values: dict[str, Any] = {
        "tran_id": tranId,
        "tran_type_cd": screen.typeCd,
        "tran_cat_cd": int(screen.catCd),
        "tran_amt": StoreInto(NumvalC(screen.amount), TRAN_AMT_INT_DIGITS, TRAN_AMT_DEC_DIGITS),
        "merchant_id": screen.merchantId,
        "card_num": TrimPadding(screen.cardNum),
    }
    for column, attributeName in TEXT_COLUMNS:
        values[column] = TrimPadding(getattr(screen, attributeName))
    values.update(TimestampColumns("orig_ts", screen.origDate))
    values.update(TimestampColumns("proc_ts", screen.procDate))
    return values


def UnstorableRecordError(error: TimestampFormatError) -> LegacyMessageError:
    """Log the WRITE's WHEN OTHER condition for a record TRANSACT cannot hold; return its 503.

    The DISPLAY of line 743 is logged as TRANSACT's IOERR raised from ``error``, so the line
    names the innermost cause's class and never the date text the error carries.
    """
    writeError = FileIoError(TRANSACTION_FILE_NAME)
    writeError.__cause__ = error
    LogFileCondition(LOGGER, TRANSACTION_ID, WRITE_OPERATION, writeError)
    return ScreenError(ADD_FAILED_MESSAGE, ACCT_ID_FIELD, HTTP_FILE_ERROR)


async def WriteNewTransaction(context: OnlineContext, screen: ScreenFields) -> str:
    """Port ADD-TRANSACTION and WRITE-TRANSACT-FILE (lines 442-466, 711-749) in one write unit.

    AllocateNextTranId is the single next-ID rule (empty table 0000000000000001, wrap after
    9999999999999999). A failure before an ID is allocated is the browse's WHEN OTHER; after it,
    including the commit, the WRITE's. Every outcome puts the cursor on the account field.

    Raises:
        LegacyMessageError: 409 ``Tran ID already exist...``; 503 ``Unable to lookup
            Transaction...`` or ``Unable to Add Transaction...``.
    """
    repository = TransactionRepository(context)
    tranId = ""
    try:
        async with WriteUnit(context.engine, TRANSACTION_FILE_NAME) as unit:
            tranId = await repository.AllocateNextTranId(unit)
            await repository.InsertTransaction(unit, BuildTransactionValues(screen, tranId))
    except DuplicateKeyError as error:
        raise ScreenError(
            DUPLICATE_TRAN_ID_MESSAGE, ACCT_ID_FIELD, DUPLICATE_TRAN_ID_STATUS
        ) from error
    except RepositoryError as error:
        operation = WRITE_OPERATION if tranId else BROWSE_BACKWARD_OPERATION
        LogFileCondition(LOGGER, TRANSACTION_ID, operation, error)
        message = ADD_FAILED_MESSAGE if tranId else TRAN_LOOKUP_FAILED_MESSAGE
        raise ScreenError(message, ACCT_ID_FIELD, HTTP_FILE_ERROR) from error
    except TimestampFormatError as error:
        # A year-0000 date passes CSUTLDTC (CEEDAYS 2513) but no TIMESTAMP(6) value holds it,
        # so the record cannot be written: the WRITE's WHEN OTHER outcome, nothing stored.
        raise UnstorableRecordError(error) from error
    return tranId


def AddedMessage(tranId: str) -> str:
    """Return the green success literal; TRAN-ID is DELIMITED BY SPACE (line 731)."""
    return ADD_SUCCESS_TEMPLATE.format(tranId=tranId.partition(FIELD_PADDING)[0])


async def AddNewTransaction(context: OnlineContext, screen: ScreenFields) -> TransactionAddResponse:
    """Write the transaction, then INITIALIZE-ALL-FIELDS (lines 762-779) for the success screen.

    Every field is blank, confirm included, and the cursor is on the account field.
    """
    tranId = await WriteNewTransaction(context, screen)
    return TransactionAddResponse(
        message=AddedMessage(tranId),
        cursor_field=ACCT_ID_FIELD,
        fields=TransactionAddFields(),
        tran_id=tranId,
    )


async def ProcessEnterKey(context: OnlineContext, screen: ScreenFields) -> TransactionAddResponse:
    """Port PROCESS-ENTER-KEY (lines 164-188): key edits, data edits, then EVALUATE CONFIRMI.

    ``Y``/``y`` adds; ``N``, ``n`` or blank returns 200 with ``Confirm to add this
    transaction...`` and the edited screen; any other value is 400
    ``Invalid value. Valid values are (Y/N)...``.
    """
    await ValidateInputKeyFields(context, screen)
    ValidateInputDataFields(screen)
    confirmValue = screen.confirm.ljust(CONFIRM_LENGTH, FIELD_PADDING)
    if confirmValue in CONFIRM_ADD_VALUES:
        return await AddNewTransaction(context, screen)
    if confirmValue in CONFIRM_PROMPT_VALUES:
        return TransactionAddResponse(
            message=CONFIRM_PROMPT_MESSAGE,
            cursor_field=CONFIRM_FIELD,
            fields=ToResponseFields(screen),
        )
    raise ScreenError(CONFIRM_INVALID_MESSAGE, CONFIRM_FIELD, HTTP_EDIT_FAILURE)


def FitField(text: str, length: int) -> str:
    """Return a record value as MOVE leaves it in a PIC X(length) map field, padding trimmed."""
    return TrimPadding(text[:length])


def CopiedDate(record: Mapping[str, Any], column: str) -> str:
    """Return the first 10 characters of a stored TRAN-ORIG-TS or TRAN-PROC-TS text."""
    timestampFormat = record[column + TIMESTAMP_FORMAT_COLUMN_SUFFIX]
    return FitField(EncodeTimestamp(record[column], timestampFormat), DATE_FIELD_LENGTH)


def CopiedFields(record: Mapping[str, Any]) -> dict[str, str]:
    """Return the map fields COPY-LAST-TRAN-DATA moves from the record (lines 481-492).

    TRAN-CAT-CD 9(04) becomes 4 digits, TRAN-AMT goes through WS-TRAN-AMT-E ``+99999999.99``,
    and each X field is cut to its map field.
    """
    copied = {
        attributeName: FitField(record[column], length)
        for attributeName, column, length in COPIED_TEXT_FIELDS
    }
    categoryCode = int(StoreInto(record["tran_cat_cd"], CAT_CD_LENGTH, 0, signed=False))
    copied["catCd"] = f"{categoryCode:0{CAT_CD_LENGTH}d}"
    copied["amount"] = FormatEdited(record["tran_amt"], ONLINE_AMOUNT_PICTURE)
    copied["origDate"] = CopiedDate(record, "orig_ts")
    copied["procDate"] = CopiedDate(record, "proc_ts")
    return copied


async def CopyLastTranData(context: OnlineContext, screen: ScreenFields) -> TransactionAddResponse:
    """Port COPY-LAST-TRAN-DATA (lines 471-495), the F5 action.

    The key edits run first, then the highest TRANSACT record in key order is copied into the
    data fields; an empty file (READPREV ENDFILE) copies the initial record area. The key
    fields stay as validated, and PROCESS-ENTER-KEY then runs with the submitted confirm.

    Raises:
        LegacyMessageError: a key edit failure, 503 ``Unable to lookup Transaction...`` on a
            browse error, or any outcome of ProcessEnterKey.
    """
    await ValidateInputKeyFields(context, screen)
    try:
        lastRecord = await TransactionRepository(context).GetHighestTransaction()
    except RepositoryError as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, BROWSE_BACKWARD_OPERATION, error)
        raise ScreenError(TRAN_LOOKUP_FAILED_MESSAGE, ACCT_ID_FIELD, HTTP_FILE_ERROR) from error
    copiedFields = EMPTY_RECORD_FIELDS if lastRecord is None else CopiedFields(lastRecord)
    for attributeName, value in copiedFields.items():
        setattr(screen, attributeName, value)
    return await ProcessEnterKey(context, screen)


async def AddTransaction(
    context: OnlineContext, request: TransactionAddRequest
) -> TransactionAddResponse:
    """Handle ``POST /transactions`` (CT02): ENTER or F5 on the Transaction Add screen.

    ``copy_last`` runs COPY-LAST-TRAN-DATA and ``enter`` runs PROCESS-ENTER-KEY, both on the
    fields exactly as keyed.

    Raises:
        LegacyMessageError: the first failing edit or file condition, with the four standard
            body keys only; the screen comes back in ``fields`` on the 200 confirm prompt alone.
    """
    screen = FromRequest(request)
    if request.action == TransactionAddAction.COPY_LAST:
        return await CopyLastTranData(context, screen)
    return await ProcessEnterKey(context, screen)
