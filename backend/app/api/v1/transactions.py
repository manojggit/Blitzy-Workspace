"""Replaces: COTRN00C (CT00), BMS COTRN00; COTRN01C (CT01), BMS COTRN01; COTRN02C (CT02), BMS
COTRN02; copybook CVTRA05Y.

The transaction router, mounted at ``/api/v1/transactions``. It replaces the CT00, CT01 and
CT02 transaction definitions of ``csd/CARDDEMO.CSD`` and the main-menu ``XCTL`` into those
programs. Every route is regular-only through ``GetRegularContext``: an admin (user type
exactly ``A``) is refused with 403 and an empty message, and a POST without an allow-listed
Origin is refused with 403 by ``core.middleware.OriginGuard`` before it reaches a handler.

Each handler makes one awaited call to the service that ports its program and returns the
service's model. Legacy outcomes are raised by the services as ``LegacyMessageError`` and
serialised by ``app.core.errors`` as the four standard keys; screen text breaking its BMS
LENGTH or the code-page-037 repertoire is a 422. Every CT00 screen outcome, the paging
literals and ``Tran ID must be Numeric ...`` included, is a 200 list response, so the list and
selection routes declare only the 503 file error besides the session and role refusals.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from app.api.v1 import LegacyErrorResponses
from app.core.dependencies import GetRegularContext, OnlineContext
from app.schemas.common import ScreenText
from app.schemas.transactions import (
    TransactionAddRequest,
    TransactionAddResponse,
    TransactionDetailResponse,
    TransactionListQuery,
    TransactionListResponse,
    TransactionSelectionRequest,
    TransactionSelectionResponse,
)
from app.services.transactions import cotrn00c, cotrn01c, cotrn02c

# TRNIDIN, LENGTH=16, on COTRN01 (TRAN-ID X(16) of CVTRA05Y).
TRAN_ID_LENGTH = 16
# Raw screen text with no pattern: a blank ID sent as %20 must reach COTRN01C's
# ``Tran ID can NOT be empty...`` edit rather than a 422.
TRAN_ID_PATH = Annotated[
    ScreenText(TRAN_ID_LENGTH),
    Path(description="Transaction ID as typed on the screen (up to 16 characters)."),
]
REGULAR_CONTEXT = Annotated[OnlineContext, Depends(GetRegularContext)]

ROUTER = APIRouter(prefix="/transactions", tags=["transactions"])


@ROUTER.get(
    "",
    response_model=TransactionListResponse,
    summary="CT00 / COTRN00C: list transactions (10 per page)",
    description=(
        "Without a startKey, the browse starts at the first TRAN-ID greater than or equal to "
        "searchTranId, in EBCDIC order. A page's first_key or last_key is sent back unchanged "
        "as startKey with direction backward (F7) or forward (F8)."
    ),
    responses=LegacyErrorResponses(401, 403, 503),
)
async def ListTransactions(
    query: Annotated[TransactionListQuery, Query()], context: REGULAR_CONTEXT
) -> TransactionListResponse:
    """Return one COTRN00 page of at most 10 transactions with its paging or edit literal."""
    return await cotrn00c.ListTransactions(context, query)


@ROUTER.post(
    "/selection",
    response_model=TransactionSelectionResponse,
    summary="CT00 / COTRN00C: process transaction list selection",
    description=(
        "The first row with a selection code decides: S opens the Transaction View through "
        "route. Any other code gives the invalid-selection literal with the list browsed "
        "again from search_tran_id."
    ),
    responses=LegacyErrorResponses(401, 403, 503),
)
async def SelectTransaction(
    selectionRequest: TransactionSelectionRequest, context: REGULAR_CONTEXT
) -> TransactionSelectionResponse:
    """Return the route of a valid S selection, or the re-browsed list page."""
    return await cotrn00c.ProcessTransactionSelection(context, selectionRequest)


@ROUTER.get(
    "/{tranId}",
    response_model=TransactionDetailResponse,
    summary="CT01 / COTRN01C: view transaction",
    description=(
        "The card number is masked, and both timestamps are shown as their stored "
        "26-character text. A blank ID is refused and an absent one is not found."
    ),
    responses=LegacyErrorResponses(400, 401, 403, 404, 503),
)
async def ViewTransaction(
    tranId: TRAN_ID_PATH, context: REGULAR_CONTEXT
) -> TransactionDetailResponse:
    """Return the TRANSACT record with the TRAN-ID as keyed, as the COTRN01 map shows it."""
    return await cotrn01c.ViewTransaction(context, tranId)


@ROUTER.post(
    "",
    response_model=TransactionAddResponse,
    summary="CT02 / COTRN02C: add transaction (enter, copy last)",
    description=(
        "enter edits the fields in screen order and adds the transaction when confirm is Y. "
        "copy_last first copies the highest transaction's fields into the form, then runs the "
        "same confirm flow; a successful add returns 200 with the new tran_id."
    ),
    responses=LegacyErrorResponses(400, 401, 403, 404, 409, 503),
)
async def AddTransaction(
    addRequest: TransactionAddRequest, context: REGULAR_CONTEXT
) -> TransactionAddResponse:
    """Return the confirm prompt or the COTRN02C added literal, or raise the first failure."""
    return await cotrn02c.AddTransaction(context, addRequest)
