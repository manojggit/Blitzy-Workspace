"""FastAPI application factory and ASGI entry point of the CardDemo backend.

Replaces: CICS region CARDDEMO transaction inventory (csd/CARDDEMO.CSD) and RACF sign-on entry;
serves the 17 base transactions under /api/v1.

The DEFINE TRANSACTION entries of csd/CARDDEMO.CSD become the endpoint groups of
app.api.v1.router, in menu order: CC00 (COSGN00C); CM00 (COMEN01C) and CA00 (COADM01C);
CAVW (COACTVWC) and CAUP (COACTUPC); CCLI (COCRDLIC), CCDL (COCRDSLC) and CCUP (COCRDUPC);
CT00, CT01 and CT02 (COTRN00C, COTRN01C and COTRN02C); CR00 (CORPT00C); CB00 (COBIL00C); and
CU00-CU03 (COUSR00C-COUSR03C). CDV1 (COCRDSEC) has no program source and no route. The health
probes are served outside /api/v1. Specified in the Technical Specification, Sections 0.6.5,
0.6.9, 0.7.1 and 0.11.1.

This module only wires the shared components of app.core and app.api. CreateApp keeps the
settings, the lazily connecting engine and the session factory on app.state, where the
dependencies, the batch-window locks and the readiness probe read them, so tests build an app
with their own settings. InstallOpenApiGenerator declares the 422 body the request-validation
handler sends, LegacyValidationErrorResponse, in place of FastAPI's HTTPValidationError, which
lists only ``detail``. Uvicorn serves the module-level ``app``:
``cd backend && ../.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000``.
"""

from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.constants import REF_PREFIX
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.health import ROUTER as HEALTH_ROUTER
from app.api.v1.router import ROUTER as API_V1_ROUTER
from app.core.config import API_V1_PREFIX, GetSettings, Settings
from app.core.database import BuildEngine, BuildSessionFactory, DisposeEngine
from app.core.errors import (
    HandleHttpException,
    HandleLegacyMessageError,
    HandleRequestValidationError,
    LegacyMessageError,
)
from app.core.logging import ConfigureLogging
from app.core.middleware import OriginGuard
from app.schemas.common import LegacyValidationErrorResponse

API_TITLE = "CardDemo API"
API_VERSION = "1.0.0"
API_DESCRIPTION = (
    "REST replacement of the CardDemo CICS online transactions: each base transaction ID is"
    " an endpoint group under /api/v1, with the legacy program's field edits and message"
    " literals. Authentication is a session cookie (carddemo_session, HttpOnly) set by"
    " POST /api/v1/auth/login, and every cookie-authenticated POST, PUT and DELETE must carry"
    " an allow-listed Origin header. Error bodies are"
    " {message, info_message, field_errors, cursor_field}; a 422 also carries FastAPI's detail"
    " list."
)
# One entry per router tag; each name equals the tag its router declares in app.api.
OPENAPI_TAGS = [
    {"name": "auth", "description": "CC00 (COSGN00C): sign-on, sign-off and current user."},
    {"name": "menu", "description": "CM00 (COMEN01C) main menu and CA00 (COADM01C) admin menu."},
    {"name": "accounts", "description": "CAVW (COACTVWC) account view, CAUP (COACTUPC) update."},
    {
        "name": "cards",
        "description": "CCLI (COCRDLIC) card list, CCDL (COCRDSLC) view, CCUP (COCRDUPC) update.",
    },
    {
        "name": "transactions",
        "description": "CT00 (COTRN00C) list, CT01 (COTRN01C) view, CT02 (COTRN02C) add.",
    },
    {"name": "reports", "description": "CR00 (CORPT00C): transaction report with exports."},
    {"name": "billpay", "description": "CB00 (COBIL00C): bill payment of the account balance."},
    {
        "name": "admin-users",
        "description": "CU00-CU03 (COUSR00C-COUSR03C): list, add, update and delete users.",
    },
    {"name": "health", "description": "Liveness and readiness probes, outside /api/v1."},
]
DOCS_URL = "/docs"
REDOC_URL = "/redoc"
OPENAPI_URL = "/openapi.json"
ENABLED_DOCS_OPTIONS = {"docs_url": DOCS_URL, "redoc_url": REDOC_URL, "openapi_url": OPENAPI_URL}
# None removes each route, so the three paths answer FastAPI's 404 and disclose no API surface.
DISABLED_DOCS_OPTIONS = {"docs_url": None, "redoc_url": None, "openapi_url": None}
CORS_ALLOWED_METHODS = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
CORS_ALLOWED_HEADERS = ["Content-Type", "Accept"]
# The CR00 CSV and PDF exports name their file in Content-Disposition.
CORS_EXPOSED_HEADERS = ["Content-Disposition"]
# FastAPI declares this status on every operation it validates, with its HTTPValidationError.
VALIDATION_ERROR_STATUS = "422"
VALIDATION_ERROR_DESCRIPTION = (
    "Structurally invalid request, which a 3270 terminal could never send: malformed or"
    " unreadable JSON, or a parameter or field outside its declared type, length, enum or"
    " code-page-037 repertoire."
)
JSON_MEDIA_TYPE = "application/json"
# FastAPI's 422 components; HTTPValidationError references ValidationError, so it comes first.
FASTAPI_VALIDATION_SCHEMAS = ("HTTPValidationError", "ValidationError")


