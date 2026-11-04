"""Integration tests of CCLI (COCRDLIC, BMS map COCRDLI), CCDL (COCRDSLC, BMS map COCRDSL) and
CCUP (COCRDUPC, BMS map COCRDUP), through the ASGI app against the migrated, seeded test database.

Expected values are typed from the COBOL sources and copybook CVACT02Y, never read from the
services under test:

- CCLI: seven cards a page (cbl/COCRDLIC.cbl lines 177-178) in EBCDIC card-number order, which
  equals digit order for the all-digit seed keys; F8, F7, the paging, filter and selection
  literals (lines 899-1244); and the XCTL on ``S`` and ``U`` (lines 532-561), replaced by
  ``POST /cards/selection`` returning an opaque ``card_ref`` (AAP 0.7.1).
- CCDL: the account and card key edits and the read by card number only (cbl/COCRDSLC.cbl
  lines 626-775).
- CCUP: the detail edits (cbl/COCRDUPC.cbl lines 645-692), the ENTER, F5 and F12 decision on the
  screen token's state (lines 948-1022), the information line of every state (3250-SETUP-INFOMSG,
  lines 1138-1159) and the rewrite of 9200-WRITE-PROCESSING (lines 1429-1511). Lock and
  before-image conflicts belong to test_concurrency.py.
- Roles: the Style B reset of the COMMAREA user type to U (cbl/COCRDLIC.cbl line 320) is
  superseded by the session identity (AAP 0.6.12, 0.7.1). An admin is refused with 403 and an
  empty message and stays an admin; every other stored type lists cards as itself.

No response may carry a full 16-digit card number or a CVV key. Card rows are changed only
through ``syncEngine``; ``restoreSeedState`` restores the seed after every test.
"""

import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from typing import Any, NamedTuple

import httpx
import pytest
import pytest_asyncio
from cryptography import fernet
from fastapi import FastAPI
from sqlalchemy import Engine, text

from app.core.security import SECONDS_PER_MINUTE

# pytest reads this module variable by its own lower-case name, so it keeps that name rather than
# the upper-case constant form of MK_Modernization Rule clause 1. Every CCUP test commits card
# changes through syncEngine, which the restore undoes.
pytestmark = pytest.mark.usefixtures("restoreSeedState")

# --- Paths ---------------------------------------------------------------------------------------
API_V1 = "/api/v1"
CARDS_URL = API_V1 + "/cards"
SELECTION_URL = CARDS_URL + "/selection"
EDIT_SUFFIX = "/edit"
ADMIN_MENU_URL = API_V1 + "/menu/admin"
CURRENT_USER_URL = API_V1 + "/auth/me"

# --- HTTP statuses of the legacy outcomes (AAP 0.6.5) --------------------------------------------
HTTP_OK = 200
HTTP_EDIT_FAILURE = 400
HTTP_SIGN_ON_FAILURE = 401
HTTP_ACCESS_REFUSED = 403
HTTP_NOT_FOUND = 404
HTTP_FILE_ERROR = 503
# A request no 3270 terminal could send, refused before any edit (AAP 0.6.4).
HTTP_MALFORMED = 422
LEGACY_ERROR_KEYS = frozenset({"message", "info_message", "field_errors", "cursor_field"})

# --- Seed data (data/EBCDIC/AWS.M2.CARDDEMO.CARDDATA.PS, one card per account) -------------------
SAMPLE_CARD_NUMBER = "2988091353094312"
SAMPLE_ACCOUNT_ID = "00000000004"
SAMPLE_CARD_URL = f"{CARDS_URL}/{SAMPLE_CARD_NUMBER}"
UNKNOWN_ACCOUNT_ID = "99999999999"
SEED_CARD_COUNT = 50
# WS-MAX-SCREEN-LINES (cbl/COCRDLIC.cbl lines 177-178): 50 cards are 7 full pages and 1 card.
PAGE_SIZE_ROWS = 7
EXPECTED_PAGE_SIZES = [PAGE_SIZE_ROWS] * (SEED_CARD_COUNT // PAGE_SIZE_ROWS) + [
    SEED_CARD_COUNT % PAGE_SIZE_ROWS
]
# WS-CA-SCREEN-NUM is PIC 9(1), so no paging walk can need more than 9 pages.
MAX_SCREEN_PAGES = 9
MASKED_PAN_PREFIX = "*" * 12
LAST_FOUR = 4
MASKED_SAMPLE_CARD = MASKED_PAN_PREFIX + SAMPLE_CARD_NUMBER[-LAST_FOUR:]
LIST_ROW_KEYS = frozenset({"card_ref", "acct_id", "card_num_masked", "active_status"})
RAW_PAN_PATTERN = re.compile(r"(?<!\d)\d{16}(?!\d)")
CVV_KEY_MARKERS = ("cvv", "verification")
# The row each selection test codes; any row of the first page would do.
VIEW_ROW_INDEX = 2
UPDATE_ROW_INDEX = 4

# --- CCLI literals (cbl/COCRDLIC.cbl lines 115-126, 903, 908, 1022, 1058, 1219) ------------------
RECORD_ACTIONS_INFO = "TYPE S FOR DETAIL, U TO UPDATE ANY RECORD"
NO_RECORDS_FOUND_MESSAGE = "NO RECORDS FOUND FOR THIS SEARCH CONDITION."
NO_PREVIOUS_PAGES_MESSAGE = "NO PREVIOUS PAGES TO DISPLAY"
NO_MORE_PAGES_MESSAGE = "NO MORE PAGES TO DISPLAY"
NO_MORE_RECORDS_MESSAGE = "NO MORE RECORDS TO SHOW"
MORE_THAN_ONE_MESSAGE = "PLEASE SELECT ONLY ONE RECORD TO VIEW OR UPDATE"
INVALID_ACTION_MESSAGE = "INVALID ACTION CODE"
ACCOUNT_FILTER_MESSAGE = "ACCOUNT FILTER,IF SUPPLIED MUST BE A 11 DIGIT NUMBER"
CARD_FILTER_MESSAGE = "CARD ID FILTER,IF SUPPLIED MUST BE A 16 DIGIT NUMBER"
FORWARD = "forward"
BACKWARD = "backward"
# F8 sends the page's last_key, F7 its first_key.
PAGE_KEY_BY_DIRECTION = {FORWARD: "last_key", BACKWARD: "first_key"}

# --- CCDL literals (cbl/COCRDSLC.cbl lines 126-154) ----------------------------------------------
DETAILS_SHOWN_INFO = "   Displaying requested details"
PROMPT_FOR_KEYS_INFO = "Please enter Account and Card Number"
ACCOUNT_NOT_PROVIDED_MESSAGE = "Account number not provided"
NO_INPUT_MESSAGE = "No input received"
CARD_NOT_FOUND_MESSAGE = "Did not find cards for this search condition"
# secrets.token_urlsafe(40) is 54 characters: longer than CARDSID, so a card_ref to the routes.
FORGED_CARD_REF_BYTES = 40
FORGED_SCREEN_TOKEN_BYTES = 80
# Seconds past a Fernet token's lifetime that the moved clock reads.
EXPIRY_MARGIN_SECONDS = 60

