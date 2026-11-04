"""Integration tests for the main menu CM00 / COMEN01C and the admin menu CA00 / COADM01C.

Replaces: COMEN01C (CM00) with map COMEN01 and option table COMEN02Y, and COADM01C (CA00) with
map COADM01 and option table COADM02Y. Every request goes through the ASGI app against the test
database, signed on by the fixtures of tests/conftest.py; no test writes beyond those fixtures'
sign-on users.

The cases assert:

- the option tables, numbered from 1 in copybook order with the X(35) names right-trimmed:
  COMEN02Y's 11 entries (option 8 is the active ``Transaction Add``; the ``(Admin Only)`` text
  is a comment) and COADM02Y's 6 (CDEMO-ADMIN-OPT-COUNT VALUE 6);
- PROCESS-ENTER-KEY's option edit (cbl/COMEN01C.cbl lines 117-134, cbl/COADM01C.cbl lines
  121-138): the OPTION text (BMS LENGTH=2, JUSTIFY=(RIGHT,ZERO)) is right-justified and
  zero-filled, and a non-numeric, zero or out-of-range value is refused with
  ``Please enter a valid option number...`` and the cursor on ``option``;
- the frontend route that replaces each installed option's ``EXEC CICS XCTL``;
- main-menu option 11, whose COPAUS0C ``INQUIRE PROGRAM`` fails (lines 145-167), and admin
  options 5-6, whose COTRTLIC and COTRTUPC raise PGMIDERR (COADM01C lines 268-276; the option
  name operand of that STRING is commented out), answered with their "not installed" literals;
- the role guards: only user type exactly ``A`` reaches CA00, every other type reaches CM00;
- a session-less request is 401, and a selection without an allow-listed Origin is 403.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

API_V1 = "/api/v1"
MAIN_MENU = "main"
ADMIN_MENU = "admin"
SELECTION_SUFFIX = "/selection"
MENU_METHOD_LIST = "GET"
MENU_METHOD_SELECT = "POST"
MENU_METHODS = (MENU_METHOD_LIST, MENU_METHOD_SELECT)

HTTP_OK = 200
HTTP_EDIT_FAILURE = 400
HTTP_SIGN_ON_FAILURE = 401
HTTP_ACCESS_REFUSED = 403
HTTP_MALFORMED = 422

# COCOM01Y 88 CDEMO-USRTYP-ADMIN VALUE 'A' and 88 CDEMO-USRTYP-USER VALUE 'U': COSGN00C sends
# type A to COADM01C and every other type to COMEN01C (cbl/COSGN00C.cbl lines 230-239).
ADMIN_USER_TYPE = "A"
REGULAR_USER_TYPE = "U"
MENU_OWNER_TYPES = {MAIN_MENU: REGULAR_USER_TYPE, ADMIN_MENU: ADMIN_USER_TYPE}
# Stored types other than exactly A, each of which COUSR01C accepts and COSGN00C routes to CM00.
NON_ADMIN_USER_TYPES = ("U", "u", "a", "X")

# COMEN02Y CDEMO-MENU-OPT-NAME and COADM02Y CDEMO-ADMIN-OPT-NAME, PIC X(35) VALUE literals as
# written in the copybooks, in table order.
MAIN_MENU_LABELS = (
    "Account View                       ",
    "Account Update                     ",
    "Credit Card List                   ",
    "Credit Card View                   ",
    "Credit Card Update                 ",
    "Transaction List                   ",
    "Transaction View                   ",
    "Transaction Add                    ",
    "Transaction Reports                ",
    "Bill Payment                       ",
    "Pending Authorization View         ",
)
ADMIN_MENU_LABELS = (
    "User List (Security)               ",
    "User Add (Security)                ",
    "User Update (Security)             ",
    "User Delete (Security)             ",
    "Transaction Type List/Update (Db2) ",
    "Transaction Type Maintenance (Db2) ",
)
# CDEMO-MENU-OPT-COUNT VALUE 11 and CDEMO-ADMIN-OPT-COUNT VALUE 6.
MAIN_MENU_OPTION_COUNT = 11
ADMIN_MENU_OPTION_COUNT = 6

# The App Router pages that replace XCTL PROGRAM(CDEMO-MENU-OPT-PGMNAME(WS-OPTION)).
MAIN_MENU_ROUTES = {
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
# XCTL PROGRAM(CDEMO-ADMIN-OPT-PGMNAME(WS-OPTION)), COUSR00C-COUSR03C.
ADMIN_MENU_ROUTES = {
    1: "/admin/users",
    2: "/admin/users/add",
    3: "/admin/users/update",
    4: "/admin/users/delete",
}
FIRST_OPTION = "1"
FIRST_OPTION_ROUTES = {MAIN_MENU: MAIN_MENU_ROUTES[1], ADMIN_MENU: ADMIN_MENU_ROUTES[1]}

# The OPTION field as keyed: JUSTIFY=(RIGHT,ZERO) and the INSPECT REPLACING ALL ' ' BY '0' turn
# each of these into 01.
OPTION_ONE_SPELLINGS = ("1", "01", "1 ", " 1")
# COMEN01C line 131 and COADM01C line 135, with the cursor on the OPTION field (IC).
INVALID_OPTION_MESSAGE = "Please enter a valid option number..."
OPTION_CURSOR_FIELD = "option"
# Refused by both menus: blank, zero, non-numeric and above both option counts.
INVALID_OPTIONS_OF_BOTH_MENUS = ("", "  ", "0", "00", "AB", "1A", "99")
INVALID_OPTION_CASES = (
    *((MAIN_MENU, optionText) for optionText in INVALID_OPTIONS_OF_BOTH_MENUS),
    (MAIN_MENU, "12"),
    *((ADMIN_MENU, optionText) for optionText in INVALID_OPTIONS_OF_BOTH_MENUS),
    (ADMIN_MENU, "7"),
)
# Longer than the BMS OPTION LENGTH=2, which no 3270 can send.
OVERLONG_OPTIONS = ("123", "001")

# COMEN01C lines 163-167: 'This option ' + the name DELIMITED BY '  ' + ' is not installed...'.
PENDING_AUTHORIZATION_OPTION = "11"
PENDING_AUTHORIZATION_NOT_INSTALLED_MESSAGE = (
    "This option Pending Authorization View is not installed..."
)
# COADM01C lines 270-276: no option name, a space before the ellipsis and none after "option ".
ADMIN_DB2_OPTIONS = ("5", "6")
ADMIN_NOT_INSTALLED_MESSAGE = "This option is not installed ..."
# COMEN01C line 140, the 403 of every non-admin refused CA00; the trailing space is the source's.
ADMIN_ONLY_MESSAGE_TEXT = "No access - Admin Only option... "
# The 403 of an admin refused CM00, the 401 of a missing session and the Origin guard's 403.
NO_MESSAGE = ""
# Exactly the allow-listed http://127.0.0.1:3000 is accepted; another host spelling is not.
NOT_ALLOW_LISTED_ORIGIN = "http://localhost:3000"
ORIGIN_HEADER_CASES = ({}, {"Origin": NOT_ALLOW_LISTED_ORIGIN})

type LoginAs = Callable[[str], Awaitable[Any]]


# --- Helpers -------------------------------------------------------------------------------------
def MenuPath(menu: str) -> str:
    """Return the option-list path of ``menu``, ``main`` (CM00) or ``admin`` (CA00)."""
    return f"{API_V1}/menu/{menu}"


async def SelectOption(client: httpx.AsyncClient, menu: str, option: str) -> httpx.Response:
    """Press ENTER on ``menu`` with ``option`` keyed in the OPTION field."""
    return await client.post(MenuPath(menu) + SELECTION_SUFFIX, json={"option": option})


async def SendMenuRequest(client: httpx.AsyncClient, menu: str, method: str) -> httpx.Response:
    """List ``menu``'s options (GET) or select its first option (POST)."""
    if method == MENU_METHOD_SELECT:
        return await SelectOption(client, menu, FIRST_OPTION)
    return await client.get(MenuPath(menu))


