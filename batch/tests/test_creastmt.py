"""CREASTMT statement generation: the CBSTM03A builder and the CBSTM03B reads.

Pins cbl/CBSTM03A.CBL (STATEMENT-LINES and HTML-LINES, lines 86-223; WS-TRNX-TABLE, lines
226-233; 8100-TRNXFILE-OPEN to 8599-EXIT, lines 758-850) and jcl/CREASTMT.JCL, as ported to
STATEMNT.PS records of 80 columns and STATEMNT.HTML records of exactly 100 columns, with the
51-card by 10-transaction table bound (Technical Specification, Sections 0.6.11 and 0.6.12).
"""

from collections.abc import Iterator
from decimal import Decimal
from unittest import mock

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql

from batch.jobs.statement_gen import cbstm03a, cbstm03b

CARD_NUM = "0000000000000101"
CUST_ID = "000000001"
ACCT_ID = "00000000001"
PS_LINE_WIDTH = 80
HTML_LINE_WIDTH = 100
# 19 STATEMNT.PS and 64 STATEMNT.HTML records per statement, plus 1 and 11 per transaction.
PS_FIXED_LINE_COUNT = 19
HTML_FIXED_LINE_COUNT = 64
HTML_LINES_PER_TRANSACTION = 11
CAPACITY_MESSAGE_PREFIX = "STATEMENT TABLE CAPACITY EXCEEDED: "
FULL_TABLE = (cbstm03a.MAX_TRANSACTIONS_PER_CARD,) * cbstm03a.MAX_STATEMENT_CARDS
TRANSACTION_READ_COLUMNS = ["card_num", "tran_id", "tran_desc", "tran_amt"]
TRANSACTION_READ_ORDER_AND_LIMIT = (
    "ORDER BY cards.cards.card_num_sort, transactions.transactions.tran_id_sort LIMIT 511"
)
XREF_READ_JOINS_AND_ORDER = (
    "FROM xref.card_xref JOIN cards.cards ON cards.cards.card_num = xref.card_xref.card_num"
    " LEFT OUTER JOIN customers.customers ON customers.customers.cust_id = xref.card_xref.cust_id"
    " LEFT OUTER JOIN accounts.accounts ON accounts.accounts.acct_id = xref.card_xref.acct_id"
    " ORDER BY cards.cards.card_num_sort"
)
# The characters the statement template escapes, with their entities, and the field records
# that hold CUST-ADDR-LINE-1 X(50) and ST-TRANDT X(49): ST-LINE2 and the first ST-LINE14 in
# STATEMNT.PS, and <p> lines closed by '  </p>' and '</p>' in STATEMNT.HTML.
HTML_ESCAPED_ENTITIES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
ADDRESS_LINE_WIDTH = 50
TRAN_DETAILS_WIDTH = 49
PS_ADDRESS_LINE_1_INDEX = 2
PS_FIRST_TRANSACTION_INDEX = 16
HTML_PARAGRAPH_OPEN = "<p>"
HTML_ADDRESS_LINE_CLOSE = "  </p>"
HTML_FIELD_LINE_CLOSE = "</p>"
FITTING_ADDRESS_RECORD = "<p>O'NEIL &amp; &lt;SONS&gt;  </p>"


def XrefRecord(cardNum: str = CARD_NUM) -> dict[str, object]:
    """Return a CARD-XREF-RECORD row for one card of the customer and account below."""
    return {"card_num": cardNum, "cust_id": CUST_ID, "acct_id": ACCT_ID}


def CustomerRecord(addressLine1: str = "1 Main St") -> dict[str, object]:
    """Return a CUSTOMER-RECORD row; address line 1 is the free text a test varies."""
    return {
        "cust_id": CUST_ID,
        "first_name": "Ann",
        "middle_name": "",
        "last_name": "Lee",
        "addr_line_1": addressLine1,
        "addr_line_2": "",
        "addr_line_3": "Town",
        "addr_state_cd": "WA",
        "addr_country_cd": "USA",
        "addr_zip": "98101",
        "fico_credit_score": 700,
    }