# --- CCUP literals (cbl/COCRDUPC.cbl lines 157-210) ----------------------------------------------
CARD_NAME_NOT_PROVIDED_MESSAGE = "Card name not provided"
CARD_NAME_ALPHA_MESSAGE = "Card name can only contain alphabets and spaces"
EXPIRY_MONTH_MESSAGE = "Card expiry month must be between 1 and 12"
EXPIRY_YEAR_MESSAGE = "Invalid card expiry year"
NO_CHANGES_MESSAGE = "No change detected with respect to values fetched."
UPDATE_FAILED_MESSAGE = "Update of record failed"
DETAILS_NOT_FETCHED = "DETAILS_NOT_FETCHED"
SHOW_DETAILS = "SHOW_DETAILS"
CHANGES_NOT_OK = "CHANGES_NOT_OK"
CHANGES_OK_NOT_CONFIRMED = "CHANGES_OK_NOT_CONFIRMED"
CHANGES_DONE = "CHANGES_DONE"
CHANGES_FAILED = "CHANGES_FAILED"
# 3250-SETUP-INFOMSG (lines 1138-1159) with the WS-INFO-MSG 88 levels (lines 157-171).
CARD_INFO_BY_STATE = {
    DETAILS_NOT_FETCHED: "Please enter Account and Card Number",
    SHOW_DETAILS: "Details of selected card shown above",
    CHANGES_NOT_OK: "Update card details presented above.",
    CHANGES_OK_NOT_CONFIRMED: "Changes validated.Press F5 to save",
    CHANGES_DONE: "Changes committed to database",
    CHANGES_FAILED: "Changes unsuccessful. Please try again",
}
ENTER = "enter"
SAVE = "save"
CANCEL = "cancel"

# --- COCRDUP screen fields, as CardScreenFields names them ---------------------------------------
ACCT_ID_FIELD = "acct_id"
CARD_NUM_FIELD = "card_num"
NAME_FIELD = "embossed_name"
STATUS_FIELD = "active_status"
MONTH_FIELD = "expiry_month"
YEAR_FIELD = "expiry_year"
DAY_FIELD = "expiry_day"
DETAIL_FIELD_NAMES = (NAME_FIELD, STATUS_FIELD, MONTH_FIELD, YEAR_FIELD)
SCREEN_FIELD_NAMES = (ACCT_ID_FIELD, CARD_NUM_FIELD, *DETAIL_FIELD_NAMES, DAY_FIELD)
EMPTY_SCREEN_FIELDS = dict.fromkeys(SCREEN_FIELD_NAMES, "")

# The card every CCUP test edits: a letters-and-spaces name, status Y and a 2025 expiry whose
# day 31 exists in March but not in February, so one before-image day serves the rewrite and
# its failure. COCRDUPC shows the fetched name upper-cased (lines 1354-1367) and no day.
EDITABLE_NAME = "Edit Card Holder"
EDITABLE_STATUS = "Y"
EDITABLE_EXPIRY_DATE = date(2025, 1, 31)
EDITABLE_SCREEN_FIELDS = {
    ACCT_ID_FIELD: SAMPLE_ACCOUNT_ID,
    CARD_NUM_FIELD: MASKED_SAMPLE_CARD,
    NAME_FIELD: EDITABLE_NAME.upper(),
    STATUS_FIELD: EDITABLE_STATUS,
    MONTH_FIELD: f"{EDITABLE_EXPIRY_DATE.month:02d}",
    YEAR_FIELD: f"{EDITABLE_EXPIRY_DATE.year:04d}",
    DAY_FIELD: "",
}
RENAMED_NAME = "Renamed Card Holder"
RENAME_CHANGES = {NAME_FIELD: RENAMED_NAME}
INVALID_DETAIL_CHANGES = {NAME_FIELD: "12AB", STATUS_FIELD: "Z"}
DISCARDED_CHANGES = {NAME_FIELD: "Discarded Edit", STATUS_FIELD: "N"}
# F5 rewrites the validated name as entered, the new month and year and the before-image day.
SAVED_NAME = "Saved Card Holder"
SAVE_CHANGES = {NAME_FIELD: SAVED_NAME, MONTH_FIELD: "03", YEAR_FIELD: "2030"}
SAVED_COLUMNS = {
    "embossed_name": SAVED_NAME,
    "expiration_date": date(2030, 3, EDITABLE_EXPIRY_DATE.day),
}
IGNORED_SAVE_FIELDS = {NAME_FIELD: "Ignored Holder", STATUS_FIELD: "N", YEAR_FIELD: "2040"}
IMPOSSIBLE_DATE_CHANGES = {MONTH_FIELD: "02"}

# --- Roles and origins ---------------------------------------------------------------------------
ADMIN_USER_TYPE = "A"
OTHER_USER_TYPES = ("X", "u")
FOREIGN_ORIGIN = "http://127.0.0.1:9"

# --- SQL: constant text, values always bound -----------------------------------------------------
CARD_ORDER_SQL = text(
    "SELECT card_num, acct_id, active_status FROM cards.cards ORDER BY card_num_sort"
)
CARD_ROW_SQL = text(
    "SELECT card_num, acct_id, cvv_cd, embossed_name, expiration_date, active_status"
    " FROM cards.cards WHERE card_num = :cardNum"
)
MAKE_EDITABLE_SQL = text(
    "UPDATE cards.cards SET embossed_name = :embossedName, active_status = :activeStatus,"
    " expiration_date = :expirationDate WHERE card_num = :cardNum RETURNING acct_id"
)


# --- Value types ---------------------------------------------------------------------------------
class FilterCase(NamedTuple):
    """CCLI filter text and the first literal and failing fields 2210/2220 give it."""

    query: dict[str, str]
    message: str
    fieldErrors: list[str]


class SelectionCase(NamedTuple):
    """CRDSELn codes by row index and the 2250-EDIT-ARRAY outcome they give."""

    codesByRow: dict[int, str]
    message: str
    fieldErrors: list[str]
    cursorField: str


class KeyEditCase(NamedTuple):
    """A typed CCDL card key and account query, and the 2200-EDIT-MAP-INPUTS outcome."""

    cardKey: str
    query: dict[str, str]
    message: str
    fieldErrors: list[str]


class CardRefCase(NamedTuple):
    """How a card_ref is spoiled (expired, else forged) and the route it is sent to."""

    expire: bool
    routeSuffix: str


class CardKeySourceCase(NamedTuple):
    """A ``{cardNum}`` value of at most 16 characters, its query, and the outcome they give.

    The query's ``isCardRef`` declares whether the value is a card_ref or the CARDSID text. A
    legacy outcome shows the key prompt and no ``detail``; a 422 shows no prompt and FastAPI's
    ``detail``.
    """

    cardKey: str
    query: dict[str, str]
    status: int
    message: str
    fieldErrors: list[str]
    infoMessage: str = PROMPT_FOR_KEYS_INFO
    detail: list[dict[str, Any]] | None = None


class DetailEditCase(NamedTuple):
    """Detail field changes keyed on a fetched CCUP screen and the ENTER outcome."""

    changes: dict[str, str]
    status: int
    message: str
    state: str


class EndpointCall(NamedTuple):
    """One request to a card endpoint, with its query and JSON body."""

    method: str
    path: str
    params: dict[str, str] | None = None
    body: dict[str, Any] | None = None


