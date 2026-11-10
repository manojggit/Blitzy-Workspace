"""Test-database bootstrap: the batch conftest reads backend/.env as the settings classes do.

Pins ResolveTestDatabaseUrl, which batch/tests/conftest.py calls at import, to the Technical
Specification, Sections 0.7.3 ("Path and environment-file resolution": the test variables are
resolved like the settings, from backend/.env with the process environment overriding it) and
0.7.6 (TEST_SYNC_DATABASE_URL defaults to SYNC_DATABASE_URL with the database renamed
carddemo_test):

- The python-dotenv syntax that BatchSettings accepts resolves exactly: an inline ``# comment``
  after an unquoted value, ``export NAME=...`` assignments, and double- and single-quoted values.
- Without TEST_SYNC_DATABASE_URL, or with it blank, SYNC_DATABASE_URL is used with the database
  carddemo_test; with neither file nor variable present the process environment alone applies.
- A nonempty process value overrides the file; an empty one counts as unset and falls through
  to the file.
- No URL, or one that does not parse, raises RuntimeError naming the variable, never the URL.

Each case writes its own env file under tmp_path and clears both variables, which the conftest
set at import, from the process environment, so no case reads backend/.env or needs a database.
"""

import secrets
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

import pytest

from batch.tests.conftest import INVALID_URL_MESSAGE, MISSING_URL_MESSAGE, ResolveTestDatabaseUrl

TEST_SYNC_NAME = "TEST_SYNC_DATABASE_URL"
SYNC_NAME = "SYNC_DATABASE_URL"
URL_VARIABLE_NAMES = (TEST_SYNC_NAME, SYNC_NAME)
ENV_FILE_NAME = ".env"
TEST_URL = "postgresql+psycopg2://tester@127.0.0.1:5432/carddemo_bootstrap_test"
OVERRIDE_URL = "postgresql+psycopg2://tester@127.0.0.1:5432/carddemo_override_test"
SYNC_URL = "postgresql+psycopg2://tester@127.0.0.1:5432/carddemo"
RENAMED_SYNC_URL = "postgresql+psycopg2://tester@127.0.0.1:5432/carddemo_test"
GENERATED_PASSWORD = secrets.token_urlsafe(16)
# Unparsable URLs that carry a password: a scheme without its colon, and a non-numeric port.
MALFORMED_SCHEME_URL = f"postgresql+psycopg2//tester:{GENERATED_PASSWORD}@127.0.0.1:5432/carddemo"
MALFORMED_PORT_URL = f"postgresql+psycopg2://tester:{GENERATED_PASSWORD}@127.0.0.1:port/carddemo"
NO_PROCESS_VALUES: Mapping[str, str] = {}


class BootstrapCase(NamedTuple):
    """The env-file lines, the process values set over them, and the URL the bootstrap returns.

    An empty envLines leaves the env file absent, as in a container without backend/.env.
    """

    envLines: tuple[str, ...]
    processValues: Mapping[str, str]
    expectedUrl: str


