"""Unit tests for app.core.logging, the stdout logging that masks card numbers.

app.core.logging replaces the CICS region and JES SYSOUT logs of CardDemo. It guarantees that no
raw card number, copybook CVACT02Y CARD-NUM PIC X(16) of file CARDDAT (csd/CARDDEMO.CSD),
reaches any log, Uvicorn's access log of /api/v1/cards/{cardNum} and cardNum= requests included
(Technical Specification, Section 0.6.5, Logging). These tests emit a uvicorn.access record for a
raw-PAN request line and assert the masked output, pin MaskPanRuns, and check that
ConfigureLogging installs one masking root handler, gives Uvicorn's handlers the timestamped
LOG_LINE_FORMAT, and changes nothing when called again. They also check that the traceback of a
stored value failing a response or screen model, as Pydantic or FastAPI raises it, reaches the
log with the value redacted.

No database is used. Each test that configures logging runs under the isolatedLogging fixture,
which restores the root and Uvicorn loggers afterwards, so later tests log as before. The
SuppressEngineStatementLogging tests build pools that never reach a database, under the
isolatedSqlalchemyLogging fixture, and check that the engine and pool loggers stay off below
WARNING with "sqlalchemy" at DEBUG, and that a pool's error about a stand-in psycopg2
connection still reaches the log with the connection's libpq dsn redacted.
"""

import io
import logging
import re
import secrets
import sys
from dataclasses import dataclass

import pytest
from fastapi.exceptions import ResponseValidationError
from psycopg2.extensions import make_dsn
from pydantic import BaseModel, ValidationError
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool, QueuePool
from uvicorn.logging import AccessFormatter

from app.core.logging import (
    HANDLER_NAME,
    LOG_LINE_FORMAT,
    REDACTED_REPLACEMENT,
    SQLALCHEMY_ENGINE_LOGGER,
    SQLALCHEMY_POOL_LOGGER,
    UVICORN_LOGGERS,
    ConfigureLogging,
    MaskPanRuns,
    MaskSensitiveText,
    PanMaskingFilter,
    SuppressEngineStatementLogging,
)
from app.schemas.accounts import AccountScreenFields, AccountViewResponse

# Two card numbers of the sample CARDDATA image (data/ASCII/carddata.txt), and their masks.
SAMPLE_PAN = "4859452612877065"
MASKED_PAN = "************7065"
SECOND_SAMPLE_PAN = "2988091353094312"
MASKED_SECOND_PAN = "************4312"
PAN_REQUEST_PATH = f"/api/v1/cards/{SAMPLE_PAN}?cardNum={SAMPLE_PAN}"
MASKED_REQUEST_PATH = f"/api/v1/cards/{MASKED_PAN}?cardNum={MASKED_PAN}"

# Uvicorn 0.34.0 logs every response with this message and a 5-tuple of args (client address,
# method, full path, HTTP version, status code), which its AccessFormatter unpacks.
ACCESS_LOGGER_NAME = "uvicorn.access"
ACCESS_FORMAT = '%(client_addr)s - "%(request_line)s" %(status_code)s'
ACCESS_MESSAGE = '%s - "%s %s HTTP/%s" %d'
CLIENT_ADDRESS = "127.0.0.1:50000"
REQUEST_METHOD = "GET"
HTTP_VERSION = "1.1"
HTTP_STATUS_OK = 200
ACCESS_ARGUMENT_COUNT = 5
PATH_ARGUMENT_INDEX = 2
STATUS_ARGUMENT_INDEX = 4

APPLICATION_LOGGER_NAME = "app.services.cards.cocrdslc"
CARD_MESSAGE = "card %s"

