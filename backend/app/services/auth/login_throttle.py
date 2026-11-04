"""Replaces: no legacy artefact (new control, AAP 0.7.1). Guards: COSGN00C (CC00) sign-on,
key SEC-USR-ID X(08) of copybook CSUSR01Y.

Only the sign-on failures cosgn00c answers with 401 are counted in security.login_attempts,
against the upper-cased user ID it passes, as COSGN00C reads USRSEC, whether or not the user
exists. cosgn00c answers a blank user ID with its edit before any admission, as COSGN00C reads
no USRSEC record for it, so the blank ID is never a key. An attempt is admitted only while the
failures counted since the last reset plus the password checks in flight stay below
LOGIN_MAX_ATTEMPTS. A concurrent burst of wrong passwords therefore checks at most
LOGIN_MAX_ATTEMPTS of them before the lock, and concurrent correct sign-ons are never refused:

- AcquireCheckSlot admits an attempt to its password edit and check. One write unit locks
  the ID's row, inserting it with no failures when absent, so the admissions of one ID, and its
  failure and reset writes, run one after another. A locked ID answers 429 with
  LOGIN_LOCKED_MESSAGE and the attempt is not counted. Otherwise the attempt is admitted while
  the failures that count plus the checks in flight stay below LOGIN_MAX_ATTEMPTS, and it marks
  its check in flight before the unit commits. An attempt over the limit waits for the outcomes
  in flight instead of answering 429, deciding again every CHECK_SLOT_POLL_SECONDS; after
  CHECK_SLOT_WAIT_SECONDS it gives up with RecordLockError and counts nothing.
- RecordFailure counts the failure of an admitted attempt in its own write unit, committed
  before the response. The failure that reaches LOGIN_MAX_ATTEMPTS locks the ID for
  LOGIN_LOCKOUT_SECONDS. The count saturates at LOGIN_MAX_ATTEMPTS, so the SMALLINT
  failure_count never overflows, and the first failure after a lock has expired starts the
  count again at one. ResetAttempts deletes the row after a successful sign-on.
- ReleaseCheckSlot ends the check only after its outcome has committed, so no admission ever
  counts fewer failures plus checks than there are: a finishing failure counts twice until the
  release, which only makes admission more conservative.

A check in flight is a session-level shared advisory lock in PostgreSQL's two-int4 key space,
(CHECK_SLOT_LOCK_SPACE, hashtext(user ID)), held on a pooled connection of the attempt's own;
pg_locks counts them across every backend process of the database. Two-int4 keys never collide
with the single-bigint keys of core.locks. Two user IDs whose hashtext values collide share one
LOGIN_MAX_ATTEMPTS budget of checks, which only makes an attempt of one wait longer for the
other's checks. A connection that drops releases its marker with its session.

A sign-on holds at most four pooled connections at once, core.database.REQUEST_CONNECTION_LIMIT:
the check-slot connection, the batch-window connection, the request session and one write unit.
The table is not a batch-window file, so the throttle keeps working while the batch window is
closed; a sign-on the window refuses answers 503 and counts nothing, so it never locks the ID.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import anyio
from anyio import CancelScope
from sqlalchemy import (
    Integer,
    Row,
    String,
    TextClause,
    and_,
    bindparam,
    case,
    delete,
    or_,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.database import UNIT_FAILURES, EngineDeadline, TranslateWriteFailure, WriteUnit
from app.core.dependencies import OnlineContext
from app.core.errors import HTTP_LOGIN_LOCKED, LegacyMessageError, RecordLockError
from app.core.messages import LOGIN_LOCKED_MESSAGE
from app.models import LOGIN_ATTEMPTS

LOGGER = logging.getLogger(__name__)

# The file name carried by the RepositoryError conditions raised for security.login_attempts.
LOGIN_ATTEMPTS_FILE_NAME = "LOGINATT"
LOCKED_STATUS = HTTP_LOGIN_LOCKED
# The sign-on field that takes the cursor while the user ID is locked (USERIDL of COSGN0AI).
USER_ID_FIELD = "user_id"

# The first key of every check marker in PostgreSQL's two-int4 advisory-lock space, spelling
# the ASCII bytes "CDLC" and fitting a signed int4; the second key is hashtext(user ID).
CHECK_SLOT_LOCK_SPACE = 0x43444C43
CHECK_SLOT_POLL_SECONDS = 0.05
CHECK_SLOT_WAIT_SECONDS = 30.0

TAKE_CHECK_SLOT_SQL = "SELECT pg_advisory_lock_shared(:lockSpace, hashtext(:userId))"
RELEASE_CHECK_SLOT_SQL = "SELECT pg_advisory_unlock_shared(:lockSpace, hashtext(:userId))"
# pg_locks shows a two-int4 key unsigned, as classid and objid, with objsubid 2.
CHECKS_IN_FLIGHT_SQL = (
    "SELECT COUNT(*) FROM pg_locks WHERE locktype = 'advisory'"
    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    " AND classid = CAST(:lockSpace AS oid) AND objid = CAST(hashtext(:userId) AS oid)"
    " AND objsubid = 2 AND granted"
)

# The states of an attempt's check slot. IDLE: no marker statement has run, so the connection
# goes back to the pool. TAKING: the marker may have been granted, so the connection is
# discarded. HELD: the marker is held and is released first. CLOSED: the slot is released.
SLOT_IDLE = "IDLE"
SLOT_TAKING = "TAKING"
SLOT_HELD = "HELD"
SLOT_CLOSED = "CLOSED"

UNLOCK_FAILED_LOG = "Sign-on check-slot unlock failed (%s); the connection is discarded instead"


def CheckSlotStatement(sql: str) -> TextClause:
    """Bind :lockSpace as INTEGER and :userId as text, the two-int4 key of a check marker."""
    return text(sql).bindparams(
        bindparam("lockSpace", type_=Integer), bindparam("userId", type_=String)
    )


TAKE_CHECK_SLOT_STATEMENT = CheckSlotStatement(TAKE_CHECK_SLOT_SQL)
RELEASE_CHECK_SLOT_STATEMENT = CheckSlotStatement(RELEASE_CHECK_SLOT_SQL)
CHECKS_IN_FLIGHT_STATEMENT = CheckSlotStatement(CHECKS_IN_FLIGHT_SQL)


@dataclass
class CheckSlot:
    """One attempt's check-slot connection, its upper-cased user ID and its marker state."""

    connection: AsyncConnection
    userId: str
    state: str = SLOT_IDLE

    def MarkerKey(self) -> dict[str, object]:
        return {"lockSpace": CHECK_SLOT_LOCK_SPACE, "userId": self.userId}


