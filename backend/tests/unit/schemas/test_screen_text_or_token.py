"""Unit tests for app.schemas.common.ScreenTextOrToken, the CCDL and CCUP ``{cardNum}`` type.

The value is the CARDSID text as keyed (BMS LENGTH 16) or a longer ``card_ref`` token, as
app.services.cards.cocrdslc.DecodeCardKey tells them apart. Only screen text must keep to the
code-page-037 printable repertoire; a token is bounded by length alone, so a garbled one reaches
its decoder. The cases pin both sides of the 16-character line and the token bound.
"""

import pytest
from pydantic import TypeAdapter, ValidationError

from app.schemas.common import CARD_REF_MAX_LENGTH, ScreenTextOrToken

CARDSID_LENGTH = 16
# U+0100 has no code-page-037 byte.
OUTSIDE_CP037 = "\u0100"
CARD_KEY_ADAPTER = TypeAdapter(ScreenTextOrToken(CARDSID_LENGTH, CARD_REF_MAX_LENGTH))

ACCEPTED_VALUES = [
    pytest.param("", id="blank"),
    pytest.param("1234/678 012345.", id="cardsid-printable"),
    pytest.param(OUTSIDE_CP037 * (CARDSID_LENGTH + 1), id="token-outside-cp037"),
    pytest.param("x" * CARD_REF_MAX_LENGTH, id="token-at-bound"),
]
REJECTED_VALUES = [
    pytest.param(OUTSIDE_CP037 * CARDSID_LENGTH, id="cardsid-outside-cp037"),
    pytest.param("x" * (CARD_REF_MAX_LENGTH + 1), id="beyond-token-bound"),
]


@pytest.mark.parametrize("value", ACCEPTED_VALUES)
def test_value_is_accepted_unchanged(value: str) -> None:
    """Screen text in the repertoire and any token within the bound pass as sent."""
    assert CARD_KEY_ADAPTER.validate_python(value) == value


@pytest.mark.parametrize("value", REJECTED_VALUES)
def test_value_is_rejected_as_malformed(value: str) -> None:
    """Screen text outside the repertoire and an over-long value are validation errors."""
    with pytest.raises(ValidationError):
        CARD_KEY_ADAPTER.validate_python(value)
