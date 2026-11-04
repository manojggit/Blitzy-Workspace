"""Batch window: the advisory lock every carddemo-batch command holds in place of CLOSEFIL/OPENFIL.

Replaces: CLOSEFIL, OPENFIL (jcl/CLOSEFIL.jcl and jcl/OPENFIL.jcl, CEMT SET FIL(TRANSACT, CCXREF,
ACCTDAT, CXACAIX, USRSEC) CLO/OPE), the brackets scheduler/CardDemo.controlm puts around each
batch folder. Specified in the Technical Specification, Section 0.6.10 ("Batch window").

Pins batch/orchestration/batch_window.py and its use by batch/cli.py:

- a command started while another holds the window waits at most the lock timeout, shortened
  here to SHORT_LOCK_TIMEOUT, then exits 12 with BATCH WINDOW NOT AVAILABLE and changes nothing;
- the window is released after a successful command, after a step failure and after the CLI
  process is killed, when PostgreSQL drops the lock with the dead process's connection;
- while batch holds the window, the online side's shared request on the same key is refused,
  the lock-level form of the NOTOPEN (RESP 19, RESP2 60) refusal app.core.locks raises;
- batch never calls app.core.locks.EnterOnlineWindow.

The slow tests stage SYNTHETIC_DAILY_RECORDS daily records on a card number no cross-reference
holds, so POSTTRAN rejects every one with reason 100. The chain then holds the window long
enough to observe and still exits 4, and CREASTMT's 51-card by 10-transaction table is never
reached, as posted synthetic purchases would reach it.
"""

import signal
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import ModuleType
from typing import NoReturn

import pytest
from app.core import locks
from sqlalchemy import Connection, Engine

from batch.config import LoadBatchSettings
from batch.db import BuildAutocommitEngine
from batch.orchestration import batch_window
from batch.orchestration.batch_window import BATCH_WINDOW_UNAVAILABLE_MESSAGE, BatchWindow
from batch.tests.conftest import (
    CLI_TIMEOUT_SECONDS,
    FIXED_AS_OF_TEXT,
    BatchEnvironment,
    CliOutcome,
    InjectStepFailure,
    InvokeCli,
    IsBatchWindowHeld,
    KillProcess,
    StartCli,
    SyntheticCardNum,
    TakeSeedSnapshot,
    WaitUntil,
    WriteSyntheticDailyImage,
)

# The exit statuses the CLI reports, as the legacy condition codes: 0 clean, 4 records rejected
# (POSTTRAN RC 4), 12 failure (the refused window), 16 abend.
EXIT_OK = 0
EXIT_REJECTS = 4
EXIT_WINDOW_REFUSED = 12
EXIT_INJECTED_ABEND = 16
POSTTRAN_STEP = 13

SHORT_LOCK_TIMEOUT = "1s"
LOCK_TIMEOUT_NAME = "BATCH_WINDOW_LOCK_TIMEOUT"
SYNTHETIC_DAILY_RECORDS = 20000
SYNTHETIC_DAILY_IMAGE_NAME = "SYNTHETIC.DALYTRAN.PS"
# Synthetic card 1 is never inserted, so the xref lookup of 1500-VALIDATE-TRAN finds nothing.
UNREGISTERED_CARD_NUMBER = SyntheticCardNum(1)
WINDOW_HELD_TIMEOUT_SECONDS = 60
WINDOW_RELEASE_TIMEOUT_SECONDS = 60

RUN_ALL_ARGUMENTS = ("run-all", "--as-of", FIXED_AS_OF_TEXT)
LOAD_TRAN_TYPES_ARGUMENTS = ("load", "tran-types")
JOB_TRAN_BACKUP_ARGUMENTS = ("job", "tran-backup")
LOAD_DAILY_ARGUMENTS = ("load", "daily-transactions", "--daily-file")
REFUSED_COMMANDS = (
    pytest.param(RUN_ALL_ARGUMENTS, id="run-all"),
    pytest.param(LOAD_TRAN_TYPES_ARGUMENTS, id="load-tran-types"),
    pytest.param(JOB_TRAN_BACKUP_ARGUMENTS, id="job-tran-backup"),
)

ENTER_ONLINE_WINDOW_NAME = "EnterOnlineWindow"
BATCH_PACKAGE_NAME = "batch"
BATCH_SUBMODULE_PREFIX = BATCH_PACKAGE_NAME + "."
ONLINE_WINDOW_CALLED_MESSAGE = "batch code called app.core.locks.EnterOnlineWindow"
HELD_BY_SUBPROCESS_DESCRIPTION = "the run-all subprocess to hold the batch window"
RELEASED_AFTER_KILL_DESCRIPTION = "PostgreSQL to drop the killed run-all's batch window"


class OnlineWindowCalledError(Exception):
    """Raised by the stand-in for EnterOnlineWindow: batch code entered the online window."""


