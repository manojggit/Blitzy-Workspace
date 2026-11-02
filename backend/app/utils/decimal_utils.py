"""Replaces: COBOL DISPLAY/zoned-decimal storage semantics (MOVE and arithmetic without ROUNDED or
ON SIZE ERROR) used by CBACT04C, CBTRN02C, CBTRN03C, CBSTM03A, COBIL00C, COTRN02C, COACTUPC;
seed-image overpunch fields of CVACT01Y, CVTRA01Y, CVTRA02Y, CVTRA05Y, CVTRA06Y.

StoreInto stores a value into a numeric receiver as IBM Enterprise COBOL does when a statement
has neither ROUNDED nor ON SIZE ERROR: surplus fraction digits are truncated toward zero, then
integer digits that do not fit the receiver are dropped, keeping the sign.

DecodeZoned and EncodeZoned convert zoned DISPLAY text with a trailing sign overpunch. EBCDIC
images are decoded with code page 037 first, which turns zone X'C' into "{" and "A"-"I" and zone
X'D' into "}" and "J"-"R", so one codec serves the EBCDIC and the ASCII seed images alike.
"""

from decimal import (
    ROUND_DOWN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)

# Trailing sign overpunch characters, indexed by the digit value they carry.
POSITIVE_OVERPUNCH = "{ABCDEFGHI"
NEGATIVE_OVERPUNCH = "}JKLMNOPQR"
# The only digits a zoned field holds; str.isdigit() would also accept non-ASCII digits.
UNSIGNED_DIGITS = "0123456789"

# 31 digits cover the 30-digit ARITH(COMPAT) intermediates, and ROUND_DOWN truncates anything
# longer as COBOL intermediates do. Callers compute under
# `with decimal.localcontext(COBOL_CONTEXT): ...` and store each result with StoreInto.
# localcontext works on a copy, so this shared object is never mutated by a computation. The
# default traps are listed explicitly so a changed decimal.DefaultContext cannot disable them.
COBOL_CONTEXT = Context(
    prec=31,
    rounding=ROUND_DOWN,
    traps=[InvalidOperation, DivisionByZero, Overflow],
)


class ZonedDecimalError(ValueError):
    """Malformed zoned text, an impossible picture width, or a value no picture can hold."""


def StoreInto(value: Decimal | int, intDigits: int, decDigits: int, signed: bool = True) -> Decimal:
    """Store value into a PIC [S]9(intDigits)V9(decDigits) receiver, as MOVE and COMPUTE do.

    Surplus fraction digits truncate toward zero (no ROUNDED) and integer digits beyond intDigits
    are dropped, keeping the sign (no ON SIZE ERROR). An unsigned receiver keeps the absolute
    value. The result always has exponent -decDigits, and a zero result is never negative.
    A value whose integer part needs more than the 31 digits of a COBOL intermediate raises
    decimal.InvalidOperation.
    """
    number = _RequireDecimal(value)
    _RequirePicture(intDigits, decDigits)
    # A 31-digit intermediate keeps every digit while gaining the receiver's decimal places.
    # Nothing is rounded: quantize truncates explicitly and the modulo is exact.
    with localcontext(COBOL_CONTEXT, prec=COBOL_CONTEXT.prec + decDigits):
        truncated = number.quantize(Decimal(10) ** -decDigits, rounding=ROUND_DOWN)
        magnitude = _TruncateHighOrder(truncated.copy_abs(), intDigits)
    if signed and truncated.is_signed() and not magnitude.is_zero():
        return magnitude.copy_negate()
    return magnitude


def DecodeZoned(text: str, scale: int) -> Decimal:
    """Decode zoned DISPLAY text with a trailing sign overpunch and scale implied decimals.

    Every character but the last is a digit 0-9. The last is a digit (positive) or an overpunch:
    "{" and "A"-"I" are +0 to +9, "}" and "J"-"R" are -0 to -9. Negative zero decodes as zero.
    """
    _RequireZonedShape(text, scale)
    leadingDigits = _DecodeLeadingDigits(text)
    signBit, lastDigit = _DecodeSignDigit(text)
    digitTuple = leadingDigits + (lastDigit,)
    if not any(digitTuple):
        signBit = 0
    return Decimal((signBit, digitTuple, -scale))


