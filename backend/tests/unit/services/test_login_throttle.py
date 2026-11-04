"""Unit tests of the sign-on lockout that guards COSGN00C (CC00, map COSGN00).

The throttle has no legacy source (AAP 0.7.1); it guards the sign-on of cbl/COSGN00C.cbl, whose
key is SEC-USR-ID X(08) of copybook CSUSR01Y. Each failure counts against the user ID in
security.login_attempts, LOGIN_MAX_ATTEMPTS (5) failures lock the ID for LOGIN_LOCKOUT_SECONDS
(900), a locked ID answers 429 with ``Too many failed attempts. Try again later ...`` on the
user ID, and a success deletes the row.

The functions of app.services.auth.login_throttle are called directly, with a service context
of makeOnlineContext. FakeClock replaces login_throttle.UtcNow, the module's only clock. Every
row is re-read through syncEngine, a connection of its own, so each read also proves that the
write committed. The ``throttleKey`` teardown deletes the row of its generated user ID, and
restoreSeedState truncates the table with every other one.

The throttle counts the user ID exactly as its caller passes it: cosgn00c upper-cases the keyed
ID first, as COSGN00C's FUNCTION UPPER-CASE does, so the lower-case sign-on case is pinned
through cosgn00c in test_login_throttle_concurrent_attempts.py, not here.
"""

import secrets
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import pytest
from sqlalchemy import Engine, RowMapping, text

from app.core import messages
from app.core.config import Settings
from app.core.dependencies import OnlineContext
from app.core.errors import LegacyMessageError
from app.services.auth import login_throttle

type ContextFactory = Callable[..., Awaitable[OnlineContext]]

# The lockout of AAP 0.7.1: its literal, status and cursor field, and its default limits.
LOCKED_MESSAGE = "Too many failed attempts. Try again later ..."
LOCKED_STATUS = 429
USER_ID_FIELD = "user_id"
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 900
LOCKOUT_PERIOD = timedelta(seconds=LOCKOUT_SECONDS)

# An aware UTC instant, as the TIMESTAMPTZ columns hold, with microseconds they keep exactly.
CLOCK_START = datetime(2022, 7, 18, 9, 30, 15, 250000, tzinfo=UTC)
# The clock moves this far before each recorded failure, so the lock provably runs from the
# failure that reaches the limit and last_failure_at from the latest one.
FAILURE_INTERVAL_SECONDS = 1
# token_hex(4) gives eight hex digits: the full SEC-USR-ID X(08) width, upper-cased as cosgn00c
# passes it. Seed IDs (ADMIN001, USER0001, ...) and conftest IDs (seven characters) never match.
USER_ID_HEX_BYTES = 4

ATTEMPT_ROW_QUERY = text(
    "SELECT failure_count, locked_until, last_failure_at FROM security.login_attempts"
    " WHERE user_id = :userId"
)
DELETE_ATTEMPT_ROW_STATEMENT = text("DELETE FROM security.login_attempts WHERE user_id = :userId")
SIGN_ON_USER_COUNT_QUERY = text("SELECT count(*) FROM security.users WHERE user_id = :userId")


class LockProbe(NamedTuple):
    """How long after the locking failure the lock is checked, and whether it still holds."""

    secondsAfterLock: int
    locked: bool


LOCK_PROBES = [
    LockProbe(secondsAfterLock=LOCKOUT_SECONDS - 1, locked=True),
    LockProbe(secondsAfterLock=LOCKOUT_SECONDS, locked=False),
    LockProbe(secondsAfterLock=LOCKOUT_SECONDS + 1, locked=False),
]
LOCK_PROBE_IDS = ["one-second-before-expiry", "at-locked-until", "one-second-after-expiry"]


@dataclass
class FakeClock:
    """The throttle's clock in these tests: answers ``current`` until it is advanced."""

    current: datetime

    def Now(self) -> datetime:
        """Return the current fake instant."""
        return self.current

    def Advance(self, seconds: int) -> None:
        """Move the clock forward by ``seconds``."""
        self.current += timedelta(seconds=seconds)


