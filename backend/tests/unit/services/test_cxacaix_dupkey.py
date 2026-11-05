"""Unit tests for a CXACAIX read that returns DUPKEY, and the four programs that read the path.

CXACAIX is the non-unique alternate index of CARDXREF on XREF-ACCT-ID (jcl/XREFFILE.jcl
KEYS(11,25)). When a second cross-reference has the account ID, CICS reads the first record in
AIX order, the account's lowest XREF-CARD-NUM in EBCDIC order, into the INTO area and returns
DUPKEY (RESP 15, RESP2 140). Each program tests only NORMAL and NOTFND, so DUPKEY is WHEN OTHER:

- cbl/COBIL00C.cbl READ-CXACAIX-FILE (lines 408-436) sets ``Unable to lookup XREF AIX file...``
    and the IF CONF-PAY-YES branch goes on (lines 210-235). MOVE XREF-CARD-NUM TO TRAN-CARD-NUM
    carries the first record's card, the WRITE succeeds and its STRING (lines 527-530) is the
    last literal, and the COMPUTE and the REWRITE pay the whole balance. The DISPLAY of RESP and
    REAS is the file-condition log line;
- cbl/COACTVWC.cbl and cbl/COACTUPC.cbl 9200-GETCARDXREF-BYACCT (lines 759-768, 3686-3695)
    answer WS-FILE-ERROR-MESSAGE for READ on CXACAIX with RESP 15 and RESP2 140;
- cbl/COTRN02C.cbl READ-CXACAIX-FILE (lines 597-603) answers
    ``Unable to lookup Acct in XREF AIX file...``.

Seed account 00000000035 holds 166.00 and one card, 1561409106491600 (data/EBCDIC
AWS.M2.CARDDEMO.CARDXREF.PS), and TRANSACT is empty. Each test commits a second card and its
cross-reference for that account. The second card sorts after the seed card, before it in both
orders, or before it in EBCDIC order only: ``A`` is X'C1' and ``1`` is X'F1', while code-point
order puts digits first. The seed is restored after each test.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import date
from decimal import Decimal
from typing import NamedTuple

import pytest
from sqlalchemy import Engine, insert, text

from app.core.dependencies import OnlineContext
from app.core.errors import LegacyMessageError
from app.models import Card, CardXref
from app.repositories.card_xref_repository import CardXrefRepository, DuplicateAlternateKeyError
from app.schemas.billpay import BillPayRequest
from app.schemas.transactions import TransactionAddRequest
from app.services.accounts import coactupc, coactvwc
from app.services.billpay import cobil00c
from app.services.transactions import cotrn02c

# The seed account and its only cross-reference before each test adds a second one.
DUPLICATED_ACCT_ID = "00000000035"
SEED_CUST_ID = "000000035"
SEED_CARD_NUM = "1561409106491600"
SEED_BALANCE = Decimal("166.00")
PAID_BALANCE = Decimal("0.00")
# The CVACT02Y fields of the second card other than its key and account.
SECOND_CARD_FIELDS = {
    "cvv_cd": "123",
    "embossed_name": "SECOND CARD",
    "expiration_date": date(2030, 1, 1),
    "active_status": "Y",
}

# DUPKEY on the CXACAIX path; the exception text names only the file and the codes.
XREF_ACCOUNT_PATH_NAME = "CXACAIX"
DUPKEY_RESP = 15
DUPKEY_RESP2 = 140
DUPKEY_ERROR_TEXT = "CXACAIX RESP 15 RESP2 140"

# CB00: confirm Y, the first TRAN-ID of an empty TRANSACT, the STRING of lines 527-530 with
# its two spaces, and the DISPLAY of WHEN OTHER as the file-condition log line.
CONFIRM_YES = "Y"
FIRST_TRAN_ID = "0000000000000001"
FIRST_PAYMENT_MESSAGE = "Payment successful.  Your Transaction ID is 0000000000000001."
DUPKEY_LOG_MESSAGE = (
    "Transaction CB00 READ on CXACAIX returned RESP:15 REAS:140 (cause none, SQLSTATE none)"
)

# COACTVWC/COACTUPC WS-FILE-ERROR-MESSAGE: 'READ' X(8), CXACAIX X(9), RESP and RESP2 as X(10).
CXACAIX_FILE_ERROR_MESSAGE = (
    "File Error: READ     on CXACAIX   returned RESP 000000015 ,RESP2 000000140"
)
# The COTRN02C READ-CXACAIX-FILE WHEN OTHER literal.
CT02_XREF_LOOKUP_FAILED_MESSAGE = "Unable to lookup Acct in XREF AIX file..."
FILE_ERROR_STATUS = 503

BALANCE_QUERY = text("SELECT curr_balance FROM accounts.accounts WHERE acct_id = :acctId")
STORED_PAYMENTS_QUERY = text(
    "SELECT tran_id, card_num, tran_amt FROM transactions.transactions ORDER BY tran_id"
)


class SecondCardCase(NamedTuple):
    """The card number of the second cross-reference, and the first card in AIX order."""

    insertedCardNum: str
    firstCardNum: str


SECOND_CARD_CASES = [
    SecondCardCase("9999000000000035", SEED_CARD_NUM),
    SecondCardCase("0000000000000035", "0000000000000035"),
    SecondCardCase("A000000000000035", "A000000000000035"),
]
SECOND_CARD_CASE_IDS = [
    "inserted-card-sorts-after-the-seed-card",
    "inserted-card-sorts-before-the-seed-card",
    "letter-sorts-before-digits-in-ebcdic-only",
]


class WhenOtherCase(NamedTuple):
    """A program's CXACAIX read by the duplicated account, and its WHEN OTHER literal."""

    readByAccount: Callable[[OnlineContext], Awaitable[object]]
    expectedMessage: str


