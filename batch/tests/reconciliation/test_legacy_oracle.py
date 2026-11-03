"""Synthetic edge cases of the independent legacy oracle that the sample images never reach.

The sample chain behind ``batch/tests/fixtures/expected`` takes none of these branches, so each
case runs the oracle in-test on a synthetic input and pins the legacy outcome it transcribes:

- the TRANFILE replace load (``jcl/TRANFILE.jcl`` STEP15) and the COMBTRAN reload
  (``jcl/COMBTRAN.jcl`` STEP10) refuse a duplicate TRAN-ID, as IDCAMS REPRO into the TRANSACT
  KSDS does, and publish no new state;
- CBTRN02C's PIC 9(09) WS-TRANSACTION-COUNT and WS-REJECT-COUNT wrap at 10**9, and the return
  code tests the stored reject counter (``cbl/CBTRN02C.cbl`` lines 184-186, 206, 214, 229-230);
- CBTRN03C starts WS-CURR-CARD-NUM as SPACES, so a leading all-space card reads no xref
  (``cbl/CBTRN03C.cbl`` lines 137 and 181-188).

Like the rest of this package it imports nothing from ``app`` or ``batch``, so a defect shared
with the production code cannot validate itself.
"""

import dataclasses
import shutil
from collections.abc import Iterator, Mapping
from decimal import Decimal
from pathlib import Path

import pytest

from . import legacy_oracle

# A canonical CVTRA05Y transaction; TransactionRecord adds both timestamps at the frozen clock,
# so its TRAN-PROC-DT lies in the 2022-07-18 report range. Each case overrides what it pins.
BASE_TRANSACTION_VALUES: dict[str, object] = {
    "tran_id": "0000000000000001",
    "tran_type_cd": "01",
    "tran_cat_cd": 1,
    "tran_source": "POS TERM",
    "tran_desc": "SYNTHETIC PURCHASE",
    "tran_amt": Decimal("10.00"),
    "merchant_id": "000000001",
    "merchant_name": "SYNTHETIC MERCHANT",
    "merchant_city": "SYNTHETIC CITY",
    "merchant_zip": "00000",
    "card_num": "0000000000000101",
}


def TransactionRecord(overrides: Mapping[str, object]) -> str:
    """Return a 350-character CVTRA05Y record: BASE_TRANSACTION_VALUES with overrides applied.

    orig_ts and proc_ts hold the frozen DB2 clock text (format D) unless overridden.
    """
    clockValue, clockFormat = legacy_oracle.DecodeTimestampText(legacy_oracle.DB2_CLOCK_TEXT)
    values = {
        **BASE_TRANSACTION_VALUES,
        "orig_ts": clockValue,
        "orig_ts" + legacy_oracle.TIMESTAMP_FORMAT_SUFFIX: clockFormat,
        "proc_ts": clockValue,
        "proc_ts" + legacy_oracle.TIMESTAMP_FORMAT_SUFFIX: clockFormat,
        **overrides,
    }
    return legacy_oracle.FormatRecord(
        values, legacy_oracle.TRANSACTION_FIELDS, legacy_oracle.TRANSACTION_LRECL
    )


def EmptyState() -> legacy_oracle.OracleState:
    """Return an OracleState whose ten VSAM clusters are all empty."""
    stateFields = dataclasses.fields(legacy_oracle.OracleState)
    return legacy_oracle.OracleState(**{stateField.name: {} for stateField in stateFields})


# CBTRN02C WS-COUNTERS: both PIC 9(09) counters wrap at 10**9, and the return code tests the
# stored WS-REJECT-COUNT (cbl/CBTRN02C.cbl lines 184-186, 206, 214-215, 229-230).
POSTTRAN_LAST_COUNTER_VALUE = 999_999_999
# The 80-character DALYREJS trailer of a reason-100 reject: 9(04) reason plus X(76) text.
POSTTRAN_INVALID_CARD_TRAILER = "0100" + "INVALID CARD NUMBER FOUND".ljust(76)


@pytest.mark.parametrize(
    ("counter", "expected"),
    [(0, 1), (999_999_998, 999_999_999), (999_999_999, 0)],
)
def test_posttran_counter_add_one_wraps_at_receiver_width(counter: int, expected: int) -> None:
    """ADD 1 into a PIC 9(09) counter stores (n + 1) % 10**9, lines 206 and 214."""
    assert legacy_oracle.NextCounterValue(counter) == expected


