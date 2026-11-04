"""Tests of the CC00 (COSGN00C) login throttle under concurrent sign-on attempts.

cbl/COSGN00C.cbl PROCESS-ENTER-KEY (lines 108-140) edits the user ID, then the password, and
READ-USER-SEC-FILE (lines 209-257) reads USRSEC and compares the password. The throttle that
guards it has no legacy source (AAP 0.7.1): each failure counts against the upper-cased user
ID, LOGIN_MAX_ATTEMPTS failures lock it for LOGIN_LOCKOUT_SECONDS, a locked ID answers 429 with
``Too many failed attempts. Try again later ...`` on the user ID, and a success resets the
count. The failures counted since the last reset plus the password checks in flight never
exceed LOGIN_MAX_ATTEMPTS, and an attempt over the limit waits for the checks in flight, so:
- concurrent correct sign-ons of one user ID all sign on, never more than LOGIN_MAX_ATTEMPTS
    of them checking at once, and leave no row;
- a concurrent burst of wrong passwords checks exactly LOGIN_MAX_ATTEMPTS of them (401
    ``Wrong Password. Try again ...``) and answers 429 to the rest, and the lock runs
    LOGIN_LOCKOUT_SECONDS from the failure that reached the limit;
- the correct password sent while the limit's checks are in flight waits for them and then
    answers 429, as does a later one;
- an attempt of a locked ID is not counted;
- once the lock has expired the correct password signs on and deletes the row, and a wrong one
    restarts the count at one;
- a success after one failure short of the limit deletes the row;
- each blank-password edit (``Please enter Password ...``) counts one failure, so
    LOGIN_MAX_ATTEMPTS of them lock the ID;
- a blank user ID (``Please enter User ID ...``) is answered before the throttle, so however
    many are sent it is never counted, never refused with 429 and never checks a password;
- an attempt still waiting at the throttle's wait limit answers 503
    ``Unable to verify the User ...``, uncounted and unchecked;
- an admitted attempt whose USRSEC read or reset fails answers that 503 and counts one failure;
- a failed admission or failure write answers that 503, and no check marker outlives its
    attempt, even when releasing it fails.

CheckGate holds each password check until LOGIN_MAX_ATTEMPTS checks are in flight together, as
a bcrypt compare far slower than an admission does, so each burst outcome is deterministic, and
it records the most checks ever in flight. Every attempt is a POST /api/v1/auth/login of
apiClient, so it runs cosgn00c.SignOn in a request of its own, under the request admission
that bounds the pooled connections of a burst. createUser generates the user and its password,
and restoreSeedState truncates security.login_attempts with every other table afterwards.
"""

import asyncio
import contextlib
import logging
import secrets
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import datetime, timedelta
from typing import NamedTuple, Protocol
from unittest import mock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import Engine, Executable, text

from app.core.config import Settings
from app.core.dependencies import OnlineContext
from app.core.errors import FileIoError
from app.repositories.user_repository import USER_FILE_NAME, UserRepository
from app.services.auth import cosgn00c, login_throttle

REGULAR_USER_TYPE = "U"
LOGIN_PATH = "/api/v1/auth/login"
# Attempts a burst sends beyond LOGIN_MAX_ATTEMPTS: 15 with the default limit of 5, the size of
# the reported mixed burst.
BURST_EXTRA_ATTEMPTS = 10
GATE_TIMEOUT_SECONDS = 30
# How far past the stored lock expiry the throttle's clock is moved.
CLOCK_STEP = timedelta(seconds=1)
WRONG_CREDENTIAL_HEX_BYTES = 4
# The throttle's wait limit while the test holds check markers of the user ID.
SHORT_WAIT_SECONDS = 0.3
# How long a discarded connection's check marker may take to leave pg_locks, and the poll.
MARKER_DROP_TIMEOUT_SECONDS = 10
MARKER_POLL_SECONDS = 0.05

