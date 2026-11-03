"""Replaces: COACTUPC (CAUP), BMS COACTUP, copybooks CVACT01Y, CVCUS01Y, CSLKPCDY, CSUTLDPY

The CAUP account update, served as ``GET /api/v1/accounts/{acctId}/edit``
(``FetchAccountForEdit``) and ``PUT /api/v1/accounts/{acctId}`` (``ProcessAccountUpdate``, one
ENTER, F5 or F12). The ``screen_token`` replaces the COMMAREA extension ACCT-UPDATE-SCREEN-DATA:
the ACUP-CHANGE-ACTION state, the account and customer keys, the ACUP-OLD-* before-image and,
once validated, the ACUP-NEW-* values. A missing, forged or expired token, or one issued for
another account than the path ``acctId``, is DETAILS_NOT_FETCHED. Only the fields the token's
state leaves editable are read from the request; every protected value comes from the token, as
a protected 3270 field returns only what was sent to it:

========================  =====================  ======================  ======================
State before              Fields read            ENTER                   F5 save / F12 cancel
========================  =====================  ======================  ======================
DETAILS_NOT_FETCHED       account id             key edit, then read     as ENTER / as ENTER
SHOW_DETAILS, NOT_OK      every field 3320 opens compare, then edits     as ENTER / re-read
CHANGES_OK_NOT_CONFIRMED  none                   screen kept             rewrite / re-read
CHANGES_DONE, FAILED      none                   empty key screen        the same / the same
========================  =====================  ======================  ======================

F5 runs 9600-WRITE-PROCESSING in two write units. Both records are locked, account first, and
compared with the before-image before either rewrite. The ACCTDAT unit commits its rewrite
before the CUSTDAT rewrite starts, so a failed, cancelled or interrupted customer rewrite leaves
the account change in place, as RECOVERY(NONE) made SYNCPOINT ROLLBACK undo nothing.

Screen text a 3270 left blank, or keyed as ``*``, is LOW-VALUES in the source; here it is "" and
it is stored as "". Every message, its precedence (the first message set wins while the field
flags of every failing edit are kept) and every write effect follow the COBOL paragraphs each
function names.
"""

import logging
import string
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from types import MappingProxyType
from typing import Any, NoReturn

from cryptography.fernet import InvalidToken
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import WriteUnit
from app.core.dependencies import OnlineContext
from app.core.errors import (
    HTTP_EDIT_FAILURE,
    HTTP_FILE_ERROR,
    HTTP_RECORD_CHANGED,
    HTTP_RECORD_LOCKED,
    LegacyErrorDetails,
    LegacyMessageError,
    LogFileCondition,
    RepositoryError,
)
from app.core.security import DecodeScreenToken, EncodeScreenToken
from app.repositories.account_repository import ACCOUNT_FILE_NAME, AccountRepository
from app.repositories.customer_repository import CUSTOMER_FILE_NAME, CustomerRepository
from app.schemas.accounts import AccountScreen, AccountScreenFields, AccountUpdateRequest
from app.schemas.common import UpdateAction, UpdateScreenState
from app.services.accounts.coactvwc import (
    ACCOUNT_ID_FIELD,
    ACCOUNT_KEY_LENGTH,
    FLAG_BLANK,
    FLAG_VALID,
    AccountReadResult,
    EditAccountField,
    FitReturnMessage,
    IsAsciiDigits,
    IsBlankScreenInput,
    IsoDateText,
    ReadAccountChain,
)
from app.utils.cobol_format import ACCOUNT_AMOUNT_PICTURE, FormatEdited
from app.utils.cobol_intrinsics import Numval, NumvalC, TestNumvalC
from app.utils.date_validation import (
    DateEditResult,
    DatePartStatus,
    EditDateCcyymmdd,
    EditDateOfBirth,
)
from app.utils.decimal_utils import StoreInto
from app.utils.lookup_codes import (
    IsValidNanpAreaCode,
    IsValidUsStateCode,
    IsValidUsStateZipPrefix,
)

LOGGER = logging.getLogger(__name__)

# LIT-THISPGM and LIT-THISTRANID.
PROGRAM_NAME = "COACTUPC"
TRANSACTION_ID = "CAUP"
# The CICS commands of 9600-WRITE-PROCESSING, as its logged file conditions name them.
READ_UPDATE_OPERATION = "READ UPDATE"
REWRITE_OPERATION = "REWRITE"
# WS-EDIT-VARIABLE-NAME PIC X(25): a longer field label is cut before TRIM puts it in a message.
EDIT_VARIABLE_NAME_LENGTH = 25
# WS-EDIT-CURRENCY-9-2-F, the picture every COACTUP amount is shown in.
SCREEN_AMOUNT_PICTURE = ACCOUNT_AMOUNT_PICTURE
HTTP_OK = HTTPStatus.OK

# WS-RETURN-MSG texts set on a reachable path. The 88-level texts no statement sets, such as
# ACCT-STATUS-MUST-BE-YES-NO and CRED-LIMIT-IS-BLANK, are deliberately absent, and so is the
# " can have numbers or alphabets only." suffix of 1230/1240, which no PERFORM reaches.
MSG_ACCOUNT_NOT_PROVIDED = "Account number not provided"
MSG_NO_INPUT_RECEIVED = "No input received"
MSG_ACCOUNT_KEY_INVALID = "Account Number if supplied must be a 11 digit Non-Zero Number"
MSG_NO_CHANGES = "No change detected with respect to values fetched."
MSG_LOCK_ACCOUNT = "Could not lock account record for update"
MSG_LOCK_CUSTOMER = "Could not lock customer record for update"
MSG_RECORD_CHANGED = "Record changed by some one else. Please review"
MSG_UPDATE_FAILED = "Update of record failed"
MSG_INVALID_ZIP_FOR_STATE = "Invalid zip code for state"

# The suffixes 1215-1280 STRING after TRIM(WS-EDIT-VARIABLE-NAME). The CSUTLDPY date suffixes
# come from app.utils.date_validation with each DateEditResult.
MUST_BE_SUPPLIED_SUFFIX = " must be supplied."
MUST_BE_YES_NO_SUFFIX = " must be Y or N."
ALPHA_ONLY_SUFFIX = " can have alphabets only."
ALL_NUMERIC_SUFFIX = " must be all numeric."
NOT_ZERO_SUFFIX = " must not be zero."
NOT_VALID_SUFFIX = " is not valid"
AREA_CODE_REQUIRED_SUFFIX = ": Area code must be supplied."
AREA_CODE_NOT_NUMERIC_SUFFIX = ": Area code must be A 3 digit number."
AREA_CODE_ZERO_SUFFIX = ": Area code cannot be zero"
AREA_CODE_NOT_NANP_SUFFIX = ": Not valid North America general purpose area code"
PREFIX_CODE_REQUIRED_SUFFIX = ": Prefix code must be supplied."
PREFIX_CODE_NOT_NUMERIC_SUFFIX = ": Prefix code must be A 3 digit number."
PREFIX_CODE_ZERO_SUFFIX = ": Prefix code cannot be zero"
LINE_NUMBER_REQUIRED_SUFFIX = ": Line number code must be supplied."
LINE_NUMBER_NOT_NUMERIC_SUFFIX = ": Line number code must be A 4 digit number."
LINE_NUMBER_ZERO_SUFFIX = ": Line number code cannot be zero"
SSN_PART1_INVALID_SUFFIX = ": should not be 000, 666, or between 900 and 999"
STATE_CODE_INVALID_SUFFIX = ": is not a valid state code"
FICO_RANGE_SUFFIX = ": should be between 300 and 850"

# WS-INFO-MSG 88 levels. FOUND-ACCOUNT-DATA is set by 1200 but always replaced by
# 3250-SETUP-INFOMSG before the screen is sent, so it is never a final information line.
INFO_FOUND_ACCOUNT_DATA = "Details of selected account shown above"
INFO_PROMPT_FOR_SEARCH_KEYS = "Enter or update id of account to update"
INFO_PROMPT_FOR_CHANGES = "Update account details presented above."
INFO_PROMPT_FOR_CONFIRMATION = "Changes validated.Press F5 to save"
INFO_CONFIRM_UPDATE_SUCCESS = "Changes committed to database"
INFO_INFORM_FAILURE = "Changes unsuccessful. Please try again"

# 3250-SETUP-INFOMSG: the information line of each ACUP-CHANGE-ACTION state.
INFO_BY_STATE: Mapping[UpdateScreenState, str] = MappingProxyType(
    {
        UpdateScreenState.DETAILS_NOT_FETCHED: INFO_PROMPT_FOR_SEARCH_KEYS,
        UpdateScreenState.SHOW_DETAILS: INFO_PROMPT_FOR_CHANGES,
        UpdateScreenState.CHANGES_NOT_OK: INFO_PROMPT_FOR_CHANGES,
        UpdateScreenState.CHANGES_OK_NOT_CONFIRMED: INFO_PROMPT_FOR_CONFIRMATION,
        UpdateScreenState.CHANGES_DONE: INFO_CONFIRM_UPDATE_SUCCESS,
        UpdateScreenState.CHANGES_FAILED: INFO_INFORM_FAILURE,
    }
)
# The states whose screen shows fetched details, so their token carries the before-image.
FETCHED_STATES = (
    UpdateScreenState.SHOW_DETAILS,
    UpdateScreenState.CHANGES_NOT_OK,
    UpdateScreenState.CHANGES_OK_NOT_CONFIRMED,
)
# 88 ACUP-CHANGES-OKAYED-AND-DONE and ACUP-CHANGES-FAILED: the next key starts afresh.
FINISHED_STATES = (UpdateScreenState.CHANGES_DONE, UpdateScreenState.CHANGES_FAILED)

# The schemas.accounts AccountScreenFields attributes that field_errors and cursor_field name.
STATUS_FIELD = "active_status"
GROUP_FIELD = "group_id"
CUSTOMER_ID_FIELD = "cust_id"
STATE_FIELD = "addr_state_cd"
ZIP_FIELD = "addr_zip"
COUNTRY_FIELD = "addr_country_cd"
FICO_FIELD = "fico_credit_score"

