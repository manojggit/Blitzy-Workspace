"""GDG retention recovery: the next ReconcileCatalog completes an interrupted LIMIT 5 retention.

Pins batch/orchestration/gdg.py to LIMIT 5 SCRATCH (jcl/DALYREJS.jcl lines 25-27,
jcl/DEFGDGB.jcl lines 48-59) and the NOSCRATCH TRANSACT.BKUP of catlg/LISTCAT.txt line 1637, as
Technical Specification Section 0.6.8 ("GDG archives") ports them, across the two crash windows:
after a sixth generation is PUBLISHED but before RollOff runs, and after RollOff commits
ROLLED_OFF but before the SCRATCH file is unlinked. A second ReconcileCatalog changes nothing.

The module is self-contained. It reads TEST_SYNC_DATABASE_URL from the process environment,
else from backend/.env, refuses a database whose name does not end in _test, and creates the
ledger and catalog tables in a uniquely named scratch schema of that database through
schema_translate_map, dropping the schema after each test. Generation files go under tmp_path.
"""

import hashlib
import logging
import secrets
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import NamedTuple, NoReturn

import pytest
from app.core.paths import ENV_FILE
from app.models import BATCH_SCHEMA, CYCLE_STEPS, CYCLES, GDG_GENERATIONS, JOB_RUNS, Base
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.engine import Row, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.schema import CreateSchema, DropSchema

from batch.orchestration import gdg
from batch.orchestration.clock import Clock
from batch.orchestration.job_context import JobContext
from batch.orchestration.run_ledger import (
    GDG_PUBLISHED,
    GDG_ROLLED_OFF,
    OPENED_BY_RUN_ALL,
    BuildRunParams,
    OpenCycle,
    RunStart,
    StartRun,
)

TEST_DATABASE_SUFFIX = "_test"
MISSING_URL_MESSAGE = (
    "TEST_SYNC_DATABASE_URL is not set in the environment or backend/.env; set it to a"
    " postgresql+psycopg2 URL of a database whose name ends in _test"
)
INVALID_URL_MESSAGE = "TEST_SYNC_DATABASE_URL is not a valid database URL"
NOT_TEST_DATABASE_MESSAGE = (
    "TEST_SYNC_DATABASE_URL names database {databaseName!r}, whose name does not end in _test;"
    " these tests create and drop schemas there"
)
SCRATCH_SCHEMA_PREFIX = "gdg_retention_"
SCRATCH_SCHEMA_SUFFIX_BYTES = 6
CATALOG_TABLES = [CYCLES, JOB_RUNS, CYCLE_STEPS, GDG_GENERATIONS]
GDG_SUBDIRECTORY = "gdg"
RECORD_FILLER = " "
CRASH_MESSAGE = "simulated crash of the batch process"
UNLINK_REFUSED_MESSAGE = "simulated unlink failure of a rolled-off generation file"
OLDEST_GENERATION = 1
SIXTH_GENERATION = gdg.GDG_LIMIT + 1
# A final file whose generation has no catalog row, which reconciliation must leave alone.
UNCATALOGED_FILE_NAME = gdg.GENERATION_FILE_TEMPLATE.format(generation=99)
NOTHING_RECONCILED = gdg.ReconcileSummary(published=0, lost=0, orphansDeleted=0)
GDG_BASE_LIST = list(gdg.GDG_BASES.values())
GDG_BASE_IDS = [base.name for base in GDG_BASE_LIST]


class ProducerStep(NamedTuple):
    """The chain step and legacy job name of the run that writes a base's generations."""

    stepNo: int
    jobName: str


PRODUCER_STEPS = {
    gdg.TRANSACT_BKUP.name: ProducerStep(6, "TRANBKP"),
    gdg.DALYREJS.name: ProducerStep(13, "POSTTRAN"),
    gdg.SYSTRAN.name: ProducerStep(14, "INTCALC"),
    gdg.TRANSACT_COMBINED.name: ProducerStep(16, "COMBTRAN"),
}


