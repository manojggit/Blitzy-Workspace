"""Integration tests of the batch window, record locks and TRAN-ID allocation.

Replaces: CLOSEFIL and OPENFIL (jcl/CLOSEFIL.jcl, jcl/OPENFIL.jcl: CEMT SET FIL(TRANSACT, CCXREF,
ACCTDAT, CXACAIX, USRSEC) CLO and OPE, which make online access answer NOTOPEN, RESP 19 and RESP2
60), TRANBKP (jcl/TRANBKP.jcl), the READ UPDATE record locks of CAUP / COACTUPC
(cbl/COACTUPC.cbl lines 3888-3950) and CCUP / COCRDUPC (cbl/COCRDUPC.cbl lines 1429-1511), and
the STARTBR HIGH-VALUES / READPREV / ADD 1 next TRAN-ID of CT02 / COTRN02C (cbl/COTRN02C.cbl
lines 442-449) and CB00 / COBIL00C (cbl/COBIL00C.cbl lines 212-217). Every file is
RECOVERY(NONE) in csd/CARDDEMO.CSD, so each write is durable once its own unit commits.

Every concurrency case goes through the in-process ASGI app against the test database and
proves the concurrency contract of the Technical Specification, Section 0.6.10:

- the batch window: while a session holds pg_advisory_lock(BATCH_WINDOW_LOCK), exactly as
  batch/orchestration/batch_window.BatchWindow does, CC00 and CB00 answer their WHEN OTHER
  literals (cbl/COSGN00C.cbl line 254, cbl/COBIL00C.cbl line 368) with HTTP 503, CCLI still
  lists cards because CARDDAT stays open, and both work again once the window is released;
- TRANBKP against CT02, with the batch CLI run as a subprocess: a backup holding the window
  refuses the add (cbl/COTRN02C.cbl line 600), and a backup started during an add waits for it
  and archives its row, so every transaction ends in the archive or in the table;
- record locks: a CAUP save meeting a held account lock and a CCUP save meeting a held card
  lock answer 423 after the write unit's 5 s lock_timeout, and a record changed since the
  fetch answers 409 with the fresh data shown;
- TRAN-ID allocation: parallel CT02 adds and a CB00 payment get distinct consecutive IDs, the
  ID after 9999999999999999 wraps to 0000000000000000 and then collides, and an add after
  COMBTRAN follows the interest IDs.

The subprocess cannot reach tranbkp.AFTER_ARCHIVE_READ_HOOKS, so a transaction row locked FOR
UPDATE on a separate connection holds TRANBKP after its archive read, at its DELETE. A paused
online request is a repository call patched to stop at a Gate. Waits on another session are
observed in pg_locks and pg_stat_activity of this test database only, because sibling test
databases on the same server use the same lock keys. Every subprocess is waited for or killed,
every paused request released and reaped and every sync connection closed, in finally.
restoreSeedState restores every table, the batch schema the CLI writes included, so the
TRANBKP cases need no ledger cleanup of their own. Clients, generated users and the test
database are fixtures of tests/conftest.py, so no password appears here.

PausedRequest reaps its request on every exit, a failure of the gate wait or of its block and a
cancellation of its caller included: the request is released, given time to end, cancelled if
it has not, and awaited, and how it ended is retrieved, before the failure or the cancellation
propagates. Its own cases check this: a request ending before the gate, answered or raised,
fails with that outcome; an add hanging short of the gate is cancelled and reaped; a failure
raised in the block comes out unchanged, with the request's exception retrieved; and one
cancellation of the helper, while it awaits the gate or while it reaps, propagates only once
the add has ended and released its locks.
"""

import asyncio
import contextlib
import gc
import importlib.util
import os
import re
import subprocess
import sys
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import httpx
import pytest
from sqlalchemy import BigInteger, Engine, TextClause, bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.locks import BATCH_WINDOW_LOCK
from app.repositories.account_repository import AccountRepository
from app.repositories.transaction_repository import TransactionRepository

# pytest reads a module's marks only under this exact lower-case name. Usefixtures are set up
# first and torn down last, so the app engine is disposed of before the seed restore runs.
pytestmark = pytest.mark.usefixtures("restoreSeedState")

# --- HTTP ----------------------------------------------------------------------------------------
API_V1 = "/api/v1"
LOGIN_URL = API_V1 + "/auth/login"
BILLPAY_URL = API_V1 + "/billpay"
CARDS_URL = API_V1 + "/cards"
ACCOUNTS_URL = API_V1 + "/accounts"
TRANSACTIONS_URL = API_V1 + "/transactions"
EDIT_SUFFIX = "/edit"
HTTP_OK = 200
HTTP_RECORD_CHANGED = 409
HTTP_RECORD_LOCKED = 423
# Every WHEN OTHER file-error literal answers 503, the batch window's NOTOPEN included. This
# holds for the CC00 sign-on as well: only its blank-field, NOTFND and wrong-password literals
# are the 401 sign-on failures.
HTTP_FILE_ERROR = 503
LEGACY_ERROR_KEYS = frozenset({"message", "info_message", "field_errors", "cursor_field"})

# --- Sample data: data/EBCDIC ACCTDATA, CARDDATA and CARDXREF images -----------------------------
# Account 00000000004 has one card, 2988091353094312, and a seed balance of 40.00.
SAMPLE_ACCOUNT_ID = "00000000004"
SAMPLE_CARD_NUMBER = "2988091353094312"
REGULAR_USER_TYPE = "U"
# COCRDLIC shows 7 cards per page (cbl/COCRDLIC.cbl lines 177-178).
CARD_LIST_PAGE_ROWS = 7

# --- Literals, verbatim from the COBOL sources ---------------------------------------------------
UNABLE_TO_VERIFY_TEXT = "Unable to verify the User ..."
ACCOUNT_LOOKUP_FAILED_TEXT = "Unable to lookup Account..."
CONFIRM_PAYMENT_TEXT = "Confirm to make a bill payment..."
PAYMENT_SUCCESS_TEMPLATE = "Payment successful.  Your Transaction ID is {tranId}."
ACCOUNT_XREF_LOOKUP_FAILED_TEXT = "Unable to lookup Acct in XREF AIX file..."
TRANSACTION_ADDED_TEMPLATE = "Transaction added successfully.  Your Tran ID is {tranId}."
TRAN_ID_EXISTS_TEXT = "Tran ID already exist..."
ACCOUNT_LOCK_TEXT = "Could not lock account record for update"
CARD_LOCK_TEXT = "Could not lock record for update"
RECORD_CHANGED_TEXT = "Record changed by some one else. Please review"
CHANGES_FAILED_INFO = "Changes unsuccessful. Please try again"
CHANGES_COMMITTED_INFO = "Changes committed to database"

# --- CAUP and CCUP keys, screen states and fields ------------------------------------------------
ENTER_ACTION = "enter"
SAVE_ACTION = "save"
SHOW_DETAILS_STATE = "SHOW_DETAILS"
CONFIRM_STATE = "CHANGES_OK_NOT_CONFIRMED"
DONE_STATE = "CHANGES_DONE"
FAILED_STATE = "CHANGES_FAILED"
CREDIT_LIMIT_FIELD = "credit_limit"
BALANCE_FIELD = "curr_balance"
CARD_NAME_FIELD = "embossed_name"
NO_CHANGES: Mapping[str, str] = MappingProxyType({})
# Two credit limits that differ from each other and from the seed value 3503.00.
FIRST_CREDIT_LIMIT = Decimal("2345.00")
SECOND_CREDIT_LIMIT = Decimal("6789.00")

