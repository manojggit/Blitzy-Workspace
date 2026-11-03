"""Independent legacy oracle: a stdlib-only reading of the CardDemo batch COBOL and JCL.

Transcribes: CBTRN02C (POSTTRAN), CBACT04C (INTCALC), the TRANBKP unload, the COMBTRAN sort,
CBSTM03A and CBSTM03B (CREASTMT) and CBTRN03C (TRANREPT), with record offsets copied from the
copybooks CVACT01Y, CVCUS01Y (CUSTREC), CVACT02Y, CVACT03Y, CVTRA01Y-CVTRA07Y, CSUSR01Y and
COSTM01.

The repository holds no legacy batch outputs, only the input images in ``data/EBCDIC``. This
module derives the expected outputs from the cited paragraphs, and ``build_expected.py`` writes
them to ``batch/tests/fixtures/expected``. It deliberately imports nothing from ``app`` or
``batch`` and carries its own layouts, codec and arithmetic, so a defect shared by the production
code cannot validate itself.

Values follow the PostgreSQL column names. ``X(n)`` text is right-trimmed, ``9(n)`` identifiers
stay digit text, ``9(n)`` codes are ``int``, signed zoned fields are ``Decimal``, ``X(10)`` dates
are ISO text (``""`` when blank) and ``X(26)`` timestamps are ``datetime | None`` plus a
``<name>_format`` code. Every arithmetic step runs under ``ORACLE_CONTEXT`` and is stored with
``TruncateTo``, COBOL's store-into-receiver without ``ROUNDED`` or ``ON SIZE ERROR``.

Jobs never mutate the ``OracleState`` they receive: they copy what they change and return a new
state through ``dataclasses.replace``.
"""

import codecs
import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_DOWN, Context, Decimal, InvalidOperation
from pathlib import Path

# Field kinds. Each FieldSpec names one; ParseRecord and FormatRecord dispatch on it.
KIND_TEXT = "X"
KIND_DIGITS = "9"
KIND_CODE = "N"
KIND_ZONED = "S"
KIND_DATE = "D"
KIND_TIMESTAMP = "T"

TIMESTAMP_FORMAT_SUFFIX = "_format"


@dataclass(frozen=True)
class FieldSpec:
    """One copybook field: its column name, 0-based offset, length and kind.

    intDigits and decDigits apply to KIND_ZONED fields, for example 10 and 2 for S9(10)V99.
    """

    name: str
    offset: int
    length: int
    kind: str
    intDigits: int = 0
    decDigits: int = 0


# CVACT01Y ACCOUNT-RECORD (ACCTDATA), LRECL 300. FILLER X(178) at offset 122.
ACCOUNT_FIELDS = (
    FieldSpec("acct_id", 0, 11, KIND_DIGITS),
    FieldSpec("active_status", 11, 1, KIND_TEXT),
    FieldSpec("curr_balance", 12, 12, KIND_ZONED, 10, 2),
    FieldSpec("credit_limit", 24, 12, KIND_ZONED, 10, 2),
    FieldSpec("cash_credit_limit", 36, 12, KIND_ZONED, 10, 2),
    FieldSpec("open_date", 48, 10, KIND_DATE),
    FieldSpec("expiration_date", 58, 10, KIND_DATE),
    FieldSpec("reissue_date", 68, 10, KIND_DATE),
    FieldSpec("curr_cycle_credit", 78, 12, KIND_ZONED, 10, 2),
    FieldSpec("curr_cycle_debit", 90, 12, KIND_ZONED, 10, 2),
    FieldSpec("addr_zip", 102, 10, KIND_TEXT),
    FieldSpec("group_id", 112, 10, KIND_TEXT),
)

# CVCUS01Y CUSTOMER-RECORD (CUSTDATA), identical to CUSTREC, LRECL 500. FILLER X(168) at 332.
CUSTOMER_FIELDS = (
    FieldSpec("cust_id", 0, 9, KIND_DIGITS),
    FieldSpec("first_name", 9, 25, KIND_TEXT),
    FieldSpec("middle_name", 34, 25, KIND_TEXT),
    FieldSpec("last_name", 59, 25, KIND_TEXT),
    FieldSpec("addr_line_1", 84, 50, KIND_TEXT),
    FieldSpec("addr_line_2", 134, 50, KIND_TEXT),
    FieldSpec("addr_line_3", 184, 50, KIND_TEXT),
    FieldSpec("addr_state_cd", 234, 2, KIND_TEXT),
    FieldSpec("addr_country_cd", 236, 3, KIND_TEXT),
    FieldSpec("addr_zip", 239, 10, KIND_TEXT),
    FieldSpec("phone_num_1", 249, 15, KIND_TEXT),
    FieldSpec("phone_num_2", 264, 15, KIND_TEXT),
    FieldSpec("ssn", 279, 9, KIND_DIGITS),
    FieldSpec("govt_issued_id", 288, 20, KIND_TEXT),
    FieldSpec("date_of_birth", 308, 10, KIND_DATE),
    FieldSpec("eft_account_id", 318, 10, KIND_TEXT),
    FieldSpec("primary_holder_ind", 328, 1, KIND_TEXT),
    FieldSpec("fico_credit_score", 329, 3, KIND_CODE),
)

# CVACT02Y CARD-RECORD (CARDDATA), LRECL 150. FILLER X(59) at offset 91.
CARD_FIELDS = (
    FieldSpec("card_num", 0, 16, KIND_TEXT),
    FieldSpec("acct_id", 16, 11, KIND_DIGITS),
    FieldSpec("cvv_cd", 27, 3, KIND_DIGITS),
    FieldSpec("embossed_name", 30, 50, KIND_TEXT),
    FieldSpec("expiration_date", 80, 10, KIND_DATE),
    FieldSpec("active_status", 90, 1, KIND_TEXT),
)

# CVACT03Y CARD-XREF-RECORD (CARDXREF), LRECL 50. FILLER X(14) at offset 36.
XREF_FIELDS = (
    FieldSpec("card_num", 0, 16, KIND_TEXT),
    FieldSpec("cust_id", 16, 9, KIND_DIGITS),
    FieldSpec("acct_id", 25, 11, KIND_DIGITS),
)

# CVTRA01Y TRAN-CAT-BAL-RECORD (TCATBALF), LRECL 50. FILLER X(22) at offset 28.
TCATBAL_FIELDS = (
    FieldSpec("acct_id", 0, 11, KIND_DIGITS),
    FieldSpec("tran_type_cd", 11, 2, KIND_TEXT),
    FieldSpec("tran_cat_cd", 13, 4, KIND_CODE),
    FieldSpec("balance", 17, 11, KIND_ZONED, 9, 2),
)

# CVTRA02Y DIS-GROUP-RECORD (DISCGRP), LRECL 50. FILLER X(28) at offset 22.
DISCGRP_FIELDS = (
    FieldSpec("group_id", 0, 10, KIND_TEXT),
    FieldSpec("tran_type_cd", 10, 2, KIND_TEXT),
    FieldSpec("tran_cat_cd", 12, 4, KIND_CODE),
    FieldSpec("interest_rate", 16, 6, KIND_ZONED, 4, 2),
)

# CVTRA03Y TRAN-TYPE-RECORD (TRANTYPE), LRECL 60. FILLER X(08) at offset 52.
TRANTYPE_FIELDS = (
    FieldSpec("tran_type", 0, 2, KIND_TEXT),
    FieldSpec("description", 2, 50, KIND_TEXT),
)

# CVTRA04Y TRAN-CAT-RECORD (TRANCATG), LRECL 60. FILLER X(04) at offset 56.
TRANCATG_FIELDS = (
    FieldSpec("tran_type_cd", 0, 2, KIND_TEXT),
    FieldSpec("tran_cat_cd", 2, 4, KIND_CODE),
    FieldSpec("description", 6, 50, KIND_TEXT),
)

# CVTRA05Y TRAN-RECORD (TRANSACT, SYSTRAN, TRANSACT.BKUP, TRANSACT.COMBINED), LRECL 350.
# FILLER X(20) at offset 330.
TRANSACTION_FIELDS = (
    FieldSpec("tran_id", 0, 16, KIND_TEXT),
    FieldSpec("tran_type_cd", 16, 2, KIND_TEXT),
    FieldSpec("tran_cat_cd", 18, 4, KIND_CODE),
    FieldSpec("tran_source", 22, 10, KIND_TEXT),
    FieldSpec("tran_desc", 32, 100, KIND_TEXT),
    FieldSpec("tran_amt", 132, 11, KIND_ZONED, 9, 2),
    FieldSpec("merchant_id", 143, 9, KIND_DIGITS),
    FieldSpec("merchant_name", 152, 50, KIND_TEXT),
    FieldSpec("merchant_city", 202, 50, KIND_TEXT),
    FieldSpec("merchant_zip", 252, 10, KIND_TEXT),
    FieldSpec("card_num", 262, 16, KIND_TEXT),
    FieldSpec("orig_ts", 278, 26, KIND_TIMESTAMP),
    FieldSpec("proc_ts", 304, 26, KIND_TIMESTAMP),
)

# CVTRA06Y DALYTRAN-RECORD (DALYTRAN) has the CVTRA05Y offsets under DALYTRAN-* names, LRECL 350.
# FILLER X(20) at offset 330.
DAILY_FIELDS = TRANSACTION_FIELDS

# CSUSR01Y SEC-USER-DATA (USRSEC), LRECL 80. SEC-USR-FILLER X(23) at offset 57.
USRSEC_FIELDS = (
    FieldSpec("user_id", 0, 8, KIND_TEXT),
    FieldSpec("first_name", 8, 20, KIND_TEXT),
    FieldSpec("last_name", 28, 20, KIND_TEXT),
    FieldSpec("password", 48, 8, KIND_TEXT),
    FieldSpec("user_type", 56, 1, KIND_TEXT),
)

# CBTRN02C REJECT-RECORD (DALYREJS), LRECL 430: REJECT-TRAN-DATA X(350), the daily record, then
# WS-VALIDATION-TRAILER, FAIL-REASON 9(04) and FAIL-REASON-DESC X(76) (cbl/CBTRN02C.cbl lines
# 176-182).
DALYREJS_FIELDS = DAILY_FIELDS + (
    FieldSpec("reject_reason", 350, 4, KIND_CODE),
    FieldSpec("reject_description", 354, 76, KIND_TEXT),
)

ACCOUNT_LRECL = 300
CUSTOMER_LRECL = 500
CARD_LRECL = 150
XREF_LRECL = 50
TCATBAL_LRECL = 50
DISCGRP_LRECL = 50
TRANTYPE_LRECL = 60
TRANCATG_LRECL = 60
TRANSACTION_LRECL = 350
USRSEC_LRECL = 80
DALYREJS_LRECL = 430
STATEMENT_PS_WIDTH = 80
STATEMENT_HTML_WIDTH = 100
REPORT_LINE_WIDTH = 133

# Layout name -> (fields, record length). The statement files are line-only layouts.
LAYOUTS: dict[str, tuple[tuple[FieldSpec, ...], int]] = {
    "ACCOUNT": (ACCOUNT_FIELDS, ACCOUNT_LRECL),
    "CUSTOMER": (CUSTOMER_FIELDS, CUSTOMER_LRECL),
    "CARD": (CARD_FIELDS, CARD_LRECL),
    "XREF": (XREF_FIELDS, XREF_LRECL),
    "TCATBAL": (TCATBAL_FIELDS, TCATBAL_LRECL),
    "DISCGRP": (DISCGRP_FIELDS, DISCGRP_LRECL),
    "TRANTYPE": (TRANTYPE_FIELDS, TRANTYPE_LRECL),
    "TRANCATG": (TRANCATG_FIELDS, TRANCATG_LRECL),
    "TRANSACTION": (TRANSACTION_FIELDS, TRANSACTION_LRECL),
    "DAILY": (DAILY_FIELDS, TRANSACTION_LRECL),
    "USRSEC": (USRSEC_FIELDS, USRSEC_LRECL),
    "DALYREJS": (DALYREJS_FIELDS, DALYREJS_LRECL),
    "STATEMNT.PS": ((), STATEMENT_PS_WIDTH),
    "STATEMNT.HTML": ((), STATEMENT_HTML_WIDTH),
}

# 31 digits cover the 30-digit ARITH(COMPAT) intermediates; ROUND_DOWN is COBOL truncation.
ORACLE_CONTEXT = Context(prec=31, rounding=ROUND_DOWN)
CP037_CODEC = "cp037"
DIGIT_CHARACTERS = frozenset("0123456789")
POSITIVE_OVERPUNCH = "{ABCDEFGHI"
NEGATIVE_OVERPUNCH = "}JKLMNOPQR"
ZERO_AMOUNT = Decimal("0.00")
CENT = Decimal("0.01")

TIMESTAMP_LENGTH = 26
DATE_LENGTH = 10
BLANK_TIMESTAMP = " " * TIMESTAMP_LENGTH
BLANK_FORMAT_CODE = " "
FORMAT_SPACE = "S"
FORMAT_DB2 = "D"
FORMAT_DATE_ONLY = "T"
TIMESTAMP_FORMAT_CODES = (FORMAT_SPACE, FORMAT_DB2, FORMAT_DATE_ONLY)
# Separators after the date and between the time parts: S is YYYY-MM-DD HH:MM:SS.ffffff, and D is
# the YYYY-MM-DD-HH.MM.SS.ffffff text of Z-GET-DB2-FORMAT-TIMESTAMP (cbl/CBTRN02C.cbl 692-705).
TIMESTAMP_SEPARATORS = {FORMAT_SPACE: (" ", ":"), FORMAT_DB2: ("-", ".")}
TIMESTAMP_PARSE_FORMATS = {
    FORMAT_SPACE: "%Y-%m-%d %H:%M:%S.%f",
    FORMAT_DB2: "%Y-%m-%d-%H.%M.%S.%f",
    FORMAT_DATE_ONLY: "%Y-%m-%d",
}


def DecodeCp037(raw: bytes) -> str:
    """Decode EBCDIC code page 037 bytes into text, one character per byte.

    Args:
        raw: Bytes of a legacy image as the JCL loads it.

    Returns:
        The decoded text, with the zone nibble of overpunched digits mapped to {, A-I, }, J-R.
    """
    return codecs.decode(raw, CP037_CODEC)


def EncodeCp037(text: str) -> bytes:
    """Encode text into EBCDIC code page 037 bytes, the inverse of DecodeCp037.

    Raises:
        UnicodeEncodeError: A character has no code page 037 byte.
    """
    return codecs.encode(text, CP037_CODEC)


def EbcdicSortKey(text: str) -> bytes:
    """Return the VSAM collating key of a key text: its code page 037 bytes.

    Every keyed read of TRANSACT, CCXREF, CARDDAT, USRSEC, TCATBALF and TRXFL returns records in
    this order, and the DFSORT CH comparisons of COMBTRAN, CREASTMT and TRANREPT use it too.
    """
    return EncodeCp037(text)


def RequireDigits(text: str, label: str) -> None:
    """Raise ValueError unless every character of text is an ASCII digit.

    The message names the field only, never its content, because SSNs pass through here.
    """
    if any(character not in DIGIT_CHARACTERS for character in text):
        raise ValueError(f"{label}: expected only the digits 0-9")


def OverpunchDigit(text: str) -> tuple[int, bool]:
    """Return the digit and the negative flag carried by the last character of a zoned field.

    { and A-I are positive 0-9, } and J-R negative 0-9, and a plain digit is unsigned positive.
    """
    signCharacter = text[-1]
    if signCharacter in DIGIT_CHARACTERS:
        return int(signCharacter), False
    if signCharacter in POSITIVE_OVERPUNCH:
        return POSITIVE_OVERPUNCH.index(signCharacter), False
    if signCharacter in NEGATIVE_OVERPUNCH:
        return NEGATIVE_OVERPUNCH.index(signCharacter), True
    raise ValueError(f"invalid trailing overpunch character in zoned field {text!r}")


