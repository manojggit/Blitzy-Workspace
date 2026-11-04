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
  gone LOST and deletes orphan .part files, then the work directories (*.work, such as the
  spill directory of COMBTRAN's sort) that a killed producer left beside them. It then applies
  retention to every base and deletes the leftover files of ROLLED_OFF SCRATCH generations, so a
  crash between publication and retention, or between ROLLED_OFF and the unlink, is completed by
  the next command. A LOST row blocks the OPEN cycle until seed-all or run-all --new-cycle
  abandons it (run_ledger.AbandonOpenCycle).
- Abnormal disposition: an exception inside NewGeneration deletes the .part and catalogs
  nothing, as DISP=(NEW,CATLG,DELETE) does.
- Retention: LIMIT 5, applied after every publication and again by every ReconcileCatalog. The
  oldest PUBLISHED generation beyond it becomes ROLLED_OFF, committed before SCRATCH bases delete
  its file; NOSCRATCH TRANSACT.BKUP keeps it.
- Containment: every archive operation stays beneath the configured CARDDEMO_OUTPUT_DIR. That
  root is opened as given, following links, so it may be absolute or linked; the gdg directory,
  each base directory and each work directory are opened relative to their parent's descriptor
  without following a link (OpenArchiveDirectory). One that is a symbolic link or not a
  directory raises ManagedDirectoryError, an OSError the file-error mappings turn into exit 12,
  and nothing is read, written or deleted through it. The held descriptor then serves every
  creation, read, rename, listing and deletion in that directory, files opened with O_NOFOLLOW,
  so a directory swapped for a link once opened changes nothing this module reaches.

Records arrive already encoded by record_layouts.EncodeRecord; this module only checks their
length and writes them, one per line ended by "\\n". Base names come from GDG_BASES only, never
from user input. The batch window admits one batch process, so generation numbers need no lock.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import logging
import operator
import os
import re
import secrets
import shutil
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from app.core.logging import PrintableText
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
# The suffix of a producer's work directory in its base directory, beside its .part file, such
# as the spill directory of COMBTRAN's sort. One lives only as long as the command that made it.
WORK_DIRECTORY_SUFFIX = ".work"
GENERATION_FILE_TEMPLATE = "G{generation:04d}V00.dat"
# The final file names GENERATION_FILE_TEMPLATE produces; a match is kept only when it
# round-trips through the template, so G01V00.dat or G00001V00.dat names no generation.
GENERATION_FILE_PATTERN = re.compile(r"G(?P<generation>\d+)V00\.dat", re.ASCII)
# The configured output root is opened as given, following links, since an operator may place it
# in or under a linked directory. Every managed directory below it (GDG_SUBDIRECTORY, a base, a
# producer's work directory) is opened relative to its parent's descriptor without following a
# link, and every file relative to its directory's descriptor, never through a link at its name.
ROOT_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
MANAGED_DIRECTORY_FLAGS = ROOT_DIRECTORY_FLAGS | os.O_NOFOLLOW
READ_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
READ_FILE_MODE = "rb"
NEW_FILE_MODE = "wb"
NEW_FILE_PERMISSIONS = 0o666
MANAGED_DIRECTORY_PERMISSIONS = 0o777
WORK_DIRECTORY_PERMISSIONS = 0o700
# O_NOFOLLOW with O_DIRECTORY refuses a symbolic link with ENOTDIR on Linux and ELOOP elsewhere;
# ENOTDIR also refuses a file standing where a managed directory belongs.
REFUSED_DIRECTORY_ERRNOS = frozenset({errno.ELOOP, errno.ENOTDIR})
# Names that would leave the managed chain instead of descending it, refused with EINVAL.
NON_DESCENDING_NAMES = frozenset({"", os.curdir, os.pardir})
REFUSED_DIRECTORY_TEXT = (
    "managed archive directory is a symbolic link or not a directory; it is never followed"
)
# A work directory or run file name: its prefix, UNIQUE_NAME_BYTES random bytes as hex, and its
# suffix, created exclusively and retried with a new name up to UNIQUE_NAME_ATTEMPTS times.
UNIQUE_NAME_BYTES = 8
UNIQUE_NAME_ATTEMPTS = 100
UNIQUE_NAMES_EXHAUSTED_TEXT = "every random name tried is already taken"
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
ORPHAN_WORK_DIRECTORY_DELETED_LOG = "ORPHAN WORK DIRECTORY %s DELETED"
LEFTOVER_DELETED_LOG = "ROLLED-OFF GENERATION %s: LEFTOVER FILE %s DELETED"
# A directory fsync refusal names its class and errno symbol, never the OS error text.
DIRECTORY_FSYNC_LOG = "DIRECTORY FSYNC REFUSED FOR %s: %s ERRNO %s"
UNKNOWN_CODE = "UNKNOWN"
RECONCILE_LOG = (
    "GDG CATALOG RECONCILED: %d PUBLISHED, %d LOST, %d ORPHAN .part FILES DELETED,"
    " %d ORPHAN WORK DIRECTORIES DELETED"
)
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
    """What ReconcileCatalog did: rows published, rows found LOST, orphan .part files deleted.

    workDirectoriesDeleted counts the orphan work directories deleted; a reconcile that found
    none, the usual case, leaves it 0.
    """

    published: int
    lost: int
    orphansDeleted: int
    workDirectoriesDeleted: int = 0


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


class ManagedDirectoryError(OSError):
    """A managed archive directory that is a symbolic link or not a directory, refused unopened.

    Its errno is the kernel's (ENOTDIR or ELOOP; EINVAL for a name such as ".." that would leave
    the chain) and its filename the full path of the refused directory. Being an OSError, it
    reaches exit status 12 through every file-error mapping: the CLI's FILE ERROR line, the
    chain's and each job's. Nothing is created, read or deleted through the refused directory.
    """


@contextlib.contextmanager
def NamingFileErrors(path: Path) -> Iterator[None]:
    """Name path in full in any OSError the block raises.

    A call relative to a directory descriptor names only the entry, or the descriptor; the FILE
    ERROR lines show the full path instead, the one an operator repairs.
    """
    try:
        yield
    except OSError as error:
        error.filename = str(path)
        raise


def UniqueNames(prefix: str, suffix: str) -> Iterator[str]:
    """Yield UNIQUE_NAME_ATTEMPTS random names, each prefix, random hex digits and suffix."""
    for _ in range(UNIQUE_NAME_ATTEMPTS):
        yield f"{prefix}{secrets.token_hex(UNIQUE_NAME_BYTES)}{suffix}"


@dataclass(frozen=True)
class ManagedDirectory:
    """An open archive directory: the descriptor every operation in it is relative to, and the
    full path its diagnostics name.

    Files are opened with O_NOFOLLOW and subdirectories with O_NOFOLLOW and O_DIRECTORY; names
    are created, renamed, listed and deleted relative to the descriptor. Replacing the
    directory's path with a symbolic link after it was opened therefore changes nothing reached
    through it, and no link inside it is ever followed. Close() releases the descriptor.
    """

    descriptor: int
    path: Path

    def EntryPath(self, name: str) -> Path:
        """Return the full path of an entry, as diagnostics show it."""
        return self.path / name

    def OpenFile(self, name: str, flags: int, mode: str) -> BinaryIO:
        """Open an entry with flags, never through a link at its name, as a mode file object."""
        with NamingFileErrors(self.EntryPath(name)):
            fileDescriptor = os.open(name, flags, NEW_FILE_PERMISSIONS, dir_fd=self.descriptor)
            try:
                return os.fdopen(fileDescriptor, mode)
            except OSError:
                os.close(fileDescriptor)
                raise

    def ReadFile(self, name: str) -> BinaryIO:
        """Open an entry for binary reading; FileNotFoundError when it is absent."""
        return self.OpenFile(name, READ_FILE_FLAGS, READ_FILE_MODE)

    def CreateFile(self, name: str) -> BinaryIO:
        """Create a new entry for binary writing; FileExistsError when the name is taken."""
        return self.OpenFile(name, NEW_FILE_FLAGS, NEW_FILE_MODE)

    def CreateUniqueFile(self, prefix: str, suffix: str) -> tuple[str, BinaryIO]:
        """Create a new entry with a random name (UniqueNames); return its name and file."""
        for name in UniqueNames(prefix, suffix):
            with contextlib.suppress(FileExistsError):
                return name, self.CreateFile(name)
        raise FileExistsError(
            errno.EEXIST, UNIQUE_NAMES_EXHAUSTED_TEXT, str(self.EntryPath(prefix + suffix))
        )

    def Unlink(self, name: str) -> None:
        """Delete an entry, a symbolic link itself rather than its target; absent is no error."""
        with contextlib.suppress(FileNotFoundError), NamingFileErrors(self.EntryPath(name)):
            os.unlink(name, dir_fd=self.descriptor)

    def Replace(self, sourceName: str, targetName: str) -> None:
        """Rename one entry over another within the directory, then make the rename durable."""
        with NamingFileErrors(self.EntryPath(sourceName)):
            os.replace(
                sourceName, targetName, src_dir_fd=self.descriptor, dst_dir_fd=self.descriptor
            )
        FsyncDirectory(self)

    def Entries(self) -> list[os.DirEntry[str]]:
        """Return the directory's entries in name order.

        Call their is_file and is_dir with follow_symlinks=False, and before Close(): an entry
        without a known type is examined relative to this descriptor.
        """
        with NamingFileErrors(self.path), os.scandir(self.descriptor) as entries:
            return sorted(entries, key=operator.attrgetter("name"))

    def OpenDirectory(self, name: str) -> ManagedDirectory:
        """Open a subdirectory without following a link at its name; the caller closes it.

        A symbolic link, a non-directory or a name that would leave the chain raises
        ManagedDirectoryError; a missing one raises FileNotFoundError naming its full path.
        """
        childPath = self.EntryPath(name)
        shownPath = str(childPath)
        if name in NON_DESCENDING_NAMES:
            raise ManagedDirectoryError(errno.EINVAL, REFUSED_DIRECTORY_TEXT, shownPath)
        try:
            childDescriptor = os.open(name, MANAGED_DIRECTORY_FLAGS, dir_fd=self.descriptor)
        except OSError as error:
            if error.errno in REFUSED_DIRECTORY_ERRNOS:
                refusal = ManagedDirectoryError(error.errno, REFUSED_DIRECTORY_TEXT, shownPath)
                raise refusal from error
            error.filename = shownPath
            raise
        return ManagedDirectory(childDescriptor, childPath)

    def MakeDirectory(self, name: str, permissions: int) -> None:
        """Create a subdirectory; FileExistsError when the name is taken, by a link included."""
        with NamingFileErrors(self.EntryPath(name)):
            os.mkdir(name, permissions, dir_fd=self.descriptor)

    def OpenSubdirectory(self, name: str, create: bool) -> ManagedDirectory:
        """Open a subdirectory as OpenDirectory does, with create making it first when absent."""
        if create:
            with contextlib.suppress(FileExistsError):
                self.MakeDirectory(name, MANAGED_DIRECTORY_PERMISSIONS)
        return self.OpenDirectory(name)

    def MakeUniqueDirectory(self, prefix: str, suffix: str) -> str:
        """Create a new private (0o700) subdirectory with a random name; return its name."""
        for name in UniqueNames(prefix, suffix):
            with contextlib.suppress(FileExistsError):
                self.MakeDirectory(name, WORK_DIRECTORY_PERMISSIONS)
                return name
        raise FileExistsError(
            errno.EEXIST, UNIQUE_NAMES_EXHAUSTED_TEXT, str(self.EntryPath(prefix + suffix))
        )

    def RemoveTree(self, name: str) -> None:
        """Delete a subdirectory with its contents; a symbolic link there raises OSError.

        shutil.rmtree given dir_fd works relative to this descriptor and, where
        shutil.rmtree.avoids_symlink_attacks holds (Linux, macOS), follows no link inside.
        """
        with NamingFileErrors(self.EntryPath(name)):
            shutil.rmtree(name, dir_fd=self.descriptor)

    def Close(self) -> None:
        """Release the descriptor."""
        os.close(self.descriptor)


def OpenRootDirectory(root: Path, create: bool) -> ManagedDirectory:
    """Open the configured output root as given, following links; create makes it when absent."""
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return ManagedDirectory(os.open(root, ROOT_DIRECTORY_FLAGS), root)


def OpenArchiveDirectory(directoryPath: Path, create: bool) -> ManagedDirectory:
    """Open an archive base directory, or an archive file's directory; the caller closes it.

    The chain comes from the path's structure. A base directory is <root>/gdg/<BASE>, so
    directoryPath.parent.parent, the configured CARDDEMO_OUTPUT_DIR, is opened as given,
    following links, then directoryPath.parent.name and directoryPath.name are each opened
    relative to the one before, never following a link (ManagedDirectory.OpenDirectory). A
    managed directory that is a symbolic link, or not a directory, raises ManagedDirectoryError,
    so nothing outside the archive is reached through it. With create, each missing directory
    is made first; without it, a missing one raises FileNotFoundError.
    """
    directory = OpenRootDirectory(directoryPath.parent.parent, create)
    for name in (directoryPath.parent.name, directoryPath.name):
        try:
            child = directory.OpenSubdirectory(name, create)
        finally:
            directory.Close()
        directory = child
    return directory


@contextlib.contextmanager
def ArchiveDirectory(directoryPath: Path, create: bool) -> Iterator[ManagedDirectory]:
    """Hold an archive directory open for the block (OpenArchiveDirectory), then close it."""
    directory = OpenArchiveDirectory(directoryPath, create)
    try:
        yield directory
    finally:
        directory.Close()


@contextlib.contextmanager
def ExistingArchiveDirectory(directoryPath: Path) -> Iterator[ManagedDirectory | None]:
    """Hold an existing archive directory open for the block; None when it or a parent is absent.

    Nothing is created: recovery, publication, retention and listing find nothing to do in a
    directory that does not exist, as a listing of it would.
    """
    try:
        directory = OpenArchiveDirectory(directoryPath, create=False)
    except FileNotFoundError:
        directory = None
    try:
        yield directory
    finally:
        if directory is not None:
            directory.Close()


@contextlib.contextmanager
def WorkDirectory(baseDirectory: Path, prefix: str) -> Iterator[ManagedDirectory]:
    """Hold a new private work directory of an archive base directory, then delete it.

    Named prefix, random hex digits and WORK_DIRECTORY_SUFFIX, it is made with mode 0o700
    relative to the descriptor of baseDirectory (OpenArchiveDirectory, creating what is
    missing) and reopened without following a link. The block creates, reads and deletes its
    files through the yielded ManagedDirectory. On exit, whatever happened, it is removed with
    its contents relative to the same base descriptor; ReconcileCatalog removes a killed
    process's.
    """
    with contextlib.ExitStack() as stack:
        base = stack.enter_context(ArchiveDirectory(baseDirectory, create=True))
        workName = base.MakeUniqueDirectory(prefix, WORK_DIRECTORY_SUFFIX)
        stack.callback(base.RemoveTree, workName)
        workDirectory = base.OpenDirectory(workName)
        stack.callback(workDirectory.Close)
        yield workDirectory


def OpenArchiveFile(path: Path) -> BinaryIO:
    """Open an archive file for binary reading, through no symbolic link.

    Its directory is opened as OpenArchiveDirectory describes, and the file relative to it with
    O_NOFOLLOW; a missing directory or file raises FileNotFoundError.
    """
    with ArchiveDirectory(path.parent, create=False) as directory:
        return directory.ReadFile(path.name)


def GenerationName(baseName: str, generation: int) -> str:
    """Return the dataset-style name of a generation, for example SYSTRAN.G0003V00."""
    return GENERATION_NAME_TEMPLATE.format(base=baseName, generation=generation)


def BaseDirectoryPath(context: JobContext, base: GdgBase) -> Path:
    """Return the path of the base's directory, GdgDir()/<BASE>, without opening it."""
    return context.settings.GdgDir() / base.name


def BaseDirectory(context: JobContext, base: GdgBase) -> Path:
    """Return the base's directory under GdgDir(), creating it when absent.

    It is created and checked through OpenArchiveDirectory, so a gdg or base directory that is a
    symbolic link raises ManagedDirectoryError instead of being followed.
    """
    directoryPath = BaseDirectoryPath(context, base)
    with ArchiveDirectory(directoryPath, create=True):
        return directoryPath


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


def PartName(generation: int) -> str:
    """Return the name of a generation's .part file, for example G0001V00.dat.part."""
    return GENERATION_FILE_TEMPLATE.format(generation=generation) + PART_SUFFIX


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
    """Writes the records of one new generation to its open .part file, hashing every byte.

    directory is the held base directory the .part file was created in, relative to which
    DiscardPart deletes it; partPath is its full path, for diagnostics.
    """

    def __init__(
        self, handle: BinaryIO, directory: ManagedDirectory, base: GdgBase, generation: int
    ) -> None:
        self.handle = handle
        self.directory = directory
        self.base = base
        self.generation = generation
        self.partName = PartName(generation)
        self.partPath = directory.EntryPath(self.partName)
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
    directory: ManagedDirectory, base: GdgBase, generation: int
) -> GenerationWriter:
    """Create the generation's .part file in its held base directory, replacing a stale one.

    The stale name is unlinked first, a link included, and the file is created exclusively
    without following a link, so the records land in the base directory and nowhere else.
    """
    partName = PartName(generation)
    directory.Unlink(partName)
    return GenerationWriter(directory.CreateFile(partName), directory, base, generation)


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
        writer.directory.Unlink(writer.partName)


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
    transaction and the generation is published, and retention applied, at once. The base
    directory is created and held open through the block (OpenArchiveDirectory); a gdg or base
    directory that is a symbolic link raises ManagedDirectoryError before anything is written.
    """
    RequireLedgered(context)
    generation = AllocateGeneration(context, base, connection)
    with ArchiveDirectory(BaseDirectoryPath(context, base), create=True) as directory:
        writer = OpenGenerationWriter(directory, base, generation)
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


def HandleSha256(handle: BinaryIO) -> str:
    """Return the SHA-256 hex digest of an open file's remaining bytes, read in 64 KiB chunks."""
    digest = hashlib.sha256()
    while chunk := handle.read(HASH_CHUNK_SIZE):
        digest.update(chunk)
    return digest.hexdigest()


