"""CREASTMT statement generation: the CBSTM03A builder and the CBSTM03B reads.

Pins cbl/CBSTM03A.CBL (STATEMENT-LINES and HTML-LINES, lines 86-223; WS-TRNX-TABLE, lines
225-233; 8100-TRNXFILE-OPEN to 8599-EXIT, lines 730-853) and jcl/CREASTMT.JCL, as ported to
STATEMNT.PS records of 80 columns and STATEMNT.HTML records of exactly 100 columns, with the
51-card by 10-transaction table bound (Technical Specification, Sections 0.6.11 and 0.6.12).

The unit tests drive the builder, the reads and the publication directly. The tests at the end
run ``carddemo-batch job statement-gen`` (chain step 17) on the seeded test database: after
chain steps 6 and 13-16 the files equal the committed fixtures byte for byte, and on edited or
synthetic tables they equal the independent oracle's CREASTMT, legacy_oracle.RunCreastmt.
"""

import errno
import hashlib
import logging
import os
import secrets
from collections.abc import Callable, Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple
from unittest import mock

import pytest
from app.models import Card, CardXref, Customer, Transaction
from app.utils.cobol_format import PictureError
from jinja2.exceptions import UndefinedError
from sqlalchemy import Engine, Select, delete, func, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from batch.config import STATEMENTS_SUBDIRECTORY
from batch.jobs.statement_gen import cbstm03a, cbstm03b, creastmt
from batch.orchestration import run_ledger
from batch.orchestration.job_context import (
    EXIT_ABEND,
    EXIT_OK,
    EXIT_REJECTS,
    JobContext,
    JobFailure,
)
from batch.tests.conftest import (
    EXPECTED_DIR,
    FIXED_AS_OF_TEXT,
    BatchEnvironment,
    CliOutcome,
    CountJobRuns,
    InsertSyntheticCards,
    InsertSyntheticTransactions,
    InvokeCli,
    StepRow,
    SyntheticTransactionSpec,
)
from batch.tests.reconciliation import compare, legacy_oracle

CARD_NUM = "0000000000000101"
CUST_ID = "000000001"
ACCT_ID = "00000000001"
PS_LINE_WIDTH = 80
HTML_LINE_WIDTH = 100
# The file encodings, written out rather than read from creastmt, so a changed module encoding
# fails here: STATEMNT.PS one Latin-1 byte per column, STATEMNT.HTML the UTF-8 it declares.
PS_ENCODING = "latin-1"
HTML_ENCODING = "utf-8"
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


# RunJob's 9999-ABEND-PROGRAM diagnostics: what the ERROR log line and JobFailure may carry.
ABEND_CARD_GROUP_COUNT = 4
ABEND_CARD_GROUP_LIMIT = 10_000
ABEND_STATEMENT = "SELECT card_num FROM transactions.transactions"


class AbendSyntheticValues(NamedTuple):
    """Row-like sensitive values made at run time, for the abend tests to inject and look for."""

    cardNumber: str
    holderName: str
    sessionToken: str


class AbendDriverError(Exception):
    """A DBAPI error stand-in: its text is the server message, and pgcode is its SQLSTATE."""

    def __init__(self, message: str, pgcode: object) -> None:
        super().__init__(message)
        self.pgcode = pgcode


def AbendRandomValues() -> AbendSyntheticValues:
    """Return a 16-digit card-like number assembled from four random groups, a name, a token."""
    cardNumber = "".join(
        f"{secrets.randbelow(ABEND_CARD_GROUP_LIMIT):04d}" for _ in range(ABEND_CARD_GROUP_COUNT)
    )
    holderName = f"Holder{secrets.token_hex(4).upper()}"
    return AbendSyntheticValues(cardNumber, holderName, secrets.token_urlsafe(16))


def AbendRowText(values: AbendSyntheticValues) -> str:
    """Return error text quoting a row that holds every synthetic value."""
    return f"card {values.cardNumber} holder {values.holderName} token {values.sessionToken}"


def AbendDetailText(values: AbendSyntheticValues) -> str:
    """Return a PostgreSQL unique-violation message whose DETAIL line quotes the row."""
    return (
        'duplicate key value violates unique constraint "abend_pkey"\n'
        f"DETAIL:  Key (card_num, holder, token)=({values.cardNumber}, {values.holderName},"
        f" {values.sessionToken}) already exists."
    )


def AbendIntegrityError(values: AbendSyntheticValues) -> Exception:
    """Return a unique violation as SQLAlchemy wraps it: SQLSTATE 23505, DETAIL and parameters."""
    driverError = AbendDriverError(AbendDetailText(values), "23505")
    return IntegrityError(ABEND_STATEMENT, {"card_num": values.cardNumber}, driverError)


def AbendConnectionError(values: AbendSyntheticValues) -> Exception:
    """Return a refused connection as SQLAlchemy wraps it: no SQLSTATE, credentials in its text."""
    driverError = AbendDriverError(f"password authentication failed: {AbendRowText(values)}", None)
    return OperationalError(ABEND_STATEMENT, None, driverError)


def AbendMalformedCodeError(values: AbendSyntheticValues) -> Exception:
    """Return a database error whose pgcode is row text rather than a 5-character SQLSTATE."""
    driverError = AbendDriverError(AbendDetailText(values), values.sessionToken)
    return DBAPIError(ABEND_STATEMENT, None, driverError)


def AbendEncodingError(values: AbendSyntheticValues) -> Exception:
    """Return the UnicodeEncodeError of a strict Latin-1 write, its reason quoting the row."""
    unencodableText = f"{values.holderName}\u0100"
    position = len(values.holderName)
    return UnicodeEncodeError(
        creastmt.STATEMENT_FILE_ENCODING,
        unencodableText,
        position,
        position + 1,
        AbendRowText(values),
    )


