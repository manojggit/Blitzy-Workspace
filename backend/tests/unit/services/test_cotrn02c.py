"""Unit tests for COTRN02C (CT02, Transaction Add, BMS map COTRN02) and its CSUTLDTC date edits.

app.services.transactions.cotrn02c.AddTransaction is called directly, never over HTTP, against
the migrated and seeded test database, and the seed is restored after every case. Expected
literals, cursor fields and statuses are typed from cbl/COTRN02C.cbl:

- VALIDATE-INPUT-KEY-FIELDS with READ-CXACAIX-FILE and READ-CCXREF-FILE (lines 193-230,
  576-637): numeric keys, NOTFND (404) and WHEN OTHER (503);
- VALIDATE-INPUT-DATA-FIELDS (lines 251-436): presence in source order, the type and category
  IS NOT NUMERIC edits, the amount picture, both date formats, the two CALL 'CSUTLDTC' checks
  with the CEEDAYS message 2513 exemption, and the merchant ID edit;
- the CONFIRMI EVALUATE of PROCESS-ENTER-KEY (lines 169-188);
- ADD-TRANSACTION and WRITE-TRANSACT-FILE (lines 442-466, 711-749): the next TRAN-ID, the
  stored record, the success STRING with its two spaces and the DUPREC literal (409);
- COPY-LAST-TRAN-DATA (lines 471-495): VALIDATE-INPUT-KEY-FIELDS, then the F5 copy of the
  highest TRANSACT record;
- STARTBR-TRANSACT-FILE and READPREV-TRANSACT-FILE (lines 642-697), which ADD-TRANSACTION and
  COPY-LAST-TRAN-DATA perform: WHEN OTHER (503) before any write.

COTRN02C edits in first-failure mode, so a refusal names one literal and puts the cursor on its
field. Seed account 00000000004 holds card 2988091353094312, a 40.00 balance and, like every
account, no transaction; account 00000000099 and card 4000000000000000 do not exist.

Every refused, unconfirmed or copy-only request is guarded by reading every column of every
TRANSACT, ACCTDAT and TCATBALF row before and after the call, so an in-place rewrite or a
balance change fails the case as surely as an added record. Each of those cases keeps a TRANSACT
record on file, so a rewrite has a record to change; only the F5 empty-file case runs on the
empty seed file, because its READPREV ENDFILE outcome needs one. A confirmed F5 copy likewise
leaves its source record and every ACCTDAT and TCATBALF row unchanged. The F5 invalid-key cases
make the TRANSACT browse fail with IOERR, so a browse run ahead of the key edits shows as a 503
and as an awaited read.
"""

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, NamedTuple
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Engine, text

from app.core.errors import FileIoError, LegacyMessageError
from app.repositories.card_xref_repository import CardXrefRepository
from app.repositories.transaction_repository import TransactionRepository
from app.schemas.transactions import (
    TransactionAddFields,
    TransactionAddRequest,
    TransactionAddResponse,
)
from app.services.transactions import cotrn02c
from app.utils.date_validation import CEEDAYS_VALID_SEVERITY, CheckDate

REGULAR_USER_TYPE = "U"
HTTP_EDIT_FAILURE = 400
HTTP_NOT_FOUND = 404
HTTP_DUPLICATE_KEY = 409
HTTP_FILE_ERROR = 503

# --- Seed data ---------------------------------------------------------------------------------
ADDING_ACCT_ID = "00000000004"
ADDING_CARD_NUM = "2988091353094312"
MASKED_ADDING_CARD_NUM = "************4312"
SEED_BALANCE = Decimal("40.00")
UNKNOWN_ACCT_ID = "00000000099"
UNKNOWN_CARD_NUM = "4000000000000000"
# The CICS files READ-CXACAIX-FILE, READ-CCXREF-FILE and the TRANSACT browse address.
ACCOUNT_PATH_FILE = "CXACAIX"
CARD_XREF_FILE = "CCXREF"
TRANSACTION_FILE = "TRANSACT"

# --- The COTRN02 map keyed for the seed account; every field passes the CT02 edits -------------
ENTERED_DATE = "2022-07-17"
ENTERED_DATE_MIDNIGHT = datetime(2022, 7, 17)
VALID_SCREEN_FIELDS = MappingProxyType(
    {
        "acct_id": ADDING_ACCT_ID,
        "card_num": "",
        "type_cd": "01",
        "cat_cd": "0001",
        "source": "POS TERM",
        "desc": "UNIT TEST PURCHASE",
        "amount": "+00000050.00",
        "orig_date": ENTERED_DATE,
        "proc_date": ENTERED_DATE,
        "merchant_id": "000000001",
        "merchant_name": "UNIT MERCHANT",
        "merchant_city": "UNIT CITY",
        "merchant_zip": "00000",
        "confirm": "Y",
    }
)
ENTER_ACTION = "enter"
COPY_LAST_ACTION = "copy_last"
NO_OVERRIDES: Mapping[str, str] = MappingProxyType({})
# WS-DATE-FORMAT, the mask COTRN02C passes to CSUTLDTC. A date before 1582-10-15 makes CEEDAYS
# answer message 2513, which COTRN02C lets through.
CSUTLDTC_DATE_MASK = "YYYY-MM-DD"
PRE_GREGORIAN_DATE = "1500-01-01"
CEEDAYS_MSG_UNSUPPORTED_RANGE = 2513

