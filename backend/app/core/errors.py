"""Legacy error contract: the screen message line, the cursor and the CICS file conditions.

Replaces the BMS ERRMSG line and cursor (``MOVE -1 TO <field>L``), the information line of
COACTUPC/COCRDUPC, and CICS RESP/RESP2 file conditions (e.g. COACTVWC
``WS-FILE-ERROR-MESSAGE``), as AAP 0.6.5 defines them.

Every error body this module builds carries the four standard legacy keys ``message``,
``info_message``, ``field_errors`` and ``cursor_field``. The CAUP and CCUP screens add their
screen keys, the CT00 and CU00 invalid-selection refusals add their list page's ``rows``,
``page``, ``first_key`` and ``last_key`` (CT00 also ``search_cleared``), and the 422 body of a
structurally invalid request adds FastAPI's ``detail`` list (``loc``, ``msg`` and ``type``
only) beside them. A JSON body FastAPI cannot parse at all, such as one that is not valid UTF-8,
gets the same 422 body as malformed JSON. The CSMSG02Y abend work area has no counterpart here:
no catch-all handler is registered, so an unexpected error is FastAPI's 500 and the frontend
shows ``UNEXPECTED ABEND OCCURRED.``.

``LogFileCondition`` replaces the ``DISPLAY 'RESP:' WS-RESP-CD 'REAS:' WS-REAS-CD`` of the
``WHEN OTHER`` branches: every service logs a file condition it answers with a literal through
it, with codes and class names only.

This is a leaf module. It imports only the standard library, FastAPI, Starlette and
``sqlalchemy.exc``, so repositories and batch jobs import it without ``SECRET_KEY`` or a
database.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

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
CICS_RESP_NOTOPEN = 19
CICS_RESP_RECORDBUSY = 101

UNIQUE_VIOLATION_SQLSTATE = "23505"
LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"

# The batch window's NOTOPEN and a record another task holds clear on a retry, so they are
# logged at WARNING; any other condition (IOERR, DUPREC, ...) is a failure, logged at ERROR.
TRANSIENT_FILE_CONDITIONS = frozenset({CICS_RESP_NOTOPEN, CICS_RESP_RECORDBUSY})
FILE_CONDITION_LOG = "Transaction %s %s on %s returned RESP:%d REAS:%d (cause %s, SQLSTATE %s)"
# Logged for the cause and the SQLSTATE of a condition that has none, such as NOTOPEN.
NO_FAILURE_DETAIL = "none"
# The deepest exception chain FailureCauseOf follows, so a cyclic chain still ends.
MAX_CAUSE_DEPTH = 10
# The driver error attributes holding the SQLSTATE: asyncpg and SQLAlchemy's asyncpg adapter
# set sqlstate, psycopg2 sets pgcode.
SQLSTATE_ATTRIBUTES = ("sqlstate", "pgcode")

STANDARD_BODY_KEYS = ("message", "info_message", "field_errors", "cursor_field")
REQUEST_LOCATION_KINDS = frozenset({"body", "query", "path", "header", "cookie"})

# The detail of the HTTP 400 FastAPI raises when reading a JSON body fails other than by a
# JSON decode error: a body that is not valid UTF-8, or one nested too deeply to parse.
FASTAPI_BODY_PARSE_DETAIL = "There was an error parsing the body"
# The validation error such a body is answered with: FastAPI's own malformed-JSON item, without
# the decode position, which a body that cannot be decoded at all does not have.
BODY_PARSE_ERRORS = ({"type": "json_invalid", "loc": ("body",), "msg": "JSON decode error"},)


@dataclass(frozen=True)
class LegacyErrorDetails:
    """The optional parts of a legacy error body.

    ``infoMessage`` is the separate information line of CAVW, CAUP, CCDL and CCUP.
    ``fieldErrors`` lists every failing field in source order, as the all-field edit mode of
    COACTVWC, COACTUPC, COCRDLIC, COCRDSLC and COCRDUPC keeps each failing field's flag.
    ``extraBody`` adds keys beside the four standard ones, such as the CAUP and CCUP screen
    ``fields``, ``state`` and ``screen_token``, or the ``rows``, ``page``, ``first_key`` and
    ``last_key`` of a CT00 or CU00 invalid-selection refusal. Its values must already be
    JSON-ready (for example ``model_dump(mode="json")``): the body is sent without
    ``jsonable_encoder``, which would turn ``Decimal`` amounts into floats.
    ``exactFieldErrors`` declares ``fieldErrors`` the exact failing-field list even when it is
    empty, so a screen whose outcome fails no field, such as the CCUP no-change and write
    outcomes, places the cursor without marking any field. Unset, an empty ``fieldErrors``
    falls back to the cursor field, the contract of the first-failure programs.
    """

    infoMessage: str = ""
    fieldErrors: tuple[str, ...] = ()
    extraBody: Mapping[str, object] | None = None
    exactFieldErrors: bool = False


def ErrorBody(
    message: str, field: str = "", details: LegacyErrorDetails | None = None
) -> dict[str, object]:
    """Build the legacy error body around a message literal, which is passed through unchanged.

    ``field_errors`` is ``details.fieldErrors`` when it names any field or when
    ``details.exactFieldErrors`` is set, otherwise ``[field]`` for a non-blank ``field``,
    otherwise empty. ``cursor_field`` is ``field`` when non-blank, otherwise the first failing
    field, replacing ``MOVE -1 TO <field>L``. Keys of ``details.extraBody`` are added but never
    replace the four standard keys.
    """
    errorDetails = details if details is not None else LegacyErrorDetails()
    hasField = bool(field.strip())
    fieldErrors = list(errorDetails.fieldErrors)
    if not fieldErrors and hasField and not errorDetails.exactFieldErrors:
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
    """A CICS file condition (RESP and RESP2) raised against a legacy file name.

    ``fileName`` is the legacy file the program addressed: the CICS file USRSEC, ACCTDAT,
    CUSTDAT, CARDDAT, CARDAIX, CCXREF, CXACAIX or TRANSACT, or the TRANTYPE or TRANCATG DD name
    of proc/TRANREPT.prc for the CBTRN03C reference reads. The sign-on throttle, whose
    security.login_attempts table replaces no legacy file, uses LOGINATT. Each service catches
    the family and builds its program's literal from it, as the program's
    ``EVALUATE WS-RESP-CD`` did. The exception text holds only the file name and the codes,
    never row data, card numbers or SSNs.
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


