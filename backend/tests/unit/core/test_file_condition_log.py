"""Unit tests for app.core.errors.LogFileCondition, the log record of a caught file condition.

LogFileCondition replaces the ``DISPLAY 'RESP:' WS-RESP-CD 'REAS:' WS-REAS-CD`` of the online
programs' ``WHEN OTHER`` branches. These tests pin its level (NOTOPEN and RECORDBUSY at WARNING,
every other condition at ERROR), its context (transaction, operation, file, RESP, RESP2, the
innermost cause's class and the SQLSTATE) and that no exception text reaches the record. The
exception chains copy the ones SQLAlchemy's asyncpg adapter, asyncpg and psycopg2 raise, and no
database is used.
"""

import logging

import pytest
from asyncpg.exceptions import UndefinedColumnError
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError

from app.core.errors import (
    HTTP_FILE_ERROR,
    DuplicateKeyError,
    FailureCauseOf,
    FileIoError,
    LogFileCondition,
    RecordLockError,
)
from app.core.locks import FileNotOpenError
from app.services.transactions import cotrn01c

TEST_LOGGER_NAME = "tests.unit.core.file_condition_log"
TEST_TRANSACTION_ID = "CT99"
TEST_OPERATION = "READ"
# A card-number-shaped run, built at run time, placed in every exception text and parameter.
SENSITIVE_MARKER = "".join(str(digit) for digit in range(10)) + "543210"
FAILED_STATEMENT = "SELECT card_num FROM cards.cards WHERE card_num = $1"
FAILED_PARAMETERS = (SENSITIVE_MARKER,)
# The COTRN01C READ-TRANSACT-FILE WHEN OTHER literal and the record CT01 logs beside it.
CT01_LOOKUP_FAILED_LITERAL = "Unable to lookup Transaction..."
CT01_CONDITION_LOG_MESSAGE = (
    "Transaction CT01 READ on TRANSACT returned RESP:17 REAS:0 "
    "(cause ConnectionRefusedError, SQLSTATE none)"
)


class AdapterProgrammingError(Exception):
    """SQLAlchemy's asyncpg adapter error: it carries the SQLSTATE as sqlstate and pgcode."""

    def __init__(self, text, sqlState):
        super().__init__(text)
        self.sqlstate = sqlState
        self.pgcode = sqlState


class Psycopg2CheckViolation(Exception):
    """A psycopg2 driver error: it carries the SQLSTATE as pgcode only."""

    def __init__(self, text, sqlState):
        super().__init__(text)
        self.pgcode = sqlState


class CyclicCauseA(Exception):
    """One end of an exception chain that loops back on itself."""


class CyclicCauseB(Exception):
    """The other end of the looping chain; it carries the chain's SQLSTATE."""

    # Named as asyncpg.PostgresError names it: an exception to MK_Modernization Rule clause 1.
    sqlstate = "XX000"


def RaisedFrom(error, cause):
    """Return ``error`` with ``cause`` as its ``__cause__``, as ``raise ... from`` sets it."""
    error.__cause__ = cause
    return error


def AsyncpgUndefinedColumnChain():
    """Return FileIoError raised from the three-link chain of an undefined column (42703)."""
    driverError = UndefinedColumnError(f"column embossed_name does not exist {SENSITIVE_MARKER}")
    adapterError = RaisedFrom(AdapterProgrammingError(str(driverError), "42703"), driverError)
    wrappedError = RaisedFrom(
        ProgrammingError(FAILED_STATEMENT, FAILED_PARAMETERS, adapterError), adapterError
    )
    return RaisedFrom(FileIoError("CARDDAT"), wrappedError)


def ConnectionRefusedChain():
    """Return FileIoError raised from the unwrapped connect failure of a stopped database."""
    refusal = ConnectionRefusedError(111, f"Connect call failed {SENSITIVE_MARKER}")
    return RaisedFrom(FileIoError("TRANSACT"), refusal)


def Psycopg2OrigOnlyChain():
    """Return FileIoError raised from a DBAPIError whose psycopg2 error is held only in orig."""
    driverError = Psycopg2CheckViolation(f"check violated {SENSITIVE_MARKER}", "23514")
    return RaisedFrom(
        FileIoError("ACCTDAT"), DBAPIError(FAILED_STATEMENT, FAILED_PARAMETERS, driverError)
    )


def LockTimeoutChain():
    """Return RecordLockError raised from the lock_not_available (55P03) of a held record."""
    driverError = AdapterProgrammingError(f"lock not available {SENSITIVE_MARKER}", "55P03")
    wrappedError = RaisedFrom(
        OperationalError(FAILED_STATEMENT, FAILED_PARAMETERS, driverError), driverError
    )
    return RaisedFrom(RecordLockError("USRSEC"), wrappedError)


