"""Unit tests for the COACTUPC (CAUP) F5 save when a record cannot be locked for update.

F5 on the COACTUP screen in ACUP-CHANGES-OK-NOT-CONFIRMED performs 9600-WRITE-PROCESSING
(cbl/COACTUPC.cbl lines 2602-2615). A non-NORMAL READ UPDATE of ACCTDAT sets
COULD-NOT-LOCK-ACCT-FOR-UPDATE (line 3912), and one of CUSTDAT sets
COULD-NOT-LOCK-CUST-FOR-UPDATE (line 3939); either ends 9600 before any REWRITE. The CAUP save
contract answers both with 423, the lock literal (lines 518, 520), CHANGES_FAILED and the
INFORM-FAILURE information line (line 477). Expected values are typed from those lines:

- a CUSTDAT or ACCTDAT write unit that fails while it is being opened answers its own file's
    lock literal, and no record is read or rewritten;
- an account READ UPDATE answering NOTFND, NOTOPEN (the batch window) or RECORDBUSY answers
    the account literal, and the customer is neither read nor rewritten;
- a customer READ UPDATE answering NOTFND or RECORDBUSY, once the account is locked, answers
    the customer literal, and neither record is rewritten.

app.services.accounts.coactupc's WriteUnit is replaced by a stub yielding one sentinel
connection per file, and its AccountRepository and CustomerRepository by stubs whose READ
UPDATEs and REWRITEs are AsyncMocks, so no database is needed.
"""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType, SimpleNamespace
from typing import NamedTuple
from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings
from app.core.dependencies import OnlineContext
from app.core.errors import FileIoError, LegacyMessageError, RecordLockError, RepositoryError
from app.core.locks import FileNotOpenError
from app.schemas.accounts import AccountUpdateRequest
from app.schemas.common import UpdateAction, UpdateScreenState
from app.services.accounts import coactupc

# LIT-ACCTFILENAME and LIT-CUSTFILENAME.
ACCOUNT_FILE = "ACCTDAT"
CUSTOMER_FILE = "CUSTDAT"
# The COULD-NOT-LOCK-ACCT-FOR-UPDATE and COULD-NOT-LOCK-CUST-FOR-UPDATE texts (lines 517-520).
LOCK_ACCOUNT_MESSAGE = "Could not lock account record for update"
LOCK_CUSTOMER_MESSAGE = "Could not lock customer record for update"
# The INFORM-FAILURE text (lines 476-477), the information line of CHANGES_FAILED.
INFORM_FAILURE_INFO = "Changes unsuccessful. Please try again"
CHANGES_FAILED_STATE = "CHANGES_FAILED"
RECORD_LOCKED_STATUS = 423
# The keys CDEMO-ACCT-ID and CDEMO-CUST-ID the fetched screen carries in its token.
ACCOUNT_ID = "00000000011"
CUSTOMER_ID = "000000011"
ACCOUNT_ID_FIELD = "acct_id"
CUSTOMER_ID_FIELD = "cust_id"
ZERO_AMOUNT_TEXT = "0.00"
# The record an ACCTDAT READ UPDATE locks before CUSTDAT is read; the lock failures end 9600
# before 9700 compares it, so only its key is held.
LOCKED_ACCOUNT_RECORD = MappingProxyType({ACCOUNT_ID_FIELD: ACCOUNT_ID})
# The connection each stubbed write unit yields, by legacy file.
UNIT_CONNECTIONS = MappingProxyType(
    {
        ACCOUNT_FILE: SimpleNamespace(unitFile=ACCOUNT_FILE),
        CUSTOMER_FILE: SimpleNamespace(unitFile=CUSTOMER_FILE),
    }
)
SECRET_KEY_BYTES = 32
DATABASE_PASSWORD_BYTES = 12


class StubWriteUnits:
    """app.core.database.WriteUnit replaced: one sentinel connection per file, entries recorded.

    ``failingFile`` names the file whose unit raises FileIoError while it is being opened, as
    WriteUnit translates a failed connect, BEGIN or ``SET LOCAL lock_timeout``.
    """

    def __init__(self) -> None:
        self.failingFile: str | None = None
        self.enteredFiles: list[str] = []

    @asynccontextmanager
    async def Open(self, engine: object, fileName: str) -> AsyncIterator[SimpleNamespace]:
        """Enter the unit of ``fileName``: fail for ``failingFile``, else yield its connection."""
        self.enteredFiles.append(fileName)
        if fileName == self.failingFile:
            raise FileIoError(fileName)
        yield UNIT_CONNECTIONS[fileName]


class StubAccountRepository:
    """AccountRepository's ACCTDAT READ UPDATE and REWRITE as AsyncMocks; the read locks."""

    def __init__(self) -> None:
        self.GetAccountForUpdate = AsyncMock(return_value=dict(LOCKED_ACCOUNT_RECORD))
        self.UpdateAccount = AsyncMock()

    def ForContext(self, context: OnlineContext) -> "StubAccountRepository":
        """Return this stub as the repository the service builds from its context."""
        return self


