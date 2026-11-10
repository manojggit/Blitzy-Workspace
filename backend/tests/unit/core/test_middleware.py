"""Unit tests of app.core.middleware: the CSRF origin guard and the response security headers.

Replaces: the cross-site write protection the CICS region gave CardDemo implicitly
(csd/CARDDEMO.CSD), where only a 3270 terminal session could start a transaction and sign-on
COSGN00C (CC00) was the one entry before any session existed. OriginGuard supplies that
protection for the session cookie, as the Technical Specification, Section 0.7.1, defines it:
a POST, PUT or DELETE whose Origin header is missing or not allow-listed is answered 403 with
the legacy error body and an empty message before any handler runs, and POST
/api/v1/auth/login, the COSGN00C port, is the only exempt write. Section 0.11.1 makes
http://127.0.0.1:3000 the one local frontend origin, so http://localhost:3000, a different
site, is refused.

The guard wraps a bare FastAPI app whose routes only echo, so no settings, secret or database
are needed. A pure-ASGI recorder sits directly beneath the guard and lists the method and path
of every request it receives, in a fresh list per test. An empty list proves a refused write
reached nothing beneath the guard, not merely that its response was replaced; one entry proves
a passed request reached the app once.

ResponseSecurityHeaders wraps a second bare app and must set exactly the four headers of
EXPECTED_SECURITY_HEADERS, once each, on a JSON success, an error, a streamed body and a
response whose own Cache-Control it replaces. Driven at the raw ASGI level, it must forward a
streamed body chunk by chunk, so no export is buffered, and hand a lifespan or websocket scope
to the app with the very receive and send it was given.
"""

from collections.abc import AsyncIterator
from typing import NoReturn

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import API_V1_PREFIX
from app.core.middleware import (
    GUARDED_METHODS,
    ORIGIN_EXEMPT_ROUTES,
    RESPONSE_SECURITY_HEADERS,
    OriginGuard,
    ResponseSecurityHeaders,
)

ALLOWED_ORIGIN = "http://127.0.0.1:3000"
API_BASE_URL = "http://127.0.0.1:8000"
ACCOUNT_PATH = API_V1_PREFIX + "/accounts/x"
LOGIN_PATH = API_V1_PREFIX + "/auth/login"
# No Origin at all, a foreign site, and localhost, a different site from 127.0.0.1.
REFUSED_ORIGINS = (None, "http://evil.example", "http://localhost:3000")
REFUSED_ORIGIN_IDS = ["missing", "foreign-site", "localhost"]
GUARDED = ("POST", "PUT", "DELETE")
ACCOUNT_METHODS = ["GET", *GUARDED]
HTTP_OK = 200
HTTP_FORBIDDEN = 403
HANDLER_BODY = {"ok": True}
EMPTY_LEGACY_BODY = {"message": "", "info_message": "", "field_errors": [], "cursor_field": ""}

# The OWASP REST Security Cheat Sheet set; HSTS is excluded on the plain-HTTP local target.
EXPECTED_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
}
HSTS_HEADER = "Strict-Transport-Security"
CACHE_CONTROL_HEADER = "Cache-Control"
# A public caching instruction a route might set, which the policy must replace, not join.
ROUTE_CACHE_CONTROL = "public, max-age=3600"
SECURED_JSON_PATH = "/secured/json"
SECURED_CACHED_PATH = "/secured/cached"
SECURED_STREAM_PATH = "/secured/stream"
UNKNOWN_PATH = "/secured/unknown"
HTTP_NOT_FOUND = 404
STREAM_CHUNKS = (b"first chunk\n", b"second chunk\n", b"third chunk\n")
SECURED_RESPONSE_CASES = [
    pytest.param(SECURED_JSON_PATH, HTTP_OK, id="json-200"),
    pytest.param(UNKNOWN_PATH, HTTP_NOT_FOUND, id="error-404"),
    pytest.param(SECURED_STREAM_PATH, HTTP_OK, id="streamed-200"),
    pytest.param(SECURED_CACHED_PATH, HTTP_OK, id="route-cache-control-replaced"),
]
# ASGI spec 2.4 lets StreamingResponse send without listening for a disconnect on receive.
STREAM_SCOPE = {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "GET", "path": "/"}
RESPONSE_START = "http.response.start"
NON_HTTP_SCOPE_TYPES = ["lifespan", "websocket"]

