"""Standard-library logging to stdout, with card numbers masked.

Replaces: the CICS region and JES SYSOUT logs of CardDemo; masks copybook CVACT02Y
CARD-NUM PIC X(16). Specified in the Technical Specification, Section 0.6.5 (Logging).

Card numbers reach the logs in request paths and query strings (/api/v1/cards/{cardNum},
cardNum=). Every run of exactly 16 ASCII digits is therefore shown as twelve asterisks plus
its last four digits, both in the Uvicorn access log and in application logs. A 16-digit
transaction ID (CVTRA05Y TRAN-ID) is masked as well: the filter cannot tell the two apart,
and over-masking is the safe direction. SSNs are not handled here, because they never
appear in URLs and services mask them with security.MaskSsn before logging.

This module imports only the standard library, so batch and the backend can both call
ConfigureLogging() without any settings in the environment.
"""

import logging
import re
import sys
from collections.abc import Mapping

PAN_RUN_PATTERN = re.compile(r"(?<![0-9])[0-9]{12}([0-9]{4})(?![0-9])")
MASKED_PAN_REPLACEMENT = r"************\1"
HANDLER_NAME = "carddemo-stdout"
UVICORN_ACCESS_LOGGER = "uvicorn.access"
LOG_LINE_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def MaskPanRuns(text: str) -> str:
    """Return text with each 16-digit run shown as twelve asterisks and its last four digits."""
    return PAN_RUN_PATTERN.sub(MASKED_PAN_REPLACEMENT, text)


def MaskValue(value: object) -> object:
    """Return a string log argument masked, and any other value unchanged."""
    if isinstance(value, str):
        return MaskPanRuns(value)
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


class PanMaskingFilter(logging.Filter):
    """Masks card numbers in a log record in place. It never drops a record."""

    # logging calls this exact name, so it stays lower-case: a framework-mandated
    # exception to the MK_Modernization Rule's PascalCase method naming.
    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        maskedRendered = MaskPanRuns(rendered)
        if maskedRendered == rendered:
            return True
        # Uvicorn's AccessFormatter unpacks record.args as the 5-tuple (client, method,
        # path, HTTP version, status), so args are masked in place rather than folded
        # into msg, which would leave that formatter nothing to unpack.
        record.args = MaskArguments(record.args)
        if isinstance(record.msg, str):
            record.msg = MaskPanRuns(record.msg)
        remaining = record.getMessage()
        if MaskPanRuns(remaining) != remaining:
            # The run spans several arguments, or sits in a non-string argument.
            record.msg = maskedRendered
            record.args = None
        return True


def BuildStdoutHandler() -> logging.Handler:
    """Return the named stdout handler that masks every record it writes."""
    stdoutHandler = logging.StreamHandler(sys.stdout)
    stdoutHandler.name = HANDLER_NAME
    stdoutHandler.setFormatter(logging.Formatter(LOG_LINE_FORMAT))
    stdoutHandler.addFilter(PanMaskingFilter())
    return stdoutHandler


def ConfigureLogging() -> None:
    """Log to stdout at INFO with card numbers masked. Calling it again changes nothing."""
    rootLogger = logging.getLogger()
    # The filter sits on the handler: a logger's own filters are not consulted for
    # records that propagate up from child loggers such as app.services.*.
    if not any(handler.name == HANDLER_NAME for handler in rootLogger.handlers):
        rootLogger.addHandler(BuildStdoutHandler())
    rootLogger.setLevel(logging.INFO)
    # Uvicorn's access records do not propagate and are logged on this logger itself,
    # so a logger-level filter masks them before Uvicorn's own handler formats them.
    accessLogger = logging.getLogger(UVICORN_ACCESS_LOGGER)
    if not any(isinstance(existing, PanMaskingFilter) for existing in accessLogger.filters):
        accessLogger.addFilter(PanMaskingFilter())