# Uvicorn 0.34.0's LOGGING_CONFIG writes the "uvicorn" and uvicorn.access lines with a level
# prefix and no time; this format stands in for its DefaultFormatter and AccessFormatter.
UVICORN_DEFAULT_FORMAT = "%(levelname)s:     %(message)s"
SERVER_LOGGER_NAME = "uvicorn"
ERROR_LOGGER_NAME = "uvicorn.error"
STARTUP_MESSAGE = "Application startup complete."
# The asctime that LOG_LINE_FORMAT writes first, as logging.Formatter renders it by default.
LOG_TIMESTAMP_PATTERN = r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} "
SERVER_LINE_PATTERN = re.compile(
    LOG_TIMESTAMP_PATTERN + re.escape(f"INFO {ERROR_LOGGER_NAME} {STARTUP_MESSAGE}")
)
MASKED_ACCESS_LINE = (
    f'INFO {ACCESS_LOGGER_NAME} {CLIENT_ADDRESS} - "{REQUEST_METHOD} {MASKED_REQUEST_PATH}'
    f' HTTP/{HTTP_VERSION}" {HTTP_STATUS_OK}'
)
ACCESS_LINE_PATTERN = re.compile(LOG_TIMESTAMP_PATTERN + re.escape(MASKED_ACCESS_LINE))


@dataclass(frozen=True)
class LoggerSnapshot:
    """The handlers, filters, level and propagation of one logger."""

    logger: logging.Logger
    handlers: tuple[logging.Handler, ...]
    filters: tuple[object, ...]
    level: int
    propagate: bool


@dataclass(frozen=True)
class HandlerSnapshot:
    """The filters and formatter of one Uvicorn handler, and the stderr stream it moves off."""

    handler: logging.Handler
    filters: tuple[object, ...]
    formatter: logging.Formatter | None
    stderrStream: object | None


def SnapshotLogger(logger: logging.Logger) -> LoggerSnapshot:
    """Return the state of a logger that ConfigureLogging may change."""
    return LoggerSnapshot(
        logger=logger,
        handlers=tuple(logger.handlers),
        filters=tuple(logger.filters),
        level=logger.level,
        propagate=logger.propagate,
    )


def RestoreLogger(snapshot: LoggerSnapshot) -> None:
    """Put a logger back in the state SnapshotLogger recorded."""
    snapshot.logger.handlers = list(snapshot.handlers)
    snapshot.logger.filters = list(snapshot.filters)
    snapshot.logger.setLevel(snapshot.level)
    snapshot.logger.propagate = snapshot.propagate


def SnapshotHandler(handler: logging.Handler) -> HandlerSnapshot:
    """Return a handler's filters and formatter, and its stream when that is stderr.

    ConfigureLogging moves only stderr streams to stdout, so no other stream is recorded. That
    keeps the restore off pytest's capture handlers, whose streams pytest swaps every phase.
    """
    stream = handler.stream if isinstance(handler, logging.StreamHandler) else None
    isStderr = stream is not None and (stream is sys.stderr or stream is sys.__stderr__)
    stderrStream = stream if isStderr else None
    return HandlerSnapshot(
        handler=handler,
        filters=tuple(handler.filters),
        formatter=handler.formatter,
        stderrStream=stderrStream,
    )


def RestoreHandler(snapshot: HandlerSnapshot) -> None:
    """Put a handler's filters and formatter back, and the stderr stream moved to stdout."""
    snapshot.handler.filters = list(snapshot.filters)
    snapshot.handler.setFormatter(snapshot.formatter)
    if isinstance(snapshot.handler, logging.StreamHandler) and snapshot.stderrStream is not None:
        snapshot.handler.setStream(snapshot.stderrStream)


def MaskingFiltersOf(filterer: logging.Filterer) -> list[PanMaskingFilter]:
    """Return the PanMaskingFilter instances among a logger's or handler's filters."""
    return [existing for existing in filterer.filters if isinstance(existing, PanMaskingFilter)]


