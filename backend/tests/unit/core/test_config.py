"""Unit tests for app.core.config, the environment settings of the backend and Alembic.

The unit replaces the CICS region settings of csd/CARDDEMO.CSD and the configuration that
travelled in the COMMAREA (cpy/COCOM01Y.cpy), as specified in the Technical Specification,
Sections 0.7.1 and 0.7.6. These tests pin that a settings object never renders SECRET_KEY or
a database-URL password, while its attributes keep the raw values the engines and the token
signer read, and that LOGIN_MAX_ATTEMPTS is accepted only within the SMALLINT range of
security.login_attempts.failure_count. Every key and password is generated at run time with
the secrets module, and backend/.env is never read (_env_file=None).
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
