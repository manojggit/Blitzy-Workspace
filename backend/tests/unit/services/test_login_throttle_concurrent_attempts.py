"""Unit tests for the CC00 (COSGN00C) login throttle under concurrent sign-on attempts.

cbl/COSGN00C.cbl PROCESS-ENTER-KEY (lines 108-140) edits the user ID, then the password, and
READ-USER-SEC-FILE (lines 209-257) reads USRSEC and compares the password. The throttle that
guards it has no legacy source (AAP 0.7.1): LOGIN_MAX_ATTEMPTS failures lock the upper-cased
user ID for LOGIN_LOCKOUT_SECONDS, a locked ID answers 429 with
``Too many failed attempts. Try again later ...`` on the user ID, and a success resets the
count. Every attempt is counted before its edits and its password check, so:
- a concurrent burst of wrong passwords checks exactly LOGIN_MAX_ATTEMPTS of them (401
    ``Wrong Password. Try again ...``) and answers 429 to the rest, and the lock runs
    LOGIN_LOCKOUT_SECONDS from the attempt that reached the limit;
- a burst that includes the correct password checks LOGIN_MAX_ATTEMPTS passwords at most, and
    the correct password sent once the limit is reserved, or after the lock, answers 429;
- an attempt of a locked ID is not counted;
- once the lock has expired the correct password signs on and deletes the row, and a wrong one
    restarts the count at one;
- a success after one failure short of the limit deletes the row;
- the blank-field edits (``Please enter User ID ...``, ``Please enter Password ...``) count
    once each, so LOGIN_MAX_ATTEMPTS of them lock the ID.

Every burst holds each counted attempt until the whole burst's reservations are decided, as a
bcrypt compare far slower than a reservation does, so each burst outcome is deterministic. Each
attempt runs cosgn00c.SignOn with an OnlineContext of its own against the migrated, seeded test
database. createUser generates the user and its password, and restoreSeedState truncates
security.login_attempts with every other table afterwards.
"""

import asyncio
import secrets
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import NamedTuple, Protocol
from unittest import mock

import pytest
import pytest_asyncio
from sqlalchemy import Engine, text

from app.core.config import Settings
from app.core.dependencies import OnlineContext
from app.core.errors import LegacyMessageError
from app.schemas.auth import LoginRequest
from app.services.auth import cosgn00c, login_throttle

REGULAR_USER_TYPE = "U"
# Attempts a burst sends beyond LOGIN_MAX_ATTEMPTS: 15 with the default limit of 5, the size of
# the reported mixed burst.
BURST_EXTRA_ATTEMPTS = 10
# The 0-based position of the reported burst's correct guess (#12 of 15).
REPORTED_CORRECT_POSITION = 11
LAST_POSITION = -1
BARRIER_TIMEOUT_SECONDS = 30
# How far past the stored lock expiry the throttle's clock is moved.
CLOCK_STEP = timedelta(seconds=1)
WRONG_CREDENTIAL_HEX_BYTES = 4

# COSGN00C literals (lines 120, 125, 249), the throttle literal, and MOVE -1 TO USERIDL / PASSWDL.
USER_ID_REQUIRED_MESSAGE = "Please enter User ID ..."
CREDENTIAL_REQUIRED_MESSAGE = "Please enter Password ..."
WRONG_CREDENTIAL_MESSAGE = "Wrong Password. Try again ..."
LOCKED_MESSAGE = "Too many failed attempts. Try again later ..."
USER_ID_FIELD = "user_id"
CREDENTIAL_FIELD = "password"
SIGN_ON_FAILURE_STATUS = 401
LOCKED_STATUS = 429
SIGNED_ON_STATUS = 200
NO_MESSAGE = ""
NO_FIELD = ""
# A blank map field: empty, or spaces only. A blank user ID is throttled as the blank ID.
BLANK_FIELD = ""
SPACES_FIELD = "        "

ATTEMPT_ROW_QUERY = text(
    "SELECT failure_count, locked_until, last_failure_at FROM security.login_attempts "
    "WHERE user_id = :userId"
)

REAL_CREDENTIAL_MATCHES = cosgn00c.CredentialMatches
REAL_RESERVE_ATTEMPT = login_throttle.ReserveAttempt


class GeneratedUser(Protocol):
    """The sign-on user createUser commits: its ID and the password it was created with."""

    userId: str
    password: str


class SignOnOutcome(NamedTuple):
    """What COSGN00 shows: the status, literal and cursor field, or the signed-on user ID."""

    status: int
    message: str
    cursorField: str
    signedOnUserId: str = ""


