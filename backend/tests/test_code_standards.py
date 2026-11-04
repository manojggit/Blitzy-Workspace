"""MK_Modernization Rule and traceability gate: static AST checks over every Python tree.

Specified in the Technical Specification, Sections 0.10 and 0.11.3. The checks parse every
module under backend/app, backend/alembic/versions, backend/tests and batch (batch/tests
included), import none of them, and need no database and no fixture:

- Naming (clause 1): classes, type aliases and functions are PascalCase, parameters and
  variables camelCase, and module-level assignments UPPER_CASE constants. Identifiers a
  framework dictates keep their form; each exemption set below names its framework. Class-body
  attributes are not checked, because ORM columns, Pydantic fields, Settings environment
  fields, dataclass fields and enum members keep their fixed names.
- Size (clause 3): a function body, its docstring excluded, spans at most 20 lines, and a
  function takes at most four parameters, counted as ruff's PLR0913 counts them (receiver,
  *args and **kwargs excluded). Only a signature a framework fixes is exempt from the parameter
  limit, so pytest test functions and constructors are counted.
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
import re
import textwrap
from collections.abc import Callable, Iterator
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

PASCAL_CASE_PATTERN = re.compile(r"^_{0,2}[A-Z][A-Za-z0-9]*$")
# The bare "_" is the conventional throwaway name, so it counts as camelCase.
CAMEL_CASE_PATTERN = re.compile(r"^(?:_|_{0,2}[a-z][A-Za-z0-9]*)$")
UPPER_CASE_PATTERN = re.compile(r"^_{0,2}[A-Z][A-Z0-9_]*$")
DUNDER_PATTERN = re.compile(r"^__[a-z0-9_]+__$")
# Enum hooks such as _missing_ and _generate_next_value_.
SUNDER_PATTERN = re.compile(r"^_[a-z][a-z0-9_]*_$")
TEST_FUNCTION_PATTERN = re.compile(r"^test_")
PYTEST_HOOK_PATTERN = re.compile(r"^pytest_")
HOOK_STYLE_PATTERN = re.compile(r"^use[A-Z]")
VISITOR_PATTERN = re.compile(r"^visit_[A-Za-z]+$")
LEGACY_WORD_PATTERN = re.compile(r"\b[A-Z][A-Z0-9-]{2,}\b")
TABLE_SEPARATOR_PATTERN = re.compile(r"^:?-+:?$")

# Alembic: the revision functions and the revision identifiers it reads from each script.
ALEMBIC_FUNCTIONS = frozenset({"upgrade", "downgrade"})
ALEMBIC_MODULE_VARIABLES = frozenset({"revision", "down_revision", "branch_labels", "depends_on"})
# Uvicorn: the ASGI target is app.main:app.
MODULE_VARIABLE_EXEMPTIONS: dict[str, frozenset[str]] = {"backend/app/main.py": frozenset({"app"})}
# pytest: module-level marks and plugin declarations.
TEST_MODULE_VARIABLES = frozenset({"pytestmark", "pytest_plugins"})
# Methods a base class calls by name, valid only in a class body.
FRAMEWORK_METHOD_NAMES = frozenset(
    {
        # Standard-library logging.Filter, logging.Formatter and logging.Handler.
        "filter",
        "format",
        "formatTime",
        "emit",
        # json.JSONEncoder.
        "default",
        # Starlette BaseHTTPMiddleware.dispatch and BaseRoute.matches.
        "dispatch",
        "matches",
        # FastAPI APIRoute.get_route_handler.
        "get_route_handler",
        # SQLAlchemy TypeDecorator, and TypeEngine.literal_processor.
        "process_bind_param",
        "process_result_value",
        "process_literal_param",
        "load_dialect_impl",
        "coerce_compared_value",
        "copy",
        "literal_processor",
        # pydantic and pydantic-settings.
        "settings_customise_sources",
        "model_post_init",
        # Click Command.make_context and Command.invoke.
        "make_context",
        "invoke",
        # pytest xunit-style setup and teardown.
        "setup_method",
        "teardown_method",
        "setup_class",
        "teardown_class",
        # ast.NodeVisitor; its visit_<NodeType> methods match VISITOR_PATTERN.
        "generic_visit",
    }
)
RECEIVER_NAMES = frozenset({"self", "cls", "mcs"})
# Python fixes only the name of these dunders; the class author writes their parameters.
AUTHOR_SIGNATURE_DUNDERS = frozenset({"__init__", "__new__", "__call__"})
# pytest built-in fixtures, injected by parameter name.
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
# Python's property protocol: an accessor is an attribute, so camelCase is accepted.
PROPERTY_DECORATORS = frozenset({"property", "cached_property"})
PROPERTY_ACCESSOR_ATTRIBUTES = frozenset({"setter", "getter", "deleter"})
# typing: a type declared by a factory call or a TypeAlias annotation is named like a class.
TYPE_FACTORY_CALLS = frozenset({"TypeVar", "NewType", "ParamSpec", "TypeVarTuple"})
TYPE_ALIAS_ANNOTATION = "TypeAlias"

FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPE_BOUNDARY_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
ASSIGNMENT_TYPES = (ast.Assign, ast.AnnAssign, ast.AugAssign)
MODULE_BLOCK_TYPES = (ast.If, ast.Try, ast.TryStar, ast.With)
MATCH_CAPTURE_TYPES = (ast.MatchAs, ast.MatchStar)

type FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
type ParameterOwner = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


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


def TerminalName(node: ast.AST) -> str:
    """The last identifier of a Name or dotted Attribute, for example cached_property."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def IterFunctions(tree: ast.AST) -> Iterator[tuple[FunctionNode, bool]]:
    """Yield every function, nested ones included, with whether a class body defines it."""
    pending: list[ast.AST] = [tree]
    while pending:
        parent = pending.pop()
        for child in ast.iter_child_nodes(parent):
            if isinstance(child, FUNCTION_TYPES):
                yield child, isinstance(parent, ast.ClassDef)
            pending.append(child)


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