# --- WS-MESSAGE literals of cbl/COTRN02C.cbl ---------------------------------------------------
ACCOUNT_OR_CARD_REQUIRED_MESSAGE = "Account or Card Number must be entered..."  # line 226
ACCOUNT_NOT_NUMERIC_MESSAGE = "Account ID must be Numeric..."  # line 199
CARD_NOT_NUMERIC_MESSAGE = "Card Number must be Numeric..."  # line 213
ACCOUNT_NOT_FOUND_MESSAGE = "Account ID NOT found..."  # line 593
ACCOUNT_XREF_FAILED_MESSAGE = "Unable to lookup Acct in XREF AIX file..."  # line 600
CARD_NOT_FOUND_MESSAGE = "Card Number NOT found..."  # line 626
CARD_XREF_FAILED_MESSAGE = "Unable to lookup Card # in XREF file..."  # line 633
TYPE_CD_EMPTY_MESSAGE = "Type CD can NOT be empty..."  # line 254
TYPE_NOT_NUMERIC_MESSAGE = "Type CD must be Numeric..."  # line 325
CATEGORY_NOT_NUMERIC_MESSAGE = "Category CD must be Numeric..."  # line 331
AMOUNT_FORMAT_MESSAGE = "Amount should be in format -99999999.99"  # line 345
ORIG_DATE_FORMAT_MESSAGE = "Orig Date should be in format YYYY-MM-DD"  # line 360
PROC_DATE_FORMAT_MESSAGE = "Proc Date should be in format YYYY-MM-DD"  # line 375
ORIG_DATE_INVALID_MESSAGE = "Orig Date - Not a valid date..."  # line 401
PROC_DATE_INVALID_MESSAGE = "Proc Date - Not a valid date..."  # line 421
MERCHANT_ID_NOT_NUMERIC_MESSAGE = "Merchant ID must be Numeric..."  # line 432
CONFIRM_PROMPT_MESSAGE = "Confirm to add this transaction..."  # line 178
CONFIRM_INVALID_MESSAGE = "Invalid value. Valid values are (Y/N)..."  # line 184
DUPLICATE_TRAN_ID_MESSAGE = "Tran ID already exist..."  # line 738
TRAN_LOOKUP_FAILED_MESSAGE = "Unable to lookup Transaction..."  # lines 664, 693
# The STRING of lines 728-733: 'successfully. ' then ' Your Tran ID is ' leave two spaces.
ADD_SUCCESS_TEMPLATE = "Transaction added successfully.  Your Tran ID is {tranId}."
# WS-TRAN-ID-N PIC 9(16) (lines 444-449): zeros on READPREV ENDFILE plus 1, and the ADD 1 that
# keeps only the low-order sixteen digits.
FIRST_TRAN_ID = "0000000000000001"
MAX_TRAN_ID = "9999999999999999"
WRAPPED_TRAN_ID = "0000000000000000"

# The presence EVALUATE (lines 251-320) in source order: map field, then its literal.
REQUIRED_FIELD_LITERALS = (
    ("type_cd", TYPE_CD_EMPTY_MESSAGE),
    ("cat_cd", "Category CD can NOT be empty..."),
    ("source", "Source can NOT be empty..."),
    ("desc", "Description can NOT be empty..."),
    ("amount", "Amount can NOT be empty..."),
    ("orig_date", "Orig Date can NOT be empty..."),
    ("proc_date", "Proc Date can NOT be empty..."),
    ("merchant_id", "Merchant ID can NOT be empty..."),
    ("merchant_name", "Merchant Name can NOT be empty..."),
    ("merchant_city", "Merchant City can NOT be empty..."),
    ("merchant_zip", "Merchant Zip can NOT be empty..."),
)