class AttemptInput(NamedTuple):
    """The keyed map fields of one attempt and the user ID the throttle counts it against."""

    userId: str
    credential: str
    throttledUserId: str


class AttemptRow(NamedTuple):
    """A committed security.login_attempts row."""

    failureCount: int
    lockedUntil: datetime | None
    lastFailureAt: datetime | None


WRONG_OUTCOME = SignOnOutcome(SIGN_ON_FAILURE_STATUS, WRONG_CREDENTIAL_MESSAGE, CREDENTIAL_FIELD)
LOCKED_OUTCOME = SignOnOutcome(LOCKED_STATUS, LOCKED_MESSAGE, USER_ID_FIELD)
BLANK_USER_ID_OUTCOME = SignOnOutcome(
    SIGN_ON_FAILURE_STATUS, USER_ID_REQUIRED_MESSAGE, USER_ID_FIELD
)
BLANK_CREDENTIAL_OUTCOME = SignOnOutcome(
    SIGN_ON_FAILURE_STATUS, CREDENTIAL_REQUIRED_MESSAGE, CREDENTIAL_FIELD
)


class ReservationBarrier:
    """Wraps login_throttle.ReserveAttempt so no counted attempt goes on alone.

    Each counted attempt waits until all ``burstSize`` reservations are decided, counted or
    refused. ``limitReserved`` is set once ``maxAttempts`` reservations are counted.
    """

    def __init__(self, burstSize: int, maxAttempts: int) -> None:
        self.burstSize = burstSize
        self.maxAttempts = maxAttempts
        self.decidedCount = 0
        self.countedCount = 0
        self.allDecided = asyncio.Event()
        self.limitReserved = asyncio.Event()

    def CountDecision(self) -> None:
        """Count one decided reservation; release the waiting attempts after the last one."""
        self.decidedCount += 1
        if self.decidedCount == self.burstSize:
            self.allDecided.set()

    def CountReservation(self) -> None:
        """Count one counted attempt; mark the limit reserved once there are maxAttempts."""
        self.countedCount += 1
        if self.countedCount == self.maxAttempts:
            self.limitReserved.set()

    async def ReserveAttempt(self, context: OnlineContext, userId: str) -> None:
        """Run the real reservation, then hold a counted attempt until the burst is decided."""
        try:
            await REAL_RESERVE_ATTEMPT(context, userId)
        finally:
            self.CountDecision()
        self.CountReservation()
        await asyncio.wait_for(self.allDecided.wait(), BARRIER_TIMEOUT_SECONDS)


async def SignOnOutcomeOf(context: OnlineContext, attemptInput: AttemptInput) -> SignOnOutcome:
    """Press ENTER on COSGN00 through cosgn00c.SignOn and return what the screen shows."""
    request = LoginRequest(user_id=attemptInput.userId, password=attemptInput.credential)
    try:
        response = await cosgn00c.SignOn(context, request)
    except LegacyMessageError as error:
        return SignOnOutcome(error.httpStatus, error.message, error.field)
    return SignOnOutcome(SIGNED_ON_STATUS, NO_MESSAGE, NO_FIELD, response.user_id)


def OtherCredential(credential: str) -> str:
    """Return a generated 8-character upper-case password that differs from ``credential``."""
    otherCredential = credential
    while otherCredential == credential:
        otherCredential = secrets.token_hex(WRONG_CREDENTIAL_HEX_BYTES).upper()
    return otherCredential


def FrozenClock(instant: datetime) -> Callable[[], datetime]:
    """Return a stand-in for login_throttle.UtcNow that always answers ``instant``."""

    def Now() -> datetime:
        return instant

    return Now


