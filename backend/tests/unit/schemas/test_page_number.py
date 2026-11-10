"""Unit tests of the page number a list request sends back: a non-screen integer field.

The ``page`` of the CT00 list query (``TransactionListQuery``) and selection body
(``TransactionSelectionRequest``), the CCLI list query (``CardListQuery``) and the CU00 list
query (``UserListQuery``) is the page number the client was shown. A structurally wrong value
gets the standard 422 before any program edit runs (Technical Specification, Sections 0.6.4
and 0.6.5). A lax ``int`` coerced a JSON boolean, numeric text and an integral float (``true``,
``"1"``, ``" 1 "``, ``1.0``), and the out-of-range ``9223372036854775808``, into a page, and
the request was processed. These tests pin that:

- a query page, validated from query text as FastAPI does, accepts only ASCII decimal digits
    from 0 to the largest page its PIC clause holds (CDEMO-CT00-PAGE-NUM and CDEMO-CU00-PAGE-NUM
    9(08): 99999999; WS-CA-SCREEN-NUM 9(1): 9), and an ``int`` such as the omitted default;
- the selection body page accepts only a JSON integer from 0 to 99999999;
- each page field publishes type integer with its minimum and maximum, in the model's JSON
    schema and in the OpenAPI document;
- a live request with a wrong page gets the 422 body whose ``detail`` items hold only loc, msg
    and type, so no input is echoed.

The live requests need no database: FastAPI validates before any handler runs, and the request
session is lazy. SECRET_KEY and the user IDs are generated with the secrets module, sessions
are minted with IssueSessionToken, and backend/.env is never read.
"""

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, NamedTuple

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.database import DisposeEngine
from app.core.security import IssueSessionToken
from app.schemas.cards import CardListQuery
from app.schemas.transactions import TransactionListQuery, TransactionSelectionRequest
from app.schemas.users import UserListQuery

# The largest value of a PIC 9(08) and of a PIC 9(1) page number.
EIGHT_DIGIT_PAGE_MAX = 99999999
ONE_DIGIT_PAGE_MAX = 9
BEYOND_INT64 = 2**63
# Query inputs every query page refuses, whatever its maximum.
REJECTED_QUERY_INPUTS: dict[str, object] = {
    "decimal-point": "1.0",
    "leading-blank": " 1",
    "surrounding-blanks": " 1 ",
    "plus-sign": "+1",
    "minus-sign": "-1",
    "digit-separator": "1_0",
    "empty": "",
    "boolean-text": "true",
    "arabic-indic-digit": "\u0661",
    "full-width-digit": "\uff11",
    "beyond-int64-text": str(BEYOND_INT64),
    "bool": True,
    "float": 1.0,
}
# Body values the selection page refuses: anything but a JSON integer, or one out of range.
REJECTED_BODY_INPUTS: dict[str, object] = {
    "true": True,
    "false": False,
    "numeric-string": "1",
    "padded-numeric-string": " 1 ",
    "integral-float": 1.0,
    "null": None,
    "negative": -1,
    "over-maximum": EIGHT_DIGIT_PAGE_MAX + 1,
    "beyond-int64": BEYOND_INT64,
}

ALLOWED_ORIGIN = "http://127.0.0.1:3000"
API_BASE_URL = "http://127.0.0.1:8000"
# A well-formed URL of a database nobody creates: the engine is lazy and never connects.
UNUSED_DATABASE_URL = "postgresql+{driver}://page_number_test@127.0.0.1/page_number_test"
SECRET_KEY_BYTES = 32
USER_ID_PREFIX = "Q"
USER_ID_HEX_BYTES = 3
ADMIN_USER_TYPE = "A"
REGULAR_USER_TYPE = "U"
HTTP_UNPROCESSABLE = 422
LEGACY_VALIDATION_KEYS = {"detail", "message", "info_message", "field_errors", "cursor_field"}
VALIDATION_DETAIL_KEYS = {"loc", "msg", "type"}
PAGE_FIELD = "page"
BOUND_KEYS = ("type", "minimum", "maximum")
TRANSACTIONS_PATH = "/api/v1/transactions"
SELECTION_PATH = "/api/v1/transactions/selection"
CARDS_PATH = "/api/v1/cards"
ADMIN_USERS_PATH = "/api/v1/admin/users"


class PageField(NamedTuple):
    """A model whose ``page`` is under test, its case id, and the largest page its PIC holds."""

    caseId: str
    model: type[BaseModel]
    maxPage: int


@dataclass(frozen=True)
class PageRequest:
    """A live request carrying a wrong page, the user type it is sent as, and the page's part."""

    method: str
    path: str
    userType: str
    location: str
    content: bytes | None = None


QUERY_PAGE_FIELDS = [
    PageField("ct00-list", TransactionListQuery, EIGHT_DIGIT_PAGE_MAX),
    PageField("ccli-list", CardListQuery, ONE_DIGIT_PAGE_MAX),
    PageField("cu00-list", UserListQuery, EIGHT_DIGIT_PAGE_MAX),
]
BODY_PAGE_FIELD = PageField("ct00-selection", TransactionSelectionRequest, EIGHT_DIGIT_PAGE_MAX)