RESOLUTION_CASES = [
    pytest.param(
        BootstrapCase(
            (f"{TEST_SYNC_NAME}={TEST_URL} # local tests", f"{SYNC_NAME}={SYNC_URL}"),
            NO_PROCESS_VALUES,
            TEST_URL,
        ),
        id="inline-comment",
    ),
    pytest.param(
        BootstrapCase(
            (f"export {TEST_SYNC_NAME}={TEST_URL}", f"{SYNC_NAME}={SYNC_URL}"),
            NO_PROCESS_VALUES,
            TEST_URL,
        ),
        id="export",
    ),
    pytest.param(
        BootstrapCase((f'{TEST_SYNC_NAME}="{TEST_URL}"',), NO_PROCESS_VALUES, TEST_URL),
        id="double-quoted",
    ),
    pytest.param(
        BootstrapCase((f"{TEST_SYNC_NAME}='{TEST_URL}'",), NO_PROCESS_VALUES, TEST_URL),
        id="single-quoted",
    ),
    pytest.param(
        BootstrapCase(
            (f"{SYNC_NAME}={SYNC_URL} # local database",), NO_PROCESS_VALUES, RENAMED_SYNC_URL
        ),
        id="fallback-inline-comment",
    ),
    pytest.param(
        BootstrapCase((f"export {SYNC_NAME}={SYNC_URL}",), NO_PROCESS_VALUES, RENAMED_SYNC_URL),
        id="fallback-export",
    ),
    pytest.param(
        BootstrapCase((f'{SYNC_NAME}="{SYNC_URL}"',), NO_PROCESS_VALUES, RENAMED_SYNC_URL),
        id="fallback-double-quoted",
    ),
    pytest.param(
        BootstrapCase((f"{SYNC_NAME}='{SYNC_URL}'",), NO_PROCESS_VALUES, RENAMED_SYNC_URL),
        id="fallback-single-quoted",
    ),
    pytest.param(
        BootstrapCase(
            (f"{TEST_SYNC_NAME}=", f"{SYNC_NAME}={SYNC_URL}"), NO_PROCESS_VALUES, RENAMED_SYNC_URL
        ),
        id="fallback-blank-test-url",
    ),
    pytest.param(
        BootstrapCase((), {SYNC_NAME: SYNC_URL}, RENAMED_SYNC_URL),
        id="fallback-process-only",
    ),
    pytest.param(
        BootstrapCase(
            (f"{TEST_SYNC_NAME}={TEST_URL}",), {TEST_SYNC_NAME: OVERRIDE_URL}, OVERRIDE_URL
        ),
        id="process-overrides-file",
    ),
    pytest.param(
        BootstrapCase((f"{TEST_SYNC_NAME}={TEST_URL}",), {TEST_SYNC_NAME: ""}, TEST_URL),
        id="empty-process-falls-through",
    ),
]

UNRESOLVABLE_CASES = [
    pytest.param((), MISSING_URL_MESSAGE, id="no-url"),
    pytest.param(
        (f"{SYNC_NAME}={MALFORMED_SCHEME_URL}",),
        INVALID_URL_MESSAGE.format(variableName=SYNC_NAME),
        id="malformed-scheme",
    ),
    pytest.param(
        (f"{SYNC_NAME}={MALFORMED_PORT_URL}",),
        INVALID_URL_MESSAGE.format(variableName=SYNC_NAME),
        id="malformed-port",
    ),
]


@pytest.fixture(name="clearedUrlVariables", autouse=True)
def ClearedUrlVariablesFixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove both URL variables, which the conftest set at import, for the test's duration."""
    for variableName in URL_VARIABLE_NAMES:
        monkeypatch.delenv(variableName, raising=False)


def WriteEnvFile(directory: Path, envLines: tuple[str, ...]) -> Path:
    """Return the env-file path under directory, holding envLines; absent when envLines is empty."""
    envFile = directory / ENV_FILE_NAME
    if envLines:
        envFile.write_text("\n".join(envLines) + "\n", encoding="utf-8")
    return envFile


@pytest.mark.parametrize("bootstrapCase", RESOLUTION_CASES)
def test_resolves_url_as_settings_read_it(
    bootstrapCase: BootstrapCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The URL comes from the process value, else the env file, parsed with dotenv syntax."""
    envFile = WriteEnvFile(tmp_path, bootstrapCase.envLines)
    for variableName, processValue in bootstrapCase.processValues.items():
        monkeypatch.setenv(variableName, processValue)
    assert ResolveTestDatabaseUrl(envFile) == bootstrapCase.expectedUrl


@pytest.mark.parametrize(("envLines", "expectedMessage"), UNRESOLVABLE_CASES)
def test_unresolvable_url_raises_redacted_error(
    envLines: tuple[str, ...], expectedMessage: str, tmp_path: Path
) -> None:
    """No URL or an unparsable one stops the bootstrap; its traceback never holds the password."""
    envFile = WriteEnvFile(tmp_path, envLines)
    with pytest.raises(RuntimeError) as raised:
        ResolveTestDatabaseUrl(envFile)
    assert str(raised.value) == expectedMessage
    assert GENERATED_PASSWORD not in "".join(traceback.format_exception(raised.value))