def ResetToUnconfigured(uvicornLoggers: list[logging.Logger]) -> None:
    """Remove what an earlier ConfigureLogging left, and make Uvicorn's loggers propagate.

    Building the app (main.CreateApp) configures logging for the rest of the session, so a test
    that counts what ConfigureLogging adds starts from an unconfigured state instead. pytest
    attaches its capture handlers to every non-propagating logger as each test phase starts;
    with Uvicorn's loggers propagating, ConfigureLogging never adds its filter to those handlers.
    """
    rootLogger = logging.getLogger()
    rootLogger.handlers = [
        handler for handler in rootLogger.handlers if handler.name != HANDLER_NAME
    ]
    for uvicornLogger in uvicornLoggers:
        uvicornLogger.propagate = True
        uvicornLogger.filters = [
            existing
            for existing in uvicornLogger.filters
            if not isinstance(existing, PanMaskingFilter)
        ]


@pytest.fixture(name="isolatedLogging")
def IsolatedLogging():
    """Run a test with no CardDemo masking installed, and restore every logger it touches.

    The root logger and Uvicorn's loggers get back their handlers, filters, levels and
    propagation, and Uvicorn's handlers their filters, formatters and stderr streams.
    """
    uvicornLoggers = [logging.getLogger(loggerName) for loggerName in UVICORN_LOGGERS]
    loggerSnapshots = [SnapshotLogger(logger) for logger in (logging.getLogger(), *uvicornLoggers)]
    handlerSnapshots = [
        SnapshotHandler(handler) for logger in uvicornLoggers for handler in logger.handlers
    ]
    ResetToUnconfigured(uvicornLoggers)
    yield
    for loggerSnapshot in loggerSnapshots:
        RestoreLogger(loggerSnapshot)
    for handlerSnapshot in handlerSnapshots:
        RestoreHandler(handlerSnapshot)