FILTER_CASES = [
    pytest.param(FilterCase({"acctId": "ABC"}, ACCOUNT_FILTER_MESSAGE, [ACCT_ID_FIELD]), id="acct"),
    pytest.param(FilterCase({"cardNum": "12"}, CARD_FILTER_MESSAGE, [CARD_NUM_FIELD]), id="card"),
    pytest.param(
        FilterCase(
            {"acctId": "ABC", "cardNum": "12"},
            ACCOUNT_FILTER_MESSAGE,
            [ACCT_ID_FIELD, CARD_NUM_FIELD],
        ),
        id="both",
    ),
]
# Rows 2 to 7 take the cursor when flagged; row 1 has no MOVE -1, so the cursor stays on ACCTSID.
SELECTION_ERROR_CASES = [
    pytest.param(
        SelectionCase(
            {0: "S", 1: "U"},
            MORE_THAN_ONE_MESSAGE,
            ["rows[0].selection", "rows[1].selection"],
            "rows[1].selection",
        ),
        id="two-selected",
    ),
    pytest.param(
        SelectionCase({2: "X"}, INVALID_ACTION_MESSAGE, ["rows[2].selection"], "rows[2].selection"),
        id="code-x",
    ),
    pytest.param(
        SelectionCase({0: "s"}, INVALID_ACTION_MESSAGE, ["rows[0].selection"], ACCT_ID_FIELD),
        id="lower-case-s",
    ),
]
KEY_EDIT_CASES = [
    pytest.param(
        KeyEditCase(SAMPLE_CARD_NUMBER, {}, ACCOUNT_NOT_PROVIDED_MESSAGE, [ACCT_ID_FIELD]),
        id="no-account",
    ),
    pytest.param(
        KeyEditCase(SAMPLE_CARD_NUMBER, {"acctId": "ABC"}, ACCOUNT_FILTER_MESSAGE, [ACCT_ID_FIELD]),
        id="account-not-numeric",
    ),
    pytest.param(
        KeyEditCase("12", {"acctId": SAMPLE_ACCOUNT_ID}, CARD_FILTER_MESSAGE, [CARD_NUM_FIELD]),
        id="card-too-short",
    ),
]
CARD_REF_CASES = [
    pytest.param(CardRefCase(expire=False, routeSuffix=""), id="forged-view"),
    pytest.param(CardRefCase(expire=True, routeSuffix=""), id="expired-view"),
    pytest.param(CardRefCase(expire=False, routeSuffix=EDIT_SUFFIX), id="forged-edit"),
    pytest.param(CardRefCase(expire=True, routeSuffix=EDIT_SUFFIX), id="expired-edit"),
]
# 88 VALID-MONTH 1 THRU 12 and 88 VALID-YEAR 1950 THRU 2099 (cbl/COCRDUPC.cbl lines 95, 99).
DETAIL_EDIT_CASES = [
    pytest.param(
        DetailEditCase(
            {MONTH_FIELD: "13"}, HTTP_EDIT_FAILURE, EXPIRY_MONTH_MESSAGE, CHANGES_NOT_OK
        ),
        id="month-13",
    ),
    pytest.param(
        DetailEditCase(
            {YEAR_FIELD: "1949"}, HTTP_EDIT_FAILURE, EXPIRY_YEAR_MESSAGE, CHANGES_NOT_OK
        ),
        id="year-1949",
    ),
    pytest.param(
        DetailEditCase(
            {YEAR_FIELD: "2100"}, HTTP_EDIT_FAILURE, EXPIRY_YEAR_MESSAGE, CHANGES_NOT_OK
        ),
        id="year-2100",
    ),
    pytest.param(
        DetailEditCase({YEAR_FIELD: "1950"}, HTTP_OK, "", CHANGES_OK_NOT_CONFIRMED),
        id="year-1950",
    ),
    pytest.param(
        DetailEditCase({YEAR_FIELD: "2099"}, HTTP_OK, "", CHANGES_OK_NOT_CONFIRMED),
        id="year-2099",
    ),
    pytest.param(
        DetailEditCase(
            {NAME_FIELD: ""}, HTTP_EDIT_FAILURE, CARD_NAME_NOT_PROVIDED_MESSAGE, CHANGES_NOT_OK
        ),
        id="blank-name",
    ),
]
SAMPLE_KEY_QUERY = {"acctId": SAMPLE_ACCOUNT_ID}
# The frontend declares a path taken from a cardRef URL a card_ref token, whatever its length.
CARD_REF_DECLARATION = {"isCardRef": "true"}
CARD_KEY_DECLARATIONS = [
    pytest.param({}, id="by-length"),
    pytest.param(CARD_REF_DECLARATION, id="declared"),
]
# A cardRef value no server issued, short enough to be CARDSID text when typed.
SHORT_GARBLED_TEXT = "garbage"
# U+2603, which code page 037 lacks; the client sends it as %E2%98%83.
NON_CP037_SHORT_TEXT = "\u2603"
# The 422 detail of CARDSID text outside the repertoire (schemas.common.RequireCp037Printable).
NON_CP037_CARD_KEY_DETAIL = [
    {
        "loc": ["path", "cardNum"],
        "msg": "Value error, text contains characters a 3270 terminal cannot send (code page 037)",
        "type": "value_error",
    }
]
CARD_KEY_SOURCE_CASES = [
    pytest.param(
        CardKeySourceCase(
            SHORT_GARBLED_TEXT,
            SAMPLE_KEY_QUERY | CARD_REF_DECLARATION,
            HTTP_NOT_FOUND,
            CARD_NOT_FOUND_MESSAGE,
            [ACCT_ID_FIELD, CARD_NUM_FIELD],
        ),
        id="declared-short-text",
    ),
    pytest.param(
        CardKeySourceCase(
            SAMPLE_CARD_NUMBER,
            SAMPLE_KEY_QUERY | CARD_REF_DECLARATION,
            HTTP_NOT_FOUND,
            CARD_NOT_FOUND_MESSAGE,
            [ACCT_ID_FIELD, CARD_NUM_FIELD],
        ),
        id="declared-card-number",
    ),
    pytest.param(
        CardKeySourceCase(
            SHORT_GARBLED_TEXT,
            SAMPLE_KEY_QUERY,
            HTTP_EDIT_FAILURE,
            CARD_FILTER_MESSAGE,
            [CARD_NUM_FIELD],
        ),
        id="typed-short-text",
    ),
    pytest.param(
        CardKeySourceCase(
            NON_CP037_SHORT_TEXT,
            SAMPLE_KEY_QUERY | CARD_REF_DECLARATION,
            HTTP_NOT_FOUND,
            CARD_NOT_FOUND_MESSAGE,
            [ACCT_ID_FIELD, CARD_NUM_FIELD],
        ),
        id="declared-text-outside-cp037",
    ),
    pytest.param(
        CardKeySourceCase(
            NON_CP037_SHORT_TEXT,
            SAMPLE_KEY_QUERY,
            HTTP_MALFORMED,
            "",
            [CARD_NUM_FIELD],
            infoMessage="",
            detail=NON_CP037_CARD_KEY_DETAIL,
        ),
        id="typed-text-outside-cp037",
    ),
]
CARD_KEY_ROUTE_SUFFIXES = [pytest.param("", id="view"), pytest.param(EDIT_SUFFIX, id="edit")]
SELECTION_CALL = EndpointCall("POST", SELECTION_URL, body={"rows": []})
UPDATE_CALL = EndpointCall("PUT", SAMPLE_CARD_URL, body={"action": ENTER})
CARD_ENDPOINTS = [
    pytest.param(EndpointCall("GET", CARDS_URL), id="ccli-list"),
    pytest.param(SELECTION_CALL, id="ccli-selection"),
    pytest.param(EndpointCall("GET", SAMPLE_CARD_URL, SAMPLE_KEY_QUERY), id="ccdl-view"),
    pytest.param(
        EndpointCall("GET", SAMPLE_CARD_URL + EDIT_SUFFIX, SAMPLE_KEY_QUERY), id="ccup-fetch"
    ),
    pytest.param(UPDATE_CALL, id="ccup-update"),
]
WRITE_ENDPOINTS = [
    pytest.param(SELECTION_CALL, id="ccli-selection"),
    pytest.param(UPDATE_CALL, id="ccup-update"),
]


