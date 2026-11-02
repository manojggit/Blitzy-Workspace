"""Liveness and readiness probes of the CardDemo backend: GET /health and GET /health/ready.

Replaces no legacy program. The CICS region of csd/CARDDEMO.CSD made the CARDDEMO files
available implicitly (STATUS(ENABLED), OPENTIME(FIRSTREF)); these probes are the operational
control that reports the same availability for the HTTP service. Specified in the Technical
Specification, Section 0.6.5.

main.CreateApp includes ROUTER with no prefix, so both probes stay outside /api/v1. Neither
reads a session cookie. The readiness probe connects straight through app.state.engine and
never takes the batch-window lock of core.locks, so it stays ready while a batch command holds
the window.
"""

import asyncio
import logging
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.dependencies import GetAppSettings

LOGGER = logging.getLogger(__name__)

# Equal to the "health" entry of main.OPENAPI_TAGS.
HEALTH_TAG = "health"
ROUTER = APIRouter(tags=[HEALTH_TAG])

STATUS_OK = "ok"
STATUS_READY = "ready"
STATUS_UNAVAILABLE = "unavailable"

READINESS_PROBE_SQL = "SELECT 1"

# Every failure that means "database not ready", and nothing broader. asyncio.timeout raises
# TimeoutError; refused or unresolvable connects are OSError; statement and pool failures are
# SQLAlchemyError. The asyncpg dialect passes connect-time rejections through unwrapped, so
# asyncpg.PostgresError (database starting up, too many connections, bad credentials, unknown
# database) and asyncpg.InterfaceError are listed as well: each of them must give 503, not 500.
PROBE_FAILURES = (
    TimeoutError,
    OSError,
    SQLAlchemyError,
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
)

LIVENESS_DESCRIPTION = "Returns ok when the process is serving. Does not touch the database."
READINESS_DESCRIPTION = (
    "Runs SELECT 1 on the database within HEALTH_READY_TIMEOUT_SECONDS and returns ready, or"
    " HTTP 503 with unavailable when the query fails or times out. Takes no batch-window lock"
    " and needs no session."
)
UNAVAILABLE_DESCRIPTION = "Database not reachable within HEALTH_READY_TIMEOUT_SECONDS"


class HealthStatusResponse(BaseModel):
    """The body of both probes: a single status word."""

    status: str = Field(
        description="ok from /health; ready, or unavailable with HTTP 503, from /health/ready",
    )


async def ProbeDatabase(engine: AsyncEngine, timeoutSeconds: float) -> bool:
    """Return True when SELECT 1 completes within timeoutSeconds, False on a probe failure.

    Only the exception class name is logged: its text can carry the database host, user or URL.
    """
    try:
        async with asyncio.timeout(timeoutSeconds):
            async with engine.connect() as connection:
                await connection.execute(text(READINESS_PROBE_SQL))
    except PROBE_FAILURES as probeError:
        LOGGER.warning("Readiness probe failed: %s", type(probeError).__name__)
        return False
    return True


@ROUTER.get(
    "/health",
    response_model=HealthStatusResponse,
    summary="Liveness probe",
    description=LIVENESS_DESCRIPTION,
)
async def GetHealth() -> HealthStatusResponse:
    """Report that the process is serving requests."""
    return HealthStatusResponse(status=STATUS_OK)


@ROUTER.get(
    "/health/ready",
    response_model=HealthStatusResponse,
    summary="Readiness probe",
    description=READINESS_DESCRIPTION,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": HealthStatusResponse,
            "description": UNAVAILABLE_DESCRIPTION,
        },
    },
)
async def GetReadiness(
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(GetAppSettings)],
) -> HealthStatusResponse:
    """Report ready when the database answers in time; otherwise set 503 and unavailable.

    The status is set on the injected response, so both outcomes pass the response model.
    """
    engine: AsyncEngine = request.app.state.engine
    isReady = await ProbeDatabase(engine, settings.HEALTH_READY_TIMEOUT_SECONDS)
    if not isReady:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthStatusResponse(status=STATUS_UNAVAILABLE)
    return HealthStatusResponse(status=STATUS_READY)