class RecordCollector(logging.Handler):
    """Keeps every record it receives, so a test can inspect the args a formatter unpacks."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    # logging calls this exact name, so it stays lower-case: a framework-mandated
    # exception to the MK_Modernization Rule's PascalCase method naming.
    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def AttachCapture(logger: logging.Logger) -> tuple[io.StringIO, list[logging.LogRecord]]:
    """Attach an access-formatted stream handler and a RecordCollector to a logger.

    Returns the stream the formatted lines go to and the list the records go to. Neither
    handler carries a filter, so only the filters on the logger itself can mask what they get.
    """
    capturedOutput = io.StringIO()
    accessHandler = logging.StreamHandler(capturedOutput)
    accessHandler.setFormatter(AccessFormatter(ACCESS_FORMAT, use_colors=False))
    recordCollector = RecordCollector()
    logger.addHandler(accessHandler)
    logger.addHandler(recordCollector)
    return capturedOutput, recordCollector.records


def EmitAccessRecord(accessLogger: logging.Logger, fullPath: str) -> None:
    """Log one 200 response for a GET of fullPath exactly as Uvicorn 0.34.0 logs it."""
    accessLogger.info(
        ACCESS_MESSAGE, CLIENT_ADDRESS, REQUEST_METHOD, fullPath, HTTP_VERSION, HTTP_STATUS_OK
    )


@pytest.mark.usefixtures("isolatedLogging")
def test_access_log_masks_pan_in_path_and_query():
    accessLogger = logging.getLogger(ACCESS_LOGGER_NAME)
    # Uvicorn's default LOGGING_CONFIG makes uvicorn.access non-propagating before the app
    # configures logging, so only the filter on the logger itself guards the access log.
    accessLogger.propagate = False
    ConfigureLogging()
    accessLogger.setLevel(logging.INFO)
    capturedOutput, collectedRecords = AttachCapture(accessLogger)
    EmitAccessRecord(accessLogger, PAN_REQUEST_PATH)
    output = capturedOutput.getvalue()
    assert output.count(MASKED_PAN) == 2
    assert SAMPLE_PAN not in output
    assert len(collectedRecords) == 1
    accessArgs = collectedRecords[0].args
    assert isinstance(accessArgs, tuple)
    assert len(accessArgs) == ACCESS_ARGUMENT_COUNT
    assert accessArgs[PATH_ARGUMENT_INDEX] == MASKED_REQUEST_PATH
    assert accessArgs[STATUS_ARGUMENT_INDEX] == HTTP_STATUS_OK


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(SAMPLE_PAN, MASKED_PAN, id="sixteen-digits"),
        pytest.param(f"cardNum={SAMPLE_PAN}&x=1", f"cardNum={MASKED_PAN}&x=1", id="query-value"),
        pytest.param(
            f"{SAMPLE_PAN}/{SECOND_SAMPLE_PAN}",
            f"{MASKED_PAN}/{MASKED_SECOND_PAN}",
            id="two-card-numbers",
        ),
        pytest.param("485945261287706", "485945261287706", id="fifteen-digits"),
        pytest.param("48594526128770651", "48594526128770651", id="seventeen-digits"),
        pytest.param("GET /api/v1/menu/main", "GET /api/v1/menu/main", id="no-digits"),
    ],
)
def test_mask_pan_runs(text, expected):
    assert MaskPanRuns(text) == expected


# Stored values the online path cannot write: a first name outside the code-page-037 repertoire
# of AccountScreenFields, and a FICO score above the 999 of AccountViewResponse. The score has
# six digits so that no traceback line number can hold it.
STORED_FIRST_NAME = "Larry€"
STORED_FICO_SCORE = 918273
# Uvicorn logs an exception that leaves the application, FastAPI's unexpected 500, this way.
SERVER_ERROR_MESSAGE = "Exception in ASGI application"
# FastAPI opens the loc of every response validation error with "response".
RESPONSE_LOCATION = ("response",)


@dataclass(frozen=True)
class StoredValueCase:
    """A stored value that fails a response or screen model, and the error text it must not leave.

    echoedText is what only the echoed value puts in the traceback; errorType and errorField
    are the Pydantic error type and the field its error names, which the log keeps.
    """

    model: type[BaseModel]
    storedFields: dict[str, object]
    echoedText: str
    errorType: str
    errorField: str


FIRST_NAME_CASE = StoredValueCase(
    model=AccountScreenFields,
    storedFields={"first_name": STORED_FIRST_NAME},
    echoedText=STORED_FIRST_NAME,
    errorType="value_error",
    errorField="first_name",
)
FICO_SCORE_CASE = StoredValueCase(
    model=AccountViewResponse,
    storedFields={"fico_credit_score": STORED_FICO_SCORE},
    echoedText=str(STORED_FICO_SCORE),
    errorType="less_than_equal",
    errorField="fico_credit_score",
)


def ResponseErrorOf(validationError: ValidationError) -> ResponseValidationError:
    """Return a model's ValidationError as FastAPI's ResponseValidationError reports it."""
    responseErrors = [
        {**error, "loc": (*RESPONSE_LOCATION, *error["loc"])}
        for error in validationError.errors(include_url=False)
    ]
    return ResponseValidationError(errors=responseErrors)


def RaiseStoredValueError(case: StoredValueCase, raisedByFastApi: bool) -> None:
    """Build the case's model from its stored fields, as a service or FastAPI would build it.

    A service builds the model itself and raises Pydantic's ValidationError. FastAPI validates
    a route's return value against its response_model and raises ResponseValidationError.
    """
    try:
        case.model(**case.storedFields)
    except ValidationError as validationError:
        if raisedByFastApi:
            raise ResponseErrorOf(validationError) from None
        raise


def BuildServerErrorRecord(case: StoredValueCase, raisedByFastApi: bool) -> logging.LogRecord:
    """Return the uvicorn.error record of the unexpected 500, with the raised error's traceback."""
    with pytest.raises((ValidationError, ResponseValidationError)) as raisedError:
        RaiseStoredValueError(case, raisedByFastApi)
    return logging.LogRecord(
        name=ERROR_LOGGER_NAME,
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=SERVER_ERROR_MESSAGE,
        args=None,
        exc_info=(raisedError.type, raisedError.value, raisedError.tb),
    )


