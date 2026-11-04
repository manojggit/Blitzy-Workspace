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

They also pin the environment contract: the backend refuses to start while SECRET_KEY is
unset, blank or shorter than 32 characters; unknown variables such as a leftover AUTH_MODE are
ignored; unset variables take the documented defaults of backend/.env.example; the API docs
follow ENABLE_API_DOCS when it is set and APP_ENV otherwise; MigrationSettings builds without
SECRET_KEY; and GetSettings builds Settings once until cache_clear().

Every key and password is generated at run time with the secrets module. Variables change only
through monkeypatch, so the bootstrap environment of tests/conftest.py is restored after each
test, and backend/.env is never read (_env_file=None, or no env_file for GetSettings).
"""

import io
import logging
import secrets
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import API_V1_PREFIX, GetSettings, MigrationSettings, Settings
from app.core.paths import REPO_ROOT

SETTINGS_LOGGER_NAME = "carddemo.tests.unit.core.config"
# The PostgreSQL SMALLINT maximum, the type of security.login_attempts.failure_count.
SMALLINT_MAXIMUM = 32767
LOGIN_MAX_ATTEMPTS_LOCATION = [("LOGIN_MAX_ATTEMPTS",)]
BACKEND_CORS_ORIGINS_LOCATION = [("BACKEND_CORS_ORIGINS",)]
DEFAULT_CORS_ORIGINS = Settings.model_fields["BACKEND_CORS_ORIGINS"].default
LOCAL_FRONTEND_ORIGIN = "http://127.0.0.1:3000"
SECOND_LOCAL_ORIGIN = "http://127.0.0.1:3001"
LOCAL_ORIGIN_PAIR = (LOCAL_FRONTEND_ORIGIN, SECOND_LOCAL_ORIGIN)
# The fail-closed rule: SECRET_KEY needs 32 characters; secrets.token_hex(n) returns 2 * n.
REQUIRED_SECRET_KEY_LENGTH = 32
REQUIRED_SECRET_KEY_BYTES = REQUIRED_SECRET_KEY_LENGTH // 2
# Generated once per run, one character short of the rule.
SHORT_SECRET_KEY = secrets.token_hex(REQUIRED_SECRET_KEY_BYTES)[: REQUIRED_SECRET_KEY_LENGTH - 1]
SECRET_KEY_LOCATION = [("SECRET_KEY",)]
APP_ENV_LOCATION = [("APP_ENV",)]
# The default of each variable while no source sets it, as backend/.env.example documents it.
DOCUMENTED_DEFAULTS = {
    "ACCESS_TOKEN_EXPIRE_MINUTES": 60,
    "SESSION_COOKIE_NAME": "carddemo_session",
    "BCRYPT_ROUNDS": 12,
    "LOGIN_MAX_ATTEMPTS": 5,
    "LOGIN_LOCKOUT_SECONDS": 900,
    "BACKEND_CORS_ORIGINS": LOCAL_FRONTEND_ORIGIN,
    "DB_CONNECT_TIMEOUT_SECONDS": 10.0,
    "DB_COMMAND_TIMEOUT_SECONDS": 30.0,
    "HEALTH_READY_TIMEOUT_SECONDS": 2.0,
    "ENABLE_API_DOCS": None,
    "CARDDEMO_EBCDIC_DIR": "data/EBCDIC",
    "CARDDEMO_ASCII_DIR": "data/ASCII",
}
DIRECTORY_METHODS = {
    "CARDDEMO_EBCDIC_DIR": Settings.EbcdicDir,
    "CARDDEMO_ASCII_DIR": Settings.AsciiDir,
}
# Lines an existing backend/.env may still hold that no settings class declares.
LEFTOVER_ENV_FILE_LINES = ("AUTH_MODE=bearer", "CARDDEMO_OUTPUT_DIR=var/batch-output")
ENV_FILE_COOKIE_NAME = "env_file_session"
PROCESS_COOKIE_NAME = "process_session"


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


def BuildSettings():
    """Return Settings built from the process environment alone, never from backend/.env."""
    return Settings(_env_file=None)


def ErrorLocations(validationError):
    """Return the field location of every error a ValidationError holds, in order."""
    return [fieldError["loc"] for fieldError in validationError.errors()]


def AssertSecretKeyRefused(**settingsArguments):
    """Assert building Settings fails on SECRET_KEY alone, with no input echoed in the error."""
    with pytest.raises(ValidationError) as raisedError:
        Settings(_env_file=None, **settingsArguments)
    assert ErrorLocations(raisedError.value) == SECRET_KEY_LOCATION
    assert "input_value" not in str(raisedError.value)


def SetOrDeleteVariable(environment, variableName, variableValue):
    """Set variableName to variableValue through monkeypatch, or delete it when that is None."""
    if variableValue is None:
        environment.delenv(variableName, raising=False)
    else:
        environment.setenv(variableName, variableValue)


def WriteEnvFile(directory, envLines):
    """Write envLines as a dotenv file in directory and return its path."""
    envFile = directory / ".env"
    envFile.write_text("\n".join(envLines) + "\n", encoding="utf-8")
    return envFile


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


@pytest.fixture(name="settingsEnvironment")
def SettingsEnvironment(monkeypatch, secretKey, databasePassword):
    """Return monkeypatch after setting a generated SECRET_KEY and both database URLs.

    The URLs are set because tests/conftest.py sets them only when they resolve, so these tests
    pass with and without backend/.env. Every other variable keeps its bootstrap value until the
    test changes it through the returned monkeypatch, which restores them all at teardown.
    """
    monkeypatch.setenv("SECRET_KEY", secretKey)
    monkeypatch.setenv("DATABASE_URL", BuildDatabaseUrl("asyncpg", databasePassword))
    monkeypatch.setenv("SYNC_DATABASE_URL", BuildDatabaseUrl("psycopg2", databasePassword))
    return monkeypatch


@pytest.fixture(name="clearedSettingsCache")
def ClearedSettingsCache():
    """Clear the GetSettings cache before and after the test, so no test sees another's Settings."""
    GetSettings.cache_clear()
    yield
    GetSettings.cache_clear()


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