class StubCustomerRepository:
    """CustomerRepository's CUSTDAT READ UPDATE and REWRITE as AsyncMocks."""

    def __init__(self) -> None:
        self.GetCustomerForUpdate = AsyncMock()
        self.UpdateCustomer = AsyncMock()

    def ForContext(self, context: OnlineContext) -> "StubCustomerRepository":
        """Return this stub as the repository the service builds from its context."""
        return self


@dataclass(frozen=True)
class StubbedSave:
    """What one F5 runs against: its online context, the write units and both repositories."""

    context: OnlineContext
    writeUnits: StubWriteUnits
    accountRepository: StubAccountRepository
    customerRepository: StubCustomerRepository


class UnitEntryFailure(NamedTuple):
    """The file whose write unit fails to open, the units entered, and the literal answered."""

    failingFile: str
    expectedEnteredFiles: list[str]
    expectedMessage: str


UNIT_ENTRY_FAILURES = [
    UnitEntryFailure(CUSTOMER_FILE, [CUSTOMER_FILE], LOCK_CUSTOMER_MESSAGE),
    UnitEntryFailure(ACCOUNT_FILE, [CUSTOMER_FILE, ACCOUNT_FILE], LOCK_ACCOUNT_MESSAGE),
]
UNIT_ENTRY_FAILURE_IDS = ["CUSTDAT-unit", "ACCTDAT-unit"]
# A READ UPDATE answering NOTFND returns None; any other condition is the error it raises.
ACCOUNT_READ_FAILURES = [None, FileNotOpenError(ACCOUNT_FILE), RecordLockError(ACCOUNT_FILE)]
ACCOUNT_READ_FAILURE_IDS = ["NOTFND", "NOTOPEN", "RECORDBUSY"]
CUSTOMER_READ_FAILURES = [None, RecordLockError(CUSTOMER_FILE)]
CUSTOMER_READ_FAILURE_IDS = ["NOTFND", "RECORDBUSY"]


def BuildDatabaseUrl(driverName: str, databasePassword: str) -> str:
    """Return a PostgreSQL URL on host 127.0.0.1 that no stubbed save ever connects to."""
    return f"postgresql+{driverName}://coactupc_test:{databasePassword}@127.0.0.1/coactupc_test"


def BuildSettings() -> Settings:
    """Return backend Settings with a generated SECRET_KEY for the screen token."""
    databasePassword = secrets.token_urlsafe(DATABASE_PASSWORD_BYTES)
    return Settings(
        _env_file=None,
        SECRET_KEY=secrets.token_urlsafe(SECRET_KEY_BYTES),
        DATABASE_URL=BuildDatabaseUrl("asyncpg", databasePassword),
        SYNC_DATABASE_URL=BuildDatabaseUrl("psycopg2", databasePassword),
    )


def BuildContext() -> OnlineContext:
    """Return an online context whose engine only the stubbed write units receive."""
    appState = SimpleNamespace(engine=SimpleNamespace())
    # The request's own state is where core.locks.RequestEngine looks before app.state.engine.
    request = SimpleNamespace(app=SimpleNamespace(state=appState), state=SimpleNamespace())
    return OnlineContext(request=request, session=SimpleNamespace(), settings=BuildSettings())


@pytest.fixture(name="stubbedSave")
def StubbedSaveFixture(monkeypatch) -> StubbedSave:
    """Make coactupc open the stub write units and build the stub repositories; return them."""
    stubbedSave = StubbedSave(
        BuildContext(), StubWriteUnits(), StubAccountRepository(), StubCustomerRepository()
    )
    monkeypatch.setattr(coactupc, "WriteUnit", stubbedSave.writeUnits.Open)
    monkeypatch.setattr(coactupc, "AccountRepository", stubbedSave.accountRepository.ForContext)
    monkeypatch.setattr(coactupc, "CustomerRepository", stubbedSave.customerRepository.ForContext)
    return stubbedSave


def ConfirmedState() -> coactupc.AccountUpdateState:
    """Return ACUP-CHANGES-OK-NOT-CONFIRMED with blank images and zero amounts.

    The lock failures end 9600 before 9700 compares the before-image or a REWRITE moves the new
    values, so neither is read; the amounts hold values only so the token decodes complete.
    """
    beforeImage = {column: "" for column in coactupc.IMAGE_COLUMNS}
    beforeImage.update({column: ZERO_AMOUNT_TEXT for column in coactupc.AMOUNT_FIELDS})
    beforeImage.update({ACCOUNT_ID_FIELD: ACCOUNT_ID, CUSTOMER_ID_FIELD: CUSTOMER_ID})
    newValues = coactupc.NewAccountData(
        texts={name: "" for name in coactupc.SCREEN_FIELD_LENGTHS},
        amounts={name: Decimal(ZERO_AMOUNT_TEXT) for name in coactupc.AMOUNT_FIELDS},
    )
    return coactupc.AccountUpdateState(
        UpdateScreenState.CHANGES_OK_NOT_CONFIRMED, ACCOUNT_ID, CUSTOMER_ID, beforeImage, newValues
    )


