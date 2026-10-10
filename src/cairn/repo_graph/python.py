"""Python syntax facts and conservative local import relationships."""

import ast
import keyword
import sys
from collections.abc import Iterator
from dataclasses import dataclass

from cairn.repo_graph.models import (
    DependencyRecord,
    ImportRecord,
    ModuleRecord,
    PythonStatus,
    RelationshipStatus,
    SymbolKind,
    SymbolRecord,
)

MAX_PYTHON_NAME_LENGTH = 256
MAX_IMPORT_NAMES = 64


@dataclass(frozen=True, slots=True)
class PythonFacts:
    module: ModuleRecord
    symbols: tuple[SymbolRecord, ...]
    imports: tuple[ImportRecord, ...]


def _identifier(value: str) -> bool:
    return (
        len(value) <= MAX_PYTHON_NAME_LENGTH
        and value.isidentifier()
        and not keyword.iskeyword(value)
    )


def _module_name(value: str) -> bool:
    return len(value) <= MAX_PYTHON_NAME_LENGTH and all(
        _identifier(part) for part in value.split(".")
    )


def _path_parts(path: str) -> tuple[str, ...] | None:
    parts = tuple(path.split("/"))
    if "\\" in path or "\0" in path or any(part in {"", ".", ".."} for part in parts):
        return None
    return parts


def _identity(path: str, roots: tuple[str, ...]) -> tuple[str | None, bool]:
    parts = _path_parts(path)
    basename = path.rsplit("/", 1)[-1]
    is_package = basename in {"__init__.py", "__init__.pyi"}
    if parts is None or not basename.endswith((".py", ".pyi")):
        return None, is_package
    matched: tuple[str, ...] | None = None
    for root in roots:
        root_parts = () if root == "." else _path_parts(root)
        if (
            root_parts is not None
            and len(root_parts) < len(parts)
            and parts[: len(root_parts)] == root_parts
            and (matched is None or len(root_parts) > len(matched))
        ):
            matched = root_parts
    if matched is None:
        return None, is_package
    relative = parts[len(matched) :]
    if is_package:
        components = relative[:-1]
    else:
        suffix_length = 4 if basename.endswith(".pyi") else 3
        components = (*relative[:-1], basename[:-suffix_length])
    if not components or any(not _identifier(part) for part in components):
        return None, is_package
    name = ".".join(components)
    return (name if _module_name(name) else None), is_package


def _nodes(tree: ast.AST) -> Iterator[tuple[ast.AST, ast.AST | None]]:
    """Walk lazily so a node budget does not first enqueue the entire AST."""
    stack: list[tuple[ast.AST | None, Iterator[ast.AST]]] = [(None, iter((tree,)))]
    while stack:
        parent, children = stack[-1]
        node = next(children, None)
        if node is None:
            stack.pop()
            continue
        yield node, parent
        stack.append((node, ast.iter_child_nodes(node)))