# The method and path of one request that reached the app beneath OriginGuard.
type DownstreamCall = tuple[str, str]
# The scope, receive and send one call of an ASGI app received.
type AsgiCall = tuple[Scope, Receive, Send]


async def EchoOk() -> dict[str, bool]:
    """Answer every test route, so a response carrying HANDLER_BODY shows the handler ran."""
    return dict(HANDLER_BODY)


class DownstreamRecorder:
    """Pure-ASGI layer that records each HTTP request it receives, then passes it on unchanged."""

    def __init__(self, app: ASGIApp, downstreamCalls: list[DownstreamCall]) -> None:
        self.app = app
        self.downstreamCalls = downstreamCalls

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Recorded before the app runs, so a call whose output is later discarded still counts.
        if scope["type"] == "http":
            self.downstreamCalls.append((scope["method"], scope["path"]))
        await self.app(scope, receive, send)


def BuildGuardedApp(downstreamCalls: list[DownstreamCall]) -> OriginGuard:
    """Return OriginGuard, allow-listing only ALLOWED_ORIGIN, over the recorded echoing app."""
    application = FastAPI()
    application.add_api_route(ACCOUNT_PATH, EchoOk, methods=ACCOUNT_METHODS)
    application.add_api_route(LOGIN_PATH, EchoOk, methods=["POST"])
    recordedApplication = DownstreamRecorder(application, downstreamCalls)
    return OriginGuard(recordedApplication, allowedOrigins=[ALLOWED_ORIGIN])


@pytest.fixture(name="downstreamCalls")
def DownstreamCalls() -> list[DownstreamCall]:
    """Return the empty list this test's guarded app records each request beneath the guard in."""
    return []


