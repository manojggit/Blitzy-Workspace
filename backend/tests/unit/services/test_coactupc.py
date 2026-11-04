"""Unit tests for COACTUPC (transaction CAUP), the account update of BMS map COACTUP.

Replaces: COACTUPC (CAUP), BMS COACTUP, copybooks CVACT01Y, CVCUS01Y, CSLKPCDY, CSUTLDPY,
CSUTLDWY and CSMSG02Y. The service app.services.accounts.coactupc is called directly:
FetchAccountForEdit for the edit fetch and ProcessAccountUpdate for ENTER, F5 and F12. Every
expected value is a constant typed from the cited lines of cbl/COACTUPC.cbl, cpy/CSUTLDPY.cpy
and cpy/CSLKPCDY.cpy, or from the seeded EBCDIC images; no expectation is produced by the
service. Writes are read back with plain SQL on the psycopg2 engine.

Test accounts. No seed account passes every 1200-EDIT-MAP-INPUTS edit as loaded, so the edit
cases run on account 00000000016 (customer 000000016), whose only defect is the VT state with a
84xxx ZIP: VALID_BASELINE sets the state to UT, a VALID-US-STATE-ZIP-CD2-COMBO, so a case's own
change is the only failing edit. Account 00000000004 cannot serve: its customer's Phone Number 2
area code 156 is no VALID-GENERAL-PURP-CODE, MI with ZIP 39 is no combo, and its 10-character
ZIP "39035-0455" never equals the 5 characters ACSZIPC returns, so 1205-COMPARE-OLD-NEW finds a
change in every resubmitted screen. Account 00000000001 (FICO 274) pins the low-FICO outcome.

Unreachable texts are not tested as outcomes: the CSUTLDPY EDIT-DATE-LE builder
`` validation error Sev code: ...`` (every date CEEDAYS would reject fails an earlier year,
month, day or combination edit of the CCYY 19/20 range), the Country edits (ACSCTRY is protected,
so its value comes from the token's before-image), and the " can have numbers or alphabets
only." suffix of 1230/1240, which no PERFORM reaches.
"""

import json
import secrets
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from types import MappingProxyType, SimpleNamespace
from typing import Any, NamedTuple

import pytest
import pytest_asyncio
from sqlalchemy import Engine, text

from app.core.errors import FileIoError, LegacyMessageError, RecordLockError
from app.core.locks import FileNotOpenError
from app.core.security import (
    SCREEN_STATE_HKDF_INFO,
    DecodeScreenToken,
    DeriveFernet,
    EncodeScreenToken,
)
from app.repositories.account_repository import AccountRepository
from app.repositories.card_xref_repository import CardXrefRepository
from app.repositories.customer_repository import CustomerRepository
from app.schemas.accounts import AccountScreen, AccountScreenFields, AccountUpdateRequest
from app.schemas.common import UpdateAction, UpdateScreenState
from app.services.accounts import coactupc
from app.services.accounts.coactupc import FetchAccountForEdit, ProcessAccountUpdate
from app.utils.lookup_codes import VALID_NANP_AREA_CODES

# --- Accounts, users and files -------------------------------------------------------------------
EDIT_ACCOUNT_ID = "00000000016"
EDIT_CUSTOMER_ID = "000000016"
LOW_FICO_ACCOUNT_ID = "00000000001"
UNKNOWN_ACCOUNT_ID = "00000000099"
REGULAR_USER_TYPE = "U"
# LIT-ACCTFILENAME and LIT-CUSTFILENAME.
ACCOUNT_FILE = "ACCTDAT"
CUSTOMER_FILE = "CUSTDAT"
# LIT-CARDXREFNAME-ACCT-PATH, the cross-reference by account that 9200-GETCARDXREF-BYACCT reads.
XREF_ACCOUNT_PATH = "CXACAIX"
# The AccountScreenFields attributes the cursor and field_errors name most often.
ACCOUNT_ID_FIELD = "acct_id"
STATUS_FIELD = "active_status"
BIRTH_DATE_FIELDS = ("dob_year", "dob_month", "dob_day")
DATE_PARTS = ("year", "month", "day")
PHONE_PARTS = ("area", "prefix", "line")

# --- HTTP statuses of the legacy error contract (AAP 0.6.5) --------------------------------------
HTTP_EDIT_FAILURE = 400
HTTP_NOT_FOUND = 404
HTTP_RECORD_CHANGED = 409
HTTP_RECORD_LOCKED = 423
HTTP_FILE_ERROR = 503

# --- Screen states (ACUP-CHANGE-ACTION) ----------------------------------------------------------
DETAILS_NOT_FETCHED = UpdateScreenState.DETAILS_NOT_FETCHED
SHOW_DETAILS = UpdateScreenState.SHOW_DETAILS
CHANGES_NOT_OK = UpdateScreenState.CHANGES_NOT_OK
CHANGES_OK_NOT_CONFIRMED = UpdateScreenState.CHANGES_OK_NOT_CONFIRMED
CHANGES_DONE = UpdateScreenState.CHANGES_DONE
CHANGES_FAILED = UpdateScreenState.CHANGES_FAILED

# --- WS-INFO-MSG 88 levels (lines 463-477), as 3250-SETUP-INFOMSG sets them per state -----------
INFO_FOUND_ACCOUNT_DATA = "Details of selected account shown above"
INFO_PROMPT_FOR_SEARCH_KEYS = "Enter or update id of account to update"
INFO_PROMPT_FOR_CHANGES = "Update account details presented above."
INFO_PROMPT_FOR_CONFIRMATION = "Changes validated.Press F5 to save"
INFO_CONFIRM_UPDATE_SUCCESS = "Changes committed to database"
INFO_INFORM_FAILURE = "Changes unsuccessful. Please try again"

# --- WS-RETURN-MSG texts set on a reachable path -------------------------------------------------
# 1210-EDIT-ACCOUNT sets WS-PROMPT-FOR-ACCT for a blank key, then 1200 line 1442 overwrites it
# with NO-SEARCH-CRITERIA-RECEIVED; lines 1806-1810 STRING the key literal.
MSG_ACCOUNT_NOT_PROVIDED = "Account number not provided"
MSG_NO_INPUT_RECEIVED = "No input received"
MSG_ACCOUNT_KEY_INVALID = "Account Number if supplied must be a 11 digit Non-Zero Number"
# The 9200-GETCARDXREF-BYACCT NOTFND builder (RESP 13, RESP2 0), cut to WS-RETURN-MSG X(75).
MSG_XREF_NOT_FOUND = "Account:00000000099 not found in Cross ref file.  Resp:000000013  Reas:0000"
# The 9300-GETACCTDATA-BYACCT and 9400-GETCUSTDATA-BYCUST NOTFND builders (lines 3723-3733,
# 3773-3783) for account 00000000016 and its customer 000000016, cut to X(75) the same way.
MSG_ACCOUNT_NOT_FOUND = (
    "Account:00000000016 not found in Acct Master file.Resp:000000013  Reas:0000"
)
MSG_CUSTOMER_NOT_FOUND = (
    "CustId:000000016 not found in customer master.Resp: 000000013  REAS:0000000"
)
# The 9200, 9300 and 9400 WHEN OTHER texts (lines 3686, 3736, 3785): WS-FILE-ERROR-MESSAGE
# (lines 389-408), 'READ' in X(8) and the file in X(9), cut to X(75) and right-trimmed. The
# batch window's CXACAIX answers NOTOPEN (RESP 19, RESP2 60); IOERR is RESP 17, RESP2 0.
MSG_XREF_WINDOW_CLOSED = (
    "File Error: READ     on CXACAIX   returned RESP 000000019 ,RESP2 000000060"
)
MSG_ACCOUNT_READ_ERROR = (
    "File Error: READ     on ACCTDAT   returned RESP 000000017 ,RESP2 000000000"
)
MSG_CUSTOMER_READ_ERROR = (
    "File Error: READ     on CUSTDAT   returned RESP 000000017 ,RESP2 000000000"
)
# The NO-CHANGES-DETECTED, lock, changed and failed 88 texts that 1205 and 9600 set.
MSG_NO_CHANGES = "No change detected with respect to values fetched."
MSG_LOCK_ACCOUNT = "Could not lock account record for update"
MSG_LOCK_CUSTOMER = "Could not lock customer record for update"
MSG_RECORD_CHANGED = "Record changed by some one else. Please review"
MSG_UPDATE_FAILED = "Update of record failed"

# --- The eight field literals pinned verbatim (AAP 0.6.9) ----------------------------------------
MSG_ACCOUNT_STATUS_YES_NO = "Account Status must be Y or N."
MSG_CREDIT_LIMIT_REQUIRED = "Credit Limit must be supplied."
MSG_CREDIT_LIMIT_NOT_VALID = "Credit Limit is not valid"
MSG_FICO_RANGE = "FICO Score: should be between 300 and 850"
MSG_SSN_PART_1_INVALID = "SSN: First 3 chars: should not be 000, 666, or between 900 and 999"
MSG_PHONE_1_AREA_ZERO = "Phone Number 1: Area code cannot be zero"
MSG_OPEN_DATE_CENTURY = "Open Date : Century is not valid."
MSG_INVALID_ZIP_FOR_STATE = "Invalid zip code for state"
PINNED_FIELD_LITERALS = frozenset(
    {
        MSG_ACCOUNT_STATUS_YES_NO,
        MSG_CREDIT_LIMIT_REQUIRED,
        MSG_CREDIT_LIMIT_NOT_VALID,
        MSG_FICO_RANGE,
        MSG_SSN_PART_1_INVALID,
        MSG_PHONE_1_AREA_ZERO,
        MSG_OPEN_DATE_CENTURY,
        MSG_INVALID_ZIP_FOR_STATE,
    }
)

# --- 88-level texts no reachable statement leaves on the screen (lines 466-526) ------------------
NEVER_SHOWN_TEXTS = frozenset(
    {
        INFO_FOUND_ACCOUNT_DATA,
        MSG_ACCOUNT_NOT_PROVIDED,
        "PF03 pressed.Exiting",
        "Last name not provided",
        "Name can only contain alphabets and spaces",
        "Account number must be a non zero 11 digit number",
        "Did not find this account in account card xref file",
        "Did not find this account in account master file",
        "Did not find associated customer in master file",
        "Account Active Status must be Y or N",
        "Credit Limit must be supplied",
        "Card expiry month must be between 1 and 12",
        "Invalid card expiry year",
        "Did not find this account in cards database",
        "Did not find cards for this search condition",
        "Error reading Card Data File",
        "Looks Good.... so far",
    }
)
# The 1230-EDIT-ALPHANUM-REQD and 1240-EDIT-ALPHANUM-OPT suffix (lines 1999, 2095).
UNREACHABLE_ALPHANUMERIC_SUFFIX = " can have numbers or alphabets only."

