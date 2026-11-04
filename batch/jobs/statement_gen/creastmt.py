"""Replaces: CREASTMT (job steps DELDEF01, STEP010, STEP020, STEP030, STEP040).

Chain step 17, files only: it writes STATEMNT.PS (80-column records) and STATEMNT.HTML
(100-column records) under CARDDEMO_OUTPUT_DIR/statements/, writes no table, and exits 0, or 16
when CBSTM03A abends. The TRXFL work file of DELDEF01, STEP010 and STEP020 is one ordered query,
cbstm03b.ReadStatementTransactions. STEP040 is the cbstm03a builder, whose statements stream
into STATEMNT.PS.part and STATEMNT.HTML.part beside the targets. STEP030's delete of the
previous statements becomes one publication of both files once both are complete: each previous
file is hard-linked to a .prev backup, the STATEMNT.PUBLISHING marker is created, both .part
files are renamed over their targets and the directory is synced. Removing the marker is the
commit point. A failure before it, in a read, the build, a write, an fsync or either rename,
leaves the previous statements as they were and no work file; should the rollback fail too,
the marker stays for the next run. A run killed during its publication is rolled back by the
next run before that run reads, and the backups and .part files a run killed at any other
point left are deleted then too. A failure after the commit point, such as the ledger finish,
exits 16 with the new statements in place, and the retry writes them again from the same
tables. The CLI holds the batch window around the command, so this module takes no lock; it
checks that the window still holds (RequireWindow) before its recovery and its publication.
"""

import errno
import logging
import os
import re
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from app.core.logging import PrintableText
from jinja2 import TemplateError
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from batch.jobs.statement_gen import cbstm03a, cbstm03b
from batch.orchestration import run_ledger
from batch.orchestration.batch_window import RequireWindow
from batch.orchestration.job_context import EXIT_ABEND, EXIT_OK, JobContext, JobFailure, JobResult

JOB_NAME = "statement-gen"
JCL_NAME = "CREASTMT"
PROGRAM_NAME = "CBSTM03A"
STEP_NAME = "STEP040"

STATEMENT_FILE_NAME = "STATEMNT.PS"
HTML_FILE_NAME = "STATEMNT.HTML"
# STATEMNT.PS is strict Latin-1, one byte per column. STATEMNT.HTML is UTF-8, as its HTML-L04 meta
# charset line declares (cbl/CBSTM03A.CBL:153), so its records are 100 characters. Both files
# carry the same field text and STATEMNT.PS is written first, so text Latin-1 refuses never
# reaches STATEMNT.HTML.
STATEMENT_FILE_ENCODING = "latin-1"
HTML_FILE_ENCODING = "utf-8"
LINE_TERMINATOR = "\n"
TEMPORARY_SUFFIX = ".part"
# The publication work files: each previous statement file's hard-linked backup, and the
# marker that exists from the completed backups to the commit point.
BACKUP_SUFFIX = ".prev"
PUBLICATION_MARKER_NAME = "STATEMNT.PUBLISHING"

# The STEP040 DD names and what stands in for each dataset, for the TIOT log line.
INPUT_DATASETS = (
    ("TRNXFILE", "transactions.transactions"),
    ("XREFFILE", "xref.card_xref"),
    ("ACCTFILE", "accounts.accounts"),
    ("CUSTFILE", "customers.customers"),
)
OUTPUT_DATASETS = (("STMTFILE", STATEMENT_FILE_NAME), ("HTMLFILE", HTML_FILE_NAME))

# Every failure that ends CBSTM03A with 9999-ABEND-PROGRAM, and the step with exit 16: the
# statement abends, a database error, a file error, a picture, encoding or record-layout error
# (ValueError) and a statement template error.
ABEND_ERRORS = (cbstm03a.StatementAbendError, DBAPIError, OSError, ValueError, TemplateError)
# The abend diagnostics DescribeAbend gives the log and JobFailure for the failures whose own
# text can quote row values: each names the exception classes, the SQLSTATE or errno symbol and
# fixed text, never the error's message.
ABEND_DATABASE_TEMPLATE = "DATABASE ERROR {errorClass} ({driverClass}) SQLSTATE {sqlState}"
ABEND_FILE_TEMPLATE = "FILE ERROR {errorClass} ERRNO {errnoName}"
ABEND_RENDERING_TEMPLATE = "STATEMENT TEMPLATE ERROR {errorClass}"
ABEND_RECORD_TEMPLATE = "PICTURE, ENCODING OR RECORD LAYOUT ERROR {errorClass}"
# What a diagnostic shows for a missing driver error, SQLSTATE or errno symbol. A SQLSTATE is
# five digits or upper-case letters, so a pgcode of any other form is shown as missing too.
ABEND_UNKNOWN_CODE = "UNKNOWN"
ABEND_SQLSTATE_PATTERN = re.compile(r"[0-9A-Z]{5}")

