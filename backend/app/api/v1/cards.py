"""Replaces: COCRDLIC (CCLI), BMS COCRDLI; COCRDSLC (CCDL), BMS COCRDSL; COCRDUPC (CCUP), BMS
COCRDUP; copybooks CVACT02Y, CVCRD01Y.

The card router, mounted at ``/api/v1/cards``. It replaces the CCLI, CCDL and CCUP transaction
definitions of ``csd/CARDDEMO.CSD`` and COCRDLIC's ``XCTL`` into COCRDSLC (selection ``S``) and
COCRDUPC (selection ``U``). Every route is regular-role through ``GetRegularContext``: any user
type other than exactly ``A`` passes, and an admin is refused with 403 and an empty message.

Each handler makes one awaited call to the service that ports its program and returns the
service's model. Legacy outcomes are raised by the services as ``LegacyMessageError`` and
serialised by ``app.core.errors``; screen text breaking its BMS LENGTH or the code-page-037
repertoire is a 422. Responses carry masked card numbers and never the CVV. The list's page
keys and ``card_ref`` are opaque server-issued tokens, so a full card number never enters a
frontend URL. CCLI, CCDL and CCUP touch no batch-window file. A POST or PUT without an
allow-listed Origin is refused with 403 by ``core.middleware.OriginGuard``. The routes are
``LosslessKeyRoute`` routes, so ``/{cardNum}`` receives CARDSID text holding ``/`` as one
value, while ``/selection`` and the literal ``/edit`` suffix keep their precedence.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from app.api.v1 import LegacyErrorResponses, LosslessKeyRoute
from app.core.dependencies import GetRegularContext, OnlineContext
from app.schemas.cards import (
    CardDetailResponse,
    CardKeyQuery,
    CardListQuery,
    CardListResponse,
    CardScreen,
    CardSelectionRequest,
    CardSelectionResponse,
    CardUpdateRequest,
)
from app.schemas.common import CARD_REF_MAX_LENGTH, ScreenTextOrToken
from app.services.cards import cocrdlic, cocrdslc, cocrdupc

# BMS CARDSID LENGTH=16 on both COCRDSL and COCRDUP (CARD-NUM X(16) of CVACT02Y).
CARD_NUM_LENGTH = 16
# Raw screen text with no digit pattern: the CARDSID text as keyed (a blank key sent as %20, a
# key holding "/" sent as %2F and kept in one segment by LosslessKeyRoute) must reach the
# services' ``Card number not provided`` and ``CARD ID FILTER,IF SUPPLIED MUST BE A 16 DIGIT
# NUMBER`` edits rather than a 422 or a framework 404. A longer value is a ``card_ref`` token,
# checked for length only, so on the GET routes cocrdslc.ResolveCardKey answers a garbled one
# with ``Did not find cards for this search condition``; the PUT route never decodes it.
CARD_KEY_PATH = Annotated[
    ScreenTextOrToken(CARD_NUM_LENGTH, CARD_REF_MAX_LENGTH),
    Path(
        description=(
            "Card number as typed (16 characters) or an opaque card_ref token from the card list."
        ),
    ),
]
REGULAR_CONTEXT = Annotated[OnlineContext, Depends(GetRegularContext)]

ROUTER = APIRouter(prefix="/cards", tags=["cards"], route_class=LosslessKeyRoute)


@ROUTER.get(
    "",
    response_model=CardListResponse,
    summary="CCLI / COCRDLIC: list cards (7 per page)",
    description=(
        "Lists at most 7 cards in card number order, filtered by acctId and cardNum, with each "
        "card number masked and no CVV. A page's first_key or last_key is an opaque token sent "
        "back as startKey: omit direction for ENTER, or send backward (F7) or forward (F8). "
        "The rows' selection codes travel as repeated selections parameters and are edited "
        "after the filters: more than one S or U, or any other code, is a 400 carrying the "
        "rows redisplayed from the first card, the page keys and page. A single S or U never "
        "transfers here; POST /cards/selection does."
    ),
    responses=LegacyErrorResponses(400, 401, 403, 503),
)
async def ListCards(
    query: Annotated[CardListQuery, Query()], context: REGULAR_CONTEXT
) -> CardListResponse:
    """Return one COCRDLI page of at most 7 cards, or raise its filter or file outcome."""
    return await cocrdlic.ListCards(context, query)


@ROUTER.post(
    "/selection",
    response_model=CardSelectionResponse,
    summary="CCLI / COCRDLIC: process card list selection",
    description=(
        "The filters are edited first, then the selection codes. One S returns action view and "
        "one U action update, each with the row's opaque card_ref; more than one selection or "
        "any other code is a 400 carrying the redisplayed rows and page keys."
    ),
    # 404 is declared too: cocrdlic.TransferToCard refuses an invalid or expired row card_ref
    # with ``Did not find cards for this search condition``, and every status the route can
    # return must be in the OpenAPI document the contract tests check.
    responses=LegacyErrorResponses(400, 401, 403, 404, 503),
)
async def SelectCard(
    selectionRequest: CardSelectionRequest, context: REGULAR_CONTEXT
) -> CardSelectionResponse:
    """Return the card_ref and action of a valid S or U selection, replacing COCRDLIC's XCTL."""
    return await cocrdlic.ProcessCardSelection(context, selectionRequest)


