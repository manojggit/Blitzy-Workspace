"""The shared atomic replace-load routine of every dataset loader job.

Replaces: ACCTFILE, REPROC

Each legacy load job rebuilt its cluster with the same IDCAMS steps: DELETE <cluster> with
IF MAXCC LE 08 THEN SET MAXCC = 0, DEFINE CLUSTER ... KEYS(n o), and REPRO INFILE(<PS>)
OUTFILE(<KSDS>); DUSRSECJ does the same after its IEBGENER copy. ACCTFILE, CARDFILE, CUSTFILE,
XREFFILE, TRANFILE, TRANTYPE, TRANCATG, DISCGRP, TCATBALF, DUSRSECJ and the DALYTRAN staging
load each declare a LOADER_SPEC and call ReplaceTable, the single implementation of that
pattern. Specified in the Technical Specification, Sections 0.6.8, 0.6.12 and 0.7.2.

ReplaceTable reads and decodes the image and builds the rows before any database work, then
empties and refills the table in one transaction with the deferrable foreign keys deferred, and
finishes the ledger run in that same transaction, so every run gives the same result. The
failure paths of IDCAMS belong to the IBM utility, not to CardDemo source, so a load is atomic:
an unsupported encoding, a missing or malformed image, a duplicate key REPRO would have
reported, or a foreign-key violation found at commit raises JobFailure(EXIT_FAILURE) and leaves
the table as it was.

Financial-reload refusal, cycles, skips and RUNNING rows belong to batch_chain and run_ledger,
and the batch window is held by batch/cli.py around the command. Rows are never logged: they
carry card numbers, SSNs and password hashes.
"""

import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.core.paths import ResolveRepoPath
from app.seed.image_reader import (
    ENCODING_ASCII,
    ENCODING_EBCDIC,
    ImageFormatError,
    ReadImage,
    SampleImagePath,
)
from app.seed.sample_seed import BuildPlainRows
from app.utils.record_layouts import RecordLayout
from sqlalchemy import Table, delete, insert, text
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

type RowBuilder = Callable[[Sequence[dict[str, object]], JobContext], list[dict[str, object]]]


@dataclass(frozen=True)
class LoaderSpec:
    """What one loader job replaces and how its image becomes rows.

    table is the target Core table, such as Account.__table__. layout is the copybook layout
    of app.utils.record_layouts. dataset is the image_reader.SAMPLE_IMAGE_NAMES key, which is
    also the table name. encodings lists the encodings the dataset's images exist in, the
    default first; TRANFILE and DUSRSECJ have no ASCII copy. primingCheck, when set, sees each
    raw record and skips the ones it accepts. rowBuilder turns the decoded records into insert
    rows; without one, each record is inserted as decoded.
    """

    table: Table
    layout: RecordLayout
    dataset: str
    encodings: tuple[str, ...] = IMAGE_ENCODINGS
    primingCheck: Callable[[bytes], bool] | None = None
    rowBuilder: RowBuilder | None = None


def BuildPlainTableRows(
    records: Sequence[dict[str, object]], context: JobContext
) -> list[dict[str, object]]:
    """Return each decoded record as its insert row, exactly as the Alembic sample seed does."""
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
    """Return the exit-12 text of an image ReadImage refused.

    ReadImage reports an absent file as ImageFormatError chained to the FileNotFoundError, so
    that case is named as a missing image rather than a malformed one.
    """
    if isinstance(error.__cause__, FileNotFoundError):
        return IMAGE_NOT_FOUND_TEMPLATE.format(dataset=spec.dataset, path=imagePath)
    return IMAGE_REJECTED_TEMPLATE.format(dataset=spec.dataset, reason=error)


@contextmanager
def MappedLoadErrors(spec: LoaderSpec, imagePath: Path) -> Iterator[None]:
    """Turn the expected failures of one load into JobFailure(EXIT_FAILURE), chained to them.

    Only a missing image, a malformed or undecodable image, and an integrity violation are
    mapped. A duplicate key surfaces at the insert and a deferred foreign-key violation at
    commit, so the context encloses the whole transaction. JobFailure and every other error
    propagate unchanged, for batch_chain to record.
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


def ReadRows(
    context: JobContext, spec: LoaderSpec, imagePath: Path, encoding: str
) -> tuple[list[dict[str, object]], int]:
    """Read, decode and build the insert rows of the image; return them and the skipped count.

    This runs before the transaction opens, so row building, bcrypt hashing for DUSRSECJ
    included, holds no lock.
    """
    contents = ReadImage(imagePath, spec.layout, encoding, spec.primingCheck)
    rowBuilder = spec.rowBuilder or BuildPlainTableRows
    return rowBuilder(contents.records, context), contents.skippedCount


def WriteRows(
    context: JobContext, spec: LoaderSpec, rows: list[dict[str, object]], result: JobResult
) -> None:
    """Replace the table's rows and finish the ledger run, committing both together.

    Generated sort columns and audit timestamps are never in the rows: the database fills
    them. No insert runs for an empty list, because SQLAlchemy would execute one default-valued
    INSERT. Any error rolls back the delete, the inserts and the ledger write alike.
    """
    with context.engine.begin() as connection:
        connection.execute(DEFER_CONSTRAINTS_SQL)
        connection.execute(delete(spec.table))
        if rows:
            connection.execute(insert(spec.table), rows)
        # Records nothing for a context bound to no run, as a loader called directly is.
        run_ledger.FinishAtomicRun(connection, context, result)


def ReplaceTable(context: JobContext, spec: LoaderSpec, imagePath: Path | None = None) -> JobResult:
    """Replace spec's table with the records of its image, atomically, and return the counts.

    imagePath overrides the sample image, as dalytran's --daily-file does. The result carries
    rows_loaded and records_skipped, and is recorded COMPLETED in the load's own transaction
    when the context is bound to a run. Raises JobFailure(EXIT_FAILURE) for an unsupported
    encoding, a missing or malformed image, or an integrity violation, leaving the table and
    the ledger unchanged.
    """
    sourcePath, encoding = ResolveSource(context, spec, imagePath)
    with MappedLoadErrors(spec, sourcePath):
        rows, skippedCount = ReadRows(context, spec, sourcePath, encoding)
        counts = {ROWS_LOADED_KEY: len(rows), RECORDS_SKIPPED_KEY: skippedCount}
        result = JobResult(EXIT_OK, counts)
        WriteRows(context, spec, rows, result)
    LOGGER.info(LOAD_COMPLETE_TEMPLATE, spec.dataset, len(rows), skippedCount)
    return result