class ScratchDatabaseSettings(BaseSettings):
    """TEST_SYNC_DATABASE_URL as configured: the process environment, then backend/.env."""

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore", env_ignore_empty=True)

    TEST_SYNC_DATABASE_URL: str = Field(default="", repr=False)


class GdgDirectorySettings:
    """The one BatchSettings method gdg.py reads, GdgDir(), rooted under the test's tmp_path."""

    def __init__(self, gdgDir: Path) -> None:
        self.gdgDir = gdgDir

    def GdgDir(self) -> Path:
        """Return the generation-archive root of this test."""
        return self.gdgDir


class SimulatedCrash(Exception):
    """Stands for the batch process dying at the patched point of the publication protocol."""


def UrlDatabaseName(url: str) -> str:
    """Return the database name of url; fail the run, without quoting the URL, when unparsable."""
    try:
        databaseName = make_url(url).database
    except (ArgumentError, ValueError):
        databaseName = None
    if databaseName is None:
        pytest.fail(INVALID_URL_MESSAGE, pytrace=False)
    return databaseName


def ResolveTestDatabaseUrl() -> str:
    """Return TEST_SYNC_DATABASE_URL; fail the run unless it names a database ending in _test."""
    testUrl = ScratchDatabaseSettings().TEST_SYNC_DATABASE_URL
    if not testUrl:
        pytest.fail(MISSING_URL_MESSAGE, pytrace=False)
    databaseName = UrlDatabaseName(testUrl)
    if not databaseName.endswith(TEST_DATABASE_SUFFIX):
        pytest.fail(NOT_TEST_DATABASE_MESSAGE.format(databaseName=databaseName), pytrace=False)
    return testUrl


@pytest.fixture(name="databaseEngine", scope="module")
def DatabaseEngineFixture() -> Iterator[Engine]:
    """Yield an engine on the test database, disposed of when the module ends."""
    engine = create_engine(ResolveTestDatabaseUrl(), hide_parameters=True)
    yield engine
    engine.dispose()


@pytest.fixture(name="catalogEngine")
def CatalogEngineFixture(databaseEngine: Engine) -> Iterator[Engine]:
    """Yield an engine whose batch schema is a new scratch schema holding the catalog tables."""
    schemaName = SCRATCH_SCHEMA_PREFIX + secrets.token_hex(SCRATCH_SCHEMA_SUFFIX_BYTES)
    with databaseEngine.begin() as connection:
        connection.execute(CreateSchema(schemaName))
    try:
        translatedEngine = databaseEngine.execution_options(
            schema_translate_map={BATCH_SCHEMA: schemaName}
        )
        Base.metadata.create_all(translatedEngine, tables=CATALOG_TABLES)
        yield translatedEngine
    finally:
        with databaseEngine.begin() as connection:
            connection.execute(DropSchema(schemaName, cascade=True))


@pytest.fixture(name="jobContext")
def JobContextFixture(catalogEngine: Engine, tmp_path: Path) -> JobContext:
    """Return an unbound context over the scratch catalog, with GdgDir() under tmp_path."""
    settings = GdgDirectorySettings(tmp_path / GDG_SUBDIRECTORY)
    return JobContext(engine=catalogEngine, settings=settings, clock=Clock())


def StartProducerRun(context: JobContext, base: gdg.GdgBase) -> JobContext:
    """Open a run-all cycle, start the base's producing step in it, and bind the context."""
    producerStep = PRODUCER_STEPS[base.name]
    cycleId = OpenCycle(context, OPENED_BY_RUN_ALL)
    runStart = RunStart(
        cycleId=cycleId,
        stepNo=producerStep.stepNo,
        jobName=producerStep.jobName,
        params=BuildRunParams(context),
    )
    return context.ForRun(cycleId, producerStep.stepNo, StartRun(context, runStart))


def GenerationRecord(base: gdg.GdgBase, generation: int) -> str:
    """Return one LRECL record naming its generation, so every file has its own SHA-256."""
    return gdg.GenerationName(base.name, generation).ljust(base.lrecl, RECORD_FILLER)


def PublishGeneration(context: JobContext, base: gdg.GdgBase) -> int:
    """Publish one generation of base as a producing job does, retention included."""
    with gdg.NewGeneration(context, base) as writer:
        writer.WriteRecord(GenerationRecord(base, writer.generation))
    return writer.generation