def DecodeOverpunch(text: str, scale: int) -> Decimal:
    """Decode signed DISPLAY text with a trailing overpunch, such as PIC S9(09)V99.

    Args:
        text: The zoned digits, for example "0000000150{".
        scale: The implied decimal places (the V99 of the picture).

    Returns:
        The signed value, for example Decimal("15.00"). A negative zero comes back as zero.

    Raises:
        ValueError: The text is empty, or holds a non-digit before its sign character.
    """
    if not text:
        raise ValueError("empty zoned decimal field")
    lastDigit, negative = OverpunchDigit(text)
    RequireDigits(text[:-1], f"zoned decimal field {text!r}")
    magnitude = Decimal(int(text[:-1] + str(lastDigit))).scaleb(-scale, ORACLE_CONTEXT)
    return ORACLE_CONTEXT.minus(magnitude) if negative else magnitude


def TruncateTo(value: Decimal, intDigits: int, decDigits: int) -> Decimal:
    """Store a value into a COBOL receiver of intDigits.decDigits without ROUNDED or SIZE ERROR.

    Surplus fractional digits are truncated toward zero, then the integer part loses its
    high-order digits modulo 10 ** intDigits, keeping the sign. A zero result is positive zero.

    Example:
        TruncateTo(Decimal("1000000000.50") - Decimal("0.50"), 10, 2) is 1000000000.00, while
        TruncateTo(Decimal("1000000000.50"), 9, 2) is 0.50.
    """
    quantum = Decimal(1).scaleb(-decDigits)
    truncated = value.quantize(quantum, rounding=ROUND_DOWN, context=ORACLE_CONTEXT)
    modulus = ORACLE_CONTEXT.power(10, intDigits)
    stored = ORACLE_CONTEXT.remainder(truncated.copy_abs(), modulus)
    if truncated.is_signed() and stored != 0:
        stored = ORACLE_CONTEXT.minus(stored)
    return stored.quantize(quantum, context=ORACLE_CONTEXT)


def StoreSum(addends: Iterable[Decimal], intDigits: int, decDigits: int) -> Decimal:
    """Add the addends exactly under ORACLE_CONTEXT and store the sum with TruncateTo.

    This is one COBOL ADD or COMPUTE of a sum into a receiver of intDigits.decDigits.
    """
    total = Decimal(0)
    for addend in addends:
        total = ORACLE_CONTEXT.add(total, addend)
    return TruncateTo(total, intDigits, decDigits)


def DigitText(storedValue: Decimal, intDigits: int, decDigits: int) -> str:
    """Return the unsigned digits of a value already stored with TruncateTo, zero-padded."""
    scaled = storedValue.copy_abs().scaleb(decDigits, ORACLE_CONTEXT)
    return f"{int(scaled):0{intDigits + decDigits}d}"


def EncodeOverpunch(value: Decimal, intDigits: int, decDigits: int) -> str:
    """Encode a value as signed DISPLAY text with a trailing overpunch, the DecodeOverpunch inverse.

    The value is stored with TruncateTo first. The last digit d becomes "{ABCDEFGHI"[d] when the
    stored value is zero or positive, and "}JKLMNOPQR"[d] when it is negative.

    Example:
        EncodeOverpunch(Decimal("-0.50"), 9, 2) is "0000000005}".
    """
    storedValue = TruncateTo(value, intDigits, decDigits)
    digits = DigitText(storedValue, intDigits, decDigits)
    signCharacters = NEGATIVE_OVERPUNCH if storedValue < 0 else POSITIVE_OVERPUNCH
    return digits[:-1] + signCharacters[int(digits[-1])]


def DateText(moment: date) -> str:
    """Return YYYY-MM-DD with a zero-padded four-digit year, whatever the platform strftime does."""
    return f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"


def CoerceTimestamp(value: object) -> datetime | None:
    """Return a naive datetime for a timestamp value, or None for NULL or blank text.

    Accepts a datetime, a date (midnight), None, or text in one of the four 26-character forms,
    which includes the normalised YYYY-MM-DD HH:MM:SS.ffffff that compare.py reads from CSV.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            raise ValueError("timestamps are naive local time; got a zone-aware value")
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        if not value.strip(" "):
            return None
        return DecodeTimestampText(value.ljust(TIMESTAMP_LENGTH))[0]
    raise TypeError(f"unsupported timestamp value type {type(value).__name__}")


def EncodeTimestampText(value: object, formatCode: str | None) -> str:
    """Rebuild the 26-character X(26) text of a timestamp from its value and format code.

    Args:
        value: A naive datetime, None, or text accepted by CoerceTimestamp.
        formatCode: "S" (YYYY-MM-DD HH:MM:SS.ffffff), "D" (YYYY-MM-DD-HH.MM.SS.ffffff), "T"
            (YYYY-MM-DD and 16 spaces), or blank.

    Returns:
        The legacy text; 26 spaces for a None value or a blank format code.

    Raises:
        ValueError: The format code is not one of the four forms.
    """
    moment = CoerceTimestamp(value)
    code = (formatCode or BLANK_FORMAT_CODE)[:1]
    if moment is None or code == BLANK_FORMAT_CODE:
        return BLANK_TIMESTAMP
    if code == FORMAT_DATE_ONLY:
        return DateText(moment).ljust(TIMESTAMP_LENGTH)
    if code not in TIMESTAMP_SEPARATORS:
        raise ValueError(f"unknown timestamp format code {formatCode!r}")
    dateSeparator, timeSeparator = TIMESTAMP_SEPARATORS[code]
    return (
        f"{DateText(moment)}{dateSeparator}{moment.hour:02d}{timeSeparator}"
        f"{moment.minute:02d}{timeSeparator}{moment.second:02d}.{moment.microsecond:06d}"
    )


def ParseTimestampForm(text: str, formatCode: str) -> datetime | None:
    """Parse 26-character text in one timestamp form, or return None when it is not that form.

    strptime tolerates short fields, so the parsed value must re-encode to exactly the input.
    """
    candidate = text[:DATE_LENGTH] if formatCode == FORMAT_DATE_ONLY else text
    try:
        moment = datetime.strptime(candidate, TIMESTAMP_PARSE_FORMATS[formatCode])
    except ValueError:
        return None
    if EncodeTimestampText(moment, formatCode) != text:
        return None
    return moment


def DecodeTimestampText(text: str) -> tuple[datetime | None, str]:
    """Decode an X(26) timestamp into its value and format code (AAP 0.6.6 forms).

    The forms are S (DALYTRAN ORIG-TS, CB00), D (POSTTRAN PROC-TS and INTCALC timestamps from
    Z-GET-DB2-FORMAT-TIMESTAMP, cbl/CBTRN02C.cbl lines 692-705, whose 6 trailing digits are read as
    microseconds), T (a CT02 date followed by 16 spaces, read as midnight) and 26 spaces.

    Returns:
        (value, code); (None, " ") for 26 spaces.

    Raises:
        ValueError: The text is not 26 characters, or matches none of the four forms.
    """
    if len(text) != TIMESTAMP_LENGTH:
        raise ValueError(f"timestamp text must be {TIMESTAMP_LENGTH} characters, got {text!r}")
    if text == BLANK_TIMESTAMP:
        return None, BLANK_FORMAT_CODE
    for formatCode in TIMESTAMP_FORMAT_CODES:
        moment = ParseTimestampForm(text, formatCode)
        if moment is not None:
            return moment, formatCode
    raise ValueError(f"unrecognised 26-character timestamp text {text!r}")


def DecodeTextField(fieldText: str, spec: FieldSpec) -> dict[str, object]:
    """Decode X(n): the text with its trailing space padding removed."""
    return {spec.name: fieldText.rstrip(" ")}


def DecodeDigitsField(fieldText: str, spec: FieldSpec) -> dict[str, object]:
    """Decode a 9(n) identifier as its raw digit text, so leading zeros survive."""
    RequireDigits(fieldText, spec.name)
    return {spec.name: fieldText}


def DecodeCodeField(fieldText: str, spec: FieldSpec) -> dict[str, object]:
    """Decode a 9(n) code, such as TRAN-CAT-CD 9(04), as an int."""
    RequireDigits(fieldText, spec.name)
    return {spec.name: int(fieldText)}


def DecodeZonedField(fieldText: str, spec: FieldSpec) -> dict[str, object]:
    """Decode a signed DISPLAY field with a trailing overpunch as a Decimal."""
    return {spec.name: DecodeOverpunch(fieldText, spec.decDigits)}


def DecodeDateField(fieldText: str, spec: FieldSpec) -> dict[str, object]:
    """Decode an X(10) YYYY-MM-DD date as its text, or "" when the field is blank."""
    return {spec.name: fieldText.strip(" ")}


def DecodeTimestampField(fieldText: str, spec: FieldSpec) -> dict[str, object]:
    """Decode an X(26) timestamp into <name> (datetime or None) and <name>_format."""
    value, formatCode = DecodeTimestampText(fieldText)
    return {spec.name: value, spec.name + TIMESTAMP_FORMAT_SUFFIX: formatCode}


FIELD_DECODERS = {
    KIND_TEXT: DecodeTextField,
    KIND_DIGITS: DecodeDigitsField,
    KIND_CODE: DecodeCodeField,
    KIND_ZONED: DecodeZonedField,
    KIND_DATE: DecodeDateField,
    KIND_TIMESTAMP: DecodeTimestampField,
}


def ToDecimal(value: object) -> Decimal:
    """Return a Decimal for a Decimal, int or numeric text value; floats are refused.

    Raises:
        ValueError: The text is not a number.
        TypeError: The value is a float, a bool or another type; money is never floating point.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError(f"unsupported numeric value type {type(value).__name__}")
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"not a decimal number: {value!r}") from error


def EncodeTextField(values: Mapping[str, object], spec: FieldSpec) -> str:
    """Encode X(n): the text left-justified and space-padded or truncated to n."""
    value = values[spec.name]
    text = "" if value is None else str(value)
    return text.ljust(spec.length)[: spec.length]


def EncodeDigitsField(values: Mapping[str, object], spec: FieldSpec) -> str:
    """Encode a 9(n) identifier: digit text as given, or an int zero-padded to n digits."""
    value = values[spec.name]
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{abs(value) % 10**spec.length:0{spec.length}d}"
    return EncodeTextField(values, spec)


def EncodeCodeField(values: Mapping[str, object], spec: FieldSpec) -> str:
    """Encode a 9(n) code zero-padded to n digits; an unsigned receiver keeps no sign."""
    number = abs(int(values[spec.name])) % 10**spec.length
    return f"{number:0{spec.length}d}"


def EncodeZonedField(values: Mapping[str, object], spec: FieldSpec) -> str:
    """Encode a signed DISPLAY field with a trailing overpunch through EncodeOverpunch."""
    amount = ToDecimal(values[spec.name])
    return EncodeOverpunch(amount, spec.intDigits, spec.decDigits)


def EncodeDateField(values: Mapping[str, object], spec: FieldSpec) -> str:
    """Encode an X(10) date: ISO text from a date or a string, or spaces for "" and None."""
    value = values[spec.name]
    if value is None:
        text = ""
    elif isinstance(value, date):
        text = DateText(value)
    else:
        text = str(value).strip(" ")
    return text.ljust(spec.length)[: spec.length]


def EncodeTimestampField(values: Mapping[str, object], spec: FieldSpec) -> str:
    """Encode an X(26) timestamp from <name> and <name>_format through EncodeTimestampText."""
    formatCode = values[spec.name + TIMESTAMP_FORMAT_SUFFIX]
    return EncodeTimestampText(values[spec.name], str(formatCode or BLANK_FORMAT_CODE))


FIELD_ENCODERS = {
    KIND_TEXT: EncodeTextField,
    KIND_DIGITS: EncodeDigitsField,
    KIND_CODE: EncodeCodeField,
    KIND_ZONED: EncodeZonedField,
    KIND_DATE: EncodeDateField,
    KIND_TIMESTAMP: EncodeTimestampField,
}


def ParseRecord(text: str, fields: Sequence[FieldSpec]) -> dict[str, object]:
    """Decode one fixed-width record into a dict keyed by column name; FILLER is not read.

    Args:
        text: The record text, at least as long as the end of the layout's last field.
        fields: A layout such as TRANSACTION_FIELDS.

    Returns:
        The column values, plus a <name>_format entry for each timestamp field.

    Raises:
        ValueError: The record is too short, or a digit, zoned or timestamp field is malformed.
    """
    recordEnd = max((spec.offset + spec.length for spec in fields), default=0)
    if len(text) < recordEnd:
        raise ValueError(
            f"record of {len(text)} characters is shorter than its layout ({recordEnd})"
        )
    values: dict[str, object] = {}
    for spec in fields:
        fieldText = text[spec.offset : spec.offset + spec.length]
        values.update(FIELD_DECODERS[spec.kind](fieldText, spec))
    return values


def FormatRecord(values: Mapping[str, object], fields: Sequence[FieldSpec], lrecl: int) -> str:
    """Encode column values as one fixed-width record, the canonical GDG record form.

    Every field is laid out at its offset on an lrecl-character canvas of spaces, so FILLER is
    written as spaces, as the production GDG writers do. The TCATBALF, DISCGRP, TRANTYPE and
    TRANCATG sample images carry "0" characters in their FILLER, so only their field bytes
    round-trip; no job writes those layouts to a file.

    Raises:
        KeyError: A column, or a timestamp's <name>_format entry, is missing from values.
        ValueError: A field lies beyond lrecl, or a value cannot be encoded in its picture.
    """
    canvas = [" "] * lrecl
    for spec in fields:
        fieldText = FIELD_ENCODERS[spec.kind](values, spec)
        if spec.offset + spec.length > lrecl or len(fieldText) != spec.length:
            raise ValueError(f"field {spec.name} does not fit a {lrecl}-character record")
        canvas[spec.offset : spec.offset + spec.length] = fieldText
    return "".join(canvas)


class ImageRecordError(ValueError):
    """A legacy image that cannot be loaded, located to one of its records.

    The message is "<path>: record <recordNumber>: <reason>". A decoding reason is the text of
    the ParseRecord error. For a 9(n) field it names the field without quoting its content,
    because SSNs pass through RequireDigits, while a zoned decimal or timestamp error quotes the
    malformed field text.

    Attributes:
        path: The image file.
        recordNumber: The 1-based number of the record, in file order.
        reason: What is wrong with that record.
    """

    def __init__(self, path: Path, recordNumber: int, reason: str) -> None:
        """Keep the image, the record number and the reason, and build the message."""
        super().__init__(f"{path}: record {recordNumber}: {reason}")
        self.path = path
        self.recordNumber = recordNumber
        self.reason = reason


def ReadEbcdicRecords(path: Path, lrecl: int) -> list[str]:
    """Read a fixed-length code page 037 image with no terminators into record texts.

    Raises:
        FileNotFoundError: The image does not exist.
        ValueError: lrecl is not positive.
        ImageRecordError: The file size is not a multiple of lrecl; the error names the
            incomplete last record and the bytes it holds.
    """
    raw = path.read_bytes()
    if lrecl <= 0:
        raise ValueError(f"{path}: LRECL {lrecl} is not positive")
    if len(raw) % lrecl != 0:
        raise ImageRecordError(
            path,
            len(raw) // lrecl + 1,
            f"{len(raw) % lrecl} of {lrecl} bytes present; the image size {len(raw)} is not a "
            f"multiple of LRECL {lrecl}",
        )
    return [DecodeCp037(raw[start : start + lrecl]) for start in range(0, len(raw), lrecl)]


