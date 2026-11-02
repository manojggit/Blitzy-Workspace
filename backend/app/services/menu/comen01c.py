"""Replaces: COMEN01C (CM00), BMS COMEN01, copybooks COMEN02Y, COCOM01Y, CSUSR01Y.

The main menu of regular users. An option's program is reached through the returned frontend
route instead of ``EXEC CICS XCTL``.

Legacy paths with no code here: ``EIBCALEN = 0`` (back to sign-on) is the session guard
``RequireRegular`` with its 401; PF3 (``XCTL COSGN00C``) is the frontend's ``POST /auth/logout``;
other AID keys cannot reach the server, because the menu page offers only ENTER and F3;
POPULATE-HEADER-INFO is the frontend ScreenShell; the ``MOVE WS-OPTION TO OPTIONO`` echo is not
part of the response.
"""

from app.core.dependencies import OnlineContext
from app.core.errors import HTTP_ACCESS_REFUSED, LegacyMessageError
from app.core.messages import ADMIN_ONLY_MESSAGE
from app.schemas.menu import MenuResponse, MenuSelectionRequest, MenuSelectionResponse
from app.services.menu.menu_options import (
    MAIN_MENU_OPTIONS,
    MENU_LABEL_LENGTH,
    OPTION_CURSOR_FIELD,
    OPTION_USER_TYPE_ADMIN_ONLY,
    BuildMenuResponse,
    MenuOption,
    SelectOption,
)

# COCOM01Y line 28, 88 CDEMO-USRTYP-USER VALUE 'U': the only user type refused an 'A' option.
REGULAR_USER_TYPE = "U"
# COMEN01C line 147: the program whose presence EXEC CICS INQUIRE PROGRAM checks.
AUTHORIZATION_PROGRAM_NAME = "COPAUS0C"
# COMEN01C line 169, CDEMO-MENU-OPT-PGMNAME(WS-OPTION)(1:5) = 'DUMMY'.
DUMMY_PROGRAM_PREFIX = "DUMMY"
# The STRING operands of COMEN01C lines 163-166 and 172-175, spaces included.
OPTION_MESSAGE_PREFIX = "This option "
NOT_INSTALLED_SUFFIX = " is not installed..."
COMING_SOON_SUFFIX = "is coming soon ..."
# DELIMITED BY '  ' (line 165) and DELIMITED BY SPACE (line 174).
NOT_INSTALLED_DELIMITER = "  "
WORD_DELIMITER = " "
# An option that navigates leaves the message line blank (MOVE SPACES TO WS-MESSAGE, line 79).
NO_MESSAGE = ""


def GetMainMenu(context: OnlineContext) -> MenuResponse:
    """List all 11 options (SEND-MENU-SCREEN and BUILD-MENU-OPTIONS, lines 87-90, 262-303).

    ``context`` is not read; it keeps the router contract both menu services share.
    """
    return BuildMenuResponse(MAIN_MENU_OPTIONS)


def SelectMainOption(
    context: OnlineContext, request: MenuSelectionRequest
) -> MenuSelectionResponse:
    """Port PROCESS-ENTER-KEY (lines 115-191); the first failing check ends processing.

    An invalid option raises 400 and an admin-only refusal 403. Otherwise the response carries
    the option's route, or ``route`` None with the not-installed or coming-soon message. The
    response has no severity. The legacy ERRMSG is red by default (BMS COLOR=RED) and for the
    not-installed message (DFHRED, line 162); only coming-soon, which no active option reaches,
    is green (DFHGREEN, line 171). The menu page therefore shows a 4xx message, and a 200
    response with ``route`` None, as an error.
    """
    option = SelectOption(request.option, MAIN_MENU_OPTIONS)
    _CheckAdminOnlyOption(context, option)
    return _DispatchOption(option)


def _CheckAdminOnlyOption(context: OnlineContext, option: MenuOption) -> None:
    """Refuse an option flagged ``A`` to a type ``U`` user (lines 136-143).

    The test is CDEMO-USRTYP-USER, exactly ``U``, so ``u``, ``X`` and the other regular types
    pass, as does a context with no user. No active COMEN02Y entry is flagged ``A``.
    """
    isTypeUUser = context.user is not None and context.user.user_type == REGULAR_USER_TYPE
    if isTypeUUser and option.userType == OPTION_USER_TYPE_ADMIN_ONLY:
        raise LegacyMessageError(
            ADMIN_ONLY_MESSAGE, field=OPTION_CURSOR_FIELD, httpStatus=HTTP_ACCESS_REFUSED
        )


def _DispatchOption(option: MenuOption) -> MenuSelectionResponse:
    """Port the EVALUATE of lines 145-188: COPAUS0C, then DUMMY programs, then XCTL."""
    if option.programName == AUTHORIZATION_PROGRAM_NAME and option.route is None:
        # The INQUIRE PROGRAM fails: COPAUS0C is in the excluded authorization module.
        return MenuSelectionResponse(route=None, message=_BuildNotInstalledMessage(option))
    if option.programName.startswith(DUMMY_PROGRAM_PREFIX):
        return MenuSelectionResponse(route=None, message=_BuildComingSoonMessage(option))
    return MenuSelectionResponse(route=option.route, message=NO_MESSAGE)


def _BuildNotInstalledMessage(option: MenuOption) -> str:
    """Port the STRING of lines 161-167: the X(35) option name up to its first two spaces."""
    paddedLabel = option.label.ljust(MENU_LABEL_LENGTH)
    namePart = paddedLabel.partition(NOT_INSTALLED_DELIMITER)[0]
    return OPTION_MESSAGE_PREFIX + namePart + NOT_INSTALLED_SUFFIX


def _BuildComingSoonMessage(option: MenuOption) -> str:
    """Port the STRING of lines 170-176: the name up to its first space, with no separator."""
    paddedLabel = option.label.ljust(MENU_LABEL_LENGTH)
    firstWord = paddedLabel.partition(WORD_DELIMITER)[0]
    return OPTION_MESSAGE_PREFIX + firstWord + COMING_SOON_SUFFIX
