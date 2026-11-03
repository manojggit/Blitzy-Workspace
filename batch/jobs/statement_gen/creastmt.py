"""Replaces: CREASTMT (job steps DELDEF01, STEP010, STEP020, STEP030, STEP040).

Chain step 17, files only: it writes STATEMNT.PS (80-column records) and STATEMNT.HTML
(100-column records) under CARDDEMO_OUTPUT_DIR/statements/, writes no table, and exits 0, or 16
when CBSTM03A abends. The TRXFL work file of DELDEF01, STEP010 and STEP020 is one ordered query,
cbstm03b.ReadStatementTransactions. STEP040 is the cbstm03a builder, whose statements stream
into STATEMNT.PS.part and STATEMNT.HTML.part beside the targets. STEP030's delete of the
previous statements becomes the replace of both files once both are complete, so a failed run
leaves the previous statements as they were and no .part file. A run killed mid-write leaves
only .part files, which the next run removes before it writes. The CLI holds the batch window
around the command, so this module takes no lock.
"""

import logging
import os
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from jinja2 import TemplateError
from sqlalchemy.exc import DBAPIError

from batch.jobs.statement_gen import cbstm03a, cbstm03b
from batch.orchestration import run_ledger
from batch.orchestration.job_context import EXIT_ABEND, EXIT_OK, JobContext, JobFailure, JobResult

# The CLI job name and the JCL name it accepts as an alias, as batch_chain's JobDefinition names
# chain step 17.
JOB_NAME = "statement-gen"
JCL_NAME = "CREASTMT"
PROGRAM_NAME = "CBSTM03A"
STEP_NAME = "STEP040"

STATEMENT_FILE_NAME = "STATEMNT.PS"
HTML_FILE_NAME = "STATEMNT.HTML"
# Every cp037 character has a Latin-1 form, so strict Latin-1 writes one byte per column and
# refuses any other text with UnicodeEncodeError. The fixtures use the same encoding.
OUTPUT_ENCODING = "latin-1"
LINE_TERMINATOR = "\n"
TEMPORARY_SUFFIX = ".part"

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

START_LOG = "START OF EXECUTION OF JOB %s"
DATASETS_LOG = "Running JCL : %s Step %s inputs %s; outputs %s"
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
    database, file, picture, encoding or template error. The previous statements then stay.
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
        LOGGER.error(ABEND_LOG, PROGRAM_NAME, error)
        raise JobFailure(EXIT_ABEND, f"ABENDING PROGRAM {PROGRAM_NAME}: {error}") from error
    LOGGER.info(END_LOG, JCL_NAME, counts.statements, counts.transactions)
    return result


def DescribeInputs() -> str:
    """Return the STEP040 input DD names with their tables; this replaces the TIOT DD walk."""
    return ", ".join(f"{ddName}={tableName}" for ddName, tableName in INPUT_DATASETS)


def DescribeOutputs(statementsDir: Path) -> str:
    """Return the STEP040 output DD names with the files they are written to."""
    return ", ".join(f"{ddName}={statementsDir / fileName}" for ddName, fileName in OUTPUT_DATASETS)


def WriteStatements(context: JobContext, statementsDir: Path) -> StatementCounts:
    """STEP010, STEP020 and STEP040: read TRXFL and the master files, and write the statements.

    The whole TRNXFILE table loads first, as 8100-TRNXFILE-OPEN does before the first
    cross-reference, so a capacity or empty-file abend happens before any file is opened. The
    statements then stream from the cross-reference read into the files. That read is closed
    before the connection, on success and on failure alike, and the connection writes nothing.
    """
    with context.engine.connect() as connection:
        table = cbstm03a.LoadTransactionTable(cbstm03b.ReadStatementTransactions(connection))
        with closing(cbstm03b.ReadXrefsInCardOrder(connection)) as sources:
            statements = (
                cbstm03a.AssembleStatementData(source.xref, source.customer, source.account, table)
                for source in sources
            )
            statementRecords = cbstm03a.BuildStatementFiles(table, statements)
            return WriteStatementFiles(statementsDir, statementRecords)


