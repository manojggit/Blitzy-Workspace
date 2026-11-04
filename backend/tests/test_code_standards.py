"""MK_Modernization Rule and traceability gate: static AST checks over every Python tree.

Specified in the Technical Specification, Sections 0.10 and 0.11.3. The checks parse every
module under backend/app, backend/alembic/versions, backend/tests and batch (batch/tests
included), import none of them, and need no database and no fixture. Scanned base classes are
followed by AST; only base classes from outside those trees are imported, to read their members.
Every base, decorator, wrapper call and annotation a check relies on is resolved as Python
resolves its leading name where it is evaluated: in that scope, then the enclosing defs (class
bodies skipped from nested code), the module and builtins, following global and nonlocal
declarations. The first scope that binds the name decides: imports that all agree give their
qualified name and a lone class definition that class, while any other binding (a parameter, an
assignment, a mix) leaves the name unresolved, which exempts nothing:

- Naming (clause 1): classes, type aliases and functions are PascalCase, parameters and local
  variables camelCase, and module-level assignments UPPER_CASE constants. A module-level
  assignment declares a type, named like a class, only where its callee resolves to typing's
  TypeVar, NewType, ParamSpec or TypeVarTuple, or its annotation to typing.TypeAlias (or to the
  typing_extensions member, where that package is importable). A leading underscore marks a
  module or class member private, so it is allowed there and on no local or parameter.
- Class members (clause 1) follow the module rules: a class variable is an UPPER_CASE constant,
  a property value, or a def a property, cached_property or accessor decorator makes one, a
  camelCase member, and a lambda, staticmethod or classmethod value a PascalCase method. A
  ClassVar or Final annotation marks a class variable. A field is a camelCase member, except as
  its framework fixes it: pydantic BaseSettings fields are UPPER_CASE environment variables,
  other pydantic BaseModel fields snake_case JSON keys, and the attributes of a
  DeclarativeBase-mapped class snake_case columns.
  An attribute stored on a scanned class (Limits.X = ...) follows the class-variable rules. One
  stored on a method's own receiver (self.x, or cls.x in a classmethod), in if, try and with
  blocks too, or on a name its scope binds only to instances of one scanned class (annotated
  with it, or assigned straight from a call of it) is a camelCase member, or PascalCase when a
  lambda or a unittest.mock object stands in for a method. A stored name the class or a scanned
  ancestor declares in its body is accepted, since the declaration is where it is checked. An
  attribute stored on any other object, whose class is imported or not statically resolvable,
  is not checked.
- Framework exemptions are verified, never read from spelling. A test_* def keeps its name only
  where pytest collects it as a test: in a file its default python_files collect (test_*.py or
  *_test.py in a test tree), at module level or directly in the body of a module-level Test*
  class that defines no __init__ or __new__, itself or through an ancestor. pytest's xunit
  setup and teardown hooks keep their names in the same positions: the class hooks on such a
  class, the module and function hooks at module level of such a file. In a class body, a
  dunder keeps its name, as does a def or attribute overriding a member an imported ancestor
  defines, and a visit_<Name> method of an ast.NodeVisitor subclass where ast.<Name> is a node
  class; a duck-typed stand-in keeps the names the class STAND_IN_CONTRACTS records for it
  defines. At module level, only the PEP 562 hooks, the hooks pytest declares (in the test
  trees) and Alembic's upgrade and downgrade keep their names. A parameter keeps a non-camelCase
  name only when the overridden member's signature or pytest's hook specification declares it,
  or when it names a pytest built-in fixture (tmp_path and the like) on a test-tree def pytest
  injects fixtures into: such a test, or a producer whose decorator resolves to pytest's or
  pytest-asyncio's fixture. Every other parameter, those of positionally called callbacks (xunit
  hooks, visit_ methods) and of lambdas included, stays camelCase. pytest and pytest-asyncio
  fixtures, whatever name their decorator is imported under, are PascalCase functions whose
  decorator declares a camelCase name= string literal, so injected parameters stay camelCase.
- Size (clause 3): a def body, its docstring excluded, and a lambda's expression body each span
  at most 20 lines, and each takes at most four parameters: positional and keyword-only ones,
  excluding *args, **kwargs and a def's self or cls receiver. Only a class-body override that
  declares exactly the counted parameters of the member it overrides is exempt from the limit.
- Source references (clause 5, Section 0.11.3): every ported module under backend/app/services,
  batch/jobs and batch/loaders opens with a "Replaces:" docstring naming a legacy artefact.
- Traceability (Section 0.11.3): the program-to-module map in docs/traceability.md lists each
  ported COBOL program once, against the one module that ports it, and that module names it.

Ruff enforces the rest of the rule (line length, tab indentation, compound statements,
hard-coded secrets and broad excepts; see backend/pyproject.toml). Its N rules stay unselected,
because they demand the opposite naming convention.
"""

import ast
import functools
import importlib
import inspect
import re
import textwrap
import warnings
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.core.paths import REPO_ROOT

SCAN_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "backend" / "app",
    REPO_ROOT / "backend" / "alembic" / "versions",
    REPO_ROOT / "backend" / "tests",
    REPO_ROOT / "batch",
)
SKIPPED_DIRECTORY_NAMES = frozenset({"__pycache__", ".venv", "node_modules"})
TEST_TREES: tuple[Path, ...] = (REPO_ROOT / "backend" / "tests", REPO_ROOT / "batch" / "tests")
ALEMBIC_VERSIONS_DIR = REPO_ROOT / "backend" / "alembic" / "versions"
HEADER_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "backend" / "app" / "services",
    REPO_ROOT / "batch" / "jobs",
    REPO_ROOT / "batch" / "loaders",
)
LEGACY_SOURCE_DIRS = ("cbl", "cpy", "cpy-bms", "bms", "jcl", "proc", "ctl")
HEADER_LABEL = "Replaces:"
TRACEABILITY_PATH = REPO_ROOT / "docs" / "traceability.md"
TRACEABILITY_RELATIVE_PATH = "docs/traceability.md"
PROGRAM_MAP_HEADING = "## Program-to-module map"
PROGRAM_COLUMN_HEADER = "Program"

EXPECTED_PROGRAM_MODULES: dict[str, str] = {
    "COSGN00C": "backend/app/services/auth/cosgn00c.py",
    "COMEN01C": "backend/app/services/menu/comen01c.py",
    "COADM01C": "backend/app/services/menu/coadm01c.py",
    "COACTVWC": "backend/app/services/accounts/coactvwc.py",
    "COACTUPC": "backend/app/services/accounts/coactupc.py",
    "COCRDLIC": "backend/app/services/cards/cocrdlic.py",
    "COCRDSLC": "backend/app/services/cards/cocrdslc.py",
    "COCRDUPC": "backend/app/services/cards/cocrdupc.py",
    "COTRN00C": "backend/app/services/transactions/cotrn00c.py",
    "COTRN01C": "backend/app/services/transactions/cotrn01c.py",
    "COTRN02C": "backend/app/services/transactions/cotrn02c.py",
    "CBTRN02C": "backend/app/services/transactions/cbtrn02c.py",
    "CORPT00C": "backend/app/services/reports/corpt00c.py",
    "CBTRN03C": "backend/app/services/reports/cbtrn03c.py",
    "COBIL00C": "backend/app/services/billpay/cobil00c.py",
    "COUSR00C": "backend/app/services/users/cousr00c.py",
    "COUSR01C": "backend/app/services/users/cousr01c.py",
    "COUSR02C": "backend/app/services/users/cousr02c.py",
    "COUSR03C": "backend/app/services/users/cousr03c.py",
    "CBACT04C": "batch/jobs/interest_calc.py",
    "CBSTM03A": "batch/jobs/statement_gen/cbstm03a.py",
    "CBSTM03B": "batch/jobs/statement_gen/cbstm03b.py",
    "CSUTLDTC": "backend/app/utils/date_validation.py",
}

MAX_FUNCTION_BODY_LINES = 20
MAX_PARAMETERS = 4
LAMBDA_LABEL = "<lambda>"

# A leading underscore is Python's visibility marker. It means something only on a module or
# class member, so classes, functions, constants and members may carry one; a local variable or
# parameter, which nothing outside its function sees, may not.
PASCAL_CASE_PATTERN = re.compile(r"^_{0,2}[A-Z][A-Za-z0-9]*$")
# A local variable or parameter; the bare "_" is the conventional throwaway name, so it counts.
CAMEL_CASE_PATTERN = re.compile(r"^(?:_|[a-z][A-Za-z0-9]*)$")
# A camelCase class member: a field, an instance attribute or a property accessor.
MEMBER_CASE_PATTERN = re.compile(r"^_{0,2}[a-z][A-Za-z0-9]*$")
# A fixture alias is the parameter name tests inject, so it is a camelCase parameter name and
# never the bare "_" throwaway.
FIXTURE_ALIAS_PATTERN = re.compile(r"^[a-z][A-Za-z0-9]*$")
UPPER_CASE_PATTERN = re.compile(r"^_{0,2}[A-Z][A-Z0-9_]*$")
# A column or JSON key name, which mirrors the snake_case schema.
SNAKE_CASE_PATTERN = re.compile(r"^_{0,2}[a-z][a-z0-9_]*$")
DUNDER_PATTERN = re.compile(r"^__[a-z0-9_]+__$")
TEST_FUNCTION_PATTERN = re.compile(r"^test_")
PYTEST_HOOK_PATTERN = re.compile(r"^pytest_")
LEGACY_WORD_PATTERN = re.compile(r"\b[A-Z][A-Z0-9-]{2,}\b")
TABLE_SEPARATOR_PATTERN = re.compile(r"^:?-+:?$")

