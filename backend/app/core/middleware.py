"""CSRF origin guard for cookie-authenticated writes.

Replaces: the implicit terminal-session trust of the CICS region (csd/CARDDEMO.CSD), where only
a 3270 terminal signed on through COSGN00C (CC00) could start a CardDemo transaction and no
cross-site request existed. Specified in the Technical Specification, Sections 0.7.1 and 0.10.4.

The session cookie is HttpOnly and SameSite=Lax. Every POST, PUT and DELETE must in addition
carry an Origin header exactly equal to one BACKEND_CORS_ORIGINS entry: no prefix or wildcard
matching, so http://127.0.0.1:3000 and http://localhost:3000 are different origins. A request
without one is answered 403 with the legacy error body and an empty message. POST
/api/v1/auth/login is the only exemption, because it runs before any session exists and its
body carries only credentials. GET, HEAD and OPTIONS, the CORS preflight, are never guarded.

The guard is pure ASGI and inspects only the scope's method, path and Origin header. A request
it passes reaches the app with receive and send untouched, so the guard never consumes the
request body, and responses, streamed ones such as the CR00 report export included, flow through
unchanged. A refused request is answered by the guard itself with the 403 JSONResponse.
main.CreateApp adds it with add_middleware and wraps the whole stack in CORSMiddleware, so CORS
is the outer layer and its headers decorate the 403 as well. That 403 never carries
Access-Control-Allow-Origin, because the guard refuses only an Origin CORS does not allow.
"""

from collections.abc import Sequence

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import API_V1_PREFIX
from app.core.errors import HTTP_ACCESS_REFUSED, ErrorBody

GUARDED_METHODS = frozenset({"POST", "PUT", "DELETE"})
ORIGIN_EXEMPT_ROUTES = frozenset({("POST", API_V1_PREFIX + "/auth/login")})
FORBIDDEN_STATUS = HTTP_ACCESS_REFUSED


def IsGuarded(method: str, path: str) -> bool:
    """Return True when a request with this method and path needs an allow-listed Origin."""
    return method in GUARDED_METHODS and (method, path) not in ORIGIN_EXEMPT_ROUTES


class OriginGuard:
    """ASGI middleware refusing guarded writes whose Origin is missing or not allow-listed."""

    def __init__(self, app: ASGIApp, allowedOrigins: Sequence[str]) -> None:
        # A lone string is itself a Sequence[str]; iterating it would allow-list single
        # characters and silently refuse every write, so it is rejected as misconfiguration.
        if isinstance(allowedOrigins, str):
            raise TypeError("allowedOrigins must be a sequence of origins, not one string")
        self.app = app
        self.allowedOrigins = frozenset(
            origin.strip() for origin in allowedOrigins if origin.strip()
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not IsGuarded(scope["method"], scope["path"]):
            await self.app(scope, receive, send)
            return
        if Headers(scope=scope).get("origin") not in self.allowedOrigins:
            refusal = JSONResponse(ErrorBody(""), status_code=FORBIDDEN_STATUS)
            await refusal(scope, receive, send)
            return
        await self.app(scope, receive, send)