def SqlStateAttributeOf(error: BaseException | None) -> str:
    """Return the SQLSTATE a driver error carries, or "" when it carries none.

    SQLAlchemy's asyncpg adapter sets both ``sqlstate`` and ``pgcode``, asyncpg ``sqlstate``
    and psycopg2, used by batch, ``pgcode`` only. Only a non-empty text value counts.
    """
    for attributeName in SQLSTATE_ATTRIBUTES:
        sqlState = getattr(error, attributeName, None)
        if isinstance(sqlState, str) and sqlState:
            return sqlState
    return ""


def SqlStateOf(dbError: DBAPIError) -> str:
    """Return the SQLSTATE of the driver error wrapped by SQLAlchemy, or "" when it has none."""
    return SqlStateAttributeOf(dbError.orig)


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


def NextCauseOf(error: BaseException) -> BaseException | None:
    """Return the exception ``error`` was raised from, else the driver error it wraps, or None.

    ``raise ... from`` sets ``__cause__``; a DBAPIError built without it still holds the driver
    error in ``orig``.
    """
    if error.__cause__ is not None:
        return error.__cause__
    wrappedError = getattr(error, "orig", None)
    return wrappedError if isinstance(wrappedError, BaseException) else None


def FailureCauseOf(error: BaseException) -> tuple[str, str]:
    """Return the class name of the innermost cause of ``error`` and the first SQLSTATE found.

    The chain is followed inward from the cause of ``error`` for at most MAX_CAUSE_DEPTH links,
    so a cyclic chain still ends. An error raised from nothing, such as the batch window's
    NOTOPEN, gives NO_FAILURE_DETAIL for both. Only class names and SQLSTATE codes are read,
    never an exception's text, arguments or traceback, which can echo SQL, bound keys, card
    numbers or SSNs.
    """
    cause = NextCauseOf(error)
    if cause is None:
        return (NO_FAILURE_DETAIL, NO_FAILURE_DETAIL)
    sqlState = SqlStateAttributeOf(cause)
    for _ in range(MAX_CAUSE_DEPTH - 1):
        innerCause = NextCauseOf(cause)
        if innerCause is None:
            break
        cause = innerCause
        sqlState = sqlState or SqlStateAttributeOf(cause)
    return (type(cause).__name__, sqlState or NO_FAILURE_DETAIL)


