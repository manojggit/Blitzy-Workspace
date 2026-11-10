"""Replaces: the CICS transaction table of csd/CARDDEMO.CSD (DEFINE TRANSACTION entries).

The single dispatch point of the online API. ``ROUTER`` aggregates the eight cluster routers
that serve the 17 base transactions, in menu order:

    auth: CC00 (COSGN00C).
    menu: CM00 (COMEN01C) and CA00 (COADM01C).
    accounts: CAVW (COACTVWC) and CAUP (COACTUPC).
    cards: CCLI (COCRDLIC), CCDL (COCRDSLC) and CCUP (COCRDUPC).
    transactions: CT00 (COTRN00C), CT01 (COTRN01C) and CT02 (COTRN02C).
    reports: CR00 (CORPT00C).
    billpay: CB00 (COBIL00C).
    admin_users: CU00-CU03 (COUSR00C-COUSR03C).

CDV1 (COCRDSEC) has no program source, so it has no route.

``app.main`` mounts ``ROUTER`` with ``prefix=API_V1_PREFIX`` ("/api/v1"), so ``ROUTER`` itself
carries no prefix, tags or dependencies: each cluster router declares its own resource prefix
and tag, and the OpenAPI tags stay exactly those ``app.main`` lists. The health probes are not
included here, because they are served outside /api/v1.
"""

from fastapi import APIRouter

from app.api.v1 import accounts, admin_users, auth, billpay, cards, menu, reports, transactions

ROUTER = APIRouter()

# Every cluster module names its router ROUTER, so the modules are imported, not the names.
CLUSTER_ROUTERS = (
    auth.ROUTER,
    menu.ROUTER,
    accounts.ROUTER,
    cards.ROUTER,
    transactions.ROUTER,
    reports.ROUTER,
    billpay.ROUTER,
    admin_users.ROUTER,
)

for clusterRouter in CLUSTER_ROUTERS:
    ROUTER.include_router(clusterRouter)
