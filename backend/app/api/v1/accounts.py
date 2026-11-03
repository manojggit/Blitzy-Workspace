"""Replaces: COACTVWC (CAVW), BMS COACTVW; COACTUPC (CAUP), BMS COACTUP; copybooks CVACT01Y,
CVCUS01Y, CVACT03Y.

The account router, mounted at ``/api/v1/accounts``. It replaces the CAVW and CAUP transaction
definitions of ``csd/CARDDEMO.CSD`` and ENTER, F5 and F12 on maps COACTVW and COACTUP. Every
route is regular-role through ``GetRegularContext``: any user type other than exactly ``A``
passes, and an admin is refused with 403 and an empty message.

Each handler makes one awaited call to the service that ports its program and returns the
service's model. CAUP's action state machine lives entirely in ``coactupc``: which fields the
state leaves editable, the edits, the no-change check and the two-unit save. Legacy outcomes
are raised by the services as ``LegacyMessageError`` and serialised by ``app.core.errors``;
screen text breaking its BMS LENGTH or the code-page-037 repertoire is a 422. The view masks
the SSN; the update screen does not, because COACTUPC edits it. A PUT without an allow-listed
Origin is refused with 403 by ``core.middleware.OriginGuard``. The routes are
``LosslessKeyRoute`` routes, so ``/{acctId}`` receives a key holding ``/`` as one value and
``/{acctId}/edit`` is reached only by a literal ``/edit`` suffix.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.api.v1 import LegacyErrorResponses, LosslessKeyRoute
from app.core.dependencies import GetRegularContext, OnlineContext
from app.schemas.accounts import AccountScreen, AccountUpdateRequest, AccountViewResponse
from app.schemas.common import ScreenText
from app.services.accounts import coactupc, coactvwc

# BMS ACCTSID LENGTH=11 on both COACTVW and COACTUP.
ACCOUNT_ID_LENGTH = 11
# Raw screen text with no digit pattern and never an int: a blank key (sent as %20), a key
# holding "/" (sent as %2F, kept in one segment by LosslessKeyRoute) and any other non-numeric
# key must reach the services' key edits, which answer ``No input received`` and the 11-digit
# non-zero literals, rather than a 422 or a framework 404.
ACCOUNT_ID_PATH = Annotated[
    ScreenText(ACCOUNT_ID_LENGTH),
    Path(description="Account number as typed on the screen (up to 11 characters)."),
]
REGULAR_CONTEXT = Annotated[OnlineContext, Depends(GetRegularContext)]

ROUTER = APIRouter(prefix="/accounts", tags=["accounts"], route_class=LosslessKeyRoute)


@ROUTER.get(
    "/{acctId}",
    response_model=AccountViewResponse,
    summary="CAVW / COACTVWC: view account",
    description=(
        "Edits the account number, then reads the card cross-reference, the account and the "
        "customer, and returns them for display with the SSN masked."
    ),
    responses=LegacyErrorResponses(400, 401, 403, 404, 503),
)
async def ViewAccount(acctId: ACCOUNT_ID_PATH, context: REGULAR_CONTEXT) -> AccountViewResponse:
    """Return the COACTVW details of the account, or raise its key, not-found or file outcome."""
    return await coactvwc.ViewAccount(context, acctId)


@ROUTER.get(
    "/{acctId}/edit",
    response_model=AccountScreen,
    summary="CAUP / COACTUPC: fetch account for update",
    description=(
        "Edits the account number and fetches the account and customer into the update screen "
        "in state SHOW_DETAILS, with the encrypted screen_token the next PUT sends back."
    ),
    responses=LegacyErrorResponses(400, 401, 403, 404, 503),
)
async def FetchAccountForEdit(acctId: ACCOUNT_ID_PATH, context: REGULAR_CONTEXT) -> AccountScreen:
    """Return the COACTUP screen of the fetched account, as a fresh CAUP ENTER shows it."""
    return await coactupc.FetchAccountForEdit(context, acctId)


@ROUTER.put(
    "/{acctId}",
    response_model=AccountScreen,
    summary="CAUP / COACTUPC: process account update (enter, save, cancel)",
    description=(
        "Runs one step of the ENTER (enter: validate), F5 (save, only after validation) and "
        "F12 (cancel) state machine on the state carried by screen_token, and returns the next "
        "screen state with the legacy message, field_errors and cursor_field."
    ),
    responses=LegacyErrorResponses(400, 401, 403, 404, 409, 423, 503),
)
async def UpdateAccount(
    acctId: ACCOUNT_ID_PATH, updateRequest: AccountUpdateRequest, context: REGULAR_CONTEXT
) -> AccountScreen:
    """Return the COACTUP screen after one ENTER, F5 or F12, or raise its legacy outcome."""
    return await coactupc.ProcessAccountUpdate(context, acctId, updateRequest)
