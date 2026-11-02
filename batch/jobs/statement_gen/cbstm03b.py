"""Replaces: CBSTM03B (file access) and CREASTMT steps DELDEF01, STEP010, STEP020 (the TRXFL
work file); copybooks COSTM01, CVACT03Y, CUSTREC, CVACT01Y.

The TRXFL sort and card-first re-key, SORT FIELDS=(263,16,CH,A,1,16,CH,A), is one ordered query
in EBCDIC key order, so no work file or table stands in for TRXFL. Every read runs on the
caller's synchronous connection: CBSTM03B's OPEN and CLOSE belong to the caller, a keyed read
that finds nothing (status 23) returns None, and a database error propagates for the job step
to map to exit 16. Nothing here writes or commits.
"""

from app.models import Account, Card, CardXref, Customer, Transaction
from sqlalchemy import select
from sqlalchemy.engine import Connection, RowMapping

# Card-number order comes from the stored, indexed cards.cards.card_num_sort column rather than
# ebcdic_sort_key() computed per row, because every key-ordered read uses the generated sort
# columns. The foreign keys on transactions.card_num and card_xref.card_num guarantee the joined
# card row, so the inner joins below drop no record.


def ReadStatementTransactions(connection: Connection) -> list[RowMapping]:
    """READ TRNX-FILE: every transaction in TRXFL key order, card number then TRAN-ID."""
    statement = (
        select(
            Transaction.card_num,
            Transaction.tran_id,
            Transaction.tran_type_cd,
            Transaction.tran_cat_cd,
            Transaction.tran_source,
            Transaction.tran_desc,
            Transaction.tran_amt,
        )
        .join(Card, Card.card_num == Transaction.card_num)
        .order_by(Card.card_num_sort, Transaction.tran_id_sort)
    )
    return list(connection.execute(statement).mappings())


def ReadXrefsInCardOrder(connection: Connection) -> list[RowMapping]:
    """READ XREF-FILE: every cross-reference record in CARDXREF prime-key order."""
    statement = (
        select(CardXref.card_num, CardXref.cust_id, CardXref.acct_id)
        .join(Card, Card.card_num == CardXref.card_num)
        .order_by(Card.card_num_sort)
    )
    return list(connection.execute(statement).mappings())


def ReadCustomer(connection: Connection, custId: str) -> RowMapping | None:
    """READ CUST-FILE KEY: the customer whose CUST-ID is custId, or None when absent."""
    statement = select(
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
    ).where(Customer.cust_id == custId)
    return connection.execute(statement).mappings().first()


def ReadAccount(connection: Connection, acctId: str) -> RowMapping | None:
    """READ ACCT-FILE KEY: the account whose ACCT-ID is acctId, or None when absent."""
    statement = select(Account.acct_id, Account.curr_balance).where(Account.acct_id == acctId)
    return connection.execute(statement).mappings().first()