async def SignOnMenuOwner(loginAs: LoginAs, menu: str) -> httpx.AsyncClient:
    """Return a client signed on as the user type whose menu ``menu`` is."""
    signedOnClient = await loginAs(MENU_OWNER_TYPES[menu])
    return signedOnClient.client


def AssertLegacyError(
    response: httpx.Response, status: int, message: str, cursorField: str = ""
) -> None:
    """Assert the legacy error body: ``message``, no information line, and the cursor field.

    ``field_errors`` names the cursor field alone, or nothing when no field takes the cursor.
    """
    assert response.status_code == status, response.text
    body = response.json()
    assert body["message"] == message
    assert body["info_message"] == ""
    assert body["field_errors"] == ([cursorField] if cursorField else [])
    assert body["cursor_field"] == cursorField


def AssertRoute(response: httpx.Response, route: str) -> None:
    """Assert a selection that navigates to ``route`` with a blank message line."""
    assert response.status_code == HTTP_OK, response.text
    body = response.json()
    assert body["route"] == route
    assert body.get("message", "") == ""


def AssertMenuStays(response: httpx.Response, message: str) -> None:
    """Assert a selection that stays on the menu and shows ``message``."""
    assert response.status_code == HTTP_OK, response.text
    body = response.json()
    assert body["route"] is None
    assert body["message"] == message


