"""Replaces: CBSTM03A (statement writer); copybooks COSTM01, CUSTREC, CVACT01Y, CVACT03Y.

A pure builder: it takes the rows cbstm03b.py reads, keyed by PostgreSQL column names, does no
I/O, and returns the exact record images. STATEMNT.PS lines are the 80-column ST-LINE0 to
ST-LINE15 layouts; STATEMNT.HTML lines are 100 columns, rendered through
templates/statement.html.j2. The TRNXFILE table keeps CBSTM03A's 51-card by 10-transaction
bound, and larger input raises StatementCapacityError before any statement is built.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from functools import lru_cache
from pathlib import Path

from app.utils.cobol_format import (
    STATEMENT_AMOUNT_PICTURE,
    STATEMENT_BALANCE_PICTURE,
    FormatEdited,
)
from app.utils.decimal_utils import COBOL_CONTEXT, EncodeZoned, StoreInto
from jinja2 import Environment, FileSystemLoader, StrictUndefined

# FD-STMTFILE-REC PIC X(80) and FD-HTMLFILE-REC PIC X(100).
STATEMENT_LINE_WIDTH = 80
HTML_LINE_WIDTH = 100
# WS-CARD-TBL OCCURS 51 TIMES and WS-TRAN-TBL OCCURS 10 TIMES (lines 225-233).
MAX_STATEMENT_CARDS = 51
MAX_TRANSACTIONS_PER_CARD = 10
TEMPLATE_DIRECTORY = Path(__file__).resolve().parent / "templates"
TEMPLATE_NAME = "statement.html.j2"

# PIC widths of the copybook source fields and of the STATEMENT-LINES and HTML-LINES receivers.
CARD_NUM_WIDTH = 16  # TRNX-CARD-NUM, XREF-CARD-NUM, WS-CARD-NUM X(16)
NAME_PART_WIDTH = 25  # CUST-FIRST-NAME, CUST-MIDDLE-NAME, CUST-LAST-NAME X(25)
ADDRESS_LINE_WIDTH = 50  # CUST-ADDR-LINE-1 to -3, ST-ADD1, ST-ADD2 X(50)
STATE_CODE_WIDTH = 2  # CUST-ADDR-STATE-CD X(02)
COUNTRY_CODE_WIDTH = 3  # CUST-ADDR-COUNTRY-CD X(03)
ZIP_CODE_WIDTH = 10  # CUST-ADDR-ZIP X(10)
STATEMENT_NAME_WIDTH = 75  # ST-NAME X(75)
ADDRESS_LINE_3_WIDTH = 80  # ST-ADD3 X(80)
HTML_NAME_WIDTH = 50  # L23-NAME X(50)
DETAIL_FIELD_WIDTH = 20  # ST-ACCT-ID, ST-FICO-SCORE, L11-ACCT X(20)
TRAN_ID_WIDTH = 16  # TRNX-ID, ST-TRANID X(16)
TRAN_DESC_SOURCE_WIDTH = 100  # TRNX-DESC X(100)
TRAN_DETAILS_WIDTH = 49  # ST-TRANDT X(49)
EDITED_AMOUNT_WIDTH = 13  # ST-CURR-BAL 9(9).99-, ST-TRANAMT and ST-TOTAL-TRAMT Z(9).99-
FICO_DIGITS = 3  # CUST-FICO-CREDIT-SCORE 9(03)
# WS-TOTAL-AMT PIC S9(9)V99 COMP-3.
TOTAL_INT_DIGITS = 9
TOTAL_DEC_DIGITS = 2

# STRING delimiters: ' ' for the ST-NAME and ST-ADD3 parts, '  ' for the HTML name and address
# texts, '*' for the HTML basic-detail and transaction field texts.
WORD_DELIMITER = " "
HTML_TEXT_DELIMITER = "  "
HTML_FIELD_DELIMITER = "*"

# CBSTM03B return codes that make CBSTM03A abend: end of file on the first TRNXFILE read, and
# record not found on a keyed CUSTFILE or ACCTFILE read.
END_OF_FILE_STATUS = "10"
RECORD_NOT_FOUND_STATUS = "23"

# HTML records per statement: 22 from 5100-WRITE-HTML-HEADER, 34 from 5200-WRITE-HTML-NMADBS and
# the 8 closing lines of 4000-TRNXFILE-GET, plus 11 per transaction from 6000-WRITE-TRANS.
HTML_FIXED_LINE_COUNT = 64
HTML_LINES_PER_TRANSACTION = 11

type RecordMapping = Mapping[str, object]
type TransactionTable = Mapping[str, tuple[RecordMapping, ...]]


class StatementAbendError(Exception):
    """A CBSTM03A abend (9999-ABEND-PROGRAM, CALL 'CEE3ABD'); the CREASTMT step exits 16."""


class StatementCapacityError(StatementAbendError):
    """TRNXFILE needs more than the 51-card by 10-transaction WS-TRNX-TABLE can hold."""


class EmptyTransactionFileError(StatementAbendError):
    """The first TRNXFILE read returned end of file, which 8100-TRNXFILE-OPEN does not accept."""


class MissingStatementRecordError(StatementAbendError):
    """A keyed CUSTFILE or ACCTFILE read for a cross-reference record found no record."""


@dataclass(frozen=True)
class StatementData:
    """The records of one statement.

    xref, customer and account are CARD-XREF-RECORD, CUSTOMER-RECORD and ACCOUNT-RECORD;
    transactions is the TRNXFILE table entry of the xref card, empty when it has none.
    """

    xref: RecordMapping
    customer: RecordMapping
    account: RecordMapping
    transactions: tuple[RecordMapping, ...]


@dataclass(frozen=True)
class StatementOutput:
    """The STATEMNT.PS and STATEMNT.HTML record images, and what they hold.

    transactionCount is the number of ST-LINE14 transaction lines across all statements.
    """

    psLines: tuple[str, ...]
    htmlLines: tuple[str, ...]
    statementCount: int
    transactionCount: int


@dataclass(frozen=True)
class FieldSlot:
    """A field inside a line layout: the attribute holding its text, and its PIC width."""

    name: str
    width: int


@dataclass(frozen=True)
class StatementFields:
    """The STATEMENT-LINES fields of one statement, each at its PIC width."""

    name: str
    addressLine1: str
    addressLine2: str
    addressLine3: str
    accountId: str
    currentBalance: str
    ficoScore: str
    totalAmount: str


@dataclass(frozen=True)
class TransactionFields:
    """ST-TRANID, ST-TRANDT and ST-TRANAMT of one ST-LINE14."""

    tranId: str
    tranDetails: str
    tranAmount: str


@dataclass(frozen=True)
class HtmlTransactionLine:
    """The texts 6000-WRITE-TRANS strings into the three HTML transaction cells."""

    tranIdText: str
    tranDetailsText: str
    tranAmountText: str


# STATEMENT-LINES (lines 86-146): literal segments are the FILLER values at their PIC widths.
ST_LINE0 = ("*" * 31, "START OF STATEMENT", "*" * 31)
ST_LINE1 = (FieldSlot("name", STATEMENT_NAME_WIDTH), " " * 5)
ST_LINE2 = (FieldSlot("addressLine1", ADDRESS_LINE_WIDTH), " " * 30)
ST_LINE3 = (FieldSlot("addressLine2", ADDRESS_LINE_WIDTH), " " * 30)
ST_LINE4 = (FieldSlot("addressLine3", ADDRESS_LINE_3_WIDTH),)
ST_LINE5 = ("-" * 80,)
ST_LINE6 = (" " * 33, "Basic Details ", " " * 33)
ST_LINE7 = ("Account ID         :", FieldSlot("accountId", DETAIL_FIELD_WIDTH), " " * 40)
ST_LINE8 = (
    "Current Balance    :",
    FieldSlot("currentBalance", EDITED_AMOUNT_WIDTH),
    " " * 7,
    " " * 40,
)
ST_LINE9 = ("FICO Score         :", FieldSlot("ficoScore", DETAIL_FIELD_WIDTH), " " * 40)
ST_LINE10 = ("-" * 80,)
ST_LINE11 = (" " * 30, "TRANSACTION SUMMARY ", " " * 30)
ST_LINE12 = ("-" * 80,)
ST_LINE13 = ("Tran ID         ", "Tran Details    ".ljust(51), "  Tran Amount")
ST_LINE14 = (
    FieldSlot("tranId", TRAN_ID_WIDTH),
    " ",
    FieldSlot("tranDetails", TRAN_DETAILS_WIDTH),
    "$",
    FieldSlot("tranAmount", EDITED_AMOUNT_WIDTH),
)
ST_LINE14A = ("Total EXP:", " " * 56, "$", FieldSlot("totalAmount", EDITED_AMOUNT_WIDTH))
ST_LINE15 = ("*" * 32, "END OF STATEMENT", "*" * 32)

# The WRITE order of 5000-CREATE-STATEMENT (lines 460 and 488-502), before the transactions.
STATEMENT_HEADER_LAYOUTS = (
    ST_LINE0,
    ST_LINE1,
    ST_LINE2,
    ST_LINE3,
    ST_LINE4,
    ST_LINE5,
    ST_LINE6,
    ST_LINE5,
    ST_LINE7,
    ST_LINE8,
    ST_LINE9,
    ST_LINE10,
    ST_LINE11,
    ST_LINE12,
    ST_LINE13,
    ST_LINE12,
)
# The WRITE order at the end of 4000-TRNXFILE-GET (lines 435-437), after the transactions.
STATEMENT_TRAILER_LAYOUTS = (ST_LINE12, ST_LINE14A, ST_LINE15)

# The STRING sources of ST-NAME (lines 462-469) and ST-ADD3 (lines 472-481), at their PIC widths.
NAME_COLUMN_WIDTHS = (
    ("first_name", NAME_PART_WIDTH),
    ("middle_name", NAME_PART_WIDTH),
    ("last_name", NAME_PART_WIDTH),
)
ADDRESS_LINE_3_COLUMN_WIDTHS = (
    ("addr_line_3", ADDRESS_LINE_WIDTH),
    ("addr_state_cd", STATE_CODE_WIDTH),
    ("addr_country_cd", COUNTRY_CODE_WIDTH),
    ("addr_zip", ZIP_CODE_WIDTH),
)


def LoadTransactionTable(
    transactions: Sequence[RecordMapping],
) -> dict[str, tuple[RecordMapping, ...]]:
    """Load WS-TRNX-TABLE as 8100-TRNXFILE-OPEN, 8500-READTRNX-READ and 8599-EXIT do.

    transactions are in TRXFL key order: card number, then TRAN-ID. Each run of one card number
    is a table entry, and the result maps the card number, padded to X(16), to its records in
    read order. Raises EmptyTransactionFileError when there is no record, and
    StatementCapacityError beyond 51 entries or 10 records in one entry, before returning.
    """
    records = tuple(transactions)
    if not records:
        raise EmptyTransactionFileError(ReadErrorMessage("TRNXFILE", END_OF_FILE_STATUS))
    entries: list[tuple[str, list[RecordMapping]]] = []
    for record in records:
        AppendTableEntry(entries, record)
    table: dict[str, tuple[RecordMapping, ...]] = {}
    # TRXFL is a KSDS keyed by card and TRAN-ID, so a card forms one entry. Input out of that
    # order keeps every record: the 4000 scan prints each entry equal to the xref card.
    for cardKey, entryRecords in entries:
        table[cardKey] = table.get(cardKey, ()) + tuple(entryRecords)
    return table


def AppendTableEntry(entries: list[tuple[str, list[RecordMapping]]], record: RecordMapping) -> None:
    """Add one TRNXFILE record to the table, as 8500-READTRNX-READ does.

    A card number other than the previous record's starts a new entry (ADD 1 TO CR-CNT);
    otherwise the record joins the current entry (ADD 1 TO TR-CNT). The table positions are
    1-based in the messages, and no card number is ever put in one.
    """
    cardKey = PadText(record["card_num"], CARD_NUM_WIDTH)
    if not entries or entries[-1][0] != cardKey:
        if len(entries) == MAX_STATEMENT_CARDS:
            raise StatementCapacityError(
                f"STATEMENT TABLE CAPACITY EXCEEDED: MORE THAN {MAX_STATEMENT_CARDS} CARDS"
                f" AT TABLE ENTRY {len(entries) + 1}"
            )
        entries.append((cardKey, []))
    entryRecords = entries[-1][1]
    if len(entryRecords) == MAX_TRANSACTIONS_PER_CARD:
        raise StatementCapacityError(
            "STATEMENT TABLE CAPACITY EXCEEDED: MORE THAN"
            f" {MAX_TRANSACTIONS_PER_CARD} TRANSACTIONS FOR ONE CARD AT TABLE ENTRY {len(entries)}"
        )
    entryRecords.append(record)


def AssembleStatementData(
    xref: RecordMapping,
    customer: RecordMapping | None,
    account: RecordMapping | None,
    table: TransactionTable,
) -> StatementData:
    """Gather one statement's records, as 1000-MAINLINE does after 1000-XREFFILE-GET-NEXT.

    2000-CUSTFILE-GET runs before 3000-ACCTFILE-GET, and each abends on any status but 00, so a
    missing customer, then a missing account, raises MissingStatementRecordError. The
    transactions are the table entry 4000-TRNXFILE-GET finds for the xref card, or none.
    """
    if customer is None:
        raise MissingStatementRecordError(ReadErrorMessage("CUSTFILE", RECORD_NOT_FOUND_STATUS))
    if account is None:
        raise MissingStatementRecordError(ReadErrorMessage("ACCTFILE", RECORD_NOT_FOUND_STATUS))
    transactions = table.get(PadText(xref["card_num"], CARD_NUM_WIDTH), ())
    return StatementData(xref, customer, account, tuple(transactions))


def BuildStatementFiles(
    table: TransactionTable, statements: Sequence[StatementData]
) -> StatementOutput:
    """Build both statement files, one statement per cross-reference, as 1000-MAINLINE does.

    statements are in XREFFILE (card) order. A table card with no cross-reference is never
    printed, and a cross-reference with no table entry gets an empty transaction summary.
    """
    # 8100-TRNXFILE-OPEN loads the table before the first statement and abends on an empty
    # TRNXFILE, so no statement is built without a loaded table.
    if not table:
        raise EmptyTransactionFileError(ReadErrorMessage("TRNXFILE", END_OF_FILE_STATUS))
    psLines: list[str] = []
    htmlLines: list[str] = []
    for statement in statements:
        statementPsLines, statementHtmlLines = BuildStatement(statement)
        psLines.extend(statementPsLines)
        htmlLines.extend(statementHtmlLines)
    transactionCount = sum(len(statement.transactions) for statement in statements)
    return StatementOutput(tuple(psLines), tuple(htmlLines), len(statements), transactionCount)


def BuildStatement(data: StatementData) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return one statement's PS and HTML lines.

    Ports 5000-CREATE-STATEMENT, 5100-WRITE-HTML-HEADER, 5200-WRITE-HTML-NMADBS, one
    6000-WRITE-TRANS per transaction, and the totals and closing lines of 4000-TRNXFILE-GET.
    """
    fields = BuildStatementFields(data)
    transactionFields = tuple(BuildTransactionFields(record) for record in data.transactions)
    return RenderPsLines(fields, transactionFields), RenderHtmlLines(fields, transactionFields)


