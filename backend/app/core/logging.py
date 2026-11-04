"""Standard-library logging to stdout, with card numbers masked and card references redacted.

Replaces: the CICS region and JES SYSOUT logs of CardDemo; masks copybook CVACT02Y
CARD-NUM PIC X(16). Specified in the Technical Specification, Section 0.6.5 (Logging).

Card numbers reach the logs in request paths and query strings (/api/v1/cards/{cardNum},
cardNum=). Every run of exactly 16 ASCII digits is therefore shown as twelve asterisks plus
its last four digits, both in the Uvicorn access log and in application logs. A card number
can also sit inside a longer digit run, such as a mistyped 19-digit key or one glued to the
%20 Uvicorn logs for a space, or be written in groups of any size split by spaces, dashes or
%20. A digit chain, groups of digits joined throughout by one of those separators or a lone
run, that holds 16 or more digits therefore keeps only its last four digits, behind one
asterisk per hidden digit; four or more four-digit groups, even with mixed separators, become
twelve asterisks plus the last group. A 16-digit transaction ID (CVTRA05Y TRAN-ID) is masked
as well: the filter cannot tell the two apart, and over-masking is the safe direction.
Shorter chains stay readable, such as 11-digit account IDs, dates, timestamps and two dates
joined by a space. SSNs are not handled here, because they never appear in URLs and services
mask them with security.MaskSsn before logging.

The encrypted card_ref and screen_token navigation tokens are redacted whole: every Fernet
token wherever it appears, any path segment after /api/v1/cards/ other than a card number or
the route segment "selection", and the values of the cardNum, card_ref, cardRef, screen_token
and screenToken query parameters. The startKey of GET /api/v1/cards is a card_ref and is
redacted too; the startKey of the other lists is a plain key and stays readable. Every JWT,
the form of the session cookie, is redacted wherever it appears.

Uvicorn logs a request target's query exactly as the client sent it, for every response
status, so a mistaken or hostile request can carry a password, an SSN, a CVV, the session
token or a connection string under any parameter name, or in the value of a declared one. A
request target, a query whose decoded path holds a / or is the * of OPTIONS *, therefore
keeps only what its route declares. ROUTE_QUERY_SHAPES holds each route's declaration, a
safe-logging allow-list rather than the route's parameter list. It matches the end of the
decoded path to a GET route, and gives the wire names that route's log keeps readable, each
with the shape of its value: acctId is up to 11 digits, direction is forward or backward, and
so on. A pair whose name the route does not declare, even one the route reads, is replaced
whole by [REDACTED], its name included; every path no entry matches, such as auth, menu,
accounts, billpay and health, declares none. A declared value without its shape is replaced
by [REDACTED] after the name. One with its shape is written as sent unless it holds a card
number, which is masked. cardNum and the card-list startKey keep the card handling above.
Names and values are judged decoded, as Starlette decodes them, so %70assword=... or
pass+word=... is redacted like password=..., and repeated pairs are judged one by one. The
text after a ? elsewhere, such as "what?x" in a message, has each parameter judged by its
decoded value alone.

Uvicorn's TRACE log writes each ASGI scope, whose query_string and raw_path bytes do not take
the path?query form. Before a record is rendered, each scope among its args is replaced by a
copy whose query_string is masked as the query of a request target on the scope's path, in
its own bytes or str type, and whose raw_path is shown as <...>, the placeholder Uvicorn
gives the headers.

A stored value that fails a response or screen model, such as a customer's name, reaches the
error log in the traceback of the unexpected 500. The input values that Pydantic's
ValidationError and FastAPI's validation exceptions echo are therefore redacted wherever they
appear, keeping each error's type, location and message.

SQLAlchemy's pool writes a DBAPI connection's repr in the warnings and errors it still logs,
such as a failed close, and psycopg2's repr carries the connection's libpq dsn: the database
user, host, port and database name. Every such dsn is replaced by [REDACTED] at every level,
keeping the rest of the repr.

Masking covers each record's message, args, traceback and stack text. The root logger's
stdout handler comes first among the root handlers, so every other root handler receives
the record already masked. Each Uvicorn logger and handler masks too, and Uvicorn's error
log, which its default configuration writes to stderr, is written to stdout instead. Uvicorn's
handlers write the stdout handler's LOG_LINE_FORMAT, so its access and server lines carry the
timestamp that application and batch lines carry, and the logs can be ordered together.

This module imports only the standard library, so batch and the backend can both call
ConfigureLogging() without any settings in the environment.
"""

import logging
import re
import sys
import types
import urllib.parse
from collections.abc import Mapping
from typing import TextIO

