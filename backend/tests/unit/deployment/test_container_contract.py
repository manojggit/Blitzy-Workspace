"""Static contract tests for the container images and the Compose stack of the local run.

The files under test are docker-compose.yml, backend/Dockerfile with the root .dockerignore, and
frontend/Dockerfile with frontend/.dockerignore, as specified in the Technical Specification,
Sections 0.8.1, 0.8.5 and 0.11.1. Each file is read as text and parsed here, so the suite needs
no database, network or Docker engine. These tests pin:

- the build contexts: the root .dockerignore sends backend/ and batch/ and never an env file,
  virtual environment, cache, seed image or batch output, and frontend/.dockerignore drops the
  dependencies, the build output and every real env file but keeps the templates. Both are
  evaluated with the moby/patternmatcher rules BuildKit applies: the last matching pattern
  wins, and a pattern that matches a parent directory matches every path beneath it. Every
  COPY source of both Dockerfiles is sent;
- the backend image: python:3.13-slim at the major.minor of .python-version, the runtime
  requirements of backend and batch only, both packages installed editable side by side so
  app.core.paths.REPO_ROOT is their shared root, pip check after that install, the alembic.ini
  working directory, the uvicorn command on the port Compose publishes, and no secret in an
  ENV, ARG or COPY;
- the Compose wiring: db alone in the default profile, loopback-only ports, db credentials
  defaulting to the backend/.env.example values, database URLs on host db and its container
  port carrying the db's POSTGRES_USER, POSTGRES_PASSWORD and POSTGRES_DB expressions, which
  resolve to the backend/.env.example role, password and database, the read-only seed mount,
  the batch output mount, every variable a settings field, no SECRET_KEY, and the start order
  db healthy, migrator completed, backend healthy, frontend;
- the frontend image: NEXT_PUBLIC_API_URL declared as a build argument before npm run build,
  and set by Compose to the backend's published origin, the frontend/.env.local.example value.

The expected credentials are read at run time from backend/.env.example and never written
here, and SECRET_KEY is generated with the secrets module. Building and running the images
(docker build, docker compose up, the carddemo-batch CLI inside the backend image) is smoke
verification that belongs to the integration and QA lane, not to this suite. PyYAML is not a
direct requirement: the pinned schemathesis 4.33.0 of backend/requirements-dev.txt installs it
(Requires-Dist: pyyaml).
"""

import json
import posixpath
import re
import secrets
import shlex
from dataclasses import dataclass
from urllib.parse import urlsplit

import pytest
import yaml
from batch.config import BatchSettings

from app.api.health import ROUTER as HEALTH_ROUTER
from app.core.config import IsBrowserOrigin, MigrationSettings, ParseDatabaseUrl, Settings
from app.core.paths import REPO_ROOT

# --- Files under test ----------------------------------------------------------------------------
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
FRONTEND_CONTEXT = REPO_ROOT / "frontend"
BACKEND_DOCKERFILE_PATH = REPO_ROOT / "backend" / "Dockerfile"
FRONTEND_DOCKERFILE_PATH = FRONTEND_CONTEXT / "Dockerfile"
BACKEND_ENV_EXAMPLE_PATH = REPO_ROOT / "backend" / ".env.example"
FRONTEND_ENV_EXAMPLE_PATH = FRONTEND_CONTEXT / ".env.local.example"
PYTHON_VERSION_PATH = REPO_ROOT / ".python-version"
ALEMBIC_INI_PATH = REPO_ROOT / "backend" / "alembic.ini"
ROOT_DOCKERIGNORE = ".dockerignore"
FRONTEND_DOCKERIGNORE = "frontend/.dockerignore"
# Image name: (Dockerfile, build context). Each context's own .dockerignore filters it.
DOCKER_BUILDS = {
    "backend": (BACKEND_DOCKERFILE_PATH, REPO_ROOT),
    "frontend": (FRONTEND_DOCKERFILE_PATH, FRONTEND_CONTEXT),
}

# --- Build contexts ------------------------------------------------------------------------------
ROOT_CONTEXT_SENT = (
    "backend/requirements.txt",
    "backend/pyproject.toml",
    "backend/alembic.ini",
    "backend/app/main.py",
    "backend/alembic/versions/0009_batch_gdg_catalog.py",
    "batch/requirements.txt",
    "batch/pyproject.toml",
    "batch/__init__.py",
    "batch/config.py",
)
ROOT_CONTEXT_EXCLUDED = (
    ".env",
    "backend/.env",
    "backend/.env.local",
    "batch/.env",
    "frontend/.env.local",
    "frontend/.env.e2e",
    "frontend/node_modules/next/package.json",
    "data/EBCDIC/AWS.M2.CARDDEMO.USRSEC.PS",
    "var/batch-output/statements/STATEMNT.PS",
    ".git/config",
    ".venv/bin/python",
    "backend/.venv/bin/python",
    "cbl/COSGN00C.cbl",
    "docker-compose.yml",
    "backend/app/__pycache__/main.cpython-313.pyc",
    "backend/carddemo_backend.egg-info/PKG-INFO",
    "backend/.pytest_cache/README.md",
    "backend/.ruff_cache/CACHEDIR.TAG",
    "backend/.hypothesis/unicode_data/15.1.0/charmap.json.gz",
    "batch/.hypothesis/constants/163b62afdb8d9345",
    "backend/.coverage",
)
# Paths relative to frontend/, the frontend build context.
FRONTEND_CONTEXT_SENT = (
    "package.json",
    "package-lock.json",
    "next.config.ts",
    "src/app/layout.tsx",
    "public/favicon.svg",
    ".env.local.example",
)
FRONTEND_CONTEXT_EXCLUDED = (
    ".env",
    ".env.local",
    ".env.e2e",
    "node_modules/next/package.json",
    ".next/standalone/server.js",
)
# moby/patternmatcher regex text of the single-character tokens; any other character is literal.
IGNORE_TOKEN_REGEX = {"*": "[^/]*", "?": "[^/]", "[": "[", "]": "]"}

