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
