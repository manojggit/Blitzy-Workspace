"""The TRANTYPE loader: reloads reference.transaction_types from the TRANTYPE image.

Replaces: TRANTYPE, CVTRA03Y

jcl/TRANTYPE.jcl deletes and redefines AWS.M2.CARDDEMO.TRANTYPE.VSAM.KSDS (STEP05, STEP10:
KEYS(2 0) RECORDSIZE(60 60)), then copies AWS.M2.CARDDEMO.TRANTYPE.PS into it (STEP15 REPRO).
Each 60-byte TRAN-TYPE-RECORD becomes one row; its FILLER X(08) at offset 52 is dropped.

Chain step 7. It runs before TRANCATG because transaction_categories references this table;
the foreign keys are deferred to commit, so this parent can be replaced while children exist,
and a type removed from the image while still referenced fails the load with exit 12 and a
full rollback. No later step writes this table, so the load is accepted in any cycle state.
"""

from app.models import TransactionType
from app.seed.image_reader import ENCODING_ASCII, ENCODING_EBCDIC
from app.utils.record_layouts import TRANSACTION_TYPE_LAYOUT

from batch.loaders import replace_loader
from batch.orchestration.job_context import JobContext, JobResult

JOB_NAME = "tran-types"
JCL_NAME = "TRANTYPE"

LOADER_SPEC = replace_loader.LoaderSpec(
    table=TransactionType.__table__,
    layout=TRANSACTION_TYPE_LAYOUT,
    dataset="transaction_types",
    encodings=(ENCODING_EBCDIC, ENCODING_ASCII),
)


def RunLoader(context: JobContext) -> JobResult:
    """Replace reference.transaction_types with the TRANTYPE image's records, atomically.

    The image is the EBCDIC AWS.M2.CARDDEMO.TRANTYPE.PS by default, or data/ASCII/trantype.txt
    with --encoding ascii; --source-dir overrides the directory. Returns rows_loaded and
    records_skipped. Raises JobFailure(EXIT_FAILURE) for a missing or malformed image or an
    integrity violation, leaving the table unchanged.
    """
    return replace_loader.ReplaceTable(context, LOADER_SPEC)
