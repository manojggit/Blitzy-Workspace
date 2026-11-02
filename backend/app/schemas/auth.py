"""Request and response models of CC00 sign-on: login, logout and the current user.

Replaces program COSGN00C (transaction CC00) and its BMS map COSGN00 (``bms/COSGN00.bms``,
symbolic map ``cpy-bms/COSGN00.CPY``), the user record of copybook CSUSR01Y, the CSMSG01Y
thank-you literal, and the COMMAREA user fields of COCOM01Y (CDEMO-USER-ID, CDEMO-USER-TYPE),
which the HttpOnly session cookie now carries.

``LoginRequest`` holds the only identity inputs the backend ever reads from a caller. No
response model holds a password, a password hash or a session token: the router sets the
token in the cookie alone.

Field names are the snake_case JSON keys of the API contract, mirroring the ``security.users``
columns. They are wire names fixed by that contract, so the MK_Modernization Rule's camelCase
convention for variables does not apply to them.
"""

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import ScreenText


class LoginRequest(BaseModel):
    """The COSGN00 sign-on fields that COSGN00C receives on ENTER.

    Both fields are raw text as keyed, limited only by their BMS LENGTH and the code-page-037
    repertoire (422 otherwise). They are neither required nor upper-cased here: COSGN00C
    upper-cases both before the USRSEC lookup and compare, and a blank field reaches the
    service so that it answers with the legacy ``Please enter ...`` literal.
    """

    user_id: ScreenText(8) = Field(
        default="",
        description=(
            "Map field USERID (LENGTH 8): the user ID as keyed. The service upper-cases it "
            "before reading the user record (SEC-USR-ID X(08))."
        ),
    )
    password: ScreenText(8) = Field(
        default="",
        description=(
            "Map field PASSWD (LENGTH 8, dark field): the password as keyed. The service "
            "upper-cases it before verifying it against the stored bcrypt hash."
        ),
    )


class LoginResponse(BaseModel):
    """A successful COSGN00C sign-on: the signed-on user and the menu to open.

    Replaces the COMMAREA user fields COSGN00C fills before its XCTL to COADM01C or COMEN01C.
    The session token travels only in the HttpOnly cookie the router sets, never in this body.
    """

    user_id: str = Field(
        max_length=8,
        description="The signed-on user ID: the SEC-USR-ID X(08) key read with the upper-cased ID.",
    )
    user_type: str = Field(
        max_length=1,
        description=(
            "The stored user type (SEC-USR-TYPE X(01)), never rewritten. Exactly A is an "
            "admin; every other value is a regular user."
        ),
    )
    first_name: str = Field(max_length=20, description="SEC-USR-FNAME X(20), right-trimmed.")
    last_name: str = Field(max_length=20, description="SEC-USR-LNAME X(20), right-trimmed.")
    next_route: Literal["/admin", "/menu"] = Field(
        description=(
            "The page to open next, decided by the server: /admin (COADM01C) when the user "
            "type is exactly A (CDEMO-USRTYP-ADMIN), otherwise /menu (COMEN01C)."
        ),
    )


class LogoutResponse(BaseModel):
    """The COSGN00C sign-off (F3) answer; the router clears the session cookie with it."""

    message: str = Field(
        description=(
            "The CSMSG01Y CCDA-MSG-THANK-YOU literal: Thank you for using CardDemo application..."
        ),
    )


class CurrentUserResponse(BaseModel):
    """The user of the current COSGN00C session, for the header and the choice of menu.

    The user ID and type come from the verified session token and hold for its lifetime, as
    the COMMAREA did for a CICS session. ``next_route`` follows the same admin rule as
    ``LoginResponse``, so the frontend never derives a role itself.
    """

    user_id: str = Field(
        max_length=8,
        description="The signed-on user ID from the session token (CDEMO-USER-ID X(08)).",
    )
    user_type: str = Field(
        max_length=1,
        description=(
            "The user type from the session token (CDEMO-USER-TYPE X(01)). Exactly A is an "
            "admin; every other value is a regular user."
        ),
    )
    first_name: str = Field(
        max_length=20,
        description="SEC-USR-FNAME X(20), right-trimmed; empty when the record cannot be read.",
    )
    last_name: str = Field(
        max_length=20,
        description="SEC-USR-LNAME X(20), right-trimmed; empty when the record cannot be read.",
    )
    next_route: Literal["/admin", "/menu"] = Field(
        description=(
            "The user's own menu: /admin (COADM01C) when the user type is exactly A, "
            "otherwise /menu (COMEN01C)."
        ),
    )
