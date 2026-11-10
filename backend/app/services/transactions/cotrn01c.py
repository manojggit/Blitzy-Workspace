"""Replaces: COTRN01C (CT01), BMS COTRN01, copybooks CVTRA05Y.

The CardDemo transaction view screen, served as ``GET /api/v1/transactions/{tranId}``.
PROCESS-ENTER-KEY (lines 144-192) and READ-TRANSACT-FILE (lines 267-296) end at the first
failure, as the source's ``EVALUATE`` and ``ERR-FLG`` chain does, and every message is the
COTRN01C literal verbatim. Every outcome puts the cursor on the transaction ID field.

The rest of MAIN-PARA has no code here. ``EIBCALEN = 0`` is the regular-user guard's 401; the
first-entry display of the transaction COTRN00C selected (lines 103-108) is the page calling
ViewTransaction with its ``tranId`` query parameter; F3, F4 (CLEAR-CURRENT-SCREEN) and F5 (to
COTRN00C) are client navigation and form clearing; any other AID key cannot be sent; and
POPULATE-HEADER-INFO is the frontend ``ScreenShell``.

The source's ``READ ... UPDATE`` holds its lock only for the display task, so TRANSACT is read
with a plain SELECT on the request session and no write unit is opened. The response masks the
card number; the TRANSACT record carries no CVV.
"""

import logging
from collections.abc import Mapping
from typing import Any

from app.core.dependencies import OnlineContext
from app.core.errors import (
    HTTP_EDIT_FAILURE,
    HTTP_FILE_ERROR,
    HTTP_NOT_FOUND,
    LegacyMessageError,
    LogFileCondition,
    RepositoryError,
)
from app.core.security import MaskCardNumber
from app.repositories.transaction_repository import TransactionRepository
from app.schemas.transactions import TransactionDetailResponse
from app.utils.decimal_utils import StoreInto
from app.utils.record_layouts import EncodeTimestamp

LOGGER = logging.getLogger(__name__)

# WS-TRANID (line 37).
TRANSACTION_ID = "CT01"
# The CICS command of READ-TRANSACT-FILE, as its logged file conditions name it.
READ_OPERATION = "READ"

# COTRN01C message literals, verbatim (lines 149, 285 and 292).
TRAN_ID_EMPTY_MESSAGE = "Tran ID can NOT be empty..."
TRAN_ID_NOT_FOUND_MESSAGE = "Transaction ID NOT found..."
TRAN_LOOKUP_FAILED_MESSAGE = "Unable to lookup Transaction..."

# MOVE -1 TO TRNIDINL: the cursor target, named as the response attribute of the entered ID.
TRAN_ID_CURSOR_FIELD = "tran_id"

# MOVE TRAN-AMT TO WS-TRAN-AMT (line 177): the PIC +99999999.99 receiver keeps eight integer
# digits, so a ninth integer digit of the S9(09)V99 amount is dropped, as the map showed it.
DISPLAY_AMOUNT_INT_DIGITS = 8
DISPLAY_AMOUNT_DEC_DIGITS = 2
# COTRN01 map widths narrower than their CVTRA05Y fields: TDESC, MNAME and MCITY.
TRAN_DESC_MAP_LENGTH = 60
MERCHANT_NAME_MAP_LENGTH = 30
MERCHANT_CITY_MAP_LENGTH = 25


def RequireTranId(tranId: str) -> None:
    """Port the PROCESS-ENTER-KEY edit (lines 146-156): reject a blank transaction ID.

    A field the frontend sends is never LOW-VALUES (the screen-text repertoire excludes NUL),
    so ``TRNIDINI = SPACES OR LOW-VALUES`` is a test for empty or all-space text. COTRN01C has
    no numeric edit, so any other text is looked up as keyed.

    Raises:
        LegacyMessageError: 400 ``Tran ID can NOT be empty...``, cursor on the transaction ID.
    """
    if not tranId.strip(" "):
        raise LegacyMessageError(
            TRAN_ID_EMPTY_MESSAGE, field=TRAN_ID_CURSOR_FIELD, httpStatus=HTTP_EDIT_FAILURE
        )


