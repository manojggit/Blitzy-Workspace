"""The DISCGRP loader job: chain step 9, loading the disclosure groups from their image.

Replaces: DISCGRP, CVTRA02Y

jcl/DISCGRP.jcl deleted AWS.M2.CARDDEMO.DISCGRP.VSAM.KSDS (STEP05), redefined it with
KEYS(16 0) RECORDSIZE(50 50) (STEP10) and copied AWS.M2.CARDDEMO.DISCGRP.PS into it with REPRO
(STEP15). Here replace_loader.ReplaceTable empties disclosure.disclosure_groups and refills it
from the image in one transaction, so a rerun of an unchanged image gives the same rows; the
shipped EBCDIC and ASCII sample images hold 51 records each. Each 50-byte CVTRA02Y record keeps
its 16-byte key (group, transaction type, category) and its DIS-INT-RATE S9(04)V99, decoded
from zoned decimal into an exact Decimal; FILLER X(28) is not read.

The EBCDIC image the JCL loads is the default input; --encoding ascii reads data/ASCII's copy,
whose DEFAULT/07/0001 rate is 0.00 where the EBCDIC image has 15.00. The foreign key to
reference.transaction_categories is deferred and checked at commit.
"""

from app.models import DisclosureGroup
from app.seed.image_reader import ENCODING_ASCII, ENCODING_EBCDIC
from app.utils.record_layouts import DISCLOSURE_GROUP_LAYOUT

from batch.loaders import replace_loader
from batch.orchestration.job_context import JobContext, JobResult

JOB_NAME = "disclosure-groups"
JCL_NAME = "DISCGRP"

LOADER_SPEC = replace_loader.LoaderSpec(
    table=DisclosureGroup.__table__,
    layout=DISCLOSURE_GROUP_LAYOUT,
    dataset="disclosure_groups",
    encodings=(ENCODING_EBCDIC, ENCODING_ASCII),
)


def RunLoader(context: JobContext) -> JobResult:
    """Replace disclosure.disclosure_groups with the DISCGRP image's records and return counts.

    The result's counts carry rows_loaded and records_skipped. Raises JobFailure(EXIT_FAILURE)
    for an unsupported encoding, a missing or malformed image, or an integrity violation, and
    leaves the table unchanged.
    """
    return replace_loader.ReplaceTable(context, LOADER_SPEC)
