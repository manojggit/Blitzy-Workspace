"""Replaces: DEFGDGB, DEFGDGD, DALYREJS, REPTFILE, REPROC, REPROCT (GDG bases, generations and
LISTCAT retention).

The generation data groups the chain writes, as fixed-width files under
BatchSettings.GdgDir()/<BASE>/G####V00.dat with the catalog batch.gdg_generations. Specified in
the Technical Specification, Sections 0.6.8 ("GDG archives"), 0.6.11 ("GDG records") and 0.7.2,
and transcribed in docs/batch.md ("GDG archives").

- Publication: NewGeneration writes G####V00.dat.part and fsyncs it, the COMMITTED catalog row
  is the producing step's final database write, then the file is renamed and the row becomes
  PUBLISHED. Given the caller's connection (atomic and final-transaction callers), the row joins
  the caller's transaction and PublishRunGenerations publishes it once that has committed.
- Recovery: ReconcileCatalog completes an interrupted publication, marks a row whose file is
  gone LOST and deletes orphan .part files. A LOST row blocks the OPEN cycle until seed-all or
  run-all --new-cycle abandons it (run_ledger.AbandonOpenCycle).
- Abnormal disposition: an exception inside NewGeneration deletes the .part and catalogs
  nothing, as DISP=(NEW,CATLG,DELETE) does.
- Retention: LIMIT 5. The oldest PUBLISHED generation beyond it becomes ROLLED_OFF; SCRATCH bases
  also delete its file, NOSCRATCH TRANSACT.BKUP keeps it.

Records arrive already encoded by record_layouts.EncodeRecord; this module only checks their
length and writes them, one per line ended by "\\n". Base names come from GDG_BASES only, never
from user input. The batch window admits one batch process, so generation numbers need no lock.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from app.models import GDG_GENERATIONS, JOB_RUNS
from app.utils.record_layouts import DALYREJS_LAYOUT, TRANSACTION_LAYOUT
from sqlalchemy import Select, Update, func, insert, select, update
from sqlalchemy.engine import Connection, Row

from batch.orchestration.job_context import JobContext
from batch.orchestration.run_ledger import (
    GDG_COMMITTED,
    GDG_LOST,
    GDG_PUBLISHED,
    GDG_ROLLED_OFF,
    FindOpenCycle,
)

LOGGER = logging.getLogger(__name__)

# LIMIT 5 of every chained base in catlg/LISTCAT.txt and jcl/DEFGDGB.jcl.
GDG_LIMIT = 5
# The code-page-037 repertoire is exactly Latin-1, so every character is one byte and each
# record is exactly LRECL bytes plus the terminator, the basis the reconciliation compares on.
GDG_FILE_ENCODING = "latin-1"
RECORD_TERMINATOR = b"\n"
FORBIDDEN_RECORD_CHARACTERS = ("\r", "\n")
PART_SUFFIX = ".part"
GENERATION_FILE_TEMPLATE = "G{generation:04d}V00.dat"
GENERATION_NAME_TEMPLATE = "{base}.G{generation:04d}V00"
LOST_GENERATION_TEMPLATE = (
    "GENERATION {base}.G{generation:04d}V00 OF CYCLE {cycleId} IS LOST;"
    " RUN seed-all OR run-all --new-cycle"
)
LOST_GENERATION_SEPARATOR = "; "
MISSING_GENERATION_TEMPLATE = "{base}({relative}) NOT FOUND"
HASH_CHUNK_SIZE = 64 * 1024

UNLEDGERED_MESSAGE = "a generation is cataloged against a started run; the context has none"

PUBLISHED_LOG = "GENERATION %s PUBLISHED: %d RECORDS"
LOST_LOG = "GENERATION %s IS LOST: NEITHER %s NOR ITS .part FILE MATCHES SHA-256 %s"
ROLLED_OFF_LOG = "GENERATION %s ROLLED OFF (LIMIT %d); FILE %s"
ORPHAN_DELETED_LOG = "ORPHAN GENERATION FILE %s DELETED"
DIRECTORY_FSYNC_LOG = "DIRECTORY FSYNC REFUSED FOR %s: %s"
RECONCILE_LOG = "GDG CATALOG RECONCILED: %d PUBLISHED, %d LOST, %d ORPHAN .part FILES DELETED"
FILE_SCRATCHED = "SCRATCHED"
FILE_KEPT = "KEPT (NOSCRATCH)"


@dataclass(frozen=True)
class GdgBase:
    """A generation data group base: its name, record length and LISTCAT SCRATCH attribute."""

    name: str
    lrecl: int
    scratch: bool


# NOSCRATCH per catlg/LISTCAT.txt line 1637, which outranks the SCRATCH of jcl/DEFGDGB.jcl.
TRANSACT_BKUP = GdgBase("TRANSACT.BKUP", TRANSACTION_LAYOUT.lrecl, False)
DALYREJS = GdgBase("DALYREJS", DALYREJS_LAYOUT.lrecl, True)
SYSTRAN = GdgBase("SYSTRAN", TRANSACTION_LAYOUT.lrecl, True)
TRANSACT_COMBINED = GdgBase("TRANSACT.COMBINED", TRANSACTION_LAYOUT.lrecl, True)
GDG_BASES: dict[str, GdgBase] = {
    base.name: base for base in (TRANSACT_BKUP, DALYREJS, SYSTRAN, TRANSACT_COMBINED)
}


@dataclass(frozen=True)
class Generation:
    """One cataloged generation; path is the absolute location of its final file."""

    baseName: str
    generation: int
    path: Path
    recordCount: int
    sha256: str
    state: str


@dataclass(frozen=True)
class ReconcileSummary:
    """What ReconcileCatalog did: rows published, rows found LOST, orphan .part files deleted."""

    published: int
    lost: int
    orphansDeleted: int


class LostGenerationError(Exception):
    """The OPEN cycle has LOST generations; message names each one and the recovery commands."""

    def __init__(self, message: str, generations: tuple[Generation, ...]) -> None:
        super().__init__(message)
        self.message = message
        self.generations = generations


class MissingGenerationError(LookupError):
    """No PUBLISHED generation exists at the requested relative number, a JCL error on MVS."""


class GenerationRecordError(ValueError):
    """A record that is not LRECL Latin-1 characters, or that holds a CR or LF."""


def GenerationName(baseName: str, generation: int) -> str:
    """Return the dataset-style name of a generation, for example SYSTRAN.G0003V00."""
    return GENERATION_NAME_TEMPLATE.format(base=baseName, generation=generation)


def BaseDirectory(context: JobContext, base: GdgBase) -> Path:
    """Return the base's directory under GdgDir(), creating it when absent."""
    directory = context.settings.GdgDir() / base.name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def RelativeFilePath(base: GdgBase, generation: int) -> str:
    """Return the catalog file_path, "<BASE>/G####V00.dat", relative to GdgDir().

    A relative path keeps the archives valid when CARDDEMO_OUTPUT_DIR moves, as in containers.
    """
    fileName = GENERATION_FILE_TEMPLATE.format(generation=generation)
    return PurePosixPath(base.name, fileName).as_posix()


