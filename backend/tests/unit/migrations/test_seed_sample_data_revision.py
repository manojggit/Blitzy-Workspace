"""Unit tests for Alembic revision 0002, the sample seed that replaces the IDCAMS REPRO loads.

The revision logs a skipped seed or truncate at INFO in offline mode and at WARNING under
APP_ENV=production. A malformed seed image is logged by whitelisted metadata only (image
directory, image file name, record number, error and cause classes) and re-raised as an
ImageFormatError with no record text, because the decoder's message can quote raw field content
such as a card number or an SSN. The directory and file name in an error line have each control
character escaped, so a line feed in either cannot split the line. A seeding upgrade sets the
seed-provenance marker revision 0008 reads only after the load, and a seeding downgrade clears
it after the truncate; a skipped or failed run, whose op stub offers only get_bind, issues no
statement at all. The revision is loaded from its file, and its Alembic context, op proxy and
MigrationSettings are replaced by stubs, so no backend/.env and no database is needed. The
malformed images are copies of data/EBCDIC with one DALYTRAN.PS field overwritten by text
carrying SENSITIVE_MARKER.
"""

import errno
import importlib.util
import logging
import shutil
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import NamedTuple, NoReturn

import pytest

from app.core.paths import REPO_ROOT
from app.seed.image_reader import SAMPLE_IMAGE_NAMES, ImageFormatError
from app.utils.record_layouts import DAILY_TRANSACTION_LAYOUT, RecordFormatError

REVISION_PATH = REPO_ROOT / "backend" / "alembic" / "versions" / "0002_seed_sample_data.py"
REVISION_MODULE_NAME = "revision_0002_seed_sample_data"
MIGRATION_LOGGER_NAME = "alembic.runtime.migration"
SHIPPED_EBCDIC_DIR = REPO_ROOT / "data" / "EBCDIC"
EBCDIC_CODEC = "cp037"
# The lowest BCRYPT_ROUNDS the settings accept; no row is ever hashed in these tests.
TEST_BCRYPT_ROUNDS = 4

DAILY_IMAGE_NAME = SAMPLE_IMAGE_NAMES["daily_transactions"].ebcdicName
DAILY_LRECL = DAILY_TRANSACTION_LAYOUT.lrecl
CORRUPT_RECORD_NUMBER = 2
# CVTRA06Y DALYTRAN-ORIG-TS X(26) at offset 278 and DALYTRAN-AMT S9(09)V99 at offset 132.
ORIG_TS_OFFSET = 278
TRAN_AMT_OFFSET = 132
# Stands for a card number, SSN or customer text that a malformed record carries.
SENSITIVE_MARKER = "SECRETMARK"
# Fails any use: no test may reach the database.
UNUSED_CONNECTION = object()
# The call-log entry of one LoadSampleData call; every other entry is a statement op.execute ran.
LOAD_CALL = "load the sample images"
# The rows per table the LoadSampleData stub reports, which upgrade() only logs.
STUB_SEED_COUNTS: dict[str, int] = {"accounts": 50, "transactions": 0}
EXPECTED_CLEAR_MARKER_SQL = "COMMENT ON SCHEMA batch IS NULL"


@dataclass(frozen=True)
class SettingsStub:
    """The MigrationSettings members revision 0002 reads, without backend/.env or a database."""

    imageDir: Path = SHIPPED_EBCDIC_DIR
    # Named as MigrationSettings names them: an exception to MK_Modernization Rule clause 1.
    APP_ENV: str = "local"
    BCRYPT_ROUNDS: int = TEST_BCRYPT_ROUNDS

    def EbcdicDir(self) -> Path:
        """Return the image directory, as MigrationSettings.EbcdicDir does."""
        return self.imageDir


class FieldCorruption(NamedTuple):
    """Text carrying SENSITIVE_MARKER, written over one field of DALYTRAN.PS record 2."""

    fieldOffset: int
    fieldText: str


class SkipCase(NamedTuple):
    """A run in which revision 0002 skips its step, and the level and reason it must log."""

    offlineMode: bool
    appEnv: str
    expectedLevel: int
    expectedReason: str


class MigrationStep(NamedTuple):
    """A revision entry point and the step name its skip message carries."""

    functionName: str
    stepName: str


class SeededRun(NamedTuple):
    """A revision entry point run with seeding allowed, and the calls it must make, in order.

    Each call name is LOAD_CALL or the name of the revision 0002 SQL constant op.execute runs.
    """

    functionName: str
    callNames: tuple[str, ...]


class ImageDirectoryCase(NamedTuple):
    """A configured image directory and the text revision 0002's error line shows for it."""

    imageDir: Path
    shownDir: str


