"""Replaces: CORPT00C (CR00), BMS CORPT00; CBTRN03C report lines (copybook CVTRA07Y);
proc TRANREPT (superseded TDQ job).

The CR00 transaction report router, mounted at ``/api/v1/reports``. CORPT00C wrote inline JCL
to TDQ ``JOBS``, which ran ``proc/TRANREPT.prc`` and then CBTRN03C to print the TRANREPT file.
Here ``GET /transactions`` returns the CBTRN03C report lines as JSON for the screen, and
``GET /transactions/export`` streams the same lines as a CSV or PDF attachment. A confirmed
report's body is a ``ReportStreamingResponse``: its chunks are sent as ``corpt00c`` builds them
in memory, after the reads that can still change the status, and the backend writes no file.
The screen's JSON is the ``TransactionReportResponse`` its ``response_model`` documents,
written by ``corpt00c`` and sent as it is.

CR00 serves regular users (any type other than exactly ``A``) through ``GetRegularReportContext``,
which refuses an admin with 403 and an empty message. Its session takes no request admission:
``corpt00c`` admits the request only for its reads and its build, so a report waiting for a
report slot, or a client still reading a built report, never holds up other online requests.
CR00 is deliberately not gated by the batch window, although the report reads TRANSACT:
CORPT00C only submitted a TDQ job and never opened TRANSACT, so the report reads the committed
transaction rows without entering the window. TRANBKP archives and empties TRANSACT in one
transaction, and the report reads one snapshot, so it never sees a half-archived table.
``corpt00c`` owns every edit and literal and raises each legacy outcome as
``LegacyMessageError``, serialised by ``app.core.errors``; query text breaking its BMS LENGTH or
the code-page-037 repertoire is a 422. PF3 (back to the main menu) is frontend navigation and has
no route.
"""

import functools
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.api.v1 import LegacyErrorResponses
from app.core.dependencies import GetRegularReportContext, OnlineContext
from app.schemas.reports import ReportExportQuery, ReportQuery, TransactionReportResponse
from app.services.reports import corpt00c
from app.services.reports.report_export import JSON_MEDIA_TYPE

ROUTER = APIRouter(prefix="/reports", tags=["reports"])

REGULAR_REPORT_CONTEXT = Annotated[OnlineContext, Depends(GetRegularReportContext)]

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


class ReportStreamingResponse(StreamingResponse):
    """The body of a confirmed CR00 report, sent while ``corpt00c`` builds it.

    ``corpt00c.StreamReport`` first reads and checks the report, before any byte is sent, so a
    503, 499 or abend it raises still reaches the exception handlers, which FastAPI applies
    around the response; it then builds the report while ``StreamingResponse`` sends each chunk
    and ends the body once the build has sent the last one. Each message is sent through
    ``corpt00c.SendWithinTimeout``, so a client that stops reading stops the report.
    """

    def __init__(
        self,
        reportStream: corpt00c.ReportStream,
        mediaType: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(reportStream.chunks, media_type=mediaType, headers=headers)
        self.reportStream = reportStream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Read and check the report, then send its chunks while it is built."""
        sendWithinTimeout = functools.partial(corpt00c.SendWithinTimeout, send)
        sendChunks = functools.partial(super().__call__, scope, receive, sendWithinTimeout)
        await corpt00c.StreamReport(self.reportStream, sendChunks)


@ROUTER.get(
    "/transactions",
    response_model=TransactionReportResponse,
    summary="CR00 / CORPT00C: run transaction report",
    description=RUN_REPORT_DESCRIPTION,
    responses=LegacyErrorResponses(400, 401, 403, 503),
)
async def RunTransactionReport(
    query: Annotated[ReportQuery, Query()], context: REGULAR_REPORT_CONTEXT
) -> Response:
    """Process ENTER on CORPT00 (CORPT00C PROCESS-ENTER-KEY) and return the report screen.

    corpt00c writes the ``TransactionReportResponse`` JSON a line at a time, as FastAPI would
    serialise the model, so the body is sent as written and no model holding every line exists:
    a prompt or cleared screen at once, a confirmed report as its chunks are built.
    """
    screen = corpt00c.PrepareTransactionReportJson(context, query)
    if isinstance(screen, corpt00c.ReportStream):
        return ReportStreamingResponse(screen, JSON_MEDIA_TYPE)
    return Response(content=screen, media_type=JSON_MEDIA_TYPE)


@ROUTER.get(
    "/transactions/export",
    response_class=StreamingResponse,
    response_model=None,
    summary="CR00 / CORPT00C: export transaction report (CSV or PDF)",
    description=EXPORT_REPORT_DESCRIPTION,
    responses={**EXPORT_SUCCESS_RESPONSE, **LegacyErrorResponses(400, 401, 403, 503)},
)
async def ExportTransactionReport(
    query: Annotated[ReportExportQuery, Query()], context: REGULAR_REPORT_CONTEXT
) -> StreamingResponse:
    """Stream the confirmed CR00 report as the attachment format the query names."""
    reportExport = corpt00c.ExportTransactionReport(context, query, query.export_format)
    disposition = ATTACHMENT_DISPOSITION_TEMPLATE.format(fileName=reportExport.fileName)
    return ReportStreamingResponse(
        reportExport.reportStream,
        reportExport.mediaType,
        {CONTENT_DISPOSITION_HEADER: disposition},
    )
