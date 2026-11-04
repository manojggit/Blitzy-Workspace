"""Unit tests for app.core.errors, the legacy error contract of the online API (AAP 0.6.5).

app.core.errors replaces the BMS ERRMSG message line and the cursor placement
``MOVE -1 TO <field>L`` with the error body keys ``message``, ``info_message``,
``field_errors`` and ``cursor_field``, and the CICS RESP and RESP2 file conditions of the
``WHEN OTHER`` branches (cbl/COACTVWC.cbl ``WS-FILE-ERROR-MESSAGE``, lines 86-105, on the files
csd/CARDDEMO.CSD defines) with the RepositoryError family. The CSMSG02Y abend work area
(``ABEND-DATA``) has no counterpart, because no catch-all handler is registered.

These tests pin the body keys and the order of its details; the status HandleLegacyMessageError
sends for each message kind (400 edit failure, 401 sign-on, 403 role or CSRF refusal, 404 not
found, 409 record changed, 423 lock, 429 login lockout, 503 ``WHEN OTHER`` file error); the RESP
and RESP2 codes of each repository error; the SQLSTATE mapping of SqlStateOf and
MapDatabaseError for both drivers; and that the 422 body of HandleRequestValidationError never
echoes the rejected input. The app is built here from the two handlers alone, so no settings or
database are needed.
"""

from typing import NamedTuple

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from app.core.errors import (
    DuplicateKeyError,
    FileIoError,
    HandleLegacyMessageError,
    HandleRequestValidationError,
    LegacyErrorDetails,
    LegacyMessageError,
    MapDatabaseError,
    RecordLockError,
    RecordNotFoundError,
    RepositoryError,
    SqlStateOf,
)

LEGACY_BODY_KEYS = {"message", "info_message", "field_errors", "cursor_field"}
VALIDATION_ITEM_KEYS = {"loc", "msg", "type"}
LEGACY_STATUSES = (400, 401, 403, 404, 409, 423, 429, 503)
HTTP_EDIT_FAILURE = 400
HTTP_UNPROCESSABLE = 422

UNIQUE_VIOLATION = "23505"
LOCK_NOT_AVAILABLE = "55P03"
FOREIGN_KEY_VIOLATION = "23503"
CONNECTION_FAILURE = "08006"
FAILED_STATEMENT = "SELECT 1"

API_BASE_URL = "http://127.0.0.1:8000"
JSON_CONTENT_TYPE = "application/json"
RAISE_PATH = "/raise/{status}"
VALIDATE_PATH = "/validate"

ACCT_ID_FIELD = "acct_id"
# ACCT-ID 9(11), the length of the COACTVW and COACTUP ACCTSID field.
ACCOUNT_ID_LENGTH = 11
OVERLONG_ACCOUNT_ID = "123456789012"
ACCOUNT_NOT_PROVIDED_MESSAGE = "Account number not provided"
ACCOUNT_NOT_FOUND_MESSAGE = "Account ID NOT found..."
OPEN_DATE_MESSAGE = "Open Date : Century is not valid."
UPDATE_INFO_MESSAGE = "Update account details presented above."
# NO-CHANGES-DETECTED fails no field; 3300 only puts the cursor on ACSTTUS.
NO_CHANGES_MESSAGE = "No change detected with respect to values fetched."
STATUS_FIELD = "active_status"
# COACTUPC edits Open Date before Credit Limit, so source order is not alphabetical order.
CAUP_FAILING_FIELDS = ("open_date", "credit_limit")
CAUP_STATE_BODY = {"state": "CHANGES_NOT_OK"}
XREF_AIX_FILE = "CXACAIX"


class AccountKeyRequest(BaseModel):
    """A screen request holding one field, the account key, limited to its BMS length.

    The field is snake_case, the JSON key style of every request schema.
    """

    acct_id: str = Field(default="", max_length=ACCOUNT_ID_LENGTH)


async def RaiseAccountNotFound(status: int) -> None:
    """Raise the CB00 and CT02 account not-found literal with the status the path names."""
    raise LegacyMessageError(ACCOUNT_NOT_FOUND_MESSAGE, ACCT_ID_FIELD, status)


async def EchoAccountKey(accountKey: AccountKeyRequest) -> dict[str, str]:
    """Return the account key of a request that passed validation."""
    return {ACCT_ID_FIELD: accountKey.acct_id}


