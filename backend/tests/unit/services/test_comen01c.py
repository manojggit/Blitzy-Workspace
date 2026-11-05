"""Unit tests for app.services.menu.comen01c, the port of COMEN01C (transaction CM00).

COMEN01C lists the 11 options of copybook COMEN02Y on map COMEN01. On ENTER, PROCESS-ENTER-KEY
(cbl/COMEN01C.cbl lines 117-187) right-trims the keyed option, turns its spaces into zeros and
refuses a non-numeric, zero or above-11 value. It refuses an option flagged A to a user of type
exactly U (COCOM01Y 88 CDEMO-USRTYP-USER), answers COPAUS0C with "not installed" and a DUMMY
program with "coming soon", and otherwise transfers control, which the port answers with the
option's frontend route. Expected values are typed from those lines and from COMEN02Y lines
24-90. The service is synchronous and does no I/O, so the context is an in-file fake and no
database is needed.
"""

import secrets
from dataclasses import dataclass

import pytest

from app.core.dependencies import CurrentUser
from app.core.errors import LegacyMessageError
from app.schemas.menu import MenuSelectionRequest, MenuSelectionResponse
from app.services.menu import comen01c
from app.services.menu.menu_options import MenuOption

# COMEN02Y lines 24-90: CDEMO-MENU-OPT-NAME X(35), right-trimmed, in table order. Option 8 is the
# active label; the "Transaction Add (Admin Only)" line above it is a comment.
EXPECTED_MAIN_LABELS = (
    "Account View",
    "Account Update",
    "Credit Card List",
    "Credit Card View",
    "Credit Card Update",
    "Transaction List",
    "Transaction View",
    "Transaction Add",
    "Transaction Reports",
    "Bill Payment",
    "Pending Authorization View",
)
# COMEN02Y CDEMO-MENU-OPT-COUNT VALUE 11.
EXPECTED_OPTION_COUNT = 11
TRANSACTION_ADD_OPTION = 8
TRANSACTION_ADD_LABEL = "Transaction Add"
# The frontend pages that replace XCTL PROGRAM(CDEMO-MENU-OPT-PGMNAME(WS-OPTION)).
EXPECTED_ROUTES = {
    1: "/accounts/view",
    2: "/accounts/update",
    3: "/cards",
    4: "/cards/view",
    5: "/cards/update",
    6: "/transactions",
    7: "/transactions/view",
    8: "/transactions/add",
    9: "/reports/transactions",
    10: "/billpay",
}
ACCOUNT_VIEW_ROUTE = "/accounts/view"
# Option 11 names COPAUS0C, whose INQUIRE PROGRAM fails: the authorization module is excluded.
PENDING_AUTHORIZATION_OPTION = "11"
FIRST_OPTION = "1"

# Line 131: a non-numeric, above-count or zero WS-OPTION.
INVALID_OPTION_MESSAGE = "Please enter a valid option number..."
# Lines 161-167: the option name DELIMITED BY '  ', then ' is not installed...'.
NOT_INSTALLED_MESSAGE = "This option Pending Authorization View is not installed..."
# Lines 139-140, with the literal's trailing space.
ADMIN_ONLY_MESSAGE = "No access - Admin Only option... "
# Lines 170-176 for "Future Option": DELIMITED BY SPACE keeps "Future", with no separator.
COMING_SOON_MESSAGE = "This option Futureis coming soon ..."
# Line 79, MOVE SPACES TO WS-MESSAGE: an option that navigates leaves the message line blank.
NO_MESSAGE = ""
# The request field replacing the IC attribute of the BMS OPTION field.
OPTION_CURSOR_FIELD = "option"
INVALID_OPTION_STATUS = 400
ADMIN_ONLY_STATUS = 403

# COCOM01Y 88 CDEMO-USRTYP-USER VALUE 'U': the only type refused an option flagged A.
REGULAR_USER_TYPE = "U"
# Lower-case u, admin A and any other stored type fail the exact CDEMO-USRTYP-USER test.
NOT_EXACTLY_U_USER_TYPES = ("u", "A", "X")
# Hypothetical table entries: no active COMEN02Y entry is flagged A or names a DUMMY program.
ADMIN_ONLY_OPTION = MenuOption(1, "Secret Option", "COACTVWC", "A", ACCOUNT_VIEW_ROUTE)
COMING_SOON_OPTION = MenuOption(1, "Future Option", "DUMMY001", REGULAR_USER_TYPE, None)