# --- The highest TRANSACT record COPY-LAST-TRAN-DATA reads -------------------------------------
LAST_TRAN_ID = "2022071800000050"
NEXT_AFTER_LAST_TRAN_ID = "2022071800000051"
LAST_ROW_VALUES = MappingProxyType(
    {
        "tran_type_cd": "03",
        "tran_cat_cd": 2,
        "tran_source": "OPERATOR",
        "tran_desc": "UNIT TEST REFUND",
        "tran_amt": Decimal("1.00"),
        "merchant_id": "000000777",
        "merchant_name": "COPIED MERCHANT",
        "merchant_city": "COPIED CITY",
        "merchant_zip": "12345",
        "card_num": ADDING_CARD_NUM,
        "orig_ts": datetime(2022, 6, 10, 19, 27, 53),
        "orig_ts_format": "S",
        "proc_ts": datetime(2022, 7, 18),
        "proc_ts_format": "D",
    }
)
# The map after F5: the record's fields, TRAN-AMT through +99999999.99, TRAN-CAT-CD as 4 digits,
# each timestamp's first 10 characters, and the keys as the xref read left them.
COPIED_SCREEN_FIELDS = MappingProxyType(
    {
        "acct_id": ADDING_ACCT_ID,
        "card_num": MASKED_ADDING_CARD_NUM,
        "type_cd": "03",
        "cat_cd": "0002",
        "source": "OPERATOR",
        "desc": "UNIT TEST REFUND",
        "amount": "+00000001.00",
        "orig_date": "2022-06-10",
        "proc_date": "2022-07-18",
        "merchant_id": "000000777",
        "merchant_name": "COPIED MERCHANT",
        "merchant_city": "COPIED CITY",
        "merchant_zip": "12345",
        "confirm": "",
    }
)

# --- The TRAN-RECORD ADD-TRANSACTION writes (lines 450-465) ------------------------------------
# Both timestamps hold the entered date and 16 spaces: format T, stored as midnight.
ADDED_ROW = MappingProxyType(
    {
        "tran_type_cd": "01",
        "tran_cat_cd": 1,
        "tran_source": "POS TERM",
        "tran_desc": "UNIT TEST PURCHASE",
        "tran_amt": Decimal("50.00"),
        "merchant_id": "000000001",
        "merchant_name": "UNIT MERCHANT",
        "merchant_city": "UNIT CITY",
        "merchant_zip": "00000",
        "card_num": ADDING_CARD_NUM,
        "orig_ts": ENTERED_DATE_MIDNIGHT,
        "orig_ts_format": "T",
        "proc_ts": ENTERED_DATE_MIDNIGHT,
        "proc_ts_format": "T",
    }
)
# F5 then a confirmed add: the copied values, with the copied dates stored in format T.
COPIED_ADDED_ROW = MappingProxyType(
    {
        **LAST_ROW_VALUES,
        "orig_ts": datetime(2022, 6, 10),
        "orig_ts_format": "T",
        "proc_ts_format": "T",
    }
)

# --- Plain SQL; every value is bound -----------------------------------------------------------
INSERT_TRANSACTION_SQL = text(
    "INSERT INTO transactions.transactions (tran_id, tran_type_cd, tran_cat_cd, tran_source,"
    " tran_desc, tran_amt, merchant_id, merchant_name, merchant_city, merchant_zip, card_num,"
    " orig_ts, orig_ts_format, proc_ts, proc_ts_format) VALUES (:tran_id, :tran_type_cd,"
    " :tran_cat_cd, :tran_source, :tran_desc, :tran_amt, :merchant_id, :merchant_name,"
    " :merchant_city, :merchant_zip, :card_num, :orig_ts, :orig_ts_format, :proc_ts,"
    " :proc_ts_format)"
)
STORED_ROW_SQL = text(
    "SELECT tran_type_cd, tran_cat_cd, tran_source, tran_desc, tran_amt, merchant_id,"
    " merchant_name, merchant_city, merchant_zip, card_num, orig_ts, orig_ts_format, proc_ts,"
    " proc_ts_format FROM transactions.transactions WHERE tran_id = :tranId"
)
COUNT_TRANSACTIONS_SQL = text("SELECT count(*) FROM transactions.transactions")
READ_BALANCE_SQL = text("SELECT curr_balance FROM accounts.accounts WHERE acct_id = :acctId")
# Every column of every TRANSACT, ACCTDAT and TCATBALF row in primary-key order: the stored
# state a refused, unconfirmed or copy-only request must leave exactly as it found it.
TRANSACT_TABLE = "transactions.transactions"
STORED_STATE_SQL = MappingProxyType(
    {
        TRANSACT_TABLE: text("SELECT * FROM transactions.transactions ORDER BY tran_id"),
        "accounts.accounts": text("SELECT * FROM accounts.accounts ORDER BY acct_id"),
        "transactions.transaction_category_balances": text(
            "SELECT * FROM transactions.transaction_category_balances"
            " ORDER BY acct_id, tran_type_cd, tran_cat_cd"
        ),
    }
)


class Refusal(NamedTuple):
    """A screen COTRN02C sends with ERR-FLG on: its HTTP status, literal and cursor field."""

    httpStatus: int
    message: str
    cursorField: str


class XrefFailure(NamedTuple):
    """An xref read method replaced by one failing with IOERR, the keys and the refusal."""

    methodName: str
    fileName: str
    overrides: Mapping[str, str]
    refusal: Refusal


class TranLookupFailure(NamedTuple):
    """A TRANSACT browse method replaced by one failing with IOERR, and the keys and action."""

    methodName: str
    overrides: Mapping[str, str]