PAN_RUN_PATTERN = re.compile(r"(?<![0-9])[0-9]{12}([0-9]{4})(?![0-9])")
MASKED_PAN_REPLACEMENT = r"************\1"
# Every mask keeps exactly four digits, behind at least twelve asterisks and before a non-digit.
# No digit group of exactly four digits right after twelve asterisks starts a match, so a mask's
# kept digits never join the groups after them, and masking again changes nothing. A run of any
# other length after twelve asterisks is no mask's, and is matched like any other. Twelve literal
# asterisks before the first four-digit group of a spaced card number look like a mask, so that
# group starts no match and the groups after it are judged without it.
AFTER_MASK_GUARD = r"(?!(?<=\*{12})[0-9]{4}(?![0-9]))"
# A card number written as four-digit groups split by a space, a dash or %20, the form a space
# takes in a logged path. Each separator is independent, and a preceding %20 may open the run.
DIGIT_GROUP_SEPARATOR = r"(?: |-|%20)"
SEPARATED_DIGIT_GROUPS_PATTERN = re.compile(
    rf"(?:(?<![0-9])|(?<=%20)){AFTER_MASK_GUARD}[0-9]{{4}}"
    rf"(?:{DIGIT_GROUP_SEPARATOR}[0-9]{{4}}){{2,}}{DIGIT_GROUP_SEPARATOR}([0-9]{{4}})(?![0-9])"
)
# A digit chain: groups of digits joined throughout by one separator, a space, a dash or %20,
# starting where no digit precedes it. A lone run is a chain of one group. A card number of
# any grouping, or one glued into a longer run, therefore lies in one chain, while two dates
# joined by a space stay two chains. Possessive quantifiers scan each chain once.
DIGIT_CHAIN_PATTERN = re.compile(
    rf"(?<![0-9]){AFTER_MASK_GUARD}[0-9]++(?:({DIGIT_GROUP_SEPARATOR})[0-9]++(?:\1[0-9]++)*+)?+"
)
CARD_NUMBER_DIGIT_COUNT = 16
VISIBLE_DIGIT_COUNT = 4
MASK_CHARACTER = "*"
REDACTED_REPLACEMENT = "[REDACTED]"
# A Fernet token is URL-safe base64 of version byte 0x80 and a 64-bit timestamp, so it opens
# with gAAAAA; its = padding is %3D in the percent-encoded path Uvicorn logs.
FERNET_TOKEN_PATTERN = re.compile(r"gAAAAA[0-9A-Za-z_-]{20,}(?:=|%3[Dd])*")
# The session cookie is a JWT: three base64url segments joined by dots. Its header is JSON,
# whose opening {" encodes as eyJ; an unsigned token has an empty third segment. A match opens
# only at the start of a base64url run and takes the whole run atomically, so text glued to a
# token is redacted with it and each run is scanned once, keeping long log lines linear.
BASE64URL_CHARACTER = "[0-9A-Za-z_-]"
JWT_PATTERN = re.compile(
    rf"(?<!{BASE64URL_CHARACTER})(?>{BASE64URL_CHARACTER}*?eyJ{BASE64URL_CHARACTER}*+)"
    rf"\.{BASE64URL_CHARACTER}++\.{BASE64URL_CHARACTER}*+"
)
# Pydantic ends each error line of a ValidationError with [type=<error type>, input_value=<repr>,
# input_type=<class name>]. The repr may itself hold ", input_type=str]", so the value runs to
# the input_type that ends the line. Each line is tried once, from its start: the lookahead
# finds that ending before the value is sought, so a long log line is scanned in linear time.
VALIDATION_INPUT_VALUE_PATTERN = re.compile(
    r"^(?=[^\n]*, input_type=\w++\]$)"
    r"([^\n]*?\[type=\w++, input_value=)[^\n]*(, input_type=\w++\])$",
    re.MULTILINE,
)
VALIDATION_INPUT_VALUE_REPLACEMENT = rf"\g<1>{REDACTED_REPLACEMENT}\g<2>"
# FastAPI's validation exceptions print each error dict on a line, its 'input' after 'type',
# 'loc' and 'msg'. The repr may hold any text, so the input and the rest of its line are
# replaced. The quote that opens the key is never written by masking, which writes no quote,
# so masking never makes a new match and masking its own result changes nothing.
VALIDATION_INPUT_KEY_PATTERN = re.compile(r"( 'input': )[^\n]*+")
VALIDATION_INPUT_KEY_REPLACEMENT = rf"\g<1>{REDACTED_REPLACEMENT}"
# psycopg2 writes a connection as <connection object at 0x...; dsn: '...', closed: 0>, its libpq
# dsn unescaped, and SQLAlchemy's pool logs that repr in its warnings and errors. A dsn value
# may itself be quoted, as in dbname='my db', so the dsn runs to the last ', closed: <n>> on its
# line; with no such tail, to the line's last quote, or else to the line's end. A match without
# a tail leaves no quote on its line to start another, so a long line is still scanned in linear
# time. The replacement writes no quote, so masking its own result again changes nothing.
CONNECTION_DSN_PATTERN = re.compile(
    r"dsn: '(?>[^\n]*(?=', closed: [0-9]++>)|[^\n]*(?=')|[^\n]*+)'?"
)
CONNECTION_DSN_REPLACEMENT = f"dsn: {REDACTED_REPLACEMENT}"
# The slots are anchored to the API path and to query separators, so traceback file paths
# such as services/cards/ and keyword arguments in traceback source lines stay readable.
# Neither slot value takes in a ?, so redacting one never moves where a request query starts.
CARD_PATH_SLOT_PATTERN = re.compile(r"(/api/v1/cards/)([^/?#\s\"']+)")
CARD_QUERY_SLOT_PATTERN = re.compile(
    r"((?:^|[?&])(?:cardNum|card_ref|cardRef|screen_token|screenToken)=)([^?&#\s\"']+)"
)
CARD_NUMBER_PATTERN = re.compile(r"[0-9]{16}")
MASKED_CARD_NUMBER_PATTERN = re.compile(r"\*{12}[0-9]{4}")
# POST /api/v1/cards/selection is the one fixed route segment after /api/v1/cards/.
VISIBLE_CARD_SLOT_VALUES = frozenset({REDACTED_REPLACEMENT, "selection"})
# A request target: the text before its first ? and the query after it. Starlette reads #,
# quotes and every other non-space character of a logged query as query text, so only
# whitespace ends it. Matching from the start of a whitespace-free run keeps this linear.
REQUEST_QUERY_PATTERN = re.compile(r"(?<!\S)([^\s?]*)\?(\S*)")
CARD_REFERENCE_QUERY_NAMES = frozenset({"card_ref", "cardRef", "screen_token", "screenToken"})
CARD_NUMBER_QUERY_NAME = "cardNum"
# The value shapes of the declared query parameters (schemas.cards, transactions, users and
# reports), each matched against the whole decoded value. A value of any other shape, which
# the API rejects or reads as free text, is redacted. A 16-digit transaction key is logged
# masked, so its shape also admits that mask and masking a logged query again changes nothing.
ACCOUNT_ID_SHAPE = re.compile(r"[0-9]{0,11}")
TRANSACTION_KEY_SHAPE = re.compile(r"[0-9]{0,16}|\*{12}[0-9]{4}")
USER_KEY_SHAPE = re.compile(r"[0-9A-Za-z]{0,8}")
DIRECTION_SHAPE = re.compile(r"forward|backward")
PAGE_SHAPE = re.compile(r"[0-9]{1,9}")
USER_SCREEN_SHAPE = re.compile(r"update|delete")
REPORT_TYPE_SHAPE = re.compile(r"monthly|yearly|custom")
DATE_PART_SHAPE = re.compile(r"[0-9]{0,2}")
YEAR_SHAPE = re.compile(r"[0-9]{0,4}")
CONFIRM_SHAPE = re.compile(r"[A-Za-z]")
SELECTION_CODE_SHAPE = re.compile(r"[A-Za-z]")
EXPORT_FORMAT_SHAPE = re.compile(r"csv|pdf")
# Marks a declared parameter that carries a card number or a card_ref token, whose value
# RedactQueryValue shows instead of a shape. It matches no text, so it never admits a value.
CARD_PARAMETER_SHAPE = re.compile(r"(?!)")
CARD_LIST_QUERY_SHAPES = types.MappingProxyType(
    {
        "acctId": ACCOUNT_ID_SHAPE,
        CARD_NUMBER_QUERY_NAME: CARD_PARAMETER_SHAPE,
        "startKey": CARD_PARAMETER_SHAPE,
        "direction": DIRECTION_SHAPE,
        "page": PAGE_SHAPE,
        "selections": SELECTION_CODE_SHAPE,
    }
)
# GET /cards/{cardNum} and its /edit form log acctId by its shape. Any other name, the isCardRef
# flag these routes read included, is redacted whole. cardNum, which they do not read as a query
# parameter, is admitted only so that a card number sent as its value is masked as on the card
# list, and a raw-PAN request line logs its path and query card numbers alike.
CARD_KEY_QUERY_SHAPES = types.MappingProxyType(
    {"acctId": ACCOUNT_ID_SHAPE, CARD_NUMBER_QUERY_NAME: CARD_PARAMETER_SHAPE}
)
TRANSACTION_LIST_QUERY_SHAPES = types.MappingProxyType(
    {
        "searchTranId": TRANSACTION_KEY_SHAPE,
        "startKey": TRANSACTION_KEY_SHAPE,
        "firstKey": TRANSACTION_KEY_SHAPE,
        "direction": DIRECTION_SHAPE,
        "page": PAGE_SHAPE,
    }
)
USER_LIST_QUERY_SHAPES = types.MappingProxyType(
    {
        "searchUserId": USER_KEY_SHAPE,
        "startKey": USER_KEY_SHAPE,
        "direction": DIRECTION_SHAPE,
        "page": PAGE_SHAPE,
    }
)
USER_KEY_QUERY_SHAPES = types.MappingProxyType({"screen": USER_SCREEN_SHAPE})
REPORT_QUERY_SHAPES = types.MappingProxyType(
    {
        "reportType": REPORT_TYPE_SHAPE,
        "startMm": DATE_PART_SHAPE,
        "startDd": DATE_PART_SHAPE,
        "startYyyy": YEAR_SHAPE,
        "endMm": DATE_PART_SHAPE,
        "endDd": DATE_PART_SHAPE,
        "endYyyy": YEAR_SHAPE,
        "confirm": CONFIRM_SHAPE,
    }
)
REPORT_EXPORT_QUERY_SHAPES = types.MappingProxyType(
    {**REPORT_QUERY_SHAPES, "format": EXPORT_FORMAT_SHAPE}
)
NO_QUERY_SHAPES = types.MappingProxyType({})
# The GET routes that read query parameters, matched against the end of the decoded request
# path, so a full URL or a root_path prefix still matches; a trailing slash is allowed. Each
# list route precedes its key route. Every other path, such as auth, menu, accounts, billpay
# and health, declares no query parameter.
ROUTE_QUERY_SHAPES = (
    (re.compile(r"/api/v1/cards/?\Z"), CARD_LIST_QUERY_SHAPES),
    (re.compile(r"/api/v1/cards/[^/]++(?:/edit)?/?\Z"), CARD_KEY_QUERY_SHAPES),
    (re.compile(r"/api/v1/transactions/?\Z"), TRANSACTION_LIST_QUERY_SHAPES),
    (re.compile(r"/api/v1/admin/users/?\Z"), USER_LIST_QUERY_SHAPES),
    (re.compile(r"/api/v1/admin/users/[^/]++/?\Z"), USER_KEY_QUERY_SHAPES),
    (re.compile(r"/api/v1/reports/transactions/?\Z"), REPORT_QUERY_SHAPES),
    (re.compile(r"/api/v1/reports/transactions/export/?\Z"), REPORT_EXPORT_QUERY_SHAPES),
)
# OPTIONS * sends the asterisk-form request target, the one request target that can carry a
# query without a /. Uvicorn logs it percent-encoded as %2A.
ASTERISK_FORM_TARGET = "*"
# Uvicorn's TRACE log writes each ASGI scope with its headers shown as <...>. The projection
# masks its query_string as a request query and shows its raw_path bytes the same way.
SCOPE_QUERY_KEY = "query_string"
SCOPE_RAW_PATH_KEY = "raw_path"
SCOPE_PATH_KEY = "path"
OMITTED_CONTENT_PLACEHOLDER = "<...>"
# Latin-1 maps every byte to one character and back, so a decoded query re-encodes losslessly.
SCOPE_QUERY_ENCODING = "latin-1"
# Characters a masked query name or value keeps unencoded. &, =, +, %, # and quotes are
# encoded, so the masked text decodes to itself and keeps the query's pairs apart.
QUERY_TEXT_SAFE = "!$()*,/:;?@[]"
HANDLER_NAME = "carddemo-stdout"
UVICORN_ACCESS_LOGGER = "uvicorn.access"
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", UVICORN_ACCESS_LOGGER, "uvicorn.asgi")
LOG_LINE_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
# One formatter for the stdout handler and every Uvicorn handler, so their lines share one
# timestamped form and can be ordered together. A formatter keeps no per-record state.
LOG_LINE_FORMATTER = logging.Formatter(LOG_LINE_FORMAT)
TRACEBACK_FORMATTER = logging.Formatter()


