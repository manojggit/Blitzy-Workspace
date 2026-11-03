"""Integration tests for raw screen keys in the paths of CAVW, CAUP, CCDL, CCUP, CT01, CU02 and
CU03 (COACTVWC, COACTUPC, COCRDSLC, COCRDUPC, COTRN01C, COUSR02C and COUSR03C).

A 3270 key field may hold any code-page-037 printable character, "/" included, and COUSR01C
adds users whose IDs are "A/B", "/", "." or "..". The frontend sends such a key as one path
segment (lib/apiClient.ts EncodePathKey): "/" as %2F, "%" as %25, and a blank, "." or ".." key
with one trailing space, which the programs' space-padded key compares ignore. The cases assert:

- a key holding "/" reaches the program's key edit or keyed read, with its literal, status and
    at least the legacy error keys and no ``detail``, instead of FastAPI's 404
    ``{"detail": "Not Found"}``;
- ``/accounts/A%2Fedit`` is the CAVW view of key ``A/edit``, never the CAUP fetch of ``A``;
- a ``card_ref`` longer than CARDSID holding a character outside code page 037 is
    ``Did not find cards for this search condition`` on the GET routes and ignored by the CCUP
    PUT, while CARDSID-length text outside the repertoire and an over-long value stay 422;
- an existing TRAN-ID holding "/" is shown, and CU01-added IDs with "/" or dots are fetched,
    updated and deleted;
- ordinary keys, ``card_ref`` tokens, the selections and the collections answer as before.

Clients, generated sign-on users and the seed restore are the fixtures of tests/conftest.py.
"""

import secrets
from decimal import Decimal
from typing import NamedTuple

import httpx
import pytest
from sqlalchemy import Engine, insert, select

from app.models import Card, Transaction
from app.schemas.common import CARD_REF_MAX_LENGTH

API_V1 = "/api/v1"
USERS_PATH = API_V1 + "/admin/users"
HTTP_OK = 200
HTTP_EDIT_FAILURE = 400
HTTP_NOT_FOUND = 404
HTTP_MALFORMED = 422

# The error body of every legacy outcome (schemas.common.LegacyErrorResponse); the CAUP and
# CCUP bodies also carry their screen (schemas.accounts.AccountScreen, schemas.cards.CardScreen),
# both of which declare fields, info_message, state and screen_token.
LEGACY_ERROR_KEYS = frozenset({"message", "info_message", "field_errors", "cursor_field"})
ACCOUNT_SCREEN_ERROR_KEYS = LEGACY_ERROR_KEYS | {"fields", "state", "screen_token"}
CARD_SCREEN_ERROR_KEYS = ACCOUNT_SCREEN_ERROR_KEYS

# A seeded account (data/EBCDIC/AWS.M2.CARDDEMO.ACCTDATA.PS) with exactly one card.
SEED_ACCOUNT_ID = "00000000004"
ACCOUNT_QUERY = "?acctId=" + SEED_ACCOUNT_ID
ENTER_BODY = {"action": "enter"}

# cbl/COACTVWC.cbl line 672 (two spaces after "must") and cbl/COACTUPC.cbl lines 1807-1808.
CAVW_ACCOUNT_KEY_MESSAGE = "Account Filter must  be a non-zero 11 digit number"
CAUP_ACCOUNT_KEY_MESSAGE = "Account Number if supplied must be a 11 digit Non-Zero Number"
# cbl/COCRDSLC.cbl lines 711 and 154; cbl/COCRDUPC.cbl line 186, ENTER on an empty screen.
CARD_KEY_MESSAGE = "CARD ID FILTER,IF SUPPLIED MUST BE A 16 DIGIT NUMBER"
CARD_NOT_FOUND_MESSAGE = "Did not find cards for this search condition"
NO_INPUT_MESSAGE = "No input received"
# The NOTFND literals of cbl/COTRN01C.cbl line 285 and cbl/COUSR02C.cbl line 342.
TRAN_NOT_FOUND_MESSAGE = "Transaction ID NOT found..."
USER_NOT_FOUND_MESSAGE = "User ID NOT found..."

