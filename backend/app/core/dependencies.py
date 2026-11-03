"""Session identity, the admin and regular role guards, and the online request context.

Replaces: the COMMAREA identity of copybook COCOM01Y (CDEMO-USER-ID X(08) and CDEMO-USER-TYPE
X(01), 88 CDEMO-USRTYP-ADMIN VALUE 'A'), the COSGN00C routing that sends type A to COADM01C and
every other type to COMEN01C (cbl/COSGN00C.cbl lines 230-239), and the COMMAREA user type that
COCRDLIC and the other Style B programs reset to U. Specified in the Technical Specification,
Sections 0.7.1 and 0.6.10.

Identity comes only from the signed session cookie, which GetCurrentUser verifies. No request
body, query parameter or header field is ever read as identity or role. The user type in the
token holds for the token's lifetime, as the COMMAREA did for a CICS session, so no database read
happens per request. Admin is exactly A; every other stored type (U, u, a, X, ...) is regular.

- RequireAdmin guards CA00 (COADM01C) and CU00-CU03 (COUSR00C-COUSR03C). Anyone else gets 403
  with the COMEN01C literal ``No access - Admin Only option... ``.
- RequireRegular guards CM00, CAVW, CAUP, CCLI, CCDL, CCUP, CT00-CT02, CR00 and CB00. An admin
  gets 403 with an empty message, and the client returns to the user's own menu.
- OnlineContext is the one parameter object services and repositories receive. Every context
  dependency releases the request's shared batch-window lock (core.locks) when the request ends,
  whether it succeeds, fails or is cancelled.

COMEN01C's own admin-only option check (CDEMO-USRTYP-USER, exactly U) is ported in the menu
service, not here.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Annotated

import jwt
from anyio import CancelScope
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings
from app.core.database import GetSession
from app.core.errors import HTTP_ACCESS_REFUSED, HTTP_SIGN_ON_FAILURE, LegacyMessageError
from app.core.locks import LeaveOnlineWindow
from app.core.messages import ADMIN_ONLY_MESSAGE
from app.core.security import ReadSessionToken

# COCOM01Y 88 CDEMO-USRTYP-ADMIN VALUE 'A': the only user type COSGN00C sends to COADM01C.
ADMIN_USER_TYPE = "A"
# The message of a 401 session refusal and of an admin refused a regular endpoint.
NO_MESSAGE = ""


def GetAppSettings(request: Request) -> Settings:
    """Return the Settings that main.CreateApp keeps on app.state, so tests inject their own."""
    return request.app.state.settings


@dataclass(frozen=True)
class CurrentUser:
    """The signed-on user, read from the verified session token.

    ``user_id`` and ``user_type`` replace CDEMO-USER-ID and CDEMO-USER-TYPE. They keep the
    snake_case of the ``GET /auth/me`` JSON keys they are serialised to.
    """

    user_id: str
    user_type: str

    def IsAdmin(self) -> bool:
        """Return True only for type exactly A, the COSGN00C ``IF CDEMO-USRTYP-ADMIN`` test."""
        return self.user_type == ADMIN_USER_TYPE


def SessionRefusal() -> LegacyMessageError:
    """Return the 401 error of a missing, blank, expired or invalid session cookie."""
    return LegacyMessageError(NO_MESSAGE, httpStatus=HTTP_SIGN_ON_FAILURE)


async def GetCurrentUser(
    request: Request, settings: Annotated[Settings, Depends(GetAppSettings)]
) -> CurrentUser:
    """Return the user of the verified session cookie; raise 401 when there is none.

    Only the SESSION_COOKIE_NAME cookie is read. The token error is not chained, so nothing
    about a rejected token reaches the response or the logged traceback.
    """
    token = request.cookies.get(settings.SESSION_COOKIE_NAME, "")
    if not token.strip():
        raise SessionRefusal()
    try:
        claims = ReadSessionToken(token, settings)
    except jwt.InvalidTokenError:
        raise SessionRefusal() from None
    return CurrentUser(user_id=claims.userId, user_type=claims.userType)


async def RequireAdmin(user: Annotated[CurrentUser, Depends(GetCurrentUser)]) -> CurrentUser:
    """Pass a type A user; refuse any other with 403 and the admin-only literal."""
    if not user.IsAdmin():
        raise LegacyMessageError(ADMIN_ONLY_MESSAGE, httpStatus=HTTP_ACCESS_REFUSED)
    return user


async def RequireRegular(user: Annotated[CurrentUser, Depends(GetCurrentUser)]) -> CurrentUser:
    """Pass any user whose type is not exactly A; refuse an admin with 403 and no message."""
    if user.IsAdmin():
        raise LegacyMessageError(NO_MESSAGE, httpStatus=HTTP_ACCESS_REFUSED)
    return user


@dataclass
class OnlineContext:
    """What one online request hands its service and repositories.

    ``session`` is the request session for reads that lock nothing. ``user`` is the verified
    session user; it is None only in the anonymous context of the public sign-on request.
    """

    request: Request
    session: AsyncSession
    settings: Settings
    user: CurrentUser | None = None

    def Engine(self) -> AsyncEngine:
        """Return the app.state engine of core.database.WriteUnit and core.locks windows."""
        return self.request.app.state.engine

    engine = property(Engine)


async def GetOnlineContext(
    request: Request,
    session: Annotated[AsyncSession, Depends(GetSession)],
    settings: Annotated[Settings, Depends(GetAppSettings)],
) -> AsyncIterator[OnlineContext]:
    """Yield the request's anonymous context and release its batch-window lock at the end.

    The context reads no cookie and carries no user: POST /auth/login is public and builds
    its context here. GetAuthenticatedContext, GetAdminContext and GetRegularContext add the
    verified user. There is no ``except``, so the endpoint's exceptions reach the handlers.
    """
    context = OnlineContext(request=request, session=session, settings=settings)
    try:
        yield context
    finally:
        # Shielded: under an anyio cancellation every await would be cancelled again, yet the
        # shared window lock must be released even when the request task is cancelled.
        with CancelScope(shield=True):
            await LeaveOnlineWindow(request)


async def GetAuthenticatedContext(
    user: Annotated[CurrentUser, Depends(GetCurrentUser)],
    context: Annotated[OnlineContext, Depends(GetOnlineContext)],
) -> OnlineContext:
    """Return the context of any signed-on user, with no role guard (GET /auth/me).

    The user is declared first, so a missing or invalid cookie gives 401 before the context
    is built. FastAPI caches GetCurrentUser per request, so the cookie is decoded once.
    """
    return replace(context, user=user)


async def GetAdminContext(
    user: Annotated[CurrentUser, Depends(RequireAdmin)],
    context: Annotated[OnlineContext, Depends(GetOnlineContext)],
) -> OnlineContext:
    """Return the context of an admin (CA00, CU00-CU03); the guard runs before the context."""
    return replace(context, user=user)


async def GetRegularContext(
    user: Annotated[CurrentUser, Depends(RequireRegular)],
    context: Annotated[OnlineContext, Depends(GetOnlineContext)],
) -> OnlineContext:
    """Return the context of a regular user; the guard runs before the context.

    Serves CM00, CAVW, CAUP, CCLI, CCDL, CCUP, CT00-CT02, CR00 and CB00.
    """
    return replace(context, user=user)
