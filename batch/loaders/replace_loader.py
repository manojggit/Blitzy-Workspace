"""The shared atomic replace-load routine of every dataset loader job.

Replaces: ACCTFILE, REPROC

Each legacy load job rebuilt its cluster with the same IDCAMS steps: DELETE <cluster> with
IF MAXCC LE 08 THEN SET MAXCC = 0, DEFINE CLUSTER ... KEYS(n o), and REPRO INFILE(<PS>)
OUTFILE(<KSDS>); DUSRSECJ does the same after its IEBGENER copy. ACCTFILE, CARDFILE, CUSTFILE,
XREFFILE, TRANFILE, TRANTYPE, TRANCATG, DISCGRP, TCATBALF, DUSRSECJ and the DALYTRAN staging
load each declare a LOADER_SPEC and call ReplaceTable, the single implementation of that
pattern. Specified in the Technical Specification, Sections 0.6.8, 0.6.12 and 0.7.2.

ReplaceTable opens the image before any database work, then, in one transaction with the
deferrable foreign keys deferred, empties the table and refills it from the image as a stream:
records are decoded, turned into rows and inserted INSERT_BATCH_SIZE at a time, so memory stays
bounded whatever the size of the image or of a --daily-file. The ledger run finishes in that
same transaction once the whole image has been read, so every run gives the same result. The
failure paths of IDCAMS belong to the IBM utility, not to CardDemo source, so a load is atomic:
an unsupported encoding, a missing image, a malformed record anywhere in the image, a duplicate
key REPRO would have reported, or a foreign-key violation found at commit raises
JobFailure(EXIT_FAILURE) and leaves the table as it was. A malformed record found after earlier
batches were inserted rolls back the DELETE, those batches and the ledger write together.

Financial-reload refusal, cycles, skips and RUNNING rows belong to batch_chain and run_ledger,
and the batch window is held by batch/cli.py around the command. Rows are never logged: they
carry card numbers, SSNs and password hashes.
"""

import itertools
import logging
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.core.paths import ResolveRepoPath
from app.seed.image_reader import (
    ENCODING_ASCII,
    ENCODING_EBCDIC,
    ImageFormatError,
    ImageStream,
    SampleImagePath,
    StreamImage,
)
from app.seed.sample_seed import BuildPlainRows
from app.utils.record_layouts import RecordLayout
from sqlalchemy import Table, delete, insert, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from batch.orchestration import run_ledger
from batch.orchestration.job_context import (
    EXIT_FAILURE,
    EXIT_OK,
    JobContext,
    JobFailure,
    JobResult,
)

LOGGER = logging.getLogger(__name__)

# The encodings of a dataset with both a JCL EBCDIC image and an ASCII copy, the JCL's first.
IMAGE_ENCODINGS: tuple[str, ...] = (ENCODING_EBCDIC, ENCODING_ASCII)

# Every foreign key is DEFERRABLE INITIALLY IMMEDIATE (app.models.FOREIGN_KEY_OPTIONS), so a
# parent table can be emptied and refilled while child rows reference it; the check runs at
# commit.
DEFER_CONSTRAINTS_SQL = text("SET CONSTRAINTS ALL DEFERRED")

# Rows per INSERT executemany. It bounds the decoded records and insert rows a load holds at
# once, and equals the insertmanyvalues page size of SQLAlchemy's psycopg2 dialect, so each
# batch is sent as one multi-row INSERT.
INSERT_BATCH_SIZE = 1000

# The batch.job_runs.counts keys of a load, the ones Alembic 0008 writes for the seed cycle.
ROWS_LOADED_KEY = "rows_loaded"
RECORDS_SKIPPED_KEY = "records_skipped"

# Exit-12 messages, printed by the CLI. None quotes row content.
ENCODING_REFUSED_TEMPLATE = "{dataset}: ENCODING {encoding} NOT SUPPORTED; AVAILABLE: {available}"
IMAGE_NOT_FOUND_TEMPLATE = "{dataset}: INPUT IMAGE NOT FOUND: {path}"
IMAGE_REJECTED_TEMPLATE = "{dataset}: INPUT IMAGE REJECTED: {reason}"
LOAD_ROLLED_BACK_TEMPLATE = "{dataset}: LOAD ROLLED BACK: {reason}"
# psycopg2's message text carries a DETAIL line quoting the offending key, which can be a card
# number or an SSN, so a rolled-back load reports only these diagnostic fields.
INTEGRITY_REASON_TEMPLATE = "SQLSTATE {sqlState} ON CONSTRAINT {constraintName}"
ENCODING_SEPARATOR = ", "