@dataclass(frozen=True)
class FakeMenuContext:
    """The only OnlineContext attribute the COMEN01C port reads: the signed-on user."""

    user: CurrentUser


def BuildContext(userType: str) -> FakeMenuContext:
    """Return a context whose user has ``userType`` and a random eight-character user ID."""
    randomUserId = secrets.token_hex(4).upper()
    return FakeMenuContext(user=CurrentUser(userId=randomUserId, userType=userType))


def SelectAsUser(optionText: str, userType: str = REGULAR_USER_TYPE) -> MenuSelectionResponse:
    """Press ENTER on COMEN01 with ``optionText`` keyed by a user of ``userType``."""
    request = MenuSelectionRequest(option=optionText)
    return comen01c.SelectMainOption(BuildContext(userType), request)


def PatchFirstOption(monkeypatch: pytest.MonkeyPatch, option: MenuOption) -> None:
    """Make ``option`` entry 1 of the main-menu table; the option count stays 11."""
    patchedTable = (option, *comen01c.MAIN_MENU_OPTIONS[1:])
    monkeypatch.setattr(comen01c, "MAIN_MENU_OPTIONS", patchedTable)


def test_main_menu_lists_the_eleven_copybook_options_in_order():
    menu = comen01c.GetMainMenu(BuildContext(REGULAR_USER_TYPE))
    optionNumbers = [option.number for option in menu.options]
    optionLabels = tuple(option.label for option in menu.options)
    assert optionNumbers == list(range(1, EXPECTED_OPTION_COUNT + 1))
    assert optionLabels == EXPECTED_MAIN_LABELS
    assert menu.options[TRANSACTION_ADD_OPTION - 1].label == TRANSACTION_ADD_LABEL


@pytest.mark.parametrize("optionText", ["1", " 1", "1 ", "01"])
def test_trimmed_and_zero_filled_forms_of_option_one_open_account_view(optionText):
    response = SelectAsUser(optionText)
    assert response.route == ACCOUNT_VIEW_ROUTE
    assert response.message == NO_MESSAGE


@pytest.mark.parametrize("optionText", ["", "0", "00", "12", "a", "1a"])
def test_blank_zero_out_of_range_or_non_numeric_option_is_refused(optionText):
    with pytest.raises(LegacyMessageError) as excInfo:
        SelectAsUser(optionText)
    assert excInfo.value.message == INVALID_OPTION_MESSAGE
    assert excInfo.value.httpStatus == INVALID_OPTION_STATUS
    assert excInfo.value.Body()["cursor_field"] == OPTION_CURSOR_FIELD


@pytest.mark.parametrize(("optionNumber", "expectedRoute"), sorted(EXPECTED_ROUTES.items()))
def test_each_installed_option_returns_its_page_route(optionNumber, expectedRoute):
    response = SelectAsUser(str(optionNumber))
    assert response.route == expectedRoute
    assert response.message == NO_MESSAGE


def test_pending_authorization_option_stays_on_the_menu_as_not_installed():
    response = SelectAsUser(PENDING_AUTHORIZATION_OPTION)
    assert response.route is None
    assert response.message == NOT_INSTALLED_MESSAGE


def test_option_flagged_admin_only_is_refused_to_a_type_u_user(monkeypatch):
    PatchFirstOption(monkeypatch, ADMIN_ONLY_OPTION)
    with pytest.raises(LegacyMessageError) as excInfo:
        SelectAsUser(FIRST_OPTION, REGULAR_USER_TYPE)
    assert excInfo.value.message == ADMIN_ONLY_MESSAGE
    assert excInfo.value.httpStatus == ADMIN_ONLY_STATUS


@pytest.mark.parametrize("userType", NOT_EXACTLY_U_USER_TYPES)
def test_option_flagged_admin_only_opens_for_a_type_other_than_exactly_u(monkeypatch, userType):
    PatchFirstOption(monkeypatch, ADMIN_ONLY_OPTION)
    response = SelectAsUser(FIRST_OPTION, userType)
    assert response.route == ACCOUNT_VIEW_ROUTE
    assert response.message == NO_MESSAGE


def test_dummy_program_option_is_coming_soon_with_the_first_word_of_its_name(monkeypatch):
    PatchFirstOption(monkeypatch, COMING_SOON_OPTION)
    response = SelectAsUser(FIRST_OPTION)
    assert response.route is None
    assert response.message == COMING_SOON_MESSAGE
