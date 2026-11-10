"""Replaces: CBSTM03B (file access) and CREASTMT steps DELDEF01, STEP010, STEP020 (the TRXFL
work file); copybooks COSTM01, CVACT03Y, CUSTREC, CVACT01Y.

The TRXFL sort and card-first re-key, SORT FIELDS=(263,16,CH,A,1,16,CH,A), is one ordered query
in EBCDIC key order, so no work file or table stands in for TRXFL. It is bounded to
TRANSACTION_TABLE_READ_LIMIT records, one past the 51-card by 10-transaction table, so
oversized history is refused without being fetched. The XREFFILE browse and the keyed CUSTFILE
and ACCTFILE reads of each cross-reference are one outer-joined query, streamed through a
server-side cursor; a keyed read that finds nothing (status 23) gives None. Every read runs on
the caller's synchronous connection: CBSTM03B's OPEN and CLOSE belong to the caller, and a
database error propagates for the job step to map to exit 16. Nothing here writes or commits.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from app.models import Account, Card, CardXref, Customer, Transaction
from sqlalchemy import Label, Select, select
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.orm import InstrumentedAttribute

from batch.jobs.statement_gen.cbstm03a import TRANSACTION_TABLE_READ_LIMIT, RecordMapping

# Card-number order comes from the stored, indexed cards.cards.card_num_sort column rather than
# ebcdic_sort_key() computed per row, because every key-ordered read uses the generated sort
# columns. The foreign keys on transactions.card_num and card_xref.card_num guarantee the joined
# card row, so the inner joins below drop no record. Customers and accounts are outer-joined, so
# a keyed read that finds nothing still yields its cross-reference, for CBSTM03A to abend on.

# Rows per fetch from the server-side cursor of the cross-reference read.
XREF_FETCH_BATCH_SIZE = 100

# The columns of each record the cross-reference read returns, and the label prefix that keeps
# the three tables' names apart in the joined row. Each group starts with its primary key.
XREF_PREFIX = "xref_"
XREF_COLUMNS = (CardXref.card_num, CardXref.cust_id, CardXref.acct_id)
CUSTOMER_PREFIX = "customer_"
CUSTOMER_COLUMNS = (
    Customer.cust_id,
    Customer.first_name,
    Customer.middle_name,
    Customer.last_name,
    Customer.addr_line_1,
    Customer.addr_line_2,
    Customer.addr_line_3,
    Customer.addr_state_cd,
    Customer.addr_country_cd,
    Customer.addr_zip,
    Customer.fico_credit_score,
)
ACCOUNT_PREFIX = "account_"
ACCOUNT_COLUMNS = (Account.acct_id, Account.curr_balance)

type ColumnGroup = tuple[InstrumentedAttribute[Any], ...]


@dataclass(frozen=True)
class StatementSource:
    """One CARD-XREF-RECORD with the CUSTOMER-RECORD and ACCOUNT-RECORD its keys name.

    Each record maps column names to values. customer or account is None when its keyed read
    finds no record (CBSTM03B status 23), which cbstm03a.AssembleStatementData refuses.
    """

    xref: RecordMapping
    customer: RecordMapping | None
    account: RecordMapping | None


def ReadStatementTransactions(connection: Connection) -> list[RowMapping]:
    """READ TRNX-FILE: the transactions in TRXFL key order, card number then TRAN-ID.

    Reads the four columns CBSTM03A uses, and at most TRANSACTION_TABLE_READ_LIMIT records, so
    history beyond the 51 by 10 table is refused without being fetched.
    """
    statement = (
        select(
            Transaction.card_num,
            Transaction.tran_id,
            Transaction.tran_desc,
            Transaction.tran_amt,
        )
        .join(Card, Card.card_num == Transaction.card_num)
        .order_by(Card.card_num_sort, Transaction.tran_id_sort)
        .limit(TRANSACTION_TABLE_READ_LIMIT)
    )
    return list(connection.execute(statement).mappings())


def ReadXrefsInCardOrder(connection: Connection) -> Iterator[StatementSource]:
    """READ XREF-FILE, then READ CUST-FILE and READ ACCT-FILE KEY, for every cross-reference.

    Yields one StatementSource per cross-reference in CARDXREF prime-key order, from one query
    whose rows stream through a server-side cursor, XREF_FETCH_BATCH_SIZE rows per fetch.
    Closing the generator before its end closes the cursor.
    """
    executionOptions = {"yield_per": XREF_FETCH_BATCH_SIZE}
    with connection.execute(BuildXrefStatement(), execution_options=executionOptions) as result:
        for row in result.mappings():
            yield SplitStatementSource(row)


def BuildXrefStatement() -> Select[Any]:
    """Return the cross-reference browse in card order, outer-joined to its keyed records."""
    return (
        select(
            *LabelColumns(XREF_PREFIX, XREF_COLUMNS),
            *LabelColumns(CUSTOMER_PREFIX, CUSTOMER_COLUMNS),
            *LabelColumns(ACCOUNT_PREFIX, ACCOUNT_COLUMNS),
        )
        .select_from(CardXref)
        .join(Card, Card.card_num == CardXref.card_num)
        .outerjoin(Customer, Customer.cust_id == CardXref.cust_id)
        .outerjoin(Account, Account.acct_id == CardXref.acct_id)
        .order_by(Card.card_num_sort)
    )


def LabelColumns(prefix: str, columns: ColumnGroup) -> list[Label[Any]]:
    """Label each column with prefix and its column name, so joined names never collide."""
    return [column.label(prefix + column.key) for column in columns]


def SplitStatementSource(row: RowMapping) -> StatementSource:
    """Split one joined row into its cross-reference, customer and account records."""
    return StatementSource(
        xref=RecordColumns(row, XREF_PREFIX, XREF_COLUMNS),
        customer=JoinedRecordColumns(row, CUSTOMER_PREFIX, CUSTOMER_COLUMNS),
        account=JoinedRecordColumns(row, ACCOUNT_PREFIX, ACCOUNT_COLUMNS),
    )


def RecordColumns(row: RowMapping, prefix: str, columns: ColumnGroup) -> dict[str, object]:
    """Return one table's columns of a joined row, keyed by column name."""
    return {column.key: row[prefix + column.key] for column in columns}


def JoinedRecordColumns(
    row: RowMapping, prefix: str, columns: ColumnGroup
) -> dict[str, object] | None:
    """Return an outer-joined table's columns of a row, or None when the join found no record.

    No record leaves every one of its columns NULL, its primary key, the first column, included.
    """
    if row[prefix + columns[0].key] is None:
        return None
    return RecordColumns(row, prefix, columns)
