"""Replaces: COSGN00C (CC00), BMS COSGN00, copybooks CSUSR01Y, CSMSG01Y, COCOM01Y.

The CC00 sign-on router, mounted at ``/api/v1/auth``. RACF sign-on and the COMMAREA identity
(CDEMO-USER-ID, CDEMO-USER-TYPE) become an HttpOnly session cookie: a successful sign-on sets
the SESSION_COOKIE_NAME cookie holding a signed session token, and no response body ever
carries a token. ENTER on COSGN00 is ``POST /login``, PF3 is ``POST /logout``, and the frontend
reads ``GET /me`` to choose the user's menu and fill the screen header.

Only ``POST /login`` reads a caller's identity, from its ``user_id`` and ``password`` fields;
every other route takes the user from the verified cookie. ``core.middleware.OriginGuard``
exempts only ``POST /api/v1/auth/login``, so ``POST /logout`` without an allow-listed Origin is
refused with 403 before it reaches its handler. The COSGN00C service owns every literal and
every edit; its ``LegacyMessageError`` outcomes are serialised by ``app.core.errors``.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.api.v1 import LegacyErrorResponses
from app.core.config import Settings
from app.core.dependencies import (
    GetAppSettings,
    GetAuthenticatedContext,
    GetOnlineContext,
    OnlineContext,
)
from app.core.security import IssueSessionToken, SessionCookieOptions
from app.schemas.auth import CurrentUserResponse, LoginRequest, LoginResponse, LogoutResponse
from app.services.auth import cosgn00c

ROUTER = APIRouter(prefix="/auth", tags=["auth"])

# The session cookie attributes Response.delete_cookie does not accept: it expires the cookie
# itself and takes only the path, domain, secure, httponly and samesite attributes.
COOKIE_LIFETIME_KEYS = ("max_age", "expires")

LOGIN_DESCRIPTION = (
    "Runs COSGN00C's sign-on: the user ID and password are upper-cased and verified against"
    " the stored user record. On success the HttpOnly session cookie is set, and the body"
    " returns the user and the next route: /admin for user type A, /menu for any other type."
)
LOGOUT_DESCRIPTION = (
    "Replaces PF3 on COSGN00: clears the session cookie and returns the CSMSG01Y thank-you"
    " message. No session is required, so repeating the call is harmless."
)
CURRENT_USER_DESCRIPTION = (
    "Returns the signed-on user from the session cookie, with the names from the user record"
    " and the user's own menu route. The frontend uses it to choose the menu and to fill the"
    " screen header. Any signed-on user type may call it."
)


def ClearCookieOptions(settings: Settings) -> dict[str, object]:
    """Return the session cookie attributes without its lifetime, for Response.delete_cookie.

    Path, SameSite, Secure and HttpOnly are those of the set call, so the browser matches and
    removes the same cookie.
    """
    return {
        attributeName: attributeValue
        for attributeName, attributeValue in SessionCookieOptions(settings).items()
        if attributeName not in COOKIE_LIFETIME_KEYS
    }


@ROUTER.post(
    "/login",
    response_model=LoginResponse,
    summary="CC00 / COSGN00C: sign on",
    description=LOGIN_DESCRIPTION,
    responses=LegacyErrorResponses(401, 429),
)
async def Login(
    loginRequest: LoginRequest,
    response: Response,
    context: Annotated[OnlineContext, Depends(GetOnlineContext)],
) -> LoginResponse:
    """Process ENTER on COSGN00 (COSGN00C PROCESS-ENTER-KEY) and set the session cookie.

    The cookie is set on the injected response, which FastAPI merges into the model response.
    A failed sign-on raises before the cookie is set, so no cookie accompanies an error.
    """
    settings = context.settings
    signedOnUser = await cosgn00c.SignOn(context, loginRequest)
    sessionToken = IssueSessionToken(signedOnUser.user_id, signedOnUser.user_type, settings)
    response.set_cookie(
        settings.SESSION_COOKIE_NAME, sessionToken, **SessionCookieOptions(settings)
    )
    return signedOnUser


@ROUTER.post(
    "/logout",
    response_model=LogoutResponse,
    summary="CC00 / COSGN00C: sign off (F3)",
    description=LOGOUT_DESCRIPTION,
    responses=LegacyErrorResponses(403),
)
async def Logout(
    response: Response, settings: Annotated[Settings, Depends(GetAppSettings)]
) -> LogoutResponse:
    """Process PF3 on COSGN00 (COSGN00C MAIN-PARA): clear the session cookie and sign off."""
    response.delete_cookie(settings.SESSION_COOKIE_NAME, **ClearCookieOptions(settings))
    return cosgn00c.SignOff()


@ROUTER.get(
    "/me",
    response_model=CurrentUserResponse,
    summary="CC00 / COSGN00C: current signed-on user",
    description=CURRENT_USER_DESCRIPTION,
    responses=LegacyErrorResponses(401),
)
async def ReadCurrentUser(
    context: Annotated[OnlineContext, Depends(GetAuthenticatedContext)],
) -> CurrentUserResponse:
    """Return the session user; a missing, expired or invalid cookie is refused with 401."""
    return await cosgn00c.DescribeCurrentUser(context)
