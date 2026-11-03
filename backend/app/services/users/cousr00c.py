"""Replaces: COUSR00C (CU00), BMS COUSR00, copybooks CSUSR01Y.

The admin user list: USRSEC users 10 rows per page in EBCDIC key order, browsed from the
search key with ENTER, paged with F8 and F7, and the U or D row selection that opens the
update (COUSR02C) or delete (COUSR03C) screen.

The COMMAREA paging state (CDEMO-CU00-PAGE-NUM, -USRID-FIRST, -USRID-LAST) comes back from the
client as ``page`` and ``startKey``, and the browse key's EIBAID as ``direction``: none for
ENTER and the first entry, forward for F8, backward for F7. NEXT-PAGE-FLG is derived instead:
F8 reads past the last key shown, and a blank last key or finding nothing after it is the
flag's ``N``. An F7 or F8 that reads no row re-sent the screen with its old rows and only the
message changed (SEND-ERASE-NO, or rows left in place), so it raises LegacyMessageError: the
error body has the four standard keys only, and the page keeps its table. ENTER always answers
a page. A search that finds no user ID at or after its key (STARTBR NOTFND) is page 1, as
every ENTER search resets the page number, with the rows still shown read again from the first
key shown and a blank last key, from which F8 answers NEXT-PAGE-NO before any read. An invalid
selection code whose literal the browse leaves on the ERRMSG line is a 400 edit failure whose
body adds the browsed page's ``rows``, ``page``, ``first_key`` and ``last_key``.

Legacy paths with no code here: ``EIBCALEN = 0`` (back to sign-on) is the RequireAdmin session
guard; PF3 (``XCTL COADM01C``) is frontend navigation; other AID keys cannot reach the server;
POPULATE-HEADER-INFO is the frontend ScreenShell; clearing USRIDIN after a browse is the page's.
"""

import logging
from collections.abc import Awaitable, Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

from app.core.dependencies import OnlineContext
from app.core.errors import (
    HTTP_EDIT_FAILURE,
    HTTP_FILE_ERROR,
    LegacyErrorDetails,
    LegacyMessageError,
    LogFileCondition,
    RepositoryError,
)
from app.core.locks import FileNotOpenError
from app.core.messages import (
    ALREADY_AT_BOTTOM_MESSAGE,
    ALREADY_AT_TOP_MESSAGE,
    AT_TOP_OF_PAGE_MESSAGE,
    REACHED_BOTTOM_MESSAGE,
    REACHED_TOP_MESSAGE,
)
from app.repositories.user_repository import UserRepository
from app.schemas.common import Direction, LegacyErrorResponse
from app.schemas.users import (
    UserListQuery,
    UserListResponse,
    UserListRow,
    UserSelection,
    UserSelectionErrorResponse,
    UserSelectionRequest,
    UserSelectionResponse,
)

LOGGER = logging.getLogger(__name__)

# WS-TRANID (line 37).
TRANSACTION_ID = "CU00"
# The CICS commands of the forward and backward browses, which one repository call runs, as
# their logged file conditions name them.
BROWSE_FORWARD_OPERATION = "STARTBR/READNEXT"
BROWSE_BACKWARD_OPERATION = "STARTBR/READPREV"

