"""User administration contracts of CU00-CU03.

Replaces the BMS symbolic maps COUSR00-COUSR03 (``cpy-bms/COUSR00.CPY`` to
``cpy-bms/COUSR03.CPY``) of programs COUSR00C (CU00, user list), COUSR01C (CU01, add user),
COUSR02C (CU02, update user) and COUSR03C (CU03, delete user), and the USRSEC record of
copybook CSUSR01Y.

The four programs end validation at the first failing edit, so their responses carry
``message`` and ``cursor_field`` only. Screen fields are raw ``ScreenText`` limited to their
BMS LENGTH; every presence check and its literal belongs to the services.

No response model carries a password or a password hash. Plaintext arrives only in
``UserCreateRequest`` and ``UserUpdateRequest``, and the services hash it as entered. The
CU03 delete confirmation uses ``app.schemas.common.MessageResponse``.
"""

from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.common import (
    Direction,
    PageState,
    QueryModel,
    ScreenText,
)


class UserListQuery(QueryModel):
    """Query parameters of ``GET /admin/users``, the COUSR00 browse (COUSR00C, CU00).

    Replaces the USRIDIN search field and the CDEMO-CU00 COMMAREA paging keys. A query with no
    ``direction`` is ENTER, or the first entry, and browses from ``searchUserId``.
    """

    search_user_id: ScreenText(8) = Field(
        default="",
        alias="searchUserId",
        description=(
            "USRIDIN, LENGTH 8: the browse starts at the first user ID greater than or equal "
            "to it, in EBCDIC order; blank starts at the lowest key."
        ),
    )
    start_key: ScreenText(8) = Field(
        default="",
        alias="startKey",
        description=(
            "Read only with a direction: the shown page's first_key (direction backward, F7) "
            "or last_key (direction forward, F8). An empty or blank key on F8 answers "
            "'You are already at the bottom of the page...'. A raw user ID of at most 8 "
            "characters, which may hold spaces or any code-page-037 character."
        ),
    )
    direction: Direction | None = Field(
        default=None,
        alias="direction",
        description=(
            "F8 reads forward after startKey (READNEXT); F7 reads backward (READPREV). Omitted "
            "for ENTER and the first entry, which browse from searchUserId (PROCESS-ENTER-KEY)."
        ),
    )
    page: int = Field(
        default=0,
        ge=0,
        alias="page",
        description=(
            "The page number shown when the key was pressed (CDEMO-CU00-PAGE-NUM). 0 is the "
            "screen an ENTER left when no user ID was at or after the search key, from which "
            "F8 answers 'You are already at the bottom of the page...' and F7 "
            "'You are already at the top of the page...'."
        ),
    )


class UserListRow(BaseModel):
    """One COUSR00 list row (COUSR00C, CU00): the USRIDnn, FNAMEnn, LNAMEnn and UTYPEnn fields.

    It carries no password and no password hash.
    """

    user_id: str = Field(description="USRIDnn: the user ID (SEC-USR-ID, X(08)).")
    first_name: str = Field(description="FNAMEnn: the first name (SEC-USR-FNAME, X(20)).")
    last_name: str = Field(description="LNAMEnn: the last name (SEC-USR-LNAME, X(20)).")
    user_type: str = Field(description="UTYPEnn: the stored user type (SEC-USR-TYPE, X(01)).")


class UserListResponse(PageState):
    """One COUSR00 page of at most 10 users (COUSR00C, CU00), with its paging position."""

    rows: list[UserListRow] = Field(
        default_factory=list,
        max_length=10,
        description="The users of the page, in EBCDIC user-ID order; at most 10 rows.",
    )
    message: str = Field(
        default="",
        description=(
            "The ERRMSG line, verbatim: a paging literal or "
            "'Invalid selection. Valid values are U and D'; empty for none."
        ),
    )
    cursor_field: str | None = Field(
        default=None,
        description="Snake_case name of the field that receives focus; null for none.",
    )


class UserSelection(BaseModel):
    """One COUSR00 row as keyed (COUSR00C, CU00): its SELnnnn code and USRIDnn key."""

    user_id: ScreenText(8) = Field(
        default="",
        description="USRIDnn, LENGTH 8: the user ID shown in the row.",
    )
    selection: ScreenText(1) = Field(
        default="",
        description=(
            "SELnnnn, LENGTH 1: U or u opens the update screen, D or d the delete screen; "
            "any other non-blank code gives the invalid-selection literal."
        ),
    )


