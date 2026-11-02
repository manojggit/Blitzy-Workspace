"""Independent legacy oracle and chain-parity reconciliation for the CardDemo batch jobs.

The repository holds no legacy batch outputs, so this package derives the expected outputs from
a stdlib-only re-reading of the COBOL rules: CBTRN02C (POSTTRAN), CBACT04C (INTCALC), the
COMBTRAN merge, CBSTM03A and CBSTM03B (CREASTMT) and CBTRN03C (TRANREPT). Its modules import
nothing from the ``app`` or ``batch`` production code, so a defect shared by production code
cannot validate itself. Within the package they use relative imports only, such as
``from . import legacy_oracle``.

Submodules:
    legacy_oracle: the independent reading of the cited COBOL paragraphs and copybook offsets.
    compare: byte-for-byte file comparison and plain-SQL table comparison.
    build_expected: writes ``batch/tests/fixtures/expected``; run it with
        ``python -m batch.tests.reconciliation.build_expected``.
    test_reconciliation: the chain-parity test, which runs the ``carddemo-batch`` subprocess.
"""
