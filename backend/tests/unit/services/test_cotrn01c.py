"""Unit tests for COTRN01C (CT01), the Transaction View screen of map COTRN01.

cbl/COTRN01C.cbl PROCESS-ENTER-KEY refuses a blank TRNIDIN with ``Tran ID can NOT be empty...``
(line 149). READ-TRANSACT-FILE answers NOTFND with ``Transaction ID NOT found...`` (line 285) and
any other RESP with ``Unable to lookup Transaction...`` (line 292), the batch window's NOTOPEN
included. Every outcome moves -1 to TRNIDINL, so the cursor is on the transaction ID. A found
record is moved field by field to the map (lines 176-190). Expected values are typed from those
lines and from the 26-character TRAN-ORIG-TS and TRAN-PROC-TS text forms (copybook CVTRA05Y):
- a blank ID, empty or 16 spaces, answers 400;
- an ID no record has answers 404;
- an IOERR or a NOTOPEN from the TRANSACT read answers 503;
- a record of each timestamp form (S, D, T and blank) is shown with its own values, the amount a
    Decimal, the card number masked and both timestamps as their exact 26-character texts;
- the source's READ ... UPDATE is a plain SELECT, so no case changes a stored record.

Each case calls cotrn01c.ViewTransaction directly against the migrated, seeded test database,
with four records committed on a seed card; the seed is restored afterwards.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from operator import itemgetter
from typing import NamedTuple
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Engine, text

from app.core.errors import FileIoError, LegacyMessageError
from app.core.locks import FileNotOpenError
from app.repositories.transaction_repository import TransactionRepository
from app.services.transactions import cotrn01c

REGULAR_USER_TYPE = "U"
# WS-TRANSACT-FILE (line 39): the CICS file READ-TRANSACT-FILE reads.
TRANSACT_FILE = "TRANSACT"

# The PROCESS-ENTER-KEY and READ-TRANSACT-FILE literals (lines 149, 285 and 292).
TRAN_ID_EMPTY_MESSAGE = "Tran ID can NOT be empty..."
TRAN_ID_NOT_FOUND_MESSAGE = "Transaction ID NOT found..."
TRAN_LOOKUP_FAILED_MESSAGE = "Unable to lookup Transaction..."
# MOVE -1 TO TRNIDINL on every path: the cursor is on the entered transaction ID.
TRAN_ID_CURSOR_FIELD = "tran_id"
EDIT_FAILURE_STATUS = 400
NOT_FOUND_STATUS = 404
FILE_ERROR_STATUS = 503

# TRNIDIN as a field the user leaves empty or fills with spaces.
BLANK_TRAN_IDS = ["", " " * 16]
# No record has this TRAN-ID.
UNKNOWN_TRAN_ID = "9999999999999998"

# The seed card of account 00000000004, masked as twelve asterisks plus its last four digits.
VIEW_CARD_NUM = "2988091353094312"
MASKED_CARD_NUM = "*" * 12 + "4312"
# Any 16-digit run in the response JSON would be a card number shown in clear.
SIXTEEN_DIGIT_RUN = re.compile(r"[0-9]{16}")
TIMESTAMP_TEXT_LENGTH = 26

INSERT_TRANSACTION_SQL = text(
    "INSERT INTO transactions.transactions (tran_id, tran_type_cd, tran_cat_cd, tran_source, "
    "tran_desc, tran_amt, merchant_id, merchant_name, merchant_city, merchant_zip, card_num, "
    "orig_ts, orig_ts_format, proc_ts, proc_ts_format) VALUES (:tran_id, :tran_type_cd, "
    ":tran_cat_cd, :tran_source, :tran_desc, :tran_amt, :merchant_id, :merchant_name, "
    ":merchant_city, :merchant_zip, :card_num, :orig_ts, :orig_ts_format, :proc_ts, "
    ":proc_ts_format)"
)
STORED_TRANSACTIONS_SQL = text(
    "SELECT tran_id, tran_type_cd, tran_cat_cd, tran_source, tran_desc, tran_amt, merchant_id, "
    "merchant_name, merchant_city, merchant_zip, card_num, orig_ts, orig_ts_format, proc_ts, "
    "proc_ts_format FROM transactions.transactions ORDER BY tran_id"
)


class ViewableRow(NamedTuple):
    """A committed TRANSACT record and the TORIGDT and TPROCDT texts its record held."""

    values: Mapping[str, object]
    origTsText: str
    procTsText: str


# One record per TRAN-ORIG-TS/TRAN-PROC-TS text form, keyed by its format code. TRAN-ID is
# X(16) and COTRN01C has no numeric edit, so letters in the IDs leave the card number as the
# only value a 16-digit run could come from. Each text value fits its COTRN01 map field.
VIEWABLE_ROWS: Mapping[str, ViewableRow] = {
    # The space-separated form S, as DALYTRAN ORIG-TS and the CB00 clock write it.
    "S": ViewableRow(
        values={
            "tran_id": "CT01VIEWFORMAT0S",
            "tran_type_cd": "01",
            "tran_cat_cd": 1,
            "tran_source": "POS TERM",
            "tran_desc": "PYTEST GROCERY PURCHASE",
            "tran_amt": Decimal("123.45"),
            "merchant_id": "000000101",
            "merchant_name": "PYTEST GROCERY",
            "merchant_city": "SPRINGFIELD",
            "merchant_zip": "62701",
            "card_num": VIEW_CARD_NUM,
            "orig_ts": datetime(2022, 6, 10, 19, 27, 53),
            "orig_ts_format": "S",
            "proc_ts": datetime(2022, 6, 10, 19, 27, 53),
            "proc_ts_format": "S",
        },
        origTsText="2022-06-10 19:27:53.000000",
        procTsText="2022-06-10 19:27:53.000000",
    ),
    # The DB2 form D of Z-GET-DB2-FORMAT-TIMESTAMP, as an INTCALC interest record holds it.
    "D": ViewableRow(
        values={
            "tran_id": "CT01VIEWFORMAT0D",
            "tran_type_cd": "01",
            "tran_cat_cd": 5,
            "tran_source": "System",
            "tran_desc": "Int. for a/c 00000000004",
            "tran_amt": Decimal("1.53"),
            "merchant_id": "000000000",
            "merchant_name": "",
            "merchant_city": "",
            "merchant_zip": "",
            "card_num": VIEW_CARD_NUM,
            "orig_ts": datetime(2022, 7, 18, 0, 0, 0),
            "orig_ts_format": "D",
            "proc_ts": datetime(2022, 7, 18, 0, 0, 0),
            "proc_ts_format": "D",
        },
        origTsText="2022-07-18-00.00.00.000000",
        procTsText="2022-07-18-00.00.00.000000",
    ),
    # The date-only form T that CT02 writes, stored as midnight; the text is 16 spaces padded.
    "T": ViewableRow(
        values={
            "tran_id": "CT01VIEWFORMAT0T",
            "tran_type_cd": "03",
            "tran_cat_cd": 1,
            "tran_source": "OPERATOR",
            "tran_desc": "PYTEST RETURN CREDIT",
            "tran_amt": Decimal("-17.25"),
            "merchant_id": "000000202",
            "merchant_name": "PYTEST OUTLET",
            "merchant_city": "SHELBYVILLE",
            "merchant_zip": "62565",
            "card_num": VIEW_CARD_NUM,
            "orig_ts": datetime(2022, 7, 17, 0, 0, 0),
            "orig_ts_format": "T",
            "proc_ts": datetime(2022, 7, 17, 0, 0, 0),
            "proc_ts_format": "T",
        },
        origTsText="2022-07-17                ",
        procTsText="2022-07-17                ",
    ),
    # A blank TRAN-PROC-TS, as every DALYTRAN record carries: NULL with format code blank.
    " ": ViewableRow(
        values={
            "tran_id": "CT01VIEWFORMAT0B",
            "tran_type_cd": "01",
            "tran_cat_cd": 1,
            "tran_source": "POS TERM",
            "tran_desc": "PYTEST UNPROCESSED PURCHASE",
            "tran_amt": Decimal("50.00"),
            "merchant_id": "000000303",
            "merchant_name": "PYTEST CAFE",
            "merchant_city": "CAPITAL CITY",
            "merchant_zip": "62702",
            "card_num": VIEW_CARD_NUM,
            "orig_ts": datetime(2022, 6, 10, 19, 27, 53),
            "orig_ts_format": "S",
            "proc_ts": None,
            "proc_ts_format": " ",
        },
        origTsText="2022-06-10 19:27:53.000000",
        procTsText="                          ",
    ),
}
TIMESTAMP_FORMAT_IDS = ["S", "D", "T", "blank"]
# A record that exists, read in the TRANSACT file-condition cases.
EXISTING_ROW = VIEWABLE_ROWS["S"]


@dataclass(frozen=True)
class InsertedTransactions:
    """The committed CT01 records, by format code, and the engine that reads them back."""

    rows: Mapping[str, ViewableRow]
    syncEngine: Engine

    def InsertedValues(self) -> list[dict[str, object]]:
        """Return the values each record was inserted with, in TRAN-ID order."""
        insertedValues = [dict(row.values) for row in self.rows.values()]
        return sorted(insertedValues, key=itemgetter("tran_id"))

    def StoredValues(self) -> list[dict[str, object]]:
        """Return every committed TRANSACT record, in TRAN-ID order."""
        with self.syncEngine.connect() as connection:
            storedRows = connection.execute(STORED_TRANSACTIONS_SQL).mappings()
            return [dict(storedRow) for storedRow in storedRows]


@pytest.fixture(name="viewableTransactions")
def ViewableTransactions(restoreSeedState: None, syncEngine: Engine) -> InsertedTransactions:
    """Commit VIEWABLE_ROWS on the seed card; restoreSeedState removes them at teardown."""
    with syncEngine.begin() as connection:
        connection.execute(
            INSERT_TRANSACTION_SQL, [dict(row.values) for row in VIEWABLE_ROWS.values()]
        )
    return InsertedTransactions(rows=VIEWABLE_ROWS, syncEngine=syncEngine)


def RefusalBody(message: str) -> dict[str, object]:
    """Return the four standard keys of a COTRN01C refusal, the cursor on the transaction ID."""
    return {
        "message": message,
        "info_message": "",
        "field_errors": [TRAN_ID_CURSOR_FIELD],
        "cursor_field": TRAN_ID_CURSOR_FIELD,
    }


def ExpectedDetail(row: ViewableRow) -> dict[str, object]:
    """Return the COTRN01 map fields of ``row``: its values, the card masked, no message."""
    values = row.values
    return {
        "tran_id": values["tran_id"],
        "card_num_masked": MASKED_CARD_NUM,
        "tran_type_cd": values["tran_type_cd"],
        "tran_cat_cd": values["tran_cat_cd"],
        "tran_source": values["tran_source"],
        "tran_desc": values["tran_desc"],
        "tran_amt": values["tran_amt"],
        "orig_ts": row.origTsText,
        "proc_ts": row.procTsText,
        "merchant_id": values["merchant_id"],
        "merchant_name": values["merchant_name"],
        "merchant_city": values["merchant_city"],
        "merchant_zip": values["merchant_zip"],
        "message": "",
    }


@pytest.mark.parametrize("blankTranId", BLANK_TRAN_IDS, ids=["empty", "sixteen-spaces"])
async def test_blank_tran_id_is_refused_with_the_empty_literal(
    makeOnlineContext, viewableTransactions, blankTranId
):
    """A blank TRNIDIN answers 400 ``Tran ID can NOT be empty...`` and changes no record."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)

    with pytest.raises(LegacyMessageError) as excInfo:
        await cotrn01c.ViewTransaction(context, blankTranId)

    assert excInfo.value.httpStatus == EDIT_FAILURE_STATUS
    assert excInfo.value.Body() == RefusalBody(TRAN_ID_EMPTY_MESSAGE)
    assert viewableTransactions.StoredValues() == viewableTransactions.InsertedValues()