# Alembic: the revision functions and the revision identifiers it reads from each script.
ALEMBIC_FUNCTIONS = frozenset({"upgrade", "downgrade"})
ALEMBIC_MODULE_VARIABLES = frozenset({"revision", "down_revision", "branch_labels", "depends_on"})
# Uvicorn: the ASGI target is app.main:app.
MODULE_VARIABLE_EXEMPTIONS: dict[str, frozenset[str]] = {"backend/app/main.py": frozenset({"app"})}
# pytest: module-level marks and plugin declarations.
TEST_MODULE_VARIABLES = frozenset({"pytestmark", "pytest_plugins"})
# pytest's default python_files: only these files hold tests, as test_* functions at module level
# or in the classes the default python_classes collects: Test-prefixed ones at module level.
TEST_FILE_PATTERNS = ("test_*.py", "*_test.py")
TEST_CLASS_PATTERN = re.compile(r"^Test")
# pytest skips a test class that defines either, itself or through an ancestor other than object.
CONSTRUCTOR_NAMES = frozenset({"__init__", "__new__"})
# pytest's python plugin (_pytest/python.py) calls these xunit-style setup and teardown functions
# by name, passing any argument positionally: the class hooks on a collected test class, the
# module and function hooks at module level of a collected test file.
XUNIT_CLASS_HOOKS = frozenset({"setup_class", "teardown_class", "setup_method", "teardown_method"})
XUNIT_MODULE_HOOKS = frozenset(
    {"setup_module", "teardown_module", "setup_function", "teardown_function"}
)
# ast.NodeVisitor.visit passes each node, positionally, to the visit_<node class name> method.
NODE_VISITOR_CLASS = "ast.NodeVisitor"
NODE_VISITOR_METHOD_PATTERN = re.compile(r"^visit_(\w+)$")
# PEP 562: the module-level dunder functions Python calls by name.
MODULE_DUNDER_FUNCTIONS = frozenset({"__getattr__", "__dir__"})
RECEIVER_NAMES = frozenset({"self", "cls"})
# pytest built-in fixtures, which pytest injects by parameter name into tests and fixtures only.
PYTEST_BUILTIN_FIXTURE_PARAMETERS = frozenset(
    {
        "tmp_path",
        "tmp_path_factory",
        "record_property",
        "record_testsuite_property",
        "record_xml_attribute",
        "doctest_namespace",
    }
)
# pytest and pytest-asyncio: fixture(...) from either module declares the injected name in name=.
FIXTURE_DECORATOR_MODULES = frozenset({"pytest", "pytest_asyncio"})
FIXTURE_DECORATOR_NAME = "fixture"
FIXTURE_ALIAS_KEYWORD = "name"
# Python's property protocol: an accessor is an attribute, so camelCase is accepted. Like every
# qualified name below, a factory is matched by what its decorator or callee resolves to.
PROPERTY_FACTORIES = frozenset({"builtins.property", "functools.cached_property"})
PROPERTY_ACCESSOR_ATTRIBUTES = frozenset({"setter", "getter", "deleter"})
# typing: a type declared by a factory call or a TypeAlias annotation is named like a class. The
# callee or annotation counts only as it resolves to one of these members of typing, or of its
# typing_extensions backport where that package is importable (TypingModules).
TYPING_MODULE = "typing"
TYPING_BACKPORT_MODULE = "typing_extensions"
TYPE_FACTORY_CALLS = frozenset({"TypeVar", "NewType", "ParamSpec", "TypeVarTuple"})
TYPE_ALIAS_ANNOTATIONS = frozenset({"TypeAlias"})
# Class resolution. A scanned module is read by AST under its import name (backend/app is the app
# package, backend/tests the tests package, batch the batch package) and is never imported; any
# other base class is imported, so its members can be read from its MRO.
PACKAGE_ROOTS: tuple[tuple[Path, str], ...] = (
    (REPO_ROOT / "backend" / "app", "app"),
    (REPO_ROOT / "backend" / "tests", "tests"),
    (REPO_ROOT / "batch", "batch"),
)
PROJECT_PACKAGES = frozenset(package for _, package in PACKAGE_ROOTS)
PACKAGE_INIT_STEM = "__init__"
BUILTINS_MODULE = "builtins"
# A star import may bind any name, so the module scope records it under this key.
STAR_IMPORT_NAME = "*"
ANNOTATIONS_ATTRIBUTE = "__annotations__"
STATICMETHOD_DECORATOR = "builtins.staticmethod"
METHOD_WRAPPER_CALLS = frozenset({STATICMETHOD_DECORATOR, "builtins.classmethod"})
POSITIONAL_KINDS = frozenset(
    {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
)
# Member naming. typing: a class-level annotation that declares a class variable, not a field.
CLASS_VARIABLE_ANNOTATIONS = frozenset({"typing.ClassVar", "typing.Final"})
# unittest.mock: an instance stored on a receiver stands in for a method (self.GetCard).
METHOD_STAND_IN_CLASSES = frozenset(
    {"unittest.mock.Mock", "unittest.mock.MagicMock", "unittest.mock.AsyncMock"}
)
ORM_BASE_CLASS = "sqlalchemy.orm.DeclarativeBase"
# Duck-typed stand-ins that mirror a class without inheriting from it, by repository-relative
# path and class name: a member name the mirrored class defines keeps that class's form.
STAND_IN_CONTRACTS: dict[tuple[str, str], str] = {
    # SQLAlchemy's pool calls the PEP 249 close() of the psycopg2 connection it holds.
    ("backend/tests/unit/core/test_logging.py", "FailingCloseConnection"): (
        "psycopg2.extensions.connection"
    ),
    # app.core.errors reads an asyncpg error's SQLSTATE from its sqlstate attribute.
    ("backend/tests/unit/core/test_file_condition_log.py", "CyclicCauseB"): (
        "asyncpg.PostgresError"
    ),
    (
        "backend/tests/unit/repositories/test_repository_failures.py",
        "LockNotAvailableDriverError",
    ): "asyncpg.PostgresError",
    # Revision 0002 and the batch row builders read these settings' environment fields.
    ("backend/tests/unit/migrations/test_seed_sample_data_revision.py", "SettingsStub"): (
        "app.core.config.MigrationSettings"
    ),
    ("batch/tests/test_replace_loader_streaming.py", "SettingsStub"): "batch.config.BatchSettings",
}

FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPE_BOUNDARY_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
ASSIGNMENT_TYPES = (ast.Assign, ast.AnnAssign, ast.AugAssign)
MODULE_BLOCK_TYPES = (ast.If, ast.Try, ast.TryStar, ast.With)
MATCH_CAPTURE_TYPES = (ast.MatchAs, ast.MatchStar)

type FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
type ParameterOwner = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
# What a name resolves to: a qualified name, the class definition that binds it, or None.
type NameTarget = str | ast.ClassDef | None


@dataclass(frozen=True, order=True)
class Violation:
    """One rule breach, located by repository-relative path and line."""

    relativePath: str
    line: int
    message: str

    def Describe(self) -> str:
        return f"{self.relativePath}:{self.line}: {self.message}"


@dataclass(frozen=True)
class SourceModule:
    """A scanned file: its parsed tree, or the error that stopped the parse."""

    path: Path
    relativePath: str
    tree: ast.Module | None
    error: str


@dataclass(frozen=True)
class FixtureBindings:
    """Names that reach a fixture decorator: module names for module.fixture, bare callee names."""

    moduleNames: frozenset[str]
    functionNames: frozenset[str]


@dataclass(frozen=True)
class ClassReference:
    """A class found by AST in a scanned module, or in in-test source parsed as one."""

    module: SourceModule
    node: ast.ClassDef


@dataclass(frozen=True)
class NamingRule:
    """The forms a class member's name may take, and how a breach describes them."""

    patterns: tuple[re.Pattern[str], ...]
    form: str

    def Accepts(self, name: str) -> bool:
        return any(pattern.match(name) for pattern in self.patterns)


# pytest.fixture, pytest_asyncio.fixture and a bare fixture, recognised whatever a module imports.
CANONICAL_FIXTURE_BINDINGS = FixtureBindings(
    FIXTURE_DECORATOR_MODULES, frozenset({FIXTURE_DECORATOR_NAME})
)

CONSTANT_RULE = NamingRule((UPPER_CASE_PATTERN,), "an UPPER_CASE constant")
MEMBER_RULE = NamingRule((MEMBER_CASE_PATTERN,), "camelCase")
METHOD_RULE = NamingRule((PASCAL_CASE_PATTERN,), "PascalCase")
# An ORM class's unannotated attribute may be a mapped column, named as the column is.
ORM_ATTRIBUTE_RULE = NamingRule(
    (UPPER_CASE_PATTERN, SNAKE_CASE_PATTERN), "an UPPER_CASE constant or a snake_case column"
)
# The fields of a class extending one of these frameworks keep the names the framework maps them
# to: environment variables, JSON keys and columns. BaseSettings is a BaseModel, so it comes first.
FRAMEWORK_FIELD_RULES: tuple[tuple[str, NamingRule], ...] = (
    (
        "pydantic_settings.BaseSettings",
        NamingRule((UPPER_CASE_PATTERN,), "an UPPER_CASE environment variable"),
    ),
    ("pydantic.BaseModel", NamingRule((SNAKE_CASE_PATTERN,), "a snake_case JSON key")),
    (ORM_BASE_CLASS, NamingRule((SNAKE_CASE_PATTERN,), "a snake_case column")),
)
# A class-body value that makes its name another kind of member: a property is read like a
# field, and a wrapped function is a method.
VALUE_CALL_RULES: dict[str, NamingRule] = {
    **dict.fromkeys(PROPERTY_FACTORIES, MEMBER_RULE),
    **dict.fromkeys(METHOD_WRAPPER_CALLS, METHOD_RULE),
}

type Checker = Callable[[SourceModule], list[Violation]]


def IsSkippedPath(path: Path) -> bool:
    """Skip caches, environments, dependency trees and hidden directories."""
    parts = path.relative_to(REPO_ROOT).parts
    return any(part in SKIPPED_DIRECTORY_NAMES or part.startswith(".") for part in parts)


def IterSourcePaths() -> list[Path]:
    paths: set[Path] = set()
    for root in SCAN_ROOTS:
        paths.update(path for path in root.rglob("*.py") if not IsSkippedPath(path))
    return sorted(paths)


def ReadModule(path: Path) -> SourceModule:
    relativePath = path.relative_to(REPO_ROOT).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relativePath)
    except (SyntaxError, UnicodeDecodeError) as error:
        return SourceModule(path, relativePath, None, f"{type(error).__name__}: {error}")
    return SourceModule(path, relativePath, tree, "")


@functools.cache
def LoadModules() -> tuple[SourceModule, ...]:
    return tuple(ReadModule(path) for path in IterSourcePaths())


@functools.cache
def LegacyArtefactNames() -> frozenset[str]:
    """Upper-cased file stems of the legacy sources, for example COACTUPC, CVACT01Y, ACCTFILE."""
    names: set[str] = set()
    for directory in LEGACY_SOURCE_DIRS:
        names.update(path.stem.upper() for path in (REPO_ROOT / directory).iterdir())
    return frozenset(names)


def ParseSource(text: str, relativePath: str) -> SourceModule:
    """Wrap in-test source as if it were the scanned file at relativePath."""
    return SourceModule(REPO_ROOT / relativePath, relativePath, ast.parse(text), "")


def IsUnder(module: SourceModule, roots: tuple[Path, ...]) -> bool:
    return any(module.path.is_relative_to(root) for root in roots)


def IsInTestTree(module: SourceModule) -> bool:
    return IsUnder(module, TEST_TREES)


def IsAlembicVersion(module: SourceModule) -> bool:
    return module.path.is_relative_to(ALEMBIC_VERSIONS_DIR)


def IsTestFile(module: SourceModule) -> bool:
    """A test-tree file whose name pytest's default python_files patterns collect."""
    return IsInTestTree(module) and any(module.path.match(name) for name in TEST_FILE_PATTERNS)


def FormatViolations(violations: list[Violation]) -> str:
    return "\n".join(violation.Describe() for violation in violations)


def CollectViolations(checkers: tuple[Checker, ...]) -> list[Violation]:
    """Run each checker over every parsed module; unparsable files fail their own test."""
    violations: list[Violation] = []
    for module in LoadModules():
        if module.tree is not None:
            for checker in checkers:
                violations.extend(checker(module))
    return sorted(violations)


def ModuleImportName(module: SourceModule) -> str:
    """The dotted name a scanned module is imported under, or "" outside the three packages."""
    for root, package in PACKAGE_ROOTS:
        if module.path.is_relative_to(root):
            parts = list(module.path.relative_to(root).with_suffix("").parts)
            if parts[-1] == PACKAGE_INIT_STEM:
                parts.pop()
            return ".".join([package, *parts])
    return ""


def RelativeImportBase(module: SourceModule, level: int) -> str:
    """The package a relative import of the given level starts from, or "" when there is none."""
    parts = ModuleImportName(module).split(".")
    if module.path.stem != PACKAGE_INIT_STEM:
        parts.pop()
    kept = len(parts) - level + 1
    return ".".join(parts[:kept]) if kept > 0 and parts[0] else ""


def ImportSource(statement: ast.ImportFrom, module: SourceModule) -> str:
    """The absolute module a from-import reads, or "" when a relative one cannot resolve."""
    if statement.level == 0:
        return statement.module or ""
    base = RelativeImportBase(module, statement.level)
    return ".".join(part for part in (base, statement.module) if part) if base else ""


def ImportBindings(
    statement: ast.Import | ast.ImportFrom, module: SourceModule
) -> Iterator[tuple[str, str]]:
    """Yield the local name and the qualified name of each binding an import statement makes.

    A star import binds STAR_IMPORT_NAME, and it and a relative import that cannot resolve give
    the qualified name "", which resolves to nothing.
    """
    if isinstance(statement, ast.Import):
        for alias in statement.names:
            rootName = alias.name.split(".")[0]
            yield (alias.asname, alias.name) if alias.asname else (rootName, rootName)
        return
    source = ImportSource(statement, module)
    for alias in statement.names:
        isNamed = bool(source) and alias.name != STAR_IMPORT_NAME
        yield alias.asname or alias.name, f"{source}.{alias.name}" if isNamed else ""


@functools.cache
def ParentMap(module: SourceModule) -> dict[int, ast.AST]:
    """Each node's parent node, keyed by the child's identity."""
    return {
        id(child): node for node in ast.walk(module.tree) for child in ast.iter_child_nodes(node)
    }


def IsScopeBody(child: ast.AST, parent: ast.AST) -> bool:
    """Whether child belongs to the body a module, class, def or lambda evaluates in its scope."""
    if isinstance(parent, ast.Module):
        return True
    if isinstance(parent, ast.Lambda):
        return child is parent.body
    body = parent.body if isinstance(parent, (*FUNCTION_TYPES, ast.ClassDef)) else []
    return any(child is statement for statement in body)


def EnclosingScope(module: SourceModule, node: ast.AST) -> ast.AST:
    """The module, class, def or lambda whose scope evaluates node.

    A definition's decorators, defaults, annotations and class bases are evaluated where the
    definition stands, so they belong to the scope around it.
    """
    parents = ParentMap(module)
    child, parent = node, parents.get(id(node))
    while parent is not None and not IsScopeBody(child, parent):
        child, parent = parent, parents.get(id(parent))
    return module.tree if parent is None else parent


@functools.cache
def ScopeChain(module: SourceModule, scope: ast.AST) -> tuple[ast.AST, ...]:
    """The scopes Python searches for a name evaluated in scope: it, enclosing defs, the module.

    A class body is searched only as the scope itself; code nested in it skips it.
    """
    chain, current = [scope], scope
    while not isinstance(current, ast.Module):
        current = EnclosingScope(module, current)
        if not isinstance(current, ast.ClassDef):
            chain.append(current)
    return tuple(chain)


@functools.cache
def DeclaredNames(scope: ast.AST, declarationType: type[ast.stmt]) -> frozenset[str]:
    """The names a scope's own global (or nonlocal) statements declare."""
    nodes = [node for node in IterScopeNodes(scope) if isinstance(node, declarationType)]
    return frozenset(name for node in nodes for name in node.names)


def NodeBindings(node: ast.AST, module: SourceModule) -> Iterator[tuple[str, object]]:
    """Yield what an import, def, class, type alias or del binds; an import binds a name string."""
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        yield from ImportBindings(node, module)
    elif isinstance(node, (*FUNCTION_TYPES, ast.ClassDef)):
        yield node.name, node
    elif isinstance(node, ast.TypeAlias):
        yield node.name.id, node
    elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Del):
        yield node.id, node


def NestedRebindings(
    module: SourceModule, scope: ast.AST, declarationType: type[ast.stmt]
) -> Iterator[tuple[str, object]]:
    """Yield the names nested scopes declare global (or nonlocal) and bind, so rebind out here."""
    for nested in ast.walk(scope):
        if nested is not scope and isinstance(nested, SCOPE_BOUNDARY_TYPES):
            declared = DeclaredNames(nested, declarationType)
            rebound = declared.intersection(ScopeBindings(module, nested))
            yield from ((name, nested) for name in rebound)


def ScopeBindingPairs(module: SourceModule, scope: ast.AST) -> Iterator[tuple[str, object]]:
    """Yield each name a scope binds, with the qualified name an import gives it or its binder.

    Binders are the scope's parameters and type parameters, its assignment, loop, with, except
    and match targets, and its defs, classes and deletions; a comprehension's targets count too.
    """
    nodes = list(IterScopeNodes(scope))
    yield from ((name, node) for node, name in BindingNodes(nodes))
    for node in nodes:
        yield from NodeBindings(node, module)
    if isinstance(scope, (*FUNCTION_TYPES, ast.Lambda)):
        yield from ((parameter.arg, parameter) for parameter in ParameterList(scope))
    yield from ((parameter.name, parameter) for parameter in getattr(scope, "type_params", []))
    if isinstance(scope, ast.Module):
        yield from NestedRebindings(module, scope, ast.Global)
    elif not isinstance(scope, ast.ClassDef):
        yield from NestedRebindings(module, scope, ast.Nonlocal)


@functools.cache
def ScopeBindings(module: SourceModule, scope: ast.AST) -> dict[str, tuple[object, ...]]:
    """Every binding a module, class, def or lambda scope makes, by the name it binds."""
    bindings: dict[str, list[object]] = {}
    for name, binding in ScopeBindingPairs(module, scope):
        bindings.setdefault(name, []).append(binding)
    return {name: tuple(values) for name, values in bindings.items()}


def ModuleBindings(module: SourceModule, name: str) -> tuple[object, ...]:
    """The module scope's bindings of name; a star import, which may bind any name, is one."""
    bindings = ScopeBindings(module, module.tree)
    return (*bindings.get(name, ()), *bindings.get(STAR_IMPORT_NAME, ()))


