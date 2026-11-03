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

``LosslessKeyRoute`` is the route class of the routers whose path carries a raw screen key
(accounts, cards, transactions, admin_users). A 3270 key may hold ``/``, which the client
sends as ``%2F``; the server decodes the ASGI ``path`` before Starlette matches it, so the
default ``[^/]+`` parameter would see two segments and FastAPI would answer 404
``{"detail": "Not Found"}`` instead of the program's key edit or lookup.
"""

from typing import Any
from urllib.parse import unquote

from fastapi.routing import APIRoute
from starlette.routing import Match
from starlette.types import Scope

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


def EscapeSegment(segment: str) -> str:
    """Return a decoded path segment with only ``%`` and ``/`` percent-encoded again.

    ``%`` is escaped first, so the ``%2F`` written for ``/`` is not escaped twice, and
    ``unquote`` of the result is ``segment`` exactly.
    """
    return segment.replace("%", "%25").replace("/", "%2F")


def LosslessRoutePath(scope: Scope) -> str | None:
    """Return the request path decoded segment by segment, keeping ``%2F`` inside a segment.

    The segments are split on the literal ``/`` of ``raw_path``, the request target as sent,
    so a key's encoded slash stays inside its segment. None when there is no ``raw_path``,
    when it is not ASCII, or when the result does not decode to the ASGI ``path`` or start
    with its ``root_path``: Starlette's trailing-slash probe, for one, rewrites ``path``
    only. The caller then matches the decoded ``path`` as Starlette does.
    """
    rawPath = scope.get("raw_path")
    if not rawPath:
        return None
    try:
        rawTarget = rawPath.decode("ascii").partition("?")[0]
    except UnicodeDecodeError:
        return None
    losslessPath = "/".join(EscapeSegment(unquote(segment)) for segment in rawTarget.split("/"))
    isSameRequest = unquote(losslessPath) == scope["path"]
    if not isSameRequest or not losslessPath.startswith(scope.get("root_path", "")):
        return None
    return losslessPath


class LosslessKeyRoute(APIRoute):
    """An APIRoute whose path parameters receive the screen key exactly as the client keyed it.

    It matches the path built by ``LosslessRoutePath``, where a ``/`` inside a key is still
    ``%2F``, so ``/accounts/A%2Fedit`` is the view of key ``A/edit`` and never the edit fetch
    of ``A``. Only this route's own parameters are then decoded. Path, parameter names,
    precedence and the OpenAPI document are those of ``APIRoute``; a request that has no
    lossless path is matched exactly as ``APIRoute`` matches it.
    """

    # Starlette's router calls this exact name, so it stays lower-case: a framework-mandated
    # exception to the MK_Modernization Rule's PascalCase method naming.
    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        """Return APIRoute's match of the lossless path, own params decoded, else of ``path``."""
        losslessPath = LosslessRoutePath(scope)
        if losslessPath is None:
            return super().matches(scope)
        match, childScope = super().matches({**scope, "path": losslessPath})
        if match is not Match.NONE:
            childScope["path_params"] = self.DecodeOwnParams(childScope["path_params"])
        return match, childScope

    def DecodeOwnParams(self, pathParams: dict[str, Any]) -> dict[str, Any]:
        """Return ``pathParams`` with this route's text parameters percent-decoded once."""
        return {
            name: unquote(value)
            if name in self.param_convertors and isinstance(value, str)
            else value
            for name, value in pathParams.items()
        }
