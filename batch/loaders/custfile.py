"""The CUSTFILE loader job: replace customers.customers with the CUSTDATA image's records.

Replaces: CUSTFILE, CVCUS01Y, CUSTREC

Chain step 3 and the `load customers` command, with the JCL name CUSTFILE as its alias. The
IDCAMS STEP05 DELETE, STEP10 DEFINE CLUSTER KEYS(9 0) RECORDSIZE(500 500) and STEP15 REPRO
INFILE(CUSTDATA) OUTFILE(CUSTVSAM) become one atomic replace of the table from
AWS.M2.CARDDEMO.CUSTDATA.PS, or from its custdata.txt ASCII copy, through
replace_loader.ReplaceTable. The CLCIFIL and OPCIFIL CEMT close and open of CUSTDAT are the
batch window that batch/cli.py holds around the command. Every CVCUS01Y field is loaded as
decoded, FICO scores below 300 included.
"""

from app.models import Customer
from app.seed.image_reader import ENCODING_ASCII, ENCODING_EBCDIC
from app.utils.record_layouts import CUSTOMER_LAYOUT

from batch.loaders import replace_loader
from batch.orchestration.job_context import JobContext, JobResult

JOB_NAME = "customers"
JCL_NAME = "CUSTFILE"

LOADER_SPEC = replace_loader.LoaderSpec(
    table=Customer.__table__,
    layout=CUSTOMER_LAYOUT,
    dataset="customers",
    encodings=(ENCODING_EBCDIC, ENCODING_ASCII),
)


def RunLoader(context: JobContext) -> JobResult:
    """Replace customers.customers with the CUSTDATA image's 500-byte records, atomically.

    The image is the EBCDIC one unless the run's --encoding is ascii, read from --source-dir
    or the encoding's settings directory. Returns rows_loaded and records_skipped; a missing or
    malformed image or an integrity violation raises JobFailure(EXIT_FAILURE) and leaves the
    table unchanged.
    """
    return replace_loader.ReplaceTable(context, LOADER_SPEC)