def BuildErrorApp() -> FastAPI:
    """Return an app holding only the two legacy handlers and the two test routes."""
    errorApp = FastAPI()
    errorApp.add_exception_handler(LegacyMessageError, HandleLegacyMessageError)
    errorApp.add_exception_handler(RequestValidationError, HandleRequestValidationError)
    errorApp.add_api_route(RAISE_PATH, RaiseAccountNotFound, methods=["GET"])
    errorApp.add_api_route(VALIDATE_PATH, EchoAccountKey, methods=["POST"])
    return errorApp


def BuildClient(errorApp: FastAPI) -> httpx.AsyncClient:
    """Return an httpx client that sends its requests to ``errorApp`` in process."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=errorApp), base_url=API_BASE_URL)


class AsyncpgStyleError(Exception):
    """A driver error carrying its SQLSTATE as ``sqlstate``, as asyncpg errors do."""

    def __init__(self, sqlState: str) -> None:
        super().__init__(f"driver error {sqlState}")
        self.sqlstate = sqlState


class Psycopg2StyleError(Exception):
    """A driver error carrying its SQLSTATE as ``pgcode`` only, as psycopg2 errors do."""

    def __init__(self, sqlState: str) -> None:
        super().__init__(f"driver error {sqlState}")
        self.pgcode = sqlState


class MappingCase(NamedTuple):
    """A SQLAlchemy error class, the driver error it wraps and the condition it maps to."""

    dbErrorClass: type[DBAPIError]
    driverErrorClass: type[Exception]
    sqlState: str
    expectedClass: type[RepositoryError]


MAPPING_CASES = [
    MappingCase(IntegrityError, AsyncpgStyleError, UNIQUE_VIOLATION, DuplicateKeyError),
    MappingCase(IntegrityError, Psycopg2StyleError, UNIQUE_VIOLATION, DuplicateKeyError),
    MappingCase(OperationalError, AsyncpgStyleError, LOCK_NOT_AVAILABLE, RecordLockError),
    MappingCase(OperationalError, Psycopg2StyleError, LOCK_NOT_AVAILABLE, RecordLockError),
    MappingCase(OperationalError, AsyncpgStyleError, CONNECTION_FAILURE, FileIoError),
    MappingCase(IntegrityError, AsyncpgStyleError, FOREIGN_KEY_VIOLATION, FileIoError),
]
MAPPING_IDS = [
    "unique-violation-asyncpg-duprec",
    "unique-violation-psycopg2-duprec",
    "lock-not-available-asyncpg-recordbusy",
    "lock-not-available-psycopg2-recordbusy",
    "connection-failure-ioerr",
    "foreign-key-violation-ioerr",
]


class RepositoryCodeCase(NamedTuple):
    """A repository error class, the legacy file it is raised for and its CICS RESP code."""

    errorClass: type[RepositoryError]
    fileName: str
    expectedResp: int


REPOSITORY_CODE_CASES = [
    RepositoryCodeCase(RecordNotFoundError, "ACCTDAT", 13),
    RepositoryCodeCase(DuplicateKeyError, "TRANSACT", 14),
    RepositoryCodeCase(RecordLockError, "USRSEC", 101),
    RepositoryCodeCase(FileIoError, "CUSTDAT", 17),
]
REPOSITORY_CODE_IDS = ["notfnd", "duprec", "recordbusy", "ioerr"]


def WrappedDatabaseError(mappingCase: MappingCase) -> DBAPIError:
    """Return the case's SQLAlchemy error wrapping a driver error with the case's SQLSTATE."""
    driverError = mappingCase.driverErrorClass(mappingCase.sqlState)
    return mappingCase.dbErrorClass(FAILED_STATEMENT, {}, driverError)


def test_body_has_exactly_the_legacy_keys():
    legacyError = LegacyMessageError(ACCOUNT_NOT_PROVIDED_MESSAGE, ACCT_ID_FIELD, HTTP_EDIT_FAILURE)
    errorBody = legacyError.Body()
    assert set(errorBody) == LEGACY_BODY_KEYS
    assert errorBody["message"] == ACCOUNT_NOT_PROVIDED_MESSAGE
    assert errorBody["info_message"] == ""
    # A first-failure error lists its cursor field as its one failing field.
    assert errorBody["field_errors"] == [ACCT_ID_FIELD]
    assert errorBody["cursor_field"] == ACCT_ID_FIELD


def test_exact_empty_field_errors_keep_only_the_cursor():
    details = LegacyErrorDetails(exactFieldErrors=True)
    errorBody = LegacyMessageError(NO_CHANGES_MESSAGE, STATUS_FIELD, details=details).Body()
    assert (errorBody["field_errors"], errorBody["cursor_field"]) == ([], STATUS_FIELD)


def test_body_carries_details_in_order():
    details = LegacyErrorDetails(
        infoMessage=UPDATE_INFO_MESSAGE, fieldErrors=CAUP_FAILING_FIELDS, extraBody=CAUP_STATE_BODY
    )
    errorBody = LegacyMessageError(OPEN_DATE_MESSAGE, details=details).Body()
    assert set(errorBody) == LEGACY_BODY_KEYS | set(CAUP_STATE_BODY)
    assert errorBody["message"] == OPEN_DATE_MESSAGE
    assert errorBody["info_message"] == UPDATE_INFO_MESSAGE
    assert errorBody["field_errors"] == list(CAUP_FAILING_FIELDS)
    assert errorBody["cursor_field"] == CAUP_FAILING_FIELDS[0]
    assert errorBody["state"] == CAUP_STATE_BODY["state"]


def test_extra_body_never_replaces_the_legacy_keys():
    details = LegacyErrorDetails(extraBody={"message": OPEN_DATE_MESSAGE, "cursor_field": "ssn"})
    legacyError = LegacyMessageError(ACCOUNT_NOT_PROVIDED_MESSAGE, ACCT_ID_FIELD, details=details)
    errorBody = legacyError.Body()
    assert errorBody["message"] == ACCOUNT_NOT_PROVIDED_MESSAGE
    assert errorBody["cursor_field"] == ACCT_ID_FIELD


def test_default_status_is_400():
    legacyError = LegacyMessageError(ACCOUNT_NOT_PROVIDED_MESSAGE)
    assert legacyError.httpStatus == HTTP_EDIT_FAILURE
    assert legacyError.field == ""
    assert legacyError.details is None
    assert str(legacyError) == ACCOUNT_NOT_PROVIDED_MESSAGE


@pytest.mark.parametrize("status", LEGACY_STATUSES)
async def test_handler_returns_error_status(status):
    expectedBody = LegacyMessageError(ACCOUNT_NOT_FOUND_MESSAGE, ACCT_ID_FIELD, status).Body()
    async with BuildClient(BuildErrorApp()) as client:
        response = await client.get(RAISE_PATH.format(status=status))
    assert response.status_code == status
    assert response.headers["content-type"] == JSON_CONTENT_TYPE
    assert response.json() == expectedBody


@pytest.mark.parametrize("codeCase", REPOSITORY_CODE_CASES, ids=REPOSITORY_CODE_IDS)
def test_repository_error_codes(codeCase):
    repositoryError = codeCase.errorClass(codeCase.fileName)
    assert isinstance(repositoryError, RepositoryError)
    assert repositoryError.fileName == codeCase.fileName
    assert repositoryError.resp == codeCase.expectedResp
    assert repositoryError.resp2 == 0
    assert str(repositoryError) == f"{codeCase.fileName} RESP {codeCase.expectedResp} RESP2 0"


@pytest.mark.parametrize("driverErrorClass", [AsyncpgStyleError, Psycopg2StyleError])
def test_sql_state_of_reads_both_driver_attributes(driverErrorClass):
    dbError = IntegrityError(FAILED_STATEMENT, {}, driverErrorClass(UNIQUE_VIOLATION))
    assert SqlStateOf(dbError) == UNIQUE_VIOLATION


def test_sql_state_of_is_empty_without_a_driver_code():
    dbError = OperationalError(FAILED_STATEMENT, {}, ConnectionRefusedError())
    assert SqlStateOf(dbError) == ""


@pytest.mark.parametrize("mappingCase", MAPPING_CASES, ids=MAPPING_IDS)
def test_map_database_error(mappingCase):
    mappedError = MapDatabaseError(WrappedDatabaseError(mappingCase), XREF_AIX_FILE)
    assert type(mappedError) is mappingCase.expectedClass
    assert mappedError.fileName == XREF_AIX_FILE


async def test_validation_error_returns_422_without_input_or_ctx():
    async with BuildClient(BuildErrorApp()) as client:
        response = await client.post(VALIDATE_PATH, json={ACCT_ID_FIELD: OVERLONG_ACCOUNT_ID})
    errorBody = response.json()
    assert response.status_code == HTTP_UNPROCESSABLE
    assert set(errorBody) == LEGACY_BODY_KEYS | {"detail"}
    assert [item["loc"] for item in errorBody["detail"]] == [["body", ACCT_ID_FIELD]]
    for item in errorBody["detail"]:
        assert set(item) == VALIDATION_ITEM_KEYS
    assert errorBody["field_errors"] == [ACCT_ID_FIELD]
    assert errorBody["cursor_field"] == ACCT_ID_FIELD
    assert errorBody["message"] == ""
    assert OVERLONG_ACCOUNT_ID not in response.text