# An image directory whose name holds a line feed before forged log text and an ANSI escape, an
# image name holding a carriage return, and both as the error lines show them: each control
# character as its escape, so neither name splits its line or forges another.
FORGED_LOG_TEXT = "ERROR forged-seed-success"
HOSTILE_IMAGE_DIR = REPO_ROOT / "data" / f"EBCDIC\n{FORGED_LOG_TEXT}\x1b[31m"
SHOWN_HOSTILE_IMAGE_DIR = f"{REPO_ROOT / 'data'}/EBCDIC\\n{FORGED_LOG_TEXT}\\x1b[31m"
HOSTILE_IMAGE_NAME = f"{DAILY_IMAGE_NAME}\r{FORGED_LOG_TEXT}"
SHOWN_HOSTILE_IMAGE_NAME = f"{DAILY_IMAGE_NAME}\\r{FORGED_LOG_TEXT}"
IMAGE_DIRECTORY_CASES = (
    pytest.param(
        ImageDirectoryCase(SHIPPED_EBCDIC_DIR, str(SHIPPED_EBCDIC_DIR)), id="shipped-directory"
    ),
    pytest.param(
        ImageDirectoryCase(HOSTILE_IMAGE_DIR, SHOWN_HOSTILE_IMAGE_DIR), id="control-bearing"
    ),
)
FIELD_CORRUPTIONS = (
    pytest.param(
        FieldCorruption(ORIG_TS_OFFSET, f"{SENSITIVE_MARKER}-123-45-6789-XYZ"),
        id="orig-ts-timestamp",
    ),
    pytest.param(FieldCorruption(TRAN_AMT_OFFSET, f"{SENSITIVE_MARKER}Z"), id="tran-amt-zoned"),
)
SKIP_CASES = (
    pytest.param(
        SkipCase(True, "local", logging.INFO, "offline mode has no connection to the tables"),
        id="offline-info",
    ),
    pytest.param(
        SkipCase(False, "production", logging.WARNING, "APP_ENV=production loads no sample data"),
        id="production-warning",
    ),
)
MIGRATION_STEPS = (
    pytest.param(MigrationStep("upgrade", "sample seed"), id="upgrade"),
    pytest.param(MigrationStep("downgrade", "sample seed truncate"), id="downgrade"),
)
SEEDED_RUNS = (
    pytest.param(SeededRun("upgrade", (LOAD_CALL, "RECORD_SEED_MARKER_SQL")), id="upgrade"),
    pytest.param(
        SeededRun("downgrade", ("TRUNCATE_DATASET_TABLES", "CLEAR_SEED_MARKER_SQL")),
        id="downgrade",
    ),
)
SEED_APP_ENVS = (pytest.param("local", id="local"), pytest.param("test", id="test"))


@pytest.fixture(name="seedRevision", scope="module")
def SeedRevision() -> ModuleType:
    """Return revision 0002 loaded from its file; its name starts with a digit."""
    revisionSpec = importlib.util.spec_from_file_location(REVISION_MODULE_NAME, REVISION_PATH)
    revisionModule = importlib.util.module_from_spec(revisionSpec)
    revisionSpec.loader.exec_module(revisionModule)
    return revisionModule


@pytest.fixture(name="stubRevision")
def StubRevision(seedRevision, monkeypatch) -> Callable[[bool, SettingsStub], ModuleType]:
    """Return a function that points revision 0002 at stubs for one run, then returns it.

    The function takes the run mode and the settings the revision is to read. Its op stub
    offers only get_bind, so a truncate the skip should have prevented fails the test.
    """

    def StubCollaborators(offlineMode: bool, settings: SettingsStub) -> ModuleType:
        contextStub = SimpleNamespace(is_offline_mode=lambda: offlineMode)
        monkeypatch.setattr(seedRevision, "context", contextStub)
        monkeypatch.setattr(seedRevision, "op", SimpleNamespace(get_bind=lambda: UNUSED_CONNECTION))
        monkeypatch.setattr(seedRevision, "MigrationSettings", lambda: settings)
        return seedRevision

    return StubCollaborators


@pytest.fixture(name="seedImageDir")
def SeedImageDir(request) -> Path:
    """Return a per-test directory holding copies of the eleven EBCDIC seed images 0002 reads.

    pytest's tmp_path is fetched by name, so the tests' injected parameters stay camelCase.
    """
    imageDir = request.getfixturevalue("tmp_path")
    for imageNames in SAMPLE_IMAGE_NAMES.values():
        imageName = imageNames.ebcdicName
        shutil.copyfile(SHIPPED_EBCDIC_DIR / imageName, imageDir / imageName)
    return imageDir


