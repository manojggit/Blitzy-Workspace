"""Unit tests for the list projections of the CCLI and CT00 browses.

app.repositories.card_repository ports the COCRDLIC STARTBR, READNEXT and READPREV loops over
CARDDAT, and app.repositories.transaction_repository the COTRN00C browses of TRANSACT. A list
row shows a few fields of its record: COCRDLIC 1200-SCREEN-ARRAY-INIT moves the card number,
account and status, and COTRN00C POPULATE-TRAN-DATA the TRAN-ID, original timestamp, description
and amount. These tests pin that the CCLI forward page, look-ahead and backward page, and the
CT00 forward and backward browses, select exactly those columns (plus the card sort key the
look-ahead continues from), and never the CVV, embossed name, expiry, merchant fields,
transaction card number or processing timestamp. They also pin that the CCLI look-ahead READNEXT
still runs after a full page, with no filter, from the last record's sort key, and becomes
element ``limit`` of the browse.

The card request session is a stub that records each statement and answers it with canned
rows, so no database is needed; CARDDAT is no batch-window file, so the stub request is never
read. The TRANSACT statements come straight from the module's statement builders.
"""

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Select

from app.core.dependencies import OnlineContext
from app.repositories.card_repository import (
    BROWSE_BACKWARD,
    BROWSE_FORWARD,
    CardFilters,
    CardRepository,
)
from app.repositories.transaction_repository import (
    BackwardBrowseStatement,
    ForwardBrowseStatement,
)

# WS-MAX-SCREEN-LINES of COCRDLIC: the CCLI page size.
CARD_PAGE_SIZE = 7
# An invented CARD-ACCT-ID 9(11) and CARD-NUM X(16); the stub session never looks them up.
ACCOUNT_ID = "00000000042"
CARD_START_KEY = "0000000000000009"
CARD_NUM_LENGTH = 16
# An invented TRAN-ID X(16) and the limit + 1 rows a CT00 page browse reads.
TRAN_START_KEY = "0000000000000001"
TRAN_ROW_LIMIT = 11
# The CARD-RECORD fields 1200-SCREEN-ARRAY-INIT reads, plus the look-ahead sort key.
CARD_LIST_COLUMNS = ["card_num", "card_num_sort", "acct_id", "active_status"]
CARD_UNREAD_COLUMNS = {"cvv_cd", "embossed_name", "expiration_date"}
# The TRAN-RECORD fields POPULATE-TRAN-DATA reads.
TRANSACTION_LIST_COLUMNS = ["tran_id", "orig_ts", "orig_ts_format", "tran_desc", "tran_amt"]
TRANSACTION_UNREAD_COLUMNS = {
    "merchant_id",
    "merchant_name",
    "merchant_city",
    "merchant_zip",
    "card_num",
    "proc_ts",
}
# The only condition of the look-ahead READNEXT: no 9500-FILTER-RECORDS filter.
LOOK_AHEAD_CONDITION = "cards.cards.card_num_sort > :card_num_sort_1"


class ProjectionCase(NamedTuple):
    """One browse statement source and the columns a list row needs from it."""

    readStatement: Callable[[], Awaitable[Select[Any]]]
    listColumns: list[str]
    unreadColumns: set[str]


def CardRow(position):
    """Return a canned CARDDAT browse row whose key and sort key both encode ``position``."""
    cardNum = str(position).zfill(CARD_NUM_LENGTH)
    return {
        "card_num": cardNum,
        "card_num_sort": cardNum,
        "acct_id": ACCOUNT_ID,
        "active_status": "Y",
    }


def FullPageAnswers():
    """Return the answers of a full forward browse: a page of seven rows, then the look-ahead."""
    pageRows = [CardRow(position) for position in range(1, CARD_PAGE_SIZE + 1)]
    return [pageRows, [CardRow(CARD_PAGE_SIZE + 1)]]


def CannedResult(rows):
    """Return a statement result whose ``mappings().all()`` gives ``rows``."""
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    return result