def test_posttran_counters_wrap_and_status_reads_stored_reject_count() -> None:
    """One more reject at 999999999 stores 0 in both counters, so the status is 0 (229-230)."""
    record = TransactionRecord({})
    work = legacy_oracle.NewPostingWorkArea(EmptyState(), legacy_oracle.DB2_CLOCK_TEXT)
    work.processedCount = POSTTRAN_LAST_COUNTER_VALUE
    work.rejectCount = POSTTRAN_LAST_COUNTER_VALUE
    abended = legacy_oracle.PostDailyFile(work, [record])
    assert abended is False
    assert work.rejectRecords == [record + POSTTRAN_INVALID_CARD_TRAILER]
    assert (work.processedCount, work.rejectCount) == (0, 0)
    assert legacy_oracle.PosttranExitCode(work.rejectCount, False) == legacy_oracle.EXIT_OK


@pytest.mark.parametrize(
    ("rejectCount", "abended", "expected"),
    [(0, False, 0), (1, False, 4), (0, True, 16), (1, True, 16)],
)
def test_posttran_exit_code_tests_stored_reject_count(
    rejectCount: int, abended: bool, expected: int
) -> None:
    """Abend 999 gives 16; else IF WS-REJECT-COUNT > 0 MOVE 4 TO RETURN-CODE (229-230), else 0."""
    assert legacy_oracle.PosttranExitCode(rejectCount, abended) == expected


def test_posttran_run_reports_stored_counters_and_status() -> None:
    """RunPosttran returns the stored counters, every DALYREJS record and status 4 (206-230)."""
    record = TransactionRecord({})
    outcome = legacy_oracle.RunPosttran(EmptyState(), [record], legacy_oracle.DB2_CLOCK_TEXT)
    assert (outcome.processedCount, outcome.rejectCount, outcome.exitCode) == (1, 1, 4)
    assert outcome.rejectRecords == [record + POSTTRAN_INVALID_CARD_TRAILER]


# CBTRN03C: WS-CURR-CARD-NUM starts as SPACES (cbl/CBTRN03C.cbl lines 137 and 181-188).
TRANREPT_CARD_NUM = str(BASE_TRANSACTION_VALUES["card_num"])
TRANREPT_BLANK_CARD_NUM = " " * legacy_oracle.CARD_NUM_LENGTH
TRANREPT_ACCT_ID = "00000000101"
# TRAN-REPORT-ACCOUNT-ID X(11) of TRANSACTION-DETAIL-REPORT, after TRAN-ID X(16) and one space.
TRANREPT_ACCOUNT_SLICE = slice(17, 17 + legacy_oracle.ACCT_ID_LENGTH)


class RecordingXrefs(Mapping[str, dict[str, object]]):
    """CARDXREF records by card number that remember every key read, in read order."""

    def __init__(self, xrefRecords: Mapping[str, dict[str, object]]) -> None:
        """Hold the xref records and start with no read recorded."""
        self.xrefRecords = dict(xrefRecords)
        self.readKeys: list[str] = []

    def __getitem__(self, cardKey: str) -> dict[str, object]:
        """Record the read, then return the record or raise KeyError (INVALID KEY)."""
        self.readKeys.append(cardKey)
        return self.xrefRecords[cardKey]

    def __iter__(self) -> Iterator[str]:
        """Iterate the card numbers held, without recording a read."""
        return iter(self.xrefRecords)

    def __len__(self) -> int:
        """Return how many xref records are held."""
        return len(self.xrefRecords)


def TranreptInput(transactions: list[str], xrefs: RecordingXrefs) -> legacy_oracle.ReportInput:
    """Return a TRANREPT input over the report range, with type 01 and category (01, 1)."""
    return legacy_oracle.ReportInput(
        transactions=transactions,
        xrefs=xrefs,
        transactionTypes={"01": {"description": "Purchase"}},
        transactionCategories={("01", 1): {"description": "Regular Sales Draft"}},
        startDate=legacy_oracle.REPORT_START_DATE,
        endDate=legacy_oracle.REPORT_END_DATE,
    )