def DuplicateKeyChain():
    """Return DuplicateKeyError raised from a unique violation (23505) a WHEN OTHER reached."""
    driverError = AdapterProgrammingError(f"duplicate key {SENSITIVE_MARKER}", "23505")
    wrappedError = RaisedFrom(
        ProgrammingError(FAILED_STATEMENT, FAILED_PARAMETERS, driverError), driverError
    )
    return RaisedFrom(DuplicateKeyError("CXACAIX"), wrappedError)


@pytest.mark.parametrize(
    ("buildError", "expectedLevel", "expectedMessage"),
    [
        (
            AsyncpgUndefinedColumnChain,
            logging.ERROR,
            (
                "Transaction CT99 READ on CARDDAT returned RESP:17 REAS:0 "
                "(cause UndefinedColumnError, SQLSTATE 42703)"
            ),
        ),
        (
            ConnectionRefusedChain,
            logging.ERROR,
            (
                "Transaction CT99 READ on TRANSACT returned RESP:17 REAS:0 "
                "(cause ConnectionRefusedError, SQLSTATE none)"
            ),
        ),
        (
            Psycopg2OrigOnlyChain,
            logging.ERROR,
            (
                "Transaction CT99 READ on ACCTDAT returned RESP:17 REAS:0 "
                "(cause Psycopg2CheckViolation, SQLSTATE 23514)"
            ),
        ),
        (
            DuplicateKeyChain,
            logging.ERROR,
            (
                "Transaction CT99 READ on CXACAIX returned RESP:14 REAS:0 "
                "(cause AdapterProgrammingError, SQLSTATE 23505)"
            ),
        ),
        (
            lambda: FileNotOpenError("USRSEC"),
            logging.WARNING,
            "Transaction CT99 READ on USRSEC returned RESP:19 REAS:60 (cause none, SQLSTATE none)",
        ),
        (
            LockTimeoutChain,
            logging.WARNING,
            (
                "Transaction CT99 READ on USRSEC returned RESP:101 REAS:0 "
                "(cause AdapterProgrammingError, SQLSTATE 55P03)"
            ),
        ),
    ],
    ids=[
        "ioerr-asyncpg-undefined-column",
        "ioerr-connection-refused",
        "ioerr-psycopg2-pgcode-in-orig",
        "dupkey-in-when-other",
        "notopen-batch-window",
        "recordbusy-lock-timeout",
    ],
)
def test_file_condition_is_logged_once_with_level_and_context(
    buildError, expectedLevel, expectedMessage, caplog
):
    """One record on the given logger, at the condition's level, with codes and class names."""
    caplog.set_level(logging.DEBUG, logger=TEST_LOGGER_NAME)
    error = buildError()

    LogFileCondition(
        logging.getLogger(TEST_LOGGER_NAME), TEST_TRANSACTION_ID, TEST_OPERATION, error
    )

    assert [(logRecord.name, logRecord.levelno) for logRecord in caplog.records] == [
        (TEST_LOGGER_NAME, expectedLevel)
    ]
    assert caplog.records[0].getMessage() == expectedMessage
    assert caplog.records[0].exc_info is None
    assert SENSITIVE_MARKER not in caplog.text


def test_cyclic_cause_chain_ends_with_its_sqlstate():
    """A chain that loops back on itself stops at the depth limit instead of looping forever."""
    firstCause, secondCause = CyclicCauseA(SENSITIVE_MARKER), CyclicCauseB(SENSITIVE_MARKER)
    RaisedFrom(firstCause, secondCause)
    RaisedFrom(secondCause, firstCause)
    error = RaisedFrom(FileIoError("TRANSACT"), firstCause)

    causeName, sqlState = FailureCauseOf(error)

    assert causeName in {CyclicCauseA.__name__, CyclicCauseB.__name__}
    assert sqlState == CyclicCauseB.sqlstate


def test_service_when_other_branch_logs_the_condition(caplog):
    """CT01's READ-TRANSACT-FILE WHEN OTHER logs the condition and keeps its 503 literal."""
    caplog.set_level(logging.DEBUG, logger=cotrn01c.LOGGER.name)
    error = ConnectionRefusedChain()

    outcome = cotrn01c.FileErrorOutcome(error)

    loggedRecords = [
        (logRecord.name, logRecord.levelno, logRecord.getMessage()) for logRecord in caplog.records
    ]
    assert (outcome.message, outcome.httpStatus) == (CT01_LOOKUP_FAILED_LITERAL, HTTP_FILE_ERROR)
    assert loggedRecords == [(cotrn01c.LOGGER.name, logging.ERROR, CT01_CONDITION_LOG_MESSAGE)]
