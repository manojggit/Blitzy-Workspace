"""GDG archives: publication, catalog recovery, LOST blocking, abnormal disposition and retention.

Pins batch/orchestration/gdg.py to Technical Specification Section 0.6.8 ("GDG archives"): the
four chained bases TRANSACT.BKUP, DALYREJS, SYSTRAN and TRANSACT.COMBINED, written as
CARDDEMO_OUTPUT_DIR/gdg/<BASE>/G####V00.dat with the batch.gdg_generations catalog.

- Publication: .part, then the COMMITTED row with its SHA-256, then the rename to PUBLISHED; (0)
  and (-n) count PUBLISHED rows only, the DSN=...(0) references of jcl/COMBTRAN.jcl.
- Recovery: ReconcileCatalog, at the start of every command, completes an interrupted rename,
  marks a row whose file is gone LOST and deletes orphan .part files and work directories. Each
  deleted .part is named in one warning with its control characters escaped, and a refused
  directory fsync is logged by escaped path, error class and errno symbol only.
- Containment: a gdg or base directory that is a symbolic link is refused with
  ManagedDirectoryError, and exit 12 from a command, before anything is listed, created or
  deleted through it; a directory swapped for a link once opened is still the one acted on; a
  link named like a .part or a work directory inside a real base directory is never followed; a
  catalog row whose file_path is not the <BASE>/G####V00.dat derived for it, or whose base is
  not a GDG base, is refused with CatalogPathError and exit 12 before any file effect.
- LOST blocks the OPEN cycle: every command exits 12 naming the generation until run-all
  --new-cycle abandons the cycle and its LOST rows.
- Abnormal disposition: DISP=(NEW,CATLG,DELETE) of DALYREJS(+1) and SYSTRAN(+1)
  (jcl/POSTTRAN.jcl lines 34-38, jcl/INTCALC.jcl lines 37-41) discards the .part of a failed
  producer and catalogs nothing.
- Retention: LIMIT(5) SCRATCH (jcl/DEFGDGB.jcl lines 24-27, jcl/DALYREJS.jcl) deletes the file of
  the generation rolled off, except TRANSACT.BKUP, NOSCRATCH per catlg/LISTCAT.txt line 1637,
  which keeps it while its row becomes ROLLED_OFF and unresolvable.

The crash windows of retention itself are pinned by test_gdg_retention_recovery.py.
"""

import errno
import hashlib
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from app.models import CYCLES, GDG_GENERATIONS, JOB_RUNS
from sqlalchemy import Engine, func, insert, select, update
from sqlalchemy.engine import RowMapping

from batch.jobs import posttran
from batch.orchestration import batch_chain, gdg, run_ledger
from batch.orchestration.job_context import (
    EXIT_ABEND,
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_REJECTS,
    JobContext,
    JobFailure,
)
from batch.tests.conftest import (
    EXPECTED_DIR,
    FIXED_AS_OF_TEXT,
    CountJobRuns,
    InjectStepFailure,
    InvokeCli,
    StepRow,
)
from batch.tests.reconciliation.compare import (
    GDG_DIRECTORY_NAME,
    CompareFixedWidthFiles,
    LatestPublishedGeneration,
    ResolveCatalogFilePath,
)

# The final file name of a generation, G followed by four digits and V00.
GENERATION_FILE_PATTERN = re.compile(r"G\d{4}V00\.dat")
GENERATION_FILE_TEMPLATE = "G{generation:04d}V00.dat"
PART_SUFFIX = ".part"
# One byte per character and "\n" after every record, the comparison basis of Section 0.6.11.
GDG_FILE_ENCODING = "latin-1"
RECORD_TERMINATOR = b"\n"
TEST_JOB_NAME = "test-gdg"
FIRST_RECORD_TEXT = "GDG TEST RECORD 1"
SECOND_RECORD_TEXT = "GDG TEST RECORD 2"
RETENTION_RECORD_TEXT = "GDG RETENTION RECORD"
CORRUPT_FILE_BYTES = b"CORRUPTED GENERATION FILE\n"
FORGED_SHA256 = "0" * 64
INJECTED_PRODUCER_MESSAGE = "injected failure of a generation producer"
INJECTED_ABEND_MESSAGE = "INJECTED POSTTRAN ABEND WITH DALYREJS(+1) OPEN"
ORPHAN_GENERATION = 9
ORPHAN_PART_NAME = GENERATION_FILE_TEMPLATE.format(generation=ORPHAN_GENERATION) + PART_SUFFIX
# An orphan .part with no catalog row and the name its deletion warning shows: a generation's
# name as it is, and a name holding a line feed before forged ERROR text, a carriage return and
# an ANSI escape with each control character escaped, so it neither splits the line nor forges
# another.
ORPHAN_PART_CASES = (
    pytest.param(ORPHAN_PART_NAME, ORPHAN_PART_NAME, id="generation-name"),
    pytest.param(
        "G0009\nERROR forged-recovery-success\r\x1b[31m.dat.part",
        "G0009\\nERROR forged-recovery-success\\r\\x1b[31m.dat.part",
        id="control-bearing-name",
    ),
)
# A base directory whose name holds a line feed and an ANSI escape, as a refused directory fsync
# names it, and the OS text that refusal must not repeat.
FSYNC_REFUSED_DIRECTORY_NAME = "SYSTRAN\nERROR forged-fsync\x1b[31m"
SHOWN_FSYNC_REFUSED_DIRECTORY_NAME = "SYSTRAN\\nERROR forged-fsync\\x1b[31m"
FSYNC_REFUSAL_OS_TEXT = "injected directory fsync refusal"
# The work directory, with one run file, that a killed COMBTRAN's sort left in its base directory.
ORPHAN_WORK_DIRECTORY_NAME = "carddemo-combtran-killed" + gdg.WORK_DIRECTORY_SUFFIX
ORPHAN_RUN_FILE_NAME = "killed.run"
# LIMIT(5) of jcl/DEFGDGB.jcl and catlg/LISTCAT.txt line 1637, stated here rather than read from
# batch/orchestration/gdg.py, so a production limit other than 5 fails the retention tests.
LEGACY_GDG_LIMIT = 5
OLDEST_GENERATION = 1
SIXTH_GENERATION = LEGACY_GDG_LIMIT + 1
OLDEST_RETAINED_RELATIVE = -(LEGACY_GDG_LIMIT - 1)
ROLLED_OFF_RELATIVE = -LEGACY_GDG_LIMIT
SCRATCH_BASES = [gdg.DALYREJS, gdg.SYSTRAN, gdg.TRANSACT_COMBINED]
SCRATCH_BASE_IDS = [base.name for base in SCRATCH_BASES]
CATALOG_OUTPUT_DIRECTORY_NAME = "catalog-output"
LINKED_DIRECTORY_NAME = "LINKED.BASE"
SAMPLE_FILE_NAME = GENERATION_FILE_TEMPLATE.format(generation=OLDEST_GENERATION)
SAMPLE_FILE_PATH = f"{gdg.SYSTRAN.name}/{SAMPLE_FILE_NAME}"
LINKED_FILE_NAME = GENERATION_FILE_TEMPLATE.format(generation=SIXTH_GENERATION)
# Catalog file_path values resolving outside outputDir/gdg, or to it. "{outsideDir}" stands for
# the directory beside the output root that BuildCatalogRoots links LINKED.BASE and
# SYSTRAN/LINKED_FILE_NAME to.
ESCAPING_CATALOG_PATHS = {
    "absolute-outside-root": "{outsideDir}/" + SAMPLE_FILE_PATH,
    "dot-dot-escape": "../" + SAMPLE_FILE_PATH,
    "symlinked-parent-directory": f"{LINKED_DIRECTORY_NAME}/{SAMPLE_FILE_NAME}",
    "symlinked-file": f"{gdg.SYSTRAN.name}/{LINKED_FILE_NAME}",
    "gdg-root-itself": ".",
}
# Catalog file_path values inside the GDG root; "{gdgRoot}" stands for outputDir/gdg.
INSIDE_CATALOG_PATHS = {
    "relative-base-path": SAMPLE_FILE_PATH,
    "absolute-path-inside-root": "{gdgRoot}/" + SAMPLE_FILE_PATH,
}

