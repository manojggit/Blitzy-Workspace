"""Unit tests for the COTRN02C (CT02) WRITE WHEN OTHER outcome of a date no timestamp holds.

cbl/COTRN02C.cbl lets a date before 1582-10-15 through CSUTLDTC, whose CEEDAYS message 2513
passes the edit (lines 389-427), so year 0000 reaches ADD-TRANSACTION, which moves the date
into TRAN-ORIG-TS or TRAN-PROC-TS (lines 464-465). No TIMESTAMP(6) value holds year 0000, so
the record cannot be written: WRITE-TRANSACT-FILE takes its WHEN OTHER branch (lines 742-748),
whose DISPLAY of RESP and REAS (line 743) every WHEN OTHER branch logs through
LogFileCondition. Expected values are typed from those lines and from the seed, where account
00000000004 has a card and no transaction exists. For a year-0000 orig date and for a year-0000
proc date:
- 503 ``Unable to Add Transaction...`` with the cursor on ACTIDIN, in the four standard keys;
- exactly one ERROR record, ``Transaction CT02 WRITE on TRANSACT returned RESP:17 REAS:0
    (cause ValueError, SQLSTATE none)``: IOERR on TRANSACT, whose innermost cause is the
    year-out-of-range ValueError beneath the TimestampFormatError, with no traceback, date or
    card number;
- no transaction stored.

Each case adds through cotrn02c.AddTransaction against the migrated, seeded test database, and
the seed is restored afterwards.
"""

import logging

import pytest
from sqlalchemy import Engine, text

from app.core.errors import LegacyMessageError
from app.schemas.transactions import TransactionAddRequest
from app.services.transactions import cotrn02c

REGULAR_USER_TYPE = "U"
# The seed account entered as the key; every other field passes the CT02 edits as keyed.
ADDING_ACCT_ID = "00000000004"
VALID_DATE = "2022-07-17"
# Passes the format edit and CSUTLDTC (CEEDAYS message 2513); no TIMESTAMP(6) value holds it.
YEAR_ZERO_DATE = "0000-01-01"
CONFIRMED_ADD_FIELDS = {
    "action": "enter",
    "acct_id": ADDING_ACCT_ID,
    "type_cd": "01",
    "cat_cd": "0001",
    "source": "POS TERM",
    "desc": "PYTEST PURCHASE",
    "amount": "+00000050.00",
    "orig_date": VALID_DATE,
    "proc_date": VALID_DATE,
    "merchant_id": "000000001",
    "merchant_name": "PYTEST MERCHANT",
    "merchant_city": "PYTEST CITY",
    "merchant_zip": "00000",
    "confirm": "Y",
}

# The WHEN OTHER literal (lines 745-746) and MOVE -1 TO ACTIDINL (line 747).
ADD_FAILED_BODY = {
    "message": "Unable to Add Transaction...",
    "info_message": "",
    "field_errors": ["acct_id"],
    "cursor_field": "acct_id",
}
FILE_ERROR_STATUS = 503
WRITE_CONDITION_LOG_MESSAGE = (
    "Transaction CT02 WRITE on TRANSACT returned RESP:17 REAS:0 (cause ValueError, SQLSTATE none)"
)

XREF_CARD_QUERY = text("SELECT card_num FROM xref.card_xref WHERE acct_id = :acctId")
STORED_TRAN_IDS_QUERY = text("SELECT tran_id FROM transactions.transactions ORDER BY tran_id")


def AccountCardNumber(syncEngine: Engine) -> str:
    """Return the card number the adding account's xref record holds."""
    with syncEngine.connect() as connection:
        return connection.execute(XREF_CARD_QUERY, {"acctId": ADDING_ACCT_ID}).scalar_one()


def StoredTranIds(syncEngine: Engine) -> list[str]:
    """Return every committed TRAN-ID, in TRAN-ID order."""
    with syncEngine.connect() as connection:
        return list(connection.execute(STORED_TRAN_IDS_QUERY).scalars())


@pytest.mark.parametrize(
    "yearZeroField", ["orig_date", "proc_date"], ids=["orig-date-year-0000", "proc-date-year-0000"]
)
async def test_year_zero_date_logs_the_write_condition_and_stores_nothing(
    makeOnlineContext, syncEngine, caplog, yearZeroField
):
    """The WRITE's 503 literal is answered, its file condition logged once, and nothing stored."""
    caplog.set_level(logging.DEBUG, logger=cotrn02c.LOGGER.name)
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    request = TransactionAddRequest(**{**CONFIRMED_ADD_FIELDS, yearZeroField: YEAR_ZERO_DATE})

    with pytest.raises(LegacyMessageError) as raised:
        await cotrn02c.AddTransaction(context, request)

    assert (raised.value.httpStatus, raised.value.Body()) == (FILE_ERROR_STATUS, ADD_FAILED_BODY)
    loggedFailures = [
        (logRecord.name, logRecord.levelno, logRecord.getMessage())
        for logRecord in caplog.records
        if logRecord.levelno >= logging.WARNING
    ]
    assert loggedFailures == [(cotrn02c.LOGGER.name, logging.ERROR, WRITE_CONDITION_LOG_MESSAGE)]
    assert all(logRecord.exc_info is None for logRecord in caplog.records)
    assert YEAR_ZERO_DATE not in caplog.text
    assert AccountCardNumber(syncEngine) not in caplog.text
    assert StoredTranIds(syncEngine) == []