def EncodeZoned(value: Decimal | int, intDigits: int, decDigits: int, signed: bool = True) -> str:
    """Encode value as PIC [S]9(intDigits)V9(decDigits) zoned DISPLAY text, after a COBOL MOVE.

    The digits are zero-padded to intDigits + decDigits with no decimal point. A signed picture
    overpunches the last digit: "{" and "A"-"I" for zero or positive, "}" and "J"-"R" for negative.
    """
    stored = StoreInto(value, intDigits, decDigits, signed)
    digitText = "".join(str(digit) for digit in stored.as_tuple().digits)
    digitText = digitText.zfill(intDigits + decDigits)
    if not signed:
        return digitText
    overpunch = NEGATIVE_OVERPUNCH if stored.is_signed() else POSITIVE_OVERPUNCH
    return digitText[:-1] + overpunch[UNSIGNED_DIGITS.index(digitText[-1])]


def _RequireDecimal(value: Decimal | int) -> Decimal:
    """Return value as an exact Decimal; any type but Decimal or int, bool included, is refused."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        typeName = type(value).__name__
        raise TypeError(f"COBOL numeric value must be Decimal or int, never float: got {typeName}")
    number = Decimal(value)
    if not number.is_finite():
        raise ZonedDecimalError(f"cannot store the non-finite value {number} in a numeric picture")
    return number


def _RequirePicture(intDigits: int, decDigits: int) -> None:
    """Refuse a picture with a negative digit count or with no digits at all."""
    if intDigits < 0 or decDigits < 0 or intDigits + decDigits == 0:
        raise ZonedDecimalError(
            f"impossible picture 9({intDigits})V9({decDigits}): digit counts must be"
            " non-negative and not both zero"
        )


def _TruncateHighOrder(magnitude: Decimal, intDigits: int) -> Decimal:
    """Drop the integer digits of a non-negative value beyond intDigits (no ON SIZE ERROR)."""
    return magnitude % Decimal(10**intDigits)


def _RequireZonedShape(text: str, scale: int) -> None:
    """Refuse non-str or empty text, and a scale outside 0 to the field's digit count."""
    if not isinstance(text, str):
        raise TypeError(f"zoned text must be str, not {type(text).__name__}")
    if text == "":
        raise ZonedDecimalError("zoned text is empty")
    if scale < 0 or scale > len(text):
        raise ZonedDecimalError(
            f"scale {scale} is outside 0 to {len(text)} for the zoned text {text!r}"
        )


def _DecodeLeadingDigits(text: str) -> tuple[int, ...]:
    """Return the digits before the sign position, refusing any character other than 0-9."""
    leadingText = text[:-1]
    for position, character in enumerate(leadingText, start=1):
        if character not in UNSIGNED_DIGITS:
            raise ZonedDecimalError(_BadCharacterMessage(text, position))
    return tuple(UNSIGNED_DIGITS.index(character) for character in leadingText)


def _DecodeSignDigit(text: str) -> tuple[int, int]:
    """Return (signBit, digit) for the last character: a plain digit or a sign overpunch."""
    character = text[-1]
    if character in UNSIGNED_DIGITS:
        return 0, UNSIGNED_DIGITS.index(character)
    if character in POSITIVE_OVERPUNCH:
        return 0, POSITIVE_OVERPUNCH.index(character)
    if character in NEGATIVE_OVERPUNCH:
        return 1, NEGATIVE_OVERPUNCH.index(character)
    raise ZonedDecimalError(_BadCharacterMessage(text, len(text)))


def _BadCharacterMessage(text: str, position: int) -> str:
    """Describe the invalid character at the 1-based position of the zoned text."""
    character = text[position - 1]
    return f"invalid character {character!r} at position {position} of the zoned text {text!r}"
