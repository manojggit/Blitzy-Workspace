"""Unit tests for app.core.paths, the repository-root resolution of data paths.

The unit replaces the JCL DSN= dataset references, such as AWS.M2.CARDDEMO.ACCTDATA.PS in
jcl/ACCTFILE.jcl, and the CICS FILE DSNAME bindings of csd/CARDDEMO.CSD with paths relative to
the repository root, as specified in the Technical Specification, Section 0.7.3. These tests
pin that a relative path, and the CARDDEMO_EBCDIC_DIR and CARDDEMO_ASCII_DIR values that
app.core.config.Settings resolves through it, give one absolute path whether the process starts
in the repository root, in backend/ (Alembic, uvicorn), in frontend/ (Playwright launching the
batch CLI) or outside the checkout, and that an absolute value, as Compose passes in the
containers, is kept unchanged. Settings never reads backend/.env here (_env_file=None), and no
database is touched.
"""

import secrets
from pathlib import Path

import pytest

import app
from app.core.config import Settings
from app.core.paths import ENV_FILE, REPO_ROOT, ResolveRepoPath

# The absolute directories docker-compose.yml passes to the backend container.
CONTAINER_EBCDIC_DIR = "/app/data/EBCDIC"
CONTAINER_OUTPUT_DIR = "/srv/var/batch-output"
# Directories a CardDemo process starts in, relative to REPO_ROOT; "" is the root itself.
WORKING_DIRECTORIES = ("", "backend", "frontend")
# Stands in the parametrization for tmp_path, a directory outside the checkout.
OUTSIDE_CHECKOUT = None
STARTING_DIRECTORIES = (*WORKING_DIRECTORIES, OUTSIDE_CHECKOUT)
STARTING_DIRECTORY_IDS = ("repo-root", "backend", "frontend", "outside-checkout")
DIRECTORY_VARIABLES = ("CARDDEMO_EBCDIC_DIR", "CARDDEMO_ASCII_DIR")
EBCDIC_IMAGE_DIR = REPO_ROOT / "data" / "EBCDIC"
ASCII_IMAGE_DIR = REPO_ROOT / "data" / "ASCII"
RELATIVE_OVERRIDE = "custom/images"
# Settings requires both database URLs. Nothing connects to them, and they hold no password.
UNUSED_ASYNC_URL = "postgresql+asyncpg://paths_test@127.0.0.1:5432/paths_test"
UNUSED_SYNC_URL = "postgresql+psycopg2://paths_test@127.0.0.1:5432/paths_test"
SECRET_KEY_BYTES = 32


def BuildDirectorySettings():
    """Return Settings built from the process environment alone, never from backend/.env.

    The SECRET_KEY is generated and the database URLs are never connected to, so only the
    directory settings depend on the environment the test prepares.
    """
    return Settings(
        _env_file=None,
        SECRET_KEY=secrets.token_urlsafe(SECRET_KEY_BYTES),
        DATABASE_URL=UNUSED_ASYNC_URL,
        SYNC_DATABASE_URL=UNUSED_SYNC_URL,
    )


@pytest.fixture(name="cleanDirectoryEnv")
def CleanDirectoryEnv(monkeypatch):
    """Remove the directory variables, so a developer shell cannot replace the defaults."""
    for variableName in DIRECTORY_VARIABLES:
        monkeypatch.delenv(variableName, raising=False)


@pytest.fixture(name="startingDirectory", params=STARTING_DIRECTORIES, ids=STARTING_DIRECTORY_IDS)
def StartingDirectory(request, monkeypatch, tmp_path):
    """Change the working directory to one starting directory for the test, and return it."""
    if request.param is OUTSIDE_CHECKOUT:
        startingDirectory = tmp_path
    else:
        startingDirectory = REPO_ROOT / request.param
    monkeypatch.chdir(startingDirectory)
    return startingDirectory


def test_repo_root_is_two_levels_above_app_package():
    assert REPO_ROOT == Path(app.__file__).resolve().parents[2]
    assert REPO_ROOT.is_absolute()


def test_repo_root_contains_legacy_backend_and_seed_trees():
    assert (REPO_ROOT / "cbl").is_dir()
    assert (REPO_ROOT / "backend" / "app").is_dir()
    assert EBCDIC_IMAGE_DIR.is_dir()
    assert ASCII_IMAGE_DIR.is_dir()


def test_env_file_points_to_backend_dotenv_read_by_settings():
    assert ENV_FILE == REPO_ROOT / "backend" / ".env"
    assert Settings.model_config["env_file"] == ENV_FILE


def test_relative_path_resolves_against_repo_root(startingDirectory):
    assert Path.cwd().samefile(startingDirectory)
    assert ResolveRepoPath("data/EBCDIC") == EBCDIC_IMAGE_DIR
    assert ResolveRepoPath(Path("data/ASCII")) == ASCII_IMAGE_DIR


@pytest.mark.usefixtures("startingDirectory", "cleanDirectoryEnv")
def test_settings_directory_defaults_resolve_from_any_directory():
    directorySettings = BuildDirectorySettings()
    assert directorySettings.CARDDEMO_EBCDIC_DIR == "data/EBCDIC"
    assert directorySettings.CARDDEMO_ASCII_DIR == "data/ASCII"
    assert directorySettings.EbcdicDir() == EBCDIC_IMAGE_DIR
    assert directorySettings.AsciiDir() == ASCII_IMAGE_DIR


def test_absolute_path_is_returned_unchanged(tmp_path):
    assert ResolveRepoPath(CONTAINER_EBCDIC_DIR) == Path(CONTAINER_EBCDIC_DIR)
    assert ResolveRepoPath(CONTAINER_OUTPUT_DIR) == Path(CONTAINER_OUTPUT_DIR)
    assert ResolveRepoPath(tmp_path) == tmp_path


@pytest.mark.usefixtures("startingDirectory")
@pytest.mark.parametrize(
    ("overrideValue", "expectedPath"),
    [
        (CONTAINER_EBCDIC_DIR, Path(CONTAINER_EBCDIC_DIR)),
        (RELATIVE_OVERRIDE, REPO_ROOT / "custom" / "images"),
    ],
    ids=["absolute-container-path", "relative-path"],
)
def test_directory_overrides_resolve_from_any_directory(overrideValue, expectedPath, monkeypatch):
    for variableName in DIRECTORY_VARIABLES:
        monkeypatch.setenv(variableName, overrideValue)
    directorySettings = BuildDirectorySettings()
    assert directorySettings.EbcdicDir() == expectedPath
    assert directorySettings.AsciiDir() == expectedPath


def test_absolute_env_override_is_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv("CARDDEMO_EBCDIC_DIR", str(tmp_path / "EBCDIC"))
    monkeypatch.setenv("CARDDEMO_ASCII_DIR", str(tmp_path / "ASCII"))
    directorySettings = BuildDirectorySettings()
    assert directorySettings.EbcdicDir() == tmp_path / "EBCDIC"
    assert directorySettings.AsciiDir() == tmp_path / "ASCII"