def MaskPanRuns(text: str) -> str:
    """Return text with each 16-digit run shown as twelve asterisks and its last four digits."""
    return PAN_RUN_PATTERN.sub(MASKED_PAN_REPLACEMENT, text)


def MaskDigitChain(chainMatch: re.Match[str]) -> str:
    """Return a digit chain of 16 or more digits as asterisks and its last four digits.

    Only the digits of its groups count, never those of a %20 separator. The separators are
    dropped, and each hidden digit becomes one asterisk. A shorter chain is returned as it is.
    """
    chainText, separator = chainMatch.group(0), chainMatch.group(1)
    chainDigits = chainText.replace(separator, "") if separator else chainText
    if len(chainDigits) < CARD_NUMBER_DIGIT_COUNT:
        return chainText
    hiddenCount = len(chainDigits) - VISIBLE_DIGIT_COUNT
    return MASK_CHARACTER * hiddenCount + chainDigits[hiddenCount:]


def MaskDigitRuns(text: str) -> str:
    """Return text with every digit chain that may hold a card number shown by its last 4 digits.

    Four or more four-digit groups split by spaces, dashes or %20, in any mix, become twelve
    asterisks and the last group. Then each digit chain, a lone run or groups joined by one
    repeated separator, that holds 16 or more digits is masked by MaskDigitChain, whatever its
    grouping, so every run of 16 or more digits is masked, even one right after asterisks.
    Neither step starts at a group of exactly four digits right after twelve asterisks, the
    digits a mask keeps (AFTER_MASK_GUARD), so masking the result again changes nothing.
    Shorter chains, such as 11-digit account IDs, 9-digit customer IDs, dates, timestamps,
    addresses with ports and two dates joined by a space, stay readable.
    """
    maskedText = SEPARATED_DIGIT_GROUPS_PATTERN.sub(MASKED_PAN_REPLACEMENT, text)
    return DIGIT_CHAIN_PATTERN.sub(MaskDigitChain, maskedText)


