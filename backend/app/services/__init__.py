"""CardDemo service layer replacing the CICS online programs and the CBTRN02C/CBTRN03C rules.

Each module ports one legacy COBOL program, named after it in lower case. The eight cluster
packages and the programs they replace:
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

Every service module opens with its own "Replaces: ..." header naming its program,
transaction ID, BMS map and copybooks.

This package re-exports nothing, and importing it reads no settings and opens no database
connection, so batch can import the shared rules directly, for example:

    from app.services.transactions.cbtrn02c import ValidateDailyTransaction
"""
