"""Unit tests of the test-database guard of tests/conftest.py, run without a database.

The backend fixtures drop and truncate the test database, so the guard must refuse a URL pair
that could reach any other database before an engine exists. SQLAlchemy passes a URL's query to
psycopg2 and asyncpg after the URL's own host, port and database, so a query key such as
``dbname`` or ``host`` would win over them: every such key is refused on either URL, whatever
its case. Both drivers fill an omitted host or port from PGHOST and PGPORT, so a URL must name
both; the refusal holds whatever those variables hold. The two URLs must also reach one host,
port and database whose name ends in ``_test``, so per-clone names such as ``carddemo_c7_test``
pass. No failure text quotes a username, a password or an override value. The asyncpg/psycopg2
server-identity comparison is checked here on built identities;
tests/integration/test_test_database_connection_guard.py runs the checks against the real test
database.

The guard comes from the databaseGuard fixture, as tests never import conftest. URLs are built
with URL.create from credentials generated per test.
"""

import dataclasses
import secrets
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import URL

ASYNC_DRIVER = "postgresql+asyncpg"
SYNC_DRIVER = "postgresql+psycopg2"
ASYNC_ROLE = "async"
SYNC_ROLE = "sync"
DRIVER_BY_ROLE = {ASYNC_ROLE: ASYNC_DRIVER, SYNC_ROLE: SYNC_DRIVER}

GUARD_HOST = "127.0.0.1"
OTHER_HOST = "127.0.0.2"
MIXED_CASE_HOST = "Db.Example.Test"
POSTGRESQL_DEFAULT_PORT = 5432
OTHER_PORT = 5433
CLONE_TEST_DATABASE = "carddemo_c7_test"
OTHER_TEST_DATABASE = "carddemo_c8_test"
NON_TEST_DATABASE = "carddemo_c7"
TEST_DATABASE_SUFFIX = "_test"

USERNAME_PREFIX = "guard_"
USERNAME_TOKEN_BYTES = 8
CREDENTIAL_TOKEN_BYTES = 16
OVERRIDE_TOKEN_BYTES = 8
# A libpq keyword that names no server or database; the guard lets it through.
HARMLESS_QUERY = (("connect_timeout", "10"),)
SERVER_START = datetime(2026, 1, 1, tzinfo=UTC)
LATER_SERVER_START = datetime(2026, 1, 2, tzinfo=UTC)

# Every query key that redirects psycopg2 or asyncpg, plus mixed-case spellings.
OVERRIDE_KEYS = (
    "dbname",
    "database",
    "host",
    "hostaddr",
    "port",
    "service",
    "dsn",
    "DbName",
    "HOST",
)

INVALID_URL_FRAGMENT = "names no database"
MISMATCHED_DATABASES_FRAGMENT = "name different databases"
NOT_TEST_DATABASE_FRAGMENT = f"{NON_TEST_DATABASE!r}: a test database name must end with"
MISMATCHED_SERVERS_FRAGMENT = "reached different databases or servers"
ENDPOINT_ADVICE_FRAGMENT = "Name both the host and the port in the URL"
# The variables psycopg2 (libpq) and asyncpg fill an omitted host and port from.
HOST_VARIABLE = "PGHOST"
PORT_VARIABLE = "PGPORT"


@dataclasses.dataclass(frozen=True)
class GuardCredentials:
    """The generated username and password every URL of one test carries."""

    username: str
    password: str = dataclasses.field(repr=False)


@dataclasses.dataclass(frozen=True)
class UrlParts:
    """The parts of one test database URL besides its driver and credentials."""

    host: str | None = GUARD_HOST
    port: int | None = POSTGRESQL_DEFAULT_PORT
    database: str | None = CLONE_TEST_DATABASE
    query: tuple[tuple[str, str], ...] = ()


@dataclasses.dataclass(frozen=True)
class EndpointOmission:
    """URL parts that leave the host, the port or both to the driver, and the parts left out."""

    urlParts: UrlParts
    missingParts: str


@dataclasses.dataclass(frozen=True)
class UrlPair:
    """The parts of the asyncpg URL and of the psycopg2 URL one case hands the guard."""

    asyncParts: UrlParts = UrlParts()
    syncParts: UrlParts = UrlParts()


def TargetText(host: str, port: int, databaseName: str) -> str:
    """Return the ``host:port/database`` form the guard quotes a target in."""
    return f"{host}:{port}/{databaseName}"


def RenderUrl(driverName: str, credentials: GuardCredentials, urlParts: UrlParts) -> str:
    """Return the URL of ``urlParts`` for ``driverName``, its generated password included."""
    databaseUrl = URL.create(
        driverName,
        username=credentials.username,
        password=credentials.password,
        host=urlParts.host,
        port=urlParts.port,
        database=urlParts.database,
        query=dict(urlParts.query),
    )
    return databaseUrl.render_as_string(hide_password=False)