# --- WS-EDIT-VARIABLE-NAME labels (lines 1472-1657, 2439-2481), cut to PIC X(25) (line 53) -------
ACCOUNT_STATUS_LABEL = "Account Status"
CREDIT_LIMIT_LABEL = "Credit Limit"
CASH_CREDIT_LIMIT_LABEL = "Cash Credit Limit"
CURRENT_BALANCE_LABEL = "Current Balance"
CYCLE_CREDIT_LABEL = "Current Cycle Credit Limi"
CYCLE_DEBIT_LABEL = "Current Cycle Debit Limit"
SSN_PART_1_LABEL = "SSN: First 3 chars"
SSN_PART_2_LABEL = "SSN 4th & 5th chars"
SSN_PART_3_LABEL = "SSN Last 4 chars"
FICO_LABEL = "FICO Score"
FIRST_NAME_LABEL = "First Name"
MIDDLE_NAME_LABEL = "Middle Name"
LAST_NAME_LABEL = "Last Name"
ADDRESS_LINE_1_LABEL = "Address Line 1"
STATE_LABEL = "State"
ZIP_LABEL = "Zip"
CITY_LABEL = "City"
PHONE_1_LABEL = "Phone Number 1"
PHONE_2_LABEL = "Phone Number 2"
EFT_ACCOUNT_LABEL = "EFT Account Id"
PRIMARY_HOLDER_LABEL = "Primary Card Holder"
OPEN_DATE_LABEL = "Open Date"
EXPIRY_DATE_LABEL = "Expiry Date"
REISSUE_DATE_LABEL = "Reissue Date"
BIRTH_DATE_LABEL = "Date of Birth"

# --- Suffixes the 1215-1280 edits STRING after TRIM(WS-EDIT-VARIABLE-NAME) -----------------------
MUST_BE_SUPPLIED = " must be supplied."  # lines 1841, 1869, 1915, 2126, 2191
MUST_BE_YES_NO = " must be Y or N."  # line 1886
ALPHABETS_ONLY = " can have alphabets only."  # lines 1941, 2047
ALL_NUMERIC = " must be all numeric."  # line 2146
NOT_ZERO = " must not be zero."  # line 2163
NOT_VALID = " is not valid"  # line 2209
AREA_CODE_REQUIRED = ": Area code must be supplied."  # line 2254
AREA_CODE_NOT_NUMERIC = ": Area code must be A 3 digit number."  # line 2272
AREA_CODE_ZERO = ": Area code cannot be zero"  # line 2286
AREA_CODE_NOT_NANP = ": Not valid North America general purpose area code"  # line 2306
PREFIX_CODE_REQUIRED = ": Prefix code must be supplied."  # line 2325
PREFIX_CODE_NOT_NUMERIC = ": Prefix code must be A 3 digit number."  # line 2343
PREFIX_CODE_ZERO = ": Prefix code cannot be zero"  # line 2357
LINE_NUMBER_REQUIRED = ": Line number code must be supplied."  # line 2378
LINE_NUMBER_NOT_NUMERIC = ": Line number code must be A 4 digit number."  # line 2396
LINE_NUMBER_ZERO = ": Line number code cannot be zero"  # line 2410
STATE_CODE_INVALID = ": is not a valid state code"  # line 2503

# --- CSUTLDPY date suffixes (cpy/CSUTLDPY.cpy lines 37-363) --------------------------------------
YEAR_REQUIRED = " : Year must be supplied."
YEAR_NOT_NUMERIC = " must be 4 digit number."
CENTURY_INVALID = " : Century is not valid."
MONTH_REQUIRED = " : Month must be supplied."
MONTH_INVALID = ": Month must be a number between 1 and 12."
DAY_REQUIRED = " : Day must be supplied."
DAY_INVALID = ":day must be a number between 1 and 31."
NO_31_DAYS = ":Cannot have 31 days in this month."
NO_30_DAYS = ":Cannot have 30 days in this month."
NOT_LEAP_YEAR = ":Not a leap year.Cannot have 29 days in this month."
# EDIT-DATE-OF-BIRTH (line 363) keeps the trailing space of its literal.
FUTURE_DATE = ":cannot be in the future "

# --- Seed values of account 00000000016 and customer 000000016 (data/EBCDIC images) --------------
# The ACCTDAT, CXACAIX and CUSTDAT values 3202-SHOW-ORIGINAL-VALUES shows; amounts are edited by
# WS-EDIT-CURRENCY-9-2-F, PIC +ZZZ,ZZZ,ZZZ.99 (line 371), and the SSN is shown in full.
EXPECTED_FETCHED_FIELDS = MappingProxyType(
    {
        "acct_id": EDIT_ACCOUNT_ID,
        "active_status": "Y",
        "open_year": "2014",
        "open_month": "09",
        "open_day": "11",
        "credit_limit": "+      8,922.00",
        "curr_balance": "+        733.00",
        "curr_cycle_credit": "+           .00",
        "cust_id": EDIT_CUSTOMER_ID,
        "ssn_part_1": "649",
        "ssn_part_2": "82",
        "ssn_part_3": "7971",
        "dob_year": "1983",
        "dob_month": "04",
        "dob_day": "27",
        "fico_credit_score": "326",
        "addr_state_cd": "VT",
        "addr_zip": "84390",
        "addr_country_cd": "USA",
        "phone_num_1_area": "631",
    }
)
# The account fields 3202 shows after an ACCTDAT READ that failed (lines 2793-2825): the
# ACCOUNT-RECORD no READ filled, blank, each amount zero edited by +ZZZ,ZZZ,ZZZ.99. ACCTSIDO shows
# the key, CC-ACCT-ID (line 2708).
UNREAD_ACCOUNT_AMOUNT_SHOWN = "+           .00"
UNREAD_ACCOUNT_FIELDS = MappingProxyType(
    {
        "acct_id": EDIT_ACCOUNT_ID,
        "active_status": "",
        "open_year": "",
        "open_month": "",
        "open_day": "",
        "credit_limit": UNREAD_ACCOUNT_AMOUNT_SHOWN,
        "expiration_year": "",
        "expiration_month": "",
        "expiration_day": "",
        "cash_credit_limit": UNREAD_ACCOUNT_AMOUNT_SHOWN,
        "reissue_year": "",
        "reissue_month": "",
        "reissue_day": "",
        "curr_balance": UNREAD_ACCOUNT_AMOUNT_SHOWN,
        "curr_cycle_credit": UNREAD_ACCOUNT_AMOUNT_SHOWN,
        "group_id": "",
        "curr_cycle_debit": UNREAD_ACCOUNT_AMOUNT_SHOWN,
    }
)
# UT with ZIP 84390 is a VALID-US-STATE-ZIP-CD2-COMBO (UT84), so every edit of the fetched screen
# passes; VT84 is not one.
VALID_BASELINE = MappingProxyType({"addr_state_cd": "UT"})
SAVED_STATE = "UT"
SAVE_CHANGES = MappingProxyType({**VALID_BASELINE, "credit_limit": "5000.00"})
SAVED_CREDIT_LIMIT = Decimal("5000.00")
SAVED_CREDIT_LIMIT_SHOWN = "+      5,000.00"
# Keyed into protected fields of a confirmation screen; F5 must ignore them.
OTHER_FIELD_VALUES = MappingProxyType(
    {"credit_limit": "7777.00", "first_name": "Other", "active_status": "N"}
)
# Values every detail edit rejects; sent where no detail edit may run.
GARBAGE_DETAILS = MappingProxyType(
    {"active_status": "X", "credit_limit": "12a", "fico_credit_score": "abc", "dob_month": "13"}
)
INVALID_KEY_TEXT = "abc"
# Keyed into the fields 3320-UNPROTECT-FEW-ATTRS leaves protected once details are shown.
PROTECTED_FIELD_CHANGES = MappingProxyType(
    {"acct_id": INVALID_KEY_TEXT, "cust_id": "999999999", "addr_country_cd": "XYZ"}
)
CHANGED_BALANCE = Decimal("999.00")
CHANGED_BALANCE_SHOWN = "+        999.00"
# Customer 000000001 is seeded with FICO 274, NC with ZIP 12546 (NC12 is no combo) and Phone
# Number 2 area code 373 (no VALID-GENERAL-PURP-CODE); NY12 is a combo and 201 a valid code.
LOW_FICO_CREDIT_CHANGE = MappingProxyType({"credit_limit": "3000.00"})
LOW_FICO_CORRECTIONS = MappingProxyType(
    {
        **LOW_FICO_CREDIT_CHANGE,
        "fico_credit_score": "700",
        "addr_state_cd": "NY",
        "phone_num_2_area": "201",
    }
)
# FUNCTION CURRENT-DATE for EDIT-DATE-OF-BIRTH, patched into the service.
FIXED_TODAY = date(2024, 2, 29)

# --- Plain SQL on the psycopg2 engine; values are always bound -----------------------------------
SELECT_ACCOUNT_SQL = text("SELECT * FROM accounts.accounts WHERE acct_id = :acctId")
SELECT_CUSTOMER_SQL = text("SELECT * FROM customers.customers WHERE cust_id = :custId")
UPDATE_BALANCE_SQL = text(
    "UPDATE accounts.accounts SET curr_balance = :balance WHERE acct_id = :acctId"
)
# Another connection's row lock on the customer, refused with SQLSTATE 55P03 instead of waiting.
LOCK_CUSTOMER_NOWAIT_SQL = text(
    "SELECT 1 FROM customers.customers WHERE cust_id = :custId FOR UPDATE NOWAIT"
)
# The constraint refuses only customer 000000016 with the state UT that SAVE_CHANGES submits; no
# seeded customer is in UT, and NOT VALID skips checking the stored rows. A lock still held when
# it is added or dropped fails the statement after lock_timeout instead of hanging the run.
DDL_LOCK_TIMEOUT_SQL = text("SET LOCAL lock_timeout = '5s'")
ADD_REWRITE_CHECK_SQL = text(
    "ALTER TABLE customers.customers ADD CONSTRAINT ck_test_customer_rewrite_rejected"
    " CHECK (cust_id <> '000000016' OR addr_state_cd <> 'UT') NOT VALID"
)
DROP_REWRITE_CHECK_SQL = text(
    "ALTER TABLE customers.customers DROP CONSTRAINT IF EXISTS ck_test_customer_rewrite_rejected"
)
# LogFileCondition's record when the constraint refuses the CUSTDAT UPDATE (SQLSTATE 23514).
CUSTOMER_REWRITE_REFUSED_LOG = (
    "Transaction CAUP REWRITE on CUSTDAT returned RESP:17 REAS:0"
    " (cause CheckViolationError, SQLSTATE 23514)"
)