def AcceptedQueryInputs(maxPage: int) -> dict[str, tuple[object, int]]:
    """Return the (input, page) pairs a query page of maximum ``maxPage`` accepts."""
    return {
        "zero": ("0", 0),
        "one": ("1", 1),
        "leading-zero": ("01", 1),
        "maximum": (str(maxPage), maxPage),
        "int": (maxPage, maxPage),
    }


def AcceptedBodyInputs(maxPage: int) -> dict[str, tuple[object, int]]:
    """Return the (input, page) pairs the selection body page accepts: JSON integers only."""
    return {"zero": (0, 0), "one": (1, 1), "maximum": (maxPage, maxPage)}


def RejectedQueryInputs(maxPage: int) -> dict[str, object]:
    """Return the inputs a query page of maximum ``maxPage`` refuses, its maximum + 1 included."""
    return {**REJECTED_QUERY_INPUTS, "over-maximum": str(maxPage + 1)}


def AcceptedCases(field: PageField, inputs: dict[str, tuple[object, int]]) -> list[Any]:
    """Return one (model, input, page) case of ``field`` per accepted input."""
    return [
        pytest.param(field.model, value, page, id=f"{field.caseId}-{inputId}")
        for inputId, (value, page) in inputs.items()
    ]


def RejectedCases(field: PageField, inputs: dict[str, object]) -> list[Any]:
    """Return one (model, input) case of ``field`` per refused input."""
    return [
        pytest.param(field.model, value, id=f"{field.caseId}-{inputId}")
        for inputId, value in inputs.items()
    ]


def SelectionBody(pageJson: str) -> bytes:
    """Return a CT00 selection body whose ``page`` is the JSON text ``pageJson``."""
    return f'{{"selections": [], "search_tran_id": "", "page": {pageJson}}}'.encode()


def SelectionRequest(caseId: str, pageJson: str) -> Any:
    """Return the case of a regular user's CT00 selection whose body page is ``pageJson``."""
    pageRequest = PageRequest(
        "POST", SELECTION_PATH, REGULAR_USER_TYPE, "body", SelectionBody(pageJson)
    )
    return pytest.param(pageRequest, id=f"ct00-selection-{caseId}")


def QueryRequest(caseId: str, pathAndQuery: str, userType: str) -> Any:
    """Return the case of a GET of ``pathAndQuery`` sent as a user of ``userType``."""
    return pytest.param(PageRequest("GET", pathAndQuery, userType, "query"), id=caseId)


ACCEPTED_PAGES = [
    *[
        case
        for field in QUERY_PAGE_FIELDS
        for case in AcceptedCases(field, AcceptedQueryInputs(field.maxPage))
    ],
    *AcceptedCases(BODY_PAGE_FIELD, AcceptedBodyInputs(BODY_PAGE_FIELD.maxPage)),
]
REJECTED_PAGES = [
    *[
        case
        for field in QUERY_PAGE_FIELDS
        for case in RejectedCases(field, RejectedQueryInputs(field.maxPage))
    ],
    *RejectedCases(BODY_PAGE_FIELD, REJECTED_BODY_INPUTS),
]
PAGE_FIELD_BOUNDS = [
    pytest.param(field.model, field.maxPage, id=field.caseId)
    for field in [*QUERY_PAGE_FIELDS, BODY_PAGE_FIELD]
]
# (operation path, request part, largest page) of each page field in the OpenAPI document.
PUBLISHED_PAGE_FIELDS = [
    pytest.param(TRANSACTIONS_PATH, "query", EIGHT_DIGIT_PAGE_MAX, id="ct00-list"),
    pytest.param(SELECTION_PATH, "body", EIGHT_DIGIT_PAGE_MAX, id="ct00-selection"),
    pytest.param(CARDS_PATH, "query", ONE_DIGIT_PAGE_MAX, id="ccli-list"),
    pytest.param(ADMIN_USERS_PATH, "query", EIGHT_DIGIT_PAGE_MAX, id="cu00-list"),
]
WRONG_PAGE_REQUESTS = [
    SelectionRequest("true", "true"),
    SelectionRequest("padded-numeric-string", '" 1 "'),
    SelectionRequest("integral-float", "1.0"),
    SelectionRequest("beyond-int64", str(BEYOND_INT64)),
    QueryRequest("ct00-list-decimal-point", f"{TRANSACTIONS_PATH}?page=1.0", REGULAR_USER_TYPE),
    QueryRequest("ccli-list-leading-blank", f"{CARDS_PATH}?page=%201", REGULAR_USER_TYPE),
    QueryRequest("ccli-list-over-maximum", f"{CARDS_PATH}?page=10", REGULAR_USER_TYPE),
    QueryRequest("cu00-list-plus-sign", f"{ADMIN_USERS_PATH}?page=%2B1", ADMIN_USER_TYPE),
]


