"""API contract tests over the OpenAPI document FastAPI generates for the CardDemo backend.

They cover the 17 base transactions of csd/CARDDEMO.CSD (CC00, CM00, CA00, CAVW, CAUP, CCLI,
CCDL, CCUP, CT00, CT01, CT02, CR00, CB00, CU00, CU01, CU02 and CU03) and the health probes.
Schemathesis generates requests from /openapi.json. Each is sent in the test's own event loop by
a conftest client signed on in the role its endpoint requires, and every response must pass the
server-error, status-code, content-type and response-schema checks.

Run the suite from the repository root:

```bash
cd backend && ../.venv/bin/pytest tests/contract
```
"""

import dataclasses
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis.checks import not_a_server_error
from schemathesis.specs.openapi.checks import (
    content_type_conformance,
    response_schema_conformance,
    status_code_conformance,
)

from app.core.config import API_V1_PREFIX

OPENAPI_PATH = "/openapi.json"
# Equal to the BACKEND_CORS_ORIGINS value tests/conftest.py sets, which OriginGuard allow-lists.
ALLOWED_ORIGIN = "http://127.0.0.1:3000"
ADMIN_TAGS = frozenset({"admin-users"})
ADMIN_MENU_PREFIX = API_V1_PREFIX + "/menu/admin"
CONTRACT_CHECKS = [
    not_a_server_error,
    status_code_conformance,
    content_type_conformance,
    response_schema_conformance,
]
HTTPX_REQUEST_KEYS = frozenset(
    {"method", "url", "headers", "params", "json", "data", "content", "files"}
)
RAW_BODY_TYPES = (bytes, str)
LEGACY_ERROR_STATUSES = frozenset({"400", "401", "403", "404", "409", "423", "429", "503"})
LEGACY_ERROR_SCHEMA_REF = "#/components/schemas/LegacyErrorResponse"
COMPONENT_SCHEMA_PREFIX = "#/components/schemas/"
LEGACY_ERROR_KEYS = frozenset({"message", "info_message", "field_errors", "cursor_field"})
JSON_MEDIA_TYPE = "application/json"
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "patch"})
HTTP_OK = 200
# The function-scoped fixtures are shared by every example of the run, the database calls make a
# per-example deadline meaningless, and no example database is written into the source tree.
CONTRACT_SETTINGS = settings(
    max_examples=25,
    deadline=None,
    database=None,
    suppress_health_check=[
        HealthCheck.function_scoped_fixture,
        HealthCheck.too_slow,
        HealthCheck.filter_too_much,
        HealthCheck.data_too_large,
    ],
)
# Every operation of AAP Section 0.6.9, by transaction ID, plus the probes outside /api/v1.
EXPECTED_OPERATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "CC00": (
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/logout"),
        ("GET", "/api/v1/auth/me"),
    ),
    "CM00": (("GET", "/api/v1/menu/main"), ("POST", "/api/v1/menu/main/selection")),
    "CA00": (("GET", "/api/v1/menu/admin"), ("POST", "/api/v1/menu/admin/selection")),
    "CAVW": (("GET", "/api/v1/accounts/{acctId}"),),
    "CAUP": (("GET", "/api/v1/accounts/{acctId}/edit"), ("PUT", "/api/v1/accounts/{acctId}")),
    "CCLI": (("GET", "/api/v1/cards"), ("POST", "/api/v1/cards/selection")),
    "CCDL": (("GET", "/api/v1/cards/{cardNum}"),),
    "CCUP": (("GET", "/api/v1/cards/{cardNum}/edit"), ("PUT", "/api/v1/cards/{cardNum}")),
    "CT00": (("GET", "/api/v1/transactions"), ("POST", "/api/v1/transactions/selection")),
    "CT01": (("GET", "/api/v1/transactions/{tranId}"),),
    "CT02": (("POST", "/api/v1/transactions"),),
    "CR00": (
        ("GET", "/api/v1/reports/transactions"),
        ("GET", "/api/v1/reports/transactions/export"),
    ),
    "CB00": (("POST", "/api/v1/billpay"),),
    "CU00": (("GET", "/api/v1/admin/users"), ("POST", "/api/v1/admin/users/selection")),
    "CU01": (("POST", "/api/v1/admin/users"),),
    "CU02": (("GET", "/api/v1/admin/users/{userId}"), ("PUT", "/api/v1/admin/users/{userId}")),
    # The GET is CU02's fetch as well, told apart by its screen query parameter.
    "CU03": (
        ("GET", "/api/v1/admin/users/{userId}"),
        ("DELETE", "/api/v1/admin/users/{userId}"),
    ),
    "HEALTH": (("GET", "/health"), ("GET", "/health/ready")),
}
SCHEMA = schemathesis.pytest.from_fixture("contractSchema")


@dataclasses.dataclass(frozen=True)
class ContractClients:
    """The two signed-on clients: one per role guard (RequireAdmin, RequireRegular)."""

    adminClient: httpx.AsyncClient
    regularClient: httpx.AsyncClient


@pytest.fixture(name="contractSchema")
def ContractSchema(testApp: Any) -> Any:
    """Return the schemathesis schema of the conftest's app, loaded from its /openapi.json.

    CreateApp's lifespan connects nothing at startup, so loading through the ASGI app opens no
    database connection outside the test's event loop. No path-parameter filter is registered:
    schemathesis itself never generates "", ".", ".." or "/" as a path value, the values a URL
    segment cannot carry to the declared operation.
    """
    return schemathesis.openapi.from_asgi(OPENAPI_PATH, testApp)