def IsFrameworkMandated(function: FunctionNode, parentIsClass: bool, module: SourceModule) -> bool:
    """A name some framework calls, so the framework, not the rule, fixes its form."""
    name = function.name
    if DUNDER_PATTERN.match(name) or SUNDER_PATTERN.match(name) or HOOK_STYLE_PATTERN.match(name):
        return True
    if IsInTestTree(module) and (
        TEST_FUNCTION_PATTERN.match(name) or PYTEST_HOOK_PATTERN.match(name)
    ):
        return True
    if IsAlembicVersion(module) and name in ALEMBIC_FUNCTIONS:
        return True
    return parentIsClass and (name in FRAMEWORK_METHOD_NAMES or bool(VISITOR_PATTERN.match(name)))


def IsSignatureDictated(function: FunctionNode, parentIsClass: bool, module: SourceModule) -> bool:
    """Whether the framework also fixes the parameters, both their names and their count.

    Two kinds of framework-named function are still checked, because their authors choose the
    parameters: pytest's test_* functions, whose parameters are the fixture names the suite
    author selects, declared camelCase through pytest.fixture(name=...); and the constructors
    and __call__ in AUTHOR_SIGNATURE_DUNDERS, whose parameters the class author writes.
    """
    if IsInTestTree(module) and TEST_FUNCTION_PATTERN.match(function.name):
        return False
    if function.name in AUTHOR_SIGNATURE_DUNDERS:
        return False
    return IsFrameworkMandated(function, parentIsClass, module)


def IsPropertyAccessor(function: FunctionNode) -> bool:
    for decorator in function.decorator_list:
        decoratorName = TerminalName(decorator)
        if decoratorName in PROPERTY_DECORATORS:
            return True
        if isinstance(decorator, ast.Attribute) and decoratorName in PROPERTY_ACCESSOR_ATTRIBUTES:
            return True
    return False


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
    function: FunctionNode, parentIsClass: bool, module: SourceModule
) -> bool:
    if IsFrameworkMandated(function, parentIsClass, module):
        return True
    if IsPropertyAccessor(function) and CAMEL_CASE_PATTERN.match(function.name):
        return True
    return bool(PASCAL_CASE_PATTERN.match(function.name))


def CheckFunctionNames(module: SourceModule) -> list[Violation]:
    return [
        NameViolation(module, function, f"function {function.name!r} is not PascalCase")
        for function, parentIsClass in IterFunctions(module.tree)
        if not IsAcceptedFunctionName(function, parentIsClass, module)
    ]


def ParameterList(owner: ParameterOwner) -> list[ast.arg]:
    arguments = owner.args
    variadic = [parameter for parameter in (arguments.vararg, arguments.kwarg) if parameter]
    return [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, *variadic]


def IsAcceptedParameterName(name: str, module: SourceModule) -> bool:
    if name in RECEIVER_NAMES or CAMEL_CASE_PATTERN.match(name):
        return True
    return IsInTestTree(module) and name in PYTEST_BUILTIN_FIXTURE_PARAMETERS


def ParameterViolations(owner: ParameterOwner, module: SourceModule) -> list[Violation]:
    label = getattr(owner, "name", "<lambda>")
    return [
        NameViolation(
            module, parameter, f"parameter {parameter.arg!r} of {label!r} is not camelCase"
        )
        for parameter in ParameterList(owner)
        if not IsAcceptedParameterName(parameter.arg, module)
    ]


def CheckParameterNames(module: SourceModule) -> list[Violation]:
    violations: list[Violation] = []
    for function, parentIsClass in IterFunctions(module.tree):
        if not IsSignatureDictated(function, parentIsClass, module):
            violations.extend(ParameterViolations(function, module))
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Lambda):
            violations.extend(ParameterViolations(node, module))
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
    """Yield the names module-level assignments bind, inside if, try and with blocks too."""
    for statement in statements:
        if isinstance(statement, ASSIGNMENT_TYPES):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                yield from ((name, statement) for name in TargetNames(target))
        elif isinstance(statement, MODULE_BLOCK_TYPES):
            yield from ModuleAssignmentTargets(NestedStatements(statement))