class KeyedRefusal(NamedTuple):
    """Map fields keyed over the valid screen, and the refusal COTRN02C answers them with."""

    overrides: Mapping[str, str]
    refusal: Refusal


class ShownField(NamedTuple):
    """A map field the 200 confirm prompt sends back, with the value it must hold."""

    fieldName: str
    value: str


def EditFailure(message: str, cursorField: str) -> Refusal:
    """Return the 400 refusal of a field edit or of the CONFIRMI WHEN OTHER."""
    return Refusal(HTTP_EDIT_FAILURE, message, cursorField)


# --- VALIDATE-INPUT-KEY-FIELDS: the account first, else the card, else neither -----------------
KEY_EDIT_CASES = [
    pytest.param(
        {"acct_id": "", "card_num": ""},
        EditFailure(ACCOUNT_OR_CARD_REQUIRED_MESSAGE, "acct_id"),
        id="both-keys-blank",
    ),
    pytest.param(
        {"acct_id": "abc"},
        EditFailure(ACCOUNT_NOT_NUMERIC_MESSAGE, "acct_id"),
        id="account-not-numeric",
    ),
    pytest.param(
        {"acct_id": "", "card_num": "12ab"},
        EditFailure(CARD_NOT_NUMERIC_MESSAGE, "card_num"),
        id="card-not-numeric",
    ),
    pytest.param(
        {"acct_id": UNKNOWN_ACCT_ID},
        Refusal(HTTP_NOT_FOUND, ACCOUNT_NOT_FOUND_MESSAGE, "acct_id"),
        id="account-not-found",
    ),
    pytest.param(
        {"acct_id": "", "card_num": UNKNOWN_CARD_NUM},
        Refusal(HTTP_NOT_FOUND, CARD_NOT_FOUND_MESSAGE, "card_num"),
        id="card-not-found",
    ),
]

# F5 runs the same key edits before the TRANSACT browse, so a key failure reads and copies nothing.
COPY_LAST_KEY_EDIT_CASES = [
    pytest.param(
        KeyedRefusal({**keyCase.values[0], "action": COPY_LAST_ACTION}, keyCase.values[1]),
        id=f"copy-last-{keyCase.id}",
    )
    for keyCase in KEY_EDIT_CASES
]

# --- The presence EVALUATE: each field blanked while every earlier field is valid --------------
REQUIRED_FIELD_CASES = [
    pytest.param({fieldName: ""}, EditFailure(message, fieldName), id=f"{fieldName}-blank")
    for fieldName, message in REQUIRED_FIELD_LITERALS
] + [
    pytest.param(
        {"type_cd": "", "amount": ""},
        EditFailure(TYPE_CD_EMPTY_MESSAGE, "type_cd"),
        id="type-cd-and-amount-blank",
    ),
]

# --- Numeric codes, amount picture, date formats, CSUTLDTC, merchant ID and CONFIRMI -----------
DATA_EDIT_CASES = [
    pytest.param(
        {"type_cd": "0A"}, EditFailure(TYPE_NOT_NUMERIC_MESSAGE, "type_cd"), id="type-cd-alpha"
    ),
    pytest.param(
        {"cat_cd": "00X1"},
        EditFailure(CATEGORY_NOT_NUMERIC_MESSAGE, "cat_cd"),
        id="category-alpha",
    ),
    # IS NOT NUMERIC tests the whole PIC X(4) field, whose unkeyed positions hold spaces.
    pytest.param(
        {"cat_cd": "1"},
        EditFailure(CATEGORY_NOT_NUMERIC_MESSAGE, "cat_cd"),
        id="category-short-entry",
    ),
    pytest.param(
        {"amount": "$1,234.567"}, EditFailure(AMOUNT_FORMAT_MESSAGE, "amount"), id="amount-currency"
    ),
    pytest.param(
        {"amount": "00000050.00"}, EditFailure(AMOUNT_FORMAT_MESSAGE, "amount"), id="amount-no-sign"
    ),
    pytest.param(
        {"amount": "+0000005000"},
        EditFailure(AMOUNT_FORMAT_MESSAGE, "amount"),
        id="amount-no-point",
    ),
    pytest.param(
        {"orig_date": "2022/07/17"},
        EditFailure(ORIG_DATE_FORMAT_MESSAGE, "orig_date"),
        id="orig-date-format",
    ),
    pytest.param(
        {"proc_date": "2022/07/17"},
        EditFailure(PROC_DATE_FORMAT_MESSAGE, "proc_date"),
        id="proc-date-format",
    ),
    # Both format edits run before either CALL 'CSUTLDTC'.
    pytest.param(
        {"orig_date": "2022-02-30", "proc_date": "2022/07/17"},
        EditFailure(PROC_DATE_FORMAT_MESSAGE, "proc_date"),
        id="formats-before-csutldtc",
    ),
    pytest.param(
        {"orig_date": "2022-02-30"},
        EditFailure(ORIG_DATE_INVALID_MESSAGE, "orig_date"),
        id="orig-date-invalid",
    ),
    pytest.param(
        {"proc_date": "2022-02-30"},
        EditFailure(PROC_DATE_INVALID_MESSAGE, "proc_date"),
        id="proc-date-invalid",
    ),
    pytest.param(
        {"proc_date": "2022-02-30", "merchant_id": "12345678A"},
        EditFailure(PROC_DATE_INVALID_MESSAGE, "proc_date"),
        id="csutldtc-before-merchant-id",
    ),
    pytest.param(
        {"merchant_id": "12345678A"},
        EditFailure(MERCHANT_ID_NOT_NUMERIC_MESSAGE, "merchant_id"),
        id="merchant-id-not-numeric",
    ),
    pytest.param({"confirm": "Q"}, EditFailure(CONFIRM_INVALID_MESSAGE, "confirm"), id="confirm-q"),
]