def OverwriteDailyField(imageDir: Path, corruption: FieldCorruption) -> None:
    """Write corruption's text, in cp037, over its field of DALYTRAN.PS record 2."""
    imagePath = imageDir / DAILY_IMAGE_NAME
    imageBytes = bytearray(imagePath.read_bytes())
    fieldStart = (CORRUPT_RECORD_NUMBER - 1) * DAILY_LRECL + corruption.fieldOffset
    fieldBytes = corruption.fieldText.encode(EBCDIC_CODEC)
    imageBytes[fieldStart : fieldStart + len(fieldBytes)] = fieldBytes
    imagePath.write_bytes(bytes(imageBytes))


def TruncateDailyImage(imageDir: Path) -> None:
    """Drop the last byte of DALYTRAN.PS, so its size is no longer a multiple of the LRECL."""
    imagePath = imageDir / DAILY_IMAGE_NAME
    imagePath.write_bytes(imagePath.read_bytes()[:-1])


def RecordTexts(logRecord: logging.LogRecord) -> tuple[str, ...]:
    """Return every text a handler could write for logRecord: message, args, exception text."""
    return (logRecord.getMessage(), repr(logRecord.args), str(logRecord.exc_text))


def SoleErrorMessage(logRecords: list[logging.LogRecord]) -> str:
    """Assert exactly one ERROR record, on Alembic's migration logger, and return its message."""
    errorRecords = [logRecord for logRecord in logRecords if logRecord.levelno == logging.ERROR]
    assert [logRecord.name for logRecord in errorRecords] == [MIGRATION_LOGGER_NAME]
    return errorRecords[0].getMessage()