class ThrottleHarness:
    """Signs a generated user on through cosgn00c.SignOn, each attempt with its own context.

    ``checkSpy`` replaces cosgn00c.CredentialMatches, counting every password check.
    """

    def __init__(
        self,
        user: GeneratedUser,
        makeContext: Callable[..., Awaitable[OnlineContext]],
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.user = user
        self.makeContext = makeContext
        self.settings = settings
        self.monkeypatch = monkeypatch
        self.wrongCredential = OtherCredential(user.password)
        self.checkSpy = mock.AsyncMock(side_effect=REAL_CREDENTIAL_MATCHES)
        monkeypatch.setattr(cosgn00c, "CredentialMatches", self.checkSpy)

    async def Attempt(self, attemptInput: AttemptInput) -> SignOnOutcome:
        """Sign on once, with a context of its own."""
        context = await self.makeContext(REGULAR_USER_TYPE)
        return await SignOnOutcomeOf(context, attemptInput)

    async def FailSequentially(self, attempts: int) -> list[SignOnOutcome]:
        """Sign on with the wrong password ``attempts`` times, one after another."""
        wrongAttempt = WrongPasswordAttempt(self)
        return [await self.Attempt(wrongAttempt) for _ in range(attempts)]

    async def AttemptBurst(self, credentials: Sequence[str]) -> list[SignOnOutcome]:
        """Sign the user on concurrently, once per credential, each with a context of its own."""
        contexts = [await self.makeContext(REGULAR_USER_TYPE) for _ in credentials]
        userId = self.user.userId
        attempts = [
            SignOnOutcomeOf(context, AttemptInput(userId, credential, userId))
            for context, credential in zip(contexts, credentials, strict=True)
        ]
        return list(await asyncio.gather(*attempts))

    async def AttemptOnceLimitReserved(
        self, barrier: ReservationBarrier, attemptInput: AttemptInput
    ) -> SignOnOutcome:
        """Sign on once ``barrier`` has counted LOGIN_MAX_ATTEMPTS attempts of the burst."""
        context = await self.makeContext(REGULAR_USER_TYPE)
        await asyncio.wait_for(barrier.limitReserved.wait(), BARRIER_TIMEOUT_SECONDS)
        return await SignOnOutcomeOf(context, attemptInput)

    def InstallBarrier(self, burstSize: int) -> ReservationBarrier:
        """Hold every counted attempt until ``burstSize`` reservations are decided."""
        barrier = ReservationBarrier(burstSize, self.settings.LOGIN_MAX_ATTEMPTS)
        self.monkeypatch.setattr(login_throttle, "ReserveAttempt", barrier.ReserveAttempt)
        return barrier

    def SignedOnOutcome(self) -> SignOnOutcome:
        """Return the outcome of the generated user signing on."""
        return SignOnOutcome(SIGNED_ON_STATUS, NO_MESSAGE, NO_FIELD, self.user.userId)


def CorrectPasswordAttempt(harness: ThrottleHarness) -> AttemptInput:
    """The generated user's ID and the password it was created with."""
    userId = harness.user.userId
    return AttemptInput(userId, harness.user.password, userId)


def WrongPasswordAttempt(harness: ThrottleHarness) -> AttemptInput:
    """The generated user's ID with a generated password that is not its own."""
    userId = harness.user.userId
    return AttemptInput(userId, harness.wrongCredential, userId)


def BlankPasswordAttempt(harness: ThrottleHarness) -> AttemptInput:
    """The generated user's ID with an empty password field."""
    userId = harness.user.userId
    return AttemptInput(userId, BLANK_FIELD, userId)


def SpacesPasswordAttempt(harness: ThrottleHarness) -> AttemptInput:
    """The generated user's ID with a password field of spaces only."""
    userId = harness.user.userId
    return AttemptInput(userId, SPACES_FIELD, userId)


def LowerCaseUserIdAttempt(harness: ThrottleHarness) -> AttemptInput:
    """The generated user's ID keyed in lower case, with its password; counted upper-cased."""
    userId = harness.user.userId
    return AttemptInput(userId.lower(), harness.user.password, userId)


def BlankUserIdAttempt(harness: ThrottleHarness) -> AttemptInput:
    """An empty user ID field with the generated user's password; counted as the blank ID."""
    return AttemptInput(BLANK_FIELD, harness.user.password, BLANK_FIELD)


class BlankFieldCase(NamedTuple):
    """A blank-field attempt, the edit literal it gets, and the attempt probed once locked."""

    buildAttempt: Callable[[ThrottleHarness], AttemptInput]
    outcome: SignOnOutcome
    buildProbe: Callable[[ThrottleHarness], AttemptInput]


LOCKED_ID_ATTEMPTS = [
    CorrectPasswordAttempt,
    WrongPasswordAttempt,
    BlankPasswordAttempt,
    LowerCaseUserIdAttempt,
]
LOCKED_ID_ATTEMPT_IDS = ["correct-password", "wrong-password", "blank-password", "lower-case-id"]
CORRECT_POSITIONS = [0, REPORTED_CORRECT_POSITION, LAST_POSITION]
CORRECT_POSITION_IDS = ["first", "twelfth-of-fifteen", "last"]
BLANK_FIELD_CASES = [
    BlankFieldCase(BlankPasswordAttempt, BLANK_CREDENTIAL_OUTCOME, CorrectPasswordAttempt),
    BlankFieldCase(SpacesPasswordAttempt, BLANK_CREDENTIAL_OUTCOME, CorrectPasswordAttempt),
    BlankFieldCase(BlankUserIdAttempt, BLANK_USER_ID_OUTCOME, BlankUserIdAttempt),
]
BLANK_FIELD_CASE_IDS = ["empty-password", "spaces-password", "empty-user-id"]


@pytest_asyncio.fixture(name="throttle")
async def ThrottleHarnessFixture(
    createUser: Callable[[str], GeneratedUser],
    makeOnlineContext: Callable[..., Awaitable[OnlineContext]],
    testSettings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> ThrottleHarness:
    """Return the harness of a generated regular user, created off the event loop."""
    user = await asyncio.to_thread(createUser, REGULAR_USER_TYPE)
    return ThrottleHarness(user, makeOnlineContext, testSettings, monkeypatch)


def StoredAttemptRow(syncEngine: Engine, userId: str) -> AttemptRow | None:
    """Return the committed security.login_attempts row of ``userId``, or None."""
    with syncEngine.connect() as connection:
        row = connection.execute(ATTEMPT_ROW_QUERY, {"userId": userId}).one_or_none()
    if row is None:
        return None
    return AttemptRow(*row)


def AssertLockedAtTheLimit(settings: Settings, row: AttemptRow | None) -> None:
    """Assert LOGIN_MAX_ATTEMPTS counted and a lock LOGIN_LOCKOUT_SECONDS after the last one."""
    assert row is not None
    assert row.failureCount == settings.LOGIN_MAX_ATTEMPTS
    assert row.lockedUntil == row.lastFailureAt + timedelta(seconds=settings.LOGIN_LOCKOUT_SECONDS)


async def LockThenExpire(throttle: ThrottleHarness, syncEngine: Engine) -> datetime:
    """Lock the user ID by wrong passwords, then move the throttle's clock past the lock."""
    await throttle.FailSequentially(throttle.settings.LOGIN_MAX_ATTEMPTS)
    lockedRow = StoredAttemptRow(syncEngine, throttle.user.userId)
    AssertLockedAtTheLimit(throttle.settings, lockedRow)
    afterExpiry = lockedRow.lockedUntil + CLOCK_STEP
    throttle.monkeypatch.setattr(login_throttle, "UtcNow", FrozenClock(afterExpiry))
    return afterExpiry


async def test_concurrent_wrong_passwords_check_only_the_attempt_limit(throttle, syncEngine):
    """Exactly LOGIN_MAX_ATTEMPTS wrong passwords are checked; the rest of the burst gets 429."""
    maxAttempts = throttle.settings.LOGIN_MAX_ATTEMPTS
    burstSize = maxAttempts + BURST_EXTRA_ATTEMPTS
    throttle.InstallBarrier(burstSize)
    outcomes = await throttle.AttemptBurst([throttle.wrongCredential] * burstSize)
    expectedOutcomes = {WRONG_OUTCOME: maxAttempts, LOCKED_OUTCOME: burstSize - maxAttempts}
    assert Counter(outcomes) == Counter(expectedOutcomes)
    assert throttle.checkSpy.await_count == maxAttempts
    AssertLockedAtTheLimit(throttle.settings, StoredAttemptRow(syncEngine, throttle.user.userId))


@pytest.mark.parametrize("correctPosition", CORRECT_POSITIONS, ids=CORRECT_POSITION_IDS)
async def test_burst_with_the_correct_password_checks_no_more_than_the_limit(
    throttle, correctPosition
):
    """Wherever the correct guess sits, the burst's 401s and its one 200 at most total the limit."""
    maxAttempts = throttle.settings.LOGIN_MAX_ATTEMPTS
    credentials = [throttle.wrongCredential] * (maxAttempts + BURST_EXTRA_ATTEMPTS - 1)
    credentials.insert(correctPosition, throttle.user.password)
    throttle.InstallBarrier(len(credentials))
    outcomes = Counter(await throttle.AttemptBurst(credentials))
    signedOnCount = outcomes[throttle.SignedOnOutcome()]
    assert signedOnCount <= 1
    assert outcomes[WRONG_OUTCOME] + signedOnCount == maxAttempts
    assert outcomes[LOCKED_OUTCOME] == len(credentials) - maxAttempts
    assert throttle.checkSpy.await_count == maxAttempts


async def test_correct_password_once_the_limit_is_reserved_answers_429(throttle, syncEngine):
    """The correct guess sent while the counted checks are in flight, and after them, gets 429."""
    maxAttempts = throttle.settings.LOGIN_MAX_ATTEMPTS
    wrongCredentials = [throttle.wrongCredential] * (maxAttempts + BURST_EXTRA_ATTEMPTS)
    barrier = throttle.InstallBarrier(len(wrongCredentials) + 1)
    burstOutcomes, lateOutcome = await asyncio.gather(
        throttle.AttemptBurst(wrongCredentials),
        throttle.AttemptOnceLimitReserved(barrier, CorrectPasswordAttempt(throttle)),
    )
    assert lateOutcome == LOCKED_OUTCOME
    assert Counter(burstOutcomes)[WRONG_OUTCOME] == maxAttempts
    assert await throttle.Attempt(CorrectPasswordAttempt(throttle)) == LOCKED_OUTCOME
    assert throttle.checkSpy.await_count == maxAttempts
    AssertLockedAtTheLimit(throttle.settings, StoredAttemptRow(syncEngine, throttle.user.userId))


@pytest.mark.parametrize("buildAttempt", LOCKED_ID_ATTEMPTS, ids=LOCKED_ID_ATTEMPT_IDS)
async def test_attempt_of_a_locked_id_answers_429_and_is_not_counted(
    throttle, syncEngine, buildAttempt
):
    """While the lock lasts every attempt gets 429, its password unchecked and the row unchanged."""
    maxAttempts = throttle.settings.LOGIN_MAX_ATTEMPTS
    assert await throttle.FailSequentially(maxAttempts) == [WRONG_OUTCOME] * maxAttempts
    lockedRow = StoredAttemptRow(syncEngine, throttle.user.userId)
    AssertLockedAtTheLimit(throttle.settings, lockedRow)
    assert await throttle.Attempt(buildAttempt(throttle)) == LOCKED_OUTCOME
    assert StoredAttemptRow(syncEngine, throttle.user.userId) == lockedRow
    assert throttle.checkSpy.await_count == maxAttempts


async def test_correct_password_after_the_lock_expires_signs_on_and_deletes_the_row(
    throttle, syncEngine
):
    """An expired lock no longer refuses: the correct password signs on and resets the row."""
    await LockThenExpire(throttle, syncEngine)
    outcome = await throttle.Attempt(CorrectPasswordAttempt(throttle))
    assert outcome == throttle.SignedOnOutcome()
    assert StoredAttemptRow(syncEngine, throttle.user.userId) is None


async def test_wrong_password_after_the_lock_expires_restarts_the_count_at_one(
    throttle, syncEngine
):
    """An expired lock is cleared and the next failure is counted as the first."""
    afterExpiry = await LockThenExpire(throttle, syncEngine)
    assert await throttle.Attempt(WrongPasswordAttempt(throttle)) == WRONG_OUTCOME
    restartedRow = AttemptRow(failureCount=1, lockedUntil=None, lastFailureAt=afterExpiry)
    assert StoredAttemptRow(syncEngine, throttle.user.userId) == restartedRow


async def test_success_one_failure_short_of_the_limit_deletes_the_row(throttle, syncEngine):
    """LOGIN_MAX_ATTEMPTS - 1 failures, then the correct password: signed on, row deleted."""
    failureCount = throttle.settings.LOGIN_MAX_ATTEMPTS - 1
    assert await throttle.FailSequentially(failureCount) == [WRONG_OUTCOME] * failureCount
    storedRow = StoredAttemptRow(syncEngine, throttle.user.userId)
    assert (storedRow.failureCount, storedRow.lockedUntil) == (failureCount, None)
    outcome = await throttle.Attempt(CorrectPasswordAttempt(throttle))
    assert outcome == throttle.SignedOnOutcome()
    assert StoredAttemptRow(syncEngine, throttle.user.userId) is None


@pytest.mark.parametrize("blankFieldCase", BLANK_FIELD_CASES, ids=BLANK_FIELD_CASE_IDS)
async def test_blank_field_edits_count_once_each_and_lock_the_id(
    throttle, syncEngine, blankFieldCase
):
    """Each blank-field edit adds exactly one; the edit that reaches the limit locks the ID."""
    attemptInput = blankFieldCase.buildAttempt(throttle)
    for attemptNumber in range(1, throttle.settings.LOGIN_MAX_ATTEMPTS + 1):
        assert await throttle.Attempt(attemptInput) == blankFieldCase.outcome
        storedRow = StoredAttemptRow(syncEngine, attemptInput.throttledUserId)
        assert storedRow.failureCount == attemptNumber
    AssertLockedAtTheLimit(throttle.settings, storedRow)
    assert await throttle.Attempt(blankFieldCase.buildProbe(throttle)) == LOCKED_OUTCOME
    assert throttle.checkSpy.await_count == 0