# --- Dockerfiles ---------------------------------------------------------------------------------
INSTRUCTION_PATTERN = re.compile(r"(?P<keyword>\S+)\s*(?P<arguments>.*)", re.DOTALL)
COPY_FLAGS_PATTERN = re.compile(r"(?P<flags>(?:--\S+\s+)*)(?P<paths>.+)", re.DOTALL)
COPY_KEYWORDS = frozenset({"COPY", "ADD"})
DECLARATION_KEYWORDS = frozenset({"ENV", "ARG"})
COMMAND_SEPARATORS = frozenset({"&&", "||", ";", "|", "&"})
PIP_PROGRAMS = (("pip",), ("pip3",), ("python", "-m", "pip"), ("python3", "-m", "pip"))
REQUIREMENT_OPTIONS = frozenset({"-r", "--requirement"})
EDITABLE_OPTIONS = frozenset({"-e", "--editable"})
BACKEND_BASE_IMAGE = "python:3.13-slim"
RUNTIME_REQUIREMENTS = ["backend/requirements.txt", "batch/requirements.txt"]
EDITABLE_SOURCES = ["backend", "batch"]
DEVELOPMENT_REQUIREMENTS = "requirements-dev"
BACKEND_COMMAND = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
FRONTEND_BUILD_COMMAND = ["npm", "run", "build"]
FRONTEND_COMMAND = ["node", "server.js"]
API_URL_VARIABLE = "NEXT_PUBLIC_API_URL"
SECRET_VARIABLES = frozenset(
    {"SECRET_KEY", "DATABASE_URL", "SYNC_DATABASE_URL", "PGPASSWORD", "POSTGRES_PASSWORD"}
)
ENV_FILE_MARKER = ".env"

# --- Compose -------------------------------------------------------------------------------------
DB_SERVICE = "db"
MIGRATOR_SERVICE = "migrator"
BACKEND_SERVICE = "backend"
FRONTEND_SERVICE = "frontend"
COMPOSE_SERVICES = {DB_SERVICE, MIGRATOR_SERVICE, BACKEND_SERVICE, FRONTEND_SERVICE}
FULL_PROFILE_SERVICES = (MIGRATOR_SERVICE, BACKEND_SERVICE, FRONTEND_SERVICE)
BACKEND_IMAGE_SERVICES = (MIGRATOR_SERVICE, BACKEND_SERVICE)
PUBLISHING_SERVICES = {DB_SERVICE, BACKEND_SERVICE, FRONTEND_SERVICE}
FULL_PROFILE = ["full"]
LOOPBACK_HOST = "127.0.0.1"
POSTGRES_IMAGE = "postgres:17"
POSTGRES_DATA_VOLUME = "pgdata"
POSTGRES_DATA_DIR = "/var/lib/postgresql/data"
INITDB_DIR = "/docker-entrypoint-initdb.d/"
INIT_SCRIPT_SOURCE = "docker/postgres/init-test-db.sql"
BACKEND_ENV_FILE = ["backend/.env"]
MIGRATION_COMMAND = ["alembic", "upgrade", "head"]
READ_ONLY_MODE = "ro"
DATA_SOURCE = "data"
BATCH_OUTPUT_SOURCE = "var/batch-output"
BATCH_OUTPUT_TARGET = "/srv/var/batch-output"
READINESS_PATH = "/health/ready"
PROBE_URL_PATTERN = re.compile(r"https?://[^\s'\"]+")
# ${NAME:-default}, the Compose interpolation with a default for an unset or empty variable.
DEFAULTED_VARIABLE_PATTERN = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*):-(?P<default>[^}]*)\}"
)
# Compose variable of the db service, and the part of the backend/.env.example URL it matches.
POSTGRES_URL_PARTS = (
    ("POSTGRES_USER", "username"),
    ("POSTGRES_PASSWORD", "password"),
    ("POSTGRES_DB", "database"),
)
DATABASE_URL_DRIVERS = {
    "DATABASE_URL": "postgresql+asyncpg",
    "SYNC_DATABASE_URL": "postgresql+psycopg2",
}
SEED_DIRECTORIES = (
    ("CARDDEMO_EBCDIC_DIR", "/app/data/EBCDIC"),
    ("CARDDEMO_ASCII_DIR", "/app/data/ASCII"),
)
DEPENDENCY_ORDER = {
    MIGRATOR_SERVICE: {DB_SERVICE: "service_healthy"},
    BACKEND_SERVICE: {
        DB_SERVICE: "service_healthy",
        MIGRATOR_SERVICE: "service_completed_successfully",
    },
    FRONTEND_SERVICE: {BACKEND_SERVICE: "service_healthy"},
}
SETTINGS_CASES = (
    pytest.param(BACKEND_SERVICE, Settings, id="backend-Settings"),
    pytest.param(BACKEND_SERVICE, BatchSettings, id="backend-BatchSettings"),
    pytest.param(MIGRATOR_SERVICE, MigrationSettings, id="migrator-MigrationSettings"),
)
SECRET_KEY_BYTES = 32


@dataclass(frozen=True)
class BuildStep:
    """One Dockerfile instruction with the WORKDIR and the context COPY targets in force."""

    keyword: str
    arguments: str
    workdir: str
    copiedPaths: dict


# --- .dockerignore matcher -----------------------------------------------------------------------
def CleanIgnorePattern(pattern):
    """Return a pattern cleaned as moby/patternmatcher cleans it: normalized, no leading /."""
    cleanedPattern = posixpath.normpath(pattern)
    if len(cleanedPattern) > 1 and cleanedPattern.startswith("/"):
        return cleanedPattern.lstrip("/")
    return cleanedPattern