@asynccontextmanager
async def Lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Close the engine's pooled connections at shutdown; connect nothing at startup.

    The app starts while PostgreSQL is still coming up: GET /health/ready reports readiness.
    """
    try:
        yield
    finally:
        await DisposeEngine(application.state.engine)


def BuildFastApi(settings: Settings) -> FastAPI:
    """Build the FastAPI app with its OpenAPI metadata, serving the docs only when enabled.

    Settings.ApiDocsEnabled holds the ENABLE_API_DOCS and APP_ENV rule, so it is not repeated
    here: unset outside production serves the docs, and production hides them.
    """
    docsOptions = ENABLED_DOCS_OPTIONS if settings.ApiDocsEnabled() else DISABLED_DOCS_OPTIONS
    return FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=API_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        lifespan=Lifespan,
        **docsOptions,
    )


def AttachState(application: FastAPI, settings: Settings) -> None:
    """Keep the settings, the lazily connecting engine and its session factory on app.state."""
    application.state.settings = settings
    application.state.engine = BuildEngine(settings)
    application.state.sessionFactory = BuildSessionFactory(application.state.engine)


def AddMiddleware(application: FastAPI, settings: Settings) -> None:
    """Add the CSRF origin guard, then CORS, so CORS is the outer layer.

    Starlette wraps later-added middleware around earlier ones. CORS therefore answers the
    preflight OPTIONS before the guard sees it, and decorates the guard's 403 with its headers,
    so the browser can read the refusal. Credentials are allowed, so the origins are always the
    explicit BACKEND_CORS_ORIGINS list: Settings refuses a wildcard or a malformed entry at
    startup.
    """
    allowedOrigins = settings.CorsOrigins()
    application.add_middleware(OriginGuard, allowedOrigins=allowedOrigins)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowedOrigins),
        allow_credentials=True,
        allow_methods=CORS_ALLOWED_METHODS,
        allow_headers=CORS_ALLOWED_HEADERS,
        expose_headers=CORS_EXPOSED_HEADERS,
    )


def AddExceptionHandlers(application: FastAPI) -> None:
    """Register the legacy-message, request-validation and HTTP-exception handlers only.

    The HTTP-exception handler rewrites one case, FastAPI's 400 for a JSON body it cannot read
    (not valid UTF-8, for example), into the 422 request-validation body, and hands every other
    HTTP exception to FastAPI's default handler unchanged. No catch-all handler is registered:
    an unexpected error is FastAPI's default 500, which the frontend shows as the legacy abend
    text UNEXPECTED ABEND OCCURRED.
    """
    application.add_exception_handler(LegacyMessageError, HandleLegacyMessageError)
    application.add_exception_handler(RequestValidationError, HandleRequestValidationError)
    application.add_exception_handler(StarletteHTTPException, HandleHttpException)


def IncludeRouters(application: FastAPI) -> None:
    """Serve the health probes at the root and the eight cluster routers under /api/v1."""
    application.include_router(HEALTH_ROUTER)
    application.include_router(API_V1_ROUTER, prefix=API_V1_PREFIX)


def SchemaRefsIn(node: Any) -> Iterator[str]:
    """Yield every ``$ref`` value inside ``node``, a part of the OpenAPI document."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value
            else:
                yield from SchemaRefsIn(value)
    elif isinstance(node, list):
        for item in node:
            yield from SchemaRefsIn(item)