def ResolveFilePath(context: JobContext, filePath: str) -> Path:
    """Return the absolute path of a catalog file_path."""
    return context.settings.GdgDir() / filePath


def PartPath(path: Path) -> Path:
    """Return the .part sibling a generation file is written as before publication."""
    return path.with_name(path.name + PART_SUFFIX)


def GenerationFromRow(context: JobContext, row: Row) -> Generation:
    """Build a Generation from a batch.gdg_generations row."""
    return Generation(
        baseName=row.base_name,
        generation=row.generation,
        path=ResolveFilePath(context, row.file_path),
        recordCount=row.record_count,
        sha256=row.sha256,
        state=row.state,
    )


def EncodeGenerationRecord(record: str, base: GdgBase, recordNumber: int) -> bytes:
    """Return one record's bytes and terminator, refusing a record the base cannot hold."""
    where = f"{base.name} record {recordNumber}"
    if not isinstance(record, str) or len(record) != base.lrecl:
        length = len(record) if isinstance(record, str) else type(record).__name__
        raise GenerationRecordError(f"{where} is {length} characters; its LRECL is {base.lrecl}")
    if any(character in record for character in FORBIDDEN_RECORD_CHARACTERS):
        raise GenerationRecordError(f"{where} holds a CR or LF")
    try:
        return record.encode(GDG_FILE_ENCODING) + RECORD_TERMINATOR
    except UnicodeEncodeError as error:
        raise GenerationRecordError(f"{where} holds a character outside Latin-1") from error