def FileErrorOutcome(error: RepositoryError) -> LegacyMessageError:
    """Return the WHEN OTHER outcome of the TRANSACT read: 503 ``Unable to lookup Transaction...``.

    The condition is logged as the source's RESP and REAS DISPLAY, the batch window's NOTOPEN
    (FileNotOpenError) included. The cursor stays on the transaction ID.
    """
    LogFileCondition(LOGGER, TRANSACTION_ID, READ_OPERATION, error)
    return LegacyMessageError(
        TRAN_LOOKUP_FAILED_MESSAGE, field=TRAN_ID_CURSOR_FIELD, httpStatus=HTTP_FILE_ERROR
    )


async def ReadTransactionOrRaise(context: OnlineContext, tranId: str) -> Mapping[str, Any]:
    """Port READ-TRANSACT-FILE (lines 267-296): read the TRANSACT record of ``tranId``.

    The repository matches the key with trailing spaces removed, as the space-padded
    TRAN-ID X(16) key compares, and enters the online batch window first.

    Raises:
        LegacyMessageError: 404 ``Transaction ID NOT found...`` (NOTFND); 503
            ``Unable to lookup Transaction...`` (WHEN OTHER), the batch window included. Both
            put the cursor on the transaction ID.
    """
    try:
        tranRecord = await TransactionRepository(context).GetTransaction(tranId)
    except RepositoryError as error:
        raise FileErrorOutcome(error) from error
    if tranRecord is None:
        raise LegacyMessageError(
            TRAN_ID_NOT_FOUND_MESSAGE, field=TRAN_ID_CURSOR_FIELD, httpStatus=HTTP_NOT_FOUND
        )
    return tranRecord


def BuildDetailResponse(tranRecord: Mapping[str, Any]) -> TransactionDetailResponse:
    """Port the map moves of PROCESS-ENTER-KEY (lines 176-190) into the detail response.

    The card number is masked, text wider than its map field is truncated to the map width,
    and each timestamp is rebuilt as the 26-character text the record held. The message is
    empty: COTRN01C sets none on a successful display.
    """
    displayAmount = StoreInto(
        tranRecord["tran_amt"], DISPLAY_AMOUNT_INT_DIGITS, DISPLAY_AMOUNT_DEC_DIGITS
    )
    return TransactionDetailResponse(
        tran_id=tranRecord["tran_id"],
        card_num_masked=MaskCardNumber(tranRecord["card_num"]),
        tran_type_cd=tranRecord["tran_type_cd"],
        tran_cat_cd=tranRecord["tran_cat_cd"],
        tran_source=tranRecord["tran_source"],
        tran_desc=tranRecord["tran_desc"][:TRAN_DESC_MAP_LENGTH],
        tran_amt=displayAmount,
        orig_ts=EncodeTimestamp(tranRecord["orig_ts"], tranRecord["orig_ts_format"]),
        proc_ts=EncodeTimestamp(tranRecord["proc_ts"], tranRecord["proc_ts_format"]),
        merchant_id=tranRecord["merchant_id"],
        merchant_name=tranRecord["merchant_name"][:MERCHANT_NAME_MAP_LENGTH],
        merchant_city=tranRecord["merchant_city"][:MERCHANT_CITY_MAP_LENGTH],
        merchant_zip=tranRecord["merchant_zip"],
    )


async def ViewTransaction(context: OnlineContext, tranId: str) -> TransactionDetailResponse:
    """Port PROCESS-ENTER-KEY (lines 144-192), the ENTER key: show the transaction ``tranId``.

    Args:
        context: The regular user's request context.
        tranId: The TRNIDIN field as keyed, up to 16 characters.

    Returns:
        The record's fields as the COTRN01 map shows them, with the card number masked and an
        empty message.

    Raises:
        LegacyMessageError: 400 for a blank transaction ID, 404 when no transaction has it, 503
            when TRANSACT cannot be read, the batch window included.
    """
    RequireTranId(tranId)
    tranRecord = await ReadTransactionOrRaise(context, tranId)
    return BuildDetailResponse(tranRecord)