async def ViewAccount(context: OnlineContext) -> object:
    """Press ENTER on COACTVW (CAVW) with the duplicated account."""
    return await coactvwc.ViewAccount(context, DUPLICATED_ACCT_ID)


async def FetchAccountForUpdate(context: OnlineContext) -> object:
    """Fetch the duplicated account's details on COACTUP (CAUP)."""
    return await coactupc.FetchDetails(context, DUPLICATED_ACCT_ID)


async def AddTransactionByAccount(context: OnlineContext) -> object:
    """Press ENTER on COTRN02 (CT02) with only the duplicated account keyed."""
    return await cotrn02c.AddTransaction(context, TransactionAddRequest(acct_id=DUPLICATED_ACCT_ID))


WHEN_OTHER_CASES = [
    WhenOtherCase(ViewAccount, CXACAIX_FILE_ERROR_MESSAGE),
    WhenOtherCase(FetchAccountForUpdate, CXACAIX_FILE_ERROR_MESSAGE),
    WhenOtherCase(AddTransactionByAccount, CT02_XREF_LOOKUP_FAILED_MESSAGE),
]
WHEN_OTHER_CASE_IDS = ["cavw-view", "caup-fetch", "ct02-account-key"]


def CommitSecondCard(syncEngine: Engine, cardNum: str) -> None:
    """Commit a second card of the duplicated account and its cross-reference record."""
    with syncEngine.begin() as connection:
        connection.execute(
            insert(Card).values(card_num=cardNum, acct_id=DUPLICATED_ACCT_ID, **SECOND_CARD_FIELDS)
        )
        connection.execute(
            insert(CardXref).values(
                card_num=cardNum, cust_id=SEED_CUST_ID, acct_id=DUPLICATED_ACCT_ID
            )
        )


def XrefRecord(cardNum: str) -> dict[str, str]:
    """Return the duplicated account's cross-reference record for ``cardNum``."""
    return {"card_num": cardNum, "cust_id": SEED_CUST_ID, "acct_id": DUPLICATED_ACCT_ID}