def NameBindings(module: SourceModule, scope: ast.AST, name: str) -> tuple[object, ...] | None:
    """The bindings of name in the first scope of its lookup chain (ScopeChain) that binds it.

    A global declaration sends the lookup to the module, a nonlocal one to the enclosing defs.
    () means no scope binds the name; None means a nonlocal declaration that no def binds.
    """
    functionsOnly = False
    for current in ScopeChain(module, scope):
        if isinstance(current, ast.Module):
            return None if functionsOnly else ModuleBindings(module, name)
        if name in DeclaredNames(current, ast.Global):
            return ModuleBindings(module, name)
        if name in DeclaredNames(current, ast.Nonlocal):
            functionsOnly = True
        elif name in ScopeBindings(module, current):
            return ScopeBindings(module, current)[name]
    return None


def BindingTarget(bindings: tuple[object, ...]) -> NameTarget:
    """The qualified name one scope's imports of a name agree on, or its one class definition.

    Any other binding, a parameter or an assignment included, or a mix of them, gives None.
    """
    if len(set(bindings)) == 1 and isinstance(bindings[0], str):
        return bindings[0] or None
    if len(bindings) == 1 and isinstance(bindings[0], ast.ClassDef):
        return bindings[0]
    return None


def ResolveName(module: SourceModule, scope: ast.AST, name: str) -> NameTarget:
    """What name means where scope evaluates it: a qualified name, a class definition or None.

    The deciding scope's bindings give the BindingTarget; a name no scope binds is a builtin.
    """
    bindings = NameBindings(module, scope, name)
    if bindings == ():
        return f"{BUILTINS_MODULE}.{name}"
    return BindingTarget(bindings) if bindings else None


@functools.cache
def ProjectModules() -> dict[str, SourceModule]:
    """The parsed scanned modules of the app, tests and batch packages, by import name."""
    modules = {
        ModuleImportName(module): module for module in LoadModules() if module.tree is not None
    }
    modules.pop("", None)
    return modules


def DottedText(node: ast.expr) -> str:
    """The dotted text of a Name or Attribute chain such as sa.types.TypeDecorator, else ""."""
    if isinstance(node, ast.Attribute):
        prefix = DottedText(node.value)
        return f"{prefix}.{node.attr}" if prefix else ""
    return node.id if isinstance(node, ast.Name) else ""


def JoinedName(prefix: str, rest: str) -> str:
    """A qualified name prefix and a dotted remainder, joined; either may be empty."""
    return ".".join(part for part in (prefix, rest) if part)


def ExpressionTarget(expression: ast.expr, module: SourceModule) -> tuple[NameTarget, str]:
    """What a dotted expression's leading name means where it is evaluated, and the rest of it."""
    head, _, rest = DottedText(expression).partition(".")
    target = ResolveName(module, EnclosingScope(module, expression), head) if head else None
    return target, rest


def QualifiedName(expression: ast.expr, module: SourceModule) -> str:
    """The qualified name a dotted expression spells through the imports in its scope, or ""."""
    target, rest = ExpressionTarget(expression, module)
    return JoinedName(target, rest) if isinstance(target, str) else ""


def NestedClass(reference: ClassReference, attributePath: str) -> ClassReference | None:
    """The class a dotted attribute path such as Outer.Inner reaches through class bodies."""
    current = reference.node
    for name in filter(None, attributePath.split(".")):
        nested = [node for node in current.body if isinstance(node, ast.ClassDef)]
        matches = [node for node in nested if node.name == name]
        if not matches:
            return None
        current = matches[0]
    return ClassReference(reference.module, current)


def ResolveClassExpression(
    expression: ast.expr, module: SourceModule
) -> ClassReference | type | None:
    """A class expression's class, its leading name resolved where it is evaluated (ResolveName).

    A subscripted base such as Mapping[str, int] is its unsubscripted class.
    """
    base = expression.value if isinstance(expression, ast.Subscript) else expression
    target, rest = ExpressionTarget(base, module)
    if isinstance(target, ast.ClassDef):
        return NestedClass(ClassReference(module, target), rest)
    return ResolveQualifiedName(JoinedName(target, rest)) if target else None


@functools.cache
def ResolveQualifiedName(qualifiedName: str) -> ClassReference | type | None:
    """A class by qualified name: a project class by AST, any other class by import."""
    if qualifiedName.split(".")[0] in PROJECT_PACKAGES:
        return ProjectClass(qualifiedName)
    return ExternalClass(qualifiedName)


def ProjectClass(qualifiedName: str) -> ClassReference | type | None:
    """A project class, read from the scanned module the longest prefix of its name names."""
    parts = qualifiedName.split(".")
    for split in range(len(parts) - 1, 0, -1):
        module = ProjectModules().get(".".join(parts[:split]))
        if module is not None:
            return ModuleMemberClass(module, parts[split], ".".join(parts[split + 1 :]))
    return None


def ModuleMemberClass(
    module: SourceModule, name: str, attributePath: str
) -> ClassReference | type | None:
    """The class a scanned module's top level defines or re-exports as name, then attributePath.

    Only module-scope bindings count (ModuleBindings), resolved as BindingTarget resolves them.
    """
    bindings = ModuleBindings(module, name)
    target = BindingTarget(bindings) if bindings else None
    if isinstance(target, ast.ClassDef):
        return NestedClass(ClassReference(module, target), attributePath)
    if not target or target == f"{ModuleImportName(module)}.{name}":
        return None
    return ResolveQualifiedName(JoinedName(target, attributePath))


@functools.cache
def ExternalClass(qualifiedName: str) -> type | None:
    """Import the longest importable prefix of a qualified name and read the rest as attributes."""
    parts = qualifiedName.split(".")
    for split in range(len(parts), 0, -1):
        try:
            value: object = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        for name in parts[split:]:
            value = getattr(value, name, None)
        return value if isinstance(value, type) else None
    return None


@functools.cache
def BaseClasses(reference: ClassReference) -> tuple[ClassReference | type, ...]:
    """What each base of a scanned class resolves to; an unresolvable base contributes nothing."""
    resolved = [ResolveClassExpression(base, reference.module) for base in reference.node.bases]
    return tuple(base for base in resolved if base is not None and base != reference)


@functools.cache
def ProjectLineage(reference: ClassReference) -> tuple[ClassReference, ...]:
    """A scanned class followed by every scanned class it inherits from."""
    lineage = [reference]
    for base in BaseClasses(reference):
        if isinstance(base, ClassReference):
            lineage.extend(ProjectLineage(base))
    return tuple(lineage)


def ExternalAncestors(reference: ClassReference) -> tuple[type, ...]:
    """The imported classes a scanned class extends, directly or through scanned ancestors."""
    return tuple(
        base
        for ancestor in ProjectLineage(reference)
        for base in BaseClasses(ancestor)
        if isinstance(base, type)
    )


def ExternalMro(reference: ClassReference) -> tuple[type, ...]:
    """Every class in the MROs of a scanned class's imported ancestors, nearest first."""
    return tuple(owner for ancestor in ExternalAncestors(reference) for owner in ancestor.__mro__)


def DefinesName(owner: type, name: str) -> bool:
    """Whether a class's own namespace binds or annotates name."""
    annotations = vars(owner).get(ANNOTATIONS_ATTRIBUTE, {})
    return name in vars(owner) or (isinstance(annotations, dict) and name in annotations)


def IsInheritedMember(reference: ClassReference, name: str) -> bool:
    """Whether an imported ancestor of the class defines name, so its framework fixes the name."""
    return any(DefinesName(owner, name) for owner in ExternalMro(reference))


def InheritedMember(reference: ClassReference, name: str) -> object:
    """The value the nearest imported ancestor binds to name, or None when none binds it."""
    for owner in ExternalMro(reference):
        if name in vars(owner):
            return vars(owner)[name]
    return None


def FrameworkParameters(member: object) -> tuple[inspect.Parameter, ...]:
    """The declared parameters of a framework method, classmethod or staticmethod, else ()."""
    function = getattr(member, "__func__", member)
    try:
        return tuple(inspect.signature(function).parameters.values())
    except (TypeError, ValueError):
        return ()


def FrameworkCountedNames(member: object) -> tuple[str, ...]:
    """A framework method's positional and keyword-only parameter names, its receiver dropped."""
    parameters = FrameworkParameters(member)
    positional = [parameter.name for parameter in parameters if parameter.kind in POSITIONAL_KINDS]
    if positional and not isinstance(member, staticmethod):
        positional = positional[1:]
    keywordOnly = [
        parameter.name for parameter in parameters if parameter.kind == parameter.KEYWORD_ONLY
    ]
    return (*positional, *keywordOnly)


@functools.cache
def PytestHooks() -> object:
    """pytest's hook specifications, read from a fresh PytestPluginManager."""
    return pytest.PytestPluginManager().hook


def IsPytestHook(name: str, module: SourceModule) -> bool:
    """A test-tree pytest_* name that pytest's hook specifications declare."""
    isHookName = IsInTestTree(module) and PYTEST_HOOK_PATTERN.match(name)
    return bool(isHookName) and hasattr(PytestHooks(), name)


def PytestHookArguments(name: str) -> frozenset[str]:
    """The argument names pytest's specification of a hook declares."""
    specification = getattr(getattr(PytestHooks(), name, None), "spec", None)
    return frozenset(specification.argnames) if specification is not None else frozenset()


def IterFunctions(tree: ast.AST) -> Iterator[tuple[FunctionNode, ast.AST]]:
    """Yield every def, nested ones included, with the module, class or def whose scope holds it."""
    pending: list[tuple[ast.AST, ast.AST]] = [(tree, tree)]
    while pending:
        node, scope = pending.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, FUNCTION_TYPES):
                yield child, scope
            pending.append((child, child if isinstance(child, SCOPE_BOUNDARY_TYPES) else scope))


def IterLambdas(tree: ast.AST) -> list[ast.Lambda]:
    """Every lambda in a tree, nested ones included."""
    return [node for node in ast.walk(tree) if isinstance(node, ast.Lambda)]


def IterScopeNodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Yield the nodes of a module, function or lambda body, stopping at nested scopes."""
    body = scope.body if isinstance(scope.body, list) else [scope.body]
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, SCOPE_BOUNDARY_TYPES):
            pending.extend(ast.iter_child_nodes(node))


def NameViolation(module: SourceModule, node: ast.AST, message: str) -> Violation:
    return Violation(module.relativePath, getattr(node, "lineno", 0), message)


def ModuleLevelStatements(statements: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield a module or class body's statements, and those of its if, try and with blocks."""
    for statement in statements:
        yield statement
        if isinstance(statement, MODULE_BLOCK_TYPES):
            yield from ModuleLevelStatements(NestedStatements(statement))


def DefinesConstructor(reference: ClassReference) -> bool:
    """Whether the class, or an ancestor other than object, defines __init__ or __new__.

    Scanned classes are read from their bodies and imported ones from their namespaces, as
    pytest's hasinit and hasnew read the class.
    """
    if any(ClassBodyNames(ancestor) & CONSTRUCTOR_NAMES for ancestor in ProjectLineage(reference)):
        return True
    owners = [owner for owner in ExternalMro(reference) if owner is not object]
    return any(DefinesName(owner, name) for owner in owners for name in CONSTRUCTOR_NAMES)


def IsCollectedTestClass(scope: ast.AST, module: SourceModule) -> bool:
    """A Test* class that pytest's default python_classes collects from a collected test file.

    It is defined at module level and has no constructor, of its own or by inheritance.
    """
    return (
        isinstance(scope, ast.ClassDef)
        and IsTestFile(module)
        and bool(TEST_CLASS_PATTERN.match(scope.name))
        and any(statement is scope for statement in ModuleLevelStatements(module.tree.body))
        and not DefinesConstructor(ClassReference(module, scope))
    )


def IsTestFunction(function: FunctionNode, scope: ast.AST, module: SourceModule) -> bool:
    """A test_* def that pytest collects as a test from a collected test file.

    It is defined at module level, or directly in the body of a collected Test* class.
    """
    if not (IsTestFile(module) and TEST_FUNCTION_PATTERN.match(function.name)):
        return False
    return isinstance(scope, ast.Module) or IsCollectedTestClass(scope, module)


def IsNodeVisitorMethod(name: str, reference: ClassReference) -> bool:
    """A visit_<Name> method of an ast.NodeVisitor subclass, where ast.<Name> is a node class."""
    match = NODE_VISITOR_METHOD_PATTERN.match(name)
    with warnings.catch_warnings(action="ignore", category=DeprecationWarning):
        nodeClass = getattr(ast, match.group(1), None) if match else None
    isNodeClass = isinstance(nodeClass, type) and issubclass(nodeClass, ast.AST)
    return isNodeClass and ExtendsFramework(reference, NODE_VISITOR_CLASS)


def IsClassCallback(name: str, reference: ClassReference) -> bool:
    """A class-body def name a framework calls, verified against that framework.

    A dunder, a member an imported ancestor defines, an IsNodeVisitorMethod callback, or an
    XUNIT_CLASS_HOOKS hook of a collected test class.
    """
    if DUNDER_PATTERN.match(name) or IsInheritedMember(reference, name):
        return True
    if name in XUNIT_CLASS_HOOKS:
        return IsCollectedTestClass(reference.node, reference.module)
    return IsNodeVisitorMethod(name, reference)


def IsModuleCallback(name: str, module: SourceModule) -> bool:
    """A module-level def name a framework calls, verified against that framework.

    A PEP 562 hook, a hook pytest declares in a test tree, an Alembic revision function, or an
    XUNIT_MODULE_HOOKS hook of a collected test file.
    """
    return (
        name in MODULE_DUNDER_FUNCTIONS
        or IsPytestHook(name, module)
        or (IsAlembicVersion(module) and name in ALEMBIC_FUNCTIONS)
        or (IsTestFile(module) and name in XUNIT_MODULE_HOOKS)
    )


def IsFrameworkMandated(function: FunctionNode, scope: ast.AST, module: SourceModule) -> bool:
    """A name a framework calls in this very position, verified against that framework.

    A test function pytest collects (IsTestFunction), a class-body name IsClassCallback accepts,
    or a module-level one IsModuleCallback accepts.
    """
    if IsTestFunction(function, scope, module):
        return True
    if isinstance(scope, ast.ClassDef):
        return IsClassCallback(function.name, ClassReference(module, scope))
    return isinstance(scope, ast.Module) and IsModuleCallback(function.name, module)


