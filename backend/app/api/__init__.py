"""HTTP layer of the CardDemo backend.

Replaces the CICS transaction dispatch defined in csd/CARDDEMO.CSD: each of the 17 base
transactions (CC00, CM00, CA00, CAVW, CAUP, CCLI, CCDL, CCUP, CT00, CT01, CT02, CR00, CB00
and CU00-CU03) becomes a REST endpoint group.

Contents:
    health: the liveness and readiness probes, served outside /api/v1.
    v1: the eight versioned cluster routers under /api/v1: auth, menu, accounts, cards,
        transactions, reports, billpay and admin_users.

Handlers are thin and delegate to app.services. This package re-exports nothing.
"""