def ParseImageRecord(
    path: Path, recordNumber: int, record: str, fields: Sequence[FieldSpec]
) -> dict[str, object]:
    """ParseRecord one record of an image, naming the image and the record when it fails.

    Raises:
        ImageRecordError: The record does not decode with fields.
    """
    try:
        return ParseRecord(record, fields)
    except ValueError as error:
        raise ImageRecordError(path, recordNumber, str(error)) from error


def ParseImageRecords(
    path: Path, records: Sequence[str], fields: Sequence[FieldSpec]
) -> list[dict[str, object]]:
    """ParseRecord every record of an image, in file order, numbering the records from 1.

    Raises:
        ImageRecordError: A record does not decode with fields.
    """
    return [
        ParseImageRecord(path, recordNumber, record, fields)
        for recordNumber, record in enumerate(records, start=1)
    ]


@dataclass(frozen=True)
class TableSpec:
    """The compared shape of one PostgreSQL table: the single definition for every comparison.

    columns are in model order and exclude the generated *_sort columns, created_at, updated_at
    and password_hash. ebcdicKeys is True where the key order is the VSAM EBCDIC order.
    """

    name: str
    columns: tuple[str, ...]
    keyColumns: tuple[str, ...]
    ebcdicKeys: bool


USERS_TABLE = "security.users"
ACCOUNTS_TABLE = "accounts.accounts"
CUSTOMERS_TABLE = "customers.customers"
CARDS_TABLE = "cards.cards"
CARD_XREF_TABLE = "xref.card_xref"
TRANSACTIONS_TABLE = "transactions.transactions"
DAILY_TRANSACTIONS_TABLE = "transactions.daily_transactions"
CATEGORY_BALANCES_TABLE = "transactions.transaction_category_balances"
DISCLOSURE_GROUPS_TABLE = "disclosure.disclosure_groups"
TRANSACTION_TYPES_TABLE = "reference.transaction_types"
TRANSACTION_CATEGORIES_TABLE = "reference.transaction_categories"
RECORD_SEQ_COLUMN = "record_seq"

TRANSACTION_COLUMNS = tuple(spec.name for spec in TRANSACTION_FIELDS) + (
    "orig_ts" + TIMESTAMP_FORMAT_SUFFIX,
    "proc_ts" + TIMESTAMP_FORMAT_SUFFIX,
)
USER_COLUMNS = ("user_id", "first_name", "last_name", "user_type")
CATEGORY_KEY_COLUMNS = ("acct_id", "tran_type_cd", "tran_cat_cd")
DISCLOSURE_KEY_COLUMNS = ("group_id", "tran_type_cd", "tran_cat_cd")

TABLE_SPECS: dict[str, TableSpec] = {
    spec.name: spec
    for spec in (
        TableSpec(USERS_TABLE, USER_COLUMNS, ("user_id",), True),
        TableSpec(ACCOUNTS_TABLE, tuple(f.name for f in ACCOUNT_FIELDS), ("acct_id",), False),
        TableSpec(CUSTOMERS_TABLE, tuple(f.name for f in CUSTOMER_FIELDS), ("cust_id",), False),
        TableSpec(CARDS_TABLE, tuple(f.name for f in CARD_FIELDS), ("card_num",), True),
        TableSpec(CARD_XREF_TABLE, tuple(f.name for f in XREF_FIELDS), ("card_num",), True),
        TableSpec(TRANSACTIONS_TABLE, TRANSACTION_COLUMNS, ("tran_id",), True),
        TableSpec(
            DAILY_TRANSACTIONS_TABLE,
            (RECORD_SEQ_COLUMN,) + TRANSACTION_COLUMNS,
            (RECORD_SEQ_COLUMN,),
            False,
        ),
        TableSpec(
            CATEGORY_BALANCES_TABLE,
            tuple(f.name for f in TCATBAL_FIELDS),
            CATEGORY_KEY_COLUMNS,
            True,
        ),
        TableSpec(
            DISCLOSURE_GROUPS_TABLE,
            tuple(f.name for f in DISCGRP_FIELDS),
            DISCLOSURE_KEY_COLUMNS,
            True,
        ),
        TableSpec(
            TRANSACTION_TYPES_TABLE, tuple(f.name for f in TRANTYPE_FIELDS), ("tran_type",), True
        ),
        TableSpec(
            TRANSACTION_CATEGORIES_TABLE,
            tuple(f.name for f in TRANCATG_FIELDS),
            ("tran_type_cd", "tran_cat_cd"),
            True,
        ),
    )
}

# The record layout whose pictures give each table's key bytes, for RowSortKey.
TABLE_LAYOUT_FIELDS: dict[str, tuple[FieldSpec, ...]] = {
    USERS_TABLE: USRSEC_FIELDS,
    ACCOUNTS_TABLE: ACCOUNT_FIELDS,
    CUSTOMERS_TABLE: CUSTOMER_FIELDS,
    CARDS_TABLE: CARD_FIELDS,
    CARD_XREF_TABLE: XREF_FIELDS,
    TRANSACTIONS_TABLE: TRANSACTION_FIELDS,
    DAILY_TRANSACTIONS_TABLE: DAILY_FIELDS,
    CATEGORY_BALANCES_TABLE: TCATBAL_FIELDS,
    DISCLOSURE_GROUPS_TABLE: DISCGRP_FIELDS,
    TRANSACTION_TYPES_TABLE: TRANTYPE_FIELDS,
    TRANSACTION_CATEGORIES_TABLE: TRANCATG_FIELDS,
}


def KeyFieldSpecs(tableName: str) -> tuple[FieldSpec, ...]:
    """Return the FieldSpecs of a table's key columns, in key order; daily keys have none."""
    spec = TABLE_SPECS[tableName]
    byName = {fieldSpec.name: fieldSpec for fieldSpec in TABLE_LAYOUT_FIELDS[tableName]}
    return tuple(byName[column] for column in spec.keyColumns if column in byName)


TABLE_KEY_FIELDS: dict[str, tuple[FieldSpec, ...]] = {
    tableName: KeyFieldSpecs(tableName) for tableName in TABLE_SPECS
}


def ResolveTableSpec(tableName: str) -> TableSpec:
    """Return the TableSpec for a qualified name ("accounts.accounts") or a bare one ("accounts").

    Raises:
        KeyError: No table, or more than one, has that name.
    """
    if tableName in TABLE_SPECS:
        return TABLE_SPECS[tableName]
    matches = [spec for name, spec in TABLE_SPECS.items() if name.split(".", 1)[1] == tableName]
    if len(matches) != 1:
        raise KeyError(f"unknown table {tableName!r}")
    return matches[0]


