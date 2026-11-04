"""GDG archives: publication, catalog recovery, LOST blocking, abnormal disposition and retention.

Pins batch/orchestration/gdg.py to Technical Specification Section 0.6.8 ("GDG archives"): the
four chained bases TRANSACT.BKUP, DALYREJS, SYSTRAN and TRANSACT.COMBINED, written as
CARDDEMO_OUTPUT_DIR/gdg/<BASE>/G####V00.dat with the batch.gdg_generations catalog.

- Publication: .part, then the COMMITTED row with its SHA-256, then the rename to PUBLISHED; (0)
  and (-n) count PUBLISHED rows only, the DSN=...(0) references of jcl/COMBTRAN.jcl.
- Recovery: ReconcileCatalog, at the start of every command, completes an interrupted rename,
  marks a row whose file is gone LOST and deletes orphan .part files.
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

import hashlib
import re
from collections.abc import Sequence
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
from batch.tests.reconciliation.compare import CompareFixedWidthFiles, LatestPublishedGeneration

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
OLDEST_GENERATION = 1
SIXTH_GENERATION = gdg.GDG_LIMIT + 1
OLDEST_RETAINED_RELATIVE = -(gdg.GDG_LIMIT - 1)
SCRATCH_BASES = [gdg.DALYREJS, gdg.SYSTRAN, gdg.TRANSACT_COMBINED]
SCRATCH_BASE_IDS = [base.name for base in SCRATCH_BASES]

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


class InjectedProducerError(Exception):
    """Stands for any failure of a job while it writes a new generation."""


# --- Helpers -------------------------------------------------------------------------------------
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
    """Leave a generation COMMITTED with neither its final nor its .part file, a lost archive."""
    baseName, generation = row["base_name"], row["generation"]
    SetGenerationState(context.engine, baseName, generation, run_ledger.GDG_COMMITTED)
    finalPath = context.settings.GdgDir() / row["file_path"]
    finalPath.unlink(missing_ok=True)
    PartPath(finalPath).unlink(missing_ok=True)


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
        gdg.ResolveGeneration(context, base, -gdg.GDG_LIMIT)


# --- Publication and recovery --------------------------------------------------------------------
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


def test_reconcile_deletes_orphan_part_file_without_catalog_row(jobContext: JobContext) -> None:
    """A .part with no catalog row, a crash before commit, is deleted by the reconcile."""
    orphanPath = PartPath(GenerationPath(jobContext, gdg.SYSTRAN, ORPHAN_GENERATION))
    orphanPath.parent.mkdir(parents=True)
    orphanPath.write_bytes(CORRUPT_FILE_BYTES)

    summary = gdg.ReconcileCatalog(jobContext)

    assert summary == gdg.ReconcileSummary(published=0, lost=0, orphansDeleted=1)
    assert not orphanPath.exists()
    assert CatalogRows(jobContext.engine, gdg.SYSTRAN.name) == []


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


# --- LOST blocking -------------------------------------------------------------------------------
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


# --- Retention and abnormal disposition of a producing job --------------------------------------
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