def UtcNow() -> datetime:
    """Return the current time as an aware UTC datetime; the only clock of this module."""
    return datetime.now(UTC)


def LockedError() -> LegacyMessageError:
    """Return the 429 lockout: LOGIN_LOCKED_MESSAGE with the cursor on the user ID."""
    return LegacyMessageError(LOGIN_LOCKED_MESSAGE, field=USER_ID_FIELD, httpStatus=LOCKED_STATUS)


def BuildAdmissionUpsert(userId: str) -> Insert:
    """Build the upsert that row-locks the user ID and returns its failure count and lock.

    An absent row is inserted with no failures; an existing row is rewritten unchanged. Either
    way PostgreSQL holds the row lock until the admission unit commits, so the admissions of
    one ID, and its failure and reset writes, run one after another, each against what the
    previous one committed.
    """
    noFailures = insert(LOGIN_ATTEMPTS).values(
        user_id=userId, failure_count=0, locked_until=None, last_failure_at=None
    )
    return noFailures.on_conflict_do_update(
        index_elements=[LOGIN_ATTEMPTS.c.user_id],
        set_={"failure_count": LOGIN_ATTEMPTS.c.failure_count},
    ).returning(LOGIN_ATTEMPTS.c.failure_count, LOGIN_ATTEMPTS.c.locked_until)


def CountingFailures(attemptRow: Row, now: datetime, maxAttempts: int) -> int:
    """Return the failures that count against a new check; raise the 429 while locked.

    None count once the lock has expired, because the next failure restarts the count at one.
    An unlocked count is capped at ``maxAttempts - 1``: a row left at or over the limit, as
    after the limit was lowered, then admits one check at a time, whose failure locks the ID.
    """
    failureCount, lockedUntil = attemptRow
    if lockedUntil is None:
        return min(failureCount, maxAttempts - 1)
    if lockedUntil > now:
        raise LockedError()
    return 0


async def CountChecksInFlight(unit: AsyncConnection, slot: CheckSlot) -> int:
    """Return how many attempts of the slot's user ID hold a check marker, on any backend."""
    result = await unit.execute(CHECKS_IN_FLIGHT_STATEMENT, slot.MarkerKey())
    return result.scalar_one()