# WS-USER-DATA, USER-REC OCCURS 10 TIMES (line 57).
PAGE_SIZE = 10
# PROCESS-ENTER-KEY line 212.
INVALID_SELECTION_MESSAGE = "Invalid selection. Valid values are U and D"
# The WHEN OTHER branches of STARTBR-, READNEXT- and READPREV-USER-SEC-FILE (lines 610, 644,
# 678); the batch window's NOTOPEN reaches them too.
UNABLE_TO_LOOKUP_USER_MESSAGE = "Unable to lookup User..."
# The frontend pages of XCTL COUSR02C and XCTL COUSR03C (lines 190-209), keyed by userId.
UPDATE_ROUTE = "/admin/users/update"
DELETE_ROUTE = "/admin/users/delete"
ROUTE_USER_ID_PARAMETER = "userId"
# EVALUATE CDEMO-CU00-USR-SEL-FLG, WHEN 'U' WHEN 'u' and WHEN 'D' WHEN 'd' (lines 189-201).
UPDATE_SELECTION_CODES = ("U", "u")
DELETE_SELECTION_CODES = ("D", "d")
# Every path ends with MOVE -1 TO USRIDINL: the cursor goes to the search field.
SEARCH_CURSOR_FIELD = "search_user_id"
REFUSAL_STATUS = HTTP_EDIT_FAILURE
LOOKUP_ERROR_STATUS = HTTP_FILE_ERROR
# MOVE SPACES TO WS-MESSAGE (MAIN-PARA): a page with no paging literal has a blank line.
NO_MESSAGE = ""
# ENTER moves 0 to CDEMO-CU00-PAGE-NUM before PROCESS-PAGE-FORWARD adds 1 (line 227).
ENTER_BASE_PAGE = 0
FIRST_PAGE = 1
# CDEMO-CU00-PAGE-NUM PIC 9(08) (line 70): COMPUTE ... + 1 with no ON SIZE ERROR (lines 309-310,
# 320-321) keeps the low-order eight digits.
PAGE_NUMBER_MODULUS = 10**8
# A blank last key is NEXT-PAGE-NO: F8 refuses it before any read (PROCESS-PF8-KEY).
NO_NEXT_PAGE_KEY = ""
# The first key of no shown page: a GET query carries none, so there are no rows to re-read.
NO_SHOWN_KEY = ""
# The UserListResponse fields an invalid-selection 400 carries beside the four standard keys:
# those UserSelectionErrorResponse adds to LegacyErrorResponse, so the body keeps to its schema.
LIST_PAGE_BODY_FIELDS = set(UserSelectionErrorResponse.model_fields) - set(
    LegacyErrorResponse.model_fields
)
BLANK_CHARACTER = " "


async def ListUsers(context: OnlineContext, query: UserListQuery) -> UserListResponse:
    """Port the COUSR00 browse keys: ENTER and the first entry, F8 and F7.

    The first entry (MAIN-PARA lines 115-119) and ENTER run PROCESS-ENTER-KEY, whose browse
    starts at the search key (lines 218-228, then PROCESS-PAGE-FORWARD). F8 is PROCESS-PF8-KEY
    (lines 260-277) and F7 PROCESS-PF7-KEY (lines 237-255), each followed by
    PROCESS-PAGE-FORWARD or PROCESS-PAGE-BACKWARD.

    Args:
        context: The admin request context; its session serves the USRSEC browse.
        query: No ``direction`` is ENTER and the first entry, browsing from
            ``search_user_id``; forward is F8 after the last key ``start_key``; backward is F7
            before the first key ``start_key``; both from page ``page``.

    Returns:
        The page of at most 10 users, its page number, its first and last user IDs, and the
        paging literal that applies, or an empty message. An ENTER search that finds no user
        ID at or after its key answers ``You are at the top of the page...`` on page 1 with no
        rows, as the query carries no shown page, and a blank last key.

    Raises:
        LegacyMessageError: 400 with ``You are already at the top of the page...`` (F7 on page
            1 or 0), ``You are already at the bottom of the page...`` (F8 with a blank last key
            or with nothing after it), ``You are at the top of the page...`` (F7's STARTBR
            NOTFND) or ``You have reached the top of the page...`` (nothing before the first
            key), each with the four standard keys only; 503 with ``Unable to lookup User...``
            while the batch window holds USRSEC or on a database failure.
    """
    repository = UserRepository(context)
    if query.direction is None:
        return await _BrowseFromSearch(repository, query.search_user_id, NO_MESSAGE, NO_SHOWN_KEY)
    if query.direction == Direction.BACKWARD:
        return await _PageBackward(repository, query.start_key, query.page)
    return await _PageForward(repository, query.start_key, query.page)