@pytest.mark.parametrize("raisedByFastApi", [False, True], ids=["pydantic", "fastapi-response"])
@pytest.mark.parametrize(
    "case", [FIRST_NAME_CASE, FICO_SCORE_CASE], ids=["screen-first-name", "view-fico-score"]
)
def test_server_error_traceback_redacts_stored_values(case, raisedByFastApi):
    serverErrorRecord = BuildServerErrorRecord(case, raisedByFastApi)
    rawTraceback = logging.Formatter().formatException(serverErrorRecord.exc_info)
    # The unmasked traceback echoes the stored value, so the case exercises the redaction.
    assert case.echoedText in rawTraceback
    maskingFilter = PanMaskingFilter()
    assert maskingFilter.filter(serverErrorRecord) is True
    loggedText = logging.Formatter().format(serverErrorRecord)
    assert case.echoedText not in loggedText
    assert REDACTED_REPLACEMENT in loggedText
    assert case.errorType in loggedText
    assert case.errorField in loggedText
    maskedTraceback = serverErrorRecord.exc_text
    maskingFilter.filter(serverErrorRecord)
    assert serverErrorRecord.exc_text == maskedTraceback


def BuildCardRecord() -> logging.LogRecord:
    """Return an application record whose only argument is a raw card number."""
    return logging.LogRecord(
        name=APPLICATION_LOGGER_NAME,
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=CARD_MESSAGE,
        args=(SAMPLE_PAN,),
        exc_info=None,
    )


@pytest.mark.usefixtures("isolatedLogging")
def test_root_handler_carries_masking_filter():
    ConfigureLogging()
    rootLogger = logging.getLogger()
    maskingHandlers = [handler for handler in rootLogger.handlers if MaskingFiltersOf(handler)]
    assert len(maskingHandlers) == 1
    stdoutHandler = maskingHandlers[0]
    # First among the root handlers, so every other one receives records already masked.
    assert rootLogger.handlers[0] is stdoutHandler
    assert rootLogger.level == logging.INFO
    assert isinstance(stdoutHandler, logging.StreamHandler)
    assert stdoutHandler.stream is sys.stdout
    [maskingFilter] = MaskingFiltersOf(stdoutHandler)
    cardRecord = BuildCardRecord()
    assert maskingFilter.filter(cardRecord) is True
    assert cardRecord.getMessage() == CARD_MESSAGE % MASKED_PAN


@pytest.mark.usefixtures("isolatedLogging")
def test_configure_logging_is_idempotent():
    rootLogger = logging.getLogger()
    accessLogger = logging.getLogger(ACCESS_LOGGER_NAME)
    rootHandlerCountBefore = len(rootLogger.handlers)
    ConfigureLogging()
    countsAfterFirstCall = (len(rootLogger.handlers), len(MaskingFiltersOf(accessLogger)))
    ConfigureLogging()
    countsAfterSecondCall = (len(rootLogger.handlers), len(MaskingFiltersOf(accessLogger)))
    assert countsAfterFirstCall == (rootHandlerCountBefore + 1, 1)
    assert countsAfterSecondCall == countsAfterFirstCall


def AttachUvicornHandler(logger: logging.Logger) -> io.StringIO:
    """Give a logger a handler as Uvicorn's default configuration does: no time, no filter.

    The logger stops propagating and logs at INFO, as Uvicorn 0.34.0's LOGGING_CONFIG sets the
    "uvicorn" and uvicorn.access loggers. Returns the stream the handler writes to.
    """
    capturedOutput = io.StringIO()
    uvicornHandler = logging.StreamHandler(capturedOutput)
    uvicornHandler.setFormatter(logging.Formatter(UVICORN_DEFAULT_FORMAT))
    logger.addHandler(uvicornHandler)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return capturedOutput