async def PressSave(context: OnlineContext) -> LegacyMessageError:
    """Press F5 on the COACTUP screen holding validated changes; return the error it raises."""
    screenToken = coactupc.EncodeUpdateState(ConfirmedState(), context)
    saveRequest = AccountUpdateRequest(action=UpdateAction.SAVE, screen_token=screenToken)
    with pytest.raises(LegacyMessageError) as raised:
        await coactupc.ProcessAccountUpdate(context, ACCOUNT_ID, saveRequest)
    return raised.value


def FailReadUpdate(readUpdate: AsyncMock, readFailure: RepositoryError | None) -> None:
    """Make a READ UPDATE answer NOTFND when ``readFailure`` is None, else raise it."""
    readUpdate.return_value = None
    readUpdate.side_effect = readFailure


def AssertLockFailureScreen(lockError: LegacyMessageError, expectedMessage: str) -> None:
    """Assert the 423 CHANGES_FAILED screen with the lock literal and INFORM-FAILURE.

    A lock failure sets no field flag, so no field is listed and 3300 WHEN OTHER puts the
    cursor on ACCTSID.
    """
    errorBody = lockError.Body()
    assert lockError.httpStatus == RECORD_LOCKED_STATUS
    assert (lockError.message, errorBody["message"]) == (expectedMessage, expectedMessage)
    assert errorBody["info_message"] == INFORM_FAILURE_INFO
    assert errorBody["state"] == CHANGES_FAILED_STATE
    assert (errorBody["field_errors"], errorBody["cursor_field"]) == ([], ACCOUNT_ID_FIELD)


def AssertNothingRewritten(stubbedSave: StubbedSave) -> None:
    """Assert that neither the ACCTDAT nor the CUSTDAT REWRITE ran."""
    stubbedSave.accountRepository.UpdateAccount.assert_not_awaited()
    stubbedSave.customerRepository.UpdateCustomer.assert_not_awaited()


@pytest.mark.parametrize("unitFailure", UNIT_ENTRY_FAILURES, ids=UNIT_ENTRY_FAILURE_IDS)
async def test_write_unit_failing_to_open_answers_its_file_lock_literal(stubbedSave, unitFailure):
    """A unit that fails while it is being opened is a lock failure on its own file."""
    stubbedSave.writeUnits.failingFile = unitFailure.failingFile
    lockError = await PressSave(stubbedSave.context)
    AssertLockFailureScreen(lockError, unitFailure.expectedMessage)
    assert stubbedSave.writeUnits.enteredFiles == unitFailure.expectedEnteredFiles
    stubbedSave.accountRepository.GetAccountForUpdate.assert_not_awaited()
    stubbedSave.customerRepository.GetCustomerForUpdate.assert_not_awaited()
    AssertNothingRewritten(stubbedSave)


@pytest.mark.parametrize("readFailure", ACCOUNT_READ_FAILURES, ids=ACCOUNT_READ_FAILURE_IDS)
async def test_account_read_update_failure_answers_account_lock_literal(stubbedSave, readFailure):
    """A non-NORMAL ACCTDAT READ UPDATE ends 9600 before CUSTDAT is read (lines 3907-3915)."""
    FailReadUpdate(stubbedSave.accountRepository.GetAccountForUpdate, readFailure)
    lockError = await PressSave(stubbedSave.context)
    AssertLockFailureScreen(lockError, LOCK_ACCOUNT_MESSAGE)
    stubbedSave.accountRepository.GetAccountForUpdate.assert_awaited_once_with(
        UNIT_CONNECTIONS[ACCOUNT_FILE], ACCOUNT_ID
    )
    stubbedSave.customerRepository.GetCustomerForUpdate.assert_not_awaited()
    AssertNothingRewritten(stubbedSave)


@pytest.mark.parametrize("readFailure", CUSTOMER_READ_FAILURES, ids=CUSTOMER_READ_FAILURE_IDS)
async def test_customer_read_update_failure_answers_customer_lock_literal(stubbedSave, readFailure):
    """A non-NORMAL CUSTDAT READ UPDATE after the account lock ends 9600 (lines 3934-3942)."""
    FailReadUpdate(stubbedSave.customerRepository.GetCustomerForUpdate, readFailure)
    lockError = await PressSave(stubbedSave.context)
    AssertLockFailureScreen(lockError, LOCK_CUSTOMER_MESSAGE)
    stubbedSave.accountRepository.GetAccountForUpdate.assert_awaited_once_with(
        UNIT_CONNECTIONS[ACCOUNT_FILE], ACCOUNT_ID
    )
    stubbedSave.customerRepository.GetCustomerForUpdate.assert_awaited_once_with(
        UNIT_CONNECTIONS[CUSTOMER_FILE], CUSTOMER_ID
    )
    AssertNothingRewritten(stubbedSave)
