"""Read the legacy CardDemo seed images (data/EBCDIC/*, data/ASCII/*) in place.

Replaces the IDCAMS REPRO steps of ACCTFILE, CARDFILE, CUSTFILE, XREFFILE, TRANFILE, TRANTYPE,
TRANCATG, DISCGRP, TCATBALF and DUSRSECJ, and stages the DALYTRAN daily file.
Submodules: image_reader (ReadImage) and sample_seed (LoadSampleData, run by revision 0002).
"""