def FileSha256(path: Path) -> str:
    """Return the SHA-256 hex digest of an archive file, opened as OpenArchiveFile does."""
    with OpenArchiveFile(path) as handle:
        return HandleSha256(handle)


def HashMatches(directory: ManagedDirectory, name: str, expectedSha256: str) -> bool:
    """Return whether the directory's entry exists with the expected SHA-256."""
    try:
        with directory.ReadFile(name) as handle:
            return HandleSha256(handle) == expectedSha256
    except FileNotFoundError:
        return False


def LogFsyncRefused(directory: Path, error: OSError) -> None:
    """Log a directory fsync refusal at DEBUG by escaped path, error class and errno symbol."""
    errnoName = errno.errorcode.get(error.errno, UNKNOWN_CODE) if error.errno else UNKNOWN_CODE
    LOGGER.debug(DIRECTORY_FSYNC_LOG, PrintableText(directory), type(error).__name__, errnoName)


def FsyncDirectory(directory: ManagedDirectory) -> None:
    """Make a rename in the held directory durable; a platform refusing it is logged at DEBUG."""
    try:
        os.fsync(directory.descriptor)
    except OSError as error:
        LogFsyncRefused(directory.path, error)


def PlaceGenerationFile(finalPath: Path, expectedSha256: str) -> str:
    """Put a COMMITTED generation's file in place; return PUBLISHED, or LOST when it is gone.

    Both hashes and the rename act on one held descriptor of the base directory, so the .part
    file found matching is the one renamed. A missing base directory holds no file: LOST.
    """
    with ExistingArchiveDirectory(finalPath.parent) as directory:
        if directory is None:
            return GDG_LOST
        if HashMatches(directory, finalPath.name, expectedSha256):
            return GDG_PUBLISHED
        partName = PartPath(finalPath).name
        if not HashMatches(directory, partName, expectedSha256):
            return GDG_LOST
        directory.Replace(partName, finalPath.name)
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
        LOGGER.error(LOST_LOG, generationName, PrintableText(finalPath), row.sha256)
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
    DeleteArchiveFile(ResolveFilePath(context, row.file_path))
    return FILE_SCRATCHED