def IsTypeDeclaration(statement: ast.stmt) -> bool:
    value = getattr(statement, "value", None)
    if isinstance(value, ast.Call) and TerminalName(value.func) in TYPE_FACTORY_CALLS:
        return True
    annotation = getattr(statement, "annotation", None)
    return annotation is not None and TerminalName(annotation) == TYPE_ALIAS_ANNOTATION


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
    return IsTypeDeclaration(statement) and bool(PASCAL_CASE_PATTERN.match(name))


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


def CheckFunctionLength(module: SourceModule) -> list[Violation]:
    violations: list[Violation] = []
    for function, _ in IterFunctions(module.tree):
        span = FunctionBodySpan(function)
        if span > MAX_FUNCTION_BODY_LINES:
            message = (
                f"function {function.name!r} body spans {span} lines"
                f" (limit {MAX_FUNCTION_BODY_LINES})"
            )
            violations.append(NameViolation(module, function, message))
    return violations


def CountParameters(function: FunctionNode) -> int:
    """Positional and keyword-only parameters, excluding a receiver, *args and **kwargs."""
    positional = [*function.args.posonlyargs, *function.args.args]
    if positional and positional[0].arg in RECEIVER_NAMES:
        positional = positional[1:]
    return len(positional) + len(function.args.kwonlyargs)


def CheckParameterCount(module: SourceModule) -> list[Violation]:
    violations: list[Violation] = []
    for function, parentIsClass in IterFunctions(module.tree):
        count = CountParameters(function)
        if count > MAX_PARAMETERS and not IsSignatureDictated(function, parentIsClass, module):
            message = (
                f"function {function.name!r} takes {count} parameters (limit {MAX_PARAMETERS})"
            )
            violations.append(NameViolation(module, function, message))
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
    CheckLocalNames,
    CheckModuleNames,
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


VIOLATING_PATH = "backend/app/services/demo/violations.py"
VIOLATING_SOURCE = textwrap.dedent(
    """
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


    """
) + NumberedFunctionSource("LongFunction", MAX_FUNCTION_BODY_LINES + 1)
VIOLATING_TEST_PATH = "backend/tests/unit/test_violations.py"
VIOLATING_SOURCES = {
    VIOLATING_PATH: VIOLATING_SOURCE,
    VIOLATING_TEST_PATH: (
        "def test_x(account_id):\n    return account_id\n\n\n"
        "def test_five(one, two, three, four, five):\n    return one\n"
    ),
}

EXEMPT_SOURCES = (
    pytest.param(
        """
        pytestmark = []


        def DemoFixture(tmp_path):
            return tmp_path


        def test_x(demoFixture):
            return demoFixture
        """,
        "backend/tests/unit/test_demo.py",
        id="pytest-test-function-and-builtin-fixture",
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
        from typing import TypeVar

        __all__ = ["MaskingFilter", "OnlineContext"]
        T = TypeVar("T")


        class MaskingFilter(logging.Filter):
            def filter(self, record):
                return True


        class OnlineContext:
            @property
            def engine(self):
                return None
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


def test_module_level_names_are_upper_case_constants() -> None:
    AssertNoViolations(CollectViolations((CheckModuleNames,)))


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
        (VIOLATING_PATH, "lacks a 'Replaces:' header"),
        (VIOLATING_TEST_PATH, "parameter 'account_id' of 'test_x' is not camelCase"),
        (VIOLATING_TEST_PATH, "function 'test_five' takes 5 parameters"),
    ],
)
def test_checkers_flag_synthetic_violations(relativePath: str, expectedFragment: str) -> None:
    violations = CheckSource(VIOLATING_SOURCES[relativePath], relativePath)
    messages = [violation.message for violation in violations]
    assert any(expectedFragment in message for message in messages), messages


@pytest.mark.parametrize(("source", "relativePath"), EXEMPT_SOURCES)
def test_checkers_accept_framework_exemptions(source: str, relativePath: str) -> None:
    AssertNoViolations(CheckSource(textwrap.dedent(source), relativePath))


@pytest.mark.parametrize(("bodyLines", "expectedCount"), [(20, 0), (21, 1)])
def test_length_rule_counts_body_lines_after_the_docstring(
    bodyLines: int, expectedCount: int
) -> None:
    module = ParseSource(NumberedFunctionSource("Sample", bodyLines), "backend/app/demo.py")
    assert len(CheckFunctionLength(module)) == expectedCount


@pytest.mark.parametrize(("text", "expectedFragment"), PROGRAM_MAP_DEFECTS)
def test_program_map_reader_reports_missing_heading_and_duplicates(
    text: str, expectedFragment: str
) -> None:
    _, violations = ReadProgramMap(text)
    assert any(expectedFragment in violation.message for violation in violations), violations