async def ProcessUserSelection(
    context: OnlineContext, request: UserSelectionRequest
) -> UserSelectionResponse:
    """Port ENTER with selection codes, PROCESS-ENTER-KEY (lines 149-232).

    The first row with a non-blank code is the selection (EVALUATE TRUE, lines 151-185). With
    a non-blank user ID, ``U`` or ``u`` routes to the update screen and ``D`` or ``d`` to the
    delete screen, ending the request as XCTL ended the task. Any other code sets
    ``Invalid selection. Valid values are U and D`` and processing continues: the list is
    browsed from ``search_user_id``, and a paging literal set by that browse replaces it.

    Args:
        context: The admin request context; its session serves the USRSEC browse.
        request: The shown rows with their codes in screen order, and the search key as keyed.
            The first row's user ID is the first key shown.

    Returns:
        The route of a valid selection with empty list fields, or ``route`` None with the
        re-browsed page and its message. A search that finds no user ID at or after its key
        answers ``You are at the top of the page...`` on page 1, with the rows still shown read
        again from the first key shown and a blank last key, from which F8 answers
        ``You are already at the bottom of the page...``.

    Raises:
        LegacyMessageError: 400 ``Invalid selection. Valid values are U and D`` when the
            browse leaves that literal on the ERRMSG line, its body adding the re-browsed
            page's ``rows``, ``page``, ``first_key`` and ``last_key``; 503
            ``Unable to lookup User...`` while the batch window holds USRSEC or on a database
            failure.
    """
    selection = _FindFirstSelection(request.selections)
    priorMessage = NO_MESSAGE
    if selection is not None and not _IsBlank(selection.user_id):
        route = _RouteFor(selection)
        if route is not None:
            return UserSelectionResponse(route=route)
        priorMessage = INVALID_SELECTION_MESSAGE
    repository = UserRepository(context)
    firstShownKey = request.selections[0].user_id if request.selections else NO_SHOWN_KEY
    listPage = await _BrowseFromSearch(
        repository, request.search_user_id, priorMessage, firstShownKey
    )
    if listPage.message == INVALID_SELECTION_MESSAGE:
        raise _InvalidSelectionRefusal(listPage)
    return UserSelectionResponse(**listPage.model_dump())


def _IsBlank(text: str) -> bool:
    """Return whether screen text is SPACES OR LOW-VALUES: empty or only spaces."""
    return text.strip(BLANK_CHARACTER) == ""


def _FindFirstSelection(selections: Sequence[UserSelection]) -> UserSelection | None:
    """Return the first row whose SELnnnn code is not blank; later codes are ignored."""
    for selection in selections:
        if not _IsBlank(selection.selection):
            return selection
    return None


def _RouteFor(selection: UserSelection) -> str | None:
    """Return the update or delete route for a U/u or D/d code, or None for any other code.

    The code is compared exactly and the user ID is passed as keyed, never case-folded.
    """
    if selection.selection in UPDATE_SELECTION_CODES:
        return _RouteWithUserId(UPDATE_ROUTE, selection.user_id)
    if selection.selection in DELETE_SELECTION_CODES:
        return _RouteWithUserId(DELETE_ROUTE, selection.user_id)
    return None


def _RouteWithUserId(route: str, userId: str) -> str:
    """Return ``route`` with the selected user ID as its ``userId`` query parameter."""
    return f"{route}?{urlencode({ROUTE_USER_ID_PARAMETER: userId})}"


def _Refusal(message: str) -> LegacyMessageError:
    """Return the 400 refusal of a browse that reads no row, with the cursor on the search."""
    return LegacyMessageError(message, field=SEARCH_CURSOR_FIELD, httpStatus=REFUSAL_STATUS)


def _InvalidSelectionRefusal(listPage: UserListResponse) -> LegacyMessageError:
    """Return the 400 of an invalid selection code the re-browsed page still shows.

    The edit failure rides on the page the browse built: the body adds its ``rows``, ``page``,
    ``first_key`` and ``last_key`` beside the four standard keys, JSON-ready, as
    ``UserSelectionErrorResponse`` declares, so the client shows that page under the
    invalid-selection literal.
    """
    pageBody = listPage.model_dump(mode="json", include=LIST_PAGE_BODY_FIELDS)
    return LegacyMessageError(
        INVALID_SELECTION_MESSAGE,
        field=SEARCH_CURSOR_FIELD,
        httpStatus=REFUSAL_STATUS,
        details=LegacyErrorDetails(extraBody=pageBody),
    )