def BuildContext(answers):
    """Return an online context whose session answers each execute with the next row list."""
    execute = AsyncMock(side_effect=[CannedResult(rows) for rows in answers])
    return OnlineContext(request=None, session=SimpleNamespace(execute=execute), settings=None)


async def RunCardBrowse(direction, answers):
    """Run one account-filtered CCLI browse over the stub; return its records and statements."""
    context = BuildContext(answers)
    filters = CardFilters(acctId=ACCOUNT_ID)
    repository = CardRepository(context)
    records = await repository.BrowseCards(filters, CARD_START_KEY, direction, CARD_PAGE_SIZE)
    statements = [call.args[0] for call in context.session.execute.call_args_list]
    return records, statements


async def CardForwardPageStatement():
    """Return the 9000-READ-FORWARD page statement of a full CCLI page."""
    _, statements = await RunCardBrowse(BROWSE_FORWARD, FullPageAnswers())
    return statements[0]


async def CardLookAheadStatement():
    """Return the look-ahead READNEXT statement that follows a full CCLI page."""
    _, statements = await RunCardBrowse(BROWSE_FORWARD, FullPageAnswers())
    return statements[1]


async def CardBackwardPageStatement():
    """Return the 9100-READ-BACKWARDS page statement of CCLI."""
    _, statements = await RunCardBrowse(BROWSE_BACKWARD, [[CardRow(1)]])
    return statements[0]


async def TransactionForwardStatement():
    """Return the CT00 PROCESS-PAGE-FORWARD browse statement."""
    return ForwardBrowseStatement(TRAN_START_KEY, TRAN_ROW_LIMIT)


async def TransactionBackwardStatement():
    """Return the CT00 PROCESS-PAGE-BACKWARD browse statement."""
    return BackwardBrowseStatement(TRAN_START_KEY, TRAN_ROW_LIMIT)


PROJECTION_CASES = {
    "ccli-forward-page": ProjectionCase(
        CardForwardPageStatement, CARD_LIST_COLUMNS, CARD_UNREAD_COLUMNS
    ),
    "ccli-look-ahead": ProjectionCase(
        CardLookAheadStatement, CARD_LIST_COLUMNS, CARD_UNREAD_COLUMNS
    ),
    "ccli-backward-page": ProjectionCase(
        CardBackwardPageStatement, CARD_LIST_COLUMNS, CARD_UNREAD_COLUMNS
    ),
    "ct00-forward": ProjectionCase(
        TransactionForwardStatement, TRANSACTION_LIST_COLUMNS, TRANSACTION_UNREAD_COLUMNS
    ),
    "ct00-backward": ProjectionCase(
        TransactionBackwardStatement, TRANSACTION_LIST_COLUMNS, TRANSACTION_UNREAD_COLUMNS
    ),
}


@pytest.mark.parametrize(
    "projectionCase", list(PROJECTION_CASES.values()), ids=list(PROJECTION_CASES)
)
async def test_list_browse_selects_only_the_list_columns(projectionCase):
    statement = await projectionCase.readStatement()
    selectedColumns = [column.name for column in statement.selected_columns]
    assert selectedColumns == projectionCase.listColumns
    assert projectionCase.unreadColumns.isdisjoint(selectedColumns)


async def test_look_ahead_after_full_page_is_unfiltered_from_last_sort_key():
    answers = FullPageAnswers()
    lastSortKey = answers[0][-1]["card_num_sort"]
    lookAheadRow = answers[1][0]
    records, statements = await RunCardBrowse(BROWSE_FORWARD, answers)
    assert len(statements) == 2
    pageStatement, lookAheadStatement = statements
    assert pageStatement.compile().params["acct_id_1"] == ACCOUNT_ID
    assert str(lookAheadStatement.whereclause) == LOOK_AHEAD_CONDITION
    assert lookAheadStatement.compile().params == {"card_num_sort_1": lastSortKey, "param_1": 1}
    assert len(records) == CARD_PAGE_SIZE + 1
    assert records[CARD_PAGE_SIZE] == lookAheadRow
