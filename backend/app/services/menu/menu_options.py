"""Replaces: copybooks COMEN02Y (main menu options), COADM02Y (admin menu options); option edit
of COMEN01C (CM00) and COADM01C (CA00).

The tables keep the legacy option numbers, labels and program names in copybook order. ``route``
is the frontend page that replaces ``XCTL PROGRAM(...)``; it is ``None`` where the target program
belongs to an excluded module (COPAUS0C, COTRTLIC, COTRTUPC), which the menu services answer
with the legacy "not installed" message.
"""

from dataclasses import dataclass

from app.core.errors import HTTP_EDIT_FAILURE, LegacyMessageError
from app.schemas.menu import OPTION_FIELD_LENGTH, OPTION_LABEL_LENGTH, MenuResponse
from app.schemas.menu import MenuOption as MenuOptionModel

# CDEMO-MENU-OPT-NAME and CDEMO-ADMIN-OPT-NAME PIC X(35). The BMS OPTION field length (2) is
# OPTION_FIELD_LENGTH, shared with the request schema so both stay one definition.
MENU_LABEL_LENGTH = OPTION_LABEL_LENGTH
# The MenuSelectionRequest field that takes the cursor, replacing IC on the OPTION map field.
OPTION_CURSOR_FIELD = "option"
# CDEMO-MENU-OPT-USRTYPE values; COMEN01C line 137 refuses an 'A' option to a 'U' user.
OPTION_USER_TYPE_REGULAR = "U"
OPTION_USER_TYPE_ADMIN_ONLY = "A"
# COMEN01C line 131 and COADM01C line 135.
INVALID_OPTION_MESSAGE = "Please enter a valid option number..."
# The NUMERIC class test accepts only the EBCDIC digits; str.isdigit() would also accept
# Unicode digits such as a superscript two, which code page 037 can carry.
OPTION_DIGITS = frozenset("0123456789")


@dataclass(frozen=True)
class MenuOption:
    """One CDEMO-MENU-OPT (COMEN02Y) or CDEMO-ADMIN-OPT (COADM02Y) table entry.

    ``label`` is OPT-NAME X(35) right-trimmed. ``userType`` is OPT-USRTYPE X(01), and ``""`` for
    admin entries, whose layout has no such field.
    """

    number: int
    label: str
    programName: str
    userType: str
    route: str | None


# COMEN02Y lines 25-90, CDEMO-MENU-OPT-COUNT VALUE 11. Option 8 uses the active label; the
# "Transaction Add (Admin Only)" text on line 69 is a comment.
MAIN_MENU_OPTIONS: tuple[MenuOption, ...] = (
    MenuOption(1, "Account View", "COACTVWC", OPTION_USER_TYPE_REGULAR, "/accounts/view"),
    MenuOption(2, "Account Update", "COACTUPC", OPTION_USER_TYPE_REGULAR, "/accounts/update"),
    MenuOption(3, "Credit Card List", "COCRDLIC", OPTION_USER_TYPE_REGULAR, "/cards"),
    MenuOption(4, "Credit Card View", "COCRDSLC", OPTION_USER_TYPE_REGULAR, "/cards/view"),
    MenuOption(5, "Credit Card Update", "COCRDUPC", OPTION_USER_TYPE_REGULAR, "/cards/update"),
    MenuOption(6, "Transaction List", "COTRN00C", OPTION_USER_TYPE_REGULAR, "/transactions"),
    MenuOption(7, "Transaction View", "COTRN01C", OPTION_USER_TYPE_REGULAR, "/transactions/view"),
    MenuOption(8, "Transaction Add", "COTRN02C", OPTION_USER_TYPE_REGULAR, "/transactions/add"),
    MenuOption(
        9, "Transaction Reports", "CORPT00C", OPTION_USER_TYPE_REGULAR, "/reports/transactions"
    ),
    MenuOption(10, "Bill Payment", "COBIL00C", OPTION_USER_TYPE_REGULAR, "/billpay"),
    # COPAUS0C is in the excluded authorization module: the failed EXEC CICS INQUIRE PROGRAM.
    MenuOption(11, "Pending Authorization View", "COPAUS0C", OPTION_USER_TYPE_REGULAR, None),
)

# COADM02Y lines 26-53, CDEMO-ADMIN-OPT-COUNT VALUE 6 (the VALUE 4 line is a comment).
ADMIN_MENU_OPTIONS: tuple[MenuOption, ...] = (
    MenuOption(1, "User List (Security)", "COUSR00C", "", "/admin/users"),
    MenuOption(2, "User Add (Security)", "COUSR01C", "", "/admin/users/add"),
    MenuOption(3, "User Update (Security)", "COUSR02C", "", "/admin/users/update"),
    MenuOption(4, "User Delete (Security)", "COUSR03C", "", "/admin/users/delete"),
    # COTRTLIC and COTRTUPC are in the excluded Db2 module: the CICS PGMIDERR condition.
    MenuOption(5, "Transaction Type List/Update (Db2)", "COTRTLIC", "", None),
    MenuOption(6, "Transaction Type Maintenance (Db2)", "COTRTUPC", "", None),
)


def _JustifyOptionField(fieldText: str) -> str:
    """Port the scan, JUST RIGHT move and INSPECT of COMEN01C lines 117-123 (COADM01C 121-127).

    The backward scan stops at position 1, so an all-space field moves one space. Only the space
    character is significant, as in the COBOL comparison with SPACES.
    """
    significantText = fieldText.rstrip(" ") or fieldText[:1]
    justifiedText = significantText.rjust(OPTION_FIELD_LENGTH)
    return justifiedText.replace(" ", "0")


def ParseOptionNumber(optionText: str, optionCount: int) -> int | None:
    """Port the option edit of COMEN01C lines 117-134 and COADM01C lines 121-138.

    Return the option number, or ``None`` where the program sets ``INVALID_OPTION_MESSAGE``: a
    non-numeric, zero or out-of-range option. Text longer than the 2-byte OPTION field cannot
    come from the map, so it is rejected rather than truncated.
    """
    if len(optionText) > OPTION_FIELD_LENGTH:
        return None
    zeroFilledText = _JustifyOptionField(optionText.ljust(OPTION_FIELD_LENGTH))
    if not all(character in OPTION_DIGITS for character in zeroFilledText):
        return None
    optionNumber = int(zeroFilledText)
    if optionNumber == 0 or optionNumber > optionCount:
        return None
    return optionNumber


def SelectOption(optionText: str, options: tuple[MenuOption, ...]) -> MenuOption:
    """Return the entry the keyed option selects (COMEN01C lines 117-134, COADM01C 121-138).

    The option count is the table length, the copybook OPT-COUNT. An invalid option raises
    ``INVALID_OPTION_MESSAGE`` and ends processing: the COBOL falls through to tests that
    subscript the table with the invalid number, which is undefined, so nothing after it runs.
    """
    optionNumber = ParseOptionNumber(optionText, len(options))
    if optionNumber is None:
        raise LegacyMessageError(
            INVALID_OPTION_MESSAGE, field=OPTION_CURSOR_FIELD, httpStatus=HTTP_EDIT_FAILURE
        )
    return options[optionNumber - 1]


def BuildMenuResponse(options: tuple[MenuOption, ...]) -> MenuResponse:
    """List every table entry in order (BUILD-MENU-OPTIONS, COMEN01C 262-303, COADM01C 229-266).

    The legacy ``NN. `` prefix is not added to the label: the frontend shows the number apart.
    """
    return MenuResponse(
        options=[MenuOptionModel(number=option.number, label=option.label) for option in options]
    )
