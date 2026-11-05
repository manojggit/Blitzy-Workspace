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
that file is absent, as in containers, the environment alone applies. Batch imports no backend
configuration (app.core.config): the file location and path resolution come from
app.core.paths (ENV_FILE, ResolveRepoPath), and the settings class is declared here.

Every directory method returns an absolute path: an absolute setting is used unchanged and a
relative one resolves under the repository root, never the working directory. The methods do
not create directories; the writers create what they need.
"""

from pathlib import Path
from typing import Literal

from app.core.paths import ENV_FILE, ResolveRepoPath
from app.seed.image_reader import ENCODING_ASCII, ENCODING_EBCDIC
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

GDG_SUBDIRECTORY = "gdg"
STATEMENTS_SUBDIRECTORY = "statements"


class BatchSettings(BaseSettings):
    """Settings of every batch command: the database URL, data directories and output root.

    Replaces the values every batch job took from its JCL: the runtime mode, the sync database
    connection that stands in for the VSAM catalog, the EBCDIC and ASCII seed-image
    directories, the output root of the generation archives and statements, and the cost of
    the password hashes that replace the plaintext USRSEC passwords jcl/DUSRSECJ.jcl loaded.
    Building it raises pydantic.ValidationError when SYNC_DATABASE_URL is unset or a value is
    invalid; it declares no SECRET_KEY, so it never needs one.
    """

    # extra="ignore": the backend's lines in the shared backend/.env, such as SECRET_KEY,
    # DATABASE_URL or TEST_*, are harmless. env_ignore_empty: an empty value such as
    # "CARDDEMO_OUTPUT_DIR=" in the process environment or in backend/.env is skipped like an
    # unset one, so the next lower-priority source with a nonempty value supplies the setting.
    # hide_input_in_errors: a ValidationError never echoes a rejected value or the database
    # URL with its password.
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
        case_sensitive=True,
        hide_input_in_errors=True,
    )

    APP_ENV: Literal["local", "test", "production"] = "local"
    # repr=False and exclude=True keep the database password out of repr(), str(), log records
    # and model_dump output.
    SYNC_DATABASE_URL: str = Field(repr=False, exclude=True)
    CARDDEMO_EBCDIC_DIR: str = "data/EBCDIC"
    BCRYPT_ROUNDS: int = Field(default=12, ge=4, le=31)
    CARDDEMO_ASCII_DIR: str = "data/ASCII"
    CARDDEMO_OUTPUT_DIR: str = "var/batch-output"

    def IsProduction(self) -> bool:
        return self.APP_ENV == "production"

    def EbcdicDir(self) -> Path:
        """Return the directory of the EBCDIC seed images (CARDDEMO_EBCDIC_DIR), absolute."""
        return ResolveRepoPath(self.CARDDEMO_EBCDIC_DIR)

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