def BuildUrls(databaseGuard, credentials: GuardCredentials, urlPair: UrlPair):
    """Return the guard's DatabaseUrls of the asyncpg and psycopg2 URLs of ``urlPair``."""
    return databaseGuard.urlsType(
        asyncUrl=RenderUrl(ASYNC_DRIVER, credentials, urlPair.asyncParts),
        syncUrl=RenderUrl(SYNC_DRIVER, credentials, urlPair.syncParts),
    )


def RefusalOf(databaseGuard, databaseUrls) -> BaseException:
    """Return the failure RequireTestDatabase stops the run with for ``databaseUrls``."""
    with pytest.raises(pytest.fail.Exception) as refusal:
        databaseGuard.requireTestDatabase(databaseUrls)
    return refusal.value


def AssertCredentialFree(failureText: str, credentials: GuardCredentials) -> None:
    """Assert that ``failureText`` quotes neither the username nor the password."""
    assert credentials.username not in failureText
    assert credentials.password not in failureText


@pytest.fixture(name="guardCredentials")
def GuardCredentialsFixture() -> GuardCredentials:
    """Return a username and password generated for this test alone."""
    return GuardCredentials(
        username=USERNAME_PREFIX + secrets.token_hex(USERNAME_TOKEN_BYTES),
        password=secrets.token_urlsafe(CREDENTIAL_TOKEN_BYTES),
    )


@pytest.fixture(name="redirectingPgEnvironment")
def RedirectingPgEnvironmentFixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point PGHOST and PGPORT at another server, as a developer's shell environment could."""
    monkeypatch.setenv(HOST_VARIABLE, OTHER_HOST)
    monkeypatch.setenv(PORT_VARIABLE, str(OTHER_PORT))


@pytest.fixture(name="serverIdentity")
def ServerIdentityFixture(databaseGuard):
    """Return a ServerIdentity of the clone test database on GUARD_HOST."""
    return databaseGuard.identityType(
        databaseName=CLONE_TEST_DATABASE,
        serverAddress=GUARD_HOST,
        serverPort=POSTGRESQL_DEFAULT_PORT,
        startedAt=SERVER_START,
    )


@pytest.mark.parametrize("driverRole", (ASYNC_ROLE, SYNC_ROLE))
@pytest.mark.parametrize("overrideKey", OVERRIDE_KEYS)
def test_query_key_redirecting_the_connection_is_refused(
    databaseGuard, guardCredentials, driverRole, overrideKey
):
    """A server or database key in either URL's query is refused, named without its value."""
    overrideValue = secrets.token_hex(OVERRIDE_TOKEN_BYTES)
    overriddenParts = UrlParts(query=((overrideKey, overrideValue),))
    urlPair = (
        UrlPair(asyncParts=overriddenParts)
        if driverRole == ASYNC_ROLE
        else UrlPair(syncParts=overriddenParts)
    )
    databaseUrls = BuildUrls(databaseGuard, guardCredentials, urlPair)
    failureText = str(RefusalOf(databaseGuard, databaseUrls))
    assert repr(overrideKey.lower()) in failureText
    assert overrideValue not in failureText
    AssertCredentialFree(failureText, guardCredentials)


@pytest.mark.parametrize(
    ("urlPair", "expectedFragments"),
    (
        pytest.param(
            UrlPair(syncParts=UrlParts(host=OTHER_HOST)),
            (
                TargetText(GUARD_HOST, POSTGRESQL_DEFAULT_PORT, CLONE_TEST_DATABASE),
                TargetText(OTHER_HOST, POSTGRESQL_DEFAULT_PORT, CLONE_TEST_DATABASE),
            ),
            id="different-host",
        ),
        pytest.param(
            UrlPair(syncParts=UrlParts(port=OTHER_PORT)),
            (
                TargetText(GUARD_HOST, POSTGRESQL_DEFAULT_PORT, CLONE_TEST_DATABASE),
                TargetText(GUARD_HOST, OTHER_PORT, CLONE_TEST_DATABASE),
            ),
            id="different-port",
        ),
        pytest.param(
            UrlPair(syncParts=UrlParts(database=OTHER_TEST_DATABASE)),
            (MISMATCHED_DATABASES_FRAGMENT,),
            id="different-names",
        ),
        pytest.param(
            UrlPair(
                asyncParts=UrlParts(database=NON_TEST_DATABASE),
                syncParts=UrlParts(database=NON_TEST_DATABASE),
            ),
            (NOT_TEST_DATABASE_FRAGMENT, repr(TEST_DATABASE_SUFFIX)),
            id="non-test-name",
        ),
        pytest.param(
            UrlPair(asyncParts=UrlParts(database=None), syncParts=UrlParts(database=None)),
            (INVALID_URL_FRAGMENT,),
            id="no-database",
        ),
    ),
)
def test_url_pair_outside_one_test_database_is_refused(
    databaseGuard, guardCredentials, urlPair, expectedFragments
):
    """URLs reaching two servers or databases, or a non-test database, are refused."""
    failureText = str(RefusalOf(databaseGuard, BuildUrls(databaseGuard, guardCredentials, urlPair)))
    for expectedFragment in expectedFragments:
        assert expectedFragment in failureText
    AssertCredentialFree(failureText, guardCredentials)


