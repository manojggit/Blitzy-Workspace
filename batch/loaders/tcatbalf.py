"""Load the transaction category balances from the TCATBALF image, chain step 10.

Replaces: TCATBALF, CVTRA01Y

jcl/TCATBALF.jcl deletes and redefines the KSDS AWS.M2.CARDDEMO.TCATBALF.VSAM.KSDS
(KEYS(17 0) RECORDSIZE(50 50)) and REPROs AWS.M2.CARDDEMO.TCATBALF.PS into it. Here
replace_loader.ReplaceTable empties transactions.transaction_category_balances and refills it
from that EBCDIC image, or from data/ASCII/tcatbal.txt with --encoding ascii, in one
transaction: 50 rows, none skipped. The 17-byte TRAN-CAT-KEY becomes the three-column primary
key, TRAN-CAT-BAL S9(09)V99 is decoded from zoned overpunch into an exact Decimal, and the
deferred foreign key to accounts.accounts is checked at commit.

POSTTRAN (step 13) writes this table, so run_ledger refuses this load once POSTTRAN has run in
the OPEN cycle, before RunLoader is called; this module never checks it.
"""

from app.models import TransactionCategoryBalance
from app.seed.image_reader import ENCODING_ASCII, ENCODING_EBCDIC
from app.utils.record_layouts import TRAN_CAT_BALANCE_LAYOUT

from batch.loaders import replace_loader
from batch.orchestration.job_context import JobContext, JobResult

JOB_NAME = "tran-cat-balances"
JCL_NAME = "TCATBALF"

LOADER_SPEC = replace_loader.LoaderSpec(
    table=TransactionCategoryBalance.__table__,
    layout=TRAN_CAT_BALANCE_LAYOUT,
    dataset="transaction_category_balances",
    encodings=(ENCODING_EBCDIC, ENCODING_ASCII),
)


def RunLoader(context: JobContext) -> JobResult:
    """Replace the category balance table with the image's records and return the load counts.

    Raises JobFailure(EXIT_FAILURE) for an unsupported encoding, a missing or malformed image,
    or an integrity violation, leaving the table as it was.
    """
    return replace_loader.ReplaceTable(context, LOADER_SPEC)
