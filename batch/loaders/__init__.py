"""Dataset load jobs replacing the IDCAMS DELETE/DEFINE/REPRO jobs and the DALYTRAN precondition.

Replaces: ACCTFILE, CARDFILE, CUSTFILE, XREFFILE, TRANFILE, TRANTYPE, TRANCATG, DISCGRP,
TCATBALF and DUSRSECJ (the IDCAMS load jobs), and the DALYTRAN.PS precondition of POSTTRAN.

Submodules: replace_loader (the shared atomic replace-load routine) and one loader per job:
acctfile, cardfile, custfile, xreffile, tranfile, trantype, trancatg, discgrp, tcatbalf,
security_init (DUSRSECJ) and dalytran (stages the DALYTRAN daily file).
"""
