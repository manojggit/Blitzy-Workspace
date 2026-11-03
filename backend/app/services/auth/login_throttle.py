"""Replaces: no legacy artefact (new control, AAP 0.7.1). Guards: COSGN00C (CC00) sign-on,
key SEC-USR-ID X(08) of copybook CSUSR01Y.

Every sign-on attempt of an unlocked user ID is counted in security.login_attempts, whether or
not the user exists, before its edits and its password check run; cosgn00c passes the ID
upper-cased, as COSGN00C reads USRSEC. The check of the lock and the count are one atomic
upsert, so concurrent attempts of one ID are counted one after another and no more than
LOGIN_MAX_ATTEMPTS of them ever reach the password check. The attempt that reaches
LOGIN_MAX_ATTEMPTS locks the ID for LOGIN_LOCKOUT_SECONDS. While the lock lasts, sign-on answers
429 with LOGIN_LOCKED_MESSAGE and the attempt is not counted. The count saturates at
LOGIN_MAX_ATTEMPTS, so the SMALLINT failure_count never overflows. A successful sign-on deletes
the row, so only failed attempts stay counted; the first attempt after a lock has expired starts
the count again at one. The table is not a batch-window file, so the throttle keeps working
while the batch window is closed.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, delete, or_, update
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import WriteUnit
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


def BuildAttemptUpsert(userId: str, now: datetime, maxAttempts: int) -> Insert:
    """Build the upsert that counts one sign-on attempt and returns the new attempt count.

    One atomic statement, so concurrent first attempts of one user ID never collide on the
    primary key. On a conflict PostgreSQL locks the existing row even when the WHERE clause
    then refuses the update, so concurrent attempts of one user ID are counted one after
    another, each against the count and the lock the previous one committed. The WHERE clause
    refuses a row whose lock has not expired by ``now``: the row stays unchanged and the
    statement returns no row. Every row it lets through has no lock or an expired one, so the
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
    """Lock the user ID until ``lockedUntil``, inside the unit that counted the attempt."""
    statement = (
        update(LOGIN_ATTEMPTS)
        .where(LOGIN_ATTEMPTS.c.user_id == userId)
        .values(locked_until=lockedUntil)
    )
    await unit.execute(statement)


async def ReserveAttempt(context: OnlineContext, userId: str) -> None:
    """Count one sign-on attempt of the user ID before it is checked; refuse a locked ID.

    The count, and the lock of the attempt that reaches LOGIN_MAX_ATTEMPTS, commit in one write
    unit before cosgn00c edits the request or checks the password. The attempt therefore stays
    counted whatever the request does next, and a concurrent attempt of the same ID is counted
    after it.

    Raises:
        LegacyMessageError: 429 with LOGIN_LOCKED_MESSAGE while the user ID holds a lock that
            has not expired; the attempt is not counted.
        RepositoryError: the LOGINATT file condition when the write unit fails.
    """
    now = UtcNow()
    settings = context.settings
    attemptUpsert = BuildAttemptUpsert(userId, now, settings.LOGIN_MAX_ATTEMPTS)
    async with WriteUnit(context.engine, LOGIN_ATTEMPTS_FILE_NAME) as unit:
        attemptCount = (await unit.execute(attemptUpsert)).scalar_one_or_none()
        if attemptCount is not None and attemptCount >= settings.LOGIN_MAX_ATTEMPTS:
            lockedUntil = now + timedelta(seconds=settings.LOGIN_LOCKOUT_SECONDS)
            await ApplyLockout(unit, userId, lockedUntil)
    if attemptCount is None:
        raise LegacyMessageError(
            LOGIN_LOCKED_MESSAGE, field=USER_ID_FIELD, httpStatus=LOCKED_STATUS
        )


async def ResetAttempts(context: OnlineContext, userId: str) -> None:
    """Delete the user ID's failure row after a successful sign-on; a missing row is no error."""
    statement = delete(LOGIN_ATTEMPTS).where(LOGIN_ATTEMPTS.c.user_id == userId)
    async with WriteUnit(context.engine, LOGIN_ATTEMPTS_FILE_NAME) as unit:
        await unit.execute(statement)
