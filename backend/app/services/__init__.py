"""CardDemo service layer replacing the CICS online programs and the CBTRN02C/CBTRN03C rules.

Each program-port module below ports one legacy COBOL program and is named after it in lower
case. login_throttle, menu_options and report_export are supplementary modules, not program
ports. The eight cluster packages and their modules:
    auth: cosgn00c (COSGN00C, CC00 sign-on) and login_throttle (the sign-on lockout).
    menu: comen01c (COMEN01C, CM00 main menu), coadm01c (COADM01C, CA00 admin menu) and
        menu_options (the COMEN02Y and COADM02Y option tables).
    accounts: coactvwc (COACTVWC, CAVW) and coactupc (COACTUPC, CAUP).
    cards: cocrdlic (COCRDLIC, CCLI), cocrdslc (COCRDSLC, CCDL) and cocrdupc (COCRDUPC, CCUP).
    transactions: cotrn00c (COTRN00C, CT00), cotrn01c (COTRN01C, CT01), cotrn02c (COTRN02C,
        CT02) and cbtrn02c (the CBTRN02C posting rules run by the POSTTRAN batch job).
    reports: corpt00c (CORPT00C, CR00), cbtrn03c (the CBTRN03C report line builder) and
        report_export (the CSV and PDF renderings of that report).
    billpay: cobil00c (COBIL00C, CB00).
    users: cousr00c, cousr01c, cousr02c and cousr03c (COUSR00C-COUSR03C, CU00-CU03).

Every module listed above opens with a "Replaces:" header naming the legacy artefacts it
replaces, such as its program, transaction ID or batch job, BMS map, copybooks and print file,
or stating that it replaces none.

This package re-exports nothing, and importing it reads no settings and opens no database
connection, so batch can import the shared rules directly, for example:

    from app.services.transactions.cbtrn02c import ValidateDailyTransaction
"""