def CsvValue(value: object) -> str:
    """Return the single text normalisation used by the fixture CSVs and compare.NormalizeValue.

    None is "", a Decimal is 2 places truncated toward zero (a negative zero is "0.00"), a
    datetime is YYYY-MM-DD HH:MM:SS.ffffff, a date is ISO, an int is plain digits, and text is
    right-trimmed of spaces, so a blank format code " " is "".

    Raises:
        TypeError: The value is a bool, a float or another unsupported type.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        raise TypeError("bool is not a CardDemo column value")
    if isinstance(value, Decimal):
        cents = value.quantize(CENT, rounding=ROUND_DOWN, context=ORACLE_CONTEXT)
        return format(cents if cents != 0 else ZERO_AMOUNT, "f")
    if isinstance(value, datetime):
        return EncodeTimestampText(value, FORMAT_SPACE)
    if isinstance(value, date):
        return DateText(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value.rstrip(" ")
    raise TypeError(f"unsupported CSV value type {type(value).__name__}")


def RowKey(row: Mapping[str, object], spec: TableSpec) -> tuple[str, ...]:
    """Return the CsvValue of each key column, for example ("00000000001", "01", "1")."""
    return tuple(CsvValue(row[column]) for column in spec.keyColumns)


def RowSortKey(row: Mapping[str, object], spec: TableSpec) -> tuple[object, ...]:
    """Return the fixture-file order key of a row: VSAM key order, or record_seq for daily rows.

    The key bytes are the key fields as the record holds them (text space-padded, codes
    zero-padded to their PIC width) in code page 037, which is the KSDS collating sequence.
    Digit-only keys such as acct_id sort identically in ASCII and EBCDIC.
    """
    if spec.keyColumns == (RECORD_SEQ_COLUMN,):
        return (int(row[RECORD_SEQ_COLUMN]),)
    keyText = "".join(
        FIELD_ENCODERS[fieldSpec.kind](row, fieldSpec) for fieldSpec in TABLE_KEY_FIELDS[spec.name]
    )
    return (EbcdicSortKey(keyText),)


# Frozen clock text (format D) of --as-of 2022-07-18T00:00:00, the INTCALC PARM of
# jcl/INTCALC.jcl STEP15, and the one-day TRANREPT range of the sample report.
DB2_CLOCK_TEXT = "2022-07-18-00.00.00.000000"
PARM_DATE = "2022071800"
REPORT_START_DATE = "2022-07-18"
REPORT_END_DATE = "2022-07-18"

# The EBCDIC images the JCL loads. The byte-identical duplicate ACCDATA.PS is ignored.
IMAGE_PREFIX = "AWS.M2.CARDDEMO."
ACCTDATA_IMAGE = "ACCTDATA.PS"
CUSTDATA_IMAGE = "CUSTDATA.PS"
CARDDATA_IMAGE = "CARDDATA.PS"
CARDXREF_IMAGE = "CARDXREF.PS"
TCATBALF_IMAGE = "TCATBALF.PS"
DISCGRP_IMAGE = "DISCGRP.PS"
TRANTYPE_IMAGE = "TRANTYPE.PS"
TRANCATG_IMAGE = "TRANCATG.PS"
DALYTRAN_IMAGE = "DALYTRAN.PS"
DALYTRAN_INIT_IMAGE = "DALYTRAN.PS.INIT"
USRSEC_IMAGE = "USRSEC.PS"

# The TRANFILE priming record (jcl/TRANFILE.jcl STEP15) is 342 NUL characters, then "00000100".
PRIMING_PREFIX_LENGTH = 342
PRIMING_CHARACTER = "\x00"
# SEC-USR-PWD: decoded for ReadUsers only, never kept in OracleState or a fixture.
CREDENTIAL_COLUMN = "password"

EXIT_OK = 0
EXIT_REJECTS = 4
# The status of a refused IDCAMS utility step, such as a REPRO that meets a duplicate key.
EXIT_UTILITY_FAILURE = 12
EXIT_ABEND = 16
CHAIN_STOP_EXIT = 8


class OracleAbend(Exception):
    """Where the COBOL performs 9999-ABEND-PROGRAM, or the chain stops on a step at exit 8 or more.

    Attributes:
        message: What the legacy program would have displayed before abending.
        exitCode: The step status; 16 for an abend.
    """

    def __init__(self, message: str, exitCode: int = EXIT_ABEND) -> None:
        """Keep the message and the exit status of the abend."""
        super().__init__(message)
        self.message = message
        self.exitCode = exitCode


@dataclass(frozen=True)
class OracleState:
    """The dataset contents between chain steps, one dict per VSAM cluster.

    Keys: accounts by acct_id, customers by cust_id, cards and xrefs by card_num,
    categoryBalances by (acct_id, tran_type_cd, tran_cat_cd int), disclosureGroups by (group_id,
    tran_type_cd, tran_cat_cd int), transactionTypes by tran_type, transactionCategories by
    (tran_type_cd, tran_cat_cd int), and users by user_id. Rows are ParseRecord dicts; users carry
    no password. transactions maps the 16-character TRAN-ID to its 350-character record text.
    """

    accounts: dict[str, dict[str, object]]
    customers: dict[str, dict[str, object]]
    cards: dict[str, dict[str, object]]
    xrefs: dict[str, dict[str, object]]
    categoryBalances: dict[tuple[str, str, int], dict[str, object]]
    disclosureGroups: dict[tuple[str, str, int], dict[str, object]]
    transactionTypes: dict[str, dict[str, object]]
    transactionCategories: dict[tuple[str, int], dict[str, object]]
    transactions: dict[str, str]
    users: dict[str, dict[str, object]]


# (OracleState attribute, table, image, layout) for the loaders of chain steps 1-4 and 7-10.
STATE_TABLE_SOURCES = (
    ("accounts", ACCOUNTS_TABLE, ACCTDATA_IMAGE, "ACCOUNT"),
    ("customers", CUSTOMERS_TABLE, CUSTDATA_IMAGE, "CUSTOMER"),
    ("cards", CARDS_TABLE, CARDDATA_IMAGE, "CARD"),
    ("xrefs", CARD_XREF_TABLE, CARDXREF_IMAGE, "XREF"),
    ("transactionTypes", TRANSACTION_TYPES_TABLE, TRANTYPE_IMAGE, "TRANTYPE"),
    ("transactionCategories", TRANSACTION_CATEGORIES_TABLE, TRANCATG_IMAGE, "TRANCATG"),
    ("disclosureGroups", DISCLOSURE_GROUPS_TABLE, DISCGRP_IMAGE, "DISCGRP"),
    ("categoryBalances", CATEGORY_BALANCES_TABLE, TCATBALF_IMAGE, "TCATBAL"),
)
STATE_ATTRIBUTE_BY_TABLE = {
    **{table: attribute for attribute, table, image, layout in STATE_TABLE_SOURCES},
    USERS_TABLE: "users",
}


def ImagePath(ebcdicDir: Path, imageName: str) -> Path:
    """Return the path of a sample image, for example <dir>/AWS.M2.CARDDEMO.ACCTDATA.PS."""
    return ebcdicDir / (IMAGE_PREFIX + imageName)


def ReadImageRows(ebcdicDir: Path, imageName: str, layoutName: str) -> list[dict[str, object]]:
    """Read one EBCDIC image and parse every record with the named layout, in file order.

    Raises:
        FileNotFoundError: The image does not exist.
        ImageRecordError: The image size is not a multiple of the LRECL, or a record does not
            decode with the layout.
    """
    fields, lrecl = LAYOUTS[layoutName]
    path = ImagePath(ebcdicDir, imageName)
    return ParseImageRecords(path, ReadEbcdicRecords(path, lrecl), fields)


class DuplicateKeyError(ValueError):
    """Two rows share a key, which the IDCAMS REPRO into a KSDS refuses.

    Rows are numbered from 1 in the order IndexRows receives them, which is file order for the
    rows of an image. The message names the key columns, never the key value, which may be a
    card or account number.

    Attributes:
        rowNumber: The row that repeats the key.
        firstRowNumber: The earlier row that holds the same key.
        reason: The message without its leading "record <rowNumber>: ".
    """

    def __init__(self, rowNumber: int, firstRowNumber: int, keyColumns: Sequence[str]) -> None:
        """Keep both row numbers and build the reason from the key column names."""
        self.rowNumber = rowNumber
        self.firstRowNumber = firstRowNumber
        self.reason = f"duplicate key ({', '.join(keyColumns)}) of record {firstRowNumber}"
        super().__init__(f"record {rowNumber}: {self.reason}")


def IndexRows(rows: Iterable[dict[str, object]], keyColumns: tuple[str, ...]) -> dict:
    """Index rows by their key: the value itself for one key column, else a tuple.

    Raises:
        DuplicateKeyError: Two rows share a key, which the IDCAMS REPRO into a KSDS refuses. It
            is a ValueError and names both 1-based row numbers.
    """
    indexed: dict = {}
    firstRowNumbers: dict = {}
    for rowNumber, row in enumerate(rows, start=1):
        values = tuple(row[column] for column in keyColumns)
        key = values[0] if len(values) == 1 else values
        if key in indexed:
            raise DuplicateKeyError(rowNumber, firstRowNumbers[key], keyColumns)
        indexed[key] = row
        firstRowNumbers[key] = rowNumber
    return indexed


def IndexImageRows(
    path: Path, rows: Sequence[dict[str, object]], keyColumns: tuple[str, ...]
) -> dict:
    """IndexRows the rows of one image, kept in file order, naming the image on a duplicate key.

    Raises:
        ImageRecordError: A record repeats the key of an earlier record.
    """
    try:
        return IndexRows(rows, keyColumns)
    except DuplicateKeyError as error:
        raise ImageRecordError(path, error.rowNumber, error.reason) from error


def IsPrimingRecord(record: str) -> bool:
    """Return True for the TRANFILE priming record, whose first 342 characters are all NUL."""
    return record[:PRIMING_PREFIX_LENGTH] == PRIMING_CHARACTER * PRIMING_PREFIX_LENGTH


def IndexTransactionImage(path: Path, records: Sequence[str]) -> dict[str, str]:
    """Index the TRANFILE records by TRAN-ID, as the REPRO into KEYS(16 0) does; priming skipped.

    Records are numbered from 1 in file order, the priming record included. The REPRO writes the
    priming record into the KSDS too, so a record that is not a priming record and repeats the
    TRAN-ID of any earlier record, the priming record's all-NUL key included, is refused. A
    priming record itself is skipped, as every load of the image skips it.

    Raises:
        ImageRecordError: A record that is not a priming record repeats the TRAN-ID of an
            earlier record; the reason names that record, as DuplicateKeyError does.
    """
    keyColumns = TABLE_SPECS[TRANSACTIONS_TABLE].keyColumns
    loaded: dict[str, str] = {}
    firstRecordNumbers: dict[str, int] = {}
    for recordNumber, record in enumerate(records, start=1):
        tranId = record[TRAN_ID_SLICE]
        if IsPrimingRecord(record):
            firstRecordNumbers.setdefault(tranId, recordNumber)
            continue
        if tranId in firstRecordNumbers:
            duplicate = DuplicateKeyError(recordNumber, firstRecordNumbers[tranId], keyColumns)
            raise ImageRecordError(path, recordNumber, duplicate.reason)
        firstRecordNumbers[tranId] = recordNumber
        loaded[tranId] = record
    return loaded


def LoadTransactionImage(ebcdicDir: Path) -> dict[str, str]:
    """Replay TRANFILE STEP15 (jcl/TRANFILE.jcl lines 67-75): REPRO DALYTRAN.PS.INIT into TRANSACT.

    Every record that is not the priming record is decoded with CVTRA05Y, then IndexTransactionImage
    keys the records by TRAN-ID. The KSDS is defined KEYS(16 0) (line 53), so its REPRO refuses a
    duplicate key, and the replace load then exits 12 and rolls back: nothing is returned. The
    sample image holds only the priming record, which is skipped, so the sample result is empty.

    Raises:
        FileNotFoundError: The image does not exist.
        ImageRecordError: The image size is not a multiple of 350, or a record that is not the
            priming record does not decode with CVTRA05Y or repeats the TRAN-ID of an earlier
            record. Records are numbered from 1 in file order, the priming record included.
    """
    path = ImagePath(ebcdicDir, DALYTRAN_INIT_IMAGE)
    records = ReadEbcdicRecords(path, TRANSACTION_LRECL)
    for recordNumber, record in enumerate(records, start=1):
        if not IsPrimingRecord(record):
            ParseImageRecord(path, recordNumber, record, TRANSACTION_FIELDS)
    return IndexTransactionImage(path, records)


def ReadUsers(ebcdicDir: Path) -> list[dict[str, object]]:
    """Return the USRSEC rows in file order, with the right-trimmed plaintext password.

    This exists only so test_security_init.py can verify the stored hashes at test time. Never
    print or log the result.
    """
    return ReadImageRows(ebcdicDir, USRSEC_IMAGE, "USRSEC")


def LoadSampleState(ebcdicDir: Path) -> OracleState:
    """Replay the loaders of chain steps 1-5 and 7-11 (ACCTFILE ... DUSRSECJ) on the images.

    Expected counts: users 10, accounts 50, customers 50, cards 50, xrefs 50, types 7,
    categories 18, disclosure groups 51, category balances 50 and transactions 0.

    Raises:
        FileNotFoundError: An image does not exist.
        ImageRecordError: An image size is not a multiple of its LRECL, or one of its records
            does not decode or repeats the key of an earlier record.
    """
    tables = {
        attribute: IndexImageRows(
            ImagePath(ebcdicDir, image),
            ReadImageRows(ebcdicDir, image, layout),
            TABLE_SPECS[table].keyColumns,
        )
        for attribute, table, image, layout in STATE_TABLE_SOURCES
    }
    users = [
        {column: value for column, value in row.items() if column != CREDENTIAL_COLUMN}
        for row in ReadUsers(ebcdicDir)
    ]
    return OracleState(
        **tables,
        transactions=LoadTransactionImage(ebcdicDir),
        users=IndexImageRows(
            ImagePath(ebcdicDir, USRSEC_IMAGE), users, TABLE_SPECS[USERS_TABLE].keyColumns
        ),
    )


def ReadDailyRecords(path: Path) -> list[str]:
    """Return the DALYTRAN image as 350-character record texts in file order (300 records).

    Every record is first decoded with DAILY_FIELDS, as the DALYTRAN staging load (chain step
    12) decodes each field and refuses a malformed image. The texts are returned unchanged.

    Raises:
        FileNotFoundError: The image does not exist.
        ImageRecordError: The image size is not a multiple of 350, or a record does not decode.
    """
    records = ReadEbcdicRecords(path, TRANSACTION_LRECL)
    ParseImageRecords(path, records, DAILY_FIELDS)
    return records


def StateSourceRows(state: OracleState, spec: TableSpec) -> list[Mapping[str, object]]:
    """Return the oracle-form rows of one table, parsing transaction records with CVTRA05Y.

    Raises:
        KeyError: The table is not part of OracleState; daily rows come from DailyRows.
    """
    if spec.name == TRANSACTIONS_TABLE:
        return [ParseRecord(record, TRANSACTION_FIELDS) for record in state.transactions.values()]
    if spec.name not in STATE_ATTRIBUTE_BY_TABLE:
        raise KeyError(f"{spec.name} is not held in OracleState; use DailyRows")
    return list(getattr(state, STATE_ATTRIBUTE_BY_TABLE[spec.name]).values())


def StateRows(state: OracleState, tableName: str) -> list[dict[str, object]]:
    """Return the rows of one table from the oracle state, in fixture-file (VSAM key) order.

    Values stay in oracle form and are keyed by the TableSpec columns; users omit the password.
    """
    spec = ResolveTableSpec(tableName)
    sourceRows = StateSourceRows(state, spec)
    rows = [{column: row[column] for column in spec.columns} for row in sourceRows]
    return sorted(rows, key=lambda row: RowSortKey(row, spec))


def DailyRows(dailyRecords: Iterable[str]) -> list[dict[str, object]]:
    """Return transactions.daily_transactions: DALYTRAN records with record_seq from 1."""
    spec = TABLE_SPECS[DAILY_TRANSACTIONS_TABLE]
    rows = []
    for recordSeq, record in enumerate(dailyRecords, start=1):
        values = {RECORD_SEQ_COLUMN: recordSeq, **ParseRecord(record, DAILY_FIELDS)}
        rows.append({column: values[column] for column in spec.columns})
    return rows


@dataclass(frozen=True)
class TranbkpOutcome:
    """TRANBKP: the TRANSACT.BKUP(+1) records in key order, and the state with TRANSACT empty."""

    bkupRecords: list[str]
    state: OracleState


@dataclass(frozen=True)
class PosttranOutcome:
    """POSTTRAN: the updated state, the DALYREJS(+1) records, the two counters and the status."""

    state: OracleState
    rejectRecords: list[str]
    processedCount: int
    rejectCount: int
    exitCode: int


@dataclass(frozen=True)
class IntcalcInput:
    """INTCALC parameters: the 10-character PARM-DATE and the frozen clock text."""

    runDate: str
    clockText: str


@dataclass(frozen=True)
class IntcalcOutcome:
    """INTCALC: the state with rewritten accounts, the SYSTRAN(+1) records and the status."""

    state: OracleState
    systranRecords: list[str]
    exitCode: int


@dataclass(frozen=True)
class StatementOutcome:
    """CREASTMT: the STATEMNT.PS (80) and STATEMNT.HTML (100) lines and the status."""

    psLines: list[str]
    htmlLines: list[str]
    exitCode: int


@dataclass(frozen=True)
class ReportInput:
    """TRANREPT input: 350-character transaction records, the lookup files and the date range."""

    transactions: Iterable[str]
    xrefs: Mapping[str, dict[str, object]]
    transactionTypes: Mapping[str, dict[str, object]]
    transactionCategories: Mapping[tuple[str, int], dict[str, object]]
    startDate: str
    endDate: str


@dataclass(frozen=True)
class ChainOutcome:
    """Every intermediate and final output of the sample chain, step by step."""

    loadedState: OracleState
    dailyRecords: list[str]
    bkupRun1: TranbkpOutcome
    posttran: PosttranOutcome
    intcalc: IntcalcOutcome
    bkupRun2: TranbkpOutcome
    combinedRecords: list[str]
    finalState: OracleState
    statements: StatementOutcome
    reportLines: list[str]
    exitCode: int


# CVTRA05Y / CVTRA06Y offsets read straight from 350-character record texts.
TRAN_ID_SLICE = slice(0, 16)
TRAN_TYPE_SLICE = slice(16, 18)
TRAN_CAT_SLICE = slice(18, 22)
TRAN_SOURCE_SLICE = slice(22, 32)
TRAN_DESC_SLICE = slice(32, 132)
TRAN_AMOUNT_SLICE = slice(132, 143)
TRAN_CARD_SLICE = slice(262, 278)
ORIG_DATE_SLICE = slice(278, 288)
PROC_DATE_SLICE = slice(304, 314)
CARD_NUM_LENGTH = 16
ACCT_ID_LENGTH = 11
AMOUNT_SCALE = 2


def TransactionKeyOrder(record: str) -> bytes:
    """Return the TRANSACT KSDS key order of a record: TRAN-ID (KEYS(16 0)) in EBCDIC."""
    return EbcdicSortKey(record[TRAN_ID_SLICE])


def RecordAmount(record: str) -> Decimal:
    """Return TRAN-AMT / DALYTRAN-AMT S9(09)V99 of a 350-character record."""
    return DecodeOverpunch(record[TRAN_AMOUNT_SLICE], AMOUNT_SCALE)


def RunTranbkp(state: OracleState) -> TranbkpOutcome:
    """Transcribes jcl/TRANBKP.jcl: STEP05R, STEP05 and STEP10.

    STEP05R (line 23) runs the REPROC unload of TRANSACT in key order into TRANSACT.BKUP(+1);
    STEP05 (line 37) deletes the cluster and its AIX, and STEP10 (line 51) defines it empty.
    """
    bkupRecords = sorted(state.transactions.values(), key=TransactionKeyOrder)
    return TranbkpOutcome(bkupRecords, dataclasses.replace(state, transactions={}))


# CBTRN02C WS-VALIDATION-FAIL-REASON codes and texts (cbl/CBTRN02C.cbl lines 385-419).
NO_REJECT = (0, "")
REJECT_INVALID_CARD = (100, "INVALID CARD NUMBER FOUND")
REJECT_ACCOUNT_NOT_FOUND = (101, "ACCOUNT RECORD NOT FOUND")
REJECT_OVERLIMIT = (102, "OVERLIMIT TRANSACTION")
REJECT_EXPIRED_ACCOUNT = (103, "TRANSACTION RECEIVED AFTER ACCT EXPIRATION")
REJECT_DESCRIPTION_LENGTH = 76
# WS-COUNTERS: WS-TRANSACTION-COUNT and WS-REJECT-COUNT are PIC 9(09) VALUE 0
# (cbl/CBTRN02C.cbl lines 184-186), so each ADD 1 stores (n + 1) % 10**9: 999999999 -> 0.
POSTTRAN_COUNTER_MODULUS = 10**9


@dataclass
class PostingWorkArea:
    """The files and WS-COUNTERS CBTRN02C changes while it runs; mutable, private to RunPosttran.

    processedCount and rejectCount are the stored WS-TRANSACTION-COUNT and WS-REJECT-COUNT
    (cbl/CBTRN02C.cbl lines 184-186); rejectRecords is every DALYREJS record written.
    """

    accounts: dict[str, dict[str, object]]
    categoryBalances: dict[tuple[str, str, int], dict[str, object]]
    transactions: dict[str, str]
    xrefs: Mapping[str, dict[str, object]]
    procTimestamp: tuple[datetime | None, str]
    rejectRecords: list[str] = field(default_factory=list)
    processedCount: int = 0
    rejectCount: int = 0


def NextCounterValue(counter: int) -> int:
    """Transcribes ADD 1 into a PIC 9(09) WS-COUNTERS receiver, cbl/CBTRN02C.cbl lines 206, 214.

    The source has no ON SIZE ERROR, so the high-order digit is lost: 999999999 -> 0.
    """
    return (counter + 1) % POSTTRAN_COUNTER_MODULUS


def CheckAccountLimits(account: Mapping[str, object], record: str) -> tuple[int, str]:
    """Transcribes the NOT INVALID KEY branch of 1500-B-LOOKUP-ACCT, cbl/CBTRN02C.cbl 403-420.

    WS-TEMP-BAL S9(09)V99 = cycle credit - cycle debit + amount is tested against the credit
    limit (102). The expiry test (103) runs independently and overwrites a 102. It compares the
    X(10) expiry with DALYTRAN-ORIG-TS(1:10) alphanumerically, so a blank expiry is below any date.
    """
    debit = ORACLE_CONTEXT.minus(account["curr_cycle_debit"])
    tempBal = StoreSum((account["curr_cycle_credit"], debit, RecordAmount(record)), 9, 2)
    reason = NO_REJECT
    if not account["credit_limit"] >= tempBal:
        reason = REJECT_OVERLIMIT
    expiration = EbcdicSortKey(str(account["expiration_date"]).ljust(DATE_LENGTH))
    if not expiration >= EbcdicSortKey(record[ORIG_DATE_SLICE]):
        reason = REJECT_EXPIRED_ACCOUNT
    return reason


def ValidateDailyRecord(work: PostingWorkArea, record: str) -> tuple[int, str]:
    """Transcribes 1500-VALIDATE-TRAN with 1500-A-LOOKUP-XREF and 1500-B-LOOKUP-ACCT.

    cbl/CBTRN02C.cbl lines 370-422: the card is read in CCXREF by its exact 16-character key
    (100 when absent), then the xref's account in ACCTDAT (101 when absent), then the limits.

    Returns:
        (reason, description); (0, "") when the record is valid.
    """
    xref = work.xrefs.get(record[TRAN_CARD_SLICE].rstrip(" "))
    if xref is None:
        return REJECT_INVALID_CARD
    account = work.accounts.get(xref["acct_id"])
    if account is None:
        return REJECT_ACCOUNT_NOT_FOUND
    return CheckAccountLimits(account, record)


def BuildRejectRecord(record: str, reason: int, description: str) -> str:
    """Transcribes 2500-WRITE-REJECT-REC, cbl/CBTRN02C.cbl lines 446-465: one DALYREJS record.

    MOVE DALYTRAN-RECORD TO REJECT-TRAN-DATA keeps the raw 350 daily characters, followed by
    WS-VALIDATION-FAIL-REASON 9(04) and its X(76) description: 430 characters.
    """
    trailer = f"{reason:04d}" + description.ljust(REJECT_DESCRIPTION_LENGTH)
    return record[:TRANSACTION_LRECL] + trailer[: DALYREJS_LRECL - TRANSACTION_LRECL]


def UpdateCategoryBalance(work: PostingWorkArea, acctId: str, daily: Mapping[str, object]) -> None:
    """Transcribes 2700-UPDATE-TCATBAL with its 2700-A create and 2700-B update paragraphs.

    cbl/CBTRN02C.cbl lines 467-542: the key is the xref account, the daily type and category.
    A missing record is INITIALIZEd (balance 0) and created; either way ADD DALYTRAN-AMT TO
    TRAN-CAT-BAL stores into S9(09)V99.
    """
    key = (acctId, daily["tran_type_cd"], daily["tran_cat_cd"])
    existing = work.categoryBalances.get(key)
    if existing is None:
        existing = dict(zip(CATEGORY_KEY_COLUMNS, key, strict=True)) | {"balance": ZERO_AMOUNT}
    balance = StoreSum((existing["balance"], daily["tran_amt"]), 9, 2)
    work.categoryBalances[key] = {**existing, "balance": balance}


def UpdateAccountBalances(work: PostingWorkArea, acctId: str, amount: Decimal) -> None:
    """Transcribes 2800-UPDATE-ACCOUNT-REC, cbl/CBTRN02C.cbl lines 545-560.

    The amount is added to ACCT-CURR-BAL, and to the cycle credit when it is zero or more, else to
    the cycle debit, each stored into S9(10)V99. Path 109 (REWRITE INVALID KEY) is unreachable
    here because the account was just read in 1500-B-LOOKUP-ACCT.
    """
    account = work.accounts[acctId]
    cycleColumn = "curr_cycle_credit" if amount >= 0 else "curr_cycle_debit"
    work.accounts[acctId] = {
        **account,
        "curr_balance": StoreSum((account["curr_balance"], amount), 10, 2),
        cycleColumn: StoreSum((account[cycleColumn], amount), 10, 2),
    }


def BuildPostedRecord(
    daily: Mapping[str, object], procTimestamp: tuple[datetime | None, str]
) -> str:
    """Transcribes the moves of 2000-POST-TRANSACTION, cbl/CBTRN02C.cbl lines 424-438.

    Every DALYTRAN field moves to TRAN-RECORD, ORIG-TS unchanged (line 436); PROC-TS is the DB2
    clock text of Z-GET-DB2-FORMAT-TIMESTAMP (lines 692-705).
    """
    procValue, procFormat = procTimestamp
    values = {**daily, "proc_ts": procValue, "proc_ts" + TIMESTAMP_FORMAT_SUFFIX: procFormat}
    return FormatRecord(values, TRANSACTION_FIELDS, TRANSACTION_LRECL)


def PostDailyRecord(work: PostingWorkArea, record: str) -> bool:
    """Transcribes one pass of the CBTRN02C mainline loop, cbl/CBTRN02C.cbl lines 202-219.

    A rejected record runs ADD 1 TO WS-REJECT-COUNT, then 2500-WRITE-REJECT-REC (lines 214-215).
    A valid record runs 2000-POST-TRANSACTION (424-444): 2700, 2800, then 2900 writes TRANSACT.

    Returns:
        False when 2900-WRITE-TRANSACTION-FILE (562-579) meets a duplicate TRAN-ID, which performs
        9999-ABEND-PROGRAM (abend 999) after this record's balance and account updates.
    """
    if len(record) != TRANSACTION_LRECL:
        raise ValueError(f"daily record of {len(record)} characters; DALYTRAN LRECL is 350")
    reason, description = ValidateDailyRecord(work, record)
    if reason != 0:
        work.rejectCount = NextCounterValue(work.rejectCount)
        work.rejectRecords.append(BuildRejectRecord(record, reason, description))
        return True
    daily = ParseRecord(record, DAILY_FIELDS)
    acctId = work.xrefs[record[TRAN_CARD_SLICE].rstrip(" ")]["acct_id"]
    UpdateCategoryBalance(work, acctId, daily)
    UpdateAccountBalances(work, acctId, daily["tran_amt"])
    posted = BuildPostedRecord(daily, work.procTimestamp)
    if posted[TRAN_ID_SLICE] in work.transactions:
        return False
    work.transactions[posted[TRAN_ID_SLICE]] = posted
    return True


def NewPostingWorkArea(state: OracleState, clockText: str) -> PostingWorkArea:
    """Open the CBTRN02C files for one run: copies of the dicts POSTTRAN changes, and the clock."""
    return PostingWorkArea(
        accounts=dict(state.accounts),
        categoryBalances=dict(state.categoryBalances),
        transactions=dict(state.transactions),
        xrefs=state.xrefs,
        procTimestamp=DecodeTimestampText(clockText),
    )


def PostDailyFile(work: PostingWorkArea, dailyRecords: Iterable[str]) -> bool:
    """Transcribes the PERFORM UNTIL END-OF-FILE loop of cbl/CBTRN02C.cbl lines 202-219.

    Each record read runs ADD 1 TO WS-TRANSACTION-COUNT (line 206) into work.processedCount
    before it is validated; rejects add to work.rejectCount in PostDailyRecord.

    Returns:
        Whether the run abended on a duplicate TRAN-ID.
    """
    for record in dailyRecords:
        work.processedCount = NextCounterValue(work.processedCount)
        if not PostDailyRecord(work, record):
            return True
    return False


def PosttranExitCode(rejectCount: int, abended: bool) -> int:
    """Transcribes the CBTRN02C return code, cbl/CBTRN02C.cbl lines 229-230, and abend 999.

    Args:
        rejectCount: The stored WS-REJECT-COUNT, already wrapped at 10**9.
        abended: Whether 9999-ABEND-PROGRAM ended the run.

    Returns:
        EXIT_ABEND after an abend; EXIT_REJECTS when WS-REJECT-COUNT > 0; else EXIT_OK.
    """
    if abended:
        return EXIT_ABEND
    if rejectCount > 0:
        return EXIT_REJECTS
    return EXIT_OK


def RunPosttran(state: OracleState, dailyRecords: Iterable[str], clockText: str) -> PosttranOutcome:
    """Transcribes CBTRN02C (jcl/POSTTRAN.jcl STEP15), cbl/CBTRN02C.cbl lines 193-234.

    Records are processed in file order; each completed write stays, as in the legacy VSAM files.
    The status is 4 when the stored WS-REJECT-COUNT is above zero (lines 229-230), else 0. Both
    PIC 9(09) counters wrap at 10**9 (lines 184-186), so exactly 10**9 rejects store 0 and give
    status 0, while rejectRecords still holds every DALYREJS record. A duplicate TRAN-ID gives
    exit 16 and the partial state, which already holds that record's category-balance and account
    updates: what a --force rerun over already-posted records yields. On exit 16, rejectRecords
    holds what was written before the abend, which DALYREJS DISP=(NEW,CATLG,DELETE) discards.
    """
    work = NewPostingWorkArea(state, clockText)
    abended = PostDailyFile(work, dailyRecords)
    rejects = list(work.rejectRecords)
    exitCode = PosttranExitCode(work.rejectCount, abended)
    newState = dataclasses.replace(
        state,
        accounts=work.accounts,
        categoryBalances=work.categoryBalances,
        transactions=work.transactions,
    )
    return PosttranOutcome(newState, rejects, work.processedCount, work.rejectCount, exitCode)


# CBACT04C constants: the /1200 of 1300-COMPUTE-INTEREST, the DEFAULT group of 1200-A, the
# WS-TRANID-SUFFIX 9(06) wrap (line 173) and the constant fields of 1300-B-WRITE-TX (482-494).
INTEREST_DIVISOR = Decimal(1200)
DEFAULT_GROUP_ID = "DEFAULT"
PARM_DATE_LENGTH = 10
TRAN_ID_SUFFIX_MODULUS = 10**6
INTEREST_DESCRIPTION_PREFIX = "Int. for a/c "
INTEREST_RECORD_CONSTANTS: dict[str, object] = {
    "tran_type_cd": "01",
    "tran_cat_cd": 5,
    "tran_source": "System",
    "merchant_id": "000000000",
    "merchant_name": "",
    "merchant_city": "",
    "merchant_zip": "",
}


@dataclass
class InterestWorkArea:
    """CBACT04C working storage while it runs; mutable and private to RunIntcalc.

    account and xref are ACCOUNT-RECORD and CARD-XREF-RECORD as last read; lastAcctId is
    WS-LAST-ACCT-NUM (None until the first record, standing for WS-FIRST-TIME = 'Y').
    """

    state: OracleState
    runDate: str
    timestamp: tuple[datetime | None, str]
    accounts: dict[str, dict[str, object]]
    systranRecords: list[str] = field(default_factory=list)
    lastAcctId: str | None = None
    account: dict[str, object] | None = None
    xref: dict[str, object] | None = None
    totalInt: Decimal = ZERO_AMOUNT
    suffix: int = 0


def NextTranIdSuffix(suffix: int) -> int:
    """Transcribes ADD 1 TO WS-TRANID-SUFFIX (PIC 9(06)), cbl/CBACT04C.cbl line 474: 999999 -> 0."""
    return (suffix + 1) % TRAN_ID_SUFFIX_MODULUS


def FirstXrefForAccount(
    xrefs: Mapping[str, dict[str, object]], acctId: str
) -> dict[str, object] | None:
    """Transcribes 1110-GET-XREF-DATA, cbl/CBACT04C.cbl lines 393-413 (READ ... KEY IS ACCT-ID).

    The CXACAIX path returns the first of an account's cards in prime-key (card_num EBCDIC)
    order. None means INVALID KEY, which the program turns into an abend.
    """
    matches = [xref for xref in xrefs.values() if xref["acct_id"] == acctId]
    if not matches:
        return None
    return min(matches, key=lambda xref: EbcdicSortKey(str(xref["card_num"]).ljust(16)))


def RewriteAccount(work: InterestWorkArea) -> None:
    """Transcribes 1050-UPDATE-ACCOUNT, cbl/CBACT04C.cbl lines 350-370.

    ADD WS-TOTAL-INT TO ACCT-CURR-BAL stores into S9(10)V99, both cycle fields become 0, and the
    account record last read is rewritten.
    """
    account = work.account
    work.accounts[str(account["acct_id"])] = {
        **account,
        "curr_balance": StoreSum((account["curr_balance"], work.totalInt), 10, 2),
        "curr_cycle_credit": ZERO_AMOUNT,
        "curr_cycle_debit": ZERO_AMOUNT,
    }


def StartAccount(work: InterestWorkArea, acctId: str) -> bool:
    """Transcribes the account-change branch of the mainline, cbl/CBACT04C.cbl lines 194-206.

    After the first account, 1050-UPDATE-ACCOUNT rewrites the previous one. WS-TOTAL-INT is reset,
    then 1100-GET-ACCT-DATA (372-391) reads the account and 1110-GET-XREF-DATA its first card.

    Returns:
        False when either read is INVALID KEY, which performs 9999-ABEND-PROGRAM.
    """
    if work.lastAcctId is not None:
        RewriteAccount(work)
    work.totalInt = ZERO_AMOUNT
    work.lastAcctId = acctId
    work.account = work.accounts.get(acctId)
    if work.account is None:
        return False
    work.xref = FirstXrefForAccount(work.state.xrefs, acctId)
    return work.xref is not None


def LookupInterestRate(work: InterestWorkArea, balance: Mapping[str, object]) -> Decimal | None:
    """Transcribes 1200-GET-INTEREST-RATE and 1200-A-GET-DEFAULT-INT-RATE, cbl/CBACT04C.cbl 415-460.

    The key is ACCT-GROUP-ID with the balance's type and category. Status 23 retries with the
    group DEFAULT; None means DEFAULT is missing too, which performs 9999-ABEND-PROGRAM.
    """
    groups = work.state.disclosureGroups
    typeCode, categoryCode = balance["tran_type_cd"], balance["tran_cat_cd"]
    group = groups.get((work.account["group_id"], typeCode, categoryCode))
    if group is None:
        group = groups.get((DEFAULT_GROUP_ID, typeCode, categoryCode))
    return None if group is None else group["interest_rate"]


def TimestampColumns(timestamp: tuple[datetime | None, str]) -> dict[str, object]:
    """Return orig_ts and proc_ts, with their format codes, both set to one timestamp."""
    value, formatCode = timestamp
    return {
        "orig_ts": value,
        "orig_ts" + TIMESTAMP_FORMAT_SUFFIX: formatCode,
        "proc_ts": value,
        "proc_ts" + TIMESTAMP_FORMAT_SUFFIX: formatCode,
    }


def BuildInterestRecord(work: InterestWorkArea, monthlyInterest: Decimal) -> str:
    """Transcribes 1300-B-WRITE-TX, cbl/CBACT04C.cbl lines 473-515: one SYSTRAN record.

    TRAN-ID is PARM-DATE followed by WS-TRANID-SUFFIX; type 01, category 0005, source System;
    the description is 'Int. for a/c ' and ACCT-ID; merchant ID 0 and blank merchant fields; the
    xref card; and both timestamps from Z-GET-DB2-FORMAT-TIMESTAMP (lines 496-498).
    """
    values = {
        **INTEREST_RECORD_CONSTANTS,
        "tran_id": work.runDate[:PARM_DATE_LENGTH].ljust(PARM_DATE_LENGTH) + f"{work.suffix:06d}",
        "tran_desc": INTEREST_DESCRIPTION_PREFIX + str(work.account["acct_id"]),
        "tran_amt": monthlyInterest,
        "card_num": work.xref["card_num"],
        **TimestampColumns(work.timestamp),
    }
    return FormatRecord(values, TRANSACTION_FIELDS, TRANSACTION_LRECL)


def ComputeInterest(work: InterestWorkArea, balance: Mapping[str, object], rate: Decimal) -> None:
    """Transcribes 1300-COMPUTE-INTEREST, cbl/CBACT04C.cbl lines 462-470.

    WS-MONTHLY-INT S9(09)V99 = (TRAN-CAT-BAL * DIS-INT-RATE) / 1200, then ADD to WS-TOTAL-INT
    and 1300-B-WRITE-TX. 1400-COMPUTE-FEES (lines 518-520) is an empty stub, so nothing follows.
    """
    product = ORACLE_CONTEXT.multiply(balance["balance"], rate)
    monthlyInterest = TruncateTo(ORACLE_CONTEXT.divide(product, INTEREST_DIVISOR), 9, 2)
    work.totalInt = StoreSum((work.totalInt, monthlyInterest), 9, 2)
    work.suffix = NextTranIdSuffix(work.suffix)
    work.systranRecords.append(BuildInterestRecord(work, monthlyInterest))


def ProcessCategoryBalance(work: InterestWorkArea, balance: Mapping[str, object]) -> bool:
    """Transcribes one record of the CBACT04C mainline loop, cbl/CBACT04C.cbl lines 191-217.

    Returns:
        False when the step abends: a missing account, xref or DEFAULT disclosure group.
    """
    if balance["acct_id"] != work.lastAcctId and not StartAccount(work, str(balance["acct_id"])):
        return False
    rate = LookupInterestRate(work, balance)
    if rate is None:
        return False
    if rate != 0:
        ComputeInterest(work, balance, rate)
    return True


def RunIntcalc(state: OracleState, intcalcInput: IntcalcInput) -> IntcalcOutcome:
    """Transcribes CBACT04C (jcl/INTCALC.jcl STEP15), cbl/CBACT04C.cbl lines 180-232.

    TCATBALF is read in key order (acct_id, type, category); TCATBALF itself is input only.
    The final account is never rewritten: the end-of-file READ ends the PERFORM UNTIL before its
    outer ELSE (lines 188-222) can perform 1050-UPDATE-ACCOUNT. An empty file, or a single
    account, therefore rewrites nothing, while the last account's SYSTRAN records are still
    written. Rate-0 records write nothing and keep the suffix; a zero balance at a non-zero rate
    still writes a 0.00 record. A missing account, xref or DEFAULT group gives exit 16.
    """
    work = InterestWorkArea(
        state=state,
        runDate=intcalcInput.runDate,
        timestamp=DecodeTimestampText(intcalcInput.clockText),
        accounts=dict(state.accounts),
    )
    exitCode = EXIT_OK
    for balance in StateRows(state, CATEGORY_BALANCES_TABLE):
        if not ProcessCategoryBalance(work, balance):
            exitCode = EXIT_ABEND
            break
    newState = dataclasses.replace(state, accounts=work.accounts)
    return IntcalcOutcome(newState, list(work.systranRecords), exitCode)


def RunCombtran(bkupRecords: Iterable[str], systranRecords: Iterable[str]) -> list[str]:
    """Transcribes jcl/COMBTRAN.jcl: STEP05R (line 22) and STEP10 (line 41).

    STEP05R sorts TRANSACT.BKUP(0) and SYSTRAN(0) with SORT FIELDS=(TRAN-ID,A) (line 30) into
    TRANSACT.COMBINED(+1); STEP10's REPRO of that into TRANSACT is ReloadCombinedTransactions.
    The sort is stable, so equal keys keep their input order.
    """
    return sorted([*bkupRecords, *systranRecords], key=TransactionKeyOrder)


def CombinedDuplicateMessage(recordNumber: int, firstRecordNumber: int | None) -> str:
    """Return COMBTRAN STEP10's refusal of a duplicate TRAN-ID, naming records, never the key.

    Args:
        recordNumber: The 1-based TRANSACT.COMBINED(+1) record that repeats the key.
        firstRecordNumber: The earlier combined record that holds the key, or None when TRANSACT
            already held it before the REPRO.
    """
    keyColumns = TABLE_SPECS[TRANSACTIONS_TABLE].keyColumns
    if firstRecordNumber is None:
        reason = f"duplicate key ({', '.join(keyColumns)}) of a record already in TRANSACT"
    else:
        reason = DuplicateKeyError(recordNumber, firstRecordNumber, keyColumns).reason
    return f"COMBTRAN STEP10 REPRO: TRANSACT.COMBINED(+1) record {recordNumber}: {reason}"


def ReloadCombinedTransactions(state: OracleState, combinedRecords: Sequence[str]) -> OracleState:
    """Transcribes jcl/COMBTRAN.jcl STEP10 (lines 41-48): REPRO TRANSACT.COMBINED(+1) to TRANSACT.

    Each combined record is added to the TRANSACT KSDS (KEYS(16 0)) under its TRAN-ID. The REPRO
    refuses a TRAN-ID that TRANSACT already holds or that an earlier combined record carries,
    and COMBTRAN then ends at status 12 with full rollback, so no state is returned and the
    state passed in is left as it was.

    Args:
        state: The state COMBTRAN starts from, after the second TRANBKP emptied TRANSACT.
        combinedRecords: TRANSACT.COMBINED(+1) in file order, as RunCombtran returns it.

    Returns:
        A new state whose transactions hold the earlier ones plus every combined record.

    Raises:
        OracleAbend: A combined record repeats a TRAN-ID, with exitCode EXIT_UTILITY_FAILURE.
    """
    transactions = dict(state.transactions)
    firstRecordNumbers: dict[str, int] = {}
    for recordNumber, record in enumerate(combinedRecords, start=1):
        tranId = record[TRAN_ID_SLICE]
        if tranId in transactions:
            message = CombinedDuplicateMessage(recordNumber, firstRecordNumbers.get(tranId))
            raise OracleAbend(message, EXIT_UTILITY_FAILURE)
        transactions[tranId] = record
        firstRecordNumbers[tranId] = recordNumber
    return dataclasses.replace(state, transactions=transactions)


def TrailingSign(storedValue: Decimal) -> str:
    """Return the trailing '-' sign position of an edited picture: '-' when negative, else ' '."""
    return "-" if storedValue < 0 else " "


def EditBalance9(value: Decimal) -> str:
    """Edit a value with PIC 9(9).99- (ST-CURR-BAL, cbl/CBSTM03A.CBL line 113): 13 characters.

    The S9(10)V99 balance loses its high-order digit in the move; every digit is shown.
    """
    storedValue = TruncateTo(value, 9, 2)
    digits = DigitText(storedValue, 9, 2)
    return digits[:9] + "." + digits[9:] + TrailingSign(storedValue)


def EditAmountZ(value: Decimal) -> str:
    """Edit a value with PIC Z(9).99- (ST-TRANAMT, ST-TOTAL-TRAMT, lines 137, 142): 13 characters.

    Leading integer zeros become spaces (all nine when the integer part is 0); the point and both
    decimals always print, so zero is "         .00 ".
    """
    storedValue = TruncateTo(value, 9, 2)
    digits = DigitText(storedValue, 9, 2)
    integerText = digits[:9].lstrip("0").rjust(9)
    return integerText + "." + digits[9:] + TrailingSign(storedValue)


def GroupedIntegerText(integerDigits: str) -> str:
    """Zero-suppress nine integer digits through ZZZ,ZZZ,ZZZ: 11 characters.

    Leading zeros become spaces, and so does a comma with no significant digit to its left.
    """
    pieces = []
    significant = False
    for index, digit in enumerate(integerDigits):
        if index in (3, 6):
            pieces.append("," if significant else " ")
        significant = significant or digit != "0"
        pieces.append(digit if significant else " ")
    return "".join(pieces)


def EditReportAmount(value: Decimal, signCharacter: str) -> str:
    """Edit a value with -ZZZ,ZZZ,ZZZ.ZZ or +ZZZ,ZZZ,ZZZ.ZZ (cpy/CVTRA07Y.cpy): 15 characters.

    Every digit position is Z, so a zero value prints as 15 spaces (IBM Enterprise COBOL). The
    fixed sign comes first: '-' shows '-' or a space, '+' shows '+' or '-'. Suppression stops at
    the decimal point, so the two decimals always print. -1234.56 with '-' is '-      1,234.56'.
    """
    storedValue = TruncateTo(value, 9, 2)
    if storedValue == 0:
        return " " * 15
    digits = DigitText(storedValue, 9, 2)
    if storedValue < 0:
        sign = "-"
    else:
        sign = "+" if signCharacter == "+" else " "
    return sign + GroupedIntegerText(digits[:9]) + "." + digits[9:]


def CutAtDelimiter(text: str, delimiter: str | None) -> str:
    """Return the part of a STRING source sent before its first delimiter (all of it for SIZE)."""
    if delimiter is None:
        return text
    return text.split(delimiter, 1)[0]


def StringDelimited(parts: Iterable[tuple[str, str | None]], width: int) -> str:
    """Transcribes COBOL STRING ... DELIMITED BY ... INTO a receiver initialised to spaces.

    Args:
        parts: (source, delimiter) pairs in statement order; a None delimiter is DELIMITED BY SIZE.
        width: The receiver length. Overflowing text is cut off; the rest stays spaces.
    """
    text = "".join(CutAtDelimiter(source, delimiter) for source, delimiter in parts)
    return text[:width].ljust(width)


def PaddedText(value: object, width: int) -> str:
    """Return a value as a MOVE into X(width) leaves it: left-justified, space-padded, cut."""
    text = "" if value is None else str(value)
    return text.ljust(width)[:width]


def EscapeHtml(text: str) -> str:
    """Escape only &, < and > in a statement field value; apostrophes and quotes stay raw."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# CBSTM03A STATEMENT-LINES, cbl/CBSTM03A.CBL lines 85-146 (80-character STATEMNT.PS lines).