# Every COACTUP input with its BMS LENGTH, in map order.
SCREEN_FIELD_LENGTHS: Mapping[str, int] = MappingProxyType(
    {
        ACCOUNT_ID_FIELD: 11,
        STATUS_FIELD: 1,
        "open_year": 4,
        "open_month": 2,
        "open_day": 2,
        "credit_limit": 15,
        "expiration_year": 4,
        "expiration_month": 2,
        "expiration_day": 2,
        "cash_credit_limit": 15,
        "reissue_year": 4,
        "reissue_month": 2,
        "reissue_day": 2,
        "curr_balance": 15,
        "curr_cycle_credit": 15,
        GROUP_FIELD: 10,
        "curr_cycle_debit": 15,
        CUSTOMER_ID_FIELD: 9,
        "ssn_part_1": 3,
        "ssn_part_2": 2,
        "ssn_part_3": 4,
        "dob_year": 4,
        "dob_month": 2,
        "dob_day": 2,
        FICO_FIELD: 3,
        "first_name": 25,
        "middle_name": 25,
        "last_name": 25,
        "addr_line_1": 50,
        STATE_FIELD: 2,
        "addr_line_2": 50,
        ZIP_FIELD: 5,
        "addr_line_3": 50,
        COUNTRY_FIELD: 3,
        "phone_num_1_area": 3,
        "phone_num_1_prefix": 3,
        "phone_num_1_line": 4,
        "govt_issued_id": 20,
        "phone_num_2_area": 3,
        "phone_num_2_prefix": 3,
        "phone_num_2_line": 4,
        "eft_account_id": 10,
        "primary_holder_ind": 1,
    }
)
OPEN_DATE_FIELDS = ("open_year", "open_month", "open_day")
EXPIRY_DATE_FIELDS = ("expiration_year", "expiration_month", "expiration_day")
REISSUE_DATE_FIELDS = ("reissue_year", "reissue_month", "reissue_day")
BIRTH_DATE_FIELDS = ("dob_year", "dob_month", "dob_day")
SSN_FIELDS = ("ssn_part_1", "ssn_part_2", "ssn_part_3")
PHONE_1_FIELDS = ("phone_num_1_area", "phone_num_1_prefix", "phone_num_1_line")
PHONE_2_FIELDS = ("phone_num_2_area", "phone_num_2_prefix", "phone_num_2_line")
# BMS JUSTIFY=(RIGHT): RECEIVE MAP right-justifies the text and pads it on the left.
RIGHT_JUSTIFIED_FIELDS = frozenset(
    OPEN_DATE_FIELDS
    + EXPIRY_DATE_FIELDS
    + REISSUE_DATE_FIELDS
    + BIRTH_DATE_FIELDS
    + PHONE_1_FIELDS
    + PHONE_2_FIELDS
)
# The S9(10)V99 amounts. Their field names are also their accounts.accounts column names.
AMOUNT_FIELDS = (
    "credit_limit",
    "cash_credit_limit",
    "curr_balance",
    "curr_cycle_credit",
    "curr_cycle_debit",
)
# 3300-SETUP-SCREEN-ATTRS cursor order: field_errors are listed and the cursor placed in it.
FIELD_ORDER = (
    (ACCOUNT_ID_FIELD, STATUS_FIELD)
    + OPEN_DATE_FIELDS
    + ("credit_limit",)
    + EXPIRY_DATE_FIELDS
    + ("cash_credit_limit",)
    + REISSUE_DATE_FIELDS
    + ("curr_balance", "curr_cycle_credit", "curr_cycle_debit")
    + SSN_FIELDS
    + BIRTH_DATE_FIELDS
    + (FICO_FIELD, "first_name", "middle_name", "last_name", "addr_line_1")
    + (STATE_FIELD, ZIP_FIELD, "addr_line_3", COUNTRY_FIELD)
    + PHONE_1_FIELDS
    + PHONE_2_FIELDS
    + ("eft_account_id", "primary_holder_ind")
)
# 3320-UNPROTECT-FEW-ATTRS opens every detail field except ACSTNUM and ACSCTRY (DFHBMPRF).
DETAIL_EDITABLE_FIELDS = frozenset(SCREEN_FIELD_LENGTHS) - {
    ACCOUNT_ID_FIELD,
    CUSTOMER_ID_FIELD,
    COUNTRY_FIELD,
}
# 3300-SETUP-SCREEN-ATTRS: the fields each state leaves unprotected (DFHBMFSE).
EDITABLE_FIELDS_BY_STATE: Mapping[UpdateScreenState, frozenset[str]] = MappingProxyType(
    {
        UpdateScreenState.DETAILS_NOT_FETCHED: frozenset({ACCOUNT_ID_FIELD}),
        UpdateScreenState.SHOW_DETAILS: DETAIL_EDITABLE_FIELDS,
        UpdateScreenState.CHANGES_NOT_OK: DETAIL_EDITABLE_FIELDS,
        UpdateScreenState.CHANGES_OK_NOT_CONFIRMED: frozenset(),
        UpdateScreenState.CHANGES_DONE: frozenset(),
        UpdateScreenState.CHANGES_FAILED: frozenset({ACCOUNT_ID_FIELD}),
    }
)
# 1205-COMPARE-OLD-NEW compares these fields as UPPER-CASE(TRIM(new)) = UPPER-CASE(TRIM(old));
# every other field, the amounts apart, is compared exactly at its fixed width.
UPPER_TRIM_COMPARE_FIELDS = frozenset(
    {
        STATUS_FIELD,
        GROUP_FIELD,
        CUSTOMER_ID_FIELD,
        "first_name",
        "middle_name",
        "last_name",
        "addr_line_1",
        "addr_line_2",
        "addr_line_3",
        STATE_FIELD,
        COUNTRY_FIELD,
        ZIP_FIELD,
        "govt_issued_id",
        "primary_holder_ind",
    }
)

# The before-image (ACUP-OLD-DETAILS) is held as text by column name: amounts as plain decimal
# strings, dates as YYYY-MM-DD ("" for NULL), the FICO score as its 9(03) digits.
ACCOUNT_TEXT_COLUMNS = (ACCOUNT_ID_FIELD, STATUS_FIELD, GROUP_FIELD)
ACCOUNT_DATE_FIELDS: Mapping[str, tuple[str, str, str]] = MappingProxyType(
    {
        "open_date": OPEN_DATE_FIELDS,
        "expiration_date": EXPIRY_DATE_FIELDS,
        "reissue_date": REISSUE_DATE_FIELDS,
    }
)
CUSTOMER_TEXT_COLUMNS = (
    CUSTOMER_ID_FIELD,
    "first_name",
    "middle_name",
    "last_name",
    "addr_line_1",
    "addr_line_2",
    "addr_line_3",
    STATE_FIELD,
    COUNTRY_FIELD,
    ZIP_FIELD,
    "phone_num_1",
    "phone_num_2",
    "ssn",
    "govt_issued_id",
    "eft_account_id",
    "primary_holder_ind",
)
BIRTH_DATE_COLUMN = "date_of_birth"
IMAGE_COLUMNS = frozenset(
    ACCOUNT_TEXT_COLUMNS
    + AMOUNT_FIELDS
    + tuple(ACCOUNT_DATE_FIELDS)
    + CUSTOMER_TEXT_COLUMNS
    + (BIRTH_DATE_COLUMN, FICO_FIELD)
)
# 3202-SHOW-ORIGINAL-VALUES: screen field -> (before-image column, offset, length) it shows.
# The ZIP keeps all 10 stored characters here, for the compare; the 5-character ACSZIPC field
# shows only the first 5.
IMAGE_FIELD_SOURCES: Mapping[str, tuple[str, int, int]] = MappingProxyType(
    {
        ACCOUNT_ID_FIELD: (ACCOUNT_ID_FIELD, 0, 11),
        STATUS_FIELD: (STATUS_FIELD, 0, 1),
        "open_year": ("open_date", 0, 4),
        "open_month": ("open_date", 5, 2),
        "open_day": ("open_date", 8, 2),
        "expiration_year": ("expiration_date", 0, 4),
        "expiration_month": ("expiration_date", 5, 2),
        "expiration_day": ("expiration_date", 8, 2),
        "reissue_year": ("reissue_date", 0, 4),
        "reissue_month": ("reissue_date", 5, 2),
        "reissue_day": ("reissue_date", 8, 2),
        GROUP_FIELD: (GROUP_FIELD, 0, 10),
        CUSTOMER_ID_FIELD: (CUSTOMER_ID_FIELD, 0, 9),
        "ssn_part_1": ("ssn", 0, 3),
        "ssn_part_2": ("ssn", 3, 2),
        "ssn_part_3": ("ssn", 5, 4),
        "dob_year": (BIRTH_DATE_COLUMN, 0, 4),
        "dob_month": (BIRTH_DATE_COLUMN, 5, 2),
        "dob_day": (BIRTH_DATE_COLUMN, 8, 2),
        FICO_FIELD: (FICO_FIELD, 0, 3),
        "first_name": ("first_name", 0, 25),
        "middle_name": ("middle_name", 0, 25),
        "last_name": ("last_name", 0, 25),
        "addr_line_1": ("addr_line_1", 0, 50),
        STATE_FIELD: (STATE_FIELD, 0, 2),
        "addr_line_2": ("addr_line_2", 0, 50),
        ZIP_FIELD: (ZIP_FIELD, 0, 10),
        "addr_line_3": ("addr_line_3", 0, 50),
        COUNTRY_FIELD: (COUNTRY_FIELD, 0, 3),
        "phone_num_1_area": ("phone_num_1", 1, 3),
        "phone_num_1_prefix": ("phone_num_1", 5, 3),
        "phone_num_1_line": ("phone_num_1", 9, 4),
        "govt_issued_id": ("govt_issued_id", 0, 20),
        "phone_num_2_area": ("phone_num_2", 1, 3),
        "phone_num_2_prefix": ("phone_num_2", 5, 3),
        "phone_num_2_line": ("phone_num_2", 9, 4),
        "eft_account_id": ("eft_account_id", 0, 10),
        "primary_holder_ind": ("primary_holder_ind", 0, 1),
    }
)
# CUST-UPDATE-RECORD columns moved from the screen text as entered (lines 4007-4059).
CUSTOMER_REWRITE_TEXT_FIELDS = (
    "first_name",
    "middle_name",
    "last_name",
    "addr_line_1",
    "addr_line_2",
    "addr_line_3",
    STATE_FIELD,
    COUNTRY_FIELD,
    ZIP_FIELD,
    "govt_issued_id",
    "eft_account_id",
    "primary_holder_ind",
)
PHONE_COLUMN_FIELDS: Mapping[str, tuple[str, str, str]] = MappingProxyType(
    {"phone_num_1": PHONE_1_FIELDS, "phone_num_2": PHONE_2_FIELDS}
)
# The fixed bytes of the CUST-PHONE-NUM STRING: '(' area ')' prefix '-' line.
PHONE_TEXT_PUNCTUATION = ("(", ")", "-")

# The screen_token payload keys. The program name keeps a CCUP (COCRDUPC) token out.
PAYLOAD_PROGRAM_KEY = "pgm"
PAYLOAD_STATE_KEY = "state"
PAYLOAD_ACCOUNT_KEY = "acctId"
PAYLOAD_CUSTOMER_KEY = "custId"
PAYLOAD_OLD_KEY = "old"
PAYLOAD_NEW_KEY = "new"
PAYLOAD_TEXTS_KEY = "texts"
PAYLOAD_AMOUNTS_KEY = "amounts"
SCREEN_STATE_VALUES = frozenset(screenState.value for screenState in UpdateScreenState)