def CrashBeforeRetention(context: JobContext, baseNames: Iterable[str]) -> NoReturn:
    """Stand in for RollOffBases in a process that dies once its generations are PUBLISHED."""
    raise SimulatedCrash(CRASH_MESSAGE)


def RefuseUnlink(context: JobContext, base: gdg.GdgBase, row: Row) -> NoReturn:
    """Stand in for ScratchFile when the unlink after the ROLLED_OFF commit fails or never runs."""
    raise PermissionError(UNLINK_REFUSED_MESSAGE)


def CrashBetweenPublicationAndRetention(context: JobContext, base: gdg.GdgBase) -> None:
    """Publish GDG_LIMIT generations, then a sixth whose process dies before RollOff runs."""
    for _ in range(gdg.GDG_LIMIT):
        PublishGeneration(context, base)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(gdg, "RollOffBases", CrashBeforeRetention)
        with pytest.raises(SimulatedCrash):
            PublishGeneration(context, base)


def CrashBetweenRollOffAndUnlink(context: JobContext, base: gdg.GdgBase) -> None:
    """Publish GDG_LIMIT generations, then a sixth whose RollOff fails after ROLLED_OFF commits."""
    for _ in range(gdg.GDG_LIMIT):
        PublishGeneration(context, base)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(gdg, "ScratchFile", RefuseUnlink)
        with pytest.raises(PermissionError):
            PublishGeneration(context, base)


def CatalogStates(context: JobContext, base: gdg.GdgBase) -> dict[int, str]:
    """Return the state of every cataloged generation of base, keyed by generation number."""
    statement = (
        select(GDG_GENERATIONS.c.generation, GDG_GENERATIONS.c.state)
        .where(GDG_GENERATIONS.c.base_name == base.name)
        .order_by(GDG_GENERATIONS.c.generation)
    )
    with context.engine.connect() as connection:
        return dict(connection.execute(statement).tuples().all())


def CatalogRows(context: JobContext) -> list[tuple[object, ...]]:
    """Return every column of every catalog row, in base and generation order."""
    statement = select(GDG_GENERATIONS).order_by(
        GDG_GENERATIONS.c.base_name, GDG_GENERATIONS.c.generation
    )
    with context.engine.connect() as connection:
        return [tuple(row) for row in connection.execute(statement)]


def ExpectedStates(rolledOff: Iterable[int], published: Iterable[int]) -> dict[int, str]:
    """Return the catalog states a test expects, keyed by generation number."""
    states = {generation: GDG_ROLLED_OFF for generation in rolledOff}
    states.update({generation: GDG_PUBLISHED for generation in published})
    return dict(sorted(states.items()))


def BaseFileNames(context: JobContext, base: gdg.GdgBase) -> set[str]:
    """Return the names of the files in the base's directory."""
    return {path.name for path in (context.settings.GdgDir() / base.name).iterdir()}


def GenerationFileNames(generations: Iterable[int]) -> set[str]:
    """Return the final file names of the given generations."""
    return {
        gdg.GENERATION_FILE_TEMPLATE.format(generation=generation) for generation in generations
    }


def FileDigests(context: JobContext) -> dict[str, str]:
    """Return the SHA-256 of every file under GdgDir(), keyed by its relative path."""
    root = context.settings.GdgDir()
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def RetainedFileNames(base: gdg.GdgBase) -> set[str]:
    """Return the files left once generation 1 rolled off; NOSCRATCH keeps generation 1's file."""
    firstKept = OLDEST_GENERATION + 1 if base.scratch else OLDEST_GENERATION
    return GenerationFileNames(range(firstKept, SIXTH_GENERATION + 1))


def LeftoverLogCount(records: list[logging.LogRecord]) -> int:
    """Return how many leftover-file deletions gdg logged."""
    return sum(1 for record in records if record.msg == gdg.LEFTOVER_DELETED_LOG)