def BuildTestSettings() -> Settings:
    """Return Settings with a generated key, the allow-listed origin and an unused database."""
    return Settings(
        _env_file=None,
        APP_ENV="test",
        SECRET_KEY=secrets.token_urlsafe(SECRET_KEY_BYTES),
        DATABASE_URL=UNUSED_DATABASE_URL.format(driver="asyncpg"),
        SYNC_DATABASE_URL=UNUSED_DATABASE_URL.format(driver="psycopg2"),
        BACKEND_CORS_ORIGINS=ALLOWED_ORIGIN,
    )


def SessionCookieHeader(settings: Settings, userType: str) -> dict[str, str]:
    """Return the Cookie header of a session minted for a generated user ID of ``userType``."""
    userId = USER_ID_PREFIX + secrets.token_hex(USER_ID_HEX_BYTES).upper()
    sessionToken = IssueSessionToken(userId, userType, settings)
    return {"Cookie": f"{settings.SESSION_COOKIE_NAME}={sessionToken}"}


def PublishedPageSchema(document: dict[str, Any], path: str, location: str) -> dict[str, Any]:
    """Return the schema the OpenAPI ``document`` publishes for the page of ``path``."""
    if location == "body":
        bodySchema = document["components"]["schemas"][TransactionSelectionRequest.__name__]
        return bodySchema["properties"][PAGE_FIELD]
    parameters = document["paths"][path]["get"]["parameters"]
    return next(param["schema"] for param in parameters if param["name"] == PAGE_FIELD)


@pytest_asyncio.fixture(name="pageApp", scope="module")
async def PageApp() -> AsyncIterator[FastAPI]:
    """Yield CreateApp's app with test settings; dispose of its never-connected engine after."""
    from app.main import CreateApp

    application = CreateApp(BuildTestSettings())
    yield application
    await DisposeEngine(application.state.engine)


@pytest_asyncio.fixture(name="pageClient")
async def PageClient(pageApp: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client of pageApp sending the allow-listed Origin, closed at teardown."""
    transport = httpx.ASGITransport(app=pageApp)
    headers = {"Origin": ALLOWED_ORIGIN, "Content-Type": "application/json"}
    async with httpx.AsyncClient(
        transport=transport, base_url=API_BASE_URL, headers=headers
    ) as client:
        yield client


@pytest.mark.parametrize(("model", "value", "expectedPage"), ACCEPTED_PAGES)
def test_page_is_accepted(model: type[BaseModel], value: object, expectedPage: int) -> None:
    """Digit text or an int (query pages), or a JSON integer (the body page), in range."""
    assert model.model_validate({PAGE_FIELD: value}).page == expectedPage


@pytest.mark.parametrize(("model", "value"), REJECTED_PAGES)
def test_page_is_rejected_as_malformed(model: type[BaseModel], value: object) -> None:
    """A bool, a float, non-digit text or a value outside the PIC range is a ``page`` error."""
    with pytest.raises(ValidationError) as raised:
        model.model_validate({PAGE_FIELD: value})
    assert [error["loc"] for error in raised.value.errors()] == [(PAGE_FIELD,)]


@pytest.mark.parametrize(("model", "maxPage"), PAGE_FIELD_BOUNDS)
def test_page_schema_is_a_bounded_integer(model: type[BaseModel], maxPage: int) -> None:
    """The model's JSON schema states type integer and the integer bounds 0 and its maximum."""
    pageSchema = model.model_json_schema()["properties"][PAGE_FIELD]
    assert {key: pageSchema[key] for key in BOUND_KEYS} == {
        "type": "integer",
        "minimum": 0,
        "maximum": maxPage,
    }
    assert type(pageSchema["minimum"]) is int
    assert type(pageSchema["maximum"]) is int


@pytest.mark.parametrize(("path", "location", "maxPage"), PUBLISHED_PAGE_FIELDS)
def test_published_page_is_a_bounded_integer(pageApp, path, location, maxPage) -> None:
    """The OpenAPI document gives each page field type integer, minimum 0 and its maximum."""
    pageSchema = PublishedPageSchema(pageApp.openapi(), path, location)
    assert {key: pageSchema[key] for key in BOUND_KEYS} == {
        "type": "integer",
        "minimum": 0,
        "maximum": maxPage,
    }


@pytest.mark.parametrize("pageRequest", WRONG_PAGE_REQUESTS)
async def test_wrong_page_gets_the_sanitised_422_body(pageApp, pageClient, pageRequest) -> None:
    """The 422 body has the five keys and names page; its detail items echo no input."""
    headers = SessionCookieHeader(pageApp.state.settings, pageRequest.userType)
    response = await pageClient.request(
        pageRequest.method, pageRequest.path, content=pageRequest.content, headers=headers
    )
    assert response.status_code == HTTP_UNPROCESSABLE
    responseBody = response.json()
    assert set(responseBody) == LEGACY_VALIDATION_KEYS
    assert responseBody["field_errors"] == [PAGE_FIELD]
    assert responseBody["cursor_field"] == PAGE_FIELD
    assert [item["loc"] for item in responseBody["detail"]] == [[pageRequest.location, PAGE_FIELD]]
    assert all(set(item) == VALIDATION_DETAIL_KEYS for item in responseBody["detail"])