def EmitUvicornLines(accessLogger: logging.Logger) -> None:
    """Log Uvicorn's startup-complete line on uvicorn.error and one raw-PAN access line."""
    logging.getLogger(ERROR_LOGGER_NAME).info(STARTUP_MESSAGE)
    EmitAccessRecord(accessLogger, PAN_REQUEST_PATH)


@pytest.mark.usefixtures("isolatedLogging")
def test_uvicorn_handlers_write_the_timestamped_line_format():
    serverLogger = logging.getLogger(SERVER_LOGGER_NAME)
    accessLogger = logging.getLogger(ACCESS_LOGGER_NAME)
    serverOutput = AttachUvicornHandler(serverLogger)
    accessOutput = AttachUvicornHandler(accessLogger)
    ConfigureLogging()
    EmitUvicornLines(accessLogger)
    ConfigureLogging()
    EmitUvicornLines(accessLogger)
    uvicornHandlers = [*serverLogger.handlers, *accessLogger.handlers]
    probeRecord = BuildCardRecord()
    probeLines = [handler.formatter.format(probeRecord) for handler in uvicornHandlers]
    assert probeLines == [logging.Formatter(LOG_LINE_FORMAT).format(probeRecord)] * 2
    assert [len(MaskingFiltersOf(handler)) for handler in uvicornHandlers] == [1, 1]
    serverLines = serverOutput.getvalue().splitlines()
    accessLines = accessOutput.getvalue().splitlines()
    assert [bool(SERVER_LINE_PATTERN.fullmatch(line)) for line in serverLines] == [True, True]
    assert [bool(ACCESS_LINE_PATTERN.fullmatch(line)) for line in accessLines] == [True, True]


@pytest.mark.usefixtures("isolatedLogging")
def test_stdout_handler_follows_a_replaced_and_closed_stdout(monkeypatch):
    # Click's CliRunner swaps in and closes a stdout for every in-process batch command.
    ConfigureLogging()
    applicationLogger = logging.getLogger(APPLICATION_LOGGER_NAME)
    firstStdout, laterStdout, stderrOutput = io.StringIO(), io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderrOutput)
    monkeypatch.setattr(sys, "stdout", firstStdout)
    applicationLogger.info(CARD_MESSAGE, SAMPLE_PAN)
    firstOutput = firstStdout.getvalue()
    firstStdout.close()
    monkeypatch.setattr(sys, "stdout", laterStdout)
    applicationLogger.info(CARD_MESSAGE, SECOND_SAMPLE_PAN)
    assert MASKED_PAN in firstOutput
    assert MASKED_SECOND_PAN in laterStdout.getvalue()
    assert SECOND_SAMPLE_PAN not in laterStdout.getvalue()
    assert stderrOutput.getvalue() == ""


# The logger above every SQLAlchemy logger; an operator lowers it to see SQLAlchemy's DEBUG.
SQLALCHEMY_ROOT_LOGGER = "sqlalchemy"
SUPPRESSED_LOGGER_NAMES = (SQLALCHEMY_ENGINE_LOGGER, SQLALCHEMY_POOL_LOGGER)


def RefuseConnection() -> None:
    """Serve as the DBAPI connect function of a pool these tests build but never connect."""
    raise AssertionError("a logging unit test opened a database connection")