async def test_unknown_tran_id_is_refused_with_the_not_found_literal(
    makeOnlineContext, viewableTransactions
):
    """A TRAN-ID no record has is NOTFND: 404 ``Transaction ID NOT found...``, no record changed."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)

    with pytest.raises(LegacyMessageError) as excInfo:
        await cotrn01c.ViewTransaction(context, UNKNOWN_TRAN_ID)

    assert excInfo.value.httpStatus == NOT_FOUND_STATUS
    assert excInfo.value.Body() == RefusalBody(TRAN_ID_NOT_FOUND_MESSAGE)
    assert viewableTransactions.StoredValues() == viewableTransactions.InsertedValues()


@pytest.mark.parametrize("conditionType", [FileIoError, FileNotOpenError], ids=["ioerr", "notopen"])
async def test_transact_file_condition_is_refused_with_the_lookup_literal(
    makeOnlineContext, viewableTransactions, monkeypatch, conditionType
):
    """IOERR, or the batch window's NOTOPEN, on the TRANSACT read is the WHEN OTHER 503."""
    getTransaction = AsyncMock(side_effect=conditionType(TRANSACT_FILE))
    monkeypatch.setattr(TransactionRepository, "GetTransaction", getTransaction)
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    existingTranId = EXISTING_ROW.values["tran_id"]

    with pytest.raises(LegacyMessageError) as excInfo:
        await cotrn01c.ViewTransaction(context, existingTranId)

    assert excInfo.value.httpStatus == FILE_ERROR_STATUS
    assert excInfo.value.Body() == RefusalBody(TRAN_LOOKUP_FAILED_MESSAGE)
    getTransaction.assert_awaited_once_with(existingTranId)
    assert viewableTransactions.StoredValues() == viewableTransactions.InsertedValues()


@pytest.mark.parametrize("timestampFormat", list(VIEWABLE_ROWS), ids=TIMESTAMP_FORMAT_IDS)
async def test_record_is_shown_with_its_values_masked_card_and_timestamp_texts(
    makeOnlineContext, viewableTransactions, timestampFormat
):
    """Each record's map fields are its values, with the exact 26-character timestamp texts."""
    viewedRow = viewableTransactions.rows[timestampFormat]
    context = await makeOnlineContext(REGULAR_USER_TYPE)

    detail = await cotrn01c.ViewTransaction(context, viewedRow.values["tran_id"])

    assert detail.model_dump() == ExpectedDetail(viewedRow)
    assert isinstance(detail.tran_amt, Decimal)
    assert len(detail.orig_ts) == len(detail.proc_ts) == TIMESTAMP_TEXT_LENGTH
    assert SIXTEEN_DIGIT_RUN.search(detail.model_dump_json()) is None
    assert viewableTransactions.StoredValues() == viewableTransactions.InsertedValues()