# 88 FLG-YES-NO-ISVALID VALUES 'Y', 'N'; X(1) LOW-VALUES, SPACES or ZEROS is "not supplied".
YES_NO_VALUES = ("Y", "N")
YES_NO_NOT_SUPPLIED = (" ", "0")
# LIT-ALL-ALPHA-FROM-X: the 52 letters 1225 and 1235 convert to spaces before the TRIM test.
ALPHA_CHARACTERS = frozenset(string.ascii_letters + " ")
# 88 INVALID-SSN-PART1 VALUES 0, 666, 900 THRU 999.
INVALID_SSN_PART1_VALUES = frozenset({0, 666}) | frozenset(range(900, 1000))
# 88 FICO-RANGE-IS-VALID VALUES 300 THROUGH 850.
VALID_FICO_RANGE = range(300, 851)
SSN_PART_LABELS = ("SSN: First 3 chars", "SSN 4th & 5th chars", "SSN Last 4 chars")
# FUNCTION UPPER-CASE and LOWER-CASE of single-byte text change only the 26 unaccented letters.
ASCII_UPPER = str.maketrans(string.ascii_lowercase, string.ascii_uppercase)
ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
# The DD of a date part: COMPUTE WS-EDIT-DATE-DD-N = NUMVAL(day) stores into unsigned 9(2).
DAY_DIGITS = 2

# The 2000-DECIDE-ACTION outcomes of 9600-WRITE-PROCESSING (lines 2603-2615).
OUTCOME_ACCOUNT_LOCK_FAILED = "account-lock-failed"
OUTCOME_CUSTOMER_LOCK_FAILED = "customer-lock-failed"
OUTCOME_CHANGED = "changed"
OUTCOME_UPDATE_FAILED = "update-failed"
OUTCOME_DONE = "done"


def UpperAscii(text: str) -> str:
    """FUNCTION UPPER-CASE: a-z become A-Z and nothing else changes."""
    return text.translate(ASCII_UPPER)


def LowerAscii(text: str) -> str:
    """FUNCTION LOWER-CASE: A-Z become a-z and nothing else changes."""
    return text.translate(ASCII_LOWER)


def AmountValue(text: str) -> Decimal:
    """Return a before-image amount string as the exact S9(10)V99 value it holds."""
    return Decimal(text)


# 9700-CHECK-CHANGE-IN-REC (lines 4115-4191): each before-image column with the normaliser its
# compare applies. Account status, amounts and dates are exact; the group is compared
# lower-cased; names, address lines, state, country and government id upper-cased, not trimmed;
# ZIP, both phones, SSN, birth date, EFT id, card holder flag and FICO score exactly.
RECORD_CHANGE_RULES: tuple[tuple[tuple[str, ...], Callable[[str], object]], ...] = (
    ((STATUS_FIELD, "open_date", "expiration_date", "reissue_date"), str),
    (AMOUNT_FIELDS, AmountValue),
    ((GROUP_FIELD,), LowerAscii),
    (
        (
            "first_name",
            "middle_name",
            "last_name",
            "addr_line_1",
            "addr_line_2",
            "addr_line_3",
            STATE_FIELD,
            COUNTRY_FIELD,
            "govt_issued_id",
        ),
        UpperAscii,
    ),
    (
        (
            ZIP_FIELD,
            "phone_num_1",
            "phone_num_2",
            "ssn",
            BIRTH_DATE_COLUMN,
            "eft_account_id",
            "primary_holder_ind",
            FICO_FIELD,
        ),
        str,
    ),
)


@dataclass(frozen=True)
class NewAccountData:
    """ACUP-NEW-DETAILS: every COACTUP field as 1100-RECEIVE-MAP leaves it, and the amounts.

    ``texts`` holds each field at its BMS length, left-justified, or right-justified for a
    JUSTIFY=(RIGHT) field; "" is a blank field (LOW-VALUES). ``amounts`` holds ACUP-NEW-*-N of
    each amount whose text passes TEST-NUMVAL-C, stored into S9(10)V99, and None otherwise.
    """

    texts: Mapping[str, str]
    amounts: Mapping[str, Decimal | None]


@dataclass(frozen=True)
class AccountUpdateState:
    """WS-THIS-PROGCOMMAREA as the screen token carries it between requests.

    ``acctId`` and ``custId`` are CDEMO-ACCT-ID and CDEMO-CUST-ID once details are fetched.
    ``beforeImage`` (ACUP-OLD-DETAILS by column name) is set in every FETCHED_STATES state, and
    ``newValues`` (the validated ACUP-NEW-DETAILS) only in CHANGES_OK_NOT_CONFIRMED.
    """

    screenState: UpdateScreenState
    acctId: str = ""
    custId: str = ""
    beforeImage: Mapping[str, str] | None = None
    newValues: NewAccountData | None = None


NOT_FETCHED_STATE = AccountUpdateState(UpdateScreenState.DETAILS_NOT_FETCHED)


@dataclass(frozen=True)
class ScreenOutcome:
    """Everything 3000-SEND-MAP puts on the COACTUP screen for one response.

    ``fields`` holds the display text by AccountScreenFields attribute; a missing field is
    blank. A non-blank ``message`` (the ERRMSG line) is sent as an error with ``httpStatus``.
    The information line is always the state's (3250-SETUP-INFOMSG).
    """

    updateState: AccountUpdateState
    fields: Mapping[str, str] = field(default_factory=dict)
    message: str = ""
    httpStatus: int = HTTP_OK
    fieldErrors: tuple[str, ...] = ()
    cursorField: str = ACCOUNT_ID_FIELD


def EncodeUpdateState(updateState: AccountUpdateState, context: OnlineContext) -> str:
    """Return the screen_token of ``updateState``: the COMMON-RETURN move of the COMMAREA.

    Blank values are left out to keep the token small; DecodeUpdateState restores them. The
    token is Fernet-encrypted, so the SSN and the other customer values inside it are never
    readable by the client.
    """
    payload: dict[str, object] = {
        PAYLOAD_PROGRAM_KEY: PROGRAM_NAME,
        PAYLOAD_STATE_KEY: updateState.screenState.value,
        PAYLOAD_ACCOUNT_KEY: updateState.acctId,
        PAYLOAD_CUSTOMER_KEY: updateState.custId,
    }
    if updateState.beforeImage is not None:
        payload[PAYLOAD_OLD_KEY] = NonBlankTexts(updateState.beforeImage)
    if updateState.newValues is not None:
        payload[PAYLOAD_NEW_KEY] = NewValuesPayload(updateState.newValues)
    return EncodeScreenToken(payload, context.settings)


def NonBlankTexts(texts: Mapping[str, str]) -> dict[str, str]:
    """Return the texts that are not blank, without their trailing spaces."""
    return {name: text.rstrip(" ") for name, text in texts.items() if text.strip(" ")}


def NewValuesPayload(newValues: NewAccountData) -> dict[str, object]:
    """Return the validated new values as JSON: texts, and amounts as plain decimal strings."""
    amounts = {
        name: str(amount) for name, amount in newValues.amounts.items() if amount is not None
    }
    return {PAYLOAD_TEXTS_KEY: NonBlankTexts(newValues.texts), PAYLOAD_AMOUNTS_KEY: amounts}


def DecodeUpdateState(screenToken: str, context: OnlineContext) -> AccountUpdateState:
    """Return the state a screen_token carries; DETAILS_NOT_FETCHED restarts the flow.

    A missing or blank token, a forged, tampered or expired one (InvalidToken), another
    program's token and any incomplete payload all give DETAILS_NOT_FETCHED.
    """
    if not screenToken.strip():
        return NOT_FETCHED_STATE
    try:
        payload = DecodeScreenToken(screenToken, context.settings)
    except InvalidToken:
        return NOT_FETCHED_STATE
    return StateFromPayload(payload) or NOT_FETCHED_STATE


def StateFromPayload(payload: Mapping[str, Any]) -> AccountUpdateState | None:
    """Return the AccountUpdateState a decrypted payload describes, or None when it is not one."""
    stateValue = payload.get(PAYLOAD_STATE_KEY)
    keys = (payload.get(PAYLOAD_ACCOUNT_KEY), payload.get(PAYLOAD_CUSTOMER_KEY))
    if payload.get(PAYLOAD_PROGRAM_KEY) != PROGRAM_NAME or not isinstance(stateValue, str):
        return None
    if stateValue not in SCREEN_STATE_VALUES or not all(isinstance(key, str) for key in keys):
        return None
    updateState = AccountUpdateState(
        screenState=UpdateScreenState(stateValue),
        acctId=keys[0],
        custId=keys[1],
        beforeImage=ImageFromPayload(payload.get(PAYLOAD_OLD_KEY)),
        newValues=NewValuesFromPayload(payload.get(PAYLOAD_NEW_KEY)),
    )
    return updateState if IsCompleteState(updateState) else None


def IsTextMapping(value: object, allowedKeys: frozenset[str]) -> bool:
    """Return True for a JSON object whose keys are allowed and whose values are all text."""
    if not isinstance(value, dict) or not allowedKeys.issuperset(value):
        return False
    return all(isinstance(text, str) for text in value.values())


def ParseAmount(text: str) -> Decimal | None:
    """Return the finite decimal a token amount string holds, or None when it holds none."""
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def ImageFromPayload(value: object) -> dict[str, str] | None:
    """Return the before-image of a token, blank columns restored, or None when malformed."""
    if not IsTextMapping(value, IMAGE_COLUMNS):
        return None
    image = {column: value.get(column, "") for column in IMAGE_COLUMNS}
    if any(ParseAmount(image[column]) is None for column in AMOUNT_FIELDS):
        return None
    return image


def NewValuesFromPayload(value: object) -> NewAccountData | None:
    """Return the validated new values of a token, re-padded as received, or None."""
    if not isinstance(value, dict):
        return None
    texts, amounts = value.get(PAYLOAD_TEXTS_KEY), value.get(PAYLOAD_AMOUNTS_KEY)
    if not IsTextMapping(texts, frozenset(SCREEN_FIELD_LENGTHS)):
        return None
    if not IsTextMapping(amounts, frozenset(AMOUNT_FIELDS)):
        return None
    parsedAmounts = {name: ParseAmount(amounts.get(name, "")) for name in AMOUNT_FIELDS}
    if any(amount is None for amount in parsedAmounts.values()):
        return None
    receivedTexts = {name: ReceiveField(name, texts.get(name, "")) for name in SCREEN_FIELD_LENGTHS}
    return NewAccountData(receivedTexts, parsedAmounts)