def AccountRecord() -> dict[str, object]:
    """Return an ACCOUNT-RECORD row with the two columns a statement prints."""
    return {"acct_id": ACCT_ID, "curr_balance": Decimal("10.00")}


def TransactionRecord(
    sequence: int, description: str = "PURCHASE", cardNum: str = CARD_NUM
) -> dict[str, object]:
    """Return a TRNX-RECORD row whose TRAN-ID is the sequence number in 16 digits."""
    return {
        "card_num": cardNum,
        "tran_id": f"{sequence:016d}",
        "tran_desc": description,
        "tran_amt": Decimal("1.25"),
    }


def BuildSingleStatement(
    customer: dict[str, object], transactions: list[dict[str, object]]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the PS and HTML records of the statement of CARD_NUM with these transactions."""
    table = cbstm03a.LoadTransactionTable(transactions)
    statement = cbstm03a.AssembleStatementData(XrefRecord(), customer, AccountRecord(), table)
    return cbstm03a.BuildStatement(statement)


@pytest.mark.parametrize("transactionCount", [1, 3, 10])
def test_statement_records_have_legacy_widths_and_counts(transactionCount: int) -> None:
    """Each statement has 19 + n PS records of 80 columns and 64 + 11n HTML records of 100."""
    transactions = [TransactionRecord(sequence) for sequence in range(1, transactionCount + 1)]
    psLines, htmlLines = BuildSingleStatement(CustomerRecord(), transactions)
    assert len(psLines) == PS_FIXED_LINE_COUNT + transactionCount
    assert len(htmlLines) == HTML_FIXED_LINE_COUNT + HTML_LINES_PER_TRANSACTION * transactionCount
    assert {len(line) for line in psLines} == {PS_LINE_WIDTH}
    assert {len(line) for line in htmlLines} == {HTML_LINE_WIDTH}


def TableInput(cardTransactionCounts: tuple[int, ...]) -> list[dict[str, object]]:
    """Return TRNX-RECORD rows in TRXFL key order, with each card's count of transactions."""
    return [
        TransactionRecord(sequence, cardNum=f"{cardIndex:016d}")
        for cardIndex, transactionCount in enumerate(cardTransactionCounts, start=1)
        for sequence in range(1, transactionCount + 1)
    ]


def GuardedRecords[RecordType](
    records: list[RecordType], consumed: list[RecordType]
) -> Iterator[RecordType]:
    """Yield records one at a time, noting each in consumed, and fail if asked for more."""
    for record in records:
        consumed.append(record)
        yield record
    pytest.fail("a record was asked for after the last one the consumer needs")


def StatementsOfCards(
    table: cbstm03a.TransactionTable, cardCount: int
) -> list[cbstm03a.StatementData]:
    """Return the statements of cards 1 to cardCount, as TableInput numbers them."""
    return [
        cbstm03a.AssembleStatementData(
            XrefRecord(f"{cardIndex:016d}"), CustomerRecord(), AccountRecord(), table
        )
        for cardIndex in range(1, cardCount + 1)
    ]


def XrefReadConnection(rows: list[dict[str, object]]) -> mock.MagicMock:
    """Return a stand-in Connection whose execute result, entered, yields rows as mappings."""
    connection = mock.MagicMock()
    connection.execute.return_value.__enter__.return_value.mappings.return_value = iter(rows)
    return connection


def JoinedXrefRow(customerFound: bool, accountFound: bool) -> dict[str, object]:
    """Return a cross-reference read row; a record not found leaves its columns NULL."""
    row = {cbstm03b.XREF_PREFIX + key: value for key, value in XrefRecord().items()}
    for prefix, record, found in (
        (cbstm03b.CUSTOMER_PREFIX, CustomerRecord(), customerFound),
        (cbstm03b.ACCOUNT_PREFIX, AccountRecord(), accountFound),
    ):
        row.update({prefix + key: value if found else None for key, value in record.items()})
    return row


def CompiledSql(statement: Select, literalBinds: bool = False) -> str:
    """Return the PostgreSQL text of an executed statement, on one line."""
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": literalBinds}
    )
    return " ".join(str(compiled).split())