def WriteStatementFiles(
    statementsDir: Path, statementRecords: Iterable[cbstm03a.StatementRecords]
) -> StatementCounts:
    """STEP030 and the STMTFILE and HTMLFILE DDs: write both .part files, then replace both.

    Each statement's records are written as they are built, one LF after every record. Only
    when every statement is written are both files made durable and renamed over STATEMNT.PS
    and STATEMNT.HTML. A failure before that removes both .part files and leaves the previous
    statements untouched.
    """
    # STEP030 deletes the previous statements before STEP040 writes; here they are replaced
    # only once both new files are complete, so a failed run never leaves a partial statement.
    statementsDir.mkdir(parents=True, exist_ok=True)
    RemoveStaleTemporaryFiles(statementsDir)
    counts = StatementCounts()
    with ExitStack() as openFiles:
        statementFile = openFiles.enter_context(
            OpenTemporaryFile(statementsDir, STATEMENT_FILE_NAME)
        )
        htmlFile = openFiles.enter_context(OpenTemporaryFile(statementsDir, HTML_FILE_NAME))
        for records in statementRecords:
            WriteRecords(statementFile, records.psLines)
            WriteRecords(htmlFile, records.htmlLines)
            counts.Add(records)
        completedFiles = ((statementFile, STATEMENT_FILE_NAME), (htmlFile, HTML_FILE_NAME))
        PublishFiles(statementsDir, completedFiles)
    return counts


def TemporaryPath(statementsDir: Path, fileName: str) -> Path:
    """Return the .part file a statement file is written to before it replaces fileName."""
    # A fixed name, like the G####V00.dat.part of the generation archives, is safe because the
    # batch window admits one batch command at a time, and it lets the next run find leftovers.
    return statementsDir / f"{fileName}{TEMPORARY_SUFFIX}"


def RemoveStaleTemporaryFiles(statementsDir: Path) -> None:
    """Delete the .part files a killed earlier run left, before this run writes its own.

    The interrupted run's step returned to PENDING, and this run is its retry.
    """
    for _, fileName in OUTPUT_DATASETS:
        temporaryPath = TemporaryPath(statementsDir, fileName)
        if os.path.lexists(temporaryPath):
            temporaryPath.unlink()
            LOGGER.warning(STALE_FILE_LOG, temporaryPath)


@contextmanager
def OpenTemporaryFile(statementsDir: Path, fileName: str) -> Iterator[IO[str]]:
    """Create fileName's .part file for writing, and delete it unless it has been renamed.

    This stands in for DISP=(NEW,CATLG,DELETE): the file is cataloged under its final name only
    by PublishFiles, and deleted when the step ends any other way. Exclusive creation refuses a
    path that already exists, a symbolic link included, with FileExistsError.
    """
    temporaryPath = TemporaryPath(statementsDir, fileName)
    temporaryFile = temporaryPath.open("x", encoding=OUTPUT_ENCODING, newline=LINE_TERMINATOR)
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


def PublishFiles(statementsDir: Path, completedFiles: tuple[tuple[IO[str], str], ...]) -> None:
    """Make each complete .part file durable, then rename it over its target file name.

    completedFiles pairs each open .part file with its target file name. Both files are on
    disk before either is renamed. A failure between the two renames leaves the new STATEMNT.PS
    beside the previous STATEMNT.HTML until the failed step is rerun.
    """
    for temporaryFile, _ in completedFiles:
        temporaryFile.flush()
        os.fsync(temporaryFile.fileno())
        temporaryFile.close()
    for _, fileName in completedFiles:
        os.replace(TemporaryPath(statementsDir, fileName), statementsDir / fileName)
    SyncDirectory(statementsDir)


def SyncDirectory(directory: Path) -> None:
    """fsync a directory, so its renames are durable before the ledger records the run."""
    directoryDescriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directoryDescriptor)
    finally:
        os.close(directoryDescriptor)