# An 11-character ACCTSID and a 16-character CARDSID, each holding "/".
SLASH_ACCOUNT_SEGMENT = "00000%2F00001"
SLASH_CARD_SEGMENT = "12345678%2F1234567"
# Longer than CARDSID, so a card_ref, and holding U+0100, which code page 037 lacks.
MALFORMED_CARD_REF_SEGMENT = "x" * 20 + "%C4%80"
# CARDSID-length screen text holding U+0100, and a value beyond the card_ref bound.
NON_CP037_CARD_SEGMENT = "xx%C4%80"
OVERLONG_CARD_SEGMENT = "x" * (CARD_REF_MAX_LENGTH + 1)

# A 16-character TRAN-ID X(16) holding "/", inserted on a seeded card.
SLASH_TRAN_ID = "TRAN/WITH/SLASH1"
SLASH_TRAN_SEGMENT = "TRAN%2FWITH%2FSLASH1"
TRAN_TYPE_CD = "01"
TRAN_CAT_CD = 1
TRAN_AMOUNT = Decimal("1.00")
MERCHANT_ID = "000000001"

NEW_USER_TYPE = "U"
NEW_FIRST_NAME = "PATH"
CHANGED_FIRST_NAME = "MOVED"
NEW_LAST_NAME = "KEY"
FETCH_SCREENS = ("update", "delete")


class KeyOutcome(NamedTuple):
    """One request carrying a raw key, and the legacy outcome its program answers."""

    method: str
    path: str
    status: int
    message: str
    bodyKeys: frozenset[str]
    body: dict[str, str] | None = None