XREF_FAILURE_CASES = [
    pytest.param(
        XrefFailure(
            "GetXrefByAccount",
            ACCOUNT_PATH_FILE,
            {"acct_id": ADDING_ACCT_ID},
            Refusal(HTTP_FILE_ERROR, ACCOUNT_XREF_FAILED_MESSAGE, "acct_id"),
        ),
        id="cxacaix-ioerr",
    ),
    pytest.param(
        XrefFailure(
            "GetXrefByCard",
            CARD_XREF_FILE,
            {"acct_id": "", "card_num": ADDING_CARD_NUM},
            Refusal(HTTP_FILE_ERROR, CARD_XREF_FAILED_MESSAGE, "card_num"),
        ),
        id="ccxref-ioerr",
    ),
]

# The STARTBR/READPREV of HIGH-VALUES behind ENTER's next TRAN-ID and behind the F5 copy.
TRAN_LOOKUP_FAILURE_CASES = [
    pytest.param(
        TranLookupFailure("AllocateNextTranId", NO_OVERRIDES),
        id="allocate-next-tran-id-on-enter",
    ),
    pytest.param(
        TranLookupFailure("GetHighestTransaction", {"action": COPY_LAST_ACTION, "confirm": "Y"}),
        id="highest-transaction-on-copy-last",
    ),
]

# Entries every edit accepts, keyed with a blank CONFIRM so the prompt shows the edited map.
ACCEPTED_ENTRY_CASES = [
    pytest.param(
        {"amount": "+00000050.00"}, ShownField("amount", "+00000050.00"), id="amount-plus"
    ),
    pytest.param(
        {"amount": "-00000050.00"}, ShownField("amount", "-00000050.00"), id="amount-minus"
    ),
    pytest.param(
        {"orig_date": PRE_GREGORIAN_DATE},
        ShownField("orig_date", PRE_GREGORIAN_DATE),
        id="orig-date-ceedays-2513",
    ),
    pytest.param(
        {"proc_date": PRE_GREGORIAN_DATE},
        ShownField("proc_date", PRE_GREGORIAN_DATE),
        id="proc-date-ceedays-2513",
    ),
    # READ-CCXREF-FILE moves XREF-ACCT-ID into ACTIDINI.
    pytest.param(
        {"acct_id": "", "card_num": ADDING_CARD_NUM},
        ShownField("acct_id", ADDING_ACCT_ID),
        id="card-key-fills-account",
    ),
]

NEXT_TRAN_ID_CASES = [
    pytest.param(LAST_TRAN_ID, NEXT_AFTER_LAST_TRAN_ID, id="after-interest-tran-id"),
    pytest.param(MAX_TRAN_ID, WRAPPED_TRAN_ID, id="wraps-past-sixteen-nines"),
]


def BuildAddRequest(overrides: Mapping[str, str] = NO_OVERRIDES) -> TransactionAddRequest:
    """Return ENTER on the COTRN02 map keyed valid for the seed account, with ``overrides``."""
    return TransactionAddRequest(**{"action": ENTER_ACTION, **VALID_SCREEN_FIELDS, **overrides})


def InsertTransactionRow(
    syncEngine: Engine, tranId: str, rowValues: Mapping[str, Any] = LAST_ROW_VALUES
) -> None:
    """Commit a TRANSACT record with key ``tranId`` on the seed account's card."""
    with syncEngine.begin() as connection:
        connection.execute(INSERT_TRANSACTION_SQL, {"tran_id": tranId, **rowValues})


def CountTransactions(syncEngine: Engine) -> int:
    """Return the number of committed TRANSACT records."""
    with syncEngine.connect() as connection:
        return connection.execute(COUNT_TRANSACTIONS_SQL).scalar_one()


def ReadBalance(syncEngine: Engine, acctId: str) -> Decimal:
    """Return the committed ACCT-CURR-BAL of ``acctId``."""
    with syncEngine.connect() as connection:
        return connection.execute(READ_BALANCE_SQL, {"acctId": acctId}).scalar_one()