def BuildStatementFields(data: StatementData) -> StatementFields:
    """Fill STATEMENT-LINES as 5000-CREATE-STATEMENT and 4000-TRNXFILE-GET do.

    ST-ACCT-ID and ST-FICO-SCORE receive the 9(11) and 9(03) digits left-justified; ST-CURR-BAL
    drops the top digit of the S9(10)V99 balance, as the 9(9).99- picture holds nine.
    """
    customer = data.customer
    account = data.account
    ficoDigits = EncodeZoned(customer["fico_credit_score"], FICO_DIGITS, 0, signed=False)
    return StatementFields(
        name=StringFirstWords(customer, NAME_COLUMN_WIDTHS, STATEMENT_NAME_WIDTH),
        addressLine1=PadText(customer["addr_line_1"], ADDRESS_LINE_WIDTH),
        addressLine2=PadText(customer["addr_line_2"], ADDRESS_LINE_WIDTH),
        addressLine3=StringFirstWords(customer, ADDRESS_LINE_3_COLUMN_WIDTHS, ADDRESS_LINE_3_WIDTH),
        accountId=PadText(account["acct_id"], DETAIL_FIELD_WIDTH),
        currentBalance=FormatEdited(account["curr_balance"], STATEMENT_BALANCE_PICTURE),
        ficoScore=PadText(ficoDigits, DETAIL_FIELD_WIDTH),
        totalAmount=FormatEdited(SumTransactions(data.transactions), STATEMENT_AMOUNT_PICTURE),
    )


