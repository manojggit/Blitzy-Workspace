"""Replaces: COCRDSLC (CCDL), BMS COCRDSL, copybooks CVCRD01Y, CVACT02Y, CVCUS01Y, CSMSG02Y

The CCDL card detail view, served as ``GET /api/v1/cards/{cardNum}?acctId``. Typed keys are
edited in all-field mode; a ``card_ref`` sent with its card-list row's account is the entry
from COCRDLIC and is read without the edits. Either way CARDDAT is read by card number only:
the CARDAIX paragraph 9150-GETCARD-BYACCT is never performed in the source, so no account
lookup exists and the account entered is never compared with the card's account.

Shared with cocrdupc.py and cocrdlic.py: the key edits (``EditCardKeys``, ``EditAccountKey``,
``EditCardKey``, ``PadScreenText``, ``IsBlankKey``), ``IsListEntry``, ``DecodeCardKey``,
``ResolveCardKey``, ``NotFoundError``, ``FileErrorFromRepository``, ``BuildFileErrorMessage``,
``ExpiryParts``, and the message, field-name and length constants.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from cryptography.fernet import InvalidToken

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
from app.core.security import DecodeCardRef, EncodeCardRef, MaskCardNumber
from app.repositories.card_repository import CARD_FILE_NAME, CardRepository
from app.schemas.cards import CardDetailResponse

if TYPE_CHECKING:
    # Type-only: the Settings that OnlineContext.settings carries and DecodeCardRef expects.
    from app.core.config import Settings

LOGGER = logging.getLogger(__name__)

# LIT-THISTRANID (lines 165-166).
TRANSACTION_ID = "CCDL"

# WS-RETURN-MSG 88 levels and literal MOVEs set on a reachable path (lines 134-158, 670, 711).
ACCOUNT_NOT_PROVIDED_MESSAGE = "Account number not provided"
CARD_NOT_PROVIDED_MESSAGE = "Card number not provided"
NO_INPUT_RECEIVED_MESSAGE = "No input received"
ACCOUNT_FILTER_MESSAGE = "ACCOUNT FILTER,IF SUPPLIED MUST BE A 11 DIGIT NUMBER"
CARD_FILTER_MESSAGE = "CARD ID FILTER,IF SUPPLIED MUST BE A 16 DIGIT NUMBER"
CARD_NOT_FOUND_MESSAGE = "Did not find cards for this search condition"
# WS-INFO-MSG 88 levels (lines 126-132); the three leading spaces are part of the literal.
DETAILS_SHOWN_INFO = "   Displaying requested details"
PROMPT_FOR_KEYS_INFO = "Please enter Account and Card Number"

# BMS COCRDSL ACCTSID and CARDSID LENGTH.
ACCT_ID_LENGTH = 11
CARD_NUM_LENGTH = 16
# The schemas.cards attribute names that field_errors and cursor_field carry.
ACCT_ID_FIELD = "acct_id"
CARD_NUM_FIELD = "card_num"
# WS-EDIT-ACCT-FLAG and WS-EDIT-CARD-FLAG 88 values (lines 55-62).
FLAG_BLANK = " "
FLAG_INVALID = "0"
FLAG_VALID = "1"
# The 9100-GETCARD-BYACCTCARD WHEN OTHER operation and the WS-RETURN-MSG X(75) receiver.
READ_OPERATION = "READ"
FILE_ERROR_MESSAGE_LENGTH = 75
STATUS_EDIT_FAILED = HTTP_EDIT_FAILURE
STATUS_NOT_FOUND = HTTP_NOT_FOUND
STATUS_FILE_ERROR = HTTP_FILE_ERROR

# WS-FILE-ERROR-MESSAGE (lines 102-121): fixed literals and the widths of its variable parts.
FILE_ERROR_PREFIX = "File Error: "
FILE_ERROR_ON = " on "
FILE_ERROR_RETURNED = " returned RESP "
FILE_ERROR_RESP2 = ",RESP2 "
FILE_ERROR_TRAILER = "     "
ERROR_OPNAME_LENGTH = 8
ERROR_FILE_LENGTH = 9
ERROR_RESP_LENGTH = 10
# WS-RESP-CD and WS-REAS-CD are S9(09) COMP: the MOVE to X(10) shows 9 unsigned digits.
RESP_CODE_MODULUS = 10**9
# 2200-EDIT-MAP-INPUTS: a field holding only '*' (space-padded) is treated as not entered.
BLANK_KEY_MARKER = "*"


@dataclass(frozen=True)
class FieldEdit:
    """The outcome of one key edit: its filter flag, the validated key and its message.

    ``value`` is the key text when the flag is valid and "" otherwise, as the edits move
    ZEROES to CDEMO-ACCT-ID or CDEMO-CARD-NUM for a blank or invalid key. ``message`` is ""
    when the edit sets none.
    """

    flag: str
    value: str
    message: str = ""


@dataclass(frozen=True)
class KeyRule:
    """The BMS width and the two literals of one numeric key edit (2210 or 2220)."""

    length: int
    notProvidedMessage: str
    filterMessage: str


ACCOUNT_KEY_RULE = KeyRule(ACCT_ID_LENGTH, ACCOUNT_NOT_PROVIDED_MESSAGE, ACCOUNT_FILTER_MESSAGE)
CARD_KEY_RULE = KeyRule(CARD_NUM_LENGTH, CARD_NOT_PROVIDED_MESSAGE, CARD_FILTER_MESSAGE)


@dataclass(frozen=True)
class CardKeyEdit:
    """The outcome of 2200-EDIT-MAP-INPUTS over the account and card keys.

    ``acctId`` and ``cardNum`` hold the validated keys ("" for a key that failed).
    ``fieldErrors`` lists every failing key in source order and ``cursorField`` is the field
    1300-SETUP-SCREEN-ATTRS positions the cursor on.
    """

    acctId: str
    cardNum: str
    acctFlag: str
    cardFlag: str
    message: str
    fieldErrors: tuple[str, ...]
    cursorField: str

    def IsValid(self) -> bool:
        """Return True when both FLG-ACCTFILTER-ISVALID and FLG-CARDFILTER-ISVALID hold."""
        return self.acctFlag == FLAG_VALID and self.cardFlag == FLAG_VALID


def BuildFileErrorMessage(opName: str, fileName: str, resp: int, resp2: int) -> str:
    """Port of WS-FILE-ERROR-MESSAGE (lines 102-121) as moved into WS-RETURN-MSG X(75).

    The operation and file name are left-justified in X(8) and X(9). RESP and RESP2 are the
    S9(09) COMP codes moved into X(10) fields: nine unsigned digits and a trailing space. The
    80-byte group is truncated to 75 and right-trimmed for display.
    """
    fullMessage = (
        FILE_ERROR_PREFIX
        + opName.ljust(ERROR_OPNAME_LENGTH)[:ERROR_OPNAME_LENGTH]
        + FILE_ERROR_ON
        + fileName.ljust(ERROR_FILE_LENGTH)[:ERROR_FILE_LENGTH]
        + FILE_ERROR_RETURNED
        + FormatRespCode(resp)
        + FILE_ERROR_RESP2
        + FormatRespCode(resp2)
        + FILE_ERROR_TRAILER
    )
    return fullMessage[:FILE_ERROR_MESSAGE_LENGTH].rstrip(" ")


def FormatRespCode(code: int) -> str:
    """Return a CICS response code as ERROR-RESP shows it: nine digits, then a space."""
    return f"{abs(code) % RESP_CODE_MODULUS:09d}".ljust(ERROR_RESP_LENGTH)


def PadScreenText(text: str, length: int) -> str:
    """Return the field text right-padded with spaces to its BMS length, as RECEIVE MAP gives it.

    Longer text is kept whole, so a key edit sees the wrong length and fails it.
    """
    return text.ljust(length)


def IsBlankKey(text: str) -> bool:
    """Port of the 2200-EDIT-MAP-INPUTS ``'*' OR SPACES -> LOW-VALUES`` test (lines 614-627).

    True for empty text, all spaces, or a single ``*`` followed only by spaces.
    """
    return text.rstrip(" ") in ("", BLANK_KEY_MARKER)


def IsAsciiDigits(text: str) -> bool:
    """Return True when text is non-empty and every character is 0-9 (COBOL IS NUMERIC).

    ``str.isdigit`` alone would also accept non-ASCII digits such as superscripts.
    """
    return text.isascii() and text.isdigit()


def EditNumericKey(keyText: str, rule: KeyRule) -> FieldEdit:
    """The shared body of 2210-EDIT-ACCOUNT and 2220-EDIT-CARD.

    Not entered, all spaces or all zeros (``CC-ACCT-ID-N EQUAL ZEROS``) is blank with the
    not-provided literal. Anything but ``rule.length`` ASCII digits on the space-padded field
    (``IS NOT NUMERIC``, so a short key fails) is invalid with the filter literal.
    """
    paddedText = PadScreenText(keyText, rule.length)
    if IsBlankKey(keyText) or paddedText == "0" * rule.length:
        return FieldEdit(flag=FLAG_BLANK, value="", message=rule.notProvidedMessage)
    if len(paddedText) != rule.length or not IsAsciiDigits(paddedText):
        return FieldEdit(flag=FLAG_INVALID, value="", message=rule.filterMessage)
    return FieldEdit(flag=FLAG_VALID, value=paddedText)


def EditAccountKey(acctText: str) -> FieldEdit:
    """Port of 2210-EDIT-ACCOUNT (lines 647-679): an 11-digit, non-zero account number."""
    return EditNumericKey(acctText, ACCOUNT_KEY_RULE)


def EditCardKey(cardText: str) -> FieldEdit:
    """Port of 2220-EDIT-CARD (lines 685-720): a 16-digit, non-zero card number."""
    return EditNumericKey(cardText, CARD_KEY_RULE)


def EditCardKeys(acctText: str, cardText: str) -> CardKeyEdit:
    """Port of 2200-EDIT-MAP-INPUTS (lines 608-641) in all-field mode, with the 1300 cursor.

    Both edits run, account first. The first edit message is kept, because each edit sets
    its message only while WS-RETURN-MSG-OFF holds; when both keys are blank the unconditional
    cross-field SET gives ``No input received``. The cursor goes to the first failing key,
    else to the account (1300-SETUP-SCREEN-ATTRS, lines 515-524).
    """
    keyEdits = (
        (ACCT_ID_FIELD, EditAccountKey, acctText),
        (CARD_NUM_FIELD, EditCardKey, cardText),
    )
    results = [(fieldName, editFunction(keyText)) for fieldName, editFunction, keyText in keyEdits]
    accountEdit, cardEdit = (edit for _, edit in results)
    fieldErrors = tuple(fieldName for fieldName, edit in results if edit.flag != FLAG_VALID)
    message = next((edit.message for _, edit in results if edit.message), "")
    if accountEdit.flag == FLAG_BLANK and cardEdit.flag == FLAG_BLANK:
        message = NO_INPUT_RECEIVED_MESSAGE
    return CardKeyEdit(
        acctId=accountEdit.value,
        cardNum=cardEdit.value,
        acctFlag=accountEdit.flag,
        cardFlag=cardEdit.flag,
        message=message,
        fieldErrors=fieldErrors,
        cursorField=next(iter(fieldErrors), ACCT_ID_FIELD),
    )


def IsListEntry(cardKey: str, acctId: str) -> bool:
    """Return True for the entry from the card list: a ``card_ref`` and a list row's account.

    COCRDLIC passes the selected row's WS-ROW-ACCTNO and WS-ROW-CARD-NUM in CDEMO-ACCT-ID and
    CDEMO-CARD-NUM (cbl/COCRDLIC.cbl lines 531-534 for S, 559-562 for U), and both COCRDSLC
    (lines 337-348) and COCRDUPC (cbl/COCRDUPC.cbl lines 482-497) read the card with those
    keys without editing them. Only a ``card_ref`` path value with an account of exactly 11
    ASCII digits, zeros included, has that shape; with any other account the keys are not list
    keys and run the key edits.
    """
    isCardRef = len(cardKey) > CARD_NUM_LENGTH
    return isCardRef and len(acctId) == ACCT_ID_LENGTH and IsAsciiDigits(acctId)


def ListEntryKeys(acctId: str, cardText: str) -> CardKeyEdit:
    """Return the card-list keys as lines 339-342 take them: INPUT-OK, with no key edit.

    CDEMO-ACCT-ID and CDEMO-CARD-NUM move to CC-ACCT-ID-N and CC-CARD-NUM-N as COCRDLIC sent
    them, so both filter flags are valid, no message is set and the cursor is the account.
    """
    return CardKeyEdit(
        acctId=acctId,
        cardNum=cardText,
        acctFlag=FLAG_VALID,
        cardFlag=FLAG_VALID,
        message="",
        fieldErrors=(),
        cursorField=ACCT_ID_FIELD,
    )


def DecodeCardKey(cardKey: str, settings: "Settings") -> str | None:
    """Return the card text of the ``{cardNum}`` path value, replacing COMMAREA CDEMO-CARD-NUM.

    A value of up to 16 characters is the CARDSID text as keyed and is returned unchanged for
    the key edits. A longer value is a ``card_ref`` token. None answers a token that is invalid
    or expired, or that does not carry a card key of at most 16 characters (such as a CCLI page
    key).
    """
    if len(cardKey) <= CARD_NUM_LENGTH:
        return cardKey
    try:
        cardText = DecodeCardRef(cardKey, settings)
    except InvalidToken:
        return None
    if len(cardText) > CARD_NUM_LENGTH:
        return None
    return cardText


def ResolveCardKey(cardKey: str, settings: "Settings") -> str:
    """Return the card text ``DecodeCardKey`` finds in the ``{cardNum}`` path value.

    A path value carrying no card key raises the 404 ``Did not find cards for this search
    condition``.
    """
    cardText = DecodeCardKey(cardKey, settings)
    if cardText is None:
        raise NotFoundError()
    return cardText


def NotFoundError() -> LegacyMessageError:
    """Port of 9100-GETCARD-BYACCTCARD WHEN DFHRESP(NOTFND) (lines 755-761).

    Both filter flags are set NOT-OK, so both keys are failing fields and the cursor goes to
    the account; the information line falls back to the key prompt (lines 490-492).
    """
    return LegacyMessageError(
        CARD_NOT_FOUND_MESSAGE,
        field=ACCT_ID_FIELD,
        httpStatus=STATUS_NOT_FOUND,
        details=LegacyErrorDetails(
            infoMessage=PROMPT_FOR_KEYS_INFO, fieldErrors=(ACCT_ID_FIELD, CARD_NUM_FIELD)
        ),
    )


def FileErrorFromRepository(error: RepositoryError) -> LegacyMessageError:
    """Port of 9100-GETCARD-BYACCTCARD WHEN OTHER (lines 762-771): the READ file-error text.

    The account flag is set NOT-OK, so the account is the failing field and the cursor.
    """
    message = BuildFileErrorMessage(
        READ_OPERATION, error.fileName or CARD_FILE_NAME, error.resp, error.resp2
    )
    return LegacyMessageError(
        message,
        field=ACCT_ID_FIELD,
        httpStatus=STATUS_FILE_ERROR,
        details=LegacyErrorDetails(infoMessage=PROMPT_FOR_KEYS_INFO, fieldErrors=(ACCT_ID_FIELD,)),
    )


def ExpiryParts(expirationDate: date | None) -> tuple[str, str, str]:
    """Return (year, month, day) of CARD-EXPIRAION-DATE as its (1:4), (6:2) and (9:2) text.

    The ``DATE`` column replaces the ``YYYY-MM-DD`` text; NULL (a blank date) gives empty parts.
    """
    if expirationDate is None:
        return ("", "", "")
    return (
        f"{expirationDate.year:04d}",
        f"{expirationDate.month:02d}",
        f"{expirationDate.day:02d}",
    )


async def ViewCard(context: OnlineContext, cardKey: str, acctId: str) -> CardDetailResponse:
    """Port of the CCDL entry from the card list and of its ENTER: 9000-READ-DATA, 1000-SEND-MAP.

    ``cardKey`` is the ``{cardNum}`` path value, a keyed card number or a ``card_ref`` token,
    and ``acctId`` the account. A ``card_ref`` sent with its list row's account is the
    card-list entry (lines 337-348): both keys are taken as the list sent them and the card is
    read without 2200-EDIT-MAP-INPUTS, so a card keyed all zeros opens. Any other keys, typed
    ones included, run 2000-PROCESS-INPUTS: both key edits in all-field mode before the read.

    Raises:
        LegacyMessageError: 404 ``Did not find cards for this search condition`` for an
            invalid or expired ``card_ref``, one carrying no card key, or a card that does not
            exist; 400 with the key edit literal, never for the card-list entry; 503 with the
            CARDDAT READ file-error text.
    """
    cardText = ResolveCardKey(cardKey, context.settings)
    if IsListEntry(cardKey, acctId):
        keys = ListEntryKeys(acctId, cardText)
    else:
        keys = EditCardKeys(acctId, cardText)
        RaiseKeyEditError(keys)
    card = await ReadCardOrRaise(context, keys.cardNum)
    return BuildCardDetail(card, keys, context.settings)


def RaiseKeyEditError(keys: CardKeyEdit) -> None:
    """Raise the 400 of a failed 2200-EDIT-MAP-INPUTS (INPUT-ERROR) with the key prompt."""
    if keys.IsValid():
        return
    raise LegacyMessageError(
        keys.message,
        field=keys.cursorField,
        httpStatus=STATUS_EDIT_FAILED,
        details=LegacyErrorDetails(infoMessage=PROMPT_FOR_KEYS_INFO, fieldErrors=keys.fieldErrors),
    )


async def ReadCardOrRaise(context: OnlineContext, cardNum: str) -> Mapping[str, Any]:
    """Port of 9100-GETCARD-BYACCTCARD (lines 736-773): READ FILE(CARDDAT) by card number only.

    NOTFND raises ``NotFoundError`` and any other file condition ``FileErrorFromRepository``.
    """
    try:
        card = await CardRepository(context).GetCard(cardNum)
    except RepositoryError as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, READ_OPERATION, error)
        raise FileErrorFromRepository(error) from error
    if card is None:
        raise NotFoundError()
    return card


def BuildCardDetail(
    card: Mapping[str, Any], keys: CardKeyEdit, settings: "Settings"
) -> CardDetailResponse:
    """Port of 1200-SETUP-SCREEN-VARS (lines 457-497) for FOUND-CARDS-FOR-ACCOUNT.

    The account is shown as entered, never the card's own account. The card number leaves
    only masked and inside its ``card_ref`` token, the embossed name as stored, and the expiry
    as month and year (the map has no day field). The CVV is never read into the response.
    The cursor is the account (1300 WHEN OTHER).
    """
    cardNum = card["card_num"]
    expiryYear, expiryMonth, _ = ExpiryParts(card["expiration_date"])
    return CardDetailResponse(
        acct_id=keys.acctId,
        card_ref=EncodeCardRef(cardNum, settings),
        card_num_masked=MaskCardNumber(cardNum),
        embossed_name=card["embossed_name"],
        active_status=card["active_status"],
        expiry_month=expiryMonth,
        expiry_year=expiryYear,
        message="",
        info_message=DETAILS_SHOWN_INFO,
        field_errors=[],
        cursor_field=ACCT_ID_FIELD,
    )