def TranslateIgnoreToken(pattern, position):
    """Return the regex text of the pattern token at position and the token's length."""
    if pattern.startswith("**", position):
        tokenLength = 3 if pattern.startswith("**/", position) else 2
        if position + tokenLength >= len(pattern):
            return ".*", tokenLength
        return "(.*/)?", tokenLength
    character = pattern[position]
    if character == "\\" and position + 1 < len(pattern):
        return re.escape(pattern[position + 1]), 2
    return IGNORE_TOKEN_REGEX.get(character, re.escape(character)), 1


def CompileIgnorePattern(cleanedPattern):
    """Return the anchored regex moby/patternmatcher builds for one cleaned pattern."""
    regexParts = []
    position = 0
    while position < len(cleanedPattern):
        regexText, tokenLength = TranslateIgnoreToken(cleanedPattern, position)
        regexParts.append(regexText)
        position += tokenLength
    return re.compile("".join(regexParts))


def ReadIgnoreRules(ignorePath):
    """Return the (isException, regex) rules of a .dockerignore file, in file order."""
    ignoreRules = []
    for rawLine in ignorePath.read_text(encoding="utf-8").splitlines():
        if rawLine.startswith("#"):
            continue
        pattern = rawLine.strip()
        isException = pattern.startswith("!")
        if isException:
            pattern = pattern[1:].strip()
        if pattern:
            ignoreRules.append((isException, CompileIgnorePattern(CleanIgnorePattern(pattern))))
    return ignoreRules


def IsExcluded(contextPath, ignoreRules):
    """Return True when the last rule matching contextPath or a parent directory excludes it."""
    pathParts = posixpath.normpath(contextPath).split("/")
    candidatePaths = ["/".join(pathParts[:count]) for count in range(1, len(pathParts) + 1)]
    isExcluded = False
    for isException, patternRegex in ignoreRules:
        if any(patternRegex.fullmatch(candidatePath) for candidatePath in candidatePaths):
            isExcluded = not isException
    return isExcluded


# --- Dockerfile parser ---------------------------------------------------------------------------
def SplitInstruction(instructionText):
    """Return (KEYWORD, arguments) of one Dockerfile instruction with continuations joined."""
    instructionParts = INSTRUCTION_PATTERN.fullmatch(instructionText.strip())
    return instructionParts["keyword"].upper(), instructionParts["arguments"].strip()


def ReadInstructions(dockerfilePath):
    """Return the (KEYWORD, arguments) pairs of a Dockerfile, comment and blank lines dropped."""
    dockerInstructions = []
    pendingText = ""
    for rawLine in dockerfilePath.read_text(encoding="utf-8").splitlines():
        strippedLine = rawLine.strip()
        if not strippedLine or strippedLine.startswith("#"):
            continue
        if strippedLine.endswith("\\"):
            pendingText += strippedLine[:-1] + " "
            continue
        dockerInstructions.append(SplitInstruction(pendingText + strippedLine))
        pendingText = ""
    if pendingText.strip():
        dockerInstructions.append(SplitInstruction(pendingText))
    return dockerInstructions


def SplitCopyArguments(arguments):
    """Return the flags, the sources and the destination of COPY or ADD arguments."""
    copyParts = COPY_FLAGS_PATTERN.fullmatch(arguments)
    pathText = copyParts["paths"].strip()
    copyPaths = json.loads(pathText) if pathText.startswith("[") else pathText.split()
    return copyParts["flags"].split(), copyPaths[:-1], copyPaths[-1]


def IsStageCopy(copyFlags):
    """Return True when COPY flags read from another stage or image (--from=) not the context."""
    return any(copyFlag.startswith("--from=") for copyFlag in copyFlags)


def MapCopyTargets(arguments, workdir, contextRoot):
    """Return {image path: context source} of a context COPY or ADD; {} for a --from= copy."""
    copyFlags, copySources, destination = SplitCopyArguments(arguments)
    if IsStageCopy(copyFlags):
        return {}
    copyTargets = {}
    for source in copySources:
        cleanSource = posixpath.normpath(source)
        targetPath = posixpath.join(workdir, destination)
        if destination.endswith("/") and not (contextRoot / cleanSource).is_dir():
            targetPath = posixpath.join(targetPath, posixpath.basename(cleanSource))
        copyTargets[posixpath.normpath(targetPath)] = cleanSource
    return copyTargets


def WalkStage(stageInstructions, contextRoot):
    """Return the BuildSteps of one stage, tracking its WORKDIR and its context COPY targets."""
    workdir = "/"
    copiedPaths = {}
    buildSteps = []
    for keyword, arguments in stageInstructions:
        if keyword == "WORKDIR":
            workdir = posixpath.normpath(posixpath.join(workdir, arguments))
        if keyword in COPY_KEYWORDS:
            copiedPaths = {**copiedPaths, **MapCopyTargets(arguments, workdir, contextRoot)}
        buildSteps.append(BuildStep(keyword, arguments, workdir, copiedPaths))
    return buildSteps


def ReadStages(dockerfilePath, contextRoot):
    """Return the build stages of a Dockerfile, each the BuildSteps from its FROM onward."""
    groupedStages = []
    for keyword, arguments in ReadInstructions(dockerfilePath):
        if keyword == "FROM":
            groupedStages.append([])
        if groupedStages:
            groupedStages[-1].append((keyword, arguments))
    return [WalkStage(stageInstructions, contextRoot) for stageInstructions in groupedStages]


def AllSteps(dockerStages):
    """Return the BuildSteps of every stage, in file order."""
    return [buildStep for stageSteps in dockerStages for buildStep in stageSteps]


def MapToSource(imagePath, copiedPaths):
    """Return the context-relative source an image path was copied from, or None."""
    cleanPath = posixpath.normpath(imagePath)
    for targetPath in sorted(copiedPaths, key=len, reverse=True):
        if cleanPath == targetPath or cleanPath.startswith(targetPath.rstrip("/") + "/"):
            relativePart = posixpath.relpath(cleanPath, targetPath)
            return posixpath.normpath(posixpath.join(copiedPaths[targetPath], relativePart))
    return None