START_LOG = "START OF EXECUTION OF JOB %s"
DATASETS_LOG = "Running JCL : %s Step %s inputs %s; outputs %s"
ROLLBACK_LOG = "ROLLING BACK THE UNCOMMITTED PUBLICATION MARKED BY %s"
RESTORED_FILE_LOG = "RESTORED %s FROM %s"
UNPUBLISHED_FILE_LOG = "REMOVED %s, WHICH HAD NO PREVIOUS FILE"
STALE_FILE_LOG = "REMOVED %s LEFT BY AN INTERRUPTED RUN"
ABEND_LOG = "ABENDING PROGRAM %s: %s"
END_LOG = "END OF EXECUTION OF JOB %s: %d STATEMENTS, %d TRANSACTIONS"

LOGGER = logging.getLogger(__name__)


@dataclass
class StatementCounts:
    """What the run records: the statements written and their ST-LINE14 transaction lines."""

    statements: int = 0
    transactions: int = 0

    def Add(self, records: cbstm03a.StatementRecords) -> None:
        """Count one written statement and its transaction lines."""
        self.statements += 1
        self.transactions += records.transactionCount

    def AsLedgerCounts(self) -> dict[str, int]:
        """Return the counts as batch.job_runs.counts."""
        return {"statements": self.statements, "transactions": self.transactions}


def RunJob(context: JobContext) -> JobResult:
    """Run CREASTMT: build every statement and write STATEMNT.PS and STATEMNT.HTML.

    Returns EXIT_OK with the statement and transaction-line counts, after recording the run
    COMPLETED when the chain started it. Raises JobFailure with EXIT_ABEND when CBSTM03A
    abends: the table capacity exceeded, an empty TRNXFILE, a missing customer or account, or a
    database, file, picture, encoding or template error. An abend before the publication's
    commit point leaves the previous statements as they were, or, when the rollback fails too,
    the marker that makes the next run restore them. One after it, in deleting the backups or
    in the ledger finish, leaves the new statements in place for the retry to write again.
    """
    statementsDir = context.settings.StatementsDir()
    LOGGER.info(START_LOG, JCL_NAME)
    LOGGER.info(DATASETS_LOG, JCL_NAME, STEP_NAME, DescribeInputs(), DescribeOutputs(statementsDir))
    try:
        counts = WriteStatements(context, statementsDir)
        result = JobResult(EXIT_OK, counts.AsLedgerCounts())
        # Files-only step: the only database write is the ledger finish, in its own transaction
        # once both files are in place, as run_ledger specifies for files-only steps.
        run_ledger.FinishIfRunning(context, result)
    except ABEND_ERRORS as error:
        abendDiagnostic = DescribeAbend(error)
        LOGGER.error(ABEND_LOG, PROGRAM_NAME, abendDiagnostic)
        raise JobFailure(EXIT_ABEND, ABEND_LOG % (PROGRAM_NAME, abendDiagnostic)) from error
    LOGGER.info(END_LOG, JCL_NAME, counts.statements, counts.transactions)
    return result


def DescribeAbend(error: Exception) -> str:
    """Return the diagnostic of an ABEND_ERRORS failure, safe to log and to print.

    A StatementAbendError keeps its text, the DISPLAY text of a failed read or a table-capacity
    refusal, built only from module literals, DD names, return codes and table positions, never
    from a row value (cbstm03a.ReadErrorMessage and AppendTableEntry). The text of any other
    failure can quote row values: a driver message carries the server's DETAIL and CONTEXT
    lines even with hide_parameters, and a file, value or template error can name what it
    refused. Those failures are described by exception class, SQLSTATE or errno symbol, and
    fixed text only.
    """
    errorClass = type(error).__name__
    if isinstance(error, cbstm03a.StatementAbendError):
        return str(error)
    if isinstance(error, DBAPIError):
        return DescribeDatabaseAbend(error)
    if isinstance(error, OSError):
        return ABEND_FILE_TEMPLATE.format(errorClass=errorClass, errnoName=ErrnoName(error))
    if isinstance(error, TemplateError):
        return ABEND_RENDERING_TEMPLATE.format(errorClass=errorClass)
    return ABEND_RECORD_TEMPLATE.format(errorClass=errorClass)