def RedactCardSlotValue(slotValue: str) -> str:
    """Return a card slot's value with a card number masked and a token redacted.

    A slot holds a 16-digit card number, which keeps its last four digits, or an opaque
    card_ref or screen_token, which is replaced whole. A masked number, the redaction marker
    and the fixed segment "selection" are kept, so masking a result again changes nothing.
    """
    if CARD_NUMBER_PATTERN.fullmatch(slotValue):
        return MaskPanRuns(slotValue)
    if MASKED_CARD_NUMBER_PATTERN.fullmatch(slotValue) or slotValue in VISIBLE_CARD_SLOT_VALUES:
        return slotValue
    return REDACTED_REPLACEMENT


def RedactCardSlot(slotMatch: re.Match[str]) -> str:
    return slotMatch.group(1) + RedactCardSlotValue(slotMatch.group(2))


def MaskPlainText(text: str) -> str:
    """Return text, read literally, with tokens and card slots redacted and card numbers masked.

    Every Fernet token and every JWT, such as the session cookie, is redacted wherever it
    appears. Every digit run that may hold a card number is then masked by MaskDigitRuns,
    before the card slots, so a slot value that stops at a space cannot leave the later groups
    of a spaced card number unmasked. Last, the path segment after /api/v1/cards/ and the value
    of a cardNum, card_ref, cardRef, screen_token or screenToken parameter go through
    RedactCardSlot, which keeps a masked card number and the route segment "selection" and
    redacts any other value.
    """
    redactedText = FERNET_TOKEN_PATTERN.sub(REDACTED_REPLACEMENT, text)
    redactedText = JWT_PATTERN.sub(REDACTED_REPLACEMENT, redactedText)
    redactedText = MaskDigitRuns(redactedText)
    redactedText = CARD_PATH_SLOT_PATTERN.sub(RedactCardSlot, redactedText)
    return CARD_QUERY_SLOT_PATTERN.sub(RedactCardSlot, redactedText)


