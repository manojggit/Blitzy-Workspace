"""Accounts service cluster: CAVW account view and CAUP account update.

Replaces: COACTVWC (CAVW), COACTUPC (CAUP); BMS COACTVW, COACTUP.

Submodules: coactvwc (ViewAccount) and coactupc (FetchAccountForEdit, ProcessAccountUpdate).
Import them by full path, for example
``from app.services.accounts.coactupc import ProcessAccountUpdate``; this package re-exports
nothing, and loading it reads no settings and opens no database connection.
"""