def parse_python(
    path: str,
    data: bytes,
    sha256: str,
    *,
    roots: tuple[str, ...],
    max_nodes: int,
    max_records: int,
) -> PythonFacts:
    """Parse bytes without execution, preserving only names and source provenance.

    Symbols are direct module statements; imports are syntactic statements at
    any depth. A limited result retains a deterministic traversal prefix and
    never exposes a partial ``from`` import name list.
    """
    if type(max_nodes) is not int or max_nodes < 1:
        raise ValueError("AST node limit must be a positive integer")
    if type(max_records) is not int or max_records < 0:
        raise ValueError("Python record limit must be a nonnegative integer")
    name, is_package = _identity(path, roots)
    status = PythonStatus.PARSED if name is not None else PythonStatus.UNSUPPORTED
    try:
        tree = ast.parse(data, filename="<repository source>")
    except (SyntaxError, ValueError, UnicodeError, RecursionError, MemoryError):
        return PythonFacts(
            ModuleRecord(path, name, is_package, PythonStatus.INVALID, sha256), (), ()
        )
    symbols: list[SymbolRecord] = []
    imports: list[ImportRecord] = []
    for count, (node, parent) in enumerate(_nodes(tree), start=1):
        if count > max_nodes:
            status = PythonStatus.LIMITED
            break
        if parent is tree and isinstance(
            node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            if not _identifier(node.name):
                status = PythonStatus.LIMITED
                continue
            if len(symbols) + len(imports) >= max_records:
                status = PythonStatus.LIMITED
                break
            kind = (
                SymbolKind.CLASS
                if isinstance(node, ast.ClassDef)
                else SymbolKind.ASYNC_FUNCTION
                if isinstance(node, ast.AsyncFunctionDef)
                else SymbolKind.FUNCTION
            )
            symbols.append(
                SymbolRecord(
                    path,
                    node.name,
                    kind,
                    node.lineno,
                    node.end_lineno or node.lineno,
                    sha256,
                )
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if not _module_name(alias.name):
                    status = PythonStatus.LIMITED
                    continue
                if len(symbols) + len(imports) >= max_records:
                    status = PythonStatus.LIMITED
                    break
                imports.append(
                    ImportRecord(
                        path,
                        alias.name,
                        (),
                        0,
                        node.lineno,
                        node.end_lineno or node.lineno,
                        sha256,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            if (
                len(node.names) > MAX_IMPORT_NAMES
                or (node.module is not None and not _module_name(node.module))
                or any(
                    alias.name != "*" and not _identifier(alias.name)
                    for alias in node.names
                )
            ):
                status = PythonStatus.LIMITED
                continue
            if len(symbols) + len(imports) >= max_records:
                status = PythonStatus.LIMITED
                break
            imports.append(
                ImportRecord(
                    path,
                    node.module,
                    tuple(alias.name for alias in node.names),
                    node.level,
                    node.lineno,
                    node.end_lineno or node.lineno,
                    sha256,
                )
            )
    return PythonFacts(
        ModuleRecord(path, name, is_package, status, sha256),
        tuple(
            sorted(symbols, key=lambda record: (record.line, record.name, record.kind))
        ),
        tuple(
            sorted(
                imports,
                key=lambda record: (
                    record.line,
                    record.module or "",
                    record.level,
                    record.names,
                ),
            )
        ),
    )


def _candidate(
    source: ModuleRecord, imported: ImportRecord
) -> tuple[str | None, RelationshipStatus | None]:
    if (
        source.status is not PythonStatus.PARSED
        or source.name is None
        or not _module_name(source.name)
        or imported.level < 0
        or (imported.module is not None and not _module_name(imported.module))
    ):
        return (
            imported.module if imported.level == 0 else None,
            RelationshipStatus.UNSUPPORTED,
        )
    if imported.level == 0:
        return imported.module, (
            RelationshipStatus.UNSUPPORTED if imported.module is None else None
        )
    package = source.name.split(".")
    if not source.is_package:
        package.pop()
    ascent = imported.level - 1
    if ascent >= len(package):
        return None, RelationshipStatus.UNRESOLVED
    if ascent:
        package = package[:-ascent]
    if imported.module is not None:
        package.extend(imported.module.split("."))
    name = ".".join(package)
    if not _module_name(name):
        return None, RelationshipStatus.UNSUPPORTED
    return name, None


def resolve_dependencies(
    modules: tuple[ModuleRecord, ...], imports: tuple[ImportRecord, ...]
) -> tuple[DependencyRecord, ...]:
    """Resolve exact indexed module identities without runtime path inference.

    ``from package import member`` relates to ``package``; members do not imply
    submodules. Only unique parsed targets resolve, and only absolute imports
    with a standard-library root can be labeled external.
    """
    sources: dict[str, list[ModuleRecord]] = {}
    index: dict[str, list[ModuleRecord]] = {}
    for module in modules:
        sources.setdefault(module.path, []).append(module)
        if module.name is not None and _module_name(module.name):
            index.setdefault(module.name, []).append(module)
    local_roots = {name.split(".", 1)[0] for name in index}
    dependencies: list[DependencyRecord] = []
    for imported in imports:
        candidates = sources.get(imported.path, [])
        name: str | None = imported.module if imported.level == 0 else None
        target: str | None = None
        if len(candidates) != 1:
            status = RelationshipStatus.UNSUPPORTED
        else:
            name, unsupported = _candidate(candidates[0], imported)
            if unsupported is not None:
                status = unsupported
            else:
                matches = index.get(name, []) if name is not None else []
                if len(matches) == 1 and matches[0].status is PythonStatus.PARSED:
                    target = matches[0].path
                    status = RelationshipStatus.RESOLVED
                elif len(matches) > 1:
                    status = RelationshipStatus.AMBIGUOUS
                elif (
                    imported.level == 0
                    and name is not None
                    and name.split(".", 1)[0] in sys.stdlib_module_names
                    and name.split(".", 1)[0] not in local_roots
                ):
                    status = RelationshipStatus.EXTERNAL
                else:
                    status = RelationshipStatus.UNRESOLVED
        dependencies.append(
            DependencyRecord(
                imported.path,
                name,
                target,
                status,
                imported.line,
                imported.end_line,
                imported.source_sha256,
            )
        )
    return tuple(
        sorted(
            dependencies,
            key=lambda record: (
                record.path,
                record.line,
                record.module or "",
                record.end_line,
                record.target_path or "",
                record.status,
            ),
        )
    )