class UserSelectionRequest(BaseModel):
    """The ENTER body of ``POST /admin/users/selection`` (COUSR00, COUSR00C, CU00)."""

    selections: list[UserSelection] = Field(
        default_factory=list,
        max_length=10,
        description="The rows of the shown page with their selection codes; at most 10.",
    )
    search_user_id: ScreenText(8) = Field(
        default="",
        description="USRIDIN, LENGTH 8: the search key applied after the selection.",
    )


class UserSelectionResponse(UserListResponse):
    """The COUSR00 ENTER outcome (COUSR00C, CU00): a route, or the re-browsed page."""

    route: str | None = Field(
        default=None,
        description=(
            "Frontend path of a valid selection, /admin/users/update?userId=<id> (COUSR02C) "
            "or /admin/users/delete?userId=<id> (COUSR03C); null when the list fields carry "
            "the re-browsed page and its message."
        ),
    )


class UserCreateRequest(BaseModel):
    """The ENTER body of ``POST /admin/users``, the COUSR01 add screen (COUSR01C, CU01).

    Every field is raw text. The service checks presence in source order, from
    ``First Name can NOT be empty...`` to ``User Type can NOT be empty...``.
    """

    first_name: ScreenText(20) = Field(default="", description="FNAME, LENGTH 20.")
    last_name: ScreenText(20) = Field(default="", description="LNAME, LENGTH 20.")
    user_id: ScreenText(8) = Field(default="", description="USERID, LENGTH 8: the new key.")
    password: ScreenText(8) = Field(
        default="",
        description=(
            "PASSWD, LENGTH 8 (dark field): hashed as entered, with its case kept; never "
            "stored, returned or logged as text."
        ),
    )
    user_type: ScreenText(1) = Field(
        default="",
        description=(
            "USRTYPE, LENGTH 1: any character is accepted, as COUSR01C checks no value; "
            "only A signs on as an admin."
        ),
    )


class UserFetchScreen(StrEnum):
    """The screen fetching a user: COUSR02 (COUSR02C, CU02) or COUSR03 (COUSR03C, CU03)."""

    UPDATE = "update"
    DELETE = "delete"


class UserFetchQuery(QueryModel):
    """Query parameters of ``GET /admin/users/{userId}``, the COUSR02 and COUSR03 ENTER fetch.

    COUSR02C and COUSR03C share the endpoint and differ only in the prompt they return.
    """

    screen: UserFetchScreen = Field(
        default=UserFetchScreen.UPDATE,
        alias="screen",
        description="update for the COUSR02C prompt, delete for the COUSR03C prompt.",
    )


class UserDetailResponse(BaseModel):
    """A fetched user as COUSR02 (COUSR02C, CU02) or COUSR03 (COUSR03C, CU03) shows it.

    COUSR02C displayed the stored password; under hashing no password or hash is returned.
    """

    user_id: str = Field(description="USRIDIN: the fetched user ID (SEC-USR-ID).")
    first_name: str = Field(description="FNAME: the first name (SEC-USR-FNAME).")
    last_name: str = Field(description="LNAME: the last name (SEC-USR-LNAME).")
    user_type: str = Field(description="USRTYPE: the stored user type (SEC-USR-TYPE).")
    message: str = Field(
        default="",
        description=(
            "The fetching program's prompt, verbatim: "
            "'Press PF5 key to save your updates ...' (COUSR02C) or "
            "'Press PF5 key to delete this user ...' (COUSR03C)."
        ),
    )


class UserUpdateRequest(BaseModel):
    """The body of ``PUT /admin/users/{userId}``: COUSR02 F5, or F3 save-then-exit (COUSR02C).

    The user ID comes from the path. The service checks presence in source order, skipping
    the password, and answers ``Please modify to update ...`` when nothing changed.
    """

    first_name: ScreenText(20) = Field(default="", description="FNAME, LENGTH 20.")
    last_name: ScreenText(20) = Field(default="", description="LNAME, LENGTH 20.")
    password: ScreenText(8) = Field(
        default="",
        description=(
            "PASSWD, LENGTH 8 (dark field): blank keeps the stored hash; a non-blank value "
            "replaces it, hashed as entered, only when it does not verify against it."
        ),
    )
    user_type: ScreenText(1) = Field(
        default="",
        description="USRTYPE, LENGTH 1: any character is accepted, as COUSR02C checks no value.",
    )


class UserMessageResponse(BaseModel):
    """The success body of COUSR01C (CU01) and COUSR02C (CU02): the ERRMSG line and cursor."""

    message: str = Field(
        description=(
            "The program's literal, verbatim, for example 'User <id> has been added ...', "
            "'User <id> has been updated ...' or 'Please modify to update ...'."
        ),
    )
    cursor_field: str | None = Field(
        default=None,
        description="Snake_case name of the field that receives focus; null for none.",
    )
