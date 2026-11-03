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
with their own settings. Uvicorn serves the module-level ``app``:
``cd backend && ../.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000``.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import ROUTER as HEALTH_ROUTER
from app.api.v1.router import ROUTER as API_V1_ROUTER
from app.core.config import API_V1_PREFIX, GetSettings, Settings
from app.core.database import BuildEngine, BuildSessionFactory, DisposeEngine
from app.core.errors import (
    HandleLegacyMessageError,
    HandleRequestValidationError,
    LegacyMessageError,
)
from app.core.logging import ConfigureLogging
from app.core.middleware import OriginGuard

API_TITLE = "CardDemo API"
API_VERSION = "1.0.0"
API_DESCRIPTION = (
    "REST replacement of the CardDemo CICS online transactions: each base transaction ID is"
    " an endpoint group under /api/v1, with the legacy program's field edits and message"
    " literals. Authentication is a session cookie (carddemo_session, HttpOnly) set by"
    " POST /api/v1/auth/login, and every cookie-authenticated POST, PUT and DELETE must carry"
    " an allow-listed Origin header. Error bodies are"
    " {message, info_message, field_errors, cursor_field}."
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
    explicit BACKEND_CORS_ORIGINS list, never "*".
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
    """Register the legacy-message and request-validation handlers, and nothing broader.

    No catch-all handler is registered: an unexpected error is FastAPI's default 500, which the
    frontend shows as the legacy abend text UNEXPECTED ABEND OCCURRED.
    """
    application.add_exception_handler(LegacyMessageError, HandleLegacyMessageError)
    application.add_exception_handler(RequestValidationError, HandleRequestValidationError)


def IncludeRouters(application: FastAPI) -> None:
    """Serve the health probes at the root and the eight cluster routers under /api/v1."""
    application.include_router(HEALTH_ROUTER)
    application.include_router(API_V1_ROUTER, prefix=API_V1_PREFIX)


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
    return application


# "app" is the uvicorn target name app.main:app, a framework-mandated exception to UPPER_CASE.
app = CreateApp()