# --- Screen token forgery ------------------------------------------------------------------------
RANDOM_TOKEN_BYTES = 64
FOREIGN_SECRET_BYTES = 48
SECONDS_PER_MINUTE = 60
EXPIRY_MARGIN_SECONDS = 60


# --- Value objects -------------------------------------------------------------------------------
class FieldCase(NamedTuple):
    """Fields keyed over the fetched screen, the first message and the failing fields."""

    changes: Mapping[str, str]
    expectedMessage: str
    expectedFieldErrors: tuple[str, ...]


class ExpectedScreen(NamedTuple):
    """The status, message lines, failing fields, cursor and state of an error screen."""

    httpStatus: int
    message: str
    infoMessage: str
    fieldErrors: tuple[str, ...]
    cursorField: str
    state: UpdateScreenState


class KeyCase(NamedTuple):
    """An account key as keyed and the screen 1210-EDIT-ACCOUNT and 9000-READ-ACCT answer."""

    keyText: str
    expected: ExpectedScreen


class NoChangeCase(NamedTuple):
    """An account and fields that 1205-COMPARE-OLD-NEW finds equal to the before-image."""

    acctId: str
    changes: Mapping[str, str]


class AmountCase(NamedTuple):
    """Credit Limit text that passes TEST-NUMVAL-C and the S9(10)V99 value it is stored as."""

    rawText: str
    storedAmount: Decimal


@dataclass(frozen=True)
class UpdateScenario:
    """A regular user's online context, the test engine, account 16 fetched, and its rows."""

    context: Any
    syncEngine: Engine
    screen: AccountScreen
    accountRow: Mapping[str, Any]
    customerRow: Mapping[str, Any]


# --- Case builders -------------------------------------------------------------------------------
def Case(changes: Mapping[str, str], expectedMessage: str, *expectedFieldErrors: str) -> FieldCase:
    """Return a FieldCase whose failing fields are listed in 3300 cursor order."""
    return FieldCase(MappingProxyType(dict(changes)), expectedMessage, expectedFieldErrors)


def ChangesId(changes: Mapping[str, str]) -> str:
    """Return a readable pytest id for keyed fields, such as ``open_day='31'``."""
    return ",".join(f"{fieldName}={fieldText!r}" for fieldName, fieldText in changes.items())


def CaseId(fieldCase: FieldCase) -> str:
    """Return the pytest id of a FieldCase: its keyed fields."""
    return ChangesId(fieldCase.changes)


def AmountCases(fieldName: str, label: str) -> list[FieldCase]:
    """1250-EDIT-SIGNED-9V2 on one amount: blank, ``*`` (blank to 1100) and a TEST-NUMVAL-C miss."""
    return [
        Case({fieldName: ""}, label + MUST_BE_SUPPLIED, fieldName),
        Case({fieldName: "*"}, label + MUST_BE_SUPPLIED, fieldName),
        Case({fieldName: "12a"}, label + NOT_VALID, fieldName),
    ]


def PhoneCases(phonePrefix: str, label: str) -> list[FieldCase]:
    """1260-EDIT-US-PHONE-NUM on one phone: each part's required, numeric and zero tests."""
    area, prefix, line = (f"{phonePrefix}_{phonePart}" for phonePart in PHONE_PARTS)
    return [
        Case({area: ""}, label + AREA_CODE_REQUIRED, area),
        Case({area: "2a1"}, label + AREA_CODE_NOT_NUMERIC, area),
        Case({area: "000"}, label + AREA_CODE_ZERO, area),
        Case({area: "800"}, label + AREA_CODE_NOT_NANP, area),
        Case({prefix: ""}, label + PREFIX_CODE_REQUIRED, prefix),
        Case({prefix: "3a3"}, label + PREFIX_CODE_NOT_NUMERIC, prefix),
        Case({prefix: "000"}, label + PREFIX_CODE_ZERO, prefix),
        Case({line: ""}, label + LINE_NUMBER_REQUIRED, line),
        Case({line: "86a7"}, label + LINE_NUMBER_NOT_NUMERIC, line),
        Case({line: "0000"}, label + LINE_NUMBER_ZERO, line),
    ]


def DateCases(datePrefix: str, label: str) -> list[FieldCase]:
    """EDIT-DATE-CCYYMMDD on one date: every part edit and EDIT-DAY-MONTH-YEAR failure.

    The 31-day and 30-day failures flag the month and the day; the leap-year failure flags all
    three parts (CSUTLDPY lines 209-282). 1900 is a century year, so CCYY is divided by 400.
    """
    year, month, day = (f"{datePrefix}_{datePart}" for datePart in DATE_PARTS)
    return [
        Case({year: ""}, label + YEAR_REQUIRED, year),
        Case({year: "20a4"}, label + YEAR_NOT_NUMERIC, year),
        Case({year: "1850"}, label + CENTURY_INVALID, year),
        Case({month: ""}, label + MONTH_REQUIRED, month),
        Case({month: "13"}, label + MONTH_INVALID, month),
        Case({day: ""}, label + DAY_REQUIRED, day),
        Case({day: "32"}, label + DAY_INVALID, day),
        Case({month: "04", day: "31"}, label + NO_31_DAYS, month, day),
        Case({month: "02", day: "30"}, label + NO_30_DAYS, month, day),
        Case({year: "2023", month: "02", day: "29"}, label + NOT_LEAP_YEAR, year, month, day),
        Case({year: "1900", month: "02", day: "29"}, label + NOT_LEAP_YEAR, year, month, day),
    ]


def EditFailure(message: str, fieldErrors: tuple[str, ...]) -> ExpectedScreen:
    """The ACUP-CHANGES-NOT-OK screen: 400, the first message, the cursor on the first field."""
    return ExpectedScreen(
        HTTP_EDIT_FAILURE,
        message,
        INFO_PROMPT_FOR_CHANGES,
        fieldErrors,
        fieldErrors[0],
        CHANGES_NOT_OK,
    )


def KeyFailure(httpStatus: int, message: str) -> ExpectedScreen:
    """The DETAILS_NOT_FETCHED screen of a failed key edit or read, the key flagged."""
    return ExpectedScreen(
        httpStatus,
        message,
        INFO_PROMPT_FOR_SEARCH_KEYS,
        (ACCOUNT_ID_FIELD,),
        ACCOUNT_ID_FIELD,
        DETAILS_NOT_FETCHED,
    )


def UnreadAccountFailure(httpStatus: int, message: str) -> ExpectedScreen:
    """SHOW_DETAILS after an ACCTDAT read failure with the customer found, the key flagged."""
    return ExpectedScreen(
        httpStatus,
        message,
        INFO_PROMPT_FOR_CHANGES,
        (ACCOUNT_ID_FIELD,),
        ACCOUNT_ID_FIELD,
        SHOW_DETAILS,
    )


def SaveFailure(httpStatus: int, message: str) -> ExpectedScreen:
    """A failed F5: CHANGES_FAILED and INFORM-FAILURE; no field failed, so the key is the cursor."""
    return ExpectedScreen(
        httpStatus,
        message,
        INFO_INFORM_FAILURE,
        (ACCOUNT_ID_FIELD,),
        ACCOUNT_ID_FIELD,
        CHANGES_FAILED,
    )