def SplitShellCommands(commandText):
    """Return the word lists of the simple commands a RUN line chains with &&, ||, ; or |."""
    if commandText.startswith("["):
        return [json.loads(commandText)]
    shellLexer = shlex.shlex(commandText, posix=True, punctuation_chars=True)
    shellLexer.whitespace_split = True
    shellCommands = [[]]
    for word in shellLexer:
        if word in COMMAND_SEPARATORS:
            shellCommands.append([])
        else:
            shellCommands[-1].append(word)
    return [commandWords for commandWords in shellCommands if commandWords]


def RunCommands(buildSteps):
    """Return (step, words) for every simple command of every RUN step, in build order."""
    return [
        (buildStep, commandWords)
        for buildStep in buildSteps
        if buildStep.keyword == "RUN"
        for commandWords in SplitShellCommands(buildStep.arguments)
    ]


def IsPipCommand(commandWords, subcommand):
    """Return True when commandWords run the pip subcommand, as pip, pip3 or python -m pip."""
    return any(
        tuple(commandWords[: len(program) + 1]) == (*program, subcommand)
        for program in PIP_PROGRAMS
    )


def OptionValues(commandWords, optionNames):
    """Return the values commandWords give any of optionNames, as NAME VALUE or NAME=VALUE."""
    optionValues = []
    for position, word in enumerate(commandWords):
        optionName, separator, inlineValue = word.partition("=")
        if word in optionNames and position + 1 < len(commandWords):
            optionValues.append(commandWords[position + 1])
        elif separator and optionName in optionNames:
            optionValues.append(inlineValue)
    return optionValues


def PipInstallTargets(buildSteps, optionNames):
    """Return {image path: context source} of every pip install value of optionNames."""
    installTargets = {}
    for buildStep, commandWords in RunCommands(buildSteps):
        if not IsPipCommand(commandWords, "install"):
            continue
        for optionValue in OptionValues(commandWords, optionNames):
            imagePath = posixpath.normpath(posixpath.join(buildStep.workdir, optionValue))
            installTargets[imagePath] = MapToSource(imagePath, buildStep.copiedPaths)
    return installTargets


def StageImage(stageSteps):
    """Return the image a stage's FROM names, ignoring --platform and AS NAME."""
    fromWords = [word for word in stageSteps[0].arguments.split() if not word.startswith("--")]
    return fromWords[0]


def LastArguments(stageSteps, keyword):
    """Return the arguments of the stage's last instruction of keyword, '' when it has none."""
    keywordArguments = [step.arguments for step in stageSteps if step.keyword == keyword]
    return keywordArguments[-1] if keywordArguments else ""


def ExecForm(arguments):
    """Return a JSON exec-form argument list, or the text unchanged when it is shell form."""
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        return arguments


def DeclaredNames(buildStep):
    """Return the variable names an ENV or ARG step declares, as NAME=value or NAME value."""
    declarationWords = shlex.split(buildStep.arguments)
    if buildStep.keyword == "ENV" and "=" not in declarationWords[0]:
        return [declarationWords[0]]
    return [word.partition("=")[0] for word in declarationWords]


def CopySources(dockerStages, contextOnly):
    """Return the sources of every COPY and ADD; contextOnly drops the --from= stage copies."""
    copySources = []
    for buildStep in AllSteps(dockerStages):
        if buildStep.keyword not in COPY_KEYWORDS:
            continue
        copyFlags, stepSources, _ = SplitCopyArguments(buildStep.arguments)
        if not (contextOnly and IsStageCopy(copyFlags)):
            copySources.extend(stepSources)
    return copySources


def BuildRunPosition(stageSteps):
    """Return the index of the stage's RUN step that runs npm run build, or None."""
    for position, buildStep in enumerate(stageSteps):
        if buildStep.keyword != "RUN":
            continue
        if FRONTEND_BUILD_COMMAND in SplitShellCommands(buildStep.arguments):
            return position
    return None


# --- Compose and env-file readers ----------------------------------------------------------------
def ReadEnvAssignments(envPath):
    """Return the KEY=VALUE assignments of an env template, comments and blank lines skipped."""
    envAssignments = {}
    for rawLine in envPath.read_text(encoding="utf-8").splitlines():
        strippedLine = rawLine.strip()
        if not strippedLine or strippedLine.startswith("#") or "=" not in strippedLine:
            continue
        variableName, _, variableValue = strippedLine.partition("=")
        envAssignments[variableName.strip()] = variableValue.strip()
    return envAssignments


def ServiceEnvironment(service):
    """Return a service's environment as a dict, from the mapping or the list form."""
    environment = service.get("environment") or {}
    if isinstance(environment, list):
        return dict(entry.partition("=")[::2] for entry in environment)
    return environment


def BuildArguments(service):
    """Return a service's build arguments as a dict, from the mapping or the list form."""
    buildSettings = service.get("build")
    if not isinstance(buildSettings, dict):
        return {}
    buildArguments = buildSettings.get("args") or {}
    if isinstance(buildArguments, list):
        return dict(entry.partition("=")[::2] for entry in buildArguments)
    return buildArguments


def ParsePortMapping(portEntry):
    """Return (host IP, host port, container port) of a short-syntax Compose port mapping."""
    portParts = str(portEntry).partition("/")[0].rsplit(":", 2)
    return tuple([""] * (3 - len(portParts)) + portParts)


def ContainerPort(service):
    """Return the container port of a service's first port mapping."""
    return ParsePortMapping(service["ports"][0])[2]


def PublishedOrigin(service):
    """Return the http origin a service's first port mapping publishes on the host."""
    hostIp, hostPort, _ = ParsePortMapping(service["ports"][0])
    return f"http://{hostIp}:{hostPort}"


def ServiceVolumes(service):
    """Return (source, target, mode) of each short-syntax volume; mode is '' when absent."""
    volumeParts = [str(volumeEntry).split(":") for volumeEntry in service.get("volumes", [])]
    return [(parts[0], parts[1], ":".join(parts[2:])) for parts in volumeParts]