def test_statement_stream_loads_full_table_from_generator() -> None:
    """51 cards of 10 transactions, the whole WS-TRNX-TABLE, load exactly from a generator."""
    records = TableInput(FULL_TABLE)
    table = cbstm03a.LoadTransactionTable(record for record in records)
    assert [len(entry) for entry in table.values()] == list(FULL_TABLE)
    assert [record for entry in table.values() for record in entry] == records


@pytest.mark.parametrize(
    ("cardTransactionCounts", "expectedMessage"),
    [
        pytest.param((1,) * 52, "MORE THAN 51 CARDS AT TABLE ENTRY 52", id="52nd-card"),
        pytest.param(
            (11,), "MORE THAN 10 TRANSACTIONS FOR ONE CARD AT TABLE ENTRY 1", id="11th-transaction"
        ),
        pytest.param(
            FULL_TABLE + (1,), "MORE THAN 51 CARDS AT TABLE ENTRY 52", id="card-past-full-table"
        ),
        pytest.param(
            FULL_TABLE[:-1] + (11,),
            "MORE THAN 10 TRANSACTIONS FOR ONE CARD AT TABLE ENTRY 51",
            id="11th-transaction-of-51st-card",
        ),
    ],
)
def test_statement_stream_stops_at_first_capacity_violation(
    cardTransactionCounts: tuple[int, ...], expectedMessage: str
) -> None:
    """The loader refuses at the record that breaks the bound and asks for nothing after it."""
    records = TableInput(cardTransactionCounts)
    consumed: list[dict[str, object]] = []
    with pytest.raises(cbstm03a.StatementCapacityError) as raised:
        cbstm03a.LoadTransactionTable(GuardedRecords(records, consumed))
    assert str(raised.value) == CAPACITY_MESSAGE_PREFIX + expectedMessage
    assert consumed == records


@pytest.mark.parametrize(
    "cardTransactionCounts",
    [
        pytest.param((10,) * 60, id="cards-past-full-table"),
        pytest.param((1,) * 600, id="one-transaction-cards"),
        pytest.param((3,) * 5 + (600,), id="long-card-run"),
        pytest.param((10,) * 50 + (9, 10, 10), id="51st-card-not-full"),
    ],
)
def test_transaction_read_limit_keeps_the_full_read_refusal(
    cardTransactionCounts: tuple[int, ...],
) -> None:
    """Oversized input cut to the read limit is refused with the full input's message."""
    records = TableInput(cardTransactionCounts)
    with pytest.raises(cbstm03a.StatementCapacityError) as fullRead:
        cbstm03a.LoadTransactionTable(records)
    with pytest.raises(cbstm03a.StatementCapacityError) as limitedRead:
        cbstm03a.LoadTransactionTable(records[: cbstm03a.TRANSACTION_TABLE_READ_LIMIT])
    assert str(limitedRead.value) == str(fullRead.value)


def test_transaction_read_selects_four_columns_in_key_order_with_limit() -> None:
    """READ TRNX-FILE fetches the printed columns in TRXFL order, at most 511 bound records."""
    connection = mock.MagicMock()
    rows = TableInput((2,))
    connection.execute.return_value.mappings.return_value = iter(rows)
    assert cbstm03b.ReadStatementTransactions(connection) == rows
    statement = connection.execute.call_args.args[0]
    assert connection.execute.call_count == 1
    assert list(statement.selected_columns.keys()) == TRANSACTION_READ_COLUMNS
    assert CompiledSql(statement).endswith("LIMIT %(param_1)s")
    assert statement.compile(dialect=postgresql.dialect()).params == {"param_1": 511}
    assert CompiledSql(statement, literalBinds=True).endswith(TRANSACTION_READ_ORDER_AND_LIMIT)