@pytest.fixture(name="contractClients")
def SignedOnContractClients(
    adminClient: httpx.AsyncClient, regularClient: httpx.AsyncClient
) -> ContractClients:
    """Return the conftest's admin and regular clients, each with its own cookie jar."""
    assert adminClient is not regularClient
    return ContractClients(adminClient=adminClient, regularClient=regularClient)


def RequiresAdmin(case: Any) -> bool:
    """Return True when the case's operation is guarded by RequireAdmin (CA00, CU00-CU03)."""
    isAdminTag = bool(set(case.operation.tags or ()) & ADMIN_TAGS)
    return isAdminTag or case.path.startswith(ADMIN_MENU_PREFIX)


def ClientFor(case: Any, clients: ContractClients) -> httpx.AsyncClient:
    """Return the client signed on in the role the case's operation requires."""
    if RequiresAdmin(case):
        return clients.adminClient
    return clients.regularClient


def TransportKwargs(case: Any, baseUrl: str) -> dict[str, Any]:
    """Return the case as httpx.AsyncClient.request keywords, with the allow-listed Origin."""
    caseKwargs = case.as_transport_kwargs(base_url=baseUrl, headers={"Origin": ALLOWED_ORIGIN})
    requestKwargs = {
        keyName: keyValue
        for keyName, keyValue in caseKwargs.items()
        if keyName in HTTPX_REQUEST_KEYS
    }
    if isinstance(requestKwargs.get("data"), RAW_BODY_TYPES):
        requestKwargs["content"] = requestKwargs.pop("data")
    return requestKwargs


async def SendCase(case: Any, clients: ContractClients) -> httpx.Response:
    """Send the case with its role's client in the test's event loop; redirects not followed.

    The cookie jar is restored afterwards, so a generated sign-on or sign-off response never
    replaces or clears the session cookie the later operations of that role are sent with.
    """
    client = ClientFor(case, clients)
    sessionCookies = httpx.Cookies(client.cookies)
    response = await client.request(**TransportKwargs(case, str(client.base_url)))
    client.cookies = sessionCookies
    return response


def DocumentedOperations(document: dict[str, Any]) -> set[tuple[str, str]]:
    """Return every (METHOD, path) pair of the document, a trailing slash removed."""
    return {
        (methodName.upper(), path.rstrip("/") or "/")
        for path, pathItem in document.get("paths", {}).items()
        for methodName in pathItem
        if methodName in HTTP_METHODS
    }


def ExpectedOperations() -> set[tuple[str, str]]:
    """Return the union of EXPECTED_OPERATIONS, normalised as DocumentedOperations does."""
    return {
        (methodName, path.rstrip("/") or "/")
        for operations in EXPECTED_OPERATIONS.values()
        for methodName, path in operations
    }


def ApiOperations(document: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield (METHOD, path, operation) for every operation under /api/v1."""
    for path, pathItem in document.get("paths", {}).items():
        if path.startswith(API_V1_PREFIX):
            for methodName, operation in pathItem.items():
                if methodName in HTTP_METHODS:
                    yield methodName.upper(), path, operation


def IsLegacyErrorSchema(document: dict[str, Any], schemaRef: str | None) -> bool:
    """Return True when schemaRef is LegacyErrorResponse, or a component holding its four keys.

    The CU00 invalid-selection 400 declares UserSelectionErrorResponse, which extends the
    legacy error body with the re-browsed page, so the four keys decide rather than the name.
    """
    if schemaRef == LEGACY_ERROR_SCHEMA_REF:
        return True
    if not schemaRef or not schemaRef.startswith(COMPONENT_SCHEMA_PREFIX):
        return False
    componentName = schemaRef.removeprefix(COMPONENT_SCHEMA_PREFIX)
    componentSchema = document.get("components", {}).get("schemas", {}).get(componentName, {})
    return LEGACY_ERROR_KEYS.issubset(componentSchema.get("properties", {}))


def LegacyErrorViolations(document: dict[str, Any]) -> list[str]:
    """Return "<METHOD> <path> <status>" for each legacy error status without the legacy body."""
    violations: list[str] = []
    for methodName, path, operation in ApiOperations(document):
        for statusCode, response in operation.get("responses", {}).items():
            schemaRef = (
                response.get("content", {}).get(JSON_MEDIA_TYPE, {}).get("schema", {}).get("$ref")
            )
            isViolation = statusCode in LEGACY_ERROR_STATUSES and not IsLegacyErrorSchema(
                document, schemaRef
            )
            if isViolation:
                violations.append(f"{methodName} {path} {statusCode}")
    return violations


async def FetchDocument(client: httpx.AsyncClient) -> dict[str, Any]:
    """Return the served OpenAPI document; /openapi.json must answer 200 under APP_ENV=test."""
    response = await client.get(OPENAPI_PATH)
    assert response.status_code == HTTP_OK, response.text
    return response.json()


@SCHEMA.parametrize()
@CONTRACT_SETTINGS
async def test_operation_conforms_to_contract(
    case: Any, contractClients: ContractClients, restoreSeedState: None
) -> None:
    response = await SendCase(case, contractClients)
    case.validate_response(response, checks=CONTRACT_CHECKS)


async def test_openapi_document_lists_every_transaction_operation(
    apiClient: httpx.AsyncClient,
) -> None:
    documentedOperations = DocumentedOperations(await FetchDocument(apiClient))
    expectedOperations = ExpectedOperations()
    missingOperations = sorted(expectedOperations - documentedOperations)
    unexpectedOperations = sorted(documentedOperations - expectedOperations)
    assert documentedOperations == expectedOperations, (
        f"missing: {missingOperations}; unexpected: {unexpectedOperations}"
    )


async def test_legacy_error_statuses_use_legacy_error_model(apiClient: httpx.AsyncClient) -> None:
    document = await FetchDocument(apiClient)
    assert LegacyErrorViolations(document) == []
