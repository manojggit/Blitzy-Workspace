"""Standard-library logging to stdout, with card numbers masked and card references redacted.

Replaces: the CICS region and JES SYSOUT logs of CardDemo; masks copybook CVACT02Y
CARD-NUM PIC X(16). Specified in the Technical Specification, Section 0.6.5 (Logging).

Card numbers reach the logs in request paths and query strings (/api/v1/cards/{cardNum},
cardNum=). Every run of exactly 16 ASCII digits is therefore shown as twelve asterisks plus
its last four digits, both in the Uvicorn access log and in application logs. A 16-digit
transaction ID (CVTRA05Y TRAN-ID) is masked as well: the filter cannot tell the two apart,
and over-masking is the safe direction. SSNs are not handled here, because they never
appear in URLs and services mask them with security.MaskSsn before logging.

The encrypted card_ref and screen_token navigation tokens are redacted whole: every Fernet
token wherever it appears, any path segment after /api/v1/cards/ other than a card number or
the route segment "selection", and the values of the cardNum, card_ref, cardRef, screen_token
and screenToken query parameters. The startKey of GET /api/v1/cards is a card_ref and is
redacted too; the startKey of the other lists is a plain key and stays readable.

Query parameters are judged by their decoded value, as Starlette decodes them, because Uvicorn
logs the query string exactly as the client sent it. A percent-encoded parameter name or value
is therefore redacted or masked as its decoded form would be, and any other parameter is
written as sent unless its decoded name or value holds a card number or a Fernet token.

Masking covers each record's message, args, traceback and stack text. The root logger's
stdout handler comes first among the root handlers, so every other root handler receives
the record already masked. Each Uvicorn logger and handler masks too, and Uvicorn's error
log, which its default configuration writes to stderr, is written to stdout instead.

This module imports only the standard library, so batch and the backend can both call
ConfigureLogging() without any settings in the environment.
"""

import logging
import re
import sys
import urllib.parse
from collections.abc import Mapping

PAN_RUN_PATTERN = re.compile(r"(?<![0-9])[0-9]{12}([0-9]{4})(?![0-9])")
MASKED_PAN_REPLACEMENT = r"************\1"
REDACTED_REPLACEMENT = "[REDACTED]"
# A Fernet token is URL-safe base64 of version byte 0x80 and a 64-bit timestamp, so it opens
# with gAAAAA; its = padding is %3D in the percent-encoded path Uvicorn logs.
FERNET_TOKEN_PATTERN = re.compile(r"gAAAAA[0-9A-Za-z_-]{20,}(?:=|%3[Dd])*")
# The slots are anchored to the API path and to query separators, so traceback file paths
# such as services/cards/ and keyword arguments in traceback source lines stay readable.
# Neither slot value takes in a ?, so redacting one never moves where a request query starts.
CARD_PATH_SLOT_PATTERN = re.compile(r"(/api/v1/cards/)([^/?#\s\"']+)")
CARD_QUERY_SLOT_PATTERN = re.compile(
    r"((?:^|[?&])(?:cardNum|card_ref|cardRef|screen_token|screenToken)=)([^?&#\s\"']+)"
)
CARD_NUMBER_PATTERN = re.compile(r"[0-9]{16}")
MASKED_CARD_NUMBER_PATTERN = re.compile(r"\*{12}[0-9]{4}")
# POST /api/v1/cards/selection is the one fixed route segment after /api/v1/cards/.
VISIBLE_CARD_SLOT_VALUES = frozenset({REDACTED_REPLACEMENT, "selection"})
# A request target: the text before its first ? and the query after it. Starlette reads #,
# quotes and every other non-space character of a logged query as query text, so only
# whitespace ends it. Matching from the start of a whitespace-free run keeps this linear.
REQUEST_QUERY_PATTERN = re.compile(r"(?<!\S)([^\s?]*)\?(\S*)")
CARD_REFERENCE_QUERY_NAMES = frozenset({"card_ref", "cardRef", "screen_token", "screenToken"})
CARD_NUMBER_QUERY_NAME = "cardNum"
START_KEY_QUERY_NAME = "startKey"
CARD_LIST_PATH = "/api/v1/cards"
# Characters a masked query name or value keeps unencoded. &, =, +, %, # and quotes are
# encoded, so the masked text decodes to itself and keeps the query's pairs apart.
QUERY_TEXT_SAFE = "!$()*,/:;?@[]"
HANDLER_NAME = "carddemo-stdout"
UVICORN_ACCESS_LOGGER = "uvicorn.access"
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", UVICORN_ACCESS_LOGGER, "uvicorn.asgi")
LOG_LINE_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
TRACEBACK_FORMATTER = logging.Formatter()


