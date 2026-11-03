"""Integration tests of GET /health/ready while the database stops responding (QA finding F01).

Covers the readiness bound of the Technical Specification, Section 0.6.5: the probe runs
SELECT 1 within HEALTH_READY_TIMEOUT_SECONDS and otherwise answers 503 unavailable, its
cleanup included, even when the database keeps the connection open and never answers, as it
does under ``docker pause``. StallableProxy relays the test database through a local port and
can hold every byte while keeping both sockets open, which is what a paused database looks
like to the client. The database is stalled after a warm probe, at the probe's first query,
and before any connection.
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import Engine
from sqlalchemy.engine import make_url

from app.core.config import Settings

PROXY_HOST = "127.0.0.1"
# The literal IPv4 host, not localhost: localhost may resolve to IPv6 ::1 first, while
# PostgreSQL binds IPv4 127.0.0.1.
DEFAULT_DATABASE_HOST = "127.0.0.1"
DEFAULT_DATABASE_PORT = 5432
API_BASE_URL = "http://127.0.0.1:8000"
READINESS_PATH = "/health/ready"
HTTP_OK = 200
HTTP_SERVICE_UNAVAILABLE = 503
READY_BODY = {"status": "ready"}
UNAVAILABLE_BODY = {"status": "unavailable"}

RELAY_CHUNK_BYTES = 65536
END_OF_STREAM = b""
# The first byte of the frontend messages that start a statement: Parse and Query. The startup
# packet and SSLRequest begin with a zero length byte and SASL responses with b"p", so only a
# statement matches.
QUERY_MESSAGE_TYPES = (b"P", b"Q")

PROBE_TIMEOUT_SECONDS = 1.0
# How long after PROBE_TIMEOUT_SECONDS the unavailable answer may still arrive.
RESPONSE_MARGIN_SECONDS = 1.5
# A request still running after this long fails the test instead of hanging it.
REQUEST_LIMIT_SECONDS = 10.0
# Time for the proxy to see the probe's terminated sockets close.
CLEANUP_GRACE_SECONDS = 0.1

STALL_AFTER_WARM_PROBE = "after-warm-probe"
STALL_AT_FIRST_QUERY = "at-first-query"
STALL_BEFORE_CONNECT = "before-connect"


class StallableProxy:
    """A TCP relay to the test database that can hold every byte and keep every socket open.

    Stall() holds the bytes of both directions, as a paused database does: writes are accepted
    and nothing comes back. StallAtFirstQuery() stalls the relay by itself when a client sends
    its first statement, so the connection is already open and authenticated when the database
    stops answering. Each source is read even while stalled, so a client that closes or aborts
    its socket ends its relay at once, and openClientCount counts the relays still open.
    """

    def __init__(self, upstreamHost: str, upstreamPort: int) -> None:
        self.upstreamHost = upstreamHost
        self.upstreamPort = upstreamPort
        self.relayGate = asyncio.Event()
        self.relayGate.set()
        self.stallAtFirstQuery = False
        self.openClientCount = 0
        self.port = 0
        self.server: asyncio.Server | None = None
        self.relayTasks: set[asyncio.Task[None]] = set()
        self.relayWriters: set[asyncio.StreamWriter] = set()

    async def Start(self) -> None:
        """Listen on an ephemeral port of PROXY_HOST, kept in ``port``."""
        self.server = await asyncio.start_server(self.RelayClient, PROXY_HOST, 0)
        self.port = self.server.sockets[0].getsockname()[1]

    def Stall(self) -> None:
        """Hold every relayed byte from now on, keeping each socket open."""
        self.relayGate.clear()

    def StallAtFirstQuery(self) -> None:
        """Stall the relay when a client next sends a Parse or Query message."""
        self.stallAtFirstQuery = True

    def Resume(self) -> None:
        """Release the held bytes and relay freely again, with no stall at the next query."""
        self.stallAtFirstQuery = False
        self.relayGate.set()

    async def Close(self) -> None:
        """Stop listening, abort every relayed socket and wait for every relay task to end."""
        if self.server is None:
            return
        self.server.close()
        for relayWriter in tuple(self.relayWriters):
            relayWriter.transport.abort()
        for relayTask in tuple(self.relayTasks):
            relayTask.cancel()
        await asyncio.gather(*self.relayTasks, return_exceptions=True)
        await self.server.wait_closed()

    async def RelayClient(
        self, clientReader: asyncio.StreamReader, clientWriter: asyncio.StreamWriter
    ) -> None:
        """Relay one accepted client connection to the database until either side closes."""
        relayTask = asyncio.current_task()
        self.relayTasks.add(relayTask)
        connectionWriters = [clientWriter]
        self.relayWriters.add(clientWriter)
        self.openClientCount += 1
        try:
            serverStreams = await self.OpenUpstream()
            if serverStreams is not None and self.server.is_serving():
                connectionWriters.append(serverStreams[1])
                self.relayWriters.add(serverStreams[1])
                await self.RelayBothWays((clientReader, clientWriter), serverStreams)
        finally:
            self.openClientCount -= 1
            self.relayTasks.discard(relayTask)
            for connectionWriter in connectionWriters:
                connectionWriter.transport.abort()
                self.relayWriters.discard(connectionWriter)

    async def OpenUpstream(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        """Connect to the database, or return None when it refuses, so the client sees a close."""
        try:
            return await asyncio.open_connection(self.upstreamHost, self.upstreamPort)
        except OSError:
            return None

    async def RelayBothWays(
        self,
        clientStreams: tuple[asyncio.StreamReader, asyncio.StreamWriter],
        serverStreams: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    ) -> None:
        """Pump both directions until one source closes, then stop the other pump."""
        clientReader, clientWriter = clientStreams
        serverReader, serverWriter = serverStreams
        pumps = {
            asyncio.create_task(self.Pump(clientReader, serverWriter, True)),
            asyncio.create_task(self.Pump(serverReader, clientWriter, False)),
        }
        try:
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)

    async def Pump(
        self, source: asyncio.StreamReader, target: asyncio.StreamWriter, watchQueries: bool
    ) -> None:
        """Copy source to target until source closes; Forward writes the chunks in order.

        Bytes still held when source closes are delivered only while the relay is not stalled.
        """
        heldChunks: asyncio.Queue[bytes] = asyncio.Queue()
        forwarder = asyncio.create_task(self.Forward(heldChunks, target))
        try:
            while chunk := await source.read(RELAY_CHUNK_BYTES):
                if watchQueries:
                    self.StallOnQuery(chunk)
                heldChunks.put_nowait(chunk)
            heldChunks.put_nowait(END_OF_STREAM)
            if self.relayGate.is_set():
                await forwarder
        finally:
            forwarder.cancel()
            await asyncio.gather(forwarder, return_exceptions=True)

    async def Forward(self, heldChunks: asyncio.Queue[bytes], target: asyncio.StreamWriter) -> None:
        """Write each held chunk to target once the relay is not stalled, up to END_OF_STREAM."""
        while (chunk := await heldChunks.get()) != END_OF_STREAM:
            await self.relayGate.wait()
            target.write(chunk)
            await target.drain()

    def StallOnQuery(self, chunk: bytes) -> None:
        """Stall the relay when StallAtFirstQuery() is armed and chunk starts a statement."""
        if self.stallAtFirstQuery and chunk[:1] in QUERY_MESSAGE_TYPES:
            self.Stall()


def ProxiedDatabaseUrl(databaseUrl: str, proxyPort: int) -> str:
    """Return databaseUrl pointed at the proxy port of PROXY_HOST, its credentials kept."""
    proxiedUrl = make_url(databaseUrl).set(host=PROXY_HOST, port=proxyPort)
    return proxiedUrl.render_as_string(hide_password=False)


async def RequestReadiness(client: httpx.AsyncClient) -> httpx.Response:
    """GET /health/ready, raising TimeoutError after REQUEST_LIMIT_SECONDS instead of hanging."""
    async with asyncio.timeout(REQUEST_LIMIT_SECONDS):
        return await client.get(READINESS_PATH)


async def AssertReady(client: httpx.AsyncClient) -> None:
    """Assert that GET /health/ready answers 200 with the ready body."""
    response = await RequestReadiness(client)
    assert (response.status_code, response.json()) == (HTTP_OK, READY_BODY)


async def StallDatabase(
    stallPoint: str, stallableProxy: StallableProxy, client: httpx.AsyncClient
) -> None:
    """Make the proxied database stop answering at stallPoint, one of the STALL_* values."""
    if stallPoint == STALL_AFTER_WARM_PROBE:
        await AssertReady(client)
        stallableProxy.Stall()
    elif stallPoint == STALL_AT_FIRST_QUERY:
        stallableProxy.StallAtFirstQuery()
    else:
        stallableProxy.Stall()


@pytest_asyncio.fixture(name="stallableProxy")
async def StallableProxyFixture(
    testSettings: Settings, syncEngine: Engine
) -> AsyncIterator[StallableProxy]:
    """Yield a started StallableProxy of the test database; close it at teardown.

    syncEngine refuses a database whose name lacks _test before the proxy relays to it.
    """
    databaseUrl = make_url(testSettings.DATABASE_URL)
    stallableProxy = StallableProxy(
        databaseUrl.host or DEFAULT_DATABASE_HOST, databaseUrl.port or DEFAULT_DATABASE_PORT
    )
    await stallableProxy.Start()
    try:
        yield stallableProxy
    finally:
        await stallableProxy.Close()


@pytest_asyncio.fixture(name="proxiedClient")
async def ProxiedClientFixture(
    testSettings: Settings, stallableProxy: StallableProxy
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client of CreateApp on the proxied database, timing readiness out after 1 second.

    The lifespan is entered here, as httpx.ASGITransport sends no lifespan events. Teardown
    resumes the proxy first, so a failed test's held connections can close, then leaves the
    lifespan and disposes of the engine. app.main is imported here, not at module level, because
    importing it builds the module-level app from the process settings.
    """
    from app.core.database import DisposeEngine
    from app.main import CreateApp

    proxiedSettings = testSettings.model_copy(
        update={
            "DATABASE_URL": ProxiedDatabaseUrl(testSettings.DATABASE_URL, stallableProxy.port),
            "HEALTH_READY_TIMEOUT_SECONDS": PROBE_TIMEOUT_SECONDS,
        }
    )
    application = CreateApp(proxiedSettings)
    async with contextlib.AsyncExitStack() as exitStack:
        exitStack.push_async_callback(DisposeEngine, application.state.engine)
        await exitStack.enter_async_context(application.router.lifespan_context(application))
        exitStack.callback(stallableProxy.Resume)
        transport = httpx.ASGITransport(app=application)
        client = httpx.AsyncClient(transport=transport, base_url=API_BASE_URL)
        yield await exitStack.enter_async_context(client)


