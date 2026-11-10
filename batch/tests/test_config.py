"""BatchSettings: the environment contract every carddemo-batch command builds its settings from.

Replaces: the DSN=AWS.M2.CARDDEMO.* dataset bindings and the step values the batch JCL
(jcl/POSTTRAN.jcl, jcl/INTCALC.jcl, jcl/CREASTMT.JCL, jcl/DUSRSECJ.jcl and the *FILE load
jobs) gave each step. Specified in the Technical Specification, Sections 0.7.3 and 0.7.6.

Pins batch/config.py:

- importing batch.cli and every other production batch module loads no backend configuration
  (app.core.config), so batch reaches the backend only through the modules Section 0.7.3 lists;
- BatchSettings declares exactly APP_ENV, SYNC_DATABASE_URL, BCRYPT_ROUNDS, CARDDEMO_ASCII_DIR,
  CARDDEMO_EBCDIC_DIR and CARDDEMO_OUTPUT_DIR, builds from an env file holding the backend's
  lines with SECRET_KEY unset, and keeps the database URL out of repr() and model_dump();
- the process environment overrides the env file, which overrides the documented defaults, and
  an empty value in either source is skipped like an unset one;
- an unset SYNC_DATABASE_URL, an out-of-range or malformed BCRYPT_ROUNDS and an unknown APP_ENV
  are refused on that field alone, with an error that echoes no input, so neither the rejected
  value nor the database URL reaches a log.

batch/tests/conftest.py writes SYNC_DATABASE_URL, APP_ENV and BCRYPT_ROUNDS into os.environ at
import, so every variable here changes through monkeypatch, which restores the bootstrap after
each test. Env files are written under tmp_path and backend/.env is never read. The URLs carry
no credential, except one whose password secrets.token_urlsafe generates at run time
(MK_Modernization Rule, clause 3). No test needs the database.
"""

import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from app.core.paths import REPO_ROOT
from pydantic import ValidationError

from batch.config import BatchSettings

BACKEND_CONFIG_MODULE = "app.core.config"
BATCH_DIR = REPO_ROOT / "batch"
BATCH_TESTS_DIR = BATCH_DIR / "tests"
# Runs in a fresh interpreter, so nothing this session or its conftest imported is counted.
BOUNDARY_PROBE = (
    "import importlib\n"
    "import sys\n"
    "for moduleName in sys.argv[1:]:\n"
    "    importlib.import_module(moduleName)\n"
    f"print({BACKEND_CONFIG_MODULE!r} in sys.modules)\n"
)
PROBE_TIMEOUT_SECONDS = 120
BATCH_VARIABLES = frozenset(
    {
        "APP_ENV",
        "SYNC_DATABASE_URL",
        "BCRYPT_ROUNDS",
        "CARDDEMO_ASCII_DIR",
        "CARDDEMO_EBCDIC_DIR",
        "CARDDEMO_OUTPUT_DIR",
    }
)
# The backend's session-signing key variable, which BatchSettings never declares.
BACKEND_SIGNING_KEY_VARIABLE = "SECRET_KEY"
SYNC_URL_VARIABLE = "SYNC_DATABASE_URL"
OUTPUT_DIR_VARIABLE = "CARDDEMO_OUTPUT_DIR"
# BatchSettings only stores the URL, so no database is reached and none needs credentials.
CREDENTIAL_FREE_SYNC_URL = "postgresql+psycopg2://127.0.0.1/batch_settings_test"
# Lines of the shared backend/.env that only the backend declares; batch ignores them.
BACKEND_ONLY_LINES = {
    "DATABASE_URL": "postgresql+asyncpg://127.0.0.1/batch_settings_test",
    "SESSION_COOKIE_NAME": "carddemo_session",
}
# The text pydantic adds to each error line unless hide_input_in_errors is set.
ECHOED_INPUT_MARKER = "input_value"
PROCESS_OUTPUT_DIR = "process-output"
FILE_OUTPUT_DIR = "file-output"
EMPTY_VALUE = ""