def MountOf(service, source):
    """Return the (target, mode options) of the service's single volume mounting source."""
    sourceMounts = [
        (target, mode.split(","))
        for volumeSource, target, mode in ServiceVolumes(service)
        if posixpath.normpath(volumeSource) == source
    ]
    assert len(sourceMounts) == 1, f"expected one mount of {source}"
    return sourceMounts[0]


def DependencyConditions(service):
    """Return {service: condition} of depends_on; the short list form means service_started."""
    dependsOn = service.get("depends_on") or {}
    if isinstance(dependsOn, list):
        return dict.fromkeys(dependsOn, "service_started")
    return {
        name: options.get("condition", "service_started") for name, options in dependsOn.items()
    }


def KnownVariableNames():
    """Return every name a backend-image service may set: a field of a settings class."""
    settingsFields = (Settings.model_fields, MigrationSettings.model_fields)
    return set().union(*settingsFields, BatchSettings.model_fields)


def ResolveUnsetVariables(composeText):
    """Return composeText with each ${NAME:-default} resolved as Compose does with NAME unset."""
    return DEFAULTED_VARIABLE_PATTERN.sub(r"\g<default>", composeText)


def DockerignoreCases(ignoreName, contextPaths, expectedExcluded):
    """Return one pytest.param per context path for the .dockerignore at ignoreName."""
    verdict = "excluded" if expectedExcluded else "sent"
    return [
        pytest.param(
            ignoreName, contextPath, expectedExcluded, id=f"{ignoreName}:{verdict}:{contextPath}"
        )
        for contextPath in contextPaths
    ]


DOCKERIGNORE_CASES = (
    *DockerignoreCases(ROOT_DOCKERIGNORE, ROOT_CONTEXT_SENT, False),
    *DockerignoreCases(ROOT_DOCKERIGNORE, ROOT_CONTEXT_EXCLUDED, True),
    *DockerignoreCases(FRONTEND_DOCKERIGNORE, FRONTEND_CONTEXT_SENT, False),
    *DockerignoreCases(FRONTEND_DOCKERIGNORE, FRONTEND_CONTEXT_EXCLUDED, True),
)


# --- Fixtures ------------------------------------------------------------------------------------
@pytest.fixture(name="composeFile", scope="module")
def ComposeFileFixture():
    """Return docker-compose.yml as yaml.safe_load reads it, merge keys resolved shallowly."""
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(name="composeServices", scope="module")
def ComposeServicesFixture(composeFile):
    """Return the services mapping of docker-compose.yml."""
    return composeFile["services"]


@pytest.fixture(name="backendStages", scope="module")
def BackendStagesFixture():
    """Return the build stages of backend/Dockerfile, built from the repository root."""
    return ReadStages(*DOCKER_BUILDS["backend"])


@pytest.fixture(name="frontendStages", scope="module")
def FrontendStagesFixture():
    """Return the build stages of frontend/Dockerfile, built from frontend/."""
    return ReadStages(*DOCKER_BUILDS["frontend"])


@pytest.fixture(name="exampleDatabaseUrl", scope="module")
def ExampleDatabaseUrlFixture():
    """Return the parsed SYNC_DATABASE_URL of backend/.env.example, the local credentials."""
    exampleUrl = ReadEnvAssignments(BACKEND_ENV_EXAMPLE_PATH)["SYNC_DATABASE_URL"]
    return ParseDatabaseUrl(exampleUrl, "SYNC_DATABASE_URL")


# --- Build contexts ------------------------------------------------------------------------------
@pytest.mark.parametrize(("ignoreName", "contextPath", "expectedExcluded"), DOCKERIGNORE_CASES)
def test_dockerignore_filters_build_context(ignoreName, contextPath, expectedExcluded):
    """Each .dockerignore sends the image sources and keeps secrets and local state out."""
    ignoreRules = ReadIgnoreRules(REPO_ROOT / ignoreName)
    assert IsExcluded(contextPath, ignoreRules) is expectedExcluded


@pytest.mark.parametrize("imageName", list(DOCKER_BUILDS))
def test_dockerignore_sends_every_copy_source(imageName):
    """Every context COPY source of a Dockerfile exists and its context's .dockerignore sends it."""
    dockerfilePath, contextRoot = DOCKER_BUILDS[imageName]
    # BuildKit prefers <Dockerfile>.dockerignore beside the Dockerfile to the context's file.
    assert not dockerfilePath.with_name(f"{dockerfilePath.name}.dockerignore").exists()
    ignoreRules = ReadIgnoreRules(contextRoot / ".dockerignore")
    copySources = CopySources(ReadStages(dockerfilePath, contextRoot), contextOnly=True)
    assert copySources
    assert [source for source in copySources if IsExcluded(source, ignoreRules)] == []
    assert all((contextRoot / source).exists() for source in copySources)


# --- Dockerfiles ---------------------------------------------------------------------------------
@pytest.mark.parametrize("imageName", list(DOCKER_BUILDS))
def test_dockerfile_declares_no_secret(imageName):
    """No ENV or ARG of a Dockerfile defines a secret or a database URL."""
    dockerStages = ReadStages(*DOCKER_BUILDS[imageName])
    declaredNames = {
        declaredName
        for buildStep in AllSteps(dockerStages)
        if buildStep.keyword in DECLARATION_KEYWORDS
        for declaredName in DeclaredNames(buildStep)
    }
    assert declaredNames.isdisjoint(SECRET_VARIABLES)


@pytest.mark.parametrize("imageName", list(DOCKER_BUILDS))
def test_dockerfile_copies_no_env_file(imageName):
    """No COPY or ADD source of a Dockerfile names an env file."""
    copySources = CopySources(ReadStages(*DOCKER_BUILDS[imageName]), contextOnly=False)
    assert [source for source in copySources if ENV_FILE_MARKER in source] == []