def test_statement_stream_builds_one_statement_per_advance() -> None:
    """Each advance takes one statement and yields BuildStatement's records unchanged."""
    table = cbstm03a.LoadTransactionTable(TableInput((1, 3)))
    statements = StatementsOfCards(table, 3)
    consumed: list[cbstm03a.StatementData] = []
    records = cbstm03a.BuildStatementFiles(table, GuardedRecords(statements, consumed))
    assert consumed == []
    for position, statement in enumerate(statements, start=1):
        statementRecords = next(records)
        assert len(consumed) == position
        psLines, htmlLines = cbstm03a.BuildStatement(statement)
        assert (statementRecords.psLines, statementRecords.htmlLines) == (psLines, htmlLines)
        assert statementRecords.transactionCount == len(statement.transactions)
    assert [len(statement.transactions) for statement in statements] == [1, 3, 0]


def test_statement_stream_refuses_empty_transaction_file_before_any_statement() -> None:
    """An empty TRNXFILE abends in the loader, and the builder refuses before taking one."""
    endOfFileMessage = "ERROR READING TRNXFILE RETURN CODE: 10"
    with pytest.raises(cbstm03a.EmptyTransactionFileError, match=endOfFileMessage):
        cbstm03a.LoadTransactionTable(record for record in ())
    statements = StatementsOfCards(cbstm03a.LoadTransactionTable(TableInput((1,))), 1)
    consumed: list[cbstm03a.StatementData] = []
    with pytest.raises(cbstm03a.EmptyTransactionFileError, match=endOfFileMessage):
        cbstm03a.BuildStatementFiles({}, GuardedRecords(statements, consumed))
    assert consumed == []


def test_xref_read_issues_one_streamed_outer_joined_query_in_card_order() -> None:
    """One query with a statement-level yield_per reads every xref with its two records."""
    connection = XrefReadConnection([JoinedXrefRow(True, True)])
    sources = list(cbstm03b.ReadXrefsInCardOrder(connection))
    assert sources == [cbstm03b.StatementSource(XrefRecord(), CustomerRecord(), AccountRecord())]
    assert connection.execute.call_count == 1
    connection.execution_options.assert_not_called()
    statement = connection.execute.call_args.args[0]
    assert connection.execute.call_args.kwargs == {
        "execution_options": {"yield_per": cbstm03b.XREF_FETCH_BATCH_SIZE}
    }
    assert list(statement.selected_columns.keys()) == list(JoinedXrefRow(True, True))
    assert CompiledSql(statement).endswith(XREF_READ_JOINS_AND_ORDER)


@pytest.mark.parametrize(
    ("customerFound", "accountFound", "expectedMessage"),
    [
        pytest.param(False, True, "ERROR READING CUSTFILE RETURN CODE: 23", id="no-customer"),
        pytest.param(True, False, "ERROR READING ACCTFILE RETURN CODE: 23", id="no-account"),
        pytest.param(False, False, "ERROR READING CUSTFILE RETURN CODE: 23", id="neither"),
    ],
)
def test_xref_read_maps_records_not_found_to_status_23(
    customerFound: bool, accountFound: bool, expectedMessage: str
) -> None:
    """An outer-join miss is None, which abends with the customer checked before the account."""
    (source,) = cbstm03b.ReadXrefsInCardOrder(
        XrefReadConnection([JoinedXrefRow(customerFound, accountFound)])
    )
    assert source.customer == (CustomerRecord() if customerFound else None)
    assert source.account == (AccountRecord() if accountFound else None)
    table = cbstm03a.LoadTransactionTable(TableInput((1,)))
    with pytest.raises(cbstm03a.MissingStatementRecordError) as raised:
        cbstm03a.AssembleStatementData(source.xref, source.customer, source.account, table)
    assert str(raised.value) == expectedMessage


def test_xref_read_closes_its_cursor_when_the_writer_stops_early() -> None:
    """Closing the stream after one record closes the result, and with it the cursor."""
    connection = XrefReadConnection([JoinedXrefRow(True, True)] * 3)
    sources = cbstm03b.ReadXrefsInCardOrder(connection)
    next(sources)
    result = connection.execute.return_value
    result.__exit__.assert_not_called()
    sources.close()
    result.__exit__.assert_called_once()