# --- Case tables ---------------------------------------------------------------------------------
# 1200-EDIT-MAP-INPUTS over every editable COACTUP field, in edit order (lines 1470-1669). Each
# row is keyed over VALID_BASELINE, so its own field is the only one failing.
FIELD_LITERAL_CASES = [
    Case({STATUS_FIELD: ""}, ACCOUNT_STATUS_LABEL + MUST_BE_SUPPLIED, STATUS_FIELD),
    Case({STATUS_FIELD: "0"}, ACCOUNT_STATUS_LABEL + MUST_BE_SUPPLIED, STATUS_FIELD),
    Case({STATUS_FIELD: "X"}, MSG_ACCOUNT_STATUS_YES_NO, STATUS_FIELD),
    Case({STATUS_FIELD: "y"}, ACCOUNT_STATUS_LABEL + MUST_BE_YES_NO, STATUS_FIELD),
    *AmountCases("credit_limit", CREDIT_LIMIT_LABEL),
    *AmountCases("cash_credit_limit", CASH_CREDIT_LIMIT_LABEL),
    *AmountCases("curr_balance", CURRENT_BALANCE_LABEL),
    *AmountCases("curr_cycle_credit", CYCLE_CREDIT_LABEL),
    *AmountCases("curr_cycle_debit", CYCLE_DEBIT_LABEL),
    Case({"ssn_part_1": ""}, SSN_PART_1_LABEL + MUST_BE_SUPPLIED, "ssn_part_1"),
    Case({"ssn_part_1": "abc"}, SSN_PART_1_LABEL + ALL_NUMERIC, "ssn_part_1"),
    Case({"ssn_part_2": ""}, SSN_PART_2_LABEL + MUST_BE_SUPPLIED, "ssn_part_2"),
    Case({"ssn_part_2": "ab"}, SSN_PART_2_LABEL + ALL_NUMERIC, "ssn_part_2"),
    Case({"ssn_part_2": "00"}, SSN_PART_2_LABEL + NOT_ZERO, "ssn_part_2"),
    Case({"ssn_part_3": ""}, SSN_PART_3_LABEL + MUST_BE_SUPPLIED, "ssn_part_3"),
    Case({"ssn_part_3": "abcd"}, SSN_PART_3_LABEL + ALL_NUMERIC, "ssn_part_3"),
    Case({"ssn_part_3": "0000"}, SSN_PART_3_LABEL + NOT_ZERO, "ssn_part_3"),
    Case({"fico_credit_score": ""}, FICO_LABEL + MUST_BE_SUPPLIED, "fico_credit_score"),
    Case({"fico_credit_score": "abc"}, FICO_LABEL + ALL_NUMERIC, "fico_credit_score"),
    Case({"fico_credit_score": "000"}, FICO_LABEL + NOT_ZERO, "fico_credit_score"),
    Case({"first_name": ""}, FIRST_NAME_LABEL + MUST_BE_SUPPLIED, "first_name"),
    Case({"first_name": "J0hn"}, FIRST_NAME_LABEL + ALPHABETS_ONLY, "first_name"),
    Case({"middle_name": "M1"}, MIDDLE_NAME_LABEL + ALPHABETS_ONLY, "middle_name"),
    Case({"last_name": ""}, LAST_NAME_LABEL + MUST_BE_SUPPLIED, "last_name"),
    Case({"last_name": "D0e"}, LAST_NAME_LABEL + ALPHABETS_ONLY, "last_name"),
    Case({"addr_line_1": ""}, ADDRESS_LINE_1_LABEL + MUST_BE_SUPPLIED, "addr_line_1"),
    Case({"addr_state_cd": ""}, STATE_LABEL + MUST_BE_SUPPLIED, "addr_state_cd"),
    Case({"addr_state_cd": "U1"}, STATE_LABEL + ALPHABETS_ONLY, "addr_state_cd"),
    Case({"addr_state_cd": "XX"}, STATE_LABEL + STATE_CODE_INVALID, "addr_state_cd"),
    # 88 VALID-US-STATE-CODE compares the field exactly, so a lower-case code fails.
    Case({"addr_state_cd": "ut"}, STATE_LABEL + STATE_CODE_INVALID, "addr_state_cd"),
    Case({"addr_zip": ""}, ZIP_LABEL + MUST_BE_SUPPLIED, "addr_zip"),
    Case({"addr_zip": "8439a"}, ZIP_LABEL + ALL_NUMERIC, "addr_zip"),
    Case({"addr_zip": "00000"}, ZIP_LABEL + NOT_ZERO, "addr_zip"),
    Case({"addr_line_3": ""}, CITY_LABEL + MUST_BE_SUPPLIED, "addr_line_3"),
    Case({"addr_line_3": "C1ty"}, CITY_LABEL + ALPHABETS_ONLY, "addr_line_3"),
    *PhoneCases("phone_num_1", PHONE_1_LABEL),
    *PhoneCases("phone_num_2", PHONE_2_LABEL),
    Case({"eft_account_id": ""}, EFT_ACCOUNT_LABEL + MUST_BE_SUPPLIED, "eft_account_id"),
    Case({"eft_account_id": "12345abcde"}, EFT_ACCOUNT_LABEL + ALL_NUMERIC, "eft_account_id"),
    Case({"eft_account_id": "0000000000"}, EFT_ACCOUNT_LABEL + NOT_ZERO, "eft_account_id"),
    Case({"primary_holder_ind": ""}, PRIMARY_HOLDER_LABEL + MUST_BE_SUPPLIED, "primary_holder_ind"),
    Case({"primary_holder_ind": "X"}, PRIMARY_HOLDER_LABEL + MUST_BE_YES_NO, "primary_holder_ind"),
]
# The CSUTLDPY builders reachable from the four COACTUP dates (lines 1478, 1490, 1503, 1533).
DATE_LITERAL_CASES = [
    *DateCases("open", OPEN_DATE_LABEL),
    *DateCases("expiration", EXPIRY_DATE_LABEL),
    *DateCases("reissue", REISSUE_DATE_LABEL),
    *DateCases("dob", BIRTH_DATE_LABEL),
]
# 88 INVALID-SSN-PART1 VALUES 0, 666, 900 THRU 999 (lines 121-123); 1245 rejects 000 as zero
# before that test is reached. 88 FICO-RANGE-IS-VALID VALUES 300 THROUGH 850. UT with ZIP 10
# is no VALID-US-STATE-ZIP-CD2-COMBO, which flags both fields (lines 2539-2555).
BOUNDARY_REJECTED_CASES = [
    Case({"ssn_part_1": "000"}, SSN_PART_1_LABEL + NOT_ZERO, "ssn_part_1"),
    Case({"ssn_part_1": "666"}, MSG_SSN_PART_1_INVALID, "ssn_part_1"),
    Case({"ssn_part_1": "900"}, MSG_SSN_PART_1_INVALID, "ssn_part_1"),
    Case({"ssn_part_1": "999"}, MSG_SSN_PART_1_INVALID, "ssn_part_1"),
    Case({"fico_credit_score": "299"}, MSG_FICO_RANGE, "fico_credit_score"),
    Case({"fico_credit_score": "851"}, MSG_FICO_RANGE, "fico_credit_score"),
    Case({"addr_zip": "10001"}, MSG_INVALID_ZIP_FOR_STATE, "addr_state_cd", "addr_zip"),
]
# Values on the passing side of the same edits; 2000 is a leap year (divided by 400) and an
# all-blank phone is optional (lines 2225-2240).
BOUNDARY_ACCEPTED_CHANGES = [
    MappingProxyType({"ssn_part_1": "899"}),
    MappingProxyType({"fico_credit_score": "300"}),
    MappingProxyType({"fico_credit_score": "850"}),
    MappingProxyType({"phone_num_1_area": min(VALID_NANP_AREA_CODES)}),
    MappingProxyType({"open_year": "2000", "open_month": "02", "open_day": "29"}),
    MappingProxyType({"reissue_month": "04", "reissue_day": "30"}),
    MappingProxyType({"middle_name": ""}),
    MappingProxyType({"phone_num_2_area": "", "phone_num_2_prefix": "", "phone_num_2_line": ""}),
    MappingProxyType({STATUS_FIELD: "N"}),
]
# All-field mode: every edit runs, the first message set wins (edit order) and the failing
# fields are listed and the cursor placed in 3300-SETUP-SCREEN-ATTRS order. In the last row
# Phone Number 2 fails before the cross-field state/ZIP edit, yet the state is the cursor.
MULTI_FIELD_CASES = [
    Case(
        {STATUS_FIELD: "X", "credit_limit": ""},
        MSG_ACCOUNT_STATUS_YES_NO,
        STATUS_FIELD,
        "credit_limit",
    ),
    Case(
        {"primary_holder_ind": "Q", "fico_credit_score": "299", "open_month": "13"},
        OPEN_DATE_LABEL + MONTH_INVALID,
        "open_month",
        "fico_credit_score",
        "primary_holder_ind",
    ),
    Case(
        {"phone_num_2_area": "800", "addr_zip": "10001"},
        PHONE_2_LABEL + AREA_CODE_NOT_NANP,
        "addr_state_cd",
        "addr_zip",
        "phone_num_2_area",
    ),
]
KEY_CASES = [
    KeyCase("", KeyFailure(HTTP_EDIT_FAILURE, MSG_NO_INPUT_RECEIVED)),
    KeyCase("*", KeyFailure(HTTP_EDIT_FAILURE, MSG_NO_INPUT_RECEIVED)),
    KeyCase(INVALID_KEY_TEXT, KeyFailure(HTTP_EDIT_FAILURE, MSG_ACCOUNT_KEY_INVALID)),
    KeyCase("00000000000", KeyFailure(HTTP_EDIT_FAILURE, MSG_ACCOUNT_KEY_INVALID)),
    KeyCase("1234567890", KeyFailure(HTTP_EDIT_FAILURE, MSG_ACCOUNT_KEY_INVALID)),
    KeyCase(UNKNOWN_ACCOUNT_ID, KeyFailure(HTTP_NOT_FOUND, MSG_XREF_NOT_FOUND)),
]
KEY_CASE_IDS = ["blank", "asterisk", "non-numeric", "all-zeros", "ten-digits", "not-in-xref"]
NO_CHANGE_CASES = [
    NoChangeCase(EDIT_ACCOUNT_ID, MappingProxyType({})),
    # 1205-COMPARE-OLD-NEW compares the status, names and city upper-cased (lines 1685-1735).
    NoChangeCase(
        EDIT_ACCOUNT_ID,
        MappingProxyType(
            {STATUS_FIELD: "y", "first_name": "CARROLL", "addr_line_3": "concepcionland"}
        ),
    ),
    # Account 1 as fetched holds values every edit rejects; the compare runs before any edit.
    NoChangeCase(LOW_FICO_ACCOUNT_ID, MappingProxyType({})),
]
NO_CHANGE_IDS = ["as-fetched", "case-only", "invalid-values-as-fetched"]
# NO-CHANGES-DETECTED puts the cursor on ACSTTUS (lines 3010-3012); no field failed.
NO_CHANGE_SCREEN = ExpectedScreen(
    HTTP_EDIT_FAILURE,
    MSG_NO_CHANGES,
    INFO_PROMPT_FOR_CHANGES,
    (STATUS_FIELD,),
    STATUS_FIELD,
    SHOW_DETAILS,
)
LOW_FICO_CASES = [
    Case(
        LOW_FICO_CREDIT_CHANGE,
        MSG_FICO_RANGE,
        "fico_credit_score",
        "addr_state_cd",
        "addr_zip",
        "phone_num_2_area",
    ),
    Case(
        {**LOW_FICO_CREDIT_CHANGE, "fico_credit_score": "700"},
        PHONE_2_LABEL + AREA_CODE_NOT_NANP,
        "addr_state_cd",
        "addr_zip",
        "phone_num_2_area",
    ),
]
# COMPUTE ACUP-NEW-CREDIT-LIMIT-N = FUNCTION NUMVAL-C(...) (lines 1078-1080) stores into
# S9(10)V99 without ROUNDED or ON SIZE ERROR: surplus decimals and the 11th integer digit are lost.
RAW_AMOUNT_CASES = [
    AmountCase("$1,234.567", Decimal("1234.56")),
    AmountCase("1,000.00", Decimal("1000.00")),
    AmountCase("100.00CR", Decimal("-100.00")),
    AmountCase("100.00DB", Decimal("-100.00")),
    AmountCase("5.999", Decimal("5.99")),
    AmountCase("-5.999", Decimal("-5.99")),
    AmountCase("12345678901.00", Decimal("2345678901.00")),
]


# --- Database reads and writes -------------------------------------------------------------------
def ReadAccountRow(syncEngine: Engine, acctId: str) -> dict[str, Any]:
    """Return the accounts.accounts row of ``acctId`` as committed."""
    with syncEngine.connect() as connection:
        accountRow = connection.execute(SELECT_ACCOUNT_SQL, {"acctId": acctId}).mappings().one()
    return dict(accountRow)


def ReadCustomerRow(syncEngine: Engine, custId: str) -> dict[str, Any]:
    """Return the customers.customers row of ``custId`` as committed."""
    with syncEngine.connect() as connection:
        customerRow = connection.execute(SELECT_CUSTOMER_SQL, {"custId": custId}).mappings().one()
    return dict(customerRow)


def SetAccountBalance(syncEngine: Engine, acctId: str, balance: Decimal) -> None:
    """Commit a new ACCT-CURR-BAL, as another task's REWRITE between two COACTUP screens."""
    with syncEngine.begin() as connection:
        connection.execute(UPDATE_BALANCE_SQL, {"balance": balance, "acctId": acctId})