def test_backend_base_image_matches_python_version(backendStages):
    """The backend image starts from python:3.13-slim, the major.minor of .python-version."""
    baseImage = StageImage(backendStages[0])
    pinnedVersion = PYTHON_VERSION_PATH.read_text(encoding="utf-8").strip()
    majorMinor = ".".join(pinnedVersion.split(".")[:2])
    assert baseImage == BACKEND_BASE_IMAGE
    assert baseImage.partition(":")[2].removesuffix("-slim") == majorMinor


def test_backend_installs_runtime_requirements_only(backendStages):
    """pip install -r reads exactly the copied backend and batch runtime requirements."""
    requirementTargets = PipInstallTargets(AllSteps(backendStages), REQUIREMENT_OPTIONS)
    runArguments = [step.arguments for step in AllSteps(backendStages) if step.keyword == "RUN"]
    assert sorted(requirementTargets.values()) == RUNTIME_REQUIREMENTS
    assert all((REPO_ROOT / source).is_file() for source in RUNTIME_REQUIREMENTS)
    assert not any(DEVELOPMENT_REQUIREMENTS in arguments for arguments in runArguments)


def test_backend_installs_backend_and_batch_editable_side_by_side(backendStages):
    """backend and batch install editable under one root that mirrors the repository layout.

    app.core.paths.REPO_ROOT is three levels above backend/app/__init__.py, so in the image it is
    the shared parent of the two editable sources.
    """
    editableTargets = PipInstallTargets(AllSteps(backendStages), EDITABLE_OPTIONS)
    assert sorted(editableTargets.values()) == EDITABLE_SOURCES
    imageRoots = {posixpath.dirname(imagePath) for imagePath in editableTargets}
    assert len(imageRoots) == 1
    imageRoot = imageRoots.pop()
    for imagePath, source in editableTargets.items():
        assert imagePath == posixpath.join(imageRoot, source)


def test_backend_runs_pip_check_after_editable_install(backendStages):
    """pip check runs after the editable install, so a broken requirement set fails the build."""
    runWords = [commandWords for _, commandWords in RunCommands(AllSteps(backendStages))]
    editablePositions = [
        position
        for position, commandWords in enumerate(runWords)
        if IsPipCommand(commandWords, "install") and OptionValues(commandWords, EDITABLE_OPTIONS)
    ]
    checkPositions = [
        position
        for position, commandWords in enumerate(runWords)
        if IsPipCommand(commandWords, "check")
    ]
    assert editablePositions
    assert checkPositions
    assert max(checkPositions) > max(editablePositions)


def test_backend_final_workdir_holds_alembic_ini(backendStages):
    """The final WORKDIR is the copied backend/, whose alembic.ini the migrator's command finds."""
    finalStep = backendStages[-1][-1]
    assert MapToSource(finalStep.workdir, finalStep.copiedPaths) == "backend"
    assert ALEMBIC_INI_PATH.is_file()


def test_backend_command_serves_app_on_compose_port(backendStages, composeServices):
    """CMD runs uvicorn on the EXPOSEd port, the container port Compose publishes for backend."""
    finalStage = backendStages[-1]
    exposedPort = LastArguments(finalStage, "EXPOSE").partition("/")[0]
    assert ExecForm(LastArguments(finalStage, "CMD")) == BACKEND_COMMAND
    assert exposedPort == BACKEND_COMMAND[-1]
    assert exposedPort == ContainerPort(composeServices[BACKEND_SERVICE])


def test_frontend_build_stage_declares_api_url_before_build(frontendStages):
    """The stage that runs npm run build declares ARG NEXT_PUBLIC_API_URL before that RUN."""
    buildStages = [stage for stage in frontendStages if BuildRunPosition(stage) is not None]
    assert len(buildStages) == 1
    buildStage = buildStages[0]
    argumentPositions = [
        position
        for position, buildStep in enumerate(buildStage)
        if buildStep.keyword == "ARG" and API_URL_VARIABLE in DeclaredNames(buildStep)
    ]
    assert argumentPositions
    assert min(argumentPositions) < BuildRunPosition(buildStage)


def test_frontend_command_serves_standalone_on_compose_port(frontendStages, composeServices):
    """The final stage runs node server.js and EXPOSEs the container port Compose publishes."""
    finalStage = frontendStages[-1]
    exposedPort = LastArguments(finalStage, "EXPOSE").partition("/")[0]
    assert ExecForm(LastArguments(finalStage, "CMD")) == FRONTEND_COMMAND
    assert exposedPort == ContainerPort(composeServices[FRONTEND_SERVICE])


# --- Compose: services, profiles, ports and start order ------------------------------------------
def test_compose_runs_db_by_default_and_the_rest_under_full(composeServices):
    """db is the only default-profile service; migrator, backend and frontend are in full."""
    assert set(composeServices) == COMPOSE_SERVICES
    assert "profiles" not in composeServices[DB_SERVICE]
    for serviceName in FULL_PROFILE_SERVICES:
        assert composeServices[serviceName]["profiles"] == FULL_PROFILE


def test_compose_publishes_ports_on_loopback_only(composeServices):
    """db, backend and frontend publish ports, each bound to 127.0.0.1 only."""
    publishingServices = {name for name, service in composeServices.items() if service.get("ports")}
    portMappings = [
        ParsePortMapping(portEntry)
        for service in composeServices.values()
        for portEntry in service.get("ports", [])
    ]
    assert publishingServices == PUBLISHING_SERVICES
    assert [hostIp for hostIp, _, _ in portMappings if hostIp != LOOPBACK_HOST] == []


@pytest.mark.parametrize("serviceName", list(DEPENDENCY_ORDER))
def test_compose_start_order(composeServices, serviceName):
    """migrator waits for db healthy, backend for migrator success, frontend for backend."""
    assert DependencyConditions(composeServices[serviceName]) == DEPENDENCY_ORDER[serviceName]