def DescribeDatabaseAbend(error: DBAPIError) -> str:
    """Return a database abend's wrapper and driver error classes and its SQLSTATE.

    The SQLSTATE is the driver error's pgcode, which a failure with no server response, such as
    a refused connection, does not have.
    """
    driverError = error.orig
    sqlState = getattr(driverError, "pgcode", None)
    if not isinstance(sqlState, str) or not ABEND_SQLSTATE_PATTERN.fullmatch(sqlState):
        sqlState = ABEND_UNKNOWN_CODE
    driverClass = ABEND_UNKNOWN_CODE if driverError is None else type(driverError).__name__
    return ABEND_DATABASE_TEMPLATE.format(
        errorClass=type(error).__name__, driverClass=driverClass, sqlState=sqlState
    )


def ErrnoName(error: OSError) -> str:
    """Return the errno symbol of a file error, such as ENOENT, or UNKNOWN when it has none."""
    if isinstance(error.errno, int):
        return errno.errorcode.get(error.errno, ABEND_UNKNOWN_CODE)
    return ABEND_UNKNOWN_CODE


def DescribeInputs() -> str:
    """Return the STEP040 input DD names with their tables; this replaces the TIOT DD walk."""
    return ", ".join(f"{ddName}={tableName}" for ddName, tableName in INPUT_DATASETS)


def DescribeOutputs(statementsDir: Path) -> str:
    """Return the STEP040 output DD names with the files they are written to.

    The paths come from CARDDEMO_OUTPUT_DIR, so each is shown with every non-printable
    character escaped and the log line stays one line.
    """
    return ", ".join(
        f"{ddName}={PrintableText(statementsDir / fileName)}"
        for ddName, fileName in OUTPUT_DATASETS
    )


def WriteStatements(context: JobContext, statementsDir: Path) -> StatementCounts:
    """STEP010, STEP020 and STEP040: read TRXFL and the master files, and write the statements.

    RecoverInterruptedPublication first finishes what a killed run left, before any table is
    read, so a run that then fails before the commit point leaves the previous statements. The
    whole TRNXFILE table loads next, as 8100-TRNXFILE-OPEN does before the first
    cross-reference, so a capacity or empty-file abend happens before any file is opened. The
    statements then stream from the cross-reference read into the files. That read is closed
    before the connection, on success and on failure alike, and the connection writes nothing.
    The recovery and the publication each run only once RequireWindow on that connection has
    found the command's batch window still held.
    """
    with context.engine.connect() as connection:
        RequireWindow(connection)
        RecoverInterruptedPublication(statementsDir)
        table = cbstm03a.LoadTransactionTable(cbstm03b.ReadStatementTransactions(connection))
        with closing(cbstm03b.ReadXrefsInCardOrder(connection)) as sources:
            statements = (
                cbstm03a.AssembleStatementData(source.xref, source.customer, source.account, table)
                for source in sources
            )
            statementRecords = cbstm03a.BuildStatementFiles(table, statements)
            return WriteStatementFiles(statementsDir, statementRecords, connection)


def WriteStatementFiles(
    statementsDir: Path,
    statementRecords: Iterable[cbstm03a.StatementRecords],
    connection: Connection | None = None,
) -> StatementCounts:
    """STEP030 and the STMTFILE and HTMLFILE DDs: write both .part files, then publish both.

    Each statement's records are written as they are built, one LF after every record. Only
    when every statement is written are both files made durable and published over STATEMNT.PS
    and STATEMNT.HTML as a pair (PublishFiles), which checks the batch window on ``connection``,
    the job connection, when one is given. A failure before the publication's commit point
    removes both .part files and every other work file, and leaves the previous statements as
    they were. The caller has already run RecoverInterruptedPublication.
    """
    # STEP030 deletes the previous statements before STEP040 writes; here they are replaced as a
    # pair, only once both new files are complete, so a failed run never leaves a partial or
    # mixed pair.
    statementsDir.mkdir(parents=True, exist_ok=True)
    counts = StatementCounts()
    with ExitStack() as openFiles:
        statementFile = openFiles.enter_context(
            OpenTemporaryFile(statementsDir, STATEMENT_FILE_NAME, STATEMENT_FILE_ENCODING)
        )
        htmlFile = openFiles.enter_context(
            OpenTemporaryFile(statementsDir, HTML_FILE_NAME, HTML_FILE_ENCODING)
        )
        for records in statementRecords:
            WriteRecords(statementFile, records.psLines)
            WriteRecords(htmlFile, records.htmlLines)
            counts.Add(records)
        completedFiles = ((statementFile, STATEMENT_FILE_NAME), (htmlFile, HTML_FILE_NAME))
        PublishFiles(statementsDir, completedFiles, connection)
    return counts