class GenerationWriter:
    """Writes the records of one new generation to its open .part file, hashing every byte."""

    def __init__(self, handle: BinaryIO, base: GdgBase, generation: int) -> None:
        self.handle = handle
        self.base = base
        self.generation = generation
        self.partPath = Path(handle.name)
        self.recordCount = 0
        self.digest = hashlib.sha256()

    def WriteRecord(self, record: str) -> None:
        """Write one LRECL-character record and its "\\n" terminator."""
        recordBytes = EncodeGenerationRecord(record, self.base, self.recordCount + 1)
        self.handle.write(recordBytes)
        self.digest.update(recordBytes)
        self.recordCount += 1

    def HexDigest(self) -> str:
        """Return the SHA-256 of every byte written so far, as the catalog stores it."""
        return self.digest.hexdigest()


def NextGenerationNumber(connection: Connection, base: GdgBase) -> int:
    """Return one past the base's highest generation number, whatever that row's state."""
    statement = select(func.coalesce(func.max(GDG_GENERATIONS.c.generation), 0) + 1).where(
        GDG_GENERATIONS.c.base_name == base.name
    )
    return connection.execute(statement).scalar_one()


def AllocateGeneration(context: JobContext, base: GdgBase, connection: Connection | None) -> int:
    """Number the new generation, on the caller's open transaction when it has one.

    A caller's connection that is not yet in a transaction holds no catalog row of its own, so
    a short read of the engine gives the same number without opening the caller's transaction
    for the whole job.
    """
    if connection is not None and connection.in_transaction():
        return NextGenerationNumber(connection, base)
    with context.engine.connect() as ownConnection:
        return NextGenerationNumber(ownConnection, base)


def OpenGenerationWriter(
    context: JobContext, base: GdgBase, connection: Connection | None
) -> GenerationWriter:
    """Allocate the next generation and open its .part file for writing."""
    generation = AllocateGeneration(context, base, connection)
    fileName = GENERATION_FILE_TEMPLATE.format(generation=generation)
    partPath = PartPath(BaseDirectory(context, base) / fileName)
    return GenerationWriter(partPath.open("wb"), base, generation)


def SyncAndClose(writer: GenerationWriter) -> None:
    """Flush the .part file to stable storage and close it."""
    writer.handle.flush()
    os.fsync(writer.handle.fileno())
    writer.handle.close()


def DiscardPart(writer: GenerationWriter) -> None:
    """Close and delete an uncataloged .part file (DISP=(NEW,CATLG,DELETE) on an abend)."""
    try:
        writer.handle.close()
    finally:
        writer.partPath.unlink(missing_ok=True)


def CommittedRowValues(context: JobContext, writer: GenerationWriter) -> dict[str, object]:
    """Return the COMMITTED catalog row of a fully written generation."""
    return {
        "base_name": writer.base.name,
        "generation": writer.generation,
        "file_path": RelativeFilePath(writer.base, writer.generation),
        "record_count": writer.recordCount,
        "sha256": writer.HexDigest(),
        "state": GDG_COMMITTED,
        "run_id": context.runId,
        "created_at": func.now(),
    }


def InsertCommittedRow(connection: Connection, values: dict[str, object]) -> Row:
    """Insert a COMMITTED catalog row on the given connection and return it."""
    statement = insert(GDG_GENERATIONS).values(**values).returning(*GDG_GENERATIONS.c)
    return connection.execute(statement).one()


def CatalogGeneration(
    context: JobContext, writer: GenerationWriter, connection: Connection | None
) -> Row:
    """Insert the COMMITTED row: in the caller's transaction, or in a transaction of its own."""
    values = CommittedRowValues(context, writer)
    if connection is not None:
        return InsertCommittedRow(connection, values)
    with context.engine.begin() as ownConnection:
        return InsertCommittedRow(ownConnection, values)


def RequireLedgered(context: JobContext) -> None:
    """Refuse a context with no started run: catalog rows carry run_id, and LOST blocks by cycle."""
    if not context.IsLedgered():
        raise ValueError(UNLEDGERED_MESSAGE)