PS_LINE0 = "*" * 31 + "START OF STATEMENT" + "*" * 31
PS_LINE5 = "-" * 80
PS_LINE6 = " " * 33 + "Basic Details".ljust(14) + " " * 33
PS_LINE10 = "-" * 80
PS_LINE11 = " " * 30 + "TRANSACTION SUMMARY " + " " * 30
PS_LINE12 = "-" * 80
PS_LINE13 = "Tran ID         " + "Tran Details    ".ljust(51) + "  Tran Amount"
PS_LINE15 = "*" * 32 + "END OF STATEMENT" + "*" * 32
PS_ACCOUNT_ID_LABEL = "Account ID         :"
PS_BALANCE_LABEL = "Current Balance    :"
PS_FICO_LABEL = "FICO Score         :"
PS_TOTAL_LABEL = "Total EXP:" + " " * 56 + "$"

# CBSTM03A HTML-LINES, cbl/CBSTM03A.CBL lines 148-223. A continued literal joins the fragment that
# ends in column 72 with the continuation text, with no space inserted.
HTML_L01 = "<!DOCTYPE html>"
HTML_L02 = '<html lang="en">'
HTML_L03 = "<head>"
HTML_L04 = '<meta charset="utf-8">'
HTML_L05 = "<title>HTML Table Layout</title>"
HTML_L06 = "</head>"
HTML_L07 = '<body style="margin:0px;">'
HTML_L08 = '<table  align="center" frame="box" style="width:70%; font:12px Segoe UI,sans-serif;">'
HTML_LTRS = "<tr>"
HTML_LTRE = "</tr>"
HTML_LTDE = "</td>"
HTML_L10 = '<td colspan="3" style="padding:0px 5px;background-color:#1d1d96b3;">'
HTML_L15 = '<td colspan="3" style="padding:0px 5px;background-color:#FFAF33;">'
HTML_L16 = '<p style="font-size:16px">Bank of XYZ</p>'
HTML_L17 = "<p>410 Terry Ave N</p>"
HTML_L18 = "<p>Seattle WA 99999</p>"
HTML_L22_35 = '<td colspan="3" style="padding:0px 5px;background-color:#f2f2f2;">'
HTML_L30_42 = (
    '<td colspan="3" style="padding:0px 5px;background-color:#33FFD1; text-align:center;">'
)
HTML_L31 = '<p style="font-size:16px">Basic Details</p>'
HTML_L43 = '<p style="font-size:16px">Transaction Summary</p>'
HTML_L47 = '<td style="width:25%; padding:0px 5px; background-color:#33FF5E; text-align:left;">'
HTML_L48 = '<p style="font-size:16px">Tran ID</p>'
HTML_L50 = '<td style="width:55%; padding:0px 5px; background-color:#33FF5E; text-align:left;">'
HTML_L51 = '<p style="font-size:16px">Tran Details</p>'
HTML_L53 = '<td style="width:20%; padding:0px 5px; background-color:#33FF5E; text-align:right;">'
HTML_L54 = '<p style="font-size:16px">Amount</p>'
HTML_L58 = '<td style="width:25%; padding:0px 5px; background-color:#f2f2f2; text-align:left;">'
HTML_L61 = '<td style="width:55%; padding:0px 5px; background-color:#f2f2f2; text-align:left;">'
HTML_L64 = '<td style="width:20%; padding:0px 5px; background-color:#f2f2f2; text-align:right;">'
HTML_L75 = "<h3>End of Statement</h3>"
HTML_L78 = "</table>"
HTML_L79 = "</body>"
HTML_L80 = "</html>"
HTML_ACCOUNT_HEADER_PREFIX = "<h3>Statement for Account Number: "
HTML_ACCOUNT_HEADER_SUFFIX = "</h3>"
HTML_NAME_PREFIX = '<p style="font-size:16px">'
HTML_PARAGRAPH_OPEN = "<p>"
HTML_PARAGRAPH_CLOSE = "</p>"
HTML_ACCOUNT_ID_LABEL = "<p>Account ID         : "
HTML_BALANCE_LABEL = "<p>Current Balance    : "
HTML_FICO_LABEL = "<p>FICO Score         : "
# 5200-WRITE-HTML-NMADBS cuts the name and address lines at a double space and appends one; the
# basic-detail and transaction fields are STRINGed DELIMITED BY '*'.
HTML_NAME_DELIMITER = "  "
HTML_FIELD_DELIMITER = "*"