def BuildTransactionFields(record: RecordMapping) -> TransactionFields:
    """Move one table record into ST-LINE14, as 6000-WRITE-TRANS does (lines 676-678)."""
    return TransactionFields(
        tranId=PadText(record["tran_id"], TRAN_ID_WIDTH),
        tranDetails=PadText(record["tran_desc"], TRAN_DESC_SOURCE_WIDTH)[:TRAN_DETAILS_WIDTH],
        tranAmount=FormatEdited(record["tran_amt"], STATEMENT_AMOUNT_PICTURE),
    )


def SumTransactions(transactions: Sequence[RecordMapping]) -> Decimal:
    """Return WS-TOTAL-AMT: MOVE ZERO, then ADD TRNX-AMT per printed transaction (lines 325, 429).

    Each ADD stores into S9(9)V99 without ON SIZE ERROR, so high-order digits truncate.
    """
    totalAmount = Decimal("0.00")
    with localcontext(COBOL_CONTEXT):
        for record in transactions:
            totalAmount = StoreInto(
                totalAmount + record["tran_amt"], TOTAL_INT_DIGITS, TOTAL_DEC_DIGITS
            )
    return totalAmount


def StringFirstWords(
    record: RecordMapping, columnWidths: tuple[tuple[str, int], ...], receiverWidth: int
) -> str:
    """STRING each field DELIMITED BY ' ' and then ' ' INTO a space-filled receiver.

    Each field is first padded to its PIC width, as the record area holds it, so a blank field
    contributes only the ' ' that follows it. Text beyond the receiver is lost (no ON OVERFLOW).
    """
    words = (
        DelimitedBy(PadText(record[column], width), WORD_DELIMITER) + " "
        for column, width in columnWidths
    )
    return PadText("".join(words), receiverWidth)


