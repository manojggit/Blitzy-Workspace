"""Unit tests for app.core.config, the environment settings of the backend and Alembic.

The unit replaces the CICS region settings of csd/CARDDEMO.CSD and the configuration that
travelled in the COMMAREA (cpy/COCOM01Y.cpy), as specified in the Technical Specification,
Sections 0.7.1 and 0.7.6. These tests pin that a settings object never renders SECRET_KEY or
a database-URL password, while its attributes keep the raw values the engines and the token
signer read, that LOGIN_MAX_ATTEMPTS is accepted only within the SMALLINT range of
security.login_attempts.failure_count, and that BACKEND_CORS_ORIGINS is accepted only as one or
more origins exactly as a browser sends them in the Origin header, so a wildcard, a path, a
trailing slash, a malformed or noncanonical host or port, or an empty list never reaches the
credentialed CORS middleware, and its error never echoes the value.
Every key and password is generated at run time with the secrets module, and backend/.env is
never read (_env_file=None).
"""

import io
import logging
import secrets

import pytest
from pydantic import ValidationError

from app.core.config import MigrationSettings, Settings

SETTINGS_LOGGER_NAME = "carddemo.tests.unit.core.config"
# The PostgreSQL SMALLINT maximum, the type of security.login_attempts.failure_count.
SMALLINT_MAXIMUM = 32767
LOGIN_MAX_ATTEMPTS_LOCATION = [("LOGIN_MAX_ATTEMPTS",)]
BACKEND_CORS_ORIGINS_LOCATION = [("BACKEND_CORS_ORIGINS",)]
DEFAULT_CORS_ORIGINS = Settings.model_fields["BACKEND_CORS_ORIGINS"].default
LOCAL_FRONTEND_ORIGIN = "http://127.0.0.1:3000"
SECOND_LOCAL_ORIGIN = "http://127.0.0.1:3001"
LOCAL_ORIGIN_PAIR = (LOCAL_FRONTEND_ORIGIN, SECOND_LOCAL_ORIGIN)


def BuildDatabaseUrl(driverName, databasePassword):
    """Return a PostgreSQL URL on host 127.0.0.1 whose credentials carry databasePassword."""
    return f"postgresql+{driverName}://settings_test:{databasePassword}@127.0.0.1/settings_test"


def RenderAsDict(settingsObject):
    """Return the text of settingsObject.model_dump(), the Python-mode dump."""
    return str(settingsObject.model_dump())


def RenderThroughLogRecord(settingsObject):
    """Return what a logging handler writes for a record that formats settingsObject."""
    logStream = io.StringIO()
    logHandler = logging.StreamHandler(logStream)
    settingsLogger = logging.getLogger(SETTINGS_LOGGER_NAME)
    settingsLogger.setLevel(logging.INFO)
    settingsLogger.addHandler(logHandler)
    try:
        settingsLogger.info("settings: %s", settingsObject)
    finally:
        settingsLogger.removeHandler(logHandler)
    return logStream.getvalue()


def LoginMaxAttemptsOutcome(attemptLimit, secretKey, databasePassword):
    """Return the LOGIN_MAX_ATTEMPTS Settings keeps for attemptLimit, or the rejected locations."""
    try:
        builtSettings = Settings(
            _env_file=None,
            SECRET_KEY=secretKey,
            DATABASE_URL=BuildDatabaseUrl("asyncpg", databasePassword),
            SYNC_DATABASE_URL=BuildDatabaseUrl("psycopg2", databasePassword),
            LOGIN_MAX_ATTEMPTS=attemptLimit,
        )
    except ValidationError as error:
        return [fieldError["loc"] for fieldError in error.errors()]
    return builtSettings.LOGIN_MAX_ATTEMPTS


def BuildCorsSettings(corsOrigins, secretKey, databasePassword):
    """Return Settings holding the generated key and password and BACKEND_CORS_ORIGINS."""
    return Settings(
        _env_file=None,
        SECRET_KEY=secretKey,
        DATABASE_URL=BuildDatabaseUrl("asyncpg", databasePassword),
        SYNC_DATABASE_URL=BuildDatabaseUrl("psycopg2", databasePassword),
        BACKEND_CORS_ORIGINS=corsOrigins,
    )


@pytest.fixture(name="secretKey")
def SecretKey():
    """Return a generated SECRET_KEY long enough for Settings.RejectWeakSecretKey."""
    return secrets.token_urlsafe(32)


@pytest.fixture(name="databasePassword")
def DatabasePassword():
    """Return a generated database password, URL-safe so it embeds in a connection URL."""
    return secrets.token_urlsafe(12)


@pytest.fixture(name="runtimeSettings")
def RuntimeSettings(secretKey, databasePassword):
    """Return backend Settings holding the generated key and password."""
    return Settings(
        _env_file=None,
        SECRET_KEY=secretKey,
        DATABASE_URL=BuildDatabaseUrl("asyncpg", databasePassword),
        SYNC_DATABASE_URL=BuildDatabaseUrl("psycopg2", databasePassword),
    )


@pytest.fixture(name="migrationSettings")
def BuiltMigrationSettings(databasePassword):
    """Return Alembic MigrationSettings holding the generated password."""
    return MigrationSettings(
        _env_file=None,
        SYNC_DATABASE_URL=BuildDatabaseUrl("psycopg2", databasePassword),
    )


