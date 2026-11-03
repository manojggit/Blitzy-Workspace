"""Replaces: COUSR00C (CU00), COUSR01C (CU01), COUSR02C (CU02), COUSR03C (CU03); BMS COUSR00,
COUSR01, COUSR02, COUSR03; copybook CSUSR01Y.

The admin user-administration router, mounted at ``/api/v1/admin/users``. It replaces the
CU00-CU03 transaction definitions of ``csd/CARDDEMO.CSD`` and the admin-menu ``XCTL`` into
those programs. Every route is admin-only through ``GetAdminContext``: user type exactly ``A``
passes, and any other type is refused with 403 ``No access - Admin Only option... ``.

Each handler makes one awaited call to the service that ports its program and returns the
service's model. Legacy outcomes are raised by the services as ``LegacyMessageError`` and
serialised by ``app.core.errors``; screen text breaking its BMS LENGTH or the code-page-037
repertoire is a 422. ENTER on COUSR02 and COUSR03 share ``GET /{userId}``, told apart by the
``screen`` query parameter. COUSR01's F12 has no route: the program answers it with the
CSMSG01Y invalid-key literal without reading any field, and the page shows that literal itself.
The routes are ``LosslessKeyRoute`` routes, so ``/{userId}`` reaches every ID COUSR01C accepts,
one holding ``/`` included.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from app.api.v1 import LegacyErrorResponses, LosslessKeyRoute
from app.core.dependencies import GetAdminContext, OnlineContext
from app.schemas.common import ScreenText
from app.schemas.users import (
    UserCreateRequest,
    UserDetailResponse,
    UserFetchQuery,
    UserFetchScreen,
    UserListQuery,
    UserListResponse,
    UserMessageResponse,
    UserSelectionRequest,
    UserSelectionResponse,
    UserUpdateRequest,
)
from app.services.users import cousr00c, cousr01c, cousr02c, cousr03c

# USRIDIN, LENGTH=8, on COUSR02 and COUSR03 (SEC-USR-ID X(08) of CSUSR01Y).
USER_ID_LENGTH = 8
# Raw screen text with no pattern: a blank ID sent as %20 must reach the services'
# ``User ID can NOT be empty...`` edit rather than a 422, and an ID holding "/" (sent as %2F,
# kept in one segment by LosslessKeyRoute) its keyed read rather than a framework 404.
USER_ID_PATH = Annotated[
    ScreenText(USER_ID_LENGTH),
    Path(description="User ID as typed on the screen (up to 8 characters)."),
]
ADMIN_CONTEXT = Annotated[OnlineContext, Depends(GetAdminContext)]

# The ENTER fetch of COUSR02C (update prompt) and COUSR03C (delete prompt), keyed by screen.
FETCH_SERVICES: dict[
    UserFetchScreen, Callable[[OnlineContext, str], Awaitable[UserDetailResponse]]
] = {
    UserFetchScreen.UPDATE: cousr02c.FetchUser,
    UserFetchScreen.DELETE: cousr03c.FetchUserForDelete,
}

ROUTER = APIRouter(prefix="/admin/users", tags=["admin-users"], route_class=LosslessKeyRoute)


@ROUTER.get(
    "",
    response_model=UserListResponse,
    summary="CU00 / COUSR00C: list users (10 per page)",
    description=(
        "Without a direction, the browse starts at the first user ID greater than or equal to "
        "searchUserId, in EBCDIC order, as page 1. A search finding no such user ID answers "
        "'You are at the top of the page...' on page 1 with no rows and a blank last_key, from "
        "which F8 is refused. A page's first_key or last_key is sent back as startKey with "
        "direction backward (F7) or forward (F8); a paging key that reads no row is a 400."
    ),
    responses=LegacyErrorResponses(400, 401, 403, 503),
)
async def ListUsers(
    query: Annotated[UserListQuery, Query()], context: ADMIN_CONTEXT
) -> UserListResponse:
    """Return one COUSR00 page of at most 10 users, or raise its paging or file outcome."""
    return await cousr00c.ListUsers(context, query)


@ROUTER.post(
    "/selection",
    response_model=UserSelectionResponse,
    summary="CU00 / COUSR00C: process user list selection",
    description=(
        "The first row with a selection code decides: U opens the update screen and D the "
        "delete screen through route. Any other code sets the invalid-selection literal and "
        "the list is browsed again from search_user_id. When that literal stays, the answer is "
        "a 400 whose body adds the browsed page's rows, page, first_key and last_key; when the "
        "browse replaces it with a paging literal, the page is a 200. A search finding no user "
        "ID answers page 1 with the shown rows read again from the first row's user_id."
    ),
    responses=LegacyErrorResponses(400, 401, 403, 503),
)
async def SelectUser(
    selectionRequest: UserSelectionRequest, context: ADMIN_CONTEXT
) -> UserSelectionResponse:
    """Return a valid U or D selection's route or the re-browsed page, or raise its 400 or 503."""
    return await cousr00c.ProcessUserSelection(context, selectionRequest)


@ROUTER.post(
    "",
    response_model=UserMessageResponse,
    summary="CU01 / COUSR01C: add user",
    description=(
        "Every field must be non-blank, checked in screen order, and any user type is "
        "accepted. The password is hashed as entered and never returned."
    ),
    responses=LegacyErrorResponses(400, 401, 403, 409, 503),
)
async def AddUser(createRequest: UserCreateRequest, context: ADMIN_CONTEXT) -> UserMessageResponse:
    """Write the new USRSEC user and return the COUSR01C added literal."""
    return await cousr01c.AddUser(context, createRequest)


@ROUTER.get(
    "/{userId}",
    response_model=UserDetailResponse,
    summary="CU02 / COUSR02C, CU03 / COUSR03C: fetch user",
    description=(
        "screen selects the prompt: update returns the COUSR02C save prompt and delete the "
        "COUSR03C delete prompt. No password or password hash is returned."
    ),
    responses=LegacyErrorResponses(400, 401, 403, 404, 503),
)
async def FetchUser(
    userId: USER_ID_PATH, query: Annotated[UserFetchQuery, Query()], context: ADMIN_CONTEXT
) -> UserDetailResponse:
    """Return the user with the prompt of the screen that fetched it."""
    return await FETCH_SERVICES[query.screen](context, userId)


@ROUTER.put(
    "/{userId}",
    response_model=UserMessageResponse,
    summary="CU02 / COUSR02C: update user",
    description=(
        "Serves F5, and F3, which saves before returning. A blank password keeps the stored "
        "one; a new password is hashed as entered. A submission changing nothing is a 400."
    ),
    responses=LegacyErrorResponses(400, 401, 403, 404, 503),
)
async def UpdateUser(
    userId: USER_ID_PATH, updateRequest: UserUpdateRequest, context: ADMIN_CONTEXT
) -> UserMessageResponse:
    """Rewrite the USRSEC user and return the COUSR02C updated literal."""
    return await cousr02c.UpdateUser(context, userId, updateRequest)


@ROUTER.delete(
    "/{userId}",
    response_model=UserMessageResponse,
    summary="CU03 / COUSR03C: delete user",
    description=(
        "Serves F5 on the delete screen. A blank user ID is refused and an absent one is "
        "not found, with nothing deleted."
    ),
    responses=LegacyErrorResponses(400, 401, 403, 404, 503),
)
async def DeleteUser(userId: USER_ID_PATH, context: ADMIN_CONTEXT) -> UserMessageResponse:
    """Delete the USRSEC user and return the COUSR03C deleted literal."""
    return await cousr03c.DeleteUser(context, userId)