# --- Waits and timeouts --------------------------------------------------------------------------
PROCESS_TIMEOUT_SECONDS = 120
WAIT_TIMEOUT_SECONDS = 60
GATE_TIMEOUT_SECONDS = 30
# How long PausedRequest waits for a request it has cancelled, after the GATE_TIMEOUT_SECONDS
# it gives a released request to end on its own.
REAP_TIMEOUT_SECONDS = 30
# The gate waiter is cancelled at once: it only watches the gate.
NO_GRACE_SECONDS = 0
# The gate wait and release grace of the case whose add hangs short of the gate; long enough
# for the add to reach its INSERT first.
SHORT_GATE_TIMEOUT_SECONDS = 5.0
# How long a released add of the own-cancellation case holds its locks before its INSERT; long
# enough to outlast a reap that is cut short.
SLOW_RELEASE_SECONDS = 1.0
POLL_INTERVAL_SECONDS = 0.05

# --- Failure messages ----------------------------------------------------------------------------
BATCH_PACKAGE_NAME = "batch"
BATCH_PACKAGE_MISSING_MESSAGE = (
    "The batch package is not installed, and the TRANBKP cases run its CLI: install it with"
    " '.venv/bin/pip install -e batch' from the repository root."
)
WAIT_FAILED_MESSAGE = "Gave up after {seconds} s waiting for {description}."
PROCESS_TIMEOUT_MESSAGE = "TRANBKP did not end within {seconds} s and was killed."
GATE_SKIPPED_MESSAGE = "The request ended before its patched call paused: it {outcome}"
GATE_TIMEOUT_MESSAGE = "The patched call did not pause within {seconds} s."
TASK_NOT_REAPED_MESSAGE = "Still running {seconds} s after its cancellation: {task!r}"
TASKS_LEFT_RUNNING_MESSAGE = "Tasks left running after PausedRequest ended: {tasks!r}"
HELPER_NOT_CANCELLED_MESSAGE = "PausedRequest did not end by its own cancellation: {task!r}"
BLOCK_ENTERED_MESSAGE = "The PausedRequest block ran, although the gate was never reached."
REQUEST_NOT_PAUSED_MESSAGE = "The request had ended when the PausedRequest block began."
REQUEST_NOT_ENDED_MESSAGE = "The released request was not left to end on its own."
HANG_NOT_REACHED_MESSAGE = "The add never reached its hanging INSERT."
LOCKS_NOT_HELD_MESSAGE = "The paused add held no advisory lock, so their release proves nothing."
# asyncio's report of a task dropped with an exception nobody retrieved.
UNRETRIEVED_TASK_EXCEPTION_MESSAGE = "Task exception was never retrieved"

# --- How a request task ended, as AwaitGate reports it -------------------------------------------
REQUEST_CANCELLED_OUTCOME = "was cancelled"
REQUEST_RAISED_OUTCOME = "raised {error!r}"
REQUEST_ANSWERED_OUTCOME = "answered {status} {body}"
CONNECTION_REFUSED_TEXT = "the test transport refused the connection"
CONNECTION_LOST_TEXT = "the test transport lost the connection"
BLOCK_FAILURE_TEXT = "a failure raised in the PausedRequest block"

# --- CT02 (COTRN02C) and CB00 (COBIL00C) request bodies ------------------------------------------
# A Transaction Add ENTER every COTRN02C edit accepts, keyed by account, confirmed with Y.
VALID_ADD_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        "action": ENTER_ACTION,
        "acct_id": SAMPLE_ACCOUNT_ID,
        "card_num": "",
        "type_cd": "01",
        "cat_cd": "0001",
        "source": "POS TERM",
        "desc": "CONCURRENCY PURCHASE",
        "amount": "+00000050.00",
        "orig_date": "2022-07-17",
        "proc_date": "2022-07-17",
        "merchant_id": "000000001",
        "merchant_name": "CONCURRENCY MERCHANT",
        "merchant_city": "CONCURRENCY CITY",
        "merchant_zip": "00000",
        "confirm": "Y",
    }
)
# A blank confirm asks for confirmation; Y pays the whole balance.
PROMPT_PAYMENT_BODY: Mapping[str, str] = MappingProxyType(
    {"acct_id": SAMPLE_ACCOUNT_ID, "confirm": ""}
)
CONFIRMED_PAYMENT_BODY: Mapping[str, str] = MappingProxyType(
    {"acct_id": SAMPLE_ACCOUNT_ID, "confirm": "Y"}
)
PARALLEL_ADD_COUNT = 3

# --- TRAN-IDs ------------------------------------------------------------------------------------
TRAN_ID_LENGTH = 16
HIGHEST_TRAN_ID = "9999999999999999"
WRAPPED_TRAN_ID = "0000000000000000"
# The four IDs an empty TRANSACT gives four allocations in a row.
CONSECUTIVE_TRAN_IDS = tuple(f"{number:016d}" for number in range(1, 5))
# INTCALC's IDs are PARM-DATE followed by a 6-digit suffix (cbl/CBACT04C.cbl lines 474-479).
INTEREST_RUN_DATE = "2022071800"
INTEREST_SOURCE = "System"
INTEREST_DESCRIPTION = "Int. for a/c " + SAMPLE_ACCOUNT_ID
NEXT_AFTER_INTEREST_ID = INTEREST_RUN_DATE + "000004"
ONLINE_TRAN_ID_BEFORE_COMBTRAN = "0000000000000009"

# --- Seeded transactions: the sample card, S-form timestamps -------------------------------------
SEED_ROW_VALUES: Mapping[str, object] = MappingProxyType(
    {
        "typeCd": "01",
        "catCd": 1,
        "amount": Decimal("12.34"),
        "merchantId": "000000001",
        "merchantName": "CONCURRENCY SEED MERCHANT",
        "merchantCity": "CONCURRENCY SEED CITY",
        "merchantZip": "10001",
        "cardNum": SAMPLE_CARD_NUMBER,
        "timestamp": datetime(2022, 6, 10, 19, 27, 53),
        "timestampFormat": "S",
    }
)


@dataclass(frozen=True)
class SeedTransaction:
    """One TRAN-RECORD (CVTRA05Y) committed on the sample card before a test's requests."""

    tranId: str
    source: str = "POS TERM"
    description: str = "CONCURRENCY SEED"


BACKUP_FIRST_TRAN_IDS = ("0000000000000003", "0000000000000005", "0000000000000007")
BACKUP_FIRST_SEEDS = tuple(SeedTransaction(tranId) for tranId in BACKUP_FIRST_TRAN_IDS)
IN_FLIGHT_SEED = SeedTransaction("0000000000000005")
INTEREST_SEEDS = tuple(
    SeedTransaction(f"{INTEREST_RUN_DATE}{suffix:06d}", INTEREST_SOURCE, INTEREST_DESCRIPTION)
    for suffix in range(1, 4)
)
COMBINED_SEEDS = (SeedTransaction(ONLINE_TRAN_ID_BEFORE_COMBTRAN), *INTEREST_SEEDS)

# --- CAUP (COACTUPC) and CCUP (COCRDUPC) editable records ----------------------------------------
# Account values passing every COACTUPC edit: status Y, and past dates of century 19 or 20.
EDITABLE_ACCOUNT_VALUES: Mapping[str, object] = MappingProxyType(
    {
        "active_status": "Y",
        "open_date": date(2010, 1, 15),
        "expiration_date": date(2020, 6, 30),
        "reissue_date": date(2018, 3, 1),
    }
)
# Customer values passing every COACTUPC edit. NY10 is a VALID-US-STATE-ZIP-CD2-COMBO and 212 a
# VALID-GENERAL-PURP-CODE (cpy/CSLKPCDY.cpy); the phones keep the seed form (999)999-9999. The
# seed customer of the sample account fails several edits, its state and ZIP pair among them.
EDITABLE_CUSTOMER_VALUES: Mapping[str, object] = MappingProxyType(
    {
        "first_name": "Ada",
        "middle_name": "Maria",
        "last_name": "Lovelace",
        "addr_line_1": "100 Main Street",
        "addr_line_2": "Suite 5",
        "addr_line_3": "New York",
        "addr_state_cd": "NY",
        "addr_zip": "10001",
        "addr_country_cd": "USA",
        "phone_num_1": "(212)555-0100",
        "phone_num_2": "(212)555-0100",
        "ssn": "123456789",
        "date_of_birth": date(1980, 5, 15),
        "fico_credit_score": 700,
        "eft_account_id": "5550001111",
        "primary_holder_ind": "Y",
    }
)
# A card every COCRDUPC edit accepts unchanged: letters and spaces, status Y, a 2025 expiry.
EDITABLE_CARD_NAME = "Edit Card Holder"
EDITABLE_CARD_VALUES: Mapping[str, object] = MappingProxyType(
    {"embossedName": EDITABLE_CARD_NAME, "activeStatus": "Y", "expirationDate": date(2025, 1, 31)}
)
VALIDATED_CARD_NAME = "Validated Card Holder"
CONCURRENT_CARD_NAME = "Concurrent Card Holder"

