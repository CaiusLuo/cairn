"""Check the import rules that keep execution, persistence and adapters separate."""

import ast
from importlib.util import resolve_name
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "cairn"
PURE_MODULES = {
    "cairn.agent.budget",
    "cairn.agent.context.evidence",
    "cairn.agent.context.window",
    "cairn.agent.models.catalog",
    "cairn.agent.models.client",
    "cairn.agent.models.messages",
    "cairn.agent.models.selection",
    "cairn.agent.permissions",
    "cairn.agent.turn.events",
    "cairn.agent.turn.state",
    "cairn.conversation.models",
    "cairn.development.board.models",
    "cairn.development.execution.models",
    "cairn.development.models",
    "cairn.scheduling.models",
}
PURE_PACKAGES = ("cairn.agent.context.compaction",)
EFFECT_IMPORTS = {
    "asyncio",
    "dotenv",
    "ftplib",
    "http",
    "httpx",
    "litellm",
    "multiprocessing",
    "prompt_toolkit",
    "rich",
    "shutil",
    "smtplib",
    "socket",
    "sqlite3",
    "ssl",
    "subprocess",
    "tempfile",
    "tomlkit",
    "typer",
    "urllib",
    "xmlrpc",
}


def _in_package(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def _targets(tree: ast.Module, package: str) -> set[str]:
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = resolve_name("." * node.level + base, package)
            if base != "cairn":
                targets.add(base)
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


def _imports() -> dict[str, set[str]]:
    imports = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        module, package = _module_context(path)
        imports[module] = _targets(ast.parse(path.read_text(encoding="utf-8")), package)
    return imports


def test_agent_and_shared_modules_do_not_depend_on_entrypoints() -> None:
    allowed = {
        "cairn.agent": ("cairn.agent", "cairn.shared"),
        "cairn.shared": ("cairn.shared",),
    }
    violations = []
    for module, targets in _imports().items():
        for owner, dependencies in allowed.items():
            if not _in_package(module, owner):
                continue
            for target in targets:
                if target.startswith("cairn.") and not any(
                    _in_package(target, dependency) for dependency in dependencies
                ):
                    violations.append(f"{module} -> {target}")
    assert not violations, "Invalid Cairn dependencies:\n" + "\n".join(
        sorted(violations)
    )


def test_models_and_policies_stay_free_of_infrastructure_dependencies() -> None:
    imports = _imports()
    assert imports.keys() >= PURE_MODULES
    pure = PURE_MODULES | {
        module
        for module in imports
        if any(_in_package(module, package) for package in PURE_PACKAGES)
    }
    violations = []
    for module in pure:
        for target in imports[module]:
            actual_module = target if target in imports else target.rpartition(".")[0]
            if target.partition(".")[0] in EFFECT_IMPORTS or (
                actual_module in imports and actual_module not in pure
            ):
                violations.append(f"{module} -> {target}")
    assert not violations, "Effect dependencies in models:\n" + "\n".join(
        sorted(violations)
    )


def test_packages_have_no_import_cycles() -> None:
    imports = _imports()
    graph = {module: targets & imports.keys() for module, targets in imports.items()}
    visited: set[str] = set()

    def walk(module: str, path: tuple[str, ...]) -> None:
        assert module not in path, "Circular Cairn imports: " + " -> ".join(
            (*path, module)
        )
        if module not in visited:
            for dependency in sorted(graph[module]):
                walk(dependency, (*path, module))
            visited.add(module)

    for module in sorted(graph):
        walk(module, ())


def test_local_git_and_remote_github_are_independent() -> None:
    imports = _imports()
    remote = "cairn.integrations.github"
    local: tuple[str, ...] = (
        "cairn.development.worktree",
        "cairn.agent.context.git",
        "cairn.shared.git_env",
    )
    for module, targets in imports.items():
        if _in_package(module, remote):
            forbidden = local
        elif module in local:
            forbidden = (remote,)
        else:
            continue
        assert not {
            target
            for target in targets
            if any(_in_package(target, package) for package in forbidden)
        }, module


def test_import_targets_resolve_package_siblings() -> None:
    path = PACKAGE_ROOT / "integrations" / "wechat" / "__init__.py"
    module, package = _module_context(path)
    assert module == package == "cairn.integrations.wechat"
    tree = ast.parse("from . import state\nfrom ..github import transport")
    assert _targets(tree, package) >= {
        "cairn.integrations.wechat.state",
        "cairn.integrations.github.transport",
    }