def IsCompleteState(updateState: AccountUpdateState) -> bool:
    """Return True when a decoded state carries what its screen needs.

    A fetched state needs both keys and the before-image; CHANGES_OK_NOT_CONFIRMED also needs
    the validated values its F5 rewrites.
    """
    if updateState.screenState not in FETCHED_STATES:
        return True
    hasKeys = bool(updateState.acctId and updateState.custId)
    hasDetails = hasKeys and updateState.beforeImage is not None
    if updateState.screenState is UpdateScreenState.CHANGES_OK_NOT_CONFIRMED:
        return hasDetails and updateState.newValues is not None
    return hasDetails


def ReceiveField(fieldName: str, text: str) -> str:
    """Port of one 1100-RECEIVE-MAP move: "" for ``*`` or spaces, else the text at BMS length.

    The text loses its trailing spaces and is padded to the field's BMS length on the right,
    or on the left for a JUSTIFY=(RIGHT) field, as BMS presents a keyed field to the program.
    """
    if IsBlankScreenInput(text):
        return ""
    trimmedText = text.rstrip(" ")
    fieldLength = SCREEN_FIELD_LENGTHS[fieldName]
    if fieldName in RIGHT_JUSTIFIED_FIELDS:
        return trimmedText.rjust(fieldLength)
    return trimmedText.ljust(fieldLength)


def ReceivedAmount(text: str) -> Decimal | None:
    """Port of the amount moves of 1100-RECEIVE-MAP (lines 1072-1140).

    ``COMPUTE ACUP-NEW-*-N = FUNCTION NUMVAL-C(...)`` runs only when TEST-NUMVAL-C passes, and
    stores into S9(10)V99 without ROUNDED, so ``$1,234.567`` gives 1234.56. A blank or
    non-conforming amount has no value (None), which never equals a stored amount.
    """
    if not text or not TestNumvalC(text):
        return None
    return StoreInto(NumvalC(text), 10, 2)


def ReceiveMap(
    screenFields: AccountScreenFields, updateState: AccountUpdateState
) -> NewAccountData:
    """Port of 1100-RECEIVE-MAP once details are shown (lines 1060-1428).

    Only the fields the state leaves editable are read from the request. The protected account
    id, customer id and country, which COACTUP returns as they were sent (DFHBMPRF), come from
    the token's before-image.
    """
    keyedTexts = screenFields.model_dump()
    sourceTexts = ImageFieldTexts(updateState.beforeImage or {})
    editableFields = EDITABLE_FIELDS_BY_STATE[updateState.screenState]
    sourceTexts.update({name: keyedTexts[name] for name in editableFields})
    texts = {name: ReceiveField(name, sourceTexts.get(name, "")) for name in SCREEN_FIELD_LENGTHS}
    amounts = {name: ReceivedAmount(texts[name]) for name in AMOUNT_FIELDS}
    return NewAccountData(texts, amounts)


def StoredText(value: object) -> str:
    """Return a stored X(n) column as text without its trailing spaces (CHAR columns pad)."""
    return str(value).rstrip(" ")


def FicoText(score: int) -> str:
    """Return CUST-FICO-CREDIT-SCORE as the 9(03) digits ACUP-OLD-CUST-FICO-SCORE-X holds."""
    return f"{int(StoreInto(score, 3, 0, signed=False)):03d}"


def AmountText(amount: Decimal) -> str:
    """Return a NUMERIC(12,2) amount as the plain decimal string the before-image holds."""
    return str(StoreInto(amount, 10, 2))


def BeforeImageOf(account: Mapping[str, Any], customer: Mapping[str, Any]) -> dict[str, str]:
    """Port of 9500-STORE-FETCHED-DATA (lines 3801-3884): ACUP-OLD-DETAILS from the records.

    The same image of the records 9600 reads for update is what 9700 compares with it.
    """
    image = {column: StoredText(account[column]) for column in ACCOUNT_TEXT_COLUMNS}
    image.update({column: AmountText(account[column]) for column in AMOUNT_FIELDS})
    image.update({column: IsoDateText(account[column]) for column in ACCOUNT_DATE_FIELDS})
    image.update({column: StoredText(customer[column]) for column in CUSTOMER_TEXT_COLUMNS})
    image[BIRTH_DATE_COLUMN] = IsoDateText(customer[BIRTH_DATE_COLUMN])
    image[FICO_FIELD] = FicoText(customer[FICO_FIELD])
    return image


def ImageFieldTexts(beforeImage: Mapping[str, str]) -> dict[str, str]:
    """Return each non-amount screen field's slice of the before-image (IMAGE_FIELD_SOURCES)."""
    return {
        name: beforeImage.get(column, "")[offset : offset + length]
        for name, (column, offset, length) in IMAGE_FIELD_SOURCES.items()
    }


def DisplayOfImage(beforeImage: Mapping[str, str]) -> dict[str, str]:
    """Port of 3202-SHOW-ORIGINAL-VALUES: the before-image as COACTUP shows it.

    Each text is cut to its map field (the ZIP to its first 5 characters) and the amounts are
    edited as ``+ZZZ,ZZZ,ZZZ.99``. The SSN is shown in full: CAUP edits it.
    """
    shownTexts = {
        name: text[: SCREEN_FIELD_LENGTHS[name]].rstrip(" ")
        for name, text in ImageFieldTexts(beforeImage).items()
    }
    for name in AMOUNT_FIELDS:
        shownTexts[name] = FormatEdited(AmountValue(beforeImage[name]), SCREEN_AMOUNT_PICTURE)
    return shownTexts


def DisplayOfNew(newValues: NewAccountData, failedFields: frozenset[str]) -> dict[str, str]:
    """Port of 3203-SHOW-UPDATED-VALUES: the new values as COACTUP shows them.

    An amount whose flag is valid is edited from its ACUP-NEW-*-N value; a failing or blank one
    shows the text as keyed (ACUP-NEW-*-X).
    """
    shownTexts = {name: text.rstrip(" ") for name, text in newValues.texts.items()}
    for name in AMOUNT_FIELDS:
        amount = newValues.amounts.get(name)
        if amount is not None and name not in failedFields:
            shownTexts[name] = FormatEdited(amount, SCREEN_AMOUNT_PICTURE)
    return shownTexts


def ComparableText(fieldName: str, text: str) -> str:
    """Return a field's text in the form 1205-COMPARE-OLD-NEW compares it.

    UPPER_TRIM_COMPARE_FIELDS compare as UPPER-CASE(TRIM(text)); every other field compares
    exactly at its fixed width.
    """
    # A blank new field is LOW-VALUES in the source, which never equals a stored field of
    # spaces. The tables store both LOW-VALUES and spaces as "", so the two cannot be told apart
    # here; a blank field therefore compares equal to a blank stored field.
    if fieldName in UPPER_TRIM_COMPARE_FIELDS:
        return UpperAscii(text.strip(" "))
    return text.ljust(SCREEN_FIELD_LENGTHS[fieldName])


def CompareOldNew(newValues: NewAccountData, beforeImage: Mapping[str, str]) -> bool:
    """Port of 1205-COMPARE-OLD-NEW (lines 1681-1779): True when CHANGE-HAS-OCCURRED.

    The new values are compared with the token's before-image, never with the previous
    submission. Case-only changes to the UPPER_TRIM_COMPARE_FIELDS are no change. An amount
    with no NUMVAL-C value never equals its stored amount.
    """
    for name, oldText in ImageFieldTexts(beforeImage).items():
        if ComparableText(name, newValues.texts[name]) != ComparableText(name, oldText):
            return True
    return any(newValues.amounts[name] != AmountValue(beforeImage[name]) for name in AMOUNT_FIELDS)


@dataclass
class EditCollector:
    """WS-RETURN-MSG, INPUT-ERROR and the field flags the 1200 edits leave set.

    ``fieldErrors`` lists every failing field once, in the order the edits fail them.
    """

    message: str = ""
    fieldErrors: list[str] = field(default_factory=list)
    inputError: bool = False

    def Fail(self, fieldKeys: tuple[str, ...], text: str) -> None:
        """SET INPUT-ERROR, flag each field, and keep text only while WS-RETURN-MSG-OFF holds."""
        self.inputError = True
        for fieldKey in fieldKeys:
            if fieldKey not in self.fieldErrors:
                self.fieldErrors.append(fieldKey)
        if not self.message:
            self.message = text

    def SortedFieldErrors(self) -> tuple[str, ...]:
        """Return the failing fields in the 3300-SETUP-SCREEN-ATTRS cursor order."""
        return tuple(sorted(self.fieldErrors, key=FIELD_ORDER.index))


@dataclass
class AccountEditContext:
    """The working storage the 1200 edit paragraphs share during one ENTER.

    ``values`` are the ACUP-NEW-* texts as received. ``today`` is FUNCTION CURRENT-DATE for
    EDIT-DATE-OF-BIRTH. ``stateValid`` and ``zipValid`` are FLG-STATE-ISVALID and
    FLG-ZIPCODE-ISVALID, which gate 1280-EDIT-US-STATE-ZIP-CD.
    """

    values: Mapping[str, str]
    collector: EditCollector
    today: date
    stateValid: bool = False
    zipValid: bool = False


@dataclass(frozen=True)
class EditStep:
    """One step of 1200-EDIT-MAP-INPUTS: MOVE label TO WS-EDIT-VARIABLE-NAME, then the edit.

    ``checkLength`` is the WS-EDIT-ALPHANUM-LENGTH (or picture width) the edit tests.
    """

    label: str
    fieldKeys: tuple[str, ...]
    checkLength: int
    editFunction: Callable[["AccountEditContext", "EditStep"], object]


@dataclass(frozen=True)
class PhonePartRule:
    """The EDIT-AREA-CODE, EDIT-US-PHONE-PREFIX or EDIT-US-PHONE-LINENUM tests of one part."""

    length: int
    requiredSuffix: str
    notNumericSuffix: str
    zeroSuffix: str
    checkGeneralPurposeCode: bool = False


PHONE_PART_RULES = (
    PhonePartRule(
        3,
        AREA_CODE_REQUIRED_SUFFIX,
        AREA_CODE_NOT_NUMERIC_SUFFIX,
        AREA_CODE_ZERO_SUFFIX,
        checkGeneralPurposeCode=True,
    ),
    PhonePartRule(
        3, PREFIX_CODE_REQUIRED_SUFFIX, PREFIX_CODE_NOT_NUMERIC_SUFFIX, PREFIX_CODE_ZERO_SUFFIX
    ),
    PhonePartRule(
        4, LINE_NUMBER_REQUIRED_SUFFIX, LINE_NUMBER_NOT_NUMERIC_SUFFIX, LINE_NUMBER_ZERO_SUFFIX
    ),
)