def IsSensitiveQueryParameter(queryName: str) -> bool:
    """Tell whether a decoded query parameter name carries a card number or a card reference."""
    return queryName in CARD_REFERENCE_QUERY_NAMES or queryName == CARD_NUMBER_QUERY_NAME


def RedactQueryValue(queryName: str, queryValue: str) -> str:
    """Return a decoded cardNum value as a card slot shows it, and any card reference redacted."""
    if queryName == CARD_NUMBER_QUERY_NAME:
        return RedactCardSlotValue(queryValue)
    return REDACTED_REPLACEMENT


def MaskQueryText(rawText: str, decodedText: str) -> str:
    """Return a query name or value as sent, or its decoded text masked and encoded again.

    The decoded text is masked by MaskPlainText. Unless that changes it, the text is kept
    exactly as sent, so diagnostics stay as the client wrote them.
    """
    maskedText = MaskPlainText(decodedText)
    if maskedText == decodedText:
        return rawText
    return urllib.parse.quote_plus(maskedText, safe=QUERY_TEXT_SAFE, errors="backslashreplace")


def MaskQueryPair(pairText: str) -> str:
    """Return one name=value pair of a query that is not a request target's, judged by value.

    The name and value are split at the first = and decoded as Starlette's parse_qsl decodes
    them. A card number or card reference parameter with a value has that value replaced by
    RedactQueryValue; any other name and value go through MaskQueryText.
    """
    rawName, separator, rawValue = pairText.partition("=")
    queryName = urllib.parse.unquote_plus(rawName)
    queryValue = urllib.parse.unquote_plus(rawValue)
    if rawValue and IsSensitiveQueryParameter(queryName):
        return rawName + separator + RedactQueryValue(queryName, queryValue)
    return MaskQueryText(rawName, queryName) + separator + MaskQueryText(rawValue, queryValue)


def RouteQueryShapes(decodedPath: str) -> Mapping[str, re.Pattern[str]]:
    """Return the value shapes of the query parameters the route of a decoded path declares.

    The first ROUTE_QUERY_SHAPES pattern found at the end of the path names the route. A path
    no pattern matches declares no query parameter.
    """
    for routePattern, queryShapes in ROUTE_QUERY_SHAPES:
        if routePattern.search(decodedPath):
            return queryShapes
    return NO_QUERY_SHAPES


def MaskDeclaredValue(queryName: str, rawValue: str, valueShape: re.Pattern[str]) -> str:
    """Return a declared parameter's non-empty value as sent, masked, or redacted by its shape.

    The value is decoded as Starlette's parse_qsl decodes it. A card number or card_ref value
    goes through RedactQueryValue. A value whose whole decoded text has its shape goes through
    MaskQueryText, so a 16-digit transaction key stays masked; any other value is redacted.
    """
    queryValue = urllib.parse.unquote_plus(rawValue)
    if valueShape is CARD_PARAMETER_SHAPE:
        return RedactQueryValue(queryName, queryValue)
    if valueShape.fullmatch(queryValue):
        return MaskQueryText(rawValue, queryValue)
    return REDACTED_REPLACEMENT


def MaskTargetQueryPair(pairText: str, queryShapes: Mapping[str, re.Pattern[str]]) -> str:
    """Return one pair of a request target's query, judged by its route's declared parameters.

    The name is decoded as Starlette's parse_qsl decodes it, so an encoded alias such as
    %70assword is judged as password. A pair whose decoded name its route does not declare
    becomes [REDACTED], name included, since the name itself may be a secret. A declared pair
    with an empty value or no = is kept, as is an empty pair between && or after a bare ?. Any
    other declared pair keeps its name as sent, and MaskDeclaredValue shows its value.
    """
    if not pairText:
        return pairText
    rawName, separator, rawValue = pairText.partition("=")
    queryName = urllib.parse.unquote_plus(rawName)
    if queryName not in queryShapes:
        return REDACTED_REPLACEMENT
    if not rawValue:
        return pairText
    valueShape = queryShapes[queryName]
    return rawName + separator + MaskDeclaredValue(queryName, rawValue, valueShape)


def MaskTargetQuery(queryText: str, decodedPath: str) -> str:
    """Return a request target's query with each & pair judged by MaskTargetQueryPair.

    The route is read once from the decoded request path by RouteQueryShapes.
    """
    queryShapes = RouteQueryShapes(decodedPath)
    maskedPairs = (MaskTargetQueryPair(pairText, queryShapes) for pairText in queryText.split("&"))
    return "&".join(maskedPairs)