def FrameworkParameterNames(
    function: FunctionNode, scope: ast.AST, module: SourceModule
) -> frozenset[str]:
    """Parameter names a framework fixes: those of the overridden member or of the pytest hook.

    The parameters of every other def, test functions and dunders that override nothing
    included, are the author's to name.
    """
    if isinstance(scope, ast.ClassDef):
        member = InheritedMember(ClassReference(module, scope), function.name)
        return frozenset(parameter.name for parameter in FrameworkParameters(member))
    if isinstance(scope, ast.Module) and IsPytestHook(function.name, module):
        return PytestHookArguments(function.name)
    return frozenset()


def IsPropertyFactory(expression: ast.expr, module: SourceModule) -> bool:
    """property or functools.cached_property, resolved where the expression is evaluated."""
    return QualifiedName(expression, module) in PROPERTY_FACTORIES


def IsPropertyGetter(binding: object, module: SourceModule) -> bool:
    """A def decorated with a property factory."""
    decorators = getattr(binding, "decorator_list", [])
    return isinstance(binding, FUNCTION_TYPES) and any(
        IsPropertyFactory(decorator, module) for decorator in decorators
    )


def IsPropertyAccessorDecorator(decorator: ast.expr, module: SourceModule) -> bool:
    """A property factory, or the setter, getter or deleter of a property its scope defines.

    That property's name is bound there only by defs, one of them decorated with a factory.
    """
    if IsPropertyFactory(decorator, module):
        return True
    isAccessor = isinstance(decorator, ast.Attribute) and isinstance(decorator.value, ast.Name)
    if not isAccessor or decorator.attr not in PROPERTY_ACCESSOR_ATTRIBUTES:
        return False
    scope = EnclosingScope(module, decorator)
    bindings = NameBindings(module, scope, decorator.value.id) or ()
    isOnlyDefs = all(isinstance(binding, FUNCTION_TYPES) for binding in bindings)
    return isOnlyDefs and any(IsPropertyGetter(binding, module) for binding in bindings)


def IsPropertyAccessor(function: FunctionNode, module: SourceModule) -> bool:
    """A def a verified property decorator (IsPropertyAccessorDecorator) makes an attribute."""
    return any(
        IsPropertyAccessorDecorator(decorator, module) for decorator in function.decorator_list
    )


def CheckClassNames(module: SourceModule) -> list[Violation]:
    violations: list[Violation] = []
    for node in ast.walk(module.tree):
        if isinstance(node, ast.ClassDef) and not PASCAL_CASE_PATTERN.match(node.name):
            violations.append(NameViolation(module, node, f"class {node.name!r} is not PascalCase"))
        elif isinstance(node, ast.TypeAlias) and not PASCAL_CASE_PATTERN.match(node.name.id):
            message = f"type alias {node.name.id!r} is not PascalCase"
            violations.append(NameViolation(module, node, message))
    return violations


def IsAcceptedFunctionName(
    function: FunctionNode, scope: ast.AST, module: SourceModule, bindings: FixtureBindings
) -> bool:
    """A fixture producer is PascalCase even in a framework-exempt form such as test_*.

    pytest injects a fixture by its name= alias, so no framework calls the producer by its name.
    """
    if FixtureDecorator(function, bindings) is not None:
        return bool(PASCAL_CASE_PATTERN.match(function.name))
    if IsFrameworkMandated(function, scope, module):
        return True
    if IsPropertyAccessor(function, module) and MEMBER_CASE_PATTERN.match(function.name):
        return True
    return bool(PASCAL_CASE_PATTERN.match(function.name))


def CheckFunctionNames(module: SourceModule) -> list[Violation]:
    bindings = ModuleFixtureBindings(module)
    return [
        NameViolation(module, function, f"function {function.name!r} is not PascalCase")
        for function, scope in IterFunctions(module.tree)
        if not IsAcceptedFunctionName(function, scope, module, bindings)
    ]


def ParameterList(owner: ParameterOwner) -> list[ast.arg]:
    arguments = owner.args
    variadic = [parameter for parameter in (arguments.vararg, arguments.kwarg) if parameter]
    return [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, *variadic]


def IsAcceptedParameterName(name: str) -> bool:
    """A self or cls receiver, or a camelCase name; no framework context is needed for either."""
    return name in RECEIVER_NAMES or bool(CAMEL_CASE_PATTERN.match(name))


def IsPytestFixture(function: FunctionNode, module: SourceModule) -> bool:
    """A def decorated, bare or called, with pytest's or pytest-asyncio's resolved fixture."""
    for decorator in function.decorator_list:
        callee = decorator.func if isinstance(decorator, ast.Call) else decorator
        source, _, name = QualifiedName(callee, module).rpartition(".")
        if name == FIXTURE_DECORATOR_NAME and IsFixtureModule(source):
            return True
    return False


def BuiltinFixtureParameters(
    function: FunctionNode, scope: ast.AST, module: SourceModule
) -> frozenset[str]:
    """pytest's built-in fixture names, on a test-tree def that pytest injects fixtures into.

    pytest requests fixtures only for the tests it collects (IsTestFunction) and for fixture
    producers (IsPytestFixture). Every other def, and every lambda, names its parameters
    camelCase.
    """
    isInjected = IsPytestFixture(function, module) or IsTestFunction(function, scope, module)
    return PYTEST_BUILTIN_FIXTURE_PARAMETERS if IsInTestTree(module) and isInjected else frozenset()


def OwnerLabel(owner: ParameterOwner) -> str:
    """A def's name, or <lambda> for a lambda."""
    return getattr(owner, "name", LAMBDA_LABEL)


def ParameterViolations(
    owner: ParameterOwner, module: SourceModule, frameworkNames: frozenset[str]
) -> list[Violation]:
    """A parameter is camelCase unless the framework the owner implements declares or injects it."""
    label = OwnerLabel(owner)
    return [
        NameViolation(
            module, parameter, f"parameter {parameter.arg!r} of {label!r} is not camelCase"
        )
        for parameter in ParameterList(owner)
        if not IsAcceptedParameterName(parameter.arg) and parameter.arg not in frameworkNames
    ]


def CheckParameterNames(module: SourceModule) -> list[Violation]:
    """Def and lambda parameters are camelCase, except as a verified framework fixes them."""
    violations: list[Violation] = []
    for function, scope in IterFunctions(module.tree):
        frameworkNames = FrameworkParameterNames(function, scope, module)
        injectedNames = BuiltinFixtureParameters(function, scope, module)
        violations.extend(ParameterViolations(function, module, frameworkNames | injectedNames))
    for node in IterLambdas(module.tree):
        violations.extend(ParameterViolations(node, module, frozenset()))
    return violations


def IsFixtureModule(dottedName: str) -> bool:
    """pytest, pytest_asyncio, or a submodule of either, such as pytest_asyncio.plugin."""
    return dottedName.split(".")[0] in FIXTURE_DECORATOR_MODULES


def ImportedModuleNames(statement: ast.Import) -> Iterator[str]:
    """Names an import statement binds to a fixture module: import pytest_asyncio as pa binds pa."""
    for alias in statement.names:
        if IsFixtureModule(alias.name):
            yield alias.asname or alias.name.split(".")[0]


def ImportedFunctionNames(statement: ast.ImportFrom) -> Iterator[str]:
    """Names a from-import binds to fixture: from pytest import fixture as Fixture binds Fixture."""
    if statement.level == 0 and IsFixtureModule(statement.module or ""):
        for alias in statement.names:
            if alias.name == FIXTURE_DECORATOR_NAME:
                yield alias.asname or alias.name


def ModuleFixtureBindings(module: SourceModule) -> FixtureBindings:
    """The canonical spellings plus the names any import in the module binds to a fixture.

    Every import counts, including those inside functions and if TYPE_CHECKING blocks.
    """
    moduleNames = set(CANONICAL_FIXTURE_BINDINGS.moduleNames)
    functionNames = set(CANONICAL_FIXTURE_BINDINGS.functionNames)
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Import):
            moduleNames.update(ImportedModuleNames(node))
        elif isinstance(node, ast.ImportFrom):
            functionNames.update(ImportedFunctionNames(node))
    return FixtureBindings(frozenset(moduleNames), frozenset(functionNames))


def DottedRoot(node: ast.expr) -> str:
    """The leading identifier of a dotted name: pytest_asyncio in pytest_asyncio.plugin."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def IsFixtureDecoratorCallee(callee: ast.expr, bindings: FixtureBindings) -> bool:
    """fixture called through a name bound to it, or as an attribute of a fixture module."""
    if isinstance(callee, ast.Name):
        return callee.id in bindings.functionNames
    return (
        isinstance(callee, ast.Attribute)
        and callee.attr == FIXTURE_DECORATOR_NAME
        and DottedRoot(callee.value) in bindings.moduleNames
    )


def FixtureDecorator(
    function: FunctionNode, bindings: FixtureBindings = CANONICAL_FIXTURE_BINDINGS
) -> ast.expr | None:
    """The fixture decorator on a function, bare (@pytest.fixture) or called (@pytest.fixture()).

    Without a module's own bindings, only the canonical spellings are recognised.
    """
    for decorator in function.decorator_list:
        callee = decorator.func if isinstance(decorator, ast.Call) else decorator
        if IsFixtureDecoratorCallee(callee, bindings):
            return decorator
    return None


def FixtureAliasProblem(decorator: ast.expr) -> str:
    """Why a fixture decorator's name= alias breaks the convention, or "" when it is camelCase."""
    keywords = decorator.keywords if isinstance(decorator, ast.Call) else []
    aliases = [keyword.value for keyword in keywords if keyword.arg == FIXTURE_ALIAS_KEYWORD]
    if not aliases:
        return "declares no camelCase name= alias"
    if not isinstance(aliases[0], ast.Constant) or not isinstance(aliases[0].value, str):
        return "name= alias is not a string literal"
    if not FIXTURE_ALIAS_PATTERN.fullmatch(aliases[0].value):
        return f"name= alias {aliases[0].value!r} is not camelCase"
    return ""


def CheckFixtureNames(module: SourceModule) -> list[Violation]:
    """Each fixture declares, as a string literal, the camelCase name its consumers inject.

    The PascalCase form of the producing function itself is CheckFunctionNames' concern.
    """
    bindings = ModuleFixtureBindings(module)
    violations: list[Violation] = []
    for function, _ in IterFunctions(module.tree):
        decorator = FixtureDecorator(function, bindings)
        problem = FixtureAliasProblem(decorator) if decorator is not None else ""
        if problem:
            message = f"fixture {function.name!r} {problem}"
            violations.append(NameViolation(module, function, message))
    return violations


def BindingNodes(nodes: list[ast.AST]) -> Iterator[tuple[ast.AST, str]]:
    """Yield each name a scope binds, with the node that binds it.

    Assignment, loop, with, walrus and comprehension targets are Store-context Names; the
    others are except-handler names and match captures. Imports are not checked, and a type
    alias name is checked as a class name instead.
    """
    aliasIds = {id(node.name) for node in nodes if isinstance(node, ast.TypeAlias)}
    for node in nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if id(node) not in aliasIds:
                yield node, node.id
        elif isinstance(node, (ast.ExceptHandler, *MATCH_CAPTURE_TYPES)) and node.name:
            yield node, node.name
        elif isinstance(node, ast.MatchMapping) and node.rest:
            yield node, node.rest


def OuterDeclarations(nodes: list[ast.AST]) -> set[str]:
    """Names a scope declares global or nonlocal, which bind outside it."""
    return {
        name
        for node in nodes
        if isinstance(node, (ast.Global, ast.Nonlocal))
        for name in node.names
    }


def ScopeNamingViolations(
    scope: ast.AST, module: SourceModule, excludedIds: frozenset[int]
) -> list[Violation]:
    nodes = list(IterScopeNodes(scope))
    declared = OuterDeclarations(nodes)
    return [
        NameViolation(module, node, f"variable {name!r} is not camelCase")
        for node, name in BindingNodes(nodes)
        if id(node) not in excludedIds
        and name not in declared
        and not CAMEL_CASE_PATTERN.match(name)
    ]


def CheckLocalNames(module: SourceModule) -> list[Violation]:
    violations: list[Violation] = []
    for node in ast.walk(module.tree):
        if isinstance(node, (*FUNCTION_TYPES, ast.Lambda)):
            violations.extend(ScopeNamingViolations(node, module, frozenset()))
    return violations


def NestedStatements(statement: ast.stmt) -> list[ast.stmt]:
    nested = [*getattr(statement, "body", []), *getattr(statement, "orelse", [])]
    nested.extend(getattr(statement, "finalbody", []))
    for handler in getattr(statement, "handlers", []):
        nested.extend(handler.body)
    return nested


def TargetNames(target: ast.expr) -> Iterator[ast.Name]:
    if isinstance(target, ast.Name):
        yield target
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from TargetNames(element)
    elif isinstance(target, ast.Starred):
        yield from TargetNames(target.value)


def ModuleAssignmentTargets(statements: list[ast.stmt]) -> Iterator[tuple[ast.Name, ast.stmt]]:
    """Yield the names a module or class body's assignments bind, in if, try and with blocks too."""
    for statement in statements:
        if isinstance(statement, ASSIGNMENT_TYPES):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                yield from ((name, statement) for name in TargetNames(target))
        elif isinstance(statement, MODULE_BLOCK_TYPES):
            yield from ModuleAssignmentTargets(NestedStatements(statement))


@functools.cache
def TypingModules() -> frozenset[str]:
    """typing, and its typing_extensions backport when that package is importable."""
    try:
        importlib.import_module(TYPING_BACKPORT_MODULE)
    except ImportError:
        return frozenset({TYPING_MODULE})
    return frozenset({TYPING_MODULE, TYPING_BACKPORT_MODULE})


def IsTypingMember(expression: ast.expr, module: SourceModule, members: frozenset[str]) -> bool:
    """Whether expression, resolved where it is evaluated, names one of members in TypingModules."""
    source, _, member = QualifiedName(expression, module).rpartition(".")
    return source in TypingModules() and member in members


def IsTypeDeclaration(statement: ast.stmt, module: SourceModule) -> bool:
    """A typing factory call or TypeAlias annotation, each verified by what it resolves to."""
    value = getattr(statement, "value", None)
    if isinstance(value, ast.Call) and IsTypingMember(value.func, module, TYPE_FACTORY_CALLS):
        return True
    annotation = getattr(statement, "annotation", None)
    return annotation is not None and IsTypingMember(annotation, module, TYPE_ALIAS_ANNOTATIONS)


