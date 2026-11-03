"""Unit tests for app.services.menu.coadm01c, the port of COADM01C (CA00).

COADM01C sends map COADM01 (COADM1A) listing the option table of copybook COADM02Y, and on
ENTER edits the keyed OPTION field, then transfers control to the option's program. Expected
values are typed from the source lines:
- the six COADM02Y entries (lines 26-53, CDEMO-ADMIN-OPT-COUNT VALUE 6), labels right-trimmed
    from PIC X(35);
- the option edit of PROCESS-ENTER-KEY (lines 121-138): JUST RIGHT, zero fill, NUMERIC, not
    zero and not above the count, else ``Please enter a valid option number...``;
- the not-installed STRING of lines 150-156 (DUMMY program) and PGMIDERR-ERR-PARA (lines
    270-276, absent program), whose option-name operand is commented out;
- no user-type check anywhere in COADM01C: the router's admin guard is tested with the API.

The service reads nothing from its context, so a frozen stand-in is passed and no database or
HTTP client is used.
"""

from dataclasses import dataclass

import pytest

from app.core.dependencies import CurrentUser
from app.core.errors import LegacyMessageError
from app.schemas.menu import MenuSelectionRequest, MenuSelectionResponse
from app.services.menu import coadm01c
from app.services.menu.menu_options import MenuOption

# COADM02Y CDEMO-ADMIN-OPT-NAME values in table order, trailing spaces trimmed.
EXPECTED_ADMIN_LABELS = (
    "User List (Security)",
    "User Add (Security)",
    "User Update (Security)",
    "User Delete (Security)",
    "Transaction Type List/Update (Db2)",
    "Transaction Type Maintenance (Db2)",
)
# CDEMO-ADMIN-OPT-COUNT VALUE 6 (COADM02Y line 22; the VALUE 4 line is a comment).
EXPECTED_OPTION_NUMBERS = (1, 2, 3, 4, 5, 6)
# The pages that replace XCTL to COUSR00C, COUSR01C, COUSR02C and COUSR03C.
EXPECTED_ROUTES = (
    ("1", "/admin/users"),
    ("2", "/admin/users/add"),
    ("3", "/admin/users/update"),
    ("4", "/admin/users/delete"),
)
# COTRTLIC and COTRTUPC belong to the excluded Db2 module: the CICS PGMIDERR condition.
NOT_INSTALLED_OPTIONS = ("5", "6")
# 'This option ' + 'is not installed ...' with the name operand commented out (lines 152-155).
NOT_INSTALLED_MESSAGE = "This option is not installed ..."
# COADM01C line 135.
INVALID_OPTION_MESSAGE = "Please enter a valid option number..."
# Zero, above the count of 6, blank (zero-filled to 00) and non-numeric.
INVALID_OPTION_TEXTS = ("0", "7", "", "x")
# Forms of option 1 that the JUST RIGHT move and the space-to-zero INSPECT turn into 01.
JUSTIFIED_FIRST_OPTION_TEXTS = ("01", " 1", "1 ")
FIRST_OPTION_ROUTE = "/admin/users"
# The edit failure keeps the cursor on the OPTION field (MOVE -1 TO OPTIONL).
OPTION_CURSOR_FIELD = "option"
EDIT_FAILURE_STATUS = 400
# An option that navigates sends no ERRMSG text.
NO_MESSAGE = ""
# A synthetic signed-on user ID; the menu never reads it.
CONTEXT_USER_ID = "MENUTEST"
ADMIN_USER_TYPE = "A"
REGULAR_USER_TYPE = "U"
# A table whose option 1 names a DUMMY program: line 141 skips the XCTL even with a route.
DUMMY_OPTION_LABEL = "Dummy Admin Option"
DUMMY_ADMIN_OPTIONS = (
    MenuOption(1, DUMMY_OPTION_LABEL, "DUMMYPGM", "", FIRST_OPTION_ROUTE),
    MenuOption(2, "User Add (Security)", "COUSR01C", "", "/admin/users/add"),
)


@dataclass(frozen=True)
class FakeMenuContext:
    """The part of OnlineContext a menu request carries: the verified session user."""

    user: CurrentUser


def BuildContext(userType: str) -> FakeMenuContext:
    """Return a menu context whose session user has the given user type."""
    return FakeMenuContext(user=CurrentUser(user_id=CONTEXT_USER_ID, user_type=userType))


def SelectAsAdmin(optionText: str) -> MenuSelectionResponse:
    """Send ENTER with the keyed option text as an admin and return the service's response."""
    request = MenuSelectionRequest(option=optionText)
    return coadm01c.SelectAdminOption(BuildContext(ADMIN_USER_TYPE), request)


def test_admin_menu_lists_the_six_copybook_options_in_table_order() -> None:
    response = coadm01c.GetAdminMenu(BuildContext(ADMIN_USER_TYPE))

    assert tuple(option.number for option in response.options) == EXPECTED_OPTION_NUMBERS
    assert tuple(option.label for option in response.options) == EXPECTED_ADMIN_LABELS


@pytest.mark.parametrize(("optionText", "expectedRoute"), EXPECTED_ROUTES)
def test_security_options_route_to_their_user_pages(optionText: str, expectedRoute: str) -> None:
    response = SelectAsAdmin(optionText)

    assert response.route == expectedRoute
    assert response.message == NO_MESSAGE


@pytest.mark.parametrize("optionText", JUSTIFIED_FIRST_OPTION_TEXTS)
def test_option_text_is_right_justified_and_zero_filled_before_the_edit(optionText: str) -> None:
    response = SelectAsAdmin(optionText)

    assert response.route == FIRST_OPTION_ROUTE
    assert response.message == NO_MESSAGE


@pytest.mark.parametrize("optionText", NOT_INSTALLED_OPTIONS)
def test_db2_options_answer_not_installed_without_navigating(optionText: str) -> None:
    response = SelectAsAdmin(optionText)

    assert response.route is None
    assert response.message == NOT_INSTALLED_MESSAGE


def test_dummy_program_answers_not_installed_without_the_option_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coadm01c, "ADMIN_MENU_OPTIONS", DUMMY_ADMIN_OPTIONS)

    response = SelectAsAdmin("1")

    assert response.route is None
    assert response.message == NOT_INSTALLED_MESSAGE
    assert DUMMY_OPTION_LABEL not in response.message


@pytest.mark.parametrize("optionText", INVALID_OPTION_TEXTS)
def test_invalid_option_is_refused_with_the_cursor_on_the_option_field(optionText: str) -> None:
    with pytest.raises(LegacyMessageError) as excInfo:
        SelectAsAdmin(optionText)

    assert excInfo.value.httpStatus == EDIT_FAILURE_STATUS
    assert excInfo.value.message == INVALID_OPTION_MESSAGE
    assert excInfo.value.Body()["cursor_field"] == OPTION_CURSOR_FIELD


@pytest.mark.parametrize("userType", (ADMIN_USER_TYPE, REGULAR_USER_TYPE))
def test_selection_makes_no_user_type_check(userType: str) -> None:
    request = MenuSelectionRequest(option="1")

    response = coadm01c.SelectAdminOption(BuildContext(userType), request)

    assert response.route == FIRST_OPTION_ROUTE
    assert response.message == NO_MESSAGE
