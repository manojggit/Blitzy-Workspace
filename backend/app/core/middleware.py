"""CSRF origin guard for cookie-authenticated writes, and the response security headers.

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
is outside the guard and its headers decorate the 403 as well. That 403 never carries
Access-Control-Allow-Origin, because the guard refuses only an Origin CORS does not allow.

ResponseSecurityHeaders wraps CORS as the outermost layer and sets RESPONSE_SECURITY_HEADERS on
every HTTP response the backend sends: successes, legacy errors, the guard's 403, an unexpected
500, CORS preflight answers, the streamed CR00 exports, the health probes and the API docs. The
policy is uniform because the backend serves no cacheable asset, a probe must report live state
and the docs are a development aid. It is pure ASGI and edits only the http.response.start
message, so a streamed body is never buffered, and it catches nothing.
"""

from collections.abc import Sequence
from types import MappingProxyType

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import API_V1_PREFIX
from app.core.errors import HTTP_ACCESS_REFUSED, ErrorBody

GUARDED_METHODS = frozenset({"POST", "PUT", "DELETE"})
ORIGIN_EXEMPT_ROUTES = frozenset({("POST", API_V1_PREFIX + "/auth/login")})
FORBIDDEN_STATUS = HTTP_ACCESS_REFUSED
# The OWASP REST Security Cheat Sheet set for an API. no-store keeps SSNs, other PII and the
# report exports out of every private cache after sign-off; nosniff stops a download being run
# as another content type; DENY and frame-ancestors 'none' forbid framing a response. HSTS is
# absent: the local run target serves plain HTTP on 127.0.0.1.
RESPONSE_SECURITY_HEADERS = MappingProxyType(
    {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "frame-ancestors 'none'",
    }
)
RESPONSE_START_MESSAGE = "http.response.start"


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


def SetResponseSecurityHeaders(message: Message) -> None:
    # A value the app set is replaced, so the policy holds for every response.
    message.setdefault("headers", [])
    responseHeaders = MutableHeaders(scope=message)
    for headerName, headerValue in RESPONSE_SECURITY_HEADERS.items():
        responseHeaders[headerName] = headerValue


class ResponseSecurityHeaders:
    """ASGI middleware setting RESPONSE_SECURITY_HEADERS on every HTTP response sent through it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def SendWithSecurityHeaders(message: Message) -> None:
            if message["type"] == RESPONSE_START_MESSAGE:
                SetResponseSecurityHeaders(message)
            await send(message)

        await self.app(scope, receive, SendWithSecurityHeaders)
