"""Replaces: COMEN01C (CM00), BMS COMEN01, copybook COMEN02Y; COADM01C (CA00), BMS COADM01,
copybook COADM02Y.

The main menu (CM00) and the admin menu (CA00) of csd/CARDDEMO.CSD. ``GET`` lists the option
table; ``POST .../selection`` replaces ENTER on the OPTION field, and the chosen program's
``EXEC CICS XCTL`` becomes the ``route`` of ``MenuSelectionResponse``. The menu services own
every literal and every edit; they do no I/O, so they are called without ``await``.

CM00 serves regular users (any type other than exactly ``A``) through ``GetRegularContext``,
which refuses an admin with 403 and an empty message. CA00 serves admins (type exactly ``A``)
through ``GetAdminContext``, which refuses anyone else with 403 and the COMEN01C literal
``No access - Admin Only option... ``. A POST without an allow-listed Origin is refused with 403
by ``core.middleware.OriginGuard`` before it reaches a handler.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.v1 import LegacyErrorResponses, SessionFirstRoute
from app.core.dependencies import GetAdminContext, GetRegularContext, OnlineContext
from app.schemas.menu import MenuResponse, MenuSelectionRequest, MenuSelectionResponse
from app.services.menu import coadm01c, comen01c

ROUTER = APIRouter(prefix="/menu", tags=["menu"], route_class=SessionFirstRoute)

MAIN_MENU_DESCRIPTION = (
    "Lists the 11 options of the COMEN02Y table in table order, as COMEN01C builds them on map"
    " COMEN01, including option 11, whose program is not installed. Regular users only."
)
MAIN_SELECTION_DESCRIPTION = (
    "Runs COMEN01C's option edit on the keyed OPTION text. The response carries the frontend"
    " route of the chosen option, or a null route with the legacy not-installed or coming-soon"
    " message when the option has no installed program."
)
ADMIN_MENU_DESCRIPTION = (
    "Lists the 6 options of the COADM02Y table in table order, as COADM01C builds them on map"
    " COADM01, including options 5 and 6, whose programs are not installed. Admins only."
)
ADMIN_SELECTION_DESCRIPTION = (
    "Runs COADM01C's option edit on the keyed OPTION text. The response carries the frontend"
    " route of the chosen option, or a null route with the legacy not-installed message when"
    " the option has no installed program."
)


@ROUTER.get(
    "/main",
    response_model=MenuResponse,
    summary="CM00 / COMEN01C: main menu options",
    description=MAIN_MENU_DESCRIPTION,
    responses=LegacyErrorResponses(401, 403),
)
async def GetMainMenu(
    context: Annotated[OnlineContext, Depends(GetRegularContext)],
) -> MenuResponse:
    """Return the main menu option table (COMEN01C SEND-MENU-SCREEN)."""
    return comen01c.GetMainMenu(context)


@ROUTER.post(
    "/main/selection",
    response_model=MenuSelectionResponse,
    summary="CM00 / COMEN01C: select a main menu option",
    description=MAIN_SELECTION_DESCRIPTION,
    responses=LegacyErrorResponses(400, 401, 403),
)
async def SelectMainMenuOption(
    selectionRequest: MenuSelectionRequest,
    context: Annotated[OnlineContext, Depends(GetRegularContext)],
) -> MenuSelectionResponse:
    """Process ENTER on COMEN01 (COMEN01C PROCESS-ENTER-KEY)."""
    return comen01c.SelectMainOption(context, selectionRequest)


@ROUTER.get(
    "/admin",
    response_model=MenuResponse,
    summary="CA00 / COADM01C: admin menu options",
    description=ADMIN_MENU_DESCRIPTION,
    responses=LegacyErrorResponses(401, 403),
)
async def GetAdminMenu(
    context: Annotated[OnlineContext, Depends(GetAdminContext)],
) -> MenuResponse:
    """Return the admin menu option table (COADM01C SEND-MENU-SCREEN)."""
    return coadm01c.GetAdminMenu(context)


@ROUTER.post(
    "/admin/selection",
    response_model=MenuSelectionResponse,
    summary="CA00 / COADM01C: select an admin menu option",
    description=ADMIN_SELECTION_DESCRIPTION,
    responses=LegacyErrorResponses(400, 401, 403),
)
async def SelectAdminMenuOption(
    selectionRequest: MenuSelectionRequest,
    context: Annotated[OnlineContext, Depends(GetAdminContext)],
) -> MenuSelectionResponse:
    """Process ENTER on COADM01 (COADM01C PROCESS-ENTER-KEY)."""
    return coadm01c.SelectAdminOption(context, selectionRequest)