def TemporaryPath(statementsDir: Path, fileName: str) -> Path:
    """Return the .part file a statement file is written to before it replaces fileName."""
    # A fixed name, like the G####V00.dat.part of the generation archives, is safe because the
    # batch window admits one batch command at a time, and it lets the next run find leftovers.
    return statementsDir / f"{fileName}{TEMPORARY_SUFFIX}"


def BackupPath(statementsDir: Path, fileName: str) -> Path:
    """Return the hard link that keeps fileName's previous file until its publication ends."""
    return statementsDir / f"{fileName}{BACKUP_SUFFIX}"


def MarkerPath(statementsDir: Path) -> Path:
    """Return the marker whose presence means a publication has begun and not committed."""
    return statementsDir / PUBLICATION_MARKER_NAME


def RecoverInterruptedPublication(statementsDir: Path) -> None:
    """Finish what a killed earlier run left in statementsDir, before this run reads anything.

    A marker means a run was killed during its publication, before the commit point, or its
    rollback failed, so the publication is rolled back and the previous statements stand again.
    Without a marker, the backups of a run stopped after its commit point or before its marker
    and the .part files of a run killed mid-write are deleted. Each file removed or restored is
    logged by path. A missing directory is left missing, so a run that then fails creates
    nothing. The interrupted run's step returned to PENDING, and this run is its retry.
    """
    if not statementsDir.is_dir():
        return
    RemoveStaleFiles(statementsDir, TemporaryPath)
    markerPath = MarkerPath(statementsDir)
    if not os.path.lexists(markerPath):
        RemoveStaleFiles(statementsDir, BackupPath)
        return
    LOGGER.warning(ROLLBACK_LOG, PrintableText(markerPath))
    RollBackPublication(statementsDir)


def RemoveStaleFiles(statementsDir: Path, pathOf: Callable[[Path, str], Path]) -> None:
    """Delete the work files at pathOf that an interrupted run left, logging each one."""
    for stalePath in RemoveWorkFiles(statementsDir, pathOf):
        LOGGER.warning(STALE_FILE_LOG, PrintableText(stalePath))


def RemoveWorkFiles(statementsDir: Path, pathOf: Callable[[Path, str], Path]) -> list[Path]:
    """Delete each statement file's work file at pathOf(statementsDir, fileName).

    Returns the paths it deleted.
    """
    removedPaths: list[Path] = []
    for _, fileName in OUTPUT_DATASETS:
        workPath = pathOf(statementsDir, fileName)
        if os.path.lexists(workPath):
            workPath.unlink()
            removedPaths.append(workPath)
    return removedPaths


@contextmanager
def OpenTemporaryFile(statementsDir: Path, fileName: str, encoding: str) -> Iterator[IO[str]]:
    """Create fileName's .part file to write in encoding, and delete it unless it was renamed.

    This stands in for DISP=(NEW,CATLG,DELETE): the file is cataloged under its final name only
    by PublishFiles, and deleted when the step ends any other way. Exclusive creation refuses a
    path that already exists, a symbolic link included, with FileExistsError. A record with no
    form in encoding raises UnicodeEncodeError when it is written.
    """
    temporaryPath = TemporaryPath(statementsDir, fileName)
    temporaryFile = temporaryPath.open("x", encoding=encoding, newline=LINE_TERMINATOR)
    try:
        with temporaryFile:
            yield temporaryFile
    finally:
        temporaryPath.unlink(missing_ok=True)


def WriteRecords(outputFile: IO[str], records: Iterable[str]) -> None:
    """WRITE each fixed-width record followed by one LF.

    Raises ValueError for a record holding an LF, which would split it into two records.
    """
    for record in records:
        if LINE_TERMINATOR in record:
            raise ValueError(f"A {outputFile.name} record holds a line feed")
        outputFile.write(record + LINE_TERMINATOR)


