"""Seed the dataset tables from the legacy sample images.

Replaces: REPRO steps of jobs ACCTFILE, CARDFILE, CUSTFILE, XREFFILE, TRANFILE, TRANTYPE,
    TRANCATG, DISCGRP, TCATBALF; DUSRSECJ IEBGENER and REPRO steps; DALYTRAN staging; images
    data/EBCDIC/AWS.M2.CARDDEMO.*.PS; copybooks CVACT01Y, CVCUS01Y, CVACT02Y, CVACT03Y,
    CVTRA03Y, CVTRA04Y, CVTRA02Y, CVTRA01Y, CSUSR01Y, CVTRA06Y, CVTRA05Y; the loads of VSAM
    clusters AWS.M2.CARDDEMO.{ACCTDATA,CUSTDATA,CARDDATA,CARDXREF,TRANTYPE,TRANCATG,DISCGRP,
    TCATBALF,USRSEC,TRANSACT}.VSAM.KSDS and the sequential DALYTRAN.PS staging input.

app.seed.sample_seed.LoadSampleData replaces the eleven dataset tables from the EBCDIC images,
which are the images the JCL loads, inside env.py's single migration transaction. The USRSEC
passwords are stored only as bcrypt hashes, and the TRANFILE priming record is skipped, so
transactions starts empty. The seed runs only when APP_ENV is local or test, so the sample
users never reach a production database, where the skip is logged as a warning, and never in
offline mode (alembic upgrade --sql), which has no connection to load through. A completed seed
leaves SEED_MARKER_COMMENT on the batch schema in the same transaction, and downgrade() clears
it with the truncate. Revision 0008 reads that marker and records only a seed it finds as the
first batch cycle.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-02 23:05:00.000000
"""

import logging
from collections.abc import Sequence
from pathlib import Path

from alembic import context, op
from app.core.config import MigrationSettings
from app.core.logging import PrintableText
from app.seed.image_reader import ENCODING_EBCDIC, ImageFormatError
from app.seed.sample_seed import LoadSampleData

# Revision identifiers, used by Alembic. Alembic looks these names, and upgrade and downgrade,
# up exactly as spelt, so they keep them instead of the MK_Modernization Rule's naming clause.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The APP_ENV values that load the sample data; production never does.
SEED_APP_ENVS: tuple[str, ...] = ("local", "test")

# Alembic's own migration logger, which alembic.ini shows at INFO, unlike app loggers.
LOGGER = logging.getLogger("alembic.runtime.migration")

# The cause logged for an ImageFormatError raised without one: a framing or size problem
# found before any field is decoded.
FRAMING_ERROR_CODE = "framing"
# Replaces the reason of a re-raised ImageFormatError, which may quote the record's text.
WITHHELD_REASON = "the record text is withheld"

# One statement over all eleven tables, so any foreign key between them is satisfied.
TRUNCATE_DATASET_TABLES = (
    "TRUNCATE TABLE security.users, accounts.accounts, customers.customers, cards.cards,"
    " xref.card_xref, transactions.transactions, transactions.daily_transactions,"
    " transactions.transaction_category_balances, disclosure.disclosure_groups,"
    " reference.transaction_types, reference.transaction_categories"
)

# The seed-provenance marker: the batch schema's comment, which revision 0001 leaves unset and
# nothing else sets. Revision 0008 holds a frozen copy and records the seed cycle only when the
# schema carries it. Each statement is a complete static SQL literal, never formatted.
SEED_MARKER_COMMENT = "CardDemo sample seed loaded by Alembic revision 0002"
RECORD_SEED_MARKER_SQL = (
    "COMMENT ON SCHEMA batch IS 'CardDemo sample seed loaded by Alembic revision 0002'"
)
CLEAR_SEED_MARKER_SQL = "COMMENT ON SCHEMA batch IS NULL"


def ShouldSeed(settings: MigrationSettings) -> bool:
    """Return True when APP_ENV allows the sample seed and the run has a database connection."""
    return settings.APP_ENV in SEED_APP_ENVS and not context.is_offline_mode()


def LogSeedSkipped(settings: MigrationSettings, stepName: str) -> None:
    """Log that stepName was skipped and why, leaving the dataset tables untouched.

    An offline run is an ordinary skip, logged at INFO. An online skip means APP_ENV is
    production, which must never hold the sample data, so it is logged at WARNING.
    """
    if context.is_offline_mode():
        LOGGER.info("%s skipped: offline mode has no connection to the tables", stepName)
    else:
        LOGGER.warning("%s skipped: APP_ENV=%s loads no sample data", stepName, settings.APP_ENV)