REGULAR_KEY_OUTCOMES = [
    pytest.param(
        KeyOutcome(
            "GET",
            f"/accounts/{SLASH_ACCOUNT_SEGMENT}",
            HTTP_EDIT_FAILURE,
            CAVW_ACCOUNT_KEY_MESSAGE,
            LEGACY_ERROR_KEYS,
        ),
        id="cavw-slash",
    ),
    pytest.param(
        KeyOutcome(
            "GET",
            f"/accounts/{SLASH_ACCOUNT_SEGMENT}/edit",
            HTTP_EDIT_FAILURE,
            CAUP_ACCOUNT_KEY_MESSAGE,
            ACCOUNT_SCREEN_ERROR_KEYS,
        ),
        id="caup-fetch-slash",
    ),
    pytest.param(
        KeyOutcome(
            "PUT",
            f"/accounts/{SLASH_ACCOUNT_SEGMENT}",
            HTTP_EDIT_FAILURE,
            CAUP_ACCOUNT_KEY_MESSAGE,
            ACCOUNT_SCREEN_ERROR_KEYS,
            ENTER_BODY,
        ),
        id="caup-enter-slash",
    ),
    pytest.param(
        KeyOutcome(
            "GET",
            "/accounts/A%2Fedit",
            HTTP_EDIT_FAILURE,
            CAVW_ACCOUNT_KEY_MESSAGE,
            LEGACY_ERROR_KEYS,
        ),
        id="cavw-key-ending-in-edit",
    ),
    pytest.param(
        KeyOutcome(
            "GET",
            f"/cards/{SLASH_CARD_SEGMENT}{ACCOUNT_QUERY}",
            HTTP_EDIT_FAILURE,
            CARD_KEY_MESSAGE,
            LEGACY_ERROR_KEYS,
        ),
        id="ccdl-slash",
    ),
    pytest.param(
        KeyOutcome(
            "GET",
            f"/cards/{SLASH_CARD_SEGMENT}/edit{ACCOUNT_QUERY}",
            HTTP_EDIT_FAILURE,
            CARD_KEY_MESSAGE,
            CARD_SCREEN_ERROR_KEYS,
        ),
        id="ccup-fetch-slash",
    ),
    pytest.param(
        KeyOutcome(
            "PUT",
            "/cards/A%2FB",
            HTTP_EDIT_FAILURE,
            NO_INPUT_MESSAGE,
            CARD_SCREEN_ERROR_KEYS,
            ENTER_BODY,
        ),
        id="ccup-enter-slash",
    ),
    pytest.param(
        KeyOutcome(
            "GET",
            f"/cards/{MALFORMED_CARD_REF_SEGMENT}{ACCOUNT_QUERY}",
            HTTP_NOT_FOUND,
            CARD_NOT_FOUND_MESSAGE,
            LEGACY_ERROR_KEYS,
        ),
        id="ccdl-malformed-card-ref",
    ),
    pytest.param(
        KeyOutcome(
            "GET",
            f"/cards/{MALFORMED_CARD_REF_SEGMENT}/edit{ACCOUNT_QUERY}",
            HTTP_NOT_FOUND,
            CARD_NOT_FOUND_MESSAGE,
            LEGACY_ERROR_KEYS,
        ),
        id="ccup-malformed-card-ref",
    ),
    pytest.param(
        KeyOutcome(
            "PUT",
            f"/cards/{MALFORMED_CARD_REF_SEGMENT}",
            HTTP_EDIT_FAILURE,
            NO_INPUT_MESSAGE,
            CARD_SCREEN_ERROR_KEYS,
            ENTER_BODY,
        ),
        id="ccup-enter-malformed-card-ref-path-ignored",
    ),
    *(
        pytest.param(
            KeyOutcome(
                "GET",
                f"/transactions/{segment}",
                HTTP_NOT_FOUND,
                TRAN_NOT_FOUND_MESSAGE,
                LEGACY_ERROR_KEYS,
            ),
            id=f"ct01-absent-{label}",
        )
        for segment, label in (
            ("A%2FB", "slash-inside"),
            ("%2F", "slash-only"),
            ("%25", "percent"),
            ("%2E%2E", "raw-encoded-dots"),
            (".%20", "padded-dot"),
            ("..%20", "padded-dots"),
        )
    ),
]

MALFORMED_CARD_KEYS = [
    pytest.param(NON_CP037_CARD_SEGMENT, id="cardsid-outside-cp037"),
    pytest.param(OVERLONG_CARD_SEGMENT, id="beyond-card-ref-bound"),
]
CARD_ROUTE_SUFFIXES = [pytest.param("", id="ccdl"), pytest.param("/edit", id="ccup-fetch")]

USER_KEYS = [
    pytest.param("A/B", "A%2FB", id="slash-inside"),
    pytest.param("/", "%2F", id="slash-only"),
    pytest.param(".", ".%20", id="dot"),
    pytest.param("..", "..%20", id="dot-dot"),
]

# {cardNum} is the seeded PAN of SEED_ACCOUNT_ID and {cardRef} its CCLI card_ref token.
ORDINARY_KEY_PATHS = [
    pytest.param(f"/accounts/{SEED_ACCOUNT_ID}", id="cavw"),
    pytest.param(f"/accounts/{SEED_ACCOUNT_ID}/edit", id="caup-fetch"),
    pytest.param("/cards/{cardNum}" + ACCOUNT_QUERY, id="ccdl-pan"),
    pytest.param("/cards/{cardNum}/edit" + ACCOUNT_QUERY, id="ccup-fetch-pan"),
    pytest.param("/cards/{cardRef}" + ACCOUNT_QUERY, id="ccdl-card-ref"),
    pytest.param("/cards/{cardRef}/edit" + ACCOUNT_QUERY, id="ccup-fetch-card-ref"),
]