def LockCustomerRowWithoutWaiting(syncEngine: Engine, custId: str) -> int:
    """Lock the customers.customers row of ``custId`` on a new connection; return rows locked.

    FOR UPDATE NOWAIT raises SQLSTATE 55P03 at once while another transaction, such as a
    CUSTDAT write unit left open, still holds the row lock. The lock ends at this commit.
    """
    with syncEngine.begin() as connection:
        lockedRows = connection.execute(LOCK_CUSTOMER_NOWAIT_SQL, {"custId": custId}).all()
    return len(lockedRows)


def RewrittenAccount(accountRow: Mapping[str, Any], creditLimit: Decimal) -> dict[str, Any]:
    """Return the account after an F5 that changed only the credit limit.

    ACCT-UPDATE-RECORD has no ACCT-ADDR-ZIP (lines 418-432), so ACCT-UPDATE-GROUP-ID, moved from
    the blank ACUP-NEW-GROUP-ID (line 4002), lands where CVACT01Y keeps ACCT-ADDR-ZIP and the
    INITIALIZEd FILLER covers ACCT-GROUP-ID: the REWRITE blanks both columns.
    """
    return {**accountRow, "credit_limit": creditLimit, "addr_zip": "", "group_id": ""}


def AssertRowsUnchanged(scenario: UpdateScenario) -> None:
    """Assert that account 16 and customer 16 still hold the rows read before the test."""
    assert ReadAccountRow(scenario.syncEngine, EDIT_ACCOUNT_ID) == scenario.accountRow
    assert ReadCustomerRow(scenario.syncEngine, EDIT_CUSTOMER_ID) == scenario.customerRow


# --- Requests ------------------------------------------------------------------------------------
async def FetchScreen(context: Any, acctId: str) -> AccountScreen:
    """Return the SHOW_DETAILS screen of ``GET /accounts/{acctId}/edit``."""
    screen = await FetchAccountForEdit(context, acctId)
    assert screen.state == SHOW_DETAILS
    return screen


def WithChanges(
    screen: AccountScreen, changes: Mapping[str, str], action: UpdateAction = UpdateAction.ENTER
) -> AccountUpdateRequest:
    """Return a key press on ``screen`` with ``changes`` keyed over its fields."""
    return AccountUpdateRequest(
        action=action,
        screen_token=screen.screen_token,
        fields=screen.fields.model_copy(update=dict(changes)),
    )


def PressKey(screen: AccountScreen, action: UpdateAction) -> AccountUpdateRequest:
    """Return ``action`` pressed on ``screen`` with its fields sent back unchanged."""
    return WithChanges(screen, {}, action)


def ScreenOfError(error: LegacyMessageError) -> AccountScreen:
    """Return the COACTUP screen an error body carries, to press the next key on it."""
    return AccountScreen.model_validate(error.Body())


async def ExpectScreenError(
    context: Any, acctId: str, request: AccountUpdateRequest
) -> LegacyMessageError:
    """Send ``request`` and return the LegacyMessageError the screen is answered with."""
    with pytest.raises(LegacyMessageError) as excInfo:
        await ProcessAccountUpdate(context, acctId, request)
    return excInfo.value


async def ConfirmChanges(scenario: UpdateScenario, changes: Mapping[str, str]) -> AccountScreen:
    """Press ENTER with valid ``changes`` and return the CHANGES_OK_NOT_CONFIRMED screen."""
    request = WithChanges(scenario.screen, changes)
    confirmed = await ProcessAccountUpdate(scenario.context, EDIT_ACCOUNT_ID, request)
    AssertShownScreen(confirmed, CHANGES_OK_NOT_CONFIRMED, INFO_PROMPT_FOR_CONFIRMATION)
    return confirmed


def BirthDateChanges(birthDate: date) -> dict[str, str]:
    """Return the DOBYEAR, DOBMON and DOBDAY texts of ``birthDate``."""
    dateTexts = (f"{birthDate.year:04d}", f"{birthDate.month:02d}", f"{birthDate.day:02d}")
    return dict(zip(BIRTH_DATE_FIELDS, dateTexts, strict=True))


def FixedToday() -> date:
    """FUNCTION CURRENT-DATE as the patched service reads it."""
    return FIXED_TODAY