def IsRequestTarget(decodedPath: str) -> bool:
    """Tell whether the decoded text before a ? is a request target's path.

    Every request path and URL holds a /, and the asterisk-form target of OPTIONS * is *.
    """
    return "/" in decodedPath or decodedPath == ASTERISK_FORM_TARGET


def MaskQueryString(queryText: str, requestPath: str) -> str:
    """Return a query with each of its & pairs masked, judged pair by pair.

    requestPath, the text before the ?, is percent-decoded. When it is a request target's path,
    the query goes through MaskTargetQuery, which keeps only what its route declares. The text
    after a ? in other text, such as "what?x" in a message or a traceback source line, has each
    pair judged by its decoded value through MaskQueryPair.
    """
    decodedPath = urllib.parse.unquote(requestPath)
    if IsRequestTarget(decodedPath):
        return MaskTargetQuery(queryText, decodedPath)
    return "&".join(MaskQueryPair(pairText) for pairText in queryText.split("&"))


def MaskRequestQuery(queryMatch: re.Match[str]) -> str:
    """Return a request target with its path as sent and its query masked by MaskQueryString."""
    requestPath, queryText = queryMatch.group(1), queryMatch.group(2)
    return requestPath + "?" + MaskQueryString(queryText, requestPath)


def RedactValidationInputs(text: str) -> str:
    """Return text with the input values that Pydantic and FastAPI validation errors echo redacted.

    A Pydantic error line keeps its error type and input type around input_value=[REDACTED]. A
    FastAPI error dict keeps its type, loc and msg, and 'input': [REDACTED] ends its line, ctx
    included. Either value may be a stored customer value that failed a response model.
    """
    redactedText = VALIDATION_INPUT_VALUE_PATTERN.sub(VALIDATION_INPUT_VALUE_REPLACEMENT, text)
    return VALIDATION_INPUT_KEY_PATTERN.sub(VALIDATION_INPUT_KEY_REPLACEMENT, redactedText)


def RedactConnectionDsns(text: str) -> str:
    """Return text with the libpq dsn of each DBAPI connection repr replaced by [REDACTED].

    The rest of the repr is kept, so a pool error still names the connection object and its
    closed state: <connection object at 0x...; dsn: [REDACTED], closed: 0>.
    """
    return CONNECTION_DSN_PATTERN.sub(CONNECTION_DSN_REPLACEMENT, text)


def MaskSensitiveText(text: str) -> str:
    """Return text with undeclared query parameters and tokens redacted and card numbers masked.

    The libpq dsn of each DBAPI connection repr is redacted first by RedactConnectionDsns, then
    the input values that validation errors echo by RedactValidationInputs, before a query
    rewrite could take in the separators that delimit them. Each query after a ? is then
    rewritten pair by pair by MaskRequestQuery: a request target keeps only the parameters its
    route declares and redacts a value without its declared shape, and any other query has each
    parameter judged by its decoded value. MaskPlainText then redacts each literal Fernet token,
    JWT and card slot and masks each digit chain that may hold a card number. Applied to its own
    result, it returns that result unchanged.
    """
    redactedText = RedactValidationInputs(RedactConnectionDsns(text))
    return MaskPlainText(REQUEST_QUERY_PATTERN.sub(MaskRequestQuery, redactedText))


def MaskValue(value: object) -> object:
    """Return a string log argument masked, and any other value unchanged."""
    if isinstance(value, str):
        return MaskSensitiveText(value)
    return value


def MaskArguments(
    args: tuple[object, ...] | Mapping[str, object] | None,
) -> tuple[object, ...] | Mapping[str, object] | None:
    """Mask the string items of a record's args, keeping its tuple or mapping shape."""
    if isinstance(args, tuple):
        return tuple(MaskValue(value) for value in args)
    if isinstance(args, Mapping):
        return {key: MaskValue(value) for key, value in args.items()}
    return args


def MaskScopeQuery(queryString: object, decodedPath: str) -> object:
    """Return an ASGI scope's query_string masked by MaskTargetQuery, as bytes or str again.

    A scope is always a request's, so its query keeps only what the route of its decoded path
    declares, whatever that path holds. Bytes are read as Latin-1, so every byte maps to one
    character and back. A value of any other type cannot be read as a query and is shown as
    <...>.
    """
    if isinstance(queryString, bytes):
        maskedQuery = MaskTargetQuery(queryString.decode(SCOPE_QUERY_ENCODING), decodedPath)
        return maskedQuery.encode(SCOPE_QUERY_ENCODING, errors="backslashreplace")
    if isinstance(queryString, str):
        return MaskTargetQuery(queryString, decodedPath)
    return OMITTED_CONTENT_PLACEHOLDER


def ProjectScopeMapping(scope: Mapping[str, object]) -> Mapping[str, object]:
    """Return a mapping that is an ASGI scope as a copy safe to log, and any other unchanged.

    A mapping with a query_string or raw_path key is copied. Its query_string is masked by
    MaskScopeQuery with the scope's path, which ASGI holds decoded, naming the route, and its
    raw_path bytes, which may hold a percent-encoded card number, are shown as <...>. The
    decoded path stays, and is masked with the rendered message.
    """
    if SCOPE_QUERY_KEY not in scope and SCOPE_RAW_PATH_KEY not in scope:
        return scope
    projectedScope = dict(scope)
    scopePath = projectedScope.get(SCOPE_PATH_KEY, "")
    decodedPath = scopePath if isinstance(scopePath, str) else ""
    if SCOPE_QUERY_KEY in projectedScope:
        queryString = projectedScope[SCOPE_QUERY_KEY]
        projectedScope[SCOPE_QUERY_KEY] = MaskScopeQuery(queryString, decodedPath)
    if SCOPE_RAW_PATH_KEY in projectedScope:
        projectedScope[SCOPE_RAW_PATH_KEY] = OMITTED_CONTENT_PLACEHOLDER
    return projectedScope