def MaskPanRuns(text: str) -> str:
    """Return text with each 16-digit run shown as twelve asterisks and its last four digits."""
    return PAN_RUN_PATTERN.sub(MASKED_PAN_REPLACEMENT, text)


def RedactCardSlotValue(slotValue: str) -> str:
    """Return a card slot's value with a card number masked and a token redacted.

    A slot holds a 16-digit card number, which keeps its last four digits, or an opaque
    card_ref or screen_token, which is replaced whole. A masked number, the redaction marker
    and the fixed segment "selection" are kept, so masking a result again changes nothing.
    """
    if CARD_NUMBER_PATTERN.fullmatch(slotValue):
        return MaskPanRuns(slotValue)
    if MASKED_CARD_NUMBER_PATTERN.fullmatch(slotValue) or slotValue in VISIBLE_CARD_SLOT_VALUES:
        return slotValue
    return REDACTED_REPLACEMENT


def RedactCardSlot(slotMatch: re.Match[str]) -> str:
    """Return a card path or query slot with its value passed through RedactCardSlotValue."""
    return slotMatch.group(1) + RedactCardSlotValue(slotMatch.group(2))


def MaskPlainText(text: str) -> str:
    """Return text, read literally, with tokens and card slots redacted and card numbers masked.

    Every Fernet token is redacted wherever it appears. The path segment after /api/v1/cards/
    and the value of a cardNum, card_ref, cardRef, screen_token or screenToken parameter
    go through RedactCardSlot. Every remaining 16-digit run is then masked by MaskPanRuns.
    """
    redactedText = FERNET_TOKEN_PATTERN.sub(REDACTED_REPLACEMENT, text)
    redactedText = CARD_PATH_SLOT_PATTERN.sub(RedactCardSlot, redactedText)
    redactedText = CARD_QUERY_SLOT_PATTERN.sub(RedactCardSlot, redactedText)
    return MaskPanRuns(redactedText)


def IsSensitiveQueryParameter(queryName: str, requestPath: str) -> bool:
    """Tell whether a decoded query parameter name carries a card number or a card reference.

    The startKey of GET /api/v1/cards is a card_ref (schemas.cards.CardListQuery); the startKey
    of the other lists is a plain record key.
    """
    if queryName in CARD_REFERENCE_QUERY_NAMES or queryName == CARD_NUMBER_QUERY_NAME:
        return True
    decodedPath = urllib.parse.unquote(requestPath).rstrip("/")
    return queryName == START_KEY_QUERY_NAME and decodedPath.endswith(CARD_LIST_PATH)


def RedactQueryValue(queryName: str, queryValue: str) -> str:
    """Return a decoded cardNum value as a card slot shows it, and any card reference redacted."""
    if queryName == CARD_NUMBER_QUERY_NAME:
        return RedactCardSlotValue(queryValue)
    return REDACTED_REPLACEMENT


def MaskQueryText(rawText: str, decodedText: str) -> str:
    """Return a query name or value as sent, or its decoded text masked and encoded again.

    The decoded text is masked by MaskPlainText. Unless that changes it, the text is kept
    exactly as sent, so diagnostics stay as the client wrote them.
    """
    maskedText = MaskPlainText(decodedText)
    if maskedText == decodedText:
        return rawText
    return urllib.parse.quote_plus(maskedText, safe=QUERY_TEXT_SAFE, errors="backslashreplace")


def MaskQueryPair(pairText: str, requestPath: str) -> str:
    """Return one name=value pair of a request query masked by its decoded meaning.

    The name and value are split at the first = and decoded as Starlette's parse_qsl decodes
    them. A card number or card reference parameter with a value has that value replaced by
    RedactQueryValue; any other name and value go through MaskQueryText.
    """
    rawName, separator, rawValue = pairText.partition("=")
    queryName = urllib.parse.unquote_plus(rawName)
    queryValue = urllib.parse.unquote_plus(rawValue)
    if rawValue and IsSensitiveQueryParameter(queryName, requestPath):
        return rawName + separator + RedactQueryValue(queryName, queryValue)
    return MaskQueryText(rawName, queryName) + separator + MaskQueryText(rawValue, queryValue)