# --- Fixtures ------------------------------------------------------------------------------------
@pytest.fixture(name="backgroundRuns")
def BackgroundRuns() -> Iterator[list[subprocess.Popen]]:
    """Yield a list for started CLI processes; any still running at teardown is killed.

    A failed test therefore never leaves a run-all holding the window for the next test.
    """
    processes: list[subprocess.Popen] = []
    yield processes
    for process in processes:
        if process.poll() is None:
            KillProcess(process)


@pytest.fixture(name="onlineProbe")
def OnlineProbe() -> Iterator[Connection]:
    """Yield an unpooled autocommit connection standing in for an online request's connection.

    Closing it ends its session, so a shared lock a failed test leaves on it is dropped.
    """
    probeEngine = BuildAutocommitEngine(LoadBatchSettings())
    try:
        with probeEngine.connect() as probeConnection:
            yield probeConnection
    finally:
        probeEngine.dispose()


# --- Helpers -------------------------------------------------------------------------------------
def ShortenLockTimeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every window entered in this process wait at most SHORT_LOCK_TIMEOUT."""
    monkeypatch.setattr(batch_window, LOCK_TIMEOUT_NAME, SHORT_LOCK_TIMEOUT)


def AssertWindowRefused(outcome: CliOutcome) -> None:
    """The command exited 12 and reported BATCH WINDOW NOT AVAILABLE."""
    assert outcome.exitCode == EXIT_WINDOW_REFUSED, outcome.output
    assert BATCH_WINDOW_UNAVAILABLE_MESSAGE in outcome.output


def AssertWindowFree(engine: Engine) -> None:
    """No session holds the window, and a new one is granted within SHORT_LOCK_TIMEOUT.

    BatchWindow raises BatchWindowUnavailableError after SHORT_LOCK_TIMEOUT when it is not.
    """
    assert not IsBatchWindowHeld(engine)
    with BatchWindow(LoadBatchSettings(), lockTimeout=SHORT_LOCK_TIMEOUT):
        assert IsBatchWindowHeld(engine)
    assert not IsBatchWindowHeld(engine)


def OutputFiles(outputDir: Path) -> list[Path]:
    """Return every file and directory under the batch output root."""
    return sorted(outputDir.rglob("*"))


def StageRejectedDailyFile(imageDir: Path) -> None:
    """Stage SYNTHETIC_DAILY_RECORDS daily records, each on UNREGISTERED_CARD_NUMBER."""
    imagePath = imageDir / SYNTHETIC_DAILY_IMAGE_NAME
    WriteSyntheticDailyImage(imagePath, SYNTHETIC_DAILY_RECORDS, [UNREGISTERED_CARD_NUMBER])
    outcome = InvokeCli([*LOAD_DAILY_ARGUMENTS, str(imagePath)])
    assert outcome.exitCode == EXIT_OK, outcome.output


def StartRunAllHoldingWindow(
    engine: Engine, backgroundRuns: list[subprocess.Popen]
) -> subprocess.Popen:
    """Start run-all as a subprocess and return it once it holds the batch window."""
    process = StartCli(RUN_ALL_ARGUMENTS)
    backgroundRuns.append(process)
    WaitUntil(
        lambda: IsBatchWindowHeld(engine),
        WINDOW_HELD_TIMEOUT_SECONDS,
        HELD_BY_SUBPROCESS_DESCRIPTION,
    )
    return process


def TryOnlineSharedLock(connection: Connection) -> bool:
    """Run the online side's non-waiting shared request on BATCH_WINDOW_LOCK; return the grant."""
    parameters = {"lockKey": locks.BATCH_WINDOW_LOCK}
    return bool(connection.execute(locks.TRY_SHARED_WINDOW_STATEMENT, parameters).scalar_one())


def ReleaseOnlineSharedLock(connection: Connection) -> bool:
    """Release the shared BATCH_WINDOW_LOCK the connection holds; return whether it held one."""
    parameters = {"lockKey": locks.BATCH_WINDOW_LOCK}
    return bool(connection.execute(locks.RELEASE_SHARED_WINDOW_STATEMENT, parameters).scalar_one())


def ImportedBatchModules() -> list[ModuleType]:
    """Return the batch package and every batch.* module imported so far."""
    return [
        module
        for moduleName, module in list(sys.modules.items())
        if module is not None
        and (moduleName == BATCH_PACKAGE_NAME or moduleName.startswith(BATCH_SUBMODULE_PREFIX))
    ]


def ReplaceEnterOnlineWindow(monkeypatch: pytest.MonkeyPatch, calls: list[Sequence]) -> None:
    """Make EnterOnlineWindow record each call and raise OnlineWindowCalledError.

    app.core.locks is patched first, so a batch module importing the name later binds the
    stand-in; every batch module that already bound the name is patched too.
    """

    def RefuseOnlineWindow(*arguments: object) -> NoReturn:
        calls.append(arguments)
        raise OnlineWindowCalledError(ONLINE_WINDOW_CALLED_MESSAGE)

    monkeypatch.setattr(locks, ENTER_ONLINE_WINDOW_NAME, RefuseOnlineWindow)
    for module in ImportedBatchModules():
        if hasattr(module, ENTER_ONLINE_WINDOW_NAME):
            monkeypatch.setattr(module, ENTER_ONLINE_WINDOW_NAME, RefuseOnlineWindow)