# --- Response assertions -------------------------------------------------------------------------
def AssertLegacyError(
    response: httpx.Response, status: int, message: str, cursorField: str = ""
) -> dict[str, Any]:
    """Assert a legacy error body with ``status`` and ``message``; return the body.

    ``cursorField`` is checked when given, replacing the program's MOVE -1 TO <field>L.
    """
    assert response.status_code == status, response.text
    body = response.json()
    assert LEGACY_ERROR_KEYS <= body.keys(), body
    assert body["message"] == message
    if cursorField:
        assert body["cursor_field"] == cursorField
    return body


def ExpectOk(response: httpx.Response) -> dict[str, Any]:
    """Assert a 200 response and return its JSON body."""
    assert response.status_code == HTTP_OK, response.text
    return response.json()


def AssertNoRawPan(responseText: str) -> None:
    """Assert no run of exactly 16 digits, a full card number, appears in a response."""
    assert RAW_PAN_PATTERN.search(responseText) is None, "a response carries a full card number"


def JsonKeys(node: object) -> set[str]:
    """Return every object key at any depth of a decoded JSON value."""
    if isinstance(node, Mapping):
        return set(node).union(*(JsonKeys(value) for value in node.values()))
    if isinstance(node, list):
        return set().union(*(JsonKeys(item) for item in node))
    return set()


def AssertCleanResponse(response: httpx.Response) -> None:
    """Assert a card response holds no full card number and no CVV key (AAP 0.7.1)."""
    AssertNoRawPan(response.text)
    keyNames = {key.lower() for key in JsonKeys(response.json())}
    assert not any(marker in key for key in keyNames for marker in CVV_KEY_MARKERS), keyNames


def AssertScreenState(body: Mapping[str, Any], state: str) -> None:
    """Assert a COCRDUP screen's state and the 3250-SETUP-INFOMSG line that state shows."""
    assert body["state"] == state
    assert body["info_message"] == CARD_INFO_BY_STATE[state]


def MaskedCard(cardNum: str) -> str:
    """Return a card number as every response shows it: twelve asterisks and the last 4."""
    return MASKED_PAN_PREFIX + cardNum[-LAST_FOUR:]


def RowIdentities(rows: list[Mapping[str, Any]]) -> list[tuple[str, str, str]]:
    """Return (account, masked card, status) of each CCLI row, in screen order."""
    return [(row["acct_id"], row["card_num_masked"], row["active_status"]) for row in rows]


def ExpectedIdentities(cardRows: list[Mapping[str, Any]]) -> list[tuple[str, str, str]]:
    """Return the CCLI row identities CARDDAT records must show, in the given order."""
    return [(row["acct_id"], MaskedCard(row["card_num"]), row["active_status"]) for row in cardRows]


def AssertDetailShowsCard(detail: Mapping[str, Any], card: Mapping[str, Any]) -> None:
    """Assert a COCRDSL screen shows the CARDDAT record, masked, with the details info line."""
    expiration = card["expiration_date"]
    assert detail["acct_id"] == card["acct_id"]
    assert detail["card_num_masked"] == MaskedCard(card["card_num"])
    assert detail["embossed_name"] == card["embossed_name"]
    assert detail["active_status"] == card["active_status"]
    assert detail["expiry_month"] == f"{expiration.month:02d}"
    assert detail["expiry_year"] == f"{expiration.year:04d}"
    assert detail["message"] == ""
    assert detail["info_message"] == DETAILS_SHOWN_INFO
    assert detail["card_ref"]


# --- Database access through syncEngine ----------------------------------------------------------
def ReadCardOrder(syncEngine: Engine) -> list[dict[str, Any]]:
    """Return every card's number, account and status in EBCDIC card-number order."""
    with syncEngine.connect() as connection:
        return [dict(row) for row in connection.execute(CARD_ORDER_SQL).mappings()]


def ReadCardRow(syncEngine: Engine, cardNum: str) -> dict[str, Any]:
    """Return the committed CARDDAT record of ``cardNum``, CVV included."""
    with syncEngine.connect() as connection:
        return dict(connection.execute(CARD_ROW_SQL, {"cardNum": cardNum}).mappings().one())


def MakeCardEditable(syncEngine: Engine, cardNum: str) -> str:
    """Commit a card every COCRDUPC edit accepts unchanged and return its account.

    The name is letters and spaces, the status Y and the expiry 2025-01-31.
    """
    values = {
        "embossedName": EDITABLE_NAME,
        "activeStatus": EDITABLE_STATUS,
        "expirationDate": EDITABLE_EXPIRY_DATE,
        "cardNum": cardNum,
    }
    with syncEngine.begin() as connection:
        return connection.execute(MAKE_EDITABLE_SQL, values).scalar_one()


# --- Requests ------------------------------------------------------------------------------------
def CardUrl(cardKey: str) -> str:
    """Return the CCDL and CCUP path of a typed card number or a card_ref."""
    return f"{CARDS_URL}/{cardKey}"


async def ListCards(client: httpx.AsyncClient, query: dict[str, Any]) -> httpx.Response:
    """Send one CCLI ENTER, F7 or F8 and check the response exposes no card data."""
    response = await client.get(CARDS_URL, params=query)
    AssertCleanResponse(response)
    return response


def PageQuery(listBody: Mapping[str, Any], direction: str) -> dict[str, Any]:
    """Return the F8 (forward) or F7 (backward) query the COCRDLI page sends from a page."""
    return {
        "startKey": listBody[PAGE_KEY_BY_DIRECTION[direction]],
        "page": listBody["page"],
        "direction": direction,
    }