async def TakeCheckSlot(slot: CheckSlot) -> None:
    """Mark the attempt's check in flight on its own connection, then end that transaction.

    The commit leaves the connection idle rather than idle in transaction; the session-level
    marker survives it. The state is TAKING until the commit returns, so after a failure or a
    cancellation in between, when the marker may be held, the connection is discarded.
    """
    slot.state = SLOT_TAKING
    await slot.connection.execute(TAKE_CHECK_SLOT_STATEMENT, slot.MarkerKey())
    await slot.connection.commit()
    slot.state = SLOT_HELD


async def DecideAdmission(context: OnlineContext, slot: CheckSlot) -> bool:
    """Return whether the attempt's check is admitted now; raise the 429 while locked.

    The marker is taken before the admission unit commits and frees the row, so the next
    admission of the ID already counts it.
    """
    maxAttempts = context.settings.LOGIN_MAX_ATTEMPTS
    async with WriteUnit(context.engine, LOGIN_ATTEMPTS_FILE_NAME) as unit:
        attemptRow = (await unit.execute(BuildAdmissionUpsert(slot.userId))).one()
        countingFailures = CountingFailures(attemptRow, UtcNow(), maxAttempts)
        checksInFlight = await CountChecksInFlight(unit, slot)
        admitted = countingFailures + checksInFlight < maxAttempts
        if admitted:
            await TakeCheckSlot(slot)
    return admitted


async def WaitForAdmission(context: OnlineContext, slot: CheckSlot) -> None:
    """Decide until the attempt is admitted; raise RecordLockError past CHECK_SLOT_WAIT_SECONDS.

    An attempt over the limit waits for the outcomes in flight: each failure they record can
    lock the ID, which the next decision answers with 429, and each success frees a check.
    """
    deadline = anyio.current_time() + CHECK_SLOT_WAIT_SECONDS
    while not await DecideAdmission(context, slot):
        if anyio.current_time() >= deadline:
            raise RecordLockError(LOGIN_ATTEMPTS_FILE_NAME)
        await anyio.sleep(CHECK_SLOT_POLL_SECONDS)


async def OpenSlotConnection(engine: AsyncEngine) -> AsyncConnection:
    """Check out the attempt's check-slot connection; a failure becomes the LOGINATT condition.

    The checkout and its pre-ping run within EngineDeadline of the engine, whose expiry is a
    TimeoutError and so FileIoError.
    """
    try:
        with anyio.fail_after(EngineDeadline(engine)):
            return await engine.connect()
    except UNIT_FAILURES as exc:
        raise TranslateWriteFailure(exc, LOGIN_ATTEMPTS_FILE_NAME) from exc


async def AcquireCheckSlot(context: OnlineContext, userId: str) -> CheckSlot:
    """Return the attempt's check slot once its edits and password check are admitted.

    The caller then runs the edits and the check, commits the outcome with RecordFailure or
    ResetAttempts, and calls ReleaseCheckSlot. An attempt that is not admitted has its slot
    released here.

    Raises:
        LegacyMessageError: 429 with LOGIN_LOCKED_MESSAGE while the user ID holds a lock that
            has not expired, at the first decision or after waiting; nothing is counted.
        RepositoryError: the LOGINATT condition when the connection or an admission unit
            fails, and RecordLockError when no check is admitted within
            CHECK_SLOT_WAIT_SECONDS; nothing is counted.
    """
    slot = CheckSlot(connection=await OpenSlotConnection(context.engine), userId=userId)
    admitted = False
    try:
        await WaitForAdmission(context, slot)
        admitted = True
    finally:
        if not admitted:
            await ReleaseCheckSlot(slot)
    return slot


async def UnlockCheckSlot(slot: CheckSlot) -> None:
    await slot.connection.execute(RELEASE_CHECK_SLOT_STATEMENT, slot.MarkerKey())
    await slot.connection.commit()


async def DiscardSlotConnection(connection: AsyncConnection) -> None:
    """Close a connection that may hold a check marker without returning it to the pool.

    Invalidating closes the driver connection, so PostgreSQL ends the session and drops the
    marker; the pool's reset on return only rolls back and would keep it.
    """
    await connection.invalidate()
    await connection.close()


