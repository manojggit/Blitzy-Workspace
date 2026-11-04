"""The ACCTFILE account master load, chain step 1: the ACCTDATA image into accounts.accounts.

Replaces: ACCTFILE, CVACT01Y

jcl/ACCTFILE.jcl rebuilt AWS.M2.CARDDEMO.ACCTDATA.VSAM.KSDS with IDCAMS: STEP05 DELETE,
STEP10 DEFINE CLUSTER KEYS(11 0) RECORDSIZE(300 300), and STEP15 REPRO from
AWS.M2.CARDDEMO.ACCTDATA.PS. Here replace_loader.ReplaceTable empties and refills the table in
one transaction from the 300-byte CVACT01Y records, decoded strictly by copybook offset.

The EBCDIC image the JCL loads is the default; --encoding ascii loads data/ASCII/acctdata.txt,
whose account 00000000049 carries addr_zip A000000000 where the EBCDIC image has ZEROAPR.
Accounts are written later by POSTTRAN and INTCALC, so run_ledger refuses this reload once
either has run in the OPEN cycle; that check belongs to the chain, not to this module.
"""

from app.models import Account
from app.seed.image_reader import ENCODING_ASCII, ENCODING_EBCDIC
from app.utils.record_layouts import ACCOUNT_LAYOUT

from batch.loaders import replace_loader
from batch.orchestration.job_context import JobContext, JobResult

JOB_NAME = "accounts"
JCL_NAME = "ACCTFILE"

# Decoded records are keyed by the column names, so each one is inserted as decoded.
LOADER_SPEC = replace_loader.LoaderSpec(
    table=Account.__table__,
    layout=ACCOUNT_LAYOUT,
    dataset="accounts",
    encodings=(ENCODING_EBCDIC, ENCODING_ASCII),
)


def RunLoader(context: JobContext) -> JobResult:
    """Replace accounts.accounts with the ACCTDATA image's records and return the load counts."""
    return replace_loader.ReplaceTable(context, LOADER_SPEC)