@pytest.mark.parametrize("secretKeyVariable", ["", None], ids=["empty-variable", "unset-variable"])
def test_blank_secret_key_refuses_to_start(secretKeyVariable, settingsEnvironment):
    SetOrDeleteVariable(settingsEnvironment, "SECRET_KEY", secretKeyVariable)
    AssertSecretKeyRefused()


def test_blank_secret_key_argument_refuses_to_start(settingsEnvironment):
    # An init argument outranks the process environment, and env_ignore_empty never skips it.
    AssertSecretKeyRefused(SECRET_KEY="")


@pytest.mark.parametrize(
    "weakSecretKey",
    [SHORT_SECRET_KEY, " " * REQUIRED_SECRET_KEY_LENGTH, f"  {SHORT_SECRET_KEY}  "],
    ids=["thirty-one-characters", "spaces-only", "thirty-one-characters-padded"],
)
def test_short_secret_key_refuses_to_start(weakSecretKey, settingsEnvironment):
    settingsEnvironment.setenv("SECRET_KEY", weakSecretKey)
    AssertSecretKeyRefused()


def test_thirty_two_character_secret_key_is_accepted(settingsEnvironment):
    acceptedSecretKey = secrets.token_hex(REQUIRED_SECRET_KEY_BYTES)
    settingsEnvironment.setenv("SECRET_KEY", acceptedSecretKey)
    assert BuildSettings().SECRET_KEY == acceptedSecretKey


@pytest.mark.parametrize("variableName", ["DATABASE_URL", "SYNC_DATABASE_URL"])
def test_missing_database_url_refuses_to_start(variableName, settingsEnvironment):
    settingsEnvironment.delenv(variableName)
    with pytest.raises(ValidationError) as raisedError:
        BuildSettings()
    assert ErrorLocations(raisedError.value) == [(variableName,)]


@pytest.mark.parametrize("settingsClass", [Settings, MigrationSettings])
def test_leftover_variables_are_ignored(settingsClass, settingsEnvironment, tmp_path):
    settingsEnvironment.setenv("AUTH_MODE", "bearer")
    envFile = WriteEnvFile(tmp_path, LEFTOVER_ENV_FILE_LINES)
    builtSettings = settingsClass(_env_file=envFile)
    assert not hasattr(builtSettings, "AUTH_MODE")
    assert not hasattr(builtSettings, "CARDDEMO_OUTPUT_DIR")


@pytest.mark.parametrize(
    ("processCookieName", "expectedCookieName"),
    [
        (None, ENV_FILE_COOKIE_NAME),
        ("", ENV_FILE_COOKIE_NAME),
        (PROCESS_COOKIE_NAME, PROCESS_COOKIE_NAME),
    ],
    ids=["unset-reads-env-file", "empty-reads-env-file", "set-overrides-env-file"],
)
def test_process_environment_overrides_env_file(
    processCookieName, expectedCookieName, settingsEnvironment, tmp_path
):
    SetOrDeleteVariable(settingsEnvironment, "SESSION_COOKIE_NAME", processCookieName)
    envFile = WriteEnvFile(tmp_path, [f"SESSION_COOKIE_NAME={ENV_FILE_COOKIE_NAME}"])
    assert Settings(_env_file=envFile).SESSION_COOKIE_NAME == expectedCookieName


def test_app_env_defaults_to_local(settingsEnvironment):
    settingsEnvironment.delenv("APP_ENV", raising=False)
    builtSettings = BuildSettings()
    assert builtSettings.APP_ENV == "local"
    assert builtSettings.IsProduction() is False


