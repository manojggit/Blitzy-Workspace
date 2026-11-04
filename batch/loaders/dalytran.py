"""Chain step 12: stage the DALYTRAN daily transaction file in transactions.daily_transactions.

Replaces: POSTTRAN, CVTRA06Y

jcl/POSTTRAN.jcl reads the sequential file AWS.M2.CARDDEMO.DALYTRAN.PS through its DALYTRAN
DD, in the 350-byte layout of copybook CVTRA06Y (FILLER X(20) at offset 330 dropped); the file
only had to exist before CBTRN02C ran. This staging load replaces that precondition. It
replaces the table's rows with the image's records, numbered record_seq from 1 in file order,
the order POSTTRAN reads them. It runs as `load daily-transactions` (alias DALYTRAN) and as
the last loader of seed-all.

The image is --daily-file when given, resolved under the repository root when relative, and
otherwise the sample image of the run's --encoding: CARDDEMO_EBCDIC_DIR/AWS.M2.CARDDEMO.DALYTRAN.PS
by default, or CARDDEMO_ASCII_DIR/dailytran.txt. --source-dir replaces either directory. An
explicit --daily-file is decoded with the run's --encoding too.

Records are staged as the file holds them. A repeated tran_id is kept, because a sequential
file may repeat one, and card numbers are not checked, so CBTRN02C reject 100 (INVALID CARD
NUMBER FOUND) stays reachable. The daily file is input only, so this is not a financial reload
and runs in any cycle state. replace_loader makes the load atomic and turns a missing,
malformed or undecodable image into exit 12 with the table unchanged.
"""

from collections.abc import Iterable, Iterator
from pathlib import Path

from app.core.paths import ResolveRepoPath
from app.models import DailyTransaction
from app.seed.image_reader import ENCODING_ASCII, ENCODING_EBCDIC
from app.seed.sample_seed import BuildDailyTransactionRows
from app.utils.record_layouts import DAILY_TRANSACTION_LAYOUT

from batch.loaders import replace_loader
from batch.orchestration.job_context import JobContext, JobResult

# The canonical CLI name and the legacy name of batch_chain.JOB_DEFINITIONS["daily-transactions"].
JOB_NAME = "daily-transactions"
JCL_NAME = "DALYTRAN"


def BuildStagedRows(
    records: Iterable[dict[str, object]], context: JobContext
) -> Iterator[dict[str, object]]:
    """Return the staging rows of the decoded daily records, record_seq numbered from 1.

    The numbering is the sample seed's own (Alembic revision 0002), so the seed and this load
    stage identical rows. The daily builder hashes nothing; BCRYPT_ROUNDS is passed only
    because every seed row builder shares one signature.
    """
    # Lazy rather than a list: replace_loader inserts the rows a batch at a time as they are
    # built, and a list would hold a --daily-file of any size in memory at once.
    return BuildDailyTransactionRows(records, context.settings.BCRYPT_ROUNDS)


LOADER_SPEC = replace_loader.LoaderSpec(
    table=DailyTransaction.__table__,
    layout=DAILY_TRANSACTION_LAYOUT,
    dataset="daily_transactions",
    encodings=(ENCODING_EBCDIC, ENCODING_ASCII),
    rowBuilder=BuildStagedRows,
)


def ResolveDailyFile(context: JobContext) -> Path | None:
    """Return the run's --daily-file as an absolute path, or None when it was not given.

    A relative --daily-file resolves under the repository root, never the working directory.
    None makes ReplaceTable stage the sample daily image of the run's --encoding.
    """
    dailyFile = context.parameters.dailyFile
    if dailyFile is None:
        return None
    return ResolveRepoPath(dailyFile)


def RunLoader(context: JobContext) -> JobResult:
    """Replace transactions.daily_transactions with the daily image's records, atomically.

    Returns EXIT_OK with rows_loaded and records_skipped. Raises JobFailure(EXIT_FAILURE) for
    an encoding the images lack, a missing image or a malformed record, leaving the table and
    the ledger unchanged.
    """
    return replace_loader.ReplaceTable(context, LOADER_SPEC, ResolveDailyFile(context))
