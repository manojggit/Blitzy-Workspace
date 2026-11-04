"""Environment settings of the CardDemo backend and its Alembic migrations.

Replaces: the CICS region configuration of csd/CARDDEMO.CSD (the CARDDEMO group's FILE,
PROGRAM and TRANSACTION resources and the region they run in), the RACF sign-on parameters
behind COSGN00C, and the session state the COCOM01Y COMMAREA carried between transactions.
Specified in the Technical Specification, Sections 0.7.1 and 0.7.6.

Each value comes from the process environment, which overrides backend/.env (ENV_FILE), which
overrides the defaults below. Field names are the environment-variable names, identical to
backend/.env.example, docker-compose.yml and batch/config.py. SECRET_KEY and the database
URLs come only from the environment (the process environment or backend/.env). SECRET_KEY
defaults to blank and fails closed. CommonSettings requires SYNC_DATABASE_URL, and its
subclass Settings requires DATABASE_URL as well; both are declared without a default.
MigrationSettings defaults SYNC_DATABASE_URL to blank so that offline migration SQL
(alembic upgrade --sql) can be generated without a database. A blank default holds no
credential, so no credential is written in code. The engines and Alembic parse a URL through
ParseDatabaseUrl, whose error names the setting and never repeats the URL or its password.

Importing this module reads no setting; values are read when a settings class is built.
"""

import ipaddress
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from app.core.paths import ENV_FILE, ResolveRepoPath

API_V1_PREFIX = "/api/v1"
MINIMUM_SECRET_KEY_LENGTH = 32
REJECTED_KEY_MESSAGE = (
    f"SECRET_KEY must be set to at least {MINIMUM_SECRET_KEY_LENGTH} characters"
    " (generate one with: openssl rand -hex 32)"
)
# Names the setting that held a URL SQLAlchemy cannot parse, never the URL itself.
INVALID_DATABASE_URL_MESSAGE = "{variableName} is not a valid database URL"
# The largest count security.login_attempts.failure_count holds: the PostgreSQL SMALLINT
# maximum. LOGIN_MAX_ATTEMPTS above it could never be counted to, so no lockout would follow.
MAXIMUM_LOGIN_FAILURE_COUNT = 32767
# One origin as a browser sends it in the Origin header. CORS and OriginGuard compare entries
# exactly, so an entry in any other form is refused, never normalized: a lower-case http or
# https scheme; a host name, a dotted-quad IPv4 address or a bracketed IPv6 address in
# compressed form; a port from 1 to MAXIMUM_PORT with no leading zero, omitted when it is the
# scheme's default; and nothing after. The shape fixes the characters; the predicates below
# check the host and the port.
BROWSER_ORIGIN_SHAPE = re.compile(
    r"(?P<scheme>https?)://(?P<host>\[[0-9a-f:]+\]|[a-z0-9.-]+)(?::(?P<port>[1-9][0-9]{0,4}))?"
)
MAXIMUM_PORT = 65535
DEFAULT_PORTS = {"http": 80, "https": 443}
MAXIMUM_HOST_NAME_LENGTH = 253
# One host-name label: 1 to 63 letters, digits or hyphens, starting and ending with no hyphen.
HOST_NAME_LABEL_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# A last label a browser reads as a number, which makes the whole host an IPv4 address.
NUMERIC_LABEL_PATTERN = re.compile(r"[0-9]+|0x[0-9a-f]*")
# Names the setting and the form it needs, never the rejected value.
INVALID_CORS_ORIGINS_MESSAGE = (
    "BACKEND_CORS_ORIGINS must list one or more comma-separated origins, each exactly as a"
    " browser sends it in the Origin header: lower-case scheme://host[:port] with no path or"
    f" trailing slash, a port from 1 to {MAXIMUM_PORT} with no leading zero, omitted when it"
    " is the scheme's default, and an IPv6 host in compressed form, such as"
    " http://127.0.0.1:3000; a wildcard is refused because CORS allows credentials"
)


def SplitCorsOrigins(originList: str) -> tuple[str, ...]:
    """Return the comma-separated entries of originList, trimmed, with blank entries dropped."""
    trimmedOrigins = (origin.strip() for origin in originList.split(","))
    return tuple(origin for origin in trimmedOrigins if origin)


def IsCanonicalPort(scheme: str, port: str | None) -> bool:
    """Return True when port is absent, or at most MAXIMUM_PORT and not the scheme's default."""
    if port is None:
        return True
    portNumber = int(port)
    return portNumber <= MAXIMUM_PORT and portNumber != DEFAULT_PORTS[scheme]