def RecordSeedingCalls(revisionModule: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Point revision 0002's op and LoadSampleData at stubs that log each call; return the log.

    op.execute logs its statement; LoadSampleData logs LOAD_CALL and returns STUB_SEED_COUNTS.
    """
    callLog: list[str] = []

    def LoadStubImages(*loadArguments: object) -> dict[str, int]:
        callLog.append(LOAD_CALL)
        return dict(STUB_SEED_COUNTS)

    opStub = SimpleNamespace(get_bind=lambda: UNUSED_CONNECTION, execute=callLog.append)
    monkeypatch.setattr(revisionModule, "op", opStub)
    monkeypatch.setattr(revisionModule, "LoadSampleData", LoadStubImages)
    return callLog


def ExpectedCalls(revisionModule: ModuleType, seededRun: SeededRun) -> list[str]:
    """Return seededRun's call log: LOAD_CALL as it is, each SQL constant name as its text."""
    return [
        callName if callName == LOAD_CALL else getattr(revisionModule, callName)
        for callName in seededRun.callNames
    ]


def AssertWithheldError(raisedError: ImageFormatError) -> None:
    """Assert raisedError keeps the original's path and record number and chains nothing."""
    originalError = raisedError.__context__
    assert isinstance(originalError, ImageFormatError)
    assert Path(raisedError.path).name == DAILY_IMAGE_NAME
    assert raisedError.recordNumber == originalError.recordNumber
    assert raisedError.__cause__ is None
    assert raisedError.__suppress_context__ is True


@pytest.mark.parametrize("corruption", FIELD_CORRUPTIONS)
def test_malformed_field_is_logged_and_raised_without_its_text(
    stubRevision, corruption, seedImageDir, caplog
):
    OverwriteDailyField(seedImageDir, corruption)
    revisionModule = stubRevision(False, SettingsStub(seedImageDir))
    caplog.set_level(logging.INFO, logger=MIGRATION_LOGGER_NAME)
    with pytest.raises(ImageFormatError) as raised:
        revisionModule.upgrade()
    AssertWithheldError(raised.value)
    assert raised.value.recordNumber == CORRUPT_RECORD_NUMBER
    # The suppressed original does quote the marker, so the checks below are not vacuous.
    assert SENSITIVE_MARKER in str(raised.value.__context__)
    assert SENSITIVE_MARKER not in "".join(traceback.format_exception(raised.value))
    for logRecord in caplog.records:
        assert all(SENSITIVE_MARKER not in recordText for recordText in RecordTexts(logRecord))
    errorMessage = SoleErrorMessage(caplog.records)
    assert f"image {DAILY_IMAGE_NAME} record {CORRUPT_RECORD_NUMBER}," in errorMessage
    assert f"cause {RecordFormatError.__name__}" in errorMessage


def test_causeless_framing_error_is_logged_and_raised_by_its_fixed_code(
    stubRevision, seedImageDir, caplog
):
    TruncateDailyImage(seedImageDir)
    revisionModule = stubRevision(False, SettingsStub(seedImageDir))
    caplog.set_level(logging.INFO, logger=MIGRATION_LOGGER_NAME)
    with pytest.raises(ImageFormatError) as raised:
        revisionModule.upgrade()
    AssertWithheldError(raised.value)
    assert raised.value.reason == "framing; the record text is withheld"
    errorMessage = SoleErrorMessage(caplog.records)
    assert f"image {DAILY_IMAGE_NAME} record {raised.value.recordNumber}," in errorMessage
    assert errorMessage.endswith("cause framing")


@pytest.mark.parametrize("directoryCase", IMAGE_DIRECTORY_CASES)
def test_file_not_found_is_logged_by_its_class_and_reraised_unchanged(
    stubRevision, monkeypatch, caplog, directoryCase
):
    missingError = FileNotFoundError(errno.ENOENT, "No such file", f"{SENSITIVE_MARKER}.PS")

    def RaiseMissingImage(*loadArguments: object) -> NoReturn:
        raise missingError

    revisionModule = stubRevision(False, SettingsStub(directoryCase.imageDir))
    monkeypatch.setattr(revisionModule, "LoadSampleData", RaiseMissingImage)
    caplog.set_level(logging.INFO, logger=MIGRATION_LOGGER_NAME)
    with pytest.raises(FileNotFoundError) as raised:
        revisionModule.upgrade()
    assert raised.value is missingError
    expectedMessage = f"sample seed failed for the images in {directoryCase.shownDir}: "
    assert SoleErrorMessage(caplog.records) == expectedMessage + "FileNotFoundError"
    assert SENSITIVE_MARKER not in caplog.text


def test_image_format_error_log_escapes_the_directory_and_image_name(seedRevision, caplog):
    imageError = ImageFormatError(HOSTILE_IMAGE_DIR / HOSTILE_IMAGE_NAME, 0, "framing")
    caplog.set_level(logging.INFO, logger=MIGRATION_LOGGER_NAME)
    seedRevision.LogImageFormatError(HOSTILE_IMAGE_DIR, imageError, "framing")
    expectedMessage = (
        f"sample seed failed for the images in {SHOWN_HOSTILE_IMAGE_DIR}: ImageFormatError"
        f" in image {SHOWN_HOSTILE_IMAGE_NAME} record 0, cause framing"
    )
    assert SoleErrorMessage(caplog.records) == expectedMessage


@pytest.mark.parametrize("migrationStep", MIGRATION_STEPS)
@pytest.mark.parametrize("skipCase", SKIP_CASES)
def test_skipped_step_logs_offline_at_info_and_production_at_warning(
    stubRevision, skipCase, migrationStep, caplog
):
    revisionModule = stubRevision(skipCase.offlineMode, SettingsStub(APP_ENV=skipCase.appEnv))
    caplog.set_level(logging.INFO, logger=MIGRATION_LOGGER_NAME)
    getattr(revisionModule, migrationStep.functionName)()
    expectedMessage = f"{migrationStep.stepName} skipped: {skipCase.expectedReason}"
    assert [(logRecord.levelno, logRecord.getMessage()) for logRecord in caplog.records] == [
        (skipCase.expectedLevel, expectedMessage)
    ]


@pytest.mark.parametrize("seededRun", SEEDED_RUNS)
@pytest.mark.parametrize("appEnv", SEED_APP_ENVS)
def test_seeding_run_sets_or_clears_the_seed_marker_after_its_data_step(
    stubRevision, appEnv, seededRun, monkeypatch
):
    revisionModule = stubRevision(False, SettingsStub(APP_ENV=appEnv))
    callLog = RecordSeedingCalls(revisionModule, monkeypatch)
    getattr(revisionModule, seededRun.functionName)()
    assert callLog == ExpectedCalls(revisionModule, seededRun)


def test_seed_marker_statements_set_and_clear_the_batch_schema_comment(seedRevision):
    expectedRecordSql = f"COMMENT ON SCHEMA batch IS '{seedRevision.SEED_MARKER_COMMENT}'"
    assert seedRevision.RECORD_SEED_MARKER_SQL == expectedRecordSql
    assert seedRevision.CLEAR_SEED_MARKER_SQL == EXPECTED_CLEAR_MARKER_SQL
    # The marker is embedded in a quoted SQL literal, so it must hold no quote of its own.
    assert "'" not in seedRevision.SEED_MARKER_COMMENT
