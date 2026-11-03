"""Replaces: no legacy artefact (new control, AAP 0.7.1). Guards: COSGN00C (CC00) sign-on,
key SEC-USR-ID X(08) of copybook CSUSR01Y.

Every failed sign-on adds one to the failure count of its user ID in security.login_attempts,
whether or not the user exists; cosgn00c passes the ID upper-cased, as COSGN00C reads USRSEC.
The failure that reaches LOGIN_MAX_ATTEMPTS locks the ID for LOGIN_LOCKOUT_SECONDS, and while
the lock lasts sign-on answers 429 with LOGIN_LOCKED_MESSAGE. The count saturates at
LOGIN_MAX_ATTEMPTS, so a failure counted at the limit keeps it there and the SMALLINT
failure_count never overflows. A successful sign-on deletes the row; the first failure after a
lock has expired starts the count again at one. The table is not a batch-window file, so the
throttle keeps working while the batch window is closed.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, delete, select, update
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import UNIT_FAILURES, TranslateWriteFailure, WriteUnit
from app.core.dependencies import OnlineContext
from app.core.errors import HTTP_LOGIN_LOCKED, LegacyMessageError
from app.core.messages import LOGIN_LOCKED_MESSAGE
from app.models import LOGIN_ATTEMPTS

# The file name carried by the RepositoryError conditions raised for security.login_attempts.
LOGIN_ATTEMPTS_FILE_NAME = "LOGINATT"
LOCKED_STATUS = HTTP_LOGIN_LOCKED
# The sign-on field that takes the cursor while the user ID is locked (USERIDL of COSGN0AI).
USER_ID_FIELD = "user_id"


def UtcNow() -> datetime:
    """Return the current time as an aware UTC datetime; the only clock of this module."""
    return datetime.now(UTC)


async def ReadLockedUntil(context: OnlineContext, userId: str) -> datetime | None:
    """Return the lock expiry stored for the user ID, or None when it has no lock or no row.

    The read locks nothing, so it runs on the request session. Besides a wrapped DBAPIError,
    the session's first statement can meet an unwrapped connect failure, so the whole
    UNIT_FAILURES set becomes the RepositoryError family, which cosgn00c reports as the
    COSGN00C ``WHEN OTHER`` literal instead of an unexpected error.
    """
    statement = select(LOGIN_ATTEMPTS.c.locked_until).where(LOGIN_ATTEMPTS.c.user_id == userId)
    try:
        result = await context.session.execute(statement)
    except UNIT_FAILURES as error:
        raise TranslateWriteFailure(error, LOGIN_ATTEMPTS_FILE_NAME) from error
    return result.scalar_one_or_none()


async def CheckLocked(context: OnlineContext, userId: str) -> None:
    """Raise the 429 lockout error while the user ID holds a lock that has not yet expired."""
    lockedUntil = await ReadLockedUntil(context, userId)
    if lockedUntil is not None and lockedUntil > UtcNow():
        raise LegacyMessageError(
            LOGIN_LOCKED_MESSAGE, field=USER_ID_FIELD, httpStatus=LOCKED_STATUS
        )


def BuildFailureUpsert(userId: str, now: datetime, maxAttempts: int) -> Insert:
    """Build the upsert that counts one failure and returns the new failure count.

    One atomic statement, so concurrent first failures of one user ID never collide on the
    primary key. A lock that has expired by ``now`` is cleared and the count restarts at one.
    Otherwise the count saturates at ``maxAttempts`` (LOGIN_MAX_ATTEMPTS): a row already at
    the limit, as after concurrent failures, keeps it instead of adding one, so the SMALLINT
    failure_count never overflows.
    """
    lockExpired = and_(
        LOGIN_ATTEMPTS.c.locked_until.is_not(None), LOGIN_ATTEMPTS.c.locked_until <= now
    )
    limitReached = LOGIN_ATTEMPTS.c.failure_count >= maxAttempts
    firstFailure = insert(LOGIN_ATTEMPTS).values(
        user_id=userId, failure_count=1, locked_until=None, last_failure_at=now
    )
    return firstFailure.on_conflict_do_update(
        index_elements=[LOGIN_ATTEMPTS.c.user_id],
        set_={
            "failure_count": case(
                (lockExpired, 1),
                (limitReached, maxAttempts),
                else_=LOGIN_ATTEMPTS.c.failure_count + 1,
            ),
            "locked_until": case((lockExpired, None), else_=LOGIN_ATTEMPTS.c.locked_until),
            "last_failure_at": now,
        },
    ).returning(LOGIN_ATTEMPTS.c.failure_count)


async def ApplyLockout(unit: AsyncConnection, userId: str, lockedUntil: datetime) -> None:
    """Lock the user ID until ``lockedUntil``, inside the unit that counted the failure."""
    statement = (
        update(LOGIN_ATTEMPTS)
        .where(LOGIN_ATTEMPTS.c.user_id == userId)
        .values(locked_until=lockedUntil)
    )
    await unit.execute(statement)


async def RecordFailure(context: OnlineContext, userId: str) -> None:
    """Count one failed sign-on for the user ID, locking it once the attempt limit is reached."""
    now = UtcNow()
    settings = context.settings
    failureUpsert = BuildFailureUpsert(userId, now, settings.LOGIN_MAX_ATTEMPTS)
    # The unit commits when the block exits, before cosgn00c raises its sign-on error, so the
    # failure stays recorded although the request itself ends in an error.
    async with WriteUnit(context.engine, LOGIN_ATTEMPTS_FILE_NAME) as unit:
        failureCount = (await unit.execute(failureUpsert)).scalar_one()
        if failureCount >= settings.LOGIN_MAX_ATTEMPTS:
            lockedUntil = now + timedelta(seconds=settings.LOGIN_LOCKOUT_SECONDS)
            await ApplyLockout(unit, userId, lockedUntil)


async def ResetAttempts(context: OnlineContext, userId: str) -> None:
    """Delete the user ID's failure row after a successful sign-on; a missing row is no error."""
    statement = delete(LOGIN_ATTEMPTS).where(LOGIN_ATTEMPTS.c.user_id == userId)
    async with WriteUnit(context.engine, LOGIN_ATTEMPTS_FILE_NAME) as unit:
        await unit.execute(statement)