def test_unknown_app_env_refuses_to_start(settingsEnvironment):
    settingsEnvironment.setenv("APP_ENV", "staging")
    with pytest.raises(ValidationError) as raisedError:
        BuildSettings()
    assert ErrorLocations(raisedError.value) == APP_ENV_LOCATION


@pytest.mark.parametrize(
    ("appEnv", "expectedProduction"),
    [("local", False), ("test", False), ("production", True)],
)
def test_is_production_only_for_production(appEnv, expectedProduction, settingsEnvironment):
    settingsEnvironment.setenv("APP_ENV", appEnv)
    assert BuildSettings().IsProduction() is expectedProduction


@pytest.mark.parametrize(
    ("appEnv", "enableApiDocs", "expectedEnabled"),
    [
        ("local", None, True),
        ("test", None, True),
        ("production", None, False),
        ("production", "", False),
        ("production", "true", True),
        ("local", "false", False),
    ],
    ids=[
        "local-unset",
        "test-unset",
        "production-unset",
        "production-empty",
        "production-forced-on",
        "local-forced-off",
    ],
)
def test_api_docs_follow_enable_api_docs_then_app_env(
    appEnv, enableApiDocs, expectedEnabled, settingsEnvironment
):
    settingsEnvironment.setenv("APP_ENV", appEnv)
    SetOrDeleteVariable(settingsEnvironment, "ENABLE_API_DOCS", enableApiDocs)
    assert BuildSettings().ApiDocsEnabled() is expectedEnabled


def test_cors_origins_variable_splits_on_commas(settingsEnvironment):
    corsOriginList = f"{LOCAL_FRONTEND_ORIGIN}, {SECOND_LOCAL_ORIGIN}"
    settingsEnvironment.setenv("BACKEND_CORS_ORIGINS", corsOriginList)
    assert BuildSettings().CorsOrigins() == LOCAL_ORIGIN_PAIR


def test_documented_defaults_apply_when_variables_unset(settingsEnvironment):
    for variableName in DOCUMENTED_DEFAULTS:
        settingsEnvironment.delenv(variableName, raising=False)
    builtSettings = BuildSettings()
    appliedDefaults = {
        variableName: getattr(builtSettings, variableName) for variableName in DOCUMENTED_DEFAULTS
    }
    assert appliedDefaults == DOCUMENTED_DEFAULTS


@pytest.mark.parametrize(
    ("variableName", "variableValue", "expectedDirectory"),
    [
        ("CARDDEMO_EBCDIC_DIR", None, REPO_ROOT / "data" / "EBCDIC"),
        ("CARDDEMO_EBCDIC_DIR", "/app/data/EBCDIC", Path("/app/data/EBCDIC")),
        ("CARDDEMO_ASCII_DIR", None, REPO_ROOT / "data" / "ASCII"),
        ("CARDDEMO_ASCII_DIR", "/app/data/ASCII", Path("/app/data/ASCII")),
    ],
    ids=["ebcdic-default", "ebcdic-absolute", "ascii-default", "ascii-absolute"],
)
def test_data_directories_resolve_relative_values_under_repo_root(
    variableName, variableValue, expectedDirectory, settingsEnvironment
):
    SetOrDeleteVariable(settingsEnvironment, variableName, variableValue)
    assert DIRECTORY_METHODS[variableName](BuildSettings()) == expectedDirectory


def test_migration_settings_build_without_secret_key(settingsEnvironment, databasePassword):
    settingsEnvironment.delenv("SECRET_KEY")
    builtMigrationSettings = MigrationSettings(_env_file=None)
    expectedSyncUrl = BuildDatabaseUrl("psycopg2", databasePassword)
    assert builtMigrationSettings.SYNC_DATABASE_URL == expectedSyncUrl
    assert "SECRET_KEY" not in MigrationSettings.model_fields


def test_migration_settings_default_sync_database_url_to_blank(settingsEnvironment):
    # Offline SQL generation (alembic upgrade --sql) needs no database URL.
    settingsEnvironment.delenv("SYNC_DATABASE_URL")
    assert MigrationSettings(_env_file=None).SYNC_DATABASE_URL == ""


def test_api_v1_prefix():
    assert API_V1_PREFIX == "/api/v1"


@pytest.mark.usefixtures("clearedSettingsCache")
def test_get_settings_builds_settings_once_until_cache_clear(settingsEnvironment):
    # No env_file, so a developer's backend/.env cannot reach the cached Settings.
    settingsEnvironment.setitem(Settings.model_config, "env_file", None)
    firstSettings = GetSettings()
    assert isinstance(firstSettings, Settings)
    assert GetSettings() is firstSettings
    GetSettings.cache_clear()
    assert GetSettings() is not firstSettings