def MaskRequestQuery(queryMatch: re.Match[str]) -> str:
    """Return a request target with its path as sent and each & pair of its query masked."""
    requestPath, queryText = queryMatch.group(1), queryMatch.group(2)
    maskedPairs = [MaskQueryPair(pairText, requestPath) for pairText in queryText.split("&")]
    return requestPath + "?" + "&".join(maskedPairs)


def MaskSensitiveText(text: str) -> str:
    """Return text with card_ref and screen_token values redacted and card numbers masked.

    Each request query is first rewritten pair by pair by MaskRequestQuery, which judges every
    parameter by its decoded value. MaskPlainText then redacts each literal Fernet token and
    card slot and masks each remaining 16-digit run. Applied to its own result, it returns that
    result unchanged.
    """
    return MaskPlainText(REQUEST_QUERY_PATTERN.sub(MaskRequestQuery, text))


def MaskValue(value: object) -> object:
    """Return a string log argument masked, and any other value unchanged."""
    if isinstance(value, str):
        return MaskSensitiveText(value)
    return value


def MaskArguments(
    args: tuple[object, ...] | Mapping[str, object] | None,
) -> tuple[object, ...] | Mapping[str, object] | None:
    """Mask the string items of a record's args, keeping its tuple or mapping shape."""
    if isinstance(args, tuple):
        return tuple(MaskValue(value) for value in args)
    if isinstance(args, Mapping):
        return {key: MaskValue(value) for key, value in args.items()}
    return args


def RenderMessage(record: logging.LogRecord) -> str:
    """Return the record's message, or its msg and args side by side when they do not fit."""
    try:
        return record.getMessage()
    except (TypeError, ValueError, KeyError):
        # Formatting a mismatched msg and args is the handler's error to report, so the
        # filter masks what the handler would print instead of raising into the caller.
        return f"{record.msg} {record.args}"


def MaskMessage(record: logging.LogRecord) -> None:
    """Mask the record's message in place, keeping the shape of its args.

    Uvicorn's AccessFormatter unpacks record.args as the 5-tuple (client, method, path,
    HTTP version, status), so string args are masked in place, and msg has only its card
    numbers masked, since redacting a slot such as "/cards/%s" would consume its placeholder.
    The masked message is folded into msg only when the record still renders unmasked text.
    Uvicorn's color_message would re-render the original text, so it is dropped.
    """
    rendered = RenderMessage(record)
    maskedRendered = MaskSensitiveText(rendered)
    if maskedRendered == rendered:
        return
    record.args = MaskArguments(record.args)
    if isinstance(record.msg, str):
        record.msg = MaskPanRuns(record.msg)
    remaining = RenderMessage(record)
    if MaskSensitiveText(remaining) != remaining:
        # The value spans several arguments, sits in a non-string argument or in msg.
        record.msg = maskedRendered
        record.args = None
    record.__dict__.pop("color_message", None)


def MaskDiagnostics(record: logging.LogRecord) -> None:
    """Mask the traceback and stack text that formatters append after the message.

    The traceback is formatted as logging.Formatter formats it and cached in exc_text, which
    every formatter appends instead of formatting exc_info again; exc_info itself is kept.
    """
    if isinstance(record.exc_info, tuple) and not record.exc_text:
        record.exc_text = TRACEBACK_FORMATTER.formatException(record.exc_info)
    if record.exc_text:
        record.exc_text = MaskSensitiveText(record.exc_text)
    if record.stack_info:
        record.stack_info = MaskSensitiveText(record.stack_info)


class PanMaskingFilter(logging.Filter):
    """Masks card numbers and redacts card_ref and screen_token values in a log record in place.

    It covers the message, its args, the traceback and the stack text. It never drops a record.
    """

    # logging calls this exact name, so it stays lower-case: a framework-mandated
    # exception to the MK_Modernization Rule's PascalCase method naming.
    def filter(self, record: logging.LogRecord) -> bool:
        MaskMessage(record)
        MaskDiagnostics(record)
        return True