@pytest.fixture(name="throttleClock")
def ThrottleClock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Return a FakeClock at CLOCK_START that login_throttle.UtcNow answers from."""
    fakeClock = FakeClock(current=CLOCK_START)
    monkeypatch.setattr(login_throttle, "UtcNow", fakeClock.Now)
    return fakeClock


@pytest.fixture(name="throttleKey")
def ThrottleKey(syncEngine: Engine, migratedDatabase: object) -> Iterator[str]:
    """Yield a generated upper-case user ID; delete its security.login_attempts row after."""
    userId = secrets.token_hex(USER_ID_HEX_BYTES).upper()
    yield userId
    with syncEngine.begin() as connection:
        connection.execute(DELETE_ATTEMPT_ROW_STATEMENT, {"userId": userId})


def ReadAttempts(syncEngine: Engine, userId: str) -> RowMapping | None:
    """Return the committed security.login_attempts row of ``userId``, or None when absent."""
    with syncEngine.connect() as connection:
        result = connection.execute(ATTEMPT_ROW_QUERY, {"userId": userId})
        return result.mappings().one_or_none()


def CountSignOnUsers(syncEngine: Engine, userId: str) -> int:
    """Return how many security.users rows carry ``userId``."""
    with syncEngine.connect() as connection:
        return connection.execute(SIGN_ON_USER_COUNT_QUERY, {"userId": userId}).scalar_one()


async def RecordFailures(
    context: OnlineContext, userId: str, failureCount: int, clock: FakeClock
) -> None:
    """Record ``failureCount`` failed sign-ons of ``userId``, FAILURE_INTERVAL_SECONDS apart."""
    for _ in range(failureCount):
        clock.Advance(FAILURE_INTERVAL_SECONDS)
        await login_throttle.RecordFailure(context, userId)


async def AdmitCheck(context: OnlineContext, userId: str) -> None:
    """Pass the throttle's lock check for ``userId``, as cosgn00c does before the password check.

    The module has no separate lock query: AcquireCheckSlot is its lock check, raising the 429
    while the lock lasts, and the slot it returns is released at once.
    """
    checkSlot = await login_throttle.AcquireCheckSlot(context, userId)
    await login_throttle.ReleaseCheckSlot(checkSlot)


async def AssertCheckLocked(context: OnlineContext, userId: str) -> None:
    """Assert that the lock check of ``userId`` answers the 429 lockout on the user ID field."""
    with pytest.raises(LegacyMessageError) as excInfo:
        await AdmitCheck(context, userId)
    lockedBody = excInfo.value.Body()
    assert excInfo.value.httpStatus == LOCKED_STATUS
    assert lockedBody["message"] == LOCKED_MESSAGE
    assert lockedBody["cursor_field"] == USER_ID_FIELD


def test_expectations_match_the_configured_throttle(testSettings: Settings) -> None:
    """The limits and literal these tests expect are the settings and message the app uses."""
    assert testSettings.LOGIN_MAX_ATTEMPTS == MAX_ATTEMPTS
    assert testSettings.LOGIN_LOCKOUT_SECONDS == LOCKOUT_SECONDS
    assert messages.LOGIN_LOCKED_MESSAGE == LOCKED_MESSAGE


async def test_failures_below_the_limit_are_counted_without_a_lock(
    makeOnlineContext: ContextFactory,
    throttleClock: FakeClock,
    throttleKey: str,
    syncEngine: Engine,
) -> None:
    """Four failures leave the ID unlocked; its lock check passes and counts nothing."""
    context = await makeOnlineContext()
    await RecordFailures(context, throttleKey, MAX_ATTEMPTS - 1, throttleClock)
    attemptRow = ReadAttempts(syncEngine, throttleKey)
    assert attemptRow["failure_count"] == MAX_ATTEMPTS - 1
    assert attemptRow["locked_until"] is None
    assert attemptRow["last_failure_at"] == throttleClock.current
    await AdmitCheck(context, throttleKey)
    assert ReadAttempts(syncEngine, throttleKey) == attemptRow


async def test_failure_reaching_the_limit_locks_the_user_id(
    makeOnlineContext: ContextFactory,
    throttleClock: FakeClock,
    throttleKey: str,
    syncEngine: Engine,
) -> None:
    """The fifth failure locks the ID for 900 seconds; a refused check counts nothing."""
    context = await makeOnlineContext()
    await RecordFailures(context, throttleKey, MAX_ATTEMPTS, throttleClock)
    attemptRow = ReadAttempts(syncEngine, throttleKey)
    assert attemptRow["failure_count"] == MAX_ATTEMPTS
    assert attemptRow["locked_until"] == throttleClock.current + LOCKOUT_PERIOD
    assert attemptRow["last_failure_at"] == throttleClock.current
    await AssertCheckLocked(context, throttleKey)
    assert ReadAttempts(syncEngine, throttleKey) == attemptRow


@pytest.mark.parametrize("lockProbe", LOCK_PROBES, ids=LOCK_PROBE_IDS)
async def test_lock_lasts_until_locked_until(
    makeOnlineContext: ContextFactory,
    throttleClock: FakeClock,
    throttleKey: str,
    lockProbe: LockProbe,
) -> None:
    """The lock holds while locked_until is later than now, and ends at locked_until."""
    context = await makeOnlineContext()
    await RecordFailures(context, throttleKey, MAX_ATTEMPTS, throttleClock)
    throttleClock.Advance(lockProbe.secondsAfterLock)
    if lockProbe.locked:
        await AssertCheckLocked(context, throttleKey)
    else:
        await AdmitCheck(context, throttleKey)


async def test_failure_while_locked_is_not_counted(
    makeOnlineContext: ContextFactory,
    throttleClock: FakeClock,
    throttleKey: str,
    syncEngine: Engine,
) -> None:
    """A failure recorded before the lock expires leaves the count and the lock unchanged."""
    context = await makeOnlineContext()
    await RecordFailures(context, throttleKey, MAX_ATTEMPTS, throttleClock)
    lockedRow = ReadAttempts(syncEngine, throttleKey)
    await RecordFailures(context, throttleKey, 1, throttleClock)
    assert ReadAttempts(syncEngine, throttleKey) == lockedRow


async def test_first_failure_after_the_lock_expires_restarts_the_count(
    makeOnlineContext: ContextFactory,
    throttleClock: FakeClock,
    throttleKey: str,
    syncEngine: Engine,
) -> None:
    """Once the lock has expired, the next failure counts one and clears locked_until."""
    context = await makeOnlineContext()
    await RecordFailures(context, throttleKey, MAX_ATTEMPTS, throttleClock)
    throttleClock.Advance(LOCKOUT_SECONDS)
    await RecordFailures(context, throttleKey, 1, throttleClock)
    attemptRow = ReadAttempts(syncEngine, throttleKey)
    assert attemptRow["failure_count"] == 1
    assert attemptRow["locked_until"] is None
    assert attemptRow["last_failure_at"] == throttleClock.current


async def test_reset_deletes_the_row(
    makeOnlineContext: ContextFactory,
    throttleClock: FakeClock,
    throttleKey: str,
    syncEngine: Engine,
) -> None:
    """A success deletes the row, resetting an absent row is no error, and counting restarts."""
    context = await makeOnlineContext()
    await RecordFailures(context, throttleKey, MAX_ATTEMPTS - 1, throttleClock)
    await login_throttle.ResetAttempts(context, throttleKey)
    assert ReadAttempts(syncEngine, throttleKey) is None
    await login_throttle.ResetAttempts(context, throttleKey)
    assert ReadAttempts(syncEngine, throttleKey) is None
    await RecordFailures(context, throttleKey, 1, throttleClock)
    assert ReadAttempts(syncEngine, throttleKey)["failure_count"] == 1


async def test_failure_of_an_unknown_user_id_is_counted(
    makeOnlineContext: ContextFactory,
    throttleClock: FakeClock,
    throttleKey: str,
    syncEngine: Engine,
) -> None:
    """An ID with no security.users row gets its own row: the table has no foreign key."""
    context = await makeOnlineContext()
    assert CountSignOnUsers(syncEngine, throttleKey) == 0
    await RecordFailures(context, throttleKey, 1, throttleClock)
    attemptRow = ReadAttempts(syncEngine, throttleKey)
    assert attemptRow["failure_count"] == 1
    assert attemptRow["locked_until"] is None
    assert attemptRow["last_failure_at"] == throttleClock.current