def AssertMenuOptions(
    response: httpx.Response, copybookLabels: tuple[str, ...], optionCount: int
) -> None:
    """Assert options 1 to ``optionCount`` in order, labelled by the right-trimmed X(35) names."""
    assert response.status_code == HTTP_OK, response.text
    options = response.json()["options"]
    assert [option["number"] for option in options] == list(range(1, optionCount + 1))
    expectedLabels = [label.rstrip(" ") for label in copybookLabels]
    assert [option["label"] for option in options] == expectedLabels


def CopySessionClient(
    signedOnClient: httpx.AsyncClient, application: FastAPI, headers: dict[str, str]
) -> httpx.AsyncClient:
    """Return a client of ``application`` holding ``signedOnClient``'s cookies and ``headers``.

    Unlike the fixture clients, it sends no Origin header unless ``headers`` names one.
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url=signedOnClient.base_url,
        cookies=signedOnClient.cookies,
        headers=headers,
    )


# --- Menu content --------------------------------------------------------------------------------
async def test_main_menu_lists_the_comen02y_options(regularClient: httpx.AsyncClient) -> None:
    """CM00 lists COMEN02Y's 11 options, 8 being the active ``Transaction Add`` entry."""
    response = await regularClient.get(MenuPath(MAIN_MENU))
    AssertMenuOptions(response, MAIN_MENU_LABELS, MAIN_MENU_OPTION_COUNT)


async def test_admin_menu_lists_the_coadm02y_options(adminClient: httpx.AsyncClient) -> None:
    """CA00 lists COADM02Y's 6 options, the two Db2 entries included."""
    response = await adminClient.get(MenuPath(ADMIN_MENU))
    AssertMenuOptions(response, ADMIN_MENU_LABELS, ADMIN_MENU_OPTION_COUNT)


# --- Selections ----------------------------------------------------------------------------------
@pytest.mark.parametrize(("optionNumber", "route"), MAIN_MENU_ROUTES.items())
async def test_main_menu_option_routes_to_its_page(
    regularClient: httpx.AsyncClient, optionNumber: int, route: str
) -> None:
    """Each installed CM00 option answers the page that replaces its XCTL."""
    response = await SelectOption(regularClient, MAIN_MENU, str(optionNumber))
    AssertRoute(response, route)


@pytest.mark.parametrize(("optionNumber", "route"), ADMIN_MENU_ROUTES.items())
async def test_admin_menu_option_routes_to_its_page(
    adminClient: httpx.AsyncClient, optionNumber: int, route: str
) -> None:
    """Each installed CA00 option answers the page that replaces its XCTL."""
    response = await SelectOption(adminClient, ADMIN_MENU, str(optionNumber))
    AssertRoute(response, route)


@pytest.mark.parametrize("menu", (MAIN_MENU, ADMIN_MENU))
@pytest.mark.parametrize("optionText", OPTION_ONE_SPELLINGS)
async def test_option_field_is_right_justified_and_zero_filled(
    loginAs: LoginAs, menu: str, optionText: str
) -> None:
    """``1``, ``01``, ``1 `` and `` 1`` all select option 1 on both menus."""
    ownerClient = await SignOnMenuOwner(loginAs, menu)
    response = await SelectOption(ownerClient, menu, optionText)
    AssertRoute(response, FIRST_OPTION_ROUTES[menu])


async def test_main_menu_option_11_is_not_installed(regularClient: httpx.AsyncClient) -> None:
    """Option 11 (COPAUS0C, excluded authorization module) stays on CM00 with its name."""
    response = await SelectOption(regularClient, MAIN_MENU, PENDING_AUTHORIZATION_OPTION)
    AssertMenuStays(response, PENDING_AUTHORIZATION_NOT_INSTALLED_MESSAGE)


@pytest.mark.parametrize("optionText", ADMIN_DB2_OPTIONS)
async def test_admin_menu_db2_options_are_not_installed(
    adminClient: httpx.AsyncClient, optionText: str
) -> None:
    """Options 5 and 6 (COTRTLIC, COTRTUPC, excluded Db2 module) stay on CA00 with no name."""
    response = await SelectOption(adminClient, ADMIN_MENU, optionText)
    AssertMenuStays(response, ADMIN_NOT_INSTALLED_MESSAGE)


