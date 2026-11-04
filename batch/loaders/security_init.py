"""The DUSRSECJ loader: replaces security.users from the USRSEC image, passwords bcrypt-hashed.

Replaces: DUSRSECJ, CSUSR01Y

DUSRSECJ deleted USRSEC.PS (PREDEL IEFBR14), rebuilt it from in-stream data (STEP01 IEBGENER,
LRECL 80), defined the USRSEC KSDS keyed KEYS(8,0) on SEC-USR-ID (STEP02 IDCAMS) and REPROed
the file into it (STEP03). This loader, chain step 11 and the CLI's `load init-users`, reads
the AWS.M2.CARDDEMO.USRSEC.PS image in the CSUSR01Y layout and replaces the table atomically
through replace_loader. The image exists only in EBCDIC, so `--encoding ascii` exits 12.

Each SEC-USR-PWD is bcrypt-hashed at BCRYPT_ROUNDS exactly as decoded, case kept, by the row
builder the Alembic sample seed uses; the plaintext never reaches a row, the ledger counts or a
log line. The sample users are seed data only: under APP_ENV=production the load is refused
with exit 12 before the image is resolved or read.
"""

from collections.abc import Iterable, Iterator

from app.models import User
from app.seed.image_reader import ENCODING_EBCDIC
from app.seed.sample_seed import BuildUserRows
from app.utils.record_layouts import USER_SECURITY_LAYOUT

from batch.loaders import replace_loader
from batch.orchestration.job_context import EXIT_FAILURE, JobContext, JobFailure, JobResult

JOB_NAME = "init-users"
JCL_NAME = "DUSRSECJ"

# The exit-12 text of a production run; it names no user and quotes no image content.
PRODUCTION_REFUSED_MESSAGE = (
    "INIT-USERS REFUSED: SAMPLE USERS ARE NOT LOADED WHEN APP_ENV=production"
)


def BuildHashedUserRows(
    records: Iterable[dict[str, object]], context: JobContext
) -> Iterator[dict[str, object]]:
    """Return a lazy iterator of USRSEC insert rows, SEC-USR-PWD replaced by its bcrypt hash.

    Each row holds user_id, first_name, last_name, user_type and password_hash, hashed at the
    run's BCRYPT_ROUNDS by sample_seed.BuildUserRows, so this loader and the Alembic seed build
    identical rows. The generated user_id_sort and the audit timestamps are left to the
    database. records is replace_loader's lazy image stream, not a list, so rows are built one
    insert batch at a time, as its RowBuilder contract requires.
    """
    return BuildUserRows(records, context.settings.BCRYPT_ROUNDS)


LOADER_SPEC = replace_loader.LoaderSpec(
    table=User.__table__,
    layout=USER_SECURITY_LAYOUT,
    dataset="users",
    encodings=(ENCODING_EBCDIC,),
    rowBuilder=BuildHashedUserRows,
)


def RunLoader(context: JobContext) -> JobResult:
    """Replace security.users with the USRSEC image's users and return the load's counts.

    Raises JobFailure(EXIT_FAILURE, PRODUCTION_REFUSED_MESSAGE) when APP_ENV is production,
    before the image is resolved or read and with the table unchanged. Otherwise
    replace_loader.ReplaceTable loads the image, raising JobFailure(EXIT_FAILURE) for an
    unsupported encoding, a missing or malformed image, or an integrity violation.
    """
    if context.settings.IsProduction():
        raise JobFailure(EXIT_FAILURE, PRODUCTION_REFUSED_MESSAGE)
    return replace_loader.ReplaceTable(context, LOADER_SPEC)
