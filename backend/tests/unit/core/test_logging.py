"""Unit tests for app.core.logging, the stdout logging that masks card numbers.

app.core.logging replaces the CICS region and JES SYSOUT logs of CardDemo. It guarantees that no
raw card number, copybook CVACT02Y CARD-NUM PIC X(16) of file CARDDAT (csd/CARDDEMO.CSD),
reaches any log, Uvicorn's access log of /api/v1/cards/{cardNum} and cardNum= requests included
(Technical Specification, Section 0.6.5, Logging). These tests emit a uvicorn.access record for a
raw-PAN request line and assert the masked output, pin MaskPanRuns, and check that
ConfigureLogging installs one masking root handler and changes nothing when called again.

No database is used. Each test that configures logging runs under the isolatedLogging fixture,
which restores the root and Uvicorn loggers afterwards, so later tests log as before.
"""

import io
import logging
import sys
from dataclasses import dataclass

import pytest
from uvicorn.logging import AccessFormatter

from app.core.logging import (
    HANDLER_NAME,
    UVICORN_LOGGERS,
    ConfigureLogging,
    MaskPanRuns,
    PanMaskingFilter,
)

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
    """The filters of one Uvicorn handler, and the stderr stream ConfigureLogging moves off."""

    handler: logging.Handler
    filters: tuple[object, ...]
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
    """Return a handler's filters, and its stream when that is stderr.

    ConfigureLogging moves only stderr streams to stdout, so no other stream is recorded. That
    keeps the restore off pytest's capture handlers, whose streams pytest swaps every phase.
    """
    stream = handler.stream if isinstance(handler, logging.StreamHandler) else None
    isStderr = stream is not None and (stream is sys.stderr or stream is sys.__stderr__)
    stderrStream = stream if isStderr else None
    return HandlerSnapshot(
        handler=handler, filters=tuple(handler.filters), stderrStream=stderrStream
    )


def RestoreHandler(snapshot: HandlerSnapshot) -> None:
    """Put a handler's filters back, and the stderr stream ConfigureLogging moved to stdout."""
    snapshot.handler.filters = list(snapshot.filters)
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
    propagation, and Uvicorn's handlers their filters and stderr streams.
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
