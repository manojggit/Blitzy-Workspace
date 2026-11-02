"""Cross-cutting infrastructure for the CardDemo backend.

Replaces: the CICS region services of csd/CARDDEMO.CSD, namely the COMMAREA identity
(COCOM01Y CDEMO-USER-ID and CDEMO-USER-TYPE), RACF sign-on, CLOSEFIL/OPENFIL file open and
close, and the RECOVERY(NONE) write durability of the CARDDEMO files; plus the common message
copybooks CSMSG01Y, CSMSG02Y and COTTL01Y.

Submodules:
    paths: repository root, repository-relative path resolution and the backend .env file.
    config: environment settings, failing closed on a blank or short SECRET_KEY.
    database: async engine, request session and the per-record-write WriteUnit.
    errors: the legacy message error contract and its exception handlers.
    messages: legacy message literals shared by more than one ported program.
    security: password hashing, session tokens, card and screen tokens, PAN and SSN masking.
    locks: the batch-window and TRAN-ID allocation advisory locks.
    dependencies: current user, admin and regular role guards, batch-window release.
    middleware: the Origin guard on cookie-authenticated writes.
    logging: stdout logging with card numbers masked.

The package body is empty on purpose: loading it reads no settings, configures no logging and
creates no engine, so batch, Alembic and app.seed use paths, security, locks, logging,
messages and errors without a SECRET_KEY.
"""