@pytest_asyncio.fixture(name="guardedClient")
async def GuardedClient(
    downstreamCalls: list[DownstreamCall],
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client of BuildGuardedApp that sends no Origin unless a request adds one."""
    transport = httpx.ASGITransport(app=BuildGuardedApp(downstreamCalls))
    async with httpx.AsyncClient(transport=transport, base_url=API_BASE_URL) as client:
        yield client


def OriginHeaders(origin: str | None) -> dict[str, str]:
    """Return headers carrying ``origin`` as the Origin header, or no header when it is None."""
    if origin is None:
        return {}
    return {"Origin": origin}


@pytest.mark.parametrize("origin", REFUSED_ORIGINS, ids=REFUSED_ORIGIN_IDS)
@pytest.mark.parametrize("method", GUARDED)
async def test_guarded_write_without_allowed_origin_is_refused(
    guardedClient, downstreamCalls, method, origin
):
    response = await guardedClient.request(method, ACCOUNT_PATH, headers=OriginHeaders(origin))
    assert response.status_code == HTTP_FORBIDDEN
    assert response.json() == EMPTY_LEGACY_BODY
    assert downstreamCalls == []


@pytest.mark.parametrize("method", GUARDED)
async def test_allowed_origin_passes(guardedClient, downstreamCalls, method):
    response = await guardedClient.request(
        method, ACCOUNT_PATH, headers=OriginHeaders(ALLOWED_ORIGIN)
    )
    assert response.status_code == HTTP_OK
    assert response.json() == HANDLER_BODY
    assert downstreamCalls == [(method, ACCOUNT_PATH)]


async def test_get_without_origin_passes(guardedClient, downstreamCalls):
    response = await guardedClient.get(ACCOUNT_PATH)
    assert response.status_code == HTTP_OK
    assert response.json() == HANDLER_BODY
    assert downstreamCalls == [("GET", ACCOUNT_PATH)]


async def test_login_is_exempt_without_origin(guardedClient, downstreamCalls):
    response = await guardedClient.post(LOGIN_PATH)
    assert response.status_code == HTTP_OK
    assert response.json() == HANDLER_BODY
    assert downstreamCalls == [("POST", LOGIN_PATH)]


def test_only_login_is_exempt():
    assert ORIGIN_EXEMPT_ROUTES == frozenset({("POST", LOGIN_PATH)})
    assert set(GUARDED_METHODS) == {"POST", "PUT", "DELETE"}


def test_single_origin_string_is_rejected_as_misconfiguration():
    # A lone string would allow-list its characters and refuse every write.
    with pytest.raises(TypeError):
        OriginGuard(FastAPI(), allowedOrigins=ALLOWED_ORIGIN)


async def CachedEchoOk() -> JSONResponse:
    return JSONResponse(HANDLER_BODY, headers={CACHE_CONTROL_HEADER: ROUTE_CACHE_CONTROL})


async def StreamedChunks() -> AsyncIterator[bytes]:
    for chunk in STREAM_CHUNKS:
        yield chunk


async def StreamChunks() -> StreamingResponse:
    return StreamingResponse(StreamedChunks(), media_type="text/plain")


async def NoRequestBody() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


async def UnexpectedSend(message: Message) -> NoReturn:
    raise AssertionError(f"no message may be sent here, got {message['type']}")


class AsgiCallRecorder:
    """Pure-ASGI app that records each call it receives and sends nothing."""

    def __init__(self) -> None:
        self.calls: list[AsgiCall] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls.append((scope, receive, send))


def BuildSecuredApp() -> ResponseSecurityHeaders:
    application = FastAPI()
    application.add_api_route(SECURED_JSON_PATH, EchoOk)
    application.add_api_route(SECURED_CACHED_PATH, CachedEchoOk)
    application.add_api_route(SECURED_STREAM_PATH, StreamChunks)
    return ResponseSecurityHeaders(application)


def AssertSecurityHeaders(headers: httpx.Headers) -> None:
    for headerName, headerValue in EXPECTED_SECURITY_HEADERS.items():
        assert headers.get_list(headerName) == [headerValue], headerName
    assert HSTS_HEADER not in headers


@pytest_asyncio.fixture(name="securedClient")
async def SecuredClient() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=BuildSecuredApp())
    async with httpx.AsyncClient(transport=transport, base_url=API_BASE_URL) as client:
        yield client


def test_response_security_headers_are_exactly_the_four_policy_headers():
    assert dict(RESPONSE_SECURITY_HEADERS) == EXPECTED_SECURITY_HEADERS


@pytest.mark.parametrize(("path", "status"), SECURED_RESPONSE_CASES)
async def test_every_http_response_carries_the_security_headers(securedClient, path, status):
    response = await securedClient.get(path)
    assert response.status_code == status
    AssertSecurityHeaders(response.headers)


async def test_streamed_body_is_forwarded_chunk_by_chunk_after_the_secured_start():
    sentMessages: list[Message] = []

    async def RecordSent(message: Message) -> None:
        sentMessages.append(message)

    securedStream = ResponseSecurityHeaders(await StreamChunks())
    await securedStream(dict(STREAM_SCOPE), NoRequestBody, RecordSent)
    startMessage, *bodyMessages = sentMessages
    assert startMessage["type"] == RESPONSE_START
    AssertSecurityHeaders(httpx.Headers(startMessage["headers"]))
    assert [message["body"] for message in bodyMessages] == [*STREAM_CHUNKS, b""]
    assert [message["more_body"] for message in bodyMessages] == [True, True, True, False]


@pytest.mark.parametrize("scopeType", NON_HTTP_SCOPE_TYPES)
async def test_non_http_scope_reaches_the_app_untouched(scopeType):
    recorder = AsgiCallRecorder()
    scope = {"type": scopeType}
    await ResponseSecurityHeaders(recorder)(scope, NoRequestBody, UnexpectedSend)
    [(receivedScope, receivedReceive, receivedSend)] = recorder.calls
    assert receivedScope is scope
    assert receivedScope == {"type": scopeType}
    assert receivedReceive is NoRequestBody
    assert receivedSend is UnexpectedSend