def LogFileCondition(
    logger: logging.Logger, transactionId: str, operation: str, error: RepositoryError
) -> None:
    """Log, on ``logger``, a file condition that a ``WHEN OTHER`` branch answers with a literal.

    The record names the CICS transaction ID, the operation (the CICS command of the paragraph
    that failed), the legacy file, RESP and RESP2, and the class name and SQLSTATE of
    FailureCauseOf. TRANSIENT_FILE_CONDITIONS (NOTOPEN, RECORDBUSY) are logged at WARNING and
    every other condition at ERROR. Only codes and class names are logged, never exception text
    or record values.
    """
    logLevel = logging.WARNING if error.resp in TRANSIENT_FILE_CONDITIONS else logging.ERROR
    causeName, sqlState = FailureCauseOf(error)
    logger.log(
        logLevel,
        FILE_CONDITION_LOG,
        transactionId,
        operation,
        error.fileName,
        error.resp,
        error.resp2,
        causeName,
        sqlState,
    )


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

    Every request FastAPI's validation rejects arrives here: malformed JSON; a wrong JSON type
    or null; an invalid enum value on a non-screen field (an ``action``, a paging
    ``direction``, the user-fetch ``screen``, the CR00 report type or export format); a missing
    export format; a negative page number, or a CU00 page wider than its PIC 9(08); a list or
    opaque token over its maximum length; and screen text over its BMS field length or outside
    the code-page-037 repertoire, which a 3270 terminal could never send (AAP 0.6.4). The legacy
    type, sign, range and format edits run in the services, never here.

    The body is FastAPI's ``HTTPValidationError`` ``detail`` list, each item holding only
    ``loc``, ``msg`` and ``type``, with the four standard legacy keys beside it. ``message`` and
    ``info_message`` are empty. ``field_errors`` names each failing field as its location names
    it: the snake_case attribute of a body field, or the wire name of a query or path parameter
    (``reportType``, ``format``). A location that names no field, such as a malformed JSON
    body, adds none. ``cursor_field`` is the first named field, or empty when there is none.
    """
    errors = exc.errors()
    fieldDetails = LegacyErrorDetails(fieldErrors=tuple(FieldErrorsOf(errors)))
    content = {"detail": SanitisedDetail(errors), **ErrorBody("", details=fieldDetails)}
    return JSONResponse(status_code=HTTP_VALIDATION_FAILURE, content=content)


def IsBodyParseFailure(exc: StarletteHTTPException) -> bool:
    """Return True only for the HTTP 400 FastAPI raises when it cannot read a JSON body.

    FastAPI's request handler turns a ``json.JSONDecodeError`` into a ``RequestValidationError``
    but every other failure of ``request.json()``, such as the ``UnicodeDecodeError`` of a body
    that is not valid UTF-8 or the ``RecursionError`` of a too deeply nested one, into
    ``HTTPException(400, FASTAPI_BODY_PARSE_DETAIL)`` raised from that error. All three marks
    are required, so a 400 the application raises itself, which has no cause, never matches.
    """
    return (
        exc.status_code == HTTP_EDIT_FAILURE
        and exc.detail == FASTAPI_BODY_PARSE_DETAIL
        and exc.__cause__ is not None
    )


async def HandleHttpException(request: Request, exc: StarletteHTTPException) -> Response:
    """Answer FastAPI's body-parse 400 as malformed JSON; leave every other HTTP error to FastAPI.

    This is not a catch-all: it is registered for ``HTTPException`` alone, which replaces
    FastAPI's default handler for that one class. A JSON body FastAPI cannot read at all gets
    the 422 body of ``HandleRequestValidationError`` for ``BODY_PARSE_ERRORS``, the same as
    malformed JSON, so it carries the four standard legacy keys and echoes none of the body.
    Every other HTTP exception, such as the 404 of an unknown path or of disabled API docs and
    the 405 of an unsupported method with its ``Allow`` header, is passed to FastAPI's own
    ``http_exception_handler`` and answered exactly as it would be without this handler.
    """
    if IsBodyParseFailure(exc):
        bodyParseError = RequestValidationError(BODY_PARSE_ERRORS)
        return await HandleRequestValidationError(request, bodyParseError)
    return await http_exception_handler(request, exc)