# COSGN00C literals (lines 120, 125, 249, 254), the throttle literal, and MOVE -1 TO USERIDL /
# PASSWDL.
USER_ID_REQUIRED_MESSAGE = "Please enter User ID ..."
CREDENTIAL_REQUIRED_MESSAGE = "Please enter Password ..."
WRONG_CREDENTIAL_MESSAGE = "Wrong Password. Try again ..."
UNABLE_TO_VERIFY_MESSAGE = "Unable to verify the User ..."
LOCKED_MESSAGE = "Too many failed attempts. Try again later ..."
USER_ID_FIELD = "user_id"
CREDENTIAL_FIELD = "password"
SIGN_ON_FAILURE_STATUS = 401
LOCKED_STATUS = 429
FILE_ERROR_STATUS = 503
SIGNED_ON_STATUS = 200
NO_MESSAGE = ""
NO_FIELD = ""
# A blank map field: empty, or spaces only. A blank user ID is never throttled: no row is keyed.
BLANK_FIELD = ""
SPACES_FIELD = "        "
# The no-failure row an admission inserts, left by an attempt whose failure write failed.
NO_FAILURE_COUNT = 0
UNLOCK_FAILED_LOG_TEXT = "Sign-on check-slot unlock failed"

ATTEMPT_ROW_QUERY = text(
    "SELECT failure_count, locked_until, last_failure_at FROM security.login_attempts "
    "WHERE user_id = :userId"
)
# Statements PostgreSQL refuses with division_by_zero (SQLSTATE 22012) when they run: they
# stand in for a throttle statement that fails on the server. The first takes the parameters of
# a check-marker statement, the second takes none, as the failure upsert.
FAILING_SLOT_STATEMENT = login_throttle.CheckSlotStatement("SELECT :lockSpace / 0, :userId")
FAILING_STATEMENT = text("SELECT 1 / 0")

REAL_CREDENTIAL_MATCHES = cosgn00c.CredentialMatches
REAL_DECIDE_ADMISSION = login_throttle.DecideAdmission
# The throttle's own marker statements, kept for the test's checks while a case replaces them.
REAL_CHECKS_IN_FLIGHT_STATEMENT = login_throttle.CHECKS_IN_FLIGHT_STATEMENT
REAL_TAKE_CHECK_SLOT_STATEMENT = login_throttle.TAKE_CHECK_SLOT_STATEMENT
REAL_RELEASE_CHECK_SLOT_STATEMENT = login_throttle.RELEASE_CHECK_SLOT_STATEMENT


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
UNABLE_TO_VERIFY_OUTCOME = SignOnOutcome(FILE_ERROR_STATUS, UNABLE_TO_VERIFY_MESSAGE, USER_ID_FIELD)
BLANK_USER_ID_OUTCOME = SignOnOutcome(
    SIGN_ON_FAILURE_STATUS, USER_ID_REQUIRED_MESSAGE, USER_ID_FIELD
)
BLANK_CREDENTIAL_OUTCOME = SignOnOutcome(
    SIGN_ON_FAILURE_STATUS, CREDENTIAL_REQUIRED_MESSAGE, CREDENTIAL_FIELD
)


class CheckGate:
    """Wraps cosgn00c.CredentialMatches, holding each password check until the gate opens.

    A full gate has ``heldTogether`` checks in flight together; it opens by itself then when
    ``opensWhenFull`` is set, and otherwise once Open is called. ``peakInFlight`` is the most
    checks ever in flight at once.
    """

    def __init__(self, heldTogether: int, opensWhenFull: bool) -> None:
        self.heldTogether = heldTogether
        self.opensWhenFull = opensWhenFull
        self.inFlight = 0
        self.peakInFlight = 0
        self.full = asyncio.Event()
        self.opened = asyncio.Event()

    def CountEntry(self) -> None:
        """Count one more check in flight; mark the gate full, and open it if it opens itself."""
        self.inFlight += 1
        self.peakInFlight = max(self.peakInFlight, self.inFlight)
        if self.inFlight >= self.heldTogether:
            self.full.set()
            if self.opensWhenFull:
                self.opened.set()

    def Open(self) -> None:
        """Let every held check, and every later one, go on."""
        self.opened.set()

    async def CredentialMatches(self, upperCredential: str, passwordHash: str) -> bool:
        """Hold the check until the gate opens, then run the real bcrypt compare."""
        self.CountEntry()
        try:
            await asyncio.wait_for(self.opened.wait(), GATE_TIMEOUT_SECONDS)
            return await REAL_CREDENTIAL_MATCHES(upperCredential, passwordHash)
        finally:
            self.inFlight -= 1