@pytest.mark.parametrize(("menu", "optionText"), INVALID_OPTION_CASES)
async def test_invalid_option_is_refused(loginAs: LoginAs, menu: str, optionText: str) -> None:
    """A blank, zero, non-numeric or out-of-range option is 400 with the cursor on OPTION."""
    ownerClient = await SignOnMenuOwner(loginAs, menu)
    response = await SelectOption(ownerClient, menu, optionText)
    AssertLegacyError(response, HTTP_EDIT_FAILURE, INVALID_OPTION_MESSAGE, OPTION_CURSOR_FIELD)


@pytest.mark.parametrize("menu", (MAIN_MENU, ADMIN_MENU))
@pytest.mark.parametrize("optionText", OVERLONG_OPTIONS)
async def test_option_longer_than_the_bms_field_is_malformed(
    loginAs: LoginAs, menu: str, optionText: str
) -> None:
    """An option of more than 2 characters is 422 naming ``option``, before any option edit."""
    ownerClient = await SignOnMenuOwner(loginAs, menu)
    response = await SelectOption(ownerClient, menu, optionText)
    AssertLegacyError(response, HTTP_MALFORMED, NO_MESSAGE, OPTION_CURSOR_FIELD)
    assert response.json()["detail"]


# --- Role guards ---------------------------------------------------------------------------------
@pytest.mark.parametrize("method", MENU_METHODS)
@pytest.mark.parametrize("userType", NON_ADMIN_USER_TYPES)
async def test_non_admin_user_is_refused_the_admin_menu(
    loginAs: LoginAs, userType: str, method: str
) -> None:
    """Every type other than exactly A gets 403 with the admin-only literal on CA00."""
    signedOnClient = await loginAs(userType)
    response = await SendMenuRequest(signedOnClient.client, ADMIN_MENU, method)
    AssertLegacyError(response, HTTP_ACCESS_REFUSED, ADMIN_ONLY_MESSAGE_TEXT)


@pytest.mark.parametrize("userType", NON_ADMIN_USER_TYPES)
async def test_non_admin_user_reaches_the_main_menu(loginAs: LoginAs, userType: str) -> None:
    """Every type other than exactly A lists CM00 and selects its options."""
    signedOnClient = await loginAs(userType)
    listResponse = await SendMenuRequest(signedOnClient.client, MAIN_MENU, MENU_METHOD_LIST)
    AssertMenuOptions(listResponse, MAIN_MENU_LABELS, MAIN_MENU_OPTION_COUNT)
    selectResponse = await SendMenuRequest(signedOnClient.client, MAIN_MENU, MENU_METHOD_SELECT)
    AssertRoute(selectResponse, FIRST_OPTION_ROUTES[MAIN_MENU])


@pytest.mark.parametrize("method", MENU_METHODS)
async def test_admin_is_refused_the_main_menu(adminClient: httpx.AsyncClient, method: str) -> None:
    """An admin gets 403 with an empty message on CM00, and returns to its own menu."""
    response = await SendMenuRequest(adminClient, MAIN_MENU, method)
    AssertLegacyError(response, HTTP_ACCESS_REFUSED, NO_MESSAGE)


@pytest.mark.parametrize("method", MENU_METHODS)
@pytest.mark.parametrize("menu", (MAIN_MENU, ADMIN_MENU))
async def test_request_without_session_is_refused(
    apiClient: httpx.AsyncClient, menu: str, method: str
) -> None:
    """With no session cookie, every menu endpoint answers 401 with an empty message."""
    response = await SendMenuRequest(apiClient, menu, method)
    AssertLegacyError(response, HTTP_SIGN_ON_FAILURE, NO_MESSAGE)


@pytest.mark.parametrize("originHeaders", ORIGIN_HEADER_CASES, ids=("missing", "not-allowed"))
@pytest.mark.parametrize("menu", (MAIN_MENU, ADMIN_MENU))
async def test_selection_without_allow_listed_origin_is_refused(
    loginAs: LoginAs, testApp: FastAPI, menu: str, originHeaders: dict[str, str]
) -> None:
    """A selection carrying a valid session but no allow-listed Origin is 403.

    The copied session still lists the menu, because GET is never Origin-guarded, so the
    refusal comes from the Origin alone.
    """
    ownerClient = await SignOnMenuOwner(loginAs, menu)
    async with CopySessionClient(ownerClient, testApp, originHeaders) as copiedSessionClient:
        listResponse = await copiedSessionClient.get(MenuPath(menu))
        refusedResponse = await SelectOption(copiedSessionClient, menu, FIRST_OPTION)
    assert listResponse.status_code == HTTP_OK, listResponse.text
    AssertLegacyError(refusedResponse, HTTP_ACCESS_REFUSED, NO_MESSAGE)
    AssertRoute(await SelectOption(ownerClient, menu, FIRST_OPTION), FIRST_OPTION_ROUTES[menu])
