"""Version 1 of the CardDemo REST API, served under /api/v1.

Replaces the CICS transaction dispatch defined in csd/CARDDEMO.CSD for the 17 base
transactions: CC00, CM00, CA00, CAVW, CAUP, CCLI, CCDL, CCUP, CT00, CT01, CT02, CR00, CB00,
CU00, CU01, CU02 and CU03. CDV1 (COCRDSEC) has no program source and is out of scope.

Modules:
    auth: CC00 (COSGN00C) sign-on, sign-off and the current user.
    menu: CM00 (COMEN01C) and CA00 (COADM01C) menus and option routing.
    accounts: CAVW (COACTVWC) account view and CAUP (COACTUPC) account update.
    cards: CCLI (COCRDLIC) card list, CCDL (COCRDSLC) card view, CCUP (COCRDUPC) card update.
    transactions: CT00 (COTRN00C) list, CT01 (COTRN01C) view, CT02 (COTRN02C) add.
    reports: CR00 (CORPT00C) transaction report and its CSV and PDF exports.
    billpay: CB00 (COBIL00C) bill payment.
    admin_users: CU00-CU03 (COUSR00C-COUSR03C) user list, add, update and delete.
    router: aggregates the eight routers into the one router app.main mounts.

``LegacyErrorResponses`` declares the legacy error body, ``LegacyErrorResponse``, for each
non-2xx status a route lists in its ``responses=``, so the OpenAPI document describes every
legacy outcome. FastAPI declares 422 itself. This package imports no router, settings or
service, because importing any router module loads this file first.
"""

from typing import Any

from app.schemas.common import LegacyErrorResponse

ERROR_DESCRIPTIONS: dict[int, str] = {
    400: "Legacy field-edit failure: the message, the failing fields and the cursor field.",
    401: "Not signed on, session expired, or sign-on failure.",
    403: "Role refused, or the Origin header is missing or not allowed.",
    404: "Record not found.",
    409: "Record changed since it was fetched, or duplicate key.",
    423: "Record locked by another user.",
    429: "Too many failed sign-on attempts.",
    503: "File error, or the batch window is closed.",
}


def LegacyErrorResponses(*statusCodes: int) -> dict[int | str, dict[str, Any]]:
    """Return the ``responses=`` entries declaring the legacy error body for each status."""
    return {
        status: {"model": LegacyErrorResponse, "description": ERROR_DESCRIPTIONS[status]}
        for status in statusCodes
    }
