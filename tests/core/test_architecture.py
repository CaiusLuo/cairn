"""Keep the package dependency direction explicit."""

import ast
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / "src" / "cairn"
LAYERS = {"core", "infra", "runtime", "terminal"}
ALLOWED = {
    "core": {"core"},
    "infra": {"core", "infra"},
    "runtime": {"core", "infra", "runtime"},
    "terminal": {"core", "infra", "runtime", "terminal"},
}

#: Concrete adapters and effect modules that a stable domain contract must not
#: depend on. `core` may import the standard library, but not a library or module
#: that performs provider calls, process execution, networking or persistence.
FORBIDDEN_IN_CORE = frozenset(
    {
        # Third-party infrastructure declared by the project.
        "litellm",
        "tomlkit",
        "typer",
        "rich",
        "prompt_toolkit",
        "dotenv",
        # Standard-library effects: processes, networking, sockets, persistence.
        "asyncio",
        "subprocess",
        "multiprocessing",
        "socket",
        "ssl",
        "sqlite3",
        "shutil",
        "tempfile",
        "http",
        "urllib",
        "ftplib",
        "smtplib",
        "http.client",
        "xmlrpc",
    }
)


def _layer_for(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) > 1 and parts[0] == "cairn" and parts[1] in LAYERS:
        return parts[1]
    return None


def _targets(tree: ast.Module, package_name: str) -> set[str]:
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = resolve_name(
                    "." * node.level + (node.module or ""), package_name
                )
            else:
                base = node.module or ""
            if base == "cairn":
                targets.update(f"cairn.{alias.name}" for alias in node.names)
            elif base:
                targets.add(base)
                # A from-import may name a module or an exported value. Module
                # candidates become graph edges only if that module exists.
                targets.update(f"{base}.{alias.name}" for alias in node.names)
    return targets


def _module_context(path: Path) -> tuple[str, str]:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = ("cairn", *relative.parts[:-1])
    if path.name != "__init__.py":
        parts = (*parts, path.stem)
    module = ".".join(parts)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    return module, package


def test_package_imports_follow_the_dependency_direction() -> None:
    violations: list[str] = []
    for layer in sorted(LAYERS):
        for path in sorted((PACKAGE_ROOT / layer).rglob("*.py")):
            _, package_name = _module_context(path)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for target in _targets(tree, package_name):
                target_layer = _layer_for(target)
                if target_layer is not None and target_layer not in ALLOWED[layer]:
                    violations.append(
                        f"{path.relative_to(ROOT)} imports {target} ({layer} -> {target_layer})"
                    )

    assert not violations, "Invalid Cairn package dependencies:\n" + "\n".join(
        violations
    )


def _imported_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module)
    return roots


def _forbidden_root(module: str) -> str | None:
    for forbidden in FORBIDDEN_IN_CORE:
        if module == forbidden or module.startswith(f"{forbidden}."):
            return forbidden
    return None


def test_core_stays_free_of_infrastructure_dependencies() -> None:
    violations: list[str] = []
    for path in sorted((PACKAGE_ROOT / "core").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in sorted(_imported_roots(tree)):
            forbidden = _forbidden_root(module)
            if forbidden is not None:
                violations.append(
                    f"{path.relative_to(ROOT)} imports {module} "
                    f"({forbidden} is infrastructure)"
                )

    assert not violations, (
        "core must stay independent of provider, process, network and persistence "
        "modules; move this behaviour into infra and depend on a core contract:\n"
        + "\n".join(violations)
    )


def test_packages_have_no_import_cycles() -> None:
    """No module inside a Cairn layer may be part of an import cycle.

    A cycle means two modules must always change together, and it hides which of
    them owns the contract between them. Type-only imports count: a cycle held
    together by ``TYPE_CHECKING`` is still two modules that cannot be understood
    or tested independently.
    """
    graph: dict[str, set[str]] = {}
    for layer in sorted(LAYERS):
        for path in sorted((PACKAGE_ROOT / layer).rglob("*.py")):
            module_name, package_name = _module_context(path)
            graph[module_name] = {
                target
                for target in _targets(
                    ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
                    package_name,
                )
                if _layer_for(target) is not None
            }

    cycles: list[str] = []

    def walk(origin: str, node: str, seen: tuple[str, ...]) -> None:
        for target in sorted(graph.get(node, ())):
            if target == origin:
                cycles.append(" -> ".join((*seen, target)))
            elif target in graph and target not in seen:
                walk(origin, target, (*seen, target))

    for module in sorted(graph):
        walk(module, module, (module,))

    assert not cycles, "Circular Cairn imports:\n" + "\n".join(sorted(set(cycles)))


def test_import_targets_include_package_siblings() -> None:
    tree = ast.parse("from . import schema\nfrom cairn.infra.config import loader")
    assert _targets(tree, "cairn.infra.config") >= {
        "cairn.infra.config.schema",
        "cairn.infra.config.loader",
    }


def test_independent_effect_owners_do_not_import_each_other() -> None:
    # Local Git and remote GitHub have different safety boundaries. Likewise,
    # approval storage and session receipts must not borrow another store's
    # lifecycle. Shared SQLite version policy belongs in infra.sqlite_policy.
    independent = (
        ("git", "github"),
        ("github", "git"),
        ("memory", "sessions"),
        ("sessions", "memory"),
    )
    violations: list[str] = []
    for owner, forbidden in independent:
        prefix = f"cairn.infra.{forbidden}"
        for path in sorted((PACKAGE_ROOT / "infra" / owner).rglob("*.py")):
            _, package = _module_context(path)
            for target in _targets(ast.parse(path.read_text()), package):
                if target == prefix or target.startswith(prefix + "."):
                    violations.append(f"{path.relative_to(ROOT)} imports {target}")
    assert not violations, "Independent effect owners coupled:\n" + "\n".join(
        violations
    )


def test_effect_owner_guard_resolves_package_relative_imports() -> None:
    # Package initializers are importers in their own package, not its parent.
    # All scans must see a forbidden sibling even when it uses relative syntax.
    path = PACKAGE_ROOT / "infra" / "git" / "__init__.py"
    module, package = _module_context(path)
    assert module == package == "cairn.infra.git"
    targets = _targets(ast.parse("from ..github import transport"), package)
    assert "cairn.infra.github" in targets
    assert "cairn.infra.github.transport" in targets
    assert _module_context(PACKAGE_ROOT / "infra" / "git" / "context.py") == (
        "cairn.infra.git.context",
        "cairn.infra.git",
    )