@dataclass(frozen=True)
class PrecedenceCase:
    """One variable's process and env-file values (None when absent) and the value kept."""

    variableName: str
    processValue: str | None
    fileValue: str | None
    expectedValue: str | int


PRECEDENCE_CASES = (
    pytest.param(
        PrecedenceCase(
            OUTPUT_DIR_VARIABLE, PROCESS_OUTPUT_DIR, FILE_OUTPUT_DIR, PROCESS_OUTPUT_DIR
        ),
        id="process-over-file",
    ),
    pytest.param(
        PrecedenceCase(OUTPUT_DIR_VARIABLE, None, FILE_OUTPUT_DIR, FILE_OUTPUT_DIR),
        id="file-over-default",
    ),
    pytest.param(
        PrecedenceCase(OUTPUT_DIR_VARIABLE, EMPTY_VALUE, FILE_OUTPUT_DIR, FILE_OUTPUT_DIR),
        id="empty-process-falls-through-to-file",
    ),
    pytest.param(
        PrecedenceCase(OUTPUT_DIR_VARIABLE, EMPTY_VALUE, EMPTY_VALUE, "var/batch-output"),
        id="empty-process-and-file-fall-through-to-default",
    ),
    pytest.param(
        PrecedenceCase(OUTPUT_DIR_VARIABLE, None, None, "var/batch-output"),
        id="output-dir-default",
    ),
    pytest.param(PrecedenceCase("CARDDEMO_EBCDIC_DIR", None, None, "data/EBCDIC"), id="ebcdic"),
    pytest.param(PrecedenceCase("CARDDEMO_ASCII_DIR", None, None, "data/ASCII"), id="ascii"),
    pytest.param(PrecedenceCase("APP_ENV", None, None, "local"), id="app-env-default"),
    pytest.param(PrecedenceCase("APP_ENV", "production", "test", "production"), id="app-env"),
    pytest.param(PrecedenceCase("BCRYPT_ROUNDS", None, None, 12), id="bcrypt-rounds-default"),
    pytest.param(PrecedenceCase("BCRYPT_ROUNDS", "4", "31", 4), id="bcrypt-rounds-lowest"),
    pytest.param(PrecedenceCase("BCRYPT_ROUNDS", None, "31", 31), id="bcrypt-rounds-highest"),
)
REJECTED_VALUES = (
    pytest.param("BCRYPT_ROUNDS", "3", id="bcrypt-rounds-below-4"),
    pytest.param("BCRYPT_ROUNDS", "32", id="bcrypt-rounds-above-31"),
    pytest.param("BCRYPT_ROUNDS", "twelve", id="bcrypt-rounds-not-an-integer"),
    pytest.param("APP_ENV", "staging", id="app-env-unknown"),
)


def ProductionModuleNames() -> list[str]:
    """Return the dotted name of every batch module outside batch/tests, in path order."""
    modulePaths = sorted(BATCH_DIR.rglob("*.py"))
    productionPaths = [path for path in modulePaths if not path.is_relative_to(BATCH_TESTS_DIR)]
    relativeParts = [path.relative_to(REPO_ROOT).with_suffix("").parts for path in productionPaths]
    return [".".join(parts).removesuffix(".__init__") for parts in relativeParts]


def WriteEnvFile(envFile: Path, fileValues: dict[str, str]) -> Path:
    """Write fileValues to envFile as NAME=value lines, as backend/.env holds them; return it."""
    envLines = [f"{variableName}={value}\n" for variableName, value in fileValues.items()]
    envFile.write_text("".join(envLines), encoding="utf-8")
    return envFile


def SetProcessValue(monkeypatch: pytest.MonkeyPatch, variableName: str, value: str | None) -> None:
    """Set variableName in the process environment to value, or delete it when value is None."""
    if value is None:
        monkeypatch.delenv(variableName, raising=False)
    else:
        monkeypatch.setenv(variableName, value)