class AdmissionWatch:
    """Wraps login_throttle.DecideAdmission and signals once it has refused an admission."""

    def __init__(self) -> None:
        self.refused = asyncio.Event()

    async def DecideAdmission(self, context: OnlineContext, slot: login_throttle.CheckSlot) -> bool:
        """Run the real decision; signal ``refused`` when the attempt has to wait."""
        admitted = await REAL_DECIDE_ADMISSION(context, slot)
        if not admitted:
            self.refused.set()
        return admitted


async def SignOnOutcomeOf(client: httpx.AsyncClient, attemptInput: AttemptInput) -> SignOnOutcome:
    """Press ENTER on COSGN00 through POST /api/v1/auth/login; return what the screen shows."""
    credentials = {"user_id": attemptInput.userId, "password": attemptInput.credential}
    response = await client.post(LOGIN_PATH, json=credentials)
    body = response.json()
    if response.status_code == SIGNED_ON_STATUS:
        return SignOnOutcome(SIGNED_ON_STATUS, NO_MESSAGE, NO_FIELD, body["user_id"])
    return SignOnOutcome(response.status_code, body["message"], body["cursor_field"])


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
    """Signs a generated user on through POST /api/v1/auth/login, each attempt a request.

    ``checkSpy`` replaces cosgn00c.CredentialMatches, counting every password check.
    """

    def __init__(
        self,
        user: GeneratedUser,
        client: httpx.AsyncClient,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.user = user
        self.client = client
        self.settings = settings
        self.monkeypatch = monkeypatch
        self.wrongCredential = OtherCredential(user.password)
        self.checkSpy = mock.AsyncMock(side_effect=REAL_CREDENTIAL_MATCHES)
        monkeypatch.setattr(cosgn00c, "CredentialMatches", self.checkSpy)

    async def Attempt(self, attemptInput: AttemptInput) -> SignOnOutcome:
        """Sign on once."""
        return await SignOnOutcomeOf(self.client, attemptInput)

    async def FailSequentially(self, attempts: int) -> list[SignOnOutcome]:
        """Sign on with the wrong password ``attempts`` times, one after another."""
        wrongAttempt = WrongPasswordAttempt(self)
        return [await self.Attempt(wrongAttempt) for _ in range(attempts)]

    async def AttemptBurst(self, credentials: Sequence[str]) -> list[SignOnOutcome]:
        """Sign the user on concurrently, once per credential."""
        userId = self.user.userId
        attempts = [
            self.Attempt(AttemptInput(userId, credential, userId)) for credential in credentials
        ]
        return list(await asyncio.gather(*attempts))

    def InstallGate(self, opensWhenFull: bool) -> CheckGate:
        """Hold every password check until LOGIN_MAX_ATTEMPTS of them are in flight together."""
        gate = CheckGate(self.settings.LOGIN_MAX_ATTEMPTS, opensWhenFull)
        self.checkSpy.side_effect = gate.CredentialMatches
        return gate

    def InstallAdmissionWatch(self) -> AdmissionWatch:
        """Signal the first admission login_throttle refuses."""
        watch = AdmissionWatch()
        self.monkeypatch.setattr(login_throttle, "DecideAdmission", watch.DecideAdmission)
        return watch

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
    """An empty user ID field with the generated user's password; the blank ID is never keyed."""
    return AttemptInput(BLANK_FIELD, harness.user.password, BLANK_FIELD)


def SpacesUserIdAttempt(harness: ThrottleHarness) -> AttemptInput:
    """A user ID field of spaces only with the generated user's password; never keyed either."""
    return AttemptInput(SPACES_FIELD, harness.user.password, BLANK_FIELD)


class BlankFieldCase(NamedTuple):
    """A blank-field attempt, the edit literal it gets, and the attempt probed once locked."""

    buildAttempt: Callable[[ThrottleHarness], AttemptInput]
    outcome: SignOnOutcome
    buildProbe: Callable[[ThrottleHarness], AttemptInput]


def OneOverTheLimit(maxAttempts: int) -> int:
    """Return one attempt more than LOGIN_MAX_ATTEMPTS: 6 with the default limit, as reported."""
    return maxAttempts + 1


def FourTimesTheLimit(maxAttempts: int) -> int:
    """Return four times LOGIN_MAX_ATTEMPTS: 20 with the default limit, as reported."""
    return maxAttempts * 4


def FailingFailureUpsert(userId: str, now: datetime, maxAttempts: int) -> Executable:
    """Stand in for login_throttle.BuildFailureUpsert with a statement that fails when run."""
    return FAILING_STATEMENT


class FailedOperationCase(NamedTuple):
    """An operation of an admitted sign-on that fails with IOERR, and the checks it leaves."""

    owner: object
    attributeName: str
    fileName: str
    passwordChecks: int


def RaisingIoError(fileName: str) -> Callable[..., Awaitable[None]]:
    """Return a stand-in for a database call that fails with the IOERR condition of fileName."""

    async def RaiseIoError(*arguments: object) -> None:
        raise FileIoError(fileName)

    return RaiseIoError


LOCKED_ID_ATTEMPTS = [
    CorrectPasswordAttempt,
    WrongPasswordAttempt,
    BlankPasswordAttempt,
    LowerCaseUserIdAttempt,
]
LOCKED_ID_ATTEMPT_IDS = ["correct-password", "wrong-password", "blank-password", "lower-case-id"]
CORRECT_BURST_SIZES = [OneOverTheLimit, FourTimesTheLimit]
CORRECT_BURST_SIZE_IDS = ["limit-plus-one", "four-times-the-limit"]
# The admission statements a failed admission is tried with: the count of the checks in
# flight, and the marker the admitted check takes.
ADMISSION_STATEMENTS = ["CHECKS_IN_FLIGHT_STATEMENT", "TAKE_CHECK_SLOT_STATEMENT"]
ADMISSION_STATEMENT_IDS = ["count-checks-in-flight", "take-check-marker"]
FAILED_OPERATION_CASES = [
    FailedOperationCase(UserRepository, "GetUser", USER_FILE_NAME, 0),
    FailedOperationCase(
        login_throttle, "ResetAttempts", login_throttle.LOGIN_ATTEMPTS_FILE_NAME, 1
    ),
]
FAILED_OPERATION_IDS = ["usrsec-read", "reset-delete"]
BLANK_FIELD_CASES = [
    BlankFieldCase(BlankPasswordAttempt, BLANK_CREDENTIAL_OUTCOME, CorrectPasswordAttempt),
    BlankFieldCase(SpacesPasswordAttempt, BLANK_CREDENTIAL_OUTCOME, CorrectPasswordAttempt),
]
BLANK_FIELD_CASE_IDS = ["empty-password", "spaces-password"]
BLANK_USER_ID_ATTEMPTS = [BlankUserIdAttempt, SpacesUserIdAttempt]
BLANK_USER_ID_ATTEMPT_IDS = ["empty-user-id", "spaces-user-id"]


@pytest_asyncio.fixture(name="throttle")
async def ThrottleHarnessFixture(
    createUser: Callable[[str], GeneratedUser],
    apiClient: httpx.AsyncClient,
    testSettings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> ThrottleHarness:
    """Return the harness of a generated regular user, created off the event loop."""
    user = await asyncio.to_thread(createUser, REGULAR_USER_TYPE)
    return ThrottleHarness(user, apiClient, testSettings, monkeypatch)


def MarkerKey(userId: str) -> dict[str, object]:
    """Return the bound parameters of a check marker of ``userId``, as login_throttle binds them."""
    return {"lockSpace": login_throttle.CHECK_SLOT_LOCK_SPACE, "userId": userId}


def CheckMarkersOf(syncEngine: Engine, userId: str) -> int:
    """Return how many check markers of ``userId`` pg_locks shows granted, on any backend."""
    with syncEngine.connect() as connection:
        result = connection.execute(REAL_CHECKS_IN_FLIGHT_STATEMENT, MarkerKey(userId))
        return result.scalar_one()


async def CheckMarkersOnceDropped(syncEngine: Engine, userId: str) -> int:
    """Return the check markers of ``userId`` once none is left, or at the drop timeout.

    A discarded connection's marker leaves pg_locks when its server process has ended, which
    follows the close shortly.
    """
    deadline = time.monotonic() + MARKER_DROP_TIMEOUT_SECONDS
    markerCount = CheckMarkersOf(syncEngine, userId)
    while markerCount and time.monotonic() < deadline:
        await asyncio.sleep(MARKER_POLL_SECONDS)
        markerCount = CheckMarkersOf(syncEngine, userId)
    return markerCount


@contextlib.contextmanager
def HeldCheckMarker(syncEngine: Engine, userId: str) -> Iterator[None]:
    """Hold one check marker of ``userId`` on a connection of its own, as a check in flight."""
    with syncEngine.connect() as connection:
        connection.execute(REAL_TAKE_CHECK_SLOT_STATEMENT, MarkerKey(userId))
        connection.commit()
        try:
            yield
        finally:
            connection.execute(REAL_RELEASE_CHECK_SLOT_STATEMENT, MarkerKey(userId))
            connection.commit()


@contextlib.contextmanager
def HeldCheckMarkers(syncEngine: Engine, userId: str, markerCount: int) -> Iterator[None]:
    """Hold ``markerCount`` check markers of ``userId``, each on a connection of its own."""
    with contextlib.ExitStack() as heldMarkers:
        for _ in range(markerCount):
            heldMarkers.enter_context(HeldCheckMarker(syncEngine, userId))
        yield


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


@pytest.mark.parametrize("burstSizeFor", CORRECT_BURST_SIZES, ids=CORRECT_BURST_SIZE_IDS)
async def test_concurrent_correct_sign_ons_all_sign_on_and_check_at_most_the_limit(
    throttle, syncEngine, burstSizeFor
):
    """No correct sign-on of a burst is refused; at most LOGIN_MAX_ATTEMPTS check at once."""
    maxAttempts = throttle.settings.LOGIN_MAX_ATTEMPTS
    burstSize = burstSizeFor(maxAttempts)
    gate = throttle.InstallGate(opensWhenFull=True)
    outcomes = await throttle.AttemptBurst([throttle.user.password] * burstSize)
    assert outcomes == [throttle.SignedOnOutcome()] * burstSize
    assert gate.peakInFlight == maxAttempts
    assert throttle.checkSpy.await_count == burstSize
    assert StoredAttemptRow(syncEngine, throttle.user.userId) is None
    assert CheckMarkersOf(syncEngine, throttle.user.userId) == 0


async def test_concurrent_wrong_passwords_check_only_the_attempt_limit(throttle, syncEngine):
    """Exactly LOGIN_MAX_ATTEMPTS wrong passwords are checked; the rest of the burst gets 429."""
    maxAttempts = throttle.settings.LOGIN_MAX_ATTEMPTS
    burstSize = maxAttempts + BURST_EXTRA_ATTEMPTS
    gate = throttle.InstallGate(opensWhenFull=True)
    outcomes = await throttle.AttemptBurst([throttle.wrongCredential] * burstSize)
    expectedOutcomes = {WRONG_OUTCOME: maxAttempts, LOCKED_OUTCOME: burstSize - maxAttempts}
    assert Counter(outcomes) == Counter(expectedOutcomes)
    assert throttle.checkSpy.await_count == maxAttempts
    assert gate.peakInFlight == maxAttempts
    AssertLockedAtTheLimit(throttle.settings, StoredAttemptRow(syncEngine, throttle.user.userId))
    assert CheckMarkersOf(syncEngine, throttle.user.userId) == 0


async def test_correct_password_while_the_limit_is_in_flight_waits_then_answers_429(
    throttle, syncEngine
):
    """The correct guess waits for the limit's wrong checks, then gets 429; so does a later one."""
    maxAttempts = throttle.settings.LOGIN_MAX_ATTEMPTS
    gate = throttle.InstallGate(opensWhenFull=False)
    watch = throttle.InstallAdmissionWatch()
    wrongBurst = asyncio.create_task(
        throttle.AttemptBurst([throttle.wrongCredential] * maxAttempts)
    )
    await asyncio.wait_for(gate.full.wait(), GATE_TIMEOUT_SECONDS)
    waitingAttempt = asyncio.create_task(throttle.Attempt(CorrectPasswordAttempt(throttle)))
    await asyncio.wait_for(watch.refused.wait(), GATE_TIMEOUT_SECONDS)
    gate.Open()
    assert await wrongBurst == [WRONG_OUTCOME] * maxAttempts
    assert await waitingAttempt == LOCKED_OUTCOME
    assert await throttle.Attempt(CorrectPasswordAttempt(throttle)) == LOCKED_OUTCOME
    assert throttle.checkSpy.await_count == maxAttempts
    AssertLockedAtTheLimit(throttle.settings, StoredAttemptRow(syncEngine, throttle.user.userId))
    assert CheckMarkersOf(syncEngine, throttle.user.userId) == 0


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
async def test_blank_password_edits_count_once_each_and_lock_the_id(
    throttle, syncEngine, blankFieldCase
):
    """Each blank-password edit adds exactly one; the edit that reaches the limit locks the ID."""
    attemptInput = blankFieldCase.buildAttempt(throttle)
    for attemptNumber in range(1, throttle.settings.LOGIN_MAX_ATTEMPTS + 1):
        assert await throttle.Attempt(attemptInput) == blankFieldCase.outcome
        storedRow = StoredAttemptRow(syncEngine, attemptInput.throttledUserId)
        assert storedRow.failureCount == attemptNumber
    AssertLockedAtTheLimit(throttle.settings, storedRow)
    assert await throttle.Attempt(blankFieldCase.buildProbe(throttle)) == LOCKED_OUTCOME
    assert throttle.checkSpy.await_count == 0


@pytest.mark.parametrize("buildAttempt", BLANK_USER_ID_ATTEMPTS, ids=BLANK_USER_ID_ATTEMPT_IDS)
async def test_blank_user_id_past_the_limit_is_never_counted_or_refused_with_429(
    throttle, syncEngine, buildAttempt
):
    """Every blank user ID gets its edit literal; the blank ID keeps no row and no marker."""
    attemptInput = buildAttempt(throttle)
    attempts = OneOverTheLimit(throttle.settings.LOGIN_MAX_ATTEMPTS)
    outcomes = [await throttle.Attempt(attemptInput) for _ in range(attempts)]
    assert outcomes == [BLANK_USER_ID_OUTCOME] * attempts
    assert StoredAttemptRow(syncEngine, attemptInput.throttledUserId) is None
    assert CheckMarkersOf(syncEngine, attemptInput.throttledUserId) == 0
    assert throttle.checkSpy.await_count == 0


async def test_attempt_still_waiting_at_the_wait_limit_answers_503_uncounted_and_unchecked(
    throttle, syncEngine
):
    """With the limit held in flight, an attempt gives up with 503 and changes nothing."""
    maxAttempts = throttle.settings.LOGIN_MAX_ATTEMPTS
    userId = throttle.user.userId
    assert await throttle.Attempt(WrongPasswordAttempt(throttle)) == WRONG_OUTCOME
    storedRow = StoredAttemptRow(syncEngine, userId)
    throttle.monkeypatch.setattr(login_throttle, "CHECK_SLOT_WAIT_SECONDS", SHORT_WAIT_SECONDS)
    with HeldCheckMarkers(syncEngine, userId, maxAttempts - storedRow.failureCount):
        outcome = await throttle.Attempt(CorrectPasswordAttempt(throttle))
    assert outcome == UNABLE_TO_VERIFY_OUTCOME
    assert StoredAttemptRow(syncEngine, userId) == storedRow
    assert throttle.checkSpy.await_count == 1
    assert CheckMarkersOf(syncEngine, userId) == 0
    assert await throttle.Attempt(CorrectPasswordAttempt(throttle)) == throttle.SignedOnOutcome()


@pytest.mark.parametrize("statementName", ADMISSION_STATEMENTS, ids=ADMISSION_STATEMENT_IDS)
async def test_failed_admission_answers_503_counting_and_checking_nothing(
    throttle, syncEngine, statementName
):
    """An admission that fails on the server rolls back: 503, no row, no check, no marker."""
    throttle.monkeypatch.setattr(login_throttle, statementName, FAILING_SLOT_STATEMENT)
    outcome = await throttle.Attempt(CorrectPasswordAttempt(throttle))
    assert outcome == UNABLE_TO_VERIFY_OUTCOME
    assert StoredAttemptRow(syncEngine, throttle.user.userId) is None
    assert throttle.checkSpy.await_count == 0
    assert await CheckMarkersOnceDropped(syncEngine, throttle.user.userId) == 0


@pytest.mark.parametrize("failedOperation", FAILED_OPERATION_CASES, ids=FAILED_OPERATION_IDS)
async def test_503_of_an_admitted_attempt_counts_one_failure(throttle, syncEngine, failedOperation):
    """A USRSEC read or a reset that fails answers 503, which counts as the attempt's failure."""
    failingCall = RaisingIoError(failedOperation.fileName)
    throttle.monkeypatch.setattr(failedOperation.owner, failedOperation.attributeName, failingCall)
    outcome = await throttle.Attempt(CorrectPasswordAttempt(throttle))
    assert outcome == UNABLE_TO_VERIFY_OUTCOME
    storedRow = StoredAttemptRow(syncEngine, throttle.user.userId)
    assert (storedRow.failureCount, storedRow.lockedUntil) == (1, None)
    assert throttle.checkSpy.await_count == failedOperation.passwordChecks
    assert CheckMarkersOf(syncEngine, throttle.user.userId) == 0


async def test_failed_failure_write_answers_503_and_releases_the_check(throttle, syncEngine):
    """A wrong password whose failure cannot be counted answers 503, never an uncounted 401."""
    throttle.monkeypatch.setattr(login_throttle, "BuildFailureUpsert", FailingFailureUpsert)
    outcome = await throttle.Attempt(WrongPasswordAttempt(throttle))
    assert outcome == UNABLE_TO_VERIFY_OUTCOME
    noFailureRow = AttemptRow(failureCount=NO_FAILURE_COUNT, lockedUntil=None, lastFailureAt=None)
    assert StoredAttemptRow(syncEngine, throttle.user.userId) == noFailureRow
    assert throttle.checkSpy.await_count == 1
    assert CheckMarkersOf(syncEngine, throttle.user.userId) == 0


async def test_failed_marker_release_discards_the_connection_and_its_marker(
    throttle, syncEngine, caplog
):
    """The sign-on stands, the unlock failure is logged, and the marker goes with the session."""
    caplog.set_level(logging.WARNING, logger=login_throttle.LOGGER.name)
    throttle.monkeypatch.setattr(
        login_throttle, "RELEASE_CHECK_SLOT_STATEMENT", FAILING_SLOT_STATEMENT
    )
    outcome = await throttle.Attempt(CorrectPasswordAttempt(throttle))
    assert outcome == throttle.SignedOnOutcome()
    assert StoredAttemptRow(syncEngine, throttle.user.userId) is None
    assert any(UNLOCK_FAILED_LOG_TEXT in record.getMessage() for record in caplog.records)
    assert await CheckMarkersOnceDropped(syncEngine, throttle.user.userId) == 0
