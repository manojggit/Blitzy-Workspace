"""Load the card cross-reference table from the CARDXREF image, chain step 4.

Replaces: XREFFILE, CVACT03Y

XREFFILE deleted and redefined the CARDXREF cluster with KEYS(16 0) RECORDSIZE(50 50) and
REPROed AWS.M2.CARDDEMO.CARDXREF.PS into it (STEP05-STEP15), then defined and built the CXACAIX
alternate index on XREF-ACCT-ID, KEYS(11,25) (STEP20-STEP30). The table's primary key replaces
the cluster key and the permanent index ix_card_xref_acct_id replaces the alternate index, so a
reload only replaces the rows. The ASCII copy's 36-character lines omit the trailing FILLER
X(14), which image_reader pads.
"""

from app.models import CardXref
from app.seed.image_reader import ENCODING_ASCII, ENCODING_EBCDIC
from app.utils.record_layouts import CARD_XREF_LAYOUT

from batch.loaders import replace_loader
from batch.orchestration.job_context import JobContext, JobResult

JOB_NAME = "card-xref"
JCL_NAME = "XREFFILE"

LOADER_SPEC = replace_loader.LoaderSpec(
    table=CardXref.__table__,
    layout=CARD_XREF_LAYOUT,
    dataset="card_xref",
    encodings=(ENCODING_EBCDIC, ENCODING_ASCII),
)


def RunLoader(context: JobContext) -> JobResult:
    """Replace xref.card_xref with the records of the CARDXREF image, atomically.

    The image is the EBCDIC one unless the run's --encoding is ascii. Returns the load's counts;
    raises JobFailure(EXIT_FAILURE) for a missing or malformed image or a foreign-key violation,
    leaving the table unchanged.
    """
    return replace_loader.ReplaceTable(context, LOADER_SPEC)
