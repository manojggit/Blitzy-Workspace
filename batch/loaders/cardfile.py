"""The CARDFILE loader job, chain step 2: replace cards.cards with the CARDDATA image.

Replaces: CARDFILE, CVACT02Y

jcl/CARDFILE.jcl deletes and redefines the CARDDATA KSDS, KEYS(16 0) RECORDSIZE(150 150), then
REPROs AWS.M2.CARDDEMO.CARDDATA.PS into it; ReplaceTable does both in one transaction. The
CLCIFIL and OPCIFIL CEMT close and open are superseded by the batch window batch/cli.py holds
around the command. The CARDAIX KEYS(11 16) define, path and BLDINDEX of STEP40-STEP60 need no
counterpart: the permanent index ix_cards_acct_id (Alembic 0003) replaces the alternate index,
and PostgreSQL maintains it as rows are inserted. Each 150-byte CARD-RECORD becomes one row as
decoded; the generated card_num_sort column is filled by the database. The deferred foreign key
to accounts.accounts is checked at commit, so a card for a missing account rolls the whole load
back with exit 12.
"""

from app.models import Card
from app.seed.image_reader import ENCODING_ASCII, ENCODING_EBCDIC
from app.utils.record_layouts import CARD_LAYOUT

from batch.loaders import replace_loader
from batch.orchestration.job_context import JobContext, JobResult

# The CLI load name and the legacy JCL alias, as batch_chain registers chain step 2.
JOB_NAME = "cards"
JCL_NAME = "CARDFILE"

LOADER_SPEC = replace_loader.LoaderSpec(
    table=Card.__table__,
    layout=CARD_LAYOUT,
    dataset="cards",
    encodings=(ENCODING_EBCDIC, ENCODING_ASCII),
)


def RunLoader(context: JobContext) -> JobResult:
    """Replace cards.cards with the CARDDATA image of context's encoding, atomically.

    The EBCDIC image is the default and the ASCII copy is read for --encoding ascii. Returns
    the rows_loaded and records_skipped counts; raises JobFailure(EXIT_FAILURE) for a missing
    or malformed image, a duplicate card number or a card whose account does not exist,
    leaving the table unchanged.
    """
    return replace_loader.ReplaceTable(context, LOADER_SPEC)