async def FollowPagesForward(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Return the first page and every page F8 reaches until ``NO MORE RECORDS TO SHOW``."""
    pages = [ExpectOk(await ListCards(client, {}))]
    while pages[-1]["message"] != NO_MORE_RECORDS_MESSAGE and len(pages) < MAX_SCREEN_PAGES:
        nextResponse = await ListCards(client, PageQuery(pages[-1], FORWARD))
        pages.append(ExpectOk(nextResponse))
    return pages


async def PostSelection(
    client: httpx.AsyncClient, listBody: Mapping[str, Any], codesByRow: Mapping[int, str]
) -> httpx.Response:
    """Send ENTER on a shown page with the given CRDSELn codes; other rows stay blank."""
    rows = [
        {"card_ref": row["card_ref"], "selection": codesByRow.get(rowIndex, "")}
        for rowIndex, row in enumerate(listBody["rows"])
    ]
    selection = {"acct_id": "", "card_num": "", "rows": rows}
    response = await client.post(SELECTION_URL, json=selection)
    AssertCleanResponse(response)
    return response


async def SelectRow(
    client: httpx.AsyncClient, listBody: Mapping[str, Any], rowIndex: int, code: str
) -> httpx.Response:
    """Send ENTER with ``code`` keyed in row ``rowIndex`` of a shown page."""
    return await PostSelection(client, listBody, {rowIndex: code})


async def GetCard(client: httpx.AsyncClient, path: str, query: dict[str, str]) -> httpx.Response:
    """Send a CCDL view or CCUP fetch and check the response exposes no card data."""
    response = await client.get(path, params=query)
    AssertCleanResponse(response)
    return response


async def PutCard(
    client: httpx.AsyncClient, cardKey: str, body: Mapping[str, Any]
) -> httpx.Response:
    """Send one CCUP ENTER, F5 or F12 and check the response exposes no card data."""
    response = await client.put(CardUrl(cardKey), json=dict(body))
    AssertCleanResponse(response)
    return response


def UpdateBody(
    screen: Mapping[str, Any], action: str, changes: Mapping[str, str]
) -> dict[str, Any]:
    """Return a CCUP request from a screen: its token and its detail fields with ``changes``."""
    detailFields = {fieldName: screen["fields"][fieldName] for fieldName in DETAIL_FIELD_NAMES}
    return {
        "action": action,
        "screen_token": screen["screen_token"],
        "fields": detailFields | dict(changes),
    }


def BuildRequest(client: httpx.AsyncClient, call: EndpointCall) -> httpx.Request:
    """Return the request of ``call``, carrying the client's session cookie and Origin."""
    return client.build_request(call.method, call.path, params=call.params, json=call.body)


def ExpireFernetTokens(monkeypatch: pytest.MonkeyPatch, lifetimeMinutes: int) -> None:
    """Move the clock cryptography.fernet reads past every card_ref and screen_token TTL."""
    expiredAt = time.time() + lifetimeMinutes * SECONDS_PER_MINUTE + EXPIRY_MARGIN_SECONDS
    monkeypatch.setattr(fernet, "time", SimpleNamespace(time=lambda: expiredAt))


async def FetchEditScreen(client: httpx.AsyncClient, acctId: str) -> dict[str, Any]:
    """Open CCUP as the card list does: list the account's card, then fetch it by card_ref."""
    listBody = ExpectOk(await ListCards(client, {"acctId": acctId}))
    cardRef = listBody["rows"][0]["card_ref"]
    response = await GetCard(client, CardUrl(cardRef) + EDIT_SUFFIX, {"acctId": acctId})
    return ExpectOk(response)


@dataclass(frozen=True)
class CardUpdateSession:
    """A regular user's CCUP screen of the editable sample card, fetched from the card list.

    ``before`` is the committed record at fetch time; every PUT goes to the fetched card_ref.
    """

    client: httpx.AsyncClient
    syncEngine: Engine
    screen: dict[str, Any]
    before: dict[str, Any]

    async def Send(self, body: Mapping[str, Any]) -> httpx.Response:
        """PUT ``body`` on the fetched card's path."""
        return await PutCard(self.client, self.screen["card_ref"], body)

    async def Put(
        self, screen: Mapping[str, Any], action: str, changes: Mapping[str, str]
    ) -> httpx.Response:
        """PUT ``action`` with ``screen``'s token and detail fields, as changed."""
        return await self.Send(UpdateBody(screen, action, changes))

    async def Validate(self, changes: Mapping[str, str]) -> dict[str, Any]:
        """ENTER valid ``changes`` on the fetched screen; return the confirmation screen."""
        body = ExpectOk(await self.Put(self.screen, ENTER, changes))
        AssertScreenState(body, CHANGES_OK_NOT_CONFIRMED)
        return body

    def ReadCard(self) -> dict[str, Any]:
        """Return the sample card's committed record now."""
        return ReadCardRow(self.syncEngine, SAMPLE_CARD_NUMBER)


@pytest_asyncio.fixture(name="cardUpdate")
async def CardUpdateFixture(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> CardUpdateSession:
    """Return the CCUP session of the sample card, made editable and fetched in SHOW_DETAILS."""
    acctId = MakeCardEditable(syncEngine, SAMPLE_CARD_NUMBER)
    screen = await FetchEditScreen(regularClient, acctId)
    before = ReadCardRow(syncEngine, SAMPLE_CARD_NUMBER)
    return CardUpdateSession(regularClient, syncEngine, screen, before)


# --- CCLI: list and paging (COCRDLIC) ------------------------------------------------------------
async def test_first_page_lists_seven_masked_cards_in_key_order(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """A fresh entry lists the 7 lowest card numbers on page 1, masked, with no CVV."""
    body = ExpectOk(await ListCards(regularClient, {}))
    assert body["page"] == 1
    assert RowIdentities(body["rows"]) == ExpectedIdentities(
        ReadCardOrder(syncEngine)[:PAGE_SIZE_ROWS]
    )
    assert all(row.keys() == LIST_ROW_KEYS for row in body["rows"])
    assert body["message"] == ""
    assert body["info_message"] == RECORD_ACTIONS_INFO
    assert body["first_key"]
    assert body["last_key"]


async def test_forward_paging_lists_every_card_once(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """F8 from each page walks all 50 cards in key order: 7 pages of 7, then 1 card."""
    pages = await FollowPagesForward(regularClient)
    listed = [identity for page in pages for identity in RowIdentities(page["rows"])]
    assert [len(page["rows"]) for page in pages] == EXPECTED_PAGE_SIZES
    assert [page["page"] for page in pages] == list(range(1, len(EXPECTED_PAGE_SIZES) + 1))
    assert listed == ExpectedIdentities(ReadCardOrder(syncEngine))
    assert [page["message"] for page in pages[:-1]] == [""] * (len(pages) - 1)
    assert pages[-1]["message"] == NO_MORE_RECORDS_MESSAGE


async def test_forward_past_the_last_page_reports_no_more_pages(
    regularClient: httpx.AsyncClient,
) -> None:
    """F8 once the last page was shown keeps it and gives ``NO MORE PAGES TO DISPLAY``."""
    lastPage = (await FollowPagesForward(regularClient))[-1]
    body = ExpectOk(await ListCards(regularClient, PageQuery(lastPage, FORWARD)))
    assert body["message"] == NO_MORE_PAGES_MESSAGE
    assert body["info_message"] == ""
    assert body["page"] == lastPage["page"]
    assert RowIdentities(body["rows"]) == RowIdentities(lastPage["rows"])


async def test_backward_on_the_first_page_reports_no_previous_pages(
    regularClient: httpx.AsyncClient,
) -> None:
    """F7 on page 1 re-reads page 1 and gives ``NO PREVIOUS PAGES TO DISPLAY``."""
    firstPage = ExpectOk(await ListCards(regularClient, {}))
    body = ExpectOk(await ListCards(regularClient, PageQuery(firstPage, BACKWARD)))
    assert body["message"] == NO_PREVIOUS_PAGES_MESSAGE
    assert body["info_message"] == ""
    assert body["page"] == 1
    assert RowIdentities(body["rows"]) == RowIdentities(firstPage["rows"])


async def test_backward_from_the_third_page_shows_the_second(
    regularClient: httpx.AsyncClient,
) -> None:
    """F7 on page 3 reads the 7 cards before its first card: the page-2 rows."""
    firstPage = ExpectOk(await ListCards(regularClient, {}))
    secondPage = ExpectOk(await ListCards(regularClient, PageQuery(firstPage, FORWARD)))
    thirdPage = ExpectOk(await ListCards(regularClient, PageQuery(secondPage, FORWARD)))
    body = ExpectOk(await ListCards(regularClient, PageQuery(thirdPage, BACKWARD)))
    assert thirdPage["page"] == 3
    assert body["page"] == 2
    assert body["message"] == ""
    assert RowIdentities(body["rows"]) == RowIdentities(secondPage["rows"])


async def test_account_filter_lists_only_its_card(regularClient: httpx.AsyncClient) -> None:
    """An 11-digit account filter lists that account's one card, then ENDFILE."""
    body = ExpectOk(await ListCards(regularClient, {"acctId": SAMPLE_ACCOUNT_ID}))
    assert [row["acct_id"] for row in body["rows"]] == [SAMPLE_ACCOUNT_ID]
    assert body["rows"][0]["card_num_masked"] == MASKED_SAMPLE_CARD
    assert body["message"] == NO_MORE_RECORDS_MESSAGE


async def test_account_filter_without_cards_reports_no_records(
    regularClient: httpx.AsyncClient,
) -> None:
    """No card on page 1 is a 200 page with ``NO RECORDS FOUND FOR THIS SEARCH CONDITION.``."""
    body = ExpectOk(await ListCards(regularClient, {"acctId": UNKNOWN_ACCOUNT_ID}))
    assert body["rows"] == []
    assert body["message"] == NO_RECORDS_FOUND_MESSAGE
    assert body["info_message"] == ""


@pytest.mark.parametrize("filterCase", FILTER_CASES)
async def test_invalid_filters_are_refused_in_all_field_mode(
    regularClient: httpx.AsyncClient, filterCase: FilterCase
) -> None:
    """Both filter edits run; the first literal is kept, every failing filter is marked."""
    response = await ListCards(regularClient, filterCase.query)
    body = AssertLegacyError(
        response, HTTP_EDIT_FAILURE, filterCase.message, filterCase.fieldErrors[0]
    )
    assert body["field_errors"] == filterCase.fieldErrors
    assert body["info_message"] == ""
    assert "rows" not in body


async def test_list_enter_with_two_selections_redisplays_the_page(
    regularClient: httpx.AsyncClient,
) -> None:
    """ENTER with two S codes on GET /cards is a 400 carrying the rows, page keys and page."""
    firstPage = ExpectOk(await ListCards(regularClient, {}))
    enterQuery = {"startKey": firstPage["first_key"], "page": 1, "selections": ["S", "S"]}
    response = await ListCards(regularClient, enterQuery)
    body = AssertLegacyError(
        response, HTTP_EDIT_FAILURE, MORE_THAN_ONE_MESSAGE, "rows[1].selection"
    )
    assert body["field_errors"] == ["rows[0].selection", "rows[1].selection"]
    assert body["page"] == 1
    assert RowIdentities(body["rows"]) == RowIdentities(firstPage["rows"])
    assert body["first_key"]
    assert body["last_key"]


# --- CCLI selection and card_ref navigation (COCRDLIC XCTL, AAP 0.7.1) ---------------------------
@pytest.mark.parametrize("declaration", CARD_KEY_DECLARATIONS)
async def test_view_selection_opens_the_card_detail(
    regularClient: httpx.AsyncClient, syncEngine: Engine, declaration: dict[str, str]
) -> None:
    """``S`` returns action view and a card_ref that CCDL reads with the row's account."""
    listBody = ExpectOk(await ListCards(regularClient, {}))
    selected = ExpectOk(await SelectRow(regularClient, listBody, VIEW_ROW_INDEX, "S"))
    assert selected["action"] == "view"
    row = listBody["rows"][VIEW_ROW_INDEX]
    query = {"acctId": row["acct_id"]} | declaration
    response = await GetCard(regularClient, CardUrl(selected["card_ref"]), query)
    cardNum = ReadCardOrder(syncEngine)[VIEW_ROW_INDEX]["card_num"]
    AssertDetailShowsCard(ExpectOk(response), ReadCardRow(syncEngine, cardNum))


@pytest.mark.parametrize("declaration", CARD_KEY_DECLARATIONS)
async def test_update_selection_opens_the_card_update(
    regularClient: httpx.AsyncClient, syncEngine: Engine, declaration: dict[str, str]
) -> None:
    """``U`` returns action update and a card_ref that CCUP fetches into SHOW_DETAILS."""
    listBody = ExpectOk(await ListCards(regularClient, {}))
    selected = ExpectOk(await SelectRow(regularClient, listBody, UPDATE_ROW_INDEX, "U"))
    assert selected["action"] == "update"
    row = listBody["rows"][UPDATE_ROW_INDEX]
    editPath = CardUrl(selected["card_ref"]) + EDIT_SUFFIX
    query = {"acctId": row["acct_id"]} | declaration
    screen = ExpectOk(await GetCard(regularClient, editPath, query))
    cardNum = ReadCardOrder(syncEngine)[UPDATE_ROW_INDEX]["card_num"]
    AssertScreenState(screen, SHOW_DETAILS)
    assert screen["fields"][ACCT_ID_FIELD] == row["acct_id"]
    assert screen["fields"][NAME_FIELD] == ReadCardRow(syncEngine, cardNum)["embossed_name"].upper()
    assert screen["screen_token"]


@pytest.mark.parametrize("selectionCase", SELECTION_ERROR_CASES)
async def test_selection_errors_redisplay_the_list(
    regularClient: httpx.AsyncClient, selectionCase: SelectionCase
) -> None:
    """More than one S or U, or any other code, is a 400 carrying the first page and keys."""
    listBody = ExpectOk(await ListCards(regularClient, {}))
    response = await PostSelection(regularClient, listBody, selectionCase.codesByRow)
    body = AssertLegacyError(
        response, HTTP_EDIT_FAILURE, selectionCase.message, selectionCase.cursorField
    )
    assert body["field_errors"] == selectionCase.fieldErrors
    assert RowIdentities(body["rows"]) == RowIdentities(listBody["rows"])
    assert body["first_key"]
    assert body["last_key"]


async def test_no_selection_transfers_nowhere(regularClient: httpx.AsyncClient) -> None:
    """ENTER with every row blank returns no action and no card_ref."""
    listBody = ExpectOk(await ListCards(regularClient, {}))
    body = ExpectOk(await PostSelection(regularClient, listBody, {}))
    assert body["action"] is None
    assert body["card_ref"] == ""


async def test_typed_card_number_is_read_and_shown_masked(
    regularClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """A typed 16-digit card number in the path passes the key edits and comes back masked."""
    response = await GetCard(regularClient, SAMPLE_CARD_URL, SAMPLE_KEY_QUERY)
    AssertDetailShowsCard(ExpectOk(response), ReadCardRow(syncEngine, SAMPLE_CARD_NUMBER))


@pytest.mark.parametrize("refCase", CARD_REF_CASES)
async def test_forged_or_expired_card_ref_is_not_found(
    regularClient: httpx.AsyncClient,
    testApp: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    refCase: CardRefCase,
) -> None:
    """A forged card_ref, or a real one past its lifetime, gives the CARDDAT NOTFND literal."""
    listBody = ExpectOk(await ListCards(regularClient, SAMPLE_KEY_QUERY))
    cardRef = listBody["rows"][0]["card_ref"]
    if refCase.expire:
        ExpireFernetTokens(monkeypatch, testApp.state.settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    else:
        cardRef = secrets.token_urlsafe(FORGED_CARD_REF_BYTES)
    path = CardUrl(cardRef) + refCase.routeSuffix
    response = await GetCard(regularClient, path, SAMPLE_KEY_QUERY)
    body = AssertLegacyError(response, HTTP_NOT_FOUND, CARD_NOT_FOUND_MESSAGE, ACCT_ID_FIELD)
    assert body["info_message"] == PROMPT_FOR_KEYS_INFO


@pytest.mark.parametrize("routeSuffix", CARD_KEY_ROUTE_SUFFIXES)
@pytest.mark.parametrize("sourceCase", CARD_KEY_SOURCE_CASES)
async def test_declared_source_decides_card_ref_or_typed_card_number(
    regularClient: httpx.AsyncClient, sourceCase: CardKeySourceCase, routeSuffix: str
) -> None:
    """A short path declared a card_ref is decoded as one; undeclared, it is CARDSID text.

    A declared value that is no token, whatever its characters, gives the CARDDAT NOTFND literal
    with both keys failing (AAP 0.7.1), never the typed card number's filter literal or a 422.
    The same text typed is edited, or, outside code page 037, refused with the 422 of AAP 0.6.4.
    """
    path = CardUrl(sourceCase.cardKey) + routeSuffix
    response = await GetCard(regularClient, path, sourceCase.query)
    body = AssertLegacyError(
        response, sourceCase.status, sourceCase.message, sourceCase.fieldErrors[0]
    )
    assert body["field_errors"] == sourceCase.fieldErrors
    assert body["info_message"] == sourceCase.infoMessage
    assert body.get("detail") == sourceCase.detail
    if routeSuffix and sourceCase.status == HTTP_NOT_FOUND:
        assert body["fields"][CARD_NUM_FIELD] == ""


@pytest.mark.parametrize("keyCase", KEY_EDIT_CASES)
async def test_typed_keys_are_edited_before_the_read(
    regularClient: httpx.AsyncClient, keyCase: KeyEditCase
) -> None:
    """CCDL edits both typed keys, account first, and reads nothing when either fails."""
    response = await GetCard(regularClient, CardUrl(keyCase.cardKey), keyCase.query)
    body = AssertLegacyError(response, HTTP_EDIT_FAILURE, keyCase.message, keyCase.fieldErrors[0])
    assert body["field_errors"] == keyCase.fieldErrors
    assert body["info_message"] == PROMPT_FOR_KEYS_INFO


# --- CCUP: the action state machine (COCRDUPC) ---------------------------------------------------
async def test_fetch_shows_the_card_and_issues_a_screen_token(
    cardUpdate: CardUpdateSession,
) -> None:
    """The entry from the list reads the card into SHOW_DETAILS, name upper-cased, no day."""
    screen = cardUpdate.screen
    AssertScreenState(screen, SHOW_DETAILS)
    assert screen["fields"] == EDITABLE_SCREEN_FIELDS
    assert screen["message"] == ""
    assert screen["cursor_field"] == NAME_FIELD
    assert screen["screen_token"]
    assert screen["card_ref"]


async def test_enter_without_change_reports_no_change(cardUpdate: CardUpdateSession) -> None:
    """ENTER with the fetched values gives the no-change literal and keeps SHOW_DETAILS."""
    response = await cardUpdate.Put(cardUpdate.screen, ENTER, {})
    body = AssertLegacyError(response, HTTP_EDIT_FAILURE, NO_CHANGES_MESSAGE, NAME_FIELD)
    assert body["field_errors"] == []
    AssertScreenState(body, SHOW_DETAILS)
    assert body["screen_token"]


async def test_enter_runs_every_detail_edit(cardUpdate: CardUpdateSession) -> None:
    """Every detail edit runs: the name literal is kept and both failing fields are marked."""
    response = await cardUpdate.Put(cardUpdate.screen, ENTER, INVALID_DETAIL_CHANGES)
    body = AssertLegacyError(response, HTTP_EDIT_FAILURE, CARD_NAME_ALPHA_MESSAGE, NAME_FIELD)
    assert body["field_errors"] == [NAME_FIELD, STATUS_FIELD]
    AssertScreenState(body, CHANGES_NOT_OK)
    assert cardUpdate.ReadCard() == cardUpdate.before


@pytest.mark.parametrize("editCase", DETAIL_EDIT_CASES)
async def test_detail_edit_outcomes(
    cardUpdate: CardUpdateSession, editCase: DetailEditCase
) -> None:
    """Month 1-12, year 1950-2099 and a supplied name pass; ENTER never writes."""
    response = await cardUpdate.Put(cardUpdate.screen, ENTER, editCase.changes)
    assert response.status_code == editCase.status, response.text
    body = response.json()
    assert body["message"] == editCase.message
    AssertScreenState(body, editCase.state)
    assert cardUpdate.ReadCard() == cardUpdate.before


async def test_valid_change_awaits_confirmation_without_writing(
    cardUpdate: CardUpdateSession,
) -> None:
    """Valid changes give CHANGES_OK_NOT_CONFIRMED with the new values; nothing is written."""
    confirm = await cardUpdate.Validate(RENAME_CHANGES)
    assert confirm["fields"][NAME_FIELD] == RENAMED_NAME
    assert confirm["message"] == ""
    assert cardUpdate.ReadCard() == cardUpdate.before


async def test_save_writes_the_validated_values_of_the_token(
    cardUpdate: CardUpdateSession,
) -> None:
    """F5 rewrites the token's values, whatever fields it carries; CVV and account are kept."""
    confirm = await cardUpdate.Validate(SAVE_CHANGES)
    body = ExpectOk(await cardUpdate.Put(confirm, SAVE, IGNORED_SAVE_FIELDS))
    AssertScreenState(body, CHANGES_DONE)
    assert body["message"] == ""
    assert body["fields"][NAME_FIELD] == SAVED_NAME
    assert cardUpdate.ReadCard() == cardUpdate.before | SAVED_COLUMNS


async def test_save_of_a_date_that_cannot_exist_fails_the_rewrite(
    cardUpdate: CardUpdateSession,
) -> None:
    """February with the before-image day 31 fails the REWRITE; the next key starts afresh."""
    confirm = await cardUpdate.Validate(IMPOSSIBLE_DATE_CHANGES)
    response = await cardUpdate.Put(confirm, SAVE, {})
    failed = AssertLegacyError(response, HTTP_FILE_ERROR, UPDATE_FAILED_MESSAGE, ACCT_ID_FIELD)
    AssertScreenState(failed, CHANGES_FAILED)
    assert cardUpdate.ReadCard() == cardUpdate.before
    reset = ExpectOk(await cardUpdate.Put(failed, ENTER, {}))
    AssertScreenState(reset, DETAILS_NOT_FETCHED)


async def test_save_before_validation_only_validates(cardUpdate: CardUpdateSession) -> None:
    """F5 in SHOW_DETAILS is processed as ENTER: the changes are validated, not written."""
    response = await cardUpdate.Put(cardUpdate.screen, SAVE, RENAME_CHANGES)
    AssertScreenState(ExpectOk(response), CHANGES_OK_NOT_CONFIRMED)
    assert cardUpdate.ReadCard() == cardUpdate.before


async def test_enter_after_validation_runs_no_edit(cardUpdate: CardUpdateSession) -> None:
    """ENTER in CHANGES_OK_NOT_CONFIRMED ignores the fields and keeps the validated values."""
    confirm = await cardUpdate.Validate(RENAME_CHANGES)
    body = ExpectOk(await cardUpdate.Put(confirm, ENTER, INVALID_DETAIL_CHANGES))
    AssertScreenState(body, CHANGES_OK_NOT_CONFIRMED)
    assert body["fields"] == confirm["fields"]
    assert cardUpdate.ReadCard() == cardUpdate.before


@pytest.mark.parametrize("validateFirst", [False, True], ids=["show-details", "not-confirmed"])
async def test_cancel_rereads_the_card(cardUpdate: CardUpdateSession, validateFirst: bool) -> None:
    """F12 discards the keyed or validated changes and shows the committed card again."""
    screen = cardUpdate.screen
    if validateFirst:
        screen = await cardUpdate.Validate(RENAME_CHANGES)
    body = ExpectOk(await cardUpdate.Put(screen, CANCEL, DISCARDED_CHANGES))
    AssertScreenState(body, SHOW_DETAILS)
    assert body["fields"] == EDITABLE_SCREEN_FIELDS
    assert cardUpdate.ReadCard() == cardUpdate.before


@pytest.mark.parametrize("action", [ENTER, SAVE, CANCEL])
async def test_any_key_before_fetch_is_enter(
    regularClient: httpx.AsyncClient, syncEngine: Engine, action: str
) -> None:
    """With no screen token every key edits the typed keys and fetches; details are ignored."""
    acctId = MakeCardEditable(syncEngine, SAMPLE_CARD_NUMBER)
    keyFields = {ACCT_ID_FIELD: acctId, CARD_NUM_FIELD: SAMPLE_CARD_NUMBER} | RENAME_CHANGES
    response = await PutCard(
        regularClient, SAMPLE_CARD_NUMBER, {"action": action, "fields": keyFields}
    )
    body = ExpectOk(response)
    AssertScreenState(body, SHOW_DETAILS)
    assert body["fields"] == EDITABLE_SCREEN_FIELDS


@pytest.mark.parametrize("action", [ENTER, SAVE, CANCEL])
async def test_any_key_after_done_shows_the_empty_key_screen(
    cardUpdate: CardUpdateSession, action: str
) -> None:
    """After CHANGES_DONE the input is discarded and the empty key screen is sent."""
    confirm = await cardUpdate.Validate(RENAME_CHANGES)
    done = ExpectOk(await cardUpdate.Put(confirm, SAVE, {}))
    body = ExpectOk(await cardUpdate.Put(done, action, DISCARDED_CHANGES))
    AssertScreenState(body, DETAILS_NOT_FETCHED)
    assert body["fields"] == EMPTY_SCREEN_FIELDS
    assert body["card_ref"] == ""
    assert cardUpdate.ReadCard()["embossed_name"] == RENAMED_NAME


@pytest.mark.parametrize("expireToken", [False, True], ids=["forged", "expired"])
async def test_invalid_screen_token_restarts_the_flow(
    cardUpdate: CardUpdateSession,
    testApp: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    expireToken: bool,
) -> None:
    """F5 with a forged or expired token is the not-fetched ENTER: blank keys, no write."""
    body = UpdateBody(await cardUpdate.Validate(RENAME_CHANGES), SAVE, {})
    if expireToken:
        ExpireFernetTokens(monkeypatch, testApp.state.settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    else:
        body["screen_token"] = secrets.token_urlsafe(FORGED_SCREEN_TOKEN_BYTES)
    response = await cardUpdate.Send(body)
    restarted = AssertLegacyError(response, HTTP_EDIT_FAILURE, NO_INPUT_MESSAGE, ACCT_ID_FIELD)
    assert restarted["field_errors"] == [ACCT_ID_FIELD, CARD_NUM_FIELD]
    AssertScreenState(restarted, DETAILS_NOT_FETCHED)
    assert cardUpdate.ReadCard() == cardUpdate.before


# --- Roles and the superseded Style B user-type reset (AAP 0.6.12, 0.7.1) ------------------------
@pytest.mark.parametrize("call", CARD_ENDPOINTS)
async def test_admin_is_refused_and_stays_an_admin(
    adminClient: httpx.AsyncClient, call: EndpointCall
) -> None:
    """Every card endpoint gives an admin 403 with an empty message; type A and its menu stay.

    The request carries the allow-listed Origin, so the refusal is the regular-role guard's.
    """
    response = await adminClient.send(BuildRequest(adminClient, call))
    AssertLegacyError(response, HTTP_ACCESS_REFUSED, "")
    menuResponse = await adminClient.get(ADMIN_MENU_URL)
    assert menuResponse.status_code == HTTP_OK, menuResponse.text
    currentUser = ExpectOk(await adminClient.get(CURRENT_USER_URL))
    assert currentUser["user_type"] == ADMIN_USER_TYPE


async def test_admin_save_of_a_validated_screen_writes_nothing(
    cardUpdate: CardUpdateSession, adminClient: httpx.AsyncClient
) -> None:
    """An admin's F5 on a regular user's validated screen is refused and the card is unchanged.

    The same request then saves for the regular user, so the refused request was a valid save.
    """
    saveBody = UpdateBody(await cardUpdate.Validate(RENAME_CHANGES), SAVE, {})
    refused = await PutCard(adminClient, cardUpdate.screen["card_ref"], saveBody)
    AssertLegacyError(refused, HTTP_ACCESS_REFUSED, "")
    assert cardUpdate.ReadCard() == cardUpdate.before
    saved = ExpectOk(await cardUpdate.Send(saveBody))
    AssertScreenState(saved, CHANGES_DONE)
    assert cardUpdate.ReadCard() == cardUpdate.before | {"embossed_name": RENAMED_NAME}


@pytest.mark.parametrize("userType", OTHER_USER_TYPES)
async def test_every_other_user_type_lists_cards_as_itself(
    loginAs: Callable[[str], Awaitable[Any]], userType: str
) -> None:
    """Any stored type but exactly A is regular, and the session keeps the stored type."""
    signedOn = await loginAs(userType)
    body = ExpectOk(await ListCards(signedOn.client, {}))
    assert len(body["rows"]) == PAGE_SIZE_ROWS
    currentUser = ExpectOk(await signedOn.client.get(CURRENT_USER_URL))
    assert currentUser["user_type"] == userType


@pytest.mark.parametrize("call", CARD_ENDPOINTS)
async def test_anonymous_requests_are_refused(
    apiClient: httpx.AsyncClient, call: EndpointCall
) -> None:
    """Every card endpoint answers 401 with an empty message without a session cookie."""
    response = await apiClient.send(BuildRequest(apiClient, call))
    AssertLegacyError(response, HTTP_SIGN_ON_FAILURE, "")


@pytest.mark.parametrize("origin", [FOREIGN_ORIGIN, None], ids=["foreign-origin", "no-origin"])
@pytest.mark.parametrize("call", WRITE_ENDPOINTS)
async def test_writes_without_an_allowed_origin_are_refused(
    regularClient: httpx.AsyncClient, call: EndpointCall, origin: str | None
) -> None:
    """A cookie-authenticated POST or PUT needs an allow-listed Origin (OriginGuard)."""
    request = BuildRequest(regularClient, call)
    del request.headers["Origin"]
    if origin is not None:
        request.headers["Origin"] = origin
    response = await regularClient.send(request)
    AssertLegacyError(response, HTTP_ACCESS_REFUSED, "")