async def _Lookup(
    browse: Awaitable[list[dict[str, Any]] | None], operation: str
) -> list[dict[str, Any]] | None:
    """Await one USRSEC browse; a file condition becomes ``Unable to lookup User...``.

    Ports the WHEN OTHER branches of the STARTBR, READNEXT and READPREV paragraphs (lines
    607-613, 641-647, 675-681). FileNotOpenError is the NOTOPEN of a USRSEC that CLOSEFIL closed.
    The condition is logged with ``operation``, the CICS commands of the browse awaited.
    """
    try:
        return await browse
    except (FileNotOpenError, RepositoryError) as exc:
        LogFileCondition(LOGGER, TRANSACTION_ID, operation, exc)
        raise LegacyMessageError(
            UNABLE_TO_LOOKUP_USER_MESSAGE,
            field=SEARCH_CURSOR_FIELD,
            httpStatus=LOOKUP_ERROR_STATUS,
        ) from exc


async def _BrowseFromSearch(
    repository: UserRepository, searchUserId: str, priorMessage: str, firstShownKey: str
) -> UserListResponse:
    """ENTER: browse from the search key, inclusive, as page 1 (lines 218-228, 282-331).

    A blank search key is LOW-VALUES, the lowest key (line 219); ENTER skips no READNEXT
    (line 288), so the first user ID at or after the key is shown. STARTBR NOTFND (lines
    600-606) sets USER-SEC-EOF and not ERR-FLG, so PROCESS-PAGE-FORWARD continues with no row
    read and sets NEXT-PAGE-NO (lines 317-322), with the rows left in place because
    POPULATE-USER-DATA never ran (lines 384-441). That outcome is ``_SearchNotFound``'s, from
    ``firstShownKey``, the first user ID of the page shown, or blank when none is shown. The
    ENDBR that follows (line 325) has no counterpart here, as no browse is held open, so that
    coded continuation is what is ported.
    """
    startKey = None if _IsBlank(searchUserId) else searchUserId
    browse = repository.BrowseForward(startKey, inclusive=True, limit=PAGE_SIZE)
    records = await _Lookup(browse, BROWSE_FORWARD_OPERATION)
    if records is None:
        return await _SearchNotFound(repository, firstShownKey)
    return _BuildForwardPage(records, ENTER_BASE_PAGE, priorMessage)


async def _SearchNotFound(repository: UserRepository, firstShownKey: str) -> UserListResponse:
    """Return ENTER's STARTBR NOTFND page: ``You are at the top of the page...`` on page 1.

    The literal replaces any earlier message, the invalid-selection literal included. Every
    ENTER search resets the page number to 1. The rows the program leaves shown are read again
    from ``firstShownKey``. The last key is blank, the NEXT-PAGE-NO the browse set, so the next
    F8 answers ``You are already at the bottom of the page...`` before any read, and F7 refuses
    as a page not above 1.
    """
    shownRows = await _RereadShownPage(repository, firstShownKey)
    notFoundPage = _BuildPage(shownRows, FIRST_PAGE, AT_TOP_OF_PAGE_MESSAGE)
    return notFoundPage.model_copy(update={"last_key": NO_NEXT_PAGE_KEY})


async def _RereadShownPage(repository: UserRepository, firstShownKey: str) -> list[dict[str, Any]]:
    """Read again the 10 user IDs at and after ``firstShownKey``: the page a NOTFND leaves shown.

    A blank key means no page is shown, so nothing is read; a key with no user ID at or after
    it any more shows no rows.
    """
    if _IsBlank(firstShownKey):
        return []
    browse = repository.BrowseForward(firstShownKey, inclusive=True, limit=PAGE_SIZE - 1)
    return await _Lookup(browse, BROWSE_FORWARD_OPERATION) or []


async def _PageForward(repository: UserRepository, lastKey: str, page: int) -> UserListResponse:
    """F8: the page after the last key shown (PROCESS-PF8-KEY, PROCESS-PAGE-FORWARD).

    The READNEXT skipped after STARTBR (line 289) makes the browse exclusive of the last key.
    A blank last key, which an ENTER's STARTBR NOTFND page carries, or no user ID after the
    key, is NEXT-PAGE-NO: ``You are already at the bottom of the page...`` with the shown rows
    kept (lines 270-276). The blank key is refused before any read; the page number plays no
    part in the test.
    """
    if _IsBlank(lastKey):
        raise _Refusal(ALREADY_AT_BOTTOM_MESSAGE)
    browse = repository.BrowseForward(lastKey, inclusive=False, limit=PAGE_SIZE)
    records = await _Lookup(browse, BROWSE_FORWARD_OPERATION)
    if not records:
        raise _Refusal(ALREADY_AT_BOTTOM_MESSAGE)
    return _BuildForwardPage(records, page, NO_MESSAGE)