def test_tranrept_leading_blank_card_reads_no_xref() -> None:
    """A leading all-space card equals the initial WS-CURR-CARD-NUM, so CARDXREF is not read.

    cbl/CBTRN03C.cbl lines 137 and 181-188: the only xref read is the second card's. The blank
    card's TRAN-REPORT-ACCOUNT-ID shows spaces, and WS-FIRST-TIME is already 'N' when the card
    changes, so the blank card's Account Total precedes the second card's detail line.
    """
    xrefs = RecordingXrefs({TRANREPT_CARD_NUM: {"acct_id": TRANREPT_ACCT_ID}})
    records = [
        TransactionRecord({"tran_id": "0000000000000001", "card_num": TRANREPT_BLANK_CARD_NUM}),
        TransactionRecord({"tran_id": "0000000000000002", "card_num": TRANREPT_CARD_NUM}),
    ]
    lines = legacy_oracle.BuildReportLines(TranreptInput(records, xrefs))
    tranIds = [line[legacy_oracle.TRAN_ID_SLICE] for line in lines]
    blankIndex = tranIds.index("0000000000000001")
    cardIndex = tranIds.index("0000000000000002")
    blankTotal = legacy_oracle.EditReportAmount(Decimal("10.00"), "+")
    assert xrefs.readKeys == [TRANREPT_CARD_NUM]
    assert lines[blankIndex][TRANREPT_ACCOUNT_SLICE] == " " * legacy_oracle.ACCT_ID_LENGTH
    assert lines[cardIndex][TRANREPT_ACCOUNT_SLICE] == TRANREPT_ACCT_ID
    assert blankIndex < cardIndex - 2
    assert lines[cardIndex - 2].rstrip() == legacy_oracle.ACCOUNT_TOTAL_LABEL + blankTotal
    assert lines[cardIndex - 1] == legacy_oracle.REPORT_HEADER_2


def test_tranrept_blank_card_after_a_card_reads_xref_and_abends() -> None:
    """A blank card after another card differs from WS-CURR-CARD-NUM, so CARDXREF is read.

    SelectReportRecords sorts an all-space card before every other card, so the case drives
    ReportOneRecord directly on the work area the first card left. The blank key is absent from
    CARDXREF, and INVALID KEY performs 9999-ABEND-PROGRAM (lines 181-188 and 484-492).
    """
    xrefs = RecordingXrefs({TRANREPT_CARD_NUM: {"acct_id": TRANREPT_ACCT_ID}})
    work = legacy_oracle.ReportWorkArea(TranreptInput([], xrefs))
    cardRecord = TransactionRecord({"tran_id": "0000000000000001", "card_num": TRANREPT_CARD_NUM})
    blankRecord = TransactionRecord(
        {"tran_id": "0000000000000002", "card_num": TRANREPT_BLANK_CARD_NUM}
    )
    legacy_oracle.ReportOneRecord(work, cardRecord)
    with pytest.raises(legacy_oracle.OracleAbend, match="INVALID CARD NUMBER IN CARDXREF"):
        legacy_oracle.ReportOneRecord(work, blankRecord)
    assert xrefs.readKeys == [TRANREPT_CARD_NUM, ""]


# TRANFILE STEP15 and COMBTRAN STEP10 REPRO into the TRANSACT KSDS, KEYS(16 0)
# (jcl/TRANFILE.jcl lines 53 and 67-75; jcl/COMBTRAN.jcl lines 41-48): a duplicate TRAN-ID is
# refused, the step ends at status 12 and no new state is published.
TRANFILE_PRIMING_RECORD = (
    legacy_oracle.PRIMING_CHARACTER * legacy_oracle.PRIMING_PREFIX_LENGTH + "00000100"
)
TRANFILE_FIRST_TRAN_ID = "0000000000000001"
TRANFILE_SECOND_TRAN_ID = "0000000000000002"
TRANFILE_FIRST_RECORD = TransactionRecord({"tran_id": TRANFILE_FIRST_TRAN_ID})
TRANFILE_FIRST_RECORD_AGAIN = TransactionRecord(
    {"tran_id": TRANFILE_FIRST_TRAN_ID, "tran_amt": Decimal("20.00")}
)
TRANFILE_SECOND_RECORD = TransactionRecord({"tran_id": TRANFILE_SECOND_TRAN_ID})
TRANFILE_TRAN_ID_LENGTH = legacy_oracle.TRAN_ID_SLICE.stop
# Not a priming record, yet its TRAN-ID is the priming record's all-NUL key.
TRANFILE_PRIMING_KEY_RECORD = (
    legacy_oracle.PRIMING_CHARACTER * TRANFILE_TRAN_ID_LENGTH
    + TRANFILE_SECOND_RECORD[TRANFILE_TRAN_ID_LENGTH:]
)
# The status COMBTRAN ends with when its REPRO meets a duplicate key.
COMBTRAN_DUPLICATE_KEY_STATUS = 12
# The first INTCALC TRAN-ID: PARM-DATE 2022071800 and suffix 000001 (jcl/INTCALC.jcl STEP15).
COMBTRAN_INTEREST_TRAN_ID = legacy_oracle.PARM_DATE + "000001"
COMBTRAN_BKUP_RECORD = TransactionRecord({"tran_id": COMBTRAN_INTEREST_TRAN_ID})
COMBTRAN_SYSTRAN_RECORD = TransactionRecord(
    {
        "tran_id": COMBTRAN_INTEREST_TRAN_ID,
        "tran_cat_cd": 5,
        "tran_source": "System",
        "tran_desc": "Int. for a/c 00000000001",
    }
)
COMBTRAN_LOWER_SYSTRAN_RECORD = TransactionRecord({"tran_id": TRANFILE_FIRST_TRAN_ID})
SAMPLE_EBCDIC_DIR = Path(__file__).resolve().parents[3] / "data" / "EBCDIC"