LOAD_COMPLETE_TEMPLATE = "%s: %d ROWS LOADED, %d RECORDS SKIPPED"

# A row builder receives the image's decoded records as a lazy stream in file order and returns
# their insert rows lazily, one per record and in the same order.
type RowBuilder = Callable[[Iterable[dict[str, object]], JobContext], Iterable[dict[str, object]]]


@dataclass(frozen=True)
class LoaderSpec:
    """What one loader job replaces and how its image becomes rows.

    table is the target Core table, such as Account.__table__. layout is the copybook layout
    of app.utils.record_layouts. dataset is the image_reader.SAMPLE_IMAGE_NAMES key, which is
    also the table name. encodings lists the encodings the dataset's images exist in, the
    default first; TRANFILE and DUSRSECJ have no ASCII copy. primingCheck, when set, sees each
    raw record and skips the ones it accepts. rowBuilder turns the stream of decoded records
    into insert rows as they are read, one row per record, without collecting either; without
    one, each record is inserted as decoded.
    """

    table: Table
    layout: RecordLayout
    dataset: str
    encodings: tuple[str, ...] = IMAGE_ENCODINGS
    primingCheck: Callable[[bytes], bool] | None = None
    rowBuilder: RowBuilder | None = None


def BuildPlainTableRows(
    records: Iterable[dict[str, object]], context: JobContext
) -> Iterator[dict[str, object]]:
    """Return a lazy iterator of each record as its insert row, as the Alembic sample seed does."""
    return BuildPlainRows(records, context.settings.BCRYPT_ROUNDS)


def EffectiveEncoding(context: JobContext, spec: LoaderSpec) -> str:
    """Return the run's --encoding, or spec's default, refusing one spec's images lack.

    Raises JobFailure(EXIT_FAILURE) before anything touches the filesystem.
    """
    encoding = context.parameters.encoding or spec.encodings[0]
    if encoding not in spec.encodings:
        message = ENCODING_REFUSED_TEMPLATE.format(
            dataset=spec.dataset,
            encoding=encoding,
            available=ENCODING_SEPARATOR.join(spec.encodings),
        )
        raise JobFailure(EXIT_FAILURE, message)
    return encoding


def ResolveImagePath(context: JobContext, spec: LoaderSpec) -> tuple[Path, str]:
    """Return the sample image of spec's dataset and the encoding to decode it with.

    The directory is --source-dir when given, resolved under the repository root when
    relative, and otherwise the encoding's settings directory (CARDDEMO_EBCDIC_DIR or
    CARDDEMO_ASCII_DIR). The file name comes from image_reader.SAMPLE_IMAGE_NAMES. An
    encoding spec does not support raises JobFailure(EXIT_FAILURE).
    """
    encoding = EffectiveEncoding(context, spec)
    sourceDir = context.parameters.sourceDir
    if sourceDir is not None:
        directory = ResolveRepoPath(sourceDir)
    else:
        directory = context.settings.ImageDir(encoding)
    return SampleImagePath(directory, spec.dataset, encoding)


def ResolveSource(
    context: JobContext, spec: LoaderSpec, imagePath: Path | None
) -> tuple[Path, str]:
    """Return the image to load and its encoding: imagePath when given, else the sample image.

    A given imagePath, such as dalytran's --daily-file, resolves under the repository root
    when relative and is decoded with the run's encoding, which spec must support.
    """
    if imagePath is None:
        return ResolveImagePath(context, spec)
    return ResolveRepoPath(imagePath), EffectiveEncoding(context, spec)


def IntegrityReason(error: IntegrityError) -> str:
    """Return the SQLSTATE and constraint name of a violation, never the key values."""
    databaseError = error.orig
    diagnostics = getattr(databaseError, "diag", None)
    return INTEGRITY_REASON_TEMPLATE.format(
        sqlState=getattr(databaseError, "pgcode", None),
        constraintName=getattr(diagnostics, "constraint_name", None),
    )


def ImageErrorMessage(spec: LoaderSpec, imagePath: Path, error: ImageFormatError) -> str:
    """Return the exit-12 text of an image StreamImage refused.

    StreamImage reports an absent file as ImageFormatError chained to the FileNotFoundError, so
    that case is named as a missing image rather than a malformed one.
    """
    if isinstance(error.__cause__, FileNotFoundError):
        return IMAGE_NOT_FOUND_TEMPLATE.format(dataset=spec.dataset, path=imagePath)
    return IMAGE_REJECTED_TEMPLATE.format(dataset=spec.dataset, reason=error)