def DeleteArchiveFile(path: Path) -> None:
    """Delete an archive file, a link at its name rather than its target; absent is no error.

    The deletion is relative to its directory's descriptor (ExistingArchiveDirectory).
    """
    with ExistingArchiveDirectory(path.parent) as directory:
        if directory is not None:
            directory.Unlink(path.name)


def RollOff(context: JobContext, base: GdgBase, limit: int = GDG_LIMIT) -> int:
    """Roll off every PUBLISHED generation of base beyond the newest limit; return the count.

    The rows become ROLLED_OFF, so no reference resolves them, and commit before any SCRATCH
    file is deleted: a crash or unlink error in between leaves an unreferenced leftover file,
    which the next ReconcileCatalog deletes, never a resolvable generation without its file.
    TRANSACT.BKUP (NOSCRATCH) keeps its files.
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


def DeleteBaseOrphanParts(directoryPath: Path) -> int:
    """Delete the .part entries of one base directory, each named in a warning; return a count.

    A symbolic link named like a .part is deleted itself, never its target.
    """
    with ExistingArchiveDirectory(directoryPath) as directory:
        if directory is None:
            return 0
        entries = directory.Entries()
        partNames = [entry.name for entry in entries if entry.name.endswith(PART_SUFFIX)]
        for partName in partNames:
            directory.Unlink(partName)
            LOGGER.warning(ORPHAN_DELETED_LOG, PrintableText(directory.EntryPath(partName)))
    return len(partNames)


def DeleteOrphanParts(context: JobContext) -> int:
    """Delete every .part file left under the base directories; return how many there were."""
    return sum(
        DeleteBaseOrphanParts(BaseDirectoryPath(context, base)) for base in GDG_BASES.values()
    )


def IsWorkDirectory(entry: os.DirEntry[str]) -> bool:
    """Return whether a base directory entry is a real directory with the work suffix."""
    return entry.name.endswith(WORK_DIRECTORY_SUFFIX) and entry.is_dir(follow_symlinks=False)


def DeleteBaseOrphanWorkDirectories(directoryPath: Path) -> int:
    """Delete one base directory's work directories, each named in a warning; return how many."""
    with ExistingArchiveDirectory(directoryPath) as directory:
        if directory is None:
            return 0
        workNames = [entry.name for entry in directory.Entries() if IsWorkDirectory(entry)]
        for workName in workNames:
            directory.RemoveTree(workName)
            workPath = directory.EntryPath(workName)
            LOGGER.warning(ORPHAN_WORK_DIRECTORY_DELETED_LOG, PrintableText(workPath))
    return len(workNames)