@pytest.fixture(name="isolatedSqlalchemyLogging")
def IsolatedSqlalchemyLogging():
    """Run a test with "sqlalchemy" at DEBUG and the engine and pool loggers unset.

    That is the state an operator's DEBUG configuration leaves before an engine is built. The
    three loggers get back their handlers, filters, levels and propagation afterwards.
    """
    loggerNames = (SQLALCHEMY_ROOT_LOGGER, *SUPPRESSED_LOGGER_NAMES)
    loggerSnapshots = [SnapshotLogger(logging.getLogger(loggerName)) for loggerName in loggerNames]
    logging.getLogger(SQLALCHEMY_ROOT_LOGGER).setLevel(logging.DEBUG)
    for loggerName in SUPPRESSED_LOGGER_NAMES:
        logging.getLogger(loggerName).setLevel(logging.NOTSET)
    yield
    for loggerSnapshot in loggerSnapshots:
        RestoreLogger(loggerSnapshot)


@pytest.mark.parametrize(
    "poolClass",
    [
        pytest.param(QueuePool, id="queue-pool"),
        pytest.param(NullPool, id="null-pool"),
        pytest.param(AsyncAdaptedQueuePool, id="async-adapted-queue-pool"),
    ],
)
@pytest.mark.usefixtures("isolatedSqlalchemyLogging")
def test_engine_and_pool_records_stay_off_with_sqlalchemy_at_debug(poolClass):
    # A pool writes its connections' repr, with psycopg2's libpq dsn, to its class logger.
    poolLogger = poolClass(RefuseConnection).logger
    engineLogger = logging.getLogger(SQLALCHEMY_ENGINE_LOGGER)
    assert poolLogger.isEnabledFor(logging.DEBUG)
    SuppressEngineStatementLogging()
    for suppressedLogger in (engineLogger, poolLogger):
        assert not suppressedLogger.isEnabledFor(logging.DEBUG)
        assert not suppressedLogger.isEnabledFor(logging.INFO)
        assert suppressedLogger.isEnabledFor(logging.WARNING)


@pytest.mark.parametrize(
    ("ownLevel", "expectedLevel"),
    [
        pytest.param(logging.NOTSET, logging.WARNING, id="unset-raised"),
        pytest.param(logging.DEBUG, logging.WARNING, id="debug-raised"),
        pytest.param(logging.WARNING, logging.WARNING, id="warning-kept"),
        pytest.param(logging.ERROR, logging.ERROR, id="error-kept"),
    ],
)
@pytest.mark.usefixtures("isolatedSqlalchemyLogging")
def test_suppression_raises_engine_and_pool_levels_only_below_warning(ownLevel, expectedLevel):
    suppressedLoggers = [logging.getLogger(loggerName) for loggerName in SUPPRESSED_LOGGER_NAMES]
    for suppressedLogger in suppressedLoggers:
        suppressedLogger.setLevel(ownLevel)
    SuppressEngineStatementLogging()
    assert [suppressedLogger.level for suppressedLogger in suppressedLoggers] == [
        expectedLevel,
        expectedLevel,
    ]


# The identity a psycopg2 connection repr shows, joined into a dsn at run time by psycopg2's
# make_dsn, so the code holds no connection string. The database name holds a space and a
# quote, so make_dsn writes it quoted and escaped, last, right before the dsn's closing quote.
# psycopg2 shows the password as xxx; the probe carries a generated one, so the tests also show
# that an unobscured one never leaks.
PROBE_USER = "dsn-probe-user"
PROBE_HOST = "dsn-probe-host.invalid"
PROBE_PORT = "54321"
PROBE_DATABASE = "dsn probe's db"
PROBE_PASSWORD = secrets.token_urlsafe(12)
PROBE_DSN = make_dsn(
    user=PROBE_USER,
    password=PROBE_PASSWORD,
    host=PROBE_HOST,
    port=PROBE_PORT,
    dbname=PROBE_DATABASE,
)
PROBE_IDENTITY_TEXTS = (PROBE_USER, PROBE_HOST, PROBE_PORT, "dsn probe", PROBE_PASSWORD)
# psycopg2 2.9.10 writes a connection as <connection object at %p; dsn: '%s', closed: %ld>.
PROBE_ADDRESS = "0x7f3a2c00"
CLOSE_FAILURE_MESSAGE = "server closed the connection unexpectedly"
CLOSE_ERROR_MESSAGE = "Exception closing connection"


