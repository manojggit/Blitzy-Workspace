"""Unit tests for app.api.v1.LosslessKeyRoute, the route class of the raw-key routers.

uvicorn and httpx put the percent-decoded request target in the ASGI ``path`` and the target as
sent in ``raw_path``. The cases build those scopes directly, so no database or app is needed:

- LosslessRoutePath keeps an encoded "/" or "%" inside its segment. It returns None, so the
    route matches the decoded ``path`` as APIRoute does, without a ``raw_path``, for a
    non-ASCII one, for Starlette's trailing-slash probe (``path`` rewritten, ``raw_path``
    not) and for a ``root_path`` the lossless path does not start with;
- a key route receives "A/B", "%", a typed "%2F" and a padded dot as keyed, a key ending in
    "/edit" belongs to the item route, and the edit route needs a literal "/edit" suffix;
- only the route's own parameters are decoded, a method mismatch is still a partial match, and
    APIRoute itself cannot match an encoded slash.
"""

from typing import Any
from urllib.parse import unquote

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Match

from app.api.v1 import LosslessKeyRoute, LosslessRoutePath

ITEM_PATH = "/items/{itemId}"
EDIT_PATH = "/items/{itemId}/edit"
KEY_NAME = "itemId"
GET_METHOD = "GET"
PUT_METHOD = "PUT"
# A parameter of an enclosing route, which the key route must pass through untouched.
OUTER_PARAMS = {"outerKey": "x%2Fy"}


async def ShowItem(itemId: str) -> str:
    """Return the key the route received."""
    return itemId


ITEM_ROUTE = LosslessKeyRoute(ITEM_PATH, ShowItem, methods=[GET_METHOD])
EDIT_ROUTE = LosslessKeyRoute(EDIT_PATH, ShowItem, methods=[GET_METHOD])

LOSSLESS_PATH_CASES = [
    pytest.param("/items/A%2FB", {}, "/items/A%2FB", id="encoded-slash-kept"),
    pytest.param("/items/%252F", {}, "/items/%252F", id="typed-percent-2f-kept"),
    pytest.param("/items/%41%2f", {}, "/items/A%2F", id="escapes-normalised"),
    pytest.param(
        "/items/A%2FB?screen=update", {"path": "/items/A/B"}, "/items/A%2FB", id="query-dropped"
    ),
    pytest.param("/api/items/A%2FB", {"root_path": "/api"}, "/api/items/A%2FB", id="root-path"),
    pytest.param("/items/A", {"raw_path": None}, None, id="no-raw-path"),
    pytest.param(
        "/items/A", {"raw_path": b"/items/\xc4\x80", "path": "/items/\u0100"}, None, id="not-ascii"
    ),
    pytest.param("/items/A%2FB/", {"path": "/items/A/B"}, None, id="trailing-slash-probe"),
    pytest.param("/a%25b/items/x", {"root_path": "/a%b"}, None, id="root-path-not-a-prefix"),
]

KEY_MATCH_CASES = [
    pytest.param(ITEM_ROUTE, "/items/A%2FB", "A/B", id="slash-inside-key"),
    pytest.param(ITEM_ROUTE, "/items/%2F", "/", id="slash-only-key"),
    pytest.param(ITEM_ROUTE, "/items/%25", "%", id="percent-key"),
    pytest.param(ITEM_ROUTE, "/items/%252F", "%2F", id="typed-percent-2f-key"),
    pytest.param(ITEM_ROUTE, "/items/.%20", ". ", id="padded-dot-key"),
    pytest.param(ITEM_ROUTE, "/items/A%2Fedit", "A/edit", id="key-ending-in-edit"),
    pytest.param(EDIT_ROUTE, "/items/A/edit", "A", id="literal-edit-suffix"),
]

UNMATCHED_CASES = [
    pytest.param(EDIT_ROUTE, "/items/A%2Fedit", id="edit-suffix-inside-key"),
    pytest.param(ITEM_ROUTE, "/items/A/B", id="two-segments"),
]

DECODED_FALLBACK_CASES = [
    pytest.param("/items/A", {"raw_path": None}, id="no-raw-path"),
    pytest.param("/items/A/", {"path": "/items/A"}, id="trailing-slash-probe"),
]


def BuildScope(rawTarget: str, overrides: dict[str, Any]) -> dict[str, Any]:
    """Return the HTTP scope uvicorn builds for ``rawTarget``, with ``overrides`` applied."""
    rawPath = rawTarget.encode("ascii").partition(b"?")[0]
    scope = {
        "type": "http",
        "method": GET_METHOD,
        "path": unquote(rawPath.decode("ascii")),
        "raw_path": rawPath,
        "root_path": "",
        "path_params": dict(OUTER_PARAMS),
    }
    scope.update(overrides)
    return scope


@pytest.mark.parametrize(("rawTarget", "overrides", "expectedPath"), LOSSLESS_PATH_CASES)
def test_lossless_route_path(
    rawTarget: str, overrides: dict[str, Any], expectedPath: str | None
) -> None:
    """The lossless path, or None where the decoded path must be matched instead."""
    assert LosslessRoutePath(BuildScope(rawTarget, overrides)) == expectedPath


@pytest.mark.parametrize(("route", "rawTarget", "expectedKey"), KEY_MATCH_CASES)
def test_key_route_receives_the_key_as_keyed(
    route: LosslessKeyRoute, rawTarget: str, expectedKey: str
) -> None:
    """A full match decodes the route's own key once and leaves outer parameters alone."""
    match, childScope = route.matches(BuildScope(rawTarget, {}))
    assert match is Match.FULL
    assert childScope["path_params"] == {**OUTER_PARAMS, KEY_NAME: expectedKey}
    assert childScope["route"] is route


@pytest.mark.parametrize(("route", "rawTarget"), UNMATCHED_CASES)
def test_key_route_does_not_match(route: LosslessKeyRoute, rawTarget: str) -> None:
    """An encoded slash never forms a segment boundary, and a literal one always does."""
    match, _ = route.matches(BuildScope(rawTarget, {}))
    assert match is Match.NONE


@pytest.mark.parametrize(("rawTarget", "overrides"), DECODED_FALLBACK_CASES)
def test_key_route_matches_the_decoded_path_without_a_lossless_one(
    rawTarget: str, overrides: dict[str, Any]
) -> None:
    """Without a lossless path the route matches as APIRoute does, the slash probe included."""
    match, childScope = ITEM_ROUTE.matches(BuildScope(rawTarget, overrides))
    assert match is Match.FULL
    assert childScope["path_params"][KEY_NAME] == "A"


def test_method_mismatch_is_a_partial_match_with_the_key_decoded() -> None:
    """Another method still matches partially, so Starlette answers 405 as it would."""
    match, childScope = ITEM_ROUTE.matches(BuildScope("/items/A%2FB", {"method": PUT_METHOD}))
    assert match is Match.PARTIAL
    assert childScope["path_params"][KEY_NAME] == "A/B"


def test_api_route_cannot_match_an_encoded_slash() -> None:
    """The default route class sees two segments: the defect LosslessKeyRoute removes."""
    defaultRoute = APIRoute(ITEM_PATH, ShowItem, methods=[GET_METHOD])
    match, _ = defaultRoute.matches(BuildScope("/items/A%2FB", {}))
    assert match is Match.NONE