@contextlib.contextmanager
def NewGeneration(
    context: JobContext, base: GdgBase, connection: Connection | None = None
) -> Iterator[GenerationWriter]:
    """Write a new (+1) generation of base, yielding the writer of its records.

    On an exception inside the block the .part file is deleted and nothing is cataloged. On a
    normal exit the file is fsynced and its COMMITTED row inserted as the final database write.
    With connection, the row joins the caller's transaction and stays COMMITTED until
    PublishRunGenerations runs after the caller's commit. Without it, the row commits in its own
    transaction and the generation is published, and retention applied, at once.
    """
    RequireLedgered(context)
    writer = OpenGenerationWriter(context, base, connection)
    cataloged = False
    try:
        yield writer
        SyncAndClose(writer)
        row = CatalogGeneration(context, writer, connection)
        cataloged = True
    finally:
        if not cataloged:
            DiscardPart(writer)
    if connection is None:
        PublishAndRetain(context, [row])


def FileSha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file, read in 64 KiB chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def HashMatches(path: Path, expectedSha256: str) -> bool:
    """Return whether the file exists with the expected SHA-256."""
    try:
        return FileSha256(path) == expectedSha256
    except FileNotFoundError:
        return False


def FsyncDirectory(directory: Path) -> None:
    """Make a rename in directory durable; a platform that refuses directory fsync is logged."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError as error:
        LOGGER.debug(DIRECTORY_FSYNC_LOG, directory, error)
        return
    try:
        os.fsync(descriptor)
    except OSError as error:
        LOGGER.debug(DIRECTORY_FSYNC_LOG, directory, error)
    finally:
        os.close(descriptor)


def PlaceGenerationFile(finalPath: Path, expectedSha256: str) -> str:
    """Put a COMMITTED generation's file in place; return PUBLISHED, or LOST when it is gone."""
    if HashMatches(finalPath, expectedSha256):
        return GDG_PUBLISHED
    partPath = PartPath(finalPath)
    if not HashMatches(partPath, expectedSha256):
        return GDG_LOST
    os.replace(partPath, finalPath)
    FsyncDirectory(finalPath.parent)
    return GDG_PUBLISHED


def SetStateStatement(row: Row, fromState: str, toState: str) -> Update:
    """Return the update moving one catalog row from fromState to toState."""
    return (
        update(GDG_GENERATIONS)
        .where(
            GDG_GENERATIONS.c.base_name == row.base_name,
            GDG_GENERATIONS.c.generation == row.generation,
            GDG_GENERATIONS.c.state == fromState,
        )
        .values(state=toState)
    )


def PublishCommittedRow(context: JobContext, row: Row) -> str:
    """Publish one COMMITTED row, or mark it LOST when no file matches; return the new state.

    The file is renamed before the row changes, so a crash in between leaves a COMMITTED row
    whose final file matches, which the next ReconcileCatalog publishes.
    """
    finalPath = ResolveFilePath(context, row.file_path)
    newState = PlaceGenerationFile(finalPath, row.sha256)
    with context.engine.begin() as connection:
        connection.execute(SetStateStatement(row, GDG_COMMITTED, newState))
    generationName = GenerationName(row.base_name, row.generation)
    if newState == GDG_LOST:
        LOGGER.error(LOST_LOG, generationName, finalPath, row.sha256)
    else:
        LOGGER.info(PUBLISHED_LOG, generationName, row.record_count)
    return newState


def PublishRows(context: JobContext, rows: Iterable[Row]) -> tuple[set[str], int]:
    """Publish COMMITTED rows; return the base names that gained a generation and the LOST count."""
    gainedBaseNames: set[str] = set()
    lostCount = 0
    for row in rows:
        if PublishCommittedRow(context, row) == GDG_PUBLISHED:
            gainedBaseNames.add(row.base_name)
        else:
            lostCount += 1
    return gainedBaseNames, lostCount


def RollOffBases(context: JobContext, baseNames: Iterable[str]) -> int:
    """Apply retention to each named base; return how many generations rolled off."""
    return sum(RollOff(context, GDG_BASES[baseName]) for baseName in sorted(baseNames))


def PublishAndRetain(context: JobContext, rows: list[Row]) -> int:
    """Publish COMMITTED rows, then apply retention to every base that gained one.

    Returns the number published. A row found LOST is logged and left LOST, which blocks the
    OPEN cycle (CheckNoLostGenerations, ResolveGeneration).
    """
    gainedBaseNames, lostCount = PublishRows(context, rows)
    RollOffBases(context, gainedBaseNames)
    return len(rows) - lostCount


def SelectRows(context: JobContext, statement: Select) -> list[Row]:
    """Run a catalog query in a short read and return its rows."""
    with context.engine.connect() as connection:
        return list(connection.execute(statement).all())


