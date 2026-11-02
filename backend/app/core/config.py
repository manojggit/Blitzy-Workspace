"""Environment settings of the CardDemo backend, its Alembic migrations and the batch CLI.

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
credential, so no credential is written in code.

Importing this module reads no setting; values are read when a settings class is built.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.paths import ENV_FILE, ResolveRepoPath

API_V1_PREFIX = "/api/v1"
MINIMUM_SECRET_KEY_LENGTH = 32
REJECTED_KEY_MESSAGE = (
    f"SECRET_KEY must be set to at least {MINIMUM_SECRET_KEY_LENGTH} characters"
    " (generate one with: openssl rand -hex 32)"
)


class CommonSettings(BaseSettings):
    """Settings shared by the backend runtime, Alembic and the batch CLI.

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
    SYNC_DATABASE_URL: str
    CARDDEMO_EBCDIC_DIR: str = "data/EBCDIC"
    BCRYPT_ROUNDS: int = Field(default=12, ge=4, le=31)

    def IsProduction(self) -> bool:
        """Return True when APP_ENV is production."""
        return self.APP_ENV == "production"

    def EbcdicDir(self) -> Path:
        """Return CARDDEMO_EBCDIC_DIR absolute: relative values resolve under REPO_ROOT."""
        return ResolveRepoPath(self.CARDDEMO_EBCDIC_DIR)


class Settings(CommonSettings):
    """Backend runtime settings: database, session, sign-on throttle, CORS, timeouts, docs.

    Replaces the RACF sign-on and terminal-session controls of the CICS region. Building it
    raises pydantic.ValidationError when SECRET_KEY is blank or shorter than
    MINIMUM_SECRET_KEY_LENGTH characters, so the backend refuses to start without a real key.
    """

    DATABASE_URL: str
    SECRET_KEY: str = Field(default="", validate_default=True)
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=60, gt=0)
    SESSION_COOKIE_NAME: str = "carddemo_session"
    LOGIN_MAX_ATTEMPTS: int = Field(default=5, gt=0)
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

    def CorsOrigins(self) -> tuple[str, ...]:
        """Return the BACKEND_CORS_ORIGINS entries, trimmed, with blank entries dropped."""
        trimmedOrigins = (origin.strip() for origin in self.BACKEND_CORS_ORIGINS.split(","))
        return tuple(origin for origin in trimmedOrigins if origin)

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
    SYNC_DATABASE_URL: str = ""
