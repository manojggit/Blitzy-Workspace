"""Legacy error contract: the screen message line, the cursor and the CICS file conditions.

Replaces the BMS ERRMSG line and cursor (``MOVE -1 TO <field>L``), the information line of
COACTUPC/COCRDUPC, and CICS RESP/RESP2 file conditions (e.g. COACTVWC
``WS-FILE-ERROR-MESSAGE``), as AAP 0.6.5 defines them.

Every error body has exactly the keys ``message``, ``info_message``, ``field_errors`` and
``cursor_field``; the CAUP and CCUP screens add their screen keys. The CSMSG02Y abend work area
has no counterpart here: no catch-all handler is registered, so an unexpected error is FastAPI's
500 and the frontend shows ``UNEXPECTED ABEND OCCURRED.``.

This is a leaf module. It imports only the standard library, FastAPI and ``sqlalchemy.exc``, so
repositories and batch jobs import it without ``SECRET_KEY`` or a database.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError

HTTP_EDIT_FAILURE = 400
HTTP_SIGN_ON_FAILURE = 401
HTTP_ACCESS_REFUSED = 403
HTTP_NOT_FOUND = 404
HTTP_RECORD_CHANGED = 409
HTTP_VALIDATION_FAILURE = 422
HTTP_RECORD_LOCKED = 423
HTTP_LOGIN_LOCKED = 429
HTTP_FILE_ERROR = 503

CICS_RESP_NOTFND = 13
CICS_RESP_DUPREC = 14
CICS_RESP_IOERR = 17
CICS_RESP_RECORDBUSY = 101

UNIQUE_VIOLATION_SQLSTATE = "23505"
LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"

STANDARD_BODY_KEYS = ("message", "info_message", "field_errors", "cursor_field")
REQUEST_LOCATION_KINDS = frozenset({"body", "query", "path", "header", "cookie"})


@dataclass(frozen=True)
class LegacyErrorDetails:
    """The optional parts of a legacy error body.

    ``infoMessage`` is the separate information line of CAVW, CAUP, CCDL and CCUP.
    ``fieldErrors`` lists every failing field in source order, as the all-field edit mode of
    COACTVWC, COACTUPC, COCRDLIC, COCRDSLC and COCRDUPC keeps each failing field's flag.
    ``extraBody`` adds keys beside the four standard ones, such as the CAUP and CCUP screen
    ``fields``, ``state`` and ``screen_token``. Its values must already be JSON-ready (for
    example ``model_dump(mode="json")``): the body is sent without ``jsonable_encoder``, which
    would turn ``Decimal`` amounts into floats.
    """

    infoMessage: str = ""
    fieldErrors: tuple[str, ...] = ()
    extraBody: Mapping[str, object] | None = None


def ErrorBody(
    message: str, field: str = "", details: LegacyErrorDetails | None = None
) -> dict[str, object]:
    """Build the legacy error body around a message literal, which is passed through unchanged.

    ``field_errors`` is ``details.fieldErrors`` when it names any field, otherwise ``[field]``
    for a non-blank ``field``, otherwise empty. ``cursor_field`` is ``field`` when non-blank,
    otherwise the first failing field, replacing ``MOVE -1 TO <field>L``. Keys of
    ``details.extraBody`` are added but never replace the four standard keys.
    """
    errorDetails = details if details is not None else LegacyErrorDetails()
    hasField = bool(field.strip())
    fieldErrors = list(errorDetails.fieldErrors)
    if not fieldErrors and hasField:
        fieldErrors = [field]
    body: dict[str, object] = {
        "message": message,
        "info_message": errorDetails.infoMessage or "",
        "field_errors": fieldErrors,
        "cursor_field": field if hasField else next(iter(fieldErrors), ""),
    }
    for key, value in (errorDetails.extraBody or {}).items():
        if key not in STANDARD_BODY_KEYS:
            body[key] = value
    return body


class LegacyMessageError(Exception):
    """A legacy program's message literal, returned to the client as the error body.

    HTTP status by message kind (AAP 0.6.5):

    - 400 ``HTTP_EDIT_FAILURE``: field edit failures (the default).
    - 401 ``HTTP_SIGN_ON_FAILURE``: sign-on failures and a missing or invalid session.
    - 403 ``HTTP_ACCESS_REFUSED``: role refusals and the CSRF origin guard.
    - 404 ``HTTP_NOT_FOUND``: not-found literals.
    - 409 ``HTTP_RECORD_CHANGED``: ``Record changed by some one else. Please review``.
    - 423 ``HTTP_RECORD_LOCKED``: the lock literals.
    - 429 ``HTTP_LOGIN_LOCKED``: the login lockout.
    - 503 ``HTTP_FILE_ERROR``: ``WHEN OTHER`` file-error literals, batch-window refusals included.
    """

    def __init__(
        self,
        message: str,
        field: str = "",
        httpStatus: int = HTTP_EDIT_FAILURE,
        details: LegacyErrorDetails | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.httpStatus = httpStatus
        self.details = details

    def Body(self) -> dict[str, object]:
        """Return the error body: the four standard keys plus any ``extraBody`` keys."""
        return ErrorBody(self.message, self.field, self.details)


class RepositoryError(Exception):
    """A CICS file condition (RESP and RESP2) raised against a legacy CICS file name.

    ``fileName`` is the file the program addressed: USRSEC, ACCTDAT, CUSTDAT, CARDDAT, CARDAIX,
    CCXREF, CXACAIX or TRANSACT. Each service catches the family and builds its program's
    literal from it, as the program's ``EVALUATE WS-RESP-CD`` did. The exception text holds only
    the file name and the codes, never row data, card numbers or SSNs.
    """

    def __init__(self, fileName: str, resp: int, resp2: int = 0) -> None:
        super().__init__(f"{fileName} RESP {resp} RESP2 {resp2}")
        self.fileName = fileName
        self.resp = resp
        self.resp2 = resp2


class RecordNotFoundError(RepositoryError):
    """CICS NOTFND (RESP 13, RESP2 0): no record has the requested key."""

    def __init__(self, fileName: str) -> None:
        super().__init__(fileName, CICS_RESP_NOTFND, 0)


class DuplicateKeyError(RepositoryError):
    """CICS DUPREC (RESP 14, RESP2 0): the key exists already (SQLSTATE 23505)."""

    def __init__(self, fileName: str) -> None:
        super().__init__(fileName, CICS_RESP_DUPREC, 0)


class RecordLockError(RepositoryError):
    """CICS RECORDBUSY (RESP 101, RESP2 0): the record is locked (SQLSTATE 55P03).

    PostgreSQL raises 55P03 (lock_not_available) when ``lock_timeout`` ends a wait in
    ``SELECT ... FOR UPDATE``.
    """

    def __init__(self, fileName: str) -> None:
        super().__init__(fileName, CICS_RESP_RECORDBUSY, 0)


class FileIoError(RepositoryError):
    """CICS IOERR (RESP 17, RESP2 0): any other database or connection failure."""

    def __init__(self, fileName: str) -> None:
        super().__init__(fileName, CICS_RESP_IOERR, 0)


def SqlStateOf(dbError: DBAPIError) -> str:
    """Return the SQLSTATE of the driver error wrapped by SQLAlchemy, or "" when it has none.

    SQLAlchemy's asyncpg adapter sets both ``sqlstate`` and ``pgcode`` on ``orig``; psycopg2,
    used by batch, sets ``pgcode`` only.
    """
    driverError = dbError.orig
    return getattr(driverError, "sqlstate", None) or getattr(driverError, "pgcode", None) or ""


def MapDatabaseError(dbError: DBAPIError, fileName: str) -> RepositoryError:
    """Translate a database error into the CICS file condition the legacy program would see.

    Callers ``raise MapDatabaseError(exc, fileName) from exc``. 23505 gives DUPREC, 55P03 gives
    RECORDBUSY, and every other SQLSTATE, or none, gives IOERR.
    """
    sqlState = SqlStateOf(dbError)
    if sqlState == UNIQUE_VIOLATION_SQLSTATE:
        return DuplicateKeyError(fileName)
    if sqlState == LOCK_NOT_AVAILABLE_SQLSTATE:
        return RecordLockError(fileName)
    return FileIoError(fileName)


async def HandleLegacyMessageError(request: Request, exc: LegacyMessageError) -> JSONResponse:
    """Send a ``LegacyMessageError`` as its body, with the status it carries."""
    return JSONResponse(status_code=exc.httpStatus, content=exc.Body())


def FieldNameOf(location: Sequence[Any]) -> str:
    """Return the field a validation error location names, or "" when it names none.

    The field is the last string element after the location kind: ``("body", "password")``
    gives ``password`` and ``("body", "rows", 0)`` gives ``rows``, while ``("body",)`` and the
    JSON decode position ``("body", 7)`` give "".
    """
    hasKind = bool(location) and location[0] in REQUEST_LOCATION_KINDS
    fieldParts = location[1:] if hasKind else location
    fieldNames = [part for part in fieldParts if isinstance(part, str)]
    return fieldNames[-1] if fieldNames else ""


def FieldErrorsOf(errors: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return the fields the validation errors name, de-duplicated, in error order."""
    fieldErrors: list[str] = []
    for error in errors:
        fieldName = FieldNameOf(error["loc"])
        if fieldName and fieldName not in fieldErrors:
            fieldErrors.append(fieldName)
    return fieldErrors


def SanitisedDetail(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, object]]:
    """Keep only ``loc``, ``msg`` and ``type`` of each validation error.

    Pydantic's ``input``, ``ctx`` and ``url`` keys would echo the rejected value, which may be a
    password, card number or SSN, so they are dropped. The items still satisfy FastAPI's
    ``ValidationError`` schema, whose required keys are exactly these three.
    """
    return [
        {"loc": list(error["loc"]), "msg": str(error["msg"]), "type": str(error["type"])}
        for error in errors
    ]


async def HandleRequestValidationError(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Answer a structurally malformed request with 422 and a body that echoes no input.

    Only the BMS field-length and character-repertoire limits fail here (AAP 0.6.4), which a
    3270 terminal could never send; the legacy edits run in the services. The body is FastAPI's
    ``HTTPValidationError`` (``detail``) with the four legacy keys beside it.
    """
    errors = exc.errors()
    fieldDetails = LegacyErrorDetails(fieldErrors=tuple(FieldErrorsOf(errors)))
    content = {"detail": SanitisedDetail(errors), **ErrorBody("", details=fieldDetails)}
    return JSONResponse(status_code=HTTP_VALIDATION_FAILURE, content=content)