def PatchToday(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the service's ``date.today()`` return FIXED_TODAY.

    Only ``today`` is reached on an ENTER; the service builds ``date`` values only on F5.
    """
    monkeypatch.setattr(coactupc, "date", SimpleNamespace(today=FixedToday))


# --- Assertions ----------------------------------------------------------------------------------
def AssertNoNeverShownText(message: str, infoMessage: str) -> None:
    """Assert that neither message line holds a text no reachable statement leaves on screen."""
    assert message.rstrip(" ") not in NEVER_SHOWN_TEXTS
    assert infoMessage not in NEVER_SHOWN_TEXTS
    assert UNREACHABLE_ALPHANUMERIC_SUFFIX not in message


def AssertErrorScreen(error: LegacyMessageError, expected: ExpectedScreen) -> None:
    """Assert the status and every legacy key of an error screen's body."""
    errorBody = error.Body()
    assert error.httpStatus == expected.httpStatus
    assert errorBody["message"] == expected.message
    assert errorBody["info_message"] == expected.infoMessage
    assert errorBody["field_errors"] == list(expected.fieldErrors)
    assert errorBody["cursor_field"] == expected.cursorField
    assert errorBody["state"] == expected.state
    assert errorBody["screen_token"]
    AssertNoNeverShownText(errorBody["message"], errorBody["info_message"])


def AssertShownScreen(screen: AccountScreen, state: UpdateScreenState, infoMessage: str) -> None:
    """Assert a screen sent without an error: no message, no failing field, the cursor on ACCTSID.

    No flag is set, so 3300's WHEN OTHER places the cursor on the account id (line 3166).
    """
    assert (screen.message, screen.info_message) == ("", infoMessage)
    assert screen.field_errors == []
    assert screen.cursor_field == ACCOUNT_ID_FIELD
    assert screen.state == state
    AssertNoNeverShownText(screen.message, screen.info_message)


async def AssertEditFailureCase(scenario: UpdateScenario, fieldCase: FieldCase) -> None:
    """Key the case over VALID_BASELINE and assert its CHANGES_NOT_OK screen; nothing is written."""
    request = WithChanges(scenario.screen, {**VALID_BASELINE, **fieldCase.changes})
    error = await ExpectScreenError(scenario.context, EDIT_ACCOUNT_ID, request)
    AssertErrorScreen(error, EditFailure(fieldCase.expectedMessage, fieldCase.expectedFieldErrors))
    AssertRowsUnchanged(scenario)


# --- Read failure stubs: the CICS file conditions of 9000-READ-ACCT ------------------------------
async def CloseXrefPath(self: CardXrefRepository, acctId: str) -> dict[str, Any] | None:
    """CXACAIX READ answering NOTOPEN (RESP 19, RESP2 60): the batch window has closed it."""
    raise FileNotOpenError(XREF_ACCOUNT_PATH)


async def MissAccount(self: AccountRepository, acctId: str) -> dict[str, Any] | None:
    """ACCTDAT READ answering NOTFND (RESP 13), which xref.card_xref's foreign key rules out."""
    return None


async def MissCustomer(self: CustomerRepository, custId: str) -> dict[str, Any] | None:
    """CUSTDAT READ answering NOTFND (RESP 13), which xref.card_xref's foreign key rules out."""
    return None


async def FailAccountRead(self: AccountRepository, acctId: str) -> dict[str, Any] | None:
    """ACCTDAT READ answering IOERR (RESP 17)."""
    raise FileIoError(ACCOUNT_FILE)


async def FailCustomerRead(self: CustomerRepository, custId: str) -> dict[str, Any] | None:
    """CUSTDAT READ answering IOERR (RESP 17), after the cross-reference names the customer."""
    raise FileIoError(CUSTOMER_FILE)


class ReadStub(NamedTuple):
    """A 9000-READ-ACCT read replaced by a stub; every read not replaced runs on the seed."""

    repositoryClass: type
    methodName: str
    stub: Callable[..., Any]


class ReadFailureCase(NamedTuple):
    """The reads of account 16 replaced by stubs, and the screen its edit fetch is answered with.

    ``expectedFields`` builds the fields that screen shows from the screen fetched before the
    stubs were installed.
    """

    readStubs: tuple[ReadStub, ...]
    expected: ExpectedScreen
    expectedFields: Callable[[AccountScreen], dict[str, str]]


def KeyScreenFieldsOf(fetched: AccountScreen) -> dict[str, str]:
    """3201-SHOW-INITIAL-VALUES: ACCTSID shows the key, every detail field is blank."""
    return AccountScreenFields(acct_id=EDIT_ACCOUNT_ID).model_dump()


def UnreadAccountFieldsOf(fetched: AccountScreen) -> dict[str, str]:
    """3202 after an ACCTDAT failure: the customer as fetched, the unread account's fields blank."""
    return {**fetched.fields.model_dump(), **UNREAD_ACCOUNT_FIELDS}


def KeyScreenCase(
    readStubs: tuple[ReadStub, ...], httpStatus: int, message: str
) -> ReadFailureCase:
    """A read failure answered on the DETAILS_NOT_FETCHED key screen."""
    return ReadFailureCase(readStubs, KeyFailure(httpStatus, message), KeyScreenFieldsOf)


def UnreadAccountCase(
    readStubs: tuple[ReadStub, ...], httpStatus: int, message: str
) -> ReadFailureCase:
    """An ACCTDAT read failure with the customer found, answered in SHOW_DETAILS."""
    return ReadFailureCase(
        readStubs, UnreadAccountFailure(httpStatus, message), UnreadAccountFieldsOf
    )


XREF_PATH_CLOSED_STUB = ReadStub(CardXrefRepository, "GetXrefByAccount", CloseXrefPath)
ACCOUNT_MISSING_STUB = ReadStub(AccountRepository, "GetAccount", MissAccount)
CUSTOMER_MISSING_STUB = ReadStub(CustomerRepository, "GetCustomer", MissCustomer)
ACCOUNT_IOERR_STUB = ReadStub(AccountRepository, "GetAccount", FailAccountRead)
CUSTOMER_IOERR_STUB = ReadStub(CustomerRepository, "GetCustomer", FailCustomerRead)
# 9000-READ-ACCT (lines 3608-3649). A CXACAIX failure ends the chain; an ACCTDAT failure does not
# (line 3719 is commented out), so CUSTDAT is still read: its NOTFND keeps a message already set
# (IF WS-RETURN-MSG-OFF) and its WHEN OTHER replaces it. Only the CXACAIX and ACCTDAT failures
# set FLG-ACCTFILTER-NOT-OK; the key is the cursor and the one field listed either way.
# A NORMAL CUSTDAT read sets FOUND-CUST-IN-MASTER (line 3765), and 2000-DECIDE-ACTION then sets
# ACUP-SHOW-DETAILS (lines 2577-2578): after an ACCTDAT failure 9500 stores the customer and the
# ACCOUNT-RECORD the failed READ never filled, and 3202 shows them under PROMPT-FOR-CHANGES.
# Without a customer read the screen stays DETAILS_NOT_FETCHED and 3201 shows the key alone.
ACCOUNT_MISSING_CASE = UnreadAccountCase(
    (ACCOUNT_MISSING_STUB,), HTTP_NOT_FOUND, MSG_ACCOUNT_NOT_FOUND
)
ACCOUNT_IOERR_CASE = UnreadAccountCase(
    (ACCOUNT_IOERR_STUB,), HTTP_FILE_ERROR, MSG_ACCOUNT_READ_ERROR
)
READ_FAILURE_CASES = [
    KeyScreenCase((XREF_PATH_CLOSED_STUB,), HTTP_FILE_ERROR, MSG_XREF_WINDOW_CLOSED),
    ACCOUNT_MISSING_CASE,
    KeyScreenCase((CUSTOMER_MISSING_STUB,), HTTP_NOT_FOUND, MSG_CUSTOMER_NOT_FOUND),
    ACCOUNT_IOERR_CASE,
    KeyScreenCase((CUSTOMER_IOERR_STUB,), HTTP_FILE_ERROR, MSG_CUSTOMER_READ_ERROR),
    KeyScreenCase(
        (ACCOUNT_MISSING_STUB, CUSTOMER_IOERR_STUB), HTTP_FILE_ERROR, MSG_CUSTOMER_READ_ERROR
    ),
    KeyScreenCase(
        (ACCOUNT_MISSING_STUB, CUSTOMER_MISSING_STUB), HTTP_NOT_FOUND, MSG_ACCOUNT_NOT_FOUND
    ),
]
READ_FAILURE_CASE_IDS = [
    "cxacaix-notopen",
    "acctdat-notfnd",
    "custdat-notfnd",
    "acctdat-ioerr",
    "custdat-ioerr",
    "acctdat-notfnd-then-custdat-ioerr",
    "acctdat-notfnd-then-custdat-notfnd",
]
# The ACCTDAT failures whose customer is found: their SHOW_DETAILS token is pressed on.
UNREAD_ACCOUNT_CASES = [ACCOUNT_MISSING_CASE, ACCOUNT_IOERR_CASE]
UNREAD_ACCOUNT_CASE_IDS = ["acctdat-notfnd", "acctdat-ioerr"]


# --- Repository failure stubs: the CICS file conditions of 9600-WRITE-PROCESSING -----------------
async def RefuseAccountLock(self: AccountRepository, unit: object, acctId: str) -> None:
    """ACCTDAT READ UPDATE answering RECORDBUSY (RESP 101)."""
    raise RecordLockError(ACCOUNT_FILE)


async def RefuseCustomerLock(self: CustomerRepository, unit: object, custId: str) -> None:
    """CUSTDAT READ UPDATE answering RECORDBUSY (RESP 101), after the account is locked."""
    raise RecordLockError(CUSTOMER_FILE)


# --- Screen token forgers ------------------------------------------------------------------------
def RandomToken(screen: AccountScreen, settings: Any) -> str:
    """Return random text in place of the screen token."""
    return secrets.token_urlsafe(RANDOM_TOKEN_BYTES)


def ForeignSecretToken(screen: AccountScreen, settings: Any) -> str:
    """Return the screen's state re-encrypted under a SECRET_KEY the backend does not hold."""
    foreignSecret = secrets.token_urlsafe(FOREIGN_SECRET_BYTES)
    foreignSettings = settings.model_copy(update={"SECRET_KEY": foreignSecret})
    return EncodeScreenToken(DecodeScreenToken(screen.screen_token, settings), foreignSettings)


def TamperedToken(screen: AccountScreen, settings: Any) -> str:
    """Return the screen token with one character of its ciphertext changed."""
    screenToken = screen.screen_token
    middle = len(screenToken) // 2
    replacement = "B" if screenToken[middle] == "A" else "A"
    return screenToken[:middle] + replacement + screenToken[middle + 1 :]


def ExpiredToken(screen: AccountScreen, settings: Any) -> str:
    """Return the screen's state encrypted at a time older than ACCESS_TOKEN_EXPIRE_MINUTES."""
    statePayload = json.dumps(DecodeScreenToken(screen.screen_token, settings)).encode("utf-8")
    lifetimeSeconds = settings.ACCESS_TOKEN_EXPIRE_MINUTES * SECONDS_PER_MINUTE
    issuedAt = int(time.time()) - lifetimeSeconds - EXPIRY_MARGIN_SECONDS
    screenCipher = DeriveFernet(settings.SECRET_KEY, SCREEN_STATE_HKDF_INFO)
    return screenCipher.encrypt_at_time(statePayload, issuedAt).decode("ascii")


TOKEN_FORGERS: list[Callable[[AccountScreen, Any], str]] = [
    RandomToken,
    ForeignSecretToken,
    TamperedToken,
    ExpiredToken,
]
TOKEN_FORGER_IDS = ["random", "foreign-secret", "tampered", "expired"]


# --- Fixtures ------------------------------------------------------------------------------------
@pytest_asyncio.fixture(name="onlineContext")
async def OnlineContextFixture(makeOnlineContext: Callable[..., Any]) -> Any:
    """Return the OnlineContext of a regular (type U) user, who reaches CAUP from CM00."""
    return await makeOnlineContext(REGULAR_USER_TYPE)


@pytest_asyncio.fixture(name="scenario")
async def ScenarioFixture(onlineContext: Any, syncEngine: Engine) -> UpdateScenario:
    """Return account 16 fetched into SHOW_DETAILS, with its rows as committed before the test."""
    return UpdateScenario(
        context=onlineContext,
        syncEngine=syncEngine,
        screen=await FetchScreen(onlineContext, EDIT_ACCOUNT_ID),
        accountRow=ReadAccountRow(syncEngine, EDIT_ACCOUNT_ID),
        customerRow=ReadCustomerRow(syncEngine, EDIT_CUSTOMER_ID),
    )


@pytest.fixture(name="rejectCustomerRewrite")
def RejectCustomerRewriteFixture(restoreSeedState: None, syncEngine: Engine) -> Iterator[None]:
    """Yield nothing while a CHECK constraint refuses the customer row SAVE_CHANGES submits.

    The real CUSTDAT UPDATE then fails in PostgreSQL with SQLSTATE 23514. ALTER TABLE needs an
    ACCESS EXCLUSIVE lock, which an online context's open read transaction blocks, so a test
    requests this fixture before ``scenario``: the constraint is added before any context reads
    customers.customers and dropped after the contexts close, before restoreSeedState restores
    the tables.
    """
    with syncEngine.begin() as connection:
        connection.execute(DDL_LOCK_TIMEOUT_SQL)
        connection.execute(ADD_REWRITE_CHECK_SQL)
    try:
        yield
    finally:
        with syncEngine.begin() as connection:
            connection.execute(DDL_LOCK_TIMEOUT_SQL)
            connection.execute(DROP_REWRITE_CHECK_SQL)


class LockCase(NamedTuple):
    """A READ UPDATE of 9600-WRITE-PROCESSING replaced by a stub, and the literal answered."""

    repositoryClass: type
    methodName: str
    stub: Callable[..., Any]
    expectedMessage: str


# COULD-NOT-LOCK-ACCT-FOR-UPDATE (line 3912) and COULD-NOT-LOCK-CUST-FOR-UPDATE (line 3939).
LOCK_CASES = [
    LockCase(AccountRepository, "GetAccountForUpdate", RefuseAccountLock, MSG_LOCK_ACCOUNT),
    LockCase(CustomerRepository, "GetCustomerForUpdate", RefuseCustomerLock, MSG_LOCK_CUSTOMER),
]
LOCK_CASE_IDS = ["ACCTDAT", "CUSTDAT"]
# DATA-WAS-CHANGED-BEFORE-UPDATE: SHOW_DETAILS again, with the records 9600 read for update.
RECORD_CHANGED_SCREEN = ExpectedScreen(
    HTTP_RECORD_CHANGED,
    MSG_RECORD_CHANGED,
    INFO_PROMPT_FOR_CHANGES,
    (ACCOUNT_ID_FIELD,),
    ACCOUNT_ID_FIELD,
    SHOW_DETAILS,
)


def AmountCaseId(amountCase: AmountCase) -> str:
    """Return the pytest id of an AmountCase: the Credit Limit text as keyed."""
    return amountCase.rawText


async def FetchWithReadStubs(
    scenario: UpdateScenario, monkeypatch: pytest.MonkeyPatch, readFailureCase: ReadFailureCase
) -> LegacyMessageError:
    """Install the case's read stubs, fetch account 16, and return the error it is answered with."""
    for readStub in readFailureCase.readStubs:
        monkeypatch.setattr(readStub.repositoryClass, readStub.methodName, readStub.stub)
    with pytest.raises(LegacyMessageError) as excInfo:
        await FetchAccountForEdit(scenario.context, EDIT_ACCOUNT_ID)
    return excInfo.value


async def SaveExpectingError(scenario: UpdateScenario) -> LegacyMessageError:
    """Validate SAVE_CHANGES, press F5, and return the error the save is answered with."""
    confirmed = await ConfirmChanges(scenario, SAVE_CHANGES)
    saveRequest = PressKey(confirmed, UpdateAction.SAVE)
    return await ExpectScreenError(scenario.context, EDIT_ACCOUNT_ID, saveRequest)


# --- Phase 2: the edit fetch, the key edit and the field literals --------------------------------
def test_pinned_literals_are_expected_messages_of_the_edit_tables():
    """Each literal pinned verbatim is what a composed table row expects."""
    editCases = [*FIELD_LITERAL_CASES, *DATE_LITERAL_CASES, *BOUNDARY_REJECTED_CASES]
    assert PINNED_FIELD_LITERALS <= {fieldCase.expectedMessage for fieldCase in editCases}


async def test_fetch_shows_account_and_customer_with_ssn_unmasked(scenario):
    """9000-READ-ACCT reads CXACAIX, ACCTDAT and CUSTDAT; 3202 shows them, the SSN in full."""
    AssertShownScreen(scenario.screen, SHOW_DETAILS, INFO_PROMPT_FOR_CHANGES)
    shownFields = scenario.screen.fields.model_dump()
    assert {name: shownFields[name] for name in EXPECTED_FETCHED_FIELDS} == dict(
        EXPECTED_FETCHED_FIELDS
    )
    assert scenario.screen.screen_token


@pytest.mark.parametrize("keyCase", KEY_CASES, ids=KEY_CASE_IDS)
async def test_fetch_answers_the_key_edit_or_not_found_literal(onlineContext, keyCase):
    """1210-EDIT-ACCOUNT (lines 1783-1815), then the 9200 NOTFND builder (404)."""
    with pytest.raises(LegacyMessageError) as excInfo:
        await FetchAccountForEdit(onlineContext, keyCase.keyText)
    AssertErrorScreen(excInfo.value, keyCase.expected)


@pytest.mark.parametrize("readFailureCase", READ_FAILURE_CASES, ids=READ_FAILURE_CASE_IDS)
async def test_fetch_read_failure_answers_its_file_literal_and_writes_nothing(
    scenario, monkeypatch, readFailureCase
):
    """A CXACAIX, ACCTDAT or CUSTDAT READ that is not NORMAL: 9000's message, nothing written.

    A customer found after an ACCTDAT failure is shown in SHOW_DETAILS beside the blank account
    fields; every other failure shows the key alone, so the next ENTER fetches afresh.
    """
    error = await FetchWithReadStubs(scenario, monkeypatch, readFailureCase)
    AssertErrorScreen(error, readFailureCase.expected)
    assert error.Body()["fields"] == readFailureCase.expectedFields(scenario.screen)
    AssertRowsUnchanged(scenario)


@pytest.mark.parametrize("readFailureCase", UNREAD_ACCOUNT_CASES, ids=UNREAD_ACCOUNT_CASE_IDS)
async def test_enter_on_unread_account_screen_compares_with_its_token(
    scenario, monkeypatch, readFailureCase
):
    """The SHOW_DETAILS token of an ACCTDAT failure carries the before-image its screen shows.

    ENTER with the fields sent back unchanged runs 1205-COMPARE-OLD-NEW against that token, so
    the blank account and the fetched customer are no change; nothing is written.
    """
    error = await FetchWithReadStubs(scenario, monkeypatch, readFailureCase)
    shown = ScreenOfError(error)
    enterRequest = PressKey(shown, UpdateAction.ENTER)
    noChange = await ExpectScreenError(scenario.context, EDIT_ACCOUNT_ID, enterRequest)
    AssertErrorScreen(noChange, NO_CHANGE_SCREEN)
    assert noChange.Body()["fields"] == shown.fields.model_dump()
    AssertRowsUnchanged(scenario)


async def test_enter_without_token_edits_the_keyed_account_id(onlineContext):
    """Before details are fetched ACCTSID is unprotected and is the key ENTER edits."""
    keyedFields = AccountScreenFields(acct_id=INVALID_KEY_TEXT)
    request = AccountUpdateRequest(action=UpdateAction.ENTER, fields=keyedFields)
    error = await ExpectScreenError(onlineContext, EDIT_ACCOUNT_ID, request)
    AssertErrorScreen(error, KeyFailure(HTTP_EDIT_FAILURE, MSG_ACCOUNT_KEY_INVALID))


@pytest.mark.parametrize("fieldCase", FIELD_LITERAL_CASES, ids=CaseId)
async def test_field_edit_answers_its_literal(scenario, fieldCase):
    """Each failing 1215-1280 edit: its message, its field flagged, the cursor on it."""
    await AssertEditFailureCase(scenario, fieldCase)


@pytest.mark.parametrize("fieldCase", DATE_LITERAL_CASES, ids=CaseId)
async def test_date_edit_answers_its_csutldpy_literal(scenario, fieldCase):
    """EDIT-DATE-CCYYMMDD: the label, the CSUTLDPY suffix and the parts its flags fail."""
    await AssertEditFailureCase(scenario, fieldCase)


@pytest.mark.parametrize("fieldCase", BOUNDARY_REJECTED_CASES, ids=CaseId)
async def test_boundary_value_is_rejected(scenario, fieldCase):
    """The failing side of the SSN, FICO and state/ZIP 88-level ranges."""
    await AssertEditFailureCase(scenario, fieldCase)


@pytest.mark.parametrize("changes", BOUNDARY_ACCEPTED_CHANGES, ids=ChangesId)
async def test_boundary_value_is_accepted(scenario, changes):
    """The passing side of the same edits validates, and validating writes nothing."""
    await ConfirmChanges(scenario, {**VALID_BASELINE, **changes})
    AssertRowsUnchanged(scenario)


@pytest.mark.parametrize("fieldCase", MULTI_FIELD_CASES, ids=CaseId)
async def test_all_field_mode_keeps_the_first_message_and_every_failing_field(scenario, fieldCase):
    """WS-RETURN-MSG keeps the first message set; every failing field keeps its flag."""
    await AssertEditFailureCase(scenario, fieldCase)


@pytest.mark.parametrize("birthOffsetDays", [0, 1], ids=["today", "tomorrow"])
async def test_birth_date_not_before_today_cannot_be_in_the_future(
    scenario, monkeypatch, birthOffsetDays
):
    """EDIT-DATE-OF-BIRTH passes only when today is later (CSUTLDPY lines 341-370)."""
    PatchToday(monkeypatch)
    birthDate = FIXED_TODAY + timedelta(days=birthOffsetDays)
    futureCase = Case(
        BirthDateChanges(birthDate), BIRTH_DATE_LABEL + FUTURE_DATE, *BIRTH_DATE_FIELDS
    )
    await AssertEditFailureCase(scenario, futureCase)


async def test_birth_date_before_today_is_accepted(scenario, monkeypatch):
    """A date of birth one day before FUNCTION CURRENT-DATE validates."""
    PatchToday(monkeypatch)
    birthDate = FIXED_TODAY - timedelta(days=1)
    await ConfirmChanges(scenario, {**VALID_BASELINE, **BirthDateChanges(birthDate)})
    AssertRowsUnchanged(scenario)


@pytest.mark.usefixtures("restoreSeedState")
@pytest.mark.parametrize("amountCase", RAW_AMOUNT_CASES, ids=AmountCaseId)
async def test_numval_c_amount_validates_and_is_saved_truncated(scenario, amountCase):
    """TEST-NUMVAL-C accepts the text as keyed (line 2200); F5 stores NUMVAL-C truncated."""
    changes = {**VALID_BASELINE, "credit_limit": amountCase.rawText}
    confirmed = await ConfirmChanges(scenario, changes)
    saveRequest = PressKey(confirmed, UpdateAction.SAVE)
    done = await ProcessAccountUpdate(scenario.context, EDIT_ACCOUNT_ID, saveRequest)
    AssertShownScreen(done, CHANGES_DONE, INFO_CONFIRM_UPDATE_SUCCESS)
    savedAccount = ReadAccountRow(scenario.syncEngine, EDIT_ACCOUNT_ID)
    assert savedAccount == RewrittenAccount(scenario.accountRow, amountCase.storedAmount)


# --- Phase 3: the ACUP-CHANGE-ACTION state machine -----------------------------------------------
@pytest.mark.parametrize(
    "action", list(UpdateAction), ids=[action.value for action in UpdateAction]
)
async def test_first_key_press_runs_only_the_key_edit_then_fetches(scenario, action):
    """DETAILS_NOT_FETCHED: 1200 runs 1210 alone (lines 1433-1446), and F5 or F12 is ENTER."""
    keyedFields = AccountScreenFields(acct_id=EDIT_ACCOUNT_ID, **GARBAGE_DETAILS)
    request = AccountUpdateRequest(action=action, fields=keyedFields)
    shown = await ProcessAccountUpdate(scenario.context, EDIT_ACCOUNT_ID, request)
    AssertShownScreen(shown, SHOW_DETAILS, INFO_PROMPT_FOR_CHANGES)
    assert shown.fields == scenario.screen.fields
    AssertRowsUnchanged(scenario)


@pytest.mark.parametrize("noChangeCase", NO_CHANGE_CASES, ids=NO_CHANGE_IDS)
async def test_unchanged_fields_answer_no_change_before_any_edit(scenario, noChangeCase):
    """1205-COMPARE-OLD-NEW runs first; no change exits 1200 before every field edit."""
    shown = await FetchScreen(scenario.context, noChangeCase.acctId)
    request = WithChanges(shown, noChangeCase.changes)
    error = await ExpectScreenError(scenario.context, noChangeCase.acctId, request)
    AssertErrorScreen(error, NO_CHANGE_SCREEN)
    assert error.Body()["fields"] == shown.fields.model_dump()
    AssertRowsUnchanged(scenario)


async def test_valid_change_asks_for_confirmation_without_writing(scenario):
    """A clean pass sets ACUP-CHANGES-OK-NOT-CONFIRMED and shows the values to confirm."""
    confirmed = await ConfirmChanges(scenario, SAVE_CHANGES)
    shownTexts = (confirmed.fields.credit_limit, confirmed.fields.addr_state_cd)
    assert shownTexts == (SAVED_CREDIT_LIMIT_SHOWN, SAVED_STATE)
    AssertRowsUnchanged(scenario)


async def test_fetched_token_keeps_the_protected_fields(scenario):
    """3320-UNPROTECT-FEW-ATTRS keeps ACCTSID, ACSTNUM and ACSCTRY protected: keyed text is lost."""
    confirmed = await ConfirmChanges(scenario, {**SAVE_CHANGES, **PROTECTED_FIELD_CHANGES})
    shownFields = confirmed.fields.model_dump()
    assert {name: shownFields[name] for name in PROTECTED_FIELD_CHANGES} == {
        name: EXPECTED_FETCHED_FIELDS[name] for name in PROTECTED_FIELD_CHANGES
    }


async def test_enter_on_confirmation_screen_runs_no_edit(scenario):
    """In CHANGES_OK_NOT_CONFIRMED every field is protected, so ENTER keeps the screen."""
    confirmed = await ConfirmChanges(scenario, SAVE_CHANGES)
    request = WithChanges(confirmed, GARBAGE_DETAILS)
    kept = await ProcessAccountUpdate(scenario.context, EDIT_ACCOUNT_ID, request)
    AssertShownScreen(kept, CHANGES_OK_NOT_CONFIRMED, INFO_PROMPT_FOR_CONFIRMATION)
    assert kept.fields == confirmed.fields
    AssertRowsUnchanged(scenario)


async def test_save_before_validation_only_validates(scenario):
    """F5 is valid only in CHANGES_OK_NOT_CONFIRMED (lines 905-916); elsewhere it is ENTER."""
    request = WithChanges(scenario.screen, SAVE_CHANGES, UpdateAction.SAVE)
    validated = await ProcessAccountUpdate(scenario.context, EDIT_ACCOUNT_ID, request)
    AssertShownScreen(validated, CHANGES_OK_NOT_CONFIRMED, INFO_PROMPT_FOR_CONFIRMATION)
    AssertRowsUnchanged(scenario)


@pytest.mark.usefixtures("restoreSeedState")
async def test_save_writes_the_validated_token_values_not_the_request_fields(scenario):
    """9600-WRITE-PROCESSING rewrites ACUP-NEW-DETAILS from the token, whatever is keyed."""
    confirmed = await ConfirmChanges(scenario, SAVE_CHANGES)
    saveRequest = WithChanges(confirmed, OTHER_FIELD_VALUES, UpdateAction.SAVE)
    done = await ProcessAccountUpdate(scenario.context, EDIT_ACCOUNT_ID, saveRequest)
    AssertShownScreen(done, CHANGES_DONE, INFO_CONFIRM_UPDATE_SUCCESS)
    savedAccount = ReadAccountRow(scenario.syncEngine, EDIT_ACCOUNT_ID)
    assert savedAccount == RewrittenAccount(scenario.accountRow, SAVED_CREDIT_LIMIT)
    savedCustomer = ReadCustomerRow(scenario.syncEngine, EDIT_CUSTOMER_ID)
    assert savedCustomer == {**scenario.customerRow, "addr_state_cd": SAVED_STATE}


@pytest.mark.usefixtures("restoreSeedState")
async def test_cancel_rereads_the_record_and_discards_the_edits(scenario):
    """F12 (lines 2572-2580) re-reads the record: the keyed edits go, a newer balance shows."""
    editRequest = WithChanges(scenario.screen, {STATUS_FIELD: "X"})
    error = await ExpectScreenError(scenario.context, EDIT_ACCOUNT_ID, editRequest)
    assert error.Body()["state"] == CHANGES_NOT_OK
    SetAccountBalance(scenario.syncEngine, EDIT_ACCOUNT_ID, CHANGED_BALANCE)
    cancelRequest = PressKey(ScreenOfError(error), UpdateAction.CANCEL)
    cancelled = await ProcessAccountUpdate(scenario.context, EDIT_ACCOUNT_ID, cancelRequest)
    AssertShownScreen(cancelled, SHOW_DETAILS, INFO_PROMPT_FOR_CHANGES)
    shownTexts = (cancelled.fields.active_status, cancelled.fields.curr_balance)
    assert shownTexts == (EXPECTED_FETCHED_FIELDS[STATUS_FIELD], CHANGED_BALANCE_SHOWN)


@pytest.mark.usefixtures("restoreSeedState")
async def test_key_after_changes_done_returns_the_empty_key_screen(scenario):
    """After ACUP-CHANGES-OKAYED-AND-DONE any key starts afresh, reading and writing nothing."""
    confirmed = await ConfirmChanges(scenario, SAVE_CHANGES)
    done = await ProcessAccountUpdate(
        scenario.context, EDIT_ACCOUNT_ID, PressKey(confirmed, UpdateAction.SAVE)
    )
    nextRequest = WithChanges(done, OTHER_FIELD_VALUES)
    restarted = await ProcessAccountUpdate(scenario.context, EDIT_ACCOUNT_ID, nextRequest)
    AssertShownScreen(restarted, DETAILS_NOT_FETCHED, INFO_PROMPT_FOR_SEARCH_KEYS)
    assert restarted.fields == AccountScreenFields()
    savedAccount = ReadAccountRow(scenario.syncEngine, EDIT_ACCOUNT_ID)
    assert savedAccount == RewrittenAccount(scenario.accountRow, SAVED_CREDIT_LIMIT)


@pytest.mark.usefixtures("restoreSeedState")
async def test_key_after_failed_save_returns_the_empty_key_screen(scenario, monkeypatch):
    """After a CHANGES_FAILED save any key starts afresh as well."""
    monkeypatch.setattr(AccountRepository, "GetAccountForUpdate", RefuseAccountLock)
    error = await SaveExpectingError(scenario)
    nextRequest = WithChanges(ScreenOfError(error), OTHER_FIELD_VALUES)
    restarted = await ProcessAccountUpdate(scenario.context, EDIT_ACCOUNT_ID, nextRequest)
    AssertShownScreen(restarted, DETAILS_NOT_FETCHED, INFO_PROMPT_FOR_SEARCH_KEYS)
    assert restarted.fields == AccountScreenFields()
    AssertRowsUnchanged(scenario)


@pytest.mark.parametrize("forgeToken", TOKEN_FORGERS, ids=TOKEN_FORGER_IDS)
async def test_forged_token_restarts_at_details_not_fetched(scenario, forgeToken):
    """Without a valid token ACCTSID is unprotected again: its keyed text is edited as the key."""
    forgedToken = forgeToken(scenario.screen, scenario.context.settings)
    keyedChanges = {**SAVE_CHANGES, ACCOUNT_ID_FIELD: INVALID_KEY_TEXT}
    keyedRequest = WithChanges(scenario.screen, keyedChanges)
    forgedRequest = keyedRequest.model_copy(update={"screen_token": forgedToken})
    error = await ExpectScreenError(scenario.context, EDIT_ACCOUNT_ID, forgedRequest)
    AssertErrorScreen(error, KeyFailure(HTTP_EDIT_FAILURE, MSG_ACCOUNT_KEY_INVALID))
    AssertRowsUnchanged(scenario)


# --- Phase 4: the F5 save outcomes of 9600-WRITE-PROCESSING --------------------------------------
@pytest.mark.usefixtures("restoreSeedState")
@pytest.mark.parametrize("lockCase", LOCK_CASES, ids=LOCK_CASE_IDS)
async def test_lock_failure_answers_its_file_literal_and_writes_nothing(
    scenario, monkeypatch, lockCase
):
    """A non-NORMAL READ UPDATE ends 9600 before either REWRITE (lines 3896-3950)."""
    monkeypatch.setattr(lockCase.repositoryClass, lockCase.methodName, lockCase.stub)
    error = await SaveExpectingError(scenario)
    AssertErrorScreen(error, SaveFailure(HTTP_RECORD_LOCKED, lockCase.expectedMessage))
    AssertRowsUnchanged(scenario)


@pytest.mark.usefixtures("restoreSeedState")
async def test_record_changed_since_fetch_shows_the_fresh_record(scenario):
    """9700-CHECK-CHANGE-IN-REC finds the record changed: nothing is rewritten, fresh data shows."""
    confirmed = await ConfirmChanges(scenario, SAVE_CHANGES)
    SetAccountBalance(scenario.syncEngine, EDIT_ACCOUNT_ID, CHANGED_BALANCE)
    saveRequest = PressKey(confirmed, UpdateAction.SAVE)
    error = await ExpectScreenError(scenario.context, EDIT_ACCOUNT_ID, saveRequest)
    AssertErrorScreen(error, RECORD_CHANGED_SCREEN)
    shownFields = error.Body()["fields"]
    assert (shownFields["curr_balance"], shownFields["credit_limit"]) == (
        CHANGED_BALANCE_SHOWN,
        EXPECTED_FETCHED_FIELDS["credit_limit"],
    )
    changedAccount = {**scenario.accountRow, "curr_balance": CHANGED_BALANCE}
    assert ReadAccountRow(scenario.syncEngine, EDIT_ACCOUNT_ID) == changedAccount
    assert ReadCustomerRow(scenario.syncEngine, EDIT_CUSTOMER_ID) == scenario.customerRow


@pytest.mark.usefixtures("restoreSeedState", "rejectCustomerRewrite")
async def test_failed_customer_rewrite_keeps_the_committed_account_change(scenario, caplog):
    """RECOVERY(NONE): the ACCTDAT REWRITE stands when the CUSTDAT REWRITE fails (4062-4100).

    A CHECK constraint refuses the customer row, so the real CUSTDAT UPDATE fails after the
    ACCTDAT unit has committed. SQLSTATE 23514 is IOERR (RESP 17) on CUSTDAT, the CUSTDAT unit
    is rolled back and its row lock released, and the customer row stays as it was.
    """
    caplog.set_level("WARNING", logger=coactupc.LOGGER.name)
    error = await SaveExpectingError(scenario)
    AssertErrorScreen(error, SaveFailure(HTTP_FILE_ERROR, MSG_UPDATE_FAILED))
    loggedConditions = [
        (logRecord.name, logRecord.levelname, logRecord.getMessage())
        for logRecord in caplog.records
    ]
    assert loggedConditions == [(coactupc.LOGGER.name, "ERROR", CUSTOMER_REWRITE_REFUSED_LOG)]
    savedAccount = ReadAccountRow(scenario.syncEngine, EDIT_ACCOUNT_ID)
    assert savedAccount == RewrittenAccount(scenario.accountRow, SAVED_CREDIT_LIMIT)
    assert ReadCustomerRow(scenario.syncEngine, EDIT_CUSTOMER_ID) == scenario.customerRow
    assert LockCustomerRowWithoutWaiting(scenario.syncEngine, EDIT_CUSTOMER_ID) == 1


@pytest.mark.parametrize("fieldCase", LOW_FICO_CASES, ids=["credit-limit-only", "fico-corrected"])
async def test_low_fico_account_fails_the_edits_its_seed_values_fail(scenario, fieldCase):
    """Any change runs every edit, so account 1's seeded FICO 274 blocks a Credit Limit change."""
    lowFicoScreen = await FetchScreen(scenario.context, LOW_FICO_ACCOUNT_ID)
    request = WithChanges(lowFicoScreen, fieldCase.changes)
    error = await ExpectScreenError(scenario.context, LOW_FICO_ACCOUNT_ID, request)
    AssertErrorScreen(error, EditFailure(fieldCase.expectedMessage, fieldCase.expectedFieldErrors))


async def test_low_fico_account_validates_once_its_seed_defects_are_corrected(scenario):
    """With the FICO score, state and Phone Number 2 area code corrected, account 1 validates."""
    lowFicoScreen = await FetchScreen(scenario.context, LOW_FICO_ACCOUNT_ID)
    request = WithChanges(lowFicoScreen, LOW_FICO_CORRECTIONS)
    confirmed = await ProcessAccountUpdate(scenario.context, LOW_FICO_ACCOUNT_ID, request)
    AssertShownScreen(confirmed, CHANGES_OK_NOT_CONFIRMED, INFO_PROMPT_FOR_CONFIRMATION)
