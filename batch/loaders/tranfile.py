"""TRANFILE loader, chain step 5: rebuilds transactions.transactions from DALYTRAN.PS.INIT.

Replaces: TRANFILE, CVTRA05Y

The image's single record only primes the newly defined cluster: PostgreSQL text cannot hold its
NUL bytes and a table needs no priming, so the record is skipped and the load leaves the table
empty, as the legacy DELETE/DEFINE did. The TRAN-PROC-TS AIX is ix_transactions_proc_ts, which
TRANIDX re-validates. No ASCII copy of the image exists, so only EBCDIC is accepted.
"""

from app.models import Transaction
from app.seed.image_reader import ENCODING_EBCDIC, IsTranfilePrimingRecord
from app.utils.record_layouts import TRANSACTION_LAYOUT

from batch.loaders import replace_loader
from batch.orchestration.job_context import JobContext, JobResult

# The `carddemo-batch load` command name, and the legacy JCL member it is also accepted as.
JOB_NAME = "transactions"
JCL_NAME = "TRANFILE"

LOADER_SPEC = replace_loader.LoaderSpec(
    table=Transaction.__table__,
    layout=TRANSACTION_LAYOUT,
    dataset="transactions",
    encodings=(ENCODING_EBCDIC,),
    primingCheck=IsTranfilePrimingRecord,
)


def RunLoader(context: JobContext) -> JobResult:
    """Empty transactions.transactions and reload it from the TRANFILE image, atomically.

    The priming record is skipped, so the result is rows_loaded 0 and records_skipped 1.
    Raises JobFailure(EXIT_FAILURE) for --encoding ascii, a missing or malformed image, or an
    integrity violation, leaving the table unchanged.
    """
    return replace_loader.ReplaceTable(context, LOADER_SPEC)
