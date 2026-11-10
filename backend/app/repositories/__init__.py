"""Online data access for CardDemo: one repository per VSAM file and access path.

Replaces: CICS file control over the CardDemo VSAM files of csd/CARDDEMO.CSD (USRSEC, ACCTDAT,
CUSTDAT, CARDDAT, CCXREF, CXACAIX and TRANSACT) and the TRANTYPE and TRANCATG reference files.
Keys and alternate-index paths follow catlg/LISTCAT.txt. CARDAIX, also defined in the CSD, has
no reachable online access path and therefore no repository.

Submodules, with the file each replaces and its key:
    user_repository: USRSEC, user ID (8 bytes).
    account_repository: ACCTDAT, account ID (11 bytes).
    customer_repository: CUSTDAT, customer ID (9 bytes).
    card_repository: CARDDAT, card number (16 bytes).
    card_xref_repository: CCXREF, card number (16 bytes), and its CXACAIX path, account ID
        (11 bytes at offset 25).
    transaction_repository: TRANSACT, transaction ID (16 bytes).
    reference_repository: TRANTYPE, type code (2 bytes), and TRANCATG, type and category
        (6 bytes).

Only app/services/** consumes these repositories. Batch never imports them: its jobs issue
sync SQLAlchemy Core statements against the same tables.

This package re-exports nothing. Import each repository from its submodule.
"""
