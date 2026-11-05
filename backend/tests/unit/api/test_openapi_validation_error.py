"""Unit tests of the 422 body the OpenAPI document declares: LegacyValidationErrorResponse.

app.core.errors.HandleRequestValidationError answers every request FastAPI's validation rejects
with FastAPI's ``detail`` list beside the four legacy keys message, info_message, field_errors
and cursor_field (Technical Specification, Sections 0.6.4 and 0.6.5). The generated document
must declare that body, complete for the contract tests (Section 0.10.5). These tests pin that
app.main.InstallOpenApiGenerator:

- points the 422 response of exactly the operations FastAPI itself gives one, all six admin-user
    operations included, at LegacyValidationErrorResponse;
- declares its five keys as required, and removes FastAPI's HTTPValidationError and
    ValidationError components, leaving no dangling reference;
- leaves every other response and component as FastAPI generates it, so each legacy status
    keeps the error body its router declares, one carrying the four legacy keys;
- caches the completed document, serves it at /openapi.json, and adds no route: with the docs
    disabled in production, /openapi.json stays 404.

Live 422 bodies of sign-on and of the admin-user operations are validated against the declared
component. FastAPI validates parameters and bodies before any handler runs, and the request
session is lazy, so no database is needed: the app is built with CreateApp from Settings whose
engine never connects. SECRET_KEY and the admin user ID are generated with the secrets module,
the admin session cookie is minted with IssueSessionToken, and backend/.env is never read.
"""

import json
import secrets
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import jsonschema_rs
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.core.config import Settings
from app.core.database import DisposeEngine
from app.core.security import IssueSessionToken

ALLOWED_ORIGIN = "http://127.0.0.1:3000"
API_BASE_URL = "http://127.0.0.1:8000"
# A well-formed URL of a database nobody creates: the engine is lazy and never connects.
UNUSED_DATABASE_URL = "postgresql+{driver}://openapi_422_test@127.0.0.1/openapi_422_test"
SECRET_KEY_BYTES = 32
TEST_APP_ENV = "test"
PRODUCTION_APP_ENV = "production"
ENABLE_API_DOCS_VARIABLE = "ENABLE_API_DOCS"
OPENAPI_PATH = "/openapi.json"
JSON_HEADERS = {"Content-Type": "application/json"}
HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_UNPROCESSABLE = 422

ADMIN_USER_TYPE = "A"
ADMIN_USER_ID_PREFIX = "Q"
ADMIN_USER_ID_HEX_BYTES = 3

SCHEMA_REF_PREFIX = "#/components/schemas/"
VALIDATION_STATUS = "422"
LEGACY_VALIDATION_SCHEMA = "LegacyValidationErrorResponse"
VALIDATION_DETAIL_SCHEMA = "ValidationErrorDetail"
FASTAPI_VALIDATION_SCHEMAS = ["HTTPValidationError", "ValidationError"]
LEGACY_ERROR_KEYS = {"message", "info_message", "field_errors", "cursor_field"}
LEGACY_VALIDATION_KEYS = {"detail", "message", "info_message", "field_errors", "cursor_field"}
VALIDATION_DETAIL_KEYS = {"loc", "msg", "type"}
LEGACY_ERROR_STATUSES = ["400", "401", "403", "404", "409", "423", "429", "503"]
# The legacy statuses belong to the /api/v1 transactions; the health probes' 503 has its own body.
API_V1_PATH_PREFIX = "/api/v1/"
ADMIN_USER_OPERATIONS = {
    ("GET", "/api/v1/admin/users"),
    ("POST", "/api/v1/admin/users/selection"),
    ("POST", "/api/v1/admin/users"),
    ("GET", "/api/v1/admin/users/{userId}"),
    ("PUT", "/api/v1/admin/users/{userId}"),
    ("DELETE", "/api/v1/admin/users/{userId}"),
}

LOGIN_PATH = "/api/v1/auth/login"
ADMIN_USERS_PATH = "/api/v1/admin/users"
# One character over the BMS LENGTH of USERID (8) and FNAME (20).
OVER_LONG_USER_ID = "ABCDEFGHI"
OVER_LONG_FIRST_NAME = "F" * 21


@dataclass(frozen=True)
class InvalidRequest:
    """A request FastAPI's validation rejects with 422, and whether it is sent as an admin."""

    method: str
    path: str
    content: bytes | None = None
    signedOnAsAdmin: bool = False


