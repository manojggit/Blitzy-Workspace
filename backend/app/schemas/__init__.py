"""Pydantic v2 request and response models of the CardDemo /api/v1 API.

The models cover the 17 CardDemo online transactions: CC00, CM00, CA00, CAVW, CAUP, CCLI,
CCDL, CCUP, CT00, CT01, CT02, CR00, CB00 and CU00-CU03. They replace the BMS symbolic maps
in cpy-bms/*.CPY and the COMMAREA keys of cpy/COCOM01Y.cpy.

Submodules:
    common: the shared screen-text type, enums and the error body.
    auth, menu, accounts, cards, transactions, reports, billpay, users: one per cluster.

This package re-exports nothing. Import each model from its submodule, for example:

    from app.schemas.accounts import AccountScreen

frontend/src/lib/api/*.ts mirrors these models field for field, with snake_case JSON keys.
"""