# Receiver widths of the statement fields (CUSTREC, CVACT01Y and STATEMENT-LINES).
CUSTOMER_NAME_WIDTH = 25
ST_NAME_WIDTH = 75
ST_ADDRESS_WIDTH = 50
ST_ADD3_WIDTH = 80
L23_NAME_WIDTH = 50
ST_ACCT_ID_WIDTH = 20
ST_FICO_WIDTH = 20
ST_TRANDT_WIDTH = 49
FICO_DIGITS = 3
STATEMENT_MAX_CARDS = 51
STATEMENT_MAX_TRANSACTIONS = 10
# The STRING sources of ST-NAME (lines 462-469) and ST-ADD3 (lines 472-481): each column is
# DELIMITED BY ' ' and followed by ' ' DELIMITED BY SIZE.
ST_NAME_SOURCES = (
    ("first_name", CUSTOMER_NAME_WIDTH),
    ("middle_name", CUSTOMER_NAME_WIDTH),
    ("last_name", CUSTOMER_NAME_WIDTH),
)
ST_ADD3_SOURCES = (
    ("addr_line_3", 50),
    ("addr_state_cd", 2),
    ("addr_country_cd", 3),
    ("addr_zip", 10),
)


@dataclass(frozen=True)
class LineSlot:
    """A named slot in a statement line table, filled per statement with a line built from data."""

    name: str


SLOT_PS_NAME = LineSlot("psName")
SLOT_PS_ADDRESS_1 = LineSlot("psAddress1")
SLOT_PS_ADDRESS_2 = LineSlot("psAddress2")
SLOT_PS_ADDRESS_3 = LineSlot("psAddress3")
SLOT_PS_ACCOUNT_ID = LineSlot("psAccountId")
SLOT_PS_BALANCE = LineSlot("psBalance")
SLOT_PS_FICO = LineSlot("psFico")
SLOT_PS_TRANSACTION = LineSlot("psTransaction")
SLOT_PS_TOTAL = LineSlot("psTotal")
SLOT_HTML_ACCOUNT_HEADER = LineSlot("htmlAccountHeader")
SLOT_HTML_NAME = LineSlot("htmlName")
SLOT_HTML_ADDRESS_1 = LineSlot("htmlAddress1")
SLOT_HTML_ADDRESS_2 = LineSlot("htmlAddress2")
SLOT_HTML_ADDRESS_3 = LineSlot("htmlAddress3")
SLOT_HTML_ACCOUNT_ID = LineSlot("htmlAccountId")
SLOT_HTML_BALANCE = LineSlot("htmlBalance")
SLOT_HTML_FICO = LineSlot("htmlFico")
SLOT_HTML_TRAN_ID = LineSlot("htmlTranId")
SLOT_HTML_TRAN_DESC = LineSlot("htmlTranDesc")
SLOT_HTML_TRAN_AMOUNT = LineSlot("htmlTranAmount")

# STATEMNT.PS writes: ST-LINE0 (5000, line 460), ST-LINE1 to ST-LINE13 (lines 488-502), one
# ST-LINE14 per transaction (6000, line 679), then ST-LINE12, ST-LINE14A and ST-LINE15
# (4000, lines 435-437).
PS_STATEMENT_HEAD = (
    PS_LINE0,
    SLOT_PS_NAME,
    SLOT_PS_ADDRESS_1,
    SLOT_PS_ADDRESS_2,
    SLOT_PS_ADDRESS_3,
    PS_LINE5,
    PS_LINE6,
    PS_LINE5,
    SLOT_PS_ACCOUNT_ID,
    SLOT_PS_BALANCE,
    SLOT_PS_FICO,
    PS_LINE10,
    PS_LINE11,
    PS_LINE12,
    PS_LINE13,
    PS_LINE12,
)
PS_TRANSACTION_LINES = (SLOT_PS_TRANSACTION,)
PS_STATEMENT_TAIL = (PS_LINE12, SLOT_PS_TOTAL, PS_LINE15)