# --- SQL: constant text, values always bound -----------------------------------------------------
INSERT_TRANSACTION_SQL = text(
    "INSERT INTO transactions.transactions (tran_id, tran_type_cd, tran_cat_cd, tran_source,"
    " tran_desc, tran_amt, merchant_id, merchant_name, merchant_city, merchant_zip, card_num,"
    " orig_ts, orig_ts_format, proc_ts, proc_ts_format) VALUES (:tranId, :typeCd, :catCd,"
    " :source, :description, :amount, :merchantId, :merchantName, :merchantCity, :merchantZip,"
    " :cardNum, :timestamp, :timestampFormat, :timestamp, :timestampFormat)"
)
TRAN_IDS_SQL = text("SELECT tran_id FROM transactions.transactions ORDER BY tran_id_sort")
MAKE_ACCOUNT_EDITABLE_SQL = text(
    "UPDATE accounts.accounts SET active_status = :active_status, open_date = :open_date,"
    " expiration_date = :expiration_date, reissue_date = :reissue_date"
    " WHERE acct_id = :acct_id"
)
# The account's customer is the one its cross-reference names, as COACTUPC reads CXACAIX first.
MAKE_CUSTOMER_EDITABLE_SQL = text(
    "UPDATE customers.customers SET first_name = :first_name, middle_name = :middle_name,"
    " last_name = :last_name, addr_line_1 = :addr_line_1, addr_line_2 = :addr_line_2,"
    " addr_line_3 = :addr_line_3, addr_state_cd = :addr_state_cd, addr_zip = :addr_zip,"
    " addr_country_cd = :addr_country_cd, phone_num_1 = :phone_num_1,"
    " phone_num_2 = :phone_num_2, ssn = :ssn, date_of_birth = :date_of_birth,"
    " fico_credit_score = :fico_credit_score, eft_account_id = :eft_account_id,"
    " primary_holder_ind = :primary_holder_ind"
    " WHERE cust_id IN (SELECT cust_id FROM xref.card_xref WHERE acct_id = :acct_id)"
)
ACCOUNT_AMOUNTS_SQL = text(
    "SELECT credit_limit, curr_balance FROM accounts.accounts WHERE acct_id = :acctId"
)
RAISE_BALANCE_SQL = text(
    "UPDATE accounts.accounts SET curr_balance = curr_balance + 1 WHERE acct_id = :acctId"
)
MAKE_CARD_EDITABLE_SQL = text(
    "UPDATE cards.cards SET embossed_name = :embossedName, active_status = :activeStatus,"
    " expiration_date = :expirationDate WHERE card_num = :cardNum"
)
RENAME_CARD_SQL = text(
    "UPDATE cards.cards SET embossed_name = :embossedName WHERE card_num = :cardNum"
)
CARD_NAME_SQL = text("SELECT embossed_name FROM cards.cards WHERE card_num = :cardNum")
TRANSACTION_ROW_LOCK_SQL = text(
    "SELECT 1 FROM transactions.transactions WHERE tran_id = :tranId FOR UPDATE"
)
CARD_ROW_LOCK_SQL = text("SELECT 1 FROM cards.cards WHERE card_num = :cardNum FOR UPDATE")
# The batch window as BatchWindow takes it: the single BIGINT key, session-level and exclusive.
ACQUIRE_WINDOW_SQL = text("SELECT pg_advisory_lock(:lockKey)").bindparams(
    bindparam("lockKey", type_=BigInteger)
)
RELEASE_WINDOW_SQL = text("SELECT pg_advisory_unlock(:lockKey)").bindparams(
    bindparam("lockKey", type_=BigInteger)
)
AUTOCOMMIT_ISOLATION = "AUTOCOMMIT"

# --- TRANBKP subprocess and its TRANSACT.BKUP(+1) generation -------------------------------------
# Chain step 6; --force runs it whatever the state the seed cycle left the step in.
TRAN_BACKUP_COMMAND = (
    sys.executable,
    "-m",
    "batch.cli",
    "job",
    "tran-backup",
    "--occurrence",
    "1",
    "--force",
)
OUTPUT_DIR_VARIABLE = "CARDDEMO_OUTPUT_DIR"
GDG_DIRECTORY_NAME = "gdg"
GENERATION_FILE_GLOB = "G*V00.dat"
# batch/orchestration/gdg.py writes each 350-byte CVTRA05Y record in latin-1, ended by "\n".
GDG_FILE_ENCODING = "latin-1"
RECORD_TERMINATOR = "\n"
TRANSACTION_RECORD_LENGTH = 350


# --- Value objects -------------------------------------------------------------------------------
@dataclass(frozen=True)
class Probe:
    """A count query on the server's lock or activity view, what it detects and its parameters."""

    description: str
    statement: TextClause
    parameters: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RowLock:
    """A ``SELECT ... FOR UPDATE`` of one row and the bound key that names the row."""

    statement: TextClause
    parameters: Mapping[str, str]


@dataclass(frozen=True)
class EditScreen:
    """The CAUP or CCUP screen of one record: its update path and the query its fetch needs."""

    updateUrl: str
    fetchQuery: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessResult:
    """The exit status and the decoded output of a finished subprocess."""

    returnCode: int
    stdout: str
    stderr: str


@dataclass
class Gate:
    """Where a patched repository call stops, before or after the original runs.

    The first ``callsToPause`` calls stop: each sets ``reached`` and waits for ``release``.
    """

    pauseAfterCall: bool = False
    callsToPause: int = 1
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    pausedCalls: int = 0

    def TakePause(self) -> bool:
        """Return True, counting the call, while fewer than ``callsToPause`` calls have paused."""
        if self.pausedCalls >= self.callsToPause:
            return False
        self.pausedCalls += 1
        return True

    async def Pause(self) -> None:
        """Signal ``reached``, then wait for ``release``, bounded so a failed test never hangs."""
        self.reached.set()
        await asyncio.wait_for(self.release.wait(), GATE_TIMEOUT_SECONDS)


type RepositoryWrite = Callable[[Any, AsyncConnection, Mapping[str, Any]], Awaitable[None]]

ACCOUNT_SCREEN = EditScreen(f"{ACCOUNTS_URL}/{SAMPLE_ACCOUNT_ID}")
# CCUP is opened with typed keys, which the {cardNum} path and the acctId query carry.
CARD_SCREEN = EditScreen(
    f"{CARDS_URL}/{SAMPLE_CARD_NUMBER}", MappingProxyType({"acctId": SAMPLE_ACCOUNT_ID})
)
FIRST_SEED_ROW_LOCK = RowLock(
    TRANSACTION_ROW_LOCK_SQL, MappingProxyType({"tranId": BACKUP_FIRST_TRAN_IDS[0]})
)
SAMPLE_CARD_ROW_LOCK = RowLock(CARD_ROW_LOCK_SQL, MappingProxyType({"cardNum": SAMPLE_CARD_NUMBER}))