def LegacyValidationResponse() -> dict[str, Any]:
    """Return a new 422 response entry declaring LegacyValidationErrorResponse."""
    schemaRef = {"$ref": REF_PREFIX + LegacyValidationErrorResponse.__name__}
    return {
        "description": VALIDATION_ERROR_DESCRIPTION,
        "content": {JSON_MEDIA_TYPE: {"schema": schemaRef}},
    }


def LegacyValidationSchemas() -> dict[str, Any]:
    """Return the components of LegacyValidationErrorResponse and of the models it nests.

    Pydantic puts the nested models under ``$defs``; they become sibling components, and every
    reference points into ``components.schemas``, as FastAPI's own references do.
    """
    responseSchema = LegacyValidationErrorResponse.model_json_schema(
        ref_template=REF_PREFIX + "{model}", mode="serialization"
    )
    nestedSchemas = responseSchema.pop("$defs", {})
    return {**nestedSchemas, LegacyValidationErrorResponse.__name__: responseSchema}


def RepointValidationResponses(document: dict[str, Any]) -> int:
    """Replace every operation's 422 response with LegacyValidationResponse; return the count.

    Only operations that already declare 422 change, and no other status is touched.
    """
    repointedCount = 0
    for pathItem in document.get("paths", {}).values():
        for operation in pathItem.values():
            responses = operation.get("responses", {}) if isinstance(operation, dict) else {}
            if VALIDATION_ERROR_STATUS in responses:
                responses[VALIDATION_ERROR_STATUS] = LegacyValidationResponse()
                repointedCount += 1
    return repointedCount


def DropUnreferencedSchemas(document: dict[str, Any], schemaNames: Sequence[str]) -> None:
    """Remove, in order, each named component that no ``$ref`` in ``document`` points at.

    A component that only an earlier, removed one referenced is removed too, so ``schemaNames``
    lists a referring component before the components it references.
    """
    componentSchemas = document.get("components", {}).get("schemas", {})
    for schemaName in schemaNames:
        if REF_PREFIX + schemaName not in SchemaRefsIn(document):
            componentSchemas.pop(schemaName, None)


def DeclareLegacyValidationBody(document: dict[str, Any]) -> None:
    """Declare, in place, the 422 body HandleRequestValidationError sends on every operation.

    FastAPI declares 422 with its HTTPValidationError, which lists ``detail`` alone, while the
    body sent also carries the four standard legacy keys. The components stay sorted by name,
    as FastAPI writes them.
    """
    if RepointValidationResponses(document):
        components = document.setdefault("components", {})
        componentSchemas = {**components.get("schemas", {}), **LegacyValidationSchemas()}
        components["schemas"] = dict(sorted(componentSchemas.items()))
    DropUnreferencedSchemas(document, FASTAPI_VALIDATION_SCHEMAS)


def InstallOpenApiGenerator(application: FastAPI) -> None:
    """Make ``application`` publish the legacy 422 body in its OpenAPI document.

    This is FastAPI's documented extension point. The replacement builds FastAPI's own
    document once, completes it with DeclareLegacyValidationBody and keeps it in
    ``openapi_schema``, as FastAPI caches its document, so /openapi.json, /docs and the
    contract tests read the same document. It adds no route: with the docs disabled,
    /openapi.json stays FastAPI's 404.
    """
    buildFastApiDocument = application.openapi

    def BuildLegacyDocument() -> dict[str, Any]:
        """Return the cached document, building and completing it on the first call."""
        if application.openapi_schema:
            return application.openapi_schema
        document = buildFastApiDocument()
        DeclareLegacyValidationBody(document)
        application.openapi_schema = document
        return document

    # FastAPI reads this exact attribute name, so it stays lower-case: a framework-mandated
    # exception to the MK_Modernization Rule's PascalCase method naming.
    application.openapi = BuildLegacyDocument


def CreateApp(settings: Settings | None = None) -> FastAPI:
    """Build the CardDemo backend app from ``settings``, or from the process-wide Settings.

    GetSettings refuses a blank or short SECRET_KEY with a validation error, which is left to
    propagate, so the backend never starts without a real key. Logging is configured first,
    so card numbers are masked in every record the app and Uvicorn write.
    """
    appSettings = settings if settings is not None else GetSettings()
    ConfigureLogging()
    application = BuildFastApi(appSettings)
    AttachState(application, appSettings)
    AddMiddleware(application, appSettings)
    AddExceptionHandlers(application)
    IncludeRouters(application)
    InstallOpenApiGenerator(application)
    return application


# "app" is the uvicorn target name app.main:app, a framework-mandated exception to UPPER_CASE.
app = CreateApp()