def PadText(value: object, width: int) -> str:
    """MOVE a value into a PIC X(width) receiver: truncate on the right, pad with spaces.

    Database text columns are right-trimmed, so padding restores the fixed-width field. A NULL
    column moves as spaces.
    """
    text = "" if value is None else str(value)
    return text[:width].ljust(width)


def DelimitedBy(text: str, delimiter: str) -> str:
    """Return what a STRING phrase DELIMITED BY delimiter transfers from text.

    That is the text before the first occurrence of delimiter, or all of it when it is absent.
    """
    return text.split(delimiter, 1)[0]


def FitLine(text: str, width: int) -> str:
    """WRITE a record of PIC X(width): truncate on the right and pad with spaces."""
    return text[:width].ljust(width)


def RenderPsLine(layout: tuple[str | FieldSlot, ...], source: object) -> str:
    """Lay out one STMT-FILE record: literals as written, each FieldSlot as its field's text."""
    segments = (
        PadText(getattr(source, segment.name), segment.width)
        if isinstance(segment, FieldSlot)
        else segment
        for segment in layout
    )
    return FitLine("".join(segments), STATEMENT_LINE_WIDTH)


def RenderPsLines(
    fields: StatementFields, transactionFields: tuple[TransactionFields, ...]
) -> tuple[str, ...]:
    """Return the statement's STMT-FILE records: 16 header lines, one ST-LINE14 per transaction
    (6000-WRITE-TRANS), then ST-LINE12, ST-LINE14A and ST-LINE15 (4000-TRNXFILE-GET)."""
    headerLines = [RenderPsLine(layout, fields) for layout in STATEMENT_HEADER_LAYOUTS]
    transactionLines = [RenderPsLine(ST_LINE14, transaction) for transaction in transactionFields]
    trailerLines = [RenderPsLine(layout, fields) for layout in STATEMENT_TRAILER_LAYOUTS]
    return tuple(headerLines + transactionLines + trailerLines)