def JsonContent(value: object) -> bytes:
    """Return ``value`` as a JSON request body."""
    return json.dumps(value).encode()


INVALID_REQUESTS = [
    pytest.param(
        InvalidRequest("POST", LOGIN_PATH, JsonContent({"user_id": "123456789"})),
        id="login-user-id-over-bms-length",
    ),
    pytest.param(InvalidRequest("POST", LOGIN_PATH, b"{bad"), id="login-malformed-json"),
    pytest.param(InvalidRequest("POST", LOGIN_PATH, b"\xff"), id="login-not-utf-8"),
    pytest.param(InvalidRequest("POST", LOGIN_PATH, b"[" * 100_000), id="login-nested-too-deep"),
    pytest.param(
        InvalidRequest("GET", f"{ADMIN_USERS_PATH}?direction=bogus", signedOnAsAdmin=True),
        id="cu00-list-direction-outside-enum",
    ),
    pytest.param(
        InvalidRequest(
            "POST",
            f"{ADMIN_USERS_PATH}/selection",
            JsonContent({"selections": "S"}),
            signedOnAsAdmin=True,
        ),
        id="cu00-selection-wrong-json-type",
    ),
    pytest.param(
        InvalidRequest("POST", ADMIN_USERS_PATH, JsonContent({"user_id": 5}), signedOnAsAdmin=True),
        id="cu01-add-user-id-wrong-json-type",
    ),
    pytest.param(
        InvalidRequest("GET", f"{ADMIN_USERS_PATH}/{OVER_LONG_USER_ID}", signedOnAsAdmin=True),
        id="cu02-fetch-user-id-over-bms-length",
    ),
    pytest.param(
        InvalidRequest(
            "PUT",
            f"{ADMIN_USERS_PATH}/X",
            JsonContent({"first_name": OVER_LONG_FIRST_NAME}),
            signedOnAsAdmin=True,
        ),
        id="cu02-update-first-name-over-bms-length",
    ),
    pytest.param(
        InvalidRequest("DELETE", f"{ADMIN_USERS_PATH}/{OVER_LONG_USER_ID}", signedOnAsAdmin=True),
        id="cu03-delete-user-id-over-bms-length",
    ),
]


def BuildTestSettings(appEnv: str = TEST_APP_ENV) -> Settings:
    """Return Settings with a generated key, the allow-listed origin and an unused database."""
    return Settings(
        _env_file=None,
        APP_ENV=appEnv,
        SECRET_KEY=secrets.token_urlsafe(SECRET_KEY_BYTES),
        DATABASE_URL=UNUSED_DATABASE_URL.format(driver="asyncpg"),
        SYNC_DATABASE_URL=UNUSED_DATABASE_URL.format(driver="psycopg2"),
        BACKEND_CORS_ORIGINS=ALLOWED_ORIGIN,
    )


def BuildApp(settings: Settings) -> FastAPI:
    """Return CreateApp's app for ``settings``; app.main is imported here, not at module level."""
    from app.main import CreateApp

    return CreateApp(settings)


def BuildClient(application: FastAPI) -> httpx.AsyncClient:
    """Return a client of ``application`` that sends the allow-listed Origin OriginGuard checks."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url=API_BASE_URL,
        headers={"Origin": ALLOWED_ORIGIN},
    )


def FastApiDocument(application: FastAPI) -> dict[str, Any]:
    """Return the document FastAPI's own generator builds for the routes of ``application``."""
    return get_openapi(
        title=application.title, version=application.version, routes=application.routes
    )


def OperationsOf(document: dict[str, Any]) -> Iterator[tuple[tuple[str, str], dict[str, Any]]]:
    """Yield ``((METHOD, path), operation)`` for every operation of ``document``."""
    for path, pathItem in document["paths"].items():
        for method, operation in pathItem.items():
            yield (method.upper(), path), operation


def OperationsDeclaring(document: dict[str, Any], status: str) -> set[tuple[str, str]]:
    """Return the ``(METHOD, path)`` of every operation of ``document`` declaring ``status``."""
    return {key for key, operation in OperationsOf(document) if status in operation["responses"]}


def Without(entries: dict[str, Any], excludedKeys: set[str]) -> dict[str, Any]:
    """Return a copy of ``entries`` without the keys in ``excludedKeys``."""
    return {key: value for key, value in entries.items() if key not in excludedKeys}


