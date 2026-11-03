"""Replaces: COBIL00C (CB00), BMS COBIL00; copybooks CVACT01Y, CVACT03Y, CVTRA05Y.

The bill payment router, mounted at ``/api/v1/billpay``. It replaces the CB00 transaction
definition of ``csd/CARDDEMO.CSD`` and ENTER on map COBIL00.

CB00 serves regular users (any type other than exactly ``A``) through ``GetRegularContext``,
which refuses an admin with 403 and an empty message. A POST without an allow-listed Origin is
refused with 403 by ``core.middleware.OriginGuard`` before it reaches the handler. The service
owns every edit, literal and write; its failures are raised as ``LegacyMessageError`` and
serialised by ``app.core.errors`` as the four-key legacy error body, while the current balance
travels only on the HTTP 200 screen. Screen text breaking its BMS LENGTH or the code-page-037
repertoire is a 422.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.v1 import LegacyErrorResponses, SessionFirstRoute
from app.core.dependencies import GetRegularContext, OnlineContext
from app.schemas.billpay import BillPayRequest, BillPayResponse
from app.services.billpay import cobil00c

ROUTER = APIRouter(prefix="/billpay", tags=["billpay"], route_class=SessionFirstRoute)

PAY_BILL_DESCRIPTION = (
    "Shows the account's current balance and asks for Y/N confirmation. On Y, records a"
    " BILL PAYMENT - ONLINE transaction for the full balance and reduces the balance by it."
)


@ROUTER.post(
    "",
    response_model=BillPayResponse,
    summary="CB00 / COBIL00C: pay account balance",
    description=PAY_BILL_DESCRIPTION,
    responses=LegacyErrorResponses(400, 401, 403, 404, 409, 503),
)
async def PayBill(
    payRequest: BillPayRequest,
    context: Annotated[OnlineContext, Depends(GetRegularContext)],
) -> BillPayResponse:
    """Process ENTER on COBIL00 (COBIL00C PROCESS-ENTER-KEY) and return the screen sent back."""
    return await cobil00c.PayBill(context, payRequest)
