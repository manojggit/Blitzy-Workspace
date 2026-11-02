"""Transactions service cluster.

Replaces: COTRN00C (CT00), COTRN01C (CT01), COTRN02C (CT02), CBTRN02C (POSTTRAN posting rules).

Submodules: cotrn00c (transaction list), cotrn01c (transaction view), cotrn02c (transaction
add) and cbtrn02c (the daily-transaction posting rules that batch.jobs.posttran runs).
Consumers import each submodule by its full path, for example
``from app.services.transactions.cbtrn02c import ValidateDailyTransaction``; this package
re-exports nothing and imports nothing, so loading it reads no settings and opens no database
connection.
"""