@pytest.mark.parametrize("base", GDG_BASE_LIST, ids=GDG_BASE_IDS)
def test_reconcile_applies_retention_left_undone_after_publication(
    jobContext: JobContext, base: gdg.GdgBase
) -> None:
    """Six PUBLISHED after the crash; reconciling rolls off generation 1 and keeps (0)..(-4)."""
    context = StartProducerRun(jobContext, base)
    CrashBetweenPublicationAndRetention(context, base)
    allGenerations = range(OLDEST_GENERATION, SIXTH_GENERATION + 1)
    assert CatalogStates(context, base) == ExpectedStates([], allGenerations)
    assert gdg.ResolveGeneration(context, base, -gdg.GDG_LIMIT).generation == OLDEST_GENERATION

    assert gdg.ReconcileCatalog(context) == NOTHING_RECONCILED

    newerGenerations = range(OLDEST_GENERATION + 1, SIXTH_GENERATION + 1)
    assert CatalogStates(context, base) == ExpectedStates([OLDEST_GENERATION], newerGenerations)
    assert BaseFileNames(context, base) == RetainedFileNames(base)
    assert gdg.ResolveGeneration(context, base, 0).generation == SIXTH_GENERATION
    oldestRetained = gdg.ResolveGeneration(context, base, -(gdg.GDG_LIMIT - 1))
    assert oldestRetained.generation == OLDEST_GENERATION + 1
    with pytest.raises(gdg.MissingGenerationError):
        gdg.ResolveGeneration(context, base, -gdg.GDG_LIMIT)


@pytest.mark.parametrize("base", GDG_BASE_LIST, ids=GDG_BASE_IDS)
def test_reconcile_deletes_scratch_file_left_after_roll_off(
    jobContext: JobContext, base: gdg.GdgBase, caplog: pytest.LogCaptureFixture
) -> None:
    """The ROLLED_OFF file a failed unlink left goes for SCRATCH bases; BKUP and strays stay."""
    context = StartProducerRun(jobContext, base)
    CrashBetweenRollOffAndUnlink(context, base)
    newerGenerations = range(OLDEST_GENERATION + 1, SIXTH_GENERATION + 1)
    expectedStates = ExpectedStates([OLDEST_GENERATION], newerGenerations)
    assert CatalogStates(context, base) == expectedStates
    allFiles = GenerationFileNames(range(OLDEST_GENERATION, SIXTH_GENERATION + 1))
    assert BaseFileNames(context, base) == allFiles
    (context.settings.GdgDir() / base.name / UNCATALOGED_FILE_NAME).write_bytes(b"")

    with caplog.at_level(logging.WARNING, logger=gdg.LOGGER.name):
        assert gdg.ReconcileCatalog(context) == NOTHING_RECONCILED

    assert CatalogStates(context, base) == expectedStates
    assert BaseFileNames(context, base) == RetainedFileNames(base) | {UNCATALOGED_FILE_NAME}
    assert LeftoverLogCount(caplog.records) == (1 if base.scratch else 0)
    with pytest.raises(gdg.MissingGenerationError):
        gdg.ResolveGeneration(context, base, -gdg.GDG_LIMIT)


@pytest.mark.parametrize(
    "crashWindow",
    [CrashBetweenPublicationAndRetention, CrashBetweenRollOffAndUnlink],
    ids=["publication-retention", "rolled-off-unlink"],
)
@pytest.mark.parametrize("base", GDG_BASE_LIST, ids=GDG_BASE_IDS)
def test_second_reconcile_changes_nothing(
    jobContext: JobContext,
    base: gdg.GdgBase,
    crashWindow: Callable[[JobContext, gdg.GdgBase], None],
) -> None:
    """Once one ReconcileCatalog has recovered either crash, another changes no row or file."""
    context = StartProducerRun(jobContext, base)
    crashWindow(context, base)
    gdg.ReconcileCatalog(context)
    rowsAfterRecovery = CatalogRows(context)
    filesAfterRecovery = FileDigests(context)

    assert gdg.ReconcileCatalog(context) == NOTHING_RECONCILED

    assert CatalogRows(context) == rowsAfterRecovery
    assert FileDigests(context) == filesAfterRecovery