@pytest.fixture(name="duplicateKeyDir")
def DuplicateKeyDir(request: pytest.FixtureRequest) -> Path:
    """Return the test's own pytest tmp_path, injected under a camelCase name."""
    return request.getfixturevalue("tmp_path")


def WriteTransactionImage(directory: Path, records: list[str]) -> Path:
    """Write records as the cp037 TRANFILE image (DALYTRAN.PS.INIT) in directory; return it."""
    path = legacy_oracle.ImagePath(directory, legacy_oracle.DALYTRAN_INIT_IMAGE)
    path.write_bytes(b"".join(legacy_oracle.EncodeCp037(record) for record in records))
    return path


def StateHolding(records: list[str]) -> legacy_oracle.OracleState:
    """Return an otherwise empty OracleState whose TRANSACT holds records by TRAN-ID."""
    transactions = {record[legacy_oracle.TRAN_ID_SLICE]: record for record in records}
    return dataclasses.replace(EmptyState(), transactions=transactions)


@pytest.mark.parametrize(
    ("records", "recordNumber", "firstRecordNumber"),
    [
        ([TRANFILE_PRIMING_RECORD, TRANFILE_FIRST_RECORD, TRANFILE_FIRST_RECORD_AGAIN], 3, 2),
        ([TRANFILE_PRIMING_RECORD, TRANFILE_PRIMING_KEY_RECORD], 2, 1),
    ],
    ids=["repeats-an-earlier-record", "repeats-the-priming-key"],
)
def test_tranfile_load_refuses_a_duplicate_tran_id(
    duplicateKeyDir: Path, records: list[str], recordNumber: int, firstRecordNumber: int
) -> None:
    """The REPRO into KEYS(16 0) refuses a repeated TRAN-ID; records count from the priming one."""
    path = WriteTransactionImage(duplicateKeyDir, records)
    with pytest.raises(legacy_oracle.ImageRecordError) as raised:
        legacy_oracle.LoadTransactionImage(duplicateKeyDir)
    assert raised.value.path == path
    assert raised.value.recordNumber == recordNumber
    assert raised.value.reason == f"duplicate key (tran_id) of record {firstRecordNumber}"


def test_tranfile_load_keeps_distinct_records_and_skips_the_priming_record(
    duplicateKeyDir: Path,
) -> None:
    """Distinct TRAN-IDs all load, keyed by TRAN-ID, and the priming record is skipped."""
    records = [TRANFILE_PRIMING_RECORD, TRANFILE_FIRST_RECORD, TRANFILE_SECOND_RECORD]
    WriteTransactionImage(duplicateKeyDir, records)
    assert legacy_oracle.LoadTransactionImage(duplicateKeyDir) == {
        TRANFILE_FIRST_TRAN_ID: TRANFILE_FIRST_RECORD,
        TRANFILE_SECOND_TRAN_ID: TRANFILE_SECOND_RECORD,
    }


