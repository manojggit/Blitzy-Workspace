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
- batch never calls app.core.locks.EnterOnlineWindow;
- once the guard session alone is ended (pg_terminate_backend of the window's holder), the next
  job commit, new job connection, generation publication and statement publication are refused
  with BatchWindowLostError, a loss while a publication hashes or syncs its files included:
  nothing commits or is renamed, the window is never taken again, and the CLI exits 12 with
  BATCH WINDOW LOST;
- a command entering after such a loss waits for the former owner's job sessions, so while a
  paused POSTTRAN keeps them it exits 12 with BATCH WINDOW NOT AVAILABLE, classifying and
  cleaning nothing, and once POSTTRAN has stopped the next command classifies it INTERRUPTED.

The slow tests stage SYNTHETIC_DAILY_RECORDS daily records on a card number no cross-reference
holds, so POSTTRAN rejects every one with reason 100. The chain then holds the window long
enough to observe and still exits 4, and the capacity of CREASTMT's 51-card by 10-transaction
table is not exceeded, as posted synthetic purchases would exceed it.
"""

import contextlib
import functools
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import NoReturn

import pytest
from app.core import locks
from app.core.paths import REPO_ROOT
from app.models import GDG_GENERATIONS, Account, Transaction, TransactionType
from sqlalchemy import BigInteger, Connection, Engine, bindparam, func, select, text, update

from batch.config import LoadBatchSettings
from batch.db import BuildAutocommitEngine, BuildEngine
from batch.jobs.statement_gen import creastmt
from batch.orchestration import batch_window, gdg, run_ledger
from batch.orchestration.batch_window import (
    BATCH_WINDOW_LOST_MESSAGE,
    BATCH_WINDOW_UNAVAILABLE_MESSAGE,
    BatchWindow,
    BatchWindowLostError,
)
from batch.tests.conftest import (
    CLI_TIMEOUT_SECONDS,
    FIXED_AS_OF_TEXT,
    BatchEnvironment,
    CliCommand,
    CliOutcome,
    CountJobRuns,
    InjectStepFailure,
    InsertSyntheticCards,
    InsertSyntheticTransactions,
    InvokeCli,
    IsBatchWindowHeld,
    KillProcess,
    RequireBatchExecutable,
    StartCli,
    StepRow,
    SyntheticCardNum,
    SyntheticTransactionSpec,
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
PAUSED_POSTTRAN_DESCRIPTION = "POSTTRAN to wait on the held account row lock"

POST_TRAN_ARGUMENTS = ("job", "post-tran", "--as-of", FIXED_AS_OF_TEXT)
POSTTRAN_LOG_NAME = "posttran.log"
PUBLISH_RUN_GENERATIONS_NAME = "PublishRunGenerations"
HASH_MATCHES_NAME = "HashMatches"
FSYNC_NAME = "fsync"
STATEMENT_JOB_ARGUMENTS = ("job", creastmt.JOB_NAME)
FORCE_OPTION = "--force"
STATEMENT_TRANSACTION_AMOUNT = Decimal("1.00")
GDG_DIRECTORY_NAME = "gdg"
PART_FILE_GLOB = "*.part"
GENERATION_FILE_GLOB = "G*V00.dat"
# Written over every transaction type by a write the fence must refuse; never committed.
FENCED_TYPE_DESCRIPTION = "WRITTEN AFTER THE WINDOW WAS LOST"
TERMINATE_TIMEOUT_MILLISECONDS = 5000

# The guard: the one session of this database holding BATCH_WINDOW_LOCK exclusively. pg_locks
# shows a single-bigint key as classid (high 32 bits) and objid (low 32 bits), objsubid 1.
WINDOW_HOLDER_STATEMENT = text(
    "SELECT pid FROM pg_locks"
    " WHERE locktype = 'advisory' AND granted AND mode = 'ExclusiveLock' AND objsubid = 1"
    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    " AND ((classid::bigint << 32) | objid::bigint) = :lockKey"
).bindparams(bindparam("lockKey", type_=BigInteger))
# With a timeout, pg_terminate_backend returns once the backend has exited, its locks dropped.
TERMINATE_BACKEND_STATEMENT = text("SELECT pg_terminate_backend(:backendPid, :timeoutMillis)")
# A session of this database, other than the probe's own, waiting on a heavyweight lock.
LOCK_WAITERS_STATEMENT = text(
    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
    " AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'"
)
# The account of each staged daily record that has a cross-reference, in posting order.
POSTING_ACCOUNTS_STATEMENT = text(
    "SELECT x.acct_id FROM transactions.daily_transactions AS d"
    " JOIN xref.card_xref AS x ON x.card_num = d.card_num ORDER BY d.record_seq"
)
# Every lock, held or awaited, on a key of this database.
KEY_LOCK_COUNT_STATEMENT = text(
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objsubid = 1"
    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    " AND ((classid::bigint << 32) | objid::bigint) = :lockKey"
).bindparams(bindparam("lockKey", type_=BigInteger))
ACCOUNT_ROW_LOCK_STATEMENT = text(
    "SELECT 1 FROM accounts.accounts WHERE acct_id = :acctId FOR UPDATE"
)
FENCED_WRITE_STATEMENT = update(TransactionType).values(description=FENCED_TYPE_DESCRIPTION)


class OnlineWindowCalledError(Exception):
    """Raised by the stand-in for EnterOnlineWindow: batch code entered the online window."""


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


@pytest.fixture(name="jobEngine")
def JobEngine(batchEnvironment: BatchEnvironment) -> Iterator[Engine]:
    """Yield a pooled job engine on the seeded test database for a window to fence."""
    engine = BuildEngine(LoadBatchSettings())
    try:
        yield engine
    finally:
        engine.dispose()


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


def TerminateWindowHolder(engine: Engine) -> None:
    """End only the window's guard session, as a failure of that one connection would."""
    with engine.connect() as connection:
        holderPid = connection.execute(
            WINDOW_HOLDER_STATEMENT, {"lockKey": locks.BATCH_WINDOW_LOCK}
        ).scalar_one()
        parameters = {"backendPid": holderPid, "timeoutMillis": TERMINATE_TIMEOUT_MILLISECONDS}
        assert connection.execute(TERMINATE_BACKEND_STATEMENT, parameters).scalar_one()


def AssertWindowLost(outcome: CliOutcome) -> None:
    """The command exited 12 and reported BATCH WINDOW LOST."""
    assert outcome.exitCode == EXIT_WINDOW_REFUSED, outcome.output
    assert BATCH_WINDOW_LOST_MESSAGE in outcome.output


def CountJobSessionLocks(engine: Engine) -> int:
    """Return how many locks on BATCH_JOB_SESSION_LOCK this database's sessions hold or await."""
    with engine.connect() as connection:
        parameters = {"lockKey": batch_window.BATCH_JOB_SESSION_LOCK}
        return connection.execute(KEY_LOCK_COUNT_STATEMENT, parameters).scalar_one()


def CommitNothing(engine: Engine) -> None:
    """Commit a read in engine.begin(): the fence admits it while the window holds."""
    with engine.begin() as connection:
        connection.execute(select(func.count()).select_from(TransactionType))


def CommitInBeginBlock(engine: Engine) -> None:
    """Write in engine.begin(), whose block exit commits, as an atomic step's unit does."""
    with engine.begin() as connection:
        connection.execute(FENCED_WRITE_STATEMENT)


def CommitExplicitly(engine: Engine) -> None:
    """Write on engine.connect() and commit by hand, as POSTTRAN's final unit does."""
    with engine.connect() as connection:
        connection.execute(FENCED_WRITE_STATEMENT)
        connection.commit()


COMMIT_PATTERNS = (
    pytest.param(CommitInBeginBlock, id="begin-block"),
    pytest.param(CommitExplicitly, id="explicit-commit"),
)


def BaseFileNames(outputDir: Path, base: gdg.GdgBase, pattern: str) -> list[str]:
    """Return the names of the files matching pattern in the base's GDG directory."""
    baseDirectory = outputDir / GDG_DIRECTORY_NAME / base.name
    return sorted(path.name for path in baseDirectory.glob(pattern))


def GenerationStates(engine: Engine, base: gdg.GdgBase) -> list[str]:
    """Return the catalog state of every generation of the base, oldest first."""
    statement = (
        select(GDG_GENERATIONS.c.state)
        .where(GDG_GENERATIONS.c.base_name == base.name)
        .order_by(GDG_GENERATIONS.c.generation)
    )
    with engine.connect() as connection:
        return list(connection.execute(statement).scalars())


def SecondPostingAccount(engine: Engine) -> str:
    """Return the first posting account, in record order, other than the first record's."""
    with engine.connect() as connection:
        accountIds = list(connection.execute(POSTING_ACCOUNTS_STATEMENT).scalars())
    return next(acctId for acctId in accountIds if acctId != accountIds[0])


@contextlib.contextmanager
def HeldAccountLock(engine: Engine, acctId: str) -> Iterator[Callable[[], None]]:
    """Lock one account row FOR UPDATE; yield its release, a rollback exit repeats harmlessly."""
    with engine.connect() as connection:
        connection.execute(ACCOUNT_ROW_LOCK_STATEMENT, {"acctId": acctId}).one()
        yield connection.rollback


def PostingState(engine: Engine, acctId: str) -> tuple[int, object]:
    """Return the transaction count and the account's balance, both of which a posting moves."""
    countStatement = select(func.count()).select_from(Transaction)
    balanceStatement = select(Account.curr_balance).where(Account.acct_id == acctId)
    with engine.connect() as connection:
        transactionCount = connection.execute(countStatement).scalar_one()
        return transactionCount, connection.execute(balanceStatement).scalar_one()


def IsLockAwaited(engine: Engine) -> bool:
    """Return whether another session of the test database waits on a heavyweight lock."""
    with engine.connect() as connection:
        return connection.execute(LOCK_WAITERS_STATEMENT).scalar_one() > 0


def StartLoggedCli(arguments: Sequence[str], logPath: Path) -> subprocess.Popen:
    """Start carddemo-batch as StartCli does, with its stdout and stderr written to logPath."""
    RequireBatchExecutable()
    with logPath.open("wb") as logFile:
        return subprocess.Popen(
            CliCommand(arguments),
            cwd=REPO_ROOT,
            env=dict(os.environ),
            stdout=logFile,
            stderr=subprocess.STDOUT,
        )


def StartPausedPostTran(
    engine: Engine, backgroundRuns: list[subprocess.Popen], logPath: Path
) -> subprocess.Popen:
    """Start POSTTRAN and return it once it waits on the held account lock, between its units."""
    process = StartLoggedCli(POST_TRAN_ARGUMENTS, logPath)
    backgroundRuns.append(process)
    WaitUntil(
        lambda: IsLockAwaited(engine), WINDOW_HELD_TIMEOUT_SECONDS, PAUSED_POSTTRAN_DESCRIPTION
    )
    return process


def AssertLiveRunUntouched(environment: BatchEnvironment) -> None:
    """The paused POSTTRAN's run is still RUNNING, and its DALYREJS .part is still on disk."""
    assert CountJobRuns(environment.engine, POSTTRAN_STEP, run_ledger.RUN_RUNNING) == 1
    assert BaseFileNames(environment.outputDir, gdg.DALYREJS, PART_FILE_GLOB) != []


def AssertStoppedRunClassifiedInterrupted(environment: BatchEnvironment) -> None:
    """The stopped POSTTRAN cataloged no DALYREJS; the next command marks its run INTERRUPTED."""
    assert BaseFileNames(environment.outputDir, gdg.DALYREJS, PART_FILE_GLOB) == []
    assert GenerationStates(environment.engine, gdg.DALYREJS) == []
    outcome = InvokeCli(LOAD_TRAN_TYPES_ARGUMENTS)
    assert outcome.exitCode == EXIT_OK, outcome.output
    assert CountJobRuns(environment.engine, POSTTRAN_STEP, run_ledger.RUN_INTERRUPTED) == 1
    assert StepRow(environment.engine, POSTTRAN_STEP)["state"] == run_ledger.STEP_INTERRUPTED


def LoseWindowOnFirstMatchingCall(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    patchTarget: tuple[ModuleType, str],
    isLossPoint: Callable[..., bool],
) -> None:
    """Wrap patchTarget's function so its first call isLossPoint accepts ends the guard first.

    That call, and every other, then runs the original function, so a later command is unaffected.
    """
    module, functionName = patchTarget
    originalFunction = getattr(module, functionName)
    lossCalls: list[tuple[object, ...]] = []

    def TerminateThenDelegate(*arguments: object) -> object:
        if not lossCalls and isLossPoint(*arguments):
            lossCalls.append(arguments)
            TerminateWindowHolder(engine)
        return originalFunction(*arguments)

    monkeypatch.setattr(module, functionName, TerminateThenDelegate)


def IsAnyCall(*arguments: object) -> bool:
    return True


def IsPartFile(directory: object, name: str, expectedSha256: str) -> bool:
    return name.endswith(gdg.PART_SUFFIX)


def IsFinalFile(directory: object, name: str, expectedSha256: str) -> bool:
    return not IsPartFile(directory, name, expectedSha256)


GENERATION_LOSS_POINTS = (
    pytest.param(PUBLISH_RUN_GENERATIONS_NAME, IsAnyCall, id="before-publication"),
    pytest.param(HASH_MATCHES_NAME, IsFinalFile, id="final-file-hash"),
    pytest.param(HASH_MATCHES_NAME, IsPartFile, id="part-file-hash"),
)
STATEMENT_LOSS_POINTS = (
    pytest.param(creastmt.STATEMENT_FILE_NAME, id="ps-part-fsync"),
    pytest.param(creastmt.HTML_FILE_NAME, id="html-part-fsync"),
)


def DescribesFile(descriptor: int, path: Path) -> bool:
    """Return whether the open descriptor is the file at path; False when path is missing."""
    try:
        return os.path.samestat(os.fstat(descriptor), os.stat(path))
    except FileNotFoundError:
        return False


def AddStatementTransactions(engine: Engine, cardNums: Sequence[str]) -> None:
    """Insert one transaction for each card, so the next CREASTMT writes other statements."""
    spec = SyntheticTransactionSpec(
        cardNums=cardNums, perCard=1, amount=STATEMENT_TRANSACTION_AMOUNT
    )
    InsertSyntheticTransactions(engine, spec)


def StatementDirectoryState(statementsDir: Path) -> dict[str, tuple[int, bytes]]:
    """Return every file in statementsDir by name, with its inode and bytes, so a rename shows."""
    return {path.name: (path.stat().st_ino, path.read_bytes()) for path in statementsDir.iterdir()}


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


@pytest.mark.parametrize("commitWrite", COMMIT_PATTERNS)
def test_job_commit_after_guard_session_loss_is_refused_and_window_never_retaken(
    batchEnvironment: BatchEnvironment, jobEngine: Engine, commitWrite: Callable[[Engine], None]
) -> None:
    """Ending only the guard session refuses every later commit of the fenced job engine.

    A commit before the loss is admitted. After it, the write is rolled back, a second attempt,
    on the session the first one kept, is refused too, and no session holds the window again.
    """
    with BatchWindow(LoadBatchSettings(), lockTimeout=SHORT_LOCK_TIMEOUT, jobEngine=jobEngine):
        CommitNothing(jobEngine)
        TerminateWindowHolder(batchEnvironment.engine)
        with pytest.raises(BatchWindowLostError):
            commitWrite(jobEngine)
        with pytest.raises(BatchWindowLostError):
            commitWrite(jobEngine)
        assert not IsBatchWindowHeld(batchEnvironment.engine)
    assert TakeSeedSnapshot(batchEnvironment.engine) == batchEnvironment.snapshot
    AssertWindowFree(batchEnvironment.engine)


def test_job_session_opened_after_guard_session_loss_is_refused(
    batchEnvironment: BatchEnvironment, jobEngine: Engine
) -> None:
    """A job connection first opened once the guard session has ended is closed and refused.

    The refused session keeps no lock: nothing holds BATCH_JOB_SESSION_LOCK, and the window is
    free for the next command.
    """
    with BatchWindow(LoadBatchSettings(), lockTimeout=SHORT_LOCK_TIMEOUT, jobEngine=jobEngine):
        TerminateWindowHolder(batchEnvironment.engine)
        with pytest.raises(BatchWindowLostError):
            jobEngine.connect()
        assert CountJobSessionLocks(batchEnvironment.engine) == 0
    AssertWindowFree(batchEnvironment.engine)


def test_lost_window_holds_off_the_next_command_until_the_paused_run_stops(
    batchEnvironment: BatchEnvironment,
    backgroundRuns: list[subprocess.Popen],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """POSTTRAN paused between its units loses its guard session, and then resumes.

    The window is free, yet a second command waits for POSTTRAN's live job sessions and exits
    12 with BATCH WINDOW NOT AVAILABLE, leaving the RUNNING row and the DALYREJS .part alone.
    Resumed, POSTTRAN stops at its next commit with exit 12 and BATCH WINDOW LOST, posting
    nothing more, and the next command classifies its run INTERRUPTED.
    """
    engine = batchEnvironment.engine
    logPath = tmp_path / POSTTRAN_LOG_NAME
    pausedAccount = SecondPostingAccount(engine)
    with HeldAccountLock(engine, pausedAccount) as releaseAccountLock:
        process = StartPausedPostTran(engine, backgroundRuns, logPath)
        stateAtPause = PostingState(engine, pausedAccount)
        TerminateWindowHolder(engine)
        ShortenLockTimeout(monkeypatch)
        AssertWindowRefused(InvokeCli(LOAD_TRAN_TYPES_ARGUMENTS))
        AssertLiveRunUntouched(batchEnvironment)
        releaseAccountLock()
        assert process.wait(timeout=CLI_TIMEOUT_SECONDS) == EXIT_WINDOW_REFUSED
    assert BATCH_WINDOW_LOST_MESSAGE in logPath.read_text()
    assert PostingState(engine, pausedAccount) == stateAtPause
    AssertStoppedRunClassifiedInterrupted(batchEnvironment)
    AssertWindowFree(engine)


@pytest.mark.parametrize(("functionName", "isLossPoint"), GENERATION_LOSS_POINTS)
def test_publication_after_guard_session_loss_renames_nothing(
    batchEnvironment: BatchEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    functionName: str,
    isLossPoint: Callable[..., bool],
) -> None:
    """A guard lost once TRANBKP has committed stops its publication before the rename.

    The loss comes as publication starts, or while it hashes the final file or the .part. The
    command exits 12 with BATCH WINDOW LOST, leaving the TRANSACT.BKUP generation COMMITTED
    with only its .part on disk, as a crash before the rename would; the next command's catalog
    recovery publishes it.
    """
    lossTarget = (gdg, functionName)
    LoseWindowOnFirstMatchingCall(monkeypatch, batchEnvironment.engine, lossTarget, isLossPoint)
    AssertWindowLost(InvokeCli(JOB_TRAN_BACKUP_ARGUMENTS))
    outputDir = batchEnvironment.outputDir
    assert BaseFileNames(outputDir, gdg.TRANSACT_BKUP, GENERATION_FILE_GLOB) == []
    assert len(BaseFileNames(outputDir, gdg.TRANSACT_BKUP, PART_FILE_GLOB)) == 1
    assert GenerationStates(batchEnvironment.engine, gdg.TRANSACT_BKUP) == [gdg.GDG_COMMITTED]
    outcome = InvokeCli(LOAD_TRAN_TYPES_ARGUMENTS)
    assert outcome.exitCode == EXIT_OK, outcome.output
    assert len(BaseFileNames(outputDir, gdg.TRANSACT_BKUP, GENERATION_FILE_GLOB)) == 1
    assert GenerationStates(batchEnvironment.engine, gdg.TRANSACT_BKUP) == [gdg.GDG_PUBLISHED]


@pytest.mark.parametrize("fileName", STATEMENT_LOSS_POINTS)
def test_statement_publication_after_guard_session_loss_changes_no_statement_file(
    batchEnvironment: BatchEnvironment, monkeypatch: pytest.MonkeyPatch, fileName: str
) -> None:
    """A guard lost while CREASTMT syncs fileName's .part stops the publication before it starts.

    The forced rerun exits 12 with BATCH WINDOW LOST. Though the tables now give other
    statements, the previous STATEMNT.PS and STATEMNT.HTML keep their inodes and bytes, and no
    .part, backup or marker is left beside them.
    """
    engine = batchEnvironment.engine
    cardNums = InsertSyntheticCards(engine, 1)
    AddStatementTransactions(engine, cardNums)
    outcome = InvokeCli(STATEMENT_JOB_ARGUMENTS)
    assert outcome.exitCode == EXIT_OK, outcome.output
    statementsDir = LoadBatchSettings().StatementsDir()
    previousState = StatementDirectoryState(statementsDir)
    AddStatementTransactions(engine, cardNums)
    partPath = creastmt.TemporaryPath(statementsDir, fileName)
    isLossPoint = functools.partial(DescribesFile, path=partPath)
    LoseWindowOnFirstMatchingCall(monkeypatch, engine, (os, FSYNC_NAME), isLossPoint)
    AssertWindowLost(InvokeCli([*STATEMENT_JOB_ARGUMENTS, FORCE_OPTION]))
    assert sorted(previousState) == [creastmt.HTML_FILE_NAME, creastmt.STATEMENT_FILE_NAME]
    assert StatementDirectoryState(statementsDir) == previousState