def ReadStoredRow(syncEngine: Engine, tranId: str) -> dict[str, Any]:
    """Return the committed record of ``tranId``, every column but the key."""
    with syncEngine.connect() as connection:
        return dict(connection.execute(STORED_ROW_SQL, {"tranId": tranId}).mappings().one())


def ReadStoredState(syncEngine: Engine) -> dict[str, list[dict[str, Any]]]:
    """Return every committed TRANSACT, ACCTDAT and TCATBALF row, all columns, keyed by table."""
    with syncEngine.connect() as connection:
        return {
            tableName: [dict(row) for row in connection.execute(statement).mappings()]
            for tableName, statement in STORED_STATE_SQL.items()
        }


def ReadStoredBalances(syncEngine: Engine) -> dict[str, list[dict[str, Any]]]:
    """Return every committed ACCTDAT and TCATBALF row, all columns, keyed by table."""
    storedBalances = ReadStoredState(syncEngine)
    del storedBalances[TRANSACT_TABLE]
    return storedBalances


def ExpectedBody(refusal: Refusal) -> dict[str, object]:
    """Return the error body of a first-failure screen: one failing field, the cursor on it.

    The body is the four standard keys and carries no ``fields``: cotrn02c declares that the
    client keeps the screen as keyed and that only the 200 confirm prompt sends the map back.
    """
    return {
        "message": refusal.message,
        "info_message": "",
        "field_errors": [refusal.cursorField],
        "cursor_field": refusal.cursorField,
    }


def PromptResponse(screenFields: Mapping[str, str]) -> TransactionAddResponse:
    """Return the 200 confirm prompt: its literal, the cursor on CONFIRM and the map sent back."""
    return TransactionAddResponse(
        message=CONFIRM_PROMPT_MESSAGE,
        cursor_field="confirm",
        fields=TransactionAddFields(**screenFields),
    )


def AddedResponse(tranId: str) -> TransactionAddResponse:
    """Return the 200 success screen: INITIALIZE-ALL-FIELDS, the cursor on ACTIDIN."""
    return TransactionAddResponse(
        message=ADD_SUCCESS_TEMPLATE.format(tranId=tranId),
        cursor_field="acct_id",
        fields=TransactionAddFields(),
        tran_id=tranId,
    )


def FailingXrefRead(fileName: str) -> Callable[[CardXrefRepository, str], Awaitable[None]]:
    """Return a CardXrefRepository read method that raises CICS IOERR on ``fileName``."""

    async def RaiseFileIoError(repository: CardXrefRepository, keyValue: str) -> None:
        raise FileIoError(fileName)

    return RaiseFileIoError


async def AssertRefusedWithoutWrite(
    context: Any, syncEngine: Engine, request: TransactionAddRequest, refusal: Refusal
) -> None:
    """Assert AddTransaction answers ``request`` with ``refusal`` and changes no stored row."""
    stateBefore = ReadStoredState(syncEngine)
    with pytest.raises(LegacyMessageError) as raised:
        await cotrn02c.AddTransaction(context, request)
    outcome = (raised.value.httpStatus, raised.value.Body())
    assert outcome == (refusal.httpStatus, ExpectedBody(refusal))
    assert ReadStoredState(syncEngine) == stateBefore


async def RespondWithoutWrite(
    context: Any, syncEngine: Engine, request: TransactionAddRequest
) -> TransactionAddResponse:
    """Return AddTransaction's 200 answer to ``request``, asserting it changed no stored row."""
    stateBefore = ReadStoredState(syncEngine)
    response = await cotrn02c.AddTransaction(context, request)
    assert ReadStoredState(syncEngine) == stateBefore
    return response


@pytest.mark.usefixtures("restoreSeedState")
@pytest.mark.parametrize(
    ("overrides", "refusal"), [*KEY_EDIT_CASES, *REQUIRED_FIELD_CASES, *DATA_EDIT_CASES]
)
async def test_first_failing_edit_is_the_only_message(
    makeOnlineContext, syncEngine, overrides, refusal
):
    """Each edit, in source order, ends validation with its literal, status and cursor."""
    InsertTransactionRow(syncEngine, LAST_TRAN_ID)
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    await AssertRefusedWithoutWrite(context, syncEngine, BuildAddRequest(overrides), refusal)