def ResponseRef(operation: dict[str, Any], status: str) -> str:
    """Return the schema reference of the JSON body ``operation`` declares for ``status``."""
    return operation["responses"][status]["content"]["application/json"]["schema"]["$ref"]


def ComponentProperties(document: dict[str, Any], schemaRef: str) -> set[str]:
    """Return the property names of the component of ``document`` that ``schemaRef`` names."""
    componentName = schemaRef.removeprefix(SCHEMA_REF_PREFIX)
    return set(document["components"]["schemas"][componentName]["properties"])


def SchemaRefsIn(node: Any) -> Iterator[str]:
    """Yield every ``$ref`` value inside ``node``."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref":
                yield value
            else:
                yield from SchemaRefsIn(value)
    elif isinstance(node, list):
        for item in node:
            yield from SchemaRefsIn(item)


def ComponentValidator(document: dict[str, Any], schemaName: str) -> jsonschema_rs.Validator:
    """Return a validator of component ``schemaName``, with every component resolvable."""
    rootSchema = {"$ref": SCHEMA_REF_PREFIX + schemaName, "components": document["components"]}
    return jsonschema_rs.Draft202012Validator(rootSchema)


@pytest_asyncio.fixture(name="openApiApp", scope="module")
async def OpenApiApp() -> AsyncIterator[FastAPI]:
    """Yield CreateApp's app with test settings; dispose of its never-connected engine after."""
    application = BuildApp(BuildTestSettings())
    yield application
    await DisposeEngine(application.state.engine)


@pytest.fixture(name="openApiDocument", scope="module")
def OpenApiDocument(openApiApp: FastAPI) -> dict[str, Any]:
    """Return the app's OpenAPI document, as /openapi.json and the contract tests read it."""
    return openApiApp.openapi()


@pytest.fixture(name="fastApiDocument", scope="module")
def FastApiDocumentFixture(openApiApp: FastAPI) -> dict[str, Any]:
    """Return FastAPI's own document of the same routes, before the legacy 422 declaration."""
    return FastApiDocument(openApiApp)


