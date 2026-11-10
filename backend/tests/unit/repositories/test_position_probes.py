"""Unit tests for the STARTBR GTEQ position probes of the CT00 and CU00 backward browses.

app.repositories.transaction_repository ports the COTRN00C browses of TRANSACT, and
app.repositories.user_repository the COUSR00C browses of USRSEC. Before each F7 page,
PROCESS-PAGE-BACKWARD positions with STARTBR GTEQ, which the port reproduces as a probe for the
first key at or after the start key. These tests pin that each probe, given a start key or
LOW-VALUES (None), reads in ascending order of its generated EBCDIC sort column and stops after
one row, so PostgreSQL serves it from that column's index instead of scanning the table up to
the key.

The statements come straight from the modules' statement builders and are compiled for the
PostgreSQL dialect with their values inlined, so no database is needed.
"""

from collections.abc import Callable
from typing import Any, NamedTuple

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql

from app.repositories.transaction_repository import StartPositionStatement
from app.repositories.user_repository import PositionSelect

# An invented TRAN-ID X(16) and SEC-USR-ID X(08); the compiled probes never reach a database.
TRAN_START_KEY = "0000000000000001"
USER_START_KEY = "USERA001"
# The clauses each probe ends with: ascending sort-column order, then a single row.
TRANSACTION_PROBE_TAIL = "ORDER BY transactions.transactions.tran_id_sort ASC LIMIT 1"
USER_PROBE_TAIL = "ORDER BY security.users.user_id_sort ASC LIMIT 1"


class ProbeCase(NamedTuple):
    """One position probe builder, the start key it is given and the clauses it must end with."""

    buildProbe: Callable[[str | None], Select[Any]]
    startKey: str | None
    expectedTail: str


PROBE_CASES = {
    "ct00-start-key": ProbeCase(StartPositionStatement, TRAN_START_KEY, TRANSACTION_PROBE_TAIL),
    "ct00-low-values": ProbeCase(StartPositionStatement, None, TRANSACTION_PROBE_TAIL),
    "cu00-start-key": ProbeCase(PositionSelect, USER_START_KEY, USER_PROBE_TAIL),
    "cu00-low-values": ProbeCase(PositionSelect, None, USER_PROBE_TAIL),
}


def CompiledSql(statement):
    """Return the PostgreSQL text of ``statement`` with its values inlined, on one line."""
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    return " ".join(str(compiled).split())


@pytest.mark.parametrize("probeCase", list(PROBE_CASES.values()), ids=list(PROBE_CASES))
def test_position_probe_reads_one_row_in_sort_column_order(probeCase):
    probeSql = CompiledSql(probeCase.buildProbe(probeCase.startKey))
    assert probeSql.endswith(probeCase.expectedTail)