def BuildHtmlContext(
    fields: StatementFields, transactionFields: tuple[TransactionFields, ...]
) -> dict[str, object]:
    """Return the template's field texts, cut as the HTML MOVE and STRING statements cut them.

    L11-ACCT is moved whole (5100-WRITE-HTML-HEADER, line 529). The name, from ST-NAME's first
    50 characters, and the address lines end at the first double space (5200-WRITE-HTML-NMADBS,
    lines 560-592); the basic-detail texts end at the first '*' (lines 613-633).
    """
    return {
        "accountNumberText": fields.accountId,
        "nameText": DelimitedBy(fields.name[:HTML_NAME_WIDTH], HTML_TEXT_DELIMITER),
        "addressLine1Text": DelimitedBy(fields.addressLine1, HTML_TEXT_DELIMITER),
        "addressLine2Text": DelimitedBy(fields.addressLine2, HTML_TEXT_DELIMITER),
        "addressLine3Text": DelimitedBy(fields.addressLine3, HTML_TEXT_DELIMITER),
        "accountIdText": DelimitedBy(fields.accountId, HTML_FIELD_DELIMITER),
        "currentBalanceText": DelimitedBy(fields.currentBalance, HTML_FIELD_DELIMITER),
        "ficoScoreText": DelimitedBy(fields.ficoScore, HTML_FIELD_DELIMITER),
        "transactions": tuple(BuildHtmlTransactionLine(line) for line in transactionFields),
    }