def BuildDatabaseUrl(databasePassword: str) -> str:
    """Return a psycopg2 URL on host 127.0.0.1 whose credentials carry databasePassword."""
    return f"postgresql+psycopg2://batch_settings_test:{databasePassword}@127.0.0.1/settings_test"


def ErrorLocations(validationError: ValidationError) -> list[tuple[str | int, ...]]:
    """Return the field location of every error a ValidationError holds, in order."""
    return [fieldError["loc"] for fieldError in validationError.errors()]


def test_batch_modules_load_no_backend_configuration() -> None:
    """A fresh interpreter importing every production batch module never loads app.core.config."""
    moduleNames = ProductionModuleNames()
    assert "batch.cli" in moduleNames
    probe = subprocess.run(
        [sys.executable, "-c", BOUNDARY_PROBE, *moduleNames],
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
        check=False,
        cwd=REPO_ROOT,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == str(False)


def test_builds_from_env_file_without_secret_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With SECRET_KEY unset, the shared env file alone builds the six batch settings."""
    monkeypatch.delenv(BACKEND_SIGNING_KEY_VARIABLE, raising=False)
    monkeypatch.delenv(SYNC_URL_VARIABLE, raising=False)
    fileValues = {SYNC_URL_VARIABLE: CREDENTIAL_FREE_SYNC_URL, **BACKEND_ONLY_LINES}
    builtSettings = BatchSettings(_env_file=WriteEnvFile(tmp_path / ".env", fileValues))
    assert set(BatchSettings.model_fields) == BATCH_VARIABLES
    assert builtSettings.SYNC_DATABASE_URL == CREDENTIAL_FREE_SYNC_URL
    assert CREDENTIAL_FREE_SYNC_URL not in repr(builtSettings)
    assert SYNC_URL_VARIABLE not in builtSettings.model_dump()


@pytest.mark.parametrize("precedenceCase", PRECEDENCE_CASES)
def test_process_environment_overrides_env_file_over_defaults(
    precedenceCase: PrecedenceCase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The process value wins over the env file's, which wins over the default; empty is unset."""
    fileValues = {SYNC_URL_VARIABLE: CREDENTIAL_FREE_SYNC_URL}
    if precedenceCase.fileValue is not None:
        fileValues[precedenceCase.variableName] = precedenceCase.fileValue
    monkeypatch.delenv(SYNC_URL_VARIABLE, raising=False)
    SetProcessValue(monkeypatch, precedenceCase.variableName, precedenceCase.processValue)
    builtSettings = BatchSettings(_env_file=WriteEnvFile(tmp_path / ".env", fileValues))
    assert getattr(builtSettings, precedenceCase.variableName) == precedenceCase.expectedValue


def test_unset_sync_database_url_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env file and SYNC_DATABASE_URL unset, building fails on that field alone."""
    monkeypatch.delenv(SYNC_URL_VARIABLE, raising=False)
    with pytest.raises(ValidationError) as raisedError:
        BatchSettings(_env_file=None)
    assert ErrorLocations(raisedError.value) == [(SYNC_URL_VARIABLE,)]
    assert ECHOED_INPUT_MARKER not in str(raisedError.value)


@pytest.mark.parametrize(("variableName", "rejectedValue"), REJECTED_VALUES)
def test_rejected_value_error_echoes_no_input(
    variableName: str, rejectedValue: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected value fails on its own field, and the error repeats neither it nor the URL."""
    databasePassword = secrets.token_urlsafe(16)
    monkeypatch.setenv(SYNC_URL_VARIABLE, BuildDatabaseUrl(databasePassword))
    monkeypatch.setenv(variableName, rejectedValue)
    with pytest.raises(ValidationError) as raisedError:
        BatchSettings(_env_file=None)
    errorText = str(raisedError.value)
    assert ErrorLocations(raisedError.value) == [(variableName,)]
    assert ECHOED_INPUT_MARKER not in errorText
    assert databasePassword not in errorText
