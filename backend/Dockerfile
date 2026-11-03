# CardDemo backend image: the FastAPI backend (package app) and the carddemo-batch CLI (package
# batch), both installed editable from /srv/backend and /srv/batch. docker-compose.yml builds it
# once for two services of the `full` profile:
#   - migrator overrides the command with `alembic upgrade head` and runs once, after `db` is
#     healthy;
#   - backend serves the API with the default command below. The batch chain also runs in it:
#     `docker compose --profile full run --rm backend carddemo-batch run-all`.
#
# The build context is the repository root, filtered by the root .dockerignore down to backend/
# and batch/, so every COPY source below is a repository-relative path. backend/.env is never
# copied: SECRET_KEY, the database URLs and APP_ENV reach the container only from the Compose
# environment at run time, and the settings classes read the environment alone when the file
# is absent.
#
# Build from the repository root:
#
#     docker build -f backend/Dockerfile -t carddemo-backend .

# asyncpg, psycopg2-binary, bcrypt, cryptography and reportlab all ship manylinux wheels for
# CPython 3.13, so no compiler or other OS package is installed.
FROM python:3.13-slim

# Unbuffered output, because the backend and the batch CLI log to stdout. No bytecode files or
# pip cache are written into the layers. pip installs into the image's own interpreter as root
# by design, so its root-user warning is silenced.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /srv

# Dependency layer: the pinned runtime requirements only, copied ahead of the sources so a
# source change reuses this layer. requirements-dev.txt is left out, because no test runs in the
# image.
COPY backend/requirements.txt backend/requirements.txt
COPY batch/requirements.txt batch/requirements.txt
RUN pip install -r backend/requirements.txt -r batch/requirements.txt

# The editable installs register carddemo-backend (package app) and carddemo-batch (package
# batch, console script carddemo-batch) while keeping the sources in place, so
# app.core.paths.REPO_ROOT resolves to /srv. pip check fails the build on a broken requirement
# set. /srv/var/batch-output is the default CARDDEMO_OUTPUT_DIR, which Compose mounts over.
COPY backend/ /srv/backend/
COPY batch/ /srv/batch/
RUN pip install -e /srv/backend -e /srv/batch \
    && pip check \
    && mkdir -p /srv/var/batch-output

# alembic.ini is here, so the migrator's `alembic upgrade head` finds it.
WORKDIR /srv/backend

# Runs as root: this is a local development image, and root can write the bind-mounted
# ./var/batch-output whatever its owner on the host.
#
# No HEALTHCHECK instruction: the Compose backend service's health check is authoritative. The
# image has no curl, so that check calls GET /health/ready through Python's urllib:
#     python -c "import urllib.request; urllib.request.urlopen(
#         'http://127.0.0.1:8000/health/ready', timeout=5)"
EXPOSE 8000

# Binds every interface inside the container; Compose publishes the port on 127.0.0.1 only.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