@pytest.mark.parametrize(
    "renderSettings",
    [repr, str, Settings.model_dump_json, RenderAsDict, RenderThroughLogRecord],
    ids=["repr", "str", "model_dump_json", "model_dump", "log-record"],
)
def test_settings_renderings_omit_key_and_password(
    renderSettings, runtimeSettings, secretKey, databasePassword
):
    renderedText = renderSettings(runtimeSettings)
    assert "BCRYPT_ROUNDS" in renderedText
    assert secretKey not in renderedText
    assert databasePassword not in renderedText


@pytest.mark.parametrize(
    "renderSettings",
    [repr, MigrationSettings.model_dump_json],
    ids=["repr", "model_dump_json"],
)
def test_migration_settings_renderings_omit_password(
    renderSettings, migrationSettings, databasePassword
):
    renderedText = renderSettings(migrationSettings)
    assert "BCRYPT_ROUNDS" in renderedText
    assert databasePassword not in renderedText


def test_settings_attributes_keep_raw_values(
    runtimeSettings, migrationSettings, secretKey, databasePassword
):
    syncDatabaseUrl = BuildDatabaseUrl("psycopg2", databasePassword)
    assert runtimeSettings.SECRET_KEY == secretKey
    assert runtimeSettings.DATABASE_URL == BuildDatabaseUrl("asyncpg", databasePassword)
    assert runtimeSettings.SYNC_DATABASE_URL == syncDatabaseUrl
    assert migrationSettings.SYNC_DATABASE_URL == syncDatabaseUrl


@pytest.mark.parametrize(
    ("attemptLimit", "expectedOutcome"),
    [
        (1, 1),
        (SMALLINT_MAXIMUM, SMALLINT_MAXIMUM),
        (0, LOGIN_MAX_ATTEMPTS_LOCATION),
        (SMALLINT_MAXIMUM + 1, LOGIN_MAX_ATTEMPTS_LOCATION),
    ],
    ids=["one", "smallint-maximum", "zero", "above-smallint-maximum"],
)
def test_login_max_attempts_stays_within_failure_count_range(
    attemptLimit, expectedOutcome, secretKey, databasePassword
):
    assert LoginMaxAttemptsOutcome(attemptLimit, secretKey, databasePassword) == expectedOutcome


@pytest.mark.parametrize(
    "rejectedOrigins",
    [
        "*",
        LOCAL_FRONTEND_ORIGIN + "/",
        ",",
        LOCAL_FRONTEND_ORIGIN + "/app",
        "127.0.0.1:3000",
        "HTTP://127.0.0.1:3000",
        "null",
        LOCAL_FRONTEND_ORIGIN + ",*",
        "http://[:::]:3000",
        "http://[1]:3000",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:03000",
        "http://127.0.0.1:0",
        "http://127.0.0.1:80",
        "https://carddemo.example.com:443",
        "http://[0:0:0:0:0:0:0:1]:3000",
        "http://127.1:3000",
        "http://127.0.0.01:3000",
        "http://-carddemo.example:3000",
        "http://carddemo..example:3000",
    ],
    ids=[
        "wildcard",
        "trailing-slash",
        "no-entries",
        "path",
        "no-scheme",
        "upper-case-scheme",
        "null-origin",
        "origin-then-wildcard",
        "ipv6-triple-colon",
        "ipv6-single-group",
        "port-above-maximum",
        "port-leading-zero",
        "port-zero",
        "http-default-port",
        "https-default-port",
        "ipv6-expanded",
        "ipv4-shorthand",
        "ipv4-octet-leading-zero",
        "label-leading-hyphen",
        "empty-label",
    ],
)
def test_backend_cors_origins_refuses_non_origin_entries(
    rejectedOrigins, secretKey, databasePassword
):
    with pytest.raises(ValidationError) as raisedError:
        BuildCorsSettings(rejectedOrigins, secretKey, databasePassword)
    rejectedLocations = [fieldError["loc"] for fieldError in raisedError.value.errors()]
    assert rejectedLocations == BACKEND_CORS_ORIGINS_LOCATION
    assert "input_value" not in str(raisedError.value)


@pytest.mark.parametrize(
    ("acceptedOrigins", "expectedOrigins"),
    [
        (DEFAULT_CORS_ORIGINS, (LOCAL_FRONTEND_ORIGIN,)),
        (f"{LOCAL_FRONTEND_ORIGIN},{SECOND_LOCAL_ORIGIN}", LOCAL_ORIGIN_PAIR),
        (f" {LOCAL_FRONTEND_ORIGIN} , ,{SECOND_LOCAL_ORIGIN},", LOCAL_ORIGIN_PAIR),
        ("https://carddemo.example.com", ("https://carddemo.example.com",)),
        ("http://[::1]:3000", ("http://[::1]:3000",)),
        ("http://127.0.0.1:65535", ("http://127.0.0.1:65535",)),
        ("http://localhost:3000", ("http://localhost:3000",)),
        ("http://[2001:db8::1]", ("http://[2001:db8::1]",)),
        ("https://carddemo.example.com:8443", ("https://carddemo.example.com:8443",)),
    ],
    ids=[
        "default",
        "two-origins",
        "padded-with-blank-entries",
        "https-host-name",
        "ipv6-literal",
        "port-maximum",
        "single-label-host",
        "ipv6-default-port",
        "https-non-default-port",
    ],
)
def test_backend_cors_origins_accepts_origin_lists(
    acceptedOrigins, expectedOrigins, secretKey, databasePassword
):
    builtSettings = BuildCorsSettings(acceptedOrigins, secretKey, databasePassword)
    assert builtSettings.CorsOrigins() == expectedOrigins