# STATEMNT.HTML writes: 5100-WRITE-HTML-HEADER (lines 506-555), then 5200-WRITE-HTML-NMADBS
# (558-672), then 6000-WRITE-TRANS per transaction (675-723), then the tail of 4000 (439-454).
HTML_STATEMENT_HEAD = (
    HTML_L01,
    HTML_L02,
    HTML_L03,
    HTML_L04,
    HTML_L05,
    HTML_L06,
    HTML_L07,
    HTML_L08,
    HTML_LTRS,
    HTML_L10,
    SLOT_HTML_ACCOUNT_HEADER,
    HTML_LTDE,
    HTML_LTRE,
    HTML_LTRS,
    HTML_L15,
    HTML_L16,
    HTML_L17,
    HTML_L18,
    HTML_LTDE,
    HTML_LTRE,
    HTML_LTRS,
    HTML_L22_35,
    SLOT_HTML_NAME,
    SLOT_HTML_ADDRESS_1,
    SLOT_HTML_ADDRESS_2,
    SLOT_HTML_ADDRESS_3,
    HTML_LTDE,
    HTML_LTRE,
    HTML_LTRS,
    HTML_L30_42,
    HTML_L31,
    HTML_LTDE,
    HTML_LTRE,
    HTML_LTRS,
    HTML_L22_35,
    SLOT_HTML_ACCOUNT_ID,
    SLOT_HTML_BALANCE,
    SLOT_HTML_FICO,
    HTML_LTDE,
    HTML_LTRE,
    HTML_LTRS,
    HTML_L30_42,
    HTML_L43,
    HTML_LTDE,
    HTML_LTRE,
    HTML_LTRS,
    HTML_L47,
    HTML_L48,
    HTML_LTDE,
    HTML_L50,
    HTML_L51,
    HTML_LTDE,
    HTML_L53,
    HTML_L54,
    HTML_LTDE,
    HTML_LTRE,
)
HTML_TRANSACTION_LINES = (
    HTML_LTRS,
    HTML_L58,
    SLOT_HTML_TRAN_ID,
    HTML_LTDE,
    HTML_L61,
    SLOT_HTML_TRAN_DESC,
    HTML_LTDE,
    HTML_L64,
    SLOT_HTML_TRAN_AMOUNT,
    HTML_LTDE,
    HTML_LTRE,
)
HTML_STATEMENT_TAIL = (
    HTML_LTRS,
    HTML_L10,
    HTML_L75,
    HTML_LTDE,
    HTML_LTRE,
    HTML_L78,
    HTML_L79,
    HTML_L80,
)


@dataclass(frozen=True)
class StatementTexts:
    """The STATEMENT-LINES fields 5000-CREATE-STATEMENT fills for one card (lines 458-485)."""

    name: str
    address1: str
    address2: str
    address3: str
    accountId: str
    balance: str
    fico: str


def DelimitedSources(
    customer: Mapping[str, object], sources: Iterable[tuple[str, int]]
) -> list[tuple[str, str | None]]:
    """Return STRING sources: each X(n) column DELIMITED BY ' ', then ' ' DELIMITED BY SIZE."""
    parts: list[tuple[str, str | None]] = []
    for column, width in sources:
        parts.append((PaddedText(customer[column], width), " "))
        parts.append((" ", None))
    return parts


def BuildStatementTexts(
    customer: Mapping[str, object], account: Mapping[str, object]
) -> StatementTexts:
    """Transcribes the STRINGs and MOVEs of 5000-CREATE-STATEMENT, cbl/CBSTM03A.CBL 458-485.

    INITIALIZE STATEMENT-LINES clears ST-NAME and ST-ADD3 to spaces first. ST-NAME keeps only the
    first word of each name part; ST-ADD3 likewise of address line 3, state, country and ZIP.
    """
    fico = int(customer["fico_credit_score"]) % 10**FICO_DIGITS
    return StatementTexts(
        name=StringDelimited(DelimitedSources(customer, ST_NAME_SOURCES), ST_NAME_WIDTH),
        address1=PaddedText(customer["addr_line_1"], ST_ADDRESS_WIDTH),
        address2=PaddedText(customer["addr_line_2"], ST_ADDRESS_WIDTH),
        address3=StringDelimited(DelimitedSources(customer, ST_ADD3_SOURCES), ST_ADD3_WIDTH),
        accountId=PaddedText(account["acct_id"], ST_ACCT_ID_WIDTH),
        balance=EditBalance9(account["curr_balance"]),
        fico=PaddedText(f"{fico:0{FICO_DIGITS}d}", ST_FICO_WIDTH),
    )


def PsHeadSlots(texts: StatementTexts) -> dict[LineSlot, str]:
    """Return ST-LINE1 to ST-LINE4 and ST-LINE7 to ST-LINE9 (cbl/CBSTM03A.CBL lines 90-119)."""
    return {
        SLOT_PS_NAME: texts.name + " " * 5,
        SLOT_PS_ADDRESS_1: texts.address1 + " " * 30,
        SLOT_PS_ADDRESS_2: texts.address2 + " " * 30,
        SLOT_PS_ADDRESS_3: texts.address3,
        SLOT_PS_ACCOUNT_ID: PS_ACCOUNT_ID_LABEL + texts.accountId + " " * 40,
        SLOT_PS_BALANCE: PS_BALANCE_LABEL + texts.balance + " " * 47,
        SLOT_PS_FICO: PS_FICO_LABEL + texts.fico + " " * 40,
    }


def HtmlFittedLine(prefix: str, fieldText: str, suffix: str) -> str:
    """Return prefix, the HTML-escaped field text and suffix, within STATEMENT_HTML_WIDTH.

    Escaping lengthens &, < and >, so raw characters are dropped from the end of the field text
    until the escaped line fits the 100-column record; the literals are kept whole.
    """
    while fieldText and len(prefix + EscapeHtml(fieldText) + suffix) > STATEMENT_HTML_WIDTH:
        fieldText = fieldText[:-1]
    return prefix + EscapeHtml(fieldText) + suffix


def HtmlNameLine(prefix: str, text: str) -> str:
    """Return a 5200 name or address line: the field DELIMITED BY '  ', then '  ' and </p>."""
    fieldText = CutAtDelimiter(text, HTML_NAME_DELIMITER)
    return HtmlFittedLine(prefix, fieldText, HTML_NAME_DELIMITER + HTML_PARAGRAPH_CLOSE)


def HtmlFieldLine(prefix: str, text: str) -> str:
    """Return a 5200 or 6000 field line: the literal, the field DELIMITED BY '*', then </p>."""
    fieldText = CutAtDelimiter(text, HTML_FIELD_DELIMITER)
    return HtmlFittedLine(prefix, fieldText, HTML_PARAGRAPH_CLOSE)


def HtmlHeadSlots(texts: StatementTexts) -> dict[LineSlot, str]:
    """Return HTML-L11 (5100, lines 529-530) and the name, address and basic-detail lines of
    5200-WRITE-HTML-NMADBS (cbl/CBSTM03A.CBL lines 560-633).
    """
    accountHeader = HtmlFittedLine(
        HTML_ACCOUNT_HEADER_PREFIX, texts.accountId, HTML_ACCOUNT_HEADER_SUFFIX
    )
    return {
        SLOT_HTML_ACCOUNT_HEADER: accountHeader,
        SLOT_HTML_NAME: HtmlNameLine(HTML_NAME_PREFIX, texts.name[:L23_NAME_WIDTH]),
        SLOT_HTML_ADDRESS_1: HtmlNameLine(HTML_PARAGRAPH_OPEN, texts.address1),
        SLOT_HTML_ADDRESS_2: HtmlNameLine(HTML_PARAGRAPH_OPEN, texts.address2),
        SLOT_HTML_ADDRESS_3: HtmlNameLine(HTML_PARAGRAPH_OPEN, texts.address3),
        SLOT_HTML_ACCOUNT_ID: HtmlFieldLine(HTML_ACCOUNT_ID_LABEL, texts.accountId),
        SLOT_HTML_BALANCE: HtmlFieldLine(HTML_BALANCE_LABEL, texts.balance),
        SLOT_HTML_FICO: HtmlFieldLine(HTML_FICO_LABEL, texts.fico),
    }


def TransactionSlots(record: str) -> dict[LineSlot, str]:
    """Transcribes 6000-WRITE-TRANS, cbl/CBSTM03A.CBL lines 675-723, for one TRXFL record.

    ST-TRANID X(16), ST-TRANDT X(49) from TRNX-DESC and ST-TRANAMT Z(9).99- build ST-LINE14 and
    the three HTML field lines.
    """
    tranId = record[TRAN_ID_SLICE]
    description = record[TRAN_DESC_SLICE][:ST_TRANDT_WIDTH]
    amountText = EditAmountZ(RecordAmount(record))
    return {
        SLOT_PS_TRANSACTION: tranId + " " + description + "$" + amountText,
        SLOT_HTML_TRAN_ID: HtmlFieldLine(HTML_PARAGRAPH_OPEN, tranId),
        SLOT_HTML_TRAN_DESC: HtmlFieldLine(HTML_PARAGRAPH_OPEN, description),
        SLOT_HTML_TRAN_AMOUNT: HtmlFieldLine(HTML_PARAGRAPH_OPEN, amountText),
    }


def RenderLines(
    sequence: Iterable[str | LineSlot], slots: Mapping[LineSlot, str], width: int
) -> list[str]:
    """Write a line table: literals as they are, slots from the dict, each right-padded to width.

    Lines are never truncated here: every slot text already fits its LRECL, the escaped HTML
    field lines through HtmlFittedLine, so each line comes out exactly width columns.
    """
    lines = []
    for item in sequence:
        text = slots[item] if isinstance(item, LineSlot) else item
        lines.append(text.ljust(width))
    return lines