@contextmanager
def MappedLoadErrors(spec: LoaderSpec, imagePath: Path) -> Iterator[None]:
    """Turn the expected failures of one load into JobFailure(EXIT_FAILURE), chained to them.

    Only a missing image, a malformed or undecodable image, and an integrity violation are
    mapped. A missing image surfaces when StreamImage opens it, a malformed record while a
    batch is being read, a duplicate key at a batch's insert and a deferred foreign-key
    violation at commit, so the context encloses StreamImage and the whole transaction, and
    each error reaches it after the transaction has rolled back. JobFailure and every other
    error propagate unchanged, for batch_chain to record.
    """
    try:
        yield
    except FileNotFoundError as error:
        message = IMAGE_NOT_FOUND_TEMPLATE.format(dataset=spec.dataset, path=imagePath)
        raise JobFailure(EXIT_FAILURE, message) from error
    except ImageFormatError as error:
        raise JobFailure(EXIT_FAILURE, ImageErrorMessage(spec, imagePath, error)) from error
    except UnicodeDecodeError as error:
        message = IMAGE_REJECTED_TEMPLATE.format(dataset=spec.dataset, reason=error)
        raise JobFailure(EXIT_FAILURE, message) from error
    except IntegrityError as error:
        message = LOAD_ROLLED_BACK_TEMPLATE.format(
            dataset=spec.dataset, reason=IntegrityReason(error)
        )
        raise JobFailure(EXIT_FAILURE, message) from error


def InsertBatches(connection: Connection, table: Table, rows: Iterable[dict[str, object]]) -> int:
    """Insert rows into table INSERT_BATCH_SIZE at a time, as they are built; return the count.

    Each batch is drawn from rows only when the previous one has been executed, so one batch
    is held at a time. No INSERT runs when rows yields nothing, because SQLAlchemy would
    execute one default-valued INSERT for an empty parameter list.
    """
    rowCount = 0
    for batch in itertools.batched(rows, INSERT_BATCH_SIZE):
        connection.execute(insert(table), list(batch))
        rowCount += len(batch)
    return rowCount


def WriteRows(context: JobContext, spec: LoaderSpec, image: ImageStream) -> JobResult:
    """Replace the table's rows with the image's, finish the ledger run, and commit together.

    The rows are built from the stream and inserted in batches inside the transaction.
    Generated sort columns and audit timestamps are never in the rows: the database fills
    them. Any error, a malformed record found after earlier batches included, rolls back the
    delete, every inserted batch and the ledger write alike. Returns the load's counts.
    """
    rowBuilder = spec.rowBuilder or BuildPlainTableRows
    with context.engine.begin() as connection:
        connection.execute(DEFER_CONSTRAINTS_SQL)
        connection.execute(delete(spec.table))
        rowCount = InsertBatches(connection, spec.table, rowBuilder(image, context))
        # Reads any records the builder left, so the whole image is framed and validated and
        # skippedCount is final before commit; a builder yielding one row per record leaves none.
        deque(image, maxlen=0)
        counts = {ROWS_LOADED_KEY: rowCount, RECORDS_SKIPPED_KEY: image.skippedCount}
        result = JobResult(EXIT_OK, counts)
        # Records nothing for a context bound to no run, as a loader called directly is.
        run_ledger.FinishAtomicRun(connection, context, result)
    return result


def ReplaceTable(context: JobContext, spec: LoaderSpec, imagePath: Path | None = None) -> JobResult:
    """Replace spec's table with the records of its image, atomically, and return the counts.

    imagePath overrides the sample image, as dalytran's --daily-file does. The image is opened
    before the transaction and streamed into the table inside it, INSERT_BATCH_SIZE rows at a
    time. The result carries rows_loaded and records_skipped, and is recorded COMPLETED in the
    load's own transaction when the context is bound to a run. Raises JobFailure(EXIT_FAILURE)
    for an unsupported encoding, a missing image, a malformed record anywhere in the image, or
    an integrity violation, leaving the table and the ledger unchanged.
    """
    sourcePath, encoding = ResolveSource(context, spec, imagePath)
    with (
        MappedLoadErrors(spec, sourcePath),
        StreamImage(sourcePath, spec.layout, encoding, spec.primingCheck) as image,
    ):
        result = WriteRows(context, spec, image)
    LOGGER.info(
        LOAD_COMPLETE_TEMPLATE,
        spec.dataset,
        result.counts[ROWS_LOADED_KEY],
        result.counts[RECORDS_SKIPPED_KEY],
    )
    return result
