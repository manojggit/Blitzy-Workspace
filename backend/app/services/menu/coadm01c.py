"""Replaces: COADM01C (CA00), BMS COADM01, copybooks COADM02Y, COCOM01Y, CSUSR01Y.

The CardDemo admin menu. An option's program is reached by the returned frontend route instead
of ``EXEC CICS XCTL``, and the ``PGMIDERR`` handler becomes the not-installed message.

The rest of MAIN-PARA has no code here. ``EIBCALEN = 0`` (lines 86-88) is the session guard's
401, which sends the client to ``/signon``; PF3 (lines 100-102) is the page's call to
``POST /auth/logout``; any other AID key (lines 103-106) cannot be sent, because the page offers
only ENTER and F3; and POPULATE-HEADER-INFO (lines 205-224) is the frontend ``ScreenShell``.
"""

from app.core.dependencies import OnlineContext
from app.schemas.menu import MenuResponse, MenuSelectionRequest, MenuSelectionResponse
from app.services.menu.menu_options import (
    ADMIN_MENU_OPTIONS,
    BuildMenuResponse,
    MenuOption,
    SelectOption,
)

# CDEMO-ADMIN-OPT-PGMNAME(WS-OPTION)(1:5) NOT = 'DUMMY' (line 141) skips the XCTL.
DUMMY_PROGRAM_PREFIX = "DUMMY"
# No option name: its STRING operand is commented out in COADM01C lines 152-155 and 273-276.
NOT_INSTALLED_MESSAGE = "This option is not installed ..."


def GetAdminMenu(context: OnlineContext) -> MenuResponse:
    """List the six COADM02Y options (SEND-MENU-SCREEN, BUILD-MENU-OPTIONS, lines 91-94, 229-266).

    ``context`` keeps the shared router contract; the admin menu reads nothing from it.
    """
    return BuildMenuResponse(ADMIN_MENU_OPTIONS)


def _IsInstalledProgram(option: MenuOption) -> bool:
    """Return whether the XCTL of lines 141-148 transfers control to the option's program.

    A DUMMY program skips the XCTL (line 141). A program with no route is not part of this
    application: the CICS PGMIDERR condition that HANDLE CONDITION (lines 77-79) sends to
    PGMIDERR-ERR-PARA.
    """
    if option.programName.startswith(DUMMY_PROGRAM_PREFIX):
        return False
    return option.route is not None


def SelectAdminOption(
    context: OnlineContext, request: MenuSelectionRequest
) -> MenuSelectionResponse:
    """Port PROCESS-ENTER-KEY (lines 119-158) and PGMIDERR-ERR-PARA (lines 270-283).

    The option edit of lines 121-138 runs in ``SelectOption``, whose ``LegacyMessageError``
    (400, ``Please enter a valid option number...``, cursor ``option``) ends processing; the
    frontend shows it with error severity, the ERRMSG default red. COADM01C makes no user-type
    check: the router's admin guard runs before this call, and ``context`` is not read.

    An installed program answers its route with an empty message, replacing the XCTL. A DUMMY
    or absent program answers ``route=None`` with ``NOT_INSTALLED_MESSAGE``, which lines 151
    and 272 colour DFHGREEN: the frontend shows that 200 response with non-error severity.
    """
    option = SelectOption(request.option, ADMIN_MENU_OPTIONS)
    if _IsInstalledProgram(option):
        return MenuSelectionResponse(route=option.route, message="")
    return MenuSelectionResponse(route=None, message=NOT_INSTALLED_MESSAGE)