def PersistedBalance(syncEngine: Engine) -> Decimal:
    """Return the duplicated account's committed ACCT-CURR-BAL."""
    with syncEngine.connect() as connection:
        return connection.execute(BALANCE_QUERY, {"acctId": DUPLICATED_ACCT_ID}).scalar_one()


def StoredPayments(syncEngine: Engine) -> list[tuple[str, str, Decimal]]:
    """Return every committed transaction's TRAN-ID, TRAN-CARD-NUM and TRAN-AMT."""
    with syncEngine.connect() as connection:
        return [tuple(row) for row in connection.execute(STORED_PAYMENTS_QUERY)]


def BillPayLogRecords(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    """Return the level and text of every record logged on the COBIL00C logger."""
    return [
        (logRecord.levelno, logRecord.getMessage())
        for logRecord in caplog.records
        if logRecord.name == cobil00c.LOGGER.name
    ]


@pytest.mark.parametrize("secondCard", SECOND_CARD_CASES, ids=SECOND_CARD_CASE_IDS)
async def test_account_path_read_raises_dupkey_with_the_first_record_in_aix_order(
    makeOnlineContext, syncEngine, secondCard
):
    """DUPKEY carries the lowest XREF-CARD-NUM in EBCDIC order, whichever row came first."""
    CommitSecondCard(syncEngine, secondCard.insertedCardNum)
    context = await makeOnlineContext()
    with pytest.raises(DuplicateAlternateKeyError) as raised:
        await CardXrefRepository(context).GetXrefByAccount(DUPLICATED_ACCT_ID)
    error = raised.value
    assert (error.fileName, error.resp, error.resp2) == (
        XREF_ACCOUNT_PATH_NAME,
        DUPKEY_RESP,
        DUPKEY_RESP2,
    )
    assert error.firstRecord == XrefRecord(secondCard.firstCardNum)
    assert str(error) == DUPKEY_ERROR_TEXT


@pytest.mark.parametrize("secondCard", SECOND_CARD_CASES, ids=SECOND_CARD_CASE_IDS)
async def test_bill_payment_on_dupkey_is_written_on_the_first_card(
    makeOnlineContext, syncEngine, caplog, secondCard
):
    """The xref literal is set and logged; the WRITE on the first card then replaces it."""
    CommitSecondCard(syncEngine, secondCard.insertedCardNum)
    context = await makeOnlineContext()
    caplog.set_level(logging.INFO, logger=cobil00c.LOGGER.name)
    request = BillPayRequest(acct_id=DUPLICATED_ACCT_ID, confirm=CONFIRM_YES)
    response = await cobil00c.PayBill(context, request)
    assert (response.message, response.tran_id) == (FIRST_PAYMENT_MESSAGE, FIRST_TRAN_ID)
    assert StoredPayments(syncEngine) == [(FIRST_TRAN_ID, secondCard.firstCardNum, SEED_BALANCE)]
    assert PersistedBalance(syncEngine) == PAID_BALANCE
    assert BillPayLogRecords(caplog) == [(logging.ERROR, DUPKEY_LOG_MESSAGE)]


@pytest.mark.parametrize("whenOther", WHEN_OTHER_CASES, ids=WHEN_OTHER_CASE_IDS)
async def test_other_account_path_readers_answer_their_when_other_literal(
    makeOnlineContext, syncEngine, whenOther
):
    """CAVW, CAUP and CT02 stop at their CXACAIX WHEN OTHER branch with its 503 literal."""
    CommitSecondCard(syncEngine, SECOND_CARD_CASES[0].insertedCardNum)
    context = await makeOnlineContext()
    with pytest.raises(LegacyMessageError) as raised:
        await whenOther.readByAccount(context)
    assert (raised.value.message, raised.value.httpStatus) == (
        whenOther.expectedMessage,
        FILE_ERROR_STATUS,
    )
