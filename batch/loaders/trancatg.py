"""The TRANCATG loader: chain step 8, which replaces the transaction category table.

Replaces: TRANCATG, CVTRA04Y

jcl/TRANCATG.jcl deletes AWS.M2.CARDDEMO.TRANCATG.VSAM.KSDS (STEP05), defines it with
KEYS(6 0) RECORDSIZE(60 60) (STEP10) and REPROs AWS.M2.CARDDEMO.TRANCATG.PS into it (STEP15).
Here the 60-byte CVTRA04Y records of that image, or of its ASCII copy trancatg.txt, replace the
rows of reference.transaction_categories in one transaction through replace_loader. The key
TRAN-TYPE-CD X(02) plus TRAN-CAT-CD 9(04) is the table's primary key, and FILLER X(04) at
offset 56 is dropped. The foreign key to reference.transaction_types and the one from
disclosure.disclosure_groups are deferred inside the replace and checked at commit.

No later chain step writes this table, so the load is always reloadable.
"""

from app.models import TransactionCategory
from app.seed.image_reader import ENCODING_ASCII, ENCODING_EBCDIC
from app.utils.record_layouts import TRANSACTION_CATEGORY_LAYOUT

from batch.loaders import replace_loader
from batch.orchestration.job_context import JobContext, JobResult

JOB_NAME = "tran-categories"
JCL_NAME = "TRANCATG"

LOADER_SPEC = replace_loader.LoaderSpec(
    table=TransactionCategory.__table__,
    layout=TRANSACTION_CATEGORY_LAYOUT,
    dataset="transaction_categories",
    encodings=(ENCODING_EBCDIC, ENCODING_ASCII),
)


def RunLoader(context: JobContext) -> JobResult:
    """Replace reference.transaction_categories with the TRANCATG image's records.

    The image is the EBCDIC AWS.M2.CARDDEMO.TRANCATG.PS unless the run asks for ascii. Returns
    the rows_loaded and records_skipped counts, 18 and 0 for the sample images. Raises
    JobFailure(EXIT_FAILURE) for a missing or malformed image or an integrity violation, with
    the table unchanged.
    """
    return replace_loader.ReplaceTable(context, LOADER_SPEC)