def BuildStdoutHandler() -> logging.Handler:
    """Return the named stdout handler that masks every record it writes."""
    stdoutHandler = logging.StreamHandler(sys.stdout)
    stdoutHandler.name = HANDLER_NAME
    stdoutHandler.setFormatter(logging.Formatter(LOG_LINE_FORMAT))
    stdoutHandler.addFilter(PanMaskingFilter())
    return stdoutHandler


def AttachMaskingFilter(filterer: logging.Filterer) -> None:
    """Add a PanMaskingFilter to a logger or handler that does not have one yet."""
    if not any(isinstance(existing, PanMaskingFilter) for existing in filterer.filters):
        filterer.addFilter(PanMaskingFilter())


def InstallStdoutHandler() -> None:
    """Make the named stdout handler the first root handler, and log at INFO.

    The filter sits on the handler: a logger's own filters are not consulted for records
    that propagate up from child loggers such as app.services.*. As the first root handler,
    it masks each record in place before any other root handler receives it.
    """
    rootLogger = logging.getLogger()
    namedHandlers = [handler for handler in rootLogger.handlers if handler.name == HANDLER_NAME]
    stdoutHandler = namedHandlers[0] if namedHandlers else BuildStdoutHandler()
    otherHandlers = [handler for handler in rootLogger.handlers if handler.name != HANDLER_NAME]
    rootLogger.handlers = [stdoutHandler, *otherHandlers]
    rootLogger.setLevel(logging.INFO)


def RouteStderrToStdout(handler: logging.Handler) -> None:
    """Point a stream handler that writes to stderr at stdout; leave any other handler alone."""
    if not isinstance(handler, logging.StreamHandler) or handler.stream is None:
        return
    if handler.stream is sys.stderr or handler.stream is sys.__stderr__:
        handler.setStream(sys.stdout)


def ProtectUvicornLogger(loggerName: str) -> None:
    """Mask what a Uvicorn logger and its handlers write, and move its stderr output to stdout.

    Under Uvicorn's default configuration its records never reach the root handler: the
    non-propagating "uvicorn" logger writes uvicorn.error records through its stderr handler,
    and the non-propagating uvicorn.access logger writes through its own handler. A logger's
    filter masks the records logged on it and survives a later logging.config.dictConfig; a
    handler's filter masks every record the handler writes, including propagated ones.
    """
    uvicornLogger = logging.getLogger(loggerName)
    AttachMaskingFilter(uvicornLogger)
    for handler in uvicornLogger.handlers:
        AttachMaskingFilter(handler)
        RouteStderrToStdout(handler)


def ConfigureLogging() -> None:
    """Log to stdout at INFO with card numbers masked and card references redacted.

    It covers the root handlers and Uvicorn's loggers. Calling it again changes nothing.
    """
    InstallStdoutHandler()
    for loggerName in UVICORN_LOGGERS:
        ProtectUvicornLogger(loggerName)


# SQLAlchemy's Engine writes every statement with its bound parameters at INFO and every
# fetched row at DEBUG to this logger, which every CardDemo engine uses (none sets a
# logging_name), as soon as it or the "sqlalchemy" logger above it allows those levels.
SQLALCHEMY_ENGINE_LOGGER = "sqlalchemy.engine.Engine"


def SuppressEngineStatementLogging() -> None:
    """Keep SQLAlchemy from creating statement, parameter and result-row log records.

    Bound parameters and fetched rows carry password hashes, SSNs, CVVs and customer data,
    which no log may hold. The engine logger's own level is raised to WARNING unless it is
    already WARNING or above, so isEnabledFor(INFO) and isEnabledFor(DEBUG) stay false and
    SQLAlchemy never builds those records, even with the "sqlalchemy" logger at DEBUG; engine
    warnings and errors still propagate. logging.config.fileConfig and dictConfig reset this
    logger to NOTSET when they configure "sqlalchemy.engine", so each engine builder calls this
    after logging is configured, and also sets hide_parameters=True, which keeps bound values
    out of SQLAlchemy's error text.
    """
    engineLogger = logging.getLogger(SQLALCHEMY_ENGINE_LOGGER)
    if engineLogger.level < logging.WARNING:
        engineLogger.setLevel(logging.WARNING)
