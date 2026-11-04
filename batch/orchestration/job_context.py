"""Replaces: JCL condition codes (COND=(4,LT), RC 0/4/12/16), EXEC PARM= (INTCALC
PARM='2022071800') and the job step environment.

The contract between the chain and every batch runner: RunLoader(context) in batch/loaders/*
and RunJob(context) in batch/jobs/* take one JobContext and either return a JobResult or raise
JobFailure. Exit statuses stand in for the step condition codes: the chain continues after
EXIT_OK or EXIT_REJECTS and stops at CHAIN_STOP_THRESHOLD or above, as COND=(4,LT) does.
docs/batch.md lists the statuses each job returns.

This module is a leaf. At runtime it imports only the standard library and the encoding values
of app.seed.image_reader, so every loader and job can import it without a cycle, and it reads
no setting or environment variable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from app.seed.image_reader import ENCODING_ASCII, ENCODING_EBCDIC

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from batch.config import BatchSettings
    from batch.orchestration.clock import Clock

EXIT_OK = 0
# POSTTRAN rejected at least one daily record: the legacy RETURN-CODE 4.
EXIT_REJECTS = 4
# Missing or malformed input, a refused command or rerun, a COMBTRAN duplicate key, or the
# batch window not available or lost.
EXIT_FAILURE = 12
# Abend: POSTTRAN or INTCALC abend 999, no DEFAULT disclosure group, or the CREASTMT statement
# table capacity exceeded.
EXIT_ABEND = 16
# COND=(4,LT) bypasses the later steps once a return code exceeds 4; 8 is the next
# conventional condition code.
CHAIN_STOP_THRESHOLD = 8

# The INTCALC PARM [jcl/INTCALC.jcl:STEP15], which CBACT04C reads as the 10-character PARM-DATE.
DEFAULT_RUN_DATE = "2022071800"
RUN_DATE_LENGTH = 10

SUPPORTED_ENCODINGS = (ENCODING_EBCDIC, ENCODING_ASCII)
# TRANBKP runs twice in a cycle: occurrence 1 is chain step 6 and occurrence 2 is step 15.
SUPPORTED_OCCURRENCES = (1, 2)
# The refusal states the rule only, never the value: a mistyped card number is not repeated.
OCCURRENCE_RULE_MESSAGE = (
    f"--occurrence must be {SUPPORTED_OCCURRENCES[0]} or {SUPPORTED_OCCURRENCES[1]}"
)


def _CheckRunDate(runDate: str) -> None:
    """Refuse a --run-date that is not exactly RUN_DATE_LENGTH printable ASCII characters."""
    isPrintableAscii = isinstance(runDate, str) and runDate.isascii() and runDate.isprintable()
    if not isPrintableAscii or len(runDate) != RUN_DATE_LENGTH:
        raise ValueError(f"--run-date must be exactly {RUN_DATE_LENGTH} printable ASCII characters")


def _CheckEncoding(encoding: str) -> None:
    """Refuse an --encoding other than the two seed-image encodings."""
    if encoding not in SUPPORTED_ENCODINGS:
        raise ValueError(
            f"--encoding must be {ENCODING_EBCDIC!r} or {ENCODING_ASCII!r}, got {encoding!r}"
        )


def _CheckOccurrence(occurrence: int | None) -> None:
    """Refuse an --occurrence that names neither TRANBKP occurrence."""
    if occurrence is not None and occurrence not in SUPPORTED_OCCURRENCES:
        raise ValueError(OCCURRENCE_RULE_MESSAGE)


def _PathText(path: Path | None) -> str | None:
    """Return a path as JSON text, or None when the parameter was not given."""
    if path is None:
        return None
    return str(path)


@dataclass(frozen=True)
class JobParameters:
    """The command parameters of one run, validated where they enter from the CLI.

    Paths are kept as given: BatchSettings and the loaders resolve them through
    app.core.paths.ResolveRepoPath.
    """

    runDate: str = DEFAULT_RUN_DATE
    encoding: str = ENCODING_EBCDIC
    sourceDir: Path | None = None
    dailyFile: Path | None = None
    occurrence: int | None = None

    def __post_init__(self) -> None:
        _CheckRunDate(self.runDate)
        _CheckEncoding(self.encoding)
        _CheckOccurrence(self.occurrence)

    def AsLedgerParams(self) -> dict[str, str | int | None]:
        """Return the parameters as the batch.job_runs.params JSON, with snake_case keys."""
        return {
            "run_date": self.runDate,
            "encoding": self.encoding,
            "source_dir": _PathText(self.sourceDir),
            "daily_file": _PathText(self.dailyFile),
            "occurrence": self.occurrence,
        }


@dataclass(frozen=True)
class JobContext:
    """What every runner receives: engine, settings, clock, parameters and run identity.

    engine is the pooled job engine from batch.db.BuildEngine, never the batch-window engine.
    cycleId, stepNo and runId identify the batch.job_runs row once the chain has started the
    run; they stay None for a runner called directly, as unit tests do.
    """

    engine: Engine
    settings: BatchSettings
    clock: Clock
    parameters: JobParameters = field(default_factory=JobParameters)
    cycleId: int | None = None
    stepNo: int | None = None
    runId: int | None = None

    def ForRun(self, cycleId: int | None, stepNo: int | None, runId: int) -> JobContext:
        """Return a copy bound to a started run; a load with no OPEN cycle has no cycle or step."""
        return replace(self, cycleId=cycleId, stepNo=stepNo, runId=runId)

    def IsLedgered(self) -> bool:
        """Return whether a batch.job_runs row records this run."""
        return self.runId is not None


@dataclass(frozen=True)
class JobResult:
    """The outcome of a runner that completed: EXIT_OK or EXIT_REJECTS, and its record counts.

    counts is stored as batch.job_runs.counts, for example {"rows_loaded": 50} for a loader.
    A status at or above CHAIN_STOP_THRESHOLD is raised as JobFailure, never returned.
    """

    exitCode: int = EXIT_OK
    counts: dict[str, int] = field(default_factory=dict)


class JobFailure(Exception):
    """A failure that stops the chain, carrying its exit status: EXIT_FAILURE or EXIT_ABEND.

    The chain records the run as FAILED with exitCode; message is the text the CLI prints.
    """

    def __init__(self, exitCode: int, message: str) -> None:
        super().__init__(message)
        self.exitCode = exitCode
        self.message = message