# TRANBKP's DELETE waiting on the row lock the test holds. pg_backend_pid() is the probe's own
# connection, and the holder of the row lock waits on nothing.
BACKUP_BLOCKED_ON_ROW_LOCK = Probe(
    "TRANBKP to wait at its DELETE on the locked transaction row",
    text(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        " AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'"
        " AND query ILIKE :queryPattern"
    ),
    MappingProxyType({"queryPattern": "%transactions.transactions%"}),
)
# pg_locks shows a single-BIGINT advisory key as its high 32 bits in classid and its low 32
# bits in objid.
BACKUP_WAITING_FOR_WINDOW = Probe(
    "TRANBKP to wait for the batch window an online request holds shared",
    text(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
        " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        " AND classid::bigint = :highKey AND objid::bigint = :lowKey"
    ),
    MappingProxyType(
        {
            "highKey": (BATCH_WINDOW_LOCK >> 32) & 0xFFFFFFFF,
            "lowKey": BATCH_WINDOW_LOCK & 0xFFFFFFFF,
        }
    ),
)
# Every advisory lock held or awaited by a session of this test database: the batch window and
# the TRAN-ID allocation keys a CT02 add holds at its INSERT among them.
ADVISORY_LOCKS_HELD = Probe(
    "an advisory lock of this test database",
    text(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
        " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    ),
)


# --- Test data, committed through syncEngine and undone by restoreSeedState ---------------------
def InsertTransactions(syncEngine: Engine, rows: Sequence[SeedTransaction]) -> None:
    """Commit ``rows`` to transactions.transactions in one transaction."""
    parameters = [
        {
            **SEED_ROW_VALUES,
            "tranId": row.tranId,
            "source": row.source,
            "description": row.description,
        }
        for row in rows
    ]
    with syncEngine.begin() as connection:
        connection.execute(INSERT_TRANSACTION_SQL, parameters)


def ReadTranIds(syncEngine: Engine) -> list[str]:
    """Return every committed TRAN-ID in TRANSACT key order (EBCDIC, ``tran_id_sort``)."""
    with syncEngine.connect() as connection:
        return list(connection.execute(TRAN_IDS_SQL).scalars())


def MakeAccountEditable(syncEngine: Engine, acctId: str) -> None:
    """Commit account and customer values that pass every COACTUPC edit."""
    with syncEngine.begin() as connection:
        connection.execute(
            MAKE_ACCOUNT_EDITABLE_SQL, {"acct_id": acctId, **EDITABLE_ACCOUNT_VALUES}
        )
        connection.execute(
            MAKE_CUSTOMER_EDITABLE_SQL, {"acct_id": acctId, **EDITABLE_CUSTOMER_VALUES}
        )


def ReadAccountAmounts(syncEngine: Engine, acctId: str) -> dict[str, Decimal]:
    """Return the committed credit limit and current balance of ``acctId``."""
    with syncEngine.connect() as connection:
        row = connection.execute(ACCOUNT_AMOUNTS_SQL, {"acctId": acctId}).mappings().one()
    return dict(row)


def RaiseAccountBalance(syncEngine: Engine, acctId: str) -> None:
    """Commit ``curr_balance + 1`` on ``acctId``, as another task's rewrite would."""
    with syncEngine.begin() as connection:
        connection.execute(RAISE_BALANCE_SQL, {"acctId": acctId})


def MakeCardEditable(syncEngine: Engine, cardNum: str) -> None:
    """Commit a card every COCRDUPC edit accepts unchanged."""
    with syncEngine.begin() as connection:
        connection.execute(MAKE_CARD_EDITABLE_SQL, {"cardNum": cardNum, **EDITABLE_CARD_VALUES})


def RenameCard(syncEngine: Engine, cardNum: str, embossedName: str) -> None:
    """Commit a new embossed name on ``cardNum``, as another task's rewrite would."""
    with syncEngine.begin() as connection:
        connection.execute(RENAME_CARD_SQL, {"cardNum": cardNum, "embossedName": embossedName})


def ReadCardName(syncEngine: Engine, cardNum: str) -> str:
    """Return the committed embossed name of ``cardNum``."""
    with syncEngine.connect() as connection:
        return connection.execute(CARD_NAME_SQL, {"cardNum": cardNum}).scalar_one()


def ValidAddBody() -> dict[str, str]:
    """Return a fresh CT02 ENTER body that every COTRN02C edit accepts, confirmed with Y."""
    return dict(VALID_ADD_FIELDS)


# --- Locks held by the test, each on a sync connection of its own -------------------------------
@contextlib.contextmanager
def HoldBatchWindow(syncEngine: Engine) -> Iterator[None]:
    """Close the batch window to online access, as CLOSEFIL did, until the block ends (OPENFIL).

    The exclusive session lock is taken on an autocommit connection, as BatchWindow takes it,
    and released, with the connection closed, whatever the block raises.
    """
    with syncEngine.connect() as connection:
        connection.execution_options(isolation_level=AUTOCOMMIT_ISOLATION)
        connection.execute(ACQUIRE_WINDOW_SQL, {"lockKey": BATCH_WINDOW_LOCK})
        try:
            yield
        finally:
            connection.execute(RELEASE_WINDOW_SQL, {"lockKey": BATCH_WINDOW_LOCK})


@contextlib.contextmanager
def HoldRowLock(syncEngine: Engine, rowLock: RowLock) -> Iterator[Callable[[], None]]:
    """Lock one row FOR UPDATE in an open transaction; yield the release, which exit repeats.

    The release rolls the transaction back; a second rollback has no effect, and closing the
    connection at exit ends the lock whatever the block raises.
    """
    with syncEngine.connect() as connection:
        connection.execute(rowLock.statement, dict(rowLock.parameters)).one()
        yield connection.rollback


def CountRows(syncEngine: Engine, probe: Probe) -> int:
    """Return the count the probe's query reads now, on a pooled connection of the test."""
    with syncEngine.connect() as connection:
        return connection.execute(probe.statement, dict(probe.parameters)).scalar_one()


async def WaitUntil(syncEngine: Engine, probe: Probe, timeoutSeconds: float) -> None:
    """Poll ``probe`` every POLL_INTERVAL_SECONDS until it counts a row; fail after the timeout."""
    eventLoop = asyncio.get_running_loop()
    deadline = eventLoop.time() + timeoutSeconds
    while eventLoop.time() < deadline:
        if CountRows(syncEngine, probe) > 0:
            return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(WAIT_FAILED_MESSAGE.format(seconds=timeoutSeconds, description=probe.description))


# --- TRANBKP, the batch CLI as a subprocess ------------------------------------------------------
def RequireBatchPackage() -> None:
    """Fail, never skip, when the batch package whose CLI the TRANBKP cases run is missing."""
    if importlib.util.find_spec(BATCH_PACKAGE_NAME) is None:
        pytest.fail(BATCH_PACKAGE_MISSING_MESSAGE, pytrace=False)


def StartTranBackup(outputDir: Path) -> subprocess.Popen[bytes]:
    """Start ``carddemo-batch job tran-backup`` in its module form, writing under ``outputDir``.

    The child inherits the test database URLs and APP_ENV=test that tests/conftest.py put in
    the environment, so it runs against the test database.
    """
    childEnvironment = {**os.environ, OUTPUT_DIR_VARIABLE: str(outputDir)}
    return subprocess.Popen(
        TRAN_BACKUP_COMMAND, env=childEnvironment, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


@contextlib.contextmanager
def RunningTranBackup(outputDir: Path) -> Iterator[subprocess.Popen[bytes]]:
    """Start TRANBKP; at exit kill it if it still runs, then close its pipes and reap it."""
    with StartTranBackup(outputDir) as process:
        try:
            yield process
        finally:
            if process.poll() is None:
                process.kill()


async def FinishProcess(process: subprocess.Popen[bytes]) -> ProcessResult:
    """Wait off the event loop for the subprocess to end; kill it and fail past the timeout."""
    try:
        stdout, stderr = await asyncio.to_thread(
            process.communicate, timeout=PROCESS_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        process.kill()
        await asyncio.to_thread(process.communicate)
        pytest.fail(PROCESS_TIMEOUT_MESSAGE.format(seconds=PROCESS_TIMEOUT_SECONDS))
    return ProcessResult(process.returncode, stdout.decode(), stderr.decode())


def AssertProcessSucceeded(result: ProcessResult) -> None:
    """Assert the CLI exited 0, showing its output otherwise."""
    assert result.returnCode == 0, f"exit {result.returnCode}\n{result.stderr}\n{result.stdout}"


def ReadArchiveTranIds(outputDir: Path) -> set[str]:
    """Return the TRAN-ID of every record of the one TRANSACT.BKUP generation TRANBKP wrote."""
    generationFiles = sorted((outputDir / GDG_DIRECTORY_NAME).rglob(GENERATION_FILE_GLOB))
    assert len(generationFiles) == 1, generationFiles
    generationText = generationFiles[0].read_bytes().decode(GDG_FILE_ENCODING)
    records = [line for line in generationText.split(RECORD_TERMINATOR) if line]
    assert all(len(record) == TRANSACTION_RECORD_LENGTH for record in records)
    return {record[:TRAN_ID_LENGTH] for record in records}


@pytest.fixture(name="backupOutputDir")
def BackupOutputDirFixture(request: pytest.FixtureRequest) -> Path:
    """Return this test's CARDDEMO_OUTPUT_DIR, pytest's tmp_path; fail when batch is missing."""
    RequireBatchPackage()
    return request.getfixturevalue("tmp_path")


# --- Paused repository calls ---------------------------------------------------------------------
def PausedCall(original: RepositoryWrite, gate: Gate) -> RepositoryWrite:
    """Return ``original`` wrapped to stop at ``gate``, before or after it runs as the gate says.

    Installed on the repository class, the wrapper receives the repository instance first.
    """

    async def GatedCall(repository: Any, unit: AsyncConnection, values: Mapping[str, Any]) -> None:
        pausesHere = gate.TakePause()
        if pausesHere and not gate.pauseAfterCall:
            await gate.Pause()
        await original(repository, unit, values)
        if pausesHere and gate.pauseAfterCall:
            await gate.Pause()

    return GatedCall


def InstallGate(
    monkeypatch: pytest.MonkeyPatch, repositoryClass: type, methodName: str, gate: Gate
) -> None:
    """Replace ``repositoryClass.methodName`` by its PausedCall for the current test only."""
    original = getattr(repositoryClass, methodName)
    monkeypatch.setattr(repositoryClass, methodName, PausedCall(original, gate))


def SlowlyReleasedCall(
    original: RepositoryWrite, gate: Gate, slowPhase: asyncio.Event
) -> RepositoryWrite:
    """Return ``original`` wrapped to stop at ``gate`` and, once released, wait before it runs.

    The wait sets ``slowPhase`` and lasts SLOW_RELEASE_SECONDS, holding every lock the request
    holds at the call, so a reap cut short leaves the request running with its locks.
    """

    async def PauseThenWriteSlowly(
        repository: Any, unit: AsyncConnection, values: Mapping[str, Any]
    ) -> None:
        await gate.Pause()
        slowPhase.set()
        await asyncio.sleep(SLOW_RELEASE_SECONDS)
        await original(repository, unit, values)

    return PauseThenWriteSlowly


def HangingCall(hangReached: asyncio.Event) -> RepositoryWrite:
    """Return a repository write that sets ``hangReached`` and then waits until it is cancelled."""

    async def HangInsteadOfWriting(
        repository: Any, unit: AsyncConnection, values: Mapping[str, Any]
    ) -> None:
        hangReached.set()
        await asyncio.Event().wait()

    return HangInsteadOfWriting


async def WaitThroughCancellation(
    task: asyncio.Task[Any], timeoutSeconds: float
) -> asyncio.CancelledError | None:
    """Wait until ``task`` has ended or ``timeoutSeconds`` have passed, even when cancelled.

    A cancellation of the caller does not end the wait: it is caught, and the wait goes on until
    the same deadline. Return the last cancellation caught, or None, for the caller to re-raise
    once it is done with ``task``. asyncio.wait raises neither the task's exception nor its
    cancellation.
    """
    eventLoop = asyncio.get_running_loop()
    deadline = eventLoop.time() + timeoutSeconds
    cancellation = None
    while not task.done() and eventLoop.time() < deadline:
        try:
            await asyncio.wait({task}, timeout=deadline - eventLoop.time())
        except asyncio.CancelledError as error:
            cancellation = error
    return cancellation


async def ReapTask(task: asyncio.Task[Any], graceSeconds: float) -> None:
    """Give ``task`` ``graceSeconds`` to end, else cancel it and wait REAP_TIMEOUT_SECONDS more.

    A cancellation of the caller cuts neither wait short; the last one caught is re-raised only
    after both. The exception of a task that ended without being cancelled is retrieved, so
    asyncio never reports it as unretrieved. A task still running after both waits is left for
    the caller to report.
    """
    cancellation = await WaitThroughCancellation(task, graceSeconds)
    if not task.done():
        task.cancel()
        lateCancellation = await WaitThroughCancellation(task, REAP_TIMEOUT_SECONDS)
        cancellation = lateCancellation or cancellation
    if task.done() and not task.cancelled():
        task.exception()
    if cancellation is not None:
        raise cancellation


def AssertTaskEnded(task: asyncio.Task[Any]) -> None:
    """Fail when ``task`` is still running after ReapTask has cancelled and awaited it."""
    if not task.done():
        pytest.fail(TASK_NOT_REAPED_MESSAGE.format(seconds=REAP_TIMEOUT_SECONDS, task=task))


def DescribeRequestOutcome(requestTask: asyncio.Task[httpx.Response]) -> str:
    """Return how the ended ``requestTask`` finished: cancelled, raised, or answered."""
    if requestTask.cancelled():
        return REQUEST_CANCELLED_OUTCOME
    error = requestTask.exception()
    if error is not None:
        return REQUEST_RAISED_OUTCOME.format(error=error)
    response = requestTask.result()
    return REQUEST_ANSWERED_OUTCOME.format(status=response.status_code, body=response.text)


async def AwaitGate(gate: Gate, requestTask: asyncio.Task[httpx.Response]) -> None:
    """Wait until a patched call reaches ``gate``; fail when the request ends first or times out.

    The gate waiter is cancelled and reaped on every exit, a cancellation of the caller
    included. A request that ended first fails the test with how it ended; its exception is
    described, never raised.
    """
    reachedWaiter = asyncio.create_task(gate.reached.wait())
    try:
        await asyncio.wait(
            {reachedWaiter, requestTask},
            timeout=GATE_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        await ReapTask(reachedWaiter, NO_GRACE_SECONDS)
    AssertTaskEnded(reachedWaiter)
    if gate.reached.is_set():
        return
    if requestTask.done():
        pytest.fail(GATE_SKIPPED_MESSAGE.format(outcome=DescribeRequestOutcome(requestTask)))
    pytest.fail(GATE_TIMEOUT_MESSAGE.format(seconds=GATE_TIMEOUT_SECONDS))


@contextlib.asynccontextmanager
async def PausedRequest(
    request: Coroutine[Any, Any, httpx.Response], gate: Gate
) -> AsyncIterator[asyncio.Task[httpx.Response]]:
    """Send ``request`` as a task and yield it once its patched call has stopped at ``gate``.

    On every exit, a failure of the gate wait or of the block and a cancellation of the caller
    included, the gate is released and the request reaped (ReapTask): given
    GATE_TIMEOUT_SECONDS to end, then cancelled and awaited. How it ended is retrieved, and a
    cancellation of the caller propagates only after the reap. A failure of the gate wait or
    of the block propagates unchanged; after a block that ends normally, a request still
    running fails the test.
    """
    requestTask = asyncio.create_task(request)
    try:
        await AwaitGate(gate, requestTask)
        yield requestTask
    finally:
        gate.release.set()
        await ReapTask(requestTask, GATE_TIMEOUT_SECONDS)
    AssertTaskEnded(requestTask)


# --- Requests and assertions ---------------------------------------------------------------------
def ExpectOk(response: httpx.Response) -> dict[str, Any]:
    """Assert HTTP 200 and return the JSON body."""
    assert response.status_code == HTTP_OK, response.text
    return response.json()


def AssertLegacyError(response: httpx.Response, status: int, message: str) -> dict[str, Any]:
    """Assert a legacy error body with ``status`` and the literal verbatim; return the body."""
    assert response.status_code == status, response.text
    body = response.json()
    assert LEGACY_ERROR_KEYS <= body.keys()
    assert body["message"] == message
    return body


def AssertScreenState(screen: Mapping[str, Any], state: str, infoMessage: str) -> None:
    """Assert a CAUP or CCUP screen is in ``state`` and shows that state's information line."""
    assert screen["state"] == state, screen
    assert screen["info_message"] == infoMessage


def AssertAddedMessages(bodies: Sequence[Mapping[str, Any]], template: str) -> None:
    """Assert each success body's message names the TRAN-ID the body returns."""
    for body in bodies:
        assert body["message"] == template.format(tranId=body["tran_id"])


def ScreenAmount(screenText: str) -> Decimal:
    """Return the value a +ZZZ,ZZZ,ZZZ.99 screen amount shows, such as ``+         41.00``."""
    return Decimal(screenText.replace(",", "").replace(" ", ""))


def UpdateBody(
    screen: Mapping[str, Any], action: str, overrides: Mapping[str, str]
) -> dict[str, Any]:
    """Return the key press that sends the screen's token back with ``overrides`` keyed."""
    return {
        "action": action,
        "screen_token": screen["screen_token"],
        "fields": {**screen["fields"], **overrides},
    }


async def ValidatedScreen(
    client: httpx.AsyncClient, editScreen: EditScreen, overrides: Mapping[str, str]
) -> dict[str, Any]:
    """Fetch the record for edit, ENTER ``overrides`` and return the confirmation screen.

    The returned screen_token carries the validated values and the before-image of the fetch.
    """
    fetchUrl = editScreen.updateUrl + EDIT_SUFFIX
    fetched = ExpectOk(await client.get(fetchUrl, params=dict(editScreen.fetchQuery)))
    enterBody = UpdateBody(fetched, ENTER_ACTION, overrides)
    validated = ExpectOk(await client.put(editScreen.updateUrl, json=enterBody))
    assert validated["state"] == CONFIRM_STATE, validated
    return validated


async def PressSave(
    client: httpx.AsyncClient, editScreen: EditScreen, validatedScreen: Mapping[str, Any]
) -> httpx.Response:
    """Press F5 on a validated CAUP or CCUP screen and return the response."""
    saveBody = UpdateBody(validatedScreen, SAVE_ACTION, NO_CHANGES)
    return await client.put(editScreen.updateUrl, json=saveBody)


# --- PausedRequest's own cases: stand-in requests and checks -------------------------------------
async def RefusedRequest() -> httpx.Response:
    """Stand in for a request whose connection is refused before the app is reached."""
    raise httpx.ConnectError(CONNECTION_REFUSED_TEXT)


async def FailingAfterGate(gate: Gate) -> httpx.Response:
    """Stand in for a request that stops at ``gate`` and, once released, loses its connection."""
    await gate.Pause()
    raise httpx.ReadError(CONNECTION_LOST_TEXT)


async def LeavePausedRequestAtOnce(
    request: Coroutine[Any, Any, httpx.Response], gate: Gate
) -> None:
    """Enter PausedRequest and leave its block as soon as the request is paused, unawaited."""
    async with PausedRequest(request, gate) as requestTask:
        assert not requestTask.done(), REQUEST_NOT_PAUSED_MESSAGE


async def RaiseInsidePausedRequest(gate: Gate) -> bool:
    """Raise a failure in a PausedRequest block before its request is awaited; assert it comes out.

    Return whether the request had then ended on its own, neither cancelled nor still running.
    No reference to the request outlives this call, so the collector finalises it afterwards,
    and asyncio reports it then if its exception was never retrieved.
    """
    blockFailure = AssertionError(BLOCK_FAILURE_TEXT)
    with pytest.raises(AssertionError) as raised:
        async with PausedRequest(FailingAfterGate(gate), gate) as requestTask:
            raise blockFailure
    assert raised.value is blockFailure, raised.value
    return requestTask.done() and not requestTask.cancelled()


@contextlib.contextmanager
def RecordedLoopErrors() -> Iterator[list[dict[str, Any]]]:
    """Yield the list of what the running loop reports to its exception handler in the block.

    The collector runs before the recording starts, so earlier garbage is reported as usual,
    and again before it ends, so a task dropped in the block with its exception never
    retrieved is reported into the list. The previous handler is restored at exit.
    """
    eventLoop = asyncio.get_running_loop()
    previousHandler = eventLoop.get_exception_handler()
    gc.collect()
    loopErrors: list[dict[str, Any]] = []
    eventLoop.set_exception_handler(lambda handlerLoop, context: loopErrors.append(context))
    try:
        yield loopErrors
    finally:
        gc.collect()
        eventLoop.set_exception_handler(previousHandler)


def AssertNoTaskLeft(tasksBefore: set[asyncio.Task[Any]]) -> None:
    """Assert that every task of the running loop that is not in ``tasksBefore`` has ended."""
    leftTasks = asyncio.all_tasks() - tasksBefore
    assert not leftTasks, TASKS_LEFT_RUNNING_MESSAGE.format(tasks=leftTasks)


# Requests that end before the gate on TRANSACT's INSERT: CCLI answers without touching
# TRANSACT, and a refused connection raises. Each comes with the outcome AwaitGate must report.
ENDED_REQUESTS = [
    pytest.param(
        lambda client: client.get(CARDS_URL),
        REQUEST_ANSWERED_OUTCOME.format(status=HTTP_OK, body=""),
        id="answered",
    ),
    pytest.param(
        lambda client: RefusedRequest(),
        REQUEST_RAISED_OUTCOME.format(error=httpx.ConnectError(CONNECTION_REFUSED_TEXT)),
        id="raised",
    ),
]


# --- The batch window (CLOSEFIL / OPENFIL) -------------------------------------------------------
async def test_window_refuses_sign_on_until_released(
    apiClient: httpx.AsyncClient,
    createUser: Callable[[str], Any],
    syncEngine: Engine,
) -> None:
    """CC00 reads USRSEC first: NOTOPEN is the WHEN OTHER literal (COSGN00C line 254), then 200."""
    user = createUser(REGULAR_USER_TYPE)
    credentials = {"user_id": user.userId, "password": user.password}
    with HoldBatchWindow(syncEngine):
        refused = await apiClient.post(LOGIN_URL, json=credentials)
    AssertLegacyError(refused, HTTP_FILE_ERROR, UNABLE_TO_VERIFY_TEXT)
    signedOn = await apiClient.post(LOGIN_URL, json=credentials)
    assert signedOn.status_code == HTTP_OK, signedOn.text


async def test_window_refuses_bill_payment_until_released(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """CB00 reads ACCTDAT first: NOTOPEN is ``Unable to lookup Account...`` (COBIL00C line 368)."""
    with HoldBatchWindow(syncEngine):
        refused = await regularClient.post(BILLPAY_URL, json=dict(PROMPT_PAYMENT_BODY))
    AssertLegacyError(refused, HTTP_FILE_ERROR, ACCOUNT_LOOKUP_FAILED_TEXT)
    prompted = ExpectOk(await regularClient.post(BILLPAY_URL, json=dict(PROMPT_PAYMENT_BODY)))
    assert prompted["message"] == CONFIRM_PAYMENT_TEXT


async def test_window_leaves_card_list_open(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """CCLI browses CARDDAT, which CLOSEFIL leaves open, so it lists its first page as usual."""
    with HoldBatchWindow(syncEngine):
        listed = ExpectOk(await regularClient.get(CARDS_URL))
    assert len(listed["rows"]) == CARD_LIST_PAGE_ROWS


# --- TRANBKP against CT02 ------------------------------------------------------------------------
async def test_backup_holding_window_refuses_transaction_add(
    regularClient: httpx.AsyncClient, syncEngine: Engine, backupOutputDir: Path
) -> None:
    """A backup past its archive read holds the window: CT02 cannot add a row it would lose.

    The add's first window file is CXACAIX (COTRN02C line 600). The archive then holds exactly
    the seeded rows and the table is empty, so no transaction is in neither.
    """
    InsertTransactions(syncEngine, BACKUP_FIRST_SEEDS)
    # The backup exits first, killed if still blocked, and only then is the row lock released.
    with (
        HoldRowLock(syncEngine, FIRST_SEED_ROW_LOCK) as releaseRowLock,
        RunningTranBackup(backupOutputDir) as backup,
    ):
        await WaitUntil(syncEngine, BACKUP_BLOCKED_ON_ROW_LOCK, WAIT_TIMEOUT_SECONDS)
        refused = await regularClient.post(TRANSACTIONS_URL, json=ValidAddBody())
        AssertLegacyError(refused, HTTP_FILE_ERROR, ACCOUNT_XREF_LOOKUP_FAILED_TEXT)
        releaseRowLock()
        AssertProcessSucceeded(await FinishProcess(backup))
    assert ReadArchiveTranIds(backupOutputDir) == set(BACKUP_FIRST_TRAN_IDS)
    assert ReadTranIds(syncEngine) == []


async def test_backup_waits_for_transaction_add_in_flight(
    regularClient: httpx.AsyncClient,
    syncEngine: Engine,
    backupOutputDir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backup started while CT02 is inserting waits for the window and archives the new row.

    The add is paused after its INSERT, inside its write unit, holding the window shared.
    """
    InsertTransactions(syncEngine, (IN_FLIGHT_SEED,))
    gate = Gate(pauseAfterCall=True)
    InstallGate(monkeypatch, TransactionRepository, "InsertTransaction", gate)
    addRequest = regularClient.post(TRANSACTIONS_URL, json=ValidAddBody())
    async with PausedRequest(addRequest, gate) as addTask:
        with RunningTranBackup(backupOutputDir) as backup:
            await WaitUntil(syncEngine, BACKUP_WAITING_FOR_WINDOW, WAIT_TIMEOUT_SECONDS)
            gate.release.set()
            added = ExpectOk(await addTask)
            AssertProcessSucceeded(await FinishProcess(backup))
    AssertAddedMessages([added], TRANSACTION_ADDED_TEMPLATE)
    assert ReadArchiveTranIds(backupOutputDir) == {IN_FLIGHT_SEED.tranId, added["tran_id"]}
    assert ReadTranIds(syncEngine) == []


# --- READ UPDATE record locks and before-image compares ------------------------------------------
async def test_concurrent_account_saves_lock_out_the_second(
    regularClient: httpx.AsyncClient, syncEngine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first CAUP save holds the account and customer locks; the second meets the lock.

    The second save waits the unit's 5 s lock_timeout and answers 423 in CHANGES_FAILED, the
    first then commits, and its credit limit is the one stored.
    """
    MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    firstScreen = await ValidatedScreen(
        regularClient, ACCOUNT_SCREEN, {CREDIT_LIMIT_FIELD: str(FIRST_CREDIT_LIMIT)}
    )
    secondScreen = await ValidatedScreen(
        regularClient, ACCOUNT_SCREEN, {CREDIT_LIMIT_FIELD: str(SECOND_CREDIT_LIMIT)}
    )
    gate = Gate()
    InstallGate(monkeypatch, AccountRepository, "UpdateAccount", gate)
    firstSave = PressSave(regularClient, ACCOUNT_SCREEN, firstScreen)
    async with PausedRequest(firstSave, gate) as firstSaveTask:
        lockedOut = await PressSave(regularClient, ACCOUNT_SCREEN, secondScreen)
        gate.release.set()
        saved = ExpectOk(await firstSaveTask)
    lockedBody = AssertLegacyError(lockedOut, HTTP_RECORD_LOCKED, ACCOUNT_LOCK_TEXT)
    AssertScreenState(lockedBody, FAILED_STATE, CHANGES_FAILED_INFO)
    assert lockedBody["field_errors"] == []
    AssertScreenState(saved, DONE_STATE, CHANGES_COMMITTED_INFO)
    assert ReadAccountAmounts(syncEngine, SAMPLE_ACCOUNT_ID)["credit_limit"] == FIRST_CREDIT_LIMIT


async def test_card_save_meeting_held_lock_answers_record_lock(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """A CCUP save whose READ UPDATE meets a held card lock answers 423 and writes nothing."""
    MakeCardEditable(syncEngine, SAMPLE_CARD_NUMBER)
    validated = await ValidatedScreen(
        regularClient, CARD_SCREEN, {CARD_NAME_FIELD: VALIDATED_CARD_NAME}
    )
    with HoldRowLock(syncEngine, SAMPLE_CARD_ROW_LOCK):
        lockedOut = await PressSave(regularClient, CARD_SCREEN, validated)
    lockedBody = AssertLegacyError(lockedOut, HTTP_RECORD_LOCKED, CARD_LOCK_TEXT)
    AssertScreenState(lockedBody, FAILED_STATE, CHANGES_FAILED_INFO)
    assert ReadCardName(syncEngine, SAMPLE_CARD_NUMBER) == EDITABLE_CARD_NAME


async def test_account_changed_since_fetch_answers_record_changed(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """A CAUP save over a record changed since the fetch answers 409 with the fresh balance."""
    MakeAccountEditable(syncEngine, SAMPLE_ACCOUNT_ID)
    seededLimit = ReadAccountAmounts(syncEngine, SAMPLE_ACCOUNT_ID)["credit_limit"]
    validated = await ValidatedScreen(
        regularClient, ACCOUNT_SCREEN, {CREDIT_LIMIT_FIELD: str(FIRST_CREDIT_LIMIT)}
    )
    RaiseAccountBalance(syncEngine, SAMPLE_ACCOUNT_ID)
    conflict = await PressSave(regularClient, ACCOUNT_SCREEN, validated)
    body = AssertLegacyError(conflict, HTTP_RECORD_CHANGED, RECORD_CHANGED_TEXT)
    stored = ReadAccountAmounts(syncEngine, SAMPLE_ACCOUNT_ID)
    assert body["state"] == SHOW_DETAILS_STATE
    assert body["field_errors"] == []
    assert ScreenAmount(body["fields"][BALANCE_FIELD]) == stored[BALANCE_FIELD]
    assert stored[CREDIT_LIMIT_FIELD] == seededLimit


async def test_card_changed_since_fetch_answers_record_changed(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """A CCUP save over a card renamed since the fetch answers 409 with the fresh name shown."""
    MakeCardEditable(syncEngine, SAMPLE_CARD_NUMBER)
    validated = await ValidatedScreen(
        regularClient, CARD_SCREEN, {CARD_NAME_FIELD: VALIDATED_CARD_NAME}
    )
    RenameCard(syncEngine, SAMPLE_CARD_NUMBER, CONCURRENT_CARD_NAME)
    conflict = await PressSave(regularClient, CARD_SCREEN, validated)
    body = AssertLegacyError(conflict, HTTP_RECORD_CHANGED, RECORD_CHANGED_TEXT)
    assert body["state"] == SHOW_DETAILS_STATE
    # COCRDUPC shows the fetched name upper-cased (cbl/COCRDUPC.cbl lines 1354-1367).
    assert body["fields"][CARD_NAME_FIELD] == CONCURRENT_CARD_NAME.upper()
    assert ReadCardName(syncEngine, SAMPLE_CARD_NUMBER) == CONCURRENT_CARD_NAME


# --- Generated TRAN-IDs (STARTBR HIGH-VALUES / READPREV / ADD 1) ---------------------------------
async def test_parallel_adds_and_payment_get_consecutive_ids(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """Three CT02 adds and one CB00 payment at once get 0000000000000001-0000000000000004."""
    requests = [
        regularClient.post(TRANSACTIONS_URL, json=ValidAddBody())
        for addNumber in range(PARALLEL_ADD_COUNT)
    ]
    requests.append(regularClient.post(BILLPAY_URL, json=dict(CONFIRMED_PAYMENT_BODY)))
    bodies = [ExpectOk(response) for response in await asyncio.gather(*requests)]
    AssertAddedMessages(bodies[:PARALLEL_ADD_COUNT], TRANSACTION_ADDED_TEMPLATE)
    AssertAddedMessages(bodies[PARALLEL_ADD_COUNT:], PAYMENT_SUCCESS_TEMPLATE)
    assert sorted(body["tran_id"] for body in bodies) == list(CONSECUTIVE_TRAN_IDS)
    assert ReadTranIds(syncEngine) == list(CONSECUTIVE_TRAN_IDS)


async def test_next_id_after_highest_wraps_then_collides(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """ADD 1 into 9(16) wraps 9999999999999999 to zeros, and the next add then collides.

    The highest key in EBCDIC order is still 9999999999999999, so the second add allocates
    0000000000000000 again and its WRITE meets the duplicate key: the remaining unique
    violation the allocation lock cannot prevent.
    """
    InsertTransactions(syncEngine, (SeedTransaction(HIGHEST_TRAN_ID),))
    wrapped = ExpectOk(await regularClient.post(TRANSACTIONS_URL, json=ValidAddBody()))
    assert wrapped["message"] == TRANSACTION_ADDED_TEMPLATE.format(tranId=WRAPPED_TRAN_ID)
    collided = await regularClient.post(TRANSACTIONS_URL, json=ValidAddBody())
    AssertLegacyError(collided, HTTP_RECORD_CHANGED, TRAN_ID_EXISTS_TEXT)
    assert ReadTranIds(syncEngine) == [WRAPPED_TRAN_ID, HIGHEST_TRAN_ID]


async def test_next_id_after_combtran_follows_interest_ids(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """After COMBTRAN merges the INTCALC IDs, the next online ID is the highest interest ID + 1."""
    InsertTransactions(syncEngine, COMBINED_SEEDS)
    added = ExpectOk(await regularClient.post(TRANSACTIONS_URL, json=ValidAddBody()))
    assert added["message"] == TRANSACTION_ADDED_TEMPLATE.format(tranId=NEXT_AFTER_INTEREST_ID)
    assert added["tran_id"] == NEXT_AFTER_INTEREST_ID


# --- PausedRequest itself: requests that never pause at the gate ---------------------------------
@pytest.mark.parametrize(("requestOf", "expectedOutcome"), ENDED_REQUESTS)
async def test_paused_request_reports_a_request_ending_before_the_gate(
    regularClient: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    requestOf: Callable[[httpx.AsyncClient], Coroutine[Any, Any, httpx.Response]],
    expectedOutcome: str,
) -> None:
    """A request that ends before its patched call pauses fails the test with how it ended.

    Its exception is described, not raised, the block never runs, and the gate waiter is
    reaped, so no task is left running.
    """
    gate = Gate()
    InstallGate(monkeypatch, TransactionRepository, "InsertTransaction", gate)
    skippedPattern = re.escape(GATE_SKIPPED_MESSAGE.format(outcome=expectedOutcome))
    tasksBefore = asyncio.all_tasks()
    with pytest.raises(pytest.fail.Exception, match=skippedPattern):
        async with PausedRequest(requestOf(regularClient), gate):
            pytest.fail(BLOCK_ENTERED_MESSAGE)
    AssertNoTaskLeft(tasksBefore)


async def test_paused_request_cancels_and_reaps_an_add_hanging_short_of_the_gate(
    regularClient: httpx.AsyncClient, syncEngine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An add hanging at its INSERT, its locks held, is cancelled and reaped before the failure.

    The gate wait and the grace after the release are SHORT_GATE_TIMEOUT_SECONDS. Checked at
    once: the failure reports the timeout, no task is left running and no advisory lock of
    this test database survives.
    """
    hangReached = asyncio.Event()
    monkeypatch.setattr(TransactionRepository, "InsertTransaction", HangingCall(hangReached))
    monkeypatch.setitem(globals(), "GATE_TIMEOUT_SECONDS", SHORT_GATE_TIMEOUT_SECONDS)
    timeoutPattern = re.escape(GATE_TIMEOUT_MESSAGE.format(seconds=SHORT_GATE_TIMEOUT_SECONDS))
    addRequest = regularClient.post(TRANSACTIONS_URL, json=ValidAddBody())
    tasksBefore = asyncio.all_tasks()
    with pytest.raises(pytest.fail.Exception, match=timeoutPattern):
        async with PausedRequest(addRequest, Gate()):
            pytest.fail(BLOCK_ENTERED_MESSAGE)
    assert hangReached.is_set(), HANG_NOT_REACHED_MESSAGE
    AssertNoTaskLeft(tasksBefore)
    assert CountRows(syncEngine, ADVISORY_LOCKS_HELD) == 0


# --- PausedRequest itself: a failure of its block, and its own cancellation ---------------------
async def test_loop_error_recorder_reports_an_unretrieved_task_exception() -> None:
    """RecordedLoopErrors, which the block-failure case relies on, reports such a dropped task."""
    with RecordedLoopErrors() as loopErrors:
        await asyncio.wait({asyncio.create_task(RefusedRequest())})
    assert [report["message"] for report in loopErrors] == [UNRETRIEVED_TASK_EXCEPTION_MESSAGE]


async def test_paused_request_lets_a_block_failure_propagate_and_reaps_the_request() -> None:
    """A failure raised in the block before the request is awaited comes out of it unchanged.

    The request, released by the exit, had ended on its own by then and its exception was
    retrieved: once the collector runs, asyncio reports no unretrieved task exception, and no
    task is left running.
    """
    tasksBefore = asyncio.all_tasks()
    with RecordedLoopErrors() as loopErrors:
        assert await RaiseInsidePausedRequest(Gate()), REQUEST_NOT_ENDED_MESSAGE
    AssertNoTaskLeft(tasksBefore)
    assert not loopErrors, loopErrors


@pytest.mark.parametrize("cancelWhileReaping", [False, True], ids=["awaiting-gate", "reaping"])
async def test_paused_request_reaps_the_add_before_its_own_cancellation_propagates(
    regularClient: httpx.AsyncClient,
    syncEngine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    cancelWhileReaping: bool,
) -> None:
    """One cancellation of PausedRequest ends it only after the paused add has ended.

    The add stops at its INSERT holding the batch window shared and the TRAN-ID allocation
    lock and, once released, holds both for SLOW_RELEASE_SECONDS more. PausedRequest is
    cancelled while it awaits a gate that shares only the add's release, so it never sees the
    add arrive, or while it reaps the add after its block. Checked at once when it ends: it
    ended by that cancellation, no task is left and no advisory lock survives.
    """
    callGate, slowPhase = Gate(), asyncio.Event()
    slowCall = SlowlyReleasedCall(TransactionRepository.InsertTransaction, callGate, slowPhase)
    monkeypatch.setattr(TransactionRepository, "InsertTransaction", slowCall)
    helperGate = callGate if cancelWhileReaping else Gate(release=callGate.release)
    cancelSignal = slowPhase if cancelWhileReaping else callGate.reached
    addRequest = regularClient.post(TRANSACTIONS_URL, json=ValidAddBody())
    tasksBefore = asyncio.all_tasks()
    helperTask = asyncio.create_task(LeavePausedRequestAtOnce(addRequest, helperGate))
    try:
        await asyncio.wait_for(cancelSignal.wait(), GATE_TIMEOUT_SECONDS)
        assert CountRows(syncEngine, ADVISORY_LOCKS_HELD) > 0, LOCKS_NOT_HELD_MESSAGE
    finally:
        helperTask.cancel()
        await asyncio.wait({helperTask}, timeout=REAP_TIMEOUT_SECONDS)
    assert helperTask.cancelled(), HELPER_NOT_CANCELLED_MESSAGE.format(task=helperTask)
    AssertNoTaskLeft(tasksBefore)
    assert CountRows(syncEngine, ADVISORY_LOCKS_HELD) == 0