def ProjectScope(argument: object) -> object:
    if isinstance(argument, Mapping):
        return ProjectScopeMapping(argument)
    return argument


def ProjectArguments(
    args: tuple[object, ...] | Mapping[str, object] | None,
) -> tuple[object, ...] | Mapping[str, object] | None:
    """Return a record's args with each ASGI scope among them projected by ProjectScope.

    Uvicorn's TRACE log passes the scope as one item of an args tuple, and logging unwraps a
    lone mapping argument into args itself. The tuple keeps its length and positions, and args
    holding no scope are returned as the same object.
    """
    if isinstance(args, Mapping):
        return ProjectScopeMapping(args)
    if not isinstance(args, tuple):
        return args
    projectedArgs = tuple(ProjectScope(value) for value in args)
    if all(projected is value for projected, value in zip(projectedArgs, args, strict=True)):
        return args
    return projectedArgs


def RenderMessage(record: logging.LogRecord) -> str:
    """Return the record's message, or its msg and args side by side when they do not fit."""
    try:
        return record.getMessage()
    except (TypeError, ValueError, KeyError):
        # Formatting a mismatched msg and args is the handler's error to report, so the
        # filter masks what the handler would print instead of raising into the caller.
        return f"{record.msg} {record.args}"


def MaskMessage(record: logging.LogRecord) -> None:
    """Mask the record's message in place, keeping the shape of its args.

    Each ASGI scope in args, as Uvicorn's TRACE log passes it, is first replaced by the copy
    ProjectArguments makes, whose query and raw path are safe to render. Uvicorn's
    AccessFormatter, which a logging configuration applied after ConfigureLogging can install
    again, unpacks record.args as the 5-tuple (client, method, path, HTTP version, status),
    so string args are masked in place, and msg has only its card numbers masked,
    since redacting a slot such as "/cards/%s" would consume its placeholder. The masked
    message is folded into msg only when the record still renders unmasked text. Uvicorn's
    color_message would re-render the original text, so it is dropped.
    """
    record.args = ProjectArguments(record.args)
    rendered = RenderMessage(record)
    maskedRendered = MaskSensitiveText(rendered)
    if maskedRendered == rendered:
        return
    record.args = MaskArguments(record.args)
    if isinstance(record.msg, str):
        record.msg = MaskDigitRuns(record.msg)
    remaining = RenderMessage(record)
    if MaskSensitiveText(remaining) != remaining:
        # The value spans several arguments, sits in a non-string argument or in msg.
        record.msg = maskedRendered
        record.args = None
    record.__dict__.pop("color_message", None)


def MaskDiagnostics(record: logging.LogRecord) -> None:
    """Mask the traceback and stack text that formatters append after the message.

    The traceback is formatted as logging.Formatter formats it and cached in exc_text, which
    every formatter appends instead of formatting exc_info again; exc_info itself is kept.
    """
    if isinstance(record.exc_info, tuple) and not record.exc_text:
        record.exc_text = TRACEBACK_FORMATTER.formatException(record.exc_info)
    if record.exc_text:
        record.exc_text = MaskSensitiveText(record.exc_text)
    if record.stack_info:
        record.stack_info = MaskSensitiveText(record.stack_info)


class PanMaskingFilter(logging.Filter):
    """Masks card numbers and redacts tokens and undeclared query parameters in a record in place.

    A declared query parameter whose value lacks its route's value shape is redacted too, as are
    each input value a validation error echoes and each connection repr's libpq dsn. It covers
    the message, its args (ASGI scopes included), the traceback and the stack text. It never
    drops a record.
    """

    # logging calls this exact name, so it stays lower-case: a framework-mandated
    # exception to the MK_Modernization Rule's PascalCase method naming.
    def filter(self, record: logging.LogRecord) -> bool:
        MaskMessage(record)
        MaskDiagnostics(record)
        return True


class CurrentStdoutHandler(logging.StreamHandler):
    """A stream handler that writes each record to sys.stdout as it stands at that moment.

    A handler bound to the stream of its first configuration breaks once that stream is
    swapped and closed, as Click's CliRunner does for every in-process carddemo-batch command:
    each later record then fails, and logging prints the failure, with the exception chain the
    record was logged in, to stderr. logging.lastResort resolves sys.stderr the same way.
    """

    def __init__(self) -> None:
        # StreamHandler.__init__ would assign the stream, which this handler never stores.
        logging.Handler.__init__(self)

    @property
    def stream(self) -> TextIO:
        return sys.stdout


def BuildStdoutHandler() -> logging.Handler:
    """Return the named stdout handler that masks every record it writes."""
    stdoutHandler = CurrentStdoutHandler()
    stdoutHandler.name = HANDLER_NAME
    stdoutHandler.setFormatter(LOG_LINE_FORMATTER)
    stdoutHandler.addFilter(PanMaskingFilter())
    return stdoutHandler