def test_unparsable_url_is_refused_without_quoting_it(databaseGuard, guardCredentials):
    """A URL SQLAlchemy cannot parse is refused unchained, as the parse error quotes it."""
    syncUrl = RenderUrl(SYNC_DRIVER, guardCredentials, UrlParts())
    unparsableUrl = syncUrl.removeprefix(SYNC_DRIVER)
    databaseUrls = databaseGuard.urlsType(asyncUrl=unparsableUrl, syncUrl=syncUrl)
    refusal = RefusalOf(databaseGuard, databaseUrls)
    assert INVALID_URL_FRAGMENT in str(refusal)
    assert refusal.__context__ is None
    AssertCredentialFree(str(refusal), guardCredentials)


@pytest.mark.usefixtures("redirectingPgEnvironment")
@pytest.mark.parametrize("driverRole", (ASYNC_ROLE, SYNC_ROLE))
@pytest.mark.parametrize(
    "omission",
    (
        pytest.param(EndpointOmission(UrlParts(port=None), "port"), id="port"),
        pytest.param(EndpointOmission(UrlParts(host=None), "host"), id="host"),
        pytest.param(
            EndpointOmission(UrlParts(host=None, port=None), "host and port"), id="host-and-port"
        ),
    ),
)
def test_url_leaving_its_endpoint_to_the_environment_is_refused(
    databaseGuard, guardCredentials, driverRole, omission
):
    """A URL omitting its host or port is refused beside an explicit one, whatever PG* hold."""
    urlPair = (
        UrlPair(asyncParts=omission.urlParts)
        if driverRole == ASYNC_ROLE
        else UrlPair(syncParts=omission.urlParts)
    )
    failureText = str(RefusalOf(databaseGuard, BuildUrls(databaseGuard, guardCredentials, urlPair)))
    assert f"{DRIVER_BY_ROLE[driverRole]} test database URL" in failureText
    assert f"names no {omission.missingParts}," in failureText
    assert ENDPOINT_ADVICE_FRAGMENT in failureText
    AssertCredentialFree(failureText, guardCredentials)


@pytest.mark.parametrize(
    ("urlPair", "expectedEndpoint"),
    (
        pytest.param(UrlPair(), (GUARD_HOST, POSTGRESQL_DEFAULT_PORT), id="explicit-port"),
        pytest.param(
            UrlPair(asyncParts=UrlParts(port=OTHER_PORT), syncParts=UrlParts(port=OTHER_PORT)),
            (GUARD_HOST, OTHER_PORT),
            id="explicit-other-port",
        ),
        pytest.param(
            UrlPair(
                asyncParts=UrlParts(host=MIXED_CASE_HOST),
                syncParts=UrlParts(host=MIXED_CASE_HOST.lower()),
            ),
            (MIXED_CASE_HOST.lower(), POSTGRESQL_DEFAULT_PORT),
            id="host-case",
        ),
        pytest.param(
            UrlPair(syncParts=UrlParts(query=HARMLESS_QUERY)),
            (GUARD_HOST, POSTGRESQL_DEFAULT_PORT),
            id="harmless-query",
        ),
    ),
)
def test_matching_clone_test_database_pair_is_accepted(
    databaseGuard, guardCredentials, urlPair, expectedEndpoint
):
    """One per-clone ``_test`` database both URLs name explicitly passes, its target resolved."""
    target = databaseGuard.requireTestDatabase(BuildUrls(databaseGuard, guardCredentials, urlPair))
    assert (target.host, target.port, target.databaseName) == (
        *expectedEndpoint,
        CLONE_TEST_DATABASE,
    )


@pytest.mark.parametrize(
    ("fieldName", "otherValue"),
    (
        pytest.param("databaseName", OTHER_TEST_DATABASE, id="database"),
        pytest.param("serverAddress", OTHER_HOST, id="address"),
        pytest.param("serverPort", OTHER_PORT, id="port"),
        pytest.param("startedAt", LATER_SERVER_START, id="another-server-instance"),
    ),
)
def test_drivers_reaching_different_servers_are_refused(
    databaseGuard, serverIdentity, fieldName, otherValue
):
    """Any difference between the asyncpg and psycopg2 server identities is refused."""
    otherIdentity = dataclasses.replace(serverIdentity, **{fieldName: otherValue})
    with pytest.raises(pytest.fail.Exception) as refusal:
        databaseGuard.requireOneServer(otherIdentity, serverIdentity)
    assert MISMATCHED_SERVERS_FRAGMENT in str(refusal.value)


def test_drivers_reaching_one_server_are_accepted(databaseGuard, serverIdentity):
    """Equal asyncpg and psycopg2 server identities pass."""
    databaseGuard.requireOneServer(serverIdentity, dataclasses.replace(serverIdentity))