# --- Compose: db ---------------------------------------------------------------------------------
def test_db_runs_postgres_17_on_pgdata(composeFile):
    """db runs postgres:17 with its data directory on the declared named volume pgdata."""
    dbService = composeFile["services"][DB_SERVICE]
    dataVolumes = [
        (source, target)
        for source, target, _ in ServiceVolumes(dbService)
        if target == POSTGRES_DATA_DIR
    ]
    assert dbService["image"] == POSTGRES_IMAGE
    assert dataVolumes == [(POSTGRES_DATA_VOLUME, POSTGRES_DATA_DIR)]
    assert POSTGRES_DATA_VOLUME in composeFile["volumes"]


@pytest.mark.parametrize(("variableName", "urlPart"), POSTGRES_URL_PARTS)
def test_db_credentials_default_to_env_example(
    composeServices, exampleDatabaseUrl, variableName, urlPart
):
    """POSTGRES_USER, POSTGRES_PASSWORD and POSTGRES_DB default to the .env.example URL's."""
    expression = ServiceEnvironment(composeServices[DB_SERVICE])[variableName]
    variableMatch = DEFAULTED_VARIABLE_PATTERN.fullmatch(expression)
    assert variableMatch is not None
    assert variableMatch["name"] == variableName
    assert variableMatch["default"] == getattr(exampleDatabaseUrl, urlPart)


def test_db_publishes_env_example_host_and_port(composeServices, exampleDatabaseUrl):
    """db publishes PostgreSQL on the host and port the backend/.env.example URLs connect to."""
    hostIp, hostPort, _ = ParsePortMapping(composeServices[DB_SERVICE]["ports"][0])
    assert (hostIp, hostPort) == (exampleDatabaseUrl.host, str(exampleDatabaseUrl.port))


def test_db_mounts_test_database_init_script_read_only(composeServices):
    """init-test-db.sql exists and is mounted read-only into /docker-entrypoint-initdb.d/."""
    mountTarget, mountModes = MountOf(composeServices[DB_SERVICE], INIT_SCRIPT_SOURCE)
    assert mountTarget.startswith(INITDB_DIR)
    assert READ_ONLY_MODE in mountModes
    assert (REPO_ROOT / INIT_SCRIPT_SOURCE).is_file()


def test_db_healthcheck_runs_pg_isready_over_tcp(composeServices):
    """pg_isready probes 127.0.0.1 over TCP, so the socket-only init server is not ready."""
    healthTest = composeServices[DB_SERVICE]["healthcheck"]["test"]
    assert healthTest[0] == "CMD-SHELL"
    checkWords = shlex.split(healthTest[1])
    assert checkWords[0] == "pg_isready"
    assert OptionValues(checkWords, {"-h", "--host"}) == [LOOPBACK_HOST]
    assert "POSTGRES_USER" in "".join(OptionValues(checkWords, {"-U", "--username"}))
    assert "POSTGRES_DB" in "".join(OptionValues(checkWords, {"-d", "--dbname"}))


# --- Compose: migrator and backend ---------------------------------------------------------------
def test_migrator_builds_backend_image_from_repository_root(composeServices):
    """migrator builds backend/Dockerfile with the repository root as its context."""
    buildSettings = composeServices[MIGRATOR_SERVICE]["build"]
    assert buildSettings == {"context": ".", "dockerfile": "backend/Dockerfile"}
    dockerfilePath = (REPO_ROOT / buildSettings["context"] / buildSettings["dockerfile"]).resolve()
    assert dockerfilePath == BACKEND_DOCKERFILE_PATH.resolve()
    assert dockerfilePath.is_file()


def test_migrator_runs_alembic_upgrade_once(composeServices):
    """migrator runs alembic upgrade head and is never restarted."""
    migrator = composeServices[MIGRATOR_SERVICE]
    assert migrator["command"] == MIGRATION_COMMAND
    assert migrator["restart"] == "no"


def test_backend_reuses_migrator_image(composeServices):
    """backend runs the image migrator builds and has no build of its own."""
    backend = composeServices[BACKEND_SERVICE]
    assert backend["image"] == composeServices[MIGRATOR_SERVICE]["image"]
    assert "build" not in backend


@pytest.mark.parametrize("serviceName", BACKEND_IMAGE_SERVICES)
def test_backend_image_service_reads_backend_env_file(composeServices, serviceName):
    """migrator and backend read backend/.env, whose template backend/.env.example exists."""
    envFile = composeServices[serviceName]["env_file"]
    assert (envFile if isinstance(envFile, list) else [envFile]) == BACKEND_ENV_FILE
    assert BACKEND_ENV_EXAMPLE_PATH.is_file()


@pytest.mark.parametrize("serviceName", BACKEND_IMAGE_SERVICES)
@pytest.mark.parametrize("variableName", sorted(DATABASE_URL_DRIVERS))
def test_database_urls_carry_db_credentials_on_host_db(
    composeServices, exampleDatabaseUrl, serviceName, variableName
):
    """The URLs carry db's POSTGRES_* expressions on host db and default to .env.example's."""
    driverName = DATABASE_URL_DRIVERS[variableName]
    dbService = composeServices[DB_SERVICE]
    dbEnvironment = ServiceEnvironment(dbService)
    dbPort = ContainerPort(dbService)
    urlText = ServiceEnvironment(composeServices[serviceName])[variableName]
    assert urlText == (
        f"{driverName}://{dbEnvironment['POSTGRES_USER']}:{dbEnvironment['POSTGRES_PASSWORD']}"
        f"@{DB_SERVICE}:{dbPort}/{dbEnvironment['POSTGRES_DB']}"
    )
    databaseUrl = ParseDatabaseUrl(ResolveUnsetVariables(urlText), variableName)
    assert (databaseUrl.drivername, databaseUrl.host) == (driverName, DB_SERVICE)
    assert str(databaseUrl.port) == dbPort
    for _, urlPart in POSTGRES_URL_PARTS:
        assert getattr(databaseUrl, urlPart) == getattr(exampleDatabaseUrl, urlPart)


