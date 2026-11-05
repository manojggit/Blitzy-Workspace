"""Batch job tests, the reconciliation oracle and expected-output fixtures for carddemo-batch.

Holds one suite per loader and processing job plus the chain, run-ledger, generation-archive,
batch-window and CLI suites. The ``reconciliation`` subpackage is an oracle independent of the
production code, and ``fixtures/expected`` holds the outputs it derives. Run the suites from the
repository root with ``.venv/bin/pytest batch/tests``.
"""