# --- Refusal -------------------------------------------------------------------------------------
@pytest.mark.parametrize("arguments", REFUSED_COMMANDS)
def test_second_batch_command_while_window_held_exits_12_and_changes_nothing(
    batchEnvironment: BatchEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    arguments: Sequence[str],
) -> None:
    """The held window stands in for a running batch command; the second one changes nothing.

    Every table, the job_runs history and the accounts.accounts balances included, and every
    sequence read the same afterwards, and no generation or statement file is written.
    """
    ShortenLockTimeout(monkeypatch)
    stateBefore = TakeSeedSnapshot(batchEnvironment.engine)
    with BatchWindow(LoadBatchSettings()):
        outcome = InvokeCli(arguments)
    AssertWindowRefused(outcome)
    assert TakeSeedSnapshot(batchEnvironment.engine) == stateBefore
    assert OutputFiles(batchEnvironment.outputDir) == []


@pytest.mark.slow
def test_window_held_by_one_run_all_refuses_a_concurrent_run_all(
    batchEnvironment: BatchEnvironment,
    backgroundRuns: list[subprocess.Popen],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A run-all started while another runs exits 12; the first one finishes unaffected."""
    StageRejectedDailyFile(tmp_path)
    process = StartRunAllHoldingWindow(batchEnvironment.engine, backgroundRuns)
    ShortenLockTimeout(monkeypatch)
    AssertWindowRefused(InvokeCli(RUN_ALL_ARGUMENTS))
    assert process.wait(timeout=CLI_TIMEOUT_SECONDS) == EXIT_REJECTS
    AssertWindowFree(batchEnvironment.engine)


# --- Release -------------------------------------------------------------------------------------
def test_window_released_after_successful_command(batchEnvironment: BatchEnvironment) -> None:
    """OPENFIL's effect: the window opens again once a command completes."""
    outcome = InvokeCli(LOAD_TRAN_TYPES_ARGUMENTS)
    assert outcome.exitCode == EXIT_OK, outcome.output
    AssertWindowFree(batchEnvironment.engine)


def test_window_released_after_step_failure(
    batchEnvironment: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A POSTTRAN abend stops the chain with exit 16, and the window still opens again."""
    InjectStepFailure(monkeypatch, POSTTRAN_STEP, EXIT_INJECTED_ABEND)
    outcome = InvokeCli(RUN_ALL_ARGUMENTS)
    assert outcome.exitCode == EXIT_INJECTED_ABEND, outcome.output
    AssertWindowFree(batchEnvironment.engine)


@pytest.mark.slow
def test_window_released_after_cli_process_is_killed(
    batchEnvironment: BatchEnvironment,
    backgroundRuns: list[subprocess.Popen],
    tmp_path: Path,
) -> None:
    """A run-all killed while it holds the window never leaves the window closed."""
    StageRejectedDailyFile(tmp_path)
    process = StartRunAllHoldingWindow(batchEnvironment.engine, backgroundRuns)
    assert KillProcess(process) == -signal.SIGKILL
    WaitUntil(
        lambda: not IsBatchWindowHeld(batchEnvironment.engine),
        WINDOW_RELEASE_TIMEOUT_SECONDS,
        RELEASED_AFTER_KILL_DESCRIPTION,
    )
    outcome = InvokeCli(LOAD_TRAN_TYPES_ARGUMENTS)
    assert outcome.exitCode == EXIT_OK, outcome.output


# --- Online side ---------------------------------------------------------------------------------
def test_held_window_refuses_online_shared_lock(onlineProbe: Connection) -> None:
    """While batch holds the window the online shared request is refused; after exit, granted.

    A refusal is what app.core.locks.EnterOnlineWindow turns into FileNotOpenError (NOTOPEN).
    """
    with BatchWindow(LoadBatchSettings(), lockTimeout=SHORT_LOCK_TIMEOUT):
        grantedWhileHeld = TryOnlineSharedLock(onlineProbe)
    grantedAfterExit = TryOnlineSharedLock(onlineProbe)
    releasedAfterExit = ReleaseOnlineSharedLock(onlineProbe)
    assert grantedWhileHeld is False
    assert grantedAfterExit is True
    assert releasedAfterExit is True


def test_batch_never_calls_enter_online_window(
    batchEnvironment: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole chain runs to its POSTTRAN exit 4 without entering the online window."""
    onlineWindowCalls: list[Sequence] = []
    ReplaceEnterOnlineWindow(monkeypatch, onlineWindowCalls)
    outcome = InvokeCli(RUN_ALL_ARGUMENTS)
    assert outcome.exitCode == EXIT_REJECTS, outcome.output
    assert OnlineWindowCalledError.__name__ not in outcome.output
    assert onlineWindowCalls == []