REGULAR_COLLECTION_REQUESTS = [
    pytest.param("GET", "/cards", None, id="ccli-list"),
    pytest.param("POST", "/cards/selection", {"rows": []}, id="ccli-selection"),
    pytest.param("GET", "/transactions", None, id="ct00-list"),
    pytest.param("POST", "/transactions/selection", {"selections": []}, id="ct00-selection"),
]
ADMIN_COLLECTION_REQUESTS = [
    pytest.param("GET", "/admin/users", None, id="cu00-list"),
    pytest.param("POST", "/admin/users/selection", {"selections": []}, id="cu00-selection"),
]


def ReadSeedCardNumber(syncEngine: Engine) -> str:
    """Return the card number of SEED_ACCOUNT_ID's only card."""
    with syncEngine.connect() as connection:
        statement = select(Card.card_num).where(Card.acct_id == SEED_ACCOUNT_ID)
        return connection.execute(statement).scalar_one()


def InsertTransaction(syncEngine: Engine, tranId: str) -> None:
    """Commit a posted transaction keyed ``tranId`` on SEED_ACCOUNT_ID's card."""
    transactionRow = {
        "tran_id": tranId,
        "tran_type_cd": TRAN_TYPE_CD,
        "tran_cat_cd": TRAN_CAT_CD,
        "tran_amt": TRAN_AMOUNT,
        "merchant_id": MERCHANT_ID,
        "card_num": ReadSeedCardNumber(syncEngine),
    }
    with syncEngine.begin() as connection:
        connection.execute(insert(Transaction.__table__).values(transactionRow))


def NewUserFields(userId: str) -> dict[str, str]:
    """Return a complete COUSR01 add screen for ``userId`` with a generated password."""
    return {
        "first_name": NEW_FIRST_NAME,
        "last_name": NEW_LAST_NAME,
        "user_id": userId,
        "password": secrets.token_hex(4).upper(),
        "user_type": NEW_USER_TYPE,
    }


def ChangedUserFields() -> dict[str, str]:
    """Return a COUSR02 update screen changing the first name; a blank password keeps it."""
    return {
        "first_name": CHANGED_FIRST_NAME,
        "last_name": NEW_LAST_NAME,
        "password": "",
        "user_type": NEW_USER_TYPE,
    }


async def ExpectUserFetched(adminClient: httpx.AsyncClient, userPath: str, userId: str) -> None:
    """Assert that the COUSR02 and COUSR03 ENTER fetches both return the user ``userId``."""
    for screen in FETCH_SCREENS:
        response = await adminClient.get(userPath, params={"screen": screen})
        assert response.status_code == HTTP_OK, response.text
        assert response.json()["user_id"] == userId


async def ExpectUserChangedAndDeleted(
    adminClient: httpx.AsyncClient, userPath: str, userId: str
) -> None:
    """Assert the COUSR02C update and COUSR03C delete literals, then that the user is gone."""
    updated = await adminClient.put(userPath, json=ChangedUserFields())
    assert updated.status_code == HTTP_OK, updated.text
    assert updated.json()["message"] == f"User {userId} has been updated ..."
    deleted = await adminClient.delete(userPath)
    assert deleted.status_code == HTTP_OK, deleted.text
    assert deleted.json()["message"] == f"User {userId} has been deleted ..."
    missing = await adminClient.get(userPath, params={"screen": FETCH_SCREENS[0]})
    assert missing.status_code == HTTP_NOT_FOUND, missing.text
    assert missing.json()["message"] == USER_NOT_FOUND_MESSAGE


@pytest.mark.parametrize("outcome", REGULAR_KEY_OUTCOMES)
async def test_raw_key_reaches_its_program(
    regularClient: httpx.AsyncClient, outcome: KeyOutcome
) -> None:
    """A raw key gets its program's literal and legacy body, never a framework 404."""
    response = await regularClient.request(outcome.method, API_V1 + outcome.path, json=outcome.body)
    body = response.json()
    assert response.status_code == outcome.status, response.text
    assert body["message"] == outcome.message
    assert outcome.bodyKeys <= set(body)
    assert "detail" not in body


