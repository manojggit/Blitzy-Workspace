"""Replaces: FUNCTION CURRENT-DATE in CBTRN02C and CBACT04C (Z-GET-DB2-FORMAT-TIMESTAMP).

The time source of every batch job. Clock.Now() returns the local time, or the --as-of value,
which freezes the clock so reconciliation runs are deterministic. Either is truncated to
hundredths of a second, because FUNCTION CURRENT-DATE carries hundredths and
Z-GET-DB2-FORMAT-TIMESTAMP writes YYYY-MM-DD-HH.MM.SS.cc0000.

Values are naive local time: the legacy programs read the system's local clock, and the
timestamps they produce are stored as TIMESTAMP(6) WITHOUT TIME ZONE. This module imports only
the standard library and reads no setting.
"""

import re
from dataclasses import dataclass
from datetime import datetime

HUNDREDTHS_MICROSECONDS = 10000

# Applied with fullmatch, so a date alone, a space separator or a time-zone offset is refused
# and the parsed value is always naive. re.ASCII keeps \d to 0-9: without it, other Unicode
# digits pass the shape check and fail later in fromisoformat with a less useful message.
AS_OF_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?", re.ASCII)


def ParseAsOf(text: str) -> datetime:
    """Parse the --as-of option, YYYY-MM-DDTHH:MM:SS[.ffffff], into a naive datetime.

    Raises ValueError when the text has another shape, or names an invalid date or time such
    as month 13. The value is not truncated here; Clock.Now() truncates it.
    """
    if AS_OF_PATTERN.fullmatch(text) is None:
        raise ValueError("--as-of must be YYYY-MM-DDTHH:MM:SS[.ffffff]")
    return datetime.fromisoformat(text)


def TruncateToHundredths(value: datetime) -> datetime:
    """Drop the sub-hundredth digits of the seconds, which FUNCTION CURRENT-DATE never has."""
    hundredths = value.microsecond // HUNDREDTHS_MICROSECONDS
    return value.replace(microsecond=hundredths * HUNDREDTHS_MICROSECONDS)


@dataclass(frozen=True)
class Clock:
    """The batch clock: live local time, or frozen at asOf for every call."""

    asOf: datetime | None = None

    def Now(self) -> datetime:
        """Return asOf, or the current local time when it is unset, truncated to hundredths."""
        current = self.asOf if self.asOf is not None else datetime.now()
        return TruncateToHundredths(current)

    def AsOfText(self) -> str | None:
        """Return asOf in ISO form for the run history, or None for a live clock."""
        if self.asOf is None:
            return None
        return self.asOf.isoformat()