def EditMessage(label: str, suffix: str) -> str:
    """Return ``STRING TRIM(WS-EDIT-VARIABLE-NAME) suffix INTO WS-RETURN-MSG``.

    The label is first cut to X(25), so ``Current Cycle Credit Limit`` reads
    ``Current Cycle Credit Limi``, and the message stops at 75 characters.
    """
    return FitReturnMessage(label[:EDIT_VARIABLE_NAME_LENGTH].strip(" ") + suffix)


def FailStep(editContext: AccountEditContext, step: EditStep, suffix: str) -> None:
    """Fail every field of the step with its label followed by suffix."""
    editContext.collector.Fail(step.fieldKeys, EditMessage(step.label, suffix))


def FieldText(editContext: AccountEditContext, fieldKey: str, length: int) -> str:
    """Return WS-EDIT-ALPHANUM-ONLY(1:length) after the new value is MOVEd into it."""
    return editContext.values[fieldKey].ljust(length)[:length]


def IsBlankText(text: str) -> bool:
    """The ``EQUAL LOW-VALUES OR EQUAL SPACES OR LENGTH(TRIM(...)) = 0`` test of every edit."""
    return text.strip(" ") == ""


def EditMandatory(editContext: AccountEditContext, step: EditStep) -> None:
    """Port of 1215-EDIT-MANDATORY: the field must be supplied."""
    if IsBlankText(FieldText(editContext, step.fieldKeys[0], step.checkLength)):
        FailStep(editContext, step, MUST_BE_SUPPLIED_SUFFIX)


def EditYesNo(editContext: AccountEditContext, step: EditStep) -> None:
    """Port of 1220-EDIT-YESNO: supplied (not LOW-VALUES, spaces or zeros), then Y or N.

    88 FLG-YES-NO-ISVALID holds only for upper-case Y and N.
    """
    text = FieldText(editContext, step.fieldKeys[0], step.checkLength)
    if text in YES_NO_NOT_SUPPLIED:
        FailStep(editContext, step, MUST_BE_SUPPLIED_SUFFIX)
    elif text not in YES_NO_VALUES:
        FailStep(editContext, step, MUST_BE_YES_NO_SUFFIX)


def AlphaRequiredFailure(text: str) -> str:
    """Return the 1225-EDIT-ALPHA-REQD suffix for text, or "" when it passes.

    The paragraph converts every letter to a space, so anything but letters and spaces fails.
    """
    if IsBlankText(text):
        return MUST_BE_SUPPLIED_SUFFIX
    if not ALPHA_CHARACTERS.issuperset(text):
        return ALPHA_ONLY_SUFFIX
    return ""


def EditAlphaRequired(editContext: AccountEditContext, step: EditStep) -> bool:
    """Port of 1225-EDIT-ALPHA-REQD: supplied, letters and spaces only. True when it passes."""
    suffix = AlphaRequiredFailure(FieldText(editContext, step.fieldKeys[0], step.checkLength))
    if suffix:
        FailStep(editContext, step, suffix)
        return False
    return True


def EditAlphaOptional(editContext: AccountEditContext, step: EditStep) -> None:
    """Port of 1235-EDIT-ALPHA-OPT: a blank field passes, otherwise letters and spaces only."""
    text = FieldText(editContext, step.fieldKeys[0], step.checkLength)
    if not IsBlankText(text) and not ALPHA_CHARACTERS.issuperset(text):
        FailStep(editContext, step, ALPHA_ONLY_SUFFIX)


def NumericFailure(text: str) -> str:
    """Return the 1245-EDIT-NUM-REQD suffix for text, or "" when it passes.

    Every checked character must be a digit (``IS NUMERIC``, so an embedded or trailing space
    fails), and ``FUNCTION NUMVAL`` of the digits must not be zero.
    """
    if IsBlankText(text):
        return MUST_BE_SUPPLIED_SUFFIX
    if not IsAsciiDigits(text):
        return ALL_NUMERIC_SUFFIX
    if int(text) == 0:
        return NOT_ZERO_SUFFIX
    return ""


def EditNumericRequired(
    editContext: AccountEditContext, fieldKey: str, label: str, length: int
) -> bool:
    """Port of 1245-EDIT-NUM-REQD on one field under one label. True when it passes."""
    suffix = NumericFailure(FieldText(editContext, fieldKey, length))
    if suffix:
        editContext.collector.Fail((fieldKey,), EditMessage(label, suffix))
        return False
    return True


def EditNumericStep(editContext: AccountEditContext, step: EditStep) -> bool:
    """Run 1245-EDIT-NUM-REQD on the step's field, label and length."""
    return EditNumericRequired(editContext, step.fieldKeys[0], step.label, step.checkLength)


def EditSignedAmount(editContext: AccountEditContext, step: EditStep) -> None:
    """Port of 1250-EDIT-SIGNED-9V2: supplied, then TEST-NUMVAL-C on the text as keyed."""
    text = editContext.values[step.fieldKeys[0]]
    if IsBlankText(text):
        FailStep(editContext, step, MUST_BE_SUPPLIED_SUFFIX)
    elif not TestNumvalC(text):
        FailStep(editContext, step, NOT_VALID_SUFFIX)


def DateTextOf(editContext: AccountEditContext, step: EditStep) -> str:
    """Return WS-EDIT-DATE-CCYYMMDD: the year, month and day parts of ACUP-NEW-*-DATE."""
    return "".join(
        editContext.values[fieldKey].ljust(SCREEN_FIELD_LENGTHS[fieldKey])
        for fieldKey in step.fieldKeys
    )


def EditDateField(editContext: AccountEditContext, step: EditStep) -> DateEditResult:
    """Port of PERFORM EDIT-DATE-CCYYMMDD THRU EDIT-DATE-CCYYMMDD-EXIT for one date.

    The date's CSUTLDPY suffix follows its label; the parts whose flag is not valid are the
    failing fields (MOVE WS-EDIT-DATE-FLGS TO the date's flags).
    """
    result = EditDateCcyymmdd(DateTextOf(editContext, step))
    if result.inputError:
        partStatuses = (result.yearStatus, result.monthStatus, result.dayStatus)
        failedKeys = tuple(
            fieldKey
            for fieldKey, status in zip(step.fieldKeys, partStatuses, strict=True)
            if status is not DatePartStatus.VALID
        )
        editContext.collector.Fail(failedKeys, EditMessage(step.label, result.messageSuffix))
    return result


def EditBirthDate(editContext: AccountEditContext, step: EditStep) -> None:
    """Port of the 1200 Date of Birth edit (lines 1533-1543).

    EDIT-DATE-CCYYMMDD first; only when its flags are valid, EDIT-DATE-OF-BIRTH (cpy/CSUTLDPY.cpy
    lines 341-370), whose failure (a date not before today) flags all three parts.
    """
    if not EditDateField(editContext, step).isValid:
        return
    result = EditDateOfBirth(DateTextOf(editContext, step), editContext.today)
    if result.inputError:
        FailStep(editContext, step, result.messageSuffix)


def EditUsSsn(editContext: AccountEditContext, step: EditStep) -> None:
    """Port of 1265-EDIT-US-SSN: each part numeric, non-zero, and part 1 not 666 or 900-999.

    Each part is edited under its own label. A part 1 of 000 fails as ``must not be zero.``
    before the INVALID-SSN-PART1 test is reached. Parts 2 and 3 are edited only when part 1
    passes 1245-EDIT-NUM-REQD: ``IF FLG-EDIT-US-SSN-PART1-ISVALID`` stays open until the period
    after the part 3 edit (lines 2448-2488), so a blank, non-numeric or zero part 1 is the only
    failing SSN field, while a part 1 of 666 or 900-999 still has parts 2 and 3 edited.
    """
    parts = tuple(zip(step.fieldKeys, SSN_PART_LABELS, strict=True))
    firstKey, firstLabel = parts[0]
    firstLength = SCREEN_FIELD_LENGTHS[firstKey]
    if not EditNumericRequired(editContext, firstKey, firstLabel, firstLength):
        return
    if int(FieldText(editContext, firstKey, firstLength)) in INVALID_SSN_PART1_VALUES:
        invalidMessage = EditMessage(firstLabel, SSN_PART1_INVALID_SUFFIX)
        editContext.collector.Fail((firstKey,), invalidMessage)
    for fieldKey, label in parts[1:]:
        EditNumericRequired(editContext, fieldKey, label, SCREEN_FIELD_LENGTHS[fieldKey])


def EditFicoScore(editContext: AccountEditContext, step: EditStep) -> None:
    """Port of 1245-EDIT-NUM-REQD, then 1275-EDIT-FICO-SCORE (300 to 850) when it passes."""
    if not EditNumericStep(editContext, step):
        return
    score = int(FieldText(editContext, step.fieldKeys[0], step.checkLength))
    if score not in VALID_FICO_RANGE:
        FailStep(editContext, step, FICO_RANGE_SUFFIX)


def EditStateCode(editContext: AccountEditContext, step: EditStep) -> None:
    """Port of 1225-EDIT-ALPHA-REQD, then 1270-EDIT-US-STATE-CD when it passes.

    The state code must match a VALID-US-STATE-CODE value exactly, so ``ny`` fails.
    """
    if not EditAlphaRequired(editContext, step):
        return
    if not IsValidUsStateCode(FieldText(editContext, step.fieldKeys[0], step.checkLength)):
        FailStep(editContext, step, STATE_CODE_INVALID_SUFFIX)
        return
    editContext.stateValid = True


def EditZipCode(editContext: AccountEditContext, step: EditStep) -> None:
    """Port of 1245-EDIT-NUM-REQD on the 5-character ZIP; its outcome is FLG-ZIPCODE-ISVALID."""
    editContext.zipValid = EditNumericStep(editContext, step)


def PhonePartFailure(text: str, rule: PhonePartRule) -> str:
    """Return the failing suffix of one phone part, or "" when it passes."""
    if IsBlankText(text):
        return rule.requiredSuffix
    if not IsAsciiDigits(text):
        return rule.notNumericSuffix
    if int(text) == 0:
        return rule.zeroSuffix
    if rule.checkGeneralPurposeCode and not IsValidNanpAreaCode(text):
        return AREA_CODE_NOT_NANP_SUFFIX
    return ""