def IsCompressedIpv6Literal(bracketedHost: str) -> bool:
    """Return True when bracketedHost is [address], the address in RFC 5952 compressed form."""
    try:
        address = ipaddress.IPv6Address(bracketedHost[1:-1])
    except ipaddress.AddressValueError:
        return False
    return f"[{address.compressed}]" == bracketedHost


def IsCanonicalIpv4Address(hostName: str) -> bool:
    """Return True when hostName is an IPv4 address in dotted-quad form with no leading zero."""
    try:
        address = ipaddress.IPv4Address(hostName)
    except ipaddress.AddressValueError:
        return False
    return str(address) == hostName


def IsCanonicalHostName(hostName: str) -> bool:
    """Return True when hostName is a canonical IPv4 address or a valid lower-case host name.

    A browser reads a host whose last label is a number as an IPv4 address and sends it in
    dotted-quad form, so such a host must already be in that form.
    """
    hostLabels = hostName.split(".")
    if NUMERIC_LABEL_PATTERN.fullmatch(hostLabels[-1]):
        return IsCanonicalIpv4Address(hostName)
    everyLabelIsValid = all(HOST_NAME_LABEL_PATTERN.fullmatch(label) for label in hostLabels)
    return everyLabelIsValid and len(hostName) <= MAXIMUM_HOST_NAME_LENGTH


def IsBrowserOrigin(entry: str) -> bool:
    """Return True when entry is exactly the Origin header text a browser sends for an origin."""
    originParts = BROWSER_ORIGIN_SHAPE.fullmatch(entry)
    if originParts is None:
        return False
    host = originParts["host"]
    if host.startswith("["):
        hostIsCanonical = IsCompressedIpv6Literal(host)
    else:
        hostIsCanonical = IsCanonicalHostName(host)
    return hostIsCanonical and IsCanonicalPort(originParts["scheme"], originParts["port"])


class CommonSettings(BaseSettings):
    """Settings shared by the backend runtime and Alembic.

    Replaces the region-wide values every CardDemo program and job relied on: the runtime
    mode, the sync database connection that stands in for the VSAM catalog, the directory of
    the EBCDIC AWS.M2.CARDDEMO.* seed images, and the cost of the password hashes that replace
    the plaintext USRSEC passwords loaded by jcl/DUSRSECJ.jcl.
    """

    # extra="ignore": leftover lines such as AUTH_MODE, CARDDEMO_OUTPUT_DIR or TEST_* in
    # backend/.env are harmless. env_ignore_empty: an empty value such as "SECRET_KEY=" in the
    # process environment or in backend/.env is skipped like an unset one, so the next
    # lower-priority source with a nonempty value supplies the setting. The blank SECRET_KEY
    # default, which Settings.RejectWeakSecretKey refuses, applies only when no source supplies
    # a nonempty value. hide_input_in_errors: a ValidationError never echoes a rejected
    # SECRET_KEY or a database URL with its password.
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
        case_sensitive=True,
        hide_input_in_errors=True,
    )

    APP_ENV: Literal["local", "test", "production"] = "local"
    # repr=False and exclude=True on SECRET_KEY and the database URLs keep the key and the
    # database passwords out of repr(), str(), log records and model_dump output.
    SYNC_DATABASE_URL: str = Field(repr=False, exclude=True)
    CARDDEMO_EBCDIC_DIR: str = "data/EBCDIC"
    BCRYPT_ROUNDS: int = Field(default=12, ge=4, le=31)

    def IsProduction(self) -> bool:
        return self.APP_ENV == "production"

    def EbcdicDir(self) -> Path:
        """Return CARDDEMO_EBCDIC_DIR absolute: relative values resolve under REPO_ROOT."""
        return ResolveRepoPath(self.CARDDEMO_EBCDIC_DIR)