# F5 on the empty seed TRANSACT: READPREV ENDFILE copies the record area's initial spaces
# and zeros, so TTYPCDI is blank and PROCESS-ENTER-KEY stops at its presence edit. The
# confirm prompt is therefore unreachable on an empty file, as in COPY-LAST-TRAN-DATA.
@pytest.mark.usefixtures("restoreSeedState")
async def test_copy_last_empty_file_stops_at_type_cd(makeOnlineContext, syncEngine):
    """F5 with no TRANSACT record answers the Type CD presence literal and changes no row."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    request = BuildAddRequest({"action": COPY_LAST_ACTION, "confirm": ""})
    refusal = EditFailure(TYPE_CD_EMPTY_MESSAGE, "type_cd")
    await AssertRefusedWithoutWrite(context, syncEngine, request, refusal)


@pytest.mark.usefixtures("restoreSeedState")
@pytest.mark.parametrize("xrefFailure", XREF_FAILURE_CASES)
async def test_xref_file_error_is_the_when_other_literal(
    makeOnlineContext, syncEngine, monkeypatch, xrefFailure
):
    """A CXACAIX or CCXREF read failing with IOERR gives its WHEN OTHER literal as a 503."""
    InsertTransactionRow(syncEngine, LAST_TRAN_ID)
    failingRead = FailingXrefRead(xrefFailure.fileName)
    monkeypatch.setattr(CardXrefRepository, xrefFailure.methodName, failingRead)
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    request = BuildAddRequest(xrefFailure.overrides)
    await AssertRefusedWithoutWrite(context, syncEngine, request, xrefFailure.refusal)


@pytest.mark.usefixtures("restoreSeedState")
@pytest.mark.parametrize("tranLookupFailure", TRAN_LOOKUP_FAILURE_CASES)
async def test_transact_browse_error_is_unable_to_lookup_transaction(
    makeOnlineContext, syncEngine, monkeypatch, tranLookupFailure
):
    """A TRANSACT browse failing with IOERR gives the 503 WHEN OTHER literal and no INSERT.

    A record is on file, so the F5 case cannot pass through the READPREV ENDFILE outcome.
    """
    InsertTransactionRow(syncEngine, LAST_TRAN_ID)
    failingLookup = AsyncMock(side_effect=FileIoError(TRANSACTION_FILE))
    insertSpy = AsyncMock()
    monkeypatch.setattr(TransactionRepository, tranLookupFailure.methodName, failingLookup)
    monkeypatch.setattr(TransactionRepository, "InsertTransaction", insertSpy)
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    request = BuildAddRequest(tranLookupFailure.overrides)
    refusal = Refusal(HTTP_FILE_ERROR, TRAN_LOOKUP_FAILED_MESSAGE, "acct_id")
    await AssertRefusedWithoutWrite(context, syncEngine, request, refusal)
    failingLookup.assert_awaited_once()
    insertSpy.assert_not_awaited()
    assert ReadBalance(syncEngine, ADDING_ACCT_ID) == SEED_BALANCE


@pytest.mark.usefixtures("restoreSeedState")
@pytest.mark.parametrize("keyedRefusal", COPY_LAST_KEY_EDIT_CASES)
async def test_copy_last_edits_the_keys_before_reading_transact(
    makeOnlineContext, syncEngine, monkeypatch, keyedRefusal
):
    """F5 with an invalid key answers the key literal and never starts the TRANSACT browse.

    A record is on file and the browse fails with IOERR, so a browse run ahead of
    VALIDATE-INPUT-KEY-FIELDS would answer 503 ``Unable to lookup Transaction...`` instead.
    """
    InsertTransactionRow(syncEngine, LAST_TRAN_ID)
    failingBrowse = AsyncMock(side_effect=FileIoError(TRANSACTION_FILE))
    monkeypatch.setattr(TransactionRepository, "GetHighestTransaction", failingBrowse)
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    request = BuildAddRequest(keyedRefusal.overrides)
    await AssertRefusedWithoutWrite(context, syncEngine, request, keyedRefusal.refusal)
    failingBrowse.assert_not_awaited()


def test_pre_gregorian_date_is_ceedays_message_2513():
    """CSUTLDTC fails a date before 1582-10-15 with message 2513, the one COTRN02C accepts."""
    result = CheckDate(PRE_GREGORIAN_DATE, CSUTLDTC_DATE_MASK)
    assert result.severity != CEEDAYS_VALID_SEVERITY
    assert result.messageNumber == CEEDAYS_MSG_UNSUPPORTED_RANGE


@pytest.mark.usefixtures("restoreSeedState")
@pytest.mark.parametrize(("overrides", "shownField"), ACCEPTED_ENTRY_CASES)
async def test_accepted_entry_reaches_the_confirm_prompt(
    makeOnlineContext, syncEngine, overrides, shownField
):
    """Every edit passes, so a blank CONFIRM returns the prompt with the edited map."""
    InsertTransactionRow(syncEngine, LAST_TRAN_ID)
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    request = BuildAddRequest({**overrides, "confirm": ""})
    response = await RespondWithoutWrite(context, syncEngine, request)
    outcome = (response.message, response.cursor_field, response.tran_id)
    assert outcome == (CONFIRM_PROMPT_MESSAGE, "confirm", None)
    assert getattr(response.fields, shownField.fieldName) == shownField.value
    assert CountTransactions(syncEngine) == 1
    assert ReadStoredRow(syncEngine, LAST_TRAN_ID) == dict(LAST_ROW_VALUES)


@pytest.mark.usefixtures("restoreSeedState")
@pytest.mark.parametrize("confirm", ["", "N", "n"], ids=["blank", "N", "n"])
async def test_blank_or_n_confirm_prompts_without_writing(makeOnlineContext, syncEngine, confirm):
    """CONFIRMI N, n or SPACES sends the prompt with the map, the card masked; nothing is added."""
    InsertTransactionRow(syncEngine, LAST_TRAN_ID)
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    request = BuildAddRequest({"confirm": confirm})
    response = await RespondWithoutWrite(context, syncEngine, request)
    promptFields = {**VALID_SCREEN_FIELDS, "card_num": MASKED_ADDING_CARD_NUM, "confirm": confirm}
    assert response == PromptResponse(promptFields)
    assert CountTransactions(syncEngine) == 1
    assert ReadStoredRow(syncEngine, LAST_TRAN_ID) == dict(LAST_ROW_VALUES)


@pytest.mark.usefixtures("restoreSeedState")
@pytest.mark.parametrize("confirm", ["Y", "y"])
async def test_confirmed_add_on_an_empty_file_writes_the_first_tran_id(
    makeOnlineContext, syncEngine, confirm
):
    """The first TRAN-ID is 0000000000000001; dates are stored as format T; no balance moves."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    response = await cotrn02c.AddTransaction(context, BuildAddRequest({"confirm": confirm}))
    assert response == AddedResponse(FIRST_TRAN_ID)
    assert ReadStoredRow(syncEngine, FIRST_TRAN_ID) == dict(ADDED_ROW)
    assert CountTransactions(syncEngine) == 1
    assert ReadBalance(syncEngine, ADDING_ACCT_ID) == SEED_BALANCE