@pytest_asyncio.fixture(name="openApiClient")
async def OpenApiClient(openApiApp: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client of openApiApp, closed at teardown."""
    async with BuildClient(openApiApp) as client:
        yield client


@pytest.fixture(name="adminCookieHeader", scope="module")
def AdminCookieHeader(openApiApp: FastAPI) -> dict[str, str]:
    """Return the Cookie header of a session minted for a generated type-A user ID."""
    settings = openApiApp.state.settings
    adminUserId = ADMIN_USER_ID_PREFIX + secrets.token_hex(ADMIN_USER_ID_HEX_BYTES).upper()
    sessionToken = IssueSessionToken(adminUserId, ADMIN_USER_TYPE, settings)
    return {"Cookie": f"{settings.SESSION_COOKIE_NAME}={sessionToken}"}


def test_422_is_declared_on_exactly_the_operations_fastapi_validates(
    openApiDocument, fastApiDocument
):
    validatedOperations = OperationsDeclaring(fastApiDocument, VALIDATION_STATUS)
    assert validatedOperations
    assert OperationsDeclaring(openApiDocument, VALIDATION_STATUS) == validatedOperations
    assert ADMIN_USER_OPERATIONS <= validatedOperations


def test_every_422_references_the_legacy_validation_body(openApiDocument):
    expectedRef = SCHEMA_REF_PREFIX + LEGACY_VALIDATION_SCHEMA
    for operationKey, operation in OperationsOf(openApiDocument):
        if VALIDATION_STATUS in operation["responses"]:
            assert ResponseRef(operation, VALIDATION_STATUS) == expectedRef, operationKey


@pytest.mark.parametrize(
    ("schemaName", "declaredKeys"),
    [
        pytest.param(LEGACY_VALIDATION_SCHEMA, LEGACY_VALIDATION_KEYS, id="response-body"),
        pytest.param(VALIDATION_DETAIL_SCHEMA, VALIDATION_DETAIL_KEYS, id="detail-item"),
    ],
)
def test_component_declares_exactly_its_keys_all_required(
    schemaName, declaredKeys, openApiDocument
):
    component = openApiDocument["components"]["schemas"][schemaName]
    assert set(component["properties"]) == declaredKeys
    assert set(component["required"]) == declaredKeys


@pytest.mark.parametrize("schemaName", FASTAPI_VALIDATION_SCHEMAS)
def test_fastapi_validation_component_is_removed(schemaName, openApiDocument):
    assert schemaName not in openApiDocument["components"]["schemas"]
    assert SCHEMA_REF_PREFIX + schemaName not in set(SchemaRefsIn(openApiDocument))


def test_every_schema_reference_resolves(openApiDocument):
    componentNames = set(openApiDocument["components"]["schemas"])
    for schemaRef in SchemaRefsIn(openApiDocument):
        assert schemaRef.startswith(SCHEMA_REF_PREFIX), schemaRef
        assert schemaRef.removeprefix(SCHEMA_REF_PREFIX) in componentNames, schemaRef


def test_every_other_response_is_fastapis_own(openApiDocument, fastApiDocument):
    fastApiOperations = dict(OperationsOf(fastApiDocument))
    ownOperations = dict(OperationsOf(openApiDocument))
    assert ownOperations.keys() == fastApiOperations.keys()
    for operationKey, operation in ownOperations.items():
        fastApiResponses = fastApiOperations[operationKey]["responses"]
        assert Without(operation["responses"], {VALIDATION_STATUS}) == Without(
            fastApiResponses, {VALIDATION_STATUS}
        ), operationKey


def test_every_other_component_is_fastapis_own(openApiDocument, fastApiDocument):
    ownSchemas = openApiDocument["components"]["schemas"]
    fastApiSchemas = fastApiDocument["components"]["schemas"]
    addedSchemas = {LEGACY_VALIDATION_SCHEMA, VALIDATION_DETAIL_SCHEMA}
    assert Without(ownSchemas, addedSchemas) == Without(
        fastApiSchemas, set(FASTAPI_VALIDATION_SCHEMAS)
    )


@pytest.mark.parametrize("status", LEGACY_ERROR_STATUSES)
def test_legacy_status_keeps_its_declared_legacy_error_body(
    status, openApiDocument, fastApiDocument
):
    declaringOperations = {
        (method, path)
        for method, path in OperationsDeclaring(openApiDocument, status)
        if path.startswith(API_V1_PATH_PREFIX)
    }
    assert declaringOperations
    fastApiOperations = dict(OperationsOf(fastApiDocument))
    for operationKey, operation in OperationsOf(openApiDocument):
        if operationKey in declaringOperations:
            schemaRef = ResponseRef(operation, status)
            assert schemaRef == ResponseRef(fastApiOperations[operationKey], status), operationKey
            declaredKeys = ComponentProperties(openApiDocument, schemaRef)
            assert LEGACY_ERROR_KEYS <= declaredKeys, operationKey


@pytest.mark.parametrize("invalidRequest", INVALID_REQUESTS)
async def test_live_422_body_is_the_declared_component(
    invalidRequest, openApiClient, openApiDocument, adminCookieHeader
):
    headers = {**JSON_HEADERS, **(adminCookieHeader if invalidRequest.signedOnAsAdmin else {})}
    response = await openApiClient.request(
        invalidRequest.method, invalidRequest.path, content=invalidRequest.content, headers=headers
    )
    assert response.status_code == HTTP_UNPROCESSABLE
    responseBody = response.json()
    declaredComponent = openApiDocument["components"]["schemas"][LEGACY_VALIDATION_SCHEMA]
    assert set(responseBody) == set(declaredComponent["properties"])
    ComponentValidator(openApiDocument, LEGACY_VALIDATION_SCHEMA).validate(responseBody)


async def test_completed_document_is_cached_and_served(openApiApp, openApiClient):
    firstDocument = openApiApp.openapi()
    assert openApiApp.openapi() is firstDocument
    response = await openApiClient.get(OPENAPI_PATH)
    assert response.status_code == HTTP_OK
    assert response.json() == firstDocument


async def test_production_without_docs_setting_serves_no_openapi_document(monkeypatch):
    monkeypatch.delenv(ENABLE_API_DOCS_VARIABLE, raising=False)
    application = BuildApp(BuildTestSettings(PRODUCTION_APP_ENV))
    try:
        async with BuildClient(application) as client:
            response = await client.get(OPENAPI_PATH)
    finally:
        await DisposeEngine(application.state.engine)
    assert response.status_code == HTTP_NOT_FOUND
    assert application.openapi()["components"]["schemas"][LEGACY_VALIDATION_SCHEMA]