class Settings(CommonSettings):
    """Backend runtime settings: database, session, sign-on throttle, CORS, timeouts, docs.

    Replaces the RACF sign-on and terminal-session controls of the CICS region. Building it
    raises pydantic.ValidationError when SECRET_KEY is blank or shorter than
    MINIMUM_SECRET_KEY_LENGTH characters, so the backend refuses to start without a real key.
    It raises one as well when BACKEND_CORS_ORIGINS lists no origin, or an entry IsBrowserOrigin
    refuses because no browser sends it as an Origin, such as a wildcard, a path, a trailing
    slash, a leading-zero, out-of-range or default port, or a host not in canonical form, so
    credentialed CORS never allows every origin and OriginGuard never allows only an Origin no
    browser sends.
    """

    DATABASE_URL: str = Field(repr=False, exclude=True)
    SECRET_KEY: str = Field(default="", validate_default=True, repr=False, exclude=True)
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=60, gt=0)
    SESSION_COOKIE_NAME: str = "carddemo_session"
    LOGIN_MAX_ATTEMPTS: int = Field(default=5, gt=0, le=MAXIMUM_LOGIN_FAILURE_COUNT)
    LOGIN_LOCKOUT_SECONDS: int = Field(default=900, gt=0)
    # A plain comma-separated string: a list field would make pydantic-settings expect JSON.
    BACKEND_CORS_ORIGINS: str = "http://127.0.0.1:3000"
    DB_CONNECT_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    DB_COMMAND_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    HEALTH_READY_TIMEOUT_SECONDS: float = Field(default=2.0, gt=0, allow_inf_nan=False)
    ENABLE_API_DOCS: bool | None = None
    CARDDEMO_ASCII_DIR: str = "data/ASCII"

    @field_validator("SECRET_KEY")
    @classmethod
    def RejectWeakSecretKey(cls, value: str) -> str:
        """Refuse a blank or short SECRET_KEY, without putting the value in the message."""
        if len(value.strip()) < MINIMUM_SECRET_KEY_LENGTH:
            raise ValueError(REJECTED_KEY_MESSAGE)
        return value

    @field_validator("BACKEND_CORS_ORIGINS")
    @classmethod
    def RejectInvalidCorsOrigins(cls, value: str) -> str:
        """Refuse an origin list with no entry or a non-origin entry, without echoing the value."""
        originEntries = SplitCorsOrigins(value)
        everyEntryIsOrigin = all(IsBrowserOrigin(entry) for entry in originEntries)
        if not originEntries or not everyEntryIsOrigin:
            raise ValueError(INVALID_CORS_ORIGINS_MESSAGE)
        return value

    def CorsOrigins(self) -> tuple[str, ...]:
        """Return the BACKEND_CORS_ORIGINS entries through SplitCorsOrigins, each one an origin."""
        return SplitCorsOrigins(self.BACKEND_CORS_ORIGINS)

    def ApiDocsEnabled(self) -> bool:
        """Return ENABLE_API_DOCS when it is set, otherwise True outside production."""
        if self.ENABLE_API_DOCS is not None:
            return self.ENABLE_API_DOCS
        return not self.IsProduction()

    def AsciiDir(self) -> Path:
        """Return CARDDEMO_ASCII_DIR absolute: relative values resolve under REPO_ROOT."""
        return ResolveRepoPath(self.CARDDEMO_ASCII_DIR)


@lru_cache(maxsize=1)
def GetSettings() -> Settings:
    """Return the process-wide Settings, built once; tests reset it with cache_clear()."""
    return Settings()


class MigrationSettings(CommonSettings):
    """Settings of the Alembic environment and the sample-seed revision 0002.

    Replaces the dataset provisioning parameters of the IDCAMS load jobs (jcl/DUSRSECJ.jcl and
    the *FILE jobs). Alembic must not need SECRET_KEY, so this class does not declare it and
    migrations run with the backend's secret unset.
    """

    # Blank instead of required: offline SQL generation (alembic upgrade --sql) needs no
    # database URL, and alembic/env.py raises its own actionable error when an online run
    # finds the URL blank. A blank default holds no credential.
    SYNC_DATABASE_URL: str = Field(default="", repr=False, exclude=True)


def ParseDatabaseUrl(databaseUrl: str, variableName: str) -> URL:
    """Return a configured database URL as a SQLAlchemy URL, for an engine or Alembic.

    SQLAlchemy repeats the rejected text, password included, in its parse errors: ArgumentError
    quotes a string that does not match the URL form, and the ValueError of a non-numeric port
    quotes the port slot, which holds the password when the URL lacks its "@". Either becomes
    a ValueError that names only ``variableName``, such as DATABASE_URL, and is raised from
    None, so the traceback shows no chained error holding the URL.
    """
    try:
        return make_url(databaseUrl)
    except (ArgumentError, ValueError):
        raise ValueError(INVALID_DATABASE_URL_MESSAGE.format(variableName=variableName)) from None