def EditUsPhone(editContext: AccountEditContext, step: EditStep) -> None:
    """Port of 1260-EDIT-US-PHONE-NUM (lines 2225-2427).

    A phone whose parts are all blank is optional and passes. Otherwise the area code,
    prefix and line number are each edited and each failure flags only its own part.
    """
    # The source's optional test is (A blank) AND (B blank) AND (A = SPACES OR C = LOW-VALUES).
    # 1100-RECEIVE-MAP turns every blank part into LOW-VALUES, so A = SPACES never holds and the
    # phone is optional only when the line number is blank too; this is ported as it runs.
    if all(IsBlankText(editContext.values[fieldKey]) for fieldKey in step.fieldKeys):
        return
    for fieldKey, rule in zip(step.fieldKeys, PHONE_PART_RULES, strict=True):
        suffix = PhonePartFailure(FieldText(editContext, fieldKey, rule.length), rule)
        if suffix:
            editContext.collector.Fail((fieldKey,), EditMessage(step.label, suffix))


def EditStateZip(editContext: AccountEditContext, step: EditStep) -> None:
    """Port of 1280-EDIT-US-STATE-ZIP-CD, run only when the state and the ZIP each passed.

    The state code and the first two ZIP characters must form a VALID-US-STATE-ZIP-CD2-COMBO;
    a failure flags both fields with the fixed literal ``Invalid zip code for state``.
    """
    if not (editContext.stateValid and editContext.zipValid):
        return
    stateCode, zipCode = (editContext.values[fieldKey] for fieldKey in step.fieldKeys)
    if not IsValidUsStateZipPrefix(stateCode, zipCode):
        editContext.collector.Fail(step.fieldKeys, MSG_INVALID_ZIP_FOR_STATE)


# 1200-EDIT-MAP-INPUTS (lines 1470-1669): every edit runs, in this order (all-field mode).
ACCOUNT_EDIT_TABLE = (
    EditStep("Account Status", (STATUS_FIELD,), 1, EditYesNo),
    EditStep("Open Date", OPEN_DATE_FIELDS, 8, EditDateField),
    EditStep("Credit Limit", ("credit_limit",), 15, EditSignedAmount),
    EditStep("Expiry Date", EXPIRY_DATE_FIELDS, 8, EditDateField),
    EditStep("Cash Credit Limit", ("cash_credit_limit",), 15, EditSignedAmount),
    EditStep("Reissue Date", REISSUE_DATE_FIELDS, 8, EditDateField),
    EditStep("Current Balance", ("curr_balance",), 15, EditSignedAmount),
    EditStep("Current Cycle Credit Limit", ("curr_cycle_credit",), 15, EditSignedAmount),
    EditStep("Current Cycle Debit Limit", ("curr_cycle_debit",), 15, EditSignedAmount),
    EditStep("SSN", SSN_FIELDS, 9, EditUsSsn),
    EditStep("Date of Birth", BIRTH_DATE_FIELDS, 8, EditBirthDate),
    EditStep("FICO Score", (FICO_FIELD,), 3, EditFicoScore),
    EditStep("First Name", ("first_name",), 25, EditAlphaRequired),
    EditStep("Middle Name", ("middle_name",), 25, EditAlphaOptional),
    EditStep("Last Name", ("last_name",), 25, EditAlphaRequired),
    EditStep("Address Line 1", ("addr_line_1",), 50, EditMandatory),
    EditStep("State", (STATE_FIELD,), 2, EditStateCode),
    EditStep("Zip", (ZIP_FIELD,), 5, EditZipCode),
    EditStep("City", ("addr_line_3",), 50, EditAlphaRequired),
    EditStep("Country", (COUNTRY_FIELD,), 3, EditAlphaRequired),
    EditStep("Phone Number 1", PHONE_1_FIELDS, 15, EditUsPhone),
    EditStep("Phone Number 2", PHONE_2_FIELDS, 15, EditUsPhone),
    EditStep("EFT Account Id", ("eft_account_id",), 10, EditNumericStep),
    EditStep("Primary Card Holder", ("primary_holder_ind",), 1, EditYesNo),
    EditStep("", (STATE_FIELD, ZIP_FIELD), 2, EditStateZip),
)


def RunEditTable(newValues: NewAccountData, today: date) -> EditCollector:
    """Walk ACCOUNT_EDIT_TABLE over the new values and return what the edits left set."""
    editContext = AccountEditContext(values=newValues.texts, collector=EditCollector(), today=today)
    for step in ACCOUNT_EDIT_TABLE:
        step.editFunction(editContext, step)
    return editContext.collector


def BuildScreen(outcome: ScreenOutcome, context: OnlineContext) -> AccountScreen:
    """Port of 3000-SEND-MAP: the 3200 values, the 3250 information line and the 3300 cursor.

    The response carries the new screen token. No ``*`` marker is put in a blank failing
    field: ``field_errors`` carries what CSSETATY's red attribute and ``*`` showed.
    """
    screenState = outcome.updateState.screenState
    return AccountScreen(
        fields=AccountScreenFields(**outcome.fields),
        state=screenState,
        screen_token=EncodeUpdateState(outcome.updateState, context),
        message=outcome.message,
        info_message=INFO_BY_STATE[screenState],
        field_errors=list(outcome.fieldErrors),
        cursor_field=outcome.cursorField,
    )


def RaiseScreenError(outcome: ScreenOutcome, context: OnlineContext) -> NoReturn:
    """Raise the LegacyMessageError of a screen sent back with its ERRMSG line set.

    The screen ``fields``, ``state`` and ``screen_token`` ride beside the four standard keys,
    so the page keeps the state machine running on a 4xx or 5xx response. The error contract
    lists the cursor field in ``field_errors`` when no field failed.
    """
    screen = BuildScreen(outcome, context)
    screenBody = screen.model_dump(mode="json", include={"fields", "state", "screen_token"})
    raise LegacyMessageError(
        outcome.message,
        field=outcome.cursorField,
        httpStatus=outcome.httpStatus,
        details=LegacyErrorDetails(
            infoMessage=screen.info_message,
            fieldErrors=outcome.fieldErrors,
            extraBody=screenBody,
        ),
    )


def Respond(outcome: ScreenOutcome, context: OnlineContext) -> AccountScreen:
    """Return the screen of an outcome, or raise it as an error when its message is set."""
    if outcome.message:
        RaiseScreenError(outcome, context)
    return BuildScreen(outcome, context)


def KeyScreenFields(keyText: str) -> dict[str, str]:
    """3200 with no details shown: ACCTSID shows the key as received, every other field blank."""
    if IsBlankScreenInput(keyText):
        return {}
    return {ACCOUNT_ID_FIELD: keyText.rstrip(" ")[:ACCOUNT_KEY_LENGTH]}


def KeyErrorOutcome(keyText: str, message: str) -> ScreenOutcome:
    """The DETAILS_NOT_FETCHED screen of a failed 1210-EDIT-ACCOUNT, the key flagged."""
    return ScreenOutcome(
        NOT_FETCHED_STATE,
        KeyScreenFields(keyText),
        message,
        HTTP_EDIT_FAILURE,
        (ACCOUNT_ID_FIELD,),
    )


def ReadFailureOutcome(readResult: AccountReadResult) -> ScreenOutcome:
    """The screen of a 9000-READ-ACCT that left WS-RETURN-MSG set, before details are shown.

    The key is flagged when FLG-ACCTFILTER-NOT-OK is set. The source enters SHOW_DETAILS
    whenever the customer is found even after an account NOTFND; that cannot happen here,
    because ``xref.card_xref`` has foreign keys to both masters, so the state stays
    DETAILS_NOT_FETCHED.
    """
    fieldErrors = () if readResult.acctFilterOk else (ACCOUNT_ID_FIELD,)
    return ScreenOutcome(
        NOT_FETCHED_STATE,
        KeyScreenFields(readResult.acctId),
        readResult.message,
        readResult.httpStatus,
        fieldErrors,
    )


def ShowDetailsOutcome(readResult: AccountReadResult) -> ScreenOutcome:
    """SET ACUP-SHOW-DETAILS after a 9000-READ-ACCT that read the account and the customer."""
    beforeImage = BeforeImageOf(readResult.account, readResult.customer)
    shownState = AccountUpdateState(
        UpdateScreenState.SHOW_DETAILS,
        readResult.acctId,
        beforeImage[CUSTOMER_ID_FIELD],
        beforeImage,
    )
    return ScreenOutcome(shownState, DisplayOfImage(beforeImage))


async def FetchDetails(context: OnlineContext, keyText: str) -> AccountScreen:
    """Port of 1210-EDIT-ACCOUNT, then 2000 WHEN ACUP-DETAILS-NOT-FETCHED and 9000-READ-ACCT.

    A blank key sets ``Account number not provided``, but 1200 then sets
    NO-SEARCH-CRITERIA-RECEIVED, so the answer is ``No input received``; anything but 11 digits,
    or all zeros, is ``Account Number if supplied must be a 11 digit Non-Zero Number``. Both
    are 400. A read failure answers with the chain's message: 404 for a NOTFND builder, 503 for
    a file error, the batch-window CXACAIX literal included.
    """
    keyFlag = EditAccountField(keyText)[0]
    if keyFlag != FLAG_VALID:
        keyMessage = MSG_NO_INPUT_RECEIVED if keyFlag == FLAG_BLANK else MSG_ACCOUNT_KEY_INVALID
        RaiseScreenError(KeyErrorOutcome(keyText, keyMessage), context)
    readResult = await ReadAccountChain(context, keyText.ljust(ACCOUNT_KEY_LENGTH), TRANSACTION_ID)
    if readResult.message:
        RaiseScreenError(ReadFailureOutcome(readResult), context)
    return BuildScreen(ShowDetailsOutcome(readResult), context)


def NoChangeOutcome(updateState: AccountUpdateState, newValues: NewAccountData) -> ScreenOutcome:
    """The NO-CHANGES-DETECTED screen: the state is kept and the cursor is the account status.

    1466 clears every field flag. SHOW_DETAILS shows the fetched values (3202) and
    CHANGES_NOT_OK the received ones (3203).
    """
    if updateState.screenState is UpdateScreenState.SHOW_DETAILS:
        shownTexts = DisplayOfImage(updateState.beforeImage)
    else:
        shownTexts = DisplayOfNew(newValues, frozenset())
    return ScreenOutcome(
        updateState, shownTexts, MSG_NO_CHANGES, HTTP_EDIT_FAILURE, cursorField=STATUS_FIELD
    )


def EditFailureOutcome(
    updateState: AccountUpdateState, newValues: NewAccountData, collector: EditCollector
) -> ScreenOutcome:
    """The ACUP-CHANGES-NOT-OK screen: the first message and every failing field.

    The cursor is the first failing field in 3300 order; the token keeps the before-image.
    """
    fieldErrors = collector.SortedFieldErrors()
    failedState = replace(updateState, screenState=UpdateScreenState.CHANGES_NOT_OK, newValues=None)
    return ScreenOutcome(
        failedState,
        DisplayOfNew(newValues, frozenset(fieldErrors)),
        collector.message,
        HTTP_EDIT_FAILURE,
        fieldErrors,
        fieldErrors[0] if fieldErrors else ACCOUNT_ID_FIELD,
    )