def DeleteOrphanWorkDirectories(context: JobContext) -> int:
    """Delete every work directory left under the base directories; return how many there were.

    A work directory lives only as long as the command that made it, and the batch window admits
    one batch command, so any found at the start of a command, or after its failed step has
    closed its own, belongs to a killed process. It is deleted with its contents, relative to
    the base directory's descriptor. A symbolic link or a file with the suffix is no producer's
    work directory and is left alone, its target untouched.
    """
    return sum(
        DeleteBaseOrphanWorkDirectories(BaseDirectoryPath(context, base))
        for base in GDG_BASES.values()
    )


def GenerationOfFileName(fileName: str) -> int | None:
    """Return the generation a final file name holds, or None for any other name."""
    match = GENERATION_FILE_PATTERN.fullmatch(fileName)
    if match is None:
        return None
    generation = int(match["generation"])
    if GENERATION_FILE_TEMPLATE.format(generation=generation) != fileName:
        return None
    return generation


def FinalFileGenerations(context: JobContext, base: GdgBase) -> list[int]:
    """Return the generation numbers of the final files in the base's directory, ascending.

    Only regular files count; a symbolic link with a generation's name is never followed.
    """
    with ExistingArchiveDirectory(BaseDirectoryPath(context, base)) as directory:
        if directory is None:
            return []
        generations = [
            GenerationOfFileName(entry.name)
            for entry in directory.Entries()
            if entry.is_file(follow_symlinks=False)
        ]
    return sorted(generation for generation in generations if generation is not None)