def CommittedRowsStatement() -> Select:
    """Return the query of every COMMITTED catalog row, in base and generation order."""
    return (
        select(GDG_GENERATIONS)
        .where(GDG_GENERATIONS.c.state == GDG_COMMITTED)
        .order_by(GDG_GENERATIONS.c.base_name, GDG_GENERATIONS.c.generation)
    )


def PublishRunGenerations(context: JobContext) -> int:
    """Publish the COMMITTED generations of the context's run; return how many were published.

    batch_chain calls it after every successful step, once the step's transaction has
    committed; with no COMMITTED row, or no started run, it does nothing.
    """
    if not context.IsLedgered():
        return 0
    statement = CommittedRowsStatement().where(GDG_GENERATIONS.c.run_id == context.runId)
    return PublishAndRetain(context, SelectRows(context, statement))


def PublishedNewestFirstStatement(base: GdgBase) -> Select:
    """Return the query of the base's PUBLISHED generations, newest first: (0), (-1), ..."""
    return (
        select(GDG_GENERATIONS)
        .where(GDG_GENERATIONS.c.base_name == base.name, GDG_GENERATIONS.c.state == GDG_PUBLISHED)
        .order_by(GDG_GENERATIONS.c.generation.desc())
    )


def ScratchFile(context: JobContext, base: GdgBase, row: Row) -> str:
    """Delete a rolled-off generation's file when its base is SCRATCH; return what happened."""
    if not base.scratch:
        return FILE_KEPT
    ResolveFilePath(context, row.file_path).unlink(missing_ok=True)
    return FILE_SCRATCHED


def RollOff(context: JobContext, base: GdgBase, limit: int = GDG_LIMIT) -> int:
    """Roll off every PUBLISHED generation of base beyond the newest limit; return the count.

    The rows become ROLLED_OFF, so no reference resolves them, and commit before any SCRATCH
    file is deleted: a crash in between leaves an unreferenced leftover file, never a
    resolvable generation without its file. TRANSACT.BKUP (NOSCRATCH) keeps its files.
    """
    statement = PublishedNewestFirstStatement(base).offset(limit)
    with context.engine.begin() as connection:
        expiredRows = connection.execute(statement).all()
        for row in expiredRows:
            connection.execute(SetStateStatement(row, GDG_PUBLISHED, GDG_ROLLED_OFF))
    for row in expiredRows:
        disposition = ScratchFile(context, base, row)
        generationName = GenerationName(row.base_name, row.generation)
        LOGGER.info(ROLLED_OFF_LOG, generationName, limit, disposition)
    return len(expiredRows)


def DeleteOrphanParts(context: JobContext) -> int:
    """Delete every .part file left under the base directories; return how many there were."""
    deletedCount = 0
    for base in GDG_BASES.values():
        directory = context.settings.GdgDir() / base.name
        for partPath in sorted(directory.glob("*" + PART_SUFFIX)):
            partPath.unlink(missing_ok=True)
            LOGGER.warning(ORPHAN_DELETED_LOG, partPath)
            deletedCount += 1
    return deletedCount


def ReconcileCatalog(context: JobContext) -> ReconcileSummary:
    """Complete or condemn interrupted publications, and delete orphan .part files.

    batch/cli.py runs it at the start of every command, under the batch window and after
    run_ledger.ClassifyInterruptedRuns; batch_chain also runs it after a failed step. Once every
    COMMITTED row is PUBLISHED or LOST, any .part left is an orphan of a crash before commit, or
    the corrupt file of a LOST row.
    """
    committedRows = SelectRows(context, CommittedRowsStatement())
    gainedBaseNames, lostCount = PublishRows(context, committedRows)
    orphansDeleted = DeleteOrphanParts(context)
    RollOffBases(context, gainedBaseNames)
    summary = ReconcileSummary(
        published=len(committedRows) - lostCount, lost=lostCount, orphansDeleted=orphansDeleted
    )
    LOGGER.info(RECONCILE_LOG, summary.published, summary.lost, summary.orphansDeleted)
    return summary