def FilledEntityRecord(entity: str, closingMarkup: str) -> str:
    """Return a <p> field record holding as many whole entities as 100 columns leave room for."""
    fieldRoom = HTML_LINE_WIDTH - len(HTML_PARAGRAPH_OPEN) - len(closingMarkup)
    return HTML_PARAGRAPH_OPEN + entity * (fieldRoom // len(entity)) + closingMarkup


@pytest.mark.parametrize("character", list(HTML_ESCAPED_ENTITIES))
def test_html_record_fits_escaped_values_by_whole_entities(character: str) -> None:
    """A full address line and description of one escaped character still give 100-column
    HTML records, cut by whole entities before the closing markup; PS keeps the raw text."""
    entity = HTML_ESCAPED_ENTITIES[character]
    customer = CustomerRecord(addressLine1=character * ADDRESS_LINE_WIDTH)
    transactions = [TransactionRecord(1, character * TRAN_DETAILS_WIDTH)]
    psLines, htmlLines = BuildSingleStatement(customer, transactions)
    assert {len(line) for line in htmlLines} == {HTML_LINE_WIDTH}
    fieldRecords = [
        line.rstrip() for line in htmlLines if line.startswith(HTML_PARAGRAPH_OPEN + entity)
    ]
    assert fieldRecords == [
        FilledEntityRecord(entity, HTML_ADDRESS_LINE_CLOSE),
        FilledEntityRecord(entity, HTML_FIELD_LINE_CLOSE),
    ]
    assert {len(line) for line in psLines} == {PS_LINE_WIDTH}
    assert psLines[PS_ADDRESS_LINE_1_INDEX] == (character * ADDRESS_LINE_WIDTH).ljust(PS_LINE_WIDTH)
    assert character * TRAN_DETAILS_WIDTH in psLines[PS_FIRST_TRANSACTION_INDEX]
    assert entity not in "".join(psLines)


@pytest.mark.parametrize(
    ("description", "expectedDescriptionRecord"),
    [
        pytest.param(
            "CAFE 'A' & <B>",
            "<p>CAFE 'A' &amp; &lt;B&gt;" + " " * 35 + "</p>",
            id="apostrophes-raw",
        ),
        pytest.param("&" * 11, "<p>" + "&amp;" * 11 + " " * 38 + "</p>", id="exactly-100-columns"),
    ],
)
def test_html_record_keeps_fitting_value_whole(
    description: str, expectedDescriptionRecord: str
) -> None:
    """A value whose escaped record fits in 100 columns is escaped exactly and never shortened."""
    customer = CustomerRecord(addressLine1="O'NEIL & <SONS>")
    _, htmlLines = BuildSingleStatement(customer, [TransactionRecord(1, description)])
    assert FITTING_ADDRESS_RECORD.ljust(HTML_LINE_WIDTH) in htmlLines
    assert expectedDescriptionRecord.ljust(HTML_LINE_WIDTH) in htmlLines


@pytest.mark.parametrize("marker", [cbstm03a.FIELD_VALUE_START, cbstm03a.FIELD_VALUE_END])
def test_html_record_refuses_field_marker_in_value(marker: str) -> None:
    """A field value holding a field marker raises ValueError instead of misplacing the field."""
    with pytest.raises(ValueError, match="field marker character"):
        BuildSingleStatement(CustomerRecord(), [TransactionRecord(1, "PURCHASE" + marker)])


@pytest.mark.parametrize(
    ("renderedLine", "expectedMessage"),
    [
        pytest.param(
            cbstm03a.MarkFieldValue("&") + "x" * (HTML_LINE_WIDTH + 1),
            "literal text is 101 columns",
            id="literals-over-width",
        ),
        pytest.param(
            HTML_PARAGRAPH_OPEN + cbstm03a.FIELD_VALUE_START + "x" + HTML_FIELD_LINE_CLOSE,
            "does not pair up",
            id="unpaired-marker",
        ),
    ],
)
def test_html_record_refuses_unfittable_line(renderedLine: str, expectedMessage: str) -> None:
    """A rendered line with literals over 100 columns or a lone field marker raises ValueError."""
    with pytest.raises(ValueError, match=expectedMessage):
        cbstm03a.FitHtmlRecord(renderedLine)