@pytest.mark.parametrize(
    "stallPoint", (STALL_AFTER_WARM_PROBE, STALL_AT_FIRST_QUERY, STALL_BEFORE_CONNECT)
)
async def test_readiness_is_unavailable_within_timeout_when_database_stops_responding(
    stallPoint: str, stallableProxy: StallableProxy, proxiedClient: httpx.AsyncClient
) -> None:
    """A stalled database gives 503 unavailable within the timeout, then ready once it answers.

    No probe connection is left open on the proxy afterwards, so the probe's cleanup did not
    wait on the stalled database.
    """
    await StallDatabase(stallPoint, stallableProxy, proxiedClient)
    startedAt = time.monotonic()
    stalledResponse = await RequestReadiness(proxiedClient)
    elapsedSeconds = time.monotonic() - startedAt
    assert stalledResponse.status_code == HTTP_SERVICE_UNAVAILABLE
    assert stalledResponse.json() == UNAVAILABLE_BODY
    assert elapsedSeconds < PROBE_TIMEOUT_SECONDS + RESPONSE_MARGIN_SECONDS
    await asyncio.sleep(CLEANUP_GRACE_SECONDS)
    assert stallableProxy.openClientCount == 0
    stallableProxy.Resume()
    await AssertReady(proxiedClient)