async def ReleaseCheckSlot(slot: CheckSlot) -> None:
    """End the attempt's check: release its marker, if held, then its connection.

    Called once the attempt's outcome has committed; a released slot is left as it is. The
    release is shielded, so it completes even when the request is cancelled. When the unlock
    fails or is interrupted, or the marker may be held unconfirmed, the connection is
    discarded and PostgreSQL drops the marker with the session. The failure is logged, not
    raised, so the sign-on's own outcome stands.
    """
    if slot.state == SLOT_CLOSED:
        return
    heldState = slot.state
    slot.state = SLOT_CLOSED
    with CancelScope(shield=True):
        released = heldState == SLOT_IDLE
        try:
            if heldState == SLOT_HELD:
                await UnlockCheckSlot(slot)
                released = True
        except UNIT_FAILURES as exc:
            LOGGER.warning(UNLOCK_FAILED_LOG, type(exc).__name__)
        finally:
            if released:
                await slot.connection.close()
            else:
                await DiscardSlotConnection(slot.connection)


def BuildFailureUpsert(userId: str, now: datetime, maxAttempts: int) -> Insert:
    """Build the upsert that counts one failed sign-on and returns the new failure count.

    One atomic statement, so a failure of a user ID whose row a success has just deleted never
    collides on the primary key. On a conflict PostgreSQL locks the existing row even when the
    WHERE clause then refuses the update, so failures of one user ID are counted one after
    another, each against the count and the lock the previous one committed. The WHERE clause
    refuses a row whose lock has not expired by ``now``: the row stays unchanged and the
    statement returns no row; admission never lets a check start while a lock lasts, so this
    only guards the count. Every row it lets through has no lock or an expired one, so the
    update leaves it unlocked: an expired lock is cleared and the count restarts at one.
    Otherwise the count saturates at ``maxAttempts`` (LOGIN_MAX_ATTEMPTS): an unlocked row
    already at the limit, as after the limit was lowered, keeps it instead of adding one, so
    the SMALLINT failure_count never overflows.
    """
    lockedUntil = LOGIN_ATTEMPTS.c.locked_until
    lockExpired = and_(lockedUntil.is_not(None), lockedUntil <= now)
    unlocked = or_(lockedUntil.is_(None), lockedUntil <= now)
    limitReached = LOGIN_ATTEMPTS.c.failure_count >= maxAttempts
    firstAttempt = insert(LOGIN_ATTEMPTS).values(
        user_id=userId, failure_count=1, locked_until=None, last_failure_at=now
    )
    return firstAttempt.on_conflict_do_update(
        index_elements=[LOGIN_ATTEMPTS.c.user_id],
        set_={
            "failure_count": case(
                (lockExpired, 1),
                (limitReached, maxAttempts),
                else_=LOGIN_ATTEMPTS.c.failure_count + 1,
            ),
            "locked_until": None,
            "last_failure_at": now,
        },
        where=unlocked,
    ).returning(LOGIN_ATTEMPTS.c.failure_count)


async def ApplyLockout(unit: AsyncConnection, userId: str, lockedUntil: datetime) -> None:
    statement = (
        update(LOGIN_ATTEMPTS)
        .where(LOGIN_ATTEMPTS.c.user_id == userId)
        .values(locked_until=lockedUntil)
    )
    await unit.execute(statement)


async def RecordFailure(context: OnlineContext, userId: str) -> None:
    """Count one failed sign-on of an admitted attempt; lock the ID at LOGIN_MAX_ATTEMPTS.

    The count, and the lock of the failure that reaches LOGIN_MAX_ATTEMPTS, commit in one write
    unit before the response is sent and before the attempt's check slot is released, so the
    next admission of the ID sees the failure before its check stops counting.

    Raises:
        RepositoryError: the LOGINATT file condition when the write unit fails.
    """
    now = UtcNow()
    settings = context.settings
    failureUpsert = BuildFailureUpsert(userId, now, settings.LOGIN_MAX_ATTEMPTS)
    async with WriteUnit(context.engine, LOGIN_ATTEMPTS_FILE_NAME) as unit:
        failureCount = (await unit.execute(failureUpsert)).scalar_one_or_none()
        if failureCount is not None and failureCount >= settings.LOGIN_MAX_ATTEMPTS:
            lockedUntil = now + timedelta(seconds=settings.LOGIN_LOCKOUT_SECONDS)
            await ApplyLockout(unit, userId, lockedUntil)


async def ResetAttempts(context: OnlineContext, userId: str) -> None:
    """Delete the user ID's failure row after a successful sign-on; a missing row is no error."""
    statement = delete(LOGIN_ATTEMPTS).where(LOGIN_ATTEMPTS.c.user_id == userId)
    async with WriteUnit(context.engine, LOGIN_ATTEMPTS_FILE_NAME) as unit:
        await unit.execute(statement)