def RolledOffRowsStatement(base: GdgBase, generations: list[int]) -> Select:
    """Return the query of the base's ROLLED_OFF rows among the given generation numbers."""
    return (
        select(GDG_GENERATIONS)
        .where(
            GDG_GENERATIONS.c.base_name == base.name,
            GDG_GENERATIONS.c.generation.in_(generations),
            GDG_GENERATIONS.c.state == GDG_ROLLED_OFF,
        )
        .order_by(GDG_GENERATIONS.c.generation)
    )


def DeleteLeftoverFiles(context: JobContext, base: GdgBase) -> int:
    """Delete the files left behind by ROLLED_OFF generations of a SCRATCH base; return the count.

    RollOff commits ROLLED_OFF before it deletes a file, so a crash or unlink error leaves the
    file. The final file names present in the base directory drive the lookup: only the rows of
    those generation numbers are read, by primary key, never by scanning the catalog history.
    A file with no row, a file whose row is in another state (such as the corrupt final file of
    a LOST or ABANDONED generation) and any file with another name are left alone; NOSCRATCH
    bases keep theirs.
    """
    generations = FinalFileGenerations(context, base) if base.scratch else []
    if not generations:
        return 0
    rolledOffRows = SelectRows(context, RolledOffRowsStatement(base, generations))
    for row in rolledOffRows:
        ScratchFile(context, base, row)
        generationName = GenerationName(row.base_name, row.generation)
        leftoverPath = ResolveFilePath(context, row.file_path)
        LOGGER.warning(LEFTOVER_DELETED_LOG, generationName, PrintableText(leftoverPath))
    return len(rolledOffRows)