POSTTRAN_STEP = 13
INTCALC_STEP = 14
COMBTRAN_STEP = 16
COMBTRAN_JCL_NAME = "COMBTRAN"
# The steps the seeded cycle still has to run before COMBTRAN: TRANBKP twice, POSTTRAN, INTCALC.
STEPS_BEFORE_COMBTRAN = (6, POSTTRAN_STEP, INTCALC_STEP, 15)
RUN_ALL_ARGUMENTS = ("run-all", "--as-of", FIXED_AS_OF_TEXT)
NEW_CYCLE_ARGUMENTS = ("run-all", "--new-cycle", "--as-of", FIXED_AS_OF_TEXT)
NEW_CYCLE_AT_COMBTRAN_ARGUMENTS = (
    "run-all",
    "--new-cycle",
    "--start-at",
    COMBTRAN_JCL_NAME,
    "--as-of",
    FIXED_AS_OF_TEXT,
)
COMB_TRAN_ARGUMENTS = ("job", "comb-tran")
POST_TRAN_ARGUMENTS = ("job", "post-tran", "--as-of", FIXED_AS_OF_TEXT)
LOAD_TRAN_TYPES_ARGUMENTS = ("load", "tran-types")
COMBINED_FIXTURE = EXPECTED_DIR / "combtran_combined.dat"
TRANSACTION_LAYOUT_NAME = "TRANSACTION"
PROCESS_STAGED_RECORD_NAME = "ProcessStagedRecord"
# Containment: a directory outside the archive holding what recovery deletes in a base directory,
# an orphan .part and a work directory with one file, which no archive operation may change.
OUTSIDE_DIRECTORY_NAME = "outside-sentinel"
ORPHAN_RUN_FILE_PATH = f"{ORPHAN_WORK_DIRECTORY_NAME}/{ORPHAN_RUN_FILE_NAME}"
OUTSIDE_FILES = {ORPHAN_PART_NAME: CORRUPT_FILE_BYTES, ORPHAN_RUN_FILE_PATH: CORRUPT_FILE_BYTES}
# O_NOFOLLOW with O_DIRECTORY refuses a symbolic link with ENOTDIR on Linux and ELOOP elsewhere.
REFUSED_LINK_ERRNO_NAMES = {"ENOTDIR", "ELOOP"}
REFUSAL_LINE_PATTERN = re.compile(r"FILE ERROR ManagedDirectoryError ERRNO (?P<errnoName>\w+) ON ")
# The name the real base directory takes once a link has replaced it, mid-operation, and the
# bytes of the .part file its publication renames.
SWAPPED_OUT_SUFFIX = ".opened"
PUBLISHED_BYTES = b"GENERATION PLACED BY PUBLICATION\n"
# A forged catalog row, numbered above every generation a test publishes; the bytes of the file
# its file_path names, whose SHA-256 the row carries; a base name GDG_BASES does not hold; and
# the FILE ERROR line the command's refusal of the row ends with.
FORGED_GENERATION = 900
FORGED_FILE_BYTES = b"FILE A FORGED CATALOG ROW NAMES\n"
FORGED_FILE_SHA256 = hashlib.sha256(FORGED_FILE_BYTES).hexdigest()
UNKNOWN_BASE_NAME = "FORGED.BASE"
CATALOG_PATH_REFUSAL_PREFIX = "FILE ERROR CatalogPathError ERRNO EINVAL ON "


class InjectedProducerError(Exception):
    """Stands for any failure of a job while it writes a new generation."""


def LedgeredContext(jobContext: JobContext) -> JobContext:
    """Start a RUNNING test run in the OPEN cycle and return the context bound to it.

    Catalog rows reference batch.job_runs, and a LOST generation blocks the cycle of its run.
    """
    with jobContext.engine.begin() as connection:
        cycleId = run_ledger.FindOpenCycle(connection)
        runStatement = (
            insert(JOB_RUNS)
            .values(
                cycle_id=cycleId,
                job_name=TEST_JOB_NAME,
                params={},
                status=run_ledger.RUN_RUNNING,
                counts={},
                started_at=func.now(),
            )
            .returning(JOB_RUNS.c.run_id)
        )
        runId = connection.execute(runStatement).scalar_one()
    return jobContext.ForRun(cycleId, None, runId)


def CatalogRows(engine: Engine, baseName: str) -> list[dict]:
    """Return every catalog row of a base as a column-name dict, in generation order."""
    statement = (
        select(GDG_GENERATIONS)
        .where(GDG_GENERATIONS.c.base_name == baseName)
        .order_by(GDG_GENERATIONS.c.generation)
    )
    with engine.connect() as connection:
        return [dict(rowMapping) for rowMapping in connection.execute(statement).mappings()]


def CatalogStates(engine: Engine, baseName: str) -> dict[int, str]:
    """Return the state of every cataloged generation of a base, keyed by generation number."""
    return {row["generation"]: row["state"] for row in CatalogRows(engine, baseName)}


def CycleStatuses(engine: Engine) -> list[str]:
    """Return the status of every batch cycle, oldest first."""
    statement = select(CYCLES.c.status).order_by(CYCLES.c.cycle_id)
    with engine.connect() as connection:
        return list(connection.execute(statement).scalars())


def Record(base: gdg.GdgBase, recordText: str) -> str:
    """Return recordText padded with spaces to the base's LRECL."""
    return recordText.ljust(base.lrecl)


def PublishGenerations(
    context: JobContext, base: gdg.GdgBase, count: int, recordText: str
) -> list[int]:
    """Publish count one-record generations of base, as a self-committing producer does."""
    generations = []
    for _ in range(count):
        with gdg.NewGeneration(context, base) as writer:
            writer.WriteRecord(Record(base, recordText))
        generations.append(writer.generation)
    return generations


def FileName(generation: int) -> str:
    """Return the final file name of a generation, for example G0001V00.dat."""
    return GENERATION_FILE_TEMPLATE.format(generation=generation)


def GenerationPath(context: JobContext, base: gdg.GdgBase, generation: int) -> Path:
    """Return the absolute path of a generation's final file under GdgDir()/<BASE>."""
    return context.settings.GdgDir() / base.name / FileName(generation)


def PartPath(path: Path) -> Path:
    """Return the .part sibling a generation file is written as before its publication."""
    return path.with_name(path.name + PART_SUFFIX)


def BaseFileNames(context: JobContext, base: gdg.GdgBase) -> set[str]:
    """Return the names of the files in the base's directory; none when it does not exist."""
    directory = context.settings.GdgDir() / base.name
    if not directory.is_dir():
        return set()
    return {path.name for path in directory.iterdir()}


def SetGenerationState(engine: Engine, baseName: str, generation: int, state: str) -> None:
    """Move one catalog row to state, as a crash at another point of the protocol leaves it."""
    statement = (
        update(GDG_GENERATIONS)
        .where(
            GDG_GENERATIONS.c.base_name == baseName,
            GDG_GENERATIONS.c.generation == generation,
        )
        .values(state=state)
    )
    with engine.begin() as connection:
        connection.execute(statement)


