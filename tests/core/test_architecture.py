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
                # `from . import sibling` may load a sibling module directly.
                if node.module is None:
                    targets.update(f"{base}.{alias.name}" for alias in node.names)
    return targets


def test_package_imports_follow_the_dependency_direction() -> None:
    violations: list[str] = []
    for layer in sorted(LAYERS):
        for path in sorted((PACKAGE_ROOT / layer).rglob("*.py")):
            relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
            parts = ("cairn", *relative.parts[:-1])
            if path.name != "__init__.py":
                parts = (*parts, path.stem)
            module_name = ".".join(parts)
            package_name = (
                module_name
                if path.name == "__init__.py"
                else module_name.rpartition(".")[0]
            )
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
            relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
            parts = ("cairn", *relative.parts[:-1])
            if path.name != "__init__.py":
                parts = (*parts, path.stem)
            module_name = ".".join(parts)
            graph[module_name] = {
                target
                for target in _targets(
                    ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
                    module_name.rpartition(".")[0],
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