def ModuleNameExemptions(module: SourceModule) -> frozenset[str]:
    exemptions = set(MODULE_VARIABLE_EXEMPTIONS.get(module.relativePath, frozenset()))
    if IsAlembicVersion(module):
        exemptions.update(ALEMBIC_MODULE_VARIABLES)
    if IsInTestTree(module):
        exemptions.update(TEST_MODULE_VARIABLES)
    return frozenset(exemptions)


def IsAcceptedModuleName(name: str, statement: ast.stmt, module: SourceModule) -> bool:
    if UPPER_CASE_PATTERN.match(name) or DUNDER_PATTERN.match(name):
        return True
    if name in ModuleNameExemptions(module):
        return True
    return IsTypeDeclaration(statement, module) and bool(PASCAL_CASE_PATTERN.match(name))


def CheckModuleNames(module: SourceModule) -> list[Violation]:
    """Module assignments are constants; other module-level bindings are variables."""
    violations: list[Violation] = []
    constantIds: set[int] = set()
    for target, statement in ModuleAssignmentTargets(module.tree.body):
        constantIds.add(id(target))
        if not IsAcceptedModuleName(target.id, statement, module):
            message = f"module name {target.id!r} is not an UPPER_CASE constant"
            violations.append(NameViolation(module, target, message))
    violations.extend(ScopeNamingViolations(module.tree, module, frozenset(constantIds)))
    return violations


def ClassBodyNames(reference: ClassReference) -> frozenset[str]:
    """The names a class body binds: its attributes and fields, methods and nested classes."""
    names = {target.id for target, _ in ModuleAssignmentTargets(reference.node.body)}
    definitions = (*FUNCTION_TYPES, ast.ClassDef)
    statements = ModuleLevelStatements(reference.node.body)
    names.update(node.name for node in statements if isinstance(node, definitions))
    return frozenset(names)


def IsDeclaredMember(reference: ClassReference, name: str) -> bool:
    """Whether the class or a scanned ancestor binds name in its body, where it is checked."""
    return any(name in ClassBodyNames(ancestor) for ancestor in ProjectLineage(reference))


def IsStandInMember(reference: ClassReference, name: str) -> bool:
    """Whether the class a STAND_IN_CONTRACTS stand-in mirrors defines name.

    An imported class is read from its MRO; a scanned one from the class bodies of its scanned
    ancestors, then from the MROs of its imported ones.
    """
    key = (reference.module.relativePath, reference.node.name)
    contract = ResolveQualifiedName(STAND_IN_CONTRACTS[key]) if key in STAND_IN_CONTRACTS else None
    if isinstance(contract, ClassReference):
        return IsDeclaredMember(contract, name) or IsInheritedMember(contract, name)
    return isinstance(contract, type) and any(
        DefinesName(owner, name) for owner in contract.__mro__
    )


def IsContractMember(reference: ClassReference, name: str) -> bool:
    """A name an imported ancestor, or the class a stand-in mirrors, defines and so fixes."""
    return IsInheritedMember(reference, name) or IsStandInMember(reference, name)


def ExtendsFramework(reference: ClassReference, qualifiedName: str) -> bool:
    framework = ExternalClass(qualifiedName)
    ancestors = ExternalAncestors(reference)
    return framework is not None and any(issubclass(base, framework) for base in ancestors)


def FieldRule(reference: ClassReference) -> NamingRule:
    """How a class's fields are named: as its framework maps them, else as camelCase members."""
    for qualifiedName, rule in FRAMEWORK_FIELD_RULES:
        if ExtendsFramework(reference, qualifiedName):
            return rule
    return MEMBER_RULE


def IsClassVariable(statement: ast.stmt, module: SourceModule) -> bool:
    """An unannotated class-body assignment, or one annotated typing's ClassVar or Final."""
    annotation = getattr(statement, "annotation", None)
    if annotation is None:
        return True
    base = annotation.value if isinstance(annotation, ast.Subscript) else annotation
    return QualifiedName(base, module) in CLASS_VARIABLE_ANNOTATIONS


def ClassVariableRule(value: ast.expr | None, reference: ClassReference) -> NamingRule:
    """A lambda or wrapped function is a method and a property a member; the rest are constants."""
    if isinstance(value, ast.Lambda):
        return METHOD_RULE
    callee = QualifiedName(value.func, reference.module) if isinstance(value, ast.Call) else ""
    if callee in VALUE_CALL_RULES:
        return VALUE_CALL_RULES[callee]
    return ORM_ATTRIBUTE_RULE if ExtendsFramework(reference, ORM_BASE_CLASS) else CONSTANT_RULE


def ClassAttributeRule(
    name: str, statement: ast.stmt, reference: ClassReference
) -> NamingRule | None:
    """The rule a class-body assignment target follows; None when Python or a contract fixes it."""
    if DUNDER_PATTERN.match(name) or IsContractMember(reference, name):
        return None
    if IsClassVariable(statement, reference.module):
        return ClassVariableRule(getattr(statement, "value", None), reference)
    return FieldRule(reference)


def ClassBodyViolations(reference: ClassReference) -> list[Violation]:
    """Class-body assignments follow ClassAttributeRule; other class bindings are variables."""
    module, className = reference.module, reference.node.name
    violations: list[Violation] = []
    assignedIds: set[int] = set()
    for target, statement in ModuleAssignmentTargets(reference.node.body):
        assignedIds.add(id(target))
        rule = ClassAttributeRule(target.id, statement, reference)
        if rule is not None and not rule.Accepts(target.id):
            message = f"class attribute {target.id!r} of {className!r} is not {rule.form}"
            violations.append(NameViolation(module, target, message))
    violations.extend(ScopeNamingViolations(reference.node, module, frozenset(assignedIds)))
    return violations


def MethodReceiver(statement: ast.stmt, module: SourceModule) -> str:
    """The receiver parameter of a def in a class body; "" for a staticmethod or any other node."""
    if not isinstance(statement, FUNCTION_TYPES):
        return ""
    decoratorNames = {QualifiedName(decorator, module) for decorator in statement.decorator_list}
    positional = [*statement.args.posonlyargs, *statement.args.args]
    isStatic = STATICMETHOD_DECORATOR in decoratorNames
    return positional[0].arg if positional and not isStatic else ""


def ReceiverScopeNodes(method: FunctionNode, receiver: str) -> Iterator[ast.AST]:
    """The method body's nodes in which the receiver name still means the receiver.

    A nested def or lambda that rebinds the name as a parameter is not entered, nor is a nested
    class, whose methods have receivers of their own.
    """
    pending: list[ast.AST] = list(method.body)
    while pending:
        node = pending.pop()
        yield node
        isScope = isinstance(node, (*FUNCTION_TYPES, ast.Lambda))
        rebinds = isScope and receiver in {parameter.arg for parameter in ParameterList(node)}
        if not rebinds and not isinstance(node, ast.ClassDef):
            pending.extend(ast.iter_child_nodes(node))


def IsReceiverStore(node: ast.AST, receiver: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == receiver
    )


def ReceiverStores(
    method: FunctionNode, receiver: str
) -> list[tuple[ast.Attribute, ast.expr | None]]:
    """Each attribute a method stores on its receiver, with the value assigned to it directly."""
    nodes = list(ReceiverScopeNodes(method, receiver))
    values = AssignedValues(nodes)
    return [(node, values.get(id(node))) for node in nodes if IsReceiverStore(node, receiver)]


def AssignedValues(nodes: Iterable[ast.AST]) -> dict[int, ast.expr | None]:
    """The value each direct target of an assignment among nodes receives, by target identity."""
    return {
        id(target): node.value
        for node in nodes
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
    }


def IsMethodStandIn(value: ast.expr | None, module: SourceModule) -> bool:
    """A lambda, or a unittest.mock Mock, MagicMock or AsyncMock: a stand-in for a method."""
    if isinstance(value, ast.Lambda):
        return True
    return isinstance(value, ast.Call) and (
        QualifiedName(value.func, module) in METHOD_STAND_IN_CLASSES
    )


def IsAcceptedReceiverAttribute(
    target: ast.Attribute, value: ast.expr | None, reference: ClassReference
) -> bool:
    """A camelCase member, a PascalCase method stand-in, or a contract-fixed or declared name.

    A name the class lineage declares in its body (IsDeclaredMember) is checked there, once.
    """
    name = target.attr
    if MEMBER_CASE_PATTERN.match(name) or DUNDER_PATTERN.match(name):
        return True
    if IsMethodStandIn(value, reference.module) and PASCAL_CASE_PATTERN.match(name):
        return True
    return IsContractMember(reference, name) or IsDeclaredMember(reference, name)


def InstanceAttributeProblem(
    target: ast.Attribute, value: ast.expr | None, reference: ClassReference
) -> str:
    """Why an attribute stored on an instance of a scanned class breaks member naming, or ""."""
    if IsAcceptedReceiverAttribute(target, value, reference):
        return ""
    return f"instance attribute {target.attr!r} of {reference.node.name!r} is not camelCase"


def ReceiverAttributeViolations(reference: ClassReference) -> list[Violation]:
    """Attributes a class's methods, in if, try and with blocks too, store on their receiver."""
    violations: list[Violation] = []
    for statement in ModuleLevelStatements(reference.node.body):
        receiver = MethodReceiver(statement, reference.module)
        for target, value in ReceiverStores(statement, receiver) if receiver else []:
            message = InstanceAttributeProblem(target, value, reference)
            if message:
                violations.append(NameViolation(reference.module, target, message))
    return violations


def ClassStoreProblem(name: str, value: ast.expr | None, reference: ClassReference) -> str:
    """Why storing name on a scanned class breaks the class-variable rules, or "" when it does not.

    A dunder, a contract member and a name the class lineage declares are accepted.
    """
    if DUNDER_PATTERN.match(name) or IsContractMember(reference, name):
        return ""
    rule = ClassVariableRule(value, reference)
    if rule.Accepts(name) or IsDeclaredMember(reference, name):
        return ""
    return f"class attribute {name!r} of {reference.node.name!r} is not {rule.form}"


def InstanceDeclaration(binding: object, module: SourceModule) -> ast.expr | None:
    """The class expression that types a binding, or None for a binding nothing types.

    A parameter or annotated name is typed by its annotation, and a name assigned straight from a
    call by the callee.
    """
    if isinstance(binding, ast.arg):
        return binding.annotation
    statement = ParentMap(module).get(id(binding)) if isinstance(binding, ast.Name) else None
    if isinstance(statement, ast.AnnAssign):
        return statement.annotation
    isCall = isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Call)
    isDirect = isCall and any(target is binding for target in statement.targets)
    return statement.value.func if isDirect else None


def InstanceBindingClass(binding: object, module: SourceModule) -> ClassReference | None:
    """The scanned class whose instance a binding holds, by its InstanceDeclaration."""
    declaration = InstanceDeclaration(binding, module)
    owner = ResolveClassExpression(declaration, module) if declaration is not None else None
    return owner if isinstance(owner, ClassReference) else None


def InstanceClass(expression: ast.expr, module: SourceModule) -> ClassReference | None:
    """The scanned class a name holds an instance of, when all its bindings in scope agree."""
    if not isinstance(expression, ast.Name):
        return None
    scope = EnclosingScope(module, expression)
    bindings = NameBindings(module, scope, expression.id) or ()
    classes = {InstanceBindingClass(binding, module) for binding in bindings}
    return classes.pop() if len(classes) == 1 else None


def AttributeStoreProblem(
    target: ast.Attribute, value: ast.expr | None, module: SourceModule
) -> str:
    """Why an attribute store on a scanned class or on an instance of one breaks naming, or ""."""
    owner = ResolveClassExpression(target.value, module) if DottedText(target.value) else None
    if isinstance(owner, ClassReference):
        return ClassStoreProblem(target.attr, value, owner)
    instanceClass = InstanceClass(target.value, module)
    return InstanceAttributeProblem(target, value, instanceClass) if instanceClass else ""


def AttributeStoreViolations(module: SourceModule) -> list[Violation]:
    """Attributes stored on a scanned class, or on a name that holds only instances of one.

    A store on any other object, whose class is imported or not statically known, is unchecked.
    """
    nodes = list(ast.walk(module.tree))
    values = AssignedValues(nodes)
    violations: list[Violation] = []
    for node in nodes:
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            message = AttributeStoreProblem(node, values.get(id(node)), module)
            if message:
                violations.append(NameViolation(module, node, message))
    return violations


def CheckMemberNames(module: SourceModule) -> list[Violation]:
    """Every class's body bindings, and attributes stored on scanned classes and instances.

    A receiver store that AttributeStoreViolations also reaches is reported once.
    """
    violations = set(AttributeStoreViolations(module))
    for node in ast.walk(module.tree):
        if isinstance(node, ast.ClassDef):
            reference = ClassReference(module, node)
            violations.update(ClassBodyViolations(reference))
            violations.update(ReceiverAttributeViolations(reference))
    return sorted(violations)