def DirectJobContext(statementsDir: Path) -> JobContext:
    """Return the JobContext of a direct RunJob call: no ledger run, a mock engine and clock."""
    settings = mock.Mock(spec=["StatementsDir"])
    settings.StatementsDir.return_value = statementsDir
    return JobContext(engine=mock.MagicMock(), settings=settings, clock=mock.Mock())


def AbendRunJob(statementsDir: Path, transactionRead: mock.Mock) -> JobFailure:
    """Run RunJob with transactionRead as the TRNXFILE read; return the JobFailure it raises."""
    with (
        mock.patch.object(cbstm03b, "ReadStatementTransactions", transactionRead),
        pytest.raises(JobFailure) as raised,
    ):
        creastmt.RunJob(DirectJobContext(statementsDir))
    return raised.value


def AbendLogMessages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the ERROR messages creastmt logged."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == creastmt.LOGGER.name and record.levelno == logging.ERROR
    ]


@pytest.mark.parametrize(
    ("errorFactory", "expectedDiagnostic"),
    [
        pytest.param(
            AbendIntegrityError,
            "DATABASE ERROR IntegrityError (AbendDriverError) SQLSTATE 23505",
            id="integrity-error",
        ),
        pytest.param(
            AbendConnectionError,
            "DATABASE ERROR OperationalError (AbendDriverError) SQLSTATE UNKNOWN",
            id="operational-error-without-sqlstate",
        ),
        pytest.param(
            AbendMalformedCodeError,
            "DATABASE ERROR DBAPIError (AbendDriverError) SQLSTATE UNKNOWN",
            id="dbapi-error-with-malformed-sqlstate",
        ),
        pytest.param(
            lambda values: FileExistsError(
                errno.EEXIST, os.strerror(errno.EEXIST), AbendRowText(values)
            ),
            "FILE ERROR FileExistsError ERRNO EEXIST",
            id="file-error-with-errno",
        ),
        pytest.param(
            lambda values: OSError(AbendRowText(values)),
            "FILE ERROR OSError ERRNO UNKNOWN",
            id="file-error-without-errno",
        ),
        pytest.param(
            lambda values: ValueError(AbendRowText(values)),
            "PICTURE, ENCODING OR RECORD LAYOUT ERROR ValueError",
            id="record-layout-error",
        ),
        pytest.param(
            lambda values: PictureError(AbendRowText(values)),
            "PICTURE, ENCODING OR RECORD LAYOUT ERROR PictureError",
            id="picture-error",
        ),
        pytest.param(
            AbendEncodingError,
            "PICTURE, ENCODING OR RECORD LAYOUT ERROR UnicodeEncodeError",
            id="encoding-error",
        ),
        pytest.param(
            lambda values: UndefinedError(AbendRowText(values)),
            "STATEMENT TEMPLATE ERROR UndefinedError",
            id="template-error",
        ),
    ],
)
def test_run_job_abend_names_error_class_and_code_never_error_text(
    errorFactory: Callable[[AbendSyntheticValues], Exception],
    expectedDiagnostic: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A database, file, value or template abend logs and raises no text of the error itself."""
    caplog.set_level(logging.ERROR, logger=creastmt.LOGGER.name)
    values = AbendRandomValues()
    error = errorFactory(values)
    assert all(value in str(error) for value in values)
    failure = AbendRunJob(tmp_path, mock.Mock(side_effect=error))
    expectedMessage = f"ABENDING PROGRAM CBSTM03A: {expectedDiagnostic}"
    assert (failure.exitCode, failure.message, failure.__cause__) == (
        EXIT_ABEND,
        expectedMessage,
        error,
    )
    assert AbendLogMessages(caplog) == [expectedMessage]
    assert [value for value in values if value in caplog.text or value in failure.message] == []


def AbendOverCapacityRows(values: AbendSyntheticValues) -> list[dict[str, object]]:
    """Return a full table of 51 cards and a 52nd card's row holding the synthetic values."""
    overflowRow = TransactionRecord(1, AbendRowText(values), cardNum=values.cardNumber)
    return TableInput((1,) * cbstm03a.MAX_STATEMENT_CARDS) + [overflowRow]


@pytest.mark.parametrize(
    ("rowsFactory", "expectedDiagnostic"),
    [
        pytest.param(
            AbendOverCapacityRows,
            CAPACITY_MESSAGE_PREFIX + "MORE THAN 51 CARDS AT TABLE ENTRY 52",
            id="table-capacity",
        ),
        pytest.param(
            lambda values: [], "ERROR READING TRNXFILE RETURN CODE: 10", id="empty-trnxfile"
        ),
    ],
)
def test_run_job_abend_keeps_the_cbstm03a_display_text(
    rowsFactory: Callable[[AbendSyntheticValues], list[dict[str, object]]],
    expectedDiagnostic: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A CBSTM03A abend logs and raises its DISPLAY text verbatim, with exit 16 and no row text."""
    caplog.set_level(logging.ERROR, logger=creastmt.LOGGER.name)
    values = AbendRandomValues()
    failure = AbendRunJob(tmp_path, mock.Mock(return_value=rowsFactory(values)))
    expectedMessage = f"ABENDING PROGRAM CBSTM03A: {expectedDiagnostic}"
    assert isinstance(failure.__cause__, cbstm03a.StatementAbendError)
    assert str(failure.__cause__) == expectedDiagnostic
    assert (failure.exitCode, failure.message) == (EXIT_ABEND, expectedMessage)
    assert AbendLogMessages(caplog) == [expectedMessage]
    assert [value for value in values if value in caplog.text] == []


# PublishFiles and RecoverInterruptedPublication: STATEMNT.PS and STATEMNT.HTML are published as
# one pair, and a failure or a kill before the commit point leaves the previous pair as it was.
PUBLICATION_DIRECTORY_NAME = "statements"
PUBLICATION_PS_NAME = "STATEMNT.PS"
PUBLICATION_HTML_NAME = "STATEMNT.HTML"
PUBLICATION_NON_ASCII_LABEL = "CAFÉ ÑANDÚ ¢50"
PUBLICATION_STATEMENT_COUNT = 2
PUBLICATION_INJECTED_TEXT = "injected rename failure"
PUBLICATION_LEDGER_STATEMENT = "UPDATE batch.job_runs SET status = %(status)s"
PUBLICATION_CYCLE_ID = 1
PUBLICATION_STEP_NO = 17
PUBLICATION_RUN_ID = 1


def PublicationRecords(label: str) -> list[cbstm03a.StatementRecords]:
    """Return statements of one 80-column PS and one 100-column HTML record, marked by label.

    Statement n counts n transaction lines.
    """
    return [
        cbstm03a.StatementRecords(
            psLines=(f"{label} {PUBLICATION_PS_NAME} {index}".ljust(PS_LINE_WIDTH),),
            htmlLines=(f"{label} {PUBLICATION_HTML_NAME} {index}".ljust(HTML_LINE_WIDTH),),
            transactionCount=index,
        )
        for index in range(1, PUBLICATION_STATEMENT_COUNT + 1)
    ]


def PublicationContents(records: list[cbstm03a.StatementRecords]) -> dict[str, bytes]:
    """Return the bytes of the two statement files written from records, by file name."""
    psText = "".join(line + "\n" for statement in records for line in statement.psLines)
    htmlText = "".join(line + "\n" for statement in records for line in statement.htmlLines)
    return {
        PUBLICATION_PS_NAME: psText.encode(PS_ENCODING),
        PUBLICATION_HTML_NAME: htmlText.encode(HTML_ENCODING),
    }


def PublicationWritePair(statementsDir: Path, label: str) -> dict[str, bytes]:
    """Write the pair of PublicationRecords(label) as the statement files; return its bytes."""
    contents = PublicationContents(PublicationRecords(label))
    statementsDir.mkdir(parents=True, exist_ok=True)
    for fileName, content in contents.items():
        (statementsDir / fileName).write_bytes(content)
    return contents


def PublicationFiles(statementsDir: Path) -> dict[str, bytes] | None:
    """Return every file in statementsDir by name with its bytes, or None with no directory."""
    if not statementsDir.exists():
        return None
    return {path.name: path.read_bytes() for path in statementsDir.iterdir()}


def PublicationInterruptedState(
    statementsDir: Path, hasPrevious: bool, renamesDone: int
) -> dict[str, bytes]:
    """Leave the files of a run killed after its backups, its marker and renamesDone renames.

    Returns what a rollback must leave: the previous pair, or no file after a first run.
    """
    statementsDir.mkdir(parents=True, exist_ok=True)
    previous = PublicationWritePair(statementsDir, "PREVIOUS") if hasPrevious else {}
    for fileName in previous:
        os.link(statementsDir / fileName, creastmt.BackupPath(statementsDir, fileName))
    creastmt.MarkerPath(statementsDir).touch()
    newContents = PublicationContents(PublicationRecords("NEW"))
    for position, (fileName, content) in enumerate(newContents.items()):
        temporaryPath = creastmt.TemporaryPath(statementsDir, fileName)
        temporaryPath.write_bytes(content)
        if position < renamesDone:
            os.replace(temporaryPath, statementsDir / fileName)
    return previous


def PublicationRollbackLogs(statementsDir: Path, hasPrevious: bool, renamesDone: int) -> list[str]:
    """Return the WARNING lines recovery logs for PublicationInterruptedState's files, in order:
    each .part file left, the rollback, then each renamed file restored or removed."""
    fileNames = [PUBLICATION_PS_NAME, PUBLICATION_HTML_NAME]
    staleParts = [
        creastmt.STALE_FILE_LOG % creastmt.TemporaryPath(statementsDir, fileName)
        for fileName in fileNames[renamesDone:]
    ]
    changedFiles = [
        creastmt.RESTORED_FILE_LOG
        % (statementsDir / fileName, creastmt.BackupPath(statementsDir, fileName))
        if hasPrevious
        else creastmt.UNPUBLISHED_FILE_LOG % (statementsDir / fileName)
        for fileName in fileNames[:renamesDone]
    ]
    return [*staleParts, creastmt.ROLLBACK_LOG % creastmt.MarkerPath(statementsDir), *changedFiles]


def PublicationFailingReplace(failingRenames: tuple[int, ...]) -> Callable[[Path, Path], None]:
    """Return an os.replace stand-in that raises OSError on the .part renames numbered in
    failingRenames, counting from 1, and performs every other rename."""
    realReplace = os.replace
    partRenames: list[Path] = []

    def Replace(source: Path, destination: Path) -> None:
        if os.fspath(source).endswith(creastmt.TEMPORARY_SUFFIX):
            partRenames.append(source)
            if len(partRenames) in failingRenames:
                raise OSError(errno.EIO, PUBLICATION_INJECTED_TEXT, os.fspath(source))
        realReplace(source, destination)

    return Replace


def PublicationJobContext(statementsDir: Path) -> JobContext:
    """Return a chain-started run's JobContext, its statements in statementsDir."""
    context = DirectJobContext(statementsDir)
    return context.ForRun(PUBLICATION_CYCLE_ID, PUBLICATION_STEP_NO, PUBLICATION_RUN_ID)


def PublicationRunJob(statementsDir: Path, transactionRows: list[dict[str, object]]) -> JobFailure:
    """Run RunJob over transactionRows and one card's cross-reference, with a ledger finish
    that fails; return the JobFailure it raises."""
    source = cbstm03b.StatementSource(XrefRecord(), CustomerRecord(), AccountRecord())
    sources = (xrefSource for xrefSource in [source])
    ledgerError = DBAPIError(PUBLICATION_LEDGER_STATEMENT, None, ConnectionError("lost"))
    with (
        mock.patch.object(cbstm03b, "ReadStatementTransactions", return_value=transactionRows),
        mock.patch.object(cbstm03b, "ReadXrefsInCardOrder", return_value=sources),
        mock.patch.object(creastmt.run_ledger, "FinishIfRunning", side_effect=ledgerError),
        pytest.raises(JobFailure) as raised,
    ):
        creastmt.RunJob(PublicationJobContext(statementsDir))
    return raised.value


@pytest.mark.parametrize(
    "label", ["NEW", PUBLICATION_NON_ASCII_LABEL], ids=["ascii-records", "non-ascii-records"]
)
@pytest.mark.parametrize("hasPrevious", [True, False], ids=["previous-pair", "first-run"])
def test_publication_replaces_both_files_and_leaves_no_work_file(
    tmp_path: Path, hasPrevious: bool, label: str
) -> None:
    """A complete run leaves exactly the new pair, STATEMNT.PS in Latin-1 and STATEMNT.HTML in
    UTF-8: no .part file, backup or marker."""
    statementsDir = tmp_path / PUBLICATION_DIRECTORY_NAME
    if hasPrevious:
        PublicationWritePair(statementsDir, "PREVIOUS")
    records = PublicationRecords(label)
    counts = creastmt.WriteStatementFiles(statementsDir, records)
    assert PublicationFiles(statementsDir) == PublicationContents(records)
    assert counts.AsLedgerCounts() == {"statements": 2, "transactions": 3}


@pytest.mark.parametrize("failingRename", [1, 2], ids=["first-rename", "second-rename"])
@pytest.mark.parametrize("hasPrevious", [True, False], ids=["previous-pair", "first-run"])
def test_publication_rename_failure_leaves_the_previous_pair_and_no_work_file(
    tmp_path: Path, hasPrevious: bool, failingRename: int
) -> None:
    """An OSError on either .part rename is raised after the rollback, which leaves the
    previous pair byte for byte, or no statement file after a first run, and nothing else."""
    statementsDir = tmp_path / PUBLICATION_DIRECTORY_NAME
    previous = PublicationWritePair(statementsDir, "PREVIOUS") if hasPrevious else {}
    with (
        mock.patch.object(creastmt.os, "replace", PublicationFailingReplace((failingRename,))),
        pytest.raises(OSError, match=PUBLICATION_INJECTED_TEXT),
    ):
        creastmt.WriteStatementFiles(statementsDir, PublicationRecords("NEW"))
    assert PublicationFiles(statementsDir) == previous


def test_publication_refuses_an_unrecovered_marker_and_keeps_its_backups(tmp_path: Path) -> None:
    """A marker no recovery has handled stops the publication before it touches a file."""
    statementsDir = tmp_path / PUBLICATION_DIRECTORY_NAME
    PublicationInterruptedState(statementsDir, True, 2)
    interruptedFiles = PublicationFiles(statementsDir)
    with pytest.raises(FileExistsError):
        creastmt.WriteStatementFiles(statementsDir, PublicationRecords("LATER"))
    assert PublicationFiles(statementsDir) == interruptedFiles


@pytest.mark.parametrize("renamesDone", [0, 1, 2], ids=["no-rename", "one-rename", "two-renames"])
@pytest.mark.parametrize("hasPrevious", [True, False], ids=["previous-pair", "first-run"])
def test_recovery_rolls_back_a_publication_killed_before_its_commit_point(
    tmp_path: Path, hasPrevious: bool, renamesDone: int, caplog: pytest.LogCaptureFixture
) -> None:
    """A marker left by a killed run makes the next run restore the previous pair, or remove
    the new files of a first run, and delete every work file."""
    statementsDir = tmp_path / PUBLICATION_DIRECTORY_NAME
    previous = PublicationInterruptedState(statementsDir, hasPrevious, renamesDone)
    caplog.set_level(logging.WARNING, logger=creastmt.LOGGER.name)
    creastmt.RecoverInterruptedPublication(statementsDir)
    assert PublicationFiles(statementsDir) == previous
    assert caplog.messages == PublicationRollbackLogs(statementsDir, hasPrevious, renamesDone)


def test_failed_rollback_keeps_its_marker_and_the_next_run_completes_it(tmp_path: Path) -> None:
    """A rollback that fails in turn is raised with the marker and backups kept, and the next
    run's recovery restores the previous pair."""
    statementsDir = tmp_path / PUBLICATION_DIRECTORY_NAME
    previous = PublicationWritePair(statementsDir, "PREVIOUS")
    with (
        mock.patch.object(creastmt.os, "replace", PublicationFailingReplace((2, 3))),
        pytest.raises(OSError, match=PUBLICATION_INJECTED_TEXT),
    ):
        creastmt.WriteStatementFiles(statementsDir, PublicationRecords("NEW"))
    assert creastmt.MarkerPath(statementsDir).exists()
    creastmt.RecoverInterruptedPublication(statementsDir)
    assert PublicationFiles(statementsDir) == previous


def test_recovery_cut_short_keeps_its_marker_and_the_next_run_completes_it(tmp_path: Path) -> None:
    """A recovery killed after restoring STATEMNT.PS keeps the marker and backups, so the next
    recovery restores STATEMNT.HTML too, with STATEMNT.PS restored once more unchanged."""
    statementsDir = tmp_path / PUBLICATION_DIRECTORY_NAME
    previous = PublicationInterruptedState(statementsDir, True, 2)
    with (
        mock.patch.object(creastmt.os, "replace", PublicationFailingReplace((2,))),
        pytest.raises(OSError, match=PUBLICATION_INJECTED_TEXT),
    ):
        creastmt.RecoverInterruptedPublication(statementsDir)
    assert creastmt.MarkerPath(statementsDir).exists()
    assert (statementsDir / PUBLICATION_PS_NAME).read_bytes() == previous[PUBLICATION_PS_NAME]
    creastmt.RecoverInterruptedPublication(statementsDir)
    assert PublicationFiles(statementsDir) == previous


@pytest.mark.parametrize(
    "leftoverPathOf",
    [
        pytest.param(creastmt.BackupPath, id="backups-after-commit"),
        pytest.param(creastmt.TemporaryPath, id="parts-mid-write"),
    ],
)
def test_recovery_without_marker_deletes_leftover_work_files_and_keeps_the_pair(
    tmp_path: Path,
    leftoverPathOf: Callable[[Path, str], Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With no marker, the backups or .part files a killed run left are deleted and logged by
    path, and the statement files stay as they are."""
    statementsDir = tmp_path / PUBLICATION_DIRECTORY_NAME
    current = PublicationWritePair(statementsDir, "CURRENT")
    leftoverPaths = [leftoverPathOf(statementsDir, fileName) for fileName in current]
    for leftoverPath in leftoverPaths:
        leftoverPath.write_bytes(b"LEFTOVER")
    caplog.set_level(logging.WARNING, logger=creastmt.LOGGER.name)
    creastmt.RecoverInterruptedPublication(statementsDir)
    assert PublicationFiles(statementsDir) == current
    assert caplog.messages == [creastmt.STALE_FILE_LOG % path for path in leftoverPaths]


def test_recovery_creates_nothing_without_a_statement_directory(tmp_path: Path) -> None:
    """Recovery of an output directory with no statements creates no directory or file."""
    creastmt.RecoverInterruptedPublication(tmp_path / PUBLICATION_DIRECTORY_NAME)
    assert list(tmp_path.iterdir()) == []


def test_ledger_failure_after_the_commit_point_exits_16_with_the_new_statements(
    tmp_path: Path,
) -> None:
    """A ledger finish that fails after the commit point exits 16 with the new pair in place."""
    statementsDir = tmp_path / PUBLICATION_DIRECTORY_NAME
    PublicationWritePair(statementsDir, "PREVIOUS")
    failure = PublicationRunJob(statementsDir, [TransactionRecord(1)])
    psLines, htmlLines = BuildSingleStatement(CustomerRecord(), [TransactionRecord(1)])
    newRecords = [cbstm03a.StatementRecords(psLines, htmlLines, transactionCount=1)]
    assert failure.exitCode == EXIT_ABEND
    assert isinstance(failure.__cause__, DBAPIError)
    assert PublicationFiles(statementsDir) == PublicationContents(newRecords)


@pytest.mark.parametrize(
    "cardTransactionCounts",
    [pytest.param((1,) * 52, id="52nd-card"), pytest.param((11,), id="11th-transaction")],
)
@pytest.mark.parametrize(
    "interrupted", [True, False], ids=["killed-mid-publication", "fresh-output-directory"]
)
def test_read_abend_after_recovery_leaves_the_previous_statements(
    tmp_path: Path, interrupted: bool, cardTransactionCounts: tuple[int, ...]
) -> None:
    """Recovery runs before the reads, so a capacity abend exits 16 with the previous pair
    restored from a killed publication, or with no statement directory created."""
    statementsDir = tmp_path / PUBLICATION_DIRECTORY_NAME
    previous = PublicationInterruptedState(statementsDir, True, 1) if interrupted else None
    failure = PublicationRunJob(statementsDir, TableInput(cardTransactionCounts))
    assert failure.exitCode == EXIT_ABEND
    assert PublicationFiles(statementsDir) == previous


# Chain steps 6 and 13-16 as standalone jobs, with the status each ends with on the sample data:
# POSTTRAN rejects records, the legacy RC 4. TRANSACT then holds the COMBTRAN records.
CHAIN_SET_UP_JOBS = (
    (("job", "tran-backup"), EXIT_OK),
    (("job", "post-tran", "--as-of", FIXED_AS_OF_TEXT), EXIT_REJECTS),
    (("job", "interest-calc", "--as-of", FIXED_AS_OF_TEXT), EXIT_OK),
    (("job", "tran-backup"), EXIT_OK),
    (("job", "comb-tran"), EXIT_OK),
)
STATEMENT_JOB_ARGUMENTS = ("job", creastmt.JOB_NAME)
FORCE_OPTION = "--force"
COMBTRAN_STEP_NO = 16
STATEMENT_STEP_NO = 17
# WS-CARD-TBL OCCURS 51 and WS-TRAN-TBL OCCURS 10 (cbl/CBSTM03A.CBL lines 226-228), written out
# rather than read from cbstm03a, so a changed bound in the module under test fails here.
STATEMENT_CAPACITY_CARDS = 51
STATEMENT_CAPACITY_TRANSACTIONS = 10
SYNTHETIC_AMOUNT = Decimal("12.34")
SYNTHETIC_TRANSACTIONS_PER_CARD = 2
PS_LAYOUT_NAME = "STATEMNT.PS"
HTML_LAYOUT_NAME = "STATEMNT.HTML"
EXPECTED_STATEMENT_PATHS = (
    EXPECTED_DIR / "creastmt_STATEMNT.PS",
    EXPECTED_DIR / "creastmt_STATEMNT.HTML",
)
ORACLE_DIRECTORY_NAME = "oracle"
# The entities a default HTML autoescape would write for an apostrophe; CBSTM03A writes it raw.
APOSTROPHE_ENTITIES = ("&#39;", "&#x27;")
# ST-LINE1, the second STATEMNT.PS record of every statement, holds ST-NAME X(75). STRING keeps
# each name part up to its first space and follows it with one space (5000-CREATE-STATEMENT).
PS_NAME_LINE_INDEX = 1
MULTI_WORD_NAMES = {"first_name": "MARY ANN", "middle_name": "JO BETH", "last_name": "VAN DYKE"}
EXPECTED_STATEMENT_NAME = "MARY JO VAN "


class StatementTextCase(NamedTuple):
    """Text set in a customer's address line 1 and a transaction's description, with the values
    STATEMNT.PS must hold raw, as Latin-1 bytes, and STATEMNT.HTML escaped."""

    address: str
    description: str
    psBytes: tuple[bytes, ...]
    escapedValues: tuple[str, ...]


# ASCII markup, and code page 037 text outside ASCII. An HTML field value ends at its first '*'
# (DELIMITED BY '*'); STATEMNT.PS keeps ST-TRANDT X(49) whole.
MARKUP_TEXT_CASE = StatementTextCase(
    address="O'NEIL & <SONS>",
    description="CAFE 'A' & <B>",
    psBytes=(b"O'NEIL & <SONS>", b"CAFE 'A' & <B>"),
    escapedValues=("O'NEIL &amp; &lt;SONS&gt;", "CAFE 'A' &amp; &lt;B&gt;"),
)
NON_ASCII_TEXT_CASE = StatementTextCase(
    address="CAFÉ <B>ÑANDÚ</B> & ¢50",
    description='Café <b>Ñandú</b> & ¢50 "q" O\'Brien *star after',
    psBytes=(
        b"CAF\xc9 <B>\xd1AND\xda</B> & \xa250",
        b'Caf\xe9 <b>\xd1and\xfa</b> & \xa250 "q" O\'Brien *star after',
    ),
    escapedValues=(
        "<p>CAFÉ &lt;B&gt;ÑANDÚ&lt;/B&gt; &amp; ¢50  </p>",
        '<p>Café &lt;b&gt;Ñandú&lt;/b&gt; &amp; ¢50 "q" O\'Brien </p>',
    ),
)


def StatementPaths(outputDir: Path) -> tuple[Path, Path]:
    """Return STATEMNT.PS and STATEMNT.HTML under outputDir/statements, where CREASTMT writes."""
    statementsDir = outputDir / STATEMENTS_SUBDIRECTORY
    return statementsDir / creastmt.STATEMENT_FILE_NAME, statementsDir / creastmt.HTML_FILE_NAME


def RunChainThroughCombtran(batchEnvironment: BatchEnvironment) -> None:
    """Run chain steps 6 and 13-16 of the seed cycle with the frozen clock, as the fixtures did."""
    for arguments, expectedExitCode in CHAIN_SET_UP_JOBS:
        outcome = InvokeCli(arguments)
        assert outcome.exitCode == expectedExitCode, outcome.output
    combtranStep = StepRow(batchEnvironment.engine, COMBTRAN_STEP_NO)
    assert combtranStep["state"] == run_ledger.STEP_COMPLETED


def RunStatementJob(expectedExitCode: int, force: bool = False) -> CliOutcome:
    """Run ``job statement-gen``, chain step 17, and assert it ends with expectedExitCode."""
    arguments = [*STATEMENT_JOB_ARGUMENTS, *((FORCE_OPTION,) if force else ())]
    outcome = InvokeCli(arguments)
    assert outcome.exitCode == expectedExitCode, outcome.output
    return outcome


def ReadLines(path: Path) -> list[str]:
    """Return a statement file's records, each without its LF terminator."""
    return compare.ReadFixedWidthRecords(path)


def DecodedRecords(path: Path, encoding: str) -> list[str]:
    """Return a statement file's records decoded strictly from encoding, each without its LF."""
    records = path.read_bytes().decode(encoding).split(compare.RECORD_TERMINATOR)
    assert records.pop() == ""
    return records


def FileDigests(paths: tuple[Path, ...]) -> list[str]:
    """Return the SHA-256 of each file's bytes."""
    return [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]


def StatementDifferences(
    actualPaths: tuple[Path, Path], expectedPaths: tuple[Path, Path]
) -> list[str]:
    """Return every line difference of STATEMNT.PS and STATEMNT.HTML from the expected files."""
    layoutNames = (PS_LAYOUT_NAME, HTML_LAYOUT_NAME)
    return [
        difference
        for actualPath, expectedPath, layoutName in zip(
            actualPaths, expectedPaths, layoutNames, strict=True
        )
        for difference in compare.CompareFixedWidthFiles(actualPath, expectedPath, layoutName)
    ]


def RunOracleStatements(engine: Engine) -> legacy_oracle.StatementOutcome:
    """Run the oracle's CREASTMT over the tables as they stand, read through compare's plain SQL.

    Returns its STATEMNT.PS and STATEMNT.HTML lines and exit status: no lines on an abend.
    """
    with engine.connect() as connection:
        return legacy_oracle.RunCreastmt(compare.ReadTableAsOracleState(connection))


def WriteOracleStatements(engine: Engine, oracleDir: Path) -> tuple[Path, Path]:
    """Write the two files of RunOracleStatements(engine), after asserting the oracle succeeded.

    The lines are written as the fixtures are: STATEMNT.PS in Latin-1 and STATEMNT.HTML in
    UTF-8, one LF after every record. Returns the paths, as StatementPaths(oracleDir).
    """
    outcome = RunOracleStatements(engine)
    assert outcome.exitCode == legacy_oracle.EXIT_OK
    oraclePaths = StatementPaths(oracleDir)
    oraclePaths[0].parent.mkdir(parents=True)
    oracleFiles = zip(
        oraclePaths,
        (outcome.psLines, outcome.htmlLines),
        (PS_ENCODING, HTML_ENCODING),
        strict=True,
    )
    for oraclePath, lines, encoding in oracleFiles:
        recordText = "".join(line + compare.RECORD_TERMINATOR for line in lines)
        oraclePath.write_bytes(recordText.encode(encoding))
    return oraclePaths


def FirstStatementXref(engine: Engine) -> dict[str, str]:
    """Return the card and customer of the first statement: the lowest card in key order."""
    statement = (
        select(CardXref.card_num, CardXref.cust_id)
        .join(Card, Card.card_num == CardXref.card_num)
        .order_by(Card.card_num_sort)
        .limit(1)
    )
    with engine.connect() as connection:
        return dict(connection.execute(statement).mappings().one())


def CardNumbers(engine: Engine) -> list[str]:
    """Return every card number in CARDDATA key order."""
    statement = select(Card.card_num).order_by(Card.card_num_sort)
    with engine.connect() as connection:
        return list(connection.execute(statement).scalars())


def SetCustomerColumns(engine: Engine, custId: str, values: dict[str, str]) -> None:
    """Overwrite columns of one customer, as bound parameters."""
    with engine.begin() as connection:
        connection.execute(update(Customer).where(Customer.cust_id == custId).values(**values))


def SetFirstTransactionDescription(engine: Engine, cardNum: str, description: str) -> None:
    """Overwrite TRAN-DESC of the card's lowest TRAN-ID, as a bound parameter."""
    firstTranId = (
        select(func.min(Transaction.tran_id))
        .where(Transaction.card_num == cardNum)
        .scalar_subquery()
    )
    with engine.begin() as connection:
        connection.execute(
            update(Transaction)
            .where(Transaction.tran_id == firstTranId)
            .values(tran_desc=description)
        )


def InsertCardTransactions(engine: Engine, cardNums: Sequence[str], perCard: int) -> None:
    """Insert perCard synthetic transactions of SYNTHETIC_AMOUNT for each card of cardNums."""
    spec = SyntheticTransactionSpec(cardNums=cardNums, perCard=perCard, amount=SYNTHETIC_AMOUNT)
    InsertSyntheticTransactions(engine, spec)


def FillFiftyTwoCards(engine: Engine) -> None:
    """Give 52 cards one transaction each, one card past WS-CARD-TBL OCCURS 51."""
    InsertSyntheticCards(engine, STATEMENT_CAPACITY_CARDS + 1 - len(CardNumbers(engine)))
    InsertCardTransactions(engine, CardNumbers(engine), 1)


def FillElevenTransactionsForOneCard(engine: Engine) -> None:
    """Give the first card eleven transactions, one past WS-TRAN-TBL OCCURS 10."""
    elevenTransactions = STATEMENT_CAPACITY_TRANSACTIONS + 1
    InsertCardTransactions(engine, CardNumbers(engine)[:1], elevenTransactions)


def test_statement_files_equal_expected_fixtures_byte_for_byte(
    batchEnvironment: BatchEnvironment,
) -> None:
    """After chain steps 6 and 13-16, STEP040 writes both files exactly as the fixtures hold
    them, the sample apostrophes (custdata.txt record 31, dailytran.txt record 76) raw."""
    RunChainThroughCombtran(batchEnvironment)
    RunStatementJob(EXIT_OK)
    actualPaths = StatementPaths(batchEnvironment.outputDir)
    assert StatementDifferences(actualPaths, EXPECTED_STATEMENT_PATHS) == []
    htmlText = actualPaths[1].read_text(encoding=HTML_ENCODING)
    assert "'" in htmlText
    assert [entity for entity in APOSTROPHE_ENTITIES if entity in htmlText] == []
    statementStep = StepRow(batchEnvironment.engine, STATEMENT_STEP_NO)
    assert statementStep["state"] == run_ledger.STEP_COMPLETED


def test_statement_lines_have_legacy_widths(batchEnvironment: BatchEnvironment) -> None:
    """Every STATEMNT.PS record is LRECL 80 and every STATEMNT.HTML record LRECL 100, counted in
    the characters of each file's encoding."""
    RunChainThroughCombtran(batchEnvironment)
    RunStatementJob(EXIT_OK)
    psPath, htmlPath = StatementPaths(batchEnvironment.outputDir)
    assert {len(line) for line in DecodedRecords(psPath, PS_ENCODING)} == {PS_LINE_WIDTH}
    assert {len(line) for line in DecodedRecords(htmlPath, HTML_ENCODING)} == {HTML_LINE_WIDTH}


def test_statement_rerun_overwrites_files_with_identical_content(
    batchEnvironment: BatchEnvironment,
) -> None:
    """A forced rerun of the completed step 17 writes new files with the same bytes."""
    RunChainThroughCombtran(batchEnvironment)
    RunStatementJob(EXIT_OK)
    paths = StatementPaths(batchEnvironment.outputDir)
    firstDigests = FileDigests(paths)
    firstInodes = [path.stat().st_ino for path in paths]
    RunStatementJob(EXIT_OK, force=True)
    assert FileDigests(paths) == firstDigests
    assert [path.stat().st_ino for path in paths] != firstInodes
    engine = batchEnvironment.engine
    assert CountJobRuns(engine, STATEMENT_STEP_NO, run_ledger.RUN_COMPLETED) == 2


def test_statement_name_keeps_only_first_word_of_each_name_part(
    batchEnvironment: BatchEnvironment, tmp_path: Path
) -> None:
    """ST-NAME holds the first word of the first, middle and last names, each then a space."""
    engine = batchEnvironment.engine
    xref = FirstStatementXref(engine)
    SetCustomerColumns(engine, xref["cust_id"], MULTI_WORD_NAMES)
    InsertCardTransactions(engine, [xref["card_num"]], SYNTHETIC_TRANSACTIONS_PER_CARD)
    RunStatementJob(EXIT_OK)
    actualPaths = StatementPaths(batchEnvironment.outputDir)
    nameLine = ReadLines(actualPaths[0])[PS_NAME_LINE_INDEX]
    assert nameLine == EXPECTED_STATEMENT_NAME.ljust(PS_LINE_WIDTH)
    oraclePaths = WriteOracleStatements(engine, tmp_path / ORACLE_DIRECTORY_NAME)
    assert StatementDifferences(actualPaths, oraclePaths) == []


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(MARKUP_TEXT_CASE, id="ascii-markup"),
        pytest.param(NON_ASCII_TEXT_CASE, id="non-ascii-cp037-text"),
    ],
)
def test_html_escapes_only_ampersand_and_angle_brackets_and_keeps_apostrophes(
    batchEnvironment: BatchEnvironment, tmp_path: Path, case: StatementTextCase
) -> None:
    """User text reaches STATEMNT.HTML with &, < and > escaped and nothing else changed, and
    STATEMNT.PS raw: the input-cleaning pin of the statement template. STATEMNT.HTML records are
    100 UTF-8 characters and STATEMNT.PS records 80 Latin-1 bytes."""
    engine = batchEnvironment.engine
    xref = FirstStatementXref(engine)
    SetCustomerColumns(engine, xref["cust_id"], {"addr_line_1": case.address})
    InsertCardTransactions(engine, [xref["card_num"]], SYNTHETIC_TRANSACTIONS_PER_CARD)
    SetFirstTransactionDescription(engine, xref["card_num"], case.description)
    RunStatementJob(EXIT_OK)
    actualPaths = StatementPaths(batchEnvironment.outputDir)
    psRecords = DecodedRecords(actualPaths[0], PS_ENCODING)
    htmlRecords = DecodedRecords(actualPaths[1], HTML_ENCODING)
    assert {len(record) for record in psRecords} == {PS_LINE_WIDTH}
    assert {len(record) for record in htmlRecords} == {HTML_LINE_WIDTH}
    psText, htmlText = "\n".join(psRecords), "\n".join(htmlRecords)
    assert [value for value in case.escapedValues if value not in htmlText] == []
    assert [entity for entity in APOSTROPHE_ENTITIES if entity in htmlText] == []
    assert [value for value in (case.address, case.description) if value not in psText] == []
    psBytes = actualPaths[0].read_bytes()
    assert [value for value in case.psBytes if value not in psBytes] == []
    oraclePaths = WriteOracleStatements(engine, tmp_path / ORACLE_DIRECTORY_NAME)
    assert StatementDifferences(actualPaths, oraclePaths) == []


def test_capacity_of_51_cards_by_10_transactions_is_processed_exactly(
    batchEnvironment: BatchEnvironment, tmp_path: Path
) -> None:
    """A full WS-TRNX-TABLE, 51 cards of 10 transactions each, gives the oracle's statements."""
    engine = batchEnvironment.engine
    InsertSyntheticCards(engine, STATEMENT_CAPACITY_CARDS - len(CardNumbers(engine)))
    cardNums = CardNumbers(engine)
    assert len(cardNums) == STATEMENT_CAPACITY_CARDS
    with engine.begin() as connection:
        connection.execute(delete(Transaction))
    InsertCardTransactions(engine, cardNums, STATEMENT_CAPACITY_TRANSACTIONS)
    RunStatementJob(EXIT_OK)
    actualPaths = StatementPaths(batchEnvironment.outputDir)
    oraclePaths = WriteOracleStatements(engine, tmp_path / ORACLE_DIRECTORY_NAME)
    assert StatementDifferences(actualPaths, oraclePaths) == []
    fullTableLines = PS_FIXED_LINE_COUNT + STATEMENT_CAPACITY_TRANSACTIONS
    assert len(ReadLines(actualPaths[0])) == STATEMENT_CAPACITY_CARDS * fullTableLines


# The expected outcome is legacy_oracle.RunCreastmt's, run in-test on the arranged tables.
@pytest.mark.parametrize(
    "fillOverCapacity",
    [
        pytest.param(FillFiftyTwoCards, id="52nd-card"),
        pytest.param(FillElevenTransactionsForOneCard, id="11th-transaction"),
    ],
)
@pytest.mark.parametrize(
    "hasPrevious", [False, True], ids=["fresh-output-directory", "previous-statements"]
)
def test_over_capacity_statement_run_exits_16_and_leaves_previous_files_untouched(
    batchEnvironment: BatchEnvironment,
    fillOverCapacity: Callable[[Engine], None],
    hasPrevious: bool,
) -> None:
    """Input past the 51 by 10 table ends as legacy_oracle.RunCreastmt predicts in-test: exit 16
    and no statement lines, so a fresh output directory stays without statement files, previous
    files keep their bytes, and step 17 is FAILED."""
    engine = batchEnvironment.engine
    statementsDir = StatementPaths(batchEnvironment.outputDir)[0].parent
    previous = PublicationWritePair(statementsDir, "PREVIOUS") if hasPrevious else None
    fillOverCapacity(engine)
    oracleOutcome = RunOracleStatements(engine)
    assert oracleOutcome == legacy_oracle.StatementOutcome(
        psLines=[], htmlLines=[], exitCode=legacy_oracle.EXIT_ABEND
    )
    outcome = RunStatementJob(oracleOutcome.exitCode)
    assert CAPACITY_MESSAGE_PREFIX in outcome.output
    assert PublicationFiles(statementsDir) == previous
    statementStep = StepRow(engine, STATEMENT_STEP_NO)
    assert statementStep["state"] == run_ledger.STEP_FAILED