def DeleteRolledOffLeftovers(context: JobContext) -> int:
    """Delete the leftover files of ROLLED_OFF generations of every SCRATCH base; return a count."""
    return sum(DeleteLeftoverFiles(context, base) for base in GDG_BASES.values())


def ReconcileCatalog(context: JobContext) -> ReconcileSummary:
    """Complete interrupted publications, retention and SCRATCH deletions; delete orphan parts.

    batch/cli.py runs it at the start of every command, under the batch window and after
    run_ledger.ClassifyInterruptedRuns; batch_chain also runs it after a failed step. Once every
    COMMITTED row is PUBLISHED or LOST, any .part left is an orphan of a crash before commit, or
    the corrupt file of a LOST row, and any work directory, such as the spill directory of a
    killed COMBTRAN's sort, is an orphan too; both are deleted. Retention then runs for every
    base, whether or not it gained a generation here, so the extra generation a crash between
    publication and RollOff left resolvable rolls off before any command resolves it. Last, the
    files of ROLLED_OFF SCRATCH generations that a crash or unlink error left behind are
    deleted. Each stage finds nothing to do when the catalog is already consistent, so a second
    call changes nothing. Every stage works relative to held base directory descriptors: a gdg
    or base directory that is a symbolic link raises ManagedDirectoryError before anything is
    deleted through it, and a missing one has nothing to delete.
    """
    committedRows = SelectRows(context, CommittedRowsStatement())
    _, lostCount = PublishRows(context, committedRows)
    orphansDeleted = DeleteOrphanParts(context)
    workDirectoriesDeleted = DeleteOrphanWorkDirectories(context)
    RollOffBases(context, GDG_BASES.keys())
    DeleteRolledOffLeftovers(context)
    summary = ReconcileSummary(
        published=len(committedRows) - lostCount,
        lost=lostCount,
        orphansDeleted=orphansDeleted,
        workDirectoriesDeleted=workDirectoriesDeleted,
    )
    LOGGER.info(
        RECONCILE_LOG,
        summary.published,
        summary.lost,
        summary.orphansDeleted,
        summary.workDirectoriesDeleted,
    )
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
    where = f"{PrintableText(generation.path)} record {recordNumber}"
    if not line.endswith(RECORD_TERMINATOR):
        raise GenerationRecordError(f"{where} is not terminated by a line feed")
    record = line[: -len(RECORD_TERMINATOR)].decode(GDG_FILE_ENCODING)
    if len(record) != lrecl or "\r" in record:
        raise GenerationRecordError(f"{where} is not {lrecl} characters free of CR")
    return record


def ReadGeneration(generation: Generation) -> Iterator[str]:
    """Yield the records of a generation file in file order, terminators removed.

    The single reader of GDG files: every line must be exactly the base's LRECL characters
    followed by "\\n", else GenerationRecordError. The file is opened as OpenArchiveFile does,
    through no symbolic link at its name or at a managed directory above it.
    """
    with OpenArchiveFile(generation.path) as handle:
        for recordNumber, line in enumerate(handle, start=1):
            yield DecodeGenerationLine(line, generation, recordNumber)