def test_combtran_sort_keeps_colliding_records_in_input_order() -> None:
    """STEP05R only sorts, stably: a BKUP/SYSTRAN TRAN-ID collision keeps both, BKUP first."""
    combined = legacy_oracle.RunCombtran(
        [COMBTRAN_BKUP_RECORD], [COMBTRAN_LOWER_SYSTRAN_RECORD, COMBTRAN_SYSTRAN_RECORD]
    )
    assert combined == [
        COMBTRAN_LOWER_SYSTRAN_RECORD,
        COMBTRAN_BKUP_RECORD,
        COMBTRAN_SYSTRAN_RECORD,
    ]


@pytest.mark.parametrize(
    ("heldRecords", "combinedRecords", "expectedReason"),
    [
        (
            [],
            [COMBTRAN_LOWER_SYSTRAN_RECORD, COMBTRAN_BKUP_RECORD, COMBTRAN_SYSTRAN_RECORD],
            "record 3: duplicate key (tran_id) of record 2",
        ),
        (
            [COMBTRAN_BKUP_RECORD],
            [COMBTRAN_LOWER_SYSTRAN_RECORD, COMBTRAN_SYSTRAN_RECORD],
            "record 2: duplicate key (tran_id) of a record already in TRANSACT",
        ),
    ],
    ids=["bkup-systran-collision", "collision-with-transact"],
)
def test_combtran_reload_refuses_a_duplicate_tran_id(
    heldRecords: list[str], combinedRecords: list[str], expectedReason: str
) -> None:
    """STEP10 ends at status 12 on a duplicate TRAN-ID and leaves the input state as it was."""
    state = StateHolding(heldRecords)
    heldBefore = dict(state.transactions)
    with pytest.raises(legacy_oracle.OracleAbend) as raised:
        legacy_oracle.ReloadCombinedTransactions(state, combinedRecords)
    assert raised.value.exitCode == COMBTRAN_DUPLICATE_KEY_STATUS
    assert raised.value.message.endswith(expectedReason)
    assert state.transactions == heldBefore


def test_combtran_reload_adds_records_and_leaves_the_input_state_unchanged() -> None:
    """STEP10 adds every combined record to what TRANSACT holds and returns a new state."""
    state = StateHolding([COMBTRAN_LOWER_SYSTRAN_RECORD])
    reloaded = legacy_oracle.ReloadCombinedTransactions(state, [COMBTRAN_SYSTRAN_RECORD])
    assert reloaded.transactions == {
        TRANFILE_FIRST_TRAN_ID: COMBTRAN_LOWER_SYSTRAN_RECORD,
        COMBTRAN_INTEREST_TRAN_ID: COMBTRAN_SYSTRAN_RECORD,
    }
    assert state.transactions == {TRANFILE_FIRST_TRAN_ID: COMBTRAN_LOWER_SYSTRAN_RECORD}


def test_combtran_chain_stops_when_a_posted_tran_id_repeats_an_interest_id(
    duplicateKeyDir: Path,
) -> None:
    """A posted daily record carrying the first INTCALC TRAN-ID stops the chain at COMBTRAN.

    Daily record 1 of the sample is posted, so TRANSACT.BKUP(0) holds it beside SYSTRAN(0)'s
    first interest record. All 261 other posted TRAN-IDs sort lower, so the BKUP record is
    combined record 262 and the interest record 263, and the chain ends before CREASTMT.
    """
    ebcdicDir = duplicateKeyDir / "EBCDIC"
    shutil.copytree(SAMPLE_EBCDIC_DIR, ebcdicDir)
    dailyPath = legacy_oracle.ImagePath(ebcdicDir, legacy_oracle.DALYTRAN_IMAGE)
    dailyBytes = dailyPath.read_bytes()
    collidingId = legacy_oracle.EncodeCp037(COMBTRAN_INTEREST_TRAN_ID)
    dailyPath.write_bytes(collidingId + dailyBytes[TRANFILE_TRAN_ID_LENGTH:])
    with pytest.raises(legacy_oracle.OracleAbend) as raised:
        legacy_oracle.RunSampleChain(ebcdicDir, legacy_oracle.DB2_CLOCK_TEXT)
    assert raised.value.exitCode == COMBTRAN_DUPLICATE_KEY_STATUS
    assert raised.value.message == (
        "COMBTRAN STEP10 REPRO: TRANSACT.COMBINED(+1) record 263: "
        "duplicate key (tran_id) of record 262"
    )