@ROUTER.get(
    "/{cardNum}",
    response_model=CardDetailResponse,
    summary="CCDL / COCRDSLC: view card",
    description=(
        "Edits both the account number and the card number, then reads the card by card number "
        "only. The card number is returned masked with an opaque card_ref, and the CVV is never "
        "returned."
    ),
    responses=LegacyErrorResponses(400, 401, 403, 404, 503),
)
async def ViewCard(
    cardNum: CARD_KEY_PATH, query: Annotated[CardKeyQuery, Query()], context: REGULAR_CONTEXT
) -> CardDetailResponse:
    """Return the COCRDSL details of the card, or raise its key edit, not-found or file outcome."""
    return await cocrdslc.ViewCard(context, cardNum, query.acct_id)


@ROUTER.get(
    "/{cardNum}/edit",
    response_model=CardScreen,
    summary="CCUP / COCRDUPC: fetch card for update",
    description=(
        "Fetches the card into the update screen in state SHOW_DETAILS, with the card number "
        "masked, no CVV, and the screen_token the next PUT sends back. A card_ref from the card "
        "list, sent with its row's 11-digit acctId, is read with those list keys and no key "
        "edits; a typed card number has both keys edited first."
    ),
    responses=LegacyErrorResponses(400, 401, 403, 404, 503),
)
async def FetchCardForEdit(
    cardNum: CARD_KEY_PATH, query: Annotated[CardKeyQuery, Query()], context: REGULAR_CONTEXT
) -> CardScreen:
    """Return the COCRDUP screen of the fetched card, as the card-list entry shows it."""
    return await cocrdupc.FetchCardForEdit(context, cardNum, query.acct_id)


@ROUTER.put(
    "/{cardNum}",
    response_model=CardScreen,
    summary="CCUP / COCRDUPC: process card update (enter, save, cancel)",
    description=(
        "Runs one step of the ENTER (enter), F5 (save) and F12 (cancel) state machine on the "
        "state carried by screen_token: enter validates, save writes the validated values and "
        "cancel re-reads the card. Error bodies carry the screen fields, state and screen_token. "
        "The cardNum path is the route identity only: the keys come from fields before the "
        "fetch and from screen_token afterwards, so the path value, a card_ref included, is "
        "not decoded."
    ),
    responses=LegacyErrorResponses(400, 401, 403, 404, 409, 423, 503),
)
async def UpdateCard(
    cardNum: CARD_KEY_PATH, updateRequest: CardUpdateRequest, context: REGULAR_CONTEXT
) -> CardScreen:
    """Return the COCRDUP screen after one ENTER, F5 or F12, or raise its legacy outcome."""
    return await cocrdupc.ProcessCardUpdate(context, cardNum, updateRequest)
