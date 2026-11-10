"""Repository-root resolution of data paths and the backend environment file.

Replaces: the JCL DSN= dataset references (AWS.M2.CARDDEMO.*, for example
AWS.M2.CARDDEMO.ACCTDATA.PS in jcl/ACCTFILE.jcl) and the CICS FILE DSNAME bindings in
csd/CARDDEMO.CSD. A relative seed-image or batch-output path resolves under the repository
root, an absolute one is kept unchanged, and the environment file is backend/.env under that
root (ENV_FILE). Specified in the Technical Specification, Section 0.7.3.

None of these depends on the working directory, so the backend, Alembic, the batch CLI, the
tests and the containers agree whether they start from the repository root, backend/,
frontend/ or /srv/backend.

This module imports only the standard library and the app package marker, and reads no
environment variable or setting, so batch can use it without SECRET_KEY.
"""

from pathlib import Path

import app

# app.__file__ is <root>/backend/app/__init__.py. The editable install keeps the source
# tree in place, so this is the checkout root, and /srv in the backend image.
REPO_ROOT: Path = Path(app.__file__).resolve().parents[2]

# Read by the pydantic-settings classes, which ignore the file when it is absent (as in
# containers), so its existence is deliberately not checked here.
ENV_FILE: Path = REPO_ROOT / "backend" / ".env"


def ResolveRepoPath(value: str | Path) -> Path:
    """Return an absolute value unchanged, and a relative one joined to REPO_ROOT."""
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return REPO_ROOT / candidate