def AttachMaskingFilter(filterer: logging.Filterer) -> None:
    """Add a PanMaskingFilter to a logger or handler that does not have one yet."""
    if not any(isinstance(existing, PanMaskingFilter) for existing in filterer.filters):
        filterer.addFilter(PanMaskingFilter())


def InstallStdoutHandler() -> None:
    """Make the named stdout handler the first root handler, and log at INFO.

    The filter sits on the handler: a logger's own filters are not consulted for records
    that propagate up from child loggers such as app.services.*. As the first root handler,
    it masks each record in place before any other root handler receives it.
    """
    rootLogger = logging.getLogger()
    namedHandlers = [handler for handler in rootLogger.handlers if handler.name == HANDLER_NAME]
    stdoutHandler = namedHandlers[0] if namedHandlers else BuildStdoutHandler()
    otherHandlers = [handler for handler in rootLogger.handlers if handler.name != HANDLER_NAME]
    rootLogger.handlers = [stdoutHandler, *otherHandlers]
    rootLogger.setLevel(logging.INFO)


def RouteStderrToStdout(handler: logging.Handler) -> None:
    """Point a stream handler that writes to stderr at stdout; leave any other handler alone."""
    if not isinstance(handler, logging.StreamHandler) or handler.stream is None:
        return
    if handler.stream is sys.stderr or handler.stream is sys.__stderr__:
        handler.setStream(sys.stdout)


def ProtectUvicornLogger(loggerName: str) -> None:
    """Mask what a Uvicorn logger and its handlers write, and send it to stdout in LOG_LINE_FORMAT.

    Under Uvicorn's default configuration its records never reach the root handler: the
    non-propagating "uvicorn" logger writes uvicorn.error records through its stderr handler,
    and the non-propagating uvicorn.access logger writes through its own handler. A logger's
    filter masks the records logged on it and survives a later logging.config.dictConfig; a
    handler's filter masks every record the handler writes, including propagated ones. Each
    handler's formatter, which in Uvicorn's default configuration writes no time, is replaced
    by LOG_LINE_FORMATTER, so Uvicorn's lines carry the timestamp, level and logger name that
    application and batch lines carry.
    """
    uvicornLogger = logging.getLogger(loggerName)
    AttachMaskingFilter(uvicornLogger)
    for handler in uvicornLogger.handlers:
        AttachMaskingFilter(handler)
        RouteStderrToStdout(handler)
        handler.setFormatter(LOG_LINE_FORMATTER)


def ConfigureLogging() -> None:
    """Log to stdout at INFO with card numbers masked and card references redacted.

    It covers the root handlers and Uvicorn's loggers, whose handlers then write the root
    handler's timestamped LOG_LINE_FORMAT. Calling it again changes nothing.
    """
    InstallStdoutHandler()
    for loggerName in UVICORN_LOGGERS:
        ProtectUvicornLogger(loggerName)


# SQLAlchemy's Engine writes every statement with its bound parameters at INFO and every
# fetched row at DEBUG to this logger, which every CardDemo engine uses (none sets a
# logging_name), as soon as it or the "sqlalchemy" logger above it allows those levels.
SQLALCHEMY_ENGINE_LOGGER = "sqlalchemy.engine.Engine"
# Every connection pool logs below this logger (sqlalchemy.pool.impl.QueuePool, NullPool,
# AsyncAdaptedQueuePool) and writes the DBAPI connection's repr at DEBUG (connect, checkout,
# checkin, reset) and at INFO (invalidation, recycling). psycopg2's repr carries the libpq
# dsn: the database user, host, port and database name. The pool's warnings and errors, such
# as a failed close, still reach the log, with that dsn redacted by PanMaskingFilter.
SQLALCHEMY_POOL_LOGGER = "sqlalchemy.pool"
SUPPRESSED_SQLALCHEMY_LOGGERS = (SQLALCHEMY_ENGINE_LOGGER, SQLALCHEMY_POOL_LOGGER)


def SuppressEngineStatementLogging() -> None:
    """Keep SQLAlchemy from creating statement, parameter, result-row and pool log records.

    Bound parameters and fetched rows carry password hashes, SSNs, CVVs and customer data, and
    the pool's connection records carry the connection's dsn (database user, host, port and
    database name), which no log may hold. The engine and pool loggers' own levels are raised
    to WARNING unless already WARNING or above, so isEnabledFor(INFO) and isEnabledFor(DEBUG)
    stay false on them and on each pool class logger without a level of its own. SQLAlchemy
    then never builds those records, even with the "sqlalchemy" logger at DEBUG; engine and
    pool warnings and errors still propagate, and PanMaskingFilter redacts the dsn of any
    connection they show. logging.config.fileConfig and dictConfig reset these loggers to
    NOTSET when they configure a logger above them, such as "sqlalchemy.engine", so each engine
    builder calls this after logging is configured, and also sets hide_parameters=True, which
    keeps bound values out of SQLAlchemy's error text.
    """
    for loggerName in SUPPRESSED_SQLALCHEMY_LOGGERS:
        suppressedLogger = logging.getLogger(loggerName)
        if suppressedLogger.level < logging.WARNING:
            suppressedLogger.setLevel(logging.WARNING)
