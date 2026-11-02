"""Replaces: COBOL numeric-edited MOVE semantics for the pictures of copybook CVTRA07Y (CBTRN03C
report), CBSTM03A statement lines and the COTRN0xC/COBIL00C/COACTVWC/COACTUPC display fields.

FormatEdited renders a Decimal or int as IBM Enterprise COBOL does when the value is moved,
without ROUNDED, into a numeric-edited receiver built from 9, Z, the comma, the decimal point and
one fixed + or - sign. The picture constants are the receivers the ported programs declare, and
every service and batch job formats through this one interpreter.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

from app.utils.decimal_utils import StoreInto

# CVTRA07Y TRAN-REPORT-AMT: the CBTRN03C report detail amount.
REPORT_DETAIL_AMOUNT_PICTURE = "-ZZZ,ZZZ,ZZZ.ZZ"
# CVTRA07Y REPT-PAGE-TOTAL, REPT-ACCOUNT-TOTAL and REPT-GRAND-TOTAL.
REPORT_TOTAL_AMOUNT_PICTURE = "+ZZZ,ZZZ,ZZZ.ZZ"
# CBSTM03A ST-TRANAMT and ST-TOTAL-TRAMT.
STATEMENT_AMOUNT_PICTURE = "Z(9).99-"
# CBSTM03A ST-CURR-BAL. It receives ACCT-CURR-BAL S9(10)V99, so the top integer digit is lost.
STATEMENT_BALANCE_PICTURE = "9(9).99-"
# WS-TRAN-AMT and WS-TRAN-AMT-E of COTRN00C, COTRN01C, COTRN02C and COBIL00C.
ONLINE_AMOUNT_PICTURE = "+99999999.99"
# COBIL00C WS-CURR-BAL.
ONLINE_BALANCE_PICTURE = "+9999999999.99"
# The COACTVW PICOUT amounts and the COACTUPC display amounts.
ACCOUNT_AMOUNT_PICTURE = "+ZZZ,ZZZ,ZZZ.99"

LEADING_SIGN = "leading"
TRAILING_SIGN = "trailing"
DIGIT_SYMBOLS = frozenset("9Z")
SIGN_SYMBOLS = frozenset("+-")
SUPPORTED_SYMBOLS = frozenset("9Z,.+-")
# Two-character symbols are named whole in errors, not by their first character.
UNSUPPORTED_SYMBOL_PAIRS = frozenset({"CR", "DB"})
REPETITION_PATTERN = re.compile(r"(?P<symbol>[^()])\((?P<count>[0-9]+)\)")


class PictureError(ValueError):
    """A numeric-edited picture that is malformed or uses a symbol this module does not support."""


@dataclass(frozen=True)
class EditedPicture:
    """A parsed numeric-edited picture: its expanded symbols, fixed sign and digit layout."""

    symbols: str
    signPosition: str | None
    signSymbol: str | None
    intDigits: int
    decDigits: int
    allDigitsSuppressed: bool


def FormatEdited(value: Decimal | int, picture: str) -> str:
    """Return value as a MOVE without ROUNDED leaves it in the numeric-edited picture.

    Supported symbols: 9 (a digit); Z (a digit, or a space while leading zeros are suppressed);
    the comma (a space while suppressing); one decimal point (always shown, and it ends
    suppression); one fixed + or - as the first or last symbol. X(n) repeats a symbol n times.
    The value is first stored into the picture's digit positions with StoreInto, so surplus
    fraction digits truncate and high-order integer digits are dropped. A + position shows + or
    -, a - position shows a space or -, and a zero result is positive. When every digit position
    is Z and the stored value is zero, the whole item is spaces, sign, commas and point included.
    Raises PictureError for any other picture, and TypeError for a float or other non-numeric value.
    """
    pictureSpec = _ParsePicture(picture)
    stored = StoreInto(value, pictureSpec.intDigits, pictureSpec.decDigits, signed=True)
    if pictureSpec.allDigitsSuppressed and stored.is_zero():
        return " " * len(pictureSpec.symbols)
    body = _RenderBody(pictureSpec, _DigitStream(stored, pictureSpec))
    signCharacter = _SignCharacter(pictureSpec, stored < 0)
    leadingSign = signCharacter if pictureSpec.signPosition == LEADING_SIGN else ""
    trailingSign = signCharacter if pictureSpec.signPosition == TRAILING_SIGN else ""
    result = leadingSign + body + trailingSign
    assert len(result) == len(pictureSpec.symbols), (picture, result)
    return result


@lru_cache(maxsize=128)
def _ParsePicture(picture: str) -> EditedPicture:
    """Expand and check picture, then return its sign and digit layout."""
    if not isinstance(picture, str):
        raise TypeError(f"a picture must be str, not {type(picture).__name__}")
    symbols = _ExpandRepetitions(picture)
    _RequireSupportedSymbols(symbols, picture)
    signPosition, signSymbol = _LocateSign(symbols, picture)
    integerPart, _, fractionPart = symbols.partition(".")
    digitSymbols = [symbol for symbol in symbols if symbol in DIGIT_SYMBOLS]
    return EditedPicture(
        symbols=symbols,
        signPosition=signPosition,
        signSymbol=signSymbol,
        intDigits=_CountDigitPositions(integerPart),
        decDigits=_CountDigitPositions(fractionPart),
        allDigitsSuppressed=all(symbol == "Z" for symbol in digitSymbols),
    )


def _ExpandRepetitions(picture: str) -> str:
    """Replace each X(n) with n copies of X, refusing n = 0 and any stray parenthesis."""
    expanded = REPETITION_PATTERN.sub(_RepeatSymbol, picture)
    for parenthesis in "()":
        if parenthesis in expanded:
            raise PictureError(f"malformed repetition {parenthesis!r} in picture {picture!r}")
    return expanded


def _RepeatSymbol(repetition: re.Match[str]) -> str:
    """Return the symbol of one X(n) repetition n times."""
    count = int(repetition["count"])
    if count == 0:
        raise PictureError(f"zero repetition count in picture {repetition.string!r}")
    return repetition["symbol"] * count


def _RequireSupportedSymbols(symbols: str, picture: str) -> None:
    """Refuse a symbol other than 9, Z, comma, point and sign, a second point, and no digits."""
    for index, symbol in enumerate(symbols):
        if symbol not in SUPPORTED_SYMBOLS:
            symbolPair = symbols[index : index + 2]
            namedSymbol = symbolPair if symbolPair in UNSUPPORTED_SYMBOL_PAIRS else symbol
            raise PictureError(f"unsupported symbol {namedSymbol!r} in picture {picture!r}")
    if symbols.count(".") > 1:
        raise PictureError(f"more than one decimal point '.' in picture {picture!r}")
    if not any(symbol in DIGIT_SYMBOLS for symbol in symbols):
        raise PictureError(f"picture {picture!r} has no 9 or Z digit position")


def _LocateSign(symbols: str, picture: str) -> tuple[str | None, str | None]:
    """Return (signPosition, signSymbol) of the one fixed + or - at either end, if any."""
    signIndexes = [index for index, symbol in enumerate(symbols) if symbol in SIGN_SYMBOLS]
    if not signIndexes:
        return None, None
    if len(signIndexes) > 1:
        secondSign = symbols[signIndexes[1]]
        raise PictureError(
            f"second sign {secondSign!r} in picture {picture!r}: floating insertion is not"
            " supported"
        )
    signIndex = signIndexes[0]
    if signIndex == 0:
        return LEADING_SIGN, symbols[signIndex]
    if signIndex == len(symbols) - 1:
        return TRAILING_SIGN, symbols[signIndex]
    raise PictureError(f"sign {symbols[signIndex]!r} in the middle of picture {picture!r}")


def _CountDigitPositions(pictureSegment: str) -> int:
    """Return the number of 9 and Z positions in a segment of an expanded picture."""
    return sum(1 for symbol in pictureSegment if symbol in DIGIT_SYMBOLS)


def _DigitStream(stored: Decimal, pictureSpec: EditedPicture) -> str:
    """Return abs(stored) as intDigits + decDigits zero-padded digits with no decimal point."""
    digitText = "".join(str(digit) for digit in stored.as_tuple().digits)
    return digitText.zfill(pictureSpec.intDigits + pictureSpec.decDigits)


def _RenderBody(pictureSpec: EditedPicture, digits: str) -> str:
    """Render every symbol but the sign, consuming one digit for each 9 or Z position."""
    digitIterator = iter(digits)
    suppressing = True
    rendered = []
    for symbol in pictureSpec.symbols:
        if symbol in DIGIT_SYMBOLS:
            digit = next(digitIterator)
            suppressing = suppressing and symbol == "Z" and digit == "0"
            rendered.append(" " if suppressing else digit)
        elif symbol == ".":
            suppressing = False
            rendered.append(symbol)
        elif symbol == ",":
            rendered.append(" " if suppressing else symbol)
    return "".join(rendered)


def _SignCharacter(pictureSpec: EditedPicture, isNegative: bool) -> str:
    """Return the fixed sign's character: + shows + or -, - shows a space or -, none shows ""."""
    if pictureSpec.signSymbol is None:
        return ""
    if isNegative:
        return "-"
    if pictureSpec.signSymbol == "+":
        return "+"
    return " "