def ConfirmationOutcome(
    updateState: AccountUpdateState, newValues: NewAccountData
) -> ScreenOutcome:
    """The ACUP-CHANGES-OK-NOT-CONFIRMED screen; the validated values are kept in the token."""
    confirmState = replace(
        updateState, screenState=UpdateScreenState.CHANGES_OK_NOT_CONFIRMED, newValues=newValues
    )
    return ScreenOutcome(confirmState, DisplayOfNew(newValues, frozenset()))


def KeepConfirmationOutcome(updateState: AccountUpdateState) -> ScreenOutcome:
    """Port of ENTER in ACUP-CHANGES-OK-NOT-CONFIRMED: no edit runs, the screen is kept.

    1200 exits as soon as the compare is done, and every field is protected, so the token's
    validated values are shown again with the confirmation prompt.
    """
    return ScreenOutcome(updateState, DisplayOfNew(updateState.newValues, frozenset()))


def EnterOutcome(
    updateState: AccountUpdateState, newValues: NewAccountData, today: date
) -> ScreenOutcome:
    """Port of 1200-EDIT-MAP-INPUTS and 2000-DECIDE-ACTION with details shown; never writes.

    The compare runs first and a no-change result short-circuits every field edit, invalid
    values included. Otherwise every edit runs (all-field mode): any failure gives
    CHANGES_NOT_OK, a clean pass CHANGES_OK_NOT_CONFIRMED.
    """
    if not CompareOldNew(newValues, updateState.beforeImage):
        return NoChangeOutcome(updateState, newValues)
    collector = RunEditTable(newValues, today)
    if collector.inputError:
        return EditFailureOutcome(updateState, newValues, collector)
    return ConfirmationOutcome(updateState, newValues)


def ProcessEnter(
    context: OnlineContext, updateState: AccountUpdateState, screenFields: AccountScreenFields
) -> AccountScreen:
    """Port of ENTER in SHOW_DETAILS or CHANGES_NOT_OK: receive, compare, edit, send.

    ``today`` for EDIT-DATE-OF-BIRTH is the server's local date, as FUNCTION CURRENT-DATE was.
    """
    newValues = ReceiveMap(screenFields, updateState)
    return Respond(EnterOutcome(updateState, newValues, date.today()), context)


def ProvisionalOutcome(
    updateState: AccountUpdateState, screenFields: AccountScreenFields
) -> ScreenOutcome:
    """The screen 1200 leaves before an F12 re-read: the ENTER outcome, or the kept one."""
    if updateState.screenState is UpdateScreenState.CHANGES_OK_NOT_CONFIRMED:
        return KeepConfirmationOutcome(updateState)
    newValues = ReceiveMap(screenFields, updateState)
    return EnterOutcome(updateState, newValues, date.today())


def CancelReadFailureOutcome(
    provisional: ScreenOutcome, readResult: AccountReadResult
) -> ScreenOutcome:
    """The screen of an F12 whose re-read failed: the provisional screen with the read message.

    When FLG-ACCTFILTER-NOT-OK is set, 3300 flags only the key. The source would leave
    FOUND-CUST-IN-MASTER set by 1200 and show SHOW_DETAILS over an INITIALIZEd before-image,
    whose S9(10)V99 amounts then hold spaces and have no defined display; the provisional
    state, its values and its token are kept instead, so the screen stays defined.
    """
    if readResult.acctFilterOk:
        fieldErrors = provisional.fieldErrors
    else:
        fieldErrors = (ACCOUNT_ID_FIELD,)
    return replace(
        provisional,
        message=readResult.message,
        httpStatus=readResult.httpStatus,
        fieldErrors=fieldErrors,
        cursorField=fieldErrors[0] if fieldErrors else ACCOUNT_ID_FIELD,
    )


async def ProcessCancel(
    context: OnlineContext, updateState: AccountUpdateState, screenFields: AccountScreenFields
) -> AccountScreen:
    """Port of 2000 WHEN CCARD-AID-PFK12 (lines 2572-2580): re-read, discarding the edits.

    1200 runs first, then the message is cleared and 9000-READ-ACCT re-reads the token's key.
    A successful read shows SHOW_DETAILS with a fresh before-image and no flagged field.
    """
    provisional = ProvisionalOutcome(updateState, screenFields)
    readResult = await ReadAccountChain(context, updateState.acctId, TRANSACTION_ID)
    if readResult.message:
        return Respond(CancelReadFailureOutcome(provisional, readResult), context)
    return Respond(ShowDetailsOutcome(readResult), context)


class WriteAbandoned(Exception):
    """GO TO 9600-WRITE-PROCESSING-EXIT before a rewrite, raised inside the write units.

    Raising it rolls back every unit still open, so nothing more is written; the
    WriteAttempt says why the write stopped.
    """


@dataclass
class WriteAttempt:
    """The progress of one 9600-WRITE-PROCESSING, read once both write units have ended.

    ``outcome`` is what ending now with a file condition would mean: a customer lock failure
    while the CUSTDAT unit is being opened, an account lock failure until the account is
    locked, a customer lock failure until the customer is locked, then an update failure. It
    becomes OUTCOME_DONE only after the customer unit commits.
    ``freshImage`` is the before-image of the records 9700 found changed.
    """

    outcome: str = OUTCOME_ACCOUNT_LOCK_FAILED
    freshImage: dict[str, str] | None = None

    def StepInProgress(self) -> str:
        """Return the CICS command a file condition ending the units now would have failed."""
        if self.outcome == OUTCOME_UPDATE_FAILED:
            return REWRITE_OPERATION
        return READ_UPDATE_OPERATION


@dataclass(frozen=True)
class WriteUnits:
    """The ACCTDAT and CUSTDAT write units of one 9600-WRITE-PROCESSING."""

    accountUnit: AsyncConnection
    customerUnit: AsyncConnection


@dataclass(frozen=True)
class WriteOutcomeScreen:
    """How 2000-DECIDE-ACTION and 3000-SEND-MAP present one 9600 outcome."""

    screenState: UpdateScreenState
    httpStatus: int
    message: str


# 2000-DECIDE-ACTION (lines 2603-2615). A customer lock failure is CHANGES_FAILED, as the CAUP
# save contract specifies for every lock failure, although COULD-NOT-LOCK-CUST-FOR-UPDATE has
# no WHEN of its own there and WHEN OTHER would set ACUP-CHANGES-OKAYED-AND-DONE.
WRITE_OUTCOMES: Mapping[str, WriteOutcomeScreen] = MappingProxyType(
    {
        OUTCOME_ACCOUNT_LOCK_FAILED: WriteOutcomeScreen(
            UpdateScreenState.CHANGES_FAILED, HTTP_RECORD_LOCKED, MSG_LOCK_ACCOUNT
        ),
        OUTCOME_CUSTOMER_LOCK_FAILED: WriteOutcomeScreen(
            UpdateScreenState.CHANGES_FAILED, HTTP_RECORD_LOCKED, MSG_LOCK_CUSTOMER
        ),
        OUTCOME_CHANGED: WriteOutcomeScreen(
            UpdateScreenState.SHOW_DETAILS, HTTP_RECORD_CHANGED, MSG_RECORD_CHANGED
        ),
        OUTCOME_UPDATE_FAILED: WriteOutcomeScreen(
            UpdateScreenState.CHANGES_FAILED, HTTP_FILE_ERROR, MSG_UPDATE_FAILED
        ),
        OUTCOME_DONE: WriteOutcomeScreen(UpdateScreenState.CHANGES_DONE, HTTP_OK, ""),
    }
)


def RecordsChangedSinceFetch(
    currentImage: Mapping[str, str], beforeImage: Mapping[str, str]
) -> bool:
    """Port of 9700-CHECK-CHANGE-IN-REC: True when the locked records differ from the fetch."""
    return any(
        normaliser(currentImage[column]) != normaliser(beforeImage[column])
        for columns, normaliser in RECORD_CHANGE_RULES
        for column in columns
    )


def DateOfParts(texts: Mapping[str, str], dateFields: tuple[str, str, str]) -> date | None:
    """Return the date ``STRING year '-' month '-' day`` writes, or None when there is none.

    The date column cannot hold the raw text, so the day is the value EDIT-DAY validated:
    NUMVAL of the part stored into unsigned 9(2), which reads `` 5`` and ``05`` alike. Parts no
    edit passed, such as a blank year, give None.
    """
    yearText, monthText, dayText = (texts[fieldKey] for fieldKey in dateFields)
    dayValue = int(StoreInto(Numval(dayText), DAY_DIGITS, 0, signed=False))
    try:
        return date(int(yearText), int(monthText), dayValue)
    except ValueError:
        return None


def AccountRewriteValues(updateState: AccountUpdateState) -> dict[str, object] | None:
    """Return ACCT-UPDATE-RECORD (lines 3956-4002) by column, or None when a date has no value.

    ACCT-UPDATE-RECORD has no ACCT-ADDR-ZIP field, so ACCT-UPDATE-GROUP-ID lies at offset 102,
    where CVACT01Y keeps ACCT-ADDR-ZIP, and the never-initialised FILLER covers ACCT-GROUP-ID.
    The REWRITE therefore stores the entered group in ``addr_zip`` and blanks ``group_id``
    (LOW-VALUES, stored as ""); this is ported as the source writes it.
    """
    newValues = updateState.newValues
    dates = {
        column: DateOfParts(newValues.texts, parts) for column, parts in ACCOUNT_DATE_FIELDS.items()
    }
    if None in dates.values():
        return None
    return {
        ACCOUNT_ID_FIELD: updateState.acctId,
        STATUS_FIELD: newValues.texts[STATUS_FIELD],
        **{column: newValues.amounts[column] for column in AMOUNT_FIELDS},
        **dates,
        ZIP_FIELD: newValues.texts[GROUP_FIELD].rstrip(" "),
        GROUP_FIELD: "",
    }


def PhoneText(texts: Mapping[str, str], phoneFields: tuple[str, str, str]) -> str:
    """Return ``STRING '(' A ')' B '-' C`` with blank parts as spaces, right-trimmed."""
    area, prefix, line = (texts[name].ljust(SCREEN_FIELD_LENGTHS[name]) for name in phoneFields)
    opening, closing, separator = PHONE_TEXT_PUNCTUATION
    return (opening + area + closing + prefix + separator + line).rstrip(" ")