def InsertCommittedRow(context: JobContext, base: gdg.GdgBase, generation: int) -> None:
    """Catalog a COMMITTED generation of the context's run that was never published."""
    values = {
        "base_name": base.name,
        "generation": generation,
        "file_path": f"{base.name}/{FileName(generation)}",
        "record_count": 0,
        "sha256": FORGED_SHA256,
        "state": run_ledger.GDG_COMMITTED,
        "run_id": context.runId,
    }
    with context.engine.begin() as connection:
        connection.execute(insert(GDG_GENERATIONS).values(**values))


def LoseLatestGeneration(context: JobContext, row: dict) -> None:
    """Leave a generation COMMITTED with neither its final nor its .part file, a lost archive.

    The row's file_path is confined to this test's GDG root first, so a path resolving outside
    it raises ValueError before the catalog or any file changes.
    """
    finalPath = ResolveCatalogFilePath(context.settings.OutputDir(), row["file_path"])
    baseName, generation = row["base_name"], row["generation"]
    SetGenerationState(context.engine, baseName, generation, run_ledger.GDG_COMMITTED)
    finalPath.unlink(missing_ok=True)
    PartPath(finalPath).unlink(missing_ok=True)


def BuildCatalogRoots(tempRoot: Path) -> tuple[Path, Path]:
    """Create an output root whose GDG root holds symlinks to a directory beside the root.

    Returns (outputDir, outsideDir), both under tempRoot: outputDir/gdg/LINKED.BASE links to
    outsideDir, and outputDir/gdg/SYSTRAN/LINKED_FILE_NAME links to a file in outsideDir.
    """
    outputDir = tempRoot / CATALOG_OUTPUT_DIRECTORY_NAME
    outsideDir = tempRoot / OUTSIDE_DIRECTORY_NAME
    gdgRoot = outputDir / GDG_DIRECTORY_NAME
    (gdgRoot / gdg.SYSTRAN.name).mkdir(parents=True)
    outsideDir.mkdir()
    outsideFile = outsideDir / LINKED_FILE_NAME
    outsideFile.write_bytes(CORRUPT_FILE_BYTES)
    (gdgRoot / LINKED_DIRECTORY_NAME).symlink_to(outsideDir, target_is_directory=True)
    (gdgRoot / gdg.SYSTRAN.name / LINKED_FILE_NAME).symlink_to(outsideFile)
    return outputDir, outsideDir


def LostGenerationText(engine: Engine, baseName: str, generation: int) -> str:
    """Return the refusal that names a LOST generation of the OPEN cycle."""
    with engine.connect() as connection:
        cycleId = run_ledger.FindOpenCycle(connection)
    return gdg.LOST_GENERATION_TEMPLATE.format(
        base=baseName, generation=generation, cycleId=cycleId
    )


def StopChainAfterStep15(engine: Engine) -> dict:
    """Run the seeded cycle with COMBTRAN failing; return the SYSTRAN(0) row INTCALC published.

    The failure is patched in a MonkeyPatch context of its own, because undoing the test's
    monkeypatch would also restore the CARDDEMO_OUTPUT_DIR the outputDir fixture set.
    """
    with pytest.MonkeyPatch.context() as patch:
        InjectStepFailure(patch, COMBTRAN_STEP, EXIT_FAILURE)
        outcome = InvokeCli(RUN_ALL_ARGUMENTS)
    assert outcome.exitCode == EXIT_FAILURE, outcome.output
    completedStates = [StepRow(engine, stepNo)["state"] for stepNo in STEPS_BEFORE_COMBTRAN]
    assert completedStates == [run_ledger.STEP_COMPLETED] * len(STEPS_BEFORE_COMBTRAN)
    assert StepRow(engine, COMBTRAN_STEP)["state"] == run_ledger.STEP_FAILED
    interestStep = StepRow(engine, INTCALC_STEP)
    [systranRow] = CatalogRows(engine, gdg.SYSTRAN.name)
    assert interestStep["cycle_status"] == run_ledger.CYCLE_OPEN
    assert systranRow["run_id"] == interestStep["last_run_id"]
    assert systranRow["state"] == run_ledger.GDG_PUBLISHED
    return systranRow


def AssertBlockedByLostGeneration(arguments: Sequence[str], lostText: str) -> None:
    """Run a command and check it exits 12 with the refusal naming the LOST generation."""
    outcome = InvokeCli(arguments)
    assert outcome.exitCode == EXIT_FAILURE, outcome.output
    assert lostText in outcome.output


def AssertNewCycleStartAtRefused(engine: Engine, lostGeneration: int, runCountBefore: int) -> None:
    """Run run-all --new-cycle --start-at COMBTRAN and check it exits 12, changing nothing.

    A new cycle starts from the loaders, so it may not start at COMBTRAN and resolve the blocked
    cycle's archives. The refusal precedes the abandonment: SYSTRAN(lostGeneration) stays LOST,
    the cycle stays OPEN, and the run count is still runCountBefore.
    """
    outcome = InvokeCli(NEW_CYCLE_AT_COMBTRAN_ARGUMENTS)
    refusal = batch_chain.NEW_CYCLE_START_REFUSED_TEMPLATE.format(
        stepNo=COMBTRAN_STEP, jobName=COMBTRAN_JCL_NAME
    )
    assert outcome.exitCode == EXIT_FAILURE, outcome.output
    assert refusal in outcome.output
    assert CatalogStates(engine, gdg.SYSTRAN.name) == {lostGeneration: run_ledger.GDG_LOST}
    assert CycleStatuses(engine) == [run_ledger.CYCLE_OPEN]
    assert CountJobRuns(engine) == runCountBefore


def AssertNewCycleRebuildsCombined(engine: Engine, outputDir: Path) -> None:
    """Run run-all --new-cycle and check its TRANSACT.COMBINED(0) equals the oracle's records."""
    outcome = InvokeCli(NEW_CYCLE_ARGUMENTS)
    assert outcome.exitCode == EXIT_REJECTS, outcome.output
    with engine.connect() as connection:
        combinedPath = LatestPublishedGeneration(connection, gdg.TRANSACT_COMBINED.name, outputDir)
    differences = CompareFixedWidthFiles(combinedPath, COMBINED_FIXTURE, TRANSACTION_LAYOUT_NAME)
    assert differences == []