def StatementMasterRecords(
    state: OracleState, xref: Mapping[str, object]
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """Transcribes 2000-CUSTFILE-GET and 3000-ACCTFILE-GET, cbl/CBSTM03A.CBL lines 368-414.

    CBSTM03B 3000-CUSTFILE-PROC and 4000-ACCTFILE-PROC (cbl/CBSTM03B.CBL lines 181-230) read by
    key; any status but '00' performs 9999-ABEND-PROGRAM.
    """
    customer = state.customers.get(str(xref["cust_id"]))
    account = state.accounts.get(str(xref["acct_id"]))
    if customer is None or account is None:
        raise OracleAbend("CBSTM03A: ERROR READING CUSTFILE OR ACCTFILE, RETURN CODE: 23")
    return customer, account


def BuildStatement(
    state: OracleState, xref: Mapping[str, object], records: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Transcribes one pass of 1000-MAINLINE, cbl/CBSTM03A.CBL lines 316-329, for one xref.

    5000-CREATE-STATEMENT with 5100 and 5200 writes the heads, then 4000-TRNXFILE-GET (416-456)
    writes each transaction of the card through 6000-WRITE-TRANS, adds TRNX-AMT to WS-TOTAL-AMT
    S9(9)V99 COMP-3 (line 429), and writes the tails.
    """
    texts = BuildStatementTexts(*StatementMasterRecords(state, xref))
    psLines = RenderLines(PS_STATEMENT_HEAD, PsHeadSlots(texts), STATEMENT_PS_WIDTH)
    htmlLines = RenderLines(HTML_STATEMENT_HEAD, HtmlHeadSlots(texts), STATEMENT_HTML_WIDTH)
    totalAmount = ZERO_AMOUNT
    for record in records:
        slots = TransactionSlots(record)
        psLines += RenderLines(PS_TRANSACTION_LINES, slots, STATEMENT_PS_WIDTH)
        htmlLines += RenderLines(HTML_TRANSACTION_LINES, slots, STATEMENT_HTML_WIDTH)
        totalAmount = StoreSum((totalAmount, RecordAmount(record)), 9, 2)
    totalSlots = {SLOT_PS_TOTAL: PS_TOTAL_LABEL + EditAmountZ(totalAmount)}
    psLines += RenderLines(PS_STATEMENT_TAIL, totalSlots, STATEMENT_PS_WIDTH)
    htmlLines += RenderLines(HTML_STATEMENT_TAIL, {}, STATEMENT_HTML_WIDTH)
    return psLines, htmlLines


def StatementSortKey(record: str) -> tuple[bytes, bytes]:
    """Return the CREASTMT STEP010 key: SORT FIELDS=(263,16,CH,A,1,16,CH,A), card then TRAN-ID."""
    return EbcdicSortKey(record[TRAN_CARD_SLICE]), EbcdicSortKey(record[TRAN_ID_SLICE])


def RequireStatementCapacity(cardTable: Mapping[str, Sequence[str]]) -> None:
    """Refuse input beyond WS-TRNX-TABLE (cbl/CBSTM03A.CBL lines 225-233; AAP 0.6.12).

    WS-CARD-TBL OCCURS 51 and WS-TRAN-TBL OCCURS 10 have no bounds check, so larger input
    overlays storage. The port's defined outcome is exit 16 before any statement is written.
    """
    if len(cardTable) > STATEMENT_MAX_CARDS:
        raise OracleAbend(f"CBSTM03A: more than {STATEMENT_MAX_CARDS} cards in TRNXFILE")
    for records in cardTable.values():
        if len(records) > STATEMENT_MAX_TRANSACTIONS:
            raise OracleAbend(
                f"CBSTM03A: more than {STATEMENT_MAX_TRANSACTIONS} transactions for one card"
            )


def LoadStatementTable(transactions: Mapping[str, str]) -> dict[str, list[str]]:
    """Replay CREASTMT STEP010-STEP020 and the CBSTM03A table load into WS-TRNX-TABLE.

    jcl/CREASTMT.JCL STEP010 (lines 44-54) sorts TRANSACT by card and TRAN-ID with the OUTREC
    re-key into the TRXFL KSDS; 8100-TRNXFILE-OPEN (cbl/CBSTM03A.CBL 730-762) and
    8500-READTRNX-READ (818-853) group the records by card. An empty TRNXFILE fails the first
    read (status 10), which performs 9999-ABEND-PROGRAM.
    """
    ordered = sorted(transactions.values(), key=StatementSortKey)
    if not ordered:
        raise OracleAbend("CBSTM03A: ERROR READING TRNXFILE, RETURN CODE: 10")
    cardTable: dict[str, list[str]] = {}
    for record in ordered:
        cardTable.setdefault(record[TRAN_CARD_SLICE], []).append(record)
    RequireStatementCapacity(cardTable)
    return cardTable


def BuildAllStatements(
    state: OracleState, cardTable: Mapping[str, Sequence[str]]
) -> tuple[list[str], list[str]]:
    """Transcribes the 1000-MAINLINE loop over XREFFILE, cbl/CBSTM03A.CBL lines 316-329.

    1000-XREFFILE-GET-NEXT (345-366) reads CARDXREF sequentially in card order; every xref gets a
    statement, with the transactions whose card equals XREF-CARD-NUM (the 4000 table scan).
    """
    psLines: list[str] = []
    htmlLines: list[str] = []
    for xref in StateRows(state, CARD_XREF_TABLE):
        cardKey = PaddedText(xref["card_num"], CARD_NUM_LENGTH)
        statementPs, statementHtml = BuildStatement(state, xref, cardTable.get(cardKey, ()))
        psLines.extend(statementPs)
        htmlLines.extend(statementHtml)
    return psLines, htmlLines


def RunCreastmt(state: OracleState) -> StatementOutcome:
    """Transcribes CREASTMT (jcl/CREASTMT.JCL STEP010-STEP040) and CBSTM03A with CBSTM03B.

    The TIOT walk and the ALTER / GO TO dispatch of cbl/CBSTM03A.CBL (lines 262-314) only open the
    files and have no output. Any abend, including the capacity refusal, gives exit 16 and no
    lines.
    """
    try:
        cardTable = LoadStatementTable(state.transactions)
        psLines, htmlLines = BuildAllStatements(state, cardTable)
    except OracleAbend as abend:
        return StatementOutcome([], [], abend.exitCode)
    return StatementOutcome(psLines, htmlLines, EXIT_OK)


# CVTRA07Y report lines (133-character TRANREPT records) and CBTRN03C working storage.
REPORT_PAGE_SIZE = 20
LINE_COUNTER_MODULUS = 10**9
REPORT_NAME_PREFIX = "DALYREPT".ljust(38) + "Daily Transaction Report".ljust(41) + "Date Range: "
REPORT_HEADER_1 = (
    "Transaction ID".ljust(17)
    + "Account ID".ljust(12)
    + "Transaction Type".ljust(19)
    + "Tran Category".ljust(35)
    + "Tran Source".ljust(14)
    + " "
    + "        Amount".ljust(16)
)
REPORT_HEADER_2 = "-" * REPORT_LINE_WIDTH
REPORT_BLANK_LINE = ""
PAGE_TOTAL_LABEL = "Page Total".ljust(11) + "." * 86
ACCOUNT_TOTAL_LABEL = "Account Total".ljust(13) + "." * 84
GRAND_TOTAL_LABEL = "Grand Total".ljust(11) + "." * 86
TYPE_DESCRIPTION_WIDTH = 15
CATEGORY_DESCRIPTION_WIDTH = 29
# WS-CURR-CARD-NUM PIC X(16) VALUE SPACES (cbl/CBTRN03C.cbl line 137).
REPORT_INITIAL_CARD_NUM = " " * CARD_NUM_LENGTH


@dataclass
class ReportWorkArea:
    """CBTRN03C WS-REPORT-VARS and the lookup records while it runs; private to BuildReportLines.

    currCard starts as REPORT_INITIAL_CARD_NUM, the 16 spaces of WS-CURR-CARD-NUM (line 137).
    xrefAcctId holds XREF-ACCT-ID of CARD-XREF-RECORD, the WORKING-STORAGE COPY CVACT03Y at line
    98, which only READ XREF-FILE INTO CARD-XREF-RECORD fills (line 485). CARD-XREF-RECORD has no
    VALUE clause, so the language leaves its content before the first read unpredictable. A
    leading all-space card reads no xref, so its detail line takes this oracle's defined outcome,
    "", which TRAN-REPORT-ACCOUNT-ID X(11) prints as 11 spaces (line 364).
    """

    reportInput: ReportInput
    lines: list[str] = field(default_factory=list)
    lineCounter: int = 0
    pageTotal: Decimal = ZERO_AMOUNT
    accountTotal: Decimal = ZERO_AMOUNT
    grandTotal: Decimal = ZERO_AMOUNT
    firstTime: bool = True
    currCard: str = REPORT_INITIAL_CARD_NUM
    xrefAcctId: str = ""


def WriteReportLine(work: ReportWorkArea, text: str) -> None:
    """Transcribes 1111-WRITE-REPORT-REC (cbl/CBTRN03C.cbl 343-359) and ADD 1 TO WS-LINE-COUNTER."""
    work.lines.append(text.ljust(REPORT_LINE_WIDTH))
    work.lineCounter = (work.lineCounter + 1) % LINE_COUNTER_MODULUS


def WriteReportHeaders(work: ReportWorkArea) -> None:
    """Transcribes 1120-WRITE-HEADERS, cbl/CBTRN03C.cbl lines 324-341: four counted lines."""
    reportInput = work.reportInput
    dateRange = PaddedText(reportInput.startDate, DATE_LENGTH) + " to "
    WriteReportLine(work, REPORT_NAME_PREFIX + dateRange + PaddedText(reportInput.endDate, 10))
    WriteReportLine(work, REPORT_BLANK_LINE)
    WriteReportLine(work, REPORT_HEADER_1)
    WriteReportLine(work, REPORT_HEADER_2)


def WritePageTotals(work: ReportWorkArea) -> None:
    """Transcribes 1110-WRITE-PAGE-TOTALS, cbl/CBTRN03C.cbl lines 293-304.

    The page total is written and added to WS-GRAND-TOTAL, then reset, and HEADER-2 follows.
    """
    WriteReportLine(work, PAGE_TOTAL_LABEL + EditReportAmount(work.pageTotal, "+"))
    work.grandTotal = StoreSum((work.grandTotal, work.pageTotal), 9, 2)
    work.pageTotal = ZERO_AMOUNT
    WriteReportLine(work, REPORT_HEADER_2)


def WriteAccountTotals(work: ReportWorkArea) -> None:
    """Transcribes 1120-WRITE-ACCOUNT-TOTALS, cbl/CBTRN03C.cbl lines 306-316."""
    WriteReportLine(work, ACCOUNT_TOTAL_LABEL + EditReportAmount(work.accountTotal, "+"))
    work.accountTotal = ZERO_AMOUNT
    WriteReportLine(work, REPORT_HEADER_2)


def WriteGrandTotals(work: ReportWorkArea) -> None:
    """Transcribes 1110-WRITE-GRAND-TOTALS, cbl/CBTRN03C.cbl lines 318-322 (not counted)."""
    grandLine = GRAND_TOTAL_LABEL + EditReportAmount(work.grandTotal, "+")
    work.lines.append(grandLine.ljust(REPORT_LINE_WIDTH))


def LookupReportXref(work: ReportWorkArea, card: str) -> str:
    """Transcribes 1500-A-LOOKUP-XREF, cbl/CBTRN03C.cbl lines 484-492: XREF-ACCT-ID of a card.

    Raises:
        OracleAbend: INVALID KEY, which performs 9999-ABEND-PROGRAM.
    """
    xref = work.reportInput.xrefs.get(card.rstrip(" "))
    if xref is None:
        raise OracleAbend("CBTRN03C: INVALID CARD NUMBER IN CARDXREF")
    return str(xref["acct_id"])


def LookupReportDescriptions(work: ReportWorkArea, record: str) -> tuple[str, str]:
    """Transcribes 1500-B-LOOKUP-TRANTYPE and 1500-C-LOOKUP-TRANCATG, lines 494-512.

    Raises:
        OracleAbend: INVALID KEY on either file, which performs 9999-ABEND-PROGRAM.
    """
    RequireDigits(record[TRAN_CAT_SLICE], "tran_cat_cd")
    typeCode = record[TRAN_TYPE_SLICE].rstrip(" ")
    tranType = work.reportInput.transactionTypes.get(typeCode)
    category = work.reportInput.transactionCategories.get((typeCode, int(record[TRAN_CAT_SLICE])))
    if tranType is None or category is None:
        raise OracleAbend("CBTRN03C: INVALID TRANSACTION TYPE OR TRAN CATG KEY")
    return str(tranType["description"]), str(category["description"])


def ReportDetailLine(record: str, xrefAcctId: str, descriptions: tuple[str, str]) -> str:
    """Transcribes 1120-WRITE-DETAIL, cbl/CBTRN03C.cbl lines 361-374 (TRANSACTION-DETAIL-REPORT)."""
    typeDescription, categoryDescription = descriptions
    return (
        f"{record[TRAN_ID_SLICE]} {PaddedText(xrefAcctId, ACCT_ID_LENGTH)} "
        f"{record[TRAN_TYPE_SLICE]}-{PaddedText(typeDescription, TYPE_DESCRIPTION_WIDTH)} "
        f"{record[TRAN_CAT_SLICE]}-{PaddedText(categoryDescription, CATEGORY_DESCRIPTION_WIDTH)} "
        f"{record[TRAN_SOURCE_SLICE]}    {EditReportAmount(RecordAmount(record), '-')}  "
    )


def WriteTransactionReport(
    work: ReportWorkArea, record: str, descriptions: tuple[str, str]
) -> None:
    """Transcribes 1100-WRITE-TRANSACTION-REPORT, cbl/CBTRN03C.cbl lines 274-290.

    Headers come first on the first record; whenever MOD(WS-LINE-COUNTER, 20) = 0 the page total
    and headers are written; then the amount is added to the page and account totals and the
    detail line is written.
    """
    if work.firstTime:
        work.firstTime = False
        WriteReportHeaders(work)
    if work.lineCounter % REPORT_PAGE_SIZE == 0:
        WritePageTotals(work)
        WriteReportHeaders(work)
    amount = RecordAmount(record)
    work.pageTotal = StoreSum((work.pageTotal, amount), 9, 2)
    work.accountTotal = StoreSum((work.accountTotal, amount), 9, 2)
    WriteReportLine(work, ReportDetailLine(record, work.xrefAcctId, descriptions))


def ReportOneRecord(work: ReportWorkArea, record: str) -> None:
    """Transcribes the in-file branch of the CBTRN03C mainline, cbl/CBTRN03C.cbl lines 179-196.

    Lines 181-188: only when the X(16) TRAN-CARD-NUM differs from WS-CURR-CARD-NUM is the
    previous account total written (after the first record) and the new card's xref read, so a
    card equal to the initial spaces reads no xref. The type and category are read for every
    record.
    """
    card = record[TRAN_CARD_SLICE]
    if card != work.currCard:
        if not work.firstTime:
            WriteAccountTotals(work)
        work.currCard = card
        work.xrefAcctId = LookupReportXref(work, card)
    descriptions = LookupReportDescriptions(work, record)
    WriteTransactionReport(work, record, descriptions)


def FinishReport(work: ReportWorkArea, lastRecord: str) -> None:
    """Transcribes the end-of-file ELSE branch of the mainline, cbl/CBTRN03C.cbl lines 197-203.

    TRAN-RECORD still holds the last record, so its amount is added to the page and account totals
    a second time; the page total and the grand total follow. The final account total is never
    written (AAP 0.6.12).
    """
    amount = RecordAmount(lastRecord)
    work.pageTotal = StoreSum((work.pageTotal, amount), 9, 2)
    work.accountTotal = StoreSum((work.accountTotal, amount), 9, 2)
    WritePageTotals(work)
    WriteGrandTotals(work)


def SelectReportRecords(reportInput: ReportInput) -> list[str]:
    """Replay proc/TRANREPT.prc STEP05R (lines 35-46): INCLUDE then SORT by card number.

    INCLUDE keeps records whose TRAN-PROC-DT, positions 305-314, lies between the dates by CH
    comparison, so a blank PROC-TS fails. The sort is by TRAN-CARD-NUM, with TRAN-ID fixing the
    order of equal cards (the TRANSACT key order the sort input arrives in).
    """
    startKey = EbcdicSortKey(PaddedText(reportInput.startDate, DATE_LENGTH))
    endKey = EbcdicSortKey(PaddedText(reportInput.endDate, DATE_LENGTH))
    selected = [
        record
        for record in reportInput.transactions
        if startKey <= EbcdicSortKey(record[PROC_DATE_SLICE]) <= endKey
    ]
    return sorted(selected, key=StatementSortKey)


def BuildReportLines(reportInput: ReportInput) -> list[str]:
    """Transcribes CBTRN03C (proc/TRANREPT.prc STEP10R), cbl/CBTRN03C.cbl lines 159-217.

    Returns the 133-character TRANREPT lines, or [] when no record is in the date range (the
    empty file leaves the loop through NEXT SENTENCE at line 177 without writing anything).

    Raises:
        OracleAbend: A card, type or category lookup is INVALID KEY.
    """
    records = SelectReportRecords(reportInput)
    if not records:
        return []
    work = ReportWorkArea(reportInput)
    for record in records:
        ReportOneRecord(work, record)
    FinishReport(work, records[-1])
    return work.lines


@dataclass(frozen=True)
class ProcessingSteps:
    """The outputs of chain steps 6 and 13-16, between the loaders and CREASTMT."""

    bkupRun1: TranbkpOutcome
    posttran: PosttranOutcome
    intcalc: IntcalcOutcome
    bkupRun2: TranbkpOutcome
    combinedRecords: list[str]


def RequireChainContinues(exitCode: int, jobName: str) -> None:
    """Stop the chain as COND=(4,LT) does: a step status of 8 or more raises OracleAbend."""
    if exitCode >= CHAIN_STOP_EXIT:
        raise OracleAbend(f"{jobName} ended with status {exitCode}; the chain stops", exitCode)


def RunProcessingSteps(
    loadedState: OracleState, dailyRecords: Sequence[str], clockText: str
) -> ProcessingSteps:
    """Run chain steps 6 (TRANBKP), 13 (POSTTRAN), 14 (INTCALC), 15 (TRANBKP), 16 (COMBTRAN)."""
    bkupRun1 = RunTranbkp(loadedState)
    posttran = RunPosttran(bkupRun1.state, dailyRecords, clockText)
    RequireChainContinues(posttran.exitCode, "POSTTRAN")
    intcalc = RunIntcalc(posttran.state, IntcalcInput(PARM_DATE, clockText))
    RequireChainContinues(intcalc.exitCode, "INTCALC")
    bkupRun2 = RunTranbkp(intcalc.state)
    combinedRecords = RunCombtran(bkupRun2.bkupRecords, intcalc.systranRecords)
    return ProcessingSteps(bkupRun1, posttran, intcalc, bkupRun2, combinedRecords)


def SampleReportInput(finalState: OracleState) -> ReportInput:
    """Return the TRANREPT input over the final TRANSACT for the 2022-07-18 sample range."""
    return ReportInput(
        finalState.transactions.values(),
        finalState.xrefs,
        finalState.transactionTypes,
        finalState.transactionCategories,
        REPORT_START_DATE,
        REPORT_END_DATE,
    )


def RunReportingSteps(
    steps: ProcessingSteps,
) -> tuple[OracleState, StatementOutcome, list[str]]:
    """Run CREASTMT (step 17) and the sample TRANREPT over the TRANSACT COMBTRAN reloaded.

    COMBTRAN STEP10's reload (ReloadCombinedTransactions) runs first, over the state the second
    TRANBKP left.

    Raises:
        OracleAbend: COMBTRAN's reload meets a duplicate TRAN-ID (exitCode 12), so the chain
            stops before CREASTMT; or CREASTMT ends at 8 or more.
    """
    finalState = ReloadCombinedTransactions(steps.bkupRun2.state, steps.combinedRecords)
    statements = RunCreastmt(finalState)
    RequireChainContinues(statements.exitCode, "CREASTMT")
    reportLines = BuildReportLines(SampleReportInput(finalState))
    return finalState, statements, reportLines


def RunSampleChain(ebcdicDir: Path, clockText: str) -> ChainOutcome:
    """Run the AAP 0.6.8 chain over the sample images and the sample report.

    Steps 1-5 and 7-11 load the images, step 12 stages DALYTRAN, then steps 6 and 13-16, then
    CREASTMT (17) over the combined TRANSACT. TRANIDX (18) only rebuilds an index. The report is
    the 2022-07-18 TRANREPT over the final TRANSACT. The status is the highest step status, 4 on
    the sample because POSTTRAN rejects records.

    Raises:
        OracleAbend: A step ends at 8 or more, where the chain would stop; exitCode 12 when
            COMBTRAN's reload meets a duplicate TRAN-ID.
    """
    loadedState = LoadSampleState(ebcdicDir)
    dailyRecords = ReadDailyRecords(ImagePath(ebcdicDir, DALYTRAN_IMAGE))
    steps = RunProcessingSteps(loadedState, dailyRecords, clockText)
    finalState, statements, reportLines = RunReportingSteps(steps)
    exitCode = max(steps.posttran.exitCode, steps.intcalc.exitCode, statements.exitCode)
    return ChainOutcome(
        loadedState,
        dailyRecords,
        steps.bkupRun1,
        steps.posttran,
        steps.intcalc,
        steps.bkupRun2,
        steps.combinedRecords,
        finalState,
        statements,
        reportLines,
        exitCode,
    )