@pytest.mark.parametrize("suffix", CARD_ROUTE_SUFFIXES)
@pytest.mark.parametrize("segment", MALFORMED_CARD_KEYS)
async def test_card_key_no_terminal_could_send_is_malformed(
    regularClient: httpx.AsyncClient, segment: str, suffix: str
) -> None:
    """CARDSID-length text outside code page 037, or text beyond the card_ref bound, is 422."""
    response = await regularClient.get(f"{API_V1}/cards/{segment}{suffix}{ACCOUNT_QUERY}")
    assert response.status_code == HTTP_MALFORMED, response.text
    assert response.json()["detail"][0]["loc"] == ["path", "cardNum"]


async def test_existing_transaction_with_slash_is_shown(
    regularClient: httpx.AsyncClient, syncEngine: Engine, restoreSeedState: None
) -> None:
    """COTRN01C reads and shows a TRAN-ID holding "/" as keyed."""
    InsertTransaction(syncEngine, SLASH_TRAN_ID)
    response = await regularClient.get(f"{API_V1}/transactions/{SLASH_TRAN_SEGMENT}")
    assert response.status_code == HTTP_OK, response.text
    assert response.json()["tran_id"] == SLASH_TRAN_ID


@pytest.mark.parametrize(("userId", "segment"), USER_KEYS)
async def test_added_user_key_is_fetched_updated_and_deleted(
    adminClient: httpx.AsyncClient, userId: str, segment: str
) -> None:
    """Every ID COUSR01C adds stays reachable by COUSR02C and COUSR03C."""
    userPath = f"{USERS_PATH}/{segment}"
    added = await adminClient.post(USERS_PATH, json=NewUserFields(userId))
    assert added.status_code == HTTP_OK, added.text
    assert added.json()["message"] == f"User {userId} has been added ..."
    await ExpectUserFetched(adminClient, userPath, userId)
    await ExpectUserChangedAndDeleted(adminClient, userPath, userId)


@pytest.mark.parametrize("pathTemplate", ORDINARY_KEY_PATHS)
async def test_ordinary_key_and_card_ref_reach_the_same_route(
    regularClient: httpx.AsyncClient, syncEngine: Engine, pathTemplate: str
) -> None:
    """A seeded account, PAN or card_ref still reaches its view, or its edit fetch with state."""
    listed = await regularClient.get(f"{API_V1}/cards", params={"acctId": SEED_ACCOUNT_ID})
    assert listed.status_code == HTTP_OK, listed.text
    cardRef = listed.json()["rows"][0]["card_ref"]
    path = pathTemplate.format(cardNum=ReadSeedCardNumber(syncEngine), cardRef=cardRef)
    response = await regularClient.get(API_V1 + path)
    assert response.status_code == HTTP_OK, response.text
    assert ("state" in response.json()) == ("/edit" in path)


@pytest.mark.parametrize(("method", "path", "body"), REGULAR_COLLECTION_REQUESTS)
async def test_regular_collection_and_selection_routes_answer(
    regularClient: httpx.AsyncClient, method: str, path: str, body: dict | None
) -> None:
    """The CCLI and CT00 list and selection routes still answer with their screens."""
    response = await regularClient.request(method, API_V1 + path, json=body)
    assert response.status_code == HTTP_OK, response.text
    assert "detail" not in response.json()


@pytest.mark.parametrize(("method", "path", "body"), ADMIN_COLLECTION_REQUESTS)
async def test_admin_collection_and_selection_routes_answer(
    adminClient: httpx.AsyncClient, method: str, path: str, body: dict | None
) -> None:
    """The CU00 list and selection routes still answer with their screens."""
    response = await adminClient.request(method, API_V1 + path, json=body)
    assert response.status_code == HTTP_OK, response.text
    assert "detail" not in response.json()