def InjectAbendOnceRejectsWritten(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Make POSTTRAN abend at the first record after DALYREJS(+1) received a reject.

    The abend is raised inside the step, not by conftest.InjectStepFailure, which fails before
    the runner opens the generation and so would leave no .part to discard. Returns a list that
    records, at the abend, whether the .part file existed, so the test sees the failure strike
    while the generation was being written.
    """
    originalProcess = posttran.ProcessStagedRecord
    partStates: list[bool] = []

    def AbendingProcess(
        context: JobContext,
        stagedRow: RowMapping,
        rejectWriter: gdg.GenerationWriter,
        counts: posttran.PostingCounts,
    ) -> None:
        if rejectWriter.recordCount > 0:
            partStates.append(rejectWriter.partPath.is_file())
            raise JobFailure(EXIT_ABEND, INJECTED_ABEND_MESSAGE)
        originalProcess(context, stagedRow, rejectWriter, counts)

    monkeypatch.setattr(posttran, PROCESS_STAGED_RECORD_NAME, AbendingProcess)
    return partStates


def AssertOldestRolledOff(context: JobContext, base: gdg.GdgBase) -> None:
    """Check six publications left generation 1 ROLLED_OFF and generations 2-6 resolvable."""
    newerGenerations = range(OLDEST_GENERATION + 1, SIXTH_GENERATION + 1)
    expectedStates = {OLDEST_GENERATION: run_ledger.GDG_ROLLED_OFF}
    expectedStates.update({generation: run_ledger.GDG_PUBLISHED for generation in newerGenerations})
    assert CatalogStates(context.engine, base.name) == expectedStates
    assert all(
        GenerationPath(context, base, generation).is_file() for generation in newerGenerations
    )
    assert gdg.ResolveGeneration(context, base, 0).generation == SIXTH_GENERATION
    oldestRetained = gdg.ResolveGeneration(context, base, OLDEST_RETAINED_RELATIVE)
    assert oldestRetained.generation == OLDEST_GENERATION + 1
    with pytest.raises(gdg.MissingGenerationError):
        gdg.ResolveGeneration(context, base, ROLLED_OFF_RELATIVE)


def test_new_generation_publishes_file_with_sha256_catalog_row(jobContext: JobContext) -> None:
    """Two SYSTRAN records give G0001V00.dat, no .part, and a PUBLISHED row with its SHA-256."""
    context = LedgeredContext(jobContext)
    records = [Record(gdg.SYSTRAN, FIRST_RECORD_TEXT), Record(gdg.SYSTRAN, SECOND_RECORD_TEXT)]
    with gdg.NewGeneration(context, gdg.SYSTRAN) as writer:
        for record in records:
            writer.WriteRecord(record)

    finalPath = GenerationPath(context, gdg.SYSTRAN, OLDEST_GENERATION)
    fileBytes = finalPath.read_bytes()
    [row] = CatalogRows(context.engine, gdg.SYSTRAN.name)
    assert GENERATION_FILE_PATTERN.fullmatch(finalPath.name)
    assert BaseFileNames(context, gdg.SYSTRAN) == {finalPath.name}
    expectedBytes = b"".join(
        record.encode(GDG_FILE_ENCODING) + RECORD_TERMINATOR for record in records
    )
    assert fileBytes == expectedBytes
    assert row["state"] == run_ledger.GDG_PUBLISHED
    assert (row["record_count"], row["run_id"]) == (len(records), context.runId)
    assert row["sha256"] == hashlib.sha256(fileBytes).hexdigest()
    assert row["file_path"] == f"{gdg.SYSTRAN.name}/{finalPath.name}"
    assert list(gdg.ReadGeneration(gdg.ResolveGeneration(context, gdg.SYSTRAN))) == records


def test_resolve_generation_counts_published_rows_only(jobContext: JobContext) -> None:
    """A newer COMMITTED row is skipped: (0) and (-1) are the two PUBLISHED generations."""
    context = LedgeredContext(jobContext)
    firstGeneration, secondGeneration = PublishGenerations(
        context, gdg.SYSTRAN, 2, FIRST_RECORD_TEXT
    )
    InsertCommittedRow(context, gdg.SYSTRAN, secondGeneration + 1)

    assert gdg.ResolveGeneration(context, gdg.SYSTRAN, 0).generation == secondGeneration
    assert gdg.ResolveGeneration(context, gdg.SYSTRAN, -1).generation == firstGeneration
    with pytest.raises(gdg.MissingGenerationError):
        gdg.ResolveGeneration(context, gdg.SYSTRAN, -2)


def test_reconcile_completes_interrupted_publication_between_commit_and_rename(
    jobContext: JobContext,
) -> None:
    """A COMMITTED row whose file is still .part is renamed and PUBLISHED by the next command."""
    context = LedgeredContext(jobContext)
    [generation] = PublishGenerations(context, gdg.SYSTRAN, 1, FIRST_RECORD_TEXT)
    finalPath = GenerationPath(context, gdg.SYSTRAN, generation)
    publishedBytes = finalPath.read_bytes()
    finalPath.rename(PartPath(finalPath))
    SetGenerationState(context.engine, gdg.SYSTRAN.name, generation, run_ledger.GDG_COMMITTED)

    outcome = InvokeCli(LOAD_TRAN_TYPES_ARGUMENTS)

    assert outcome.exitCode == EXIT_OK, outcome.output
    expectedStates = {generation: run_ledger.GDG_PUBLISHED}
    assert CatalogStates(context.engine, gdg.SYSTRAN.name) == expectedStates
    assert finalPath.read_bytes() == publishedBytes
    assert BaseFileNames(context, gdg.SYSTRAN) == {finalPath.name}


def test_reconcile_publishes_committed_row_whose_final_file_exists(
    jobContext: JobContext,
) -> None:
    """A crash after the rename but before the row update leaves a file the reconcile accepts."""
    context = LedgeredContext(jobContext)
    [generation] = PublishGenerations(context, gdg.SYSTRAN, 1, FIRST_RECORD_TEXT)
    SetGenerationState(context.engine, gdg.SYSTRAN.name, generation, run_ledger.GDG_COMMITTED)

    summary = gdg.ReconcileCatalog(context)

    assert summary == gdg.ReconcileSummary(published=1, lost=0, orphansDeleted=0)
    expectedStates = {generation: run_ledger.GDG_PUBLISHED}
    assert CatalogStates(context.engine, gdg.SYSTRAN.name) == expectedStates
    assert BaseFileNames(context, gdg.SYSTRAN) == {FileName(generation)}


@pytest.mark.parametrize("heldSuffix", ["", PART_SUFFIX], ids=["final-file", "part-file"])
def test_reconcile_marks_committed_row_lost_when_no_file_has_its_hash(
    jobContext: JobContext, heldSuffix: str
) -> None:
    """A COMMITTED row whose final or .part file holds other bytes becomes LOST, never PUBLISHED."""
    context = LedgeredContext(jobContext)
    [generation] = PublishGenerations(context, gdg.SYSTRAN, 1, FIRST_RECORD_TEXT)
    finalPath = GenerationPath(context, gdg.SYSTRAN, generation)
    finalPath.unlink()
    finalPath.with_name(finalPath.name + heldSuffix).write_bytes(CORRUPT_FILE_BYTES)
    SetGenerationState(context.engine, gdg.SYSTRAN.name, generation, run_ledger.GDG_COMMITTED)

    summary = gdg.ReconcileCatalog(context)

    assert (summary.published, summary.lost) == (0, 1)
    expectedStates = {generation: run_ledger.GDG_LOST}
    assert CatalogStates(context.engine, gdg.SYSTRAN.name) == expectedStates
    assert not PartPath(finalPath).exists()


@pytest.mark.parametrize(("orphanName", "shownName"), ORPHAN_PART_CASES)
def test_reconcile_deletes_orphan_part_file_without_catalog_row(
    jobContext: JobContext, caplog: pytest.LogCaptureFixture, orphanName: str, shownName: str
) -> None:
    """A .part with no catalog row, a crash before commit, is deleted by the reconcile.

    Its one warning names it with every control character escaped.
    """
    orphanPath = jobContext.settings.GdgDir() / gdg.SYSTRAN.name / orphanName
    orphanPath.parent.mkdir(parents=True)
    orphanPath.write_bytes(CORRUPT_FILE_BYTES)

    with caplog.at_level(logging.WARNING, logger=gdg.LOGGER.name):
        summary = gdg.ReconcileCatalog(jobContext)

    assert summary == gdg.ReconcileSummary(published=0, lost=0, orphansDeleted=1)
    assert not orphanPath.exists()
    assert CatalogRows(jobContext.engine, gdg.SYSTRAN.name) == []
    gdgWarnings = [
        record.getMessage() for record in caplog.records if record.name == gdg.LOGGER.name
    ]
    assert gdgWarnings == [gdg.ORPHAN_DELETED_LOG % f"{orphanPath.parent}/{shownName}"]


def test_directory_fsync_refusal_is_logged_by_escaped_path_class_and_errno_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A platform refusing a directory fsync is logged at DEBUG by the directory, its controls
    escaped, and the error's class and errno symbol, never by the OS error text."""
    directory = tmp_path / FSYNC_REFUSED_DIRECTORY_NAME
    directory.mkdir()

    def RefuseFsync(descriptor: int) -> None:
        raise OSError(errno.EINVAL, FSYNC_REFUSAL_OS_TEXT)

    with gdg.ArchiveDirectory(directory, create=False) as heldDirectory:
        monkeypatch.setattr(gdg.os, "fsync", RefuseFsync)
        with caplog.at_level(logging.DEBUG, logger=gdg.LOGGER.name):
            gdg.FsyncDirectory(heldDirectory)
    shownDirectory = f"{tmp_path}/{SHOWN_FSYNC_REFUSED_DIRECTORY_NAME}"
    expectedMessage = f"DIRECTORY FSYNC REFUSED FOR {shownDirectory}: OSError ERRNO EINVAL"
    assert caplog.messages == [expectedMessage]


def test_reconcile_deletes_orphan_work_directory_and_keeps_generation_files(
    jobContext: JobContext,
) -> None:
    """A killed producer's *.work directory is deleted with its runs; generations are untouched.

    The PUBLISHED generation beside it keeps its file and row, and a second reconcile finds
    nothing to delete.
    """
    context = LedgeredContext(jobContext)
    base = gdg.TRANSACT_COMBINED
    [generation] = PublishGenerations(context, base, 1, FIRST_RECORD_TEXT)
    workDirectory = context.settings.GdgDir() / base.name / ORPHAN_WORK_DIRECTORY_NAME
    workDirectory.mkdir()
    (workDirectory / ORPHAN_RUN_FILE_NAME).write_bytes(CORRUPT_FILE_BYTES)

    summary = gdg.ReconcileCatalog(context)

    expectedSummary = gdg.ReconcileSummary(
        published=0, lost=0, orphansDeleted=0, workDirectoriesDeleted=1
    )
    assert summary == expectedSummary
    assert BaseFileNames(context, base) == {FileName(generation)}
    assert CatalogStates(context.engine, base.name) == {generation: run_ledger.GDG_PUBLISHED}
    nothingDeleted = gdg.ReconcileSummary(published=0, lost=0, orphansDeleted=0)
    assert gdg.ReconcileCatalog(context) == nothingDeleted


def test_abnormal_disposition_discards_part_and_catalogs_nothing(jobContext: JobContext) -> None:
    """A producer failing mid-write leaves no row and no file: DISP=(NEW,CATLG,DELETE)."""
    context = LedgeredContext(jobContext)
    with pytest.raises(InjectedProducerError), gdg.NewGeneration(context, gdg.SYSTRAN) as writer:
        writer.WriteRecord(Record(gdg.SYSTRAN, FIRST_RECORD_TEXT))
        raise InjectedProducerError(INJECTED_PRODUCER_MESSAGE)

    assert CatalogRows(context.engine, gdg.SYSTRAN.name) == []
    assert BaseFileNames(context, gdg.SYSTRAN) == set()


def test_caller_transaction_keeps_generation_committed_until_published(
    jobContext: JobContext,
) -> None:
    """With the caller's connection the row commits with it and the rename waits for publication."""
    context = LedgeredContext(jobContext)
    base = gdg.TRANSACT_COMBINED
    with context.engine.connect() as callerConnection:
        with gdg.NewGeneration(context, base, connection=callerConnection) as writer:
            writer.WriteRecord(Record(base, FIRST_RECORD_TEXT))
        assert CatalogRows(context.engine, base.name) == []
        callerConnection.commit()
    partName = PartPath(Path(FileName(writer.generation))).name
    assert CatalogStates(context.engine, base.name) == {writer.generation: run_ledger.GDG_COMMITTED}
    assert BaseFileNames(context, base) == {partName}

    assert gdg.PublishRunGenerations(context) == 1

    assert CatalogStates(context.engine, base.name) == {writer.generation: run_ledger.GDG_PUBLISHED}
    assert BaseFileNames(context, base) == {FileName(writer.generation)}


def test_rolled_back_caller_transaction_leaves_orphan_part_for_reconcile(
    jobContext: JobContext,
) -> None:
    """A rolled-back atomic step catalogs nothing; the reconcile deletes its orphan .part."""
    context = LedgeredContext(jobContext)
    base = gdg.TRANSACT_COMBINED
    with context.engine.connect() as callerConnection:
        with gdg.NewGeneration(context, base, connection=callerConnection) as writer:
            writer.WriteRecord(Record(base, FIRST_RECORD_TEXT))
        callerConnection.rollback()
    assert CatalogRows(context.engine, base.name) == []
    assert BaseFileNames(context, base) == {PartPath(Path(FileName(writer.generation))).name}

    summary = gdg.ReconcileCatalog(context)

    assert summary == gdg.ReconcileSummary(published=0, lost=0, orphansDeleted=1)
    assert BaseFileNames(context, base) == set()


@dataclass(frozen=True)
class LinkedDirectoryCase:
    """A symbolic link pointing outside the archive, at the gdg directory when linksGdg, else at
    base's directory."""

    linksGdg: bool
    base: gdg.GdgBase


# Both levels, for a base the reconcile meets after others (SYSTRAN) and the one it meets last.
LINKED_DIRECTORY_CASES = [
    pytest.param(
        LinkedDirectoryCase(linksGdg, base), id=f"{'gdg' if linksGdg else 'base'}-link-{base.name}"
    )
    for linksGdg in (False, True)
    for base in (gdg.SYSTRAN, gdg.TRANSACT_COMBINED)
]


@dataclass(frozen=True)
class SwapCase:
    """An operation on SYSTRAN's directory during which a link to a directory outside replaces
    it, once the operation has opened it.

    held is what the real directory and the outside one each hold before; operation runs given
    the context and the directory's path; expectedResult is what it returns; heldAfter is what
    the real directory, renamed aside, holds afterwards.
    """

    held: dict[str, bytes]
    operation: Callable[[JobContext, Path], object]
    expectedResult: object
    heldAfter: dict[str, bytes | None]


def OutsideDirectory(context: JobContext) -> Path:
    """Return the path of a directory beside the test's output root, outside the archive."""
    return context.settings.OutputDir().parent / OUTSIDE_DIRECTORY_NAME


def PlantFiles(directory: Path, files: dict[str, bytes]) -> None:
    """Create directory and each file of files, by its path relative to it, with its bytes."""
    directory.mkdir(parents=True, exist_ok=True)
    for relativeName, fileBytes in files.items():
        filePath = directory / relativeName
        filePath.parent.mkdir(parents=True, exist_ok=True)
        filePath.write_bytes(fileBytes)


def TreeSnapshot(directory: Path) -> dict[str, bytes | None]:
    """Return every path under directory, relative to it, with its bytes; None for a directory."""
    return {
        path.relative_to(directory).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in sorted(directory.rglob("*"))
    }


def LinkManagedDirectory(context: JobContext, case: LinkedDirectoryCase, outside: Path) -> Path:
    """Make the case's managed directory a symbolic link to outside; return the link's path.

    outside receives OUTSIDE_FILES where the linked base directory's files would be.
    """
    gdgDirectory = context.settings.GdgDir()
    if case.linksGdg:
        PlantFiles(outside / case.base.name, OUTSIDE_FILES)
        linkPath = gdgDirectory
    else:
        PlantFiles(outside, OUTSIDE_FILES)
        gdgDirectory.mkdir()
        linkPath = gdgDirectory / case.base.name
    linkPath.symlink_to(outside, target_is_directory=True)
    return linkPath


def ReconcileThroughLink(context: JobContext, base: gdg.GdgBase) -> None:
    """Run catalog recovery, which lists and deletes orphans in every base directory."""
    gdg.ReconcileCatalog(context)


def WriteGenerationThroughLink(context: JobContext, base: gdg.GdgBase) -> None:
    """Write and publish a one-record generation of base, as a producer does."""
    PublishGenerations(context, base, 1, FIRST_RECORD_TEXT)


LINKED_DIRECTORY_OPERATIONS = (
    pytest.param(ReconcileThroughLink, id="reconcile"),
    pytest.param(WriteGenerationThroughLink, id="new-generation"),
)


def SwapForLinkOnceOpened(
    monkeypatch: pytest.MonkeyPatch, directoryPath: Path, outside: Path
) -> list[Path]:
    """Replace directoryPath with a link to outside right after an operation first opens it.

    The real directory is renamed beside the link; the returned list receives its new path.
    """
    originalOpen = gdg.OpenArchiveDirectory
    swappedPaths: list[Path] = []

    def OpenThenSwap(openedPath: Path, create: bool) -> gdg.ManagedDirectory:
        directory = originalOpen(openedPath, create)
        if openedPath == directoryPath and not swappedPaths:
            swappedPath = directoryPath.with_name(directoryPath.name + SWAPPED_OUT_SUFFIX)
            directoryPath.rename(swappedPath)
            directoryPath.symlink_to(outside, target_is_directory=True)
            swappedPaths.append(swappedPath)
        return directory

    monkeypatch.setattr(gdg, "OpenArchiveDirectory", OpenThenSwap)
    return swappedPaths


def DeleteOrphanPartsOf(context: JobContext, directoryPath: Path) -> int:
    """Delete the orphan .part files of every base directory, as recovery does."""
    return gdg.DeleteOrphanParts(context)


def DeleteOrphanWorkDirectoriesOf(context: JobContext, directoryPath: Path) -> int:
    """Delete the orphan work directories of every base directory, as recovery does."""
    return gdg.DeleteOrphanWorkDirectories(context)


def PublishFirstGenerationIn(context: JobContext, directoryPath: Path) -> str:
    """Publish generation 1 of the directory from its .part file holding PUBLISHED_BYTES."""
    expectedSha256 = hashlib.sha256(PUBLISHED_BYTES).hexdigest()
    finalPath = directoryPath / FileName(OLDEST_GENERATION)
    with context.engine.connect() as connection:
        return gdg.PlaceGenerationFile(finalPath, expectedSha256, connection)


SWAP_CASES = (
    pytest.param(
        SwapCase({ORPHAN_PART_NAME: CORRUPT_FILE_BYTES}, DeleteOrphanPartsOf, 1, {}),
        id="orphan-part",
    ),
    pytest.param(
        SwapCase({ORPHAN_RUN_FILE_PATH: CORRUPT_FILE_BYTES}, DeleteOrphanWorkDirectoriesOf, 1, {}),
        id="orphan-work-directory",
    ),
    pytest.param(
        SwapCase(
            {gdg.PartName(OLDEST_GENERATION): PUBLISHED_BYTES},
            PublishFirstGenerationIn,
            run_ledger.GDG_PUBLISHED,
            {FileName(OLDEST_GENERATION): PUBLISHED_BYTES},
        ),
        id="publication",
    ),
)


@pytest.mark.parametrize("operation", LINKED_DIRECTORY_OPERATIONS)
@pytest.mark.parametrize("case", LINKED_DIRECTORY_CASES)
def test_linked_managed_directory_is_refused_and_the_directory_outside_is_untouched(
    jobContext: JobContext,
    operation: Callable[[JobContext, gdg.GdgBase], None],
    case: LinkedDirectoryCase,
) -> None:
    """A gdg or base directory that is a symbolic link is refused, never followed.

    Recovery and a producer each raise ManagedDirectoryError naming the link, with the kernel's
    errno, before they list, create or delete anything: the directory the link points to keeps
    its .part file and work directory, the link stays, and nothing is cataloged.
    """
    context = LedgeredContext(jobContext)
    outside = OutsideDirectory(context)
    linkPath = LinkManagedDirectory(context, case, outside)
    outsideBefore = TreeSnapshot(outside)

    with pytest.raises(gdg.ManagedDirectoryError) as raised:
        operation(context, case.base)

    assert raised.value.filename == str(linkPath)
    assert errno.errorcode[raised.value.errno] in REFUSED_LINK_ERRNO_NAMES
    assert linkPath.is_symlink()
    assert TreeSnapshot(outside) == outsideBefore
    assert CatalogRows(context.engine, case.base.name) == []


@pytest.mark.parametrize("case", SWAP_CASES)
def test_directory_swapped_for_a_link_once_opened_is_still_the_one_acted_on(
    jobContext: JobContext, monkeypatch: pytest.MonkeyPatch, case: SwapCase
) -> None:
    """A base directory replaced by a link after the operation opened it is still the one used.

    Orphan deletion and publication act through the held directory descriptor, so they change
    the real directory, now renamed aside, and leave the same names in the directory the link
    points to exactly as they were.
    """
    directoryPath = gdg.BaseDirectoryPath(jobContext, gdg.SYSTRAN)
    outside = OutsideDirectory(jobContext)
    PlantFiles(directoryPath, case.held)
    PlantFiles(outside, case.held)
    outsideBefore = TreeSnapshot(outside)
    swappedPaths = SwapForLinkOnceOpened(monkeypatch, directoryPath, outside)

    result = case.operation(jobContext, directoryPath)

    [swappedPath] = swappedPaths
    assert result == case.expectedResult
    assert directoryPath.is_symlink()
    assert TreeSnapshot(swappedPath) == case.heldAfter
    assert TreeSnapshot(outside) == outsideBefore


def test_links_named_like_orphans_in_a_real_base_directory_are_never_followed(
    jobContext: JobContext,
) -> None:
    """A link named like a .part is deleted itself; one named like a work directory is left.

    Neither link's target changes, and only the .part link counts as a deleted orphan.
    """
    directoryPath = gdg.BaseDirectoryPath(jobContext, gdg.SYSTRAN)
    outside = OutsideDirectory(jobContext)
    PlantFiles(outside, OUTSIDE_FILES)
    outsideBefore = TreeSnapshot(outside)
    directoryPath.mkdir(parents=True)
    (directoryPath / ORPHAN_PART_NAME).symlink_to(outside / ORPHAN_PART_NAME)
    workLink = directoryPath / ORPHAN_WORK_DIRECTORY_NAME
    workLink.symlink_to(outside / ORPHAN_WORK_DIRECTORY_NAME, target_is_directory=True)

    summary = gdg.ReconcileCatalog(jobContext)

    assert summary == gdg.ReconcileSummary(published=0, lost=0, orphansDeleted=1)
    assert BaseFileNames(jobContext, gdg.SYSTRAN) == {ORPHAN_WORK_DIRECTORY_NAME}
    assert workLink.is_symlink()
    assert TreeSnapshot(outside) == outsideBefore


def test_new_generation_replaces_a_link_at_its_part_name_instead_of_writing_through_it(
    jobContext: JobContext,
) -> None:
    """A link standing at the new generation's .part name is removed, never written through.

    The records go to a new regular file, published in the base directory, and the file the
    link pointed to keeps its bytes.
    """
    context = LedgeredContext(jobContext)
    directoryPath = gdg.BaseDirectoryPath(context, gdg.SYSTRAN)
    outside = OutsideDirectory(context)
    PlantFiles(outside, OUTSIDE_FILES)
    outsideBefore = TreeSnapshot(outside)
    directoryPath.mkdir(parents=True)
    (directoryPath / gdg.PartName(OLDEST_GENERATION)).symlink_to(outside / ORPHAN_PART_NAME)

    [generation] = PublishGenerations(context, gdg.SYSTRAN, 1, FIRST_RECORD_TEXT)

    finalPath = GenerationPath(context, gdg.SYSTRAN, generation)
    recordBytes = Record(gdg.SYSTRAN, FIRST_RECORD_TEXT).encode(GDG_FILE_ENCODING)
    assert generation == OLDEST_GENERATION
    assert not finalPath.is_symlink()
    assert finalPath.read_bytes() == recordBytes + RECORD_TERMINATOR
    assert BaseFileNames(context, gdg.SYSTRAN) == {finalPath.name}
    assert TreeSnapshot(outside) == outsideBefore


def test_command_refuses_a_linked_base_directory_with_one_file_error_line(
    jobContext: JobContext,
) -> None:
    """load tran-types exits 12 with one FILE ERROR line naming the linked SYSTRAN directory.

    The prelude's recovery refuses the link before it deletes anything, so no run is recorded
    and the directory the link points to keeps its files.
    """
    outside = OutsideDirectory(jobContext)
    linkPath = LinkManagedDirectory(jobContext, LinkedDirectoryCase(False, gdg.SYSTRAN), outside)
    outsideBefore = TreeSnapshot(outside)
    runsBefore = CountJobRuns(jobContext.engine)

    outcome = InvokeCli(LOAD_TRAN_TYPES_ARGUMENTS)

    lines = outcome.output.splitlines()
    refusals = [match for line in lines if (match := REFUSAL_LINE_PATTERN.search(line))]
    assert outcome.exitCode == EXIT_FAILURE, outcome.output
    assert len(refusals) == 1, outcome.output
    assert refusals[0]["errnoName"] in REFUSED_LINK_ERRNO_NAMES
    assert refusals[0].string.endswith(f" ON {linkPath}"), outcome.output
    assert CountJobRuns(jobContext.engine) == runsBefore
    assert TreeSnapshot(outside) == outsideBefore


def AbsoluteOutsideFilePath(context: JobContext, baseName: str) -> tuple[Path, str]:
    """Return a file outside the archive and its absolute path as a forged file_path."""
    namedFile = OutsideDirectory(context) / baseName / FileName(FORGED_GENERATION)
    return namedFile, str(namedFile)


def ClimbingOutsideFilePath(context: JobContext, baseName: str) -> tuple[Path, str]:
    """Return a file outside the archive and its path from GdgDir(), climbing out with "..".

    The file's directory and that directory's parent are real names, so joined to GdgDir() the
    path reaches the file without a ".." where a managed directory name is opened.
    """
    namedFile = OutsideDirectory(context) / baseName / FileName(FORGED_GENERATION)
    climbingPath = namedFile.relative_to(context.settings.GdgDir(), walk_up=True)
    return namedFile, climbingPath.as_posix()


def UnknownBaseFilePath(context: JobContext, baseName: str) -> tuple[Path, str]:
    """Return the file of a base GDG_BASES does not hold, and "<BASE>/G####V00.dat" for it."""
    fileName = FileName(FORGED_GENERATION)
    return context.settings.GdgDir() / baseName / fileName, f"{baseName}/{fileName}"


@dataclass(frozen=True)
class ForgedRowCase:
    """A forged catalog row of generation FORGED_GENERATION, by base name and state.

    namedFile returns, given the context and the base name, the file the row's file_path names
    and that file_path. Without the refusal, a COMMITTED row would have the matching .part beside
    that file renamed, a PUBLISHED row with LEGACY_GDG_LIMIT newer generations and a ROLLED_OFF
    row whose final file is left in its base directory would each have that file deleted, and a
    LOST row would be read by the LOST check.
    """

    baseName: str
    state: str
    namedFile: Callable[[JobContext, str], tuple[Path, str]]


OUTSIDE_FILE_PATH_FORMS = {"absolute": AbsoluteOutsideFilePath, "dot-dot": ClimbingOutsideFilePath}
FORGED_ROW_STATES = (
    run_ledger.GDG_COMMITTED,
    run_ledger.GDG_PUBLISHED,
    run_ledger.GDG_ROLLED_OFF,
    run_ledger.GDG_LOST,
)
FORGED_ROW_CASES = [
    pytest.param(ForgedRowCase(gdg.SYSTRAN.name, state, namedFile), id=f"{formName}-{state}")
    for formName, namedFile in OUTSIDE_FILE_PATH_FORMS.items()
    for state in FORGED_ROW_STATES
] + [
    pytest.param(
        ForgedRowCase(UNKNOWN_BASE_NAME, run_ledger.GDG_COMMITTED, UnknownBaseFilePath),
        id=f"unknown-base-{run_ledger.GDG_COMMITTED}",
    )
]


def CatalogRowValues(
    context: JobContext, baseName: str, generation: int, state: str
) -> dict[str, object]:
    """Return a catalog row of the context's run, with the derived file_path and forged hash."""
    return {
        "base_name": baseName,
        "generation": generation,
        "file_path": f"{baseName}/{FileName(generation)}",
        "record_count": 1,
        "sha256": FORGED_FILE_SHA256,
        "state": state,
        "run_id": context.runId,
    }


def PlantForgedRow(context: JobContext, case: ForgedRowCase) -> str:
    """Catalog the case's forged row with the files that make a stage act on it; return its path.

    The file it names holds FORGED_FILE_BYTES, as its .part when the row is COMMITTED. Its base
    directory exists, as an earlier generation leaves it, holding the row's own final file when
    the row is ROLLED_OFF. A PUBLISHED row gets LEGACY_GDG_LIMIT newer PUBLISHED generations.
    """
    namedFile, filePath = case.namedFile(context, case.baseName)
    plantedFile = PartPath(namedFile) if case.state == run_ledger.GDG_COMMITTED else namedFile
    PlantFiles(plantedFile.parent, {plantedFile.name: FORGED_FILE_BYTES})
    leftovers = {FileName(FORGED_GENERATION): FORGED_FILE_BYTES}
    isRolledOff = case.state == run_ledger.GDG_ROLLED_OFF
    PlantFiles(context.settings.GdgDir() / case.baseName, leftovers if isRolledOff else {})
    forgedRow = CatalogRowValues(context, case.baseName, FORGED_GENERATION, case.state)
    rows = [{**forgedRow, "file_path": filePath}]
    if case.state == run_ledger.GDG_PUBLISHED:
        rows += [
            CatalogRowValues(context, case.baseName, generation, run_ledger.GDG_PUBLISHED)
            for generation in range(FORGED_GENERATION + 1, FORGED_GENERATION + 1 + LEGACY_GDG_LIMIT)
        ]
    with context.engine.begin() as connection:
        connection.execute(insert(GDG_GENERATIONS), rows)
    return filePath


@pytest.mark.parametrize("case", FORGED_ROW_CASES)
def test_catalog_row_whose_file_path_is_not_derived_is_refused_before_any_file_effect(
    jobContext: JobContext, case: ForgedRowCase
) -> None:
    """load tran-types exits 12 on a catalog row whose file_path is not its derived path.

    The stage meeting the row (publication, retention, leftover deletion or the LOST check)
    refuses it with one ERROR line and one FILE ERROR CatalogPathError line naming the stored
    file_path, before anything is read, renamed or deleted: the files outside and inside the
    archive keep their bytes, every row of the base keeps its state, and no run is recorded.
    """
    context = LedgeredContext(jobContext)
    filePath = PlantForgedRow(context, case)
    outside, archive = OutsideDirectory(context), context.settings.GdgDir()
    outsideBefore, archiveBefore = TreeSnapshot(outside), TreeSnapshot(archive)
    statesBefore = CatalogStates(context.engine, case.baseName)
    runsBefore = CountJobRuns(context.engine)

    outcome = InvokeCli(LOAD_TRAN_TYPES_ARGUMENTS)

    refusals = [line for line in outcome.output.splitlines() if CATALOG_PATH_REFUSAL_PREFIX in line]
    generationName = gdg.GenerationName(case.baseName, FORGED_GENERATION)
    refusedLog = gdg.CATALOG_PATH_REFUSED_LOG % (generationName, filePath)
    assert outcome.exitCode == EXIT_FAILURE, outcome.output
    assert len(refusals) == 1, outcome.output
    assert refusals[0].endswith(CATALOG_PATH_REFUSAL_PREFIX + filePath), outcome.output
    assert outcome.output.count(refusedLog) == 1, outcome.output
    assert TreeSnapshot(outside) == outsideBefore
    assert TreeSnapshot(archive) == archiveBefore
    assert CatalogStates(context.engine, case.baseName) == statesBefore
    assert CountJobRuns(context.engine) == runsBefore


@pytest.mark.parametrize(
    "filePathTemplate", list(ESCAPING_CATALOG_PATHS.values()), ids=list(ESCAPING_CATALOG_PATHS)
)
def test_catalog_file_path_resolving_outside_gdg_root_is_refused(
    tmp_path: Path, filePathTemplate: str
) -> None:
    """A catalog file_path resolving outside outputDir/gdg, or to it, raises ValueError naming it.

    An absolute path outside the root, a "../" escape, an in-root symlink to a directory or file
    outside, and the root itself are refused alike, the check LatestPublishedGeneration's readers
    and LoseLatestGeneration's deletions rely on.
    """
    outputDir, outsideDir = BuildCatalogRoots(tmp_path)
    filePath = filePathTemplate.format(outsideDir=outsideDir)

    with pytest.raises(ValueError, match=re.escape(repr(filePath))):
        ResolveCatalogFilePath(outputDir, filePath)


@pytest.mark.parametrize(
    "filePathTemplate", list(INSIDE_CATALOG_PATHS.values()), ids=list(INSIDE_CATALOG_PATHS)
)
def test_catalog_file_path_inside_gdg_root_resolves_there(
    tmp_path: Path, filePathTemplate: str
) -> None:
    """The batch's relative "<BASE>/G####V00.dat" and an in-root absolute path name one file."""
    outputDir, _ = BuildCatalogRoots(tmp_path)
    gdgRoot = outputDir / GDG_DIRECTORY_NAME
    filePath = filePathTemplate.format(gdgRoot=gdgRoot)

    resolvedPath = ResolveCatalogFilePath(outputDir, filePath)

    assert resolvedPath == gdgRoot.resolve() / SAMPLE_FILE_PATH


def test_lost_generation_blocks_cycle_until_new_cycle(
    jobContext: JobContext, outputDir: Path
) -> None:
    """A LOST SYSTRAN(0) refuses run-all and job comb-tran until run-all --new-cycle from step 1."""
    engine = jobContext.engine
    systranRow = StopChainAfterStep15(engine)
    lostGeneration = systranRow["generation"]
    LoseLatestGeneration(jobContext, systranRow)
    lostText = LostGenerationText(engine, gdg.SYSTRAN.name, lostGeneration)
    runCountBefore = CountJobRuns(engine)

    AssertBlockedByLostGeneration(RUN_ALL_ARGUMENTS, lostText)
    assert CatalogStates(engine, gdg.SYSTRAN.name) == {lostGeneration: run_ledger.GDG_LOST}
    AssertBlockedByLostGeneration(RUN_ALL_ARGUMENTS, lostText)
    AssertBlockedByLostGeneration(COMB_TRAN_ARGUMENTS, lostText)
    AssertNewCycleStartAtRefused(engine, lostGeneration, runCountBefore)

    AssertNewCycleRebuildsCombined(engine, outputDir)
    assert CycleStatuses(engine) == [run_ledger.CYCLE_ABANDONED, run_ledger.CYCLE_COMPLETED]
    expectedStates = {
        lostGeneration: run_ledger.GDG_ABANDONED,
        lostGeneration + 1: run_ledger.GDG_PUBLISHED,
    }
    assert CatalogStates(engine, gdg.SYSTRAN.name) == expectedStates


def test_resolve_generation_refuses_base_with_lost_row_in_open_cycle(
    jobContext: JobContext,
) -> None:
    """SYSTRAN(0) is refused while its newest generation is LOST; other bases still resolve."""
    context = LedgeredContext(jobContext)
    [generation] = PublishGenerations(context, gdg.SYSTRAN, 1, FIRST_RECORD_TEXT)
    LoseLatestGeneration(context, CatalogRows(context.engine, gdg.SYSTRAN.name)[0])
    assert gdg.ReconcileCatalog(context).lost == 1

    with pytest.raises(gdg.LostGenerationError) as raised:
        gdg.ResolveGeneration(context, gdg.SYSTRAN, 0)

    assert [lost.generation for lost in raised.value.generations] == [generation]
    assert raised.value.message == LostGenerationText(context.engine, gdg.SYSTRAN.name, generation)
    with pytest.raises(gdg.LostGenerationError):
        gdg.CheckNoLostGenerations(context)
    with pytest.raises(gdg.MissingGenerationError):
        gdg.ResolveGeneration(context, gdg.DALYREJS, 0)


@pytest.mark.parametrize("base", SCRATCH_BASES, ids=SCRATCH_BASE_IDS)
def test_sixth_scratch_generation_rolls_off_oldest_and_deletes_file(
    jobContext: JobContext, base: gdg.GdgBase
) -> None:
    """LIMIT(5) SCRATCH: the sixth publication rolls off generation 1 and deletes its file."""
    context = LedgeredContext(jobContext)
    PublishGenerations(context, base, SIXTH_GENERATION, RETENTION_RECORD_TEXT)

    assert base.scratch
    AssertOldestRolledOff(context, base)
    assert not GenerationPath(context, base, OLDEST_GENERATION).exists()


def test_sixth_bkup_generation_rolls_off_oldest_but_keeps_file_noscratch(
    jobContext: JobContext,
) -> None:
    """NOSCRATCH TRANSACT.BKUP: generation 1 rolls off, unresolvable, but its file is kept."""
    context = LedgeredContext(jobContext)
    PublishGenerations(context, gdg.TRANSACT_BKUP, SIXTH_GENERATION, RETENTION_RECORD_TEXT)

    assert not gdg.TRANSACT_BKUP.scratch
    AssertOldestRolledOff(context, gdg.TRANSACT_BKUP)
    assert GenerationPath(context, gdg.TRANSACT_BKUP, OLDEST_GENERATION).is_file()


def test_failed_posttran_catalogs_no_dalyrejs_generation(
    jobContext: JobContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSTTRAN abending with DALYREJS(+1) open leaves no catalog row and no file."""
    partStates = InjectAbendOnceRejectsWritten(monkeypatch)

    outcome = InvokeCli(POST_TRAN_ARGUMENTS)

    assert outcome.exitCode == EXIT_ABEND, outcome.output
    assert INJECTED_ABEND_MESSAGE in outcome.output
    assert partStates == [True]
    assert StepRow(jobContext.engine, POSTTRAN_STEP)["state"] == run_ledger.STEP_FAILED
    assert CatalogRows(jobContext.engine, gdg.DALYREJS.name) == []
    assert BaseFileNames(jobContext, gdg.DALYREJS) == set()