def CustomerRewriteValues(updateState: AccountUpdateState) -> dict[str, object] | None:
    """Return CUST-UPDATE-RECORD (lines 4007-4059) by column, or None when the birth date has none.

    Texts are as entered, right-trimmed and with their case kept; the protected country is the
    fetched one. The ZIP is the 5 characters entered and the SSN the three parts joined.
    """
    texts = updateState.newValues.texts
    birthDate = DateOfParts(texts, BIRTH_DATE_FIELDS)
    if birthDate is None:
        return None
    customerValues: dict[str, object] = {
        name: texts[name].rstrip(" ") for name in CUSTOMER_REWRITE_TEXT_FIELDS
    }
    customerValues.update(
        {column: PhoneText(texts, parts) for column, parts in PHONE_COLUMN_FIELDS.items()}
    )
    customerValues[CUSTOMER_ID_FIELD] = updateState.custId
    customerValues["ssn"] = "".join(texts[name] for name in SSN_FIELDS)
    customerValues[BIRTH_DATE_COLUMN] = birthDate
    customerValues[FICO_FIELD] = int(texts[FICO_FIELD])
    return customerValues


async def LockCompareAndRewriteAccount(
    context: OnlineContext,
    updateState: AccountUpdateState,
    units: WriteUnits,
    attempt: WriteAttempt,
) -> None:
    """The READ UPDATEs, 9700-CHECK-CHANGE-IN-REC and the ACCTDAT REWRITE of 9600.

    Every non-NORMAL READ UPDATE response, NOTFND included, is a lock failure. Records that
    differ from the before-image are not rewritten.
    """
    accountRepository = AccountRepository(context)
    account = await accountRepository.GetAccountForUpdate(units.accountUnit, updateState.acctId)
    if account is None:
        raise WriteAbandoned
    attempt.outcome = OUTCOME_CUSTOMER_LOCK_FAILED
    customerRepository = CustomerRepository(context)
    customer = await customerRepository.GetCustomerForUpdate(units.customerUnit, updateState.custId)
    if customer is None:
        raise WriteAbandoned
    currentImage = BeforeImageOf(account, customer)
    if RecordsChangedSinceFetch(currentImage, updateState.beforeImage):
        attempt.outcome, attempt.freshImage = OUTCOME_CHANGED, currentImage
        raise WriteAbandoned
    attempt.outcome = OUTCOME_UPDATE_FAILED
    accountValues = AccountRewriteValues(updateState)
    if accountValues is None:
        raise WriteAbandoned
    await accountRepository.UpdateAccount(units.accountUnit, accountValues)


async def RewriteCustomer(
    context: OnlineContext, updateState: AccountUpdateState, customerUnit: AsyncConnection
) -> None:
    """The CUSTDAT REWRITE of 9600, run after the ACCTDAT unit has committed."""
    customerValues = CustomerRewriteValues(updateState)
    if customerValues is None:
        raise WriteAbandoned
    await CustomerRepository(context).UpdateCustomer(customerUnit, customerValues)


async def WriteProcessing(
    context: OnlineContext, updateState: AccountUpdateState, attempt: WriteAttempt
) -> None:
    """Port of 9600-WRITE-PROCESSING (lines 3888-4104) in two write units.

    The CUSTDAT unit is opened first and holds no lock until the account is locked in the
    ACCTDAT unit, so the account record is always locked first. A failure opening the CUSTDAT
    unit is a customer lock failure. Inside it, a failure is an account lock failure until the
    account is locked, the ACCTDAT unit's opening included. Leaving the ACCTDAT unit commits
    the account rewrite before the customer rewrite starts: a failed, cancelled or interrupted
    customer rewrite rolls back only the CUSTDAT unit and the account change persists.
    """
    attempt.outcome = OUTCOME_CUSTOMER_LOCK_FAILED
    async with WriteUnit(context.engine, CUSTOMER_FILE_NAME) as customerUnit:
        attempt.outcome = OUTCOME_ACCOUNT_LOCK_FAILED
        async with WriteUnit(context.engine, ACCOUNT_FILE_NAME) as accountUnit:
            units = WriteUnits(accountUnit, customerUnit)
            await LockCompareAndRewriteAccount(context, updateState, units, attempt)
        await RewriteCustomer(context, updateState, customerUnit)


async def RunWriteProcessing(
    context: OnlineContext, updateState: AccountUpdateState
) -> WriteAttempt:
    """Run 9600-WRITE-PROCESSING and return how it ended.

    A file condition at any step, the units' connect and commit included, ends the attempt with
    the outcome of the step in progress: a customer lock failure while the CUSTDAT unit is
    being opened, an account lock failure until the account is locked, a customer lock failure
    until the customer is locked, then an update failure. The batch window's FileNotOpenError
    on the account READ UPDATE is one, so a save during the window answers ``Could not lock
    account record for update``. Task cancellation is not caught.
    """
    attempt = WriteAttempt()
    try:
        await WriteProcessing(context, updateState, attempt)
    except WriteAbandoned:
        return attempt
    except RepositoryError as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, attempt.StepInProgress(), error)
        return attempt
    attempt.outcome = OUTCOME_DONE
    return attempt


def SaveOutcome(updateState: AccountUpdateState, attempt: WriteAttempt) -> ScreenOutcome:
    """The screen after 9600: fresh records for a change, otherwise the new values (3203).

    A record changed since the fetch shows SHOW_DETAILS with the records just read for update,
    so the next save compares with what is stored now; the source re-shows the stale
    ACUP-OLD-* values instead, and the fresh data is the specified outcome here. Every other
    outcome shows the values the user confirmed, all protected, with the cursor on the account.
    """
    outcomeScreen = WRITE_OUTCOMES[attempt.outcome]
    freshImage = attempt.freshImage
    if freshImage is not None:
        resultState = AccountUpdateState(
            outcomeScreen.screenState, updateState.acctId, freshImage[CUSTOMER_ID_FIELD], freshImage
        )
        shownTexts = DisplayOfImage(freshImage)
    else:
        resultState = AccountUpdateState(
            outcomeScreen.screenState, updateState.acctId, updateState.custId
        )
        shownTexts = DisplayOfNew(updateState.newValues, frozenset())
    return ScreenOutcome(resultState, shownTexts, outcomeScreen.message, outcomeScreen.httpStatus)


async def ProcessSave(context: OnlineContext, updateState: AccountUpdateState) -> AccountScreen:
    """Port of 2000 WHEN ACUP-CHANGES-OK-NOT-CONFIRMED AND CCARD-AID-PFK05 (lines 2601-2615).

    The token's validated values are written, whatever the request's fields hold.

    Raises:
        LegacyMessageError: 423 ``Could not lock account record for update`` (CHANGES_FAILED)
            or ``Could not lock customer record for update`` (CHANGES_FAILED, nothing written);
            409 ``Record changed by some one else. Please review`` with the fresh records
            shown; 503 ``Update of record failed`` (CHANGES_FAILED).
    """
    attempt = await RunWriteProcessing(context, updateState)
    return Respond(SaveOutcome(updateState, attempt), context)


def NormaliseAction(action: UpdateAction, screenState: UpdateScreenState) -> UpdateAction:
    """Port of the AID check (lines 905-916): a key the state does not allow becomes ENTER.

    F5 is valid only in ACUP-CHANGES-OK-NOT-CONFIRMED, F12 only once details are fetched.
    """
    if action == UpdateAction.SAVE and screenState is UpdateScreenState.CHANGES_OK_NOT_CONFIRMED:
        return action
    if action == UpdateAction.CANCEL and screenState is not UpdateScreenState.DETAILS_NOT_FETCHED:
        return action
    return UpdateAction.ENTER


def BindStateToPath(updateState: AccountUpdateState, acctId: str) -> AccountUpdateState:
    """Return the decoded state when its token was issued for the path account.

    A FETCHED_STATES token acts on its own account key, so it is kept only when the path
    ``acctId``, padded to 11 characters, is that key; a token issued for another account is
    invalid and restarts the flow as DETAILS_NOT_FETCHED, reading and writing nothing of it.
    DETAILS_NOT_FETCHED, CHANGES_DONE and CHANGES_FAILED act on no token key and are kept.
    """
    if updateState.screenState not in FETCHED_STATES:
        return updateState
    if acctId.ljust(ACCOUNT_KEY_LENGTH) == updateState.acctId:
        return updateState
    return NOT_FETCHED_STATE


async def FetchAccountForEdit(context: OnlineContext, acctId: str) -> AccountScreen:
    """Serve ``GET /accounts/{acctId}/edit``: a fresh CAUP entry, ENTER with the path key.

    The key runs 1210-EDIT-ACCOUNT and the account, cross-reference and customer are read as
    the DETAILS_NOT_FETCHED ENTER reads them, giving SHOW_DETAILS with the SSN unmasked.

    Raises:
        LegacyMessageError: 400 with the key edit literal, 404 with a not-found builder, or
            503 with the READ file-error text (the CXACAIX literal during the batch window).
    """
    return await FetchDetails(context, acctId)


async def ProcessAccountUpdate(
    context: OnlineContext, acctId: str, request: AccountUpdateRequest
) -> AccountScreen:
    """Port of 0000-MAIN (lines 859-1004): one ENTER, F5 or F12 on the COACTUP screen.

    The state comes from ``request.screen_token``, bound to the path by BindStateToPath: a
    fetched-state token issued for another account than the path ``acctId`` is
    DETAILS_NOT_FETCHED. In DETAILS_NOT_FETCHED the key is ``request.fields.acct_id`` when it
    is not empty, otherwise the path ``acctId``, so a bare ``PUT /accounts/{id}`` with
    ``action: "enter"`` fetches ``{id}``. In the fetched states the key is the token's, which
    the path names. After CHANGES_DONE or CHANGES_FAILED any key returns the empty key screen
    without reading.

    Raises:
        LegacyMessageError: 400 for a key or field edit failure or no change, 404 for a
            not-found message, 409 for a record changed since the fetch, 423 for a record lock,
            503 for a file error or a failed rewrite. Each body carries the screen ``fields``,
            ``state`` and ``screen_token``.
    """
    updateState = BindStateToPath(DecodeUpdateState(request.screen_token, context), acctId)
    screenState = updateState.screenState
    action = NormaliseAction(request.action, screenState)
    if screenState in FINISHED_STATES:
        return BuildScreen(ScreenOutcome(NOT_FETCHED_STATE), context)
    if screenState is UpdateScreenState.DETAILS_NOT_FETCHED:
        return await FetchDetails(context, request.fields.acct_id or acctId)
    if action == UpdateAction.CANCEL:
        return await ProcessCancel(context, updateState, request.fields)
    if screenState is UpdateScreenState.CHANGES_OK_NOT_CONFIRMED:
        if action == UpdateAction.SAVE:
            return await ProcessSave(context, updateState)
        return Respond(KeepConfirmationOutcome(updateState), context)
    return ProcessEnter(context, updateState, request.fields)