def PublishFiles(
    statementsDir: Path,
    completedFiles: tuple[tuple[IO[str], str], ...],
    connection: Connection | None,
) -> None:
    """Make both complete .part files durable, then publish them over both targets as a pair.

    completedFiles pairs each open .part file with its target file name. Once both are synced,
    RequireWindow on ``connection``, when one is given, refuses a lost batch window before the
    publication changes any file. BackUpTargets then keeps the previous files and creates the
    marker; both .part files are renamed over their targets and the directory is synced.
    Removing the marker is the commit point. An OSError before it, from the marker's sync,
    either rename, the renames' sync or the removal itself, rolls the publication back
    (RollBackPublication) and is raised again, so the previous statements stand as they were.
    After it the directory is synced and the backups are deleted; a failure there leaves the new
    statements in place, and the next run deletes any backup left.
    """
    for temporaryFile, _ in completedFiles:
        temporaryFile.flush()
        os.fsync(temporaryFile.fileno())
        temporaryFile.close()
    if connection is not None:
        RequireWindow(connection)
    BackUpTargets(statementsDir)
    try:
        SyncDirectory(statementsDir)
        for _, fileName in completedFiles:
            os.replace(TemporaryPath(statementsDir, fileName), statementsDir / fileName)
        SyncDirectory(statementsDir)
        MarkerPath(statementsDir).unlink()
    except OSError:
        RollBackPublication(statementsDir)
        raise
    SyncDirectory(statementsDir)
    RemoveWorkFiles(statementsDir, BackupPath)


def BackUpTargets(statementsDir: Path) -> None:
    """Hard-link each existing statement file to its backup name, then create the marker.

    Once the marker exists, a backup exists exactly when its statement file did, and neither
    file has left its path. A failure before then deletes the backups and leaves the statement
    files as they were. A marker already present belongs to a publication no run has recovered
    yet, so its backups are left alone and FileExistsError is raised.
    """
    markerPath = MarkerPath(statementsDir)
    if os.path.lexists(markerPath):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(markerPath))
    try:
        for _, fileName in OUTPUT_DATASETS:
            targetPath = statementsDir / fileName
            backupPath = BackupPath(statementsDir, fileName)
            backupPath.unlink(missing_ok=True)
            if os.path.lexists(targetPath):
                os.link(targetPath, backupPath, follow_symlinks=False)
        # The backups are durable before the marker that makes a later rollback trust them.
        SyncDirectory(statementsDir)
        markerPath.touch(exist_ok=False)
    except OSError:
        RemoveWorkFiles(statementsDir, BackupPath)
        raise


def RollBackPublication(statementsDir: Path) -> None:
    """Undo an uncommitted publication, so the previous statements stand as they were.

    The .part files are deleted, and each statement file is restored from its backup or, when
    it had none, deleted. Removing the marker ends the rollback; the backups go only after it,
    so a rollback cut short keeps its marker and backups, and the next run completes it.
    """
    RemoveWorkFiles(statementsDir, TemporaryPath)
    for _, fileName in OUTPUT_DATASETS:
        RestoreTarget(statementsDir, fileName)
    SyncDirectory(statementsDir)
    MarkerPath(statementsDir).unlink(missing_ok=True)
    SyncDirectory(statementsDir)
    RemoveWorkFiles(statementsDir, BackupPath)


def RestoreTarget(statementsDir: Path, fileName: str) -> None:
    """Return fileName to its state before the publication, logging each file it changes."""
    targetPath = statementsDir / fileName
    backupPath = BackupPath(statementsDir, fileName)
    if not os.path.lexists(backupPath):
        if os.path.lexists(targetPath):
            targetPath.unlink()
            LOGGER.warning(UNPUBLISHED_FILE_LOG, PrintableText(targetPath))
        return
    if os.path.lexists(targetPath) and os.path.samestat(os.lstat(backupPath), os.lstat(targetPath)):
        return
    # The backup is linked to the free .part name and that link renamed over the target, so the
    # backup itself stays until the rollback ends and a rollback cut short can be run again.
    stagingPath = TemporaryPath(statementsDir, fileName)
    os.link(backupPath, stagingPath, follow_symlinks=False)
    os.replace(stagingPath, targetPath)
    LOGGER.warning(RESTORED_FILE_LOG, PrintableText(targetPath), PrintableText(backupPath))


def SyncDirectory(directory: Path) -> None:
    """fsync a directory, so its links, renames and removals are durable before the next step."""
    directoryDescriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directoryDescriptor)
    finally:
        os.close(directoryDescriptor)
