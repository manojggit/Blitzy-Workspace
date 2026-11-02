"""Environment settings of the carddemo-batch CLI and its jobs.

Replaces: the DSN=AWS.M2.CARDDEMO.* dataset references of jcl/ACCTFILE.jcl, jcl/POSTTRAN.jcl,
jcl/INTCALC.jcl, jcl/CREASTMT.JCL, jcl/DUSRSECJ.jcl and jcl/TRANBKP.jcl. The input images
(ACCTDATA.PS, DALYTRAN.PS, USRSEC.PS and the other *.PS files) come from the EBCDIC and ASCII
data directories; the generation archives (DALYREJS, SYSTRAN, TRANSACT.BKUP,
TRANSACT.COMBINED) and the statements (STATEMNT.PS, STATEMNT.HTML) go under the output
directory. Specified in the Technical Specification, Sections 0.7.3 and 0.7.6.

BatchSettings reads APP_ENV, SYNC_DATABASE_URL, BCRYPT_ROUNDS, CARDDEMO_EBCDIC_DIR,
CARDDEMO_ASCII_DIR and CARDDEMO_OUTPUT_DIR, and nothing else, so batch runs with SECRET_KEY
unset. Field names are the environment-variable names, identical to backend/.env.example,
docker-compose.yml and backend/app/core/config.py. Values come from the process environment,
which overrides backend/.env (app.core.paths.ENV_FILE), which overrides the defaults below; when
that file is absent, as in containers, the environment alone applies. The env-file handling is
inherited from CommonSettings and is not repeated here.

Every directory method returns an absolute path: an absolute setting is used unchanged and a
relative one resolves under the repository root, never the working directory. The methods do
not create directories; the writers create what they need.
"""

from pathlib import Path

from app.core.config import CommonSettings
from app.core.paths import ResolveRepoPath
from app.seed.image_reader import ENCODING_ASCII, ENCODING_EBCDIC

GDG_SUBDIRECTORY = "gdg"
STATEMENTS_SUBDIRECTORY = "statements"


class BatchSettings(CommonSettings):
    """Settings of every batch command: the database URL, data directories and output root.

    Inherits APP_ENV, SYNC_DATABASE_URL (required), CARDDEMO_EBCDIC_DIR, BCRYPT_ROUNDS,
    IsProduction() and EbcdicDir() from CommonSettings.
    """

    CARDDEMO_ASCII_DIR: str = "data/ASCII"
    CARDDEMO_OUTPUT_DIR: str = "var/batch-output"

    def AsciiDir(self) -> Path:
        """Return the directory of the ASCII seed images (CARDDEMO_ASCII_DIR), absolute."""
        return ResolveRepoPath(self.CARDDEMO_ASCII_DIR)

    def ImageDir(self, encoding: str) -> Path:
        """Return the seed-image directory for a loader's --encoding value.

        ENCODING_EBCDIC gives EbcdicDir() and ENCODING_ASCII gives AsciiDir(). Any other value
        raises ValueError naming the accepted values.
        """
        if encoding == ENCODING_EBCDIC:
            return self.EbcdicDir()
        if encoding == ENCODING_ASCII:
            return self.AsciiDir()
        raise ValueError(
            f"Unsupported image encoding {encoding!r}: expected"
            f" {ENCODING_EBCDIC!r} or {ENCODING_ASCII!r}"
        )

    def OutputDir(self) -> Path:
        """Return the batch output root (CARDDEMO_OUTPUT_DIR), absolute."""
        return ResolveRepoPath(self.CARDDEMO_OUTPUT_DIR)

    def GdgDir(self) -> Path:
        """Return the generation-archive root, OutputDir()/gdg, holding <BASE>/G####V00.dat."""
        return self.OutputDir() / GDG_SUBDIRECTORY

    def StatementsDir(self) -> Path:
        """Return the statement directory, OutputDir()/statements (STATEMNT.PS, STATEMNT.HTML)."""
        return self.OutputDir() / STATEMENTS_SUBDIRECTORY


def LoadBatchSettings() -> BatchSettings:
    """Build BatchSettings from the current environment and backend/.env.

    Every call builds a new instance, so each CLI command and each test sees the environment
    as it stands. Raises pydantic.ValidationError when a value is missing or invalid, for
    example an unset SYNC_DATABASE_URL; batch/cli.py turns that into exit status 12.
    """
    return BatchSettings()