def ConnectionRepr(dsnField: str) -> str:
    """Return psycopg2's repr of an open connection, with dsnField after its "dsn: " label."""
    return f"<connection object at {PROBE_ADDRESS}; dsn: {dsnField}, closed: 0>"


PROBE_CONNECTION_REPR = ConnectionRepr(f"'{PROBE_DSN}'")
REDACTED_CONNECTION_REPR = ConnectionRepr(REDACTED_REPLACEMENT)


def FailClose() -> None:
    """Fail as a DBAPI connection's close() fails once the server has dropped the connection."""
    raise OSError(CLOSE_FAILURE_MESSAGE)


class FailingCloseConnection:
    """A DBAPI connection stand-in: psycopg2's repr of the probe dsn, and a close() that fails."""

    # PEP 249 and SQLAlchemy's pool call this exact name, so it stays lower-case: a
    # framework-mandated exception to the MK_Modernization Rule's PascalCase method naming.
    close = staticmethod(FailClose)

    def __repr__(self) -> str:
        return PROBE_CONNECTION_REPR


@pytest.mark.usefixtures("isolatedLogging", "isolatedSqlalchemyLogging")
def test_pool_close_error_is_logged_with_the_connection_dsn_redacted(monkeypatch):
    ConfigureLogging()
    SuppressEngineStatementLogging()
    capturedStdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", capturedStdout)
    failingPool = QueuePool(FailingCloseConnection, reset_on_return=None)
    failingPool.connect().close()
    failingPool.dispose()
    outputLines = capturedStdout.getvalue().splitlines()
    errorLine = f"ERROR {failingPool.logger.name} {CLOSE_ERROR_MESSAGE} {REDACTED_CONNECTION_REPR}"
    errorLinePattern = re.compile(LOG_TIMESTAMP_PATTERN + re.escape(errorLine))
    recordLines = [line for line in outputLines if re.match(LOG_TIMESTAMP_PATTERN, line)]
    # The error stays visible with its traceback; the DEBUG checkout and checkin stay off.
    assert [bool(errorLinePattern.fullmatch(line)) for line in recordLines] == [True]
    assert outputLines[-1] == f"OSError: {CLOSE_FAILURE_MESSAGE}"
    loggedText = "\n".join(outputLines)
    leakedTexts = [text for text in PROBE_IDENTITY_TEXTS if text in loggedText]
    assert leakedTexts == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(PROBE_CONNECTION_REPR, REDACTED_CONNECTION_REPR, id="quoted-value-last"),
        pytest.param(
            f"connection {PROBE_CONNECTION_REPR}, which will be terminated.",
            f"connection {REDACTED_CONNECTION_REPR}, which will be terminated.",
            id="repr-in-message",
        ),
        # psycopg2 keeps a dsn that holds no password as written, here a URL with a query.
        pytest.param(
            ConnectionRepr(f"'postgresql://{PROBE_USER}@{PROBE_HOST}:{PROBE_PORT}/db?sslmode=x'"),
            REDACTED_CONNECTION_REPR,
            id="url-dsn",
        ),
        pytest.param(
            f"dsn: '{PROBE_DSN}' rejected",
            f"dsn: {REDACTED_REPLACEMENT} rejected",
            id="no-repr-tail",
        ),
        pytest.param(
            f"dsn: 'host={PROBE_HOST} port={PROBE_PORT}\nTraceback (most recent call last):",
            f"dsn: {REDACTED_REPLACEMENT}\nTraceback (most recent call last):",
            id="unterminated-stops-at-line-end",
        ),
    ],
)
def test_mask_sensitive_text_redacts_connection_dsns(text, expected):
    assert MaskSensitiveText(text) == expected
    assert MaskSensitiveText(expected) == expected
