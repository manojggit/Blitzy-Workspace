"""A TCP relay to the test database that can stall, or drop, every connection it carries.

Shared by the integration tests of a database that stops answering or goes away mid-request
(tests/integration/test_health_hung_database.py, tests/integration/test_database_outage.py),
the outages of the Technical Specification, Sections 0.6.5 and 0.6.13. Tests point the app's
DATABASE_URL at the relay with ProxiedDatabaseUrl and then:

- Stall() holds every byte while keeping both sockets open, which is what ``docker pause``
  looks like to the client; StallAtFirstQuery() and StallAtQuery() stall at a later statement;
- DropConnections() aborts every relayed socket and refuses new clients, which is what
  ``docker stop`` looks like to the client and to PostgreSQL, which ends the sessions.

The relay holds no credentials of its own: the proxied URL keeps the test database's.
"""

import asyncio

from sqlalchemy.engine import make_url

PROXY_HOST = "127.0.0.1"
# The literal IPv4 host, not localhost: localhost may resolve to IPv6 ::1 first, while
# PostgreSQL binds IPv4 127.0.0.1.
DEFAULT_DATABASE_HOST = "127.0.0.1"
DEFAULT_DATABASE_PORT = 5432

RELAY_CHUNK_BYTES = 65536
END_OF_STREAM = b""
# The first byte of the frontend messages that start a statement: Parse and Query. The startup
# packet and SSLRequest begin with a zero length byte and SASL responses with b"p", so only a
# statement matches.
QUERY_MESSAGE_TYPES = (b"P", b"Q")


class StallableProxy:
    """A TCP relay to the test database that can hold every byte and keep every socket open.

    Stall() holds the bytes of both directions, as a paused database does: writes are accepted
    and nothing comes back. StallAtFirstQuery() and StallAtQuery() stall the relay by
    themselves when clients send their first or their n-th statement, so the connection is
    already open and authenticated when the database stops answering. DropConnections() aborts
    every relayed socket and refuses new clients, as a stopped database does. Each source is
    read even while stalled, so a client that closes or aborts its socket ends its relay at
    once, and openClientCount counts the relays still open.
    """

    def __init__(self, upstreamHost: str, upstreamPort: int) -> None:
        self.upstreamHost = upstreamHost
        self.upstreamPort = upstreamPort
        self.relayGate = asyncio.Event()
        self.relayGate.set()
        self.queriesUntilStall = 0
        self.refusingConnections = False
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
        self.StallAtQuery(1)

    def StallAtQuery(self, queryNumber: int) -> None:
        """Stall the relay when clients have sent queryNumber statements from now on.

        A pool's pre-ping sends BEGIN and then its ping statement, so queryNumber 2 lets the
        BEGIN through and stalls the ping, inside a transaction the ping must then roll back.
        """
        self.queriesUntilStall = queryNumber

    def DropConnections(self) -> None:
        """Abort every relayed socket and refuse new clients, as a stopped database does.

        Both sides see the connection lost: PostgreSQL ends each session, releasing its locks,
        and the client's pending and later operations on it fail. Resume() accepts clients again.
        """
        self.refusingConnections = True
        for relayWriter in tuple(self.relayWriters):
            relayWriter.transport.abort()

    def Resume(self) -> None:
        """Release the held bytes and relay freely again: no stall at a later query, no refusal."""
        self.queriesUntilStall = 0
        self.refusingConnections = False
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
        """Relay one accepted client connection to the database until either side closes.

        While DropConnections() refuses clients, the client's socket is closed at once.
        """
        relayTask = asyncio.current_task()
        self.relayTasks.add(relayTask)
        connectionWriters = [clientWriter]
        self.relayWriters.add(clientWriter)
        self.openClientCount += 1
        try:
            serverStreams = None if self.refusingConnections else await self.OpenUpstream()
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
        """Count a chunk starting a statement while StallAtQuery() is armed; stall at the last."""
        if self.queriesUntilStall and chunk[:1] in QUERY_MESSAGE_TYPES:
            self.queriesUntilStall -= 1
            if not self.queriesUntilStall:
                self.Stall()


def ProxyOf(databaseUrl: str) -> StallableProxy:
    """Return an unstarted StallableProxy relaying to the host and port of databaseUrl."""
    parsedUrl = make_url(databaseUrl)
    return StallableProxy(
        parsedUrl.host or DEFAULT_DATABASE_HOST, parsedUrl.port or DEFAULT_DATABASE_PORT
    )


def ProxiedDatabaseUrl(databaseUrl: str, proxyPort: int) -> str:
    """Return databaseUrl pointed at the proxy port of PROXY_HOST, its credentials kept."""
    proxiedUrl = make_url(databaseUrl).set(host=PROXY_HOST, port=proxyPort)
    return proxiedUrl.render_as_string(hide_password=False)