def _BuildForwardPage(
    records: Sequence[Mapping[str, Any]], basePage: int, priorMessage: str
) -> UserListResponse:
    """Build a PROCESS-PAGE-FORWARD result (lines 298-323).

    ``records`` holds up to 10 page rows plus the look-ahead READNEXT. Without the look-ahead
    the browse reached ENDFILE, whose ``You have reached the bottom of the page...`` replaces
    any earlier message (lines 634-640). The page number grows by 1 once a row was read,
    modulo 10**8 as its PIC 9(08) truncates: page 99999999 is followed by page 0.
    """
    pageRows = records[:PAGE_SIZE]
    hasNextPage = len(records) > PAGE_SIZE
    message = priorMessage if hasNextPage else REACHED_BOTTOM_MESSAGE
    page = (basePage + 1) % PAGE_NUMBER_MODULUS if pageRows else basePage
    return _BuildPage(pageRows, page, message)


async def _PageBackward(repository: UserRepository, firstKey: str, page: int) -> UserListResponse:
    """F7: the page before the first key shown (PROCESS-PF7-KEY, PROCESS-PAGE-BACKWARD).

    Page 1 refuses with ``You are already at the top of the page...`` (lines 248-255). A blank
    first key is LOW-VALUES (line 240). The READPREV skipped after STARTBR (line 343) makes the
    browse exclusive of the first key. STARTBR NOTFND and a READPREV ENDFILE before any row
    leave the shown rows in place (lines 600-606, 668-674).
    """
    if page <= FIRST_PAGE:
        raise _Refusal(ALREADY_AT_TOP_MESSAGE)
    startKey = None if _IsBlank(firstKey) else firstKey
    browse = repository.BrowseBackward(startKey, limit=PAGE_SIZE)
    records = await _Lookup(browse, BROWSE_BACKWARD_OPERATION)
    if records is None:
        raise _Refusal(AT_TOP_OF_PAGE_MESSAGE)
    if not records:
        raise _Refusal(REACHED_TOP_MESSAGE)
    return _BuildBackwardPage(records, page)


def _BuildBackwardPage(records: Sequence[Mapping[str, Any]], page: int) -> UserListResponse:
    """Build a PROCESS-PAGE-BACKWARD result (lines 351-372).

    ``records`` holds the users before the first key, nearest first: up to 10 page rows plus
    the look-ahead READPREV. The program filled rows 10 down to 1, so the page is the nearest
    10 in ascending order. Fewer than 10 is ENDFILE with the page number unchanged; exactly 10
    with ENDFILE on the look-ahead resets it to 1; a look-ahead row lowers it by 1.
    """
    pageRows = list(reversed(records[:PAGE_SIZE]))
    if len(records) < PAGE_SIZE:
        return _BuildPage(pageRows, page, REACHED_TOP_MESSAGE)
    if len(records) > PAGE_SIZE:
        return _BuildPage(pageRows, page - 1, NO_MESSAGE)
    return _BuildPage(pageRows, FIRST_PAGE, REACHED_TOP_MESSAGE)


def _BuildPage(pageRows: Sequence[Mapping[str, Any]], page: int, message: str) -> UserListResponse:
    """Return the COUSR00 screen: the rows, the page number, the first and last keys, ERRMSG.

    The first and last user IDs replace CDEMO-CU00-USRID-FIRST and -LAST (lines 388-389,
    434-435); they are raw keys, which the client sends back as ``startKey``.
    """
    listRows = [_ToListRow(record) for record in pageRows]
    return UserListResponse(
        rows=listRows,
        page=page,
        first_key=listRows[0].user_id if listRows else "",
        last_key=listRows[-1].user_id if listRows else "",
        message=message,
        cursor_field=SEARCH_CURSOR_FIELD,
    )


def _ToListRow(record: Mapping[str, Any]) -> UserListRow:
    """Return the USRIDnn, FNAMEnn, LNAMEnn and UTYPEnn fields of one SEC-USER-DATA record.

    POPULATE-USER-DATA moves only these four fields; ``password_hash`` is never read.
    """
    return UserListRow(
        user_id=record["user_id"],
        first_name=record["first_name"],
        last_name=record["last_name"],
        user_type=record["user_type"],
    )