def BuildHtmlTransactionLine(transaction: TransactionFields) -> HtmlTransactionLine:
    """Cut ST-TRANID, ST-TRANDT and ST-TRANAMT as 6000-WRITE-TRANS strings them (lines 686-716).

    Each is STRING ... DELIMITED BY '*', so a field text ends at its first '*' and is otherwise
    whole, trailing spaces included.
    """
    return HtmlTransactionLine(
        tranIdText=DelimitedBy(transaction.tranId, HTML_FIELD_DELIMITER),
        tranDetailsText=DelimitedBy(transaction.tranDetails, HTML_FIELD_DELIMITER),
        tranAmountText=DelimitedBy(transaction.tranAmount, HTML_FIELD_DELIMITER),
    )


def RenderHtmlLines(
    fields: StatementFields, transactionFields: tuple[TransactionFields, ...]
) -> tuple[str, ...]:
    """Return the statement's HTML-FILE records from templates/statement.html.j2.

    Ports 5100-WRITE-HTML-HEADER, 5200-WRITE-HTML-NMADBS, 6000-WRITE-TRANS and the closing
    lines of 4000-TRNXFILE-GET. Raises ValueError when the rendering does not have the
    64 + 11 * n records CBSTM03A writes, which a defective template or a field value holding a
    line break would cause.
    """
    template = BuildTemplateEnvironment().get_template(TEMPLATE_NAME)
    renderedText = template.render(BuildHtmlContext(fields, transactionFields))
    renderedLines = renderedText.split("\n")
    if renderedLines[-1] == "":
        renderedLines.pop()
    expectedCount = HTML_FIXED_LINE_COUNT + HTML_LINES_PER_TRANSACTION * len(transactionFields)
    if len(renderedLines) != expectedCount:
        raise ValueError(
            f"{TEMPLATE_NAME} rendered {len(renderedLines)} lines for"
            f" {len(transactionFields)} transactions; CBSTM03A writes {expectedCount}"
        )
    # Padded to the record width and never cut: escaping lengthens a field value, and a cut at
    # column 100 could split an entity. Unescaped records stay within 89 columns.
    return tuple(line.ljust(HTML_LINE_WIDTH) for line in renderedLines)


def EscapeFieldValue(value: object) -> str:
    """Escape &, < and > in a template field value, the Jinja2 finalize hook.

    Apostrophes and quotes stay as CBSTM03A wrote them, for byte parity; the three escaped
    characters keep user-entered text, such as a CT02 description, from injecting markup.
    """
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@lru_cache(maxsize=1)
def BuildTemplateEnvironment() -> Environment:
    """Return the Jinja2 environment for templates/statement.html.j2, built on first use."""
    # autoescape stays off because HTML autoescaping also rewrites the apostrophes in the
    # sample customer and transaction text, which breaks byte parity with CBSTM03A. Field
    # values are cleaned by EscapeFieldValue through finalize instead (MK_Modernization Rule
    # clause 4). auto_reload is off so a cached template is never re-checked on disk.
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIRECTORY),
        autoescape=False,
        auto_reload=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
        finalize=EscapeFieldValue,
    )


def ReadErrorMessage(ddName: str, returnCode: str) -> str:
    """Return the DISPLAY text CBSTM03A shows before 9999-ABEND-PROGRAM for a failed read."""
    return f"ERROR READING {ddName} RETURN CODE: {returnCode}"