def LostRowsStatement(cycleId: int, baseName: str | None) -> Select:
    """Return the query of the LOST generations written by runs of a cycle, optionally one base."""
    statement = (
        select(GDG_GENERATIONS)
        .join(JOB_RUNS, GDG_GENERATIONS.c.run_id == JOB_RUNS.c.run_id)
        .where(JOB_RUNS.c.cycle_id == cycleId, GDG_GENERATIONS.c.state == GDG_LOST)
    )
    if baseName is not None:
        statement = statement.where(GDG_GENERATIONS.c.base_name == baseName)
    return statement.order_by(GDG_GENERATIONS.c.base_name, GDG_GENERATIONS.c.generation)


def LostGenerationsOfOpenCycle(
    context: JobContext, baseName: str | None = None
) -> tuple[int | None, list[Generation]]:
    """Return the OPEN cycle's id and its LOST generations; (None, []) with no OPEN cycle."""
    with context.engine.connect() as connection:
        cycleId = FindOpenCycle(connection)
        if cycleId is None:
            return None, []
        rows = connection.execute(LostRowsStatement(cycleId, baseName)).all()
    return cycleId, [GenerationFromRow(context, row) for row in rows]


def BuildLostGenerationError(cycleId: int, generations: list[Generation]) -> LostGenerationError:
    """Return the error naming every lost generation of the cycle and the recovery commands."""
    message = LOST_GENERATION_SEPARATOR.join(
        LOST_GENERATION_TEMPLATE.format(
            base=generation.baseName, generation=generation.generation, cycleId=cycleId
        )
        for generation in generations
    )
    return LostGenerationError(message, tuple(generations))


def FindLostGenerations(context: JobContext) -> list[Generation]:
    """Return the LOST generations of the OPEN cycle, by base and generation; [] with none OPEN."""
    return LostGenerationsOfOpenCycle(context)[1]


def CheckNoLostGenerations(context: JobContext) -> None:
    """Raise LostGenerationError while the OPEN cycle has a LOST generation.

    batch_chain calls it before executing anything in run-all, job and load; only seed-all and
    run-all --new-cycle bypass it, abandoning the cycle and its LOST rows.
    """
    cycleId, lostGenerations = LostGenerationsOfOpenCycle(context)
    if lostGenerations:
        raise BuildLostGenerationError(cycleId, lostGenerations)


def CheckRelative(relative: int) -> None:
    """Refuse a relative generation number other than (0) or (-n)."""
    if isinstance(relative, bool) or not isinstance(relative, int) or relative > 0:
        raise ValueError(f"a relative generation is 0 or negative, got {relative!r}")


def ResolveGeneration(context: JobContext, base: GdgBase, relative: int = 0) -> Generation:
    """Resolve base(relative), (0) the newest, counting PUBLISHED generations only.

    Raises LostGenerationError when the base has a LOST generation in the OPEN cycle, because
    an older generation would silently stand in for it, and MissingGenerationError when no
    PUBLISHED generation is that old.
    """
    CheckRelative(relative)
    cycleId, lostGenerations = LostGenerationsOfOpenCycle(context, base.name)
    if lostGenerations:
        raise BuildLostGenerationError(cycleId, lostGenerations)
    rows = SelectRows(context, PublishedNewestFirstStatement(base).offset(-relative).limit(1))
    if not rows:
        missingMessage = MISSING_GENERATION_TEMPLATE.format(base=base.name, relative=relative)
        raise MissingGenerationError(missingMessage)
    return GenerationFromRow(context, rows[0])


def DecodeGenerationLine(line: bytes, generation: Generation, recordNumber: int) -> str:
    """Return one record of a generation file, refusing a line the base cannot hold."""
    lrecl = GDG_BASES[generation.baseName].lrecl
    where = f"{generation.path} record {recordNumber}"
    if not line.endswith(RECORD_TERMINATOR):
        raise GenerationRecordError(f"{where} is not terminated by a line feed")
    record = line[: -len(RECORD_TERMINATOR)].decode(GDG_FILE_ENCODING)
    if len(record) != lrecl or "\r" in record:
        raise GenerationRecordError(f"{where} is not {lrecl} characters free of CR")
    return record


def ReadGeneration(generation: Generation) -> Iterator[str]:
    """Yield the records of a generation file in file order, terminators removed.

    The single reader of GDG files: every line must be exactly the base's LRECL characters
    followed by "\\n", else GenerationRecordError.
    """
    with generation.path.open("rb") as handle:
        for recordNumber, line in enumerate(handle, start=1):
            yield DecodeGenerationLine(line, generation, recordNumber)
