"""Replaces: COBOL intrinsic functions TEST-NUMVAL-C, NUMVAL-C, TEST-NUMVAL and NUMVAL as used
by COACTUPC, COTRN02C, CORPT00C and copybook CSUTLDPY.

Screen fields reach the services as raw text, and the services apply these functions to that
text in the order the programs do. Values are exact Decimals with the fraction digits as keyed;
callers store them into their receivers with decimal_utils.StoreInto, which truncates.
"""

import re
from decimal import Decimal

# The COBOL_CONTEXT precision: an argument with more digits does not conform.
MAX_DIGITS = 31
NEGATIVE_SIGNS = frozenset({"-", "CR", "DB"})

# Fragments shared by both grammars. A space is only " ": TAB, LOW-VALUES and any other
# character are invalid. [0-9] is used instead of \d, which also matches non-ASCII digits that
# Decimal() accepts and a 3270 field cannot carry. The conditional admits a trailing sign only
# when no leading sign was keyed, so at most one sign is present.
_LEADING_SIGN_GRAMMAR = r"[ ]*(?:(?P<leadingSign>[+-])[ ]*)?"
_TRAILING_SIGN_GRAMMAR = r"[ ]*(?(leadingSign)|(?P<trailingSign>[+-]|(?i:CR|DB))?)[ ]*"

# NUMVAL-C: [sp][+|-][sp][$][sp]{int[.[frac]] | .frac}[sp][+|-|CR|DB][sp], where commas may
# group the integer part and never follow the decimal point.
NUMVAL_C_PATTERN = re.compile(
    _LEADING_SIGN_GRAMMAR
    + r"(?:\$[ ]*)?(?P<number>[0-9][0-9,]*(?:\.[0-9]*)?|\.[0-9]+)"
    + _TRAILING_SIGN_GRAMMAR
)

# NUMVAL: the NUMVAL-C grammar without the currency sign and the commas.
NUMVAL_PATTERN = re.compile(
    _LEADING_SIGN_GRAMMAR + r"(?P<number>[0-9]+(?:\.[0-9]*)?|\.[0-9]+)" + _TRAILING_SIGN_GRAMMAR
)


def TestNumvalC(text: str) -> bool:
    """Return True when text conforms to NUMVAL-C, as TEST-NUMVAL-C(text) = 0 tests.

    Signs are a leading "+" or "-", or a trailing "+", "-", "CR" or "DB"; "$" may follow a
    leading sign. Empty, all-space and LOW-VALUES text does not conform.
    """
    return _MatchGrammar(NUMVAL_C_PATTERN, text) is not None


def NumvalC(text: str) -> Decimal:
    """Return the exact value of NUMVAL-C text, ignoring spaces, "$" and grouping commas.

    Text that does not conform gives Decimal(0). CORPT00C applies NUMVAL-C to its date parts
    without testing them first, and this fixed result leaves the decision to its own checks.
    """
    return _ParseNumber(_MatchGrammar(NUMVAL_C_PATTERN, text))


def TestNumval(text: str) -> bool:
    """Return True when text conforms to NUMVAL, as TEST-NUMVAL(text) = 0 tests.

    The grammar is that of NUMVAL-C without the "$" currency sign and the grouping commas.
    """
    return _MatchGrammar(NUMVAL_PATTERN, text) is not None


def Numval(text: str) -> Decimal:
    """Return the exact value of NUMVAL text, or Decimal(0) when the text does not conform."""
    return _ParseNumber(_MatchGrammar(NUMVAL_PATTERN, text))


def _MatchGrammar(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    """Return the match of pattern against the whole of text, or None when it does not conform."""
    if not isinstance(text, str):
        raise TypeError(f"COBOL intrinsic argument must be str, not {type(text).__name__}")
    match = pattern.fullmatch(text)
    if match is None:
        return None
    digitCount = sum(character.isdigit() for character in match["number"])
    if digitCount > MAX_DIGITS:
        return None
    return match


def _ParseNumber(match: re.Match[str] | None) -> Decimal:
    """Return the signed value of a grammar match, or Decimal(0) when there is no match."""
    if match is None:
        return Decimal(0)
    value = Decimal(match["number"].replace(",", ""))
    signText = (match["leadingSign"] or match["trailingSign"] or "+").upper()
    if signText in NEGATIVE_SIGNS and not value.is_zero():
        # copy_negate() is exact, where unary minus rounds to the 28-digit default context.
        return value.copy_negate()
    return value