def ImageErrorCode(error: ImageFormatError) -> str:
    """Return the class name of error's cause, or FRAMING_ERROR_CODE when it has no cause."""
    if error.__cause__ is None:
        return FRAMING_ERROR_CODE
    return type(error.__cause__).__name__


def LogImageFormatError(imageDir: Path, error: ImageFormatError, errorCode: str) -> None:
    """Log a missing or malformed image by whitelisted metadata only, never by the error's text.

    The metadata is the image directory, the image file name, the record number (0 for the
    whole file), the error class and errorCode, the class of its cause. The directory and file
    name are shown with every non-printable character escaped, so the line stays one line.
    """
    imageName = Path(error.path).name
    errorClass = type(error).__name__
    LOGGER.error(
        "sample seed failed for the images in %s: %s in image %s record %d, cause %s",
        PrintableText(imageDir),
        errorClass,
        PrintableText(imageName),
        error.recordNumber,
        errorCode,
    )


def WithheldImageError(error: ImageFormatError, errorCode: str) -> ImageFormatError:
    """Return an ImageFormatError with error's path and record number but no record text."""
    return ImageFormatError(error.path, error.recordNumber, f"{errorCode}; {WITHHELD_REASON}")


def LoadImages(settings: MigrationSettings) -> dict[str, int]:
    """Load the EBCDIC sample images on the migration connection; return rows per table.

    The image reader raises every image failure as an ImageFormatError, which is logged by its
    directory, file name, record number and error and cause classes. A missing image, an
    ImageFormatError for record 0 chained to a FileNotFoundError, is logged with cause
    FileNotFoundError and re-raised unchanged, because it names only the path. Any other, such as
    a malformed image, is re-raised as an ImageFormatError with the same path and record number
    and no record text. Either way the whole upgrade rolls back in env.py's single migration
    transaction.
    """
    imageDir = settings.EbcdicDir()
    try:
        return LoadSampleData(op.get_bind(), imageDir, ENCODING_EBCDIC, settings.BCRYPT_ROUNDS)
    except ImageFormatError as error:
        errorCode = ImageErrorCode(error)
        LogImageFormatError(imageDir, error, errorCode)
        if isinstance(error.__cause__, FileNotFoundError):
            # Nothing was read from the absent file: the message holds only its escaped path and
            # the OS reason, and the chained FileNotFoundError shows the path by repr.
            raise
        # The original error is not chained: its message, and its causes' messages, can quote
        # raw field content of the malformed record, such as a card number or an SSN.
        raise WithheldImageError(error, errorCode) from None


def LogSeedCounts(counts: dict[str, int]) -> None:
    """Log one summary line with the rows loaded into each table, in load order."""
    tableCounts = ", ".join(f"{tableName}={rowCount}" for tableName, rowCount in counts.items())
    LOGGER.info("sample seed loaded %s", tableCounts)


def RecordSeedMarker() -> None:
    """Mark the batch schema as holding the sample seed, in the seed's own transaction."""
    op.execute(RECORD_SEED_MARKER_SQL)


def ClearSeedMarker() -> None:
    """Remove the seed marker from the batch schema, in the truncate's own transaction."""
    op.execute(CLEAR_SEED_MARKER_SQL)


def upgrade() -> None:
    """Load the sample images when ShouldSeed allows it; otherwise log the skip and load nothing.

    Only a completed load leaves the seed marker: a failed one raises before it is written.
    """
    settings = MigrationSettings()
    if not ShouldSeed(settings):
        LogSeedSkipped(settings, "sample seed")
        return
    LogSeedCounts(LoadImages(settings))
    RecordSeedMarker()


def downgrade() -> None:
    """Empty the eleven dataset tables wherever upgrade() seeds them, then clear the seed marker.

    The truncate is gated like the seed: where upgrade() loaded nothing (production, offline
    mode) the tables may hold real data the seed never wrote, so it is left in place.
    """
    settings = MigrationSettings()
    if not ShouldSeed(settings):
        LogSeedSkipped(settings, "sample seed truncate")
        return
    op.execute(TRUNCATE_DATASET_TABLES)
    ClearSeedMarker()