@pytest.mark.parametrize("serviceName", BACKEND_IMAGE_SERVICES)
@pytest.mark.parametrize(("variableName", "expectedPath"), SEED_DIRECTORIES)
def test_seed_directories_lie_on_read_only_data_mount(
    composeServices, serviceName, variableName, expectedPath
):
    """The seed directories are repository data/ subdirectories under the read-only ./data mount."""
    service = composeServices[serviceName]
    mountTarget, mountModes = MountOf(service, DATA_SOURCE)
    relativePart = posixpath.relpath(expectedPath, mountTarget)
    assert ServiceEnvironment(service)[variableName] == expectedPath
    assert READ_ONLY_MODE in mountModes
    assert not relativePart.startswith("..")
    assert (REPO_ROOT / DATA_SOURCE / relativePart).is_dir()


@pytest.mark.parametrize("serviceName", BACKEND_IMAGE_SERVICES)
def test_environment_keys_are_settings_fields(composeServices, serviceName):
    """Every variable migrator and backend set is a field of a settings class they build."""
    serviceEnvironment = ServiceEnvironment(composeServices[serviceName])
    assert set(serviceEnvironment) - KnownVariableNames() == set()


@pytest.mark.parametrize("serviceName", sorted(COMPOSE_SERVICES))
def test_no_service_receives_secret_key(composeServices, serviceName):
    """SECRET_KEY reaches no service from Compose: it comes from backend/.env only."""
    service = composeServices[serviceName]
    assert "SECRET_KEY" not in ServiceEnvironment(service)
    assert "SECRET_KEY" not in BuildArguments(service)


def test_backend_cors_origin_is_frontend_published_origin(composeServices):
    """BACKEND_CORS_ORIGINS is the frontend's published origin, the backend/.env.example value."""
    backendEnvironment = ServiceEnvironment(composeServices[BACKEND_SERVICE])
    frontendOrigin = PublishedOrigin(composeServices[FRONTEND_SERVICE])
    templateOrigin = ReadEnvAssignments(BACKEND_ENV_EXAMPLE_PATH)["BACKEND_CORS_ORIGINS"]
    assert backendEnvironment["BACKEND_CORS_ORIGINS"] == frontendOrigin
    assert frontendOrigin == templateOrigin


def test_backend_output_dir_is_writable_batch_output_mount(composeServices):
    """CARDDEMO_OUTPUT_DIR is the writable ./var/batch-output mount at /srv/var/batch-output."""
    backend = composeServices[BACKEND_SERVICE]
    mountTarget, mountModes = MountOf(backend, BATCH_OUTPUT_SOURCE)
    assert ServiceEnvironment(backend)["CARDDEMO_OUTPUT_DIR"] == mountTarget
    assert mountTarget == BATCH_OUTPUT_TARGET
    assert READ_ONLY_MODE not in mountModes


def test_backend_healthcheck_probes_readiness_on_container_port(composeServices):
    """The backend health check calls GET /health/ready on its container port through Python."""
    backend = composeServices[BACKEND_SERVICE]
    healthTest = backend["healthcheck"]["test"]
    probeUrls = PROBE_URL_PATTERN.findall(" ".join(healthTest[2:]))
    assert healthTest[:2] == ["CMD", "python"]
    assert len(probeUrls) == 1
    probeUrl = urlsplit(probeUrls[0])
    assert probeUrl.path == READINESS_PATH
    assert READINESS_PATH in {route.path for route in HEALTH_ROUTER.routes}
    assert str(probeUrl.port) == ContainerPort(backend)


@pytest.mark.parametrize(("serviceName", "settingsClass"), SETTINGS_CASES)
def test_compose_environment_builds_settings(composeServices, serviceName, settingsClass):
    """The settings class a container builds accepts the Compose environment unchanged."""
    serviceEnvironment = ServiceEnvironment(composeServices[serviceName])
    fieldValues = {
        name: value
        for name, value in serviceEnvironment.items()
        if name in settingsClass.model_fields
    }
    if "SECRET_KEY" in settingsClass.model_fields:
        fieldValues["SECRET_KEY"] = secrets.token_urlsafe(SECRET_KEY_BYTES)
    builtSettings = settingsClass(_env_file=None, **fieldValues)
    assert {name: getattr(builtSettings, name) for name in fieldValues} == fieldValues


# --- Compose: frontend ---------------------------------------------------------------------------
def test_frontend_builds_from_frontend_context(composeServices):
    """frontend builds frontend/Dockerfile with frontend/ as its context."""
    buildSettings = composeServices[FRONTEND_SERVICE]["build"]
    assert buildSettings["context"] == "./frontend"
    assert buildSettings["dockerfile"] == "Dockerfile"
    dockerfilePath = (REPO_ROOT / buildSettings["context"] / buildSettings["dockerfile"]).resolve()
    assert dockerfilePath == FRONTEND_DOCKERFILE_PATH.resolve()
    assert dockerfilePath.is_file()


def test_frontend_api_url_is_backend_published_origin(composeServices):
    """The NEXT_PUBLIC_API_URL build argument is the backend's published origin, as templated."""
    apiUrl = BuildArguments(composeServices[FRONTEND_SERVICE])[API_URL_VARIABLE]
    templateUrl = ReadEnvAssignments(FRONTEND_ENV_EXAMPLE_PATH)[API_URL_VARIABLE]
    assert apiUrl == PublishedOrigin(composeServices[BACKEND_SERVICE])
    assert apiUrl == templateUrl
    assert IsBrowserOrigin(apiUrl)


def test_frontend_api_url_is_not_a_runtime_variable(composeServices):
    """NEXT_PUBLIC_API_URL is a build argument only, never the frontend's runtime environment."""
    assert API_URL_VARIABLE not in ServiceEnvironment(composeServices[FRONTEND_SERVICE])
