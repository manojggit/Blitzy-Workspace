"""User administration services for the CardDemo admin menu (CU00-CU03).

Replaces: COUSR00C (CU00), COUSR01C (CU01), COUSR02C (CU02) and COUSR03C (CU03), their BMS
maps COUSR00-COUSR03, and the USRSEC record copybook CSUSR01Y.

Submodules, one per legacy program:
    cousr00c: list USRSEC users 10 rows per page, with search, paging and U/D selection.
    cousr01c: add a regular or admin user.
    cousr02c: fetch and update a user.
    cousr03c: fetch and delete a user.

This package re-exports nothing, and importing it reads no settings and opens no database
connection. Import each service from its submodule, for example app.services.users.cousr00c.
"""