def IsDocstring(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def StatementStart(statement: ast.stmt) -> int:
    """First line of a statement; a decorated definition starts at its first decorator."""
    decorators = getattr(statement, "decorator_list", [])
    return min([statement.lineno, *(decorator.lineno for decorator in decorators)])


def FunctionBodySpan(function: FunctionNode) -> int:
    """Lines from the first body statement after the docstring to the end of the last one.

    The signature, decorators and docstring are excluded; blank lines and comments between the
    first and last statements count.
    """
    body = function.body[1:] if IsDocstring(function.body[0]) else function.body
    if not body:
        return 0
    return body[-1].end_lineno - StatementStart(body[0]) + 1


def BodySpan(owner: ParameterOwner) -> int:
    """A def's FunctionBodySpan, or the lines a lambda's expression body spans."""
    if isinstance(owner, ast.Lambda):
        return owner.body.end_lineno - owner.body.lineno + 1
    return FunctionBodySpan(owner)


def CheckFunctionLength(module: SourceModule) -> list[Violation]:
    """Every def and lambda body spans at most MAX_FUNCTION_BODY_LINES lines."""
    owners = [function for function, _ in IterFunctions(module.tree)]
    violations: list[Violation] = []
    for owner in [*owners, *IterLambdas(module.tree)]:
        span = BodySpan(owner)
        if span > MAX_FUNCTION_BODY_LINES:
            message = (
                f"function {OwnerLabel(owner)!r} body spans {span} lines"
                f" (limit {MAX_FUNCTION_BODY_LINES})"
            )
            violations.append(NameViolation(module, owner, message))
    return violations


def CountedParameterNames(owner: ParameterOwner) -> tuple[str, ...]:
    """Positional and keyword-only parameter names, without variadics or a def's self/cls receiver.

    A lambda has no receiver, so each of its parameters counts.
    """
    positional = [*owner.args.posonlyargs, *owner.args.args]
    if isinstance(owner, FUNCTION_TYPES) and positional and positional[0].arg in RECEIVER_NAMES:
        positional = positional[1:]
    return tuple(parameter.arg for parameter in (*positional, *owner.args.kwonlyargs))


def HasFrameworkSignature(function: FunctionNode, scope: ast.AST, module: SourceModule) -> bool:
    """A class-body override declaring exactly the counted parameters of the member it overrides."""
    if not isinstance(scope, ast.ClassDef):
        return False
    member = InheritedMember(ClassReference(module, scope), function.name)
    return member is not None and CountedParameterNames(function) == FrameworkCountedNames(member)


def CheckParameterCount(module: SourceModule) -> list[Violation]:
    """Every def and lambda takes at most MAX_PARAMETERS counted parameters."""
    owners: list[ParameterOwner] = [
        function
        for function, scope in IterFunctions(module.tree)
        if not HasFrameworkSignature(function, scope, module)
    ]
    violations: list[Violation] = []
    for owner in [*owners, *IterLambdas(module.tree)]:
        count = len(CountedParameterNames(owner))
        if count > MAX_PARAMETERS:
            message = (
                f"function {OwnerLabel(owner)!r} takes {count} parameters (limit {MAX_PARAMETERS})"
            )
            violations.append(NameViolation(module, owner, message))
    return violations


def HeaderViolations(module: SourceModule) -> list[Violation]:
    """A ported module's docstring carries the Replaces: label and a legacy artefact name."""
    if module.path.name == "__init__.py" or not IsUnder(module, HEADER_ROOTS):
        return []
    docstring = ast.get_docstring(module.tree) or ""
    namedArtefacts = set(LEGACY_WORD_PATTERN.findall(docstring)) & LegacyArtefactNames()
    if HEADER_LABEL in docstring and namedArtefacts:
        return []
    message = f"module docstring lacks a {HEADER_LABEL!r} header naming a legacy artefact"
    return [Violation(module.relativePath, 1, message)]


def TableCells(line: str) -> list[str]:
    return [cell.strip().strip("`").strip() for cell in line.strip().strip("|").split("|")]


def IsHeaderOrSeparator(cells: list[str]) -> bool:
    if cells[:1] == [PROGRAM_COLUMN_HEADER]:
        return True
    return all(TABLE_SEPARATOR_PATTERN.match(cell) for cell in cells)


def ProgramMapRows(lines: list[str], start: int) -> list[tuple[int, list[str]]]:
    """Table rows after the heading at index start, up to a blank line or the next heading."""
    rows: list[tuple[int, list[str]]] = []
    inTable = False
    for lineNumber, rawLine in enumerate(lines[start + 1 :], start=start + 2):
        line = rawLine.strip()
        if line.startswith("#") or (inTable and not line):
            break
        if line.startswith("|"):
            inTable = True
            rows.append((lineNumber, TableCells(line)))
    return [(lineNumber, cells) for lineNumber, cells in rows if not IsHeaderOrSeparator(cells)]


def ReadProgramMap(text: str) -> tuple[dict[str, str], list[Violation]]:
    """Parse the program-to-module table, reporting a missing heading and duplicate rows."""
    lines = text.splitlines()
    headings = [index for index, line in enumerate(lines) if line.strip() == PROGRAM_MAP_HEADING]
    if not headings:
        message = f"heading {PROGRAM_MAP_HEADING!r} not found"
        return {}, [Violation(TRACEABILITY_RELATIVE_PATH, 0, message)]
    programMap: dict[str, str] = {}
    violations: list[Violation] = []
    for lineNumber, cells in ProgramMapRows(lines, headings[0]):
        program, modulePath = [*cells, "", ""][:2]
        if program in programMap:
            message = f"program {program} is listed more than once"
            violations.append(Violation(TRACEABILITY_RELATIVE_PATH, lineNumber, message))
        programMap[program] = modulePath
    return programMap, violations


def MapDifferenceViolations(programMap: dict[str, str]) -> list[Violation]:
    messages = [
        f"program {program} is missing from the map"
        for program in sorted(EXPECTED_PROGRAM_MODULES.keys() - programMap.keys())
    ]
    messages.extend(
        f"program {program} is not a ported program"
        for program in sorted(programMap.keys() - EXPECTED_PROGRAM_MODULES.keys())
    )
    messages.extend(
        f"program {program} maps to {programMap[program]}, expected"
        f" {EXPECTED_PROGRAM_MODULES[program]}"
        for program in sorted(programMap.keys() & EXPECTED_PROGRAM_MODULES.keys())
        if programMap[program] != EXPECTED_PROGRAM_MODULES[program]
    )
    return [Violation(TRACEABILITY_RELATIVE_PATH, 0, message) for message in messages]


def SharedModuleViolations(programMap: dict[str, str]) -> list[Violation]:
    """One program, one module: no module path may port two programs."""
    programsByModule: dict[str, list[str]] = {}
    for program, modulePath in sorted(programMap.items()):
        programsByModule.setdefault(modulePath, []).append(program)
    return [
        Violation(TRACEABILITY_RELATIVE_PATH, 0, f"{modulePath} ports {', '.join(programs)}")
        for modulePath, programs in sorted(programsByModule.items())
        if len(programs) > 1
    ]


def ModuleFileViolations() -> list[Violation]:
    """Each expected module exists and its docstring names its program as a whole word."""
    modulesByPath = {module.relativePath: module for module in LoadModules()}
    violations: list[Violation] = []
    for program, modulePath in sorted(EXPECTED_PROGRAM_MODULES.items()):
        module = modulesByPath.get(modulePath)
        if module is None:
            violations.append(Violation(modulePath, 0, f"module porting {program} is missing"))
        elif module.tree is not None:
            docstring = ast.get_docstring(module.tree) or ""
            if not re.search(rf"\b{program}\b", docstring):
                message = f"module docstring does not name its program {program}"
                violations.append(Violation(modulePath, 1, message))
    return violations


def ProgramMapViolations() -> list[Violation]:
    try:
        text = TRACEABILITY_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [Violation(TRACEABILITY_RELATIVE_PATH, 0, "traceability document is missing")]
    programMap, violations = ReadProgramMap(text)
    violations.extend(MapDifferenceViolations(programMap))
    violations.extend(SharedModuleViolations(programMap))
    violations.extend(ModuleFileViolations())
    return sorted(violations)


NAMING_CHECKERS: tuple[Checker, ...] = (
    CheckClassNames,
    CheckFunctionNames,
    CheckParameterNames,
    CheckFixtureNames,
    CheckLocalNames,
    CheckModuleNames,
    CheckMemberNames,
)
SIZE_CHECKERS: tuple[Checker, ...] = (CheckFunctionLength, CheckParameterCount)
ALL_CHECKERS: tuple[Checker, ...] = (*NAMING_CHECKERS, *SIZE_CHECKERS, HeaderViolations)


def CheckSource(text: str, relativePath: str) -> list[Violation]:
    """Run every module checker over in-test source placed at relativePath."""
    module = ParseSource(text, relativePath)
    return sorted(violation for checker in ALL_CHECKERS for violation in checker(module))


def AssertNoViolations(violations: list[Violation]) -> None:
    assert violations == [], FormatViolations(violations)


def NumberedFunctionSource(name: str, bodyLines: int) -> str:
    """A function with a two-line docstring followed by bodyLines one-line statements."""
    statements = "".join(f"    value{index} = {index}\n" for index in range(bodyLines))
    return f'def {name}():\n    """Docstring line one.\n    Docstring line two."""\n{statements}'


def NumberedDefSource(bodyLines: int) -> str:
    """The NumberedFunctionSource def named Sample."""
    return NumberedFunctionSource("Sample", bodyLines)


def NumberedLambdaSource(bodyLines: int) -> str:
    """A lambda whose parenthesised expression body spans bodyLines lines."""
    terms = "".join(f"    + {index}\n" for index in range(1, bodyLines))
    return f"SAMPLE = lambda value: (\n    value\n{terms})\n"


VIOLATING_PATH = "backend/app/services/demo/violations.py"
VIOLATING_SOURCE = textwrap.dedent(
    """
    import ast
    import logging
    from dataclasses import dataclass
    from typing import ClassVar

    from pydantic import BaseModel
    from pydantic_settings import BaseSettings

    lowerConstant = 1


    class lower_case:
        value = 1


    def snake_name():
        account_id = 1
        return account_id


    def TakesSnakeParameter(account_id):
        return account_id


    def FiveParameters(first, second, third, fourth, fifth):
        return first


    class FiveParameterRecord:
        def __init__(self, record_id, second, third, fourth, fifth):
            self.recordId = record_id


    def useFiveParameters(first, second, third, fourth, fifth):
        return first


    def useSnakeParameter(account_id):
        return account_id


    def _five_parameters_(first, second, third, fourth, fifth):
        return first


    class FiveParameterMeta(type):
        def Build(mcs, first, second, third, fourth):
            return first


    class FiveParameterOverrides:
        def filter(self, first, second, third, fourth, fifth):
            return first

        def copy(self, first, second, third, fourth, fifth):
            return first

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            extra_settings,
        ):
            return (init_settings,)


    def useHelper():
        return 1


    def _ordinary_helper_():
        return 1


    class PlainRecord:
        def copy(self, snake_parameter):
            return snake_parameter


    class PlainSettings:
        @classmethod
        def settings_customise_sources(
            cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
        ):
            return (init_settings,)


    class RenamedRecordFilter(logging.Filter):
        def filter(self, log_record):
            return True


    def pytest_configure(config):
        return config


    def ReadRows(pair, _unusedFlag):
        _header, rows = pair
        return rows


    class OrdinaryRecord:
        account_balance: int = 0


    @dataclass
    class DataRecord:
        account_balance: int


    class BalanceHolder:
        def __init__(self, callback):
            self.account_balance = 1
            self.Callback = callback


    class LoginLimits:
        maxAttempts = 5
        retryLimit: ClassVar[int] = 3


    class LoopRecord:
        for loop_index in range(2):
            pass


    class CamelKeyModel(BaseModel):
        acctId: str


    class LowerCaseSettings(BaseSettings):
        secret_key: str = ""


    class NotANodeVisitor(ast.NodeVisitor):
        def visit_NotANode(self, node):
            return node

        def visit_Name(self, name_node):
            return name_node


    class PlainVisitor:
        def visit_FunctionDef(self, node):
            return node


    class StoredRecord:
        pass


    class OpenLimits:
        pass


    OpenLimits.maxAttempts = 5
    OpenLimits.handler = lambda: 1
    OpenLimits.retry_count, OpenLimits.MAX_RETRIES = 1, 2


    def BuildStoredRecord():
        record = StoredRecord()
        record.account_balance = 1
        return record


    def FillStoredRecord(record: StoredRecord, factory):
        record.total_count = 1
        annotated: StoredRecord = factory()
        annotated.annotated_value = 1


    class GuardedRecord:
        if True:

            def Set(self):
                self.account_balance = 1


    class AccountModel(BaseModel):
        acct_id: str

        def Set(self, value):
            self.acct_id = value
            self.other_field = value


    """
) + NumberedFunctionSource("LongFunction", MAX_FUNCTION_BODY_LINES + 1)
VIOLATING_TEST_PATH = "backend/tests/unit/test_violations.py"
VIOLATING_TEST_SOURCE = textwrap.dedent(
    """
    import pytest
    import pytest as pt
    import pytest_asyncio
    import pytest_asyncio as pa
    import pytest_asyncio.plugin
    from pytest import fixture
    from pytest import fixture as SyncFixture
    from pytest_asyncio import fixture as AsyncFixture
    from pydantic_settings import BaseSettings

    ALIAS_NAME = "nonLiteralAlias"


    @pytest.fixture(name="snake_fixture")
    def SnakeAliasFixture():
        return 1


    @pytest.fixture
    def ImplicitNameFixture():
        return 1


    @pytest_asyncio.fixture(scope="module")
    async def AsyncImplicitFixture():
        return 1


    @fixture(name="PascalAlias")
    def PascalAliasFixture():
        return 1


    @pytest.fixture(name=ALIAS_NAME)
    def NonLiteralAliasFixture():
        return 1


    @pytest.fixture(name="snakeProducer")
    def snake_producer():
        return 1


    @pt.fixture(name="aliased_snake")
    def AliasedModuleSnakeFixture():
        return 1


    @pa.fixture
    async def AliasedModuleImplicitFixture():
        return 1


    @pytest_asyncio.plugin.fixture
    async def SubmoduleImplicitFixture():
        return 1


    @SyncFixture(name="renamed_snake")
    def RenamedSnakeFixture():
        return 1


    @AsyncFixture
    async def RenamedImplicitFixture():
        return 1


    @pytest.fixture(name="newlineAlias\\n")
    def NewlineAliasFixture():
        return 1


    @pytest.fixture(name="_privateAlias")
    def PrivateAliasFixture():
        return 1


    @pytest.fixture(name="hookProducer")
    def useHookProducer():
        return 1


    @pytest.fixture(name="testProducer")
    def test_producer():
        return 1


    def test_x(account_id):
        return account_id


    def test_five(one, two, three, four, five):
        return one


    def RunsNestedTest():
        def test_nested():
            return 1

        return test_nested()


    def pytest_not_a_hook():
        return 1


    def pytest_configure(config, plugin_value):
        return config


    class MismatchedSettings(BaseSettings):
        @classmethod
        def settings_customise_sources(
            cls, settings_cls, init_settings, env_settings, dotenv_settings, secret_settings
        ):
            return (init_settings,)


    def MakeFile(tmp_path):
        return tmp_path


    MAKE_PATH = lambda tmp_path: tmp_path


    class TestParameters:
        def setup_method(self, bound_method):
            return bound_method

        def test_snake(self, account_id):
            return account_id

        class TestInnerClass:
            def test_inner_class_method(self):
                return 1


    class TestConstructed:
        def __init__(self):
            self.ready = True

        def setup_method(self):
            self.ready = True

        def test_constructed(self, tmp_path):
            return tmp_path


    class TestMapping(dict):
        def test_mapping(self):
            return 1


    def setup_class():
        return None
    """
)
# A test-tree module that pytest does not collect, so neither its test_* function nor the
# test_* method of its Test* class is a test.
VIOLATING_HELPER_PATH = "backend/tests/unit/helpers.py"
VIOLATING_HELPER_SOURCE = textwrap.dedent(
    """
    def test_helper():
        return 1


    def setup_module(module):
        return module


    class TestHelper:
        def teardown_method(self):
            return None

        def test_method(self):
            return 1
    """
)
# A STAND_IN_CONTRACTS stand-in keeps only the names of the class it mirrors.
VIOLATING_STAND_IN_PATH = "backend/tests/unit/core/test_logging.py"
VIOLATING_STAND_IN_SOURCE = "class FailingCloseConnection:\n    reset_all = staticmethod(print)\n"
# A base resolves only where Python evaluates it, so a parameter that shadows the framework
# import, or an import made inside another function, leaves the override unverified.
VIOLATING_SHADOWED_BASE_PATH = "backend/app/core/shadowed_base.py"
VIOLATING_SHADOWED_BASE_SOURCE = textwrap.dedent(
    """
    from pydantic_settings import BaseSettings as baseClass


    def Build(baseClass):
        class Ordinary(baseClass):
            @classmethod
            def settings_customise_sources(
                cls, settings_cls, init_settings, env_settings,
                dotenv_settings, file_secret_settings
            ):
                return (init_settings,)

        return Ordinary
    """
)
VIOLATING_UNRELATED_IMPORT_PATH = "backend/app/core/unrelated_import.py"
VIOLATING_UNRELATED_IMPORT_SOURCE = textwrap.dedent(
    """
    def LoadFramework():
        from pydantic_settings import BaseSettings as baseClass

        return baseClass


    def Build():
        class Ordinary(baseClass):
            @classmethod
            def settings_customise_sources(
                cls, settings_cls, init_settings, env_settings,
                dotenv_settings, file_secret_settings
            ):
                return (init_settings,)

        return Ordinary
    """
)
# Decorators, wrapper and type factory calls and annotations count by what they resolve to, never
# by spelling.
VIOLATING_SPELLED_PATH = "backend/app/core/spelled_decorators.py"
VIOLATING_SPELLED_SOURCE = textwrap.dedent(
    """
    from pydantic import BaseModel

    from app.core import helpers
    from app.core.helpers import TypeAlias


    def TypeVar(name):
        return name


    SpelledMode = TypeVar("SpelledMode")
    SpelledHandle = helpers.NewType("SpelledHandle", int)


    class SpelledAccessors:
        @helpers.property
        def accountBalance(self):
            return 0

        def Plain(self):
            return 0

        @Plain.setter
        def plainSetter(self, value):
            return value

        cachedTotal = helpers.property(Plain)

        @helpers.staticmethod
        def Store(self):
            self.stored_value = 1


    class SpelledModel(BaseModel):
        RETRY_LIMIT: helpers.ClassVar[int] = 3


    SpelledAlias: TypeAlias = SpelledModel
    """
)
VIOLATING_SPELLED_FIXTURE_PATH = "backend/tests/unit/test_spelled_fixture.py"
VIOLATING_SPELLED_FIXTURE_SOURCE = textwrap.dedent(
    """
    from app.core.helpers import fixture


    @fixture(name="spelledFixture")
    def SpelledFixture(tmp_path):
        return tmp_path
    """
)
VIOLATING_SOURCES = {
    VIOLATING_PATH: VIOLATING_SOURCE,
    VIOLATING_SPELLED_PATH: VIOLATING_SPELLED_SOURCE,
    VIOLATING_SPELLED_FIXTURE_PATH: VIOLATING_SPELLED_FIXTURE_SOURCE,
    VIOLATING_TEST_PATH: VIOLATING_TEST_SOURCE,
    VIOLATING_HELPER_PATH: VIOLATING_HELPER_SOURCE,
    VIOLATING_STAND_IN_PATH: VIOLATING_STAND_IN_SOURCE,
    VIOLATING_SHADOWED_BASE_PATH: VIOLATING_SHADOWED_BASE_SOURCE,
    VIOLATING_UNRELATED_IMPORT_PATH: VIOLATING_UNRELATED_IMPORT_SOURCE,
}

EXEMPT_SOURCES = (
    pytest.param(
        """
        import pytest
        import pytest_asyncio
        from pytest import fixture

        pytestmark = []


        @pytest.fixture(name="demoFixture")
        def DemoFixture(tmp_path):
            return tmp_path


        @pytest_asyncio.fixture(name="asyncDemo", scope="module")
        async def AsyncDemo():
            return 1


        @fixture(name="bareDemo")
        def BareDemo():
            return 1


        @pytest.mark.usefixtures("demoFixture")
        def test_x(demoFixture, asyncDemo, bareDemo):
            return demoFixture
        """,
        "backend/tests/unit/test_demo.py",
        id="pytest-fixture-aliases-test-function-and-builtin-fixture",
    ),
    pytest.param(
        """
        import pytest as pt
        import pytest_asyncio.plugin
        from pytest_asyncio import fixture as asyncFixture


        @pt.fixture(name="aliasedDemo")
        def AliasedDemo():
            return 1


        @asyncFixture(name="renamedDemo")
        async def RenamedDemo():
            return 1


        @pytest_asyncio.plugin.fixture(name="submoduleDemo")
        async def SubmoduleDemo():
            return 1


        @pt.mark.usefixtures("aliasedDemo")
        def test_x(aliasedDemo, renamedDemo, submoduleDemo):
            return aliasedDemo
        """,
        "backend/tests/unit/test_demo_aliases.py",
        id="aliased-and-renamed-fixture-imports-and-aliased-usefixtures-mark",
    ),
    pytest.param(
        """
        import sys


        class TestThing:
            def test_one(self, tmp_path):
                return tmp_path

            @staticmethod
            def test_static(tmp_path_factory):
                return tmp_path_factory


        if sys.version_info >= (3, 13):

            class TestGuardedThing(object):
                def test_guarded(self, record_property):
                    return record_property
        """,
        "backend/tests/unit/test_demo_classes.py",
        id="collected-test-class-methods-and-builtin-fixtures",
    ),
    pytest.param(
        """
        revision = "0001"
        down_revision = None


        def upgrade():
            return None


        def downgrade():
            return None
        """,
        "backend/alembic/versions/0001_demo.py",
        id="alembic-revision",
    ),
    pytest.param("app = CreateApp()\n", "backend/app/main.py", id="uvicorn-target"),
    pytest.param(
        """
        import logging
        import typing
        from typing import TypeAlias, TypeVar

        import typing_extensions

        __all__ = ["MaskingFilter", "OnlineContext"]
        T = TypeVar("T")
        Handle = typing.NewType("Handle", int)
        Params = typing_extensions.ParamSpec("Params")


        class MaskingFilter(logging.Filter):
            def filter(self, record):
                return True


        class OnlineContext:
            @property
            def engine(self):
                return None


        ContextAlias: TypeAlias = OnlineContext
        """,
        "backend/app/core/demo.py",
        id="logging-filter-property-typevar-dunder",
    ),
    pytest.param(
        """
        from pydantic_settings import BaseSettings


        class DemoSettings(BaseSettings):
            @classmethod
            def settings_customise_sources(
                cls,
                settings_cls,
                init_settings,
                env_settings,
                dotenv_settings,
                file_secret_settings,
            ):
                return (init_settings,)
        """,
        "backend/app/core/demo_settings.py",
        id="pydantic-settings-fixed-signature",
    ),
    pytest.param(
        """
        import enum

        import click


        class DemoCommand(click.Command):
            def make_context(self, info_name, args, parent=None, **extra):
                return super().make_context(info_name, args, parent, **extra)


        class DemoResource:
            def __exit__(self, excType, excValue, traceback):
                return None


        class DemoStatus(enum.Enum):
            ACTIVE = "Y"

            @classmethod
            def _missing_(cls, value):
                return None
        """,
        "backend/app/core/demo_overrides.py",
        id="framework-fixed-parameter-names",
    ),
    pytest.param(
        """
        import sqlalchemy as sa


        class EscapedText(sa.types.TypeDecorator):
            impl = sa.Text
            cache_ok = True

            def process_bind_param(self, value, dialect):
                return value

            def literal_processor(self, dialect):
                return None
        """,
        "backend/alembic/versions/0003_demo.py",
        id="sqlalchemy-type-decorator-override",
    ),
    pytest.param(
        """
        def pytest_configure(config):
            return None


        def pytest_warning_recorded(warning_message, when, nodeid, location):
            return None
        """,
        "batch/tests/conftest.py",
        id="pytest-hooks-in-conftest",
    ),
    pytest.param(
        """
        import enum
        from typing import ClassVar
        from unittest.mock import AsyncMock

        import sqlalchemy as sa
        from pydantic import BaseModel, ConfigDict
        from pydantic_settings import BaseSettings, SettingsConfigDict
        from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


        class DemoBase(DeclarativeBase):
            pass


        class DemoAccount(DemoBase):
            __tablename__ = "accounts"
            __table_args__ = {"schema": "carddemo"}

            acct_id: Mapped[str] = mapped_column(sa.String(11), primary_key=True)
            curr_bal = mapped_column(sa.Numeric(12, 2))


        class DemoResponse(BaseModel):
            model_config = ConfigDict(extra="forbid")

            acct_id: str
            cursor_field: str = ""


        class DemoSettings(BaseSettings):
            model_config = SettingsConfigDict(env_file=None)

            SECRET_KEY: str = ""


        class DemoStatus(enum.Enum):
            ACTIVE = "Y"


        class DemoContext:
            MAX_RETRIES: ClassVar[int] = 3
            requestCount: int = 0

            def Engine(self):
                return None

            engine = property(Engine)


        class StubRepository:
            def __init__(self):
                self.GetCard = AsyncMock(return_value=None)
                self.Lookup = lambda key: key
                self.callCount = 0
                self._cache = {}
        """,
        "backend/app/core/demo_members.py",
        id="orm-pydantic-settings-enum-property-and-method-stand-in-members",
    ),
    pytest.param(
        "class FailingCloseConnection:\n    close = staticmethod(print)\n",
        "backend/tests/unit/core/test_logging.py",
        id="stand-in-contract-member",
    ),
    pytest.param(
        """
        import sys
        from typing import TYPE_CHECKING

        if TYPE_CHECKING or sys.version_info >= (3, 13):
            from logging import Filter


        class GuardedFilter(Filter):
            def filter(self, record):
                return True


        def BuildHandler():
            import logging

            class RecordHandler(logging.Handler):
                def emit(self, record):
                    return None

            return RecordHandler
        """,
        "backend/app/core/demo_scoped_imports.py",
        id="guarded-module-import-and-function-local-import-bases",
    ),
    pytest.param(
        """
        import ast


        class NameCollector(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                return node.name


        class NameRewriter(ast.NodeTransformer):
            def visit_Name(self, node):
                return node
        """,
        "backend/app/core/demo_visitors.py",
        id="ast-node-visitor-callbacks",
    ),
    pytest.param(
        """
        def setup_module(module):
            return None


        def teardown_function(function):
            return None


        class TestRecord:
            @classmethod
            def setup_class(cls):
                cls.shared = True

            def setup_method(self, method):
                self.ready = True

            def teardown_method(self):
                self.ready = False

            def test_ready(self):
                assert self.ready
        """,
        "backend/tests/unit/test_demo_xunit.py",
        id="pytest-xunit-setup-and-teardown",
    ),
    pytest.param(
        """
        from pydantic import BaseModel
        from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


        class DemoBase(DeclarativeBase):
            pass


        class DemoAccount(DemoBase):
            __tablename__ = "accounts"

            acct_id: Mapped[str] = mapped_column(primary_key=True)

            def Set(self, value):
                self.acct_id = value


        class DemoRequest(BaseModel):
            acct_id: str

            def Set(self, value):
                self.acct_id = value


        class DemoLimits:
            MAX_ATTEMPTS = 3


        class DemoRecord:
            if True:

                def Reset(self):
                    self.accountBalance = 0


        class DemoError(Exception):
            pass


        DemoLimits.MAX_ATTEMPTS = 5
        DemoLimits.Handler = staticmethod(print)


        def Rebuild(account: DemoAccount, value):
            account.acct_id = value
            request = DemoRequest(acct_id=value)
            request.acct_id = value
            record = DemoRecord()
            record.accountBalance = 1
            error = DemoError(value)
            error.__cause__ = None
            return account, request, record, error
        """,
        "backend/app/core/demo_attribute_stores.py",
        id="declared-field-mutation-and-camel-case-attribute-stores",
    ),
    pytest.param(
        """
        import functools


        class DemoBalance:
            @property
            def accountBalance(self):
                return self._balance

            @accountBalance.setter
            def accountBalance(self, value):
                self._balance = value

            @functools.cached_property
            def cachedTotal(self):
                return 0
        """,
        "backend/app/core/demo_properties.py",
        id="resolved-property-getter-setter-and-cached-property",
    ),
)

PROGRAM_MAP_DEFECTS = (
    pytest.param("# Traceability\n\nNo map here.\n", "not found", id="missing-heading"),
    pytest.param(
        f"{PROGRAM_MAP_HEADING}\n\n| Program | Module |\n|---|---|\n"
        "| COSGN00C | `backend/app/services/auth/cosgn00c.py` |\n"
        "| COSGN00C | `backend/app/services/auth/cosgn00c.py` |\n",
        "listed more than once",
        id="duplicate-row",
    ),
)


def test_scan_roots_exist() -> None:
    assert [root for root in SCAN_ROOTS if not root.is_dir()] == []
    assert LoadModules(), "no Python module was found under the scan roots"
    assert LegacyArtefactNames(), "no legacy artefact was found in the legacy source folders"


def test_every_scanned_module_parses() -> None:
    failures = [
        f"{module.relativePath}: {module.error}" for module in LoadModules() if module.error
    ]
    assert failures == [], "\n".join(failures)


def test_classes_and_functions_are_pascal_case() -> None:
    AssertNoViolations(CollectViolations((CheckClassNames, CheckFunctionNames)))


def test_parameters_and_local_variables_are_camel_case() -> None:
    AssertNoViolations(CollectViolations((CheckParameterNames, CheckLocalNames)))


def test_fixtures_declare_camel_case_injection_names() -> None:
    AssertNoViolations(CollectViolations((CheckFixtureNames,)))


def test_module_level_names_are_upper_case_constants() -> None:
    AssertNoViolations(CollectViolations((CheckModuleNames,)))


def test_class_attributes_fields_and_instance_attributes_follow_member_naming() -> None:
    AssertNoViolations(CollectViolations((CheckMemberNames,)))


def test_function_bodies_have_at_most_twenty_lines() -> None:
    AssertNoViolations(CollectViolations((CheckFunctionLength,)))


def test_functions_take_at_most_four_parameters() -> None:
    AssertNoViolations(CollectViolations((CheckParameterCount,)))


def test_ported_modules_open_with_source_reference_header() -> None:
    AssertNoViolations(CollectViolations((HeaderViolations,)))


def test_traceability_program_map_matches_ported_modules() -> None:
    AssertNoViolations(ProgramMapViolations())


@pytest.mark.parametrize(
    ("relativePath", "expectedFragment"),
    [
        (VIOLATING_PATH, "class 'lower_case' is not PascalCase"),
        (VIOLATING_PATH, "function 'snake_name' is not PascalCase"),
        (VIOLATING_PATH, "parameter 'account_id' of 'TakesSnakeParameter' is not camelCase"),
        (VIOLATING_PATH, "variable 'account_id' is not camelCase"),
        (VIOLATING_PATH, "module name 'lowerConstant' is not an UPPER_CASE constant"),
        (VIOLATING_PATH, "function 'LongFunction' body spans 21 lines"),
        (VIOLATING_PATH, "function 'FiveParameters' takes 5 parameters"),
        (VIOLATING_PATH, "function '__init__' takes 5 parameters"),
        (VIOLATING_PATH, "parameter 'record_id' of '__init__' is not camelCase"),
        (VIOLATING_PATH, "function 'useFiveParameters' takes 5 parameters"),
        (VIOLATING_PATH, "parameter 'account_id' of 'useSnakeParameter' is not camelCase"),
        (VIOLATING_PATH, "function '_five_parameters_' takes 5 parameters"),
        (VIOLATING_PATH, "function 'Build' takes 5 parameters"),
        (VIOLATING_PATH, "function 'filter' takes 5 parameters"),
        (VIOLATING_PATH, "function 'copy' takes 5 parameters"),
        (VIOLATING_PATH, "function 'settings_customise_sources' takes 6 parameters"),
        (VIOLATING_PATH, "lacks a 'Replaces:' header"),
        (VIOLATING_TEST_PATH, "parameter 'account_id' of 'test_x' is not camelCase"),
        (VIOLATING_TEST_PATH, "function 'test_five' takes 5 parameters"),
        (
            VIOLATING_TEST_PATH,
            "fixture 'SnakeAliasFixture' name= alias 'snake_fixture' is not camelCase",
        ),
        (VIOLATING_TEST_PATH, "fixture 'ImplicitNameFixture' declares no camelCase name= alias"),
        (VIOLATING_TEST_PATH, "fixture 'AsyncImplicitFixture' declares no camelCase name= alias"),
        (
            VIOLATING_TEST_PATH,
            "fixture 'PascalAliasFixture' name= alias 'PascalAlias' is not camelCase",
        ),
        (
            VIOLATING_TEST_PATH,
            "fixture 'NonLiteralAliasFixture' name= alias is not a string literal",
        ),
        (VIOLATING_TEST_PATH, "function 'snake_producer' is not PascalCase"),
        (
            VIOLATING_TEST_PATH,
            "fixture 'AliasedModuleSnakeFixture' name= alias 'aliased_snake' is not camelCase",
        ),
        (
            VIOLATING_TEST_PATH,
            "fixture 'AliasedModuleImplicitFixture' declares no camelCase name= alias",
        ),
        (
            VIOLATING_TEST_PATH,
            "fixture 'SubmoduleImplicitFixture' declares no camelCase name= alias",
        ),
        (
            VIOLATING_TEST_PATH,
            "fixture 'RenamedSnakeFixture' name= alias 'renamed_snake' is not camelCase",
        ),
        (VIOLATING_TEST_PATH, "fixture 'RenamedImplicitFixture' declares no camelCase name= alias"),
        (
            VIOLATING_TEST_PATH,
            "fixture 'NewlineAliasFixture' name= alias 'newlineAlias\\n' is not camelCase",
        ),
        (VIOLATING_TEST_PATH, "function 'useHookProducer' is not PascalCase"),
        (VIOLATING_TEST_PATH, "function 'test_producer' is not PascalCase"),
        (VIOLATING_PATH, "function 'useHelper' is not PascalCase"),
        (VIOLATING_PATH, "function '_ordinary_helper_' is not PascalCase"),
        (VIOLATING_PATH, "function 'copy' is not PascalCase"),
        (VIOLATING_PATH, "parameter 'snake_parameter' of 'copy' is not camelCase"),
        (VIOLATING_PATH, "function 'settings_customise_sources' takes 5 parameters"),
        (VIOLATING_PATH, "parameter 'log_record' of 'filter' is not camelCase"),
        (VIOLATING_PATH, "function 'pytest_configure' is not PascalCase"),
        (VIOLATING_TEST_PATH, "function 'test_nested' is not PascalCase"),
        (VIOLATING_TEST_PATH, "function 'pytest_not_a_hook' is not PascalCase"),
        (VIOLATING_TEST_PATH, "parameter 'plugin_value' of 'pytest_configure' is not camelCase"),
        (VIOLATING_TEST_PATH, "function 'settings_customise_sources' takes 5 parameters"),
        (
            VIOLATING_TEST_PATH,
            "parameter 'secret_settings' of 'settings_customise_sources' is not camelCase",
        ),
        (VIOLATING_HELPER_PATH, "function 'test_helper' is not PascalCase"),
        (VIOLATING_HELPER_PATH, "function 'test_method' is not PascalCase"),
        (VIOLATING_TEST_PATH, "parameter 'tmp_path' of 'MakeFile' is not camelCase"),
        (VIOLATING_TEST_PATH, "parameter 'tmp_path' of '<lambda>' is not camelCase"),
        (VIOLATING_TEST_PATH, "parameter 'account_id' of 'test_snake' is not camelCase"),
        (VIOLATING_TEST_PATH, "function 'test_inner_class_method' is not PascalCase"),
        (VIOLATING_TEST_PATH, "function 'test_constructed' is not PascalCase"),
        (VIOLATING_TEST_PATH, "parameter 'tmp_path' of 'test_constructed' is not camelCase"),
        (VIOLATING_TEST_PATH, "function 'test_mapping' is not PascalCase"),
        (VIOLATING_PATH, "variable '_header' is not camelCase"),
        (VIOLATING_PATH, "parameter '_unusedFlag' of 'ReadRows' is not camelCase"),
        (
            VIOLATING_TEST_PATH,
            "fixture 'PrivateAliasFixture' name= alias '_privateAlias' is not camelCase",
        ),
        (VIOLATING_PATH, "class attribute 'account_balance' of 'OrdinaryRecord' is not camelCase"),
        (VIOLATING_PATH, "class attribute 'account_balance' of 'DataRecord' is not camelCase"),
        (
            VIOLATING_PATH,
            "instance attribute 'account_balance' of 'BalanceHolder' is not camelCase",
        ),
        (VIOLATING_PATH, "instance attribute 'Callback' of 'BalanceHolder' is not camelCase"),
        (
            VIOLATING_PATH,
            "class attribute 'maxAttempts' of 'LoginLimits' is not an UPPER_CASE constant",
        ),
        (
            VIOLATING_PATH,
            "class attribute 'retryLimit' of 'LoginLimits' is not an UPPER_CASE constant",
        ),
        (VIOLATING_PATH, "variable 'loop_index' is not camelCase"),
        (
            VIOLATING_PATH,
            "class attribute 'acctId' of 'CamelKeyModel' is not a snake_case JSON key",
        ),
        (VIOLATING_PATH, "'secret_key' of 'LowerCaseSettings' is not an UPPER_CASE environment"),
        (
            VIOLATING_STAND_IN_PATH,
            "class attribute 'reset_all' of 'FailingCloseConnection' is not PascalCase",
        ),
        (VIOLATING_SHADOWED_BASE_PATH, "function 'settings_customise_sources' takes 5 parameters"),
        (
            VIOLATING_SHADOWED_BASE_PATH,
            "parameter 'settings_cls' of 'settings_customise_sources' is not camelCase",
        ),
        (
            VIOLATING_UNRELATED_IMPORT_PATH,
            "function 'settings_customise_sources' takes 5 parameters",
        ),
        (
            VIOLATING_UNRELATED_IMPORT_PATH,
            "parameter 'file_secret_settings' of 'settings_customise_sources' is not camelCase",
        ),
        (VIOLATING_PATH, "function 'visit_NotANode' is not PascalCase"),
        (VIOLATING_PATH, "function 'visit_FunctionDef' is not PascalCase"),
        (VIOLATING_TEST_PATH, "function 'setup_method' is not PascalCase"),
        (VIOLATING_TEST_PATH, "function 'setup_class' is not PascalCase"),
        (VIOLATING_HELPER_PATH, "function 'setup_module' is not PascalCase"),
        (VIOLATING_HELPER_PATH, "function 'teardown_method' is not PascalCase"),
        (VIOLATING_PATH, "parameter 'name_node' of 'visit_Name' is not camelCase"),
        (VIOLATING_TEST_PATH, "parameter 'bound_method' of 'setup_method' is not camelCase"),
        (
            VIOLATING_PATH,
            "instance attribute 'account_balance' of 'StoredRecord' is not camelCase",
        ),
        (VIOLATING_PATH, "instance attribute 'total_count' of 'StoredRecord' is not camelCase"),
        (
            VIOLATING_PATH,
            "instance attribute 'annotated_value' of 'StoredRecord' is not camelCase",
        ),
        (
            VIOLATING_PATH,
            "class attribute 'maxAttempts' of 'OpenLimits' is not an UPPER_CASE constant",
        ),
        (VIOLATING_PATH, "class attribute 'handler' of 'OpenLimits' is not PascalCase"),
        (
            VIOLATING_PATH,
            "class attribute 'retry_count' of 'OpenLimits' is not an UPPER_CASE constant",
        ),
        (
            VIOLATING_PATH,
            "instance attribute 'account_balance' of 'GuardedRecord' is not camelCase",
        ),
        (VIOLATING_PATH, "instance attribute 'other_field' of 'AccountModel' is not camelCase"),
        (VIOLATING_SPELLED_PATH, "function 'accountBalance' is not PascalCase"),
        (VIOLATING_SPELLED_PATH, "function 'plainSetter' is not PascalCase"),
        (
            VIOLATING_SPELLED_PATH,
            "class attribute 'cachedTotal' of 'SpelledAccessors' is not an UPPER_CASE constant",
        ),
        (
            VIOLATING_SPELLED_PATH,
            "instance attribute 'stored_value' of 'SpelledAccessors' is not camelCase",
        ),
        (
            VIOLATING_SPELLED_PATH,
            "class attribute 'RETRY_LIMIT' of 'SpelledModel' is not a snake_case JSON key",
        ),
        (VIOLATING_SPELLED_PATH, "module name 'SpelledMode' is not an UPPER_CASE constant"),
        (VIOLATING_SPELLED_PATH, "module name 'SpelledHandle' is not an UPPER_CASE constant"),
        (VIOLATING_SPELLED_PATH, "module name 'SpelledAlias' is not an UPPER_CASE constant"),
        (
            VIOLATING_SPELLED_FIXTURE_PATH,
            "parameter 'tmp_path' of 'SpelledFixture' is not camelCase",
        ),
    ],
)
def test_checkers_flag_synthetic_violations(relativePath: str, expectedFragment: str) -> None:
    violations = CheckSource(VIOLATING_SOURCES[relativePath], relativePath)
    messages = [violation.message for violation in violations]
    assert any(expectedFragment in message for message in messages), messages


@pytest.mark.parametrize(("source", "relativePath"), EXEMPT_SOURCES)
def test_checkers_accept_framework_exemptions(source: str, relativePath: str) -> None:
    AssertNoViolations(CheckSource(textwrap.dedent(source), relativePath))


@pytest.mark.parametrize(
    ("sourceBuilder", "bodyLines", "expectedCount"),
    [
        pytest.param(NumberedDefSource, 20, 0, id="def-at-limit"),
        pytest.param(NumberedDefSource, 21, 1, id="def-over-limit"),
        pytest.param(NumberedLambdaSource, 20, 0, id="lambda-at-limit"),
        pytest.param(NumberedLambdaSource, 21, 1, id="lambda-over-limit"),
    ],
)
def test_length_rule_counts_def_body_after_docstring_and_lambda_body(
    sourceBuilder: Callable[[int], str], bodyLines: int, expectedCount: int
) -> None:
    module = ParseSource(sourceBuilder(bodyLines), "backend/app/demo.py")
    assert len(CheckFunctionLength(module)) == expectedCount


@pytest.mark.parametrize(
    ("source", "expectedCount"),
    [
        pytest.param("SAMPLE = lambda one, two, three, four: one\n", 0, id="at-limit"),
        pytest.param("SAMPLE = lambda one, two, three, four, five: one\n", 1, id="over-limit"),
        pytest.param("SAMPLE = lambda one, two, three, *, four, five: one\n", 1, id="keyword-only"),
        pytest.param("SAMPLE = lambda self, two, three, four, five: self\n", 1, id="no-receiver"),
        pytest.param(
            "SAMPLE = lambda one, two, three, four, *rest, **extra: one\n", 0, id="variadic"
        ),
    ],
)
def test_parameter_rule_counts_every_named_lambda_parameter(
    source: str, expectedCount: int
) -> None:
    module = ParseSource(source, "backend/app/demo.py")
    assert len(CheckParameterCount(module)) == expectedCount


@pytest.mark.parametrize(("text", "expectedFragment"), PROGRAM_MAP_DEFECTS)
def test_program_map_reader_reports_missing_heading_and_duplicates(
    text: str, expectedFragment: str
) -> None:
    _, violations = ReadProgramMap(text)
    assert any(expectedFragment in violation.message for violation in violations), violations
