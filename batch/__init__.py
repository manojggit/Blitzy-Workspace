"""CardDemo batch package replacing the base-application JCL jobs and CA-7/Control-M chains.

Subpackages: ``orchestration`` (the job chain, run ledger, generation archives, batch window,
clock and job context), ``loaders`` (the dataset load jobs), ``jobs`` (the processing jobs) and
``tests``. Modules: ``cli`` (the command-line interface), ``config`` (batch settings) and ``db``
(the synchronous database engine). The CLI entry point is ``batch.cli:APP``, installed as the
``carddemo-batch`` console script.

Importing this package reads no settings and opens no database connection: each command
configures itself when it runs.

Replaces: scheduler definitions CardDemo.ca7 (CA-7) and CardDemo.controlm (Control-M).
"""
