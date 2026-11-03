"""Replaces: CORPT00C (CR00), BMS CORPT00; CBTRN03C report lines (copybook CVTRA07Y);
proc TRANREPT (superseded TDQ job).

The CR00 transaction report router, mounted at ``/api/v1/reports``. CORPT00C wrote inline JCL
to TDQ ``JOBS``, which ran ``proc/TRANREPT.prc`` and then CBTRN03C to print the TRANREPT file.
Here ``GET /transactions`` returns the CBTRN03C report lines as JSON for the screen, and
``GET /transactions/export`` streams the same lines as a CSV or PDF attachment, built in memory:
the backend writes no file.

CR00 serves regular users (any type other than exactly ``A``) through ``GetRegularContext``,
which refuses an admin with 403 and an empty message. CR00 is deliberately not gated by the batch
window, although the report reads TRANSACT: CORPT00C only submitted a TDQ job and never opened
TRANSACT, so the report reads the committed transaction rows without entering the window.
TRANBKP archives and empties TRANSACT in one transaction, so a report never sees a half-archived
table. ``corpt00c`` owns every edit and literal and raises each legacy outcome as
``LegacyMessageError``, serialised by ``app.core.errors``; query text breaking its BMS LENGTH or
the code-page-037 repertoire is a 422. PF3 (back to the main menu) is frontend navigation and has
no route.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.api.v1 import LegacyErrorResponses
from app.core.dependencies import GetRegularContext, OnlineContext
from app.schemas.reports import ReportExportQuery, ReportQuery, TransactionReportResponse
from app.services.reports import corpt00c

ROUTER = APIRouter(prefix="/reports", tags=["reports"])

REGULAR_CONTEXT = Annotated[OnlineContext, Depends(GetRegularContext)]

CONTENT_DISPOSITION_HEADER = "Content-Disposition"
# The file name comes from corpt00c, built from the validated report dates only, so no keyed
# text is reflected into the header.
ATTACHMENT_DISPOSITION_TEMPLATE = 'attachment; filename="{fileName}"'

EXPORT_SUCCESS_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "The report as a CSV or PDF attachment.",
        "content": {
            "text/csv": {"schema": {"type": "string"}},
            "application/pdf": {"schema": {"type": "string", "format": "binary"}},
        },
    },
}

RUN_REPORT_DESCRIPTION = (
    "Runs CORPT00C's ENTER edits in first-failure order. reportType selects monthly (the current"
    " month), yearly (the current year) or custom (the entered start and end date parts), and"
    " confirm Y builds the report: the response carries the CBTRN03C detail, account-total,"
    " page-total and grand-total lines. A blank confirm returns the confirmation prompt and N"
    " clears the screen, both without lines."
)
EXPORT_REPORT_DESCRIPTION = (
    "Runs the same edits as the on-screen report and streams the same lines from memory, as CSV"
    " (one row per line) or PDF (the 133-column print lines), named"
    " TRANREPT_<start>_<end>.<format>. Only confirm Y downloads; a blank or N confirm is a 400"
    " with the prompt or cleared-screen message. The session cookie is sent on the same-site"
    " download."
)


@ROUTER.get(
    "/transactions",
    response_model=TransactionReportResponse,
    summary="CR00 / CORPT00C: run transaction report",
    description=RUN_REPORT_DESCRIPTION,
    responses=LegacyErrorResponses(400, 401, 403, 503),
)
async def RunTransactionReport(
    query: Annotated[ReportQuery, Query()], context: REGULAR_CONTEXT
) -> TransactionReportResponse:
    """Process ENTER on CORPT00 (CORPT00C PROCESS-ENTER-KEY) and return the report screen."""
    return await corpt00c.RunTransactionReport(context, query)


@ROUTER.get(
    "/transactions/export",
    response_class=StreamingResponse,
    response_model=None,
    summary="CR00 / CORPT00C: export transaction report (CSV or PDF)",
    description=EXPORT_REPORT_DESCRIPTION,
    responses={**EXPORT_SUCCESS_RESPONSE, **LegacyErrorResponses(400, 401, 403, 503)},
)
async def ExportTransactionReport(
    query: Annotated[ReportExportQuery, Query()], context: REGULAR_CONTEXT
) -> StreamingResponse:
    """Stream the confirmed CR00 report as the attachment format the query names."""
    content, mediaType, fileName = await corpt00c.ExportTransactionReport(
        context, query, query.export_format
    )
    return StreamingResponse(
        iter([content]),
        media_type=mediaType,
        headers={
            CONTENT_DISPOSITION_HEADER: ATTACHMENT_DISPOSITION_TEMPLATE.format(fileName=fileName)
        },
    )