@pytest.mark.usefixtures("restoreSeedState")
@pytest.mark.parametrize(("highestTranId", "nextTranId"), NEXT_TRAN_ID_CASES)
async def test_next_tran_id_follows_the_highest_key(
    makeOnlineContext, syncEngine, highestTranId, nextTranId
):
    """The new key is the highest TRAN-ID plus 1, kept to sixteen digits."""
    InsertTransactionRow(syncEngine, highestTranId)
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    response = await cotrn02c.AddTransaction(context, BuildAddRequest())
    assert response == AddedResponse(nextTranId)
    assert ReadStoredRow(syncEngine, nextTranId) == dict(ADDED_ROW)
    assert CountTransactions(syncEngine) == 2


@pytest.mark.usefixtures("restoreSeedState")
async def test_wrapped_tran_id_that_exists_is_duplicate(makeOnlineContext, syncEngine):
    """The ID after 9999999999999999 wraps to an existing key: DUPREC, 409, nothing added."""
    InsertTransactionRow(syncEngine, MAX_TRAN_ID)
    InsertTransactionRow(syncEngine, WRAPPED_TRAN_ID)
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    refusal = Refusal(HTTP_DUPLICATE_KEY, DUPLICATE_TRAN_ID_MESSAGE, "acct_id")
    await AssertRefusedWithoutWrite(context, syncEngine, BuildAddRequest(), refusal)
    assert CountTransactions(syncEngine) == 2


@pytest.mark.usefixtures("restoreSeedState")
async def test_copy_last_fills_the_screen_from_the_highest_record(makeOnlineContext, syncEngine):
    """F5 copies the highest TRAN-ID's fields over the keyed ones, then prompts for CONFIRM."""
    InsertTransactionRow(syncEngine, FIRST_TRAN_ID, ADDED_ROW)
    InsertTransactionRow(syncEngine, LAST_TRAN_ID)
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    request = BuildAddRequest({"action": COPY_LAST_ACTION, "confirm": ""})
    response = await RespondWithoutWrite(context, syncEngine, request)
    assert response == PromptResponse(COPIED_SCREEN_FIELDS)
    assert CountTransactions(syncEngine) == 2


@pytest.mark.usefixtures("restoreSeedState")
async def test_copy_last_confirmed_adds_the_copied_record(makeOnlineContext, syncEngine):
    """F5 with CONFIRM Y writes the copied fields under the next TRAN-ID.

    The copied record and every ACCTDAT and TCATBALF row are left exactly as they were.
    """
    InsertTransactionRow(syncEngine, LAST_TRAN_ID)
    balancesBefore = ReadStoredBalances(syncEngine)
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    request = BuildAddRequest({"action": COPY_LAST_ACTION, "confirm": "Y"})
    response = await cotrn02c.AddTransaction(context, request)
    assert response == AddedResponse(NEXT_AFTER_LAST_TRAN_ID)
    assert ReadStoredRow(syncEngine, NEXT_AFTER_LAST_TRAN_ID) == dict(COPIED_ADDED_ROW)
    assert ReadBalance(syncEngine, ADDING_ACCT_ID) == SEED_BALANCE
    assert ReadStoredRow(syncEngine, LAST_TRAN_ID) == dict(LAST_ROW_VALUES)
    assert CountTransactions(syncEngine) == 2
    assert ReadStoredBalances(syncEngine) == balancesBefore
